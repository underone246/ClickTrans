"""进程内存查询。托盘菜单里「查看当前内存占用」用这个。

用 GetProcessMemoryInfo 拿 WorkingSetSize —— 就是任务管理器里显示的那个
数字，用户能对得上。

两个坑：
1. `GetCurrentProcess()` 返回的是伪句柄 -1，必须显式声明 restype 为 HANDLE，
   否则 ctypes 默认按 c_int 处理会拿到错误的值
2. 新系统上函数在 kernel32 里叫 K32GetProcessMemoryInfo，psapi 那个是
   兼容入口。两个都试一遍最稳
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_get_info = None


def _resolve() -> object | None:
    global _get_info
    if _get_info is not None:
        return _get_info or None
    if sys.platform != "win32":
        _get_info = False
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        candidates = [getattr(kernel32, "K32GetProcessMemoryInfo", None)]
        try:
            candidates.append(ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo)
        except Exception:
            pass

        fn = next((c for c in candidates if c is not None), None)
        if fn is None:
            _get_info = False
            return None

        fn.restype = wintypes.BOOL
        fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        _get_info = (fn, kernel32)
        return _get_info
    except Exception:
        _get_info = False
        return None


def working_set_bytes() -> int:
    """当前进程工作集字节数。拿不到就返回 0。"""
    resolved = _resolve()
    if not resolved:
        return 0
    try:
        fn, kernel32 = resolved
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = kernel32.GetCurrentProcess()
        if not fn(handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    except Exception:
        return 0


def peak_working_set_bytes() -> int:
    resolved = _resolve()
    if not resolved:
        return 0
    try:
        fn, kernel32 = resolved
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not fn(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.PeakWorkingSetSize)
    except Exception:
        return 0


def human_mb(value: int) -> str:
    if value <= 0:
        return "未知"
    return f"{value / 1024 / 1024:.0f} MB"
