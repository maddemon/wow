# GDL 时光徽章单独归类 设计

**Goal:** 金币买 / 卖时光徽章在流水里归为独立来源「时光徽章」，右侧分类小计里能单列、能点筛；
但收支总量、日历格、号列表、本周/本月/全部汇总一律照旧 —— 净额仍然是真实钱包变化。

**用户诉求原话（2026-11-12）：**

- 「他消费了 68 万购买了时光徽章，但是这样会污染这个号的挣钱情况，我想时光徽章特殊处理」
- 「好像特殊处理也不对，这样会导致每个月看起来都有钱，但是总金币一直很少（一直买徽章的话）」
- 定稿：「只加分类，界面不改」；「买和卖都标」；「已经记进去的那笔 68 万不管历史，只管以后」。

## 口径

- 新增来源名 `token`，界面词「时光徽章」/「WoW Token」。
- **不是搬钱**：`token` 不进 `Ledger.TRANSFER_SOURCES`。`daily` / `byDayChar` / 范围合计 /
  `GetDay` / `GetRangeChars` 全部照常累加，`transfer=false`。
- 唯一变化：流水的 `src` 从 `ah` / `unknown` 变成 `token`；分类小计自动多一栏；类型下拉多一项。
  其余界面（日历格、号列表、四档汇总、悬停）一个数字都不动。

## 识别

### 买入（金币 → 徽章）

- 钩 `C_WowTokenPublic.BuyToken`（12.1.5 有，见 warcraft.wiki.gg 的 `C_WowTokenPublic` 列表）。
- 调用瞬间读 `C_WowTokenPublic.GetCurrentMarketPrice()`（铜），入「待认领」队列
  `{ amount, at }`：TTL 30 秒、上限 10 条 —— 与搬钱队列同一套寿命 / 上限口径。
- 下一笔 `PLAYER_MONEY` **出账**且金额精确相等才认成 `token`；对不上（取消购买、价格在确认期间变了）
  就照旧走界面上下文（拍卖行 / 其他），**不猜、不按「接近」匹配**。
- 价格读不到 / API 不存在 / 是秘密值：不排队，记账行为与现在完全一样。

### 卖出（徽章 → 金币）

- 卖出的金币由拍卖行邮件送来。在现有 `TakeInboxMoney` 抓快照那一步看邮件标题：
  标题里含时光徽章的物品名，就记 `token`，否则照旧 `ah` / `mail`。
- 物品名来源：`GetItemInfo(WOW_TOKEN_ITEM_ID)`（暴雪自己的 `BrowseWowTokenResults_Update`
  就是这么取名的），`WOW_TOKEN_ITEM_ID` 不在时退回物品 ID 122284；
  两者都读不到时退回语言表的 `SRC_TOKEN`（「时光徽章」/「WoW Token」）。
- 仍然要求快照金额 == 本次钱包变动额，与现有 `ah` 判据一致。
- 普通拍卖行邮件、玩家邮寄一律不受影响。

## 置信度与失败口径（用户问「徽章不一定能判准？」）

- **买入：高。** 判据是「BuyToken 被调用 + 出账金额精确等于当时市价」。任一条不成立 → 不标，
  保持现在的「拍卖行 / 其他」。**绝不会把普通消费标成徽章。**
- **卖出：中高。** 判据是「拍卖行邮件标题含徽章物品名」。标题对不上 → 不标，保持「拍卖行」。
  **绝不会把普通拍卖收入标成徽章。**
- 所以最坏结果 = 和现在一样（漏标），不会出现新的错账；`/gdl probe` 会把抓到的邮件原文打出来，
  第一次真卖出后一看就能校准。

## 界面

- `Flows.lua` 的 `SOURCES` 顺序：`vendor, repair, ah, token, mail, quest, loot, trade,
  unknown, warband, guildbank`（下拉顺序；`token` 挨着「拍卖行」）。
- 分类小计走现有 `SortedSources(bySource)`，自动出现「时光徽章」一栏，点它是现有筛选能力。
- **不加**新列、新行、新悬停、新窗。

## 取证

`/gdl probe` 增加（都不带行首缩进，避免和「最近 N 笔」的 `  ` 行混在一起）：

- `C_WowTokenPublic.BuyToken: 有 / 没有`
- `C_WowTokenPublic.GetCurrentMarketPrice: 有 / 没有`
- `时光徽章物品名: <名字> / 读不到`
- `待认领的徽章买入: N 条`

## 测试

| 用例 | 钉什么 |
| --- | --- |
| `money_token_buy_marks_token` | BuyToken + 出账 680000 → `token`；会话支出 +680000 |
| `money_token_buy_amount_mismatch` | 出账金额与市价不等 → 回落上下文，不标 `token` |
| `money_token_sell_mail` | 拍卖行邮件标题 = 物品名 → 收入记 `token` |
| `money_token_sell_other_item_stays_ah` | 普通物品标题 → 仍 `ah` |
| `money_token_name_unavailable` | 物品名读不到时退回语言表；仍能认 |
| `ledger_token_counts_normally` | `token` 条目进 daily/byDayChar/范围合计，`transfer=false` |
| `locale_token_source_name` | 两语言 `SRC_TOKEN` 都有；类型下拉含「时光徽章 / WoW Token」 |

- `CAL_ALL_SOURCES` 测试常量同步加 `token`（locale 完整性用例据此要求 `SRC_TOKEN`）。
- 现有 80 条用例必须全绿。

## 边界

- **不回溯**：已经记进存档的 68 万保持原分类（用户明确「不管历史」）。
- 卖出识别依赖邮件标题；国服真实标题若与物品名不同，用 `/gdl probe` 的邮件原文校准。
- 不做「按金额阈值猜徽章」这类启发式：误标比漏标更糟。
- 不引入设置开关、不加存档字段、不升 schema。

## 版本

`0.3.2` → `0.4.0`（`GoldDailyLedger.toc` 与 `Core.lua` 的 `GDL.VERSION` 两处同步）。
