# 2026-10-27 第三十轮：整体改名 GoldQuestLedger（含存档搬迁），图标沿用现状

## 一、用户口径（原话引，别改写）

> 我已经关闭了游戏，现在你可以删除王牌狩猎了，另外这个世界任务插件换个名字，目前 WorldQuestTracker 已经被人用过了。
> 我们是金币世界任务多角色跟踪。文件夹名字也得换，另外和 AHSnipe 一样 也要有个图标

对齐之后的三条决定（问答确认，算口径）：

- 新名 **GoldQuestLedger**；**全套一起改**（文件夹 = toc = 全局命名空间 = 存档变量名 = 斜杠命令）+ **存档搬迁**。
- 图标：**现状已满足，跟着改名搬迁即可**。本轮**不新增任何图标代码**——插件列表那张
  （`## IconTexture: Interface\Icons\INV_Misc_Coin_01`，toc:6）和小地图那颗可拖动、角度存进存档的
  金币按钮（`Matrix.lua:1736-1834` 的 `Mx.EnsureMapButton`）都已经在，AHSnipe 侧其实**没有**小地图按钮
  （`grep -rniE "minimap|LibDataBroker" AHSnipe/` = 0 命中），可对照的只有 toc 的 IconTexture 写法。
- 「王牌狩猎」91806 已删（游戏退出后手工改档，见本节末尾"已完成"）。

## 二、改名撞上的第一个硬事实：WQT 这三个字母本身是别人家的

`Api.lua:194/233/286` 与测试 `run_wqt_tests.py:3229/3277` 共 **5 处**在注释里署名外部插件
「**Tercioo WQT** / Tercioo/World-Quest-Tracker」——用户要躲开的正是这个同名项目。
**这 5 处是出处引用，一个字母都不许动**：机械替换必须带白名单，否则等于篡改"我们是跟谁学的"这条证据。
（本轮改名之后，本仓库内部再也不会出现裸的 `WQT`，注释里那几处才反而更有存在价值。）

## 三、映射表（唯一真源；两栏都是实测处数）

| 现名 | 新名 | 实测面 |
|---|---|---|
| 目录 `WorldQuestTracker/` | `GoldQuestLedger/` | `git mv`（12 个文件随目录走） |
| `WorldQuestTracker.toc` | `GoldQuestLedger.toc` | `git mv`；WoW 按"目录名/目录名.toc"加载，两者必须同名 |
| 全局命名空间 `WQT` | `GQL` | 11 个 .lua 合计 ≈901 个 token（Probe 260 / Sync 179 / Matrix 156 / Options 63 / Dump 59 / Config 48 / Track 44 / Core 41 / Ledger 36 / Api 14 / LogWindow 6） |
| 存档变量 `WQTDB` | `GQLDB` | 代码 6 处：`Config.lua:2`(注释) `:35` `:319` `:320`、`Matrix.lua:1737`(注释)、toc:7 |
| 斜杠命令 `/wqt` | `/gql` | `Core.lua:36` `SLASH_WQT1` + `:37` `SlashCmdList["WQT"]` + 代码里帮助/日志文案 + 测试 24 行 + docs 131 行 |
| 帧全局名 `WQT*` | `GQL*` | 28 个（`WQTFrame`/`WQTLogFrame`/`WQTLogEditBox`/`WQTSyncFrame`/`WQTMatrix*`×13/`WQTMapButton`/`WQTOptionsFrame`/`WQTOpt*`×3/`WQTKeywordPanel*`×6） |
| 聊天前缀 `"WQT: "`、`"WQT 命令："` | `"GQL: "`、`"GQL 命令："` | Core.lua:16/32/41/51、Dump.lua:39/44/157/169/385、Matrix.lua:1317/1323、Options.lua 里 `print("WQT: " ..` 多处 |
| UI 窗口标题 2 处 | `GoldQuestLedger …` | `Matrix.lua:1275`（"…任务×角色矩阵"）、`LogWindow.lua:29`（"…日志…"） |
| toc `## Title:` / `## SavedVariables:` | `GoldQuestLedger 世界任务金币账本` / `GQLDB` | toc:2、toc:7（`## IconTexture` toc:6 **不动**） |
| 测试脚本 `tests/run_wqt_tests.py` | `tests/run_gql_tests.py` | 含 `WQT` 的行 992、含小写 `wqt` 的行 1050、含 `WQTDB` 的行 325 |
| 替身私有前缀 `WQT_MOCK_ENV`/`WQT_LOG`/`WQT_NEWTIMERS`/`WQT_NOW`/`WQT_CURSOR`/`WQT_FOCUS`/`WQT_FireTimers`/`WQT_KNOWN_EVENTS` | `GQL_*` 同名换前缀 | runner L32–L100 一带 |
| Python 局部 `lua, wqt = load_lua(...)` | `lua, gql = ...` | 全文件（1050 行含小写 wqt） |

**替换顺序（必须照这个顺序，反过来会漏）**：
`WorldQuestTracker` → `GoldQuestLedger` ⇒ `WQTDB` → `GQLDB` ⇒ `WQT` → `GQL` ⇒ `/wqt` → `/gql` ⇒ 小写 `wqt` → `gql`。
每步都带第二节那 5 处白名单跳过（匹配 `Tercioo WQT`、`Tercioo/World-Quest-Tracker` 的行整行跳过）。

## 四、四个"改一处不改另一处就静默失效"的耦合点

1. **ESC 名单是字符串，和 `CreateFrame` 的名字参数是两处文本**：
   `Matrix.lua:1284` `RegisterEscapeClose("WQTMatrixFrame")`、`Options.lua:194` `"WQTOptionsFrame"`、
   `Options.lua:439` `"WQTKeywordPanel"`（`Config.lua:254` 是把名字塞进 `UISpecialFrames` 的实现）。
   只改 `CreateFrame` 那侧 ⇒ ESC 关窗**静默失效**，不报错。`Matrix.lua:771` 注释已写明按 `_G.` 名字反查是死路。
2. **斜杠命令两处耦合 + 一个命名规矩**：`SLASH_GQL1 = "/gql"` 的全局变量名必须恰好等于 `SLASH_` + key 大写，
   `SlashCmdList["GQL"]` 的 key 必须等于 `GQL`。改错任何一处 = 聊天框里命令直接消失。
   替身里 `run_wqt_tests.py:90-91`（`SlashCmdList = {}` / `SLASH_WQT1 = nil`）要跟着走。
3. **toc 文件名 = 目录名**；WoW 只认 `目录名/目录名.toc`。
4. **测试脚本按目录名硬路径取源码**：`run_wqt_tests.py:12` `PLUGIN_DIR = os.path.join(ROOT, "WorldQuestTracker")`、
   `:2861` / `:6535` / `:10802` 三处 `os.path.join(here, "..", "WorldQuestTracker")`；
   静态守卫 `:6553` 用正则 `\bWQTDB\b` 锁死存档全局名（**改了存档名不同步改这里，守卫会放行旧名直读、误杀新名直读**）。
   脚本不读 toc，模块名（Core/Config/…）不含插件名，文件名不用动。

## 五、存档搬迁 = 改名，不是迁移

- 老档实测形状（本轮我亲手做的删段 + 校验）：`WTF/Account/BLUEFANE/SavedVariables/WorldQuestTracker.lua`，
  顶层 12 个键 `chars / clockDomain / config / cycleAnchor / logs / manual / midnightMapIds / minimapAngle / quests / schema(=5) / stickyGoldReset / turnins`，
  `quests` 现 305 条。
- 搬迁动作就两条：文件改名 `WorldQuestTracker.lua` → `GoldQuestLedger.lua`；首行 `WQTDB = {` → `GQLDB = {`。
  **不改 schema 档位、不加 v6 迁移**——`Config.Init` 读的是全局名，名字对上了整套账本原样接着用，
  几个 CD 的金币记录与小地图角度 `db.minimapAngle`（现值 142.125…，`Matrix.lua:1749/1822`）都不丢。
- 校验口径（做完必须打印）：lupa 能 `load` 整份、`GQLDB.quests` 条数=305、`GQLDB.schema`=5、
  文件内 `WQTDB` 出现 0 次、`GQLDB` 出现 1 次（只有那一行赋值头）。
- 老文件**不删**：改名成 `WorldQuestTracker.lua.before-gql-rename` 留在原地（可回退），因为它在
  SavedVariables 目录里只是个孤儿，删了没有收益。

## 六、任务清单（串行；每个任务一个 fresh 子代理，主 agent 逐行 review 后自己提交）

### Task 1 — 代码 + 测试脚本整体改名（**一个子代理做完**，因为改名与 runner 必须同时到位才可能绿）

- [x] 1.0 前置：`git status --porcelain -- WorldQuestTracker tests/run_wqt_tests.py` 必须为空
      （共享工作区，别的会话正在改 `AHSnipe/*`、`tests/run_tests.py`、`docs/tasks/2026-10-13-ahsnipe-plan.md`——**这三个范围一律不许碰**）。
- [x] 1.1 RED：新增 `test_no_old_addon_name_left_anywhere`（静态守卫，放在 upvalue 预算守卫旁边）。
      扫 `PLUGIN_DIR` 下全部 `.lua` + `.toc`，再扫 runner 自己（`__file__`）；
      命中 `WQT`（含 `WQTDB`）或 `WorldQuestTracker` 即失败，消息里把 `文件:行号 原文` 逐条列出，最多列 20 条。
      **白名单**：整行含 `Tercioo` 的跳过（第二节那 5 处）。
      改名前这条必然红（老目录里 900+ 处），改名后应当且仅当白名单外全绿。
- [x] 1.2 RED：新增 `test_gql_slash_command_registered`：`load_lua("Core", "Config", mock=True)` 之后
      断言 `_G.SLASH_GQL1 == "/gql"` 且 `type(SlashCmdList["GQL"]) == "function"`，
      并断言 `SLASH_WQT1` 为 nil（旧命令名不留残骸）。这是第四节点 2 那条耦合的**行为门**，别只靠静态守卫。
- [x] 1.3 实现（改名，逐条做完再跑测试）：
      ① `git mv WorldQuestTracker GoldQuestLedger` + `git mv GoldQuestLedger/WorldQuestTracker.toc GoldQuestLedger/GoldQuestLedger.toc`；
      ② 按第三节映射表 + 替换顺序做机械替换，覆盖 11 个 `.lua` + `.toc` + runner；
      ③ `git mv tests/run_wqt_tests.py tests/run_gql_tests.py`；
      ④ 同步 toc:2 Title、toc:7 SavedVariables；代码里所有 `/wqt` 文案、`"WQT: "` 前缀、两处 UI 标题；
      ⑤ **不动**：toc:6 IconTexture、`Matrix.lua:1736-1834` 小地图按钮逻辑（只换名字里的 `WQT` 前缀与 `db.minimapAngle` 的**键名不变**）、
         `Config.lua` 的 schema/迁移/门槛/关键词逻辑、任何判据。**本轮是改名，业务判据一行都不许动。**
- [x] 1.4 GREEN：`python tests/run_gql_tests.py` 期望 **304/304**（302 + 新增 2）；
      `python tests/run_gql_tests.py --syntax` 全 PASS；upvalue 守卫全 PASS（改名不改闭包引用数，若这里变红说明替换把某个 local 吞了）。
- [x] 1.5 自查（写进回报，不许略过）：`grep -rn "WQT\|WorldQuestTracker" GoldQuestLedger/ tests/run_gql_tests.py`
      的**全部剩余命中**必须都在含 `Tercioo` 的行上；逐条贴出来。
- 硬停止：红掉的用例超过 5 条就停手回报；任何"要不要顺手把这条断言放松一下"的冲动都停下来报告。

### Task 2 — 存档搬迁 + 部署（**主 agent 自己做，不派子代理**：动的是玩家机器上的游戏数据与插件目录）

- [x] 2.1 `SavedVariables/WorldQuestTracker.lua` → 复制成 `GoldQuestLedger.lua` 并把 `WQTDB` → `GQLDB`；
      老文件改名 `WorldQuestTracker.lua.before-gql-rename`（**不删**）。
      搬迁前再确认一次游戏进程不在（本轮已确认 `tasklist` 无 Wow.exe）。
      搬迁后按第五节校验口径打印。
- [x] 2.2 部署：整目录复制 `GoldQuestLedger/` 到 `Interface/AddOns/GoldQuestLedger/`；
      老目录 `Interface/AddOns/WorldQuestTracker/` **移出 AddOns**到 `D:\Games\World of Warcraft\_retail_\_addon_backup\WorldQuestTracker-20261027\`
      （两份同时存在会重复建小地图按钮、重复挂 ESC 名单、`WQT = WQT or NS` 让后加载者静默让位）。
      ⚠ 这一步动的是玩家游戏目录，**做之前要点名问一次**。

### Task 3 — 文档口径（子代理；只碰 living docs + 历史计划加横幅）

- [x] 3.1 **living docs 全文改名**（含 `/wqt`→`/gql`、`WQTDB`→`GQLDB`、`run_wqt_tests.py`→`run_gql_tests.py`，带 Tercioo 白名单）：
      `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`、
      `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`（CRLF，用带 EOL 检测的编辑法）、
      `docs/wqt-weekly-quest-families.md`。
      这三份是玩家照着执行的东西，命令名和路径必须是新名，否则验收时会去敲一个已经不存在的命令。
- [x] 3.2 **历史计划正文不改**，`docs/superpowers/plans/` 下 8 份带旧名的文件各加一行横幅：
      `> 旧名横幅：本文件写成时插件还叫 WorldQuestTracker（全局 WQT / 存档 WQTDB / 命令 /wqt / 测试 run_wqt_tests.py）；
      2026-10-27 起改名 GoldQuestLedger（GQL / GQLDB / /gql / run_gql_tests.py）。逐轮决策记录按当时口径保留，不追改。`
      理由：那些是逐轮"当时实测到什么"的记录，把正文里的旧名洗成新名会让证据和现状混在一起；
      文件名同样不动（docs 里大量 `run_wqt_tests.py:9054` 式行号引用，改文件名会把交叉引用全部弄断）。
- [x] 3.3 checklist 追加 `## 30. 第三十轮：整体改名 GoldQuestLedger + 存档搬迁（2026-10-27，304/304）`：
      用户原话、二节的 Tercioo 撞名事实、映射表、四个耦合点、存档搬迁为什么不是迁移、
      代价（下面第七节）、用例账、游戏内验收清单。

## 七、代价（照实说，不粉）

1. **`/wqt` 从此失效**，一律 `/gql`；聊天里已有的旧宏/旧收藏命令要玩家自己改。
2. 老插件目录留在 AddOns 里会**双份加载**，必须移走（2.2）。移到备份目录而不是删除，是可回退。
3. 历史计划正文里的旧名（`WorldQuestTracker/` 路径、`/wqt`、`WQTDB`、`run_wqt_tests.py`）**保持原样**，
   只有横幅说明；docs 里 151 行 `WorldQuestTracker` 与 131 行 `/wqt` 中约 100+ 行落在历史计划里。
4. 存档文件改名后，如果玩家自己从别处恢复过老档，得记得老档里的全局名是 `WQTDB`，要改成 `GQLDB` 才读得进。
5. 本轮**没有**新图标：插件列表和小地图都还是那张金币 `Interface\Icons\INV_Misc_Coin_01`。

## 八、用例账

302 → 304（新增 2：旧名静态守卫、`/gql` 命令注册行为门；改写 0；删除 0）。
既有 302 条只允许出现"标识符换名"级别的改动（`WQT*` → `GQL*`、`g.WQTDB` → `g.GQLDB`、`wqt` 局部名），
**断言语义一条都不许动**；如果某条为了变绿必须改语义，停下来回报。

## 九、游戏内验收（部署后要点名回传的）

1. 插件列表里出现 **GoldQuestLedger 世界任务金币账本**，图标是金币；`WorldQuestTracker` 那条已经不在列表里。
2. 聊天 `/gql` → 打印 `GQL 命令：config | dump | log | matrix | probe | quest | sync`；`/wqt` → 暴雪"未知命令"。
3. `/gql matrix` 打开矩阵，窗口标题是"GoldQuestLedger 任务×角色矩阵"；小地图金币按钮在，位置还是拖动过的那个角度
   （存档值 142.125…，**不该复位**）。
4. 矩阵里应能看到改名前那几个 CD 的金币记录（305 条任务记录全在）；`/gql dump` 有账本内容 = 存档搬迁成功。
5. ESC 能关矩阵、设置面板、「关键词 · 屏蔽管理」三扇窗（第四节点 1 那条字符串耦合）。
6. 登录后矩阵照旧自动抓一次池（`/gql log` 有 `登录同步完成 用时N秒`）。
7. `/gql log` 窗口标题是新名。

## 十、已完成（本轮第一件事，玩家退出游戏后我亲手做的）

`quests[91806]`「王牌狩猎」整块已从存档删除：删的是 `WorldQuestTracker.lua` 第 7454–7473 行那 20 行
（**全文 16 处 `91806` 里唯一含 `["name"] = "王牌狩猎"` 的那一处**）。其余 15 处一个没动，实测分布：
`manual` 13 处（各角色手记"这一号交过"）+ `turnins` 1 处（交任务那笔金币历史）+ `chars` 1 处（角色视图）。
校验：lupa 载入整份 OK、`quests` 306 → 305、逐顶层键深比对**只有 `quests` 变了**、
`王牌狩猎` 出现次数 1 → 0、`91806` 16 → 15、`schema` 仍是 5、`minimapAngle` 仍是 142.125…。
备份 `WorldQuestTracker.lua.before-drop-91806` 留在原地。
下次它上板会被快照自动收回，按当轮金币重新判定。

> **口径纠正**：checklist §28 的验收第 6 条当时写的是"`91806` 从 14 次变 13 次"——那是按更早一份存档数出来的，
> 删段前文件已被游戏重写过多轮，实测是 **16 → 15**，判据请按实测数走（已同步改掉 §28 那一行）。

## 十一、执行记录与计划纠错（主 agent review 后回写）

三个任务都已落地：代码 + 测试脚本改名 = 提交 `c9e73b8`；文档口径 = 提交 `705c8e2`；
存档搬迁 + 部署 = 2026-10-27 玩家确认"我一次做完"后由主 agent 亲自做完（第六节 Task 2 那两条勾上）。

**review 方法（改名这种千行机械 diff 逐行看没用）**：把改名前的每个旧文件按同一套映射**自己重放一遍**，
与子代理的结果逐字节比 ⇒ 12 个插件文件（含 toc）**完全等于纯换名**，runner 只多 49 行 / 少 0 行
（两条新用例 + 2 行注册）。提交后又对 `HEAD~1` 的 blob 重放校验一次，结论相同。我自己跑也是 304/304。

计划里写错 / 被子代理纠正的三处：

1. **简报里"`WQT` 的行 992、含小写 `wqt` 的行 1050"是行数口径，子代理按 `-o` 数出的是 token 口径（2053/1128）**
   —— 两边都对，只是不同量；不影响执行。
2. **第 4 节"三处 `os.path.join`"实测是四处**（L12 `PLUGIN_DIR` + L2861 + L6535 + L10802），
   checklist §30 耦合点 4 那一句按"三处"写的是**错的**，已随本节一并纠正为"四处"。
3. **6 个 .lua 在改前就是 CRLF**（Api/Dump/Matrix/Probe/Sync/Track），任务简报说".lua 一律 LF"不准；
   子代理按文件实测字节处理，行尾原样保留。附带事实：仓库 `core.autocrlf=true`，磁盘行尾不影响提交的
   blob（HEAD 里这些文件本来就是 LF），所以改名后 `git diff` 不会出现整文件重排。

两处**必要的偏离**（子代理提出，我认可并已核）：

- 新用例里的旧名匹配串写成拆开拼接（`"WQ" + "T"`、`"SLASH_W" + "QT1"`）：整串会被换名脚本自己吞掉，
  守卫就变成扫 `GQL`；断言"旧命令全局名为 nil"若写成整串也会被换成断言新名，那条门当场失效。
- 三次 `git mv` 一次做完再替换（替换目标路径必须先就位），覆盖文件集合与计划一致。

`grep -rn "WQT\|WorldQuestTracker" GoldQuestLedger/ tests/run_gql_tests.py` 改名后仅剩 4 处大写命中 +
`Api.lua:233` 的 `Tercioo/World-Quest-Tracker`（带连字符，不匹配该 grep），全部在含 `Tercioo` 的署名行上。
