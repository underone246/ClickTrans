"""DPI 坐标换算 —— 整个项目最容易出 bug 的地方。

这些用例是纯粹的数学验证，不需要真的接显示器。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QRect, QSize

from clicktrans.capture.geometry import physical_rect


class TestPhysicalRect:
    def test_100_percent_is_identity(self):
        rect = physical_rect(QRect(10, 20, 100, 50), 1.0, QSize(1920, 1080))
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (10, 20, 100, 50)

    def test_150_percent_scales_up(self):
        rect = physical_rect(QRect(10, 20, 100, 50), 1.5, QSize(2880, 1620))
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (15, 30, 150, 75)

    def test_200_percent_scales_up(self):
        rect = physical_rect(QRect(10, 20, 100, 50), 2.0, QSize(3840, 2160))
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (20, 40, 200, 100)

    def test_125_percent_rounds(self):
        # 125% 缩放下 7 逻辑像素 = 8.75 物理像素 → 9
        rect = physical_rect(QRect(3, 7, 100, 100), 1.25, QSize(2000, 1125))
        assert (rect.x(), rect.y()) == (4, 9)

    def test_invalid_dpr_falls_back_to_one(self):
        rect = physical_rect(QRect(5, 5, 10, 10), 0.0, QSize(100, 100))
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (5, 5, 10, 10)

    @pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5])
    def test_never_exceeds_bounds(self, dpr):
        """任何缩放下裁剪矩形都不能越界 —— 越界会截出黑边或直接崩。"""
        bounds = QSize(int(1920 * dpr), int(1080 * dpr))
        rect = physical_rect(QRect(0, 0, 5000, 5000), dpr, bounds)
        assert rect.x() >= 0 and rect.y() >= 0
        assert rect.x() + rect.width() <= bounds.width()
        assert rect.y() + rect.height() <= bounds.height()

    def test_negative_origin_is_clamped(self):
        rect = physical_rect(QRect(-50, -30, 100, 100), 1.0, QSize(1920, 1080))
        assert rect.x() == 0 and rect.y() == 0
        # 宽度被截断，但不会变成负数
        assert rect.width() >= 0 and rect.height() >= 0

    def test_zero_size_selection(self):
        rect = physical_rect(QRect(10, 10, 0, 0), 1.0, QSize(100, 100))
        assert rect.width() == 0 and rect.height() == 0

    def test_empty_bounds(self):
        rect = physical_rect(QRect(10, 10, 50, 50), 1.0, QSize(0, 0))
        assert rect.width() == 0 and rect.height() == 0

    def test_selection_at_far_edge(self):
        """贴着右下角框选，不能被截掉。"""
        bounds = QSize(1920, 1080)
        rect = physical_rect(QRect(1820, 1030, 100, 50), 1.0, bounds)
        assert rect.right() == bounds.width() - 1 or rect.x() + rect.width() == bounds.width()
        assert rect.width() == 100
        assert rect.height() == 50


class TestRoundTrip:
    """逻辑坐标 → 物理坐标换算必须可逆，否则选框和内容会对不上。"""

    @pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
    def test_scaled_selection_maps_back(self, dpr):
        logical_screen = QSize(int(1920 / dpr), int(1080 / dpr))
        bounds = QSize(int(logical_screen.width() * dpr), int(logical_screen.height() * dpr))

        local = QRect(100, 200, 300, 150)
        phys = physical_rect(local, dpr, bounds)

        # 反算回逻辑坐标，误差不能超过 1 像素
        back = QRect(
            round(phys.x() / dpr),
            round(phys.y() / dpr),
            round(phys.width() / dpr),
            round(phys.height() / dpr),
        )
        assert abs(back.x() - local.x()) <= 1
        assert abs(back.y() - local.y()) <= 1
        assert abs(back.width() - local.width()) <= 1
        assert abs(back.height() - local.height()) <= 1
