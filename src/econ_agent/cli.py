"""环境检查入口；研究命令在后续阶段实现。"""

import argparse
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, NoReturn

from econ_agent import __version__
from econ_agent.config import ModelConfig


class ChineseArgumentParser(argparse.ArgumentParser):
    """中文帮助和参数错误；协议名称与命令拼写保持兼容。"""

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法：", 1)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        reason = (
            message
            if any("\u4e00" <= c <= "\u9fff" for c in message)
            else ("无法解析命令或参数，请使用 --help 查看可用选项")
        )
        self.exit(2, f"参数错误：{reason}\n")


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
    parser = ChineseArgumentParser(
        prog="econ-agent", add_help=False, description="通过 Stata MCP 开展实证经济研究"
    )
    options = parser.add_argument_group("选项")
    options.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    options.add_argument("--version", action="version", version=__version__, help="显示程序版本")
    commands = parser.add_subparsers(dest="command", required=True, title="命令")
    doctor = commands.add_parser("doctor", help="检查配置，不调用外部服务", add_help=False)
    doctor_options = doctor.add_argument_group("选项")
    doctor_options.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    doctor_options.add_argument("--backend", default="stata", help="分析后端，首版仅支持 stata")
    doctor_options.add_argument("--json", action="store_true", help="输出脱敏 JSON 配置摘要")
    args = parser.parse_args(argv)
    if args.backend != "stata":
        parser.error("首版仅支持 stata 后端")
    try:
        summary = environment_summary()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"econ-agent {__version__} / 后端=stata / Python {platform.python_version()}")
        print("本次仅检查配置；真实执行情况请以显式探测记录为准。")
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
