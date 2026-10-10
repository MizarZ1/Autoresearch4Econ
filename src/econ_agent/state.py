"""单个研究 run 的九表存储、文件归档、人工确认与保守恢复。"""

import fcntl
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, TypeVar, cast
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from econ_agent import __version__
from econ_agent.migrations import StateFormatError, prepare_database
from econ_agent.schemas import (
    Artifact,
    Attempt,
    CallStatus,
    Contract,
    DesignContent,
    ExecutionStatus,
    ExperimentResult,
    ExperimentSpec,
    ExtractionStatus,
    FileFingerprint,
    HumanAnswer,
    HumanRequest,
    Identifier,
    InputManifest,
    ModelCall,
    NormalizedResult,
    ParseStatus,
    ResearchDesign,
    ResearchTask,
    RunEnvironment,
    RunState,
    RunStatus,
    ToolCall,
    ValidationCheck,
    ValidationRecord,
    canonical_json,
    content_hash,
    utc_now,
)

T = TypeVar("T", bound=Contract)


class StateConflict(ValueError):
    """当前状态、关联或版本不允许这项变更。"""


class RunLockedError(RuntimeError):
    """同一个 run 已有其他执行者。"""


class ArtifactIntegrityError(StateConflict):
    """文件不存在、被改动或不属于声明的产物。"""


@dataclass(frozen=True)
class RecoveryReport:
    recovered_artifact_ids: tuple[str, ...] = ()
    interrupted_attempt_ids: tuple[str, ...] = ()
    unknown_call_ids: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _parse(model: type[T], payload: str) -> T:
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise StateFormatError("对象格式版本不受支持，保留原文件并使用兼容程序")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise StateFormatError("保存的对象不满足当前 v1 契约，请保留原记录并排查") from exc


def _safe_json(value: Mapping[str, JsonValue]) -> str:
    """有效配置和短事件只允许脱敏信息；真实凭证不进入存储入口。"""
    forbidden = {"api_key", "authorization", "password", "access_token", "secret", "cookie"}

    def check(item: JsonValue) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in forbidden:
                    raise StateConflict("有效配置和事件禁止包含凭证字段")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    data = dict(value)
    check(data)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _environment_metadata(overrides: Mapping[str, JsonValue]) -> RunEnvironment:
    """只读取程序和 Git 元数据；这里不启动 Stata，也不读取模型凭证。"""
    commit: str | None = None
    dirty: bool | None = None
    try:
        root = Path(__file__).resolve().parents[2]
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if revision.returncode == 0 and status.returncode == 0:
            commit = revision.stdout.strip()
            dirty = bool(status.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass  # 安装包可不在 Git 检出目录中，未知版本明确保存为空。
    return RunEnvironment.model_validate(
        dict(overrides)
        | {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "git_commit": commit,
            "worktree_dirty": dirty,
        }
    )


class RunStore:
    """持有进程级排他锁的写入者；只读连接可并存且不执行迁移。"""

    def __init__(self, directory: Path, *, writable: bool, initialize: bool = False) -> None:
        self.directory = directory.resolve()
        self.run_id = TypeAdapter(Identifier).validate_python(self.directory.name)
        self.executor_id = new_id("executor")
        self.writable = writable
        self._closed = False
        self._lock_fd: int | None = None
        self.recovery_report = RecoveryReport()
        database = self.directory / "state.sqlite"
        if not self.directory.is_dir() or (not initialize and not database.is_file()):
            raise StateFormatError("研究目录或数据库不存在，不自动创建替代会话")
        try:
            if writable:
                self._lock_fd = os.open(self.directory / ".run.lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RunLockedError(
                        "已有执行者持有此 run，请等待其结束；只读查询仍可使用"
                    ) from exc
            mode = "rwc" if initialize else ("rw" if writable else "ro")
            self._connection = sqlite3.connect(
                database.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=1
            )
            self._connection.row_factory = sqlite3.Row
            prepare_database(self._connection, writable=writable)
            if writable:
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
            else:
                self._connection.execute("PRAGMA query_only=ON")
            if not initialize:
                row = self._one("SELECT * FROM runs")
                if row["run_id"] != self.run_id or row["analysis_backend"] != "stata":
                    raise StateFormatError("目录名称、run 身份或分析后端不一致")
                self.load_task()
                _parse(InputManifest, row["input_manifest_json"])
                _parse(RunEnvironment, row["environment_json"])
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(
        cls,
        runs_root: Path,
        task: ResearchTask,
        *,
        run_id: str | None = None,
        config: Mapping[str, JsonValue] | None = None,
        environment: Mapping[str, JsonValue] | None = None,
    ) -> Self:
        task = ResearchTask.model_validate(task)
        identifier = TypeAdapter(Identifier).validate_python(run_id or new_id("run"))
        manifest_items = []
        sources: list[tuple[Path, Literal["data", "dictionary", "material"]]] = [
            (task.data_path, "data")
        ]
        if task.dictionary_path:
            sources.append((task.dictionary_path, "dictionary"))
        sources.extend((p, "material") for p in task.material_paths)
        for source, role in sources:
            if not source.is_file():
                raise StateConflict(f"输入文件不存在：{source.name}")
            manifest_items.append(
                FileFingerprint(
                    path=str(source),
                    sha256=file_hash(source),
                    byte_size=source.stat().st_size,
                    role=role,
                )
            )
        manifest = InputManifest(files=tuple(manifest_items))
        config_json = _safe_json(config or {})
        _safe_json(environment or {})
        environment_json = canonical_json(_environment_metadata(environment or {}))
        runs_root.mkdir(parents=True, exist_ok=True)
        directory = runs_root / identifier
        directory.mkdir()  # 同名目录已存在时立即失败，不能覆盖已有研究。
        store = cls(directory, writable=True, initialize=True)
        try:
            with store._transaction():
                now = utc_now()
                store._insert(
                    "runs",
                    {
                        "run_id": identifier,
                        "analysis_backend": "stata",
                        "status": RunStatus.NEW,
                        "task_json": canonical_json(task),
                        "config_json": config_json,
                        "environment_json": environment_json,
                        "input_manifest_json": canonical_json(manifest),
                        "executor_id": store.executor_id,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                store._event("run_created", {"analysis_backend": "stata"})
            store.archive_bytes("task.json", canonical_json(task).encode(), kind="task")
            store.archive_bytes("environment.json", environment_json.encode(), kind="environment")
            store.archive_bytes(
                "input_manifest.json", canonical_json(manifest).encode(), kind="input_manifest"
            )
            return store
        except BaseException:
            store.close()
            raise

    @classmethod
    def open(cls, directory: Path, *, writable: bool = True) -> Self:
        store = cls(directory, writable=writable)
        if writable:
            try:
                store.recovery_report = store.recover()
                with store._transaction():
                    store._connection.execute(
                        "UPDATE runs SET executor_id=? WHERE run_id=?",
                        (store.executor_id, store.run_id),
                    )
            except BaseException:
                store.close()
                raise
        return store

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._closed = True

    def _require_writer(self) -> None:
        if self._closed or not self.writable:
            raise StateConflict("该连接已关闭或仅供只读查询")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._require_writer()
        if self._connection.in_transaction:
            raise StateConflict("禁止嵌套事务或把外部执行放进数据库事务")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException as exc:
            self._connection.rollback()
            if isinstance(exc, sqlite3.IntegrityError):
                raise StateConflict("关联、不可变内容或唯一约束不满足") from exc
            raise

    def _one(self, query: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
        row = cast(sqlite3.Row | None, self._connection.execute(query, params).fetchone())
        if row is None:
            raise StateConflict("指定对象不存在或不属于此 run")
        return row

    def _all(self, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return cast(list[sqlite3.Row], self._connection.execute(query, params).fetchall())

    def _insert(self, table: str, fields: Mapping[str, object]) -> None:
        # 表名和字段名由本模块固定提供，外部值一律使用参数绑定。
        columns = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        self._connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(fields.values())
        )

    def _event(self, event_type: str, payload: Mapping[str, JsonValue], **refs: str) -> None:
        sequence = self._one("SELECT COALESCE(MAX(sequence),0)+1 AS n FROM events")["n"]
        self._insert(
            "events",
            {
                "event_id": new_id("event"),
                "run_id": self.run_id,
                "sequence": sequence,
                "event_type": event_type,
                "payload_json": _safe_json({"schema_version": 1, **payload}),
                "created_at": utc_now(),
                **refs,
            },
        )

    def load_task(self) -> ResearchTask:
        return _parse(ResearchTask, self._one("SELECT task_json FROM runs")["task_json"])

    def load_input_manifest(self) -> InputManifest:
        return _parse(InputManifest, self._one("SELECT input_manifest_json FROM runs")[0])

    def artifacts(self, *, kind: str | None = None) -> tuple[Artifact, ...]:
        rows = self._all(
            "SELECT artifact_id FROM artifacts"
            + (" WHERE kind=?" if kind else "")
            + " ORDER BY rowid",
            (kind,) if kind else (),
        )
        return tuple(self._artifact(row[0]) for row in rows)

    def load_state(self) -> RunState:
        row = self._one("SELECT * FROM runs")
        return RunState(
            run_id=self.run_id,
            analysis_backend=row["analysis_backend"],
            status=row["status"],
            current_design_id=row["current_design_id"],
            research_round=row["research_round"],
            budget=self.load_task().budget,
            stop_reason=row["stop_reason"],
            completed_experiment_ids=tuple(
                r[0]
                for r in self._all(
                    "SELECT DISTINCT experiment_id FROM attempts WHERE "
                    "execution_status='SUCCEEDED' ORDER BY experiment_id"
                )
            ),
            pending_request_ids=tuple(
                r[0]
                for r in self._all(
                    "SELECT request_id FROM human_requests WHERE status='PENDING' ORDER BY "
                    "created_at"
                )
            ),
        )

    def events(self) -> list[dict[str, JsonValue]]:
        return [
            {
                "event_id": r["event_id"],
                "sequence": r["sequence"],
                "type": r["event_type"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in self._all("SELECT * FROM events ORDER BY sequence")
        ]

    def _set_run_status(self, status: RunStatus, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE runs SET status=?,stop_reason=?,updated_at=? WHERE run_id=?",
            (status, reason, utc_now(), self.run_id),
        )

    def transition_run(
        self, target: RunStatus, *, expected: RunStatus | None = None, reason: str | None = None
    ) -> RunState:
        allowed = {
            RunStatus.NEW: {RunStatus.RUNNING},
            RunStatus.RUNNING: {
                RunStatus.PAUSED,
                RunStatus.COMPLETED,
                RunStatus.STOPPED,
                RunStatus.FAILED,
            },
            RunStatus.WAITING_HUMAN: {RunStatus.PAUSED, RunStatus.STOPPED},
            RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.STOPPED},
        }
        with self._transaction():
            state = self.load_state()
            if expected is not None and state.status != expected:
                raise StateConflict("会话状态已变化，拒绝迟到的更新")
            if target not in allowed.get(state.status, set()):
                raise StateConflict("不允许此会话状态转换")
            if target in {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.STOPPED}:
                if self._all(
                    "SELECT attempt_id FROM attempts WHERE execution_status='RUNNING' OR "
                    "(execution_status IN ('INTERRUPTED','CANCELLED') AND termination_confirmed=0)"
                ):
                    raise StateConflict("旧分析尚未确认停止，不能恢复、结束或重复执行")
            if target == RunStatus.RUNNING:
                if self._all(
                    "SELECT tool_call_id FROM tool_calls WHERE status='UNKNOWN' AND "
                    "json_extract(payload_json,'$.error_category')='termination'"
                    " AND NOT EXISTS(SELECT 1 FROM attempts a WHERE "
                    "a.tool_call_id=tool_calls.tool_call_id AND a.termination_confirmed=1)"
                ):
                    raise StateConflict("遗留业务工具工作进程尚未确认停止，不能恢复执行")
                if self._file_issues():
                    raise ArtifactIntegrityError("输入或归档文件改变，必须先解决恢复问题")
                if state.pending_request_ids:
                    target = RunStatus.WAITING_HUMAN
            if target == RunStatus.COMPLETED:
                if state.pending_request_ids or not self._all(
                    "SELECT artifact_id FROM artifacts WHERE kind='report'"
                ):
                    raise StateConflict("仍有待答问题或尚未归档报告，不能记为研究完成")
                if not self.selected_results():
                    raise StateConflict("尚无已选用并通过核验的结果，不能记为完整研究完成")
                for table in ("model_calls", "tool_calls"):
                    if self._all(
                        f"SELECT operation_id FROM {table} "
                        "WHERE status IN ('PENDING','RUNNING','UNKNOWN')"
                    ):
                        raise StateConflict("仍有未终结调用，不能记为完整研究完成")
            if target in {RunStatus.PAUSED, RunStatus.FAILED}:
                for row in self._all(
                    "SELECT payload_json FROM attempts WHERE execution_status='RUNNING'"
                ):
                    attempt = _parse(Attempt, row[0])
                    self._write_attempt(attempt.model_copy(update={"cancel_requested": True}))
                    self._event("attempt_cancel_requested", {}, attempt_id=attempt.attempt_id)
            self._set_run_status(target, reason)
            self._event(
                "run_status_changed", {"from": state.status, "to": target, "reason": reason}
            )
        return self.load_state()

    def advance_round(self, *, expected_round: int) -> None:
        with self._transaction():
            state = self.load_state()
            if state.status != RunStatus.RUNNING or state.research_round != expected_round:
                raise StateConflict("研究轮次或会话状态已变化")
            if state.research_round >= state.budget.max_research_rounds:
                raise StateConflict("研究轮次预算已耗尽")
            self._connection.execute(
                "UPDATE runs SET research_round=research_round+1 WHERE run_id=?", (self.run_id,)
            )
            self._event("round_advanced", {"round": expected_round + 1})

    def _require_running_design(self, design_id: str) -> None:
        state = self.load_state()
        row = self._one(
            "SELECT confirmation FROM designs WHERE design_id=? AND run_id=?",
            (design_id, self.run_id),
        )
        if (
            state.status != RunStatus.RUNNING
            or state.current_design_id != design_id
            or row[0] != "CONFIRMED"
        ):
            raise StateConflict("只能执行当前已确认设计，待人工决定或历史设计不能解锁分析")

    def design(self, design_id: str) -> ResearchDesign:
        row = self._one(
            "SELECT * FROM designs WHERE design_id=? AND run_id=?", (design_id, self.run_id)
        )
        design = _parse(ResearchDesign, row["payload_json"])
        if content_hash(design) != row["content_sha256"] or design.run_id != self.run_id:
            raise StateFormatError("设计内容与保存的 hash 或身份不一致")
        return design

    def create_design(
        self,
        content: DesignContent,
        *,
        change_reason: str,
        based_on_result_ids: tuple[str, ...] = (),
    ) -> ResearchDesign:
        with self._transaction():
            return self._create_design_unlocked(
                content, change_reason=change_reason, based_on_result_ids=based_on_result_ids
            )

    def _create_design_unlocked(
        self,
        content: DesignContent,
        *,
        change_reason: str,
        based_on_result_ids: tuple[str, ...] = (),
        decision_request_id: str | None = None,
    ) -> ResearchDesign:
        if self.load_state().status not in {RunStatus.RUNNING, RunStatus.WAITING_HUMAN}:
            raise StateConflict("当前会话不能提出研究设计")
        for artifact_id in based_on_result_ids:
            self._artifact(artifact_id, kind="result")
        version = self._one("SELECT COALESCE(MAX(version),0)+1 AS n FROM designs")["n"]
        design = ResearchDesign(
            design_id=new_id("design"),
            run_id=self.run_id,
            version=version,
            content=content,
            change_reason=change_reason,
            based_on_result_ids=based_on_result_ids,
            decision_request_id=decision_request_id,
        )
        self._insert(
            "designs",
            {
                "design_id": design.design_id,
                "run_id": self.run_id,
                "version": version,
                "payload_json": canonical_json(design),
                "content_sha256": content_hash(design),
                "confirmation": "PROPOSED",
                "created_at": utc_now(),
            },
        )
        # 新设计使旧的待答设计确认过期；确认状态和旧证据仍保留。
        requests = self._all(
            "SELECT payload_json FROM human_requests WHERE status='PENDING' AND "
            "kind='design_confirmation'"
        )
        for row in requests:
            request = _parse(HumanRequest, row[0]).model_copy(update={"status": "CANCELLED"})
            self._write_request(request)
            self._event("human_request_superseded", {}, request_id=request.request_id)
        if requests and not self.load_state().pending_request_ids:
            self._set_run_status(RunStatus.RUNNING)
        self._event("design_proposed", {"version": version}, design_id=design.design_id)
        return design

    def request_human(
        self,
        design_id: str,
        question: str,
        *,
        kind: Literal["design_confirmation", "research_question"] = "design_confirmation",
    ) -> HumanRequest:
        with self._transaction():
            return self._request_human_unlocked(design_id, question, kind=kind)

    def _request_human_unlocked(
        self,
        design_id: str,
        question: str,
        *,
        kind: Literal["design_confirmation", "research_question"] = "design_confirmation",
    ) -> HumanRequest:
        state = self.load_state()
        if state.status != RunStatus.RUNNING or state.pending_request_ids:
            raise StateConflict("当前会话不能创建新的待答问题")
        design = self.design(design_id)
        if kind == "design_confirmation":
            latest = self._one("SELECT MAX(version) FROM designs")[0]
            if design.version != latest:
                raise StateConflict("只能请求确认最新的设计版本")
            confirmation = self._one(
                "SELECT confirmation FROM designs WHERE design_id=?", (design_id,)
            )[0]
            if confirmation != "PROPOSED":
                raise StateConflict("已决定的设计不能再次审批，修改须提出新版本")
        request = HumanRequest(
            request_id=new_id("human"),
            run_id=self.run_id,
            kind=kind,
            question=question,
            target_design_id=design_id,
            target_design_sha256=content_hash(design),
        )
        self._insert(
            "human_requests",
            {
                "request_id": request.request_id,
                "run_id": self.run_id,
                "kind": kind,
                "target_design_id": design_id,
                "target_design_sha256": request.target_design_sha256,
                "status": request.status,
                "payload_json": canonical_json(request),
                "created_at": utc_now(),
            },
        )
        self._set_run_status(RunStatus.WAITING_HUMAN)
        self._event(
            "human_requested",
            {"question": question},
            request_id=request.request_id,
            design_id=design_id,
        )
        return request

    def _write_request(self, request: HumanRequest) -> None:
        self._connection.execute(
            "UPDATE human_requests SET status=?,payload_json=? WHERE request_id=?",
            (request.status, canonical_json(request), request.request_id),
        )

    def apply_answer(self, request_id: str, *, design_sha256: str, answer: HumanAnswer) -> RunState:
        with self._transaction():
            return self._apply_answer_unlocked(
                request_id, design_sha256=design_sha256, answer=answer
            )

    def _apply_answer_unlocked(
        self, request_id: str, *, design_sha256: str, answer: HumanAnswer
    ) -> RunState:
        row = self._one(
            "SELECT * FROM human_requests WHERE request_id=? AND run_id=?",
            (request_id, self.run_id),
        )
        request = _parse(HumanRequest, row["payload_json"])
        if design_sha256 != request.target_design_sha256:
            raise StateConflict("人工答复与请求的设计 hash 不一致")
        if request.status == "APPLIED":
            if request.answer != answer:
                raise StateConflict("已应用的人工答复不能改写")
            return self.load_state()
        state = self.load_state()
        if request.status != "PENDING" or state.status not in {
            RunStatus.WAITING_HUMAN,
            RunStatus.PAUSED,
        }:
            raise StateConflict("答复已过期、撤销或不属于当前等待状态")
        design = self.design(request.target_design_id)
        if content_hash(design) != design_sha256:
            raise StateConflict("设计内容已经变化")
        if answer.decision == "pause":
            self._set_run_status(RunStatus.PAUSED, answer.explanation)
            self._event("human_wait_paused", {}, request_id=request_id)
            return self.load_state()
        if request.kind == "design_confirmation":
            if design.version != self._one("SELECT MAX(version) FROM designs")[0]:
                raise StateConflict("旧设计批准不能解锁新设计")
            if answer.decision == "accept":
                self._connection.execute(
                    "UPDATE designs SET confirmation='CONFIRMED' WHERE design_id=?",
                    (design.design_id,),
                )
                self._connection.execute(
                    "UPDATE runs SET current_design_id=? WHERE run_id=?",
                    (design.design_id, self.run_id),
                )
            elif answer.decision in {"reject", "revise"}:
                self._connection.execute(
                    "UPDATE designs SET confirmation='REJECTED' WHERE design_id=?",
                    (design.design_id,),
                )
        request = request.model_copy(update={"status": "APPLIED", "answer": answer})
        self._write_request(request)
        now = utc_now()
        self._connection.execute(
            "UPDATE human_requests SET answered_at=?,applied_at=? WHERE request_id=?",
            (now, now, request_id),
        )
        target = RunStatus.PAUSED if state.status == RunStatus.PAUSED else RunStatus.RUNNING
        if answer.decision == "stop":
            if self._all(
                "SELECT attempt_id FROM attempts WHERE termination_confirmed=0 AND "
                "execution_status!='PENDING'"
            ):
                raise StateConflict("底层任务未确认结束，不能应用停止答复")
            target = RunStatus.STOPPED
        self._set_run_status(
            target,
            answer.explanation if target in {RunStatus.PAUSED, RunStatus.STOPPED} else None,
        )
        self._event(
            "human_answer_applied",
            {"decision": answer.decision},
            request_id=request_id,
            design_id=design.design_id,
        )
        return self.load_state()

    def create_experiment(self, spec: ExperimentSpec) -> None:
        spec = ExperimentSpec.model_validate(spec)
        with self._transaction():
            if spec.run_id != self.run_id:
                raise StateConflict("分析规格不属于此 run")
            self._require_running_design(spec.design_id)
            self._artifact(spec.input_artifact_id, kind="input")
            for artifact_id in spec.based_on_result_ids:
                self._artifact(artifact_id, kind="result")
            self._insert(
                "experiments",
                {
                    "experiment_id": spec.experiment_id,
                    "run_id": self.run_id,
                    "design_id": spec.design_id,
                    "initiation": spec.initiation,
                    "input_artifact_id": spec.input_artifact_id,
                    "payload_json": canonical_json(spec),
                    "content_sha256": content_hash(spec),
                    "created_at": utc_now(),
                },
            )
            self._event(
                "experiment_created", {"purpose": spec.purpose}, experiment_id=spec.experiment_id
            )

    def human_request(self, request_id: str) -> HumanRequest:
        return _parse(
            HumanRequest,
            self._one(
                "SELECT payload_json FROM human_requests WHERE request_id=? AND run_id=?",
                (request_id, self.run_id),
            )[0],
        )

    def human_requests(self) -> tuple[HumanRequest, ...]:
        return tuple(
            _parse(HumanRequest, r[0])
            for r in self._all("SELECT payload_json FROM human_requests ORDER BY rowid")
        )

    def designs(self) -> tuple[ResearchDesign, ...]:
        return tuple(
            self.design(r[0]) for r in self._all("SELECT design_id FROM designs ORDER BY version")
        )

    def design_confirmation(self, design_id: str) -> str:
        self.design(design_id)
        return cast(
            str, self._one("SELECT confirmation FROM designs WHERE design_id=?", (design_id,))[0]
        )

    def experiments(self) -> tuple[ExperimentSpec, ...]:
        return tuple(
            self.experiment(r[0])
            for r in self._all("SELECT experiment_id FROM experiments ORDER BY rowid")
        )

    def load_config(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], json.loads(self._one("SELECT config_json FROM runs")[0]))

    def revise_answer(
        self, request_id: str, *, design_sha256: str, answer: HumanAnswer, content: DesignContent
    ) -> HumanRequest:
        """人工答复、新版本及其待确认请求在同一事务内保存；不自动批准修改。"""
        content = DesignContent.model_validate(content)
        if answer.decision != "revise":
            raise StateConflict("创建修改版本必须来自人工修改意见")
        with self._transaction():
            request = self.human_request(request_id)
            if request.target_design_sha256 != design_sha256:
                raise StateConflict("修改意见与原请求的设计 hash 不一致")
            if request.status == "APPLIED":
                versions = [d for d in self.designs() if d.decision_request_id == request_id]
                if request.answer != answer or len(versions) != 1 or versions[0].content != content:
                    raise StateConflict("已应用的修改意见不能改写或重复创建版本")
                return next(
                    r
                    for r in self.human_requests()
                    if r.target_design_id == versions[0].design_id
                    and r.kind == "design_confirmation"
                )
            if content == self.design(request.target_design_id).content:
                raise StateConflict("修改内容与原设计相同，请澄清具体变化")
            based_on = tuple(r.result_id for r in self.selected_results() if r.result_id)
            self._apply_answer_unlocked(request_id, design_sha256=design_sha256, answer=answer)
            # 从暂停收到反馈仍先回到待确认，不跳过新版本的确认。
            if self.load_state().status == RunStatus.PAUSED:
                self._set_run_status(RunStatus.RUNNING)
            design = self._create_design_unlocked(
                content,
                change_reason=answer.explanation,
                based_on_result_ids=based_on,
                decision_request_id=request_id,
            )
            return self._request_human_unlocked(
                design.design_id,
                "请确认按你的反馈生成的新设计；确认后旧版本结果仅作为历史保留，并执行当前版本分析。",
            )

    def close_interrupted_call(self, identifier: str, *, model: bool) -> None:
        """关闭本地遗留调用，保留服务端状态及用量未知，不改记成功或自动重发。"""
        with self._transaction():
            call = self.model_call(identifier) if model else self.tool_call(identifier)
            if call.status != CallStatus.UNKNOWN:
                raise StateConflict("只能核对并关闭遗留的未知调用")
            if not model:
                attempts = [
                    self.attempt(r[0])
                    for r in self._all(
                        "SELECT attempt_id FROM attempts WHERE tool_call_id=?", (identifier,)
                    )
                ]
                if any(not a.termination_confirmed for a in attempts):
                    raise StateConflict("分析工作进程未确认停止，不能关闭父调用")
            updated = call.model_copy(
                update={
                    "status": CallStatus.CANCELLED,
                    "ended_at": utc_now(),
                    "duration_seconds": None,
                    "error_category": "interrupted",
                    "error_summary": "本地遗留调用已中断关闭；服务端处理、耗时及未返回用量保持未知",
                }
            )
            self._write_call("model_calls" if model else "tool_calls", updated)
            self._event(
                "interrupted_call_closed",
                {"server_outcome": "unknown"},
                **({"model_call_id": identifier} if model else {"tool_call_id": identifier}),
            )

    def experiment(self, experiment_id: str) -> ExperimentSpec:
        row = self._one(
            "SELECT * FROM experiments WHERE experiment_id=? AND run_id=?",
            (experiment_id, self.run_id),
        )
        spec = _parse(ExperimentSpec, row["payload_json"])
        if content_hash(spec) != row["content_sha256"]:
            raise StateFormatError("分析规格与保存的 hash 不一致")
        return spec

    @staticmethod
    def _call_columns(call: ModelCall | ToolCall) -> dict[str, object]:
        names = ["run_id", "operation_id", "retry_no", "status", "started_at"]
        if isinstance(call, ModelCall):
            names.extend(
                [
                    "model_call_id",
                    "parent_tool_call_id",
                    "context_design_id",
                    "request_artifact_id",
                    "response_artifact_id",
                    "parse_status",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                ]
            )
        else:
            names.extend(
                [
                    "tool_call_id",
                    "source_model_call_id",
                    "arguments_artifact_id",
                    "response_artifact_id",
                    "arguments_status",
                ]
            )
        raw = call.model_dump(mode="json")
        return {name: raw[name] for name in names} | {"payload_json": canonical_json(call)}

    def _validate_call_refs(self, call: ModelCall | ToolCall) -> None:
        if call.run_id != self.run_id:
            raise StateConflict("调用不属于此 run")
        if isinstance(call, ModelCall):
            if call.parent_tool_call_id:
                self.tool_call(call.parent_tool_call_id)
            if call.context_design_id:
                self.design(call.context_design_id)
            for result_id in call.context_result_ids:
                self._artifact(result_id, kind="result")
            refs = [
                (call.request_artifact_id, "model_request"),
                (call.response_artifact_id, "model_response"),
            ]
        else:
            if call.source_model_call_id:
                source = self.model_call(call.source_model_call_id)
                if (
                    source.status != CallStatus.SUCCEEDED
                    or source.parse_status != ParseStatus.VALID
                ):
                    raise StateConflict("只有已成功解析的模型动作才能产生业务工具调用")
            refs = [
                (call.arguments_artifact_id, "tool_arguments"),
                (call.response_artifact_id, "tool_response"),
            ]
        for artifact_id, kind in refs:
            if artifact_id:
                artifact = self._artifact(artifact_id)
                # 分析工具可以引用同一尝试的原始返回，不额外复制业务返回文件。
                if artifact.kind != kind and not (
                    kind == "tool_response" and artifact.kind == "raw_result"
                ):
                    raise StateConflict("调用产物种类不匹配")
                if isinstance(call, ModelCall):
                    if artifact.model_call_id != call.model_call_id:
                        raise StateConflict("模型请求或响应属于其他调用")
                elif artifact.tool_call_id != call.tool_call_id:
                    raise StateConflict("工具参数或返回属于其他调用")

    def record_model_call(self, call: ModelCall) -> None:
        self._record_call("model_calls", call)

    def record_tool_call(self, call: ToolCall) -> None:
        self._record_call("tool_calls", call)

    def _record_call(self, table: str, call: ModelCall | ToolCall) -> None:
        # 新登记也重新校验，不能让 model_copy 绕过对象版本或字段范围限制。
        type(call).model_validate(call.model_dump())
        with self._transaction():
            if call.status != CallStatus.PENDING:
                raise StateConflict("请求发送前必须先登记 PENDING 调用")
            state = self.load_state()
            human_interpretation = (
                isinstance(call, ModelCall)
                and call.purpose == "解析研究者反馈"
                and state.status == RunStatus.WAITING_HUMAN
                and any(
                    self.human_request(r).target_design_id == call.context_design_id
                    for r in state.pending_request_ids
                )
            )
            if state.status != RunStatus.RUNNING and not human_interpretation:
                raise StateConflict("当前会话不能发起新的调用")
            self._validate_call_refs(call)
            budget = self.load_state().budget
            maximum = budget.max_model_calls if table == "model_calls" else budget.max_tool_calls
            if self._one(f"SELECT COUNT(*) FROM {table}")[0] >= maximum:
                raise StateConflict("调用预算已耗尽，失败和发送状态不明的请求也计入预算")
            self._insert(table, self._call_columns(call) | {"created_at": utc_now()})
            ref = (
                {"model_call_id": call.model_call_id}
                if isinstance(call, ModelCall)
                else {"tool_call_id": call.tool_call_id}
            )
            self._event(
                "call_registered",
                {"operation_id": call.operation_id, "retry_no": call.retry_no},
                **ref,
            )

    def model_call(self, identifier: str) -> ModelCall:
        return _parse(
            ModelCall,
            self._one(
                "SELECT payload_json FROM model_calls WHERE model_call_id=? AND run_id=?",
                (identifier, self.run_id),
            )[0],
        )

    def tool_call(self, identifier: str) -> ToolCall:
        return _parse(
            ToolCall,
            self._one(
                "SELECT payload_json FROM tool_calls WHERE tool_call_id=? AND run_id=?",
                (identifier, self.run_id),
            )[0],
        )

    def _write_call(self, table: str, call: ModelCall | ToolCall) -> None:
        columns = self._call_columns(call)
        key = "model_call_id" if isinstance(call, ModelCall) else "tool_call_id"
        identifier = columns.pop(key)
        assignments = ",".join(f"{name}=?" for name in columns)
        self._connection.execute(
            f"UPDATE {table} SET {assignments} WHERE {key}=?", (*columns.values(), identifier)
        )

    def update_model_call(self, call: ModelCall, *, expected: CallStatus) -> None:
        with self._transaction():
            old = self.model_call(call.model_call_id)
            self._check_call_update(old, call, expected)
            self._write_call("model_calls", call)
            self._event(
                "model_call_updated",
                {"status": call.status, "parse_status": call.parse_status},
                model_call_id=call.model_call_id,
            )

    def update_tool_call(self, call: ToolCall, *, expected: CallStatus) -> None:
        with self._transaction():
            old = self.tool_call(call.tool_call_id)
            if self._all(
                "SELECT attempt_id FROM attempts WHERE tool_call_id=?", (call.tool_call_id,)
            ) and not (old.status == call.status == CallStatus.PENDING):
                raise StateConflict("分析工具的终态由对应 attempt 原子更新")
            self._check_call_update(old, call, expected)
            self._write_call("tool_calls", call)
            self._event(
                "tool_call_updated", {"status": call.status}, tool_call_id=call.tool_call_id
            )

    def _check_call_update(
        self, old: ModelCall | ToolCall, new: ModelCall | ToolCall, expected: CallStatus
    ) -> None:
        # 从 JSON 再校验，避免 model_copy(update=...) 绕过 Pydantic 的值约束。
        type(new).model_validate(new.model_dump())
        if old.status != expected:
            raise StateConflict("调用状态已变化，拒绝迟到更新")
        fields = ["run_id", "operation_id", "retry_no"]
        fields.extend(
            [
                "model_call_id",
                "purpose",
                "provider",
                "requested_model",
                "prompt_version",
                "config_version",
                "parent_tool_call_id",
                "context_design_id",
                "context_result_ids",
                "interaction_language",
            ]
            if isinstance(old, ModelCall)
            else ["tool_call_id", "tool_name", "source_model_call_id", "provider_tool_call_id"]
        )
        if type(old) is not type(new) or any(getattr(old, f) != getattr(new, f) for f in fields):
            raise StateConflict("调用身份和来源不可改写")
        allowed = {
            CallStatus.PENDING: {
                CallStatus.RUNNING,
                CallStatus.FAILED,
                CallStatus.CANCELLED,
                CallStatus.UNKNOWN,
            },
            CallStatus.RUNNING: {
                CallStatus.SUCCEEDED,
                CallStatus.FAILED,
                CallStatus.CANCELLED,
                CallStatus.UNKNOWN,
            },
        }
        changed = {
            key for key, value in new.model_dump().items() if value != old.model_dump().get(key)
        }
        same_pending = old.status == new.status == CallStatus.PENDING and changed <= (
            {"request_artifact_id"}
            if isinstance(old, ModelCall)
            else {"arguments_artifact_id", "arguments_status"}
        )
        parse_only = (
            isinstance(old, ModelCall)
            and isinstance(new, ModelCall)
            and old.status == new.status == CallStatus.SUCCEEDED
            and old.parse_status == ParseStatus.PENDING
            and new.parse_status in {ParseStatus.VALID, ParseStatus.INVALID}
            and changed <= {"parse_status", "error_category", "error_summary"}
        )
        if (
            new != old
            and not same_pending
            and not parse_only
            and new.status not in allowed.get(old.status, set())
        ):
            raise StateConflict("调用终态不可回退；重试必须登记新的请求记录")
        if old.started_at is not None and new.started_at != old.started_at:
            raise StateConflict("调用开始时间不可改写")
        for name in ("request_artifact_id", "arguments_artifact_id", "response_artifact_id"):
            previous = getattr(old, name, None)
            if previous is not None and getattr(new, name, None) != previous:
                raise StateConflict("调用已登记的产物引用不可改写")
        self._validate_call_refs(new)

    def model_calls(self) -> tuple[ModelCall, ...]:
        """按登记顺序读取逐次请求，供上下文、预算和审计使用。"""
        return tuple(
            _parse(ModelCall, row[0])
            for row in self._all("SELECT payload_json FROM model_calls ORDER BY rowid")
        )

    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(
            _parse(ToolCall, row[0])
            for row in self._all("SELECT payload_json FROM tool_calls ORDER BY rowid")
        )

    def call_summary(self) -> dict[str, JsonValue]:
        models = [
            _parse(ModelCall, r[0]) for r in self._all("SELECT payload_json FROM model_calls")
        ]
        tools = [_parse(ToolCall, r[0]) for r in self._all("SELECT payload_json FROM tool_calls")]
        known = [m.total_tokens for m in models if m.total_tokens is not None]
        return {
            "schema_version": 1,
            "model_calls": len(models),
            "tool_calls": len(tools),
            "model_transport_failures": sum(m.status == CallStatus.FAILED for m in models),
            "model_parse_failures": sum(m.parse_status == ParseStatus.INVALID for m in models),
            "tool_failures": sum(t.status == CallStatus.FAILED for t in tools),
            "model_retries": sum(m.retry_no > 0 for m in models),
            "tool_retries": sum(t.retry_no > 0 for t in tools),
            "unfinished_model_calls": sum(
                m.status in {CallStatus.PENDING, CallStatus.RUNNING, CallStatus.UNKNOWN}
                for m in models
            ),
            "unfinished_tool_calls": sum(
                t.status in {CallStatus.PENDING, CallStatus.RUNNING, CallStatus.UNKNOWN}
                for t in tools
            ),
            "known_total_tokens": sum(known) if known else None,
            "missing_total_usage_calls": sum(m.total_tokens is None for m in models),
        }

    def attempts(self, experiment_id: str) -> tuple[Attempt, ...]:
        self.experiment(experiment_id)
        return tuple(
            _parse(Attempt, row[0])
            for row in self._all(
                "SELECT payload_json FROM attempts WHERE experiment_id=? ORDER BY attempt_no",
                (experiment_id,),
            )
        )

    def attempt(self, attempt_id: str) -> Attempt:
        return _parse(
            Attempt,
            self._one(
                "SELECT payload_json FROM attempts WHERE attempt_id=? AND run_id=?",
                (attempt_id, self.run_id),
            )[0],
        )

    @staticmethod
    def _attempt_columns(attempt: Attempt) -> dict[str, object]:
        fields = [
            "attempt_id",
            "run_id",
            "experiment_id",
            "attempt_no",
            "tool_call_id",
            "analysis_backend",
            "execution_status",
            "extraction_status",
            "cancel_requested",
            "termination_confirmed",
            "input_artifact_id",
            "code_artifact_id",
            "raw_artifact_id",
            "result_artifact_id",
            "validation_artifact_id",
        ]
        raw = attempt.model_dump(mode="json")
        return {f: raw[f] for f in fields} | {"payload_json": canonical_json(attempt)}

    def _write_attempt(self, attempt: Attempt) -> None:
        Attempt.model_validate(attempt.model_dump())
        columns = self._attempt_columns(attempt)
        identifier = columns.pop("attempt_id")
        assignments = ",".join(f"{name}=?" for name in columns)
        self._connection.execute(
            f"UPDATE attempts SET {assignments} WHERE attempt_id=?", (*columns.values(), identifier)
        )

    def create_attempt(
        self, experiment_id: str, tool_call_id: str, *, rerun_reason: str | None = None
    ) -> Attempt:
        with self._transaction():
            spec = self.experiment(experiment_id)
            self._require_running_design(spec.design_id)
            call = self.tool_call(tool_call_id)
            if (
                call.status != CallStatus.PENDING
                or call.tool_name != "run_analysis"
                or call.arguments_status != "pass"
            ):
                raise StateConflict("新的分析尝试需要已校验且尚未执行的 run_analysis 调用")
            previous = self._all(
                "SELECT payload_json FROM attempts WHERE experiment_id=? ORDER BY attempt_no",
                (experiment_id,),
            )
            for row in previous:
                old = _parse(Attempt, row[0])
                if not old.termination_confirmed:
                    raise StateConflict("旧尝试尚未确认结束，不能创建重跑")
                if old.execution_status == ExecutionStatus.SUCCEEDED and not rerun_reason:
                    raise StateConflict("成功尝试保留；确需重新统计执行时必须说明理由")
            attempt = Attempt(
                attempt_id=new_id("attempt"),
                run_id=self.run_id,
                experiment_id=experiment_id,
                attempt_no=len(previous) + 1,
                tool_call_id=tool_call_id,
                input_artifact_id=spec.input_artifact_id,
            )
            self._insert("attempts", self._attempt_columns(attempt) | {"created_at": utc_now()})
            self._event(
                "attempt_registered",
                {"attempt_no": attempt.attempt_no, "rerun_reason": rerun_reason},
                attempt_id=attempt.attempt_id,
                tool_call_id=tool_call_id,
            )
        return attempt

    def start_attempt(
        self,
        attempt_id: str,
        code_artifact_id: str,
        *,
        service_instance_id: str,
        session_id: str,
        backend_task_id: str | None = None,
    ) -> Attempt:
        with self._transaction():
            attempt = self.attempt(attempt_id)
            spec = self.experiment(attempt.experiment_id)
            self._require_running_design(spec.design_id)
            if attempt.execution_status != ExecutionStatus.PENDING or attempt.cancel_requested:
                raise StateConflict("尝试已启动或请求取消")
            code = self._artifact(code_artifact_id, kind="code")
            if code.attempt_id != attempt_id or code.tool_call_id != attempt.tool_call_id:
                raise StateConflict("代码必须属于此尝试及其工具调用")
            self._artifact(attempt.input_artifact_id, kind="input")
            attempt = Attempt.model_validate(
                attempt.model_dump()
                | {
                    "execution_status": ExecutionStatus.RUNNING,
                    "code_artifact_id": code_artifact_id,
                    "executor_id": self.executor_id,
                    "service_instance_id": service_instance_id,
                    "session_id": session_id,
                    "backend_task_id": backend_task_id,
                }
            )
            self._write_attempt(attempt)
            call = self.tool_call(attempt.tool_call_id)
            if call.status != CallStatus.PENDING:
                raise StateConflict("对应工具调用已开始或结束")
            call = call.model_copy(update={"status": CallStatus.RUNNING, "started_at": utc_now()})
            self._write_call("tool_calls", call)
            self._event(
                "attempt_started", {}, attempt_id=attempt_id, tool_call_id=attempt.tool_call_id
            )
        return attempt

    def request_cancel(self, attempt_id: str) -> None:
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if attempt.execution_status not in {
                ExecutionStatus.PENDING,
                ExecutionStatus.RUNNING,
                ExecutionStatus.INTERRUPTED,
            }:
                raise StateConflict("已结束的尝试不能重新请求取消")
            if not attempt.cancel_requested:
                updates: dict[str, object] = {"cancel_requested": True}
                if attempt.execution_status == ExecutionStatus.PENDING:
                    # 当前执行者尚未启动的尝试可直接取消；遗留任务仍须核对底层状态。
                    updates.update(
                        execution_status=ExecutionStatus.CANCELLED, termination_confirmed=True
                    )
                    call = self.tool_call(attempt.tool_call_id)
                    self._write_call(
                        "tool_calls",
                        call.model_copy(
                            update={"status": CallStatus.CANCELLED, "ended_at": utc_now()}
                        ),
                    )
                self._write_attempt(attempt.model_copy(update=updates))
                self._event("attempt_cancel_requested", {}, attempt_id=attempt_id)

    def finish_attempt(
        self,
        attempt_id: str,
        status: ExecutionStatus,
        *,
        executor_id: str,
        termination_confirmed: bool,
        duration_seconds: float,
        raw_artifact_id: str | None = None,
        error_summary: str | None = None,
        error_category: str | None = None,
    ) -> None:
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if (
                attempt.execution_status != ExecutionStatus.RUNNING
                or attempt.executor_id != executor_id
                or executor_id != self.executor_id
            ):
                raise StateConflict("尝试状态或执行者不匹配，迟到输出只能单独归档")
            if not termination_confirmed or status not in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
                raise StateConflict("必须确认底层执行结束后才能写入终态")
            if attempt.cancel_requested and status == ExecutionStatus.SUCCEEDED:
                raise StateConflict("取消后的迟到结果不能标记为成功")
            if status == ExecutionStatus.SUCCEEDED and raw_artifact_id is None:
                raise StateConflict("执行成功必须归档真实原始返回")
            if raw_artifact_id:
                raw = self._artifact(raw_artifact_id, kind="raw_result")
                if raw.attempt_id != attempt_id or raw.tool_call_id != attempt.tool_call_id:
                    raise StateConflict("原始返回必须来自此尝试及其工具调用")
            updated = attempt.model_copy(
                update={
                    "execution_status": status,
                    "termination_confirmed": True,
                    "raw_artifact_id": raw_artifact_id,
                    "error_summary": error_summary,
                }
            )
            self._write_attempt(updated)
            call = self.tool_call(attempt.tool_call_id)
            call_status = {
                ExecutionStatus.SUCCEEDED: CallStatus.SUCCEEDED,
                ExecutionStatus.FAILED: CallStatus.FAILED,
                ExecutionStatus.CANCELLED: CallStatus.CANCELLED,
            }[status]
            call = ToolCall.model_validate(
                call.model_dump()
                | {
                    "status": call_status,
                    "ended_at": utc_now(),
                    "duration_seconds": duration_seconds,
                    "response_artifact_id": raw_artifact_id,
                    "error_summary": error_summary,
                    "error_category": error_category,
                }
            )
            self._write_call("tool_calls", call)
            self._event(
                "attempt_finished",
                {"status": status},
                attempt_id=attempt_id,
                tool_call_id=attempt.tool_call_id,
            )

    def bind_backend_task(self, attempt_id: str, backend_task_id: str) -> None:
        """收到后台任务 ID 后立即持久化，禁止重连后用新实例冒认旧任务。"""
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if (
                attempt.execution_status != ExecutionStatus.RUNNING
                or attempt.executor_id != self.executor_id
            ):
                raise StateConflict("任务绑定需要当前执行者持有的运行中尝试")
            if attempt.backend_task_id is not None and attempt.backend_task_id != backend_task_id:
                raise StateConflict("后台任务身份不可改写")
            updated = Attempt.model_validate(
                attempt.model_dump() | {"backend_task_id": backend_task_id}
            )
            self._write_attempt(updated)
            self._event("backend_task_bound", {}, attempt_id=attempt_id)

    def interrupt_attempt(self, attempt_id: str, *, reason: str) -> None:
        """终止未确认时保留旧服务身份、取消标记及未知调用，并暂停会话。"""
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if (
                attempt.execution_status != ExecutionStatus.RUNNING
                or attempt.executor_id != self.executor_id
            ):
                raise StateConflict("只有当前执行者可以登记未确认结束的尝试")
            self._write_attempt(
                attempt.model_copy(
                    update={
                        "execution_status": ExecutionStatus.INTERRUPTED,
                        "cancel_requested": True,
                        "termination_confirmed": False,
                        "error_summary": reason,
                    }
                )
            )
            call = self.tool_call(attempt.tool_call_id)
            self._write_call(
                "tool_calls",
                call.model_copy(
                    update={
                        "status": CallStatus.UNKNOWN,
                        "error_category": "termination",
                        "error_summary": reason,
                    }
                ),
            )
            if self.load_state().status not in {
                RunStatus.STOPPED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }:
                self._set_run_status(RunStatus.PAUSED, reason)
            self._event("attempt_termination_unknown", {}, attempt_id=attempt_id)

    def mark_extraction(self, attempt_id: str, status: ExtractionStatus, *, reason: str) -> None:
        """提取失败不改写已经完成的统计执行，也不沿用旧核验。"""
        if status not in {ExtractionStatus.FAILED, ExtractionStatus.PENDING}:
            raise StateConflict("成功提取必须通过 save_result 保存实际结果")
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if attempt.execution_status != ExecutionStatus.SUCCEEDED:
                raise StateConflict("只有成功执行可以登记独立的提取状态")
            self._write_attempt(
                attempt.model_copy(
                    update={
                        "extraction_status": status,
                        "error_summary": reason,
                        "result_artifact_id": None,
                        "validation_artifact_id": None,
                    }
                )
            )
            self._connection.execute(
                "UPDATE experiments SET selected_result_artifact_id=NULL WHERE experiment_id=?",
                (attempt.experiment_id,),
            )
            self._event(
                "extraction_needs_attention",
                {"status": status, "reason": reason},
                attempt_id=attempt_id,
            )

    def confirm_interrupted_stopped(self, attempt_id: str) -> None:
        """调用者须先确认旧服务任务已结束；本方法只记录这项明确的确认。"""
        with self._transaction():
            attempt = self.attempt(attempt_id)
            if attempt.execution_status != ExecutionStatus.INTERRUPTED:
                raise StateConflict("只能确认遗留中断尝试的停止状态")
            self._write_attempt(attempt.model_copy(update={"termination_confirmed": True}))
            self._event("interrupted_task_stop_confirmed", {}, attempt_id=attempt_id)

    def confirm_query_stopped(self, tool_call_id: str) -> None:
        """调用者须先取得旧工作进程退出证据；不改写未知调用为成功。"""
        with self._transaction():
            call = self.tool_call(tool_call_id)
            if call.status != CallStatus.UNKNOWN or call.error_category != "termination":
                raise StateConflict("只能确认因工作进程终止不明而暂停的业务查询")
            if self._all("SELECT attempt_id FROM attempts WHERE tool_call_id=?", (tool_call_id,)):
                raise StateConflict("分析尝试须通过其专用停止确认入口处理")
            self._write_call(
                "tool_calls", call.model_copy(update={"error_category": "termination_confirmed"})
            )
            self._event("query_worker_stop_confirmed", {}, tool_call_id=tool_call_id)

    def _artifact_path(self, relative_path: str) -> Path:
        target = self.directory / relative_path
        if target.resolve().is_relative_to(self.directory) is False:
            raise ArtifactIntegrityError("产物路径超出 run 目录")
        for path in (target, *target.parents):
            if path == self.directory:
                break
            if path.is_symlink():
                raise ArtifactIntegrityError("归档路径不能经过符号链接")
        return target

    def _validate_artifact_refs(self, artifact: Artifact) -> None:
        if artifact.run_id != self.run_id:
            raise StateConflict("产物不属于此 run")
        if artifact.design_id:
            self.design(artifact.design_id)
        if artifact.model_call_id:
            self.model_call(artifact.model_call_id)
        if artifact.tool_call_id:
            self.tool_call(artifact.tool_call_id)
        if artifact.attempt_id:
            attempt = self.attempt(artifact.attempt_id)
            spec = self.experiment(attempt.experiment_id)
            if (
                artifact.tool_call_id != attempt.tool_call_id
                or artifact.design_id != spec.design_id
            ):
                raise StateConflict("尝试产物的设计和调用来源必须完全匹配")
        if (
            artifact.kind in {"code", "raw_result", "result", "validation"}
            and artifact.attempt_id is None
        ):
            raise StateConflict("代码、结果和核验必须绑定具体执行尝试")

    def _insert_artifact(self, artifact: Artifact) -> None:
        self._validate_artifact_refs(artifact)
        fields = artifact.model_dump(mode="json")
        fields.pop("schema_version")
        fields["payload_json"] = canonical_json(artifact)
        self._insert("artifacts", fields)
        self._event("artifact_archived", {"kind": artifact.kind}, artifact_id=artifact.artifact_id)

    def _artifact(self, identifier: str, *, kind: str | None = None) -> Artifact:
        row = self._one(
            "SELECT * FROM artifacts WHERE artifact_id=? AND run_id=?", (identifier, self.run_id)
        )
        artifact = _parse(Artifact, row["payload_json"])
        if (
            artifact.artifact_id != identifier
            or artifact.run_id != self.run_id
            or any(
                getattr(artifact, key) != row[key]
                for key in ("sha256", "relative_path", "byte_size", "kind")
            )
        ):
            raise StateFormatError("产物索引与内容身份不一致")
        if kind is not None and artifact.kind != kind:
            raise StateConflict("产物种类与用途不一致")
        target = self._artifact_path(artifact.relative_path)
        if (
            not target.is_file()
            or target.stat().st_size != artifact.byte_size
            or file_hash(target) != artifact.sha256
        ):
            raise ArtifactIntegrityError(f"产物缺失或被改动：{artifact.relative_path}")
        return artifact

    def artifact(self, identifier: str) -> Artifact:
        return self._artifact(identifier)

    def read_artifact(self, identifier: str) -> bytes:
        artifact = self._artifact(identifier)
        data = self._artifact_path(artifact.relative_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ArtifactIntegrityError("读取期间产物发生变化")
        return data

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _prepare_artifact(
        self,
        relative_path: str,
        content: bytes,
        *,
        kind: str,
        artifact_id: str | None = None,
        design_id: str | None = None,
        attempt_id: str | None = None,
        model_call_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> tuple[Artifact, Path]:
        self._require_writer()
        if self._connection.in_transaction:
            raise StateConflict("文件归档必须在短数据库事务之外准备")
        artifact = Artifact.model_validate(
            {
                "artifact_id": artifact_id or new_id("artifact"),
                "run_id": self.run_id,
                "kind": kind,
                "relative_path": relative_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
                "design_id": design_id,
                "attempt_id": attempt_id,
                "model_call_id": model_call_id,
                "tool_call_id": tool_call_id,
            }
        )
        self._validate_artifact_refs(artifact)
        target = self._artifact_path(artifact.relative_path)
        if target.exists() or self._all(
            "SELECT artifact_id FROM artifacts WHERE artifact_id=? OR relative_path=?",
            (artifact.artifact_id, artifact.relative_path),
        ):
            raise StateConflict("产物 ID 或路径已存在，归档不能覆盖原文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = self.directory / ".archive_pending"
        pending.mkdir(exist_ok=True)
        if pending.is_symlink():
            raise ArtifactIntegrityError("归档恢复目录不能使用符号链接")
        journal = pending / f"{artifact.artifact_id}.json"
        with journal.open("xb") as stream:
            stream.write(canonical_json(artifact).encode())
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory(pending)
        temporary = target.parent / f".{artifact.artifact_id}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # 同目录的硬链接发布具有原子可见性，并在目标存在时失败，绝不覆盖。
            os.link(temporary, target)
            temporary.unlink()
            self._sync_directory(target.parent)
        except BaseException:
            # 保留恢复意图和现有文件，恢复流程只登记匹配的证据，不推定执行成功。
            raise
        return artifact, journal

    def archive_bytes(
        self,
        relative_path: str,
        content: bytes,
        *,
        kind: str,
        design_id: str | None = None,
        attempt_id: str | None = None,
        model_call_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> Artifact:
        artifact, journal = self._prepare_artifact(
            relative_path,
            content,
            kind=kind,
            design_id=design_id,
            attempt_id=attempt_id,
            model_call_id=model_call_id,
            tool_call_id=tool_call_id,
        )
        with self._transaction():
            self._insert_artifact(artifact)
        journal.unlink()
        self._sync_directory(journal.parent)
        return artifact

    def save_result(self, attempt_id: str, normalized: NormalizedResult) -> Artifact:
        attempt = self.attempt(attempt_id)
        if attempt.execution_status != ExecutionStatus.SUCCEEDED or attempt.cancel_requested:
            raise StateConflict("只有未取消的成功执行才能产生提取结果")
        if attempt.code_artifact_id is None or attempt.raw_artifact_id is None:
            raise StateConflict("缺少已归档代码或原始返回")
        spec = self.experiment(attempt.experiment_id)
        if normalized.kind != spec.expected_result_type:
            raise StateConflict("提取结果形态不符合分析规格")
        input_artifact = self._artifact(attempt.input_artifact_id, kind="input")
        code = self._artifact(attempt.code_artifact_id, kind="code")
        self._artifact(attempt.raw_artifact_id, kind="raw_result")
        result_id = new_id("result")
        result = ExperimentResult(
            experiment_id=attempt.experiment_id,
            attempt_id=attempt_id,
            design_id=spec.design_id,
            result_id=result_id,
            execution_status=attempt.execution_status,
            raw_artifact_ids=(attempt.raw_artifact_id,),
            normalized=normalized,
            extraction_status=ExtractionStatus.SUCCEEDED,
            input_sha256=input_artifact.sha256,
            code_sha256=code.sha256,
        )
        path = f"experiments/{attempt.experiment_id}/{attempt_id}/results/{result_id}.json"
        artifact, journal = self._prepare_artifact(
            path,
            canonical_json(result).encode(),
            kind="result",
            artifact_id=result_id,
            design_id=spec.design_id,
            attempt_id=attempt_id,
            tool_call_id=attempt.tool_call_id,
        )
        with self._transaction():
            self._insert_artifact(artifact)
            self._write_attempt(
                attempt.model_copy(
                    update={
                        "result_artifact_id": result_id,
                        "validation_artifact_id": None,
                        "extraction_status": ExtractionStatus.SUCCEEDED,
                    }
                )
            )
            # 重新提取使用新版本，旧核验和旧选择不能自动套到新数值上。
            self._connection.execute(
                "UPDATE experiments SET selected_result_artifact_id=NULL WHERE experiment_id=?",
                (attempt.experiment_id,),
            )
            self._event("result_extracted", {}, artifact_id=result_id, attempt_id=attempt_id)
        journal.unlink()
        return artifact

    def result(self, result_id: str) -> ExperimentResult:
        artifact = self._artifact(result_id, kind="result")
        result = _parse(ExperimentResult, self.read_artifact(result_id).decode())
        if (
            result.result_id != artifact.artifact_id
            or result.attempt_id != artifact.attempt_id
            or result.design_id != artifact.design_id
        ):
            raise ArtifactIntegrityError("规范化结果的身份与产物来源不一致")
        return result

    def save_validation(
        self,
        result_id: str,
        checks: tuple[ValidationCheck, ...],
        *,
        rules_version: str = "1",
        method_blocking: bool = False,
    ) -> Artifact:
        result_artifact = self._artifact(result_id, kind="result")
        result = self.result(result_id)
        attempt = self.attempt(result.attempt_id)
        if attempt.result_artifact_id != result_id:
            raise StateConflict("核验必须绑定此尝试当前提取的结果版本")
        for check in checks:
            for identifier in check.evidence_artifact_ids:
                self._artifact(identifier)
        design = self.design(result.design_id)
        validation = ValidationRecord(
            result_artifact_id=result_id,
            result_sha256=result_artifact.sha256,
            design_id=design.design_id,
            design_sha256=content_hash(design),
            rules_version=rules_version,
            checks=checks,
            method_blocking=method_blocking,
        )
        identifier = new_id("validation")
        path = (
            f"experiments/{attempt.experiment_id}/{attempt.attempt_id}"
            f"/validations/{identifier}.json"
        )
        artifact, journal = self._prepare_artifact(
            path,
            canonical_json(validation).encode(),
            kind="validation",
            artifact_id=identifier,
            design_id=design.design_id,
            attempt_id=attempt.attempt_id,
            tool_call_id=attempt.tool_call_id,
        )
        with self._transaction():
            self._insert_artifact(artifact)
            self._write_attempt(
                attempt.model_copy(update={"validation_artifact_id": artifact.artifact_id})
            )
            self._event(
                "result_validated",
                {"eligible_checks": validation.eligible()},
                artifact_id=artifact.artifact_id,
                attempt_id=attempt.attempt_id,
            )
        journal.unlink()
        return artifact

    def _eligible_result(self, result_id: str) -> ExperimentResult:
        artifact = self._artifact(result_id, kind="result")
        result = self.result(result_id)
        attempt = self.attempt(result.attempt_id)
        spec = self.experiment(attempt.experiment_id)
        state = self.load_state()
        if result.experiment_id != spec.experiment_id or result.design_id != spec.design_id:
            raise StateConflict("结果不属于声明的分析规格")
        if state.current_design_id != spec.design_id:
            raise StateConflict("历史设计的结果不能作为当前研究结果")
        if (
            self._one("SELECT confirmation FROM designs WHERE design_id=?", (spec.design_id,))[0]
            != "CONFIRMED"
        ):
            raise StateConflict("结果对应的设计尚未确认")
        if (
            attempt.cancel_requested
            or attempt.execution_status != ExecutionStatus.SUCCEEDED
            or not attempt.termination_confirmed
        ):
            raise StateConflict("执行失败、取消或尚未结束的结果不能选用")
        if (
            attempt.result_artifact_id != result_id
            or attempt.extraction_status != ExtractionStatus.SUCCEEDED
            or result.execution_status != ExecutionStatus.SUCCEEDED
            or result.extraction_status != ExtractionStatus.SUCCEEDED
            or result.normalized is None
            or not result.raw_artifact_ids
            or attempt.raw_artifact_id not in result.raw_artifact_ids
        ):
            raise StateConflict("结果未提取成功或已被新的提取版本替代")
        if attempt.code_artifact_id is None or attempt.validation_artifact_id is None:
            raise StateConflict("结果缺少代码或逐项核验")
        code = self._artifact(attempt.code_artifact_id, kind="code")
        input_artifact = self._artifact(attempt.input_artifact_id, kind="input")
        if result.code_sha256 != code.sha256 or result.input_sha256 != input_artifact.sha256:
            raise ArtifactIntegrityError("结果与实际代码、输入的 hash 不对应")
        for raw_id in result.raw_artifact_ids:
            raw = self._artifact(raw_id, kind="raw_result")
            if raw.attempt_id != attempt.attempt_id:
                raise StateConflict("原始输出来自其他尝试")
        validation_artifact = self._artifact(attempt.validation_artifact_id, kind="validation")
        validation = _parse(
            ValidationRecord, self.read_artifact(validation_artifact.artifact_id).decode()
        )
        if (
            validation_artifact.attempt_id != attempt.attempt_id
            or validation.result_artifact_id != result_id
            or validation.result_sha256 != artifact.sha256
            or validation.design_id != spec.design_id
            or validation.design_sha256 != content_hash(self.design(spec.design_id))
        ):
            raise StateConflict("核验与确切结果、设计或内容 hash 不匹配")
        if not validation.eligible():
            raise StateConflict("必需检查尚未通过或方法存在阻断问题")
        for check in validation.checks:
            for identifier in check.evidence_artifact_ids:
                self._artifact(identifier)
        if self._input_issues():
            raise ArtifactIntegrityError("原始输入已改变，当前结果需暂停并重新确认输入版本")
        return result

    def select_result(self, experiment_id: str, result_id: str) -> None:
        with self._transaction():
            result = self._eligible_result(result_id)
            if result.experiment_id != experiment_id:
                raise StateConflict("不能选择另一个分析的结果")
            self._connection.execute(
                "UPDATE experiments SET selected_result_artifact_id=? WHERE experiment_id=?",
                (result_id, experiment_id),
            )
            self._event("result_selected", {}, experiment_id=experiment_id, artifact_id=result_id)

    def selected_results(self) -> tuple[ExperimentResult, ...]:
        # 报告与导出复用同一检查入口，每次重新核验文件及当前设计。
        rows = self._all(
            "SELECT selected_result_artifact_id FROM experiments WHERE design_id=? "
            "AND selected_result_artifact_id IS NOT NULL ORDER BY created_at",
            (self.load_state().current_design_id,),
        )
        return tuple(self._eligible_result(row[0]) for row in rows)

    def _input_issues(self) -> list[str]:
        manifest = _parse(InputManifest, self._one("SELECT input_manifest_json FROM runs")[0])
        issues = []
        for item in manifest.files:
            source = Path(item.path)
            if (
                not source.is_file()
                or source.stat().st_size != item.byte_size
                or file_hash(source) != item.sha256
            ):
                issues.append(f"输入缺失或改变：{source.name}")
        return issues

    def _file_issues(self) -> list[str]:
        issues = self._input_issues()
        for row in self._all("SELECT artifact_id FROM artifacts"):
            try:
                self._artifact(row[0])
            except (ArtifactIntegrityError, StateFormatError) as exc:
                issues.append(str(exc))
        return issues

    def recover(self) -> RecoveryReport:
        self._require_writer()
        recovered: list[str] = []
        issues: list[str] = []
        pending = self.directory / ".archive_pending"
        if pending.is_symlink():
            raise ArtifactIntegrityError("恢复意图目录不能使用符号链接")
        for journal in sorted(pending.glob("*.json")):
            try:
                artifact = _parse(Artifact, journal.read_text())
                if journal.stem != artifact.artifact_id:
                    raise ArtifactIntegrityError("恢复意图文件名与产物 ID 不一致")
                self._validate_artifact_refs(artifact)
                target = self._artifact_path(artifact.relative_path)
                if (
                    not target.is_file()
                    or target.stat().st_size != artifact.byte_size
                    or file_hash(target) != artifact.sha256
                ):
                    raise ArtifactIntegrityError("恢复意图对应的文件尚未完成或 hash 不匹配")
                rows = self._all(
                    "SELECT payload_json FROM artifacts WHERE artifact_id=?",
                    (artifact.artifact_id,),
                )
                if rows:
                    if _parse(Artifact, rows[0][0]) != artifact:
                        raise ArtifactIntegrityError("恢复意图与数据库产物身份不一致")
                else:
                    with self._transaction():
                        self._insert_artifact(artifact)
                        self._event(
                            "artifact_index_recovered", {}, artifact_id=artifact.artifact_id
                        )
                    recovered.append(artifact.artifact_id)
                journal.unlink()
            except (ValueError, OSError, StateFormatError) as exc:
                issues.append(f"待处理归档 {journal.name}：{type(exc).__name__}")
        issues.extend(self._file_issues())
        indexed = {r[0] for r in self._all("SELECT relative_path FROM artifacts")}
        ignored = {"state.sqlite", "state.sqlite-wal", "state.sqlite-shm", ".run.lock"}
        for path in self.directory.rglob("*"):
            relative = path.relative_to(self.directory)
            if relative.parts[0] == ".archive_pending" or relative.as_posix() in ignored:
                continue
            if (path.is_file() or path.is_symlink()) and relative.as_posix() not in indexed:
                issues.append(f"未登记文件，不能推定执行成功：{relative.as_posix()}")
        interrupted: list[str] = []
        unknown: list[str] = []
        with self._transaction():
            for row in self._all(
                "SELECT payload_json FROM attempts WHERE execution_status IN ('PENDING','RUNNING')"
            ):
                attempt = _parse(Attempt, row[0])
                self._write_attempt(
                    attempt.model_copy(
                        update={
                            "execution_status": ExecutionStatus.INTERRUPTED,
                            "termination_confirmed": False,
                        }
                    )
                )
                interrupted.append(attempt.attempt_id)
                self._event("attempt_interrupted_on_recovery", {}, attempt_id=attempt.attempt_id)
            for table, model, key in (
                ("model_calls", ModelCall, "model_call_id"),
                ("tool_calls", ToolCall, "tool_call_id"),
            ):
                for row in self._all(
                    f"SELECT payload_json FROM {table} WHERE status IN ('PENDING','RUNNING')"
                ):
                    call: ModelCall | ToolCall = (
                        _parse(ModelCall, row[0])
                        if model is ModelCall
                        else _parse(ToolCall, row[0])
                    )
                    self._write_call(table, call.model_copy(update={"status": CallStatus.UNKNOWN}))
                    identifier = cast(str, getattr(call, key))
                    unknown.append(identifier)
                    self._event("call_unknown_on_recovery", {}, **{key: identifier})
            if issues or interrupted or unknown:
                state = self.load_state()
                if state.status not in {RunStatus.COMPLETED, RunStatus.STOPPED, RunStatus.FAILED}:
                    self._set_run_status(
                        RunStatus.PAUSED, "恢复发现待处理文件或发送、执行状态不明的任务"
                    )
                self._event(
                    "recovery_needs_attention",
                    {
                        "issues": list[JsonValue](issues),
                        "interrupted_attempts": list[JsonValue](interrupted),
                        "unknown_calls": list[JsonValue](unknown),
                    },
                )
        return RecoveryReport(tuple(recovered), tuple(interrupted), tuple(unknown), tuple(issues))
