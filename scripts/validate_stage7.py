"""显式真实方法验收：独立原生参考、生产 MCP 映射和迁移后的复现包。"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from econ_agent.exporting import export_run
from econ_agent.inputs import prepare_input
from econ_agent.reporting import result_tables
from econ_agent.schemas import (
    Budget,
    CoefficientResult,
    DesignContent,
    DiDStructure,
    ExperimentSpec,
    HumanAnswer,
    RDDStructure,
    RegressionSpecification,
    ResearchTask,
    RunStatus,
    ScalarResult,
    SummarySpecification,
    ToolCall,
    ValidationRecord,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import StataProgram
from econ_agent.stata_mcp import StataMCPTransport
from econ_agent.state import RunStore, StateConflict, new_id

ROOT = Path(__file__).resolve().parents[1]


def new_attempt(store: RunStore, spec: ExperimentSpec) -> str:
    call = ToolCall(
        tool_call_id=new_id("tool"),
        run_id=store.run_id,
        operation_id=new_id("operation"),
        tool_name="run_analysis",
        arguments_status="pass",
    )
    store.record_tool_call(call)
    return store.create_attempt(spec.experiment_id, call.tool_call_id).attempt_id


def synthetic_data(path: Path) -> str:
    """固定种子的自编重复截面；与 AJR 作者数据完全分开。"""
    rng = random.Random(2001)
    columns = (
        "x",
        "ylinear",
        "group",
        "post",
        "did",
        "ydid",
        "running",
        "centered",
        "treated",
        "interaction",
        "yrdd",
        "binary",
        "included",
    )
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        successes = (8, 20, 40, 60, 72)
        for i in range(400):
            x = i // 80 - 2
            group, post = i % 2, (i // 2) % 2
            running = -1 + 2 * (i + 0.5) / 400
            treated = int(running >= 0)
            interaction = treated * running
            writer.writerow(
                (
                    x,
                    "" if i == 0 else 1.5 + 2 * x + rng.gauss(0, 0.3),
                    group,
                    post,
                    group * post,
                    3 + 0.7 * group + 0.2 * post + 1.8 * group * post + rng.gauss(0, 0.3),
                    running,
                    running,
                    treated,
                    interaction,
                    2 + 3 * treated + 0.5 * running - 0.8 * interaction + rng.gauss(0, 0.2),
                    int(i % 80 < successes[x + 2]),
                    1,
                )
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cases() -> dict[str, StataProgram]:
    rules = {
        "linear": RegressionSpecification(
            family="linear", outcome="ylinear", regressors=("x",), sample_expression="included==1"
        ),
        "did": RegressionSpecification(
            family="did",
            outcome="ydid",
            regressors=("group", "post", "did"),
            sample_expression="included==1",
            covariance="robust",
            did=DiDStructure(treatment="group", post="post", interaction="did"),
        ),
        "rdd": RegressionSpecification(
            family="rdd",
            outcome="yrdd",
            regressors=("treated", "centered", "interaction"),
            sample_expression="abs(running-0)<=0.5",
            covariance="robust",
            rdd=RDDStructure(
                running="running",
                centered="centered",
                treatment="treated",
                interaction="interaction",
                bandwidth=0.5,
            ),
        ),
        "logit": RegressionSpecification(
            family="logit", outcome="binary", regressors=("x",), sample_expression="included==1"
        ),
    }
    programs = {
        name: StataProgram(code=rule.command(), regression=rule) for name, rule in rules.items()
    }
    summary = SummarySpecification(variable="x", sample_expression="included==1")
    programs["summary"] = StataProgram(code=summary.command(), summary=summary)
    return programs


def native(code: str, directory: Path, log_name: str) -> None:
    """子进程只接收分析必需环境；不继承模型密钥，使用本机既有 PyStata。"""
    child = """import sys
sys.path.insert(0,sys.argv[1]+"/utilities")
from pystata import config
config.init("mp",splash=False)
from pystata import stata
stata.run(sys.argv[2],quietly=True)
"""
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    with (directory / log_name).open("w") as stream:
        subprocess.run(
            [sys.executable, "-c", child, str(ROOT / "local_reference/stata18"), code],
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=True,
        )


def compare(result: CoefficientResult | ScalarResult, text: str) -> None:
    rows = list(csv.DictReader(io.StringIO(text)))
    if isinstance(result, ScalarResult):
        assert len(rows) == 1
        for scalar in result.values:
            assert scalar.value is not None and math.isclose(
                scalar.value, float(rows[0][scalar.name]), abs_tol=1e-7, rel_tol=1e-6
            )
        assert int(float(rows[0]["N"])) == result.sample_size
        return
    assert [e.name for e in result.estimates] == [r["parameter"] for r in rows]
    for estimate, row in zip(result.estimates, rows, strict=True):
        for value, field in (
            (estimate.value, "coefficient"),
            (estimate.standard_error, "standard_error"),
            (estimate.p_value, "p_value"),
        ):
            assert value is not None and math.isclose(
                value, float(row[field]), abs_tol=1e-7, rel_tol=1e-6
            )
        assert estimate.confidence_interval
        for value, field in zip(estimate.confidence_interval, ("lower", "upper"), strict=True):
            assert math.isclose(value, float(row[field]), abs_tol=1e-7, rel_tol=1e-6)
        assert result.sample_size == int(float(row["n"]))
        if estimate.degrees_of_freedom is None:
            assert row["df"] in {"", "."}
        else:
            assert estimate.degrees_of_freedom == float(row["df"])


async def invalid_rdd(store: RunStore, backend: StataBackend, input_id: str) -> dict[str, Any]:
    """故意声明错误阈值；真实估计成功仍必须因结构不符被拒绝选用。"""
    original = cases()["rdd"].regression
    assert original and original.rdd
    rule = original.model_copy(update={"rdd": original.rdd.model_copy(update={"cutoff": 0.25})})
    program = StataProgram(code=rule.command(), regression=rule)
    design = store.create_design(
        DesignContent(
            population="故意错误的合成 RDD 夹具",
            variables={v: "合成变量" for v in rule.variables()},
            sample_rule=rule.sample_expression,
            estimand="拒绝阈值与处理分配不一致的结果",
            methods=("regress",),
        ),
        change_reason="开发负例，不是科学研究设计",
    )
    request = store.request_human(design.design_id, "确认开发负例夹具")
    store.apply_answer(
        request.request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="accept", explanation="脚本化负例确认"),
    )
    spec = ExperimentSpec(
        experiment_id=new_id("experiment"),
        run_id=store.run_id,
        design_id=design.design_id,
        input_artifact_id=input_id,
        purpose="真实 RDD 错误阈值负例",
        initiation="planned",
        method="regress",
        variables=rule.variables(),
        sample_rule=rule.sample_expression,
        expected_result_type="coefficients",
    )
    store.create_experiment(spec)
    outcome = await backend.run_analysis(spec, attempt_id=new_attempt(store, spec), program=program)
    assert outcome.error is None and outcome.result.result_id and outcome.validation_artifact_id
    validation = ValidationRecord.model_validate_json(
        store.read_artifact(outcome.validation_artifact_id)
    )
    assert not validation.eligible()
    try:
        store.select_result(spec.experiment_id, outcome.result.result_id)
    except StateConflict:
        rejected = True
    else:
        rejected = False
    assert rejected and not store.selected_results()
    return {
        "actual_cutoff": 0,
        "declared_cutoff": 0.25,
        "result_id": outcome.result.result_id,
        "attempt_id": outcome.result.attempt_id,
        "validation_artifact_id": outcome.validation_artifact_id,
        "execution_succeeded": True,
        "specification_failed": True,
        "selection_rejected": True,
    }


def mapping_hashes() -> dict[str, str]:
    names = (
        "schemas.py",
        "stata_contracts.py",
        "stata_results.py",
        "stata_methods.py",
        "stata_backend.py",
        "harness.py",
        "inference.py",
    )
    return {
        name: hashlib.sha256((ROOT / "src/econ_agent" / name).read_bytes()).hexdigest()
        for name in names
    }


def write_registry(record: dict[str, Any], evidence_name: str = "stage7_methods") -> None:
    proof = {
        "schema_version": 1,
        "environment": record["cases"]["linear"]["environment"],
        "mapping_sha256": mapping_hashes(),
        "validated_scopes": {
            "regress": (
                "OLS 常规 t、两组两期重复截面 DiD HC1 t；"
                "锐断点 RDD h=0.5/c=0/均匀权重/HC1 t；"
                "证据 docs/evidence/stage7_methods.json"
            ),
            "logit": (
                "合成二元 Logit、观测信息矩阵与正态推断、满秩且收敛；"
                "证据 docs/evidence/stage7_methods.json"
            ),
            "summarize": (
                "合成单变量未加权 N/mean/sd/min/max；即时 r() 快照与独立参考；"
                "证据 docs/evidence/stage7_methods.json"
            ),
        },
        "evidence_ids": {
            name: {
                k: case[k] for k in ("run_id", "attempt_id", "result_id", "validation_artifact_id")
            }
            for name, case in record["cases"].items()
        },
    }
    iv_path = ROOT / "docs/evidence/demo_v01_iv.json"
    if evidence_name == "demo_v01_methods" and iv_path.is_file():
        iv = json.loads(iv_path.read_text())
        if (
            iv.get("status") == "passed"
            and all(
                iv.get("code_sha256", {}).get("src/econ_agent/" + name) == fingerprint
                for name, fingerprint in proof["mapping_sha256"].items()
            )
            and all(iv.get("environment", {}).get(k) == v for k, v in proof["environment"].items())
        ):
            proof["validated_scopes"]["ivregress"] = (
                "AJR 单工具 2SLS、常规协方差、small；无控制与纬度控制；N=64；"
                "独立参考、重新提取及生命周期检查；证据 docs/evidence/demo_v01_iv.json"
            )
    ajr_path = ROOT / "docs/evidence/stage7_ajr.json"
    if ajr_path.is_file():
        ajr = json.loads(ajr_path.read_text())
        if ajr.get("status") == "passed" and ajr.get("native_rerun", {}).get("new_native_session"):
            if all(
                ajr.get("code_sha256", {}).get("src/econ_agent/" + name) == fingerprint
                for name, fingerprint in proof["mapping_sha256"].items()
            ):
                proof["validated_scopes"]["ivregress"] = (
                    "AJR 单工具 2SLS、常规协方差、small；无控制与纬度控制；"
                    "当前包在新原生会话重跑；证据 docs/evidence/stage7_ajr.json"
                )
                proof["ajr_result_ids"] = [r["result_id"] for r in ajr["current_results"]]
    proof["validated_scopes"] = {
        k: v.replace("stage7_methods.json", evidence_name + ".json")
        for k, v in proof["validated_scopes"].items()
    }
    (ROOT / "src/econ_agent/method_validation.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2) + "\n"
    )


async def main(evidence_name: str = "stage7_methods") -> int:
    initial_mapping = mapping_hashes()
    directory = ROOT / "local_reference/stage7" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    directory.mkdir(parents=True)
    data = directory / "synthetic.csv"
    input_sha = synthetic_data(data)
    dictionary = directory / "dictionary.md"
    dictionary.write_text("自编合成测试数据，400 个独立观测；非面板。所有变量只用于数值验收。")
    native(
        f'do "{ROOT}/examples/methods/reference.do" "{data}" "{directory}"',
        directory,
        "native_session.log",
    )
    # 描述统计再用标准库独立核对；不作为生产分析后端。
    with data.open(newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    x = [float(r["x"]) for r in source_rows]
    summary_reference = next(csv.DictReader(io.StringIO((directory / "summary.csv").read_text())))
    assert math.isclose(float(summary_reference["mean"]), statistics.mean(x), abs_tol=1e-12)
    assert math.isclose(float(summary_reference["sd"]), statistics.stdev(x), abs_tol=1e-12)
    task = ResearchTask(
        data_path=data,
        dictionary_path=dictionary,
        research_goal="固定开发者合成方法验收，不是自主研究演示",
        max_research_rounds=5,
        budget=Budget(
            max_research_rounds=5, max_model_calls=1, max_tool_calls=10, max_runtime_seconds=1200
        ),
    )
    logit_reference = list(csv.DictReader(io.StringIO((directory / "logit.csv").read_text())))
    slope = next(row for row in logit_reference if row["parameter"] == "x")
    probabilities = (0.1, 0.25, 0.5, 0.75, 0.9)
    information = 80 * sum(
        prob * (1 - prob) * level**2
        for level, prob in zip((-2, -1, 0, 1, 2), probabilities, strict=True)
    )
    assert math.isclose(float(slope["coefficient"]), math.log(3), abs_tol=1e-7)
    assert math.isclose(float(slope["standard_error"]), 1 / math.sqrt(information), abs_tol=1e-7)
    did_reference = list(csv.DictReader(io.StringIO((directory / "did.csv").read_text())))
    cell_means = {
        (group, period): statistics.mean(
            float(row["ydid"])
            for row in source_rows
            if int(row["group"]) == group and int(row["post"]) == period
        )
        for group in (0, 1)
        for period in (0, 1)
    }
    did_value = cell_means[1, 1] - cell_means[1, 0] - cell_means[0, 1] + cell_means[0, 0]
    assert math.isclose(
        float(next(row for row in did_reference if row["parameter"] == "did")["coefficient"]),
        did_value,
        abs_tol=1e-7,
    )
    record: dict[str, Any] = {
        "schema_version": 1,
        "synthetic": True,
        "seed": 2001,
        "input_sha256": input_sha,
        "cases": {},
        "native_reference_code_sha256": hashlib.sha256(
            (ROOT / "examples/methods/reference.do").read_bytes()
        ).hexdigest(),
        "absolute_tolerance": 1e-7,
        "relative_tolerance": 1e-6,
    }
    record["additional_independent_checks"] = {
        "summary_standard_library_matched": True,
        "logit_closed_form_slope": math.log(3),
        "logit_information_se": 1 / math.sqrt(information),
        "did_four_cell_difference": did_value,
    }
    command_sources = {}
    for command in ("regress", "logit", "summarize", "ivregress"):
        for suffix in ("ado", "sthlp"):
            source = ROOT / "local_reference/stata18/ado/base" / command[0] / f"{command}.{suffix}"
            if source.is_file():
                header = re.search(r"(?m)^\*! version[^\n]+", source.read_text(errors="replace"))
                command_sources[source.name] = {
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "version_header": header.group(0) if header else None,
                    "source": "本机既有 Stata 随附文件；未下载更新",
                }
    record["command_sources"] = command_sources
    fixtures: dict[str, Any] = {
        "source": (
            "真实 Stata 18 MCP 返回 e() 白名单；描述统计另取主 do-file 即时 r() 快照；"
            "独立原生 lincom/描述统计参考；自编合成数据"
        ),
        "cases": {},
    }
    programs = cases()
    executable = ROOT / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp"
    with RunStore.create(ROOT / "runs", task) as store:
        prepared = prepare_input(store, ROOT / "backups")
        store.transition_run(RunStatus.RUNNING)
        async with StataMCPTransport(executable, ROOT, directory / "mcp_server.log") as transport:
            backend = StataBackend(
                store, transport, prepared, ROOT / "tmp/stata", timeout_seconds=60
            )
            for name, program in programs.items():
                print("正在真实核对方法：" + name, flush=True)
                rule = program.regression or program.summary
                assert rule is not None
                variables = {v: "合成测试变量" for v in rule.variables()}
                design = store.create_design(
                    DesignContent(
                        population="自编合成独立观测",
                        variables=variables,
                        sample_rule=rule.sample_expression,
                        estimand="数值验收，不声称真实因果效应",
                        methods=(
                            program.regression.method() if program.regression else "summarize",
                        ),
                        assumptions=("合成数据用于工程核验，不能证明应用中的识别条件",),
                    ),
                    change_reason="固定开发方法夹具；脚本化确认，不是独立研究者审阅",
                )
                request = store.request_human(design.design_id, "确认固定开发方法夹具")
                store.apply_answer(
                    request.request_id,
                    design_sha256=content_hash(design),
                    answer=HumanAnswer(decision="accept", explanation="开发脚本执行的夹具确认"),
                )
                spec = ExperimentSpec(
                    experiment_id=new_id("experiment"),
                    run_id=store.run_id,
                    design_id=design.design_id,
                    purpose="合成方法数值核对：" + name,
                    initiation="planned",
                    input_artifact_id=prepared.input_artifact_id,
                    method=design.content.methods[0],
                    variables=rule.variables(),
                    sample_rule=rule.sample_expression,
                    expected_result_type="scalars" if program.summary else "coefficients",
                )
                store.create_experiment(spec)
                outcome = await backend.run_analysis(
                    spec, attempt_id=new_attempt(store, spec), program=program
                )
                assert outcome.error is None, outcome.model_dump(mode="json")
                assert outcome.validation_artifact_id and outcome.result.result_id
                validation = ValidationRecord.model_validate_json(
                    store.read_artifact(outcome.validation_artifact_id)
                )
                assert validation.eligible(), validation.model_dump(mode="json")
                result = outcome.result.normalized
                assert isinstance(result, (CoefficientResult, ScalarResult))
                compare(result, (directory / (name + ".csv")).read_text())
                store.select_result(spec.experiment_id, outcome.result.result_id)
                payload = json.loads(
                    store.read_artifact(
                        store.attempt(outcome.result.attempt_id).raw_artifact_id or ""
                    )
                )
                e = payload["stored"]["data"]["e"]
                stored = {
                    "data": {
                        "e": {
                            k: v
                            for k, v in e.items()
                            if k
                            in {
                                "cmd",
                                "cmdline",
                                "depvar",
                                "vce",
                                "N",
                                "df_r",
                                "rank",
                                "converged",
                                "wtype",
                                "wexp",
                                "_matrices",
                            }
                        },
                        "r": payload["stored"]["data"]["r"],
                    }
                }
                csv_id = payload["files"]["scalars.csv" if program.summary else "coefficients.csv"]
                fixtures["cases"][name] = {
                    "program": program.model_dump(mode="json"),
                    "stored": stored,
                    "named_csv": store.read_artifact(csv_id).decode(),
                    "normalized": result.model_dump(mode="json"),
                    "native_csv": (directory / (name + ".csv")).read_text(),
                    "scalar_snapshot": json.loads(
                        store.read_artifact(payload["files"]["r_snapshot.json"])
                    )
                    if program.summary
                    else None,
                }
                table = result_tables(store, store.selected_results())
                assert table["facts"]
                # 每种形态分别导出；包搬迁到新目录后真实执行，不依赖原 run 路径。
                store.transition_run(RunStatus.PAUSED, reason="固定方法夹具已结束，导出只读复现包")
                bundle = export_run(store, ROOT / "output")
                relocated = directory / new_id("relocated_" + name)
                shutil.copytree(bundle, relocated)
                native(
                    f'do "{relocated}/reproduce.do" "{relocated}"', directory, name + "_rerun.log"
                )
                target = (
                    relocated
                    / "rerun_output"
                    / outcome.result.attempt_id
                    / ("scalars.csv" if program.summary else "coefficients.csv")
                )
                compare(result, target.read_text())
                assert (target.parent / "sample_check.txt").read_text().strip() == "1"
                record["cases"][name] = {
                    "run_id": store.run_id,
                    "design_id": design.design_id,
                    "experiment_id": spec.experiment_id,
                    "attempt_id": outcome.result.attempt_id,
                    "result_id": outcome.result.result_id,
                    "validation_artifact_id": outcome.validation_artifact_id,
                    "command": program.code,
                    "environment": {
                        k: payload["environment"][k]
                        for k in ("mcp_version", "stata_build", "stata_library_sha256")
                    },
                    "normalized": result.model_dump(mode="json"),
                    "native_reference_matched": True,
                    "relocated_bundle_matched": True,
                    "non_coefficient_facts": table["facts"] if program.summary else None,
                }
                # 同一工程夹具进入下一设计，旧版本仍可追溯，不伪装同一科学设计。
                store.transition_run(RunStatus.RUNNING, reason="继续下一项独立方法夹具")
            record["invalid_rdd"] = await invalid_rdd(store, backend, prepared.input_artifact_id)
            assert mapping_hashes() == initial_mapping, "真实验收期间映射代码发生变化，须重跑"
            write_registry(record, evidence_name)
            record["capabilities"] = [
                c.model_dump(mode="json") for c in await backend.capabilities()
            ]
        store.transition_run(RunStatus.PAUSED, reason="阶段七固定方法验收完成")
        record["calls"] = store.call_summary()
    assert hashlib.sha256(data.read_bytes()).hexdigest() == input_sha
    record["code_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            Path(__file__),
            ROOT / "examples/methods/reference.do",
            *sorted((ROOT / "src/econ_agent").glob("*.py")),
        ]
    }
    record["finished_at"] = datetime.now(UTC).isoformat()
    fixtures["input_sha256"] = input_sha
    fixtures["environment"] = record["cases"]["linear"]["environment"]
    (ROOT / "examples/methods/stata18_reference.json").write_text(
        json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "docs/evidence" / (evidence_name + ".json")).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    (directory / "validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "local_reference/stage7/latest.txt").write_text(str(directory))
    print("真实方法验收及迁移复现通过：" + str(directory), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="显式真实 Stata 方法验收")
    parser.add_argument(
        "--evidence-name", choices=("stage7_methods", "demo_v01_methods"), default="stage7_methods"
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().evidence_name)))
