# GoldQuestLedger 第 36 轮计划：任务身份改成「任务号 + 结束时间」这个唯一编号，把"本轮起点"整套删掉

计划唯一真源。分支 master，动 `GoldQuestLedger/Ledger.lua`、`Sync.lua`、`Config.lua`、
`Matrix.lua`、`Dump.lua`、`Probe.lua`、`Options.lua`、`tests/run_gql_tests.py`，文档两份。
基线 **341/341**（本轮开工时工作区实测；HEAD 是第 35 轮 `dca3389`，其上还压着一份**未提交**的
"轮次键"增量，见 §十五 T0 —— 它就是本轮 W1/W2/R1/R2 的前半，本轮在它上面继续）。

## 一、用户原话与取证

12.1.5 实测（2026-10-28 早）：截图里 **举钳欢庆 94612** 一行有 10~11 个绿色/琥珀 ✓，
他的原话：

> 这个举钳欢庆，本轮次所有人都没做呢，为什么这么多√，两个没打√的角色是刚登录过的。

> 既然不是本轮次的 为什么会用上呢 这逻辑怎么写的？

> 为什么要有本轮起点的概念，我从很早就说了，一个任务id+结束时间，算他的唯一标识

**定案（他 2026-10-28 的原话，本轮判据的出处，一字不许改口径）**：

> 周常（获取不到结束时间的任务组）先按周CD的时间算结束时间。
> 其他的任务，完成时都要存这个任务唯一编号。旧的数据如果没存，都要修改。
> 这样就不会出现这个问题了。

面板清单已逐行给他核过并批准（真档 `D:/Games/World of Warcraft/_retail_/WTF/Account/BLUEFANE/
SavedVariables/GoldQuestLedger.lua`，mtime 10-28 09:07，只读回放，档一个字没改）：
**20 行里 19 行的勾全部挂得上，唯一挂不上的就是 94612**。他的判语：

> 举钳欢庆 挂不上才是正常的，因为我最开始就说了 本轮次还没人做过

97128 巢穴：尼姆瑞莎 + 90962 斯托玛兰突袭战他另问过，答复"都是周常，按周常CD" ⇒ 走周 CD 尾巴那一档。

**94612 真档形状（本轮的靶心）**

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `roundAt` | 10-22 06:59 | **与 `cycleAnchor` 逐秒相同** ⇒ 这条记录从没滚过轮 |
| `roundEnd` / `expiresSrc` | 10-31 15:00 / 接口倒计时 | 当前这条实例的真实结束时刻 |
| `turnins` | 一笔都没有 | 它不靠"本轮交过"占行 |
| `QuestMoney` | 1058 金（门槛 500 金） | 行是靠金币门槛上的板 ⇒ 清勾不掉行 |
| 10 个 ✓ 的来源 | 7 笔 10-22 快照 + 3 笔 10-22 补录 | 全是上一条实例的证据 |
| 今早同步的两个号 | 恶灵之力 10-28 09:01 `open`、群山之力 08:54 `open` | **客户端自己答"这一条没做"** |

最后一行是本轮的定音证据：刚登录的号客户端答 `open`，可格子还是 ✓ —— 因为补录优先级高于快照，
而"算不算本轮"是拿 `t >= roundAt` 反推的，`roundAt` 钉在 CD 锚点上 ⇒ 10-22 那一笔永远算本轮。

**为什么会钉住**（代码层根因，本轮要整块拆掉的东西）：换轮只有两个检测器，两个都要先有"上一条的结束时刻"
才判得出来 —— `SetExpiry` 要比 `rec.roundEnd`（`Ledger.lua:853-856`），`RoundEnded` 要 `expiresSrc == "接口倒计时"`
且 `roundAt < expiresAt`（`Ledger.lua:602-606`）。记录被重建过（Prune/AddQuest/池里重新出现）这条链就断了；
断掉之后 `L.RoundAt`（`Ledger.lua:527-541`）的兜底把起点写成 `db.cycleAnchor` —— **这是 fail-open**：
"我判不出这一条什么时候开始" 被实现成了 "那它从本周开始一直都在"。
凡是"取不到就当成立"的门，都会把上一条实例的证据放进本轮。

## 二、定案（一句话）

**身份 = 任务号 + 结束时间，合成一个唯一编号；完成凭据在写下的那一刻把当时的编号烙进自己记录里，
判定时只问"这笔记录的编号 == 这条任务当前的编号吗"。不再有任何"起点"，不再有任何反推。**

- 结束时间取哪一档：**板面倒计时优先**（`expiresSrc == "接口倒计时"` 且还没过去）；
  **取不到就按周 CD 的尾巴**（他点名的周常组与问不出倒计时的行都走这条）；
- 编号 = `questID .. ":" .. floor((结束时刻 + 450) / 900)`（15 分钟一档，见 §三第 3 条为什么加 450）；
- 编号算不出来（周 CD 尾巴也定不出来）= **身份未定** ⇒ 这一行所有格子报 `?`，
  绝不报"做过"，也不拿任何旧到期值把行砍掉。**判不出来 ⇒ 不成立**，这是本轮唯一的降级方向。

## 三、新模型的三条边界（写代码前必须想清的三条）

1. **板面倒计时优先，且只认"还活着的"那一条**。`L.InstanceEnd(db, rec, now)`：
   `rec.roundEnd`（`expiresSrc == "接口倒计时"`）且 `> now` 才收回；**已经过去的那一条是墓碑，
   一律不收 —— 而且不收之后也不许退到②尾巴**（实施方 10-28 review 时补的这条，已落代码 + 用例）：
   12 小时区的条实例如果"答得出倒计时 → 全角色交完之后再也答不出"（第 25 轮真档实测的
   89377「秘密狩猎」就是这个形状），退回按周算的尾巴会把**同一周里的上下两条实例并成同一个编号**
   = 上一轮的 ✓ 冒充本轮，正是本轮要消灭的那类 fail-open。⇒ 这种一律 nil（格子 `?`）。
   ②尾巴只给**压根没答出过板面倒计时**的记录 —— 那才是用户原话里"获取不到结束时间的任务组"
   （真档实测 20 行里落这一档的 9 行：92636 / 90962 / 93244 / 97128 / 95918 / 90573 / 89507 /
   98172 / 96718，全都没有 `roundEnd`，与用户批准的那份清单一致）。
   第 34 轮 89267 那条"上一条实例的过期值吃掉整行"就是拿墓碑当了到期判据，同一个根因。
2. **周 CD 尾巴必须是纯账本算式，不许每行现问接口**。
   `L.InstanceEnd` 的尾巴档 = **`db.cycleAnchor + 604800`** —— 锚点本来就是 `Config.RefreshCycle`
   用周重置倒计时算出来的（`anchor = 下次周重置 - 604800`，`Config.lua:233-244`），
   加回一周就是服务器那头的轮换点，零接口调用、零新字段。
   **不许在这条路上调 `Config.NextCycleEnd`**：它每次都要问一遍 `Api.WeeklyResetCountdown`，
   矩阵每帧 20 行 × 每帧 = 新增 20 次接口调用，正是他 10-27 明令禁止的那类修法。
   `cycleAnchor` 是 0（锚点定不出来）⇒ 尾巴也没有 ⇒ **身份未定**，按 §四/§十第 5 条降级成 `?`。
3. **分桶必须偏 half-bucket**。世界任务板的结束时刻本来就落在整点上（真档实测 19:00 / 07:00 / 15:00 / 11:00），
   而接口答的是"剩余秒数"，`now + remaining` 只会比真值小 0~1 秒。
   `floor(endAt / 900)` 的桶边界正好压在整点上 ⇒ 同一条例实例会有一半概率被算成两条。
   所以取 `floor((endAt + 450) / 900)`（就近取 15 分钟），±1 秒抖动永远落在同一个桶里。
   用例必须钉这一条：`now+remaining` 差 1 秒的两次读数 ⇒ 编号相同。

## 四、判据点全量盘点（26 处，逐条给判定）

下面每一处现在都是"拿时间戳跟 `roundAt` 比大小"，全部换成"比编号"。左列是位置，中列是新的读法，
右列是老代码里那一句。

**位置一律按函数名找**：下面括号里的行号是 T1 提交之前量的，`L.InstanceEnd`/`L.InstanceKey`
落地后往后的行号整体涨了约 60 行 —— 照着名字定位，别照行号改。

**写侧（烙编号，5 处）**

| # | 位置 | 改法 |
| --- | --- | --- |
| W1 | `Ledger.OnTurnIn` | T2 先**在 `round` 旁边多写** `k = L.InstanceKey(db, questID, rec, t)`；T4 删 `round` 写点。`cycle` 字段留着给取证 |
| W2 | `Ledger.SetManual` | 同 W1：多写一份 `k`，`round` 到 T4 再删 |
| W3 | `Ledger.SetView` | **本轮新增的一格**：现在只写 `{s,t}`，压根没有身份 ⇒ 写 `{s,t,k}`。这一处就是 94612 那 7 笔假勾的入口 |
| W4 | `Ledger.SetRewardRead` | 同样补 `k`（按号读数今天也是拿 `t >= roundAt` 判的） |
| W5 | 金币粘滞 + observation | T2 新增 `L.NoteGoldThisInstance`（Sync 板循环里 `SetExpiry` 之后）写 `rec.goldKey`，并给 observation 补 `obs.key`；`DiffPool` 里老的 `goldRound = at` / `firstSeen` 写点**留到 T4 一起删**（`firstSeen` 降级成纯取证字段，见 §六） |

**读侧（比编号，13 处）**

| # | 位置 | 改法 |
| --- | --- | --- |
| R1 | `L.State` 补录门 `:1276-1283` | `m.k == curKey`；老记录没 `k` ⇒ **不匹配**（不退回时间戳门槛，见 §五） |
| R2 | `L.State` 流水门 `:1303-1309` | `last.k == curKey`，同上 |
| R3 | `L.State` 快照门 `:1318-1325` | `view.k == curKey` —— 这一句把 94612 那批判掉 |
| R4 | `L.State` 兜底 `unknown` 那句 `:1331-1332` | 条件里两处 `>= at` 换成 `view.k == curKey` |
| R5 | `L.CompletedThisRound` `:504-515` | 纯比键（绿勾/琥珀色那一岔） |
| R6 | `L.DoneByAnyCharThisRound` `:1560-1570` | `view.k == curKey and view.s == "done"` |
| R7 | `TurnInCred` `:1633-1668` | `r.k == curKey`（`cycle == cycleAnchor` 那一半被编号吸收，留着只当冗余守卫，见 §六第 2 条） |
| R8 | `L.CharRewardMoney` `:1032-1040` | `r.k == curKey` |
| R9 | `L.GoldReaders` `:1045-1062` | `r.k == curKey` |
| R10 | `L.QuestMoney` 粘滞 `:1491-1492` | `rec.goldKey == curKey` |
| R11 | `L.GoldFromOtherMode` `:1527` | 同上 |
| R12 | `L.WeekStats` 补录段 `:1369-1371` | `m.k == curKey`（周常档的编号本来就是周 CD 尾巴，等价于今天的 CD 门） |
| R13 | `Sync.lua:851` 流水顶掉快照那句 | `last.k == L.InstanceKey(db, rec, now)` |

**到期/剩余（3 处）** —— 见 §七

**取证与界面（5 处）** —— 见 §八

## 五、要整块删掉的老机器（不许留"兼容分支"）

| 删除项 | 位置 | 为什么必须删 |
| --- | --- | --- |
| `L.RoundAt`（含写回 `rec.roundAt` 的副作用） | `Ledger.lua:527-541` | 本轮的靶心。它的兜底 `start or anchor` 就是 fail-open |
| `L.RoundKey` | `:551-553` | 它只是 `RoundAt` 的别名；编号由 `L.InstanceKey` 直接给出 |
| `RollRound` | `:588-591` | 换轮不再需要"推起点"：编号跟着结束时刻走，结束时刻一变编号自然变 |
| `RoundEnded` | `:602-606` | 同上；检测器整类不要了 |
| `ROUND_CHANGE_TOLERANCE` / `ROUND_END_SLACK` | 文件头 | 15 分钟这条边界挪进分桶（§三第 3 条），不再靠两处常量 |
| `EffectiveExpiry` 里 `exp < at ⇒ nil` 那个 hack | `:572-579` | §七给出新推导，同一条保证保留、来源换成编号 |
| 存档字段 `rec.roundAt` | 迁移里删 | 留着就会有代码想读它 |
| 老存档"没烙键就退回时间戳门槛" | `:1304-1309`、`:513-514`、`:1277-1282` | 那一支是 fail-open 的第二张脸：`t >= anchor` 对 10-22 那批永远成立。**没编号 = 不匹配**，回填交给 §九的迁移一次做完 |

`rec.goneAt` / `rec.highFreq` / `res.renewed` 留着 —— 那是"多久没见过它"和面板提示，不参与身份。

**字段名这一条本轮定了不改**：存"板面答出的结束时刻"那份继续叫 **`rec.roundEnd`**
（全仓只有 `L.SetExpiry` 一处写它，语义本来就是"这一条实例的结束时刻"= 身份），
只把 `rec.roundAt` 删掉。改名会把 Dump/Probe/Sync/一大片用例牵进无谓的 diff，
而本轮要消灭的是判据不是拼写。`rec.expiresAt / expiresSrc / expiresGuessed` 那一组
在 T5 之后**没有任何判据读者**（到期与剩余一律问 `L.InstanceEnd`），连同 24 小时排程值一起下线；
`L.InstanceEnd` 里现在那句 `expiresSrc == "接口倒计时"` 的守卫随 T5 一起去掉
（roundEnd 只有一个写点，不需要再验来源标签）。

## 六、写侧顺序与两份 observation 的搬运

1. **Sync 的两处循环顺序**：`L.DiffPool`（`Sync.lua:820`）跑在逐条倒计时读取（`:863-873`）之前。
   同一次快照里 DiffPool 算出的编号只能用"上一次读到的结束时刻"。
   处理方式有两步，都在 T2 落：
   **(a) 逐条倒计时读取挪到循环体最前**（`QuestTimeLeft` + `L.SetExpiry` 整段提到 `L.SetView` 之前）。
   今天 `SetView`/`SetRewardRead` 跑在倒计时读取之前 ⇒ 它们烙的编号只能用"上一次读到的结束时刻"：
   板上第一次见到某条任务时那份结束时刻还不存在 ⇒ 编号 nil ⇒ **这一笔快照永久挂不上身份**，
   ✓ 要等下一次同步才长出来。读倒计时本身是纯 `pcall` 查询、写在 `rec` 上，挪到前面不改变今天的任何判据。
   **(b) 金币粘滞（W5）从 DiffPool 里挪到 Sync 的池循环末尾**，在 `L.SetExpiry` 之后调
   `L.NoteGoldThisInstance(db, questID, money, now)`。差一轮快照的代价变成"当次没记上、下次记上"，
   比"用上一条实例的编号记上、下次换编号时又被自己判掉"好；而且这一处本来就在循环里，零新增接口调用。
   **T2 只让 `NoteGoldThisInstance` 写 `rec.goldKey` + `obs.key`，不碰 `goldMoney`**（谁写钱是 T4
   换读侧那一起才改的事；这一刀同时写两份会跟 DiffPool 抢 `goldMoney`，界面当场变样）。
   DiffPool 里其余 `at` 用法（`obs.firstSeen`、读闪延续的 `>= at`）一并换成编号：
   **延续只许从 `other.key == curKey` 的那份搬**（第 34/35 轮"只许同轮之内搬"的编号版，语义等价）。
2. **`cycle` 字段留不留**：留。它是取证字段（`/gql quest` 要能说"这笔是本 CD 哪一次交的"），
   但**判据不再读它** —— R7 那道 `cycle == cycleAnchor` 降级成"和编号一起写、只给报告看"，
   编号才是唯一凭据。理由：锚点在接口定不出来时是 0，那道门一进门就放行（`:1297` 的注释自己就写着）。
3. **`obs.firstSeen`** 降级成纯取证（"这一副奖励第一次读到的时刻"），Dump 那句"上次金额"继续读它；
   从此没有任何判据读它。

## 七、到期门：两条既有的保证都要保住

- **96718 那条"时间结束后都要移除"**（用户 10-24 定）：
  `expired = instEnd ~= nil and instEnd <= now and not (rec.lastSeenAt > instEnd)`。
  第三个条件是新写的判据，读的全是账本里已有的字段（`lastSeenAt` 每次 DiffPool 都写）。
- **89267 那条"上一条实例的过期值不许吃掉整行"**（第 34 轮，用户实测）：
  换轮之后倒计时问不出 ⇒ `instEnd` 已是过去值 ⇒ 但这条任务还在板上被看见过
  （`lastSeenAt > instEnd`）⇒ **不判到期**，行留着、剩余那格空着、悬停不报倒计时，
  等接口自己写新的。这一保证从此由编号模型直接推出，不再需要 `exp < at` 那个特例。
- `L.EffectiveExpiry(db, rec)` **函数名与 7 处调用点全部保留**（矩阵到期门 / `row.remaining` /
  悬停倒计时 / 右栏 keepable / 右栏同名去重打分 / `NextRefreshAt` / `/gql quest`），只换实现：
  返回 `instEnd`（且只在 `instEnd > now` 时返回，绝不返回过去值 ⇒ 不会再印"剩余 已到期"挂在活行上）。
  到期门另用上面那句 `expired`（两处都问 `L.InstanceEnd`，不许自拼 `now >= rec.expiresAt`）。
- **`SetCycleExpiry` 的 24 小时排程值整条删**：它是编出来的数，一旦参与身份就会每天造一条新实例。
  代价照实：周重置接口问不出时不再排一次"24 小时后自动刷新"（今天也只是排一次重试，行为差别很小），
  `expiresGuessed` 字段与界面"猜的数不上色"那一岔一起删 —— 从此没有猜的数。

## 八、界面、`/gql quest`、Probe 连带

- `L.State` 的 `gate` 参数**收窄**：不再选"哪道新鲜度门"（编号一套通吃，周常的编号就是周 CD 尾巴那一档），
  只选"本轮一份证据都没有时该报 `?` 还是报 可做/已结束"。常驻周常报 `?`（今天的行为，不许退化成灰色 `—`），
  池内世界任务报 `open`。参数名保持 `gate`、取值不变，改注释说明它只管兜底答复。
- `Dump.lua:428` 那句 `本轮起点=%s` ⇒ 改成 `唯一编号=%s  结束时间=%s（来源=板面倒计时/周CD尾巴）`；
  `offListReason` 的分岔顺序照旧（屏蔽→占行→准入→到期→同名），到期那一支的措辞跟着 §七 的新句子改。
- 身份未定这一档要有一句话可说：`这一条现在挂不上编号（板面倒计时没读到、周 CD 尾巴也定不出来）
  ⇒ 格子一律 ?，不做任何判据`。这是取证面的话，进 `/gql quest` 与 Probe，**不进悬停**
  （用户 10-27 第 33 轮定的：界面只说这一号这一轮的事）。
- `Probe.lua:281` 那行 `cycleAnchor=…` 后面补一句"周 CD 尾巴 = 锚点 + 604800 = <可读时刻>"，
  让尾巴这一档当场可核（纯算式，不新增字段、不新增接口调用）。

## 九、迁移：`Config.SCHEMA_VERSION` 5 → 6（"旧的数据如果没存，都要修改"）

新函数 `Ledger.BackfillInstanceKeysOnce(db, now)`，注册进 `Config.MIGRATIONS[6]`。逐条记录做：

1. 先按 §三/§七 算出这条记录**当前**的编号 `curKey`；算不出（身份未定）⇒ 这条记录一笔都不回填，
   老证据自然全不匹配，界面对它就是 10 个 `?`（宁缺不编）。
2. 定"能不能证明某一笔属于这一条实例"的**起点边界**：
   - **板面档**（`rec.roundEnd` 有值 = 这条答出过板面倒计时）⇒ 起点只认"这条记录自己滚过的 `roundAt`"，
     即 `rec.roundAt` 存在且 `≠ db.cycleAnchor`。等于锚点或压根没有 = 从没滚过 = 老档里没存过
     这一条实例几点开始 ⇒ **判为挂不上**（94612 走的正是这一支，用户已批准）。
   - **尾巴档**（`rec.roundEnd` 为空 = 压根没答出过倒计时，用户点名的"获取不到结束时间的任务组"）
     ⇒ 起点 = `db.cycleAnchor`（常驻周常的本轮就是整个 CD）。
     真档实测这一档正好是 9 行（92636/90962/93244/97128/95918/90573/89507/98172/96718），
     与用户批准的那份清单逐行一致 ⇒ 迁移后它们的勾一个不许掉。
3. 对每笔证据（`turnins` 每行、`manual`、`chars[*].views[*]`、`chars[*].reads[*]`）：
   `t >= 起点` ⇒ 烙上 `k = curKey`；`t < 起点` 或起点不可证 ⇒ **不烙**（保持无编号 = 永不匹配）。
4. 删掉 `rec.roundAt`（第 2 步已经把它当取证依据用完，留着就会有代码想读它）和
   `goldMoney/goldRound`（换成 `goldKey`，下一次快照自然重写）；
   `rec.roundEnd` **保留原名**（§五的命名决定）；`expiresAt/expiresSrc/expiresGuessed`
   留到 T5 随排程值一起下线，本档不动它们。
5. **数据一条都不删**。用户批准的是"勾挂不上"，不是"抹掉流水"：
   `db.turnins` 是金币账本，94612 那几笔（如果有）真在这个 CD 里拿到过钱，删了"本周金币"就少报；
   无编号 = 不再产生 ✓、不再算"本轮交过"、不再算上板凭据，效果与他要的"全部删掉"一致，代价小一档。
   汇报时把这一条差异照实说。
6. 迁移只在 `Config.Init` 跑一趟（现有框架），失败停在 v5 下次登录重试；
   日志报 `数据升级 v6 完成（回填 X 笔 / 判为更早 Y 笔）`，Y 必须能在 `/gql quest` 里逐笔看到是谁。

## 十、用例（`tests/run_gql_tests.py`，基线 341）

**新增（钉新模型，全部要有对照组）**

1. `test_id_plus_end_time_is_the_identity_not_the_start`：同一 questID，结束时刻换 15 分钟以上 ⇒ 编号变、
   旧证据全部不再匹配；结束时刻不变、只换奖励签名 ⇒ 编号不变、✓ 一个不丢（第 25 轮那条老用例换写法保留）。
2. `test_a_one_second_jitter_never_forges_a_new_instance`（§三第 3 条）：`now+remaining` 差 0~1 秒的两次读数 ⇒
   同一条编号。**把 450 那个偏移摘掉，这条必须红。**
3. `test_weekly_family_without_countdown_uses_the_cd_tail`：问不出倒计时的注册周常 ⇒ 编号 = 周 CD 尾巴那一档，
   同一 CD 内两次读数同编号；跨 CD ⇒ 换编号（97128/90962 的形状）。
4. `test_an_unkeyed_record_never_claims_this_round`：证据没有 `k` ⇒ 格子 `?`，且**不许**退回 `t >= anchor`
   （94612 那批的直接靶心；把 §五最后那一行"退回时间戳"复活，这条必须红）。
5. `test_identity_undeterminable_reports_unknown_not_done`：`cycleAnchor` 是 0（锚点定不出来）
   且板面倒计时问不出 ⇒
   整行格子 `?`、行不被砍、金额与名字照旧（fail-closed 的两面）。
6. `test_snapshot_written_this_round_is_the_missing_case_today`：走真实 `Sync` 事件入口，
   登录快照写下的 `{s,t,k}` 里必须带 `k`（替身要复刻 12.1.5 的 `open` 答复）—— 这一条就是本轮 bug 的复现用例。
   **而且要钉"第一次上板就带编号"**：`db.quests` 里这条记录事先没有 `roundEnd`，倒计时问得出 5 小时 ⇒
   这一次快照写下的 view/read 就必须已经带 `k`（§六 1(a) 那个顺序；把读取挪回 `SetView` 之后，这条必须红）。
7. `test_stale_expiry_never_eats_a_row_that_the_board_still_shows`（保住第 34 轮）+
   `test_row_still_dies_at_its_own_round_expiry`（保住 96718 那句）：两条都读 `L.EffectiveExpiry`/新 `expired`。
8. `test_v6_backfill_drops_only_the_unprovable_marks`：照 94612 真档字段形状造记录（`roundAt == cycleAnchor`、
   `instEnd` 在未来、7 笔快照 + 3 笔补录全是更早）⇒ 回填后 0 笔挂上、面板仍 20 行、流水一条没少；
   对照组照 93573 形状（`roundAt` 滚过）⇒ 14 笔全挂上。
9. 静态守卫：`RoundAt|RoundKey|RollRound|RoundEnded|rec.roundAt` 在全仓（含测试）**只许出现在注释/迁移文档里**；
   `L.InstanceKey` 是判据唯一入口；`db.turnins`/`db.manual`/`views` 没有第二处自拼时间戳门。

**改写**：现在 207 行提到 `roundAt/RoundAt/RollRound/roundEnd/expiresSrc/gate/ThisCycle/cycleAnchor` 的用例，
逐条分三类处理 —— (a) 纯 fixture 造数据：机械换成 `instEnd` + 烙键；
(b) 钉"换轮推起点"这条老规则：删掉或换成钉"换结束时刻就换编号"；
(c) 钉行为保证（第 25/32/34/35 轮那些成对用例）：**判据一字不动，只换写法**，
每条都要能指出"摘掉本轮哪一处改动它就红"。预期总数 341 → 约 347±5，删掉的必须逐条列名汇报。

## 十一、性能约束（他 10-27 定的"零新增接口调用"这条要复述给实施方）

- `L.InstanceEnd` / `L.InstanceKey` 纯读账本；周 CD 尾巴 = `db.cycleAnchor + 604800` 一条算式，
  **不许在这条路上问 `Config.NextCycleEnd` / `Api.WeeklyResetCountdown`**。
- 每行每帧最多算一次编号：`MatrixRows` 在循环外把 `now` 定下来，行内算一次并传给 `L.State`/
  `DoneByAnyCharThisRound`/`row.remaining`，**不许每格算一次**（14 号 × 20 行 × 每帧）。
  实现方式自选（参数传入或行内局部变量），但 `L.State` 的对外签名要保持可用（右栏/用例也在调）。
- 落盘体积：每笔证据多一个数字字段 `k`，`turnins` 上限仍是 8 笔/条，不新增表。

## 十二、代价照实（要原样转达用户，不许缩）

1. **换一条实例的那一次同步里，这一条的 ✓ 会整排消失**（这是本轮要的效果，不是副作用）：
   新实例的证据要等交过/登录过才重新长出来。他 10-25 就定过"旧的做没做已经不重要了、也不用显示了"。
2. **身份未定那一小段窗口里记下的补录/流水不会自动挂回来**：那一笔没烙上编号 ⇒ 永久不匹配，
   重登一次或再点一次格子即补上。取向照他 10-25 那句话：少报可以，假勾不行。
3. **周 CD 尾巴问不出时不再排 24 小时重试**（§七最后一条），面板要等登录或手动刷新才重新抓 ——
   与今天"开面板不抓"那条开放项同族（见 [[project-wqt-gold-row-latency-openbug]]）。
4. **老存档里"起点重建不出来"的那几笔勾一律清掉**，94612 是唯一命中的一行（真档实测）。
   今早同步的恶灵之力/群山之力客户端答的是 `open` ⇒ 这两个格子本来就该空 ⇒ 回填后确实空，不会自己长回来。
5. **本轮改动量大**（26 处判据 + 迁移 + 用例改写），实施分七刀（T2 写侧 → T3 迁移 → T4 读侧 →
   T5 到期 → T6 取证文案 → T7 文档 → T8 真档回放，见 §十三），每刀都要全绿才进下一刀；
   第 34、35、36 轮都还没在真客户端验证过，部署后需要他 `/reload` + 开一次面板回传截图。

## 十三、任务切分（每刀结束时 341+ 全绿，逐刀提交）

**顺序是定死的，不许调换**：写侧（T2）→ **迁移（T3）** → 读侧（T4）。理由是 §五 删掉了
"没键才退回时间戳"那一支 —— 如果读侧先换，老存档里每一笔历史凭据都没有 `k`，
读侧一比就不等 ⇒ 部署那一次同步会把**全部 19 行好勾**连带 94612 的 10 笔假勾一起清光，
而他只要求"挂不上的删掉"。迁移先落地，读侧切换时数据已经全带编号。

- **T0 基线确认（主 agent 已做完，实施方不许回退）**：工作区里那份未提交的"轮次键"增量
  = `L.RoundKey` + `OnTurnIn`/`SetManual` 烙 `round` + `L.State`/`CompletedThisRound` 先比键、
  没键才退回时间戳（实测 341/341 全绿）。它就是本轮 W1/W2/R1/R2 的前半。
  **本轮的增量恰好在"没键才退回时间戳"那一支上**：§五要求把它删掉 ⇒ 老存档必须先过 §九 的迁移。
- **T1 加法（不动老判据）—— 已完成，提交 `b444d17`，345/345**：`L.InstanceEnd` + `L.InstanceKey`
  （尾巴档 = `cycleAnchor + 604800`，不新增字段、不新增接口调用）+ 4 条用例。
  两条 review 期的改动已进代码，后续任务按这个形状继续：
  ①实例编号 = `questID .. ":" .. floor((endAt+450)/900)` 的字符串，签名是
  `L.InstanceKey(db, questID, rec, now)`（rec 不存自己的 id，questID 显式传）；
  ②"答出过倒计时但已过去"一律 nil，不退尾巴（§三第 1 条）。
- **T2 写侧烙编号（纯加法，读侧一字不改）**：W1–W5 —— `OnTurnIn`/`SetManual` 在现有 `round` 旁边
  **多写一份** `k = L.InstanceKey(db, questID, rec, t)`、
  `SetView` 新增那格写 `{s,t,k}`（94612 十个假勾的入口）、`SetRewardRead` 补 `k`、
  金币粘滞并行走一条 `L.NoteGoldThisInstance`（Sync 板循环里 `SetExpiry` 之后调，写
  `rec.goldKey` + `obs.key`；`DiffPool` 里老的 `goldRound`/`firstSeen` 写点这一刀先留着不动）+ 用例 6。
  **T2 不许删任何老写点** —— `round`/`goldRound` 连同它们的读点一起在 T4 消失。
  还要落 §六 1(a)：**把逐条倒计时读取（`QuestTimeLeft` + `SetExpiry`）整段提到 `SetView` 之前**，
  否则第一次上板的那笔快照烙的是"上一条实例"的编号、当场判不出来 ⇒ ✓ 要等下一次同步才长出来。
  这一刀结束时老判据照旧跑（还认 `round`/时间戳），新字段只是并行多写一份 ⇒ 全绿且不改变界面。
- **T3 迁移 v5→v6（§九，先于读侧）**：`Ledger.BackfillInstanceKeysOnce` + `SCHEMA_VERSION = 6` +
  schema 注释 + 用例 8。只给**没有** `k` 的旧凭据补编号；补不出的（起点重建不出来）**不留 `k`**，
  数据一律不删。读侧此刻还没换，所以这一刀落地后界面仍然一字不变 —— 迁移是"把钥匙配好"，
  不是"把门换掉"。日志报"回填 X 笔 / 判为更早 Y 笔"。
- **T4 读侧换成比键（这一刀才换门）**：R1–R13 + §五 的整块删除（`L.RoundAt`/`RollRound`/
  `RoundEnded`/`roundAt`/容差常量/"退回时间戳"那一支）+ `gate` 参数收窄（§八第 1 条）
  + 用例 1/4/5/6/10 类改写。切换完 94612 那 10 笔自然对不上 ⇒ 界面按 §十四第 2 条变干净。
- **T5 到期门**：`EffectiveExpiry` 新实现 + `expired` 新判据（§七）+ 删 `SetCycleExpiry` 的 24 小时
  排程值与 `expiresGuessed` + 用例 7。**这一刀同时把 `L.InstanceEnd` 里
  `expiresSrc == "接口倒计时"` 那道守卫去掉**（`expiresSrc` 这个字段随排程值一起退役，
  留着守卫就是引用一个再也不写的字段）。
- **T6 取证与界面文案**：`Dump`/`Probe`/`Options`/`Matrix` 悬停措辞（§八）+ 用例 9 的静态守卫。
- **T7 文档**：`docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 追加 §36（原话 + 定案 + 落点 +
  用例名 + 真档影响面）；`docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`
  把"轮次身份"那节整段按新模型重写（现在还写着 `roundAt`/检测器，是下一轮误判的源头）。
- **T8 真档回放验收**（§十四第 2 条）。

## 十四、验收

1. `PYTHONUTF8=1 python tests/run_gql_tests.py` 全绿（基线 341，逐刀报数）。
2. **真存档只读回放**（`Wow.exe` 当前未运行，档 mtime 10-28 09:07；**绝不写档**）：
   迁移前 vs 迁移后跑 `MatrixRows(db, now)`，断言四件事 ——
   - 行数 **20 → 20**（94612 靠 1058 金过门槛，本来就不靠勾占行）；
   - 勾 **只有 94612 那 10 笔消失**，其余 19 行逐格一字不变（逐行比对，不只报总数）；
   - `db.turnins` 笔数一条没少（§九第 5 条要能被验证）；
   - 94612 那一行部署后 `/reload` **当场**就干净（不等任何新快照）—— 这条必须明确回答，
     是 10-25 那句"没有变化，是我拷贝错了吗"的固定验收问法。
3. 变异验证：§五 每一条"不许退回时间戳"各摘一次、`+450` 偏移摘一次、`expired` 第三个条件摘两次
   （保住第 34 轮 / 保住 96718 各一条）、迁移的起点分岔摘一次 —— 每次只许对应那几条用例红。
4. 逐行自查 `git diff`：金币门槛、五道准入门、`TurnInAdmits`、`ignored` 永久否决、家族/分组判据
   一字未动（本轮只动身份）。`grep -n "roundAt\|RoundAt\|RollRound\|RoundEnded" GoldQuestLedger/*.lua`
   结果只许是注释。
5. 零新增接口调用：`grep` 核对新增代码里没有 `C_/GetQuest` 这类调用，`db.cycleEnd` 只在 `RefreshCycle` 里写。
6. 部署前核对 AddOns 目录（共享工作区，并行会话在改）：
   `diff GoldQuestLedger/Ledger.lua "D:/Games/World of Warcraft/_retail_/Interface/AddOns/GoldQuestLedger/Ledger.lua"`
   与 `Matrix.lua` 同样一条 —— 上一轮实测两边不一致，不核对就部署等于部署别人的版本。
7. 汇报用大白话：为什么原来"勾会跨轮留下来"、现在改成"每笔完成都存了这条任务这一轮的编号"；
   并点名两件没做的事：真客户端未验证、以及迁移只清勾不删流水与他那句"全部删掉"的那一格差异。

## 十五、工作区

共享工作区，并行会话在改 `AHSnipe/*`、`tests/run_tests.py`、`docs/tasks/2026-10-13-ahsnipe-plan.md`
（本轮开工时 `git status` 实测这几项是脏的，`GoldQuestLedger/Ledger.lua`、`Matrix.lua`、
`tests/run_gql_tests.py` 也带着第 35 轮之后的未提交改动 —— **T0 先逐行确认这些改动是什么、
要不要先提交，再动身份**）。本轮只提交本轮碰的文件 + 两份文档。
`tests/tmp_panel_audit.py`、`tmp_identity_audit.py`、`tmp_expiry_audit.py`、`tmp_gate_probe.py`
是本轮的一次性取证脚本（只读档），**不入库**，验收完删掉。
