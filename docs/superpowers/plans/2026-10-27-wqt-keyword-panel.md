# WorldQuestTracker：右键免确认 + 关键词/屏蔽两栏窗口 + 设置面板瘦身——实施计划

> **For agentic workers:** 逐任务实施，每个改动步骤先跑 `python tests/run_wqt_tests.py -k <族>` 看到 RED，再实现，再跑到全绿；收尾必须回到全量 `python tests/run_wqt_tests.py` + `--syntax`。步骤用 `- [ ]` 勾选。

**Goal:** 四条玩家口径一次做完：① 右键"移除"不再弹确认框，当场生效；② 矩阵"添加任务"那个弹框改成**两栏窗口**——左栏关键词列表（仍可加/删/补扫），选中一个关键词，右栏列出它命中的任务，每行带一个「忽略 / 取消忽略」按钮；③ 从旧存档里删掉「王牌狩猎」(questID 91806) 这条任务记录，下次它上板时按新数据自动重建；④ 设置面板只留"跟踪地图 + 登录抓池"，金币门槛那段和大堆说明文字全部撤下。

**Architecture:** 分层不动，改动集中在两个 UI 文件 + 用例 + 文档：
`Matrix.lua`（右键直接屏蔽、确认框整块删除、旧弹框 `OpenAddDialog`/`RefreshAddList`/`dlgAdd`/`Mx.listed` 下线、"添加任务"按钮改调 `WQT.Options.OpenKeywordPanel()`）、
`Options.lua`（设置面板瘦身 + 新增两栏窗口，数据一律走既有入口：`Config.GetKeywords/AddKeyword/RemoveKeyword`、`Sync.UnregisterKeyword/HuntKeywordMaps`、`Track.AddQuest/RemoveQuest`）、
`tests/run_wqt_tests.py`（改写 8 条、删除 2 条、新增 5 条）、
`docs/`（设计文档 §文件职责、验收清单 §28）。
**数据层一行不改**：`Ledger.AddQuest/RemoveQuest`、五道准入门、`ignored` 的永久语义、`db.config.minGold` 的存取全部照旧——本轮只动界面和一份存档数据。

**Tech Stack:** WoW 12.1.5 内嵌 Lua（5.1 语义）、SavedVariables `WQTDB`、离线测试 Python 3 + lupa。

---

## 一、用户口径（原话，不要改写）

> 1、取消右键屏蔽任务弹窗提醒
> 2、添加任务窗口 改为 左侧是关键词列表， 选中关键词，右侧是命中的任务名称列表，该列表可以点按钮 忽略 或 取消忽略
> 3、删除旧数据中的 王牌狩猎 任务 等下次出现时 他应该会自动被发现 加入吧
> 4、设置里 关键词部分移除掉， 其他大堆的说明文字也去掉，二和三也去掉 暂时不能配置

追问后的两条补充（同样算口径）：

- 关键词**还能继续添加或删除**——"设置里移除掉"是指这个入口从设置面板挪走，不是把功能废了。落点：新窗口左栏（它本来就是关键词列表）。
- **不许动数据**："地图也是同理，你只是改 UI，为什么要动数据呢？只是第三条让你删除一个任务数据。" → 设置面板删段落 = 只删控件；`db.config.keywords` / `extraMaps` / `minGold` 一个字段都不清；唯一允许动的数据是第 3 条点名的 91806。

## 二、最终形态

### 2.1 新窗口「关键词 · 屏蔽管理」（矩阵"添加任务"按钮打开）

```
┌─ 关键词 · 屏蔽管理 ──────────────────────────────────────── ✕ ┐
│ 关键词          命中 │ 「丰饶」命中的任务                       │
│ ────────────────────┼────────────────────────────────────── │
│ ▶ 特别任务      9    │ 辉煌丰饶                  0g  [ 忽略 ]  │
│   符文石        4    │ 丰饶：草药洞穴          320g  [ 忽略 ]  │
│ ▶ 斯托玛兰      1    │ 丰饶供品                  ?  [取消忽略] │
│   追踪萨拉塔斯  2    │ （点"忽略"= 永久屏蔽，换 CD、换轮都不回） │
│   亚基          3    │                                        │
│ ▶ 丰饶          3    │                                        │
│   虚空决战      0    │                                        │
│ ────────────────────┤                                        │
│ [____________][添加] │                                        │
│ [   补扫跟踪地图   ] │                                        │
└──────────────────────────────────────────────────────────────┘
```

- 帧名 `WQTKeywordPanel`，父帧 **UIParent**（不再挂矩阵主帧下面：弹框是矩阵子帧那条"点亮背后那行"的特判因此可以整块删掉，见 3.1）。挂 ESC 名单（`Config.RegisterEscapeClose`）。
- 左栏：一行一个关键词，行文字 = `关键词  命中数`；点行 = 选中（选中行换色，不弹任何窗）；行尾「删」按钮 = `Config.RemoveKeyword` + `Sync.UnregisterKeyword`（与今天设置面板那一路完全同口径）；栏底 = EditBox + 「添加」按钮 + 「补扫跟踪地图」按钮（三个控件都是从设置面板整段搬过来的，行为不改）。
- 右栏：选中关键词的命中任务，一行 = `任务名  金额  [忽略 / 取消忽略]`。按钮文字按 `rec.ignored` 决定：屏蔽过 → 「取消忽略」，其余 → 「忽略」。点按钮只走 `Track.AddQuest` / `Track.RemoveQuest`，别的什么都不做。
- 金额问不出显示 `?`（**不许**显示"非金币"——这条是 12.1.5 撞过的，旧用例 `add_dialog_shows_unknown_money_as_question` 要平移到新窗口继续钉住）。
- 没有命中时右栏一行灰字：`（没有命中：这个名字不在跟踪地图的板上，也不在账本里）`——就这一句，不写第二段解释。

### 2.2 设置面板瘦身后只剩

```
┌─ 世界任务追踪 · 设置 ────────────────────────── ✕ ┐
│ 跟踪地图（默认=本资料片区域）                      │
│   永歌森林  (默认)                [ 默认 ]         │
│   …                                              │
│   我的图                          [ 移除 ]         │
│   [ ＋添加当前所在地图 ]                            │
│   [ 地图名_______ ][ 按名称添加 ]                   │
│ 登录时抓一次世界任务池                             │
│   ☑ 登录时同步                                     │
└──────────────────────────────────────────────────┘
```

- **删**：`二、关键词` 整段（sec2 / kwRows / kwHint / kwEdit / addKw / huntBtn）、`三、金币门槛` 整段（sec3 / goldEdit / goldBtn / goldHint）、面板内的「刷新」按钮（矩阵标题栏早有同一入口，用例 `matrix_refresh_button_sits_by_settings_and_resyncs` 已经钉住）。
- **删**：所有大段说明文字（`kwHint` 那 6 行、`goldHint` 那句"本轮有角色交过的任务不看门槛…"）。
- **改**：段标题去掉"一、""二点五、"这种编号（只剩两段，编号没有信息量）。
- **保留原样**：跟踪地图那一段的每一个控件和行为（默认图列表、额外图"移除"、＋添加当前所在地图、按名称添加）、登录同步勾选、滚动条与滚轮方向、不透明深灰底。
- 金币门槛：UI 撤下，**数据与判据一律不动**——`db.config.minGold` 仍是唯一真源，`Config.MinMatrixMoney()`/`GetMinGold()`/`SetMinGold()` 三个函数都留着（门槛"暂时不能配置"，`SetMinGold` 是这张配置表唯一的合法写点与区间校验，删它等于把 0–100000 金那套 clamp 的用例一起删掉）。矩阵、`/wqt quest`、`/wqt probe` 报的还是同一个数，取证口径不断链。

## 三、影响面盘点（同类的一起解决，不留半截）

| # | 落点 | 现在 | 改完 |
|---|---|---|---|
| 3.1 | `Matrix.lua:342-346` 行热区 OnClick | 右键 → `Mx.ConfirmRemove` | 右键 → `WQT.Track.RemoveQuest` 当场屏蔽（聊天仍报"已永久屏蔽 …要恢复点矩阵的\"添加任务\""） |
| 3.2 | `Matrix.lua:1832-1889` `RemoveTip` + `Mx.ConfirmRemove` + `dlgRm`（声明在 :36） | 确认框 | **整块删除**，不留"以后可能还要"的壳 |
| 3.3 | `Matrix.lua:644-653` `FocusOnMatrix` 里 `node == dlgAdd or node == dlgRm` 特判 | 弹框是矩阵子帧，要手动判外部 | 特判整句删掉：新窗口是 UIParent 的子帧，向上走撞不到 `frame`，天然判外部 |
| 3.4 | `Matrix.lua:1658-1830` `OpenAddDialog`/`RefreshAddList`/`dlgAdd`、`Mx.listed`（:1446/:1570/:1715） | 全池平铺、点一行=撤销屏蔽 | **下线**；按钮 `OnClick` 改调 `WQT.Options.OpenKeywordPanel()`。`Mx.listed` 只服务于旧"矩阵已有"标签，跟着删 |
| 3.5 | `Options.lua:174-455` 各段 | 四段 + 大堆文字 | 见 2.2 |
| 3.6 | `Options.lua:53-64` `KeywordHits` | 只数账本命中 | 扩展成 `Options.KeywordMatches(kw)`：账本记录 ∪ `Api.GetPool()` 命中的任务（按 questID 去重，名字统一走 `Ledger.QuestName`，大小写不敏感子串，同 `Config.KeywordHit` 的 `find(low,1,true)` 口径），返回 `{ {questID,name,money,ignored}, ... }`；左栏那个数 = `#KeywordMatches(kw)` |
| 3.7 | `Track.lua:160` 文案 | "要恢复点矩阵的\"添加任务\"" | 保留：按钮名没改，指路仍然对 |
| 3.8 | `Ledger`/`Sync`/`Dump`/`Probe`/`Config` | — | **零改动**（永久屏蔽语义、五道准入门、`kwLogAdded` 退注册、`/wqt quest` 取证全部照旧） |
| 3.9 | 存档数据 | `quests[91806]`（名=王牌狩猎）赖在账本里 | 手工删那一段（见第五节），`chars.views` / `turnins` / `manual` 里 91806 的**金币历史一律留着** |

## 四、任务清单

### Task 1 — 右键直接屏蔽，确认框整块下线

**Files:** `WorldQuestTracker/Matrix.lua`（:36、:342-346、:644-653、:1832-1889）

- [ ] 1.1 RED：新增 `test_right_click_ignores_without_a_confirm_dialog`——矩阵里右键一行（走 `right_click_matrix_row` 那条真实 OnClick），断言：当场 `WQTDB.quests[qid].ignored == true`、该行掉出矩阵、聊天报"已永久屏蔽"、**`WQTRemoveQuestDialog` 这个全局始终不存在**（旧用例里所有 `frame_texts(g.WQTRemoveQuestDialog)` 的读法反过来用：`g.WQTRemoveQuestDialog == nil`）。
- [ ] 1.2 RED：改写 `test_right_click_remove_survives_resync`（:7486）与 `test_removed_keyword_log_quest_does_not_come_back`（:7586）——去掉"弹确认框 / 点取消 / 点确定"三步（含 `确定不再显示「任务190」`、`永久屏蔽` 文案断言、`click_dialog_button(..., "取消")` 那段"点取消不该真动这一行"），改成右键即屏蔽；恢复那一路改走新窗口（见 3.3 的 `click_panel_ignore_button`），前置要先 `Config.AddKeyword("任务")` 才命中得到。
- [ ] 1.3 实现：`Matrix.lua:342-346` 的 OnClick 直接调 `WQT.Track.RemoveQuest(self.__questID)`（保留 `WQT.Track` 判空）；删 `RemoveTip`、`Mx.ConfirmRemove`、`dlgRm` 声明与 :649 那半句特判。
- [ ] 1.4 GREEN：`-k right_click,removed_keyword_log,ignore`。

### Task 2 — 设置面板瘦身

**Files:** `WorldQuestTracker/Options.lua`

- [ ] 2.1 RED：改写 `test_options_gold_threshold_control_writes_config`（:6116）→ 反向断言设置面板里**不再出现**"金币门槛""三、""现在 = "；门槛数据仍照旧生效（`Config.SetMinGold("100")` 后 `MatrixRows` 的行数变化那部分断言保留，走数据层）。删除 `test_options_refresh_button_resnaps`（:5981，矩阵那入口已有用例钉住）。
- [ ] 2.2 RED：`test_options_shows_keyword_hit_count`（:6058）搬家成 `test_panel_shows_keyword_hit_count`（读 `WQTKeywordPanel`）：命中 1 与命中 0 的关键词都要在左栏报出数——这条继续钉住"用户分不清是关键词写错还是图没跟踪"那个实测困惑，只是从段落说明改成行尾一个数。
- [ ] 2.3 实现：按 2.2 节删控件与文案、去编号；`Options.Refresh()` 里对应排版整段撤掉，`Build()` 里 `sec2/sec3/kwEdit/addKw/kwHint/huntBtn/goldEdit/goldBtn/goldHint/refreshBtn/kwRows` 的创建全删（连同 :27 的 local 声明）。
- [ ] 2.4 GREEN：`-k options`。**注意**：`test_options_wheel_matches_matrix_direction`（:10584）、`test_windows_are_opaque`、`test_escape_closes_matrix_and_options` 不许改口径——面板仍有滚动条、仍不透明、仍挂 ESC。

### Task 3 — 两栏窗口「关键词 · 屏蔽管理」

**Files:** `WorldQuestTracker/Options.lua`（新增 `Options.OpenKeywordPanel`/`RefreshKeywordPanel`）、`WorldQuestTracker/Matrix.lua`（按钮接线）

- [ ] 3.1 契约先定死（两个文件都按这个签名写，不许各自发挥）：
      `WQT.Options.OpenKeywordPanel()` 懒建帧 + 重画 + `Show`；全局帧名 `WQTKeywordPanel`；父帧 `UIParent`；`SetFrameStrata("DIALOG")`。
      选中态存在帧上：`panel.__kw`。重画入口 `WQT.Options.RefreshKeywordPanel()`（左栏、右栏、命中数一次刷完，点完按钮/加删关键词后都调它）。
- [ ] 3.2 RED（先写用例再实现，逐条挂进 `tests` 列表）：
      - `test_panel_lists_only_the_selected_keyword_matches`：左栏点「丰饶」→ 右栏只出现名字含"丰饶"的行，池里不含那个词的对照任务不许出现；改点「亚基」→ 右栏整栏换掉（钉住"复用行池不 Show 就既看不见也点不着"那个老坑，`test_add_dialog_reused_rows_are_shown` 的断言平移进来）。
      - `test_panel_ignore_button_marks_permanently`：右栏点「忽略」→ `ignored=true`、`kwLogAdded` 被清、矩阵该行消失、按钮文字当场变「取消忽略」。
      - `test_panel_unignore_button_clears_the_mark`：接着点「取消忽略」→ `ignored==nil`、聊天报"已取消忽略"、行按自动判据回不回都照实（关键词仍命中就得回来）。
      - `test_panel_can_add_and_remove_keyword`：输入框 + 「添加」新增关键词并出现在左栏；「删」掉一个关键词后 `Sync.UnregisterKeyword` 那一路把由它注册的周常行退掉（口径照今天设置面板那条，用例从 :5765/:5818 的语义搬过来，不新造规则）。
      - `test_panel_hunt_button_survives_missing_sync`（:10619 搬家）：`WQT.Sync` 没加载时点「补扫跟踪地图」不许抛错，只报一句降级。
      - `test_hover_stays_off_while_the_panel_is_open`（:6484 改写）：鼠标几何压在矩阵行上、`GetMouseFocus` 落在新窗口里 → 高亮整条跳过。
- [ ] 3.3 测试侧辅助函数（替身帧只翻一层，两栏窗口的行埋在 scroll→content 底下，见 :5851 那段注释的教训）：
      `select_panel_keyword(lua, text)`、`click_panel_ignore_button(lua, qid)`、`panel_right_rows(lua)`——一律走真实 `__scripts['OnClick']`，不许直调内部渲染函数。
- [ ] 3.4 实现：布局照 2.1 节；行池复用（左栏池 `kwPool`、右栏池 `questPool`，末尾没用上的 Hide、复用的必 Show）；滚轮方向与矩阵/设置一致（`offY - delta*STEP`，同一个 `OnWheel`）；两栏各自的滚动区，右栏内容高变化时同步 `SetMinMaxValues`。
      数据只读不写：命中集合走 3.6 的 `Options.KeywordMatches`，按钮只调 `Track.*`。
- [ ] 3.5 矩阵接线：`Matrix.lua:1298-1302` 的 `addBtn` 文字保持「添加任务」，`OnClick` 改 `WQT.Options.OpenKeywordPanel()`；按 3.4 行删掉 `OpenAddDialog`/`RefreshAddList`/`dlgAdd`/`Mx.listed`。
- [ ] 3.6 GREEN：`-k panel,hover,options,add_dialog`，再全量。

### Task 4 — 文档口径跟上

- [ ] 4.1 `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`：§文件职责里 `Options.lua` 那句（:183）改成"设置面板（跟踪地图、登录同步）+ 关键词/屏蔽管理窗口"；§可配项（:150-151）补一句金币门槛本轮撤下 UI、数据与判据不动。
- [ ] 4.2 `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 追加 `## 28. 第二十八轮：右键免确认 + 关键词/屏蔽两栏窗口 + 设置面板瘦身（2026-10-27，N/N）`：写清四条口径、右键不再弹框、门槛暂时不能配置（默认 500 金照旧生效）、关键词入口搬家（还能加删）、存档删了 91806 什么、代价照实（见第六节）。旧段落里"弹一个确认框"（:85）与"设置面板加三、金币门槛"（:413）各补一行"(2026-10-27 本轮撤下)"，不重写历史。
- [ ] 4.3 `docs/wqt-weekly-quest-families.md` 里提到"右键移除/添加弹框"的口径（:206、:208）改成新窗口的按钮说法。

### Task 5 — 存档数据：删掉「王牌狩猎」91806（人工确认后再动）

见第五节。**这一条要游戏完全退出后才做**，且要玩家点头才写文件。

## 五、存档操作细则（Task 5）

- 文件：`D:\Games\World of Warcraft\_retail_\WTF\Account\BLUEFANE\SavedVariables\WorldQuestTracker.lua`（顶层就是 `WQTDB = {`）。
- 目标：`["quests"]`（从 :4655 起）里 `[91806] = { ... }` 这一段 = **:7331–:7350**（`["name"] = "王牌狩猎"`、`currentSig="-1"`、observations 两条）。
- 只删那 20 行。`chars[*].views[91806]`、`turnins[*][91806]`、`manual[*][91806]`（文件里 91806 另出现 13 次）**全部保留**——那是各小号真实到账的金币历史，删了等于抹账。
- 前置检查：`tasklist` 里不许有 `Wow.exe`（游戏开着的话，退出/`/reload` 时客户端会把整份存档按内存原样写回，我改的会被盖掉）。
- 操作：先复制一份 `WorldQuestTracker.lua.before-drop-91806` 同目录留档 → 删段 → 用 `lua`/lupa `loadfile` 验语法 + 确认 `王牌狩猎` 出现次数变 0、`[91806]` 剩 13 次 → 把 diff（行数、删掉的原文）报给玩家。
- 不做的事：不改 `db.schema`、不加 v6 迁移（玩家选的是直接改存档，代码里不留一次性逻辑）。

## 六、代价与边界（照实说，不粉）

1. **金币门槛暂时改不了**：存档里是 500 金就一直 500 金。想改只能改存档或等这轮 UI 加回来。
2. **右键没有反悔**：确认框没了，点歪一下就当场屏蔽。撤销走新窗口那一行「取消忽略」；如果屏蔽的那条名字不含任何关键词，它在新窗口里出现不到，只能靠改关键词或改存档恢复——用户明确说他的屏蔽都来自关键词误命中，本轮按这个口径不做额外入口。
3. **旧弹框那个全池平铺列表没了**：以前能在一屏里按金币翻所有池内任务，现在必须先选关键词。
4. **设置面板不再报"账本命中"**：命中数搬到新窗口左栏每一行尾巴上，信息不减。

## 七、游戏内验收（改完部署后要做的）

1. 整目录覆盖 `WorldQuestTracker/*` 到 `Interface/AddOns/WorldQuestTracker/`，`/reload`。
2. 矩阵里右键一行任务 → **不弹任何窗**，行当场消失，聊天报"已永久屏蔽 …"。
3. 点「添加任务」→ 左栏是关键词列表（尾巴带命中数），点「丰饶」→ 右栏列出命中任务，每行一个「忽略」按钮；点其中一条的「取消忽略」→ 按钮当场变「忽略」，聊天报"已取消忽略 …"。
4. 左栏输入框加一个关键词、再删一个 → 删掉后由它注册的周常行跟着退列。
5. 点「设置」→ 只剩"跟踪地图"和"登录时抓一次世界任务池"两段，没有关键词段、没有金币门槛段、没有大段说明文字；地图那三段控件逐个还能用。
6. 矩阵里**不再有**「王牌狩猎」那一行（存档已删）。等它下次上板时自动被快照收回来，重新按当轮金币判定。

## 八、用例账

296 → 预计 299（新增 5：右键免确认、两栏 3 条、面板加删关键词；改写 8：`right_click_remove_survives_resync`、`removed_keyword_log_quest_does_not_come_back`、`add_dialog_*` 两条搬家、`options_shows_keyword_hit_count`、`options_hunt_button_survives_missing_sync`、`hover_ignores_rows_while_add_dialog_open`、`options_gold_threshold_control_writes_config` 改反向断言；删除 2：`options_refresh_button_resnaps`、`add_dialog_labels_only_ignored_and_auto_listed`（旧标签体系随弹框下线））。
