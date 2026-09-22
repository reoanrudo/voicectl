"""処理の流れと状態遷移。

  押下 → 録音開始＋画面情報の先読み → 離す → STT → （停止/確認/ヒント中の特別処理）
  → 高速経路 or Jev で判定 → 自信度に応じて 実行 / 確認 / 聞き返し → オーバーレイに結果表示

キーフックや UI から呼ばれるメソッドはキューに積み、処理は専用スレッド 1 本で順番に行う。
"""
from __future__ import annotations

import json
import os
import logging
import queue
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from . import candidates, followup, textparse, winutil
from .config import ROOT, Config
from .context import ContextCollector, Snapshot
from .engines.base import DecisionEngine
from .engines.keyword import KeywordEngine, fast_path
from .executor import ExecutionError, Executor
from .schema import Decision

log = logging.getLogger(__name__)

_UNFINISHED = re.compile(r"(を|が|に|で|から|と|の|は|へ|も|や|って|より|まで|、|でし|まし)$")


_ACTION_TE = ("開いて", "ひらいて", "起動して", "コピーして", "切り取って", "貼り付けて", "保存して", "撮って",
              "最大化して", "最小化して", "スクロールして", "クリックして", "押して", "閉じて", "選択して", "入力して")
_CHAIN = re.compile(r"(?<=て)(?:から|、それから|、そのあと|、次に|、)|(?<=して)(?:から)"
                    r"|(?:" + "|".join(f"(?<={v})" for v in _ACTION_TE) + r")"
                    r"(?!ください|下さい|お願い|おねがい|おいて|くれ|ほしい|欲しい|みて"
                    r"|(?:開いて|ひらいて|表示して|実行して)[。．.]?$)(?=[^、。]{2,})|。")
# 「GTOトレーナーを選択して開いて」の「開いて」のように、後ろが動詞だけなら 1 つの操作なので分けない
# 「オブシリアン開いて全面に表示して」「保存して閉じて」のように、読点なしで命令がつながった複文も分ける。
# ただし後に続きがない・「〜してください」のような言い回しの続きなら分けない


def _split_chain(text: str) -> list[str]:
    """「〇〇を開いてから△△して」「〇〇して、△△して」を命令ごとに分ける。分けられなければ 1 要素。"""
    parts = [p.strip("、。 ") for p in _CHAIN.split(text.strip())]
    parts = [p for p in parts if len(p) >= 2]
    return parts if len(parts) > 1 else [text]


def winutil_profile(ctl):
    """今の前面のアプリのプロファイル（分けた命令ごとに前面が変わることがあるため取り直す）。"""
    return ctl.profiles.active(winutil.foreground_window())


_OPEN_VERB = re.compile(r"(開いて|ひらいて|起動|立ち上げ|開け|開く)")
_CORRECTION = re.compile(r"(?:(?:アプリ|サイト|名前|正しく|それ|開きたいの|開くの)(?:は|が))?"
                         r"(?P<n>[^、。はがをにで]{1,24}?)(?:です|だよ|だって|のこと|ですよ|ですね|っす|な)(?:よ|ね)?")


def _correction(text: str, last_open: tuple[str, float] | None, within: float = 45.0) -> str | None:
    """直前（45 秒以内）に「〇〇を開いて」と言っていて、今の発話が「〇〇です」だけなら、開き直す命令を返す。

    直前の文にブラウザの指定（「Brave で」）があれば引き継ぐ。
    """
    if last_open is None or time.monotonic() - last_open[1] > within:
        return None
    c = textparse.compact(textparse.normalize(text)).rstrip("。．.!！")
    m = _CORRECTION.fullmatch(c)
    if not m:
        return None
    name = m.group("n").strip()
    if not name or _OPEN_VERB.search(name) or name in ("そう", "違う", "ちがう", "これ", "それ", "あれ", "何", "なん"):
        return None
    prev = textparse.compact(last_open[0])
    b = re.match(r"(Brave|ブレイブ|Chrome|クローム|Edge|エッジ|Firefox|ファイアフォックス)(で|から|を使って)", prev, flags=re.I)
    rest = _split_chain(last_open[0])[1:]   # 「オブシリアン開いて全面に表示して」の後半は残す
    return (b.group(0) if b else "") + f"{name}を開いて" + "".join("、" + p for p in rest)


def _noun(text: str) -> str:
    return re.sub(r"(を|の)?(開いて|ひらいて|押して|クリック|出して|表示して|見せて|して|ください)+$", "",
                  textparse.compact(text).rstrip("。"))


def _element_named(text: str, lookup) -> str | None:
    """発話の名詞（「設定を開いて」の「設定」）と名前が完全に一致する画面上のボタン等の候補 ID。"""
    noun = _noun(text)
    if len(noun) < 2:
        return None
    for cid, cand in (lookup.get("element") or {}).items():
        if cand.data.source == "uia" and textparse.compact(cand.data.name) == noun:
            return cid
    return None


def _names_on_screen(text: str, kd: Decision, lookup) -> bool:
    """即実行のキー操作（「設定を開いて」＝Windows の設定）でも、同じ名前のボタンやメニューが今の画面にあるなら
    アプリ内の操作の可能性が高いので、Jev に判断させる（アプリ内を優先する方針）。"""
    if kd.action != "key_combo":
        return False
    noun = _noun(text)
    if len(noun) < 2:
        return False
    for kind in ("element", "menu"):
        for cand in (lookup.get(kind) or {}).values():
            name = cand.data.name if kind == "element" else cand.data.path[-1]
            if textparse.compact(name) == noun:
                return True
    return False


# 画面の名前を押す可能性がある言い方（「保存を押して」「設定を開いて」）。即実行の対象から外す
_FAST_VERB = re.compile(r"(押して|押す|開いて|ひらいて|見せて|みせて|表示して|出して|クリック|選んで|選択)")


def _looks_unfinished(text: str) -> bool:
    """助詞や読点で終わっていて、命令の途中で切れていそうか。"""
    return bool(_UNFINISHED.search(text.strip().rstrip("。.!！?？ ")))


class Controller:
    def __init__(self, cfg: Config, ui, recorder, stt, engine: DecisionEngine | None, catalog):
        self.cfg = cfg
        self.ui = ui              # UiBridge（Qt シグナル）
        self.recorder = recorder
        self.stt = stt
        self.engine = engine      # None ならキーワードエンジンのみ
        self.keyword = KeywordEngine()
        self.catalog = catalog
        self.context = ContextCollector(cfg.get("screen.use_uia", True), cfg.get("screen.use_ocr", True),
                                        cfg.get("screen.ocr_language", "ja"))
        self.executor = Executor(cfg.get("mouse.step_px", {}), cfg.get("mouse.scroll_notches", {}))
        self.th_exec = float(cfg.get("thresholds.execute", 0.8))
        self.th_confirm = float(cfg.get("thresholds.confirm", 0.5))
        # アプリ内のメニュー・ボタン（取り消しにくい操作を除く）は、この確信度以上なら確認せずに実行する
        self.th_in_app = float(cfg.get("thresholds.in_app_execute", 0.5))
        self.confirm_timeout = float(cfg.get("thresholds.confirm_timeout_sec", 8))
        from .config import dictionary
        from .profiles import Profiles
        self.profiles = Profiles()
        self.dictionary = self.profiles.readings()   # プロファイルのアプリ名の読み（ピオソルバー → PioSolver など）
        self.dictionary.update(dictionary(cfg))      # アプリの読み辞書 ＋ config.yaml の dictionary
        catalog.extra = self.profiles.launchable()   # スタートメニューにないアプリ
        self._appmaps: dict[str, object] = {}
        self.confirm_words = [str(w) for w in (cfg.get("menu_confirm_words") or
                                               ["終了", "閉じる", "削除", "消去", "クリア", "リセット", "初期化",
                                                "Exit", "Quit", "Close", "Delete", "Remove", "Clear", "Reset",
                                                "Eliminate", "Disconnect"])]
        self.limits = {"app": cfg.get("decision.jev.max_app_candidates", 60),
                       "element": cfg.get("decision.jev.max_element_candidates", 120),
                       "window": cfg.get("decision.jev.max_window_candidates", 40)}

        self.paused = False
        self.previous: Decision | None = None
        self.previous_lookup: candidates.Lookup = {}
        self.pending: tuple[Decision, candidates.Lookup, float] | None = None  # 確認待ち（期限つき）
        self.pending_plan: tuple[list, candidates.Lookup, float] | None = None  # LLM の複数手順の確認待ち
        # 直前の操作の文脈（「続き」「さっきのページ」「さっきのアプリ」で使う）
        self.session: dict = {}
        # 直前に実行した手順（steps, lookup, hwnd, 次に実行する手順, 時刻）。「続き」で残り／失敗した手順から再開する
        self._last_plan: tuple[list, candidates.Lookup, int, int, float] | None = None
        self._last_was_plan = False
        # 「〇〇して△△して」のように分けて実行した命令の残り（parts, 次に実行する位置, 時刻）
        self._last_chain: tuple[list[str], int, float] | None = None
        self._last_facts: dict = {}    # 直近の判定で Jev に渡した facts（LLM フォールバックで再利用）
        from .engines.llm import LLMFallback
        lc = cfg.get("decision.llm_fallback") or {}
        local = lc.get("local") or {}
        # モデル名とタイムアウトは backend ごとに取る（opencode のときに local の bonsai2-27b を渡して
        # 毎回失敗していた。2026-09-22）
        use_local = str(lc.get("backend", "local")).lower() == "local"
        src = local if use_local else {}
        self.llm = (LLMFallback(str(src.get("model") or lc.get("model", "bonsai2-27b")),
                                str(lc.get("agent", "voicectl-fallback")),
                                float(src.get("timeout_sec") or lc.get("timeout_sec", 10.0)),
                                int(lc.get("max_plan_steps", 5)), int(lc.get("max_text_len", 200)),
                                backend=str(lc.get("backend", "local")),
                                base_url=str(local.get("base_url", "http://127.0.0.1:8080/v1")),
                                max_tokens=int(local.get("max_tokens", 512)))
                    if lc.get("enabled", True) else None)
        # 覚えた言い方（学習）。使うほど LLM も Jev も通さずに実行できるようになる
        from .learner import Learner
        learn_cfg = cfg.get("learning") or {}
        self.learner = (Learner(ROOT / str(learn_cfg.get("path", "state/learned.json")),
                                enabled=bool(learn_cfg.get("enabled", True)),
                                trust_at=int(learn_cfg.get("trust_at", 3)),
                                fuzzy=float(learn_cfg.get("fuzzy", 0.86)),
                                max_entries=int(learn_cfg.get("max_entries", 500)))
                        if learn_cfg.get("enabled", True) else None)
        self._llm_off_until = 0.0      # LLM が続けて失敗したときに休む時刻（永久には切らない）
        self.llm_cooldown = float(lc.get("cooldown_sec", 90))
        self._pending_text = ""        # 確認待ちの元の発話（「はい」で覚えるため）
        self._learned_key: str | None = None   # 直前の実行が学習した言い方だったときのキー
        self._last_learnable: tuple[str, Decision, candidates.Lookup] | None = None  # 「これ覚えて」用
        self.hints: dict | None = None  # {"mode": "elements", "items": {n: center}} / {"mode": "grid", "region": ...}
        self._listening = False
        self._active = recorder   # 今の録音の入手元
        self.remote = None        # RemoteMicServer（app.py が設定）
        self._remote_hotkey = False
        # 話の途切れで命令を区切る（F5 を離すのを待たずに実行する）
        self.auto_segment = bool(cfg.get("listen.auto_segment", True))
        self.silence_sec = float(cfg.get("listen.silence_sec", 0.4))
        self.tap_sec = float(cfg.get("listen.tap_sec", 0.3))
        self.continuous_enabled = bool(cfg.get("listen.tap_for_continuous", True))
        self._continuous = False   # 常時聞き取りモード
        self._press_t = 0.0
        self._seg_speech = False   # 今の区切りの中で発話が始まっているか
        self._segments_done = 0    # 今回の聞き取りで処理した区切りの数
        self._carry: tuple[str, float] | None = None  # 途中で切れた命令の前半（文, 時刻）
        self._last_pending: Decision | None = None    # 直前に確認待ちだった命令
        import collections
        self._history: collections.deque = collections.deque(maxlen=3)  # 直近の「言ったこと・したこと・結果」
        from .agent import JevAgent
        self.agent = JevAgent(self, int(cfg.get("agent.max_steps", 12)), float(cfg.get("agent.max_seconds", 90)))
        self.agent_enabled = bool(cfg.get("agent.enabled", True))
        self._current_text = ""
        self._last_open: tuple[str, float] | None = None   # 直前の「〇〇を開いて」系の発話（言い直しの対象）
        from . import intents
        self.speaker = intents.Speaker(bool(cfg.get("voice_reply.enabled", True)), int(cfg.get("voice_reply.rate", 1)))
        self.routines = intents.load_routines(cfg.get("routines", {}))
        # 配置のひな形（config の layouts。「開発の配置」など）
        self.layouts = {str(k): v for k, v in (cfg.get("layouts") or {}).items()}
        self._dictating = False     # 書き取りモード中（話した内容をそのまま入力する）
        self._pending_proc_kill: tuple[int, str, float] | None = None   # プロセス強制終了の確認待ち
        self._last_proc_top: list = []   # 直前に見せた重いプロセスの一覧（「1番を止めて」用）
        self._last_partial: tuple[str, float, int] | None = None   # 途中経過の最後の認識（文, 時刻, 音声の長さ）
        self._wake_session = False  # ウェイクワードで始まった聞き取りセッション（無音で自動終了）
        self._wake_idle_sec = 12.0  # セッション中、何秒無音が続いたら待機に戻るか
        self._wake_last = 0.0       # セッション中に最後に音声を処理した時刻
        self.wake = None            # WakeListener（start() で作る）
        from .demo import DemoRecorder
        self.demo = DemoRecorder()  # やって見せた操作の記録
        self._routine_depth = 0
        # 文章アシスタント（DeepSeek）。文章の作成・要約・翻訳・書き直し・質問への回答
        self.assistant = None
        if cfg.get("assistant.enabled", True):
            from .assistant import Assistant
            self.assistant = Assistant(str(cfg.get("assistant.model", "deepseek/deepseek-v4-flash")),
                                       timeout_sec=float(cfg.get("assistant.timeout_sec", 40)))
        self._draft: tuple[str, int, float] | None = None   # 入力待ちの下書き（本文, 入力先のウィンドウ, 時刻）
        self._ai_busy = False

        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="controller", daemon=True)
        log_dir = ROOT / cfg.get("logging.dir", "logs")
        log_dir.mkdir(exist_ok=True)
        self._log_path = log_dir / f"decisions-{datetime.now():%Y%m%d}.jsonl"

    # ---- 外部から呼ばれる（キーフック・トレイ） ----
    def start(self) -> None:
        self._thread.start()
        threading.Thread(target=self._level_loop, daemon=True).start()
        if self.cfg.get("transcript.live_partial", True):
            threading.Thread(target=self._partial_loop, name="partial-stt", daemon=True).start()
        if self.auto_segment:
            threading.Thread(target=self._segment_loop, name="segmenter", daemon=True).start()
        if self.recorder is not None and self.stt is not None:
            from .wake import WakeListener
            self.wake = WakeListener(self.recorder, self.stt, self, self.cfg)
            self.wake.start()   # 待機中に「コンピューター」などで聞き取りを始められるようにする
        if self.llm is not None and getattr(self.llm, "backend", "") == "local":
            # 最初の発話が待たされないよう、起動と同時にローカル LLM を温めておく
            threading.Thread(target=self.llm.warmup, name="llm-warmup", daemon=True).start()

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def dictating(self) -> bool:
        return self._dictating

    def set_wake_enabled(self, on: bool) -> None:
        """トレイメニューからウェイクワードの有効・無効を切り替える。"""
        if self.wake is not None:
            self.wake.enabled = bool(on)

    def wake_session_start(self, idle_sec: float = 12.0) -> None:
        """ウェイクワードで常時聞き取りセッションを始める（無音が続けば自動で待機に戻る）。"""
        if self.paused or self._listening:
            return
        self._press_t = time.monotonic()
        self._active = self.recorder
        self._active.start()
        self._listening = True
        self._continuous = True
        self._seg_speech = False
        self._segments_done = 0
        self._wake_idle_sec = max(3.0, idle_sec)
        self._wake_session = True
        self._wake_last = time.monotonic()
        log.info("ウェイクワードで聞き取り開始")
        self.ui.idle.emit("listening", "常時聞き取り中")
        self._q.put(("wake_begin",))

    def ptt_down(self, source=None) -> None:
        """source: 音声の入手元。省略時は PC のマイク、ブラウザマイクからは RemoteSource が渡される。"""
        if self.paused:
            return
        self._press_t = time.monotonic()
        if self._listening:  # 常時聞き取り中の押下は、離したときにタップかどうかで判断する
            return
        self._remote_hotkey = False
        if source is None and self.remote and self.remote.ready() and self.remote.remote_down():
            # ブラウザマイクが準備済みなら、F5 でもその端末のマイクを使う
            source = self.remote.source
            self._remote_hotkey = True
        self._active = source or self.recorder
        log.info("録音開始（%s）", "ブラウザ" if source else "PC のマイク")
        self._active.start()
        self._listening = True
        self._continuous = False
        self._seg_speech = False
        self._segments_done = 0
        self._q.put(("begin",))

    def ptt_up(self, from_browser: bool = False) -> None:
        if not self._listening:
            return
        tap = time.monotonic() - self._press_t < self.tap_sec
        if self._continuous:
            if tap and not from_browser:  # 常時聞き取り中にもう一度タップ → 終了
                self._end_listening()
            return
        if tap and self.continuous_enabled and self.auto_segment and not from_browser and not self._seg_speech:
            # 短いタップ → 常時聞き取りモード（話すたびに区切って実行）
            self._continuous = True
            log.info("常時聞き取りモード開始")
            self.ui.idle.emit("listening", "常時聞き取り中")
            self.ui.status.emit("listening", "常時聞き取り中", ["話すたびに実行します", "F5 をもう一度タップで終了"], 0.0)
            return
        self._end_listening()

    def _end_listening(self) -> None:
        if self._continuous:
            log.info("常時聞き取りモード終了")
        self._wake_session = False
        self._listening = False
        self._continuous = False
        self.ui.idle.emit("idle", "待機中")
        if self._remote_hotkey:
            self.remote.remote_up()
            self._q.put(("remote_up",))  # ネットワーク上を流れている最後の音声を待ってから止める
            return
        audio = self._active.stop()
        self._q.put(("audio", audio, True))

    def emergency_stop(self) -> None:
        self.agent.abort = True
        self._q.put(("stop", "緊急停止キー"))

    def confirm(self, yes: bool) -> None:
        self._q.put(("confirm", yes))

    def is_confirming(self) -> bool:
        return self.pending is not None or self.pending_plan is not None

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.ui.status.emit("paused" if paused else "idle", "一時停止中" if paused else "待機中", [], 0.0)

    def show_learning(self) -> None:
        """覚えている言い方を表示する（トレイメニューと「何覚えてる？」から）。"""
        self._q.put(("learning", None))

    def write_learning_suggestions(self) -> str:
        """固定の規則へ移す候補を Markdown に書き出し、そのパスを返す（トレイメニューから）。"""
        if self.learner is None:
            return ""
        try:
            return str(self.learner.write_suggestions())
        except Exception as e:
            log.warning("学習の候補を書き出せません: %s", e)
            return ""

    # ---- 処理スレッド ----
    def _loop(self) -> None:
        while True:
            try:
                ev = self._q.get(timeout=0.5)
            except queue.Empty:
                self._check_timeout()
                continue
            try:
                kind = ev[0]
                if kind == "begin":
                    self.context.begin()
                    self.ui.status.emit("listening", "聞き取り中…", [], 0.0)
                elif kind == "wake_begin":
                    self.context.begin()
                    self.ui.status.emit("listening", "ウェイクワードで聞き取り中",
                                        ["話すたびに実行します", f"{self._wake_idle_sec:.0f} 秒言葉がなければ終わります",
                                         "F5 をもう一度タップしても終了"], 0.0)
                elif kind == "begin_ctx":
                    if self.context._pending is None:  # 区切りごとの画面情報の先読み
                        self.context.begin()
                elif kind == "audio":
                    self._handle_audio(ev[1], final=ev[2])
                elif kind == "remote_up":
                    time.sleep(0.2)
                    self._handle_audio(self._active.stop(), final=True)
                elif kind == "stop":
                    self._stop(ev[1])
                elif kind == "confirm":
                    self._resolve_confirm(ev[1], via="キー")
                elif kind == "learning":
                    self._show_learning()
                elif kind == "text":   # 動作確認用：文字で命令を入れる（debug_inbox.py から）
                    self.context.begin()
                    fg = winutil.foreground_window()
                    self._handle_text(ev[1], 0.0, self.profiles.active(fg), True)
            except Exception as e:
                log.exception("処理中にエラー")
                self.ui.status.emit("error", "エラー", [str(e)[:60]], 5.0)

    def say(self, text: str) -> None:
        """文字で命令を入れる（音声認識の代わり。動作確認用）。"""
        self._q.put(("text", text))

    def _partial_loop(self) -> None:
        """話している最中、約 1 秒ごとにここまでの音声を文字起こしして文字起こしバーに出す。"""
        interval = float(self.cfg.get("transcript.partial_interval_sec", 1.0))
        while True:
            time.sleep(interval)
            if not self._listening or (self.auto_segment and not self._seg_speech):
                continue
            audio = self._active.peek() if hasattr(self._active, "peek") else None
            if audio is None or len(audio) < 16000 * 0.6:
                continue
            try:
                text = self.stt.transcribe(audio)
            except Exception:
                log.exception("途中経過の文字起こしに失敗")
                continue
            if self._listening and text:
                self._last_partial = (text, time.monotonic(), len(audio))
                self.ui.transcript_partial.emit(text)
                # 話している最中に、確定できそうな操作を先に表示する（予測バー）
                if self.cfg.get("transcript.predict", True):
                    try:
                        win0 = winutil.foreground_window()
                        snap0 = Snapshot(win0, winutil.get_cursor(), [], [])
                        pd = self.keyword.decide(text, snap0, {}, None)
                        if fast_path(pd) and pd.action not in ("stop", "show_hints", "repeat"):
                            self.ui.status.emit("listening", "聞き取り中…", [f"→ {pd.describe({})}"], 0.0)
                    except Exception:
                        pass

    def _segment_loop(self) -> None:
        """聞き取り中、発話の終わり（無音 silence_sec 秒）を見つけたら、そこまでを 1 つの命令として処理に回す。"""
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        opts = VadOptions(min_silence_duration_ms=int(self.silence_sec * 1000), speech_pad_ms=100)
        need_silence = int(self.silence_sec * 16000)
        while True:
            time.sleep(0.1)
            if not self._listening:
                continue
            src = self._active
            audio = src.peek()
            if len(audio) < 16000 * 0.3:
                continue
            try:
                ts = get_speech_timestamps(audio, opts)
            except Exception:
                log.exception("発話区間の検出に失敗")
                continue
            if not ts:
                if len(audio) > 16000 * 4:
                    src.trim(1.0)  # 無音がたまり続けないようにする（常時聞き取り中）
                continue
            if not self._seg_speech:
                self._seg_speech = True
                if self._wake_session:
                    self._wake_last = time.monotonic()   # 話し続けている間はセッションを延長する
                self._q.put(("begin_ctx",))
            if len(audio) - ts[-1]["end"] >= need_silence and self._listening:
                seg = src.take()
                self._seg_speech = False
                self._segments_done += 1
                self._q.put(("audio", seg, False))

    def _level_loop(self) -> None:
        while True:
            time.sleep(0.06)
            if self._listening:
                self.ui.level.emit(self._active.level)

    def _check_timeout(self) -> None:
        if self.pending and time.monotonic() > self.pending[2]:
            self.pending = None
            self.ui.status.emit("idle", "確認がないため取り消しました", [], 3.0)
        if self.pending_plan and time.monotonic() > self.pending_plan[2]:
            self.pending_plan = None
            self.ui.status.emit("idle", "確認がないため取り消しました", [], 3.0)
        if self._wake_session and self._listening and time.monotonic() - self._wake_last > self._wake_idle_sec:
            log.info("ウェイクワードの聞き取りを無音で終了")
            self._end_listening()

    @staticmethod
    def _screen_has(noun: str, lookup: candidates.Lookup) -> bool:
        """名前で指したものが画面の候補に近いものを持つか（聞き返しの「ありません」表示の判定）。"""
        from . import textparse
        n = textparse.compact(noun)
        if len(n) < 2:
            return True   # 短すぎて判断できないときは余計なことを言わない
        for kind in ("element", "menu"):
            for cand in (lookup.get(kind) or {}).values():
                name = cand.data.name if kind == "element" else cand.data.path[-1]
                if textparse.similarity(n, name) >= 0.6:
                    return True
        return False

    @staticmethod
    def _can_reuse_partial(p: tuple[str, float, int] | None, audio_len: int, now: float) -> bool:
        """途中経過の認識を確定に使えるか。最新（1.5 秒以内）で、その後の追加音声が 0.5 秒以内のときだけ
        （それ以上話が続いていれば後半が欠けるので、認識し直す）。"""
        return bool(p and now - p[1] <= 1.5 and 0 < audio_len - p[2] <= 16000 * 0.5)

    @staticmethod
    def _has_speech(audio: np.ndarray) -> bool:
        if len(audio) < 16000 * 0.2:
            return False
        from faster_whisper.vad import get_speech_timestamps
        return bool(get_speech_timestamps(audio))

    def _stop(self, reason: str) -> None:
        self.pending = None
        self.pending_plan = None
        self.executor.release()
        if self.hints:
            self.hints = None
            self.ui.hints_clear.emit()
        self.ui.status.emit("done", "停止しました", [reason], 2.5)
        self.ui.transcript_result.emit("停止")

    # ---- 1 発話の処理 ----
    def _handle_audio(self, audio: np.ndarray, final: bool = True) -> None:
        """final=False は話の途切れで区切った途中の命令、True は F5 を離したときの残り。"""
        if self._wake_session:
            self._wake_last = time.monotonic()   # ウェイクのセッションは話し続ける間だけ続く
        min_len = float(self.cfg.get("audio.min_duration_sec", 0.25)) * 16000
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        log.info("録音%s: %.2f 秒, ピーク %.3f", "" if final else "（区切り）", len(audio) / 16000, peak)
        if final and self.auto_segment and self._segments_done and not self._has_speech(audio):
            # すでに区切って処理済みで、離したときの残りは無音 → 何もしない（直前の結果表示を消さない）
            self.context.collect(budget_sec=0)
            return
        dead = len(audio) == 0 or (hasattr(self._active, "alive") and not self._active.alive())
        if final and dead and self._active is self.recorder and hasattr(self.recorder, "reopen"):
            # マイクからデータが 1 つも来ていない：デバイス一覧を取り直す（リモート接続でマイク転送を有効にした場合など）
            self.context.collect(budget_sec=0)
            try:
                name = self.recorder.reopen()
                lines = [f"マイクを選び直しました：{name[:30]}"]
                if self.remote:
                    lines += ["リモート接続中は Mac のブラウザでマイクページを開いてください",
                              "（トレイ →「ブラウザマイクの URL をコピー」）"]
                self.ui.status.emit("error", "マイクから音声が届いていません", lines, 10.0)
            except Exception as e:
                self.ui.status.emit("error", "マイクから音声が届いていません", [str(e)[:50]], 6.0)
            self.ui.transcript_result.emit("マイクから音声が届いていません")
            return
        if len(audio) >= min_len and peak < 0.005:
            self.context.collect(budget_sec=0)
            self.ui.status.emit("error", "マイクに音が入っていません", ["config.yaml の audio.device を確認してください"], 5.0)
            return
        if len(audio) < min_len:
            self.context.collect(budget_sec=0)  # 先読みを捨てる
            self.ui.status.emit("idle", "待機中", [], 0.0)
            return
        self.ui.status.emit("processing", "認識中…", [], 0.0)
        fg = winutil.foreground_window()
        profile = self.profiles.active(fg)
        t0 = time.perf_counter()
        p = self._last_partial
        self._last_partial = None
        if self._can_reuse_partial(p, len(audio), time.monotonic()):
            # 話している最中の認識（文字起こしバーの表示用）がほぼ全体をカバーしているので、
            # 認識し直さずその文を確定に使う（初動が約 0.35 秒速くなる）
            text = p[0]
            log.info("途中経過の認識を確定に再利用: %s", text[:40])
        else:
            text = self.stt.transcribe(audio, profile.vocabulary if profile else None)
        stt_ms = (time.perf_counter() - t0) * 1000
        if not text:
            self.context.collect(budget_sec=0)
            self.ui.status.emit("retry", "聞き取れませんでした", ["もう一度お願いします"], 3.0)
            self.ui.transcript_final.emit("（聞き取れませんでした）")
            return
        self._handle_text(text, stt_ms, profile, final)

    def _handle_text(self, text: str, stt_ms: float = 0.0, profile=None, final: bool = True, depth: int = 0) -> None:
        """認識した文を処理する（読み替え → 停止/確認/ヒント → 判定 → 実行）。"""
        words = dict(self.dictionary)
        if profile:
            words.update(profile.dictionary)  # 前面のアプリの専門用語
        converted = textparse.apply_dictionary(text, words)
        if converted != text:
            log.info("読み替え: %s → %s", text, converted)
            text = converted
        if self._carry and time.monotonic() - self._carry[1] < 6.0:
            text = self._carry[0] + text  # 前の区切りの続き
        self._carry = None
        if not final and _looks_unfinished(text):
            # 「ツールバーからGTOトレーナーを」のように助詞で終わった → 次の区切りとつなげてから判定する
            self._carry = (text.rstrip("。、. "), time.monotonic())
            self.context.collect(budget_sec=0)
            self.ui.transcript_partial.emit(text + " …")
            self.ui.status.emit("listening", "続けてどうぞ", [text], 0.0)
            return
        log.info("認識: %s (%.0f ms)", text, stt_ms)
        self.ui.transcript_final.emit(text)

        # マウスを動かし続けている最中：「そこ」「ストップ」で止める。「そこでクリック」なら止めてクリック
        if getattr(self, "_gliding", False):
            from . import intents as _it
            stop, click = _it.is_glide_stop(text)
            if stop or textparse.is_stop(text):
                self._gliding = False
                time.sleep(0.05)
                if click:
                    self.executor.click("left")
                self.context.collect(budget_sec=0)
                self.ui.status.emit("done", "止めました" + ("（クリック）" if click else ""), [], 2.0)
                return
            c2 = textparse.compact(text)
            if re.search(r"(速く|はやく|早く)", c2):
                self._glide_speed = min(40.0, getattr(self, "_glide_speed", 8.0) * 2)
                self.context.collect(budget_sec=0)
                return
            if re.search(r"(ゆっくり|遅く|おそく)", c2):
                self._glide_speed = max(2.0, getattr(self, "_glide_speed", 8.0) / 2)
                self.context.collect(budget_sec=0)
                return
            self._gliding = False   # 別の命令が来たら動かすのをやめて、その命令を処理する
        # シャットダウン・再起動の待ち時間中の「取り消して」「やめて」→ 中止する
        armed = getattr(self, "_power_armed", 0)
        if armed and time.monotonic() - armed < 16 and (textparse.is_stop(text) or textparse.is_no(text)
                                                         or re.search(r"(取り消|キャンセル|中止)", text)):
            import subprocess
            subprocess.Popen(["shutdown", "/a"], creationflags=0x08000000)
            self._power_armed = 0
            self.context.collect(budget_sec=0)
            self.ui.status.emit("done", "中止しました", ["シャットダウン・再起動を取り消しました"], 4.0)
            return
        # 停止は何よりも優先し、Jev を通さない
        if textparse.is_stop(text):
            self.context.collect(budget_sec=0)
            self._stop(f"「{text}」")
            return
        # 下書き（AI が書いた文章）への返事：「入力して」→ 入力 /「もっと短く」→ 直す /「いらない」→ 捨てる
        dr = getattr(self, "_draft", None)
        if dr is not None and time.monotonic() - dr[2] < 300:
            from . import intents as _it
            if _it.is_draft_accept(text):
                self.context.collect(budget_sec=0)
                self._paste_draft()
                return
            if _it.is_draft_cancel(text):
                self.context.collect(budget_sec=0)
                self._draft = None
                self.ui.answer_clear.emit()
                self._reply("下書きを捨てました", [], state="idle", secs=2.0)
                return
            if _it.is_draft_refine(text):
                self.context.collect(budget_sec=0)
                self._ai_async("refine", text, dr[0], title="下書きを直しています", draft=True, hwnd=dr[1])
                return
        # 書き取りモード中は、「書き取り終わり」以外はすべてそのまま入力する
        if getattr(self, "_dictating", False):
            self.context.collect(budget_sec=0)
            self._dictate(text)
            return
        # エクスプローラーでの移動の確認への返事
        pe = getattr(self, "_pending_explorer", None)
        if pe is not None:
            self._pending_explorer = None
            if time.monotonic() - pe[2] < 10 and textparse.is_yes(text):
                from . import uiaction, winctx
                self.context.collect(budget_sec=0)
                n, dest = uiaction.run_isolated(winctx.copy_or_move, pe[1], pe[0]["dest"], True, timeout=10.0)
                self._reply(f"{n} 件を{dest}に移動しました", [])
                return
        # 電源の操作（シャットダウン・再起動など）の確認への返事。「はい」以外はすべて取り消し
        pp = getattr(self, "_pending_power", None)
        if pp is not None:
            self._pending_power = None
            if time.monotonic() - pp[1] < 10 and textparse.is_yes(text):
                self.context.collect(budget_sec=0)
                self._power(pp[0])
                return
            self.ui.status.emit("idle", "取り消しました", [], 2.0)
            if textparse.is_no(text) or textparse.is_stop(text):
                self.context.collect(budget_sec=0)
                return
        # メモ全消去の確認への返事。「はい」以外はすべて取り消し
        pm = getattr(self, "_pending_memo_clear", None)
        if pm is not None:
            self._pending_memo_clear = None
            if time.monotonic() - pm < 10 and textparse.is_yes(text):
                from . import intents
                self.context.collect(budget_sec=0)
                self._reply(f"メモを {intents.memo_clear()} 件消しました", [], secs=4.0)
                return
            self.ui.status.emit("idle", "取り消しました", [], 2.0)
            if textparse.is_no(text) or textparse.is_stop(text):
                self.context.collect(budget_sec=0)
                return
        # プロセス強制終了の確認への返事。「はい」以外はすべて取り消し
        pk = getattr(self, "_pending_proc_kill", None)
        if pk is not None:
            self._pending_proc_kill = None
            if time.monotonic() - pk[2] < 10 and textparse.is_yes(text):
                self.context.collect(budget_sec=0)
                try:
                    from . import intents
                    intents.kill_process(pk[0], pk[1])
                    self._reply(f"{pk[1][:24]} を終了しました", [])
                except Exception as e:
                    self._reply("終了できませんでした", [str(e)[:50]], state="retry")
                return
            self.ui.status.emit("idle", "取り消しました", [], 2.0)
            if textparse.is_no(text) or textparse.is_stop(text):
                self.context.collect(budget_sec=0)
                return
        # 確認待ちへの返事
        if self.pending or self.pending_plan:
            self.pending_plan = None   # 新しい命令が来たら plan の確認も取り消す
            if self.pending:
                if textparse.is_yes(text):
                    self.context.collect(budget_sec=0)
                    self._resolve_confirm(True, via=f"「{text}」")
                    return
                if textparse.is_no(text):
                    self.context.collect(budget_sec=0)
                    self._resolve_confirm(False, via=f"「{text}」")
                    return
                self._last_pending = self.pending[0]  # 同じ命令の言い直しなら「はい」とみなすために覚えておく
                self.pending = None  # 別の命令が来たら確認は取り消して新しい命令として扱う
        # ヒント表示中の番号・クリック
        if self.hints and self._handle_hint_utterance(text):
            self.context.collect(budget_sec=0)
            return
        if self.hints:
            self.hints = None
            self.ui.hints_clear.emit()

        # 裸の否定（「違う」「違います」）：確認も候補もないときは、取り消しの意思表示として静かに受ける
        if not (self.pending or self.pending_plan) and textparse.is_denial(text):
            self.context.collect(budget_sec=0)
            if self._last_pending is not None:
                self._last_pending = None
                self._reply("取り消しました", [f"聞き取り：{text}"], secs=2.0)
            else:
                self.ui.status.emit("idle", "待機中", [f"聞き取り：{text}", "直前に確認はありません"], 2.5)
                self.ui.transcript_result.emit(f"聞き取り：{text}（直前に確認はありません）")
            return

        # 直前の操作の続き（「続き」「さっきのページ」「さっきのアプリ」）は決まった処理で済ませる
        if self._handle_followup(text):
            self.context.collect(budget_sec=0)
            return

        # 「アプリは Obsidian です」「X です」＝直前の「〇〇を開いて」の対象の言い直し → 正しい名前で開き直す
        fixed = _correction(text, getattr(self, "_last_open", None))
        if fixed is not None and depth == 0:
            log.info("言い直しとして実行: %s → %s", text, fixed)
            self._last_open = None
            self._handle_text(fixed, stt_ms, profile, True)   # 直した文は「〇〇です」の形ではないので再帰しない
            return
        if _OPEN_VERB.search(textparse.compact(text)):
            self._last_open = (text, time.monotonic())

        # ダイアログ（保存確認・メッセージボックスなど）への返事：「はい」「保存しない」「キャンセル」→ そのボタンを押す
        if len(textparse.compact(text)) <= 16 and hasattr(self, "_answer_dialog") and self._answer_dialog(text):
            self.context.collect(budget_sec=0)
            return
        # 数値・名前・時間つきの決まった形の命令（音量30・3番目のタブ・〇〇というファイル・タイマーなど）
        from . import intents
        it = intents.parse(text, getattr(self, "routines", None), getattr(self, "layouts", None))
        if it is not None and it.kind in ("explorer", "zip_selection", "unzip_selection", "checksum_selection"):
            fgx = winutil.foreground_window()
            if not fgx or fgx.process.lower() != "explorer.exe" or fgx.title in ("", "Program Manager"):
                it = None   # エクスプローラーが前面でなければ通常の判定へ（「一つ上」はスクロールなどの意味もある）
        if it is not None and it.kind in ("window_named", "paste_to") and intents.find_window(it.args["name"]) is None:
            it = None   # 開いているウィンドウの名前でなければ（「サイドバーを閉じて」など）アプリ内の操作として判定する
        if it is not None:
            self.context.collect(budget_sec=0)
            self._current_text = text
            self._run_intent(it, text, stt_ms)
            return
        # 回数つき（「3回戻って」「下に5回スクロール」）→ 残りを判定して実行し、同じ操作を繰り返す
        cnt = intents.count_prefix(text) if depth == 0 else None
        if cnt is not None:
            self._repeat_n(cnt[0], cnt[1], stt_ms, profile)
            return

        # 複数の手順が必要な目的（「YouTube でリュウジの動画」「〇〇を検索して」など）は、手順のひな形で実行する
        from . import tasks
        fg_now = winutil.foreground_window()
        task = tasks.parse(text, fg_now.title if fg_now else "")
        parts = _split_chain(text)
        # 全体で検索語がきれいに取れたときだけ全体をひな形として使う（「Brave を開いて、YouTube で…」は分ける）
        # デスクトップ版がインストール済みのサービス（Notion・Spotify など）は、ブラウザの指定がなければアプリを起動する
        if (task is not None and task.kind == "open_site" and not task.browser and task.site in tasks.APP_LIKE
                and self.catalog.find(tasks.SITES[task.site][0])):
            task = None
        # 「Notion を開いてログインして」のように、サイトを開いた後にも手順が続くときは分けて順に実行する
        if task is not None and len(parts) > 1 and task.kind in ("open_site", "open_web", "open_location"):
            task = None
        if task is not None and not (len(parts) > 1 and re.search(r"(開いて|起動|入力)", task.query)):
            self.context.collect(budget_sec=0)
            self._run_task(task, text)
            return
        # 前に覚えた言い方なら、Jev も LLM も通さずにそのまま実行する（分けるより前に見る：
        # 覚えた手順のほうが、機械的に分けた実行より正確なため）
        learned = self.learner.lookup(text, fg_now.process.lower() if fg_now and fg_now.process else "") \
            if self.learner is not None else None
        if learned is not None:
            self.context.collect(budget_sec=0)
            self._run_learned(learned, text, stt_ms)
            return
        # つながった命令（「〇〇してから△△して」）は分けて順に実行する
        if len(parts) > 1 and depth == 0:
            self.context.collect(budget_sec=0)
            log.info("命令を分けて順に実行: %s", " ／ ".join(parts))
            for i, part in enumerate(parts):
                if self.agent.abort:   # Pause キーなどで止められたら、残りは「続き」に残す
                    self._last_chain = (parts, i, time.monotonic())
                    self.ui.status.emit("idle", "途中で止めました",
                                        [f"全 {len(parts)} 手のうち {i} 手まで実行",
                                         "「続き」と言うと残りを実行します"], 6.0)
                    return
                if i:
                    time.sleep(1.2)  # 前の操作の結果（画面の切り替わりなど）を待つ
                self._handle_text(part, stt_ms, winutil_profile(self), True, depth=1)
            self._last_chain = None
            return

        # 確定の短い命令（「右」「クリックして」「左半分にして」「コピーして」）は、画面の収集（UIA・OCR）を
        # 待たずに即実行する（初動を最速にする）。画面の名前で押す言い方（「保存を押して」）は対象が必要なので、
        # これまでどおり画面を見てから判定する
        if self.cfg.get("decision.fast_path", True):
            win0 = winutil.foreground_window()
            snap0 = Snapshot(win0, winutil.get_cursor(), winutil.list_windows(), [])
            kd = self.keyword.decide(text, snap0, {}, self.previous)
            # ターミナルでのコピー/貼り付けは Ctrl+C 中断の危険があるため、状況を見てから判定する
            terminal = bool(win0 and win0.process.lower() in ("windowsterminal.exe", "conhost.exe"))
            if fast_path(kd) and kd.action not in ("stop", "show_hints") \
                    and not (kd.action == "key_combo" and _FAST_VERB.search(textparse.compact(text))) \
                    and not (terminal and kd.params.get("key") in ("copy", "paste")):
                kd.engine = "keyword(即実行)"
                log.info("画面を見ずに即実行: %s → %s", text, kd.action)
                self._act(kd, {}, text, snap0, stt_ms)
                return

        snap = self.context.collect()
        menu = self._appmap(snap.window)
        entries = None
        if menu:
            aliases = profile.menu_aliases if profile else {}
            for e in menu.entries:  # プロファイルの日本語の言い方をメニュー項目に付ける
                e.aliases = aliases.get(" > ".join(e.path), [])
            entries = menu.entries
        ctl_alias = profile.control_aliases if profile else {}
        for el in snap.elements:
            if el.source != "uia":
                continue
            learned = menu.control_for(el.name, el.auto_id) if menu else None
            if learned:
                el.group = learned.group
            el.aliases = (ctl_alias.get(f"{el.name}#{el.auto_id}", []) + ctl_alias.get(el.name, [])
                          + (ctl_alias.get(f"#{el.auto_id}", []) if el.auto_id else []))
        lookup = candidates.build(text, snap, self.catalog.candidates(), self.limits,
                                  menu=entries, commands=profile.commands if profile else None)
        d = self._decide(text, snap, lookup)
        self._act(d, lookup, text, snap, stt_ms)

    def _decide(self, text: str, snap: Snapshot, lookup: candidates.Lookup) -> Decision:
        self._last_facts = {}
        # ターミナルではコピー/貼り付けの意味が変わるため、キーワード判定の前に窓の種類だけ確かめる
        kind = ""
        try:
            from . import winctx as _winctx
            wc = self.context.win_context(snap.window, timeout=0.6)
            if isinstance(wc, _winctx.WinContext):
                kind = wc.kind
        except Exception:
            kind = ""
        kd = self.keyword.decide(text, snap, lookup, self.previous,
                                 facts={"windows": {"window_kind": kind}} if kind else None)
        if self.engine is None or (self.cfg.get("decision.fast_path", True) and fast_path(kd)
                                   and not _names_on_screen(text, kd, lookup)):
            return kd
        facts: dict = {}
        try:
            facts = self._facts(text, lookup, kd)
            try:   # Windows の状況（窓の種類・ダイアログ・フォーカス・エクスプローラーのフォルダ）
                from . import uiaction, winctx
                wc = self.context.win_context(snap.window, timeout=0.6)   # F5 を押した瞬間に先読みしたもの
                if wc is None:   # 窓が変わるなどで先読みが使えないときは今から取る
                    wc = uiaction.run_isolated(winctx.collect, snap.window, timeout=0.6)
                if isinstance(wc, winctx.WinContext):
                    facts["windows"] = wc.to_facts()
            except Exception:
                log.debug("Windows の状況を取れません", exc_info=True)
            self._last_facts = facts   # LLM フォールバックで使い回す
            d = self.engine.decide(text, snap, lookup, self.previous, facts=facts, recent=list(self._history),
                                  session=self.session_state())
        except Exception as e:
            log.warning("Jev の判定に失敗: %s", e)
            if not self.cfg.get("decision.fallback_to_keyword", True):
                raise
            kd.engine = "keyword(fallback)"
            return kd
        # 今のアプリのメニュー・独自コマンドにはっきり当てはまるなら、Jev が別の操作（別アプリの起動など）を
        # 低い確信度で選んでいてもアプリ内の機能を優先する
        if kd.action in ("menu_command", "app_command", "click_element") and kd.confidence >= 0.9 \
                and d.action != kd.action and d.confidence < 0.8:
            kd.engine = "keyword(アプリ内優先)"
            return kd
        # 「設定を開いて」で画面に「設定」ボタンがあるのに、Jev が別アプリの起動やキー操作を迷いながら選んだ
        # → 今のアプリの同じ名前のボタンを押す（アプリ内を優先する方針）
        if d.action in ("launch_app", "key_combo", "unknown") and d.confidence < 0.8:
            hit = _element_named(text, lookup)
            if hit is not None:
                return Decision("click_element", {"element": hit, "button": "left"}, 0.85,
                                engine="keyword(画面の同名ボタン優先)", is_pc_command=d.is_pc_command, supported=True)
        # コード側の照合が Jev と同じ対象を強く支持しているか（確認を省いてよいかの判断に使う）
        for kind, key in (("menu_command", "best_menu_matches"), ("click_element", "best_element_matches")):
            best = facts.get(key) or []
            param = "menu" if kind == "menu_command" else "element"
            if d.action == kind and best and best[0]["option"] == d.params.get(param) and best[0]["match"] >= 1.0:
                d.supported = True
        # 独立した 2 つの方法（Jev とキーワード照合）が同じ結論なら、確度は高いとみなす
        if kd.action == d.action and kd.action != "unknown" and kd.confidence >= 0.9 \
                and all(d.params.get(k) == v for k, v in kd.params.items()):
            d.confidence = max(d.confidence, 0.85)
            d.supported = True
            d.engine = "jev+keyword"
        if d.action == "type_text":
            d.text = textparse.extract_text(text) or kd.text
            if not d.text:
                d.confidence = 0.0
        # 「もうちょい」など：方向が曖昧で前回の続きなら、前回の方向を引き継ぐ
        if d.action in ("mouse_move", "scroll") and self.previous and self.previous.action == d.action \
                and d.is_continuation > 0.6 and d.detail.get("direction", 1.0) < 0.6:
            d.params["direction"] = self.previous.params.get("direction", d.params.get("direction"))
        return d

    def _facts(self, text: str, lookup: candidates.Lookup, kd: Decision) -> dict:
        """照合などをコードで計算し、型の決まった事実として Jev に渡す（typesafe-mario の方式）。
        Jev の判断をコードで上書きするのではなく、判断材料を増やすためのもの。"""
        from . import phonetic
        c = textparse.compact(text)
        facts: dict = {}

        # カタカナ語がどの英単語に聞こえるか（候補に出てくる英単語の中から探す）
        vocab: set[str] = set()
        for cand in (lookup.get("menu") or {}).values():
            vocab.update(phonetic.content_words(" ".join(cand.data.path)))
        for cand in (lookup.get("element") or {}).values():
            vocab.update(phonetic.content_words(cand.data.name))
        for cand in list((lookup.get("app") or {}).values())[:60]:
            vocab.update(phonetic.content_words(cand.data["name"]))
        skel = {w: phonetic.skeleton_en(w) for w in vocab}
        kana_en = {}
        for k in dict.fromkeys(re.findall(r"[ァ-ヶー]{3,}", text)):
            sk = phonetic.skeleton_kana(k)
            if len(sk) < 2:
                continue
            exact = [w for w, s in skel.items() if s == sk]
            if exact:
                kana_en[k] = exact[0]
                continue
            parts = sorted(((sk.find(s), w) for w, s in skel.items() if len(s) >= 2 and s in sk), key=lambda x: x[0])
            if parts and sum(len(skel[w]) for _, w in parts) >= len(sk) * 0.7:
                kana_en[k] = " ".join(w for _, w in parts)
        if kana_en:
            facts["katakana_english"] = kana_en

        def top3(kind, score_fn, label_fn):
            scored = sorted(((score_fn(cand), cid, cand) for cid, cand in (lookup.get(kind) or {}).items()),
                            key=lambda x: -x[0])[:3]
            return [{"option": cid, "item": label_fn(cand), "match": round(s, 2)} for s, cid, cand in scored if s >= 0.3]

        def alias_hit(aliases):
            return 2.0 if any(len(textparse.compact(a)) >= 2 and textparse.compact(a) in c for a in aliases or []) else 0.0

        m = top3("menu", lambda cd: max(alias_hit(cd.data.aliases), phonetic.menu_match(text, cd.data.path),
                                        phonetic.ja_menu_match(text, cd.data.path),
                                        textparse.similarity(text, cd.data.path[-1])), lambda cd: " > ".join(cd.data.path))
        if m:
            facts["best_menu_matches"] = m
        e = top3("element", lambda cd: max(alias_hit(cd.data.aliases), phonetic.match_ratio(text, cd.data.name),
                                           phonetic.ja_ratio(text, cd.data.name),
                                           textparse.similarity(text, cd.data.name)), lambda cd: cd.data.name)
        if e:
            facts["best_element_matches"] = e
        a = top3("app", lambda cd: textparse.similarity(text, cd.label), lambda cd: cd.data["name"])
        if a:
            facts["best_app_matches"] = a
        facts["verbs"] = {
            "launch": bool(re.search(r"(起動|立ち上げ|開いて|ひらいて)", c)),
            "click": bool(re.search(r"(押して|押す|クリック|タップ|選んで|選択)", c)),
            "show": bool(re.search(r"(表示|見せて|みせて|出して)", c)),
            "type": bool(re.search(r"(と入力|って打|と打|って入力|と書いて)", c)),
            "move_or_scroll": bool(re.search(r"(右|左|上|下|スクロール)", c)),
        }
        if kd.action != "unknown":
            facts["keyword_guess"] = {"action": kd.action, "target": kd.describe(lookup),
                                      "confidence": round(kd.confidence, 2)}
        return facts

    def _agent_view(self, goal: str):
        """エージェントの 1 手分の画面の読み取り（毎手、最新の画面で候補と事実を作り直す）。"""
        self.context.begin()
        snap = self.context.collect(budget_sec=1.5)
        profile = self.profiles.active(snap.window)
        menu = self._appmap(snap.window)
        entries = None
        if menu:
            aliases = profile.menu_aliases if profile else {}
            for e in menu.entries:
                e.aliases = aliases.get(" > ".join(e.path), [])
            entries = menu.entries
        ctl_alias = profile.control_aliases if profile else {}
        for el in snap.elements:
            if el.source == "uia":
                learned = menu.control_for(el.name, el.auto_id) if menu else None
                if learned:
                    el.group = learned.group
                el.aliases = (ctl_alias.get(f"{el.name}#{el.auto_id}", []) + ctl_alias.get(el.name, [])
                              + (ctl_alias.get(f"#{el.auto_id}", []) if el.auto_id else []))
        lookup = candidates.build(goal, snap, self.catalog.candidates(), self.limits,
                                  menu=entries, commands=profile.commands if profile else None)
        facts = self._facts(goal, lookup, Decision("unknown"))
        return snap, lookup, facts

    def _run_agent(self, goal: str) -> None:
        self._current_text = goal
        log.info("エージェントで目的を進める: %s", goal)

        def status(n, label):
            self.ui.status.emit("processing", f"手順 {n}: {label[:40]}", [f"目的：{goal}", "Pause キーで中断"], 0.0)
            self.ui.transcript_result.emit(f"手順 {n}: {label[:50]}")

        self.ui.status.emit("processing", "手順を考えています…", [f"目的：{goal}", "Pause キーで中断"], 0.0)
        try:
            r = self.agent.run(goal, status)
        except Exception as e:
            log.exception("エージェントでエラー")
            self.ui.status.emit("error", "エラーで止まりました", [str(e)[:60]], 5.0)
            return
        done = f"{len(r.steps)} 手"
        if r.success and r.replay:
            # 「これ覚えて」で、この手順をルーチンとして保存できるようにする（次から同じ言い方で即再生）
            self._last_agent = (goal, r.replay, time.monotonic())
            done += "・うまくいったら「これ覚えて」"
        self.ui.status.emit("done" if r.success else "retry", r.reason, [f"目的：{goal}", done], 6.0)
        self.ui.transcript_result.emit(f"{r.reason}（{done}）")
        self._remember(f"エージェント: {goal}", "done" if r.success else r.reason)
        self._log(goal, Decision("agent", {}, 1.0 if r.success else 0.0, engine="agent"),
                  " → ".join(s.label for s in r.steps) + f" ｜ {r.reason}", 0, Snapshot(winutil.foreground_window(), (0, 0)))

    def _run_task(self, task, text: str) -> None:
        from . import tasks
        desc = task.describe()
        self._current_text = text
        self.ui.status.emit("processing", desc + "…", [f"聞き取り：{text}"], 0.0)
        info: dict = {}
        try:
            result = tasks.run(task, info)
        except Exception as e:
            log.exception("手順の実行に失敗")
            self.ui.status.emit("error", "実行できませんでした", [desc, str(e)[:50]], 4.0)
            self.ui.transcript_result.emit(f"実行できませんでした（{e}）")
            self._remember(desc, f"failed: {e}")
            return
        self._note_task(task, info)
        log.info("目的を実行: %s → %s", desc, result)
        self.ui.status.emit("done", result, [f"聞き取り：{text}"], 4.0)
        self.ui.transcript_result.emit(result)
        self._remember(desc, "done")

    # ---- 直前の操作の文脈（「続き」「さっきのページ」用） ----
    def _note_task(self, task, info: dict) -> None:
        """サイト・検索語・開いた URL を覚える（次の発話で「さっきの」として使えるようにする）。"""
        from . import tasks
        if task.kind == "open_location":
            self.session.update({"last_location": tasks.LOCATIONS[task.site][0],
                                 "last_target": tasks.LOCATIONS[task.site][0]})
            return
        self.session.update({"last_site": tasks.SITES[task.site][0] if task.site in tasks.SITES else task.site})
        if task.query:
            self.session["last_query"] = task.query
        if info.get("url"):
            self.session["last_url"] = info["url"]
        self.session["last_target"] = task.describe()

    def _note_action(self, d: Decision, lookup: candidates.Lookup) -> None:
        """実行できた操作の対象を覚える（「さっきのアプリ」「さっきのページ」用）。"""
        desc = d.describe(lookup)
        self.session["last_target"] = desc
        self.session["last_action"] = d.action
        if d.action in ("launch_app", "switch_window", "snap_window", "window_control"):
            cid = d.params.get("app") or d.params.get("window")
            cand = (lookup.get("app") if d.params.get("app") else lookup.get("window", {})).get(cid)
            if cand is not None:
                name = cand.data["name"] if isinstance(cand.data, dict) else cand.label
                self.session["last_app"] = name.split(" — ")[0]

    def session_state(self) -> dict:
        """Jev / LLM に渡す、直前の操作の要約（指示語の解決に使う）。"""
        keys = ("last_app", "last_site", "last_query", "last_url", "last_location", "last_target")
        return {k: v for k, v in self.session.items() if k in keys and v}

    _PLAN_TTL = 300.0   # 「続き」で再開できる時間（秒）

    # ---- 決まった形の命令（intents.py） ----
    _REPEATABLE = {"key_combo", "scroll", "mouse_move", "click"}

    def _repeat_n(self, n: int, rest: str, stt_ms: float, profile) -> None:
        """「3回戻って」：「戻って」を 1 回判定・実行し、同じ操作をあと n-1 回繰り返す。"""
        before = self.previous
        self._handle_text(rest, stt_ms, profile, True, depth=1)
        d = self.previous
        if d is None or d is before or d.action not in self._REPEATABLE:
            return   # 実行されなかった・繰り返すと危ない操作（起動など）は 1 回だけ
        for i in range(n - 1):
            if self.agent.abort:
                break
            time.sleep(0.12)
            if not self._execute(d, self.previous_lookup, [f"{i + 2}/{n} 回目"]):
                break
        desc = d.describe(self.previous_lookup)
        self.ui.transcript_result.emit(f"{desc} × {n} 回")
        self.ui.status.emit("done", f"{desc} × {n} 回", [f"聞き取り：{rest}"], 3.0)

    def _reply(self, title: str, lines: list[str] | None = None, speak: str | None = None,
               state: str = "done", secs: float = 4.0) -> None:
        self.ui.status.emit(state, title, lines or [], secs)
        self.ui.transcript_result.emit(title)
        if speak:
            self.speaker.say(speak)

    def _dictate(self, text: str) -> None:
        from . import intents
        if intents.dictation_end(text) or textparse.is_stop(text):
            self._dictating = False
            self._reply("書き取りを終わりました", ["通常の命令に戻ります"], speak="書き取りを終わります")
            return
        c = textparse.compact(text).rstrip("。")
        # 書き取りを始めたウィンドウ以外には入力しない（途中で別のアプリが前面に来ても誤入力しない）
        target = getattr(self, "_dictate_hwnd", 0)
        fg = winutil.foreground_window()
        if target and (fg is None or fg.hwnd != target):
            if re.fullmatch(r"(ここに|このウィンドウに|こっちに)(書いて|入力して|切り替えて)", c) and fg:
                self._dictate_hwnd = fg.hwnd
                self._reply("書き取り先を切り替えました", [fg.title[:40]], state="listening", secs=0)
                return
            log.info("書き取り先のウィンドウが前面にないため入力しません: %s", text)
            self._reply("入力を止めています", ["書き取りを始めたウィンドウが前面にありません",
                                            "「ここに書いて」で今のウィンドウに切り替え"], state="retry", secs=0)
            return
        if re.fullmatch(r"(改行|かいぎょう|次の行)(して)?", c):
            winutil.press_combo([0x0D])
            self._reply("改行", ["書き取り中（「書き取り終わり」で終了）"], state="listening", secs=0)
            return
        if re.fullmatch(r"(一文字|1文字)(消して|戻して)", c):
            winutil.press_combo([0x08])
            return
        if re.fullmatch(r"(今の|いまの)(文|ぶん)?(を)?(取り消して|消して|けして|元に戻して)", c):
            winutil.press_combo([0x11, 0x5A])
            self._reply("直前の入力を取り消しました", ["書き取り中"], state="listening", secs=0)
            return
        out = re.sub(r"(改行|かいぎょう)(して)?", "\n", text)
        winutil.type_unicode(out)
        self._reply(f"入力: {text[:40]}", ["書き取り中（「書き取り終わり」で終了）"], state="listening", secs=0)

    def _run_intent(self, it, text: str, stt_ms: float) -> None:
        from . import intents
        heard = f"聞き取り：{text}"
        k, a = it.kind, it.args
        log.info("決まった形の命令: %s %s", k, a)
        self._log(text, Decision(k, {kk: str(v) for kk, v in a.items()}, 1.0, engine="intent"), k, stt_ms, None)
        try:
            if k.startswith("ai_"):
                self._run_ai_intent(k, a, text)
                return
            if k == "glide":
                self._start_glide(a["direction"], bool(a.get("slow")))
                return
            if k == "explorer":
                self._explorer_intent(a, heard)
                return
            if k == "help":
                self._reply("できること", intents.HELP, speak="できることを表示しました", secs=15.0)
            elif k == "demo_start":
                if self.demo.active:
                    self._reply("すでに記録中です", [f"「{self.demo.name}」", "「記録終了」で保存します"], state="retry")
                    return
                name = a["name"] or time.strftime("手順%H%M")
                self.demo.start(name)
                self._reply(f"記録中: {name}", ["クリックと声の命令を記録します（キー入力は記録しません）",
                                              "「記録終了」で保存・「記録取り消し」で破棄"],
                            speak="記録を始めます", state="listening", secs=0)
            elif k == "demo_stop":
                if not self.demo.active:
                    self._reply("記録していません", ["「〇〇の手順を記録して」で始めます"], state="retry")
                    return
                name = self.demo.name
                steps = self.demo.stop()
                if not steps:
                    self._reply("記録された操作がありません", [name], state="retry")
                    return
                from . import demo as demo_mod
                intents.save_user_routine(name, steps)
                self.routines = intents.load_routines(self.cfg.get("routines", {}))
                self._reply(f"「{name}」を保存しました（{len(steps)} 手）",
                            [demo_mod.describe_step(st)[:40] for st in steps[:3]] + [f"「{name}」と言うと再生します"],
                            speak=f"{name}を保存しました", secs=8.0)
            elif k == "demo_cancel":
                was = self.demo.active
                self.demo.stop()
                self._reply("記録を破棄しました" if was else "記録していません", [], state="idle" if was else "retry")
            elif k == "dictation_on":
                self._dictating = True
                fg = winutil.foreground_window()
                self._dictate_hwnd = fg.hwnd if fg else 0   # 書き取り先（別のウィンドウには入力しない）
                self._reply("書き取りモード", ["話した内容をそのまま入力します", "「改行」「今の消して」「書き取り終わり」"],
                            speak="書き取りを始めます", state="listening", secs=0)
            elif k == "volume_set":
                v = intents.volume(level=int(a["level"]))
                self._reply(f"音量 {v}%", [heard])
            elif k == "volume_add":
                v = intents.volume(delta=int(a["delta"]))
                self._reply(f"音量 {v}%", [heard])
            elif k == "volume_get":
                v = intents.volume()
                self._reply(f"今の音量は {v}%", [heard], speak=f"音量は{v}パーセントです")
            elif k == "tab":
                intents.switch_tab(int(a["n"]))
                self._reply("最後のタブ" if a["n"] == 9 else f"{a['n']} 番目のタブ", [heard])
            elif k == "seek":
                fg = winutil.foreground_window()
                self._reply(intents.seek(int(a["seconds"]), fg.process if fg else ""), [heard])
            elif k == "find_in_page":
                intents.find_in_page(a["q"])
                self._reply(f"ページ内で「{a['q']}」を検索", [heard, "次の一致は「次を検索」"])
            elif k in ("open_file", "recent_file"):
                self._open_file_intent(k, a, heard)
            elif k == "arrange":
                self._arrange_intent(a["left"], a["right"], heard, bool(a.get("vertical")))
            elif k == "window_named":
                w = intents.find_window(a["name"])
                if w is None:
                    self._reply(f"「{a['name']}」のウィンドウが見つかりません", [heard], state="retry")
                    return
                if a["action"] == "focus":
                    winutil.focus_window(w.hwnd)
                else:
                    winutil.window_command(w.hwnd, a["action"])
                verb = {"close": "閉じました", "minimize": "最小化しました", "maximize": "最大化しました",
                        "focus": "前面に出しました"}[a["action"]]
                self.session["last_app"] = w.title.split(" - ")[-1]
                self._reply(f"「{w.title[:30]}」を{verb}", [heard])
            elif k == "say_time":
                t = intents.say_time()
                self._reply(t, [heard], speak=t, secs=5.0)
            elif k == "say_date":
                t = intents.say_date()
                self._reply(t, [heard], speak=t, secs=5.0)
            elif k == "calc":
                v = intents.calc(a["expr"])
                self._reply(f"{a['expr']} = {v}", [heard], speak=f"{v.replace(',', '')}です", secs=8.0)
                self.session["last_calc"] = v.replace(",", "")
            elif k == "timer":
                self._start_timer(int(a["seconds"]), text)
            elif k == "read_selection":
                t = intents.copy_selection().strip()
                if not t:
                    self._reply("選択された文字がありません", [heard], state="retry")
                    return
                self._reply(f"読み上げ中: {t[:30]}", ["「黙って」で止めます"], speak=t)
            elif k == "read_clipboard":
                t = intents.read_clipboard().strip()
                self._reply(f"読み上げ中: {t[:30]}" if t else "クリップボードは空です", [], speak=t or None)
            elif k == "hush":
                if getattr(self.speaker, "busy", False):
                    self.speaker.hush()
                    self._reply("読み上げを止めました", [], secs=2.0)
                else:   # 読み上げていなければ、PC の音を消す
                    winutil.press_combo([0xAD])
                    self._reply("消音しました", ["もう一度「ミュート」で解除"], secs=3.0)
            elif k == "tab_rel":
                n = int(a["n"])
                keys = [0x11, 0x09] if n > 0 else [0x11, 0x10, 0x09]
                for _ in range(abs(n)):
                    winutil.press_combo(keys)
                    time.sleep(0.05)
                self._reply(f"{abs(n)} つ{'先' if n > 0 else '前'}のタブ", [heard])
            elif k == "paste_to":
                w = intents.find_window(a["name"])
                winutil.focus_window(w.hwnd)
                time.sleep(0.35)
                fg = winutil.foreground_window()
                if not fg or fg.hwnd != w.hwnd:
                    self._reply(f"「{a['name']}」を前面にできませんでした", [heard], state="retry")
                    return
                winutil.press_combo([0x11, 0x56])
                self._reply(f"「{w.title[:24]}」に貼り付けました", [heard])
            elif k == "settings_page":
                os.startfile(f"ms-settings:{a['page']}")
                self._reply(f"{a['label']} の設定を開きました", [heard, "切り替えは開いた画面で行ってください"])
            elif k == "line_select":
                intents.select_line()
                self._reply("行を選択しました", [heard])
            elif k == "line_delete":
                intents.delete_line()
                self._reply("行を消しました", [heard, "「元に戻す」で取り消せます"])
            elif k == "search_selection":
                t = intents.copy_selection().strip()
                if not t:
                    self._reply("選択された文字がありません", [heard], state="retry")
                    return
                t = t[:80]
                from . import tasks
                task = tasks.Task("search", "google", t, "")
                info: dict = {}
                result = tasks.run(task, info)
                self._note_task(task, info)
                self._reply(result, [f"選択した文字: {t[:40]}"], secs=5.0)
            elif k == "open_latest_download":
                got = intents.latest_download()
                if got is None:
                    self._reply("ダウンロードフォルダにファイルがありません", [heard], state="retry")
                    return
                path, mt = got
                os.startfile(path)
                age = time.time() - mt
                when = (f"{int(age // 60)} 分前" if age < 3600
                        else f"{int(age // 3600)} 時間前" if age < 86400 else f"{int(age // 86400)} 日前")
                self._reply("ダウンロードの最新のファイルを開きました",
                            [os.path.basename(path)[:50], f"（{when}）"], secs=6.0)
            elif k == "memo_add":
                ts = intents.memo_add(a["q"])
                self._reply(f"メモしました: {a['q'][:40]}", [ts, "「メモ見せて」「メモ読んで」で確認できます"],
                            speak="メモしました", secs=5.0)
            elif k == "memo_list":
                lines = intents.memo_lines()
                if not lines:
                    self._reply("メモはまだありません", ["「メモっておいて〇〇」で追加できます"])
                    return
                shown = [ln.split("\t", 1)[-1][:36] for ln in lines[-5:]][::-1]
                self._reply(f"メモ {len(lines)} 件（新しい順）", shown, secs=10.0)
            elif k == "memo_read":
                lines = intents.memo_lines()
                if not lines:
                    self._reply("メモはまだありません", [])
                    return
                body = "。".join(ln.split("\t", 1)[-1] for ln in lines[-3:])[:400]
                self._reply(f"メモを読み上げます（新しい方から {min(3, len(lines))} 件）",
                            [ln.split("\t", 1)[-1][:36] for ln in lines[-3:]][::-1], speak=body, secs=10.0)
            elif k == "memo_clear":
                self._pending_memo_clear = time.monotonic()
                self._reply("メモをすべて消しますか？", ["「はい」で消します（10 秒以内）", "ほかの言葉で取り消し"],
                            state="confirm", speak="メモをすべて消しますか", secs=10.0)
            elif k == "zip_selection":
                from . import uiaction, winctx
                fg = winutil.foreground_window()
                paths = uiaction.run_isolated(winctx.selected_paths, fg.hwnd, timeout=3.0) if fg else []
                self._reply(intents.zip_files(paths or []), [heard, "エクスプローラーに現れます"], secs=6.0)
            elif k == "unzip_selection":
                from . import uiaction, winctx
                fg = winutil.foreground_window()
                paths = uiaction.run_isolated(winctx.selected_paths, fg.hwnd, timeout=3.0) if fg else []
                zips = [p for p in (paths or []) if p.lower().endswith(".zip")]
                if not zips:
                    self._reply("zip を選んでから言ってください", [heard], state="retry")
                    return
                lines = [intents.unzip_file(zp) for zp in zips[:3]]
                self._reply("展開しました", lines, secs=6.0)
            elif k == "checksum_selection":
                from . import uiaction, winctx
                fg = winutil.foreground_window()
                paths = uiaction.run_isolated(winctx.selected_paths, fg.hwnd, timeout=3.0) if fg else []
                files = [p for p in (paths or []) if os.path.isfile(p)]
                if not files:
                    self._reply("ファイルを選んでから言ってください", [heard], state="retry")
                    return
                name = os.path.basename(files[0])
                digest = intents.sha256_file(files[0])
                self._reply(f"SHA256: {digest[:32]}…", [f"{name[:40]}", "（先頭 32 桁。全文はクリップボードにも入ります）"], secs=10.0)
                try:
                    import win32clipboard as cb
                    cb.OpenClipboard()
                    try:
                        cb.EmptyClipboard()
                        cb.SetClipboardData(cb.CF_UNICODETEXT, f"{digest}  {name}")
                    finally:
                        cb.CloseClipboard()
                except Exception:
                    pass
            elif k == "shot_clipboard":
                self.ui.copy_screen.emit()
                self._reply("画面をクリップボードに撮りました", ["「貼り付け」で貼れます"], secs=4.0)
            elif k == "app_volume":
                self._app_volume(a, heard)
            elif k == "proc_top":
                rows = intents.proc_top(a.get("by", "memory"))
                if not rows:
                    self._reply("目立って重いプロセスはありません", [heard])
                    return
                self._last_proc_top = rows   # 「1番を止めて」用（確認してから止める）
                unit = "MB" if a.get("by", "memory") == "memory" else "%"
                self._reply(f"重いプロセス トップ {len(rows)}",
                            [f"{i + 1}. {n[:20]} {v}{unit}" for i, (n, v, _pid) in enumerate(rows)], secs=12.0)
            elif k == "proc_kill":
                rows = getattr(self, "_last_proc_top", [])
                n = int(a["n"])
                if not rows or not 1 <= n <= len(rows):
                    self._reply("止める対象がありません", ["先に「メモリ食ってるの見せて」で一覧を出してください"], state="retry")
                    return
                name, _v, pid = rows[n - 1]
                self._pending_proc_kill = (pid, name, time.monotonic())
                self._reply(f"{n} 番の {name[:24]} を強制終了しますか？",
                            ["「はい」で実行（10 秒以内）", "ほかの言葉で取り消し"],
                            state="confirm", speak=f"{name}を強制終了しますか", secs=10.0)
            elif k == "taskbar_click":
                from . import uiaction, winctx
                label = uiaction.run_isolated(winctx.taskbar_click, int(a["n"]), str(a.get("name", "")), timeout=4.0)
                self._reply(f"タスクバーの {label} を押しました", [heard])
            elif k == "read_later":
                from . import uiaction, winctx
                fg = winutil.foreground_window()
                url = title = None
                if fg and fg.process.lower() in intents._BROWSER_PROCS:
                    url = uiaction.run_isolated(winctx.browser_url, fg, timeout=3.0)
                    title = fg.title
                if not url:
                    url = self.session.get("last_url")
                    title = self.session.get("last_site", "")
                if not url:
                    self._reply("読むページが見つかりません", ["ブラウザでページを開いてから言ってください"], state="retry")
                    return
                intents.read_later_add(title or "", url)
                self._reply("後で読むに追加しました", [(title or url)[:44], "「後で読むリスト」で見られます"], secs=5.0)
            elif k == "read_later_list":
                lines = intents.read_later_lines()
                shown = [ln.split("\t", 1)[-1].replace("\t", "｜")[:52] for ln in lines[-5:]][::-1]
                self._reply(f"後で読む {len(lines)} 件", shown or ["まだありません。ブラウザで「後で読む」"], secs=8.0)
            elif k == "layout":
                self._run_layout(a["name"], heard)
            elif k == "layout_list":
                names = [f"{n}（{len(v) if isinstance(v, list) else '?'} 窓）" for n, v in self.layouts.items()]
                self._reply(f"配置 {len(names)} 種", names[:6] or ["config.yaml の layouts に登録できます"], secs=8.0)
            elif k == "alarm":
                tgt = intents.alarm_target(int(a["hour"]), int(a["minute"]), bool(a.get("pm")))
                sec = intents.alarm_seconds(int(a["hour"]), int(a["minute"]), bool(a.get("pm")))
                used_app = False
                if self.cfg.get("alarm.use_clock_app", True):
                    # Windows 標準のクロックアプリに設定する（ロック画面でも鳴る・一覧で見える・止められる）
                    from . import uiaction
                    try:
                        msg = uiaction.run_isolated(intents.set_clock_alarm, tgt, "voicectl", timeout=35.0)
                        if isinstance(msg, str) and "セットしました" in msg:
                            self._reply(msg, [f"あと約 {sec // 60} 分", "止める・消すのはクロックアプリで"],
                                        secs=8.0)
                            used_app = True
                        else:   # 応答待ちで打ち切られた（保存が済んだか分からない）
                            log.warning("クロックアプリへの設定が時間内に終わらず内部タイマーで代用")
                    except Exception as e:
                        log.warning("クロックアプリに設定できなかったため内部タイマーで代用: %s", e)
                if not used_app:
                    self._start_timer(sec, text)
            elif k == "power":
                label = {"shutdown": "シャットダウン", "restart": "再起動", "sleep": "スリープ",
                         "signout": "サインアウト"}[a["kind"]]
                self._pending_power = (a["kind"], time.monotonic())
                self._reply(f"{label}しますか？", ["「はい」で実行（10 秒以内）", "ほかの言葉で取り消し"],
                            state="confirm", speak=f"{label}しますか", secs=10.0)
            elif k == "routine_add":
                steps = _split_chain(a["body"])
                intents.save_user_routine(a["name"], steps)
                self.routines = intents.load_routines(self.cfg.get("routines", {}))
                self._reply(f"「{a['name']}」を登録しました", [f"{i + 1}. {s}" for i, s in enumerate(steps[:4])],
                            speak=f"{a['name']}を登録しました", secs=6.0)
            elif k == "routine_from_suggest":
                sg = getattr(self, "_suggest", None)
                if not sg or time.monotonic() - sg[1] > 180:
                    self._reply("登録できる提案がありません", ["「〇〇と言ったら△△して」でも登録できます"], state="retry")
                    return
                intents.save_user_routine(a["name"], sg[0])
                self.routines = intents.load_routines(self.cfg.get("routines", {}))
                self._suggest = None
                self.ui.answer_clear.emit()
                self._reply(f"「{a['name']}」を登録しました", [f"{i + 1}. {s}" for i, s in enumerate(sg[0][:4])],
                            speak=f"{a['name']}を登録しました", secs=6.0)
            elif k == "routine_del":
                intents.save_user_routine(a["name"], None)
                self.routines = intents.load_routines(self.cfg.get("routines", {}))
                self._reply(f"「{a['name']}」を削除しました", [heard])
            elif k == "routine_list":
                names = [f"{n}（{len(v)} 手）" for n, v in self.routines.items()]
                self._reply(f"ルーチン {len(names)} 件", names[:6] or ["（まだありません）"], secs=8.0)
            elif k == "routine":
                self._run_routine(a["name"], text, stt_ms)
        except Exception as e:
            log.exception("決まった形の命令の実行に失敗: %s", k)
            self._reply("実行できませんでした", [heard, str(e)[:60]], state="error")
            self._remember(k, f"failed: {e}")
            return
        self._remember(k, "done")

    # ---- Windows の状況に合わせた直接操作 ----
    def _answer_dialog(self, text: str) -> bool:
        """前面がダイアログで、発話がそのボタン（はい・いいえ・保存しない・キャンセル・次へ…）を指すなら押す。"""
        from . import uiaction, winctx
        fg = winutil.foreground_window()
        if fg is None or not winctx.is_dialog(fg.hwnd):
            return False
        try:
            buttons = uiaction.run_isolated(winctx.dialog_buttons, fg.hwnd, timeout=1.0)
        except Exception:
            return False
        if not isinstance(buttons, list) or not buttons:
            return False
        target = winctx.dialog_target(text, buttons)
        if target is None:
            return False
        try:
            winctx.press_dialog_button(fg.hwnd, target)
        except Exception as e:
            self._reply("ボタンを押せませんでした", [target, str(e)[:40]], state="error")
            return True
        log.info("ダイアログに返事: %s → %s（%s）", text, target, fg.title)
        self._log(text, Decision("dialog", {"button": target}, 1.0, engine="dialog"), f"ダイアログの「{target}」", 0, None)
        self._reply(f"「{target}」を押しました", [fg.title[:40]], secs=2.5)
        self._remember(f"ダイアログ:{target}", "done")
        return True

    def _start_glide(self, direction: str, slow: bool) -> None:
        """止めるまでマウスを滑らかに動かす（最大 12 秒・画面の端で止まる）。"""
        import threading
        vec = {"right": (1, 0), "left": (-1, 0), "up": (0, -1), "down": (0, 1), "up_right": (0.7, -0.7),
               "up_left": (-0.7, -0.7), "down_right": (0.7, 0.7), "down_left": (-0.7, 0.7)}[direction]
        self._gliding = True
        self._glide_speed = 4.0 if slow else 9.0
        label = {"right": "右", "left": "左", "up": "上", "down": "下", "up_right": "右上", "up_left": "左上",
                 "down_right": "右下", "down_left": "左下"}[direction]
        self.ui.status.emit("listening", f"{label}へ動かしています", ["「そこ」で止める ・「そこでクリック」", "「速く」「ゆっくり」"], 0.0)

        def run():
            x, y = winutil.get_cursor()
            fx, fy = float(x), float(y)
            end = time.monotonic() + 12
            vl, vt, vw, vh = winutil.virtual_screen()
            while self._gliding and time.monotonic() < end:
                fx += vec[0] * self._glide_speed
                fy += vec[1] * self._glide_speed
                nx, ny = int(max(vl, min(vl + vw - 1, fx))), int(max(vt, min(vt + vh - 1, fy)))
                winutil.set_cursor(nx, ny)
                if (nx, ny) != (int(fx), int(fy)):
                    break   # 画面の端
                time.sleep(0.016)
            if self._gliding:
                self._gliding = False
                self.ui.status.emit("idle", "止まりました", [], 2.0)

        threading.Thread(target=run, daemon=True, name="glide").start()

    def _explorer_intent(self, a: dict, heard: str) -> None:
        from . import uiaction, winctx
        fg = winutil.foreground_window()
        op = a["op"]
        if op == "up":
            winutil.press_combo([0x12, 0x26])   # Alt+↑
            self._reply("一つ上のフォルダへ", [heard])
        elif op == "sort":
            uiaction.run_isolated(winctx.explorer_sort, fg.hwnd, a["by"], bool(a["desc"]), timeout=3.0)
            label = {"date": "日付", "name": "名前", "size": "サイズ", "type": "種類"}[a["by"]]
            self._reply(f"{label}の{'新しい・大きい' if a['desc'] else ''}順に並べました", [heard])
        elif op == "open_nth":
            name = uiaction.run_isolated(winctx.explorer_open_nth, fg.hwnd, int(a["n"]), timeout=4.0)
            self._reply(f"{a['n']} 番目を開きました", [str(name)[:40]])
        elif op == "open_selected":
            names = uiaction.run_isolated(winctx.explorer_open_selected, fg.hwnd, timeout=6.0) or []
            self._reply(f"{len(names)} 件を開きました", [str(names[0])[:40] if names else ""])
        elif op in ("copy", "move"):
            paths = uiaction.run_isolated(winctx.selected_paths, fg.hwnd, timeout=3.0) or []
            if not paths:
                self._reply("選んでいる項目がありません", ["先にファイルを選んでください"], state="retry")
                return
            if op == "move":   # 移動は取り消しにくいので確認する
                self._pending_explorer = (a, fg.hwnd, time.monotonic())
                self._reply(f"{len(paths)} 件を{a['dest_name']}に移動しますか？", [paths[0][-40:], "「はい」で移動"],
                            state="confirm", secs=10.0)
                return
            n, dest = uiaction.run_isolated(winctx.copy_or_move, fg.hwnd, a["dest"], False, timeout=10.0)
            self._reply(f"{n} 件を{dest}にコピーしました", [heard])
        elif op == "copy_path":
            paths = uiaction.run_isolated(winctx.selected_paths, fg.hwnd, timeout=3.0) or []
            if not paths:
                folder = uiaction.run_isolated(winctx.explorer_state, fg.hwnd, timeout=3.0)[0]
                paths = [folder] if folder else []
            if not paths:
                self._reply("パスが分かりません", [], state="retry")
                return
            import win32clipboard as cb
            cb.OpenClipboard()
            try:
                cb.EmptyClipboard()
                cb.SetClipboardData(cb.CF_UNICODETEXT, "\r\n".join(paths))
            finally:
                cb.CloseClipboard()
            self._reply("パスをコピーしました", [paths[0][-50:]])
        elif op == "terminal":
            folder = uiaction.run_isolated(winctx.explorer_state, fg.hwnd, timeout=3.0)[0]
            winctx.open_terminal_here(folder)
            self._reply("ここでターミナルを開きました", [folder[-50:]])

    # ---- 文章アシスタント（DeepSeek） ----
    _AI_TITLES = {"summarize": "要約", "translate_en": "英語に訳しました", "translate_ja": "日本語に訳しました",
                  "polite": "丁寧に書き直しました", "casual": "くだけた言い方にしました", "shorten": "短くしました",
                  "bullets": "箇条書きにしました", "proofread": "校正しました", "explain": "説明", "reply": "返信の下書き",
                  "write": "下書き", "refine": "下書き（直しました）", "answer": "答え", "screen": "この画面について",
                  "recap": "今日の振り返り"}

    def _run_ai_intent(self, k: str, a: dict, text: str) -> None:
        if self.assistant is None:
            self._reply("文章アシスタントは無効です", ["config.yaml の assistant.enabled を true に"], state="retry")
            return
        fg = winutil.foreground_window()
        hwnd = fg.hwnd if fg else 0
        if k == "ai_write":
            self._ai_async("write", text, "", title="下書きを書いています", draft=True, hwnd=hwnd)
        elif k == "ai_ask":
            self._ai_async("answer", text, "", title="考えています", speak=True)
        elif k == "ai_recap":
            self._ai_async("recap", text, self._today_log(), title="今日の操作を振り返っています", speak=True)
        elif k == "ai_screen":
            self.context.begin()
            snap = self.context.collect(budget_sec=2.0)
            names = list(dict.fromkeys(e.name for e in snap.elements if e.name))[:300]
            screen = (f"ウィンドウ: {snap.window.title if snap.window else ''}\n" + "\n".join(names))
            self._ai_async("screen", text, screen, title="画面を読んでいます", speak=True)
        elif k == "ai_selection":
            from . import intents
            sel = intents.copy_selection().strip()
            if not sel:
                self._reply("選択された文字がありません", ["先に文字を選んでから言ってください"], state="retry")
                return
            task = a["task"]
            if task == "translate":   # 英語なら日本語へ、日本語なら英語へ
                ascii_ratio = sum(ch.isascii() for ch in sel) / max(1, len(sel))
                task = "translate_ja" if ascii_ratio > 0.7 else "translate_en"
            if task == "reply":
                self._ai_async("reply", text, sel, title="返信を考えています", draft=True, hwnd=hwnd)
            elif a.get("replace"):
                self._ai_async(task, text, sel, title="書き直しています", replace_hwnd=hwnd)
            else:
                self._ai_async(task, text, sel, title="読んでいます", speak=True)

    def _ai_async(self, task: str, request: str, text: str, title: str, speak: bool = False, draft: bool = False,
                  hwnd: int = 0, replace_hwnd: int = 0) -> None:
        """DeepSeek への依頼は数秒かかるので、別スレッドで待つ（その間も次の命令を聞ける）。"""
        if self._ai_busy:
            self._reply("前の依頼を処理中です", ["少し待ってください"], state="retry", secs=3.0)
            return
        self._ai_busy = True
        self.ui.answer.emit(title, "", "", 0.0)
        self.ui.status.emit("processing", title + "…", [f"聞き取り：{request[:40]}", "DeepSeek"], 0.0)

        def work():
            try:
                fg = winutil.foreground_window()
                out = self.assistant.ask(task, request, text, fg.title if fg else "")
            except Exception as e:
                log.warning("文章アシスタントの失敗: %s", e)
                self.ui.answer_clear.emit()
                self.ui.status.emit("error", "AI が答えられませんでした", [str(e)[:60]], 5.0)
                self._ai_busy = False
                return
            self._ai_busy = False
            head = self._AI_TITLES.get(task, "AI")
            if draft:
                self._draft = (out, hwnd, time.monotonic())
                self.ui.answer.emit(head, out, "「入力して」で入力 ・「もっと短く」などで直す ・「いらない」で捨てる", 0.0)
                self.ui.status.emit("confirm", "下書きができました", ["「入力して」で入力します"], 0.0)
                self.speaker.say("下書きができました")
            elif replace_hwnd:
                fgw = winutil.foreground_window()
                if fgw is None or fgw.hwnd != replace_hwnd:   # 書き直している間に別のウィンドウへ移っていたら入力しない
                    self._draft = (out, replace_hwnd, time.monotonic())
                    self.ui.answer.emit(head, out, "ウィンドウが変わったので置き換えていません ・「入力して」で入力", 0.0)
                    self.ui.status.emit("retry", "置き換えずに表示しています", [], 5.0)
                    return
                winutil.paste_text(out)   # 選択中の文字を置き換える（「元に戻して」で戻せる）
                self.ui.answer.emit(head, out, "選択していた文字を置き換えました ・「元に戻して」で戻せます", 20.0)
                self.ui.status.emit("done", head, ["選択していた文字を置き換えました"], 4.0)
            else:
                self.ui.answer.emit(head, out, "", max(12.0, min(60.0, len(out) * 0.25)))
                self.ui.status.emit("done", head, [out[:40]], 5.0)
                if speak:
                    self.speaker.say(out[:400])
            self._remember(f"AI:{task}", "done")

        import threading
        threading.Thread(target=work, daemon=True, name="assistant").start()

    def _paste_draft(self) -> None:
        text, hwnd, _ = self._draft
        self._draft = None
        if hwnd:
            winutil.focus_window(hwnd)
            time.sleep(0.35)
        fg = winutil.foreground_window()
        if hwnd and (fg is None or fg.hwnd != hwnd):
            self._draft = (text, hwnd, time.monotonic())
            self._reply("入力先のウィンドウを前面にできません", ["入力したい所をクリックしてから「入力して」"], state="retry")
            return
        winutil.paste_text(text)
        self.ui.answer_clear.emit()
        self._reply("入力しました", [text[:40]], secs=3.0)
        self._remember("AI:入力", "done")

    def _today_log(self, limit: int = 200) -> str:
        """今日の判定ログ（言ったこと → したこと）を時刻つきで。振り返り用。"""
        path = self._log_path
        rows = []
        try:
            for line in open(path, encoding="utf-8"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                rows.append(f"{d.get('time', '')[11:16]} 「{d.get('text', '')}」→ {d.get('desc', '')[:60]}")
        except OSError:
            return ""
        return "\n".join(rows[-limit:])

    def _power(self, kind: str) -> None:
        """電源の操作。シャットダウン・再起動は 15 秒後にして、その間は「取り消して」で止められる。"""
        import subprocess
        flags = 0x08000000   # CREATE_NO_WINDOW
        if kind == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "15"], creationflags=flags)
        elif kind == "restart":
            subprocess.Popen(["shutdown", "/r", "/t", "15"], creationflags=flags)
        elif kind == "signout":
            subprocess.Popen(["shutdown", "/l"], creationflags=flags)
        elif kind == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=flags)
        if kind in ("shutdown", "restart"):
            self._power_armed = time.monotonic()
            self._reply("15 秒後に実行します", ["「取り消して」で中止"], state="confirm", secs=15.0)

    def _open_file_intent(self, k: str, a: dict, heard: str) -> None:
        from . import intents
        if k == "recent_file":
            found = intents.recent_files(a.get("exts") or [])
            what = "最近使ったファイル"
        else:
            roots = list(self.cfg.get("files.extra_roots", []) or [])
            found = intents.search_files(a["name"], bool(a.get("folder")), extra_roots=roots)
            what = f"「{a['name']}」"
            if not found and re.search(r"[ァ-ヴー]", a["name"]):   # カタカナで言ったが英字の名前のことが多い
                eng = textparse.apply_dictionary(a["name"], dict(self.dictionary))
                if eng != a["name"]:
                    found = intents.search_files(eng, bool(a.get("folder")), extra_roots=roots)
        if not found:
            self._reply(f"{what}が見つかりません", [heard, "Windows 検索の索引と config の files.extra_roots を探しました"], state="retry")
            return
        os.startfile(found[0])
        self._bring_front(os.path.basename(found[0]))
        self.session["last_target"] = os.path.basename(found[0])
        others = [os.path.basename(p) for p in found[1:4]]
        self._reply(f"開きました: {os.path.basename(found[0])}", [os.path.dirname(found[0])[:50]]
                    + ([f"ほか: {'、'.join(others)}"] if others else []), secs=5.0)

    @staticmethod
    def _bring_front(name: str, wait: float = 3.0) -> bool:
        """開いたファイル・フォルダのウィンドウを前面に出す。すでに開いていたファイルは、開き直しても
        前面に来ないアプリ（メモ帳など）があるため、タイトルに名前を含むウィンドウを探して前面にする。"""
        stem = os.path.splitext(name)[0].lower()
        end = time.monotonic() + wait
        while time.monotonic() < end:
            fg = winutil.foreground_window()
            if fg and stem in fg.title.lower():
                return True
            win = next((w for w in winutil.list_windows() if stem in w.title.lower()), None)
            if win is not None:
                winutil.focus_window(win.hwnd)
                return True
            time.sleep(0.25)
        return False

    def _arrange_intent(self, left: str, right: str, heard: str, vertical: bool = False) -> None:
        from . import intents
        wins = []
        for name in (left, right):
            w = intents.find_window(name)
            if w is None:   # 開いていなければ起動して、ウィンドウが出るまで待つ
                self._handle_text(f"{name}を開いて", 0.0, None, True, depth=1)
                end = time.monotonic() + 10
                while w is None and time.monotonic() < end:
                    time.sleep(0.5)
                    w = intents.find_window(name)
            if w is None:
                self._reply(f"「{name}」のウィンドウが見つかりません", [heard], state="retry")
                return
            wins.append(w)
        if wins[0].hwnd == wins[1].hwnd:
            self._reply("同じウィンドウを指しています", [heard], state="retry")
            return
        intents.arrange(wins[0], wins[1], vertical)
        self._reply(f"{'上' if vertical else '左'}に{left}、{'下' if vertical else '右'}に{right}", [heard])

    def _app_volume(self, a: dict, heard: str) -> None:
        """アプリごとの音量（Windows の音量ミキサーのセッション単位）。"""
        from pycaw.pycaw import AudioUtilities
        from . import textparse
        app = str(a.get("app", ""))
        op = str(a.get("op", ""))
        if textparse.compact(app).lower() in intents._GLOBAL_VOLUME_WORDS:   # 「パソコンの音量」は全体の音量
            if op == "level":
                v = intents.volume(level=int(a["level"]))
                self._reply(f"全体の音量 {v}%", [heard])
            elif op in ("下げて", "さげて"):
                v = intents.volume(delta=-10)
                self._reply(f"全体の音量 {v}%", [heard])
            elif op in ("上げて", "あげて"):
                v = intents.volume(delta=10)
                self._reply(f"全体の音量 {v}%", [heard])
            elif op in ("消して", "けして", "ミュート"):
                winutil.press_combo([0xAD])
                self._reply("消音しました", [heard])
            elif op in ("戻して", "もとにもどして", "元に戻して"):
                intents.volume(delta=0)
                self._reply("消音を解除しました", [heard])
            elif op == "最大":
                self._reply(f"全体の音量 {intents.volume(level=100)}%", [heard])
            elif op == "ゼロ":
                self._reply(f"全体の音量 {intents.volume(level=0)}%", [heard])
            return
        sessions = [s for s in AudioUtilities.GetAllSessions() if s.SimpleAudioVolume is not None]
        matched = [s for s in sessions if intents.app_volume_match(app, s.ProcessName or "")]
        if not matched:
            self._reply(f"「{app}」の音のセッションがありません",
                        [heard, "そのアプリが音を出したことがないと調整できません"], state="retry")
            return
        lines = []
        for s in matched:
            v = s.SimpleAudioVolume
            if op == "level":
                v.SetMasterVolume(int(a["level"]) / 100.0, None)
                v.SetMute(0, None)
            elif op in ("下げて", "さげて"):
                v.SetMasterVolume(max(0.0, round(v.GetMasterVolume() - 0.1, 2)), None)
            elif op in ("上げて", "あげて"):
                v.SetMasterVolume(min(1.0, round(v.GetMasterVolume() + 0.1, 2)), None)
            elif op in ("消して", "けして", "ミュート"):
                v.SetMute(1, None)
            elif op in ("戻して", "もとにもどして", "元に戻して"):
                v.SetMute(0, None)
            elif op == "最大":
                v.SetMasterVolume(1.0, None)
                v.SetMute(0, None)
            elif op == "ゼロ":
                v.SetMasterVolume(0.0, None)
            lines.append(f"{(s.ProcessName or '?').replace('.exe', '')}: "
                         f"{'消音' if v.GetMute() else f'{int(round(v.GetMasterVolume() * 100))}%'}")
        self._reply(f"{len(matched)} 件の音を変えました", lines[:4], secs=6.0)

    def _run_layout(self, name: str, heard: str) -> None:
        """config の layouts（配置のひな形）どおりに窓を並べる。開いていなければ起動する。"""
        from . import intents
        entries = self.layouts.get(name) or []
        placed = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            app_name = str(e.get("app", "")).strip()
            place = str(e.get("place", "full")).strip()
            if not app_name:
                continue
            w = intents.find_window(app_name)
            if w is None:
                self._handle_text(f"{app_name}を開いて", 0.0, None, True, depth=1)
                end = time.monotonic() + 10
                while w is None and time.monotonic() < end:
                    time.sleep(0.5)
                    w = intents.find_window(app_name)
            if w is None:
                self._reply(f"「{app_name}」のウィンドウが見つかりません", [heard], state="retry")
                return
            placed.append((w, place, app_name))
        if not placed:
            self._reply("その配置には窓の指定がありません", [heard], state="retry")
            return
        from .schema import SNAP_RECTS
        for w, place, _n in placed:
            if place in ("maximize", "screen_wide"):
                winutil.window_command(w.hwnd, "maximize")
            elif place in SNAP_RECTS:
                winutil.place_rect(w.hwnd, place)
            else:
                log.warning("配置の場所が不明: %s", place)
        winutil.focus_window(placed[-1][0].hwnd)
        self._reply(f"{name} の配置にしました（{len(placed)} 窓）",
                    [f"{n[:16]} → {p}" for _w, p, n in placed[:3]], secs=6.0)

    def _start_timer(self, sec: int, text: str) -> None:
        import threading
        label = ((f"{sec // 3600}時間" if sec >= 3600 else "") + (f"{sec % 3600 // 60}分" if sec % 3600 >= 60 else "")
                 + (f"{sec % 60}秒" if sec % 60 else ""))

        def fire():
            try:
                import winsound
                for _ in range(3):
                    winsound.MessageBeep(0x40)
                    time.sleep(0.4)
            except Exception:
                pass
            self.ui.status.emit("done", f"{label}経ちました", [f"タイマー：「{text}」"], 15.0)
            self.ui.transcript_result.emit(f"タイマー {label} 終了")
            self.speaker.say(f"{label}経ちました")

        t = threading.Timer(sec, fire)
        t.daemon = True
        t.start()
        self._reply(f"タイマー {label}", [f"{time.strftime('%H:%M', time.localtime(time.time() + sec))} に知らせます"],
                    speak=f"{label}のタイマーをセットしました")

    def _run_routine(self, name: str, text: str, stt_ms: float) -> None:
        steps = self.routines.get(name) or []
        if self._routine_depth >= 2:
            self._reply("ルーチンの中で同じルーチンを呼んでいます", [name], state="error")
            return
        self._routine_depth += 1
        self.agent.abort = False
        try:
            self._reply(f"ルーチン「{name}」", [f"{len(steps)} 手を順に実行", "Pause キーで中断"], state="processing", secs=0)
            for i, step in enumerate(steps):
                if self.agent.abort:
                    self._reply("ルーチンを途中で止めました", [f"{i}/{len(steps)} 手まで"], state="idle")
                    return
                if i:
                    time.sleep(1.5 if isinstance(step, str) else 0.9)   # 前の手の結果（起動・画面の切り替え）を待つ
                if isinstance(step, dict):   # やって見せて記録したクリック・エージェントが成功した手順
                    from . import demo as demo_mod
                    try:
                        how = demo_mod.replay_step(step, self._run_menu)
                        log.info("記録したクリックを再生: %s（%s）", demo_mod.describe_step(step), how)
                        self.ui.status.emit("processing", f"手順 {i + 1}/{len(steps)}",
                                            [demo_mod.describe_step(step)[:40]], 0.0)
                    except Exception as e:
                        log.exception("記録したクリックの再生に失敗")
                        self._reply(f"手順 {i + 1} で止まりました", [demo_mod.describe_step(step)[:40], str(e)[:50]],
                                    state="error", secs=6.0)
                        return
                    continue
                self._handle_text(step, stt_ms, winutil_profile(self), True)
            self._reply(f"ルーチン「{name}」を実行しました", [f"{len(steps)} 手"])
        finally:
            self._routine_depth -= 1

    def _handle_followup(self, text: str) -> bool:
        """「続き」「さっきのページ」「さっきのアプリ」などを、記録した文脈から実行する。処理できたら True。"""
        kind = followup.kind(text)
        if kind is None:
            return False
        if kind == "resume":
            return self._resume_plan()
        la = getattr(self, "_last_agent", None)
        if kind == "learn" and la and time.monotonic() - la[2] < 300:   # エージェントが成功した手順をルーチンにする
            from . import intents
            from . import demo as demo_mod
            goal, steps, _ = la
            name = goal.rstrip("。．. ")
            intents.save_user_routine(name, steps)
            self.routines = intents.load_routines(self.cfg.get("routines", {}))
            self._last_agent = None
            self.ui.status.emit("done", f"手順を覚えました（{len(steps)} 手）",
                                [f"「{name}」"] + [demo_mod.describe_step(st)[:40] for st in steps[:3]]
                                + ["次から同じ言い方ですぐ再生します"], 6.0)
            self.ui.transcript_result.emit(f"「{name}」の手順を覚えました")
            return True
        if kind == "learn":   # 「これ覚えて」→ 直前に実行した言い方を覚える
            if self.learner is None:
                self.ui.status.emit("idle", "学習は無効です", ["config.yaml の learning.enabled を true にしてください"], 5.0)
                return True
            if not self._last_learnable:
                self.ui.status.emit("retry", "覚えられる操作がありません", [f"聞き取り：{text}", "先に何か実行してください"], 4.0)
                return True
            said, d, lookup = self._last_learnable
            added = self.learner.remember(said, d.action, d.params, d.text, app="", source="explicit")
            if added is None:
                self.ui.status.emit("retry", "この操作は覚えられません", [said], 4.0)
                return True
            self._learned_key = added.key
            self.ui.status.emit("done", "覚えました", [f"「{said}」", f"→ {added.describe()}",
                                                       "次からは同じ言い方でそのまま実行します"], 5.0)
            self.ui.transcript_result.emit(f"「{said}」を覚えました")
            self._note_learning()
            return True
        if kind == "forget":
            if not self._forget(text):
                self.ui.status.emit("retry", "忘れる対象がありません", [f"聞き取り：{text}"], 3.0)
            return True
        if kind == "knowledge":
            self._show_learning()
            return True
        if kind == "again":
            if self._last_was_plan and self._last_plan:
                steps, lookup, _, _, _ = self._last_plan
                self._exec_plan(steps, lookup, text, 0, start=0)
                return True
            if self.previous is None:
                self.ui.status.emit("retry", "繰り返す操作がありません", [f"聞き取り：{text}"], 3.0)
                return True
            desc = self.previous.describe(self.previous_lookup)
            self.ui.status.emit("processing", f"{desc}…", ["直前の操作をもう一度"], 0.0)
            self._execute(self.previous, self.previous_lookup, [f"聞き取り：{text}", "直前の操作"])
            return True
        # 「さっきのページ」「それ開いて」→ 前に開いた場所を開き直す
        if kind in ("page", "that"):
            if self.session.get("last_url"):
                return self._reopen(self.session["last_url"], text)
            site, query = self.session.get("last_site"), self.session.get("last_query")
            if site and query:
                from . import tasks
                key = next((k for k, v in tasks.SITES.items() if v[0] == site), None)
                if key:
                    kind2 = "youtube_video" if key == "youtube" else "search"
                    return self._reopen_task(tasks.Task(kind2, key, query), text)
            if site:
                from . import tasks
                key = next((k for k, v in tasks.SITES.items() if v[0] == site), None)
                if key:
                    return self._reopen_task(tasks.Task("open_site", key), text)
            if kind == "page":
                self.ui.status.emit("retry", "直前に開いたページがありません", [f"聞き取り：{text}"], 3.0)
                return True
        # 「さっきのアプリ」→ 前に扱ったアプリを前面に出す
        if kind in ("app", "that"):
            name = self.session.get("last_app")
            if name:
                fg = winutil.foreground_window()
                if fg and (name in fg.title or name in fg.process):
                    self.ui.status.emit("done", f"「{name}」はすでに前面にあります", [f"聞き取り：{text}"], 3.0)
                    return True
                win = next((w for w in winutil.list_windows()
                            if name in w.title or name.lower() in w.process.lower()), None)
                if win:
                    winutil.focus_window(win.hwnd)
                    self.session["last_target"] = f"「{name}」に切り替え"
                    self.ui.status.emit("done", f"「{name}」を前面に出しました", [f"聞き取り：{text}"], 3.0)
                    self.ui.transcript_result.emit(f"「{name}」に切り替え")
                    return True
                self.ui.status.emit("retry", f"「{name}」のウィンドウが見つかりません", ["もう一度起動しますか？"], 4.0)
                return True
            if kind == "app":
                self.ui.status.emit("retry", "直前に使ったアプリがありません", [f"聞き取り：{text}"], 3.0)
                return True
        return False   # 文脈が無い → 通常の判定に任せる

    def _reopen(self, url: str, text: str) -> bool:
        """直前に開いた URL をもう一度開く（開いた先は記録し直す）。"""
        from . import tasks
        self.ui.status.emit("processing", "直前のページを開いています…", [url[:60]], 0.0)
        try:
            info: dict = {}
            how = tasks.open_url(url, None, info)
        except Exception as e:
            log.exception("ページを開き直せません")
            self.ui.status.emit("error", "開けませんでした", [str(e)[:60]], 4.0)
            return True
        self.session["last_url"] = info.get("url", url)
        self.session["last_target"] = f"{url[:50]} を開く"
        self.ui.status.emit("done", "直前のページを開きました", [f"（{how}）"], 4.0)
        self.ui.transcript_result.emit(f"直前のページを開きました（{how}）")
        self._remember("直前のページを開く", "done")
        return True

    def _reopen_task(self, task, text: str) -> bool:
        """直前に検索したサイト・語をもう一度たどる（「さっきの動画」など）。"""
        from . import tasks
        self._current_text = text
        desc = task.describe()
        self.ui.status.emit("processing", desc + "…", [f"聞き取り：{text}"], 0.0)
        info: dict = {}
        try:
            result = tasks.run(task, info)
        except Exception as e:
            log.exception("やり直しに失敗")
            self.ui.status.emit("error", "実行できませんでした", [desc, str(e)[:50]], 4.0)
            return True
        self._note_task(task, info)
        self.ui.status.emit("done", result, [f"聞き取り：{text}"], 4.0)
        self.ui.transcript_result.emit(result)
        self._remember(desc, "done")
        return True

    def _resume_plan(self) -> bool:
        """「続き」：中断・失敗した手順の続きから実行する。"""
        plan = self._last_plan
        if not plan or time.monotonic() - plan[4] > self._PLAN_TTL:
            return self._resume_chain()
        steps, lookup, hwnd, nxt, _ = plan
        if nxt >= len(steps):
            self.ui.status.emit("done", "直前の手順はすべて実行済みです", ["「もう一回」で最初からやり直せます"], 4.0)
            return True
        self.ui.status.emit("processing", f"手順 {nxt + 1} から再開します…",
                            [f"全 {len(steps)} 手のうち {nxt} 手まで実行済み"], 0.0)
        self._exec_plan(steps, lookup, self._current_text, hwnd, start=nxt)
        return True

    def _resume_chain(self) -> bool:
        """「続き」：分けて実行した命令（「〇〇して△△して」）の残りを実行する。"""
        chain = self._last_chain
        if not chain or time.monotonic() - chain[2] > self._PLAN_TTL:
            self.ui.status.emit("retry", "続きの手順がありません",
                                ["「続き」は、複数の手順が途中で終わったときだけ使えます"], 4.0)
            return True
        parts, nxt, _ = chain
        if nxt >= len(parts):
            self.ui.status.emit("done", "直前の手順はすべて実行済みです", ["「もう一回」で最初からやり直せます"], 4.0)
            return True
        rest = parts[nxt:]
        log.info("命令の続きを実行: %s", " ／ ".join(rest))
        self.ui.status.emit("processing", f"「{rest[0]}」から再開します…",
                            [f"全 {len(parts)} 手のうち {nxt} 手まで実行済み"], 0.0)
        for j, part in enumerate(rest):
            if j:
                time.sleep(1.2)
            self._handle_text(part, 0.0, winutil_profile(self), True, depth=1)
        self._last_chain = None
        return True

    def _remember(self, did: str, result: str) -> None:
        self._history.append({"said": self._current_text, "did": did, "result": result})
        # 手順の記録中は、実行できた声の命令を手順として残す（ルーチンの再生中・記録の命令そのものは除く）
        demo = getattr(self, "demo", None)
        if (demo is not None and demo.active and result == "done" and not did.startswith("demo")
                and not getattr(self, "_routine_depth", 0) and self._current_text):
            if not demo.steps or demo.steps[-1] != self._current_text:
                demo.add_utterance(self._current_text)
        self._last_was_plan = did.startswith("計画")   # 「もう一回」を手順全体のやり直しにするかの判断
        if result == "done" and self._current_text and not getattr(self, "_routine_depth", 0) \
                and not did.startswith(("routine", "demo", "AI:", "help")):
            self._watch_habit(self._current_text)

    # ---- 提案：同じ手順を何度も言っていたら、ルーチンにまとめることを勧める ----
    def _watch_habit(self, said: str) -> None:
        import collections
        now = time.monotonic()
        if not hasattr(self, "_habit"):
            self._habit = collections.deque(maxlen=300)   # (時刻, 発話)
            self._suggested: set = set()
            self._suggest = None
        if self._habit and self._habit[-1][1] == said:
            return   # 同じ命令の繰り返しは 1 回と数える
        self._habit.append((now, said))
        items = list(self._habit)
        for n in (3, 2):
            if len(items) < n:
                continue
            tail = items[-n:]
            if tail[-1][0] - tail[0][0] > 180:   # 3 分以内に続けて言ったものだけを手順とみなす
                continue
            seq = tuple(textparse.compact(x[1]) for x in tail)
            rotations = {seq[i:] + seq[:i] for i in range(n)}   # A→B と B→A は同じ流れとみなす
            if rotations & self._suggested or len(set(seq)) < n:
                continue
            count = sum(1 for i in range(len(items) - n + 1)
                        if tuple(textparse.compact(x[1]) for x in items[i:i + n]) == seq
                        and items[i + n - 1][0] - items[i][0] <= 180)
            if count >= 3:
                self._suggested |= rotations
                steps = [x[1] for x in tail]
                self._suggest = (steps, now)
                body = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
                self.ui.answer.emit("この流れ、よく使っていますね", body,
                                    "「〇〇として登録」と言うと、〇〇の一言で全部実行できます", 25.0)
                self.speaker.say("よく使う流れをルーチンにできます")
                return

    def _act(self, d: Decision, lookup: candidates.Lookup, text: str, snap: Snapshot, stt_ms: float) -> None:
        heard = f"聞き取り：{text}"
        self._current_text = text
        # 常時聞き取り中は、会話や独り言と判断された発話を無視する
        if self._continuous and d.engine.startswith("jev") and d.is_pc_command < 0.4 and d.action != "stop":
            log.info("会話とみなして無視: %s（PC への命令の確率 %.2f）", text, d.is_pc_command)
            self.ui.transcript_result.emit("（会話とみなして無視）")
            self._log(text, d, "（会話とみなして無視）", stt_ms, snap)
            return
        if d.action == "repeat":
            if self._last_was_plan and self._last_plan:
                steps, plookup, _, _, _ = self._last_plan
                self._last_was_plan = False
                log.info("直前の手順をもう一度実行: %d 手", len(steps))
                self._exec_plan(steps, plookup, text, 0, start=0)
                return
            if not self.previous:
                self.ui.status.emit("retry", "繰り返す操作がありません", [heard], 3.0)
                return
            d, lookup = self.previous, self.previous_lookup
        desc = d.describe(lookup)
        meta = f"{d.engine}  確信度 {d.confidence:.2f}"
        self._log(text, d, desc, stt_ms, snap)

        if d.action == "stop":
            self._stop(f"「{text}」")
            return
        # 操作の種類は決まったが対象の候補が割れている（「セーブのやつ」→ 上書き保存 27% / 保存して閉じる 23%）
        # → LLM やエージェントに回す前に、候補に番号を付けて本人に選んでもらう（Jev の確率をそのまま使う）
        if (d.engine.startswith("jev") and d.confidence < self.th_exec and d.is_pc_command >= 0.5
                and self._offer_choices(d, lookup, text)):
            self._log(text, d, "候補を提示", stt_ms, snap)
            return
        # Jev で決まらなかったら、LLM（既定はローカルの Ternary Bonsai 2 27B）に 1 回だけ問い直す
        # （聞き返し文の生成・複合命令への分解・入力文字列の生成ができる。基本的には Jev で済ませる）
        if self._llm_ready() and self._llm_worth_trying(d, text):
            if self._try_llm(d, lookup, text, snap, stt_ms):
                return
        # 起動したいアプリが見つからない（「Xを開いて」「〇〇開いて」でインストールされていない）→ その名前のサイトを開く
        no_app = d.action == "launch_app" and d.params.get("app") not in lookup.get("app", {})
        if no_app or d.action == "unknown":
            from . import tasks
            guess = tasks.web_guess(text)
            # 判定できなかった（unknown）ときは、聞き違いの多いカタカナは避けて英字の名前だけにする
            if guess is not None and (no_app or guess.query.isascii()) and not self.catalog.find(guess.query):
                log.info("アプリが見つからないためサイトを開く: %s", guess.query)
                self._run_task(guess, text)
                return
        if (d.action == "unknown" or d.confidence < self.th_confirm) and self.agent_enabled and self.engine                and d.engine.startswith("jev") and d.is_pc_command >= 0.6 and len(textparse.compact(text)) >= 5:
            self._run_agent(text)  # 1 手では決まらない目的 → Jev に 1 手ずつ選ばせて進める
            return
        if d.action == "unknown" or d.confidence < self.th_confirm:
            # 名前で指したのに画面に無いときは、それと伝える（黙って失敗しない）
            noun = _noun(text)
            extra = [] if not noun or self._screen_has(noun, lookup) else [f"「{noun}」は画面にありません"]
            self.ui.status.emit("retry", "もう一度お願いします", [heard] + extra + [meta], 4.0)
            self.ui.transcript_result.emit("聞き返し（判定できませんでした）" + (f"（{extra[0]}）" if extra else ""))
            self._remember("（判定できず）", "not_understood")
            return
        if d.action == "show_hints":
            self._show_hints(d.params.get("hint_mode", "elements"), snap)
            return
        param = next(iter(d.params.values()), None) if d.action in ("key_combo", "window_control") else None
        risky = False
        if d.action == "menu_command" and d.params.get("menu") in lookup.get("menu", {}):
            risky = any(w in " ".join(lookup["menu"][d.params["menu"]].data.path) for w in self.confirm_words)
        if d.action == "app_command" and d.params.get("command") in lookup.get("command", {}):
            risky = lookup["command"][d.params["command"]].data.confirm
        d.expect_hwnd = snap.window.hwnd if snap.window else 0
        lp, self._last_pending = self._last_pending, None
        if lp is not None and lp.action == d.action and lp.params == d.params and d.action != "unknown":
            self._execute(d, lookup, [heard, "確認中に同じ命令が来たので実行"])  # 言い直し ＝ 「はい」
            return
        in_app = d.action in ("menu_command", "app_command", "click_element", "mouse_to_element")
        if d.action == "click_element" and d.params.get("element") in lookup.get("element", {}):
            risky = any(w.lower() in lookup["element"][d.params["element"]].data.name.lower() for w in self.confirm_words)
        if d.engine.startswith("keyword") and d.confidence >= 0.9:
            d.supported = True  # 言い方の完全一致・発音の強い一致で決まったもの
        # 確認を省く緩いしきい値は、コード側の照合の裏付けがあるときだけ（崩れた認識結果で実行しないため）
        low = d.confidence < (self.th_in_app if in_app and not risky and d.supported else self.th_exec)
        if self.cfg.needs_confirm(d.action, param) or risky or low:
            self.pending = (d, lookup, time.monotonic() + self.confirm_timeout)
            self._pending_text = text   # 「はい」と答えたらこの言い方を覚える
            self.ui.status.emit("confirm", f"{desc}？", [heard, "「はい」か Enter で実行 / 「いいえ」か Esc で取消", meta], 0.0)
            self.ui.transcript_result.emit(f"確認待ち：{desc}")
            return
        self._pending_text = ""
        self._execute(d, lookup, [heard, meta])

    _CHOICE_PARAM = {"click_element": "element", "mouse_to_element": "element", "menu_command": "menu",
                     "launch_app": "app", "switch_window": "window", "app_command": "command"}

    def _offer_choices(self, d: Decision, lookup: candidates.Lookup, text: str) -> bool:
        """Jev の候補の確率が割れているとき（1 位と 2 位が近い）、上位の候補に番号を付けて選ばせる。
        画面上の項目なら、その項目の上に番号を出す（指さし感覚で「2」と言える）。"""
        qid = self._CHOICE_PARAM.get(d.action)
        alts = [(cid, p) for cid, p in (d.alternatives.get(qid) or []) if cid in (lookup.get(qid) or {})] if qid else []
        if len(alts) < 2 or alts[1][1] < 0.15 or alts[0][1] - alts[1][1] > 0.5:
            return False
        choices = {}
        labels = []
        lines = []
        alts = [a for a in alts[:3] if a[1] >= 0.1]
        for i, (cid, p) in enumerate(alts, start=1):
            dd = Decision(d.action, dict(d.params, **{qid: cid}), max(p, 0.8), engine=d.engine + "+選択",
                          is_pc_command=d.is_pc_command, supported=True)
            dd.expect_hwnd = d.expect_hwnd
            choices[i] = dd
            cand = lookup[qid][cid]
            lines.append(f"{i}. {dd.describe(lookup)}（{p:.0%}）")
            if qid == "element":
                labels.append((i, cand.data.center))
        self.hints = {"mode": "choices", "items": choices, "lookup": lookup}
        if labels:
            self.ui.hints_labels.emit(labels)
        self.ui.status.emit("confirm", "どれですか？", lines + ["番号で答えてください（「ストップ」で取消）"], 0.0)
        self.ui.transcript_result.emit("候補から選んでください")
        self._pending_text = text
        return True

    def _execute(self, d: Decision, lookup: candidates.Lookup, lines: list[str]) -> bool:
        """実行できたら True。失敗しても例外は投げず、表示と記録だけして False を返す
        （呼び出し側が残りの手順を続けられるようにするため）。"""
        desc = d.describe(lookup)
        try:
            self._check_window(d)
            if d.action in ("menu_command", "app_command", "learn_app"):
                self._run_app_action(d, lookup)
            elif d.action == "click_element" and self._click_by_uia(d, lookup):
                pass
            else:
                self.executor.run(d, lookup)
        except ExecutionError as e:
            self.ui.status.emit("error", "実行できませんでした", [desc, str(e)], 4.0)
            self.ui.transcript_result.emit(f"実行できませんでした（{e}）")
            self._remember(desc, f"failed: {e}")
            if d.engine == "learned" and self.learner is not None and self._learned_key:
                self.learner.penalize(self._learned_key, "実行に失敗")   # 覚え違いは少しずつ忘れる
            return False
        self.previous, self.previous_lookup = d, lookup
        self._note_action(d, lookup)
        self._remember(desc, "done")
        if d.engine == "learned" and self.learner is not None and self._learned_key:
            self.learner.reward(self._learned_key)
        # 「これ覚えて」で覚えられるように、最後に実行した内容と元の発話を残す
        if self._current_text and d.action not in ("unknown", "stop", "repeat"):
            self._last_learnable = (self._current_text, d, lookup)
            self._last_agent = None   # 覚える対象は直前の 1 手のほう
        if d.action == "learn_app":  # 「〇項目覚えました」の表示を残す
            self.ui.transcript_result.emit("メニューを覚えました")
            return True
        self.ui.status.emit("done", desc, lines, 3.0)
        self.ui.transcript_result.emit(desc)
        return True

    # 前面のウィンドウに対して行う操作（別のウィンドウに誤って送らないよう、判定時のウィンドウと照合する）
    _WINDOW_BOUND = {"click_element", "mouse_to_element", "menu_command", "app_command", "type_text", "learn_app",
                     "window_control", "snap_window"}
    _GLOBAL_KEYS = {"volume_up", "volume_down", "mute", "media_play_pause", "media_next", "media_prev", "start_menu",
                    "task_view", "alt_tab", "screenshot", "ime_toggle", "lock_pc", "task_manager", "win_explorer",
                    "clipboard_history", "desktop_next", "desktop_prev", "desktop_new",
                    "next_window", "prev_window", "screenshot_save", "magnify_in", "magnify_out"}

    def _check_window(self, d: Decision, retry_wait: float = 0.8) -> None:
        """判定したときの前面ウィンドウと違えば中止する。ただしウィンドウの切り替わりは一瞬遅れることが
        あるため（起動直後・フォーカス移動中）、一度だけ待って再確認する。"""
        bound = d.action in self._WINDOW_BOUND or (d.action == "key_combo" and d.params.get("key") not in self._GLOBAL_KEYS)
        if not bound or not d.expect_hwnd:
            return
        for attempt in (0, 1):
            fg = winutil.foreground_window()
            if fg is not None and fg.hwnd == d.expect_hwnd:
                return
            if attempt == 0 and retry_wait:
                log.info("前面のウィンドウが変わっているため %.1f 秒待って確認し直します", retry_wait)
                time.sleep(retry_wait)
                continue
            raise ExecutionError(f"前面のウィンドウが変わったため中止しました（今: {fg.title[:30] if fg else 'なし'}）")

    def _click_by_uia(self, d: Decision, lookup: candidates.Lookup) -> bool:
        """UI Automation で探し直して操作する。UIA で取れた要素でなければ False（マウスでクリックする）。
        UIA の操作に失敗しても、画面が変わっていなければ座標クリックに切り替える（要素が古い場合の保険）。"""
        from . import uiaction
        cand = lookup.get("element", {}).get(d.params.get("element"))
        if cand is None or cand.data.source != "uia" or not d.expect_hwnd:
            return False
        try:
            how = uiaction.run_isolated(uiaction.act_on_element, d.expect_hwnd, cand.data, d.params.get("button", "left"))
        except uiaction.ActionError as e:
            fg = winutil.foreground_window()
            if fg is None or fg.hwnd != d.expect_hwnd:
                raise ExecutionError(str(e))
            log.warning("UIA で操作できないため座標クリックに切り替えます: %s", e)
            winutil.set_cursor(*cand.data.center)
            time.sleep(0.05)
            self.executor.click(d.params.get("button", "left"))
            return True
        log.info("要素を操作: %s（%s）", cand.data.name, how)
        return True

    # ---- アプリマップ・プロファイルの操作 ----
    def _appmap(self, window):
        if window is None or not window.process:
            return None
        key = window.process.lower()
        if key not in self._appmaps:
            from . import appmap
            self._appmaps[key] = appmap.load(window.process)
        return self._appmaps[key]

    def _uia(self, fn, *args, timeout: float = 30.0):
        """UI Automation の処理は COM を初期化した専用スレッドで行う。"""
        return self.context._uia_pool.submit(fn, *args).result(timeout=timeout)

    def _run_app_action(self, d: Decision, lookup: candidates.Lookup) -> None:
        from . import appmap
        fg = winutil.foreground_window()
        if fg is None:
            raise ExecutionError("操作するウィンドウがありません")
        if d.action == "learn_app":
            self.ui.status.emit("processing", "メニューを読み取っています…", ["数秒間、マウスとキーボードに触らないでください"], 0.0)
            m = self._uia(appmap.MenuScanner().scan, fg.hwnd, fg.process, fg.title, timeout=120)
            old = appmap.load(fg.process)
            if old and not m.entries:
                m.entries = old.entries  # メニューのない子ウィンドウ（トレーナーなど）で覚えたときは前のメニューを残す
            controls = self._uia(appmap.scan_controls, fg.hwnd, fg.title, timeout=60)
            # 別のウィンドウで覚えた部品は残し、このウィンドウの部品は入れ替える
            keep = [c for c in (old.controls if old else []) if c.window != fg.title]
            m.controls = keep + controls
            if not m.entries and not m.controls:
                raise ExecutionError("メニューも操作部品も見つかりませんでした")
            appmap.save(m)
            self._appmaps[fg.process.lower()] = m
            self.ui.status.emit("done", f"{fg.process} を覚えました", [f"メニュー {len(m.entries)} 項目",
                                                                    f"ボタンなど {len(controls)} 個（このウィンドウ）"], 5.0)
            return
        if d.action == "menu_command":
            self._run_menu(fg, lookup["menu"][d.params["menu"]].data)
            return
        cmd = lookup["command"][d.params["command"]].data
        for step in cmd.steps:
            self._run_step(fg, step)

    def _run_menu(self, fg, entry) -> None:
        from . import appmap
        vks = appmap.parse_keys(entry.shortcut) if entry.shortcut else None
        if vks:
            winutil.press_combo(vks)
        else:
            from . import uiaction
            if uiaction.run_isolated(appmap.invoke_path, fg.hwnd, entry.path, timeout=4.0) is False:
                raise ExecutionError("メニューが見つかりません（アプリの画面が変わった可能性があります。"
                                     "「このアプリを覚えて」で覚え直せます）")

    def _run_step(self, fg, step: dict) -> None:
        """プロファイルの独自コマンドの 1 手順。"""
        from . import appmap
        if "keys" in step:
            vks = appmap.parse_keys(str(step["keys"]))
            if not vks:
                raise ExecutionError(f"キーの書き方が読めません: {step['keys']}")
            winutil.press_combo(vks)
        elif "text" in step:
            winutil.type_unicode(str(step["text"]))
        elif "wait" in step:
            time.sleep(float(step["wait"]))
        elif "menu" in step:
            path = [str(x) for x in step["menu"]]
            m = self._appmap(fg)
            entry = next((e for e in (m.entries if m else []) if e.path == path), None)
            if entry:
                self._run_menu(fg, entry)
            else:
                from . import uiaction
                if uiaction.run_isolated(appmap.invoke_path, fg.hwnd, path, timeout=4.0) is False:
                    raise ExecutionError(f"メニューが見つかりません: {' > '.join(path)}")
        elif "click" in step:
            name = str(step["click"])
            els = self._uia(self.context._uia.scan, fg.hwnd, fg.rect)
            best = max(els, key=lambda e: textparse.similarity(name, e.name), default=None)
            if best is None or textparse.similarity(name, best.name) < 1.0:
                raise ExecutionError(f"画面に「{name}」が見つかりません")
            winutil.set_cursor(*best.center)
            time.sleep(0.03)
            self.executor.click("left")
        else:
            raise ExecutionError(f"手順の書き方が読めません: {step}")
        time.sleep(0.05)

    def _resolve_confirm(self, yes: bool, via: str) -> None:
        if self.pending_plan is not None:
            steps, lookup, _, hwnd = self.pending_plan
            self.pending_plan = None
            if yes:
                if self._pending_text:   # 確認して実行できた手順は覚える（次からは確認も LLM も無し）
                    self._learn(self._pending_text, "unknown", None, plan=steps, source="confirm")
                self._exec_plan(steps, lookup, self._current_text, hwnd)
            else:
                self.ui.status.emit("idle", "取り消しました", [via], 2.5)
                self.ui.transcript_result.emit("取り消し")
            self._pending_text = ""
            return
        if not self.pending:
            return
        d, lookup, _ = self.pending
        self.pending = None
        text = self._pending_text
        self._pending_text = ""
        if yes:
            if d.engine == "learned":     # 学習した言い方で確認を出したときは信頼を上げる
                if self.learner is not None and self._learned_key:
                    self.learner.reward(self._learned_key)
            elif text:
                # 確認して実行できた言い方は覚える（「はい」が学習の合図になる）
                self._learn(text, d.action, d.params, text_value=d.text, source="confirm")
            self._execute(d, lookup, [f"確認：{via}"])
        else:
            if d.engine == "learned" and self.learner is not None and self._learned_key:
                self.learner.penalize(self._learned_key, "確認で取り消し")
            self.ui.status.emit("idle", "取り消しました", [via], 2.5)
            self.ui.transcript_result.emit("取り消し")

    # ---- LLM フォールバック（既定はローカルの Ternary Bonsai 2 27B / llama.cpp） ----
    def _llm_ready(self) -> bool:
        """LLM を使ってよいか。続けて失敗したときは一定時間だけ休む（永久には切らない）。

        llama-server を後から起動した・GPU が混んでいた、といった一時的な理由で
        「この起動ではもう使えない」ままになるのを避ける。
        """
        if self.llm is None:
            return False
        if self.llm.failures >= 3:
            now = time.monotonic()
            if now < self._llm_off_until:
                return False
            log.info("LLM フォールバックを再び試します（前回の失敗 %d 回）", self.llm.failures)
            self.llm.failures = 0
        return True

    def _llm_worth_trying(self, d: Decision, text: str) -> bool:
        """この発話を LLM に問い直す価値があるか。

        長い発話（12 文字以上）は「PC への命令ではない」と判定されていても渡す。Jev は長い複文が
        苦手で、is_pc_command が低いまま unknown を返すことがあり、ここで落とすと長いプロンプトが
        いつまでも解釈されずに聞き返しになる。
        """
        c = textparse.compact(text)
        if len(c) < 3 or d.engine.startswith("llm") or getattr(self, "_llm_said", 0):
            return False
        # 数・時刻・割合を含む言い方（「三割くらいに」「7時に」）は、Jev が近い操作（音量を下げる等）を
        # そこそこの確信度で選んでも数値が失われるので、確信しきれていなければ LLM の言い直しを試す
        quantity = re.search(r"([0-9０-９]|[一二三四五六七八九十百]+(割|分|秒|時|回|個|つ|番|パーセント|%))", c)
        if quantity and d.action == "key_combo":
            return True   # 「音量を三割に」を「音量を下げる」1 回にするなど、数値を捨てる判定になりやすい
        if d.action != "unknown" and d.confidence >= (self.th_exec if quantity else self.th_confirm):
            return False
        return d.is_pc_command >= 0.5 or len(c) >= 12

    def _llm_backoff(self) -> None:
        """失敗が続いたときの休み時間を設定する。"""
        if self.llm is None or self.llm.failures < 3:
            return
        self._llm_off_until = time.monotonic() + self.llm_cooldown
        log.warning("LLM フォールバックが %d 回続けて失敗したため %.0f 秒休みます（あとで自動的に再開します）",
                    self.llm.failures, self.llm_cooldown)

    def _try_llm(self, d: Decision, lookup: candidates.Lookup, text: str, snap: Snapshot, stt_ms: float) -> bool:
        """Jev で決まらなかった発話を LLM に問い直す。処理できたら True（呼び出し側は return する）。
        失敗・確信度不足のときは False を返し、JevAgent / 聞き返しの今までの流れに任せる。"""
        mode = "text" if d.action == "type_text" and not d.text else "interpret"
        recent = [f"{h.get('said')} → {h.get('did')}" for h in list(self._history)[-3:]]
        try:
            r = self.llm.ask(mode, text, snap, lookup, facts=self._last_facts,
                             previous=self.previous, recent=recent, session=self.session_state())
        except Exception as e:
            log.warning("LLM フォールバックでエラー: %s", e)
            return False
        if not r.ok:
            log.info("LLM フォールバックを使えず (%s): %s", r.error, text)
            self._llm_backoff()
            return False
        if r.say and getattr(self, "_llm_said", 0) < 1:
            # アプリがそのまま処理できる言い方に言い直してもらえた → その言い方で処理し直す（1 回だけ）
            log.info("LLM の言い直し: %s → %s", text, r.say)
            self._llm_said = 1
            try:
                self._handle_text(r.say, stt_ms, winutil_profile(self), True)
            finally:
                self._llm_said = 0
            return True
        if r.clarify:
            self._remember("（聞き返し）", "clarify")
            self._log(text, Decision("unknown", {}, 0.0, engine=r.engine),
                      f"聞き返し: {r.clarify}", stt_ms, snap)
            self.ui.status.emit("retry", r.clarify, [f"聞き取り：{text}", "言葉を変えて言ってみてください"], 6.0)
            self.ui.transcript_result.emit(r.clarify)
            return True
        if r.plan:
            self._learn(text, "unknown", None, plan=r.plan, source="llm")  # 次からは LLM なしで実行できる
            self._run_plan(r.plan, lookup, text, snap, stt_ms)
            return True
        if r.action != "unknown" and r.confidence >= self.th_confirm:
            self._learn(text, r.action, r.params, text_value=r.text, source="llm")
            self._act(r.decision(), lookup, text, snap, stt_ms)   # 通常の確認→実行の流れに乗せる
            return True
        return False   # 会話・確信度不足 → JevAgent / 聞き返しへ

    # ---- 学習（覚える・思い出す・忘れる） ----
    def _learn(self, text: str, action: str, params: dict | None, text_value: str | None = None,
               plan: list | None = None, source: str = "") -> None:
        """解釈できた言い方を覚える（次からは Jev も LLM も通さずに実行する）。"""
        if self.learner is None:
            return
        fg = winutil.foreground_window()
        self.learner.remember(text, action, params, text_value, plan, fg.process.lower() if fg and fg.process else "",
                              source)
        self._note_learning()

    def _note_learning(self) -> None:
        """覚えたことを短く知らせる（実行の表示を消さないよう、控えめにログへ）。"""
        if self.learner is None:
            return
        log.info("学習: 覚えている言い方 %d 件（確認なしで実行できるもの %d 件）",
                 len(self.learner.entries), len(self.learner.trusted()))

    def _run_learned(self, entry, text: str, stt_ms: float) -> None:
        """前に覚えた言い方で実行する。確信度は成功回数で決まる（何度も成功したものは確認を省く）。"""
        conf = self.learner.confidence(entry)
        d = Decision(entry.action, dict(entry.params or {}), conf, engine="learned", text=entry.text)
        if conf >= self.th_exec:
            d.supported = True   # 何度も成功している言い方なので確認を省く
        desc = entry.describe()
        log.info("学習した言い方で実行: %s（成功 %d 回 / 確信度 %.2f）", text, entry.hits, conf)
        self._log(text, d, f"学習: {desc}", stt_ms, None)
        self._learned_key = entry.key
        if entry.plan:
            self._exec_plan(entry.plan, self.previous_lookup, text, 0)
            return
        self.ui.status.emit("processing", f"{desc}…", [f"聞き取り：{text}", "覚えていた言い方で実行"], 0.0)
        self._act(d, self.previous_lookup, text, None, stt_ms)

    def _forget(self, text: str) -> bool:
        """「忘れて」：直前に学習で実行した言い方（または指定した言い方）を忘れる。"""
        if self.learner is None:
            return False
        target = self._learned_key
        if not target and not self._last_learnable:
            return False
        say = None
        if target:
            say = self.learner.forget(target)
            self._learned_key = None
        if say is None and self._last_learnable:
            say = self.learner.forget(self._last_learnable[0])
        if say is None:
            return False
        self.ui.status.emit("done", "忘れました", [f"「{say}」", "次はまた判定し直します"], 4.0)
        self.ui.transcript_result.emit(f"「{say}」を忘れました")
        return True

    def _show_learning(self) -> None:
        from .learner import format_stats
        if self.learner is None:
            self.ui.status.emit("idle", "学習は無効です", ["config.yaml の learning.enabled を true にしてください"], 5.0)
            return
        self.ui.status.emit("idle", "覚えていること", format_stats(self.learner), 8.0)
        self.ui.transcript_result.emit(f"学習 {len(self.learner.entries)} 件を表示")


    def _plan_desc(self, step: dict, lookup: candidates.Lookup) -> str:
        if step["action"] in ("open_site", "search_site"):
            from . import tasks
            kind = "open_site" if step["action"] == "open_site" else "search"
            return tasks.Task(kind, step["site"], step.get("query", ""), step.get("browser")).describe()
        if step["action"] == "open_location":
            from . import tasks
            return tasks.Task("open_location", step["location"]).describe()
        d = Decision(step["action"], dict(step.get("params") or {}), text=step.get("text"))
        return d.describe(lookup)

    def _run_plan(self, steps: list, lookup: candidates.Lookup, text: str, snap: Snapshot, stt_ms: float) -> None:
        """LLM が分解した複数の手順を実行する。取り消しにくい操作（入力・削除等）が含まれる plan は
        内容を一覧で示して全体を一度だけ確認する。それ以外は順に実行する。"""
        from .schema import Decision as D
        self._current_text = text
        hwnd = snap.window.hwnd if snap.window else 0
        descs = [self._plan_desc(s, lookup) for s in steps]
        engine = f"llm({self.llm.label})" if self.llm is not None else "llm"
        self._log(text, D("llm_plan", {}, 1.0, engine=engine), " → ".join(descs), stt_ms, snap)
        risky = any(self.cfg.needs_confirm(s["action"], next(iter(s.get("params", {}).values()), None))
                    for s in steps if s["action"] not in ("open_site", "search_site", "open_location"))
        lines = [f"聞き取り：{text}"] + [f"{i + 1}. {d}" for i, d in enumerate(descs)]
        if risky:
            self.pending_plan = (steps, lookup, time.monotonic() + self.confirm_timeout, hwnd)
            self.ui.status.emit("confirm", f"{len(steps)} 手順を実行します？",
                                lines + ["「はい」か Enter で実行 / 「いいえ」か Esc で取消"], 0.0)
            self.ui.transcript_result.emit(f"確認待ち：{len(steps)} 手順（{' / '.join(descs)[:80]}）")
            return
        self._exec_plan(steps, lookup, text, hwnd)

    def _exec_plan(self, steps: list, lookup: candidates.Lookup, text: str, hwnd: int = 0,
                   start: int = 0) -> None:
        """手順を順に実行する。1 手が失敗しても残りは続けて実行し、最後にまとめて結果を知らせる
        （途中で止めると、そこまでの手順が無駄になるため）。失敗した手順は「続き」でやり直せる。"""
        from . import tasks
        from .schema import Decision as D
        total = len(steps)
        done, failed = 0, []
        first_fail: int | None = None
        i = max(0, start)
        while i < total:
            s = steps[i]
            desc = self._plan_desc(s, lookup)
            if self.agent.abort:   # Pause キーなどで止められていたら、そこから先は実行しない
                self._last_plan = (steps, lookup, hwnd, i, time.monotonic())
                self._last_was_plan = False
                self.ui.status.emit("idle", "途中で止めました",
                                    [f"全 {total} 手のうち {i} 手まで実行", "「続き」と言うと残りを実行します"], 6.0)
                return
            self.ui.status.emit("processing", f"手順 {i + 1}/{total}: {desc[:40]}",
                                [f"聞き取り：{text}", "Pause で中断（失敗しても次の手順へ進みます）"], 0.0)
            try:
                if s["action"] in ("open_site", "search_site"):
                    kind = "open_site" if s["action"] == "open_site" else "search"
                    info: dict = {}
                    task = tasks.Task(kind, s["site"], s.get("query", ""), s.get("browser"))
                    result = tasks.run(task, info)
                    self._note_task(task, info)
                    self.ui.transcript_result.emit(result)
                    done += 1
                elif s["action"] == "open_location":
                    info = {}
                    task = tasks.Task("open_location", s["location"])
                    result = tasks.run(task, info)
                    self._note_task(task, info)
                    self.ui.transcript_result.emit(result)
                    done += 1
                else:
                    d = D(s["action"], dict(s.get("params") or {}), confidence=1.0, engine="llm-plan",
                          text=s.get("text"))
                    # 1 手目は発話時の画面の要素を指せる。2 手目以降は前の手順で画面が変わるため照合しない
                    d.expect_hwnd = hwnd if i == 0 and start == 0 else 0
                    if self._execute(d, lookup, [f"手順 {i + 1}/{total}", f"聞き取り：{text}"]):
                        done += 1
                    else:
                        failed.append(f"{i + 1}. {desc}")
                        first_fail = i if first_fail is None else first_fail
            except Exception as e:
                log.exception("plan の手順に失敗（残りは続けて実行します）")
                failed.append(f"{i + 1}. {desc}（{str(e)[:40]}）")
                first_fail = i if first_fail is None else first_fail
            i += 1
            if i < total:
                time.sleep(1.0)   # アプリの起動など、前の手順の結果が落ち着くのを待つ
        nxt = first_fail if first_fail is not None else total
        self._last_plan = (steps, lookup, hwnd, nxt, time.monotonic())
        self._last_was_plan = True
        if failed:
            self._remember(f"計画 {done}/{total} 手", "partial")
            self.ui.status.emit("retry", f"{total} 手のうち {len(failed)} 手が実行できませんでした",
                                failed[:3] + ["「続き」と言うと、失敗した手順からやり直します"], 9.0)
            self.ui.transcript_result.emit(f"{done}/{total} 手を実行（{len(failed)} 手が失敗）")
        else:
            self._remember(f"計画 {total} 手", "done")
            self.ui.status.emit("done", f"{total} 手順を実行しました", [f"聞き取り：{text}"], 4.0)
            self.ui.transcript_result.emit(f"{total} 手順を実行しました")
    def _show_hints(self, mode: str, snap: Snapshot) -> None:
        if mode == "elements" and snap.elements:
            items = {i + 1: el.center for i, el in enumerate(snap.elements[:99])}
            self.hints = {"mode": "elements", "items": items}
            self.ui.hints_labels.emit([(n, c) for n, c in items.items()])
            self.ui.status.emit("hints", "番号を言ってください", ["「グリッド」でマス目表示 / 「ストップ」で終了"], 0.0)
            return
        x, y = snap.cursor
        region = winutil.monitor_rect_at(x, y)
        self.hints = {"mode": "grid", "region": region}
        self.ui.hints_grid.emit(region, int(self.cfg.get("screen.grid_size", 3)))
        self.ui.status.emit("hints", "マス目の番号を言ってください", ["「クリック」でその場所をクリック / 「ストップ」で終了"], 0.0)

    def _handle_hint_utterance(self, text: str) -> bool:
        """ヒント表示中の発話を処理できたら True。"""
        h = self.hints
        n = textparse.parse_number(text)
        if h["mode"] == "choices":
            c = textparse.compact(text)
            if n is None:   # 「最初の」「2つ目」「右の」も番号として受ける
                n = {"最初": 1, "一つ目": 1, "1つ目": 1, "二つ目": 2, "2つ目": 2, "三つ目": 3, "3つ目": 3,
                     "上の": 1, "下の": 2}.get(re.sub(r"(の|ほう|方|やつ|で|を|押して|クリック)+$", "", c) or c)
            if n is not None and n in h["items"]:
                self.hints = None
                self.ui.hints_clear.emit()
                d = h["items"][n]
                if self.learner is not None and self._pending_text:   # 選んだ答えは覚える（次からは迷わない）
                    self._learn(self._pending_text, d.action, d.params, source="confirm")
                self._execute(d, h["lookup"], [f"{n} 番を選択"])
                return True
            self.hints = None
            self.ui.hints_clear.emit()
            return False
        kd = self.keyword.decide(text, None, {}, self.previous)  # type: ignore[arg-type]
        if h["mode"] == "elements":
            if n is not None and n in h["items"]:
                self.hints = None
                self.ui.hints_clear.emit()
                winutil.set_cursor(*h["items"][n])
                time.sleep(0.03)
                button = kd.params.get("button", "left") if kd.action == "click" else "left"
                self.executor.click(button)
                self.ui.status.emit("done", f"{n} 番をクリック", [], 2.5)
                return True
            if kd.action == "show_hints" and kd.params.get("hint_mode") == "grid":
                self.hints = None
                self._show_hints("grid", Snapshot(None, winutil.get_cursor()))
                return True
            return False
        # グリッド：番号で絞り込み、「クリック」で決定
        size = int(self.cfg.get("screen.grid_size", 3))
        if n is not None and 1 <= n <= size * size:
            l, t, r, b = h["region"]
            row, col = divmod(n - 1, size)
            cw, ch = (r - l) / size, (b - t) / size
            region = (int(l + cw * col), int(t + ch * row), int(l + cw * (col + 1)), int(t + ch * (row + 1)))
            winutil.set_cursor((region[0] + region[2]) // 2, (region[1] + region[3]) // 2)
            if region[2] - region[0] < 30:  # これ以上は細かくできないので、その場でクリック
                self.hints = None
                self.ui.hints_clear.emit()
                self.executor.click("left")
                self.ui.status.emit("done", "クリックしました", [], 2.5)
                return True
            h["region"] = region
            self.ui.hints_grid.emit(region, size)
            return True
        if kd.action in ("click", "drag_start", "drag_end"):
            self.hints = None
            self.ui.hints_clear.emit()
            if kd.action == "click":
                self.executor.click(kd.params.get("button", "left"))
            elif kd.action == "drag_start":
                self.executor.run(kd, {})
            else:
                self.executor.run(kd, {})
            self.ui.status.emit("done", kd.describe({}), [], 2.5)
            return True
        return False

    # ---- ログ ----
    def _log(self, text: str, d: Decision, desc: str, stt_ms: float, snap: Snapshot | None) -> None:
        rec = {"time": datetime.now().isoformat(timespec="milliseconds"), "text": text, "desc": desc,
               "decision": asdict(d), "stt_ms": round(stt_ms),
               "window": snap.window.to_state() if snap is not None and snap.window else None,
               "n_elements": len(snap.elements) if snap is not None else 0}
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("ログの書き込みに失敗")
