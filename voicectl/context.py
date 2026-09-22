"""PC の現在状態（Jev の state に渡す文脈）を集める。

ホットキーを押した瞬間に begin() で収集を開始し、話している間に UIA・OCR・Windows の状況を済ませておく。
発話が終わった時点で collect() が結果を待つ（遅延を隠すための先読み）。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field

from . import textparse, winutil
from .uia import ScreenElement, UiaScanner

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    window: winutil.WindowInfo | None
    cursor: tuple[int, int]
    windows: list[winutil.WindowInfo] = field(default_factory=list)
    elements: list[ScreenElement] = field(default_factory=list)


def _clip(rect, bounds):
    l, t, r, b = rect
    bl, bt, bw, bh = bounds
    return max(l, bl), max(t, bt), min(r, bl + bw), min(b, bt + bh)


def merge_elements(uia: list[ScreenElement], ocr: list[ScreenElement]) -> list[ScreenElement]:
    """OCR の結果のうち、UIA ですでに取れている要素と重なるものは捨てる。"""
    out = list(uia)
    for o in ocr:
        cx, cy = o.center
        oname = textparse.compact(o.name)
        dup = False
        for u in uia:
            l, t, r, b = u.rect
            if l <= cx <= r and t <= cy <= b and (oname in textparse.compact(u.name) or len(oname) <= 1):
                dup = True
                break
        if not dup and len(oname) >= 1:
            out.append(o)
    return out


def drop_own(elements: list[ScreenElement], own_rects) -> list[ScreenElement]:
    """voicectl 自身の表示の上にある要素（OCR で読んだ文字起こしバー等）を捨てる。"""
    if not own_rects:
        return elements

    def inside(el):
        cx, cy = el.center
        return any(l <= cx <= r and t <= cy <= b for l, t, r, b in own_rects)

    return [e for e in elements if not inside(e)]


class ContextCollector:
    def __init__(self, use_uia: bool = True, use_ocr: bool = True, ocr_language: str = "ja",
                 win_context_scan=None):
        self.use_uia = use_uia
        self.use_ocr = use_ocr
        self.ocr_language = ocr_language
        # UIA と OCR はそれぞれ COM/WinRT の状態をスレッドに持つので専用スレッドを 1 本ずつ使う
        self._uia_pool = ThreadPoolExecutor(1, thread_name_prefix="uia")
        self._ocr_pool = ThreadPoolExecutor(1, thread_name_prefix="ocr")
        self._winctx_pool = ThreadPoolExecutor(1, thread_name_prefix="winctx")
        self._uia = UiaScanner()
        self._ocr = None
        self._pending: tuple | None = None
        # Windows の状況（winctx.collect）の先読み。テストから差し替えられるように関数は外から渡す
        self._winctx_scan_fn = win_context_scan or self._default_winctx_scan
        self._winctx_future: Future | None = None
        self._winctx_hwnd: int | None = None
        self._winctx_at: float = 0.0

    def _ocr_scan(self, rect):
        if self._ocr is None:
            from .ocr import OcrScanner
            self._ocr = OcrScanner(self.ocr_language)
        return self._ocr.scan(rect)

    @staticmethod
    def _default_winctx_scan(win):
        """COM を初期化した専用スレッドで Windows の状況を集める（UIA と Shell COM はスレッドごとの初期化が必要）。"""
        import uiautomation as auto
        from . import winctx
        with auto.UIAutomationInitializerInThread():
            return winctx.collect(win)

    def begin(self) -> None:
        win = winutil.foreground_window()
        cursor = winutil.get_cursor()
        uia_f: Future | None = None
        ocr_f: Future | None = None
        if win and win.rect[0] > -30000:
            rect = _clip(win.rect, winutil.virtual_screen())
            if self.use_uia:
                uia_f = self._uia_pool.submit(self._uia.scan, win.hwnd, rect)
            if self.use_ocr:
                ocr_f = self._ocr_pool.submit(self._ocr_scan, rect)
        self._pending = (win, cursor, uia_f, ocr_f)
        # Windows の状況（窓の種類・ダイアログ・フォーカス・エクスプローラーの選択）も話している間に取る
        if win is not None:
            self._winctx_future = self._winctx_pool.submit(self._winctx_scan_fn, win)
            self._winctx_hwnd = win.hwnd
            self._winctx_at = time.monotonic()

    def win_context(self, expect_window=None, timeout: float = 0.6, max_age_sec: float = 10.0):
        """begin() で先読みした Windows の状況（winctx.collect の結果）を返す。

        先読みがない・前面の窓が変わっている・古すぎる・時間内に終わらないときは None を返すので、
        呼び出し側はその場で取り直せる。
        """
        fut = self._winctx_future
        if fut is None:
            return None
        if expect_window is not None and expect_window.hwnd != self._winctx_hwnd:
            return None
        if time.monotonic() - self._winctx_at > max_age_sec:
            return None
        try:
            return fut.result(timeout)
        except FutTimeout:
            log.debug("Windows の状況の先読みが終わらなかった（その場で取り直す）")
        except Exception:
            log.debug("Windows の状況の先読みに失敗", exc_info=True)
        return None

    def collect(self, budget_sec: float = 0.8) -> Snapshot:
        if self._pending is None:
            self.begin()
        win, cursor, uia_f, ocr_f = self._pending
        self._pending = None
        uia = self._result(uia_f, budget_sec, "UIA")
        ocr = self._result(ocr_f, budget_sec, "OCR")
        try:
            own = winutil.own_window_rects()
        except Exception:
            log.exception("自分の表示範囲の取得に失敗")
            own = []
        return Snapshot(window=win, cursor=cursor, windows=winutil.list_windows(),
                        elements=drop_own(merge_elements(uia, ocr), own))

    @staticmethod
    def _result(f: Future | None, budget: float, name: str) -> list:
        if f is None:
            return []
        try:
            return f.result(timeout=budget)
        except FutTimeout:
            if budget > 0:  # 先読みを捨てるだけのとき（budget 0）は警告しない
                log.warning("%s が時間内に終わらなかったため省略", name)
        except Exception:
            log.exception("%s の取得に失敗", name)
        return []
