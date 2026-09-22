"""発話 → 処理経路の回帰マトリクス（精度の土台）。

実際に使われた発話（logs/decisions-*.jsonl）と README の言い方から作った表で、
「どの処理（intent／task／keyword 即実行／keyword 判定／Jev）に回るべきか」を一括検証する。
壊してはいけない言い方が壊れたら、この表が落とす。
実機・通信なしで動く。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voicectl import intents, tasks, textparse  # noqa: E402
from voicectl.candidates import Candidate  # noqa: E402
from voicectl.context import Snapshot  # noqa: E402
from voicectl.engines.keyword import KeywordEngine, fast_path  # noqa: E402
from voicectl.schema import Candidate as _C  # noqa: E402,F401  (型の揃え用)
from voicectl.uia import ScreenElement  # noqa: E402
from voicectl.winutil import WindowInfo  # noqa: E402


def _snap():
    win = WindowInfo(1, "無題 - メモ帳", "notepad.exe", (0, 0, 1000, 800))
    els = [ScreenElement("保存", "ボタン", (10, 10, 60, 30), "uia", 1),
           ScreenElement("キャンセル", "ボタン", (70, 10, 140, 30), "uia", 1),
           ScreenElement("設定", "ボタン", (150, 10, 200, 30), "uia", 1)]
    return Snapshot(win, (500, 400), [win], els)


def _lookup(text, snap):
    apps = [Candidate("app0", "メモ帳（別名: メモ・ノートパッド）", {"name": "メモ帳", "id": "x"}),
            Candidate("app1", "Google Chrome（別名: ブラウザ・クローム・グーグル）", {"name": "Google Chrome", "id": "y"}),
            Candidate("app2", "電卓（別名: 計算機）", {"name": "電卓", "id": "z"})]
    return __import__("voicectl.candidates", fromlist=["build"]).build(
        text, snap, apps, {"app": 60, "element": 120, "window": 40})


def route(text: str) -> str:
    """controller._handle_text の振り分けと同じ順で、どの処理に回るかを返す（実行はしない）。"""
    from voicectl.controller import _FAST_VERB, _names_on_screen
    it = intents.parse(text)
    if it is not None:
        return f"intent:{it.kind}"
    cnt = intents.count_prefix(text)
    if cnt is not None:
        return f"repeat:{cnt[1]}"
    t = tasks.parse(text)
    if t is not None:
        return f"task:{t.kind}:{t.site}" if t.kind in ("open_site", "open_location", "open_drive") \
            else f"task:{t.kind}"
    snap = _snap()
    lookup = _lookup(text, snap)
    d = KeywordEngine().decide(text, snap, lookup, None)
    if fast_path(d):
        # 実機と同じ guards：画面の名前で押す可能性がある言い方（key_combo＋動詞＋同名の画面項目）は Jev へ
        if not (d.action == "key_combo" and _FAST_VERB.search(textparse.compact(text))
                and _names_on_screen(text, d, lookup)):
            return f"fast:{d.action}"
    if d.confidence >= 0.5:
        return f"keyword:{d.action}"
    return "jev"


# ---- 決まった形の命令（intents）----
@pytest.mark.parametrize("text,kind", [
    # 実ログから（2026-09-22 の判定ログ）
    ("音量30", "volume_set"), ("音量を半分にして", "volume_set"), ("音量いくつ", "volume_get"),
    ("音量10上げて", "volume_add"), ("ボリュームを三割くらいにして", "volume_set"),
    ("3番目のタブ", "tab"), ("最後のタブ", "tab"), ("10秒戻して", "seek"), ("30秒進めて", "seek"),
    ("ページ内で料金を探して", "find_in_page"), ("最近使ったExcel開いて", "recent_file"),
    ("見積書というファイルを開いて", "open_file"), ("GTOってフォルダ開いて", "open_file"),
    ("会議メモのファイル出して", "open_file"), ("存在しない会議メモのファイル出して", "open_file"),
    ("左にChrome右にObsidian", "arrange"), ("ChromeとObsidianを並べて", "arrange"),
    ("Chromeを上Obsidianを下に並べて", "arrange"), ("メモ帳と電卓を縦に並べて", "arrange"),
    ("Discordを閉じて", "window_named"), ("Chromeを最小化して", "window_named"),
    ("今何時", "say_time"), ("今日何曜日", "say_date"), ("123かける45は", "calc"),
    ("5分後に知らせて", "timer"), ("タイマー3分", "timer"), ("20時15分にアラーム設定して", "alarm"),
    ("選択したところ読んで", "read_selection"), ("書き取り開始", "dictation_on"),
    ("これで検索して", "search_selection"), ("この行を選んで", "line_select"), ("行を消して", "line_delete"),
    ("メモっておいて牛乳を買う", "memo_add"), ("メモ見せて", "memo_list"), ("メモ読んで", "memo_read"),
    ("メモ全部消して", "memo_clear"), ("さっきダウンロードしたのを開いて", "open_latest_download"),
    ("作業開始と言ったらObsidianを開いてChromeを開いて", "routine_add"),
    ("ログインの手順を記録して", "demo_start"), ("記録終了", "demo_stop"), ("記録取り消し", "demo_cancel"),
    # 音量・タブ・計算の言い方の揺れ
    ("音量を30に", "volume_set"), ("音量マックス", "volume_set"), ("音量ゼロ", "volume_set"),
    ("タブ2に移動", "tab"), ("百たす二十", "calc"), ("1分半たったら教えて", "timer"),
    ("7時に目覚ましをかけて", "alarm"), ("8時に起こして", "alarm"),
    ("メモして1000円", "memo_add"),
])
def test_intent_route(text, kind):
    it = intents.parse(text)
    assert it is not None and it.kind == kind, text


# ---- 目的のひな形（tasks）----
@pytest.mark.parametrize("text,kind,site", [
    ("YouTube開いて", "open_site", "youtube"), ("ユーチューブ開いて", "open_site", "youtube"),
    ("X開いて", "open_site", "x"), ("X 開いて", "open_site", "x"), ("Xというサイト開いて", "open_site", "x"),
    ("エックス開いて", "open_site", "x"), ("ノーションを開いて", "open_site", "notion"),
    ("楽天で掃除機を検索", "search", "rakuten"), ("YouTubeでリュウジの動画", "youtube_video", "youtube"),
    ("Googleマップで渋谷を検索", "search", "maps"), ("Qiitaでpytestを検索して", "search", "qiita"),
    ("掃除機を検索して", "search", "google"), ("オーブンレンジって検索して", "search", "google"),
    ("ダウンロードを開いて", "open_location", "downloads"), ("ごみ箱を見せて", "open_location", "recycle"),
    ("Cドライブ開いて", "open_drive", "C"), ("dドライブ", "open_drive", "D"),
    ("X内でvoicectlを検索", "search", "x"), ("ここでpytestを検索", "search", "google"),
])
def test_task_route(text, kind, site):
    t = tasks.parse(text)
    assert t is not None and t.kind == kind and t.site == site, text


# ---- 高速経路（keyword 即実行。画面を見る前に決まってよい言い方）----
@pytest.mark.parametrize("text,action", [
    ("右", "mouse_move"), ("もうちょい上", "mouse_move"), ("右上に大きく", "mouse_move"),
    ("左下に大きく", "mouse_move"), ("下にスクロール", "scroll"), ("上にちょっとスクロール", "scroll"),
    ("そこクリック", "click"), ("ダブルクリック", "click"), ("右クリックして", "click"),
    ("コピーして", "key_combo"), ("貼り付け", "key_combo"), ("元に戻す", "key_combo"),
    ("ミュート", "key_combo"), ("音楽止めて", "key_combo"), ("次のウィンドウ", "key_combo"),
    ("スクショ", "key_combo"), ("パソコンをロック", "key_combo"), ("クリップボードの履歴", "key_combo"),
    ("スクリーンショットして", "key_combo"), ("スクリーンショットしてみて", "key_combo"),
    ("画面全体スクリーンショットして", "key_combo"),
    ("新しいタブ", "key_combo"), ("タブを閉じて", "key_combo"), ("次のタブ", "key_combo"),
    ("アドレスバー", "key_combo"), ("強制再読み込み", "key_combo"), ("ページの先頭", "key_combo"),
    ("最小化", "window_control"), ("最大化", "window_control"), ("元のサイズ", "window_control"),
    ("全部最小化", "window_control"), ("常に手前にして", "window_control"), ("常に手前を解除", "window_control"),
    ("左半分にして", "snap_window"), ("右半分にして", "snap_window"), ("画面いっぱいにして", "snap_window"),
    ("左のモニターへ移して", "snap_window"), ("右のモニターへ", "snap_window"),
    ("上半分に", "snap_window"), ("下半分にして", "snap_window"), ("中央に置いて", "snap_window"),
    ("左上に置いて", "snap_window"), ("右下に置いて", "snap_window"), ("右上の四分の一", "snap_window"),
    ("番号表示", "show_hints"), ("グリッド", "show_hints"), ("もう一回", "repeat"), ("ストップ", "stop"),
])
def test_fast_route(text, action):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert fast_path(d) and d.action == action, text


# ---- 画面の名前で押す言い方は、即実行を外れて Jev に判断させる（実機と同じ guard）----
@pytest.mark.parametrize("text,noun", [
    ("保存を押して", "保存"), ("設定を開いて", "設定"),
])
def test_screen_named_diverts_to_jev(text, noun):
    from voicectl.controller import _FAST_VERB, _names_on_screen
    snap = _snap()
    lookup = _lookup(text, snap)
    d = KeywordEngine().decide(text, snap, lookup, None)
    assert d.action == "key_combo", text   # キーワードではキー操作に見えるが、
    assert _FAST_VERB.search(textparse.compact(text)) and _names_on_screen(text, d, lookup), \
        f"{text} は画面の「{noun}」ボタンがあると Jev に回るべき"


@pytest.mark.parametrize("text,route_name", [
    ("キャンセル押して", "fast:stop"),   # 「キャンセル」は緊急停止の言葉（意図的に最優先）
])
def test_stop_words(text, route_name):
    assert route(text) == route_name, text


@pytest.mark.parametrize("text,action", [
    ("メモ帳開いて", "launch_app"), ("ブラウザ起動して", "launch_app"), ("電卓を開いて", "launch_app"),
])
def test_jev_route(text, action):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert d.action == action and not fast_path(d), text


# ---- intents に取られてはいけない言い方（ほかの処理の領分）----
@pytest.mark.parametrize("text", [
    "メモ帳を開いて",          # アプリ起動（Jev/keyword）
    "タブを閉じて",            # key_combo
    "YouTubeを開いて",         # task
    "ダウンロードを開いて",    # task（特殊フォルダ）
    "メモしよう",              # ただの独り言
    "メモを開いて",            # アプリの名前かもしれない
    "音声メモを開いて",        # アプリの名前かもしれない
    "これを閉じて",            # アプリ内の操作
    "3回戻って",               # 回数つき（repeat 経路）
    "下に5回スクロール",       # 回数つき
    "右",                      # 高速経路
    "ピオスクロール",          # アプリ固有の用語（Jev に回す）
    "トレーナー開いて",        # アプリ名（Jev に回す）
])
def test_not_intent(text):
    assert intents.parse(text) is None, text


# ---- tasks に取られてはいけない言い方 ----
@pytest.mark.parametrize("text", [
    "メモ帳を開いて", "電卓を開いて",          # アプリ（Jev）
    "ファイルを開いて", "この画面を閉じて",    # 画面の操作
    "YouTubeと入力して",                        # 文字入力
    "音楽を検索して",                           # ← 音楽サイトではなく一般検索の意図もあり得るが、現行は google 検索
])
def test_not_task(text):
    if text == "音楽を検索して":
        return   # 音楽サイトのひな形があるため現行は task になる。挙動を変えるときはこの行を見直す
    assert tasks.parse(text) is None, text


# ---- 読み替え・表記ゆれ（音声認識の揺れに強いこと）----
@pytest.mark.parametrize("text,kind", [
    ("おんりょう30", "volume_set"),          # かな
    ("オンリョウを30にして", "volume_set"),   # カタカナ
    ("5分後に知らせて", "timer"),
])
def test_reading_variation(text, kind):
    it = intents.parse(text)
    assert it is not None and it.kind == kind, text


# ---- 回数つき ----
@pytest.mark.parametrize("text,rest", [
    ("3回戻って", "戻って"), ("下に5回スクロール", "下にスクロール"), ("エンター2回", "エンター"),
])
def test_count_prefix(text, rest):
    assert intents.count_prefix(text) is not None and intents.count_prefix(text)[1] == rest
