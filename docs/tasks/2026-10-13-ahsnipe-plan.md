# AHSnipe 插件实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态：全部任务完成（Task 0-7）+ 第二至十轮迭代（游戏内反馈修复 + 扫描密度优化 + 即时弹窗连续轮询 + 弹窗内容感知抑制与购买前刷新 + 面板精简与系统星星 + commodity 分块购买/购买前刷新/命中自动购买）完成，SubAgent Code Review 已执行（八轮共 27 条问题全部修复），10/10 单测通过，7 个 Lua 文件语法检查通过。待游戏内手工验证（见设计文档 §12）。**

**Goal:** 实现魔兽世界正式服（Retail 12.1）拍卖行狙击插件：AH 开启期间定时扫描指定物品一口价，低价诱饵命中时模态弹窗并一键打包购买。

**Architecture:** 零依赖纯 Lua 插件，模块化单命名空间（`AHSnipe` 全局表），PriceCluster 为纯逻辑模块（无 WoW API，可在游戏外用 Python+lupa 单测）。扫描经 `COMMODITY_SEARCH_RESULTS_*` 事件异步驱动。

**Tech Stack:** Lua 5.4（WoW Retail）、C_AuctionHouse 12.x API、Python 3.12 + lupa（游戏外测试/语法检查）

**设计文档:** `docs/tasks/2026-10-13-ahsnipe-design.md`

## Global Constraints

- 游戏版本：Retail 12.1，TOC `## Interface: 120100`（游戏内如提示版本不符则按客户端提示修改）
- 零外部依赖：不使用 Ace3 等任何库，仅原生 API
- 仅购买一口价；commodity 按单价比较；commodity 购买用两段式 `StartCommoditiesPurchase` → `ConfirmCommoditiesPurchase`
- PowerShell 命令连接用 `;`，禁用 `&&`
- 代码注释用中文，沿用现有风格
- 语言/显示文本用中文

## 12.x API 速查（已核实自 Blizzard_APIDocumentationGenerated）

| 用途 | API / 事件 |
|---|---|
| AH 开/关 | `AUCTION_HOUSE_SHOW` / `AUCTION_HOUSE_CLOSED`（无 HIDE） |
| 节流闸门 | `C_AuctionHouse.IsThrottledMessageSystemReady()` + `AUCTION_HOUSE_THROTTLED_SYSTEM_READY` 事件 |
| commodity 查询 | `C_AuctionHouse.RefreshCommoditySearchResults(itemID)` → `COMMODITY_SEARCH_RESULTS_ADDED(itemID)` 事件（分批到货） |
| commodity 结果读取 | `C_AuctionHouse.GetNumCommoditySearchResults(itemID)`、`GetCommoditySearchResultInfo(itemID, index)` → `{itemID, quantity, unitPrice, auctionID, ...}`；`HasFullCommoditySearchResults(itemID)` |
| 非 commodity 查询 | `C_AuctionHouse.SendSearchQuery(itemKey, {sort}, false)` → `ITEM_SEARCH_RESULTS_UPDATED(itemKey)`；`GetNumItemSearchResults(itemKey)`、`GetItemSearchResultInfo(itemKey, index)` → `{auctionID, quantity, buyoutAmount(可nil), ...}` |
| 物品是否 commodity | `C_AuctionHouse.MakeItemKey(itemID)` → `C_AuctionHouse.GetItemKeyInfo(itemKey)` → `.isCommodity`（异步，nil 时等 `ITEM_KEY_ITEM_INFO_RECEIVED(itemID)` 重试） |
| commodity 购买 | `C_AuctionHouse.StartCommoditiesPurchase(itemID, quantity)` → `COMMODITY_PRICE_UPDATED(unitPrice,totalPrice)` → `C_AuctionHouse.ConfirmCommoditiesPurchase(itemID, quantity)` → `COMMODITY_PURCHASE_SUCCEEDED` / `COMMODITY_PURCHASE_FAILED`；`COMMODITY_PURCHASED(itemID, quantity)` |
| 非 commodity 购买 | `C_AuctionHouse.PlaceBid(auctionID, buyoutAmount)`；成功 `ITEM_PURCHASED(itemID)` |
| 金额格式化 | `C_CurrencyInfo.GetCoinTextureString(copper)` |

注意：12.x 无 `BuyCommodity`/`BuyItem`/`RequestCommodityAuctions`（已重构移除），严禁使用。

## 文件结构

```
AHSnipe/
├── AHSnipe.toc
├── Core.lua          -- 入口：SavedVariables、AH 开/关事件驱动扫描开关、斜杠命令
├── Config.lua        -- 配置默认值、物品增删改查、SavedVariables 读写
├── PriceCluster.lua  -- 价格簇分析（纯逻辑，全局表 AHSnipePriceCluster，可游戏外测试）
├── Scanner.lua       -- 查询调度状态机：串行遍历、事件等待、超时、命中评估
├── BuyQueue.lua      -- 购买执行：commodity 一次性按总量买 / 物品逐条 PlaceBid
├── SnipeWindow.lua   -- 模态狙击弹窗
└── ConfigPanel.lua   -- /ahsnipe 配置面板
tests/
├── run_tests.py      -- Python + lupa：PriceCluster 单测 + 全部 Lua 语法检查
```

## 任务

### Task 0: 仓库初始化

- `git init`；`.gitignore` 忽略 `tests/__pycache__/`；提交设计/计划文档
- 验证：`git log` 有首次提交

### Task 1: TOC 骨架 + PriceCluster 纯逻辑 + 单测（TDD）

**Produces:** `AHSnipePriceCluster.BuildClusters(listings)`、`GetMainCluster(clusters)`、`GetThreshold(listings, discount)`、`IsBaitLike(listing, mainMinPrice)`

- [ ] 先写 `tests/run_tests.py` 用例（失败）：
  1. 截图场景：`[{1789,1},{1799,2},{2950,340},{2953,7922},{2955,326}]`（铜）→ 主簇 min=2950，threshold=2950×0.65=1917.5，1789/1799 ≤ 门槛命中
  2. 诱饵多价归簇：`{1789,1},{1799,2},{1810,3},{2950,100}` → 低价三条同簇（2950/1810≥1.5 切分）
  3. 单条挂单 → 门槛=单价×0.65
  4. 空列表 → 门槛 nil
  5. 挂单数并列 → 取价格更高的簇
  6. 全同价 → 单簇
- [ ] 运行测试确认 FAIL
- [ ] 实现 `PriceCluster.lua`（切分比 1.5；主簇=挂单总数量最大、并列取 minPrice 更高；门槛=主簇 minPrice×discount；诱饵判定=数量≤10 且单价≤主簇 minPrice×0.65）
- [ ] 运行测试确认 PASS
- [ ] 语法检查全部 Lua：`python tests/run_tests.py --syntax`
- [ ] Commit：`feat: 价格簇分析模块与单元测试`

### Task 2: TOC + Core + Config（数据层）

**Produces:** `AHSnipe.db`（`{interval=30, discount=0.65, items={[itemID]={name,threshold=nil|铜,enabled=true}}}`）、`AHSnipe.Config.AddItem(itemID,name)` / `RemoveItem(itemID)` / `SetThreshold(itemID,铜|nil)` / `SetEnabled(itemID,bool)` / `Save()`，斜杠 `/ahsnipe`（开面板）、`/ahsnipe scan`（手动触发一轮）

- [ ] 写 `.toc`（Interface 120100，SavedVariables: AHSnipeDB，按顺序加载 7 个文件）
- [ ] `Core.lua`：`AHSnipe = {}`，`ADDON_LOADED` 初始化 SavedVariables（含默认值合并），注册 `AUCTION_HOUSE_SHOW`/`AUCTION_HOUSE_CLOSED`（打开→`Scanner.Start()`，关闭→`Scanner.Stop()`），接口版本检测非正式服禁用
- [ ] `Config.lua`：数据结构 + 增删改查（itemID 为键的字典，保持升序数组索引供 UI 遍历）
- [ ] 语法检查通过；游戏外无逻辑可测，以语法+代码审查为准
- [ ] Commit：`feat: 插件骨架与配置数据层`

### Task 3: Scanner 查询状态机

**Produces:** `AHSnipe.Scanner.Start()` / `Stop()` / `ScanRound()`；命中评估：每物品算门槛（手动值优先）→ 单价≤门槛的 listing 收集 → 一轮结束取**门槛最高**的命中 → `SnipeWindow.Show(hit)`；内部串行：每次仅 1 个查询，事件到货或 10 秒超时后隔 1 秒切下一个，节流未就绪（`IsThrottledMessageSystemReady()` false）时等待

- [ ] commodity 路径：缓存 `isCommodity`（`GetItemKeyInfo` 异步重试）；`RefreshCommoditySearchResults` → 等 `COMMODITY_SEARCH_RESULTS_ADDED` 分批至 `HasFullCommoditySearchResults` 或静默 2 秒无新增 → 读全部结果
- [ ] 非 commodity 路径：`SendSearchQuery(MakeItemKey(id), {Enum.AuctionHouseSortType.Price}, false)` → 等 `ITEM_SEARCH_RESULTS_UPDATED` → 读结果，仅保留 `buyoutAmount ~= nil`，unitPrice=buyoutAmount/quantity
- [ ] 每轮遍历所有 enabled 物品，全部完成（或 AH 关闭）即本轮结束；30 秒后下一轮
- [ ] 语法检查 + 人工沿状态机走查（关闭 AH 时取消所有 pending timer/event）
- [ ] Commit：`feat: 扫描状态机与命中评估`

### Task 4: BuyQueue 购买执行

**Produces:** `AHSnipe.BuyQueue.BuyCommodity(itemID, totalQuantity, expectedTotal)`、`BuyItems(itemList)`（按单价升序逐条 PlaceBid，间隔 0.3s，`ITEM_PURCHASED` 计数成功，结束输出汇总到聊天框+弹窗）

- [ ] commodity：`StartCommoditiesPurchase` → `COMMODITY_PRICE_UPDATED` → `ConfirmCommoditiesPurchase`；成功/失败事件收尾，输出"花费 X 金 Y 银 Z 铜"
- [ ] 非 commodity：升序循环 PlaceBid，失败（事件无响应）跳过继续
- [ ] 语法检查
- [ ] Commit：`feat: 购买队列与汇总`

### Task 5: SnipeWindow 模态弹窗

**Produces:** `AHSnipe.SnipeWindow.Show(hit)` / `Hide()`；内容：物品名、单价、数量、总价（GetCoinTextureString）、诱饵标注（红色"疑似诱饵，注意秒删"）、【全部购买】【忽略】；播放提示音（`SOUNDKIT.ACCOUNT_WARNING`）

- [ ] 居中 modal frame + 遮罩（`UIPanelDialogTemplate` 或自绘 backdrop），弹窗期间不被新命中覆盖（队列排队，关闭后处理下一个）
- [ ] 全部购买：commodity → `BuyCommodity(itemID, 命中总量, 预期总价)`；非 commodity → `BuyItems`
- [ ] 语法检查
- [ ] Commit：`feat: 模态狙击弹窗`

### Task 6: ConfigPanel 配置面板

**Produces:** `/ahsnipe` 开面板；物品列表（ScrollFrame，每行：物品名、门槛 EditBox（空=自动显示计算值）、启用 CheckButton、删除按钮）；顶部：添加悬停物品按钮（记录 `TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Item, ...)` 最近悬停 itemID）、间隔/折扣两个 EditBox

- [ ] Shift+点击背包物品快速添加：`hooksecurefunc("ChatEdit_InsertLink", ...)` 捕获 `item:(%d+)`，面板打开时添加
- [ ] EditBox 失焦/回车即存盘（Copper 解析：支持 `6g50s` 格式或纯数字按金）
- [ ] 语法检查
- [ ] Commit：`feat: 配置面板`

### Task 7: 游戏内手工验证 + 收尾

- [ ] 输出手工验证清单到设计文档（对应设计 §10），由用户游戏内验证：
  1. 添加/删除/启停物品，重启保留
  2. 开/关 AH 窗口扫描启停
  3. 弹窗价格与 AH 一致、诱饵标注正确
  4. 全部购买：物品进包、金币扣减、汇总数字正确
  5. 多物品同轮命中只弹门槛最高者
- [ ] 更新设计文档状态为"已完成"
- [ ] Commit：`docs: 标记任务完成`

## 评审修复记录（2026-10-13）

Code Review 发现 3 项重要 + 4 项建议问题，全部修复：

1. 非 commodity 购买汇总时机错误 → BuyQueue 改为事件驱动（ITEM_PURCHASED 推进，2 秒超时计失败）
2. 失败计数恒为 0、手动购买误计 → 超时计 failed + itemID 归因过滤
3. 弹窗内购买汇总不可见 → OnBuyFinished 在弹窗空闲时重新展示
4. 并发 guard 不对称 → 统一 `summary` guard，重复购买提示
5. EndRound 残留 roundTimer → 调度前 Cancel 旧计时器
6. 非法门槛输入静默清空 → 仅空输入清空，非法输入保留原值并提示
7. unitPrice floor 偏差 → 门槛比较用未取整值，展示侧 floor

设计文档旧 API 引用（RequestCommodityAuctions / AUCTION_HOUSE_HIDE）已同步为 12.x 实际 API。

## 第二轮修复记录（2026-10-13，用户游戏内反馈 4 项）

1. **打开 AH 自动呼出配置面板** → Core.lua 在 `AUCTION_HOUSE_SHOW` 调 `ConfigPanel.Open()`，`AUCTION_HOUSE_CLOSED` 调 `Close()`；新增 `Panel.Open/Close/GetFrame`
2. **列表显示价格、可随时修改** → Config 增 `SetLastPrice/GetLastPrice`（Scanner 每物品扫描后写入），配置面板行新增价格列，`Panel.UpdatePrices(itemID)` 轻量刷新（不重建列表，避免打断门槛输入）
3. **方便添加物品** → 新增"从背包选择"按钮：`Panel.ToggleBagPicker()` 遍历 `Enum.BagIndex.Backpack..ReagentBag` 收集去重物品（`IsBound` 过滤灵魂绑定），点击条目即添加；Shift+点击（ChatEdit_InsertLink）保留
4. **点击购买无反应** → 根因：SnipeWindow 模态遮罩原为弹窗子 frame 且 EnableMouse(true)，层级压住"全部购买"按钮 → 改为 UIParent 子 frame + FrameLevel(f-1)，Show 前同步层级
5. **窗体拖拽** → 配置面板/背包选择器/狙击弹窗均加 `SetMovable + RegisterForDrag + OnDragStart/Stop`；配置面板 `EnableMouseWheel(true)` 支持列表滚轮
6. **购买链路诊断日志** → `Config.Log`（SavedVariables 保留 200 条），`/ahsnipe log` 输出到聊天框可全选复制反馈；SnipeWindow/BuyQueue 全链路埋点（点击购买、购买入口、节流状态、Start/Confirm/成功/失败/超时、PlaceBid/ITEM_PURCHASED）
7. 新增 `COMMODITY_PRICE_UNAVAILABLE` 事件处理（价格失效计失败）

### 第二轮 Code Review 修复（4 项）

1. `ITEM_PURCHASED` payload 可能为 itemLink 字符串 → 兼容 number/string 两种形式解析归因
2. commodity 超时用 `C_Timer.After` 不可取消、残留打断下次购买 → 改 `C_Timer.NewTimer`，`FinishSummary` 统一 Cancel
3. 价格列无法从有价回到"--" → `UpdatePrices` 补 else 分支
4. 遮罩 FrameLevel 仅创建时设定，SetToplevel 动态提升后可能失效 → Show/OnBuyFinished 前同步 `mask:SetFrameLevel(f:GetFrameLevel()-1)`

## 第三轮修复记录（2026-10-14，用户游戏内反馈 3 项）

1. **Shift+点击不添加、只会设 AH 搜索** → 根因（官方源码核实）：12.x 背包 Shift+点击走 `ChatFrameUtil.InsertLink`，AH 可见时其内部直接 `AuctionHouseFrame:SetSearchText` 并 return，旧函数 `ChatEdit_InsertLink` 不再被调用 → 改 hook `ChatFrameUtil.InsertLink`（hooksecurefunc 表形式），命中时添加监控并打印提示
2. **从背包选择无效果** → 双因：① 可交易判定改用 `GetContainerItemInfo` 官方 `isBound` 字段按格过滤（移除 ItemMixin 异步方案）；② 面板贴屏幕右缘时选择器锚右被推出屏幕 → 每次打开按 `UIParent:GetLeft/GetRight` 与面板位置选边（右够锚右、左够锚左、否则居中）；补空列表提示
3. **手动扫描** → 新增 `Scanner.ScanNow()`：取消轮次计时器、打断当前轮、立即扫描，扫描结束后的 30 秒从本次手动起重新计时；入口为面板"手动扫描"按钮 + `/ahsnipe scan`

### 第三轮 Code Review 修复（3 项）

1. 选边误用 `GetScaledRect()` 第二返回值（top）当右边界 → 改 `UIParent:GetLeft()/GetRight()`
2. ScanNow 打断会遗留未跟踪的间隙/节流重试 `C_Timer.After` 回调 → 引入轮次代际 `roundGen`，ScanNow/Stop 递增，旧轮回调校验代际失效
3. isBound 以首个堆叠为准可能误判 → 改按格过滤（仅统计未绑定堆叠）

## 第四轮修复记录（2026-10-14，用户游戏内反馈 3 项）

1. **星级显示错误** → 根因：正式服材料星级不是 itemQuality，之前误用物品品质显示 ★。改用官方 `C_TradeSkillUI.GetItemReagentQualityByItemInfo`（已核实 API 文档，返回 1-3）：`Config.UpdateItemInfo` 第4参数改存 `reagentQuality`，`GetItemDisplayName` 用其显示 ★N；DB 迁移清空旧星级字段触发重取；RequestMissingInfo 条件补 `reagentQuality == nil`
2. **手动扫描按钮被收藏列表覆盖** → 根因：锚点 `RIGHT` 的 y 偏移相对 frame **垂直中心**（420高→-210），按钮实际落到 -312 即滚动列表内部，后创建的滚动区盖住它 → 改 `TOPRIGHT` 锚定到 -102 行（滚动区 -134 起，不重叠）
3. **扫描价格与 AH 不一致** → 两个已核实的根因：
   - `COMMODITY/ITEM_*` 搜索事件是 **SynchronousEvent**（在 Refresh/SendSearchQuery 调用内同步触发）：原代码先发查询再建计时器 → 同步事件先 FinishItem，之后创建的残留超时计时器 10 秒后重复 FinishItem → queueIndex 多次递增 → **跳过后续物品**（部分物品价格永不更新）→ 改为先建计时器再发查询，FinishItem 头部加 `waitItemID ~= itemID` 防重入 guard
   - 官方 UI（Blizzard_AuctionHouseCommoditiesList.lua）同时监听 `COMMODITY_SEARCH_RESULTS_RECEIVED`/`_UPDATED`，我们只监听了 `_ADDED` → 补注册并处理这两个事件
   - 新增全链路扫描日志：轮次开始/结束、每物品查询路径（commodity/item）、无命中/0挂单/无门槛分支 → `/ahsnipe log` 可确认扫描无误

### 第四轮 Code Review 修复（2 项）

1. ITEM_KEY 10秒超时兜底未清 `waitItemID`，迟到事件可能误取消下一次查询 → 超时回调先清 waitItemID
2. "无门槛"日志文案与前置条件矛盾 → 改为"有挂单但未算出门槛"

## 第五轮：扫描密度优化（2026-10-14，策略讨论后实施）

目标：30 秒周期太长难抓短窗口诱饵，提升扫描密度。

1. **闸门事件驱动**：闸门未就绪从 `C_Timer.After(2)` 轮询改为 `waitingForGate` 标记 + `AUCTION_HOUSE_THROTTLED_SYSTEM_READY` 事件唤醒；挂 5 秒兜底自愈计时器防事件丢失（Stop/ScanNow/事件恢复时均取消）
2. **物品切换间隔** 1s→0.1s：贴节流闸门跑，闸门由暴雪控制不会过载
3. **轮间隔默认** 30s→8s（DefaultDB + Init 迁移旧值 30→8；面板下限仍为 5）
4. **交互触发扫描**：用户在 AH 内浏览物品（`ITEM/COMMODITY_SEARCH_RESULTS_UPDATED` 中 itemID ≠ waitItemID）时 `Scanner.Kick()` 0.5 秒防抖后立即开一轮（running 中不打断）；用 `ourQuery`（API 同步窗口标记）与 `lastQueriedItemID`（迟到结果）双重过滤，防止自身查询绕过轮间隔

### 第五轮 Code Review 修复（3 项）

1.【中】自身查询超时后迟到的结果事件会误触发 Kick，使 8 秒间隔失效 → 加 `lastQueriedItemID` 过滤
2.【中】闸门事件等待无兜底，事件丢失则轮次永久挂起 → 加 5 秒自愈计时器（gen 代际保护）
3.【低】死变量 `kickTimer` → 删除

## 第六轮修复（2026-10-14，用户反馈）

1. **查询 0 条挂单**（实为超时误报）→ 根因（官方 RefreshSearchResults 源码核实）：`RefreshCommoditySearchResults` 仅对已有结果缓存的物品生效，无缓存时需走 `SendSearchQuery` 完整查询，原代码无条件 Refresh 导致无请求 → 10 秒超时读空。修复：按 `HasSearchResults(itemKey)` 分支；日志区分"查询超时，本轮跳过"与"查询到 0 条挂单"；静默回调校验 waitItemID 防旧缓存误读
2. **购买完成后弹窗弹两次** → 根因：购买完成 `OnBuyFinished` 自动重新弹窗展示汇总（与聊天框汇总重复）；"有时没弹"是因为恰有排队命中占用弹窗。修复：不再自动重弹，汇总仅聊天框输出，`OnBuyFinished` 仅记日志；移除失效的 resultFS 相关代码

## 第七轮：即时弹窗 + 连续轮询（2026-10-14）

目标：诱饵窗口只有几秒，8 秒轮间歇仍可能错过；命中必须当场看到、轮次要尽量密。

1. **命中当场弹窗**：Scanner `FinishItem` 单物品命中即 `SnipeWindow.Show(hit)`（原为轮末汇总取门槛最高者展示）；移除轮末弹窗死代码
2. **连续轮询**：轮间歇默认 0（DefaultDB + Init 迁移旧值 8→0；`SetInterval` 允许 0，负数非法；面板文案"轮间歇（秒，0=连续）"），`EndRound` 中间歇 0 时仅留 0.2 秒让步间隙，速度由节流闸门天然限制
3. **弹窗去重与冷却**（SnipeWindow）：展示中同物品新命中原地更新（不入队、不重播提示音）；排队按 itemID 去重只保留最新；关闭后同物品 10 秒冷却抑制重复弹窗（诱饵挂着没被买走会被反复扫到）

### 第七轮 Code Review 修复（2 项）

1.【中】连续轮询下 Kick 每 0.5s 从 `queue[1]` 重启轮次，排位靠后的物品被饿死、在途查询作废浪费闸门额度 → running 中只置 `pendingKick`，`EndRound` 后立即补一轮（`Pause`/`ScanNow` 时清除，防恢复后意外立即开轮）
2.【中】排队命中上台前的 0.3s 窗口内同物品已有更新命中上台（current），旧快照会覆盖 current 导致展示与购买用过期 matched/auctionID → 上台前校验 current，同物品则丢弃旧快照

## 第八轮：弹窗抑制改内容感知 + 购买前刷新（2026-10-14，用户反馈）

问题：诱饵还挂着、手动都能买，软件却不再提示（关闭冷却 10 秒盲抑制）；点击购买时挂单已被买走，用扫描时的旧 auctionID 下单必然失败。

1. **冷却改签名抑制**：SnipeWindow 用挂单签名（物品=auctionID 集合；commodity=单价x数量集合）替代 10 秒时间冷却——签名不变（用户已看过/已忽略的那批）不重复弹，签名变化（新挂单、部分被买走、价格变动）立即重新提示；展示中原地更新时同步签名
2. **购买前刷新**：新增 `Scanner.RefreshHit(hit, callback)`——点击"全部购买"后对非 commodity 物品重新查询，按当前挂单与门槛重建命中再下单（commodity 本就按服务器实时价购买且有门槛复核，不刷新）；刷新会打断当前轮独占查询通道，回调后自动恢复轮询；刷新失败按原因汇报（已被买走或下架/价格已回涨/查询超时/拍卖行关闭）
3. **失败即解除抑制**：BuyQueue 购买有失败时 `SnipeWindow.ClearSuppression(itemID)`，允许下一轮立即重新提示剩余挂单

## 第九轮：面板精简 + 系统星星 + 购买成功格式订正（2026-10-14，用户反馈）

1. **面板精简**：移除"添加悬停物品"按钮、悬停跟踪（TooltipDataProcessor hook）、"从背包选择"按钮及整个背包选择器、"输入物品ID"输入框；添加物品仅剩 Shift+点击背包物品（hook `ChatFrameUtil.InsertLink` 保留，面板留提示行"Shift+点击背包物品添加监控"）；同步删除 ParseMoney 等死代码
2. **标题进标题栏**："AHSnipe 配置"改用 BasicFrameTemplate 自带 `TitleText`（模板无 TitleText 时回退自绘）
3. **废弃手动门槛**：列表行移除门槛输入框与"(自动)"提示，名称列加宽；`Scanner.GetThreshold` 一律按默认折扣系数计算（主簇最低价 × discount）；`Config.SetThreshold` 删除，Init 迁移清空旧门槛值；GetItems 不再返回 threshold 字段
4. **系统星星**：`Config.GetStarMarkup` 三级回退——优先 `Professions.GetIconForQuality`（pcall 保护防签名不符炸错），其次品质阶图集 `Professions-Icon-Quality-TierN-Inv`（运行时 `C_Texture.GetAtlasInfo` 探测存在性，铜/银/金对应 1/2/3 阶，镶嵌界面同款），最后回退文字 ★N
5. **购买成功格式订正**：第八轮误将"单价/数量/总价"整体移除，实际需求是去掉文字标签、保留数值——恢复为"购买成功：物品名，单价 * 数量 = 总价"（金币纹理串，`ReportBuy` 增加 unitPrice/quantity/totalPrice 参数）

## 测试与命令

- 单测+语法：`python tests/run_tests.py`（含 `--syntax` 全文件 loadfile 检查）
- lupa 已安装（`pip install lupa -i https://pypi.tuna.tsinghua.edu.cn/simple`）
- 游戏内无法自动测试，PriceCluster 逻辑由单测覆盖，其余模块靠语法检查+代码审查+手工清单

## 第十轮修复（2026-10-14，用户反馈：commodity 数量稍大就几乎必然"价格已回涨"失败，很少成功）

现象（日志）：扫描命中 13 个单位 @13.60g（门槛 15.72g，2 条挂单），点击全部购买后服务器返回 **26.35g 平价** → 超门槛整单放弃；x1 小量却经常成功。

根因（三个叠加）：

1. **延迟**：命中 → 弹窗（还会被其他命中排队占住数秒，本次 4 秒）→ 人工点击才发 `StartCommoditiesPurchase`；热门商品（矿石）的低价簇几秒内就被其他玩家/脚本买走；
2. **一次性全量下单**：`StartCommoditiesPurchase(itemID, 扫描总量)` 把整个请求按"当前最便宜挂单"定价，低价簇被吃掉一部分后，剩余数量也被拉到市场价，均价超门槛被**一票否决**——哪怕还有便宜单位剩着也全军覆没。总量越大，低价簇需要完整存活的窗口越长，失败率越高；x1 因为请求量小、当前最便宜的那个单位大概率还在，所以经常能成；
3. commodity 手动购买没走"购买前刷新"（非 commodity 第八轮已修），用几秒前的旧总量下单。

修复：

1. **commodity 分块逐条购买**（恢复设计 §7"按单价从低到高逐条买、失败跳过"）：`BuyQueue.BuildCommodityChunks(matched)` 按单价升序把命中挂单切成块（同价先买小堆），逐块 `StartCommoditiesPurchase → 门槛复核 → ConfirmCommoditiesPurchase`，块间 0.3s；超门槛/价格不可用/服务器失败/10s 超时只放弃该块并继续下一块；汇总按块计数并统计成交数量
2. **commodity 手动购买也走购买前刷新**（与非 commodity 一致）：点击后先 `RefreshHit` 按当前挂单重建命中再分块下单，避免用旧总量；刷新通道不可用（用户暂停/查询超时）时回调新增第三参 `staleOk=true` 允许回退原命中分块购买（服务器侧仍有实时门槛复核）；"已被买走/价格已回涨"等市场性原因不回退
3. **新增"命中自动购买"开关**（`Config.autoBuy`，默认关，配置面板复选框）：Scanner 命中即走购买流程，不等弹窗与点击，把"检测→下单"延迟从数秒压到 1 秒内——这是数量大时成交率低的根因；开启后不弹窗，结果在聊天框逐笔汇报；购买入口被占用（summary guard）时回退弹窗，用户仍可手动买
4. `Scanner.Pause(ahClosed)` 区分"用户暂停"（AH 仍开着，允许回退旧数据）与"拍卖行关闭"（禁止回退）；`Stop` 传 `true`
5. 单测新增 `BuildCommodityChunks` 三组用例（升序/同价小堆优先/非法数量过滤）

### 第十轮热修复（2026-10-14，用户反馈：点击购买完全无反应、无任何消息）

根因：`Scanner.lua` 两处裸调 `Log(...)`（RefreshHit 内第八轮预埋 + 自动购买日志），但该文件从未定义 `local Log = Config.Log`（只有 BuyQueue/SnipeWindow 定义了别名）→ 全局 `Log` 为 nil，点击购买在 RefreshHit 的 Log 行即报错中断，任何消息都未及输出；默认 `scriptErrors=0` 不弹错误框，表现为"完全没反应"。此前未暴露是因为 commodity 购买不走 RefreshHit（直接 Buy），且非 commodity 路径一直存在同样问题。

修复：Scanner.lua 顶部补 `local Log = Config.Log`（与其它模块一致）。已全量排查其余文件无同类裸全局引用。游戏内若再遇"无反应"，先 `/console scriptErrors 1` 显示 LUA 错误再反馈。

## 第十一轮修复（2026-10-14，用户反馈：现在几乎买不到东西，连一两个鱼饵都抢不到，不如手动快）

现象（日志）：扫描命中 1 个单位鱼饵 @2.89g → 点击 → 购买前刷新确认鱼饵还在（2575021.5）→ **购买消息 2.2 秒后才拿到服务器报价（14.85g）** → 超门槛放弃。对比第九轮：点击后 0.2 秒即收到报价。

根因：
1. **购买调用发生在 AH 搜索结果事件回调栈内会被节流系统推迟约 2 秒送达服务器**——第十轮给 commodity 点击路径加了"购买前刷新"，购买调用变成 `RefreshHit→sink→callback→Buy`，正处在刷新查询的结果事件栈里；鱼饵就死在这 2.2 秒。刷新本身对 x1 场景也无价值（服务器报价即最新价，数量由分块购买按块兜底）
2. 扫描与购买共用 AH 消息通道，购买在途时继续扫描会挤占购买消息的发送时机

修复：
1. **commodity 点击后立即直接下单**（回到第九轮干净调用栈）：分块购买内部逐块门槛复核，被部分抢走只损失对应块
2. **整单因"实际单价超门槛"失败的补救重试**：`MaybeRetryWithFresh`——全部块因价格失败后，用当前挂单刷新重试一次，按"现在还能便宜买到的数量"重新分块；快速路径零成本（只有整单失败后才多一次刷新）；`hit.isRetry` 标记防重试循环
3. **购买 API 一律不在事件栈内调用**：自动购买（Scanner.FinishItem 内）与非 commodity 刷新回调均 `C_Timer.After(0, ...)` 延迟一帧下单
4. **购买在途暂停扫描**：`BuyQueue.Buy` 入口 `Scanner.Pause()`，`FinishSummary` 恢复；`Resume` 增加用户暂停守护（购买期间用户手动暂停不会被误恢复）；刷新入口增加 `BuyQueue.IsBusy()` 判断，购买中点击给出"已有购买进行中"准确原因
5. RefreshHit 回退为第八轮两参形态（staleOk 机制随 commodity 刷新移除而失去使用者）

用户手动更快的本质：手动时 AH 客户端空闲、消息通道干净。自动购买（面板"命中自动购买"）配合本轮的事件栈外下单 + 在途停扫，正是把插件拉到同样的通道条件。

## 第十一轮补充修复（2026-10-15，用户日志：自动购买连续超时失败，手动成功率更高）

日志证据：`购买入口 节流就绪=false` → `购买开始 x1` → **10.0 秒后 `购买超时（未收到价格响应）`**（价格阶段，即 StartCommoditiesPurchase 零响应）；同段时间还有两条扫描查询超时（结果事件未回），AH 通道整体拥塞。

根因：自动购买"命中即下单"必然把购买消息紧邻搜索响应发出（闸门刚被搜索消耗、节流就绪=false），消息被节流系统扣住；手动点击时刻通道通常已静默，所以成功率高。与第十轮"事件栈内下单被推迟 2.2 秒"是同一通道拥塞现象的不同程度。

修复：

1. **通道静默等待后才发送购买消息**（`BuyQueue.WhenThrottledReady`）：`quietBuy` 模式（自动购买/刷新后购买/重试）下等 `IsThrottledMessageSystemReady()` 且距上次 AH 消息发送 ≥0.5s 才发，5 秒超时兜底仍发；手动点击 commodity 不等待（保持已验证的手动路径速度）
2. **Scanner 记录 AH 消息发送时刻**（`NoteAHSend`/`TimeSinceAHSend`），查询与购买发送都记账，作为静默判断依据
3. **价格阶段超时 = 购买未发生，取消后重发同一块一次**（消息可能已在拥塞通道里丢失）；确认后的超时不重发（可能已成交，防重复购买）
4. 超时日志分阶段（价格响应/购买结果），购买入口日志增加"静默等待"标记，便于下次定位

待观察：若日志显示静默等待长达数秒，说明搜索后闸门冷却期长，自动购买的出手速度上限受此约束——那将以手动点击路径为主（用户实测成功率更高），或考虑降低扫描密度给通道留余量。

## 第十一轮补充修复二（2026-10-15，自动购买连续超时的三轮日志排查）

**日志一**：价格阶段超时（10 秒零响应），购买入口 `节流就绪=false`。
**日志二**：重发时无静默等待日志（闸门就绪+通道静默+扫描已停），仍 10 秒零响应——证伪"通道拥塞"理论。
**日志三**：`已发送 StartCommoditiesPurchase（闸门就绪=true）`、pcall 无报错，仍零响应——证伪"API 静默报错"理论。

排查过程中加的观测（均保留）：

1. **pcall 包裹全部购买 API**（`SafeCall`）：计时器回调里的 Lua 错误会被静默吞掉（scriptErrors 关闭时零提示），表象就是假超时
2. **发送时刻日志**：`已发送 StartCommoditiesPurchase <item> x<q>（闸门就绪=?）`
3. **`/ahsnipe diag` 全事件诊断**（`Config.diagAH` 持久化，Core 诊断帧）：记录所有拍卖行事件（价格响应/不可用/成功/失败/闸门就绪/AH错误等，不含搜索类噪音）
4. 超时日志分阶段（价格响应/购买结果）

**判定与修复**：消息确实发出、闸门就绪、服务器零响应 → 判定为**被丢弃的购买消息卡住了购买会话**，后续 Start 被忽略。`SendCommodityPurchase` 改为**先 Cancel 再 Start**，清掉悬挂会话让新请求被受理（无会话时 Cancel 为无害空操作）。

**待验证**：① 修复后自动购买恢复 → 坐实悬挂会话；② 仍无响应则开 `/ahsnipe diag` 做一次自动购买+一次手动购买对比：手动也无响应=会话卡死（关 AH 重开可恢复，需加自动恢复逻辑），手动有响应=触发上下文差异（计时器 vs 硬件点击），再查计时器路径。

## 第十一轮补充修复三（2026-10-15，定位：计时器上下文发的购买消息被静默丢弃）

第四轮日志实证：自动购买（计时器上下文发送，无论是否等静默）连续 10 秒无响应；同场手动购买（OnClick 立即发送，闸门未就绪也成功）0.2 秒拿到报价成交。汇总全部日志：

| 发送上下文 | 结果 |
|---|---|
| OnClick 直接发 | ✅ 稳定成功 |
| AH 事件栈内直接发（第十轮刷新回调） | ✅ 成功（约 2 秒慢递） |
| 计时器/OnUpdate 上下文发（延迟一帧/静默轮询/重发） | ❌ 全部静默丢弃（10 秒零响应，pcall 无报错） |

**根因：从计时器回调发出的 AH 购买消息被客户端静默丢弃**（非节流、非报错、非服务器不响应——消息凭空消失）。前几轮加的"延迟一帧""静默等待轮询"恰好全部把发送点置于计时器上下文，逐一踩坑。

修复（回到唯一被实证可行的形态）：

1. **自动购买改为在搜索结果事件栈内直接调用 `BuyQueue.Buy`**（不再 `C_Timer.After(0)`，不等闸门、不等静默）；`FinishItem` 增加 `if not queue then return end` 守护（购买内 Pause 会清空 queue，防止推进本轮时对 nil 取长度）
2. **非 commodity 刷新回调、超时重试的购买也改为事件栈内直接发送**（去掉延迟一帧包装）
3. 移除全部等待机制（`WhenThrottledReady`/quietBuy/NoteAHSend/静默轮询）及相关常量
4. 保留：Cancel 后再 Start（清悬挂会话）、pcall 错误捕获、发送时刻日志、分阶段超时诊断、`/ahsnipe diag` 全事件诊断

预期代价：事件栈内发送约 2 秒送达（第十轮实测），成交延迟比手动点击慢，但远好于 10 秒超时。若仍失败，则差异在"计时器触发 vs 硬件点击"的根源，自动购买需重新评估（保留手动路径为主）。

## 第十一轮补充修复四（2026-10-15，自动购买仍未成功：改为强制诊断 + 移除暂停）

第五轮日志：事件栈内直接发送（无暂停前的最后一次形态）仍 10 秒无响应。累计证据：**所有扫描器触发的购买全部失败，所有硬件点击触发的购买全部成功**——与闸门状态、调用栈、发送时机均无关，唯一相关变量是触发源。

已做：
1. **移除购买期间的扫描暂停**（第十一轮为"让出通道"加的 `Scanner.Pause()`）：实测反效果——AH 消息队列靠持续的查询流量冲出去，暂停后购买消息无人冲；第十轮（无暂停）能拿到报价、手动点击（点击输入本身冲队列）能成功，均与此吻合
2. **购买期间强制开启 AH 事件诊断**（`Buy` 设 `diagRestore`、`FinishSummary` 还原）：不再依赖用户手动 `/ahsnipe diag`，每次购买自动抓取"服务器回了什么"的直接证据（价格响应/闸门就绪/静默三种可能）

待用户提供一次自动购买的 `/ahsnipe log` 判定：
- 出现 `AH事件 COMMODITY_PRICE_UPDATED` → 已恢复，收工；
- 10 秒内只有 `AH事件 AUCTION_HOUSE_THROTTLED_SYSTEM_READY`（通道活着）但没有价格响应 → 消息送达但服务器不处理（会话卡死），需研究恢复手段；
- 连 `AH事件` 都没有 → 消息根本没上链路，只有硬件点击能发起购买 → 自动购买形态不可行，回归"检测+弹窗一键购买"（该形态用户已验证好用），自动购买开关默认关闭并标注实验性。

## 第十二轮：日志可用性（2026-10-15，用户反馈：时间戳看不懂、只能截图）

1. **时间戳改为墙上时钟**：`Config.Log` 输出 `[月-日 时:分:秒] 消息`（原来用 GetTime 客户端运行时长，人类不可读且无法判断是否最新）；亚秒级间隔改由关键日志行自带相对值——价格响应/超时均输出"距发送X.Xs"
2. **新增 `LogWindow.lua`（`/ahsnipe log` 打开）**：多行 EditBox + 滚动区承载日志，游戏内点击文本框 Ctrl+A 全选、Ctrl+C 复制即可粘贴给开发者，**不再需要截图**；打开时自动滚动到底部（最新条目）；聊天框只留一行打开提示，不再刷屏 200 条
3. 新增文件已加入 TOC 与 Core 初始化链；`/ahsnipe diag` 的全事件诊断不变

## 第十三轮：自动购买定论 + 购买期间弹窗遮挡修复（2026-10-15）

**自动购买结论：不可行，认输。** 十二轮全部发送形态（事件栈直发/延迟一帧/等闸门/等静默/取消重发/暂停与不暂停扫描）逐一试过，最终日志实锤：扫描器触发的购买 0 成功，硬件点击触发 100% 成功（点击后 0.1-0.2 秒即拿到报价）。判定为客户端"仅输入事件发起的 AH 消息才被投递"的硬规则，Lua 层无法绕过。扫描查询在计时器上下文大多能成功（命中每 2 秒一次），偶发超时——说明该规则对购买消息更严格。

处置：
1. 自动购买保留代码与开关（默认关），面板标注"实验性，成交率低，推荐关闭"，开启时打印醒目提示
2. **修复购买期间弹窗遮挡**（影响手动链路的重要 UX bug）：购买进行中不再弹模态窗——此前失败的自动购买持续 20 秒，期间每 2 秒弹一次窗挡住屏幕、用户点击全被"已有购买进行中"拒绝；现在命中只记日志，下轮重新扫到，购买结束后自然弹出
3. 自动购买尝试前先查 `IsBusy`，消除"购买被拒绝"刷屏

**最终形态**：扫描检测（分块门槛计算、内容感知抑制）+ 弹窗一键购买（手动，0.2 秒级报价、可靠成交）+ 整单超门槛后的刷新重试。该形态用户已验证好用。

## 第十四轮：版本变动防护（2026-10-15，用户反馈：插件在跑但界面不弹、无“已加载”）

现象：扫描似乎在查价，但配置面板不出现；登录时没有“已加载”行；其他插件因版本变化大批报错。

分析：冒烟测试（mock 环境全链路执行）证明代码happy path 完好——问题是有一步静默报错中断了后续步骤（`AUCTION_HOUSE_SHOW` 里 `Scanner.Start` 若报错，后面的 `ConfigPanel.Open` 永执行不到；或面板创建/刷新链上某个版本敏感 API 失效）。

修复（全部带日志，错误不再被吞）：

1. **Core.lua 增加 `SafeStep` 包装**：ADDON_LOADED 各 Init、AUCTION_HOUSE_SHOW/CLOSED 的 Start/Open/Stop/Close 全部 pcall，单步失败不影响其余步骤，错误写诊断日志
2. **ConfigPanel.Open 加固**：`CreateFrameUI` 失败时聊天框明确提示“配置面板打开失败，请 /reload 重试”；`RequestMissingInfo`/`RefreshList` 失败不挡 `frame:Show()`（列表为空也要把面板显示出来）
3. **版本敏感 API 全部 pcall**：`C_TradeSkillUI.GetItemReagentQualityByItemInfo`、`C_Texture.GetAtlasInfo`、`CreateAtlasMarkup`、`Professions.GetIconForQuality`（原有）
4. **SnipeWindow/LogWindow 窗口创建**同样 pcall + 日志
5. 配置面板加 `SetToplevel(true)`，保证压在拍卖行窗口之上
6. **新增冒烟测试**（`tests/run_tests.py` 的 `smoke_test()`）：mock WoW 环境真实执行全部 8 个文件，驱动 加载→初始化→开拍卖行→面板弹出→购买链路→斜杠命令，专抓“语法正确但运行期炸错”类回归（本轮即靠它排除代码嫌疑）

用户侧待确认：`/reload` 后看 `/ahsnipe log` 有无 `步骤失败 xxx` / `界面步骤失败 xxx` 行——有则按报错修对应 API；若无则检查插件列表是否标记“过期”（版本号变更后 TOC 的 Interface 需相应上调）。

## 第十五轮：移除自动购买（2026-10-15，用户确认无法实现）

用户确认面板问题已解决（版本变动防护生效），并要求移除自动购买的全部 UI 与逻辑。

移除内容：
1. `Config.lua`：`autoBuy` 字段、Init 校验、`IsAutoBuy/SetAutoBuy` 访问器、头注释
2. `ConfigPanel.lua`：自动购买复选框及标签整块移除，列表滚动区锚点从 -130 还原为 -104（多出一行空间）
3. `Scanner.lua`：`FinishItem` 的自动购买分支整体移除，命中只走弹窗；保留“购买进行中不弹窗”守护

保留内容（均对手动购买链路有值）：
- commodity 分块购买、逐块门槛复核、整单超门槛后的刷新重试（`MaybeRetryWithFresh`）
- 价格阶段超时后取消+重发一次（丢消息恢复）、pcall 错误捕获、发送时刻日志
- 代码注释中保留历史结论记录（为什么购买 API 必须在事件栈内直接发、为什么不能暂停扫描等），防止未来重复踩坑

## 第十六轮：购买成功事件栈丢包 + 超时重发死分支（2026-10-15，用户日志：命中4单位只买到2）

现象（11:41 日志）：命中 2.89g×2 + 2.99g×2 两条挂单分 2 块购买。第 1 块（OnClick 栈）0.3s 拿到报价成交；第 2 块在 `COMMODITY_PURCHASE_SUCCEEDED` 事件栈内发送，10s 零回包；计时器重发（11:42:01，闸门就绪=true）仍零回包；再超时计块失败。11:41:52 扫描证实 2.99g×2 挂单还在——货没丢，消息丢了。最终"成功1 失败1"。

新实证：**购买生命周期成功事件栈（COMMODITY_PURCHASE_SUCCEEDED / ITEM_PURCHASED）与搜索/报价结果事件栈不是同一种通道，前者外发购买消息同样被客户端静默丢弃**。第十三轮"事件栈内约 2 秒可拿报价"的结论仅对搜索/报价结果事件栈成立。

修复（BuyQueue.lua）：

1. **块/条目成功后不再续发下一条**：成功事件内若还有剩余块（commodity）或剩余条目（非 commodity），结束本次购买并置 `summary.deferred`，聊天框提示"剩余N块稍后重新提示"；`FinishSummary` 在 failed>0 或 deferred 时清除弹窗抑制，聚焦扫描下一轮（约 1 秒）按剩余挂单重新弹窗，由用户点击继续（硬件点击是唯一实证可靠通道）
2. **删除超时重发死分支**：计时器栈发送必丢（本轮日志再次实锤：重发后仍 10s 零回包），价格响应/结果超时改为 `CommodityFailEnd`——当前块计失败后直接结束本次购买，剩余交给下次弹窗；`COMMODITY_PURCHASE_FAILED`（同属购买生命周期事件栈）也改为失败即收尾不再推进；非 commodity 的 PlaceBid 超时同理不再从计时器栈推进下一条
3. 失败事件栈内（COMMODITY_PRICE_UNAVAILABLE / 报价超门槛）的块推进保持不变（属报价结果事件栈，第十三轮实证可用）
4. 发送时机纪律注释全面改写（新纪律 1/2/3）

另修（ConfigPanel.lua）：`RequestItemInfo` 裸调全局 `GetItemInfo` 在 12.x 客户端为 nil（11:41:47 日志 `attempt to call a nil value`），新增 `GetItemInfoCompat`：优先 `C_Item:GetItemInfo`、回退全局，两形态各自 pcall，单个 API 失效不再中断整批物品信息补全。

## 第十七轮：报价否决改为收尾 + 扫描新鲜度门禁（2026-10-15，用户日志：法力百合看得到诱饵却不提醒）

现象（13:51 日志）：快照命中 20 单位（1 条 x18@2.89g + 1 条 x2@2.99g）。第 1 块 x1 从点击栈发出、0.5s 报价 2.89g 成交；第 2 块 x18 同栈发出、0.4s 报价 **6.10g** 被门槛否决——反推低价簇只剩 `18×(8.13−6.10)/(8.13−2.89)=6.97` 单位，与同一秒扫描显示的 7 吻合。否决事件栈随即推进第 3 个请求 x2，**10 秒零回包**（只有 THROTTLED_SYSTEM_READY），期间扫描每秒都看见 7→4 个便宜货却全走"购买进行中，本轮不弹窗"，最后被别人买光（13:51:47—13:51:55）。

新实证：**报价否决事件栈外发下一块同样被静默丢弃**，第十六轮第 3 条"失败事件栈推进保持不变"的假设作废。同一次点击里两次点击栈发送 0.4/0.5s 拿报价、一次否决栈发送零回包，唯一变量就是发出所在的事件栈。

修复（BuyQueue.lua）：

1. **报价超门槛 → 否决即收尾**（`CommodityFailEnd`，不再 `CommodityFailChunk`）：剩余块与本块同源同份过期快照，基于旧数量再发大概率是死请求，且会把"购买进行中不弹窗"压满超时时长；收尾后 `FinishSummary` 清抑制，≤1 秒的聚焦扫描带着**当前**数量重新弹窗
2. **报价阶段超时 10s→3s**（新增 `COMMODITY_PRICE_TIMEOUT`，计时器统一由 `ArmCommodityTimer(plan)` 按阶段挂表，切入结果阶段重新挂表）：正常回包 0.3—0.5s、最坏 2.2s，10s 只是死等；死等的真实代价是这 10 秒内弹窗全被压住。3 秒即可收尾；一个死请求压住弹窗的时长从 10 秒降到 3 秒
3. **删除 `MaybeRetryWithFresh`**（整单超门槛失败后 `C_Timer.After(0.5)`→`RefreshHit`→自动下单的补救路径）：`plan.priceFailed` 已无置位点成为死代码；且用户确认自动购买整体不可实现（见第十五轮），无需保留第二条自动续买路径。连带删除 `plan.hit`/`retried`/`hit.isRetry` 字段；`RefreshHit` 仍由 SnipeWindow 的物品（非 commodity）点击路径使用

诊断（Scanner.lua，用户主诉"提醒延迟越来越严重"）：

4. **新鲜度门禁**：查询超时/静默到点时，若本次查询未收到任何结果事件（`freshSeen`）则**不读旧缓存**、直接跳过本轮，避免"上一轮快照当本轮结果"造成门槛与数量双双失真；连续 3 次零回包说明依赖的事件形态不成立，自动 `freshGateDisabled` 退回读缓存并记日志（自愈，不会永久瞎掉）
5. **新增三类去重诊断日志**：`无命中`（最低/门槛/主簇单价与数量/主簇占比/挂单条数，同签名只记一次——用于判断是不是主簇被天量错价挂单自我污染而静默不命中）、`旧缓存拦截`、`迟到回包（距发送X.Xs）`；聚焦饿死量化：`聚焦轮次：N 个商品，其余M个本轮跳过（聚焦已持续X秒）`

测试：`python tests/run_tests.py --syntax` 10/10 通过，8 个 Lua 文件 SYNTAX-OK。待游戏内复现验证：`无命中` 占比、`旧缓存拦截`/`迟到回包` 是否触发、`聚焦已持续X秒` 上限、否决后是否 1 秒内带新数量重弹。

## 第十八轮：一键购买按键 + 命中提醒强化（2026-10-16）

诉求：用户提出用外部程序模拟点击实现自动购买。已明确拒绝——向客户端注入键鼠/读内存以绕过"购买 API 需硬件事件"属暴雪禁止的第三方自动化，绕过 Warden、封号不可申诉。改为在插件内部压缩剩下的物理延迟：现有链路其实只有一次点击（OnClick 发 Start → 报价事件栈内自动 Confirm），瓶颈是"把鼠标挪到按钮并按下"这 1～2 秒。

实现（SnipeWindow.lua）：

1. **键盘购买**：全局命名 `SecureActionButtonTemplate` 按钮 `AHSnipeKeyBuy`，`Win.Init` 常驻创建（alpha 0 + 不接收鼠标 + 必须保持 Show，隐藏帧收不到 Click）。`/ahsnipe bind <按键>` → `SetBindingClick`，`/ahsnipe unbind` 清绑，按键持久化在 `AHSnipeDB.bindKey` 并在登录后重新应用；另一条路径是宏 `/click AHSnipeKeyBuy` 自行绑键。鼠标【全部购买】与按键共用 `Win.TriggerBuy(source)`，仍在硬件栈内直发、不包计时器
2. **提醒强化**：命中新增整屏红色闪烁（OnUpdate 0.2s 冲顶 + 0.6s 淡出，`EnableMouse(false)` 不挡按钮）、屏幕上半部中央横幅（物品名+合计+按键提示，6 秒或弹窗关闭即撤）、弹窗未关闭期间每 1.5 秒重播提示音。两项分别由 `flashAlert` / `repeatPing` 持久化开关，配置面板第二行提供切换按钮与当前绑定按键回显
3. 版本不确定性按项目纪律处理：横幅字体名逐个探测（`GameFontNormalHuge` → `GameFontNormalLarge` → `GameFontNormal`）并把命中名写入日志；`PlaySound` 保持改版前调用形态并 pcall，失败只记一次日志。提醒层任何异常都不影响弹窗与购买链路

测试：`run_tests.py` 冒烟新增绑键→弹窗→触发 `AHSnipeKeyBuy` OnClick→断言 `BuyQueue.Buy` 收到命中且弹窗关闭→解绑，以及关闭两个提醒开关后仍正常弹窗；mock 补 `SetBindingClick`/`SetBinding`、命名帧注册表 `NAMED`、数值型 getter（`GetFrameLevel` 返回 Mock 会让建窗在 pcall 里静默失败，这是本轮先暴露后修掉的坑）。10/10 单测 + 8 文件 SYNTAX-OK 通过。

待游戏内验证（用户日志）：`按键购买 F -> /click AHSnipeKeyBuy` 是否写入、按键时 OnClick 是否触发（无弹窗时按一次应在日志出现"按键购买：当前无弹窗命中，忽略"，出现即证明按键链已通；按钮已同时注册 AnyDown/AnyUp，不会因双派发重复下单，因为第一次已清空 current）。命中后按键应在 0.5 秒内拿到报价（与鼠标点击同栈则说明安全按钮通道成立，若再现"零回包 10 秒超时"则该通道与计时器栈同样被丢弃，回退为只用鼠标）。

## 第十九轮：弹窗改可购买列表（2026-10-16，用户反馈：提醒晚了、看不见还有什么能买）

问题：模态弹窗一次只摆一个目标，同挂单签名还会抑制重复提示，而诱饵数据本身就有 **4~10 秒延迟**（节流闸门 ~1 查询/秒、全监控轮 8~9 秒；上一批改动刚把 `roundStartAt/querySendAt/seenAt` 三个数据年龄字段打进 hit，实测排队段就占 4~10s）——用户看到提示时那批货常常早没了，而"现在还有什么能买"在弹窗模型里永远看不见。第十八轮的键盘购买只压掉"挪鼠标+按下"这 1~2 秒，救不了"视野里只有一个目标"本身。

处置：提醒模型从"弹窗+抑制"整体换成**常驻非模态可购买列表**（规格 `docs/superpowers/specs/2026-10-16-ahsnipe-buylist-design.md`，计划 `docs/superpowers/plans/2026-10-16-ahsnipe-buylist.md`）。下列决策均由用户逐条拍板。

1. **方案 A（用户选定）**：命中加行、重复命中**原地静默刷新**（连续轮询每秒都会刷新，不重响不重排日志），列表即"此刻还能买什么"的唯一真相；模态遮罩、排队队列、`lastShownSig` 签名抑制、`ClearSuppression` 全部废弃。
2. **行只读、无行内按钮**（"我点不过来"）：每行只有 商品（图标+显示名）/单价（matched 最低价）/数量/总价 四列小号字体，不设行内【买】【忽略】。
3. **唯一购买入口 = 底部固定按钮【买队首】**：行按 `hit.seenAt` 降序，最新鲜的排队首，点一下对队首整单下单（BuyQueue 分块内核不动）。购买在途时按钮置灰显示"购买中…"、该行前缀"购买中"，**不做"记下这次点击稍后补发下一条"**——计时器/事件上下文发出的购买消息会被客户端静默丢弃（第十一轮补充修复三、第十六轮实证），结算复亮后再点自然轮到新队首。
4. **键盘一键购买整层删除**（第十八轮的遗产）：`AHSnipeKeyBuy` 命名安全按钮、`/ahsnipe bind`/`unbind`、`Config.bindKey`，连同 `flashAlert`（整屏闪烁+中央横幅）、`repeatPing`（连响）两个提醒开关一起下架——列表常驻后按键与闪烁都失去意义，提示音固定为"仅新增行响一次"。
5. **删行证据只认三种**：① 扫描用**新鲜数据**证实无低价（miss 分支调 `MarkGone`；`timedOut` 时读到的可能是部分/缓存结果，不算证据、不删行，防误删仍可买的行）；② 整单全部成功（`bought>0` 且 `failed==0` 且非 `deferred`）；③ 关闭拍卖行清空（过期快照不许再点）。有失败或剩余的块**保留行**，等下轮刷新数字或证实失效；`RefreshHit` 的购买刷新 sink 不走 miss 分支，天然不会误删。
6. **列表窗即插件主界面**（用户二次确认）：打开拍卖行不再自动弹配置面板（Core 的 `AUCTION_HOUSE_SHOW` 只启扫描），面板改由列表窗底部【配置】按钮按需打开；面板"当前价格"列随之删除，其唯一数据源 `Config.SetLastPrice/GetLastPrice` 与 `lastPrices` 快照整体废弃——推进顺序为"先 Scanner 停写快照、再与价格列同批删除"，保证每个中间提交都可运行。
7. **空列表也常驻**：显示灰字"暂无可购买物品（命中后自动入列）"，否则【配置】【买队首】入口只在命中后才出现，等于把主界面藏进偶发事件里。

实现：新模块 `AHSnipe/SnipeList.lua`（行管理 + 渲染 + 买队首 + 配置入口 + 跟随 `AUCTION_HOUSE_SHOW/CLOSED` 自管显隐，窗位存 `AHSnipeDB.listPos` 可拖动持久化）；`Scanner.FinishItem` 命中改调 `SnipeList.Add(hit)`（购买在途也照常刷新行，旧"购买进行中不弹窗"分支随弹窗模型一起废弃）、新鲜不命中调 `SnipeList.MarkGone(itemID)`；`BuyQueue.FinishSummary` 改调 `SnipeList.OnBuyFinished(summary)`；`Core` 初始化链改调 `SnipeList.Init`；删除 `AHSnipe/SnipeWindow.lua`（TOC 顶替、Notes 改口），`Scanner.RefreshHit` 的唯一使用者变为 `SnipeList.TriggerBuy` 的非 commodity 分支。行图标用 `C_Item.GetItemIconByID` 尽力而为（pcall + 失败只记一次日志，取不到只显名称），符合 12.x 形态未全实证的项目纪律。

测试：`run_tests.py` 新增 `test_config_migration`（旧绑定/提醒字段与 `lastPrices` 清除、`interval` 旧值迁移）、`test_snipe_list`（加行+响一次／同物品原地刷新不重响／`seenAt` 排序／`MarkGone` 删行）、`test_snipe_list_buy`（买队首=买最新鲜、在途拒绝再次点击、整单成功删行、失败保留）、`test_buqueue_notifies_list`（结算回调 summary 的 `itemID/bought/failed`）；smoke 步骤 2 断言反转为"打开拍卖行列表常驻、配置面板不自动弹出"，原"一键购买"步骤整段替换为"可购买列表（加行/排序/买队首/成功删行）"，命中提醒开关步骤删除。`python tests/run_tests.py --syntax` **14/14 单测通过、8 个 Lua 文件 SYNTAX-OK**（SnipeList 顶替 SnipeWindow，文件数不变）；全量 grep 确认 `SnipeWindow/bindKey/FlashAlert/RepeatPing/KeyBuy/LastPrice/lastPrices/UpdatePrices` 除 `Config.Init` 的迁移置位外无残留。

### 第十九轮 Code Review 修复（2 项）

1.【中】拖动存位取错返回值：`GetPoint()` 返回 `point, relativeTo, relativePoint, x, y` 五个值，初版只取三位 → `relativePoint`（字符串）被当作 x 存进 `listPos`，重开时锚点全坏。修正取值，并把**默认锚点与存/取统一到 UIParent 坐标系**（原先默认钉在拍卖行框体右侧、拖动后存成 UIParent 相对，再开就整体偏移）。
2.【低】测试 mock 帧对未知键返回函数而非 nil，`f.rowFS or {}`、`math.max(#rows, f.lastRows or 0)`、`if f.emptyFS then` 这类 `or` 兜底会把函数喂进 `#`/`math.max`，首轮渲染即炸。改 `type(...)`/`tonumber(...)` 判断，并在建窗时给 `f.lastRows = 0` 初值。

待游戏内验证（用户执行，逐项核对）：

1. `/reload` → 打开拍卖行：**只有可购买列表出现**（空列表显示"暂无可购买物品"，配置面板不再自动弹出）；点【配置】能打开面板、面板里不再有价格列；拖动列表窗后重开位置保持。
2. 造诱饵价 → ≤1 轮内加行+响一声；同物品连续命中只原地刷新不重响。
3. 诱饵被买走/回涨 → 行自动消失（日志"除行 …（扫描证实无低价）"）。
4. 点【买队首】→ 买的是 seenAt 最新一条；购买在途按钮置灰"购买中…"；结算复亮后再点轮到新队首。
5. 整单成功 → 该行立即消失（日志"除行 …（购买完成）"）。
6. 关闭拍卖行 → 列表清空收起；`/ahsnipe log` 无"步骤失败"、无星级/图标刷屏。

遗留问题（本轮未修）：

1. **聚焦（focus）饥饿**：命中即续期 20 秒的聚焦窗口（`FOCUS_DURATION`）+ 聚焦期 1 秒轮间歇（`FOCUS_GAP`），只要诱饵一直挂着没人买走，聚焦就不断续期、其余监控物品整轮不被查询，"首报即尾批"的延迟随时长累积。第十七轮已把现象量化进日志（`聚焦轮次：N 个商品（…），其余M个本轮跳过（聚焦已持续X秒）`），但挤占上限与退让策略仍未定。
2. **"手工看得见诱饵但插件没提醒"的漏报排查仍在等日志**：等带数据年龄的 `入列 …（数据X.Xs=排队X.Xs+回包X.Xs）` 新日志回证据，再判定属于新鲜度门禁拦截、`无命中` 静默（主簇被天量错价挂单自我污染）还是排队/聚焦延迟。

## 第二十轮：首页交互再收敛（2026-10-16，用户验收第十九轮后提出）

诉求（用户原话拆解）：①【手动扫描】【暂停】挪出配置面板、放到列表上面；②首页窗口默认就要有高度，不能空列表时塌成一条；③【买队首】改名【立即购买】四个字、按钮放大、放在列表下面。

实现（`SnipeList.lua` + `ConfigPanel.lua`，购买链路零改动）：

1. **扫描控制上移为首页顶栏**：建窗顶部 `TOPLEFT 14,-34` 放【手动扫描】84×22，右侧 4px 放【暂停/继续】64×22，点击回调与三条 print 文案从 ConfigPanel 逐字搬运，`Scanner.ScanNow/SetPaused/IsPaused` 调用点不变；建窗时按 `IsPaused()` 回显初始文案（暂停状态本就持久化在 `Config`，跨会话一致）。ConfigPanel 删除 `scanBtn`/`pauseBtn`，滚动区 `TOPLEFT` 由 `-128` 收紧为 `-96` 补上腾出的空间。理由：列表既然被认定为主界面，"扫不扫、什么时候扫"这种日常操作就不该藏在二级窗口里。
2. **默认高度与三段式布局**：新增 `ROW0_Y = -78`（第一行 y）、`FOOT_H = 52`（底部按钮区）、`MIN_H = 260`；建窗 `SetSize(430, MIN_H)`，`Render` 高度改为 `max(MIN_H, -ROW0_Y + 行数×ROW_H + FOOT_H)`——前 7 行都停在默认高度内，之后才继续长高；行锚点与空列表提示统一改用 `ROW0_Y`，避免首次命中时窗体猛跳、也避免空列表塌成一条把入口挤没。
3. **购买按钮改名放大**：`买队首` → `立即购买`，150×24 → 200×34，锚 `BOTTOMRIGHT -14,12`；【配置】改 70×22 at `BOTTOMLEFT 14,18`，两个按钮垂直中心对齐。行为完全不变（仍是"点一下买队首整条"），只把名字改成不含内部术语、按钮做大让抢购时不用瞄准。顺带把原先挂在按钮上方的单个提示 `hintFS`（"单价 数量 总价"）换成三个与数据列同 x（190/262/300）对齐的列头 `GameFontDisableSmall`，列头不再随窗口高度漂移。
4. **措辞同步**：`BuyQueue.lua` 结算注释、`AHSnipe.toc` Notes、`tests/run_tests.py` 的 docstring/注释/SMOKE 文案里的"买队首"改口；`SnipeList` 头部注释补上新布局说明。

测试：`python tests/run_tests.py --syntax` **14/14 单测通过、8 个 Lua 文件 SYNTAX-OK、exit 0**。`load_snipe_list` 的 MOCK_ENV 无需改动即可构造新增按钮与列头（建窗走 pcall，帧/字体方法齐备）。

待游戏内验证（补充第十九轮清单）：打开拍卖行后首页顶部即有【手动扫描】【暂停/继续】，配置面板里这两个按钮消失、列表区上移不空挡；空列表时窗口保持默认高度不塌；列头与单价/数量/总价三列对齐；点【立即购买】行为与改名前一致（买 seenAt 最新一条、在途置灰"购买中…"）。

遗留问题不变：聚焦饥饿未修、"手工看得见诱饵却不提醒"仍在等带数据年龄的 `入列` 日志（见第十九轮"遗留问题"）。

## Self-Review 结论

- 设计 §3–§11 全部映射到 Task 1–7；API 均已对照 Blizzard 官方文档源核实（12.x 无 BuyItem/BuyCommodity）
- 无占位符；接口签名在各任务 Produces 中给出
