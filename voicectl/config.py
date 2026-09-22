"""設定ファイルの読み書き。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config.yaml"

_HEADER = "# voicectl 設定ファイル（トレイの「設定」からも編集できます。変更の反映には再起動が必要）\n"
_VERSION = 1   # 設定の形式の版。上げるときは migrate() に移行処理を足す


def migrate(raw: dict) -> dict:
    """旧い設定に足りない項目を埋めて version をそろえる（ユーザーが設定した値は上書きしない）。"""
    if not isinstance(raw, dict):
        return raw
    try:
        ver = int(raw.get("version") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver >= _VERSION:
        raw["version"] = _VERSION
        return raw
    section_defaults = {
        "alarm": {"use_clock_app": True},
        "wake_word": {"enabled": False},
        "voice_reply": {"enabled": False},
    }
    for key, dflt in section_defaults.items():
        node = raw.get(key)
        if node is None:
            raw[key] = dict(dflt)
        elif isinstance(node, dict):
            for k, v in dflt.items():
                node.setdefault(k, v)
    if raw.get("transcript") is None:
        raw["transcript"] = {}
    if isinstance(raw["transcript"], dict):
        raw["transcript"].setdefault("partial_as_final", True)
    if raw.get("layouts") is None:
        raw["layouts"] = {}
    raw["version"] = _VERSION
    return raw


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def needs_confirm(self, action: str, param: str | None) -> bool:
        rules = self.get("always_confirm", []) or []
        return action in rules or (param is not None and f"{action}:{param}" in rules)

    def save(self) -> None:
        """設定をファイルに書き戻す（置き換え方式。書き込み途中の壊れたファイルを残さない）。"""
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(_HEADER + yaml.safe_dump(self.raw, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise


APP_READINGS = ROOT / "app_readings.yaml"


def load_app_readings(path: Path | None = None) -> dict[str, str]:
    """アプリの読み辞書（表記: [読み...]）を、読み → 表記 の形にして返す。"""
    p = path or APP_READINGS
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {str(y): str(name) for name, yomis in raw.items() for y in (yomis or [])}


def dictionary(cfg: Config) -> dict[str, str]:
    """アプリの読み辞書と config.yaml の dictionary を合わせる（同じ読みは config.yaml が優先）。"""
    merged = load_app_readings()
    merged.update({str(k): str(v) for k, v in (cfg.get("dictionary", {}) or {}).items()})
    return merged


def load(path: Path | None = None) -> Config:
    p = path or DEFAULT_PATH
    with open(p, encoding="utf-8") as f:
        return Config(migrate(yaml.safe_load(f) or {}), p)
