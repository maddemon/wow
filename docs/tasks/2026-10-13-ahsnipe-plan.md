# AHSnipe 插件实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态：全部任务完成（Task 0-7）+ 第二至七轮迭代（游戏内反馈修复 + 扫描密度优化 + 即时弹窗连续轮询）完成，SubAgent Code Review 已执行（七轮共 24 条问题全部修复），7/7 单测通过，7 个 Lua 文件语法检查通过。待游戏内手工验证（见设计文档 §12）。**

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

## 测试与命令

- 单测+语法：`python tests/run_tests.py`（含 `--syntax` 全文件 loadfile 检查）
- lupa 已安装（`pip install lupa -i https://pypi.tuna.tsinghua.edu.cn/simple`）
- 游戏内无法自动测试，PriceCluster 逻辑由单测覆盖，其余模块靠语法检查+代码审查+手工清单

## Self-Review 结论

- 设计 §3–§11 全部映射到 Task 1–7；API 均已对照 Blizzard 官方文档源核实（12.x 无 BuyItem/BuyCommodity）
- 无占位符；接口签名在各任务 Produces 中给出
