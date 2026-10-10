"""合成数据的离线存储契约测试；不冒充真实 Stata 或模型执行。"""

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from econ_agent.migrations import TABLES, FutureVersionError
from econ_agent.schemas import (
    Artifact,
    CallStatus,
    DesignContent,
    ExecutionStatus,
    ExperimentSpec,
    HumanAnswer,
    ModelCall,
    ParseStatus,
    ResearchDesign,
    ResearchTask,
    RunEnvironment,
    RunStatus,
    ScalarResult,
    ScalarValue,
    StandardErrors,
    ToolCall,
    ValidationCheck,
    content_hash,
    utc_now,
)
from econ_agent.state import ArtifactIntegrityError, RunLockedError, RunStore, StateConflict, new_id


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RunStore]:
    source = tmp_path / "source.csv"
    source.write_text("x,y\n1,2\n2,4\n")
    instance = RunStore.create(
        tmp_path / "runs",
        ResearchTask(data_path=source, research_goal="合成数据仅用于离线状态测试"),
        run_id="run_test",
    )
    yield instance
    instance.close()


def design_content() -> DesignContent:
    return DesignContent(
        population="合成测试样本",
        variables={"x": "测试变量"},
        sample_rule="全部两行",
        estimand="测试均值",
        methods=("描述统计",),
    )


def confirmed(store: RunStore) -> ResearchDesign:
    if store.load_state().status == RunStatus.NEW:
        store.transition_run(RunStatus.RUNNING)
    design = store.create_design(design_content(), change_reason="初始测试设计")
    request = store.request_human(design.design_id, "确认此合成测试设计吗？")
    store.apply_answer(
        request.request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="accept", explanation="测试确认"),
    )
    return design


def experiment(store: RunStore, *, identifier: str = "experiment_1") -> ExperimentSpec:
    current = store.load_state().current_design_id
    design_id = current if current else confirmed(store).design_id
    input_data = store.archive_bytes(f"inputs/{identifier}.csv", b"x,y\n1,2\n2,4\n", kind="input")
    spec = ExperimentSpec(
        experiment_id=identifier,
        run_id=store.run_id,
        design_id=design_id,
        purpose="测试存储和核验约束",
        initiation="planned",
        input_artifact_id=input_data.artifact_id,
        method="描述统计",
        variables=("x",),
        sample_rule="全部两行",
        expected_result_type="scalars",
    )
    store.create_experiment(spec)
    return spec


def pending_attempt(
    store: RunStore, spec: ExperimentSpec, *, rerun_reason: str | None = None
) -> str:
    tool = ToolCall(
        tool_call_id=new_id("tool"),
        run_id=store.run_id,
        operation_id=new_id("operation"),
        tool_name="run_analysis",
        arguments_status="pass",
    )
    store.record_tool_call(tool)
    attempt = store.create_attempt(spec.experiment_id, tool.tool_call_id, rerun_reason=rerun_reason)
    return attempt.attempt_id


def started_attempt(store: RunStore, spec: ExperimentSpec) -> str:
    identifier = pending_attempt(store, spec)
    attempt = store.attempt(identifier)
    code = store.archive_bytes(
        f"experiments/{spec.experiment_id}/{identifier}/analysis.do",
        b"clear\nset obs 2\ngen x=_n\nsummarize x\n",
        kind="code",
        design_id=spec.design_id,
        attempt_id=identifier,
        tool_call_id=attempt.tool_call_id,
    )
    store.start_attempt(
        identifier,
        code.artifact_id,
        service_instance_id="service_test",
        session_id="session_test",
        backend_task_id="task_test",
    )
    return identifier


def normalized(value: float = 1.5) -> ScalarResult:
    return ScalarResult(
        sample_size=2,
        sample_note="合成样本",
        actual_command="summarize",
        estimator="测试均值",
        variables=("x",),
        sample_rule="全部两行",
        standard_errors=StandardErrors(treatment="不适用", explanation="测试标量不报告标准误"),
        values=(ScalarValue(name="mean", value=value, meaning="测试均值"),),
    )


def successful_result(store: RunStore, spec: ExperimentSpec) -> Artifact:
    identifier = started_attempt(store, spec)
    attempt = store.attempt(identifier)
    raw = store.archive_bytes(
        f"experiments/{spec.experiment_id}/{identifier}/results.raw.json",
        b'{"synthetic":true,"mean":1.5}',
        kind="raw_result",
        design_id=spec.design_id,
        attempt_id=identifier,
        tool_call_id=attempt.tool_call_id,
    )
    store.finish_attempt(
        identifier,
        ExecutionStatus.SUCCEEDED,
        executor_id=store.executor_id,
        termination_confirmed=True,
        duration_seconds=0.01,
        raw_artifact_id=raw.artifact_id,
    )
    return store.save_result(identifier, normalized())


def checks(result_id: str, *, specification_status: str = "pass") -> tuple[ValidationCheck, ...]:
    return (
        ValidationCheck(
            category="computation",
            name="合成数值一致",
            status="pass",
            assessor="program",
            explanation="仅用于验证状态约束",
            evidence_artifact_ids=(result_id,),
        ),
        ValidationCheck.model_validate(
            {
                "category": "specification",
                "name": "合成规格一致",
                "status": specification_status,
                "assessor": "program",
                "explanation": "检查确切版本绑定",
                "evidence_artifact_ids": [result_id],
            }
        ),
    )


def test_nine_tables_identity_and_reopen(store: RunStore) -> None:
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == TABLES
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    state = store.load_state()
    task = store.load_task()
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.load_state() == state
        assert reopened.load_task() == task
        assert reopened.recovery_report.issues == ()
    with pytest.raises(FileExistsError):
        RunStore.create(path.parent, task, run_id=path.name)


def test_backend_and_foreign_keys_are_enforced(store: RunStore) -> None:
    with pytest.raises(ValidationError):
        ResearchTask.model_validate(store.load_task().model_dump() | {"analysis_backend": "python"})
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tool_calls(tool_call_id,run_id,operation_id,retry_no,"
                "source_model_call_id,status,arguments_status,payload_json,created_at) "
                "VALUES('tool_bad',?,'op_bad',0,'missing','PENDING','pass','{}',?)",
                (store.run_id, utc_now()),
            )
    with pytest.raises(StateConflict):
        store.record_tool_call(
            ToolCall(
                tool_call_id="tool_other",
                run_id="run_other",
                operation_id="op_other",
                tool_name="inspect_data",
            )
        )


def test_human_answer_idempotent_and_bound_to_version(store: RunStore) -> None:
    first = confirmed(store)
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        request_id = connection.execute("SELECT request_id FROM human_requests").fetchone()[0]
    old_events = len(store.events())
    answer = HumanAnswer(decision="accept", explanation="测试确认")
    store.apply_answer(request_id, design_sha256=content_hash(first), answer=answer)
    assert len(store.events()) == old_events
    second = store.create_design(design_content(), change_reason="增加新的测试版本")
    store.request_human(second.design_id, "确认第二版吗？")
    store.apply_answer(request_id, design_sha256=content_hash(first), answer=answer)
    assert store.load_state().status == RunStatus.WAITING_HUMAN
    assert store.load_state().current_design_id == first.design_id
    assert len(store.load_state().pending_request_ids) == 1
    with pytest.raises(StateConflict):
        store.apply_answer(
            request_id,
            design_sha256=content_hash(first),
            answer=HumanAnswer(decision="reject", explanation="不能改写已应用答复"),
        )


def test_superseded_pending_answer_cannot_approve_new_design(store: RunStore) -> None:
    store.transition_run(RunStatus.RUNNING)
    first = store.create_design(design_content(), change_reason="第一版")
    old = store.request_human(first.design_id, "确认第一版吗？")
    second = store.create_design(design_content(), change_reason="新版本替代未决第一版")
    new = store.request_human(second.design_id, "确认第二版吗？")
    answer = HumanAnswer(decision="accept", explanation="测试确认")
    with pytest.raises(StateConflict):
        store.apply_answer(old.request_id, design_sha256=content_hash(first), answer=answer)
    with pytest.raises(StateConflict):
        store.apply_answer(new.request_id, design_sha256=content_hash(first), answer=answer)
    assert store.load_state().current_design_id is None
    store.apply_answer(new.request_id, design_sha256=content_hash(second), answer=answer)
    assert store.load_state().current_design_id == second.design_id


def test_answer_transaction_rolls_back_all_state(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.transition_run(RunStatus.RUNNING)
    design = store.create_design(design_content(), change_reason="故障注入测试")
    request = store.request_human(design.design_id, "确认吗？")
    previous = store.load_state()

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("模拟写事件前中断")

    monkeypatch.setattr(store, "_event", crash)
    with pytest.raises(RuntimeError):
        store.apply_answer(
            request.request_id,
            design_sha256=content_hash(design),
            answer=HumanAnswer(decision="accept", explanation="测试"),
        )
    assert store.load_state() == previous
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        assert connection.execute("SELECT confirmation FROM designs").fetchone()[0] == "PROPOSED"
        assert connection.execute("SELECT status,answerED_at FROM human_requests").fetchone() == (
            "PENDING",
            None,
        )


def test_pause_and_resume_preserve_pending_decision(store: RunStore) -> None:
    store.transition_run(RunStatus.RUNNING)
    design = store.create_design(design_content(), change_reason="等待确认")
    request = store.request_human(design.design_id, "确认吗？")
    store.apply_answer(
        request.request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="pause", explanation="稍后答复"),
    )
    assert store.load_state().pending_request_ids == (request.request_id,)
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        reopened.transition_run(RunStatus.RUNNING)
        assert reopened.load_state().status == RunStatus.WAITING_HUMAN
        assert reopened.load_state().current_design_id is None


def test_immutable_designs_artifacts_and_events(store: RunStore) -> None:
    confirmed(store)
    artifact = store.archive_bytes("inputs/example.csv", b"x\n1", kind="input")
    with pytest.raises(StateConflict):
        store.archive_bytes("inputs/example.csv", b"changed", kind="input")
    assert store.read_artifact(artifact.artifact_id) == b"x\n1"
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        for sql in [
            "UPDATE designs SET payload_json='{}'",
            "UPDATE artifacts SET byte_size=0",
            "DELETE FROM events",
            "DELETE FROM designs",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)


@pytest.mark.parametrize("path", ["../outside.txt", "/absolute.txt", "state.sqlite", ".run.lock"])
def test_archive_rejects_unsafe_paths(store: RunStore, path: str) -> None:
    with pytest.raises(ValidationError):
        store.archive_bytes(path, b"test", kind="input")


def test_archive_rejects_symlink_parent(store: RunStore, tmp_path: Path) -> None:
    (store.directory / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactIntegrityError):
        store.archive_bytes("linked/outside.txt", b"test", kind="input")
    assert not (tmp_path / "outside.txt").exists()


def test_file_published_before_database_commit_is_recovered(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(artifact: Artifact) -> None:
        raise RuntimeError("模拟文件发布后数据库提交前中断")

    monkeypatch.setattr(store, "_insert_artifact", crash)
    with pytest.raises(RuntimeError):
        store.archive_bytes("inputs/recover.csv", b"x\n1", kind="input")
    assert (store.directory / "inputs/recover.csv").read_bytes() == b"x\n1"
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert len(reopened.recovery_report.recovered_artifact_ids) == 1
        identifier = reopened.recovery_report.recovered_artifact_ids[0]
        assert reopened.read_artifact(identifier) == b"x\n1"
        assert reopened.load_state().completed_experiment_ids == ()
        assert reopened.recovery_report.issues == ()


def test_committed_artifact_with_leftover_journal_is_idempotent(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.unlink

    def crash(path: Path, missing_ok: bool = False) -> None:
        if path.parent.name == ".archive_pending":
            raise OSError("模拟数据库提交后的清理中断")
        original(path, missing_ok=missing_ok)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", crash)
        with pytest.raises(OSError):
            store.archive_bytes("inputs/committed.csv", b"x\n1", kind="input")
    count = len(store.events())
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.recovery_report.recovered_artifact_ids == ()
        assert reopened.recovery_report.issues == ()
        assert len(reopened.events()) == count
        assert list((path / ".archive_pending").glob("*.json")) == []


def test_orphan_file_never_becomes_success(store: RunStore) -> None:
    (store.directory / "unindexed.json").write_text('{"execution_status":"SUCCEEDED"}')
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.load_state().status == RunStatus.PAUSED
        assert reopened.load_state().completed_experiment_ids == ()
        assert any("未登记" in issue for issue in reopened.recovery_report.issues)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_missing_or_changed_archived_file_pauses_recovery(store: RunStore, damage: str) -> None:
    artifact = store.archive_bytes("inputs/integrity.csv", b"x\n1", kind="input")
    path = store.directory / artifact.relative_path
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x\n9")
    directory = store.directory
    store.close()
    with RunStore.open(directory) as reopened:
        assert reopened.load_state().status == RunStatus.PAUSED
        assert reopened.recovery_report.issues
        with pytest.raises(ArtifactIntegrityError):
            reopened.read_artifact(artifact.artifact_id)


def test_changed_original_input_requires_new_confirmation(store: RunStore) -> None:
    source = store.load_task().data_path
    source.write_text("x,y\n1,99\n")
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.load_state().status == RunStatus.PAUSED
        with pytest.raises(ArtifactIntegrityError):
            reopened.transition_run(RunStatus.RUNNING)
        assert source.read_text() == "x,y\n1,99\n"


def test_future_database_is_not_modified(store: RunStore) -> None:
    path = store.directory
    store.close()
    with sqlite3.connect(path / "state.sqlite") as connection:
        connection.execute("PRAGMA user_version=2")
    for writable in (True, False):
        with pytest.raises(FutureVersionError):
            RunStore.open(path, writable=writable)
    with sqlite3.connect(path / "state.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_directory_identity_mismatch_is_rejected(store: RunStore) -> None:
    path = store.directory
    store.close()
    moved = path.with_name("different_run")
    path.rename(moved)
    with pytest.raises(ValueError):
        RunStore.open(moved)


def test_writer_lock_and_concurrent_reader(store: RunStore) -> None:
    with pytest.raises(RunLockedError):
        RunStore.open(store.directory)
    with RunStore.open(store.directory, writable=False) as reader:
        assert reader.load_state() == store.load_state()
        with pytest.raises(StateConflict):
            reader.transition_run(RunStatus.RUNNING)
    code = """
import sys
from pathlib import Path
from econ_agent.state import RunStore,RunLockedError
try:
    RunStore.open(Path(sys.argv[1]))
except RunLockedError:
    print('已拒绝第二个执行者')
    sys.exit(0)
sys.exit(2)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(store.directory)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 0
    assert "已拒绝第二个执行者" in process.stdout


def test_lock_released_when_process_exits(store: RunStore) -> None:
    path = store.directory
    store.close()
    code = """
import sys,os
from pathlib import Path
from econ_agent.state import RunStore
store=RunStore.open(Path(sys.argv[1]))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(path)], timeout=10, check=True)
    with RunStore.open(path) as reopened:
        assert reopened.load_state().run_id == path.name


def test_execution_extraction_and_validation_are_separate(store: RunStore) -> None:
    spec = experiment(store)
    result = successful_result(store, spec)
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, result.artifact_id)
    store.save_validation(
        result.artifact_id, checks(result.artifact_id, specification_status="pending")
    )
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, result.artifact_id)
    store.save_validation(result.artifact_id, checks(result.artifact_id))
    store.select_result(spec.experiment_id, result.artifact_id)
    saved = store.selected_results()
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.selected_results() == saved
        assert reopened.result(result.artifact_id).analysis_backend == "stata"


def test_model_cannot_substitute_deterministic_validation(store: RunStore) -> None:
    spec = experiment(store)
    result = successful_result(store, spec)
    model_checks = tuple(
        c.model_copy(update={"assessor": "model"}) for c in checks(result.artifact_id)
    )
    store.save_validation(result.artifact_id, model_checks)
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, result.artifact_id)


def test_method_blocking_prevents_selection(store: RunStore) -> None:
    spec = experiment(store)
    result = successful_result(store, spec)
    store.save_validation(result.artifact_id, checks(result.artifact_id), method_blocking=True)
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, result.artifact_id)


def test_reextraction_keeps_old_result_and_invalidates_selection(store: RunStore) -> None:
    spec = experiment(store)
    old = successful_result(store, spec)
    store.save_validation(old.artifact_id, checks(old.artifact_id))
    store.select_result(spec.experiment_id, old.artifact_id)
    assert old.attempt_id is not None
    new = store.save_result(old.attempt_id, normalized(1.6))
    assert new.artifact_id != old.artifact_id
    assert store.result(old.artifact_id).normalized is not None
    assert store.selected_results() == ()
    with pytest.raises(StateConflict):
        store.select_result(spec.experiment_id, new.artifact_id)
    with pytest.raises(StateConflict):
        store.save_validation(old.artifact_id, checks(old.artifact_id))


def test_old_design_and_wrong_experiment_cannot_use_result(store: RunStore) -> None:
    first = experiment(store)
    result = successful_result(store, first)
    store.save_validation(result.artifact_id, checks(result.artifact_id))
    second = experiment(store, identifier="experiment_2")
    with pytest.raises(StateConflict):
        store.select_result(second.experiment_id, result.artifact_id)
    store.select_result(first.experiment_id, result.artifact_id)
    confirmed(store)
    assert store.selected_results() == ()
    with pytest.raises(StateConflict):
        store.select_result(first.experiment_id, result.artifact_id)


def test_cancelled_late_success_is_rejected_and_retry_has_new_identity(store: RunStore) -> None:
    spec = experiment(store)
    identifier = started_attempt(store, spec)
    store.request_cancel(identifier)
    with pytest.raises(StateConflict):
        store.finish_attempt(
            identifier,
            ExecutionStatus.SUCCEEDED,
            executor_id=store.executor_id,
            termination_confirmed=True,
            duration_seconds=0.1,
        )
    store.finish_attempt(
        identifier,
        ExecutionStatus.CANCELLED,
        executor_id=store.executor_id,
        termination_confirmed=True,
        duration_seconds=0.1,
    )
    second = pending_attempt(store, spec)
    assert second != identifier
    assert store.attempt(second).attempt_no == 2
    assert store.attempt(identifier).cancel_requested is True


def test_recovery_does_not_restart_unknown_backend_task(store: RunStore) -> None:
    spec = experiment(store)
    identifier = started_attempt(store, spec)
    owner = store.executor_id
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        attempt = reopened.attempt(identifier)
        assert attempt.execution_status == ExecutionStatus.INTERRUPTED
        assert attempt.service_instance_id == "service_test"
        assert attempt.backend_task_id == "task_test"
        assert attempt.termination_confirmed is False
        assert reopened.load_state().status == RunStatus.PAUSED
        with pytest.raises(StateConflict):
            reopened.transition_run(RunStatus.RUNNING)
        with pytest.raises(StateConflict):
            reopened.finish_attempt(
                identifier,
                ExecutionStatus.SUCCEEDED,
                executor_id=owner,
                termination_confirmed=True,
                duration_seconds=0.1,
            )
        reopened.confirm_interrupted_stopped(identifier)
        reopened.transition_run(RunStatus.RUNNING)
        second = pending_attempt(reopened, spec)
        assert reopened.attempt(second).attempt_no == 2
        assert reopened.tool_call(attempt.tool_call_id).status == CallStatus.UNKNOWN


def test_successful_attempt_is_not_automatically_rerun(store: RunStore) -> None:
    spec = experiment(store)
    successful_result(store, spec)
    with pytest.raises(StateConflict):
        pending_attempt(store, spec)
    second = pending_attempt(store, spec, rerun_reason="明确要求重新统计执行的测试")
    assert store.attempt(second).attempt_no == 2


def model_call(store: RunStore, *, identifier: str = "model_1", retry: int = 0) -> ModelCall:
    return ModelCall(
        model_call_id=identifier,
        run_id=store.run_id,
        operation_id="operation_models",
        retry_no=retry,
        purpose="离线调用状态测试",
        provider="测试服务",
        requested_model="测试模型",
        prompt_version="zh_v1",
        config_version="1",
    )


def test_call_retry_uniqueness_usage_unknown_and_parse_status(store: RunStore) -> None:
    store.transition_run(RunStatus.RUNNING)
    first = model_call(store)
    store.record_model_call(first)
    with pytest.raises(StateConflict):
        store.record_model_call(model_call(store, identifier="model_duplicate"))
    request = store.archive_bytes(
        "calls/model/model_1/request.json",
        b'{"language":"zh-CN"}',
        kind="model_request",
        model_call_id=first.model_call_id,
    )
    first = first.model_copy(update={"request_artifact_id": request.artifact_id})
    store.update_model_call(first, expected=CallStatus.PENDING)
    first = first.model_copy(update={"status": CallStatus.RUNNING, "started_at": utc_now()})
    store.update_model_call(first, expected=CallStatus.PENDING)
    response = store.archive_bytes(
        "calls/model/model_1/response.json",
        b'{"synthetic":true}',
        kind="model_response",
        model_call_id=first.model_call_id,
    )
    first = first.model_copy(
        update={
            "status": CallStatus.SUCCEEDED,
            "response_artifact_id": response.artifact_id,
            "ended_at": utc_now(),
            "duration_seconds": 0.1,
            "total_tokens": 15,
            "cached_tokens": 5,
            "parse_status": ParseStatus.PENDING,
        }
    )
    store.update_model_call(first, expected=CallStatus.RUNNING)
    first = first.model_copy(update={"parse_status": ParseStatus.INVALID})
    store.update_model_call(first, expected=CallStatus.SUCCEEDED)
    store.record_model_call(model_call(store, identifier="model_2", retry=1))
    summary = store.call_summary()
    assert summary["model_calls"] == 2
    assert summary["known_total_tokens"] == 15
    assert summary["missing_total_usage_calls"] == 1
    assert summary["model_parse_failures"] == 1
    assert summary["model_retries"] == 1
    assert summary["model_transport_failures"] == 0
    with pytest.raises(StateConflict):
        store.record_tool_call(
            ToolCall(
                tool_call_id="tool_bad",
                run_id=store.run_id,
                operation_id="bad_operation",
                source_model_call_id=first.model_call_id,
                tool_name="inspect_data",
            )
        )


def test_call_artifact_cannot_belong_to_another_call(store: RunStore) -> None:
    store.transition_run(RunStatus.RUNNING)
    first = model_call(store)
    second = model_call(store, identifier="model_2", retry=1)
    store.record_model_call(first)
    store.record_model_call(second)
    artifact = store.archive_bytes(
        "calls/first.json", b"{}", kind="model_request", model_call_id=first.model_call_id
    )
    with pytest.raises(StateConflict):
        store.update_model_call(
            second.model_copy(update={"request_artifact_id": artifact.artifact_id}),
            expected=CallStatus.PENDING,
        )


def test_credentials_rejected_before_run_creation(store: RunStore, tmp_path: Path) -> None:
    with pytest.raises(StateConflict):
        RunStore.create(
            tmp_path / "runs",
            store.load_task(),
            run_id="secret_run",
            config={"nested": {"api_key": "测试密钥"}},
        )
    assert not (tmp_path / "runs/secret_run").exists()


def test_selected_result_rechecks_file_integrity(store: RunStore) -> None:
    spec = experiment(store)
    result = successful_result(store, spec)
    store.save_validation(result.artifact_id, checks(result.artifact_id))
    store.select_result(spec.experiment_id, result.artifact_id)
    assert store.selected_results()
    (store.directory / result.relative_path).write_text('{"schema_version":1}')
    with pytest.raises(ArtifactIntegrityError):
        store.selected_results()


def test_pause_marks_inflight_attempt_for_cancellation(store: RunStore) -> None:
    spec = experiment(store)
    identifier = started_attempt(store, spec)
    store.transition_run(RunStatus.PAUSED)
    assert store.attempt(identifier).cancel_requested
    with pytest.raises(StateConflict):
        store.finish_attempt(
            identifier,
            ExecutionStatus.SUCCEEDED,
            executor_id=store.executor_id,
            termination_confirmed=True,
            duration_seconds=0.1,
        )
    store.finish_attempt(
        identifier,
        ExecutionStatus.CANCELLED,
        executor_id=store.executor_id,
        termination_confirmed=True,
        duration_seconds=0.1,
    )
    store.transition_run(RunStatus.RUNNING)


def test_pending_cancellation_preserves_record_without_execution(store: RunStore) -> None:
    spec = experiment(store)
    identifier = pending_attempt(store, spec)
    store.request_cancel(identifier)
    attempt = store.attempt(identifier)
    assert attempt.execution_status == ExecutionStatus.CANCELLED
    assert attempt.code_artifact_id is None
    assert attempt.termination_confirmed
    assert store.tool_call(attempt.tool_call_id).status == CallStatus.CANCELLED
    assert store.attempt(pending_attempt(store, spec)).attempt_no == 2


def test_sql_status_ranges_and_attempt_number_unique(store: RunStore) -> None:
    spec = experiment(store)
    pending_attempt(store, spec)
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        for sql in [
            "UPDATE runs SET status='INVALID'",
            "UPDATE attempts SET attempt_no=0",
            "UPDATE attempts SET execution_status='INVALID'",
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)


def test_readonly_does_not_initialize_old_or_empty_database(tmp_path: Path) -> None:
    directory = tmp_path / "run_empty"
    directory.mkdir()
    with sqlite3.connect(directory / "state.sqlite"):
        pass
    with pytest.raises(ValueError):
        RunStore.open(directory, writable=False)
    with sqlite3.connect(directory / "state.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
            == 0
        )


def test_unknown_archive_intent_cannot_register_artifact(store: RunStore) -> None:
    pending = store.directory / ".archive_pending"
    record = Artifact(
        artifact_id="artifact_unknown",
        run_id=store.run_id,
        kind="code",
        relative_path="experiments/unknown/code.do",
        sha256="a" * 64,
        byte_size=1,
        design_id="missing_design",
        attempt_id="missing_attempt",
        tool_call_id="missing_call",
    )
    (pending / "artifact_unknown.json").write_text(record.model_dump_json())
    path = store.directory
    store.close()
    with RunStore.open(path) as reopened:
        assert reopened.recovery_report.recovered_artifact_ids == ()
        assert reopened.recovery_report.issues
        assert reopened.load_state().status == RunStatus.PAUSED


def test_completion_requires_evidence_and_run_transition_compares_old_state(
    store: RunStore,
) -> None:
    store.transition_run(RunStatus.RUNNING)
    with pytest.raises(StateConflict):
        store.transition_run(RunStatus.COMPLETED)
    with pytest.raises(StateConflict):
        store.transition_run(RunStatus.PAUSED, expected=RunStatus.NEW)
    assert store.load_state().status == RunStatus.RUNNING


def test_call_budget_counts_pending_and_failed_requests(store: RunStore, tmp_path: Path) -> None:
    task = ResearchTask.model_validate(
        store.load_task().model_dump()
        | {"budget": {"max_research_rounds": 3, "max_model_calls": 1}}
    )
    with RunStore.create(tmp_path / "runs", task, run_id="run_budget") as limited:
        limited.transition_run(RunStatus.RUNNING)
        call = model_call(limited)
        limited.record_model_call(call)
        limited.update_model_call(
            call.model_copy(update={"status": CallStatus.FAILED}), expected=CallStatus.PENDING
        )
        with pytest.raises(StateConflict):
            limited.record_model_call(model_call(limited, identifier="model_retry", retry=1))
        assert limited.call_summary()["known_total_tokens"] is None


def test_confirmed_design_cannot_be_decided_again(store: RunStore) -> None:
    design = confirmed(store)
    with pytest.raises(StateConflict):
        store.request_human(design.design_id, "重复审批会破坏已有设计吗？")
    assert store.load_state().status == RunStatus.RUNNING
    assert store.load_state().current_design_id == design.design_id
    assert store.load_state().pending_request_ids == ()


def test_modified_call_object_is_revalidated_before_registration(store: RunStore) -> None:
    store.transition_run(RunStatus.RUNNING)
    invalid = model_call(store).model_copy(update={"schema_version": 2})
    with pytest.raises(ValidationError):
        store.record_model_call(invalid)
    assert store._one("SELECT COUNT(*) FROM model_calls")[0] == 0


def test_environment_manifest_is_versioned_and_preserved(store: RunStore) -> None:
    with sqlite3.connect(store.directory / "state.sqlite") as connection:
        saved = connection.execute("SELECT environment_json FROM runs").fetchone()[0]
    metadata = RunEnvironment.model_validate_json(saved)
    assert metadata.schema_version == 1
    assert metadata.application_version == "0.1.0rc1"
    assert metadata.python_version == sys.version.split()[0]
    assert metadata.analysis_backend == "stata"
    assert metadata.interaction_language == "zh-CN"
    assert metadata.requested_model is None
    assert metadata.stata_version is None
    directory = store.directory
    store.close()
    with RunStore.open(directory) as reopened:
        archived = next(
            row[0]
            for row in reopened._all("SELECT artifact_id FROM artifacts WHERE kind='environment'")
        )
        assert RunEnvironment.model_validate_json(reopened.read_artifact(archived)) == metadata
