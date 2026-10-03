"""方向感知的提示词。

6 个方向共用一套骨架，按语言对注入不同的约束段。这些细节看着琐碎，
但它们决定了中↔日、英↔日这类方向的译文是「能看」还是「一眼机翻」。
"""

from __future__ import annotations

import re

from ..lang import LABEL, Lang
from .base import TranslateRequest

_SYSTEM_SKELETON = """你是一个专业的翻译引擎。把用户给出的文本翻译成{target}。

【硬性要求】
- 只输出译文本身。不要加任何前言、解释、注记、罗马音标注、引号包裹
- 严格保持原文的换行和段落结构
- 代码、URL、邮箱、变量名、文件路径、命令行原样保留，不要翻译
- 数字、单位、型号、版本号不做转换
- 如果原文本身就是目标语言，原样返回，不要改写

【本次方向】{src} → {tgt}
{direction}"""

_CODE_HINT = """
【注意】原文中有些行看起来是代码。请把这些行原样保留在译文中，
不要翻译它们，也不要把它们合并成一行。"""

_GLOSSARY_HEADER = "\n【术语表】以下词条必须按指定译法处理："


DIRECTION_RULES: dict[tuple[Lang, Lang], str] = {
    (Lang.ZH, Lang.JA): (
        "默认使用「です・ます」敬体（技术文档、说明文字都适用），"
        "除非原文明显是口语或小说对白。\n"
        "专有名词和公司名保留汉字写法，不要音译成片假名。"
    ),
    (Lang.JA, Lang.ZH): (
        "人名、地名、机构名保留原文汉字写法（如「田中」不要改成「塔纳卡」）。\n"
        "敬语层级用中文的自然表达还原，不要堆砌「您」「敬请」。" 
    ),
    (Lang.ZH, Lang.EN): (
        "技术术语保留英文原词（Transformer、Kubernetes、token 等），不要另造译名。\n"
        "成语、俗语用英文的等价表达意译，不要逐字直译。\n"
        "中文的「我们」「大家」按语境转成 we / you / one，不要一律译成 we。"
    ),
    (Lang.EN, Lang.ZH): (
        "长定语从句拆成短句，中文不习惯长前置定语。\n"
        "被动语态按中文习惯转为主动或「被」以外的自然表达。\n"
        "英文的 it / they 指代要还原成具体名词，避免满篇「它」「他们」。"
    ),
    (Lang.EN, Lang.JA): (
        "外来语写成片假名，技术术语保留英文原词（日语技术文档的通行做法）。\n"
        "默认使用「です・ます」敬体。"
    ),
    (Lang.JA, Lang.EN): (
        "敬语转成英文对应的礼貌层级（please / could you / I would appreciate），"
        "不要逐字对应。\n"
        "日语的省略主语要按上下文补出，英文句子不能没有主语。"
    ),
}

_PLAIN_JA_NOTE = (
    "\n【风格】偏好平实自然的表达，不必强求敬体。"
)
_POLITE_JA_NOTE = (
    "\n【风格】使用礼貌的敬体表达（です・ます 体）。"
)


def direction_rule(src: Lang, tgt: Lang, ja_style: str = "polite") -> str:
    rule = DIRECTION_RULES.get((src, tgt), "")
    if not rule:
        rule = f"按{LBL(tgt)}的自然书面语翻译。"
    if tgt == Lang.JA:
        rule += _POLITE_JA_NOTE if ja_style == "polite" else _PLAIN_JA_NOTE
    return rule


def LBL(lang: Lang) -> str:  # noqa: N802 - 短名字，内部工具
    return LABEL.get(lang, str(lang))


def build_messages(req: TranslateRequest, ja_style: str = "polite") -> list[dict[str, str]]:
    system = _SYSTEM_SKELETON.format(
        target=LBL(req.target_lang),
        src=LBL(req.source_lang),
        tgt=LBL(req.target_lang),
        direction=direction_rule(req.source_lang, req.target_lang, ja_style),
    )
    if req.code_hint:
        system += _CODE_HINT
    if req.glossary:
        entries = "\n".join(f"- {k} → {v}" for k, v in req.glossary.items())
        system += _GLOSSARY_HEADER + "\n" + entries

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": req.text},
    ]


# --------------------------------------------------------------------------- #
# 后处理
# --------------------------------------------------------------------------- #

_PREAMBLE = re.compile(
    r"^\s*(译文|翻译|以下是译文|这是译文|翻译如下)\s*[:：]\s*",
    re.IGNORECASE,
)


def clean_translation(text: str) -> str:
    """剥掉模型偶尔夹带的解释性前缀和包裹引号。

    正常情况下提示词已经约束住了，这里只是兜底——不能因为模型多说
    一句「好的，以下是译文：」就把卡片搞脏。
    """
    if not text:
        return text
    out = text.strip()
    out = _PREAMBLE.sub("", out)
    # 整段被一对引号包住（且内部没有同样的引号）时剥掉
    for quote in ('"', "'", "「", "『"):
        closer = {"「": "」", "『": "』"}.get(quote, quote)
        if out.startswith(quote) and out.endswith(closer) and len(out) > 2:
            inner = out[1:-1]
            if quote not in inner and closer not in inner:
                out = inner
                break
    return out.strip()
