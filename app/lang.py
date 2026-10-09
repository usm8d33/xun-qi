"""介面語言（繁體中文／English）。

文字都放在 app/i18n/<語言代碼>.json（zh.json、en.json），網頁和程式共用，兩個檔案的 key 完全一致。
之後要加入其他語言：複製 en.json 改名（例如 ja.json）翻譯內容，再把代碼加進 LANGS。

語言怎麼決定（由先到後）：
  1. 環境變數 XQ_LANG（測試或手動指定用）
  2. 程式資料夾 user_settings.json 的 "lang"（網頁「設定 › 語言」選的）
  3. 第一次啟動：依 Windows 介面語言（中文 → 繁體中文，其他 → English），並記到 user_settings.json
     （從舊版更新、之前就用過的人維持繁體中文）
"""
import json
import os
import re
import sys
import threading
from pathlib import Path

DIR = Path(__file__).with_name("i18n")
LANGS = {"zh": "繁體中文", "en": "English"}   # 語言選單顯示的名稱（永遠用該語言自己的寫法）
FALLBACK = "zh"

_tables = {}
_cur = None
_lock = threading.Lock()
_VAR = re.compile(r"\{(\w+)\}")


def table(code: str) -> dict:
    with _lock:
        if code not in _tables:
            try:
                with open(DIR / f"{code}.json", encoding="utf-8") as f:
                    _tables[code] = json.load(f)
            except OSError:
                _tables[code] = {}
        return _tables[code]


def normalize(code) -> str:
    """'zh-TW'、'zh_Hant'、'ZH' → 'zh'；'en-US' → 'en'；不認得 → ''。"""
    c = str(code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    base = c.split("-")[0].split(".")[0]
    return base if base in LANGS else ""


def detect_system() -> str:
    """Windows 介面語言是中文（任何地區）→ zh，其他 → en。"""
    if sys.platform == "win32":
        try:
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "zh" if (lid & 0x3FF) == 0x04 else "en"   # LANG_CHINESE = 0x04
        except Exception:  # noqa
            pass
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(k)
        if v:
            return "zh" if v.lower().startswith("zh") else "en"
    return "en"


def _settings():
    from . import config
    return config.read_user_settings()


def _previous_user() -> bool:
    """更新前就在用的人（舊版只有繁體中文）：有 user_settings.json，或桌面已經有舊的檔案資料夾。
    這種情況不算「第一次啟動」，維持繁體中文，不會因為 Windows 是英文就突然換語言。"""
    from . import config
    if _settings():
        return True
    d = config.desktop_dir()
    return any((d / n / ".整理器資料").is_dir() for n in ("Xun-Qi 尋棲", "LocalSend 收到的檔案"))


def _resolve() -> str:
    env = normalize(os.environ.get("XQ_LANG"))
    if env:
        return env
    s = normalize(_settings().get("lang"))
    if s:
        return s
    return "zh" if _previous_user() else detect_system()


def current() -> str:
    global _cur
    if _cur is None:
        _cur = _resolve()
    return _cur


def ensure_saved():
    """第一次啟動：把自動選到的語言記下來（之後 .bat 和程式都用同一個）。"""
    if normalize(os.environ.get("XQ_LANG")):
        return
    if not normalize(_settings().get("lang")):
        from . import config
        try:
            config.save_user_settings(lang=current())
        except OSError:
            pass


def set_lang(code: str) -> str:
    """網頁上切換語言：存進 user_settings.json，立刻生效。"""
    global _cur
    c = normalize(code)
    if not c:
        raise ValueError(t("err.lang_unknown"))
    from . import config
    config.save_user_settings(lang=c)
    _cur = c
    return c


def fill(s: str, kw: dict) -> str:
    """{name} 換成參數。英文複數：有 n 時，{s}／{es} 在 n 不是 1 時變成 "s"／"es"，{are} 是 is／are（例如 "{n} item{s}"）。"""
    if "n" in kw:
        try:
            one = float(kw["n"]) == 1
        except (TypeError, ValueError):
            one = False
        kw = {"s": "" if one else "s", "es": "" if one else "es", "are": "is" if one else "are", **kw}
    return _VAR.sub(lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), s)


def t(key: str, lang: str = None, **kw) -> str:
    """取出目前語言的文字；{name} 換成參數。缺字時用繁體中文，再沒有就回傳 key。"""
    s = table(lang or current()).get(key)
    if s is None:
        s = table(FALLBACK).get(key, key)
    return fill(s, kw) if kw else s


def options():
    return [{"code": k, "name": v} for k, v in LANGS.items()]


# ---- 資料庫裡存的分類原因（固定用中文代碼存，舊資料也一樣），顯示時才翻成目前語言
_REASONS = {"截圖尺寸": "reason.shot_size", "截圖檔名": "reason.shot_name", "文件檔": "reason.document",
            "你手動分類": "reason.manual", "AI 判斷": "reason.ai", "像你分類過的檔案": "reason.learned",
            "索引需要重建": "reason.reindex", "無法分析的檔案類型": "reason.unknown_type",
            "AI 無法判斷": "reason.ai_unsure"}
_REASON_RE = [(re.compile(r"^最像「(?P<cat>.*)」但不夠確定$"), "reason.closest"),
              (re.compile(r"^與 (?P<name>.*) 完全相同$"), "reason.same_as"),
              (re.compile(r"^類別「(?P<cat>.*)」已刪除$"), "reason.cat_deleted")]


def reason(code):
    """把資料庫裡的原因代碼換成目前語言的說明（不認得的照原樣顯示）。"""
    if not code:
        return code
    k = _REASONS.get(code)
    if k:
        return t(k)
    for rx, key in _REASON_RE:
        m = rx.match(code)
        if m:
            return t(key, **m.groupdict())
    return code
