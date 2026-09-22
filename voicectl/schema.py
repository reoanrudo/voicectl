"""アクションスキーマ：Jev に選ばせる「型付きの選択肢」の定義と、判定結果の型。

Jev は自由テキストを返せないため、すべての操作をここに列挙した選択肢の組み合わせで表す。
入力する文字列（type_text）だけは STT の結果からルールで切り出す（textparse.extract_text）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# action: 説明は Jev への判断基準として使われる
ACTIONS: dict[str, str] = {
    "launch_app": "Start / open a different application (e.g. 'メモ帳開いて', 'ブラウザ起動'). If the current app's menu "
                  "has a matching feature (a trainer, tool or window inside the app), choose menu_command instead.",
    "switch_window": "Bring an already open window to the front (e.g. 'Chromeに切り替え', 'さっきのウィンドウ').",
    "mouse_move": "Move the mouse pointer relative to where it is now in a direction (e.g. '右', 'もうちょい上').",
    "mouse_to_element": "Move the mouse pointer onto a named on-screen item without clicking (e.g. '保存の上に').",
    "click": "Click at the current mouse position (e.g. 'クリック', 'そこ押して', '右クリック', 'ダブルクリック').",
    "click_element": "Click a named button, menu, link or text visible on screen (e.g. '保存押して', 'OKをクリック').",
    "drag_start": "Press and hold the mouse button to start dragging (e.g. 'ドラッグ開始', 'つかんで').",
    "drag_end": "Release the held mouse button to drop (e.g. 'ここで離す', 'ドロップ').",
    "scroll": "Scroll the page or list (e.g. '下にスクロール', 'もっと下').",
    "type_text": "Type the dictated words as text (e.g. 'こんにちはと入力', '〜って打って'). If the phrase names a "
                 "button or feature of the current app (see facts), choose that instead.",
    "key_combo": "Press a keyboard key or shortcut, including browser back/forward/reload, tabs, volume and media "
                 "(e.g. 'コピー', '元に戻す', 'エンター', '前のページに戻って', '音量下げて', '次の曲').",
    "window_control": "Minimize, maximize, restore or close the current window, or show the desktop "
                      "(e.g. 'このウィンドウ小さくして', '閉じて', '最大化').",
    "snap_window": "Snap or move the current window with the keyboard: the left or right half of the screen, "
                   "screen-wide, or to the other monitor (e.g. '右半分にして', '左に寄せて', '別のモニターに移して').",
    "menu_command": "Run a command from the current app's menu bar, listed in the menu options "
                    "(e.g. '名前を付けて保存', '置換', 'ズームを戻して', 'ステータスバーを表示').",
    "app_command": "Run a special command registered for the current app, listed in the command options.",
    "learn_app": "Learn / memorize the menus of the current app (e.g. 'このアプリを覚えて', 'メニューを学習して').",
    "show_hints": "Show numbered labels on clickable items or a numbered grid (e.g. '番号表示', 'グリッド').",
    "repeat": "Do the previous command again (e.g. 'もう一回', 'もう一度', '同じの').",
    "stop": "Cancel / stop / never mind (e.g. 'ストップ', 'やめて', 'キャンセル').",
    "unknown": "The utterance is not a PC operation, is unclear, or is just noise.",
}

DIRECTIONS: dict[str, str] = {
    "up": "Up / 上", "down": "Down / 下", "left": "Left / 左", "right": "Right / 右",
    "up_left": "Up-left / 左上", "up_right": "Up-right / 右上",
    "down_left": "Down-left / 左下", "down_right": "Down-right / 右下",
}

# score 型：段階の並び順がそのまま量の大小になる
AMOUNTS: list[str] = [
    "A tiny bit (ちょっと, 少し, もうちょい)",
    "A normal amount (no size word given)",
    "A lot / far (大きく, もっと, いっぱい, 端まで)",
]
AMOUNT_KEYS = ["small", "medium", "large"]

BUTTONS: dict[str, str] = {
    "left": "Normal left click (クリック, 押して)",
    "right": "Right click (右クリック, メニュー出して)",
    "double": "Double click (ダブルクリック, 開いて on a file)",
    "middle": "Middle click (中クリック, ホイールクリック)",
}

# key_combo の選択肢。値は (説明, 仮想キー列)
VK = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "WIN": 0x5B}
KEYS: dict[str, tuple[str, list[int]]] = {
    "enter": ("Enter / 決定 / 改行", [0x0D]),
    "escape": ("Escape / 閉じる(ダイアログ) / 取り消し", [0x1B]),
    "tab": ("Tab / 次の項目", [0x09]),
    "shift_tab": ("Shift+Tab / 前の項目", [0x10, 0x09]),
    "backspace": ("Backspace / 一文字消す", [0x08]),
    "delete": ("Delete / 削除", [0x2E]),
    "space": ("Space / スペース", [0x20]),
    "arrow_up": ("Arrow up key / 上キー", [0x26]),
    "arrow_down": ("Arrow down key / 下キー", [0x28]),
    "arrow_left": ("Arrow left key / 左キー", [0x25]),
    "arrow_right": ("Arrow right key / 右キー", [0x27]),
    "home": ("Home / 先頭へ", [0x24]),
    "end": ("End / 末尾へ", [0x23]),
    "page_up": ("Page up / 前のページ", [0x21]),
    "page_down": ("Page down / 次のページ", [0x22]),
    "copy": ("Copy / コピー", [0x11, 0x43]),
    "paste": ("Paste / 貼り付け / ペースト", [0x11, 0x56]),
    "cut": ("Cut / 切り取り", [0x11, 0x58]),
    "undo": ("Undo / 元に戻す", [0x11, 0x5A]),
    "redo": ("Redo / やり直し", [0x11, 0x59]),
    "select_all": ("Select all / 全選択", [0x11, 0x41]),
    "save": ("Save / 保存 (Ctrl+S)", [0x11, 0x53]),
    "find": ("Find / 検索 (Ctrl+F)", [0x11, 0x46]),
    "new_tab": ("New tab / 新しいタブ", [0x11, 0x54]),
    "close_tab": ("Close tab / タブを閉じる", [0x11, 0x57]),
    "next_tab": ("Next tab / 次のタブ", [0x11, 0x09]),
    "prev_tab": ("Previous tab / 前のタブ", [0x11, 0x10, 0x09]),
    "reload": ("Reload / 更新 / 再読み込み", [0x74]),
    "back": ("Browser back, go to previous page / 前のページに戻る", [0x12, 0x25]),
    "forward": ("Browser forward, go to next page / 次のページに進む", [0x12, 0x27]),
    "zoom_in": ("Zoom in / 拡大", [0x11, 0xBB]),
    "zoom_out": ("Zoom out / 縮小", [0x11, 0xBD]),
    "zoom_reset": ("Reset zoom to 100% / ズームを戻す", [0x11, 0x30]),
    "address_bar": ("Focus the address / URL bar / アドレスバー", [0x11, 0x4C]),
    "bookmark": ("Bookmark this page / ブックマーク・お気に入りに追加", [0x11, 0x44]),
    "bookmarks_list": ("Open the bookmark manager / ブックマーク一覧", [0x11, 0x10, 0x4F]),
    "history": ("Open browsing history / 履歴", [0x11, 0x48]),
    "downloads": ("Open the downloads list / ダウンロード一覧", [0x11, 0x4A]),
    "reopen_tab": ("Reopen the last closed tab / 閉じたタブを開き直す", [0x11, 0x10, 0x54]),
    "save_as": ("Save as / 名前を付けて保存", [0x11, 0x10, 0x53]),
    "rename_file": ("Rename the selected file / 名前を変更", [0x71]),
    "new_folder": ("Create a new folder / 新しいフォルダ", [0x11, 0x10, 0x4E]),
    "find_next": ("Find the next match / 次を検索", [0x72]),
    "find_prev": ("Find the previous match / 前を検索", [0x10, 0x72]),
    "print": ("Print / 印刷", [0x11, 0x50]),
    "fullscreen": ("Fullscreen (F11) / フルスクリーン", [0x7A]),
    "dev_tools": ("Developer tools / 開発者ツール", [0x7B]),
    "word_delete": ("Delete the previous word / 一単語消す", [0x11, 0x08]),
    "lock_pc": ("Lock Windows / パソコンをロック", [0x5B, 0x4C]),
    "task_manager": ("Open Task Manager / タスクマネージャー", [0x11, 0x10, 0x1B]),
    "win_explorer": ("Open a File Explorer window (Win+E)", [0x5B, 0x45]),
    "clipboard_history": ("Open the clipboard history (Win+V) / クリップボード履歴", [0x5B, 0x56]),
    "desktop_next": ("Switch to the next virtual desktop / 次のデスクトップ", [0x5B, 0x11, 0x27]),
    "desktop_prev": ("Switch to the previous virtual desktop / 前のデスクトップ", [0x5B, 0x11, 0x25]),
    "desktop_new": ("Create a new virtual desktop / 新しいデスクトップ", [0x5B, 0x11, 0x44]),
    "new_window": ("New window / 新規作成 / 新しいウィンドウ", [0x11, 0x4E]),
    "incognito": ("Private / incognito window / シークレットウィンドウ", [0x11, 0x10, 0x4E]),
    "settings": ("Open Windows Settings (Win+I) / Windows の設定", [0x5B, 0x49]),
    "notifications": ("Open the notification center (Win+N) / 通知", [0x5B, 0x4E]),
    "replace": ("Find and replace / 置換", [0x11, 0x48]),
    "hard_reload": ("Reload ignoring the cache / 強制再読み込み", [0x11, 0x10, 0x52]),
    "page_top": ("Go to the top of the page / ページの先頭", [0x11, 0x24]),
    "page_bottom": ("Go to the end of the page / ページの最後", [0x11, 0x23]),
    "voice_typing": ("Voice typing (Win+H) / 音声入力", [0x5B, 0x48]),
    "emoji": ("Emoji picker (Win+.) / 絵文字", [0x5B, 0xBE]),
    "alt_tab": ("Switch to previous window / Alt+Tab", [0x12, 0x09]),
    "alt_f4": ("Quit app / Alt+F4 / アプリを終了", [0x12, 0x73]),
    "start_menu": ("Open Start menu / スタート", [0x5B]),
    "task_view": ("Task view / タスクビュー", [0x5B, 0x09]),
    "screenshot": ("Screenshot tool / スクショ", [0x5B, 0x10, 0x53]),
    "ime_toggle": ("Toggle Japanese input / 半角全角 / 日本語入力切替", [0xF3]),
    "volume_up": ("Volume up / 音量上げて", [0xAF]),
    "volume_down": ("Volume down / 音量下げて", [0xAE]),
    "mute": ("Mute / ミュート / 消音", [0xAD]),
    "media_play_pause": ("Play or pause media / 再生 / 一時停止", [0xB3]),
    "media_next": ("Next track / 次の曲", [0xB0]),
    "media_prev": ("Previous track / 前の曲", [0xB1]),
    # 2026-09-22 追加：もっと操作できるように
    "next_window": ("Switch to the next window (Alt+Esc) / 次のウィンドウ", [0x12, 0x1B]),
    "prev_window": ("Switch to the previous window / 前のウィンドウ", [0x12, 0x10, 0x1B]),
    "screenshot_save": ("Save a full-screen screenshot (Win+PrtScn) / 画面を丸ごと撮って保存",
                        [0x5B, 0x2C]),
}

WINDOW_ACTIONS: dict[str, str] = {
    "minimize": "Minimize / 最小化 / しまって",
    "maximize": "Maximize / 最大化 / 全画面",
    "restore": "Restore size / 元のサイズ",
    "close": "Close window / 閉じて",
    "show_desktop": "Show desktop / デスクトップ表示",
    "always_on_top": "Keep this window always on top / 常に手前に表示",
    "topmost_off": "Release always-on-top / 常に手前を解除",
}

# snap_window の選択肢。値は (説明, 仮想キー列)。SNAP_RECTS に載っている名前はキーの代わりに直接置く
SNAPS: dict[str, tuple[str, list[int] | None]] = {
    "left_half": ("Snap to the left half of the screen / 左半分 / ウィンドウを左に寄せて", None),
    "right_half": ("Snap to the right half of the screen / 右半分 / ウィンドウを右に寄せて", None),
    "screen_wide": ("Make the window fill the screen / 画面いっぱい", [0x5B, 0x26]),
    "prev_monitor": ("Send the window to the monitor on the left / 左のモニターへ / 前のモニターへ",
                     [0x5B, 0x10, 0x25]),
    "next_monitor": ("Send the window to the monitor on the right / 右のモニターへ / 別のモニターへ",
                     [0x5B, 0x10, 0x27]),
    # Win+矢印では出せない形。位置と大きさは SNAP_RECTS の割合で直接置く（キー列は None）
    "full": ("Make the window fill the work area / 作業領域いっぱい", None),
    "upper_half": ("Snap to the upper half of the screen / 上半分", None),
    "lower_half": ("Snap to the lower half of the screen / 下半分", None),
    "top_left": ("Snap to the top-left quarter / 左上の四分の一", None),
    "top_right": ("Snap to the top-right quarter / 右上の四分の一", None),
    "bottom_left": ("Snap to the bottom-left quarter / 左下の四分の一", None),
    "bottom_right": ("Snap to the bottom-right quarter / 右下の四分の一", None),
    "center": ("Put the window in the middle of the screen / 中央に置く", None),
}

# SNAP_RECTS に載っている名前は、キーではなく作業領域に対する割合 (左, 上, 右, 下) で直接置く
SNAP_RECTS: dict[str, tuple[float, float, float, float]] = {
    "left_half": (0.0, 0.0, 0.5, 1.0), "right_half": (0.5, 0.0, 1.0, 1.0),
    "full": (0.0, 0.0, 1.0, 1.0),
    "upper_half": (0.0, 0.0, 1.0, 0.5), "lower_half": (0.0, 0.5, 1.0, 1.0),
    "top_left": (0.0, 0.0, 0.5, 0.5), "top_right": (0.5, 0.0, 1.0, 0.5),
    "bottom_left": (0.0, 0.5, 0.5, 1.0), "bottom_right": (0.5, 0.5, 1.0, 1.0),
    "center": (0.15, 0.1, 0.85, 0.9),
}

HINT_MODES: dict[str, str] = {
    "elements": "Number the clickable items (番号表示, 番号出して)",
    "grid": "Number a grid over the screen (グリッド, マス目)",
}

# 各 action が必要とするパラメータ（= Jev の質問ID）
ACTION_PARAMS: dict[str, list[str]] = {
    "launch_app": ["app"],
    "switch_window": ["window"],
    "mouse_move": ["direction", "amount"],
    "mouse_to_element": ["element"],
    "click": ["button"],
    "click_element": ["element", "button"],
    "drag_start": [],
    "drag_end": [],
    "scroll": ["direction", "amount"],
    "type_text": [],
    "key_combo": ["key"],
    "window_control": ["window_action"],
    "snap_window": ["snap"],
    "show_hints": ["hint_mode"],
    "menu_command": ["menu"],
    "app_command": ["command"],
    "learn_app": [],
    "repeat": [],
    "stop": [],
    "unknown": [],
}

# 自信度の計算で考慮しないパラメータ（既定値で困らないもの）
SOFT_PARAMS = {"amount", "button"}


@dataclass
class Candidate:
    """画面上のクリック対象や、起動候補のアプリなど、実行時に解決する選択肢。"""
    id: str
    label: str
    data: Any = None


@dataclass
class Decision:
    action: str
    params: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0           # action と必須パラメータの自信度の最小値
    detail: dict[str, float] = field(default_factory=dict)  # 質問ごとの自信度
    engine: str = ""
    text: str | None = None           # type_text で入力する文字列
    is_continuation: float = 0.0      # 「もうちょい」「さっきの」など前回の続きである確率
    is_pc_command: float = 1.0        # PC への命令である確率（会話や独り言なら低い）
    top_actions: dict = field(default_factory=dict)  # Jev の action の上位の確率（ログ・調整用）
    expect_hwnd: int = 0              # 判定した時点の前面ウィンドウ（実行の直前に照合する）
    supported: bool = False           # コード側の照合（言い方の一致・発音照合）が同じ対象を強く支持しているか
    alternatives: dict = field(default_factory=dict)   # 対象の候補ごとの Jev の確率（上位 3 つ）{質問ID: [(候補ID, 確率)]}
    latency_ms: float = 0.0

    def describe(self, lookup: dict[str, dict[str, Candidate]] | None = None) -> str:
        from .describe import describe
        return describe(self, lookup or {})
