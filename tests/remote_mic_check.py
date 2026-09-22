"""ブラウザマイクの経路の確認（手動確認用）：ブラウザの代わりに WebSocket で WAV を送り、PC からの通知を表示する。

起動中の voicectl に接続する。送る音声は tests/audio/02_right.wav（「もうちょい右」：マウスが少し右に動く）。
"""
import asyncio
import json
import ssl
import sys
import time
import wave
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl.audio import resample  # noqa: E402
from voicectl.config import ROOT  # noqa: E402

WAV = Path(__file__).parent / "audio" / (sys.argv[1] if len(sys.argv) > 1 else "02_right.wav")
TOKEN = (ROOT / "cache" / "remote_mic_token.txt").read_text().strip()


async def main():
    with wave.open(str(WAV)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        x = resample(x, w.getframerate(), 16000)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 自己署名証明書
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"https://127.0.0.1:8765/?t=wrong", ssl=ctx)
        print("トークン違いのページ:", r.status)
        r = await s.get(f"https://127.0.0.1:8765/?t={TOKEN}", ssl=ctx)
        print("ページ:", r.status, "マイクボタンあり" if "押している間" in await r.text() else "内容が不正")
        async with s.ws_connect(f"wss://127.0.0.1:8765/ws?t={TOKEN}", ssl=ctx) as ws:
            t0 = time.perf_counter()
            await ws.send_str(json.dumps({"cmd": "down"}))
            for i in range(0, len(pcm), 2048):  # 実時間に近い速さで送る
                await ws.send_bytes(pcm[i:i + 2048].tobytes())
                await asyncio.sleep(2048 / 16000)
            await ws.send_str(json.dumps({"cmd": "up"}))
            t_up = time.perf_counter()
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=6)
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                print(f"  +{(time.perf_counter() - t_up) * 1000:5.0f} ms  {m['kind']}: {m['args'][:2]}")
                if m["kind"] == "transcript_result":
                    break
            print(f"送信 {len(pcm) / 16000:.2f} 秒分, 合計 {(time.perf_counter() - t0):.2f} 秒")


asyncio.run(main())
