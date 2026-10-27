# WorldQuestTracker：两栏窗口几何修复 + 设置面板去掉"登录同步"开关——实施计划

> **For agentic workers:** 逐步骤实施，每个改动步骤先跑 `python tests/run_wqt_tests.py -k <族>` 看到 RED，
> 再实现，再跑到全绿；收尾回到全量 `python tests/run_wqt_tests.py` + `--syntax`。步骤用 `- [x]` 勾选。

**Goal:** 两条口径一次做完：① 两栏「关键词 · 屏蔽管理」窗口的**底部锚点全写反了**，导致左栏只剩
几十像素可视高、栏底的"关键词输入框 + 添加"整排掉到窗口外面（=现在根本没法加关键词）、
"补扫跟踪地图"半截溢出边框 —— 修好锚点让两栏都撑满窗口、内容超出时滚轮能滚、栏底控件回到窗口内；
② 设置面板里"登录时抓一次世界任务池"那颗勾选**整块撤下**，登录默认就抓，不再提供开关。

**Architecture:** 只动 UI 层。`WorldQuestTracker/Options.lua`（`BuildPanel` 的锚点 + 设置面板的
`secLogin`/`loginChk`）、`WorldQuestTracker/Sync.lua`（**只改一句注释**）、`tests/run_wqt_tests.py`（用例）。
**数据一行不改**：`db.config.syncOnLogin` 留着（用户存档里现在就是 `true`）、
`Config.SyncOnLogin()` / `Config.SetSyncOnLogin()` 两个函数留着、`Sync.OnWorldEnter` 里
`loginPending = WQT.Config.SyncOnLogin() == true` 那一句照旧读档 —— 用户口径"只是改 UI，为什么要动数据"。

**Tech Stack:** WoW 12.1.5 内嵌 Lua（5.1 语义）、离线测试 Python 3 + lupa（替身帧把 `SetPoint`
记成 `__points = {point, relativeTo, relativePoint, x, y}`，把 `SetVerticalScroll` 记成 `__vscroll`）。

---

## 一、用户口径（原话，不要改写）

> 1、这个界面有问题， 左侧显示不全，高度有问题，多了要能滚动，又测多了最好也能滚动，补扫按钮溢出了。
> 2、设置界面里 登录抓取 那个去掉，默认就抓取 不用设置

（"又测多了"= 右侧多了。）

## 二、截图实测事实（`关键词 · 屏蔽管理` 窗口，2026-10-27 用户回传）

| # | 现象（图上读出来的） | 根因（`Options.lua` 现状） |
|---|---|---|
| 1 | 左栏只看得见 1.5 行关键词（第 2 行"符文石 4"被腰斩），下面一大片空 | `:473` `Panel.scrollL:SetPoint("BOTTOMRIGHT", f, "TOPLEFT", 12 + KW_LEFT_W, -(BOTTOM_H))` —— 相对锚点写成 **TOPLEFT**，于是底边落在"窗口顶往下 86 像素"，而顶边在"顶往下 56 像素"：一个**负高度**的滚动区，客户端裁出来只剩几十像素 |
| 2 | "补扫跟踪地图"半截掉在窗口下边缘外面 | `:526` `SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 12, -12)` —— WoW 的 y 偏移**向上为正**，`-12` 是底边往下 12 像素 = 窗口外面 |
| 3 | 关键词输入框 + "添加"按钮**整排在图上看不见**（=本轮根本没法加关键词，比"补扫溢出"更严重） | `:501` `SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 12, -BOTTOM_H + 34)` = `-52` → 底边往下 52 像素，整只控件在窗口外 |
| 4 | 右栏看不出问题，但它的滚动区同样越界 12 像素 | `:486` `SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -12, -12)` —— 最后一个 `-12` 同样应该是正的 |

**结论**：三处符号错 + 一处锚点错，全在 `BuildPanel` 里；行池/命中集合/按钮回调都不用动。

## 三、最终几何（改完之后）

窗口 640×420 不变（`PW`/`PH`），栏底固定块的高度收进一个有名字的常量：

```
local FOOT_H = 74          -- 左栏底下让给两排固定控件：输入+添加(24) / 补扫(22) + 留白
```

| 控件 | 改后锚点（全部相对 `f` 的**底边**，y 为正=往上） | 占位（距底边） |
|---|---|---|
| `Panel.huntBtn`（补扫） | `BOTTOMLEFT, f, "BOTTOMLEFT", 12, 12` | 12 → 34 |
| `Panel.kwEdit`（输入框） | `BOTTOMLEFT, f, "BOTTOMLEFT", 12, 40` | 40 → 64 |
| `Panel.addBtn`（添加） | `LEFT, Panel.kwEdit, "RIGHT", 6, 0`（不动，跟着输入框回到窗口内） | 同上 |
| 左栏 `scrollL` | 顶：`TOPLEFT, f, "TOPLEFT", 12, -HEADER_H - 22`（不动）<br>底：**`BOTTOMRIGHT, f, "BOTTOMLEFT", 12 + KW_LEFT_W, FOOT_H`** | 可视高 ≈ 420-56-74 = **290** ⇒ 14 行关键词，再多滚轮滚 |
| 右栏 `scrollR` | 顶：不动<br>底：`BOTTOMRIGHT, f, "BOTTOMRIGHT", -12, 12` | 可视高 ≈ 420-56-12 = **352** ⇒ 17 行任务，再多滚轮滚 |

- 两栏的滚轮早就挂在 `PanelWheel` 上（`offY - delta*STEP` 那套 clamp 读 `self:GetHeight()`），
  **几何一修好，滚轮就真的有得滚**（原来视口是负的，clamp 恒等于 0）。
- **不画可见滚动条**：矩阵本体也没有（`grep ScrollBar Matrix.lua` 空），窗口里保持"滚轮 = 矩阵同一套手感"。
  这条是刻意的，别顺手加：两栏各画一条要占掉 40 像素宽度，窗口得跟着加宽。
- `BOTTOM_H` 这个局部名不再存在，改成 `FOOT_H` 并只用于左栏底边；`HEADER_H`/`ROW_H`/`STEP` 是文件顶部
  的模块级常量，不动。
- **本节表格里左栏底边那一行原本写错，实施时才发现并纠正（计划自带 bug，记录在此）**：
  我写的是自身锚点也用 `BOTTOMLEFT`。两只锚点全钉 LEFT 只给出两条左边缘，**右边缘没人定义**
  ⇒ 真客户端里整栏宽度算成 0，比现在的"矮"更糟。保留自身锚点 `BOTTOMRIGHT`（它的 x 就是
  `12 + KW_LEFT_W`，正好把右边缘钉在左栏宽度的尽头），只把**相对点**从 `TOPLEFT` 改成 `BOTTOMLEFT`。
  用例 1.1 断言的是 relativePoint 与 y 的符号，与自身锚点名无关，所以判据不受影响。

## 四、设置面板去掉"登录同步"（只删控件）

- 删 `Options.lua`：`secLogin`/`loginChk` 的**声明**（`:35`）、`Build()` 里的创建（`:258-268`）、
  `Options.Refresh()` 里的排版（`:339-344`）与 `SetChecked`（`:378`）。
- 面板正文就此只剩"跟踪地图"一段（含默认图列表、移除、＋添加当前所在地图、按名称添加）。
  自适应高度那套（`contentH`/`maxH`/滚动条显隐）不动，少一段自然矮一截。
- **不动的东西**（一条都不许顺手删）：`Config.Init` 里 `db.config.syncOnLogin` 的播种（`Config.lua:336`）、
  `Config.SyncOnLogin()`（`:399`）、`Config.SetSyncOnLogin()`（`:406`）、`Sync.OnWorldEnter` 里
  `loginPending = WQT.Config.SyncOnLogin() == true`（`Sync.lua:1182`）。
  存档里现在是 `true` ⇒ 行为就是"登录默认抓"，正是用户要的。
- `Sync.lua:1180` 那句注释要改口：原文写着"设置面板与 `Config.SetSyncOnLogin` 可关"，
  面板入口本轮撤了，注释得说实话（**要留的实测事实**：用户 2026-10-24 实测过后台抓池会卡，
  所以这个开关不是白造的 —— 现在只能改存档 `["syncOnLogin"] = false`，别再往界面上加回来）。
- 测试替身里 CheckButton 的 `SetChecked` 支持（`tests/run_wqt_tests.py:270` 附近）**留着**：
  那是通用替身能力，不是为这颗勾专门造的。

## 五、任务清单

### Task 1 — 两栏窗口几何

- [x] 1.1 RED：新增 `test_panel_columns_and_footer_stay_inside_the_window`（放在
      `test_panel_can_add_and_remove_keyword` 后面，注册进 `tests` 列表）。做法：
      `sync_env([], extra=("Matrix", "Options"))` → `wqt.Options.OpenKeywordPanel()` →
      在 Lua 里读 `WQTKeywordPanel` 与它下面那几只控件的 `__points`，逐条断言：
      ① `WQTKeywordPanelScrollL` 的第二条 point 的 relativePoint == `"BOTTOMLEFT"`（**不是** TOPLEFT）
      且 y > 0；
      ② `WQTKeywordPanelScrollR` 的第二条 point 的 relativePoint == `"BOTTOMRIGHT"` 且 y > 0；
      ③ 补扫按钮与关键词输入框（按 `__text == "补扫跟踪地图"` 与全局名
      `WQTKeywordPanelKwEdit` 找）挂在 `f` 的 `"BOTTOMLEFT"` 上且 **y > 0**；
      ④ 输入框的 y 比补扫按钮的 y 大（两排不叠在一起）。
      断言消息里把读到的 `point/relativePoint/x/y` 原样打出来，别只说"不对"。
- [x] 1.2 RED（**诚实标注：这一条大概率当场就绿**，它不是本轮 bug 的门，钉的是"修几何时别把
      clamp 写坏"。真正的 RED 门是 1.1 —— 替身帧不根据锚点算高度，所以"溢出"只能在 1.1 用
      `__points` 断言，测不出"视口真的是负的"这件事）：
      打开窗口后把两栏视口人为压矮（`WQTKeywordPanelScrollL:SetSize(250, 40)`、
      `WQTKeywordPanelScrollR:SetSize(350, 40)`），加 6 个关键词 + 造几条命中，
      `Options.RefreshKeywordPanel()`，然后直接调 `scrollL` 上挂的 `OnMouseWheel`（`delta` 用
      **负值** = 真客户端往下滚，见 `Options.lua` 的 `OnWheel` 头注）并断言
      `WQTKeywordPanelScrollL.__vscroll > 0`；右栏同理（选中一个命中的关键词再滚）。
- [x] 1.3 实现：按第三节那张表改 `BuildPanel` 的四只锚点 + `BOTTOM_H` → `FOOT_H = 74`。
      注释里写清**为什么 y 是正的**（WoW 从底边锚定时 y 向上为正；上一版三处写成负数，
      控件整个掉到窗口外，用户截图里"补扫溢出"和"输入框根本看不见"是同一个错）。
- [x] 1.4 GREEN：`-k panel`。

### Task 2 — 设置面板去掉"登录同步"

- [x] 2.1 RED：新增 `test_options_panel_has_no_login_sync_switch`：`wqt.Options.Open()` →
      `frame_texts(g.WQTOptionsFrame)` 里**不许**出现"登录"两个字（反向断言），
      同时仍要有"跟踪地图"与"按名称添加"（证明只撤了那一段，没把面板一起弄没）。
- [x] 2.2 实现：按第四节删控件；`Sync.lua:1180` 注释改口。
      **不许**动 `Config.*` 与 `Sync.OnWorldEnter` 的读档那一句。
- [x] 2.3 GREEN：`-k options,login`。既有 `test_login_snapshot_runs_without_polling` 与
      `test_login_snapshot_switch_off` 的口径**一个字都不改**（它们钉的是数据层那两档行为）。

## 六、代价与边界（照实说，不粉）

1. **"登录不抓池"从此没有界面入口**：想关只能改存档 `["syncOnLogin"] = false`。
   用户 2026-10-24 实测过后台抓池会卡，这颗勾当年就是为那个加的；本轮按他"不用设置"的口径撤下，
   真卡回来时别再往面板上加控件，先按这条改存档。
2. **两栏仍然只有滚轮可滚**（不画滚动条，与矩阵一致）。窗口高度固定 420，不加自适应。
3. 关键词一多，左栏可视 14 行、右栏 17 行，超出靠滚轮 —— 本轮不做搜索框。

## 七、游戏内验收（改完部署后要做的）

1. 整目录覆盖 `WorldQuestTracker/*` 到 `Interface/AddOns/WorldQuestTracker/`，`/reload`。
2. 点「添加任务」→ 左栏一屏能看到十几行关键词；关键词加到 20 个 → 滚轮能滚到底，
   最后一行完整可见、不被腰斩。
3. 栏底两排控件都在窗口**里面**：输入框 + 「添加」在第一排、「补扫跟踪地图」在第二排，
   输入关键词点添加能真的加进左栏。
4. 选中命中很多（>17 条）的关键词 → 右栏滚轮能滚，最后一行不被裁。
5. 点「设置」→ 只剩"跟踪地图"一段（面板高度跟着变矮），没有"登录时同步"那颗勾。
6. 重新登录后矩阵照旧有本周板面（登录抓池仍在工作，只是没有开关了）。

## 八、用例账

299 → 预计 302（新增 3：几何、两栏滚动、设置面板无登录开关；改写 0；删除 0）。
本轮不删任何既有用例：`login_snapshot_switch_off` 钉的是数据层开关语义，控件撤了它照样有效。
