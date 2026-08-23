from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from steam_runtime.config import load_runtime_config


ROOT = Path(__file__).resolve().parents[2]


def test_formal_runtime_config_requires_plugin_data_credentials(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "steam_mcp_config.json"
    config_path.write_text(
        json.dumps({"steam_id": "user", "snapshot_interval_seconds": 3600}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STEAM_API_KEY", "ambient-secret")

    with pytest.raises(RuntimeError, match="缺少 steam_api_key"):
        load_runtime_config(config_path)


def test_formal_runtime_config_accepts_complete_file(tmp_path) -> None:
    config_path = tmp_path / "steam_mcp_config.json"
    config_path.write_text(
        json.dumps(
            {
                "steam_api_key": "formal-secret",
                "steam_id": "user",
                "snapshot_interval_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(config_path)

    assert config.steam_api_key == "formal-secret"
    assert config.steam_id == "user"
    assert config.snapshot_interval_seconds == 3600


def test_recording_mcp_exposes_only_user_driven_tools(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STEAM_BACKEND", "recording")
    sys.modules.pop("steam_mcp", None)

    module = importlib.import_module("steam_mcp")
    names = {tool.name for tool in module.mcp._tool_manager.list_tools()}

    assert names == {
        "get_news_for_app",
        "get_player_summaries",
        "get_owned_games",
        "get_recently_played_games",
        "get_friend_list",
        "get_player_achievements",
        "get_user_stats_for_game",
        "get_number_of_current_players",
        "resolve_app_ids",
    }
    assert not (tmp_path / "steam_mcp_config.json").exists()
    assert not (tmp_path / "steam_proactive.sqlite3").exists()


def test_runner_uses_three_bounded_log_rotations(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / "mcp" / "run_mcp.py"
    spec = importlib.util.spec_from_file_location("steam_test_runner", path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(tmp_path))

    runner._setup_logging(tmp_path)

    rotating = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].backupCount == 3
    assert rotating[0].maxBytes == 5 * 1024 * 1024
    for handler in logging.getLogger().handlers:
        handler.close()
