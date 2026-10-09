"""收到檔案之後的處理：「傳送」只記錄；「傳送並分類」則 分析 → 去重 → 分類 → 搬進「已分類」並建索引。"""
import datetime
import hashlib
import logging
import os
import shutil
import uuid
import zipfile
import queue
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import categories as catmod
from . import extract, geo
from .classify import Classifier
from .ocr import OCR, norm, snippet
from .safefs import TMP_PREFIX, OutsideError, inside, is_tmp, move_exact, move_within, unique_path

log = logging.getLogger("processor")

from .config import DUP_CAT, OTHER_CAT, PLAIN_CAT, ROOT
from .lang import reason as lang_reason, t as _t

FILTER_KEYS = ("cat", "kind", "date_from", "date_to", "place")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


class Processor:
    def __init__(self, cfg, store, embedder, ocr=None):
        self.cfg, self.store, self.emb = cfg, store, embedder
        self.ocr = ocr or OCR(cfg)
        self.clf = Classifier(cfg, store, embedder)
        self.q = queue.Queue()
        self.events = deque(maxlen=200)
        self.errors = deque(maxlen=50)
        self.busy = None
        self.batch_pending = {}  # 批次 -> 還在分類中的檔案數
        self.op_lock = threading.RLock()  # 搬檔＋改索引的整段流程都要拿這把鎖（分類、改類別、移除、還原、清理）
        store.dim = cfg.embed_dim         # 搜尋只用目前維度的向量；不同維度的舊向量等重建索引
        self.geo_left = 0
        self.enrich_paused = False
        self.enrich_now = None
        self.enrich_error = None
        self._fails, self._fail_run = {}, 0
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------ 對外入口
    def event(self, ev: dict):
        ev["t"] = time.time()
        self.events.appendleft(ev)
        if ev.get("type") == "session" and ev.get("batch"):
            self.store.add_batch(ev["batch"], ev.get("sender"), ev.get("device"), ev.get("mode"), ev.get("count"))
            if ev.get("files"):
                self.store.add_batch_items(ev["batch"], ev["files"])
            ev.pop("files", None)
        elif ev.get("type") == "upload_failed":
            self.store.set_item(ev.get("batch"), ev.get("fid"), "failed", error=ev.get("error"))

    def confirm_batch(self, bid: str, value: int = 1):
        """整批確認（或取消確認）。只改顯示狀態，不搬也不刪任何檔案。
        還在分類中的檔案不算進去，分類完會留在「待確認」。"""
        if value and self.batch_pending.get(bid):
            with self.store.lock:
                ids = [r[0] for r in self.store.conn.execute("SELECT id FROM files WHERE batch=?", (bid,))]
            return self.store.set_reviewed(bid, 1, only_ids=ids)
        return self.store.set_reviewed(bid, value)

    def on_file(self, mode, path: Path, meta: dict):
        # 先把「收到了、存在哪裡、內容是什麼」記下來；之後任何一步中斷，重啟都能接著做
        # 這一步失敗會丟例外：接收端就不會回覆手機成功
        self.store.record_received(meta.get("batch"), meta.get("fid"), path, meta.get("sha256"), meta.get("size"))
        if mode == "plain":
            try:
                self._register_plain(path, meta)
            except Exception:  # noqa  紀錄已保存為 received，重啟時會補登記
                log.exception(_t("log.register_retry", name=path.name))
        else:
            if meta.get("batch"):
                self.batch_pending[meta["batch"]] = self.batch_pending.get(meta["batch"], 0) + 1
            self.q.put(("sort", path, meta))

    def _register_plain(self, path: Path, meta: dict):
        kind = extract.kind_of(path)
        taken = f"{extract.taken_date(path, meta.get('modified')):%Y-%m}"
        fid = self.store.add(path=str(path), name=path.name, kind=kind, category=PLAIN_CAT, taken=taken,
                             sha256=meta.get("sha256"), size=meta.get("size"), mode="plain", batch=meta.get("batch"),
                             **self._geo_cols(path, kind))
        self.store.set_item(meta.get("batch"), meta.get("fid"), "indexed", file_id=fid)
        self.event({"type": "file", "name": path.name, "category": PLAIN_CAT})
        return fid

    def requeue_leftovers(self):
        """重啟時接著做：上次收到但沒登記／沒分類完／分類失敗的檔案，回到原本的批次重新處理。"""
        for d in {self.cfg.inbox_sort, self.cfg.inbox_plain}:
            if not d.exists():
                continue
            for p in d.iterdir():   # 程式自己沒收完的暫存檔（不是使用者的檔案）
                if p.is_file() and p.name.startswith(TMP_PREFIX):
                    p.unlink(missing_ok=True)
        n, seen = 0, set()
        for it in self.store.retry_items():
            meta = {"batch": it["batch"], "fid": it["fid"], "sha256": it["sha256"], "size": it["got_size"]}
            with self.store.lock:   # 其實已經登記過（中斷在更新狀態之前）→ 直接補上狀態，不重複登記
                row = self.store.conn.execute(
                    "SELECT id FROM files WHERE batch=? AND (? IS NULL OR sha256=?) "
                    "AND (path=? OR path=? OR (sha256=? AND sha256 IS NOT NULL "
                    "AND id NOT IN (SELECT file_id FROM batch_items WHERE batch=? AND file_id IS NOT NULL))) "
                    "ORDER BY (path<>? AND path<>?) LIMIT 1",
                    (it["batch"], it["sha256"], it["sha256"], it["path"], it["dest"], it["sha256"], it["batch"],
                     it["path"], it["dest"])
                ).fetchone()
            if row is not None:
                self.store.set_item(it["batch"], it["fid"], "indexed", file_id=row[0])
                continue
            want = it["sha256"]
            ok = lambda q: q.is_file() and (not want or sha256_of(q) == want)   # 內容要和收到的一樣
            p = self.store.abspath(it["path"])
            if not ok(p) and it["dest"] and ok(self.store.abspath(it["dest"])):
                # 已經搬到分類資料夾、但還沒建立索引就中斷：先記下新位置，再搬回收件資料夾重新處理
                d = self.store.abspath(it["dest"])
                back = unique_path(self.cfg.inbox_sort, d.name)
                self.store.set_item(it["batch"], it["fid"], it["status"], error=it["error"], path=back)
                try:
                    move_exact(d, back, self.cfg.data)
                    p = back
                except OSError as e:
                    log.warning(_t("log.recover_failed", name=d.name, err=e))
                    continue
            if not ok(p):
                if p.is_file() or it["dest"]:   # 位置被別的內容占用／目的地內容不符：不能當成這個收件
                    self.store.set_item(it["batch"], it["fid"], "error", error=_t("err.no_matching_file"))
                continue
            seen.add(p.resolve())
            if it["bmode"] == "plain" or inside(p, self.cfg.inbox_plain):
                try:
                    self._register_plain(p, meta)
                except Exception as e:  # noqa
                    log.exception(_t("log.register_failed", name=p.name))
                    self.store.set_item(it["batch"], it["fid"], "error", error=str(e))
            else:
                self.batch_pending[it["batch"]] = self.batch_pending.get(it["batch"], 0) + 1
                self.q.put(("sort", p, meta))
            n += 1
        for p in sorted(self.cfg.inbox_sort.iterdir()):
            if p.is_file() and not is_tmp(p) and p.resolve() not in seen:
                self.q.put(("sort", p, {}))
                n += 1
        if n:
            log.info(_t("log.requeue", n=n))

    def reindex_mismatch(self):
        """向量維度和目前模型不同的檔案重新算向量（影片片段一起重算）。"""
        d = self.cfg.embed_dim * 4
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id, path FROM files WHERE removed=0 AND ((vec IS NOT NULL AND length(vec)<>?) OR "
                "id IN (SELECT file_id FROM segs WHERE length(vec)<>?))", (d, d)).fetchall()
        for fid, p in rows:
            self.q.put(("index", self.store.abspath(p), {"id": fid}))
        return len(rows)

    def index_plain(self):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id, path FROM files WHERE mode='plain' AND vec IS NULL AND removed=0").fetchall()
        for fid, p in rows:
            self.q.put(("index", self.store.abspath(p), {"id": fid}))
        return len(rows)

    def reclassify_all(self):
        """依目前 config 的類別與你改過的範例，重新分類待確認的照片/影片（不重算向量，很快）。
        手動改過的、整批確認過的都不會動。"""
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id, path FROM files WHERE mode='sort' AND vec IS NOT NULL AND corrected=0 "
                "AND reviewed=0 AND removed=0 AND duplicate_of IS NULL AND kind IN ('image','video')").fetchall()
        self.clf.reset()
        for fid, p in rows:
            self.q.put(("reclass", self.store.abspath(p), {"id": fid}))
        return len(rows)

    def set_category(self, fid: int, category: str):
        """使用者手動改分類：搬檔案（只在檔案資料夾內），並記成學習範例。"""
        with self.op_lock:
            return self._set_category(fid, category)

    def _set_category(self, fid: int, category: str):
        r = self.store.get(fid)
        if r is None or r["mode"] != "sort":
            raise ValueError(_t("err.only_sorted"))
        if category not in self.cfg.categories and category != self.cfg.unsorted_category:
            raise ValueError(_t("err.no_such_cat"))
        src = self.store.abspath(r["path"])
        dst = move_within(src, self._folder(category, r["taken"]), self.cfg.data)
        self.store.update(fid, path=str(dst), category=category, corrected=1, duplicate_of=None,
                          reason="你手動分類", score=1.0)
        self.event({"type": "file", "name": dst.name, "category": category, "note": "manual"})
        return str(dst)

    def check(self, fid: int, value: int = 1):
        """使用者看過，覺得 AI 分得沒問題：從「建議先確認」移到一般區。不搬檔案。"""
        return self.store.set_checked(fid, value)

    # ------------------------------------------------ 疑似重複整組比較
    def _valid_cat(self, c):
        return c in self.cfg.categories or c == self.cfg.unsorted_category

    def _remove(self, r):
        """移到「待刪除」資料夾（不刪除，由使用者自己決定）。"""
        with self.op_lock:
            return self._remove_locked(r)

    def _remove_locked(self, r):
        r = self.store.get(r["id"]) or r     # 拿到鎖後重新讀一次，避免用到過期的紀錄
        if r["removed"]:
            return self.store.abspath(r["path"])
        src = self.store.abspath(r["path"])
        if not src.exists():
            raise FileNotFoundError(_t("err.file_not_found_name", name=r["name"]))
        dst = move_within(src, self.cfg.removed_dir, self.cfg.data)
        self.store.update(r["id"], path=str(dst), prev_path=self.store.rel(src), removed=1)
        return dst

    def dup_apply(self, keep, remove):
        """整組比較的結果：keep 留下、remove 移到「待刪除」。留下的檔案搬回它的類別資料夾。"""
        with self.op_lock:
            return self._dup_apply(keep, remove)

    def _dup_apply(self, keep, remove):
        keep, remove = [int(i) for i in keep], [int(i) for i in remove]
        if not keep:
            raise ValueError(_t("err.keep_one"))
        if set(keep) & set(remove):
            raise ValueError(_t("err.keep_and_remove"))
        rows = {}
        for i in keep + remove:
            r = self.store.get(i)
            if r is None or r["removed"]:
                raise ValueError(_t("err.gone_reload"))
            rows[i] = r
        for i in keep:   # 留下的檔案一定要真的還在，才可以移走其他的
            if not self.store.abspath(rows[i]["path"]).is_file():
                raise ValueError(_t("err.keep_missing", name=rows[i]["name"]))
        moved = 0
        for i in remove:
            self._remove(rows[i])
            moved += 1
        first = keep[0]
        for i in keep:
            r = self.store.get(i)
            if not r["duplicate_of"]:
                continue
            tgt = self.store.get(r["duplicate_of"])
            vec = r["vec"]
            if vec is None and tgt is not None and tgt["vec"] is not None and tgt["sha256"] == r["sha256"]:
                vec = tgt["vec"]  # 完全相同的檔案沒算過向量，直接沿用原檔的
            cat = r["category"]
            if not self._valid_cat(cat):
                cat = tgt["category"] if (tgt is not None and self._valid_cat(tgt["category"])) else None
            if cat is None and vec is not None and r["kind"] in ("image", "video"):
                cat = self.clf.classify(np.frombuffer(vec, dtype=np.float32), self.store.abspath(r["path"]),
                                        r["kind"])[0]
            if cat is None:
                cat = self.cfg.document_category if r["kind"] == "document" else (
                    OTHER_CAT if r["kind"] == "other" else self.cfg.unsorted_category)
            kw = dict(duplicate_of=None, category=cat, checked=1)
            if vec is not None and r["vec"] is None:
                kw["vec"] = np.frombuffer(vec, dtype=np.float32)
            if r["mode"] == "sort":
                dst = move_within(self.store.abspath(r["path"]), self._folder(cat, r["taken"]), self.cfg.data)
                kw["path"] = str(dst)
            self.store.update(i, **kw)
        # 指向被移走檔案的其他重複，改成指向留下的第一個
        if remove:
            q = ",".join("?" * len(remove))
            with self.store.lock:
                self.store.conn.execute(f"UPDATE files SET duplicate_of=? WHERE duplicate_of IN ({q}) AND removed=0 "
                                        f"AND id<>?", (first, *remove, first))
                self.store.conn.commit()
                self.store._cache = None
        self.event({"type": "dup", "kept": len(keep), "removed": moved})
        return {"kept": len(keep), "removed": moved}

    def clear_exact(self, bid):
        """把這批裡和舊檔「完全相同」的檔案移到「待刪除」。"""
        batch = [b for b in self.store.batches("all", None, 100000)[0] if b["id"] == bid]
        ids = [f["id"] for f in batch[0]["files"] if f["exact"]] if batch else []
        n = 0
        with self.op_lock:
            for i in ids:
                r = self.store.get(i)
                if r is None or r["removed"]:
                    continue
                orig = self.store.get(r["duplicate_of"]) if r["duplicate_of"] else None
                if not self._verified(orig, r["sha256"]):
                    # 原檔已經不在或被改過：這份才是唯一的，改成一般檔案留下
                    self._dup_apply([i], [])
                    continue
                self._remove(r)
                n += 1
        return n

    def restore(self, fid):
        """從「待刪除」移回原本的資料夾。搬檔和更新索引是同一段，期間不讓其他流程清理索引。"""
        with self.op_lock:
            return self._restore(fid)

    def _restore(self, fid):
        r = self.store.get(fid)
        if r is None or not r["removed"]:
            raise ValueError(_t("err.not_in_removed"))
        src = self.store.abspath(r["path"])
        if not src.exists():
            self.store.forget(fid)
            raise FileNotFoundError(_t("err.already_deleted"))
        back = self.store.abspath(r["prev_path"]).parent if r["prev_path"] else self.cfg.data
        try:
            dst = move_within(src, back, self.cfg.data)
        except PermissionError as e:
            if isinstance(e, OutsideError):
                raise
            raise PermissionError(_t("err.in_use_retry")) from e
        dup = r["duplicate_of"]
        if dup:
            t = self.store.get(dup)
            if t is None or t["removed"]:
                dup = None
        kw = dict(path=str(dst), removed=0, prev_path=None, duplicate_of=dup)
        if dup is None and r["category"] == DUP_CAT:   # 原檔已不在，就不算重複了
            kw["category"] = self.cfg.unsorted_category
            kw["path"] = str(move_within(dst, self._folder(kw["category"], r["taken"]), self.cfg.data))
        self.store.update(fid, **kw)
        return kw["path"]

    def removed_list(self):
        """待刪除清單；使用者已經自己刪掉的檔案，順便從索引移除。"""
        out, gone = [], []
        for r in self.store.removed_rows():
            if self.store.abspath(r["path"]).exists():
                out.append(r)
            else:
                gone.append(r["id"])
        # 清理索引要拿操作鎖；拿不到（正在分類、還原或移除）就下次再清，不會誤刪正在搬的紀錄
        if gone and self.op_lock.acquire(blocking=False):
            try:
                for i in gone:
                    r = self.store.get(i)
                    if r is not None and r["removed"] and not self.store.abspath(r["path"]).exists():
                        self.store.forget(i)
            finally:
                self.op_lock.release()
        return out

    @property
    def pending(self):
        return self.q.qsize() + (1 if self.busy else 0)

    # ------------------------------------------------ 背景工作
    def _worker(self):
        while True:
            job, path, meta = self.q.get()
            self.busy = path.name
            self.op_lock.acquire()
            try:
                if not path.exists():
                    continue
                if job == "sort":
                    fid = self._sort(path, meta)
                    self.store.set_item(meta.get("batch"), meta.get("fid"), "indexed", file_id=fid)
                elif job == "reclass":
                    self._reclass(meta["id"])
                else:
                    extra = {}
                    vec = self._embed(path, extract.kind_of(path), extra)
                    if vec is not None:
                        self.store.update(meta["id"], vec=vec)
                    if extra.get("segs"):
                        self.store.set_segs(meta["id"], extra["segs"])
            except Exception as e:  # noqa
                log.exception(_t("log.process_failed", name=path.name))
                if job == "sort":
                    try:   # 資料庫還寫不進去也不能讓 worker 停掉；收件狀態留著，重啟時會再處理
                        self.store.set_item(meta.get("batch"), meta.get("fid"), "error", error=str(e))
                    except Exception:  # noqa
                        log.exception(_t("log.record_error_failed", name=path.name))
                self.errors.appendleft({"t": time.time(), "name": path.name, "error": str(e)})
                self.event({"type": "error", "name": path.name, "error": str(e)})
            finally:
                self.op_lock.release()
                self.busy = None
                b = meta.get("batch") if job == "sort" else None
                if b and b in self.batch_pending:
                    self.batch_pending[b] -= 1
                    if self.batch_pending[b] <= 0:
                        self.batch_pending.pop(b, None)

    def _embed(self, path: Path, kind: str, extra: dict = None):
        """回傳向量。影片會把片段 [(秒數, 向量)] 放進 extra["segs"]；文件會把文字放進 extra["text"]。"""
        extra = {} if extra is None else extra
        if kind == "image":
            return self.emb.images([extract.load_image(path)])[0]
        if kind == "video":
            segs = self._segments(path)
            if not segs:
                return None
            extra["segs"] = segs
            v = np.stack([x for _, x in segs]).mean(axis=0)
            return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)
        if kind == "document":
            text = extract.document_text(path, limit=20000)
            extra["text"] = text
            return self.emb.text([f"{path.stem}\n{text[:6000]}" if text else path.stem], "document")[0]
        return None

    def _segments(self, path: Path):
        """影片片段：每 video_seg_every 秒一格，算出每格的向量。"""
        frames = extract.video_segments(path, float(self.cfg.seg_every), int(self.cfg.video_seg_max or 150))
        if not frames:
            return []
        out = []
        for k in range(0, len(frames), 16):          # 一次 16 格，避免顯示卡記憶體不夠
            chunk = frames[k:k + 16]
            vs = self.emb.images([im for _, im in chunk])
            out += [(t, v.astype(np.float32)) for (t, _), v in zip(chunk, vs)]
        return out

    def _text(self, path: Path, kind: str, doc_text=None):
        """照片：OCR；文件：本身的文字（掃描成圖片的 PDF 才用 OCR 讀第 1 頁）。
        回傳 (文字, 是否完成)。OCR 沒有模型時回傳 ("", False)，之後放了模型會在背景補做。"""
        if kind == "document":
            text = doc_text if doc_text is not None else extract.document_text(path, limit=20000)
            if not text and path.suffix.lower() == ".pdf":
                if not self.ocr.available:
                    return "", False
                im = extract.pdf_scan_image(path)
                if im is not None:
                    im.thumbnail((4000, 4000))
                    text = self.ocr.read(im, fake_path=path)
            return (text or "")[:20000], True
        if kind == "image":
            if not self.ocr.available:
                return "", False
            return self.ocr.read(extract.load_image_full(path), fake_path=path)[:20000], True
        return "", True

    def _text_cols(self, text, done):
        if not done:
            return {}
        return {"ocr": text or None, "ocr_n": norm(text) if text else None, "ocr_done": 1}

    def _folder(self, category, taken=None):
        if category == OTHER_CAT:
            return self.cfg.other_dir
        folder = self.cfg.sorted_dir / category
        if self.cfg.date_subfolder and taken:
            folder = folder / taken
        return folder

    def _reclass(self, fid):
        r = self.store.get(fid)
        if r is None or r["corrected"] or r["reviewed"] or r["removed"] or r["vec"] is None \
                or len(r["vec"]) != self.cfg.embed_dim * 4:
            return
        vec = np.frombuffer(r["vec"], dtype=np.float32)
        src = self.store.abspath(r["path"])
        cat, conf, reason, second, second_score = self.clf.classify(vec, src, r["kind"], exclude_id=fid,
                                                                    text_len=len(r["ocr_n"] or ""))
        if cat != r["category"]:
            dst = move_within(src, self._folder(cat, r["taken"]), self.cfg.data)
            self.store.update(fid, path=str(dst), category=cat, score=conf, reason=reason,
                              second=second, second_score=second_score, checked=0)
            log.info(_t("log.reclassified", name=dst.name, old=r["category"], new=cat))
        else:
            self.store.update(fid, score=conf, reason=reason, second=second, second_score=second_score)

    def _sort(self, path: Path, meta: dict):
        cfg = self.cfg
        sha = meta.get("sha256") or sha256_of(path)
        size = meta.get("size") or path.stat().st_size
        kind = extract.kind_of(path)
        taken = f"{extract.taken_date(path, meta.get('modified')):%Y-%m}"

        # 1) 完全相同的檔案（以前收過）
        same = self._verified_original(sha, size)
        if same is not None:
            dst = self._move_logged(path, cfg.dup_dir, meta)
            fid = self.store.add(path=str(dst), name=dst.name, kind=kind, category=DUP_CAT, sha256=sha, size=size,
                                 mode="sort", duplicate_of=same["id"], taken=taken, reason=f"與 {same['name']} 完全相同",
                                 batch=meta.get("batch"), **self._geo_cols(dst, kind))
            self.event({"type": "file", "name": dst.name, "category": DUP_CAT})
            return fid

        # 2) 向量（影片同時切好片段）
        extra = {}
        vec = self._embed(path, kind, extra)

        # 2b) 文字：照片 OCR、文件內容（失敗不影響分類，之後背景會再試）
        tcols = {}
        try:
            text, done = self._text(path, kind, extra.get("text"))
            tcols = self._text_cols(text, done)
        except Exception as e:  # noqa
            log.warning(_t("log.text_failed", name=path.name, err=e))
            text = ""

        # 3) 分類
        conf, reason, second, second_score = None, "", None, None
        if kind == "document":
            category, reason = cfg.document_category, "文件檔"
        elif vec is None:
            category, reason = OTHER_CAT, "無法分析的檔案類型"   # 原因存成固定代碼，顯示時翻譯（lang.reason）
        else:
            category, conf, reason, second, second_score = self.clf.classify(vec, path, kind,
                                                                             text_len=len(norm(text or "")))

        # 4) 相似照（連拍）
        dup_of = None
        if kind == "image" and vec is not None:
            ids, _, m = self.store.matrix("image")
            if len(ids) and m.shape[1] == vec.shape[0]:
                s = m @ vec
                for j in np.argsort(-s)[:20]:          # 很像的舊檔要真的還在電腦上才算
                    if float(s[j]) < cfg.near_duplicate:
                        break
                    o = self.store.get(int(ids[j]))
                    if o is not None and self.store.abspath(o["path"]).is_file():
                        dup_of = int(ids[j])
                        break

        gcols = self._geo_cols(path, kind)

        # 5) 搬到目的資料夾（只在檔案資料夾內搬）
        folder = cfg.dup_dir if dup_of is not None else self._folder(category, taken)
        dst = self._move_logged(path, folder, meta)
        fid = self.store.add(path=str(dst), name=dst.name, kind=kind, category=category, score=conf, sha256=sha,
                             size=size, mode="sort", duplicate_of=dup_of, vec=vec, taken=taken, reason=reason,
                             second=second, second_score=second_score, batch=meta.get("batch"), **gcols, **tcols)
        if extra.get("segs"):
            self.store.set_segs(fid, extra["segs"])
        self.event({"type": "file", "name": dst.name, "category": DUP_CAT if dup_of else category,
                    "score": None if conf is None else round(conf, 2)})
        log.info(_t("log.sorted", name=dst.name, cat=category, why=lang_reason(reason)) + (_t("log.dup_mark") if dup_of else ""))
        return fid

    def _move_logged(self, path: Path, folder: Path, meta: dict) -> Path:
        """先把目的地寫進收件紀錄，再搬（不覆蓋）。搬完到建立索引之間中斷，重啟能從目的地找回檔案。"""
        for _ in range(1000):
            dst = unique_path(folder, path.name)
            self.store.set_dest(meta.get("batch"), meta.get("fid"), dst)
            try:
                move_exact(path, dst, self.cfg.data)
                return dst
            except FileExistsError:
                continue
        raise FileExistsError(_t("err.no_free_name", name=path.name))

    def _verified(self, r, sha=None, size=None) -> bool:
        """這筆索引指向的檔案真的還在、大小與內容都對（才能當作「保留的原檔」）。"""
        if r is None or r["removed"]:
            return False
        p = self.store.abspath(r["path"])
        try:
            if not p.is_file():
                return False
            want = size if size is not None else r["size"]
            if want and p.stat().st_size != want:
                return False
            want_sha = sha or r["sha256"]
            return not want_sha or sha256_of(p) == want_sha
        except OSError:
            return False

    def _verified_original(self, sha, size):
        """找一個確實存在、內容相同的舊檔；索引指向的檔案不見了或被改過就不算。"""
        with self.store.lock:
            rows = self.store.conn.execute("SELECT * FROM files WHERE sha256=? AND removed=0 ORDER BY id",
                                           (sha,)).fetchall()
        for r in rows:
            if self._verified(r, sha, size):
                return r
        return None

    # ------------------------------------------------ 搜尋
    def _row_out(self, r, score=None, t=None):
        return {"id": r["id"], "name": r["name"], "kind": r["kind"], "category": r["category"],
                "mode": r["mode"], "duplicate_of": r["duplicate_of"], "taken": r["taken"],
                "place1": r["place1"], "place2": r["place2"], "reason": r["reason"], "corrected": r["corrected"],
                "score": None if score is None else round(float(score), 3), "sim": score is not None,
                "t": t, "has_text": bool(r["ocr"])}

    def _seg_best(self, qv):
        """每部影片最符合的那一格：{檔案 id: (分數, 秒數)}。"""
        sid, st, sm = self.store.seg_matrix()
        if not len(sid) or sm.shape[1] != qv.shape[0]:
            return {}
        ss = sm @ qv
        order = np.lexsort((-ss, sid))               # 依影片分組，組內分數高的在前
        so = sid[order]
        first = np.ones(len(order), bool)
        first[1:] = so[1:] != so[:-1]
        top = order[first]
        return {int(f): (float(a), float(b)) for f, a, b in zip(sid[top], ss[top], st[top])}

    def _ranked(self, qv, filters, k, exclude=()):
        ids, _, m = self.store.matrix()
        if not len(ids):
            return []
        exclude = {exclude} if isinstance(exclude, int) else set(exclude or ())
        allowed = set(self.store.filter_ids(**filters)) if any(filters.values()) else None
        sims = (m @ qv).astype(np.float32)
        best_t = {}
        pos = {int(f): i for i, f in enumerate(ids)}
        for fid, (sc, t) in self._seg_best(qv).items():   # 影片用最符合的那一格的分數
            i = pos.get(fid)
            if i is not None and sc > sims[i]:
                sims[i] = sc
                best_t[fid] = t
        out = []
        for i in np.argsort(-sims):
            fid = int(ids[i])
            if fid in exclude or (allowed is not None and fid not in allowed):
                continue
            r = self.store.get(fid)
            if r is not None:
                out.append(self._row_out(r, sims[i], best_t.get(fid)))
            if len(out) >= k:
                break
        return out

    def text_hits(self, text, filters, limit=200):
        """文字完全符合（OCR 讀到的字、文件內容），繁簡、全形半形、空白都不影響。"""
        qn = norm(text)
        if len(qn) < 2:
            return []
        out = []
        for r in self.store.rows(self.store.text_hits(qn, filters, limit)):
            o = self._row_out(r)
            o["hit"] = "text"
            o["snip"] = snippet(r["ocr"] or "", text)
            out.append(o)
        return out

    def search(self, text: str, k=60, limit=300, **filters):
        """一句話搜尋，可加類別／類型／月份篩選；沒有文字時只依篩選條件列出（新到舊）。
        有文字時：文字完全符合的排最前面，接著是 AI 語意搜尋的結果。"""
        filters = {key: filters.get(key) or "" for key in FILTER_KEYS}
        if not text:
            return [self._row_out(r) for r in self.store.rows(self.store.filter_ids(**filters, limit=limit))]
        hits = self.text_hits(text, filters)
        try:
            sem = self._ranked(self.emb.text([text], "query")[0], filters, k, exclude=[h["id"] for h in hits])
        except Exception:
            if hits:            # 模型載入失敗時，至少還有文字搜尋的結果
                return hits
            raise
        return hits + sem

    def similar(self, fid: int, k=60, **filters):
        """以圖找圖：用這個檔案的向量找最像的檔案（不用載入模型）。"""
        r = self.store.get(fid)
        if r is None or r["vec"] is None:
            raise ValueError(_t("err.no_index_similar"))
        if len(r["vec"]) != self.cfg.embed_dim * 4:
            raise ValueError(_t("err.old_index_similar"))
        filters = {key: filters.get(key) or "" for key in FILTER_KEYS}
        return self._ranked(np.frombuffer(r["vec"], dtype=np.float32), filters, k, exclude=[fid])

    def similar_image(self, data: bytes, k=60, **filters):
        """拖進搜尋框的圖片：在記憶體裡算向量找相似的，不會存檔，也不會加進索引。"""
        import io
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as f:
            im = ImageOps.exif_transpose(f).convert("RGB")
        im.thumbnail((extract.MAX_SIDE, extract.MAX_SIDE))
        filters = {key: filters.get(key) or "" for key in FILTER_KEYS}
        return self._ranked(self.emb.images([im])[0], filters, k)

    def sharpness(self, fid, path):
        """清晰度（算一次就存起來）。"""
        r = self.store.get(fid)
        if r is not None and r["sharp"] is not None:
            return r["sharp"]
        try:
            v = extract.sharpness(path)
        except Exception:
            return None
        self.store.update(fid, sharp=v)
        return v

    # ------------------------------------------------ 地點
    def _geo_cols(self, path: Path, kind: str):
        if kind not in ("image", "video"):
            return {"geo": 1}
        g = geo.gps_of(path, kind)
        if not g:
            return {"geo": 1}
        p1, p2 = geo.place_of(*g)
        return {"geo": 1, "lat": g[0], "lon": g[1], "place1": p1, "place2": p2}

    def backfill(self):
        """啟動時在背景補讀舊檔案的地點（只讀檔頭，很快），以及「傳送」檔案的拍攝月份。"""
        def run():
            with self.store.lock:
                rows = self.store.conn.execute(
                    "SELECT id, path, kind, taken, received_at FROM files WHERE removed=0 AND "
                    "(geo IS NULL OR geo=0 OR taken IS NULL)").fetchall()
            self.geo_left = len(rows)
            for fid, rel, kind, taken, rcv in rows:
                p = self.store.abspath(rel)
                try:
                    if p.exists():
                        kw = self._geo_cols(p, kind)
                        if not taken:
                            fb = datetime.datetime.fromtimestamp(rcv or time.time()).isoformat()
                            kw["taken"] = f"{extract.taken_date(p, fb):%Y-%m}"
                        self.store.update(fid, **kw)
                except Exception as e:  # noqa
                    log.debug("backfill place failed %s: %s", p.name, e)
                self.geo_left -= 1
            self.geo_left = 0
            if rows:
                log.info(_t("log.backfilled", n=len(rows)))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------ 背景補做：OCR 文字、影片片段
    def enrich_status(self):
        ocr_ok = self.ocr.available
        a, b = self.store.enrich_left(doc_only=not ocr_ok)
        return {"ocr_left": a, "seg_left": b, "paused": self.enrich_paused, "now": self.enrich_now,
                "ocr": self.ocr.state, "error": self.enrich_error}

    def pause_enrich(self, value: bool):
        self.enrich_paused = bool(value)
        if not value:
            self.enrich_error, self._fail_run = None, 0

    def start_enrich(self):
        threading.Thread(target=self._enrich_loop, daemon=True).start()

    def _idle(self):
        return not self.enrich_paused and not self.q.qsize() and not self.busy

    def _enrich_loop(self):
        time.sleep(8)
        while True:
            try:
                did = False
                if self._idle():
                    for r in self.store.enrich_next("ocr", 10, doc_only=not self.ocr.available):
                        if not self._idle():
                            break
                        self._enrich_one(r, "ocr")
                        did = True
                if self._idle() and not did:
                    for r in self.store.enrich_next("seg", 2):
                        if not self._idle():
                            break
                        self._enrich_one(r, "seg")
                        did = True
                time.sleep(0.2 if did else 5)
            except Exception:  # noqa
                log.exception(_t("log.enrich_failed"))
                time.sleep(30)

    def _enrich_one(self, r, which):
        p = self.store.abspath(r["path"])
        self.enrich_now = r["name"]
        try:
            if not p.exists():
                raise FileNotFoundError(p.name)
            if which == "ocr":
                text, done = self._text(p, r["kind"])
                if done:
                    self.store.update(r["id"], **self._text_cols(text, True))
            else:
                segs = self._segments(p)
                if segs:
                    v = np.stack([x for _, x in segs]).mean(axis=0)
                    self.store.update(r["id"], vec=(v / (np.linalg.norm(v) + 1e-9)).astype(np.float32))
                    self.store.set_segs(r["id"], segs)
                else:
                    self.store.update(r["id"], seg_done=2)
            self._fail_run = 0
        except Exception as e:  # noqa
            now = self.store.get(r["id"])
            if now is not None and now["path"] != r["path"]:
                return          # 處理時檔案剛好被搬走（改分類），下一輪再做
            what = _t("enrich.ocr") if which == "ocr" else _t("enrich.seg")
            log.warning(_t("log.enrich_one_failed", what=what, name=r["name"], err=e))
            missing = not p.exists()
            n = self._fails[r["id"]] = self._fails.get(r["id"], 0) + 1
            if missing or n >= 2:       # 同一個檔案失敗兩次才放棄它
                self.store.update(r["id"], **({"ocr_done": 2} if which == "ocr" else {"seg_done": 2}))
            if not missing:
                self._fail_run += 1
                if self._fail_run >= 3:  # 連續好幾個檔案都失敗：多半是模型有問題，先暫停
                    self.enrich_paused = True
                    self.enrich_error = _t("enrich.paused_err", what=what, err=e)
                    log.warning(self.enrich_error)
        finally:
            self.enrich_now = None

    def file_text(self, fid):
        r = self.store.get(fid)
        if r is None:
            raise ValueError(_t("chk.not_found"))
        return {"text": r["ocr"] or "", "done": r["ocr_done"], "segs": self.store.seg_count(fid)}

    # ------------------------------------------------ 自訂類別
    def _save_cats(self, cats, emojis=None):
        self.cfg["categories"] = cats
        if emojis is not None:
            self.cfg["emojis"] = emojis
        catmod.save(self.cfg)
        self.clf.reset()

    def cat_add(self, name, desc, emoji=""):
        with self.op_lock:
            name = catmod.check_name(self.cfg, name)
            cats = dict(self.cfg.categories)
            cats[name] = catmod.clean_desc(desc)
            em = dict(self.cfg.emojis)
            em[name] = (emoji or "🏷️").strip()[:4]
            self._save_cats(cats, em)
            return name

    def cat_edit(self, name, desc=None, emoji=None, new_name=None):
        """改描述／圖示；new_name 不同時一併改名（資料夾裡的檔案跟著搬）。"""
        with self.op_lock:
            if name not in self.cfg.categories:
                raise ValueError(_t("err.no_such_cat"))
            moved, failed = 0, []
            if new_name is not None and new_name.strip() != name:
                new_name = catmod.check_name(self.cfg, new_name, old=name)
                moved, failed = self._rename(name, new_name)
                name = new_name
            cats, em = dict(self.cfg.categories), dict(self.cfg.emojis)
            if desc is not None:
                cats[name] = catmod.clean_desc(desc)
            if emoji is not None:
                em[name] = (emoji or "🏷️").strip()[:4]
            self._save_cats(cats, em)
            return {"name": name, "moved": moved, "failed": failed}

    def _retarget(self, r, new_cat):
        """把一個檔案的類別改成 new_cat；在類別資料夾裡的就搬過去。回傳 (是否搬了, 錯誤)。"""
        kw = {"category": new_cat}
        if r["removed"]:
            pp = Path(r["prev_path"] or "")
            if len(pp.parts) >= 2 and pp.parts[0] == self.cfg.names["sorted"]:  # 移回時要回到新的類別資料夾
                kw["prev_path"] = str(self._folder(new_cat, r["taken"]).relative_to(self.cfg.data) / pp.name)
            self.store.update(r["id"], **kw)
            return False, None
        if r["duplicate_of"] or r["mode"] != "sort":
            self.store.update(r["id"], **kw)
            return False, None
        src = self.store.abspath(r["path"])
        if not src.exists():
            self.store.update(r["id"], **kw)
            return False, None
        try:
            dst = move_within(src, self._folder(new_cat, r["taken"]), self.cfg.data)
        except OSError as e:   # 搬不動（例如檔案開著）：類別照改，檔案先留在原資料夾
            self.store.update(r["id"], **kw)
            return False, r["name"] + _t("chk.paren", text=_t("err.in_use") if isinstance(e, PermissionError) else e)
        kw["path"] = str(dst)
        self.store.update(r["id"], **kw)
        return True, None

    def _cat_rows(self, name):
        with self.store.lock:
            return self.store.conn.execute("SELECT * FROM files WHERE category=?", (name,)).fetchall()

    def _prune_dirs(self, name):
        """類別資料夾搬空了就移除空資料夾（只移除空的，裡面有任何檔案都不動）。"""
        top = self.cfg.sorted_dir / name
        if not top.exists():
            return
        for d in sorted((p for p in top.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            top.rmdir()
        except OSError:
            pass

    def _rename(self, old, new):
        moved, failed = 0, []
        for r in self._cat_rows(old):
            ok, err = self._retarget(r, new)
            moved += ok
            if err:
                failed.append(err)
        with self.store.lock:
            self.store.conn.execute("UPDATE files SET second=? WHERE second=?", (new, old))
            self.store.conn.commit()
        cats = {(new if k == old else k): v for k, v in self.cfg.categories.items()}
        em = {(new if k == old else k): v for k, v in self.cfg.emojis.items()}
        if self.cfg.screenshot_category == old:
            self.cfg["screenshot_category"] = new
        if self.cfg.document_category == old:
            self.cfg["document_category"] = new
        self._save_cats(cats, em)
        self._prune_dirs(old)
        return moved, failed

    def cat_delete(self, name):
        """刪除類別：裡面的檔案改成「未分類」並搬到未分類資料夾，不刪除任何檔案。"""
        with self.op_lock:
            if name not in self.cfg.categories:
                raise ValueError(_t("err.no_such_cat"))
            if name == self.cfg.document_category:
                raise ValueError(_t("err.cat_doc_keep", name=name))
            if name == self.cfg.screenshot_category:
                raise ValueError(_t("err.cat_shot_keep", name=name))
            if len(self.cfg.categories) <= 2:
                raise ValueError(_t("err.cat_min"))
            un = self.cfg.unsorted_category
            moved, failed = 0, []
            for r in self._cat_rows(name):
                ok, err = self._retarget(r, un)
                moved += ok
                if err:
                    failed.append(err)
                self.store.update(r["id"], corrected=0, checked=0, reason=f"類別「{name}」已刪除",
                                  second=None, second_score=None)
            with self.store.lock:
                self.store.conn.execute("UPDATE files SET second=NULL, second_score=NULL WHERE second=?", (name,))
                self.store.conn.commit()
            cats = {k: v for k, v in self.cfg.categories.items() if k != name}
            em = {k: v for k, v in self.cfg.emojis.items() if k != name}
            self._save_cats(cats, em)
            self._prune_dirs(name)
            return {"moved": moved, "failed": failed}

    # ------------------------------------------------ 匯出（複製，原檔不動）
    def _export_rows(self, ids):
        rows = [r for r in self.store.rows([int(i) for i in ids]) if not r["removed"]]
        out = []
        for r in rows:
            p = self.store.abspath(r["path"])
            if p.exists() and inside(p, self.cfg.data):
                out.append((r, p))
        if not out:
            raise ValueError(_t("err.nothing_export"))
        return out

    def _sub_of(self, r, by):
        if by == "category":
            if r["mode"] == "plain":
                return _t("lib.plain")
            if r["duplicate_of"]:
                return _t("lib.dup")
            if r["category"] == OTHER_CAT:
                return _t("lib.other")
            return r["category"] or _t("lib.other_short")
        if by == "month":
            return r["taken"] or _t("lib.no_date")
        if by == "place":
            return " ".join(x for x in (r["place1"], r["place2"]) if x) or _t("lib.no_place")
        return ""

    def export_copy(self, ids, dest: str, by=""):
        """複製到使用者選的資料夾（在裡面新建「尋棲匯出 日期時間」），不覆蓋任何檔案。"""
        dest = (dest or "").strip().strip('"')
        if not dest:
            raise ValueError(_t("err.export_pick"))
        base = Path(os.path.expandvars(os.path.expanduser(dest)))
        if not base.is_absolute():
            raise ValueError(_t("err.export_relative"))
        base = base.resolve()
        if inside(base, self.cfg.data):
            raise ValueError(_t("err.export_in_data"))
        if inside(base, ROOT):
            raise ValueError(_t("err.export_in_app"))
        items = self._export_rows(ids)
        out = unique_path(base, _t("lib.export_folder", time=f"{datetime.datetime.now():%Y-%m-%d %H%M}"))
        out.mkdir(parents=True)
        n = 0
        for r, p in items:
            folder = out / self._sub_of(r, by) if by else out
            shutil.copy2(p, unique_path(folder, p.name))
            n += 1
        return {"count": n, "folder": str(out)}

    def export_zip(self, ids, by=""):
        """打包成 ZIP（不壓縮，照片影片本來就壓縮過，比較快），回傳下載代號。"""
        items = self._export_rows(ids)
        tmp = self.cfg.export_temp
        tmp.mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        used = set()
        with zipfile.ZipFile(tmp / f"{token}.zip", "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for r, p in items:
                sub = self._sub_of(r, by) if by else ""
                name = f"{sub}/{p.name}" if sub else p.name
                stem, suf = os.path.splitext(name)
                i = 1
                while name.lower() in used:
                    name = f"{stem} ({i}){suf}"
                    i += 1
                used.add(name.lower())
                z.write(p, name)
        return {"token": token, "count": len(items)}

    def export_file(self, token):
        if not token or not all(c in "0123456789abcdef" for c in token):
            return None
        p = self.cfg.export_temp / f"{token}.zip"
        return p if p.exists() else None

    def clean_export_temp(self):
        """清掉上次沒下載完的匯出暫存（程式自己產生的 zip）。"""
        tmp = self.cfg.export_temp
        if tmp.exists():
            for f in tmp.glob("*.zip"):
                try:
                    f.unlink()
                except OSError:
                    pass
