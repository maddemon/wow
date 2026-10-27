# WorldQuestTracker：忽略=永久屏蔽，添加=取消忽略——实施计划

> 旧名横幅：本文件写成时插件还叫 WorldQuestTracker（全局 WQT / 存档 WQTDB / 命令 /wqt / 测试 run_wqt_tests.py）；2026-10-27 起改名 GoldQuestLedger（GQL / GQLDB / /gql / run_gql_tests.py）。逐轮决策记录按当时口径保留，不追改。

> **For agentic workers:** 逐任务实施，每个改动步骤先跑 `python tests/run_wqt_tests.py` 看到 RED，再实现，再跑到全绿。步骤用 `- [x]` 勾选。改完提交前跑一次 `python tests/run_wqt_tests.py --syntax`。

**Goal:** 把"忽略 / 添加"改回一对真正的互逆操作 —— 玩家右键**移除 = 永久屏蔽**（不再换 CD 自动解除），矩阵"添加任务"里点一行 = **取消忽略**（不再是"标记这条要强制上板"）。`manualAdded` 这个字段整体下线：一条任务在不在矩阵，只由客户端和玩家配置说了算。老存档里已经躺着的所有忽略/添加标记一次性作废，玩家重新点出自己想要的那批永久屏蔽。

**Architecture:** 分层不动。改动落在 `Ledger.lua`（准入门少一道、忽略改永久、`ClearCycleIgnores` 删除、新增 v5 迁移）、`Config.lua`（迁移档位 + 存档结构注释）、`Sync.lua`（换 CD 不再解除忽略）、`Track.lua`（提示文案）、`Matrix.lua`（弹框标签 + 确认框文案）、`Dump.lua` / `Probe.lua`（取证口径）、`tests/run_wqt_tests.py`（用例）、`docs/`（口径）。

**Tech Stack:** WoW 12.1.5 内嵌 Lua（5.1 语义）、SavedVariables `WQTDB`、离线测试 Python 3 + lupa（`tests/run_wqt_tests.py`）。

---

## 一、用户口径（原话，不要改写）

> "我最开始 忽略 任务的意图 就是想把 关键词匹配到了我不想要的任务，比如丰饶关键词，匹配到了辉煌丰饶，但是辉煌丰饶不是世界任务，我要屏蔽掉，那么我这个意图其实是想永久屏蔽的。手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了。所以你先把所有已经做了忽略 / 添加 的数据 清理掉，然后实现上面真正的意图"

拆成四条可验收的规则：

1. **永久**：点了"移除"的那条，换轮、换 CD、换版本都不许自己回来。（触发案例：关键词"丰饶"误命中"辉煌丰饶"，它不是世界任务，要一辈子别上板。）
2. **添加 = 取消忽略**：弹框点一行的全部作用是撤掉那条意图，不是"玩家标记了这条要显示"。
3. **取消忽略之后该不该上板，由自动判据重算**：在不在板上 / 金币过不过门槛 / 命不命中关键词 / 是不是注册周常 / 本轮有没有号交过。
4. **老数据先清**：升级时一次性清掉所有历史 `ignored` 与 `manualAdded`，永久屏蔽从干净的一页开始 —— 否则上一版"只管本 CD"语义下随手点的忽略，会在新语义里永久生效，那是拿旧意图当真凭据。

## 二、为什么 `manualAdded` 必须整个下线（不是只改文案）

`rec.manualAdded` 现在是矩阵的六道准入门之一（`Ledger.lua:1661-1664`），而且它单独撑破了三条别的规则：

| 落点 | 现在的行为 | 与"添加=取消忽略"矛盾在哪 |
|---|---|---|
| `MatrixRows` 准入门 | 点过一次添加就绕过金币门槛 | 玩家没在"标记加了"，他在撤忽略 |
| `OccupiesRow:143-146` | 手动点名的族子任务破例占第二行；名字问不出的族本体靠它占行 | 一族一行是用户 2026-10-24 定的硬口径，破例来自一个玩家本没有的意图 |
| `MatrixRows:1712,1744,1755`（`row.manual`） | 手动那条不参与同名去重竞争 | 同上：豁免权来自一个不存在的意图 |
| `QuestKnown:1148` | 玩家点过添加 = 交任务记账的可信身份 | 手点不该是凭据（用户口径"客户端答得出就按客户端"） |
| `row.watched:1720` | 手动那条跳过"本资料片过滤" | 同上 |
| `RollRound:533` | 换轮清掉它 | 上一轮为此专门加的规则；字段没了这条也不用存在 |
| 弹框"已加"标签 / `Dump:242` / `Probe:1599,1661` | 报"玩家手动添加" | 界面在谎报玩家做过的事 |

保留它 = 六道门里仍然有一道是"玩家点了一下"，而用户明确说那不是他的意图。**删干净**才是"添加=取消忽略"，而且准入门从此全部来自客户端或玩家配置，这一格以后不会再出现"我明明没做过它怎么在列"这类报修。

## 三、最终口径表（改完之后）

| 概念 | 字段 | 归谁写 | 活多久 | 作用 |
|---|---|---|---|---|
| 永久屏蔽 | `rec.ignored = true` | 右键"移除"（`Ledger.RemoveQuest`） | **永久**，直到玩家在同一弹框点回来 | 唯一一道否决门；记录本身留下（金币账不许因为藏了一行少一笔），陈旧清理也不丢 |
| 取消屏蔽 | `rec.ignored = nil` | "添加任务"弹框 / 地图 pin 点一下（`Ledger.AddQuest`） | — | 只撤否决；撤完由自动判据重算 |
| 上板证据 | `rec.poolSeen` / `onBoard` | 快照 DiffPool / ScanBoards | 按轮次 | 准入 + 地图 |
| 本轮金币凭据 | `goldRound` / `reads` | 快照 | 按轮次 | 准入（门槛 500g，配置项） |
| 本轮有人交过 | `db.turnins[char][quest]` | QUEST_COMPLETE | 按轮次 | 准入（接口读不出金额的靠这道） |
| 关键词 | 配置 `keywords` | 玩家 | 永久（改配置即改判） | 准入 |
| 日志收的 | `rec.kwLogAdded` | 关键词 + 任务日志 | 跟着配置走 | 准入 |
| 注册周常 | `rec.regKey` | 内置表 / 关键词注册 | 常驻 | 准入 |
| ~~手动添加~~ | ~~`rec.manualAdded`~~ | — | — | **删除** |
| 格子补录 | `db.manual[char][quest]` | 点矩阵格子 | 按轮次（上一轮已修，本轮不动） | 单格状态 |

五道准入门（`kwLogAdded` / `kwHit` / `regKey` / 本轮交过 / 金币≥门槛）+ 一道永久否决（`ignored`）+ 一族一行（`OccupiesRow`）。

## 四、任务清单

### Task 1 — v5 迁移：一次性清掉所有历史忽略与添加标记

**Files:** `WorldQuestTracker/Ledger.lua`（`ClearManualAddsOnce` 旁边加新函数）、`WorldQuestTracker/Config.lua:271`（`SCHEMA_VERSION`）、`Config.MIGRATIONS`

- [x] 1.1 RED：新增用例 `test_the_upgrade_clears_every_old_ignore_and_add_mark`（放在 `test_the_upgrade_drops_the_permanent_manual_add_marks` 那条 v4 用例后面，注册进 `tests` 列表）：
      照 v4 用例的做法走真实入口造四条记录 —— A 只靠 `AddQuest` 撑着（手动标记 + 非金币）、B 本轮读到门槛金币、C 命中关键词、D 注册周常；再给 A、B 各点上"移除"（`WQT.Ledger.RemoveQuest(WQT.Config.DB(), qid)`），
      然后把 `db.quests[qid].manualAdded` / `.ignored` 手工写成 v3 存档的形状（`ignored` 写成**时间戳**，模拟老版本写法），`db.schema = 4`；
      调 `WQT.Config.Migrate(db)`，断言：日志出现 `数据升级 v5 完成`；两条 `ignored` 都变成 `nil`；`manualAdded` 也 `nil`；
      B 因为金币凭据还在、C 靠关键词、D 靠 regKey 照旧在列；A 掉出矩阵；
      再跑一次 `Migrate` 返回 false（档位闸门守住，v5 不许每趟登录都清）。
- [x] 1.2 实现 `function L.ClearIgnoreAndAddMarksOnce(db)`（放在 `ClearManualAddsOnce` 之后）：遍历 `db.quests`，清 `rec.ignored` 与 `rec.manualAdded`，返回清理条数。注释要说清三件事：
      ① 为什么清**老忽略**也一并清老添加 —— 永久语义生效后，上一版"只管本 CD"里随手点的那次移除会永久压着一行，而玩家从没打算永久屏蔽它；
      ② 代价照实 —— 用户真正想永久屏蔽的那几条（辉煌丰饶那种）会回到矩阵，需要重新右键点一次；这句必须写，否则下次有人以为又漏清了；
      ③ 闸门用档位（`db.schema`），不用"标记非空即跳过"（`ResetStickyGoldOnce` 踩过的坑）。
      同时说明 v4 的 `ClearManualAddsOnce` 为什么保留：档位阶梯要连续（v3 存档跑 4 再跑 5），删掉中间档会让升级断链。
- [x] 1.3 `Config.SCHEMA_VERSION = 5`，`MIGRATIONS[5]` 接 `L.ClearIgnoreAndAddMarksOnce`；v4 那一条的注释补一句"本轮之后 `manualAdded` 字段整体下线，v4 只管升级阶梯连续"。
- [x] 1.4 GREEN：`python tests/run_wqt_tests.py -k the_upgrade` （只跑迁移族）到全绿。

### Task 2 — 忽略改成永久：删 `ClearCycleIgnores` 与它的调用点

**Files:** `WorldQuestTracker/Ledger.lua:908-942`、`WorldQuestTracker/Sync.lua:745-753`

- [x] 2.1 RED：把 `test_an_ignore_only_last_until_the_next_cycle`（`run_wqt_tests.py:9054`）改名并**反转**成 `test_an_ignore_outlives_every_cycle_and_round`：
      同一个 CD 内照旧压着（原断言留着）；把锚点退回上周、再推过一次换 CD，断言 `ignored` **还在**、这行**还不列**（哪怕它 30000g 远超门槛又在池里）；
      日志里不许出现"换了 CD：解除"；再走真实入口 `WQT.Ledger.AddQuest` 取消忽略，断言行回来。
      文档字符串要引用户本轮原话（永久屏蔽那个"辉煌丰饶"案例），并写清"上一版按 CD 作废是理解错了意图"。
- [x] 2.2 实现：删除 `L.ClearCycleIgnores` 整段（含注释），删除 `Sync.lua:745-753` 那一段；`local rolled = WQT.Config.RefreshCycle(...)` 保留赋值但不再有人读 → 改成 `WQT.Config.RefreshCycle(...)` 一句不接返回值（`RefreshCycle` 自己会打日志，调用本身要留）。
- [x] 2.3 `L.RemoveQuest(db, questID, now)` → `L.RemoveQuest(db, questID)`：写 `rec.ignored = true`（永久意图不需要时间戳，留着数字会被读成"到期时刻"），`rec.manualAdded = nil` 那一行随 Task 3 一起消失；注释重写为"永久：换 CD、换轮都不作废，只有玩家在'添加任务'里点回来才撤"。
      更新调用点：`Track.lua:147`（`WQT.Ledger.RemoveQuest(db, questID, NowSeconds())` → 去掉第三参；`NowSeconds` 仍被 `AddQuest` 用着，别一起删）、用例里的 `RemoveQuest(..., anchor + 10)`（`run_wqt_tests.py:9067`）与 `RemoveQuest(WQT.Config.DB(), 103, 1700000000)`（同文件 5888）。
- [x] 2.4 顺带改口的注释（都还在说"本 CD"，现在是永久）：`Ledger.lua:1079,1083` 注册补扫、`Ledger.lua:1352-1370` `Prune`（永久忽略的记录永久保留是对的，写明这是有意的）、`Sync.lua:221-224` 日志源跳过、`Dump.lua:344`（"被忽略"→"永久忽略"）。
- [x] 2.5 GREEN + 全量 `python tests/run_wqt_tests.py`。

### Task 3 — `manualAdded` 字段下线：准入门六道变五道

**Files:** `WorldQuestTracker/Ledger.lua`（138-147、415、519-534、845-906、1141-1149、1648-1664、1712、1720、1740-1760）、`Config.lua:14-15`

- [x] 3.1 RED（先钉"点添加不再强制上板"）：新增 `test_a_click_add_does_not_force_the_row_onto_the_matrix`，并把上一轮的两条 round-scope 用例（`manual_add_expires_when_the_instance_rolls`、`manual_add_survives_within_the_same_instance`）**删掉**——它们钉的规则已经不存在了。新用例复用 `manual_add_env`：
      两条都在池里、都不给金币、都不命中关键词：对第一条调 `AddQuest`，第二条当对照组；断言两条都**不列**，且 `read(qid, "manualAdded") == "nil"`；
      再给第一条点上金币档（下一轮 `DiffPool` 给 `money = 门槛以上`），断言它靠金币门回来 —— 证明"能不能上板"只剩客户端凭据。
      对照组（本轮读到门槛金币的）必须照旧在列。
- [x] 3.2 RED：`test_family_anchor_lists_all_week_and_the_board_child_does_not`（`run_wqt_tests.py:5900` 附近）删掉"玩家右键点名要看的子任务才破例占行"这一断言块（`manualAdded = true` 后期望占行那段），改成**反向断言**：族子任务无论什么标记都不占第二行。
      同名去重那两条（`run_wqt_tests.py:6688`、`6937`）删掉"玩家手动点名加的那条不许被吃掉"的断言块（`row.manual` 豁免没了）；改成断言同名只留证据最强的那条。
      `run_wqt_tests.py:2277` 的 fixture 用 `manualAdded` 把行拉进矩阵 —— 换成真凭据（给 `poolSeen` / 金币观察，或配一个关键词），别保留这个字段。
- [x] 3.3 实现 `Ledger.AddQuest`：不再写 `manualAdded`；保留"建记录 + 问名字/地图/金额 + 写 observation + 续 `lastSeenAt` + 清 `ignored` + 定到期"。函数头注释重写：这条路径的**玩家侧含义只有一句"撤销别再显示这行的意图"**；记录建出来是为了让快照/取证后面问得到它，不承诺上板。`addedBy == "questlog"` 分支（`kwLogAdded`）不动。
- [x] 3.4 实现 `L.MatrixRows`：准入条件去掉 `rec.manualAdded`，注释重写成五道门 + 永久否决；`row.manual` 字段删掉，同名去重里 `if r.name and not r.manual` / `if r.manual or not r.name or best[r.name] == r` 两处跟着简化；`row.watched` 里去掉 `rec.manualAdded`。
- [x] 3.5 实现 `L.OccupiesRow`：去掉两处 `manualAdded` 让步 —— 非锚点的族成员一律不占行；族本体问不出名字就不占行。函数头注释里"两条让步"改成"一族一行没有例外"+ 保留裸 ID 不占行那条理由。
- [x] 3.6 实现 `RollRound`：不再清 `manualAdded`（字段没了），注释里那段"玩家那次添加任务（manualAdded）"整段删掉，改成"换轮作废两份凭据：金币粘滞 + …"（只剩一份就照实说一份，别留悬空引用）。
      `L.QuestKnown`：去掉 `rec.manualAdded`（`ignored` 留着 —— 玩家明确表过态的行，账还是要记）。
- [x] 3.7 `QUEST_RECORD_FIELDS`（`Ledger.lua:400-420`）：**保留** `manualAdded = true` 这一项，旁边加注释"字段已无读者，只有 v4/v5 迁移函数还会清它（`rec.manualAdded = nil`）；留着是为了字段清单守卫不许谎报"。删掉它会让守卫用例 `test_quest_records_only_come_from_the_factory` 把迁移函数自己报红。
- [x] 3.8 `Config.lua` 存档结构注释：删掉 `manualAdded=<...>` 那两行，补一行 `ignored=<玩家右键"移除"过 = 永久屏蔽，只有点"添加任务"才撤销>`。
- [x] 3.9 GREEN + 全量。

### Task 4 — 界面与提示改口

**Files:** `WorldQuestTracker/Track.lua:115-158`、`WorldQuestTracker/Matrix.lua:1680,1711-1817,1821-1833`

- [x] 4.1 RED：`test_right_click_remove_survives_resync`（`run_wqt_tests.py:7456`）改断言：
      确认框文案要出现"不再显示「任务190」"和"永久"，不许再出现"本周" / "下个 CD 自动作废"；
      删掉 `manualAdded` 那条断言（`7492` 行），改成只查 `kwLogAdded` 没被留下；
      取消忽略后的 print 文案断言从"已恢复显示"改成"已取消忽略"；
      弹框标签断言里不许再出现"已加"（`frame_texts` 全量比对，`7505`/`7517` 那两处）。
      再加一条：`AddQuest` 对一条**没被忽略过**的池任务点一下，print 要说清"不强制上板"，断言 `matrix_listed` 没变化。
- [x] 4.2 `Track.AddQuest`：`wasIgnored` 分支文案 ——
      取消忽略：`WQT: 已取消忽略 %s（会不会重新上板，按在不在板上 / 金币 / 关键词判）`；
      本来没被忽略：`WQT: 已记录 %s（手动添加不再强制上板；没在矩阵 = 插件判它这轮不该列）`。
      `Track.RemoveQuest`：`WQT: 已永久屏蔽 %s（换 CD、换轮都不会自己回来；要恢复点矩阵的"添加任务"）`，主语用任务名（照 `RemoveTip` 的做法，`#questID` 只在账本没名字时兜底）。
- [x] 4.3 `Matrix.RemoveTip`：`确定不再显示「%s」？\n（永久屏蔽：换了 CD、换下一条实例都不会自己回来；想恢复点"添加任务"里那一行）`。
- [x] 4.4 `Mx.RefreshAddList`：标签只剩"已忽略"（点一下撤销）与"矩阵已有"，删掉"已加"；`has()` 里去掉 `rec.manualAdded`；顶部 hint（`1680`）同步改成 `“已忽略”=点一下撤销 “矩阵已有”=自动在列`；函数头注释与排序注释跟着改口。
      弹框标题/按钮文字若含"添加任务"就保留（用户认知里它就是"找回被屏蔽的行"的入口），但在 hint 里说清点它是撤销屏蔽。
- [x] 4.5 GREEN + 全量。

### Task 5 — 取证层：Dump / Probe 别再报"玩家手动添加"

**Files:** `WorldQuestTracker/Dump.lua:235-244,344`、`WorldQuestTracker/Probe.lua:945,1599-1600,1661`

- [x] 5.1 `Dump.lua`：删掉 `if rec.manualAdded then why = "玩家手动添加" end`；那句"不在列（非手动、没命中关键词…）"改成如实的"不在列（没命中关键词、本轮没人交过、金币也没到 X）"；"被忽略=%s"改口成"永久屏蔽=%s"。
- [x] 5.2 `Probe.lua`：`src` 三选二（"手动/日志/池"）去掉"手动"这一支；`if rec.ignored then src = src .. "+已忽略" end` 改成 `"+永久屏蔽"`；`1661` 那处同理。
- [x] 5.3 跑一次带 dump/probe 的用例族（`python tests/run_wqt_tests.py`）确认没有断言依赖旧文案；有就改代码别改期望，除非期望本身在钉旧语义。

### Task 6 — 文档

**Files:** `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`、`docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`

- [x] 6.1 checklist 追加 `## 27. 第二十七轮：忽略=永久屏蔽，添加=取消忽略（2026-10-27，N/N）`：用户原话、§三那张最终口径表、`manualAdded` 为什么整个下线（含 §二的矛盾清单）、v5 清了两类数据与"你得重新点一遍真正想屏蔽的那几条"这条代价、被本轮取代的两条上一版规则（§25 的"换 CD 自动解除"、§26 的"manualAdded 只算本条实例"）、游戏内验收清单、用例清单。
- [x] 6.2 在 §25、§26 的标题行下面各加一行"（已被 §27 取代：…）"，正文别删 —— 历史口径要留痕，后来人得知道为什么曾经那样写过。
- [x] 6.3 设计文档：§6-C 准入门改成五道 + 永久否决；§7 存档结构删 `manualAdded`、给 `manual.ignored`/`rec.ignored` 写永久语义；§8.2 那条"右键移除只管本 CD"改成永久；提到 `ClearCycleIgnores` 的地方全删。

### Task 7 — 收尾验证

- [x] 7.1 `python tests/run_wqt_tests.py --syntax` 全绿（Lua 语法 + upvalue 预算守卫）。
- [x] 7.2 `python tests/run_wqt_tests.py` 全绿；报出总数。
- [x] 7.3 逐文件与部署目录 diff（`D:/Games/World of Warcraft/_retail_/Interface/AddOns/WorldQuestTracker/`），确认部署副本落后于仓库 —— 部署时**整目录**拷，别只拷改过的文件（上一版就是这么出"改了没生效"的）。

## 五、风险与代价（照实说，别藏）

1. **v5 清完，用户之前点的屏蔽全部失效** —— 包括他真想永久屏蔽的那几条（辉煌丰饶）。这是他自己要求的顺序（"先清理掉，然后实现真正的意图"），代价是把真正想要的那几条重新右键点一次。文档与迁移注释都要写这句。
2. **"添加任务"不再能强制显示一条非金币任务** —— 想跟踪门槛以下的任务，正路是加关键词或调门槛（两条都是配置项、都永久生效）。别再往回加"点一下就上板"的门。
3. **一族一行的破例没了** —— 玩家点名的族子任务不再占第二行。这正是用户 2026-10-24 定的口径，破例本来就是为了兜住那个现在不存在的意图。
4. **`row.manual` 同名去重豁免没了** —— 玩家点名加的那条同名任务可能被更强的证据挤掉。可接受：他不点名，是撤忽略。
5. **`QuestKnown` 少一份身份** —— 只靠 pin 点出来、客户端一个字没答的任务，交任务不再记账。符合"客户端答得出就按客户端；答不出就如实降级"。
6. 不动的东西：`db.manual` 格子补录的轮次口径（上一轮刚修，本轮无关）、金币粘滞、注册周常、地图口径。
