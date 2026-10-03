"""全局热键。

用 Win32 的 RegisterHotKey，不引入 keyboard / pynput —— 那两个库要么
需要额外权限，要么会装全局钩子拖慢系统。内核级的注册最干净。

WM_HOTKEY 通过 QAbstractNativeEventFilter 接住，和 Qt 事件循环天然合流。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .utils import log

_log = log.get("hotkey")

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

_HOTKEY_ID = 0xC7A1


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]


def parse(spec: str) -> tuple[int, int] | None:
    """'alt+q' → (modifiers, virtual_key)。解析失败返回 None。"""
    if not spec:
        return None
    parts = [p.strip().lower() for p in spec.replace("+", " ").split() if p.strip()]
    if not parts:
        return None

    modifiers = 0
    key_token = None
    for token in parts:
        if token in _MODIFIERS:
            modifiers |= _MODIFIERS[token]
        else:
            key_token = token

    if key_token is None:
        return None

    # 必须带至少一个修饰键。没有修饰键的话，注册成功会全局劫持那个按键
    # （比如把 "q" 注册掉，用户在别的程序里就再也打不出 q 了）。
    if modifiers == 0:
        return None

    vk = _virtual_key(key_token)
    if vk is None:
        return None
    return modifiers | MOD_NOREPEAT, vk


def _virtual_key(token: str) -> int | None:
    if len(token) == 1:
        ch = token.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
        return None
    if token.startswith("f") and token[1:].isdigit():
        index = int(token[1:])
        if 1 <= index <= 24:
            return 0x70 + index - 1
    named = {
        "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
        "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
        "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    }
    return named.get(token)


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    triggered = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._registered = False
        self._spec = ""
        self._app: QApplication | None = None

    @property
    def spec(self) -> str:
        return self._spec

    @property
    def is_registered(self) -> bool:
        return self._registered

    def register(self, spec: str, app: QApplication) -> tuple[bool, str]:
        self.unregister()
        self._app = app

        if sys.platform != "win32":
            return False, "全局热键目前只支持 Windows"

        parsed = parse(spec)
        if parsed is None:
            return False, f"无法解析热键「{spec}」，格式应形如 alt+q"

        modifiers, vk = parsed
        try:
            ok = ctypes.windll.user32.RegisterHotKey(None, _HOTKEY_ID, modifiers, vk)
        except Exception as exc:
            return False, f"注册热键失败：{exc}"

        if not ok:
            return False, (
                f"热键 {spec} 已被其他程序占用。"
                "请在 config.toml 里换一个（例如 alt+shift+Q），然后重启。"
            )

        app.installNativeEventFilter(self)
        self._registered = True
        self._spec = spec
        _log.info("全局热键已注册：%s", spec)
        return True, ""

    def unregister(self) -> None:
        if self._registered:
            try:
                ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
            except Exception:
                pass
        if self._app is not None:
            try:
                self._app.removeNativeEventFilter(self)
            except Exception:
                pass
        self._registered = False
        self._app = None

    # ------------------------------------------------------------ 事件过滤

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if not self._registered:
            return False, 0
        try:
            if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
                if msg.message == WM_HOTKEY and int(msg.wParam) == _HOTKEY_ID:
                    self.triggered.emit()
        except Exception:
            pass
        return False, 0
