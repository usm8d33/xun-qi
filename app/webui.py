"""本機網頁介面（只聽 127.0.0.1，區網其他人連不到）。"""
import io
import json
import logging
import mimetypes
import os
import secrets
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import backup, categories, config, extract, overview
from .processor import FILTER_KEYS
from .safefs import inside

log = logging.getLogger("webui")
STATIC = Path(__file__).parent / "static"

PICK_PS = Path(__file__).with_name("pick_folder.ps1")
OPEN_PS = Path(__file__).with_name("open_window.ps1")


def open_in_explorer(path, select=False):
    """用檔案總管開啟資料夾（select=True 時在資料夾中選取這個檔案），並把視窗拉到最前面。"""
    if sys.platform != "win32":
        return
    if OPEN_PS.exists():
        env = dict(os.environ, XQ_OPEN_PATH=str(path), XQ_OPEN_SELECT="1" if select else "0")
        subprocess.Popen(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(OPEN_PS)], env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        subprocess.Popen(["explorer", "/select,", str(path)] if select else ["explorer", str(path)])


def open_with_default_app(path):
    """用電腦預設的程式開啟檔案；先讓 Windows 允許新視窗跳到前面。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
        u.AllowSetForegroundWindow(-1)   # ASFW_ANY
    except Exception:  # noqa
        pass
    os.startfile(str(path))


def pick_folder(title="選擇手機檔案要存放的資料夾", kind="folder"):
    """跳出 Windows 的「選擇資料夾」（或 kind="files"：選檔案，可多選）視窗。
    回傳使用者選的路徑（多個時以換行分隔；取消回傳空字串）。"""
    if sys.platform != "win32":
        return ""
    env = dict(os.environ, LSS_PICK_TITLE=title, XQ_PICK_KIND=kind)
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(PICK_PS)], capture_output=True, env=env,
                       timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.decode("utf-8", "replace").strip()


def media_info(p: Path):
    """圖片解析度（只讀檔頭，很快）。"""
    try:
        if extract.kind_of(p) == "image":
            from PIL import Image
            with Image.open(p) as im:
                return im.size
    except Exception:
        pass
    return None


INLINE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",            # 不含腳本、瀏覽器可直接顯示
              ".mov", ".mp4", ".m4v", ".webm", ".3gp"}


def start(app):
    cfg = app.cfg
    token = secrets.token_urlsafe(24)   # 每次啟動產生；只有本程式的網頁拿得到，外部網站無法偽造修改請求

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, p: Path, inline=False):
            """送出檔案，支援 Range（影片才能從某一秒開始播、可以拖曳進度）。讀完立即關檔。
            只有不含腳本的照片/影片格式會在頁面內顯示；其他（HTML、SVG、PDF…）一律當成下載，
            並加上 CSP sandbox，避免收到的檔案在管理介面的來源執行程式。"""
            if inline:
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                if p.suffix.lower() in (".mov", ".m4v"):
                    ctype = "video/mp4"          # 讓瀏覽器試著播放 iPhone 的 MOV
            else:
                ctype = "application/octet-stream"
            size = p.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(size - 1, int(m.group(2)))
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'")
            if not inline:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(p.name)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with open(p, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        b = fh.read(min(1024 * 1024, left))
                        if not b:
                            break
                        self.wfile.write(b)
                        left -= len(b)
            except (ConnectionError, OSError):
                pass   # 瀏覽器拖曳進度時會中斷舊的連線，正常

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _row_path(self, q):
            try:
                r = app.store.get(int(q.get("id", "0")))
            except ValueError:
                return None
            if r is None:
                return None
            p = app.store.abspath(r["path"])
            return p if p.exists() and inside(p, cfg.data) else None

        def _host_ok(self):
            """只接受用 127.0.0.1／localhost 開的網頁（擋 DNS rebinding）。"""
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

        def _origin_ok(self):
            """修改類請求：必須來自本程式網頁，帶著這次啟動的憑證。"""
            port = self.server.server_address[1]
            if (self.headers.get("Sec-Fetch-Site") or "").lower() in ("cross-site", "same-site"):
                return False
            o = (self.headers.get("Origin") or "").lower()
            if o and o not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}:
                return False
            return secrets.compare_digest(self.headers.get("X-XQ-Token") or "", token)

        def _deny(self):
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 64 * 1024 * 1024:
                self.rfile.read(n)
            return self._send(403, b'{"error": "forbidden"}')

        # ---- 傳到手機
        def _send_api(self, path, q, body):
            try:
                d = json.loads(body or b"{}") if body else {}
                if path == "/api/send/discover":
                    app.proc_discover(scan=q.get("scan") == "1")
                    return self._json({"ok": True})
                if path == "/api/send/pick":
                    kind = "files" if q.get("kind") == "files" else "folder"
                    raw = pick_folder("選擇要傳到手機的檔案" if kind == "files" else "選擇要傳到手機的資料夾", kind)
                    paths = [x.strip() for x in raw.splitlines() if x.strip()]
                    r = app.stage.add_paths(paths) if paths else {"added": 0, "skipped": 0}
                    return self._json({"ok": True, **r, "items": app.stage.list()})
                if path == "/api/send/unstage":
                    app.stage.remove(None if d.get("all") else list(d.get("ids") or []))
                    return self._json({"ok": True, "items": app.stage.list()})
                if path == "/api/send/start":
                    if d.get("source") == "stage":
                        items = app.sender.items_from_stage(d.get("ids") or None)
                        label = "電腦上的檔案"
                    elif d.get("batch"):
                        items = app.sender.items_from_library(app.store.batch_ids(str(d["batch"])))
                        label = "整批傳回"
                    else:
                        items = app.sender.items_from_library(d.get("ids") or [])
                        label = "傳回手機"
                    return self._json({"ok": True, **app.sender.start(str(d.get("peer") or ""), items, label)})
                if path == "/api/send/cancel":
                    return self._json({"ok": app.sender.cancel()})
                if path == "/api/send/clear":
                    app.sender.clear()
                    return self._json({"ok": True})
            except Exception as e:  # noqa
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._send(404, b"")

        def do_GET(self):
            if not self._host_ok():
                return self._deny()
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                html = (STATIC / "index.html").read_text(encoding="utf-8")
                html = html.replace("</head>", f'<meta name="xq-token" content="{token}"></head>', 1)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/send/state":
                from .sender import PEERS
                return self._json({"peers": PEERS.list(), "scanning": app.scanning, "job": app.sender.status(),
                                   "stage": app.stage.list()})
            if u.path == "/api/status":
                return self._json(app.status())
            if u.path == "/api/home":
                return self._json(overview.home(app))
            if u.path == "/api/batch_check":
                return self._json(overview.batch_check(app, q.get("batch", "")))
            if u.path == "/api/batches":
                view = q.get("view") if q.get("view") in ("pending", "reviewed", "all") else "pending"
                try:
                    limit = max(1, min(500, int(q.get("limit", "30"))))
                except ValueError:
                    limit = 30
                items, total = app.store.batches(view, q.get("cat") or None, limit)
                return self._json({"batches": items, "total": total})
            if u.path == "/api/dups":
                groups = app.store.dup_groups()
                for g in groups:
                    for f in g["files"]:
                        p = app.store.abspath(f.pop("path"))
                        wh = media_info(p) if p.exists() else None
                        f["w"], f["h"] = (wh or (None, None))
                        f["folder"] = str(p.parent.relative_to(cfg.data)) if p.exists() else ""
                        if f["kind"] == "image" and p.exists():
                            f["sharp"] = app.proc.sharpness(f["id"], p)
                return self._json({"groups": groups})
            if u.path == "/api/removed":
                return self._json({"files": app.proc.removed_list(), "folder": str(cfg.removed_dir)})
            if u.path == "/api/categories":
                return self._json({"categories": categories.listing(cfg)})
            if u.path == "/api/places":
                return self._json(app.store.places())
            if u.path == "/api/timeline":
                filt = {k: q.get(k, "") for k in FILTER_KEYS}
                return self._json({"months": app.store.timeline(**filt)})
            if u.path == "/api/backups":
                return self._json({"backups": backup.listing(cfg), "folder": str(backup.backup_dir(cfg)),
                                   "pending": backup.pending(cfg)})
            if u.path == "/api/export_dl":
                p = app.proc.export_file(q.get("token", ""))
                if not p:
                    return self._send(404, "下載已過期，請重新匯出".encode())
                try:
                    size = p.stat().st_size
                    fname = f"尋棲匯出 {time.strftime('%Y-%m-%d %H%M')}.zip"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", f"attachment; filename=\"export.zip\"; filename*=UTF-8''{quote(fname)}")
                    self.end_headers()
                    with open(p, "rb") as fh:
                        while True:
                            b = fh.read(1024 * 1024)
                            if not b:
                                break
                            self.wfile.write(b)
                finally:
                    try:
                        p.unlink()   # 程式自己產生的暫存 zip，下載完就清掉
                    except OSError:
                        pass
                return
            if u.path in ("/api/search", "/api/similar"):
                filt = {k: q.get(k, "") for k in FILTER_KEYS}
                try:
                    if u.path == "/api/similar":
                        return self._json(app.proc.similar(int(q.get("id", "0")), **filt))
                    text = (q.get("q") or "").strip()
                    if not text and not any(filt.values()):
                        return self._json([])
                    try:
                        limit = max(1, min(5000, int(q.get("limit", "300"))))
                    except ValueError:
                        limit = 300
                    return self._json(app.proc.search(text, limit=limit, **filt))
                except ValueError as e:      # 可以理解的提示（例如需要先重建索引）
                    return self._json({"error": str(e)}, 400)
                except Exception as e:  # noqa
                    return self._json({"error": str(e)}, 500)
            if u.path == "/api/text":
                try:
                    return self._json(app.proc.file_text(int(q.get("id", "0"))))
                except Exception as e:  # noqa
                    return self._json({"error": str(e)}, 404)
            if u.path == "/api/thumb":
                p = self._row_path(q)
                if not p:
                    return self._send(404, b"")
                try:
                    k = extract.kind_of(p)
                    if k == "image":
                        im = extract.load_image(p)
                    elif k == "video" and q.get("t"):
                        im = extract.frame_at(p, float(q["t"]))      # 影片某一秒的畫面
                        if im is None:
                            return self._send(404, b"")
                    elif k == "video":
                        fr = extract.video_frames(p, 1)
                        if not fr:
                            return self._send(404, b"")
                        im = fr[0]
                    else:
                        return self._send(404, b"")
                    try:
                        side = max(64, min(1600, int(q.get("s", "320"))))
                    except ValueError:
                        side = 320
                    im.thumbnail((side, side))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=80)
                    return self._send(200, buf.getvalue(), "image/jpeg", cache=True)
                except Exception:
                    return self._send(404, b"")
            if u.path == "/api/file":
                p = self._row_path(q)
                if not p:
                    return self._send(404, b"")
                return self._stream(p, inline=p.suffix.lower() in INLINE_EXT)
            self._send(404, b"")

        def do_POST(self):
            if not (self._host_ok() and self._origin_ok()):
                return self._deny()
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/send/drop":     # 拖進網頁的檔案可能很大：直接串流寫到暫存，不先讀進記憶體
                try:
                    if n <= 0:
                        raise ValueError("檔案是空的")
                    iid = app.stage.add_drop(q.get("name", ""), self.rfile, n)
                    return self._json({"ok": True, "id": iid, "items": app.stage.list()})
                except Exception as e:  # noqa
                    self.close_connection = True
                    return self._json({"ok": False, "error": str(e)}, 400)
            body = self.rfile.read(n) if n else b""
            if u.path.startswith("/api/send/"):
                return self._send_api(u.path, q, body)
            if u.path == "/api/similar_upload":
                if not 0 < n <= 60 * 1024 * 1024:
                    return self._json({"error": "圖片太大或是空的"}, 400)
                data = body
                filt = {k: q.get(k, "") for k in FILTER_KEYS}
                try:
                    return self._json(app.proc.similar_image(data, **filt))
                except Exception as e:  # noqa
                    return self._json({"error": "讀不了這張圖片：" + str(e)}, 400)
            if u.path == "/api/mode" and q.get("mode") in ("plain", "sort"):
                app.web_mode = q["mode"]
                return self._json({"ok": True, "mode": app.web_mode})
            if u.path == "/api/set_category":
                try:
                    app.proc.set_category(int(q.get("id", "0")), q.get("cat", ""))
                    return self._json({"ok": True})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path in ("/api/confirm_batch", "/api/unconfirm_batch"):
                bid = q.get("batch", "")
                if not bid:
                    return self._json({"ok": False, "error": "缺少批次"}, 400)
                n = app.proc.confirm_batch(bid, 1 if u.path == "/api/confirm_batch" else 0)
                return self._json({"ok": True, "changed": n})
            if u.path == "/api/check":
                try:
                    fid = int(q.get("id", "0"))
                except ValueError:
                    return self._json({"ok": False, "error": "參數錯誤"}, 400)
                return self._json({"ok": bool(app.proc.check(fid, 0 if q.get("v") == "0" else 1))})
            if u.path == "/api/dup_apply":
                try:
                    d = json.loads(body or b"{}")
                    return self._json({"ok": True, **app.proc.dup_apply(d.get("keep", []), d.get("remove", []))})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path == "/api/clear_exact":
                try:
                    return self._json({"ok": True, "removed": app.proc.clear_exact(q.get("batch", ""))})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path == "/api/restore":
                try:
                    app.proc.restore(int(q.get("id", "0")))
                    return self._json({"ok": True})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path == "/api/open_removed":
                cfg.removed_dir.mkdir(parents=True, exist_ok=True)
                open_in_explorer(cfg.removed_dir)
                return self._json({"ok": True})
            if u.path == "/api/pick_folder":
                try:
                    title = "選擇要匯出到哪個資料夾" if q.get("for") == "export" else "選擇手機檔案要存放的資料夾"
                    return self._json({"ok": True, "path": pick_folder(title)})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)})
            if u.path == "/api/set_location":
                try:
                    raw = json.loads(body or b"{}").get("path", "")
                    newp = config.resolve_new_location(raw)
                    newp.mkdir(parents=True, exist_ok=True)
                    if newp == cfg.data:
                        return self._json({"ok": True, "path": str(newp), "restart": False})
                    config.save_user_settings(files_dir=str(newp))
                    app.pending_location = str(newp)
                    return self._json({"ok": True, "path": str(newp), "restart": True})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path in ("/api/cat_add", "/api/cat_edit", "/api/cat_delete"):
                try:
                    d = json.loads(body or b"{}")
                    if u.path == "/api/cat_add":
                        return self._json({"ok": True, "name": app.proc.cat_add(d.get("name"), d.get("desc"),
                                                                                d.get("emoji"))})
                    if u.path == "/api/cat_edit":
                        return self._json({"ok": True, **app.proc.cat_edit(d.get("name"), d.get("desc"),
                                                                           d.get("emoji"), d.get("new_name"))})
                    return self._json({"ok": True, **app.proc.cat_delete(d.get("name"))})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path in ("/api/export_copy", "/api/export_zip"):
                try:
                    d = json.loads(body or b"{}")
                    by = d.get("by") if d.get("by") in ("category", "month", "place") else ""
                    if u.path == "/api/export_zip":
                        return self._json({"ok": True, **app.proc.export_zip(d.get("ids", []), by)})
                    r = app.proc.export_copy(d.get("ids", []), d.get("dest", ""), by)
                    open_in_explorer(r["folder"])
                    return self._json({"ok": True, **r})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path == "/api/backup_make":
                try:
                    p = backup.make(cfg, app.store)
                    return self._json({"ok": True, "name": p.name})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path in ("/api/backup_restore", "/api/backup_upload"):
                try:
                    if u.path == "/api/backup_upload":
                        auto = backup.stage(cfg, app.store, body)
                    else:
                        auto = backup.stage_named(cfg, app.store, json.loads(body or b"{}").get("name", ""))
                    return self._json({"ok": True, "auto": auto.name})
                except Exception as e:  # noqa
                    return self._json({"ok": False, "error": str(e)}, 400)
            if u.path == "/api/backup_cancel":
                backup.cancel(cfg)
                return self._json({"ok": True})
            if u.path == "/api/open_backups":
                backup.backup_dir(cfg).mkdir(parents=True, exist_ok=True)
                open_in_explorer(backup.backup_dir(cfg))
                return self._json({"ok": True})
            if u.path == "/api/reclassify":
                return self._json({"queued": app.proc.reclassify_all()})
            if u.path == "/api/reindex":
                return self._json({"queued": app.proc.reindex_mismatch()})
            if u.path == "/api/index_plain":
                return self._json({"queued": app.proc.index_plain()})
            if u.path == "/api/reveal":
                p = self._row_path(q)
                if p:
                    open_in_explorer(p, select=True)
                return self._json({"ok": bool(p)})
            if u.path == "/api/open_data":
                open_in_explorer(cfg.data)
                return self._json({"ok": True})
            if u.path == "/api/enrich_pause":
                app.proc.pause_enrich(q.get("pause") == "1")
                return self._json(app.proc.enrich_status())
            if u.path == "/api/open_file":
                p = self._row_path(q)
                if p and extract.kind_of(p) not in ("image", "video", "document"):
                    return self._json({"ok": False, "error": "這種檔案不從這裡開啟，請用「在資料夾中顯示」"})
                if p and sys.platform == "win32":
                    open_with_default_app(p)      # 用電腦預設的播放器／檢視器開啟
                return self._json({"ok": bool(p)})
            if u.path == "/api/unload":
                app.emb.unload()
                app.proc.ocr.unload()
                return self._json({"ok": True})
            self._send(404, b"")

    srv = ThreadingHTTPServer(("127.0.0.1", cfg.web_port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
