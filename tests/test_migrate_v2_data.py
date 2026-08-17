from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from bootstrap.workspace_lock import WorkspaceInstanceLock
from scripts import migrate_v2_data as migration


def _legacy_data(workspace: Path) -> Path:
    source = workspace / "mcp" / "steam-mcp"
    source.mkdir(parents=True)
    (source / "steam_mcp_config.json").write_text(
        json.dumps({"steam_api_key": "secret", "steam_id": "user"}),
        encoding="utf-8",
    )
    (source / "steam_user_cache.json").write_text('{"user": "kept"}\n', encoding="utf-8")
    with sqlite3.connect(source / "steam_proactive.sqlite3") as database:
        database.execute("CREATE TABLE snapshots (value TEXT NOT NULL)")
        database.execute("INSERT INTO snapshots VALUES ('kept')")
        database.commit()
    return source


def test_migration_preserves_source_and_publishes_verified_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    target = workspace / "plugin-data" / "steam-github"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert receipt["recovery"] == {
        "kind": "retained_source",
        "path": "mcp/steam-mcp",
    }
    assert [item["status"] for item in receipt["files"]] == [
        "copied",
        "copied",
        "source_missing",
        "copied",
    ]
    assert (source / "steam_mcp_config.json").is_file()
    assert (target / "steam_mcp_config.json").read_bytes() == (
        source / "steam_mcp_config.json"
    ).read_bytes()
    with sqlite3.connect(target / "steam_proactive.sqlite3") as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert database.execute("SELECT value FROM snapshots").fetchone() == ("kept",)
    assert migration.migrate_v2_data(
        workspace=workspace,
        marketplace="github",
    ) == receipt_path


def test_in_process_failure_rolls_back_only_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _legacy_data(workspace)
    original_replace = os.replace
    calls = 0

    def fail_second_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(migration.os, "replace", fail_second_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    assert not (workspace / "plugin-data" / "steam-github").exists()
    assert list((workspace / "plugin-data").glob(".steam-v2-migrate-*")) == []


def test_process_crash_partial_publish_is_reconciled_on_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    plugin_data = workspace / "plugin-data"
    target = plugin_data / "steam-github"
    stale = plugin_data / ".steam-v2-migrate-crashed"
    target.mkdir(parents=True)
    stale.mkdir()
    (target / "steam_mcp_config.json").write_bytes(
        (source / "steam_mcp_config.json").read_bytes()
    )
    (stale / "orphan").write_text("partial", encoding="utf-8")

    receipt_path = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    statuses = {
        item["name"]: item["status"]
        for item in json.loads(receipt_path.read_text(encoding="utf-8"))["files"]
    }
    assert statuses["steam_mcp_config.json"] == "verified"
    assert statuses["steam_proactive.sqlite3"] == "copied"
    assert not stale.exists()


def test_conflict_and_busy_workspace_leave_both_trees_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = _legacy_data(workspace)
    target = workspace / "plugin-data" / "steam-github"
    target.mkdir(parents=True)
    (target / "steam_mcp_config.json").write_text("current\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="内容不同"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")
    assert json.loads((source / "steam_mcp_config.json").read_text())["steam_api_key"] == "secret"
    assert (target / "steam_mcp_config.json").read_text(encoding="utf-8") == "current\n"
    assert not (target / migration._RECEIPT).exists()

    clean_workspace = tmp_path / "busy-workspace"
    _legacy_data(clean_workspace)
    lock = WorkspaceInstanceLock(clean_workspace)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="其他 runtime 占用"):
            migration.migrate_v2_data(workspace=clean_workspace, marketplace="github")
    finally:
        lock.release()
    assert not (clean_workspace / "plugin-data").exists()


def test_symlink_source_and_final_receipt_drift_fail_loud(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _legacy_data(outside)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mcp").symlink_to(outside / "mcp", target_is_directory=True)
    with pytest.raises(FileNotFoundError, match="不安全"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")

    workspace = tmp_path / "workspace-drift"
    _legacy_data(workspace)
    receipt = migration.migrate_v2_data(workspace=workspace, marketplace="github")
    config = receipt.parent / "steam_mcp_config.json"
    config.write_text('{"steam_api_key":"changed","steam_id":"user"}', encoding="utf-8")
    with pytest.raises(ValueError, match="目标内容漂移"):
        migration.migrate_v2_data(workspace=workspace, marketplace="github")
