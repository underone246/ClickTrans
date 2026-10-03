"""历史记录面板 —— 离线时唯一有意义的入口。

数据源就是 history.json，全量在内存里，所以搜索是纯字符串匹配，没有延迟。
顶部显示用量，让用户对「500 条滚动淘汰」这件事有预期。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..history import HistoryItem, HistoryStore, export_markdown
from ..lang import CYCLE, LABEL

_DIRECTION_CHOICES = [("all", "全部方向")] + [
    (f"{a.value}-{b.value}", f"{LABEL[a]} → {LABEL[b]}")
    for a in CYCLE
    for b in CYCLE
    if a != b
]


class HistoryPanel(QWidget):
    def __init__(self, history: HistoryStore, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self._history = history
        self._cfg = config
        self._items: list[HistoryItem] = []

        self.setWindowTitle("ClickTrans · 历史记录")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(820, 560)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索原文或译文…")
        self._search.textChanged.connect(self._apply_filter)
        top.addWidget(self._search, 1)

        self._direction = QComboBox()
        for value, label in _DIRECTION_CHOICES:
            self._direction.addItem(label, value)
        self._direction.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self._direction)

        self._count = QLabel("")
        top.addWidget(self._count)
        root.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._show_detail)
        self._list.itemDoubleClicked.connect(self._copy_target)
        splitter.addWidget(self._list)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 6, 0, 0)
        detail_layout.setSpacing(4)

        detail_layout.addWidget(QLabel("原文"))
        self._source = QPlainTextEdit()
        self._source.setReadOnly(True)
        detail_layout.addWidget(self._source, 1)

        detail_layout.addWidget(QLabel("译文"))
        self._target = QPlainTextEdit()
        self._target.setReadOnly(True)
        detail_layout.addWidget(self._target, 1)
        splitter.addWidget(detail)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for label, slot in (
            ("复制译文", lambda: self._copy(True)),
            ("复制原文", lambda: self._copy(False)),
            ("置顶/取消", self._toggle_pin),
            ("重新框译", self._rebox),
            ("删除", self._delete),
            ("导出 Markdown", self._export),
            ("清空", self._clear),
        ):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            if label == "重新框译":
                button.setToolTip("把这条的原文放回剪贴板，你可以框选别处继续")
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)

    # -------------------------------------------------------------- 数据刷新

    def refresh(self) -> None:
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self._search.text()
        direction = self._direction.currentData() or "all"
        self._items = self._history.search(query, direction)

        self._list.clear()
        for item in self._items:
            label = f"{_short_time(item.ts)}　{item.direction}"
            if item.pinned:
                label += "　★"
            if item.pending:
                label += "　（未翻译）"
            prefix = "  "
            summary = item.preview(70)
            entry = QListWidgetItem(f"{prefix}{label}\n{prefix}{summary}")
            entry.setData(Qt.ItemDataRole.UserRole, item.id)
            self._list.addItem(entry)

        total, normal, limit = self._history.stats()
        self._count.setText(f"共 {total} 条　·　普通 {normal}/{limit}　·　置顶 {total - normal}")

        if self._items:
            self._list.setCurrentRow(0)
        else:
            self._source.setPlainText("")
            self._target.setPlainText("")

    def _selected(self) -> HistoryItem | None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def _show_detail(self) -> None:
        item = self._selected()
        if item is None:
            return
        self._source.setPlainText(item.source_text)
        self._target.setPlainText(item.target_text or "（未翻译，联网后可重译）")

    # ---------------------------------------------------------------- 操作

    def _copy(self, target: bool) -> None:
        item = self._selected()
        if item is None:
            return
        text = item.target_text if target else item.source_text
        if not text:
            return
        QGuiApplication.clipboard().setText(text)

    def _copy_target(self) -> None:
        self._copy(True)

    def _toggle_pin(self) -> None:
        item = self._selected()
        if item is None:
            return
        self._history.set_pinned(item.id, not item.pinned)
        self.refresh()

    def _delete(self) -> None:
        item = self._selected()
        if item is None:
            return
        self._history.remove(item.id)
        self.refresh()

    def _rebox(self) -> None:
        """把原文放进剪贴板。框选是全局热键的事，这里只做搬运。"""
        item = self._selected()
        if item is None:
            return
        QGuiApplication.clipboard().setText(item.source_text)

    def _export(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史记录", "clicktrans-history.md", "Markdown (*.md)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(export_markdown(self._items))
        except Exception as exc:
            self._count.setText(f"导出失败：{exc}")

    def _clear(self) -> None:
        self._history.clear(keep_pinned=True)
        self.refresh()

    # ---------------------------------------------------------------- 生命周期

    def update_config(self, config: Config) -> None:
        self._cfg = config

    def showEvent(self, event) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.history_flush()
        super().closeEvent(event)

    def history_flush(self) -> None:
        try:
            self._history.flush()
        except Exception:
            pass


def _short_time(ts: str) -> str:
    text = (ts or "").replace("T", " ")
    return text[:16] if len(text) >= 16 else text
