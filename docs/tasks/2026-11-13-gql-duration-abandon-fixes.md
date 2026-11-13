# GQL 任务耗时：放弃判定与显示修复（2026-11-13）

## 背景

任务耗时功能（`GoldQuestLedger/Duration.lua`）复核出两类真实缺陷：

1. **误判放弃 → 清零**：快照拍无条件把 `poolActive` 交给 `Reconcile` 当"完整池视图"，
   而 12.x 按图源返回的是 `partial=true`；`state1` 读不出、图数据没加载、任务不在本次
   地图清单里，都会被当成"放弃"，下一次池拍 Tick 按"放弃重接"把 `total` 清零。
2. **真实放弃检测不到**：矩阵关着时只有 `LogBypass → Advance`，而 `Advance` 永不暂停；
   全仓没有 `QUEST_REMOVED` 监听。接取→放弃→再接回，放弃那段会被计入。

另有一批低风险问题：`LastDurAll` 跨时钟域比较、`FormatSpan` 无小时档、
`SnapshotNow` 重复读日志、`db.durations` 类型无兜底、`/gql dur` 一次刷屏、
老档残留 `fortify-the-runestones` 注册键。

## 目标（可验收）

- partial 快照 / 状态读不出的拍，不得把池型任务误判为放弃并清零。
- 矩阵关着时玩家放弃任务，必须被检测到；放弃前那段不计入下一次尝试。
- 日志型任务的放弃语义保持不变。
- 上述低风险问题收口，既有用例全绿。

## 范围

### P0

1. 池拍暂停权按条目来源区分（partial / 未知状态不再整拍误判）。
2. 注册 `QUEST_REMOVED`，精确处理放弃。
3. `Reconcile` 接收调用方已读到的任务日志集合，去掉重复读。

### P1

4. `Duration.LastDurAll` 只在 epoch 行之间比较。
5. `Duration.FormatSpan` 增加小时档。
6. `Config.Init` 增加 `durations` 的 ensure 兜底。
7. `/gql dur` 输出封顶，避免刷屏。
8. v13 迁移清掉老档 `fortify-the-runestones` 遗留注册键（0.7.3 的存档 schema 可能已是 12，
   所以不能追加在 v12 里，单独一档保证老用户跑得到）。

### 不在本次

- 池型任务在矩阵关着时的"在线在册"精度（既有边界，不在本轮）。
- `/gql dur` 的真正分页（本次只封顶）。

> 口径补充（用户 2026-11-13 确认）：任务实例轮换了就算放弃，旧 `total` 不带进新实例。
> 已实现：可信缺席的一拍会把 `start=nil, leftOnline=false` 的旧条目补标成 `leftOnline=true`；
> `QUEST_ACCEPTED` 走 `Duration.Accepted`，已有暂停条目的再次接取直接按新尝试从 0 起算。

## 详细方案

### 1. Duration 条目来源标记 + 暂停判据

文件：`GoldQuestLedger/Duration.lua`、`GoldQuestLedger/Sync.lua`

- entry 增加 `viaPool` 字段。语义：**当前尝试尚未被常规任务日志确认**。
  - `Entry` 新建时默认 `viaPool = true`（来源未确认）。
  - `Tick` 增加可选参数 `fromLog`：`true` → `viaPool = false`；`false` → `viaPool = true`；
    不传则保持原值（`QUEST_ACCEPTED` 那条路不传）。
- `Reconcile(db, charKey, now, extraActive, opts)`：
  - `opts = { inLog = set|nil, poolTrusted = bool, unknown = set|nil }`。
  - `inLog` 缺省时自行调用 `InLogSet`（保持旧调用方可用）。
  - `poolTrusted` 缺省 `false`。
  - 推进：`inLog` 里的 qid 用 `fromLog=true`；只在 `extraActive` 里的 qid 用 `fromLog=false`。
  - 暂停条件改为：
    `not active[qid] and not (opts.unknown and opts.unknown[qid])`
    且 `(e.viaPool == false or poolTrusted)`；命中时若 `e.start` 存在则闭合区间，
    无论 `start` 是否有值都写 `leftOnline = true`（`start=nil` 的空档同样可能是
    离线期间实例已轮换/被移除，见 1.1）。
  - `extraActive == nil`（`Advance` 的日志拍）仍不暂停，行为不变。
- `Sync.SnapshotNow`：
  - 池循环里收集 `stateUnknown`（`QuestState` pcall 失败或返回 nil / `"unknown"` 的 qid）。
  - 任务日志只读一次：`local inLog = GQL.Duration.InLogSet(db)`，随 `opts.inLog` 传入；
    读不到就整拍不动。
  - `poolTrusted = not partial`。
- 结果：生产按图源 `partial=true` → `poolTrusted=false` → 池型条目不再被快照拍暂停；
  日志型条目（`viaPool=false`）仍按完整日志视图暂停。日志读取失败时 `InLogSet` 返回 nil，
  `Reconcile` 整体不动（维持现状）。
- 已知取舍：`poolTrusted=false` 时池型任务若真的消失且没有事件，entry 会滞留到
  `Finalize`；玩家主动放弃由 `QUEST_REMOVED` 覆盖。

### 1.1 离线轮换 = 放弃（用户 2026-11-13 确认）

- `PauseAll` 登出时只把区间闭合、写 `leftOnline=false`（离线不算并接续旧尝试）。
- 若离线期间任务实例已轮换/被移除，登录后的第一拍可信缺席会把它补标成
  `leftOnline=true`（`start` 已经是 nil 也要标），下次接取按新实例从 0 起算。
- 可信缺席沿用同一判据：日志型条目（`viaPool=false`）看完整日志；
  池型条目只在 `poolTrusted=true` 时补标，partial 拍不动（避免重新引入误清零）。
- 补强：`QUEST_ACCEPTED` 改走 `Duration.Accepted`——账本里已有这条的暂停条目，
  说明上一条实例已经不在（离线轮换或在线放弃），这次接取直接清零重开；
  已在计时的条目不动。

### 2. QUEST_REMOVED 精确放弃

文件：`GoldQuestLedger/Sync.lua`、`GoldQuestLedger/Duration.lua`

- `Duration.Abandon(db, charKey, questID, now)`：
  - 无 entry 返回 false。
  - 若 `e.start` 存在：`e.total += now - e.start`，`e.start = nil`。
  - `e.leftOnline = true`；返回 true。
- `Sync.frame` 注册 `QUEST_REMOVED`（`TryRegister`，与 `PLAYER_LOGOUT` 同一处文件级注册）。
- `OnEvent` 增加分支：`QUEST_REMOVED` → `Duration.Abandon(db, CurrentCharKey(), arg1, Clock())`，
  不触发快照、不触发重画。
- 与交割顺序：
  - `Finalize` 已删 entry → `Abandon` no-op。
  - `QUEST_REMOVED` 先到 → 闭合区间；随后 `Finalize` 仍取 `total`，时长不丢。
- `PauseAll` 保持 `leftOnline=false`（离线暂停、下次接续）；只有确定的"任务被移除"
  才写 `true`。

### 3. LastDurAll 跨时钟域

文件：`GoldQuestLedger/Duration.lua`

- 只比较 `tonumber(r.t) > 1e9` 的 epoch 行；一条都没有时返回 nil。
- 理由：uptime 每次开机归零，跨会话也不可比；跨域直接比大小会让 uptime 行永远选不上。
- 影响：uptime-only 环境不显示"最近一次"，`Average` 与其它统计不受影响。

### 4. FormatSpan 小时档

文件：`GoldQuestLedger/Duration.lua`

- `h > 0` → `"%d时%d分"`；其余保持 `"%d分%d秒"` / `"%d秒"`。

### 5. Config.Init ensure

文件：`GoldQuestLedger/Config.lua`

- 在现有 `ensure` 列表中加入 `ensure(db, "durations")`。
- 非 table 的坏值走现有 `bad` 日志并重建成空表。

### 6. /gql dur 输出封顶

文件：`GoldQuestLedger/Core.lua`

- 排序后最多打印 20 条（页内常量）。
- 还有剩余时追加一行：`……其余 N 条未显示（按平均耗时降序取前 20）`。
- 不改统计与排序口径。

### 7. v13 清理 fortify 遗留注册键

文件：`GoldQuestLedger/Ledger.lua`、`GoldQuestLedger/Config.lua`

- Ledger 增加 `L.ClearFortifyRegKeysOnce(db)`：
  - 只处理 `90573/90574/90575/90576` 且 `rec.regKey == "fortify-the-runestones"`；
  - 清成 `nil`，返回清掉的条数；幂等。
- `Config.SCHEMA_VERSION` 提到 13，新增 `MIGRATIONS[13]` 单独跑它；`[12]` 恢复为只做
  `Duration.ClearAllOnce`。
- 原因：0.7.3 已经发过的存档 schema 可能停在 12，追加在 `[12]` 里对这批存档永远跑不到；
  单独一档对 v11/v12 存档都生效，且幂等。
- 不动记录其它字段，不删 `turnins`。

## 用例

新增：

1. `test_task_duration_partial_pool_tick_must_not_pause_pool_quest`
   - 池拍起算（`viaPool=true`）；随后 partial 快照（`poolTrusted=false`）里池为空，
     不得暂停/清零；下一拍池里恢复 progress 时 `total` 连续。
2. `test_task_duration_unknown_state_tick_must_not_pause_pool_quest`
   - `poolTrusted=true` 但 qid 在 `unknown` 集合里：这一拍不暂停；下一拍正常推进不清零。
3. `test_task_duration_snapshot_unknown_state_does_not_pause`
   - 集成：`SnapshotNow` 的 `stateUnknown` 接线；`QuestState` 答 `"unknown"` 的一拍不暂停，
     恢复 `progress` 后累计连续。
4. `test_task_duration_log_task_pauses_even_when_pool_untrusted`
   - 日志型条目（`viaPool=false`）在 partial 拍缺席日志时仍暂停；
     重接后 `total` 清零。
5. `test_task_duration_quest_removed_closes_span_and_discards_on_reaccept`
   - 不跑快照，直接 `fire(QUEST_REMOVED)`；重接后 `total` 从 0 开始；
     `Finalize` 只算重接后的时间。
6. `test_task_duration_quest_removed_before_finalize_keeps_the_span`
   - 事件先于交割到达：闭合区间后 `Finalize` 仍拿到完整时长；重复事件幂等。
7. `test_task_duration_offline_rotation_marks_abandoned_at_login`
   - 登出暂停（`start=nil,leftOnline=false`）；登录后可信缺席那一拍补标放弃；
     `Accepted` 重接从 0 起算，旧 `total` 不带进新实例。
8. `test_task_duration_partial_pool_offline_pause_not_marked_abandoned`
   - 池型登出暂停在 partial 拍不许补标；`poolTrusted=true` 的缺席才补标。
9. `test_task_duration_last_is_epoch_only`
   - 混合 epoch / uptime 行：只取 epoch 中最新的一笔；只有 uptime 时返回 nil。
10. `test_task_duration_formats_hours`
   - `11412` → `"3时10分"`；其余档位保持。
11. `test_migrate_clears_legacy_fortify_reg_keys_once`
   - 以 `schema=12` 冒充 0.7.3 已升级存档；90573-90576 的遗留键被清、
     其它 `regKey` 不动、第二遍为 0。

修改：

12. `test_config_init_repairs_non_table_fields`：增加 `durations = "坏"`，断言被重建。
13. `test_task_duration_tracks_pool_only_world_quest_and_excludes_aqir`：空池暂停那一拍
    显式传 `poolTrusted=true`，并断言 `start=nil/total/leftOnline`，
    否则在 partial 口径下暂停分支根本没被执行（假通过）。

必须继续通过：现有 6 条 `task_duration_*` 用例。

## 验收

- `python tests/run_gql_tests.py --syntax`
- `python tests/run_gql_tests.py -k task_duration,config_init_repairs,migrate_clears`
- 全量 `python tests/run_gql_tests.py`
- 不新增依赖；不改动与本次无关的口径、注释格式。

## 状态

- [x] 实施
- [x] 自测（`python tests/run_gql_tests.py --syntax` → 574/574）
- [x] Review（三轮 SubAgent 评审；P1 已修：v13 独立迁移档、可信池暂停补断言；
      离线轮换补标放弃 + `QUEST_ACCEPTED → Accepted`；不采纳 `QUEST_ACCEPTED → fromLog=true`，
      理由已写进 Sync.lua 注释。评审提出的"Accepted 条件过宽"经核对不成立：
      `QUEST_ACCEPTED` 只在新接取时发，不会给已有任务补发，注释已写明依据）
