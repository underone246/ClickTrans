"""三语（中文 / 日语 / 英语）检测与翻译方向决策。

全部在本地算，不发网络请求。检测靠字符集统计，不引入语言识别模型
（那要多占几十 MB 内存，与内存约束冲突）。

已知局限：日语全汉字短句（「株式会社」「確認事項」）没有假名，会被判成中文。
这不致命——方向由用户偏好决定，检测错的只是源语言标记，影响的是提示词
里注入的语气约束。
"""

from __future__ import annotations

from enum import StrEnum


class Lang(StrEnum):
    ZH = "zh"
    JA = "ja"
    EN = "en"


LABEL: dict[Lang, str] = {
    Lang.ZH: "中文",
    Lang.JA: "日本語",
    Lang.EN: "English",
}

# 源语言 == 偏好语言时，改译成哪个回退语言
FALLBACK: dict[Lang, Lang] = {
    Lang.ZH: Lang.EN,
    Lang.JA: Lang.ZH,
    Lang.EN: Lang.ZH,
}

# Shift 循环切换偏好语言的顺序
CYCLE: tuple[Lang, ...] = (Lang.ZH, Lang.JA, Lang.EN)


def _count_chars(text: str) -> tuple[int, int, int, int]:
    """返回 (平假名, 片假名, CJK 汉字, 拉丁字母) 的字符计数。"""
    hira = kata = cjk = latin = 0
    for ch in text:
        o = ord(ch)
        # 3041-3096 平假名主体，309D-309E 迭字符
        if 0x3041 <= o <= 0x3096 or 0x309D <= o <= 0x309E:
            hira += 1
        # 30A1-30FA 片假名主体，30FD-30FE 迭字符
        # 刻意排除 30FB「・」和 30FC「ー」——中文排版里也会出现
        elif 0x30A1 <= o <= 0x30FA or 0x30FD <= o <= 0x30FE:
            kata += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            cjk += 1
        elif "a" <= ch <= "z" or "A" <= ch <= "Z":
            latin += 1
    return hira, kata, cjk, latin


def detect(text: str) -> Lang:
    """判定文本的源语言。规则见架构文档 4.4。"""
    hira, kata, cjk, latin = _count_chars(text)
    significant = hira + kata + cjk + latin
    if significant == 0:
        return Lang.EN

    # 1. 假名是日语独有的，命中即可直接返回
    if hira + kata > 0:
        return Lang.JA

    # 2. 无假名，汉字占比超三成 → 中文
    if cjk / significant > 0.3:
        return Lang.ZH

    # 3. 拉丁字母占绝对多数 → 英语
    if latin / significant > 0.6:
        return Lang.EN

    # 4. 混合文本，汉字与拉丁票数相比
    return Lang.ZH if cjk >= latin else Lang.EN


def resolve(src: Lang, preferred: Lang) -> tuple[Lang, Lang]:
    """推导实际翻译方向。

    一句话规则：源语言 ≠ 偏好语言 → 译成偏好语言；相等 → 译成回退语言。
    """
    if src == preferred:
        return src, FALLBACK[preferred]
    return src, preferred


def next_preferred(current: Lang) -> Lang:
    """Shift 切换用：中文 → 日语 → 英语 → 中文。"""
    try:
        idx = CYCLE.index(current)
    except ValueError:
        return CYCLE[0]
    return CYCLE[(idx + 1) % len(CYCLE)]


def parse_lang(value: str, default: Lang = Lang.ZH) -> Lang:
    """把配置里的字符串宽松地解析成 Lang。"""
    if not value:
        return default
    v = value.strip().lower()
    for lang in Lang:
        if lang.value == v:
            return lang
    aliases = {
        "zh-cn": Lang.ZH, "zh-hans": Lang.ZH, "cn": Lang.ZH, "chinese": Lang.ZH, "中文": Lang.ZH,
        "ja-jp": Lang.JA, "jp": Lang.JA, "japanese": Lang.JA, "日语": Lang.JA, "日本語": Lang.JA,
        "en-us": Lang.EN, "en-gb": Lang.EN, "english": Lang.EN, "英语": Lang.EN,
    }
    return aliases.get(v, default)
