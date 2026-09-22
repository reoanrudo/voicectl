"""文章アシスタント：DeepSeek（opencode 経由）に文章の作成・要約・翻訳・書き直し・質問への回答を頼む。

Jev は選択肢を選ぶだけで文章を作れないため、文章そのものが要る依頼だけをここに回す。
プロンプトは .opencode/agents/voicectl-writer.md。結果は本文だけ（前置きなし）で返ってくる。
呼び出しは数秒かかるので、controller 側で別スレッドから呼ぶ。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import subprocess
import time

from .engines.llm import ROOT, LLMFallback

log = logging.getLogger(__name__)

TASKS = ("write", "reply", "summarize", "translate_en", "translate_ja", "polite", "casual", "shorten", "bullets",
         "proofread", "explain", "refine", "answer", "screen", "recap")
_WEEK = "月火水木金土日"


class Assistant:
    def __init__(self, model: str = "deepseek/deepseek-v4-flash", agent: str = "voicectl-writer",
                 timeout_sec: float = 40.0, max_input: int = 6000):
        self.model = model
        self.agent = agent
        self.timeout_sec = float(timeout_sec)
        self.max_input = int(max_input)

    def ask(self, task: str, request: str, text: str = "", window: str = "") -> str:
        """結果の本文を返す。失敗したら RuntimeError。"""
        if task not in TASKS:
            raise ValueError(f"知らない依頼です: {task}")
        exe = LLMFallback._resolve_command("opencode")
        if exe is None:
            raise RuntimeError("opencode が見つかりません")
        now = _dt.datetime.now()
        payload = {"task": task, "request": request, "text": (text or "")[: self.max_input], "window": window,
                   "today": f"{now:%Y/%m/%d} {_WEEK[now.weekday()]}曜日 {now:%H:%M}"}
        cmd = exe + ["run", "--agent", self.agent, "--model", self.model, "--pure", "--format", "json",
                     "--title", "voicectl-writer", "--dir", str(ROOT), json.dumps(payload, ensure_ascii=False)]
        t0 = time.perf_counter()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=self.timeout_sec, encoding="utf-8",
                               errors="replace", stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{self.timeout_sec:.0f} 秒で返事がありませんでした")
        if p.returncode != 0:
            raise RuntimeError(f"文章アシスタントが失敗しました（exit {p.returncode}）")
        out = (LLMFallback._extract_text(p.stdout) or "").strip()
        out = out.strip("`").strip()
        if not out:
            raise RuntimeError("返事が空でした")
        log.info("文章アシスタント（%s）: %s → %d 文字 [%.1fs]", task, request[:40], len(out), time.perf_counter() - t0)
        return out
