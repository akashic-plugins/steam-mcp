# steam-mcp

Steam 是 Akashic Plugin API v3 插件。它用现有普通原语组合 current context：

```text
Core Timer ──触发──> Steam shared domain ──覆盖──> current presence
                              │
                              └──有意义变化──追加──> game snapshot history

Wake BeforeTurn ──只读 fresh state──> extra_hints
Passive BeforeTurn ──────────────────> 不变
用户 Turn ──调用 Steam MCP───────────> 主动查询 Steam API
```

## 能力与 owner

- `MCP_SERVERS`：保留用户主动查询工具。MCP 不再包含 context fetch 或手动
  snapshot 特权工具。
- `TIMERS`：正式稳定 Root 独占一个 one-shot Timer。presence 每 5 分钟刷新；
  网络瞬时失败记录结构化 Incident，并在 60 秒后重试。
- `turn.context_prepared`：只在 `channel=wake` 且 current presence 仍 fresh 时
  append 一个普通 hint；不会 abort、替换 prompt 或影响 passive Turn。

Steam SQLite 是唯一 domain state owner：

- `current_state` 是可覆盖 singleton，保存 current presence、当前游戏列表、刷新
  deadline 和最近错误。
- `snapshots`、`snapshot_runs` 保存真实游戏快照历史，不自动裁切。相同快照或空
  结果只推进 current check time，不制造历史。
- 旧文件名 `steam_proactive.sqlite3` 为了原位继承正式历史而保留；运行代码、表
  owner 和插件能力已经不依赖旧主动系统。

纯诊断日志固定为 5 MiB、最多 3 个备份。candidate 只在自己的隔离目录完成 MCP
readiness/handshake：不注册 Timer、不访问 Steam 外网、不读取或写入正式 state。

## 配置

在插件 data root 创建 `steam_mcp_config.json`：

```json
{
  "steam_api_key": "YOUR_STEAM_API_KEY",
  "steam_id": "YOUR_STEAM_ID64",
  "snapshot_interval_seconds": 21600
}
```

`snapshot_interval_seconds` 只控制游戏历史快照检查；presence 使用固定 5 分钟
freshness，避免把历史采样频率和当前状态时效揉成一个概念。

## v2 data migration

显式迁移仍使用 `scripts/migrate_v2_data.py`。它保留 `mcp/steam-mcp` 原文件，
通过 SQLite backup、integrity check 和 hash receipt 发布到
`plugin-data/steam-<marketplace>/`，不删除历史源。

## 验证

CI 固定 Core `9da3a988a2bf62b0f550bd4f6bb98c4eeb1f56f5`。测试覆盖真实
PluginManager + stdio MCP + Timer、candidate 零正式 write set、reload Timer
换班、网络失败恢复、fresh/stale/unknown、Wake/passive 分流、历史保留、日志轮转、
pyright、compileall、Plugin API contract 和 `git diff --check`。

### 用户工具发现

`plugin.py` 向 `TOOLS` 注册用户工具，保留 `mcp_steam__` 名称。目录来自 MCP `tools/list`，加载插件不会启动 MCP；实际调用才打开本插件的 MCP 路由。服务端负责参数校验，MCP 工具错误和传输失败保留原语义。修改 MCP 签名或描述时同步更新 `tool_catalog.json`。
