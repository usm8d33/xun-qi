"""把各種檔案轉成模型吃得下的東西：圖片 → PIL、影片 → 幾張畫面、文件 → 文字。"""
import datetime
import io
import logging
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("extract")

try:
    import pillow_heif
    pillow_heif.register_heif_opener()  # iPhone 的 HEIC
except Exception:  # noqa
    log.warning("沒有 pillow-heif，HEIC 照片將無法分析")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}
DOC_EXT = {".pdf", ".docx", ".txt", ".md", ".csv", ".rtf"}
MAX_SIDE = 896  # 縮圖後再給模型，省記憶體


def kind_of(path: Path) -> str:
    s = path.suffix.lower()
    if s in IMAGE_EXT:
        return "image"
    if s in VIDEO_EXT:
        return "video"
    if s in DOC_EXT:
        return "document"
    return "other"


def load_image(path: Path) -> Image.Image:
    # 用 with 立刻關閉檔案：Windows 上沒關的檔案無法被刪除或搬移
    with Image.open(path) as f:
        im = ImageOps.exif_transpose(f).convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    return im


def video_frames(path: Path, n=4):
    import cv2
    cap = cv2.VideoCapture(str(path))
    frames = []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            ok, fr = cap.read()
            if ok:
                frames.append(fr)
        else:
            for i in range(n):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / n))
                ok, fr = cap.read()
                if ok:
                    frames.append(fr)
    finally:
        cap.release()  # 一定要釋放，否則 Windows 上影片會被鎖住無法刪除
    out = []
    for fr in frames:
        im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out.append(im)
    return out


def video_segments(path: Path, every=2.0, max_n=150):
    """影片片段：每 every 秒抓一格（太長的影片最多 max_n 格，平均分配）。回傳 [(秒數, 圖片)]。"""
    import cv2
    cap = cv2.VideoCapture(str(path))
    out = []
    frames = []   # 每讀到一格就立刻縮小，不把原尺寸畫格（4K 約 24MB 一張）全部留在記憶體

    def keep(t, ok, fr):
        if ok and fr is not None:
            im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            frames.append((float(t), im))

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = total / fps if fps > 0 and total > 0 else 0
        if dur <= 0:
            ok, fr = cap.read()
            keep(0.0, ok, fr)
        else:
            n = max(1, min(int(max_n), int(dur // every) + 1))
            step = every if n < max_n else dur / n
            times = [round(min(max(0.0, dur - 0.1), step * i + (step / 2 if n == max_n else 0)), 2) for i in range(n)]
            seq = n > 20 and step <= 3.0          # 片段很密時循序讀比較快（不用每格都跳轉）
            if seq:
                want, k, i = [int(t * fps) for t in times], 0, 0
                while k < len(want):
                    if not cap.grab():
                        break
                    if i >= want[k]:
                        ok, fr = cap.retrieve()
                        keep(times[k], ok, fr)
                        k += 1
                    i += 1
            else:
                for t in times:
                    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                    ok, fr = cap.read()
                    keep(t, ok, fr)
    finally:
        cap.release()  # 一定要釋放，否則 Windows 上影片會被鎖住
    out.extend(frames)
    return out


def frame_at(path: Path, t: float):
    """影片某一秒的畫面。"""
    import cv2
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(t)) * 1000)
        ok, fr = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, fr = cap.read()
    finally:
        cap.release()
    if not ok:
        return None
    return Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))


def pdf_scan_image(path: Path):
    """掃描成圖片的 PDF（讀不出文字）：取第 1 頁裡最大的那張圖給 OCR。"""
    try:
        from pypdf import PdfReader
        with open(path, "rb") as fh:
            r = PdfReader(io.BytesIO(fh.read()))
        imgs = list(r.pages[0].images) if r.pages else []
        if not imgs:
            return None
        best = max(imgs, key=lambda x: len(x.data))
        with Image.open(io.BytesIO(best.data)) as f:
            im = ImageOps.exif_transpose(f).convert("RGB")
        return im
    except Exception as e:  # noqa
        log.debug("讀不出 PDF 圖片 %s：%s", path.name, e)
        return None


def load_image_full(path: Path, max_side=4000) -> Image.Image:
    """OCR 用：保留較高解析度（小字才讀得到）。"""
    with Image.open(path) as f:
        im = ImageOps.exif_transpose(f).convert("RGB")
    im.thumbnail((max_side, max_side))
    return im


def document_text(path: Path, limit=6000) -> str:
    s = path.suffix.lower()
    try:
        if s == ".pdf":
            from pypdf import PdfReader
            with open(path, "rb") as fh:
                r = PdfReader(io.BytesIO(fh.read()))
            text = "\n".join((p.extract_text() or "") for p in r.pages[:10])
        elif s == ".docx":
            import docx
            text = "\n".join(p.text for p in docx.Document(io.BytesIO(path.read_bytes())).paragraphs)
        else:
            raw = path.read_bytes()[: limit * 4]
            for enc in ("utf-8", "utf-16", "cp950", "big5"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", "ignore")
    except Exception as e:  # noqa
        log.warning("讀不出文件文字 %s：%s", path.name, e)
        text = ""
    return text.strip()[:limit]


def taken_date(path: Path, fallback_iso=None) -> datetime.date:
    """拍攝日期：先看 EXIF，再看手機傳來的修改時間，最後用今天。"""
    try:
        if kind_of(path) == "image":
            with Image.open(path) as f:
                exif = f.getexif()
            v = exif.get(36867) or exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if v:
                return datetime.datetime.strptime(str(v)[:10], "%Y:%m:%d").date()
        if kind_of(path) == "video":
            from .geo import video_created
            d = video_created(path)
            if d:
                return d.astimezone().date()   # 換成電腦的時區
    except Exception:
        pass
    if fallback_iso:
        try:
            return datetime.datetime.fromisoformat(fallback_iso.replace("Z", "+00:00")).date()
        except Exception:
            pass
    return datetime.date.today()


def sharpness(path: Path) -> float:
    """清晰度：統一縮到長邊 512 後算邊緣強度（拉普拉斯變異數），數字越大越清楚。
    先縮到同一大小，大圖和小圖才能公平比較。"""
    import numpy as np
    from PIL import ImageFilter
    im = load_image(path).convert("L")
    im.thumbnail((512, 512))
    lap = im.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    return float(np.asarray(lap, dtype=np.float32).var())
