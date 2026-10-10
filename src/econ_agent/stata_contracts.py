"""Stata 后端的代码、数据保护和明确错误契约。"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from econ_agent.schemas import (
    Contract,
    ExperimentResult,
    Identifier,
    Nonempty,
    RegressionSpecification,
    Sha256,
    SummarySpecification,
)
from econ_agent.schemas import IVSpecification as IVSpecification


class StataProgram(Contract):
    """模型生成的主体代码；加载副本、记录环境及归档由执行器负责。"""

    code: Nonempty
    iv: IVSpecification | None = None
    regression: RegressionSpecification | None = None
    summary: SummarySpecification | None = None

    @model_validator(mode="after")
    def one_extraction(self) -> Self:
        if sum(v is not None for v in (self.iv, self.regression, self.summary)) > 1:
            raise ValueError("一次分析只能声明一种提取规则")
        return self


class PreparedInput(Contract):
    """本地独立备份和 run 内不可变快照，不向模型暴露备份路径。"""

    input_artifact_id: Identifier
    original_path: Path
    backup_path: Path
    original_sha256: Sha256
    byte_size: int = Field(ge=0)
    file_type: Literal["dta", "csv"]


class ToolFailure(Contract):
    tool_call_id: Identifier
    category: Literal[
        "configuration",
        "arguments",
        "stata",
        "timeout",
        "cancelled",
        "transport",
        "integrity",
        "extraction",
        "termination",
        "missing_command",
    ]
    message: Nonempty
    stata_return_code: int | None = None
    diagnostic_artifact_ids: tuple[Identifier, ...] = ()
    retryable: bool = False


class AnalysisOutcome(Contract):
    """成功、未规范化及失败共享同一结果类型；错误不伪装成计算结果。"""

    result: ExperimentResult
    error: ToolFailure | None = None
    validation_artifact_id: Identifier | None = None


class BackendError(RuntimeError):
    """检查及帮助等非分析工具的错误也携带可追溯的结构化反馈。"""

    def __init__(self, failure: ToolFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
