"""开发机与 CI 工作流共用的离线检查入口。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    for arguments in (
        ["ruff", "format", "--check", "src", "scripts", "tests"],
        ["ruff", "check", "src", "scripts", "tests"],
        ["mypy"],
        ["pytest"],
        ["econ_agent.cli", "--version"],
    ):
        print(f"正在检查：{' '.join(arguments)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *arguments], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
