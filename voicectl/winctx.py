"""Windows の「今の状況」を理解する：前面は何の種類の窓か・ダイアログが出ているか・何にフォーカスがあるか・
エクスプローラーならどのフォルダで何を選んでいるか。

Jev は画面を見られないので、この状況を型の決まった事実（facts.windows）として渡し、同じ言葉でも状況に合った
操作を選ばせる（「閉じて」はダイアログなら×/キャンセル、「開いて」はエクスプローラーで選んだファイルなら開く）。
ダイアログへの返事（「はい」「保存しない」「OK」）とエクスプローラーの操作は、Jev を待たずにここで即実行できる。
"""
from __future__ import annotations

import ctypes
import logging
import re
from dataclasses import dataclass, field

from . import winutil

log = logging.getLogger(__name__)
user32 = ctypes.windll.user32

_KIND_BY_PROC = {
    "explorer.exe": "explorer", "chrome.exe": "browser", "msedge.exe": "browser", "brave.exe": "browser",
    "firefox.exe": "browser", "opera.exe": "browser", "vivaldi.exe": "browser",
    "notepad.exe": "editor", "code.exe": "editor", "obsidian.exe": "editor", "winword.exe": "office",
    "excel.exe": "office", "powerpnt.exe": "office", "windowsterminal.exe": "terminal", "cmd.exe": "terminal",
    "powershell.exe": "terminal", "pwsh.exe": "terminal", "vlc.exe": "media", "applicationframehost.exe": "uwp",
    "systemsettings.exe": "settings", "discord.exe": "chat", "slack.exe": "chat",
}


@dataclass
class WinContext:
    kind: str = "other"                 # explorer / browser / editor / dialog / terminal / media / office / ...
    title: str = ""
    process: str = ""
    state: str = "normal"               # maximized / normal / minimized
    dialog: bool = False                # 前面がダイアログ（メッセージボックス・保存確認など）か
    dialog_buttons: list[str] = field(default_factory=list)
    focus_type: str = ""                # 入力欄 / 文書 / 一覧 / ツリー / ボタン など
    focus_name: str = ""
    folder: str = ""                    # エクスプローラーの今のフォルダ
    selected: list[str] = field(default_factory=list)   # エクスプローラーで選んでいる項目
    items: list[str] = field(default_factory=list)      # エクスプローラーの表示中の項目（先頭から）

    def to_facts(self) -> dict:
        f = {"window_kind": self.kind, "window_state": self.state}
        if self.dialog:
            f["dialog_open"] = True
            f["dialog_buttons"] = self.dialog_buttons[:12]
        if self.focus_type:
            f["focused"] = f"{self.focus_type}: {self.focus_name[:40]}" if self.focus_name else self.focus_type
        if self.folder:
            f["explorer_folder"] = self.folder
            f["explorer_selected"] = self.selected[:10]
        return f


_FOCUS_TYPES = {"EditControl": "入力欄", "DocumentControl": "文書", "ListControl": "一覧", "ListItemControl": "一覧の項目",
                "TreeControl": "ツリー", "TreeItemControl": "ツリーの項目", "ButtonControl": "ボタン",
                "ComboBoxControl": "選択欄", "TabItemControl": "タブ", "MenuItemControl": "メニュー",
                "DataItemControl": "表の項目", "HyperlinkControl": "リンク", "PaneControl": "領域"}


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_dialog(hwnd: int) -> bool:
    """メッセージボックス（#32770）か、ほかのウィンドウに属する小さめのウィンドウ（保存確認など）。"""
    if not hwnd:
        return False
    if _class_name(hwnd) == "#32770":
        return True
    owner = user32.GetWindow(hwnd, 4)   # GW_OWNER
    if not owner:
        return False
    r = winutil._rect(hwnd)
    return (r[2] - r[0]) < 1400 and (r[3] - r[1]) < 900


def collect(fg: winutil.WindowInfo | None = None, explorer: bool = True) -> WinContext:
    """今の状況を集める（UI Automation の呼び出しは呼び出し元のスレッドで行う。0.1 秒前後）。"""
    fg = fg or winutil.foreground_window()
    ctx = WinContext()
    if fg is None:
        return ctx
    ctx.title, ctx.process = fg.title, fg.process
    proc = fg.process.lower()
    ctx.kind = _KIND_BY_PROC.get(proc, "other")
    if user32.IsZoomed(fg.hwnd):
        ctx.state = "maximized"
    elif user32.IsIconic(fg.hwnd):
        ctx.state = "minimized"
    if is_dialog(fg.hwnd):
        ctx.kind, ctx.dialog = "dialog", True
        ctx.dialog_buttons = dialog_buttons(fg.hwnd)
    try:
        import uiautomation as auto
        f = auto.GetFocusedControl()
        if f is not None:
            ctx.focus_type = _FOCUS_TYPES.get(f.ControlTypeName, "")
            ctx.focus_name = (f.Name or "")[:60]
    except Exception:
        pass
    if explorer and proc == "explorer.exe" and not ctx.dialog:
        try:
            ctx.folder, ctx.selected, ctx.items = explorer_state(fg.hwnd)
        except Exception:
            log.debug("エクスプローラーの状態を取れません", exc_info=True)
    return ctx


# ---- ダイアログ ----
def dialog_buttons(hwnd: int) -> list[str]:
    import uiautomation as auto
    out = []
    try:
        win = auto.ControlFromHandle(hwnd)
        for c, _ in auto.WalkControl(win, maxDepth=6):
            if c.ControlTypeName == "ButtonControl" and c.Name and c.IsEnabled:
                out.append(" ".join(c.Name.split()))
    except Exception:
        pass
    return list(dict.fromkeys(out))


# ダイアログへの返事の言い方 → ボタン名の候補（上から順に探す）
_DIALOG_WORDS = [
    (r"(はい|イエス|yes|うん|そう|オッケー|ok|いいよ|大丈夫|了解|続行|続けて)", ["はい", "Yes", "OK", "続行", "続ける", "保存", "開く", "許可"]),
    (r"(いいえ|ノー|no|いや|だめ|違う)", ["いいえ", "No"]),
    (r"(保存しない|保存せず|保存しないで|捨てて|破棄)", ["保存しない", "Don't Save", "保存しない(N)"]),
    (r"(保存して|保存する|セーブ)", ["保存", "保存する", "Save"]),
    (r"(キャンセル|やめて|やめる|取り消し|閉じて|とじて)", ["キャンセル", "Cancel", "閉じる", "Close"]),
    (r"(次へ|次|つぎ|進んで)", ["次へ", "次へ(N)", "Next"]),
    (r"(戻る|前へ|戻って)", ["戻る", "< 戻る(B)", "Back"]),
    (r"(完了|終了|フィニッシュ)", ["完了", "Finish", "終了"]),
    (r"(置き換え|上書き)", ["置き換える", "ファイルを置き換える", "上書き", "Replace"]),
    (r"(スキップ|飛ばして)", ["スキップ", "このファイルをスキップ", "Skip"]),
    (r"(許可|許して)", ["許可", "Allow", "はい"]),
    (r"(再試行|もう一回|もう一度)", ["再試行", "Retry"]),
]


def dialog_target(text: str, buttons: list[str]) -> str | None:
    """ダイアログに出ているボタンのうち、発話が指すもの。ボタン名そのものを言ったときはそれを優先する。"""
    c = re.sub(r"[\s。、！!？?]", "", text).lower()
    c = re.sub(r"(を)?(押して|クリック|して|ください|で)$", "", c)
    norm = {b: re.sub(r"[\s()（）&_.…]|\([A-Z]\)", "", b).lower() for b in buttons}
    for b, n in norm.items():   # 「保存しない」「置き換える」などボタン名そのもの
        if n and (c == n or (len(n) >= 2 and c == re.sub(r"\(.\)$", "", n))):
            return b
    for pat, names in _DIALOG_WORDS:
        if re.fullmatch(pat + r"(て|る)?(で|を)?(お願い|します|して|する)?", c, flags=re.I):
            for name in names:
                key = re.sub(r"[\s()（）&_.…]|\([A-Z]\)", "", name).lower()
                for b, n in norm.items():
                    if n == key or n.startswith(key):
                        return b
    return None


def press_dialog_button(hwnd: int, name: str) -> str:
    """ダイアログのボタンを UI Automation で押す（Invoke → だめなら位置をクリック）。"""
    from types import SimpleNamespace

    from . import uiaction
    return uiaction.run_isolated(uiaction.act_on_element, hwnd,
                                 SimpleNamespace(name=name, auto_id="", kind="ボタン", center=(0, 0)), "left",
                                 timeout=2.5)


# ---- エクスプローラー（Shell の COM で、表示とは別に確実に操作する） ----
def _shell_window(hwnd: int):
    import win32com.client
    shell = win32com.client.Dispatch("Shell.Application")
    for w in shell.Windows():
        try:
            if int(w.HWND) == int(hwnd):
                return w
        except Exception:
            continue
    return None


def explorer_state(hwnd: int) -> tuple[str, list[str], list[str]]:
    w = _shell_window(hwnd)
    if w is None:
        return "", [], []
    doc = w.Document
    folder = doc.Folder.Self.Path
    selected = [it.Name for it in doc.SelectedItems()][:20]
    return folder, selected, []   # 項目の一覧は UI Automation で Jev に渡っているので、ここでは取らない（遅いため）


def explorer_sort(hwnd: int, by: str, descending: bool) -> None:
    prop = {"date": "System.DateModified", "name": "System.ItemNameDisplay", "size": "System.Size",
            "type": "System.ItemTypeText"}[by]
    w = _shell_window(hwnd)
    w.Document.SortColumns = f"prop:{'-' if descending else ''}{prop};"


def explorer_open_nth(hwnd: int, n: int) -> str:
    """表示順で n 番目の項目を開く（UI Automation の一覧の並び＝見えている順）。"""
    import uiautomation as auto
    win = auto.ControlFromHandle(hwnd)
    items = []
    for c, _ in auto.WalkControl(win, maxDepth=14):
        if c.ControlTypeName == "ListItemControl" and c.Name:
            items.append(c)
    if not 1 <= n <= len(items):
        raise ValueError(f"{n} 番目の項目はありません（{len(items)} 件）")
    it = items[n - 1]
    it.GetSelectionItemPattern().Select()
    it.DoubleClick(simulateMove=False)
    return it.Name


def explorer_open_selected(hwnd: int) -> list[str]:
    """選んでいる項目を既定の動作（開く）で開いて、名前を返す。"""
    w = _shell_window(hwnd)
    if w is None:
        raise ValueError("エクスプローラーが見つかりません")
    names = []
    for it in w.Document.SelectedItems():
        it.InvokeVerb()   # 引数なし＝既定の動作（ふつうは「開く」）
        names.append(it.Name)
    if not names:
        raise ValueError("選んでいる項目がありません")
    return names


def selected_paths(hwnd: int) -> list[str]:
    w = _shell_window(hwnd)
    return [it.Path for it in w.Document.SelectedItems()] if w is not None else []


def copy_or_move(hwnd: int, dest_shell: str, move: bool) -> tuple[int, str]:
    """選んでいる項目を、デスクトップ・ダウンロードなどへコピー（移動）する。(件数, 行き先の名前)"""
    import win32com.client
    paths = selected_paths(hwnd)
    if not paths:
        raise ValueError("選んでいる項目がありません")
    shell = win32com.client.Dispatch("Shell.Application")
    dest = shell.Namespace(dest_shell)
    if dest is None:
        raise ValueError("行き先のフォルダが見つかりません")
    for p in paths:
        (dest.MoveHere if move else dest.CopyHere)(p, 0)
    return len(paths), dest.Title


def open_terminal_here(folder: str) -> None:
    import shutil
    import subprocess
    if shutil.which("wt"):
        subprocess.Popen(["wt", "-d", folder], creationflags=0x08000000)
    else:
        subprocess.Popen(["cmd", "/c", "start", "powershell", "-NoExit", "-Command", f"Set-Location '{folder}'"],
                         creationflags=0x08000000)


# ---- タスクバー（Win11 のアプリボタン。スタートとタスクビューは数えない）----
_TASKBAR_SKIP = ("スタート", "タスク ビュー", "タスクビュー", "start", "task view")


def _taskbar_buttons():
    import uiautomation as auto
    import win32gui
    hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
    if not hwnd:
        raise ValueError("タスクバーが見つかりません")
    win = auto.ControlFromHandle(hwnd)
    btns: list = []

    def walk(c, d):
        if d > 14 or len(btns) > 60:
            return
        try:
            if c.ControlTypeName == "ButtonControl" and c.Name and not c.IsOffscreen \
                    and c.Name.lower() not in _TASKBAR_SKIP:
                btns.append(c)
        except Exception:
            return
        try:
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win, 0)
    if not btns:
        raise ValueError("タスクバーのボタンが読めません")
    return btns


def taskbar_click(n: int, name: str = "", dry_run: bool = False) -> str:
    """タスクバーの n 番目（または名前に含まれる言葉が一致する）ボタンを押して、名前を返す。

    dry_run=True のときは押さずに「名前@x,y」を返す（実験と座標の確認用）。
    """
    import time as _time
    from . import winutil
    btns = _taskbar_buttons()
    if name:
        nm = name.lower()
        target = next((b for b in btns if nm in (b.Name or "").lower()), None)
        if target is None:
            raise ValueError(f"タスクバーに「{name}」がありません")
    else:
        if not 1 <= n <= len(btns):
            raise ValueError(f"タスクバーの {n} 番目はありません（{len(btns)} 個）")
        target = btns[n - 1]
    label = (target.Name or "?")[:24]
    r = target.BoundingRectangle
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if dry_run:
        return f"{label}@{cx},{cy}"
    winutil.set_cursor(cx, cy)
    _time.sleep(0.03)
    winutil.click("left")
    return label


# ---- ブラウザの URL（後で読む用）----
def browser_url(fg) -> str | None:
    """前面のブラウザの URL を UIA で読む（アドレスバーの値。取れなければ None）。"""
    import uiautomation as auto
    win = auto.ControlFromHandle(fg.hwnd)
    out: list[str] = []

    def walk(c, d):
        if d > 16 or out:
            return
        try:
            if c.ControlTypeName == "EditControl" and not c.IsOffscreen:
                try:
                    v = (c.GetValuePattern().Value or "").strip()
                except Exception:
                    v = ""
                if v and " " not in v and ("." in v or v.startswith("http")):
                    out.append(v)
        except Exception:
            return
        try:
            for ch in c.GetChildren():
                walk(ch, d + 1)
        except Exception:
            pass

    walk(win, 0)
    if not out:
        return None
    v = out[0]
    return v if v.startswith("http") else "https://" + v

