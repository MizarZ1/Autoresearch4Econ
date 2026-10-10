"""默认诊断保持离线，显式探测只在授权选项下调用。"""

import json
from pathlib import Path
from typing import Any

import pytest

from econ_agent import diagnostics
from econ_agent.cli import main


@pytest.mark.parametrize("probe,status", [(False, "passed"), (True, "passed"), (True, "failed")])
def test_doctor_only_connects_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    probe: bool,
    status: str,
) -> None:
    called = []

    async def fake(workspace: Path) -> dict[str, Any]:
        called.append(workspace)
        return {"status": status, "model_called": False, "capabilities": []}

    monkeypatch.setattr(diagnostics, "probe_stata", fake)
    monkeypatch.setenv("STATA_PATH", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "离线测试占位")
    (tmp_path / "StataMP.app/Contents/MacOS").mkdir(parents=True)
    (tmp_path / "StataMP.app/Contents/MacOS/stata-mp").write_text("仅测试文件存在性")
    args = ["doctor", "--json", "--workspace", str(tmp_path)]
    if probe:
        args.append("--probe-stata")
    assert main(args) == (1 if probe and status == "failed" else 0)
    result = json.loads(capsys.readouterr().out)
    assert len(called) == int(probe)
    assert ("stata_probe" in result) == probe


async def test_missing_stata_fails_without_creating_diagnostic_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STATA_PATH", str(tmp_path / "missing"))
    result = await diagnostics.probe_stata(tmp_path)
    assert result["status"] == "failed"
    assert not (tmp_path / "local_reference").exists()
