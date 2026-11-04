# 停止重复要数（到期时刻 / 钱数 / 到点重画 / 按行按号）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 插件不再重复向客户端要它已经答过的数（结束时刻、钱数），到点只做重画，把「按号读数」退回成"只有真出现分岔的任务才按号"，并把"刷新"收进打开矩阵这一下（标题栏报最后一次抓池的时刻）。

**Architecture:** 数据源与消费侧各自收口：写侧（`Sync` 的池循环、事件现场问答、定向补读）只在"账本还没定下来"时问接口；读侧（`Ledger` 的几个读数函数）把"这一号该报多少钱"的口径收成唯一一处判据。界面（`Matrix`）只换问哪一个函数、删掉冗余的刷新按钮并在标题栏加一行纯文本，不新增判据、不新增定时器、不给 `Mx.Render` 加 upvalue。

**Tech Stack:** WoW 12.1.5 Lua 5.1 插件（`GoldQuestLedger/`），离线单测 `tests/run_gql_tests.py`（lupa + WoW 替身）。

## Global Constraints

- **零新增接口调用**：修法不许增加 WoW API 调用次数、不许引入轮询（`OnUpdate`、秒级重复定时器）、不许扫整棵地图树。每个任务动手前先自问"这个改动多花了什么"，并把代价写进注释与用例。
- **取证句不上界面**：解释"我们怎么知道的"那类句子一律进 `/gql probe` / `/gql quest`，界面只留玩家这一眼要用的（用户 10-27 定稿）。
- **不升 schema、不迁移**：本轮全部改动只用账本里已有的字段（`rec.roundEnd`、`rec.goldMoney/goldKey`、`chars[].reads`、`manual[].state`）。
- **Lua 5.1 upvalue 上限 60**：`Matrix.Render` 那条函数贴着上限（文件顶部说明），改 `Matrix` 只许替换既有调用，不许给 `Render` 增加新的 upvalue。静态守卫 `test_lua_upvalue_budget` 每次全量跑都会查。
- **标题栏时间戳不许多花一次接口**：删刷新按钮后，最后同步时刻只从账本里已有的 `db.chars[].lastSync` 算，放在 `Ensure` 建皮阶段；不许新增 `C_Timer` / `OnUpdate` / 秒级倒数，也不许把 `ForceMapDataLoad()` 并进 `Mx.Toggle()`（方案 B 不采纳）。
- **时钟域**：纪元秒（>1e9）与本次开机秒不可比，跨域一律判"算不出"，不许比大小（`Ledger` 各处 house rule）。
- **测试命令**：单任务用 `python tests/run_gql_tests.py -k <用例名子串>`，收尾必须回到**不带参数**的全量 `python tests/run_gql_tests.py`，末行要看到 `N/N 单测通过`（当前基线 **440/440**，2026-11-04 实测；本轮只增不减，用例改名不算减少）。除末行外还要看退出码：`tests/run_gql_tests.py:19749` 把 `syntax:` / `upvalues:` 的失败从分子里排除，静态守卫报红时末行仍会显示全绿；`--syntax` 是"全量单测 + 额外 loadfile"，不是只跑语法检查。
- **新增用例必须注册**：`-k` 是从文件末尾的 `tests` 名单里过滤（`tests/run_gql_tests.py:19737`）。新用例不注册 ⇒ `-k <新名>` 0 命中直接退出、全量跑被 `test_every_defined_case_is_registered_in_the_runner` 判 orphan 报红。每个 Task 的 Step 1 都要写明"把新函数追加进名单"，用例改名要同步改名单里的旧条目。
- **提交**：每个任务一次本地提交，逐行 review 后由主 agent 提交，不 push。

## 一、缺陷形状（先说清病在哪）

> **全文行号都是改前位置**（对着 2026-11-04 的工作树核的）。Task 1 起 `Sync.lua` 会在 :1200 附近插入代码，后续 Task 引用的行号一律按**符号名**定位，不要照数字硬找。

用户 11-04 的三问，逐条对代码：

| 用户的话 | 代码现状 | 病 |
|---|---|---|
| 「任务能进矩阵，到期时间就固定了，不需要再要了」 | `Sync.lua:1200-1204` 每次快照对池里每一条都问一遍倒计时，`1218` 交给 `L.SetExpiry` 写 `rec.roundEnd`（实际赋值 `Ledger.lua:1186`，resident 族在 `:1176` 早退不写） | 结束时刻本来就是"这条实例离开板面的那一刻"，它没过期 ⇒ 同一条实例 ⇒ 重问只会问出同一个时刻。每行每趟白烧 1~2 次接口调用，而且每次重写都是"上一轮的钱赖着不走"那类症状的入口 |
| （同上，另一族） | 常驻周常那一族（`regKey` 不是 `pin:` / `kwLogAdded` / 日志证明存在过）问出来的倒计时是板上轮换寿命，`Ledger.SetExpiry`（`Ledger.lua:1176-1178`）开头就把它丢掉 | 问了没人收，纯烧 |
| 「到期了就不展示了，安排要什么呢」 | `Sync.lua:122-129` 到期唤醒的回调跑的是 `SnapshotNow`（整板块重问） | "该行撤掉、颜色该变"是账本算式（`Ledger.EffectiveExpiry` 跟此刻一比就知道），不需要问游戏任何东西 |
| 「钱数可能不是金币（切了模式），别人是金币，可以再查，直到拿到金币数或用户右键表示不做为止」 | ①金额缺的那几条已有定向补读（`Sync.lua:256-293`，借 `QUEST_LOG_UPDATE`，每条每趟 1 次、每趟最多 12 条（`GOLD_READ_PER_PASS`）、封顶 6 趟）；②但 `Sync.lua:1336-1344` 还挂着一套**整板块**补读（5 秒 ×4，`SchedulePreloadRetry`），每趟都是几百张图逐个问；③补读只看次数，**不看这一号有没有右键标"不做"**（`manual[].state == "skip"`，`Matrix.lua:258-274` 写、`Ledger.lua:1695-1749` 读） | 贵的不是"再查"，是"整板块重查"；而玩家已经明说"这号不做"的那条还在被反复问 |
| 「刚开始是整行的，后来改成每个号单独，这是从一个极端走向另一个极端」 | 11-03 把三处消费侧全改成只认这一号自己的读数：`Ledger.LastCompletion:1109-1111`、`Ledger.WeekStats:1839-1845`、`Matrix.lua:544-553` | 分岔任务（12.1.5 实测 97128：Mad 读到 1090 金、别的号读到宝匣钥匙碎片）确实不许借；但**不分岔的任务**上，一个还没同步过的号也一律报 `?`、周汇总那笔钱不计 —— 代价落在了绝大多数正常情况上 |

## 二、口径（定稿，实施不许自创）

1. **结束时刻没过期就不重问倒计时（三处读取点统一）**。跳过问的条件（纯账本比较，零接口）：账本里有 `rec.roundEnd`、与此刻同域、`roundEnd > now`、且 `roundEnd - now <= 691200`（一个 CD 604800 + 一天缓冲）。最后一档是脏数据门：时刻被写坏（写到几周之后）时**必须**重问，否则那条永远钉着不走。
   - `Api.QuestTimeLeft` 在 `Sync.lua` 里有**三处**读取：`CaptureQuestData`（`:387`，每次接任务/日志事件都问，`SetExpiry` 在 `:398`）、日志旁路 `SyncKeywordQuestsFromLog`（`:544`，`:551` 还绕过 `SetExpiry` 直写 `rec.roundEnd = now + secs`）、池循环（`:1202`）。**只改池循环等于口径没落地**：另两路仍每次重问、仍能改写结束时刻，而只调 `SnapshotNow` 的用例会给假绿。
   - 定稿：新增文件内共用判据 `local function ShouldAskExpiry(db, questID, rec, now)`（纯账本，返回"该不该问"），把这三处与第 2 条的 resident 族判据收进同一个函数，三处读取点一律先问它；`:551` 的直写**保留**（它有自己的 `<1 天 / 周重置回声` 闸门，换成 `L.SetExpiry` 会改掉常驻周常语义），但必须同受这道门管，注释写清两道门分工。用例覆盖：池循环两条（活着不问 / 脏数据重问）+ 一条走 `QUEST_ACCEPTED` 的现场问答；日志旁路那一路靠既有的常驻周常族回归用例守住，不另起一套替身。
   - 代价照实：`L.KeywordLogAdmits` 会问 `Config.KeywordMatch`（`Ledger.lua:2112`），不是纯字段比较；放进共用函数后每行每趟最多多一次字符串匹配，注释里要写明，不许把它算成零代价。
2. **没人收的答案就不问**。常驻周常那一族（判据与 `Ledger.SetExpiry:1176-1178` 开头同源）整族跳过倒计时那一问。`SetExpiry` 只有一个写点（`Ledger.lua:1186`），开头是纯 `return`、不清理字段，所以整族跳过对显示/过期零影响。
   代价照实：①这一族不再进快照收尾的取证 `金额读到了、倒计时读不出`（`Sync.lua:1330-1334`）；②**第 1 条 `endLive` 跳过的那一大族也不再进 `Sync.lua:1220` 的 `elseif`**，`noTimeGoldMiss` 只剩"新上板 / 脏数据 / 已过期却真问不出"那几条。`/gql probe` 若仍要这条取证，得另找落点或明确接受退化，不许只写"这一族不统计"就当没事。
3. **到点只重画**。唤醒定时器到点后**保留现有的 `GQL.Matrix.Refresh()`**（0.25 秒合并窗口、判 `frame:IsShown()`），只删 `pcall(Sync.SnapshotNow)` 与 `快照异常` 日志两句，再照旧 `ScheduleNextRefresh()`。**不换成 `Matrix.RefreshNow()`**：`Matrix.lua:1899-1906` 那个函数不判面板显隐，换掉就悄悄改了渲染语义，也不在本轮要修的范围内（Sync 里现有 8 处都走 `Refresh()`）。单实例、重排先取消旧的、关面板取消 —— 这四条现有语义一个字不动。6 小时封顶保留（`MAX_WAKE_SECONDS`），但它现在只压"多久重画一次"，不再压接口成本。
   代价照实：到点只重画 ⇒ 旧行会撤、颜色会变，但**到期换板后新任务不会自动出现**，要等玩家关开一次矩阵（`Mx.Toggle():1932-1946` 每次打开都同步抓一次池）；`db.chars[].lastSync` 也停在面板打开那一刻，Task 6 的标题栏"最后同步"会如实停住，不给"数据一直很新"的错觉。与第 4 条的代价合并成一句：**本轮把"自动补数"换成"打开矩阵时抓一次、想再抓就关开一次"**。
4. **整板块补读下线**。`SchedulePreloadRetry` / `CancelPreloadRetry` / `preloadRetries` / `PRELOAD_RETRY_SECONDS` / `PRELOAD_MAX_RETRIES` 整套删除，`pendingMoney` / `pendingTime` 两个计数一并删除（它们只服务那套补读）。
   缺的钱走定向补读（第 5 条），缺的结束时刻走下一次事件快照（做任务/接任务时 `QUEST_LOG_UPDATE` 本来就成串触发，那几条 `live` 不成立 ⇒ 自然重问）。
   代价照实：挂机不做事、也没有任何事件时，缺结束时刻的行会一直显示"金额/剩余未知"，直到玩家关开一次矩阵（**刷新按钮按 Task 6 下线**，别再把它写成兜底出口）。这是把"每 5 秒重问整板块几百张图"换掉的必然结果，也是用户 10-27 定的"不许轮询"。
   前提写清（用户 11-04 的质疑）：**按 M 与奖励金额是两条链路** —— 地图 `OnShow` 拉的是板面任务列表，奖励金额走 `C_TaskQuest.RequestPreloadRewardData` 自己异步回来（11-04 两次取证定案，本机没有"奖励数据到了"事件、`C_QuestRewards.FetchData` 也不存在）。所以"先按 M 再开矩阵"只保证板面任务一致，不保证金额列到位；**按 M 本身不构成刷新**，按钮下线后 `ForceMapDataLoad()` 仍留在 `Sync.lua:1114-1118` 的 `emptyMaps` 自动路径里，功能没丢。
   删除清单补两处注释：`Sync.lua:1155`、`:1199` 里含 `pendingTime` 字样，不清掉 Task 3 的 `grep` 不会无输出；`grep` 只作辅助，删净的证据是全量绿 + 那三个符号全仓零引用。
5. **右键"不做"= 这一号本轮不再替这条出数，补读当场了结**。四条硬约束：
   - **有效性按实例编号判**：`skip` 必须走 `L.State(db, charKey, questID, nil, now, curKey) == "skip"`，或自行比 `L.SameInstanceKey(m.k, curKey)`。raw `manual[].state` 从不自动作废，作废只发生在 `L.State:1732` **读时**（`SetManual` 写入后 `state` 永久留在存档里）；裸读会把上一轮的 × 当本轮有效，永久掐掉新实例的补读。`L.State` 还会把"标了不做又真做了"判成 `done`，与界面口径一致。
   - **了结按号**：`askedRewardData` 现有键只有 questID（`Sync.lua:214/:223`），`= -1` 会让一个号的放弃替全账号关掉补读。定稿把值改成按号子表 `questID -> { [charKey] = tries }`（`-1` 只对本号生效），读写点 `:258/:282/:284/:288/:414/:431` 同步改；`CaptureQuestData` 里"早就问过服务器了"也按当前号判。语义随之变成"每个号每次开机各发一次预加载请求"。
   - **停的条件照实**：问到正数（`Sync.lua:282`）/ `tries + 1 >= GOLD_READ_MAX_TRIES = 6`（`:284`）/ 这一号本轮有效 skip。`hasData == true`（`:417-423`）是**根本不进补读表**，不是"停"；`GOLD_READ_PER_PASS = 12`（`:226`）是每趟上限；这张表是会话级、无下板/过期清理，**不许**把"这条下板"写成停的条件。次数封顶保留：挂机期间每次日志更新白问一条永远问不出的（11-04 实测 89267）就是白烧接口调用。
   - **取证文本别写进死代码**：`Sync.lua:413` 的外层 `if` 会因 skip 整块不执行，"这一号标了不做，没去要这份数据"那句必须放在该 `if` **外面**（例如先在块外算 `askedText` 初值），放分支链最前面等于永远不可达。
   - **用例四族**：本号 skip 停问 / 别的号 skip 不挡本号 / **本号 skip 后换号登录该条仍能补读** / 上一轮烙的 skip（`m.k` 对不上当前实例）不挡本轮。`sync_env` 给了 `wall=1_790_000_000` 时 `SetManual` 的 `t` 也必须用纪元秒，否则 `m.k` 跨域烙不上、用例测的是坏实现。
6. **读数默认按行，这条上真出现分岔才按号**。新增唯一口径函数 `Ledger.CellRewardMoney`：
   - 这一号在这条实例上有读数（正数或 0）⇒ 用它，跟现在一样；
   - 没有读数、且**没判出分岔** ⇒ 借行级那份（`rec.goldMoney`，且 `rec.goldKey` 与传入的实例编号同一条）；
   - 没有读数、但分岔成立 ⇒ `nil`（界面报"金额未知"，周汇总不计这笔），因为这时候借谁的钱都是猜。
   三处消费侧（`LastCompletion` / `WeekStats` 实测分支 / `Matrix` 格子悬停）全部换成问这一个函数，不许在界面里再拼第二套判据。
   **分岔判据收紧**：`L.RewardDiffers` 不许见 `g == 0` 就判。判据 = 同一实例编号上**至少两个不同 charKey，一个正数、一个 0**。`Config.lua:30-31` 只是数据结构约定；`Api.lua:187-189` 会在"数据在本地却读不出金额"时造出推断 0，`Matrix.lua:573-575` 也承认 0 可能是做完前的旧答案；写侧"正数不被 0 冲掉"的真身在 `Ledger.lua:1400-1403`（不是早先写的 `Config.lua:32-35`，那 4 行是注释），且只在"先正数后 0"生效。一个推断 0 就把所有没读过的号打回 `?`，恰好回到本轮要消灭的症状。
   **编号显式传**：`CellRewardMoney` 的 `curKey` 优先用调用方传入的，三处消费侧各自把该场景的实例编号显式传进来（流水用 `r.k`、悬停现算当前编号）；缺省时才按传入的 `now` 现算，**不许偷偷用 `LedgerNow(db)`**（那正是"同一格两个答案、还查不出是哪一步算的"的来路）。
   代价照实：分岔任务上那个"其实给金币、只是还没同步过"的号仍然报 `?`。要消掉它只能允许借数，一允许就回到用户 11-03 截图那个错。
7. **打开即刷新，刷新按钮下线，标题栏报"最后一次抓池"的时刻**。`Mx.Toggle()` 打开时已经有 `SetPolling(true)` → 同步 `SnapshotNow()` → `Mx.RefreshNow()` → `f:Show()`（`Matrix.lua:1932-1947`），所以"打开就抓一次"是现状，**但"打开就看到正确数据"不是**，11-04 调试定案的两层原因：
   - **抓到了但没重画**：`:1946` 的首绘排在 `:1947 Show()` 之前，这一趟里 `SnapshotNow` 触发的 `GQL.Matrix.Refresh()` 全被 `Matrix.lua:1909` 的 `frame:IsShown()` 门丢掉；`Sync.lua:1336-1345` 的快照末尾自己也不重画。Task 6 要把 `f:Show()` 提到抓池之前，补上这一半（同步、零接口、不新增定时器）。
   - **数据本身还没回来**：奖励金额 1~24 秒后才异步回本地（`docs/superpowers/plans/2026-11-04-gql-direct-reward-read-evidence.md:14-17`、`:180-183`、`:206-209`），本机没有"数据到达"事件（同文档 `:176-179`），补全只能靠 10 秒去抖（`Sync.lua:1356-1379`）带一次 `Mx.Refresh()`。**这一半不是插件能消除的**，别把 Task 6 的目标写成"打开就看到全部正确数据"；标题栏时间戳如实标出每一趟抓池时刻，正是给这个异步窗口一个可见凭据。
   按钮只是"打开那一串动作"的手动副本，而且点它时池签名不变 ⇒ diff 为空 ⇒ 重画逐字相同（`Sync.lua:1317`、用户 11-04 实测），删掉它。标题栏改成一个纯文本的最后同步时刻，报**当前角色** `db.chars[charKey].lastSync`（`Sync.lua:1081`，由 `SnapshotNow` 写）。**不报 `Matrix.lastPaint`**：改后到点只重画，重画时刻会一直前进，会给出虚假的新鲜感。
   - 时钟域：`lastSync = Clock()`，可能是纪元秒（`>1e9`）也可能是开机秒（`Api.lua:1409-1426`）。纪元域 → `最后同步 HH:MM:SS`（走 `date`，离线替身里 stub 一个）；开机秒域 → 只在抓池/打开时算一次 `Api.Uptime() - lastSync`，显示 `最后同步 N 分前`，跨域算不出就 `—`；没有 `lastSync` → `最后同步 —`。不新增定时器、不做秒级倒数。
   - 文本放 `Ensure` 建皮阶段（`Matrix.lua:1419-1424` 标题栏版本文字右侧、设置按钮左边空出来的位置），`Mx.Render` 一行不动（upvalue 60 上限）。刷新文本用 `pcall` 包住，不许让整趟抓池失败。
   - 随按钮一起下线的还有 `Matrix.lua:1503-1505` 那段"等效按 M 强制加载"的悬停说明；功能没丢（`Sync.lua:1114-1118`），只是不再有界面文案。玩家唯一的手动刷新入口变成"关开一次矩阵"。
   - 用例：`test_matrix_refresh_button_sits_by_settings_and_resyncs`（`tests/run_gql_tests.py:9738`）**改名重写、不许删**；另补一条三种时钟域的形状用例。

## 三、老数据

不升 schema、不迁移。所有改动读的都是老存档里已有的字段：

- 老档里 `roundEnd` 已过期或没有 ⇒ 第 1/2 条的跳过条件不成立 ⇒ 照旧重问，行为与现在一致。
- 老档里 `reads` 缺失的号 ⇒ 第 6 条改完**有可能**从 `?` 变成借行级数（前提：这条上没判出分岔，且 `rec.goldKey` 对得上、`rec.goldMoney > 0`）。这是把"宁缺不编"放宽到"有行级凭据就报行级"，不是丢数据，但**别写成"一定生效"**：`Ledger.lua:2323` 的迁移会清 `goldMoney`（不碰 `goldKey`），老档里 `goldMoney` 为 nil 时仍报 `?`。本轮要补一条老档形状用例兜底。
- 老档里 `manual[].state == "skip"` 已经写下的那几笔 ⇒ 第 5 条**按实例编号判有效性**：`m.k` 对不上当前实例的一律当无效（宁可多问一次，不许把陈旧 × 当本轮凭据）；老档没烙 `k` 的同样按无效处理，不需要补烙字段、不迁移。

---

### Task 1: 结束时刻没过期就不重问倒计时

**Files:**
- Modify: `GoldQuestLedger/Sync.lua`（按符号定位）：`CaptureQuestData` 的倒计时那一问（:387）与 `L.SetExpiry`（:398）、日志旁路 `SyncKeywordQuestsFromLog` 的那一问（:544）与直写（:551）、`Sync.SnapshotNow` 池循环（:1200-1230）
- Test: `tests/run_gql_tests.py`（新增 3 条用例，**全部注册进文件末尾 `tests` 名单**）

**Interfaces:**
- Consumes: `L.SetExpiry(rec, now, remaining, resident)`、`L.IsPinRegKey(regKey)`、`L.KeywordLogAdmits(db, questID, rec, now)`、`Api.QuestTimeLeft(questID)` —— 全部现有，签名不动。
- Produces: 文件内 `local function ShouldAskExpiry(db, questID, rec, now) -> boolean`（唯一判据，三处读取点共用；不挂到 `Sync` 表）；本任务只减少 `Api.QuestTimeLeft` 的调用次数，不新增接口调用。后续任务不依赖它。

- [ ] **Step 1: 写失败用例 —— 活着的结束时刻不再被重问**

追加到 `tests/run_gql_tests.py` 里 `test_snapshot_records_the_instance_end_time`（:7268）之后：

```python
def test_a_live_expiry_is_never_asked_again():
    """结束时刻还在未来 = 板上还是同一条实例 ⇒ 倒计时一次都不许多问
    （用户 11-04："任务能进矩阵，那么到期时间就固定了，不需要再要了"）。
    替身按 GQL_NOW 递减（board_clock 的原语义），所以"又问了一次"在这套环境里
    看得见：问的次数 = C_TaskQuest.GetQuestTimeLeftSeconds 被调了几回。"""
    lua, gql, _ = sync_env([(161, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    asked = []
    real = g.C_TaskQuest.GetQuestTimeLeftSeconds
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), real(qid))[1]})
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    gql.Sync.SnapshotNow()                     # 第一次上板：账本里还没有结束时刻，要问
    if asked != [161]:
        return fail("a_live_expiry_is_never_asked_again",
                    f"第一次上板该问一次倒计时，实际问了 {asked}")
    asked.clear()
    g.GQL_NOW = 2000
    gql.Sync.SnapshotNow()                     # 结束时刻还在未来：一个字都不该再问
    gql.Sync.SnapshotNow()
    if asked:
        return fail("a_live_expiry_is_never_asked_again",
                    f"结束时刻没过期还重问倒计时（问了 {asked}）")
    # sync_env(board=43200) 在 GQL_NOW=0 时把结束时刻钉成 43200，倒计时答 43200-GQL_NOW；
    # 于是 now=1000 时 SetExpiry 写 1000+42200 = 43200，不是 1000+43200（原来的 44200 是错的）。
    if int(lua.execute("return math.floor(GQLDB.quests[161].roundEnd)")) != 43200:
        return fail("a_live_expiry_is_never_asked_again",
                    "跳过问不许把账本里那条结束时刻改掉（应为 43200）")
    ok("a_live_expiry_is_never_asked_again")


def test_a_suspect_expiry_is_asked_again():
    """脏数据那一档：结束时刻被写到比 now 远过一个 CD + 一天 ⇒ 判它不可信，重问。
    不重问就等于那条永远钉在矩阵里不走（用户 10-27 之后"每行都要有一条会过去的时刻"）。"""
    lua, gql, _ = sync_env([(162, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    asked = []
    real = g.C_TaskQuest.GetQuestTimeLeftSeconds
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), real(qid))[1]})
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    gql.Sync.SnapshotNow()
    asked.clear()
    lua.execute("GQLDB.quests[162].roundEnd = 1000 + 700000")   # 比 now 远 70 万秒 > 691200
    gql.Sync.SnapshotNow()
    if asked != [162]:
        return fail("a_suspect_expiry_is_asked_again",
                    f"时刻被写坏时该重问一次，实际问了 {asked}")
    ok("a_suspect_expiry_is_asked_again")


def test_a_live_expiry_is_not_asked_by_event_capture():
    """第三处读取点：现场问答（Sync.lua:387 每次 QUEST_ACCEPTED 都问、:398 还会改写 roundEnd）。
    口径不许只落在池循环上 —— 只调 SnapshotNow 的用例测不到这一路。"""
    lua, gql, _ = sync_env([(163, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    gql.Sync.SnapshotNow()                      # 第一次上板：问一次、把 roundEnd 烙成 43200
    asked = []
    real = g.C_TaskQuest.GetQuestTimeLeftSeconds
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), real(qid))[1]})
    for step in range(2):
        g.GQL_NOW = 1100 + 100 * step           # 过 LogBypass 的 10 秒节流
        fire(gql, "QUEST_ACCEPTED", 163)
    if asked:
        return fail("a_live_expiry_is_not_asked_by_event_capture",
                    f"结束时刻还活着，现场问答却重问了倒计时：{asked}")
    if int(lua.execute("return math.floor(GQLDB.quests[163].roundEnd)")) != 43200:
        return fail("a_live_expiry_is_not_asked_by_event_capture",
                    "现场问答不许改写同一条实例的结束时刻")
    ok("a_live_expiry_is_not_asked_by_event_capture")
```

把三条新用例**注册进文件末尾的 `tests` 名单**（不注册 ⇒ `-k` 0 命中直接退出、全量跑判 orphan）。

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k a_live_expiry_is_never_asked_again,a_suspect_expiry_is_asked_again,a_live_expiry_is_not_asked_by_event_capture`
Expected: 第一/第三条 `FAIL ...: ...还重问倒计时`；第二条 PASS（现在每次都问，本来就命中）。

- [ ] **Step 3: 加共用判据，三处读取点一起接上**

`Sync.lua` 里 `Clock()` 之后加一个文件内 `local` 函数（三处调用点都在它之后；纯账本、零接口）：

```lua
--- 【11-04 定稿 §二 1/2】这一问只在"账本还没定下来"时问。
--- roundEnd 就是这条实例离开板面的那一刻：还在未来 ⇒ 板上还是同一条 ⇒ 再问只会问出
--- 同一个时刻。三处读取点（CaptureQuestData / 日志旁路 / 池循环）共用这一处判据。
--- 代价：L.KeywordLogAdmits 会问 Config.KeywordMatch（字符串匹配，随玩家配置变化），
--- 每行每趟最多多一次纯 Lua 匹配；不是接口调用，但也不是零代价。
local function ShouldAskExpiry(db, questID, rec, now)
    local knownEnd = rec and tonumber(rec.roundEnd)
    if knownEnd and (knownEnd > 1e9) == (now > 1e9)
        and knownEnd > now and (knownEnd - now) <= 691200 then
        return false                         -- 同一条实例，答案已经答过了
    end
    if rec and ((rec.regKey and not L.IsPinRegKey(rec.regKey)) or rec.kwLogAdded
        or L.KeywordLogAdmits(db, questID, rec, now)) then
        return false                         -- 常驻周常：SetExpiry 开头就丢，没人收的答案不问
    end
    return true                              -- 跨域/没有/已过期/写坏了：照旧问（往多问那边走）
end
```

三个调用点：

1. `CaptureQuestData`（:380-399）：把 `local now = Clock()` 提到倒计时那一问之前，然后把

```lua
    local okT, v = pcall(GQL.Api.QuestTimeLeft, questID)
    if okT then secs = v else timeErr = tostring(v) end
```
改为
```lua
    if ShouldAskExpiry(db, questID, rec, now) then
        local okT, v = pcall(GQL.Api.QuestTimeLeft, questID)
        if okT then secs = v else timeErr = tostring(v) end
    end
```
（`secs` 为 nil 时下面 `if rec and type(secs) == "number" and secs > 0` 自然不写，现场取证日志照旧打 `倒计时=nil`。）

2. 日志旁路 `SyncKeywordQuestsFromLog`（:543）：条件改成 `if not inPool[q.questID] and ShouldAskExpiry(db, q.questID, rec, now) then`。`:551` 的 `rec.roundEnd = now + secs` **保留直写**：它外面那道 `<1 天 / 周重置回声` 闸门（`isRotation`）与 `SetExpiry` 的 resident 早退不是同一件事，换成 `SetExpiry` 会顺手改掉常驻周常的语义；只需在注释里写明"两道门分工：一道管这一轮要不要问，一道管问出来的数配不配写"。

3. 池循环（:1200-1205）：把

```lua
        local secs, timeFailed = nil, false
        if GQL.Api.QuestTimeLeft then
```
改为
```lua
        local asked = ShouldAskExpiry(db, questID, rec, now)
        local secs, timeFailed = nil, false
        if GQL.Api.QuestTimeLeft and asked then
```
并把紧随其后的分支头从 `if type(secs) == "number" and secs > 0 then` 改为

```lua
        if not asked then
            -- 没问 ⇒ SetExpiry / pendingTime / noTimeGoldMiss 一个字都不写
            -- （那三处只属于"真问了"的分支；pendingTime 的删除见 Task 3；取证退化见 §二 2）
        elseif type(secs) == "number" and secs > 0 then
```

- [ ] **Step 4: 跑到绿（含既有到期/身份那几族）**

Run: `python tests/run_gql_tests.py -k expiry,timeleft,countdown,instance_end,refresh,retry`
Expected: 全 PASS。若 `test_snapshot_records_the_instance_end_time`、`test_countdown_falls_back_to_ledger_expiry`、`test_a_flaky_interface_never_overwrites_or_invents_expiry` 变红，是跳过条件写宽了（把"该问"的形状也跳过了），回到 Step 3 收窄，不许改用例迁就实现。

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Sync.lua tests/run_gql_tests.py
git commit -m "fix: 11-04 结束时刻没过期就不再重问倒计时（含没人收的常驻周常那一族）"
```

---

### Task 2: 到点只重画，不重问整板块

**Files:**
- Modify: `GoldQuestLedger/Sync.lua:112-130`（`ScheduleNextRefresh` 的回调）
- Test: `tests/run_gql_tests.py:7316-7347`（`test_refresh_wakes_at_earliest_expiry` 的 docstring、替身计数、尾部断言改写）；本任务**不新增用例、不改名**，名单不用动

**Interfaces:**
- Consumes: `GQL.Matrix.Refresh()`（现有，`Matrix.lua:1908-1922`，0.25 秒合并窗口 + 判 `IsShown()`）、`L.NextRefreshAt(db, now)`（现有，`Ledger.lua:3062`）。
- Produces: 无新接口。唤醒回调的副作用从"抓一次快照 + 重画"变成"只重画 + 重排下一颗"，**重画函数保持不变**。

- [ ] **Step 1: 改既有用例（不新增用例）**

`tests/run_gql_tests.py:7316` 的 docstring 首行：把

```python
    """矩阵开着时按最早到期时刻排一个唤醒定时器：到点自动抓一次并重画。
```

改为

```python
    """矩阵开着时按最早到期时刻排一个唤醒定时器：到点只重画，不重问整板块。
    "该行撤掉"是账本算式（结束时刻跟此刻一比就知道），不需要向游戏要任何数
    （用户 11-04："到期了就不展示了，安排要什么呢"）。
```

在函数开头 `gql.Matrix.Toggle()` **之前**包装 `GQL.Matrix.Refresh`（`Mx == GQL.Matrix`，见 `Matrix.lua:19-20`；包装的是回调要走的那一个函数，不是 `RefreshNow`）：

```python
    lua.execute("GQL_REPAINTED = 0 "
                "local real = GQL.Matrix.Refresh "
                "GQL.Matrix.Refresh = function() GQL_REPAINTED = GQL_REPAINTED + 1 "
                "return real() end")
```

在 `g.GQL_NOW = 9000`（:7341，定时器触发）**之前**插入两行基线：

```python
    snap_before = str(gql.Config.GetLogs()).count("快照完成")
    lua.execute("GQL_REPAINTED = 0")   # Toggle 与 :7332-7333 的 5 次快照都会走到 Refresh，先清零
```

并把末尾那两句断言（:7345-7346）

```python
    if int(g.GQLDB["chars"][key]["lastSync"]) != 9000:
        return fail("refresh_wakes_at_earliest_expiry", "到点应自动抓一次快照")
```

替换为

```python
    logs = str(gql.Config.GetLogs())
    if logs.count("快照完成") > snap_before:
        return fail("refresh_wakes_at_earliest_expiry",
                    f"到点不该重问整板块（每趟抓池要问几百张图）:\n{logs}")
    if int(g.GQLDB["chars"][key]["lastSync"]) == 9000:
        return fail("refresh_wakes_at_earliest_expiry", "到点不该写 lastSync（那是抓池的痕迹）")
    if int(lua.execute("return GQL_REPAINTED or 0")) < 1:
        return fail("refresh_wakes_at_earliest_expiry", "到点该喊一次重画")
```

两个"必然失效"的形状已避开：①这条用例自己就跑了 6 次快照，判 `> 1` 会永远假红 ⇒ 改成触发前后增量；②开面板那一次与前面 5 次快照都可能经 `Sync` 的 8 处 `GQL.Matrix.Refresh()` 走到被包装的函数（`Matrix.Toggle()` 自己还会调 `Mx.RefreshNow()`，见 `Matrix.lua:1946`），不清零则重画计数永远 ≥1 ⇒ 触发前清零。

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k refresh_wakes_at_earliest_expiry`
Expected: `FAIL refresh_wakes_at_earliest_expiry: 到点不该重问整板块...`（改前到点跑一趟 `SnapshotNow`，快照数比 `snap_before` 多一条）；`lastSync` 那条也会红，两条都指向同一件事。

- [ ] **Step 3: 改回调**

`GoldQuestLedger/Sync.lua:122-129` 的定时器回调体替换为：

```lua
    refreshTimer = C_Timer.NewTimer(math.max(5, delay), function()
        refreshTimer = nil
        if not polling then return end
        -- 【11-04 定稿 §二 3：到点只重画】结束时刻在账本里，到没到一比就知道；
        -- 那一行该撤、颜色该变，全是账本算式。原来这里跑一趟 SnapshotNow =
        -- 几百张图逐个问一遍（用户点名的那笔白烧）。
        -- 重排下一颗照旧：ScheduleNextRefresh 第一句就取消旧的，同一时刻只留一个。
        ScheduleNextRefresh()
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
    end)
```

（只删 `pcall(Sync.SnapshotNow)` 与 `GQL.Config.Log("快照异常: ...")` 两句；`GQL.Matrix.Refresh()` 原样保留，**不要**换成 `RefreshNow()` —— 那会绕过合并窗口与 `IsShown()` 判断。）

- [ ] **Step 4: 跑到绿**

Run: `python tests/run_gql_tests.py -k refresh,closing_matrix,expiry,wake`
Expected: 全 PASS。`test_refresh_wake_is_capped`（:17218）与 `test_closing_matrix_cancels_pending_refresh`（:7350）不许改：6 小时封顶与关面板取消这两条语义不动。

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Sync.lua tests/run_gql_tests.py
git commit -m "fix: 11-04 到期唤醒只重画，不再重问整板块"
```

---

### Task 3: 整板块补读（5 秒 ×4）整套下线

**Files:**
- Modify: `GoldQuestLedger/Sync.lua:51-65`（`SetPolling` 里的 `CancelPreloadRetry`）、`:132-160`（`PRELOAD_*` 与两个函数）、`:1154-1180`+`:1221`+`:1336-1344`（`pendingMoney` / `pendingTime` 两个计数与触发点）
- Test: `tests/run_gql_tests.py:8102-8131`（`test_preload_retry_fills_money_without_reopening` 改写）、`:17171-17190`（`test_missing_countdown_also_schedules_retry` 改写）

**Interfaces:**
- Consumes: Task 1 之后的池循环（`pendingTime` 已只对"真问了却问不出"的那几条计数）。
- Produces: 无新接口。`Sync` 内部不再有 `SchedulePreloadRetry` / `CancelPreloadRetry` / `preloadRetries`（全仓无外部调用方，实施时用 `grep -rn "PreloadRetry\|preloadRetries\|pendingMoney\|pendingTime" GoldQuestLedger tests` 确认删净）。

- [ ] **Step 1: 把两条既有用例改成钉"不再有整板块重问"，并同步改名单**

`tests` 名单里有两个旧条目（`tests/run_gql_tests.py:19436`、`:19657`）。这两条用例是**改名**，必须把名单里对应两行一并换成新函数名，否则 `tests = [...]` 在模块加载时 NameError，整个 runner 一条都跑不了（`-k` 过滤用的是名单，未定义的名字连过滤都到不了）。

`test_preload_retry_fills_money_without_reopening`（:8102）整条替换为：

```python
def test_missing_money_never_reschedules_a_full_pool_pass():
    """金额读不全时，开着矩阵不再每 5 秒重问整板块（几百张图逐个问 = 用户实测的卡顿）。
    缺的那几条走定向补读（Sync.SettleAskedRewardGold，借 QUEST_LOG_UPDATE，每条每趟 1 次），
    见 test_reward_data_arrival_settles_gold_without_the_matrix。
    这里钉的是反面：面板开着、金额一直读不出，待触发定时器只该有"到期重画"那一颗，
    而且推进 6 个 5 秒不会多抓一次池。"""
    lua, gql, _ = sync_env([(97, None)], extra=("Matrix",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 0      # 数据一直没到
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()                           # 打开 = 抓一次，金额读空
    before = str(gql.Config.GetLogs()).count("快照完成")
    for step in range(6):
        g.GQL_NOW = 1000 + 5 * (step + 1)
        lua.globals().GQL_FireTimers()
    logs = str(gql.Config.GetLogs())
    if str(logs).count("快照完成") != before:
        return fail("missing_money_never_reschedules_a_full_pool_pass",
                    f"金额读空不该再排整板块补读:\n{logs}")
    pending = lua.execute("local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
                          "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) > 1:
        return fail("missing_money_never_reschedules_a_full_pool_pass",
                    f"待触发定时器只留到期重画那一颗，实际 {pending} 个")
    ok("missing_money_never_reschedules_a_full_pool_pass")
```

`test_missing_countdown_also_schedules_retry`（:17171）整条替换为：

```python
def test_missing_countdown_is_reasked_on_the_next_event_snapshot():
    """倒计时读不出（接口答 nil）的那几条：不排整板块补读，但下一次事件快照会重问
    —— 账本里没有结束时刻 ⇒ Task 1 那道"活着就不问"不成立 ⇒ 该问的照问（用户实测过
    "有个任务没有倒计时了"，那条不能因为下线补读而永久没剩余）。"""
    lua, gql, _ = sync_env([(143, 30000000)], extra=("Matrix",))
    g = lua.globals()
    asked = []
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), None)[1]})
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()
    if asked != [143]:
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    f"第一趟该问一次倒计时，实际 {asked}")
    asked.clear()
    gql.Sync.SnapshotNow()                        # 下一次事件快照（去抖后走的就是这条）
    if asked != [143]:
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    f"结束时刻一直没定下来，下一趟要接着问，实际 {asked}")
    logs = str(gql.Config.GetLogs())
    for step in range(6):
        g.GQL_NOW = 1000 + 5 * (step + 1)
        lua.globals().GQL_FireTimers()
    if str(gql.Config.GetLogs()).count("快照完成") > logs.count("快照完成"):
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    "倒计时读不出不该再排整板块补读")
    ok("missing_countdown_is_reasked_on_the_next_event_snapshot")
```

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k missing_money_never_reschedules,missing_countdown_is_reasked`
Expected: 第一条 `FAIL ... 金额读空不该再排整板块补读`；第二条 `FAIL ... 倒计时读不出不该再排整板块补读`。

- [ ] **Step 3: 删掉那套补读**

`GoldQuestLedger/Sync.lua` 四处：

1. :51-55 的前向声明段删掉这两行（`CancelRefreshTimer` 那行保留）：

```lua
-- 同理：关轮询时也要取消奖励预加载的补读定时器
local CancelPreloadRetry
```

2. :59-65 的 `Sync.SetPolling` 体改为：

```lua
function Sync.SetPolling(enabled)
    polling = not not enabled
    if not polling then
        if CancelRefreshTimer then CancelRefreshTimer() end
    end
end
```

3. :132-160 整段删除（`PRELOAD_RETRY_SECONDS` / `PRELOAD_MAX_RETRIES` / `preloadRetries` / `preloadTimer` / `CancelPreloadRetry` / `SchedulePreloadRetry` 连同上面那段说明注释一起删；注释里"服务器数据要几十秒才回来""第一次打开矩阵只显示一部分"那两条事实归 Task 4 之后仍在的定向补读注释，别在删除时把语义丢掉——定向补读那段在 :246-255 已经写全）。
4. 池循环的计数与触发：删 :1154 的 `local pendingMoney, pendingTime = 0, 0`、:1180 的 `if type(pool[i].money) ~= "number" then pendingMoney = pendingMoney + 1 end`、:1221 的 `pendingTime = pendingTime + 1`（该分支剩下的取证计数 `noTimeGoldMiss` 一字不动）；**顺手清掉 :1155、:1199 两条注释里的 `pendingTime` 字样**（Task 1 新写的那句注释也别再提这个计数），否则 Step 4 的 `grep` 不会无输出。并把 :1336-1344 整段替换为：

```lua
    ScheduleNextRefresh()
    -- 11-04 定稿 §二 4：这里原来还挂着一套"金额/倒计时没读到就每 5 秒重问整板块、封顶 4 次"。
    -- 整板块重问 = 几百张图逐个问，是用户实测卡顿的同一个来源；缺的钱走上面的定向补读
    -- （SettleAskedRewardGold），缺的结束时刻走下一次事件快照（Task 1 那道门不成立 ⇒ 照问）。
```

- [ ] **Step 4: 跑到绿 + 确认删净**

Run: `python tests/run_gql_tests.py -k missing_money,missing_countdown,reward_data_arrival,event_snapshot_repaints`
Expected: 全 PASS（`test_reward_data_arrival_settles_gold_without_the_matrix` 是定向补读那条正主，必须照旧绿）。
Run: `grep -rn "PreloadRetry\|preloadRetries\|pendingMoney\|pendingTime\|PRELOAD_" GoldQuestLedger tests/run_gql_tests.py`
Expected: 无输出（有输出就是没删净；`Api.lua` 里 `RequestPreloadRewardData` 那一路不属于本任务，别误删）。注意测试文件侧本来就是 0 命中，这条 `grep` **不能单独证明删净**；还要看 `SchedulePreloadRetry` / `CancelPreloadRetry` / `preloadRetries` 三个符号全仓零引用 + 全量绿。

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Sync.lua tests/run_gql_tests.py
git commit -m "fix: 11-04 整板块补读（5 秒×4）整套下线，缺的钱只走定向补读"
```

---

### Task 4: 右键"不做"当场停掉这一号为这条补读

**Files:**
- Modify: `GoldQuestLedger/Sync.lua:223`（`askedRewardData` 结构）、`:256-293`（`SettleAskedRewardGold`）、`:406-434`（现场问答那一路的预加载分支）
- Test: `tests/run_gql_tests.py` 新增 4 条（放在 `test_reward_data_arrival_settles_gold_without_the_matrix` :16828 之后，**全部注册进名单**）

**Interfaces:**
- Consumes: `L.State(db, charKey, questID, gate, now, curKey)`（`Ledger.lua:1717`，`skip` 的唯一有效判据）、`L.InstanceKey(...)`、`L.SetManual(...)`（`Matrix.lua:269` 是写点）、`Sync.CurrentCharKey()`（`Sync.lua:79`，每次现算，改 `UnitName` 就能换号）。
- Produces: 文件内 `local function SkipThisInstance(db, questID, charKey, now) -> boolean`；`askedRewardData` 的值从数字改成按号子表 `questID -> { [charKey] = tries }`。Task 5 不复用它们（那是读数口径，不是停问口径）。

- [ ] **Step 1: 写失败用例（四族）**

四族各自钉住什么：①本号本轮标 skip ⇒ 停问；②别的号标 skip ⇒ 不挡本号；③**本号标 skip 后换号登录该条仍能补读**（按号了结的反面，原稿漏了）；④上一轮烙的 `m.k` 对不上当前实例 ⇒ 不挡本轮。`SetManual` 的 `t` 必须用 **`wall` 那个纪元秒**（`1_790_000_000`），不能写 `10`：环境里 `wall` 与 `roundEnd` 都在纪元秒域，开机秒会让 `m.k` 跨域烙不上，用例测到的就是坏实现。四条全部注册进 `tests` 名单。

```python
def test_a_skipped_char_stops_the_reward_reread():
    """右键标了"这一号本轮不做" ⇒ 这一号不再替这条问钱：补读当场了结，
    现场问答那一路也不再向服务器要这份数据（用户 11-04："直到拿到金币数
    或者用户右键表示不做为止"）。替身记金额接口被问了几次，钉的就是次数。"""
    name = "a_skipped_char_stops_the_reward_reread"
    lu, gg, gl, seen = _money_env(93)            # 见下面的共用搭环境函数
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 93, 0, None)
    fire(gg, "QUEST_ACCEPTED", 93)               # 金额问不出 ⇒ 进补读表
    if not seen:
        return fail(name, "前置：现场问答那一路要问过金额接口")
    seen.clear()
    L.SetManual(db, charKey, 93, "skip", 1_790_000_000, None)
    # 每次触发之间要把 GQL_NOW 往前推：日志旁路自带节流（LOG_BYPASS_COOLDOWN），
    # 连着敲三下只会认第一下，那样测出来的是"没跑到"，不是"停住了"。
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)       # 补读借的就是这一串事件
    if seen:
        return fail(name, f"标了不做还替这一号问钱，问了 {len(seen)} 次")
    logs = str(gg.Config.GetLogs())
    if "补读停：这一号标了不做" not in logs:
        return fail(name, f"停了要在日志里说清为什么停:\n{logs}")
    ok(name)


def test_a_skipped_other_char_does_not_stop_this_one():
    """"不做"是按号的：别的号标了不做，眼前这一号该问的照问（否则一个号的放弃
    会替全账号关掉补读，正是 11-04 之前那类"当前号的事实被当全账号凭据"的形状）。"""
    name = "a_skipped_other_char_does_not_stop_this_one"
    lu, gg, gl, seen = _money_env(94)
    L, db = gg.Ledger, gl.GQLDB
    L.AddQuest(db, 94, 0, None)
    L.SetManual(db, "别人-永恒之井", 94, "skip", 1_790_000_000, None)
    seen.clear()
    fire(gg, "QUEST_ACCEPTED", 94)
    if not seen:
        return fail(name, "别的号标了不做，不该挡住当前这一号的补读")
    ok(name)


def test_a_skipped_char_does_not_close_the_quest_for_other_chars():
    """-1 只对本号生效：本号标了不做，换一个号登录这条仍要能补读。
    Sync.CurrentCharKey() 每次现算（Sync.lua:79），改 UnitName 就是换号。"""
    name = "a_skipped_char_does_not_close_the_quest_for_other_chars"
    lu, gg, gl, seen = _money_env(95)
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 95, 0, None)
    fire(gg, "QUEST_ACCEPTED", 95)
    L.SetManual(db, charKey, 95, "skip", 1_790_000_000, None)
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)      # 本号这一份被了结
    gl.UnitName = lambda who: "另一个号"        # 换号：byChar[新号] 还是 nil
    gl.GQL_NOW = 2000
    seen.clear()
    fire(gg, "QUEST_ACCEPTED", 95)             # 新号的现场问答该照样向服务器要数据
    if -95 not in seen:
        return fail(name, f"本号的放弃不该替别的号关掉补读，实际 {seen}")
    ok(name)


def test_a_stale_round_skip_does_not_stop_this_round():
    """上一轮烙的 skip：m.k 对不上当前实例 ⇒ 这一轮照旧问（不许把陈旧 × 当本轮凭据）。"""
    name = "a_stale_round_skip_does_not_stop_this_round"
    lu, gg, gl, seen = _money_env(96)
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 96, 0, None)
    fire(gg, "QUEST_ACCEPTED", 96)
    L.SetManual(db, charKey, 96, "skip", 1_790_000_000, None)
    lu.execute("for _, v in pairs(GQLDB.manual) do if v[96] then "
               "v[96].k = '96:上一轮' end end")   # 换轮后老档残留的形状
    seen.clear()
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)
    if not seen:
        return fail(name, "上一轮的 skip 不该挡住这一轮的补读")
    ok(name)
```

同文件里补上一个 helper（放在这四条用例之前；形状照 :16851 那个 `env` 抄，只是把金额接口固定答 0 并记下调用次数）：

```python
def _money_env(qid):
    """一套"金额永远问不出、倒计时答得出"的环境，并把金额接口的调用次数记下来。
    seen 里正数 = 问过金额，负数 = 向服务器要过这份数据（用例 3 靠它分辨"没被全局 -1 关掉"）。
    返回 (lua, gql, globals, 被问过的 questID 列表)。"""
    lu, gg, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Matrix",))
    gl = lu.globals()
    seen = []

    def money(a):
        seen.append(int(a))
        return 0                                  # 0 = 数据还没到（Api 只认正数）

    gl.C_TaskQuest = lu.table_from({
        "GetQuestTimeLeftSeconds": lambda a: 7200 if int(a) == qid else None,
        "RequestPreloadRewardData": lambda a: seen.append(-int(a)),
    })
    gl.HaveQuestRewardData = lambda a: False
    gl.GetQuestLogRewardMoney = money
    gl.QuestUtils_IsQuestWorldQuest = lambda a: True
    gl.C_QuestLog = lu.table_from({
        "GetInfo": lambda i: lu.table_from(
            {"questID": qid, "title": "任务%d" % qid, "questLogIndex": 1})
            if int(i) == 1 else None,
        "IsQuestStarted": lambda a: a == qid,
        "IsQuestCompleted": lambda a: False,
    })
    return lu, gg, gl, seen
```

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k skipped_char,skipped_other,stale_round_skip`
Expected: 第一条 FAIL（标了不做还替这一号问钱）；第三条 FAIL（换号后 `seen` 里没有 `-95` —— 现实现把 `askedRewardData[questID]` 当全局，`早就问过服务器了` 那支把新号也挡了）。第二条 PASS（现在压根不看 skip，谁都问）；第四条 PASS，它是"不许回退到裸读 raw state"的回归护栏，本来就不该在改前变红。

- [ ] **Step 3: 加判据并接进两处**

`GoldQuestLedger/Sync.lua` 在 `SettleAskedRewardGold` 之前（:246 那段注释之前）加：

```lua
--- 这一号在**这一条实例**上标了"本轮不做"没有（矩阵格子右键，见 Matrix.CellClick）。
--- 必须走 L.State：raw manual[].state 从不自动作废，作废只发生在 L.State:1732 读时比 m.k；
--- 裸读会把上一轮的 × 当本轮有效，永久掐掉新实例的补读。L.State 还会把
--- "标了不做又真做了"判成 done，与界面口径一致。纯账本，零接口。
local function SkipThisInstance(db, questID, charKey, now)
    local rec = db and db.quests and db.quests[questID]
    local curKey = L.InstanceKey(db, questID, rec, now)
    if not curKey then return false end
    return L.State(db, charKey, questID, nil, now, curKey) == "skip"
end
```

同时把 `:223` 的表定义注释改成按号结构：`local askedRewardData = {}`（键仍是 questID，值改成 `charKey -> tries` 的子表；`-1` 只对本号了结）。

`SettleAskedRewardGold`（:256-293）整条函数替换为（改动只有三处：`for questID, byChar` + `local tries = byChar[charKey]`、所有 `askedRewardData[questID] = ...` 改成 `byChar[charKey] = ...`、`CharSaidSkip` 换成 `SkipThisInstance`；中间落账那几支原样搬过来）：

```lua
local function SettleAskedRewardGold(db, now)
    local changed, counted = 0, 0
    local charKey = Sync.CurrentCharKey()
    for questID, byChar in pairs(askedRewardData) do
        local tries = byChar[charKey]
        if tries ~= nil and tries >= 0 then      -- -1 = 这一号已了结，不再问
            if SkipThisInstance(db, questID, charKey, now) then
                -- 【11-04 定稿 §二 5】玩家明说这一号本轮不做它 ⇒ 只把这一号了结。
                -- 别的号那一笔留在表里，换号登录照旧补读（见 Step 1 用例 3）。
                -- 次数封顶保留：挂机期间每次日志更新白问一条永远问不出的就是白烧接口调用。
                byChar[charKey] = -1
                GQL.Config.Log(("补读停：这一号标了不做 quest=%s 角色=%s")
                    :format(tostring(questID), tostring(charKey)))
            elseif counted >= GOLD_READ_PER_PASS then
                break
            else
                counted = counted + 1
                local okM, m = pcall(GQL.Api.QuestRewardMoney, questID)
                if okM and type(m) == "number" and m > 0 then
                    -- 落账走 L.AddQuest 这一个记录写点（不在这儿再拼一份观察/签名/lastMoney 的写法）：
                    -- 它自己读金额、按金额分观察桶，把刚问出来的这份直接递给它的 knownMoney ——
                    -- 两次读之间客户端换了答案的话，落账的数和记账的数会落进两个签名。
                    GQL.Ledger.AddQuest(db, questID, now, nil, m)
                    local rec = db.quests[questID]
                    if rec then
                        local curKey = L.InstanceKey(db, questID, rec, now)
                        -- 观察桶先烙上这一条实例自己的编号，再走 NoteGoldThisInstance：
                        -- 它对"编号对不上（含没编号）的那份观察"会把金额作废（那是上一条实例的账），
                        -- 刚建出来的观察还没有编号 ⇒ 排在它之后写就等于自己把刚读到的正数清成 nil。
                        local obs = L.GetObs(db, questID)
                        if obs then obs.key = curKey end
                        L.NoteGoldThisInstance(db, questID, m, now, curKey)
                        L.SetRewardRead(db, charKey, questID, m, now, curKey)
                        changed = changed + 1
                        GQL.Config.Log(("补读到金额 quest=%s 钱=%s 编号=%s（矩阵没开过也进账了）")
                            :format(tostring(questID), tostring(m), tostring(curKey or "算不出")))
                    end
                    byChar[charKey] = -1
                elseif tries + 1 >= GOLD_READ_MAX_TRIES then
                    byChar[charKey] = -1
                    GQL.Config.Log(("补读 %d 次仍问不出金额 quest=%s（服务器那份数据这一趟没到，不猜）")
                        :format(GOLD_READ_MAX_TRIES, tostring(questID)))
                else
                    byChar[charKey] = tries + 1
                end
            end
        end
    end
    return changed
end
```

（原来那句 `L.SetRewardRead(db, Sync.CurrentCharKey(), questID, m, now, curKey)` 现在用循环外取好的 `charKey` —— 同一趟少问一次当前号，纯减法。）

现场问答那一路（:412-434）整块改为。**skip 分支放在 `if` 外面**：放进 `:413` 体内是死代码（外层条件挂了 `not skip` 就整块不执行），而这句取证正是要给玩家看的；同时也别在外层条件上挂 `not skip`。

```lua
    local charKey = Sync.CurrentCharKey()
    local byChar = askedRewardData[questID]
    local askedText = ""
    if SkipThisInstance(db, questID, charKey, now) then
        askedText = "（这一号标了不做，没去要这份数据）"
    elseif money == nil and GQL.Api.PreloadRewardData then
        if byChar and byChar[charKey] then
            -- 按号判：别的号问过不算这一号问过；-1 也只对本号了结
            askedText = "（早就问过服务器了）"
        elseif hasData == true then
            -- 原注释照搬：客户端说数据在本地却读不出金额 = 这一档它答的是不给金币，不进补读表。
            askedText = "（奖励数据本来就在本地，这一档客户端答的是不给金币）"
        elseif not (GQL.Api.Callable and GQL.Api.Callable(
                GQL.Api.Resolve("C_TaskQuest.RequestPreloadRewardData"))) then
            askedText = "（本机没有向服务器要奖励数据的入口，只能等开一次世界地图）"
        else
            GQL.Api.PreloadRewardData(questID)
            byChar = byChar or {}
            byChar[charKey] = 0
            askedRewardData[questID] = byChar
            askedText = "（已向服务器要这份数据）"
        end
    end
```

（`hasData == true` 那一支照旧**不进补读表**：它不是"停"，是从来没开始；`GOLD_READ_PER_PASS = 12` 是每趟上限，也不算停的条件。）

- [ ] **Step 4: 跑到绿**

Run: `python tests/run_gql_tests.py -k skipped_char,skipped_other,stale_round_skip,reward_data_arrival,event_capture_asks`
Expected: 全 PASS。`test_event_capture_asks_the_server_once_for_reward_data`（:16310）不许改——没标不做时照旧要一次数据；`test_reward_data_arrival_settles_gold_without_the_matrix`（:16828）里 `reads2.count(89) != 8` 那条断言照旧绿（按号子表不改变单号会话内的 6 次封顶）。

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Sync.lua tests/run_gql_tests.py
git commit -m 'feat: 11-04 右键不做当场停掉这一号的补读（按实例有效、按号了结）'
```

（用单引号包消息：原稿在双引号里嵌了双引号 `右键"不做"`，shell 会把它拼断。）

---

### Task 5: 读数默认按行，这条上出现过 0 才按号

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:1417-1428`（`CharRewardMoney` 之后新增两个函数）、`:1100-1123`（`LastCompletion`）、`:1834-1847`（`WeekStats` 实测分支）
- Modify: `GoldQuestLedger/Matrix.lua:544-553`（格子悬停那行金额）
- Test: `tests/run_gql_tests.py` 新增 4 条（放在 `test_week_stats_gold_counts_own_read` :9455 之后，**全部注册进名单**）、`tests/run_gql_tests.py:9426`（`test_week_stats_gold_excludes_other_chars_read`）核对不改

**Interfaces:**
- Consumes: `L.CharRewardMoney(db, charKey, questID, now, curKey)`、`L.SameInstanceKey(a, b)`、`L.InstanceKey(db, questID, rec, now)` —— 全部现有。
- Produces:
  - `L.RewardDiffers(db, questID, now, curKey) -> boolean`：同一实例上是否**至少两个不同号、一个正数一个 0**（判据见 §二 6，不是"见 0 就算"）。
  - `L.CellRewardMoney(db, charKey, questID, now, curKey) -> number|nil`：这一格该报多少钱（正数 / 0 / nil）。`curKey` 优先用调用方显式传的；没传才按传入的 `now` 现算，**不许偷偷用 `LedgerNow(db)`**。三处消费侧一律改问它、并显式传该场景的编号，界面不许再拼第二套判据。

- [ ] **Step 1: 写失败用例**

```python
def test_a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero():
    """默认按行（用户 11-04："刚开始确实是整行的…绝大部分情况下按行没问题"）：
    这条任务上没有任何一个号读到过 0 = 没有分岔的凭据 ⇒ 还没同步过的号照旧报行级那份钱，
    不再一律 `?`。11-03 那一版把三处消费侧全改成只认本号读数，代价落到了这种正常任务上。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(155, 10890000, None)], 6000, {155: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 155, 10890000, 6100)
    L.OnTurnIn(db, "没同步过的号-永恒之井", 155, 6200, None)
    if L.RewardDiffers(db, 155, 6300) is not False:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    "没人读到过 0 = 判不出分岔")
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 155, 6300) != 10890000:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    "不分岔的条上，没读过的号该报行级那份 1089")
    count, gold = L.WeekStats(db, "没同步过的号-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != 10890000:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    f"周汇总同样按行：完成 1 条、金额 1089，实际 {count}/{gold}")
    ok("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero")


def test_a_zero_read_on_any_char_forbids_borrowing_for_the_rest():
    """例外那一档（用户 11-04 点名的形状：有的号给金币、有的号不给，不给的号也做了）：
    这条实例上风行者正数、麦德 0 = 分岔凭据成立 ⇒ 没自己读到过数的号一律不给数（`?`），
    因为这时候借谁的钱都是猜（11-03 截图「麦德 … 到手 1089🪙」就是这么来的）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(156, 10890000, None)], 6000, {156: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 156, 10890000, 6100)
    L.SetRewardRead(db, "麦德-永恒之井", 156, 0, 6100)
    L.OnTurnIn(db, "丙没读-永恒之井", 156, 6200, None)
    if L.RewardDiffers(db, 156, 6300) is not True:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "一正一 0 两个不同号 = 这条按号会变，要判成分岔")
    if L.CellRewardMoney(db, "丙没读-永恒之井", 156, 6300) is not None:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "分岔的条上没读过的号不许借风行者的 1089")
    if L.CellRewardMoney(db, "麦德-永恒之井", 156, 6300) != 0:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "读到 0 的那一号照旧报 0（客户端明说不给金币）")
    count, gold = L.WeekStats(db, "丙没读-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != -1:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    f"分岔的条上没读过的号：完成 1 条、金额如实未知，实际 {count}/{gold}")
    ok("a_zero_read_on_any_char_forbids_borrowing_for_the_rest")


def test_a_lone_zero_read_is_not_a_reward_split():
    """推断出来的 0 不许把整条任务打回按号：Api.lua:187-189 会在"数据在本地、
    金额链却读不出"时造出 money=0，Matrix.lua:573-575 也承认 0 可能是做完前的旧答案。
    只有一个号读到 0（没有另一个号读到正数）时，分岔不成立，没读过的号照旧借行级。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(157, 10890000, None)], 6000, {157: 43200})
    L.SetRewardRead(db, "推断零-永恒之井", 157, 0, 6100)
    L.OnTurnIn(db, "没同步过的号-永恒之井", 157, 6200, None)
    if L.RewardDiffers(db, 157, 6300) is not False:
        return fail("a_lone_zero_read_is_not_a_reward_split", "单号 0 不构成分岔")
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 157, 6300) != 10890000:
        return fail("a_lone_zero_read_is_not_a_reward_split", "推断 0 不该让全员报 ?")
    ok("a_lone_zero_read_is_not_a_reward_split")


def test_an_old_save_without_row_money_stays_unknown():
    """§三 的承诺要有兜底：老档里 goldKey 还在、goldMoney 被迁移清掉
    （Ledger.lua:2323；pool_pass 给 None 金额就是同一个形状）⇒ 借数如实报 ?。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(158, None, None)], 6000, {158: 43200})
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 158, 6300) is not None:
        return fail("an_old_save_without_row_money_stays_unknown", "行级金额为空时不许借、不许编")
    ok("an_old_save_without_row_money_stays_unknown")
```

四条新用例**全部注册进 `tests` 名单**。

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k borrows_the_row_gold,forbids_borrowing,lone_zero_read,old_save_without_row_money`
Expected: 四条都 `FAIL ...: 用例抛错: ... RewardDiffers / CellRewardMoney ... nil`（函数还不存在，AttributeError 即红）。

- [ ] **Step 3: 加两个函数并接三处消费侧**

`GoldQuestLedger/Ledger.lua` 在 `L.CharRewardMoney`（:1428 的 `end`）之后插入：

```lua
--- 这条实例上是否真的"按号分岔"：至少两个不同 charKey，一个正数、一个 0。
--- 纯账本一次遍历，零接口调用。**见 0 就判分岔是不行的**：Api.lua:187-189 会在
--- "数据在本地却读不出金额"时造出推断 0，Matrix.lua:573-575 也承认 0 可能是做完前的
--- 旧答案；写侧"正数不被 0 冲掉"的真身在 Ledger.lua:1400-1403（Config.lua:32-35 只是注释），
--- 且只在"先正数后 0"生效。一个推断 0 就把所有没读过的号打回 ?，正是本轮要消灭的症状。
--- 一个 charKey 只有一条 reads[questID]，所以 sawZero + sawPositive 天然来自两个不同号。
function L.RewardDiffers(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    if curKey == nil and now ~= nil then
        curKey = L.InstanceKey(db, questID, rec, tonumber(now))
    end
    if not curKey then return false end
    local sawZero, sawPositive = false, false
    for _, row in pairs(db.chars or {}) do
        local r = row and row.reads and row.reads[questID]
        if r and r.k ~= nil and L.SameInstanceKey(r.k, curKey) then
            local g = tonumber(r.g)
            if g == 0 then sawZero = true
            elseif type(g) == "number" and g > 0 then sawPositive = true end
        end
    end
    return sawZero and sawPositive
end

--- 这一格该报多少钱：正数 / 0 / nil（nil = 界面报"金额未知"）。
--- 11-04 定稿：默认按行，只有这条实例上判出分岔才按号 —— 11-03 那一版把三处消费侧
--- 全改成只认本号读数，是从"整行一个数"走到"一律按号"的另一个极端。
--- 借的那一份必须是**同一条实例**的行级金额（rec.goldKey 与传入编号对得上）：
--- 上一条实例留下的钱不给这一条背书，跟 CharRewardMoney 同一道门。
--- curKey 优先用调用方显式传的；没传才按传入的 now 算，不许偷偷用 LedgerNow(db)。
function L.CellRewardMoney(db, charKey, questID, now, curKey)
    local own = L.CharRewardMoney(db, charKey, questID, now, curKey)
    if own ~= nil then return own end
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local key = curKey
    if key == nil and now ~= nil then
        key = L.InstanceKey(db, questID, rec, tonumber(now))
    end
    if not key then return nil end
    if L.RewardDiffers(db, questID, now, key) then return nil end
    if not L.SameInstanceKey(rec.goldKey, key) then return nil end
    local g = tonumber(rec.goldMoney)
    if type(g) == "number" and g > 0 then return g end
    return nil
end
```

三处消费侧替换（只换问的那个函数，分支结构不动）：

1. `Ledger.LastCompletion`（:1110）：

```lua
            gold = L.CellRewardMoney(db, charKey, questID, nil, r.k)
```

2. `Ledger.WeekStats`（:1840）：

```lua
                    local g = L.CellRewardMoney(db, charKey, questID, nil, r.k)
```

3. `Matrix.lua`（:544-547）：把该场景的实例编号**显式算好再传**（只加两个 `local`，不开新 upvalue —— `GQL` 本来就在）：

```lua
        local own
        if charKey and GQL.Ledger and GQL.Ledger.CellRewardMoney then
            local rec = db.quests and db.quests[questID]
            local curKey = GQL.Ledger.InstanceKey(db, questID, rec, now)
            own = GQL.Ledger.CellRewardMoney(db, charKey, questID, now, curKey)
        end
```

`Matrix.lua:537-543` 那段"2026-11-03 起格子上不借行金额"的注释要跟着改成 11-04 的口径（默认按行、只有这条实例上判出分岔才按号），别留一段自相矛盾的说明。`test_lua_upvalue_budget` 只在**不带 `-k` 的全量**里跑，改完别只拿过滤跑的绿当结论。

- [ ] **Step 4: 跑到绿（含 11-03 那几族反向用例）**

Run: `python tests/run_gql_tests.py -k week_stats_gold,cell_hover,char_reward_read,gold_readers,tooltip,no_gold_line,borrowed_gold,lone_zero_read,old_save_without_row_money`
Expected: 全 PASS。`test_week_stats_gold_excludes_other_chars_read`（:9426）与 `test_cell_hover_shows_that_alts_own_gold_not_the_last_syncers`（:8930）**不许改**：那两条的前置里都有号读到 0 且另有号读到正数，分岔成立，所以借不到数——正是新口径要保住的反面。

`test_tooltip_cell_without_char_read_says_unknown`（:9482）的前置是 `cols={"风行者":10890000, "麦德":None}`，**没有 0**，新口径下会借到行级 1089 ⇒ 它必然变红。处理方式是**重新定位、不是删**：
- 给它的前置补一个读到 0 的号（例如 `物品号: 0`），让它落在"分岔"场景；docstring 相应改成"分岔时没读过的号不许借、也不许说成不给金币"。它原来钉的核心断言（`1089` 不出现在这一格、不出现"不给金币"）一字不动。
- 新口径的正向那一面由 Step 1 的 `test_a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero` 与 `test_a_lone_zero_read_is_not_a_reward_split` 覆盖，两面对照，不许只留一面。

- [ ] **Step 5: 全量 + 静态守卫**

Run: `python tests/run_gql_tests.py`（不带参数、不带 `-k`；按 Global Constraints 收尾）
Expected: 末行 `N/N 单测通过` 且**退出码 0**（N ≥ 451：基线 440 + Task 1 新增 3 条 + Task 4 新增 4 条 + Task 5 新增 4 条；Task 2/3 没有新增，Task 3 那两条是改名、不减数量。Task 6 再净增 1 条 ⇒ 452）。`static` 前缀的 `upvalues:` / `syntax:` 失败不减末行分子，只靠退出码能发现，所以两句都要看（`tests/run_gql_tests.py:19749-19751`）。
Run: `git diff --stat` 逐行 review 本任务改动。

- [ ] **Step 6: 提交**

```bash
git add GoldQuestLedger/Ledger.lua GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "fix: 11-04 奖励读数默认按行，这条实例上出现过 0 才按号（收掉另一个极端）"
```

---

### Task 6: 移除刷新按钮，标题栏报最后一次抓池时刻

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua:1419-1424`（标题栏版本文字）、`:1479-1528`（刷新按钮整块删除）、`:1932-1946`（`Mx.Toggle` 注释）、`GoldQuestLedger/Options.lua:12-15`（"标题栏早有同一颗按钮"的注释）
- Test: `tests/run_gql_tests.py:9738`（`test_matrix_refresh_button_sits_by_settings_and_resyncs` 改名重写）、新增 1 条时钟域形状用例；**两条都注册进名单**（改名那条同步改 `:19484` 的旧条目）

**Interfaces:**
- Consumes: `db.chars[charKey].lastSync`（`Sync.lua:1081`）、`GQL.Api.Uptime()`（`Api.lua:1422`）、`GQL.Sync.CurrentCharKey()`、`Mx.Toggle()` —— 全部现有。
- Produces: 标题栏一个纯 `FontString`（`Ensure` 阶段建、抓池后刷文本）+ 一个文件内 `local function LastSyncText(db)`。**无新事件、无新定时器、无新 upvalue**，`Mx.Render` 一行不动。

- [ ] **Step 1: 写失败用例（改名 1 条 + 新增 1 条，全部注册）**

把 `test_matrix_refresh_button_sits_by_settings_and_resyncs`（`tests/run_gql_tests.py:9738`）改名重写成 `test_opening_matrix_resyncs_and_shows_last_sync`。保留它原来钉住的三件：`Toggle()` 之后账本里 101 回来、**轮询被打开**、矩阵真的画出了行文本（"打开一篇漆黑"事故，`Ensure` 断在半路）。把"标题栏有刷新按钮 + 点它重画"换成两条新钉：
- 标题栏**没有**文本为"刷新"的控件；
- 标题栏有 `最后同步` 字样的文本，且跟着 `db.chars[charKey].lastSync` 变（改一次 `lastSync`、`GQLMatrixFrame:Hide()` 后再 `Mx.Toggle()` 一次，文本要跟着变）。
- **`f:Show()` 必须在抓池之前**：包装 `GQL.Sync.SnapshotNow`，在它被调用那一刻断言 `GQLMatrixFrame:IsShown()` 为 true（现状是 false，打开那趟的 `Mx.Refresh()` 全被 `Matrix.lua:1909` 丢掉）。这条钉住"抓到了但没重画"那一半，不新增用例函数。
删掉用例里 `SetPolling(False)` 与 `click_frame_by_text(..., "刷新")` 那两段：面板开着时 `polling` 恒 true（`OnHide` 是唯一的 false 入口），按钮承担过的"把轮询打开"已由 `Mx.Toggle()` 打开时的 `SetPolling(true)` 覆盖。

新增 `test_last_sync_label_handles_three_clock_shapes`（同样注册进名单）：
- 纪元域：`g.date = lambda fmt, t: "12:03:45"`，`lastSync = 1_790_000_000` ⇒ 文本 `最后同步 12:03:45`（钉住实现走 `date`，不是把 epoch 数字直接印出来；`tests/run_gql_tests.py` 目前没有 `date` 替身，这里补一个）；
- 开机秒域：`sync_env` 不 stub `_G.time` / `GetServerTime` / `GetLocalTime`，`lastSync = 1000`、`GQL_NOW = 1600` ⇒ `最后同步 10 分前`；
- 无 `lastSync` ⇒ `最后同步 —`。
三种形状都要钉"**没有为时间戳新增定时器**"：`GQL_NEWTIMERS` 在 Toggle 前后的增量只允许是 `ScheduleNextRefresh` 那一颗（delay ≥ 5 秒、只有 1 个），不许出现 `delay <= 1` 的重复定时器，也不许给任何帧挂 `OnUpdate`。

- [ ] **Step 2: 跑到红**

Run: `python tests/run_gql_tests.py -k opening_matrix_resyncs,last_sync_label`
Expected: 两条 FAIL（标题栏还有"刷新"、也没有 `最后同步` 文本）；`date` 那条断言要落在"文本不对"上，不许靠替身缺函数蒙混成绿。

- [ ] **Step 3: 删按钮 + 加时间戳**

1. 删 `Matrix.lua:1479-1528` 整块（`refreshBtn` 的创建、`OnClick` / `OnEnter` / `OnLeave`、`print` 回报，含 `:1503-1505` 那段悬停说明）。`cfgBtn`（设置按钮）在它前面，**不动**。同步改 `Matrix.lua:1938` 与 `Options.lua:12-15` 里"矩阵标题栏早有同一颗按钮（用例 ... 钉住）"的注释。
2. 在 `Ensure` 建皮阶段、标题栏版本 `FontString` 右侧（`refreshBtn` 原来锚在 `cfgBtn` 左边，那块空位现在归时间戳）建一个纯 `FontString`，文字右对齐、锚 `cfgBtn` 左边；不放进 `Mx.Render`。
3. 文本计算（**只在打开/抓池后算一次**，不挂定时器）：

```lua
local function LastSyncText(db)
    local row = db and db.chars and db.chars[GQL.Sync.CurrentCharKey()]
    local t = row and tonumber(row.lastSync)
    if not t then return "最后同步 —" end
    if t > 1e9 then
        if date then return "最后同步 " .. tostring(date("%H:%M:%S", t)) end
        return "最后同步 —"           -- 拿不到时钟函数就不编
    end
    local now = GQL.Api and GQL.Api.Uptime and GQL.Api.Uptime()
    if not now or now > 1e9 then return "最后同步 —" end  -- 跨域算不出
    return ("最后同步 %d 分前"):format(math.max(0, math.floor((now - t) / 60)))
end
```

4. `Mx.Toggle()` 打开时（`SnapshotNow()` 之后）刷一次标题文本；`Sync.SnapshotNow()` 末尾也刷一次（用 `pcall` 包住，标题文本不许让整趟抓池失败）。`Mx.OnHide` 不动。
5. **把 `f:Show()` 提到抓池之前**（`Matrix.lua:1924-1949`）：现在是 `SetPolling(true)` → `SnapshotNow()` → `RefreshNow()` → `Show()`，改成 `f:Show()` → `SetPolling(true)` → `SnapshotNow()` → `Mx.RefreshNow()`。理由：`Mx.Refresh()` 有 `frame:IsShown()` 门（`:1909`），打开那一趟里 `HuntKeywordMaps` / `LogBypass` 触发的重画全被丢；换顺序后同步首绘仍在最后，`lastPaint` 不被提前推进，零接口、不新增定时器。该帧没有 `OnShow` 脚本，改顺序不会触发额外副作用。

- [ ] **Step 4: 跑到绿**

Run: `python tests/run_gql_tests.py -k opening_matrix_resyncs,last_sync_label,windows_are_opaque,hover_ignores_foreign_mouse_focus,refresh_wakes`
Expected: 全 PASS。`test_windows_are_opaque`（:9819）与 `test_hover_ignores_foreign_mouse_focus`（:9836）不许改——它们走同一张标题栏皮；`test_refresh_wakes_at_earliest_expiry` 不许因删按钮变红（它不点按钮）。

- [ ] **Step 5: 全量 + 静态守卫**

Run: `python tests/run_gql_tests.py`（不带参数、不带 `-k`）
Expected: 末行 `N/N 单测通过` 且退出码 0（**N ≥ 452**：基线 440 + Task 1 3 条 + Task 4 4 条 + Task 5 4 条 + Task 6 净增 1 条），`static` 里 `upvalues:` 不报红（`Mx.Render` 一个 upvalue 都没加）。
Run: `git diff --stat` 逐行 review 本任务改动。

- [ ] **Step 6: 提交**

```bash
git add GoldQuestLedger/Matrix.lua GoldQuestLedger/Options.lua tests/run_gql_tests.py
git commit -m 'ui: 11-04 移除刷新按钮，打开即抓池，标题栏显示最后一次同步时刻'
```

---

## 四、本轮不做（写清楚，免得下一个人顺手加）

- **不做自动重试购买那类外部注入**：客户端答不出的事不猜。
- **不给"永不下板"的行另开豁免**：每行都要有一条会过去的结束时刻（第 41 轮 D6/D7 结案）。
- **不动金币门槛、准入门、屏蔽窗口**：本轮只动"什么时候问接口"和"某一格报哪个数"。
- **不新增事件监听**：`C_QuestRewards.FetchData` 本机没有、也没有实测过的"奖励数据到了"事件名（11-04 两次回传定案），补读只能搭 `QUEST_LOG_UPDATE`。
- **界面净减一个控件**：Task 6 删掉标题栏刷新按钮，只在原位置放一行纯文本的最后同步时刻；`skip` 的入口还是格子右键，本轮不重开 10-27 下线的那个整行右键菜单。
- **不把"等效按 M"并进打开流程**：`ForceMapDataLoad()` 留在 `Sync.lua:1114-1118` 的 `emptyMaps` 自动路径即可（方案 B 不采纳）；`Mx.Toggle()` 不为时间戳或抓池新增定时器，也不新增事件监听。

## 五、落地后回写

- 计划末尾追加一节「实测回传」，记：面板开着期间一次快照的接口调用次数变化（改前每行 1~2 次倒计时 + 整板块重问，改后只在结束时刻没定下来时问）、`?` 格子少掉的那几格是哪几个号、以及分岔判定在真档上命中过哪几条任务。
- 回传里再记一条界面实测：删掉刷新按钮后，玩家"关开矩阵"能不能把换板后的新任务带出来、标题栏"最后同步"在挂机一小时后是不是如实停住（不跟着重画前进），以及纪元域 / 开机秒域分别显示成什么形状。
- 更新项目记忆 `project-wqt-per-char-reward-variant`（按号那节补"11-04 退回按行默认 + 分岔才按号"）与 `project-wqt-gold-row-latency-openbug`（整板块补读下线）。

## 六、11-04 review 后的修订清单（对着当前工作树复核）

实施以本版为准。逐条复核原稿后的改动：

| 处 | 原稿 | 本版 |
|---|---|---|
| 基线 / 收尾 | 427/427、`--syntax` | 实测 440/440；新增用例重算为 12 条；Task 5 收尾 N ≥ 451、Task 6 收尾 N ≥ 452；不带参数全量 + 退出码 0 |
| 用例注册 | 只有 Task 1 提了 | 所有 Task 的 Step 1 都写明注册；改名的两条同步改名单 `:19436` / `:19657` |
| Task 1 用例 | `roundEnd == 44200` | `43200`（`board=43200` 钉的是固定结束时刻，倒计时答 `43200 - GQL_NOW`） |
| Task 1 落点 | 只改池循环 `:1200-1204` | 三处读取点共用 `ShouldAskExpiry`，补一条走 `QUEST_ACCEPTED` 的用例 |
| Task 2 断言 | 快照数 `> 1`、包装 `RefreshNow` | 触发前后增量、触发前清零、包装 `GQL.Matrix.Refresh`；回调保留 `Refresh()` 不换 `RefreshNow()` |
| Task 3 删净 | `grep` 期望无输出 | 删除清单补 `:1155` / `:1199` 两条注释；`grep` 只作辅助 |
| Task 4 | 裸读 `manual[].state`、`askedRewardData[questID] = -1` | 走 `L.State` 按实例判有效；值改按号子表；`askedText` 的 skip 分支移出 `if`；用例 2 → 4 条，`SetManual` 用纪元秒 |
| Task 5 | 见 0 即分岔、函数内部现算编号 | 分岔 = 同实例两个不同号一正一 0；编号显式传；用例 2 → 4 条；旧口径用例重新定位而非删 |
| 代价 | 只写了 resident 族与挂机缺时刻 | 补上取证退化、换板不自动刷新、`lastSync` 不再前进、`KeywordLogAdmits` 的字符串匹配代价；并写清"按 M 只拉列表、奖励金额是另一条异步链路" |
| 界面（11-04 设计确认新增） | 刷新按钮照旧在，§二 4 把它当兜底出口 | Task 6 删按钮 + 标题栏纯文本"最后同步"（报最后一次抓池 `lastSync`，不报重画）；§二 3/4 的兜底统一改成"关开矩阵"；方案 B（把 `ForceMapDataLoad` 并进 `Toggle`）不采纳 |
| 提交信息 | `git commit -m "…右键"不做"…"` | 单引号包消息（原稿双引号嵌套会被 shell 拼断） |
