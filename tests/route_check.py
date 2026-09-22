"""いろいろな言い方が、どの処理に回るか（実行はしない）を一覧にする手動確認用スクリプト。

実行: .venv\\Scripts\\python.exe tests\\route_check.py [言い方のファイル]
  決まった形の命令（intents）→ 手順のひな形（tasks）→ 回数つき → Jev の判定 の順に、実際の処理と同じ順で調べる。
  Jev の判定は API を呼ぶが、操作はしない。画面は架空のブラウザ（YouTube）とメモ帳を使う。
"""
import os
import sys
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if not os.environ.get("TYPESAFE_API_KEY"):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        os.environ["TYPESAFE_API_KEY"] = winreg.QueryValueEx(k, "TYPESAFE_API_KEY")[0]

from voicectl import candidates, followup, intents, tasks  # noqa: E402
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.config import load  # noqa: E402
from voicectl.context import Snapshot  # noqa: E402
from voicectl.controller import Controller  # noqa: E402
from voicectl.engines.jev import JevEngine  # noqa: E402
from voicectl.uia import ScreenElement  # noqa: E402
from voicectl.winutil import WindowInfo  # noqa: E402

DEFAULT = """
メモ帳開いて
ブラウザ起動して
エクセル開いて
YouTubeでヒカキンの動画
Googleマップで東京駅
楽天で掃除機を探して
X内の検索でMiniMaxを検索して
今日の天気を調べて
Notionを開いて
ヨドバシの公式サイト開いて
音量30にして
音量ちょっと下げて
ミュートして
次の曲
一時停止
10秒戻して
3番目のタブ
タブを閉じて
閉じたタブを戻して
新しいタブを開いて
ページ内で料金を探して
下にスクロール
3回下にスクロール
もっと下
一番上に戻って
前のページに戻って
リロードして
拡大して
全画面にして
最小化して
左半分にして
左にChrome右にメモ帳
Chromeを閉じて
デスクトップを表示
エクスプローラー開いて
ダウンロードフォルダを開いて
最近使ったPDFを開いて
見積書というファイルを開いて
スクショ撮って
クリップボードの履歴
コピーして
貼り付けて
全部選択して
元に戻して
保存して
名前を付けて保存
こんにちはと入力して
書き取り開始
5分後に知らせて
今何時
123かける45は
選択したところ読んで
番号出して
グリッド出して
保存ボタンを押して
設定を開いて
パソコンをロックして
作業開始と言ったらメモ帳を開いてChromeを開いて
ログインの手順を記録して
何ができる？
これ覚えて
さっきのページ
もう一回
ストップ
今日はいい天気ですね
えーっと
あれ何だっけ
"""


def main() -> None:
    lines = (Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else DEFAULT).splitlines()
    cases = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    cfg = load()
    j = cfg.get("decision.jev")
    catalog = AppCatalog(cfg.get("app_aliases"))
    eng = JevEngine(j["endpoint"], j["model"], j["api_key_env"], 10.0)
    eng.warmup()

    class _Sig:
        def emit(self, *a):
            pass

    class _Ui:
        status = level = hints_labels = hints_grid = hints_clear = transcript_result = _Sig()

    ctl = Controller(cfg, _Ui(), None, None, eng, catalog)
    win = WindowInfo(1, "YouTube - Google Chrome", "chrome.exe", (0, 0, 1600, 1000))
    note = WindowInfo(2, "無題 - メモ帳", "Notepad.exe", (0, 0, 1600, 1000))
    els = [ScreenElement(n, k, r, "uia", 1) for n, k, r in [
        ("保存", "ボタン", (1300, 900, 1400, 940)), ("設定", "ボタン", (1500, 40, 1540, 60)),
        ("検索", "入力欄", (400, 10, 1000, 40)), ("ホーム", "リンク", (10, 100, 80, 120)),
        ("登録チャンネル", "リンク", (10, 140, 120, 160))]]
    snap = Snapshot(win, (800, 500), [win, note], els)
    entries, commands = None, None
    if os.environ.get("ROUTE_APP") == "pio":   # PioViewer を前面にした想定（覚えたメニューとプロファイルを使う）
        win = WindowInfo(3, "PioViewer 3", "PioViewer3.exe", (0, 0, 1600, 1000))
        snap = Snapshot(win, (800, 500), [win, note], els)
        profile = ctl.profiles.active(win)
        menu = ctl._appmap(win)
        if menu:
            for e in menu.entries:
                e.aliases = profile.menu_aliases.get(" > ".join(e.path), []) if profile else []
            entries = menu.entries
        commands = profile.commands if profile else None
        ctl.dictionary.update(profile.dictionary if profile else {})
    from voicectl import textparse
    for text in cases:
        text = textparse.apply_dictionary(text, dict(ctl.dictionary))   # アプリと同じ読み替え（カタカナ → 英語など）
        route = ""
        if followup.kind(text):
            route = f"続き: {followup.kind(text)}"
        elif (it := intents.parse(text, ctl.routines)) is not None:
            route = f"決まった形: {it.kind} {it.args}"
        elif (t := tasks.parse(text, win.title)) is not None:
            route = f"ひな形: {t.describe()}"
        elif (cp := intents.count_prefix(text)) is not None:
            route = f"回数 {cp[0]} × 「{cp[1]}」"
        else:
            lookup = candidates.build(text, snap, catalog.candidates(), {"app": 250, "element": 120, "window": 40},
                                      menu=entries, commands=commands)
            d = ctl._decide(text, snap, lookup)
            route = (f"Jev: {d.describe(lookup)} [{d.confidence:.2f} {d.engine}"
                     f"{' 命令' if d.is_pc_command >= 0.6 else ' 会話?'} {d.is_pc_command:.2f}]")
        print(f"{text:28s} → {route}")


if __name__ == "__main__":
    main()
