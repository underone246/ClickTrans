"""开发用：不启动 GUI，直接验证 OCR 链路。

    python tools/check_ocr.py

会生成一张中/日/英混排的测试图，跑一遍识别，并打印三个时间点的内存：
模型加载前、加载后、卸载后。

关于第三个数字的预期：实测卸载只能收回 8-13 MB —— 模型本身就小，
剩下的是 opencv / onnxruntime 的运行时占用，它们一旦导入就不会走了。
**这不是 bug**，别去关 enable_cpu_mem_arena（实测那样只会让推理慢一倍，
并不会多释放内存，详见 tools/tune_ocr.py）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from clicktrans.ocr.rapidocr_engine import RapidOcrEngine
from clicktrans.utils import sysinfo

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑，中日文都覆盖
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

LINES = [
    "This is an English sentence for OCR testing.",
    "这是一段中文测试文字，用于验证识别效果。",
    "これは日本語のテスト文です。",
    "def process(data):",
]


def make_test_image() -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 900, 320
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    font = None
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, 30)
                print(f"使用字体：{candidate}")
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
        print("警告：没找到中文字体，回退到默认字体")

    y = 30
    for line in LINES:
        draw.text((30, y), line, fill="black", font=font)
        y += 65

    return np.asarray(image)[:, :, :3].copy()


def main() -> int:
    engine = RapidOcrEngine(min_confidence=0.5)

    baseline = sysinfo.working_set_bytes()
    print(f"\n[内存] 加载前         : {sysinfo.human_mb(baseline)}")

    started = time.perf_counter()
    engine.warmup()
    load_ms = (time.perf_counter() - started) * 1000
    loaded = sysinfo.working_set_bytes()
    print(f"[内存] 加载后         : {sysinfo.human_mb(loaded)}　（加载耗时 {load_ms:.0f} ms）")

    image = make_test_image()
    result = engine.recognize(image)
    print(f"\n[识别] 耗时 {result.elapsed_ms} ms，{len(result.blocks)} 块，"
          f"平均置信度 {result.mean_confidence:.2f}")
    print(f"[识别] 标记为代码的行号：{result.code_lines}")
    print("\n----- 重组后的文本 -----")
    print(result.text)
    print("------------------------\n")

    expected_markers = ["English", "中文", "日本語", "def"]
    lowered = result.text.lower()
    hits = [m for m in expected_markers if m.lower() in lowered]
    print(f"[校验] 命中关键词 {len(hits)}/{len(expected_markers)}：{hits}")

    # 第二次调用应该更快（模型已就绪，不含加载）
    started = time.perf_counter()
    engine.recognize(image)
    print(f"[识别] 第二次耗时 {(time.perf_counter() - started) * 1000:.0f} ms")

    engine.unload()
    time.sleep(0.5)
    after = sysinfo.working_set_bytes()
    print(f"\n[内存] 卸载后         : {sysinfo.human_mb(after)}")
    released = (loaded - after) / 1024 / 1024
    print(f"[内存] 释放了         : {released:.0f} MB")
    print(f"[内存] 相对加载前     : +{(after - baseline) / 1048576:.0f} MB"
          "（opencv / onnxruntime 的运行时占用，导入后就不会走）")
    if released < 4:
        print("  ⚠ 释放得偏少。注意：不要为此去关 enable_cpu_mem_arena，"
              "实测那只会让推理慢一倍而不会多释放内存")

    return 0 if len(hits) >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
