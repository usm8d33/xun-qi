"""傳到手機：本程式當 LocalSend 協定 v2 的「傳送端」。

兩種來源共用同一套傳送：
- 電腦上的任何檔案（Windows 視窗選檔案／資料夾，或拖進網頁的暫存檔）
- 原本從手機收到、已在檔案資料夾裡的檔案（傳送前用收件時的 SHA-256 核對沒被改過）

只讀檔，不搬移、不刪除使用者的任何檔案；只有「拖進網頁」產生的暫存檔會在傳完後清掉。
手機端要打開 LocalSend 並停在前景，按「接受」後才開始傳。
"""
import concurrent.futures
import datetime
import hashlib
import http.client
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import shutil
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .lang import t as _t
from .safefs import clean_name, inside

log = logging.getLogger("sender")

CHUNK = 1024 * 1024
PEER_TTL = 15 * 60          # 多久沒看到就從清單拿掉
ACCEPT_TIMEOUT = 300        # 等手機按「接受」最多 5 分鐘
IO_TIMEOUT = 60
MAX_FOLDER_FILES = 5000
DROP_DIR = "傳送暫存"        # （在 .整理器資料 裡）拖進網頁的檔案，傳完就清掉；實際名稱看 cfg.names["send_temp"]


class Cancelled(Exception):
    pass


class PhoneEnded(ValueError):
    """手機已結束這次傳送（後面的檔案不用再試）。"""


# ---------------------------------------------------------------- 看得到的手機
class PeerBook:
    """記錄區網中出現過的 LocalSend 裝置（多播宣告、或手機向我們登記時得知）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.peers = {}
        self.own = set()

    def seen(self, info: dict, ip: str):
        if not isinstance(info, dict):
            return
        fp = str(info.get("fingerprint") or "")
        if not fp or fp in self.own or not ip:
            return
        try:
            port = int(info.get("port") or 53317)
        except (TypeError, ValueError):
            port = 53317
        proto = "http" if info.get("protocol") == "http" else "https"
        with self.lock:
            old = self.peers.get(fp, {})
            self.peers[fp] = {
                "fingerprint": fp,
                "alias": str(info.get("alias") or old.get("alias") or _t("send.device"))[:60],
                "model": str(info.get("deviceModel") or old.get("model") or "")[:40],
                "type": str(info.get("deviceType") or old.get("type") or "")[:20],
                "ip": ip,
                "port": port if info.get("port") else old.get("port", port),
                "protocol": proto if info.get("protocol") else old.get("protocol", proto),
                "seen": time.time(),
            }

    def list(self):
        now = time.time()
        with self.lock:
            for k in [k for k, p in self.peers.items() if now - p["seen"] > PEER_TTL]:
                self.peers.pop(k)
            out = sorted(self.peers.values(), key=lambda p: -p["seen"])
        return [{**p, "ago": int(now - p["seen"])} for p in out]

    def get(self, fp):
        with self.lock:
            p = self.peers.get(fp)
            return dict(p) if p else None


PEERS = PeerBook()


# ---------------------------------------------------------------- 連線
def _ctx(dev):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE        # LocalSend 用自簽憑證；改用下面的指紋比對確認對象
    ctx.load_cert_chain(dev.cert, dev.key)  # 附上本裝置憑證，手機看到的指紋和我們宣告的一致
    return ctx


def _connect(peer, dev, timeout):
    if peer["protocol"] == "https":
        conn = http.client.HTTPSConnection(peer["ip"], peer["port"], timeout=timeout, context=_ctx(dev))
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        fp = hashlib.sha256(der or b"").hexdigest().upper()
        if fp != peer["fingerprint"].upper():
            conn.close()
            raise RuntimeError(_t("send.bad_cert"))
    else:
        conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=timeout)
        conn.connect()
    return conn


def probe(ip, dev, port=53317):
    """掃描用：直接向某個 IP 登記，對方有開 LocalSend 就會回傳它的資訊。"""
    try:
        conn = http.client.HTTPSConnection(ip, port, timeout=1.2, context=_ctx(dev))
        body = json.dumps(dev.info()).encode()
        conn.request("POST", "/api/localsend/v2/register", body=body,
                     headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
        r = conn.getresponse()
        data = r.read(65536)
        der = conn.sock.getpeercert(binary_form=True) if conn.sock else None
        conn.close()
        if r.status != 200:
            return None
        info = json.loads(data)
        real = hashlib.sha256(der or b"").hexdigest().upper()
        if str(info.get("fingerprint") or "").upper() != real:
            return None
        info.update(port=port, protocol="https")
        PEERS.seen(info, ip)
        return info
    except Exception:
        return None


def scan(dev, local_ips, port=53317):
    """掃描本機所在的每個 /24 網段（LocalSend 本身也有這個備援做法）。"""
    targets = []
    for ip in local_ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not a.is_private or a.is_loopback:
            continue
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        targets += [str(h) for h in net.hosts() if str(h) != ip]
    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        list(ex.map(lambda t: probe(t, dev, port), targets))
    return len(targets)


# ---------------------------------------------------------------- 待傳清單（電腦上的檔案）
class Stage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.items = {}   # id -> {path, name, size, temp}
        self.drop_root = cfg.state_dir / cfg.names["send_temp"]

    def clean_all(self):
        """啟動時清掉上次留下的拖曳暫存（只動程式自己的暫存資料夾）。"""
        if self.drop_root.exists():
            shutil.rmtree(self.drop_root, ignore_errors=True)

    def _blocked(self, p: Path):
        return inside(p, self.cfg.state_dir)   # 裝置私鑰、資料庫等不能傳出去

    def add_paths(self, paths):
        added, skipped = 0, 0
        files = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                for root, dirs, names in os.walk(p):
                    dirs[:] = sorted(d for d in dirs if not d.startswith((".", "$")))
                    for n in sorted(names):
                        if n.startswith(".") or n.lower() in ("desktop.ini", "thumbs.db"):
                            continue
                        files.append(Path(root) / n)
                        if len(files) > MAX_FOLDER_FILES:
                            raise ValueError(_t("send.too_many", n=MAX_FOLDER_FILES))
            elif p.is_file():
                files.append(p)
        with self.lock:
            have = {str(v["path"]).lower() for v in self.items.values()}
            for f in files:
                if self._blocked(f) or str(f).lower() in have:
                    skipped += 1
                    continue
                try:
                    size = f.stat().st_size
                except OSError:
                    skipped += 1
                    continue
                self.items[uuid.uuid4().hex[:12]] = {"path": f, "name": f.name, "size": size, "temp": False}
                have.add(str(f).lower())
                added += 1
        return {"added": added, "skipped": skipped}

    def add_drop(self, name, rfile, length):
        """拖進網頁的檔案：串流寫到暫存資料夾，不整個讀進記憶體。"""
        name = clean_name(name or _t("send.file"))
        folder = self.drop_root / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if length > free - 512 * 1024 * 1024:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError(_t("send.no_space"))
        p = folder / name
        left = length
        try:
            with open(p, "xb") as out:
                while left > 0:
                    b = rfile.read(min(CHUNK, left))
                    if not b:
                        raise ValueError(_t("send.incomplete"))
                    out.write(b)
                    left -= len(b)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        with self.lock:
            iid = uuid.uuid4().hex[:12]
            self.items[iid] = {"path": p, "name": name, "size": length, "temp": True}
        return iid

    def remove(self, ids=None):
        with self.lock:
            keys = list(self.items) if ids is None else [i for i in ids if i in self.items]
            gone = [self.items.pop(k) for k in keys]
        for it in gone:
            if it["temp"]:
                shutil.rmtree(it["path"].parent, ignore_errors=True)
        return len(gone)

    def list(self):
        with self.lock:
            return [{"id": k, "name": v["name"], "size": v["size"], "folder": "" if v["temp"] else str(v["path"].parent),
                     "temp": v["temp"]} for k, v in self.items.items()]

    def take(self, ids=None):
        with self.lock:
            keys = list(self.items) if not ids else [i for i in ids if i in self.items]
            return [(k, dict(self.items[k])) for k in keys]


# ---------------------------------------------------------------- 傳送工作
def _iso(ts):
    try:
        return datetime.datetime.fromtimestamp(ts).astimezone().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


class Sender:
    def __init__(self, cfg, store, devices_fn, stage: Stage, sha_fn):
        self.cfg = cfg
        self.store = store
        self.devices_fn = devices_fn
        self.stage = stage
        self.sha_fn = sha_fn
        self.lock = threading.Lock()
        self.job = None
        self._conn = None
        self._tl = threading.local()   # 每條傳送執行緒只改自己的工作，不會碰到後來的新工作

    @property
    def _mine(self):
        return self._tl.job

    # ---- 狀態
    def status(self):
        with self.lock:
            j = self.job
            if not j:
                return {"state": "idle"}
            return {k: v for k, v in j.items() if k not in ("items", "cancel")}

    def busy(self):
        with self.lock:
            return bool(self.job and self.job["state"] in ("checking", "waiting", "sending"))

    def clear(self):
        with self.lock:
            if self.job and self.job["state"] not in ("checking", "waiting", "sending"):
                self.job = None

    def cancel(self):
        with self.lock:
            j = self.job
            if not j or j["state"] not in ("checking", "waiting", "sending"):
                return False
            j["cancel"] = True
            conn = self._conn
        if conn is not None:     # 等手機按接受時，直接斷線才停得下來
            try:
                conn.sock and conn.sock.shutdown(2)
            except OSError:
                pass
        return True

    def _set(self, **kw):
        with self.lock:
            self._mine.update(kw)

    def _check_cancel(self):
        if self._mine.get("cancel"):
            raise Cancelled()

    # ---- 準備清單
    def items_from_library(self, ids):
        out = []
        for r in self.store.rows([int(i) for i in ids]):
            if r.get("removed"):
                continue
            p = self.store.abspath(r["path"])
            out.append({"path": p, "name": r.get("name") or p.name, "size": r.get("size"),
                        "sha256": r.get("sha256"), "lib": True, "taken": r.get("received_at")})
        return out

    def items_from_stage(self, ids):
        return [{"path": v["path"], "name": v["name"], "size": v["size"], "sha256": None, "lib": False,
                 "temp": v["temp"], "stage_id": k} for k, v in self.stage.take(ids)]

    def start(self, peer_fp, items, label=""):
        peer = PEERS.get(peer_fp)
        if not peer:
            raise ValueError(_t("send.no_peer"))
        if not items:
            raise ValueError(_t("send.nothing"))
        dev = self.devices_fn()[0]
        with self.lock:
            if self.job and self.job["state"] in ("checking", "waiting", "sending"):
                raise ValueError(_t("send.busy"))
            self.job = {"id": uuid.uuid4().hex[:8], "state": "checking", "peer": peer["alias"],
                        "label": label, "count": len(items), "total": sum(int(i.get("size") or 0) for i in items),
                        "sent": 0, "done": 0, "current": "", "skipped": [], "failed": [],
                        "message": _t("send.checking"), "started": time.time(), "items": items, "cancel": False}
        job = self.job
        threading.Thread(target=self._thread, args=(job, peer, dev, items), daemon=True).start()
        return self.status()

    # ---- 執行
    def _verify(self, items):
        ok = []
        for it in items:
            self._check_cancel()
            p = it["path"]
            self._set(current=it["name"])
            try:
                if not p.is_file():
                    raise ValueError(_t("send.missing"))
                if it["lib"] and not inside(p, self.cfg.data):
                    raise ValueError(_t("send.not_in_data"))
                size = p.stat().st_size
                if it["lib"] and it.get("size") is not None and int(it["size"]) != size:
                    raise ValueError(_t("send.size_changed"))
                if it["lib"] and it.get("sha256"):
                    if self.sha_fn(p).lower() != str(it["sha256"]).lower():
                        raise ValueError(_t("send.sha_changed"))
                it["size"] = size
                it["mtime"] = p.stat().st_mtime
                ok.append(it)
            except Cancelled:
                raise
            except Exception as e:  # noqa
                self._mine["skipped"].append({"name": it["name"], "why": str(e)})
        return ok

    def _thread(self, job, *a):
        self._tl.job = job
        self._run(*a)

    def _run(self, peer, dev, items):
        sid = None
        final = None
        sent_ok = []    # 確實傳成功的待傳項目；只清掉這些，其他留著可以再傳
        try:
            items = self._verify(items)
            if not items:
                raise ValueError(_t("send.none_ok"))
            self._set(count=len(items), total=sum(i["size"] for i in items), current="")
            files, byid = {}, {}
            for it in items:
                fid = secrets.token_hex(8)
                mime = mimetypes.guess_type(it["name"])[0] or "application/octet-stream"
                f = {"id": fid, "fileName": clean_name(it["name"]), "size": it["size"], "fileType": mime}
                if it.get("sha256"):
                    f["sha256"] = str(it["sha256"]).lower()
                md = {k: v for k, v in (("modified", _iso(it["mtime"])), ("accessed", _iso(it["mtime"]))) if v}
                if md:
                    f["metadata"] = md
                files[fid] = f
                byid[fid] = it
            self._set(state="waiting", message=_t("send.waiting", peer=peer["alias"]))
            self._check_cancel()
            conn = _connect(peer, dev, ACCEPT_TIMEOUT)
            with self.lock:
                self._conn = conn
            try:
                body = json.dumps({"info": dev.info(), "files": files}, ensure_ascii=False).encode()
                conn.request("POST", "/api/localsend/v2/prepare-upload", body=body,
                             headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
                r = conn.getresponse()
                data = r.read(1024 * 1024)
            except (OSError, http.client.HTTPException) as e:
                if self._mine.get("cancel"):
                    raise Cancelled()
                if isinstance(e, TimeoutError) or "timed out" in str(e):
                    raise ValueError(_t("send.no_answer"))
                raise ValueError(_t("send.cant_connect", err=e))
            finally:
                with self.lock:
                    self._conn = None
                conn.close()
            self._check_cancel()
            msg = {204: None, 401: _t("send.pin"), 403: _t("send.rejected"), 409: _t("send.phone_busy"),
                   429: _t("send.too_many_requests")}
            if r.status == 204:
                final = ("done", _t("send.not_needed"))
                return
            if r.status != 200:
                raise ValueError(msg.get(r.status) or _t("send.phone_error", code=r.status, msg=""))
            resp = json.loads(data or b"{}")
            sid = resp.get("sessionId")
            tokens = resp.get("files") or {}
            want = [fid for fid in files if fid in tokens]
            if not sid or not want:
                final = ("done", _t("send.none_taken"))
                sid = None
                return
            not_taken = len(files) - len(want)
            self._set(state="sending", count=len(want), total=sum(byid[f]["size"] for f in want),
                      message=_t("send.sending"))
            sent_before = 0
            for fid in want:
                self._check_cancel()
                it = byid[fid]
                self._set(current=it["name"])
                try:
                    self._upload(peer, dev, sid, fid, tokens[fid], it, sent_before)
                    with self.lock:
                        self._mine["done"] += 1
                        if it.get("stage_id"):
                            sent_ok.append(it["stage_id"])
                except Cancelled:
                    raise
                except Exception as e:  # noqa
                    log.warning(_t("log.send_failed", name=it["name"], err=e))
                    with self.lock:
                        self._mine["failed"].append({"name": it["name"], "why": str(e)})
                    ended = isinstance(e, PhoneEnded)
                else:
                    ended = False
                sent_before += it["size"]
                self._set(sent=sent_before)
                if ended:
                    break
            j = self._mine
            extra = _t("send.partial", n=len(want)) if not_taken else ""
            if j["failed"]:
                final = ("error", _t("send.some_failed", done=j["done"], n=len(j["failed"]), extra=extra))
            else:
                final = ("done", _t("send.done", n=j["done"], peer=peer["alias"], extra=extra))
                sid = None     # 全部成功：手機那邊這次工作已自然結束，不用通知取消
        except Cancelled:
            final = ("canceled", _t("send.canceled"))
        except Exception as e:  # noqa
            log.warning(_t("log.send_job_failed", err=e))
            final = ("error", str(e))
        finally:
            if sid:   # 取消或中途失敗：通知手機結束這次工作
                try:
                    c = _connect(peer, dev, 5)
                    c.request("POST", "/api/localsend/v2/cancel?" + urlencode({"sessionId": sid}), body=b"",
                              headers={"Content-Length": "0"})
                    c.getresponse().read()
                    c.close()
                except Exception:
                    pass
            # 只把確實傳成功的項目從待傳清單拿掉（拖曳暫存檔也一起清掉）；沒被接受、略過、失敗、取消的都留著
            if sent_ok:
                self.stage.remove(sent_ok)
            # 最後一次發布結果：畫面看到結果時，手機已收到取消、待傳清單也已更新
            state, msg = final or ("error", _t("send.aborted"))
            self._set(state=state, message=msg, current="", finished=time.time())

    def _upload(self, peer, dev, sid, fid, token, it, base):
        conn = _connect(peer, dev, IO_TIMEOUT)
        with self.lock:
            self._conn = conn
        try:
            q = urlencode({"sessionId": sid, "fileId": fid, "token": token})
            conn.putrequest("POST", "/api/localsend/v2/upload?" + q, skip_accept_encoding=True)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(it["size"]))
            conn.endheaders()
            n = 0
            with open(it["path"], "rb") as f:     # 讀完立即關檔（Windows 鎖檔）
                while True:
                    if self._mine.get("cancel"):
                        raise Cancelled()
                    b = f.read(CHUNK)
                    if not b:
                        break
                    conn.send(b)
                    n += len(b)
                    with self.lock:
                        self._mine["sent"] = base + n
            if n != it["size"]:
                raise ValueError(_t("send.changed_while_reading"))
            r = conn.getresponse()
            body = r.read(65536)
            if r.status == 403:
                raise PhoneEnded(_t("send.phone_ended"))
            if r.status != 200:
                try:
                    m = json.loads(body).get("message")
                except Exception:
                    m = ""
                raise ValueError(_t("send.phone_error", code=r.status, msg=m or ""))
        except (OSError, http.client.HTTPException) as e:
            if self._mine.get("cancel"):
                raise Cancelled()
            log.info(_t("log.upload_cut", err=e))     # 技術細節只寫進紀錄，畫面顯示看得懂的說明
            if isinstance(e, TimeoutError) or "timed out" in str(e):
                raise ValueError(_t("send.cut_timeout"))
            raise ValueError(_t("send.cut_closed"))
        finally:
            with self.lock:
                self._conn = None
            conn.close()
