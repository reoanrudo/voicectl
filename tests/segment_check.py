"""「話の途切れで即実行」の確認（手動確認用）。F5 を押したまま続けて命令する状況を、実時間で音声を流して再現する。

実際の操作は行わない。Jev の API キーがあれば Jev、なければキーワード照合で判定する。
"""
import logging
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.audio import resample  # noqa: E402
from voicectl.config import load  # noqa: E402
from voicectl.controller import Controller  # noqa: E402
from voicectl.stt import SpeechRecognizer  # noqa: E402

logging.basicConfig(level=logging.WARNING)
T0 = time.perf_counter()


def now():
    return time.perf_counter() - T0


class StreamRecorder:
    """音声を実時間で流し込むマイクの代役（Recorder と同じ start/peek/take/stop/trim/alive）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf = []
        self.recording = False
        self.level = 0.0

    def feed(self, x):
        with self.lock:
            if self.recording:
                self.buf.append(x)

    def start(self):
        with self.lock:
            self.buf, self.recording = [], True

    def peek(self):
        with self.lock:
            return np.concatenate(self.buf) if self.buf else np.zeros(0, np.float32)

    def take(self):
        with self.lock:
            b, self.buf = self.buf, []
        return np.concatenate(b) if b else np.zeros(0, np.float32)

    def trim(self, keep_sec):
        pass

    def alive(self):
        return True

    def stop(self):
        with self.lock:
            self.recording = False
            b, self.buf = self.buf, []
        return np.concatenate(b) if b else np.zeros(0, np.float32)


class Sig:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink

    def emit(self, *a):
        self.sink.append((now(), self.name, a))


class Ui:
    def __init__(self):
        self.events = []
        for n in ("status", "level", "hints_labels", "hints_grid", "hints_clear", "transcript_partial",
                  "transcript_final", "transcript_result", "idle"):
            setattr(self, n, Sig(n, self.events))


def wav(name):
    with wave.open(str(Path(__file__).parent / "audio" / f"{name}.wav")) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        return resample(x, w.getframerate(), 16000)


cfg = load()
engine = None
try:
    from voicectl.engines.jev import JevEngine
    j = cfg.get("decision.jev")
    engine = JevEngine(j["endpoint"], j["model"], j["api_key_env"], j["timeout_sec"])
    engine.warmup()
except Exception as e:
    print("Jev なし:", e)

vocab = [str(v) for v in cfg.get("dictionary", {}).values()] + [str(w) for w in cfg.get("vocabulary", [])]
rec = StreamRecorder()
ui = Ui()
catalog = AppCatalog(cfg.get("app_aliases"))
ctl = Controller(cfg, ui, rec, SpeechRecognizer(extra_vocab=vocab), engine, catalog)
executed = []
ctl.executor.run = lambda d, lookup: executed.append((now(), d.describe(lookup)))
ctl.start()

names = sys.argv[1:] or ["02_right", "03_click", "04_scroll"]
silence = np.zeros(int(16000 * 0.9), np.float32)
ends = []
ctl.ptt_down()
T0 = time.perf_counter()
print(f"{now():5.2f}s F5 押下（{len(names)} 個の命令を続けて話す）")
for n in names:
    x = np.concatenate([wav(n), silence])
    for i in range(0, len(x), 1600):  # 0.1 秒ずつ実時間で流す
        rec.feed(x[i:i + 1600])
        time.sleep(0.1)
        if i + 1600 >= len(x) - len(silence) and len(ends) < names.index(n) + 1:
            ends.append(now())
            print(f"{now():5.2f}s 「{n}」を話し終わる")
print(f"{now():5.2f}s F5 を離す")
ctl.ptt_up()
time.sleep(4)
for t, kind, a in ui.events:
    if kind in ("transcript_final", "transcript_result"):
        print(f"{t:5.2f}s {kind}: {a[0]}")
for t, desc in executed:
    print(f"{t:5.2f}s 実行: {desc}")
