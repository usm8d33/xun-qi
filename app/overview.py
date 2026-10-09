"""首頁統計，以及「可以從手機刪除了」之前的完整性檢查。

這裡只讀檔案，不搬也不刪任何檔案。所有檔案都用 with 開啟，讀完就關掉（Windows 會鎖住沒關的檔案）。
"""
import datetime
import os
import shutil
from pathlib import Path



def home(app):
    st, cfg = app.store, app.cfg
    pend, _ = st.batches("pending", None, 500)
    with st.lock:
        c = st.conn
        total = c.execute("SELECT COUNT(*) FROM files WHERE removed=0").fetchone()[0]
        used = c.execute("SELECT COALESCE(SUM(size),0) FROM files WHERE removed=0").fetchone()[0]
        cats = [dict(r) for r in c.execute(
            "SELECT category AS name, COUNT(*) AS n FROM files WHERE removed=0 AND duplicate_of IS NULL "
            "GROUP BY category ORDER BY n DESC")]
        for cat in cats:   # 封面候選：照片優先、新到舊，只留檔案還在的（使用者可能自己刪了檔案）
            rows = c.execute("SELECT id, path FROM files WHERE removed=0 AND duplicate_of IS NULL AND category=? "
                             "AND kind IN ('image','video') ORDER BY (kind='image') DESC, id DESC LIMIT 12",
                             (cat["name"],)).fetchall()
            cat["covers"] = [r[0] for r in rows if st.abspath(r[1]).exists()][:4]
        lb = c.execute("SELECT b.id, b.started_at, b.sender, b.mode FROM batches b "
                       "WHERE EXISTS (SELECT 1 FROM files f WHERE f.batch=b.id AND f.removed=0) "
                       "ORDER BY b.started_at DESC LIMIT 1").fetchone()
        last = None
        if lb:
            rows = [dict(r) for r in c.execute(
                "SELECT id, kind FROM files WHERE batch=? AND removed=0 ORDER BY id", (lb["id"],))]
            last = {"id": lb["id"], "started_at": lb["started_at"], "sender": lb["sender"] or "",
                    "mode": lb["mode"], "total": len(rows),
                    "images": sum(1 for r in rows if r["kind"] == "image"),
                    "videos": sum(1 for r in rows if r["kind"] == "video"),
                    "others": sum(1 for r in rows if r["kind"] not in ("image", "video")),
                    "thumbs": [r["id"] for r in rows if r["kind"] in ("image", "video")][:6]}
    try:
        free = shutil.disk_usage(str(cfg.data)).free
    except OSError:
        free = None
    return {
        "total": total,
        "pending_batches": len(pend),
        "pending_files": sum(len(b["files"]) for b in pend),
        "doubts": sum(b["doubts"] for b in pend),
        "dup_groups": len(st.dup_groups()),
        "removed": len(app.proc.removed_list()),
        "dim_mismatch": st.dim_mismatch(),
        "last": last,
        "categories": cats,
        "used": used,
        "free": free,
    }


def _taken(path: Path, kind: str):
    """拍攝時間（含時分）：照片看 EXIF，影片看 mvhd，最後用檔案修改時間。回傳本機時間。"""
    try:
        if kind == "image":
            from PIL import Image
            with Image.open(path) as f:
                ex = f.getexif()
                v = ex.get_ifd(0x8769).get(36867) or ex.get(36867) or ex.get(306)
            if v:
                return datetime.datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S")
        if kind == "video":
            from .geo import video_created
            d = video_created(path)
            if d:
                return d.astimezone().replace(tzinfo=None)
    except Exception:  # noqa
        pass
    try:
        return datetime.datetime.fromtimestamp(os.stat(path).st_mtime)
    except OSError:
        return None


def _opens(path: Path, kind: str) -> bool:
    """檔案打得開嗎？只讀，讀完立刻關閉。"""
    if kind == "image":
        from PIL import Image
        with Image.open(path) as f:
            f.load()
        return True
    if kind == "video":
        try:
            import cv2
            cap = cv2.VideoCapture(str(path))
            try:
                ok, _ = cap.read()
            finally:
                cap.release()
            if ok:
                return True
        except Exception:  # noqa
            pass
        from .geo import read_moov  # 網頁解不了的格式（例如 HEVC）至少要有完整的影片標頭
        return bool(read_moov(path))
    with open(path, "rb") as f:
        head = f.read(8)
    if path.suffix.lower() == ".pdf":
        return head.startswith(b"%PDF")
    return len(head) > 0


def _copy_missing(app, it):
    """這個已收到的項目，電腦上還有沒有有效副本？有就回傳 None，沒有就回傳原因。"""
    st, proc = app.store, app.proc
    r = st.get(it["file_id"]) if it["file_id"] else None
    if r is not None and it["sha256"] and r["sha256"] and it["sha256"] != r["sha256"]:
        return "索引的內容和收到的不一致"
    if r is not None and not r["removed"]:
        return None                       # 本身還在（檔案存在、大小、打得開由後面的逐檔檢查負責）
    if r is not None and r["removed"] and not r["duplicate_of"] and st.abspath(r["path"]).is_file():
        return None                       # 你自己移到待刪除、檔案還在：照你的決定
    sha = it["sha256"] or (r["sha256"] if r is not None else None)
    if sha and proc._verified_original(sha, it["got_size"] or (r["size"] if r is not None else None)) is not None:
        return None                       # 重複檔被清掉了，但內容相同的保留副本確實還在
    if r is None:
        return "電腦上已經找不到這個項目"
    return "移到待刪除了，但保留的原檔不在"


def batch_check(app, bid: str):
    st = app.store
    with st.lock:
        if bid == st.LEGACY:
            rows = [dict(r) for r in st.conn.execute(
                "SELECT id,path,name,kind,size FROM files WHERE batch IS NULL AND removed=0")]
        else:
            rows = [dict(r) for r in st.conn.execute(
                "SELECT id,path,name,kind,size FROM files WHERE batch=? AND removed=0", (bid,))]
        b = st.conn.execute("SELECT sender FROM batches WHERE id=?", (bid,)).fetchone()
    bad, times = [], []
    for r in rows:
        p = st.abspath(r["path"])
        why = None
        if not p.exists():
            why = "找不到檔案"
        elif r["size"] and p.stat().st_size != r["size"]:
            why = "檔案大小不對"
        else:
            try:
                if not _opens(p, r["kind"]):
                    why = "打不開"
            except Exception:  # noqa
                why = "打不開"
        if why:
            bad.append({"id": r["id"], "name": r["name"], "why": why})
            continue
        if r["kind"] in ("image", "video"):
            t = _taken(p, r["kind"])
            if t:
                times.append(t)
    # 手機說要傳的每個檔案，都要已經收到並建立索引；只看已入庫的檔案不夠
    busy, dups, items = 0, [], []
    if bid != st.LEGACY:
        items = st.batch_items(bid)
        WHY = {"expected": "沒有收到", "received": "還在處理", "failed": "傳輸失敗", "error": "處理失敗"}
        for it in items:
            if it["status"] == "indexed":
                # indexed 不代表永遠安全：要確認現在電腦上仍有這個項目的有效副本
                why = _copy_missing(app, it)
                if why:
                    bad.append({"id": it["file_id"], "name": it["name"] or it["fid"], "why": why})
                continue
            if it["status"] == "received":
                busy += 1
            why = WHY.get(it["status"], "狀態不明")
            if it["status"] in ("failed", "error") and it["error"]:
                why += f"（{it['error']}）"
            bad.append({"id": it["file_id"], "name": it["name"] or it["fid"], "why": why})
        with st.lock:
            cnt = st.conn.execute("SELECT count FROM batches WHERE id=?", (bid,)).fetchone()
            have = st.conn.execute("SELECT COUNT(*) FROM files WHERE batch=?", (bid,)).fetchone()[0]
            # 舊版批次（沒有逐檔紀錄）才用 duplicate_of 核對被移到待刪除的重複檔
            dups = [] if items else st.conn.execute(
                "SELECT f.id, f.name, o.path AS opath, o.size AS osize, o.removed AS orem "
                "FROM files f LEFT JOIN files o ON o.id=f.duplicate_of "
                "WHERE f.batch=? AND f.removed=1 AND f.duplicate_of IS NOT NULL", (bid,)).fetchall()
        if items:
            dups = [it for it in items if it["status"] == "indexed"]   # 有逐檔紀錄時，以它為準
        if not items and cnt and cnt[0] and have < cnt[0]:      # 舊版批次沒有逐檔紀錄：至少核對數量
            bad.append({"id": None, "name": f"{cnt[0] - have} 個項目", "why": "沒有收到"})
        for d in ([] if items else dups):
            op = st.abspath(d["opath"]) if d["opath"] else None
            if op is None or d["orem"] or not op.is_file() or (d["osize"] and op.stat().st_size != d["osize"]):
                bad.append({"id": d["id"], "name": d["name"], "why": "移到待刪除了，但保留的原檔不在"})
        if app.proc.batch_pending.get(bid) and not busy:
            busy = app.proc.batch_pending[bid]
            bad.append({"id": None, "name": f"{busy} 個項目", "why": "還在處理"})
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M")  # noqa
    return {
        "busy": busy,
        "ok": not bad and bool(rows or dups),   # 整批都是已確認原檔的重複檔，也可以從手機刪除
        "batch": bid,
        "sender": (b["sender"] if b else "") or "",
        "total": len(items) or len(rows),   # 有逐檔紀錄時以手機原本要傳的數量為準
        "images": sum(1 for r in rows if r["kind"] == "image"),
        "videos": sum(1 for r in rows if r["kind"] == "video"),
        "others": sum(1 for r in rows if r["kind"] not in ("image", "video")),
        "first": fmt(min(times)) if times else None,
        "last": fmt(max(times)) if times else None,
        "bad": bad,
    }
