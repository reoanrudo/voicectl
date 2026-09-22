"""音声合成で作った WAV を STT に通して、認識結果と所要時間を表示する（手動確認用）。"""
import sys, time, wave, logging
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl.audio import resample
from voicectl.stt import SpeechRecognizer

logging.basicConfig(level=logging.INFO)
rec = SpeechRecognizer()
for f in sorted((Path(__file__).parent / "audio").glob("*.wav")):
    with wave.open(str(f)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if w.getnchannels() == 2:
            x = x[::2]
        x = resample(x, w.getframerate(), 16000)
    t = time.perf_counter()
    text = rec.transcribe(x)
    print(f"{f.stem:8s} {(time.perf_counter()-t)*1000:6.0f} ms  {text}")
