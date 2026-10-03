"""单实例锁。

用 Windows 命名互斥体。进程退出时内核自动释放，不会像锁文件那样残留。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "Global\\ClickTrans.SingleInstance"):
        self.name = name
        self._handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        """返回 True 表示本进程是唯一实例；False 表示已有实例在跑。"""
        if sys.platform != "win32":
            return True
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            handle = k32.CreateMutexW(None, False, self.name)
            if not handle:
                return True  # 拿不到互斥体就当没锁，不要因此拒绝启动
            self._kernel32 = k32
            self._handle = handle
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                self.release()
                return False
            return True
        except Exception:
            return True

    def release(self) -> None:
        if self._handle and self._kernel32:
            try:
                self._kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
