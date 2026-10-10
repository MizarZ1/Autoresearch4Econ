"""安装、后端范围与凭证处理的离线检查。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from econ_agent.cli import main
from econ_agent.config import ModelConfig


@pytest.mark.parametrize(
    "endpoint",
    [
        "not-a-url",
        "ftp://example.org",
        "https://user:secret@example.org",
        "https://x.org?key=secret",
    ],
)
def test_endpoint_rejects_unsafe_config(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECON_AGENT_MODEL_URL", endpoint)
    with pytest.raises(ValueError):
        ModelConfig.from_environment()


def test_doctor_never_prints_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECON_AGENT_KEY_VARIABLE", "TEST_MODEL_KEY")
    monkeypatch.setenv("TEST_MODEL_KEY", "canary-secret-not-for-output")
    monkeypatch.setenv("ECON_AGENT_MODEL_URL", "https://example.org/v1")
    main(["doctor", "--json"])
    output = capsys.readouterr().out
    assert "canary-secret-not-for-output" not in output
    report = json.loads(output)
    assert report["model"]["key_configured"] is True
    assert report["model"]["execution_status"] == "unverified"
    assert report["stata"]["execution_status"] == "unverified"
    assert report["mcp"]["execution_status"] == "unverified"


def test_missing_configuration_fails_without_launching_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECON_AGENT_KEY_VARIABLE", "UNCONFIGURED_TEST_KEY")
    monkeypatch.delenv("UNCONFIGURED_TEST_KEY", raising=False)
    monkeypatch.setenv("STATA_PATH", str(tmp_path / "missing"))
    assert main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["model"]["key_configured"] is False
    assert report["stata"]["installation_present"] is False


def test_unsupported_backend_is_rejected() -> None:
    with pytest.raises(SystemExit) as error:
        main(["doctor", "--backend", "python"])
    assert error.value.code == 2


def test_config_error_does_not_expose_endpoint_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECON_AGENT_MODEL_URL", "https://user:canary-secret@example.org")
    assert main(["doctor"]) == 2
    assert "canary-secret" not in capsys.readouterr().err


def test_installed_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "econ_agent.cli", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "0.1.0"


def test_help_and_parameter_errors_use_chinese(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "用法：" in help_text and "命令:" in help_text and "显示帮助并退出" in help_text
    with pytest.raises(SystemExit) as error_exit:
        main(["doctor", "--backend", "python"])
    assert error_exit.value.code == 2
    assert "首版仅支持 stata 后端" in capsys.readouterr().err
