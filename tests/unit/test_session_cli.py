"""CLI 参数、无外部连接的状态查询和专用进程身份核对。"""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from econ_agent.cli import main
from econ_agent.processes import LocalProcessController, WorkerIdentity, process_identity
from econ_agent.schemas import ResearchTask
from econ_agent.session import RuntimeConfig
from econ_agent.state import RunStore


@pytest.mark.parametrize("backend", ["python", "unknown"])
def test_start_rejects_backend_before_creating_workspace(tmp_path: Path, backend: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["start", "--backend", backend, "--workspace", str(tmp_path)])
    assert exc.value.code == 2 and not (tmp_path / "runs").exists()


def test_task_backend_priority(tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "analysis_backend": "python",
                "data_path": "missing.csv",
                "research_goal": "测试后端优先级",
            }
        )
    )
    assert main(["start", "--task", str(task), "--workspace", str(tmp_path)]) == 2
    assert not (tmp_path / "runs").exists()
    parsed = ResearchTask.from_file(task, analysis_backend="stata")
    assert parsed.analysis_backend == "stata" and parsed.data_path == tmp_path / "missing.csv"


def test_resume_rejects_backend_override_and_unbound_answer(tmp_path: Path) -> None:
    for arguments in [["--backend", "python"], ["--answer", "/accept"]]:
        with pytest.raises(SystemExit) as exc:
            main(["resume", "run_test", "--workspace", str(tmp_path), *arguments])
        assert exc.value.code == 2
    assert not (tmp_path / "runs").exists()


def test_no_command_asks_inputs_and_uses_task_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n")
    answers = iter([str(data), "输入的研究目标", "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RuntimeConfig, "validate_execution", lambda _: None)

    async def capture(store: RunStore, **kwargs: Any) -> int:
        assert store.load_task().research_goal == "输入的研究目标"
        assert store.load_task().data_path == data
        assert store.load_state().analysis_backend == "stata"
        return 0

    monkeypatch.setattr("econ_agent.cli.conduct", capture)
    assert main([]) == 0 and len(list((tmp_path / "runs").iterdir())) == 1


def test_status_works_with_missing_stata_bad_model_and_forbidden_connectors(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data.csv"
    data.write_text("x\n1\n")
    with RunStore.create(
        tmp_path / "runs",
        ResearchTask(data_path=data, research_goal="离线查询"),
        run_id="run_offline",
    ) as store:
        db = store.directory / "state.sqlite"
    before = db.read_bytes()
    data.unlink()
    script = """
import sys
class NoExternal:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "psutil" or fullname.startswith(("econ_agent.stata_", "mcp", "pystata")):
            raise AssertionError("只读状态不能导入外部执行器：" + fullname)
sys.meta_path.insert(0, NoExternal())
from econ_agent.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    env = os.environ | {
        "STATA_PATH": "/缺失的本机Stata",
        "ECON_AGENT_MODEL_URL": "bad-url",
        "ECON_AGENT_KEY_VARIABLE": "NONEXISTENT_KEY",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            "status",
            "run_offline",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["run"]["analysis_backend"] == "stata"
    assert status["calls"]["known_total_tokens"] is None
    assert db.read_bytes() == before


async def test_pid_reuse_does_not_signal_unrelated_process(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = WorkerIdentity(pid=999999, created_at=100, executable="/测试专用执行器")
    current = expected.model_copy(update={"created_at": 101})
    controller = LocalProcessController()
    monkeypatch.setattr(controller, "inspect", lambda _: current)
    assert await controller.stop(expected) == "gone"


async def test_owned_child_is_reaped_by_identity() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = process_identity(child.pid)
        assert identity and identity.pid == child.pid
        assert await LocalProcessController().stop(identity) in {"stopped", "gone"}
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


async def test_second_birth_check_closes_pid_reuse_race(monkeypatch: pytest.MonkeyPatch) -> None:
    psutil = importlib.import_module("psutil")

    expected = WorkerIdentity(pid=999999, created_at=100, executable="/测试专用执行器")
    controller = LocalProcessController()
    monkeypatch.setattr(controller, "inspect", lambda _: expected)

    class Reused:
        def create_time(self) -> float:
            return 101

        def terminate(self) -> None:
            raise AssertionError("不能给复用PID的新进程发信号")

    monkeypatch.setattr(psutil, "Process", lambda _: Reused())
    assert await controller.stop(expected) == "gone"


async def test_unreadable_identity_keeps_recovery_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psutil = importlib.import_module("psutil")

    expected = WorkerIdentity(pid=999999, created_at=100, executable="/测试专用执行器")
    controller = LocalProcessController()

    def denied(_: int) -> None:
        raise psutil.AccessDenied(pid=expected.pid)

    monkeypatch.setattr(controller, "inspect", denied)
    assert await controller.stop(expected) == "unknown"
