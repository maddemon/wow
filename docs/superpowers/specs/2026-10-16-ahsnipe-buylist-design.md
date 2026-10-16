# AHSnipe 可购买列表（替代狙击弹窗）设计规格

日期：2026-10-16
状态：已与用户逐节确认（方案 A + 去行内按钮/忽略、去快捷键绑定、二次确认"列表即主界面"）

## 背景与目标

现状：命中触发单条模态弹窗（SnipeWindow.lua），同挂单签名抑制重复弹、不同物品排队、
购买在途不弹窗；另有命名安全按钮 AHSnipeKeyBuy 提供一键购买。
问题：模态窗一次只能看一个目标，抑制/排队状态复杂；诱饵数据本身有 4~10 秒延迟
（节流闸门 ~1 查询/秒，整轮 8~9 秒），用户需要"看一眼还有什么能买、无脑点购买"的心流。

目标：把提醒模型从"弹窗+抑制"换成"常驻非模态可购买列表"：
- 命中即加行，重复命中原地刷新；
- 用户只操作一个固定按钮【买队首】，点一下买列表第一条；
- 列表为空即无目标，行被扫描证实失效即自动消失。

非目标：不改 BuyQueue 购买内核（分块串行、门槛复核、忙时拒绝、点击上下文约束全部保留）；
不新增自动购买或自动重试（已实证不可行）。

## 交互设计

### 窗口
- 新文件 `AHSnipe/SnipeList.lua`，模块 `AHSnipe.SnipeList`；删除 `SnipeWindow.lua`（TOC 同步）。
- 常驻非模态小窗 `AHSnipeListFrame`：跟随 AUCTION_HOUSE_SHOW/CLOSED 显隐；关窗时清空所有行
  （关闭后数据失去意义，防止对着过期行下错单）。
- 可拖动，位置存 `AHSnipeDB.listPos`（x/y/锚点，沿用面板既有拖动持久化写法）。
- 拍卖行打开期间**窗口常驻**：列表为空时显示灰色提示"暂无可购买物品"，
  保证【配置】【买队首】入口始终可见（列表即主界面，入口不能靠命中才出现）。
- 小号字体（GameFontSmall 系），紧凑行高。

### 行（无行内按钮）
每行四列：
| 商品（图标+显示名） | 单价（matched 最低价） | 数量（matched 总量） | 总价（hit.totalPrice） |

- 排序：按数据刷新时刻 `hit.seenAt` 降序，最新鲜的排队首。
- `Add(hit)`：同 itemID 已存在 → 原地替换数据并重排（静默，不响）；不存在 → 新增行 +
  提示音 + 行高亮淡出动画。
- `MarkGone(itemID)`：删行。
- 底部左【配置】按钮：`AHSnipe.ConfigPanel.Open()`（普通按钮，不涉购买链路；这是列表成为主界面后
  唯一的图形入口，`/ahsnipe` 斜杠命令仍可切换面板）。
- 底部右唯一购买按钮【买队首】：语义 = 对队首行的 hit 发起整单购买（BuyQueue 内部分块自动串行）。
- 购买在途（BuyQueue.IsBusy）：按钮置灰显示"购买中…"，对应行加"购买中"标记；点击无效。
  不做"记下这次点击稍后自动买下一条"——延迟到计时器/事件上下文发出的购买消息会被客户端
  静默丢弃（已实证）。结算/失败结束那一刻按钮恢复可点，队首即当时最新鲜一条。

### 删除的交互层
- 模态遮罩、排队队列 queue、`lastShownSig` 抑制与 `ClearSuppression`：全部废弃。
- 键盘购买：AHSnipeKeyBuy 命名安全按钮、EnsureKeyButton/BindKey/UnbindKey/KeyButtonName、
  `/ahsnipe bind`、`/ahsnipe unbind` 子命令、Config 的 `bindKey` 字段——整体删除。
- 提醒配置：`flashAlert`（整屏闪烁+横幅）、`repeatPing`（连响）删除；
  列表提示音固定为"仅新增行响一次"。
- ConfigPanel 的闪烁按钮、连响按钮、绑定按键文案一并移除。
- ConfigPanel 的"当前价格"列一并移除（价格只在命中行上看）：删 `priceFSs`、行内价格 FontString、
  `Panel.UpdatePrices`；Scanner 不再调用 `UpdatePrices`。`lastPrices` 快照与
  `Config.SetLastPrice/GetLastPrice` 失去唯一读者，一并废弃（Config.Init 迁移时清掉旧字段）。
- Config.Init 迁移：丢弃 bindKey/flashAlert/repeatPing/lastPrices 字段，新增 listPos 默认值。

## 主界面归属

- **可购买列表 = 主界面**：打开拍卖行不再自动弹出配置面板（Core 的 `AUCTION_HOUSE_SHOW` 里
  删掉 `ConfigPanel.Open`）。旧动机是"随时看价格、改门槛"，价格列取消后配置面板不需要常驻，
  改由列表窗【配置】按钮按需打开。
- 关闭拍卖行时仍一并收起配置面板（`AUCTION_HOUSE_CLOSED` → `ConfigPanel.Close` 保留）。
- 添加监控入口不变：Shift+点击背包物品；`/ahsnipe` 命令仍可切换配置面板。

## 购买链路（不变的部分）

【买队首】/行数据 → TriggerBuy（迁入 SnipeList.lua）：
- commodity：直接 `BuyQueue.Buy(hit)`（服务器报价实时）。
- 非 commodity：先 `Scanner.RefreshHit(hit, cb)` 拿新鲜挂单再 `BuyQueue.Buy(fresh)`，
  购买 API 必须在回调栈内调用（计时器上下文会被静默丢弃，既有实证）。
- 失败原因提示沿用现文案（扫描未运行/物品类型未知/超时/已被买走等）。

## Scanner 对接

`FinishItem` 内改动（[Scanner.lua](../../../AHSnipe/Scanner.lua) 现 L224-241）：
- 命中分支：删除 `BuyQueue.IsBusy()` 跳过弹窗逻辑，一律 `AHSnipe.SnipeList.Add(hit)`。
- 不命中分支（`n > 0` 且 EvaluateHit 返回 nil，即数据新鲜、有挂单、但已无低于门槛价）：
  调用 `AHSnipe.SnipeList.MarkGone(itemID)`。
  注意：只在轮次评估路径调用；RefreshHit 购买刷新 sink 里的不命中【不】删行（那是点击瞬间的事后判定）。
  查询超时（timedOut）与旧缓存拦截不算失效证据，不删行。
- 命中加聚焦（focus）机制保留不动。

## BuyQueue 对接

`FinishSummary`（现调 ClearSuppression/OnBuyFinished 处）改为：
- 整单全部成功 → `SnipeList.MarkGone(s.itemID)` 删行；
- 有失败/剩余 → 保留行（等下轮扫描刷新数字或证实失效再删），仅清 summary 状态。
- `SnipeWindow.ClearSuppression` 调用删除（抑制机制不存在了）。

## 日志

- 入列：`入列 %s 单价%.2fg 数量%d 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs）`
  （时间戳字段 roundStartAt/querySendAt/seenAt 已由上一轮改动打入 hit）。
- 刷新行：不写日志（连续轮询下每 1 秒一条会淹没日志环）。
- 删行：`除行 %s（扫描证实无低价）` / `除行 %s（购买完成）`。
- 弹窗展示/弹窗抑制/购买进行中不弹窗 等旧日志文案随代码删除。

## 错误处理

- 列表窗创建失败：同现 SnipeWindow.Show 的 pcall 兜底，留痕日志。
- Add 收到无 itemID/无 matched 的 hit：不处理（内部构造保证，不加防御）。
- 队首行在点击瞬间已过期：由 RefreshHit 兜底报"诱饵已被买走或下架"，行在下一轮被 MarkGone。

## 测试

`tests/run_tests.py` 调整：
- 删除 keybind/bind 相关 smoke 分支（斜杠 `bind/unbind` 命令不存在后，`smoke:slash` 相应改）。
- 新 smoke：命中 Add 成行（行文本含名称/价格）→ 重复命中同物品行数不变且数字更新 →
  MarkGone 删行 → 两条命中按 seenAt 排序、点【买队首】买入的是最新鲜一条 →
  购买成功回调整单删行。
- Config 迁移测试断言 `bindKey/flashAlert/repeatPing/lastPrices` 全部被清除。
- PriceCluster/BuyQueue 单测不动；Lua 语法检查覆盖新文件。

## 验收

游戏内 `/reload`：打开 AH → 列表窗常驻（空时显"暂无可购买物品"）且配置面板不再自动弹出；
点【配置】打开面板，面板列表不再显示当前价格；制造诱饵价 → 加行+响；
连续轮询同物品 → 原地刷新不重复响；买走/回涨 → 行自动消失；
点【买队首】= 买最新鲜一条，购买在途按钮置灰"购买中…"，结算恢复后继续点即轮到下一条。
