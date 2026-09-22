"""待機中に呼びかけ（ウェイクワード）で聞き取りを始める（ハンズフリー）。

F5 を押さなくても、決まった呼びかけ（既定は「コンピューター」）をすると聞き取りが始まる。
仕組み：待機中はマイクの音量だけを見て、話し声が始まったらその発話を短く録音し、
終わったところで認識してウェイクワードが含まれるか確かめる（毎回認識すると GPU が忙しくなるため）。

- 呼びかけだけのとき（「コンピューター」）：そのまま常時聞き取りセッションに入る。
  話すたびに命令が実行され、しばらく無音が続くと自動で待機に戻る。
- 呼びかけと一緒に言ったとき（「コンピューター、音量30」）：続きの文をそのまま命令として実行する。

F5 との競合を避けるため、録音は世代番号で管理する（audio.Recorder.stop_if_generation）。
自分が始めた録音の途中で F5 が押されたら、確認は静かに中止し、F5 の録音を優先する。
"""
from __future__ import annotations

import logging
import threading
import time

from . import phonetic, textparse

log = logging.getLogger(__name__)

DEFAULT_WORDS = ["コンピューター"]


def word_readings(words: list[str]) -> list[tuple[str, str]]:
    """ウェイクワードの読み（カタカナ・長音なし）。表記ゆれに強い照合に使う。"""
    out = []
    for w in words:
        r = phonetic._reading(textparse.compact(w)).replace("ー", "")
        if len(r) >= 4:
            out.append((w, r))
    if not out:
        log.warning("ウェイクワードが短すぎるため、既定の「コンピューター」を使います")
        out = word_readings(DEFAULT_WORDS)
    return out


def match(text: str, words: list[str]) -> tuple[str, str] | None:
    """認識結果にウェイクワードが含まれていれば (単語, 続きの文)。含まれていなければ None。

    まずそのままの文字で探し（認識はたいてい「コンピューター」と書き出す）、
    見つからなければ読みで探す（「こんぴゅーたー」などの揺れ）。
    読しか見つからないときは、続きの文の位置を正確に取れないので、セッション開始だけを行う。
    """
    if not text:
        return None
    c = textparse.compact(textparse.normalize(text))
    for w in words:
        for v in sorted({w, w.rstrip("ー")}, key=len, reverse=True):
            i = c.find(v)
            if i >= 0 and len(v) >= 4:
                rest = c[i + len(v):].lstrip("ー、。,.！? ")
                return w, rest
    rd = phonetic._reading(c).replace("ー", "")
    for w, r in word_readings(words):
        if r in rd:
            return w, ""
    return None


class WakeListener:
    """待機中のマイクを監視して、ウェイクワードで controller に知らせるスレッド。"""

    def __init__(self, recorder, stt, ctl, cfg):
        self.recorder = recorder
        self.stt = stt
        self.ctl = ctl
        self.enabled = bool(cfg.get("wake_word.enabled", True))
        self.words = [str(w) for w in (cfg.get("wake_word.words") or DEFAULT_WORDS)]
        self.level = float(cfg.get("wake_word.level", 0.02))
        self.probe_silence_sec = float(cfg.get("wake_word.probe_silence_sec", 1.2))
        self.min_interval_sec = float(cfg.get("wake_word.min_interval_sec", 2.0))
        self.session_idle_sec = float(cfg.get("wake_word.session_idle_sec", 12.0))
        self._stop_ev = threading.Event()
        self._last_probe = 0.0
        self._thread = threading.Thread(target=self._run, name="wake", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_ev.set()

    def _blocked(self) -> bool:
        """ウェイクを聞かない状態：聞き取り中・書き取り中・一時停止中・読み上げ中・確認の直後。"""
        ctl = self.ctl
        return (not self.enabled or ctl.paused or ctl.listening or ctl.dictating
                or getattr(ctl.speaker, "busy", False))

    def _run(self) -> None:
        if self.enabled:
            log.info("ウェイクワード待ち: %s", "・".join(self.words))
        else:
            log.debug("ウェイクワードは無効（トレイ・config で切り替え可）")
        while not self._stop_ev.is_set():
            time.sleep(0.05)
            try:
                if self._blocked() or self.recorder.recording \
                        or time.monotonic() - self._last_probe < self.min_interval_sec:
                    continue
                if self.recorder.level < self.level:
                    continue
                # 話し声が始まった：録音して（0.3 秒の先読み付き）終わりの無音を待つ
                gen = self.recorder.generation
                self.recorder.start()
                if not self._wait_silence(gen):
                    continue   # F5 などに割り込まれた
                audio = self.recorder.stop_if_generation(gen)
                self._last_probe = time.monotonic()
                if audio is None or len(audio) < 16000 * 0.4:
                    continue
                text = self.stt.transcribe(audio, None)
                m = match(text, self.words)
                if m is None:
                    continue
                word, rest = m
                log.info("ウェイクワードを検出: %s（続き: %s）", text[:40] or word, rest[:30])
                if len(rest) >= 2:
                    self.ctl.say(rest)   # 「コンピューター、音量30」→ 続きをそのまま命令として実行
                else:
                    self.ctl.wake_session_start(self.session_idle_sec)
            except Exception:
                log.exception("ウェイクワードの監視でエラー")
                time.sleep(1.0)

    def _wait_silence(self, gen: int, max_sec: float = 8.0) -> bool:
        """発話の終わり（probe_silence_sec 秒の無音）を待つ。割り込まれたら False。"""
        silent = 0.0
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_sec:
            time.sleep(0.05)
            if self._blocked() or self.recorder.generation != gen:
                return False
            if self.recorder.level < self.level:
                silent += 0.05
                if silent >= self.probe_silence_sec:
                    return True
            else:
                silent = 0.0
        return True   # 長すぎる話はそこまでで切って確認する
