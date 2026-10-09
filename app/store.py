"""SQLite 索引：記錄每個收到的檔案、分類結果與向量。"""
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from .lang import reason as lang_reason, t as _t

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,   -- 相對於檔案資料夾的路徑（整個資料夾搬家也能用）
  name TEXT NOT NULL,
  kind TEXT,            -- image / video / document / other
  category TEXT,        -- 分類結果，或「傳送」
  score REAL,
  sha256 TEXT,
  size INTEGER,
  mode TEXT,            -- plain / sort
  duplicate_of INTEGER,
  received_at REAL,
  vec BLOB
);
CREATE INDEX IF NOT EXISTS idx_sha ON files(sha256);
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY,  -- 每次 LocalSend 傳送一批
  started_at REAL,
  sender TEXT,
  device TEXT,
  mode TEXT,
  count INTEGER         -- 手機說要傳幾個檔案
);
CREATE TABLE IF NOT EXISTS segs(   -- 影片片段：每幾秒一格的畫面向量（搜尋可以找到影片裡的那一秒）
  file_id INTEGER NOT NULL,
  t REAL NOT NULL,
  vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segs ON segs(file_id);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v);
CREATE TABLE IF NOT EXISTS batch_items(   -- 每批手機說要傳的每個檔案，與它目前的狀態
  batch TEXT NOT NULL,
  fid TEXT NOT NULL,       -- LocalSend 的 fileId
  name TEXT,
  size INTEGER,            -- 手機宣告的大小
  status TEXT NOT NULL,    -- expected / received / indexed / failed / error
  file_id INTEGER,         -- 建立索引後對應 files.id
  error TEXT,              -- 只放錯誤訊息
  path TEXT,               -- 實際存到的位置（相對路徑），重啟後重試用
  sha256 TEXT,             -- 收到的內容；索引紀錄被清掉後，靠它找保留的副本
  got_size INTEGER,
  dest TEXT,               -- 搬移前先記下的目的地（搬到一半中斷時，重啟靠它找回檔案）
  PRIMARY KEY(batch, fid)
);
"""


DOUBT_KEYS = ("category", "score", "second", "second_score", "duplicate_of", "corrected", "checked", "mode",
              "kind", "reason")


class Store:
    def __init__(self, db: Path, root: Path = None, cfg=None):
        self.root = Path(root) if root else None
        self.cfg = cfg
        self.conn = sqlite3.connect(str(db), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        for col, typ in (("corrected", "INTEGER DEFAULT 0"), ("reason", "TEXT"), ("taken", "TEXT"),
                         ("batch", "TEXT"), ("reviewed", "INTEGER DEFAULT 0"),
                         ("second", "TEXT"), ("second_score", "REAL"), ("checked", "INTEGER DEFAULT 0"),
                         ("removed", "INTEGER DEFAULT 0"), ("prev_path", "TEXT"), ("sharp", "REAL"),
                         ("lat", "REAL"), ("lon", "REAL"), ("place1", "TEXT"), ("place2", "TEXT"),
                         ("geo", "INTEGER DEFAULT 0"),
                         ("ocr", "TEXT"), ("ocr_n", "TEXT"), ("ocr_done", "INTEGER DEFAULT 0"),
                         ("seg_done", "INTEGER DEFAULT 0")):
            if col not in cols:  # 舊版資料庫自動升級
                self.conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")
        icols = {r[1] for r in self.conn.execute("PRAGMA table_info(batch_items)")}
        for col, typ in (("path", "TEXT"), ("sha256", "TEXT"), ("got_size", "INTEGER"), ("dest", "TEXT")):
            if col not in icols:
                self.conn.execute(f"ALTER TABLE batch_items ADD COLUMN {col} {typ}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_batch ON files(batch)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_taken ON files(taken)")
        self.conn.commit()
        self.lock = threading.Lock()
        self._cache = None  # (ids, matrix)
        self.dim = None     # 目前模型的向量維度（由 processor 設定）
        self._seg_cache = None
        self._migrate_items()

    def _migrate_items(self):
        """舊版收件紀錄升級（可重複執行，只填空白欄位，不覆蓋新版資料）。
        1) 上一版 received 的實際路徑放在 error 欄：確認真的是檔案資料夾內存在的檔案才搬到 path。
           不是有效路徑的，標成 error 並說明，讓批次檢查看得到、不會被當成安全。
        2) 已有 file_id 的：從 files 補上 path / sha256 / got_size（清除重複檔索引後仍能核對保留副本）。"""
        with self.lock:
            rows = self.conn.execute("SELECT batch, fid, error FROM batch_items "
                                     "WHERE status='received' AND path IS NULL").fetchall()
            for bid, fid, err in rows:
                p = self.abspath(err) if err else None
                ok = False
                if p is not None and self.root is not None:
                    try:
                        p.resolve().relative_to(self.root.resolve())
                        ok = p.is_file()
                    except (ValueError, OSError):
                        ok = False
                if ok:
                    self.conn.execute("UPDATE batch_items SET path=?, error=NULL WHERE batch=? AND fid=?",
                                      (err, bid, fid))
                else:
                    self.conn.execute("UPDATE batch_items SET status='error', error=? WHERE batch=? AND fid=?",
                                      (_t("err.legacy_no_path"), bid, fid))
            self.conn.execute(
                "UPDATE batch_items SET "
                "path=COALESCE(path, (SELECT f.path FROM files f WHERE f.id=batch_items.file_id)), "
                "sha256=COALESCE(sha256, (SELECT f.sha256 FROM files f WHERE f.id=batch_items.file_id)), "
                "got_size=COALESCE(got_size, (SELECT f.size FROM files f WHERE f.id=batch_items.file_id)) "
                "WHERE file_id IS NOT NULL AND (path IS NULL OR sha256 IS NULL OR got_size IS NULL) "
                "AND EXISTS (SELECT 1 FROM files f WHERE f.id=batch_items.file_id)")
            self.conn.commit()

    # ------------------------------------------------ 路徑（資料庫存相對路徑）
    def rel(self, path) -> str:
        p = Path(path)
        if self.root is not None and p.is_absolute():
            try:
                return str(p.resolve().relative_to(self.root.resolve()))
            except ValueError:
                pass
        return str(p)

    def abspath(self, stored) -> Path:
        p = Path(stored)
        return p if (p.is_absolute() or self.root is None) else self.root / p

    def add(self, **kw) -> int:
        if "path" in kw:
            kw["path"] = self.rel(kw["path"])
        kw.setdefault("received_at", time.time())
        if isinstance(kw.get("vec"), np.ndarray):
            kw["vec"] = kw["vec"].astype(np.float32).tobytes()
        with self.lock:
            # 編號只增不減：使用者刪掉的檔案編號不會再給新檔案用，瀏覽器才不會顯示快取裡的舊縮圖
            last = self.conn.execute("SELECT v FROM meta WHERE k='last_id'").fetchone()
            mx = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM files").fetchone()[0]
            kw["id"] = max(int(last[0]) if last else 0, mx) + 1
            self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_id',?)", (kw["id"],))
            cols = ",".join(kw)
            cur = self.conn.execute(f"INSERT INTO files({cols}) VALUES({','.join('?' * len(kw))})",
                                    list(kw.values()))
            self.conn.commit()
            self._cache = None
            return cur.lastrowid

    def update(self, fid, **kw):
        if "path" in kw:
            kw["path"] = self.rel(kw["path"])
        if isinstance(kw.get("vec"), np.ndarray):
            kw["vec"] = kw["vec"].astype(np.float32).tobytes()
        sets = ",".join(f"{k}=?" for k in kw)
        with self.lock:
            self.conn.execute(f"UPDATE files SET {sets} WHERE id=?", [*kw.values(), fid])
            self.conn.commit()
            self._cache = None
            if "removed" in kw:
                self._seg_cache = None

    def by_sha(self, sha):
        with self.lock:
            return self.conn.execute("SELECT * FROM files WHERE sha256=? AND removed=0 ORDER BY id LIMIT 1",
                                     (sha,)).fetchone()

    def get(self, fid):
        with self.lock:
            return self.conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()

    def has_path(self, path: str):
        with self.lock:
            return self.conn.execute("SELECT 1 FROM files WHERE path=?", (self.rel(path),)).fetchone() is not None

    # ------------------------------------------------ 有疑慮（建議先人工確認）
    def doubt(self, f: dict, names: dict = None):
        """回傳疑慮原因文字，沒問題回傳 None。手動改過或按過「沒問題」的不算。"""
        if f.get("mode") != "sort" or f.get("corrected") or f.get("checked"):
            return None
        cfg = self.cfg or {}
        unsorted = cfg.get("unsorted_category", "未分類")
        if f.get("duplicate_of"):
            other = (names or {}).get(f["duplicate_of"])
            return _t("doubt.dup_of", name=other) if other else _t("doubt.dup")
        if f.get("category") == unsorted:
            return lang_reason(f.get("reason")) or _t("reason.ai_unsure")
        sc = f.get("score")
        if sc is None or f.get("reason") in ("截圖尺寸", "截圖檔名", "文件檔"):
            return None
        sec, ss = f.get("second"), f.get("second_score")
        if sec and ss is not None and sc > 0 and ss >= cfg.get("doubt_close_ratio", 0.6) * sc:
            return _t("doubt.either", a=f["category"], b=sec)
        if sc < cfg.get("doubt_confidence", 0.5):
            return _t("doubt.low", pct=round(sc * 100))
        return None

    def set_checked(self, fid, value=1):
        with self.lock:
            cur = self.conn.execute("UPDATE files SET checked=? WHERE id=?", (value, fid))
            self.conn.commit()
            return cur.rowcount

    def recent(self, n=50):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id,name,kind,category,score,mode,duplicate_of,received_at,corrected,reason FROM files WHERE removed=0 "
                "ORDER BY id DESC LIMIT ?",
                (n,))]

    # ------------------------------------------------ 批次
    LEGACY = "legacy"  # 舊版收到、沒有批次資訊的檔案

    def add_batch(self, bid, sender, device, mode, count):
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO batches(id,started_at,sender,device,mode,count) "
                              "VALUES(?,?,?,?,?,?)", (bid, time.time(), sender, device, mode, count))
            self.conn.commit()

    def add_batch_items(self, bid, items):
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO batch_items(batch,fid,name,size,status) VALUES(?,?,?,?,'expected')",
                                  [(bid, i["fid"], i.get("name"), i.get("size")) for i in items])
            self.conn.commit()

    def set_item(self, bid, fid, status, file_id=None, error=None, path=None, sha256=None, got_size=None):
        """更新一個收件項目。path/sha256/got_size 只在有值時寫入，錯誤訊息不會蓋掉它們。"""
        if not (bid and fid):
            return
        with self.lock:
            self.conn.execute("UPDATE batch_items SET status=?, file_id=COALESCE(?, file_id), error=?, "
                              "path=COALESCE(?, path), sha256=COALESCE(?, sha256), got_size=COALESCE(?, got_size) "
                              "WHERE batch=? AND fid=?",
                              (status, file_id, error, None if path is None else self.rel(path), sha256, got_size,
                               bid, fid))
            if status == "indexed":   # 舊版項目缺內容識別：從索引補上（已有的不覆蓋）
                self.conn.execute(
                    "UPDATE batch_items SET "
                    "sha256=COALESCE(sha256, (SELECT f.sha256 FROM files f WHERE f.id=batch_items.file_id)), "
                    "got_size=COALESCE(got_size, (SELECT f.size FROM files f WHERE f.id=batch_items.file_id)) "
                    "WHERE batch=? AND fid=? AND file_id IS NOT NULL", (bid, fid))
            self.conn.commit()

    def record_received(self, bid, fid, path, sha256, size):
        """把「收到了、存在哪裡」確實寫進資料庫；寫不進去就丟例外（呼叫端不可回覆手機成功）。"""
        if not (bid and fid):
            return
        with self.lock:
            cur = self.conn.execute(
                "UPDATE batch_items SET status='received', error=NULL, path=?, sha256=?, got_size=? "
                "WHERE batch=? AND fid=?", (self.rel(path), sha256, size, bid, fid))
            if cur.rowcount == 0:
                self.conn.execute(
                    "INSERT INTO batch_items(batch,fid,name,size,status,path,sha256,got_size) "
                    "VALUES(?,?,?,?,'received',?,?,?)", (bid, fid, Path(path).name, size, self.rel(path), sha256, size))
            self.conn.commit()

    def set_dest(self, bid, fid, dest):
        """搬移前先記下目的地。"""
        if not (bid and fid):
            return
        with self.lock:
            self.conn.execute("UPDATE batch_items SET dest=? WHERE batch=? AND fid=?", (self.rel(dest), bid, fid))
            self.conn.commit()

    def retry_items(self):
        """重啟後要接著做的收件項目：已收到但還沒建立索引，或處理失敗的。"""
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT i.*, b.mode AS bmode FROM batch_items i LEFT JOIN batches b ON b.id=i.batch "
                "WHERE i.status IN ('received','error') AND i.path IS NOT NULL")]

    def batch_items(self, bid):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM batch_items WHERE batch=?", (bid,))]

    def get_meta(self, k, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
            self.conn.commit()

    def batches(self, view="pending", category=None, limit=30):
        """view: pending（還有未確認的檔案）/ reviewed（整批已確認）/ all。回傳批次與其檔案。"""
        cols = ("id,name,kind,category,score,mode,duplicate_of,received_at,corrected,reason,reviewed,"
                "second,second_score,checked,sha256,taken,place1,place2")
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(
                f"SELECT {cols}, COALESCE(batch, ?) AS b FROM files WHERE removed=0 ORDER BY id DESC", (self.LEGACY,))]
            info = {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM batches")}
            dups = {r["duplicate_of"] for r in rows if r["duplicate_of"]}
            names = {}
            if dups:
                q = ",".join("?" * len(dups))
                tgt = {r[0]: (r[1], r[2]) for r in self.conn.execute(
                    f"SELECT id,name,sha256 FROM files WHERE id IN ({q})", list(dups))}
                names = {k: v[0] for k, v in tgt.items()}
            else:
                tgt = {}
        for r in rows:
            r["doubt"] = None if r["reviewed"] else self.doubt(r, names)
            d = r["duplicate_of"]
            r["exact"] = bool(d and d in tgt and tgt[d][1] and tgt[d][1] == r["sha256"])
            if r["doubt"] and d and d in names:
                r["doubt"] = _t("doubt.exact" if r["exact"] else "doubt.similar", name=names[d])
            r.pop("sha256", None)
        groups = {}
        for r in rows:
            groups.setdefault(r.pop("b"), []).append(r)
        out = []
        for bid, files in groups.items():
            n_rev = sum(1 for f in files if f["reviewed"])
            state = "reviewed" if n_rev == len(files) else "pending"
            if view == "pending" and state != "pending":
                continue
            if view == "reviewed" and state != "reviewed":
                continue
            if view == "pending":
                files = [f for f in files if not f["reviewed"]]
            if category:
                files = [f for f in files if (f["category"] == category and not f["duplicate_of"])
                         or (category == "疑似重複" and f["duplicate_of"])]
                if not files:
                    continue
            b = info.get(bid, {})
            out.append({"id": bid, "started_at": b.get("started_at") or min(f["received_at"] or 0 for f in files),
                        "sender": b.get("sender") or (_t("batch.legacy") if bid == self.LEGACY else ""),
                        "mode": b.get("mode") or files[0]["mode"], "expected": b.get("count"),
                        "total": len(groups[bid]), "reviewed": n_rev,
                        "doubts": sum(1 for f in files if f["doubt"]),
                        "exact_dups": sum(1 for f in files if f["exact"]), "files": files})
        out.sort(key=lambda b: -(b["started_at"] or 0))
        return out[:limit], len(out)

    def set_reviewed(self, bid, value: int, only_ids=None):
        with self.lock:
            if bid == self.LEGACY:
                cur = self.conn.execute("UPDATE files SET reviewed=? WHERE batch IS NULL", (value,))
            elif only_ids is not None:
                q = ",".join("?" * len(only_ids)) or "NULL"
                cur = self.conn.execute(f"UPDATE files SET reviewed=? WHERE batch=? AND id IN ({q})",
                                        (value, bid, *only_ids))
            else:
                cur = self.conn.execute("UPDATE files SET reviewed=? WHERE batch=?", (value, bid))
            self.conn.commit()
            return cur.rowcount

    def counts(self):
        with self.lock:
            return {r[0] or "未知": r[1] for r in self.conn.execute(
                "SELECT CASE WHEN duplicate_of IS NOT NULL THEN '疑似重複' ELSE category END, COUNT(*) "
                "FROM files WHERE removed=0 GROUP BY 1")}

    def matrix(self, kind=None):
        """回傳 (ids, kinds, 向量矩陣)，給搜尋與相似照比對用。"""
        with self.lock:
            if self._cache is None:
                rows = self.conn.execute("SELECT id, kind, vec FROM files WHERE vec IS NOT NULL AND removed=0").fetchall()
                if rows:
                    ids = np.array([r[0] for r in rows])
                    kinds = np.array([r[1] for r in rows])
                    vs = [np.frombuffer(r[2], dtype=np.float32) for r in rows]
                    dim = self._dim_of(vs)
                    keep = [i for i, v in enumerate(vs) if len(v) == dim]   # 不同維度的舊向量先略過，等重建索引
                    ids, kinds = ids[keep], kinds[keep]
                    m = np.stack([vs[i] for i in keep]) if keep else np.zeros((0, dim), np.float32)
                else:
                    ids, kinds, m = np.array([]), np.array([]), np.zeros((0, 1), np.float32)
                self._cache = (ids, kinds, m)
            ids, kinds, m = self._cache
        if kind is not None and len(ids):
            sel = kinds == kind
            return ids[sel], kinds[sel], m[sel]
        return ids, kinds, m

    def _dim_of(self, vs):
        """目前要用的維度：模型設定的維度；還不知道時取最多的那種。"""
        if self.dim:
            return self.dim
        from collections import Counter
        return Counter(len(v) for v in vs).most_common(1)[0][0]

    def dim_mismatch(self):
        """索引裡有幾個向量的維度和目前模型不同（需要重建索引）。"""
        if not self.dim:
            return 0
        with self.lock:
            n = self.conn.execute("SELECT COUNT(*) FROM files WHERE vec IS NOT NULL AND length(vec)<>?",
                                  (self.dim * 4,)).fetchone()[0]
            n += self.conn.execute("SELECT COUNT(DISTINCT file_id) FROM segs WHERE length(vec)<>?",
                                   (self.dim * 4,)).fetchone()[0]
        return n

    # ------------------------------------------------ 影片片段
    def set_segs(self, fid, segs):
        """segs: [(秒數, 向量)]，整部影片重寫。"""
        with self.lock:
            self.conn.execute("DELETE FROM segs WHERE file_id=?", (fid,))
            self.conn.executemany("INSERT INTO segs(file_id, t, vec) VALUES(?,?,?)",
                                  [(fid, float(t), np.asarray(v, np.float32).tobytes()) for t, v in segs])
            self.conn.execute("UPDATE files SET seg_done=1 WHERE id=?", (fid,))
            self.conn.commit()
            self._seg_cache = None

    def seg_matrix(self):
        """回傳 (檔案 id, 秒數, 向量矩陣)。只含沒被移到待刪除的檔案。"""
        with self.lock:
            if self._seg_cache is None:
                rows = self.conn.execute("SELECT s.file_id, s.t, s.vec FROM segs s JOIN files f ON f.id=s.file_id "
                                         "WHERE f.removed=0").fetchall()
                vs = [np.frombuffer(r[2], dtype=np.float32) for r in rows]
                dim = self._dim_of(vs) if vs else None
                rows = [r for r, v in zip(rows, vs) if len(v) == dim]
                vs = [v for v in vs if len(v) == dim]
                if rows:
                    self._seg_cache = (np.array([r[0] for r in rows]), np.array([r[1] for r in rows], np.float32),
                                       np.stack(vs))
                else:
                    self._seg_cache = (np.array([], int), np.array([], np.float32), np.zeros((0, 1), np.float32))
            return self._seg_cache

    def seg_count(self, fid):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM segs WHERE file_id=?", (fid,)).fetchone()[0]

    # ------------------------------------------------ 文字（OCR 與文件內容）
    def text_hits(self, qn: str, filters: dict, limit=200):
        """文字裡含有 qn（已正規化）的檔案 id（新到舊）。"""
        w, args = self._where(**filters)
        like = qn.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.lock:
            return [r[0] for r in self.conn.execute(
                f"SELECT id FROM files WHERE {w} AND ocr_n LIKE ? ESCAPE '\\' "
                f"ORDER BY COALESCE(taken,'') DESC, id DESC LIMIT ?", [*args, f"%{like}%", int(limit)])]

    def enrich_left(self, doc_only=False):
        """還沒讀文字的圖片／文件，和還沒切片段的影片各有幾個。"""
        oc = self.ENRICH_OCR + (" AND kind='document'" if doc_only else "")
        with self.lock:
            a = self.conn.execute(f"SELECT COUNT(*) FROM files WHERE {oc}").fetchone()[0]
            b = self.conn.execute(f"SELECT COUNT(*) FROM files WHERE {self.ENRICH_SEG}").fetchone()[0]
        return a, b

    # 只處理「傳送並分類」的檔案，和「傳送」但已經建好索引的檔案
    ENRICH_OCR = ("removed=0 AND ocr_done=0 AND kind IN ('image','document') AND (mode='sort' OR vec IS NOT NULL)")
    ENRICH_SEG = ("removed=0 AND seg_done=0 AND kind='video' AND (mode='sort' OR vec IS NOT NULL)")

    def enrich_next(self, which, n=20, doc_only=False):
        cond = self.ENRICH_OCR if which == "ocr" else self.ENRICH_SEG
        if which == "ocr" and doc_only:
            cond += " AND kind='document'"
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                f"SELECT id, path, kind, name FROM files WHERE {cond} ORDER BY id DESC LIMIT ?", (n,))]

    # ------------------------------------------------ 疑似重複整組比較
    def dup_groups(self):
        """把疑似重複整理成一組一組：原檔 + 所有和它重複或很像的檔案。"""
        cols = "id,name,kind,category,mode,size,taken,received_at,sha256,duplicate_of,reviewed,vec,path,batch,sharp"
        with self.lock:
            rows = {r["id"]: dict(r) for r in self.conn.execute(f"SELECT {cols} FROM files WHERE removed=0")}

        def root_of(fid):
            seen = set()
            while rows.get(fid) and rows[fid]["duplicate_of"] and fid not in seen:
                seen.add(fid)
                nxt = rows[fid]["duplicate_of"]
                if nxt not in rows:
                    break
                fid = nxt
            return fid

        groups = {}
        for fid, r in rows.items():
            if r["duplicate_of"]:
                groups.setdefault(root_of(fid), set()).add(fid)
        out = []
        for root, members in groups.items():
            ids = [root] + sorted(members - {root}) if root in rows else sorted(members)
            rv = rows[ids[0]]["vec"]
            rv = np.frombuffer(rv, dtype=np.float32) if rv else None
            items = []
            for i in ids:
                r = rows[i]
                v = np.frombuffer(r["vec"], dtype=np.float32) if r["vec"] else None
                sim = float(v @ rv) if (v is not None and rv is not None and v.shape == rv.shape) else None
                items.append({k: r[k] for k in ("id", "name", "kind", "category", "mode", "size", "taken",
                                                  "received_at", "duplicate_of", "batch", "path", "sharp")}
                             | {"exact": i != ids[0] and bool(r["sha256"]) and r["sha256"] == rows[ids[0]]["sha256"],
                                "sim": None if i == ids[0] else (1.0 if r["sha256"] == rows[ids[0]]["sha256"] else sim),
                                "root": i == ids[0]})
            out.append({"root": ids[0], "files": items,
                        "latest": max(x["received_at"] or 0 for x in items)})
        out.sort(key=lambda g: -g["latest"])
        return out

    def removed_rows(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id,name,kind,category,mode,path,prev_path,duplicate_of FROM files WHERE removed=1 ORDER BY id DESC")]

    def forget(self, fid):
        """檔案已被使用者自己刪掉：只從索引移除這筆紀錄。"""
        with self.lock:
            self.conn.execute("DELETE FROM files WHERE id=?", (fid,))
            self.conn.execute("DELETE FROM segs WHERE file_id=?", (fid,))
            self._seg_cache = None
            self.conn.execute("UPDATE files SET duplicate_of=NULL WHERE duplicate_of=?", (fid,))
            self.conn.commit()
            self._cache = None

    # ------------------------------------------------ 搜尋篩選
    def _where(self, cat="", kind="", date_from="", date_to="", place=""):
        where, args = ["removed=0"], []
        if cat == "疑似重複":
            where.append("duplicate_of IS NOT NULL")
        elif cat == "傳送":
            where.append("mode='plain'")
        elif cat:
            where.append("category=? AND duplicate_of IS NULL AND mode='sort'")
            args.append(cat)
        if kind:
            where.append("kind=?")
            args.append(kind)
        if date_from:
            where.append("taken>=?")
            args.append(date_from[:7])
        if date_to:
            where.append("taken<=?")
            args.append(date_to[:7])
        if place:
            p1, _, p2 = place.partition("|")
            if p1 == "-":
                where.append("place1 IS NULL")
            else:
                where.append("place1=?")
                args.append(p1)
                if p2:
                    where.append("place2=?")
                    args.append(p2)
        return " AND ".join(where), args

    def filter_ids(self, cat="", kind="", date_from="", date_to="", place="", limit=None):
        """依類別／類型／拍攝月份／地點篩選，回傳 id 清單（新到舊）。"""
        w, args = self._where(cat, kind, date_from, date_to, place)
        sql = f"SELECT id FROM files WHERE {w} ORDER BY COALESCE(taken,'') DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.lock:
            return [r[0] for r in self.conn.execute(sql, args)]

    # ------------------------------------------------ 地點、時間軸
    def places(self):
        """所有地點：[{p1, n, sub:[{p2, n}]}]，照片多的排前面。"""
        with self.lock:
            rows = self.conn.execute("SELECT place1, place2, COUNT(*) FROM files WHERE removed=0 AND place1 IS NOT NULL "
                                     "GROUP BY 1,2").fetchall()
            none = self.conn.execute("SELECT COUNT(*) FROM files WHERE removed=0 AND place1 IS NULL AND geo=1 "
                                     "AND kind IN ('image','video')").fetchone()[0]
        out = {}
        for p1, p2, n in rows:
            d = out.setdefault(p1, {"p1": p1, "n": 0, "sub": []})
            d["n"] += n
            if p2:
                d["sub"].append({"p2": p2, "n": n})
        res = sorted(out.values(), key=lambda d: -d["n"])
        for d in res:
            d["sub"].sort(key=lambda x: -x["n"])
        return {"places": res, "none": none}

    TL_COLS = ("id,name,kind,category,score,mode,duplicate_of,received_at,corrected,reason,reviewed,taken,"
               "place1,place2")

    def timeline(self, per_month=12, **filters):
        """依拍攝月份分組：每月的數量、前幾個檔案、最多的地點。"""
        w, args = self._where(**filters)
        with self.lock:
            months = self.conn.execute(f"SELECT COALESCE(taken,'') AS m, COUNT(*) FROM files WHERE {w} "
                                       f"GROUP BY m ORDER BY m DESC", args).fetchall()
            out = []
            for m, n in months:
                cond = "taken=?" if m else "taken IS NULL"
                margs = [m] if m else []
                files = [dict(r) for r in self.conn.execute(
                    f"SELECT {self.TL_COLS} FROM files WHERE {w} AND {cond} ORDER BY id DESC LIMIT ?",
                    [*args, *margs, per_month])]
                top = [r[0] for r in self.conn.execute(
                    f"SELECT COALESCE(place2, place1) AS p, COUNT(*) FROM files WHERE {w} AND {cond} "
                    f"AND place1 IS NOT NULL GROUP BY p ORDER BY 2 DESC LIMIT 3", [*args, *margs])]
                out.append({"month": m, "count": n, "files": files, "places": top})
        return out

    def batch_ids(self, bid):
        """一批裡所有還在的檔案（傳回手機用）。"""
        with self.lock:
            if bid == self.LEGACY:
                cur = self.conn.execute("SELECT id FROM files WHERE batch IS NULL AND removed=0 ORDER BY id")
            else:
                cur = self.conn.execute("SELECT id FROM files WHERE batch=? AND removed=0 ORDER BY id", (bid,))
            return [r[0] for r in cur]

    def rows(self, ids):
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        with self.lock:
            got = {r["id"]: dict(r) for r in self.conn.execute(f"SELECT * FROM files WHERE id IN ({q})", list(ids))}
        return [got[i] for i in ids if i in got]
