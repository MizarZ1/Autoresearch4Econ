"""Stage 0 entry point. Research commands will be added in later stages."""

import argparse
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from econ_agent import __version__
from econ_agent.config import ModelConfig


def environment_summary() -> dict[str, Any]:
    config = ModelConfig.from_environment()
    stata_home = Path(os.getenv("STATA_PATH", "/Applications/Stata"))
    try:
        mcp_version: str | None = importlib.metadata.version("mcp-stata")
    except importlib.metadata.PackageNotFoundError:
        mcp_version = None
    return {
        "schema_version": 1,
        "application_version": __version__,
        "analysis_backend": "stata",
        "python": {"version": platform.python_version(), "supported": sys.version_info >= (3, 11)},
        "model": {
            "endpoint": config.base_url,
            "model": config.model,
            "key_configured": config.api_key() is not None,
            "execution_status": "unverified",
        },
        "stata": {
            "installation_present": (
                stata_home.is_file()
                or (stata_home / "StataMP.app/Contents/MacOS/stata-mp").is_file()
            ),
            "execution_status": "unverified",
        },
        "mcp": {"installed_version": mcp_version, "execution_status": "unverified"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="econ-agent")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect configuration without external calls")
    doctor.add_argument("--backend", choices=["stata"], default="stata")
    doctor.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = environment_summary()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"econ-agent {__version__} / backend=stata / Python {platform.python_version()}")
        print("Configuration inspection only; live execution remains unverified.")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    ready = (
        summary["python"]["supported"]
        and summary["model"]["key_configured"]
        and summary["stata"]["installation_present"]
        and summary["mcp"]["installed_version"] is not None
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
