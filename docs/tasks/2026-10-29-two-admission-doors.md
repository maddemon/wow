# 计划：准入门收到两道 —— "本轮交过"整体下线（狩猎：坚韧猎人 91594）

> 状态：**已实施**（第 39 轮，2026-10-29 定稿并落地；下方 §〇 记录落地与本文的偏差。
> 2026-11-02 复核补一句：判据、迁移、取证面都在工作区跑通了，**但这批改动至今没提交**——
> HEAD（`5cc9e5b`）那一份还是 `Config.SCHEMA_VERSION = 10` 加 `L.TurnInAdmits` 在位，
> "已落地"只到工作区这一层）。
> 用户 2026-10-29 定稿口径（原话）：
> "什么时候也没说做过就是准入门槛。1、匹配关键词的任务 2、金币奖励满足的任务"

## 〇、落地情况（2026-11-02 逐条 grep 复核；引用一律"文件 + 符号名"，正文里的死行号已整批去掉）

本文正文原来那些"文件:行号"落地后全漂了，一律不重新对号，改成符号引用（§三/§五 的判据照旧可 grep）。

已核实到的现状：

- `Config.SCHEMA_VERSION`（GoldQuestLedger/Config.lua）= **11**，`Config.MIGRATIONS[11]` 接
  `Ledger.ClearAdmittedKeysOnce`（GoldQuestLedger/Ledger.lua）：逐条 `rec.admittedKey = nil`、返回清掉的笔数，
  笔数由 `Config.Migrate` 那句"数据升级 v%d 完成（处理 %s 条）"进通用日志 —— W3 落地。
  **但只在工作区**：`git show HEAD:GoldQuestLedger/Config.lua` 那份仍是 `= 10` 且没有 `MIGRATIONS[11]`。
- `L.TurnInAdmits` 的函数本体与全部读者都删了：Ledger/Dump/Matrix 三个文件的**代码**（剥掉注释后）0 命中；
  tests/run_gql_tests.py 里没有任何调用点，只剩注释与文档串提到这个名字（`grep "TurnInAdmits("` 0 命中）。
  这个 0 命中由静态守卫用例 `turnin_is_not_an_admission_door` 逐字盯住。名字今天剩下的两处痕迹：
  `L.TurnInAdmits` 原位那段"【第 39 轮整块删除】"说明，以及 docs/ 下几篇旧计划
  ⇒ "全仓连测试都搜不到"这一句要按这个折扣读。
- `L.RowAdmitted`（GoldQuestLedger/Ledger.lua）签名收成 `(db, questID, rec, money, kwHit, floorMoney, now, curKey)`，
  `turnedIn`/`gainOf` 两个参数与"本轮交过"那一支一并去掉，判据只剩 §三 W1 那三行、两类依据。
- `L.GoldVerdict`（GoldQuestLedger/Ledger.lua）函数仍在、三态口径未变，只是不再被任何准入门调用
  （今天的读者是 `L.GoldQuestIDs` 与用例）。

落地时与本文不一致的地方（照实记，不改当时的判断）：

- **§二① 引的那段旧代码与仓库里能查到的"改前"不是同一个形状**：本文引的是两参数版
  （`TurnInAdmits(turnedIn[questID], GoldVerdict(...) == true)`）。可 HEAD 里被本轮删掉的实际是第 35 轮的三参数版
  `L.TurnInAdmits(on, gain, noGold)` = `on` 且 `not (noGold and gain == 0)`，调用点传的是 `GoldVerdict(...) == false`。
  两参数的调用点在任何一次提交里都 grep 不到（跟 `admittedKey` 一样，只存在于当时那片未提交的工作区，
  也可能是本文抄了更早一代的引文 —— 这一点仓库给不出答案）。两代都只问"读到过金币没有 / 接和交两处有没有都答 0"、
  **都不问够不够门槛** ⇒ §二① 的结论不动，引文按三参数那一版读。
- **§二② / W2 / W3 说的准入粘滞 `rec.admittedKey` 从未进过任何一次提交**：`git log -S "admittedKey"` 跨全历史
  0 命中，HEAD 的 `L.MatrixRows` 既没写点也没有"或 `rec.admittedKey == curKey`"那一半。本文引的"改前那两行"
  只存在于 2026-10-29 当时那片未提交的工作区。真实存档（`WTF/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua`）
  不在仓库（今天连 `WTF/` 都没有）⇒ "91594 的 `admittedKey` 此刻就在存档里"这一条离线核实不了，本文不替它背书。
- **W2 "从 `QUEST_RECORD_FIELDS` 里去掉 `admittedKey`" 未照删**：`admittedKey` 仍留在
  `L.QuestRecordFields`（GoldQuestLedger/Ledger.lua）。写点与读点确实都删了（守卫
  `turnin_is_not_an_admission_door` 直接检查 `L.MatrixRows` 函数体里不许出现 `TurnInCred`/`admittedKey`），
  清单保留这一项只为两件事：字段清单守卫 `quest_records_only_come_from_the_factory`（tests/run_gql_tests.py）
  不许把 v11 迁移自己的 `rec.admittedKey = nil` 判成"清单外写法"，以及给老存档残留留一个能被 v11 清到的名字。
- **W2 的 `TurnInCred` 预扫整趟删除落地**：`L.MatrixRows` 不再每帧翻 `db.turnins`；
  `L.TurnedInThisRound` / `L.TurnInGainsThisRound`（GoldQuestLedger/Ledger.lua，`TurnInCred` 的两枚薄壳）留着，
  只服务取证面与那一格的勾。
- **W4 落地，但"在列靠"不是单纯删一支**：`offListReason`（GoldQuestLedger/Dump.lua）里"本轮交过不作数"那一岔
  连同"五道准入门都没过"那句一起删掉，换成两类依据的白话 + "做过/交过不作准入门槛"；"在列靠"
  （GoldQuestLedger/Dump.lua 的 `/gql quest` 报告段）只报玩家点名 / 奖励金币≥门槛，交过那笔**另起一行**
  "交割：本轮有角色交过（只解释那一格的绿勾从哪来，不作在列依据）"。这个形状由用例
  `dump_names_the_non_gold_turnin_rejection` 钉住（既要求"在列靠"里不许出现交过，也要求"交割"那行说清绿勾出处）。
- **W4 里 Probe 那句条件话术落了代码、没有用例**：`金币读者`（GoldQuestLedger/Probe.lua）现在只在金额真够门槛时
  才说"矩阵那一行就是靠它们进列的"，否则只报"本轮读到金币的号：…"。tests/run_gql_tests.py 里没有任何断言盯这一句
  的两种分岔 ⇒ §五 T5 后半"括号在金额门未放行时不声称因果"**没钉住**，只算落了代码。
- **①那一支不是本轮写的**：`L.KeywordLogAdmits`（"这一条实例被某个号的日志证明存在过"）在本轮之前就提交了
  （HEAD `5cc9e5b`，邻单 docs/tasks/2026-10-29-keyword-log-weekly-resident.md），本轮只动了中间那一支；
  §三 W1 的代码样例与今天的 `L.RowAdmitted` 函数体逐行一致。
- §五 T1–T4 的用例逐条对得上名字：`a_34_gold_turned_in_row_is_not_listed`（T1，91594 形状）、
  `gold_above_floor_admits_row`（T2）、`double_mode_row_survives_without_admission_sticky` +
  `turnin_is_not_an_admission_door`（T3）、`v11_clears_the_admission_sticky_once`（T4；
  本文 T4 写的是"跑 `Config.Init`"，用例实际直接跑 `Config.Migrate(Config.DB())`，档位/幂等/别的字段不动三条边界都钉住了）。
- **§五 T6 只达成一半**：`python tests/run_gql_tests.py` 2026-11-02 本会话连跑四次，三次 **408/408 全绿、
  退出码 0**（中途采到一次 407/408 —— 同一时间这片工作区的 `tests/run_gql_tests.py` 还在被另一路改动，
  重跑即回绿，不记成本轮判据的账）。本文原先写的"本轮基线 408/408"分不清是当天还是今天，只留今天这几笔实测。
  另一半"真档只读回放 + 列出掉板行清单交用户核对后才提交"**没做完**：`WTF/` 不在仓库，回放没有可复现凭据，
  仓库里也从没留过那份清单；`tests/tools/` 下散着的一次性取证脚本（未入库）`tmp_gate_probe.py` 还按**旧签名**调
  `L.RowAdmitted`（把 turnedIn 摆在第 6 位），今天照着跑只会拿到错位的答案。

## 一、现象与取证（12.1.5 真档，今天现场 `/gql quest 91594` + `/gql dump`）

`狩猎：坚韧猎人` questID=91594，永歌森林(2395)，板面倒计时还剩 8 时 54 分：

```
矩阵：在列
现在：不在池里（刚问到的 21 条里没有它）  当前角色日志：没接
在列靠：本轮有角色交过（奖励金币被读到过，钱窗实测到手 3416 仅供参考）
账本金额：lastMoney=341600  本轮 observation.money=341600  门槛=5000000（不到门槛又不命中关键词就不列）
```

三个事实摆在一起就是缺陷本身：

1. 金额 **341600 铜 = 34 金 16 银**，门槛 **5000000 铜 = 500 金**，差 14 倍 ⇒ 金币门**没放行**。
2. 报告里没有"命中关键词「…」"那一行 ⇒ 关键词门**没放行**。
3. 顶住这一行的只有"本轮有角色交过"（爱情之力 2 小时前那一笔，编号 `91594:1992572` 对上当前）。

同一条报告的 probe 段自己写着"不到门槛又不命中关键词就不列"，而判据实际走的是第三条路
⇒ **判据和它自己的取证话术打架**，玩家照报告理解规则必然被误导。
`金币读者：…（本轮读到金币的号：矩阵那一行就是靠它们进列的）` 这一句同样是谎报：金额门根本没进列。

## 二、根因（两处，缺一处就是"改完没变化"）

### ① 交过门只问"读到过金币没有"，从不问"够不够门槛"

`L.RowAdmitted`（GoldQuestLedger/Ledger.lua，改前的旧签名带 `turnedIn` 参数）第二支：

```lua
if L.TurnInAdmits(turnedIn and turnedIn[questID],
    L.GoldVerdict(db, questID, now, curKey) == true) then return true end
if money and money >= (floorMoney or L.MatrixMoneyFloor()) then return true end
```

`L.TurnInAdmits`（改前住在 GoldQuestLedger/Ledger.lua 里 `L.TurnedInThisCycle` 那两枚薄壳下面，
**落地时已整块删除**；上面引的两参数形状是第 34 轮那一代，本轮真正删掉的是第 35 轮的三参数版 —— 见 §〇）
= `on` 且 `goldConfirmed`；而 `L.GoldVerdict`
（GoldQuestLedger/Ledger.lua，函数仍在、三态口径未变，只是不再被交过门调用）答的是**三态**"是不是金币任务"（正数→true / 0→false / 读不出→nil），
**金额大小不在它的口径里**。所以 34 金这种"客户端明说给钱、但远不到门槛"的行，
一过交过门就整段跳过金币门。注释里那句"够不够 500 金门槛是金币门的事，与交过无关"
（随 `L.TurnInAdmits` 一起删掉，见 GoldQuestLedger/Ledger.lua 原 `TurnInAdmits` 处）就是这个设计的自白 —— 它把"做过"变成了一张免门槛通行证。
（2026-11-02 复核：`goldConfirmed` 与"够不够 500 金…"两个串**在任何一次提交里都 grep 不到**，
只存在于 10-29 当时那片未提交的工作区；原文照留，不作为仓库事实。）

### ② 准入粘滞 `rec.admittedKey` 会把①的放行结果冻进存档

`L.MatrixRows`（GoldQuestLedger/Ledger.lua，下面是**改前**那两行粘滞写法）：

```lua
if admittedNow and curKey then rec.admittedKey = curKey end
... and (admittedNow or (curKey and rec.admittedKey == curKey)) then
```

粘滞和准入门是**或**的关系，而且每帧重烙。91594 这一条实例的 `admittedKey` 此刻就在存档里，
**只删①的话它照样留板到 8 时 54 分到期** —— 正是第 34/36 轮那一族"改完读成没变化"的形状。
（2026-11-02 复核：上面这两行粘滞写法**从未进过任何一次提交**（`git log -S "admittedKey"` 全历史 0 命中），
只存在于当时那片未提交的工作区；真实存档不在仓库，"此刻就在存档里"这一句离线核实不了，详见 §〇。）

## 三、修法

### W1 准入门收成两道（`L.RowAdmitted`，GoldQuestLedger/Ledger.lua）

删掉①那一整支，`turnedIn` 参数一并从签名里去掉（调用方 MatrixRows / Dump 跟着改）：

```lua
function L.RowAdmitted(db, questID, rec, money, kwHit, floorMoney, now, curKey)
    if not rec then return false end
    -- ① 玩家点名：关键词从日志收的（当前号挂着 / 这一条实例被某个号证明存在过）、注册周常
    if rec.kwLogAdded or rec.regKey
        or L.KeywordLogAdmits(db, questID, rec, now, curKey) then return true end
    -- ② 奖励金币读到过且够门槛（QuestMoney 自带按实例的双模式粘滞）
    if money and money >= (floorMoney or L.MatrixMoneyFloor()) then return true end
    -- ①的另一种形状：名字命中玩家关键词 + 客户端答得出至少一条凭据（第 32 轮口径不动）
    return not not (kwHit and L.QuestHasCredential(db, questID, now, curKey))
end
```

三道写在一起，但只有两类依据：**玩家点名（关键词/注册）** 与 **奖励金币≥门槛**。
`L.TurnInAdmits` 函数整体删除（判据不留死码），`L.TurnedInThisRound` /
`L.TurnInGainsThisRound` 留着给取证面解释"那一格绿勾是哪来的"，不再决定任何一行在不在。

### W2 删掉准入粘滞

- `QUEST_RECORD_FIELDS` 里去掉 `admittedKey`（GoldQuestLedger/Ledger.lua 的字段清单 `L.QuestRecordFields`）—— **落地时改成**：写点与读点全删，但字段清单里**保留** `admittedKey`（见 §〇 偏差：留给字段清单守卫 + v11 迁移自写 `= nil`）；
- `L.MatrixRows`（GoldQuestLedger/Ledger.lua）删写点与"或"那一半；
- `L.MatrixRows` 里那趟 `TurnInCred(db, keyNow, keyOf)` 预扫整体删掉 —— 两个返回值
  （`turnInOf`/`gainOf`）在本轮之后都没有读者（`gainOf` 现在就已经是死变量）。
  **每帧少扫一遍 `db.turnins`，零新增接口调用。**

双模式不掉行仍成立，不靠粘滞：`L.QuestMoney`（GoldQuestLedger/Ledger.lua）认
`rec.goldKey == curKey` 的按实例金币，一档读到 962 金、切回另一档读到 0，
门槛那道照样拿到 962 ⇒ 由②放行。这一条必须有用例钉住（见 §五 T3）。

### W3 schema v11 一次性清粘滞（`L.ClearAdmittedKeysOnce` + `Config.SCHEMA_VERSION` 10→11）

代码删了字段用法，**存档里已经写进去的那一份不会自愈**（v2/v6/v7/v9/v10 同一族教训）。
不清的代价就是 §二②：91594 还会留板到到期，用户读成"改完没变化"。

- 逐条 `db.quests[*].admittedKey = nil`，返回清掉的笔数（进 `Config.Migrate` 通用日志）；
- 闸门只认 `db.schema` 档位，幂等（重跑 0 条）；
- **不碰任何其它字段**：金币粘滞 `goldMoney/goldKey`、日志凭据 `kwLogKey`、角色快照一律不动。

### W4 取证面改口（`Dump.lua`）

- `offListReason`（GoldQuestLedger/Dump.lua 的 `/gql quest` 段）删掉"本轮交过不作数：这一轮没读到过它的奖励金币"那一岔
  （它存在的唯一理由就是解释①为什么否决）；"五道准入门都没过"改成两道口径的白话：
  **不是关键词/注册点名要的，奖励金币也没到 X 金**。—— 落地照此，末尾还补了一句"做过/交过不作准入门槛"。
- "在列靠"（GoldQuestLedger/Dump.lua 同一报告段）删掉交过那一支 —— **落地时改成**：交过那笔没抹掉，
  另起一行"交割：本轮有角色交过（只解释那一格的绿勾从哪来，不作在列依据）"，形状由用例
  `dump_names_the_non_gold_turnin_rejection` 钉住（详见 §〇）；
- `金币读者：…` 那句括号改成条件话术：只有金额门真放行时才说"矩阵那一行就是靠它们进列的"，
  否则只报"本轮读到金币的号：…"，不替判据编因果；—— 代码在 `Probe.lua` 落了，**没有用例钉住**（见 §〇）；
- 扫净界面与 probe 里其它指向"做过就占行"的文案（Matrix.lua 悬停、Options.lua）。
  （2026-11-02 复核：这一条**核实不了也无需再动** —— 仓库里搜不到叫"扫净界面"的入口；
  矩阵悬停那句"这行还在列是因为 X 本轮读到了金币 Y"早在第 33 轮就删了（`Matrix.lua` 里有记录），
  `Matrix.lua`/`Options.lua` 今天也没有"做过就占行"式文案 ⇒ 按"无残留"收口，出处未能核实。）

## 四、代价（照实记，不当选项端给玩家）

1. **做过≠看得见**：一条 34 金的板面任务做掉了，矩阵不再为它留一行；那一格的绿勾仍在
   （勾来自带编号的 `views`/`turnins` 凭据，本轮不动），只是行归到"够门槛或点名要"两类之外。
   想留行的正路是加关键词或调门槛，两条都是配置项。
2. **奖励金额始终读不出的金币任务不上板**（松露大乱斗那一族）：本轮起它只能靠关键词/注册进门。
   这一条不是本轮新引入的，是 10-29 上午"读不到金币奖励就不上榜"那一刀的既有口径，本轮只是
   不再让"交过"从后门把它捞回来。
3. 作废两条留档口径：**第 25 轮"任一角色完成过就至少加入矩阵"**、
   **第 35 轮"接和交两处客户端都答 0 才否决"**（`TurnInAdmits` 的两代形状）。以本轮为准。

## 五、任务拆分与验收

| # | 任务 | 验收 |
| --- | --- | --- |
| T1 | W1 准入门收两道 + 删 `TurnInAdmits` + 改签名 | 用例：交过 + 读到正数 34 金 + 不撞关键词 ⇒ **不列**（91594 形状，编号/流水/快照全按真档摆） |
| T2 | W1 的另一半：交过 + 金额≥门槛 ⇒ 列（走金币门，不是走交过） | 用例钉两条路的归属 |
| T3 | W2 删粘滞 + 删 `MatrixRows` 里的 `TurnInCred` 预扫 | 用例：双模式一档读到 962 金、另一档读到 0 ⇒ 仍列（不依赖 `admittedKey`）；静态守卫：`MatrixRows` 源码里不再出现 `TurnInCred`/`admittedKey` |
| T4 | W3 v11 迁移 | 用例：预置带 `admittedKey` 的存档跑 `Config.Init` ⇒ 全清 + 只跑一次 + 别的字段一字不动 |
| T5 | W4 取证与界面文案 | 用例：`/gql quest` 对 91594 形状的输出里不出现"本轮有角色交过"当在列依据；`金币读者` 括号在金额门未放行时不声称因果 |
| T6 | 全量测试 + 真档只读回放 | `python tests/run_gql_tests.py` 全绿；回放脚本 diff 新旧判据，**列出掉板行清单**（含每行原来靠哪道门）交给用户核对后才提交 |

回放基线：今天这份 `WTF/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua`。
预期掉板：`狩猎：坚韧猎人` 这类"金额<门槛 + 不撞关键词 + 本轮交过"的行；
`≥500 金`、关键词命中、注册周常（丰饶供品/加固符文石/至暗之夜：世界首领…）三类**一行不许掉**。

落地核对（2026-11-02 实跑/grep，逐条对应见 §〇）：T1–T4 的用例都在；**T5 只落了前半**
（后半"金币读者括号的因果话术"没有用例）；**T6 只落了一半** —— `python tests/run_gql_tests.py` 今天连跑几遍
408/408 全绿、退出码 0（中途一次 407/408 是工作区同时被改到的时点，重跑回绿），但真档只读回放没做：
仓库里没有 `WTF/`，那份基线存档取不到，预期掉板清单也从没在仓库留过一笔，
"交给用户核对后才提交"这一步不成立。

## 六、提交纪律

工作区当前已有一批**未提交**改动（`Ledger.lua`/`Sync.lua`/`Dump.lua`：交任务钱窗金额退役，
即"不再用交任务获得的奖励金币作依据"那一刀）。本轮改动叠在它上面，提交前必须
`git diff` 逐行确认两批是否要拆成两个 commit，不许顺手把别会话的在途改动一起提交。

（2026-11-02 复核：这一刀**到现在也没提交**，而且叠在上面的批次更多了 —— HEAD 仍是 `5cc9e5b`
（那份里 `Config.SCHEMA_VERSION` 还是 10、`L.TurnInAdmits` 还在），工作区除本轮外还多了
`GoldQuestLedger/AqirInvasion.lua`、`GoldQuestLedger/Defaults.lua` 两个新文件与 11-02 那批
事件 pin 归属的改动 ⇒ 已经不止两批，拆分压力比本文写时更大。）
