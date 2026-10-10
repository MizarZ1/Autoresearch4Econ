"""显式验收两项真实并发审阅及后续模型反馈；复用阶段 3 原始结果。"""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from econ_agent.config import ModelConfig
from econ_agent.harness import ResearchLoop
from econ_agent.inputs import prepare_input
from econ_agent.model_client import OpenAITransport, PersistentModelClient, redact
from econ_agent.review import ResultReviewer
from econ_agent.schemas import CallStatus, ParseStatus, ReviewArguments, RunStatus
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_mcp import StataMCPTransport
from econ_agent.state import RunStore, file_hash, new_id

ROOT = Path(__file__).resolve().parents[1]


def counts(store: RunStore) -> dict[str, int]:
    """摘要的计数字段经过类型检查，不把空值用量强制写为零。"""
    summary = store.call_summary()
    assert all(type(value) is int for value in summary.values())
    return {key: value for key, value in summary.items() if isinstance(value, int)}


async def main() -> int:
    config = ModelConfig.from_environment()
    if not config.api_key():
        print("缺少既有模型凭证，真实阶段 4 验收未执行。", flush=True)
        return 1
    latest = json.loads((ROOT / "local_reference/stage3/latest.json").read_text())
    assert latest["status"] == "passed", "须先完成阶段 3 真实验收"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    directory = ROOT / "local_reference/stage4" / stamp
    directory.mkdir(parents=True)
    record: dict[str, Any] = {
        "schema_version": 1,
        "stage": 4,
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": latest["run_id"],
        "endpoint": config.base_url,
        "requested_model": config.model,
        "sdk_retries": 0,
        "max_review_concurrency": 2,
        "review_timeout_seconds": 60,
        "review_requests_per_role": 1,
        "code_sha256": {
            str(p.relative_to(ROOT)): file_hash(p)
            for p in [Path(__file__), *sorted((ROOT / "src/econ_agent").glob("*.py"))]
        },
        "confirmation_is_developer_fixture": True,
        "说明": (
            "追加真实审阅及一项后续决策，不重新执行统计；阶段 3 的确认仍是开发夹具。"
            "模型意见不改变程序核验或代替人工确认。"
        ),
    }
    earlier = []
    for path in sorted((ROOT / "local_reference/stage4").glob("*/validation.json")):
        saved = json.loads(path.read_text())
        if saved["run_id"] == latest["run_id"]:
            earlier.append(
                {
                    "started_at": saved["started_at"],
                    "ended_at": saved["ended_at"],
                    "status": saved["status"],
                    "calls_delta": saved["calls_delta"],
                    "review_batch": saved.get("review_batch"),
                    "code_sha256": saved["code_sha256"],
                }
            )
    record["earlier_live_attempts"] = earlier
    with RunStore.open(ROOT / "runs" / latest["run_id"]) as store:
        assert store.load_state().status == RunStatus.PAUSED
        before = counts(store)
        assert not before["unfinished_model_calls"] and not before["unfinished_tool_calls"]
        assert store.load_state().budget.max_model_calls - before["model_calls"] >= 3
        assert store.load_state().budget.max_tool_calls - before["tool_calls"] >= 1
        results = store.selected_results()
        assert len(results) == 2
        prepared = prepare_input(store, ROOT / "backups")
        original_results = [r.model_dump(mode="json") for r in results]
        identifiers = tuple(r.result_id for r in results if r.result_id)
        design_id = store.load_state().current_design_id
        assert design_id
        proof: list[dict[str, str]] = []
        for result in results:
            attempt = store.attempt(result.attempt_id)
            assert attempt.validation_artifact_id and attempt.code_artifact_id
            assert attempt.termination_confirmed
            proof.append(
                {
                    "result_id": result.result_id or "",
                    "attempt_id": attempt.attempt_id,
                    "validation_artifact_id": attempt.validation_artifact_id,
                    "validation_sha256": store.artifact(attempt.validation_artifact_id).sha256,
                    "code_artifact_id": attempt.code_artifact_id,
                    "code_sha256": store.artifact(attempt.code_artifact_id).sha256,
                    "result_sha256": hashlib.sha256(
                        store.read_artifact(result.result_id or "")
                    ).hexdigest(),
                }
            )
        record.update(calls_before=before, input_results=proof, design_id=design_id)
        model_transport = OpenAITransport(config)
        store.transition_run(RunStatus.RUNNING, reason="研究者授权追加阶段 4 审阅验收")
        try:
            adapter = PersistentModelClient(store, config, model_transport, max_transport_retries=0)
            reviewer = ResultReviewer(store, adapter, timeout_seconds=60)
            executable = Path(
                os.getenv(
                    "STATA_PATH",
                    str(ROOT / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp"),
                )
            )
            # 只构造既有后端供生产循环建立上下文；不进入 MCP 会话，不调用任何统计工具。
            stata_transport = StataMCPTransport(executable, ROOT, directory / "unused_mcp.log")
            backend = StataBackend(store, stata_transport, prepared, ROOT / "tmp/stata")
            loop = ResearchLoop(
                store, adapter, backend, prepared.input_artifact_id, reviewer=reviewer
            )
            assert loop._remaining() >= 120, "剩余活动时间不足以完成本次真实验收"
            print("正在对阶段 3 的两项真实结果发送两项并发 DeepSeek 审阅。", flush=True)
            batch = await reviewer.review(
                ReviewArguments(design_id=design_id, result_artifact_ids=identifiers)
            )
            record["review_batch"] = batch.model_dump(mode="json")
            assert batch.status == "succeeded" and batch.intervals_overlap is True
            loop.feedback.append(
                {
                    "action": "review_results",
                    "tool_call_id": batch.tool_call_id,
                    "review_batch": batch.model_dump(mode="json"),
                    "error": None,
                }
            )
            decision = await adapter.decide(
                context=loop.context(),
                operation_id=new_id("operation"),
                context_design_id=design_id,
                context_result_ids=identifiers,
                timeout_seconds=min(60, loop._remaining()),
            )
            assert decision.action and decision.model_call.request_artifact_id
            sent = json.loads(
                json.loads(store.read_artifact(decision.model_call.request_artifact_id))[
                    "messages"
                ][1]["content"]
            )
            assert sent["recent_feedback"][-1]["review_batch"] == batch.model_dump(mode="json")
            record["next_decision"] = {
                "model_call_id": decision.model_call.model_call_id,
                "request_artifact_id": decision.model_call.request_artifact_id,
                "action": decision.action.model_dump(mode="json"),
                "feedback_present_in_saved_request": True,
                "executed": False,
                "说明": "真实后续模型决策已归档，本阶段不分发额外分析或论文输出动作",
            }
            loop._checkpoint(reason="阶段 4 两项审阅及后续模型请求验收完成")
            assert [r.model_dump(mode="json") for r in store.selected_results()] == original_results
            assert all(
                store.artifact(p["validation_artifact_id"]).sha256 == p["validation_sha256"]
                for p in proof
            )
            assert file_hash(store.load_task().data_path) == prepared.original_sha256
            record.update(
                original_unchanged=True,
                selected_results_unchanged=True,
                validation_unchanged=True,
                new_statistical_attempts=0,
            )
            record["status"] = "passed"
        except Exception as exc:
            record.update(status="failed", exception_type=type(exc).__name__)
            print("阶段 4 真实验收未通过，本次实际请求和独立输出均保留。", flush=True)
        finally:
            await model_transport.close()
            if store.load_state().status == RunStatus.RUNNING:
                store.transition_run(
                    RunStatus.PAUSED,
                    reason="阶段 4 真实验收完成，等待后续授权"
                    if record["status"] == "passed"
                    else "阶段 4 真实验收失败，保留记录等待核对",
                )
        after = counts(store)
        record.update(
            calls_after=after,
            calls_delta={k: after[k] - before[k] for k in before if k != "schema_version"},
            model_calls=[
                c.model_dump(mode="json") for c in store.model_calls()[before["model_calls"] :]
            ],
            run_state=store.load_state().status.value,
            ended_at=datetime.now(UTC).isoformat(),
        )
        if record["status"] == "passed":
            assert (
                record["calls_delta"]["model_calls"] == 3
                and record["calls_delta"]["tool_calls"] == 1
            )
            assert all(
                c.status == CallStatus.SUCCEEDED and c.parse_status == ParseStatus.VALID
                for c in store.model_calls()[before["model_calls"] :]
            )
            assert not after["unfinished_model_calls"] and not after["unfinished_tool_calls"]
    with RunStore.open(ROOT / "runs" / latest["run_id"], writable=False) as restored:
        if record["status"] == "passed":
            assert len(restored.selected_results()) == 2
            record["reopened_selected_count"] = 2
    (directory / "validation.json").write_text(
        json.dumps(redact(record), ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "local_reference/stage4/latest.json").write_text(
        json.dumps(
            {
                "run_id": latest["run_id"],
                "status": record["status"],
                "evidence_directory": str(directory),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if record["status"] == "passed":
        # 公共证据仅包含受控数值、意见及来源 ID；原始请求响应仍在忽略的 run 内。
        (ROOT / "docs/evidence/stage4_reviews.json").write_text(
            json.dumps(redact(record), ensure_ascii=False, indent=2) + "\n"
        )
    print(
        json.dumps(
            {
                "状态": record["status"],
                "本阶段调用": record["calls_delta"],
                "两项区间重叠": record.get("review_batch", {}).get("intervals_overlap"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
