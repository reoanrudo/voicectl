"""ルール処理・キーワードエンジン・Jev のリクエスト組み立てと応答解析のテスト（実機・通信なしで動く）。"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voicectl import candidates, schema, textparse  # noqa: E402
from voicectl.context import ContextCollector, Snapshot, merge_elements  # noqa: E402
from voicectl.engines import jev as jev_mod  # noqa: E402
from voicectl.engines.keyword import KeywordEngine, fast_path  # noqa: E402
from voicectl.schema import Candidate, Decision  # noqa: E402
from voicectl.uia import ScreenElement  # noqa: E402
from voicectl.winutil import WindowInfo  # noqa: E402


# ---- textparse ----
@pytest.mark.parametrize("text,num", [("5番", 5), ("5番。", 5), ("十二", 12), ("じゅうに", 12), ("番号3", 3),
                                      ("二十", 20), ("ご", 5), ("１５", 15), ("メモ帳", None), ("右", None)])
def test_parse_number(text, num):
    assert textparse.parse_number(text) == num


@pytest.mark.parametrize("text,out", [("こんにちはと入力して。", "こんにちは"), ("「よろしく」と入力", "よろしく"),
                                      ("明日の会議って打って", "明日の会議"), ("入力して ありがとう", "ありがとう"),
                                      ("メモ帳開いて", None)])
def test_extract_text(text, out):
    assert textparse.extract_text(text) == out


def test_stop_yes_no():
    assert textparse.is_stop("ストップ。")
    assert textparse.is_stop("止めて")
    assert not textparse.is_stop("ストップウォッチを開いて")
    assert textparse.is_yes("はい")
    assert textparse.is_yes("オッケー")
    assert textparse.is_no("いいえ")
    assert not textparse.is_yes("いいえ")


# ---- キーワードエンジン ----
def _snap():
    win = WindowInfo(1, "無題 - メモ帳", "notepad.exe", (0, 0, 1000, 800))
    els = [ScreenElement("保存", "ボタン", (10, 10, 60, 30), "uia", 1),
           ScreenElement("キャンセル", "ボタン", (70, 10, 140, 30), "uia", 1)]
    return Snapshot(win, (500, 400), [win], els)


def _lookup(text, snap):
    apps = [Candidate("app0", "メモ帳（別名: メモ・ノートパッド）", {"name": "メモ帳", "id": "x"}),
            Candidate("app1", "Google Chrome（別名: ブラウザ・クローム・グーグル）", {"name": "Google Chrome", "id": "y"}),
            Candidate("app2", "電卓（別名: 計算機）", {"name": "電卓", "id": "z"})]
    return candidates.build(text, snap, apps, {"app": 60, "element": 120, "window": 40})


@pytest.mark.parametrize("text,action,params,fast", [
    ("もうちょい右", "mouse_move", {"direction": "right", "amount": "small"}, True),
    ("右上に大きく", "mouse_move", {"direction": "up_right", "amount": "large"}, True),
    ("下にスクロール。", "scroll", {"direction": "down", "amount": "medium"}, True),
    ("そこクリック", "click", {"button": "left"}, True),
    ("ダブルクリック", "click", {"button": "double"}, True),
    ("右クリックして", "click", {"button": "right"}, True),
    ("コピーして", "key_combo", {"key": "copy"}, True),
    ("元に戻す", "key_combo", {"key": "undo"}, True),
    ("最小化", "window_control", {"window_action": "minimize"}, True),
    ("番号表示", "show_hints", {"hint_mode": "elements"}, True),
    ("グリッド", "show_hints", {"hint_mode": "grid"}, True),
    ("もう一回", "repeat", {}, True),
    ("ストップ", "stop", {}, True),
    # 2026-09-22 追加（2 回目）
    ("次のウィンドウ", "key_combo", {"key": "next_window"}, True),
    ("前の窓", "key_combo", {"key": "prev_window"}, True),
    ("音楽止めて", "key_combo", {"key": "media_play_pause"}, True),
    ("音楽流して", "key_combo", {"key": "media_play_pause"}, True),
    ("画面を撮って", "key_combo", {"key": "screenshot_save"}, True),
    ("切り取って", "key_combo", {"key": "screenshot"}, True),
    ("常に手前にして", "window_control", {"window_action": "always_on_top"}, True),
    ("常に手前を解除", "window_control", {"window_action": "topmost_off"}, True),
    # 2026-09-22 追加（3 回目）：上半分・下半分・中央・四分の一の配置
    ("上半分に", "snap_window", {"snap": "upper_half"}, True),
    ("下半分にして", "snap_window", {"snap": "lower_half"}, True),
    ("中央に置いて", "snap_window", {"snap": "center"}, True),
    ("真ん中にして", "snap_window", {"snap": "center"}, True),
    ("左上に置いて", "snap_window", {"snap": "top_left"}, True),
    ("右上の四分の一", "snap_window", {"snap": "top_right"}, True),
    ("左下に置いて", "snap_window", {"snap": "bottom_left"}, True),
    ("右下に置いて", "snap_window", {"snap": "bottom_right"}, True),
])
def test_keyword_simple(text, action, params, fast):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert d.action == action
    for k, v in params.items():
        assert d.params.get(k) == v
    assert fast_path(d) == fast


def test_keyword_fast_without_lookup():
    """即実行の短い命令は、画面の候補がなくても（= 画面を見る前でも）確定する。"""
    snap = _snap()
    eng = KeywordEngine()
    for text, action in [("右", "mouse_move"), ("クリックして", "click"), ("下にスクロール", "scroll"),
                         ("左半分にして", "snap_window"), ("上半分に", "snap_window"),
                         ("最小化", "window_control"), ("コピーして", "key_combo"), ("もう一回", "repeat")]:
        d = eng.decide(text, snap, {}, None)
        assert fast_path(d) and d.action == action, text
    # 画面の名前で押す可能性のある言い方（key_combo＋動詞）は即実行の対象から外す
    d = eng.decide("設定を開いて", snap, {}, None)
    assert d.action == "key_combo"   # 判定自体は出るが、controller 側で _FAST_VERB ではじく
    from voicectl import controller as controller_mod
    assert controller_mod._FAST_VERB.search(textparse.compact("設定を開いて"))
    assert not controller_mod._FAST_VERB.search(textparse.compact("ミュート"))
    # 「左上」だけのときはマウス移動のまま（配置は「左上に置いて」）
    d = eng.decide("左上", snap, {}, None)
    assert d.action == "mouse_move"


def test_partial_reuse_condition():
    """話している最中の認識を確定に使える条件（最新で、追加音声 0.5 秒以内）だけを検査する。"""
    from voicectl.controller import Controller
    now = 1000.0
    assert Controller._can_reuse_partial(("テストです", now - 0.2, 16000 * 2), 16000 * 2 + 8000, now)
    assert not Controller._can_reuse_partial(("テストです", now - 0.2, 16000 * 2), 16000 * 3, now)  # 1 秒追加分は再認識
    assert not Controller._can_reuse_partial(("テストです", now - 3.0, 16000 * 2), 16000 * 2 + 1000, now)  # 古い
    assert not Controller._can_reuse_partial(None, 16000 * 2, now)


def test_quarter_snap_rects():
    from voicectl.schema import SNAP_RECTS, SNAPS
    assert set(SNAP_RECTS) <= set(SNAPS)   # Jev の選択肢にも載っている
    assert SNAP_RECTS["upper_half"] == (0.0, 0.0, 1.0, 0.5)
    assert SNAP_RECTS["top_left"] == (0.0, 0.0, 0.5, 0.5)
    assert all(0 <= v <= 1 for rect in SNAP_RECTS.values() for v in rect)


def test_run_layout_places_each_window(monkeypatch):
    """配置のひな形の実行：見つけた窓を指定の場所に置き、最後の窓を前面にする（実機なし）。"""
    import queue
    import types

    from voicectl import controller as controller_mod
    from voicectl import intents as intents_mod
    from voicectl import winutil
    from voicectl.winutil import WindowInfo

    placed, focused, launched = [], [], []
    win = WindowInfo(11, "ZCode — 開発", "zcode.exe", (0, 0, 800, 600))
    win2 = WindowInfo(12, "YouTube - Brave", "brave.exe", (0, 0, 800, 600))
    monkeypatch.setattr(intents_mod, "find_window", lambda n: win if "z" in n.lower() else win2)
    monkeypatch.setattr(winutil, "place_rect", lambda h, p: placed.append((h, p)))
    monkeypatch.setattr(winutil, "window_command", lambda h, a: launched.append((h, a)))
    monkeypatch.setattr(winutil, "focus_window", lambda h: focused.append(h))

    ctl = types.SimpleNamespace(layouts={"開発": [{"app": "ZCode", "place": "left_half"},
                                                 {"app": "Brave", "place": "right_half"}]},
                                _q=queue.Queue())
    ctl._reply = lambda title, lines, **kw: placed.append(("reply", title))
    controller_mod.Controller._run_layout(ctl, "開発", "聞き取り：開発の配置")
    assert sorted(x for x in placed if x != ("reply", "開発 の配置にしました（2 窓）"))[:2] == [(11, "left_half"), (12, "right_half")]
    assert focused == [12]   # 最後の窓を前面に


def test_keyword_named_targets():
    snap = _snap()
    eng = KeywordEngine()
    lk = _lookup("メモ帳開いて", snap)
    d = eng.decide("メモ帳開いて", snap, lk, None)
    assert d.action == "launch_app" and lk["app"][d.params["app"]].data["name"] == "メモ帳"
    assert not fast_path(d)  # 名前の解決は Jev に任せる
    lk = _lookup("ブラウザ起動して", snap)
    d = eng.decide("ブラウザ起動して", snap, lk, None)
    assert d.action == "launch_app" and lk["app"][d.params["app"]].data["name"] == "Google Chrome"
    lk = _lookup("キャンセルを押して", snap)
    d = eng.decide("キャンセルを押して", snap, lk, None)
    assert d.action == "click_element" and lk["element"][d.params["element"]].data.name == "キャンセル"


def test_keyword_continuation():
    snap = _snap()
    prev = Decision("mouse_move", {"direction": "left", "amount": "medium"}, 1.0)
    d = KeywordEngine().decide("もうちょい", snap, _lookup("もうちょい", snap), prev)
    assert d.action == "mouse_move" and d.params == {"direction": "left", "amount": "small"}


def test_keyword_type_text():
    snap = _snap()
    d = KeywordEngine().decide("こんにちはと入力して", snap, _lookup("", snap), None)
    assert d.action == "type_text" and d.text == "こんにちは"


# ---- 画面要素の統合 ----
def test_merge_elements_drops_duplicate_ocr():
    uia = [ScreenElement("保存", "ボタン", (10, 10, 60, 30), "uia", 1)]
    ocr = [ScreenElement("保存", "画面上の文字", (20, 12, 50, 28), "ocr"),
           ScreenElement("送信", "画面上の文字", (200, 12, 240, 28), "ocr")]
    names = [e.name for e in merge_elements(uia, ocr)]
    assert names == ["保存", "送信"]


# ---- Windows の状況の先読み（F5 を押した瞬間に winctx.collect を済ませて遅延を隠す） ----
def _collector(scan_fn):
    return ContextCollector(use_uia=False, use_ocr=False, win_context_scan=scan_fn)


def test_win_context_prefetched_at_begin():
    seen = {}

    def scan(win):
        seen["hwnd"] = win.hwnd
        return "CTX"

    c = _collector(scan)
    c.begin()
    assert c.win_context(timeout=2.0) == "CTX"
    # 先読みしたのと同じ窓ならそのまま返り、違う窓なら使わない（取り直しさせる）
    assert c.win_context(WindowInfo(seen["hwnd"], "x", "y.exe", (0, 0, 10, 10)), timeout=2.0) == "CTX"
    assert c.win_context(WindowInfo(seen["hwnd"] + 1, "x", "y.exe", (0, 0, 10, 10)), timeout=2.0) is None


def test_win_context_before_begin_is_none():
    assert _collector(lambda win: "CTX").win_context() is None


def test_win_context_stale_prefetch_is_discarded():
    c = _collector(lambda win: "CTX")
    c.begin()
    assert c.win_context(timeout=2.0, max_age_sec=-1.0) is None


def test_win_context_scan_failure_is_none():
    def scan(win):
        raise RuntimeError("取れない")

    c = _collector(scan)
    c.begin()
    assert c.win_context(timeout=2.0) is None


def test_win_context_slow_scan_times_out_then_falls_back():
    import time as _time

    def scan(win):
        _time.sleep(0.05)
        return "CTX"

    c = _collector(scan)
    c.begin()
    assert c.win_context(timeout=0.001) is None
    assert c.win_context(timeout=2.0) == "CTX"   # そのうち終わるので、次には使える


# ---- Jev：リクエストの形 ----
def test_jev_request_shape(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        body = seen["body"]
        el_id = next(k for k, v in body["questions"]["element"]["criteria"].items() if v.startswith("保存"))
        return httpx.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {
                "action": {"type": "choice", "choice": "click_element", "confidence": 0.91,
                           "probabilities": {"click_element": 0.93, "key_combo": 0.07}},
                "element": {"type": "choice", "choice": el_id, "confidence": 0.88},
                "button": {"type": "choice", "choice": "left", "confidence": 0.97},
                "amount": {"type": "score", "score": 1.05, "confidence": 0.6,
                           "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1}},
                "is_continuation": {"type": "noul", "noul": 0.04},
            },
            "usage": {"input_tokens": 900, "output_tokens": 12},
        })

    eng = jev_mod.JevEngine("https://api.typesafe.ai/v1/systemone", "jev-latest")
    eng._client = httpx.Client(transport=httpx.MockTransport(handler),
                               headers={"Authorization": "Bearer test-key"})
    snap = _snap()
    lk = _lookup("保存ボタンを押して", snap)
    d = eng.decide("保存ボタンを押して", snap, lk, None)

    body = seen["body"]
    assert seen["auth"] == "Bearer test-key"
    assert body["model"] == "jev-latest"
    assert body["state"]["utterance"] == "保存ボタンを押して"
    assert body["state"]["active_window"]["app"] == "notepad.exe"
    q = body["questions"]
    # メニューも独自コマンドもない画面では、その 2 つのアクションは選択肢に出さない
    assert q["action"]["type"] == "choice"
    assert set(q["action"]["criteria"]) == set(schema.ACTIONS) - {"menu_command", "app_command"}
    assert q["amount"]["type"] == "score" and 2 <= len(q["amount"]["criteria"]) <= 10
    assert q["is_continuation"]["type"] == "noul"
    for kind in ("app", "element", "window", "key", "direction"):
        assert len(q[kind]["criteria"]) <= 255
    assert "none" in q["element"]["criteria"]

    assert d.action == "click_element"
    assert lk["element"][d.params["element"]].data.name == "保存"
    assert d.params["button"] == "left"
    assert d.confidence == pytest.approx(0.88)  # action と element の小さい方
    assert "amount" not in d.params            # click_element には不要なので捨てる


def test_jev_parse_none_choice_zeroes_confidence():
    qs = jev_mod.JevEngine.build_questions({"app": {"app0": Candidate("app0", "メモ帳")}})
    d = jev_mod.JevEngine.parse({"answers": {
        "action": {"type": "choice", "choice": "launch_app", "confidence": 0.95},
        "app": {"type": "choice", "choice": "none", "confidence": 0.9},
    }}, qs)
    assert d.action == "launch_app" and d.confidence == 0.0


def test_jev_score_index_variants():
    crit = schema.AMOUNTS
    assert jev_mod._score_index({"probabilities": {crit[2]: 0.7, crit[0]: 0.3}}, crit) == 2
    assert jev_mod._score_index({"probabilities": {"1": 0.9, "2": 0.05, "3": 0.05}}, crit) == 0
    assert jev_mod._score_index({"score": 0.2}, crit) == 0
    assert jev_mod._score_index({"score": 2.8, "legend": {"1": "a", "2": "b", "3": "c"}}, crit) == 2


def test_jev_errors(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    eng = jev_mod.JevEngine("https://x/v1/systemone", "jev-latest")
    eng._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(jev_mod.JevError):
        eng.decide("右", _snap(), {}, None)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(jev_mod, "_user_env", lambda name: None)  # 実機のユーザー環境変数を読まない
    with pytest.raises(jev_mod.JevError):
        jev_mod.JevEngine("https://x", "jev-latest")


# ---- 読み替え辞書 ----
@pytest.mark.parametrize("text,out", [
    ("ユーチューブ開いて", "YouTube開いて"),
    ("クロームに切り替えて", "Chromeに切り替えて"),
    ("パスワードを入力", "パスワードを入力"),          # 単語の一部（ワード）は置き換えない
    ("ワードを開いて", "Wordを開いて"),
    ("ジーメールとユーチューブ", "GmailとYouTube"),
    ("ブイエスコード起動", "VS Code起動"),
])
def test_apply_dictionary(text, out):
    d = {"ユーチューブ": "YouTube", "クローム": "Chrome", "ワード": "Word", "ジーメール": "Gmail",
         "ブイエスコード": "VS Code"}
    assert textparse.apply_dictionary(text, d) == out


def test_dictionary_then_type_text():
    t = textparse.apply_dictionary("ユーチューブと入力して", {"ユーチューブ": "YouTube"})
    assert textparse.extract_text(t) == "YouTube"


@pytest.mark.parametrize("text,out", [
    ("ピオソロバー開いて", "PioSolver開いて"),     # 1 文字の聞き間違い
    ("ピオソルバー開いて", "PioSolver開いて"),
    ("スクロールして", "スクロールして"),         # 辞書にない語はそのまま
    ("ブラウザ起動", "ブラウザ起動"),
])
def test_fuzzy_katakana(text, out):
    assert textparse.apply_dictionary(text, {"ピオソルバー": "PioSolver", "ブレイブ": "Brave"}) == out


# ---- 長音の表記ゆれ（おー / おお / おう）と読みによる照合 ----
@pytest.mark.parametrize("text,out", [
    ("おーぶけ", "おうぶけ"),      # 母音が「お」のかなの後のーは「う」に
    ("もーちょい", "もうちょい"),
    ("ユーチューブ", "ユウチュウブ"),
    ("シート", "シイト"),          # 母音が「い」のかなの後はその母音を重ねる
    ("ケーキ", "ケイキ"),          # 母音が「え」のかなの後のーは「い」に
    ("ーわー", "ーわあ"),          # 行頭のーはそのまま
])
def test_expand_long_vowels(text, out):
    assert textparse.expand_long_vowels(text) == out


@pytest.mark.parametrize("text,out", [
    ("おーぶけと入力して", "大負けと入力して"),    # 辞書の読みは「おお」、認識は「おー」
    ("おおぶけと入力して", "大負けと入力して"),    # 読みどおりに認識された場合
    ("トーキョーの地図", "東京の地図"),           # 読みは「トウキョウ」、認識は「トーキョー」
    ("とうきょうの地図", "東京の地図"),           # ひらがなで出ても置き換える
    ("ユーチューブを開いて", "YouTubeを開いて"),
])
def test_apply_dictionary_long_vowel(text, out):
    d = {"おおぶけ": "大負け", "トウキョウ": "東京", "ユーチューブ": "YouTube"}
    assert textparse.apply_dictionary(text, d) == out


@pytest.mark.parametrize("text,out", [
    ("ディーシークと入力して", "DeepSeekと入力して"),   # 1 文字の聞き間違い＋長音のゆれ
    ("ぴおそるばーを開いて", "PioSolverを開いて"),      # ひらがなで出ても
])
def test_fuzzy_katakana_long_vowel(text, out):
    assert textparse.apply_dictionary(text, {"ディープシーク": "DeepSeek", "ピオソルバー": "PioSolver"}) == out


def test_similarity_long_vowel():
    assert textparse.similarity("もーちょい", "もうちょい") >= 1.0
    assert textparse.similarity("さくじょ", "削除") == 0.0   # 漢字かなの違いは読み照合（ja_ratio）の担当


def test_ja_ratio():
    pytest.importorskip("pykakasi")
    from voicectl import phonetic
    assert phonetic.ja_ratio("佐久助おして", "削除") == 1.0    # 漢字の同音異義
    assert phonetic.ja_ratio("さくじょ押して", "削除") == 1.0  # かなと漢字
    assert phonetic.ja_ratio("もーちょい", "もうちょい") == 1.0  # 長音のゆれ
    assert phonetic.ja_ratio("コピーして", "削除") < 0.5       # 違う語


def test_candidates_reading_match():
    """読みが同じなら、かなで言っても漢字のボタンが候補の上位に来る。"""
    pytest.importorskip("pykakasi")
    snap = _snap()
    look = candidates.build("ほぞんおして", snap, [], {"app": 10, "element": 1, "window": 10})
    assert list(look["element"]) == ["e0"]   # e0 = 「保存」ボタン


# ---- 目的のひな形（複数の手順） ----
from voicectl import tasks  # noqa: E402
from voicectl.controller import _split_chain  # noqa: E402


@pytest.mark.parametrize("text,kind,site,query", [
    ("YouTubeを開いてリュウジさんの動画までたどり着いて。", "youtube_video", "youtube", "リュウジ"),
    ("ユーチューブで猫の動画を見せて", "youtube_video", "youtube", "猫"),
    ("YouTube開いて", "open_site", "youtube", ""),
    ("Googleで天気を調べて", "search", "google", "天気"),
    ("アマゾンでマウスを探して", "search", "amazon", "マウス"),
    ("明日の天気について調べて", "search", "google", "明日の天気"),
])
def test_task_parse(text, kind, site, query):
    t = tasks.parse(text)
    assert t is not None and (t.kind, t.site, t.query) == (kind, site, query)


@pytest.mark.parametrize("text", ["YouTubeと入力して", "メモ帳開いて", "戦略を表示して", "右にスクロール"])
def test_task_parse_ignores_other_commands(text):
    assert tasks.parse(text) is None


def test_task_uses_window_title_for_context():
    t = tasks.parse("リュウジさんの動画までたどり着いて", "ホーム - YouTube - Brave")
    assert t is not None and t.kind == "youtube_video" and t.query == "リュウジ"


def test_split_chain():
    assert _split_chain("メモ帳を開いてから、こんにちはと入力して") == ["メモ帳を開いて", "こんにちはと入力して"]
    assert _split_chain("戦略を表示して") == ["戦略を表示して"]


def test_split_chain_te_form():
    """読点なしの複文（〇〇開いて△△して）も分ける。続きが 3 文字未満なら分けない。"""
    assert _split_chain("オブシリアン開いて全面に表示して。") == ["オブシリアン開いて", "全面に表示して"]
    assert _split_chain("Braveを開いて、YouTubeで猫を検索して") == ["Braveを開いて", "YouTubeで猫を検索して"]
    assert _split_chain("メモ帳開いて") == ["メモ帳開いて"]
    assert _split_chain("ツールバーからGTOトレーナーを選択して開いて。") == ["ツールバーからGTOトレーナーを選択して開いて。"]


def test_looks_unfinished():
    from voicectl.controller import _looks_unfinished
    assert _looks_unfinished("ツールバーからGTOトレーナーを")
    assert _looks_unfinished("グーグルマップを開いてオーブケーキでし")   # 自動区切りで途中で切れた
    assert not _looks_unfinished("メモ帳開いて")


# ---- LLM フォールバック（DeepSeek V4.1-Flash / opencode）の応答検証 ----
from voicectl.engines.llm import LLMFallback  # noqa: E402


def _llm_lookup():
    apps = [Candidate("a3", "メモ帳", {"name": "メモ帳", "id": "x"})]
    els = [Candidate("e7", "保存 [ボタン] [位置: 中央]", ScreenElement("保存", "ボタン", (1, 1, 2, 2), "uia", 1))]
    return {"app": {c.id: c for c in apps}, "element": {c.id: c for c in els}}


def test_llm_extract_text_events():
    import json as j
    events = "\n".join(j.dumps(e) for e in [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"text": '{"a": 1}'}},
        {"type": "text", "part": {"text": '{"b": 2}'}},
        {"type": "step_finish", "part": {}},
    ])
    assert LLMFallback._extract_text(events) == '{"a": 1}{"b": 2}'


def test_llm_parse_fenced():
    r = LLMFallback()._parse('前段の説明\n```json\n{"is_pc_command": true, "action": "click_element", '
                             '"params": {"element": "e7"}, "confidence": 0.8, "reason": "x"}\n```\n後段',
                             _llm_lookup())
    assert r.ok and r.action == "click_element" and r.params == {"element": "e7"}


def test_llm_parse_rejects_fake_ids():
    r = LLMFallback()._parse('{"is_pc_command": true, "action": "click_element", '
                             '"params": {"element": "e999"}, "confidence": 0.9}', _llm_lookup())
    assert not r.ok


def test_llm_parse_clarify_and_not_pc():
    lk = _llm_lookup()
    r = LLMFallback()._parse('{"is_pc_command": false, "action": "unknown", "confidence": 0.9}', lk)
    assert r.ok and r.action == "unknown"
    r = LLMFallback()._parse('{"is_pc_command": true, "action": "unknown", '
                             '"clarify": "どれを開きますか？", "confidence": 0.3}', lk)
    assert r.ok and r.clarify and r.action == "unknown"


def test_llm_parse_plan():
    body = ('{"is_pc_command": true, "action": "unknown", "params": {}, "plan": ['
            '{"action": "launch_app", "params": {"app": "a3"}},'
            '{"action": "type_text", "text": "買い物リスト"},'
            '{"action": "key_combo", "params": {"key": "save"}},'
            '{"action": "search_site", "params": {"site": "youtube", "query": "猫", "browser": "brave"}}],'
            '"confidence": 0.88}')
    r = LLMFallback()._parse(body, _llm_lookup())
    assert r.ok and len(r.plan) == 4
    assert r.plan[0]["params"] == {"app": "a3"}
    assert r.plan[1]["text"] == "買い物リスト"
    assert r.plan[3]["site"] == "youtube" and r.plan[3]["browser"] == "brave.exe"


def test_llm_parse_bad_plan():
    lk = _llm_lookup()
    bad = ('{"is_pc_command": true, "action": "unknown", "plan": ['
           '{"action": "search_site", "params": {"site": "ないサイト", "query": "x"}}]}')
    assert not LLMFallback()._parse(bad, lk).ok
    bad2 = '{"is_pc_command": true, "action": "type_text", "params": {}, "text": "", "confidence": 0.9}'
    assert not LLMFallback()._parse(bad2, lk).ok


@pytest.mark.parametrize("text,kind,site,query,browser", [
    ("Google マップを開いて。", "open_site", "maps", "", None),                     # 空白入りのサイト名
    ("BraveでGoogle マップを開いて。", "open_site", "maps", "", "brave.exe"),         # ブラウザの指定
    ("グーグルマップを開いてオーブケーキを表示して", "search", "maps", "オーブケーキ", None),
    ("BraveでGoogleマップを検索して開いて。", "open_site", "maps", "", "brave.exe"),  # 動詞が続く
])
def test_task_parse_sites_and_browsers(text, kind, site, query, browser):
    t = tasks.parse(text)
    assert t is not None and (t.kind, t.site, t.query, t.browser) == (kind, site, query, browser)


# ---- 追加した操作の網羅性（キー・ウィンドウ寄せ・サイト・フォルダ） ----
@pytest.mark.parametrize("text,key", [
    ("アドレスバー", "address_bar"), ("URLバーを出して", "address_bar"),
    ("ブックマークして", "bookmark"), ("お気に入りに追加して", "bookmark"),
    ("履歴を見せて", "history"), ("ダウンロード一覧", "downloads"),
    ("閉じたタブを開き直して", "reopen_tab"), ("ズームを戻して", "zoom_reset"),
    ("名前を付けて保存", "save_as"), ("新しいフォルダを作って", "new_folder"),
    ("次を検索", "find_next"), ("印刷して", "print"), ("フルスクリーンにして", "fullscreen"),
    ("開発者ツールを開いて", "dev_tools"), ("パソコンをロック", "lock_pc"),
    ("タスクマネージャーを開いて", "task_manager"), ("クリップボードの履歴", "clipboard_history"),
    ("次のデスクトップ", "desktop_next"), ("前のデスクトップ", "desktop_prev"),
    ("新しいウィンドウを開いて", "new_window"), ("シークレットウィンドウ", "incognito"),
    ("Windowsの設定を開いて", "settings"), ("通知を開いて", "notifications"),
    ("検索して置換", "replace"), ("強制再読み込み", "hard_reload"),
    ("ページの先頭に戻って", "page_top"), ("ページの最後へ", "page_bottom"),
    ("音声入力", "voice_typing"), ("絵文字パネル", "emoji"),
])
def test_keyword_new_keys(text, key):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert d.action == "key_combo" and d.params.get("key") == key
    assert fast_path(d)          # Jev も LLM も通さず即実行できる


def test_keyword_particle_less_keys():
    """「タブ閉じて」のように助詞「を」を省いた言い方でも、キー操作として拾えること。"""
    snap = _snap()
    for text, key in [("タブ閉じて", "close_tab"), ("音量上げて", "volume_up"), ("新しいタブ開いて", "new_tab"),
                      ("ウィンドウ閉じて", None), ("辞書を引いて", None)]:
        d = KeywordEngine().decide(text, snap, _lookup("メモ帳開いて", snap), None)
        if key:
            assert (d.action, d.params.get("key")) == ("key_combo", key), text
        else:
            assert d.action != "key_combo", text


@pytest.mark.parametrize("text,snap_key", [
    ("右半分にして", "right_half"), ("左半分に表示して", "left_half"), ("ウィンドウを左に寄せて", "left_half"),
    ("今のウィンドウを右に寄せて", "right_half"), ("別のモニターに移して", "next_monitor"),
    ("左のモニターへ移動して", "prev_monitor"), ("次のモニター", "next_monitor"),
    ("画面いっぱいにして", "screen_wide"),
])
def test_keyword_snap_window(text, snap_key):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert d.action == "snap_window" and d.params.get("snap") == snap_key
    assert fast_path(d)


def test_keyword_snap_does_not_steal_mouse_move():
    """「左に寄せて」はマウス移動、「ウィンドウを左に寄せて」はウィンドウ移動（区別する）。"""
    snap = _snap()
    d = KeywordEngine().decide("左に寄せて", snap, _lookup("左に寄せて", snap), None)
    assert d.action == "mouse_move" and d.params.get("direction") == "left"


def test_schema_actions_have_params():
    """すべての action にパラメータ定義があり、SNAPS/KEYS の値が壊れていないこと。"""
    for action in schema.ACTIONS:
        assert action in schema.ACTION_PARAMS, action
    for key, (desc, vks) in schema.KEYS.items():
        assert desc and vks, key
    for name, (desc, vks) in schema.SNAPS.items():
        # 割合指定の配置（上半分・四分の一など）はキー列の代わりに SNAP_RECTS を使うので vks は None でよい
        assert desc and (vks or name in schema.SNAP_RECTS), name
    assert set(schema.SNAPS) & set(schema.WINDOW_ACTIONS) == set()   # 別々の選択肢として扱える


def test_describe_snap_window():
    from voicectl.describe import describe
    d = Decision("snap_window", {"snap": "right_half"}, 1.0)
    assert describe(d, {}) == "画面の右半分に寄せる"


def test_executor_snap_uses_schema_keys(monkeypatch):
    """snap_window は schema.SNAPS のキー列をそのまま押す。"""
    from voicectl import executor
    from voicectl.executor import Executor
    sent = []
    monkeypatch.setattr(executor.winutil, "press_combo", lambda vks: sent.append(vks))
    d = Decision("snap_window", {"snap": "next_monitor"}, 1.0)
    Executor({}, {}).run(d, {})
    assert sent == [schema.SNAPS["next_monitor"][1]]


@pytest.mark.parametrize("text,kind,site", [
    ("ダウンロードを開いて", "open_location", "downloads"),
    ("ごみ箱を見せて", "open_location", "recycle"),
    ("ピクチャフォルダを開いて", "open_location", "pictures"),
    ("ミュージックを開いて", "open_location", "music"),
    ("デスクトップフォルダを開いて", "open_location", "desktop"),
    ("ドキュメントフォルダ", "open_location", "documents"),
])
def test_task_parse_locations(text, kind, site):
    t = tasks.parse(text)
    assert t is not None and (t.kind, t.site) == (kind, site)
    assert "None" not in t.describe()


def test_task_parse_locations_do_not_steal_other_commands():
    assert tasks.parse("デスクトップを表示して") is None      # Win+D（デスクトップ表示）のまま
    assert tasks.parse("ダウンロード一覧を見せて") is None    # ブラウザのダウンロード一覧（Ctrl+J）
    assert tasks.parse("ミュージックフォルダの曲を消して") is None


@pytest.mark.parametrize("text,site,query", [
    ("ネットフリックスを開いて", "netflix", ""),
    ("ネトフリで鬼滅の刃を探して", "netflix", "鬼滅の刃"),
    ("スポティファイで音楽をかけて", "spotify", "音楽"),
    ("Amazonプライムで映画を探して", "prime", "映画"),
    ("ユーチューブミュージックを開いて", "music", ""),
    ("楽天でマウスを探して", "rakuten", "マウス"),
    ("Googleドライブを開いて", "gdrive", ""),
])
def test_task_parse_added_sites(text, site, query):
    t = tasks.parse(text)
    assert t is not None and (t.site, t.query) == (site, query)


# ---- 分けて実行した命令の「続き」 ----
def _fake_controller(monkeypatch, last_chain=None, last_plan=None):
    """Controller を組み立てずに「続き」の再開だけを試すための最小の代役。"""
    import types
    from voicectl import controller as ctl_mod
    messages = []
    ran: list[str] = []

    def emit(kind, main, lines=None, sec=0.0):
        messages.append((kind, main))

    fake = types.SimpleNamespace(
        _last_chain=last_chain, _last_plan=last_plan, _PLAN_TTL=300.0, _current_text="",
        _handle_text=lambda part, stt_ms=0.0, profile=None, final=True, depth=0: ran.append(part),
        ui=types.SimpleNamespace(status=types.SimpleNamespace(emit=emit),
                                 transcript_result=types.SimpleNamespace(emit=lambda s: None)),
        profiles=types.SimpleNamespace(active=lambda w: None))
    monkeypatch.setattr(ctl_mod, "winutil_profile", lambda c: None)
    monkeypatch.setattr(ctl_mod.time, "sleep", lambda s: None)
    return fake, ran, messages


def test_resume_chain_runs_remaining_parts(monkeypatch):
    """「〇〇して△△して」を分けて実行中に止めたとき、「続き」で残りから実行する。"""
    import time
    from voicectl.controller import Controller
    fake, ran, messages = _fake_controller(
        monkeypatch, last_chain=(["メモ帳開いて", "こんにちはと入力して"], 1, time.monotonic()))
    assert Controller._resume_chain(fake) is True
    assert ran == ["こんにちはと入力して"]              # 実行済みの 1 手目はやり直さない
    assert "再開" in messages[0][1]


def test_resume_chain_without_context_and_when_done(monkeypatch):
    import time
    from voicectl.controller import Controller
    fake, ran, messages = _fake_controller(monkeypatch)
    assert Controller._resume_chain(fake) is True
    assert ran == [] and "続きの手順がありません" in messages[0][1]
    fake, ran, messages = _fake_controller(monkeypatch, last_chain=(["メモ帳開いて"], 1, time.monotonic()))
    assert Controller._resume_chain(fake) is True
    assert ran == [] and "すべて実行済み" in messages[0][1]
    old = time.monotonic() - 9999                          # 時間切れ（5 分）は再開しない
    fake, ran, messages = _fake_controller(monkeypatch, last_chain=(["メモ帳開いて", "入力して"], 1, old))
    assert Controller._resume_chain(fake) is True
    assert ran == [] and "続きの手順がありません" in messages[0][1]


# ---- 続きの指示（「続き」「さっきのページ」「さっきのアプリ」） ----
@pytest.mark.parametrize("text,kind", [
    ("続き", "resume"), ("続きをやって", "resume"), ("残りをお願い", "resume"), ("その続きを実行して", "resume"),
    ("さっきのページ", "page"), ("その動画を開いて", "page"), ("さっきのサイトを見せて", "page"),
    ("さっきの検索結果をもう一度", "page"), ("あの記事を出して", "page"),
    ("さっきのアプリを出して", "app"), ("そのウィンドウに戻して", "app"),
    ("さっきのやつをもう一回", "again"), ("あのやつを実行して", "again"),
    ("それ開いて", "that"), ("これ見せて", "that"),
])
def test_followup_kind(text, kind):
    from voicectl import followup
    assert followup.kind(text) == kind


@pytest.mark.parametrize("text", [
    "そのページを検索して", "さっきの資料を要約して", "続けてどうぞ", "さっきの", "その動画を編集して",
    "これ消して", "メモ帳開いて",
])
def test_followup_kind_ignores_other_commands(text):
    from voicectl import followup
    assert followup.kind(text) is None


def test_followup_prompt_is_not_a_command():
    """「続けてどうぞ」は聞き取り中の案内文と同じだが、命令としては拾わない。"""
    from voicectl import followup
    assert followup.kind("続けてどうぞ。") is None
    assert followup.kind("続き。") == "resume"


# ---- プロファイル（特定アプリの操作） ----
def _write_profile(dir_path: Path, name: str, body: str) -> None:
    (dir_path / name).write_text(body, encoding="utf-8")


def test_profiles_prefer_title_match(tmp_path):
    """同じウィンドウで実行ファイル名とタイトルの両方が一致したら、タイトルのほうを選ぶ（YouTube など）。"""
    from voicectl.profiles import Profiles
    _write_profile(tmp_path, "brave.yaml", "name: Brave\nmatch:\n  exe: [brave]\n")
    _write_profile(tmp_path, "youtube.yaml", "name: YouTube\nmatch:\n  title: [YouTube]\n")
    profs = Profiles(tmp_path)
    win = WindowInfo(1, "猫の動画 - YouTube - Brave", "brave.exe", (0, 0, 100, 100))
    assert profs.active(win).name == "YouTube"
    win2 = WindowInfo(1, "検索結果 - Brave", "brave.exe", (0, 0, 100, 100))
    assert profs.active(win2).name == "Brave"


def test_bundled_profiles_load():
    """profiles/ の同梱プロファイルが読み込めて、手順のキー表記も解釈できること。"""
    from voicectl import appmap
    from voicectl.profiles import Profiles
    profs = Profiles()
    names = {p.name for p in profs.items}
    assert {"PioSolver PioViewer3", "YouTube", "メモ帳", "エクスプローラー", "VLC", "Visual Studio Code"} <= names
    for p in profs.items:
        for cmd in p.commands:
            assert cmd.say, f"{p.name}/{cmd.name} に言い方がありません"
            for step in cmd.steps:
                if "keys" in step:
                    assert appmap.parse_keys(str(step["keys"])), f"{p.name}/{cmd.name}: {step['keys']}"


def test_profile_command_matches_by_say(tmp_path):
    """プロファイルの独自コマンドは、登録した言い方で即実行できる（Jev を通さない）。"""
    from voicectl.profiles import AppCommand
    from voicectl.schema import Candidate as C
    snap = _snap()
    cmd = AppCommand("字幕を切り替え", ["字幕", "字幕つけて"], [{"keys": "c"}])
    lk = _lookup("字幕つけて", snap)
    lk["command"] = {"c0": C("c0", cmd.label, cmd)}
    d = KeywordEngine().decide("字幕つけて", snap, lk, None)
    assert d.action == "app_command" and d.params == {"command": "c0"}
    assert fast_path(d)


# ---- LLM フォールバック：追加した action / param の検証 ----
def test_llm_parse_snap_window():
    lk = _llm_lookup()
    r = LLMFallback()._parse('{"is_pc_command": true, "action": "snap_window", '
                             '"params": {"snap": "left_half"}, "confidence": 0.9}', lk)
    assert r.ok and r.action == "snap_window" and r.params == {"snap": "left_half"}
    bad = LLMFallback()._parse('{"is_pc_command": true, "action": "snap_window", '
                               '"params": {"snap": "真ん中"}, "confidence": 0.9}', lk)
    assert not bad.ok


def test_llm_build_input_includes_session():
    snap = _snap()
    lk = _llm_lookup()
    payload = LLMFallback().build_input("interpret", "その動画を開いて", snap, lk,
                                        session={"last_site": "YouTube", "last_query": "猫", "空": ""})
    assert payload["session"] == {"last_site": "YouTube", "last_query": "猫"}   # 空の値は渡さない
    assert payload["utterance"] == "その動画を開いて"


def test_llm_plan_open_location_and_sites():
    """plan の手順にフォルダを開く手順と追加サイトを書ける。知らない値は捨てる。"""
    lk = _llm_lookup()
    body = ('{"is_pc_command": true, "action": "unknown", "plan": ['
            '{"action": "open_location", "params": {"location": "downloads"}},'
            '{"action": "open_site", "params": {"site": "netflix"}},'
            '{"action": "search_site", "params": {"site": "netflix", "query": "鬼滅の刃"}}],'
            '"confidence": 0.9}')
    r = LLMFallback()._parse(body, lk)
    assert r.ok and [s["action"] for s in r.plan] == ["open_location", "open_site", "search_site"]
    assert r.plan[0]["location"] == "downloads" and r.plan[2]["site"] == "netflix"
    bad = LLMFallback()._parse('{"is_pc_command": true, "action": "unknown", "plan": ['
                               '{"action": "open_location", "params": {"location": "秘密の場所"}}]}', lk)
    assert not bad.ok


# ---- 長い発話が切られないための STT パラメータ ----
def test_stt_long_utterance_params():
    """長い命令が途中で切れないよう、出力の上限を上げ、息継ぎの前後に余白を付ける。"""
    from voicectl.stt import _MAX_NEW_TOKENS, transcribe_kwargs
    kw = transcribe_kwargs("テスト", "メモ帳", "ja", 1)
    assert kw["max_new_tokens"] == _MAX_NEW_TOKENS >= 256    # 64（≒40 文字）だと長い命令の後半が消えていた
    assert kw["vad_parameters"]["min_silence_duration_ms"] >= 400
    assert kw["vad_parameters"]["speech_pad_ms"] >= 200       # 区切りの前後の音を落とさない
    assert kw["temperature"] == 0.0 and kw["no_repeat_ngram_size"] == 3
    assert kw["condition_on_previous_text"] is False          # 前の命令の語尾を引きずらない


def test_llm_gate_takes_long_utterance():
    """長い発話は「PC の命令ではない」と判定されていても LLM に渡す（Jev は長い複文が苦手）。"""
    import types
    from voicectl.controller import Controller
    fake = types.SimpleNamespace(th_confirm=0.5)
    long_text = "メモ帳を開いて今日の予定を書いてから名前を付けて保存しておいてくれるかな"
    d = Decision("unknown", {}, 0.0, engine="jev")
    d.is_pc_command = 0.2
    assert Controller._llm_worth_trying(fake, d, long_text)              # 長い → 渡す
    short = Decision("unknown", {}, 0.0, engine="jev")
    short.is_pc_command = 0.2
    assert not Controller._llm_worth_trying(fake, short, "ありがとう")    # 短い会話 → 渡さない
    cmd = Decision("unknown", {}, 0.0, engine="jev")
    assert Controller._llm_worth_trying(fake, cmd, "右上のボタン押して")  # 命令と分かっていれば短くても渡す
    done = Decision("key_combo", {"key": "copy"}, 0.95, engine="keyword")
    assert not Controller._llm_worth_trying(fake, done, "コピーして")      # 確定済みは渡さない
    already = Decision("unknown", {}, 0.0, engine="llm(bonsai)")
    assert not Controller._llm_worth_trying(fake, already, "よく分からない長い命令ですけど")


def test_llm_cooldown_recovers():
    """失敗が続いても永久には切らず、一定時間後に自動で再開する（llama-server を後から起動しても使える）。"""
    import time
    import types
    from voicectl.controller import Controller
    fake = types.SimpleNamespace(llm=types.SimpleNamespace(failures=0), _llm_off_until=0.0, llm_cooldown=90.0)
    assert Controller._llm_ready(fake)
    fake.llm.failures = 3
    Controller._llm_backoff(fake)
    assert fake._llm_off_until > time.monotonic() and not Controller._llm_ready(fake)   # 休憩中
    fake._llm_off_until = time.monotonic() - 1
    assert Controller._llm_ready(fake) and fake.llm.failures == 0                      # 明けたら再開
    assert not Controller._llm_ready(types.SimpleNamespace(llm=None, _llm_off_until=0.0))


# ---- 学習：使うほど賢くなり、間違えれば忘れる ----
def _learner(tmp_path, **kw):
    from voicectl.learner import Learner
    return Learner(tmp_path / "learned.json", **kw)


def test_learner_remembers_and_reuses(tmp_path):
    """覚えた言い方はそのまま実行でき、表記が揺れても読みの骨格で見つかる。"""
    lr = _learner(tmp_path)
    e = lr.remember("トレーナーを開いて", "key_combo", {"key": "trainer"})
    assert e is not None and lr.lookup("トレーナーを開いて") is e
    assert lr.lookup("とれーなーを開いて") is not None       # 長音の揺れを吸収する
    assert lr.lookup("トレーナー を 開いて") is not None      # 空白の違いは無視する
    assert lr.lookup("まったく違うことを言っています") is None


def test_learner_trust_and_decay(tmp_path):
    """何度も成功した言い方は確認なしに昇格し、失敗が続くと信頼を下げ、最後は忘れる。"""
    lr = _learner(tmp_path, trust_at=3)
    e = lr.remember("お気に入りに追加して", "key_combo", {"key": "bookmark"})
    assert lr.confidence(e) < 0.8                         # 最初は確認を挟む
    for _ in range(3):
        lr.reward(e.key)
    assert e.hits == 3 and lr.confidence(e) >= 0.9        # 昇格：確認なしで実行
    for _ in range(3):
        lr.penalize(e.key, "テスト")
    assert lr.confidence(e) < 0.8                         # 降格：また確認を挟む（まだ忘れない）
    lr.penalize(e.key, "テスト")
    assert e.key not in lr.entries                        # 失敗が成功を上回ったら忘れる


def test_learner_persists_and_forgets(tmp_path):
    """覚えた内容はファイルに残り、次の起動でも使える。忘れさせると消える。"""
    lr = _learner(tmp_path)
    lr.remember("今日の予定を見せて", "key_combo", {"key": "calendar"})
    lr.remember("このアプリを覚えて", "unknown", None,
                plan=[{"action": "key_combo", "params": {"key": "copy"}}])
    again = _learner(tmp_path)                            # 読み込み直し（再起動と同じ）
    assert again.lookup("今日の予定を見せて") is not None
    plan_entry = again.lookup("このアプリを覚えて")
    assert plan_entry.plan and plan_entry.plan[0]["params"]["key"] == "copy"
    assert again.forget("今日の予定を見せて") is not None
    assert _learner(tmp_path).lookup("今日の予定を見せて") is None
    assert _learner(tmp_path).stats()["count"] == 1


def test_learner_app_scoping_and_skip(tmp_path):
    """アプリごとに覚えた言い方は他のアプリで使わない。解釈できていないものは覚えない。"""
    lr = _learner(tmp_path)
    assert lr.remember("これ押して", "unknown", {}) is None        # 決まっていない結果は捨てる
    lr.remember("これ押して", "key_combo", {"key": "enter"}, app="notepad.exe")
    assert lr.lookup("これ押して", app="notepad.exe") is not None
    assert lr.lookup("これ押して", app="chrome.exe") is None       # 別のアプリでは使わない
    assert lr.lookup("これ押して") is None                         # アプリが分からないときも使わない
    lr.remember("これ押して", "key_combo", {"key": "enter"}, app="chrome.exe")   # 別のアプリで覚え直す
    assert lr.lookup("これ押して", app="chrome.exe") is not None   # 覚え直したアプリに移る
    assert lr.lookup("これ押して", app="notepad.exe") is None


def test_learner_suggestions(tmp_path):
    """固定の規則へ移す候補を書き出せる（成長の記録を人が見られる）。"""
    lr = _learner(tmp_path, trust_at=2)
    e = lr.remember("ウィンドウを左に寄せて", "snap_window", {"snap": "left_half"})
    assert lr.suggestions() == []                          # まだ実績が足りない
    for _ in range(3):
        lr.reward(e.key)
    cands = lr.suggestions()
    assert len(cands) == 1 and cands[0].say == "ウィンドウを左に寄せて"
    path = lr.write_suggestions()
    assert path.exists() and "ウィンドウを左に寄せて" in path.read_text(encoding="utf-8")


def test_followup_learn_kinds():
    """「これ覚えて」「忘れて」「何覚えた」を拾い、普通の命令は横取りしない。"""
    from voicectl import followup
    assert followup.kind("これ覚えて") == "learn"
    assert followup.kind("今のを覚えておいて") == "learn"
    assert followup.kind("それ忘れて") == "forget"
    assert followup.kind("何覚えてる？") == "knowledge"
    assert followup.kind("覚えたこと見せて") == "knowledge"
    assert followup.kind("このページを検索して") is None       # 普通の命令はそのまま
    assert followup.kind("さっきのページを開いて") == "page"


# ---- ローカル LLM（bonsai）：宛先の制限と応答の扱い ----
def test_llm_local_endpoint_only_loopback():
    """local backend の宛先はこの PC だけ。外部ホストを指定しても使わない。"""
    from voicectl.engines.llm import LLMFallback as L, local_endpoint
    assert local_endpoint("http://127.0.0.1:8080/v1").endswith("/v1/chat/completions")
    assert local_endpoint("http://localhost:8080/v1").endswith("/v1/chat/completions")
    assert local_endpoint("http://192.168.1.10:8080/v1") is None     # 別の PC
    assert local_endpoint("http://example.com/v1") is None           # 外部
    assert local_endpoint("file:///c:/tmp") is None                  # http 以外
    llm = L(model="bonsai2-27b", backend="local")
    assert llm.label == "bonsai" and llm.backend == "local"


def test_llm_local_answer_is_used(monkeypatch):
    """ローカル LLM の応答（JSON）を検証して実行の判定に使い、呼べなかった回数を数える。"""
    from voicectl.engines.llm import LLMFallback as L
    snap, lk = _snap(), _llm_lookup()
    llm = L(model="bonsai2-27b", backend="local")
    monkeypatch.setattr(L, "_run_local", lambda self, payload: '{"is_pc_command": true, '
                        '"action": "key_combo", "params": {"key": "save"}, "confidence": 0.9}')
    r = llm.ask("interpret", "保存しておいて", snap, lk)
    assert r.ok and r.action == "key_combo" and r.engine == "llm(bonsai)"
    assert llm.failures == 0
    monkeypatch.setattr(L, "_run_local", lambda self, payload: None)
    assert not llm.ask("interpret", "保存しておいて", snap, lk).ok
    assert llm.failures == 1          # controller がこれを見て休憩を決める


# ---- 学習した言い方は Jev も LLM も通さない ----
def _learning_fake(tmp_path, **extra):
    """_handle_text の学習まわりだけを動かすための最小の入れ物。"""
    import types
    from voicectl.controller import Controller
    from voicectl.learner import Learner
    called: list = []
    ui = types.SimpleNamespace(transcript_final=types.SimpleNamespace(emit=lambda *a: None),
                              transcript_partial=types.SimpleNamespace(emit=lambda *a: None),
                              transcript_result=types.SimpleNamespace(emit=lambda *a: None),
                              status=types.SimpleNamespace(emit=lambda *a: None))
    fake = types.SimpleNamespace(
        dictionary={}, _carry=None, ui=ui, pending=None, pending_plan=None, hints=None,
        context=types.SimpleNamespace(collect=lambda **k: None),
        learner=Learner(tmp_path / "learned.json", trust_at=2),
        th_exec=0.8, th_confirm=0.5, previous_lookup={},
        _handle_followup=lambda text: False,
        _run_learned=lambda entry, text, stt_ms: called.append((entry, text)),
    )
    for k, v in extra.items():
        setattr(fake, k, v)
    fake.called = called
    fake._handle_text = Controller._handle_text.__get__(fake)
    return fake


def test_learned_phrase_skips_jev_and_llm(tmp_path, monkeypatch):
    """一度覚えた言い方は、Jev にも LLM にも渡さずその場で実行する（表記が揺れても効く）。"""
    import voicectl.controller as ctl
    from voicectl.winutil import WindowInfo
    fake = _learning_fake(tmp_path)
    fake.learner.remember("トレーナーを開いて", "key_combo", {"key": "trainer"}, app="notepad.exe")
    monkeypatch.setattr(ctl.winutil, "foreground_window",
                        lambda: WindowInfo(1, "無題 - メモ帳", "notepad.exe", (0, 0, 100, 100)))
    fake._handle_text("とれーなーを開いて")            # 読みが同じ別表記
    assert len(fake.called) == 1
    assert fake.called[0][0].say == "トレーナーを開いて"
    # 知らない言い方は学習を通らない（この先の Jev の判定へ進む）
    assert fake.learner.lookup("覚えていない命令です", "notepad.exe") is None
    assert len(fake.called) == 1


def test_run_learned_replays_plan_without_display(tmp_path):
    """覚えた手順（plan）は、確認なしで・表示だけに頼らずそのまま再生できる。"""
    import types
    from voicectl.controller import Controller
    from voicectl.learner import Learner
    lr = Learner(tmp_path / "learned.json", trust_at=2)
    e = lr.remember("予定を見せて", "unknown", None,
                    plan=[{"action": "key_combo", "params": {"key": "calendar"}}], source="explicit")
    for _ in range(2):
        lr.reward(e.key)                                   # 2 回成功 → 確認なしに昇格
    done: list = []
    logging: list = []
    fake = types.SimpleNamespace(
        learner=lr, th_exec=0.8, th_confirm=0.5, previous_lookup={},
        ui=types.SimpleNamespace(status=types.SimpleNamespace(emit=lambda *a: done.append(a[1])),
                                 transcript_result=types.SimpleNamespace(emit=lambda *a: None)),
        _learned_key=None,
    )
    fake._log = lambda text, d, desc, stt_ms, snap=None: logging.append((d.engine, d.action, d.confidence))
    fake._exec_plan = lambda steps, lookup, text, hwnd=0, start=0: done.append(("plan", len(steps)))
    fake._act = lambda d, lookup, text, snap, stt_ms: done.append(("act", d.action))
    Controller._run_learned(fake, e, "予定を見せて", 12.0)
    assert logging == [("learned", "unknown", 0.95)]        # engine は learned（LLM ではない）
    assert ("plan", 1) in done and not any(d[0] == "act" for d in done if isinstance(d, tuple))
    assert fake._learned_key == e.key                       # 「忘れて」で消せるように覚えているdef test_corner_position_inside_work_area():
    """右下の表示はタスクバーを除いた作業領域の中に収まる（48px の決め打ちをやめた）。"""
    from voicectl.winutil import corner_position
    work = (0, 0, 1920, 1032)          # タスクバーが 48px の画面
    x, y = corner_position(work, 460, 120, "bottom_right", 16)
    assert (x, y) == (1920 - 460 - 16, 1032 - 120 - 16)
    assert y + 120 <= work[3]          # タスクバーに重ならない
    tall = (0, 0, 1920, 1080 - 96)     # タスクバーが 2 段（96px）でも同じ計算で収まる
    x, y = corner_position(tall, 460, 120, "bottom_right", 16)
    assert y + 120 <= tall[3]
    assert corner_position(work, 460, 120, "top_left", 16) == (16, 16)
    # 画面より表示が大きいときは、はみ出さない範囲で左上に寄せる
    assert corner_position((0, 0, 400, 300), 460, 120, "bottom_right", 16) == (0, 164)
    assert corner_position((0, 0, 400, 300), 460, 120, "top_right", 16) == (0, 16)
    x, y = corner_position((0, 0, 1920, 1080), 460, 120, "bottom_right", 16)           # 自動的に隠す設定
    assert y + 120 <= 1080


# ---- 2026-09-22 改善ループ（自分の表示の除外・未インストールのサイト・言い直し・全面表示） ----

@pytest.mark.parametrize("text", ["全面に表示して", "全面に表示してくれ", "全面表示"])
def test_keyword_zenmen_is_maximize(text):
    snap = _snap()
    d = KeywordEngine().decide(text, snap, _lookup(text, snap), None)
    assert d.action == "window_control" and d.params.get("window_action") == "maximize"


def test_drop_own_overlay_elements():
    """voicectl 自身の表示（文字起こしバー）の上にある OCR 文字は画面の要素から外す。"""
    from voicectl.context import drop_own
    from voicectl.uia import ScreenElement
    bar = ScreenElement("手順 1: Click", "画面上の文字", (1700, 1000, 1900, 1030), "ocr", 0)
    btn = ScreenElement("保存", "ボタン", (100, 100, 160, 130), "uia", 0)
    assert drop_own([bar, btn], [(1650, 990, 1910, 1040)]) == [btn]
    assert drop_own([bar, btn], []) == [bar, btn]


@pytest.mark.parametrize("text,kind,site", [
    ("X開いて。", "open_site", "x"),
    ("Notionを開いて", "open_site", "notion"),
    ("ノーションを開いて", "open_site", "notion"),
    ("ヨドバシのサイト開いて", "open_web", "ヨドバシ"),
    ("ボートレースという公式サイト開いて", "open_web", "ボートレース"),
    ("楽天の公式サイトを開いて", "open_site", "rakuten"),
])
def test_tasks_unknown_sites(text, kind, site):
    from voicectl import tasks
    t = tasks.parse(text)
    assert t is not None and t.kind == kind and t.site == site


def test_web_guess_skips_screen_words():
    from voicectl import tasks
    assert tasks.web_guess("ファイルを開いて") is None
    assert tasks.web_guess("メモ帳を開いて") is None
    g = tasks.web_guess("BraveでPerplexity開いて")
    assert g is not None and g.kind == "open_web" and g.query == "Perplexity" and g.browser == "brave.exe"


def test_correction_reopens_previous_command():
    import time
    from voicectl.controller import _correction
    now = time.monotonic()
    assert _correction("アプリはObsidianです。", ("オブシリアン開いて全面に表示して。", now)) \
        == "Obsidianを開いて、全面に表示して"
    assert _correction("Notionです", ("Brave で ノーション開いて", now)) == "BraveでNotionを開いて"
    assert _correction("そうです", ("X開いて", now)) is None
    assert _correction("違うよ", ("X開いて", now)) is None
    assert _correction("Xです", ("X開いて", now - 100)) is None     # 時間が経っていたら言い直しとみなさない
    assert _correction("Xです", None) is None


def test_agent_skips_own_echo_text():
    """エージェントの選択肢に、自分の表示（「目的:…」や発話の文字）を OCR で読んだものを出さない。"""
    import types
    from voicectl.agent import JevAgent
    from voicectl.candidates import Candidate
    from voicectl.uia import ScreenElement
    els = {
        "e1": ScreenElement("飛車先を伸はして。", "画面上の文字", (0, 0, 10, 10), "ocr", 0),
        "e2": ScreenElement("目的:飛車先を伸ばして。", "画面上の文字", (0, 0, 10, 10), "ocr", 0),
        "e3": ScreenElement("YouTube", "画面上の文字", (0, 0, 10, 10), "ocr", 0),
        "e4": ScreenElement("検討", "ボタン", (0, 0, 10, 10), "uia", 0),
    }
    lookup = {"element": {k: Candidate(k, v.name, v) for k, v in els.items()}}
    ag = JevAgent(types.SimpleNamespace(confirm_words=[]))
    opts = ag._options("飛車先を伸ばして。", None, lookup)
    assert "click:e3" in opts and "click:e4" in opts
    assert "click:e1" not in opts and "click:e2" not in opts


@pytest.mark.parametrize("text,site,q,title", [
    ("X内の検索でMiniMaxを検索して。", "x", "MiniMax", ""),
    ("XでMiniMaxを検索して", "x", "MiniMax", ""),
    ("楽天の中で掃除機を探して", "rakuten", "掃除機", ""),
    ("ここでMiniMaxを検索して", "x", "MiniMax", "ホーム / X - Brave"),
    ("ここで掃除機を探して", "amazon", "掃除機", "Amazon.co.jp - Google Chrome"),
    ("ここで猫を検索して", "google", "猫", "無題 - メモ帳"),
])
def test_tasks_site_search(text, site, q, title):
    from voicectl import tasks
    t = tasks.parse(text, title)
    assert t is not None and t.kind == "search" and t.site == site and t.query == q


def test_same_name_button_beats_launch():
    """「設定を開いて」で画面に「設定」ボタンがあれば、Jev が迷って別アプリを選んでもボタンを押す。"""
    from voicectl.controller import _element_named
    from voicectl.candidates import Candidate
    from voicectl.uia import ScreenElement
    lk = {"element": {"e1": Candidate("e1", "設定", ScreenElement("設定", "ボタン", (0, 0, 10, 10), "uia", 1))}}
    assert _element_named("設定を開いて", lk) == "e1"
    assert _element_named("保存を押して", lk) is None


@pytest.mark.parametrize("text,parts", [
    ("保存して閉じて", ["保存して", "閉じて"]),
    ("コピーしてメモ帳に貼り付けて", ["コピーして", "メモ帳に貼り付けて"]),
    ("スクショ撮って保存して", ["スクショ撮って", "保存して"]),
    ("保存ボタンを押してください", ["保存ボタンを押してください"]),
    ("閉じてね", ["閉じてね"]),
])
def test_split_chain_action_te(text, parts):
    from voicectl.controller import _split_chain
    assert _split_chain(text) == parts


def test_habit_suggests_routine_after_three_repeats():
    """同じ 2 手の流れを 3 回言ったら、ルーチンにまとめることを提案する（1 回だけ）。"""
    import types
    from voicectl.controller import Controller
    shown = []
    fake = types.SimpleNamespace(ui=types.SimpleNamespace(answer=types.SimpleNamespace(emit=lambda *a: shown.append(a))),
                                 speaker=types.SimpleNamespace(say=lambda t: None))
    for _ in range(3):
        for s in ("メモ帳を開いて", "左半分にして"):
            Controller._watch_habit(fake, s)
    assert len(shown) == 1 and "メモ帳を開いて" in shown[0][1]
    assert fake._suggest[0] == ["メモ帳を開いて", "左半分にして"]
    for s in ("メモ帳を開いて", "左半分にして"):
        Controller._watch_habit(fake, s)
    assert len(shown) == 1   # 同じ提案は繰り返さない


# ---- ウェイクワード（待機中の呼びかけで聞き取りを始める・ハンズフリー） ----
def test_wake_match():
    from voicectl import wake as wake_mod
    # 呼びかけだけ → セッション開始。呼びかけと一緒に言った文はそのまま命令になる
    assert wake_mod.match("コンピューター", ["コンピューター"]) == ("コンピューター", "")
    assert wake_mod.match("コンピューター。", ["コンピューター"]) == ("コンピューター", "")
    assert wake_mod.match("コンピューター音量30にして", ["コンピューター"]) == ("コンピューター", "音量30にして")
    assert wake_mod.match("コンピューター、メモ帳開いて", ["コンピューター"]) == ("コンピューター", "メモ帳開いて")
    assert wake_mod.match("こんぴゅーたー", ["コンピューター"]) == ("コンピューター", "")  # 表記ゆれは読みで照合
    # ウェイクワードを含まない発話には反応しない
    for t in ["音量30にして", "メモ帳開いて", "", "ちょっと待って", "パソコンの調子はどう"]:
        assert wake_mod.match(t, ["コンピューター"]) is None


def test_wake_session_auto_end():
    """ウェイクワードで始めた聞き取りは、無音が続くと自動で待機に戻る（実機なしで状態遷移だけ確認）。"""
    import queue
    import time as _time
    import types

    import numpy as np
    from voicectl.controller import Controller

    started = []
    rec = types.SimpleNamespace(start=lambda: started.append(1), stop=lambda: np.zeros(0, dtype=np.float32))
    fake = types.SimpleNamespace(
        paused=False, _listening=False, _dictating=False, recorder=rec, _active=None,
        _remote_hotkey=False, _continuous=False, _seg_speech=False, _segments_done=0, _press_t=0.0,
        _wake_session=False, _wake_idle_sec=12.0, _wake_last=0.0, pending=None, pending_plan=None,
        _q=queue.Queue(),
        ui=types.SimpleNamespace(idle=types.SimpleNamespace(emit=lambda *a: None),
                                  status=types.SimpleNamespace(emit=lambda *a: None)))
    fake._end_listening = types.MethodType(Controller._end_listening, fake)
    Controller.wake_session_start(fake, idle_sec=12.0)
    fake._wake_idle_sec = 0.3   # 実機の下限（3 秒）をテスト用に短くする
    assert started and fake._listening and fake._continuous and fake._wake_session
    assert fake._q.get_nowait()[0] == "wake_begin"
    Controller._check_timeout(fake)   # まだ話した直後なので終わらない
    assert fake._listening
    _time.sleep(0.5)
    Controller._check_timeout(fake)   # 無音が続いたので待機に戻る
    assert not fake._listening and not fake._wake_session
    assert fake._q.get_nowait()[0] == "audio"
