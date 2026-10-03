"""屏幕图像与 numpy 之间的转换，以及 OCR 前的预处理。"""

from __future__ import annotations

import numpy as np
from PyQt6.QtGui import QImage


def qimage_to_ndarray(image: QImage) -> np.ndarray:
    """QImage → (h, w, 3) uint8 RGB。"""
    img = image.convertToFormat(QImage.Format.Format_RGB888)
    w, h, stride = img.width(), img.height(), img.bytesPerLine()
    if w <= 0 or h <= 0:
        return np.empty((0, 0, 3), dtype=np.uint8)

    raw = img.constBits()
    if hasattr(raw, "setsize"):
        raw.setsize(stride * h)
        buf = np.frombuffer(raw, dtype=np.uint8)
    else:
        buf = np.frombuffer(raw, dtype=np.uint8, count=stride * h)

    # bytesPerLine 可能带行尾填充，先按 stride 还原再裁掉
    return buf.reshape(h, stride)[:, : w * 3].reshape(h, w, 3).copy()


def upscale(arr: np.ndarray, factor: float) -> np.ndarray:
    """最近邻放大。小字号文字放大后识别率提升明显，代价很低。

    刻意不用双三次插值：插值会把笔画边缘糊掉，反而降低 OCR 准确率；
    最近邻保留硬边缘，对字符识别更友好。
    """
    if factor <= 1.01:
        return arr
    h, w = arr.shape[:2]
    nh, nw = max(1, int(round(h * factor))), max(1, int(round(w * factor)))
    yi = (np.arange(nh) * (h / nh)).astype(np.int32).clip(0, h - 1)
    xi = (np.arange(nw) * (w / nw)).astype(np.int32).clip(0, w - 1)
    return arr[yi][:, xi]


def fit_within(arr: np.ndarray, max_pixels: int) -> np.ndarray:
    """超过像素上限就整体缩小，避免超大选区把推理拖成几秒。

    缩小用步长采样（等价于 nearest 的逆操作），保持速度。
    """
    h, w = arr.shape[:2]
    total = h * w
    if total <= max_pixels or total == 0:
        return arr
    ratio = (max_pixels / total) ** 0.5
    nh, nw = max(1, int(h * ratio)), max(1, int(w * ratio))
    yi = (np.arange(nh) * (h / nh)).astype(np.int32).clip(0, h - 1)
    xi = (np.arange(nw) * (w / nw)).astype(np.int32).clip(0, w - 1)
    return arr[yi][:, xi]


def prepare_for_ocr(
    arr: np.ndarray,
    upscale_factor: float = 2.0,
    max_pixels: int = 4_000_000,
) -> np.ndarray:
    """预处理流水线：先限制总像素，再放大。

    顺序不能反——先放大再限制会把刚放大的细节又丢掉。
    """
    return upscale(fit_within(arr, max_pixels), upscale_factor)


def is_blank(arr: np.ndarray, tolerance: int = 6) -> bool:
    """判断是否近似纯色（常用于识别「截到全黑」的失败场景）。

    注意：用户可能真的在框选一个全黑的窗口，所以这个结果只用于
    日志与提示，不能用来直接判定截图失败。
    """
    if arr.size == 0:
        return True
    sample = arr[::4, ::4] if arr.shape[0] > 8 and arr.shape[1] > 8 else arr
    return bool(np.ptp(sample) <= tolerance)
