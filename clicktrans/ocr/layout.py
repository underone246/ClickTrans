"""OCR 行结果 → 可翻译的段落文本。

OCR 引擎返回的是**行**，直接按顺序拼成一段会丢掉结构，LLM 翻译出来的
段落就会乱掉。这里做四件事：

1. 按 y 坐标聚类成行（同一行的多个文本框要合并）
2. 行内按 x 排序拼接，中日文之间不加空格，拉丁文之间补空格
3. 行间距超过阈值 → 判定为段落分隔
4. 识别看起来像代码的行，标记出来让翻译层原样保留

注意：第 4 条只是**标记**，不改变文本结构——保持流式输出的简单性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .base import TextBlock

# 同一行的 y 中心容差 = 行高 × 该系数
_LINE_TOLERANCE = 0.6
# 段落分隔阈值 = 行高 × 该系数
_PARAGRAPH_GAP = 1.5

_STRONG_PATTERNS = (
    re.compile(r"::|=>|->|:=|!=|==|\|\||&&|</|/>|\{\s*$|^\s*\}"),
    re.compile(r"^\s*#\s*\w+"),
    re.compile(r"\w+\([^)]*\)\s*[;{]?\s*$"),
)

# 关键字表刻意收得很紧。像 from / return / import / print 这类
# 在英文散文里太常见（"from the beginning"、"please print this"），
# 放进来的话整段英文都会被误判成代码。
_KEYWORD = re.compile(
    r"\b(def|class|function|const|let|var|public|private|static|void|"
    r"struct|interface|async|await|lambda|impl|namespace|select|insert)\b"
)
_PUNCT = re.compile(r"[(){}\[\];=<>]")

_CJK_RANGES = (
    (0x3040, 0x30FF),  # 假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # 兼容汉字
    (0xFF00, 0xFFEF),  # 全角字符
)


@dataclass
class Line:
    blocks: list[TextBlock] = field(default_factory=list)
    text: str = ""
    center_y: float = 0.0
    height: float = 0.0
    is_code: bool = False


@dataclass
class Layout:
    lines: list[Line] = field(default_factory=list)
    text: str = ""
    blocks: list[TextBlock] = field(default_factory=list)
    code_lines: list[int] = field(default_factory=list)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _needs_space(left: str, right: str) -> bool:
    """两个相邻文本框之间要不要补空格。"""
    if not left or not right:
        return False
    if left[-1].isspace() or right[0].isspace():
        return False
    # 中日文之间不加空格；有一侧是拉丁字母就补上
    if _is_cjk(left[-1]) and _is_cjk(right[0]):
        return False
    return True


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_cjk(ch)) / len(text)


def looks_like_code(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # 强特征：代码符号，几乎不会出现在自然语言里
    if any(pattern.search(stripped) for pattern in _STRONG_PATTERNS):
        return True
    # 弱特征：关键字 + 代码标点，且基本不含中日文（代码里不会有汉字）
    if (
        _KEYWORD.search(stripped)
        and _PUNCT.search(stripped)
        and _cjk_ratio(stripped) < 0.2
    ):
        return True
    # 符号占比过高也可疑（例如一大串括号分号）
    symbols = sum(
        1 for ch in stripped if not ch.isalnum() and not ch.isspace() and not _is_cjk(ch)
    )
    return len(stripped) >= 8 and symbols / len(stripped) > 0.35


def group_lines(blocks: list[TextBlock]) -> list[Line]:
    """按 y 中心把文本框聚类成行，行内按 x 排序。"""
    usable = [b for b in blocks if b.text.strip()]
    if not usable:
        return []

    heights = sorted(b.height for b in usable if b.height > 0)
    median_height = heights[len(heights) // 2] if heights else 12.0
    tolerance = max(3.0, median_height * _LINE_TOLERANCE)

    lines: list[Line] = []
    for block in sorted(usable, key=lambda b: b.center_y):
        placed = False
        for line in lines:
            if abs(line.center_y - block.center_y) <= tolerance:
                line.blocks.append(block)
                # 用加权平均更新行中心，块多了之后更稳
                total = len(line.blocks)
                line.center_y = (line.center_y * (total - 1) + block.center_y) / total
                line.height = max(line.height, block.height)
                placed = True
                break
        if not placed:
            lines.append(
                Line(
                    blocks=[block],
                    center_y=block.center_y,
                    height=block.height,
                )
            )

    for line in lines:
        line.blocks.sort(key=lambda b: b.left)
        line.text = _join_blocks(line.blocks)
        line.is_code = looks_like_code(line.text)

    lines.sort(key=lambda line: line.center_y)
    return lines


def _join_blocks(blocks: list[TextBlock]) -> str:
    out = ""
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if out and _needs_space(out, text):
            out += " "
        out += text
    return out


def reconstruct(blocks: list[TextBlock]) -> Layout:
    """重组为带段落结构的纯文本。"""
    lines = group_lines(blocks)
    if not lines:
        return Layout()

    heights = sorted(line.height for line in lines if line.height > 0)
    median_height = heights[len(heights) // 2] if heights else 12.0
    paragraph_gap = median_height * _PARAGRAPH_GAP

    parts: list[str] = []
    code_lines: list[int] = []
    previous: Line | None = None
    line_index = 0

    for line in lines:
        if previous is not None:
            gap = line.center_y - previous.center_y
            # 间距过大 → 段落分隔
            if gap > paragraph_gap + median_height:
                parts.append("")  # 空行 = 段落边界
            elif previous.is_code and line.is_code:
                pass  # 连续代码行之间不插空行
        parts.append(line.text)
        if line.is_code:
            code_lines.append(line_index)
        line_index += 1
        previous = line

    text = "\n".join(parts)

    ordered: list[TextBlock] = []
    for idx, line in enumerate(lines):
        for block in line.blocks:
            ordered.append(replace(block, order=idx))

    return Layout(lines=lines, text=text, blocks=ordered, code_lines=code_lines)
