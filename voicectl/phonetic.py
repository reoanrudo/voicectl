"""カタカナと英単語の発音照合。

音声認識は英語のメニュー名や固有名詞をカタカナで返すことが多い（「トレーナー」「ストラテジー」）。
カタカナをローマ字にし、英単語と一緒に「子音の並び」（骨格）に直して比べる。
  トレーナー → toreenaa → t r n      trainer → t r n（語末・子音前の r は日本語で伸ばし棒になるので落とす）
  ストラテジー → sutorateji → s t r t s   strategy → s t r t s
似た音はまとめる（k/g、t/d/ts、s/z/j/sh/ch、p/b/v、h/f、r/l）。

漢字かな交じりの日本語どうしも、pykakasi で読み（カタカナ）に直してから同じ骨格で比べる
（ja_ratio）。漢字の同音異義・かな/漢字・長音（おー/おお/おう）の書き方の違いで一致を逃さないため。
"""
from __future__ import annotations

import re
from functools import lru_cache

_KANA = {
    "キャ": "kya", "キュ": "kyu", "キョ": "kyo", "シャ": "sha", "シュ": "shu", "ショ": "sho", "チャ": "cha",
    "チュ": "chu", "チョ": "cho", "ニャ": "nya", "ニュ": "nyu", "ニョ": "nyo", "ヒャ": "hya", "ヒュ": "hyu",
    "ヒョ": "hyo", "ミャ": "mya", "ミュ": "myu", "ミョ": "myo", "リャ": "rya", "リュ": "ryu", "リョ": "ryo",
    "ギャ": "gya", "ギュ": "gyu", "ギョ": "gyo", "ジャ": "ja", "ジュ": "ju", "ジョ": "jo", "ビャ": "bya",
    "ビュ": "byu", "ビョ": "byo", "ピャ": "pya", "ピュ": "pyu", "ピョ": "pyo", "ティ": "ti", "ディ": "di",
    "トゥ": "tu", "ドゥ": "du", "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "ヴァ": "va", "ヴィ": "vi",
    "ヴェ": "ve", "ヴォ": "vo", "ウィ": "wi", "ウェ": "we", "ウォ": "wo", "シェ": "she", "ジェ": "je", "チェ": "che",
    "デュ": "dyu", "テュ": "tyu", "フュ": "fyu", "イェ": "ye", "クァ": "kwa", "クィ": "kwi", "クェ": "kwe", "クォ": "kwo",
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o", "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so", "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te",
    "ト": "to", "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no", "ハ": "ha", "ヒ": "hi", "フ": "fu",
    "ヘ": "he", "ホ": "ho", "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo", "ヤ": "ya", "ユ": "yu",
    "ヨ": "yo", "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro", "ワ": "wa", "ヲ": "o", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go", "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze",
    "ゾ": "zo", "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do", "バ": "ba", "ビ": "bi", "ブ": "bu",
    "ベ": "be", "ボ": "bo", "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po", "ヴ": "vu",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o", "ャ": "ya", "ュ": "yu", "ョ": "yo", "ッ": "", "ー": "",
}
_KANA_KEYS = sorted(_KANA, key=len, reverse=True)
_STOP = {"the", "a", "an", "of", "to", "in", "on", "from", "and", "or", "for", "with", "by", "at", "all",
         "current", "only", "this", "etc", "is", "as", "new", "other", "same", "it"}


def kana_to_romaji(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        for k in _KANA_KEYS:
            if s.startswith(k, i):
                out.append(_KANA[k])
                i += len(k)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


# 似た子音をまとめる（先に 2 文字の組を処理する）
_GROUPS = [("sh", "s"), ("ch", "s"), ("ts", "t"), ("th", "s"), ("ph", "h"), ("ck", "k"), ("wh", "w"), ("gh", ""),
           ("kn", "n"), ("qu", "k"), ("x", "ks"), ("q", "k"), ("c", "k"), ("g", "k"), ("d", "t"), ("z", "s"),
           ("j", "s"), ("b", "p"), ("v", "p"), ("f", "h"), ("l", "r")]


def _skeleton(romaji: str) -> str:
    s = romaji
    for a, b in _GROUPS:
        s = s.replace(a, b)
    s = re.sub(r"[aeiouyw]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)  # 同じ子音の連続は 1 つに


def skeleton_kana(kana: str) -> str:
    return _skeleton(kana_to_romaji(kana))


def skeleton_en(word: str) -> str:
    w = re.sub(r"[^a-z]", "", word.lower())
    w = re.sub(r"(?<!s)s$", "", w) if len(w) > 4 else w  # 複数形 blockers → blocker、ranges → range（ss は残す）
    w = re.sub(r"tion", "shon", w)              # configuration → コンフィギュレーション
    w = re.sub(r"ture", "chur", w)              # structure → ストラクチャー
    w = re.sub(r"c(?=[eiy])", "s", w)           # center → senter
    w = re.sub(r"g(?=[eiy])", "j", w)           # range → ranje
    w = re.sub(r"(?<=[aeiou])r(?=[^aeiou]|$)", "", w)  # trainer → traine、start → stat（日本語の長音に当たる r を落とす）
    return _skeleton(w)


def content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z]{3,}", text) if w.lower() not in _STOP]


def utterance_skeleton(text: str) -> str:
    """発話のカタカナ部分と英字部分を骨格にしてつなげる（語順どおり）。"""
    parts = re.findall(r"[ァ-ヶー]+|[A-Za-z]+", text)
    skel = [skeleton_kana(p) if re.match(r"[ァ-ヶ]", p) else skeleton_en(p) for p in parts]
    return "|".join(s for s in skel if s)   # 長音だけ（「ー」）の部分は骨格にならないので落とす


def ja_skeleton(text: str) -> str:
    """発話ぜんぶの「読みの骨格」。学習した言い方を思い出すときの照合に使う。

    漢字かな交じりは読み（カタカナ）に直してから子音の並びにするので、漢字の同音異義
    （「保管」/「補完」）や長音の書き方（おー/おお/おう）、促音の抜け、カタカナ／ひらがなの
    書き分けを吸収できる。英字で書かれた語だけは読みに混ざると崩れるので、別に骨格を作って
    後ろに足す（カタカナで書いた同じ語は読みの側で同じ骨格になる）。
    """
    parts = []
    reading = _reading(text)
    if reading:
        parts.append(skeleton_kana(reading))
    en = utterance_skeleton(" ".join(re.findall(r"[A-Za-z]+", text)))
    if en:
        parts.append(en)
    return "|".join(p for p in parts if p.strip("|"))


def menu_match(text: str, path: list[str]) -> float:
    """メニュー項目との発音の一致度。項目名（最後の要素）を重く、上位のメニュー名を軽く見る。"""
    leaf = match_ratio(text, path[-1])
    whole = match_ratio(text, " ".join(path))
    return max(leaf, whole * 0.8)


def match_ratio(text: str, label: str) -> float:
    """label の英語の内容語のうち、発話（カタカナ・英字）に発音が含まれているものの割合。英語の内容語がなければ 0。"""
    words = content_words(label)
    if not words:
        return 0.0
    tokens = [t for t in utterance_skeleton(text).split("|") if t]
    u = "".join(tokens)
    if not u:
        return 0.0

    def hit(sk: str) -> bool:
        # 子音 3 つ以上なら部分一致でよい（「ルートノード」の中の node など）。
        # 2 つ以下は短すぎて偶然含まれやすい（「ボタン」p-t-n の中の Board p-t）ので、語と完全に一致したときだけ
        return (len(sk) >= 3 and sk in u) or (len(sk) == 2 and sk in tokens)

    return sum(1 for w in words if hit(skeleton_en(w))) / len(words)


# ---- 日本語（漢字かな交じり）の読みによる照合 ----
_kakasi = None  # 遅延初期化（None=未初期化 / False=使えない）


@lru_cache(maxsize=4096)
def _reading(text: str) -> str:
    """漢字かな交じりの日本語をカタカナの読みにする（pykakasi）。かなはそのままカタカナになる。
    使えなければ空文字列。候補のラベルは同じものが何度も現れるため、lru_cache で変換を再利用する。"""
    global _kakasi
    if _kakasi is None:
        try:
            import pykakasi
            _kakasi = pykakasi.kakasi()
        except Exception:
            _kakasi = False
    if _kakasi is False:
        return ""
    try:
        return "".join(item["kana"] for item in _kakasi.convert(text))
    except Exception:
        return ""


def ja_ratio(text: str, label: str) -> float:
    """日本語の読みの一致度（0〜1）。漢字の同音異義（「補完」/「保管」）やかな・漢字・長音の
    書き方の違い（「さくじょ」/「削除」、「トーキョー」/「とうきょう」）を読みに直して比べるので、
    表記が違っても見逃さない。label の読みの骨格のうち発話の骨格に含まれる割合。
    読みが取れない・label が短すぎて偶然の一致が増えるときは 0。"""
    ra, rb = _reading(text), _reading(label)
    if not ra or not rb:
        return 0.0
    sa, sb = skeleton_kana(ra), skeleton_kana(rb)
    if len(sb) < 2:  # 1 子音の骨格（「日」h など）はどこにでも含まれるので当てにしない
        return 0.0
    if sb in sa:
        return 1.0
    import difflib
    m = difflib.SequenceMatcher(None, sa, sb).find_longest_match(0, len(sa), 0, len(sb))
    return m.size / len(sb)


def ja_menu_match(text: str, path: list[str]) -> float:
    """メニュー項目との読みの一致度。項目名（最後の要素）を重く、上位のメニュー名を軽く見る。"""
    leaf = ja_ratio(text, path[-1] if path else "")
    whole = ja_ratio(text, " ".join(path))
    return max(leaf, whole * 0.8)
