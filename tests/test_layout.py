"""OCR 行结果 → 段落文本的重组逻辑。"""

from __future__ import annotations

from clicktrans.ocr.base import TextBlock
from clicktrans.ocr.layout import group_lines, looks_like_code, reconstruct


def box(x: float, y: float, w: float = 60.0, h: float = 14.0):
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


def block(text: str, x: float, y: float, w: float = 60.0, h: float = 14.0, conf: float = 0.95):
    return TextBlock(text=text, confidence=conf, box=box(x, y, w, h))


class TestGrouping:
    def test_same_row_becomes_one_line(self):
        blocks = [block("Hello", 10, 100), block("world", 80, 102)]
        lines = group_lines(blocks)
        assert len(lines) == 1
        assert lines[0].text == "Hello world"

    def test_different_rows_stay_separate(self):
        blocks = [block("First", 10, 100), block("Second", 10, 130)]
        lines = group_lines(blocks)
        assert len(lines) == 2

    def test_blocks_sorted_left_to_right(self):
        # 故意乱序输入
        blocks = [block("second", 200, 100), block("first", 10, 100)]
        lines = group_lines(blocks)
        assert lines[0].text == "first second"

    def test_lines_sorted_top_to_bottom(self):
        blocks = [block("bottom", 10, 300), block("top", 10, 100)]
        lines = group_lines(blocks)
        assert [line.text for line in lines] == ["top", "bottom"]

    def test_empty_blocks_filtered(self):
        blocks = [block("   ", 10, 100), block("real", 100, 100)]
        lines = group_lines(blocks)
        assert len(lines) == 1
        assert lines[0].text == "real"

    def test_no_blocks(self):
        assert group_lines([]) == []


class TestJoining:
    def test_latin_gets_space(self):
        blocks = [block("Hello", 10, 100), block("world", 80, 100)]
        assert group_lines(blocks)[0].text == "Hello world"

    def test_cjk_gets_no_space(self):
        blocks = [block("你好", 10, 100), block("世界", 80, 100)]
        assert group_lines(blocks)[0].text == "你好世界"

    def test_kana_gets_no_space(self):
        blocks = [block("これは", 10, 100), block("テスト", 80, 100)]
        assert group_lines(blocks)[0].text == "これはテスト"

    def test_mixed_gets_space(self):
        blocks = [block("中文", 10, 100), block("English", 80, 100)]
        assert group_lines(blocks)[0].text == "中文 English"


class TestParagraphs:
    def test_normal_line_spacing_is_not_a_break(self):
        # 行距 30，行高 14 → 正常的行间距，不该断段
        blocks = [block("line one", 10, 100), block("line two", 10, 130)]
        result = reconstruct(blocks)
        assert result.text == "line one\nline two"

    def test_large_gap_creates_paragraph_break(self):
        blocks = [
            block("line one", 10, 100),
            block("line two", 10, 130),
            block("new paragraph", 10, 260),
        ]
        result = reconstruct(blocks)
        assert result.text == "line one\nline two\n\nnew paragraph"

    def test_reconstruct_empty(self):
        assert reconstruct([]).text == ""


class TestCodeDetection:
    def test_python_def(self):
        assert looks_like_code("def process(self):")

    def test_javascript(self):
        assert looks_like_code("const result = await fetch(url);")

    def test_arrow_function(self):
        assert looks_like_code("const fn = () => {}")

    def test_namespace_operator(self):
        assert looks_like_code("std::vector<int> items;")

    def test_preprocessor(self):
        assert looks_like_code("#include <stdio.h>")

    def test_plain_english_is_not_code(self):
        assert not looks_like_code("This is a normal English sentence.")

    def test_english_with_from_is_not_code(self):
        """'from' 在英文散文里太常见，不能因为有关键字就判成代码。"""
        assert not looks_like_code("From the beginning of the story")
        assert not looks_like_code("Please return the book tomorrow")
        assert not looks_like_code("I will print this document")

    def test_chinese_is_not_code(self):
        assert not looks_like_code("这是一段普通的中文说明文字。")

    def test_chinese_with_parentheses_is_not_code(self):
        assert not looks_like_code("注意（这一点很重要）")

    def test_code_lines_are_marked(self):
        blocks = [
            block("这是一段说明文字，用来介绍下面的函数。", 10, 100, w=300),
            block("def process(data):", 10, 130, w=300),
            block("    return data", 10, 160, w=300),
        ]
        result = reconstruct(blocks)
        assert result.code_lines, "至少应标记出一行代码"
        assert 0 not in result.code_lines, "中文说明行不该被标成代码"


class TestOrdering:
    def test_blocks_carry_line_order(self):
        blocks = [block("first", 10, 100), block("second", 10, 130)]
        result = reconstruct(blocks)
        orders = [b.order for b in result.blocks]
        assert orders == sorted(orders)
        assert len(set(orders)) == 2

    def test_reading_order_preserved(self):
        blocks = [
            block("one", 10, 100),
            block("two", 10, 130),
            block("three", 10, 160),
        ]
        result = reconstruct(blocks)
        assert result.text == "one\ntwo\nthree"
