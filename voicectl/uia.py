"""UI Automation で、前面ウィンドウのクリックできる要素（名前と座標）をテキストとして取得する。

Jev は画像を見られないため、画面の中身はここと OCR（ocr.py）でテキスト化して渡す。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# UIA ControlType ID → 表示名
_TYPES = {
    50000: "ボタン", 50011: "メニュー項目", 50005: "リンク", 50007: "リスト項目", 50019: "タブ",
    50024: "ツリー項目", 50002: "チェックボックス", 50013: "ラジオボタン", 50003: "コンボボックス",
    50004: "入力欄", 50031: "分割ボタン", 50029: "データ項目", 50020: "テキスト", 50006: "画像", 50034: "ヘッダー項目",
}
_TEXT_LIKE = {50020, 50006}  # 押せるとは限らないので優先度を下げる


@dataclass
class ScreenElement:
    name: str
    kind: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom（物理ピクセル）
    source: str                      # "uia" | "ocr"
    priority: int = 0
    auto_id: str = ""                # UI Automation の AutomationId（アプリマップとの照合用）
    group: str = ""                  # 所属グループ名（アプリマップから付ける）
    aliases: list = None             # 日本語の言い方（プロファイルから付ける）

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    @property
    def label(self) -> str:
        extra = f"・グループ: {self.group}" if self.group else ""
        alias = f"・別名: {'・'.join(self.aliases)}" if self.aliases else ""
        return f"{self.name}（{self.kind}{extra}{alias}）"


class UiaScanner:
    """COM 初期化はスレッドごとに必要なので、専用スレッドから呼ぶ前提（context.py の executor）。"""

    def __init__(self, max_elements: int = 400):
        self.max_elements = max_elements
        self._uia = None
        self._cond = None
        self._cache = None

    def _init(self):
        import uiautomation as auto  # noqa: F401  （COM の型ライブラリを読み込む）
        from uiautomation.uiautomation import _AutomationClient
        self._auto = auto
        uia = _AutomationClient.instance().IUIAutomation
        cond = None
        for type_id in _TYPES:
            c = uia.CreatePropertyCondition(30003, type_id)  # UIA_ControlTypePropertyId
            cond = c if cond is None else uia.CreateOrCondition(cond, c)
        onscreen = uia.CreatePropertyCondition(30022, False)  # UIA_IsOffscreenPropertyId
        self._cond = uia.CreateAndCondition(cond, onscreen)
        cache = uia.CreateCacheRequest()
        for pid in (30005, 30001, 30003, 30010, 30011):  # Name, BoundingRectangle, ControlType, IsEnabled, AutomationId
            cache.AddProperty(pid)
        self._cache = cache
        self._uia = uia

    def scan(self, hwnd: int, window_rect: tuple[int, int, int, int]) -> list[ScreenElement]:
        if self._uia is None:
            self._init()
        t0 = time.perf_counter()
        root = self._uia.ElementFromHandle(hwnd)
        found = root.FindAllBuildCache(4, self._cond, self._cache)  # TreeScope_Descendants
        wl, wt, wr, wb = window_rect
        seen: set[tuple] = set()
        out: list[ScreenElement] = []
        for i in range(min(found.Length, 3000)):
            e = found.GetElement(i)
            name = " ".join((e.CachedName or "").split())  # 名前の中の改行を空白に（Strategy +EV など）
            if not name or len(name) > 80 or not e.CachedIsEnabled:
                continue
            r = e.CachedBoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
            if r.right - r.left < 3 or r.bottom - r.top < 3:
                continue
            if r.right < wl or r.left > wr or r.bottom < wt or r.top > wb:
                continue
            key = (name, rect)
            if key in seen:
                continue
            seen.add(key)
            ct = e.CachedControlType
            out.append(ScreenElement(name, _TYPES.get(ct, "要素"), rect, "uia", 0 if ct in _TEXT_LIKE else 1,
                                     auto_id=e.CachedAutomationId or ""))
        out.sort(key=lambda el: -el.priority)
        log.debug("UIA: %d 要素 (%.0f ms)", len(out), (time.perf_counter() - t0) * 1000)
        return out[: self.max_elements]
