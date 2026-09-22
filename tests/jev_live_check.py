"""Jev 公式 API への実呼び出しの確認（手動確認用）。キーはユーザー環境変数から読み、表示しない。

  1. 最小の呼び出し（noul 1 問）で疎通と応答形式を確認
  2. アプリと同じ質問セットで判定させ、結果と遅延を表示
"""
import json
import os
import sys
import time
import winreg
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not os.environ.get("TYPESAFE_API_KEY"):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        os.environ["TYPESAFE_API_KEY"] = winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]

from voicectl.config import load  # noqa: E402

cfg = load()
j = cfg.get("decision.jev")

# キーを送る相手は https か自分の PC だけに限る（設定の書き間違い・改ざんへの防御）
from urllib.parse import urlsplit  # noqa: E402
_host = (urlsplit(j["endpoint"]).hostname or "").lower()
if urlsplit(j["endpoint"]).scheme != "https" and _host not in ("127.0.0.1", "localhost"):
    print("endpoint は https かローカルのアドレスだけを扱います:", j["endpoint"])
    sys.exit(1)

headers = {"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}", "Content-Type": "application/json"}

print("== 1. 最小の呼び出し ==")
body = {"model": j["model"], "state": "メモ帳を開いて",
        "questions": {"is_pc_command": {"type": "noul", "instructions": "Is this a request to operate a computer?"}}}
t = time.perf_counter()
r = httpx.post(j["endpoint"], json=body, headers=headers, timeout=10)
print("status:", r.status_code, f"({(time.perf_counter() - t) * 1000:.0f} ms)")
print(json.dumps(r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text,
                 ensure_ascii=False, indent=1)[:1500])
if r.status_code != 200:
    sys.exit(1)

print("\n== 2. アプリと同じ質問セットでの判定 ==")
from voicectl import candidates  # noqa: E402
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.context import Snapshot  # noqa: E402
from voicectl.engines.jev import JevEngine  # noqa: E402
from voicectl.engines.keyword import KeywordEngine, fast_path  # noqa: E402
from voicectl.schema import Decision  # noqa: E402
from voicectl.uia import ScreenElement  # noqa: E402
from voicectl.winutil import WindowInfo  # noqa: E402

win = WindowInfo(1, "議事録.txt - メモ帳", "Notepad.exe", (0, 0, 1600, 1000))
chrome = WindowInfo(2, "YouTube - Google Chrome", "chrome.exe", (0, 0, 1600, 1000))
els = [ScreenElement(n, k, r, "uia", 1) for n, k, r in [
    ("ファイル", "メニュー項目", (10, 40, 70, 60)), ("編集", "メニュー項目", (80, 40, 130, 60)),
    ("表示", "メニュー項目", (140, 40, 190, 60)), ("保存", "ボタン", (1300, 900, 1400, 940)),
    ("キャンセル", "ボタン", (1420, 900, 1560, 940)), ("最小化", "ボタン", (1450, 0, 1500, 30)),
    ("閉じる", "ボタン", (1550, 0, 1600, 30)), ("設定", "ボタン", (1500, 40, 1540, 60))]]
snap = Snapshot(win, (800, 500), [win, chrome], els)
catalog = AppCatalog(cfg.get("app_aliases"))
eng = JevEngine(j["endpoint"], j["model"], j["api_key_env"], 10.0)
kw = KeywordEngine()
eng.warmup()
from voicectl.controller import Controller  # noqa: E402


class _Sig:
    def emit(self, *a):
        pass


class _Ui:
    status = level = hints_labels = hints_grid = hints_clear = _Sig()


ctl = Controller(cfg, _Ui(), None, None, eng, catalog)  # アプリと同じ判定処理（一致時の引き上げ等）を通す
prev_move = Decision("mouse_move", {"direction": "left", "amount": "medium"}, 1.0)
cases = [
    ("メモ帳開いて", None), ("ブラウザ起動して", None), ("計算機出して", None), ("もうちょい右", None),
    ("そこクリック", None), ("下にスクロール", None), ("保存ボタンを押して", None), ("キャンセルのとこ押して", None),
    ("右上のバツ押して", None), ("コピーして", None), ("さっきの方向にもう少し", prev_move),
    ("ページの一番下まで", None), ("音量ちょっと下げて", None), ("このウィンドウ小さくして", None),
    ("番号出して", None), ("全部選んで", None), ("前のページに戻って", None), ("Chromeに切り替えて", None),
    ("今日はいい天気ですね", None), ("えーっと", None),
]
lat = []
for text, prev in cases:
    lookup = candidates.build(text, snap, catalog.candidates(), {"app": 60, "element": 120, "window": 40})
    ctl.previous = prev
    d = ctl._decide(text, snap, lookup)
    lat.append(d.latency_ms)
    k = kw.decide(text, snap, lookup, prev)
    ks = f"{k.describe(lookup)}({k.confidence:.2f}{',即' if fast_path(k) else ''})" if k.action != "unknown" else "-"
    print(f"{text:14s} {d.latency_ms:5.0f}ms Jev: {d.describe(lookup)} [{d.confidence:.2f} {d.engine}] "
          f"cont={d.is_continuation:.2f} | KW: {ks}")
print(f"Jev 遅延: 平均 {sum(lat)/len(lat):.0f} ms / 最小 {min(lat):.0f} / 最大 {max(lat):.0f}")
