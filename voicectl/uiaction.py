"""画面要素の操作を UI Automation で確実に行う（agent-desktop の方式）。

- 実行の直前に対象を探し直す（判定時の座標を使い回さない）。AutomationId → 名前と種類 の順で探す
- 事前点検：見つからない・無効・複数あって決められない は、黙って失敗させずにエラーにする
- マウスを動かさずにパターンで操作する：ボタンは Invoke、チェックボックスは Toggle、タブ・ラジオは Select
  使えないときだけ、探し直した最新の位置をクリックする
- Invoke はダイアログが開くと閉じるまで戻らないことがあるので、毎回別スレッドで実行して一定時間で打ち切る
"""
from __future__ import annotations

import logging
import threading

from . import winutil

log = logging.getLogger(__name__)

_INVOKE, _TOGGLE, _SELECT, _EXPAND = 10000, 10015, 10010, 10005  # UIA のパターン ID
_BY_KIND = {  # 種類ごとに試すパターンの順番
    "チェックボックス": [_TOGGLE, _INVOKE],
    "タブ": [_SELECT, _INVOKE],
    "ラジオボタン": [_SELECT, _TOGGLE, _INVOKE],
    "リスト項目": [_SELECT, _INVOKE],
    "ツリー項目": [_SELECT, _EXPAND, _INVOKE],
    "コンボボックス": [_EXPAND],
}
_DEFAULT = [_INVOKE, _TOGGLE, _SELECT]


class ActionError(RuntimeError):
    pass


def run_isolated(fn, *args, timeout: float = 2.0):
    """COM を初期化した使い捨てのスレッドで fn を実行する。timeout 秒で戻らなければ「実行済み（応答待ち）」とみなす。"""
    box: dict = {}

    def target():
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            try:
                box["result"] = fn(*args)
            except Exception as e:  # 呼び出し元に渡す
                box["error"] = e

    t = threading.Thread(target=target, name="ui-action", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.info("操作の応答待ち（ダイアログが開いた可能性）: %s", getattr(fn, "__name__", fn))
        return "timeout"
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _find(hwnd: int, target):
    """探し直し：AutomationId と名前の両方が合うもの → 名前と位置が近いもの。"""
    import uiautomation as auto
    win = auto.ControlFromHandle(hwnd)
    found = []

    def walk(c, depth):
        if depth > 30:
            return
        for ch in c.GetChildren():
            name = " ".join((ch.Name or "").split())
            if name == target.name and (not target.auto_id or (ch.AutomationId or "") == target.auto_id):
                found.append(ch)
            walk(ch, depth + 1)

    walk(win, 0)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    tx, ty = target.center  # 同じ名前が複数あるときは、判定時の位置に一番近いもの
    def dist(ch):
        r = ch.BoundingRectangle
        return abs((r.left + r.right) // 2 - tx) + abs((r.top + r.bottom) // 2 - ty)
    return min(found, key=dist)


def act_on_element(hwnd: int, target, button: str = "left") -> str:
    """対象を探し直して操作する。使った方法を返す（ログ用）。"""
    ctrl = _find(hwnd, target)
    if ctrl is None:
        raise ActionError(f"「{target.name}」が見つかりません（画面が変わった可能性があります）")
    if not ctrl.IsEnabled:
        raise ActionError(f"「{target.name}」は今は押せません（無効になっています）")
    if button == "left":
        for pid in _BY_KIND.get(target.kind, _DEFAULT):
            try:
                pat = ctrl.GetPattern(pid)
            except Exception:
                pat = None
            if not pat:
                continue
            if pid == _INVOKE:
                pat.Invoke()
                return "Invoke"
            if pid == _TOGGLE:
                pat.Toggle()
                return "Toggle"
            if pid == _SELECT:
                pat.Select()
                return "Select"
            if pid == _EXPAND:
                pat.Expand()
                return "Expand"
    # パターンで操作できない・右クリック・ダブルクリック：探し直した最新の位置をクリック
    r = ctrl.BoundingRectangle
    if r.right - r.left <= 0:
        raise ActionError(f"「{target.name}」の位置が分かりません")
    winutil.set_cursor((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    import time
    time.sleep(0.03)
    if button == "double":
        winutil.click("left", 2)
    else:
        winutil.click(button)
    return "mouse"
