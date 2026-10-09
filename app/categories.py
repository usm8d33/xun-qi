"""自訂類別：在網頁上新增、改名、刪除類別，存在 檔案資料夾\\.整理器資料\\categories.json。

第一次在網頁上改類別之前，類別來自 config.yaml；改過之後以 categories.json 為準
（放在檔案資料夾裡，重裝程式也不會不見，也會一起備份）。
"""
import json
from pathlib import Path

FILE = "categories.json"
# 這些名字程式自己在用，不能當類別名稱
RESERVED = {"未分類", "疑似重複", "其他檔案", "傳送", "待刪除", "已分類", "索引備份", "匯出"}
DEFAULT_EMOJI = {"美食": "🍜", "飲品甜點": "🧋", "自然風景": "🏞️", "城市街景": "🏙️", "人物": "🧑", "合照聚會": "🎉",
                 "寵物動物": "🐾", "植物花卉": "🌸", "交通工具": "🚗", "文件收據": "🧾", "商品購物": "🛍️", "截圖": "📱"}
_BAD = set('<>:"/\\|?*.')


def path_of(cfg) -> Path:
    return cfg.state_dir / FILE


def apply_saved(cfg):
    """程式啟動時：有網頁上存過的類別就用它。"""
    p = path_of(cfg)
    cfg["emojis"] = {}
    if not p.exists():
        cfg["emojis"] = {k: DEFAULT_EMOJI.get(k, "🏷️") for k in cfg.categories}
        return
    d = json.loads(p.read_text(encoding="utf-8"))
    cfg["categories"] = {c["name"]: list(c["desc"]) for c in d["categories"]}
    cfg["emojis"] = {c["name"]: c.get("emoji") or "🏷️" for c in d["categories"]}
    for k in ("screenshot_category", "document_category"):
        if d.get(k):
            cfg[k] = d[k]


def save(cfg):
    d = {"categories": [{"name": n, "emoji": cfg["emojis"].get(n, "🏷️"), "desc": list(v if isinstance(v, list) else [v])}
                        for n, v in cfg.categories.items()],
         "screenshot_category": cfg.screenshot_category, "document_category": cfg.document_category}
    p = path_of(cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def listing(cfg):
    return [{"name": n, "emoji": cfg["emojis"].get(n, "🏷️"), "desc": list(v if isinstance(v, list) else [v]),
             "special": ("screenshot" if n == cfg.screenshot_category else
                         "document" if n == cfg.document_category else None)}
            for n, v in cfg.categories.items()]


def check_name(cfg, name: str, old: str = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("請輸入類別名稱")
    if len(name) > 20:
        raise ValueError("類別名稱最多 20 個字")
    if any(ch in _BAD or ord(ch) < 32 for ch in name):
        raise ValueError('類別名稱不能有 < > : " / \\ | ? * . 這些符號')
    if name in RESERVED or name == cfg.unsorted_category:
        raise ValueError(f"「{name}」是程式自己在用的名稱，請換一個")
    if name != old and name in cfg.categories:
        raise ValueError(f"已經有「{name}」這個類別了")
    return name


def clean_desc(desc):
    if isinstance(desc, str):
        desc = desc.splitlines()
    out = [d.strip() for d in desc if d and d.strip()]
    if not out:
        raise ValueError("至少要寫一句描述，例如「小朋友在玩、嬰兒照」")
    return out[:12]
