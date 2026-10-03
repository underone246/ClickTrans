"""入口：单实例检查 → 装配 → 托盘常驻。"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, __version__
from . import config as config_module
from .utils import log
from .utils.single_instance import SingleInstance


def main() -> int:
    cfg = config_module.ensure_default_config()
    log.setup(cfg.general.log_level, config_module.log_dir())
    logger = log.get("main")
    logger.info("%s %s 启动", APP_NAME, __version__)

    # 必须在 QApplication 构造之前设置，否则不生效。
    # PassThrough 让 Qt 如实报告屏幕缩放（1.25 / 1.5 这些），
    # 而不是四舍五入成 1 或 2 —— 坐标换算依赖这个精度。
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # 托盘程序：关掉卡片和面板不能让进程退出
    app.setQuitOnLastWindowClosed(False)

    guard = SingleInstance()
    if cfg.general.single_instance and not guard.acquire():
        logger.info("检测到已有实例在运行，退出")
        QMessageBox.information(
            None,
            APP_NAME,
            f"{APP_NAME} 已经在运行了。\n\n请查看系统托盘图标（可能被折叠在「^」里）。",
        )
        return 0

    from .app import ClickTransApp

    controller = ClickTransApp(cfg, app)
    try:
        if not controller.start():
            return 1
        return app.exec()
    except Exception:
        logger.exception("未捕获的异常，进程退出")
        return 1
    finally:
        try:
            controller.shutdown()
        except Exception:
            pass
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
