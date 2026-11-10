# GoldLedger 全账号日历（月历 → 某天各号 → 某号某天流水）

日期：2026-11-10　状态：**已作废** —— 用户 2026-11-10 决定不改造 GoldLedger，改新建插件、
记账层自己写（路线 B）。现行计划见
[2026-11-10-goldcalendarledger-new-addon.md](2026-11-10-goldcalendarledger-new-addon.md)。
本文档保留的价值只剩一节：第 1 节"数据面事实"是他已验证的现状，读他代码取证时照这里查。

## 0. 要解决的问题

现在 GoldLedger 的界面（主面板四张卡 + History + Breakdown）全部只看**当前角色**，
而它的存档本来就是全账号的。用户要的是：

1. 月历一格一天，每格显示 **收入 / 支出 / 净**，数是所有号加起来的；
2. 点某一天 → 这天**每个角色**的收入 / 支出 / 净 + 一行合计；
3. 再点某个角色 → 该角色这天的**流水**，并且带它已有的**来源分类**（拍卖行 / 商人 / 任务 /  loot / 邮件 / 修理…）。

## 1. 数据面事实（本机已核，不是推测）

- 存档形状：`GoldLedgerDB.characters["Name-Realm"] = { entries, daily["YYYY-MM-DD"]={income,expense}, monthly["YYYY-MM"] }`
  —— 全账号数据已经在同一份档里，[Data.lua:114-124](../../GoldLedger/Data.lua) `EnsureCharacterData`。
- 本机真档实况：`WTF/Account/BLUEFANE/SavedVariables/GoldLedger.lua` 里已有
  `恶灵之力-奥达曼`、`群山之力-奥达曼`、`龙吟-时光之穴`、`赤尾之寂-时光之穴` 四个号，
  `daily` 只有 `2026-11-10` 一天（今天才装）。⇒ 日历第一天就有数，往后逐日长。
- 界面侧只有 [Data.lua:420](../../GoldLedger/Data.lua) `GetAllCharactersSummary(period)` 一个函数看全账号，
  且 `period="day"` 写死成"今天"；其余 `GetDailySummary / GetMonthlySummary / GetChartData /
  GetSourceBreakdown / GetFilteredEntries / GetExportCSV` 一律走 `EnsureCharacterData()` = 当前号。
- 主面板四张卡（今天 / 昨天 / 本次会话 / 本月）都是当前号 —— 这就是"只能看当前角色"与"重复"的来源。
- 记账口径：`AddEntry` 对 `warband` / `guildbank` 两种来源**只进流水、不进任何合计**
  （[Data.lua:173-179](../../GoldLedger/Data.lua) `IsTransferSource`）。日历必须同口径，否则号间转钱会被算成收入。
- `DATE_FORMAT` 必须是 ISO `%Y-%m-%d`：`CleanupOldEntries` 拿日期键**按字符串**比大小。
- UI -kit 现成：`ui:CreatePopup / CreateListRow / CreateScrollFrame / CreateLabel / CreateButton /
  CreatePaginationBar / ShowTooltip`；模块入口现成：`GoldLedger:RegisterFeature` +
  `RegisterHeaderButton` + `RegisterSlashCommand`（`/gl`、`/goldledger`）。

## 2. 任务清单

### T1　Data 层四个只读聚合 API（`GoldLedger/Data.lua`）

| 新 API | 返回 | 取数方式 |
|---|---|---|
| `GetAccountDailySummary(dateKey)` | `{income, expense, net}` | 遍历 `characters[*].daily[dateKey]` 求和 |
| `GetAccountMonthDays(monthKey)` | 1..月末数组 `{day, dateKey, income, expense, net, hasData}` | 遍历 `characters[*].daily`，键前缀匹配 `monthKey..-`；月末天数用 `time()` 表算，不许硬编 30 |
| `GetAllCharactersSummary(period, dateKey)` | 现有函数**加一个可选参数** | `period=="day"` 时用传入的 dateKey；不传照旧=今天（Characters 窗口行为不变） |
| `GetCharacterDayEntries(charKey, dateKey, options)` | `{entries, total, page, totalPages, sourceTotals, grandTotals}` | 按 timestamp 区间过滤（当天 00:00 到次日 00:00），**不许**对每条 entry 调 `date()`；跳过 transfer 来源；分页复用现有 `perPage` 口径 |

判据：
- 全部只读；不写存档、不建表、不缓存到存档。
- 号名排序按名字（当前号是否置顶由 T2 的界面决定，数据层不管次序偏好）。

用例（新建 `tests/test_goldledger_data.py`，lupa 替身，形状照 [test_goldledger_locale.py](../test_goldledger_locale.py)）：
1. 三号同一天求和 = 各号之和；只两号有数时第三号按 0 处理，不报错。
2. `GetAccountMonthDays`：2 月 28/29、月末 30/31 各一天的格子数对；没有任何数据的月返回全空月。
3. transfer 来源（warband/guildbank）不进任何合计，但出现在 `entries` 流水里。
4. 区间过滤边界：次日 `00:00:00` 那条不许混进当天；前一天 `23:59:59` 那条要在当天内。
5. 分页尾页条数、`total` 与实际条数一致。
6. `GetAllCharactersSummary("day")` 不传 dateKey 时仍等于今天（回归门，防我把 Characters 窗口改坏）。

### T2　新模块 `GoldLedger/Modules/Calendar/`（三文件，照 Characters 的形状）

- `Calendar.lua`：`RegisterFeature("calendar", ...)` + `RegisterHeaderButton("calendar", L["CALENDAR_BUTTON"], Toggle)` + `RegisterSlashCommand("calendar", ...)` ⇒ `/gl calendar`。
- `Frame.lua`：**一扇窗口装三级**（不开第二、第三扇窗，避免叠盖与 hover 遮挡）：
  - 上半：月历网格 7 列 × 最多 6 行。每格三行：日号 / **净**（正绿负红，`GoldFormatter.Short`）/ 小字 `收 支`。
    月份标题 + `‹ ›` 两颗导航（复用 `L["UI_PREV"]`/`L["UI_NEXT"]`），今天那格描边。
  - 点某格 → 下半区列出**这天有钱变动的每个号**：`号名 | 收入 | 支出 | 净 | 「流水」按钮`，末尾一行合计。
    管理类窗口每行一颗按钮，不要行内多按钮，不要确认框。
  - 点「流水」→ 下半区换成该号该天的流水分页（时间 / 来源分类 / 金额）+ 来源分类小计；一颗「返回」。
  - 刷新只在开窗与点击时算：不挂事件、不轮询、不进 OnUpdate。
- `Locale.lua`：中英两档（上一轮已定"只留中英"），词条短：日历 / 全部角色 / 该日无记录 / 合计。
- 格子尺寸**先用探针量文字实际宽度再定**，别照着像素猜（他定的规矩）。
- toc 加载清单加三行；静态守卫用例（`every_lua_module_is_listed_in_the_toc` 同族）跟着过。

### T3　装到本机 + 核对

`cp` 到 `AddOns/GoldLedger/`（先删目标再拷，防嵌套），`diff -r` 必须空，`/reload` 生效。

## 3. 明确不做（要动等他点名）

- **主面板那四张重复卡片砍不砍、砍哪几张** = 删控件，他点名才动。
- **`CleanupOldEntries` 只清当前号** ⇒ 别的号的流水会一直攒（上限 5000 条/号）。改成全号清是数据行为变更。
- **没上过线的号就没数据**：这是记账类插件的固有边界，日历上那些天就是空格，不是 bug，也不做"补录"。

## 4. 交付方式

按既定流程：fresh subagent 逐任务实施 → 主 agent 逐行 review → 用例全绿 → 我自己提交 → 大白话汇报。
T1 与 T2 分两次提交（数据层 / 界面层），T3 单独一次发版号。
