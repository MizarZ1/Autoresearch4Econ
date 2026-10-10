"""论文写作契约；经验数字只能通过受核验的事实占位符进入正文。"""

from typing import Literal

from pydantic import Field

from econ_agent.schemas import Contract, Identifier, Nonempty

SECTIONS = (
    "研究问题",
    "背景与假设",
    "数据与变量",
    "实证设计",
    "主要结果",
    "补充分析",
    "讨论与局限",
    "参考材料",
)


class PaperClaim(Contract):
    section: Literal["主要结果", "补充分析"]
    text: Nonempty
    result_ids: tuple[Identifier, ...] = Field(min_length=1)


class PaperDraft(Contract):
    title: Nonempty
    sections: dict[str, str]
    claims: tuple[PaperClaim, ...] = Field(min_length=1)
    material_artifact_ids: tuple[Identifier, ...] = ()
