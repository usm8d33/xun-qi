"""安全的檔案操作：只在檔案資料夾內動作，絕不覆蓋、絕不刪除使用者的檔案。"""
import re
import shutil
from pathlib import Path

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean_name(name: str) -> str:
    """手機傳來的檔名可能帶資料夾路徑或 Windows 不允許的字元，只留檔名本身。"""
    name = name.replace("\\", "/").split("/")[-1].strip().strip(".")
    name = _BAD.sub("_", name) or "file"
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    return name[:180]


def unique_path(folder: Path, name: str) -> Path:
    """同名時改成 xxx (1).jpg，永遠不覆蓋。"""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    i = 1
    while True:
        q = folder / f"{stem} ({i}){suf}"
        if not q.exists():
            return q
        i += 1


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def move_within(src: Path, dst_folder: Path, root: Path) -> Path:
    """只允許在 root（檔案資料夾）裡面搬移本程式自己收到的檔案。"""
    if not (inside(src, root) and inside(dst_folder, root)):
        raise PermissionError(f"拒絕在檔案資料夾以外搬移檔案：{src}")
    dst = unique_path(dst_folder, src.name)
    shutil.move(str(src), str(dst))
    return dst


def move_exact(src: Path, dst: Path, root: Path) -> None:
    """搬到指定的檔名；目的地已存在就丟 FileExistsError（絕不覆蓋）。只在檔案資料夾內搬。"""
    import os
    if not (inside(src, root) and inside(dst.parent, root)):
        raise PermissionError(f"拒絕在檔案資料夾以外搬移檔案：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.rename(src, dst)                # Windows：目的地存在會失敗，不會覆蓋
    else:
        os.link(src, dst)                  # POSIX：link 不會覆蓋
        os.unlink(src)


def claim_name(tmp: Path, folder: Path, name: str) -> Path:
    """把暫存檔改成正式檔名；同名就換下一個，絕不覆蓋（兩個上傳同時搶同一個名字也安全）。"""
    import os
    for _ in range(10000):
        final = unique_path(folder, name)
        try:
            if os.name == "nt":
                os.rename(tmp, final)          # Windows：目的地已存在會丟 FileExistsError，不會覆蓋
            else:
                os.link(tmp, final)            # POSIX：link 不會覆蓋，成功後再移除暫存名稱
                os.unlink(tmp)
            return final
        except FileExistsError:
            continue
    raise FileExistsError(f"找不到可用的檔名：{name}")


TMP_PREFIX = ".~xq-"   # 接收中的暫存檔（程式自己的，不是使用者的檔案）


def is_tmp(p: Path) -> bool:
    return p.name.startswith(TMP_PREFIX) or p.name.endswith(".part")

