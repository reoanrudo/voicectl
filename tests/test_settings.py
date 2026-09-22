"""設定ウィンドウのテスト。ロジック部分（Qt なし）と、画面の構築ができること（オフスクリーン）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voicectl.config import load  # noqa: E402
from voicectl.settings_ui import apply_changes, group_layout_rows, layout_to_rows, parse_roots  # noqa: E402


def test_layout_rows_roundtrip():
    layouts = {"開発": [{"app": "ZCode", "place": "left_half"}, {"app": "Brave", "place": "right_half"}],
               "資料": [{"app": "Obsidian", "place": "full"}]}
    rows = layout_to_rows(layouts)
    assert len(rows) == 3 and rows[0] == ("開発", "ZCode", "left_half")
    assert group_layout_rows(rows) == layouts


def test_group_layout_rows_drops_blank_and_bad_place():
    rows = [("開発", "ZCode", "left_half"), ("開発", "", "full"), ("", "x", "full"),
            ("資料", "Obsidian", "そんな場所")]
    back = group_layout_rows(rows)
    assert back == {"開発": [{"app": "ZCode", "place": "left_half"}],
                    "資料": [{"app": "Obsidian", "place": "full"}]}   # 未入力行は捨て、場所の表記は直す


def test_parse_roots():
    assert parse_roots("S:\\; D:\\data 、テスト") == ["S:\\", "D:\\data", "テスト"]
    assert parse_roots("") == []


def test_apply_changes_nested():
    raw = {"mouse": {"step_px": {"small": 60}}}
    apply_changes(raw, [("mouse.step_px.small", 90), ("listen.auto_segment", False), ("thresholds.execute", 0.85)])
    assert raw["mouse"]["step_px"]["small"] == 90
    assert raw["listen"] == {"auto_segment": False}
    assert raw["thresholds"] == {"execute": 0.85}


def test_config_save_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("listen:\n  auto_segment: true\nthresholds:\n  execute: 0.8\n", encoding="utf-8")
    cfg = load(p)
    cfg.raw["thresholds"]["execute"] = 0.85
    cfg.raw["layouts"] = {"開発": [{"app": "ZCode", "place": "left_half"}]}
    cfg.save()
    cfg2 = load(p)
    assert cfg2.get("thresholds.execute") == 0.85
    assert cfg2.get("layouts.開発")[0]["place"] == "left_half"
    assert "voicectl" in p.read_text(encoding="utf-8")   # ヘッダーが付く


def test_settings_dialog_builds_and_collects(monkeypatch):
    """設定ウィンドウが組み立てられて、値の取り込み（保存の中身）まで通ること。オフスクリーン。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from voicectl.settings_ui import SettingsDialog
    cfg = load()   # 本物の設定を読む（テストでは書き戻さない）
    dlg = SettingsDialog(cfg, None, None)
    dlg._collect()
    assert dlg.cfg.get("listen.auto_segment") in (True, False)
    assert isinstance(dlg.cfg.get("layouts"), dict)
    assert dlg.ptt.currentText() == cfg.get("hotkeys.push_to_talk", "f5")
    QApplication.processEvents()
