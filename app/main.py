"""Xun-Qi 尋棲 主程式（相容 LocalSend 協定的接收與整理程式）。由 start.bat 啟動。"""
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import backup, config, localsend, sender, tls, webui  # noqa: E402
from app.processor import Processor, sha256_of  # noqa: E402
from app.store import Store  # noqa: E402


class App:
    def __init__(self):
        self.cfg = config.load()
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            datefmt="%H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout),
                      logging.FileHandler(self.cfg.state_dir / "log.txt", encoding="utf-8")],
        )
        for noisy in ("sentence_transformers", "transformers", "httpx", "huggingface_hub"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self.store = Store(self.cfg.state_dir / "index.db", self.cfg.data, self.cfg)
        if self.cfg.edition == "lite":
            from app.embedder_onnx import OnnxEmbedder
            self.emb = OnnxEmbedder(self.cfg)
        else:
            from app.embedder import Embedder
            self.emb = Embedder(self.cfg)
        self.proc = Processor(self.cfg, self.store, self.emb)
        self.web_mode = "sort"
        self.pending_location = None
        self.devices = []
        self.started = time.time()
        self.disc = None
        self.scanning = False
        self.stage = sender.Stage(self.cfg)            # 傳到手機：電腦上的檔案的待傳清單
        self.sender = sender.Sender(self.cfg, self.store, lambda: self.devices, self.stage, sha256_of)

    def proc_discover(self, scan=False):
        """重新搜尋手機：送出多播宣告（手機收到會回來登記）；scan=True 再掃描整個網段。"""
        if self.disc is not None:
            self.disc.announce()
        if scan and not self.scanning and self.devices:
            def run():
                self.scanning = True
                try:
                    sender.scan(self.devices[0], localsend.local_ipv4s())
                finally:
                    self.scanning = False
            threading.Thread(target=run, daemon=True).start()

    def inbox_for(self, mode):
        return self.cfg.inbox_plain if mode == "plain" else self.cfg.inbox_sort

    def status(self):
        return {
            "mode_select": self.cfg.mode_select,
            "web_mode": self.web_mode,
            "devices": [{"alias": d.alias, "port": d.port} for d in self.devices],
            "model": self.emb.state,
            "edition": self.cfg.edition_name,
            "pending": self.proc.pending,
            "busy": self.proc.busy,
            "events": list(self.proc.events)[:40],
            "errors": list(self.proc.errors)[:10],
            "counts": self.store.counts(),
            "removed": len(self.proc.removed_list()),
            "batch_pending": dict(self.proc.batch_pending),
            "data_dir": str(self.cfg.data),
            "onedrive": self.cfg.synced_by_onedrive,
            "pending_location": self.pending_location,
            "categories": list(self.cfg.categories) + [self.cfg.unsorted_category],
            "emojis": dict(self.cfg.get("emojis") or {}),
            "geo_left": self.proc.geo_left,
            "enrich": self.proc.enrich_status(),
            "restore_pending": backup.pending(self.cfg),
            "restored": bool(self.cfg.get("restored")),
            "user_name": str(self.cfg.get("user_name") or os.environ.get("USERNAME") or os.environ.get("USER") or ""),
        }

    def run(self):
        cfg = self.cfg
        if cfg.mode_select == "web":
            c, k, fp = tls.ensure_cert(cfg.state_dir, "device_main")
            self.devices = [localsend.Device(cfg.alias_plain.split("-")[0], cfg.port_plain, c, k, fp,
                                             self.inbox_for, lambda: self.web_mode)]
        else:
            c1, k1, f1 = tls.ensure_cert(cfg.state_dir, "device_plain")
            c2, k2, f2 = tls.ensure_cert(cfg.state_dir, "device_sort")
            self.devices = [
                localsend.Device(cfg.alias_plain, cfg.port_plain, c1, k1, f1, self.inbox_for, lambda: "plain"),
                localsend.Device(cfg.alias_sort, cfg.port_sort, c2, k2, f2, self.inbox_for, lambda: "sort"),
            ]
        try:
            _, self.disc = localsend.start(self.devices, self.proc.on_file, self.proc.event)
        except OSError as e:
            print("\n[錯誤] 無法開啟 LocalSend 接收埠：", e)
            print("  多半是電腦版 LocalSend 還開著（佔用 53317 埠）。請把它完全關閉（包含右下角圖示）後再重開本程式。\n")
            input("按 Enter 結束…")
            return
        try:
            webui.start(self)
        except OSError:
            print(f"\n[錯誤] 網頁埠 {cfg.web_port} 被佔用，請在 config.yaml 改 web_port。")
            input("按 Enter 結束…")
            return
        self.proc.requeue_leftovers()
        self.proc.removed_list()  # 你已自己刪掉的「待刪除」檔案，從索引移除
        self.proc.clean_export_temp()
        self.stage.clean_all()    # 上次拖進網頁、還沒傳的暫存檔
        self.proc.backfill()      # 背景補讀舊檔案的拍攝地點
        self.proc.start_enrich()  # 背景補做 OCR 文字和影片片段（有新檔案要分類時會先讓路）

        url = f"http://127.0.0.1:{cfg.web_port}"
        lan = ", ".join(localsend.local_ipv4s())
        print("=" * 60)
        print(f"  Xun-Qi 尋棲（{cfg.edition_name}）已啟動")
        print("  請把下面這個網址複製到瀏覽器開啟操作介面：")
        print(f"\n      {url}\n")
        print(f"  手機 LocalSend 會看到：{'、'.join(d.alias for d in self.devices)}")
        print(f"  電腦區網 IP：{lan}")
        print(f"  檔案存放位置：{cfg.data}")
        if cfg.synced_by_onedrive:
            print("  ［提醒］這個位置在 OneDrive 裡，收到的照片會被同步上傳到雲端。")
            print("          不想上傳的話，可在網頁左下「⚙ 存放位置」換到其他資料夾。")
        ocr_state = self.proc.ocr.state
        print(f"  OCR（照片文字）：{ocr_state}" + ("　→ 把 det／rec 兩個 .onnx 放到 models\\ocr\\" if ocr_state == "沒有模型" else ""))
        if cfg.get("restored"):
            print("  ［還原］已換上你選的索引備份。")
        print("  關閉這個視窗即結束程式。")
        print("=" * 60)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        a = App()
    except RuntimeError as e:
        print("\n[錯誤]", e)
        input("按 Enter 結束…")
        sys.exit(1)
    a.run()
