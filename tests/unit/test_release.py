"""发布边界的负例；不运行打包器、网络或外部服务。"""

import pytest

from scripts.build_release import safe_name, safe_source


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
