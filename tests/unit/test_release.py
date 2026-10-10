"""发布边界的负例；不运行打包器、网络或外部服务。"""

import pytest

from scripts.build_release import safe_name, safe_source, secrets_absent


@pytest.mark.parametrize(
    "name",
    [
        "../secret",
        "/root/a",
        "local_reference/stata18/a",
        "examples/demo/data/author.dta",
        "runs/a.db",
        ".env",
        "02_外部候选人/题目.md",
        "x/stata.lic",
        "x/raw.log",
    ],
)
def test_release_rejects_private_or_unsafe_members(name: str) -> None:
    with pytest.raises(ValueError):
        safe_name(name)


def test_source_requires_whitelist_and_tracked_members() -> None:
    safe_source("README.md", {"README.md"})
    with pytest.raises(ValueError):
        safe_source("README.md", set())
    with pytest.raises(ValueError):
        safe_source("secret.txt", {"secret.txt"})


def test_release_rejects_credentials_and_pem_but_allows_detection_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELEASE_TEST_API_KEY", "仅供测试的长占位符密钥123456789")
    with pytest.raises(ValueError):
        secrets_absent("仅供测试的长占位符密钥123456789".encode())
    with pytest.raises(ValueError):
        secrets_absent(b"-----BEGIN RSA PRIVATE KEY-----\n")
    secrets_absent(b'pattern = b"-----BEGIN PRIVATE KEY-----"')
