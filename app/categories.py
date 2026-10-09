"""自訂類別：在網頁上新增、改名、刪除類別，存在 檔案資料夾\\.整理器資料\\categories.json。

第一次在網頁上改類別之前，類別來自 config.yaml；改過之後以 categories.json 為準
（放在檔案資料夾裡，重裝程式也不會不見，也會一起備份）。

英文介面新建的檔案資料夾：config.yaml 的類別沒改過時，改用 app/i18n/categories.<語言>.json 的預設類別
（Food、Scenery…），並立刻寫成 categories.json，之後切換語言也不會改變。
"""
import json
from pathlib import Path

from . import lang

FILE = "categories.json"
# 這些名字程式自己在用，不能當類別名稱（所有語言的資料夾名稱都算）
_RES_KEYS = ("lib.unsorted", "lib.dup", "lib.other", "lib.plain", "lib.removed", "lib.sorted", "lib.backups",
             "cat.export")
RESERVED = {"未分類", "疑似重複", "其他檔案", "傳送", "待刪除", "已分類", "索引備份", "匯出"}
DEFAULT_EMOJI = {"美食": "🍜", "飲品甜點": "🧋", "自然風景": "🏞️", "城市街景": "🏙️", "人物": "🧑", "合照聚會": "🎉",
                 "寵物動物": "🐾", "植物花卉": "🌸", "交通工具": "🚗", "文件收據": "🧾", "商品購物": "🛍️", "截圖": "📱"}
ZH_DEFAULT_NAMES = set(DEFAULT_EMOJI)     # config.yaml 原本附的 12 個類別
_BAD = set('<>:"/\\|?*.')


def reserved():
    out = set(RESERVED)
    for code in lang.LANGS:
        out |= {lang.t(k, code) for k in _RES_KEYS}
    return {x.lower() for x in out}


def lang_defaults(code):
    """某個語言的預設類別（繁體中文用 config.yaml，沒有檔案就回傳 None）。"""
    p = lang.DIR / f"categories.{code}.json"
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def path_of(cfg) -> Path:
    return cfg.state_dir / FILE


def apply_saved(cfg):
    """程式啟動時：有網頁上存過的類別就用它。"""
    p = path_of(cfg)
    cfg["emojis"] = {}
    lib = cfg.get("library") or {}
    if not p.exists() and lib.get("lang", "zh") != "zh" and set(cfg.categories) == ZH_DEFAULT_NAMES:
        d = lang_defaults(lib["lang"])
        if d:   # 新的英文檔案資料夾，config.yaml 也沒改過類別：寫下英文預設類別（之後固定）
            cfg["categories"] = {c["name"]: list(c["desc"]) for c in d["categories"]}
            cfg["emojis"] = {c["name"]: c.get("emoji") or "🏷️" for c in d["categories"]}
            cfg["screenshot_category"] = d["screenshot_category"]
            cfg["document_category"] = d["document_category"]
            save(cfg)
            return
    if not p.exists():
        cfg["emojis"] = {k: DEFAULT_EMOJI.get(k, "🏷️") for k in cfg.categories}
        return
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def listing(cfg):
    return [{"name": n, "emoji": cfg["emojis"].get(n, "🏷️"), "desc": list(v if isinstance(v, list) else [v]),
             "special": ("screenshot" if n == cfg.screenshot_category else
                         "document" if n == cfg.document_category else None)}
            for n, v in cfg.categories.items()]


def check_name(cfg, name: str, old: str = None) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(lang.t("err.cat_empty"))
    if len(name) > 20:
        raise ValueError(lang.t("err.cat_long"))
    if any(ch in _BAD or ord(ch) < 32 for ch in name):
        raise ValueError(lang.t("err.cat_chars"))
    if name.lower() in reserved() or name == cfg.unsorted_category:
        raise ValueError(lang.t("err.cat_reserved", name=name))
    if name != old and name in cfg.categories:
        raise ValueError(lang.t("err.cat_exists", name=name))
    return name


def clean_desc(desc):
    if isinstance(desc, str):
        desc = desc.splitlines()
    out = [d.strip() for d in desc if d and d.strip()]
    if not out:
        raise ValueError(lang.t("err.cat_desc"))
    return out[:12]
