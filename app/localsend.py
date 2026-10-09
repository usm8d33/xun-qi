"""內建的 LocalSend 協定 v2 接收端（取代電腦版 LocalSend）。

- UDP 多播 224.0.0.167:53317 宣告自己、回應手機的宣告
- HTTPS 伺服器：/register、/info、/prepare-upload、/upload、/cancel
- 可同時開兩台「虛擬裝置」（不同名稱、不同憑證、不同埠），手機傳給哪台就決定模式
"""
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .safefs import TMP_PREFIX, claim_name, clean_name, unique_path
from .sender import PEERS

log = logging.getLogger("localsend")

MCAST_GRP = "224.0.0.167"
MCAST_PORT = 53317
PROTOCOL_VERSION = "2.2"
CHUNK = 1024 * 1024


class IncompleteBody(Exception):
    """傳輸中斷：收到的內容比宣告的少，或 chunked 格式不完整。"""


class Device:
    def __init__(self, alias, port, cert, key, fingerprint, inbox_for, mode_for):
        self.alias = alias
        self.port = port
        self.cert = str(cert)
        self.key = str(key)
        self.fingerprint = fingerprint
        self.inbox_for = inbox_for      # () -> Path，收檔資料夾
        self.mode_for = mode_for        # () -> "plain" | "sort"
        self.sessions = {}              # sessionId -> dict
        self.lock = threading.Lock()

    def info(self, announce=None, with_port=True):
        d = {
            "alias": self.alias,
            "version": PROTOCOL_VERSION,
            "deviceModel": "Windows",
            "deviceType": "desktop",
            "fingerprint": self.fingerprint,
            "download": False,
        }
        if with_port:
            d["port"] = self.port
            d["protocol"] = "https"
        if announce is not None:
            d["announce"] = announce
        return d


# ---------------------------------------------------------------- HTTP 伺服器
def make_handler(dev: Device, on_file, on_event):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "XunQi"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

        # ---- 小工具 ----
        def _json(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _read_all(self, limit=10 * 1024 * 1024):
            try:
                data = b"".join(self._iter_body())
            except IncompleteBody:
                data = b""
            return data[:limit]

        def _iter_body(self):
            """支援 Content-Length 與 chunked 兩種上傳方式。內容不完整時丟 IncompleteBody，不會默默結束。"""
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                while True:
                    line = self.rfile.readline()
                    if not line.endswith(b"\n"):
                        raise IncompleteBody("chunked 長度列不完整")
                    try:
                        size = int(line.strip().split(b";")[0], 16)
                    except ValueError:
                        raise IncompleteBody("chunked 長度格式錯誤") from None
                    if size == 0:
                        while True:              # trailer 直到空白行
                            t = self.rfile.readline()
                            if not t:
                                raise IncompleteBody("缺少 chunked 結束標記")
                            if t in (b"\r\n", b"\n"):
                                return
                    left = size
                    while left:
                        b = self.rfile.read(min(CHUNK, left))
                        if not b:
                            raise IncompleteBody("chunk 內容不完整")
                        left -= len(b)
                        yield b
                    if self.rfile.readline() not in (b"\r\n", b"\n"):
                        raise IncompleteBody("chunk 結尾不完整")
            else:
                left = int(self.headers.get("Content-Length") or 0)
                while left > 0:
                    b = self.rfile.read(min(CHUNK, left))
                    if not b:
                        raise IncompleteBody(f"還差 {left} 位元組")
                    left -= len(b)
                    yield b

        # ---- 路由 ----
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/api/localsend/v2/info", "/api/localsend/v1/info"):
                return self._json(200, dev.info(with_port=False))
            self._json(404, {"message": "Not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/localsend/v2/register":
                    try:   # 手機回應我們的宣告時會來登記：記下來，「傳到手機」才看得到它
                        PEERS.seen(json.loads(self._read_all(64 * 1024) or b"{}"), self.client_address[0])
                    except Exception:
                        pass
                    return self._json(200, dev.info(with_port=False))
                if u.path == "/api/localsend/v2/prepare-upload":
                    return self._prepare()
                if u.path == "/api/localsend/v2/upload":
                    return self._upload(q)
                if u.path == "/api/localsend/v2/cancel":
                    self._read_all()
                    with dev.lock:
                        dev.sessions.pop(q.get("sessionId"), None)
                    return self._json(200)
                self._read_all()
                self._json(404, {"message": "Not found"})
            except Exception as e:  # noqa
                log.exception("處理請求失敗")
                try:
                    self._json(500, {"message": str(e)})
                except Exception:
                    pass

        def _prepare(self):
            try:
                req = json.loads(self._read_all() or b"{}")
                files = req["files"]
            except Exception:
                return self._json(400, {"message": "Invalid body"})
            if not files:
                return self._json(204)
            sid = secrets.token_hex(16)
            batch = time.strftime("%Y%m%d-%H%M%S") + "-" + sid[:6]  # 每次傳送＝一批
            tokens, meta = {}, {}
            for fid, f in files.items():
                tokens[fid] = secrets.token_hex(16)
                meta[fid] = f
            sender = (req.get("info") or {}).get("alias", "手機")
            with dev.lock:
                # 清掉 1 小時以上沒動靜的舊工作階段
                now = time.time()
                for k in [k for k, s in dev.sessions.items() if now - s["t"] > 3600]:
                    dev.sessions.pop(k, None)
                dev.sessions[sid] = {"ip": self.client_address[0], "tokens": tokens, "meta": meta,
                                     "done": set(), "active": set(), "t": now, "mode": dev.mode_for(),
                                     "batch": batch, "sender": sender}
            on_event({"type": "session", "device": dev.alias, "sender": sender, "count": len(files),
                      "mode": dev.mode_for(), "batch": batch,
                      "files": [{"fid": k, "name": clean_name(v.get("fileName") or k),
                                 "size": v.get("size") if isinstance(v.get("size"), int) else None}
                                for k, v in files.items()]})
            log.info("%s 要傳 %d 個檔案到「%s」", sender, len(files), dev.alias)
            self._json(200, {"sessionId": sid, "files": tokens})

        def _upload(self, q):
            sid, fid, tok = q.get("sessionId"), q.get("fileId"), q.get("token")
            with dev.lock:
                s = dev.sessions.get(sid)
            if not (sid and fid and tok):
                self._read_all()
                return self._json(400, {"message": "Missing parameters"})
            if not s or s["tokens"].get(fid) != tok or s["ip"] != self.client_address[0]:
                self._read_all()
                return self._json(403, {"message": "Invalid token or IP address"})
            f = s["meta"][fid]
            with dev.lock:
                if fid in s["done"] or fid in s["active"]:   # 同一個檔案不能同時（或重複）上傳
                    busy = True
                else:
                    s["active"].add(fid)
                    busy = False
            if busy:
                self._read_all()
                return self._json(409, {"message": "File already uploading or received"})
            try:
                self._receive(s, sid, fid, f)
            finally:
                with dev.lock:
                    s["active"].discard(fid)

        def _receive(self, s, sid, fid, f):
            folder: Path = dev.inbox_for(s["mode"])
            folder.mkdir(parents=True, exist_ok=True)
            name = clean_name(f.get("fileName") or fid)
            # 暫存檔用隨機名稱、排他建立：不會和別的上傳或使用者原有的 xxx.part 撞名
            part = folder / f"{TMP_PREFIX}{secrets.token_hex(8)}.part"
            h = hashlib.sha256()
            size = 0
            fail = None
            try:
                with open(part, "xb") as out:
                    for b in self._iter_body():
                        out.write(b)
                        h.update(b)
                        size += len(b)
            except IncompleteBody as e:
                fail = f"傳輸中斷（{e}）"
            except OSError as e:
                fail = f"寫入失敗（{e}）"
            digest = h.hexdigest()
            want = (f.get("sha256") or "").lower()
            declared = f.get("size")
            if not fail and isinstance(declared, int) and declared >= 0 and declared != size:
                fail = f"大小不符：應為 {declared}，收到 {size}"
            if not fail and want and want != digest:
                fail = "SHA-256 不符"
            if fail:
                part.unlink(missing_ok=True)  # 只刪除我們自己剛寫的暫存檔
                log.warning("%s 沒有收好：%s", name, fail)
                on_event({"type": "upload_failed", "batch": s["batch"], "fid": fid, "name": name, "error": fail})
                return self._json(422 if "不符" in fail else 400, {"message": fail})
            final = claim_name(part, folder, name)  # 不覆蓋、可處理同名競爭
            # 先確實寫進收件紀錄，才算收到、才回覆手機成功
            try:
                on_file(s["mode"], final, {"sha256": digest, "size": size, "fileType": f.get("fileType"),
                                           "modified": (f.get("metadata") or {}).get("modified"),
                                           "batch": s["batch"], "fid": fid, "declared": declared})
            except Exception as e:  # noqa  紀錄沒寫進去：這次不算收到，讓手機重傳
                log.exception("登記 %s 失敗，請手機重傳", final.name)
                try:   # 只收回我們剛寫、還沒回覆成功的檔案，避免重傳後多一份
                    back = folder / f"{TMP_PREFIX}{secrets.token_hex(8)}.part"
                    os.rename(final, back)
                    back.unlink(missing_ok=True)
                except OSError:
                    log.warning("無法收回 %s，下次可能多一份同名檔", final.name)
                return self._json(500, {"message": f"電腦端紀錄寫入失敗，請重傳（{e}）"})
            with dev.lock:
                s["done"].add(fid)
                s["t"] = time.time()
                finished = len(s["done"]) == len(s["meta"])
                if finished:
                    dev.sessions.pop(sid, None)
            log.info("收到 %s（%.1f MB）", final.name, size / 1e6)
            if finished:
                on_event({"type": "session_done", "device": dev.alias, "count": len(s["meta"])})
            try:
                self._json(200)
            except (ConnectionError, OSError):
                log.warning("%s 已收到，但回覆手機時連線中斷", final.name)

    return Handler


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上開 reuse 會讓兩個程式搶同一個埠，所以只在 Linux/mac 開
    allow_reuse_address = not sys.platform.startswith("win")

    def __init__(self, addr, handler, dev: Device):
        super().__init__(addr, handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(dev.cert, dev.key)
        ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx

    def get_request(self):
        sock, addr = self.socket.accept()
        # 握手延到處理執行緒中進行，避免一個慢連線卡住其他連線
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        # 手機中斷連線等情況很常見，不要在視窗印出一大串錯誤
        log.debug("連線中斷 %s: %s", client_address, sys.exc_info()[1])

    def finish_request(self, request, client_address):
        try:
            request.settimeout(60)
            request.do_handshake()
        except Exception as e:  # noqa
            log.debug("TLS 握手失敗 %s: %s", client_address, e)
            return
        request.settimeout(None)
        super().finish_request(request, client_address)


# ---------------------------------------------------------------- 探索（多播）
def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:  # 找出預設路由那張網卡
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith("127.")] or ["0.0.0.0"]


class Discovery(threading.Thread):
    def __init__(self, devices):
        super().__init__(daemon=True)
        self.devices = devices
        self.own = {d.fingerprint for d in devices}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", MCAST_PORT))
        self.ifaces = local_ipv4s()
        for ip in self.ifaces:
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GRP), socket.inet_aton(ip))
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as e:
                log.debug("加入多播群組失敗 %s: %s", ip, e)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.peers = {}  # fingerprint -> (alias, ip, last_seen)

    def _send(self, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode()
        for ip in self.ifaces:
            try:
                if ip != "0.0.0.0":
                    self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                self.sock.sendto(data, (MCAST_GRP, MCAST_PORT))
            except OSError as e:
                log.debug("多播送出失敗 %s: %s", ip, e)

    def announce(self):
        for d in self.devices:
            self._send(d.info(announce=True))

    def _register_to(self, ip, port, proto, dev: Device):
        """用 HTTPS（附上本裝置憑證，讓手機驗證指紋）向手機登記自己。"""
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(dev.cert, dev.key)
            url = f"{proto}://{ip}:{port}/api/localsend/v2/register"
            req = urllib.request.Request(url, data=json.dumps(dev.info()).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            handlers = [urllib.request.ProxyHandler({})]  # 不走系統 proxy，直接連手機
            if proto == "https":
                handlers.append(urllib.request.HTTPSHandler(context=ctx))
            urllib.request.build_opener(*handlers).open(req, timeout=4).read()
        except Exception as e:  # noqa
            log.debug("向 %s 登記失敗：%s", ip, e)
        # 備援：也用 UDP 回應
        self._send(dev.info(announce=False))

    def run(self):
        self.announce()
        last = time.time()
        self.sock.settimeout(1.0)
        while True:
            if time.time() - last > 30:
                self.announce()
                last = time.time()
            try:
                data, (ip, _) = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                msg = json.loads(data)
            except Exception:
                continue
            fp = msg.get("fingerprint")
            if not fp or fp in self.own:
                continue
            self.peers[fp] = (msg.get("alias"), ip, time.time())
            PEERS.seen(msg, ip)
            if msg.get("announce") or msg.get("announcement"):
                port = msg.get("port", MCAST_PORT)
                proto = msg.get("protocol", "https")
                for d in self.devices:
                    threading.Thread(target=self._register_to, args=(ip, port, proto, d), daemon=True).start()


def start(devices, on_file, on_event):
    PEERS.own |= {d.fingerprint for d in devices}
    servers = []
    for d in devices:
        srv = TLSServer(("0.0.0.0", d.port), make_handler(d, on_file, on_event), d)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        log.info("裝置「%s」已在埠 %d 待命", d.alias, d.port)
    disc = Discovery(devices)
    disc.start()
    return servers, disc
