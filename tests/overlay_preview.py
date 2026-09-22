"""オーバーレイの見た目を画像に書き出して確認する（画面には出さない）。

実行: .venv\\Scripts\\python.exe tests\\overlay_preview.py  → out\\overlay_preview.png
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")   # offscreen でも日本語の書体を使う
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voicectl.overlay import HintOverlay, StatusOverlay, TranscriptBar  # noqa: E402

app = QApplication(sys.argv)
cases = [
    ("listening", "聞き取り中", ["話し終えると自動で実行します"], ("YouTubeでリュウジの動画を開いて", True, "")),
    ("processing", "手順 2: 検索欄に入力", ["目的：YouTubeでリュウジの動画", "Pause キーで中断"], ("YouTubeでリュウジの動画を開いて", False, "")),
    ("done", "音量 30%", ["聞き取り：音量30"], ("音量30", False, "音量 30%")),
    ("confirm", "実行しますか？", ["聞き取り：このタブを閉じて", "「はい」か Enter で実行"], ("このタブを閉じて", False, "")),
    ("error", "実行できませんでした", ["「保存」が見つかりません"], ("保存して", False, "")),
    ("idle", "待機中", [], ("", False, "")),
]
W, CELL_H = 900, 400
sheet = QPixmap(W * 2, CELL_H * 3)
sp = QPainter(sheet)
for idx, (state, title, lines, (said, partial, res)) in enumerate(cases):
    col, row = idx % 2, idx // 2
    ox, oy = col * W, row * CELL_H
    # 背景（明るい画面と暗い画面を交互に）
    g = QLinearGradient(ox, oy, ox + W, oy + CELL_H)
    if (col + row) % 2:
        g.setColorAt(0, QColor(236, 240, 247)); g.setColorAt(1, QColor(205, 214, 230))
    else:
        g.setColorAt(0, QColor(40, 58, 92)); g.setColorAt(1, QColor(18, 22, 34))
    sp.fillRect(ox, oy, W, CELL_H, g)
    st = StatusOverlay("bottom_right", 22, 16, True, False)
    bar = TranscriptBar(18, 0.0, 2, "F5", anchor=st, animate=False)
    st.on_layout.append(bar._layout)
    bar.history = [("メモ帳を開いて", "「メモ帳」を起動"), ("左にChrome右にObsidian", "")]
    st._levels.extend([0.2, 0.5, 0.9, 0.6, 0.3]); st._level_smooth = 0.6
    bar._levels.extend([0.2, 0.5, 0.9, 0.6, 0.3])
    st._phase = 0.12
    bar._phase = 0.1
    st.set_status(state, title, lines)
    if said:
        (bar.set_partial if partial else bar.set_final)(said)
        if res:
            bar.set_result(res)
    bar._layout()
    sg, bg = st.geometry(), bar.geometry()
    dx, dy = ox + W - sg.width() - 10, oy + CELL_H - sg.height() - 4
    sp.drawPixmap(dx + (bg.x() - sg.x()), dy + (bg.y() - sg.y()), bar.grab())
    sp.drawPixmap(dx, dy, st.grab())
    st.close(); bar.close()
sp.end()
out = Path(__file__).resolve().parent.parent / "out"
out.mkdir(exist_ok=True)
sheet.save(str(out / "overlay_preview.png"))

# 番号ラベルとグリッド
hint = HintOverlay(18)
hint.setGeometry(0, 0, 900, 420)
hint.origin = (0, 0)
bg = QPixmap(900, 420)
p = QPainter(bg)
g = QLinearGradient(0, 0, 900, 420); g.setColorAt(0, QColor(245, 247, 250)); g.setColorAt(1, QColor(210, 220, 235))
p.fillRect(0, 0, 900, 420, g)
p.setPen(QColor(60, 60, 70))
for i, name in enumerate(["ファイル", "編集", "表示", "保存", "共有", "設定"]):
    p.drawText(QRectF(40 + i * 70, 20, 70, 30), Qt.AlignCenter, name)
hint.labels = [(i + 1, (75 + i * 70, 55)) for i in range(6)] + [(12, (200, 150)), (37, (320, 150))]
p.drawPixmap(0, 0, hint.grab())
hint.labels, hint.grid = [], ((460, 110, 880, 400), 3)
p.drawPixmap(0, 0, hint.grab())
p.end()
bg.save(str(out / "overlay_preview_hints.png"))
print("書き出しました:", out / "overlay_preview.png")

# AI の答えカード（考え中・答え）
from voicectl.overlay import AnswerCard  # noqa: E402
sheet2 = QPixmap(1800, 760)
p2 = QPainter(sheet2)
for i, (title, body, hint) in enumerate([
        ("考えています", "", ""),
        ("下書き", "件名：明日の会議時間変更のお願い\n\nお疲れさまです。明日9月23日（水）の会議について、急なご連絡で恐縮ですが、"
                  "開始時刻を15時へ変更していただけないでしょうか。\n\nご確認のほど、よろしくお願いいたします。",
         "「入力して」で入力 ・「もっと短く」などで直す ・「いらない」で捨てる")]):
    ox = i * 900
    g = QLinearGradient(ox, 0, ox + 900, 760)
    g.setColorAt(0, QColor(40, 58, 92) if i == 0 else QColor(236, 240, 247))
    g.setColorAt(1, QColor(18, 22, 34) if i == 0 else QColor(205, 214, 230))
    p2.fillRect(ox, 0, 900, 760, g)
    st = StatusOverlay("bottom_right", 22, 16, True, False)
    st.set_status("processing" if i == 0 else "confirm", "考えています…" if i == 0 else "下書きができました",
                  ["DeepSeek"] if i == 0 else ["「入力して」で入力します"])
    card = AnswerCard(20, anchors=[st], animate=False)
    card._phase = 0.2
    card.show_answer(title, body, hint)
    sg, cg = st.geometry(), card.geometry()
    dx, dy = ox + 900 - sg.width() - 10, 760 - sg.height() - 4
    p2.drawPixmap(dx + (cg.x() - sg.x()), dy + (cg.y() - sg.y()), card.grab())
    p2.drawPixmap(dx, dy, st.grab())
    st.close(); card.close()
p2.end()
sheet2.save(str(out / "overlay_preview_answer.png"))
