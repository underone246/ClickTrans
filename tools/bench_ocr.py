"""开发用：OCR 性能对照实验。

每个配置必须在**独立子进程**里跑 —— onnxruntime 的 SessionOptions 补丁是
进程级全局的，同一个进程里没法干净地切换，测出来的数会互相污染。

用法（由 tools/bench_all.sh 驱动，也可以手动跑）：

    set BENCH_ARENA=on   & set BENCH_CV_THREADS=1 & python tools/bench_ocr.py
    set BENCH_ARENA=off  & set BENCH_CV_THREADS=0 & python tools/bench_ocr.py

关心的两个问题：
1. 关掉 onnxruntime 内存池（为了能真正卸载模型）到底要付多少性能代价
2. OpenCV 的线程争抢是不是耗时波动的主因
"""

from __future__ import annotations

import gc
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from clicktrans.utils import sysinfo

ARENA = os.environ.get("BENCH_ARENA", "on").lower() == "on"
CV_THREADS = int(os.environ.get("BENCH_CV_THREADS", "1"))
ROUNDS = int(os.environ.get("BENCH_ROUNDS", "3"))

CASES = [
    ("小选区 600x120 / 3 行", 600, 120, 3),
    ("中选区 1000x360 / 5 行", 1000, 360, 5),
    ("大选区 1600x800 / 14 行", 1600, 800, 14),
]


def make_image(width: int, height: int, lines: int) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = 10
    for _ in range(lines):
        draw.text((10, y), "The quick brown fox jumps over the lazy dog 0123", fill="black", font=font)
        y += 34
    return np.asarray(image)[:, :, :3].copy()


def main() -> int:
    if not ARENA:
        import onnxruntime as ort
        import onnxruntime.capi.onnxruntime_inference_collection as collection

        ort.InferenceSession.__init__ = collection.InferenceSession.__init__

    if CV_THREADS == 0:
        try:
            import cv2

            cv2.setNumThreads(1)
        except Exception as exc:
            print(f"cv2.setNumThreads 失败: {exc}")

    from clicktrans.ocr import rapidocr_engine as engine_module

    engine_module._ort_patched = False
    if ARENA:
        engine_module.patch_onnxruntime()

    engine = engine_module.RapidOcrEngine(min_confidence=0.5, upscale=1.0)

    baseline = sysinfo.working_set_bytes()
    started = time.perf_counter()
    engine.warmup()
    load_ms = (time.perf_counter() - started) * 1000
    loaded = sysinfo.working_set_bytes()

    print(f"arena={'ON ' if ARENA else 'OFF'}  cv_threads={CV_THREADS}  "
          f"| 加载 {load_ms:.0f} ms | 内存 {sysinfo.human_mb(baseline)} -> {sysinfo.human_mb(loaded)}")

    for label, width, height, lines in CASES:
        image = make_image(width, height, lines)
        engine.recognize(image)  # 预热，不计入

        walls: list[float] = []
        cpus: list[float] = []
        for _ in range(ROUNDS):
            w0, c0 = time.perf_counter(), time.process_time()
            result = engine.recognize(image)
            walls.append((time.perf_counter() - w0) * 1000)
            cpus.append((time.process_time() - c0) * 1000)

        walls.sort()
        cpus.sort()
        median = walls[len(walls) // 2]
        cpu = cpus[len(cpus) // 2]
        print(
            f"  {label:26s} 墙钟 {median:7.0f} ms (min {walls[0]:.0f})"
            f"  CPU {cpu:7.0f} ms  并行度 {cpu / max(median, 1):.1f}x"
            f"  blocks={len(result.blocks)}"
        )

    engine.unload()
    del engine
    gc.collect()
    time.sleep(0.8)
    after = sysinfo.working_set_bytes()
    print(f"  卸载后 {sysinfo.human_mb(after)}  释放 {(loaded - after) / 1048576:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
