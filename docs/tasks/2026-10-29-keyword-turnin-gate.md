# 修复：不上板的关键词周常，交任务整笔不记（回响复燃 92713）

> 状态：计划（2026-10-29）。三刀：记账门同源 → 交割当场落账 → 琥珀不压真流水。

## 现象（玩家 2026-10-29 回传截图 + `/gql quest 回响复燃` 报告）

- 回响复燃 92713 一行 14 个号，6 个号真做完了，界面只有 1 个绿勾（圣光之力）、4 个琥珀勾（他自己只点过 2 个，另 2 笔是昨天调查前就有的手点）、**魔瘾者等 5 个号什么都没有**。
- 报告的凭据段：只有圣光之力有 `流水1笔 k=92713:1993148 ✔对上当前`；其余 13 个号只有一份 `快照=done k=nil`（编号模型之前的残留，永不匹配）。
- **玩家明确否掉"下线丢账"这个解释**：做这条任务不需要下线，他也没下线。所以昨天那单的登出冲窗不是这次的病根。

## 根因（两道门不同源 + `kwLogAdded` 随角色翻面）

报告里那行 **`在列靠：命中关键词「回响复燃」`** 后面**没有** `关键词从日志收的` ⇒ 这条记录此刻 `kwLogAdded = nil`。再配上 `上过板=否`、`是世界任务=否`、`金币记在=（没记）`、`无图`、`regKey` 也没有：

| 门 | 读什么 | 92713 现在 |
|---|---|---|
| 记不记这笔交割 = `L.QuestKnown`（[Ledger.lua:1344](file:///d:/Repos/wow/GoldQuestLedger/Ledger.lua#L1344)，被 [Sync.lua:1210](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L1210) 当开门凭据用） | 只认 `poolSeen / onBoard / regKey / kwLogAdded / ignored` | **五样全空 ⇒ 钱窗不开，一笔不记** |
| 这行在不在矩阵 = `L.QuestHasCredential`（[Ledger.lua:1735](file:///d:/Repos/wow/GoldQuestLedger/Ledger.lua#L1735)） | 上面那几样 **再加"本轮有号交过"** | 靠圣光之力那一笔流水撑着，所以行在列 |

一门认交割、另一门不认 ⇒ 这行显示得出来，但小号交任务永远记不上。

`kwLogAdded` 为什么会翻成 nil：它是**每次同步按"当前这个号的任务日志里正挂着它没有"**续期的，没挂着就当场清掉（[Sync.lua:256-258](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L256)）。小号登录时任务还没接 ⇒ 登录那趟同步把标记清 nil ⇒ 之后"接了立刻交"：`QUEST_LOG_UPDATE` 触发的快照在矩阵没开时直接不跑（[Sync.lua:903](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L903)），标记一路是 nil ⇒ 交割被门挡在门外。圣光之力那次是接完之后跑过一趟同步（登录同步或开着矩阵），标记亮着，所以记上了。

**这正是"接到后立刻就交"的真实失手方式**：不是事件时序丢了，是这条路对"不上板的关键词周常"根本不开门。

## 改动

### 刀 1：记账门与准入门同源（本次事故根因）

`L.QuestKnown` 加一条身份：**这条记录的名字命中玩家自己的关键词**。

- 关键词是玩家亲手声明"这类任务我要跟踪"，而交任务事件本身是客户端答的凭据 —— 与第 32 轮"撞关键词只免金币门槛、还要一条客户端凭据"不冲突，反而补上它漏的那半。
- 判据只认 `GQL.Config.KeywordMatch(rec.name)` 这一个入口，不许再拼一遍子串匹配。
- 玩家删了关键词，这道门自动关闭（不引入新字段、不留新状态）。
- 代价照实：`DropTurninsWithoutEvidence`（v3 迁移）也读 QuestKnown，从此"名字撞关键词但从没上过板"的老流水不再被当"无凭据"清掉 —— 方向是要保的（那些是真交割），记进本文。
- **不许**放宽成"账本里有记录就开门"：那正是 `test_turnin_of_unknown_quest_never_creates_a_row` 守的那条（交一条职业/战役任务凭空造行）。这条记录能进账本本来就只可能来自池/日志/注册/关键词四条路，名字撞关键词是其中玩家亲手授权的那条。

### 刀 2：交割当场落账，15 秒窗口只管金额

现在"记完成"和"量钱"绑在同一个窗口末尾（[Sync.lua:1186-1187](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L1186)），窗口一死两样一起丢（昨天靠登出冲窗补救）。改成：

- `QUEST_TURNED_IN`（真按了"完成任务"那一下）到达时**当场**写流水 + 快照 done，把那一笔的行引用留在 `capture.row`。
- 窗口到点 / 换任务 / 登出结算时：`capture.row` 已在 ⇒ **只把金额补进那一行**（新增 `L.SettleTurnInGain(row, gain, sig)`，`gain > 0` 才 `MarkGoldByObservation`），不再追加第二笔（周统计一笔交任务只能算一次）。
- 只有 `QUEST_COMPLETE`（打开交割对话框）而始终没等到 `QUEST_TURNED_IN` ⇒ 照今天的行为在窗口末尾补记一笔，并在日志里写明"窗口补记"。
  - **为什么不顺手关掉"开了对话框没交"这个假勾口子**：12.x 到底哪些任务会发 `QUEST_TURNED_IN` 没有实测依据（事件名候选表 [Sync.lua:29](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L29) 那句注释就是这个意思）。拿"没收到 TURNED_IN"当否决凭据，一旦某类任务压根不发它，就是把真交割整批判死 —— 比现在的假勾更糟。所以这一刀只加一条能分辨两条路的日志（`交割当场落账` / `窗口补记`），等 `/gql log` 里有样本了再决定要不要收紧。
- `L.OnTurnIn` 的返回值从 `true` 改成刚写下的那一行（唯一调用方是 Sync；测试直调处不受影响则不动）。
- 连带改口 [Sync.lua:806-813](file:///d:/Repos/wow/GoldQuestLedger/Sync.lua#L806) 那段注释：钱窗空档里"先 open"的说法在当场落账之后只适用于"只发了 COMPLETE"那一岔。

### 刀 3：琥珀勾不许压住同一条实例的实测流水

`L.State` 先看补录、后看流水（[Ledger.lua:1416](file:///d:/Repos/wow/GoldQuestLedger/Ledger.lua#L1416)），于是"先点一下、流水后到"的格子永久停在琥珀 —— 颜色的语义是"绿=客户端/流水认的、琥珀=我自己记的"（用户 2026-10-25/10-27 定的），流水在账上却报琥珀就是映射错了。

- 只在**补录未核验 + 补录状态是 done + 同一条实例的实测流水在**（判据与下面那支一字不差：`SameInstanceKey(last.k, curKey)` 且 `byCycle or obs`）时改回 `done, nil`（绿）。
- 其余分支一字不改：核验过的补录仍回 `verified`，没有流水背书的仍回 `manual`（琥珀），`test_manual_beats_auto_and_survives_sync`（手动不许被自动 open 顶掉）仍成立。
- 小号格子点不出绿色这件事不在本刀范围内（客户端只答当前角色，[Matrix.lua:275](file:///d:/Repos/wow/GoldQuestLedger/Matrix.lua#L275)），本刀只保证"账本里有真凭据时颜色不许降级"。

## 用例（`tests/run_gql_tests.py`，全部走真实事件入口，不直调判据）

1. `test_keyword_weekly_turnin_gets_recorded` —— 造 92713 的形状：不在池里、`IsWorldQuest` 回 false、名字命中关键词、`kwLogAdded` 为 nil。`fire(QUEST_COMPLETE)` → 窗口结算 ⇒ 有流水 + 快照 done；对照组：名字不命中关键词的陌生任务仍一笔不记（`test_turnin_of_unknown_quest_never_creates_a_row` 必须继续绿）。
2. `test_turned_in_event_records_before_the_window_settles` —— `fire(QUEST_TURNED_IN)` 后**不跑定时器**，账本里已经有那一笔流水与 done 快照；再 `GQL_FireTimers()` 结算 ⇒ 同一笔行上金额被补上、**流水仍只有一笔**（周统计不许数两遍）。
3. `test_turnin_row_upgrades_an_earlier_manual_mark_to_green` —— 先点格子（无流水 ⇒ 琥珀），再让流水落账 ⇒ `L.State` 回 `done` 且第二返回值为 nil，界面上屏是绿勾不是琥珀。反向对照：流水编号属于上一条实例时仍琥珀。
4. 登出冲窗那条 `test_logout_within_money_window_still_records_turnin` 必须继续通过（现在走的是"当场落账 + 登出补金额"这条路，断言金额与流水笔数）。

## 验收

- `python tests/run_gql_tests.py --syntax` 全量绿（当前基线 356/356）。
- 逐行 review 三刀的 diff；`grep` 确认没有第二处自己拼关键词子串匹配、没有第二处 `OnTurnIn` 追加路径。
- 玩家侧预期：本周已丢的那 5 个号补不回来（客户端答不出小号做过没有），只能各号点一次留琥珀勾；**下个 CD 起**"接了立刻交"的关键词周常会当场亮勾。

## 不做的事

- 不动 `kwLogAdded` 的翻面语义（它是"这个号此刻日志里挂着没有"的事实，改成粘滞会绕过关键词删除的退行路）。
- 不清那 13 份 `快照=done k=nil` 残留（v9 迁移按设计只清上过板的行；k=nil 永不匹配，界面上不起作用）。
- 不收紧"开了对话框没交"的假勾口子（缺实测样本，见刀 2）。
