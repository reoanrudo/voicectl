"""マイク入力。ストリームは常時開いておき、押下中だけ録音バッファに貯める（開始遅延をなくすため）。

押す直前の 0.3 秒も残しておき、キーを押すのと同時に話し始めても頭が切れないようにする。
"""
from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)
TARGET_SR = 16000


class NoMicrophone(RuntimeError):
    pass


def _find_device(spec) -> int:
    devices = sd.query_devices()
    if isinstance(spec, int):
        return spec
    if spec is not None:
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and str(spec).lower() in d["name"].lower():
                return i
        log.warning("マイク '%s' が見つからないため既定デバイスを使用", spec)
    default_in = sd.default.device[0]
    if default_in is not None and default_in >= 0:
        return default_in
    # 既定の入力デバイスがない（リモートデスクトップ接続中など）：マイクらしいものを探す。
    # 「ステレオ ミキサー」は PC の再生音を録る装置なので使わない
    # NVIDIA Broadcast は GPU を使う仮想マイクで、開いて試すと GPU ドライバーを巻き込むおそれがある
    # （この PC で自動選択の試行中に nvlddmkm のバグチェック 0x116 が起きた）。Bluetooth のハンズフリーも開くと失敗する
    skip = ("ステレオ ミキサー", "stereo mix", "loopback", "ライン入力", "line in", "broadcast", "bthhfenum",
            "hands-free", "input ()")
    inputs = [(i, d["name"]) for i, d in enumerate(devices)
              if d["max_input_channels"] > 0 and not any(s in d["name"].lower() for s in skip)]
    ordered = sorted(inputs, key=lambda x: not any(m in x[1].lower() for m in ("マイク", "mic")))
    # 開けても音声データを出さないデバイスがある（リモート接続中の Realtek など）ので、実際に試して選ぶ
    for i, name in ordered:
        if _delivers_audio(i):
            log.warning("既定のマイクがないため '%s' を使用（音声データを確認済み）", name)
            return i
    raise NoMicrophone("音声データを出すマイクが見つかりません")


def _delivers_audio(index: int, seconds: float = 0.4) -> bool:
    got = []
    try:
        sr = int(sd.query_devices(index)["default_samplerate"])
        with sd.InputStream(device=index, channels=1, samplerate=sr, dtype="float32",
                            callback=lambda indata, *_: got.append(len(indata))):
            import time
            time.sleep(seconds)
    except Exception:
        return False
    return sum(got) > 0


class Recorder:
    def __init__(self, device=None, preroll_sec: float = 0.3):
        self.spec = device
        self.preroll_sec = preroll_sec
        self._lock = threading.Lock()
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self.level = 0.0  # オーバーレイの音量表示用
        self._last_callback = 0.0
        self.generation = 0   # start() を呼ぶたびに増える。ほかの誰かが録音を始めたかどうかの判定に使う
        self._build()

    def _build(self) -> None:
        self.device = _find_device(self.spec)
        info = sd.query_devices(self.device, "input")
        self.name = info["name"]
        self.sr = int(info["default_samplerate"])
        self._preroll: collections.deque = collections.deque(maxlen=max(1, int(self.preroll_sec * self.sr / 1024)))
        self._stream = sd.InputStream(device=self.device, channels=1, samplerate=self.sr, blocksize=1024,
                                      dtype="float32", callback=self._callback)
        log.info("マイク: %s (%d Hz)", self.name, self.sr)

    def open(self) -> None:
        self._stream.start()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()

    def reopen(self) -> str:
        """デバイス一覧を取り直してマイクを選び直す。リモート接続でマイク転送を有効にした場合などに使う。"""
        try:
            self.close()
        except Exception:
            pass
        sd._terminate()   # PortAudio は初期化時のデバイス一覧を使い続けるため、作り直さないと新しいマイクが見えない
        sd._initialize()
        self._build()
        self.open()
        return self.name

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(mono ** 2)))
        self._last_callback = time.monotonic()
        with self._lock:
            if self._recording:
                self._chunks.append(mono)
            else:
                self._preroll.append(mono)

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        with self._lock:
            self.generation += 1
            self._chunks = list(self._preroll)
            self._recording = True

    def stop_if_generation(self, gen: int) -> np.ndarray | None:
        """自分が始めた録音（世代 gen）がまだ続いているときだけ止める。ほかの誰か（F5 など）が
        録音を始め直していたら None を返して何もしない（その録音を壊さないため）。"""
        with self._lock:
            if not self._recording or self.generation != gen:
                return None
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(chunks), self.sr, TARGET_SR)

    def peek(self) -> np.ndarray:
        """録音中のここまでの音声（途中経過の文字起こし用）。"""
        with self._lock:
            chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(chunks), self.sr, TARGET_SR)

    def take(self) -> np.ndarray:
        """ここまでの音声を取り出す。録音は続ける（話の途切れごとに命令を区切るため）。"""
        with self._lock:
            chunks, self._chunks = self._chunks, []
        return resample(np.concatenate(chunks), self.sr, TARGET_SR) if chunks else np.zeros(0, dtype=np.float32)

    def trim(self, keep_sec: float) -> None:
        """話していない音声がたまり続けないよう、最後の keep_sec 秒だけ残す。"""
        with self._lock:
            keep, total = [], 0
            for c in reversed(self._chunks):
                keep.append(c)
                total += len(c)
                if total >= keep_sec * self.sr:
                    break
            self._chunks = list(reversed(keep))

    def alive(self) -> bool:
        """マイクから直近 1 秒以内にデータが届いているか。"""
        return time.monotonic() - self._last_callback < 1.0

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return resample(np.concatenate(chunks), self.sr, TARGET_SR)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.float32)
    n_out = int(len(x) * sr_out / sr_in)
    t_out = np.linspace(0, len(x) - 1, n_out)
    return np.interp(t_out, np.arange(len(x)), x).astype(np.float32)
