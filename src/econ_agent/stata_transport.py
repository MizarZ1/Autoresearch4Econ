"""Keep native Stata stdout out of the MCP protocol on POSIX hosts.

The pinned server's spawned workers inherit file descriptor 1. Stata 18 can
leave a prompt without a newline, contaminating the next JSON-RPC message.
Reserve a separate Python stdout for MCP, and route inherited native stdout to
stderr before starting the unchanged third-party server.
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
