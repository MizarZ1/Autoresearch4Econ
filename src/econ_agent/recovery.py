"""在重开会话后核对旧工作进程，不向新 MCP 服务发送旧实例的任务 ID。"""

import json
from typing import Protocol

from econ_agent.processes import LocalProcessController, WorkerIdentity
from econ_agent.schemas import CallStatus, ExecutionStatus
from econ_agent.state import RunStore, new_id


class ProcessController(Protocol):
    async def stop(self, expected: WorkerIdentity) -> str: ...


async def reconcile_interrupted(
    store: RunStore,
    controller: ProcessController | None = None,
) -> tuple[str, ...]:
    controller = controller or LocalProcessController()
    issues: list[str] = list(store.recovery_report.issues)
    attempts = {
        a.tool_call_id: a for s in store.experiments() for a in store.attempts(s.experiment_id)
    }
    records = {
        a.tool_call_id: a
        for a in store.artifacts(kind="tool_response")
        if a.relative_path.startswith("calls/workers/")
        and a.relative_path.endswith("/identity.json")
    }
    intents = {
        a.tool_call_id
        for a in store.artifacts(kind="tool_response")
        if a.relative_path.startswith("calls/workers/") and a.relative_path.endswith("/intent.json")
    }
    for call in store.tool_calls():
        attempt = attempts.get(call.tool_call_id)
        needs_worker = (
            attempt is not None
            and attempt.execution_status == ExecutionStatus.INTERRUPTED
            and not attempt.termination_confirmed
        ) or (
            call.status == CallStatus.UNKNOWN
            and not (attempt and attempt.termination_confirmed)
            and call.error_category != "termination_confirmed"
            and call.tool_name
            in {"run_analysis", "capabilities", "inspect_data", "get_method_help"}
        )
        if needs_worker:
            artifact = records.get(call.tool_call_id)
            if artifact is None:
                # 从未登记派发意图的待运行分析可以确认未启动，其他情况保持暂停。
                if (
                    attempt
                    and not attempt.backend_task_id
                    and call.tool_call_id not in intents
                    and attempt.executor_id is None
                ):
                    store.confirm_interrupted_stopped(attempt.attempt_id)
                else:
                    issues.append("遗留统计工具缺少可核对的工作进程身份：" + call.tool_call_id)
                    continue
            else:
                saved = json.loads(store.read_artifact(artifact.artifact_id))
                if (
                    saved["run_id"] != store.run_id
                    or saved["tool_call_id"] != call.tool_call_id
                    or not saved.get("identity")
                ):
                    issues.append("遗留进程身份无法验证：" + call.tool_call_id)
                    continue
                if attempt and (
                    saved["attempt_id"] != attempt.attempt_id
                    or saved["service_instance_id"] != attempt.service_instance_id
                    or saved["session_id"] != attempt.session_id
                ):
                    issues.append("遗留进程与尝试来源不匹配：" + call.tool_call_id)
                    continue
                outcome = await controller.stop(WorkerIdentity.model_validate(saved["identity"]))
                store.archive_bytes(
                    f"calls/recovery/{call.tool_call_id}/{new_id('check')}.json",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "source_artifact_id": artifact.artifact_id,
                            "outcome": outcome,
                        }
                    ).encode(),
                    kind="tool_response",
                    tool_call_id=call.tool_call_id,
                )
                if outcome not in {"gone", "stopped"}:
                    issues.append("旧专用工作进程未确认停止：" + call.tool_call_id)
                    continue
                if attempt and not attempt.termination_confirmed:
                    store.confirm_interrupted_stopped(attempt.attempt_id)
                elif call.error_category == "termination":
                    store.confirm_query_stopped(call.tool_call_id)
        if call.status == CallStatus.UNKNOWN:
            store.close_interrupted_call(call.tool_call_id, model=False)
    if not issues:
        for model_call in store.model_calls():
            if model_call.status == CallStatus.UNKNOWN:
                store.close_interrupted_call(model_call.model_call_id, model=True)
    return tuple(issues)
