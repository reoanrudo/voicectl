"""決まった形の命令（数値・名前・時間つき）を、Jev を通さずにその場で処理する。

Jev は選択肢から 1 つ選ぶモデルなので、「音量30」「見積書というファイル」「5分後に知らせて」のような
数値や自由な名前を含む命令は苦手。発話の形が決まっているものはここで切り出して直接実行する。

  - 音量            : 「音量30」「音量を半分に」「音量10上げて」
  - タブ番号        : 「3番目のタブ」「最後のタブ」
  - 動画の早送り    : 「10秒戻して」「30秒進めて」
  - ページ内検索    : 「ページ内で〇〇を探して」
  - ファイル        : 「〇〇というファイルを開いて」「最近使ったExcel開いて」
  - ウィンドウ配置  : 「左にChrome右にObsidian」「ChromeとObsidianを並べて」
  - 名前つき窓操作  : 「Chromeを閉じて」「Discordを最小化」
  - 時刻・日付      : 「今何時」「今日何日」
  - 計算            : 「123かける45は」
  - タイマー        : 「5分後に知らせて」「タイマー3分」
  - 読み上げ        : 「選択したところ読んで」「クリップボード読んで」
  - 書き取り        : 「書き取り開始」〜「書き取り終わり」
  - 選択して検索    : 「これで検索して」（選択中の文字を Web で検索）
  - 行              : 「この行を選んで」「行を消して」
  - ダウンロード    : 「さっきダウンロードしたのを開いて」
  - 音声メモ        : 「メモっておいて〇〇」「メモ見せて」「メモ読んで」
  - ルーチン        : 「作業開始」（config の routines か、声で登録したもの）
                      「〇〇と言ったら△△して」で登録、「〇〇のルーチン消して」で削除
回数つきの命令（「3回戻って」「5回下にスクロール」）は count_prefix() で回数を切り出し、
残りを通常の判定に回してから繰り返す（controller 側）。
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import textparse, winutil

log = logging.getLogger(__name__)

STATE = Path(__file__).resolve().parent.parent / "state" / "routines.json"


@dataclass
class Intent:
    kind: str
    args: dict = field(default_factory=dict)


# ---- 数値 ----
_NUM = r"(?P<num>[0-9０-９]+|[〇零一二三四五六七八九十百千]+)"


def to_int(s: str) -> int | None:
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit():
        return int(s)
    return kanji_num(s)


_KD = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def kanji_num(s: str) -> int | None:
    """「三十」「百二十五」「千五百」「二〇二六」を数に直す（万以上は _kanji_big）。"""
    if not s or any(ch not in _KD and ch not in "十百千" for ch in s):
        return None
    if all(ch in _KD for ch in s):   # 「二〇二六」のような位取りなしの並び
        return int("".join(str(_KD[ch]) for ch in s))
    total, cur = 0, 0
    for ch in s:
        if ch in _KD:
            cur = _KD[ch]
        else:
            unit = {"十": 10, "百": 100, "千": 1000}[ch]
            total += (cur or 1) * unit
            cur = 0
    return total + cur


def count_prefix(text: str) -> tuple[int, str] | None:
    """「3回戻って」「下に5回スクロール」「エンター2回」→ (回数, 回数を除いた命令)。回数は 2〜30。"""
    c = textparse.compact(textparse.normalize(text)).rstrip("。．.")
    m = re.search(_NUM + r"(回|かい|度)(ずつ)?", c)
    if not m:
        return None
    n = to_int(m.group("num"))
    if n is None or not 2 <= n <= 30:
        return None
    rest = (c[:m.start()] + c[m.end():]).strip("、")
    rest = re.sub(r"^(を|で|に)", "", rest)
    if len(rest) < 1 or re.search(r"(もう|さっき)", c):   # 「もう1回」は繰り返しの命令
        return None
    return n, rest


# ---- 各命令の解析 ----
_BROWSER_PROCS = ("chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe")
_OPEN = r"(を|の)?(開いて|ひらいて|開く|開け|出して|見せて|表示して)(ください)?"
_POLITE = r"(して|にして|して下さい|してください|お願い|ね|よ|くれ)?"


def parse(text: str, routines: dict | None = None, layouts: dict | None = None) -> Intent | None:
    t = textparse.normalize(text).strip()
    c = textparse.compact(t).rstrip("。．.!！?？")
    if not c:
        return None
    if re.fullmatch(r"(何|なに|なん)(が|を)?(できる|出来る|できます)(の|か|？|\?)*|できること(を)?(教えて|見せて)?|使い方(を)?(教えて)?|ヘルプ", c):
        return Intent("help")
    for fn in (_p_glide, _p_explorer, _p_demo, _p_dictation, _p_routine_admin, _p_power, _p_settings, _p_alarm, _p_volume,
               _p_app_volume, _p_proc, _p_taskbar, _p_proc_kill, _p_tab, _p_seek, _p_find, _p_file, _p_arrange,
               _p_named_window, _p_paste_to, _p_time, _p_calc, _p_timer, _p_read, _p_shot, _p_read_later, _p_sel_search,
               _p_line, _p_memo, _p_download, _p_zip, _p_checksum, _p_ai):
        it = fn(c)
        if it is not None:
            return it
    for name in (layouts or {}):   # 「開発の配置」（config の layouts）
        if re.fullmatch(re.escape(str(name)) + r"(の)?(配置|レイアウト)(に|で|して|してね)?", c):
            return Intent("layout", {"name": str(name)})
    if re.fullmatch(r"配置(一覧|リスト)(を)?(見せて|教えて)?", c):
        return Intent("layout_list")
    if routines:
        key = routine_key(c)
        for name in routines:
            if routine_key(name) == key:
                return Intent("routine", {"name": name})
        # 表記の揺れ（漢字／かな・長音）は読みが同じなら同じルーチンとみなす
        from . import phonetic
        rd = phonetic._reading(key).replace("ー", "")
        if len(rd) >= 4:
            for name in routines:
                if phonetic._reading(routine_key(name)).replace("ー", "") == rd:
                    return Intent("routine", {"name": name})
    return None


_DIR_WORDS = {"右上": "up_right", "左上": "up_left", "右下": "down_right", "左下": "down_left",
              "右": "right", "左": "left", "上": "up", "下": "down"}


def _p_glide(c: str) -> Intent | None:
    """「ゆっくり右に」「右にずっと」「そのまま下へ」→ 止めるまでマウスを動かし続ける（「そこ」「ストップ」で止まる）。"""
    m = re.fullmatch(r"(マウスを)?(ゆっくり|ずっと|そのまま|すーっと|スーッと)(?P<d>右上|左上|右下|左下|右|左|上|下)(に|へ|の方に|の方へ)?"
                     r"(動かして|移動して|行って|いって|動いて)?(ください)?", c) \
        or re.fullmatch(r"(マウスを)?(?P<d>右上|左上|右下|左下|右|左|上|下)(に|へ)(ずっと|ゆっくり|そのまま)(動かして|移動して|行って|いって)?", c)
    if m:
        return Intent("glide", {"direction": _DIR_WORDS[m.group("d")], "slow": "ゆっくり" in c})
    return None


_DEST = {"デスクトップ": "shell:Desktop", "ダウンロード": "shell:Downloads", "ドキュメント": "shell:Personal",
         "ピクチャ": "shell:My Pictures", "画像フォルダ": "shell:My Pictures", "ビデオ": "shell:My Video",
         "ミュージック": "shell:My Music"}


def _p_explorer(c: str) -> Intent | None:
    """エクスプローラーでの操作（前面がエクスプローラーのときだけ実行。controller が確認する）。"""
    if re.fullmatch(r"(一つ|ひとつ|1つ)?(上|うえ)(の)?(フォルダ|階層|へ|に)(に|へ)?(戻って|移動して|行って|上がって)?", c) \
            or re.fullmatch(r"(親フォルダ|上の階層)(に|へ)?(戻って|移動して|行って)?", c):
        return Intent("explorer", {"op": "up"})
    for pat, by, desc in ((r"(新しい順|最新順|日付の新しい順|更新日順)", "date", True), (r"(古い順|日付の古い順)", "date", False),
                          (r"(名前順|名前で|あいうえお順|abc順)", "name", False), (r"(大きい順|サイズの大きい順|サイズ順)", "size", True),
                          (r"(小さい順|サイズの小さい順)", "size", False), (r"(種類順|種類で)", "type", False)):
        if re.fullmatch(pat + r"(に)?(並べて|並び替えて|ならべて|ソートして|並べ替えて)?", c):
            return Intent("explorer", {"op": "sort", "by": by, "desc": desc})
    m = re.fullmatch(_NUM + r"(番目|個目|つ目)(の)?(ファイル|フォルダ|項目|やつ)?(を)?(開いて|ひらいて|開く|選んで|出して)", c)
    if m and (n := to_int(m.group("num"))) is not None and 1 <= n <= 200:
        return Intent("explorer", {"op": "open_nth", "n": n})
    if re.fullmatch(r"(選択して|選んで|選んだの|選択したの|それ|これ)(を)?(開いて|ひらいて|開く|ダブルクリック)(してください)?", c):
        return Intent("explorer", {"op": "open_selected"})   # エクスプローラーが前面のときだけ（controller が確認）
    m = re.fullmatch(r"(これ|それ|選んだ(ファイル|もの|やつ)?|選択した(ファイル|もの)?)?(を)?(?P<dest>デスクトップ|ダウンロード|ドキュメント|ピクチャ|画像フォルダ|ビデオ|ミュージック)"
                     r"(フォルダ)?(に|へ)(?P<how>コピー|複製|移動|移して|送って|動かして)(して)?(ください)?", c)
    if m:
        return Intent("explorer", {"op": "move" if re.search(r"(移動|移して|動かして|送って)", m.group("how")) else "copy",
                                   "dest": _DEST[m.group("dest")], "dest_name": m.group("dest")})
    if re.fullmatch(r"(この|ここの|選んだ|選択した)?(ファイルの|フォルダの)?(パス|場所)(を)?(コピー)(して)?", c):
        return Intent("explorer", {"op": "copy_path"})
    if re.fullmatch(r"(ここ|このフォルダ)(で|に)(ターミナル|コマンドプロンプト|powershell|パワーシェル|端末)(を)?(開いて|起動して|出して)?", c.lower()):
        return Intent("explorer", {"op": "terminal"})
    return None


def is_glide_stop(text: str) -> tuple[bool, bool]:
    """動かしている最中の発話が「止める」か、「止めてクリック」か。"""
    c = textparse.compact(textparse.normalize(text)).rstrip("。！!")
    click = bool(re.search(r"(クリック|押して|タップ)", c))
    stop = click or bool(re.fullmatch(r"(そこ|ここ|ストップ|止まって|とまって|止めて|とめて|止まれ|ok|オッケー|いいよ|そこで|ここで|はい|うん)"
                                        r"(で)?(止めて|ストップ)?(クリック|押して)?", c, flags=re.I))
    return stop, click


def _p_demo(c: str) -> Intent | None:
    """やって見せる手順の記録（demo.py）。「〇〇の手順を記録して」「記録開始」「記録終了」「記録取り消し」。"""
    if re.fullmatch(r"(記録|録画|きろく)(を)?(終了|終わり|おわり|ストップ|止めて|とめて|終えて|完了)" + _POLITE, c):
        return Intent("demo_stop")
    if re.fullmatch(r"(記録|録画|きろく)(を)?(取り消し|取り消して|とりけして|取消|キャンセル|破棄|捨てて|やめて)" + _POLITE, c):
        return Intent("demo_cancel")
    m = re.fullmatch(r"(?P<name>.{1,20}?)(の|という|って|として)(手順|操作|やり方|ルーチン)?(を|で)?(記録|録画|きろく)(して|開始|を開始|スタート|する)?"
                     + _POLITE, c)
    if m:
        return Intent("demo_start", {"name": m.group("name")})
    if re.fullmatch(r"(手順|操作)?(の)?(記録|録画|きろく)(を)?(開始|始めて|はじめて|スタート)" + _POLITE, c):
        return Intent("demo_start", {"name": ""})
    return None


def _p_dictation(c: str) -> Intent | None:
    if re.fullmatch(r"(書き取り|かきとり|ディクテーション|文字入力モード|口述)(モード)?(を)?(開始|始めて|はじめて|スタート|オン)"
                    + _POLITE, c):
        return Intent("dictation_on")
    return None


def dictation_end(text: str) -> bool:
    c = textparse.compact(textparse.normalize(text)).rstrip("。．.!！")
    return bool(re.fullmatch(r"(書き取り|かきとり|ディクテーション|文字入力モード|口述)?(モード)?(を)?"
                             r"(終わり|おわり|終了|やめて|止めて|とめて|ストップ|オフ)" + _POLITE, c))


def _p_routine_admin(c: str) -> Intent | None:
    # 「作業開始と言ったらObsidianを開いてChromeを開いて」
    m = re.fullmatch(r"(?P<name>.{2,20}?)(って|と)(言ったら|いったら|言えば|いえば|言うと|いうと)(?P<body>.{3,})", c)
    if m:
        body = re.sub(r"(ように|って)?(覚えて|おぼえて|登録して|して下さい|してください)$", "", m.group("body"))
        body = re.sub(r"(する|して)$", "して", body)
        if len(body) >= 3:
            return Intent("routine_add", {"name": m.group("name"), "body": body})
    m = re.fullmatch(r"(?P<name>.{2,20}?)(として|の名前で|という名前で)(登録|保存)(して)?(ください|お願い)?", c)
    if m:   # 提案された流れを登録する
        return Intent("routine_from_suggest", {"name": m.group("name")})
    m = re.fullmatch(r"(?P<name>.{2,20}?)の?(ルーチン|ルーティン|マクロ)(を)?(消して|削除して|忘れて)", c)
    if m:
        return Intent("routine_del", {"name": m.group("name")})
    if re.fullmatch(r"(ルーチン|ルーティン|マクロ)(の)?(一覧|リスト)(を)?(見せて|出して|表示して)?", c):
        return Intent("routine_list")
    return None


def _p_volume(c: str) -> Intent | None:
    if not re.match(r"(音量|ボリューム|おんりょう|オンリョウ)", c):
        return None
    body = re.sub(r"^(音量|ボリューム|おんりょう|オンリョウ)(を|は)?", "", c)
    if re.fullmatch(r"(最大|マックス|MAX|100パー(セント)?)" + r"(に)?" + _POLITE, body, flags=re.I):
        return Intent("volume_set", {"level": 100})
    if re.fullmatch(r"(半分|はんぶん)(に|くらいに)?" + _POLITE, body):
        return Intent("volume_set", {"level": 50})
    if re.fullmatch(r"(ゼロ|0|最小)(に)?" + _POLITE, body):
        return Intent("volume_set", {"level": 0})
    m = re.fullmatch(_NUM + r"(割|わり)(くらい|ぐらい|程度)?(に|へ)?" + _POLITE, body)
    if m and (n := to_int(m.group("num"))) is not None and 0 <= n <= 10:
        return Intent("volume_set", {"level": n * 10})   # 「三割」→ 30%
    m = re.fullmatch(_NUM + r"(パーセント|%|％)?(くらい|ぐらい)?(に|へ)?" + _POLITE, body)
    if m and (n := to_int(m.group("num"))) is not None and 0 <= n <= 100:
        return Intent("volume_set", {"level": n})
    m = re.fullmatch(_NUM + r"(パーセント|%|％)?(だけ)?(上げて|あげて|大きく|下げて|さげて|小さく)(して)?", body)
    if m and (n := to_int(m.group("num"))) is not None and 0 < n <= 100:
        sign = 1 if re.search(r"(上げ|あげ|大きく)", body) else -1
        return Intent("volume_add", {"delta": sign * n})
    if re.fullmatch(r"(いくつ|何パーセント|なんぱーせんと|どのくらい|どれくらい)[?？]?", body):
        return Intent("volume_get")
    return None


def _p_tab(c: str) -> Intent | None:
    m = re.fullmatch(r"(左から)?" + _NUM + r"(番目|個目|つ目)の?タブ(に|へ)?(移動|切り替え|切りかえ|行って|開いて|して)?(て|して)?", c) \
        or re.fullmatch(r"タブ" + _NUM + r"(番)?(に|へ)?(移動|切り替え|行って)?(て|して)?", c)
    if m and (n := to_int(m.group("num"))) is not None and 1 <= n <= 8:
        return Intent("tab", {"n": n})
    if re.fullmatch(r"(一番)?(最後|右端|いちばん右)の?タブ(に|へ)?(移動|切り替え|行って)?(て|して)?", c):
        return Intent("tab", {"n": 9})
    # 「3つ前のタブ」「2つ右のタブ」→ 前後のタブへ n 回
    m = re.fullmatch(_NUM + r"(つ|個)(前|左|次|後ろ|右|先)の?タブ(に|へ)?(戻って|移動|切り替え|行って|して)?(て|して)?", c)
    if m and (n := to_int(m.group("num"))) is not None and 1 <= n <= 20:
        return Intent("tab_rel", {"n": n if re.search(r"(次|後ろ|右|先)", c) else -n})
    return None


# 「〇〇の設定」→ Windows の設定の該当ページ（ms-settings:）を開く。切り替え（オン・オフ）はページで行う
SETTINGS_PAGES = [
    (r"(wi-?fi|ワイファイ|無線lan|無線)", "network-wifi", "Wi-Fi"),
    (r"(bluetooth|ブルートゥース|ブルートゥース)", "bluetooth", "Bluetooth"),
    (r"(ネットワーク|インターネット)", "network", "ネットワーク"),
    (r"(ディスプレイ|画面の明るさ|明るさ|解像度|モニター)", "display", "ディスプレイ"),
    (r"(サウンド|音の|スピーカー|オーディオ)", "sound", "サウンド"),
    (r"(マイク)", "privacy-microphone", "マイク"),
    (r"(カメラ)", "privacy-webcam", "カメラ"),
    (r"(通知)", "notifications", "通知"),
    (r"(電源|バッテリー|スリープ)", "powersleep", "電源"),
    (r"(windowsupdate|ウィンドウズアップデート|アップデート|更新プログラム)", "windowsupdate", "Windows Update"),
    (r"(既定のアプリ|デフォルトのアプリ)", "defaultapps", "既定のアプリ"),
    (r"(アプリ)", "appsfeatures", "アプリ"),
    (r"(マウス)", "mousetouchpad", "マウス"),
    (r"(キーボード|入力)", "typing", "入力"),
    (r"(背景|壁紙|個人用設定)", "personalization-background", "背景"),
    (r"(日付|時刻|時間)", "dateandtime", "日付と時刻"),
    (r"(言語)", "regionlanguage", "言語"),
    (r"(ストレージ|容量)", "storagesense", "ストレージ"),
    (r"(プライバシー)", "privacy", "プライバシー"),
    (r"(タスクバー)", "taskbar", "タスクバー"),
]


def _p_settings(c: str) -> Intent | None:
    low = c.lower()
    m = re.fullmatch(r"(?P<what>.{1,20}?)(の)?設定(画面)?(を)?(開いて|ひらいて|見せて|出して|表示して)?(ください)?", low) \
        or re.fullmatch(r"(?P<what>.{1,20}?)(を|の)?(オン|オフ|on|off|つけて|消して|切って|有効|無効)(に)?(して)?(ください)?", low) \
        or re.fullmatch(r"(?P<what>明るさ|画面の明るさ)(を)?(上げて|下げて|明るく|暗く)(して)?", low)
    if not m:
        return None
    what = m.group("what")
    for pat, page, label in SETTINGS_PAGES:
        if re.search(pat, what):
            return Intent("settings_page", {"page": page, "label": label})
    return None


_POWER = {"shutdown": r"(パソコン|pc|電源)?(を)?(シャットダウン|電源を切って|電源切って|切って)(して)?",
          "restart": r"(パソコン|pc)?(を)?(再起動|リスタート)(して)?",
          "sleep": r"(パソコン|pc)?(を)?(スリープ)(に)?(して)?",
          "signout": r"(サインアウト|ログアウト)(して)?"}


def _p_power(c: str) -> Intent | None:
    for kind, pat in _POWER.items():
        if re.fullmatch(pat + r"(ください)?", c.lower()):
            if kind == "shutdown" and not re.search(r"(シャットダウン|電源)", c):
                return None   # 「切って」だけは電源とは限らない
            return Intent("power", {"kind": kind})
    return None


def _p_alarm(c: str) -> Intent | None:
    """「3時に起こして」「15時半に知らせて」「7時10分にアラーム」→ 次にその時刻になったら知らせる。"""
    c = re.sub(r"^(明日の|今日の|今夜の)?(朝|夜|夕方|昼)?", "", c) if re.search(r"時", c) else c
    m = re.fullmatch(r"(午前|午後)?(?P<h>[0-9０-９〇一二三四五六七八九十]+)時(?P<m>[0-9０-９〇一二三四五六七八九十]+分|半)?"
                     r"(に|で|の)?(起こして|おこして|知らせて|しらせて|教えて|おしえて|アラーム|目覚まし|めざまし|タイマー)"
                     r"(を)?(かけて|かけといて|かけておいて|セットして|セットしといて|設定して|設定|セット|入れて|鳴らして|して|お願い)?(ください|お願い)?", c)
    if not m:
        return None
    h = to_int(m.group("h"))
    mm = 30 if m.group("m") == "半" else (to_int(m.group("m")[:-1]) if m.group("m") else 0)
    if h is None or mm is None or not (0 <= h <= 24 and 0 <= mm < 60):
        return None
    pm = c.startswith("午後")
    return Intent("alarm", {"hour": h, "minute": mm, "pm": pm})


def alarm_target(hour: int, minute: int, pm: bool, now=None) -> _dt.datetime:
    """次にその時刻になる日時。「3時」は 3:00 と 15:00 のうち近い未来のほう（午後と言えば 15:00）。"""
    now = now or _dt.datetime.now()
    cands = []
    for h in ([hour + 12] if pm and hour < 12 else [hour] if hour >= 12 or hour == 0 else [hour, hour + 12]):
        t = now.replace(hour=h % 24, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += _dt.timedelta(days=1)
        cands.append(t)
    return min(cands)


def alarm_seconds(hour: int, minute: int, pm: bool, now=None) -> int:
    """次にその時刻になるまでの秒数（内部タイマーの代用時に使う）。"""
    now = now or _dt.datetime.now()
    return max(1, int((alarm_target(hour, minute, pm, now) - now).total_seconds()))


# ---- 標準のクロックアプリへのアラーム設定（UI Automation。キー入力はしない） ----
def _clock_walk(c, cond, out, maxd: int = 18) -> None:
    try:
        if cond(c):
            out.append(c)
    except Exception:
        return
    try:
        for ch in c.GetChildren():
            _clock_walk(ch, cond, out, maxd)
    except Exception:
        pass


def _clock_invoke(c) -> bool:
    try:
        c.GetInvokePattern().Invoke()
        return True
    except Exception:
        c.Click(simulateMove=False)
        return True


def _clock_find(win, cond):
    out: list = []
    _clock_walk(win, cond, out)
    return out[0] if out else None


def _picker_clicks(cur: int, want: int, size: int, up: bool) -> int:
    """ピッカーの値を cur から want まで動かすクリック数（最短の向き）。"""
    d = (want - cur) % size
    return d if up else (size - d) % size


def _set_picker(win, picker_id: str, want: int, size: int) -> None:
    """時・分のピッカーを want に合わせる。ボタンの向きは実行時に 1 回押して確かめ、
    クリック中に UI ツリーが組み替わって要素が一時的に消えても取り直す。"""
    import time as _time
    p = None
    end = _time.monotonic() + 4
    while p is None and _time.monotonic() < end:
        p = _clock_find(win, lambda c: c.AutomationId == picker_id)
        if p is None:
            _time.sleep(0.3)
    if p is None:
        raise RuntimeError(f"{picker_id} が見つかりません")

    def controls():
        btns: list = []
        _clock_walk(p, lambda c: c.ControlTypeName == "ButtonControl", btns)
        return btns, _clock_find(p, lambda c: c.AutomationId == "ValueText")

    def read() -> int:
        nonlocal val
        for _ in range(3):
            try:
                return int(str(val.Name).strip() or "0")
            except Exception:
                _time.sleep(0.2)
                btns, val = controls()   # ツリーの組み替えで古くなったら取り直す
        raise RuntimeError(f"{picker_id} の値が読めません")

    btns, val = controls()
    if len(btns) < 2:
        raise RuntimeError(f"{picker_id} のボタンが見つかりません")
    cur = read()
    up_btn = down_btn = None
    for b in btns:   # どちらが「+」か知らないので 1 回ずつ押して確かめる
        _clock_invoke(b)
        _time.sleep(0.25)
        nxt = read()
        if (nxt - cur) % size == 1:
            up_btn, down_btn = b, next(x for x in btns if x is not b)
            break
        if (cur - nxt) % size == 1:
            down_btn, up_btn = b, next(x for x in btns if x is not b)
            break
        cur = nxt   # 端で動かなかったときは値だけ更新して次のボタン
    if up_btn is None:
        raise RuntimeError(f"{picker_id} のボタンが動きません")
    for _round in range(4):   # 押し終えたら値を確認し、ずれていれば足りない分だけ押し直す
        cur = read()
        if cur == want:
            return
        n_up = _picker_clicks(cur, want, size, up=True)
        n_down = _picker_clicks(cur, want, size, up=False)
        btn = up_btn if n_up <= n_down else down_btn
        for _ in range(min(n_up, n_down)):
            _clock_invoke(btn)
            _time.sleep(0.15)
    raise RuntimeError(f"{picker_id} を {want} に合わせられません")


def set_clock_alarm(target: _dt.datetime, label: str = "voicectl") -> str:
    """Windows 標準のクロックアプリにアラームを追加する（保存まで行い、一覧に現れたことを確認する）。
    キー入力はしない（ボタンは UI Automation の Invoke で押す）。戻り値は表示用の文。"""
    import os
    import time as _time

    import uiautomation as auto
    os.startfile("ms-clock:")
    win = None
    end = _time.monotonic() + 12
    while _time.monotonic() < end and win is None:
        _time.sleep(0.4)
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName == "ApplicationFrameWindow" and "クロック" in (w.Name or ""):
                win = w
    if win is None:
        raise RuntimeError("クロックアプリが開きません")
    _time.sleep(0.6)
    # アラームのタブへ（ナビが畳んでいたら開いてから）
    nav = _clock_find(win, lambda c: c.ControlTypeName == "ListItemControl" and (c.Name or "").strip() == "アラーム")
    if nav is None:
        mb = _clock_find(win, lambda c: c.AutomationId == "MenuButton")
        if mb is None:
            raise RuntimeError("クロックのメニューが見つかりません")
        _clock_invoke(mb)
        _time.sleep(1.0)
        nav = _clock_find(win, lambda c: c.ControlTypeName == "ListItemControl" and (c.Name or "").strip() == "アラーム")
    if nav is None:
        raise RuntimeError("アラームのタブが見つかりません")
    try:
        nav.GetSelectionItemPattern().Select()
    except Exception:
        _clock_invoke(nav)
    _time.sleep(0.8)
    add = _clock_find(win, lambda c: c.AutomationId == "AddAlarmButton")
    if add is None:
        raise RuntimeError("「アラームを追加」ボタンが見つかりません")
    _clock_invoke(add)
    _time.sleep(1.4)
    h24 = _clock_find(win, lambda c: c.AutomationId == "PeriodPicker") is None   # 24 時間表示か
    _set_picker(win, "HourPicker", target.hour if h24 else (target.hour % 12 or 12), 24 if h24 else 12)
    _set_picker(win, "MinutePicker", target.minute, 60)
    if not h24:   # 12 時間表示なら午前／午後を選ぶ
        want_period = "午後" if target.hour >= 12 else "午前"
        period = _clock_find(win, lambda c: c.ControlTypeName == "ButtonControl" and (c.Name or "") == want_period)
        if period is None:
            raise RuntimeError("午前／午後が見つかりません")
        _clock_invoke(period)
    name = _clock_find(win, lambda c: c.ControlTypeName == "EditControl" and (c.Name or "") == "アラーム名")
    if name is not None:
        try:
            name.GetValuePattern().SetValue(label)
        except Exception:
            pass   # 名前を付けられなくてもアラーム自体は設定する
    save = _clock_find(win, lambda c: c.AutomationId == "PrimaryButton")
    if save is None:
        raise RuntimeError("保存ボタンが見つかりません")
    _clock_invoke(save)
    # 一覧に時刻の一致するアラームが現れるのを待つ（組み替えの直後は見えないことがあるため数秒かけ直す。
    # ここで見つけられないと失敗扱いになり、内部タイマーが二重に掛かるので短絡判定しない）
    hh, mm = (target.hour if h24 else (target.hour % 12 or 12)), target.minute
    end = _time.monotonic() + 8
    while _time.monotonic() < end:
        items = []
        _clock_walk(win, lambda c: c.AutomationId == "AlarmTime", items)
        for it in items:
            m = re.match(r"(\d{1,2})\D(\d{2})", re.sub(r"[\u200e\u200f]", "", it.Name or ""))
            if m and int(m.group(1)) == hh and int(m.group(2)) == mm:
                return f"{label} のアラームを {hh}:{mm:02d} にセットしました"
        _time.sleep(0.7)
    raise RuntimeError("保存したアラームが一覧に見つかりません")


def _p_seek(c: str) -> Intent | None:
    m = re.fullmatch(_NUM + r"(秒|びょう)(くらい|ほど)?(戻して|もどして|戻って|巻き戻して|前へ|進めて|すすめて|飛ばして|とばして|先へ|早送りして)", c)
    if m and (n := to_int(m.group("num"))) is not None and 0 < n <= 600:
        back = bool(re.search(r"(戻|もど|前)", c))
        return Intent("seek", {"seconds": -n if back else n})
    return None


def _p_find(c: str) -> Intent | None:
    m = re.fullmatch(r"(ページ内|このページ|画面内|この中)(で|から)(?P<q>.+?)(を|って)?(探して|さがして|検索して|見つけて)", c) \
        or re.fullmatch(r"(?P<q>.+?)(を|って)?(ページ内検索|ページ内で検索|ページ内で探)(して)?", c)
    if m:
        q = m.group("q").strip("「」")
        if q:
            return Intent("find_in_page", {"q": q})
    return None


_EXT_WORDS = {
    "excel": (".xlsx", ".xls", ".xlsm", ".csv"), "エクセル": (".xlsx", ".xls", ".xlsm", ".csv"),
    "word": (".docx", ".doc"), "ワード": (".docx", ".doc"),
    "powerpoint": (".pptx", ".ppt"), "パワポ": (".pptx", ".ppt"), "パワーポイント": (".pptx", ".ppt"),
    "pdf": (".pdf",), "画像": (".png", ".jpg", ".jpeg", ".webp", ".gif"), "写真": (".png", ".jpg", ".jpeg", ".heic"),
    "動画": (".mp4", ".mov", ".mkv", ".webm"), "テキスト": (".txt", ".md"), "メモ": (".txt", ".md"),
}


def _p_file(c: str) -> Intent | None:
    m = re.fullmatch(r"(最近|さいきん|前回|この前)(使った|つかった|開いた|ひらいた|保存した|の)?(?P<kind>[^をの]{0,12}?)"
                     r"(ファイル)?" + _OPEN, c)
    if m:
        kind = m.group("kind").lower()
        if not kind or kind in _EXT_WORDS:
            return Intent("recent_file", {"exts": list(_EXT_WORDS.get(kind, ()))})
    # 「ファイル〇〇を開いて」の形は「ファイルメニューを開いて」と区別できないので扱わない
    m = re.fullmatch(r"(?P<n>.{1,40}?)(という|っていう|って)(名前の)?(?P<what>ファイル|フォルダ|フォルダー|資料|書類)" + _OPEN, c)
    if m:
        name = m.group("n").strip("「」")
        if name and name not in ("この", "その", "あの", "新しい", "最近の"):
            return Intent("open_file", {"name": name, "folder": m.group("what").startswith("フォルダ")})
    # 「会議メモのファイル出して」（「さっきの」「ダウンロードの」などの特殊フォルダ語は除く）
    m = re.fullmatch(r"(?P<n>.{2,40}?)の(?P<what>ファイル|フォルダ|資料|書類)(を)?(開いて|ひらいて|開く|出して|見せて|みせて)(ください)?", c)
    if m and m.group("n").rstrip("こ のあ") not in ("さっき", "この前", "最近", "ダウンロード", "デスクトップ", "ドキュメント",
                                               "ピクチャ", "ミュージック", "ビデオ", "ごみ箱"):
        return Intent("open_file", {"name": m.group("n"), "folder": m.group("what").startswith("フォルダ")})
    return None


_SIDE = r"(左|右|上|下)(半分)?(に|へ|は)"


def _p_arrange(c: str) -> Intent | None:
    m = re.fullmatch(r"(?P<s1>左|右|上|下)(半分)?(に|へ|は)(?P<a>.+?)(を|で)?、?(?P<s2>左|右|上|下)(半分)?(に|へ|は)(?P<b>.+?)"
                     r"(を)?(並べて|ならべて|置いて|配置して|表示して|して|で)?", c)
    if m and (m.group("s1"), m.group("s2")) in (("左", "右"), ("右", "左"), ("上", "下"), ("下", "上")):
        first, second = (m.group("a"), m.group("b")) if m.group("s1") in ("左", "上") else (m.group("b"), m.group("a"))
        it = Intent("arrange", {"left": _strip_app(first), "right": _strip_app(second)})
        if m.group("s1") in ("上", "下"):
            it.args["vertical"] = True   # 「上にChrome下にメモ帳」
        return it
    m = re.fullmatch(r"(?P<a>.+?)(を)?、?(?P<s1>上|下)(に|へ)?(?P<b>.+?)(を)?、?(?P<s2>上|下)(に|へ)?"
                     r"(に)?(並べて|ならべて|置いて|配置して|表示して|して)?", c)
    if m:
        first, second = (m.group("a"), m.group("b")) if m.group("s1") == "上" else (m.group("b"), m.group("a"))
        return Intent("arrange", {"left": _strip_app(first), "right": _strip_app(second), "vertical": True})
    m = re.fullmatch(r"(?P<a>.+?)(と)(?P<b>.+?)(を)?(上下に|たてに|縦に|縦長に)(並べて|ならべて)(表示して|して)?", c)
    if m:   # 横の「AとBを並べて」より先に見る（「上下に並べて」を横と誤って取らないように）
        return Intent("arrange", {"left": _strip_app(m.group("a")), "right": _strip_app(m.group("b")),
                                  "vertical": True})
    m = re.fullmatch(r"(?P<a>.+?)(と)(?P<b>.+?)(を)?(左右に|横に|半分ずつ|並べて|ならべて)(並べて|ならべて|表示して|して)?", c)
    if m and re.search(r"(並べ|ならべ|左右|半分ずつ)", c):
        return Intent("arrange", {"left": _strip_app(m.group("a")), "right": _strip_app(m.group("b"))})
    return None


def _strip_app(s: str) -> str:
    return re.sub(r"(の(ウィンドウ|画面|窓)|を|で|、)+$", "", s).strip()


_WIN_VERBS = {"閉じて": "close", "とじて": "close", "終了して": "close", "最小化して": "minimize", "最小化": "minimize",
              "しまって": "minimize", "最大化して": "maximize", "最大化": "maximize", "前に出して": "focus",
              "前面に出して": "focus", "表に出して": "focus"}


def _p_named_window(c: str) -> Intent | None:
    for w, act in sorted(_WIN_VERBS.items(), key=lambda kv: -len(kv[0])):
        m = re.fullmatch(r"(?P<n>.{2,30}?)(を|の(ウィンドウ|画面)を?)" + re.escape(w) + r"(ください)?", c)
        if m:
            name = m.group("n")
            if name in ("これ", "それ", "この画面", "このウィンドウ", "今の画面", "今のウィンドウ", "タブ", "このタブ",
                        "ウィンドウ", "画面", "全部", "ダイアログ", "メニュー", "ポップアップ"):
                return None
            return Intent("window_named", {"name": name, "action": act})
    return None


def _p_paste_to(c: str) -> Intent | None:
    """「メモ帳に貼り付けて」→ そのウィンドウを前面にしてから貼り付ける。"""
    m = re.fullmatch(r"(?P<n>.{2,30}?)(に|へ)(貼り付けて|貼りつけて|はりつけて|ペーストして)(ください)?", c)
    if m and m.group("n") not in ("ここ", "そこ", "これ"):
        return Intent("paste_to", {"name": m.group("n")})
    return None


def _p_time(c: str) -> Intent | None:
    if re.fullmatch(r"(今|いま)?(何時|なんじ)(ですか|かな|か|？|\?)?(教えて)?", c):
        return Intent("say_time")
    if re.fullmatch(r"(今日|きょう)(は)?(何日|なんにち|何曜日|なんようび|何月何日)(ですか|かな|か|だっけ)?(教えて)?", c):
        return Intent("say_date")
    return None


_OPS = {"たす": "+", "足す": "+", "プラス": "+", "+": "+", "ひく": "-", "引く": "-", "マイナス": "-", "-": "-",
        "かける": "*", "掛ける": "*", "×": "*", "*": "*", "わる": "/", "割る": "/", "÷": "/", "/": "/"}


def _p_calc(c: str) -> Intent | None:
    ops = "|".join(re.escape(k) for k in sorted(_OPS, key=len, reverse=True))
    m = re.fullmatch(r"(?P<expr>[0-9０-９.〇零一二三四五六七八九十百千万]+((" + ops + r")[0-9０-９.〇零一二三四五六七八九十百千万]+)+)"
                     r"(は|って)?(いくつ|何|なに|なん)?(ですか|？|\?|=|イコール)?(計算して)?", c)
    if not m:
        return None
    parts = re.split("(" + ops + ")", m.group("expr"))
    expr = ""
    for i, p in enumerate(parts):
        if i % 2:
            expr += _OPS[p]
        else:
            v = p.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            if not re.fullmatch(r"[0-9.]+", v):
                n = _kanji_big(v)
                if n is None:
                    return None
                v = str(n)
            expr += v
    return Intent("calc", {"expr": expr})


def _kanji_big(s: str) -> int | None:
    if "万" in s:
        a, _, b = s.partition("万")
        hi = to_int(a) if a else 1
        lo = to_int(b) if b else 0
        return None if hi is None or lo is None else hi * 10000 + lo
    return to_int(s)


def _p_timer(c: str) -> Intent | None:
    m = re.fullmatch(r"(タイマー)?(?P<num>[0-9０-９〇一二三四五六七八九十百]+)(?P<u>分|秒|時間)(半)?(後に|したら|経ったら|たったら)?"
                     r"(知らせて|しらせて|教えて|おしえて|呼んで|起こして|タイマー|のタイマー|アラーム|のアラーム|で)?"
                     r"(を)?(かけて|セットして|設定して|設定|セット|入れて|して|お願い)?(ください|お願い)?", c) \
        or re.fullmatch(r"タイマー(を)?(?P<num>[0-9０-９〇一二三四五六七八九十百]+)(?P<u>分|秒|時間)(半)?(で|に)?"
                        r"(かけて|セットして|設定して|設定|セット|して|お願い)?(ください|お願い)?", c)
    if not m:
        return None
    if not re.search(r"(タイマー|アラーム|知らせ|しらせ|教え|おしえ|呼んで|起こして)", c):
        return None
    n = to_int(m.group("num"))
    if n is None:
        return None
    sec = n * {"秒": 1, "分": 60, "時間": 3600}[m.group("u")]
    if "半" in c:
        sec += {"秒": 0, "分": 30, "時間": 1800}[m.group("u")]
    if not 1 <= sec <= 24 * 3600:
        return None
    return Intent("timer", {"seconds": sec})


def _p_read(c: str) -> Intent | None:
    if re.fullmatch(r"(選択した|選んだ|えらんだ)(ところ|部分|文|文字|テキスト)?(を)?(読んで|読み上げて|よんで|よみあげて)", c) \
            or re.fullmatch(r"(これ|ここ)(を)?(読んで|読み上げて|よみあげて)", c):
        return Intent("read_selection")
    if re.fullmatch(r"クリップボード(の中身|の内容)?(を)?(読んで|読み上げて|よんで)", c):
        return Intent("read_clipboard")
    if re.fullmatch(r"(黙って|だまって|静かにして|うるさい|読み上げ(を)?(止めて|やめて|ストップ))", c):
        return Intent("hush")
    return None


# ---- 2026-09-22 追加（4 回目）：もっと操作・もっと便利 ----
def _p_sel_search(c: str) -> Intent | None:
    """「これで検索して」：選択中の文字を取り出して Web で検索する。"""
    if re.fullmatch(r"(これ|それ|選択した(ところ|部分|文字|テキスト)?|選んだ(ところ|部分)?)(で|を)?"
                    r"(検索して|ググって|調べて|ウェブで調べて)(ください|お願い)?", c):
        return Intent("search_selection")
    return None


def _p_line(c: str) -> Intent | None:
    """「この行を選んで」「行を消して」：カーソルのある行を選択・削除する。"""
    if re.fullmatch(r"(この|今の|その|カーソルの|一)?行(を)?(選んで|選択して|反転して)", c):
        return Intent("line_select")
    if re.fullmatch(r"(この|今の|その|カーソルの|一)?行(を)?(消して|削除して|消去して|消す|削除する)", c):
        return Intent("line_delete")
    return None


def _p_memo(c: str) -> Intent | None:
    """音声メモ：「メモっておいて〇〇」（前置き・後置き両方）、「メモ見せて」「メモ読んで」「メモ全部消して」。"""
    if re.fullmatch(r"メモ(を)?(見せて|みせて|一覧|リスト|確認して|なんかある?)", c):
        return Intent("memo_list")
    if re.fullmatch(r"メモ(を)?(読んで|読み上げて|よんで)", c):
        return Intent("memo_read")
    if re.fullmatch(r"メモ(を)?(全部|すべて|ぜんぶ)?(消して|削除して|クリア|初期化して|まっさらにして)", c):
        return Intent("memo_clear")
    m = re.fullmatch(r"メモ(っておいて|っといて|しておいて|しといて|して|って|にとっと?く|に取って|に追加して)(?P<q>.+)", c)
    if m and len(m.group("q").strip("、。 ")) >= 2:
        return Intent("memo_add", {"q": m.group("q").strip("、。 ")})
    m = re.fullmatch(r"(?P<q>.{2,}?)(を|って|と)(メモして|メモしといて|メモって|メモっといて|メモしておいて)", c)
    if m:
        return Intent("memo_add", {"q": m.group("q")})
    return None


def _p_download(c: str) -> Intent | None:
    """「さっきダウンロードしたのを開いて」：ダウンロードフォルダの最新のファイルを開く。

    「ダウンロードを開いて」（フォルダ・一覧を開く）と区別するため、した／落とした が付くか、
    さっき／最新 のような言葉が付いているときだけ受け付ける。
    """
    if re.fullmatch(r"((さっき|直近の|最後の|最新の)ダウンロード(した|落とした)?|ダウンロード(した|落とした)|(さっき|直近の|最後の|最新の)?落とした)"
                    r"(やつ|ファイル|の)?(を)?(開いて|ひらいて|開く|見せて|みせて|表示して)(ください)?", c):
        return Intent("open_latest_download")
    return None


# ---- 選んでいるファイルの圧縮・展開・チェックサム（エクスプローラーが前面のときだけ） ----
def _p_zip(c: str) -> Intent | None:
    if re.fullmatch(r"(これ|それ|選んだ(もの|ファイル|やつ)|選択した(もの|ファイル)?)?(を)?(zipに|ジップに|圧縮)(して)?(ください)?", c):
        return Intent("zip_selection")
    if re.fullmatch(r"(zip|ジップ)(ファイル)?(を)?(ここに|このフォルダに)?(展開して|解凍して)(してください)?", c):
        return Intent("unzip_selection")
    return None


def zip_files(paths: list[str]) -> str:
    """選んでいるファイル・フォルダを同じ場所に zip でまとめる（Python 標準の zipfile。戻り値は結果の文）。"""
    import os
    import zipfile
    if not paths:
        raise ValueError("選んでいるファイルがありません")
    folder = os.path.dirname(paths[0])
    if len(paths) == 1:
        stem = os.path.splitext(os.path.basename(paths[0]))[0] or "archive"
    else:
        stem = os.path.basename(folder) or "archive"
    name = f"{stem}.zip"
    i = 2
    while os.path.exists(os.path.join(folder, name)):
        name = f"{stem}-{i}.zip"
        i += 1
    dest = os.path.join(folder, name)
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        fp = os.path.join(root, f)
                        # アーカイブ内の名前から「..」を除く（展開先の外に出ないように）
                        rel = "/".join(x for x in os.path.relpath(fp, folder).replace("\\", "/").split("/")
                                       if x not in ("..", "."))
                        z.write(fp, rel or os.path.basename(fp))
                        n += 1
            else:
                z.write(p, os.path.basename(p))
                n += 1
    return f"{name} に {n} 件をまとめました"


def unzip_file(zp: str) -> str:
    """zip を、その隣の zip と同じ名前のフォルダに展開する（上書きしない）。"""
    import os
    import zipfile
    base = os.path.splitext(os.path.basename(zp))[0]
    target = os.path.join(os.path.dirname(zp), base)
    i = 2
    while os.path.exists(target):
        target = os.path.join(os.path.dirname(zp), f"{base}-{i}")
        i += 1
    with zipfile.ZipFile(zp) as z:
        z.extractall(target)
    n = sum(len(files) for _root, _dirs, files in os.walk(target))
    return f"「{os.path.basename(target)}」に {n} 件を展開しました"


def _p_checksum(c: str) -> Intent | None:
    if re.fullmatch(r"(この|選んだ|選択した)?(ファイルの|やつの)?(チェックサム|ハッシュ|sha256|しゃ256)(を)?"
                    r"(見せて|みせて|教えて|おしえて|計算して|出して)?", c, flags=re.I):
        return Intent("checksum_selection")
    return None


def sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---- 画面の撮影（クリップボードへ）・アプリ別の音量・重いプロセス ----
def _p_shot(c: str) -> Intent | None:
    if re.fullmatch(r"画面(全体)?(を)?(撮って|とって)(クリップボード|コピー)(する|に)?", c) \
            or re.fullmatch(r"スクショ(を)?(クリップボード|コピー)(する|に)?", c):
        return Intent("shot_clipboard")
    return None


APP_VOLUME_ALIASES = {
    "chrome": ["クローム", "chrome", "ブラウザ"], "msedge": ["エッジ", "edge", "ブラウザ"],
    "brave": ["ブレイブ", "brave", "ブラウザ"], "firefox": ["ファイアフォックス", "firefox", "ブラウザ"],
    "vlc": ["ビーエルシー", "vlc"], "discord": ["ディスコード", "discord"], "slack": ["スラック", "slack"],
    "spotify": ["スポティファイ", "spotify"], "obsidian": ["オブシディアン", "オブシリアン", "obsidian"],
}
_GLOBAL_VOLUME_WORDS = {"パソコン", "pc", "ぜんぶ", "全体", "全部", "まとめて"}


def app_volume_match(app_text: str, process_name: str) -> bool:
    """声での呼び方（「クローム」「ブラウザ」など）と音のセッションのプロセス名が合うか。"""
    stem = (process_name or "").lower().removesuffix(".exe")
    c = textparse.compact(app_text).lower()
    if not stem or not c:
        return False
    if stem in c or c in stem:
        return True
    return c in [textparse.compact(a).lower() for a in APP_VOLUME_ALIASES.get(stem, [])]


def _p_app_volume(c: str) -> Intent | None:
    m = re.fullmatch(r"(?P<app>[^、。は]{2,20}?)の?(音量|音)(だけ)?(を)?"
                     r"(?P<op>下げて|さげて|上げて|あげて|消して|けして|ミュート|戻して|もとにもどして|元に戻して|最大|ゼロ)", c)
    if m:
        return Intent("app_volume", {"app": m.group("app"), "op": m.group("op")})
    m = re.fullmatch(r"(?P<app>[^、。は]{2,20}?)の?(音量|音)(だけ)?(を)?(?P<level>" + _NUM + r")"
                     r"(パー(セント)?|%|)(に|で)?", c)
    if m:
        n = to_int(m.group("level"))
        if n is None or not 0 <= n <= 100:
            return None
        return Intent("app_volume", {"app": m.group("app"), "op": "level", "level": n})
    m = re.fullmatch(r"(?P<app>[^、。は]{2,20}?)(だけ)?を?ミュート(に|する)?", c)
    if m:
        return Intent("app_volume", {"app": m.group("app"), "op": "ミュート"})
    return None


def _p_proc(c: str) -> Intent | None:
    m = re.fullmatch(r"(メモリ|めもり)(を)?(食ってる|使ってる|くってる|つかってる)(の)?(を)?(見せて|みせて|教えて|おしえて)(ください)?", c)
    if m:
        return Intent("proc_top", {"by": "memory"})
    m = re.fullmatch(r"(CPU|しぷー)(を)?(食ってる|使ってる|くってる|つかってる)(の)?(を)?(見せて|みせて|教えて|おしえて)(ください)?", c)
    if m:
        return Intent("proc_top", {"by": "cpu"})
    if re.fullmatch(r"重い(の)?(を)?(見つけて|見せて|教えて|調べて)", c):
        return Intent("proc_top", {"by": "memory"})
    return None


def proc_top(by: str = "memory") -> list[tuple[str, int, int]]:
    """メモリ（または CPU）をたくさん使っているプロセスの上位 5 件を (名前, 値, PID) で返す。"""
    import psutil
    rows = []
    if by == "cpu":
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)   # 初回の呼び出しは基準を作るだけ
            except Exception:
                continue
        time.sleep(0.5)
        for p in psutil.process_iter(["name"]):
            try:
                v = p.cpu_percent(None)
                if v > 1.0:
                    rows.append((p.info["name"] or "?", int(v), p.pid))
            except Exception:
                continue
    else:
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                mi = p.info["memory_info"]
                if mi and mi.rss >= 80 * 1024 * 1024:
                    rows.append((p.info["name"] or "?", int(mi.rss // (1024 * 1024)), p.pid))
            except Exception:
                continue
    rows.sort(key=lambda x: -x[1])
    return rows[:5]


def process_name_matches(actual: str, expected: str) -> bool:
    """プロセス名の一致判定（.exe を除いて比べる。空のときは変化なしとみなす）。"""
    a = (actual or "").lower().replace(".exe", "")
    e = (expected or "").lower().replace(".exe", "")
    return not a or a == e or (len(e) >= 3 and e in a)


def kill_process(pid: int, name: str) -> None:
    """プロセスを強制終了する。名前が変わっていたら（PID の再利用）中止する。"""
    import psutil
    p = psutil.Process(pid)
    if not process_name_matches(p.name() or "", name):
        raise ValueError(f"プロセスの名前が変わっています（{p.name()}）")
    p.kill()
    p.wait(timeout=3)


def _p_proc_kill(c: str) -> Intent | None:
    """「1番を止めて」：直前に見せた重いプロセスの一覧の n 番を止める（確認あり）。"""
    m = re.fullmatch(r"(?P<n>" + _NUM + r")(番|番目|ばんめ|つ目)?(の)?(プロセス|やつ)?を?(止めて|とめて|終了して|殺して|切って)(ください)?", c)
    if m:
        n = to_int(m.group("n"))
        if n is None or not 1 <= n <= 5:
            return None
        return Intent("proc_kill", {"n": n})
    return None


# ---- タスクバー（Win11 のアプリボタン）----
def _p_taskbar(c: str) -> Intent | None:
    m = re.fullmatch(r"タスクバー(の|から)?(?P<t>" + _NUM + r")(番目|ばんめ|つ目)?(を)?"
                     r"(押して|クリック|開いて|出して|切り替え|切り替えて)?(してください)?", c)
    if m and (m.group(5) or not re.search(r"[0-9０-９]", c) or c.rstrip("してください").endswith(("番目", "ばんめ", "つ目"))):
        n = to_int(m.group("t"))
        if n is None or not 1 <= n <= 20:
            return None
        return Intent("taskbar_click", {"n": n, "name": ""})
    m = re.fullmatch(r"タスクバー(の)?(?P<name>[^、。]{2,16}?)(を)?(押して|クリック|開いて|出して|切り替えて?)?(してください)?", c)
    if m and m.group("name"):
        return Intent("taskbar_click", {"n": 0, "name": m.group("name")})
    return None


# ---- 後で読む（ブラウザの URL を取って state/read_later.txt に積む）----
READ_LATER = Path(__file__).resolve().parent.parent / "state" / "read_later.txt"


def read_later_add(title: str, url: str) -> str:
    READ_LATER.parent.mkdir(exist_ok=True)
    ts = time.strftime("%m-%d %H:%M")
    safe = (title or "").replace("\t", " ").strip()[:60]
    with READ_LATER.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{safe}\t{url}\n")
    return ts


def read_later_lines() -> list[str]:
    if not READ_LATER.exists():
        return []
    return [ln for ln in READ_LATER.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _p_read_later(c: str) -> Intent | None:
    if re.fullmatch(r"(あとで|後で)読む(リスト|一覧)(を)?(見せて|みせて|教えて|確認して)?", c):
        return Intent("read_later_list")
    if re.fullmatch(r"((この|今の|今開いてる)(ページ|記事|サイト)(を)?|これ(を)?)?(あとで|後で)読む"
                    r"(リストに)?(追加して|いれて|入れて)?", c):
        return Intent("read_later")
    return None


HELP = [
    "アプリ・サイト：「〇〇開いて」「YouTubeで〇〇」「〇〇を検索」",
    "画面：「〇〇押して」「番号表示」「下にスクロール」「3回戻って」「行を選んで」「画面を撮ってコピー」",
    "窓：「左にChrome右にObsidian」「Chromeを上Obsidianを下に並べて」「開発の配置」「上半分に」「常に手前に」",
    "数値：「音量30」「Chromeの音量だけ30」「3番目のタブ」「10秒戻して」「5分後に知らせて」",
    "ファイル：「〇〇というファイル開いて」「これをzipに」「zipを展開して」「チェックサム見せて」「Cドライブ開いて」",
    "文字：「〇〇と入力」「書き取り開始」「選択したところ読んで」「これで検索して」",
    "メモ：「メモっておいて〇〇」「メモ見せて」「メモ読んで」",
    "覚える：「〇〇の手順を記録して」〜「記録終了」「〇〇と言ったら△△して」「これ覚えて」",
    "AI：「〇〇のメールを書いて」「これを英語にして」「この画面を要約して」「〇〇って何？」「今日何した？」",
]


# ---- AI（文章アシスタント。assistant.py）----
_SEL = r"(選択した(ところ|部分|文章|文|テキスト)?|選んだ(ところ|部分|文|文章)?|これ|この(文章|文|部分|テキスト|メール|メッセージ))"
# 選択中の文字への操作 → (task, 置き換えるか)
_SEL_OPS = [
    (r"(要約|まとめ|要点)", "summarize", False),
    (r"(英語|英訳)", "translate_en", True),
    (r"(日本語|和訳)", "translate_ja", True),
    (r"(翻訳)", "translate", True),
    (r"(丁寧|敬語|ていねい)", "polite", True),
    (r"(くだけ|カジュアル|砕け|フランク)", "casual", True),
    (r"(短く|簡潔)", "shorten", True),
    (r"(箇条書き)", "bullets", True),
    (r"(校正|誤字|直|なお|添削)", "proofread", True),
    (r"(説明|解説|わかりやすく|分かりやすく|意味)", "explain", False),
    (r"(返信|返事)", "reply", False),
]
_WRITE_KINDS = r"(メール|返信|返事|文章|文面|メッセージ|投稿|ツイート|ポスト|挨拶|あいさつ|自己紹介|お礼|謝罪|お詫び|紹介文|説明文|案内|招待|依頼文|議事録|日報|感想|コメント|キャッチコピー|タイトル|詩|歌詞の案)"


def _p_ai(c: str) -> Intent | None:
    if re.fullmatch(r"(今日|きょう)(は)?(何|なに|なん)(を)?(した|やった|してた|やってた)(っけ|か|の)?(教えて)?", c) \
            or re.fullmatch(r"(今日|きょう)の(振り返り|ふりかえり|まとめ)(を)?(して|見せて|教えて)?", c):
        return Intent("ai_recap")
    m = re.fullmatch(r"(この|今の)(画面|ページ|ウィンドウ|サイト)(を|は|の内容を|の内容は|について|で)?(?P<q>.*?)"
                     r"(要約して|まとめて|説明して|教えて|何|なに|なん|何ができる|何ができますか)(ですか|の|か)?", c)
    if m or re.fullmatch(r"(ここ|これ|画面)(に|には)?(何|なん)(て|と)(書いて|かいて)(ある|あります)(の|か)?", c):
        return Intent("ai_screen", {"q": c})
    m = re.fullmatch(_SEL + r"(を|の|に)?(?P<op>.{1,16}?)(して|にして|で書いて|に書き直して|書き直して|を書いて|を考えて|を作って|にまとめて|て)?(ください|お願い)?", c)
    if m:
        for pat, task, replace in _SEL_OPS:
            if re.search(pat, m.group("op")):
                return Intent("ai_selection", {"task": task, "replace": replace, "request": c})
    if re.search(_WRITE_KINDS + r"(文)?(を|の)?(下書き|案)?(を)?(書いて|作って|考えて|作成して)", c) \
            and not re.search(r"(と|って)(書いて|入力)", c):
        return Intent("ai_write", {"request": c})
    m = re.fullmatch(r"(?P<q>.{2,60}?)(って|とは|は)(何|なに|なん|誰|だれ|どういう意味|どこ|いつ)(ですか|なの|だっけ|？|\?)*", c) \
        or re.fullmatch(r"(?P<q>.{2,60}?)(について|を|の)(教えて|おしえて|説明して)(ください|ほしい)?", c) \
        or re.fullmatch(r"(?P<q>.{2,60}?)(はどうやって|のやり方|の方法|はどうすれば)(.*)", c)
    if m and not re.search(r"(時|分|秒)(に|後に|したら)", c):
        return Intent("ai_ask", {"q": c})
    return None


def is_draft_accept(text: str) -> bool:
    c = textparse.compact(textparse.normalize(text)).rstrip("。！!")
    return bool(re.fullmatch(r"(それで|これで)?(入力して|にゅうりょくして|貼り付けて|はりつけて|打って|書き込んで|送って|"
                             r"いい|オッケー|OK|お願い|おねがい|使って|それでお願い|はい|うん)(よ|ね|ください|お願い)?", c, flags=re.I))


def is_draft_refine(text: str) -> bool:
    c = textparse.compact(textparse.normalize(text)).rstrip("。！!")
    return bool(re.search(r"(もっと|もう少し|もうちょっと|少し)|(短く|長く|丁寧|敬語|カジュアル|くだけ|英語|日本語|箇条書き|詳しく|"
                          r"簡潔|明るく|柔らかく|硬く|フォーマル|書き直|直して|変えて|加えて|入れて|削って|追加)", c)) \
        and not re.fullmatch(r"(入力して|貼り付けて)", c)


def is_draft_cancel(text: str) -> bool:
    c = textparse.compact(textparse.normalize(text)).rstrip("。！!")
    return bool(re.fullmatch(r"(やっぱり)?(いらない|要らない|やめて|やめる|取り消し|取り消して|キャンセル|捨てて|消して|不要)(です)?", c))


# ---- 実行（コントローラから呼ぶ。戻り値は表示する結果の文） ----
def volume(level: int | None = None, delta: int | None = None) -> int:
    from pycaw.pycaw import AudioUtilities
    ep = AudioUtilities.GetSpeakers().EndpointVolume
    now = round(ep.GetMasterVolumeLevelScalar() * 100)
    if level is None and delta is None:
        return now
    new = max(0, min(100, level if level is not None else now + delta))
    ep.SetMasterVolumeLevelScalar(new / 100.0, None)
    if new > 0 and ep.GetMute():
        ep.SetMute(0, None)
    return new


def switch_tab(n: int) -> None:
    winutil.press_combo([0x11, 0x30 + n])   # Ctrl+1..9（9 は最後のタブ）


def seek(seconds: int, process: str) -> str:
    """YouTube（ブラウザ）は ← → が 5 秒、VLC は Shift+← → が 3 秒…と違うので、アプリごとに押す回数を変える。"""
    p = process.lower()
    if p == "vlc.exe":
        step, mods = 10, [0x12]           # Alt+← → は 10 秒
    else:
        step, mods = 5, []                # YouTube などのブラウザの動画は 5 秒
    presses = max(1, round(abs(seconds) / step))
    key = 0x25 if seconds < 0 else 0x27
    for _ in range(min(presses, 60)):
        winutil.press_combo(mods + [key])
        time.sleep(0.04)
    return f"{abs(seconds)} 秒{'戻しました' if seconds < 0 else '進めました'}（{step} 秒 × {presses}）"


def find_in_page(q: str) -> None:
    winutil.press_combo([0x11, 0x46])
    time.sleep(0.35)
    winutil.press_combo([0x11, 0x41])
    winutil.paste_text(q)
    time.sleep(0.1)
    winutil.press_combo([0x0D])


def _index_query(where: str, order: str, limit: int) -> list[str]:
    """Windows Search の索引に問い合わせて、実在するファイルのパスを返す。"""
    import urllib.parse
    import win32com.client
    conn = win32com.client.Dispatch("ADODB.Connection")
    rs = win32com.client.Dispatch("ADODB.Recordset")
    out: list[str] = []
    try:
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        skip = " ".join(f"AND NOT System.ItemUrl LIKE '%{d}%'" for d in ("/AppData/", "/ProgramData/", "/Start Menu/",
                                                                          "/node_modules/", "/.git/", "/site-packages/"))
        rs.Open(f"SELECT TOP {limit * 4} System.ItemUrl FROM SYSTEMINDEX WHERE {where} {skip} {order}", conn)
        while not rs.EOF and len(out) < limit:
            url = rs.Fields.Item(0).Value or ""
            path = urllib.parse.unquote(url[5:]).replace("/", "\\") if url.startswith("file:") else ""
            low = path.lower()
            if path and os.path.exists(path) and not any(x in low for x in _SKIP_DIRS):
                out.append(path)
            rs.MoveNext()
    finally:
        for o in (rs, conn):
            try:
                o.Close()
            except Exception:
                pass
    return out


# 探す対象から外す場所（スタートメニューのショートカット・アプリの内部ファイル）
_SKIP_DIRS = ("\\start menu\\", "\\スタート メニュー\\", "\\appdata\\", "\\programdata\\", "\\$recycle.bin\\",
              "\\node_modules\\", "\\.git\\", "\\site-packages\\", "\\cmakefiles\\")


def _walk_find(roots: list[str], name: str, folder: bool, limit: int, budget: float = 1.5, depth: int = 4) -> list[str]:
    """索引に入っていないドライブ（S: など）を、時間と深さを決めて名前で探す。"""
    n = name.lower()
    out: list[tuple[float, str]] = []
    end = time.monotonic() + budget
    stack = [(r, 0) for r in roots if os.path.isdir(r)]
    while stack and time.monotonic() < end:
        d, lv = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                    low = e.name.lower()
                    if e.is_dir(follow_symlinks=False):
                        if low.startswith((".", "$")) or low in ("node_modules", "__pycache__", "site-packages", "windows", "venv", "env",
                                                                  "program files", "program files (x86)", "appdata"):
                            continue
                        if folder and n in low:
                            out.append((e.stat().st_mtime, e.path))
                        if lv + 1 < depth:
                            stack.append((e.path, lv + 1))
                    elif not folder and n in low:
                        out.append((e.stat().st_mtime, e.path))
            except OSError:
                continue   # 読めない項目（System Volume Information など）は飛ばす
    out.sort(reverse=True)
    return [p for _, p in out[:limit]]


def search_files(name: str, folder: bool = False, limit: int = 8, extra_roots: list[str] | None = None) -> list[str]:
    """名前に name を含むファイル（フォルダ）を、更新日の新しい順に探す。

    まずユーザーフォルダ内を Windows Search の索引で探し、なければ索引全体、それでもなければ
    索引外の場所（extra_roots。既定は S:\）を時間を決めて直接探す。
    """
    safe = name.replace("'", "''").replace("%", "")
    kind = "System.Kind = 'folder'" if folder else "(System.Kind IS NULL OR System.Kind <> 'folder')"
    home = os.path.expanduser("~").replace("\\", "/")
    base = f"System.FileName LIKE '%{safe}%' AND {kind}"
    order = "ORDER BY System.DateModified DESC"
    found: list[str] = []
    try:
        found = _index_query(f"{base} AND SCOPE='file:{home}'", order, limit) or _index_query(base, order, limit)
    except Exception:
        log.exception("Windows Search の索引を使えません")
    if not found and extra_roots:
        found = _walk_find(extra_roots, name, folder, limit)
    return found


def recent_files(exts: list[str], limit: int = 8) -> list[str]:
    """最近更新したファイルを新しい順に返す。「最近使った項目」の記録が無効でも使えるよう、索引の更新日で探す。"""
    home = os.path.expanduser("~").replace("\\", "/")
    cond = " OR ".join(f"System.FileExtension = '{e}'" for e in exts) if exts else         " OR ".join(f"System.FileExtension = '{e}'" for e in _DOC_EXTS)
    try:
        return _index_query(f"SCOPE='file:{home}' AND ({cond})", "ORDER BY System.DateModified DESC", limit)
    except Exception:
        log.exception("Windows Search の索引を使えません")
        return []


_DOC_EXTS = (".docx", ".doc", ".xlsx", ".xls", ".csv", ".pptx", ".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".mp4")


def find_window(name: str) -> winutil.WindowInfo | None:
    """名前（アプリ名・タイトルの一部・カタカナ読み）に一番合う開いているウィンドウ。"""
    from . import phonetic
    best, score = None, 0.0
    n = textparse.compact(name).lower()
    for w in winutil.list_windows():
        proc = w.process.lower().replace(".exe", "")
        title = w.title.lower()
        s = 0.0
        if n and (n == proc or n in title or n in proc):
            s = 1.0 + len(n) / max(len(title), 1)
        else:
            s = max(phonetic.match_ratio(name, w.title.split(" - ")[-1]), phonetic.match_ratio(name, proc),
                    phonetic.ja_ratio(name, w.title.split(" - ")[-1]))
        if s > score:
            best, score = w, s
    return best if score >= 0.75 else None


def place(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    user32 = winutil.user32
    user32.ShowWindow(hwnd, 9)   # SW_RESTORE（最大化中は位置を変えられない）
    l, t, r, b = rect
    user32.SetWindowPos(hwnd, 0, l, t, r - l, b - t, 0x0040 | 0x0004)   # SWP_SHOWWINDOW | SWP_NOZORDER


def arrange(left: winutil.WindowInfo, right: winutil.WindowInfo, vertical: bool = False) -> None:
    cx = (left.rect[0] + left.rect[2]) // 2
    cy = (left.rect[1] + left.rect[3]) // 2
    l, t, r, b = winutil.work_area_at(cx, cy)
    if vertical:
        mid = (t + b) // 2
        place(left.hwnd, (l, t, r, mid))
        place(right.hwnd, (l, mid, r, b))
    else:
        mid = (l + r) // 2
        place(left.hwnd, (l, t, mid, b))
        place(right.hwnd, (mid, t, r, b))
    winutil.focus_window(right.hwnd)
    winutil.focus_window(left.hwnd)


def calc(expr: str) -> str:
    """四則演算の計算（eval は使わない。数値と + - * / と括弧だけを AST で評価する）。"""
    import ast
    import operator as _op
    if len(expr) > 80 or not re.fullmatch(r"[0-9.+\-*/() ]+", expr):
        raise ValueError("計算できない式です")
    ops = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        raise ValueError("計算できない式です")

    try:
        v = ev(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        raise ValueError("0 で割れません")
    except (SyntaxError, ValueError):
        raise ValueError("計算できない式です")
    if isinstance(v, float):
        v = round(v, 6)
        if v == int(v):
            v = int(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


_WEEK = "月火水木金土日"


def say_time() -> str:
    n = _dt.datetime.now()
    return f"{n.hour}時{n.minute}分です"


def say_date() -> str:
    n = _dt.datetime.now()
    return f"{n.month}月{n.day}日 {_WEEK[n.weekday()]}曜日です"


def read_clipboard() -> str:
    import win32clipboard as cb
    cb.OpenClipboard()
    try:
        if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
            return cb.GetClipboardData(cb.CF_UNICODETEXT) or ""
        return ""
    finally:
        cb.CloseClipboard()


def copy_selection() -> str:
    """選択中の文字を Ctrl+C で取り出す（元のクリップボードは戻す）。"""
    import win32clipboard as cb
    before = read_clipboard()
    cb.OpenClipboard()
    try:
        cb.EmptyClipboard()   # 何も選択していないときに前の中身を読んでしまわないよう空にしておく
    finally:
        cb.CloseClipboard()
    winutil.press_combo([0x11, 0x43])
    time.sleep(0.3)
    text = read_clipboard()
    if before:
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            cb.SetClipboardData(cb.CF_UNICODETEXT, before)
        finally:
            cb.CloseClipboard()
    return text


# ---- 行の選択・削除 ----
def select_line() -> None:
    """カーソルのある行を丸ごと選択する（Home → Shift+End）。"""
    winutil.press_combo([0x24])           # Home：行頭へ
    time.sleep(0.02)
    winutil.press_combo([0x10, 0x23])     # Shift+End：行末まで選択


def delete_line() -> None:
    """カーソルのある行の内容を消す（エディタでは「元に戻す」で取り消せる）。"""
    select_line()
    time.sleep(0.02)
    winutil.press_combo([0x2E])           # Delete


# ---- ダウンロードの最新のファイル ----
_DL_TMP_EXTS = {".crdownload", ".tmp", ".part", ".partial", ".download", ".opdownload"}


def _downloads_dir() -> str:
    """ダウンロードフォルダの実際のパス（OneDrive リダイレクトにも対応）。"""
    import ctypes
    import uuid
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    g = _GUID.from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
    buf = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(buf)) != 0:
        raise RuntimeError("ダウンロードフォルダの場所を取得できません")
    path = buf.value
    ctypes.windll.ole32.CoTaskMemFree(buf)
    return path


def latest_download() -> tuple[str, float] | None:
    """ダウンロードフォルダで一番新しいファイル（ダウンロード途中を除く）を (パス, 更新時刻) で返す。"""
    import os
    path = _downloads_dir()
    best, best_m = None, -1.0
    try:
        names = os.listdir(path)
    except OSError:
        return None
    for name in names:
        p = os.path.join(path, name)
        if not os.path.isfile(p) or os.path.splitext(name)[1].lower() in _DL_TMP_EXTS:
            continue
        m = os.path.getmtime(p)
        if m > best_m:
            best, best_m = p, m
    return (best, best_m) if best is not None else None


# ---- 音声メモ（state/memos.txt に 1 行 1 件） ----
MEMOS = Path(__file__).resolve().parent.parent / "state" / "memos.txt"


def memo_add(q: str) -> str:
    """メモを 1 件追記して、書き込んだ時刻を返す。"""
    MEMOS.parent.mkdir(exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M")
    with MEMOS.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{q.replace(chr(10), ' ').replace(chr(9), ' ')}\n")
    return ts


def memo_lines() -> list[str]:
    """メモを古い順に返す（空行は除く）。"""
    if not MEMOS.exists():
        return []
    return [ln for ln in MEMOS.read_text(encoding="utf-8").splitlines() if ln.strip()]


def memo_clear() -> int:
    """メモをすべて消して、消した件数を返す。"""
    n = len(memo_lines())
    if MEMOS.exists():
        MEMOS.unlink()
    return n


# ---- ルーチン ----
def routine_key(name: str) -> str:
    return re.sub(r"(を実行して|を実行|して|する|お願い|ください)$", "", textparse.compact(name).lower().rstrip("。"))


def load_routines(config_routines: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in (config_routines or {}).items():
        out[str(k)] = [str(s) for s in (v if isinstance(v, list) else [v])]
    try:
        if STATE.exists():
            for k, v in json.loads(STATE.read_text(encoding="utf-8")).items():
                out[k] = list(v)
    except Exception:
        log.exception("ルーチンを読めませんでした")
    return out


def save_user_routine(name: str, steps: list[str] | None) -> None:
    data: dict = {}
    try:
        if STATE.exists():
            data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if steps is None:
        for k in [k for k in data if routine_key(k) == routine_key(name)]:
            del data[k]
    else:
        data[name] = steps
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- 読み上げ（Windows 標準の音声合成） ----
class Speaker:
    def __init__(self, enabled: bool = True, rate: int = 1):
        self.enabled = enabled
        self.rate = rate
        self._q: list[str] = []
        self._cv = threading.Condition()
        self._voice = None
        self.busy = False   # 読み上げ中
        if enabled:
            threading.Thread(target=self._loop, daemon=True, name="tts").start()

    def say(self, text: str) -> None:
        if not self.enabled or not text:
            return
        with self._cv:
            self._q.append(text[:2000])
            self._cv.notify()

    def hush(self) -> None:
        with self._cv:
            self._q.clear()
        if self._voice is not None:
            try:
                self._voice.Speak("", 2)   # SVSFPurgeBeforeSpeak：読み上げ中の文を止める
            except Exception:
                pass

    def _loop(self) -> None:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._voice.Rate = self.rate
            for v in self._voice.GetVoices():   # 日本語の声があれば使う
                if "japan" in v.GetDescription().lower() or "日本" in v.GetDescription() or "haruka" in v.GetDescription().lower():
                    self._voice.Voice = v
                    break
        except Exception:
            log.exception("音声合成を使えません")
            self.enabled = False
            return
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                text = self._q.pop(0)
            self.busy = True
            try:
                self._voice.Speak(text, 1)   # SVSFlagsAsync
                while not self._voice.WaitUntilDone(100):
                    if not self.enabled:
                        break
            except Exception:
                log.exception("読み上げに失敗")
            finally:
                self.busy = bool(self._q)
