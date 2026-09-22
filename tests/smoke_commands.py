"""実機（マイク・画面操作）なしで、追加した言い方が通るかをまとめて確認する。

実行: .venv\\Scripts\\python.exe tests\\smoke_commands.py
操作は一切しない（キー送信・起動は差し替えて、何が呼ばれるかだけ見る）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voicectl import appmap, config as config_mod, describe, followup, schema, tasks  # noqa: E402
from voicectl.context import Snapshot  # noqa: E402
from voicectl.engines.keyword import KeywordEngine, fast_path  # noqa: E402
from voicectl.profiles import Profiles  # noqa: E402
from voicectl.uia import ScreenElement  # noqa: E402
from voicectl.winutil import WindowInfo  # noqa: E402
import voicectl.winutil as winutil  # noqa: E402

fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    if not ok:
        fails.append(f"{label}: {got!r} != {want!r}")
    print(f"{'OK ' if ok else 'NG '} {label}: {got!r}")


cfg = config_mod.load()
eng = KeywordEngine()
win = WindowInfo(1, "YouTube - Brave", "brave.exe", (0, 0, 1920, 1080))
snap = Snapshot(win, (500, 400), [win], [ScreenElement("保存", "ボタン", (10, 10, 60, 30), "uia", 1)])

print("== 高速経路（Jev も LLM も通さない＝通信なしで即実行） ==")
for text, action, params in [
    ("右半分にして", "snap_window", {"snap": "right_half"}),
    ("ウィンドウを左に寄せて", "snap_window", {"snap": "left_half"}),
    ("画面いっぱいにして", "snap_window", {"snap": "screen_wide"}),
    ("別のモニターに移して", "snap_window", {"snap": "next_monitor"}),
    ("アドレスバーを出して", "key_combo", {"key": "address_bar"}),
    ("履歴を見せて", "key_combo", {"key": "history"}),
    ("新しいフォルダを作って", "key_combo", {"key": "new_folder"}),
    ("フルスクリーンにして", "key_combo", {"key": "fullscreen"}),
    ("開発者ツールを開いて", "key_combo", {"key": "dev_tools"}),
    ("パソコンをロック", "key_combo", {"key": "lock_pc"}),
    ("タスクマネージャーを開いて", "key_combo", {"key": "task_manager"}),
    ("クリップボードの履歴", "key_combo", {"key": "clipboard_history"}),
]:
    d = eng.decide(text, snap, {}, None)
    check(f"「{text}」", (d.action, d.params, fast_path(d)), (action, params, True))

# 「左に寄せて」だけは従来どおりマウス移動（ウィンドウを動かすときは「ウィンドウを左に寄せて」）
d = eng.decide("左に寄せて", snap, {}, None)
check("「左に寄せて」（マウス移動のまま）", (d.action, d.params.get("direction")), ("mouse_move", "left"))

print("\n== タスクひな形（LLM なし） ==")
for text, kind in [("ネットフリックスを開いて", "open_site"), ("スポティファイで米津玄師をかけて", "search"),
                   ("楽天で掃除機を検索して", "search"), ("ニコニコ動画を開いて", "open_site"),
                   ("ダウンロードフォルダを開いて", "open_location"), ("ドキュメントフォルダ", "open_location"),
                   ("ごみ箱を見せて", "open_location"), ("BraveでGoogleマップを開いて", "open_site")]:
    t = tasks.parse(text)
    check(f"「{text}」", (t.kind if t else None, t.describe() if t else None),
          (kind, t.describe() if t and t.kind == kind else None))

print("\n== 続きの指示 ==")
for text, want in [("続き", "resume"), ("残りをやって", "resume"), ("さっきのページを開いて", "page"),
                   ("その動画を見せて", "page"), ("そのウィンドウに戻して", "app"), ("さっきのアプリを出して", "app"),
                   ("さっきのやつをもう一回", "again"), ("それ開いて", "that"),
                   ("これ覚えて", "learn"), ("今のを覚えておいて", "learn"),
                   ("それ忘れて", "forget"), ("何覚えてる？", "knowledge"),
                   ("メモ帳を開いて", None), ("そのページを検索して", None), ("続きを書いて", None)]:
    check(f"「{text}」", followup.kind(text), want)

print("\n== 学習（覚える・思い出す・忘れる） ==")
import tempfile  # noqa: E402
from voicectl.learner import Learner  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    lr = Learner(Path(td) / "learned.json", trust_at=2)
    lr.remember("上のタブに切り替えて", "key_combo", {"key": "next_tab"}, source="explicit")
    check("覚えた言い方をそのまま思い出す", lr.lookup("上のタブに切り替えて") is not None, True)
    check("読みが近い言い方（ひらがな）でも思い出す", lr.lookup("うえのたぶにきりかえて") is not None, True)
    check("別アプリ用に覚えた言い方は使わない",
          lr.remember("これ押して", "key_combo", {"key": "enter"}, app="notepad.exe") is not None, True)
    check("  そのアプリでは使える", lr.lookup("これ押して", app="notepad.exe") is not None, True)
    check("  他のアプリでは使わない", lr.lookup("これ押して", app="brave.exe") is None, True)
    e = lr.lookup("上のタブに切り替えて")
    lr.reward(e.key)
    lr.reward(e.key)
    check("2 回成功で確認なしに昇格", lr.confidence(e) >= 0.9, True)
    check("覚えた件数", lr.stats()["count"], 2)
    check("忘れさせた言い方", lr.forget("上のタブに切り替えて") is not None, True)
    check("  忘れたあとは使わない", lr.lookup("上のタブに切り替えて") is None, True)
    check("  保存したファイルが残る", (Path(td) / "learned.json").exists(), True)

print("\n== 画面の位置（タスクバーに隠れない） ==")
for work, w, h, corner, want in [((0, 0, 1920, 1032), 460, 140, "bottom_right", (1444, 876)),
                                 ((0, 0, 1920, 1032), 460, 140, "top_right", (1444, 16)),
                                 ((0, 0, 1920, 984), 460, 140, "bottom_right", (1444, 828))]:
    x, y = winutil.corner_position(work, w, h, corner, 16)
    check(f"作業領域 {work} の {corner}", (x, y, y + h <= work[3]), (want[0], want[1], True))

print("\n== プロファイル ==")
prof = Profiles()
p = prof.active(WindowInfo(1, "YouTube のホーム - Brave", "brave.exe", (0, 0, 100, 100)))
check("Brave で YouTube → タイトル一致を優先", p.name if p else None, "YouTube")
for profile in prof.items:
    print(f"   {profile.name}: {len(profile.commands)} コマンド")
    for cmd in profile.commands:
        for step in cmd.steps:
            if "keys" in step:
                check(f"  {profile.name} / {cmd.name} のキー「{step['keys']}」",
                      appmap.parse_keys(str(step["keys"])) is not None, True)

print("\n== 実行（ドライラン・実際には起動しない） ==")
sent: list = []
_real_startfile = tasks.os.startfile
tasks.os.startfile = lambda target: sent.append(target)
try:
    for location in tasks.LOCATIONS:
        tasks.run(tasks.Task("open_location", location), {})
finally:
    tasks.os.startfile = _real_startfile
check("フォルダ 7 種", sent, [v[1] for v in tasks.LOCATIONS.values()])

sent2: list = []
_real_press = winutil.press_combo
winutil.press_combo = lambda vks: sent2.append(vks)
try:
    from voicectl.executor import Executor
    from voicectl.schema import Decision
    Executor(cfg.raw, {}).run(Decision("snap_window", {"snap": "next_monitor"}, 1.0), {})
finally:
    winutil.press_combo = _real_press
check("snap_window のキー送信", sent2, [schema.SNAPS["next_monitor"][1]])

print("\n== 確認表示（describe） ==")
for d in [Decision("snap_window", {"snap": "screen_wide"}, 1.0),
          Decision("key_combo", {"key": "clipboard_history"}, 1.0),
          Decision("key_combo", {"key": "task_manager"}, 1.0),
          Decision("snap_window", {"snap": "prev_monitor"}, 1.0)]:
    print("   ", describe.describe(d, {}))

print("\n== 結果 ==")
if fails:
    print(f"{len(fails)} 件の不一致:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("すべて期待どおりでした。")
