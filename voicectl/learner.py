"""学習：解釈できた言い方を覚えて、次からは Jev も LLM も通さずに実行する。

「一度教えれば、次からは同じ言い方で確実に・速く動く」ための層。Jev や LLM（ローカル bonsai）が
解釈した結果と、利用者が「はい」と確認した結果を state/learned.json に貯め、次の同種の発話では
  1. 表記が同じ（空白・句読点を除いて一致）        → そのまま実行
  2. 読みの骨格がほぼ同じ（漢字の同音異義・おお/おー・促音の揺れを吸収）→ そのまま実行
として再利用する。何度も成功した言い方（trust_at 回）は「確認なしで実行」に昇格し、
逆に実行に失敗したり取り消されたりした言い方は少しずつ信頼を下げ、最後は忘れる。

覚えた内容は JSON なので、あとから人が見て keyword.py や profiles/*.yaml に移せる。
移すべき候補は suggestions() が返す（state/learned_suggestions.md にも書き出す）。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import phonetic, textparse

log = logging.getLogger(__name__)
_VERSION = 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _key(text: str) -> str:
    """表記の一致を見るためのキー（空白・句読点を除いて小文字にする）。"""
    return textparse.compact(text).lower()


def _reading_key(text: str) -> str:
    """読みの骨格（漢字の同音異義や長音の書き方の違いを吸収する）。"""
    return phonetic.ja_skeleton(text)


@dataclass
class Entry:
    say: str                       # 覚えたときの発話（表示用）
    action: str = "unknown"        # 1 手の操作
    params: dict = field(default_factory=dict)
    text: str | None = None        # type_text の入力文字列
    plan: list[dict] = field(default_factory=list)  # 複数手順（あれば plan を優先して実行）
    app: str = ""                  # 覚えたときのアプリ（exe）。空ならどこでも使う
    hits: int = 0                  # 成功した回数
    misses: int = 0                # 失敗・取り消しの回数
    source: str = ""               # llm / confirm / explicit
    created: str = ""
    used: str = ""
    key: str = ""
    rkey: str = ""

    @property
    def trusted(self) -> bool:
        """何度も成功していて、失敗が少ない＝確認なしで実行してよい。"""
        return self.hits >= 3 and self.misses * 2 <= self.hits

    def to_json(self) -> dict:
        return {"say": self.say, "action": self.action, "params": self.params, "text": self.text,
                "plan": self.plan, "app": self.app, "hits": self.hits, "misses": self.misses,
                "source": self.source, "created": self.created, "used": self.used}

    @staticmethod
    def from_json(key: str, d: dict) -> "Entry":
        e = Entry(say=str(d.get("say") or ""), action=str(d.get("action") or "unknown"),
                  params=dict(d.get("params") or {}), text=d.get("text"), plan=list(d.get("plan") or []),
                  app=str(d.get("app") or ""), hits=int(d.get("hits") or 0), misses=int(d.get("misses") or 0),
                  source=str(d.get("source") or ""), created=str(d.get("created") or ""),
                  used=str(d.get("used") or ""))
        e.key = key
        e.rkey = _reading_key(e.say)
        return e

    def describe(self) -> str:
        return _short(self)


class Learner:
    """覚えた言い方の保管庫。ファイル 1 つ（JSON）に全部入れる。"""

    def __init__(self, path: Path | str, enabled: bool = True, trust_at: int = 3, fuzzy: float = 0.86,
                 max_entries: int = 500):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.trust_at = max(1, int(trust_at))
        self.fuzzy = float(fuzzy)
        self.max_entries = int(max_entries)
        self.entries: dict[str, Entry] = {}
        self._lock = threading.RLock()
        self.learned_now = 0   # この起動で覚えた件数（表示用）
        self.hits_now = 0      # この起動で学習を使って実行できた件数
        self.load()

    # ---- 保存 ----
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("学習の読み込みに失敗（作り直します）: %s", e)
            return
        for key, d in (data.get("entries") or {}).items():
            if isinstance(d, dict):
                try:
                    self.entries[key] = Entry.from_json(key, d)
                except Exception:
                    continue
        log.info("学習を読み込みました: %d 件（うち確認なしで実行できるもの %d 件）",
                 len(self.entries), len(self.trusted()))

    def save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": _VERSION, "saved": _now(),
                "entries": {k: e.to_json() for k, e in self.entries.items()}}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)   # 書き込み途中で壊れないように置き換える
        except Exception as e:
            log.warning("学習の保存に失敗: %s", e)

    # ---- 覚える ----
    def remember(self, text: str, action: str, params: dict | None = None, text_value: str | None = None,
                 plan: list[dict] | None = None, app: str = "", source: str = "") -> Entry | None:
        """解釈できた言い方を覚える。同じ言い方を覚え直したら成功回数を増やす（新しい解釈を採用）。"""
        if not self.enabled:
            return None
        key = _key(text)
        if not key:
            return None
        plan = list(plan or [])
        if not plan and (action in ("unknown", "stop", "repeat")):
            return None   # 覚える価値のない結果は捨てる
        with self._lock:
            e = self.entries.get(key)
            if e is None:
                e = Entry(say=text.strip(), action=action, params=dict(params or {}), text=text_value,
                          plan=plan, app=app, created=_now(), key=key, rkey=_reading_key(text))
                self.entries[key] = e
                self.learned_now += 1
                log.info("覚えました（%s）: %s → %s", source or "auto", text, _short(e))
            else:
                e.hits += 1
                e.action, e.params, e.text, e.plan = action, dict(params or {}), text_value, plan
                if app:
                    e.app = app
                log.info("同じ言い方を覚え直しました（%d 回目）: %s", e.hits, text)
            e.used = _now()
            self._trim()
            self.save()
            return e

    def reward(self, key: str) -> None:
        """学習した言い方で実行できた。"""
        with self._lock:
            e = self.entries.get(key)
            if e is None:
                return
            e.hits += 1
            e.used = _now()
            self.hits_now += 1
            if e.hits == self.trust_at:
                log.info("「%s」は %d 回成功したので、次からは確認なしで実行します", e.say, e.hits)
            self.save()

    def penalize(self, key: str, why: str = "") -> None:
        """失敗・取り消し。信頼を下げ、ひどければ忘れる。"""
        with self._lock:
            e = self.entries.get(key)
            if e is None:
                return
            e.misses += 1
            if e.misses >= 3 and e.misses > e.hits:
                del self.entries[key]
                log.info("「%s」は失敗が続くので忘れます（%s）", e.say, why or "失敗")
            else:
                log.info("「%s」の信頼を下げました（成功 %d / 失敗 %d、%s）", e.say, e.hits, e.misses, why or "失敗")
            self.save()

    def forget(self, text: str) -> str | None:
        with self._lock:
            key = _key(text)
            e = self.entries.pop(key, None)
            if e is None:  # 表記が違っても読みで探す（アプリが違っても消せるようにする）
                hit = self._fuzzy(text, app="", strict_app=False)
                if hit is not None:
                    e = self.entries.pop(hit[0], None)
            if e is None:
                return None
            self.save()
            return e.say

    def _trim(self) -> None:
        """上限を超えたら、成功回数が少なく古いものから捨てる。"""
        if len(self.entries) <= self.max_entries:
            return
        order = sorted(self.entries.items(), key=lambda kv: (kv[1].hits, kv[1].used))
        for key, _ in order[: len(self.entries) - self.max_entries]:
            del self.entries[key]

    # ---- 思い出す ----
    def lookup(self, text: str, app: str = "") -> Entry | None:
        """覚えている言い方か探す。表記の一致を優先し、なければ読みの骨格で近いものを探す。"""
        if not self.enabled or not self.entries:
            return None
        with self._lock:
            key = _key(text)
            e = self.entries.get(key)
            if e is not None and (not e.app or e.app == app):
                e.used = _now()
                self.hits_now += 1
                log.info("学習した言い方で実行: %s → %s", text, _short(e))
                return e
            hit = self._fuzzy(text, app)
            if hit is not None:
                e = self.entries[hit[0]]
                e.used = _now()
                self.hits_now += 1
                log.info("学習した言い方（読み %.2f）で実行: %s ≒ %s → %s", hit[1], text, e.say, _short(e))
                return e
        return None

    def _fuzzy(self, text: str, app: str, strict_app: bool = True) -> tuple[str, float] | None:
        rk = _reading_key(text)
        if len(rk) < 4:
            return None
        best: tuple[str, float] | None = None
        for key, e in self.entries.items():
            if e.app and e.app != app and (strict_app or not app):
                continue   # 別のアプリ用に覚えたものは使わない（今のアプリが不明なら尚更）
            if not e.rkey:
                continue
            if abs(len(e.rkey) - len(rk)) > max(4, len(rk) // 2):
                continue   # 長さが違いすぎるものは比べない（速さのため）
            ratio = difflib.SequenceMatcher(None, rk, e.rkey).ratio()
            if e.app and e.app == app:
                ratio += 0.04   # 同じアプリで覚えたものは少し優先する
            if best is None or ratio > best[1]:
                best = (key, ratio)
        if best is None or best[1] < self.fuzzy:
            return None
        return best

    def confidence(self, e: Entry) -> float:
        """学習した言い方を実行するときの確信度（trusted なら確認を省く）。"""
        return 0.95 if e.hits >= self.trust_at and e.misses * 2 <= e.hits else 0.72

    # ---- 様子 ----
    def trusted(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.hits >= self.trust_at and e.misses * 2 <= e.hits]

    def stats(self) -> dict:
        return {"count": len(self.entries), "trusted": len(self.trusted()),
                "learned_now": self.learned_now, "hits_now": self.hits_now}

    def top(self, n: int = 5) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: (-e.hits, -e.misses))[:n]

    def suggestions(self, min_hits: int = 3) -> list[Entry]:
        """keyword.py / profiles に移す（固定の規則にする）候補。"""
        return [e for e in self.entries.values()
                if e.hits >= min_hits and not e.plan and e.action not in ("unknown",)]

    def write_suggestions(self) -> Path:
        """候補を Markdown に書き出す（あとから人が見て固定の規則へ移せるように）。"""
        path = self.path.parent / "learned_suggestions.md"
        cands = self.suggestions()
        lines = ["# 学習した言い方（固定の規則へ移す候補）", "",
                 f"更新: {_now()} / 覚えている数: {len(self.entries)} / 確認なしで実行できる数: {len(self.trusted())}",
                 "", "keyword.py や profiles/*.yaml に移すと、Jev も LLM も通さずに実行できます。", ""]
        for e in cands:
            lines.append(f"- 「{e.say}」（{e.app or 'どこでも'}）→ {_short(e)}  成功 {e.hits} 回")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def _short(e: Entry) -> str:
    if e.plan:
        return " → ".join(str(s.get("action")) for s in e.plan)
    parts = ", ".join(f"{k}={v}" for k, v in (e.params or {}).items())
    return f"{e.action}({parts})" if parts else e.action


def format_stats(learner: Learner) -> list[str]:
    """状態表示に出す短い説明。"""
    s = learner.stats()
    if not s["count"]:
        return ["まだ覚えた言い方はありません（確認した操作や LLM の解釈を自動で覚えます）"]
    lines = [f"覚えている言い方 {s['count']} 件 / 確認なしで実行できるもの {s['trusted']} 件"]
    for e in learner.top(3):
        lines.append(f"・{e.say} → {_short(e)}（成功 {e.hits}）")
    return lines
