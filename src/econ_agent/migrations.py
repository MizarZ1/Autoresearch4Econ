"""SQLite v1 建表入口；只为真实存在的格式升级保留迁移边界。"""

import sqlite3

DATABASE_VERSION = 1
TABLES = frozenset(
    {
        "runs",
        "designs",
        "experiments",
        "attempts",
        "artifacts",
        "human_requests",
        "events",
        "model_calls",
        "tool_calls",
    }
)


class StateFormatError(ValueError):
    """数据库格式不受当前程序支持。"""


class FutureVersionError(StateFormatError):
    """拒绝用旧程序读写未来格式。"""


DDL = """

CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, singleton INTEGER NOT NULL UNIQUE DEFAULT 1 CHECK(singleton=1),
 analysis_backend TEXT NOT NULL CHECK(analysis_backend='stata'),
 status TEXT NOT NULL CHECK(status IN ('NEW', 'RUNNING', 'WAITING_HUMAN', 'PAUSED', 'COMPLETED',
 'STOPPED', 'FAILED')),
 current_design_id TEXT, task_json TEXT NOT NULL CHECK(json_valid(task_json)),
 config_json TEXT NOT NULL CHECK(json_valid(config_json)),
 environment_json TEXT NOT NULL CHECK(json_valid(environment_json)),
 input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json)),
 research_round INTEGER NOT NULL DEFAULT 0 CHECK(research_round>=0),
 stop_reason TEXT, executor_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(current_design_id,run_id) REFERENCES designs(design_id,run_id)
);
CREATE TABLE designs (
 design_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
 version INTEGER NOT NULL CHECK(version>=1),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB
 '*[^0-9a-f]*'),
 confirmation TEXT NOT NULL CHECK(confirmation IN ('PROPOSED','CONFIRMED','REJECTED')),
 created_at TEXT NOT NULL, UNIQUE(run_id,version), UNIQUE(design_id,run_id)
);
CREATE TABLE experiments (
 experiment_id TEXT PRIMARY KEY,  run_id TEXT NOT NULL REFERENCES runs(run_id),  design_id TEXT
 NOT NULL,
 initiation TEXT NOT NULL CHECK(initiation IN ('planned','exploratory')),
 input_artifact_id TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64 AND content_sha256 NOT GLOB
 '*[^0-9a-f]*'),
 selected_result_artifact_id TEXT, created_at TEXT NOT NULL, UNIQUE(experiment_id,run_id),
 FOREIGN KEY(design_id,run_id) REFERENCES designs(design_id,run_id),
 FOREIGN KEY(input_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(selected_result_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id)
);
CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY,  run_id TEXT NOT NULL REFERENCES runs(run_id),  experiment_id TEXT
 NOT NULL,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>=1), tool_call_id TEXT NOT NULL UNIQUE,
 analysis_backend TEXT NOT NULL CHECK(analysis_backend='stata'),
 execution_status TEXT NOT NULL CHECK(execution_status IN ('PENDING', 'RUNNING', 'SUCCEEDED',
 'FAILED', 'INTERRUPTED', 'CANCELLED')),
 extraction_status TEXT NOT NULL CHECK(extraction_status IN ('PENDING', 'SUCCEEDED', 'FAILED',
 'NOT_APPLICABLE')),
 cancel_requested INTEGER NOT NULL CHECK(cancel_requested IN (0,1)),
 termination_confirmed INTEGER NOT NULL CHECK(termination_confirmed IN (0,1)),
 input_artifact_id TEXT NOT NULL, code_artifact_id TEXT, raw_artifact_id TEXT,
 result_artifact_id TEXT, validation_artifact_id TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
 UNIQUE(experiment_id,attempt_no), UNIQUE(attempt_id,run_id),
 FOREIGN KEY(experiment_id,run_id) REFERENCES experiments(experiment_id,run_id),
 FOREIGN KEY(tool_call_id,run_id) REFERENCES tool_calls(tool_call_id,run_id),
 FOREIGN KEY(input_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(code_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(raw_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(result_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(validation_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id)
);
CREATE TABLE artifacts (
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), kind TEXT NOT NULL
 CHECK(kind IN ('task','environment','input_manifest','input','design','code','raw_result','result',
 'validation','model_request','model_response','tool_arguments','tool_response','log','report')),
 relative_path TEXT NOT NULL, sha256 TEXT NOT NULL
 CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),  byte_size INTEGER NOT NULL
 CHECK(byte_size>=0),
 format_version INTEGER NOT NULL CHECK(format_version>=1), design_id TEXT, attempt_id TEXT,
 model_call_id TEXT, tool_call_id TEXT, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at TEXT NOT NULL, UNIQUE(run_id,relative_path), UNIQUE(artifact_id,run_id),
 FOREIGN KEY(design_id,run_id) REFERENCES designs(design_id,run_id),
 FOREIGN KEY(attempt_id,run_id) REFERENCES attempts(attempt_id,run_id),
 FOREIGN KEY(model_call_id,run_id) REFERENCES model_calls(model_call_id,run_id),
 FOREIGN KEY(tool_call_id,run_id) REFERENCES tool_calls(tool_call_id,run_id)
);
CREATE TABLE human_requests (
 request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
 kind TEXT NOT NULL CHECK(kind IN ('design_confirmation','research_question')),
 target_design_id TEXT NOT NULL,  target_design_sha256 TEXT NOT NULL
 CHECK(length(target_design_sha256)=64),
 status TEXT NOT NULL CHECK(status IN ('PENDING','APPLIED','CANCELLED')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
 answered_at TEXT, applied_at TEXT, UNIQUE(request_id,run_id),
 FOREIGN KEY(target_design_id,run_id) REFERENCES designs(design_id,run_id)
);
CREATE TABLE events (
 event_id TEXT PRIMARY KEY,  run_id TEXT NOT NULL REFERENCES runs(run_id),  sequence INTEGER NOT
 NULL CHECK(sequence>=1),
 event_type TEXT NOT NULL, design_id TEXT, experiment_id TEXT, attempt_id TEXT, request_id TEXT,
 artifact_id TEXT, model_call_id TEXT, tool_call_id TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
 UNIQUE(run_id,sequence),
 FOREIGN KEY(design_id,run_id) REFERENCES designs(design_id,run_id),
 FOREIGN KEY(experiment_id,run_id) REFERENCES experiments(experiment_id,run_id),
 FOREIGN KEY(attempt_id,run_id) REFERENCES attempts(attempt_id,run_id),
 FOREIGN KEY(request_id,run_id) REFERENCES human_requests(request_id,run_id),
 FOREIGN KEY(artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(model_call_id,run_id) REFERENCES model_calls(model_call_id,run_id),
 FOREIGN KEY(tool_call_id,run_id) REFERENCES tool_calls(tool_call_id,run_id)
);
CREATE TABLE model_calls (
 model_call_id TEXT PRIMARY KEY,  run_id TEXT NOT NULL REFERENCES runs(run_id),  operation_id
 TEXT NOT NULL,
 retry_no INTEGER NOT NULL CHECK(retry_no>=0), parent_tool_call_id TEXT, context_design_id TEXT,
 request_artifact_id TEXT, response_artifact_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED',
 'UNKNOWN')),
 parse_status TEXT NOT NULL CHECK(parse_status IN ('PENDING','VALID','INVALID','NOT_APPLICABLE')),
 input_tokens INTEGER CHECK(input_tokens>=0), output_tokens INTEGER CHECK(output_tokens>=0),
 total_tokens INTEGER CHECK(total_tokens>=0), cached_tokens INTEGER CHECK(cached_tokens>=0),
 reasoning_tokens INTEGER CHECK(reasoning_tokens>=0), started_at TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), created_at TEXT NOT NULL,
 UNIQUE(operation_id,retry_no), UNIQUE(model_call_id,run_id),
 FOREIGN KEY(parent_tool_call_id,run_id) REFERENCES tool_calls(tool_call_id,run_id),
 FOREIGN KEY(context_design_id,run_id) REFERENCES designs(design_id,run_id),
 FOREIGN KEY(request_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(response_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id)
);
CREATE TABLE tool_calls (
 tool_call_id TEXT PRIMARY KEY,  run_id TEXT NOT NULL REFERENCES runs(run_id),  operation_id
 TEXT NOT NULL,
 retry_no INTEGER NOT NULL CHECK(retry_no>=0), source_model_call_id TEXT,
 arguments_artifact_id TEXT, response_artifact_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED',
 'UNKNOWN')),
 arguments_status TEXT NOT NULL CHECK(arguments_status IN ('pending','pass','fail')),
 started_at TEXT,  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),  created_at TEXT
 NOT NULL,
 UNIQUE(operation_id,retry_no), UNIQUE(tool_call_id,run_id),
 FOREIGN KEY(source_model_call_id,run_id) REFERENCES model_calls(model_call_id,run_id),
 FOREIGN KEY(arguments_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id),
 FOREIGN KEY(response_artifact_id,run_id) REFERENCES artifacts(artifact_id,run_id)
);
CREATE INDEX experiments_design ON experiments(design_id);
CREATE INDEX attempts_status ON attempts(execution_status);
CREATE UNIQUE INDEX human_one_pending ON human_requests(run_id) WHERE status='PENDING';
CREATE INDEX human_target ON human_requests(target_design_id);
CREATE INDEX model_status_time ON model_calls(status,started_at);
CREATE INDEX model_parent_tool ON model_calls(parent_tool_call_id);
CREATE INDEX tool_status_time ON tool_calls(status,started_at);
CREATE INDEX tool_source_model ON tool_calls(source_model_call_id);
CREATE INDEX artifacts_attempt ON artifacts(attempt_id);
CREATE TRIGGER runs_identity BEFORE UPDATE ON runs
 WHEN NEW.run_id IS NOT OLD.run_id OR NEW.analysis_backend IS NOT OLD.analysis_backend
 OR NEW.task_json IS NOT OLD.task_json OR NEW.input_manifest_json IS NOT OLD.input_manifest_json
 OR NEW.config_json IS NOT OLD.config_json OR NEW.environment_json IS NOT OLD.environment_json
 BEGIN SELECT RAISE(ABORT,'研究会话身份和输入不可改写'); END;
CREATE TRIGGER designs_immutable BEFORE UPDATE ON designs
 WHEN NEW.design_id IS NOT OLD.design_id OR NEW.run_id IS NOT OLD.run_id OR NEW.version IS NOT
 OLD.version
 OR NEW.payload_json IS NOT OLD.payload_json OR NEW.content_sha256 IS NOT OLD.content_sha256
 BEGIN SELECT RAISE(ABORT,'设计内容不可改写'); END;
CREATE TRIGGER experiments_immutable BEFORE UPDATE ON experiments
 WHEN NEW.experiment_id IS NOT OLD.experiment_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.design_id IS NOT OLD.design_id OR NEW.input_artifact_id IS NOT OLD.input_artifact_id
 OR NEW.payload_json IS NOT OLD.payload_json OR NEW.content_sha256 IS NOT OLD.content_sha256
 OR NEW.initiation IS NOT OLD.initiation
 BEGIN SELECT RAISE(ABORT,'分析规格不可改写'); END;
CREATE TRIGGER artifacts_immutable BEFORE UPDATE ON artifacts
 BEGIN SELECT RAISE(ABORT,'已归档产物不可改写'); END;
CREATE TRIGGER events_append_only BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'事件只允许追加'); END;
CREATE TRIGGER attempts_identity BEFORE UPDATE ON attempts
 WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.experiment_id IS NOT OLD.experiment_id OR NEW.attempt_no IS NOT OLD.attempt_no
 OR NEW.tool_call_id IS NOT OLD.tool_call_id OR NEW.analysis_backend IS NOT OLD.analysis_backend
 OR NEW.input_artifact_id IS NOT OLD.input_artifact_id
 OR (OLD.code_artifact_id IS NOT NULL AND NEW.code_artifact_id IS NOT OLD.code_artifact_id)
 OR (OLD.cancel_requested=1 AND NEW.cancel_requested=0)
 BEGIN SELECT RAISE(ABORT,'尝试身份、代码和取消标记不可回退'); END;
"""


def prepare_database(connection: sqlite3.Connection, *, writable: bool) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise StateFormatError("无法启用 SQLite 外键检查")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > DATABASE_VERSION:
        raise FutureVersionError(f"数据库版本 {version} 比本程序新，请使用兼容版本；原数据库保留")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if version == 0 and not tables and writable:
        # executescript 自身不保证整组建表原子性，因此显式包含事务边界。
        history_triggers = "\n".join(
            f"CREATE TRIGGER {table}_keep_history BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT,'研究历史不可删除'); END;"
            for table in TABLES
        )
        try:
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + DDL + history_triggers + "\nPRAGMA user_version=1;\nCOMMIT;"
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        return
    if version != DATABASE_VERSION or tables != TABLES:
        raise StateFormatError("数据库不是受支持的九表 v1 格式；不进行隐式重建或迁移")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StateFormatError("数据库含有无效外键关联，请先保留原文件并排查")
