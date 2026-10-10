"""明确使用协议替身检验控制流；真实数值验收另由显式脚本执行。"""

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from econ_agent.inputs import check_input, prepare_input
from econ_agent.ports import AnalysisBackend
from econ_agent.schemas import (
    CallStatus,
    DesignContent,
    ExecutionStatus,
    ExperimentSpec,
    ExtractionStatus,
    HumanAnswer,
    ResearchTask,
    RunStatus,
    ToolCall,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import BackendError, IVSpecification, PreparedInput, StataProgram
from econ_agent.state import ArtifactIntegrityError, RunStore, StateConflict, new_id


class FakeTransport:
    """仅替代 MCP 网络边界，不替代生产存储、归档、提取及核验。"""

    service_instance_id = "mcp_test"

    def __init__(self) -> None:
        self.environment: dict[str, Any] = {"mcp_version": "测试替身"}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.mode = "normal"
        self.stop_succeeds = True
        self.main_started = asyncio.Event()
        self.result_gate: asyncio.Event | None = None
        self.on_execute: Callable[[], object] | None = None
        self.cmdline = ""

    async def process_exists(self, pid: int) -> bool:
        return any(s["pid"] == pid and s["alive"] for s in self.sessions.values())

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        data: dict[str, Any] = {}
        success = True
        session_name = arguments.get("session_id", "")
        if name == "stata_manage_session":
            action = arguments["action"]
            if action == "create":
                self.sessions[session_name] = {"pid": 9000 + len(self.sessions), "alive": True}
            elif action == "list":
                data = {
                    "sessions": [
                        {"id": k, "pid": s["pid"]} for k, s in self.sessions.items() if s["alive"]
                    ]
                }
            elif action == "stop":
                success = self.stop_succeeds
                if success:
                    self.sessions[session_name]["alive"] = False
            elif action == "detect":
                data = {"stata_version": "18，替身不表示真实安装"}
        elif name == "stata_run":
            if not arguments.get("is_file"):
                data = {"rc": 0, "stdout": "ECON_AVAILABLE=1"}
            else:
                path = Path(arguments["code"])
                text = path.read_text()
                if path.name == "analysis.do":
                    matched = re.search(r'global ECON_OUTPUT "([^"]+)"', text)
                    assert matched
                    self.sessions[session_name]["output"] = Path(matched[1])
                    body_path = re.search(r'\ndo "([^"]+)"', text)
                    assert body_path
                    self.cmdline = Path(body_path[1]).read_text()
                    self.main_started.set()
                    if self.on_execute:
                        self.on_execute()
                elif path.name == "extract.do":
                    output = self.sessions[session_name]["output"]
                    if self.mode != "missing_table":
                        (output / "coefficients.csv").write_text(
                            "parameter,coefficient,standard_error,p_value,lower,upper,df,n\n"
                            "x,1,1,0.3,-3,5,2,2\n_cons,2,2,0.3,-6,10,2,2\n"
                        )
                        (output / "sample_check.txt").write_text(
                            "0" if self.mode == "sample_mismatch" else "1"
                        )
                        if self.mode == "float_rounding":
                            # 重现联调中 postfile 默认 float 导致标准误精度丢失的缺陷。
                            text = (
                                (output / "coefficients.csv")
                                .read_text()
                                .replace("x,1,1,", "x,0.9442793851547995,0.15652546286582947,")
                            )
                            (output / "coefficients.csv").write_text(text)
                task_id = new_id("task")
                self.tasks[task_id] = {
                    "session": session_name,
                    "primary": path.name == "analysis.do",
                }
                data = {"task_id": task_id}
        elif name == "stata_task_status":
            task = self.tasks[arguments["task_id"]]
            if self.mode == "transport_failure":
                raise ConnectionError("协议替身注入的断连")
            looping = self.mode == "running" and task["primary"]
            failed = self.mode == "stata_failure" and task["primary"]
            result = {
                "success": not failed,
                "data": {"rc": 111 if failed else 0},
                "error": {"rc": 111} if failed else None,
            }
            data = {
                "task_id": arguments["task_id"],
                "status": "running" if looping else "failed" if failed else "done",
                "result": None if looping else result,
            }
        elif name == "stata_get_results":
            if self.result_gate:
                await self.result_gate.wait()
            command = self.cmdline
            if self.mode == "specification_mismatch":
                command = command.replace("group==1", "group==0")
            data = {
                "e": {
                    "cmd": "ivregress",
                    "cmdline": command,
                    "estimator": "2sls",
                    "small": "small",
                    "vce": "unadjusted",
                    "df_r": 2,
                    "N": 2,
                    "depvar": "y",
                    "endog": "x",
                    "insts": "z",
                    "_matrices": {
                        "b": {
                            "values": [[99 if self.mode == "bad_matrix" else 1, 2]],
                            "truncated": False,
                        },
                        "V": {"values": [[1, 0], [0, 4]], "truncated": False},
                    },
                }
            }
            if self.mode == "float_rounding":
                data["e"]["_matrices"]["b"]["values"][0][0] = 0.9442793851547995
                data["e"]["_matrices"]["V"]["values"][0][0] = 0.024500218792233887
        elif name == "stata_inspect_data":
            data = {"variables": [{"name": "y"}], "rendered": "替身数据说明"}
        elif name == "stata_get_help":
            if arguments["topic"] == "missing_method":
                success = False
            data = {"rendered": "替身帮助文本"}
        return {"tool": name, "success": success, "data": data, "error": None, "log": None}


BackendFixture = tuple[RunStore, StataBackend, FakeTransport, ExperimentSpec, StataProgram]


@pytest.fixture
def setup_backend(
    tmp_path: Path,
) -> Iterator[tuple[RunStore, StataBackend, FakeTransport, ExperimentSpec, StataProgram]]:
    source = tmp_path / "source.csv"
    source.write_text("y,x,z,group\n1,2,3,1\n2,3,4,1\n")
    store = RunStore.create(
        tmp_path / "runs", ResearchTask(data_path=source, research_goal="仅供离线后端契约测试")
    )
    prepared = prepare_input(store, tmp_path / "backups")
    store.transition_run(RunStatus.RUNNING)
    design = store.create_design(
        DesignContent(
            population="两行合成数据",
            variables={"y": "测试因变量", "x": "测试内生变量", "z": "测试工具"},
            sample_rule="group==1",
            estimand="合成结构检查",
            methods=("ivregress 2sls",),
        ),
        change_reason="测试夹具",
    )
    request = store.request_human(design.design_id, "测试确认")
    store.apply_answer(
        request.request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="accept", explanation="合成测试"),
    )
    spec = ExperimentSpec(
        experiment_id=new_id("experiment"),
        run_id=store.run_id,
        design_id=design.design_id,
        purpose="协议控制流测试",
        initiation="planned",
        input_artifact_id=prepared.input_artifact_id,
        method="ivregress 2sls",
        variables=("y", "x", "z"),
        sample_rule="group==1",
        expected_result_type="coefficients",
    )
    store.create_experiment(spec)
    iv = IVSpecification(
        outcome="y", endogenous="x", instruments=("z",), sample_expression="group==1"
    )
    program = StataProgram(code=iv.command(), iv=iv)
    transport = FakeTransport()
    backend = StataBackend(store, transport, prepared, tmp_path / "work")
    interface: AnalysisBackend = backend
    assert interface is backend
    yield store, backend, transport, spec, program
    store.close()


def attempt(store: RunStore, spec: ExperimentSpec) -> str:
    call = ToolCall(
        tool_call_id=new_id("tool"),
        run_id=store.run_id,
        operation_id=new_id("operation"),
        tool_name="run_analysis",
        arguments_status="pass",
    )
    store.record_tool_call(call)
    return store.create_attempt(spec.experiment_id, call.tool_call_id).attempt_id


async def test_full_operation_archives_and_reextracts_without_mcp(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, spec, program = setup_backend
    identifier = attempt(store, spec)
    outcome = await backend.run_analysis(spec, attempt_id=identifier, program=program)
    assert outcome.error is None and outcome.result.extraction_status == ExtractionStatus.SUCCEEDED
    assert outcome.result.result_id is not None
    store.select_result(spec.experiment_id, outcome.result.result_id)
    calls_before = len(transport.calls)
    again = backend.reextract(identifier)
    assert again.result.result_id != outcome.result.result_id
    assert len(transport.calls) == calls_before
    assert store.selected_results() == ()
    assert store.attempt(identifier).attempt_no == 1
    assert store.call_summary()["tool_calls"] == 1
    assert store.tool_call(store.attempt(identifier).tool_call_id).arguments_artifact_id is not None
    assert all(not s["alive"] for s in transport.sessions.values())
    raw_id = store.attempt(identifier).raw_artifact_id
    assert raw_id is not None
    raw = json.loads(store.read_artifact(raw_id))
    assert raw["worker_terminated"] and raw["seed"] == 2001
    stored_index = next(
        i for i, (name, _) in enumerate(transport.calls) if name == "stata_get_results"
    )
    extract_index = next(
        i
        for i, (name, args) in enumerate(transport.calls)
        if name == "stata_run" and args.get("code", "").endswith("extract.do")
    )
    assert stored_index < extract_index
    directory = store.directory
    store.close()
    with RunStore.open(directory) as reopened:
        assert not reopened.recovery_report.issues
        assert reopened.attempt(identifier).execution_status == ExecutionStatus.SUCCEEDED


@pytest.mark.parametrize("mode", ["bad_matrix", "missing_table", "float_rounding"])
async def test_extraction_failure_does_not_rewrite_execution(
    setup_backend: BackendFixture, mode: str
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = mode
    outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
    assert outcome.result.execution_status == ExecutionStatus.SUCCEEDED
    assert (
        outcome.result.normalized is None
        and outcome.result.extraction_status == ExtractionStatus.FAILED
    )
    assert outcome.error is not None
    assert outcome.error.category == "extraction"
    assert store.selected_results() == ()


@pytest.mark.parametrize("mode", ["specification_mismatch", "sample_mismatch"])
async def test_failed_specification_cannot_be_selected(
    setup_backend: BackendFixture, mode: str
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = mode
    outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
    assert outcome.result.normalized is not None
    assert outcome.result.result_id is not None
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, outcome.result.result_id)


async def test_general_commands_keep_raw_without_forced_coefficient_table(
    setup_backend: BackendFixture,
) -> None:
    store, backend, _, spec, _ = setup_backend
    outcome = await backend.run_analysis(
        spec, attempt_id=attempt(store, spec), program=StataProgram(code="summarize y")
    )
    assert outcome.result.execution_status == ExecutionStatus.SUCCEEDED
    assert (
        outcome.result.normalized is None
        and outcome.result.extraction_status == ExtractionStatus.PENDING
    )
    assert outcome.result.raw_artifact_ids


async def test_failed_execution_has_error_code_and_retry_preserves_files(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = "stata_failure"
    first_id = attempt(store, spec)
    failed = await backend.run_analysis(spec, attempt_id=first_id, program=program)
    assert failed.error is not None
    assert (
        failed.result.execution_status == ExecutionStatus.FAILED
        and failed.error.stata_return_code == 111
    )
    old_files = {a.artifact_id: a.sha256 for a in store.artifacts()}
    transport.mode = "normal"
    second_id = attempt(store, spec)
    assert store.attempt(second_id).attempt_no == 2
    passed = await backend.run_analysis(spec, attempt_id=second_id, program=program)
    assert passed.result.execution_status == ExecutionStatus.SUCCEEDED
    for identifier, hash_value in old_files.items():
        assert store.artifact(identifier).sha256 == hash_value
    assert store.attempt(first_id).execution_status == ExecutionStatus.FAILED


async def test_timeout_confirms_worker_exit_before_retry(setup_backend: BackendFixture) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = "running"
    backend.timeout = 0.01
    identifier = attempt(store, spec)
    failed = await backend.run_analysis(spec, attempt_id=identifier, program=program)
    assert failed.error is not None
    assert failed.error.category == "timeout"
    assert failed.result.execution_status == ExecutionStatus.CANCELLED
    assert store.attempt(identifier).termination_confirmed
    assert not any(s["alive"] for s in transport.sessions.values())
    assert attempt(store, spec) != identifier


async def test_cancel_uses_owned_session_and_rejects_late_success(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = "running"
    identifier = attempt(store, spec)
    pending = asyncio.create_task(
        backend.run_analysis(spec, attempt_id=identifier, program=program)
    )
    await transport.main_started.wait()
    assert await backend.cancel(identifier)
    outcome = await pending
    assert outcome.result.execution_status == ExecutionStatus.CANCELLED
    assert (
        store.attempt(identifier).cancel_requested
        and store.attempt(identifier).termination_confirmed
    )
    before = len(transport.calls)
    assert await backend.cancel("attempt_from_old_service") is False
    assert len(transport.calls) == before


async def test_task_cancellation_is_saved_and_propagated(setup_backend: BackendFixture) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = "running"
    identifier = attempt(store, spec)
    pending = asyncio.create_task(
        backend.run_analysis(spec, attempt_id=identifier, program=program)
    )
    await transport.main_started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert store.attempt(identifier).execution_status == ExecutionStatus.CANCELLED
    assert store.attempt(identifier).termination_confirmed


async def test_unconfirmed_termination_keeps_unknown_task_and_blocks_retry(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.mode = "transport_failure"
    transport.stop_succeeds = False
    identifier = attempt(store, spec)
    outcome = await backend.run_analysis(spec, attempt_id=identifier, program=program)
    assert outcome.error is not None
    assert outcome.error.category == "termination"
    saved = store.attempt(identifier)
    assert saved.execution_status == ExecutionStatus.INTERRUPTED and saved.backend_task_id
    assert saved.service_instance_id == transport.service_instance_id
    assert store.tool_call(saved.tool_call_id).status == CallStatus.UNKNOWN
    assert store.load_state().status == RunStatus.PAUSED
    with pytest.raises(StateConflict):
        store.transition_run(RunStatus.RUNNING)


async def test_original_tamper_pauses_without_repairing_file(setup_backend: BackendFixture) -> None:
    store, backend, transport, spec, program = setup_backend
    original = store.load_task().data_path
    backup = backend.prepared.backup_path.read_bytes()
    transport.on_execute = lambda: original.write_text("已修改，不能静默恢复")
    outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
    assert outcome.error is not None
    assert (
        outcome.error.category == "integrity"
        and outcome.result.execution_status == ExecutionStatus.FAILED
    )
    assert store.load_state().status == RunStatus.PAUSED
    assert original.read_text() == "已修改，不能静默恢复"
    assert backend.prepared.backup_path.read_bytes() == backup


async def test_helpers_record_one_business_call_each(setup_backend: BackendFixture) -> None:
    store, backend, transport, _, _ = setup_backend
    await backend.inspect_data(backend.prepared.input_artifact_id)
    await backend.get_method_help("ivregress")
    capabilities = await backend.capabilities()
    assert store.call_summary()["tool_calls"] == 3
    assert all(c.validation == "unverified" for c in capabilities)
    assert all(c.availability == "available" for c in capabilities)
    assert not any(s["alive"] for s in transport.sessions.values())
    with pytest.raises(BackendError) as error:
        await backend.get_method_help("missing_method")
    assert error.value.failure.category == "missing_command"
    assert error.value.failure.diagnostic_artifact_ids


async def test_lock_covers_reading_before_next_analysis(setup_backend: BackendFixture) -> None:
    store, backend, transport, spec, program = setup_backend
    second = spec.model_copy(update={"experiment_id": new_id("experiment")})
    store.create_experiment(second)
    first_id, second_id = attempt(store, spec), attempt(store, second)
    transport.result_gate = asyncio.Event()
    first = asyncio.create_task(backend.run_analysis(spec, attempt_id=first_id, program=program))
    await transport.main_started.wait()
    second_task = asyncio.create_task(
        backend.run_analysis(second, attempt_id=second_id, program=program)
    )
    assert store.attempt(second_id).execution_status == ExecutionStatus.PENDING
    transport.result_gate.set()
    results = await asyncio.gather(first, second_task)
    assert all(r.result.execution_status == ExecutionStatus.SUCCEEDED for r in results)
    assert len(transport.sessions) == 2


def test_backup_and_snapshot_are_independent_and_idempotent(
    setup_backend: BackendFixture, tmp_path: Path
) -> None:
    store, backend, _, _, _ = setup_backend
    prepared: PreparedInput = backend.prepared
    original = store.load_task().data_path
    snapshot = store.directory / store.artifact(prepared.input_artifact_id).relative_path
    assert not original.samefile(prepared.backup_path) and not snapshot.samefile(
        prepared.backup_path
    )
    assert prepare_input(store, tmp_path / "backups") == prepared
    prepared.backup_path.write_text("错误备份")
    with pytest.raises(ArtifactIntegrityError):
        check_input(store, prepared)
    assert original.read_text().startswith("y,x,z,group")


async def test_original_path_in_body_is_rejected_before_rpc(setup_backend: BackendFixture) -> None:
    store, backend, transport, spec, _ = setup_backend
    outcome = await backend.run_analysis(
        spec,
        attempt_id=attempt(store, spec),
        program=StataProgram(code=f'use "{store.load_task().data_path}", clear'),
    )
    assert outcome.error is not None and outcome.error.category == "arguments"
    assert outcome.result.execution_status == ExecutionStatus.FAILED
    assert transport.calls == []


async def test_body_code_is_part_of_selected_result_evidence(setup_backend: BackendFixture) -> None:
    store, backend, _, spec, program = setup_backend
    outcome = await backend.run_analysis(spec, attempt_id=attempt(store, spec), program=program)
    assert outcome.result.result_id is not None
    store.select_result(spec.experiment_id, outcome.result.result_id)
    body = next(a for a in store.artifacts(kind="code") if a.relative_path.endswith("body.do"))
    (store.directory / body.relative_path).write_text("已改变的主体代码")
    with pytest.raises(ArtifactIntegrityError):
        store.selected_results()


async def test_uncertain_query_worker_blocks_resume_until_confirmed(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, _, _ = setup_backend
    transport.stop_succeeds = False
    with pytest.raises(BackendError) as error:
        await backend.get_method_help("ivregress")
    assert error.value.failure.category == "termination"
    with pytest.raises(StateConflict):
        store.transition_run(RunStatus.RUNNING)
    # 调用者核对真实进程的逻辑在真实取消验收中验证；这里用协议替身注入退出证据。
    for session in transport.sessions.values():
        session["alive"] = False
    store.confirm_query_stopped(error.value.failure.tool_call_id)
    assert store.tool_call(error.value.failure.tool_call_id).status == CallStatus.UNKNOWN
    assert store.transition_run(RunStatus.RUNNING).status == RunStatus.RUNNING


async def test_cancel_during_result_read_does_not_recreate_session(
    setup_backend: BackendFixture,
) -> None:
    store, backend, transport, spec, program = setup_backend
    transport.result_gate = asyncio.Event()
    identifier = attempt(store, spec)
    pending = asyncio.create_task(
        backend.run_analysis(spec, attempt_id=identifier, program=program)
    )
    await transport.main_started.wait()
    assert await backend.cancel(identifier)
    transport.result_gate.set()
    outcome = await pending
    assert outcome.result.execution_status == ExecutionStatus.CANCELLED
    assert len(transport.sessions) == 1 and not any(s["alive"] for s in transport.sessions.values())


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0])
def test_analysis_timeout_cannot_disable_deadline(
    setup_backend: BackendFixture, timeout: float, tmp_path: Path
) -> None:
    store, backend, transport, _, _ = setup_backend
    with pytest.raises(ValueError):
        StataBackend(store, transport, backend.prepared, tmp_path / "work", timeout_seconds=timeout)
