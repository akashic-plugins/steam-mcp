#!/usr/bin/env python3
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _runtime_dir() -> Path:
    raw = os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip()
    if not raw:
        raise RuntimeError("Steam MCP 缺少 AKA_PLUGIN_DATA_DIR")
    return Path(raw).resolve()


def _setup_logging(runtime_dir: Path) -> None:
    """将诊断写入 stderr 和三个有界本地轮转文件。"""

    runtime_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    )
    file_handler = RotatingFileHandler(
        runtime_dir / "steam_mcp.runtime.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    for path in (script_dir.parent, script_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    backend = os.environ.get("STEAM_BACKEND", "formal").strip().lower()
    if backend not in {"formal", "recording"}:
        raise RuntimeError(f"未知 STEAM_BACKEND: {backend}")
    if backend == "formal":
        from steam_runtime.config import load_runtime_config

        data_root = _runtime_dir()
        _ = load_runtime_config(data_root / "steam_mcp_config.json")

    _setup_logging(_runtime_dir())
    from steam_mcp import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
