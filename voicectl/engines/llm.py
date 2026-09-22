"""ローカル LLM（Ternary Bonsai 2 27B / llama.cpp）によるフォールバック判定。

Jev で決まらなかった発話（確信度不足・unknown・入力文字列が取れない）だけを問い合わせる。
既定はローカルの llama-server（OpenAI 互換 API）。ネットに出ないので速く（実測 1〜2 秒）、
料金もかからず、発話が外部に送られない。backend: opencode を選べば従来どおり opencode 経由
（DeepSeek など）でも呼べる。

プロンプトは .opencode/agents/voicectl-fallback.md（本文＝システムプロンプト）を使う。
LLM は JSON を返すだけで実行はしない。候補 ID や action はここで検証し、捏造・不正な値は捨てる。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .. import schema
from ..candidates import Lookup
from ..context import Snapshot
from ..schema import Decision
from ..config import ROOT

log = logging.getLogger(__name__)

# LLM に渡す候補の件数上限（応答時間と通信量のため。Jev より少なくてよい）
_LIMITS = {"element": 60, "menu": 40, "app": 30, "window": 15, "command": 30}
# plan のステップでのみ使える action（tasks.py のサイト・フォルダひな型が対応）
_PLAN_ONLY = {"open_site", "search_site", "open_location"}
_WEEK = "月火水木金土日"
_PROMPT_FILE = ROOT / ".opencode" / "agents" / "voicectl-fallback.md"
_SYSTEM_CACHE: str | None = None
# local backend の宛先として許すホスト（この PC の中だけ。発話を外部に送らないための制限）
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MAX_RESPONSE = 1 << 20   # 応答は最大 1 MB まで読む


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトを追わない（別の宛先へ転送されるのを防ぐ）。"""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def system_prompt() -> str:
    """システムプロンプト（opencode のエージェント定義の本文）。ローカル backend でも同じものを使う。"""
    global _SYSTEM_CACHE
    if _SYSTEM_CACHE is None:
        try:
            text = _PROMPT_FILE.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text.startswith("---"):  # YAML フロントマター（opencode 用の設定）を外す
            parts = text.split("---", 2)
            text = parts[2] if len(parts) >= 3 else text
        _SYSTEM_CACHE = text.strip()
    return _SYSTEM_CACHE


def local_endpoint(base_url: str) -> str | None:
    """ローカル LLM の URL を検証して chat/completions の URL を返す。

    http/https で、宛先がこの PC（ループバック）のときだけ許可する。設定を書き換えたときに
    発話が外部のホストへ送られるのを防ぐため（送るのは画面の候補や発話そのもの）。
    """
    p = urlparse(base_url or "")
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        log.warning("ローカル LLM の URL が不正です: %r", base_url)
        return None
    if host not in _LOCAL_HOSTS:
        log.warning("ローカル LLM の宛先がこの PC ではありません（%s）。local backend は使いません", host)
        return None
    try:
        socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}/chat/completions"


@dataclass
class LLMResult:
    ok: bool = False
    action: str = "unknown"
    params: dict = field(default_factory=dict)
    text: str | None = None
    plan: list[dict] = field(default_factory=list)
    clarify: str | None = None
    say: str | None = None   # アプリが決まった形で処理できる言い方への言い直し（「20時15分に起こして」など）
    confidence: float = 0.0
    reason: str = ""
    error: str = ""
    engine: str = "llm"

    def decision(self) -> Decision:
        return Decision(action=self.action, params=dict(self.params), confidence=self.confidence,
                        engine=self.engine, text=self.text)


class LLMFallback:
    name = "llm"

    def __init__(self, model: str = "bonsai2-27b", agent: str = "voicectl-fallback",
                 timeout_sec: float = 10.0, max_plan_steps: int = 5, max_text_len: int = 200,
                 command: str = "opencode", cwd: Path | None = None, backend: str = "local",
                 base_url: str = "http://127.0.0.1:8080/v1", max_tokens: int = 512):
        self.model = model
        self.agent = agent
        self.timeout_sec = float(timeout_sec)
        self.max_plan_steps = int(max_plan_steps)
        self.max_text_len = int(max_text_len)
        self.command = command
        self.cwd = str(cwd or ROOT)
        self.backend = str(backend or "local").lower()
        self.base_url = str(base_url)
        self.max_tokens = int(max_tokens)
        self.failures = 0   # 呼び出しに失敗が続いたら controller 側で一定時間休む
        # 表示やログ用の短い名前（bonsai2-27b → bonsai、deepseek/deepseek-flash → deepseek）
        name = self.model.split("/")[-1]
        head = name.split("-")[0]
        self.label = re.sub(r"\d+$", "", head) or head or self.backend

    # ---- 入力の組み立て ----
    def build_input(self, mode: str, text: str, snap: Snapshot, lookup: Lookup,
                    facts: dict | None = None, previous: Decision | None = None,
                    recent: list | None = None, session: dict | None = None) -> dict:
        """プロンプト（.opencode/agents/voicectl-fallback.md）の入力契約どおりの JSON。"""
        cands: dict[str, dict[str, str]] = {}
        for kind, limit in _LIMITS.items():
            src = lookup.get(kind) or {}
            if src:
                cands[kind] = {cid: c.label for cid, c in list(src.items())[:limit]}
        state = {
            "mode": mode,
            "utterance": text,
            "today": datetime.now().strftime(f"%Y-%m-%d ") + "(" + _WEEK[datetime.now().weekday()] + ")",
            "active_window": snap.window.to_state() if snap.window else None,
            "focused_control": None,
            "cursor": {"x": snap.cursor[0], "y": snap.cursor[1]},
            "previous_command": previous.describe(lookup) if previous else None,
            "recent_commands": list(recent or [])[-3:],
            "session": {k: v for k, v in (session or {}).items() if v},
            "visible_elements": [e.name for e in snap.elements if e.source == "uia"][:40],
            "candidates": cands,
            "facts": facts or {},
        }
        return state

    # ---- 呼び出し ----
    def ask(self, mode: str, text: str, snap: Snapshot, lookup: Lookup,
            facts: dict | None = None, previous: Decision | None = None,
            recent: list | None = None, session: dict | None = None) -> LLMResult:
        payload = self.build_input(mode, text, snap, lookup, facts, previous, recent, session)
        t0 = time.perf_counter()
        out = self._run_local(payload) if self.backend == "local" else self._run_opencode(payload)
        if out is None:
            self.failures += 1
            return LLMResult(error=f"{self.backend}_failed", engine=f"llm({self.label})")
        r = self._parse(out, lookup)
        r.engine = f"llm({self.label})"
        r.reason += f" [{time.perf_counter() - t0:.1f}s]"
        if r.ok:
            self.failures = 0
            log.info("LLM フォールバック: %s → %s（%.2f）%s", text, r.action, r.confidence, r.reason[:80])
        return r

    def warmup(self) -> bool:
        """ローカル LLM を一度だけ叩いて温める（最初の 1 発話が待たされないように）。

        llama-server は起動直後の 1 回目だけ余分に時間がかかる（プロンプトの評価）。起動と同時に
        小さな要求を投げておくと、実際の発話では 2〜3 秒で返る。失敗しても何も変えない。
        """
        if self.backend != "local":
            return False
        url = local_endpoint(self.base_url)
        if url is None:
            self.backend = "off"
            return False
        body = {"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with _OPENER.open(req, timeout=max(30.0, self.timeout_sec)) as resp:
                resp.read(_MAX_RESPONSE)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.info("ローカル LLM の予備呼び出しに失敗（起動していない場合は問題ありません）: %s", e)
            return False
        log.info("ローカル LLM（%s）を使える状態にしました", self.model)
        return True

    def _run_local(self, payload: dict) -> str | None:
        """この PC の llama-server（OpenAI 互換）に 1 回だけ問い合わせる。

        思考（reasoning）は切る。返すのは短い JSON だけなので、思考させると数秒〜十数秒かかるうえ、
        思考の途中で切れると JSON が返らないことがある（実測）。
        """
        url = local_endpoint(self.base_url)
        if url is None:
            self.backend = "off"   # 宛先が不正な設定 → 以後は呼ばない
            return None
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt()},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read(_MAX_RESPONSE).decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            log.info("ローカル LLM に接続できません（%s）: %s", url, e)
            return None
        except (TimeoutError, OSError) as e:
            log.warning("ローカル LLM がタイムアウト (%.0f s): %s", self.timeout_sec, e)
            return None
        except ValueError as e:
            log.warning("ローカル LLM の応答が JSON ではありません: %s", e)
            return None
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            log.warning("ローカル LLM の応答に choices がありません: %s", str(data)[:200])
            return None
        content = msg.get("content") or ""
        if not content.strip():   # 思考だけ返って本文が空のときに備えて reasoning_content も見る
            content = msg.get("reasoning_content") or ""
        return content or None

    def _run_opencode(self, payload: dict) -> str | None:
        """opencode run を実行し、応答テキスト（type:text イベントの連結）を返す。失敗したら None。"""
        import shutil
        exe = self._resolve_command(self.command)
        if exe is None:
            log.warning("opencode が見つかりません（LLM フォールバック無効）")
            return None
        cmd = exe + ["run", "--agent", self.agent, "--model", self.model, "--pure", "--format", "json",
                     "--title", "voicectl", "--dir", self.cwd, json.dumps(payload, ensure_ascii=False)]
        try:
            # 標準入力を閉じる：開いたままだと opencode が入力を待ち続け、毎回タイムアウトしていた（2026-09-22）
            p = subprocess.run(cmd, capture_output=True, timeout=self.timeout_sec,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            log.warning("LLM フォールバックがタイムアウト (%.0f s)", self.timeout_sec)
            return None
        except OSError as e:
            log.warning("opencode の起動に失敗: %s", e)
            return None
        if p.returncode != 0:
            tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][-3:]
            log.warning("opencode が失敗 (exit %d, 入力 %d 文字): %s | %s", p.returncode, len(cmd[-1]),
                        (p.stderr or "")[:300], " / ".join(t[:200] for t in tail))
            return None
        return self._extract_text(p.stdout)

    @staticmethod
    def _resolve_command(command: str) -> list[str] | None:
        """opencode の実行方法を解決する。npm の .CMD シム（opencode.CMD）は Python の subprocess から
        直接実行すると出力が取れないため、シムが呼ぶ本体（opencode.exe）を直接使う。
        見つからなければ cmd /c 経由、それも無ければ None。"""
        import shutil
        from pathlib import Path
        exe = shutil.which(command)
        if not exe:
            return None
        if not exe.lower().endswith((".cmd", ".bat")):
            return [exe]
        near = Path(exe).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if near.exists():
            return [str(near)]
        return ["cmd", "/c", exe]

    @staticmethod
    def _extract_text(stdout: str) -> str | None:
        """--format json のイベント列から、応答のテキスト部分を取り出す。"""
        texts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            part = ev.get("part") or {}
            if ev.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if not texts:
            return None
        return "".join(texts)

    # ---- 応答の検証 ----
    def _parse(self, out: str, lookup: Lookup) -> LLMResult:
        data = self._load_json(out)
        if data is None:
            return LLMResult(error="not_json")
        r = LLMResult()
        r.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        r.reason = str(data.get("reason") or "")[:200]
        if data.get("is_pc_command") is False:
            r.action, r.ok = "unknown", True
            r.reason = "会話・独り言と判断 " + r.reason
            return r
        say = data.get("say")
        if isinstance(say, str) and 2 <= len(say.strip()) <= 80:
            r.say, r.action, r.ok = say.strip(), "unknown", True
            return r
        clarify = data.get("clarify")
        if isinstance(clarify, str) and clarify.strip():
            r.clarify, r.action, r.ok = clarify.strip()[:60], "unknown", True
            return r
        plan = data.get("plan")
        if isinstance(plan, list) and plan:
            steps = self._check_plan(plan, lookup)
            if steps is None:
                return LLMResult(error="bad_plan")
            r.plan, r.action, r.ok = steps, "unknown", True
            return r
        action = str(data.get("action") or "unknown")
        if action not in schema.ACTIONS or action == "unknown":
            r.action, r.ok = "unknown", True
            return r
        params = self._check_params(action, data.get("params") or {}, lookup, in_plan=False)
        if params is None:
            return LLMResult(error="bad_params")
        text = data.get("text")
        if action == "type_text":
            if not isinstance(text, str) or not (1 <= len(text) <= self.max_text_len):
                return LLMResult(error="bad_text")
        r.action, r.params, r.text, r.ok = action, params, (text if isinstance(text, str) else None), True
        return r

    @staticmethod
    def _load_json(out: str) -> dict | None:
        """応答から最初の JSON オブジェクトを取り出す（コードフェンス付き・前後に文章があってもよい）。"""
        out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
        start = out.find("{")
        for end in range(len(out), start, -1):
            if out[start:end].rstrip().endswith("}"):
                try:
                    v = json.loads(out[start:end])
                    return v if isinstance(v, dict) else None
                except ValueError:
                    continue
        return None

    def _check_params(self, action: str, params: dict, lookup: Lookup, in_plan: bool) -> dict | None:
        """action が要求する param だけを検証して返す。不正な値があれば None（命令ごと捨てる）。"""
        allowed = set(schema.ACTION_PARAMS.get(action, []))
        out: dict[str, str] = {}
        for k, v in params.items():
            if k not in allowed or not isinstance(v, str) or not v:
                continue
            if k in ("element", "menu", "app", "window", "command"):
                if v not in (lookup.get(k) or {}):
                    return None   # 存在しない候補 ID ＝ 捏造
            elif k == "key" and v not in schema.KEYS:
                return None
            elif k == "direction" and v not in schema.DIRECTIONS:
                return None
            elif k == "amount" and v not in schema.AMOUNT_KEYS:
                return None
            elif k == "button" and v not in schema.BUTTONS:
                return None
            elif k == "window_action" and v not in schema.WINDOW_ACTIONS:
                return None
            elif k == "snap" and v not in schema.SNAPS:
                return None
            elif k == "hint_mode" and v not in schema.HINT_MODES:
                return None
            out[k] = v
        return out

    def _check_plan(self, plan: list, lookup: Lookup) -> list[dict] | None:
        """plan の各ステップを検証する。open_site / search_site / open_location はひな形に対応するかだけ見る。"""
        if not (1 <= len(plan) <= self.max_plan_steps) or not all(isinstance(s, dict) for s in plan):
            return None
        from ..tasks import LOCATIONS, SITES, _BROWSER_WORDS as BROWSER_WORDS
        browsers = {w.lower(): exe for exe, ws in BROWSER_WORDS.items() for w in ws}
        steps: list[dict] = []
        for s in plan[:self.max_plan_steps]:
            action = str(s.get("action") or "")
            if action == "open_location":
                loc = str(s.get("params", {}).get("location") or "")
                if loc not in LOCATIONS:
                    return None
                steps.append({"action": action, "location": loc})
                continue
            if action in _PLAN_ONLY:
                site = str(s.get("params", {}).get("site") or "")
                if site not in SITES:
                    return None
                step = {"action": action, "site": site, "query": str(s.get("params", {}).get("query") or "")[:80]}
                b = str(s.get("params", {}).get("browser") or "").lower()
                if b:
                    exe = browsers.get(b)
                    if not exe:
                        return None
                    step["browser"] = exe
                if action == "search_site" and not step["query"]:
                    return None
                steps.append(step)
                continue
            if action not in schema.ACTIONS or action in ("unknown", "stop", "repeat", "learn_app"):
                return None   # plan には具体的な操作だけ並べる
            params = self._check_params(action, s.get("params") or {}, lookup, in_plan=True)
            if params is None:
                return None
            text = s.get("text")
            if action == "type_text" and (not isinstance(text, str) or not (1 <= len(text) <= self.max_text_len)):
                return None
            steps.append({"action": action, "params": params, "text": text if isinstance(text, str) else None})
        return steps
