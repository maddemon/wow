# 金币按号归属（97128「到手 1089」借号）

日期：2026-11-03　靶心：用户 11-03 截图 —— 97128 麦德的格子悬停印「风行者 · 麦德 交于 11-03 09:16 到手 1089🪙」，
而麦德这一号做它**不给金币**（给物品），1089 是另一个号本轮同步时读到的钱。

## 一、缺陷形状（先说清病在哪）

> 本节行号是**改前**位置（写计划时对着当时的工作树核的）；落地后的行号见第五节。

金币在账本里有两份数据源，语义不同：

| 数据源 | 存法 | 唯一写点 | 语义 |
|---|---|---|---|
| 按号 | `db.chars[号].reads[questID] = { g, t, k }` | `Ledger.SetRewardRead`（Sync 池循环） | **这一号**这一副奖励读到多少：正数=给这么多；0=客户端明说这号不给金币；没有这一笔=这号在这条实例上没读过 |
| 行级 | `rec.goldMoney` + `rec.goldKey` | `Ledger.NoteGoldThisInstance`（Sync 池循环） | 这一条实例里**任意一个号**读到过的正数（谁最后同步就覆盖成谁的） |

两处都在同一次同步、同一条 `pool[i].money` 上写（`Sync.lua:1020` 与 `1026`），**改显示口径不新增任何接口调用**。

消费侧口径不一致：

- ✓ `Matrix.lua:540`（悬停上面那行金额）先问按号那份 —— 对
- ✗ `Matrix.lua:602` → `Ledger.LastCompletion:1085`（「交于…到手 X」）只比 `rec.goldKey == 这笔流水的实例编号`，
  **压根没读按号那份** → 把别的号的钱挂在这一号名下
- ✗ `Ledger.WeekStats:1787`（矩阵列头「本周金币」）同一句 → 白干的号也加 1089，**总额虚高**
- ✗ `Matrix.lua:574` 用同一个借来的正数抑制掉「这号做它不给金币」那句 → 界面上既没真答案、又只剩假归属

号级自己还有一处保护缺失（用户 11-03 的提醒：同一任务不同模式（是否狩猎）奖励不同）：

- 行级：`Ledger.lua:729` 只在 `money > 0` 时写 → 切狩猎模式读到 0 冲不掉已读到的正数（双模式不掉行靠它）
- 号级：`Ledger.lua:1359` 无条件覆盖 → 同号同一条实例，普通模式读到 1089 后切狩猎模式同步一次，读数被写成 0，
  格子从「1089」翻成「这号不给金币」，周汇总跟着掉

## 二、口径（定稿，实施不许自创）

1. **号级补正数保护**：同一 charKey + 同一条实例编号内，已有正数时新的 0 不许覆盖它；
   新的正数照旧覆盖（0→正数、正数→正数都放行）；编号不同照旧整笔重写（旧实例的数不给新实例背书）。
   纯账本一次比较，零接口调用。
2. **某一号的到手只由这一号的读数决定**：`LastCompletion` / `WeekStats` 的实测分支改问 `reads`，
   编号用**那笔流水自己的 `r.k`**（不是"当前编号"）—— 交任务那一刻挂在哪条实例，就取那一条实例上这号读到的数。
3. **没读过就空着**（用户 11-03 定「可以」，宁缺不编）：这号在这条实例上没有读数 ⇒ 不印别人的钱。
   于是格子上：`reads` 缺失 → 上面那行金额报「金额未知」（不再退回行金额）、「到手 X」整段不印。
   行级金额继续服务它本来的两件事：**行不掉了 / 金币门槛**（`QuestMoney`、`GoldFromOtherMode` 语义不动，
   只是不再冒充某一号的到手）。整行热区（没有 charKey）的悬停照旧印行金额。
4. **读到 0 就说 0**：`own == 0` 且这一条实例真有凭据（done）→ 印「这号 这轮做它不给金币」，
   「交于 …」后面不带钱。第 2 条改完，`gain` 本身就是按号的，`Matrix.lua:574` 那句抑制条件自然不再命中
   （借来的正数没了），不需要为它另开分支。
   代价照实：关键词类「没做完读不到钱、交完就离开板面」的那笔旧 0 会显成「不给金币」——
   那是数据源本身的局限，归 `/gql quest` 的「金币读者」取证，界面不替客户端圆场。
5. **补录那一路不动**：`Matrix.lua:290-301` 已经是按号优先、别人读过就留 `?`；`LastCompletion` 的补录分支
   读的是这一号自己写下的 `manual.g`，本来就按号。
6. **`TurnInCred` 的 gain 不动**（`Ledger.lua:2165`）：它按 questID 聚合、号在这一趟被 `for _, byQuest` 丢掉，
   天生是行级口径，只喂 `/gql quest` 与周汇总取证；注释里写明「这一条实例读到过的金币，不是某一号的到手」。

## 三、老数据

- 已被切模式冲掉的 `reads`（本 CD 内先读到正数、后读到 0 的号）**补不回来**：钱窗数据源已经过去了。
  下一次同步（这号还在这条实例的板面上时）自然按新保护重写；换实例后一起作废。
- `reads` 缺失的老流水行：「到手」从此不印（第 3 条）。这是把假数去掉，不是丢数据。
- 不升 schema、不迁移：`reads` 的行文形状（`{g,t,k}`）一个字不改，只是写侧多一道保护、读侧多问一处。

## 四、任务

- **T1** `Ledger.SetRewardRead`（`Ledger.lua:1350`）：同 charKey 同编号内正数保护（第 1 条）。
  注释写清「行级那份的保护在 `NoteGoldThisInstance:729`，这一处以前没有 ⇒ 切模式会把真读数冲成 0」。
- **T2** `Ledger.LastCompletion`（`Ledger.lua:1076`）：实测分支 `gold` 改 `L.CharRewardMoney(db, charKey, questID, nil, r.k)`；
  `r.k == nil` 时按「没编号 = 不匹配」回 nil；函数头注释改口（金额从此是按号的）。
- **T3** `Ledger.WeekStats`（`Ledger.lua:1787`）：同一改法（`CharRewardMoney(db, charKey, questID, nil, r.k)`），
  正数才加进 gold 并置 `goldKnown`；nil/0 不计金额（完成数照计）。
- **T4** `Matrix.ShowMoneyTooltip`（`Matrix.lua:535-583`、`599-615`）：格子上不借行金额（第 3 条）；
  「到手」只在 `gain` 为正数时拼（0 与 nil 都不拼）；保留 `fmt` 问不出就少这一行的守卫；
  注释里那句「上面是 xx 读到的」的旧口径按新分岔改口。
- **T5** 注释与文档口径：`Ledger.lua:2128-2133`（TurnInCred gain 三条优先级）、`QuestMoney`/`GoldFromOtherMode`
  函数头、`Config.lua:26-29`（存档注释补「正数不被同实例的 0 冲掉」）、`Sync.lua:1017-1026` 写侧说明。
- **T6** 用例（`tests/run_gql_tests.py`，都要进 runner 清单）：
  1. `test_char_reward_read_positive_survives_mode_flip` — 同号同编号：1089 后读到 0 ⇒ 仍 1089
  2. `test_char_reward_read_zero_then_positive` — 0 后读到正数 ⇒ 更新为正数
  3. `test_char_reward_read_new_instance_replaces_value` — 换实例编号 ⇒ 旧的 1089 不作数（`CharRewardMoney` 回 nil）
  4. `test_last_completion_gain_is_per_char` — 麦德 reads=0、`rec.goldMoney`=1089（别的号读的）⇒ 到手 nil；
     reads=1089 ⇒ 到手 1089
  5. `test_last_completion_gain_uses_turnin_instance_key` — 流水挂上一条实例的编号、reads 挂当前编号 ⇒ 到手 nil
  6. `test_week_stats_gold_excludes_other_chars_read` — 别的号读到金币、本号 reads 缺失/0 ⇒ 本号本周金币不含那笔
  7. `test_week_stats_gold_counts_own_read` — 本号 reads=1089 ⇒ 含 1089
  8. `test_tooltip_cell_without_char_read_says_unknown` — 格子金额行报「金额未知」，不印别人的钱
  9. `test_tooltip_row_hotzone_still_shows_row_money` — 无 charKey 的行级悬停照旧印行金额
  10. `test_tooltip_zero_read_prints_no_gold_line` — own=0 且 done ⇒ 印「这轮做它不给金币」、交割行不带钱

验收：`python tests/run_gql_tests.py` 全绿（基线 417/417，2026-11-03）。

## 五、落地（2026-11-03）

T1-T4 按口径落，T5 的 Sync.lua 那一项**取消**（工作树里另有会话未提交的 `ForceMapDataLoad` / `LogBypass` 改动撞在同一个文件，写侧说明改写在 `Ledger.NoteGoldThisInstance` 与 `L.SetRewardRead` 的函数头），其余注释落点不变：

- T1 `Ledger.lua:1394-1399`（保护句 `money == 0 and SameInstanceKey(old.k, curKey) and old.g > 0 ⇒ 不写、回 false`）
- T2 `Ledger.lua:1109-1111`（`r.k ~= nil` 显式分岔后 `CharRewardMoney(db, charKey, questID, nil, r.k)`）
- T3 `Ledger.lua:1834-1840`（同一改法，正数才计并置 `goldKnown`；尾部 `-1` 那句一字未改）
- T4 `Matrix.lua:544-553`（`own`/`shown` 显式 if 分岔）、`:634`（`gain > 0` 才拼「到手」）、`:572-596` 只改注释、守卫保留
- 新增 10 条用例在 `tests/run_gql_tests.py:9245-9553`，全部进 runner

动到的既有用例（逐条核过，**没有放宽任何断言**）：

- 四条只补夹具：`turnin_state_and_week_stats`、`manual_not_double_counted_with_turnin`、`weekstats_unknown_when_clock_domains_split`、`matrix_columns_stale_and_manual_only_quest_kept` —— 老夹具只演了行级那一步（`NoteGoldThisInstance`），而真实同步在同一条 `pool[i].money` 上还写号级那一份（`Sync.SnapshotNow` 池循环里 `L.NoteGoldThisInstance` 与 `L.SetRewardRead` 相邻那两行）；第一条另**新增**一句"行级那份不许冒充本号本周金币"（收紧）。
- 一条被推翻并改名：`turned_in_with_a_gold_read_retracts_the_no_gold_line` → `turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line`。它钉的正是 §一 列为 ✗ 的那句抑制（拿别人的 2068 盖掉"这号不给金币"），§二第 4 条「代价照实」要求反转；同时补一句 `QuestMoney` 行级 2068 仍在岗的断言。
- `unsynced_cell_gets_no_attribution_line` 的说明文字按新行为改口（断言本来就只查"不许出现归属解释/不给金币"，未动）。

取证面复核：`Dump.lua:362-369` 那句本来就写成"本轮有角色交过 … 这一条实例读到的奖励金币 X 仅供参考"，主语是行不是号，不受这次影响；`L.TurnInGainsThisRound` 按计划 §二第 6 条留在行级，注释已标明读它的人那句话答的是哪一问。

老数据一条如实交代：**被切模式冲掉的 `reads` 补不回来**（钱窗数据源已经过去），下一次同步自然按新保护重写；老存档里带着借号金额的手工补录（`manual.g`）仍会印一次"到手"，那是补录分支按号存下的历史值，不迁移。

验证：`python tests/run_gql_tests.py` = **427/427**、`python tests/run_tests.py` = 77/77；负向验证逐处 mutate（撤 T1/T2/T3/T4 各红在对应断言，撤 T2 复现截图原句「… 交于 … 到手 1089…」）。
