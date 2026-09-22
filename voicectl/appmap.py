"""アプリマップ：アプリのメニュー構造とショートカットを UI Automation で読み取って保存し、声でメニューを実行する。

「このアプリを覚えて」で前面のアプリを読み取る（メニューを順に開いて中身を記録する。何も実行はしない）。
保存先は cache/appmaps/<exe 名>.json。実行時はショートカットがあればキー操作、なければメニューをたどって Invoke する。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ROOT

log = logging.getLogger(__name__)
MAP_DIR = ROOT / "cache" / "appmaps"
_MENU_ITEM, _MENU, _MENUBAR = 50011, 50009, 50010


@dataclass
class MenuEntry:
    path: list[str]            # 例: ["ファイル", "名前を付けて保存"]
    shortcut: str = ""         # 例: "Ctrl+Shift+S"
    aliases: list[str] = field(default_factory=list)  # 日本語の言い方（プロファイルの menu_aliases から付ける）

    @property
    def label(self) -> str:
        return (" > ".join(self.path) + (f"（{self.shortcut}）" if self.shortcut else "")
                + (f"（別名: {'・'.join(self.aliases)}）" if self.aliases else ""))


@dataclass
class ControlEntry:
    name: str
    kind: str                  # ボタン / チェックボックス / タブ など
    auto_id: str = ""
    group: str = ""            # 所属グループ名（「Select data to display」など）
    window: str = ""           # どのウィンドウの部品か（タイトル）


@dataclass
class AppMap:
    exe: str
    title: str
    scanned_at: str
    entries: list[MenuEntry] = field(default_factory=list)
    controls: list[ControlEntry] = field(default_factory=list)

    def control_for(self, name: str, auto_id: str) -> ControlEntry | None:
        for c in self.controls:
            if c.name == name and (not auto_id or not c.auto_id or c.auto_id == auto_id):
                return c
        return None


def _clean(name: str) -> tuple[str, str]:
    """「名前を付けて保存(&A)...\tCtrl+Shift+S」→ ("名前を付けて保存", "Ctrl+Shift+S")"""
    shortcut = ""
    if "\t" in name:
        name, shortcut = name.split("\t", 1)
    name = re.sub(r"\(&?\w\)|&", "", name)  # アクセスキー表記を除く
    name = re.sub(r"(\.\.\.|…)$", "", name).strip()
    return name, shortcut.strip()


def _key(exe: str) -> Path:
    return MAP_DIR / (re.sub(r"[^\w.-]", "_", exe.lower()) + ".json")


def load(exe: str) -> AppMap | None:
    p = _key(exe)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return AppMap(raw["exe"], raw["title"], raw["scanned_at"],
                  [MenuEntry(e["path"], e.get("shortcut", "")) for e in raw["entries"]],
                  [ControlEntry(**c) for c in raw.get("controls", [])])


def save(m: AppMap) -> Path:
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    p = _key(m.exe)
    data = asdict(m)
    for e in data["entries"]:
        e.pop("aliases", None)  # 言い方はプロファイル側で持つ
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


_CONTROL_TYPES = {50000: "ボタン", 50002: "チェックボックス", 50013: "ラジオボタン", 50019: "タブ", 50003: "コンボボックス",
                  50005: "リンク", 50031: "分割ボタン", 50004: "入力欄", 50015: "スライダー"}


def scan_controls(hwnd: int, title: str) -> list[ControlEntry]:
    """ウィンドウ内のボタン・チェックボックス・タブなどを、所属グループと一緒に記録する。UIA スレッドで呼ぶこと。"""
    import uiautomation as auto
    win = auto.ControlFromHandle(hwnd)
    out: list[ControlEntry] = []

    def walk(c, depth, group):
        if depth > 30:
            return
        for ch in c.GetChildren():
            ct = ch.ControlType
            name = " ".join((ch.Name or "").split())  # 改行や連続空白を 1 つの空白に
            if ct == 50037:  # タイトルバー（最小化・閉じる）は除く
                continue
            g = group
            if ct in (50026, 50018) and name:  # Group / Tab の名前を所属として引き継ぐ
                g = name
            if ct in _CONTROL_TYPES and name and not re.search(r"Row \d+", name) and len(name) <= 60:
                out.append(ControlEntry(name, _CONTROL_TYPES[ct], ch.AutomationId or "", group, title))
            walk(ch, depth + 1, g)

    walk(win, 0, "")
    return out


# ---- 読み取り（UIA スレッドで呼ぶこと） ----
class MenuScanner:
    def __init__(self, max_depth: int = 3, settle_sec: float = 0.35):
        self.max_depth = max_depth
        self.settle = settle_sec

    def scan(self, hwnd: int, exe: str, title: str) -> AppMap:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        bar = self._find_menubar(win)
        entries: list[MenuEntry] = []
        if bar is None:
            log.info("メニューバーが見つかりません: %s", exe)
        else:
            for top in bar.GetChildren():
                if top.ControlType != _MENU_ITEM:
                    continue
                name, sc = _clean(top.Name or "")
                if not name:
                    continue
                self._walk_item(top, [name], entries, 1, win)
        m = AppMap(exe, title, time.strftime("%Y-%m-%d %H:%M"), entries)
        log.info("アプリマップ: %s — %d 項目", exe, len(entries))
        return m

    @staticmethod
    def _find_menubar(win):
        """アプリのメニューバーを探す（クラシックなアプリにある「システム」メニューバーは除く）。"""
        bars = []

        def visit(c, depth):
            if depth > 6:
                return
            for ch in c.GetChildren():
                if ch.ControlType == _MENUBAR:
                    bars.append(ch)
                elif depth < 6:
                    visit(ch, depth + 1)

        visit(win, 0)
        for b in bars:
            if (b.Name or "") not in ("システム", "System", "System Menu Bar"):
                return b
        return None

    def _walk_item(self, item, path, entries, depth, win):
        """メニュー項目を開いて子を記録する。子がなければ実行できる項目として登録する。"""
        try:
            pat = item.GetExpandCollapsePattern()
        except Exception:
            pat = None
        children = []
        if pat is not None and depth <= self.max_depth:
            try:
                pat.Expand()
                time.sleep(self.settle)
                children = self._popup_items(item, win)
            except Exception:
                log.debug("展開できません: %s", path)
        if not children:
            if depth > 1:  # 最上位（「ファイル」など）そのものは実行対象にしない
                # 走査中にメニューが閉じると項目の COM オブジェクトが無効になって例外が出る（JW_CAD で発生）。
                # その項目のショートカットだけ諦め、走査全体は止めない
                try:
                    sc = item.AcceleratorKey or ""
                    name, sc2 = _clean(item.Name or "")
                except Exception:
                    sc, sc2 = "", ""
                entries.append(MenuEntry(path, sc2 or sc))
        else:
            for ch in children:
                try:
                    name, sc = _clean(ch.Name or "")
                except Exception:
                    continue   # 消えた項目は飛ばす
                if not name or name in ("-",):
                    continue
                self._walk_item(ch, path + [name], entries, depth + 1, win)
        if pat is not None and children:
            try:
                pat.Collapse()
                time.sleep(0.1)
            except Exception:
                pass

    @staticmethod
    def _popup_items(item, win):
        """展開して現れたメニューの項目。項目の子として出るアプリと、別ウィンドウ（#32768）として出るアプリがある。"""
        import uiautomation as auto
        kids = [c for c in item.GetChildren() if c.ControlType == _MENU_ITEM]
        if kids:
            return kids
        for menu in item.GetChildren():
            if menu.ControlType == _MENU:
                return [c for c in menu.GetChildren() if c.ControlType == _MENU_ITEM]
        # クラシックな Win32 メニュー：デスクトップ直下のポップアップ
        for c in auto.GetRootControl().GetChildren():
            if c.ControlType == _MENU or c.ClassName == "#32768":
                items = [x for x in c.GetChildren() if x.ControlType == _MENU_ITEM]
                if items:
                    return items
        # WinUI（Windows 11 のメモ帳など）はウィンドウ内の PopupHost に出る。サブメニューは別の PopupHost に重なるので、
        # 開いた項目の兄弟（＝親メニューの項目）を含まない、最も新しいポップアップを採用する
        parent_level = {(_clean(s.Name or "")[0]) for s in (item.GetParentControl().GetChildren()
                                                            if item.GetParentControl() else [])}
        found = []
        for c in win.GetChildren():
            if c.ControlType == _MENU or "Popup" in (c.ClassName or ""):
                items = _descendant_items(c, 6)
                names = {_clean(x.Name or "")[0] for x in items}
                if items and not (names & parent_level and len(names & parent_level) == len(names)):
                    found = [x for x in items if _clean(x.Name or "")[0] not in parent_level] or items
        return found


def _descendant_items(c, depth: int) -> list:
    """c の子孫にあるメニュー項目（メニュー項目の中へは潜らない）。"""
    out = []
    for ch in c.GetChildren():
        if ch.ControlType == _MENU_ITEM:
            out.append(ch)
        elif depth > 0:
            out.extend(_descendant_items(ch, depth - 1))
    return out


# ---- 実行 ----
_VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B,
       "escape": 0x1B, "tab": 0x09, "space": 0x20, "delete": 0x2E, "del": 0x2E, "backspace": 0x08, "home": 0x24,
       "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
       "insert": 0x2D, "plus": 0xBB, "minus": 0xBD, "+": 0xBB, "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE,
       "/": 0xBF, "\\": 0xDC, ";": 0xBA, ":": 0xBA, "[": 0xDB, "]": 0xDD, "'": 0xDE}


def parse_keys(spec: str) -> list[int] | None:
    """「Ctrl+Shift+S」「F5」「Alt+Enter」を仮想キーの列にする。読めなければ None。"""
    spec = spec.strip()
    if not spec:
        return None
    parts = re.split(r"\s*\+\s*(?=.)", spec)
    vks = []
    for p in parts:
        k = p.strip().lower()
        if k in _VK:
            vks.append(_VK[k])
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", k):
            vks.append(0x6F + int(k[1:]))
        elif len(k) == 1 and (k.isalnum()):
            vks.append(ord(k.upper()))
        else:
            return None
    return vks


def invoke_path(hwnd: int, path: list[str], settle_sec: float = 0.35) -> bool:
    """メニューを順に開いて最後の項目を実行する（ショートカットがない項目用）。UIA スレッドで呼ぶこと。"""
    import uiautomation as auto
    win = auto.ControlFromHandle(hwnd)
    bar = MenuScanner._find_menubar(win)
    if bar is None:
        return False
    current = [c for c in bar.GetChildren() if c.ControlType == _MENU_ITEM]
    for i, name in enumerate(path):
        target = next((c for c in current if _clean(c.Name or "")[0] == name), None)
        if target is None:
            _escape_menus()
            return False
        last = i == len(path) - 1
        if last:
            try:
                target.GetInvokePattern().Invoke()
            except Exception:
                target.Click(simulateMove=False)
            return True
        try:
            target.GetExpandCollapsePattern().Expand()
        except Exception:
            target.Click(simulateMove=False)
        time.sleep(settle_sec)
        current = MenuScanner._popup_items(target, win)
    return False


def _escape_menus() -> None:
    from . import winutil
    for _ in range(3):
        winutil.press_combo([0x1B])
