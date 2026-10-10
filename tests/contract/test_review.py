"""用异步屏障替代 HTTP 边界，检查生产审阅、持久化和主循环反馈。"""

import asyncio
import json
from typing import Any

import pytest

from econ_agent.config import ModelConfig
from econ_agent.harness import ResearchLoop, tool_descriptions
from econ_agent.model_client import PersistentModelClient
from econ_agent.review import ROLES, ResultReviewer
from econ_agent.schemas import CallStatus, ParseStatus, ReviewArguments, RunStatus
from econ_agent.state import RunStore, StateConflict, new_id
from tests.contract.test_stata_backend import BackendFixture, attempt
from tests.contract.test_stata_backend import setup_backend as setup_backend
from tests.unit.test_model_client import FINISH, SequenceTransport, envelope


def report(context: dict[str, Any]) -> dict[str, Any]:
    identifiers = [entry["result"]["result_id"] for entry in context["results"]]
    return {
        "role": context["role"],
        "design_id": context["design"]["design_id"],
        "result_artifact_ids": identifiers,
        "summary": "合成审阅意见，不表示真实经济推断",
        "findings": [
            {"status": "pending", "explanation": "缺少识别诊断", "evidence_result_ids": identifiers}
        ],
        "suggested_analyses": [
            {
                "purpose": "检查工具强度",
                "reason": "当前没有第一阶段结果",
                "based_on_result_ids": identifiers,
            }
        ],
    }


class GateTransport:
    """两项请求必须都进入屏障，测试才释放；不按耗时猜测并发。"""

    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.first_finished = asyncio.Event()
        self.requests: list[dict[str, Any]] = []
        self.active = self.peak = 0

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if len(self.requests) >= 2:
            self.entered.set()
        context = json.loads(request["messages"][1]["content"])
        try:
            await self.release.wait()
            if context["role"] == "interpretation":
                if self.mode == "transport":
                    raise RuntimeError("合成错误，异常正文不能归档")
                if self.mode in {"timeout", "cancel_peer"}:
                    await asyncio.Event().wait()
            response = report(context)
            if context["role"] == "interpretation":
                if self.mode == "role":
                    response["role"] = "design_consistency"
                elif self.mode == "design":
                    response["design_id"] = "design_foreign"
                elif self.mode == "results":
                    response["result_artifact_ids"] = ["artifact_foreign"]
                elif self.mode == "finding":
                    response["findings"][0]["evidence_result_ids"] = ["artifact_foreign"]
                elif self.mode == "suggestion":
                    response["suggested_analyses"][0]["based_on_result_ids"] = ["artifact_foreign"]
            if context["role"] == "design_consistency":
                self.first_finished.set()
            return envelope(response)
        finally:
            self.active -= 1


async def inputs(fixture: BackendFixture) -> ReviewArguments:
    store, backend, _, spec, program = fixture
    outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
    assert outcome.result.result_id is not None
    store.select_result(spec.experiment_id, outcome.result.result_id)
    return ReviewArguments(
        design_id=spec.design_id, result_artifact_ids=(outcome.result.result_id,)
    )


def model(store: RunStore, transport: GateTransport | SequenceTransport) -> PersistentModelClient:
    return PersistentModelClient(
        store, ModelConfig("https://example.invalid", "替身", "TEST_KEY"), transport
    )


async def start(
    fixture: BackendFixture, mode: str = "normal", *, timeout: float = 10
) -> tuple[ResultReviewer, GateTransport, ReviewArguments]:
    arguments = await inputs(fixture)
    transport = GateTransport(mode)
    return (
        ResultReviewer(fixture[0], model(fixture[0], transport), timeout_seconds=timeout),
        transport,
        arguments,
    )


async def wait(event: asyncio.Event) -> None:
    # 超时仅防止测试死锁，不作为并发通过阈值。
    await asyncio.wait_for(event.wait(), timeout=5)


async def test_barrier_proves_two_requests_and_independent_parent_links(
    setup_backend: BackendFixture,
) -> None:
    store = setup_backend[0]
    reviewer, transport, arguments = await start(setup_backend)
    original = store.selected_results()
    work = asyncio.create_task(reviewer.review(arguments))
    await wait(transport.entered)
    calls = store.model_calls()
    assert len(calls) == transport.active == transport.peak == 2
    assert all(c.status == CallStatus.RUNNING for c in calls)
    assert len({c.parent_tool_call_id for c in calls}) == 1
    assert len({c.operation_id for c in calls}) == 2
    assert len({c.request_artifact_id for c in calls}) == 2
    assert all(c.context_result_ids == arguments.result_artifact_ids for c in calls)
    assert all(c.context_design_id == arguments.design_id and c.retry_no == 0 for c in calls)
    for request in transport.requests:
        context = json.loads(request["messages"][1]["content"])
        assert context["result_artifact_ids"] == list(arguments.result_artifact_ids)
        assert context["design_id"] == arguments.design_id
    transport.release.set()
    batch = await work
    assert batch.status == "succeeded" and batch.intervals_overlap is True
    assert store.tool_call(batch.tool_call_id).status == CallStatus.SUCCEEDED
    assert len({r.output_artifact_id for r in batch.reviews}) == 2
    assert all(r.report and r.report.findings[0].status == "pending" for r in batch.reviews)
    assert store.selected_results() == original
    assert not store.call_summary()["unfinished_model_calls"]
    assert not store.call_summary()["unfinished_tool_calls"]


async def test_batches_share_a_two_request_limit(setup_backend: BackendFixture) -> None:
    reviewer, transport, arguments = await start(setup_backend)
    one = asyncio.create_task(reviewer.review(arguments))
    await wait(transport.entered)
    two = asyncio.create_task(reviewer.review(arguments))
    await asyncio.sleep(0)
    assert len(transport.requests) == 2
    transport.release.set()
    batches = await asyncio.gather(one, two)
    assert all(b.status == "succeeded" for b in batches)
    assert len(transport.requests) == 4 and transport.peak == 2


@pytest.mark.parametrize(
    "mode", ["transport", "timeout", "role", "design", "results", "finding", "suggestion"]
)
async def test_partial_failure_preserves_peer_without_retry(
    setup_backend: BackendFixture, mode: str
) -> None:
    reviewer, transport, arguments = await start(
        setup_backend, mode, timeout=0.1 if mode == "timeout" else 10
    )
    work = asyncio.create_task(reviewer.review(arguments))
    await wait(transport.entered)
    transport.release.set()
    batch = await work
    assert batch.status == "partial" and batch.reviews[0].report is not None
    assert batch.reviews[1].report is None and batch.reviews[1].output_artifact_id
    calls = setup_backend[0].model_calls()
    assert len(calls) == len(transport.requests) == 2
    assert all(c.retry_no == 0 for c in calls)
    failed = calls[1]
    if mode in {"transport", "timeout"}:
        assert failed.status == CallStatus.FAILED and failed.total_tokens is None
        assert failed.error_category == mode
    else:
        assert failed.status == CallStatus.SUCCEEDED and failed.parse_status == ParseStatus.INVALID
        assert failed.error_category == "response_schema"
    assert setup_backend[0].tool_call(batch.tool_call_id).error_category == "partial_review"
    assert not setup_backend[0].call_summary()["unfinished_model_calls"]


@pytest.mark.parametrize("completed_peer", [False, True])
async def test_cancel_archives_both_children_and_preserves_completed_peer(
    setup_backend: BackendFixture, completed_peer: bool
) -> None:
    reviewer, transport, arguments = await start(setup_backend, "cancel_peer")
    work = asyncio.create_task(reviewer.review(arguments))
    await wait(transport.entered)
    if completed_peer:
        transport.release.set()
        await wait(transport.first_finished)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    store = setup_backend[0]
    calls = store.model_calls()
    assert calls[0].status == (CallStatus.SUCCEEDED if completed_peer else CallStatus.CANCELLED)
    assert calls[1].status == CallStatus.CANCELLED
    parent = [t for t in store.tool_calls() if t.tool_name == "review_results"][0]
    assert parent.status == CallStatus.CANCELLED and parent.response_artifact_id
    saved = json.loads(store.read_artifact(parent.response_artifact_id))
    assert (saved["reviews"][0]["report"] is not None) == completed_peer
    assert not store.call_summary()["unfinished_model_calls"]
    assert not store.call_summary()["unfinished_tool_calls"]


async def test_insufficient_pair_budget_does_not_send_or_invent_requests(
    setup_backend: BackendFixture,
) -> None:
    reviewer, transport, arguments = await start(setup_backend)
    store = setup_backend[0]
    maximum = store.load_state().budget.max_model_calls
    consume = model(store, SequenceTransport([envelope()] * (maximum - 1)))
    for _ in range(maximum - 1):
        await consume.decide(context="合成预算夹具", operation_id=new_id("operation"))
    batch = await reviewer.review(arguments)
    assert batch.status == "failed" and batch.intervals_overlap is None
    assert all(r.status == "unavailable" and r.model_call_id is None for r in batch.reviews)
    assert len(store.model_calls()) == maximum - 1 and not transport.requests


@pytest.mark.parametrize("case", ["foreign", "duplicate", "design"])
async def test_invalid_inputs_rejected_before_business_or_model_call(
    setup_backend: BackendFixture, case: str
) -> None:
    reviewer, transport, arguments = await start(setup_backend)
    invalid = arguments.model_copy(
        update={
            "result_artifact_ids": ("artifact_foreign",)
            if case == "foreign"
            else arguments.result_artifact_ids * 2
            if case == "duplicate"
            else arguments.result_artifact_ids,
            "design_id": "design_foreign" if case == "design" else arguments.design_id,
        }
    )
    before = len(setup_backend[0].tool_calls())
    with pytest.raises(StateConflict):
        await reviewer.review(invalid)
    assert not transport.requests and len(setup_backend[0].tool_calls()) == before


async def test_input_tampering_during_io_invalidates_batch_and_pauses(
    setup_backend: BackendFixture,
) -> None:
    reviewer, transport, arguments = await start(setup_backend)
    store = setup_backend[0]
    selected = store.selected_results()[0]
    work = asyncio.create_task(reviewer.review(arguments))
    await wait(transport.entered)
    artifact = store.artifact(store.attempt(selected.attempt_id).code_artifact_id or "")
    (store.directory / artifact.relative_path).write_text("合成篡改")
    transport.release.set()
    batch = await work
    assert batch.status == "failed" and not batch.input_valid
    assert all(r.report for r in batch.reviews)
    assert store.tool_call(batch.tool_call_id).error_category == "stale_inputs"
    assert store.load_state().status == RunStatus.PAUSED


async def test_main_loop_receives_both_reports_without_new_analysis(
    setup_backend: BackendFixture,
) -> None:
    store, backend, _, spec, _ = setup_backend
    arguments = await inputs(setup_backend)
    contexts: list[dict[str, Any]] = []

    class FeedbackTransport:
        async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            context = json.loads(request["messages"][1]["content"])
            contexts.append(context)
            if "role" in context:
                return envelope(report(context))
            feedback = context["recent_feedback"]
            if not feedback:
                return envelope(
                    {
                        "action": "review_results",
                        "reason": "依据当前结果审阅",
                        "arguments": arguments.model_dump(mode="json"),
                    }
                )
            assert feedback[-1]["review_batch"]["status"] == "succeeded"
            assert {r["role"] for r in feedback[-1]["review_batch"]["reviews"]} == set(ROLES)
            return envelope(FINISH)

    adapter = PersistentModelClient(
        store, ModelConfig("https://example.invalid", "替身", "TEST_KEY"), FeedbackTransport()
    )
    reviewer = ResultReviewer(store, adapter)
    loop = ResearchLoop(store, adapter, backend, spec.input_artifact_id, reviewer=reviewer)
    before = len(store.attempts(spec.experiment_id))
    await loop.run()
    assert len(store.model_calls()) == 4 and len(contexts) == 4
    assert len(store.attempts(spec.experiment_id)) == before
    parent = [t for t in store.tool_calls() if t.tool_name == "review_results"][0]
    assert parent.source_model_call_id == store.model_calls()[0].model_call_id
    assert all(c.parent_tool_call_id == parent.tool_call_id for c in store.model_calls()[1:3])
    assert store.selected_results()[0].result_id == arguments.result_artifact_ids[0]
    assert "review_results" in {t["name"] for t in tool_descriptions(reviews_enabled=True)}
    assert not store.call_summary()["unfinished_tool_calls"]
