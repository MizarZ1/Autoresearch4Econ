"""只读导出已有证据及可迁移的 Stata 重跑入口，不调用模型或分析后端。"""

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from econ_agent.model_client import redact
from econ_agent.paper_contracts import SECTIONS
from econ_agent.reporting import (
    csv_bytes,
    current_report,
    json_bytes,
    result_tables,
    tables_markdown,
)
from econ_agent.schemas import Artifact, utc_now
from econ_agent.state import RunStore, StateConflict, new_id


def call_records(store: RunStore) -> list[dict[str, Any]]:
    records = [dict(kind="model", **c.model_dump(mode="json")) for c in store.model_calls()] + [
        dict(kind="tool", **c.model_dump(mode="json")) for c in store.tool_calls()
    ]
    index = {a.artifact_id: a for a in store.artifact_index()}
    for record in records:
        record["artifact_refs"] = {
            field: {
                "artifact_id": index[identifier].artifact_id,
                "sha256": index[identifier].sha256,
                "kind": index[identifier].kind,
            }
            for field in ("request_artifact_id", "response_artifact_id", "arguments_artifact_id")
            if (identifier := record.get(field)) is not None and identifier in index
        }
    return records


def usage_summary(store: RunStore, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """已知部分与缺失分别汇总；并发请求时间取区间并集，不能相加冒充墙钟时间。"""
    summary: dict[str, Any] = dict(store.call_summary())
    models = [c for c in calls if c["kind"] == "model"]
    for field in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
    ):
        values = [c[field] for c in models if c[field] is not None]
        summary[field] = {
            "known_sum": sum(values) if values else None,
            "missing_calls": len(models) - len(values),
        }
    intervals = sorted(
        (
            datetime.fromisoformat(c["started_at"]).timestamp(),
            datetime.fromisoformat(c["ended_at"]).timestamp(),
        )
        for c in calls
        if c["started_at"] and c["ended_at"]
    )
    total = 0.0
    end: float | None = None
    for left, right in intervals:
        total += max(0.0, right - max(left, end if end is not None else left))
        end = max(right, end if end is not None else right)
    summary.update(
        {
            "known_request_active_seconds": total if intervals else None,
            "missing_interval_calls": len(calls) - len(intervals),
            "duration_note": "此项是有记录的模型和工具请求区间并集，不是研究总墙钟时长",
            "model_transport_failure_rate": summary["model_transport_failures"] / len(models)
            if models
            else None,
            "model_parse_failure_rate": summary["model_parse_failures"] / len(models)
            if models
            else None,
        }
    )
    return summary


def analysis_history(store: RunStore, selected_ids: set[str]) -> list[dict[str, Any]]:
    rows = []
    for spec in store.experiments():
        attempts = store.attempts(spec.experiment_id)
        for attempt in attempts or (None,):
            selected = bool(attempt and attempt.result_artifact_id in selected_ids)
            reason = (
                "当前已选用并核验"
                if selected
                else (
                    "历史设计，不纳入当前结论"
                    if spec.design_id != store.load_state().current_design_id
                    else "未被明确选用，或执行/提取/必需核验尚未通过；数值仅作历史记录"
                )
            )
            rows.append(
                {
                    "experiment_id": spec.experiment_id,
                    "purpose": spec.purpose,
                    "design_id": spec.design_id,
                    "design_version": store.design(spec.design_id).version,
                    "initiation": spec.initiation,
                    "based_on_result_ids": ";".join(spec.based_on_result_ids),
                    "method": spec.method,
                    "sample_rule": spec.sample_rule,
                    "attempt_id": attempt.attempt_id if attempt else None,
                    "attempt_no": attempt.attempt_no if attempt else None,
                    "execution_status": attempt.execution_status if attempt else "尚无尝试",
                    "extraction_status": attempt.extraction_status if attempt else None,
                    "termination_confirmed": attempt.termination_confirmed if attempt else None,
                    "current_eligible": selected,
                    "exclusion_reason": reason,
                    "input_artifact_id": spec.input_artifact_id,
                    "code_artifact_id": attempt.code_artifact_id if attempt else None,
                    "raw_artifact_id": attempt.raw_artifact_id if attempt else None,
                    "result_artifact_id": attempt.result_artifact_id if attempt else None,
                    "validation_artifact_id": attempt.validation_artifact_id if attempt else None,
                    "tool_call_id": attempt.tool_call_id if attempt else None,
                    "error_summary": attempt.error_summary if attempt else None,
                }
            )
    return rows


def metadata(store: RunStore) -> dict[str, Any]:
    return {
        "run": store.load_state().model_dump(mode="json"),
        "designs": [
            {
                "design": d.model_dump(mode="json"),
                "confirmation": store.design_confirmation(d.design_id),
            }
            for d in store.designs()
        ],
        "human_requests": [r.model_dump(mode="json") for r in store.human_requests()],
        "events": store.events(),
        "calls": call_records(store),
        "attempts": [
            a.model_dump(mode="json")
            for e in store.experiments()
            for a in store.attempts(e.experiment_id)
        ],
        "experiments": [spec.model_dump(mode="json") for spec in store.experiments()],
    }


def portable_scripts(
    store: RunStore, copied: dict[str, Artifact], selected_ids: set[str]
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    scripts: dict[str, bytes] = {}
    entries = []
    for spec in store.experiments():
        for attempt in store.attempts(spec.experiment_id):
            artifacts = {
                Path(a.relative_path).name: a
                for a in copied.values()
                if a.attempt_id == attempt.attempt_id
            }
            selected = attempt.result_artifact_id in selected_ids
            required = [attempt.input_artifact_id, attempt.code_artifact_id]
            if (
                any(identifier not in copied for identifier in required)
                or "body.do" not in artifacts
                or "extract.do" not in artifacts
            ):
                entries.append(
                    {
                        "attempt_id": attempt.attempt_id,
                        "current": selected,
                        "portable": False,
                        "reason": "缺少完整输入、原主体或提取脚本，不生成可重跑标记",
                    }
                )
                continue
            assert attempt.code_artifact_id
            wrapper = store.read_artifact(attempt.code_artifact_id).decode()
            body = store.read_artifact(artifacts["body.do"].artifact_id).decode()
            extraction = store.read_artifact(artifacts["extract.do"].artifact_id).decode()
            seed = re.search(r"(?m)^set seed (\d+)\s*$", wrapper)
            # 只有已有受控变量路径的标准包装器可迁移；不静默改写任意模型脚本。
            if (
                not seed
                or re.search(r'["\'](?:/|[A-Za-z]:[/\\])', body + extraction)
                or "version 18" not in wrapper
            ):
                entries.append(
                    {
                        "attempt_id": attempt.attempt_id,
                        "current": selected,
                        "portable": False,
                        "reason": "环境、种子或绝对路径需要人工适配，未声称可移植",
                    }
                )
                continue
            input_artifact = copied[attempt.input_artifact_id]
            load = (
                'import delimited "$ECON_INPUT", clear varnames(1)'
                if input_artifact.relative_path.endswith(".csv")
                else 'use "$ECON_INPUT", clear'
            )
            output = f"rerun_output/{attempt.attempt_id}"
            script = "\n".join(
                [
                    "* 派生重跑入口；原主体、提取脚本及输入的 hash 见复现清单。",
                    "args package_root",
                    'if `"`package_root\'"\' == "" local package_root `"`c(pwd)\'"\'',
                    "version 18",
                    "clear all",
                    "set more off",
                    "set processors 1",
                    f"set seed {seed[1]}",
                    'capture mkdir "`package_root\'/rerun_output"',
                    f'capture mkdir "`package_root\'/{output}"',
                    f'global ECON_INPUT "`package_root\'/sources/{input_artifact.relative_path}"',
                    f'global ECON_OUTPUT "`package_root\'/{output}"',
                    "capture log close",
                    'log using "$ECON_OUTPUT/reproduce.log", text replace',
                    load,
                    f'do "`package_root\'/sources/{artifacts["body.do"].relative_path}"',
                    f'do "`package_root\'/sources/{artifacts["extract.do"].relative_path}"',
                    "log close",
                    "",
                ]
            )
            name = f"rerun/{attempt.attempt_id}.do"
            scripts[name] = script.encode()
            entries.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "current": selected,
                    "portable": True,
                    "script": name,
                    "seed": int(seed[1]),
                    "stata_version": 18,
                    "source_artifacts": [
                        {"artifact_id": a.artifact_id, "sha256": a.sha256}
                        for a in (
                            input_artifact,
                            copied[attempt.code_artifact_id],
                            artifacts["body.do"],
                            artifacts["extract.do"],
                        )
                    ],
                    "derived_sha256": hashlib.sha256(scripts[name]).hexdigest(),
                }
            )
    lines = [
        "* 在全新 Stata 会话从本包重建当前结果；历史尝试入口见 script_manifest.json。",
        "args package_root",
        'if `"`package_root\'"\' == "" local package_root `"`c(pwd)\'"\'',
    ]
    lines += [
        f'do "`package_root\'/{e["script"]}" `"`package_root\'"\''
        for e in entries
        if e["current"] and e["portable"]
    ]
    lines += ["* 本入口不下载命令或更新包；使用本机既有 Stata。", ""]
    scripts["reproduce.do"] = "\n".join(lines).encode()
    return scripts, entries


def export_run(store: RunStore, output_root: Path) -> Path:
    """源会话零写入；新目录先完整准备，最后原子发布复现包清单。"""
    before = metadata(store)
    identifier = new_id("export")
    parent = output_root.expanduser().resolve() / store.run_id
    if parent.is_relative_to(store.directory):
        raise ValueError("导出目录不能位于源运行目录内，以免破坏归档恢复")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / ("." + identifier)
    destination = parent / identifier
    temporary.mkdir()
    files: dict[str, dict[str, Any]] = {}
    exclusions = []
    copied: dict[str, Artifact] = {}
    issues = []

    def write(name: str, payload: bytes, source: Artifact | None = None) -> None:
        path = temporary / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "source_artifact_id": source.artifact_id if source else None,
            "source_sha256": source.sha256 if source else None,
        }

    try:
        try:
            results = store.selected_results()
        except (StateConflict, OSError):
            results = ()
            issues.append("当前输入或结果证据核验失败，导出不包含当前经验数字")
        selected_ids = {r.result_id for r in results if r.result_id}
        report = current_report(store)
        if report:
            for name, ref in report["files"].items():
                write(
                    name,
                    store.read_artifact(ref["artifact_id"]),
                    store.artifact(ref["artifact_id"]),
                )
            write("report_manifest.json", json_bytes(report))
            if report["status"] != "complete":
                issues.append("现有论文为部分交付")
        else:
            issues.append("没有与当前选择及证据一致的已发布论文，导出为部分交付")
            tables = result_tables(store, results)
            write(
                "paper.md",
                (
                    "# 未完成的论文交付\n\n没有当前有效的论文版本；未调用模型或重跑分析。\n\n"
                    + "\n".join("## " + section + "\n\n待补充。\n" for section in SECTIONS)
                ).encode(),
            )
            write("tables.md", tables_markdown(tables).encode())
            write("tables.json", json_bytes(tables))
            columns = (
                tuple(tables["rows"][0]) if tables["rows"] else ("table", "name", "value", "field")
            )
            write("tables.csv", csv_bytes(tables["rows"], columns))
            write(
                "evidence.json",
                json_bytes({"claims": [], "facts": tables["facts"], "status": "partial"}),
            )
        # 明确白名单：完整模型请求/响应、秘密配置、输入保护/备份记录不导出。
        for artifact in store.artifact_index():
            allowed = artifact.kind in {
                "input",
                "design",
                "code",
                "raw_result",
                "result",
                "validation",
                "log",
                "environment",
            }
            human_input = (
                artifact.relative_path.startswith("calls/human/")
                and artifact.kind == "tool_arguments"
            )
            if not allowed and not human_input:
                exclusions.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "kind": artifact.kind,
                        "reason": "不在默认源文件白名单；报告仅复制当前发布版本",
                    }
                )
                continue
            try:
                payload = store.read_artifact(artifact.artifact_id)
            except (StateConflict, OSError):
                issues.append("源产物损坏，已排除：" + artifact.artifact_id)
                exclusions.append(
                    {"artifact_id": artifact.artifact_id, "reason": "hash 或文件核验失败"}
                )
                continue
            if human_input:
                # 人工原始答复是研究证据；不导出模型解释请求或完整响应。
                clean = json_bytes(redact(json.loads(payload)))
                write("human_inputs/" + artifact.artifact_id + ".json", clean, artifact)
            else:
                write("sources/" + artifact.relative_path, payload, artifact)
                copied[artifact.artifact_id] = artifact
        calls = call_records(store)
        write(
            "calls.jsonl",
            ("\n".join(json.dumps(redact(c), ensure_ascii=False) for c in calls) + "\n").encode(),
        )
        write("usage.json", json_bytes(usage_summary(store, calls)))
        history = analysis_history(store, selected_ids)
        columns = (
            tuple(history[0])
            if history
            else ("experiment_id", "purpose", "current_eligible", "exclusion_reason")
        )
        write("analysis_history.csv", csv_bytes(history, columns))
        for name in ("run", "designs", "human_requests", "events", "attempts", "experiments"):
            write(name + ".json", json_bytes(redact(before[name])))
        input_sources = [
            {
                "name": Path(item.path).name,
                "role": item.role,
                "sha256": item.sha256,
                "byte_size": item.byte_size,
                "snapshot_artifact_ids": [
                    a.artifact_id
                    for a in copied.values()
                    if a.kind == "input" and a.sha256 == item.sha256
                ],
            }
            for item in store.load_input_manifest().files
        ]
        write("input_sources.json", json_bytes(input_sources))
        scripts, entries = portable_scripts(store, copied, selected_ids)
        for name, payload in scripts.items():
            write(name, payload)
        write("script_manifest.json", json_bytes(entries))
        if any(e["current"] and not e["portable"] for e in entries) or not selected_ids:
            issues.append("当前重跑入口不完整，不能标记完整复现包")
        environment_artifacts = [a for a in copied.values() if a.kind == "environment"]
        environment = (
            json.loads(store.read_artifact(environment_artifacts[0].artifact_id))
            if environment_artifacts
            else {"状态": "环境记录缺失"}
        )
        raw_environment = []
        for artifact in copied.values():
            if artifact.kind == "raw_result" and Path(artifact.relative_path).name == "raw.json":
                raw_environment.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "record": json.loads(store.read_artifact(artifact.artifact_id)).get(
                            "environment"
                        ),
                    }
                )
        write(
            "environment.json",
            json_bytes(
                {
                    "run_environment": environment,
                    "analysis_environments": raw_environment,
                    "说明": "实际环境以各次原始返回为准；未记录的扩展命令版本保持未知",
                }
            ),
        )
        notes = (
            "\n".join("- " + issue for issue in issues)
            or "无工程完整性缺项；论文识别解释仍需研究者审阅。"
        )
        reproduce = f"""# 复现说明

本包后端为 Stata；使用本机既有安装，不包含许可、厂商程序或自动下载步骤。

1. 将整个包移动到任意目录。打开一个全新的 Stata 会话，切换到本包根目录。
2. 执行 `do reproduce.do`，或 `do reproduce.do "本包绝对路径"`。入口只重跑当前已选用结果。
3. 检查 `rerun_output/<attempt_id>/coefficients.csv`（系数）或 `scalars.csv`（描述统计）、
   `sample_check.txt` 及日志。
   以 `tables.json`、`evidence.json` 和原始 CSV 核对数值。
   AJR 工程验收容差：绝对容差 1e-7、相对容差 1e-6，按二者较大值核对，样本量和自由度一致；
   其他方法不借用此验收结论。

`script_manifest.json` 逐项记录原脚本、输入 artifact ID/hash、派生入口 hash、固定种子和可迁移状态。
原始 `analysis.do` 保留原路径作为历史证据；
实际重跑用派生入口，并运行未经改写的主体及提取脚本。
历史分析可通过清单中的单独入口重跑，不混入当前结论。


输入数据和材料来自 `sources/inputs/` 的已核验快照；
它们与备份有区别。
输入来源 hash 见 bundle_manifest.json，
环境及依赖版本见 environment.json 和 sources/environment.json。
默认未包含保护记录、数据库、完整模型提示词/响应及秘密配置。
导出本身没有调用模型或统计。


模型/工具逐次关联、时间、失败、解析及重试状态见 calls.jsonl；
usage.json 只汇总已知用量，缺失保持空值。
并发请求时间按区间并集报告，不冒充整个研究的墙钟时间。

全部实质分析、旧设计及未纳入原因见 analysis_history.csv；
原始人工答复、程序选定规则和脚本化确认见 human_inputs/ 与 human_requests.json，
不能将脚本化确认当作独立人工审阅。


当前交付限制：

{notes}

自动检查覆盖版本、执行/提取/必需核验、文件 hash、占位符及表格数字的引用。
自然语言句子的语义、因果识别假设、文献真实性和研究结论仍需研究者抽查。
没有被执行的检验不能视为已经完成。
"""
        write("reproduce.md", reproduce.encode())
        if metadata(store) != before:
            raise StateConflict("导出期间源会话变化，未发布混合版本复现包")
        # 再读一次当前报告与结果，避免跨文件复制时证据或选择发生变化。
        if report != current_report(store):
            raise StateConflict("导出期间报告证据发生变化")
        if results and tuple(r.result_id for r in store.selected_results()) != tuple(
            r.result_id for r in results
        ):
            raise StateConflict("导出期间当前结果发生变化")
        manifest = {
            "schema_version": 1,
            "run_id": store.run_id,
            "export_id": identifier,
            "created_at": utc_now(),
            "status": "partial" if issues else "complete",
            "report_id": report["report_id"] if report else None,
            "current_result_ids": sorted(selected_ids),
            "files": files,
            "issues": issues,
            "exclusions": exclusions,
            "说明": "本清单不自包含自身 hash；每个源文件及派生文件逐项核验",
        }
        (temporary / "bundle_manifest.json").write_bytes(json_bytes(manifest))
        temporary.rename(destination)
        return destination
    except BaseException:
        shutil.rmtree(temporary)
        raise
