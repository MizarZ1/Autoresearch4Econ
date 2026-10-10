"""显式阶段 3 验收：真实模型自行生成动作和代码，再由本机 Stata 执行。"""

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
from econ_agent.schemas import (
    Budget,
    DesignContent,
    HumanAnswer,
    ResearchTask,
    RunStatus,
    content_hash,
)
from econ_agent.stata_backend import StataBackend
from econ_agent.stata_contracts import AnalysisOutcome
from econ_agent.stata_mcp import StataMCPTransport
from econ_agent.state import RunStore
from scripts.validate_stage2 import check_reference

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    config = ModelConfig.from_environment()
    if not config.api_key():
        print("缺少既有模型凭证，真实阶段 3 验收未执行。", flush=True)
        return 1
    source = ResearchTask.from_file(ROOT / "examples/demo/task.json")
    task = ResearchTask.model_validate(
        source.model_dump()
        | {
            "research_goal": (
                "在 baseco==1 的样本中用 logem4 作为 avexpr 的工具变量，估计对 logpgp95 的关系。"
                "本阶段先做已确认设计内的基准 IV，收到真实结果后决定并执行一项补充分析，"
                "例如加入 lat_abst 检查比较结果，再结束并说明证据与识别局限。"
                "本阶段不比较 OLS，不改变设计，不进行论文导出；不得追求显著性。"
            ),
            "budget": Budget(
                max_research_rounds=3, max_model_calls=8, max_tool_calls=12, max_runtime_seconds=480
            ).model_dump(),
        }
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    directory = ROOT / "local_reference/stage3" / stamp
    directory.mkdir(parents=True)
    code = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [Path(__file__), *sorted((ROOT / "src/econ_agent").glob("*.py"))]
    }
    record: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "endpoint": config.base_url,
        "requested_model": config.model,
        "sdk_retries": 0,
        "code_sha256": code,
        "confirmation_is_developer_fixture": True,
        "说明": "真实模型生成动作和主体代码；设计确认是开发夹具，不是阶段 5 人工修改验收",
    }
    executable = Path(
        os.getenv(
            "STATA_PATH", str(ROOT / "local_reference/stata18/StataMP.app/Contents/MacOS/stata-mp")
        )
    )
    with RunStore.create(
        ROOT / "runs",
        task,
        run_id=f"run_stage3_{stamp}",
        environment={"requested_model": config.model, "random_seed": 404},
    ) as store:
        record["run_id"] = store.run_id
        prepared = prepare_input(store, ROOT / "backups")
        store.transition_run(RunStatus.RUNNING)
        design = store.create_design(
            DesignContent(
                population="AJR 作者国家数据的基准样本",
                variables={
                    "logpgp95": "收入对数",
                    "avexpr": "制度保护指标",
                    "logem4": "定居者死亡率对数",
                    "lat_abst": "绝对纬度控制",
                },
                sample_rule="baseco==1",
                estimand="基准 IV 与设计内补充 IV 的估计比较",
                methods=("ivregress 2sls",),
                assumptions=("工具外生性及排除限制不可仅凭数值证明，必须披露",),
            ),
            change_reason="阶段 3 开发验收预先固定的设计范围，动作与代码由真实模型决定",
        )
        request = store.request_human(design.design_id, "确认阶段 3 开发验收设计")
        store.apply_answer(
            request.request_id,
            design_sha256=content_hash(design),
            answer=HumanAnswer(decision="accept", explanation="开发验收夹具确认"),
        )
        model_transport = OpenAITransport(config)
        try:
            async with StataMCPTransport(
                executable, ROOT, directory / "mcp_server.log"
            ) as transport:
                backend = StataBackend(
                    store, transport, prepared, ROOT / "tmp/stata", timeout_seconds=60
                )
                model = PersistentModelClient(
                    store, config, model_transport, timeout_seconds=60, max_transport_retries=1
                )
                loop = ResearchLoop(store, model, backend, prepared.input_artifact_id)
                print(
                    "正在运行真实 DeepSeek 与本机 Stata 研究循环；不下载 Stata 更新包。", flush=True
                )
                reason = await loop.run()
                record.update(
                    stop_reason=reason,
                    feedback=loop.feedback,
                    environment=transport.environment,
                    calls=store.call_summary(),
                )
                results = store.selected_results()
                assert len(results) >= 2, "真实模型未完成基准后的补充分析，请查阅本次 trace"
                record["results"] = [r.model_dump(mode="json") for r in results]
                links = []
                for result in results:
                    spec = store.experiment(result.experiment_id)
                    attempt = store.attempt(result.attempt_id)
                    tool = store.tool_call(attempt.tool_call_id)
                    assert tool.source_model_call_id
                    call = store.model_call(tool.source_model_call_id)
                    assert call.request_artifact_id
                    context = json.loads(
                        json.loads(store.read_artifact(call.request_artifact_id))["messages"][1][
                            "content"
                        ]
                    )
                    sent = {r["result_id"] for r in context["selected_results"]}
                    assert set(spec.based_on_result_ids) <= sent
                    if spec.based_on_result_ids:
                        assert set(spec.based_on_result_ids) <= set(call.context_result_ids)
                    links.append(
                        {
                            "result_id": result.result_id,
                            "model_call_id": call.model_call_id,
                            "request_artifact_id": call.request_artifact_id,
                            "tool_call_id": tool.tool_call_id,
                            "based_on_result_ids": spec.based_on_result_ids,
                        }
                    )
                    specification = "latitude" if "lat_abst" in spec.variables else "baseline"
                    assert attempt.validation_artifact_id
                    record.setdefault("reference_validations", []).append(
                        check_reference(
                            store,
                            AnalysisOutcome(
                                result=result, validation_artifact_id=attempt.validation_artifact_id
                            ),
                            specification,
                        )
                    )
                assert any(link["based_on_result_ids"] for link in links)
                record["decision_links"] = links
                record["original_unchanged"] = (
                    hashlib.sha256(task.data_path.read_bytes()).hexdigest()
                    == prepared.original_sha256
                )
                assert record["original_unchanged"]
                assert all(store.attempt(r.attempt_id).termination_confirmed for r in results)
                assert not store.call_summary()["unfinished_model_calls"]
                assert not store.call_summary()["unfinished_tool_calls"]
                record["all_analysis_workers_stopped"] = True
                record["model_calls"] = [c.model_dump(mode="json") for c in store.model_calls()]
                record["loop_ended_with_finish"] = any(
                    f.get("action") == "finish_research" and not f.get("error")
                    for f in loop.feedback
                )
                assert record["loop_ended_with_finish"]
                record["status"] = "passed"
        except Exception as exc:
            # 不输出异常正文、模型返回或许可日志，失败证据仍保存在 run。
            record.update(status="failed", exception_type=type(exc).__name__)
            print("真实阶段 3 验收未通过，保留本次运行记录供排查。", flush=True)
        finally:
            await model_transport.close()
        record["ended_at"] = datetime.now(UTC).isoformat()
        (directory / "validation.json").write_text(
            json.dumps(redact(record), ensure_ascii=False, indent=2) + "\n"
        )
        (ROOT / "local_reference/stage3/latest.json").write_text(
            json.dumps(
                {
                    "run_id": store.run_id,
                    "status": record["status"],
                    "evidence_directory": str(directory),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        run_directory = store.directory
    with RunStore.open(run_directory, writable=False) as reopened:
        assert len(reopened.selected_results()) >= 2 if record["status"] == "passed" else True
    print(
        json.dumps(
            {
                "状态": record["status"],
                "运行": record["run_id"],
                "调用摘要": record.get("calls"),
                "停止原因": record.get("stop_reason"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
