"""核心流程使用的小接口；不在这里初始化模型、MCP 或数据库连接。"""

from typing import Protocol

from econ_agent.schemas import (
    ExperimentResult,
    ExperimentSpec,
    HumanAnswer,
    MethodCapability,
    ModelCall,
    ModelResponse,
    RunState,
    ToolCall,
)
from econ_agent.stata_contracts import AnalysisOutcome, StataProgram


class ModelClient(Protocol):
    """中文交互，逐次观察实际请求；重试记录和用量均由适配器提供。"""

    async def decide(
        self,
        *,
        context: str,
        operation_id: str,
        context_design_id: str | None = None,
        context_result_ids: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
    ) -> ModelResponse: ...


class AnalysisBackend(Protocol):
    """执行、提取与归档作为完整操作；取消必须确认底层任务结束。"""

    async def capabilities(self) -> tuple[MethodCapability, ...]: ...

    async def inspect_data(
        self, input_artifact_id: str, *, source_model_call_id: str | None = None
    ) -> str: ...

    async def get_method_help(
        self, method: str, *, source_model_call_id: str | None = None
    ) -> str: ...

    async def run_analysis(
        self, spec: ExperimentSpec, *, attempt_id: str, program: StataProgram
    ) -> AnalysisOutcome: ...

    async def cancel(self, attempt_id: str) -> bool: ...


class RunStorePort(Protocol):
    """存储适配器集中管理状态、人工决定、调用与有效结果选择。"""

    def load_state(self) -> RunState: ...

    def record_model_call(self, call: ModelCall) -> None: ...

    def record_tool_call(self, call: ToolCall) -> None: ...

    def apply_answer(
        self, request_id: str, *, design_sha256: str, answer: HumanAnswer
    ) -> RunState: ...

    def select_result(self, experiment_id: str, result_id: str) -> None: ...

    def selected_results(self) -> tuple[ExperimentResult, ...]: ...
