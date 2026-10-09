"""EmbeddingGemma 2 包裝：需要時才載入、閒置自動卸載；只載文字＋圖片模組（440M），不載音訊。"""
import gc
import hashlib
import logging
import os
import threading
import time

import numpy as np

from .lang import t as _t

log = logging.getLogger("embedder")


class Embedder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None
        self.device = "cpu"
        self.lock = threading.RLock()
        self.last_used = 0.0
        self.fake = os.environ.get("LSS_FAKE_EMBED") == "1"  # 測試用，不載入真模型
        threading.Thread(target=self._idle_watch, daemon=True).start()

    # ---------------- 載入 / 卸載 ----------------
    @property
    def state(self):
        if self.fake:
            return _t("model.test")
        return _t("model.loaded", dev=self.device) if self.model is not None else _t("model.not_loaded")

    def _load(self):
        if self.model is not None or self.fake:
            return
        import torch
        from sentence_transformers import SentenceTransformer

        path = self.cfg.model_dir
        if not (path / "model.safetensors").exists():
            raise FileNotFoundError(_t("model.missing", path=path))
        xpu = getattr(torch, "xpu", None)
        if torch.cuda.is_available() and not self.cfg.force_cpu and _works(torch, "cuda"):
            self.device = "cuda"
            # 官方說明：不可用 float16（會變 NaN），支援就用 bfloat16，否則 float32
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float32
        elif xpu is not None and xpu.is_available() and not self.cfg.force_cpu and _works(torch, "xpu"):
            self.device = "xpu"  # Intel Arc 獨顯／Core Ultra 內顯
            dtype = torch.bfloat16
        else:
            self.device = "cpu"
            dtype = torch.float32
            torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))  # 只用一半 CPU，不拖慢電腦
        t = time.time()
        log.info(_t("log.model_loading", dev=self.device))
        self.model = SentenceTransformer(
            str(path),
            device=self.device,
            model_kwargs={"torch_dtype": dtype},
            config_kwargs={"audio_config": None},
            local_files_only=True,
        )
        log.info(_t("log.model_loaded", sec=f"{time.time() - t:.1f}"))

    def unload(self):
        with self.lock:
            if self.model is None:
                return
            self.model = None
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
                    torch.xpu.empty_cache()
            except Exception:
                pass
            log.info(_t("log.model_unloaded"))

    def _idle_watch(self):
        while True:
            time.sleep(30)
            mins = self.cfg.model_idle_unload_min
            if self.model is not None and mins and time.time() - self.last_used > mins * 60:
                self.unload()

    # ---------------- 編碼 ----------------
    def _enc(self, inputs, **kw):
        return self.model.encode(inputs, truncate_dim=self.cfg.embed_dim, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False, **kw)

    def text(self, texts, kind="query"):
        """kind: query（搜尋句、類別描述）或 document（文件內容）。"""
        with self.lock:
            self.last_used = time.time()
            if self.fake:
                return np.stack([_fake_vec(t, self.cfg.embed_dim) for t in texts])
            self._load()
            prompt = "SearchQuery" if kind == "query" else "Document"
            return self._enc(list(texts), prompt_name=prompt)

    def images(self, pil_images):
        with self.lock:
            self.last_used = time.time()
            if self.fake:
                return np.stack([_fake_img_vec(im, self.cfg.embed_dim) for im in pil_images])
            self._load()
            try:
                return self._enc(list(pil_images))
            except Exception as e1:  # 不同版本 sentence-transformers 的多模態輸入格式不同，換一種再試
                log.debug("image input failed (%s), retrying with dict format", e1)
                return self._enc([{"image": im} for im in pil_images])


def _works(torch, device):
    """確認這張顯示卡真的能跑（例如 RTX 50 搭到太舊的 PyTorch 會失敗），不行就改用 CPU。"""
    try:
        return float((torch.ones(4, device=device) * 2).sum()) == 8.0
    except Exception as e:  # noqa
        log.warning(_t("log.gpu_fallback", dev=device, err=e))
        return False


# ---------------- 測試用假向量 ----------------
def _norm(v):
    return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)


def _fake_vec(text, dim):
    seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
    return _norm(np.random.default_rng(seed).standard_normal(dim))


def _fake_img_vec(im, dim):
    small = np.asarray(im.convert("RGB").resize((8, 8)), dtype=np.float32).ravel() / 255.0
    v = np.resize(small - small.mean(), dim)
    return _norm(v)
