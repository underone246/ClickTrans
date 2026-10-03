"""全屏框选覆盖窗。

设计要点：覆盖窗显示的是**已经抓好的静态截图**，不是透明玻璃窗。
原因有两个：
1. 透明窗在视频播放器 / GPU 加速窗口 / 独占全屏下会抓不到底下的内容
2. 静态截图保证「所见即所得」——你框的就是你看到的

这也带来一个额外好处：覆盖窗可以放心地抢焦点（原窗口失焦也无所谓，
因为画面已经冻结了），于是 Esc / Shift 的键盘处理变得非常可靠。

每块屏幕一个独立的覆盖窗，不做跨屏大窗——跨屏窗口在混合 DPI 下会被
系统拉伸，选框和实际内容对不上。拖拽被限制在鼠标按下时所在的那块屏幕内。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QWidget

from ..lang import LABEL, Lang, next_preferred
from ..utils import log
from .geometry import ScreenShot
from .grabber import capture_screens

_log = log.get("overlay")

_MASK = QColor(0, 0, 0, 100)
_ACCENT = QColor(76, 154, 255)
_LABEL_BG = QColor(20, 22, 26, 220)
_LABEL_FG = QColor(235, 238, 245)
_MIN_SIZE = 5


class Overlay(QWidget):
    """单块屏幕上的框选窗。"""

    selected = pyqtSignal(object, object)  # (ScreenShot, QRect 逻辑局部坐标)
    cancelled = pyqtSignal()

    def __init__(self, shot: ScreenShot, preferred: Lang, parent: QWidget | None = None):
        super().__init__(parent)
        self._shot = shot
        self._preferred = preferred

        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._cursor: QPoint | None = None
        self._dragging = False

        # 用 setDevicePixelRatio 让 Qt 按逻辑尺寸 1:1 贴图，避免二次缩放糊掉
        self._pixmap = QPixmap.fromImage(shot.image)
        self._pixmap.setDevicePixelRatio(shot.dpr)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(shot.logical_rect)

        self._font = QFont()
        self._font.setPointSize(10)
        self._font.setBold(True)

    # ------------------------------------------------------------ 生命周期

    def open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)

        # 1. 整屏冻结画面
        painter.drawPixmap(0, 0, self._pixmap)

        # 2. 灰化遮罩
        painter.fillRect(self.rect(), _MASK)

        selection = self.selection_rect()
        if selection.isNull():
            self._paint_crosshair(painter)
        else:
            # 3. 选区内重绘原图 → 完全透亮
            painter.save()
            painter.setClipRect(selection)
            painter.drawPixmap(0, 0, self._pixmap)
            painter.restore()

            # 4. 选框边框与尺寸标注
            painter.setPen(QPen(_ACCENT, 1))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            self._paint_size(painter, selection)

        self._paint_hint(painter)
        painter.end()

    def _paint_crosshair(self, painter: QPainter) -> None:
        if self._cursor is None:
            return
        painter.setPen(QPen(_ACCENT, 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, self._cursor.y(), self.width(), self._cursor.y())
        painter.drawLine(self._cursor.x(), 0, self._cursor.x(), self.height())

    def _paint_size(self, painter: QPainter, selection: QRect) -> None:
        text = f"{selection.width()} × {selection.height()}"
        painter.setFont(self._font)
        metrics = painter.fontMetrics()
        pad = 5
        box = QRect(0, 0, metrics.horizontalAdvance(text) + pad * 2, metrics.height() + pad)

        # 优先贴在选框下方，下方放不下就放到内部顶端
        x = selection.left()
        y = selection.bottom() + 4
        if y + box.height() > self.height():
            y = max(0, selection.top() - box.height() - 4)
        x = max(0, min(x, self.width() - box.width()))
        box.moveTo(x, y)

        painter.fillRect(box, _LABEL_BG)
        painter.setPen(_LABEL_FG)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_hint(self, painter: QPainter) -> None:
        text = f"偏好语言：{LABEL[self._preferred]}　（Shift 切换）"
        painter.setFont(self._font)
        metrics = painter.fontMetrics()
        pad = 8
        box = QRect(
            16, 16,
            metrics.horizontalAdvance(text) + pad * 2,
            metrics.height() + pad,
        )
        painter.fillRect(box, _LABEL_BG)
        painter.setPen(_LABEL_FG)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # ------------------------------------------------------------- 交互

    def selection_rect(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        x1, y1 = self._origin.x(), self._origin.y()
        x2, y2 = self._current.x(), self._current.y()
        rect = QRect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        return self._clamp(rect)

    def _clamp(self, point_or_rect):
        if isinstance(point_or_rect, QPoint):
            return QPoint(
                max(0, min(point_or_rect.x(), self.width() - 1)),
                max(0, min(point_or_rect.y(), self.height() - 1)),
            )
        return point_or_rect.intersected(self.rect())

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = True
        self._origin = self._clamp(event.position().toPoint())
        self._current = self._origin
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._cursor = self._clamp(event.position().toPoint())
        if self._dragging:
            self._current = self._cursor
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = self._clamp(event.position().toPoint())
        selection = self.selection_rect()

        if selection.width() < _MIN_SIZE or selection.height() < _MIN_SIZE:
            # 误点一下而已，不要当成取消，让用户继续框
            self._origin = self._current = None
            self.update()
            return

        self.selected.emit(self._shot, selection)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        if key == Qt.Key.Key_Shift:
            self._preferred = next_preferred(self._preferred)
            self.update()
            return
        super().keyPressEvent(event)


class CaptureSession:
    """管理一次框选：抓图 → 每屏开一个覆盖窗 → 等待结果。"""

    def __init__(self, preferred: Lang):
        self._preferred = preferred
        self._overlays: list[Overlay] = []
        self._finished = False

    @property
    def preferred(self) -> Lang:
        return self._preferred

    def start(self, on_selected, on_cancelled) -> bool:
        shots = capture_screens()
        if not shots:
            _log.error("抓取屏幕失败，无法开始框选")
            on_cancelled()
            return False

        _log.info("开始框选：%s", " | ".join(s.describe() for s in shots))

        for shot in shots:
            overlay = Overlay(shot, self._preferred)
            overlay.selected.connect(lambda s, r: self._on_selected(s, r, on_selected))
            overlay.cancelled.connect(lambda: self._on_cancelled(on_cancelled))
            self._overlays.append(overlay)

        for overlay in self._overlays:
            overlay.open()

        # 把焦点给鼠标所在的那块屏幕，这样 Esc / Shift 能立刻生效
        self._focus_screen_under_cursor()
        return True

    def _focus_screen_under_cursor(self) -> None:
        pos = QCursor.pos()
        for overlay in self._overlays:
            if overlay.geometry().contains(pos):
                overlay.raise_()
                overlay.activateWindow()
                overlay.setFocus(Qt.FocusReason.OtherFocusReason)
                return
        if self._overlays:
            self._overlays[0].raise_()

    def _on_selected(self, shot: ScreenShot, rect: QRect, callback) -> None:
        if self._finished:
            return
        self._finished = True
        self.close_all()
        callback(shot, rect)

    def _on_cancelled(self, callback) -> None:
        if self._finished:
            return
        self._finished = True
        self.close_all()
        callback()

    def close_all(self) -> None:
        for overlay in self._overlays:
            try:
                overlay.hide()
                overlay.close()
                overlay.deleteLater()
            except Exception:
                pass
        self._overlays.clear()
