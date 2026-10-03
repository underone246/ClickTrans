"""Pipeline —— 串联「采集 → 识别 → 语言判定 → 翻译 → 展示」。

跑在唯一的工作线程里。为什么只有一个线程：ONNX 推理会话不是线程安全的，
多线程要么加锁（等于串行），要么每线程一份模型（内存翻倍）。单线程 + 队列
是这里的最优解，而且 OCR 单帧 200-500 ms 也远没到需要并行的程度。

网络请求也放在这个线程（httpx 同步流式），顺带把「OCR 分段送译」天然串行化了。

取消靠 job id 比对：新的框选到来时 _latest_id 自增，正在跑的任务在每个
可中断点检查自己的 id 是否还等于 _latest_id，不等就立刻退出。
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QRect, QThread, pyqtSignal

from .capture.geometry import ScreenShot
from .config import Config
from .history import HistoryStore
from .lang import Lang, detect, resolve
from .ocr.rapidocr_engine import create_engine
from .translate.base import (
    NetworkError,
    TranslateError,
    TranslateRequest,
    TranslateResult,
)
from .translate.llm import create_translator
from .translate.prompts import clean_translation
from .utils import log
from .utils.image import qimage_to_ndarray

_log = log.get("pipeline")

# 跨线程 emit 的合并窗口：避免高频 token 把主线程事件循环淹掉
_EMIT_INTERVAL_S = 0.03
# 置信度低于这个值就放大重试一次
_LOW_CONFIDENCE = 0.5
_RETRY_UPSCALE = 3.0


@dataclass
class _Job:
    kind: str  # "translate" | "unload"
    id: int
    shot: ScreenShot | None = None
    rect: QRect | None = None
    preferred: Lang | None = None
    force_direction: tuple[Lang, Lang] | None = None


class Pipeline(QThread):
    engineStateChanged = pyqtSignal(bool)      # OCR 模型是否就绪
    statusChanged = pyqtSignal(str)            # 阶段提示，用于卡片上的小字
    ocrReady = pyqtSignal(str)                 # 识别出的原文
    directionReady = pyqtSignal(object, object)  # (src Lang, tgt Lang)
    partial = pyqtSignal(str)                  # 增量译文
    finished = pyqtSignal(object)              # TranslateResult
    failed = pyqtSignal(str)
    historyChanged = pyqtSignal()

    def __init__(self, config: Config, history: HistoryStore, parent=None):
        super().__init__(parent)
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._stop = False
        self._job_seq = 0

        self._cfg = config
        self._history = history
        self._engine = _make_engine(config)
        self._translator = create_translator(config.translate, "")
        self._pending_config: Config | None = None
        self._engine_ready = False

    # ------------------------------------------------------------ 主线程接口

    @property
    def engine(self):
        return self._engine

    @property
    def engine_ready(self) -> bool:
        return self._engine_ready

    def request_reload(self, config: Config) -> None:
        """配置变更走队列。

        不能直接在调用方（主线程）改 self._cfg / 重建 translator —— 工作线程
        可能正在用它们，会撞车。放到队列里由工作线程自己处理最安全。
        """
        self._pending_config = config
        self._job_seq += 1
        self._drain_queue()
        self._queue.put(_Job(kind="reload", id=self._job_seq))

    def _apply_config(self, config: Config) -> None:
        old = self._cfg
        self._cfg = config
        self._translator.close()
        self._translator = create_translator(config.translate, _read_key())

        # 只在这一组字段真的变了才重建引擎 —— 重建会丢掉已加载的模型，
        # 用户只是换个偏好语言不该付出重新加载一两秒的代价
        if _engine_signature(config) != _engine_signature(old):
            try:
                self._engine.unload()
            except Exception:
                pass
            self._engine = _make_engine(config)
            self._engine_ready = False
            _log.info("OCR 引擎已按新配置重建（%s/%s）", config.ocr.engine, config.ocr.model)

    def request(
        self,
        shot: ScreenShot,
        rect: QRect,
        preferred: Lang,
        force_direction: tuple[Lang, Lang] | None = None,
    ) -> None:
        self._job_seq += 1
        job = _Job(
            kind="translate",
            id=self._job_seq,
            shot=shot,
            rect=QRect(rect),
            preferred=preferred,
            force_direction=force_direction,
        )
        self._drain_queue()
        self._queue.put(job)

    def cancel(self) -> None:
        self._job_seq += 1
        self._drain_queue()

    def request_unload(self) -> None:
        self._job_seq += 1
        self._drain_queue()
        self._queue.put(_Job(kind="unload", id=self._job_seq))

    def stop(self) -> None:
        self._stop = True
        self._drain_queue()
        self._queue.put(None)

    def _drain_queue(self) -> None:
        """只丢弃排队中的翻译任务，保留 unload / reload 这类控制指令。

        这个细节很关键：如果连控制指令一起丢掉，用户刚改完设置就框选，
        配置变更会被悄悄吞掉。
        """
        kept: list[_Job | None] = []
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is None or job.kind != "translate":
                kept.append(job)
        for job in kept:
            self._queue.put(job)

    # ---------------------------------------------------------------- 线程体

    def run(self) -> None:  # noqa: D102
        self._warmup()
        while not self._stop:
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                if job.kind == "unload":
                    self._handle_unload()
                elif job.kind == "reload":
                    if self._pending_config is not None:
                        self._apply_config(self._pending_config)
                        self._pending_config = None
                else:
                    self._handle_translate(job)
            except Exception as exc:  # 兜底，绝不让工作线程死掉
                _log.exception("任务执行异常")
                self.failed.emit(f"内部错误：{exc}")

    def _warmup(self) -> None:
        try:
            self.statusChanged.emit("正在加载 OCR 模型…")
            self._engine.warmup()
            self._engine_ready = True
            self.engineStateChanged.emit(True)
            _log.info("OCR 模型就绪")
        except Exception as exc:
            self._engine_ready = False
            self.engineStateChanged.emit(False)
            self.failed.emit(str(exc))
            return
        try:
            self._translator.warmup()
        except Exception:
            pass

    def _handle_unload(self) -> None:
        try:
            self._engine.unload()
        except Exception as exc:
            _log.warning("卸载模型失败: %s", exc)
        self._engine_ready = False
        self.engineStateChanged.emit(False)

    # ------------------------------------------------------------ 翻译全流程

    def _handle_translate(self, job: _Job) -> None:
        if job.shot is None or job.rect is None:
            return

        started = time.perf_counter()

        # 1. 裁剪
        image = job.shot.crop(job.rect)
        if image.isNull() or image.width() < 2 or image.height() < 2:
            self.failed.emit("选区太小了，再框大一点吧。")
            return
        array = qimage_to_ndarray(image)
        if array.size == 0:
            self.failed.emit("截图内容为空。")
            return

        # 2. OCR
        # 只在模型没热的时候说「唤醒」，否则两条提示会连着发出，
        # 用户只看到后一条，反而掩盖了「要多等一两秒」这个事实
        if self._engine_ready:
            self.statusChanged.emit("识别中…")
        else:
            self.statusChanged.emit("正在唤醒 OCR 模型…")
        try:
            result = self._engine.recognize(array)
        except Exception as exc:
            _log.exception("OCR 失败")
            self.failed.emit(f"文字识别失败：{exc}")
            return
        if self._stale(job):
            return

        # 置信度太低就放大重试一次
        if result.blocks and result.mean_confidence < _LOW_CONFIDENCE:
            _log.info("置信度偏低（%.2f），放大 %.1fx 重试", result.mean_confidence, _RETRY_UPSCALE)
            self.statusChanged.emit("识别质量较低，重试中…")
            try:
                retry = self._engine.recognize(array, upscale=_RETRY_UPSCALE)
                if retry.text.strip() and retry.mean_confidence > result.mean_confidence:
                    result = retry
            except Exception as exc:
                _log.warning("放大重试失败: %s", exc)
            if self._stale(job):
                return

        if result.is_empty:
            self.failed.emit("未识别到文字。")
            return

        self.ocrReady.emit(result.text)

        # 3. 语言判定（本地，不发请求）
        if job.force_direction:
            src, tgt = job.force_direction
        else:
            preferred = job.preferred or self._cfg.general.preferred
            src = detect(result.text)
            src, tgt = resolve(src, preferred)
        self.directionReady.emit(src, tgt)
        _log.info(
            "方向判定：%s → %s（字符数 %d）",
            src.value, tgt.value, len(result.text),
        )

        # 4. 命中历史就秒回
        cached = self._history.find(result.text, src.value, tgt.value)
        if cached is not None:
            self.statusChanged.emit("来自历史记录")
            self.finished.emit(
                TranslateResult(
                    text=cached.target_text,
                    source_text=result.text,
                    src_lang=src,
                    tgt_lang=tgt,
                    engine=cached.engine,
                    from_history=True,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            return

        # 5. 流式翻译
        self.statusChanged.emit("翻译中…")
        req = TranslateRequest(
            text=result.text,
            source_lang=src,
            target_lang=tgt,
            glossary=dict(self._cfg.glossary.terms),
            code_hint=bool(result.code_lines),
        )

        accumulated = ""
        pending = ""
        last_emit = 0.0
        try:
            for chunk in self._translator.translate(req):
                if self._stale(job):
                    _log.info("任务已被新的框选取代，中止")
                    return
                accumulated += chunk
                pending += chunk
                now = time.perf_counter()
                if now - last_emit >= _EMIT_INTERVAL_S:
                    self.partial.emit(pending)
                    pending = ""
                    last_emit = now
            if pending:
                self.partial.emit(pending)
        except NetworkError as exc:
            # 离线：识别结果暂存为「未翻译」，联网后可补译
            self._history.add(
                result.text, "", src.value, tgt.value,
                engine=self._cfg.translate.model, pending=True,
            )
            self.historyChanged.emit()
            self.failed.emit(
                f"{exc}\n已识别原文，可按「原文」查看；联网后会重新翻译。"
            )
            return
        except TranslateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:
            _log.exception("翻译异常")
            self.failed.emit(f"翻译失败：{exc}")
            return

        if self._stale(job):
            return

        final_text = clean_translation(accumulated)
        if not final_text.strip():
            self.failed.emit("翻译接口返回了空结果，可以点「重译」再试一次。")
            return

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._history.add(
            result.text, final_text, src.value, tgt.value,
            engine=self._cfg.translate.model,
        )
        self.historyChanged.emit()

        self.finished.emit(
            TranslateResult(
                text=final_text,
                source_text=result.text,
                src_lang=src,
                tgt_lang=tgt,
                engine=self._cfg.translate.model,
                elapsed_ms=elapsed_ms,
            )
        )

    def _stale(self, job: _Job) -> bool:
        return job.id != self._job_seq or self._stop


def _read_key() -> str:
    from . import config as config_module

    return config_module.load_api_key()


def _make_engine(cfg: Config):
    return create_engine(
        cfg.ocr.engine,
        upscale=cfg.ocr.upscale,
        max_pixels=cfg.ocr.max_pixels,
        min_confidence=cfg.ocr.min_confidence,
        model=cfg.ocr.model,
        det_limit_side_len=cfg.ocr.det_limit_side_len,
        det_limit_type=cfg.ocr.det_limit_type,
        use_angle_cls=cfg.ocr.use_angle_cls,
        rec_batch_num=cfg.ocr.rec_batch_num,
    )


def _engine_signature(cfg: Config) -> tuple:
    """影响已加载模型的字段集合。改了这些才值得重建引擎。"""
    ocr = cfg.ocr
    return (
        ocr.engine,
        ocr.model,
        ocr.upscale,
        ocr.max_pixels,
        ocr.min_confidence,
        ocr.det_limit_side_len,
        ocr.det_limit_type,
        ocr.use_angle_cls,
        ocr.rec_batch_num,
    )
