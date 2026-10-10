"""在 POSIX 上分离原生 Stata 输出和 MCP 协议 stdout。

固定版本服务的工作进程会继承文件描述符 1。Stata 18 的无换行提示符可能
污染 JSON-RPC，因此为 Python 协议保留独立 stdout，将原生输出重定向到 stderr，
随后启动未改写的第三方服务。
"""

import importlib
import os
import sys


def reserve_protocol_stdout() -> None:
    sys.stdout.flush()
    descriptor = os.dup(sys.stdout.fileno())
    os.set_inheritable(descriptor, False)
    protocol_stdout = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), 1)
    sys.stdout = protocol_stdout


def main() -> None:
    reserve_protocol_stdout()
    importlib.import_module("mcp_stata.server").main()


if __name__ == "__main__":
    main()
