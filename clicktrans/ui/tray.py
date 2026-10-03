"""托盘常驻。

图标状态直接反映 OCR 模型的生命周期，这是内存约束带来的必要反馈：
- 空心（灰）：模型未加载，下次框选要等 1.5-2 秒
- 半透明：正在加载
- 实心（蓝）：就绪

没有这个反馈，用户会以为程序卡死。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMenu, QMessageBox, QSystemTrayIcon

from .. import APP_NAME, __version__
from ..config import Config
from ..lang import CYCLE, LABEL
from ..utils import log, sysinfo

_log = log.get("tray")

_STATE_COLORS = {
    "ready": QColor(88, 150, 240),
    "loading": QColor(140, 148, 160),
    "idle": QColor(96, 104, 118),
}


def make_icon(state: str = "idle", size: int = 64) -> QIcon:
    color = _STATE_COLORS.get(state, _STATE_COLORS["idle"])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 底板
    painter.setPen(Qt.PenStyle.NoPen)
    background = QColor(color)
    background.setAlpha(48 if state != "ready" else 70)
    painter.setBrush(background)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)

    # 虚线选框 —— 呼应「框选」这个动作
    pen = QPen(color, max(1.5, size * 0.045))
    if state != "ready":
        pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    margin = int(size * 0.19)
    painter.drawRect(margin, margin, size - margin * 2, size - margin * 2)

    # 「译」
    font = QFont()
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "sans-serif"):
        font.setFamily(family)
        if font.exactMatch():
            break
    font.setPixelSize(int(size * 0.46))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "译")
    painter.end()

    return QIcon(pixmap)


class Tray(QObject):
    captureRequested = pyqtSignal()
    historyRequested = pyqtSignal()
    preferredChanged = pyqtSignal(str)
    engineChoiceChanged = pyqtSignal(str)
    idleChanged = pyqtSignal(int)
    autostartChanged = pyqtSignal(bool)
    unloadRequested = pyqtSignal()
    apiKeyRequested = pyqtSignal()
    configRequested = pyqtSignal()
    logRequested = pyqtSignal()
    clearHistoryRequested = pyqtSignal()
    quitRequested = pyqtSignal()

    def __init__(self, config: Config, parent: QObject | None = None):
        super().__init__(parent)
        self._cfg = config
        self._state = "idle"
        self._engine_ready = False

        self._icon = QSystemTrayIcon(make_icon("idle"), self)
        self._icon.setToolTip(f"{APP_NAME} · 启动中")
        self._icon.activated.connect(self._on_activated)
        self._menu = self._build_menu()
        self._icon.setContextMenu(self._menu)

    # -------------------------------------------------------------- 生命周期

    def show(self) -> None:
        self._icon.show()
        self.refresh()

    def hide(self) -> None:
        self._icon.hide()

    def set_engine_state(self, ready: bool, loading: bool = False) -> None:
        self._engine_ready = ready
        self._state = "ready" if ready else ("loading" if loading else "idle")
        self._icon.setIcon(make_icon(self._state))
        self.refresh()

    def notify(self, title: str, message: str, seconds: int = 4) -> None:
        try:
            self._icon.showMessage(title, message, make_icon(self._state), seconds * 1000)
        except Exception:
            pass

    def refresh(self) -> None:
        mem = sysinfo.human_mb(sysinfo.working_set_bytes())
        state_text = {
            "ready": "模型就绪",
            "loading": "模型加载中",
            "idle": "模型未加载",
        }.get(self._state, "未知")
        self._icon.setToolTip(f"{APP_NAME} {__version__}　{state_text}　内存 {mem}")
        self._mem_action.setText(f"内存占用：{mem}　·　{state_text}")
        self._unload_action.setEnabled(self._engine_ready)

    def update_config(self, config: Config) -> None:
        self._cfg = config
        self._sync_checks()

    # ------------------------------------------------------------------ 菜单

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        capture = QAction("开始框选", menu)
        capture.setShortcut(self._cfg.general.hotkey)
        capture.triggered.connect(self.captureRequested.emit)
        menu.addAction(capture)

        history = QAction("历史记录…", menu)
        history.triggered.connect(self.historyRequested.emit)
        menu.addAction(history)

        menu.addSeparator()

        # --- 偏好语言
        lang_menu = menu.addMenu("偏好语言")
        self._lang_group = QActionGroup(lang_menu)
        self._lang_group.setExclusive(True)
        self._lang_actions: dict[str, QAction] = {}
        for lang in CYCLE:
            action = QAction(LABEL[lang], lang_menu)
            action.setCheckable(True)
            action.setData(lang.value)
            action.triggered.connect(lambda _=False, v=lang.value: self.preferredChanged.emit(v))
            self._lang_group.addAction(action)
            lang_menu.addAction(action)
            self._lang_actions[lang.value] = action

        # --- 模型与内存
        engine_menu = menu.addMenu("OCR 模型")
        self._engine_group = QActionGroup(engine_menu)
        self._engine_group.setExclusive(True)
        self._engine_actions: dict[str, QAction] = {}
        for value, label in (("mobile", "mobile（省内存，推荐）"), ("server", "server（更准，多占 200MB+）")):
            action = QAction(label, engine_menu)
            action.setCheckable(True)
            action.setData(value)
            action.triggered.connect(lambda _=False, v=value: self.engineChoiceChanged.emit(v))
            self._engine_group.addAction(action)
            engine_menu.addAction(action)
            self._engine_actions[value] = action

        idle_menu = menu.addMenu("空闲卸载模型")
        self._idle_group = QActionGroup(idle_menu)
        self._idle_group.setExclusive(True)
        self._idle_actions: dict[int, QAction] = {}
        for value, label in ((0, "不卸载"), (10, "10 分钟"), (30, "30 分钟（推荐）"), (60, "60 分钟")):
            action = QAction(label, idle_menu)
            action.setCheckable(True)
            action.setData(value)
            action.triggered.connect(lambda _=False, v=value: self.idleChanged.emit(v))
            self._idle_group.addAction(action)
            idle_menu.addAction(action)
            self._idle_actions[value] = action

        self._mem_action = QAction("内存占用：—", menu)
        self._mem_action.setEnabled(False)
        menu.addAction(self._mem_action)

        self._unload_action = QAction("立即卸载模型", menu)
        self._unload_action.triggered.connect(self.unloadRequested.emit)
        menu.addAction(self._unload_action)

        menu.addSeparator()

        self._autostart_action = QAction("开机自启", menu)
        self._autostart_action.setCheckable(True)
        self._autostart_action.toggled.connect(self.autostartChanged.emit)
        menu.addAction(self._autostart_action)

        api_key = QAction("设置翻译 API Key…", menu)
        api_key.triggered.connect(self.apiKeyRequested.emit)
        menu.addAction(api_key)

        clear_history = QAction("清空历史记录…", menu)
        clear_history.triggered.connect(self._confirm_clear)
        menu.addAction(clear_history)

        menu.addSeparator()

        open_config = QAction("打开配置文件", menu)
        open_config.triggered.connect(self.configRequested.emit)
        menu.addAction(open_config)

        open_log = QAction("查看日志", menu)
        open_log.triggered.connect(self.logRequested.emit)
        menu.addAction(open_log)

        about = QAction("关于", menu)
        about.triggered.connect(self._show_about)
        menu.addAction(about)

        menu.addSeparator()

        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self.quitRequested.emit)
        menu.addAction(quit_action)

        self._sync_checks()
        return menu

    def _sync_checks(self) -> None:
        preferred = self._cfg.general.preferred.value
        for value, action in self._lang_actions.items():
            action.setChecked(value == preferred)

        model = self._cfg.ocr.model
        for value, action in self._engine_actions.items():
            action.setChecked(value == model)

        idle = int(self._cfg.ocr.unload_after_idle_min)
        for value, action in self._idle_actions.items():
            action.setChecked(value == idle)

        # 以注册表的实际状态为准，不是以配置为准 —— 用户可能在别处关掉了
        autostart = self._cfg.general.launch_at_startup
        try:
            from ..utils import autostart as autostart_module

            autostart = autostart_module.is_enabled()
        except Exception:
            pass
        self._autostart_action.blockSignals(True)
        self._autostart_action.setChecked(autostart)
        self._autostart_action.blockSignals(False)

    # ---------------------------------------------------------------- 交互

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.captureRequested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.historyRequested.emit()

    def _confirm_clear(self) -> None:
        box = QMessageBox()
        box.setWindowTitle("清空历史记录")
        box.setText("确定要清空历史记录吗？置顶的条目会被保留。")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        # 托盘程序没有主窗口，弹窗必须显式置顶，否则会藏在别的窗口后面
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.clearHistoryRequested.emit()

    def _show_about(self) -> None:
        mem = sysinfo.human_mb(sysinfo.working_set_bytes())
        state = "已加载" if self._engine_ready else "未加载"
        QMessageBox.information(
            None,
            f"关于 {APP_NAME}",
            f"{APP_NAME} {__version__}\n\n"
            "鼠标框选屏幕任意区域，实时翻译中文 / 日语 / 英语。\n"
            "截图与识别全程在本机完成，只有识别出的文字会发送到翻译接口。\n\n"
            f"当前内存：{mem}\n"
            f"OCR 模型：{state}\n"
            f"历史记录：{self._cfg.history.resolved_path()}",
        )

    def ask_api_key(self, current_hint: str) -> str | None:
        text, ok = QInputDialog.getText(
            None,
            "设置翻译 API Key",
            "粘贴你的 API Key（留空则清除）：",
            QLineEdit.EchoMode.Password,
            current_hint,
        )
        return text if ok else None
