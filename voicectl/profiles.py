"""アプリごとのプロファイル：そのアプリが前面にあるときだけ有効になる、用語辞書・認識ヒント・独自コマンド。

profiles/*.yaml に 1 アプリ 1 ファイルで書く。書式は profiles/README.md を参照。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT

log = logging.getLogger(__name__)
PROFILE_DIR = ROOT / "profiles"


@dataclass
class AppCommand:
    name: str
    say: list[str]            # 言い方の例（照合と Jev の判断材料に使う）
    steps: list[dict]         # [{keys: "Ctrl+B"}, {menu: [...]}, {text: "..."}, {wait: 0.5}, {click: "名前"}]
    confirm: bool = False     # 実行前に必ず確認する

    @property
    def label(self) -> str:
        return self.name + (f"（言い方: {'・'.join(self.say)}）" if self.say else "")


@dataclass
class Profile:
    name: str
    exe: list[str] = field(default_factory=list)      # 実行ファイル名の一部（小文字で照合）
    title: list[str] = field(default_factory=list)    # ウィンドウタイトルの一部
    launch: str | None = None                         # スタートメニューにないアプリの起動パス
    readings: list[str] = field(default_factory=list)  # アプリ名の読み（起動・切り替え用）
    dictionary: dict[str, str] = field(default_factory=dict)
    vocabulary: list[str] = field(default_factory=list)
    commands: list[AppCommand] = field(default_factory=list)
    menu_aliases: dict[str, list[str]] = field(default_factory=dict)  # "File > Save full tree": [ツリーを保存, ...]
    control_aliases: dict[str, list[str]] = field(default_factory=dict)  # "Board" または "EV#radio0": [言い方, ...]
    path: Path | None = None

    def matches(self, exe: str, title: str) -> bool:
        e, t = (exe or "").lower(), (title or "").lower()
        return any(x.lower() in e for x in self.exe if x) or any(x.lower() in t for x in self.title if x)


def _parse(p: Path) -> Profile:
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    m = raw.get("match", {}) or {}
    cmds = []
    for name, c in (raw.get("commands", {}) or {}).items():
        c = c or {}
        cmds.append(AppCommand(str(name), [str(s) for s in (c.get("say") or [])], list(c.get("do") or []),
                               bool(c.get("confirm", False))))
    return Profile(
        name=str(raw.get("name") or p.stem), exe=[str(x) for x in (m.get("exe") or [])],
        title=[str(x) for x in (m.get("title") or [])], launch=raw.get("launch"),
        readings=[str(x) for x in (raw.get("readings") or [])],
        dictionary={str(k): str(v) for k, v in (raw.get("dictionary") or {}).items()},
        vocabulary=[str(x) for x in (raw.get("vocabulary") or [])], commands=cmds,
        menu_aliases={str(k).strip(): [str(a) for a in (v or [])] for k, v in (raw.get("menu_aliases") or {}).items()},
        control_aliases={str(k).strip(): [str(a) for a in (v or [])]
                         for k, v in (raw.get("control_aliases") or {}).items()},
        path=p)


class Profiles:
    def __init__(self, directory: Path = PROFILE_DIR):
        self.items: list[Profile] = []
        for p in sorted(directory.glob("*.yaml")) if directory.exists() else []:
            try:
                self.items.append(_parse(p))
            except Exception:
                log.exception("プロファイルを読めません: %s", p.name)
        if self.items:
            log.info("プロファイル: %s", "、".join(x.name for x in self.items))

    def active(self, window) -> Profile | None:
        """前面のアプリに合うプロファイル。ウィンドウタイトルで一致するもの（YouTube など、同じアプリでも
        画面によって変わるもの）を、実行ファイル名だけの一致より優先する。"""
        if window is None:
            return None
        t = (window.title or "").lower()
        hit = [x for x in self.items if x.matches(window.process, window.title)]
        if not hit:
            return None
        return next((x for x in hit if any(w.lower() in t for w in x.title if w)), hit[0])

    def launchable(self) -> list[dict]:
        """スタートメニューにないアプリを、アプリ一覧に足す形で返す。"""
        return [{"name": x.name, "id": x.launch} for x in self.items if x.launch]

    def readings(self) -> dict[str, str]:
        """読み → アプリ名（起動・切り替えで使う。どのアプリが前面でも有効）。"""
        return {r: x.name for x in self.items for r in x.readings}
