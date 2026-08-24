from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType


repo_root = Path(__file__).resolve().parents[1]
package = ModuleType("steam_test_plugin")
package.__path__ = [str(repo_root)]
package.__package__ = "steam_test_plugin"
sys.modules["steam_test_plugin"] = package
