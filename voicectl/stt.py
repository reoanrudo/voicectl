"""faster-whisper による日本語音声認識。指定した名前の GPU（既定は 4080。3060 は PCIe が不安定なため使わない）で動かす。"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# 無音やノイズで Whisper がよく出す定型の幻聴
_HALLUCINATIONS = ("ご視聴ありがとうございました", "ご視聴ありがとうございます", "チャンネル登録", "おやすみなさい",
                   "ありがとうございました", "字幕", "(音楽)", "(拍手)", "♪")

_PROMPT = ("パソコンの音声操作。メモ帳開いて。右クリック。ダブルクリック。下にスクロール。もうちょい右。"
           "番号表示。グリッド。コピー。貼り付け。元に戻す。ストップ。")

# 1 回の出力トークンの上限。日本語は 1 文字 ≒ 1〜1.5 トークンなので、
# 64（≒40 文字）だと長い命令の後半（「…して、それから〜」）が黙って切られていた。
# ただし Whisper はプロンプト（認識ヒント）と出力を合わせて 448 トークンまでしか扱えないため、
# ヒントの長さに応じて実際の上限を下げる（384 固定だと長いヒントで例外になっていた）。
_MAX_NEW_TOKENS = 384
_MIN_NEW_TOKENS = 48
_WHISPER_WINDOW = 448        # Whisper の 1 ウィンドウ（プロンプト＋出力＋特殊トークン）
_TOKEN_MARGIN = 8
# 発話の途中の息継ぎで分割されても、前後の音を落とさないようにする余白（ミリ秒）
_SPEECH_PAD_MS = 300


def prompt_tokens(prompt: str, hotwords: str | None, tokenizer=None) -> int:
    """認識ヒント（initial_prompt + hotwords）が使うトークン数。"""
    text = " ".join(x for x in (prompt, hotwords) if x)
    if not text:
        return 0
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(text).ids)
        except Exception:
            pass
    return max(1, int(len(text) * 1.2))   # トークナイザが使えないときの概算（日本語 1 文字 ≒ 1.2）


def max_new_tokens(prompt: str, hotwords: str | None = None, tokenizer=None,
                   want: int = _MAX_NEW_TOKENS) -> int:
    """ヒントを入れても窓に収まる出力の上限。長い命令を切らず、かつ例外を出さない値。"""
    room = _WHISPER_WINDOW - prompt_tokens(prompt, hotwords, tokenizer) - _TOKEN_MARGIN
    return max(_MIN_NEW_TOKENS, min(want, room))


def transcribe_kwargs(prompt: str, hotwords: str | None, language: str, beam_size: int,
                      tokenizer=None, want: int = _MAX_NEW_TOKENS) -> dict:
    """Whisper に渡すパラメータ（モデルなしで検証できるよう分けてある）。

    temperature=0.0 で質が低いときの温度を変えた再試行（最大 6 回）をしない：音割れやヒントで
    崩れると 1 回 0.4 秒が 6〜12 秒になっていた（実測）。
    """
    return {
        "language": language, "beam_size": beam_size, "vad_filter": True,
        # 息継ぎで発話が途中で切れないよう、区切りの前後に余白を足す
        "vad_parameters": {"min_silence_duration_ms": 400, "speech_pad_ms": _SPEECH_PAD_MS},
        "without_timestamps": True, "condition_on_previous_text": False,
        "initial_prompt": prompt, "hotwords": hotwords,
        "temperature": 0.0, "no_repeat_ngram_size": 3,
        "max_new_tokens": max_new_tokens(prompt, hotwords, tokenizer, want),
    }


def _setup_cuda_dlls() -> None:
    """pip の nvidia-cublas / nvidia-cudnn の DLL を ctranslate2 から見えるようにする。"""
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
        d = base / sub / "bin"
        if d.is_dir():
            os.add_dll_directory(str(d))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def find_gpu_index(name_part: str) -> int:
    """CUDA_DEVICE_ORDER=PCI_BUS_ID 前提で、名前に name_part を含む GPU の番号を返す。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        for i in range(pynvml.nvmlDeviceGetCount()):
            n = pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(i))
            n = n.decode() if isinstance(n, bytes) else n
            if name_part.lower() in n.lower():
                log.info("STT は GPU %d (%s) を使用", i, n)
                return i
    except Exception:
        log.exception("GPU の列挙に失敗")
    log.warning("'%s' を含む GPU が見つからないため GPU 0 を使用", name_part)
    return 0


class SpeechRecognizer:
    def __init__(self, model: str = "large-v3-turbo", gpu_name: str = "4080", compute_type: str = "float16",
                 language: str = "ja", beam_size: int = 1, extra_vocab: list[str] | None = None):
        os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")  # nvml と番号を揃える
        # 使う GPU だけを CUDA に見せる。ほかの GPU（PCIe が不安定な 3060 など）には CUDA の処理を一切載せない
        os.environ["CUDA_VISIBLE_DEVICES"] = str(find_gpu_index(gpu_name))
        _setup_cuda_dlls()
        from faster_whisper import WhisperModel
        t0 = time.perf_counter()
        self.model = WhisperModel(model, device="cuda", device_index=0, compute_type=compute_type)
        self.language = language
        self.beam_size = beam_size
        self._lock = threading.Lock()
        vocab = list(dict.fromkeys(extra_vocab or []))  # 重複を除いて順番は保つ
        # ヒントは短く保つ（長いと認識が崩れやすく、崩れると再試行で遅くなる）。固有名詞・英語表記は hotwords で渡す
        self.prompt = _PROMPT
        self.hotwords = " ".join(vocab[:30]) or None
        log.info("STT モデル読み込み完了 (%.1f s)", time.perf_counter() - t0)
        # 初回の CUDA 初期化を済ませておく（無音だと VAD で全部捨てられてデコーダが動かないため、VAD なしで流す）
        warm = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
        list(self.model.transcribe(warm, language=language, beam_size=1, vad_filter=False)[0])

    def transcribe(self, audio: np.ndarray, extra_vocab: list[str] | None = None) -> str:
        """extra_vocab: 前面のアプリのプロファイルにある用語（その場だけ認識ヒントに足す）。"""
        prompt, hotwords = self.prompt, self.hotwords
        if extra_vocab:  # 前面のアプリの用語は hotwords の先頭に足す（合計が長くなりすぎないよう切る）
            hotwords = " ".join((extra_vocab[:20] + (hotwords.split() if hotwords else []))[:40])
        tok = getattr(self.model, "hf_tokenizer", None)
        kw = transcribe_kwargs(prompt, hotwords, self.language, self.beam_size, tok)
        with self._lock:  # 途中経過の文字起こしと確定の文字起こしが同時に GPU を使わないようにする
            try:
                segments, info = self.model.transcribe(audio, **kw)
                text = "".join(s.text for s in segments).strip()
            except ValueError as e:
                # ヒントが長すぎて窓に収まらないときは、ヒントを外してもう一度だけ試す（発話を捨てない）
                log.warning("認識ヒント付きで失敗したため、ヒントを外して再試行します: %s", e)
                kw.pop("initial_prompt", None)
                kw.pop("hotwords", None)
                kw["max_new_tokens"] = max_new_tokens("", None, tok)
                segments, info = self.model.transcribe(audio, **kw)
                text = "".join(s.text for s in segments).strip()
        if any(h in text for h in _HALLUCINATIONS) and len(text) < 30:
            return ""
        # 長く話したのに出力が極端に短いときは、上限で切られた可能性が高い（ログで気づけるようにする）
        secs = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
        if secs > 3.0 and len(text) / secs < 2.0:
            log.warning("認識結果が短すぎます（%.1f 秒の発話で %d 文字）: %s", secs, len(text), text[:60])
        return text
