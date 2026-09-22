"""起動できるアプリの一覧（Get-StartApps で UWP を含めて取得）と起動処理。"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .schema import Candidate

log = logging.getLogger(__name__)
CACHE = Path(__file__).resolve().parent.parent / "cache" / "apps.json"

# ヘルプやアンインストーラなど、起動対象として邪魔なものは除外
_SKIP_WORDS = ("uninstall", "アンインストール", "help", "ヘルプ", "readme", "release notes", "homepage", "website",
               "manual", "マニュアル", "documentation", "license")


class AppCatalog:
    def __init__(self, aliases: dict[str, list[str]] | None = None):
        self.aliases = aliases or {}
        self.extra: list[dict] = []  # スタートメニューにないアプリ（プロファイルの launch）
        self._apps: list[dict] = []
        self._lock = threading.Lock()
        if CACHE.exists():
            try:
                self._apps = json.loads(CACHE.read_text(encoding="utf-8"))
            except Exception:
                log.warning("アプリ一覧のキャッシュを読めませんでした")

    def refresh(self) -> None:
        t0 = time.perf_counter()
        cmd = ["powershell.exe", "-NoProfile", "-Command",
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"]
        out = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=0x08000000).stdout
        items = json.loads(out.decode("utf-8") or "[]")
        if isinstance(items, dict):
            items = [items]
        apps = [{"name": i["Name"], "id": i["AppID"]} for i in items
                if i.get("Name") and not any(w in i["Name"].lower() for w in _SKIP_WORDS)]
        with self._lock:
            self._apps = apps
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(apps, ensure_ascii=False), encoding="utf-8")
        log.info("アプリ一覧を更新: %d 件 (%.0f ms)", len(apps), (time.perf_counter() - t0) * 1000)

    def refresh_async(self) -> None:
        threading.Thread(target=self._safe_refresh, daemon=True).start()

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("アプリ一覧の取得に失敗")

    def names_without_reading(self, spellings: set[str]) -> list[str]:
        """英字を含むのに、読み辞書のどの表記にも当てはまらないアプリ名（新しく入れたアプリなど）。"""
        with self._lock:
            names = sorted({a["name"] for a in self._apps})
        out = []
        for n in names:
            if not any(c.isascii() and c.isalpha() for c in n):
                continue  # 日本語名は読み不要
            if not any(s.lower() in n.lower() for s in spellings):
                out.append(n)
        return out

    def find(self, name: str) -> dict | None:
        """名前が一致する（または「名前 + 空白」で始まる）インストール済みアプリ。"""
        n = name.lower().strip()
        with self._lock:
            apps = list(self._apps) + list(self.extra)
        for a in apps:
            an = a["name"].lower()
            if an == n or an.startswith(n + " "):
                return a
        return None

    def candidates(self) -> list[Candidate]:
        with self._lock:
            apps = list(self._apps) + list(self.extra)
        out = []
        for idx, a in enumerate(apps):
            extra = [al for key, als in self.aliases.items() if key.lower() in a["name"].lower() for al in als]
            label = a["name"] + (f"（別名: {'・'.join(extra)}）" if extra else "")
            out.append(Candidate(id=f"app{idx}", label=label, data=a))
        return out


def launch(app: dict) -> None:
    app_id = app["id"]
    if os.path.exists(app_id):  # プロファイルで指定した実行ファイルのパス
        os.startfile(app_id)
    elif app_id.startswith(("http://", "https://")):
        os.startfile(app_id)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=0x08000000)
