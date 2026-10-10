"""基础兼容模型客户端：逐次请求、中文动作、脱敏归档和独立的解析状态。"""

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol, cast

from openai import APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from econ_agent.config import ModelConfig
from econ_agent.schemas import (
    ACTION_ADAPTER,
    MODEL_SYSTEM_PROMPT_ZH,
    CallStatus,
    ModelCall,
    ModelFailure,
    ModelResponse,
    ParseStatus,
    utc_now,
)
from econ_agent.state import RunStore, new_id


class ModelBudgetExceeded(RuntimeError):
    """请求未发送，不伪造一次模型调用或零用量。"""


class CompletionTransport(Protocol):
    async def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...


class OpenAITransport:
    """仅使用基础 Chat Completions；禁用 SDK 隐式重试。"""

    def __init__(self, config: ModelConfig) -> None:
        key = config.api_key()
        if not key:
            raise ValueError("未找到模型凭证，请配置既有密钥环境变量")
        self.client = AsyncOpenAI(api_key=key, base_url=config.base_url, max_retries=0)

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.chat.completions.create(**request)
        raw = response.model_dump(mode="json")
        raw["server_request_id"] = getattr(response, "_request_id", None)
        return cast(dict[str, Any], raw)

    async def close(self) -> None:
        await self.client.close()


def redact(value: Any, secrets: tuple[str, ...] | None = None) -> Any:
    """凭证字段、认证文本及环境中实际秘密不进入请求、响应或错误产物。"""
    if secrets is None:
        secrets = tuple(
            value
            for name, value in os.environ.items()
            if re.search(r"KEY|TOKEN|PASSWORD|SECRET", name, re.I) and value
        )
    if isinstance(value, Mapping):
        return {
            str(key): "[已脱敏]"
            if re.search(
                r"api.?key|authorization|password|secret|access.?token|cookie", str(key), re.I
            )
            else redact(child, secrets)
            for key, child in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[已脱敏]")
        return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [已脱敏]", value)
    return value


def _tokens(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_fields(raw: dict[str, Any] | None) -> dict[str, Any]:
    """缺失保持空值，缓存和推理子项不重复加到总用量。"""
    usage = raw or {}
    prompt = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    return {
        "raw_usage": raw,
        "input_tokens": _tokens(usage.get("prompt_tokens")),
        "output_tokens": _tokens(usage.get("completion_tokens")),
        "total_tokens": _tokens(usage.get("total_tokens")),
        "cached_tokens": _tokens(usage.get("prompt_cache_hit_tokens", prompt.get("cached_tokens")))
        if isinstance(prompt, dict)
        else None,
        "reasoning_tokens": _tokens(completion.get("reasoning_tokens"))
        if isinstance(completion, dict)
        else None,
    }


class PersistentModelClient:
    def __init__(
        self,
        store: RunStore,
        config: ModelConfig,
        transport: CompletionTransport,
        *,
        timeout_seconds: float = 60,
        max_transport_retries: int = 1,
        max_output_tokens: int = 2500,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("模型超时必须为有限正数")
        if not 0 <= max_transport_retries <= 2 or max_output_tokens < 1:
            raise ValueError("模型重试及输出预算超出允许范围")
        self.store, self.config, self.transport = store, config, transport
        self.timeout_seconds = timeout_seconds
        self.max_transport_retries = max_transport_retries
        self.max_output_tokens = max_output_tokens
        self.secrets = tuple(
            value
            for name, value in os.environ.items()
            if re.search(r"KEY|TOKEN|PASSWORD|SECRET", name, re.I) and value
        )
        if config.api_key() and config.api_key() not in self.secrets:
            self.secrets += (config.api_key() or "",)

    async def decide(
        self,
        *,
        context: str,
        operation_id: str,
        context_design_id: str | None = None,
        context_result_ids: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
    ) -> ModelResponse:
        allowance = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        if not math.isfinite(allowance) or allowance <= 0:
            raise ModelBudgetExceeded("模型请求没有剩余运行时间")
        deadline = time.monotonic() + allowance
        last: ModelResponse | None = None
        for retry in range(self.max_transport_retries + 1):
            if len(self.store.model_calls()) >= self.store.load_state().budget.max_model_calls:
                if last is not None:
                    return last
                raise ModelBudgetExceeded("模型调用预算已耗尽，请求未发送")
            remaining = min(self.timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                if last is not None:
                    return last
                raise ModelBudgetExceeded("模型请求没有剩余运行时间")
            call = ModelCall(
                model_call_id=new_id("model"),
                run_id=self.store.run_id,
                operation_id=operation_id,
                retry_no=retry,
                purpose="选择下一项研究动作",
                provider=self.config.base_url,
                requested_model=self.config.model,
                prompt_version="research-loop-zh-1",
                config_version="model-compatible-1",
                context_design_id=context_design_id,
                context_result_ids=context_result_ids,
            )
            self.store.record_model_call(call)
            request = redact(
                {
                    "model": self.config.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": MODEL_SYSTEM_PROMPT_ZH
                            + "每次只返回一个动作，必须遵守上下文工具列表及预算。"
                            "返回动作实例，不要复制 JSON Schema；顶层只有 action、reason、arguments"
                            "（schema_version 可省略），"
                            "禁止 type、properties、$defs 等 schema 字段。\n",
                        },
                        {"role": "user", "content": context},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": self.max_output_tokens,
                },
                self.secrets,
            )
            prefix = f"calls/model/{call.model_call_id}"
            artifact = self.store.archive_bytes(
                f"{prefix}/request.json",
                json.dumps(request, ensure_ascii=False).encode(),
                kind="model_request",
                model_call_id=call.model_call_id,
            )
            call = call.model_copy(update={"request_artifact_id": artifact.artifact_id})
            self.store.update_model_call(call, expected=CallStatus.PENDING)
            call = call.model_copy(update={"status": CallStatus.RUNNING, "started_at": utc_now()})
            self.store.update_model_call(call, expected=CallStatus.PENDING)
            start = time.monotonic()
            try:
                async with asyncio.timeout(remaining):
                    raw = redact(await self.transport.complete(request), self.secrets)
            except (Exception, asyncio.CancelledError) as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                category: Literal["timeout", "transport"] = (
                    "timeout" if isinstance(exc, (TimeoutError, APITimeoutError)) else "transport"
                )
                message = (
                    "模型请求已取消，服务端处理与用量未知"
                    if cancelled
                    else (
                        "模型请求超时，服务端处理与用量未知"
                        if category == "timeout"
                        else "模型传输失败，请查阅脱敏诊断"
                    )
                )
                diagnostic = {
                    "错误": message,
                    "exception_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                }
                response = self.store.archive_bytes(
                    f"{prefix}/response.json",
                    json.dumps(diagnostic, ensure_ascii=False).encode(),
                    kind="model_response",
                    model_call_id=call.model_call_id,
                )
                call = call.model_copy(
                    update={
                        "status": CallStatus.CANCELLED if cancelled else CallStatus.FAILED,
                        "ended_at": utc_now(),
                        "duration_seconds": time.monotonic() - start,
                        "parse_status": ParseStatus.NOT_APPLICABLE,
                        "response_artifact_id": response.artifact_id,
                        "error_category": "cancelled" if cancelled else category,
                        "error_summary": message,
                    }
                )
                self.store.update_model_call(call, expected=CallStatus.RUNNING)
                if cancelled:
                    raise
                last = ModelResponse(
                    model_call=call,
                    error=ModelFailure(
                        category=category,
                        message=message,
                        retryable=True,
                    ),
                )
                if retry < self.max_transport_retries:
                    continue
                return last
            response = self.store.archive_bytes(
                f"{prefix}/response.json",
                json.dumps(raw, ensure_ascii=False).encode(),
                kind="model_response",
                model_call_id=call.model_call_id,
            )
            raw_usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
            call = call.model_copy(
                update={
                    "status": CallStatus.SUCCEEDED,
                    "ended_at": utc_now(),
                    "duration_seconds": time.monotonic() - start,
                    "response_artifact_id": response.artifact_id,
                    "returned_model": raw.get("model")
                    if isinstance(raw.get("model"), str)
                    else None,
                    "server_request_id": raw.get("server_request_id")
                    if isinstance(raw.get("server_request_id"), str)
                    else None,
                    **usage_fields(raw_usage),
                }
            )
            self.store.update_model_call(call, expected=CallStatus.RUNNING)
            parse_category: Literal["response_json", "response_schema"] = "response_json"
            try:
                content = raw["choices"][0]["message"]["content"]
                decoded = json.loads(content)
                parse_category = "response_schema"
                action = ACTION_ADAPTER.validate_python(decoded)
            except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
                message = (
                    "模型响应不是有效 JSON 动作"
                    if parse_category == "response_json"
                    else ("模型动作不满足契约，未执行任何工具；请依据工具列表修正字段")
                )
                if isinstance(exc, ValidationError):
                    details = []
                    for issue in exc.errors(include_input=False, include_url=False)[:5]:
                        location = ".".join(str(part) for part in issue["loc"])
                        explanation = {
                            "extra_forbidden": "不允许额外字段，请删除",
                            "missing": "缺少必需字段",
                            "union_tag_invalid": "动作名不在支持列表内",
                        }.get(issue["type"], "字段类型或取值不满足契约")
                        details.append(f"{location}：{explanation}")
                    message += "；" + redact("；".join(details), self.secrets)
                call = call.model_copy(
                    update={
                        "parse_status": ParseStatus.INVALID,
                        "error_category": parse_category,
                        "error_summary": message,
                    }
                )
                self.store.update_model_call(call, expected=CallStatus.SUCCEEDED)
                return ModelResponse(
                    model_call=call,
                    error=ModelFailure(
                        category=parse_category,
                        message=message,
                        retryable=False,
                    ),
                )
            call = call.model_copy(update={"parse_status": ParseStatus.VALID})
            self.store.update_model_call(call, expected=CallStatus.SUCCEEDED)
            return ModelResponse(model_call=call, action=action)
        raise AssertionError("有界模型循环必须返回明确结果")
