"""讀取 config.yaml 與網頁上存的設定，決定檔案存放位置。

程式資料夾只放程式、Python、模型；手機傳來的檔案、搜尋索引、裝置憑證都放在「檔案資料夾」
（預設 桌面\\Xun-Qi 尋棲；英文介面第一次建立時是 桌面\\Xun-Qi），重裝程式不影響檔案與索引。

檔案資料夾裡各個子資料夾的名稱（傳送／Sent、已分類／Sorted…）在第一次建立時依當時的語言決定，
記在 .整理器資料\\library.json（英文是 .xunqi-data\\library.json）；之後切換語言也不會改名或找不到。
"""
import json
import os
import sys
from pathlib import Path

import yaml

from . import lang

ROOT = Path(__file__).resolve().parent.parent  # 程式資料夾
USER_SETTINGS = ROOT / "user_settings.json"    # 網頁「存放位置」寫在這裡
FOLDER_NAME = "Xun-Qi 尋棲"                    # 繁體中文介面的預設資料夾名稱（英文介面是 Xun-Qi，見 i18n 的 lib.folder）
OLD_FOLDER_NAME = "LocalSend 收到的檔案"          # 改名前的預設位置：還在就繼續用，不用搬檔
LIBRARY_FILE = "library.json"                    # 檔案資料夾裡各子資料夾的名稱（建立時決定，之後固定）

# 三種版本（setup.bat 選的，寫在 runtime\edition.txt）
#   lite     輕量版：ONNX int8、不需要 PyTorch，CPU 或 DirectML；OCR mobile；影片每 6 秒一格
#   standard 標準版：PyTorch（有 NVIDIA/Intel 顯示卡就用）；OCR mobile；影片每 4 秒一格
#   flagship 旗艦版：PyTorch；OCR server（最準）；影片每 2 秒一格
EDITIONS = {"lite": ("edition.lite", 6.0, "mobile"), "standard": ("edition.standard", 4.0, "mobile"),
            "flagship": ("edition.flagship", 2.0, "server")}
STATE_NAME = ".整理器資料"                       # 隱藏：索引、憑證、紀錄、分類暫存（英文介面新建的是 .xunqi-data）
# 檔案資料夾裡的名稱（library.json 的 key → i18n 的 lib.<key>）
LIB_KEYS = ("state", "plain", "sorted", "dup", "other", "removed", "sorting", "export_temp", "send_temp",
            "backups", "restore_pending", "unsorted")

DEFAULTS = {
    "alias_plain": "",        # 空白 = 依語言（我的電腦-傳送／My PC - Send）
    "alias_sort": "",         # 空白 = 依語言（我的電腦-傳送並分類／My PC - Send & Sort）
    "port_plain": 53317,
    "port_sort": 53318,
    "mode_select": "phone",
    "web_port": 8765,
    "files_dir": "",          # 空白 = 桌面\Xun-Qi 尋棲（英文介面新建：桌面\Xun-Qi）
    "edition": "",            # 空白 = 用 setup.bat 選的版本
    "categories": {},
    "temperature": 50.0,
    "min_confidence": 0.35,
    "doubt_confidence": 0.5,
    "doubt_close_ratio": 0.6,
    "learn_similarity": 0.82,
    "screenshot_category": "截圖",
    "document_category": "文件收據",
    "unsorted_category": "未分類",   # 實際用的是檔案資料夾 library.json 的 unsorted（建立時依語言決定）
    "near_duplicate": 0.97,
    "date_subfolder": True,
    "embed_dim": 256,
    "video_frames": 4,          # 舊設定（縮圖用）
    "video_seg_every": 0,       # 影片片段：每幾秒抓一格（0 = 依版本 6／4／2 秒）
    "video_seg_max": 150,       # 每部影片最多幾格（太長就平均分配）
    "ocr_enabled": True,        # 讀照片裡的文字（models\\ocr 沒有模型時自動關閉）
    "ocr_max_side": 2000,       # OCR 偵測時圖片長邊最多幾像素
    "ocr_min_score": 0.5,       # 辨識信心低於這個的文字不要
    "ocr_doc_chars": 30,        # 照片裡讀到這麼多字以上……
    "ocr_doc_boost": 0.02,      # ……分類時稍微偏向「文件收據」（0 = 不偏向）
    "model_idle_unload_min": 10,
    "force_cpu": False,
}

# 資料庫裡的特殊類別代碼（所有語言、所有檔案資料夾都固定用這幾個字，畫面上才翻成目前語言）
PLAIN_CAT = "傳送"        # 「傳送」收到的檔案
DUP_CAT = "疑似重複"      # 和以前收過的檔案完全相同
OTHER_CAT = "其他檔案"    # 無法分析的檔案類型
# 舊名稱（繁體中文檔案資料夾的子資料夾名稱，也是舊版程式的常數）
PLAIN_NAME = "傳送"
SORTED_NAME = "已分類"
DUP_NAME = "疑似重複"
OTHER_NAME = "其他檔案"
REMOVED_NAME = "待刪除"   # 你在「重複比較」選擇不要的檔案放這裡，由你自己決定要不要刪
EXPORT_TEMP = "匯出暫存"  # （在 .整理器資料 裡）打包 ZIP 下載用，下載完就清掉


def desktop_dir() -> Path:
    """Windows 真正的桌面位置（桌面被移到 OneDrive 或其他磁碟也找得到）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                            ("d4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")  # FOLDERID_Desktop
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                p = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                if p.exists():
                    return p
        except Exception:
            pass
    p = Path(os.path.expanduser("~")) / "Desktop"
    return p if p.exists() else Path(os.path.expanduser("~"))


def read_user_settings() -> dict:
    try:
        with open(USER_SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def default_names(code: str = None) -> dict:
    """新建檔案資料夾時，各子資料夾的名稱（依語言）。"""
    return {k: lang.t("lib." + k, code) for k in LIB_KEYS}


def state_names():
    """所有語言的隱藏資料夾名稱（找既有的檔案資料夾用）。"""
    out = [STATE_NAME]
    for code in lang.LANGS:
        n = lang.t("lib.state", code)
        if n not in out:
            out.append(n)
    return out


def find_state(folder: Path):
    """這個資料夾是本程式的檔案資料夾嗎？是的話回傳裡面的隱藏資料夾。"""
    for n in state_names():
        if (folder / n).is_dir():
            return folder / n
    return None


def library_names(data: Path, cfg: dict = None) -> dict:
    """讀檔案資料夾的子資料夾名稱。已經存在的檔案資料夾一律沿用原本的名稱：
    有 library.json 就照它；舊版（沒有 library.json）一定是繁體中文名稱。
    全新的資料夾才依目前語言決定（還沒寫檔，load() 建好資料夾後才寫）。"""
    st = find_state(data)
    if st is not None:
        try:
            with open(st / LIBRARY_FILE, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = None
        if isinstance(saved, dict) and saved.get("names"):
            code = lang.normalize(saved.get("lang")) or "zh"
            names = {**default_names(code), **{k: v for k, v in saved["names"].items() if k in LIB_KEYS and v}}
            names["state"] = st.name
            return {"lang": code, "names": names, "new": False, "saved": True}
        code = "en" if st.name == lang.t("lib.state", "en") else "zh"
        names = {**default_names(code), "state": st.name}
        if code == "zh" and cfg is not None:     # 舊版：未分類的名稱以 config.yaml 為準
            names["unsorted"] = str(cfg.get("unsorted_category") or names["unsorted"])
        return {"lang": code, "names": names, "new": False, "saved": False}
    code = lang.current()
    names = default_names(code)
    if code == "zh" and cfg is not None:
        names["unsorted"] = str(cfg.get("unsorted_category") or names["unsorted"])
    return {"lang": code, "names": names, "new": True, "saved": False}


def write_library(c):
    lib = c["library"]
    p = c.state_dir / LIBRARY_FILE
    d = {"lang": lib["lang"], "names": lib["names"],
         "note": "Folder names inside this files folder. Do not edit. / 檔案資料夾裡的子資料夾名稱，請不要修改。"}
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    tmp.replace(p)
    lib["saved"] = True


def save_user_settings(**kw):
    s = read_user_settings()
    s.update(kw)
    with open(USER_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


class Config(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    # ---- 路徑 ----
    @property
    def data(self) -> Path:          # 檔案資料夾（所有收到的檔案都在這裡面）
        p = str(self.get("files_dir") or "").strip() or str(self.get("auto_files_dir") or "").strip()
        if p:
            p = Path(os.path.expandvars(os.path.expanduser(p)))
        else:
            p = default_location()
        if not p.is_absolute():
            p = ROOT / p
        return p.resolve()

    @property
    def names(self) -> dict:         # 檔案資料夾裡的子資料夾名稱（load() 讀好；沒有時用舊的繁體中文名稱）
        lib = self.get("library")
        return lib["names"] if lib else default_names("zh")

    @property
    def inbox_plain(self) -> Path:   # 「傳送」收到的檔案
        return self.data / self.names["plain"]

    @property
    def state_dir(self) -> Path:     # 索引、憑證、紀錄
        return self.data / self.names["state"]

    @property
    def inbox_sort(self) -> Path:    # 「傳送並分類」暫存，處理完會移到 已分類
        return self.state_dir / self.names["sorting"]

    @property
    def sorted_dir(self) -> Path:
        return self.data / self.names["sorted"]

    @property
    def dup_dir(self) -> Path:
        return self.data / self.names["dup"]

    @property
    def other_dir(self) -> Path:
        return self.data / self.names["other"]

    @property
    def removed_dir(self) -> Path:
        return self.data / self.names["removed"]

    @property
    def export_temp(self) -> Path:   # 打包 ZIP 下載用的暫存
        return self.state_dir / self.names["export_temp"]

    @property
    def alias(self):
        """手機上看到的兩台裝置名稱：config.yaml 沒改（空白或原本的預設值）就依語言。"""
        def pick(key, zh_default):
            v = str(self.get(key) or "").strip()
            return v if v and v != zh_default else lang.t("alias." + key.split("_")[1])
        return pick("alias_plain", "我的電腦-傳送"), pick("alias_sort", "我的電腦-傳送並分類")

    @property
    def edition(self) -> str:
        e = str(self.get("edition") or "").strip().lower()
        if not e:
            try:
                e = (ROOT / "runtime" / "edition.txt").read_text(encoding="utf-8").strip().lower()
            except OSError:
                e = ""
        return e if e in EDITIONS else "standard"

    @property
    def edition_name(self) -> str:
        return lang.t(EDITIONS[self.edition][0])

    @property
    def seg_every(self) -> float:
        return float(self.get("video_seg_every") or 0) or EDITIONS[self.edition][1]

    @property
    def ocr_size(self) -> str:
        return EDITIONS[self.edition][2]

    @property
    def model_dir(self) -> Path:
        if self.edition == "lite":
            return ROOT / "models" / "embeddinggemma-2-onnx"
        return ROOT / "models" / "embeddinggemma-2"

    @property
    def ocr_dir(self) -> Path:
        return ROOT / "models" / "ocr"

    @property
    def synced_by_onedrive(self) -> bool:
        return any("onedrive" in part.lower() for part in self.data.parts)


def hide(path: Path):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def default_location() -> Path:
    """沒有指定位置時的檔案資料夾：桌面上已經有的就沿用（Xun-Qi 尋棲 → 舊的 LocalSend 收到的檔案 →
    英文介面建立的 Xun-Qi），都沒有才依目前語言新建。"""
    desk = desktop_dir()
    p = desk / FOLDER_NAME
    if p.exists():
        return p
    for name in (OLD_FOLDER_NAME, lang.t("lib.folder", "en")):
        if find_state(desk / name) is not None:
            return desk / name
    return desk / lang.t("lib.folder")


def load() -> Config:
    lang.ensure_saved()                    # 第一次啟動：記下依 Windows 介面語言選的語言
    cfg = dict(DEFAULTS)
    f = ROOT / "config.yaml"
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            cfg.update(yaml.safe_load(fh) or {})
    us = read_user_settings()
    if us.get("files_dir"):
        cfg["files_dir"] = us["files_dir"]   # 網頁上選的位置優先
    elif us.get("auto_files_dir"):
        cfg["auto_files_dir"] = us["auto_files_dir"]   # 第一次自動選的桌面位置（之後切換語言也不變）
    c = Config(cfg)
    c["library"] = library_names(c.data, cfg)
    try:
        for d in (c.inbox_plain, c.sorted_dir, c.state_dir, c.inbox_sort):
            d.mkdir(parents=True, exist_ok=True)
        if not c["library"]["saved"]:
            write_library(c)
    except OSError as e:
        raise RuntimeError(lang.t("err.make_data_dir", path=c.data, err=e)) from e
    if not str(cfg.get("files_dir") or "").strip() and not us.get("auto_files_dir"):
        try:
            save_user_settings(auto_files_dir=str(c.data))
        except OSError:
            pass
    c["unsorted_category"] = c.names["unsorted"]
    hide(c.state_dir)
    from . import backup, categories
    c["restored"] = backup.apply_pending(c.state_dir, c.names["restore_pending"])   # 上次在網頁上選的「還原」，在開資料庫前換上
    categories.apply_saved(c)                            # 網頁上自訂過的類別（英文的新資料夾會先寫好英文類別）
    return c


def resolve_new_location(raw: str) -> Path:
    """使用者指定的新位置。若資料夾不是空的、也不是本程式的檔案資料夾，就在裡面建一個子資料夾，
    確保程式不會和使用者原本的檔案混在一起。"""
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise ValueError(lang.t("err.loc_empty"))
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        raise ValueError(lang.t("err.loc_relative"))
    p = p.resolve()
    if p == Path(p.anchor):
        p = p / lang.t("lib.folder")   # 不直接用整顆磁碟根目錄
    try:
        p.relative_to(ROOT)
        inside_root = True
    except ValueError:
        inside_root = False
    if inside_root:
        raise ValueError(lang.t("err.loc_in_app"))
    if p.exists():
        if not p.is_dir():
            raise ValueError(lang.t("err.loc_is_file"))
        if any(p.iterdir()) and find_state(p) is None:
            p = p / lang.t("lib.folder")
    return p
