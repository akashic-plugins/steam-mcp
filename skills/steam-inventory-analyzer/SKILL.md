---
name: steam-inventory-analyzer
description: 查询和分析 Steam 玩家库存、游戏库、成就、好友、在线人数与游戏新闻。用户提到 Steam, 库存, 饰品, CS2 库存, Dota2 库存, appid, Steam 好友, Steam 成就, Steam 游戏库 时使用。
when_to_use: 用户要查 Steam 玩家资料、库存、游戏时长、最近在玩什么、好友列表、成就或某个游戏的在线人数时，优先使用 steam MCP 工具。
metadata: {"akashic": {"always": false}}
---

# Steam Inventory Analyzer

优先使用 `steam` MCP，不要先走网页搜索。

## 常见任务

- 查某个 Steam 用户最近玩了什么
- 查某个游戏当前在线人数
- 查某个用户的游戏库和时长
- 查某个用户在指定游戏里的成就或统计
- 查 Steam 新闻

## 使用方式

1. 如果用户给的是 vanity URL，先解析成 Steam ID64。
2. 再按任务选择 `steam` MCP 工具。
3. 返回原始结果时，顺手补一段人话总结。

## 输出建议

- 先给结论
- 再给关键字段
- 游戏相关优先展示游戏名、时长、成就数、在线人数
- 库存相关优先展示总数、稀有项、可疑重复项

## 注意

- 需要 Steam Web API Key
- `steam_mcp_config.json` 一般放在插件数据目录
- 有些库存接口会因游戏不同而表现不一致，失败时要明确报错，不要编造结果
