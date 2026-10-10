"""离线论文与导出契约；合成结果不冒充真实统计或人工审阅。"""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from econ_agent.cli import main
from econ_agent.exporting import export_run, usage_summary
from econ_agent.paper_contracts import SECTIONS, PaperDraft
from econ_agent.reporting import (
    current_report,
    generate_report,
    paper_context,
    result_tables,
    snapshot,
    validate_draft,
)
from econ_agent.schemas import ResearchTask, RunStatus
from econ_agent.state import RunStore, StateConflict
from tests.contract.test_state import checks, confirmed, experiment, successful_result


@pytest.fixture
def ready(tmp_path: Path) -> Iterator[RunStore]:
    source = tmp_path / "source.csv"
    source.write_text("x,y\n1,2\n2,4\n")
    with RunStore.create(
        tmp_path / "runs", ResearchTask(data_path=source, research_goal="离线测试")
    ) as store:
        confirmed(store)
        spec = experiment(store)
        result = successful_result(store, spec)
        store.save_validation(result.artifact_id, checks=checks(result.artifact_id))
        store.select_result(spec.experiment_id, result.artifact_id)
        store.transition_run(RunStatus.PAUSED, reason="测试循环已结束")
        yield store


def draft_from(context: dict[str, Any]) -> PaperDraft:
    identifier = context["tables"]["contexts"][0]["result_id"]
    return PaperDraft.model_validate(
        dict(
            title="合成样本结果初稿",
            sections={
                name: "待补充。" if name != "参考材料" else "正式文献引用待补。"
                for name in SECTIONS
            },
            claims=(
                {
                    "section": "主要结果",
                    "text": "样本量为{{fact:F0001}}，均值为{{fact:F0002}}。",
                    "result_ids": (identifier,),
                },
            ),
        )
    )


@pytest.mark.parametrize(
    "text",
    [
        "本研究纳入九百九十九个国家。",
        "样本量为六十四。",
        "系数估计为负零点八。",
        "标准误为零点一。",
    ],
)
def test_chinese_empirical_numbers_cannot_bypass_facts(ready: RunStore, text: str) -> None:
    context = paper_context(ready, result_tables(ready, ready.selected_results()))
    draft = draft_from(context)
    sections = {**draft.sections, "讨论与局限": text}
    with pytest.raises(ValueError, match="中文经验数字"):
        validate_draft(draft.model_copy(update={"sections": sections}), context)


class PaperTransport:
    requests = 0
    mutate: Any = None

    def __init__(self, config: Any) -> None:
        pass

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        type(self).requests += 1
        context = json.loads(request["messages"][1]["content"])
        if self.mutate:
            self.mutate()
        return {
            "model": "离线测试模型",
            "choices": [{"message": {"content": draft_from(context).model_dump_json()}}],
        }

    async def close(self) -> None:
        pass


@pytest.fixture
def paper_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    PaperTransport.requests = 0
    PaperTransport.mutate = None
    monkeypatch.setattr("econ_agent.model_client.OpenAITransport", PaperTransport)


async def test_paper_exact_facts_unknown_usage_and_immutable_versions(
    ready: RunStore, paper_transport: None
) -> None:
    first = await generate_report(ready)
    assert first["status"] == "complete"
    assert ready.load_state().status == RunStatus.COMPLETED
    assert current_report(ready) == first
    paper = ready.read_artifact(first["files"]["paper.md"]["artifact_id"]).decode()
    assert "样本量为2，均值为1.5" in paper
    evidence = json.loads(ready.read_artifact(first["files"]["evidence.json"]["artifact_id"]))
    assert evidence["claims"][0]["facts"][1]["field"] == "normalized.values.0.value"
    assert (
        evidence["claims"][0]["facts"][1]["result_sha256"]
        == ready.artifact(ready.selected_results()[0].result_id or "").sha256
    )
    assert ready.model_calls()[-1].total_tokens is None
    assert ready.call_summary()["missing_total_usage_calls"] == 1
    second = await generate_report(ready)
    assert second["report_id"] != first["report_id"]
    assert PaperTransport.requests == 2
    assert ready.read_artifact(first["files"]["paper.md"]["artifact_id"]).decode() == paper


@pytest.mark.parametrize(
    "modification",
    [
        "number",
        "unknown_fact",
        "wrong_result",
        "bibliography",
        "missing_section",
        "unbound_section",
    ],
)
def test_reject_unbound_digits_stale_references_and_fabricated_citations(
    ready: RunStore, modification: str
) -> None:
    context = paper_context(ready, result_tables(ready, ready.selected_results()))
    body = draft_from(context).model_dump(mode="json")
    if modification == "number":
        body["claims"][0]["text"] += "系数为999。"
    elif modification == "unknown_fact":
        body["claims"][0]["text"] = "样本为{{fact:F9999}}。"
    elif modification == "wrong_result":
        body["claims"][0]["result_ids"] = ["result_stale"]
    elif modification == "bibliography":
        body["sections"]["参考材料"] = "杜撰作者的杜撰论文。"
    elif modification == "missing_section":
        body["sections"].pop("补充分析")
    else:
        body["sections"]["主要结果"] = "{{fact:F0001}}"
    with pytest.raises(ValueError):
        validate_draft(PaperDraft.model_validate(body), context)


async def test_output_rechecks_hash_after_writing(
    ready: RunStore, paper_transport: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ready.selected_results()[0]
    assert result.result_id
    artifact = ready.artifact(result.result_id)
    monkeypatch.setattr(
        PaperTransport,
        "mutate",
        staticmethod(lambda: (ready.directory / artifact.relative_path).write_text("篡改")),
    )
    report = await generate_report(ready)
    assert report["status"] == "partial"
    assert (
        json.loads(ready.read_artifact(report["files"]["tables.json"]["artifact_id"]))["rows"] == []
    )
    assert current_report(ready) is None
    assert ready.load_state().status == RunStatus.PAUSED


async def test_no_model_and_exhausted_budget_do_not_invent_complete_paper(
    ready: RunStore, paper_transport: None
) -> None:
    report = await generate_report(ready, use_model=False)
    assert report["status"] == "partial"
    assert ready.load_state().status == RunStatus.PAUSED
    assert PaperTransport.requests == 0
    assert json.loads(ready.read_artifact(report["files"]["tables.json"]["artifact_id"]))["rows"]
    assert current_report(ready) == report


async def test_export_is_readonly_excludes_full_model_records_and_backups(
    ready: RunStore, paper_transport: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await generate_report(ready)
    ready.archive_bytes(
        "inputs/protection.json", '{"backup":"秘密备份"}'.encode(), kind="input_manifest"
    )
    database = ready.directory / "state.sqlite"
    ready.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setenv("ECON_AGENT_MODEL_URL", "bad-model-url")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with RunStore.open(database.parent, writable=False) as readonly:
        destination = export_run(readonly, tmp_path / "output")
        manifest = json.loads((destination / "bundle_manifest.json").read_text())
        calls: list[dict[str, Any]] = [
            json.loads(line) for line in (destination / "calls.jsonl").read_text().splitlines()
        ]
        assert len(calls) == len(readonly.model_calls()) + len(readonly.tool_calls())
        assert json.loads((destination / "usage.json").read_text())["total_tokens"] == {
            "known_sum": None,
            "missing_calls": 1,
        }
        assert all(
            "model_request" not in path and "/calls/model/" not in path and "protection" not in path
            for path in manifest["files"]
        )
        assert "秘密备份" not in "".join(
            p.read_text(errors="ignore") for p in destination.rglob("*") if p.is_file()
        )
        assert "paper.md" in manifest["files"]
        for name, reference in manifest["files"].items():
            assert (
                hashlib.sha256((destination / name).read_bytes()).hexdigest() == reference["sha256"]
            )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert PaperTransport.requests == 1
    assert main(["export", database.parent.name, "--workspace", str(tmp_path)]) == 0
    assert PaperTransport.requests == 1


async def test_changed_design_blocks_old_report_and_keeps_full_analysis_history(
    ready: RunStore, paper_transport: None, tmp_path: Path
) -> None:
    await generate_report(ready)
    # 新版设计尚未确认，因此旧报告不能进入当前正文；历史原始结果仍可追溯。
    ready.begin_report_generation()
    old = ready.design(ready.load_state().current_design_id or "")
    new = ready.create_design(
        old.content.model_copy(update={"sample_rule": "新样本"}), change_reason="合成修改"
    )
    ready.request_human(new.design_id, "脚本化测试确认点")
    assert current_report(ready) is None
    destination = export_run(ready, tmp_path / "output")
    assert "样本量为2" not in (destination / "paper.md").read_text()
    history = (destination / "analysis_history.csv").read_text(encoding="utf-8-sig")
    assert "experiment_1" in history
    assert json.loads((destination / "bundle_manifest.json").read_text())["status"] == "partial"


def test_bad_specification_cannot_supply_current_numbers(ready: RunStore, tmp_path: Path) -> None:
    result = ready.selected_results()[0]
    assert result.result_id
    ready.save_validation(
        result.result_id, checks=checks(result.result_id, specification_status="fail")
    )
    with pytest.raises(StateConflict):
        snapshot(ready)
    destination = export_run(ready, tmp_path / "output")
    assert json.loads((destination / "tables.json").read_text())["rows"] == []
    assert "必需核验" in (destination / "analysis_history.csv").read_text(encoding="utf-8-sig")


def test_parallel_time_union_and_unknown_tokens(ready: RunStore) -> None:
    calls: list[dict[str, Any]] = [
        {
            "kind": "model",
            "started_at": "2026-10-10T00:00:00+00:00",
            "ended_at": "2026-10-10T00:00:10+00:00",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_tokens": None,
            "reasoning_tokens": None,
        },
        {
            "kind": "model",
            "started_at": "2026-10-10T00:00:05+00:00",
            "ended_at": "2026-10-10T00:00:15+00:00",
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 7,
            "cached_tokens": None,
            "reasoning_tokens": None,
        },
    ]
    summary = usage_summary(ready, calls)
    assert summary["known_request_active_seconds"] == 15
    assert summary["total_tokens"] == {"known_sum": 7, "missing_calls": 1}
    assert summary["reasoning_tokens"] == {"known_sum": None, "missing_calls": 2}


async def test_invalid_paper_is_parse_failure_without_retry(
    ready: RunStore, paper_transport: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PaperTransport.complete

    async def invalid(self: PaperTransport, request: dict[str, Any]) -> dict[str, Any]:
        response = await original(self, request)
        body = json.loads(response["choices"][0]["message"]["content"])
        body["claims"][0]["text"] += "附加系数999。"
        response["choices"][0]["message"]["content"] = json.dumps(body, ensure_ascii=False)
        return response

    monkeypatch.setattr(PaperTransport, "complete", invalid)
    report = await generate_report(ready)
    assert report["status"] == "partial" and PaperTransport.requests == 1
    assert ready.call_summary()["model_parse_failures"] == 1
    assert ready.call_summary()["model_transport_failures"] == 0
    assert ready.call_summary()["model_retries"] == 0
    assert "999" not in ready.read_artifact(report["files"]["paper.md"]["artifact_id"]).decode()


def test_missing_original_input_exports_partial_without_current_digits(
    ready: RunStore, tmp_path: Path
) -> None:
    ready.load_task().data_path.unlink()
    destination = export_run(ready, tmp_path / "output")
    assert json.loads((destination / "tables.json").read_text())["rows"] == []
    assert json.loads((destination / "bundle_manifest.json").read_text())["status"] == "partial"
    assert list((destination / "sources/inputs").glob("*.csv"))


def test_insignificant_and_negative_success_not_filtered(ready: RunStore) -> None:
    from tests.contract.test_state import normalized

    attempt = ready.selected_results()[0].attempt_id
    result = ready.save_result(attempt, normalized(-1.5))
    ready.save_validation(result.artifact_id, checks=checks(result.artifact_id))
    ready.select_result(ready.result(result.artifact_id).experiment_id, result.artifact_id)
    tables = result_tables(ready, ready.selected_results())
    assert tables["rows"][1]["value"] == -1.5
    assert tables["rows"][1]["result_id"] == result.artifact_id


async def test_real_budget_exhaustion_retains_partial_tables(
    ready: RunStore, paper_transport: None
) -> None:
    from econ_agent.config import ModelConfig
    from econ_agent.model_client import PersistentModelClient
    from econ_agent.state import new_id
    from tests.unit.test_model_client import SequenceTransport, envelope

    ready.begin_report_generation()
    budget = ready.load_state().budget.max_model_calls
    transport = SequenceTransport([envelope() for _ in range(budget)])
    client = PersistentModelClient(
        ready, ModelConfig("https://example.invalid", "测试", "TEST_KEY"), transport
    )
    for _ in range(budget):
        await client.decide(
            context="仅用于耗尽调用预算的离线夹具", operation_id=new_id("operation")
        )
    ready.transition_run(RunStatus.PAUSED)
    report = await generate_report(ready)
    assert report["status"] == "partial"
    assert "预算已耗尽" in report["issue"]
    assert PaperTransport.requests == 0
    assert len(ready.model_calls()) == budget


async def test_unpublished_report_is_not_complete_and_time_survives_failure(
    ready: RunStore, paper_transport: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from econ_agent.reporting import writing_seconds

    archive = ready.archive_bytes

    def interrupted(name: str, payload: bytes, **kwargs: Any) -> Any:
        if name.endswith("/manifest.json"):
            raise OSError("模拟发布清单前的磁盘错误")
        return archive(name, payload, **kwargs)

    monkeypatch.setattr(ready, "archive_bytes", interrupted)
    with pytest.raises(OSError):
        await generate_report(ready)
    assert ready.load_state().status == RunStatus.PAUSED
    assert current_report(ready) is None
    assert writing_seconds(ready) > 0
    with pytest.raises(StateConflict):
        ready.transition_run(RunStatus.COMPLETED)


async def test_writing_time_is_accounted_once_across_resume(
    ready: RunStore, paper_transport: None
) -> None:
    from econ_agent.reporting import elapsed_seconds, json_bytes, writing_seconds

    await generate_report(ready)
    paid = writing_seconds(ready)
    ready.archive_bytes(
        "calls/harness/step_elapsed_test.json",
        json_bytes({"elapsed_seconds": 10 + paid, "writing_seconds_accounted": paid}),
        kind="tool_response",
    )
    assert elapsed_seconds(ready) == pytest.approx(10 + paid)
    await generate_report(ready)
    total = writing_seconds(ready)
    assert total > paid
    assert elapsed_seconds(ready) == pytest.approx(10 + total)


def test_damaged_current_result_exports_other_sources_and_explicit_exclusion(
    ready: RunStore, tmp_path: Path
) -> None:
    result = ready.selected_results()[0]
    assert result.result_id
    artifact = ready.artifact(result.result_id)
    (ready.directory / artifact.relative_path).write_text("篡改的文件")
    destination = export_run(ready, tmp_path / "output")
    manifest = json.loads((destination / "bundle_manifest.json").read_text())
    assert manifest["status"] == "partial" and not manifest["current_result_ids"]
    assert any(
        e["artifact_id"] == result.result_id and "核验失败" in e["reason"]
        for e in manifest["exclusions"]
    )
    assert list((destination / "sources/inputs").glob("*.csv"))
    assert json.loads((destination / "tables.json").read_text())["rows"] == []


async def test_latest_bad_paper_cannot_fallback_and_events_keep_references(
    ready: RunStore, paper_transport: None, tmp_path: Path
) -> None:
    first = await generate_report(ready)
    latest = await generate_report(ready)
    artifact = ready.artifact(latest["files"]["paper.md"]["artifact_id"])
    (ready.directory / artifact.relative_path).write_text("篡改的论文")
    assert current_report(ready) is None
    destination = export_run(ready, tmp_path / "output")
    manifest = json.loads((destination / "bundle_manifest.json").read_text())
    assert manifest["status"] == "partial" and manifest["report_id"] is None
    assert "样本量为2" not in (destination / "paper.md").read_text()
    assert ready.read_artifact(first["files"]["paper.md"]["artifact_id"])
    events = json.loads((destination / "events.json").read_text())
    assert any(e["model_call_id"] == ready.model_calls()[0].model_call_id for e in events)
    assert any(e["artifact_id"] == first["files"]["paper.md"]["artifact_id"] for e in events)
    assert (destination / "experiments.json").is_file()


def test_export_cannot_pollute_source_run(ready: RunStore) -> None:
    before = set(ready.directory.iterdir())
    with pytest.raises(ValueError, match="源运行目录"):
        export_run(ready, ready.directory.parent)
    assert set(ready.directory.iterdir()) == before
