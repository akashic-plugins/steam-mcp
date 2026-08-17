#!/usr/bin/env python3
"""把 Steam v2 workspace 数据非破坏迁移到 v3 plugin-data。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from agent.plugins.manifest import (
    ensure_workspace_plugin_data_dir,
    validate_workspace_plugin_data_path,
)
from bootstrap.workspace_lock import WorkspaceInstanceLock


_DATA_FILES = (
    "steam_mcp_config.json",
    "steam_user_cache.json",
    "steam_app_cache.json",
    "steam_proactive.sqlite3",
)
_RECEIPT = ".steam-v2-migration.json"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _sqlite_integrity(path: Path) -> str:
    """只读校验 SQLite 文件。"""

    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        result = database.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise sqlite3.DatabaseError(f"Steam SQLite 完整性检查失败: {path} ({result})")
    return "ok"


def _copy_sqlite(source: Path, destination: Path) -> None:
    """用 SQLite backup 生成一致副本。"""

    _sqlite_integrity(source)
    uri = f"{source.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db, pages=256, sleep=0.1)
            destination_db.commit()
    _sqlite_integrity(destination)


def _stage(source: Path, staging: Path) -> tuple[dict[str, object], ...]:
    """复制全部现存 v2 文件并生成内容证据。"""

    entries: list[dict[str, object]] = []
    for name in _DATA_FILES:
        source_file = source / name
        if not source_file.exists() and not source_file.is_symlink():
            entries.append({"name": name, "status": "source_missing"})
            continue
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"Steam v2 数据不是普通文件: {source_file}")
        staged_file = staging / name
        if name.endswith(".sqlite3"):
            _copy_sqlite(source_file, staged_file)
        else:
            shutil.copy2(source_file, staged_file)
        entry: dict[str, object] = {
            "name": name,
            "status": "staged",
            "sha256": _digest(staged_file),
            "size": staged_file.stat().st_size,
        }
        if name.endswith(".sqlite3"):
            entry["sqlite_integrity"] = "ok"
        entries.append(entry)
    return tuple(entries)


def _record_target(entry: dict[str, object], destination: Path) -> None:
    entry["sha256"] = _digest(destination)
    entry["size"] = destination.stat().st_size
    if destination.name.endswith(".sqlite3"):
        entry["sqlite_integrity"] = _sqlite_integrity(destination)


def _validate_targets(target: Path, entries: tuple[dict[str, object], ...]) -> None:
    """拒绝覆盖不同内容，并收束进程崩溃留下的同内容文件。"""

    for entry in entries:
        destination = target / str(entry["name"])
        if destination.is_symlink():
            raise ValueError(f"Steam v3 目标不得是符号链接: {destination}")
        if entry["status"] == "source_missing":
            if not destination.exists():
                continue
            if not destination.is_file():
                raise FileExistsError(f"Steam v3 目标不是普通文件: {destination}")
            entry["status"] = "target_only"
            _record_target(entry, destination)
            continue
        if not destination.exists():
            entry["status"] = "copied"
            continue
        if (
            not destination.is_file()
            or destination.stat().st_size != entry["size"]
            or _digest(destination) != entry["sha256"]
        ):
            raise FileExistsError(f"Steam v3 目标已存在且内容不同: {destination}")
        entry["status"] = "verified"
        if destination.name.endswith(".sqlite3"):
            _sqlite_integrity(destination)
    if all(entry["status"] == "source_missing" for entry in entries):
        raise FileNotFoundError("Steam v2 与 v3 数据目录都没有可迁移文件")


def _publish(
    staging: Path,
    target: Path,
    entries: tuple[dict[str, object], ...],
    receipt: dict[str, object],
) -> None:
    """发布本事务创建的文件，进程内失败时完整回滚。"""

    published: list[Path] = []
    try:
        for entry in entries:
            if entry["status"] != "copied":
                continue
            destination = target / str(entry["name"])
            os.replace(staging / str(entry["name"]), destination)
            published.append(destination)
        staged_receipt = staging / _RECEIPT
        staged_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt_path = target / _RECEIPT
        os.replace(staged_receipt, receipt_path)
        published.append(receipt_path)
    except BaseException:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise


def _remove_crash_staging(workspace: Path) -> None:
    """清理上次进程崩溃留下的未发布 staging。"""

    plugin_data = workspace / "plugin-data"
    if plugin_data.is_symlink():
        raise ValueError(f"Steam plugin-data 目录不得是符号链接: {plugin_data}")
    if not plugin_data.is_dir():
        return
    for path in plugin_data.glob(".steam-v2-migrate-*"):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _valid_receipt(path: Path, *, target: Path, marketplace: str) -> bool:
    """验证已有最终 receipt 及其目标文件。"""

    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Steam migration receipt 不是普通文件: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    files = value.get("files") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("source") != "mcp/steam-mcp"
        or value.get("target") != f"plugin-data/steam-{marketplace}"
        or value.get("recovery")
        != {"kind": "retained_source", "path": "mcp/steam-mcp"}
        or not isinstance(files, list)
        or [item.get("name") for item in files if isinstance(item, dict)]
        != list(_DATA_FILES)
    ):
        raise ValueError(f"Steam migration receipt 无效: {path}")
    for item in files:
        if not isinstance(item, dict) or item.get("status") not in {
            "source_missing",
            "target_only",
            "verified",
            "copied",
        }:
            raise ValueError(f"Steam migration receipt 无效: {path}")
        destination = target / str(item.get("name"))
        if item["status"] == "source_missing":
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"Steam migration receipt 目标漂移: {destination}")
            continue
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or isinstance(size, bool)
            or destination.is_symlink()
            or not destination.is_file()
            or destination.stat().st_size != size
            or _digest(destination) != digest
        ):
            raise ValueError(f"Steam migration receipt 目标内容漂移: {destination}")
        if destination.name.endswith(".sqlite3"):
            if item.get("sqlite_integrity") != "ok":
                raise ValueError(f"Steam migration receipt 缺少 SQLite integrity: {path}")
            _sqlite_integrity(destination)
    return True


def migrate_v2_data(*, workspace: Path, marketplace: str) -> Path:
    """持有 workspace 独占锁迁移 Steam 数据并写最终 receipt。"""

    workspace = workspace.expanduser().resolve()
    lock = WorkspaceInstanceLock(workspace)
    lock.acquire()
    try:
        return _migrate_locked(workspace=workspace, marketplace=marketplace)
    finally:
        lock.release()


def _migrate_locked(*, workspace: Path, marketplace: str) -> Path:
    """在独占区间准备、校验并发布一次迁移。"""

    if not marketplace or not marketplace.replace("-", "").isalnum():
        raise ValueError(f"Steam marketplace 无效: {marketplace!r}")
    mcp_root = workspace / "mcp"
    source = mcp_root / "steam-mcp"
    if (
        mcp_root.is_symlink()
        or source.is_symlink()
        or not source.is_dir()
        or not source.resolve().is_relative_to(workspace)
    ):
        raise FileNotFoundError(f"Steam v2 数据目录不存在或不安全: {source}")
    target = workspace / "plugin-data" / f"steam-{marketplace}"
    validate_workspace_plugin_data_path(target, workspace)
    _remove_crash_staging(workspace)
    receipt_path = target / _RECEIPT
    if _valid_receipt(receipt_path, target=target, marketplace=marketplace):
        return receipt_path

    staging = workspace / "plugin-data" / f".steam-v2-migrate-{uuid.uuid4().hex}"
    created_target = not target.exists()
    ensure_workspace_plugin_data_dir(staging, workspace)
    try:
        entries = _stage(source, staging)
        ensure_workspace_plugin_data_dir(target, workspace)
        _validate_targets(target, entries)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "source": "mcp/steam-mcp",
            "target": f"plugin-data/steam-{marketplace}",
            "recovery": {"kind": "retained_source", "path": "mcp/steam-mcp"},
            "files": entries,
        }
        _publish(staging, target, entries, receipt)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if created_target and target.is_dir() and not any(target.iterdir()):
            target.rmdir()
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--marketplace", required=True)
    args = parser.parse_args()
    print(migrate_v2_data(workspace=args.workspace, marketplace=args.marketplace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
