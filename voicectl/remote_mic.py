"""ブラウザをマイクにする：Mac や iPhone のブラウザで開いたページから、押している間の声を PC に送る。

リモートデスクトップのマイク転送に頼らない（Windows 11 Home ではマイク転送が動かないことがあるため）。
- getUserMedia は HTTPS でないと使えないため、自己署名証明書で HTTPS を提供する
- 同じ LAN の他人に操作されないよう、URL にランダムなトークンを付ける
- 音声は 16 kHz・16bit・モノラルに変換してから WebSocket で送る
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import threading
from pathlib import Path

import numpy as np

from .config import ROOT

log = logging.getLogger(__name__)
CACHE = ROOT / "cache"
PAGE = Path(__file__).resolve().parent / "web" / "mic.html"


class RemoteSource:
    """コントローラから見るとマイクと同じ（start/stop/peek/level）。中身はブラウザから届いた音声。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._recording = False
        self.level = 0.0

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._recording = True

    def feed(self, pcm16: bytes) -> None:
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768
        self.level = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        with self._lock:
            if self._recording:
                self._chunks.append(x)

    def peek(self) -> np.ndarray:
        with self._lock:
            return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)

    def take(self) -> np.ndarray:
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def trim(self, keep_sec: float) -> None:
        with self._lock:
            keep, total = [], 0
            for c in reversed(self._chunks):
                keep.append(c)
                total += len(c)
                if total >= keep_sec * 16000:
                    break
            self._chunks = list(reversed(keep))

    def alive(self) -> bool:
        return True

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # 既定の経路のアドレス
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def _token() -> str:
    p = CACHE / "remote_mic_token.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    CACHE.mkdir(exist_ok=True)
    t = secrets.token_urlsafe(18)
    p.write_text(t, encoding="utf-8")
    return t


def _certificate(ips: list[str]) -> tuple[Path, Path]:
    """自己署名証明書を作る（アドレスが変わったら作り直す）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_p, key_p, ips_p = CACHE / "remote_mic_cert.pem", CACHE / "remote_mic_key.pem", CACHE / "remote_mic_ips.txt"
    names = sorted(set(ips) | {"127.0.0.1"})
    if cert_p.exists() and key_p.exists() and ips_p.exists() and ips_p.read_text() == ",".join(names):
        return cert_p, key_p
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"voicectl ({host})")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.DNSName(host), x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in names]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    CACHE.mkdir(exist_ok=True)
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ips_p.write_text(",".join(names))
    return cert_p, key_p


class RemoteMicServer:
    def __init__(self, controller, port: int = 8765):
        self.controller = controller
        self.port = port
        self.source = RemoteSource()
        self.token = _token()
        self.ips = local_ips()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set = set()
        self._active_ws = None  # 今しゃべっているクライアント
        self._ready_ws = None   # マイクを有効にした最新のクライアント（F5 で録音を指示する相手）
        self._draining_ws = None  # F5 を離した直後、まだ届く音声を受け取る相手

    @property
    def urls(self) -> list[str]:
        return [f"https://{ip}:{self.port}/?t={self.token}" for ip in self.ips]

    def start(self) -> None:
        threading.Thread(target=self._run, name="remote-mic", daemon=True).start()

    def _run(self) -> None:
        from aiohttp import web
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        cert, key = _certificate(self.ips)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(cert, key)
        app = web.Application()
        app.router.add_get("/", self._page)
        app.router.add_get("/ws", self._ws)
        runner = web.AppRunner(app, access_log=None)
        self._loop.run_until_complete(runner.setup())
        self._loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", self.port, ssl_context=ctx).start())
        log.info("ブラウザマイク: %s", " / ".join(self.urls))
        self._loop.run_forever()

    def _authorized(self, request) -> bool:
        return secrets.compare_digest(request.query.get("t", ""), self.token)

    async def _page(self, request):
        from aiohttp import web
        ok = self._authorized(request)
        log.info("ブラウザマイクのページ要求: %s（%s）%s", request.remote, "合言葉OK" if ok else "合言葉なし／違い",
                 request.headers.get("User-Agent", "")[:80])
        if not ok:
            return web.Response(status=403, text="403 Forbidden")
        return web.Response(text=PAGE.read_text(encoding="utf-8"), content_type="text/html", charset="utf-8")

    async def _ws(self, request):
        from aiohttp import WSMsgType, web
        if not self._authorized(request):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("ブラウザマイク接続: %s", request.remote)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if ws is self._active_ws or ws is self._draining_ws:
                        self.source.feed(msg.data)  # 録音が止まった後の音声は RemoteSource 側で捨てられる
                elif msg.type == WSMsgType.TEXT:
                    cmd = json.loads(msg.data).get("cmd")
                    if cmd == "ready":
                        self._ready_ws = ws
                        log.info("ブラウザマイク準備完了: %s（F5 でこの端末のマイクを使います）", request.remote)
                    elif cmd == "down" and self._active_ws is None:
                        self._active_ws = ws
                        self.controller.ptt_down(self.source)
                    elif cmd == "up" and ws is self._active_ws:
                        self._active_ws = None
                        self.controller.ptt_up(from_browser=True)
                    elif cmd == "stop":
                        self.controller.emergency_stop()
        finally:
            if ws is self._active_ws:  # 話している途中で切断されたら録音を終える
                self._active_ws = None
                self.controller.ptt_up(from_browser=True)
            if ws is self._ready_ws:
                self._ready_ws = None
            self._clients.discard(ws)
            log.info("ブラウザマイク切断: %s", request.remote)
        return ws

    # ---- F5（PC のキー）でブラウザのマイクを使う ----
    def ready(self) -> bool:
        return self._ready_ws is not None and not self._ready_ws.closed

    def remote_down(self) -> bool:
        """準備済みのブラウザに録音開始を指示する。指示できたら True。"""
        ws = self._ready_ws
        if ws is None or ws.closed or self._active_ws is not None or not self._loop:
            return False
        self._active_ws, self._draining_ws = ws, None
        asyncio.run_coroutine_threadsafe(ws.send_str(json.dumps({"kind": "ptt", "args": [True]})), self._loop)
        return True

    def remote_up(self) -> None:
        ws, self._active_ws = self._active_ws, None
        self._draining_ws = ws
        if ws is not None and not ws.closed and self._loop:
            asyncio.run_coroutine_threadsafe(ws.send_str(json.dumps({"kind": "ptt", "args": [False]})), self._loop)

    # コントローラ側のスレッドから呼ばれる：状態や文字起こしをページに送る
    def broadcast(self, kind: str, *args) -> None:
        if not self._loop or not self._clients:
            return
        data = json.dumps({"kind": kind, "args": list(args)}, ensure_ascii=False)
        for ws in list(self._clients):
            asyncio.run_coroutine_threadsafe(ws.send_str(data), self._loop)


class UiTee:
    """UiBridge（Qt）への通知を、ブラウザにも同時に流す。"""

    _FORWARD = ("status", "transcript_partial", "transcript_final", "transcript_result")

    def __init__(self, bridge, server: RemoteMicServer):
        for name in ("status", "level", "hints_labels", "hints_grid", "hints_clear",
                     "transcript_partial", "transcript_final", "transcript_result", "idle", "answer", "answer_clear",
                     "copy_screen"):
            if hasattr(bridge, name):
                setattr(self, name, _TeeSignal(getattr(bridge, name), server if name in self._FORWARD else None, name))


class _TeeSignal:
    def __init__(self, sig, server, name):
        self.sig, self.server, self.name = sig, server, name

    def emit(self, *args):
        self.sig.emit(*args)
        if self.server:
            try:
                self.server.broadcast(self.name, *args)
            except Exception:
                log.exception("ブラウザへの通知に失敗")
