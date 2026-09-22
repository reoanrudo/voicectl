"""Win32 API の薄いラッパー（マウス・キーボード入力、ウィンドウ情報、ウィンドウ操作）。

座標はすべて物理ピクセル。起動時に set_dpi_aware() を呼ぶこと。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- SendInput 構造体 ----
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001
WHEEL_DELTA = 120

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# 自分が送った入力を低レベルフックで見分けるための印
INJECT_TAG = 0x564F4943  # "VOIC"


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, data: int = 0) -> INPUT:
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, INJECT_TAG)
    return i


def _key(vk: int, up: bool = False, scan: int = 0, unicode: bool = False) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_UNICODE if unicode else 0)
    if not unicode and vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(0 if unicode else vk, scan, flags, 0, INJECT_TAG)
    return i


# 矢印・Insert/Delete/Home/End/PageUp/PageDown などは拡張キー扱いが必要
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5,
                 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


# ---- DPI / 画面 ----
def set_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def monitor_rect_at(x: int, y: int) -> tuple[int, int, int, int]:
    """指定座標があるモニターの (left, top, right, bottom)。"""
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return r.left, r.top, r.right, r.bottom


def work_area_at(x: int, y: int) -> tuple[int, int, int, int]:
    """指定座標があるモニターの「作業領域」(left, top, right, bottom)。

    タスクバーやドッキングしたアプリを除いた範囲。モニター全体（monitor_rect_at）を基準に
    固定値でタスクバーを避けると、タスクバーが 2 段・拡大表示・自動的に隠す設定のときに
    重なったり浮きすぎたりするため、OS が返す作業領域を使う。
    """
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def taskbar_auto_hide() -> bool:
    """タスクバーが「自動的に隠す」設定か（作業領域がモニター全体と同じなら隠れている扱い）。"""
    l, t, r, b = monitor_rect_at(0, 0)
    wl, wt, wr, wb = work_area_at(0, 0)
    return (wl, wt, wr, wb) == (l, t, r, b)


def corner_position(work: tuple[int, int, int, int], w: int, h: int,
                    corner: str = "bottom_right", margin: int = 18,
                    keep_out: int = 0) -> tuple[int, int]:
    """作業領域の中の指定した隅に、(w, h) の矩形を収める左上座標を返す。

    keep_out: さらに画面外へはみ出さないよう内側に寄せる量（グローのはみ出しなど）。
    画面（作業領域）より大きいときは左上に合わせる。
    """
    l, t, r, b = work
    m = margin + keep_out
    if r - w - m < l + m:
        x = l
    elif "left" in corner:
        x = l + m
    else:
        x = r - w - m
    if b - h - m < t + m:
        y = t
    elif "top" in corner:
        y = t + m
    else:
        y = b - h - m
    return int(x), int(y)


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height)"""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


# ---- マウス ----
def get_cursor() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def set_cursor(x: int, y: int) -> None:
    left, top, w, h = virtual_screen()
    x = max(left, min(left + w - 1, int(x)))
    y = max(top, min(top + h - 1, int(y)))
    user32.SetCursorPos(x, y)


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(button: str = "left", count: int = 1) -> None:
    down, up = _BUTTON_FLAGS[button]
    for n in range(count):
        _send([_mouse(down), _mouse(up)])
        if n + 1 < count:
            time.sleep(0.05)


def mouse_down(button: str = "left") -> None:
    _send([_mouse(_BUTTON_FLAGS[button][0])])


def mouse_up(button: str = "left") -> None:
    _send([_mouse(_BUTTON_FLAGS[button][1])])


def release_all_buttons() -> None:
    for name, (_, up) in _BUTTON_FLAGS.items():
        vk = {"left": 0x01, "right": 0x02, "middle": 0x04}[name]
        if user32.GetAsyncKeyState(vk) & 0x8000:
            _send([_mouse(up)])


def scroll(notches: int, horizontal: bool = False) -> None:
    """notches > 0 で上（横なら右）。"""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send([_mouse(flag, notches * WHEEL_DELTA)])


# ---- キーボード ----
def press_combo(vks: list[int]) -> None:
    """修飾キーを含む組み合わせを押して離す（例: [CTRL, 'C']）。"""
    # 一括で送ると、アプリがキーを処理する時点で修飾キーがもう離れていることがあるため 1 つずつ送る
    for vk in vks:
        _send([_key(vk)])
        time.sleep(0.01)
    for vk in reversed(vks):
        _send([_key(vk, up=True)])
        time.sleep(0.01)


def type_unicode(text: str, gap_sec: float = 0.01, paste_over: int = 40) -> None:
    """文字列を入力する。短い文は 1 文字ずつ、長い文はクリップボード経由で貼り付ける。

    1 文字ずつ送る場合は間隔が必要：間隔なしで連続送信すると、Windows 11 のメモ帳などは
    文字の読み取りが遅れ、最後に送った文字が繰り返し入力される（実測）。
    """
    if len(text) > paste_over:
        paste_text(text)
        return
    for ch in text:
        if ch == "\n":
            _send([_key(0x0D)])
            _send([_key(0x0D, up=True)])
        else:
            code = ord(ch)
            units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10),
                                                   0xDC00 + ((code - 0x10000) & 0x3FF)]
            for u in units:
                _send([_key(0, scan=u, unicode=True)])
                _send([_key(0, up=True, scan=u, unicode=True)])
        time.sleep(gap_sec)


def paste_text(text: str) -> None:
    """クリップボードに入れて Ctrl+V。元のクリップボード（テキストの場合）は貼り付け後に戻す。"""
    import win32clipboard as cb
    previous = None
    cb.OpenClipboard()
    try:
        if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
            previous = cb.GetClipboardData(cb.CF_UNICODETEXT)
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_UNICODETEXT, text)
    finally:
        cb.CloseClipboard()
    press_combo([0x11, 0x56])
    time.sleep(0.3)  # 貼り付け先がクリップボードを読み終えるのを待つ
    if previous is not None:
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            cb.SetClipboardData(cb.CF_UNICODETEXT, previous)
        finally:
            cb.CloseClipboard()


# ---- ウィンドウ ----
@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    def to_state(self) -> dict:
        return {"title": self.title, "app": self.process}


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def window_info(hwnd: int) -> WindowInfo:
    return WindowInfo(hwnd, _window_title(hwnd), _process_name(hwnd), _rect(hwnd))


def foreground_window() -> WindowInfo | None:
    hwnd = user32.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


def _is_alt_tab_window(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
        return False
    if user32.GetWindow(hwnd, 4):  # GW_OWNER
        return False
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    if ex & 0x80:  # WS_EX_TOOLWINDOW
        return False
    cloaked = wintypes.DWORD()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    return cloaked.value == 0


def list_windows(exclude_pid: int | None = None) -> list[WindowInfo]:
    out: list[WindowInfo] = []
    own = exclude_pid if exclude_pid is not None else os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if _is_alt_tab_window(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own:
                out.append(window_info(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return out


def own_window_rects() -> list[tuple[int, int, int, int]]:
    """voicectl 自身（文字起こしバー・状態表示・番号表示）の表示中ウィンドウの範囲。

    OCR がこれらの文字を読むと、エージェントが自分の表示をクリックしてしまうので除外に使う。
    """
    out: list[tuple[int, int, int, int]] = []
    own = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own:
                r = _rect(hwnd)
                w, h = r[2] - r[0], r[3] - r[1]
                # 画面全体を覆う番号表示などは除外対象にしない（全要素が消えてしまうため）
                if 0 < w < 1600 and 0 < h < 900:
                    out.append(r)
        return True

    user32.EnumWindows(cb, 0)
    return out


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # フォアグラウンド変更の制限を外すため、自分から入力を 1 つ送っておく。
    # 定番の「Alt を押して離す」はメニューやアクセスキー表示を起動して後続のキー入力を吸われるので、
    # 移動量 0 のマウス入力を使う
    _send([_mouse(0x0001)])  # MOUSEEVENTF_MOVE
    if not user32.SetForegroundWindow(hwnd):
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(me, fg_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(me, fg_thread, False)
    if user32.GetForegroundWindow() != hwnd:
        # それでも前面に来ないとき（直前に人の操作がなく、OS が前面の切り替えを止めている）：
        # 最小化して元に戻すと、OS が切り替えを許す（最大化していたウィンドウは最大化に戻る）
        placement_max = bool(user32.IsZoomed(hwnd))
        user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        user32.ShowWindow(hwnd, 3 if placement_max else 9)   # SW_MAXIMIZE / SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def window_command(hwnd: int, action: str) -> None:
    WM_SYSCOMMAND = 0x0112
    cmds = {"minimize": 0xF020, "maximize": 0xF030, "restore": 0xF120, "close": 0xF060}
    if action in cmds:
        user32.PostMessageW(hwnd, WM_SYSCOMMAND, cmds[action], 0)


def set_topmost(hwnd: int, on: bool) -> None:
    """ウィンドウを常に手前に表示する（on=False で解除）。位置と大きさは変えない。"""
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
    user32.SetWindowPos(hwnd, wintypes.HWND(-1) if on else wintypes.HWND(-2),
                        0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def place_rect(hwnd: int, name: str) -> None:
    """ウィンドウを作業領域の決まった割合の位置に置く（上半分・四分の 1・中央など。Win+矢印では出せない形）。"""
    from .schema import SNAP_RECTS
    fx0, fy0, fx1, fy1 = SNAP_RECTS[name]
    r = _rect(hwnd)
    l, t, rt, b = work_area_at((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE（最大化中は位置を変えられない）
    user32.SetWindowPos(hwnd, 0, int(l + (rt - l) * fx0), int(t + (b - t) * fy0),
                        int((rt - l) * (fx1 - fx0)), int((b - t) * (fy1 - fy0)), 0x0040 | 0x0004)
