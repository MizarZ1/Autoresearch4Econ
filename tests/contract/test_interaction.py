"""人工修改与恢复的离线契约测试；HTTP 和 MCP 使用明确标注的替身。"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from econ_agent.interaction import apply_feedback
from econ_agent.processes import WorkerIdentity
from econ_agent.recovery import reconcile_interrupted
from econ_agent.schemas import CallStatus, HumanAnswer, HumanRequest, ModelCall, RunStatus, ToolCall
from econ_agent.session import conduct, status_summary, stop_snapshot
from econ_agent.state import RunStore, StateConflict, new_id
from tests.contract.test_state import confirmed, design_content, experiment, pending_attempt
from tests.contract.test_state import store as store
from tests.unit.test_model_client import SequenceTransport, client, envelope


def waiting(store: RunStore) -> HumanRequest:
    if store.load_state().status == RunStatus.NEW:
        store.transition_run(RunStatus.RUNNING)
    design = store.create_design(design_content(), change_reason="测试初次确认")
    return store.request_human(design.design_id, "请确认测试设计")


def revision() -> dict[str, Any]:
    return {
        "decision": "revise",
        "explanation": "按测试意见改变样本，等待再次确认",
        "revised_design": design_content()
        .model_copy(update={"sample_rule": "x>1"})
        .model_dump(mode="json"),
    }


async def test_natural_feedback_creates_unconfirmed_version_and_preserves_text(
    store: RunStore,
) -> None:
    request = waiting(store)
    transport = SequenceTransport([envelope(revision())])
    result = await apply_feedback(
        store,
        "请把样本改为 x>1",
        request_id=request.request_id,
        design_sha256=request.target_design_sha256,
        model=client(store, transport),
    )
    assert result["status"] == "waiting_confirmation"
    assert len(store.designs()) == 2 and store.load_state().current_design_id is None
    assert store.design_confirmation(store.designs()[-1].design_id) == "PROPOSED"
    saved = store.human_request(request.request_id)
    assert saved.answer and saved.answer.explanation == "请把样本改为 x>1"
    assert store.model_calls()[0].purpose == "解析研究者反馈"
    assert store.tool_calls() == ()
    duplicate = await apply_feedback(
        store,
        "请把样本改为 x>1",
        request_id=request.request_id,
        design_sha256=request.target_design_sha256,
        model=client(store, transport),
    )
    assert duplicate["status"] == "idempotent" and len(store.designs()) == 2
    with pytest.raises(StateConflict):
        await apply_feedback(
            store,
            "/accept",
            request_id=request.request_id,
            design_sha256=request.target_design_sha256,
        )
    fresh = store.human_request(store.load_state().pending_request_ids[0])
    await apply_feedback(
        store, "/accept", request_id=fresh.request_id, design_sha256=fresh.target_design_sha256
    )
    assert store.load_state().current_design_id == fresh.target_design_id
    assert len(transport.requests) == 1


def test_revision_transaction_rolls_back_then_is_idempotent(
    store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = waiting(store)
    content = design_content().model_copy(update={"sample_rule": "x>1"})
    answer = HumanAnswer(decision="revise", explanation="测试原始意见")
    original = store._request_human_unlocked

    def fail(*args: Any, **kwargs: Any) -> HumanRequest:
        raise RuntimeError("测试事务末尾中断")

    monkeypatch.setattr(store, "_request_human_unlocked", fail)
    with pytest.raises(RuntimeError):
        store.revise_answer(
            request.request_id,
            design_sha256=request.target_design_sha256,
            answer=answer,
            content=content,
        )
    assert store.human_request(request.request_id).status == "PENDING"
    assert len(store.designs()) == 1
    monkeypatch.setattr(store, "_request_human_unlocked", original)
    first = store.revise_answer(
        request.request_id,
        design_sha256=request.target_design_sha256,
        answer=answer,
        content=content,
    )
    duplicate = store.revise_answer(
        request.request_id,
        design_sha256=request.target_design_sha256,
        answer=answer,
        content=content,
    )
    assert first == duplicate and len(store.designs()) == 2


@pytest.mark.parametrize("text", ["/accept", "/pause", "/reject 测试拒绝", "/stop"])
async def test_control_commands_do_not_need_model(store: RunStore, text: str) -> None:
    request = waiting(store)
    result = await apply_feedback(
        store, text, request_id=request.request_id, design_sha256=request.target_design_sha256
    )
    assert result["status"] == "applied" and not store.model_calls()
    if text == "/pause":
        assert store.load_state().pending_request_ids == (request.request_id,)
    elif text == "/stop":
        assert store.load_state().status == RunStatus.STOPPED
    elif text.startswith("/reject"):
        assert store.design_confirmation(request.target_design_id) == "REJECTED"
    else:
        duplicate = await apply_feedback(
            store, text, request_id=request.request_id, design_sha256=request.target_design_sha256
        )
        assert duplicate["status"] == "idempotent"


@pytest.mark.parametrize(
    "interpretation",
    [
        {"decision": "accept", "explanation": "错误解释的测试夹具"},
        {"decision": "clarify", "explanation": "需具体化", "clarification": "保留哪些观测？"},
    ],
)
async def test_revision_never_silently_accepts(
    store: RunStore,
    interpretation: dict[str, Any],
) -> None:
    request = waiting(store)
    result = await apply_feedback(
        store,
        "/revise 改一下样本",
        request_id=request.request_id,
        design_sha256=request.target_design_sha256,
        model=client(store, SequenceTransport([envelope(interpretation)])),
    )
    assert result["status"] == "pending"
    assert store.human_request(request.request_id).status == "PENDING"
    assert len(store.designs()) == 1 and store.load_state().current_design_id is None


async def test_hash_mismatch_does_not_send_or_archive_input(store: RunStore) -> None:
    request = waiting(store)
    transport = SequenceTransport([])
    before = store.artifacts()
    with pytest.raises(StateConflict):
        await apply_feedback(
            store,
            "修改样本",
            request_id=request.request_id,
            design_sha256="0" * 64,
            model=client(store, transport),
        )
    assert store.artifacts() == before and not transport.requests


async def test_pending_question_survives_reopen_without_new_call(store: RunStore) -> None:
    request = waiting(store)
    directory = store.directory
    store.close()
    with RunStore.open(directory) as resumed:
        assert await conduct(resumed, directory.parent.parent, resumed=True, no_input=True) == 0
        assert resumed.human_request(request.request_id) == request
        assert resumed.load_state().analysis_backend == "stata"
        assert not resumed.model_calls() and not resumed.tool_calls()


async def test_completed_loop_resume_does_not_dispatch(store: RunStore) -> None:
    confirmed(store)
    store.transition_run(RunStatus.PAUSED, reason="研究循环已结束：测试完成")
    assert await conduct(store, store.directory.parent.parent, resumed=True, no_input=True) == 0
    assert not store.model_calls() and not store.tool_calls()


async def test_signal_cancellation_waits_for_research_cleanup(
    store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def running(*args: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr("econ_agent.session.run_research", running)
    task = asyncio.create_task(conduct(store, store.directory.parent.parent, no_input=True))
    await started.wait()
    task.cancel()
    assert await task == 130 and cleaned.is_set()
    assert store.load_state().status == RunStatus.PAUSED


class Controller:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.identities: list[WorkerIdentity] = []

    async def stop(self, expected: WorkerIdentity) -> str:
        self.identities.append(expected)
        return self.outcome


@pytest.mark.parametrize("outcome", ["stopped", "gone", "unknown"])
async def test_recovery_verifies_owned_identity_and_preserves_unknown_usage(
    store: RunStore,
    outcome: str,
) -> None:
    spec = experiment(store)
    identifier = pending_attempt(store, spec)
    attempt = store.attempt(identifier)
    code = store.archive_bytes(
        "experiments/test/analysis.do",
        b"display 1\n",
        kind="code",
        design_id=spec.design_id,
        attempt_id=identifier,
        tool_call_id=attempt.tool_call_id,
    )
    store.start_attempt(
        identifier,
        code.artifact_id,
        service_instance_id="old_service",
        session_id="owned_session",
        backend_task_id="old_task",
    )
    identity = WorkerIdentity(pid=12345, created_at=100, executable="/测试专用进程")
    store.archive_bytes(
        f"calls/workers/{attempt.tool_call_id}/identity.json",
        json.dumps(
            {
                "run_id": store.run_id,
                "tool_call_id": attempt.tool_call_id,
                "attempt_id": identifier,
                "service_instance_id": "old_service",
                "session_id": "owned_session",
                "identity": identity.model_dump(),
            }
        ).encode(),
        kind="tool_response",
        tool_call_id=attempt.tool_call_id,
    )
    model = ModelCall(
        model_call_id=new_id("model"),
        run_id=store.run_id,
        operation_id=new_id("op"),
        requested_model="测试",
        provider="https://example.invalid",
        prompt_version="测试版本",
        config_version="测试配置",
        purpose="测试硬中断",
    )
    store.record_model_call(model)
    store.update_model_call(
        model.model_copy(update={"status": CallStatus.RUNNING}), expected=CallStatus.PENDING
    )
    directory = store.directory
    store.close()
    with RunStore.open(directory) as recovered:
        controller = Controller(outcome)
        issues = await reconcile_interrupted(recovered, controller)
        assert controller.identities == [identity]
        assert bool(issues) == (outcome == "unknown")
        assert recovered.attempt(identifier).termination_confirmed == (outcome != "unknown")
        saved = recovered.model_call(model.model_call_id)
        assert saved.total_tokens is None
        assert saved.status == (CallStatus.UNKNOWN if issues else CallStatus.CANCELLED)
        if not issues:
            assert not await reconcile_interrupted(recovered, controller)
            assert controller.identities == [identity]


async def test_recovery_missing_identity_blocks_new_execution(store: RunStore) -> None:
    from tests.contract.test_state import started_attempt

    identifier = started_attempt(store, experiment(store))
    directory = store.directory
    store.close()
    with RunStore.open(directory) as recovered:
        controller = Controller("stopped")
        assert await reconcile_interrupted(recovered, controller)
        assert not controller.identities
        assert not recovered.attempt(identifier).termination_confirmed
        assert recovered.load_state().status == RunStatus.PAUSED


@pytest.mark.parametrize("name", ["capabilities", "inspect_data", "get_method_help"])
@pytest.mark.parametrize("dispatched", [True, False])
async def test_query_recovery_distinguishes_dispatch_intent(
    store: RunStore, name: str, dispatched: bool
) -> None:
    store.transition_run(RunStatus.RUNNING)
    call = ToolCall(
        tool_call_id=new_id("tool"), run_id=store.run_id, operation_id=new_id("op"), tool_name=name
    )
    store.record_tool_call(call)
    if dispatched:
        store.archive_bytes(
            f"calls/workers/{call.tool_call_id}/intent.json",
            b"{}",
            kind="tool_response",
            tool_call_id=call.tool_call_id,
        )
    directory = store.directory
    store.close()
    with RunStore.open(directory) as reopened:
        controller = Controller("stopped")
        issues = await reconcile_interrupted(reopened, controller)
        assert bool(issues) == dispatched
        assert not controller.identities
        assert reopened.tool_call(call.tool_call_id).status == (
            CallStatus.UNKNOWN if dispatched else CallStatus.CANCELLED
        )
        if not dispatched:
            assert not await reconcile_interrupted(reopened, controller)


async def test_feedback_respects_exhausted_time_and_keeps_pending_request(store: RunStore) -> None:
    request = waiting(store)
    store.archive_bytes(
        "calls/harness/test.json",
        json.dumps(
            {
                "elapsed_seconds": store.load_state().budget.max_runtime_seconds + 1,
            }
        ).encode(),
        kind="tool_response",
    )
    transport = SequenceTransport([])
    result = await apply_feedback(
        store,
        "请解释一下",
        request_id=request.request_id,
        design_sha256=request.target_design_sha256,
        model=client(store, transport),
    )
    assert result["status"] == "pending" and not transport.requests
    assert store.human_request(request.request_id).status == "PENDING"


async def test_feedback_processing_time_is_counted_before_next_feedback(store: RunStore) -> None:
    from econ_agent.reporting import elapsed_seconds

    request = waiting(store)
    store.archive_bytes("calls/harness/test.json", b'{"elapsed_seconds": 10}', kind="tool_response")
    store.archive_bytes(
        "calls/human/test/interpretation.json", b'{"processing_seconds": 20}', kind="tool_response"
    )
    assert elapsed_seconds(store) == 30
    transport = SequenceTransport(
        [envelope({"decision": "clarify", "explanation": "测试澄清", "clarification": "请补充"})]
    )
    await apply_feedback(
        store,
        "请解释一下",
        request_id=request.request_id,
        design_sha256=request.target_design_sha256,
        model=client(store, transport),
    )
    assert elapsed_seconds(store) >= 30


def test_stop_snapshot_excludes_history_and_requests(store: RunStore, tmp_path: Path) -> None:
    request = waiting(store)
    store.archive_bytes("calls/private.json", b'{"secret_fixture":true}', kind="model_request")
    store.apply_answer(
        request.request_id,
        design_sha256=request.target_design_sha256,
        answer=HumanAnswer(decision="stop", explanation="测试快照"),
    )
    exported = stop_snapshot(store, tmp_path)
    assert not (exported / "calls").exists()
    assert status_summary(store)["run"]["status"] == "STOPPED"
    assert json.loads((exported / "results.json").read_text()) == []


def test_confirming_new_design_excludes_old_result_and_report_from_snapshot(
    store: RunStore,
    tmp_path: Path,
) -> None:
    from tests.contract.test_state import checks, successful_result

    spec = experiment(store)
    result = successful_result(store, spec)
    store.save_validation(result.artifact_id, checks(result.artifact_id))
    store.select_result(spec.experiment_id, result.artifact_id)
    store.archive_bytes("reports/old.md", b"historical-report-only", kind="report")
    request = store.request_human(spec.design_id, "测试修改样本", kind="research_question")
    fresh = store.revise_answer(
        request.request_id,
        design_sha256=request.target_design_sha256,
        answer=HumanAnswer(decision="revise", explanation="仅x>1"),
        content=design_content().model_copy(update={"sample_rule": "x>1"}),
    )
    store.apply_answer(
        fresh.request_id,
        design_sha256=fresh.target_design_sha256,
        answer=HumanAnswer(decision="accept", explanation="测试确认新版本"),
    )
    snapshot = stop_snapshot(store, tmp_path)
    assert store.selected_results() == ()
    assert store.attempts(spec.experiment_id)[0].result_artifact_id == result.artifact_id
    assert json.loads((snapshot / "results.json").read_text()) == []
    assert all(b"historical-report-only" not in p.read_bytes() for p in snapshot.iterdir())


async def test_pause_while_reading_terminal_keeps_question_and_removes_reader(
    store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = waiting(store)
    read_fd, write_fd = os.pipe()

    class Input:
        def fileno(self) -> int:
            return read_fd

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Input())
    entered = asyncio.Event()
    original = asyncio.get_running_loop().add_reader

    def add_reader(fd: int, callback: Any, *args: Any) -> None:
        original(fd, callback, *args)
        entered.set()

    monkeypatch.setattr(asyncio.get_running_loop(), "add_reader", add_reader)
    try:
        task = asyncio.create_task(conduct(store, store.directory.parent.parent))
        await entered.wait()
        task.cancel()
        assert await task == 130
        assert store.load_state().status == RunStatus.PAUSED
        assert store.human_request(request.request_id).status == "PENDING"
        assert not asyncio.get_running_loop().remove_reader(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)
