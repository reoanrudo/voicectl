"""設定ウィンドウ（配布用）。トレイメニューの「設定」から開く。

config.yaml を読み書きする（CLI からの編集と共存。保存後に再起動で反映）。
学習した言い方とルーチンの削除だけは、その場で反映される。
モジュール最上部の関数は Qt なしで動く（テストから呼べるように分けてある）。
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# 配置のひな形に選べる場所（schema.SNAP_RECTS / 最大化）
LAYOUT_PLACES = ["left_half", "right_half", "upper_half", "lower_half", "full",
                 "top_left", "top_right", "bottom_left", "bottom_right", "center", "maximize"]
PLACE_LABELS = {"left_half": "左半分", "right_half": "右半分", "upper_half": "上半分", "lower_half": "下半分",
                "full": "作業領域いっぱい", "top_left": "左上", "top_right": "右上",
                "bottom_left": "左下", "bottom_right": "右下", "center": "中央", "maximize": "最大化"}


def layout_to_rows(layouts: dict | None) -> list[tuple[str, str, str]]:
    """config の layouts をテーブルの行 (配置名, アプリ, 場所) に直す。"""
    rows = []
    for name, entries in (layouts or {}).items():
        for e in entries or []:
            if isinstance(e, dict):
                rows.append((str(name), str(e.get("app", "")), str(e.get("place", "full"))))
    return rows


def group_layout_rows(rows: list[tuple[str, str, str]]) -> dict[str, list[dict]]:
    """テーブルの行を config の layouts の形に戻す（空行・未入力は捨てる。配置名の順序は保つ）。"""
    out: dict[str, list[dict]] = {}
    for name, app, place in rows:
        name, app = name.strip(), app.strip()
        place = place.strip() or "full"
        if not name or not app:
            continue
        out.setdefault(name, []).append({"app": app, "place": place if place in LAYOUT_PLACES else "full"})
    return out


def parse_roots(text: str) -> list[str]:
    """「S:\\;D:\\data」のような入力を files.extra_roots のリストにする。"""
    import re
    return [p.strip() for p in re.split(r"[;、,]", text or "") if p.strip()]


def setx_api_key(key: str) -> str | None:
    """環境変数 TYPESAFE_API_KEY をユーザー環境変数に登録する。成功は None、失敗はエラー文。"""
    import subprocess
    try:
        subprocess.run(["setx", "TYPESAFE_API_KEY", key], check=True, capture_output=True)
        return None
    except Exception as e:
        log.warning("API キーの環境変数登録に失敗")
        return str(e)[:80]


def has_api_key() -> bool:
    """API キーが（プロセスの環境かユーザー環境変数に）あるか。値は見ない。"""
    import os
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return bool(winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0])
    except OSError:
        return False


def apply_changes(raw: dict, changes: list[tuple[str, object]]) -> dict:
    """(ドット区切りパス, 値) のリストを設定辞書に反映する（足りない階層は作る）。"""
    for dotted, value in changes:
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
    return raw


def _device_choices() -> list[tuple[str | None, str]]:
    """使える入力デバイスの一覧（(指定値, 表示)。「（既定）」は None）。"""
    import sounddevice as sd
    out: list[tuple[str | None, str]] = [(None, "（既定のマイク）")]
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append((str(d["name"]), f"{i}: {d['name']}"))
    return out


def probe_level(device_spec) -> float:
    """選んだマイクで約 1.2 秒録音して音量（RMS）を返す。だめなときは例外。"""
    import time

    import numpy as np
    import sounddevice as sd
    from .audio import _find_device
    idx = _find_device(device_spec)
    sr = int(sd.query_devices(idx)["default_samplerate"])
    rec = sd.rec(int(1.2 * sr), samplerate=sr, channels=1, device=idx, dtype="float32")
    sd.wait()
    time.sleep(0.05)
    return float(np.sqrt(np.mean(rec ** 2)))


def open_wizard(cfg, ctl, on_restart) -> None:
    """初回セットアップのウィザードを開く（すでに開いていれば手前に出す）。"""
    global _wizard
    try:
        if _wizard is not None and _wizard.isVisible():
            _wizard.raise_()
            _wizard.activateWindow()
            return
    except RuntimeError:
        _wizard = None
    _wizard = SetupWizard(cfg, ctl, on_restart)
    _wizard.show()


_wizard = None


class SetupWizard:
    """初回セットアップ（QWizard）。マイク→キー→API キー→GPU の確認→完了。"""

    def __init__(self, cfg, ctl, on_restart):
        from PySide6.QtWidgets import QWizard
        self.cfg, self.ctl, self.on_restart = cfg, ctl, on_restart
        self.w = QWizard()
        self.w.setWindowTitle("voicectl 初回セットアップ")
        self.w.addPage(self._p_welcome())
        self.w.addPage(self._p_mic())
        self.w.addPage(self._p_keys())
        self.w.addPage(self._p_api())
        self.w.addPage(self._p_gpu())
        self.w.addPage(self._p_done())
        self.w.resize(620, 460)

    def _p_welcome(self):
        from PySide6.QtWidgets import QLabel, QWizardPage, QVBoxLayout
        p = QWizardPage()
        p.setTitle("voicectl へようこそ")
        v = QVBoxLayout(p)
        for t in ("F5 を押している間に話すと、PC を声で操作できます。",
                  "この画面でマイク・キー・API キーを確かめます（あとからトレイの「設定」で変えられます）。",
                  "※ 一部の機能は Jev の API キー（TYPESAFE_API_KEY）が必要です。"):
            lab = QLabel(t)
            lab.setWordWrap(True)
            v.addWidget(lab)
        return p

    def _p_mic(self):
        from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWizardPage
        p = QWizardPage()
        p.setTitle("マイク")
        v = QVBoxLayout(p)
        self.mic = QComboBox()
        for value, label in _device_choices():
            self.mic.addItem(label, value)
        cur = self.cfg.get("audio.device")
        i = self.mic.findData(cur if cur else None)
        self.mic.setCurrentIndex(max(0, i))
        v.addWidget(QLabel("使うマイクを選んで「テスト」を押し、話してみてください。"))
        h = QHBoxLayout()
        h.addWidget(self.mic, 1)
        self.mic_test = QPushButton("テスト")
        self.mic_test.clicked.connect(self._test_mic)
        h.addWidget(self.mic_test)
        v.addLayout(h)
        self.mic_label = QLabel("")
        self.mic_label.setWordWrap(True)
        v.addWidget(self.mic_label)
        return p

    def _test_mic(self):
        from PySide6.QtWidgets import QApplication
        self.mic_test.setEnabled(False)
        self.mic_label.setText("録音中…（1.2 秒）")
        QApplication.processEvents()
        try:
            lvl = probe_level(self.mic.currentData())
            self.mic_label.setText(f"レベル {lvl:.3f} — " + ("声が届いています" if lvl >= 0.005
                                else "音がほとんど入っていません（別のマイクを選んでください）"))
        except Exception as e:
            self.mic_label.setText(f"録音できませんでした: {str(e)[:60]}")
        finally:
            self.mic_test.setEnabled(True)

    def _p_keys(self):
        from PySide6.QtWidgets import QComboBox, QLabel, QVBoxLayout, QWizardPage
        from .keys import KEY_VK
        p = QWizardPage()
        p.setTitle("話しかけるキー")
        v = QVBoxLayout(p)
        self.ptt = QComboBox()
        self.ptt.addItems(sorted(KEY_VK))
        self.ptt.setCurrentText(str(self.cfg.get("hotkeys.push_to_talk", "f5")))
        v.addWidget(QLabel("押している間だけ聞き取るキー"))
        v.addWidget(self.ptt)
        self.stop = QComboBox()
        self.stop.addItems(sorted(KEY_VK))
        self.stop.setCurrentText(str(self.cfg.get("hotkeys.emergency_stop", "pause")))
        v.addWidget(QLabel("緊急停止キー（すべて止めます）"))
        v.addWidget(self.stop)
        return p

    def _p_api(self):
        from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWizardPage
        p = QWizardPage()
        p.setTitle("Jev の API キー")
        v = QVBoxLayout(p)
        v.addWidget(QLabel("TYPESAFE_API_KEY を貼り付けてください（表示は隠れます。ファイルやログには書きません）。"))
        self.api = QLineEdit()
        self.api.setEchoMode(QLineEdit.Password)
        v.addWidget(self.api)
        v.addWidget(QLabel("※ なくても起動しますが、判定の一部（Jev）と文章アシスタントが使えません。"))
        v.addStretch(1)
        return p

    def _p_gpu(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWizardPage
        p = QWizardPage()
        p.setTitle("音声認識の確認")
        v = QVBoxLayout(p)
        name = ""
        try:
            import pynvml
            pynvml.nvmlInit()
            i = 0
            while True:
                try:
                    h = pynvml.nvmlDeviceGetHandleByIndex(i)
                except Exception:
                    break
                n = pynvml.nvmlDeviceGetName(h)
                name += (n.decode() if isinstance(n, bytes) else n) + " / "
                i += 1
                if i > 8:
                    break
        except Exception:
            name = ""
        want = str(self.cfg.get("stt.gpu_name", "4080"))
        if want.lower() in name.lower():
            v.addWidget(QLabel(f"GPU「{want}」が見つかりました。音声認識はこの GPU で速く動きます。"))
        elif name:
            v.addWidget(QLabel(f"GPU: {name.strip(' / ')}\n指定の「{want}」は見つかりませんでした。"
                               "設定の stt.gpu_name を変えると GPU を使えます（なければ CPU で動きます）。"))
        else:
            v.addWidget(QLabel("NVIDIA GPU が見つかりません。音声認識は CPU で動きます（応答が数秒かかることがあります）。"))
        self.gpu_text = name
        v.addStretch(1)
        return p

    def _p_done(self):
        from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWizardPage
        p = QWizardPage()
        p.setTitle("完了")
        v = QVBoxLayout(p)
        v.addWidget(QLabel("設定を保存します。「F5 を押しながら話してください」と画面に出たら準備完了です。"))
        self.restart_cb = QCheckBox("保存して voicectl を再起動する（おすすめ）")
        self.restart_cb.setChecked(True)
        v.addWidget(self.restart_cb)
        v.addWidget(QLabel("詳しい言い方は README.md、あとからの変更はトレイの「設定」で。"))
        v.addStretch(1)
        return p

    def _finish(self):
        changes = [
            ("hotkeys.push_to_talk", self.ptt.currentText()),
            ("hotkeys.emergency_stop", self.stop.currentText()),
            ("audio.device", self.mic.currentData()),
        ]
        apply_changes(self.cfg.raw, changes)
        key = self.api.text().strip()
        if key:
            setx_api_key(key)
        self.cfg.save()
        log.info("初回セットアップを完了しました")
        if self.restart_cb.isChecked() and self.on_restart:
            self.on_restart()


def open_settings(cfg, ctl, on_restart) -> None:
    """設定ウィンドウを開く（すでに開いていれば手前に出す）。"""
    global _dialog
    try:
        if _dialog is not None and _dialog.isVisible():
            _dialog.raise_()
            _dialog.activateWindow()
            return
    except RuntimeError:
        _dialog = None   # C++ 側が先に消えた
    _dialog = SettingsDialog(cfg, ctl, on_restart)
    _dialog.show()


_dialog: "SettingsDialog | None" = None


class SettingsDialog:
    """Qt のダイアログ。使うときだけクラスを定義する（テストは上部の関数だけを使う）。"""

    def __init__(self, cfg, ctl, on_restart):
        from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                                       QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                                       QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
                                       QVBoxLayout, QWidget)
        from PySide6.QtGui import QFont

        self.cfg, self.ctl, self.on_restart = cfg, ctl, on_restart
        self.raw = cfg.raw
        self.d = QDialog()
        self.d.setWindowTitle("voicectl の設定")
        self.d.resize(640, 540)
        tabs = QTabWidget()
        tabs.addTab(self._tab_mic_keys(), "マイク・キー")
        tabs.addTab(self._tab_decision(), "判定")
        tabs.addTab(self._tab_layouts(), "窓と配置")
        tabs.addTab(self._tab_learned(), "覚えたもの")
        tabs.addTab(self._tab_link(), "連携")

        bottom = QHBoxLayout()
        note = QLabel("変更の反映には再起動が必要です（学習とルーチンの削除は即時）")
        note.setWordWrap(True)
        b_save = QPushButton("保存")
        b_save.clicked.connect(self._save)
        b_restart = QPushButton("保存して再起動")
        b_restart.clicked.connect(self._save_restart)
        b_close = QPushButton("閉じる")
        b_close.clicked.connect(self.d.close)
        for b in (b_close, b_save, b_restart):
            bottom.addWidget(b)
        bottom.insertWidget(0, note, 1)

        root = QVBoxLayout(self.d)
        root.addWidget(tabs)
        root.addLayout(bottom)

    # ---- タブ作成 ----
    def _tab_mic_keys(self):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
        from .keys import KEY_VK
        w = QVBoxLayout()
        w.addWidget(QLabel("話しかけるキー（押している間だけ聞き取ります）"))
        self.ptt = QComboBox()
        self.ptt.addItems(sorted(KEY_VK))
        self.ptt.setCurrentText(str(self.cfg.get("hotkeys.push_to_talk", "f5")))
        w.addWidget(self.ptt)
        w.addWidget(QLabel("緊急停止キー"))
        self.stop = QComboBox()
        self.stop.addItems(sorted(KEY_VK))
        self.stop.setCurrentText(str(self.cfg.get("hotkeys.emergency_stop", "pause")))
        w.addWidget(self.stop)
        w.addWidget(QLabel("マイク"))
        h = QHBoxLayout()
        self.mic = QComboBox()
        for value, label in _device_choices():
            self.mic.addItem(label, value)
        cur = self.cfg.get("audio.device")
        i = self.mic.findData(cur if cur else None)
        self.mic.setCurrentIndex(max(0, i))
        h.addWidget(self.mic, 1)
        self.mic_test = QPushButton("テスト")
        self.mic_test.clicked.connect(self._test_mic)
        h.addWidget(self.mic_test)
        w.addLayout(h)
        self.mic_label = QLabel("「テスト」で録音して音量を確かめます（話しかけている最中に押してください）")
        self.mic_label.setWordWrap(True)
        w.addWidget(self.mic_label)
        self.auto_seg = QCheckBox("言葉が途切れたら、キーを離すのを待たずに実行する")
        self.auto_seg.setChecked(bool(self.cfg.get("listen.auto_segment", True)))
        w.addWidget(self.auto_seg)
        w.addStretch(1)
        tab = QWidget()
        tab.setLayout(w)
        return tab

    def _tab_decision(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget
        w = QVBoxLayout()
        self._slider_labels = {}
        for label, path, lo, hi, dflt in (
                ("実行する確信度（これ以上で確認なしに実行）", "thresholds.execute", 0.5, 1.0, 0.8),
                ("確認する確信度（これ以上なら確認して実行）", "thresholds.confirm", 0.3, 0.9, 0.5)):
            v = float(self.cfg.get(path, dflt))
            lab = QLabel(f"{label}: {v:.2f}")
            self._slider_labels[path] = (label, lab)
            w.addWidget(lab)
            s = QSlider(Qt.Horizontal)
            s.setRange(int(lo * 100), int(hi * 100))
            s.setValue(int(v * 100))
            s.valueChanged.connect(self._slider_moved)
            setattr(self, path.replace(".", "_") + "_slider", s)
            w.addWidget(s)
        self.cb_llm = QCheckBox("Jev で決まらないとき LLM に言い直させる（DeepSeek）")
        self.cb_llm.setChecked(bool(self.cfg.get("decision.llm_fallback.enabled", True)))
        w.addWidget(self.cb_llm)
        self.cb_ai = QCheckBox("文章アシスタント（メールの下書き・要約・質問への回答）")
        self.cb_ai.setChecked(bool(self.cfg.get("assistant.enabled", True)))
        w.addWidget(self.cb_ai)
        self.cb_voice = QCheckBox("答えを読み上げる（今は無効がおすすめ）")
        self.cb_voice.setChecked(bool(self.cfg.get("voice_reply.enabled", False)))
        w.addWidget(self.cb_voice)
        self.cb_clock = QCheckBox("アラームは標準のクロックアプリに設定する")
        self.cb_clock.setChecked(bool(self.cfg.get("alarm.use_clock_app", True)))
        w.addWidget(self.cb_clock)
        self.cb_wake = QCheckBox("待機中の呼びかけ（ウェイクワード）を有効にする")
        self.cb_wake.setChecked(bool(self.cfg.get("wake_word.enabled", False)))
        w.addWidget(self.cb_wake)
        w.addStretch(1)
        tab = QWidget()
        tab.setLayout(w)
        return tab

    def _slider_moved(self, value):
        from PySide6.QtWidgets import QSlider
        s = self.sender()
        if not isinstance(s, QSlider):
            return
        for path, (label, lab) in self._slider_labels.items():
            if getattr(self, path.replace(".", "_") + "_slider") is s:
                lab.setText(f"{label}: {value / 100:.2f}")
                break

    def _tab_layouts(self):
        from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                                       QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                                       QHeaderView)
        w = QVBoxLayout()
        w.addWidget(QLabel("配置のひな形（「開発の配置」のように呼びます。アプリは窓のタイトルやプロセス名に含まれる言葉）"))
        self.layout_table = QTableWidget(0, 3)
        self.layout_table.setHorizontalHeaderLabels(["配置名", "アプリ", "場所"])
        for name, app, place in layout_to_rows(self.cfg.get("layouts")):
            r = self.layout_table.rowCount()
            self.layout_table.insertRow(r)
            self.layout_table.setItem(r, 0, QTableWidgetItem(name))
            self.layout_table.setItem(r, 1, QTableWidgetItem(app))
            cb = QComboBox()
            cb.addItems([f"{p}（{PLACE_LABELS[p]}）" for p in LAYOUT_PLACES])
            cb.setCurrentIndex(LAYOUT_PLACES.index(place) if place in LAYOUT_PLACES else 4)
            self.layout_table.setCellWidget(r, 2, cb)
        self.layout_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        w.addWidget(self.layout_table, 1)
        h = QHBoxLayout()
        b_add = QPushButton("行を追加")
        b_add.clicked.connect(lambda: self.layout_table.insertRow(self.layout_table.rowCount()))
        b_del = QPushButton("行を削除")
        b_del.clicked.connect(lambda: self.layout_table.removeRow(self.layout_table.currentRow()))
        h.addWidget(b_add)
        h.addWidget(b_del)
        h.addStretch(1)
        w.addLayout(h)
        h2 = QHBoxLayout()
        h2.addWidget(QLabel("マウス移動量（小・中・大）"))
        self.step_spins = []
        for key in ("small", "medium", "large"):
            sp = QSpinBox()
            sp.setRange(10, 800)
            sp.setValue(int((self.cfg.get("mouse.step_px") or {}).get(key, 60 if key == "small" else 120 if key == "medium" else 300)))
            self.step_spins.append(sp)
            h2.addWidget(sp)
        w.addLayout(h2)
        h3 = QHBoxLayout()
        h3.addWidget(QLabel("スクロール量（小・中・大）"))
        self.scroll_spins = []
        for key in ("small", "medium", "large"):
            sp = QSpinBox()
            sp.setRange(1, 30)
            sp.setValue(int((self.cfg.get("mouse.scroll_notches") or {}).get(key, 2 if key == "small" else 5 if key == "medium" else 12)))
            self.scroll_spins.append(sp)
            h3.addWidget(sp)
        w.addLayout(h3)
        w.addWidget(QLabel("ファイル検索の追加場所（; 区切り。既定はユーザーのフォルダ）"))
        self.roots = QLineEdit("; ".join(self.cfg.get("files.extra_roots") or []))
        w.addWidget(self.roots)
        tab = QWidget()
        tab.setLayout(w)
        return tab

    def _tab_learned(self):
        from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTableWidget,
                                       QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView)
        w = QVBoxLayout()
        w.addWidget(QLabel("学習した言い方（削除はすぐ反映されます。成功が 3 回以上で確認なしに実行）"))
        self.learned_table = QTableWidget(0, 4)
        self.learned_table.setHorizontalHeaderLabels(["言い方", "操作", "成功", "失敗"])
        self._fill_learned()
        self.learned_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        w.addWidget(self.learned_table, 1)
        h = QHBoxLayout()
        b_del = QPushButton("選んだ言い方を忘れる")
        b_del.clicked.connect(self._forget_learned)
        h.addWidget(b_del)
        h.addStretch(1)
        w.addLayout(h)
        w.addWidget(QLabel("ルーチン（声で登録した手順。削除はすぐ反映されます）"))
        self.routine_table = QTableWidget(0, 2)
        self.routine_table.setHorizontalHeaderLabels(["名前", "手順の数"])
        for name, steps in ((self.ctl.routines if self.ctl else {}) or {}).items():
            r = self.routine_table.rowCount()
            self.routine_table.insertRow(r)
            self.routine_table.setItem(r, 0, QTableWidgetItem(str(name)))
            self.routine_table.setItem(r, 1, QTableWidgetItem(str(len(steps))))
        self.routine_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        w.addWidget(self.routine_table, 1)
        h2 = QHBoxLayout()
        b_del2 = QPushButton("選んだルーチンを削除")
        b_del2.clicked.connect(self._forget_routine)
        h2.addWidget(b_del2)
        h2.addStretch(1)
        w.addLayout(h2)
        tab = QWidget()
        tab.setLayout(w)
        return tab

    def _tab_link(self):
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
        w = QVBoxLayout()
        w.addWidget(QLabel("Jev の API キー（TYPESAFE_API_KEY。表示は隠れます。保存して環境変数に登録してください）"))
        h = QHBoxLayout()
        self.api = QLineEdit()
        self.api.setEchoMode(QLineEdit.Password)
        h.addWidget(self.api, 1)
        b = QPushButton("環境変数に登録")
        b.clicked.connect(self._set_api_key)
        h.addWidget(b)
        w.addLayout(h)
        w.addWidget(QLabel("※ 登録後は voicectl の再起動が必要です。キーはファイルやログに書きません。"))
        w.addWidget(QLabel("プライバシー: 音声の認識はこの PC 内（Whisper）。判定は Jev へ、文章の作成は DeepSeek へ送られます。"))
        w.addStretch(1)
        tab = QWidget()
        tab.setLayout(w)
        return tab

    # ---- 動作 ----
    def _fill_learned(self):
        from PySide6.QtWidgets import QTableWidgetItem
        self.learned_table.setRowCount(0)
        entries = getattr(self.ctl.learner, "entries", {}) if self.ctl else {}
        for e in entries.values():
            r = self.learned_table.rowCount()
            self.learned_table.insertRow(r)
            self.learned_table.setItem(r, 0, QTableWidgetItem(e.say[:40]))
            self.learned_table.setItem(r, 1, QTableWidgetItem(e.action))
            self.learned_table.setItem(r, 2, QTableWidgetItem(str(e.hits)))
            self.learned_table.setItem(r, 3, QTableWidgetItem(str(e.misses)))

    def _forget_learned(self):
        r = self.learned_table.currentRow()
        if r < 0:
            return
        say = self.learned_table.item(r, 0).text()
        if self.ctl and self.ctl.learner is not None:
            forgotten = self.ctl.learner.forget(say)
            log.info("設定ウィンドウから学習を削除: %s", forgotten or say)
        self._fill_learned()

    def _forget_routine(self):
        from . import intents
        r = self.routine_table.currentRow()
        if r < 0 or not self.ctl:
            return
        name = self.routine_table.item(r, 0).text()
        intents.save_user_routine(name, None)
        self.ctl.routines = intents.load_routines(self.ctl.cfg.get("routines", {}))
        self.routine_table.removeRow(r)

    def _test_mic(self):
        from PySide6.QtWidgets import QApplication
        self.mic_test.setEnabled(False)
        self.mic_label.setText("録音中…（1.2 秒）")
        QApplication.processEvents()
        try:
            lvl = probe_level(self.mic.currentData())
            ok = "声が届いています" if lvl >= 0.005 else "音がほとんど入っていません（マイク名や設定を確認）"
            self.mic_label.setText(f"レベル {lvl:.3f} — {ok}")
        except Exception as e:
            self.mic_label.setText(f"録音できませんでした: {str(e)[:60]}")
        finally:
            self.mic_test.setEnabled(True)

    def _set_api_key(self):
        from PySide6.QtWidgets import QMessageBox
        key = self.api.text().strip()
        if not key:
            QMessageBox.information(self.d, "voicectl", "キーを入力してください")
            return
        err = setx_api_key(key)
        if err:
            QMessageBox.warning(self.d, "voicectl", f"登録できませんでした: {err}")
        else:
            QMessageBox.information(self.d, "voicectl", "環境変数に登録しました。voicectl の再起動後に使えます。")

    def _collect(self):
        from PySide6.QtWidgets import QComboBox, QTableWidgetItem
        changes = []
        changes.append(("hotkeys.push_to_talk", self.ptt.currentText()))
        changes.append(("hotkeys.emergency_stop", self.stop.currentText()))
        changes.append(("audio.device", self.mic.currentData()))
        changes.append(("listen.auto_segment", self.auto_seg.isChecked()))
        changes.append(("thresholds.execute", self.thresholds_execute_slider.value() / 100))
        changes.append(("thresholds.confirm", self.thresholds_confirm_slider.value() / 100))
        changes.append(("decision.llm_fallback.enabled", self.cb_llm.isChecked()))
        changes.append(("assistant.enabled", self.cb_ai.isChecked()))
        changes.append(("voice_reply.enabled", self.cb_voice.isChecked()))
        changes.append(("alarm.use_clock_app", self.cb_clock.isChecked()))
        changes.append(("wake_word.enabled", self.cb_wake.isChecked()))
        rows = []
        for r in range(self.layout_table.rowCount()):
            item0 = self.layout_table.item(r, 0)
            item1 = self.layout_table.item(r, 1)
            place_cb = self.layout_table.cellWidget(r, 2)
            if item0 is None or item1 is None or place_cb is None:
                continue
            rows.append((item0.text(), item1.text(), LAYOUT_PLACES[place_cb.currentIndex()]))
        changes.append(("layouts", group_layout_rows(rows)))
        for sp, key in zip(self.step_spins, ("small", "medium", "large")):
            changes.append((f"mouse.step_px.{key}", sp.value()))
        for sp, key in zip(self.scroll_spins, ("small", "medium", "large")):
            changes.append((f"mouse.scroll_notches.{key}", sp.value()))
        changes.append(("files.extra_roots", parse_roots(self.roots.text())))
        apply_changes(self.raw, changes)

    def _save(self):
        from PySide6.QtWidgets import QMessageBox
        try:
            self._collect()
            self.cfg.save()
            QMessageBox.information(self.d, "voicectl", "保存しました。変更の反映には再起動が必要です。")
        except Exception as e:
            log.exception("設定の保存に失敗")
            QMessageBox.warning(self.d, "voicectl", f"保存できませんでした: {str(e)[:80]}")

    def _save_restart(self):
        from PySide6.QtWidgets import QMessageBox
        try:
            self._collect()
            self.cfg.save()
        except Exception as e:
            log.exception("設定の保存に失敗")
            QMessageBox.warning(self.d, "voicectl", f"保存できませんでした: {str(e)[:80]}")
            return
        self.d.close()
        if self.on_restart:
            self.on_restart()
