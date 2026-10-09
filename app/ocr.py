"""OCR（照片裡的文字）：PP-OCRv5 的 ONNX 模型（文字偵測 det＋文字辨識 rec），用 onnxruntime 執行。
- 需要時才載入、閒置自動卸載（和分類模型一樣）
- 有 NVIDIA 顯示卡就用 GPU（共用 PyTorch 內建的 CUDA），否則用 CPU
- 字典內建在 rec 模型裡（metadata 的 character）
另外也放「搜尋用的文字正規化」：全形半形、大小寫、空白、繁簡互通。"""
import gzip
import json
import logging
import os
import threading
import time
import unicodedata
from pathlib import Path

import numpy as np

from .lang import t as _t

log = logging.getLogger("ocr")


# ============================================================ 搜尋用正規化
_T2S = None


def _t2s():
    global _T2S
    if _T2S is None:
        try:
            with gzip.open(Path(__file__).with_name("t2s.json.gz"), "rt", encoding="utf-8") as f:
                _T2S = json.load(f)
        except Exception:  # noqa
            _T2S = {}
    return _T2S


def norm_map(s: str):
    """回傳 (正規化後的字串, 每個字對應原字串的位置)。
    正規化：NFKC（全形→半形）、小寫、去掉空白與常見標點、繁體→簡體（只用來比對，不會改原文）。"""
    t2s = _t2s()
    out, idx = [], []
    for i, ch in enumerate(s or ""):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if c.isspace() or c in ",，、。.·:：;；'\"「」『』()（）[]【】-_—~～|/\\":
                continue
            out.append(t2s.get(c, c))
            idx.append(i)
    return "".join(out), idx


def norm(s: str) -> str:
    return norm_map(s)[0]


def snippet(text: str, query: str, width=40):
    """找出符合的那一段，回傳 (片段, 標亮開始, 標亮結束)（以片段內的字元位置）；找不到回傳 None。"""
    q = norm(query)
    if not q or not text:
        return None
    n, idx = norm_map(text)
    k = n.find(q)
    if k < 0:
        return None
    a, b = idx[k], idx[k + len(q) - 1] + 1          # 原文中的位置
    s = max(0, a - width // 2)
    e = min(len(text), max(b + width // 2, s + width))
    # 片段只取同一段（不跨太多行）
    frag = text[s:e]
    lead = frag[: a - s].rfind("\n")
    if lead >= 0:
        s += lead + 1
    tail = text[b:e].find("\n")
    if tail >= 0:
        e = b + tail
    frag = text[s:e]
    return {"text": ("…" if s > 0 else "") + frag + ("…" if e < len(text) else ""),
            "a": a - s + (1 if s > 0 else 0), "b": b - s + (1 if s > 0 else 0)}


# ============================================================ OCR 模型
def _has_ort():
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


def find_models(folder: Path, prefer: str = "server"):
    """在 models\\ocr 找 det / rec 模型（依版本優先用 server 或 mobile）。"""
    if not folder.exists():
        return None, None
    files = sorted(folder.glob("*.onnx"))

    def pick(tag):
        c = [f for f in files if tag in f.name.lower()]
        c.sort(key=lambda f: (0 if prefer in f.name.lower() else 1, f.name))
        return c[0] if c else None
    return pick("det"), pick("rec")


class OCR:
    def __init__(self, cfg):
        self.cfg = cfg
        self.det = self.rec = None
        self.chars = None
        self.device = "cpu"
        self.lock = threading.RLock()
        self.last_used = 0.0
        self.fake = os.environ.get("LSS_FAKE_OCR") == "1"   # 測試用：讀同名 .txt 當成 OCR 結果
        threading.Thread(target=self._idle_watch, daemon=True).start()

    @property
    def available(self):
        if self.fake:
            return True
        if not self.cfg.ocr_enabled:
            return False
        d, r = find_models(self.cfg.ocr_dir, getattr(self.cfg, "ocr_size", "server"))
        if not (d and r):
            return False
        return _has_ort()

    @property
    def state(self):
        if self.fake:
            return _t("model.test")
        if not self.cfg.ocr_enabled:
            return _t("ocr.off")
        d, r = find_models(self.cfg.ocr_dir, getattr(self.cfg, "ocr_size", "server"))
        if not (d and r):
            return _t("ocr.no_model")
        if not _has_ort():
            return _t("ocr.no_ort")
        return _t("model.loaded", dev=self.device) if self.det is not None else _t("model.not_loaded")

    # ---------------- 載入 / 卸載 ----------------
    def _load(self):
        if self.det is not None:
            return
        det_p, rec_p = find_models(self.cfg.ocr_dir, getattr(self.cfg, "ocr_size", "server"))
        if not (det_p and rec_p):
            raise FileNotFoundError(_t("ocr.missing", path=self.cfg.ocr_dir))
        cuda = False
        if not self.cfg.force_cpu:
            try:
                import torch  # 先載入 PyTorch 再載入 onnxruntime：PyTorch 帶的 CUDA／cuDNN 可以直接共用
                cuda = torch.cuda.is_available()
            except Exception:
                pass
        import onnxruntime as ort
        provs = ["CPUExecutionProvider"]
        if cuda and "CUDAExecutionProvider" in ort.get_available_providers():
            try:
                ort.preload_dlls()      # 預設先找 PyTorch 的 lib 資料夾
            except Exception:
                pass
            provs = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        so = ort.SessionOptions()
        so.log_severity_level = 3
        if provs[0] == "CPUExecutionProvider":
            so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)   # 只用一半 CPU
        t = time.time()

        def mk(p):
            try:
                return ort.InferenceSession(str(p), so, providers=provs)
            except Exception as e:  # noqa
                if provs[0] != "CPUExecutionProvider":
                    log.warning(_t("log.ocr_gpu_fallback", err=e))
                    return ort.InferenceSession(str(p), so, providers=["CPUExecutionProvider"])
                raise
        self.det, self.rec = mk(det_p), mk(rec_p)
        self.device = "cuda" if "CUDAExecutionProvider" in self.det.get_providers() else "cpu"
        meta = self.rec.get_modelmeta().custom_metadata_map
        chars = meta.get("character", "").split("\n")
        if not chars or len(chars) < 100:
            dict_p = next(iter(sorted(self.cfg.ocr_dir.glob("*.txt"))), None)
            if not dict_p:
                raise FileNotFoundError(_t("ocr.no_dict"))
            with open(dict_p, encoding="utf-8") as fh:
                chars = fh.read().split("\n")
        self.chars = ["<blank>"] + [c for c in chars] + [" "]
        log.info(_t("log.ocr_loaded", dev=self.device, det=det_p.name, rec=rec_p.name, sec=f"{time.time() - t:.1f}"))

    def unload(self):
        with self.lock:
            if self.det is None:
                return
            self.det = self.rec = None
            log.info(_t("log.ocr_unloaded"))

    def _idle_watch(self):
        while True:
            time.sleep(30)
            mins = self.cfg.model_idle_unload_min
            if self.det is not None and mins and time.time() - self.last_used > mins * 60:
                self.unload()

    # ---------------- 辨識 ----------------
    def read(self, pil_image, fake_path: Path = None) -> str:
        """回傳整張圖的文字（由上到下、由左到右，一行一行）。沒有文字回傳空字串。"""
        with self.lock:
            self.last_used = time.time()
            if self.fake:
                if fake_path is not None:
                    t = Path(str(fake_path) + ".ocr.txt")
                    return t.read_text(encoding="utf-8") if t.exists() else ""
                return ""
            self._load()
            img = np.asarray(pil_image.convert("RGB"))[:, :, ::-1]   # → BGR，和 PaddleOCR 訓練時一樣
            boxes = self._detect(img)
            if not boxes:
                return ""
            crops = [_crop(img, b) for b in boxes]
            texts = self._recognize(crops)
            lines = _join_lines([(b, t, s) for b, (t, s) in zip(boxes, texts) if t.strip() and s >= self.cfg.ocr_min_score])
            return "\n".join(lines)

    def _detect(self, img):
        import cv2
        h, w = img.shape[:2]
        limit = self.cfg.ocr_max_side
        r = min(1.0, limit / max(h, w))
        if min(h, w) * r < 64:          # 太小的圖放大一點
            r = 64 / min(h, w)
        nh, nw = max(32, int(round(h * r / 32)) * 32), max(32, int(round(w * r / 32)) * 32)
        x = cv2.resize(img, (nw, nh)).astype(np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        x = x.transpose(2, 0, 1)[None]
        prob = self.det.run(None, {self.det.get_inputs()[0].name: x})[0][0, 0]
        mask = (prob > 0.3).astype(np.uint8)
        contours, _ = cv2.findContours(mask * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / nw, h / nh
        boxes = []
        for c in contours[:1000]:
            if len(c) < 4:
                continue
            rect = cv2.minAreaRect(c)
            if min(rect[1]) < 3:
                continue
            # 框內平均機率太低就跳過
            x0, y0, bw, bh = cv2.boundingRect(c)
            m = np.zeros((bh, bw), np.uint8)
            cv2.fillPoly(m, [c.reshape(-1, 2) - [x0, y0]], 1)
            if cv2.mean(prob[y0:y0 + bh, x0:x0 + bw], m)[0] < 0.6:
                continue
            # 往外擴大（PaddleOCR 的 unclip，比例 1.5）
            (cx, cy), (rw, rh), ang = rect
            area, peri = rw * rh, 2 * (rw + rh)
            d = area * 1.5 / max(peri, 1e-6)
            box = cv2.boxPoints(((cx, cy), (rw + 2 * d, rh + 2 * d), ang))
            if min(rw + 2 * d, rh + 2 * d) < 5:
                continue
            box[:, 0] = np.clip(box[:, 0] * sx, 0, w - 1)
            box[:, 1] = np.clip(box[:, 1] * sy, 0, h - 1)
            boxes.append(_order(box))
        return boxes

    def _recognize(self, crops):
        import cv2
        out = [None] * len(crops)
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / max(1, crops[i].shape[0]))
        bs = 8
        for k in range(0, len(order), bs):
            ids = order[k:k + bs]
            ratio = max(crops[i].shape[1] / max(1, crops[i].shape[0]) for i in ids)
            W = int(min(max(48 * ratio, 320), 3200))
            batch = np.zeros((len(ids), 3, 48, W), np.float32)
            for j, i in enumerate(ids):
                c = crops[i]
                rw = min(W, max(1, int(np.ceil(48 * c.shape[1] / max(1, c.shape[0])))))
                x = cv2.resize(c, (rw, 48)).astype(np.float32) / 255.0
                batch[j, :, :, :rw] = ((x - 0.5) / 0.5).transpose(2, 0, 1)
            pred = self.rec.run(None, {self.rec.get_inputs()[0].name: batch})[0]
            idx, prob = pred.argmax(axis=2), pred.max(axis=2)
            for j, i in enumerate(ids):
                chars, scores, prev = [], [], 0
                for t, p in zip(idx[j], prob[j]):
                    if t != prev and t != 0 and t < len(self.chars):
                        chars.append(self.chars[t])
                        scores.append(float(p))
                    prev = t
                out[i] = ("".join(chars), float(np.mean(scores)) if scores else 0.0)
        return out


def _order(box):
    """四個角排成 左上、右上、右下、左下。"""
    box = np.array(box, np.float32)
    s, d = box.sum(1), np.diff(box, axis=1).ravel()
    return np.array([box[np.argmin(s)], box[np.argmin(d)], box[np.argmax(s)], box[np.argmax(d)]], np.float32)


def _crop(img, box):
    import cv2
    w = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    h = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    w, h = max(w, 1), max(h, 1)
    M = cv2.getPerspectiveTransform(box, np.float32([[0, 0], [w, 0], [w, h], [0, h]]))
    c = cv2.warpPerspective(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if h >= w * 1.5:                    # 直書：轉成橫的
        c = np.rot90(c)
    return np.ascontiguousarray(c)


def _join_lines(items):
    """把同一列的文字框接成一行（由上到下、由左到右）。"""
    if not items:
        return []
    items = sorted(items, key=lambda it: (it[0][:, 1].mean(), it[0][:, 0].min()))
    rows, cur, cy, ch = [], [], None, None
    for b, t, _ in items:
        y, hh = b[:, 1].mean(), b[:, 1].max() - b[:, 1].min()
        if cur and abs(y - cy) <= max(ch, hh) * 0.5:
            cur.append((b[:, 0].min(), t))
        else:
            if cur:
                rows.append(cur)
            cur, cy, ch = [(b[:, 0].min(), t)], y, hh
    rows.append(cur)
    return [" ".join(t for _, t in sorted(r)) for r in rows]
