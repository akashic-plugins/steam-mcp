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
~/.akashic-plugin/data/steam-<marketplace>/
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
  "steam_id": "YOUR_STEAM_ID64"
}
```

When migrating from the old workspace MCP, the plugin copies the old config and cache files automatically on first startup.
