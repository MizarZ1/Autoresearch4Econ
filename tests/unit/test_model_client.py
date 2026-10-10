"""离线替代 HTTP 边界，校验生产模型适配器的逐次记录及凭证脱敏。"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from econ_agent.config import ModelConfig
from econ_agent.model_client import (
    ModelBudgetExceeded,
    OpenAITransport,
    PersistentModelClient,
    usage_fields,
)
from econ_agent.schemas import Budget, CallStatus, ParseStatus, ResearchTask, RunStatus
from econ_agent.state import RunStore, new_id

FINISH: dict[str, Any] = {
    "action": "finish_research",
    "reason": "测试停止",
    "arguments": {
        "stop_reason": "测试已完成",
        "conclusion_draft": "离线夹具不表示真实研究结论",
    },
}


def envelope(content: object = FINISH, *, usage: object = None) -> dict[str, Any]:
    return {
        "model": "返回测试模型",
        "server_request_id": "request-test",
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": usage,
    }


class SequenceTransport:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        if value == "wait":
            await asyncio.Event().wait()
        assert isinstance(value, dict)
        return value


@pytest.fixture
def model_store(tmp_path: Path) -> Iterator[RunStore]:
    data = tmp_path / "data.csv"
    data.write_text("y,x\n1,2\n")
    with RunStore.create(
        tmp_path / "runs",
        ResearchTask(
            data_path=data,
            research_goal="合成模型请求测试",
            budget=Budget(max_model_calls=4),
        ),
    ) as store:
        store.transition_run(RunStatus.RUNNING)
        yield store


def client(store: RunStore, transport: SequenceTransport, **options: Any) -> PersistentModelClient:
    return PersistentModelClient(
        store, ModelConfig("https://example.invalid", "测试", "TEST_KEY"), transport, **options
    )


async def test_success_unknown_usage_and_exact_request(model_store: RunStore) -> None:
    transport = SequenceTransport([envelope()])
    response = await client(model_store, transport).decide(
        context="中文请求", operation_id=new_id("op")
    )
    assert response.action is not None
    call = response.model_call
    assert call.status == CallStatus.SUCCEEDED and call.parse_status == ParseStatus.VALID
    assert call.total_tokens is None and call.raw_usage is None
    assert call.request_artifact_id and call.response_artifact_id
    request = json.loads(model_store.read_artifact(call.request_artifact_id))
    assert request == transport.requests[0]
    assert request["messages"][1]["content"] == "中文请求"
    assert call.returned_model == "返回测试模型" and call.server_request_id == "request-test"
    assert model_store.call_summary()["missing_total_usage_calls"] == 1


@pytest.mark.parametrize(
    "content,category",
    [("not json", "response_json"), ({"action": "发明的工具"}, "response_schema")],
)
async def test_http_success_invalid_action_is_not_transport_failure(
    model_store: RunStore,
    content: object,
    category: str,
) -> None:
    raw = envelope(content)
    if isinstance(content, str):
        raw["choices"][0]["message"]["content"] = content
    usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
    raw["usage"] = usage
    transport = SequenceTransport([raw])
    result = await client(model_store, transport).decide(context="中文", operation_id=new_id("op"))
    assert result.action is None and result.error and result.error.category == category
    assert result.model_call.status == CallStatus.SUCCEEDED
    assert result.model_call.parse_status == ParseStatus.INVALID
    assert result.model_call.total_tokens == 13 and len(transport.requests) == 1
    assert not model_store.tool_calls()


async def test_observed_transport_retry_keeps_both_calls_and_usage(model_store: RunStore) -> None:
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
        "prompt_cache_hit_tokens": 5,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    transport = SequenceTransport([RuntimeError("不保存异常正文"), envelope(usage=usage)])
    operation = new_id("op")
    result = await client(model_store, transport).decide(context="中文", operation_id=operation)
    calls = model_store.model_calls()
    assert len(calls) == 2 and [c.retry_no for c in calls] == [0, 1]
    assert all(c.operation_id == operation for c in calls)
    assert calls[0].status == CallStatus.FAILED and calls[0].total_tokens is None
    assert result.model_call.cached_tokens == 5 and result.model_call.reasoning_tokens == 2
    assert model_store.call_summary()["known_total_tokens"] == 16
    assert model_store.call_summary()["missing_total_usage_calls"] == 1


async def test_timeout_is_recorded_and_does_not_invent_zero_usage(model_store: RunStore) -> None:
    transport = SequenceTransport(["wait"])
    result = await client(
        model_store, transport, timeout_seconds=0.01, max_transport_retries=0
    ).decide(context="中文", operation_id=new_id("op"))
    assert result.error and result.error.category == "timeout"
    assert result.model_call.status == CallStatus.FAILED
    assert result.model_call.parse_status == ParseStatus.NOT_APPLICABLE
    assert result.model_call.raw_usage is None and result.model_call.total_tokens is None


async def test_explicit_cancellation_is_saved_then_propagated(model_store: RunStore) -> None:
    transport = SequenceTransport(["wait"])
    work = asyncio.create_task(
        client(model_store, transport).decide(context="中文", operation_id=new_id("op"))
    )
    while not transport.requests:
        await asyncio.sleep(0)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert model_store.model_calls()[0].status == CallStatus.CANCELLED
    assert model_store.call_summary()["unfinished_model_calls"] == 0


async def test_credentials_redacted_in_request_echo_and_exception(
    model_store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "合成凭证不能泄漏-12345"
    monkeypatch.setenv("TEST_KEY", secret)
    raw = envelope()
    raw.update(authorization="Bearer " + secret, diagnostic=secret)
    raw["choices"][0]["message"]["content"] = json.dumps(FINISH).replace("测试停止", secret)
    transport = SequenceTransport([RuntimeError(secret), raw])
    result = await client(model_store, transport).decide(
        context="材料可能回显 " + secret, operation_id=new_id("op")
    )
    assert result.action is not None
    for artifact in model_store.artifacts():
        assert secret.encode() not in model_store.read_artifact(artifact.artifact_id)
    assert secret not in json.dumps([c.model_dump() for c in model_store.model_calls()])
    assert secret not in json.dumps(transport.requests, ensure_ascii=False)


async def test_budget_prevents_unobserved_extra_http_request(model_store: RunStore) -> None:
    transport = SequenceTransport([envelope()] * 4)
    adapter = client(model_store, transport)
    for _ in range(4):
        await adapter.decide(context="中文", operation_id=new_id("op"))
    with pytest.raises(ModelBudgetExceeded):
        await adapter.decide(context="中文", operation_id=new_id("op"))
    assert len(model_store.model_calls()) == len(transport.requests) == 4


def test_usage_is_not_summed_or_coerced() -> None:
    usage = {
        "prompt_tokens": True,
        "completion_tokens": -1,
        "total_tokens": 8,
        "prompt_tokens_details": {"cached_tokens": 3},
    }
    fields = usage_fields(usage)
    assert fields["input_tokens"] is None and fields["output_tokens"] is None
    assert fields["total_tokens"] == 8 and fields["cached_tokens"] == 3
    assert fields["reasoning_tokens"] is None and fields["raw_usage"] == usage


def test_sdk_implicit_retries_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY", "合成凭证测试")
    adapter = OpenAITransport(ModelConfig("https://example.invalid", "测试", "TEST_KEY"))
    assert adapter.client.max_retries == 0
    asyncio.run(adapter.close())


async def test_extra_schema_field_feedback_identifies_rejected_field(model_store: RunStore) -> None:
    raw = dict(FINISH) | {"type": "object"}
    result = await client(model_store, SequenceTransport([envelope(raw)])).decide(
        context="中文", operation_id=new_id("op")
    )
    assert result.action is None and result.error
    assert "type" in result.error.message and "删除" in result.error.message
    assert "input" not in result.error.message
