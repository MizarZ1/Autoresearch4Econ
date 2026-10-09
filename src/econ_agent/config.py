"""Non-secret configuration; credentials remain in the process environment."""

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
            raise ValueError("Model endpoint must be an HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Model endpoint must not contain credentials, query, or fragment")
        if not config.model or not config.key_variable:
            raise ValueError("Model name and key variable must not be empty")
        return config

    def api_key(self) -> str | None:
        return os.getenv(self.key_variable) or None
