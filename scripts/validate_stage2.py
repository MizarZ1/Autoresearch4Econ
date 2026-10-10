"""显式真实阶段 2 验收；固定方案仅供开发者，不伪装为自主研究循环。"""

import asyncio
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from econ_agent.inputs import prepare_input
from econ_agent.schemas import (
    CoefficientResult,
    DesignContent,
    ExecutionStatus,
    ExperimentSpec,
    HumanAnswer,
    ResearchTask,
    RunStatus,
    ToolCall,
    ValidationCheck,
    ValidationRecord,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import AnalysisOutcome, IVSpecification, StataProgram
from econ_agent.stata_mcp import StataMCPTransport
from econ_agent.state import RunStore, new_id

ROOT = Path(__file__).resolve().parents[1]


def new_attempt(
    store: RunStore,
    spec: ExperimentSpec,
    *,
    reason: str | None = None,
    previous_attempt_id: str | None = None,
) -> str:
    previous = (
        store.tool_call(store.attempt(previous_attempt_id).tool_call_id)
        if previous_attempt_id
        else None
    )
    call = ToolCall(
        tool_call_id=new_id("tool"),
        run_id=store.run_id,
        operation_id=previous.operation_id if previous else new_id("operation"),
        retry_no=previous.retry_no + 1 if previous else 0,
        tool_name="run_analysis",
        arguments_status="pass",
    )
    store.record_tool_call(call)
    return store.create_attempt(
        spec.experiment_id, call.tool_call_id, rerun_reason=reason
    ).attempt_id


def check_reference(store: RunStore, outcome: AnalysisOutcome, specification: str) -> str:
    """验收器单独访问阶段 0 参考；这些数值不进入生产模型材料。"""
    result = outcome.result
    assert result.result_id is not None and isinstance(result.normalized, CoefficientResult)
    assert outcome.validation_artifact_id is not None
    reference = json.loads((ROOT / "examples/demo/reference/independent_stata18.json").read_text())
    assert reference["data_sha256"] == result.input_sha256
    rows = [
        r
        for r in reference["estimates"]
        if r["specification"] == specification and r["estimator"] == "ivregress"
    ]
    assert {r["parameter"] for r in rows} == {e.name for e in result.normalized.estimates}
    for estimate in result.normalized.estimates:
        expected = next(r for r in rows if r["parameter"] == estimate.name)
        for actual, key in (
            (estimate.value, "coefficient"),
            (estimate.standard_error, "standard_error"),
            (estimate.degrees_of_freedom, "df_r"),
        ):
            assert actual is not None and math.isclose(
                actual, float(expected[key]), rel_tol=1e-6, abs_tol=1e-7
            )
        assert result.normalized.sample_size == int(float(expected["n"])) == 64
    attempt = store.attempt(result.attempt_id)
    spec = store.experiment(result.experiment_id)
    artifact = store.archive_bytes(
        f"experiments/{spec.experiment_id}/{attempt.attempt_id}/reference/{new_id('comparison')}.json",
        json.dumps(
            {
                "schema_version": 1,
                "说明": "独立参考数值只供开发验收",
                "reference_code_sha256": reference["reference_code_sha256"],
                "rows": rows,
                "result_id": result.result_id,
                "absolute_tolerance": 1e-7,
                "relative_tolerance": 1e-6,
            },
            ensure_ascii=False,
        ).encode(),
        kind="raw_result",
        attempt_id=attempt.attempt_id,
        design_id=spec.design_id,
        tool_call_id=attempt.tool_call_id,
    )
    old = ValidationRecord.model_validate_json(store.read_artifact(outcome.validation_artifact_id))
    validation = store.save_validation(
        result.result_id,
        (
            *old.checks,
            ValidationCheck(
                category="computation",
                name="阶段零独立 Stata 数值对照",
                status="pass",
                assessor="program",
                explanation="同数据及规格的系数、标准误和自由度通过既定容差，N=64；不据此证明识别假设",
                evidence_artifact_ids=(artifact.artifact_id, result.result_id),
            ),
        ),
        rules_version="stata-iv-1+ajr-reference-1",
    )
    store.select_result(spec.experiment_id, result.result_id)
    return validation.artifact_id


async def main() -> int:
    task = ResearchTask.from_file(ROOT / "examples/demo/task.json")
    before = hashlib.sha256(task.data_path.read_bytes()).hexdigest()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    evidence = ROOT / "local_reference/stage2" / stamp
    evidence.mkdir(parents=True)
    record: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "cases": {},
        "fixed_developer_scenario": True,
    }
    record["code_sha256"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [Path(__file__), *sorted((ROOT / "src/econ_agent").glob("*.py"))]
    }
    executable = Path(
        os.getenv(
            "STATA_PATH", str(ROOT / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp")
        )
    )
    with RunStore.create(ROOT / "runs", task, run_id=f"run_stage2_{stamp}") as store:
        record["run_id"] = store.run_id
        prepared = prepare_input(store, ROOT / "backups")
        store.transition_run(RunStatus.RUNNING)
        design = store.create_design(
            DesignContent(
                population="AJR 作者整理的国家分析数据",
                variables={
                    "logpgp95": "1995 年人均收入对数",
                    "avexpr": "保护指标",
                    "logem4": "定居者死亡率对数",
                    "lat_abst": "绝对纬度控制",
                },
                sample_rule="baseco==1",
                estimand="制度指标的基准与加入纬度控制的 2SLS 比较",
                methods=("ivregress 2sls", "summarize", "通用执行验证"),
                assumptions=("工具外生性与排除限制需结合背景解释，数值吻合不证明这些条件",),
            ),
            change_reason="阶段 2 开发验收的固定规格，不作为真实人工交互演示",
        )
        request = store.request_human(design.design_id, "按预先授权的 AJR 验收卡确认固定开发规格")
        store.apply_answer(
            request.request_id,
            design_sha256=content_hash(design),
            answer=HumanAnswer(
                decision="accept", explanation="固定开发验收夹具；真实人工修改闭环留待后续阶段"
            ),
        )
        async with StataMCPTransport(executable, ROOT, evidence / "mcp_server.log") as transport:
            backend = StataBackend(
                store, transport, prepared, ROOT / "tmp/stata", timeout_seconds=60
            )
            print("正在执行真实阶段 2 验收：数据检查、帮助、基准 IV。", flush=True)
            inspection = json.loads(await backend.inspect_data(prepared.input_artifact_id))
            record["inspection"] = inspection
            await backend.get_method_help("ivregress")
            capabilities = await backend.capabilities()
            record["capabilities"] = [item.model_dump(mode="json") for item in capabilities]
            baseline_outcome: AnalysisOutcome | None = None
            for name, controls in (("baseline", ()), ("latitude", ("lat_abst",))):
                iv = IVSpecification(
                    outcome="logpgp95",
                    endogenous="avexpr",
                    instruments=("logem4",),
                    controls=controls,
                    sample_expression="baseco==1",
                )
                spec = ExperimentSpec(
                    experiment_id=new_id("experiment"),
                    run_id=store.run_id,
                    design_id=design.design_id,
                    purpose="AJR 基准数值对照" if not controls else "AJR 加入纬度控制的数值对照",
                    initiation="planned",
                    input_artifact_id=prepared.input_artifact_id,
                    method="ivregress 2sls",
                    variables=iv.variables(),
                    sample_rule="baseco==1",
                    expected_result_type="coefficients",
                )
                store.create_experiment(spec)
                if name == "baseline":
                    failed_id = new_attempt(store, spec)
                    failed = await backend.run_analysis(
                        spec,
                        attempt_id=failed_id,
                        program=StataProgram(
                            code="ivregress 2sls absent_var (avexpr=logem4) if baseco==1, small",
                            iv=iv,
                        ),
                    )
                    assert (
                        failed.result.execution_status == ExecutionStatus.FAILED
                        and failed.error is not None
                    )
                    assert failed.error.stata_return_code == 111
                    record["cases"]["syntax_failure"] = failed.model_dump(mode="json")
                attempt_id = new_attempt(
                    store, spec, previous_attempt_id=failed_id if name == "baseline" else None
                )
                outcome = await backend.run_analysis(
                    spec, attempt_id=attempt_id, program=StataProgram(code=iv.command(), iv=iv)
                )
                reference_validation = check_reference(store, outcome, name)
                record["cases"][name] = {
                    "outcome": outcome.model_dump(mode="json"),
                    "reference_validation": reference_validation,
                }
                if name == "baseline":
                    baseline_outcome = outcome
                    assert store.attempt(attempt_id).attempt_no == 2
            assert baseline_outcome is not None
            old_result = baseline_outcome.result.result_id
            regenerated = backend.reextract(baseline_outcome.result.attempt_id)
            assert regenerated.result.result_id != old_result
            check_reference(store, regenerated, "baseline")
            record["reextraction_new_result"] = regenerated.result.result_id
            print("IV 对照通过，正在验证通用命令、真实 CPU 循环的超时和取消。", flush=True)
            generic = ExperimentSpec(
                experiment_id=new_id("experiment"),
                run_id=store.run_id,
                design_id=design.design_id,
                purpose="通用命令和生命周期开发验收",
                initiation="planned",
                input_artifact_id=prepared.input_artifact_id,
                method="通用执行验证",
                variables=("avexpr",),
                sample_rule="baseco==1",
                expected_result_type="scalars",
            )
            store.create_experiment(generic)
            general = await backend.run_analysis(
                generic,
                attempt_id=new_attempt(store, generic),
                program=StataProgram(code="summarize avexpr if baseco==1"),
            )
            assert general.result.execution_status == ExecutionStatus.SUCCEEDED
            assert general.result.normalized is None
            record["cases"]["general_unparsed"] = general.model_dump(mode="json")
            loop_code = (
                "tempname econ_mark\nfile open `econ_mark' using "
                '"$ECON_OUTPUT/computation_started.txt", write text\n'
                "file write `econ_mark' \"started\"\nfile close `econ_mark'\n"
                "scalar econ_counter=0\nwhile 1 {\nscalar econ_counter=econ_counter+1\n}\n"
            )
            # 用新规格隔离技术生命周期验收，不把成功的描述统计自动重跑。
            timed_spec = generic.model_copy(update={"experiment_id": new_id("experiment")})
            store.create_experiment(timed_spec)
            timeout_id = new_attempt(store, timed_spec)
            backend.timeout = 8
            timed = await backend.run_analysis(
                timed_spec, attempt_id=timeout_id, program=StataProgram(code=loop_code)
            )
            assert timed.error is not None and timed.error.category == "timeout"
            assert timed.result.execution_status == ExecutionStatus.CANCELLED
            assert store.attempt(timeout_id).termination_confirmed
            assert (
                ROOT / "tmp/stata" / store.run_id / timeout_id / "output/computation_started.txt"
            ).read_text() == "started"
            record["cases"]["real_timeout"] = timed.model_dump(mode="json")
            backend.timeout = 60
            cancel_id = new_attempt(store, timed_spec)
            pending = asyncio.create_task(
                backend.run_analysis(
                    timed_spec, attempt_id=cancel_id, program=StataProgram(code=loop_code)
                )
            )
            marker = (
                ROOT / "tmp/stata" / store.run_id / cancel_id / "output/computation_started.txt"
            )
            deadline = asyncio.get_running_loop().time() + 30
            try:
                while not marker.exists():
                    if pending.done() or asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("取消验收的 CPU 循环未实际开始")
                    await asyncio.sleep(0.05)
                assert await backend.cancel(cancel_id)
                cancelled = await pending
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            assert cancelled.result.execution_status == ExecutionStatus.CANCELLED
            assert cancelled.error is not None and cancelled.error.category == "cancelled"
            assert store.attempt(cancel_id).termination_confirmed
            record["cases"]["real_cancel"] = cancelled.model_dump(mode="json")
            fresh_id = new_attempt(store, timed_spec)
            fresh = await backend.run_analysis(
                timed_spec,
                attempt_id=fresh_id,
                program=StataProgram(code="summarize avexpr if baseco==1"),
            )
            assert fresh.result.execution_status == ExecutionStatus.SUCCEEDED
            assert store.attempt(fresh_id).session_id != store.attempt(cancel_id).session_id
            record["cases"]["fresh_after_cancel"] = fresh.model_dump(mode="json")
            record["call_summary"] = store.call_summary()
            record["environment"] = transport.environment
        assert len(store.selected_results()) == 2
        record["original_unchanged"] = (
            hashlib.sha256(task.data_path.read_bytes()).hexdigest() == before
        )
        assert record["original_unchanged"]
        directory = store.directory
    with RunStore.open(directory) as recovered:
        assert not recovered.recovery_report.issues
        assert len(recovered.selected_results()) == 2
        record["recovered_selected_results"] = 2
    record.update(status="passed", finished_at=datetime.now(UTC).isoformat())
    # 候选交付只发布核验摘要；数据预览、数据库及原始日志继续留在本地。
    public = {
        k: record[k]
        for k in (
            "schema_version",
            "started_at",
            "finished_at",
            "status",
            "run_id",
            "fixed_developer_scenario",
            "code_sha256",
            "environment",
            "original_unchanged",
        )
    }
    public["cases"] = {
        name: {
            "normalized": record["cases"][name]["outcome"]["result"]["normalized"],
            "reference_validation": record["cases"][name]["reference_validation"],
        }
        for name in ("baseline", "latitude")
    }
    public["lifecycle_passed"] = [
        "syntax_failure",
        "real_timeout",
        "real_cancel",
        "fresh_after_cancel",
    ]
    (ROOT / "docs/evidence/demo_v01_iv.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2) + "\n"
    )
    (evidence / "validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "local_reference/stage2/latest.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "状态": "通过",
                "run_id": record["run_id"],
                "案例": list(record["cases"]),
                "原数据未改变": record["original_unchanged"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
