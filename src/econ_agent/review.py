"""同一份已核验结果的两项有界审阅，局部失败不抹去另一项输出。"""

import asyncio
import json
import math
import time
from typing import Any, Literal

from econ_agent.model_client import ModelBudgetExceeded, redact
from econ_agent.ports import ReviewModelClient
from econ_agent.schemas import (
    CallStatus,
    Contract,
    Identifier,
    ModelCall,
    ReviewArguments,
    ReviewReport,
    ReviewResponse,
    ReviewRole,
    RunStatus,
    ToolCall,
    ValidationRecord,
    canonical_json,
    utc_now,
)
from econ_agent.state import RunStore, StateConflict, new_id

ROLES: tuple[ReviewRole, ReviewRole] = ("design_consistency", "interpretation")
INSTRUCTIONS: dict[ReviewRole, str] = {
    "design_consistency": (
        "核对两项实际结果的变量、样本、估计方法、标准误和确认设计是否一致。"
        "检查确切结果及程序核验记录，说明能确认和不能确认的内容。"
        "不能把数值核验成功当作识别假设证明，不能改写设计或程序核验。"
    ),
    "interpretation": (
        "解释真实估计及不确定性，披露识别与外推限制，提出有结果依据的后续分析。"
        "不要编造第一阶段或其他未执行诊断，不把区间重叠作为正式差异检验。"
        "建议须引用输入结果 ID；改变设计的建议交由主循环询问研究者。"
    ),
}


class ReviewItem(Contract):
    role: ReviewRole
    status: Literal["succeeded", "failed", "cancelled", "unavailable"]
    model_call_id: Identifier | None = None
    output_artifact_id: Identifier | None = None
    report: ReviewReport | None = None
    error_category: str | None = None
    error_summary: str | None = None


class ReviewBatch(Contract):
    tool_call_id: Identifier
    design_id: Identifier
    result_artifact_ids: tuple[Identifier, ...]
    status: Literal["succeeded", "partial", "failed", "cancelled"]
    reviews: tuple[ReviewItem, ...]
    intervals_overlap: bool | None
    duration_seconds: float
    input_valid: bool = True
    explanation: str = "模型审阅供主循环判断，不改变程序核验、有效结果选择或人工决定"


class ResultReviewer:
    def __init__(
        self, store: RunStore, model: ReviewModelClient, *, timeout_seconds: float = 60
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("每项审阅超时须为有限正数")
        self.store, self.model = store, model
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(2)
        # 同一运行的批次按完整操作串行，批次内部仅两项独立模型 I/O 并发。
        self._batch_lock = asyncio.Lock()

    def _payload(self, arguments: ReviewArguments) -> dict[str, Any]:
        state = self.store.load_state()
        if state.status != RunStatus.RUNNING or state.current_design_id != arguments.design_id:
            raise StateConflict("只能审阅当前确认设计下的结果")
        if len(set(arguments.result_artifact_ids)) != len(arguments.result_artifact_ids):
            raise StateConflict("审阅输入不能重复结果 ID")
        selected = {r.result_id: r for r in self.store.selected_results()}
        if not set(arguments.result_artifact_ids) <= selected.keys():
            raise StateConflict(
                "审阅输入必须是当前已选用且通过核验的结果，不能引用历史或未提取数值"
            )
        results = []
        for identifier in arguments.result_artifact_ids:
            result = selected[identifier]
            attempt = self.store.attempt(result.attempt_id)
            assert attempt.validation_artifact_id
            validation = ValidationRecord.model_validate_json(
                self.store.read_artifact(attempt.validation_artifact_id)
            )
            results.append(
                {
                    "result": result.model_dump(mode="json"),
                    "specification": self.store.experiment(result.experiment_id).model_dump(
                        mode="json"
                    ),
                    "validation": validation.model_dump(mode="json"),
                }
            )
        return {
            "design_id": arguments.design_id,
            "result_artifact_ids": arguments.result_artifact_ids,
            "design": self.store.design(arguments.design_id).model_dump(mode="json"),
            "results": results,
            "说明": "以上为受核验约束的已保存真实结果；本请求不重新运行统计，不读取参考答案",
        }

    async def _one(
        self,
        role: ReviewRole,
        arguments: ReviewArguments,
        parent_id: str,
        payload: dict[str, Any],
        deadline: float,
    ) -> ReviewResponse:
        async with self._semaphore:
            remaining = min(self.timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                raise ModelBudgetExceeded("等待后已无审阅运行时间，未发送请求")
            context = json.dumps(
                redact(
                    {
                        **payload,
                        "role": role,
                        "职责": INSTRUCTIONS[role],
                        "report_schema": ReviewReport.model_json_schema(),
                    }
                ),
                ensure_ascii=False,
            )
            # 超时由统一模型适配器处理，避免双重超时把真实超时误记为外部取消。
            return await self.model.review(
                context=context,
                operation_id=new_id("operation"),
                parent_tool_call_id=parent_id,
                context_design_id=arguments.design_id,
                context_result_ids=arguments.result_artifact_ids,
                role=role,
                timeout_seconds=remaining,
            )

    def _item(
        self, role: ReviewRole, value: ReviewResponse | BaseException, parent_id: str
    ) -> ReviewItem:
        if isinstance(value, ReviewResponse):
            call = value.model_call
            item = ReviewItem(
                role=role,
                status="succeeded" if value.report else "failed",
                model_call_id=call.model_call_id,
                report=value.report,
                error_category=value.error.category if value.error else None,
                error_summary=value.error.message if value.error else None,
            )
        else:
            calls = [
                c
                for c in self.store.model_calls()
                if c.parent_tool_call_id == parent_id and c.purpose == "结果审阅：" + role
            ]
            failed_call = calls[-1] if calls else None
            category = (
                "cancelled"
                if isinstance(value, asyncio.CancelledError)
                else (
                    "timeout"
                    if isinstance(value, TimeoutError)
                    else "budget"
                    if isinstance(value, ModelBudgetExceeded)
                    else "internal"
                )
            )
            item = ReviewItem(
                role=role,
                status="cancelled"
                if category == "cancelled"
                else "failed"
                if failed_call
                else "unavailable",
                model_call_id=failed_call.model_call_id if failed_call else None,
                error_category=category,
                error_summary={
                    "cancelled": "审阅已取消，服务端处理及用量未知",
                    "timeout": "本项审阅超时，另一项输出独立保留",
                    "budget": "本项审阅缺少请求或时间预算，未伪造请求",
                    "internal": "本项审阅发生内部错误，请核对逐次调用及归档记录",
                }[category],
            )
        artifact = self.store.archive_bytes(
            f"calls/tool/{parent_id}/{role}.json",
            canonical_json(item).encode(),
            kind="tool_response",
            tool_call_id=parent_id,
            model_call_id=item.model_call_id,
        )
        return item.model_copy(update={"output_artifact_id": artifact.artifact_id})

    def _overlap(self, items: tuple[ReviewItem, ...]) -> bool | None:
        calls: list[ModelCall] = [
            self.store.model_call(i.model_call_id) for i in items if i.model_call_id
        ]
        if len(calls) != 2 or any(c.started_at is None or c.ended_at is None for c in calls):
            return None
        # UTC 规范时间戳来自同一进程；不把并发请求耗时相加作为批次耗时。
        return max(c.started_at or "" for c in calls) < min(c.ended_at or "" for c in calls)

    async def review(
        self,
        arguments: ReviewArguments,
        *,
        source_model_call_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ReviewBatch:
        allowance = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(allowance) or allowance <= 0:
            raise ModelBudgetExceeded("没有审阅运行时间，请求未发送")
        deadline = time.monotonic() + allowance
        async with self._batch_lock:
            payload = self._payload(arguments)
            if source_model_call_id:
                source = self.store.model_call(source_model_call_id)
                if not set(arguments.result_artifact_ids) <= set(source.context_result_ids):
                    raise StateConflict("审阅输入须来自发起动作的本轮模型请求")
            call = ToolCall(
                tool_call_id=new_id("tool"),
                run_id=self.store.run_id,
                operation_id=new_id("operation"),
                source_model_call_id=source_model_call_id,
                tool_name="review_results",
                arguments_status="pass",
            )
            self.store.record_tool_call(call)
            artifact = self.store.archive_bytes(
                f"calls/tool/{call.tool_call_id}/arguments.json",
                canonical_json(arguments).encode(),
                kind="tool_arguments",
                tool_call_id=call.tool_call_id,
            )
            call = call.model_copy(update={"arguments_artifact_id": artifact.artifact_id})
            self.store.update_tool_call(call, expected=CallStatus.PENDING)
            call = call.model_copy(update={"status": CallStatus.RUNNING, "started_at": utc_now()})
            self.store.update_tool_call(call, expected=CallStatus.PENDING)
            start = time.monotonic()
            cancelled = False
            available = self.store.load_state().budget.max_model_calls - len(
                self.store.model_calls()
            )
            if available < 2:
                values: list[ReviewResponse | BaseException] = [
                    ModelBudgetExceeded("两项审阅需要至少两个剩余请求预算") for _ in ROLES
                ]
            else:
                tasks = [
                    asyncio.create_task(
                        self._one(role, arguments, call.tool_call_id, payload, deadline)
                    )
                    for role in ROLES
                ]
                try:
                    values = list(await asyncio.gather(*tasks, return_exceptions=True))
                except asyncio.CancelledError:
                    cancelled = True
                    for task in tasks:
                        task.cancel()
                    values = list(await asyncio.gather(*tasks, return_exceptions=True))
            input_valid = True
            try:
                self._payload(arguments)
            except StateConflict:
                input_valid = False
            items = tuple(
                self._item(role, value, call.tool_call_id)
                for role, value in zip(ROLES, values, strict=True)
            )
            successes = sum(item.status == "succeeded" for item in items)
            status: Literal["succeeded", "partial", "failed", "cancelled"] = (
                "cancelled"
                if cancelled
                else "failed"
                if not input_valid
                else "succeeded"
                if successes == 2 and input_valid
                else "partial"
                if successes == 1
                else "failed"
            )
            batch = ReviewBatch(
                tool_call_id=call.tool_call_id,
                design_id=arguments.design_id,
                result_artifact_ids=arguments.result_artifact_ids,
                status=status,
                reviews=items,
                intervals_overlap=self._overlap(items),
                duration_seconds=time.monotonic() - start,
                input_valid=input_valid,
            )
            response = self.store.archive_bytes(
                f"calls/tool/{call.tool_call_id}/response.json",
                canonical_json(batch).encode(),
                kind="tool_response",
                tool_call_id=call.tool_call_id,
            )
            self.store.update_tool_call(
                call.model_copy(
                    update={
                        "status": CallStatus.CANCELLED
                        if cancelled
                        else CallStatus.SUCCEEDED
                        if successes == 2 and input_valid
                        else CallStatus.FAILED,
                        "ended_at": utc_now(),
                        "duration_seconds": batch.duration_seconds,
                        "response_artifact_id": response.artifact_id,
                        "error_category": "cancelled"
                        if cancelled
                        else "stale_inputs"
                        if not input_valid
                        else "partial_review"
                        if successes == 1
                        else "review_failed"
                        if successes == 0
                        else None,
                        "error_summary": "审阅期间输入失效，意见不能作为当前有效结果使用"
                        if not input_valid
                        else "两项审阅未全部完成，已保留独立输出"
                        if successes < 2
                        else None,
                    }
                ),
                expected=CallStatus.RUNNING,
            )
            unresolved = [
                c
                for c in self.store.model_calls()
                if c.parent_tool_call_id == call.tool_call_id
                and c.status in {CallStatus.PENDING, CallStatus.RUNNING, CallStatus.UNKNOWN}
            ]
            if (
                unresolved or not input_valid
            ) and self.store.load_state().status == RunStatus.RUNNING:
                self.store.transition_run(
                    RunStatus.PAUSED,
                    reason="审阅期间输入失效，请核对结果及确认设计"
                    if not input_valid
                    else "审阅调用状态未终结，请先核对恢复记录",
                )
            if cancelled:
                raise asyncio.CancelledError
            return batch
