"""TypeSafe Jev（System One モデル）による判定。

公式 API リファレンス（https://docs.typesafe.ai/api）の形式に合わせている：
  POST /v1/systemone  {model, state, questions:{id:{type, instructions, criteria}}}
  → {answers:{id:{type, choice|score|noul, probabilities, confidence, legend}}}
1 回の呼び出しに含めた質問は並列に評価されるため、必要になりうる質問をすべて同時に送る。
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from .. import schema
from ..candidates import Lookup
from ..context import Snapshot
from ..schema import Decision
from .base import DecisionEngine, overall_confidence

log = logging.getLogger(__name__)

_NONE = "none"


class JevError(RuntimeError):
    pass


class JevEngine(DecisionEngine):
    name = "jev"

    def __init__(self, endpoint: str, model: str, api_key_env: str = "TYPESAFE_API_KEY", timeout_sec: float = 3.0):
        key = os.environ.get(api_key_env) or _user_env(api_key_env)
        if not key:
            raise JevError(f"環境変数 {api_key_env} に API キーが設定されていません")
        self.endpoint = endpoint
        self.model = model
        # 接続を使い回して TLS ハンドシェイクの遅延を毎回払わないようにする
        self._client = httpx.Client(timeout=timeout_sec, http2=False,
                                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        self._last_call = time.monotonic()
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._client.close()

    def start_keepalive(self, idle_sec: float = 50.0) -> None:
        """待機中に接続が切れると次の呼び出しが数百 ms 遅くなる（実測：冷えた接続 749 ms / 温まった接続 約 260 ms）ため、
        一定時間呼び出しがなければ最小の問い合わせで接続を保つ。"""
        import threading

        def loop():
            while not self._closed:
                time.sleep(5)
                if time.monotonic() - self._last_call > idle_sec:
                    self.warmup()

        threading.Thread(target=loop, name="jev-keepalive", daemon=True).start()

    def warmup(self) -> None:
        try:
            self._post({"model": self.model, "state": "warmup",
                        "questions": {"q": {"type": "noul", "instructions": "Is this a greeting?"}}})
        except Exception as e:
            log.warning("Jev のウォームアップに失敗: %s", e)

    # ---- リクエスト組み立て ----
    @staticmethod
    def build_state(text: str, snap: Snapshot, previous: Decision | None, lookup: Lookup,
                    facts: dict | None = None, recent: list | None = None, session: dict | None = None) -> dict:
        """意味ごとにまとめた状態（typesafe-mario の方式）。照合などの計算はコードで済ませ、facts として渡す。"""
        state = {
            "utterance": text,
            "active_window": snap.window.to_state() if snap.window else None,
            "cursor": {"x": snap.cursor[0], "y": snap.cursor[1]},
            "previous_command": previous.describe(lookup) if previous else None,
        }
        if recent:
            state["recent_commands"] = recent   # 直近の「言ったこと・したこと・結果」
        if session:
            state["session"] = session          # 直前に扱ったアプリ・サイト・ページ（「さっきの」「その動画」の解決用）
        if facts:
            state["facts"] = facts
        return state

    @staticmethod
    def build_questions(lookup: Lookup) -> dict:
        u = "`utterance`"  # instructions から state のフィールドを参照する記法
        # 選択肢がないアクションは出さない（独自コマンドが 0 件なのに app_command を選ぶ、などを防ぐ）
        actions = dict(schema.ACTIONS)
        if not lookup.get("command"):
            actions.pop("app_command", None)
        if not lookup.get("menu"):
            actions.pop("menu_command", None)
        q: dict[str, dict] = {
            "action": {"type": "choice",
                       "instructions": f"The user said {u} (Japanese, casual speech) to control their Windows PC by voice. "
                                       "Which operation do they want? Use `previous_command` to resolve words like "
                                       "'もうちょい' (a bit more) or 'さっきの' (the previous one). `facts` holds matches "
                                       "computed by code: `facts.best_menu_matches` / `facts.best_element_matches` are "
                                       "features of the CURRENT app that match the words (match >= 1.0 is strong), "
                                       "`facts.katakana_english` maps katakana words to the English word they sound like. "
                                       "Prefer the current app's menu item or button when there is a strong match, "
                                       "over launching another app or typing text. `facts.windows` describes the Windows "
                                       "situation: `window_kind` (explorer / browser / editor / dialog ...), "
                                       "`dialog_open` + `dialog_buttons` (then short replies like 'はい' or '閉じて' mean "
                                       "that dialog's buttons), `focused` (what has keyboard focus), and in Explorer "
                                       "`explorer_folder` / `explorer_selected`. Choose what makes sense in that situation.",
                       "criteria": actions},
            "direction": {"type": "choice",
                          "instructions": f"For a mouse move or scroll in {u}, which direction? If the user says 'more' "
                                          "without a direction, use the direction of `previous_command`.",
                          "criteria": dict(schema.DIRECTIONS)},
            "amount": {"type": "score", "instructions": f"How far should the mouse move or page scroll for {u}?",
                       "criteria": list(schema.AMOUNTS)},
            "button": {"type": "choice", "instructions": f"Which kind of click does {u} ask for?",
                       "criteria": dict(schema.BUTTONS)},
            "key": {"type": "choice", "instructions": f"Which key or shortcut does {u} ask for?",
                    "criteria": {k: v[0] for k, v in schema.KEYS.items()}},
            "window_action": {"type": "choice", "instructions": f"Which window operation does {u} ask for?",
                              "criteria": dict(schema.WINDOW_ACTIONS)},
            "snap": {"type": "choice",
                     "instructions": f"For a snap_window request in {u}, where should the window go?",
                     "criteria": {k: v[0] for k, v in schema.SNAPS.items()}},
            "hint_mode": {"type": "choice", "instructions": f"Which kind of numbered overlay does {u} ask for?",
                          "criteria": dict(schema.HINT_MODES)},
            "is_pc_command": {"type": "noul",
                              "instructions": f"{u} is a request for the computer to do something "
                                              "(not chatter, thinking aloud, a question to another person, or noise).",
                              "criteria": {"true": "A command to operate the PC", "false": "Not a command"}},
            "is_continuation": {"type": "noul",
                                "instructions": f"{u} refers to or continues `previous_command` "
                                                "(e.g. 'もうちょい', 'もっと', 'さっきの', 'また').",
                                "criteria": {"true": "Continues or refers to the previous command",
                                             "false": "A new, self-contained command"}},
        }
        pick = {
            "app": f"Which application does {u} want to start? Names may be said loosely, in katakana or by nickname.",
            "window": f"Which open window does {u} want to switch to?",
            "menu": f"Which command from the current app's menus does {u} ask for? The options are menu paths "
                    "like 'File > Save as'. `facts.best_menu_matches` lists code-computed matches by option id.",
            "command": f"Which of the current app's special commands does {u} ask for?",
            "element": f"Which on-screen item (button, menu, link or text) does {u} refer to? "
                       "`facts.best_element_matches` lists code-computed matches by option id. "
                       "Position words like 右上 (top right) refer to the [position] tag.",
        }
        for kind, instr in pick.items():
            cands = lookup.get(kind) or {}
            if cands:
                crit = {cid: c.label for cid, c in cands.items()}
                crit[_NONE] = "None of the listed items matches"
                q[kind] = {"type": "choice", "instructions": instr, "criteria": crit}
        return q

    # ---- 呼び出しと解析 ----
    def _post(self, body: dict) -> dict:
        self._last_call = time.monotonic()
        for attempt in range(2):
            r = self._client.post(self.endpoint, json=body)
            if r.status_code in (429, 529) and attempt == 0:
                time.sleep(0.25)
                continue
            if r.status_code == 401:
                raise JevError("API キーが無効です (401)")
            if r.status_code == 422:
                raise JevError(f"リクエスト形式エラー (422): {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        raise JevError("レート制限または過負荷で失敗しました")

    def decide(self, text: str, snap: Snapshot, lookup: Lookup, previous: Decision | None,
               facts: dict | None = None, recent: list | None = None, session: dict | None = None) -> Decision:
        t0 = time.perf_counter()
        body = {"model": self.model, "state": self.build_state(text, snap, previous, lookup, facts, recent, session),
                "questions": self.build_questions(lookup)}
        data = self._post(body)
        d = self.parse(data, body["questions"])
        d.latency_ms = (time.perf_counter() - t0) * 1000
        log.debug("Jev %s usage=%s", data.get("model"), data.get("usage"))
        return d

    @staticmethod
    def parse(data: dict, questions: dict) -> Decision:
        answers = data.get("answers", {})
        params: dict[str, str] = {}
        detail: dict[str, float] = {}
        alts: dict[str, list] = {}
        for qid in ("element", "menu", "app", "window", "command"):   # 対象の候補の上位（迷ったときに選ばせる）
            probs = (answers.get(qid) or {}).get("probabilities") or {}
            top = sorted(((k, float(v)) for k, v in probs.items() if k != _NONE), key=lambda kv: -kv[1])[:3]
            if top:
                alts[qid] = [(k, round(v, 3)) for k, v in top]
        for qid, a in answers.items():
            if qid not in questions:
                continue
            kind = a.get("type") or questions[qid]["type"]
            conf = _confidence(a)
            if kind == "choice":
                choice = a.get("choice")
                if choice is None:
                    continue
                if choice == _NONE:
                    conf = 0.0
                detail[qid] = conf
                if qid == "action":
                    continue
                params[qid] = choice
            elif kind == "score":
                idx = _score_index(a, questions[qid]["criteria"])
                params[qid] = schema.AMOUNT_KEYS[idx]
                detail[qid] = conf
        action = answers.get("action", {}).get("choice", "unknown")
        if action not in schema.ACTIONS:
            action = "unknown"
        cont = answers.get("is_continuation", {}).get("noul")
        pc = answers.get("is_pc_command", {}).get("noul")
        top = sorted((answers.get("action", {}).get("probabilities") or {}).items(), key=lambda kv: -kv[1])[:3]
        needed = set(schema.ACTION_PARAMS.get(action, []))
        return Decision(action=action, params={k: v for k, v in params.items() if k in needed},
                        confidence=overall_confidence(action, detail), detail=detail, engine="jev",
                        is_continuation=float(cont) if cont is not None else 0.0,
                        is_pc_command=float(pc) if pc is not None else 1.0,
                        top_actions={k: round(float(v), 3) for k, v in top}, alternatives=alts)


def _user_env(name: str) -> str | None:
    """setx は起動済みのプロセスには反映されないため、ユーザー環境変数をレジストリから直接読む。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _confidence(a: dict) -> float:
    if a.get("confidence") is not None:
        return float(a["confidence"])
    probs = a.get("probabilities") or {}
    return float(max(probs.values())) if probs else 0.0


def _score_index(a: dict, criteria: list[str]) -> int:
    """score の答えを段階の番号（0 始まり）にする。probabilities があればその最大値を優先する。"""
    n = len(criteria)
    probs = a.get("probabilities") or {}
    if probs:
        key = max(probs, key=lambda k: probs[k])
        if key in criteria:
            return criteria.index(key)
        if str(key).isdigit():
            keys = sorted(int(k) for k in probs if str(k).isdigit())
            base = keys[0] if keys else 0
            return max(0, min(n - 1, int(key) - base))
    score = a.get("score")
    if score is None:
        return n // 2
    legend = a.get("legend") or {}
    base = min((int(k) for k in legend if str(k).isdigit()), default=0)
    return max(0, min(n - 1, round(float(score)) - base))
