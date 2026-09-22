"""複数の手順が必要な「目的」を、決まった手順のひな形で実行する。

Jev は選択肢から 1 つを選ぶモデルなので、「YouTube を開いてリュウジさんの動画までたどり着いて」のような
手順の組み立てはできない。よくある目的はここでひな形として持ち、発話から検索語などを切り出して実行する。
  - 「YouTube で〇〇（の動画）」       → YouTube を〇〇で検索し、最初の動画を開く
  - 「〇〇を検索／調べて／ググって」   → Google で検索
  - 「Amazon で〇〇を探して」          → Amazon で検索
  - 「YouTube 開いて」「Gmail 開いて」 → そのサイトを開く
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass

from . import textparse, winutil

log = logging.getLogger(__name__)

BROWSERS = ("chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe")

# サイト名（言い方）→ (表示名, 開く URL, 検索 URL（{q} に検索語）)
SITES: dict[str, tuple[str, str, str | None]] = {
    "youtube": ("YouTube", "https://www.youtube.com/", "https://www.youtube.com/results?search_query={q}"),
    "google": ("Google", "https://www.google.com/", "https://www.google.com/search?q={q}"),
    "amazon": ("Amazon", "https://www.amazon.co.jp/", "https://www.amazon.co.jp/s?k={q}"),
    "gmail": ("Gmail", "https://mail.google.com/", None),
    "github": ("GitHub", "https://github.com/", "https://github.com/search?q={q}"),
    "x": ("X", "https://x.com/", "https://x.com/search?q={q}"),
    "wikipedia": ("Wikipedia", "https://ja.wikipedia.org/", "https://ja.wikipedia.org/w/index.php?search={q}"),
    "maps": ("Google マップ", "https://www.google.com/maps", "https://www.google.com/maps/search/{q}"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com/", None),
    "claude": ("Claude", "https://claude.ai/", None),
    "netflix": ("Netflix", "https://www.netflix.com/browse", "https://www.netflix.com/search?q={q}"),
    "prime": ("Prime Video", "https://www.amazon.co.jp/gp/video/storefront",
              "https://www.amazon.co.jp/s?k={q}&i=instant-video"),
    "spotify": ("Spotify", "https://open.spotify.com/", "https://open.spotify.com/search/{q}"),
    "music": ("YouTube Music", "https://music.youtube.com/", "https://music.youtube.com/search?q={q}"),
    "yahoo": ("Yahoo!", "https://www.yahoo.co.jp/", "https://search.yahoo.co.jp/search?p={q}"),
    "bing": ("Bing", "https://www.bing.com/", "https://www.bing.com/search?q={q}"),
    "rakuten": ("楽天", "https://www.rakuten.co.jp/", "https://search.rakuten.co.jp/search/mall/{q}/"),
    "mercari": ("メルカリ", "https://jp.mercari.com/", "https://jp.mercari.com/search?keyword={q}"),
    "nicovideo": ("ニコニコ", "https://www.nicovideo.jp/", "https://www.nicovideo.jp/search/{q}"),
    "qiita": ("Qiita", "https://qiita.com/", "https://qiita.com/search?q={q}"),
    "zenn": ("Zenn", "https://zenn.dev/", "https://zenn.dev/search?q={q}"),
    "gdrive": ("Google ドライブ", "https://drive.google.com/", "https://drive.google.com/drive/search?q={q}"),
    "gphotos": ("Google フォト", "https://photos.google.com/", None),
    "gcal": ("Google カレンダー", "https://calendar.google.com/", None),
    "gdocs": ("Google ドキュメント", "https://docs.google.com/document/", None),
    "gsheets": ("Google スプレッドシート", "https://docs.google.com/spreadsheets/", None),
    "notion": ("Notion", "https://www.notion.so/", None),
    "instagram": ("Instagram", "https://www.instagram.com/", None),
    "facebook": ("Facebook", "https://www.facebook.com/", None),
    "threads": ("Threads", "https://www.threads.net/", None),
    "tiktok": ("TikTok", "https://www.tiktok.com/", "https://www.tiktok.com/search?q={q}"),
    "reddit": ("Reddit", "https://www.reddit.com/", "https://www.reddit.com/search/?q={q}"),
    "pixiv": ("pixiv", "https://www.pixiv.net/", "https://www.pixiv.net/tags/{q}"),
    "note": ("note", "https://note.com/", "https://note.com/search?q={q}"),
    "canva": ("Canva", "https://www.canva.com/", None),
    "figma": ("Figma", "https://www.figma.com/", None),
    "twitch": ("Twitch", "https://www.twitch.tv/", "https://www.twitch.tv/search?term={q}"),
    "outlook": ("Outlook", "https://outlook.live.com/", None),
    "linkedin": ("LinkedIn", "https://www.linkedin.com/", None),
    "tver": ("TVer", "https://tver.jp/", None),
    "abema": ("ABEMA", "https://abema.tv/", None),
    "deepl": ("DeepL", "https://www.deepl.com/translator", None),
    "translate": ("Google 翻訳", "https://translate.google.com/", "https://translate.google.com/?text={q}"),
    "excel": ("Excel", "https://www.office.com/launch/excel", None),
    "powerpoint": ("PowerPoint", "https://www.office.com/launch/powerpoint", None),
}
# デスクトップ版のアプリもあるサービス（インストールされていればアプリを優先する）
APP_LIKE = {"notion", "spotify", "figma", "canva", "outlook", "x", "instagram", "tiktok", "linkedin", "netflix",
            "prime", "chatgpt", "claude", "deepl", "twitch", "music", "threads", "facebook", "excel", "powerpoint"}
_SITE_WORDS = {
    "youtube": ["YouTube", "ユーチューブ", "ようつべ"],
    "google": ["Google", "グーグル", "ググって"],
    "amazon": ["Amazon", "アマゾン"],
    "gmail": ["Gmail", "ジーメール"],
    "github": ["GitHub", "ギットハブ"],
    "x": ["ツイッター", "Twitter", "エックス"],
    "wikipedia": ["Wikipedia", "ウィキペディア", "ウィキ"],
    "maps": ["Googleマップ", "グーグルマップ", "マップ", "地図"],
    "chatgpt": ["ChatGPT", "チャットGPT"],
    "claude": ["Claude", "クロード"],
    "netflix": ["Netflix", "ネットフリックス", "ネトフリ"],
    "prime": ["PrimeVideo", "プライムビデオ", "Amazonプライム"],
    "spotify": ["Spotify", "スポティファイ", "スポチファイ"],
    "music": ["YouTubeMusic", "ユーチューブミュージック"],
    "yahoo": ["Yahoo", "ヤフー"],
    "bing": ["Bing", "ビング"],
    "rakuten": ["楽天", "楽天市場"],
    "mercari": ["メルカリ"],
    "nicovideo": ["ニコニコ", "ニコニコ動画"],
    "qiita": ["Qiita", "キータ"],
    "zenn": ["Zenn", "ゼン"],
    "gdrive": ["Googleドライブ", "グーグルドライブ"],
    "gphotos": ["Googleフォト", "グーグルフォト"],
    "gcal": ["Googleカレンダー", "グーグルカレンダー"],
    "gdocs": ["Googleドキュメント", "グーグルドキュメント"],
    "gsheets": ["Googleスプレッドシート", "グーグルスプレッドシート", "スプレッドシート"],
    "notion": ["Notion", "ノーション"],
    "instagram": ["Instagram", "インスタグラム", "インスタ"],
    "facebook": ["Facebook", "フェイスブック"],
    "threads": ["Threads", "スレッズ"],
    "tiktok": ["TikTok", "ティックトック"],
    "reddit": ["Reddit", "レディット"],
    "pixiv": ["pixiv", "ピクシブ"],
    "note": ["note.com", "ノートドットコム"],   # 「ノート」だけはメモの意味と区別できないので含めない
    "canva": ["Canva", "キャンバ", "キャンヴァ"],
    "figma": ["Figma", "フィグマ"],
    "twitch": ["Twitch", "ツイッチ"],
    "outlook": ["Outlook", "アウトルック"],
    "linkedin": ["LinkedIn", "リンクトイン"],
    "tver": ["TVer", "ティーバー"],
    "abema": ["ABEMA", "アベマ"],
    "deepl": ["DeepL", "ディープエル"],
    "translate": ["Google翻訳", "グーグル翻訳"],
    "excel": ["Excel", "エクセル"],
    "powerpoint": ["PowerPoint", "パワーポイント", "パワポ"],   # Word は「パスワード」と区別できないので含めない
}
# 1 文字の名前（「X」）は部分一致させると何にでも当たるので、文全体が「X（を）開いて」のときだけ認める
_SHORT_SITE = {"x": r"(x|Ｘ)(という|って)?(サイト|アプリ)?(を|の)?(開いて|ひらいて|開く|開け|表示して|見せて)(ください)?"}
# 「〇〇というサイトを開いて」「〇〇の公式サイト開いて」→ 名前で検索して一番目のサイトを開く
_WEB_NAMED = (r"(?P<n>.+?)(っていう|という|って)(公式)?(サイト|ホームページ|ウェブサイト|webサイト|ページ)(を|に)?"
              r"(開いて|ひらいて|開く|行って|いって|表示して|見せて|出して)(ください)?")
_WEB_OF = (r"(?P<n>.+?)の(公式サイト|公式ページ|サイト|ホームページ|ウェブサイト|webサイト)(を|に)?"
           r"(開いて|ひらいて|開く|行って|いって|表示して|見せて|出して)(ください)?")
# 名前から一番目のサイトへ直接移動する（DuckDuckGo の「\」検索＝最初の結果へリダイレクト）
LUCKY_URL = "https://duckduckgo.com/?q=%5C{q}"

# 特殊フォルダ（「ダウンロードを開いて」など）→ (表示名, シェルの場所)
LOCATIONS: dict[str, tuple[str, str]] = {
    "downloads": ("ダウンロード", "shell:Downloads"),
    "documents": ("ドキュメント", "shell:Personal"),
    "pictures": ("ピクチャ", "shell:My Pictures"),
    "music": ("ミュージック", "shell:My Music"),
    "videos": ("ビデオ", "shell:My Video"),
    "desktop": ("デスクトップ", "shell:Desktop"),
    "recycle": ("ごみ箱", "shell:RecycleBinFolder"),
}
_LOCATION_WORDS = {
    "downloads": ["ダウンロード", "ダウンロードフォルダ"],
    "documents": ["ドキュメント", "マイドキュメント"],
    "pictures": ["ピクチャ", "ピクチャフォルダ", "画像フォルダ"],
    "music": ["ミュージック", "ミュージックフォルダ"],
    "videos": ["ビデオフォルダ"],
    "desktop": ["デスクトップフォルダ"],   # 「デスクトップ」だけは Win+D（デスクトップ表示）なので含めない
    "recycle": ["ごみ箱", "ゴミ箱"],
}
_LOCATION_TAIL = r"(フォルダ)?(を)?(開いて|ひらいて|開き|表示して|見せて|出して|だして|開く)?"


# 「Brave で」「Chrome で」など、開くブラウザの指定 → 実行ファイル名（App Paths に登録されている名前）
_BROWSER_WORDS = {"brave.exe": ["Brave", "ブレイブ"], "chrome.exe": ["Chrome", "クローム", "Google Chrome"],
                  "msedge.exe": ["Edge", "エッジ"], "firefox.exe": ["Firefox", "ファイアフォックス"]}


@dataclass
class Task:
    kind: str            # "open_site" | "search" | "youtube_video" | "open_location" | "open_web" | "open_drive"
    site: str
    query: str = ""
    browser: str | None = None   # 開くブラウザ（指定がなければ前面のブラウザか既定のブラウザ）

    def describe(self) -> str:
        name = (LOCATIONS[self.site][0] if self.site in LOCATIONS else
                SITES[self.site][0] if self.site in SITES else self.site)
        where = f"（{self.browser.replace('.exe', '')}）" if self.browser else ""
        if self.kind == "open_location":
            return f"{name} を開く"
        if self.kind == "open_drive":
            return f"{self.site} ドライブを開く"
        if self.kind == "open_web":
            return f"「{self.query}」のサイトを開く{where}"
        if self.kind == "open_site":
            return f"{name} を開く{where}"
        if self.kind == "youtube_video":
            return f"YouTube で「{self.query}」の動画を開く{where}"
        return f"{name} で「{self.query}」を検索{where}"


# 「Cドライブ開いて」「Dドライブ」（ドライブのルートをエクスプローラーで開く）
_DRIVE_RE = re.compile(r"^(?P<drive>[a-zａ-ｚ])ドライブ(を)?(開いて|ひらいて|開く|見せて|みせて|表示して)?(ください)?$",
                       re.IGNORECASE)


def location_of(text: str) -> str | None:
    """「ダウンロード」「ごみ箱」など、特殊フォルダを開く言い方かどうか（キーを返す）。"""
    c = textparse.compact(text).rstrip("。．. ")
    for key, words in _LOCATION_WORDS.items():
        for w in sorted(words, key=len, reverse=True):
            if re.fullmatch(re.escape(w) + _LOCATION_TAIL, c, flags=re.I):
                return key
    return None


def _site_of(text: str) -> tuple[str, str] | None:
    """発話に含まれるサイト名。(サイトのキー, 一致した言い方)。長い言い方を優先。"""
    best = None
    t = re.sub(r"\s", "", text).lower()  # 「Google マップ」のような空白入りでも一致させる
    for key, words in _SITE_WORDS.items():
        for w in words:
            if w.lower() in t and (best is None or len(w) > len(best[1])):
                best = (key, w)
    return best


def _site_key_exact(name: str) -> str | None:
    """サイト名そのもの（「X」「エックス」「楽天」）からサイトのキー。1 文字の X もここでは認める。"""
    n = re.sub(r"\s", "", name).lower()
    if n in ("x", "ｘ", "エックス", "ツイッター", "twitter"):
        return "x"
    for key, words in _SITE_WORDS.items():
        if any(w.lower() == n for w in words):
            return key
    return None


def _site_of_title(title: str) -> str | None:
    """前面のウィンドウ名から、表示中のサイト（検索できるもの）を推定する。"""
    t = title.lower()
    if re.search(r"( / x| on x)(\s|$)", t):
        return "x"
    for key in ("youtube", "amazon", "rakuten", "github", "wikipedia", "mercari", "netflix", "spotify", "reddit"):
        name = SITES[key][0].lower()
        if name in t or any(w.lower() in t for w in _SITE_WORDS.get(key, [])):
            return key
    return None


def _clean_query(q: str) -> str:
    q = re.sub(r"^(ここ|このサイト|このページ|この画面)(で|から)", "", q)
    q = re.sub(r"^(で|の|を|から|って|、|\s)+", "", q)
    q = re.sub(r"(さん|ちゃん|くん|氏)(の)?$", "", q)
    q = re.sub(r"(について|に関して|のこと)$", "", q)
    q = re.sub(r"(の|を|って|、|\s)+$", "", q)
    return q.strip()


_VIDEO_GOAL = r"(の)?(動画|ビデオ|チャンネル)?(まで|に|を)?(たどり着いて|辿り着いて|行って|いって|開いて|ひらいて|再生して|見せて|見たい|出して|探して|検索して)?"
_SEARCH_VERB = r"(を|で)?(検索して|検索|調べて|しらべて|探して|さがして|ググって)"


def parse(text: str, window_title: str = "") -> Task | None:
    """発話が目的のひな形に当てはまれば Task を返す。window_title は前面のウィンドウ名（YouTube を表示中かの判断用）。"""
    t = textparse.normalize(text).rstrip("。．. ")
    c = textparse.compact(t)
    if textparse.extract_text(t) is not None:  # 「YouTube と入力して」は文字入力
        return None
    # 「ダウンロードを開いて」「ごみ箱を見せて」など、特別なフォルダを開く
    loc = location_of(c)
    if loc:
        return Task("open_location", loc)
    # 「Cドライブ開いて」（ドライブのルート）
    m = _DRIVE_RE.fullmatch(c)
    if m:
        return Task("open_drive", m.group("drive").upper())
    # 開くブラウザの指定（「Brave で」）。ブラウザ名はサイト名・検索語から外す
    browser = None
    for exe, words in _BROWSER_WORDS.items():
        for w in sorted(words, key=len, reverse=True):
            if re.search(re.escape(w) + r"(で|を使って|から)", c, flags=re.I):
                browser = exe
                c = re.sub(re.escape(w) + r"(で|を使って|から)", "", c, count=1, flags=re.I)
                break
        if browser:
            break

    for key, pat in _SHORT_SITE.items():
        if re.fullmatch(pat, c, flags=re.I):
            return Task("open_site", key, "", browser)
    # 「X内の検索でMiniMaxを検索して」「楽天の中で掃除機を探して」：サイト名＋内／の中 → そのサイトで検索
    m = re.fullmatch(r"(?P<site>.{1,20}?)(内|の中|の検索|内の検索|のサイト内)*(で|から)(?P<q>.+?)" + _SEARCH_VERB
                     + r"(ください|お願い)?", c, flags=re.I)
    if m:
        site_name = m.group("site")
        # 「ここで〇〇を検索」「このサイトで…」は、前面のページのサイトで検索する
        if re.fullmatch(r"(ここ|このサイト|このページ|この画面)", site_name):
            key = _site_of_title(window_title)
        else:
            key = _site_key_exact(site_name)
        q = _clean_query(m.group("q"))
        if key and q and SITES[key][2]:
            return Task("search", key, q, browser)
    m = re.fullmatch(_WEB_NAMED, c, flags=re.I) or re.fullmatch(_WEB_OF, c, flags=re.I)
    if m:
        name = _clean_query(m.group("n"))
        known = _site_of(name)
        if known and known[0] in SITES and textparse.compact(known[1]).lower() == name.lower():
            return Task("open_site", known[0], "", browser)
        if name:
            return Task("open_web", name, name, browser)

    site = _site_of(c)
    if site is None and "youtube" in window_title.lower() and re.search(r"(動画|チャンネル|再生)", c):
        site = ("youtube", "")  # YouTube を表示中に「〇〇さんの動画まで…」と言った

    if site and site[0] in SITES:
        key = site[0]
        # サイト名と「を開いて」「で」などを取り除いた残りが検索語（「Google マップを開いてオーブケーキを表示して」）
        rest = c
        for w in sorted(_SITE_WORDS[key], key=len, reverse=True):
            rest = re.sub(re.escape(w) + r"(を開いて|を開き|開いて|を表示して|で|の|を)?", "|", rest, flags=re.I)
        body = "".join(p for p in rest.split("|") if p)
        tail = (_VIDEO_GOAL if key == "youtube" else
                r"(の場所|の地図|のページ)?(を|で)?(検索して|検索|調べて|探して|さがして|表示して|見せて|出して|開いて|"
                r"ひらいて|行って|かけて|流して|再生して|見て|聞いて)?(ください|お願い)?")
        strip_tail = (r"(検索して|検索|調べて|探して|表示して|見せて|開いて|出して|かけて|流して|再生して|見て|聞いて)$")
        while True:  # 「検索して開いて」のように動詞が続くことがあるので、なくなるまで取り除く
            new_body = re.sub(tail + "$", "", body)
            new_body = re.sub(strip_tail, "", new_body)
            if new_body == body:
                break
            body = new_body
        q = _clean_query(body)
        if q and SITES[key][2]:
            return Task("youtube_video" if key == "youtube" else "search", key, q, browser)
        return Task("open_site", key, "", browser)

    # サイト名なし：「〇〇を検索して」「〇〇について調べて」→ Google
    m = re.fullmatch(r"(?P<q>.+?)(について|のこと)?" + _SEARCH_VERB + r"(ください|お願い)?", c)
    if m:
        q = _clean_query(m.group("q"))
        if q and (len(q) >= 2 or not q.isascii()):   # 「猫」のような 1 文字の漢字も検索語にする
            return Task("search", "google", q)
    return None


_OPEN_NAME = r"(?P<n>[A-Za-z0-9][A-Za-z0-9.\-+ ]{0,30}|[ァ-ヴー・]{3,20})(を|の)?(開いて|ひらいて|起動して|立ち上げて|開く)(ください)?"


# 画面の中の物を指すことが多いカタカナ語（サイト名として扱わない）
_NOT_NAMES = {"ファイル", "フォルダ", "フォルダー", "メニュー", "タブ", "ページ", "ウィンドウ", "アプリ", "ブラウザ",
              "ブラウザー", "リンク", "メール", "メモ", "ノート", "ダウンロード", "ドキュメント", "ピクチャ", "ミュージック",
              "ビデオ", "デスクトップ", "ツール", "ヘルプ", "オプション", "エディタ", "エディター", "ターミナル",
              "コンソール", "カメラ", "カレンダー", "マップ", "フォト", "ゲーム", "チャット", "リスト", "ボタン",
              "ダイアログ", "パネル", "サイドバー", "プロパティ", "コントロールパネル", "ストア", "ニュース", "新しいタブ"}


def web_guess(text: str) -> Task | None:
    """インストールされていないアプリ名で「〇〇開いて」と言われたときの代わり（その名前のサイトを開く）。

    英字の名前か 3 文字以上のカタカナの名前で、文全体が「〇〇（を）開いて」のときだけ返す。
    """
    t = textparse.normalize(text).rstrip("。．. ")
    c = textparse.compact(t)
    browser = None
    for exe, words in _BROWSER_WORDS.items():
        for w in sorted(words, key=len, reverse=True):
            if re.search(re.escape(w) + r"(で|を使って|から)", c, flags=re.I):
                browser = exe
                c = re.sub(re.escape(w) + r"(で|を使って|から)", "", c, count=1, flags=re.I)
                break
    m = re.fullmatch(_OPEN_NAME, c)
    if not m or m.group("n").strip().lower() in _NOT_NAMES:
        return None
    return Task("open_web", m.group("n").strip(), m.group("n").strip(), browser)


def first_youtube_video(query: str, timeout: float = 5.0) -> str | None:
    """YouTube の検索結果ページを取得して、最初の動画の URL を返す。取れなければ None。"""
    import httpx
    try:
        # 相手は youtube.com に固定（クエリはパラメータとして渡す。自由な URL には取りに行かない）
        r = httpx.get("https://www.youtube.com/results", params={"search_query": query, "hl": "ja"},
                      timeout=timeout, follow_redirects=True,
                      headers={"Accept-Language": "ja-JP,ja;q=0.9", "User-Agent": "Mozilla/5.0"})
        m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', r.text) or re.search(r'"videoId":"([\w-]{11})"', r.text)
        return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None
    except Exception as e:
        log.warning("YouTube の検索結果を取得できません: %s", e)
        return None


def open_url(url: str, browser: str | None = None, info: dict | None = None) -> str:
    """ブラウザの指定があればそのブラウザで開く。前面がブラウザならその新しいタブで開く。
    それ以外は既定のブラウザで開く。info に開いた先（url）を書き戻す（「さっきのページ」用）。"""
    if info is not None:
        info["url"] = url
    fg = winutil.foreground_window()
    if browser and not (fg and fg.process.lower() == browser):
        import win32api
        win32api.ShellExecute(0, "open", browser, url, None, 1)  # App Paths に登録された brave.exe などを起動
        return browser.replace(".exe", "")
    if fg and fg.process.lower() in BROWSERS:
        winutil.press_combo([0x11, 0x54])  # Ctrl+T（新しいタブ。見ているページを置き換えない）
        time.sleep(0.3)
        winutil.paste_text(url)
        time.sleep(0.1)
        winutil.press_combo([0x0D])
        return f"{fg.process.replace('.exe', '')} の新しいタブ"
    os.startfile(url)
    return "既定のブラウザ"


def run(task: Task, info: dict | None = None) -> str:
    """実行して、実際に開いた先の説明を返す。info には開いた URL などを書き戻す。"""
    if task.kind == "open_location":
        name, target = LOCATIONS[task.site]
        if info is not None:
            info["location"] = name
        os.startfile(target)   # shell:Downloads などの特殊フォルダをエクスプローラーで開く
        return f"{name} を開きました"
    if task.kind == "open_drive":
        os.startfile(f"{task.site}:\\")
        return f"{task.site} ドライブを開きました"
    if task.kind == "open_web":
        how = open_url(LUCKY_URL.format(q=urllib.parse.quote(task.query)), task.browser, info)
        return f"「{task.query}」のサイトを開きました（{how}）"
    name, home, search = SITES[task.site]
    if task.kind == "open_site":
        how = open_url(home, task.browser, info)
        return f"{name} を開きました（{how}）"
    if task.kind == "youtube_video":
        video = first_youtube_video(task.query)
        if video:
            how = open_url(video, task.browser, info)
            return f"「{task.query}」の最初の動画を開きました（{how}）"
        open_url(search.format(q=urllib.parse.quote(task.query)), task.browser, info)
        return f"「{task.query}」の検索結果を開きました（動画を特定できませんでした）"
    how = open_url(search.format(q=urllib.parse.quote(task.query)), task.browser, info)
    return f"{name} で「{task.query}」を検索しました（{how}）"
