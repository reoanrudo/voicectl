"""画面表示：状態カプセル（画面の隅）・文字起こしバー・番号ラベル／番号グリッドのヒント表示。

どれもフォーカスを奪わず、クリックも透過する（操作対象のウィンドウを前面に保つため）。
QT_ENABLE_HIGHDPI_SCALING=0 で起動し、Qt の座標 = 物理ピクセルとして扱う。

見た目は「オーロラガラス」：
  - 黒に近い半透明のカプセルに、ごく細い白の縁取りと上端の光沢（ガラスの質感）
  - 聞き取り中・処理中は、縁に沿って虹色のオーロラ光がゆっくり回る（声の大きさで明るさが変わる）
  - 左端のオーブが状態を表す（聞き取り＝声に合わせて脈打つ虹色の球、処理中＝回転するリング、
    実行＝チェック、確認＝？、エラー＝！、一時停止＝‖）
  - 表示の切り替わりは、少し拡大しながらふわっと出る
位置はモニター全体ではなく「作業領域」（タスクバーを除いた範囲。winutil.work_area_at）を基準にする。
"""
from __future__ import annotations

import collections
import ctypes
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import winutil

_FLAGS = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
          | Qt.WindowDoesNotAcceptFocus)

# 影と光を描くための外側の余白。ウィジェットはこの分だけ大きくとる
_PAD = 18

# オーロラの色（ピンク → オレンジ → 紫 → 青 → ミント → ピンク）
AURORA = [QColor(255, 94, 138), QColor(255, 170, 92), QColor(150, 102, 255), QColor(64, 170, 255),
          QColor(70, 230, 190), QColor(255, 94, 138)]

# 状態ごとのアクセント色（落ち着いた彩度。オーロラを使わない状態の光とオーブに使う）
STATE_COLORS = {
    "idle": QColor(142, 152, 176), "listening": QColor(255, 94, 138), "processing": QColor(150, 102, 255),
    "done": QColor(62, 219, 160), "confirm": QColor(80, 160, 255), "retry": QColor(176, 132, 255),
    "error": QColor(255, 86, 96), "hints": QColor(64, 200, 240), "paused": QColor(130, 138, 156),
}
STATE_LABELS = {
    "idle": "待機", "listening": "聞き取り", "processing": "処理中", "done": "完了", "confirm": "確認",
    "retry": "もう一度", "error": "エラー", "hints": "ヒント", "paused": "一時停止",
}
_AURORA_STATES = ("listening", "processing")      # 縁のオーロラを回す状態
_ANIM_STATES = ("listening", "processing")        # 動かし続ける状態

_GLASS_TOP = QColor(34, 34, 42, 228)
_GLASS_BOTTOM = QColor(14, 14, 19, 238)
_TEXT = QColor(248, 248, 252)
_TEXT_DIM = QColor(172, 176, 192)
_TEXT_FAINT = QColor(128, 132, 150)


def _font(px: float, weight: int = 400, italic: bool = False) -> QFont:
    f = QFont()
    try:  # 欧文は Segoe UI Variable、和文は Noto Sans JP（なければ游ゴシック）
        f.setFamilies(["Segoe UI Variable Display", "Segoe UI Variable Text", "Noto Sans JP", "Yu Gothic UI",
                       "Meiryo UI"])
    except Exception:
        f.setFamily("Yu Gothic UI")
    f.setPixelSize(max(8, int(px)))
    try:
        f.setWeight(QFont.Weight(weight))
    except Exception:
        f.setBold(weight >= 600)
    f.setItalic(italic)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


def _alpha(c: QColor, a: float) -> QColor:
    return QColor(c.red(), c.green(), c.blue(), max(0, min(255, int(a))))


def _ease_out_back(t: float) -> float:
    """少し行き過ぎてから戻る（ばねのような）動き。"""
    c1 = 1.4
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _aurora_gradient(center: QPointF, angle: float, alpha: float = 255) -> QConicalGradient:
    g = QConicalGradient(center, angle)
    n = len(AURORA) - 1
    for i, c in enumerate(AURORA):
        g.setColorAt(i / n, _alpha(c, alpha))
    return g


def _shadow(p: QPainter, rect: QRectF, radius: float, strength: float = 1.0) -> None:
    """ぼかしの代わりに、薄い黒の角丸を重ねて柔らかい影にする（下に少しずらす）。"""
    p.setPen(Qt.NoPen)
    for i in range(7):
        grow = 2 + i * 2.2
        a = (34 - i * 4.4) * strength
        if a <= 0:
            break
        p.setBrush(QColor(0, 0, 0, int(a)))
        r = rect.adjusted(-grow, -grow + 5, grow, grow + 5)
        p.drawRoundedRect(r, radius + grow, radius + grow)


def _glass(p: QPainter, rect: QRectF, radius: float, glow_color: QColor | None, glow: float,
           aurora_angle: float | None) -> None:
    """ガラスのパネル：影 → 外側の光 → 本体 → 光沢 → 縁取り。aurora_angle があれば縁をオーロラにする。"""
    _shadow(p, rect, radius, 0.9)
    center = rect.center()
    # 外側の光（オーロラか、状態の色）
    if glow > 0.01:
        p.setBrush(Qt.NoBrush)
        for i, (grow, a) in enumerate(((1.5, 150), (4.0, 70), (7.5, 34), (11.5, 14))):
            if aurora_angle is not None:
                pen = QPen(_aurora_gradient(center, aurora_angle, a * glow), 2.2 + i * 1.3)
            else:
                pen = QPen(_alpha(glow_color, a * glow * 0.8), 2.0 + i * 1.3)
            p.setPen(pen)
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow)
    # 本体
    body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    body.setColorAt(0.0, _GLASS_TOP)
    body.setColorAt(1.0, _GLASS_BOTTOM)
    p.setPen(Qt.NoPen)
    p.setBrush(body)
    p.drawRoundedRect(rect, radius, radius)
    # 上半分の光沢
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + rect.height() * 0.55))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 20))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(sheen)
    p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
    # 縁取り（上が明るく下が暗い、ガラスの反射）
    edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    edge.setColorAt(0.0, QColor(255, 255, 255, 58))
    edge.setColorAt(1.0, QColor(255, 255, 255, 12))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(edge, 1.0))
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    if aurora_angle is not None and glow > 0.01:   # 縁そのものもオーロラに光らせる
        p.setPen(QPen(_aurora_gradient(center, aurora_angle, 210 * glow), 1.4))
        p.drawRoundedRect(rect.adjusted(0.7, 0.7, -0.7, -0.7), radius, radius)


def _orb(p: QPainter, c: QPointF, r: float, state: str, phase: float, level: float) -> None:
    """状態を表すオーブ。phase は 0→1 を繰り返す位相、level は声の大きさ（0〜1）。"""
    accent = STATE_COLORS.get(state, STATE_COLORS["idle"])
    p.setPen(Qt.NoPen)
    if state == "listening":
        # 声に合わせて広がる波紋
        for k in range(3):
            t = (phase + k / 3) % 1.0
            rr = r * (1.0 + 0.9 * t)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_alpha(AURORA[k * 2 % 5], 120 * (1 - t) * (0.4 + level)), 1.4))
            p.drawEllipse(c, rr, rr)
        rr = r * (0.82 + 0.28 * level)
        p.setPen(Qt.NoPen)
        p.setBrush(_aurora_gradient(c, -phase * 360 * 2))
        p.drawEllipse(c, rr, rr)
        hi = QRadialGradient(QPointF(c.x() - rr * 0.35, c.y() - rr * 0.4), rr * 1.1)
        hi.setColorAt(0.0, QColor(255, 255, 255, 170))
        hi.setColorAt(0.5, QColor(255, 255, 255, 30))
        hi.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hi)
        p.drawEllipse(c, rr, rr)
        return
    if state == "processing":
        # 回転するオーロラのリング
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawEllipse(c, r, r)
        pen = QPen(_aurora_gradient(c, -phase * 360), max(2.4, r * 0.28))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        rr = r * 0.78
        p.drawArc(QRectF(c.x() - rr, c.y() - rr, rr * 2, rr * 2), int(-phase * 360 * 16 * 1.5), 16 * 270)
        return
    # 静止した状態：色つきの球＋記号
    g = QRadialGradient(QPointF(c.x() - r * 0.3, c.y() - r * 0.35), r * 1.4)
    g.setColorAt(0.0, accent.lighter(135))
    g.setColorAt(1.0, accent.darker(125))
    p.setBrush(g)
    if state == "idle":
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawEllipse(c, r, r)
        p.setBrush(_alpha(accent, 230))
        p.drawEllipse(c, r * 0.34, r * 0.34)
        return
    p.drawEllipse(c, r, r)
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawEllipse(QPointF(c.x(), c.y() - r * 0.35), r * 0.62, r * 0.42)
    pen = QPen(QColor(255, 255, 255, 245), max(1.8, r * 0.17))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = r * 0.46
    if state == "done":
        path = QPainterPath(QPointF(c.x() - s, c.y() + s * 0.05))
        path.lineTo(c.x() - s * 0.28, c.y() + s * 0.7)
        path.lineTo(c.x() + s, c.y() - s * 0.6)
        p.drawPath(path)
    elif state == "error":
        p.drawLine(QPointF(c.x(), c.y() - s * 0.85), QPointF(c.x(), c.y() + s * 0.25))
        p.drawPoint(QPointF(c.x(), c.y() + s * 0.8))
    elif state == "paused":
        p.drawLine(QPointF(c.x() - s * 0.4, c.y() - s * 0.7), QPointF(c.x() - s * 0.4, c.y() + s * 0.7))
        p.drawLine(QPointF(c.x() + s * 0.4, c.y() - s * 0.7), QPointF(c.x() + s * 0.4, c.y() + s * 0.7))
    elif state == "retry":
        p.drawArc(QRectF(c.x() - s, c.y() - s, s * 2, s * 2), 16 * 60, 16 * 270)
        p.drawLine(QPointF(c.x() + s * 0.5, c.y() - s * 0.87), QPointF(c.x() + s * 0.95, c.y() - s * 0.35))
    else:   # confirm / hints
        p.setFont(_font(r * 1.15, 700))
        p.setPen(QColor(255, 255, 255, 245))
        p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), Qt.AlignCenter, "?" if state == "confirm" else "#")


def _bars(p: QPainter, rect: QRectF, levels: list[float], phase: float) -> None:
    """声の大きさの小さな波形（丸い縦棒 5 本・オーロラ色）。"""
    n = 5
    data = (levels or [0.0])[-n:]
    data = [0.0] * (n - len(data)) + data
    bw = rect.width() / (n * 1.9)
    grad = QLinearGradient(rect.topLeft(), rect.topRight())
    for i, col in enumerate(AURORA[:5]):
        grad.setColorAt(i / 4, col)
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    for i, v in enumerate(data):
        wob = 0.08 * (1 + math.sin((phase * 2 + i * 0.37) * math.tau))   # 無音でも少しだけ揺らす
        hh = max(bw, rect.height() * min(1.0, v + wob))
        x = rect.left() + i * bw * 1.9
        p.drawRoundedRect(QRectF(x, rect.center().y() - hh / 2, bw, hh), bw / 2, bw / 2)


def _no_activate(w: QWidget) -> None:
    hwnd = int(w.winId())
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x80 | 0x20)  # NOACTIVATE|TOOLWINDOW|TRANSPARENT


class _Animated(QWidget):
    """表示の出現（拡大＋フェード）と、動く状態の位相を管理する共通部分。"""

    def __init__(self, animate: bool):
        super().__init__(None, _FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.animate = bool(animate)
        self._phase = 0.0     # 0→1 を繰り返す
        self._appear = 1.0    # 出現のアニメーション（0→1）
        self._glow = 0.0      # 縁の光の強さ（なめらかに追従させる）
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._tick)

    def _wants_motion(self) -> bool:
        return False

    def _glow_target(self) -> float:
        return 0.0

    def _sync_timer(self) -> None:
        need = self.animate and (self._wants_motion() or self._appear < 1.0
                                 or abs(self._glow - self._glow_target()) > 0.02)
        if need and not self._anim.isActive():
            self._anim.start()
        elif not need and self._anim.isActive():
            self._anim.stop()
            self._glow = self._glow_target()
            self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.006) % 1.0
        if self._appear < 1.0:
            self._appear = min(1.0, self._appear + 0.075)
        self._glow += (self._glow_target() - self._glow) * 0.15
        self.update()
        self._sync_timer()

    def _begin_appear(self) -> None:
        self._appear = 0.0 if self.animate else 1.0
        if not self.animate:
            self._glow = self._glow_target()

    def _apply_appear(self, p: QPainter, rect: QRectF) -> QRectF:
        if self._appear >= 1.0:
            return rect
        t = self._appear
        p.setOpacity(min(1.0, 0.15 + t * 1.2))
        s = 0.94 + 0.06 * _ease_out_back(t)
        c = rect.center()
        w, h = rect.width() * s, rect.height() * s
        return QRectF(c.x() - w / 2, c.y() - h / 2 + (1 - t) * 6, w, h)


class StatusOverlay(_Animated):
    def __init__(self, corner: str = "bottom_right", font_px: int = 18, margin: int = 18,
                 avoid_taskbar: bool = True, animate: bool = True):
        super().__init__(animate)
        self.corner = corner
        self.font_px = font_px
        self.margin = int(margin)
        self.avoid_taskbar = bool(avoid_taskbar)
        self.state = "idle"
        self.title = "待機中"
        self.lines: list[str] = []
        self.level = 0.0
        self.idle_state, self.idle_title = "idle", "待機中"  # 表示が一定時間後に戻る先
        self.on_layout = []  # 位置が変わったときに呼ぶ（文字起こしバーをこの上に置くため）
        self._levels: collections.deque = collections.deque(maxlen=26)  # 波形用の音量履歴
        self._level_smooth = 0.0
        self._fade = QTimer(self, singleShot=True, timeout=self._to_idle)
        self._layout()
        self.show()
        _no_activate(self)

    # ---- 位置 ----
    def _work(self) -> tuple[int, int, int, int]:
        rect = winutil.work_area_at(0, 0) if self.avoid_taskbar else winutil.monitor_rect_at(0, 0)
        l, t, r, b = rect
        if self.avoid_taskbar and winutil.taskbar_auto_hide():
            b -= 8  # 自動的に隠す設定では作業領域＝モニター全体なので、下端に少し余裕を持たせる
        return l, t, r, b

    def _metrics(self) -> dict:
        f = self.font_px
        return {"pad": int(f * 0.8), "orb": int(f * 1.05), "title_h": int(f * 1.45), "lh": int(f * 1.32),
                "label_h": int(f * 0.95)}

    @property
    def full_w(self) -> int:
        """広げたときの幅（文字起こしバーもこの幅に揃える）。"""
        return int(self.font_px * 25) + _PAD * 2

    def _compact(self) -> bool:
        """待機中で補足がなければ、小さなピルにたたむ（画面の邪魔をしない）。"""
        return self.state in ("idle", "paused") and not self.lines

    def _layout(self) -> None:
        m = self._metrics()
        if self._compact():
            from PySide6.QtGui import QFontMetrics
            tw = QFontMetrics(_font(self.font_px * 0.9, 600)).horizontalAdvance(self.title)
            w = int(m["pad"] * 2.4 + m["orb"] * 1.2 + tw) + _PAD * 2
            h = int(self.font_px * 2.3) + _PAD * 2
        else:
            body = self._label_h() + m["title_h"] + m["lh"] * len(self.lines)
            panel_h = max(m["pad"] * 2 + body, int(m["orb"] * 1.7) + m["pad"] * 2)
            w = self.full_w
            h = panel_h + _PAD * 2
        x, y = winutil.corner_position(self._work(), w, h, self.corner, self.margin, keep_out=0)
        self.setGeometry(x, y, w, h)
        for cb in self.on_layout:
            cb()

    # ---- 状態 ----
    def set_idle(self, state: str, title: str) -> None:
        self.idle_state, self.idle_title = state, title
        if self.state in ("idle", "listening") and not self._fade.isActive():
            self.set_status(state, title)

    def set_status(self, state: str, title: str, lines: list[str] | None = None, hold_sec: float = 0) -> None:
        new_lines = [ln for ln in (lines or []) if ln][:4]
        if (state, title, new_lines) != (self.state, self.title, self.lines):
            changed_state = state != self.state
            self.state, self.title, self.lines = state, title, new_lines
            if changed_state:
                self._begin_appear()
            self._layout()
        self.update()
        self._sync_timer()
        if hold_sec > 0:
            self._fade.start(int(hold_sec * 1000))
        else:
            self._fade.stop()

    def set_level(self, level: float) -> None:
        self.level = float(level)
        if self.state == "listening":
            self._levels.append(min(1.0, max(0.0, self.level * 8)))

    def _to_idle(self) -> None:
        self.set_status(self.idle_state, self.idle_title)

    def _label_h(self) -> int:
        """状態の小さなラベルの高さ（見出しに同じ言葉があるときは出さない）。"""
        label = STATE_LABELS.get(self.state, "")
        return 0 if (not label or label in self.title) else self._metrics()["label_h"]

    def _wants_motion(self) -> bool:
        return self.state in _ANIM_STATES

    def _glow_target(self) -> float:
        if self.state == "listening":
            return 0.55 + 0.45 * self._level_smooth
        return {"processing": 0.7, "done": 0.45, "error": 0.6, "confirm": 0.5, "retry": 0.35,
                "hints": 0.35}.get(self.state, 0.0)

    def _tick(self) -> None:
        cur = self._levels[-1] if self._levels else 0.0
        self._level_smooth += (cur - self._level_smooth) * 0.25
        super()._tick()

    # ---- 描画 ----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        m = self._metrics()
        rect = self._apply_appear(p, QRectF(self.rect()).adjusted(_PAD, _PAD, -_PAD, -_PAD))
        radius = min(rect.height() / 2, self.font_px * 1.45)
        accent = STATE_COLORS.get(self.state, STATE_COLORS["idle"])
        aurora = self._phase * 360 * 1.5 if self.state in _AURORA_STATES else None
        _glass(p, rect, radius, accent, self._glow if self.animate else self._glow_target(), aurora)

        pad = m["pad"]
        if self._compact():   # 待機中のピル：オーブと見出しだけ
            orb_r = rect.height() * 0.2
            oc = QPointF(rect.left() + pad * 0.9 + orb_r, rect.center().y())
            _orb(p, oc, orb_r, self.state, self._phase * 3 % 1.0, 0.0)
            p.setFont(_font(self.font_px * 0.9, 600))
            p.setPen(_TEXT_DIM)
            tx = oc.x() + orb_r + pad * 0.6
            p.drawText(QRectF(tx, rect.top(), rect.right() - tx - pad * 0.6, rect.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, self.title)
            return
        label_h = self._label_h()
        orb_r = m["orb"] * 0.78
        block = label_h + m["title_h"]
        oc = QPointF(rect.left() + pad + orb_r, rect.top() + pad + block / 2)
        _orb(p, oc, orb_r, self.state, self._phase * 3 % 1.0, self._level_smooth)

        tx = oc.x() + orb_r + pad * 0.9
        right = rect.right() - pad
        ty = rect.top() + pad
        # 聞き取り中は右端に波形
        if self.state == "listening":
            bw = self.font_px * 2.0
            _bars(p, QRectF(right - bw, ty + block / 2 - self.font_px * 0.45, bw, self.font_px * 0.9),
                  list(self._levels), self._phase)
            right -= bw + pad * 0.5
        if label_h:   # 状態の小さなラベル（見出しと言葉が違うときだけ）
            p.setFont(_font(self.font_px * 0.66, 600))
            p.setPen(_alpha(accent.lighter(125), 235))
            p.drawText(QRectF(tx, ty, right - tx, label_h), Qt.AlignLeft | Qt.AlignVCenter,
                       STATE_LABELS.get(self.state, ""))
        # 見出し
        tw = right - tx
        p.setFont(_font(self.font_px * 1.02, 600))
        p.setPen(_TEXT)
        p.drawText(QRectF(tx, ty + label_h, tw, m["title_h"]), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(self.title, Qt.ElideRight, int(tw)))
        # 補足行（見出しの下。波形の分は使わず全幅）
        tw = rect.right() - pad - tx
        p.setFont(_font(self.font_px * 0.8, 400))
        for i, line in enumerate(self.lines):
            p.setPen(_TEXT_DIM if i == 0 else _TEXT_FAINT)
            y = ty + label_h + m["title_h"] + m["lh"] * i
            p.drawText(QRectF(tx, y, tw, m["lh"]), Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(line, Qt.ElideRight, int(tw)))


class TranscriptBar(_Animated):
    """文字起こしバー。話している最中は途中経過、話し終えたら確定した文と実行結果を表示する。"""

    def __init__(self, font_px: int = 22, width_ratio: float = 0.6, history: int = 2, key_label: str = "F5",
                 anchor: QWidget | None = None, margin: int = 18, avoid_taskbar: bool = True,
                 animate: bool = True):
        super().__init__(animate)
        self.font_px = font_px
        self.width_ratio = width_ratio
        self.history_len = history
        self.key_label = key_label
        self.anchor = anchor  # この上に置く（状態表示）
        self.margin = int(margin)
        self.avoid_taskbar = bool(avoid_taskbar)
        self.current = ""        # 今の発話（途中経過または確定）
        self.partial = False     # 途中経過なら True（光が流れる）
        self.result = ""         # 実行した操作
        self.history: list[tuple[str, str]] = []  # 過去の (発話, 結果)
        self._levels: collections.deque = collections.deque(maxlen=30)
        # しばらく何も話さなければ隠す（待機中は右下の小さなピルだけにする）
        self.hide_after = 12.0
        self._hide = QTimer(self, singleShot=True, timeout=self.hide)
        self._layout()
        self.show()
        _no_activate(self)
        self._hide.start(int(self.hide_after * 1000))

    def _work(self) -> tuple[int, int, int, int]:
        rect = winutil.work_area_at(0, 0) if self.avoid_taskbar else winutil.monitor_rect_at(0, 0)
        l, t, r, b = rect
        if self.avoid_taskbar and winutil.taskbar_auto_hide():
            b -= 8
        return l, t, r, b

    def _wake(self) -> None:
        """発話や結果が来たら表示して、隠すまでの時間を延ばす。"""
        if not self.isVisible():
            self.show()
            _no_activate(self)
            self._begin_appear()
        if self.partial:
            self._hide.stop()
        else:
            self._hide.start(int(self.hide_after * 1000))

    def _layout(self) -> None:
        l, t, r, b = self._work()
        w = int((r - l) * self.width_ratio)
        rows = 1 + min(len(self.history), self.history_len)
        h = int(self.font_px * 1.7 + self.font_px * 1.45 * (rows - 1) + self.font_px * 1.1) + _PAD * 2
        if self.anchor is not None:
            g = self.anchor.geometry()
            w = max(w, getattr(self.anchor, "full_w", g.width()))
            x, y = g.right() + 1 - w, g.top() + _PAD - 10 - h + _PAD   # 影の余白どうしは重ね、パネルの間を 10px 空ける
        else:
            x, y = winutil.corner_position(self._work(), w, h, "top_right", self.margin, keep_out=0)
        x = max(l + self.margin, min(x, r - w - self.margin))
        y = max(t + self.margin, min(y, b - h - self.margin))
        self.setGeometry(int(x), int(y), w, h)

    def _push_previous(self) -> None:
        """新しい発話が始まったら、確定済みの前の発話を履歴に送る。"""
        if self.current and not self.partial:
            self.history.insert(0, (self.current, self.result))
            del self.history[self.history_len:]
            self._layout()

    def set_partial(self, text: str) -> None:
        if not self.partial:
            self._begin_appear()
        self._push_previous()
        self.current, self.partial, self.result = text, True, ""
        self._wake()
        self._sync_timer()
        self.update()

    def set_final(self, text: str) -> None:
        if self.partial is False:
            self._begin_appear()
        self._push_previous()
        self.current, self.partial, self.result = text, False, ""
        self._wake()
        self._sync_timer()
        self.update()

    def set_result(self, result: str) -> None:
        self.result = result
        self._wake()
        self._sync_timer()
        self.update()

    def set_level(self, level: float) -> None:
        self._levels.append(min(1.0, max(0.0, float(level) * 8)))

    def _wants_motion(self) -> bool:
        return self.partial

    def _glow_target(self) -> float:
        if self.partial:
            return 0.35   # 状態カプセルのほうを主役にして、こちらは控えめに光らせる
        return 0.25 if self.result else 0.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        rect = self._apply_appear(p, QRectF(self.rect()).adjusted(_PAD, _PAD, -_PAD, -_PAD))
        radius = min(self.font_px * 1.2, rect.height() / 2)
        aurora = self._phase * 360 * 1.5 if self.partial else None
        _glass(p, rect, radius, STATE_COLORS["done"], self._glow if self.animate else self._glow_target(), aurora)
        pad = self.font_px * 0.85
        lh = self.font_px * 1.7
        top = rect.top() + self.font_px * 0.55
        left, right = rect.left() + pad, rect.right() - pad
        # 1 行目：今の発話
        x = left   # 聞き取り中の波形は状態カプセルに出すので、こちらは文字のきらめきだけにする
        res_w = 0.0
        if self.result:   # 右側に結果のタグ（チェックつき）
            p.setFont(_font(self.font_px * 0.74, 600))
            fm = p.fontMetrics()
            avail = (right - x) * 0.55
            txt = fm.elidedText(self.result, Qt.ElideRight, int(avail - self.font_px * 1.8))
            res_w = fm.horizontalAdvance(txt) + self.font_px * 1.9
            tag = QRectF(right - res_w, top + lh * 0.2, res_w, lh * 0.6)
            col = STATE_COLORS["done"]
            p.setPen(Qt.NoPen)
            p.setBrush(_alpha(col, 40))
            p.drawRoundedRect(tag, tag.height() / 2, tag.height() / 2)
            p.setPen(QPen(_alpha(col, 110), 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(tag.adjusted(0.5, 0.5, -0.5, -0.5), tag.height() / 2, tag.height() / 2)
            cx, cy, s = tag.left() + tag.height() * 0.55, tag.center().y(), tag.height() * 0.18
            pen = QPen(col.lighter(120), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            path = QPainterPath(QPointF(cx - s, cy))
            path.lineTo(cx - s * 0.2, cy + s * 0.8)
            path.lineTo(cx + s * 1.2, cy - s * 0.8)
            p.drawPath(path)
            p.setPen(QColor(208, 255, 234))
            p.drawText(tag.adjusted(tag.height() * 0.95, 0, -self.font_px * 0.5, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)
            res_w += self.font_px * 0.5
        text = self.current or f"{self.key_label} を押して話しかけてください"
        tw = max(20.0, right - x - res_w)
        f = _font(self.font_px * 1.0, 600 if (self.current and not self.partial) else 400)
        p.setFont(f)
        shown = p.fontMetrics().elidedText(text, Qt.ElideLeft, int(tw))
        trect = QRectF(x, top, tw, lh)
        if self.partial and self.animate:   # 途中経過は文字の上を光が流れる（考え中のきらめき）
            adv = p.fontMetrics().horizontalAdvance(shown)
            band = max(60.0, adv * 0.35)
            sx = x - band + (adv + band * 2) * (self._phase * 4 % 1.0)
            g = QLinearGradient(sx, 0, sx + band, 0)
            g.setColorAt(0.0, QColor(200, 204, 220))
            g.setColorAt(0.5, QColor(255, 255, 255))
            g.setColorAt(1.0, QColor(200, 204, 220))
            p.setPen(QPen(g, 1))
        else:
            p.setPen(_TEXT if self.current else _TEXT_FAINT)
        p.drawText(trect, Qt.AlignVCenter | Qt.AlignLeft, shown)
        # 2 行目以降：履歴（薄く）
        hl = self.font_px * 1.45
        p.setFont(_font(self.font_px * 0.76, 400))
        fm = p.fontMetrics()
        for i, (said, res) in enumerate(self.history[: self.history_len]):
            y = top + lh + hl * i
            p.setPen(Qt.NoPen)
            p.setBrush(_alpha(STATE_COLORS["done"] if res else STATE_COLORS["idle"], 170 - i * 50))
            p.drawEllipse(QPointF(left + 3, y + hl / 2), 2.6, 2.6)
            p.setPen(_alpha(_TEXT_DIM, 220 - i * 70))
            s = f"{said}   {res}" if res else said
            p.drawText(QRectF(left + 12, y, right - left - 12, hl), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(s, Qt.ElideRight, int(right - left - 12)))


class HintOverlay(QWidget):
    """番号ラベル（要素モード）または番号グリッドを全画面に描く。"""

    def __init__(self, font_px: int = 18):
        super().__init__(None, _FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.font_px = font_px
        self.labels: list[tuple[int, tuple[int, int]]] = []   # (番号, 中心座標)
        self.grid: tuple[tuple[int, int, int, int], int] | None = None  # (領域, 分割数)
        l, t, w, h = winutil.virtual_screen()
        self.origin = (l, t)
        self.setGeometry(l, t, w, h)

    def show_labels(self, labels: list[tuple[int, tuple[int, int]]]) -> None:
        self.labels, self.grid = labels, None
        self._show()

    def show_grid(self, region: tuple[int, int, int, int], n: int) -> None:
        self.labels, self.grid = [], (region, n)
        self._show()

    def _show(self) -> None:
        self.show()
        _no_activate(self)
        self.raise_()
        self.update()

    def clear(self) -> None:
        self.labels, self.grid = [], None
        self.hide()

    def _pill(self, p: QPainter, box: QRectF, text: str, font: QFont) -> None:
        """番号のピル：影 → オーロラの縁 → 暗いガラス → 白い数字。"""
        r = box.height() / 2
        _shadow(p, box, r, 0.6)
        p.setPen(Qt.NoPen)
        p.setBrush(_aurora_gradient(box.center(), 30))
        p.drawRoundedRect(box.adjusted(-1.6, -1.6, 1.6, 1.6), r + 1.6, r + 1.6)
        body = QLinearGradient(box.topLeft(), box.bottomLeft())
        body.setColorAt(0.0, QColor(36, 36, 46, 250))
        body.setColorAt(1.0, QColor(14, 14, 20, 250))
        p.setBrush(body)
        p.drawRoundedRect(box, r, r)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(box, Qt.AlignCenter, text)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        ox, oy = self.origin
        if self.grid:
            (l, t, r, b), n = self.grid
            area = QRectF(l - ox, t - oy, r - l, b - t)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 10, 16, 60))   # 領域を少しだけ暗くして、線と番号を見やすくする
            p.drawRect(area)
            cw, ch = (r - l) / n, (b - t) / n
            for i in range(n + 1):
                x, y = l - ox + cw * i, t - oy + ch * i
                for pen in (QPen(QColor(0, 0, 0, 90), 3.0), QPen(QColor(255, 255, 255, 170), 1.0)):
                    p.setPen(pen)
                    p.drawLine(QPointF(x, t - oy), QPointF(x, b - oy))
                    p.drawLine(QPointF(l - ox, y), QPointF(r - ox, y))
            size = max(12, min(int(min(cw, ch) * 0.22), self.font_px * 2))
            font = _font(size, 700)
            p.setFont(font)
            fm = p.fontMetrics()
            for idx in range(n * n):
                row, col = divmod(idx, n)
                cell = QRectF(l - ox + cw * col, t - oy + ch * row, cw, ch)
                s = str(idx + 1)
                w, h = fm.horizontalAdvance(s) + size * 1.1, size * 1.7
                self._pill(p, QRectF(cell.center().x() - w / 2, cell.center().y() - h / 2, w, h), s, font)
            return
        font = _font(self.font_px * 0.95, 700)
        p.setFont(font)
        fm = p.fontMetrics()
        for num, (cx, cy) in self.labels:
            s = str(num)
            w, h = fm.horizontalAdvance(s) + self.font_px * 1.05, self.font_px * 1.6
            self._pill(p, QRectF(cx - ox - w / 2, cy - oy - h / 2, w, h), s, font)


class AnswerCard(_Animated):
    """AI の答え・下書き・要約を出すカード。考えている間は光る仮の行を流し、答えはタイプするように現れる。

    show_answer(title, body, hint, secs)：body が空なら「考え中」の表示にする。secs 秒で自動的に隠す（0 なら隠さない）。
    状態カプセル（と文字起こしバー）の上に置く。
    """

    def __init__(self, font_px: int = 18, anchors: list | None = None, margin: int = 16,
                 avoid_taskbar: bool = True, animate: bool = True, max_lines: int = 14):
        super().__init__(animate)
        self.font_px = font_px
        self.anchors = anchors or []   # この上に置く（見えているものの一番上）
        self.margin = int(margin)
        self.avoid_taskbar = bool(avoid_taskbar)
        self.max_lines = max_lines
        self.title = ""
        self.body = ""
        self.hint = ""
        self._lines: list[str] = []
        self._reveal = 1.0        # 本文を何割まで見せたか（タイプするように出す）
        self._hide = QTimer(self, singleShot=True, timeout=self.hide)
        self.setGeometry(0, 0, 10, 10)

    def _work(self) -> tuple[int, int, int, int]:
        rect = winutil.work_area_at(0, 0) if self.avoid_taskbar else winutil.monitor_rect_at(0, 0)
        return rect

    def _width(self) -> int:
        for a in self.anchors:
            w = getattr(a, "full_w", 0)
            if w:
                return int(w * 1.12)
        return int(self.font_px * 30) + _PAD * 2

    def _wrap(self, text: str, width: float) -> list[str]:
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(_font(self.font_px * 0.92, 400))
        out: list[str] = []
        for para in text.splitlines() or [""]:
            if not para.strip():
                out.append("")
                continue
            line = ""
            for ch in para:
                # 句読点・閉じかっこは行の頭に来ないよう、前の行に入れる（禁則処理）
                if fm.horizontalAdvance(line + ch) > width and line and ch not in "、。，．,.）)」』】！？!?ーぁぃぅぇぉっゃゅょ":
                    out.append(line)
                    line = ch
                else:
                    line += ch
            out.append(line)
        while out and not out[-1]:
            out.pop()
        if len(out) > self.max_lines:
            out = out[: self.max_lines - 1] + [out[self.max_lines - 1][:-1] + "…"]
        return out

    def _layout(self) -> None:
        w = self._width()
        inner = w - _PAD * 2 - self.font_px * 1.8
        self._lines = self._wrap(self.body, inner) if self.body else ["", "", ""]
        lh = self.font_px * 1.5
        h = int(self.font_px * 2.9 + lh * len(self._lines) + (self.font_px * 1.6 if self.hint else 0)
                + self.font_px * 0.6) + _PAD * 2
        l, t, r, b = self._work()
        top = b
        right = r - self.margin
        for a in self.anchors:
            if a.isVisible():
                g = a.geometry()
                top = min(top, g.top() + _PAD)
                right = g.right() + 1 - _PAD + _PAD
        x = right - w
        y = top - 10 - h + _PAD
        x = max(l + self.margin, min(x, r - w - self.margin))
        y = max(t + self.margin, y)
        self.setGeometry(int(x), int(y), w, h)

    def show_answer(self, title: str, body: str, hint: str = "", secs: float = 0.0) -> None:
        self.title, self.body, self.hint = title, body, hint
        self._reveal = 0.0 if (body and self.animate) else 1.0
        self._layout()
        if not self.isVisible():
            self.show()
            _no_activate(self)
            self._begin_appear()
        self.raise_()
        self._sync_timer()
        self.update()
        if secs > 0:
            self._hide.start(int(secs * 1000))
        else:
            self._hide.stop()

    def clear(self) -> None:
        self._hide.stop()
        self.hide()

    def _wants_motion(self) -> bool:
        return self.isVisible() and (not self.body or self._reveal < 1.0)

    def _glow_target(self) -> float:
        return 0.75 if not self.body else (0.45 if self._reveal < 1.0 else 0.25)

    def _tick(self) -> None:
        if self.body and self._reveal < 1.0:
            total = max(1, sum(len(x) for x in self._lines))
            self._reveal = min(1.0, self._reveal + max(0.012, 6.0 / total))   # 長い文ほど 1 文字あたりを速く
        super()._tick()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        rect = self._apply_appear(p, QRectF(self.rect()).adjusted(_PAD, _PAD, -_PAD, -_PAD))
        radius = self.font_px * 1.1
        thinking = not self.body
        _glass(p, rect, radius, STATE_COLORS["processing"], self._glow if self.animate else self._glow_target(),
               self._phase * 360 * 1.5 if thinking or self._reveal < 1.0 else None)
        pad = self.font_px * 0.9
        left, right = rect.left() + pad, rect.right() - pad
        # 見出し：小さなオーロラの球＋題名
        oc = QPointF(left + self.font_px * 0.45, rect.top() + pad + self.font_px * 0.55)
        _orb(p, oc, self.font_px * 0.42, "processing" if thinking else "listening", self._phase * 3 % 1.0,
             0.3 if thinking else 0.0)
        p.setFont(_font(self.font_px * 0.95, 600))
        p.setPen(_TEXT)
        tx = left + self.font_px * 1.3
        p.drawText(QRectF(tx, rect.top() + pad - self.font_px * 0.2, right - tx, self.font_px * 1.5),
                   Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(self.title, Qt.ElideRight, int(right - tx)))
        y = rect.top() + pad + self.font_px * 1.7
        lh = self.font_px * 1.5
        if thinking:   # 考え中：光が流れる仮の行
            for i, frac in enumerate((0.92, 0.78, 0.55)):
                bar = QRectF(left, y + lh * i + lh * 0.3, (right - left) * frac, lh * 0.38)
                g = QLinearGradient(bar.topLeft(), bar.topRight())
                s = (self._phase * 5 + i * 0.15) % 1.0
                g.setColorAt(0.0, QColor(255, 255, 255, 18))
                g.setColorAt(max(0.0, s - 0.15), QColor(255, 255, 255, 18))
                g.setColorAt(s, QColor(255, 255, 255, 70))
                g.setColorAt(min(1.0, s + 0.15), QColor(255, 255, 255, 18))
                g.setColorAt(1.0, QColor(255, 255, 255, 18))
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
            return
        # 本文（タイプするように出す）
        p.setFont(_font(self.font_px * 0.92, 400))
        p.setPen(QColor(236, 238, 246))
        total = sum(len(x) for x in self._lines)
        shown = int(total * self._reveal)
        for line in self._lines:
            if shown <= 0:
                break
            part = line[:shown]
            shown -= len(line)
            p.drawText(QRectF(left, y, right - left, lh), Qt.AlignLeft | Qt.AlignVCenter, part)
            y += lh
        if self.hint and self._reveal >= 1.0:
            y = rect.bottom() - pad - self.font_px * 1.2
            p.setFont(_font(self.font_px * 0.72, 500))
            p.setPen(_TEXT_FAINT)
            p.drawText(QRectF(left, y, right - left, self.font_px * 1.2), Qt.AlignLeft | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(self.hint, Qt.ElideRight, int(right - left)))
