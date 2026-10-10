"""非敏感配置；凭证只从进程环境读取。"""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    key_variable: str

    @classmethod
    def from_environment(cls) -> "ModelConfig":
        config = cls(
            base_url=os.getenv("ECON_AGENT_MODEL_URL", "https://api.deepseek.com"),
            model=os.getenv("ECON_AGENT_MODEL", "deepseek-chat"),
            key_variable=os.getenv("ECON_AGENT_KEY_VARIABLE", "DEEPSEEK_API_KEY"),
        )
        parsed = urlsplit(config.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("模型端点必须是 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型端点不能包含凭证、查询参数或片段")
        if not config.model or not config.key_variable:
            raise ValueError("模型名与密钥环境变量名不能为空")
        return config

    def api_key(self) -> str | None:
        return os.getenv(self.key_variable) or None
