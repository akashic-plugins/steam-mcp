# steam-mcp

Akashic Steam plugin. It bundles:

- `steam` MCP server
- `steam-inventory-analyzer` skill

## Install

```bash
python main.py plugin-install --source https://github.com/akashic-plugins/steam-mcp --marketplace github
```

Restart Akashic after install.

## Data directory

Runtime data lives in:

```text
<workspace>/plugin-data/steam-<marketplace>/
```

Common files:

- `steam_mcp_config.json`
- `steam_user_cache.json`
- `steam_app_cache.json`
- `steam_proactive.sqlite3`

## Config

Create `steam_mcp_config.json` in the plugin data directory:

```json
{
  "steam_api_key": "YOUR_STEAM_API_KEY",
  "steam_id": "YOUR_STEAM_ID64",
  "snapshot_interval_seconds": 21600
}
```

`get_steam_context` 每次读取实时在线状态，并在历史游戏时长快照超过
`snapshot_interval_seconds` 时自动刷新。空的最近游玩列表也会记录快照批次，避免重复刷新。

## v2 data migration

v3 不会在插件加载时隐式复制正式数据。停止 Akashic 后显式执行：

```bash
PYTHONPATH=/path/to/akashic-agent \
python scripts/migrate_v2_data.py \
  --workspace /path/to/workspace \
  --marketplace github
```

迁移保留 `mcp/steam-mcp` 原文件，在
`plugin-data/steam-<marketplace>/.steam-v2-migration.json` 写入 hash 与 SQLite
完整性证据。进程内失败会回滚本次新增文件；进程崩溃后重跑会清理 staging，
并只接纳已经发布且内容完全相同的文件。

候选验证使用无凭证、无外网、无数据库的 recording backend；正式 MCP
只从自己的 `plugin-data` 读取 `steam_mcp_config.json`，不读取 ambient
`STEAM_API_KEY` 或 `STEAM_ID`。
