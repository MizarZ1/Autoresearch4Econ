"""持久化研究者输入、解释自然语言及创建待确认设计；不执行统计。"""

import json
import time
from typing import Any, Literal

from econ_agent.model_client import PersistentModelClient, redact
from econ_agent.reporting import elapsed_seconds
from econ_agent.schemas import HumanAnswer, HumanFeedback, RunStatus
from econ_agent.state import RunStore, StateConflict, new_id


async def apply_feedback(
    store: RunStore,
    text: str,
    *,
    request_id: str,
    design_sha256: str,
    model: PersistentModelClient | None = None,
) -> dict[str, Any]:
    request = store.human_request(request_id)
    if request.target_design_sha256 != design_sha256:
        raise StateConflict("输入的设计 hash 与等待的问题不符，未应用答复")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("答复不能为空")
    command, _, reason = cleaned.partition(" ")
    direct: dict[str, Literal["accept", "reject", "pause", "stop"]] = {
        "/accept": "accept",
        "/reject": "reject",
        "/pause": "pause",
        "/stop": "stop",
    }
    if command.startswith("/") and command not in {*direct, "/revise"}:
        raise ValueError("支持 /accept、/revise <说明>、/reject <理由>、/pause 和 /stop")
    if command in {"/reject", "/revise"} and not reason.strip():
        raise ValueError("修改或拒绝需要说明理由")
    if command == "/accept" and reason:
        raise ValueError("同意后不能附带修改；请用 /revise 保存修改并确认新版本")
    if request.status == "APPLIED":
        if request.answer and request.answer.explanation == redact(cleaned):
            return {"status": "idempotent", "说明": "该原始答复已应用，未重复请求或执行"}
        answer = (
            HumanAnswer(decision=direct[command], explanation=redact(cleaned))
            if command in direct
            else None
        )
        if answer is not None and request.answer == answer:
            store.apply_answer(request_id, design_sha256=design_sha256, answer=answer)
            return {"status": "idempotent", "说明": "该答复已应用，未重复请求或执行"}
        raise StateConflict("该请求已应用，不能把新意见写入旧请求")
    state = store.load_state()
    if request.status != "PENDING" or request_id not in state.pending_request_ids:
        raise StateConflict("答复请求已过期或不属于当前待答问题")
    if state.status == RunStatus.PAUSED:
        store.transition_run(RunStatus.RUNNING)
    if store.load_state().status != RunStatus.WAITING_HUMAN:
        raise StateConflict("当前会话没有可答复的等待状态")
    identifier = new_id("input")
    prefix = f"calls/human/{identifier}"
    store.archive_bytes(
        prefix + "/input.json",
        json.dumps(
            redact(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "design_sha256": design_sha256,
                    "text": text,
                }
            ),
            ensure_ascii=False,
        ).encode(),
        kind="tool_arguments",
        design_id=request.target_design_id,
    )
    if command in direct:
        store.apply_answer(
            request_id,
            design_sha256=design_sha256,
            answer=HumanAnswer(
                decision=direct[command],
                explanation=redact(cleaned),
            ),
        )
        return {"status": "applied", "decision": direct[command]}
    if model is None:
        raise ValueError("自然语言或 /revise 需要现有模型配置；待答问题已保留")
    design = store.design(request.target_design_id)
    results = tuple(r.result_id for r in store.selected_results() if r.result_id)
    context = json.dumps(
        redact(
            {
                "request": request.model_dump(mode="json"),
                "design": design.model_dump(mode="json"),
                "researcher_text": reason if command == "/revise" else cleaned,
                "explicit_revision": command == "/revise",
                "schema": HumanFeedback.model_json_schema(),
            }
        ),
        ensure_ascii=False,
    )
    remaining = store.load_state().budget.max_runtime_seconds - elapsed_seconds(store)
    if remaining <= 0:
        return {"status": "pending", "说明": "总运行时间预算已耗尽；原始输入和待答问题已保留"}
    if len(store.model_calls()) >= store.load_state().budget.max_model_calls - 1:
        return {"status": "pending", "说明": "模型余量须留给论文写作；可用显式命令处理待答问题"}
    started = time.monotonic()
    call, interpreted, error = await model.interpret_feedback(
        context=context,
        operation_id=new_id("operation"),
        context_design_id=design.design_id,
        context_result_ids=results,
        timeout_seconds=remaining,
    )
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "model_call_id": call.model_call_id,
        "interpretation": interpreted.model_dump(mode="json") if interpreted else None,
        "error": error.model_dump(mode="json") if error else None,
        "processing_seconds": time.monotonic() - started,
    }
    store.archive_bytes(
        prefix + "/interpretation.json",
        json.dumps(redact(payload), ensure_ascii=False).encode(),
        kind="tool_response",
        design_id=design.design_id,
        model_call_id=call.model_call_id,
    )
    if interpreted is None:
        return {"status": "pending", "说明": error.message if error else "解释失败，待答问题未改变"}
    if interpreted.decision == "clarify":
        return {"status": "pending", "说明": interpreted.clarification}
    if command == "/revise" and interpreted.decision != "revise":
        return {"status": "pending", "说明": "修改请求未得到明确的新设计，请补充具体变化"}
    answer = HumanAnswer(decision=interpreted.decision, explanation=redact(cleaned))
    if interpreted.decision == "revise":
        assert interpreted.revised_design
        next_request = store.revise_answer(
            request_id,
            design_sha256=design_sha256,
            answer=answer,
            content=interpreted.revised_design,
        )
        return {
            "status": "waiting_confirmation",
            "request_id": next_request.request_id,
            "说明": interpreted.explanation,
        }
    store.apply_answer(request_id, design_sha256=design_sha256, answer=answer)
    return {"status": "applied", "decision": interpreted.decision}
