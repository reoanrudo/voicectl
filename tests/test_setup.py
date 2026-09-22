"""設定の移行（migrate）とデバイス選択（pick_device）のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voicectl.config import migrate  # noqa: E402
from voicectl.stt import pick_device  # noqa: E402


def test_migrate_fills_new_sections():
    raw = migrate({})
    assert raw["version"] == 1
    assert raw["alarm"] == {"use_clock_app": True}
    assert raw["wake_word"] == {"enabled": False}
    assert raw["voice_reply"] == {"enabled": False}
    assert raw["transcript"]["partial_as_final"] is True
    assert raw["layouts"] == {}


def test_migrate_keeps_user_values():
    raw = migrate({"alarm": {"use_clock_app": False}, "wake_word": {"enabled": True, "words": ["ヘイ"]}})
    assert raw["alarm"]["use_clock_app"] is False          # ユーザーの値は上書きしない
    assert raw["wake_word"]["enabled"] is True
    assert raw["wake_word"]["words"] == ["ヘイ"]            # 足りない項目だけ埋める
    assert raw["version"] == 1


def test_migrate_current_version_is_noop():
    raw = {"version": 1, "alarm": {"use_clock_app": False}}
    assert migrate(raw) is raw


def test_pick_device_prefers_named_gpu():
    dev, ctype, idx = pick_device("auto", "notexist")   # この名前の GPU は無い → CPU へ
    assert dev == "cpu" and ctype == "int8" and idx is None
    dev, ctype, idx = pick_device("cpu", "4080")
    assert dev == "cpu" and idx is None                  # cpu 指定は GPU を探さない
