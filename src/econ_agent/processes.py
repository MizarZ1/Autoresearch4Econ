"""核对专用工作进程的 PID 与出生时间；不会仅凭复用的 PID 终止进程。"""

import asyncio
import importlib
from typing import Literal

from pydantic import Field

from econ_agent.schemas import Contract, Nonempty


class WorkerIdentity(Contract):
    pid: int = Field(gt=1)
    created_at: float = Field(gt=0)
    executable: Nonempty


def process_identity(pid: int) -> WorkerIdentity | None:
    # psutil 随固定 MCP 服务安装；只在显式执行或恢复进程核对时延迟导入。
    psutil = importlib.import_module("psutil")
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return WorkerIdentity(pid=pid, created_at=process.create_time(), executable=process.exe())
    except psutil.NoSuchProcess:
        return None


class LocalProcessController:
    def inspect(self, pid: int) -> WorkerIdentity | None:
        return process_identity(pid)

    async def stop(self, expected: WorkerIdentity) -> Literal["gone", "stopped", "unknown"]:
        psutil = importlib.import_module("psutil")
        try:
            current = self.inspect(expected.pid)
            if current != expected:
                return "gone"  # PID 已消失或被复用，禁止给新进程发信号。
            process = psutil.Process(expected.pid)
            if process.create_time() != expected.created_at or process.exe() != expected.executable:
                return "gone"
            # psutil 的终止方法也检查 PID 是否复用；之前保存的出生时间仍需先对应。
            process.terminate()
            for _ in range(50):
                if self.inspect(expected.pid) != expected:
                    return "stopped"
                await asyncio.sleep(0.1)
            if self.inspect(expected.pid) == expected:
                process.kill()
            for _ in range(50):
                if self.inspect(expected.pid) != expected:
                    return "stopped"
                await asyncio.sleep(0.1)
        except psutil.NoSuchProcess:
            return "gone"
        except (psutil.AccessDenied, OSError):
            return "unknown"
        return "unknown"
