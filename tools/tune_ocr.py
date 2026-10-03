"""开发用：OCR 参数调优矩阵。

每个配置在独立子进程里跑（onnxruntime 的 SessionOptions 是进程级全局的，
同进程切换会互相污染）。

    python tools/tune_ocr.py

背景（调优时发现的几个关键事实）：
- rapidocr-onnxruntime 1.2.3 自带的是 **PP-OCRv3** 模型，只覆盖中文/英文
- 检测器默认 limit_type='min', limit_side_len=736，会把短边**放大**到 736。
  所以我们自己再 pre-upscale 2x 是重复劳动，白花一倍时间
- rec 阶段通常是总耗时的大头
"""

from __future__ import annotations

import argparse
import gc
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CASES = [
    ("小选区 600x120 / 3 行", 600, 120, 3),
    ("中选区 1000x360 / 5 行", 1000, 360, 5),
    ("大选区 1600x800 / 14 行", 1600, 800, 14),
]

BASE = dict(arena="off", intra=0, spin=1, det_len=736, det_type="min", cls=1, rec_batch=6, pre=2.0)

MATRIX = [
    ("A 当前实现 (det 736/min, pre-upscale 2x)", dict(BASE)),
    ("B 不 pre-upscale", dict(BASE, pre=1.0)),
    ("C 不 pre-upscale + det_type=max 960", dict(BASE, pre=1.0, det_type="max", det_len=960)),
    ("D C + 关掉方向分类", dict(BASE, pre=1.0, det_type="max", det_len=960, cls=0)),
    ("E D + rec_batch=16", dict(BASE, pre=1.0, det_type="max", det_len=960, cls=0, rec_batch=16)),
    ("F E + arena=on", dict(BASE, pre=1.0, det_type="max", det_len=960, cls=0, rec_batch=16, arena="on")),
    ("G F + 关自旋", dict(BASE, pre=1.0, det_type="max", det_len=960, cls=0, rec_batch=16, arena="on", spin=0)),
    ("H G + intra=4", dict(BASE, pre=1.0, det_type="max", det_len=960, cls=0, rec_batch=16, arena="on", spin=0, intra=4)),
]


def make_image(width: int, height: int, lines: int):
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = 10
    for _ in range(lines):
        draw.text((10, y), "The quick brown fox jumps over the lazy dog 0123", fill="black", font=font)
        y += 34
    return np.asarray(image)[:, :, :3].copy()


def worker(cfg: dict) -> int:
    import onnxruntime as ort
    from clicktrans.utils import sysinfo
    from clicktrans.utils.image import prepare_for_ocr
    from clicktrans.ocr.layout import reconstruct
    from clicktrans.ocr.rapidocr_engine import _parse_raw

    original_init = ort.InferenceSession.__init__
    if cfg["arena"] == "on":
        import onnxruntime.capi.onnxruntime_inference_collection as collection

        original_init = collection.InferenceSession.__init__

    def patched_inner(self, path_or_bytes, sess_options=None, *a, **kw):
        if sess_options is None:
            sess_options = ort.SessionOptions()
        if cfg["arena"] == "off":
            sess_options.enable_cpu_mem_arena = False
            sess_options.enable_mem_pattern = False
        if cfg["intra"] > 0:
            sess_options.intra_op_num_threads = cfg["intra"]
            sess_options.inter_op_num_threads = 1
        if cfg["spin"] == 0:
            for entry in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
                try:
                    sess_options.add_session_config_entry(entry, "0")
                except Exception:
                    pass
        sess_options.log_severity_level = 3
        return original_init(self, path_or_bytes, sess_options, *a, **kw)

    if cfg["arena"] == "off":
        ort.InferenceSession.__init__ = patched_inner
    else:
        # arena 保持默认，但仍然要应用线程 / 自旋设置
        def patched_keep(self, path_or_bytes, sess_options=None, *a, **kw):
            if sess_options is None:
                sess_options = ort.SessionOptions()
            if cfg["intra"] > 0:
                sess_options.intra_op_num_threads = cfg["intra"]
                sess_options.inter_op_num_threads = 1
            if cfg["spin"] == 0:
                for entry in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
                    try:
                        sess_options.add_session_config_entry(entry, "0")
                    except Exception:
                        pass
            sess_options.log_severity_level = 3
            return original_init(self, path_or_bytes, sess_options, *a, **kw)

        ort.InferenceSession.__init__ = patched_keep

    from rapidocr_onnxruntime import RapidOCR

    baseline = sysinfo.working_set_bytes()
    started = time.perf_counter()
    rapid = RapidOCR(
        use_angle_cls=bool(cfg["cls"]),
        det_limit_side_len=cfg["det_len"],
        det_limit_type=cfg["det_type"],
        # 这个库有个坑：det/rec 段里只要传了任意一个参数，就必须同时带上
        # model_path 键，否则 update_xxx_params 里会 KeyError。
        # 传空字符串会被解析成包内自带的模型路径。
        det_model_path="",
        rec_model_path="",
        rec_batch_num=cfg["rec_batch"],
    )
    load_ms = (time.perf_counter() - started) * 1000
    loaded = sysinfo.working_set_bytes()

    print(f"| 加载 {load_ms:.0f}ms | 内存 {sysinfo.human_mb(baseline)}→{sysinfo.human_mb(loaded)}"
          f"→{sysinfo.human_mb(sysinfo.working_set_bytes())}")

    for label, width, height, lines in CASES:
        raw = make_image(width, height, lines)
        image = prepare_for_ocr(raw, cfg["pre"], 4_000_000)
        rapid(image)  # 预热

        walls, dets, recs = [], [], []
        for _ in range(3):
            t = time.perf_counter()
            res, elapse = rapid(image)
            walls.append((time.perf_counter() - t) * 1000)
            dets.append(elapse[0] * 1000)
            rest = sum(elapse[1:]) * 1000
            recs.append(rest)
        blocks = reconstruct(_parse_raw(res)).blocks
        med = sorted(walls)[1]
        print(f"|   {label:24s} 墙钟 {med:6.0f}ms = det {sorted(dets)[1]:5.0f} + 其余 {sorted(recs)[1]:5.0f}"
              f"  blocks={len(blocks)}")

    del rapid
    gc.collect()
    time.sleep(0.8)
    after = sysinfo.working_set_bytes()
    print(f"| 卸载后 {sysinfo.human_mb(after)}  释放 {(loaded - after) / 1048576:.0f}MB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    for key, value in BASE.items():
        parser.add_argument(f"--{key.replace('_', '-')}", type=type(value), default=value)
    args = parser.parse_args()

    if args.worker:
        cfg = {k: getattr(args, k.replace("-", "_")) for k in BASE}
        return worker(cfg)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONIOENCODING"] = "utf-8"

    for label, cfg in MATRIX:
        print(f"===== {label} =====")
        cmd = [sys.executable, __file__, "--worker"]
        for key, value in cfg.items():
            cmd += [f"--{key.replace('_', '-')}", str(value)]
        subprocess.run(cmd, env=env, cwd=str(Path(__file__).resolve().parent.parent))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
