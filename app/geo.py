"""照片／影片的拍攝地點：讀 GPS，離線換成地名（不連網路）。

地名資料（app/places.json.gz）來自 GeoNames（CC BY 4.0）：
  台灣：約 4 萬個地名點（各標好所屬的鄉鎮市區）→ 顯示「高雄市 前鎮區」
  其他國家：人口 1.5 萬以上的城市 → 顯示「日本 Osaka」
"""
import gzip
import json
import logging
import re
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("geo")
DATA = Path(__file__).with_name("places.json.gz")
TW_MAX_KM = 10      # 離台灣最近的地名點超過這個距離（例如海上），就不算在台灣
CITY_MAX_KM = 80    # 離最近的城市超過這個距離（海上、深山），只顯示國家

_lock = threading.Lock()
_db = None


def _load():
    global _db
    with _lock:
        if _db is None:
            with gzip.open(DATA, "rt", encoding="utf-8") as f:
                d = json.load(f)
            tw = d["tw"]
            ci = d["cities"]
            pts = np.array(tw["pts"], dtype=np.float64)
            _db = {
                "countries": d["countries"],
                "tw_name": [tuple(n) for n in tw["names"]],
                "tw_idx": pts[:, 2].astype(int),
                "tw_xyz": _xyz(pts[:, 0], pts[:, 1]),
                "ci_name": [(r[0], r[1]) for r in ci],
                "ci_xyz": _xyz(np.array([r[2] for r in ci]), np.array([r[3] for r in ci])),
            }
    return _db


def _xyz(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=1).astype(np.float64)


def _nearest(xyz, p):
    d = xyz @ p
    i = int(np.argmax(d))
    km = float(np.arccos(min(1.0, max(-1.0, d[i])))) * 6371.0
    return i, km


def place_of(lat: float, lon: float):
    """回傳 (第一層, 第二層)：台灣＝(縣市, 鄉鎮市區)，國外＝(國家, 城市)。找不到回傳 (None, None)。"""
    if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None, None
    db = _load()
    p = _xyz(np.array([lat]), np.array([lon]))[0]
    ti, tkm = _nearest(db["tw_xyz"], p)
    ci, ckm = _nearest(db["ci_xyz"], p)
    if tkm <= TW_MAX_KM and tkm <= ckm:
        return db["tw_name"][db["tw_idx"][ti]]
    if ckm > CITY_MAX_KM:
        return None, None      # 海上或離城市很遠：不猜
    name, cc = db["ci_name"][ci]
    return db["countries"].get(cc, cc), name


# ------------------------------------------------ 讀 GPS
def _ratio(v):
    try:
        return float(v)
    except TypeError:
        n, d = v
        return float(n) / float(d) if d else 0.0


def _dms(v, ref):
    d, m, s = (_ratio(x) for x in v)
    x = d + m / 60 + s / 3600
    return -x if str(ref).upper().startswith(("S", "W")) else x


def image_gps(path: Path):
    from PIL import Image
    with Image.open(path) as im:  # 立刻關檔（Windows 鎖檔）
        g = im.getexif().get_ifd(0x8825)
    if not g or 2 not in g or 4 not in g:
        return None
    lat, lon = _dms(g[2], g.get(1, "N")), _dms(g[4], g.get(3, "E"))
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None
    return lat, lon


_ISO6709 = re.compile(rb"([+-]\d{1,2}\.\d{2,})([+-]\d{1,3}\.\d{2,})(?:[+-]\d+(?:\.\d+)?)?/")


def video_gps(path: Path):
    """iPhone／Android 影片把位置存成「+22.6273+120.3014+010.000/」，放在檔頭的 moov 區塊裡。"""
    m = _ISO6709.search(read_moov(path) or b"")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if abs(lat) > 90 or abs(lon) > 180 or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None
    return lat, lon


def video_created(path: Path):
    """影片拍攝時間（moov/mvhd 的建立時間，UTC），讀不到回傳 None。"""
    import datetime
    mv = read_moov(path) or b""
    i = mv.find(b"mvhd")
    if i < 0 or len(mv) < i + 20:
        return None
    ver = mv[i + 4]
    t = int.from_bytes(mv[i + 8:i + 16], "big") if ver == 1 else int.from_bytes(mv[i + 8:i + 12], "big")
    if t <= 0:
        return None
    d = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=t)
    return d if 1990 <= d.year <= 2100 else None


def read_moov(path: Path):
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            h = f.read(16)
            n = int.from_bytes(h[:4], "big")
            typ = h[4:8]
            hdr = 8
            if n == 1:
                n, hdr = int.from_bytes(h[8:16], "big"), 16
            elif n == 0:
                n = size - pos
            if n < hdr:
                return None
            if typ == b"moov":
                if n > 64 * 1024 * 1024:
                    return None
                f.seek(pos + hdr)
                return f.read(n - hdr)
            pos += n
    return None


def gps_of(path: Path, kind: str):
    try:
        if kind == "image":
            return image_gps(path)
        if kind == "video":
            return video_gps(path)
    except Exception as e:  # noqa
        log.debug("cannot read GPS %s: %s", path.name, e)
    return None
