"""固定 MCP 协议适配器；不把发现的代理工具开放给核心流程。"""

import asyncio
import hashlib
import importlib.metadata
import math
import os
import plistlib
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol, Self, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from econ_agent.processes import WorkerIdentity, process_identity
from econ_agent.state import new_id

REQUIRED_TOOLS = frozenset(
    {
        "stata_manage_session",
        "stata_run",
        "stata_task_status",
        "stata_control",
        "stata_get_results",
        "stata_inspect_data",
        "stata_get_help",
    }
)


def child_environment(executable: Path, allowed_root: Path) -> dict[str, str]:
    """只传必要环境变量；不继承模型或其他服务的凭证。"""
    child = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG"}}
    child.update(
        STATA_PATH=str(executable),
        MCP_STATA_LOGLEVEL="WARNING",
        MCP_STATA_ALLOWED_ROOTS=str(allowed_root),
    )
    return child


def decode_envelope(response: CallToolResult, name: str) -> dict[str, Any]:
    structured = response.structuredContent
    if not isinstance(structured, dict) or not isinstance(structured.get("result"), dict):
        raise ValueError("固定版本 MCP 返回封套不符合已验证契约")
    payload = dict(structured["result"])
    if (
        payload.get("schema_version") != "2026-05-07"
        or payload.get("tool") != name
        or not isinstance(payload.get("success"), bool)
    ):
        raise ValueError("MCP 返回的格式版本、工具身份或状态不匹配")
    if response.isError:
        payload["success"] = False
    return payload


class StataTransport(Protocol):
    service_instance_id: str
    environment: dict[str, Any]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    async def process_exists(self, pid: int) -> bool: ...


class StataMCPTransport:
    """进入与退出必须位于同一异步任务；运行及取消请求可并发发送。"""

    def __init__(
        self, executable: Path, allowed_root: Path, log_path: Path, *, rpc_timeout: float = 30
    ) -> None:
        if not math.isfinite(rpc_timeout) or rpc_timeout <= 0:
            raise ValueError("MCP 请求超时必须为有限正数")
        self.executable = executable.resolve()
        self.allowed_root = allowed_root.resolve()
        self.log_path = log_path
        self.rpc_timeout = rpc_timeout
        self.service_instance_id = new_id("mcp")
        self.environment: dict[str, Any] = {}
        self._stack = AsyncExitStack()
        self._log: TextIO | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        if not self.executable.is_file():
            raise ValueError("配置的本机 Stata 可执行文件不存在；请提供现有安装路径")
        if importlib.metadata.version("mcp-stata") != "3.3.0":
            raise ValueError("Stata 后端要求已锁定的 mcp-stata 3.3.0")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self._stack.enter_context(self.log_path.open("x"))
        # 严格白名单不继承模型密钥、认证头、代理口令或其他秘密环境变量。
        child = child_environment(self.executable, self.allowed_root)
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "econ_agent.stata_transport"], env=child
        )
        try:
            reader, writer = await self._stack.enter_async_context(
                stdio_client(parameters, errlog=self._log)
            )
            self._session = await self._stack.enter_async_context(ClientSession(reader, writer))
            initialized = await asyncio.wait_for(self._session.initialize(), self.rpc_timeout)
            tools = await asyncio.wait_for(self._session.list_tools(), self.rpc_timeout)
            present = {t.name for t in tools.tools}
            if not REQUIRED_TOOLS <= present:
                raise ValueError("MCP 实际工具清单缺少生产执行所需的通用接口")
            self.environment = {
                "mcp_version": "3.3.0",
                "server": initialized.serverInfo.model_dump(),
                "tools": sorted(present),
                "service_instance_id": self.service_instance_id,
            }
            metadata = self.executable.parent.parent / "Info.plist"
            library = self.executable.with_name("libstata-mp.dylib")
            if metadata.is_file():
                self.environment["stata_build"] = plistlib.loads(metadata.read_bytes()).get(
                    "CFBundleVersion"
                )
            if library.is_file():
                self.environment["stata_library_sha256"] = hashlib.sha256(
                    library.read_bytes()
                ).hexdigest()
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()
        self._session = None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in REQUIRED_TOOLS:
            raise ValueError("后端只允许调用已约定的通用 Stata MCP 工具")
        if self._session is None:
            raise RuntimeError("MCP 连接尚未建立或已关闭")
        response = await asyncio.wait_for(
            self._session.call_tool(name, arguments), self.rpc_timeout
        )
        return decode_envelope(response, name)

    async def process_exists(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def process_identity(self, pid: int) -> WorkerIdentity | None:
        return process_identity(pid)
