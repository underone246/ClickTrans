"""屏幕抓取入口，含 DXGI 兜底。

Qt 的 `QScreen.grabWindow(0)` 走 GDI，覆盖 95% 场景。但游戏、全屏视频、
部分 Electron 应用走 DXGI 独占交换链，GDI 会返回纯黑。

降级链：
1. Qt grabWindow（首选）
2. 检测到「所有屏幕都是纯色」→ 试 dxcam / Windows.Graphics.Capture
3. 仍失败 → 交给上层提示用户

注意：用户可能真的在框一个全黑的窗口，所以纯色检测只作为**触发器**，
最终以 dxcam 是否给出有效结果为准。
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtGui import QImage

from ..utils import log
from ..utils.image import is_blank, qimage_to_ndarray
from .geometry import ScreenShot, grab_all_screens, virtual_desktop_logical_rect

_log = log.get("capture")


def ndarray_to_qimage(arr: np.ndarray) -> QImage:
    """(h, w, 3) uint8 RGB → QImage（会拷贝数据，保证内存独立）。"""
    arr = np.ascontiguousarray(arr[:, :, :3])
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def _try_dxcam(shots: list[ScreenShot]) -> list[ScreenShot] | None:
    """用 DXGI 桌面复制重新抓一遍。dxcam 未安装或失败则返回 None。"""
    try:
        import dxcam  # type: ignore
    except Exception:
        _log.info("dxcam 未安装，跳过 DXGI 兜底")
        return None

    try:
        camera = dxcam.create(output_color="RGB")
        frame = camera.grab()
        del camera
    except Exception as exc:
        _log.warning("dxcam 抓取失败: %s", exc)
        return None

    if frame is None:
        return None

    origin = virtual_desktop_logical_rect().topLeft()
    result: list[ScreenShot] = []
    for shot in shots:
        dpr = shot.dpr
        ox = int(round((shot.logical_rect.x() - origin.x()) * dpr))
        oy = int(round((shot.logical_rect.y() - origin.y()) * dpr))
        w, h = shot.image.width(), shot.image.height()
        sub = frame[oy : oy + h, ox : ox + w]
        if sub.shape[0] != h or sub.shape[1] != w:
            continue
        result.append(
            ScreenShot(shot.screen, shot.logical_rect, dpr, ndarray_to_qimage(sub))
        )

    return result or None


def capture_screens(enable_fallback: bool = True) -> list[ScreenShot]:
    """抓取所有屏幕。返回空列表表示完全抓不到画面。"""
    shots = grab_all_screens()
    if not shots:
        _log.error("所有屏幕都抓取失败")
        return []

    if not enable_fallback:
        return shots

    blank = [is_blank(qimage_to_ndarray(s.image)) for s in shots]
    if not all(blank):
        return shots

    _log.warning("所有屏幕都是纯色，疑似 DXGI 独占，尝试 dxcam 兜底")
    fallback = _try_dxcam(shots)
    if fallback:
        still_blank = all(is_blank(qimage_to_ndarray(s.image)) for s in fallback)
        if not still_blank:
            _log.info("dxcam 兜底成功")
            return fallback
        _log.warning("dxcam 也拿到纯色画面")

    return shots


def describe(shots: list[ScreenShot]) -> str:
    return " | ".join(s.describe() for s in shots)
