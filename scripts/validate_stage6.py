"""显式真实验收：模型写作、授权样本修改、纬度补充分析和迁移后原生 Stata 重跑。"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from econ_agent.schemas import CoefficientResult
from econ_agent.state import RunStore, new_id

ROOT = Path(__file__).resolve().parents[1]


def invoke(directory: Path, name: str, *arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "econ_agent.cli", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    log = directory / (name + ".log")
    if log.exists():
        log = directory / (new_id(name) + ".log")
    log.write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "请核对本地 CLI 日志：" + name
    return result.stdout


def status(identifier: str, directory: Path, name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(invoke(directory, name, "status", identifier, "--json")))


def answer(directory: Path, identifier: str, request: dict[str, Any], text: str, name: str) -> None:
    invoke(
        directory,
        name,
        "resume",
        identifier,
        "--no-input",
        "--answer",
        text,
        "--request-id",
        request["request_id"],
        "--design-sha256",
        request["target_design_sha256"],
    )


def rerun_bundle(bundle: Path, directory: Path) -> dict[str, Any]:
    """复制到独立路径，启动一个全新 Stata 进程，只执行包内入口。"""
    relocated = directory / new_id("relocated_package")
    shutil.copytree(bundle, relocated)
    child_code = """
import sys
sys.path.insert(0,sys.argv[1]+"/utilities")
from pystata import config
config.init("mp",splash=False)
from pystata import stata
stata.run('do "'+sys.argv[2]+'/reproduce.do" "'+sys.argv[2]+'"',quietly=True)
"""
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    with (directory / "native_reproduce.log").open("w") as log:
        subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(ROOT / "local_reference/stata18"),
                str(relocated),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=True,
        )
    manifest = json.loads((relocated / "bundle_manifest.json").read_text())
    assertions = []
    for identifier in manifest["current_result_ids"]:
        reference = next(
            v for v in manifest["files"].values() if v["source_artifact_id"] == identifier
        )
        name = next(k for k, v in manifest["files"].items() if v is reference)
        result = json.loads((relocated / name).read_text())
        normalized = CoefficientResult.model_validate(result["normalized"])
        with (relocated / "rerun_output" / result["attempt_id"] / "coefficients.csv").open(
            newline=""
        ) as stream:
            rows = list(csv.DictReader(stream))
        assert rows
        for estimate in normalized.estimates:
            row = next(r for r in rows if r["parameter"] == estimate.name)
            # 字段名称跟随已归档提取脚本，不能自行重算参考。
            for field, actual in (
                ("coefficient", estimate.value),
                ("standard_error", estimate.standard_error),
                ("p_value", estimate.p_value),
                ("df", estimate.degrees_of_freedom),
            ):
                assert actual is not None and math.isclose(
                    float(row[field]), actual, rel_tol=1e-6, abs_tol=1e-7
                )
            assert int(float(row["n"])) == normalized.sample_size
            assert float(row["df"]) == estimate.degrees_of_freedom
            assert estimate.confidence_interval
            assert math.isclose(
                float(row["lower"]), estimate.confidence_interval[0], rel_tol=1e-6, abs_tol=1e-7
            )
            assert math.isclose(
                float(row["upper"]), estimate.confidence_interval[1], rel_tol=1e-6, abs_tol=1e-7
            )
        assertions.append(
            {
                "result_id": identifier,
                "attempt_id": result["attempt_id"],
                "sample_size": normalized.sample_size,
                "matched": True,
            }
        )
    return {
        "new_native_session": True,
        "relocated_package": True,
        "absolute_tolerance": 1e-7,
        "relative_tolerance": 1e-6,
        "results": assertions,
        "child_code_sha256": hashlib.sha256(child_code.encode()).hexdigest(),
        "model_received_reference": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="只核对已有完成案例，不重新执行模型研究循环")
    parser.add_argument(
        "--recover-baseline",
        action="store_true",
        help="显式恢复失败的初始样本阶段，保留原预算及失败记录",
    )
    parser.add_argument("--directory", type=Path, help="既有验收目录")
    parser.add_argument(
        "--core-python", type=Path, help="显式核对没有 MCP/Stata 依赖的 Python 环境"
    )
    parser.add_argument(
        "--evidence-prefix",
        choices=("stage6", "stage7_ajr"),
        default="stage6",
        help="首版交付时单独保存 AJR 回归证据，不覆盖阶段六历史",
    )
    args = parser.parse_args()
    directory = args.directory or ROOT / "local_reference" / args.evidence_prefix / datetime.now(
        UTC
    ).strftime("%Y%m%dT%H%M%S%f")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=args.directory is not None)
    if not args.run_id:
        task: dict[str, Any] = {
            "data_path": str(ROOT / "examples/demo/data/maketable4.dta"),
            "research_goal": (
                "以 AJR 国家截面数据研究 avexpr 与 logpgp95 的关系，用 logem4 作工具。"
                "研究设计计划基准IV和加入lat_abst控制的补充IV，两个采用同一确认样本、"
                "常规同方差标准误、small修正、ivregress 2sls。"
                "初始样本精确为baseco==1，variables键为实际列名，含logpgp95,avexpr,logem4,lat_abst。"
                "先提出设计并等待确认；只跑无控制的基准后立即ask_researcher询问样本修改。"
                "新样本设计确认后依次跑无控制基准及加入lat_abst的补充IV。"
                "补充分析based_on_result_ids必须引用新版本基准result_id；"
                "两个都取得selected有效结果后finish_research，不再重复同一规格。"
                "未经执行的第一阶段诊断、机制检验与识别前提作为局限。"
            ),
            "material_paths": [str(ROOT / "examples/demo/materials/research_background.md")],
            "dictionary_path": str(ROOT / "examples/demo/materials/data_dictionary.md"),
            "max_research_rounds": 6,
            "budget": {
                "max_research_rounds": 6,
                "max_model_calls": 28,
                "max_tool_calls": 28,
                "max_runtime_seconds": 900,
            },
        }
        if args.evidence_prefix == "stage7_ajr":
            task["research_goal"] += (
                "两个新设计下的真实结果均选用后，调用一次review_results，"
                "将这两个结果ID都交给两项并发审阅，依据实际意见结束研究；"
                "不要增加新研究规格，所有尚未执行诊断明确为未执行。"
            )
        task_file = directory / "task.json"
        task_file.write_text(json.dumps(task, ensure_ascii=False))
        output = invoke(directory, "start", "start", "--task", str(task_file), "--no-input")
        identifier = re.search(r"已创建运行：(run_[a-z0-9]+)", output)
        assert identifier
        run_id = identifier[1]
        (directory / "run_id.txt").write_text(run_id)
        before = status(run_id, directory, "before")
        assert before["pending_requests"]
        authorization = json.loads((ROOT / "local_reference/stage5/authorization.json").read_text())
        authorization.update(
            {
                "authorization_reuse": args.evidence_prefix
                + "：沿用用户真实授权自行选择样本；此案例确认由开发脚本完成",
                "initial_and_revised_accept_are_scripted": True,
                "supplementary_analysis": "预先计划的lat_abst控制，检验工程闭环，不以显著性择优",
            }
        )
        with RunStore.open(ROOT / "runs" / run_id) as store:
            store.archive_bytes(
                f"calls/human/authorization/{new_id('source')}.json",
                json.dumps(authorization, ensure_ascii=False).encode(),
                kind="tool_arguments",
            )
        answer(
            directory, run_id, before["pending_requests"][0], "/accept", "initial_scripted_accept"
        )
    else:
        run_id = args.run_id
    if args.recover_baseline:
        correction = (
            "/revise 这是开发脚本修正规格说明，不代表新的研究者意见。sample_rule保持baseco==1。"
            "estimand修改为单工具线性IV关系估计，不能无额外假设直接称为局部平均处理效应。"
            "无控制基准和lat_abst控制补充计划保留。代码if、iv.sample_expression、sample_rule"
            "三者精确复制baseco==1，禁止追加任何!missing()；缺失由Stata默认排除。"
            "确认后先只跑基准再询问样本修改。"
        )
        recovery = status(run_id, directory, "before_baseline_recovery")
        if recovery["pending_requests"]:
            answer(
                directory, run_id, recovery["pending_requests"][0], correction, "baseline_recovery"
            )
        else:
            invoke(
                directory,
                "baseline_recovery",
                "resume",
                run_id,
                "--no-input",
                "--feedback",
                correction,
            )
        corrected = status(run_id, directory, "baseline_corrected")
        answer(
            directory,
            run_id,
            corrected["pending_requests"][0],
            "/accept",
            "correction_scripted_accept",
        )
    if not args.run_id or args.recover_baseline:
        baseline = status(run_id, directory, "baseline")
        assert (
            baseline["selected_results"]
            and baseline["selected_results"][0]["normalized"]["sample_size"] == 64
        )
        assert baseline["pending_requests"]
        feedback = (
            "/revise 沿用研究者已明确授权自行选择；本次工程验证选定baseco==1 & lat_abst>0，"
            "只修改sample_rule，其余计划包含无控制基准和lat_abst控制补充IV全部保留。"
            "这是最小工程变更，不宣称统计最优。展示新设计等待确认后，"
            "依次执行新样本基准和lat_abst控制补充，不复用旧版本结果。"
            "所有sample_rule、实际if及iv.sample_expression都精确复制baseco==1 & lat_abst>0。"
        )
        answer(directory, run_id, baseline["pending_requests"][0], feedback, "revision")
        proposed = status(run_id, directory, "proposed")
        assert (
            proposed["designs"][-1]["design"]["content"]["sample_rule"] == "baseco==1 & lat_abst>0"
        )
        answer(
            directory, run_id, proposed["pending_requests"][0], "/accept", "revised_scripted_accept"
        )
    after = status(run_id, directory, "after")
    assert not after["pending_requests"]
    assert len(after["selected_results"]) >= 2
    export_output = invoke(directory, "export", "export", run_id)
    bundle = Path(export_output.split("复现包已导出：", 1)[1].strip())
    manifest = json.loads((bundle / "bundle_manifest.json").read_text())
    assert manifest["status"] == "complete", "论文或复现包仍为部分交付，请核对报告失败记录"
    with RunStore.open(ROOT / "runs" / run_id, writable=False) as store:
        results = store.selected_results()
        assert any("lat_abst" in r.normalized.variables for r in results if r.normalized)
        assert any("lat_abst" not in r.normalized.variables for r in results if r.normalized)
        assert all(r.normalized and r.normalized.sample_size == 63 for r in results)
        calls_before: dict[str, Any] = dict(store.call_summary())
        evidence = json.loads((bundle / "evidence.json").read_text())
        assert evidence["claims"] and {f["result_id"] for f in evidence["facts"].values()} == {
            r.result_id for r in results
        }
        calls = [json.loads(line) for line in (bundle / "calls.jsonl").read_text().splitlines()]
        usage = json.loads((bundle / "usage.json").read_text())
        assert len(calls) == calls_before["model_calls"] + calls_before["tool_calls"]
        assert usage["known_total_tokens"] == calls_before["known_total_tokens"]
        assert usage["missing_total_usage_calls"] == calls_before["missing_total_usage_calls"]
        for name, ref in manifest["files"].items():
            assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == ref["sha256"]
        record = {
            "schema_version": 1,
            "status": "passed",
            "run_id": run_id,
            "report_id": manifest["report_id"],
            "current_results": [r.model_dump(mode="json") for r in results],
            "scripted_confirmations": True,
            "human_authorization_source": "用户原始答复：你自行选择最优",
            "sample_rule_reason": "最小工程变更；不宣称统计最优",
            "calls": calls_before,
            "requested_models": sorted({c.requested_model for c in store.model_calls()}),
            "returned_models": sorted(
                {c.returned_model for c in store.model_calls() if c.returned_model}
            ),
            "environment": "本机既有 Stata 18.0.185；mcp-stata 3.3.0；未下载更新包",
            "export_hashes_passed": True,
            "evidence_claims": len(evidence["claims"]),
            "original_data_unchanged": hashlib.sha256(
                store.load_task().data_path.read_bytes()
            ).hexdigest()
            == results[0].input_sha256,
        }
        if args.evidence_prefix == "stage7_ajr":
            groups: dict[str, list[Any]] = {}
            for call in store.model_calls():
                if call.parent_tool_call_id:
                    groups.setdefault(call.parent_tool_call_id, []).append(call)
            record["review_batches"] = []
            for parent, group in groups.items():
                assert len(group) == 2 and all(
                    c.status == "SUCCEEDED" and c.parse_status == "VALID" for c in group
                )
                starts = [datetime.fromisoformat(c.started_at) for c in group]
                ends = [datetime.fromisoformat(c.ended_at) for c in group]
                overlap = (min(ends) - max(starts)).total_seconds()
                assert overlap > 0
                record["review_batches"].append(
                    {
                        "parent_tool_call_id": parent,
                        "model_call_ids": [c.model_call_id for c in group],
                        "context_result_ids": group[0].context_result_ids,
                        "overlap_seconds": overlap,
                        "both_valid": True,
                    }
                )
            assert record["review_batches"]
            reference_path = ROOT / "examples/demo/reference/independent_stata18.json"
            reference = json.loads(reference_path.read_text())
            historical_candidates = []
            for experiment in store.experiments():
                for attempt in store.attempts(experiment.experiment_id):
                    if attempt.result_artifact_id:
                        result = store.result(attempt.result_artifact_id)
                        if result.normalized and result.normalized.sample_size == 64:
                            historical_candidates.append(result)
            assert historical_candidates
            historical = historical_candidates[0]
            assert isinstance(historical.normalized, CoefficientResult)
            assert historical.input_sha256 == reference["data_sha256"]
            for estimate in historical.normalized.estimates:
                expected = next(
                    row
                    for row in reference["estimates"]
                    if row["specification"] == "baseline"
                    and row["estimator"] == "ivregress"
                    and row["parameter"] == estimate.name
                )
                assert estimate.value is not None and estimate.standard_error is not None
                assert math.isclose(
                    estimate.value, float(expected["coefficient"]), abs_tol=1e-7, rel_tol=1e-6
                )
                assert math.isclose(
                    estimate.standard_error,
                    float(expected["standard_error"]),
                    abs_tol=1e-7,
                    rel_tol=1e-6,
                )
                assert estimate.degrees_of_freedom == float(expected["df_r"])
            record["baseline_reference"] = {
                "result_id": historical.result_id,
                "sample_size": 64,
                "matched": True,
                "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            }
            record["explicit_recovery_request_ids"] = [
                json.loads(store.read_artifact(a.artifact_id))["reason"].split("：", 1)[1]
                for a in store.artifacts(kind="tool_response")
                if a.relative_path.startswith("calls/harness/")
                and str(
                    json.loads(store.read_artifact(a.artifact_id)).get("reason", "")
                ).startswith("显式答复后恢复有界尝试：")
            ]
            reextraction = next(
                a
                for a in store.artifacts(kind="tool_response")
                if a.relative_path == "calls/stage7/iv_equivalent_command_reextraction.json"
            )
            record["iv_equivalent_command_reextraction"] = {
                "artifact_id": reextraction.artifact_id,
                **json.loads(store.read_artifact(reextraction.artifact_id)),
            }
    # 错误模型配置及不存在的 Stata 路径不能影响已有记录的离线导出。
    environment = dict(os.environ, ECON_AGENT_MODEL_URL="bad-url", STATA_PATH="/不存在/stata")
    environment.pop("DEEPSEEK_API_KEY", None)
    offline = subprocess.run(
        [sys.executable, "-m", "econ_agent.cli", "export", run_id],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    (directory / "offline_export.log").write_text(offline.stdout + offline.stderr)
    with RunStore.open(ROOT / "runs" / run_id, writable=False) as store:
        assert store.call_summary() == calls_before
    record["export_no_model_or_stata"] = True
    if args.core_python:
        core_code = """
import importlib.util,sys
assert importlib.util.find_spec("mcp") is None
assert importlib.util.find_spec("psutil") is None
from econ_agent.cli import main
assert main(["export",sys.argv[1],"--workspace",sys.argv[2]]) == 0
assert not any(name == "mcp" or name.startswith("mcp.") or name == "psutil" for name in sys.modules)
"""
        core = subprocess.run(
            [str(args.core_python.expanduser().absolute()), "-c", core_code, run_id, str(ROOT)],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        (directory / "core_export.log").write_text(core.stdout + core.stderr)
        assert core.returncode == 0, "无 MCP 环境导出失败，请核对 core_export.log"
        record["core_environment_export"] = {
            "passed": True,
            "mcp_installed": False,
            "psutil_installed": False,
            "child_code_sha256": hashlib.sha256(core_code.encode()).hexdigest(),
        }
    with RunStore.open(ROOT / "runs" / run_id, writable=False) as store:
        assert store.call_summary() == calls_before
    record["native_rerun"] = rerun_bundle(bundle, directory)
    record["code_sha256"] = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [Path(__file__), *sorted((ROOT / "src/econ_agent").glob("*.py"))]
    }
    record["code_sha256_scope"] = "最终导出与复现核对时的工作树；不倒填此前模型调用的代码版本"
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    (directory / "summary.json").write_text(payload)
    (
        ROOT
        / "docs/evidence"
        / ("stage6_report.json" if args.evidence_prefix == "stage6" else "stage7_ajr.json")
    ).write_text(payload)
    (ROOT / "local_reference" / args.evidence_prefix / "latest.txt").write_text(str(directory))
    print(
        args.evidence_prefix
        + " 真实验收通过：授权样本修改、基准与纬度补充、论文证据及迁移后 Stata 复现一致。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
