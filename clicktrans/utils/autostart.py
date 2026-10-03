"""开机自启。

写 HKCU 的 Run 键，不需要管理员权限（写 HKLM 才需要）。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ClickTrans"


def _command() -> str:
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 就是 exe 本身
        return f'"{sys.executable}"'

    python = Path(sys.executable)
    # 用 pythonw 启动，否则每次开机都会闪一个黑色控制台窗口
    pythonw = python.with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else python
    run_py = Path(__file__).resolve().parents[2] / "run.py"
    return f'"{launcher}" "{run_py}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "只支持 Windows"
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
        return True, ""
    except Exception as exc:
        return False, str(exc)


def disable() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "只支持 Windows"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True, ""
    except FileNotFoundError:
        return True, ""  # 本来就没有，当成成功
    except Exception as exc:
        return False, str(exc)


def apply(enabled: bool) -> tuple[bool, str]:
    """把状态同步到注册表。已经是目标状态就什么都不做。"""
    if enabled and not is_enabled():
        return enable()
    if not enabled and is_enabled():
        return disable()
    return True, ""
