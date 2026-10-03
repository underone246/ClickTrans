"""结果悬浮卡片。

三件事决定了它好不好用：

**限频渲染。** 每收到一个 token 就 adjustSize() 一次，卡片会疯狂抖动，
CPU 也吃不消。这里的做法是 token 先进缓冲区，50 ms 合并刷新一次，
而且完全不用 adjustSize() —— 高度由 QLabel.heightForWidth() 算出来，
是确定性的，不会闪。

**固定宽度、高度只增不减。** 宽度写死，高度按内容算。向上生长时固定
底边，避免卡片上下跳。

**方向徽标。** 标题栏写清 `EN → 中文`，用户一眼就能确认方向对不对——
这是三语场景下最容易出错的地方。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..lang import LABEL, Lang
from ..translate.base import TranslateResult
from . import theme

PAD = 14
HEADER_H = 26
FOOTER_H = 30
MARGIN = 12
RADIUS = 10
FLUSH_MS = 50


class ResultCard(QWidget):
    retryRequested = pyqtSignal()
    swapRequested = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config.ui
        self._palette = theme.pick(self._cfg.theme)
        self._card_width = int(self._cfg.card_width)

        self._source_text = ""
        self._target_text = ""
        self._stream_buffer: list[str] = []
        self._showing_source = False
        self._from_history = False

        self._anchor = QRect()
        self._screen_rect = QRect()
        self._place_above = False
        self._fixed_edge = 0
        self._drag_offset: QPoint | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._build_ui()

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._auto_hide)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        p = self._palette
        fs = int(self._cfg.font_size)

        root = QVBoxLayout(self)
        root.setContentsMargins(PAD, PAD - 4, PAD, PAD - 6)
        root.setSpacing(6)

        # --- 标题栏
        header = QHBoxLayout()
        header.setSpacing(8)

        self._badge = QLabel("")
        self._badge.setObjectName("badge")
        header.addWidget(self._badge)

        self._status = QLabel("")
        self._status.setObjectName("status")
        header.addWidget(self._status)
        header.addStretch(1)

        self._close_btn = QPushButton("✕")
        self._close_btn.setObjectName("tool")
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(self.dismiss)
        header.addWidget(self._close_btn)
        root.addLayout(header)

        # --- 正文
        self._body = QLabel("")
        self._body.setObjectName("body")
        self._body.setWordWrap(True)
        self._body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("scroll")
        self._scroll.setWidget(self._body)
        # 刻意关掉 widgetResizable：宽度和高度都由 _relayout() 算出来，
        # 让滚动区自己去猜会和高度计算打架，导致逐字换行的抖动。
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(self._scroll)

        # --- 底栏
        footer = QHBoxLayout()
        footer.setSpacing(2)
        self._toggle_btn = self._tool_button("原文", self.toggle_source)
        self._copy_btn = self._tool_button("复制", self.copy_text)
        self._retry_btn = self._tool_button("重译", self.retryRequested.emit)
        self._swap_btn = self._tool_button("换向", self.swapRequested.emit)
        for btn in (self._toggle_btn, self._copy_btn, self._retry_btn, self._swap_btn):
            footer.addWidget(btn)
        footer.addStretch(1)
        root.addLayout(footer)

        self.setStyleSheet(
            f"""
            #badge {{ color: {p.accent}; font-size: {fs - 2}px; font-weight: 500; }}
            #status {{ color: {p.muted}; font-size: {fs - 3}px; }}
            #body {{ color: {p.text}; font-size: {fs}px; background: transparent; }}
            #scroll {{ background: transparent; border: none; }}
            #scroll > QWidget > QWidget {{ background: transparent; }}
            QPushButton#tool {{
                color: {p.muted}; background: transparent; border: none;
                padding: 3px 9px; border-radius: 5px; font-size: {fs - 3}px;
            }}
            QPushButton#tool:hover {{ background: {p.divider}; color: {p.text}; }}
            QScrollBar:vertical {{
                background: transparent; width: 8px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {p.divider}; border-radius: 4px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            """
        )
        self._apply_frame()

    def _tool_button(self, text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("tool")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(slot)
        return btn

    def _apply_frame(self) -> None:
        """背景单独用 paintEvent 画，方便做圆角和透明度。"""
        self.update()

    # ------------------------------------------------------------- 生命周期

    def begin(
        self,
        src: Lang | None,
        tgt: Lang | None,
        anchor: QRect,
        screen_rect: QRect,
    ) -> None:
        self._source_text = ""
        self._target_text = ""
        self._stream_buffer.clear()
        self._showing_source = False
        self._from_history = False

        self._anchor = QRect(anchor)
        self._screen_rect = QRect(screen_rect)

        self._badge.setText(self._direction_text(src, tgt) if src and tgt else "识别中…")
        self._status.setText("")
        self._body.setText("")
        self._body.setStyleSheet("")
        self._toggle_btn.setEnabled(False)
        self._retry_btn.setEnabled(False)
        self._swap_btn.setEnabled(False)
        self._body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self._relayout()
        self._position()
        self.show()
        self.raise_()
        # 刻意不调 activateWindow()：抢走焦点会让用户正在看的窗口失焦
        # （光标消失、下拉框收起）。用户点一下卡片就能获得键盘焦点。

    def set_direction(self, src: Lang, tgt: Lang) -> None:
        self._badge.setText(self._direction_text(src, tgt))
        self._swap_btn.setEnabled(True)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def restart(self) -> None:
        """重译 / 换向时原地复位，位置和尺寸保持不变。"""
        self._target_text = ""
        self._stream_buffer.clear()
        self._showing_source = False
        self._from_history = False
        self._body.setText("")
        self._body.setStyleSheet("")
        self._status.setText("")
        self._badge.setText("识别中…")
        self._retry_btn.setEnabled(False)
        self._swap_btn.setEnabled(False)
        self._toggle_btn.setText("原文")
        self._relayout()
        self._reposition_for_growth()

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._stream_buffer.append(chunk)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def finish(self, result: TranslateResult) -> None:
        self._flush()
        self._source_text = result.source_text
        self._target_text = result.text
        self._from_history = result.from_history
        self._showing_source = False

        self._badge.setText(self._direction_text(result.src_lang, result.tgt_lang))
        if result.from_history:
            self._status.setText("来自历史记录")
        elif result.elapsed_ms:
            self._status.setText(f"{result.elapsed_ms} ms")
        else:
            self._status.setText("")

        self._body.setText(self._target_text)
        self._body.setStyleSheet("")
        self._toggle_btn.setEnabled(bool(self._source_text))
        self._retry_btn.setEnabled(True)
        self._relayout()
        self._reposition_for_growth()
        self._arm_auto_hide()

    def fail(self, message: str) -> None:
        self._flush()
        self._target_text = ""
        self._status.setText("失败")
        self._body.setText(message)
        self._body.setStyleSheet(f"color: {self._palette.error};")
        self._toggle_btn.setEnabled(bool(self._source_text))
        self._retry_btn.setEnabled(True)
        self._relayout()
        self._reposition_for_growth()

    def set_source(self, text: str) -> None:
        """识别到原文但还没译文时先展示原文。"""
        self._source_text = text
        self._toggle_btn.setEnabled(bool(text))

    # -------------------------------------------------------------- 内部渲染

    def _flush(self) -> None:
        if not self._stream_buffer:
            return
        self._target_text += "".join(self._stream_buffer)
        self._stream_buffer.clear()
        self._body.setText(self._target_text)
        self._relayout()
        # 只沿原方向生长，不重新规划落点 —— 否则用户手动拖过卡片之后，
        # 每来一段译文就会把它弹回选区旁边
        self._reposition_for_growth()

    def _direction_text(self, src: Lang | None, tgt: Lang | None) -> str:
        if not src or not tgt:
            return "识别中…"
        mark = "历史 · " if self._from_history else ""
        return f"{mark}{LABEL.get(src, src)} → {LABEL.get(tgt, tgt)}"

    def _relayout(self) -> None:
        """高度由内容算出来，不用 adjustSize()，因此不会闪。

        宽度预留 10px 给滚动条。滚动条是覆盖在 viewport 上的，不预留的话
        长文本的最右侧会被切掉几个字。
        """
        inner = self._card_width - PAD * 2 - 10
        self.setFixedWidth(self._card_width)

        self._body.setFixedWidth(inner)
        text_h = self._body.heightForWidth(inner)
        if text_h <= 0:
            text_h = self._body.sizeHint().height()
        self._body.setFixedHeight(max(20, text_h))

        if self._screen_rect.isNull():
            max_body = 360
        else:
            max_body = max(120, int(self._screen_rect.height() * 0.6))
        body_h = max(28, min(text_h, max_body))
        self._scroll.setFixedHeight(body_h)

        total = PAD - 4 + HEADER_H + 6 + body_h + 6 + FOOTER_H + PAD - 6
        self.setFixedHeight(total)

    def _position(self) -> None:
        size = self.size()
        if self._anchor.isNull() or self._screen_rect.isNull():
            return

        gap = MARGIN
        candidates = [
            (QPoint(self._anchor.right() + gap, self._anchor.top()), False),
            (QPoint(self._anchor.left() - size.width() - gap, self._anchor.top()), False),
            (QPoint(self._anchor.left(), self._anchor.bottom() + gap), False),
            (QPoint(self._anchor.left(), self._anchor.top() - size.height() - gap), True),
        ]

        chosen: QPoint | None = None
        above = False
        for pos, is_above in candidates:
            rect = QRect(pos, size)
            if self._screen_rect.contains(rect):
                chosen = pos
                above = is_above
                break

        if chosen is None:
            # 四个方向都放不下 → 夹紧到屏幕内
            x = min(max(self._anchor.left(), self._screen_rect.left() + 4),
                    self._screen_rect.right() - size.width() - 4)
            y = min(max(self._anchor.top(), self._screen_rect.top() + 4),
                    self._screen_rect.bottom() - size.height() - 4)
            chosen = QPoint(x, y)
            above = False

        self._place_above = above
        if above:
            self._fixed_edge = chosen.y() + size.height()
            self.move(chosen.x(), self._fixed_edge - size.height())
        else:
            self._fixed_edge = chosen.y()
            self.move(chosen.x(), chosen.y())

    def _reposition_for_growth(self) -> None:
        """高度变化后保持贴边方向不变，避免卡片上下跳。"""
        if self._anchor.isNull() or self._screen_rect.isNull():
            return
        x = self.x()
        if self._place_above:
            self.move(x, self._fixed_edge - self.height())
        else:
            self.move(x, self._fixed_edge)

    # ---------------------------------------------------------------- 交互

    def toggle_source(self) -> None:
        if not self._source_text:
            return
        self._showing_source = not self._showing_source
        self._body.setText(self._source_text if self._showing_source else self._target_text)
        self._body.setStyleSheet("" if not self._showing_source else f"color: {self._palette.muted};")
        self._toggle_btn.setText("译文" if self._showing_source else "原文")
        self._relayout()
        self._reposition_for_growth()

    def copy_text(self) -> None:
        text = self._source_text if self._showing_source else self._target_text
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self._status.setText("已复制")
        QTimer.singleShot(1500, lambda: self._status.setText(self._status_text_backup()))
        self._arm_auto_hide()

    def _status_text_backup(self) -> str:
        if self._from_history:
            return "来自历史记录"
        return ""

    def dismiss(self) -> None:
        self._hide_timer.stop()
        self.hide()
        self.dismissed.emit()

    def _arm_auto_hide(self) -> None:
        ms = int(self._cfg.auto_hide_ms)
        if ms > 0:
            self._hide_timer.start(ms)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._arm_auto_hide()
        super().leaveEvent(event)

    def _auto_hide(self) -> None:
        # 鼠标还在卡片上就再等等
        if self.geometry().contains(QCursor.pos()):
            self._arm_auto_hide()
            return
        self.dismiss()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.dismiss()
        elif key == Qt.Key.Key_Tab:
            self.toggle_source()
        elif key == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.copy_text()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        # 拖动过就锁定新位置，后续生长不再回到锚点
        if not self._anchor.isNull():
            self._fixed_edge = self.y() if not self._place_above else self.y() + self.height()

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS, RADIUS)

        alpha = float(self._cfg.card_opacity)
        r, g, b = self._palette.card
        painter.fillPath(path, QColor(r, g, b, int(max(0.0, min(1.0, alpha)) * 255)))
        painter.setPen(QPen(QColor(self._palette.border), 1))
        painter.drawPath(path)
        painter.end()

