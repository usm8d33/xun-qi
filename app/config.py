"""讀取 config.yaml 與網頁上存的設定，決定檔案存放位置。

程式資料夾只放程式、Python、模型；手機傳來的檔案、搜尋索引、裝置憑證都放在「檔案資料夾」
（預設 桌面\\Xun-Qi 尋棲），重裝程式不影響檔案與索引。
"""
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # 程式資料夾
USER_SETTINGS = ROOT / "user_settings.json"    # 網頁「存放位置」寫在這裡
FOLDER_NAME = "Xun-Qi 尋棲"
OLD_FOLDER_NAME = "LocalSend 收到的檔案"          # 改名前的預設位置：還在就繼續用，不用搬檔

# 三種版本（setup.bat 選的，寫在 runtime\edition.txt）
#   lite     輕量版：ONNX int8、不需要 PyTorch，CPU 或 DirectML；OCR mobile；影片每 6 秒一格
#   standard 標準版：PyTorch（有 NVIDIA/Intel 顯示卡就用）；OCR mobile；影片每 4 秒一格
#   flagship 旗艦版：PyTorch；OCR server（最準）；影片每 2 秒一格
EDITIONS = {"lite": ("輕量版", 6.0, "mobile"), "standard": ("標準版", 4.0, "mobile"),
            "flagship": ("旗艦版", 2.0, "server")}
STATE_NAME = ".整理器資料"                       # 隱藏：索引、憑證、紀錄、分類暫存

DEFAULTS = {
    "alias_plain": "我的電腦-傳送",
    "alias_sort": "我的電腦-傳送並分類",
    "port_plain": 53317,
    "port_sort": 53318,
    "mode_select": "phone",
    "web_port": 8765,
    "files_dir": "",          # 空白 = 桌面\Xun-Qi 尋棲
    "edition": "",            # 空白 = 用 setup.bat 選的版本
    "categories": {},
    "temperature": 50.0,
    "min_confidence": 0.35,
    "doubt_confidence": 0.5,
    "doubt_close_ratio": 0.6,
    "learn_similarity": 0.82,
    "screenshot_category": "截圖",
    "document_category": "文件收據",
    "unsorted_category": "未分類",
    "near_duplicate": 0.97,
    "date_subfolder": True,
    "embed_dim": 256,
    "video_frames": 4,          # 舊設定（縮圖用）
    "video_seg_every": 0,       # 影片片段：每幾秒抓一格（0 = 依版本 6／4／2 秒）
    "video_seg_max": 150,       # 每部影片最多幾格（太長就平均分配）
    "ocr_enabled": True,        # 讀照片裡的文字（models\\ocr 沒有模型時自動關閉）
    "ocr_max_side": 2000,       # OCR 偵測時圖片長邊最多幾像素
    "ocr_min_score": 0.5,       # 辨識信心低於這個的文字不要
    "ocr_doc_chars": 30,        # 照片裡讀到這麼多字以上……
    "ocr_doc_boost": 0.02,      # ……分類時稍微偏向「文件收據」（0 = 不偏向）
    "model_idle_unload_min": 10,
    "force_cpu": False,
}

PLAIN_NAME = "傳送"
SORTED_NAME = "已分類"
DUP_NAME = "疑似重複"
OTHER_NAME = "其他檔案"
REMOVED_NAME = "待刪除"   # 你在「重複比較」選擇不要的檔案放這裡，由你自己決定要不要刪
EXPORT_TEMP = "匯出暫存"  # （在 .整理器資料 裡）打包 ZIP 下載用，下載完就清掉


def desktop_dir() -> Path:
    """Windows 真正的桌面位置（桌面被移到 OneDrive 或其他磁碟也找得到）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                            ("d4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")  # FOLDERID_Desktop
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
                p = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                if p.exists():
                    return p
        except Exception:
            pass
    p = Path(os.path.expanduser("~")) / "Desktop"
    return p if p.exists() else Path(os.path.expanduser("~"))


def read_user_settings() -> dict:
    try:
        return json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_user_settings(**kw):
    s = read_user_settings()
    s.update(kw)
    USER_SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


class Config(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    # ---- 路徑 ----
    @property
    def data(self) -> Path:          # 檔案資料夾（所有收到的檔案都在這裡面）
        p = str(self.get("files_dir") or "").strip()
        if p:
            p = Path(os.path.expandvars(os.path.expanduser(p)))
        else:
            p = desktop_dir() / FOLDER_NAME
            old = desktop_dir() / OLD_FOLDER_NAME
            if not p.exists() and (old / STATE_NAME).exists():
                p = old
        if not p.is_absolute():
            p = ROOT / p
        return p.resolve()

    @property
    def inbox_plain(self) -> Path:   # 「傳送」收到的檔案
        return self.data / PLAIN_NAME

    @property
    def state_dir(self) -> Path:     # 索引、憑證、紀錄
        return self.data / STATE_NAME

    @property
    def inbox_sort(self) -> Path:    # 「傳送並分類」暫存，處理完會移到 已分類
        return self.state_dir / "分類中"

    @property
    def sorted_dir(self) -> Path:
        return self.data / SORTED_NAME

    @property
    def dup_dir(self) -> Path:
        return self.data / DUP_NAME

    @property
    def other_dir(self) -> Path:
        return self.data / OTHER_NAME

    @property
    def removed_dir(self) -> Path:
        return self.data / REMOVED_NAME

    @property
    def edition(self) -> str:
        e = str(self.get("edition") or "").strip().lower()
        if not e:
            try:
                e = (ROOT / "runtime" / "edition.txt").read_text(encoding="utf-8").strip().lower()
            except OSError:
                e = ""
        return e if e in EDITIONS else "standard"

    @property
    def edition_name(self) -> str:
        return EDITIONS[self.edition][0]

    @property
    def seg_every(self) -> float:
        return float(self.get("video_seg_every") or 0) or EDITIONS[self.edition][1]

    @property
    def ocr_size(self) -> str:
        return EDITIONS[self.edition][2]

    @property
    def model_dir(self) -> Path:
        if self.edition == "lite":
            return ROOT / "models" / "embeddinggemma-2-onnx"
        return ROOT / "models" / "embeddinggemma-2"

    @property
    def ocr_dir(self) -> Path:
        return ROOT / "models" / "ocr"

    @property
    def synced_by_onedrive(self) -> bool:
        return any("onedrive" in part.lower() for part in self.data.parts)


def hide(path: Path):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def load() -> Config:
    cfg = dict(DEFAULTS)
    f = ROOT / "config.yaml"
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            cfg.update(yaml.safe_load(fh) or {})
    us = read_user_settings()
    if us.get("files_dir"):
        cfg["files_dir"] = us["files_dir"]   # 網頁上選的位置優先
    c = Config(cfg)
    try:
        for d in (c.inbox_plain, c.sorted_dir, c.state_dir, c.inbox_sort):
            d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"無法建立檔案資料夾 {c.data}（{e}）。如果是外接硬碟沒接上，請接上後再開；"
                           f"或刪除程式資料夾裡的 user_settings.json 改回預設的桌面位置。") from e
    hide(c.state_dir)
    from . import backup, categories
    c["restored"] = backup.apply_pending(c.state_dir)   # 上次在網頁上選的「還原」，在開資料庫前換上
    categories.apply_saved(c)                            # 網頁上自訂過的類別
    return c


def resolve_new_location(raw: str) -> Path:
    """使用者指定的新位置。若資料夾不是空的、也不是本程式的檔案資料夾，就在裡面建一個子資料夾，
    確保程式不會和使用者原本的檔案混在一起。"""
    raw = (raw or "").strip().strip('"')
    if not raw:
        raise ValueError("請輸入資料夾路徑")
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        raise ValueError("請輸入完整路徑，例如 D:\\照片\\尋棲")
    p = p.resolve()
    if p == Path(p.anchor):
        p = p / FOLDER_NAME   # 不直接用整顆磁碟根目錄
    try:
        p.relative_to(ROOT)
        raise ValueError("檔案資料夾要和程式資料夾分開，請選程式資料夾以外的位置")
    except ValueError as e:
        if "分開" in str(e):
            raise
    if p.exists():
        if not p.is_dir():
            raise ValueError("這個路徑是檔案，不是資料夾")
        if any(p.iterdir()) and not (p / STATE_NAME).exists():
            p = p / FOLDER_NAME
    return p
