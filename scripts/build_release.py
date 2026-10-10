"""从明确白名单构建候选包，审计成员后生成交付包与 SHA256 清单。"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TAG = "demoV0.1-test"


def execute(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True)


def safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("包成员路径不安全")
    forbidden = {"local_reference", "runs", "backups", "output", "tmp", ".git", ".venv"}
    if forbidden.intersection(path.parts) or any(part.startswith("02_外部") for part in path.parts):
        raise ValueError("包中出现本地保留目录")
    if path.name.startswith(".env") and path.name != ".env.example":
        raise ValueError("包中出现环境秘密文件")
    if path.suffix.lower() in {".dta", ".db", ".sqlite", ".log", ".dylib", ".exe", ".lic"}:
        raise ValueError("包中出现数据、日志、数据库或厂商文件")


def safe_source(name: str, tracked: set[str]) -> None:
    safe_name(name)
    includes = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"][
        "targets"
    ]["sdist"]["include"]
    if name not in tracked or not any(
        name == prefix.lstrip("/") or name.startswith(prefix.lstrip("/") + "/")
        for prefix in includes
    ):
        raise ValueError("源码包出现非白名单或未提交文件：" + name)


def secrets_absent(content: bytes) -> None:
    values = {
        value.encode()
        for key, value in os.environ.items()
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "PASSWORD"))
        and len(value) >= 12
    }
    if any(value in content for value in values) or re.search(
        rb"(?m)^-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----\r?$", content
    ):
        raise ValueError("包中检出当前凭证或私钥；禁止发布")


def archive_files(path: Path, tracked: set[str]) -> dict[str, bytes]:
    """审计并返回归一化成员；拒绝链接和重复路径。"""
    result: dict[str, bytes] = {}
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                name = member.filename
                safe_name(name)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("wheel 中出现符号链接")
                if name.startswith("econ_agent/"):
                    safe_source("src/" + name, tracked)
                elif not name.startswith("econ_agent-0.1.0rc1.dist-info/"):
                    raise ValueError("wheel 中出现非项目成员")
                if name in result:
                    raise ValueError("重复包成员")
                result[name] = archive.read(member)
    else:
        with tarfile.open(path) as archive:
            for tar_member in archive.getmembers():
                if tar_member.isdir():
                    continue
                if not tar_member.isfile():
                    raise ValueError("源码包中出现链接或特殊文件")
                safe_name(tar_member.name)
                name = tar_member.name.split("/", 1)[1]
                if name != "PKG-INFO":
                    safe_source(name, tracked)
                stream = archive.extractfile(tar_member)
                assert stream is not None
                if name in result:
                    raise ValueError("重复包成员")
                result[name] = stream.read()
    for content in result.values():
        secrets_absent(content)
    return result


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="白名单构建候选交付包")
    parser.add_argument("--output", type=Path, default=ROOT / "output/releases" / TAG)
    parser.add_argument(
        "--finalize", action="store_true", help="附加已通过的确切安装验收并更新清单"
    )
    args = parser.parse_args()
    if execute("git", "status", "--porcelain").strip():
        raise ValueError("工作区须提交并保持干净后构建正式候选包")
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    tracked = set(execute("git", "ls-files").splitlines())
    if not args.finalize:
        execute("uv", "build", "--offline", "--out-dir", str(destination))
    wheel = destination / "econ_agent-0.1.0rc1-py3-none-any.whl"
    source = destination / "econ_agent-0.1.0rc1.tar.gz"
    contents = {p.name: archive_files(p, tracked) for p in (wheel, source)}
    if "econ_agent/method_validation.json" not in contents[wheel.name]:
        raise ValueError("wheel 缺少方法核验资源")
    requirements = destination / "requirements-stata.txt"
    execute(
        "uv",
        "export",
        "--locked",
        "--offline",
        "--extra",
        "stata",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--output-file",
        str(requirements),
    )
    secrets_absent(requirements.read_bytes())
    installation = destination / "installation-validation.json"
    if args.finalize:
        proof = json.loads(installation.read_text())
        old = json.loads((destination / "release-manifest.json").read_text())
        if (
            old["commit"] != execute("git", "rev-parse", "HEAD").strip()
            or proof.get("status") != "passed"
            or proof.get("wheel_sha256") != digest(wheel)
            or proof.get("sdist_sha256") != digest(source)
        ):
            raise ValueError("安装验收与当前候选提交或附件不匹配")
        secrets_absent(installation.read_bytes())
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "tag": TAG,
        "application_version": "0.1.0rc1",
        "commit": execute("git", "rev-parse", "HEAD").strip(),
        "worktree_clean": True,
        "analysis_backend": "stata",
        "docker": False,
        "remote_ci": "skipped_by_researcher",
        "independent_review": "pending",
        "merge": "not_merged",
        "original_data_included": False,
        "vendor_binary_included": False,
        "dependencies_bundled": False,
        "installation_validation": "passed" if args.finalize else "pending",
        "installation_validation_sha256": digest(installation) if args.finalize else None,
        "archives": {
            p.name: {"sha256": digest(p), "members": sorted(contents[p.name])}
            for p in (wheel, source)
        },
        "requirements_sha256": digest(requirements),
    }
    manifest_path = destination / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    delivery = destination / (TAG + "-delivery.zip")
    with zipfile.ZipFile(delivery, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents[source.name].items():
            archive.writestr(TAG + "/" + name, content)
        extra = (installation,) if args.finalize else ()
        for file in (wheel, requirements, manifest_path, *extra):
            archive.write(file, TAG + "/" + file.name)
    assets = (wheel, source, delivery, requirements, manifest_path, *extra)
    (destination / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.name}\n" for p in assets))
    print("候选包已构建并通过白名单与凭证检查：" + str(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
