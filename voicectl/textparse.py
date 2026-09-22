"""発話テキストのルール処理：正規化、停止/肯定/否定の判定、番号の読み取り、入力文字列の切り出し、候補の絞り込み。"""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[\s。、，,．.！!？?「」『』…~〜ー]+$|^[\s。、，,．.！!？?]+")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).strip()
    t = re.sub(r"[。、，,．！!？?…]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def compact(text: str) -> str:
    return re.sub(r"[\s。、，,．.！!？?…〜~]", "", normalize(text))


_STOP = ("ストップ", "すとっぷ", "止めて", "とめて", "止まれ", "やめて", "やめ", "中止", "キャンセル", "stop")
_YES = ("はい", "うん", "ええ", "オッケー", "おっけー", "ok", "okay", "実行", "いいよ", "いいです", "お願い", "どうぞ",
        "そう", "イエス", "yes")
_NO = ("いいえ", "いや", "違う", "ちがう", "だめ", "ダメ", "しない", "やめ", "no", "ノー")


def _exact_or_short(text: str, words: tuple[str, ...], max_extra: int) -> bool:
    c = compact(text).lower()
    return any(c.startswith(w.lower()) and len(c) - len(w) <= max_extra for w in words)


def is_stop(text: str) -> bool:
    return _exact_or_short(text, _STOP, 3)


def is_yes(text: str) -> bool:
    return _exact_or_short(text, _YES, 4) and not is_no(text)


def is_no(text: str) -> bool:
    return _exact_or_short(text, _NO, 4)


def is_denial(text: str) -> bool:
    """裸の否定（「違う」「違います」「そうじゃない」）。確認待ち以外で言われたときの取り消し表示に使う。"""
    c = compact(text).lower()
    return bool(re.fullmatch(r"(違う|違います|ちがう|ちがいます|そうじゃない|そうではない)(よ|ね)?", c))


# ---- 番号 ----
_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANA_NUM = [  # 長いものから照合
    ("きゅうじゅう", 90), ("はちじゅう", 80), ("ななじゅう", 70), ("ろくじゅう", 60), ("ごじゅう", 50),
    ("よんじゅう", 40), ("さんじゅう", 30), ("にじゅう", 20), ("じゅう", 10),
    ("いち", 1), ("さん", 3), ("よん", 4), ("ろく", 6), ("なな", 7), ("しち", 7), ("はち", 8),
    ("きゅう", 9), ("ぜろ", 0), ("れい", 0), ("に", 2), ("し", 4), ("ご", 5), ("く", 9),
]


def _kanji_to_int(s: str) -> int | None:
    if not s or any(ch not in _KANJI_DIGIT and ch != "十" for ch in s):
        return None
    if "十" in s:
        tens, _, ones = s.partition("十")
        t = _KANJI_DIGIT.get(tens, 1) if tens else 1
        o = _KANJI_DIGIT.get(ones, 0) if ones else 0
        return t * 10 + o
    val = 0
    for ch in s:
        val = val * 10 + _KANJI_DIGIT[ch]
    return val


def _kana_to_int(s: str) -> int | None:
    total, i, matched = 0, 0, False
    while i < len(s):
        for word, v in _KANA_NUM:
            if s.startswith(word, i):
                total += v
                i += len(word)
                matched = True
                break
        else:
            return None
    return total if matched else None


def parse_number(text: str) -> int | None:
    """「5番」「十二」「じゅうに」「番号3」などから番号を取り出す。番号だけの発話でなければ None。"""
    c = compact(text)
    c = re.sub(r"^(番号|ばんごう|ナンバー)", "", c)
    c = re.sub(r"(番|ばん|番目|を?クリック|を?押して|で)$", "", c)
    c = re.sub(r"(番|ばん)$", "", c)
    if not c:
        return None
    if c.isdigit():
        return int(c)
    k = _kanji_to_int(c)
    if k is not None:
        return k
    hira = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in c)
    return _kana_to_int(hira)


# ---- 入力文字列の切り出し（Jev は自由テキストを返せないためルールで行う） ----
_TYPE_PATTERNS = [
    re.compile(r"^[「『](?P<t>.+?)[」』]\s*(と|って)?\s*(入力|にゅうりょく|打|う|書|か|タイプ)"),
    re.compile(r"^(?P<t>.+?)\s*(と|って)\s*(入力|にゅうりょく|打って|うって|打ち込|書いて|かいて|タイプ)"),
    re.compile(r"^(入力|にゅうりょく|タイプ)\s*(して)?\s*(?P<t>.+)$"),
    re.compile(r"^(?P<t>.+?)\s*を\s*(入力|タイプ)"),
]


def extract_text(text: str) -> str | None:
    t = normalize(text)
    for pat in _TYPE_PATTERNS:
        m = pat.search(t)
        if m and m.group("t").strip():
            return m.group("t").strip()
    return None


# ---- 読み替え辞書（カタカナ → 英語表記など） ----
_KATA = "ァ-ヶー"

# ---- 長音の表記ゆれ（おー / おお / おう など）----
# 音声認識は長音を「ー」で書くことが多いが、辞書や画面の文字は「おお」「おう」などで書かれる。
# 照合のときだけ、どちらの書き方でも同じ文字列になるよう揃える（表示・入力には使わない）。
_VOWEL_PAIRS = [
    ("あいうえお", "あいうえお"), ("かきくけこ", "あいうえお"), ("がぎぐげご", "あいうえお"),
    ("さしすせそ", "あいうえお"), ("ざじずぜぞ", "あいうえお"), ("たちつてと", "あいうえお"),
    ("だぢづでど", "あいうえお"), ("なにぬねの", "あいうえお"), ("はひふへほ", "あいうえお"),
    ("ばびぶべぼ", "あいうえお"), ("ぱぴぷぺぽ", "あいうえお"), ("まみむめも", "あいうえお"),
    ("やゆよ", "あうお"), ("ゃゅょ", "あうお"), ("ぁぃぅぇぉ", "あいうえお"),
    ("らりるれろ", "あいうえお"), ("わを", "あう"), ("ゔ", "う"),
]
_VOWEL_H: dict[str, str] = {}
_VOWEL_K: dict[str, str] = {}
for _chars, _vows in _VOWEL_PAIRS:
    for _c, _v in zip(_chars, _vows):
        _VOWEL_H[_c] = _v
        _VOWEL_K[chr(ord(_c) + 0x60)] = chr(ord(_v) + 0x60)  # ひらがな → カタカナ


def _is_long_pair(a: str, b: str) -> bool:
    """a + b の 2 文字が「長音 1 拍分」を表す組か。
    「もう / もー / もお」「けい / けー / けえ」「ああ / あー」など、同じ音の長音の書き分けを 1 つに畳むために使う。"""
    va = _VOWEL_H.get(a) or _VOWEL_K.get(a)
    if not va:
        return False
    if b in ("う", "ウ") and va in ("お", "オ"):
        return True
    if b in ("お", "オ") and va in ("お", "オ"):   # とおい / とうい / とーい
        return True
    if b in ("い", "イ") and va in ("え", "エ"):
        return True
    if b in ("え", "エ") and va in ("え", "エ"):   # せえ / せい / せー
        return True
    return a == b and (a in "あいうえおアイウエオ")


def expand_long_vowels(text: str) -> str:
    """長音「ー」を、直前のかなと同じ書き方の長音に置き換える。文字数は変わらない。
    母音が「お」のかな（と、よ、もう…）の後は「う」で書く長音（とう/もう）に、「え」のかな
    （け、せ、て…）の後は「い」で書く長音（けい/せい）に、母音のかなの後はその母音を重ねる（あー→ああ）。
    かなが続いていない「ー」（行頭・英字の直後など）はそのまま。"""
    if "ー" not in text:
        return text
    out = list(text)
    for i, ch in enumerate(out):
        if ch == "ー" and i > 0:
            p = out[i - 1]
            v = _VOWEL_H.get(p)
            if v:
                out[i] = "う" if v == "お" else ("い" if v == "え" else v)
            else:
                v = _VOWEL_K.get(p)
                if v:
                    out[i] = "ウ" if v == "オ" else ("イ" if v == "エ" else v)
    return "".join(out)


def _unify_long(text: str) -> tuple[str, list[int]]:
    """かな（ひらがな・カタカナ）をカタカナに揃え、長音の書き方のゆれ（おー/おお/おう/えい…）も
    1 通りに揃えた文字列と、各文字の開始位置の対応（末尾に全体の長さの番兵を足す）を返す。
    畳み込みで文字数は詰まるため、揃え後 j 文字目が元で idx[j]〜idx[j+1] に当たる。"""
    exp = expand_long_vowels(text)
    exp = "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in exp)  # ひらがな → カタカナ
    out: list[str] = []
    idx: list[int] = []
    i = 0
    while i < len(exp):
        out.append(exp[i])
        idx.append(i)
        if i + 1 < len(exp) and _is_long_pair(exp[i], exp[i + 1]):
            i += 2   # 長音の 2 文字分を 1 文字に畳む
        else:
            i += 1
    idx.append(len(exp))
    return "".join(out), idx


def _sub_reading(text: str, yomi: str, target: str) -> str:
    """text の中の yomi（かなの書き方・長音のゆれは許容）を target に置き換える。
    見つからなければ text をそのまま返す。カタカナの読みは、前後が別のカタカナにつながって
    いないときだけ置き換える（「パスワード」の「ワード」を Word にしないため。
    境界は元の text の文字で判定する）。"""
    uni, idx = _unify_long(text)
    uy = _unify_long(yomi)[0]
    if not uy:
        return text
    kata = bool(re.fullmatch(f"[{_KATA}]+", yomi))
    out: list[str] = []
    last = 0
    for m in re.finditer(re.escape(uy), uni):
        a, b = m.start(), m.end()
        oa, ob = idx[a], idx[b]  # 揃え後の範囲 → 元の text の範囲
        if kata and ((oa > 0 and re.match(f"[{_KATA}]", text[oa - 1])) or
                     (ob < len(text) and re.match(f"[{_KATA}]", text[ob]))):
            continue  # より長いカタカナ語の一部
        out.append(text[last:oa])
        out.append(target)
        last = ob
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def apply_dictionary(text: str, mapping: dict[str, str]) -> str:
    """辞書の読みを表記に置き換える。カタカナの読みは、前後が別のカタカナにつながっていないときだけ置き換える
    （「パスワード」の「ワード」を Word にしないため）。長い読みから順に置き換える。
    読みと文の長音（おー/おお/おう）の書き方の違いは揃えてから照合する。"""
    if not mapping:
        return text
    for yomi in sorted(mapping, key=len, reverse=True):
        target = str(mapping[yomi])
        text = _sub_reading(text, yomi, target)
    return _fuzzy_katakana(text, mapping)


def _fuzzy_katakana(text: str, mapping: dict[str, str]) -> str:
    """辞書に完全一致しなかった 4 文字以上のかな語（カタカナ・ひらがなのどちらで出ても）を、
    読みがわずかに違うだけなら同じ単語とみなして置き換える（「ピオソロバー」→ ピオソルバー、
    「ディーシーク」→ ディープシーク など、音声認識の聞き間違い・表記ゆれ対策）。
    長さの差は 1 文字までに限る。2 文字まで許すと「ソルバー」が「ピオソルバー」になるなど、短い一般語を誤変換するため。"""
    import difflib
    keys = [k for k in mapping if len(k) >= 4 and re.fullmatch(f"[{_KATA}]+", k)]
    if not keys:
        return text
    ukeys = [(k, _unify_long(k)[0]) for k in keys]
    out: list[str] = []
    last = 0
    # かなのまとまり（カタカナ語・ひらがな語）を元の文字列から見つけて、まとまりごとに比べる。
    # 全体を先にカタカナへ揃えてから探すと、かなのまとまりの外の言葉まで置換範囲に入ってしまう
    for m in re.finditer(f"[{_KATA}]{{4,}}|[ぁ-ゖー]{{4,}}", text):
        uni, _ = _unify_long(m.group(0))
        best, score = None, 0.0
        for k, uk in ukeys:
            if abs(len(uk) - len(uni)) > 1:
                continue
            r = difflib.SequenceMatcher(None, uni, uk).ratio()
            if r > score:
                best, score = k, r
        if best is not None and score >= 0.8:
            out.append(text[last:m.start()])
            out.append(str(mapping[best]))
            last = m.end()
    out.append(text[last:])
    return "".join(out)


# ---- 候補の絞り込み（Jev の選択肢上限 255 と入力トークン節約のため） ----
def _ngrams(s: str, n: int = 2) -> set[str]:
    s = compact(s).lower()
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


_LABEL_SPLIT = re.compile(r"[（）()・:：、,\[\]]|別名")


def similarity(query: str, label: str) -> float:
    """ラベルを本名と別名に分け、最も近い部分の値を返す。どちらかがもう一方を含めば 1.0 以上。
    比較の前に長音の書き方（おー/おお/おう）を揃える。"""
    cq = _unify_long(compact(query).lower())[0]
    best = 0.0
    for part in _LABEL_SPLIT.split(label):
        cp = _unify_long(compact(part).lower())[0]
        if not cp or not cq:
            continue
        if cp in cq and len(cp) >= 2 or cp == cq:
            s = 1.0 + 0.5 * len(cp) / len(cq)      # 発話に名前が丸ごと入っている（長い一致を優先）
        elif cq in cp and len(cq) >= 2:
            s = 1.0 + 0.5 * len(cq) / len(cp)      # 名前の一部だけ言った
        else:
            q, p = _ngrams(cq), _ngrams(cp)
            s = len(q & p) / len(p) if q and p else 0.0
        best = max(best, s)
    return best


def rank(query: str, labels: list[tuple[str, str]], limit: int) -> list[str]:
    """labels: (id, 照合用テキスト)。類似度の高い順に id を最大 limit 件返す。元の順番は同点時に保つ。"""
    scored = [(similarity(query, text), i, cid) for i, (cid, text) in enumerate(labels)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [cid for _, _, cid in scored[:limit]]
