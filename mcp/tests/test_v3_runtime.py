from __future__ import annotations

import importlib
import json
import sys

import pytest

from runtime_config import load_runtime_config


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


def test_recording_context_never_reads_formal_config_or_creates_database(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "candidate-data"
    monkeypatch.setenv("AKA_PLUGIN_DATA_DIR", str(data_root))
    monkeypatch.setenv("STEAM_BACKEND", "recording")
    monkeypatch.setenv("STEAM_API_KEY", "ambient-secret")
    monkeypatch.setenv("STEAM_ID", "ambient-user")
    sys.modules.pop("steam_mcp", None)
    sys.modules.pop("steam_proactive", None)

    module = importlib.import_module("steam_mcp")
    result = module.get_steam_context()

    assert result == {
        "items": [
            {
                "presence": "unknown",
                "interruptibility": 0.4,
                "confidence": 0.0,
                "transition": "",
                "recording": True,
            }
        ]
    }
    assert "steam_proactive" not in sys.modules
    assert not (data_root / "steam_mcp_config.json").exists()
    assert not (data_root / "steam_proactive.sqlite3").exists()
