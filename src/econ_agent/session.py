"""研究命令的运行装配、只读状态及停止快照；外部连接延迟创建。"""

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from econ_agent.config import ModelConfig
from econ_agent.interaction import apply_feedback
from econ_agent.model_client import OpenAITransport, PersistentModelClient, redact
from econ_agent.schemas import RunStatus
from econ_agent.state import RunStore, StateConflict, new_id


@dataclass(frozen=True)
class RuntimeConfig:
    workspace: Path
    executable: Path
    model: ModelConfig

    @classmethod
    def load(cls, workspace: Path, saved: dict[str, Any] | None = None) -> "RuntimeConfig":
        workspace = workspace.resolve()
        candidate = workspace / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp"
        default = (
            str(candidate)
            if candidate.is_file()
            else "/Applications/Stata/StataMP.app/Contents/MacOS/stata-mp"
        )
        saved = saved or {}
        executable = (
            Path(os.getenv("STATA_PATH", str(saved.get("stata_path", default))))
            .expanduser()
            .resolve()
        )
        return cls(workspace, executable, ModelConfig.from_environment())

    def validate_execution(self) -> None:
        import importlib.metadata

        if not self.executable.is_file():
            raise ValueError("找不到本机已配置的 Stata；请设置 STATA_PATH，不自动下载更新包")
        if not self.model.api_key():
            raise ValueError("缺少现有模型凭证；请配置密钥环境变量")
        if importlib.metadata.version("mcp-stata") != "3.3.0":
            raise ValueError("需要锁文件规定的 mcp-stata 3.3.0")


def status_summary(store: RunStore) -> dict[str, Any]:
    from econ_agent.reporting import current_report

    state = store.load_state()
    issues = []
    try:
        results = store.selected_results()
    except StateConflict:
        results = ()
        issues.append("当前结果的文件或核验不满足读取要求，未将其作为有效数值")
    history = []
    report = current_report(store)
    if state.status == RunStatus.COMPLETED and report is None:
        issues.append("已完成时的论文目前未通过证据复核，不能作为当前有效报告")
    for specification in store.experiments():
        history.append(
            {
                "specification": specification.model_dump(mode="json"),
                "current_design": specification.design_id == state.current_design_id,
                "attempts": [
                    a.model_dump(mode="json") for a in store.attempts(specification.experiment_id)
                ],
            }
        )
    return cast(
        dict[str, Any],
        redact(
            {
                "schema_version": 1,
                "run": state.model_dump(mode="json"),
                "research_goal": store.load_task().research_goal,
                "designs": [
                    {
                        "design": d.model_dump(mode="json"),
                        "confirmation": store.design_confirmation(d.design_id),
                    }
                    for d in store.designs()
                ],
                "pending_requests": [
                    store.human_request(r).model_dump(mode="json")
                    for r in state.pending_request_ids
                ],
                "analyses": history,
                "selected_results": [r.model_dump(mode="json") for r in results],
                "calls": store.call_summary(),
                "report": {k: report[k] for k in ("report_id", "status", "issue")}
                if report
                else None,
                "issues": issues,
            }
        ),
    )


def print_status(store: RunStore, *, as_json: bool = False) -> None:
    summary = status_summary(store)
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    state = store.load_state()
    version = (
        store.design(state.current_design_id).version if state.current_design_id else "尚未确认"
    )
    print(
        f"运行：{state.run_id}；后端：stata；状态：{state.status}；当前设计：{version}", flush=True
    )
    print(
        f"分析轮次：{state.research_round}/{state.budget.max_research_rounds}；当前有效结果：{len(summary['selected_results'])}"
    )
    calls = summary["calls"]
    known_total = calls["known_total_tokens"]
    total_label = known_total if known_total is not None else "未知"
    print(
        f"模型请求：{calls['model_calls']}，传输失败：{calls['model_transport_failures']}，解析失败：{calls['model_parse_failures']}，重试：{calls['model_retries']}，未终结：{calls['unfinished_model_calls']}"
    )
    print(
        f"工具调用：{calls['tool_calls']}，失败：{calls['tool_failures']}，重试：{calls['tool_retries']}，未终结：{calls['unfinished_tool_calls']}"
    )
    print(f"已知总用量：{total_label}；缺失总用量请求：{calls['missing_total_usage_calls']}")
    if summary["report"]:
        print(
            "当前论文：" + summary["report"]["status"] + "；版本：" + summary["report"]["report_id"]
        )
    if state.stop_reason:
        print("说明：" + state.stop_reason)
    for request in summary["pending_requests"]:
        print("待答：" + request["question"])
    for issue in summary["issues"]:
        print("待核对：" + issue)


def show_request(store: RunStore) -> None:
    request = store.human_request(store.load_state().pending_request_ids[0])
    design = store.design(request.target_design_id)
    print(
        f"\n{request.question}\n设计版本：{design.version}；状态：{store.design_confirmation(design.design_id)}",
        flush=True,
    )
    print(json.dumps(design.content.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(f"请求 ID：{request.request_id}\n设计 hash：{request.target_design_sha256}")
    if design.version > 1:
        previous = next((d for d in store.designs() if d.version == design.version - 1), None)
        if previous:
            changes = {
                k: {"原值": v, "新值": design.content.model_dump(mode="json")[k]}
                for k, v in previous.content.model_dump(mode="json").items()
                if v != design.content.model_dump(mode="json")[k]
            }
            print("修改与影响：" + json.dumps(changes, ensure_ascii=False))
            print("确认新版本后旧结果保留为历史；当前版本须重新分析。")
    print(
        "请输入 /accept、/revise <说明>、/reject <理由>、/pause、/stop 或自然语言意见。", flush=True
    )


class TerminalInput:
    """用文件描述符读取明确交互点，Ctrl+C 不留下阻塞的输入线程。"""

    def __init__(self) -> None:
        self.buffer = b""

    async def read(self) -> str | None:
        while b"\n" not in self.buffer:
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[bytes] = loop.create_future()
            fd = sys.stdin.fileno()

            def receive(_ready: asyncio.Future[bytes] = ready, _fd: int = fd) -> None:
                if not _ready.done():
                    try:
                        _ready.set_result(os.read(_fd, 4096))
                    except OSError as exc:
                        _ready.set_exception(exc)

            loop.add_reader(fd, receive)
            try:
                block = await ready
            finally:
                loop.remove_reader(fd)
            if not block:
                if not self.buffer:
                    return None
                break
            self.buffer += block
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode("utf-8").rstrip("\r")


def stop_snapshot(store: RunStore, workspace: Path) -> Path:
    destination = workspace / "output" / store.run_id / new_id("stopped")
    destination.mkdir(parents=True)
    summary = status_summary(store)
    (destination / "status.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    (destination / "results.json").write_text(
        json.dumps(summary["selected_results"], ensure_ascii=False, indent=2) + "\n"
    )
    (destination / "说明.txt").write_text(
        "这是阶段 5 的现有材料快照，不是论文或完整复现包。"
        "仅包含当前有效结果及分析历史，未包含模型原始请求、响应、备份或历史报告。\n"
    )
    for result in summary["selected_results"]:
        attempt = store.attempt(result["attempt_id"])
        for artifact in store.artifacts(kind="code"):
            if artifact.attempt_id == attempt.attempt_id:
                name = attempt.attempt_id + "-" + Path(artifact.relative_path).name
                (destination / name).write_bytes(store.read_artifact(artifact.artifact_id))
    return destination


def stop_delivery(store: RunStore, workspace: Path) -> Path:
    """停止交互后导出现有记录；缺少有效论文时明确标记部分交付。"""
    from econ_agent.exporting import export_run

    return export_run(store, workspace / "output")


def progress(feedback: dict[str, Any]) -> None:
    names = {
        "run_analysis": "统计分析",
        "propose_design": "提出研究设计",
        "ask_researcher": "等待研究者意见",
        "review_results": "两项结果审阅",
        "finish_research": "研究循环结束",
        "get_method_help": "方法帮助",
        "inspect_data": "数据检查",
        "read_material": "读取材料",
    }
    print("步骤：" + names.get(feedback.get("action", ""), "模型响应校验"), flush=True)
    if feedback.get("error"):
        print("本步未完成，详细原因已保存，程序按预算决定后续处理。", flush=True)
    normalized = feedback.get("outcome", {}).get("result", {}).get("normalized")
    if normalized:
        print(f"已保存实际结果，样本量：{normalized['sample_size']}。", flush=True)


async def run_research(store: RunStore, config: RuntimeConfig) -> None:
    # status、人工暂停/拒绝和读取已保存问题不会执行这些导入或连接。
    from econ_agent.harness import ResearchLoop
    from econ_agent.inputs import prepare_input
    from econ_agent.review import ResultReviewer
    from econ_agent.stata_backend import StataBackend
    from econ_agent.stata_mcp import StataMCPTransport

    config.validate_execution()
    prepared = prepare_input(store, config.workspace / "backups")
    transport = OpenAITransport(config.model)
    try:
        log = config.workspace / "local_reference/runtime" / store.run_id / (new_id("mcp") + ".log")
        async with StataMCPTransport(config.executable, config.workspace, log) as stata:
            model = PersistentModelClient(store, config.model, transport, reserved_model_calls=1)
            backend = StataBackend(store, stata, prepared, config.workspace / "tmp/stata")
            loop = ResearchLoop(
                store,
                model,
                backend,
                prepared.input_artifact_id,
                reviewer=ResultReviewer(store, model),
                on_feedback=progress,
                reserved_model_calls=1,
            )
            await loop.run()
    finally:
        await transport.close()
    state = store.load_state()
    if (
        state.status == RunStatus.PAUSED
        and not state.pending_request_ids
        and (state.stop_reason or "").startswith("研究循环已结束")
    ):
        from econ_agent.reporting import generate_report

        print("正在整理当前核验结果并生成论文初稿。", flush=True)
        report = await generate_report(store)
        print(f"论文交付：{report['status']}；版本：{report['report_id']}", flush=True)


async def conduct(
    store: RunStore,
    workspace: Path,
    *,
    no_input: bool = False,
    answer: str | None = None,
    request_id: str | None = None,
    design_sha256: str | None = None,
    feedback: str | None = None,
    resumed: bool = False,
) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    signalled = False

    def interrupt() -> None:
        nonlocal signalled
        if not signalled:
            signalled = True
            print("\n已请求暂停，正在回收本次在途操作并保存状态，请等待。", flush=True)
            if task:
                task.cancel()

    loop.add_signal_handler(signal.SIGINT, interrupt)
    terminal = TerminalInput()
    try:
        if resumed:
            from econ_agent.recovery import reconcile_interrupted

            issues = await reconcile_interrupted(store)
            if issues:
                print("恢复需要核对，未启动模型或新统计任务：" + "；".join(issues), flush=True)
                return 1
        state = store.load_state()
        if state.status in {RunStatus.COMPLETED, RunStatus.STOPPED, RunStatus.FAILED}:
            print_status(store)
            return 0
        if feedback is not None:
            if state.pending_request_ids or not state.current_design_id:
                raise StateConflict("补充指令须用于已确认设计且没有待答问题的暂停会话")
            if state.status != RunStatus.PAUSED:
                raise StateConflict("请先暂停再补充指令")
            store.transition_run(RunStatus.RUNNING)
            request = store.request_human(
                state.current_design_id, "恢复前请补充指令或修改设计", kind="research_question"
            )
            answer, request_id, design_sha256 = (
                feedback,
                request.request_id,
                request.target_design_sha256,
            )
        elif (
            resumed
            and answer is None
            and state.status == RunStatus.PAUSED
            and not state.pending_request_ids
            and (state.stop_reason or "").startswith("研究循环已结束")
        ):
            print_status(store)
            print("研究循环已经结束；如需修改设计，可用 resume --feedback 补充指令。")
            return 0
        if answer is not None:
            assert request_id and design_sha256
            if store.human_request(request_id).target_design_sha256 != design_sha256:
                raise StateConflict("输入的设计 hash 与等待的问题不符，未应用答复")
            config = (
                ModelConfig.from_environment()
                if not answer.startswith("/") or answer.startswith("/revise ")
                else None
            )
            transport = OpenAITransport(config) if config else None
            try:
                response = await apply_feedback(
                    store,
                    answer,
                    request_id=request_id,
                    design_sha256=design_sha256,
                    model=PersistentModelClient(store, config, transport)
                    if config and transport
                    else None,
                )
                print(response.get("说明", "答复已保存。"), flush=True)
                if response["status"] == "idempotent":
                    print_status(store)
                    return 0
            finally:
                if transport:
                    await transport.close()
            if store.load_state().status == RunStatus.STOPPED:
                print("已有材料复现包：" + str(stop_delivery(store, workspace)))
                return 0
            if store.load_state().status == RunStatus.PAUSED:
                print_status(store)
                return 0
        elif store.load_state().status in {RunStatus.NEW, RunStatus.PAUSED}:
            store.transition_run(RunStatus.RUNNING)
        while True:
            state = store.load_state()
            if state.status == RunStatus.RUNNING:
                config_runtime = RuntimeConfig.load(workspace, store.load_config())
                await run_research(store, config_runtime)
            if store.load_state().status != RunStatus.WAITING_HUMAN:
                print_status(store)
                return 0
            show_request(store)
            if no_input or not sys.stdin.isatty():
                print("问题已保存。可退出后用 resume 继续；status 可只读查询。", flush=True)
                return 0
            raw = await terminal.read()
            if raw is None:
                store.transition_run(RunStatus.PAUSED, reason="终端输入结束，待答问题已保存")
                return 0
            request = store.human_request(store.load_state().pending_request_ids[0])
            configuration = (
                ModelConfig.from_environment()
                if not raw.startswith("/") or raw.startswith("/revise ")
                else None
            )
            transport = OpenAITransport(configuration) if configuration else None
            try:
                response = await apply_feedback(
                    store,
                    raw,
                    request_id=request.request_id,
                    design_sha256=request.target_design_sha256,
                    model=PersistentModelClient(store, configuration, transport)
                    if configuration and transport
                    else None,
                )
                print(response.get("说明", "答复已保存。"), flush=True)
            except (ValueError, StateConflict) as exc:
                print("答复未应用：" + redact(str(exc)), flush=True)
            finally:
                if transport:
                    await transport.close()
            if store.load_state().status == RunStatus.STOPPED:
                print("已有材料复现包：" + str(stop_delivery(store, workspace)))
                return 0
            if store.load_state().status == RunStatus.PAUSED:
                print_status(store)
                return 0
    except asyncio.CancelledError:
        if store.load_state().status in {RunStatus.RUNNING, RunStatus.WAITING_HUMAN}:
            store.transition_run(
                RunStatus.PAUSED, reason="研究者请求暂停；已保存问题、结果及在途操作的回收记录"
            )
        print_status(store)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        if store.load_state().status == RunStatus.RUNNING:
            store.transition_run(RunStatus.PAUSED, reason="执行环境或恢复状态需要核对，未继续研究")
        message = (
            str(exc)
            if isinstance(exc, ValueError) and any("\u4e00" <= c <= "\u9fff" for c in str(exc))
            else "环境或执行连接不可用，请检查既有配置和保存记录"
        )
        print("未继续：" + redact(message), file=sys.stderr)
        return 1
    finally:
        loop.remove_signal_handler(signal.SIGINT)
