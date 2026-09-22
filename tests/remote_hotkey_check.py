"""F5 でブラウザのマイクを使う流れの確認（手動確認用）。起動中のアプリとは別のポートで動かす。

ブラウザ役のクライアントが「ready」を送り、F5 相当（controller.ptt_down/up）で録音指示を受けて WAV を送る。
実際の操作は行わない。
"""
import asyncio
import json
import logging
import ssl
import sys
import threading
import time
import wave
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.audio import resample  # noqa: E402
from voicectl.config import load  # noqa: E402
from voicectl.controller import Controller  # noqa: E402
from voicectl.remote_mic import RemoteMicServer, UiTee  # noqa: E402
from voicectl.stt import SpeechRecognizer  # noqa: E402

logging.basicConfig(level=logging.WARNING)
PORT = 8799


class Sig:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink

    def emit(self, *a):
        self.sink.append((time.perf_counter(), self.name, a))


class Bridge:
    def __init__(self):
        self.events = []
        for n in ("status", "level", "hints_labels", "hints_grid", "hints_clear",
                  "transcript_partial", "transcript_final", "transcript_result"):
            setattr(self, n, Sig(n, self.events))


cfg = load()
bridge = Bridge()
server = RemoteMicServer(None, PORT)
ctl = Controller(cfg, UiTee(bridge, server), None, SpeechRecognizer(), None, AppCatalog(cfg.get("app_aliases")))
ctl.executor.run = lambda d, lookup: None
server.controller = ctl
ctl.remote = server
ctl.start()
server.start()
time.sleep(1.5)

with wave.open(str(Path(__file__).parent / "audio" / "02_right.wav")) as w:
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    pcm = (np.clip(resample(x, w.getframerate(), 16000), -1, 1) * 32767).astype(np.int16)


async def browser():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"wss://127.0.0.1:{PORT}/ws?t={server.token}", ssl=ctx) as ws:
            await ws.send_str(json.dumps({"cmd": "ready"}))
            sending = False
            i = 0
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.064)
                    m = json.loads(msg.data)
                    if m["kind"] == "ptt":
                        sending = m["args"][0]
                        print("ブラウザ: 録音指示", "開始" if sending else "終了")
                    if m["kind"] == "transcript_result":
                        print("ブラウザ: 結果", m["args"][0])
                        return
                except asyncio.TimeoutError:
                    pass
                if sending and i < len(pcm):  # 実時間相当で送る
                    await ws.send_bytes(pcm[i:i + 1024].tobytes())
                    i += 1024


def press_f5():
    time.sleep(1.0)
    print("F5 押下（ready =", server.ready(), ")")
    ctl.ptt_down()
    time.sleep(len(pcm) / 16000 + 0.2)
    print("F5 離す")
    ctl.ptt_up()


threading.Thread(target=press_f5, daemon=True).start()
asyncio.run(browser())
finals = [e for e in bridge.events if e[1] == "transcript_final"]
print("確定:", finals[-1][2][0] if finals else "なし")
