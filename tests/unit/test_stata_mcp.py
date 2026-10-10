"""固定协议封套及子进程秘密隔离；不启动真实服务。"""

from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from econ_agent.stata_mcp import StataMCPTransport, child_environment, decode_envelope


def test_child_environment_uses_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "只供脱敏测试的假密钥")
    monkeypatch.setenv("OTHER_ACCESS_TOKEN", "假口令")
    environment = child_environment(Path("/local/stata"), Path("/local/project"))
    assert set(environment) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "STATA_PATH",
        "MCP_STATA_LOGLEVEL",
        "MCP_STATA_ALLOWED_ROOTS",
    }
    assert "DEEPSEEK_API_KEY" not in environment and "OTHER_ACCESS_TOKEN" not in environment
    assert environment["STATA_PATH"] == "/local/stata"


@pytest.mark.parametrize(
    "patch", [{"schema_version": "future"}, {"tool": "another_tool"}, {"success": "true"}]
)
def test_wrong_envelope_rejected(patch: dict[str, Any]) -> None:
    payload = {"schema_version": "2026-05-07", "tool": "stata_run", "success": True} | patch
    with pytest.raises(ValueError):
        decode_envelope(
            CallToolResult(content=[], structuredContent={"result": payload}), "stata_run"
        )


def test_error_envelope_cannot_become_success() -> None:
    response = CallToolResult(
        content=[],
        isError=True,
        structuredContent={
            "result": {"schema_version": "2026-05-07", "tool": "stata_run", "success": True}
        },
    )
    assert decode_envelope(response, "stata_run")["success"] is False
    with pytest.raises(ValueError):
        decode_envelope(CallToolResult(content=[], structuredContent={}), "stata_run")


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_rpc_timeout_is_finite_and_positive(duration: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        StataMCPTransport(tmp_path / "stata", tmp_path, tmp_path / "log", rpc_timeout=duration)
