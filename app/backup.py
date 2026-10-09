"""索引備份／還原。

備份＝把「.整理器資料」裡的搜尋索引（分類、確認狀態、學過的範例、向量）、自訂類別、裝置憑證打包成一個 zip，
存到 檔案資料夾\\索引備份\\。不含照片本身。

還原：先把目前的索引自動備份一份，再把選的備份放到「.整理器資料\\還原待套用」，下次啟動程式時才換上
（程式執行中資料庫開著，不能直接替換）。
"""
import datetime
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from . import lang

BACKUP_DIR = "索引備份"          # 繁體中文檔案資料夾的名稱；實際名稱看 cfg.names["backups"]
PENDING = "還原待套用"           # 同上，cfg.names["restore_pending"]
STATE_FILES = ("categories.json", "device_plain.crt", "device_plain.key", "device_sort.crt", "device_sort.key",
               "device_main.crt", "device_main.key")
MAGIC = "LocalSendSorter-index-backup"


def backup_dir(cfg) -> Path:
    return cfg.data / cfg.names["backups"]


def _pending_dir(cfg) -> Path:
    return cfg.state_dir / cfg.names["restore_pending"]


def make(cfg, store, note="") -> Path:
    """建立備份，回傳 zip 路徑。"""
    out_dir = backup_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    pre = lang.t("lib.backup_prefix")
    dst = out_dir / f"{pre}{stamp}{('_' + note) if note else ''}.zip"
    i = 1
    while dst.exists():
        dst = out_dir / f"{pre}{stamp}_{i}.zip"
        i += 1
    with tempfile.TemporaryDirectory(dir=cfg.state_dir) as td:
        snap = Path(td) / "index.db"
        with store.lock:  # SQLite 線上備份：程式執行中也能得到一致的副本
            dst_conn = sqlite3.connect(str(snap))
            try:
                store.conn.backup(dst_conn)
            finally:
                dst_conn.close()
            n = store.conn.execute("SELECT COUNT(*) FROM files WHERE removed=0").fetchone()[0]
        tmp = dst.with_suffix(".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "index.db")
            for name in STATE_FILES:
                p = cfg.state_dir / name
                if p.exists():
                    z.write(p, name)
            z.writestr("info.json", json.dumps({"type": MAGIC, "created": stamp, "files": n,
                                                "folder": str(cfg.data)}, ensure_ascii=False))
        tmp.replace(dst)
    return dst


def listing(cfg):
    d = backup_dir(cfg)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        info = {}
        try:
            with zipfile.ZipFile(p) as z:
                info = json.loads(z.read("info.json"))
        except Exception:
            continue
        if info.get("type") != MAGIC:
            continue
        out.append({"name": p.name, "size": p.stat().st_size, "files": info.get("files"),
                    "created": p.stat().st_mtime})
    return out


def _validate(data: bytes):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = json.loads(z.read("info.json"))
        db = z.read("index.db")
    except Exception:
        raise ValueError(lang.t("err.bk_not_backup"))
    if info.get("type") != MAGIC:
        raise ValueError(lang.t("err.bk_not_backup"))
    if not db.startswith(b"SQLite format 3"):
        raise ValueError(lang.t("err.bk_broken"))
    return z


def stage(cfg, store, data: bytes) -> Path:
    """驗證備份、先自動備份目前的索引，再排定下次啟動時還原。回傳自動備份的位置。"""
    z = _validate(data)
    auto = make(cfg, store, lang.t("lib.backup_auto"))
    pend = _pending_dir(cfg)
    if pend.exists():
        shutil.rmtree(pend)   # 只是上一次排定、還沒套用的還原暫存（程式自己的檔案）
    pend.mkdir()
    for name in ("index.db", *STATE_FILES):
        if name in z.namelist():
            (pend / name).write_bytes(z.read(name))
    if "categories.json" not in z.namelist():
        (pend / "use_config_categories").write_text("1", encoding="utf-8")   # 備份時還沒自訂類別
    (pend / "ready").write_text("1", encoding="utf-8")
    return auto


def stage_named(cfg, store, name: str) -> Path:
    p = backup_dir(cfg) / Path(name).name
    if not p.exists():
        raise ValueError(lang.t("err.bk_missing"))
    with open(p, "rb") as f:
        data = f.read()
    return stage(cfg, store, data)


def cancel(cfg):
    pend = _pending_dir(cfg)
    if pend.exists():
        shutil.rmtree(pend)


def pending(cfg) -> bool:
    return (_pending_dir(cfg) / "ready").exists()


def apply_pending(state_dir: Path, pending_name: str = PENDING):
    """啟動時、開資料庫之前呼叫：把排定的還原換上。只動 .整理器資料 裡程式自己的檔案。"""
    pend = state_dir / pending_name
    if not (pend / "ready").exists():
        return False
    if (pend / "use_config_categories").exists() and (state_dir / "categories.json").exists():
        (state_dir / "categories.json").unlink()   # 回到 config.yaml 的類別（和備份當時一樣）
    for f in pend.iterdir():
        if f.name in ("ready", "use_config_categories"):
            continue
        dst = state_dir / f.name
        for extra in ("-wal", "-shm", "-journal"):   # 舊資料庫的暫存檔
            x = state_dir / (f.name + extra)
            if f.name == "index.db" and x.exists():
                x.unlink()
        os.replace(f, dst)   # 同一個資料夾內，直接換上
    shutil.rmtree(pend)
    return True
