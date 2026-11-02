# 待办与未修问题清单

本文件登记"已经查清但尚未动手"的问题。每条都带 `文件:行号` 证据；未写行号的判断一律标为"待取证"。
2026-10-29 的 AHSnipe 审查由静态阅读 + 全库引用扫描（`AHSnipe/*.lua`、`AHSnipe.toc`、`tests/run_tests.py`）得出，
**没有改动任何 `.lua`**；下面两节的修法只是候选，落地前逐条等用户确认。

## AHSnipe 审查发现汇总（2026-10-29，未修）

**基线**：`PYTHONIOENCODING=utf-8 python tests/run_tests.py` → `71/71 单测通过` + `SMOKE-OK 加载 8 个文件` 等 6 条冒烟 + `PASS smoke`；
`--syntax` → 8 个文件全部 `SYNTAX-OK`。审查在这一片绿上做，因此下面列的都是"测试覆盖不到的死角"。

**结论计数**：可安全清理的死代码 11 条（下一节）；需要确认才能修的 bug 8 条（第三节）；注释/文档口径落后 5 条（本小节末尾）。

**"已废弃 / 零调用 / 唯一使用者"类论断逐条核对（这是本轮的主要核对项）**

| 论断出处 | 核对方法 | 结论 |
|---|---|---|
| `BuyQueue.lua:8`、`BuyQueue.lua:122-124`「已废弃：按挂单分块串行购买」 | grep `BuildCommodityChunks` / `CommodityNextChunk` / `CommodityFailChunk` / `chunks` / `deferred` | **成立**：以上标识在 `AHSnipe/` 全部 8 个文件里 0 命中；现行形态是一次点击一个请求（`BuyQueue.lua:213-234` `CommodityStart`） |
| `Config.lua:75-79`「旧弹窗时代字段整体废弃」（bindKey / flashAlert / repeatPing / lastPrices） | grep 这四个字段名 | **成立**：`AHSnipe/` 里只剩 `Config.lua:76/77/78/79` 的 `= nil` 迁移置位，无任何读取点；`SetLastPrice`/`GetLastPrice` 连定义都不存在（全库仅 `tests/run_tests.py:176` 断言它们已消失） |
| `Config.lua:85`「手动门槛已废弃」 | grep `SetThreshold` / `it.threshold` | **成立**：`Config.SetThreshold` 全库 0 命中；门槛唯一来源是 `Scanner.lua:202`（`main.minPrice * Config.GetDiscount()`），旧值仅在 `Config.lua:93` 清空 |
| `ConfigPanel.lua:157`「旧函数 ChatEdit_InsertLink 不再被调用」 | grep `ChatEdit_InsertLink` | **就本仓库成立**：插件不再 hook 它，唯一 hook 是 `ChatFrameUtil.InsertLink`（`ConfigPanel.lua:159-161`）。该句描述的是客户端行为，游戏外无法复测，维持第四轮取证 |
| 计划第十九轮「`Scanner.RefreshHit` 的唯一使用者变为 `SnipeList.TriggerBuy` 的非 commodity 分支」 | grep `RefreshHit` | **成立**：定义 `Scanner.lua:635`，全库唯一调用点 `SnipeList.lua:633` |
| 计划第二十一轮「删除 `MaybeRetryWithFresh`」/ 第二十二轮「删除 `noEventStreak`/`freshGateDisabled`」/ 第十一轮「移除 `WhenThrottledReady`/`quietBuy`/`NoteAHSend`」 | grep 这些标识 | **全部成立**：`MaybeRetryWithFresh`、`noEventStreak`、`freshGateDisabled`、`priceFailed`、`hit.isRetry`、`quietBuy`、`WhenThrottledReady`、`NoteAHSend` 在 `AHSnipe/` 里均为 0 命中 |
| `BuyQueue.lua:301` 注释「是否有购买在进行中（**扫描暂停**的常见原因，供刷新入口给出准确原因）」 | 对照 `Scanner.lua:404-417`、`Scanner.lua:23-29` | **措辞落后**：第二十六轮起购买在途不再暂停扫描，而是让路（一条不发、0.5 秒复查）；`IsBusy` 的四个读取点（`Scanner.lua:94`、`Scanner.lua:636`、`SnipeList.lua:186`、`SnipeList.lua:613`）语义已不含"暂停" |

**其余文档口径落后（只列，本轮不改计划正文除「当前状态」块以外的部分）**
1. 计划「文件结构」小节仍写 `SnipeWindow.lua`、缺 `SnipeList.lua` 与 `LogWindow.lua`——TOC 实际加载 8 个文件（`AHSnipe.toc:9-16`）。
2. 计划「Global Constraints」与 Task 2 写 `## Interface: 120100` / "按顺序加载 7 个文件"——TOC 现为 `## Interface: 120105,120100`（`AHSnipe.toc:1`）。
3. 计划 Task 4 的 Produces 签名 `BuyQueue.BuyCommodity(itemID, totalQuantity, expectedTotal)`、`BuyItems(itemList)` 与现行 `(hit)` 形态不符（`BuyQueue.lua:236`、`BuyQueue.lua:273`）。
4. 计划 Task 6 的"添加悬停物品按钮 / 门槛 EditBox / 价格列"均已下架（`ConfigPanel.lua:351-356` 只剩一行 Shift+点击提示）。
5. 计划里 `10/10`、`14/14`、`24/24`、`27/27`、`56/56`、`63/63`、`64/64` 是各轮历史测试数，现行为 `71/71`。

**顺带记录的事实（无害，仅为取证）**：关闭拍卖行时事件处理顺序是 Core 帧（`Core.lua:90-92`，文件加载期注册）→ SnipeList 帧（`Core.lua:70` 注册）→ BuyQueue 帧（`Core.lua:72` 注册），
所以 `SnipeList.lua:676-679` 先把行清空，`BuyQueue.lua:425-435` 的"购买中断"结算随后才调 `OnBuyFinished`，此时 `DropRow`/`RewriteRowQty` 找不到行（`SnipeList.lua:501-527` 返回 false）。
结果与"关窗即清空过期快照"的设计一致，用户不可见，故不列为 bug。

## AHSnipe 可安全清理的死代码（只列，不要删）

"安全"的定义：生产代码零读取点、且删除不需要改测试。第 11 条是唯一例外，已单独标注。

1. `Config.GetDB`（`Config.lua:97-99`）——全库 0 引用；与 `AHSnipe.DB()`（`Config.lua:21-23`）完全重复，后者有真实读取点（`Core.lua:111`、`LogWindow.lua:57`）。
2. `Config.GetQualityColor`（`Config.lua:204-206`）——全库 0 引用；`QUALITY_COLOR` 表本身仍在用（`Config.lua:198-201` 定义、`Config.lua:300` 读取），只删这个访问器。
3. `Panel.GetFrame`（`ConfigPanel.lua:387-390`）——生产端 0 引用，唯一引用是测试（`tests/run_tests.py:2096`，断言"建窗本身要跑得通"）；删它要同步改测试，故严格说属第 11 条那一类。
4. `LogWindow.Toggle`（`LogWindow.lua:75-82`）与只被它调用的 `LogWindow.Hide`（`LogWindow.lua:71-73`）——`/ahsnipe log` 走 `Show`（`Core.lua:109-110`），二者全库 0 外部引用；且 `Toggle` 自身还有隐患（见 bug 节第 4 条），先定去留再谈修。
5. `LogWindow.Init`（`LogWindow.lua:84-86`）——函数体只有注释，`Core.lua:74` 仍 `SafeStep` 调用；属"空钩子"，可删可留（留着等于给后续初始化留位）。
6. `local PC = AHSnipe.PriceCluster`（`ConfigPanel.lua:5`）——该文件里 `PC` 只出现这一次，纯残留（`Scanner.lua:3` 有自己的一份）。
7. `AHSnipe.frame = frame`（`Core.lua:7`）与 `AHSnipe.diagFrame = f`（`Core.lua:43`）——赋值后全库无读取点。
   对照：`Scanner.frame`（`Scanner.lua:758`）、`BuyQueue.frame`（`BuyQueue.lua:448`）、`List.frame`（`SnipeList.lua:682`）都有测试读取点（`tests/run_tests.py:1164`、`tests/run_tests.py:1333`、`tests/run_tests.py:626`、`tests/run_tests.py:606`），**不是**死代码，别一起删。
8. `elseif event == "PLAYER_LOGOUT"` 分支（`Core.lua:85-87`）——该事件从未注册（注册只有 `Core.lua:90-92` 的 ADDON_LOADED / AUCTION_HOUSE_SHOW / AUCTION_HOUSE_CLOSED），分支永不进入，分支体本身也只是注释。
9. `isBaitLike` 字段及其上游：`Scanner.lua:175`（`isBaitLike = anyBait`）、`Scanner.lua:149`+`Scanner.lua:158-160`（累计 `anyBait`）。全库 Lua 侧 0 读取点——红色"疑似诱饵"标注随弹窗模型一起删除后没人消费它，唯一出现处是测试夹具 `tests/run_tests.py:249` 造数据。
10. `Config.GetItems()` 返回值里的 `quality`（`Config.lua:165`）——调用方只读 `itemID`/`name`/`enabled`（`Scanner.lua:517-539` 的 `BuildRoundQueue` 入参、`ConfigPanel.lua:261-270` 的行渲染）；`quality` 的实际读取点是 `Config.lua:299`，直接从 DB 取，不经 GetItems。删字段前先读 bug 节第 1 条，同一处代码要一并处理。
11. `PC.GetThreshold`（`PriceCluster.lua:51-57`）——生产端 0 调用（`Scanner.lua:202` 自己算门槛），但被 5 条单测钉住（`tests/run_tests.py:72`、`:103`、`:112`、`:121`、`:137`），它同时是 `PC.IsBaitLike` 之外的第二处"纯测试 API"。
    **连带项**：`PC.IsBaitLike`（`PriceCluster.lua:60-63`）生产端唯一消费点就是第 9 条那个死字段（`Scanner.lua:158`），其余引用是 `tests/run_tests.py:143-152`。
    这两条删了要动测试口径，不属于"安全清理"，列出备查。

## AHSnipe 需要用户确认才能修的 bug

1. **每次打开配置面板都全量重查所有监控物品（星级条件恒真）**
   `ConfigPanel.lua:146-152` 的补全条件是 `if not it.name or it.reagentQuality == nil`，但 `Config.GetItems()` 的返回表只有 `itemID/name/quality/enabled` 四个字段（`Config.lua:153-170`），
   **从不返回 `reagentQuality`**，所以 `it.reagentQuality` 恒为 nil → 每个监控项每次开面板都会走一遍 `RequestItemInfo`，命中成功分支后各自触发一次整表 `Panel.RefreshList()`（`ConfigPanel.lua:128`）。
   后果：N 个物品 → N 次全量列表刷新（`RefreshList` 内部还有同名分组的双重循环，`ConfigPanel.lua:236-256`）+ N 次异步加载请求；把 `Config.lua:12-18` 精心做的显示名缓存和"非热点路径"前提整个打掉。
   候选修法（二选一，都需要确认）：`GetItems` 补返 `reagentQuality`，或条件改读 `Config.GetItem(it.itemID).reagentQuality`。
   现状无测试覆盖：`config_panel_info_retry`（`tests/run_tests.py:2269-2279`）只断言"加载请求报错后重开面板要再试一次"（`calls >= 2`），不测"信息已完整的物品不该再被请求"。

2. **总价列没取整，与单价列口径不一致**
   `SnipeList.lua:266` 单价列用 `coin(math.floor(row.unitPrice))`，`SnipeList.lua:270` 总价列直接 `coin(row.totalPrice)`。
   非 commodity 的单价是 `buyoutAmount / quantity`（`Scanner.lua:242`），行上的总价按它累加（`SnipeList.lua:146`），因此可以带小数（例如 100 铜 ÷ 3），
   于是把浮点铜币喂给 `C_CurrencyInfo.GetCoinTextureString`——该 API 的浮点入参形态本仓库未实证，展示还可能与"单价 × 数量"手算对不上。
   候选修法：`coin(math.floor(row.totalPrice))`（一处改动，无测试断言当前是小数形态）。

3. **`/ahsnipe` 主入口的刷新链没有保护，与【配置】按钮不对称**
   `Panel.Open` 用 `SafeUI` 包了 `RequestMissingInfo` 与 `RefreshList`（`ConfigPanel.lua:395-396`），
   `Panel.Toggle` 却裸调（`ConfigPanel.lua:412-413`）。第二十七轮 Bug3 只统一了建窗（`EnsureFrame`，`ConfigPanel.lua:376-385`），刷新链漏了。
   而 `/ahsnipe` 走的正是 `Toggle`（`Core.lua:123-124`），列表窗【配置】走的才是 `Open`（`SnipeList.lua:375-379`）——即"更常用的那条路径没有防护"。
   版本变动导致 `RefreshList` 里某个 API 失效时，`Toggle` 会抛未捕获错误、面板仍不出现，正是第十四轮要防的那类静默中断。

4. **`LogWindow.Toggle` 建窗 unprotected 后立刻解引用**
   `LogWindow.lua:75-82`：`BuildFrame()` 裸调（`Show` 那边是 `pcall`，`LogWindow.lua:52-56`），紧接着 `frame:IsShown()`——建窗失败时 `frame` 仍为 nil，直接报错。
   它现在是零调用方（死代码节第 4 条），所以先确认"删掉 Toggle/Hide"还是"补齐保护并挂到斜杠命令"。

5. **购买期间的诊断开关会覆盖用户的手动选择**
   `BuyQueue.lua:40-42`+`BuyQueue.lua:317-320` 在购买入口记下 `diagRestore = IsDiagAH()` 并强制开诊断，`FinishSummary` 无条件还原（`BuyQueue.lua:54-57`）。
   若用户在购买进行中敲 `/ahsnipe diag` 想开诊断（`Core.lua:114-121` 是纯翻转，不区分"购买强制"与"用户主动"），结算时会被悄悄关掉——恰好抹掉他想要的取证窗口。
   候选修法：还原前判断当前值是否仍等于购买开始时写入的 true（只回滚自己那次改动）。

6. **`manualOnce` 轮次把 `pendingKick` 留在原地，下一个自然轮末尾白跳一次轮间歇**
   `Scanner.lua:491` 的 `if manualOnce then manualOnce = false; return end` 排在 `Scanner.lua:493-497` 的 pendingKick 消费之前。
   手动扫描那一轮里由 `Scanner.Watch`（`Scanner.lua:690-691`）或浏览 `Kick`（`Scanner.lua:667-669`）登记的 `pendingKick` 不被消费，顺延到下一次自然轮结束时立刻补一轮——绕过用户设定的轮间歇。
   影响不大（`ResetWork` 会清，`Scanner.lua:141`），但顺序确实是反的；修法是把 pendingKick 的消费挪到 manualOnce 早退之前，或让 manualOnce 分支也清掉它——需要确认要哪种语义。

7. **【手动扫描】会清空聚焦，和"购后复查"互相抵消**
   `Scanner.ScanNow` 在 `Scanner.lua:626-628` 无条件 `focus = {}; focusSince = 0`。
   而 `Scanner.Watch`（`Scanner.lua:679-696`）专门为"刚买过的那件最可能有续单"把物品挂进聚焦，并且注释里明确说**不能走 `Kick`**（`Scanner.lua:680-682`）就是因为 `Kick → ScanNow` 会清聚焦。
   现在列表顶栏那颗【手动扫描】按钮（`SnipeList.lua:319-329`）调的正是 `ScanNow`（`SnipeList.lua:324`）——用户购后手点一下"手动扫描"，刚挂上的复查优先级就没了。
   需要确认：手动扫描是否应保留聚焦（改成不清 focus），还是维持"手动=完全重扫"的语义。

8. **Shift+点击添加仍要求配置面板处于显示状态，主界面却已换成可购买列表**
   `ConfigPanel.lua:160-161` 的 hook 回调开头就是 `if not frame or not frame:IsShown() then return end`——面板没开就静默什么都不发生。
   第十九/二十轮之后日常主界面是可购买列表（`Core.lua:80-81` 明确不再自动弹面板，入口在 `SnipeList.lua:371-380`），
   而"Shift+点击背包物品添加监控"这句提示还只写在面板里（`ConfigPanel.lua:352-354`）。用户不开面板就加不了物品，且没有任何反馈。
   需要确认：放宽到"拍卖行/列表打开即可添加"，还是在列表窗也加上同一句提示。
