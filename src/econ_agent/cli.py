"""中文命令行入口；状态查询不创建模型或 Stata 连接。"""

import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, NoReturn

from pydantic import TypeAdapter, ValidationError

from econ_agent import __version__
from econ_agent.config import ModelConfig
from econ_agent.schemas import Identifier, ResearchTask
from econ_agent.session import RuntimeConfig, conduct, print_status
from econ_agent.state import RunStore


class ChineseArgumentParser(argparse.ArgumentParser):
    """中文帮助和参数错误；协议名称与命令拼写保持兼容。"""

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage:", "用法：", 1)
            .replace("positional arguments:", "位置参数：")
            .replace("options:", "选项：")
            .replace("show this help message and exit", "显示帮助并退出")
        )

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
    commands = parser.add_subparsers(dest="command", title="命令")
    doctor = commands.add_parser("doctor", help="检查配置，不调用外部服务", add_help=False)
    doctor_options = doctor.add_argument_group("选项")
    doctor_options.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    doctor_options.add_argument("--backend", default="stata", help="分析后端，首版仅支持 stata")
    doctor_options.add_argument("--json", action="store_true", help="输出脱敏 JSON 配置摘要")
    start = commands.add_parser("start", help="启动研究；没有任务文件时逐项询问")
    start.add_argument("--task", type=Path, help="任务 JSON 文件")
    start.add_argument("--backend", help="分析后端，首版仅支持 stata")
    start.add_argument("--workspace", type=Path, default=Path.cwd(), help="运行与输出目录的根目录")
    start.add_argument("--no-input", action="store_true", help="待答问题保存后退出")
    resume = commands.add_parser("resume", help="恢复已有研究，保留原后端")
    resume.add_argument("run_id", help="运行 ID")
    resume.add_argument("--workspace", type=Path, default=Path.cwd())
    resume.add_argument("--no-input", action="store_true", help="待答问题保存后退出")
    answer_group = resume.add_mutually_exclusive_group()
    answer_group.add_argument("--answer", help="回答已保存的问题，包括控制命令")
    answer_group.add_argument("--feedback", help="给没有待答问题的暂停会话补充指令")
    resume.add_argument("--request-id", help="答复绑定的请求 ID")
    resume.add_argument("--design-sha256", help="答复绑定的设计 hash")
    status = commands.add_parser("status", help="只读查询研究状态，不连接外部服务")
    status.add_argument("run_id", help="运行 ID")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    status.add_argument("--json", action="store_true", help="输出脱敏 JSON 状态")
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["start"])
    if args.command != "doctor":
        return research_command(parser, args)
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


def research_command(parser: ChineseArgumentParser, args: argparse.Namespace) -> int:
    """先校验输入与后端，再创建或打开本地研究目录。"""
    try:
        workspace = args.workspace.expanduser().resolve()
        if args.command == "start":
            if args.backend is not None and args.backend != "stata":
                parser.error("首版仅支持 stata 后端，未创建会话")
            task = (
                ResearchTask.from_file(args.task, analysis_backend=args.backend)
                if args.task
                else interactive_task()
            )
            runtime = RuntimeConfig.load(workspace)
            runtime.validate_execution()
            with RunStore.create(
                workspace / "runs",
                task,
                config={
                    "stata_path": str(runtime.executable),
                    "model_url": runtime.model.base_url,
                    "model": runtime.model.model,
                    "key_variable": runtime.model.key_variable,
                },
            ) as store:
                print("已创建运行：" + store.run_id, flush=True)
                return asyncio.run(conduct(store, workspace=workspace, no_input=args.no_input))
        identifier = TypeAdapter(Identifier).validate_python(args.run_id)
        if args.command == "status":
            with RunStore.open(workspace / "runs" / identifier, writable=False) as store:
                print_status(store, as_json=args.json)
            return 0
        if args.answer is not None and (not args.request_id or not args.design_sha256):
            parser.error("--answer 必须同时提供 --request-id 与 --design-sha256")
        if args.answer is None and (args.request_id or args.design_sha256):
            parser.error("请求 ID 与设计 hash 只能和 --answer 一起使用")
        with RunStore.open(workspace / "runs" / identifier) as store:
            return asyncio.run(
                conduct(
                    store,
                    workspace=workspace,
                    no_input=args.no_input,
                    answer=args.answer,
                    request_id=args.request_id,
                    design_sha256=args.design_sha256,
                    feedback=args.feedback,
                    resumed=True,
                )
            )
    except KeyboardInterrupt:
        print("已取消输入，尚未启动新的研究操作。", file=sys.stderr)
        return 130
    except ValidationError as exc:
        message = (
            "首版仅支持 stata 后端，未创建会话。"
            if any(e["loc"] == ("analysis_backend",) for e in exc.errors())
            else "输入校验失败：请核对任务字段、预算或运行 ID。"
        )
        print(message, file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError) and any("\u4e00" <= c <= "\u9fff" for c in str(exc))
            else "无法读取会话或既有执行配置，请检查路径与环境"
        )
        print("未执行：" + message, file=sys.stderr)
        return 1


def interactive_task() -> ResearchTask:
    """只询问尚未保存的研究输入，材料路径用分号分隔。"""
    data = Path(input("数据文件路径：").strip()).expanduser().resolve()
    goal = input("研究目标：").strip()
    materials = input("材料路径（可留空，多份用分号分隔）：").strip()
    dictionary = input("变量字典路径（可留空）：").strip()
    return ResearchTask(
        data_path=data,
        research_goal=goal,
        material_paths=tuple(
            Path(p.strip()).expanduser().resolve() for p in materials.split(";") if p.strip()
        ),
        dictionary_path=Path(dictionary).expanduser().resolve() if dictionary else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
