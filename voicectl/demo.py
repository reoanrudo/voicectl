"""やって見せた操作を覚えて、あとで再生する（「〇〇の手順を記録して」〜「記録終了」）。

記録するもの：
  - マウスのクリック：押した瞬間に、その位置の画面要素（名前・AutomationId・種類）とウィンドウ内の相対位置
  - 記録中に声で実行した命令：その発話（再生時に同じ発話として処理する）
キーボードの入力は記録しない（パスワードなどを残さないため）。文字を入れたい手順は「〇〇と入力」と声で言う。

再生：クリックは UI Automation で要素を探し直して操作し（位置がずれても当たる）、見つからないときだけ
記録したウィンドウ内の相対位置をクリックする。
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes
from types import SimpleNamespace

from . import winutil

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_MOUSE_LL = 14
WM_LBUTTONDOWN, WM_RBUTTONDOWN = 0x0201, 0x0204
WM_QUIT = 0x0012
LLMHF_INJECTED = 0x01


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK

_UIA_KIND = {50000: "ボタン", 50011: "メニュー項目", 50005: "リンク", 50007: "リスト項目", 50019: "タブ",
             50024: "ツリー項目", 50002: "チェックボックス", 50013: "ラジオボタン", 50003: "コンボボックス",
             50004: "入力欄", 50031: "分割ボタン", 50029: "データ項目", 50020: "テキスト", 50006: "画像",
             50034: "ヘッダー項目"}


class DemoRecorder:
    """クリックと声の命令を順に記録する。start() から stop() まで。"""

    def __init__(self):
        self.name = ""
        self.steps: list = []
        self.active = False
        self._thread_id = 0
        self._clicks: queue.Queue = queue.Queue()
        self._own_rects: list = []

    # ---- 記録の開始・終了 ----
    def start(self, name: str) -> None:
        self.name, self.steps, self.active = name, [], True
        self._own_rects = winutil.own_window_rects()
        threading.Thread(target=self._hook_loop, daemon=True, name="demo-hook").start()
        threading.Thread(target=self._resolve_loop, daemon=True, name="demo-uia").start()

    def stop(self) -> list:
        self.active = False
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._clicks.put(None)
        time.sleep(0.3)   # 最後のクリックの要素の取得を待つ
        steps, self.steps = self.steps, []
        # 「記録終了」と言う前の、状態表示などへのクリックは除く
        return [s for s in steps if s]

    def add_utterance(self, text: str) -> None:
        if self.active:
            self.steps.append(text)

    # ---- マウスのフック（押した瞬間の位置を記録） ----
    def _hook_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()

        @HOOKPROC
        def proc(code, wparam, lparam):
            if code == 0 and wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN) and self.active:
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not info.flags & LLMHF_INJECTED:   # voicectl 自身が送ったクリックは記録しない
                    self._clicks.put((info.pt.x, info.pt.y, "right" if wparam == WM_RBUTTONDOWN else "left",
                                      len(self.steps)))
                    self.steps.append(None)   # 順番を保つための空き（要素を取得したら埋める）
            return user32.CallNextHookEx(None, code, wparam, lparam)

        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, None, 0)
        if not hook:
            log.error("マウスのフックを設定できません")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
        self._thread_id = 0

    def _resolve_loop(self) -> None:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            while True:
                item = self._clicks.get()
                if item is None:
                    return
                x, y, button, slot = item
                try:
                    step = self._describe(auto, x, y, button)
                except Exception:
                    log.exception("クリックした要素を取得できません")
                    step = None
                if slot < len(self.steps):
                    self.steps[slot] = step

    def _describe(self, auto, x: int, y: int, button: str) -> dict | None:
        if any(l <= x <= r and t <= y <= b for l, t, r, b in self._own_rects):
            return None   # voicectl の表示へのクリック
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        top = user32.GetAncestor(hwnd, 2) if hwnd else 0   # GA_ROOT
        if not top:
            return None
        win = winutil.window_info(top)
        l, t, r, b = win.rect
        w, h = max(r - l, 1), max(b - t, 1)
        name, auto_id, kind = "", "", ""
        try:
            c = auto.ControlFromPoint(x, y)
            if c is not None:
                name = " ".join((c.Name or "").split())[:80]
                auto_id = c.AutomationId or ""
                kind = _UIA_KIND.get(c.ControlType, "要素")
        except Exception:
            pass
        return {"click": {"proc": win.process.lower(), "title": win.title[:80], "name": name, "auto_id": auto_id,
                          "kind": kind, "rel": [round((x - l) / w, 4), round((y - t) / h, 4)], "button": button}}


def describe_step(step) -> str:
    if isinstance(step, str):
        return f"「{step}」"
    if "menu" in step:
        return "メニュー「" + " > ".join(step["menu"].get("path", [])) + "」"
    if "focus" in step:
        return f"「{step['focus'].get('title', '')[:20]}」に切り替え"
    if "type" in step:
        return f"「{step['type']}」を入力"
    if "url" in step:
        return f"{step['url'][:40]} を開く"
    if "scroll" in step:
        return "スクロール"
    if "keys" in step:
        return "キー操作"
    c = step.get("click", {})
    where = c.get("proc", "").replace(".exe", "")
    return f"{where} の「{c.get('name') or '（名前なし）'}」をクリック"


def focus_proc(proc: str, title: str = "") -> winutil.WindowInfo:
    from . import uiaction
    win = _window_for(proc, title)
    if win is None:
        raise uiaction.ActionError(f"{proc} のウィンドウが開いていません")
    fg = winutil.foreground_window()
    if not fg or fg.hwnd != win.hwnd:
        winutil.focus_window(win.hwnd)
        time.sleep(0.4)
    return winutil.window_info(win.hwnd)


def replay_step(step: dict, run_menu) -> str:
    """記録（やって見せた・エージェントが成功した）した 1 手を再生する。run_menu はメニューを実行する関数。"""
    if "click" in step:
        return replay_click(step)
    if "menu" in step:
        m = step["menu"]
        win = focus_proc(m["proc"])
        run_menu(win, SimpleNamespace(path=m["path"], shortcut=m.get("shortcut", "")))
        return "メニュー"
    if "focus" in step:
        focus_proc(step["focus"]["proc"], step["focus"].get("title", ""))
        return "切り替え"
    if "type" in step:
        winutil.type_unicode(step["type"])
        return "入力"
    if "url" in step:
        from . import tasks
        return tasks.open_url(step["url"])
    if "scroll" in step:
        winutil.scroll(int(step["scroll"]))
        return "スクロール"
    if "keys" in step:
        winutil.press_combo([int(k) for k in step["keys"]])
        return "キー"
    raise ValueError(f"再生できない手順です: {step}")


def _window_for(proc: str, title: str) -> winutil.WindowInfo | None:
    wins = [w for w in winutil.list_windows() if w.process.lower() == proc]
    if not wins:
        return None
    fg = winutil.foreground_window()
    if fg and fg.process.lower() == proc:
        return fg
    same = [w for w in wins if w.title == title]
    return (same or wins)[0]


def replay_click(step: dict) -> str:
    """記録したクリックを再生する。使った方法を返す。"""
    from . import uiaction
    c = step["click"]
    win = _window_for(c["proc"], c.get("title", ""))
    if win is None:
        raise uiaction.ActionError(f"{c['proc']} のウィンドウが開いていません")
    fg = winutil.foreground_window()
    if not fg or fg.hwnd != win.hwnd:
        winutil.focus_window(win.hwnd)
        time.sleep(0.4)
        win = winutil.window_info(win.hwnd)
    l, t, r, b = win.rect
    x = int(l + c["rel"][0] * (r - l))
    y = int(t + c["rel"][1] * (b - t))
    if c.get("name"):
        target = SimpleNamespace(name=c["name"], auto_id=c.get("auto_id", ""), kind=c.get("kind", ""), center=(x, y))
        try:
            how = uiaction.run_isolated(uiaction.act_on_element, win.hwnd, target, c.get("button", "left"), timeout=3.0)
            return f"UIA（{how}）"
        except uiaction.ActionError as e:
            log.info("記録した要素が見つからないため位置でクリック: %s", e)
    winutil.set_cursor(x, y)
    time.sleep(0.05)
    winutil.click(c.get("button", "left"))
    return "位置"
