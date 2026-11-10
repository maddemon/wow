# GoldCalendarLedger（金币日历账本）· 新建插件

日期：2026-11-10　状态：**计划，待开工**
路线：**B —— 记账层自己写**。只借 GoldLedger 已经在他机器上跑通的**做法**（哪些事件、哪些钩子、什么口径），
一行代码都不拷他的（他包里没 LICENSE，二次分发不干净）。
前一版"给 GoldLedger 加日历模块"的计划已作废：[2026-11-10-goldledger-account-calendar.md](2026-11-10-goldledger-account-calendar.md)。

## 0. 目标

1. 月历一格一天，每格显示**收入 / 支出 / 净**，数是**所有号加起来**的。
2. 点某天 → 这天每个号的 收入 / 支出 / 净 + 一行合计。
3. 点某个号 → 该号这天的流水，带来源分类（卖店 / 修理 / 拍卖行 / 邮件 / 任务 / 拾取 / 交易 / 号间搬钱）。
4. 记账本身不再让用户查接口：钱变化怎么捕获、来源怎么归属，全部照第 1 节已验证的做法。

**不做**：不读 GoldLedger 的存档（新文件夹 = 新档，本机实测文件按文件夹名存），历史从零开始 —— 他今天才装，旧档只有 11-10 一天。

## 1. 借的是知识：GoldLedger 记账层实测做法（逐条，出处标行号）

| # | 做法 | 出处 | 我们照做时要改的地方 |
|---|---|---|---|
| 1 | `PLAYER_MONEY` 只给变化后的总额 ⇒ 自己存 `lastGold` 算 delta，`delta==0` 也要刷新基准 | [Tracker.lua:178-187](../../GoldLedger/Tracker.lua) | 原样 |
| 2 | 基准在 `PLAYER_LOGIN` 之后才初始化（登录瞬间的钱不能当收入） | [Tracker.lua:260-264](../../GoldLedger/Tracker.lua)、Core 的 PLAYER_LOGIN 链 | 原样，且必须等 `GetMoney()` 有值 |
| 3 | 来源靠**上下文事件**：`MERCHANT_SHOW`→卖店、`AUCTION_HOUSE_SHOW`→拍卖行、`MAIL_SHOW`→邮件、`TRADE_SHOW`→交易、`LOOT_OPENED`→拾取、`QUEST_TURNED_IN`→任务，各配 `*_CLOSED`→清回 unknown | [Tracker.lua:51-74](../../GoldLedger/Tracker.lua) | 原样。任务上下文一笔就清（[:232](../../GoldLedger/Tracker.lua)） |
| 4 | **号间搬钱不算收支**：钩 `C_Bank.WithdrawMoney/DepositMoney`（战团银行）与 `WithdrawGuildBankMoney/DepositGuildBankMoney`，把请求金额+方向排队，等 `PLAYER_MONEY` 精确同额同向时"消费"掉，来源记 transfer，**优先于界面上下文** | [Tracker.lua:146-175, 287-304](../../GoldLedger/Tracker.lua) | 原样。队列 TTL 30 秒、上限 10 条必须留（被拒绝的请求没有 PLAYER_MONEY，不过期会吃掉后面同额的真实交易，[:39-42](../../GoldLedger/Tracker.lua) 注释就是这条） |
| 5 | 12.x 的 secret value：`issecretvalue(amount)` 为真就跳过，不许拿它比大小 | [Tracker.lua:149](../../GoldLedger/Tracker.lua) | 原样，pcall 包一层（这函数在不通的客户端上可能压根没有） |
| 6 | 修理费混在卖店支出里 ⇒ 钩 `RepairAllItems` 置标志，下一笔 vendor 支出改判成 repair | [Tracker.lua:267-270, 197-201](../../GoldLedger/Tracker.lua) | 原样 |
| 7 | 拍卖行卖出的钱是**邮件**来的 ⇒ 钩 `TakeInboxMoney(index)`，当场用 `GetInboxHeaderInfo(index)` 抓 sender/money 进 FIFO；邮件收入弹出一条，金额精确相等**且**发件人是拍卖行才归给 ah，否则老实留在 mail | [Tracker.lua:275-285, 203-220](../../GoldLedger/Tracker.lua) | 见第 6 节风险：`AUCTION_HOUSE_MAIL_SELLER` 这个全局在国服能不能拿到、值是不是中文发件人，**没验过**（本机只有 GoldLedger 自己用它） |
| 8 | `GetInboxHeaderInfo` 在 12.1.5 仍可用 | 本机生态：Postal / Auctionator / Syndicator / MailLogger 都在用 | 原样 |
| 9 | 全局 `GetItemInfo` 一族、`GetCoinIcon/GetCoinText/GetCoinTextureString` 在 12.1.5 **已删** | 12.1.5 API_changes + 我们上一轮给 GoldLedger 打的补丁 | 物品名一律 `C_Item` 路径 + pcall；金额格式化自己写，不碰这些名字 |
| 10 | 会话统计只在内存，不落盘 | [Tracker.lua:222-229](../../GoldLedger/Tracker.lua) | 原样（"本次会话"这张卡到底要不要，见第 3 节） |

## 2. 自己的数据模型（为日历设计，不按号各存一份月历）

```
GCLDB = {
  schema    = 1,
  settings  = { language = nil, minimapPos = 225, ... },
  daily     = { ["YYYY-MM-DD"] = { income = 铜, expense = 铜 } },   -- 全账号，日历直接读
  byDayChar = { ["YYYY-MM-DD"] = { ["Name-Realm"] = { income, expense } } },
  chars     = { ["Name-Realm"] = { firstSeen, lastSeen } },
  entries   = { { t=秒, char="Name-Realm", src="ah", amt=铜(带符号), transfer=bool } },  -- 流水，新在前
}
```

- 三处冗余（`daily` / `byDayChar` / `entries`）在**写入时一次算齐**，读的时候绝不现算 ⇒ 日历和钻取都是查表。
- 一致性由用例钉死：`daily[d] == Σ byDayChar[d][*]`、`byDayChar` 与 entries 分组结果互相对得上。
- `transfer` 条目进 `entries`，**不进** `daily` / `byDayChar`（同第 1 节 #4 的口径）。
- 清理：遍历**全部号**的条目按时间清（GoldLedger 的 `CleanupOldEntries` 只清当前号，[Data.lua:605-606](../../GoldLedger/Data.lua)，这个坑不背）。上限：`entries` 全局 20000 条、`daily` 保 400 天。
- 日期键保持 ISO `%Y-%m-%d`：清理拿字符串比大小，换格式就是换排序规则。

## 3. 界面（2026-11-10 与他定稿：一扇窗、三级钻取、月历当主轴）

**主轴 = 月历；周 CD 只当读数和范围预设，不做第二个网格。**
钱是"哪天发生的"这个形状，月历一眼扫得出哪几天在挣。周 CD 从周四起、和月份天然错位，做成网格要么 52 行排不下、
要么半个月被切两条。周 CD 汇总与月历读同一张 `daily` 表 ⇒ 数据模型不因它改变。
（趋势视图"按周列出近 10 周"留作日后加 tab，本轮不做。）

```
‹ 2026年11月 ›    本周CD 净 …    本月 净 …    全部 净 …
[ 日 一 二 三 四 五 六 ]   ← 每格：日号 / 净(正绿负红) / 小字 收·支 ；今天描边；空格只画底
──────────────────────────
[本日][本周CD][本月][全部]        ← 范围切换，默认「本日」
  号名            收入      支出      净        [流水]
  当前号置顶 + 描边；末尾一行「合计」；0 变动的号不列
──────────────────────────
[流水页] 时间 | 分类 | 金额 …   + 分类小计 + [返回]
```

- **顶栏三个合计**：本周 CD / 本月 / 全部。全部 = 记账以来的流水累计，**不等于银行里的钱**
  （转账不计收支、界面外变动记 unknown），两个数对不上是正常的，代码里不许互相"修正"。
- **hover 只给日期 + 收 / 支 / 净 + 几个号有变动**，不给分类明细：
  明细要额外按天按来源聚合（存档得再养一份冗余），而且两句话说不清 —— 它属于点进去那页。
- 点日号 → 下半区列**这天有钱变动的每个号**，当前角色置顶 + 描边，行尾一颗「流水」。
  管理类窗口每行一颗按钮，不要行内多按钮，不要确认框。
- 点「流水」→ 下半区换成该号该天分页流水（时间 / 分类 / 金额，拍卖行那类带物品名和数量）+ 分类小计 + 一颗「返回」。
  **默认进当前角色**：当前角色在这天/这范围有钱变动就默认它，否则回落到合计。
- 范围是四颗预设 `本日 / 本周CD / 本月 / 全部`，**不做起止日期自选器**。
  周 CD 起点用 GQL 已实测的 `C_DateAndTime.GetSecondsUntilWeeklyReset`（国服周四）。
- 刷新只在开窗与点击时算：**不挂 OnUpdate、不轮询、不新增接口调用**。
- 格子尺寸先用探针量文字实际宽度再定，不照像素猜。
- 中英两档（只留中英）。判国服不许只看 `GetLocale()`（它是文本语言，国服能回 enUS）⇒ 服名含 CJK 字节 + `PLAYER_LOGIN` 时重解析，照上一轮给 GoldLedger 打的补丁写。
- 图标：自带美术；小地图那颗用 `ADDON_NAME` 拼路径（文件夹被改名/被别人改名也不空白），toc 的 `## IconTexture` 是唯一写死处，用例钉住"素材随插件发出且不是空壳"。

## 4. 任务分解

- **T0 骨架**：新目录 `GoldCalendarLedger/`（toc + Core + 模块注册 + `/gcl`）、离线 harness 起跑、版本号三处一致用例。
- **T1 记账层** `Money.lua`：第 1 节 #1-#10 全部落地。判据：每笔钱只产生一条 entry；来源归属优先级 = transfer > repair > ah-mail > 上下文 > unknown。
- **T2 存储层** `Ledger.lua`：第 2 节三处冗余一次写齐 + 全号清理 + schema 档位与迁移表（先留空档，日后加字段走 MIGRATIONS）。
- **T3 日历 UI** `Calendar.lua` + `CalendarFrame.lua`：月历 + 两级钻取。
- **T4 本地化 + 图标 + 小地图按钮**。
- **T5 装机验收**：拷进 AddOns（先删目标再拷，防上一轮的嵌套坑），`diff -r` 必须空，`/reload` 生效；跑一遍真号卖个灰、修个装备、跨号搬一次钱，看三条口径对不对。
- **收尾**：新插件跑通后，vendored `GoldLedger/` 从仓库与 AddOns 撤掉（**等他点头**，撤之前先确认新插件已能独立记账）。

#### T0 完成记录（2026-11-10，骨架）

落地文件：`GoldCalendarLedger/GoldCalendarLedger.toc`、`GoldCalendarLedger/Core.lua`（128 行）、
`tests/run_gcl_tests.py`（7 条全绿；GQL 套件 532/532 未受影响）。与计划的出入：

1. **版本号是两处一致而不是三处**（§4/§5 写的"三处"）：第三处预设是 CHANGELOG，
   新插件尚无条目、本轮禁止动 `CHANGELOG.md`。用例 `test_version_matches_toc` 钉 toc 与运行时
   `GCL.VERSION` 两处；版本号首次进 CHANGELOG 时补第三处。
2. **测试入口叫 `tests/run_gcl_tests.py`**（§5 写的是 `tests/test_gcl_*.py`）：照 GQL 的单一 runner
   形状起跑，用例面铺开时再讨论是否拆分，不提前建空文件。
3. `GCL.RegisterEvent` 订阅表已就位但 Core 自己还没用它（生命周期由根帧 OnEvent 直接承担），T1 起模块开始订阅；
   §5 #9 的"图标两处一致"用例随 T4 素材出现才补（本轮 toc 无 `## IconTexture`，按任务书不预搭空壳）。
4. 替身按任务书含 `Show/Hide/IsShown`（Core 目前不碰，T3 UI 用）；名单外的任何帧方法取用即抛错，
   `RegisterEvent` 对未知事件名照 12.1.5 实测行为抛错。

## 5. 用例面（离线 lupa，`tests/test_gcl_*.py`）

替身规则沿用 GQL 那三条教训：复刻抛错与返回类型、**不许凭空造客户端没有的方法**、接线用例走真实事件入口（不是直接调内部函数）。

1. delta：登录基线不计收入；`delta==0` 刷基准；连续两笔不串。
2. transfer：同额同向消费掉；被拒绝的请求（无 PLAYER_MONEY）在 TTL 后不再误吃后面同额真实交易；队列超上限丢最老。
3. repair：`RepairAllItems` 后第一笔 vendor 支出改判 repair，第二笔回到 vendor。
4. ah-mail：金额相等 + 发件人匹配 → ah；金额不等 → 留 mail；队列空 → 留 mail（不许假归因）。
5. 三处冗余一致性：随机 200 笔跨号跨天后 `daily == Σ byDayChar == Σ entries`。
6. 月历：闰年 2 月、30/31 号月、空月；跨月边界那笔（次日 00:00:00）不许混进当天。
7. 清理：只上过线的号和从没上过线的号都要被清（回归 GoldLedger 那个坑）。
8. 本地化：enUS 文本语言 + 中文服名 → 中文；`DATE_FORMAT` 不许变。
9. 静态守卫：toc 清单齐全、图标两处一致且素材有效、版本号三处一致、命令注册表里 `/gcl` 真存在。

## 6. 风险与未定

- **拍卖行邮件发件人**（第 1 节 #7）：`AUCTION_HOUSE_MAIL_SELLER` 在国服客户端上存不存在、值是不是中文，没验过。
  计划里先按"拿不到就老实记 mail"实现（宁可不归因也不假归因），同时在 probe 里把实际 sender 字符串打出来，等一次真拍卖行到账取证。
- **上下文事件的漏判**：钱在界面外变化（比如拍卖行到期自动到账、公会银行远程操作）会记成 unknown。这是这类记账插件的固有边界，不做猜测式补全。
- **入口按钮位置**：新插件自己的窗口，不依赖 GoldLedger 的 header button 机制，一颗小地图按钮 + `/gcl` 两个入口。
- 分类粒度**已定（2026-11-10）**：8 类够用，不再拆买入/卖出/押金/手续费。
  但拍卖行那类要显示物品名和数量 ⇒ T1 落 `entries` 时仍要把 meta 字段（物品名 / 数量）留在条目上，事后补不回来。
