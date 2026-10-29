# 下线玩家口述地图机制（Api.MAP_MEMORY / 玩家指定）

> 状态：**已完成**（2026-10-29）。全量回归 352/352 通过（含语法、字段守卫），Code Review 通过。

## 背景
- 加固符文石（90573）悬停显示"区域 永歌森林"，分组却落在"周常"组，用户追问后确认这是两套口径并存造成的自相矛盾。
- 口述表硬编码在 Lua 源码里（`Api.MAP_MEMORY`），没有任何配置界面；下个资料片这些条目必然整体失效成垃圾代码片段。
- 用户决定（2026-10-29）：口述映射**整体去掉**。设计口径：尽量少配置、用代码规范数据；只有代码解决不了的（如关键词，代码不可能知道每个用户的需求）才留配置与相关功能。

## 目标
1. 拆掉写入链路：
   - `Api.MAP_MEMORY` 表及其注释块（Api.lua）。
   - `Sync.ApplyMapMemory` 函数与同步流程里的调用点、日志（Sync.lua）。
   - `Ledger.SetManualMap` / `L.ManualMapSource` / `MANUAL_MAP_SRC` 局部函数（Ledger.lua）。
   - `TRUSTED_MAP_SRC` 白名单里的 manual 档：口述图不许再参与族搬运。
2. 老存档清洗（schema v7 → v8 迁移，新增 `Ledger.ClearManualMaps`）：
   - `mapSrc == 玩家指定` 的记录：清掉 mapID / mapSrc（口述图一个不留）。
   - `mapManual == true` 但 mapSrc 已被客户端来源覆盖的记录：图保留（客户端真凭据），只抹残留标记。
   - 迁移幂等；失败停档下次登录重试（沿用现有 Migrate 语义）。
3. 判定读者保留（迁移失败重试窗口期行为与现状一致，不算废弃代码）：
   - `NOT_CLIENT_SRC` 里的 manual 档。
   - `QuestHasCredential` 的 `not rec.mapManual`。
   - `Options.evidence` 的 `not rec.mapManual`。
   - 三处注释改写为"机制已下线、只认老存档残留"口径。
4. `Config.MAP_SRC.manual = "玩家指定"` 标签保留：它是 v8 迁移匹配老形状的唯一出处（字符串禁令测试对 Config.lua 豁免），注释改为"已退役，仅迁移读"。
5. `QUEST_RECORD_FIELDS` 里的 mapManual 保留：v8 迁移要写 `rec.mapManual = nil`，字段守卫不许谎报（同 manualAdded 先例）。

## 测试
- 删除 5 条机制用例：player_can_fix_maps_the_client_cannot_answer、map_memory_says_what_it_could_not_apply、map_memory_fills_every_row_sharing_a_name、shipped_map_memory_names_a_real_zone、map_memory_rejects_non_numeric_quest_id（含 runner 注册）。
- 新增 v8 迁移用例：清口述图 / 客户端图只清标记 / 迁移幂等。
- 其余手写"玩家指定"老形状的用例（周常组、屏蔽管理、凭据门、probe 取证）依赖保留的读者，预期不动全绿。

## 验收
- 全量 run_gql_tests.py（含语法、字段守卫）通过。
- 符文石、梦魇、追踪萨拉塔斯等口述行迁移后不再带"区域 xxx"备注，分组照旧归"周常"组。
