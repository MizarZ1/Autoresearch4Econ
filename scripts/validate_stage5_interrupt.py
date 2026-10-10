"""显式启动既有 Stata，检验真实 SIGINT 及硬中断恢复；固定代码是开发夹具。"""

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from econ_agent import session
from econ_agent.inputs import prepare_input
from econ_agent.processes import process_identity
from econ_agent.schemas import (
    DesignContent,
    ExecutionStatus,
    ExperimentSpec,
    HumanAnswer,
    ResearchTask,
    ToolCall,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import StataProgram
from econ_agent.stata_mcp import StataMCPTransport
from econ_agent.state import RunStore, new_id

ROOT = Path(__file__).resolve().parents[1]


async def fixed_computation(store: RunStore, config: session.RuntimeConfig) -> None:
    """替换验收脚本的模型选择边界，底层会话、归档及回收使用生产执行器。"""
    prepared = prepare_input(store, ROOT / "backups")
    design = store.create_design(
        DesignContent(
            population="AJR 数据，生命周期验收不估计研究结论",
            variables={"avexpr": "制度保护"},
            sample_rule="baseco==1",
            estimand="专用进程退出证据",
            methods=("通用执行验证",),
        ),
        change_reason="中断验收固定设计，非真实模型决策",
    )
    request = store.request_human(design.design_id, "开发夹具确认")
    store.apply_answer(
        request.request_id,
        design_sha256=content_hash(design),
        answer=HumanAnswer(decision="accept", explanation="开发脚本确认"),
    )
    spec = ExperimentSpec(
        experiment_id=new_id("experiment"),
        run_id=store.run_id,
        design_id=design.design_id,
        purpose="SIGINT与硬中断真实退出验证",
        initiation="planned",
        input_artifact_id=prepared.input_artifact_id,
        method="通用执行验证",
        variables=("avexpr",),
        sample_rule="baseco==1",
        expected_result_type="scalars",
    )
    store.create_experiment(spec)
    call = ToolCall(
        tool_call_id=new_id("tool"),
        run_id=store.run_id,
        operation_id=new_id("op"),
        tool_name="run_analysis",
        arguments_status="pass",
    )
    store.record_tool_call(call)
    attempt = store.create_attempt(spec.experiment_id, call.tool_call_id)
    marker = ROOT / "tmp/stata" / store.run_id / attempt.attempt_id / "output/started.txt"
    (ROOT / "local_reference/stage5" / (store.run_id + ".marker")).write_text(str(marker))
    code = (
        'tempname gate\nfile open `gate\' using "$ECON_OUTPUT/started.txt", write replace\n'
        "file write `gate' \"started\"\nfile close `gate'\n"
        "scalar econ_counter=0\nwhile 1 {\nscalar econ_counter=econ_counter+1\n}\n"
    )
    async with StataMCPTransport(
        config.executable, ROOT, ROOT / "local_reference/stage5" / (store.run_id + ".mcp.log")
    ) as transport:
        backend = StataBackend(store, transport, prepared, ROOT / "tmp/stata", timeout_seconds=120)
        await backend.run_analysis(
            spec, attempt_id=attempt.attempt_id, program=StataProgram(code=code)
        )


def child(identifier: str) -> int:
    task = ResearchTask.from_file(ROOT / "examples/demo/task.json")
    session.run_research = fixed_computation
    with RunStore.create(ROOT / "runs", task, run_id=identifier) as store:
        return asyncio.run(session.conduct(store, ROOT, no_input=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child")
    args = parser.parse_args()
    if args.child:
        return child(args.child)
    directory = ROOT / "local_reference/stage5"
    directory.mkdir(parents=True, exist_ok=True)
    cases: dict[str, Any] = {}
    for mode in ("sigint", "hard_crash"):
        identifier = "run_stage5_" + mode + "_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        log = directory / (identifier + ".log")
        worker = None
        with log.open("w") as output:
            producer = subprocess.Popen(
                [sys.executable, "-m", "scripts.validate_stage5_interrupt", "--child", identifier],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 60
                marker_path = directory / (identifier + ".marker")
                while time.monotonic() < deadline:
                    if producer.poll() is not None:
                        raise RuntimeError("验收子进程提前退出，请查本地日志")
                    if marker_path.exists() and Path(marker_path.read_text()).exists():
                        break
                    time.sleep(0.1)
                else:
                    raise TimeoutError("未观察到真实 Stata 计算开始标记")
                with RunStore.open(ROOT / "runs" / identifier, writable=False) as store:
                    attempts = [
                        a for e in store.experiments() for a in store.attempts(e.experiment_id)
                    ]
                    attempt = attempts[0]
                    artifact = next(
                        a for a in store.artifacts() if a.relative_path.endswith("/identity.json")
                    )
                    saved = json.loads(store.read_artifact(artifact.artifact_id))
                    assert saved["identity"]
                    from econ_agent.processes import WorkerIdentity

                    worker = WorkerIdentity.model_validate(saved["identity"])
                    assert process_identity(worker.pid) == worker
                producer.send_signal(signal.SIGINT if mode == "sigint" else signal.SIGKILL)
                exit_code = producer.wait(timeout=30)
                if mode == "hard_crash":
                    # 恢复通过实际 CLI。/stop 保存已验证材料，不触发模型或新统计任务。
                    resumed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "econ_agent.cli",
                            "resume",
                            identifier,
                            "--no-input",
                            "--feedback",
                            "/stop",
                        ],
                        cwd=ROOT,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=30,
                        check=False,
                    )
                    assert resumed.returncode == 0
                else:
                    assert exit_code == 130
                assert process_identity(worker.pid) != worker
                with RunStore.open(ROOT / "runs" / identifier, writable=False) as store:
                    saved_attempt = store.attempt(attempt.attempt_id)
                    assert saved_attempt.termination_confirmed
                    assert saved_attempt.execution_status == (
                        ExecutionStatus.CANCELLED
                        if mode == "sigint"
                        else ExecutionStatus.INTERRUPTED
                    )
                    assert len(store.attempts(attempt.experiment_id)) == 1
                    assert not store.model_calls()
                    cases[mode] = {
                        "run_id": identifier,
                        "exit_code": exit_code,
                        "worker_birth_identity_saved": True,
                        "worker_exit_confirmed": True,
                        "attempt_status": saved_attempt.execution_status,
                        "termination_confirmed": saved_attempt.termination_confirmed,
                        "attempt_count": 1,
                        "call_summary": store.call_summary(),
                        "state": store.load_state().status,
                        "developer_fixture": True,
                    }
            finally:
                if producer.poll() is None:
                    producer.kill()
                    producer.wait(timeout=10)
                if worker and process_identity(worker.pid) == worker:
                    from econ_agent.processes import LocalProcessController

                    asyncio.run(LocalProcessController().stop(worker))
    (directory / "interrupt.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    print("真实 SIGINT 与硬中断后 CLI 恢复通过；未调用模型、未下载 Stata。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
