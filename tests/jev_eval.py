"""Jev 単独の判定精度の評価（手動確認用）。キーワード照合による上書きを外して、Jev の答えだけを採点する。

PioViewer（メイン画面）を開いた状態で実行する。実際の操作は行わない。
  python tests/jev_eval.py
"""
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voicectl import candidates, textparse, winutil  # noqa: E402
from voicectl.apps import AppCatalog  # noqa: E402
from voicectl.config import load  # noqa: E402
from voicectl.context import Snapshot  # noqa: E402
from voicectl.controller import Controller  # noqa: E402
from voicectl.engines.jev import JevEngine  # noqa: E402
from voicectl.uia import UiaScanner  # noqa: E402

logging.basicConfig(level=logging.WARNING)
winutil.set_dpi_aware()

# (発話, 期待するアクション, 期待する対象の名前に含まれる文字列)
CASES = [
    ("OOPのレンジを表示して", "menu_command", "Range OOP"),
    ("IPのEVを見せて", "menu_command", "EV IP"),
    ("戦略を表示して", "menu_command", "Show > Strategy"),
    ("ツリーを保存して", "menu_command", "Save full tree"),
    ("ルートノードに戻って", "menu_command", "root node"),
    ("親ノードに戻って", "menu_command", "parent node"),
    ("ソルバーに接続して", "menu_command", "Solver > Connect"),
    ("ノードロックして", "menu_command", "lock node"),
    ("戦略を丸めて", "menu_command", "Round Strategies"),
    ("GTOトレーナー開いて", "menu_command", "Trainer V3"),
    ("ストラテジーとイーブイを表示", "menu_command", "Strategy + EV"),
    ("ベンチマークして", "menu_command", "Benchmark"),
    ("リグレットを見せて", "menu_command", "Regrets"),
    ("ボードを入力", "click_element", "Board"),
    ("ピックを押して", "click_element", "Pick"),
    ("プリフロップのタブを開いて", "click_element", "Preflop"),
    ("メモ帳開いて", "launch_app", "メモ帳"),
    ("今日はいい天気ですね", "unknown", ""),
]


class _S:
    def emit(self, *a):
        pass


class _Ui:
    status = level = hints_labels = hints_grid = hints_clear = transcript_partial = transcript_final = \
        transcript_result = idle = _S()


def main():
    cfg = load()
    j = cfg.get("decision.jev")
    eng = JevEngine(j["endpoint"], j["model"], j["api_key_env"], 10.0)
    eng.warmup()
    ctl = Controller(cfg, _Ui(), None, None, eng, AppCatalog(cfg.get("app_aliases")))
    pio = next(w for w in winutil.list_windows() if w.title.startswith("PioViewer"))
    profile = ctl.profiles.active(pio)
    menu = ctl._appmap(pio)
    for e in menu.entries:
        e.aliases = profile.menu_aliases.get(" > ".join(e.path), [])
    els = UiaScanner().scan(pio.hwnd, pio.rect)
    for el in els:
        el.aliases = (profile.control_aliases.get(f"{el.name}#{el.auto_id}", []) + profile.control_aliases.get(el.name, [])
                      + (profile.control_aliases.get(f"#{el.auto_id}", []) if el.auto_id else []))
    snap = Snapshot(pio, winutil.get_cursor(), [pio], els)
    words = dict(ctl.dictionary)
    words.update(profile.dictionary)

    ok, confs, lats = 0, [], []
    for text, want_action, want_target in CASES:
        t2 = textparse.apply_dictionary(text, words)
        lk = candidates.build(t2, snap, ctl.catalog.candidates(), ctl.limits, menu=menu.entries,
                              commands=profile.commands)
        kd = ctl.keyword.decide(t2, snap, lk, None)
        facts = ctl._facts(t2, lk, kd) if hasattr(ctl, "_facts") and "--no-facts" not in sys.argv else None
        t = time.perf_counter()
        d = eng.decide(t2, snap, lk, None, facts=facts) if facts is not None else eng.decide(t2, snap, lk, None)
        lats.append((time.perf_counter() - t) * 1000)
        desc = d.describe(lk)
        hit = d.action == want_action and (want_target in desc or not want_target)
        ok += hit
        confs.append(d.confidence if hit else 0.0)
        print(f"{'○' if hit else '×'} {text:22s} → {desc} [{d.action} {d.confidence:.2f}]")
    print(f"\n正解 {ok}/{len(CASES)}  正解時の平均確信度 {statistics.mean([c for c in confs if c] or [0]):.2f}  "
          f"平均遅延 {statistics.mean(lats):.0f} ms  {'（事実なし）' if '--no-facts' in sys.argv else '（事実あり）'}")


if __name__ == "__main__":
    main()
