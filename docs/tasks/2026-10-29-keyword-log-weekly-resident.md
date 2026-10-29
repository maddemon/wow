# 计划：关键词任务"有号接过"要整周驻板（梦魇任务 94446）

> 状态：**已完成**（2026-10-29，368/368 + 真档只读回放 16→16，验收与 review 后补的那一刀见 §六）。
> 用户 2026-10-29 定稿口径（原话）：
> "梦魇任务是关键词任务，只要有一个号接了，那就意味着我要关注这个任务，
> **不管是不是金币任务，都要上矩阵**。"

## 一、现象与取证（真档，今天 14:57 落盘那份）

`WTF/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua`：

- `db.quests[94446]` = `{ name="梦魇任务", kwLogAdded=true, currentSig="-1", lastSeenAt=… }`
  —— **没有** `poolSeen` / `onBoard` / `regKey` / `mapID` / `lastMoney` / `roundEnd`。
- 各号在 94446 上的快照：6 个号 `progress`（现在都接着，编号都是 `94446:1993148`）、
  3 个号还是上周的 `done`（**没有编号** ⇒ 第 36 轮的比编号一律不认）、5 个号一条记录都没有。
- `turnins` / `manual` 里 94446 一笔都没有。

只读回放（`tests/tmp_kwonly_audit.py`，灌真档跑 `MatrixRows`）：

| 跑法 | 面板行数 | 差集 |
| --- | --- | --- |
| 当前档（有号接着这条） | 15 | — |
| 模拟"没接这条的号登录"（清掉 `kwLogAdded`） | 14 | **掉的就是 94446 梦魇任务** |

⇒ 他报的"不接的人矩阵里看不到"不是某个号那一格空，而是**整行没了**：行、金额、勾都在
账号级那一份 `db.quests[qid]` 上，换号登录会把这条撤走（见下），撤走就全账号一起看不见。

## 二、根因

这条记录的上板凭据**只有一份**：`kwLogAdded`。而那份的语义是
"**此刻正在同步的那个号**把这条接在任务日志里"：

- 写点：Sync 收日志那段，命中关键词且这条挂在该号日志里 ⇒ `rec.kwLogAdded = true`
  （Sync.lua:244）；`Ledger.AddQuest(..., "questlog")` 是第二个写点（Ledger.lua:1100）。
- 撤点：同一次快照的收尾循环，`if rec.kwLogAdded and not keep[questID] then rec.kwLogAdded = nil end`
  （Sync.lua:259）—— 换一个没接这条的号登录就当场撤掉。

撤掉之后五道准入门（`Ledger.RowAdmitted`）逐条落空：不是日志收的、没注册、
读到过金币是"没有"（`currentSig="-1"`，客户端压根答不出金额）、没上过板、
名字撞关键词那条还要额外一条客户端凭据（第 32 轮加的）⇒ 整行消失。

这一族正是第 32 轮当场认下、写进注释没解决的那条代价：
"**真存在但客户端一条都答不出的特别任务，不接就不上板**"
（Ledger.lua:2390、计划 `2026-10-27-gql-keyword-duplicates-id-column.md:255`）。
今天这条是它第一次被玩家点名。

## 三、修法：把"有号接进日志"从"当前号的事实"升成"这一条实例的凭据"

按第 36 轮的编号模型做，不引入任何新时刻比较：

1. **新字段 `rec.kwLogKey`** = 烙下"某个号的日志挂着这条"那一刻，这条实例的编号
   （`L.InstanceKey(db, questID, rec, now)`）。这条记录问不出板面倒计时 ⇒ 编号落在
   **周 CD 尾巴**那一档（`cycleAnchor + 604800` 分桶）⇒ 下周重置自己换编号、凭据自己作废，
   不需要任何"清理"代码。
2. **唯一真源判据（读）`L.KeywordLogAdmits(db, questID, rec, now, curKey)`**
   - 签名照 `L.DoneByAnyCharThisRound`（Ledger.lua:1758）**一字不差**：`curKey` 末位可选，
     为 `nil` 时自己算一次 `L.InstanceKey(db, questID, rec, now or LedgerNow(db))`；
     矩阵那条路必须用 MatrixRows 预扫好的 `keyOf[questID]` 传进来（§十一：每行每帧只算一次）。
   - 算不出编号（`curKey` 为 `nil`/`false`）**返回 false**（fail-closed，与
     `DoneByAnyCharThisRound` 同一方向；`L.SameInstanceKey` 的 `nil==nil` 不许放行照旧有效）。
   - 本体只有两句：取编号 + `return L.SameInstanceKey(rec.kwLogKey, curKey)`。
     全仓只许出现这一串判据，照 `L.TurnInAdmits` 的先例配静态守卫用例。
3. **唯一真源写点（写）`L.NoteKeywordLogThisInstance(db, questID, rec, now)`**
   —— 照 `L.NoteGoldThisInstance`（Ledger.lua:660）的先例：内部算编号，**算不出就一个字不写、
   返回 nil**（不许兜底成周初"判不出就当一直在"）；算得出就 `rec.kwLogKey = key` 并返回 key。
   两个写点都调这一个函数，不许在调用方各自拼 `InstanceKey`（否则分岔）。
4. **`kwLogAdded` 那个布尔位语义不动**（"还挂在当前号的日志里"是 live 事实，
   消失判定、事件 pin 用词都靠它）；新增的这份是**这一条实例的凭据**，两者分开存。
   **Sync.lua:259 那道撤点只撤布尔位，绝不撤 `kwLogKey`** —— 本轮要修的就是"撤掉布尔位以后
   这一条实例的凭据还在不在"。
5. **本轮只做"放宽"，不做"收紧"**：R1–R9 每一道都是在原有条件后面 `or KeywordLogAdmits(...)`，
   一个字不删。这样最坏情况等于今天的行为（少报），不可能比今天更容易掉行 ——
   板面倒计时残留（`roundEnd` 已经过去）导致编号算不出、新判据不放行，正是这种"等于今天"的角落。

### 落点清单（读 `kwLogAdded` 的每一处都要逐条判定，不许漏）

| # | 位置 | 现在 | 改成 |
| --- | --- | --- | --- |
| W1 | Sync.lua:244（收日志写点，已存在的记录） | `rec.kwLogAdded = true` | 同一处紧跟一句 `L.NoteKeywordLogThisInstance(db, questID, rec, now)` |
| W2 | Ledger.lua:1100（`AddQuest(...,"questlog")`，新建的记录） | 同上 | 同上（两个写点必须同形状，都走这一个函数，否则分岔） |
| R1 | Ledger.lua:1792 `QuestHasCredential` | `rec.kwLogAdded` | `or L.KeywordLogAdmits(db, questID, rec, now, curKey)` |
| R2 | Ledger.lua:2316 `RowAdmitted` | `rec.kwLogAdded or rec.regKey` | 加同一判据（**继续绕金币门槛**：用户这次明说"不管是不是金币任务"） |
| R3 | Ledger.lua:883 `DiffPool` 的"本轮没在池里 ⇒ 记 gone" | 豁免 `kwLogAdded` | 加同一判据（否则撤掉布尔位后 `currentSig` 被清空、行变灰横线） |
| R4 | Sync.lua:858 "不在池里 ⇒ 写 ended" | 豁免 `kwLogAdded`/`regKey` | 加同一判据（否则整行格子被刷成 `—`） |
| R5 | Ledger.lua:1006 `SetExpiry` 常驻周常只认 CD 尾巴 | `regKey or kwLogAdded` | **签名前三位一个字不动**（`tests/run_gql_tests.py` 有 20 处按 `L.SetExpiry(rec, now, remaining)` 调用，动了全红）；加**第 4 个可选参数 `resident`**（布尔），判据由两个调用方算：Sync.lua:789、Ledger.lua:1294 各传 `L.KeywordLogAdmits(db, questID, rec, now)`（这两处手上没有本轮编号，让判据自己算一次纯算术 —— 每趟同步每行一次，不在矩阵每帧那条路上）。不放宽这一道会怎样：日志行这周之后重新上板 ⇒ 布尔位当场被撤（在池里的行由池维护）、`SetExpiry` 把板上几小时的轮换寿命写进 `roundEnd` ⇒ 编号从 CD 尾巴挪到板面档 ⇒ 刚烙的凭据当场对不上 = 加固符文石当天下午没行那个形状 |
| R6 | Sync.lua:625 事件 pin 用词 | `regKey or kwLogAdded` | 加同一判据（分组/地图跟着走） |
| R7 | Ledger.lua:2477 `row.watched` | `kwHit or kwLogAdded or fam` | 加同一判据（在 MatrixRows 里，传预扫的 `curKey`） |
| R8 | Ledger.lua:1381 `QuestKnown` | 已有 `KeywordMatch(name)`（今天第一刀） | **不动**（这一道已经放行） |
| R9 | Ledger.lua:2264/2268 `PurgeExpiredLegacyOnce` 遗产保护 | 认 `regKey/kwLogAdded/ignored` | **①②两支都加**同一判据（`L.KeywordLogAdmits(db, questID, legacyRec, wall)`）。①不放宽：本周的凭据不许当遗产删。②不放宽：给这种行写 `roundEnd` 等于把身份从 CD 尾巴挪到板面档，刚烙的凭据自己作废 |
| C1 | Ledger.lua:1128 `RemoveQuest`（玩家屏蔽） | 清 `kwLogAdded` | 一起清 `kwLogKey`（屏蔽之后"日志收的"不再是事实，同那条注释的理由） |
| C2 | Ledger.lua:412 字段清单 | 无 | 加 `kwLogKey = true`，否则 `quest_records_only_come_from_the_factory` 报"字段清单缺这个" |
| P1 | Dump.lua:303 "在列靠" | `"关键词从日志收的"` | 本周编号对得上的那份要说清是"本周有号接进日志（编号 …）"，**别把已撤布尔位的行报成"日志收的"** |
| P2 | Probe.lua:1228/1278/1620/1682 | 读布尔位 | 跟着读新判据（取证口径与界面同一个真源，第 34 轮的教训） |

### 老存档不自愈 ⇒ v10 迁移

`Config.SCHEMA_VERSION` 9→10，`MIGRATIONS[10] = L.StampKeywordLogKeysOnce(db, now)`：
给当下 `kwLogAdded == true` 的记录烙上**当前**编号。

- 为什么必须做：不烙的话，更新完第一次登录如果用的是"没接这条的号"，这些行当场撤走 ——
  正是"改完没有变化"那一族。
- 幂等：只认 `kwLogKey == nil` 的；已经烙过的不动。
- 方向是**少报不多报**：这份编号只是"这一条实例曾经被某号的日志证明存在过"，
  不给任何一格凭空打勾（勾仍然只来自快照/流水/补录那三份带编号的凭据）。

## 四、验收

1. 新用例（先 RED 后 GREEN）：
   - `test_a_keyword_weekly_stays_after_the_char_that_accepted_it_logs_out`：
     A 号接上（收日志）→ 交掉 → **换没接这条的 B 号登录** ⇒ 行还在、`currentSig` 不被清、
     格子没被刷成 `—`；对照组：老形状（只有布尔位、没有编号）当场掉行。
   - `test_the_log_credential_dies_with_the_instance`：把 `cycleAnchor` 推一周 ⇒ 编号变 ⇒ 行撤。
     （钉住"整周驻板"不是"永久驻板"。）
   - `test_a_masked_row_loses_both_log_marks`：屏蔽时 `kwLogAdded`/`kwLogKey` 一起清，
     取消屏蔽后不自动回来（要再有一次日志快照）。
   - `test_migrate_stamps_the_log_key_for_rows_already_alive`：v10 烙编号 + 幂等 +
     只认 `kwLogAdded`；连带把 v6/v7/v9 阶梯用例的终态预期推到 10。
   - 静态守卫：`KeywordLogAdmits` 那串判据全仓只出现一次。
2. 真档只读回放（`tests/tmp_kwonly_audit.py`）：清掉 `kwLogAdded` 后面板仍 **15 行**，
   94446 梦魇任务不掉。
3. 全量 `python tests/run_gql_tests.py --syntax` 通过；Code Review（fresh SubAgent）。
4. 性能：零新增接口调用。编号是纯账本算术（`L.InstanceKey`），写点本来就在那段收日志的循环里。

## 五、不做的事

- **不**把 94446 抄进 `Api.WEEKLY_PRESETS`：用户选的是通用规则，不是逐条手抄 ID。
  （内置表那条路是"池和日志两条来源都问不到"的任务走的；这条日志问得到。）
- **不**给从没接过的号凭空打勾：客户端只答当前角色，那一格继续 `?`，
  要落地只能他自己点一次（琥珀勾 = "我自己记的"，颜色语义不动）。
- **不**改 `kwLogAdded` 布尔位的语义与撤点（那是"当前号还挂着没"的 live 事实）。
- **不**动金币门槛、屏蔽语义（`ignored` 永久）、编号模型本身。

## 六、实施结果（2026-10-29）

按计划逐条落地（W1/W2 两个写点、R1-R9 九道读、C1/C2、P1/P2、v10 迁移），全量
`python tests/run_gql_tests.py` **368/368 通过**（review 后补的那条在内），`--syntax` 11 个文件全过。

### 落地时补的一道（计划里没有，review 时发现的形状）

`L.KeywordLogAdmits` 的第一句先问 `Config.KeywordMatch(rec.name)` —— **关键词删掉，这份编号
立即不作数**。理由：Sync 那道撤点手上只有"这条不在本次 keep 里"一个事实，可它背后是两种
完全不同的原因（任务离开了日志 = 用户要驻整周的那种；玩家把关键词删了 = 这行再没有绕门槛的
理由），数据层分不开，所以区分住在判据里。口径照 `L.QuestKnown` 那句现成的"玩家删了关键词
这道门自动关闭（不引入新字段、不留新状态）"，一个字节都不写进存档。
同一周里把关键词加回来，行当场回来（"这一条实例被某个号的日志证明存在过"不会因为删一次
配置变成没发生过）。用例 `a_removed_keyword_takes_the_log_credential_down_with_it`。
不加这一道 = 老注释那句"标记不清掉会在矩阵里一直赖着"换个字段重演。

### 新增用例（6 条，全部先红后绿）

- `a_keyword_weekly_stays_after_the_char_that_accepted_it_logs_out`（用户点名的那条，走真实
  同步入口三趟快照：A 号接→B 号没接→C 号再登录；钉行还在、currentSig 没被清、格子没被刷 `—`）
- `the_log_credential_dies_with_the_instance`（推一周 ⇒ 编号换档 ⇒ 行自己退，钉"驻整周"不是"驻永久"）
- `a_masked_row_loses_both_log_marks`（屏蔽清两份；取消屏蔽不许让编号自己长回来）
- `a_removed_keyword_takes_the_log_credential_down_with_it`（上面那道）
- `a_log_row_back_on_the_board_keeps_its_weekly_identity`（R5，带对照组：没有日志凭据的池内
  任务照旧写 `roundEnd`，证明这道门不是把 `SetExpiry` 变成空操作。**变异验证**：摘掉 Sync
  那句第 4 个参数 ⇒ 本用例立刻红，报 `roundEnd=1700043200`）
- `the_log_credential_has_one_read_home`（静态守卫：`SameInstanceKey(rec.kwLogKey` 全仓 1 处、
  住在 `L.KeywordLogAdmits` 体内；两个写点都调 `L.NoteKeywordLogThisInstance`；不许在调用方
  各自拼 `InstanceKey`）
- `migrate_stamps_the_log_key_for_rows_already_alive`（v10：烙当前编号、幂等、没布尔位的一条不碰、
  只烙不给勾、返回值进迁移日志）；v6/v7 阶梯用例终态预期 9→10。
- `migrate_stamps_this_weeks_log_key_when_the_anchor_is_stale`（fresh review 查出来的那条，见下面
  "review 之后补的一刀"）。

### review 之后补的一刀：v10 在"周四刚过、代码刚更新"那一趟会白烙

fresh reviewer 逐条查下来三条，第一条是真缺陷：

**v10 拿的是存档里的旧锚点。** 这一档的编号来自 `cycleAnchor + 604800` 那档尾巴，而
`Config.Init` 只在锚点为 `0/nil` 时补问周重置、**上一个 CD 的旧值它不动** ⇒ 更新完正好跨过周重置
的那趟登录，锚点还是上周的 ⇒ 烙出来的是**上周那一桶**；同一趟里锚点随后前进 ⇒ 当前编号挪一档
⇒ 刚烙的凭据对不上自己，梦魇任务照旧整行消失，而且这次是"改完那一整周都没变化"，比不迁移更难查。
邻居 v6 `BackfillInstanceKeysOnce` 开头早就处理过同一个形状（"补问之前那个锚点"那段守卫），
v10 照抄：`锚点 + 604800 <= 同域参考` ⇒ 先 `Config.RefreshCycle(wall, up)` **一次**再算编号。
代价不破口径：锚点没过期时一次都不许多问周重置（用例里那组对照钉的就是这个，实测 0 次）。
reviewer 同时点名"这一刀 revert 掉不会有任何用例变红"——所以先写用例打红再动手：
红的时候报的是 `锚点过期 ⇒ 迁移必须先补问一次把锚点推到本 CD，实际 1760595200`，
用例的前置那一句先钉住"不补问时这条算得出的是上周那一桶 `94446:1956889`、本 CD 是 `1957561`"，
免得后人以为那两桶是同一个数。

另外两条判为**不改、只记**：

- **跑完停在 10 档、即使一条都没烙**（`Migrate` 的契约是"档位推进与处理条数无关"，v2-v9 同形状）。
  残留角落只在这一条上：那一趟周重置接口问不出 ⇒ 锚点定不出 ⇒ 编号烙不上且**不会重试**。
  后果与"没修之前"同档（那一行的布尔位还挂着，号一接上就重新烙），不是更差；而周接口问不出的时候
  整个编号模型都退化，这一档不是主矛盾。要治得改 `Migrate` 的档位契约，代价大于本轮收益。
- **`KeywordLogAdmits` 里那句 `Config.KeywordMatch` 是每行重复扫配置**（矩阵一帧最多 3 次）。
  不并到 `MatrixRows` 预扫的 `kwHit`：那份判据在 Sync/Dump/Probe 三条路上都没有 `kwHit` 可传，
  把判据拆成"界面用预扫、别处自己扫"正是本轮静态守卫（`the_log_credential_has_one_read_home`）
  要防的分岔。`KeywordMatch` 是纯 Lua 子串匹配（关键词表个位数长度），零接口调用、不碰轮询。

### 真档只读回放（验收 2）

`python tests/tmp_kwresident_replay.py "<WTF>/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua"`
（灌真档 → 跑 `Config.Migrate` → 把 `kwLogAdded` 全部撤光模拟"没接这条的号登录"）：

```
① 当前档面板行数 = 16
② 跑迁移 Migrate -> True | 档位 = 10 | 烙上编号的记录 = 2 [96714, 94446]   迁移后 16 行
③ 撤掉布尔位 2 条之后面板行数 = 16
   掉出去的行：0
   94446 记录：kwLogAdded=None kwLogKey=94446:1993148 本周编号=94446:1993148
```

（这一段跑的是 16:35 落盘那份，比 §一/§二 用的 14:57 那份**新了 1 行**（面板 15→16）：这半小时里
存档里又多了一条同样只靠布尔位顶着的行 —— **96714 虚空决战瓦尔（英雄）**，形状与 94446 一模一样
（`kwLogAdded=true`、`currentSig="-1"` = 客户端答不出金额）。§一 那批取证数字照旧成立：
同一跑法在**修法前**是 15 → **14**，掉的正是梦魇任务。）

### 已知角落（少报方向，照实记）

- 一条日志维持的记录如果本周曾上过板、`roundEnd` 里留着一条**已经过去**的板面结束时刻，
  `L.InstanceEnd` ①档判不出"还活着" ⇒ 编号算不出 ⇒ 这份凭据不放行。R5（`SetExpiry` 的
  `resident`）拦的是"还活着的那条板面实例把身份挪档"，这条角落的行为**与今天一致**
  （今天布尔位一撤照样掉行），本轮只做放宽，没有变得更差。
- `rec.name` 还没问出来的记录（`KeywordMatch(nil)` 恒假）拿不到这份凭据 —— 少报不多报。
- Dump 的"在列靠"新增那句（`本周有号接进任务日志（当前这个号已经没了，这一条实例 …）`）
  只在真档出现这种形状时走得到，离线用例没有直接钉它（界面文案不受影响）。
