"""WAV を流して、STT → 画面情報 → 判定 → 実行判断まで全体を通す（手動確認用）。

実際のマウス・キー操作は行わず、実行されるはずだった内容を表示する。
Jev の API キー（TYPESAFE_API_KEY）が設定されていれば Jev、なければキーワード照合で判定する。
"""
import logging
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl import winutil  # noqa: E402
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.audio import resample  # noqa: E402
from voicectl.config import load  # noqa: E402
from voicectl.controller import Controller  # noqa: E402
from voicectl.stt import SpeechRecognizer  # noqa: E402

logging.basicConfig(level=logging.WARNING)
winutil.set_dpi_aware()


class Sig:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink

    def emit(self, *a):
        self.sink.append((self.name, a))


class FakeUi:
    def __init__(self):
        self.events = []
        for n in ("status", "level", "hints_labels", "hints_grid", "hints_clear"):
            setattr(self, n, Sig(n, self.events))


def load_wav(p):
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        return resample(x, w.getframerate(), 16000)


cfg = load()
engine = None
try:
    from voicectl.engines.jev import JevEngine
    j = cfg.get("decision.jev")
    engine = JevEngine(j["endpoint"], j["model"], j["api_key_env"], j["timeout_sec"])
except Exception as e:
    print("Jev なし:", e)

catalog = AppCatalog(cfg.get("app_aliases"))
if not catalog.candidates():
    catalog.refresh()
ui = FakeUi()
ctl = Controller(cfg, ui, None, SpeechRecognizer(), engine, catalog)
executed = []
ctl.executor.run = lambda d, lookup: executed.append(d.describe(lookup))
ctl.executor.click = lambda b: executed.append(f"click({b})")
ctl.executor.release = lambda: None

for f in sorted((Path(__file__).parent / "audio").glob("*.wav")):
    ui.events.clear()
    executed.clear()
    ctl.pending = None
    ctl.context.begin()
    time.sleep(0.8)  # 話している時間の代わり（この間に UIA / OCR が走る）
    t = time.perf_counter()
    ctl._handle_audio(load_wav(f))
    total = (time.perf_counter() - t) * 1000
    last = [e for e in ui.events if e[0] == "status"][-1][1]
    print(f"{f.stem:11s} {total:5.0f} ms | {last[0]:7s} {last[1]} | 実行: {executed or '-'}")
