"""在仓库外建立非 editable 环境，验收 wheel、sdist、资源与只读 CLI。"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="候选包独立安装验收")
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--execute-stata", action="store_true")
    args = parser.parse_args()
    packages = args.packages.resolve()
    directory = Path(tempfile.mkdtemp(prefix="econ-agent-install-"))
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    def execute(*arguments: str, expected: int = 0) -> str:
        response = subprocess.run(
            arguments,
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        with (directory / "commands.log").open("a") as log:
            log.write(response.stdout + response.stderr)
        if response.returncode != expected:
            raise RuntimeError("独立安装验收失败，请检查本地日志：" + str(directory))
        return response.stdout

    wheel = packages / "econ_agent-0.1.0rc1-py3-none-any.whl"
    source = packages / "econ_agent-0.1.0rc1.tar.gz"
    manifest = json.loads((packages / "release-manifest.json").read_text())
    for file in (wheel, source):
        assert (
            hashlib.sha256(file.read_bytes()).hexdigest()
            == manifest["archives"][file.name]["sha256"]
        )
    # 成员安全已由构建器核验；在独立目录重建，不能依赖仓库中的遗漏文件。
    with tarfile.open(source) as archive:
        archive.extractall(directory / "source", filter="data")
    extracted = next((directory / "source").iterdir())
    execute(
        "uv",
        "build",
        "--offline",
        "--wheel",
        str(extracted),
        "--out-dir",
        str(directory / "rebuilt"),
    )
    rebuilt = next((directory / "rebuilt").glob("*.whl"))
    with zipfile.ZipFile(wheel) as a, zipfile.ZipFile(rebuilt) as b:
        assert {n: a.read(n) for n in a.namelist()} == {n: b.read(n) for n in b.namelist()}, (
            "源码重建不一致"
        )
    execute(
        "uv", "venv", "--python", sys.executable, "--no-python-downloads", str(directory / "venv")
    )
    python = directory / "venv/bin/python"
    cli = directory / "venv/bin/econ-agent"
    execute(
        "uv",
        "pip",
        "install",
        "--offline",
        "--python",
        str(python),
        "--require-hashes",
        "-r",
        str(packages / "requirements-stata.txt"),
    )
    execute("uv", "pip", "install", "--offline", "--python", str(python), "--no-deps", str(wheel))
    assert execute(str(cli), "--version").strip() == "0.1.0rc1"
    doctor = json.loads(
        execute(
            str(cli),
            "doctor",
            "--json",
            expected=0
            if (
                environment.get(environment.get("ECON_AGENT_KEY_VARIABLE", "DEEPSEEK_API_KEY"))
                and Path(environment.get("STATA_PATH", "/not-configured")).is_file()
            )
            else 1,
        )
    )
    assert doctor["mcp"]["installed_version"] == "3.3.0"
    arguments = [
        str(python),
        "-I",
        str(extracted / "scripts/installed_smoke.py"),
        "--workspace",
        str(directory / "workspace"),
        "--data",
        str(extracted / "examples/quickstart/synthetic.csv"),
    ]
    if args.execute_stata:
        arguments.append("--execute-stata")
    execute(*arguments)
    proof = json.loads((directory / "workspace/installation.json").read_text())
    if args.execute_stata:
        reference = json.loads((extracted / "examples/methods/stata18_reference.json").read_text())
        for method, case in (("regress", "linear"), ("summarize", "summary")):
            actual, expected_result = proof["cases"][method], reference["cases"][case]["normalized"]
            assert actual["sample_size"] == expected_result["sample_size"]
            rows = "estimates" if method == "regress" else "values"
            for observed, expected_row in zip(actual[rows], expected_result[rows], strict=True):
                assert observed["name"] == expected_row["name"]
                for key, value in expected_row.items():
                    if isinstance(value, (int, float)):
                        assert math.isclose(observed[key], value, rel_tol=1e-6, abs_tol=1e-7)
    identifier = proof["run_id"]
    # 删除配置后状态和导出仍成功；不启动 MCP 或模型。
    for key in (
        environment.get("ECON_AGENT_KEY_VARIABLE", "DEEPSEEK_API_KEY"),
        "STATA_PATH",
        "ECON_AGENT_STATA_HOME",
    ):
        environment.pop(key, None)
    state = json.loads(
        execute(
            str(cli), "status", identifier, "--workspace", str(directory / "workspace"), "--json"
        )
    )
    assert state["run"]["status"] == "PAUSED"
    execute(str(cli), "export", identifier, "--workspace", str(directory / "workspace"))
    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "application_version": "0.1.0rc1",
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "sdist_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_rebuild_identical": True,
        "outside_repository": True,
        "non_editable": True,
        "resource_present": True,
        "locked_dependencies": True,
        "readonly_cli_without_credentials": True,
        "model_called": False,
        "real_stata": args.execute_stata,
        "independent_reference_matched": args.execute_stata,
        "cases": proof["cases"],
        "environment": proof.get("environment"),
    }
    (packages / "installation-validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        "安装、源码重建、只读 CLI"
        + ("及真实 Stata" if args.execute_stata else "")
        + "验收通过："
        + str(directory)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
