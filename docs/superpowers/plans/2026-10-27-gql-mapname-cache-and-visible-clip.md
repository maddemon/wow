# GoldQuestLedger 性能计划：MapName/MapParentID 会话缓存 + 渲染与 hover 裁到可见行

计划唯一真源。分支 master，动 `GoldQuestLedger/Api.lua`、`GoldQuestLedger/Matrix.lua`、`tests/run_gql_tests.py`。
基线 **313/313**（2026-10-27 本轮开工前实测，`python tests/run_gql_tests.py`）。

## 〇、用户口径

> 判断一下任务插件有没有性能问题，以后任务越来越多，会不会出问题

审计汇报后：

> 按 ①② 的顺序直接动手

硬约束（沿用既有口径）：**零新增接口调用**、不轮询、不扫图；只砍重复问。配置值/判据/schema 一个字节不动。

## 一、审计实测（离线替身，只数接口调用次数，不报毫秒）

lupa 探针（临时脚本，测完已删）在三档规模下数的数：

| 场景 | 10 张跟踪图 / 30 行×12 列 | 20 张跟踪图 / 200 行×20 列 |
|---|---|---|
| 一次快照的 `C_Map.GetMapInfo` | 100 次 | 400 次 |
| 一次重绘建/复用的对象 | 8841 个（约 4200 次 SetText、8800 次 SetPoint） | 同左 |
| 一帧 hover 扫的 `IsMouseOver` | 390 次/帧（≈23k/秒） | 4200 次/帧（≈252k/秒） |

三份都要跟着任务数长：

1. **`Api.MapName` / `Api.MapParentID` 没有会话缓存**（`Api.lua:1205`、`Api.lua:1275`）——每次都落到 `C_Map.GetMapInfo`。
   最烧的是 `Sync.PickPinMap`（`Sync.lua:599-609`）：候选图两两问父图判孤儿，**O(#图²)**，
   10 张图一趟快照 100 次、20 张 400 次。地图名与父子关系一个会话内不会变，问一次就该记住。
2. **`Mx.Render`（`Matrix.lua:1436`）不按可见区裁**：组/行/列三重循环把**所有**行都摆一遍
   （`PutL`/`PutCenter`/`CellButton`/`RowBorder`），200 行×20 列一次重绘就是 8800 次 SetPoint。
3. **hover 每帧全量扫**：`HoverTarget`（`Matrix.lua:649`）挂在主帧 OnUpdate 上（`Matrix.lua:1425`），
   对 `usedRowBtns + usedBtns` 逐个 `IsShown()` + `IsMouseOver()`。文件头那条注释
   （`Matrix.lua:925-926`「扫的是几十个 IsMouseOver，很轻」）在 30 行时就只剩 390 次/帧，200 行×20 列是 4200 次/帧 —— 实测已经不轻。

已经确认**不是**问题的部分（本轮不动）：`Ledger.MatrixRows` 一次重绘仅 2 次接口（120 条记录实测）；
`polling` 默认 false、只由 `Mx.Toggle` 开、OnHide 关，`ScheduleNextRefresh` 至多挂一个定时器；
逐任务的 `QuestState`/`QuestTimeLeft` 是签名要求的读数，不是白烧。

## 二、①：`Api.MapName` / `Api.MapParentID` 加会话缓存

### 2.1 照哪条现成的路子

`Api.lua:95` 已经有三张同类表（`wqVerdict, titleCache, mapCache`）和一段口径说明：
**只缓存问出来的结果，nil/报错一律不缓存**（`Api.IsMidnightMap` 的 `midnightMapCache`，`Api.lua:318/378-391` 是同一条写法）。
本文件里地图名/父图号的会话缓存放同一处声明，命名 `mapNameCache`、`mapParentCache`。

### 2.2 `MapName(mapID)`

- 入口先查表：`local cached = mapNameCache[mapID]; if cached ~= nil then return cached end`。
- 三条候选源（`MAP_NAME_SOURCES`）里**第一个答出非空字符串**的写进表再返回。
- 全问不出 → 返回 nil 且**不写表**：登录早期地图数据可能还没就绪，把 nil 冻进表等于这张图整场没有名字。

### 2.3 `MapParentID(mapID)`

这里有个必须讲清的分岔：光缓存「问到的父图号」**砍不掉**那条平方项。`PickPinMap` 的两两循环里，
一张「没有父图」的大洲图每次都答 nil，不写表 → 下一轮照旧重问 #kept 次。所以要区分
**「问过、确实没有父图」**和**「还没问过 / 客户端报错」**，前者写 `false` 当哨兵。

- `C_Map.GetMapInfo` 取不到（不可调用 / pcall 失败 / 回的不是表）→ 返回 nil，**不写表**（同 MapName 的理由）。
- 取到表：`local p = tonumber(info.parentMapID)`；`mapParentCache[mapID] = p or false`；返回 `p`（可能是 nil）。
- 缓存命中时：`local cached = mapParentCache[mapID]; if cached ~= nil then return cached or nil end`
  —— `false` 条目回 nil，调用方拿到的东西和今天一模一样（`PickPinMap` 只把它和数字比，`false ~= 数字` 与 `nil ~= 数字` 同效；全仓调用方就 `Sync.lua:603` 一处）。

效果：`PickPinMap` 的 #kept² 次调用落到 `C_Map.GetMapInfo` 的次数 = **每张图一次**，与图数线性。

### 2.4 不做失效

会话级（`/reload` 重建 Lua 状态自然清）——与现有三张表一致。地图名/父子结构在资料片里改版要重载才算，本轮不引入 `MapCanvas` 事件监听（那是新增事件+新增调用，超出「纯砍重复」的范围）。

### 2.5 ①的用例（加在 `tests/run_gql_tests.py`）

替身只装 `C_Map.GetMapInfo`，用 Python 侧计数器数它被问了几次：

1. `map_name_cached_per_session`：`Api.MapName(id)` 连问 5 次 → 底层 `GetMapInfo` **恰好 1 次**，且 5 次返回值相同。
2. `map_parent_cached_per_session`：10 张图（其中 3 张没有 `parentMapID`），每张各问 10 次（模拟 `PickPinMap` 的两两循环）
   → 底层调用 **10 次**（不是 100 次），且没有父图的 3 张每次都回 nil。这条直接钉住平方项。
3. `map_name_nil_is_not_frozen`：先让 `GetMapInfo` 回 nil/报错 → `MapName`/`MapParentID` 回 nil 且**不落表**；
   换上一个能答的替身再问 → 立刻问得出（证明缓存没把「问不出」当结论冻住）。

## 三、②：`Mx.Render` 与 hover 扫裁到可见行 ± 缓冲

### 3.1 同源的一个改动

hover 那 4200 次/帧不是因为扫描本身写了全量循环，而是**因为 `usedBtns` 就是全量**。
`HoverTarget` 只遍历 `1..usedRowBtns` / `1..usedBtns`，而这两个计数由 `Render` 里真正摆出来的热区决定。
所以 Render 裁到可见区后，`HoverScan` 的扫描量**自动**降成「可见行 × 可见列」= 常数级，扫描代码本身一行不用改。
要改的只有 `Matrix.lua:925-926` 那条已经不成立的注释。

### 3.2 Render 的裁剪窗口

- 竖窗：`offY - OV` 到 `offY + visH + OV`；横窗：`offX - OV` 到 `offX + visW + OV`；`OV = 3 * ROW_H`（60px，在 `Render` 内部算，**不开顶层 local** — 见 3.5）。
- 组的整体判定：整组（组头 `GROUP_H` + `#rows*ROW_H`）与竖窗不相交 → 组头和该行所有对象全部跳过，但 `y` 照常推进；
  相交才逐行判。单行判定：`y + ROW_H >= 竖窗顶 and y <= 竖窗底`。
- 列判定：`cx + COL_W >= 横窗顶 and cx <= 横窗底`（`cx = (ci-1)*COL_W`）——列头早就有这套隐藏逻辑（`Head.ShowCur` 的 `px + COL_W < 0 or px > visW`），正文此前没有，两处口径从此并成一条。
- `rowCount` **继续数全部行**：它是「矩阵列了几行」的口径（`Mx.rowCount` 被用例依赖），不是「画了几个对象」。裁剪只影响摆出来的对象。
- `estBodyH`（`Matrix.lua:1485-1491`）保持全量 = 最终 `contentH`：滚动条长度、`content:SetSize`、`curHi` 拉满整列都靠它，绝不能跟着裁。
- 兜底：`visH` 或 `visW` 取不到数（≤0）→ **不裁**，走今天的全量路径。离线替身和布局未就绪的帧都落这里，宁多画不误藏。

### 3.3 offY 必须在画之前夹（这条是老坑的正解）

今天 `offY` 只在渲染末尾 `ClampOffsets()`（`Matrix.lua:1630`）夹一次，注释（`1509-1512`）写明原因：那一刻 `contentH` 还是上一轮的旧值。
裁剪之后这个顺序会咬人：按旧的、偏大的 `offY` 决定「画哪些行」，画完才被夹小 → **窗口下沿那几行本轮没对象、下一轮才补上**，
表现就是「滚动错位/首格被吞」（用户实测过的老坑）。
所以 `visH` 一算好（`1508`）就用**本轮已知的 `estBodyH`** 把 `offY` 先夹一次，再进绘制循环；末尾 `ClampOffsets()` 原样留着（`contentH == estBodyH`，它退化成幂等）。
顺手补一个不一致：`VisibleRows`（`1470-1483`）在豁免行为空时回**空表**，`estBodyH` 照样加了一份 `GROUP_H`，绘制循环的 `#rows > 0` 又不画组头 —— 两处估高从此分岔。让它回 nil，两份口径同源（组头本来也没画，玩家可见行为不变）。

### 3.4 不改的东西（点名）

- 事件、定时器、轮询、`ScheduleNextRefresh` 节奏：一个都不动。
- `Ledger.MatrixRows` / `MatrixColumns` 的判据、门槛、去重、分组：不动。
- 帧/对象池的增长策略（只复用不释放）、`Pool.Begin/End`、尾部 `Hide` 三段（`1636-1640`）：不动。
- `CoverCheck` 的 0.5s 按按钮节流、`hiL.__btn` 作废（`1645`）、金额提示那套：不动。
- `SetVerticalScroll/SetHorizontalScroll` 四次调用、`UpdateScrollbars()`：位置与顺序不动。
- 悬停里显示的 ID/金币/来源文字：不动。

### 3.5 upvalue 预算

`Matrix.lua` 里最热的顶格函数就是 `Mx.Render`，WoW 的 Lua 5.1 每个闭包上限 60 个 upvalue（超了整文件加载失败，
`upvalues:Matrix.lua` 用例每次跑都数）。窗口参数一律算在 `Render` **体内**（函数局部），
新增的顶层状态只有 `Geom` 表里一个键（`Geom.overscanRows = 3`）—— 不加顶层 local。
改完必须看 `upvalues:Matrix.lua` 仍 PASS。

### 3.6 ②的用例

1. `matrix_render_clips_to_visible_rows`：造 120 行×6 列、可视高只放约 20 行 → 断言
   本轮 `Mx.rowCount == 120`（口径没变）而**建出的热区数远小于 120×6**（读 `#cellBtns`/`#rowBtns` 上限），
   `contentH` 仍等于全量高，滚动条长度按全量算。
2. `matrix_scroll_keeps_first_cell_after_content_shrinks`：内容从 120 行掉到 30 行、`offY` 停在旧的大值
   → 断言本轮结束时 `offY == max(0, contentH - visH)`，且**该窗口对应的行确实有对象**（首格不被吞）。
3. `matrix_hover_sweep_is_bounded_by_drawn_rows`：把鼠标摆在可见行上跑一次 OnUpdate，
   数 `IsMouseOver` 调用次数 ≤ (可见行+缓冲) × (可见列+缓冲) × 2；对照旧口径（全量行×列）必须更小。
4. `matrix_clip_disabled_when_viewport_unknown`：`visH`/`visW` 取不到（替身回 0）→ 全量画，行数与对象数都按今天的路径，
   钉住「取不到就不裁」的兜底。

## 四、执行顺序与验收

1. ①：`Api.lua` 两张表 + 两个函数 → 三条用例 → `python tests/run_gql_tests.py` 全绿（313 + 3）。
2. 逐行 review、`git diff` 自查，只提交本轮两个文件，①单独一个 commit。
3. ②：`Matrix.lua` 裁剪 + offY 提前夹 + `VisibleRows` 回 nil → 四条用例 → 全绿。
4. 逐行 review，确认 `upvalues:Matrix.lua` PASS、无新增接口调用（用例计数器兜底），单独一个 commit。
5. 汇报用大白话：改了哪两处、每次重绘/每帧少问多少、以及**没在真客户端实测**这件事要如实说清，
   并给他一条能自己看的取证路径（`/gql matrix` 开着跑一轮，看日志里重绘次数与卡不卡）。

工作区是共享的（并行会话在改 `AHSnipe/`、`docs/tasks/2026-10-13-ahsnipe-plan.md`、`tests/run_tests.py`）：
提交前 `git status` 看清 `GoldQuestLedger/` 与 `tests/run_gql_tests.py` 有没有别人的改动。
