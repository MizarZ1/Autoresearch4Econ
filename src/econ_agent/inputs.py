"""原件只读、独立备份、不可变快照及每次尝试的工作副本。"""

import os
from pathlib import Path

from econ_agent.stata_contracts import PreparedInput
from econ_agent.state import ArtifactIntegrityError, RunStore, file_hash


def _exclusive_copy(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # 不覆盖任何既有备份或副本，包括调用者提供的同名文件。
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(1024 * 1024):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    if file_hash(target) != expected_hash or file_hash(source) != expected_hash:
        raise ArtifactIntegrityError("复制期间输入改变，保留现有文件并暂停排查")


def prepare_input(store: RunStore, backup_root: Path) -> PreparedInput:
    """在任何 Stata 执行前准备；已有快照只核验，不重建或覆盖。"""
    existing = [
        a
        for a in store.artifacts(kind="input_manifest")
        if a.relative_path == "inputs/protection.json"
    ]
    if existing:
        prepared = PreparedInput.model_validate_json(store.read_artifact(existing[0].artifact_id))
        check_input(store, prepared)
        return prepared
    task = store.load_task()
    fingerprint = next(f for f in store.load_input_manifest().files if f.role == "data")
    if file_hash(task.data_path) != fingerprint.sha256:
        raise ArtifactIntegrityError("原件与创建会话时的 hash 不符，禁止建立新的输入快照")
    extension = task.data_path.suffix.lower().lstrip(".")
    if extension not in {"dta", "csv"}:
        raise ValueError("首版仅支持 dta 和 CSV 数据文件")
    backup = backup_root.resolve() / store.run_id / f"original.{extension}"
    if backup.is_symlink() or (backup.exists() and backup.samefile(task.data_path)):
        raise ArtifactIntegrityError("备份须为独立文件，不能链接到原件")
    if backup.exists():
        if file_hash(backup) != fingerprint.sha256:
            raise ArtifactIntegrityError("既有备份 hash 不匹配，禁止覆盖")
    else:
        _exclusive_copy(task.data_path, backup, fingerprint.sha256)
    snapshots = [
        a for a in store.artifacts(kind="input") if a.relative_path == f"inputs/data.{extension}"
    ]
    if snapshots and snapshots[0].sha256 != fingerprint.sha256:
        raise ArtifactIntegrityError("既有快照与原始版本不符，不能覆盖")
    snapshot = (
        snapshots[0]
        if snapshots
        else store.archive_bytes(f"inputs/data.{extension}", backup.read_bytes(), kind="input")
    )
    prepared = PreparedInput.model_validate(
        {
            "input_artifact_id": snapshot.artifact_id,
            "original_path": task.data_path,
            "backup_path": backup,
            "original_sha256": fingerprint.sha256,
            "byte_size": fingerprint.byte_size,
            "file_type": extension,
        }
    )
    store.archive_bytes(
        "inputs/protection.json", prepared.model_dump_json().encode(), kind="input_manifest"
    )
    check_input(store, prepared)
    return prepared


def check_input(store: RunStore, prepared: PreparedInput) -> None:
    """原件、备份、快照均核验；异常时不偷偷从备份恢复原件。"""
    fingerprint = next(f for f in store.load_input_manifest().files if f.role == "data")
    if (
        str(prepared.original_path) != fingerprint.path
        or prepared.original_sha256 != fingerprint.sha256
    ):
        raise ArtifactIntegrityError("输入保护清单不属于此任务的原始文件")
    for source in (prepared.original_path, prepared.backup_path):
        if (
            not source.is_file()
            or source.stat().st_size != prepared.byte_size
            or file_hash(source) != prepared.original_sha256
        ):
            raise ArtifactIntegrityError(
                f"输入或备份缺失、改变：{source.name}；请保留证据并从备份人工恢复"
            )
    if prepared.backup_path.is_symlink() or prepared.backup_path.samefile(prepared.original_path):
        raise ArtifactIntegrityError("备份不是独立文件，不能作为恢复依据")
    snapshot = store.artifact(prepared.input_artifact_id)
    if snapshot.kind != "input" or snapshot.sha256 != prepared.original_sha256:
        raise ArtifactIntegrityError("输入快照的来源或 hash 不正确")


def working_copy(store: RunStore, prepared: PreparedInput, directory: Path) -> Path:
    check_input(store, prepared)
    target = directory / f"input.{prepared.file_type}"
    source = store.directory / store.artifact(prepared.input_artifact_id).relative_path
    _exclusive_copy(source, target, prepared.original_sha256)
    return target
