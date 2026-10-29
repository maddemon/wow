# 修复：池任务快照的完成 flag 残留假绿（健步如飞 ×14）

> 状态：**已完成**（2026-10-29）。用户确认按两刀做；356/356 单测 + --syntax 通过，Code Review 通过。

## 现象与根因
- dump 金币任务表"健步如飞"14 号全"已交"，其中 9 个号本周完成数 0（连流水都没有）。
- 来源链：任务在池时，快照把**客户端答的状态**原样写进各号 views（编号=当时实例键）；客户端对做过的号答 done 是**上周 flag 残留**；任务结束后退出池子，"不在池里→写 ended"的清扫又因这行带 kwLogAdded 被跳过 → 旧 done 快照冻结、编号仍匹配 → 永远绿。
- 与 92713 那 12 份 k=nil 旧快照同族（flag 残留），但这批编号对得上、真会上屏。
- 已排除 v6 补编号背锅：v6 只动流水，这 9 个号没有流水（完成数 0）。

## 改动（已落地）
1. **往后**：快照写 view=done 时加"本轮有流水"门（镜像点格子的 L.CompletedThisRound 门）——客户端答 done 但没有编号对上的流水 → 降级写 open；真交过的号流水在、门放行（交割后 15 秒钱窗内 FinishWindow 自己会补写 done，自愈）。落地于 Sync.lua 池循环（Sync.lua:804-813，与流水覆盖收拢成 if/elseif，评审建议）。
2. **存量**：v9 迁移——只动 `poolSeen` 且非 `regKey` 任务的快照：view=done 但没有编号对上的流水背书 → 删该快照。注册周常不在本次范围。落地于 Ledger.DropUnbackedDoneViews（Ledger.lua:1994）+ Config.SCHEMA_VERSION=9 / MIGRATIONS[9]（Config.lua:281/333）。

## 验收（已过）
- 新用例：`test_snapshot_done_needs_this_instance_turnin`（无流水降级/有流水放行/换实例退回 open）+ `test_migrate_drops_unbacked_done_views`（删无背书、留有背书、留注册行、留非池任务、流水一条不删、幂等、日志计数）。
- 连带更新：v6/v7 迁移用例因阶梯连跑到 v9 更新终态预期（v6 tally 8/16→6/8，v9 清 10 笔：94612×7 + 93573 号2/号3 + 90333，背书按角色算）；`anchor_in_the_pool_lists_only_one_row` 的族本体 done 改由流水背书供给（flag 不再单独作凭据，问全族路径不变）。
- 全量回归 `python tests/run_gql_tests.py --syntax`：356/356 通过。
- Code Review（SubAgent）：通过，无阻断问题；建议①（twin 条件收拢）已采纳，②（阶梯耦合为固有代价）记录在案。
