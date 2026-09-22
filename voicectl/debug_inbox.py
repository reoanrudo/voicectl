"""動作確認用：この PC の中（127.0.0.1）からだけ、文字で命令を入れられる小さな HTTP 窓口。

POST http://127.0.0.1:8766/say  本文＝命令の文（UTF-8）→ 音声認識の結果と同じように処理する。
GET  http://127.0.0.1:8766/status → 直近の「言ったこと・したこと・結果」
config の debug.inbox が true のときだけ起動する。外部からは接続できない。
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def start(controller, port: int = 8766) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/say":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            text = self.rfile.read(n).decode("utf-8").strip()
            if not text:
                return self._send(400, {"error": "empty"})
            controller.say(text)
            self._send(200, {"queued": text})

        def do_GET(self):
            if self.path != "/status":
                return self._send(404, {"error": "not found"})
            self._send(200, {"history": list(controller._history),
                             "dictating": getattr(controller, "_dictating", False),
                             "recording": getattr(getattr(controller, "demo", None), "active", False)})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="debug-inbox").start()
    log.info("動作確認用の窓口: http://127.0.0.1:%d/say", port)
