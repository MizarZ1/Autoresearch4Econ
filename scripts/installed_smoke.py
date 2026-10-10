"""供独立安装环境运行的验收；固定设计与脚本化确认不冒充模型自主研究。"""

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import econ_agent
from econ_agent.inputs import prepare_input
from econ_agent.schemas import (
    DesignContent,
    ExperimentSpec,
    HumanAnswer,
    RegressionSpecification,
    ResearchTask,
    RunStatus,
    SummarySpecification,
    ToolCall,
    ValidationRecord,
    content_hash,
)
from econ_agent.stata_contracts import StataProgram
from econ_agent.state import RunStore, new_id


async def main() -> int:
    parser = argparse.ArgumentParser(description="独立安装环境固定验收")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--execute-stata", action="store_true")
    args = parser.parse_args()
    module = Path(econ_agent.__file__).resolve()
    assert module.is_relative_to(Path(sys.prefix).resolve()), "不是独立安装包"
    resource = module.with_name("method_validation.json")
    assert json.loads(resource.read_text())["validated_scopes"]
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    data = workspace / "synthetic.csv"
    shutil.copyfile(args.data, data)
    record: dict[str, Any] = {
        "application_version": econ_agent.__version__,
        "installed_module": str(module),
        "non_editable": True,
        "method_resource_present": True,
        "fixed_design": True,
        "confirmation": "scripted",
        "model_called": False,
        "cases": {},
    }
    with RunStore.create(
        workspace / "runs",
        ResearchTask(
            data_path=data,
            research_goal="候选安装包固定合成验收，不是科学研究",
            max_research_rounds=2,
        ),
    ) as store:
        record["run_id"] = store.run_id
        prepared = prepare_input(store, workspace / "backups")
        store.transition_run(RunStatus.RUNNING)
        if args.execute_stata:
            from econ_agent.stata_backend import StataBackend
            from econ_agent.stata_mcp import StataMCPTransport

            regression = RegressionSpecification(
                family="linear",
                outcome="ylinear",
                regressors=("x",),
                sample_expression="included==1",
            )
            summary = SummarySpecification(variable="x", sample_expression="included==1")
            design = store.create_design(
                DesignContent(
                    population="自编合成独立观测",
                    variables={"x": "合成解释变量", "ylinear": "合成结果变量"},
                    sample_rule="included==1",
                    estimand="固定工程验收",
                    methods=("regress", "summarize"),
                    assumptions=("合成数据不能用于真实科学结论",),
                ),
                change_reason="候选包固定开发验收",
            )
            question = store.request_human(design.design_id, "固定开发夹具确认")
            store.apply_answer(
                question.request_id,
                design_sha256=content_hash(design),
                answer=HumanAnswer(decision="accept", explanation="脚本化确认"),
            )
            async with StataMCPTransport(
                Path(os.environ["STATA_PATH"]), workspace, workspace / "mcp.log"
            ) as transport:
                backend = StataBackend(
                    store, transport, prepared, workspace / "work", timeout_seconds=60
                )
                for program in (
                    StataProgram(code=regression.command(), regression=regression),
                    StataProgram(code=summary.command(), summary=summary),
                ):
                    rule = program.regression or program.summary
                    assert rule is not None
                    method = "regress" if program.regression else "summarize"
                    spec = ExperimentSpec(
                        experiment_id=new_id("experiment"),
                        run_id=store.run_id,
                        design_id=design.design_id,
                        purpose="独立安装包 " + method,
                        initiation="planned",
                        input_artifact_id=prepared.input_artifact_id,
                        method=method,
                        variables=rule.variables(),
                        sample_rule=rule.sample_expression,
                        expected_result_type="coefficients" if program.regression else "scalars",
                    )
                    store.create_experiment(spec)
                    call = ToolCall(
                        tool_call_id=new_id("tool"),
                        run_id=store.run_id,
                        operation_id=new_id("operation"),
                        tool_name="run_analysis",
                        arguments_status="pass",
                    )
                    store.record_tool_call(call)
                    attempt = store.create_attempt(spec.experiment_id, call.tool_call_id)
                    outcome = await backend.run_analysis(
                        spec, attempt_id=attempt.attempt_id, program=program
                    )
                    assert outcome.error is None, outcome.error
                    assert outcome.validation_artifact_id and outcome.result.result_id
                    assert ValidationRecord.model_validate_json(
                        store.read_artifact(outcome.validation_artifact_id)
                    ).eligible()
                    store.select_result(spec.experiment_id, outcome.result.result_id)
                    record["cases"][method] = (
                        outcome.result.normalized.model_dump(mode="json")
                        if outcome.result.normalized
                        else None
                    )
                record["environment"] = {
                    k: transport.environment[k]
                    for k in ("mcp_version", "stata_build", "stata_library_sha256")
                }
        store.transition_run(RunStatus.PAUSED, reason="独立安装包验收完成；没有请求模型写作")
    record["status"] = "passed"
    (workspace / "installation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {k: record[k] for k in ("status", "application_version", "run_id", "model_called")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
