"""RapidOCR 引擎：模型加载 / 卸载 + onnxruntime 调优。

内存约束（架构文档 7.9）在这里落地。有两条结论是**实测出来的**，
其中一条推翻了最初的判断，所以特别记下来（数据见 tools/tune_ocr.py）：

1. **不要关内存池。** 最初按「del session 之后内存不还给 OS」的经验，
   给 onnxruntime 加了 `enable_cpu_mem_arena = False`。实测发现这是错的：
   它让推理慢一倍（中选区 233 ms → 678 ms），而卸载后的内存释放量
   毫无变化（两种设置下都是 8-13 MB）。所以这里**刻意不动 arena**。

2. **要关线程自旋。** 收益巨大：中选区 678 ms → 233 ms。
   这台机器 16 个逻辑核，onnxruntime 默认起 12+ 线程忙等，一次识别烧掉
   25 秒 CPU 才换来 2 秒墙钟。关掉自旋后线程没事就睡了，
   顺带解决了「机器一忙就掉到 6 秒」的抖动问题。

另外自带模型是 **PP-OCRv3，只覆盖中文/英文**（不是 v4，也不是多语言版）。
日语的汉字能认，假名识别偏弱——这是模型本身的限制，换 server 版模型
或放一个日语 rec 模型进来才能改善，见 README。
"""

from __future__ import annotations

import gc
import threading
import time

import numpy as np

from ..utils import log
from ..utils.image import prepare_for_ocr
from .base import OcrEngine, OcrResult, TextBlock
from .layout import reconstruct

_log = log.get("ocr")

_ort_patched = False
_ort_patch_lock = threading.Lock()


def patch_onnxruntime() -> bool:
    """关掉 onnxruntime 的线程自旋。返回是否打上补丁。

    为什么需要它：默认配置下 onnxruntime 在 16 核机器上会起 12+ 个线程
    并让它们忙等（spin）而不是睡眠。结果是 CPU 时间被烧到墙钟时间的
    12 倍，机器一有别的负载就严重抖动。

    **注意这里不动 enable_cpu_mem_arena** —— 实测关掉它只会让推理慢一倍，
    并不会多释放内存。见模块 docstring。
    """
    global _ort_patched
    with _ort_patch_lock:
        if _ort_patched:
            return True
        try:
            import onnxruntime as ort  # type: ignore
        except Exception:
            return False

        try:
            original_init = ort.InferenceSession.__init__

            def patched_init(self, path_or_bytes, sess_options=None, *args, **kwargs):
                if sess_options is None:
                    sess_options = ort.SessionOptions()
                for entry in (
                    "session.intra_op.allow_spinning",
                    "session.inter_op.allow_spinning",
                ):
                    try:
                        sess_options.add_session_config_entry(entry, "0")
                    except Exception:
                        pass
                sess_options.log_severity_level = 3
                return original_init(self, path_or_bytes, sess_options, *args, **kwargs)

            ort.InferenceSession.__init__ = patched_init
            _ort_patched = True
            _log.info("已关闭 onnxruntime 线程自旋")
            return True
        except Exception as exc:
            _log.warning("打 onnxruntime 补丁失败: %s", exc)
            return False


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(
        self,
        upscale: float = 1.0,
        max_pixels: int = 4_000_000,
        min_confidence: float = 0.6,
        model: str = "mobile",
        det_limit_side_len: int = 960,
        det_limit_type: str = "max",
        use_angle_cls: bool = False,
        rec_batch_num: int = 6,
    ):
        self.upscale = float(upscale)
        self.max_pixels = int(max_pixels)
        self.min_confidence = float(min_confidence)
        self.model = (model or "mobile").lower()
        self.det_limit_side_len = int(det_limit_side_len)
        self.det_limit_type = det_limit_type
        self.use_angle_cls = bool(use_angle_cls)
        self.rec_batch_num = int(rec_batch_num)

        self._engine = None
        self._lock = threading.RLock()
        self._loading = False

    # ------------------------------------------------------------ 生命周期

    @property
    def is_ready(self) -> bool:
        return self._engine is not None

    @property
    def is_loading(self) -> bool:
        return self._loading

    def warmup(self) -> None:
        self._ensure_engine()

    def unload(self) -> None:
        with self._lock:
            if self._engine is None:
                return
            self._engine = None
            gc.collect()
            _log.info("OCR 模型已卸载，内存已交还系统")

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            self._loading = True
            try:
                self._engine = self._create_engine()
            finally:
                self._loading = False
            return self._engine

    def _create_engine(self):
        started = time.perf_counter()
        patch_onnxruntime()
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "未安装 rapidocr-onnxruntime，无法做 OCR。\n"
                "请在虚拟环境里执行：pip install rapidocr-onnxruntime"
            ) from exc

        engine = RapidOCR(**self._engine_kwargs())
        _log.info(
            "OCR 模型加载完成（%s），耗时 %.0f ms",
            self.model, (time.perf_counter() - started) * 1000,
        )
        return engine

    def _engine_kwargs(self) -> dict:
        """组装 RapidOCR 的构造参数。

        这里有几个值刻意偏离库的默认，都是实测出来的：

        - `det_limit_type="max"` + `det_limit_side_len=960`
          库的默认是 `min` + 736，意思是把**短边放大到至少 736**。框选出来的
          往往是一条又宽又矮的文字带，在 min 模式下会被放大好几倍，检测阶段
          因此要花 600 ms 以上。改成 max（限制长边）之后不再无脑放大。

        - `upscale=1.0`（默认不再二次放大）
          因为上面那个 min 模式本身就在放大，我们再 pre-upscale 2x 纯属重复
          劳动，白白多花一倍时间。

        - `use_angle_cls=False`
          屏幕上的文字几乎不会是倒着的，方向分类只占 4-8 ms 但没意义。
          真要识别竖排或旋转文字的用户可以把它打开。
        """
        kwargs = {
            "use_angle_cls": self.use_angle_cls,
            "det_limit_side_len": self.det_limit_side_len,
            "det_limit_type": self.det_limit_type,
            "rec_batch_num": self.rec_batch_num,
        }
        kwargs.update(_model_paths(self.model))
        return kwargs

    # ---------------------------------------------------------------- 识别

    def recognize(self, image: np.ndarray, upscale: float | None = None) -> OcrResult:
        if image is None or image.size == 0:
            return OcrResult()

        engine = self._ensure_engine()
        factor = self.upscale if upscale is None else float(upscale)
        prepared = prepare_for_ocr(image, factor, self.max_pixels)

        started = time.perf_counter()
        with self._lock:
            raw, _elapse = engine(prepared)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        blocks = _parse_raw(raw)
        if self.min_confidence > 0 and blocks:
            kept = [b for b in blocks if b.confidence >= self.min_confidence]
            # 全被过滤掉说明整体置信度都低，那就保留原样，
            # 免得用户看到「未识别到文字」但屏幕上明明有字
            if kept:
                blocks = kept

        layout = reconstruct(blocks)
        _log.info(
            "OCR 完成：%d 块 / %d 行 / %d ms（放大 %.1fx）",
            len(layout.blocks), len(layout.lines), elapsed_ms, factor,
        )
        return OcrResult(
            blocks=layout.blocks,
            text=layout.text,
            elapsed_ms=elapsed_ms,
            code_lines=layout.code_lines,
        )


def _model_paths(model: str) -> dict[str, str]:
    """返回 det / rec / cls 三个模型路径。空字符串 = 用包内自带的。

    注意：**三个键必须始终存在**。rapidocr-onnxruntime 的
    `update_det_params` / `update_rec_params` 里直接 `if not
    dict['model_path']`，只要传了该段的任意一个参数却没带 model_path 就会
    KeyError。所以这里无条件返回三个键。

    server 版需要用户自己把 onnx 放进 ~/.clicktrans/models/server/。
    找不到就退回包内自带的（PP-OCRv3 中英文），并明确记一条日志——
    宁可慢一点，也不要静默用错模型。
    """
    paths = {"det_model_path": "", "rec_model_path": "", "cls_model_path": ""}
    if model != "server":
        return paths

    from ..config import app_dir

    model_dir = app_dir() / "models" / "server"
    if not model_dir.is_dir():
        _log.warning("未找到 server 模型目录 %s，回退到包内自带模型", model_dir)
        return paths

    found: dict[str, str] = {}
    for key, pattern in (
        ("det_model_path", "*det*.onnx"),
        ("rec_model_path", "*rec*.onnx"),
        ("cls_model_path", "*cls*.onnx"),
    ):
        matches = sorted(model_dir.glob(pattern))
        if matches:
            found[key] = str(matches[0])

    if "det_model_path" not in found or "rec_model_path" not in found:
        _log.warning(
            "server 模型文件不全（det=%s rec=%s），回退到包内自带模型",
            "det_model_path" in found, "rec_model_path" in found,
        )
        return paths

    paths.update(found)
    _log.info("使用自定义模型：%s", found)
    return paths


def _parse_raw(raw) -> list[TextBlock]:
    """RapidOCR 返回 [[box, text, score], ...]，box 是 4 个 [x, y]。"""
    if not raw:
        return []
    blocks: list[TextBlock] = []
    for entry in raw:
        try:
            box, text, score = entry[0], entry[1], entry[2]
        except (TypeError, IndexError):
            continue
        if not text or not str(text).strip():
            continue
        try:
            polygon = tuple((float(p[0]), float(p[1])) for p in box)
        except (TypeError, IndexError, ValueError):
            continue
        if len(polygon) < 4:
            continue
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 1.0
        blocks.append(TextBlock(text=str(text).strip(), confidence=confidence, box=polygon))
    return blocks


def create_engine(engine_name: str = "rapidocr", **kwargs) -> OcrEngine:
    """工厂。目前只有 rapidocr 一种实现。"""
    if engine_name not in ("rapidocr", "rapidocr_onnx", "default"):
        _log.warning("未知 OCR 引擎 %r，回退到 rapidocr", engine_name)
    return RapidOcrEngine(**kwargs)
