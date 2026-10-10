"""生产 Stata MCP 执行器：一个互斥操作覆盖执行、读取、终止核对和归档。"""

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from econ_agent.inputs import check_input, working_copy
from econ_agent.schemas import (
    Artifact,
    CallStatus,
    CoefficientResult,
    ExecutionStatus,
    ExperimentResult,
    ExperimentSpec,
    ExtractionStatus,
    MethodCapability,
    RunStatus,
    ScalarResult,
    ToolCall,
    canonical_json,
    utc_now,
)
from econ_agent.stata_contracts import (
    AnalysisOutcome,
    BackendError,
    PreparedInput,
    StataProgram,
    ToolFailure,
)
from econ_agent.stata_mcp import StataTransport
from econ_agent.stata_methods import (
    SCOPES,
    extract_regression,
    extract_summary,
    extraction_code,
    method_checks,
    verified_scopes,
)
from econ_agent.stata_results import extract_iv, iv_checks, iv_extraction_code
from econ_agent.state import ArtifactIntegrityError, RunStore, StateConflict, new_id

METHOD_COMMANDS = (
    "regress",
    "summarize",
    "xtreg",
    "reghdfe",
    "ivregress",
    "xtabond",
    "gmm",
    "didregress",
    "csdid",
    "rdrobust",
    "teffects",
    "synth",
    "logit",
    "probit",
    "poisson",
    "qreg",
    "arima",
    "var",
    "vec",
    "stcox",
)


@dataclass
class _Session:
    name: str
    trace: list[dict[str, Any]] = field(default_factory=list)
    pid: int | None = None
    task_id: str | None = None
    dispatched: bool = False
    stopped: bool = False
    closing: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _ExecutionFailure(RuntimeError):
    def __init__(self, category: str, message: str, rc: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.rc = rc


def _quote(path: Path) -> str:
    value = str(path.resolve())
    if any(c in value for c in ('"', "`", "$", "\n", "\r")):
        raise ValueError("声明路径含有 Stata 宏或换行字符，请使用无歧义的本地路径")
    return f'"{value}"'


class StataBackend:
    """只操作本对象创建的专用会话，不查询旧实例的后台任务。"""

    def __init__(
        self,
        store: RunStore,
        transport: StataTransport,
        prepared: PreparedInput,
        work_root: Path,
        *,
        timeout_seconds: float = 120,
        seed: int = 2001,
    ) -> None:
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or type(seed) is not int
            or not 0 <= seed <= 2**31 - 1
        ):
            raise ValueError("执行超时和随机种子不合法")
        self.store = store
        self.transport = transport
        self.prepared = prepared
        self.work_root = work_root.resolve()
        if self.work_root == store.directory or store.directory in self.work_root.parents:
            raise ValueError("可变工作目录须位于 run 归档目录之外")
        self.timeout = min(timeout_seconds, store.load_state().budget.max_runtime_seconds)
        self.seed = seed
        self._lock = asyncio.Lock()
        self._active: dict[str, _Session] = {}

    async def _call(self, scope: _Session, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if scope.closing and (
            name not in {"stata_control", "stata_manage_session", "stata_task_status"}
            or (name == "stata_manage_session" and arguments.get("action") not in {"stop", "list"})
        ):
            # 固定服务会自动重建缺失会话，取消后不能发送会触发重建的请求。
            raise _ExecutionFailure("cancelled", "专用会话正在回收，禁止重新创建或读取迟到结果")
        start = time.monotonic()
        try:
            result = await self.transport.call(name, arguments)
        except BaseException as exc:
            scope.trace.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "error_type": type(exc).__name__,
                    "duration_seconds": time.monotonic() - start,
                }
            )
            raise
        scope.trace.append(
            {
                "tool": name,
                "arguments": arguments,
                "response": result,
                "duration_seconds": time.monotonic() - start,
            }
        )
        return result

    async def _create_session(
        self, scope: _Session, *, tool_call_id: str, attempt_id: str | None = None
    ) -> None:
        intent = {
            "schema_version": 1,
            "run_id": self.store.run_id,
            "tool_call_id": tool_call_id,
            "attempt_id": attempt_id,
            "service_instance_id": self.transport.service_instance_id,
            "session_id": scope.name,
        }
        design_id = (
            self.store.experiment(self.store.attempt(attempt_id).experiment_id).design_id
            if attempt_id
            else None
        )
        self.store.archive_bytes(
            f"calls/workers/{tool_call_id}/intent.json",
            json.dumps(intent).encode(),
            kind="tool_response",
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            design_id=design_id,
        )
        scope.ready.clear()
        scope.dispatched = True
        try:
            response = await self._call(
                scope, "stata_manage_session", {"action": "create", "session_id": scope.name}
            )
            if not response.get("success"):
                raise _ExecutionFailure(
                    "configuration", "专用 Stata 会话创建失败，请检查本机许可及安装"
                )
            listed = await self._call(
                scope, "stata_manage_session", {"action": "list", "session_id": scope.name}
            )
            for session in listed.get("data", {}).get("sessions", []):
                if session["id"] == scope.name:
                    scope.pid = int(session["pid"])
            if scope.pid is None:
                raise _ExecutionFailure(
                    "termination", "无法取得专用工作进程身份，禁止假定可安全重试"
                )
            inspector = getattr(self.transport, "process_identity", None)
            identity = inspector(scope.pid) if inspector else None
            self.store.archive_bytes(
                f"calls/workers/{tool_call_id}/identity.json",
                json.dumps(
                    intent
                    | {
                        "worker_pid": scope.pid,
                        "identity": identity.model_dump(mode="json") if identity else None,
                    }
                ).encode(),
                kind="tool_response",
                tool_call_id=tool_call_id,
                attempt_id=attempt_id,
                design_id=design_id,
            )
            configured = await self._call(
                scope,
                "stata_run",
                {
                    "code": "set more off\nset processors 1",
                    "session_id": scope.name,
                    "echo": False,
                },
            )
            if not configured.get("success"):
                raise _ExecutionFailure("configuration", "无法配置已验证的单 processor 会话")
        finally:
            scope.ready.set()

    async def _stop(self, scope: _Session) -> bool:
        scope.closing = True
        if not scope.dispatched:
            return True
        async with scope.stop_lock:
            if scope.stopped:
                return True
            try:
                await asyncio.wait_for(scope.ready.wait(), 35)
                if scope.task_id is not None:
                    # 软取消不是终止证明；即使软取消失败，也继续回收专用会话。
                    try:
                        await self._call(
                            scope, "stata_control", {"action": "cancel", "id": scope.task_id}
                        )
                    except Exception:
                        pass
                response = await self._call(
                    scope, "stata_manage_session", {"action": "stop", "session_id": scope.name}
                )
                if not response.get("success") or scope.pid is None:
                    return False
                deadline = time.monotonic() + 5
                while await self.transport.process_exists(scope.pid):
                    if time.monotonic() >= deadline:
                        return False
                    await asyncio.sleep(0.05)
                scope.stopped = True
                return True
            except Exception:
                return False

    async def cancel(self, attempt_id: str) -> bool:
        scope = self._active.get(attempt_id)
        if scope is None:
            # 不从新服务寻找任何历史 task_id，也不停止非本执行器创建的会话。
            return False
        self.store.request_cancel(attempt_id)
        return await self._stop(scope)

    async def _run_file(
        self, scope: _Session, path: Path, deadline: float, *, attempt_id: str | None = None
    ) -> dict[str, Any]:
        if attempt_id and self.store.attempt(attempt_id).cancel_requested:
            raise _ExecutionFailure("cancelled", "研究者已取消此尝试")
        started = await self._call(
            scope,
            "stata_run",
            {
                "session_id": scope.name,
                "code": str(path),
                "is_file": True,
                "background": True,
                "echo": False,
                "cwd": str(path.parent),
            },
        )
        if not started.get("success"):
            raise _ExecutionFailure("stata", "Stata 拒绝启动本次 do-file")
        task_id = started.get("data", {}).get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise _ExecutionFailure("transport", "MCP 未返回有效后台任务身份")
        scope.task_id = task_id
        if attempt_id:
            self.store.bind_backend_task(attempt_id, task_id)
        while True:
            if attempt_id and self.store.attempt(attempt_id).cancel_requested:
                raise _ExecutionFailure("cancelled", "研究者已取消此尝试")
            if time.monotonic() >= deadline:
                raise _ExecutionFailure("timeout", "Stata 执行超时，已请求取消并核对工作进程")
            response = await self._call(
                scope, "stata_task_status", {"task_id": task_id, "wait": False}
            )
            data = response.get("data", {})
            if data.get("task_id") != task_id:
                raise _ExecutionFailure("transport", "后台状态返回了其他任务的身份")
            status = data.get("status")
            if status in {"done", "failed"}:
                result = data.get("result")
                if not isinstance(result, dict):
                    raise _ExecutionFailure("stata", "后台任务失败，未产生完整命令返回")
                rc = result.get("data", {}).get("rc")
                error_rc = (result.get("error") or {}).get("rc")
                if status != "done" or not result.get("success") or rc != 0:
                    raise _ExecutionFailure(
                        "stata",
                        "Stata 命令执行失败；请依据归档日志修复代码",
                        int(error_rc if error_rc is not None else rc)
                        if error_rc is not None or rc is not None
                        else None,
                    )
                return result
            if status != "running":
                raise _ExecutionFailure("transport", "MCP 无法确认已提交任务的实际状态")
            await asyncio.sleep(0.05)

    def _archive_attempt(
        self, attempt_id: str, name: str, content: bytes, *, kind: str
    ) -> Artifact:
        attempt = self.store.attempt(attempt_id)
        spec = self.store.experiment(attempt.experiment_id)
        return self.store.archive_bytes(
            f"experiments/{spec.experiment_id}/{attempt_id}/{name}",
            content,
            kind=kind,
            attempt_id=attempt_id,
            design_id=spec.design_id,
            tool_call_id=attempt.tool_call_id,
        )

    def _archive_logs(self, scope: _Session, attempt_id: str) -> list[str]:
        paths = {
            str(item["response"].get("log", {}).get("path"))
            for item in scope.trace
            if isinstance(item.get("response", {}).get("log"), dict)
        }
        ids = []
        for index, text in enumerate(sorted(paths)):
            path = Path(text)
            if path.is_file() and not path.is_symlink():
                ids.append(
                    self._archive_attempt(
                        attempt_id, f"logs/stata_{index}.log", path.read_bytes(), kind="log"
                    ).artifact_id
                )
        return ids

    async def run_analysis(
        self, spec: ExperimentSpec, *, attempt_id: str, program: StataProgram
    ) -> AnalysisOutcome:
        spec = ExperimentSpec.model_validate(spec)
        program = StataProgram.model_validate(program)
        async with self._lock:
            if self.store.experiment(spec.experiment_id) != spec:
                raise StateConflict("传入的规格与不可变记录不一致")
            attempt = self.store.attempt(attempt_id)
            if (
                attempt.experiment_id != spec.experiment_id
                or attempt.execution_status != ExecutionStatus.PENDING
            ):
                raise StateConflict("只能执行属于此规格的全新待运行尝试")
            if spec.input_artifact_id != self.prepared.input_artifact_id:
                raise StateConflict("声明输入不属于执行器已保护的数据快照")
            start = time.monotonic()
            scope = _Session(new_id("stata"))
            scope.ready.set()
            work = self.work_root / self.store.run_id / attempt_id
            work.mkdir(parents=True, exist_ok=False)
            output = work / "output"
            output.mkdir()
            body = self._archive_attempt(attempt_id, "body.do", program.code.encode(), kind="code")
            loader = (
                'use "$ECON_INPUT", clear'
                if self.prepared.file_type == "dta"
                else 'import delimited "$ECON_INPUT", clear'
            )
            input_path = work / f"input.{self.prepared.file_type}"
            extraction_code_id: str | None = None
            inline_extraction = ""
            if program.summary:
                inline_extraction = extraction_code(program) or ""
                extraction = self._archive_attempt(
                    attempt_id, "extract.do", inline_extraction.encode(), kind="code"
                )
                extraction_code_id = extraction.artifact_id
            body_entry = f"do {_quote(self.store.directory / body.relative_path)}\n"
            wrapped = (
                "* 本次尝试从独立工作副本开始；原件及备份不作为默认输入。\n"
                "version 18\nclear all\nset more off\nset processors 1\n"
                f"set seed {self.seed}\nglobal ECON_INPUT {_quote(input_path)}\n"
                f"global ECON_OUTPUT {_quote(output)}\n{loader}\n" + body_entry + inline_extraction
            )
            code = self._archive_attempt(attempt_id, "analysis.do", wrapped.encode(), kind="code")
            arguments = self._archive_attempt(
                attempt_id,
                "arguments.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "spec": spec.model_dump(mode="json"),
                        "program": program.model_dump(mode="json"),
                        "body_artifact_id": body.artifact_id,
                        "code_artifact_id": code.artifact_id,
                    },
                    ensure_ascii=False,
                ).encode(),
                kind="tool_arguments",
            )
            call = self.store.tool_call(attempt.tool_call_id)
            if call.arguments_artifact_id is None:
                self.store.update_tool_call(
                    call.model_copy(update={"arguments_artifact_id": arguments.artifact_id}),
                    expected=CallStatus.PENDING,
                )
            self.store.start_attempt(
                attempt_id,
                code.artifact_id,
                service_instance_id=self.transport.service_instance_id,
                session_id=scope.name,
            )
            self._active[attempt_id] = scope
            primary: dict[str, Any] | None = None
            stored: dict[str, Any] | None = None
            failure: ToolFailure | None = None
            files: dict[str, str] = {}
            cancelled_exception = False
            primary_done = False
            try:
                forbidden_paths = (
                    self.prepared.original_path,
                    self.prepared.backup_path,
                    self.store.directory
                    / self.store.artifact(spec.input_artifact_id).relative_path,
                )
                if any(str(path) in program.code for path in forbidden_paths):
                    raise _ExecutionFailure(
                        "arguments",
                        "主体代码须使用工作副本，不能声明原件、备份或不可变快照为写入路径",
                    )
                working_copy(self.store, self.prepared, work)
                await self._create_session(
                    scope, tool_call_id=attempt.tool_call_id, attempt_id=attempt_id
                )
                detected = await self._call(
                    scope,
                    "stata_manage_session",
                    {"action": "detect", "session_id": scope.name, "include_packages": False},
                )
                self.transport.environment["stata"] = detected.get("data", {})
                self.store.artifact(body.artifact_id)
                self.store.artifact(code.artifact_id)
                primary = await self._run_file(
                    scope,
                    self.store.directory / code.relative_path,
                    start + self.timeout,
                    attempt_id=attempt_id,
                )
                primary_done = True
                # 主估计结束后立即读取；锁保持到提取和归档结束。
                stored = await self._call(
                    scope, "stata_get_results", {"session_id": scope.name, "include_matrices": True}
                )
                if not stored.get("success"):
                    raise _ExecutionFailure("extraction", "主命令已完成，但 MCP 读取返回值失败")
                extractor = (
                    iv_extraction_code(program.iv) if program.iv else extraction_code(program)
                )
                if extractor is not None and not program.summary:
                    extraction = self._archive_attempt(
                        attempt_id,
                        "extract.do",
                        extractor.encode(),
                        kind="code",
                    )
                    extraction_code_id = extraction.artifact_id
                    await self._run_file(
                        scope, self.store.directory / extraction.relative_path, start + self.timeout
                    )
                check_input(self.store, self.prepared)
                self.store.artifact(body.artifact_id)
                self.store.artifact(code.artifact_id)
            except asyncio.CancelledError:
                cancelled_exception = True
                self.store.request_cancel(attempt_id)
                failure = ToolFailure(
                    tool_call_id=attempt.tool_call_id,
                    category="cancelled",
                    message="调用任务已取消，正在回收专用 Stata 会话",
                )
            except Exception as exc:
                if isinstance(exc, ArtifactIntegrityError):
                    category, message, rc = "integrity", str(exc), None
                    if self.store.load_state().status == RunStatus.RUNNING:
                        self.store.transition_run(RunStatus.PAUSED, reason=message)
                elif isinstance(exc, _ExecutionFailure):
                    category, message, rc = exc.category, str(exc), exc.rc
                    if primary_done and category == "stata":
                        category, message = (
                            "extraction",
                            "主估计完成，但具名结果提取命令失败；保留原始返回",
                        )
                else:
                    category, message = (
                        ("extraction", "主估计完成，但结果结构暂不能规范化")
                        if primary_done
                        else ("transport", "Stata MCP 连接或执行异常；详情见本次归档")
                    )
                    rc = None
                failure = ToolFailure.model_validate(
                    {
                        "tool_call_id": attempt.tool_call_id,
                        "category": category,
                        "message": message,
                        "stata_return_code": rc,
                        "retryable": category in {"stata", "transport", "timeout"},
                    }
                )
                if category in {"timeout", "cancelled"}:
                    self.store.request_cancel(attempt_id)
            finally:
                stopped = await self._stop(scope)
                self._active.pop(attempt_id, None)
            # 终止后再核对完整性，不能把退出前发生的写坏隐藏在成功状态里。
            try:
                check_input(self.store, self.prepared)
            except ArtifactIntegrityError as exc:
                failure = ToolFailure(
                    tool_call_id=attempt.tool_call_id, category="integrity", message=str(exc)
                )
                if self.store.load_state().status == RunStatus.RUNNING:
                    self.store.transition_run(RunStatus.PAUSED, reason=str(exc))
            for file in sorted(output.rglob("*")):
                if file.is_symlink():
                    failure = ToolFailure(
                        tool_call_id=attempt.tool_call_id,
                        category="integrity",
                        message="本次输出含符号链接，禁止归档外部文件",
                    )
                    continue
                if file.is_file():
                    relative = file.relative_to(output).as_posix()
                    files[relative] = self._archive_attempt(
                        attempt_id, f"outputs/{relative}", file.read_bytes(), kind="raw_result"
                    ).artifact_id
            log_ids = self._archive_logs(scope, attempt_id)
            if (
                failure is not None
                and failure.category == "integrity"
                and self.store.load_state().status == RunStatus.RUNNING
            ):
                self.store.transition_run(RunStatus.PAUSED, reason=failure.message)
            raw = self._archive_attempt(
                attempt_id,
                "raw.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "environment": self.transport.environment,
                        "seed": self.seed,
                        "program": program.model_dump(mode="json"),
                        "body_artifact_id": body.artifact_id,
                        "extraction_code_artifact_id": extraction_code_id,
                        "primary": primary,
                        "stored": stored,
                        "files": files,
                        "logs": log_ids,
                        "trace": scope.trace,
                        "worker_pid": scope.pid,
                        "worker_terminated": stopped,
                        "error": failure.model_dump(mode="json") if failure else None,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode(),
                kind="raw_result",
            )
            if not stopped:
                failure = ToolFailure(
                    tool_call_id=attempt.tool_call_id,
                    category="termination",
                    message="无法确认旧 Stata 工作进程停止，保留暂停状态，禁止重试",
                    diagnostic_artifact_ids=(raw.artifact_id,),
                )
                self.store.interrupt_attempt(attempt_id, reason=failure.message)
            else:
                cancelled = self.store.attempt(attempt_id).cancel_requested
                status = (
                    ExecutionStatus.CANCELLED
                    if cancelled and (failure is None or failure.category != "integrity")
                    else (
                        ExecutionStatus.SUCCEEDED
                        if primary_done and (failure is None or failure.category == "extraction")
                        else ExecutionStatus.FAILED
                    )
                )
                if cancelled and failure is None:
                    failure = ToolFailure(
                        tool_call_id=attempt.tool_call_id,
                        category="cancelled",
                        message="本次尝试已取消，迟到输出仅作排查",
                        diagnostic_artifact_ids=(raw.artifact_id,),
                    )
                self.store.finish_attempt(
                    attempt_id,
                    status,
                    executor_id=self.store.executor_id,
                    termination_confirmed=True,
                    duration_seconds=time.monotonic() - start,
                    raw_artifact_id=raw.artifact_id,
                    error_summary=failure.message if failure else None,
                    error_category=failure.category if failure else None,
                )
            if failure:
                error_artifact = self._archive_attempt(
                    attempt_id, "error.json", canonical_json(failure).encode(), kind="raw_result"
                )
                failure = failure.model_copy(
                    update={
                        "diagnostic_artifact_ids": (
                            raw.artifact_id,
                            error_artifact.artifact_id,
                            *log_ids,
                        )
                    }
                )
            if cancelled_exception:
                raise asyncio.CancelledError
            if (
                self.store.attempt(attempt_id).execution_status == ExecutionStatus.SUCCEEDED
                and failure is None
            ):
                return self.reextract(attempt_id)
            if self.store.attempt(attempt_id).execution_status == ExecutionStatus.SUCCEEDED:
                self.store.mark_extraction(
                    attempt_id,
                    ExtractionStatus.FAILED,
                    reason=failure.message if failure else "结果提取待完成",
                )
            return self._outcome(attempt_id, raw.artifact_id, failure)

    def _outcome(
        self, attempt_id: str, raw_id: str, failure: ToolFailure | None
    ) -> AnalysisOutcome:
        attempt = self.store.attempt(attempt_id)
        spec = self.store.experiment(attempt.experiment_id)
        if attempt.code_artifact_id is None:
            raise StateConflict("尝试缺少已归档的实际执行代码")
        return AnalysisOutcome(
            result=ExperimentResult(
                experiment_id=spec.experiment_id,
                attempt_id=attempt_id,
                design_id=spec.design_id,
                execution_status=attempt.execution_status,
                extraction_status=attempt.extraction_status,
                raw_artifact_ids=(raw_id,),
                input_sha256=self.store.artifact(attempt.input_artifact_id).sha256,
                code_sha256=self.store.artifact(attempt.code_artifact_id).sha256,
            ),
            error=failure,
        )

    def reextract(self, attempt_id: str) -> AnalysisOutcome:
        """只读取已保存输出，不调用 MCP、不新建统计尝试，也不复用旧核验。"""
        attempt = self.store.attempt(attempt_id)
        if (
            attempt.execution_status != ExecutionStatus.SUCCEEDED
            or attempt.cancel_requested
            or attempt.raw_artifact_id is None
        ):
            raise StateConflict("只有未取消且有原始输出的成功尝试可重新提取")
        check_input(self.store, self.prepared)
        payload = json.loads(self.store.read_artifact(attempt.raw_artifact_id))
        program = StataProgram.model_validate(payload["program"])
        if self.store.read_artifact(payload["body_artifact_id"]).decode() != program.code:
            raise ArtifactIntegrityError("主体代码与真实运行记录中的内容不符")
        spec = self.store.experiment(attempt.experiment_id)
        if program.iv is None and program.regression is None and program.summary is None:
            self.store.mark_extraction(
                attempt_id,
                ExtractionStatus.PENDING,
                reason="通用命令执行成功，当前未注册适用的规范化规则；原始返回已保存",
            )
            return self._outcome(attempt_id, attempt.raw_artifact_id, None)
        try:
            name = "scalars.csv" if program.summary else "coefficients.csv"
            named_id = payload["files"][name]
            sample_id = payload["files"]["sample_check.txt"]
            named_csv = self.store.read_artifact(named_id).decode()
            normalized: CoefficientResult | ScalarResult
            if program.iv:
                normalized = extract_iv(payload["stored"], named_csv, spec, program.iv)
            elif program.regression:
                normalized = extract_regression(
                    payload["stored"], named_csv, spec, program.regression
                )
            else:
                assert program.summary is not None
                snapshot_id = payload["files"]["r_snapshot.json"]
                scalar_snapshot = json.loads(self.store.read_artifact(snapshot_id))
                normalized = extract_summary(
                    {"data": {"r": scalar_snapshot}}, named_csv, spec, program.summary
                )
            result = self.store.save_result(attempt_id, normalized)
            evidence: tuple[str, ...] = (
                attempt.raw_artifact_id,
                named_id,
                sample_id,
                payload["body_artifact_id"],
                payload["extraction_code_artifact_id"],
            )
            if program.summary:
                evidence = (*evidence, payload["files"]["r_snapshot.json"])
            sample_match = self.store.read_artifact(sample_id).strip() == b"1"
            if program.iv:
                assert isinstance(normalized, CoefficientResult)
                checks = iv_checks(
                    payload["stored"],
                    normalized,
                    spec,
                    program,
                    sample_match=sample_match,
                    evidence_ids=evidence,
                )
            else:
                checks = method_checks(
                    payload["stored"],
                    normalized,
                    spec,
                    program,
                    sample_match=sample_match,
                    evidence_ids=evidence,
                )
            design = self.store.design(spec.design_id)
            approved = (
                set(spec.variables) <= set(design.content.variables)
                and spec.method in design.content.methods
                and spec.sample_rule == design.content.sample_rule
            )
            if not approved:
                checks = tuple(
                    c.model_copy(
                        update={"status": "fail", "explanation": "声明变量或方法超出当前确认设计"}
                    )
                    if c.category == "specification"
                    else c
                    for c in checks
                )
            validation = self.store.save_validation(
                result.artifact_id,
                checks,
                rules_version="stata-iv-2" if program.iv else "stata-methods-1",
            )
            return AnalysisOutcome(
                result=self.store.result(result.artifact_id),
                validation_artifact_id=validation.artifact_id,
            )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            failure = ToolFailure(
                tool_call_id=attempt.tool_call_id,
                category="extraction",
                message=f"执行成功，结果提取未通过：{type(exc).__name__}；请检查已保存返回",
                diagnostic_artifact_ids=(attempt.raw_artifact_id,),
            )
            self.store.mark_extraction(attempt_id, ExtractionStatus.FAILED, reason=failure.message)
            return self._outcome(attempt_id, attempt.raw_artifact_id, failure)

    async def _query(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        input_id: str | None = None,
        source_model_call_id: str | None = None,
    ) -> dict[str, Any]:
        """一个业务调用包含全部内部 MCP 请求；轮询和生命周期不额外计费。"""
        async with self._lock:
            call = ToolCall(
                tool_call_id=new_id("tool"),
                run_id=self.store.run_id,
                operation_id=new_id("operation"),
                tool_name=tool_name,
                source_model_call_id=source_model_call_id,
                arguments_status="pass",
            )
            self.store.record_tool_call(call)
            prefix = f"calls/{call.tool_call_id}"
            args = self.store.archive_bytes(
                f"{prefix}/arguments.json",
                json.dumps(arguments, ensure_ascii=False).encode(),
                kind="tool_arguments",
                tool_call_id=call.tool_call_id,
            )
            call = call.model_copy(update={"arguments_artifact_id": args.artifact_id})
            self.store.update_tool_call(call, expected=CallStatus.PENDING)
            call = call.model_copy(update={"status": CallStatus.RUNNING, "started_at": utc_now()})
            self.store.update_tool_call(call, expected=CallStatus.PENDING)
            start = time.monotonic()
            scope = _Session(new_id("query"))
            data: dict[str, Any] = {}
            failure: ToolFailure | None = None
            try:
                if tool_name == "get_method_help" and not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_ ]{0,63}", str(arguments.get("topic", ""))
                ):
                    raise _ExecutionFailure("arguments", "帮助主题只允许命令及子主题名称")
                await self._create_session(scope, tool_call_id=call.tool_call_id)
                if input_id:
                    if input_id != self.prepared.input_artifact_id:
                        raise ValueError("数据检查输入不属于已保护的任务")
                    work = self.work_root / self.store.run_id / call.tool_call_id
                    work.mkdir(parents=True, exist_ok=False)
                    source = working_copy(self.store, self.prepared, work)
                    loader = (
                        f"use {_quote(source)}, clear"
                        if self.prepared.file_type == "dta"
                        else f"import delimited {_quote(source)}, clear"
                    )
                    loaded = await self._call(
                        scope,
                        "stata_run",
                        {
                            "code": f"set more off\nset processors 1\n{loader}",
                            "session_id": scope.name,
                            "echo": False,
                        },
                    )
                    if not loaded.get("success"):
                        raise _ExecutionFailure("stata", "Stata 数据检查加载失败")
                if tool_name == "capabilities":
                    commands = []
                    for command in METHOD_COMMANDS:
                        found = await self._call(
                            scope,
                            "stata_run",
                            {
                                "code": (
                                    f"capture which {command}\nlocal econ_found=(_rc==0)\n"
                                    'display "ECON_AVAILABLE=`econ_found\'"'
                                ),
                                "session_id": scope.name,
                                "echo": False,
                            },
                        )
                        available = "ECON_AVAILABLE=1" in str(
                            found.get("data", {}).get("stdout", "")
                        )
                        commands.append(
                            {
                                "command": command,
                                "available": available,
                                "which_output": found.get("data", {}).get("stdout", ""),
                            }
                        )
                    data = {
                        "commands": commands,
                        "normalization_rules": SCOPES,
                        "validation": "unverified",
                    }
                else:
                    name = "stata_inspect_data" if tool_name == "inspect_data" else "stata_get_help"
                    response = await self._call(
                        scope, name, {**arguments, "session_id": scope.name}
                    )
                    if not response.get("success"):
                        raise _ExecutionFailure(
                            "missing_command" if tool_name == "get_method_help" else "stata",
                            "Stata 方法帮助不可用"
                            if tool_name == "get_method_help"
                            else "Stata 数据检查失败",
                        )
                    data = dict(response.get("data", {}))
                    if tool_name == "inspect_data":
                        summary = await self._call(
                            scope,
                            "stata_inspect_data",
                            {
                                "action": "summary",
                                "session_id": scope.name,
                            },
                        )
                        if not summary.get("success"):
                            raise _ExecutionFailure("stata", "Stata 样本与缺失检查失败")
                        data["summary"] = summary.get("data", {})
                check_input(self.store, self.prepared)
            except asyncio.CancelledError:
                failure = ToolFailure(
                    tool_call_id=call.tool_call_id, category="cancelled", message="业务工具已取消"
                )
            except Exception as exc:
                failure = ToolFailure.model_validate(
                    {
                        "tool_call_id": call.tool_call_id,
                        "category": exc.category
                        if isinstance(exc, _ExecutionFailure)
                        else "integrity"
                        if isinstance(exc, ArtifactIntegrityError)
                        else "transport",
                        "message": str(exc)
                        if isinstance(exc, (_ExecutionFailure, ArtifactIntegrityError))
                        else "业务工具连接异常；详情见归档",
                    }
                )
            stopped = await self._stop(scope)
            if not stopped:
                failure = ToolFailure(
                    tool_call_id=call.tool_call_id,
                    category="termination",
                    message="业务工具工作进程未确认停止，需要暂停处理",
                )
            archived = self.store.archive_bytes(
                f"{prefix}/response.json",
                json.dumps(
                    {
                        "data": data,
                        "trace": scope.trace,
                        "worker_terminated": stopped,
                        "error": failure.model_dump(mode="json") if failure else None,
                    },
                    ensure_ascii=False,
                ).encode(),
                kind="tool_response",
                tool_call_id=call.tool_call_id,
            )
            updated = call.model_copy(
                update={
                    "status": CallStatus.SUCCEEDED
                    if failure is None
                    else CallStatus.UNKNOWN
                    if not stopped
                    else CallStatus.CANCELLED
                    if failure.category == "cancelled"
                    else CallStatus.FAILED,
                    "ended_at": utc_now(),
                    "duration_seconds": time.monotonic() - start,
                    "response_artifact_id": archived.artifact_id,
                    "error_category": failure.category if failure else None,
                    "error_summary": failure.message if failure else None,
                }
            )
            self.store.update_tool_call(updated, expected=CallStatus.RUNNING)
            if failure:
                if (
                    failure.category in {"integrity", "termination"}
                    and self.store.load_state().status == RunStatus.RUNNING
                ):
                    self.store.transition_run(RunStatus.PAUSED, reason=failure.message)
                if failure.category == "cancelled":
                    raise asyncio.CancelledError
                raise BackendError(
                    failure.model_copy(update={"diagnostic_artifact_ids": (archived.artifact_id,)})
                )
            return data

    async def inspect_data(
        self, input_artifact_id: str, *, source_model_call_id: str | None = None
    ) -> str:
        described = await self._query(
            "inspect_data",
            {"action": "describe"},
            input_id=input_artifact_id,
            source_model_call_id=source_model_call_id,
        )
        return json.dumps(
            {"说明": "本机 Stata 读取的变量与数据描述", "data": described}, ensure_ascii=False
        )

    async def get_method_help(self, method: str, *, source_model_call_id: str | None = None) -> str:
        data = await self._query(
            "get_method_help", {"topic": method}, source_model_call_id=source_model_call_id
        )
        return json.dumps(
            {"说明": "已保存本机 Stata 帮助原文，请用中文解释适用条件", "data": data},
            ensure_ascii=False,
        )

    async def capabilities(self) -> tuple[MethodCapability, ...]:
        data = await self._query("capabilities", {})
        verified = verified_scopes(self.transport.environment)
        return tuple(
            MethodCapability(
                method=item["command"],
                commands=(item["command"],),
                availability="available" if item["available"] else "unavailable",
                normalization="supported" if item["command"] in SCOPES else "unknown",
                normalization_scope=SCOPES.get(item["command"]),
                validation=(
                    "validated"
                    if item["available"] and item["command"] in verified
                    else "unverified"
                ),
                tested_environment=(
                    "Stata 18.0.185，本机库 hash 与 MCP 3.3.0 精确匹配"
                    if item["available"] and item["command"] in verified
                    else None
                ),
                validation_scope=verified.get(item["command"]) if item["available"] else None,
                limitations=("仅查询本机命令，不能据此认定已通过数值测试或识别假设成立",),
            )
            for item in data["commands"]
        )
