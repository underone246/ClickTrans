"""翻译器接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

from ..lang import Lang


class TranslateError(Exception):
    """翻译失败。message 是可以直接展示给用户的中文提示。"""


class NoApiKeyError(TranslateError):
    pass


class NetworkError(TranslateError):
    """网络不可达。

    单独成一类是因为离线模式的判定依赖它：命中历史就复用，没命中就把
    识别结果暂存为「未翻译」，等联网后补译。见架构文档 7.10。
    """


@dataclass(slots=True)
class TranslateRequest:
    text: str
    source_lang: Lang  # 已在本地检测好，不让 LLM 去猜
    target_lang: Lang
    glossary: dict[str, str] = field(default_factory=dict)
    code_hint: bool = False  # 文本里含疑似代码行，提示模型原样保留


@dataclass(slots=True)
class TranslateResult:
    text: str = ""
    source_text: str = ""
    src_lang: Lang = Lang.EN
    tgt_lang: Lang = Lang.ZH
    engine: str = ""
    from_history: bool = False
    elapsed_ms: int = 0
    partial: bool = False  # 流式过程中为 True


class Translator(Protocol):
    name: str

    def translate(self, req: TranslateRequest) -> Iterator[str]:
        """返回增量片段，不是完整结果 —— 上层边收边渲染。

        返回 Iterator 而不是 str 是刻意的：如果返回完整字符串，
        「实时」就没了，用户要盯着加载圈等 1-2 秒。
        """
