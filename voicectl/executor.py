"""判定結果を実際の PC 操作に変換して実行する。"""
from __future__ import annotations

import logging
import time

from . import apps, winutil
from .candidates import Lookup
from .schema import KEYS, SNAP_RECTS, SNAPS, Decision

log = logging.getLogger(__name__)

_VEC = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0),
        "up_left": (-1, -1), "up_right": (1, -1), "down_left": (-1, 1), "down_right": (1, 1)}


class ExecutionError(RuntimeError):
    pass


class Executor:
    def __init__(self, step_px: dict[str, int], scroll_notches: dict[str, int]):
        self.step_px = step_px
        self.scroll_notches = scroll_notches
        self.dragging = False

    def _cand(self, lookup: Lookup, kind: str, d: Decision):
        cid = d.params.get(kind)
        c = lookup.get(kind, {}).get(cid) if cid else None
        if c is None:
            raise ExecutionError(f"対象（{kind}）が特定できません")
        return c.data

    def run(self, d: Decision, lookup: Lookup) -> None:
        a, p = d.action, d.params
        if a == "launch_app":
            apps.launch(self._cand(lookup, "app", d))
        elif a == "switch_window":
            winutil.focus_window(self._cand(lookup, "window", d).hwnd)
        elif a == "mouse_move":
            dx, dy = _VEC[p.get("direction", "right")]
            step = self.step_px.get(p.get("amount", "medium"), 120)
            x, y = winutil.get_cursor()
            winutil.set_cursor(x + dx * step, y + dy * step)
        elif a == "mouse_to_element":
            winutil.set_cursor(*self._cand(lookup, "element", d).center)
        elif a == "click":
            self.click(p.get("button", "left"))
        elif a == "click_element":
            winutil.set_cursor(*self._cand(lookup, "element", d).center)
            time.sleep(0.03)  # ホバー状態を反映させてからクリックする
            self.click(p.get("button", "left"))
        elif a == "drag_start":
            winutil.mouse_down("left")
            self.dragging = True
        elif a == "drag_end":
            winutil.mouse_up("left")
            self.dragging = False
        elif a == "scroll":
            direction = p.get("direction", "down")
            n = self.scroll_notches.get(p.get("amount", "medium"), 5)
            if direction in ("left", "right"):
                winutil.scroll(n if direction == "right" else -n, horizontal=True)
            else:
                winutil.scroll(n if direction.startswith("up") else -n)
        elif a == "type_text":
            if not d.text:
                raise ExecutionError("入力する文字列がありません")
            winutil.type_unicode(d.text)
        elif a == "key_combo":
            key = p.get("key")
            if key not in KEYS:
                raise ExecutionError("キーが特定できません")
            winutil.press_combo(KEYS[key][1])
        elif a == "window_control":
            act = p.get("window_action")
            if act == "show_desktop":
                winutil.press_combo([0x5B, 0x44])  # Win+D
            else:
                fg = winutil.foreground_window()
                if fg is None:
                    raise ExecutionError("操作するウィンドウがありません")
                if act in ("always_on_top", "topmost_off"):
                    winutil.set_topmost(fg.hwnd, act == "always_on_top")
                else:
                    winutil.window_command(fg.hwnd, act)
        elif a == "snap_window":
            snap = p.get("snap")
            if snap in SNAP_RECTS:   # Win+矢印では出せない形（上半分・四分の 1・中央）は直接置く
                fg = winutil.foreground_window()
                if fg is None:
                    raise ExecutionError("操作するウィンドウがありません")
                winutil.place_rect(fg.hwnd, snap)
            elif snap in SNAPS:
                winutil.press_combo(SNAPS[snap][1])
            else:
                raise ExecutionError("ウィンドウの移動先が特定できません")
        else:
            raise ExecutionError(f"実行できない操作です: {a}")

    def click(self, button: str) -> None:
        if button == "double":
            winutil.click("left", 2)
        else:
            winutil.click(button)

    def release(self) -> None:
        winutil.release_all_buttons()
        self.dragging = False
