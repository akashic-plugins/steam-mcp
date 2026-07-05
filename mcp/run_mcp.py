#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    from steam_mcp import mcp

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
