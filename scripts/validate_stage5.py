"""继续真实 CLI 的待答基准会话，验证授权选择的样本修改；确认是开发脚本。"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from econ_agent.schemas import CoefficientResult, ExperimentResult
from econ_agent.session import stop_snapshot
from econ_agent.state import RunStore, new_id

ROOT = Path(__file__).resolve().parents[1]


def invoke(directory: Path, name: str, *arguments: str, expected: int = 0) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "econ_agent.cli", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    (directory / (name + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == expected, "CLI 退出状态与预期不符，请查本地日志：" + name
    return result.stdout


def status(identifier: str, directory: Path, name: str) -> dict[str, Any]:
    return json.loads(invoke(directory, name, "status", identifier, "--json"))  # type: ignore[no-any-return]


def reference(directory: Path, result: ExperimentResult) -> dict[str, Any]:
    """新原生会话仅供数值对照，不向生产模型注入参考。"""
    child_code = """
import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1]+"/utilities")
from pystata import config
config.init("mp",splash=False)
from pystata import stata
from sfi import Scalar
stata.run('set processors 1\\nuse "'+sys.argv[2]+'", clear\\n'
          'ivregress 2sls logpgp95 (avexpr=logem4) if baseco==1 & lat_abst>0, small\\n'
          'scalar ref_n=e(N)\\nscalar ref_b=_b[avexpr]\\n'
          'scalar ref_se=_se[avexpr]\\nscalar ref_df=e(df_r)',quietly=True)
Path(sys.argv[3]).write_text(json.dumps({k:Scalar.getValue("ref_"+k) for k in ["n","b","se","df"]}))
"""
    target = directory / "independent.json"
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    with (directory / "independent.log").open("w") as log:
        subprocess.run(
            [
                sys.executable,
                "-c",
                child_code,
                str(ROOT / "local_reference/stata18"),
                str(ROOT / "examples/demo/data/maketable4.dta"),
                str(target),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=90,
        )
    expected = json.loads(target.read_text())
    assert isinstance(result.normalized, CoefficientResult)
    estimate = next(e for e in result.normalized.estimates if e.name == "avexpr")
    assert result.normalized.sample_size == expected["n"] == 63
    for actual, field in [
        (estimate.value, "b"),
        (estimate.standard_error, "se"),
        (estimate.degrees_of_freedom, "df"),
    ]:
        assert actual is not None and math.isclose(
            actual, expected[field], rel_tol=1e-6, abs_tol=1e-7
        )
    return {
        "expected": expected,
        "absolute_tolerance": 1e-7,
        "relative_tolerance": 1e-6,
        "passed": True,
        "model_received_reference": False,
        "child_code_sha256": hashlib.sha256(child_code.encode()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="已有基准 IV 结果且等待样本修改的运行")
    parser.add_argument(
        "--authorization-file", type=Path, required=True, help="本地保存的原始授权说明"
    )
    parser.add_argument("--verify-directory", type=Path, help="核对已完成的CLI记录，不重发修改")
    args = parser.parse_args()
    directory = args.verify_directory or (
        ROOT / "local_reference/stage5" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    )
    directory.mkdir(parents=True, exist_ok=args.verify_directory is not None)
    identifier = args.run_id
    authorization = json.loads(args.authorization_file.read_text())
    assert authorization["program_selected_rule"] == "baseco==1 & lat_abst>0"
    with RunStore.open(ROOT / "runs" / identifier) as archive:
        records = [
            a
            for a in archive.artifacts(kind="tool_arguments")
            if a.relative_path.startswith("calls/human/authorization/")
        ]
        if records:
            authorization_artifact = records[-1]
            assert (
                json.loads(archive.read_artifact(authorization_artifact.artifact_id))
                == authorization
            )
        else:
            authorization_artifact = archive.archive_bytes(
                f"calls/human/authorization/{new_id('source')}.json",
                json.dumps(authorization, ensure_ascii=False).encode(),
                kind="tool_arguments",
                design_id=archive.load_state().current_design_id,
            )
    before = (
        json.loads((directory / "before.log").read_text())
        if args.verify_directory
        else status(identifier, directory, "before")
    )
    assert len(before["selected_results"]) == 1
    baseline = before["selected_results"][0]
    assert baseline["normalized"]["sample_size"] == 64
    request = before["pending_requests"][0]
    if not args.verify_directory:
        invoke(directory, "waiting_resume", "resume", identifier, "--no-input")
    waiting = (
        json.loads((directory / "waiting_after.log").read_text())
        if args.verify_directory
        else status(identifier, directory, "waiting_after")
    )
    assert waiting["pending_requests"] == before["pending_requests"]
    assert waiting["calls"] == before["calls"]
    feedback = (
        "/revise 研究者已授权程序选择，本次工程验收选择排除绝对纬度为零的观测，"
        "不代表统计最优。仅将 sample_rule 精确改为 baseco==1 & lat_abst>0，"
        "其余设计包括实际变量名映射、基准IV、无控制变量、常规标准误与small全部保留。"
        "新版本仍需展示并确认；确认后实际代码if条件、iv.sample_expression和sample_rule"
        "使用同一表达式，不附加中文或缺失值条件，缺失由Stata估计时默认排除。"
    )
    if not args.verify_directory:
        invoke(
            directory,
            "revision",
            "resume",
            identifier,
            "--no-input",
            "--answer",
            feedback,
            "--request-id",
            request["request_id"],
            "--design-sha256",
            request["target_design_sha256"],
        )
    proposed = (
        json.loads((directory / "proposed.log").read_text())
        if args.verify_directory
        else status(identifier, directory, "proposed")
    )
    fresh = proposed["pending_requests"][0]
    latest = proposed["designs"][-1]
    assert latest["confirmation"] == "PROPOSED"
    assert latest["design"]["content"]["sample_rule"] == "baseco==1 & lat_abst>0"
    assert proposed["run"]["current_design_id"] == before["run"]["current_design_id"]
    assert proposed["selected_results"] == before["selected_results"]
    if not args.verify_directory:
        invoke(
            directory,
            "stale_approval",
            "resume",
            identifier,
            "--no-input",
            "--answer",
            "/accept",
            "--request-id",
            request["request_id"],
            "--design-sha256",
            request["target_design_sha256"],
            expected=1,
        )
        invoke(
            directory,
            "new_accept",
            "resume",
            identifier,
            "--no-input",
            "--answer",
            "/accept",
            "--request-id",
            fresh["request_id"],
            "--design-sha256",
            fresh["target_design_sha256"],
        )
    after = status(identifier, directory, "after")
    assert after["run"]["current_design_id"] == fresh["target_design_id"]
    assert after["selected_results"]
    assert all(r["normalized"]["sample_size"] == 63 for r in after["selected_results"])
    result = ExperimentResult.model_validate(after["selected_results"][0])
    assert result.normalized and result.normalized.sample_size == 63
    assert result.result_id != baseline["result_id"]
    assert after["run"]["status"] == "PAUSED" and not after["pending_requests"]
    invoke(directory, "finished_resume", "resume", identifier, "--no-input")
    finished = status(identifier, directory, "finished_after")
    assert finished["calls"] == after["calls"] and finished["analyses"] == after["analyses"]
    with RunStore.open(ROOT / "runs" / identifier, writable=False) as store:
        attempt = store.attempt(result.attempt_id)
        assert attempt.code_artifact_id and attempt.validation_artifact_id and result.result_id
        code = store.artifact(attempt.code_artifact_id)
        body = next(
            a
            for a in store.artifacts(kind="code")
            if a.attempt_id == attempt.attempt_id and a.relative_path.endswith("/body.do")
        )
        assert b"lat_abst>0" in store.read_artifact(body.artifact_id)
        snapshot = stop_snapshot(store, ROOT)
        exported = json.loads((snapshot / "results.json").read_text())
        assert {r["result_id"] for r in exported} == {
            r["result_id"] for r in after["selected_results"]
        }
        assert baseline["result_id"] not in {r["result_id"] for r in exported}
        assert b"lat_abst>0" in (snapshot / (attempt.attempt_id + "-body.do")).read_bytes()
        record = {
            "schema_version": 1,
            "status": "passed",
            "run_id": identifier,
            "authorization": authorization,
            "authorization_artifact_id": authorization_artifact.artifact_id,
            "authorization_sha256": authorization_artifact.sha256,
            "scripted_feedback": feedback,
            "confirmation_is_developer_script": True,
            "design_versions": [
                {
                    "version": d.version,
                    "sample_rule": d.content.sample_rule,
                    "confirmation": store.design_confirmation(d.design_id),
                }
                for d in store.designs()
            ],
            "baseline_result_id": baseline["result_id"],
            "baseline_n": 64,
            "modified_result": result.model_dump(mode="json"),
            "modified_results_count": len(after["selected_results"]),
            "说明": "模型在同一循环内额外选择了相同IV，保留全部结果；恢复后未重跑已完成尝试。",
            "modified_code_artifact_id": code.artifact_id,
            "modified_code_sha256": code.sha256,
            "modified_body_artifact_id": body.artifact_id,
            "modified_body_sha256": body.sha256,
            "modified_validation_sha256": store.artifact(attempt.validation_artifact_id).sha256,
            "waiting_question_preserved": True,
            "backend_preserved": True,
            "stale_approval_rejected": True,
            "completed_attempt_not_repeated": True,
            "snapshot_contains_current_result_only": True,
            "calls": store.call_summary(),
            "independent_reference": reference(directory, result),
            "requested_models": sorted({c.requested_model for c in store.model_calls()}),
            "returned_models": sorted(
                {c.returned_model for c in store.model_calls() if c.returned_model}
            ),
            "prior_failures_preserved": [
                c.error_summary for c in store.tool_calls() if c.error_summary
            ],
            "code_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [
                    Path(__file__),
                    ROOT / "scripts/validate_stage5_interrupt.py",
                    *sorted((ROOT / "src/econ_agent").glob("*.py")),
                ]
            },
            "original_data_unchanged": hashlib.sha256(
                store.load_task().data_path.read_bytes()
            ).hexdigest()
            == result.input_sha256,
            "interrupt": json.loads((ROOT / "local_reference/stage5/interrupt.json").read_text()),
        }
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    (directory / "summary.json").write_text(payload)
    (ROOT / "docs/evidence/stage5_cli.json").write_text(payload)
    print("真实 CLI 样本修改通过：64 → 63，旧结果仅留作历史，独立 Stata 对照一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
