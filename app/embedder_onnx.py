"""輕量版：EmbeddingGemma 2 的 ONNX int8（onnx-community/embeddinggemma-2-ONNX 的 quantized 檔）。
不需要 PyTorch；前處理只用 numpy、Pillow 與 tokenizers，照 transformers 的 Gemma4 圖片處理器寫。
需要時才載入、閒置自動卸載；有 DirectML（Windows 顯示卡）就用，否則用 CPU。"""
import gc
import logging
import math
import os
import threading
import time

import numpy as np

from .lang import t as _t

log = logging.getLogger("embedder")

PATCH, POOL, SOFT = 16, 3, 280          # 和官方 processor_config 相同
MAX_PATCHES = SOFT * POOL * POOL
BOS, EOS, BOI, IMG, EOI = 2, 1, 255999, 258880, 258882   # tokenizer 的 <bos> <eos> <|image> <​|image|> <image|>
PROMPTS = {"query": "task: search result | query: ", "document": "title: none | text: "}


def target_size(h, w):
    """保持比例、最多 MAX_PATCHES 個 16px 方塊，邊長是 48 的倍數（同 transformers 的 get_aspect_ratio_preserving_size）。"""
    target_px = MAX_PATCHES * PATCH * PATCH
    f = math.sqrt(target_px / (h * w))
    side = POOL * PATCH
    th, tw = int(math.floor(f * h / side)) * side, int(math.floor(f * w / side)) * side
    max_side = (MAX_PATCHES // POOL ** 2) * side
    if th == 0:
        th, tw = side, min(int(math.floor(w / h)) * side, max_side)
    elif tw == 0:
        tw, th = side, min(int(math.floor(h / w)) * side, max_side)
    return th, tw


def image_inputs(im):
    """PIL 圖片 → (pixel_values[1,P,768], position_ids[1,P,2], 軟 token 數)。"""
    from PIL import Image
    im = im.convert("RGB")
    th, tw = target_size(im.height, im.width)
    if (im.height, im.width) != (th, tw):
        im = im.resize((tw, th), Image.BICUBIC)
    a = np.asarray(im, dtype=np.float32).transpose(2, 0, 1) / 255.0           # C,H,W
    ph, pw = th // PATCH, tw // PATCH
    p = a.reshape(3, ph, PATCH, pw, PATCH).transpose(1, 3, 2, 4, 0).reshape(ph * pw, -1)
    gx, gy = np.meshgrid(np.arange(pw), np.arange(ph), indexing="xy")
    pos = np.stack([gx, gy], -1).reshape(ph * pw, 2).astype(np.int64)
    n = p.shape[0]
    p = np.pad(p, ((0, MAX_PATCHES - n), (0, 0)))
    pos = np.pad(pos, ((0, MAX_PATCHES - n), (0, 0)), constant_values=-1)
    return p[None].astype(np.float32), pos[None], n // (POOL * POOL)


class OnnxEmbedder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.text_s = self.vis_s = self.tok = None
        self.device = "cpu"
        self.lock = threading.RLock()
        self.last_used = 0.0
        self.fake = os.environ.get("LSS_FAKE_EMBED") == "1"
        threading.Thread(target=self._idle_watch, daemon=True).start()

    @property
    def model(self):
        return self.text_s

    @property
    def state(self):
        if self.fake:
            return _t("model.test")
        return _t("model.loaded", dev="ONNX " + str(self.device)) if self.text_s is not None else _t("model.not_loaded")

    def _session(self, path, cpu=False):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)   # 只用一半 CPU，不拖慢電腦
        prov = ["CPUExecutionProvider"]
        if not (cpu or self.cfg.force_cpu) and "DmlExecutionProvider" in ort.get_available_providers():
            prov = ["DmlExecutionProvider", "CPUExecutionProvider"]
            so.enable_mem_pattern = False          # DirectML 的要求
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        s = ort.InferenceSession(str(path), so, providers=prov)
        self.device = "DirectML" if s.get_providers()[0] == "DmlExecutionProvider" else "CPU"
        return s

    def _load(self, need_vision=False):
        if self.fake:
            return
        d = self.cfg.model_dir
        if self.text_s is None:
            from tokenizers import Tokenizer
            m = d / "onnx" / "model_quantized.onnx"
            if not m.exists():
                raise FileNotFoundError(_t("model.missing_lite", path=m))
            t = time.time()
            self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
            self.text_s = self._session(m)
            log.info(_t("log.lite_loaded", dev=self.device, sec=f"{time.time() - t:.1f}"))
        if need_vision and self.vis_s is None:
            self.vis_s = self._session(d / "onnx" / "vision_encoder_quantized.onnx")

    def _safe(self, fn):
        """DirectML 跑不動這個模型（驅動或顯示卡不支援）時，自動改用 CPU 再試一次。"""
        try:
            return fn()
        except Exception as e:  # noqa
            if self.device != "DirectML":
                raise
            log.warning(_t("log.dml_fallback", err=e))
            self.cfg["force_cpu"] = True
            self.text_s = self.vis_s = None
            return fn()

    def unload(self):
        with self.lock:
            if self.text_s is None and self.vis_s is None:
                return
            self.text_s = self.vis_s = None
            gc.collect()
            log.info(_t("log.model_unloaded"))

    def _idle_watch(self):
        while True:
            time.sleep(30)
            mins = self.cfg.model_idle_unload_min
            if self.text_s is not None and mins and time.time() - self.last_used > mins * 60:
                self.unload()

    def _run(self, ids, img=None):
        z = np.zeros((0, 512), np.float32)
        feeds = {"input_ids": np.asarray([ids], np.int64), "attention_mask": np.ones((1, len(ids)), np.int64),
                 "image_features": z if img is None else img, "video_features": z, "audio_features": z}
        e = self.text_s.run(["sentence_embedding"], feeds)[0][0][: self.cfg.embed_dim]
        return (e / (np.linalg.norm(e) + 1e-12)).astype(np.float32)

    def text(self, texts, kind="query"):
        with self.lock:
            self.last_used = time.time()
            if self.fake:
                from .embedder import _fake_vec
                return np.stack([_fake_vec(t, self.cfg.embed_dim) for t in texts])
            pre = PROMPTS["query" if kind == "query" else "document"]

            def go():
                self._load()
                return np.stack([self._run(self.tok.encode(pre + t).ids[:2048]) for t in texts])
            return self._safe(go)

    def images(self, pil_images):
        with self.lock:
            self.last_used = time.time()
            if self.fake:
                from .embedder import _fake_img_vec
                return np.stack([_fake_img_vec(im, self.cfg.embed_dim) for im in pil_images])
            inputs = [image_inputs(im) for im in pil_images]

            def go():
                self._load(need_vision=True)
                out = []
                for pv, pos, n in inputs:
                    feats = self.vis_s.run(None, {"pixel_values": pv, "pixel_position_ids": pos})[0]
                    ids = [BOS, BOI] + [IMG] * n + [EOI, EOS]     # 和官方 processor 產生的完全相同
                    out.append(self._run(ids, feats.astype(np.float32)))
                return np.stack(out)
            return self._safe(go)
