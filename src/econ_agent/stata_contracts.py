"""Stata 后端的代码、数据保护和明确错误契约。"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from econ_agent.schemas import Contract, ExperimentResult, Identifier, Nonempty, Sha256

Variable = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")]


class IVSpecification(Contract):
    """当前已实现提取规则的 2SLS 口径；其他命令仍走通用执行。"""

    outcome: Variable
    endogenous: Variable
    instruments: tuple[Variable, ...] = Field(min_length=1)
    controls: tuple[Variable, ...] = ()
    sample_expression: Nonempty
    covariance: Literal["unadjusted"] = "unadjusted"
    small_sample: Literal[True] = True

    def command(self) -> str:
        controls = " ".join(self.controls)
        instruments = " ".join(self.instruments)
        return (
            f"ivregress 2sls {self.outcome} {controls} "
            f"({self.endogenous}={instruments}) if {self.sample_expression}, small"
        )

    def variables(self) -> tuple[str, ...]:
        return (self.outcome, self.endogenous, *self.instruments, *self.controls)


class StataProgram(Contract):
    """模型生成的主体代码；加载副本、记录环境及归档由执行器负责。"""

    code: Nonempty
    iv: IVSpecification | None = None


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
