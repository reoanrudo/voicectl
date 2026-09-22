"""キーワード照合による判定。役割は2つ：
1. 高速経路：「右」「クリック」「下にスクロール」など定型の短い命令を、Jev を待たずに即処理する
2. 予備：Jev が使えない（通信エラー等）ときの代わり
"""
from __future__ import annotations

import re

from .. import textparse
from ..candidates import Lookup
from ..context import Snapshot
from ..schema import Decision
from .base import DecisionEngine

_DIR_WORDS = [  # 長いものから照合
    ("右上", "up_right"), ("左上", "up_left"), ("右下", "down_right"), ("左下", "down_left"),
    ("みぎうえ", "up_right"), ("ひだりうえ", "up_left"), ("みぎした", "down_right"), ("ひだりした", "down_left"),
    ("右", "right"), ("左", "left"), ("上", "up"), ("下", "down"),
    ("みぎ", "right"), ("ひだり", "left"), ("うえ", "up"), ("した", "down"),
]
_SMALL = ("ちょっと", "ちょい", "少し", "すこし", "もうちょい", "もうちょっと", "もう少し", "わずかに")
_LARGE = ("大きく", "おおきく", "もっと", "いっぱい", "たくさん", "ずっと", "端まで", "かなり", "思いっきり")
_FILLER = r"(に|へ|の方|のほう|方向|移動|動かして|動かす|動いて|行って|いって|寄せて|ずらして|だけ|まで|ね|よ|お願い|して|しろ)*"

_KEY_WORDS = {
    "コピー": "copy", "貼り付け": "paste", "貼りつけ": "paste", "はりつけ": "paste", "ペースト": "paste",
    "切り取り": "cut", "カット": "cut", "元に戻す": "undo", "もとにもどす": "undo", "アンドゥ": "undo",
    "やり直し": "redo", "リドゥ": "redo", "全選択": "select_all", "全部選択": "select_all", "すべて選択": "select_all",
    "保存": "save", "セーブ": "save", "検索": "find", "エンター": "enter", "決定": "enter", "改行": "enter",
    "エスケープ": "escape", "タブ": "tab", "バックスペース": "backspace", "一文字消して": "backspace",
    "削除": "delete", "デリート": "delete", "スペース": "space", "新しいタブ": "new_tab", "タブを閉じ": "close_tab",
    "次のタブ": "next_tab", "前のタブ": "prev_tab", "更新": "reload", "再読み込み": "reload", "リロード": "reload",
    "戻る": "back", "進む": "forward", "拡大": "zoom_in", "縮小": "zoom_out", "スタート": "start_menu",
    "スタートメニュー": "start_menu", "タスクビュー": "task_view", "スクショ": "screenshot",
    "スクリーンショット": "screenshot", "スクリーンショットして": "screenshot",
    "スクリーンショットしてみて": "screenshot", "画面全体スクリーンショットして": "screenshot_save", "音量上げ": "volume_up", "音量を上げ": "volume_up",
    "音量下げ": "volume_down", "音量を下げ": "volume_down", "ミュート": "mute", "消音": "mute",
    "再生": "media_play_pause", "一時停止": "media_play_pause", "次の曲": "media_next", "前の曲": "media_prev",
    "半角全角": "ime_toggle", "日本語入力": "ime_toggle", "アプリを終了": "alt_f4", "終了して": "alt_f4",
    # 2026-09-22 追加（2 回目）：もっと操作できるように。音楽・動画は「〜止めて」でも再生/一時停止として扱う
    "音楽止めて": "media_play_pause", "動画を止めて": "media_play_pause", "動画止めて": "media_play_pause",
    "音楽再生": "media_play_pause", "音楽流して": "media_play_pause", "音楽かけて": "media_play_pause",
    "次のウィンドウ": "next_window", "次の窓": "next_window", "前のウィンドウ": "prev_window", "前の窓": "prev_window",
    "画面を撮って": "screenshot_save", "全画面を撮って": "screenshot_save", "丸ごと撮って": "screenshot_save",
    "画面を切り取って": "screenshot", "切り取って": "screenshot",
    # ここから下は 2026-09-22 に追加（よく使うのに言えなかった操作）
    "アドレスバー": "address_bar", "urlバー": "address_bar", "ユーアールエル": "address_bar",
    "ブックマーク": "bookmark", "お気に入りに追加": "bookmark", "ブックマークに追加": "bookmark",
    "ブックマーク一覧": "bookmarks_list", "ブックマークを見せて": "bookmarks_list",
    "履歴": "history", "りれき": "history", "閲覧履歴": "history",
    "ダウンロード一覧": "downloads", "ダウンロードを見せて": "downloads", "ダウンロードを開いて": "downloads",
    "閉じたタブ": "reopen_tab", "閉じたタブを開き直して": "reopen_tab", "タブを復元": "reopen_tab",
    "ズームイン": "zoom_in", "ズームして": "zoom_in", "ズームアウト": "zoom_out", "ズームを戻して": "zoom_reset", "ズームリセット": "zoom_reset", "等倍に戻して": "zoom_reset",
    "名前を付けて保存": "save_as", "別名で保存": "save_as",
    "名前を変更": "rename_file", "名前の変更": "rename_file", "ファイル名を変更": "rename_file",
    "新しいフォルダ": "new_folder", "新規フォルダ": "new_folder", "フォルダを作って": "new_folder",
    "次を検索": "find_next", "次の検索": "find_next", "前を検索": "find_prev",
    "印刷": "print", "プリント": "print", "フルスクリーン": "fullscreen", "フル画面": "fullscreen",
    "開発者ツール": "dev_tools", "デベロッパーツール": "dev_tools", "デベロッパーツールを開いて": "dev_tools",
    "一単語消して": "word_delete", "単語を消して": "word_delete",
    "パソコンをロック": "lock_pc", "ロックして": "lock_pc", "画面をロック": "lock_pc",
    "タスクマネージャー": "task_manager",
    "クリップボードの履歴": "clipboard_history", "クリップボード履歴": "clipboard_history",
    "次のデスクトップ": "desktop_next", "前のデスクトップ": "desktop_prev", "新しいデスクトップ": "desktop_new",
    "新しいウィンドウ": "new_window", "新規ウィンドウ": "new_window", "ウィンドウを新しく": "new_window",
    "シークレットウィンドウ": "incognito", "シークレットモード": "incognito",
    "プライベートウィンドウ": "incognito", "プライベートモード": "incognito",
    "windowsの設定": "settings", "ウィンドウズの設定": "settings", "設定を開いて": "settings",
    "パソコンの設定": "settings", "設定画面を開いて": "settings",
    "通知": "notifications", "通知を開いて": "notifications", "通知パネル": "notifications",
    "置換": "replace", "置き換え": "replace", "検索して置換": "replace",
    "強制再読み込み": "hard_reload", "スーパーリロード": "hard_reload", "キャッシュを無視して更新": "hard_reload",
    "ページの先頭": "page_top", "ページの一番上": "page_top", "文書の先頭": "page_top",
    "ページの最後": "page_bottom", "ページの一番下": "page_bottom", "文書の最後": "page_bottom",
    "音声入力": "voice_typing", "喋って入力": "voice_typing", "しゃべって入力": "voice_typing",
    "絵文字": "emoji", "絵文字パネル": "emoji", "顔文字": "emoji",
    # 2026-09-22 動作確認で言えなかった言い方
    "進んで": "forward", "次のページ": "forward", "このタブを閉じ": "close_tab", "今のタブを閉じ": "close_tab",
    "シークレット": "incognito", "シークレットで開いて": "incognito",
    "文字を大きく": "zoom_in", "字を大きく": "zoom_in", "文字を小さく": "zoom_out", "字を小さく": "zoom_out",
}
# 画面の端に寄せる・別のモニターへ移す（「左に寄せて」だけはマウス移動なので含めない）
_SNAP_WORDS = {
    "左半分": "left_half", "左はんぶん": "left_half", "画面の左": "left_half",
    "右半分": "right_half", "右はんぶん": "right_half", "画面の右": "right_half",
    "上半分": "upper_half", "上はんぶん": "upper_half",
    "下半分": "lower_half", "下はんぶん": "lower_half",
    "画面いっぱい": "screen_wide", "スクリーンいっぱい": "screen_wide",
    "左のモニター": "prev_monitor", "前のモニター": "prev_monitor", "左のディスプレイ": "prev_monitor",
    "右のモニター": "next_monitor", "次のモニター": "next_monitor", "別のモニター": "next_monitor",
    "他のモニター": "next_monitor", "右のディスプレイ": "next_monitor", "隣のモニター": "next_monitor",
    "隣のディスプレイ": "next_monitor",
    "中央": "center", "真ん中": "center", "まんなか": "center", "画面の中央": "center",
}
# 「ウィンドウを左に寄せて」のような、ウィンドウを明示した言い方（マウス移動と区別する）
_SNAP_SIDE = re.compile(r"(この|今の)?(ウィンドウ|画面)を?(左|右)(に|へ)"
                        r"(寄せて|よせて|移して|うつして|移動して|動かして|持っていって|もっていって|表示して|して)")
_WIN_WORDS = {"最小化": "minimize", "しまって": "minimize", "最大化": "maximize", "全画面": "maximize",
              "全面": "maximize", "画面いっぱい": "maximize",   # 「全面」は「全画面」の聞き違い
              "元のサイズ": "restore", "全部最小化": "show_desktop",
              "全部のウィンドウを最小化": "show_desktop", "すべて最小化": "show_desktop", "全て最小化": "show_desktop", "ウィンドウを閉じ": "close", "閉じて": "close", "デスクトップ": "show_desktop",
              # 2026-09-22 追加（2 回目）：ウィンドウを常に手前に
              "常に手前に": "always_on_top", "常に手前": "always_on_top", "手前に固定": "always_on_top",
              "手前に固定して": "always_on_top", "常に手前解除": "topmost_off", "常に手前を解除": "topmost_off",
              "手前の固定を解除": "topmost_off", "手前の固定解除": "topmost_off"}
_POLITE = (r"(して|て|してください|して下さい|お願い|おねがい|を|に|へ|する|押して|開いて|ひらいて|出して|だして|"
           r"見せて|みせて|表示して|作って|つくって|作成して|戻って|もどって|戻して|してほしい|して欲しい|"
           r"ちょうだい|くれ|くれる|もらえる|表示|ね|よ)*")


class KeywordEngine(DecisionEngine):
    name = "keyword"

    def decide(self, text: str, snap: Snapshot, lookup: Lookup, previous: Decision | None,
               facts: dict | None = None, recent: list | None = None, session: dict | None = None) -> Decision:
        c = textparse.compact(text)
        d = self._match(c, text, lookup, previous)
        d.engine = "keyword"
        return d

    def _match(self, c: str, raw: str, lookup: Lookup, previous: Decision | None) -> Decision:
        def D(action, conf, **params):
            return Decision(action=action, params=params, confidence=conf)

        if textparse.is_stop(raw):
            return D("stop", 1.0)
        if re.fullmatch(r"(もう一回|もういっかい|もう一度|もういちど|同じの|もう1回)" + _POLITE, c):
            return D("repeat", 0.97)

        # アプリのボタン・メニューに登録した言い方と完全に一致するなら、そちらを優先する
        # （「ボードを入力」は文字入力ではなく Board ボタン）
        for kind, action in (("element", "click_element"), ("menu", "menu_command")):
            for cid, cand in (lookup.get(kind) or {}).items():
                if any(c == textparse.compact(a) for a in (cand.data.aliases or [])):
                    return D(action, 0.96, **({"element": cid, "button": "left"} if kind == "element" else {"menu": cid}))

        # 入力（「〜と入力」）
        t = textparse.extract_text(raw)
        if t is not None:
            d = D("type_text", 0.9)
            d.text = t
            return d

        # アプリを覚える
        if re.fullmatch(r"(この)?(アプリ|画面|ソフト)?の?(メニュー)?を?(覚えて|おぼえて|学習して|学習|記憶して)" + _POLITE, c)                 and ("覚え" in c or "おぼえ" in c or "学習" in c or "記憶" in c):
            return D("learn_app", 0.97)

        # プロファイルの独自コマンド・アプリマップのメニュー（言い方と完全に一致すれば確定）
        bare = re.sub(r"(して|してください|して下さい|お願い|おねがい|を|する|実行|開いて|見せて|みせて|表示して|"
                      r"出して|だして|ちょうだい|頼む|たのむ)+$", "", c)
        for cid, cand in (lookup.get("command") or {}).items():
            if any(bare == textparse.compact(ph) or c == textparse.compact(ph)
                   for ph in [cand.data.name] + cand.data.say):
                return D("app_command", 0.97, command=cid)
        menus = lookup.get("menu") or {}
        for cid, cand in menus.items():
            if bare and any(bare == textparse.compact(a) or c == textparse.compact(a) for a in cand.data.aliases):
                return D("menu_command", 0.96, menu=cid)  # プロファイルに登録した言い方と一致 → 即実行
            if bare and bare == textparse.compact(cand.data.path[-1]):
                return D("menu_command", 0.92, menu=cid)
        # 登録した言い方が発話に含まれている（「GTO、GTO、トレーナー開いて」の中の「GTOトレーナー」など）。最長一致を採る
        best = max(((len(textparse.compact(a)), cid) for cid, cand in menus.items() for a in cand.data.aliases
                    if len(textparse.compact(a)) >= 3 and textparse.compact(a) in c), default=None)
        if best:
            return D("menu_command", 0.95, menu=best[1])
        # 画面のボタンなどに登録した言い方が発話に含まれている（最長一致）
        els = lookup.get("element") or {}
        best = max(((len(textparse.compact(a)), cid) for cid, cand in els.items() for a in (cand.data.aliases or [])
                    if len(textparse.compact(a)) >= 2 and textparse.compact(a) in c), default=None)
        if best:
            return D("click_element", 0.95, element=best[1], button="left")
        # カタカナの発音が英語のメニュー項目名と一致する（「ストラテジー」→ Strategy）
        from .. import phonetic
        scored = sorted(((phonetic.match_ratio(raw, cand.data.path[-1]), len(phonetic.content_words(cand.data.path[-1])),
                          cid) for cid, cand in menus.items()), reverse=True)
        if scored and scored[0][0] >= 1.0:
            ratio, n_words, cid = scored[0]
            tied = len(scored) > 1 and scored[1][0] >= 1.0 and scored[1][1] == n_words
            return D("menu_command", 0.7 if tied else (0.93 if n_words >= 2 else 0.9), menu=cid)
        if scored and scored[0][0] >= 0.75 and scored[0][1] >= 3:  # 3 語以上の項目名で 4 分の 3 以上が一致
            return D("menu_command", 0.9, menu=scored[0][2])
        # カタカナの発音が英語のボタン名と一致する（「ピック」→ Pick、「カスタマイズ」→ Customize）。
        # 「押して」「クリック」などクリックを求める語があるときだけ（一般的な会話を誤ってクリックにしないため）
        if re.search(r"(押して|押す|クリック|タップ|選んで|選択|切り替え|オンにして|オフにして|チェック)", c):
            scored = sorted(((phonetic.match_ratio(raw, cand.data.name), len(phonetic.content_words(cand.data.name)),
                              cid) for cid, cand in els.items() if cand.data.source == "uia"), reverse=True)
            if scored and scored[0][0] >= 1.0:
                tied = len(scored) > 1 and scored[1][0] >= 1.0 and scored[1][1] == scored[0][1]
                return D("click_element", 0.7 if tied else 0.9, element=scored[0][2], button="left")

        # 番号表示・グリッド
        if re.fullmatch(r"(番号|ばんごう)(表示|出して|だして|振って|ふって)?" + _POLITE, c):
            return D("show_hints", 0.97, hint_mode="elements")
        if re.fullmatch(r"(グリッド|マス目|ますめ)(表示|出して|だして)?" + _POLITE, c):
            return D("show_hints", 0.97, hint_mode="grid")

        # クリック（現在位置）
        m = re.fullmatch(r"(そこ|ここ|そこを|ここを)?(ダブルクリック|右クリック|中クリック|クリック|押して|押す|タップ)" + _POLITE, c)
        if m:
            b = {"ダブルクリック": "double", "右クリック": "right", "中クリック": "middle"}.get(m.group(2), "left")
            return D("click", 0.97, button=b)

        # ドラッグ
        if re.fullmatch(r"(ドラッグ開始|ドラッグ|つかんで|掴んで|つかむ|押さえて|ホールド)" + _POLITE, c):
            return D("drag_start", 0.95)
        if re.fullmatch(r"(ここで)?(離して|はなして|離す|ドロップ|放して)" + _POLITE, c):
            return D("drag_end", 0.95)

        # ウィンドウを画面の端に寄せる／別のモニターへ移す（マウス移動より先に見る）
        m = _SNAP_SIDE.fullmatch(c)
        if m:
            return D("snap_window", 0.95, snap="left_half" if m.group(3) == "左" else "right_half")
        for w, snap in sorted(_SNAP_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.fullmatch(r"(この|今の|ウィンドウ|画面)?を?" + re.escape(w)
                            + r"(に|へ)?(して|表示して|移動して|動かして|寄せて|移して|やって|置いて|おいて|切り替え)?" + _POLITE, c):
                return D("snap_window", 0.95, snap=snap)
        # 四分の 1 への配置（「左上に置いて」。「左上」「左上に」だけのときはマウス移動のまま）
        m = re.fullmatch(r"(この|今の|ウィンドウ|画面)?を?(左上|右上|左下|右下|ひだりうえ|みぎうえ|ひだりした|みぎした)"
                         r"(に|へ|の)?(置いて|置く|おいて|スナップ|四分の一|よんぶんのいち|小さくして)", c)
        if m:
            q = {"左上": "top_left", "右上": "top_right", "左下": "bottom_left", "右下": "bottom_right",
                 "ひだりうえ": "top_left", "みぎうえ": "top_right",
                 "ひだりした": "bottom_left", "みぎした": "bottom_right"}[m.group(2)]
            return D("snap_window", 0.95, snap=q)

        # スクロール / マウス移動
        amount = "small" if any(w in c for w in _SMALL) else ("large" if any(w in c for w in _LARGE) else "medium")
        body = c
        for w in sorted(_SMALL + _LARGE, key=len, reverse=True):  # 「もうちょい」を「ちょい」より先に消す
            body = body.replace(w, "")
        body = re.sub(r"^もう", "", body)
        is_scroll = "スクロール" in body
        body = body.replace("スクロール", "").replace("マウス", "").replace("カーソル", "").replace("を", "")
        direction = None
        for w, dkey in _DIR_WORDS:
            if body.startswith(w):
                direction, body = dkey, body[len(w):]
                break
        if direction and re.fullmatch(_FILLER, body):
            return D("scroll" if is_scroll else "mouse_move", 0.97, direction=direction, amount=amount)
        if direction is None and re.fullmatch(_FILLER, body) and previous and previous.action in ("mouse_move", "scroll") \
                and amount != "medium":
            # 「もうちょい」「もっと」だけ → 前回と同じ方向
            return D(previous.action, 0.9, direction=previous.params.get("direction", "right"), amount=amount)
        if is_scroll and direction:
            return D("scroll", 0.85, direction=direction, amount=amount)

        # キー操作（「タブ閉じて」のように助詞を省いた言い方も許す）
        c_no_wa = c.replace("を", "")
        for w, key in sorted(_KEY_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.fullmatch(re.escape(w) + _POLITE, c, flags=re.I) or \
                    re.fullmatch(re.escape(w.replace("を", "")) + _POLITE, c_no_wa, flags=re.I):
                return D("key_combo", 0.95, key=key)
        for w, act in sorted(_WIN_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.fullmatch(re.escape(w) + _POLITE, c) or \
                    re.fullmatch(re.escape(w.replace("を", "")) + _POLITE, c_no_wa):
                return D("window_control", 0.95, window_action=act)

        # 名前付きの対象（アプリ起動・切り替え・要素クリック）
        m = re.fullmatch(r"(?P<n>.+?)を?(開いて|ひらいて|起動|立ち上げ|たちあげ|つけて)(して|て|ください|お願い)*", c)
        if m:
            return self._named("launch_app", "app", m.group("n"), lookup)
        m = re.fullmatch(r"(?P<n>.+?)(に|へ)(切り替え|切りかえ|きりかえ|移動|戻って|もどって)(て|して|ください|お願い)*", c)
        if m:
            return self._named("switch_window", "window", m.group("n"), lookup)
        m = re.fullmatch(r"(?P<n>.+?)(を|の)?(ダブルクリック|右クリック|クリック|押して|押す|タップ|選んで|選択)(して|て|ください|お願い)*", c)
        if m:
            b = {"ダブルクリック": "double", "右クリック": "right"}.get(m.group(3), "left")
            d = self._named("click_element", "element", m.group("n"), lookup)
            d.params["button"] = b
            return d
        return D("unknown", 0.0)

    @staticmethod
    def _named(action: str, kind: str, name: str, lookup: Lookup) -> Decision:
        cands = lookup.get(kind) or {}
        scored = sorted(((textparse.similarity(name, c.data.name if kind == "element" else c.label), cid)
                         for cid, c in cands.items()), reverse=True)
        if not scored or scored[0][0] <= 0:
            return Decision(action=action, params={}, confidence=0.0)
        best_s, best = scored[0]
        # 名前が丸ごと一致すれば 1.0 以上になる（textparse.similarity の仕様）。僅差の対抗馬がいれば確認に回す
        tied = len(scored) > 1 and scored[1][0] >= best_s - 0.05
        conf = (0.7 if tied else 0.9) if best_s >= 1.0 else min(0.6, best_s)
        return Decision(action=action, params={kind: best}, confidence=conf)


FAST_ACTIONS = {"mouse_move", "click", "scroll", "stop", "drag_start", "drag_end", "repeat", "key_combo",
                "window_control", "snap_window", "show_hints", "app_command", "learn_app", "menu_command"}


def fast_path(d: Decision) -> bool:
    return d.action in FAST_ACTIONS and d.confidence >= 0.95
