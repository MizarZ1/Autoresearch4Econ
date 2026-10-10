"""显式模型接口探测；发送三次小型中文请求，不发送研究数据。"""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from econ_agent.config import ModelConfig


class ProbeAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["finish"]
    message: str


async def main() -> int:
    config = ModelConfig.from_environment()
    output = Path("local_reference/m0/model_probe.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": config.base_url,
        "requested_model": config.model,
        "sdk_retries": 0,
        "calls": [],
    }
    if not config.api_key():
        record.update(status="unverified", reason="缺少模型密钥")
        output.write_text(json.dumps(record, indent=2) + "\n")
        return 1
    async with AsyncOpenAI(
        api_key=config.api_key(), base_url=config.base_url, max_retries=0, timeout=30
    ) as client:

        async def call(index: int) -> dict[str, Any]:
            item: dict[str, Any] = {"index": index, "start": time.monotonic()}
            try:
                response = await client.chat.completions.create(
                    model=config.model,
                    messages=[
                        {
                            "role": "system",
                            "content": '只返回 JSON：{"action":"finish","message":"验证通过"}。',
                        },
                        {
                            "role": "user",
                            "content": f"环境探测第 {index} 次；请用中文返回规定的 JSON。",
                        },
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=128,
                )
                action = ProbeAction.model_validate_json(response.choices[0].message.content or "")
                item.update(
                    status="passed",
                    returned_model=response.model,
                    action=action.model_dump(),
                    usage=response.usage.model_dump() if response.usage else None,
                )
            except Exception as exc:
                # 异常正文可能包含凭证，只持久化异常类型和 HTTP 状态。
                item.update(
                    status="failed",
                    error_type=type(exc).__name__,
                    http_status=getattr(exc, "status_code", None),
                )
            item["end"] = time.monotonic()
            return item

        record["calls"].append(await call(1))
        concurrent = await asyncio.gather(call(2), call(3))
        record["calls"].extend(concurrent)
        record["intervals_overlap"] = max(i["start"] for i in concurrent) < min(
            i["end"] for i in concurrent
        )
    record["status"] = (
        "passed"
        if all(i["status"] == "passed" for i in record["calls"]) and record["intervals_overlap"]
        else "failed"
    )
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
