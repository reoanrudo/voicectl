"""未知の目的を、Jev に 1 手ずつ選ばせて進める（typesafe-mario と同じく Jev を行動選択のエージェントとして使う）。

毎手、今の画面（ボタン・メニュー・ウィンドウ・フォーカス中の入力欄）と「目的」「ここまでにしたこと」を構造化して渡し、
次の 1 手を選択肢から選ばせる。同じ呼び出しで「目的を達成したか」も判断させる。
Jev は自由な文章を作れないため、入力する文字は目的の文から切り出した候補の中から選ばせる。

安全のため：取り消しにくい操作（削除・リセット・閉じる等）は選択肢に出さない。Pause キーでいつでも中断できる。
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field

from . import apps, candidates, phonetic, tasks, textparse, uiaction, winutil
from .schema import KEYS

log = logging.getLogger(__name__)

# 1 手ごとに出す、画面に依存しない操作
_BASIC = {
    "key:enter": ("Press Enter (confirm / submit / run the search)", [0x0D]),
    "key:escape": ("Press Escape (close a popup or dialog)", [0x1B]),
    "key:tab": ("Press Tab (move to the next field)", [0x09]),
    "key:back": ("Go back to the previous page / screen (Alt+Left)", [0x12, 0x25]),
    "key:select_all": ("Select all text in the focused field (Ctrl+A)", [0x11, 0x41]),
    "scroll:down": ("Scroll down to see more", None),
    "scroll:up": ("Scroll up", None),
}
_STOP_WORDS = ("して", "開いて", "ひらいて", "起動", "表示", "見せて", "出して", "探して", "検索", "調べて", "押して",
               "クリック", "選んで", "移動", "行って", "たどり着いて", "再生", "入力", "打って", "ください", "お願い",
               # 「〜て」で区切ったときに残る動詞の語幹
               "開い", "ひらい", "見せ", "出し", "探し", "押し", "選ん", "行っ", "たどり着い", "打っ", "書い", "調べ")


@dataclass
class Step:
    option: str
    label: str
    result: str = ""


@dataclass
class AgentResult:
    success: bool
    steps: list[Step] = field(default_factory=list)
    reason: str = ""
    replay: list = field(default_factory=list)   # 再生できる形の手順（成功したら「これ覚えて」で保存する）


def query_candidates(goal: str) -> list[str]:
    """目的の文から、入力に使えそうな語句の候補を切り出す（Jev は文字を作れないため、候補から選ばせる）。"""
    out: list[str] = []
    t = textparse.extract_text(goal)
    if t:
        out.append(t)
    out += re.findall(r"[「『](.+?)[」』]", goal)
    c = textparse.compact(goal)
    for w in tasks._SITE_WORDS.values():  # サイト名は検索語から外す
        for s in w:
            c = c.replace(s, "|")
    c = re.sub(r"[「」『』]", "", c)
    for chunk in re.split(r"[|、。]|(?:を|で|に|へ|と|から|まで|について|の動画|の画面|のページ|の|して|て)", c):
        chunk = chunk.strip()
        for sw in _STOP_WORDS:
            chunk = re.sub(sw + "$", "", chunk)
        chunk = re.sub(r"(さん|ちゃん|くん)$", "", chunk)
        if 2 <= len(chunk) <= 30 and not re.fullmatch(r"[0-9０-９一二三四五六七八九十]+番目?", chunk):
            out.append(chunk)
    return list(dict.fromkeys(out))[:5]


class JevAgent:
    def __init__(self, controller, max_steps: int = 12, max_seconds: float = 90.0):
        self.ctl = controller
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.abort = False

    # ---- 1 手分の選択肢 ----
    def _options(self, goal: str, snap, lookup) -> dict[str, tuple[str, object]]:
        """option id → (Jev に見せる説明, 実行に使うデータ)。"""
        opts: dict[str, tuple[str, object]] = {}
        risky = [w.lower() for w in self.ctl.confirm_words]

        def safe(name: str) -> bool:
            return not any(w in name.lower() for w in risky)

        goal_c = textparse.compact(goal)

        def echo(el) -> bool:
            """voicectl 自身の表示（「目的:…」「手順 2: …」や発話そのもの）を OCR で読んだ文字か。"""
            if el.source != "ocr":
                return False
            n = textparse.compact(el.name)
            return bool(re.match(r"(目的|手順\d)", n)) or (len(n) >= 4 and difflib.SequenceMatcher(None, n, goal_c).ratio() >= 0.6)

        for cid, c in list((lookup.get("element") or {}).items())[:110]:
            if safe(c.data.name) and not echo(c.data):
                opts[f"click:{cid}"] = (f"Click {c.label}", c)
        for cid, c in list((lookup.get("menu") or {}).items())[:60]:
            if safe(" ".join(c.data.path)):
                opts[f"menu:{cid}"] = (f"Run menu: {c.label}", c)
        for cid, c in list((lookup.get("window") or {}).items())[:15]:
            opts[f"window:{cid}"] = (f"Switch to window: {c.label}", c)
        for cid, c in list((lookup.get("app") or {}).items())[:20]:
            opts[f"app:{cid}"] = (f"Start app: {c.data['name']}", c)
        queries = query_candidates(goal)
        for i, q in enumerate(queries):
            opts[f"type:{i}"] = (f"Type the text 「{q}」 into the focused field", q)
        for key, (name, home, search) in tasks.SITES.items():
            if any(w.lower() in goal.lower() for w in tasks._SITE_WORDS[key]):
                opts[f"site:{key}"] = (f"Open the website {name}", key)
                if search:
                    for i, q in enumerate(queries[:3]):
                        opts[f"search:{key}:{i}"] = (f"Search {name} for 「{q}」", (key, q))
        for k, (desc, _) in _BASIC.items():
            opts[k] = (desc, None)
        opts["done"] = ("The goal is already achieved on the current screen — stop", None)
        opts["stuck"] = ("None of these options can make progress toward the goal — give up", None)
        return dict(list(opts.items())[:250])

    # ---- Jev への問い合わせ ----
    def _ask(self, goal, snap, opts, history, facts, new_items) -> tuple[str, float, float]:
        eng = self.ctl.engine
        focus = self._focused()
        state = {
            "goal": goal,
            "active_window": snap.window.to_state() if snap.window else None,
            "focused_control": focus,
            "visible_items": [e.name for e in snap.elements if e.source == "uia"][:40],
            "new_items_since_last_step": new_items[:20],   # 直前の手の後に画面に現れた項目（画面の変化）
            "steps_done": [{"did": s.label, "result": s.result} for s in history[-6:]],
            "facts": facts,
        }
        g = "`goal`"
        body = {"model": eng.model, "state": state, "questions": {
            "next_step": {
                "type": "choice",
                "instructions": f"The user asked their Windows PC (by voice, in Japanese) to achieve {g}. "
                                "Given the current screen (`active_window`, `focused_control`) and `steps_done`, which "
                                "single next step moves closest to the goal? Do not repeat a step that did not change "
                                "anything. Type text only when a text field is focused (or right after clicking one). "
                                "`facts.best_element_matches` / `facts.best_menu_matches` are code-computed matches to "
                                "the goal words.",
                "criteria": {k: v[0] for k, v in opts.items()}},
            "goal_achieved": {
                "type": "noul",
                "instructions": f"Judging from `active_window`, `new_items_since_last_step`, `visible_items` and "
                                f"`steps_done`, {g} has already been achieved (e.g. the requested screen, page or "
                                "setting is now shown).",
                "criteria": {"true": "Achieved", "false": "Not yet"}},
        }}
        data = eng._post(body)
        a = data.get("answers", {})
        ns = a.get("next_step", {})
        conf = ns.get("confidence") or max((ns.get("probabilities") or {0: 0}).values())
        return ns.get("choice", "stuck"), float(conf), float(a.get("goal_achieved", {}).get("noul") or 0.0)

    def _focused(self) -> dict | None:
        def get():
            import uiautomation as auto
            f = auto.GetFocusedControl()
            return {"name": (f.Name or "")[:60], "type": f.ControlTypeName} if f else None
        try:
            r = uiaction.run_isolated(get, timeout=1.0)
            return r if isinstance(r, dict) else None
        except Exception:
            return None

    # ---- 実行 ----
    def _do(self, option: str, data, snap) -> None:
        kind = option.split(":", 1)[0]
        if kind == "click":
            el = data.data
            if el.source == "uia" and snap.window:
                uiaction.run_isolated(uiaction.act_on_element, snap.window.hwnd, el, "left")
            else:
                winutil.set_cursor(*el.center)
                time.sleep(0.03)
                winutil.click("left")
        elif kind == "menu":
            self.ctl._run_menu(winutil.foreground_window(), data.data)
        elif kind == "window":
            winutil.focus_window(data.data.hwnd)
        elif kind == "app":
            apps.launch(data.data)
        elif kind == "type":
            winutil.type_unicode(data)
        elif kind == "site":
            tasks.open_url(tasks.SITES[data][1])
        elif kind == "search":
            site, q = data
            import urllib.parse
            tasks.open_url(tasks.SITES[site][2].format(q=urllib.parse.quote(q)))
        elif option == "scroll:down":
            winutil.scroll(-5)
        elif option == "scroll:up":
            winutil.scroll(5)
        elif kind == "key":
            winutil.press_combo(_BASIC[option][1])

    @staticmethod
    def _replay_step(option: str, data, snap):
        """実行した 1 手を、あとで画面の状態に頼らず再生できる形（demo.replay_step が読む形）にする。"""
        kind = option.split(":", 1)[0]
        win = snap.window
        if kind == "click" and win:
            el = data.data
            l, t, r, b = win.rect
            cx, cy = el.center
            return {"click": {"proc": win.process.lower(), "title": win.title[:80],
                              "name": el.name if el.source == "uia" else "", "auto_id": el.auto_id, "kind": el.kind,
                              "rel": [round((cx - l) / max(r - l, 1), 4), round((cy - t) / max(b - t, 1), 4)],
                              "button": "left"}}
        if kind == "menu" and win:
            return {"menu": {"proc": win.process.lower(), "path": list(data.data.path),
                             "shortcut": data.data.shortcut or ""}}
        if kind == "window":
            return {"focus": {"proc": data.data.process.lower(), "title": data.data.title[:80]}}
        if kind == "app":
            return f"{data.data['name']}を開いて"
        if kind == "type":
            return {"type": data}
        if kind == "site":
            return {"url": tasks.SITES[data][1]}
        if kind == "search":
            import urllib.parse
            site, q = data
            return {"url": tasks.SITES[site][2].format(q=urllib.parse.quote(q))}
        if option in ("scroll:down", "scroll:up"):
            return {"scroll": -5 if option == "scroll:down" else 5}
        if kind == "key":
            return {"keys": list(_BASIC[option][1])}
        return None

    @staticmethod
    def _settle(before_title: str, timeout: float = 3.0) -> str:
        """画面が落ち着くのを待つ（最低 0.7 秒。タイトルが変わったら、変化が止まるまで少し待つ）。"""
        time.sleep(0.7)
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            fg = winutil.foreground_window()
            title = fg.title if fg else ""
            if title == last:
                break
            last = title
            time.sleep(0.35)
        return last or ""

    # ---- 目的を進める ----
    def run(self, goal: str, status) -> AgentResult:
        self.abort = False
        history: list[Step] = []
        t0 = time.monotonic()
        repeats = 0
        weak = 0                      # 確信度の低い手が続いた回数
        replay: list = []
        chosen: dict[str, int] = {}   # 手ごとの選んだ回数（A→B→A→B の往復を見つける）
        prev_names: set[str] | None = None
        new_items: list[str] = []
        for n in range(1, self.max_steps + 1):
            if self.abort:
                return AgentResult(False, history, "中断しました", replay)
            if time.monotonic() - t0 > self.max_seconds:
                return AgentResult(False, history, "時間切れ", replay)
            snap, lookup, facts = self.ctl._agent_view(goal)
            names = [e.name for e in snap.elements]
            if prev_names is not None:
                new_items = [x for x in dict.fromkeys(names) if x not in prev_names]
                if history:  # 直前の手の結果に、画面の変化を書き足す
                    history[-1].result += f"; {len(new_items)} new items appeared" + (
                        f" (e.g. {', '.join(new_items[:5])})" if new_items else "")
            prev_names = set(names)
            opts = self._options(goal, snap, lookup)
            choice, conf, achieved = self._ask(goal, snap, opts, history, facts, new_items)
            label = opts.get(choice, (choice, None))[0]
            log.info("エージェント %d 手目: %s（確信度 %.2f・達成 %.2f）", n, label, conf, achieved)
            if choice == "done" or (achieved >= 0.85 and n > 1):
                return AgentResult(True, history, "目的を達成しました", replay)
            if choice == "stuck" or choice not in opts:
                return AgentResult(False, history, "これ以上進められません", replay)
            weak = weak + 1 if conf < 0.3 else 0
            if weak >= 3:
                return AgentResult(False, history, "手がかりが見つからないため止めました", replay)
            chosen[choice] = chosen.get(choice, 0) + 1
            if chosen[choice] >= 3 and not choice.startswith(("scroll", "key")):
                return AgentResult(False, history, "同じ操作を繰り返したため止めました", replay)
            if history and history[-1].option == choice:
                repeats += 1
                if repeats >= 2:
                    return AgentResult(False, history, "同じ操作を繰り返したため止めました", replay)
            else:
                repeats = 0
            status(n, label)
            before = snap.window.title if snap.window else ""
            try:
                self._do(choice, opts[choice][1], snap)
            except Exception as e:
                history.append(Step(choice, label, f"failed: {e}"))
                log.info("エージェントの手が失敗: %s", e)
                continue
            after = self._settle(before)
            history.append(Step(choice, label, "screen changed" if after != before else "no visible change in title"))
            rs = self._replay_step(choice, opts[choice][1], snap)
            if rs is not None:
                replay.append(rs)
        return AgentResult(False, history, f"{self.max_steps} 手で終わりませんでした", replay)
