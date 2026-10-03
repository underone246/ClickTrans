"""多屏 + DPI 坐标换算。

这是整个项目最容易出 bug 的地方，所以规矩定死：

**capture 模块内部一律使用物理像素，只在和 Qt 窗口系统交互的边界做一次换算。**

Qt6 默认开启 HiDPI，会同时存在两套坐标系：
- 逻辑坐标（Qt 窗口系统用的，`QScreen.geometry()` 返回的就是）
- 物理坐标（真实像素，`QImage` 里的就是）

在 150% 缩放的屏幕上框选 100px，物理上是 150px。搞混了就会截到错误区域，
或者截出一张糊图。

另外 Windows 虚拟桌面的原点是可能为负的（副屏设在主屏左侧或上方），
所以绝不要假设主屏在 (0, 0)。
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QRect, QSize
from PyQt6.QtGui import QGuiApplication, QImage, QScreen


def physical_rect(local_logical: QRect, dpr: float, bounds: QSize) -> QRect:
    """逻辑局部矩形 → 物理像素矩形，并夹紧到图像边界内。

    local_logical 是相对于覆盖窗（也就是屏幕）左上角的坐标，
    而不是虚拟桌面坐标——这样不用处理负号。

    bounds 是图像的物理尺寸。
    """
    if dpr <= 0:
        dpr = 1.0

    x = int(round(local_logical.x() * dpr))
    y = int(round(local_logical.y() * dpr))
    w = int(round(local_logical.width() * dpr))
    h = int(round(local_logical.height() * dpr))

    # 夹紧到图像范围
    x = max(0, min(x, bounds.width()))
    y = max(0, min(y, bounds.height()))
    w = max(0, min(w, bounds.width() - x))
    h = max(0, min(h, bounds.height() - y))
    return QRect(x, y, w, h)


@dataclass
class ScreenShot:
    """一块屏幕在某一时刻的冻结画面。

    image 是**物理像素**的整屏图；logical_rect 是屏幕在虚拟桌面里的
    逻辑位置（可能为负，仅用于摆放覆盖窗，不参与裁剪计算）。
    """

    screen: QScreen
    logical_rect: QRect
    dpr: float
    image: QImage

    @property
    def logical_size(self) -> QSize:
        """屏幕的逻辑尺寸，覆盖窗要按这个 size 摆。"""
        return self.logical_rect.size()

    @property
    def phys_size(self) -> QSize:
        return self.image.size()

    def crop(self, local_logical: QRect) -> QImage:
        """按逻辑局部矩形裁剪，返回物理像素的子图。"""
        rect = physical_rect(local_logical, self.dpr, self.image.size())
        if rect.width() <= 0 or rect.height() <= 0:
            return QImage()
        return self.image.copy(rect)

    def describe(self) -> str:
        return (
            f"{self.screen.name()} "
            f"logical={self.logical_rect.width()}x{self.logical_rect.height()} "
            f"phys={self.image.width()}x{self.image.height()} "
            f"dpr={self.dpr:g}"
        )


def grab_all_screens() -> list[ScreenShot]:
    """抓取所有屏幕的整屏画面。

    每屏独立抓取并各自记录 dpr。不要试图用一张跨屏大图——混合 DPI 下
    系统会把窗口拉伸，选框和实际内容对不上。
    """
    shots: list[ScreenShot] = []
    for screen in QGuiApplication.screens():
        try:
            pixmap = screen.grabWindow(0)
        except Exception:
            continue
        if pixmap.isNull():
            continue

        image = pixmap.toImage()
        if image.isNull():
            continue

        geo = screen.geometry()
        # 以图像实际尺寸为准反推 dpr，比直接信 devicePixelRatio() 稳
        # （某些缩放组合下两者会有 1px 级别的偏差）
        dpr = pixmap.devicePixelRatio()
        if dpr <= 0:
            dpr = screen.devicePixelRatio() or 1.0
        if geo.width() > 0:
            measured = image.width() / geo.width()
            if abs(measured - dpr) > 0.02:
                dpr = measured

        shots.append(
            ScreenShot(
                screen=screen,
                logical_rect=geo,
                dpr=float(dpr),
                image=image,
            )
        )
    return shots


def virtual_desktop_logical_rect() -> QRect:
    """所有屏幕的并集（逻辑坐标，原点可能为负）。"""
    rect = QRect()
    for screen in QGuiApplication.screens():
        rect = rect.united(screen.geometry())
    return rect
