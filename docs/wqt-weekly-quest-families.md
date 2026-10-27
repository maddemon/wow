# WQT 周常族知识：丰饶、加固符文石

写于 2026-10-24（第十五轮反馈之后，用户决定停止本轮迭代）。这份文档只干一件事：把**已经钉死的口径**
和**已经实测过的结论**留在那里，别的会话不必再从头猜一遍，也不必再犯同一个错。

标注约定：
- **实测** = 12.1.5 国服本机回传（`/wqt probe`、`/wqt quest`、矩阵截图）给出的答复。
- **参考实现** = SavedInstances 本机源码里抄来的口径（`Modules/Progress.lua`），我们没有它的数据源。
- **未实测** = 按命名规律猜的候选，答不出会自动跳过，别当成结论。

---

## 1. 一族一行的模型（矩阵里"族"怎么算）

- 族**只认内置表** `Api.WEEKLY_PRESETS`（`WorldQuestTracker/Api.lua:313`），不认关键词。
  写了关键词 `丰饶` 不等于建了一族；关键词只决定"这条名字要不要绕过金币门槛"。
- **占行的是族本体（锚点 ID）**，板上/日志里那些同名子任务**只当两件事的证据**：
  ① 这一族本周还活着；② 本周它挂在哪张图。判据唯一入口 `Ledger.OccupiesRow`
  （`WorldQuestTracker/Ledger.lua:134`）—— 别在别处再抄一份"占不占行"的判断。
- **族状态取全族最强的一条**（做过 > 在做 > 可做 > 问不出）：`Ledger.Families`
  （`Ledger.lua:147`）、锚点挑选 `Ledger.FamilyAnchorID`（`Ledger.lua:99`）。
- **到期口径由"它是什么"决定，不由哪个接口先答出来决定**：`Ledger.SetExpiry`（`Ledger.lua:503`）。
  带 `regKey` 或 `kwLogAdded` 的常驻周常 → 只认 CD 尾巴（`SetCycleExpiry`，下次周重置）；
  没注册的池内世界任务 → 用它自己问出来的板面倒计时。
  **病因（实测）**：图上一换轮，板面条目只剩几小时，可它的每周 lockout 还活着 ——
  拿板面倒计时当族本体的到期，当天下午整行就消失（加固符文石"这个任务没了"就是这个）。

---

## 2. 丰饶（`abundant-offerings`）

| 项 | 内容 | 依据 |
|---|---|---|
| 族本体 ID | **89507**，名字"丰饶供品" | 参考实现（SavedInstances 同一族只有一条：`type=single`、`questID=89507`、`reset="weekly"`）；实测 `C_QuestLog.GetTitleForQuestID(89507)` 答得出"丰饶供品" |
| 族词 | `match = "丰饶"` | 内置表 |
| 它的 meta/子任务 ID | 参考实现另有 meta 93890；**我们没有可靠来源拿板面子任务的 ID** | 参考实现；**未实测**，不要往内置表里塞猜来的 ID |
| 地图 | **实测：89507 在本机一个地图来源都没有**（`C_TaskQuest.GetQuestZoneID` 答 nil、`C_QuestLog.GetQuestInfo` 整个 MISSING） | 第十五轮 probe/quest 回传 |
| 于是界面 | 没有"本轮同族成员带着可信地图"时，那一行落在底部**未知地图**。这是设计，不是 bug —— 另一条路是编一张图。 | 第十七轮另接两条凭据（§4），但 89507 从没上过板，那两条对它照样可能答不出 |
| 状态 | 判"做过"走 `Api.QuestState` 的 DONE_SOURCES 链（`C_QuestLog.IsQuestCompleted` → `IsQuestCompleted` → `C_QuestLog.IsQuestFlaggedCompleted`，`Api.lua:692`）。**`IsQuestFlaggedCompleted` 单独不作数**：实测它对一次性任务做过一次就永远回"做过"，纳进家族等于每个号永久打勾。要叠着本周窗口（cycleAnchor）看；族本体自己答得出剩余（实测 89507=119.4 时）才证明它是每周 lockout。 | 实测 + 内置表 §12 |

**已定结论（别再翻）**
- 丰饶只跟 89507。板上那些"丰饶：xx 洞穴"式的轮换条目不占行、不建注册行。
- 悬停看到的"丰饶"和真正要完成的"丰饶日常"不是同一个任务（用户原话），所以**以反查完成的那条为准**。
- **绝不许把"返回玩家当前所在地图"的接口接回生产**：`C_QuestLog.GetMapForQuestPOIs` 实测答的是脚下这张图
  （站在屠戮者高地时 89507 也答 2444），这就是"丰饶跑到了屠戮者高地"的唯一解释。

---

## 3. 加固符文石（`fortify-the-runestones`）

| 项 | 内容 | 依据 |
|---|---|---|
| 族 ID | **90573 / 90574 / 90575 / 90576**（同一个周常的四个骑士团版本） | 参考实现 SavedInstances `type="any"`：**任一即算本周做过** |
| 族词 | **故意不给 `match`** | 见下 |
| 实测答复 | 90573：名=加固符文石：魔导师、剩余=118.1 时、做过=done。90574-90576：**名字问不出** | probe 回传 |
| "问不出"≠"没做" | `IsQuestFlaggedCompleted` 对客户端不认识的 ID 恒回 false，那是问不出 | 实测 |

**为什么不给 `match="加固符文石"`**：账本里还有另一族也叫这个名字 ——
"**特别任务：加固符文石**"（它跟 fortify-the-runestones 不是一回事）。写了族词就会被拖进族待遇
（非锚点不占行），结果是那一整行**凭空消失**。这条已被用例
`pool_countdown_does_not_kill_a_registered_weekly` 钉住。
**要动 `match` 之前先想这条。**

---

## 4. 地图证据链现状（12.1.5 本机）

- **生产链两条、真能答的只剩一条**（`Api.QUEST_MAP_SOURCES`，`Api.lua:765`；取用入口 `Api.QuestMapID`，`Api.lua:893`）：
  - `C_QuestLog.GetQuestInfo` —— 本机 **MISSING**（接口根本不存在）。
  - `C_TaskQuest.GetQuestZoneID` —— **实测唯一对得上板清单的一条**（91805/91808/91811 全答 2437，与它们所在那张图一致）。
- **三条已从生产摘掉**（`Api.MAP_PROBE_EXTRA` 里标 `已停用`，只在 probe 出现，`Api.lua:894`）：
  | 接口 | 为什么停 |
  |---|---|
  | `C_QuestLog.GetMapForQuestPOIs` | 答的是玩家脚下那张图 |
  | `C_TaskQuest.GetQuestInfoByQuestID` 第 2 返回值 | 不是地图号（2696/1859 的地图名问出来是 nil） |
  | `C_TaskQuest.GetQuestLocation` | 真 Usage 是 `(questID, uiMapID)` 两参，单参必抛错、只回 x,y |
  这三条同时是 `Api.RetiredMapSources()` 的内容：它们写进账本的地图号会在每次同步开头重问
  （`Ledger.ClearRetiredMaps`，`Ledger.lua:190`）。**重问分两种下场，判据是"这条真上过板没有"**：
  | 记录形状 | 处理 | 为什么 |
  |---|---|---|
  | `poolSeen`（账本记着它挂过世界任务板） | **不清图，只降级标签** `存疑：谎报接口写的`（`Ledger.SuspectMapSource()`，`Ledger.lua:188`） | 换轮换出板后，板面来源只认**当下还在板上**的任务，那个图号永远补不回来 —— 第十七轮"任务所属地图乱了、12 行堆在未知地图"就是这么造出来的 |
  | 没上过板（注册周常那种，例：丰饶 89507） | 照旧清成 nil | 那张图是接口按玩家脚下猜的，留着就是第十五轮反馈⑥"丰饶出现在了屠戮者高地" |
  两种标签都**不在** `TRUSTED_MAP_SRC` 白名单（`Ledger.lua:242`）：不参与族本体搬图，也不冒充本轮证据；
  `Sync.HealMissingMaps` 每轮照旧重问，问得出就覆盖成新答复 + 新标签。
- **两条给"不上板"任务的新凭据（第十七轮接进生产）—— 12.1.5 用户 2026-10-24 回传：两条本机全废**：
  - `C_QuestLog.GetQuestLogZoneMapID(日志索引)` —— **MISSING**（取证里那行 `MISSING C_QuestLog.GetQuestLogZoneMapID`
    就是它）。日志行自己 25 个字段里也没有地图字段（第十五轮实测）。代码链照旧留着、不答就什么都不写：
    `Api.QuestLogZoneMapID`（`Api.lua:476`）→ `Api.AcceptedQuests` 每行带 `mapID`（`Api.lua:517`）
    → `Sync.SyncKeywordQuestsFromLog` 只给"地图还没落实"的记录写，标签 `日志分区`（在白名单里）。
  - `C_QuestLog.GetBountiesForMapID(图)` —— 接口**在**，但**不按图区分**：实测"12 张图答得出，共 60 行、
    去重 5 条"= 每张图回同一批 5 条，正是第十二轮那条形状 → **跨图自检整批作废**，一条都没归图。
    （`Api.BountyQuestIDs`，`Api.lua:1039`；并进 `Api.MapQuestIndex` 的第二遍，`Api.lua:1065`；
    判据就在 `/wqt quest` 那句"同一批重复出现 = 不按图区分，这条地图凭据本轮作废"。）
  ⇒ **本 CD 结论：换轮换出板 / 压根不上板的那些行，接口侧一条地图凭据都不剩**，只剩玩家口述这一条路。
  两条凭据的代码**不删**（换版本可能就有），但别再拿"接上凭据"当这批行的出路。
- **玩家口述地图（第十七轮的"数据侧"补救，`Api.MAP_MEMORY`，`Api.lua:337`）**：
  图号一旦被清掉就**没有任何本地恢复路径** —— `observations` 只记金币/sig，不存地图历史，
  而这批任务本周既不在板上、接口也全答不出。所以唯一的来源是玩家自己知道哪个任务在哪张图。
  - 表在代码里（`{ name = "幽魂角斗士", map = "盘卷蛇岛" }` 或 `{ questID = 90573, map = "永歌森林" }`），
    **图名照矩阵组头写**，由 `Sync.ApplyMapMemory`（`Sync.lua:317`）在跟踪清单里现解成图号，
    不配就如实写日志（"不在跟踪清单里" / "账本里没有叫这个名字的行"），绝不猜。
  - 落账走 `Ledger.SetManualMap`（`Ledger.lua:226`），标签 **`玩家指定`**（在 `TRUSTED_MAP_SRC` 白名单里）：
    `ClearRetiredMaps` 不动它，接口问得出真凭据会覆盖它，表里改了图号下一轮重新生效。
  - **不做点击/拖拽 UI**（用户第十七轮："不一定要在 ui 里指定所属地图，除非每一行可以拖拽最省事"）：
    逐行点选的补地图弹框写过又删了；玩家只需要在对话里报"任务名 → 图名"，改的是这张表。
- **其余地图线索全部实测无效**：日志原始行 25 个字段里**没有任何地图字段**；`GetNextWaypoint` 答 nil；
  `GetQuestTagInfo` 只有 `{displayExpiration,isElite,quality,tagID,tagName,worldQuestType}`。
  ⇒ 逐条问任务的那批入口对"追踪萨拉塔斯"（98172）确实一个都答不出，别再找那种地图源；
  现在只剩上面两条**按图/按日志**问的凭据还没被实测否证。
- **"有 mapID 但没来源标签"的旧记录 = 旧版没来得及记标签的客户端答复，不是谎报接口的残留**
  （那三条写的号每条都带自己的标签）。处理：**留着图号 + 打标 `旧记录未标来源`
  （`Ledger.LegacyMapSource()`）+ 每轮照旧重问**（`Sync.HealMissingMaps` 的 `needAsk`，`Sync.lua:258`），
  问得出覆盖、问不出原样保留；该标签不在 `TRUSTED_MAP_SRC` 白名单，不参与族搬图。
  **上一版把这类清成 nil，代价是整批行塌进"未知地图"，用户实测"越改越坏"—— 不要退回那条。**

---

## 5. 金币、门槛与"做过的为什么看不见"

- 门槛是**配置项**：`db.config.minGold`（单位=**金**），默认 **500**（`DEFAULT_MIN_GOLD`，`Config.lua:32`），
  换算与夹取 `Config.MinMatrixMoney()`（`Config.lua:375`），改值 `Config.SetMinGold/GetMinGold`（`Config.lua:384`/`:414`）。
  ~~设置面板"三、金币门槛"：输入框 + 确定 + 现值回执~~ —— **2026-10-27 那段 UI 整体撤下**
  （用户口径四"三也去掉 暂时不能配置"）。撤的是控件不是判据：`minGold` 仍是唯一真源，
  门槛默认 500 金照旧生效，暂时只能改存档。
- **判门槛的唯一入口是 `Ledger.MatrixMoneyFloor()`，矩阵 / 面板 / `/wqt quest` / `/wqt probe`
  四处都读它。** 各抄一份数字的报告会在改门槛那天开始说瞎话。
- **实测：接口侧读不出世界任务的奖励金额。** `GetQuestLogRewardMoney` 回 0 且 `HaveQuestRewardData=false`；
  `C_QuestRewards.GetCachedQuestMoney` / `C_WorldQuestRewards.GetWorldQuestRewards` 是**未实测**的命名猜测；
  板清单 `C_TaskQuest.GetQuestsOnMap` 的行**没有金额字段**；`RequestPreloadRewardData` 存在但从没填出过数据。
  ⇒ 松露大乱斗那 962 金（用户气泡截图）只有鼠标悬停看得到，**金额这道闸永远判不出它**。
- 于是加的另一道门：**本周任一角色交过这条 ⇒ 直接占行，不看金额**
  （`TurnedInThisCycle`，`Ledger.lua:1021`；准入在 `Ledger.MatrixRows` 的门槛那段）。
  只认这笔**存在**（`g` 可能被邮箱/拍卖污染记成 nil）；换周（`cycle ~= cycleAnchor`）就不算。
  `/wqt quest` 会如实写"在列靠：本周有角色交过（玩家亲手换过金币，不看金额）"。
- 金额链：池行的 `money` **只认 `> 0`**（0 是"数据没到"的默认值，不是"这条不给金币"），继续问 `Api.MONEY_SOURCES`
  （`Api.lua:421`），全链答不出**留 nil，绝不写 0 参与比较**。

---

## 6. 已被用户实测否掉的做法（别再提）

1. **让玩家点任务 / 输入 questID**。玩家不可能知道 ID；界面只收关键词与地图名字
   （2026-10-27 起关键词入口在「关键词 · 屏蔽管理」窗口左栏，不在设置面板）。
2. **全地图树枚举**。只扫跟踪地图（本资料片区域 ∪ 用户 `extraMaps`），否则卡顿源。
3. **把任何"返回玩家当前所在地图"的接口接回生产**（`GetMapForQuestPOIs` 那一类）。
4. **清掉"没来源标签"的旧图号**（见 §4 最后一条）。
5. **写死门槛数字**（现在是配置项）。
6. **把上过板（`poolSeen`）那条的图号也一起清成 nil**（§4 那张表的第一行）。
7. **拿"每张图回同一批"的悬赏清单当地图凭据**（`MapQuestIndex` 那道跨图自检就是防这个；
   12.1.5 本机 2026-10-24 实测**正是这个形状**：12 张图 / 60 行 / 去重 5 条 → 自检整批否掉）。
8. **做"逐行点选补地图"的 UI**（用户明确否掉；只有整行可拖拽才值得做 UI，否则改 `Api.MAP_MEMORY`）。

---

## 7. 离线怎么验（`tests/run_wqt_tests.py`，210/210）

```
PYTHONIOENCODING=utf-8 python tests/run_wqt_tests.py --syntax
```

| 口径 | 用例 |
|---|---|
| 一族一行、板上子任务只当证据 | `family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it`、`anchor_in_the_pool_lists_only_one_row` |
| 上周那条不许冒充本周 + 族本体地图搬法 | `last_weeks_child_does_not_stand_in_for_this_week` |
| 注册周常不被板面倒计时提前到期 | `pool_countdown_does_not_kill_a_registered_weekly` |
| 谎报接口的号要清、旧图号要留 | `retired_map_sources_are_cleared_from_the_ledger`、`legacy_map_number_is_kept_and_reasked` |
| 换轮换出板不许塌进未知地图（`poolSeen` 降级不清图） | `a_rotated_off_board_quest_keeps_its_map` |
| `isHidden` 内部行不进矩阵 | `hidden_log_quests_are_not_collected` |
| 日志分区口径给已接任务配图 | `log_zone_map_gives_an_accepted_quest_its_map` |
| 悬赏反查配图 + 不按图区分整批作废 | `bounty_board_gives_special_assignments_their_map` |
| 注册行来源标签同形状（族白名单才认） | `registered_weekly_map_source_is_labelled_like_the_others` |
| 取证摊全字段 + 新口径一起问 | `probe_prints_every_field_of_a_row` |
| 玩家口述地图补进账本、真凭据可覆盖、别的行不动 | `the_player_can_fix_maps_the_client_cannot_answer` |
| 口述配不上要如实回执（图名不在跟踪清单 / 账本里没这条） | `map_memory_says_what_it_could_not_apply` |
| 手抄的口述表静态体检（图名是真图名、一条不重复） | `shipped_map_memory_names_a_real_zone` |
| 同名几条弱记录一条条都补（`pairs()` 顺序不许决定补哪条） | `map_memory_fills_every_row_sharing_a_name` |
| 忽略只管本 CD：同 CD 压着、跨 CD 解除后重新按准入判 | `an_ignore_only_last_until_the_next_cycle` |
| 门槛可配 + 四处同源 | `matrix_gold_threshold_is_configurable`、`options_gold_threshold_control_writes_config` |
| 本周交过即占行 | `turned_in_this_cycle_admits_row_without_money` |
| 金额链与"金额是谁答的"取证 | `getpool_zero_money_row_falls_through_to_reward_chain`、`quest_evidence_reports_who_answered_the_money` |

**替身铁律**：不许凭空造客户端没有的方法（第十四轮矩阵"打开一片漆黑"就是替身给了
`SetTooltipDescriptionFunc` 造成的假绿）；接线用例必须走真实事件入口（`fire(QUEST_COMPLETE)` →
`Sync.FinishWindow`），不许直调 `Ledger.OnTurnIn`；未实测的客户端方法在生产里走 `SafeCall`。

---

## 8. 还悬着的问题

- 板面子任务的 **questID 没有可靠来源**（族本体靠内置表，轮换出来的那些靠图清单反查名字）。
- `C_QuestRewards.*` / `HaveQuestRewardData` 在本机到底存不存在 —— 需要一份新的 `/wqt probe` 全文。
- ~~第十七轮新接的两条地图凭据本机存不存在~~ —— **已答（2026-10-24 回传）：两条全废**，见 §4。
  剩下的口子是别的版本的：`GetQuestLogZoneMapID` 在别的客户端有没有、悬赏清单哪天开始按图区分。
- "特别任务：加固符文石"这一族到底跟哪个内置族有关/无关，只有矩阵截图级的证据，没做过 ID 级取证。
- ~~底部"未知地图"那批行还缺口述地图~~ —— 用户 2026-10-24 报了 6 条，已进 `Api.MAP_MEMORY`
  （漫天烈火→盘卷蛇岛、追踪萨拉塔斯→银月城、斯托玛兰突袭战→虚影风暴、特别任务：击退圣光→哈籁恩达尔、
  特别任务：神盾密探→虚影风暴、特别任务：怨灵之怒→盘卷蛇岛）。**待验**：① 这 6 条的名字与账本里的
  完整标题逐字对得上吗（对不上日志会点名"账本里没有叫…"）；② 「银月城」「哈籁恩达尔」在跟踪清单里吗
  （不在则日志点名，改图名或让玩家站到那张图上"添加当前所在地图"）。
- **同名几条记录 = 口述表按名字配的运气问题**（第十八轮回传实测："特别任务：神盾密探 日志一句没抱怨，
  矩阵里那行照旧未知地图"）：`byTitle` 原先每个名字只留 `pairs()` 碰到的第一条，而账本里同名几条是常态
  （一族多 questID、轮换出的新旧两份都在）。现在**同名有几条弱记录就补几条**，有真凭据的那条照旧压不过
  （`map_memory_fills_every_row_sharing_a_name`）。
- **`ignored`（右键移除）= 永久屏蔽**（2026-10-27 定）。曾经做成"只管本 CD"，理由记在这里别再改回去：
  用户 2026-10-24 说过"世界任务的奖励每周都会变，本周是金币下周可能不是；如果下个 CD 它是金币任务，
  虽然忽略了也应该自动回来"。**那是误读**——他 2026-10-27 的原话是"我最开始 忽略 任务的意图 就是想把
  关键词匹配到了我不想要的任务…我要屏蔽掉，那么我这个意图其实是想永久屏蔽的"。所以：
  `Ledger.RemoveQuest` 写 `ignored = true`（**布尔，不是时间戳**——永久意图没有到期时刻，留个数字会被读成会过期），
  `Ledger.ClearCycleIgnores` 与 `Sync.SnapshotNow` 里那句"换了 CD：解除 N 条本周别显示"整段删除；
  换 CD、换轮、换版本都不许自己回来，`Prune` 也不按时清这类记录（金币账不能因为藏了一行凭空少一笔）。
  撤销只有一条路：矩阵"添加任务"打开的两栏「关键词 · 屏蔽管理」窗口里点那一行的**「取消忽略」**
  （`Ledger.AddQuest` 只清 `ignored`，不写任何上板凭据）。撤完之后这一行该不该列**完全回到常规准入**：
  在不在板上 / 金币门槛 / 关键词 / 日志收的 / 注册周常 / 本轮有人交过。
  **边界照实**：新窗口右栏只列**命中某个关键词**的任务；屏蔽过、名字又不含任何关键词的行在那里出现不到，
  只能靠改关键词或改存档找回（用户明确说他的屏蔽都来自关键词误命中，本轮按这个口径不加额外入口）。
  第十二轮"右键之后还会刷出来"那两条用例继续钉（`right_click_remove_survives_resync` +
  `removed_keyword_log_quest_does_not_come_back`）；2026-10-27 起**右键当场生效、不再弹确认框**
  （用户点名"取消右键屏蔽任务弹窗提醒"，`WQTRemoveQuestDialog` 整块下线），
  聊天回执里**写任务名不写 questID**（玩家读不到 ID，硬约束）。
  "下周不是金币就不该显示"这半句**不需要新代码**：换 CD 后它不在池里 → 老记录的到期时刻（=那个 CD 的
  周重置）已过 → `MatrixRows` 按 now 剔行；在池里但金额不够门槛 → 准入那道门挡掉。
- ~~两条蓝色"特别任务"为什么还在列~~ —— **已答**：`/wqt quest` 的"在列靠：玩家手动添加 + 命中关键词
  「特别任务」"。82157 巨兽崛起 / 86174 风暴在即，日志行 `isHidden=true、isOnMap=false`、本轮不在池里、
  当前角色日志"没接"（新 `isHidden` 过滤已经不再从这条路收它们）。**手动加的是玩家自己的意图，代码不替他
  改主意**（§6 第 1 条同源）—— 要他们走就右键那一行 → 确定（`L.RemoveQuest` 记 `ignored`，可再点回来）。
  真该改的是"加了之后每周还续命"：现在到期=下次周重置，周四自己消失。
- ESC 同时开着矩阵和设置面板时一次关掉几个 —— 本机没实测过。

相关过程记录：`docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`（§12–§15 是族/地图/门槛这几轮的完整经过）。
