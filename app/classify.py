"""分類器：多組描述的零樣本分類 + 截圖規則 + 從使用者修正中學習。"""
import logging
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("classify")

# iPhone 各代螢幕解析度（截圖就是這個尺寸）
IPHONE_SCREENS = {
    (750, 1334), (828, 1792), (1080, 1920), (1125, 2436), (1170, 2532),
    (1179, 2556), (1206, 2622), (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796), (1320, 2868),
    (1080, 2340), (2048, 2732), (1668, 2388), (1640, 2360), (1620, 2160), (1536, 2048), (1488, 2266),
}


# Android 常見螢幕解析度（各廠牌）。Android 截圖多半檔名就有 Screenshot，這是檔名被改掉時的備援
ANDROID_SCREENS = {
    (720, 1280), (720, 1520), (720, 1560), (720, 1600), (720, 1612), (720, 1640),
    (1080, 1920), (1080, 2160), (1080, 2220), (1080, 2232), (1080, 2244), (1080, 2280), (1080, 2310),
    (1080, 2340), (1080, 2376), (1080, 2388), (1080, 2400), (1080, 2408), (1080, 2412), (1080, 2436),
    (1080, 2460), (1080, 2520), (1008, 2244), (1220, 2712), (1240, 2772), (1260, 2800), (1264, 2780),
    (1280, 2800), (1280, 2856), (1344, 2992), (1440, 2560), (1440, 2880), (1440, 2960), (1440, 3040),
    (1440, 3088), (1440, 3120), (1440, 3168), (1440, 3200), (1600, 2560), (1800, 2880),
}
SHOT_WORDS = ("screenshot", "screen_shot", "screen shot", "screencapture", "螢幕截圖", "螢幕擷取", "截圖", "截屏",
              "スクリーンショット")


def image_facts(path: Path):
    """回傳 (是相機拍的, 截圖依據或 None)。
    相機照片一定有 EXIF 的 Make/Model；截圖沒有。
    依據：檔名（iPhone/Android 截圖常見檔名）、或尺寸剛好是 iPhone/iPad/Android 螢幕解析度。"""
    try:
        with Image.open(path) as im:  # 立刻關檔，Windows 才能刪除／搬移
            exif = im.getexif()
            camera = bool(exif.get(271) or exif.get(272))  # Make / Model
            w, h = im.size
            dims = (min(w, h), max(w, h))
            fmt = (im.format or "").upper()
    except Exception:
        return False, None
    if camera:
        return True, None
    name = path.name.lower()
    if any(k in name for k in SHOT_WORDS):
        return False, "截圖檔名"
    # 尺寸規則只看 PNG/WebP：手機截圖存成 PNG（Samsung 存 JPG 但檔名有 Screenshot），
    # 網路下載、LINE 存下來的照片多半是 JPG 且沒有相機資訊，用尺寸判斷容易誤判
    if fmt in ("PNG", "WEBP") and (dims in IPHONE_SCREENS or dims in ANDROID_SCREENS):
        return False, "截圖尺寸"
    return False, None


class Classifier:
    def __init__(self, cfg, store, embedder):
        self.cfg, self.store, self.emb = cfg, store, embedder
        self._protos = None

    # 類別描述 → 每類一個代表向量（多組描述取平均，比單一句子穩定）
    def prototypes(self):
        if self._protos is None:
            names, vecs = [], []
            for name, descs in self.cfg.categories.items():
                if isinstance(descs, str):
                    descs = [descs]
                sep = ": " if name.isascii() else "："     # 英文類別名稱用英文冒號
                v = self.emb.text([f"{name}{sep}{d}" if not d.startswith(name) else d for d in descs], "query")
                m = v.mean(axis=0)
                names.append(name)
                vecs.append(m / (np.linalg.norm(m) + 1e-9))
            self._protos = (names, np.stack(vecs).astype(np.float32))
        return self._protos

    def reset(self):
        self._protos = None

    def _learned(self, vec, exclude_id=None):
        """用使用者手動改過分類的檔案當範例：很像某個範例就直接採用它的分類。"""
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id, category, vec FROM files WHERE corrected=1 AND vec IS NOT NULL AND removed=0").fetchall()
        rows = [r for r in rows if r[0] != exclude_id and r[1] in self.cfg.categories]
        if not rows:
            return None, 0.0
        vs = [np.frombuffer(r[2], dtype=np.float32) for r in rows]
        rows = [r for r, v in zip(rows, vs) if len(v) == vec.shape[0]]   # 先濾掉不同維度的舊範例再堆疊
        if not rows:
            return None, 0.0
        m = np.stack([v for v in vs if len(v) == vec.shape[0]])
        sims = m @ vec
        order = np.argsort(-sims)[:5]
        votes = {}
        for i in order:
            if sims[i] >= self.cfg.learn_similarity:
                votes[rows[i][1]] = votes.get(rows[i][1], 0.0) + float(sims[i])
        if not votes:
            return None, 0.0
        cat = max(votes, key=votes.get)
        return cat, float(max(sims[i] for i in order if rows[i][1] == cat))

    def classify(self, vec, path: Path = None, kind="image", exclude_id=None, text_len=0):
        """回傳 (類別, 信心 0~1, 原因, 第二名類別, 第二名信心)。"""
        cfg = self.cfg
        if vec is not None and cfg.embed_dim and len(vec) != cfg.embed_dim:   # 舊維度向量：等重建索引
            return cfg.unsorted_category, None, "索引需要重建", None, None
        shot = cfg.screenshot_category
        camera, shot_why = image_facts(path) if (path is not None and kind == "image") else (False, None)

        if shot_why and shot in cfg.categories:
            return shot, 1.0, shot_why, None, None

        cat, sim = self._learned(vec, exclude_id)
        if cat:
            return cat, sim, "像你分類過的檔案", None, None

        names, protos = self.prototypes()
        sims = protos @ vec
        if (camera or kind == "video") and shot in names:
            sims[names.index(shot)] = -1.0  # 相機拍的照片和影片不可能是截圖
        doc = cfg.document_category
        if (kind == "image" and text_len and cfg.ocr_doc_chars and text_len >= cfg.ocr_doc_chars
                and doc in names and cfg.ocr_doc_boost):
            sims[names.index(doc)] += float(cfg.ocr_doc_boost)  # 照片裡字很多：稍微偏向文件收據
        logits = sims * cfg.temperature
        p = np.exp(logits - logits.max())
        p /= p.sum()
        order = np.argsort(-p)
        i, j = int(order[0]), int(order[1]) if len(order) > 1 else None
        second = (names[j], float(p[j])) if j is not None else (None, None)
        if p[i] < cfg.min_confidence:
            return cfg.unsorted_category, float(p[i]), f"最像「{names[i]}」但不夠確定", names[i], float(p[i])
        return names[i], float(p[i]), "AI 判斷", second[0], second[1]
