"""三语检测与方向决策。"""

from __future__ import annotations

import pytest

from clicktrans.lang import CYCLE, FALLBACK, LABEL, Lang, detect, next_preferred, parse_lang, resolve


class TestDetect:
    def test_english(self):
        assert detect("Hello world, this is a test sentence.") == Lang.EN

    def test_chinese(self):
        assert detect("这是一段中文，用来测试语言检测是否正确。") == Lang.ZH

    def test_japanese_with_kana(self):
        assert detect("これはテストです。") == Lang.JA

    def test_japanese_with_katakana(self):
        assert detect("コンピュータ・サイエンス") == Lang.JA

    def test_known_limitation_all_kanji_japanese(self):
        """日语全汉字短句会被判成中文 —— 这是已知且接受的局限。

        不修的原因：要修就得引入语言识别模型，多占几十 MB 内存，
        与内存约束冲突。好在方向由用户偏好决定，检测错的只是提示词语气。
        """
        assert detect("株式会社") == Lang.ZH
        assert detect("確認事項") == Lang.ZH

    def test_chinese_with_middle_dot_is_not_japanese(self):
        """「・」和「ー」在中文排版里也出现，不能当成假名。"""
        assert detect("人工智能·机器学习·深度学习") == Lang.ZH

    def test_empty_text(self):
        assert detect("") == Lang.EN

    def test_punctuation_only(self):
        assert detect("1234567890!@#$%^&*()") == Lang.EN

    def test_mixed_code_and_chinese(self):
        # 中英混排，汉字占多数 → 中文
        assert detect("这个函数 def foo() 用来处理数据") == Lang.ZH

    def test_code_heavy_is_english(self):
        assert detect("const result = await fetchData(url);") == Lang.EN


class TestResolve:
    @pytest.mark.parametrize(
        "src,preferred,expected",
        [
            # 与架构文档 4.4 的方向表逐格对齐
            (Lang.ZH, Lang.ZH, (Lang.ZH, Lang.EN)),
            (Lang.ZH, Lang.JA, (Lang.ZH, Lang.JA)),
            (Lang.ZH, Lang.EN, (Lang.ZH, Lang.EN)),
            (Lang.JA, Lang.ZH, (Lang.JA, Lang.ZH)),
            (Lang.JA, Lang.JA, (Lang.JA, Lang.ZH)),
            (Lang.JA, Lang.EN, (Lang.JA, Lang.EN)),
            (Lang.EN, Lang.ZH, (Lang.EN, Lang.ZH)),
            (Lang.EN, Lang.JA, (Lang.EN, Lang.JA)),
            (Lang.EN, Lang.EN, (Lang.EN, Lang.ZH)),
        ],
    )
    def test_full_direction_table(self, src, preferred, expected):
        assert resolve(src, preferred) == expected

    def test_never_same_language(self):
        """任何组合都不能出现「译成同一种语言」。"""
        for src in Lang:
            for preferred in Lang:
                first, second = resolve(src, preferred)
                assert first != second, f"src={src} preferred={preferred}"

    def test_fallback_is_never_identity(self):
        for lang in Lang:
            assert FALLBACK[lang] != lang


class TestHelpers:
    def test_cycle_order(self):
        assert CYCLE == (Lang.ZH, Lang.JA, Lang.EN)

    def test_next_preferred_wraps(self):
        assert next_preferred(Lang.ZH) == Lang.JA
        assert next_preferred(Lang.JA) == Lang.EN
        assert next_preferred(Lang.EN) == Lang.ZH

    def test_every_language_has_label(self):
        for lang in Lang:
            assert LABEL[lang]

    def test_parse_lang_accepts_aliases(self):
        assert parse_lang("ja-jp") == Lang.JA
        assert parse_lang("日本語") == Lang.JA
        assert parse_lang("中文") == Lang.ZH
        assert parse_lang("") == Lang.ZH
        assert parse_lang("klingon") == Lang.ZH
        assert parse_lang("klingon", Lang.EN) == Lang.EN
