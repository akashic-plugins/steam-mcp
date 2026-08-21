#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    backend = os.environ.get("STEAM_BACKEND", "formal").strip().lower()
    if backend not in {"formal", "recording"}:
        raise RuntimeError(f"未知 STEAM_BACKEND: {backend}")
    if backend == "formal":
        from runtime_config import load_runtime_config

        data_root = Path(os.environ["AKA_PLUGIN_DATA_DIR"]).resolve()
        _ = load_runtime_config(data_root / "steam_mcp_config.json")

    from steam_mcp import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
