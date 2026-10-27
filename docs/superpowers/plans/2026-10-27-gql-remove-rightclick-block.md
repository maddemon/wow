# GoldQuestLedger 第 31 轮计划：移除矩阵右键屏蔽 + 恢复误屏蔽的那一行

计划唯一真源。分支 master，改名后的树（`GoldQuestLedger/` + `tests/run_gql_tests.py`，基线 304/304）。

## 一、用户原话（整段引，别改写）

> 矩阵右键忽略的功能没有移除？ 我刚才不小心右键忽略掉了一行任务

选项式确认后的决定（原话）：

> 1、移除右键屏蔽功能 2、刚才我屏蔽的任务帮我恢复

明确**没选**的：不加"已屏蔽"列表、不做 Alt+右键、不改提示文案、**不恢复确认框**（第 28 轮撤掉弹窗是他点名的，本轮不许加回来）。

## 二、现状（实测，别凭记忆）

屏蔽一条任务在代码里只有一条路：`Track.RemoveQuest(questID)` → `Ledger.RemoveQuest` 记 `rec.ignored = true`（永久否决，五道入门门都不放行）。**这一层本轮一个字都不动。**

动的是**调用方**。当前有两个 UI 入口：

1. `Matrix.lua:337-350` —— 左窗格整行热区按钮的 `OnClick`：右键 → `GQL.Track.RemoveQuest(self.__questID)`，当场生效、无确认框（第 28 轮把 `pcall(RegisterForClicks, ... "RightButtonUp")` 挂上，本轮整块下线）。
2. `Options.lua:574-584` `PanelToggleQuest` —— 「关键词 · 屏蔽管理」右栏那颗按钮（`Matrix.lua:1300-1303` 的「添加任务」打开的窗口）。**这个入口保留**，屏蔽/找回今后只在这里发生。

他踩中的是入口 1：手滑右键就把一行永久屏蔽了。恢复入口是关键词面板，而**恢复范围被关键词圈住**（`Options.KeywordMatches` 只列"命中某个已有关键词"的任务），所以名字不撞任何关键词的行没有界面恢复路——这是他上一轮已经认过的代价，本轮按他新口径把入口 1 直接拆掉。

`Matrix.lua` 里没有任何"右键可屏蔽"的玩家可见提示文案（`grep -n "屏蔽\|移除" Matrix.lua` = 只有注释，无 `SetText`/提示串），所以删除不牵连文案。

## 三、要改的清单（逐条给 file:line，实施者照单执行）

### A. 代码（唯一一处行为变更）

删 `GoldQuestLedger/Matrix.lua:337-350` 整个 `if not b.__gqlHooked then ... end` 块，连同 `b.__gqlHooked` 这个守卫标志——**不留空 OnClick、不留 RegisterForClicks**（他不要这个功能，就不要留一个"注册了右键但什么都不做"的半吊子）。

必须留下的（别顺手删）：
- `Matrix.lua:336` `b.__questID = questID`：金额悬停提示靠它（`Matrix.lua:582-584` 的 `ShowMoneyTooltip(btn, btn.__questID, btn.__charKey)`，行热区和状态格共用）。
- `Matrix.lua:329` `b:EnableMouse(true)`：整行高亮靠每帧 `IsMouseOver` 扫描（`Matrix.lua:663-668` `HoverTarget`），关掉鼠标热区高亮和提示一起瞎。
- `Matrix.lua:351` `b:SetScript("OnMouseWheel", OnMouseWheel)`：滚轮在行上也要能滚。

`Track.RemoveQuest` / `Ledger.RemoveQuest` / `Track.AddQuest` 一律不动——面板还在调它们。

### B. 注释改口（"右键 = 屏蔽"这个说法在代码里已经成假，留着会误导下一个改代码的人）

把说"右键屏蔽/右键移除"的注释换成"在「关键词 · 屏蔽管理」里点忽略"：

| 文件:行 | 现说法 | 处理 |
|---|---|---|
| `Config.lua:12` | `ignored=<玩家右键"移除"过 …>` | 改口：`<玩家在"关键词 · 屏蔽管理"点"忽略" = 永久屏蔽，只有点"取消忽略"才撤销>` |
| `Track.lua:149` | `被矩阵里"右键任务名"调用` | 改口：被「关键词 · 屏蔽管理」右栏的"忽略"按钮调用 |
| `Sync.lua:221` | `玩家右键屏蔽过的不再收` | 改口：玩家屏蔽过的（`rec.ignored`）不再收 |
| `Dump.lua:365` | `只能右键移除` | 改口：只能在屏蔽管理窗口点"忽略"屏蔽 |
| `Ledger.lua:101/134/1139/1611/1687/1689` | 六处"右键移除/右键点名/右键'移除'" | 逐处改口，语义不变（永久否决、可再点回来） |
| `Ledger.lua:914` | `（12.1.5 实测反馈："右键点击后还会刷出来"）` | **实测引语原样保留**，只把"改成记一条…"那句的入口说法带正 |
| `Options.lua:571` | `与矩阵右键同一条路` | 改口：这条就是唯一入口（矩阵右键已下线） |
| `Matrix.lua:1300-1303` | 「添加任务」= 找回被屏蔽的行的唯一入口 | 补一句：屏蔽也在这里发生，矩阵右键 2026-10-27 起不再屏蔽任何东西 |

**不许动的"右键"**（说的是原生地图 pin，跟矩阵无关）：`Track.lua:3`、`Track.lua:41`（`地图右键=返回上一级、Ctrl+左键=定位标记，都被原生占用`）、`Options.lua:377`（`地图右键 pin（Shift+左键）装了画环插件就钩不到`）、`Matrix.lua:1300` 括号里"地图右键 pin 不生效时也能用"这半句。

### C. 测试（`tests/run_gql_tests.py`，304 → 预计 305）

先 RED，后实现。既有断言**只许改入口、不许放宽语义**。

1. 改写 `test_right_click_ignores_without_a_confirm_dialog`（7890）→ `test_matrix_right_click_blocks_nothing`：
   - 前提：该行本来在矩阵里（`matrix_listed`）。
   - 断言一（行为门）：行热区按钮上**没有** `__scripts["OnClick"]`，且没注册过 `RightButtonUp`（替身把 `RegisterForClicks` 记在帧上，直接读那个记录）。
   - 断言二（数据门）：对该 questID 模拟一次右键后（找不到 OnClick 就是"什么都没发生"），`GQLDB.quests[qid].ignored` 仍是 nil、该行仍在矩阵里、聊天没有"已永久屏蔽"。
   - 断言三：`GQLRemoveQuestDialog` 全局帧仍不许存在（确认框下线的旧口径继续钉）。
2. 改写 `test_right_click_remove_survives_resync`（7926）→ `test_panel_block_survives_resync_and_is_reversible`：
   里层四条口径一条都不删（同步不刷回、不在池也要能从账本找回、"取消忽略"撤销并回矩阵、`AddQuest` 不强制上板 + `manualAdded` 已下线），
   **只把造成屏蔽那一步换成点面板右栏的"忽略"按钮**（先 `Config.AddKeyword("任务190")` → `Options.OpenKeywordPanel()` → `select_panel_keyword` → `click_panel_ignore_button(lua, 190)`，全程真实 OnClick，不直调 `Track.RemoveQuest`）。
3. 改写 `test_removed_keyword_log_quest_does_not_come_back`（8022）：该测试 8037 行的 `right_click_matrix_row(lua, 9011)` 换成面板那一路（关键词"萨拉塔斯"该测试已经加过，直接开面板点忽略）。数据层断言（`logAdded == 0`、`kwLogAdded` 不许被写回）不动。
4. 删掉辅助函数 `right_click_matrix_row`（7761）——三个调用点全部改写后它没有用户，留着等于留一个"以为还能右键屏蔽"的把手。
5. 新增 `test_matrix_has_no_block_entry`（静态守卫）：扫插件全部 `.lua`，`Track.RemoveQuest` 的**调用**只允许出现在 `Options.lua`（`Ledger.lua`/`Track.lua` 里的定义行、测试自己的引用不算）；`Matrix.lua` 里出现 `RemoveQuest` 即失败。理由：这一轮的验收物是"入口少了一个"，只钉行为用例的话下一个人在 Matrix 里再挂一颗右键按钮没人拦。
6. 既有的 `test_panel_ignore_button_marks_permanently`（6108）与 `test_panel_unignore_button_clears_the_mark`（6146）口径**一个字不改**——它们钉的正是保留下来的那个入口。
7. 静态守卫 `test_no_old_addon_name_left_anywhere`、`--syntax`、`test_lua_upvalue_budget`（≤60 upvalues/闭包）都要照旧全绿；改名轮那两个新用例与本窗口无冲突，不动。

账：304 → 305（新增 1，改写 3，删除 0）。执行：
`python tests/run_gql_tests.py -k matrix,panel,right_click,block` 先跑红的，再全量 `python tests/run_gql_tests.py` + `python tests/run_gql_tests.py --syntax`（最后一行是 GBK，用 `2>&1 | tail -2 | iconv -f GBK -t UTF-8`，红绿用 `grep -a "^FAIL"` 判）。超过 5 条红停下汇报，不许放宽断言凑绿。

### D. 文档

- `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 追加 §31（CRLF 文件，别把整份换行写坏）：用户原话、为什么撤（手滑误屏蔽 + 恢复被关键词圈住）、撤了什么留了什么、数据层不动、代价、用例账 304→305。
- `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`：L85 `ignored` 字段说明去掉"右键"、L190"行热区右键 = 当场永久屏蔽"整条改写成"矩阵不提供屏蔽入口，屏蔽与找回只在两栏「关键词 · 屏蔽管理」窗口"。
- `docs/wqt-weekly-quest-families.md`：L204 标题里的"（右键移除）"、L216-218 那段、L225 的"要他们走就右键那一行 → 确定"改成面板那一路（`L.RemoveQuest` 记 `ignored` 的语义与用例名照旧引用，用例改名要同步）。
- 历史计划/§28 正文里的旧说法**保持原样**，只在 §31 里说明被本轮取代——逐轮记录要能看出演变。

## 四、Task 2：恢复他手滑屏蔽的行（**已按实测改道，没改档**）

原计划是"等他完全退出游戏 → 备份存档 → 列出全部 `ignored` 行 → 只清这一笔 → lupa 校验"。
实际执行时他在 11:37 自己 `/reload` 了一次（`Wow.exe` 仍开着，PID 38332），刷盘后读档实测：

| 存档 | schema | quests | ignored 行 |
|---|---|---|---|
| `WorldQuestTracker.lua.before-gql-rename`（改名前） | 5 | 305 | **0** |
| `GoldQuestLedger.lua.bak`（11:31 那次刷盘） | 5 | 305 | **3** |
| `GoldQuestLedger.lua`（11:37 那次刷盘） | 5 | 305 | **3** |

三行逐条读过（编号 / 名字 / 金币 / 是否命中已有关键词）：
- `95429 健步如飞`，`lastMoney` = 10729459（1072 金），**不撞任何关键词** → 界面里没有它的恢复入口，正是本轮代价第 1 条。
- `91193 特别任务解锁：顶点1 - 解锁`，命中关键词「特别任务」→ 窗口里就能点回来。
- `97625 辉煌丰饶`，命中关键词「丰饶」→ 同上。

结论与做法：条数比他口述的"一行"多，所以**一条都没替他决定**，三条连名字带编号报给他，让他自己认领。
恢复没动存档 —— 客户端还在跑时改档会被内存里的旧账覆盖回写（等于白改），而插件自己有这条路：
`/run GQL.Track.AddQuest(<编号>)` → `Ledger.AddQuest` 的 `rec.ignored = nil`（`Ledger.lua:898`），
与窗口那颗"取消忽略"是同一条代码路，当场回执 + 当场重画矩阵，下次 `/reload` 或退游戏自然刷盘。
命令是 26 个字符，粘得进聊天框（255 字上限那条限制在这里不构成问题）。

**规矩写在这里**：改玩家存档是最后兜底，能用插件自己的入口或一行 `/run` 就别动档；真要动，仍是
第四节原来那套（备份成 `.before-*`、只清那一笔、lupa 重载校验 schema/条数/逐行 diff）。

（`/gql quest <ID>` 是诊断用的"这行为什么还在列"，不碰屏蔽标记，不能当恢复入口 —— 本轮查过命令表才敢说。）

## 五、代价（照实说，不粉）

1. **非关键词命中的行今后没法屏蔽**：面板右栏只列"命中已有关键词"的任务（`Options.KeywordMatches`）。
   一条从池子直接上板、名字不撞任何关键词的任务，本轮之后没有 UI 入口能把它藏掉。
   他 2026-10-26 已认过这个限制（原话"点击/输 ID 已作废"），本轮是把制造屏蔽的入口撤掉，等于把代价坐实。
2. 已经屏蔽掉的行（含他这一条）恢复仍只有面板那一路；Task 2 手工改档是给他兜底，不是常规手段。
3. 手滑风险从此没有（这正是本轮目的）；反面是**想屏蔽要点两下**（打开窗口 → 选关键词 → 点"忽略"）。
4. 右键在矩阵里变成彻底的空白操作：不屏蔽、不弹框、不切选中，什么都不发生。

## 六、游戏内验收（部署后要点名回传的）

1. 矩阵里对任意一行**右键** → 该行不消失、聊天无"已永久屏蔽"、`/gql dump` 里这条没有 `ignored`。
2. 点「添加任务」打开「关键词 · 屏蔽管理」→ 选关键词 → 右栏点"忽略"：当场从矩阵消失、按钮翻成"取消忽略"、聊天报"已永久屏蔽"。
3. 同一个按钮点"取消忽略" → 回聊天提示 + 该行回矩阵（前提：仍命中关键词、金币过门槛）。
4. 他手滑那条任务重新出现在矩阵里，金币记录还在（没被清掉）。
5. 悬停行高亮 + 金币提示照旧（删 OnClick 不许把 hover 一起带走）。
6. 滚轮在左窗格行上仍能滚整栏。

## 七、执行记录（实施后回写）

- [x] Task 1（代码 + 注释 + 测试 + 文档）——代码子代理 + 文档子代理并行，主 agent 逐行 review、自己跑测试、自己提交。
- [x] Task 2（恢复被屏蔽的行）——按第四节改道，**没改存档**，给的是一行聊天命令。

实测数字（主 agent 自己跑，不看子代理结论）：全量 `305/305 单测通过`、`--syntax 305/305`、
`upvalues` 全 PASS（含改过的 `Matrix.lua`/`Options.lua`/`Ledger.lua`）。

子代理两处偏离，我的判定：
1. `Ledger.lua:1689` 的"右键"没改 —— **接受**。那一处唯一含"右键"的是玩家实测引语
   `"删不掉，右键点击后还会刷出来"`，与 914 被明令原样保留的是同一句；逐轮证据不许洗。作者自己的表述那半句（1687）已改口。
2. 替身新增 `RegisterForClicks` 记录（`__clicks`，按真客户端语义整组替换而非追加）—— **接受**：
   `Button:RegisterForClicks` 是真客户端方法，不属于"凭空造客户端没有的方法"那条纪律；
   用例①「也从没注册过 RightButtonUp」必须有它才钉得住。`click_matrix_row_right` 走 `:Click("RightButton")`，
   没有 OnClick 时就是"什么都没发生"，正是本轮要的行为，注释里也写了"不许改成先确认有 OnClick 再点"。

计划自带的一处口径错误，实测纠正：第三节 B 说「`Matrix.lua` 里没有任何玩家可见的右键文案」，
但 `Dump.lua:365` 那句 `只能右键移除` 是 `/gql quest` 的**打印文本**（不是注释），
本轮一并改成"只能在「关键词 · 屏蔽管理」点「忽略」屏蔽"——它有静态守卫之外没有用例钉，属文案改动，照实记在这里。

部署状态：**尚未覆盖到 `Interface/AddOns/GoldQuestLedger/`**（客户端正在跑，右键屏蔽在热更前仍然有效；
等他点头再整目录覆盖 + `/reload`）。
