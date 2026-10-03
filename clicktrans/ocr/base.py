"""OCR 引擎接口。

刻意只暴露 recognize，不暴露「检测」和「识别」两个阶段——上层不该关心
Paddle 是两阶段还是 RapidOCR 是一阶段。想换引擎，写个新类就行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass(slots=True)
class TextBlock:
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...]  # 4 点多边形，物理像素坐标
    order: int = 0

    @property
    def center_y(self) -> float:
        return sum(p[1] for p in self.box) / len(self.box)

    @property
    def left(self) -> float:
        return min(p[0] for p in self.box)

    @property
    def height(self) -> float:
        ys = [p[1] for p in self.box]
        return max(ys) - min(ys)


@dataclass(slots=True)
class OcrResult:
    blocks: list[TextBlock] = field(default_factory=list)
    text: str = ""
    elapsed_ms: int = 0
    code_lines: list[int] = field(default_factory=list)

    @property
    def mean_confidence(self) -> float:
        if not self.blocks:
            return 0.0
        return sum(b.confidence for b in self.blocks) / len(self.blocks)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class OcrEngine(Protocol):
    name: str

    def warmup(self) -> None:
        """启动时后台预加载模型。"""

    def recognize(self, image: np.ndarray, upscale: float | None = None) -> OcrResult:
        """识别一帧。upscale 用于低置信度时放大重试。"""

    def unload(self) -> None:
        """空闲时释放模型，把内存还给系统。"""

    @property
    def is_ready(self) -> bool:
        """供托盘图标反馈状态。"""
