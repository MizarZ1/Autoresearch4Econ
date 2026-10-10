"""显式 Stata 健康探测；默认 doctor 不调用外部服务。"""

import os
from pathlib import Path
from typing import Any


async def probe_stata(workspace: Path) -> dict[str, Any]:
    """仅查询本机环境与方法；保存诊断调用，不请求模型或运行研究任务。"""
    workspace = workspace.expanduser().resolve()
    local = workspace / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp"
    default = (
        local
        if local.is_file()
        else Path("/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp")
    )
    executable = Path(os.getenv("STATA_PATH", str(default))).expanduser().resolve()
    if not executable.is_file():
        return {"status": "failed", "reason": "找不到本机 Stata；请指定既有安装，不下载更新包"}
    try:
        from econ_agent.inputs import prepare_input
        from econ_agent.schemas import ResearchTask, RunStatus
        from econ_agent.stata_backend import StataBackend
        from econ_agent.stata_mcp import StataMCPTransport
        from econ_agent.state import RunStore, new_id

        directory = workspace / "local_reference/doctor" / new_id("diagnostic")
        directory.mkdir(parents=True)
        data = directory / "diagnostic.csv"
        data.write_text("diagnostic\n1\n2\n")
        with RunStore.create(
            directory / "runs",
            ResearchTask(data_path=data, research_goal="Stata 环境诊断；不是自主研究"),
        ) as store:
            prepared = prepare_input(store, directory / "backups")
            store.transition_run(RunStatus.RUNNING)
            async with StataMCPTransport(
                executable, workspace, directory / "mcp_server.log"
            ) as transport:
                backend = StataBackend(
                    store, transport, prepared, directory / "work", timeout_seconds=60
                )
                capabilities = await backend.capabilities()
                passed = any(
                    c.method == "regress" and c.availability == "available" for c in capabilities
                )
                environment = {
                    k: transport.environment.get(k)
                    for k in ("stata_build", "stata_library_sha256", "mcp_version")
                }
            store.transition_run(RunStatus.PAUSED, reason="环境诊断结束")
            return {
                "status": "passed" if passed else "failed",
                "environment": environment,
                "capabilities": [c.model_dump(mode="json") for c in capabilities],
                "run_id": store.run_id,
                "record_directory": str(directory),
                "calls": store.call_summary(),
                "model_called": False,
                "reason": "本机方法查询完成；验证范围独立列出"
                if passed
                else "没有确认本机 regress 可用，请检查诊断日志",
            }
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        return {
            "status": "failed",
            "reason": "本机 Stata MCP 探测失败；请检查配置及诊断日志",
            "error_type": type(exc).__name__,
        }
