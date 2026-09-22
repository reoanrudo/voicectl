"""決まった形の命令（intents.py）の解析のテスト。実行（音量・ファイル・ウィンドウ）はしない。"""
import pytest

from voicectl import intents


@pytest.mark.parametrize("text,kind,args", [
    ("音量30", "volume_set", {"level": 30}),
    ("音量を三十にして", "volume_set", {"level": 30}),
    ("音量最大", "volume_set", {"level": 100}),
    ("音量半分にして", "volume_set", {"level": 50}),
    ("音量10上げて", "volume_add", {"delta": 10}),
    ("音量20下げて", "volume_add", {"delta": -20}),
    ("音量いくつ", "volume_get", {}),
    ("3番目のタブ", "tab", {"n": 3}),
    ("タブ2に移動", "tab", {"n": 2}),
    ("最後のタブ", "tab", {"n": 9}),
    ("10秒戻して", "seek", {"seconds": -10}),
    ("30秒進めて", "seek", {"seconds": 30}),
    ("ページ内で料金を探して", "find_in_page", {"q": "料金"}),
    ("見積書というファイルを開いて", "open_file", {"name": "見積書", "folder": False}),
    ("GTOってフォルダ開いて", "open_file", {"name": "GTO", "folder": True}),
    ("最近使ったExcel開いて", "recent_file", {"exts": [".xlsx", ".xls", ".xlsm", ".csv"]}),
    ("左にChrome右にObsidian", "arrange", {"left": "Chrome", "right": "Obsidian"}),
    ("右にメモ帳左にChrome", "arrange", {"left": "Chrome", "right": "メモ帳"}),
    ("ChromeとObsidianを並べて", "arrange", {"left": "Chrome", "right": "Obsidian"}),
    # 2026-09-22 追加（3 回目）：上下に並べる
    ("Chromeを上Obsidianを下に並べて", "arrange", {"left": "Chrome", "right": "Obsidian", "vertical": True}),
    ("上にChrome下にメモ帳", "arrange", {"left": "Chrome", "right": "メモ帳", "vertical": True}),
    ("下にObsidian上にChromeを並べて", "arrange", {"left": "Chrome", "right": "Obsidian", "vertical": True}),
    ("ChromeとObsidianを上下に並べて", "arrange", {"left": "Chrome", "right": "Obsidian", "vertical": True}),
    ("メモ帳と電卓を縦に並べて", "arrange", {"left": "メモ帳", "right": "電卓", "vertical": True}),
    ("Discordを閉じて", "window_named", {"name": "Discord", "action": "close"}),
    ("Chromeを最小化して", "window_named", {"name": "Chrome", "action": "minimize"}),
    ("今何時", "say_time", {}),
    ("今日何曜日", "say_date", {}),
    ("123かける45は", "calc", {"expr": "123*45"}),
    ("百たす二十", "calc", {"expr": "100+20"}),
    ("5分後に知らせて", "timer", {"seconds": 300}),
    ("タイマー3分", "timer", {"seconds": 180}),
    ("1分半たったら教えて", "timer", {"seconds": 90}),
    ("選択したところ読んで", "read_selection", {}),
    ("書き取り開始", "dictation_on", {}),
    ("作業開始と言ったらObsidianを開いてChromeを開いて", "routine_add",
     {"name": "作業開始", "body": "Obsidianを開いてChromeを開いて"}),
    ("作業開始のルーチン消して", "routine_del", {"name": "作業開始"}),
    # 2026-09-22 追加（2 回目）
    ("これで検索して", "search_selection", {}),
    ("これをググって", "search_selection", {}),
    ("選択したところで検索して", "search_selection", {}),
    ("この行を選んで", "line_select", {}),
    ("行を選択して", "line_select", {}),
    ("行を消して", "line_delete", {}),
    ("一行削除して", "line_delete", {}),
    ("メモっておいて牛乳を買う", "memo_add", {"q": "牛乳を買う"}),
    ("メモしておいて明日9時に薬", "memo_add", {"q": "明日9時に薬"}),
    ("牛乳を買うってメモして", "memo_add", {"q": "牛乳を買う"}),
    ("メモ見せて", "memo_list", {}),
    ("メモを読んで", "memo_read", {}),
    ("メモ全部消して", "memo_clear", {}),
    ("さっきダウンロードしたのを開いて", "open_latest_download", {}),
    ("ダウンロードしたファイル開いて", "open_latest_download", {}),
    ("最新のダウンロードを開いて", "open_latest_download", {}),
    ("さっき落としたやつ開いて", "open_latest_download", {}),
])
def test_parse(text, kind, args):
    it = intents.parse(text)
    assert it is not None and it.kind == kind
    assert it.args == args


@pytest.mark.parametrize("text", ["メモ帳を開いて", "タブを閉じて", "閉じて", "コピーして", "YouTubeを開いて",
                                  "音量上げて", "3回戻って", "これを閉じて", "下にスクロール",
                                  "ダウンロードを開いて", "メモしよう"])
def test_not_intent(text):
    assert intents.parse(text) is None


def test_memo_store(tmp_path, monkeypatch):
    """メモの追記・一覧・全消去（state/memos.txt を一時ファイルに差し替えて確認）。"""
    f = tmp_path / "memos.txt"
    monkeypatch.setattr(intents, "MEMOS", f)
    ts = intents.memo_add("牛乳を買う")
    intents.memo_add("12時に戸締まり")
    lines = intents.memo_lines()
    assert len(lines) == 2 and ts in lines[0] and "牛乳を買う" in lines[0]
    assert intents.memo_clear() == 2
    assert intents.memo_lines() == [] and not f.exists()


def test_routine_by_name():
    assert intents.parse("作業開始", {"作業開始": ["a"]}).args == {"name": "作業開始"}
    assert intents.parse("作業開始して", {"作業開始": ["a"]}).args == {"name": "作業開始"}
    assert intents.parse("作業終了", {"作業開始": ["a"]}) is None


def test_count_prefix():
    assert intents.count_prefix("3回戻って") == (3, "戻って")
    assert intents.count_prefix("下に5回スクロール") == (5, "下にスクロール")
    assert intents.count_prefix("エンター2回押して") == (2, "エンター押して")
    assert intents.count_prefix("もう1回") is None
    assert intents.count_prefix("100回戻って") is None


def test_dictation_end_and_calc():
    assert intents.dictation_end("書き取り終わり") and intents.dictation_end("終了")
    assert not intents.dictation_end("会議は終わりました")
    assert intents.calc("123*45") == "5,535"
    assert intents.calc("10/4") == "2.5"
    assert intents.calc("1+2*3") == "7"           # 演算子の優先順位も守る
    assert intents.calc("(1+2)*3") == "9"
    with pytest.raises(ValueError):
        intents.calc("__import__('os')")
    with pytest.raises(ValueError):
        intents.calc("2**999999")                  # べき乗は使わない（重い計算の防止）
    with pytest.raises(ValueError):
        intents.calc("9" * 100)                    # 長すぎる式も拒否
    with pytest.raises(ValueError):
        intents.calc("1/0")


def test_kanji_num():
    assert [intents.kanji_num(x) for x in ["百", "二十", "百二十五", "千五百", "二〇二六"]] == [100, 20, 125, 1500, 2026]


def test_alarm_target_and_seconds():
    """標準アプリに渡す目標時刻と、内部タイマー代用の秒数が一致すること。"""
    import datetime as dt
    now = dt.datetime(2026, 9, 22, 22, 50, 30)
    # 「23時15分」→ 今日の 23:15
    t = intents.alarm_target(23, 15, False, now)
    assert (t.hour, t.minute, t.day) == (23, 15, 22)
    # 「7時」→ 近い未来の 7:00（翌朝）
    t = intents.alarm_target(7, 0, False, now)
    assert (t.hour, t.day) == (7, 23)
    # 「午後3時」→ 明日の 15:00（今日の 15:00 は過去）
    t = intents.alarm_target(3, 0, True, now)
    assert t.hour == 15 and t.day == 23
    # 秒数は目標時刻との差と一致する（22:50:30 → 23:15:00 は 24 分 30 秒）
    assert intents.alarm_seconds(23, 15, False, now) == int((intents.alarm_target(23, 15, False, now) - now).total_seconds())
    assert intents.alarm_seconds(23, 15, False, now) == 1470


def test_picker_clicks():
    """時刻ピッカーを最短の向きで回す回数。"""
    assert intents._picker_clicks(7, 8, 24, up=True) == 1
    assert intents._picker_clicks(7, 8, 24, up=False) == 23
    assert intents._picker_clicks(7, 45, 60, up=True) == 38
    assert intents._picker_clicks(7, 45, 60, up=False) == 22   # 下回しのほうが近い
    assert intents._picker_clicks(30, 30, 60, up=True) == 0


# ---- 2026-09-22 追加（8 回目）：PC 操作の便利機能 ----
@pytest.mark.parametrize("text,kind,args", [
    ("これをzipに", "zip_selection", {}),
    ("これを圧縮して", "zip_selection", {}),
    ("選んだファイルをzipにして", "zip_selection", {}),
    ("zipを展開して", "unzip_selection", {}),
    ("ジップをここに解凍して", "unzip_selection", {}),
    ("チェックサム見せて", "checksum_selection", {}),
    ("選択したファイルのハッシュ教えて", "checksum_selection", {}),
    ("画面を撮ってコピー", "shot_clipboard", {}),
    ("スクショをクリップボードに", "shot_clipboard", {}),
    ("Chromeの音量だけ30", "app_volume", {"app": "Chrome", "op": "level", "level": 30}),
    ("Chromeの音量を30パーセントに", "app_volume", {"app": "Chrome", "op": "level", "level": 30}),
    ("ブラウザの音だけ下げて", "app_volume", {"app": "ブラウザ", "op": "下げて"}),
    ("Discordの音消して", "app_volume", {"app": "Discord", "op": "消して"}),
    ("VLCをミュート", "app_volume", {"app": "VLC", "op": "ミュート"}),
    ("メモリ食ってるの見せて", "proc_top", {"by": "memory"}),
    ("CPU使ってるの教えて", "proc_top", {"by": "cpu"}),
    ("重いの見つけて", "proc_top", {"by": "memory"}),
])
def test_parse_pc_convenience(text, kind, args):
    it = intents.parse(text)
    assert it is not None and it.kind == kind, text
    assert it.args == args, text


def test_layout_route():
    """「開発の配置」は config の layouts にある名前のときだけ配置になる。"""
    layouts = {"開発": [{"app": "ZCode", "place": "left_half"}]}
    it = intents.parse("開発の配置", layouts=layouts)
    assert it is not None and it.kind == "layout" and it.args == {"name": "開発"}
    assert intents.parse("開発の配置", layouts=None) is None   # 未登録の名前は配置にしない
    assert intents.parse("配置一覧").kind == "layout_list"


@pytest.mark.parametrize("text", ["音量30", "ミュート", "画面を撮って", "コピーして", "zipファイル開いて"])
def test_pc_convenience_not_stolen(text):
    """既存の言い方を新しい機能が奪わないこと（「音量30」は全体の音量のまま）。"""
    it = intents.parse(text)
    if text == "音量30":
        assert it.kind == "volume_set"
    else:
        assert it is None, text


def test_zip_roundtrip(tmp_path):
    """zip へのまとめ → 展開がそのまま動くこと（名前の重複は -2 で避ける）。zip は選んだフォルダの隣に作る。"""
    d1 = tmp_path / "資料"
    d1.mkdir()
    (d1 / "a.txt").write_text("ほんぶん", encoding="utf-8")
    sub = d1 / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("サブ", encoding="utf-8")
    msg = intents.zip_files([str(d1)])
    zp = tmp_path / "資料.zip"
    assert zp.exists() and "資料.zip" in msg
    # 同名は上書きしない
    intents.zip_files([str(d1)])
    assert (tmp_path / "資料-2.zip").exists()
    msg2 = intents.unzip_file(str(zp))
    target = tmp_path / "資料-2"   # 元のフォルダがあるのでずらして展開される
    assert target.exists() and (target / "資料" / "a.txt").read_text(encoding="utf-8") == "ほんぶん"
    assert (target / "資料" / "sub" / "b.txt").exists()
    assert "展開しました" in msg2


def test_sha256_file(tmp_path):
    import hashlib
    f = tmp_path / "data.bin"
    f.write_bytes(b"voicectl")
    assert intents.sha256_file(str(f)) == hashlib.sha256(b"voicectl").hexdigest()


def test_app_volume_match():
    assert intents.app_volume_match("Chrome", "chrome.exe")
    assert intents.app_volume_match("クローム", "chrome.exe")
    assert intents.app_volume_match("ブラウザ", "brave.exe")   # ブラウザは複数の名前を受け付ける
    assert not intents.app_volume_match("VLC", "chrome.exe")


def test_routine_fuzzy_reading():
    """「さぎょうかいし」のようにかなで認識されても、読みが同じなら同じルーチン。"""
    r = {"作業開始": ["a"]}
    it = intents.parse("さぎょうかいし", r)
    assert it is not None and it.args == {"name": "作業開始"}


@pytest.mark.parametrize("text,kind,name", [
    ("ログインの手順を記録して", "demo_start", "ログイン"),
    ("朝の準備として記録開始", "demo_start", "朝の準備"),
    ("記録開始", "demo_start", ""),
    ("記録終了", "demo_stop", None),
    ("記録止めて", "demo_stop", None),
    ("記録取り消し", "demo_cancel", None),
])
def test_demo_intents(text, kind, name):
    it = intents.parse(text)
    assert it is not None and it.kind == kind
    if name is not None:
        assert it.args["name"] == name


def test_demo_describe_and_agent_replay_step():
    """エージェントが実行した手を、再生できる形（demo.replay_step が読む形）に直せる。"""
    import types
    from voicectl import demo
    from voicectl.agent import JevAgent
    from voicectl.candidates import Candidate
    from voicectl.uia import ScreenElement
    from voicectl.winutil import WindowInfo
    snap = types.SimpleNamespace(window=WindowInfo(1, "無題 - メモ帳", "Notepad.exe", (0, 0, 1000, 500)))
    el = ScreenElement("保存", "ボタン", (100, 100, 200, 150), "uia", 1, auto_id="save")
    st = JevAgent._replay_step("click:e1", Candidate("e1", "保存", el), snap)
    assert st["click"]["proc"] == "notepad.exe" and st["click"]["name"] == "保存"
    assert st["click"]["rel"] == [0.15, 0.25]
    assert JevAgent._replay_step("type:0", "こんにちは", snap) == {"type": "こんにちは"}
    assert JevAgent._replay_step("key:enter", None, snap) == {"keys": [0x0D]}
    assert JevAgent._replay_step("site:youtube", "youtube", snap) == {"url": "https://www.youtube.com/"}
    assert demo.describe_step(st) == "notepad の「保存」をクリック"
    assert demo.describe_step("メモ帳を開いて") == "「メモ帳を開いて」"


@pytest.mark.parametrize("text", ["ファイルを開いて", "ファイルメニューを開いて", "フォルダを開いて", "設定を開いて",
                                  "新しいタブを開いて", "このタブを閉じて", "5秒待って", "2番目のリンクを開いて"])
def test_app_commands_are_not_intents(text):
    """アプリ内の操作（メニュー・タブなど）を決まった形の命令として横取りしない。"""
    assert intents.parse(text) is None


@pytest.mark.parametrize("text,kind,args", [
    ("3つ前のタブに戻って", "tab_rel", {"n": -3}),
    ("2つ右のタブ", "tab_rel", {"n": 2}),
    ("Wi-Fiの設定開いて", "settings_page", {"page": "network-wifi", "label": "Wi-Fi"}),
    ("Bluetoothをオフにして", "settings_page", {"page": "bluetooth", "label": "Bluetooth"}),
    ("明るさを下げて", "settings_page", {"page": "display", "label": "ディスプレイ"}),
    ("パソコンをシャットダウンして", "power", {"kind": "shutdown"}),
    ("再起動して", "power", {"kind": "restart"}),
    ("スリープして", "power", {"kind": "sleep"}),
    ("3時に起こして", "alarm", {"hour": 3, "minute": 0, "pm": False}),
    ("午後7時半に知らせて", "alarm", {"hour": 7, "minute": 30, "pm": True}),
    ("うるさい", "hush", {}),
])
def test_parse_round2(text, kind, args):
    it = intents.parse(text)
    assert it is not None and it.kind == kind and it.args == args


@pytest.mark.parametrize("text", ["電気を消して", "切って", "音を消して"])
def test_not_power_or_settings(text):
    it = intents.parse(text)
    assert it is None or it.kind not in ("power", "settings_page")


def test_alarm_seconds_picks_nearest_future():
    import datetime as dt
    now = dt.datetime(2026, 9, 22, 14, 0)
    assert intents.alarm_seconds(3, 0, False, now) == 3600          # 3時 → 15:00（近いほう）
    assert intents.alarm_seconds(15, 0, False, now) == 3600
    assert intents.alarm_seconds(7, 30, True, now) == 5 * 3600 + 1800   # 午後7時半
    assert intents.alarm_seconds(13, 0, False, now) == 23 * 3600        # 過ぎていたら翌日


def test_count_prefix_single_char():
    assert intents.count_prefix("10回右") == (10, "右")


def test_paste_to():
    assert intents.parse("メモ帳に貼り付けて").args == {"name": "メモ帳"}
    assert intents.parse("ここに貼り付けて") is None


@pytest.mark.parametrize("text,kind,task", [
    ("明日の会議を15時に変更するお願いメールを書いて", "ai_write", None),
    ("選択したところを要約して", "ai_selection", "summarize"),
    ("これを英語にして", "ai_selection", "translate_en"),
    ("この文章を丁寧にして", "ai_selection", "polite"),
    ("これを直して", "ai_selection", "proofread"),
    ("これを箇条書きにして", "ai_selection", "bullets"),
    ("これに返信を書いて", "ai_selection", "reply"),
    ("この画面を要約して", "ai_screen", None),
    ("ここ何て書いてある？", "ai_screen", None),
    ("量子コンピュータって何？", "ai_ask", None),
    ("Pythonのリストについて教えて", "ai_ask", None),
    ("今日何した？", "ai_recap", None),
    ("朝の準備として登録して", "routine_from_suggest", None),
])
def test_ai_intents(text, kind, task):
    it = intents.parse(text)
    assert it is not None and it.kind == kind
    if task:
        assert it.args["task"] == task


@pytest.mark.parametrize("text", ["5分後に教えて", "3時に教えて", "こんにちはって書いて", "何ができる？", "選択したところ読んで"])
def test_ai_does_not_steal(text):
    it = intents.parse(text)
    assert it is None or not it.kind.startswith("ai_")


def test_draft_replies():
    assert intents.is_draft_accept("入力して") and intents.is_draft_accept("それでお願い")
    assert intents.is_draft_refine("もっと短く") and intents.is_draft_refine("敬語にして")
    assert intents.is_draft_cancel("いらない") and intents.is_draft_cancel("やっぱりやめて")
    assert not intents.is_draft_accept("メモ帳開いて")


@pytest.mark.parametrize("text,kind,args", [
    ("ゆっくり右に", "glide", {"direction": "right", "slow": True}),
    ("右にずっと", "glide", {"direction": "right", "slow": False}),
    ("一つ上のフォルダ", "explorer", {"op": "up"}),
    ("新しい順に並べて", "explorer", {"op": "sort", "by": "date", "desc": True}),
    ("3番目のファイルを開いて", "explorer", {"op": "open_nth", "n": 3}),
    ("これをデスクトップにコピー", "explorer", {"op": "copy", "dest": "shell:Desktop", "dest_name": "デスクトップ"}),
    ("選んだファイルをダウンロードに移動して", "explorer", {"op": "move", "dest": "shell:Downloads", "dest_name": "ダウンロード"}),
    ("パスをコピー", "explorer", {"op": "copy_path"}),
    ("ここでターミナル開いて", "explorer", {"op": "terminal"}),
])
def test_windows_direct_intents(text, kind, args):
    it = intents.parse(text)
    assert it is not None and it.kind == kind and it.args == args


def test_glide_stop_words():
    assert intents.is_glide_stop("そこ") == (True, False)
    assert intents.is_glide_stop("そこでクリック") == (True, True)
    assert intents.is_glide_stop("速く") == (False, False)


@pytest.mark.parametrize("text,buttons,target", [
    ("はい", ["はい(Y)", "いいえ(N)"], "はい(Y)"),
    ("いいよ", ["OK", "キャンセル"], "OK"),
    ("保存しない", ["保存する(S)", "保存しない(N)", "キャンセル"], "保存しない(N)"),
    ("閉じて", ["保存(S)", "保存しない(N)", "キャンセル"], "キャンセル"),
    ("次へ", ["< 戻る(B)", "次へ(N) >", "キャンセル"], "次へ(N) >"),
    ("置き換えて", ["ファイルを置き換える(R)", "ファイルをスキップする(S)"], "ファイルを置き換える(R)"),
    ("メモ帳開いて", ["はい", "いいえ"], None),
])
def test_dialog_target(text, buttons, target):
    from voicectl import winctx
    assert winctx.dialog_target(text, buttons) == target
