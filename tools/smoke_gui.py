"""开发用：无头集成冒烟测试。

不启动真实 GUI（用 Qt 的 offscreen 平台），但把真正的对象都造出来跑一遍：
结果卡片、托盘菜单、Pipeline 工作线程、一次完整的「裁剪 → OCR → 语言判定
→ 翻译」流程。

翻译会失败（没配 API Key），这恰好把错误路径也覆盖了。

    python tools/smoke_gui.py

**测试图为什么用 PIL 画、而不是 QPainter：**
offscreen 平台插件没有可用的字体数据库，`QFont("Microsoft YaHei")`
会被判为缺失字体，整行文字渲染成「豆腐块」（空心方框）。那样 OCR 自然
一块都识别不出来，测试会误报成管线坏了。
真实运行走 windows 平台插件，字体正常，所以这只是**测试脚手架**的坑。
这里统一用 PIL + 字体文件渲染，再经 ndarray_to_qimage 转回 QImage，
既能绕开字体栈，又仍然覆盖「QImage → ndarray → OCR」这条真实链路。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SANDBOX = tempfile.mkdtemp(prefix="clicktrans-smoke-")
os.environ["CLICKTRANS_HOME"] = _SANDBOX
# 保证稳定走「没配 API Key」这条失败分支，而不是真的打网络请求
os.environ.pop("CLICKTRANS_API_KEY", None)

from PyQt6.QtCore import QRect, QSize, QTimer  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from clicktrans.capture.geometry import ScreenShot, physical_rect  # noqa: E402
from clicktrans.capture.grabber import ndarray_to_qimage  # noqa: E402
from clicktrans.config import Config  # noqa: E402
from clicktrans.history import HistoryStore  # noqa: E402
from clicktrans.lang import Lang  # noqa: E402
from clicktrans.pipeline import Pipeline  # noqa: E402
from clicktrans.translate.base import TranslateResult  # noqa: E402
from clicktrans.ui.card import ResultCard  # noqa: E402
from clicktrans.ui.history_panel import HistoryPanel  # noqa: E402
from clicktrans.ui.tray import Tray, make_icon  # noqa: E402
from clicktrans.utils import log  # noqa: E402

WIDTH, HEIGHT = 900, 320

failures: list[str] = []
state: dict[str, object] = {}


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  [ok]   {message}")
    else:
        print(f"  [FAIL] {message}")
        failures.append(message)


LINES = [
    "This is an English sentence for OCR testing.",
    "这是一段中文测试文字，用于验证识别效果。",
    "def process(data):",
]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
]


def make_screen_image():
    """用 PIL 渲染测试图，返回 QImage。见模块 docstring 解释。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)

    font = None
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, 30)
                break
            except Exception:
                continue
    if font is None:
        print("  ! 没找到中文字体，OCR 断言可能不准")

    y = 34
    for line in LINES:
        draw.text((30, y), line, fill="black", font=font)
        y += 66

    return ndarray_to_qimage(np.asarray(image)[:, :, :3])


def build_shot(screen) -> ScreenShot:
    return ScreenShot(
        screen=screen,
        logical_rect=QRect(0, 0, WIDTH, HEIGHT),
        dpr=1.0,
        image=make_screen_image(),
    )


def main() -> int:
    log.setup("INFO", Path(_SANDBOX) / "logs")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    cfg = Config()
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        print("没有可用屏幕，offscreen 平台异常，跳过集成流程")
        return 0

    print("\n=== 1. 坐标换算 ===")
    rect = physical_rect(QRect(10, 20, 100, 50), 1.5, QSize(2880, 1620))
    check((rect.x(), rect.y(), rect.width(), rect.height()) == (15, 30, 150, 75),
          "1.5x DPI 换算正确")

    print("\n=== 2. 结果卡片 ===")
    card = ResultCard(cfg)
    anchor = QRect(100, 100, 300, 60)
    card.begin(None, None, anchor, QRect(0, 0, 1920, 1080))
    check(card.isVisible() or card.height() > 0, "begin() 不抛异常并能完成布局")
    height_start = card.height()

    card.set_direction(Lang.EN, Lang.ZH)
    card.append("你好")
    card.append("，世界")
    card.append("。这是一段用来测试自动换行的较长译文，需要占用好几行的高度。")
    card._flush()
    check(card.height() >= height_start, "流式追加后卡片高度只增不减")
    check(card._target_text == "你好，世界。这是一段用来测试自动换行的较长译文，需要占用好几行的高度。",
          "增量文本拼接正确")

    card.finish(TranslateResult(
        text=card._target_text,
        source_text="Hello world",
        src_lang=Lang.EN,
        tgt_lang=Lang.ZH,
        engine="smoke",
        elapsed_ms=123,
    ))
    check("EN" in card._badge.text() or "中文" in card._badge.text(), "方向徽标已显示")

    card.toggle_source()
    check(card._showing_source, "「原文」切换生效")
    card.toggle_source()
    card.restart()
    check(card._target_text == "", "restart() 复位了内容")

    card.fail("这是模拟的错误信息")
    check("模拟的错误信息" in card._body.text(), "fail() 展示了错误信息")
    card.hide()

    print("\n=== 3. 托盘 ===")
    tray = Tray(cfg)
    check(not make_icon("ready").isNull(), "托盘图标可绘制")
    tray.refresh()
    check("内存" in tray._mem_action.text(), f"内存读数可用：{tray._mem_action.text()}")
    tray.set_engine_state(True)
    tray.set_engine_state(False)
    check(True, "托盘状态切换不抛异常")

    print("\n=== 4. 历史面板 ===")
    history = HistoryStore(Path(_SANDBOX) / "history.json", limit=10)
    history.add("hello", "你好", "en", "zh")
    history.add("world", "世界", "en", "zh")
    panel = HistoryPanel(history, cfg)
    check(panel._list.count() == 2, "历史面板列出了 2 条")
    total, normal, limit = history.stats()
    check(total == 2 and normal == 2 and limit == 10, f"统计口径正确：{total}/{normal}/{limit}")
    panel.hide()
    panel.close()

    print("\n=== 5. Pipeline 端到端（OCR 真实运行）===")
    pipeline = Pipeline(cfg, history)
    shot = build_shot(screen)

    stages: list[str] = []
    pipeline.engineStateChanged.connect(
        lambda ready: state.__setitem__("engine_ready", ready)
    )
    pipeline.statusChanged.connect(stages.append)
    pipeline.ocrReady.connect(lambda text: state.__setitem__("ocr", text))
    pipeline.directionReady.connect(
        lambda src, tgt: state.__setitem__("direction", (src, tgt))
    )
    pipeline.finished.connect(lambda result: state.__setitem__("finished", result))
    pipeline.failed.connect(lambda message: state.__setitem__("failed", message))

    pipeline.start()

    QTimer.singleShot(90_000, app.quit)

    def kick():
        pipeline.request(shot, QRect(0, 0, WIDTH, HEIGHT), Lang.ZH)
        QTimer.singleShot(45_000, app.quit)

    QTimer.singleShot(500, kick)
    app.exec()

    print(f"  阶段提示：{stages}")

    ocr_text = str(state.get("ocr", ""))
    check(bool(ocr_text), "OCR 输出了文本")
    if not ocr_text:
        print("  ! OCR 没出文本。先单独跑 tools/check_ocr.py 确认引擎本身正常；")
        print("    如果那边正常，检查测试图是否被渲染成了豆腐块（见模块 docstring）。")
    plain = ocr_text.replace(" ", "").lower()
    for marker in ("english", "中文", "def"):
        check(marker in plain, f"识别结果包含「{marker}」")
    check(bool(ocr_text) and ocr_text.splitlines()[0].startswith("This"),
          "首行文字顺序正确（版面重组没串行）")
    check("direction" in state, f"语言方向已判定：{state.get('direction')}")

    direction = state.get("direction")
    if direction:
        src, tgt = direction  # type: ignore[misc]
        check(src == Lang.EN, f"英文被正确识别为源语言（实际 {src}）")
        check(tgt == Lang.ZH, f"目标语言跟随偏好（实际 {tgt}）")

    # 没配 API Key，应该走失败分支并给出可读提示，而不是崩掉
    failed = state.get("failed")
    finished = state.get("finished")
    check(bool(failed) or bool(finished),
          f"流程有终态：failed={str(failed)[:50]!r} finished={'有' if finished else '无'}")

    pipeline.stop()
    pipeline.wait(5000)
    history.close()

    print("\n=== 6. 内存 ===")
    from clicktrans.utils import sysinfo

    print(f"  当前工作集：{sysinfo.human_mb(sysinfo.working_set_bytes())}")

    print()
    if failures:
        print(f"✗ {len(failures)} 项失败：")
        for item in failures:
            print(f"    - {item}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
