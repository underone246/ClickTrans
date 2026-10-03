"""应用装配：把采集、识别、翻译、展示、托盘、热键接在一起。

所有跨线程通信都走 Qt 信号槽，没有任何共享可变状态。
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QObject, QRect, QTimer
from PyQt6.QtWidgets import QApplication

from . import APP_NAME
from .capture.geometry import ScreenShot
from .capture.overlay import CaptureSession
from .config import (
    Config,
    config_path,
    load_api_key,
    log_dir,
    save_api_key,
    save as save_config,
    secret_path,
)
from .history import HistoryStore
from .hotkey import HotkeyManager
from .lang import Lang, parse_lang
from .pipeline import Pipeline
from .ui.card import ResultCard
from .ui.history_panel import HistoryPanel
from .ui.tray import Tray
from .utils import log
from .utils import autostart

_log = log.get("app")


class ClickTransApp(QObject):
    def __init__(self, config: Config, qt_app: QApplication):
        super().__init__()
        self._cfg = config
        self._app = qt_app

        self._history = HistoryStore(
            config.history.resolved_path(),
            limit=config.history.limit,
            dedupe=config.history.dedupe,
        )
        self._pipeline = Pipeline(config, self._history)
        self._card = ResultCard(config)
        self._tray = Tray(config)
        self._hotkey = HotkeyManager()

        self._panel: HistoryPanel | None = None
        self._session = None
        self._session_active = False
        self._last_shot: ScreenShot | None = None
        self._last_rect: QRect | None = None
        self._last_direction: tuple[Lang, Lang] | None = None

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._on_idle)

        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(15_000)
        self._memory_timer.timeout.connect(self._tray.refresh)

    # ---------------------------------------------------------------- 启动

    def start(self) -> bool:
        self._wire()
        self._pipeline.start()

        ok, message = self._hotkey.register(self._cfg.general.hotkey, self._app)
        if not ok:
            _log.error("热键注册失败：%s", message)

        self._tray.show()
        self._restart_idle_timer()
        self._memory_timer.start()

        ok_auto, msg_auto = autostart.apply(self._cfg.general.launch_at_startup)
        if not ok_auto:
            _log.warning("同步开机自启失败: %s", msg_auto)

        if not ok:
            self._tray.notify("热键注册失败", message, 10)
        if not load_api_key():
            self._tray.notify(
                "还没配置翻译 API Key",
                "右键托盘图标 → 设置翻译 API Key…",
                8,
            )
        return True

    def _wire(self) -> None:
        self._tray.captureRequested.connect(self.start_capture)
        self._tray.historyRequested.connect(self.show_history)
        self._tray.preferredChanged.connect(self.set_preferred)
        self._tray.engineChoiceChanged.connect(self.set_engine_model)
        self._tray.idleChanged.connect(self.set_idle_minutes)
        self._tray.autostartChanged.connect(self.set_autostart)
        self._tray.unloadRequested.connect(self._pipeline.request_unload)
        self._tray.apiKeyRequested.connect(self.ask_api_key)
        self._tray.configRequested.connect(lambda: _open_path(config_path()))
        self._tray.logRequested.connect(lambda: _open_path(log_dir()))
        self._tray.clearHistoryRequested.connect(self._clear_history)
        self._tray.quitRequested.connect(self.shutdown)

        self._pipeline.engineStateChanged.connect(self._on_engine_state)
        self._pipeline.statusChanged.connect(self._card.set_status)
        self._pipeline.ocrReady.connect(self._card.set_source)
        self._pipeline.directionReady.connect(self._on_direction)
        self._pipeline.partial.connect(self._card.append)
        self._pipeline.finished.connect(self._on_finished)
        self._pipeline.failed.connect(self._card.fail)
        self._pipeline.historyChanged.connect(self._on_history_changed)

        self._card.retryRequested.connect(self.retry)
        self._card.swapRequested.connect(self.swap)
        self._card.dismissed.connect(self._pipeline.cancel)

    # ---------------------------------------------------------------- 框选

    def start_capture(self) -> None:
        if self._session_active:
            _log.info("已有框选进行中，忽略这次触发")
            return

        self._card.dismiss()
        self._session_active = True
        self._session = CaptureSession(self._cfg.general.preferred)
        try:
            started = self._session.start(self._on_selected, self._on_cancelled)
        except Exception as exc:
            _log.exception("打开框选失败")
            self._session_active = False
            self._tray.notify("框选失败", str(exc), 6)
            return
        if not started:
            self._session_active = False

    def _on_selected(self, shot: ScreenShot, rect: QRect) -> None:
        self._session_active = False
        try:
            self._last_shot = shot
            self._last_rect = QRect(rect)
            self._last_direction = None

            # 锚点换算到虚拟桌面坐标，卡片据此贴着选区摆
            anchor = QRect(
                shot.logical_rect.x() + rect.x(),
                shot.logical_rect.y() + rect.y(),
                rect.width(),
                rect.height(),
            )
            self._card.begin(None, None, anchor, QRect(shot.logical_rect))
            self._pipeline.request(shot, rect, self._cfg.general.preferred)
            self._restart_idle_timer()
        except Exception as exc:
            _log.exception("处理选区失败")
            self._card.fail(f"处理失败：{exc}")

    def _on_cancelled(self) -> None:
        self._session_active = False

    # ---------------------------------------------------------- pipeline 回调

    def _on_engine_state(self, ready: bool) -> None:
        self._tray.set_engine_state(ready)

    def _on_direction(self, src: Lang, tgt: Lang) -> None:
        self._last_direction = (src, tgt)
        self._card.set_direction(src, tgt)

    def _on_finished(self, result) -> None:
        self._card.finish(result)
        self._on_history_changed()

    def _on_history_changed(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.refresh()

    # ------------------------------------------------------------ 卡片操作

    def retry(self) -> None:
        if self._last_shot is None or self._last_rect is None:
            return
        self._card.restart()
        self._last_direction = None
        self._pipeline.request(
            self._last_shot, self._last_rect, self._cfg.general.preferred
        )

    def swap(self) -> None:
        if self._last_shot is None or self._last_rect is None or not self._last_direction:
            return
        src, tgt = self._last_direction
        self._card.restart()
        self._last_direction = (tgt, src)
        self._pipeline.request(
            self._last_shot,
            self._last_rect,
            self._cfg.general.preferred,
            force_direction=(tgt, src),
        )

    # ---------------------------------------------------------------- 设置项

    def set_preferred(self, value: str) -> None:
        lang = parse_lang(value, Lang.ZH)
        self._cfg.general.preferred_lang = lang.value
        self._save()
        self._pipeline.request_reload(self._cfg)
        self._tray.update_config(self._cfg)

    def set_engine_model(self, value: str) -> None:
        self._cfg.ocr.model = value
        self._save()
        self._pipeline.request_reload(self._cfg)
        self._tray.update_config(self._cfg)
        note = ""
        if value == "server":
            note = "（需要把 server 模型放到 ~/.clicktrans/models/server/，找不到会自动回退 mobile）"
        self._tray.notify("OCR 模型已切换", f"下次框选按 {value} 加载{note}", 6)

    def set_idle_minutes(self, value: int) -> None:
        self._cfg.ocr.unload_after_idle_min = int(value)
        self._save()
        self._tray.update_config(self._cfg)
        self._restart_idle_timer()

    def _restart_idle_timer(self) -> None:
        minutes = int(self._cfg.ocr.unload_after_idle_min)
        if minutes <= 0:
            self._idle_timer.stop()
            return
        self._idle_timer.start(minutes * 60_000)

    def _on_idle(self) -> None:
        if self._pipeline.engine_ready:
            _log.info("空闲超时，释放 OCR 模型")
            self._pipeline.request_unload()
        self._restart_idle_timer()

    def set_autostart(self, enabled: bool) -> None:
        ok, message = autostart.apply(enabled)
        if ok:
            self._cfg.general.launch_at_startup = enabled
            self._save()
        else:
            self._tray.notify("开机自启设置失败", message, 6)
        self._tray.update_config(self._cfg)

    def ask_api_key(self) -> None:
        current = load_api_key()
        hint = f"{current[:4]}…{current[-4:]}" if len(current) > 10 else ""
        text = self._tray.ask_api_key(hint)
        if text is None:
            return
        text = text.strip()

        if not text:
            try:
                if secret_path().exists():
                    secret_path().unlink()
            except Exception:
                pass
            self._pipeline.request_reload(self._cfg)
            self._tray.notify("已清除 API Key", "", 4)
            return

        if "…" in text:  # 用户没改动就直接确定，别把掩码存进去
            return

        method = save_api_key(text)
        self._pipeline.request_reload(self._cfg)
        if method == "dpapi":
            self._tray.notify("API Key 已保存", "已用 DPAPI 按当前用户加密", 5)
        else:
            self._tray.notify(
                "API Key 已保存（未加密）",
                "未能调用 Windows DPAPI，已降级为 base64 存储。",
                8,
            )

    def _clear_history(self) -> None:
        removed = self._history.clear(keep_pinned=True)
        self._on_history_changed()
        self._tray.notify("历史记录已清空", f"删除了 {removed} 条，置顶的保留了", 4)

    def _save(self) -> None:
        try:
            save_config(self._cfg)
        except Exception as exc:
            _log.warning("保存配置失败: %s", exc)

    # ------------------------------------------------------------ 历史面板

    def show_history(self) -> None:
        if self._panel is None:
            self._panel = HistoryPanel(self._history, self._cfg)
        self._panel.update_config(self._cfg)
        self._panel.refresh()
        self._panel.show()
        self._panel.raise_()
        self._panel.activateWindow()

    # ---------------------------------------------------------------- 退出

    def shutdown(self) -> None:
        _log.info("正在退出…")
        try:
            self._hotkey.unregister()
        except Exception:
            pass
        try:
            self._pipeline.stop()
            self._pipeline.wait(3000)
        except Exception:
            pass
        try:
            self._history.close()
        except Exception:
            pass
        try:
            self._card.hide()
            self._tray.hide()
        except Exception:
            pass
        self._app.quit()


def _open_path(path) -> None:
    try:
        path = str(path)
        if not os.path.exists(path):
            # 日志/配置文件可能还没生成，先建空的，避免调用方报错
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "a", encoding="utf-8").close()
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception as exc:
        log.get("app").warning("打开 %s 失败: %s", path, exc)
