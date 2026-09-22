"""判定結果を、オーバーレイや確認表示用の日本語にする。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schema import Candidate, Decision

_DIR = {"up": "上", "down": "下", "left": "左", "right": "右", "up_left": "左上", "up_right": "右上",
        "down_left": "左下", "down_right": "右下"}
_AMT = {"small": "少し", "medium": "", "large": "大きく"}
_BTN = {"left": "クリック", "right": "右クリック", "double": "ダブルクリック", "middle": "中クリック"}
_WIN = {"minimize": "最小化", "maximize": "最大化", "restore": "元のサイズに戻す", "close": "ウィンドウを閉じる",
        "show_desktop": "デスクトップを表示", "always_on_top": "常に手前に表示", "topmost_off": "常に手前を解除"}
_SNAP = {"left_half": "画面の左半分に寄せる", "right_half": "画面の右半分に寄せる", "screen_wide": "画面いっぱいにする",
         "prev_monitor": "左のモニターへ移す", "next_monitor": "右のモニターへ移す", "full": "作業領域いっぱいにする",
         "upper_half": "画面の上半分に置く", "lower_half": "画面の下半分に置く",
         "top_left": "画面の左上の四分の一に置く", "top_right": "画面の右上の四分の一に置く",
         "bottom_left": "画面の左下の四分の一に置く", "bottom_right": "画面の右下の四分の一に置く",
         "center": "画面の中央に置く"}


def _label(lookup: dict[str, dict[str, "Candidate"]], kind: str, cid: str | None) -> str:
    if cid is None:
        return "?"
    c = lookup.get(kind, {}).get(cid)
    if c is None:
        return cid
    if kind == "app" and isinstance(c.data, dict):
        return c.data["name"]  # 別名は表示しない
    if kind == "element":
        return c.data.name
    if kind == "menu":
        return " > ".join(c.data.path)
    if kind == "command":
        return c.data.name
    return c.label


def describe(d: "Decision", lookup: dict[str, dict[str, "Candidate"]]) -> str:
    from .schema import KEYS
    p = d.params
    a = d.action
    if a == "launch_app":
        return f"「{_label(lookup, 'app', p.get('app'))}」を起動"
    if a == "switch_window":
        return f"「{_label(lookup, 'window', p.get('window'))}」に切り替え"
    if a == "mouse_move":
        return f"マウスを{_DIR.get(p.get('direction', ''), '?')}へ{_AMT.get(p.get('amount', 'medium'), '')}移動"
    if a == "mouse_to_element":
        return f"マウスを「{_label(lookup, 'element', p.get('element'))}」へ移動"
    if a == "click":
        return _BTN.get(p.get("button", "left"), "クリック")
    if a == "click_element":
        return f"「{_label(lookup, 'element', p.get('element'))}」を{_BTN.get(p.get('button', 'left'), 'クリック')}"
    if a == "drag_start":
        return "ドラッグ開始（ボタンを押したまま）"
    if a == "drag_end":
        return "ドロップ（ボタンを離す）"
    if a == "scroll":
        return f"{_DIR.get(p.get('direction', ''), '?')}へ{_AMT.get(p.get('amount', 'medium'), '')}スクロール"
    if a == "type_text":
        return f"「{d.text or ''}」と入力"
    if a == "key_combo":
        k = p.get("key")
        return f"キー操作：{KEYS[k][0].split(' / ')[-1] if k in KEYS else '?'}"
    if a == "window_control":
        return _WIN.get(p.get("window_action", ""), "ウィンドウ操作")
    if a == "snap_window":
        return _SNAP.get(p.get("snap", ""), "ウィンドウを移動")
    if a == "show_hints":
        return "グリッドを表示" if p.get("hint_mode") == "grid" else "番号を表示"
    if a == "menu_command":
        return f"メニュー「{_label(lookup, 'menu', p.get('menu'))}」を実行"
    if a == "app_command":
        return f"「{_label(lookup, 'command', p.get('command'))}」を実行"
    if a == "learn_app":
        return "このアプリのメニューを覚える"
    if a == "repeat":
        return "直前の操作をもう一度"
    if a == "stop":
        return "停止"
    return "（不明な命令）"
