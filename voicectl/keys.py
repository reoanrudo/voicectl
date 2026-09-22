"""グローバルなキーフック：押している間だけ聞き取るホットキー、緊急停止キー、確認中の Enter/Esc。

自分が SendInput で送ったキー（LLKHF_INJECTED）は無視する。
"""
from __future__ import annotations

import logging
from typing import Callable

from pynput import keyboard

log = logging.getLogger(__name__)

KEY_VK = {
    "right_ctrl": 0xA3, "left_ctrl": 0xA2, "right_alt": 0xA5, "right_shift": 0xA1, "pause": 0x13,
    "scroll_lock": 0x91, "insert": 0x2D, "capslock": 0x14, "apps": 0x5D, "henkan": 0x1C, "muhenkan": 0x1D,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LLKHF_INJECTED = 0x10
VK_RETURN, VK_ESCAPE = 0x0D, 0x1B


class KeyHook:
    def __init__(self, ptt: str, stop: str, suppress_ptt: bool,
                 on_ptt_down: Callable[[], None], on_ptt_up: Callable[[], None], on_stop: Callable[[], None],
                 on_confirm: Callable[[bool], None], is_confirming: Callable[[], bool]):
        self.ptt_vk = KEY_VK[ptt]
        self.stop_vk = KEY_VK[stop]
        self.suppress_ptt = suppress_ptt
        self.on_ptt_down, self.on_ptt_up, self.on_stop = on_ptt_down, on_ptt_up, on_stop
        self.on_confirm, self.is_confirming = on_confirm, is_confirming
        self._ptt_held = False
        self._listener = keyboard.Listener(win32_event_filter=self._filter)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()

    def _filter(self, msg, data):
        if data.flags & LLKHF_INJECTED:
            return True
        vk = data.vkCode
        down = msg in (WM_KEYDOWN, WM_SYSKEYDOWN)
        up = msg in (WM_KEYUP, WM_SYSKEYUP)
        suppress = False
        try:
            if vk == self.ptt_vk:
                if down and not self._ptt_held:  # キーリピートでは再発火しない
                    self._ptt_held = True
                    self.on_ptt_down()
                elif up and self._ptt_held:
                    self._ptt_held = False
                    self.on_ptt_up()
                suppress = self.suppress_ptt
            elif vk == self.stop_vk:
                if down:
                    self.on_stop()
                suppress = True
            elif vk in (VK_RETURN, VK_ESCAPE) and self.is_confirming():
                if down:
                    self.on_confirm(vk == VK_RETURN)
                suppress = True
        except Exception:
            log.exception("キーフックの処理でエラー")
        if suppress:
            self._listener.suppress_event()  # 例外で抜けて、このキーを他のアプリに渡さない
        return True
