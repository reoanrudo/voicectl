"""オーバーレイ（状態表示・文字起こしバー）が画面の作業領域に収まるかを実機で確認する。

実行: .venv\\Scripts\\python.exe tests\\overlay_check.py
画面には出さない（offscreen）ので、実行中のデスクトップには影響しない。
タスクバーの高さ・自動的に隠す設定・拡大表示は OS が返す作業領域（rcWork）から取る。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 画面に出さずに寸法だけ確かめる
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from voicectl import config as config_mod, winutil  # noqa: E402
from voicectl.overlay import StatusOverlay, TranscriptBar  # noqa: E402

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        fails.append(f"{label} {detail}".strip())
    print(f"{'OK ' if ok else 'NG '} {label} {detail}".rstrip())


app = QApplication(sys.argv)
cfg = config_mod.load()
margin = int(cfg.get("overlay.margin_px", 16))
avoid = bool(cfg.get("overlay.avoid_taskbar", True))
corner = str(cfg.get("overlay.corner", "bottom_right"))

work = winutil.work_area_at(0, 0)
mon = winutil.monitor_rect_at(0, 0)
print(f"モニター {mon} / 作業領域 {work} / タスクバー {'自動的に隠す' if winutil.taskbar_auto_hide() else '常時表示'}"
      f"（高さ {mon[3] - work[3]} px）")

status = StatusOverlay(corner, int(cfg.get("overlay.font_px", 18)), margin, avoid, True)
bar = TranscriptBar(int(cfg.get("transcript.font_px", 15)), 0.0,
                    int(cfg.get("transcript.history", 2)), "F5", anchor=status,
                    margin=margin, avoid_taskbar=avoid, animate=True)
status.on_layout.append(bar._layout)


def inside(widget, name: str) -> None:
    g = widget.geometry()
    check(f"{name} が作業領域の中", g.x() >= work[0] and g.y() >= work[1]
          and g.x() + g.width() <= work[2] and g.y() + g.height() <= work[3],
          f"位置=({g.x()}, {g.y()}) 大きさ={g.width()}x{g.height()}")


# 状態を一通り表示して、そのつど位置と大きさを確かめる（文言が増えてもはみ出さない）
for state, title, lines in [
    ("idle", "待機中", []),
    ("listening", "聞き取り中", ["メモ帳を開いて、こんにちはと入力して"]),
    ("processing", "認識中…", []),
    ("done", "実行しました", ["メモ帳を起動", "入力：こんにちは"]),
    ("confirm", "実行しますか？", ["聞き取り：このタブを閉じて", "「はい」か Enter で実行 / 「いいえ」か Esc で取消"]),
    ("retry", "聞き取れませんでした", ["もう一度お願いします"]),
    ("error", "実行できません", ["「保存」が見つかりません"]),
    ("paused", "一時停止中", ["F5 で再開"]),
]:
    status.set_status(state, title, lines, 0.0)
    app.processEvents()
    inside(status, f"[{state}] 状態表示")
    inside(bar, f"[{state}] 文字起こしバー")
    gs, gb = status.geometry(), bar.geometry()
    # ウィンドウは影の余白（_PAD）どうしが重なる。見えるパネル同士が重ならないことを確かめる
    from voicectl.overlay import _PAD
    check(f"[{state}] 文字起こしバーが状態表示の上", gb.y() + gb.height() - _PAD <= gs.y() + _PAD,
          f"バー下端={gb.y() + gb.height() - _PAD} 状態表示上端={gs.y() + _PAD}")

# 文字起こしバーだけの表示（途中経過 → 確定 → 結果）
status.set_status("idle", "待機中", [], 0.0)
for setter, value in ((bar.set_partial, "メモ帳を開いて"), (bar.set_final, "メモ帳を開いて、こんにちはと入力して"),
                      (bar.set_result, "メモ帳を起動 → 入力：こんにちは")):
    setter(value)
    app.processEvents()
    inside(bar, f"文字起こし「{value[:12]}…」")

# レベル（波形）を振っても位置は変わらない
for lv in (0.0, 0.5, 1.0):
    bar.set_level(lv)
    app.processEvents()
inside(status, "波形更新後")

# 表示倍率・余白を変えたときも収まる
for margin2 in (0, 48):
    s2 = StatusOverlay(corner, 24, margin2, avoid, False)
    check(f"余白 {margin2}px でも作業領域の中",
          s2.geometry().y() + s2.geometry().height() <= work[3],
          f"位置=({s2.geometry().x()}, {s2.geometry().y()})")

print("\n== 結果 ==")
if fails:
    print(f"{len(fails)} 件の不一致:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("すべて作業領域の中に収まっていました。")
