# 2026-11-09 矩阵标题栏：只剩插件名，"最后同步"整条下线

用户 2026-11-09 复测完层序取证（三扇窗全报"无越层子帧"、无截断，卡口过），紧跟着对矩阵标题栏提了三条，
并附截图：标题栏那行字**叠在一起**了。

## 一、截图上的现场

标题栏现在从左到右是：`GoldQuestLedger 任务×角色矩阵 v0.6.2`（左对齐，[Matrix.lua:1590](../../GoldQuestLedger/Matrix.lua)）
+ `最后同步 02:18:xx`（右对齐钉在"设置"按钮左边，[Matrix.lua:1665](../../GoldQuestLedger/Matrix.lua)）
+ `设置` 按钮 + `×`。

他的三条要求（原话）：
1. 「矩阵的标题栏改一下，现在如果任务太少的时候，都挤在一起了」
2. 「最后同步 xxx 去掉，不用显示了」
3. 「插件 只显示名称就行了」「任务角色矩阵这些字不用显示了」

## 二、为什么会挤在一起（量出来的，不是猜的）

窗口宽度是**跟着列数走的**（[Matrix.lua:1797](../../GoldQuestLedger/Matrix.lua)、[:2118](../../GoldQuestLedger/Matrix.lua)）：

```lua
frameW = math.min(math.max(260, NAME_W + COL_W * #cols + 40), Geom.maxW)
```

`NAME_W=120`、`COL_W=72` ⇒ 只有 1~2 列时宽度落到下限 260（截图那两张列 = 304）。
标题从 x=12 起往右铺，`设置` 按钮（72 宽）右边缘在 -32，左边缘就在 `frameW-104`；
"最后同步"那行字再往左占一大截 ⇒ 窄窗口里标题和它必然重叠。截图看到的"叠字"就是这个。

离线量出来的真实形状（`tests/_tmp_header.py` 一次性脚本，替身 `GetStringWidth` = 字节数×6，
**假尺度、只比大小**，用完删）：

```
标题="GoldQuestLedger 任务×角色矩阵 v0.6.2" 宽=258 | 最后同步="最后同步 0 分前" 宽=126 | 设置按钮宽=72
1 列 窗宽=260 设置左边缘=156 | 标题右边=270  标题+同步右边=404  << 挤
2 列 窗宽=304 设置左边缘=200 | 标题右边=270  标题+同步右边=404  << 挤
3 列 窗宽=376 设置左边缘=272 | 标题右边=270  标题+同步右边=404  << 挤
8 列 窗宽=736 设置左边缘=632 | 标题右边=270  标题+同步右边=404
```

⇒ **光是标题自己就已经盖到"设置"按钮上**（270 > 156/200/272），和那行"最后同步"叠得更早；
要到 4 列往上才看不出来。这正是他说的"任务太少的时候都挤在一起"。

⇒ 他给的三条正好把这件事一次解决：**去掉那行字** + **标题只剩插件名**
（`GoldQuestLedger` 15 字节 ⇒ 量出来 90，右边缘 102，最窄 260 时也在设置按钮左边缘 156 之内）。


## 三、整类盘点（还有别的窗口会挤吗）

| # | 位置 | 宽度 | 标题栏内容 | 会挤？ |
|---|---|---|---|---|
| 1 | 矩阵 [Matrix.lua:1797](../../GoldQuestLedger/Matrix.lua) | **随列数变**，下限 260 | 标题 + 最后同步 + 设置 + × | **是** ⇒ 本轮 |
| 2 | 设置窗 [Options.lua:659](../../GoldQuestLedger/Options.lua) | 固定 `PANEL_W` | 标题 + × | 否 |
| 3 | 日志窗 [LogWindow.lua:41](../../GoldQuestLedger/LogWindow.lua) | 固定 `PANEL_W` | 标题 + × | 否 |
| 4 | 暴雪设置页画布头 [Options.lua:812](../../GoldQuestLedger/Options.lua) | 固定 600 | 标题 | 否 |

⇒ 只有矩阵这一处，别的窗口宽度不随数据变，没有同类风险。

**顺带发现的一处不一致（不动，等他点头）**：设置窗标题写的是「世界任务追踪 · 设置 v0.6.2」
（[Options.lua:681](../../GoldQuestLedger/Options.lua)），"世界任务追踪"是 2026-10-27 改名**之前**的旧插件名；
同一个文件里暴雪设置页那头的标题写的是「GoldQuestLedger v…」（[:817](../../GoldQuestLedger/Options.lua)）。
两处对不上。本轮不自己改，等他一句话。

## 四、任务

- **T1 标题只剩插件名**：[Matrix.lua:1597](../../GoldQuestLedger/Matrix.lua) 的 `SetText` 改成固定 `"GoldQuestLedger"`，
  不再拼「任务×角色矩阵」、不再拼版本号。（版本号仍有三处能看到：设置窗标题、小地图按钮悬停、`/gql version`，
  取证面不丢；他这条是照字面说的"只显示名称"。）
- **T2 "最后同步"整条下线**（拆干净，不留半成品）：
  - 建帧那段 [Matrix.lua:1659-1668](../../GoldQuestLedger/Matrix.lua)（含上面那段 11-04 注释）删掉；
  - 顶层声明与注释 [Matrix.lua:54-56](../../GoldQuestLedger/Matrix.lua) `local syncLabel` 删掉；
  - `LastSyncText` + `Mx.RefreshSyncLabel` [Matrix.lua:2347-2375](../../GoldQuestLedger/Matrix.lua) 删掉；
  - 打开面板那趟的调用 [Matrix.lua:2432](../../GoldQuestLedger/Matrix.lua) 删掉，并把 [:2427](../../GoldQuestLedger/Matrix.lua)
    那句"标题栏的『最后同步』就是这段窗口的可见凭据"的注释改到实话（凭据现在只在 `/gql quest` / 日志里）；
  - 抓池末尾的推送 [Sync.lua:1451-1455](../../GoldQuestLedger/Sync.lua) 删掉。
  - **数据不动**：`db.chars[].lastSync` 照写照存（[Sync.lua:1176](../../GoldQuestLedger/Sync.lua) 不动），
    `/gql dump` 那行读数（[Dump.lua:65](../../GoldQuestLedger/Dump.lua)）保留 —— 他砍的是显示，不是记录。
- **T3 用例（先红后绿）**：
  - `matrix_title_drops_readonly_label`（tests 12286）：保留"不许有『只读』"，第二条断言反过来钉
    —— 标题必须**正好**是 `GoldQuestLedger`，且整条标题栏不许再出现「任务×角色矩阵」「v0.6.2」「最后同步」。
  - `opening_matrix_resyncs_and_shows_last_sync`（12299）：这条钉了五件事，只有"最后同步"那两条要撤；
    其余四条**原样保留**（`f:Show()` 必须先于抓池、不许有"刷新"按钮、轮询被打开、账本 101 回来、
    行文本 ≥4 颗），并把用例改名成不再承诺那行字。
  - `last_sync_label_handles_three_clock_shapes`（12365）：整条讲的就是那行字 ⇒ 删。
  - 新增一条**几何**用例（他这条是"挤在一起"，不量就还是猜）：把窗口压到最窄（1 列 ⇒ 260），
    要求标题的右边界 `GetStringWidth()` + 左边距 < "设置"按钮的左边界；替身的 `GetStringWidth` 是
    `#text * 6`（tests:337），这条在离线量得出相对关系，写清楚它是相对尺子、不是像素真值。
- **T4 三门**：全量 / `--syntax` / `-k lua_upvalue_budget`（Matrix.lua 顶层 local 最坏 55/60，
  本轮只删不加，删掉 `local syncLabel` 只会更松）。
- **T5 变异检查**：① 标题重新拼回那串；② `syncLabel` 建帧留着只删刷新；③ `RefreshSyncLabel` 调用留着；
  ④ 几何用例改成永远过关。要求逐条咬红。
- **T6 版本**：他还没把 0.6.2 发出去（「那我就发版本了」是将来时），所以这轮**并进 0.6.2**，
  不另开版本号；CHANGELOG 0.6.2 补两条玩家话（标题栏只剩插件名；不再显示"最后同步"）。
  重打 `GoldQuestLedger_0.6.2.zip`、重新部署 AddOns。
- **卡口**：他实机 `/reload` 后看矩阵标题栏 —— 1~2 列那种窄窗口里不许有叠字，标题只有 `GoldQuestLedger`，
  没有"最后同步"。

## 五、本轮不动的

`lastSync` 的写入与存档、`/gql dump` 的读数、设置窗那个旧名字（等点头）、`/gql matrix` 命令的说明文字
（"打开/关闭 任务×角色 矩阵"是命令列表里的说明，不是标题栏）、列宽/行高/窗口宽度公式本身、判据、schema。
不 push。

## 六、完成记录（2026-11-09）

**改了**：标题 `SetText("GoldQuestLedger")`（Matrix.lua:1594）；"最后同步"整条链五处删净
（顶层 `local syncLabel`、建帧那段、`LastSyncText` + `Mx.RefreshSyncLabel`、Toggle 里的调用、
Sync.lua 抓池末尾的推送）；Toggle 里那句"标题栏的『最后同步』就是可见凭据"的注释改成实话（凭据在 `/gql quest` 和日志里）。
**数据没动**：`Sync.lua:1176 row.lastSync = now`、`Dump.lua:65` 读数原样。

**残留检查**：`grep -rn "syncLabel|RefreshSyncLabel|LastSyncText" GoldQuestLedger/` = 0 处；
"最后同步"只剩 Ledger.lua 里四处和本轮无关的散文（"谁最后同步算谁的"这类行级钱数语义 + dump 列说明）。

**用例增减**：改 2（`matrix_title_drops_readonly_label` 钉新形状、`opening_matrix_*` 撤掉那两条、
其余四条判据原样保留并改名 `opening_matrix_shows_first_and_resyncs`）、删 1（`last_sync_label_handles_three_clock_shapes`，
整条讲的就是那行字）、新增 1（`matrix_header_title_clears_the_settings_button` 几何）。总数 524 → **524**。
先红形状：旧标题右边界 270 > 设置按钮左边缘 156（窗宽 260）。

**计划外必要的一处**（sub agent 提出、主 agent 逐行看过后采纳）：`version_has_one_readable_source`
原来钉"矩阵标题必须带版本号 v…"，和 T1 直接冲突 ⇒ 撤掉矩阵那一半，toc 与设置窗标题两条照留，
矩阵标题的形状交给新标题用例钉。这是他要的"只显示名称"必然带出来的，不是扩大范围。

**主 agent 复核**：Matrix.lua / Sync.lua / tests / CHANGELOG 四份 diff 逐行看过，没有多余改动；
三门自己重跑一遍（524/524、`--syntax` 524/524、upvalue 1/1）；`/gql dump`、`/gql quest`、`/gql version`
三条命令名在 `RegisterCommand` 里核对过，CHANGELOG 里承诺的去处是真有的。
改完后离线再量一次：窗宽 260、设置按钮左边缘 156、标题右边界 102 ⇒ 最窄窗口留 54 的余量。

**变异检查**：① 标题拼回那串 → 红（标题用例 + 几何用例双双）；② `syncLabel` 建帧留着只删刷新 → 红；
③ Toggle 里留 `RefreshSyncLabel` 调用 → 红（nil 调用直接炸）；④ 几何断言改成永远过关 + 旧标题 → 按要求转绿，
证明几何这条单独承重。二进制读写、逐条 md5 还原一致。一次性脚本 `tests/_tmp_header.py`（量尺）与
`tests/_tmp_mutate.py`（变异）都已删。

**版本**：按 T6 **并进 0.6.2**（他还没发出去），Core.lua / toc 未动；CHANGELOG 0.6.2 补两条玩家话，
原来那句"界面、记录、任务数据一律没动"改成实话（界面本轮动了）。

**入库与部署**：本地 3 个提交（`5e99944` 用例 → `39f0751` 界面 → `95d9871` 计划与记录，未 push）；
16 个文件覆盖到 `D:/Games/World of Warcraft/_retail_/Interface/AddOns/GoldQuestLedger`，逐文件校验码与仓库一致；
`GoldQuestLedger_0.6.2.zip` **重打**（17 条目，逐文件与仓库一致）—— 他发版用的就是这一份。


**待卡口**：他实机 `/reload` 后看矩阵标题栏 —— 窄窗口（1~3 列）不许有叠字，标题只有 `GoldQuestLedger`，
不再有"最后同步"。
**等他点头的一处**：设置窗标题写的是改名前的旧名「世界任务追踪 · 设置」（§三 最后一行），本轮没动。

