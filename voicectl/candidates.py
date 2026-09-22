"""実行時に決まる選択肢（アプリ・ウィンドウ・画面要素）を、発話との類似度で絞り込んで作る。"""
from __future__ import annotations

from . import textparse
from .context import Snapshot
from .schema import Candidate

Lookup = dict[str, dict[str, Candidate]]


def _position(center: tuple[int, int], rect: tuple[int, int, int, int]) -> str:
    l, t, r, b = rect
    w, h = max(1, r - l), max(1, b - t)
    fx, fy = (center[0] - l) / w, (center[1] - t) / h
    col = "左" if fx < 1 / 3 else ("右" if fx > 2 / 3 else "中央")
    row = "上" if fy < 1 / 3 else ("下" if fy > 2 / 3 else "")
    return (row + col) if col != "中央" else (row + "中央" if row else "中央")


def _duplicate_tags(elements) -> dict[int, str]:
    """同じ名前の要素に「上段・下段」「左・右」などの区別を付ける（PioViewer の Range ボタン 2 組など）。"""
    groups: dict[str, list[int]] = {}
    for i, e in enumerate(elements):
        if e.source == "uia":
            groups.setdefault(e.name, []).append(i)
    tags = {}
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        ys = sorted({elements[i].center[1] // 12 for i in idxs})
        if len(ys) > 1:  # 縦に並んでいる → 段
            names = ["上段", "下段"] if len(ys) == 2 else [f"{n + 1}段目" for n in range(len(ys))]
            for i in idxs:
                tags[i] = f"・{names[ys.index(elements[i].center[1] // 12)]}"
        else:            # 横に並んでいる → 左から
            xs = sorted(idxs, key=lambda i: elements[i].center[0])
            names = ["左", "右"] if len(xs) == 2 else [f"左から{n + 1}番目" for n in range(len(xs))]
            for n, i in enumerate(xs):
                tags[i] = f"・{names[n]}"
    return tags


def build(text: str, snap: Snapshot, apps: list[Candidate], limits: dict[str, int],
          menu: list | None = None, commands: list | None = None) -> Lookup:
    """menu: 前面アプリのアプリマップの項目（appmap.MenuEntry）、commands: プロファイルの独自コマンド。"""
    lookup: Lookup = {}
    if menu:
        ms = [Candidate(f"m{i}", e.label, e) for i, e in enumerate(menu)]
        from . import phonetic
        scored = sorted(((textparse.similarity(text, " ".join(c.data.path) + "・" + "・".join(c.data.aliases))
                          + 1.5 * phonetic.menu_match(text, c.data.path)
                          + phonetic.ja_menu_match(text, c.data.path), -i, c.id) for i, c in enumerate(ms)),
                        reverse=True)
        ranked = [cid for _, _, cid in scored[:limits.get("menu", 200)]]
        mmap = {c.id: c for c in ms}
        lookup["menu"] = {cid: mmap[cid] for cid in ranked}
    if commands:
        lookup["command"] = {f"c{i}": Candidate(f"c{i}", c.label, c) for i, c in enumerate(commands[:250])}

    ranked = textparse.rank(text, [(c.id, c.label) for c in apps], limits.get("app", 60))
    by_id = {c.id: c for c in apps}
    lookup["app"] = {cid: by_id[cid] for cid in ranked}

    wins = [Candidate(f"w{i}", f"{w.title} — {w.process}", w) for i, w in enumerate(snap.windows)]
    ranked = textparse.rank(text, [(c.id, c.label) for c in wins], limits.get("window", 40))
    wmap = {c.id: c for c in wins}
    lookup["window"] = {cid: wmap[cid] for cid in ranked}

    wrect = snap.window.rect if snap.window else (0, 0, 1, 1)
    order = _duplicate_tags(snap.elements)
    els = [Candidate(f"e{i}", f"{e.label}{order.get(i, '')} [{_position(e.center, wrect)}]", e)
           for i, e in enumerate(snap.elements)]
    from . import phonetic
    scored = sorted(((textparse.similarity(text, c.data.name + "・" + "・".join(c.data.aliases or []))
                      + 1.5 * phonetic.match_ratio(text, c.data.name)
                      + phonetic.ja_ratio(text, c.data.name), -i, c.id) for i, c in enumerate(els)),
                    reverse=True)
    ranked = [cid for _, _, cid in scored[:limits.get("element", 120)]]
    emap = {c.id: c for c in els}
    lookup["element"] = {cid: emap[cid] for cid in ranked}
    return lookup
