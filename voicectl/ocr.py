"""Windows 標準の OCR（Windows.Media.Ocr）で画面上の文字と座標を取得する。UIA で取れない画面の補完用。"""
from __future__ import annotations

import asyncio
import logging
import time

import mss

from .uia import ScreenElement

log = logging.getLogger(__name__)


class OcrScanner:
    def __init__(self, language: str = "ja"):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        self._engine = OcrEngine.try_create_from_language(Language(language))
        if self._engine is None:
            self._engine = OcrEngine.try_create_from_user_profile_languages()
        self._max_dim = OcrEngine.max_image_dimension
        self._sct = None

    def scan(self, rect: tuple[int, int, int, int]) -> list[ScreenElement]:
        t0 = time.perf_counter()
        l, t, r, b = rect
        w, h = min(r - l, self._max_dim), min(b - t, self._max_dim)
        if w < 8 or h < 8:
            return []
        if self._sct is None:  # mss はスレッドごとにインスタンスが必要
            self._sct = mss.mss()
        shot = self._sct.grab({"left": l, "top": t, "width": w, "height": h})
        result = asyncio.run(self._recognize(shot.bgra, w, h))
        out = []
        for line in result.lines:
            for seg_text, (x0, y0, x1, y1) in _split_line(line):
                out.append(ScreenElement(seg_text, "画面上の文字", (l + x0, t + y0, l + x1, t + y1), "ocr", 0))
        log.debug("OCR: %d 件 (%.0f ms)", len(out), (time.perf_counter() - t0) * 1000)
        return out

    async def _recognize(self, bgra: bytes, w: int, h: int):
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        writer = DataWriter()
        writer.write_bytes(bgra)
        bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)
        return await self._engine.recognize_async(bmp)


def _split_line(line):
    """1 行の中で単語の間隔が大きく空いている所で区切る（ツールバーのボタン列などを分けるため）。"""
    words = list(line.words)
    if not words:
        return []
    segs, cur = [], [words[0]]
    for prev, w in zip(words, words[1:]):
        pr, wr = prev.bounding_rect, w.bounding_rect
        gap = wr.x - (pr.x + pr.width)
        if gap > max(pr.height, wr.height) * 1.5:
            segs.append(cur)
            cur = []
        cur.append(w)
    segs.append(cur)
    out = []
    for seg in segs:
        text = _join(w.text for w in seg)
        rects = [w.bounding_rect for w in seg]
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        if text.strip():
            out.append((text, (int(x0), int(y0), int(x1), int(y1))))
    return out


def _join(parts) -> str:
    """日本語は単語間の空白を詰め、英数字同士の間だけ空白を残す。"""
    out = ""
    for p in parts:
        if out and out[-1].isascii() and out[-1].isalnum() and p[:1].isascii() and p[:1].isalnum():
            out += " "
        out += p
    return out
