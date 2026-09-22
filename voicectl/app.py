"""アプリの起動：各部品を組み立て、トレイアイコンとオーバーレイを持つ常駐アプリとして動かす。"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys

from . import winutil
from .config import ROOT, load


class _NoRecorder:
    """マイクが開けなかったときの代役。録音は常に空になる。"""
    level = 0.0

    def start(self) -> None:
        pass

    def stop(self):
        import numpy as np
        return np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        pass


def _single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "voicectl-single-instance")
    return ctypes.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _setup_logging(debug: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "voicectl.log", encoding="utf-8")],
    )
    for noisy in ("httpx", "faster_whisper", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser(description="Jev を使った Windows 音声操作アプリ")
    ap.add_argument("--config", help="設定ファイルのパス")
    ap.add_argument("--keyword-only", action="store_true", help="Jev を使わずキーワード照合だけで動かす")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    _setup_logging(args.debug)
    log = logging.getLogger("voicectl")
    if not _single_instance():
        log.error("すでに起動しています")
        return 1

    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"  # Qt の座標 = 物理ピクセル
    winutil.set_dpi_aware()

    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    from pathlib import Path

    from .apps import AppCatalog
    from .audio import Recorder
    from .controller import Controller
    from .keys import KeyHook
    from .overlay import AnswerCard, HintOverlay, StatusOverlay, TranscriptBar
    from .stt import SpeechRecognizer

    cfg = load(Path(args.config) if args.config else None)

    class UiBridge(QObject):
        status = Signal(str, str, list, float)
        level = Signal(float)
        hints_labels = Signal(list)
        hints_grid = Signal(tuple, int)
        hints_clear = Signal()
        transcript_partial = Signal(str)
        transcript_final = Signal(str)
        transcript_result = Signal(str)
        idle = Signal(str, str)
        answer = Signal(str, str, str, float)   # AI の答えカード（題名, 本文, ヒント, 表示秒数）。本文が空なら考え中
        answer_clear = Signal()
        copy_screen = Signal()   # 画面をクリップボードへ（GUI スレッドで実行するためシグナル経由）

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    font_px = int(cfg.get("overlay.font_px", 18))
    margin = int(cfg.get("overlay.margin_px", 16))
    avoid_taskbar = bool(cfg.get("overlay.avoid_taskbar", True))
    animate = bool(cfg.get("overlay.animate", True))
    status = StatusOverlay(cfg.get("overlay.corner", "bottom_right"), font_px, margin, avoid_taskbar, animate)
    hints = HintOverlay(font_px)
    ui = UiBridge()

    def _copy_screen():
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            QApplication.clipboard().setPixmap(scr.grabWindow(0))

    ui.copy_screen.connect(_copy_screen)
    ui.status.connect(lambda st, title, lines, hold: status.set_status(st, title, lines, hold))
    ui.level.connect(status.set_level)
    ui.idle.connect(status.set_idle)
    ui.hints_labels.connect(hints.show_labels)
    ui.hints_grid.connect(hints.show_grid)
    ui.hints_clear.connect(hints.clear)
    bar = None
    if cfg.get("transcript.enabled", True):
        bar = TranscriptBar(int(cfg.get("transcript.font_px", 22)), float(cfg.get("transcript.width_ratio", 0.6)),
                            int(cfg.get("transcript.history", 2)),
                            str(cfg.get("hotkeys.push_to_talk", "f5")).upper().replace("_", " "), anchor=status,
                            margin=margin, avoid_taskbar=avoid_taskbar, animate=animate)
        status.on_layout.append(bar._layout)
        ui.transcript_partial.connect(bar.set_partial)
        ui.transcript_final.connect(bar.set_final)
        ui.transcript_result.connect(bar.set_result)
        ui.level.connect(bar.set_level)

    card = AnswerCard(int(cfg.get("overlay.answer_font_px", font_px - 2)),
                      anchors=[w for w in (bar, status) if w is not None],
                      margin=margin, avoid_taskbar=avoid_taskbar, animate=animate)
    ui.answer.connect(card.show_answer)
    ui.answer_clear.connect(card.clear)
    status.set_status("processing", "起動中…", ["音声認識モデルを読み込んでいます"])
    qapp.processEvents()

    catalog = AppCatalog(cfg.get("app_aliases", {}))
    catalog.refresh_async()

    def report_missing_readings():
        import time as _t
        _t.sleep(15)  # アプリ一覧の更新を待つ
        from .config import load_app_readings
        missing = catalog.names_without_reading(set(load_app_readings().values()))
        if missing:
            log.info("読みが未登録のアプリ（%d 件、app_readings.yaml に追加できます）: %s", len(missing),
                     " / ".join(missing))
    import threading as _th
    _th.Thread(target=report_missing_readings, daemon=True).start()

    engine = None
    if not args.keyword_only and cfg.get("decision.engine", "jev") == "jev":
        from .engines.jev import JevEngine, JevError
        j = cfg.get("decision.jev", {})
        try:
            engine = JevEngine(j["endpoint"], j["model"], j.get("api_key_env", "TYPESAFE_API_KEY"),
                               float(j.get("timeout_sec", 3.0)))
            engine.warmup()
            engine.start_keepalive()
        except JevError as e:
            log.error("%s — キーワード照合だけで動作します", e)
            engine = None

    extra_vocab = ([str(w) for w in (cfg.get("vocabulary", []) or [])]
                   + [str(v) for v in (cfg.get("dictionary", {}) or {}).values()]
                   + [a for als in (cfg.get("app_aliases", {}) or {}).values() for a in als])
    stt = SpeechRecognizer(cfg.get("stt.model", "large-v3-turbo"), str(cfg.get("stt.gpu_name", "4080")),
                           cfg.get("stt.compute_type", "float16"), cfg.get("stt.language", "ja"),
                           int(cfg.get("stt.beam_size", 1)), extra_vocab,
                           str(cfg.get("stt.device", "auto")))
    mic_error = None
    try:
        recorder = Recorder(cfg.get("audio.device"))
        recorder.open()
    except Exception as e:  # マイクがなくてもアプリ自体は起動し、状態表示で知らせる
        log.error("マイクを開けません: %s", e)
        mic_error = str(e)
        recorder = _NoRecorder()

    remote = None
    ctl_ui = ui
    if cfg.get("remote_mic.enabled", True):
        from .remote_mic import RemoteMicServer, UiTee
        remote = RemoteMicServer(None, int(cfg.get("remote_mic.port", 8765)))
        ctl_ui = UiTee(ui, remote)  # 状態や文字起こしをブラウザにも流す
    ctl = Controller(cfg, ctl_ui, recorder, stt, engine, catalog)
    ctl.start()
    if cfg.get("debug.inbox", False):
        from . import debug_inbox
        debug_inbox.start(ctl, int(cfg.get("debug.inbox_port", 8766)))
    if remote:
        remote.controller = ctl
        ctl.remote = remote
        try:
            remote.start()
        except Exception:
            log.exception("ブラウザマイクを開始できませんでした")
            remote = None
    hook = KeyHook(cfg.get("hotkeys.push_to_talk", "right_ctrl"), cfg.get("hotkeys.emergency_stop", "pause"),
                   bool(cfg.get("hotkeys.suppress_push_to_talk", True)),
                   ctl.ptt_down, ctl.ptt_up, ctl.emergency_stop, ctl.confirm, ctl.is_confirming)
    hook.start()

    # ---- トレイ ----
    # トレイのアイコン：画面のオーブと同じオーロラの球に、白いマイク
    from PySide6.QtCore import QPointF, Qt, QTimer
    from PySide6.QtGui import QPen, QRadialGradient
    from .overlay import _aurora_gradient
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(_aurora_gradient(QPointF(32, 32), 40))
    p.drawEllipse(3, 3, 58, 58)
    hi = QRadialGradient(QPointF(22, 18), 40)
    hi.setColorAt(0.0, QColor(255, 255, 255, 150))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(hi)
    p.drawEllipse(3, 3, 58, 58)
    p.setBrush(QColor(255, 255, 255))
    p.drawRoundedRect(25, 13, 14, 24, 7, 7)
    pen = QPen(QColor(255, 255, 255), 4)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(18, 20, 28, 26, 180 * 16, 180 * 16)
    p.drawLine(32, 46, 32, 52)
    p.end()
    tray = QSystemTrayIcon(QIcon(pm))
    menu = QMenu()
    act_pause = QAction("一時停止", menu, checkable=True)
    act_pause.toggled.connect(ctl.set_paused)
    menu.addAction(act_pause)
    act_wake = QAction("ウェイクワードで開始（ハンズフリー）", menu, checkable=True)
    act_wake.setChecked(bool(cfg.get("wake_word.enabled", True)) and not mic_error)
    act_wake.setEnabled(not mic_error)
    act_wake.toggled.connect(lambda on: ctl.set_wake_enabled(on))
    menu.addAction(act_wake)
    if remote:
        def copy_url(url):
            qapp.clipboard().setText(url)
            tray.showMessage("voicectl", "ブラウザマイクの URL をコピーしました", QSystemTrayIcon.Information, 3000)
        sub = menu.addMenu("ブラウザマイクの URL をコピー")
        for url in remote.urls:
            sub.addAction(url.split("/?")[0].replace("https://", ""), lambda u=url: copy_url(u))
    menu.addAction("アプリ一覧を更新", catalog.refresh_async)

    def _open_settings():
        from .settings_ui import open_settings

        def restart():
            # 既定の起動スクリプトで再起動する（設定の変更を確実に反映するため）
            os.startfile(str(ROOT / "run.bat"))
            qapp.quit()

        open_settings(cfg, ctl, restart)

    menu.addAction("設定", _open_settings)

    def _open_wizard():
        from .settings_ui import open_wizard

        def restart():
            os.startfile(str(ROOT / "run.bat"))
            qapp.quit()

        open_wizard(cfg, ctl, restart)

    menu.addAction("初回セットアップ", _open_wizard)

    def _show_learning():
        ctl.show_learning()

    def _write_learning():
        path = ctl.write_learning_suggestions()
        tray.showMessage("voicectl",
                         "学習の候補を書き出しました" if path else "学習は無効です（config.yaml の learning.enabled）",
                         QSystemTrayIcon.Information, 4000)
        if path:
            os.startfile(path)

    menu.addAction("覚えている言い方を見る", _show_learning)
    menu.addAction("学習の候補を書き出す", _write_learning)
    menu.addAction("ログフォルダを開く", lambda: os.startfile(ROOT / cfg.get("logging.dir", "logs")))
    menu.addAction("設定ファイルを開く", lambda: os.startfile(cfg.path))
    menu.addSeparator()
    menu.addAction("終了", qapp.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("voicectl（音声操作）")
    tray.show()

    ptt = cfg.get("hotkeys.push_to_talk", "right_ctrl")
    mode = "Jev" if engine else "キーワード照合のみ"
    if mic_error:
        status.set_status("error", "マイクが使えません", [mic_error[:40], "config.yaml の audio.device を確認してください"])
    else:
        lines = [f"{ptt} を押しながら話してください"]
        if cfg.get("wake_word.enabled", True):
            from .wake import DEFAULT_WORDS
            lines.insert(0, f"「{DEFAULT_WORDS[0]}」と声をかけても開始します")
        status.set_status("idle", "待機中", lines + [f"判定: {mode}"], 6.0)
    log.info("起動完了（判定: %s、マイク: %s）", mode, "なし" if mic_error else "あり")

    # API キーがまだ無いときは、初回セットアップのウィザードを一度だけ開く
    from .settings_ui import has_api_key
    if not has_api_key():
        QTimer.singleShot(1200, _open_wizard)

    code = qapp.exec()
    hook.stop()
    recorder.close()
    if engine:
        engine.close()
    return code
