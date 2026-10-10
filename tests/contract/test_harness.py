"""替身仅用于循环控制流；持久化、生产后端及模型适配器仍实际运行。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from econ_agent.config import ModelConfig
from econ_agent.harness import ResearchLoop, tool_descriptions
from econ_agent.model_client import PersistentModelClient
from econ_agent.schemas import CallStatus, HumanAnswer, RunStatus, content_hash
from econ_agent.state import RunStore
from tests.contract.test_stata_backend import BackendFixture
from tests.contract.test_stata_backend import setup_backend as setup_backend
from tests.unit.test_model_client import FINISH, envelope


class DecisionTransport:
    """根据本次真实请求上下文构造测试动作，不是实际模型决策。"""

    def __init__(self, choose: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.choose = choose
        self.contexts: list[dict[str, Any]] = []

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        context = json.loads(request["messages"][1]["content"])
        self.contexts.append(context)
        return envelope(self.choose(context))


def analysis(
    context: dict[str, Any], *, purpose: str = "合成基准", retry: str | None = None
) -> dict[str, Any]:
    selected = context["selected_results"]
    return {
        "action": "run_analysis",
        "reason": "检验当前设计",
        "arguments": {
            "design_id": context["design"]["design_id"],
            "design_version": 1,
            "purpose": purpose,
            "initiation": "exploratory" if selected else "planned",
            "based_on_result_ids": [r["result_id"] for r in selected],
            "input_artifact_id": context["input_artifact_id"],
            "method": "ivregress 2sls",
            "variables": ["y", "x", "z"],
            "sample_rule": "group==1",
            "expected_result_type": "coefficients",
            "code": "ivregress 2sls y (x=z) if group==1, small",
            "iv": {
                "outcome": "y",
                "endogenous": "x",
                "instruments": ["z"],
                "sample_expression": "group==1",
            },
            "retry_attempt_id": retry,
        },
    }


def loop_for(
    fixture: BackendFixture, choose: Callable[[dict[str, Any]], dict[str, Any]], **options: Any
) -> tuple[ResearchLoop, DecisionTransport]:
    store, backend, _, spec, _ = fixture
    transport = DecisionTransport(choose)
    model = PersistentModelClient(
        store,
        ModelConfig("https://example.invalid", "替身", "TEST_KEY"),
        transport,
        max_transport_retries=0,
    )
    return ResearchLoop(store, model, backend, spec.input_artifact_id, **options), transport


async def test_result_triggers_supplement_with_persistent_source_chain(
    setup_backend: BackendFixture,
) -> None:
    store, _, _, _, _ = setup_backend

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if len(context["selected_results"]) < 2:
            return analysis(
                context, purpose="合成基准" if not context["selected_results"] else "补充比较"
            )
        action = json.loads(json.dumps(FINISH))
        action["arguments"]["evidence_artifact_ids"] = [
            r["result_id"] for r in context["selected_results"]
        ]
        return cast(dict[str, Any], action)

    loop, transport = loop_for(setup_backend, choose)
    reason = await loop.run()
    assert "研究循环已结束" in reason and store.load_state().status == RunStatus.PAUSED
    results = store.selected_results()
    assert len(results) == 2 and store.load_state().research_round == 2
    assert len(transport.contexts) == 3
    second_call = store.model_calls()[1]
    baseline = results[0].result_id
    assert second_call.context_result_ids == (baseline,)
    assert second_call.request_artifact_id
    saved = json.loads(store.read_artifact(second_call.request_artifact_id))
    context = json.loads(saved["messages"][1]["content"])
    assert context["selected_results"][0]["result_id"] == baseline
    followup = store.experiment(results[1].experiment_id)
    assert followup.based_on_result_ids == (baseline,)
    tool = store.tool_call(store.attempt(results[1].attempt_id).tool_call_id)
    assert tool.source_model_call_id == second_call.model_call_id
    assert store.call_summary()["unfinished_tool_calls"] == 0
    assert "report" not in {a.kind for a in store.artifacts()}


@pytest.mark.parametrize(
    "feedback_value,expected", [("说明甲", "get_method_help"), ("说明乙", "finish_research")]
)
async def test_changed_feedback_enters_request_and_changes_next_action(
    setup_backend: BackendFixture,
    feedback_value: str,
    expected: str,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if "说明甲" in context["data_summary"]:
            return {
                "action": "get_method_help",
                "reason": "摘要触发方法检查",
                "arguments": {"method": "ivregress"},
            }
        return FINISH

    loop, transport = loop_for(setup_backend, choose)
    loop.initialized = True
    loop.data_summary = feedback_value
    loop.capabilities = await setup_backend[1].capabilities()
    response = await loop.model.decide(
        context=loop.context(),
        operation_id="feedback_operation",
        context_design_id=setup_backend[3].design_id,
    )
    assert response.action and response.action.action == expected
    assert feedback_value in transport.contexts[0]["data_summary"]
    saved = response.model_call.request_artifact_id
    assert saved and feedback_value.encode() in setup_backend[0].read_artifact(saved)


async def test_retry_failure_is_feedback_then_new_attempt_same_operation(
    setup_backend: BackendFixture,
) -> None:
    store, _, native, _, _ = setup_backend
    native.mode = "stata_failure"

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        feedback = context["recent_feedback"]
        if not feedback:
            return analysis(context)
        if feedback[-1].get("error"):
            native.mode = "normal"
            assert feedback[-1]["outcome"]["error"]["stata_return_code"] == 111
            return analysis(context, retry=feedback[-1]["attempt_id"])
        return FINISH

    loop, transport = loop_for(setup_backend, choose)
    await loop.run()
    tools = [t for t in store.tool_calls() if t.tool_name == "run_analysis"]
    assert len(tools) == 2 and tools[0].operation_id == tools[1].operation_id
    assert [t.retry_no for t in tools] == [0, 1]
    assert [t.status for t in tools] == [CallStatus.FAILED, CallStatus.SUCCEEDED]
    result = store.selected_results()[0]
    assert len(store.attempts(result.experiment_id)) == 2
    assert store.load_state().research_round == 1
    assert "outcome" in transport.contexts[1]["recent_feedback"][-1]


async def test_analysis_repair_limit_and_no_pending_or_fake_attempts(
    setup_backend: BackendFixture,
) -> None:
    store, _, native, _, _ = setup_backend
    native.mode = "stata_failure"

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        failures = [f for f in context["recent_feedback"] if f.get("attempt_id")]
        return analysis(context, retry=failures[-1]["attempt_id"] if failures else None)

    loop, _ = loop_for(setup_backend, choose, max_analysis_repairs=1, max_consecutive_errors=3)
    await loop.run()
    analysis_tools = [t for t in store.tool_calls() if t.tool_name == "run_analysis"]
    assert len(analysis_tools) == 3
    assert analysis_tools[-1].arguments_status == "fail"
    assert (
        sum(
            len(store.attempts(e))
            for e in {a["experiment_id"] for a in loop.feedback if a.get("experiment_id")}
        )
        == 2
    )
    assert store.load_state().status == RunStatus.PAUSED
    assert store.call_summary()["unfinished_tool_calls"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("design_version", 99),
        ("sample_rule", "group==0"),
        ("method", "regress"),
        ("variables", ["absent"]),
    ],
)
async def test_invalid_parameters_never_launch_analysis(
    setup_backend: BackendFixture,
    field: str,
    value: Any,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        action = analysis(context)
        action["arguments"][field] = value
        return action

    loop, _ = loop_for(setup_backend, choose)
    await loop.run()
    store, _, native, _, _ = setup_backend
    assert not native.main_started.is_set()
    assert all(
        t.arguments_status == "fail" for t in store.tool_calls() if t.tool_name == "run_analysis"
    )
    assert store.load_state().research_round == 0
    assert store.call_summary()["unfinished_tool_calls"] == 0


async def test_schema_errors_bounded_without_dispatch(setup_backend: BackendFixture) -> None:
    loop, transport = loop_for(setup_backend, lambda _: {"action": "不存在"})
    await loop.run()
    store = setup_backend[0]
    assert len(transport.contexts) == 2 and len(store.tool_calls()) == 2
    assert store.call_summary()["model_parse_failures"] == 2
    assert transport.contexts[1]["recent_feedback"][0]["error"]["category"] == "response_schema"


async def test_design_proposal_waits_without_fake_human_answer(
    setup_backend: BackendFixture,
) -> None:
    store, _, _, spec, _ = setup_backend
    content = store.design(spec.design_id).content.model_dump(mode="json")
    loop, transport = loop_for(
        setup_backend,
        lambda _: {
            "action": "propose_design",
            "reason": "提出修改",
            "arguments": {"content": content, "change_reason": "需要研究者实际确认"},
        },
    )
    await loop.run()
    assert len(transport.contexts) == 1 and store.load_state().status == RunStatus.WAITING_HUMAN
    request_id = store.load_state().pending_request_ids[0]
    request = store._one(
        "SELECT payload_json FROM human_requests WHERE request_id=?", (request_id,)
    )
    assert json.loads(request[0])["answer"] is None
    # API 延续只应用明确的测试答复，不把它记录为真实人工输入。
    proposed = loop.feedback[-1]["design"]
    design = store.design(proposed["design_id"])
    store.apply_answer(
        request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="accept", explanation="离线测试夹具"),
    )
    resumed, next_transport = loop_for(setup_backend, lambda _: FINISH)
    await resumed.run()
    assert next_transport.contexts[0]["design"]["design_id"] == design.design_id
    assert len(store.tool_calls()) == 4  # 两项初始化查询、设计工具、结束工具；没有重复查询。


async def test_round_budget_rejects_new_analysis_but_allows_finish(
    setup_backend: BackendFixture,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if context["state"]["research_round"] < 3:
            return analysis(context, purpose=f"合成分析{context['state']['research_round']}")
        if context["recent_feedback"][-1].get("error"):
            return FINISH
        return analysis(context, purpose="超出轮次")

    loop, _ = loop_for(setup_backend, choose)
    await loop.run()
    store = setup_backend[0]
    assert store.load_state().research_round == 3 and len(store.selected_results()) == 3
    assert "轮次预算" in loop.feedback[-2]["error"]
    assert store.call_summary()["unfinished_tool_calls"] == 0


async def test_runtime_budget_stops_before_startup_query(setup_backend: BackendFixture) -> None:
    loop, transport = loop_for(setup_backend, lambda _: FINISH)
    loop.elapsed_before = 999999
    reason = await loop.run()
    assert "时间预算" in reason and not transport.contexts
    assert not setup_backend[0].tool_calls()


async def test_loop_cancel_persists_model_and_run_state(setup_backend: BackendFixture) -> None:
    loop, transport = loop_for(setup_backend, lambda _: FINISH)
    gate = asyncio.Event()
    original = transport.complete

    async def waiting(request: dict[str, Any]) -> dict[str, Any]:
        gate.set()
        await asyncio.Event().wait()
        return await original(request)

    transport.complete = waiting  # type: ignore[method-assign]
    worker = asyncio.create_task(loop.run())
    await gate.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert setup_backend[0].load_state().status == RunStatus.PAUSED
    assert setup_backend[0].model_calls()[0].status == CallStatus.CANCELLED


def test_tool_description_contract_contains_current_iv_and_retry_fields() -> None:
    tools = {t["name"]: t for t in tool_descriptions()}
    assert "review_results" not in tools
    schema = tools["run_analysis"]["action_schema"]["$defs"]["AnalysisArguments"]["properties"]
    assert "iv" in schema and "retry_attempt_id" in schema
    assert "finish_research" in tools and "propose_design" in tools


@pytest.mark.parametrize(
    "coefficient,next_action", [(1.0, "finish_research"), (-1.0, "ask_researcher")]
)
async def test_changed_numeric_tool_result_changes_next_decision(
    setup_backend: BackendFixture,
    coefficient: float,
    next_action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, native, _, _ = setup_backend
    original = native.call

    async def altered(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = await original(name, arguments)
        if name == "stata_get_results":
            raw["data"]["e"]["_matrices"]["b"]["values"][0][0] = coefficient
        if (
            name == "stata_run"
            and arguments.get("is_file")
            and Path(arguments["code"]).name == "extract.do"
        ):
            session = native.sessions[arguments["session_id"]]
            output = session["output"] / "coefficients.csv"
            output.write_text(output.read_text().replace("x,1,1,", f"x,{coefficient},1,"))
        return raw

    monkeypatch.setattr(native, "call", altered)

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if not context["selected_results"]:
            return analysis(context)
        actual = context["selected_results"][0]["normalized"]["estimates"][0]["value"]
        if actual > 0:
            return FINISH
        return {
            "action": "ask_researcher",
            "reason": "根据工具返回核对设计",
            "arguments": {
                "design_id": context["design"]["design_id"],
                "question": "本次估计值为负，请核对后续解释范围",
                "impact": "影响结论措辞",
            },
        }

    loop, transport = loop_for(setup_backend, choose)
    await loop.run()
    assert loop.feedback[-1]["action"] == next_action
    second = transport.contexts[1]
    assert second["selected_results"][0]["normalized"]["estimates"][0]["value"] == coefficient
    assert (
        second["recent_feedback"][0]["outcome"]["result"]["normalized"]["estimates"][0]["value"]
        == coefficient
    )
    result = store.selected_results()[0]
    assert store.model_calls()[1].context_result_ids == (result.result_id,)


@pytest.mark.parametrize("budget_kind", ["tool", "model"])
async def test_loop_call_budget_pauses_without_extra_request_or_tool(
    tmp_path: Path,
    budget_kind: str,
) -> None:
    from econ_agent.inputs import prepare_input
    from econ_agent.schemas import Budget, DesignContent, ResearchTask
    from econ_agent.stata_backend import StataBackend
    from tests.contract.test_stata_backend import FakeTransport

    data = tmp_path / "data.csv"
    data.write_text("y,x,z,group\n1,2,3,1\n2,3,4,1\n")
    task = ResearchTask(
        data_path=data,
        research_goal="离线预算边界",
        budget=Budget(
            max_model_calls=1 if budget_kind == "model" else 5,
            max_tool_calls=3 if budget_kind == "tool" else 10,
        ),
    )
    with RunStore.create(tmp_path / "runs", task) as store:
        prepared = prepare_input(store, tmp_path / "backup")
        store.transition_run(RunStatus.RUNNING)
        design = store.create_design(
            DesignContent(
                population="合成样本",
                variables={"y": "因变量"},
                sample_rule="group==1",
                estimand="预算控制",
                methods=("summarize",),
            ),
            change_reason="夹具",
        )
        request = store.request_human(design.design_id, "测试确认")
        store.apply_answer(
            request.request_id,
            design_sha256=content_hash(design),
            answer=HumanAnswer(decision="accept", explanation="夹具"),
        )
        backend = StataBackend(store, FakeTransport(), prepared, tmp_path / "work")
        transport = DecisionTransport(
            lambda c: {
                "action": "inspect_data",
                "reason": "预算测试",
                "arguments": {"input_artifact_id": c["input_artifact_id"]},
            }
        )
        model = PersistentModelClient(
            store, ModelConfig("https://example.invalid", "测试", "TEST_KEY"), transport
        )
        loop = ResearchLoop(store, model, backend, prepared.input_artifact_id)
        reason = await loop.run()
        assert "预算已耗尽" in reason
        assert len(transport.contexts) == len(store.model_calls()) == 1
        assert len(store.tool_calls()) == 3 and store.call_summary()["unfinished_tool_calls"] == 0


async def test_help_query_keeps_source_model_and_one_business_record(
    setup_backend: BackendFixture,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if not context["recent_feedback"]:
            return {
                "action": "get_method_help",
                "reason": "核对本机方法",
                "arguments": {"method": "ivregress"},
            }
        assert "替身帮助" in context["recent_feedback"][-1]["help"]
        return FINISH

    loop, _ = loop_for(setup_backend, choose)
    await loop.run()
    store = setup_backend[0]
    calls = [c for c in store.tool_calls() if c.tool_name == "get_method_help"]
    assert len(calls) == 1 and calls[0].source_model_call_id == store.model_calls()[0].model_call_id


async def test_unavailable_method_returns_error_without_stata_attempt(
    setup_backend: BackendFixture,
) -> None:
    loop, _ = loop_for(
        setup_backend,
        lambda _: {
            "action": "get_method_help",
            "reason": "测试不可用命令",
            "arguments": {"method": "unknown_command"},
        },
    )
    await loop.run()
    store, _, native, _, _ = setup_backend
    assert not native.main_started.is_set()
    assert len([t for t in store.tool_calls() if t.tool_name == "get_method_help"]) == 2
    assert all(
        t.status == CallStatus.FAILED
        for t in store.tool_calls()
        if t.tool_name == "get_method_help"
    )


async def test_history_result_reference_is_rejected_before_execution(
    setup_backend: BackendFixture,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        action = analysis(context)
        action["arguments"]["based_on_result_ids"] = ["invented_result"]
        return action

    loop, _ = loop_for(setup_backend, choose)
    await loop.run()
    assert not setup_backend[2].main_started.is_set()
    assert "当前核验结果" in loop.feedback[-1]["error"]
    assert setup_backend[0].load_state().research_round == 0


@pytest.mark.parametrize("valid_evidence", [True, False])
async def test_finish_accepts_current_raw_evidence_and_rejects_unrelated_artifact(
    setup_backend: BackendFixture,
    valid_evidence: bool,
) -> None:
    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if not context["selected_results"]:
            return analysis(context)
        action = dict(FINISH) | {"arguments": dict(FINISH["arguments"])}
        action["arguments"]["evidence_artifact_ids"] = (
            context["selected_results"][0]["raw_artifact_ids"]
            if valid_evidence
            else ["unrelated_artifact"]
        )
        return action

    loop, _ = loop_for(setup_backend, choose)
    await loop.run()
    finish = [t for t in setup_backend[0].tool_calls() if t.tool_name == "finish_research"]
    assert finish[-1].status == (CallStatus.SUCCEEDED if valid_evidence else CallStatus.FAILED)
    assert len(setup_backend[0].selected_results()) == 1
    assert not setup_backend[0].call_summary()["unfinished_tool_calls"]


async def test_material_pages_are_limited_to_task_snapshots(setup_backend: BackendFixture) -> None:
    store, _, _, spec, _ = setup_backend
    text = "任务背景仅用于离线测试。"
    artifact = store.archive_bytes("inputs/test_material.txt", text.encode(), kind="input")

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        if not context["recent_feedback"]:
            return {
                "action": "read_material",
                "reason": "读取背景页",
                "arguments": {"artifact_id": artifact.artifact_id, "start": 2, "max_characters": 4},
            }
        assert context["recent_feedback"][-1]["text"] == text[2:6]
        return FINISH

    loop, _ = loop_for(setup_backend, choose)
    loop.materials = [{"artifact_id": artifact.artifact_id, "name": "测试背景", "role": "material"}]
    loop.initialized = True
    await loop.run()
    assert [c.tool_name for c in store.tool_calls()] == ["read_material", "finish_research"]
    invalid, _ = loop_for(
        setup_backend,
        lambda _: {
            "action": "read_material",
            "reason": "拒绝读取原数据或其他文件",
            "arguments": {"artifact_id": spec.input_artifact_id},
        },
    )
    store.transition_run(RunStatus.RUNNING)
    await invalid.run()
    assert "文字材料快照" in invalid.feedback[-1]["error"]


async def test_stale_failed_attempt_does_not_reuse_retry_number(
    setup_backend: BackendFixture,
) -> None:
    store, _, native, _, _ = setup_backend
    native.mode = "stata_failure"

    def choose(context: dict[str, Any]) -> dict[str, Any]:
        attempts = [f for f in context["recent_feedback"] if f.get("attempt_id")]
        return analysis(context, retry=attempts[0]["attempt_id"] if attempts else None)

    loop, _ = loop_for(setup_backend, choose, max_consecutive_errors=3)
    await loop.run()
    attempts = [f for f in loop.feedback if f.get("attempt_id")]
    assert len(attempts) == 2
    assert "最近一次" in loop.feedback[-1]["error"]
    calls = store.tool_calls()
    assert not store.call_summary()["unfinished_tool_calls"]
    assert len({(c.operation_id, c.retry_no) for c in calls}) == len(calls)


async def test_explicit_recovery_preserves_success_and_budgets(
    setup_backend: BackendFixture,
) -> None:
    store, _, _, _, _ = setup_backend

    def fail_after_baseline(context: dict[str, Any]) -> dict[str, Any]:
        action = analysis(context)
        if context["selected_results"]:
            action["arguments"]["based_on_result_ids"] = []
        return action

    loop, _ = loop_for(setup_backend, fail_after_baseline)
    await loop.run()
    assert loop.errors == 2
    baseline = store.selected_results()[0]
    assert baseline.result_id
    assert baseline.result_id in str(loop.feedback[-1]["error"])
    budget = store.load_state().budget
    calls = store.call_summary()
    store.transition_run(RunStatus.RUNNING)
    ordinary, _ = loop_for(setup_backend, fail_after_baseline)
    await ordinary.run()
    assert store.call_summary() == calls
    assert ordinary.errors == 2
    store.transition_run(RunStatus.RUNNING)
    request = store.request_human(baseline.design_id, "显式恢复原设计", kind="research_question")
    store.apply_answer(
        request.request_id,
        design_sha256=request.target_design_sha256,
        answer=HumanAnswer(decision="accept", explanation="接受原设计并继续；离线脚本确认"),
    )

    def continue_once(context: dict[str, Any]) -> dict[str, Any]:
        if len(context["selected_results"]) == 1:
            return analysis(context, purpose="纠正依据引用后补充一次")
        action = json.loads(json.dumps(FINISH))
        action["arguments"]["evidence_artifact_ids"] = [
            r["result_id"] for r in context["selected_results"]
        ]
        return cast(dict[str, Any], action)

    recovered, _ = loop_for(setup_backend, continue_once)
    assert recovered.errors == 0
    # 新检查点立即消费确认，构造下一实例不会再次消费同一答复。
    latest = json.loads(store.read_artifact(store.artifacts(kind="tool_response")[-1].artifact_id))
    assert request.request_id in latest["reason"]
    await recovered.run()
    assert store.load_state().budget == budget and store.load_state().research_round == 2
    assert len(store.attempts(baseline.experiment_id)) == 1
    assert baseline.result_id in {r.result_id for r in store.selected_results()}
    assert len(store.selected_results()) == 2
