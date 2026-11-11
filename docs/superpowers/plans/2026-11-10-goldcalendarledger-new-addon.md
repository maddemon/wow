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

> **本节是第 1 轮的草图，已被 §7 取代。**主轴（月历当主、周 CD 只当读数和范围、一扇窗、中英两档、
> 不轮询、图标自带）照旧有效；**下半区那块（四档按钮 + 号列表 + 分类小计 + 流水页）整块作废**，
> 换成"右侧角色净值列表 + 独立流水弹窗"。看版式一律以 §7 为准。

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
- 点「流水」→ 下半区换成该号该天分页流水（时间 / 分类 / 金额，拍卖行那类显示**邮件标题**作为物品线索）+ 分类小计 + 一颗「返回」。
  **不承诺物品名与数量**（T1 实测口径）：钱是拍卖行到账邮件送来的，`GetInboxHeaderInfo` 对纯金币邮件读不到物品，
  而全局 `GetItemInfo` 一族在 12.1.5 已删 ⇒ 编不出来的东西一律不显示，只把真读到的发件人/标题带在条目上。
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

#### T1 完成记录（2026-11-10，记账层 `Money.lua`）

落地：`GoldCalendarLedger/Money.lua`（计划 §1 的 #1-#10 全在，代码自己写，没拷 Tracker.lua 一行）、
toc 加一行、`tests/run_gcl_tests.py` 16 条全绿（新增 9 条：基线/delta、上下文、搬钱队列五种形状、
修理标志、拍卖行邮件六种归因、秘密值、优先级与"一笔只写一次"、probe 全缺接口、账本缺席降级）。
与计划的出入 / 主 agent review 时改掉的：

1. **Core 的 `RegisterEvent` 补了"顺手向根帧注册"**（订阅表原本只记账不接线，事件根本不会来）。
   同一事件只注册一次；未知事件名照客户端行为抛错，不静默吞。
2. **秘密值的基准处理改了**：子任务原写"秘密总额仍把基准推进"，那样一旦真出秘密值，
   下一笔就是"正常数 − 秘密值"，客户端要么抛错要么算出秘密，基准从此推不动 ⇒ 整条记账瘫痪。
   现在改成**基准作废**：这一笔不记，下一次问得出正常数时重立基准。宁缺笔记错笔。
3. **物品名/数量确认拿不到**（§3 已同步改口径）：纯金币的拍卖行邮件 `GetInboxHeaderInfo` 只回发件人/标题/金额，
   meta 就只带这三样，编不出来的不显示。
4. 邮件快照加了上限 10（原计划没写，防"打开邮箱没点收取"之类情况下无限增长）。
5. `/gcl probe` 已建：把 `AUCTION_HOUSE_MAIL_SELLER` 的实际值、四个可选接口在不在、
   最近 5 笔变动原样打出来 —— §6 那条"国服发件人常量到底有没有"就等他跑这一次取证。

#### T3 完成记录（2026-11-10，日历界面）

落地：`GoldCalendarLedger/Calendar.lua`（一个月历 + 两级钻取 + 四档范围，1330 行单文件）、
`GoldCalendarLedger/Locale.lua`（zhCN/enUS 文本表与查表）、toc 加两行、
`tests/run_gcl_tests.py` 42 条全绿（新增 18 条：界面 14 + 本地化 3 + "定义了就必须进清单"守卫 1）。
与计划的出入 / review 时改掉的：

1. **没拆 `CalendarFrame.lua`**（§4 写的是两个文件）：建窗、绘制、交互读的是同一份 `ui`/`state`，
   拆成两个文件只会让"改一格要跳两处"。
2. **`Locale.lua` 从 T4 提到 T3**：界面每个词都得查表，不先落地就要把两档语言在界面里写两遍。
   T4 只剩"自动判定 + 落盘"（`Locale.Detect` 已写好，`ApplySetting` 里那一行还没接 `PLAYER_LOGIN`）。
3. **版式常量全标"待量"（`GEOM` 块），像素宽度断言从用例里撤了**：替身给的字体度量是编的，
   拿它判"这行字放得下"是自己证自己。留下的判据都不依赖度量：窗高 ≤ 屏高 85%、格数 = `GRID_ROWS × 7`、
   一页条数 = 一屏可见行、截断只按字节且劈不开 UTF-8 字符。真数等 T5 跑 `/gcl probe ui` 量回来再钉。
4. **换语言必须立刻换字**（原实现只有 `Build` 里摆一次，切 enUS 后标题和按钮还是中文）：
   静态串（标题、星期表头、列头、按钮、每行那颗「流水」）集中进 `ApplyStaticTexts()`，
   `Build` 尾 + 每次 `Refresh` 各摆一次；静态守卫盯住"`Build` 里不许再出现 `T("…")`"。
5. **读不出墙上钟不许报"这个范围没有变动"**：空态按 `state.startKey` 有没有算出来分岔，
   没范围就答 `NO_CLOCK` —— 对着一个根本没确定的日子说"没事发生"是假答。
6. **月外格悬停**：客户端不保证先给上一格发 `OnLeave`，所以 `ShowCellTip` 进门先 `Hide`。
   用例只钉"提示已 Hide + 月外格一行都不新画"，不钉"行被清空"（`Hide` 后旧行留在控件里是客户端正常行为）。
7. **点"今天"那一格不动档位**：开窗选中的就是今天、范围本就是单日，再点只是同一个视图；
   点别的日子才落 `sel`（用例两种都钉着，免得日后有人把 preset 重置当成"刷新"塞回去）。

#### T4 完成记录（2026-11-10，语言判定 + 自带图标 + 小地图按钮）

落地：`Locale.lua`（判定链 + `/gcl lang`）、`Minimap.lua`（新，环上入口）、`Core.lua` 多一条
`GCL.ChainProbe`、toc 加 `## IconTexture` 与 `Minimap.lua`、素材 `GoldCalendarLedger.tga`
（128×128 未压缩 BGRA、bottom-up、descriptor 0x08，65554 字节，与兄弟插件那颗已在真机跑通的
文件逐字节同头），`tests/run_gcl_tests.py` 53 条全绿（新增 11 条）。
与计划的出入 / review 时改掉的：

1. **判定挂在 `OnLogin` 而不是 `ApplySetting`**：`GetRealmName` 在 ADDON_LOADED 那一刻还答不出，
   在那里判等于每次装机都判成"英文"。时序由用例从真事件入口钉（ADDON_LOADED 后仍是默认档 +
   来源记 `init`，PLAYER_LOGIN 才翻）。
2. **判出来的档一律不落盘**：只有 `/gcl lang` 写 `settings.language`；`auto` 是**清掉这个键**
   而不是写字符串 `"auto"`（档里没这个键才是不折不扣的"没选过"，换了客户端语言/换了号还能重判）。
   变异验证过：把判定结果写回存档，两条用例当场咬。
3. **判定依据单独一份记录**（`source` / `code` / `reason`，五种来源 init/auto/chosen/default/invalid），
   `/gcl lang` 与 `/gcl probe` 都念它，窗口上一个字都不显示 —— 照"取证措辞归 probe"那条老规矩。
   两个信号都没答时 `reason` 写的是"GetLocale=… / GetRealmName=…"原样，不猜一档交差。
4. **图标素材离线只能验字节**：客户端没有证过的"文件在不在"接口，所以 `/gcl probe` 那条明说
   "随包 tga 在不在 Lua 读不出"，判据留到 T5 真机眼看；离线那一关钉的是头 18 字节、65554 总长、
   32bpp/0x08、alpha 恒 255、颜色数 ≥4（铺满一个色 = 空白的另一张脸）、以及 toc 里不许出现
   金币 / 任务日志书本 / 兄弟插件清单图这四颗被否或用过的素材。
   美术形状：圆角窗 + 顶部琥珀标题条 + 4×4 月历格 + 一格绿（收）一格红（支）。
5. **小地图按钮的路径由 `GCL.addonName` 拼**（计划 §3 最后一条）：用例把 `GCL.addonName` 改成
   别的名字再登录，按钮那颗必须跟着换 —— 写死目录名，文件夹改名后就是空白。
   两处图标（toc 与按钮）同一颗由用例对字符串钉死，这是兄弟插件 2026-11-10 真踩过的坑。
6. **拖动只在按住期间挂 `OnUpdate`**：`OnDragStop` 立刻摘掉；用例既从行为上验（拖完读 `GetScript`
   回 nil），也从静态上验"挂/摘各一处"。角度存 `GCLDB.settings.minimapAngle`，二次登录按存的摆位。
7. **宿主取不到就降级**：一个帧都不造、不抛错，print 一条明说"窗口照旧用 /gcl open"的指路日志。
8. review 改掉的：`OnLeave` 那句注释原本写"只收我们自己弹的那条"，代码其实是无条件 `Hide`
   （小地图按钮就该这么干）—— 注释与代码不符比缺注释更坏，注释删掉、代码留着。

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

## 7. 界面重排第 2 轮（2026-11-10，他回传截图后的 6 条 + 滚动）

### 7.1 他这轮说的与原样落点

| # | 他的原话要点 | 落点 | 任务 |
|---|---|---|---|
| 1 | 左上角两颗按钮看不到文字；"本周CD"要写"本周"，但算的还是本周 CD | 字形换成字体一定有的 ASCII `<` `>`；`RANGE_week` / 顶栏标签只改字不改口径（周 CD 仍周四起） | T6 |
| 2 | 当日格子里灰色的收入/支出不显示，只留净；hover 不给净，给收入和支出 | 格子从三行改两行（日号 / 净）；`CELL_SUB` 不再上界面；提示行去掉 `TIP_NET` | T7 |
| 3 | 下半区列表错位、内容太多，整块移除 | 主窗不再有号列表 / 分类小计 / 流水页 / 翻页 / 返回 / 四档按钮 | T7 |
| 4 | 点日期后**右侧**列所有角色的净收入，hover 净看收支，点这一行弹窗看该号流水 | 右侧面板：三档 + 可滚动的"号名 / 净"列表；行 = 一颗整行按钮 | T7 + T8 |
| 5 | 流水页能筛角色、筛类型 | 弹窗里"只看这个号 / 全部号"两态按钮 + 点小计行筛类型 | T8 |
| 6 | 右侧顶部可切 本日（点日期就是本日）/ 本周 / 本月 | 三档按钮在右侧面板顶部，范围锚在**选中的那天** | T7 |
| 7 | 小号多，右侧要有最大高度、可滚动 | 右侧列表进 `UIPanelScrollFrameTemplate`；弹窗流水同样滚动 | T7 + T8 |

### 7.2 新布局（主窗一扇，双栏；流水另开一扇）

```
┌──────────────────────────────────────────────┬──────────────────────┐
│ < 2026年11月 >  本周 净… 本月 净… 全部 净…    │ [本日][本周][本月]    │
│ 日  一  二  三  四  五  六                     │ 号名            净   │
│ 每格两行：日号 / 净（正绿负红）；今天描边       │ 技能12-奥达曼  4066● │
│ 6 行 × 7 列 = 42 格；空格只画底                │ …（滚动，11 行可见）  │
└──────────────────────────────────────────────┴──────────────────────┘
```

- **顶栏三个读数保留**（本周 / 本月 / 全部），它是"总计"的唯一落点，与右侧选没选哪档无关。
  「全部」这一档**从范围按钮里去掉**（他这轮只点名 本日/本周/本月），累计只在顶栏读数里看得见。
- **三档范围锚在选中的那天**（`state.selKey`，开窗默认今天）：本日 = 那天；本周 = 那天的周 CD（周四起，接口问不到退回日历周周一，原因只进 probe）；本月 = 那天所在月。
  点日历任意一天 = 选中它并把档位落到"本日"——沿用 T3 已经钉住的那条（点今天那一格不动档位，因为开窗本就选中今天）。
- **右侧列表行**：号名（左，按字节裁断，绝不盖到净那列）+ 净（右锚，图标形式）。
  当前号琥珀色；排序沿用 `Ledger.GetRangeChars`（净降序、并列当前号置顶）；零变动的号不列。
  hover 净 ⇒ 提示两行：`收入 …` / `支出 …`（不再给净，净已经写在行上）。点整行 ⇒ 开该号该范围的流水弹窗。
- **格子两行**：`CELL_H` 从 42 降到 36（少一行字）；`CELL_SUB`（收·支小字）与它的 `Locale` 串一起下线，串不留空壳。

尺寸（`GEOM`，全部集中在这一块，版式段落之外不许再出现裸像素常量 —— `test_calendar_static_guards` 扫全文）：

| 键 | 值 | 说明 |
|---|---|---|
| `CELL_W` / `CELL_H` / `GRID_ROWS` | 88 / 36 / 6 | 左栏 616 宽、216 高 |
| `PANEL_W` / `PANEL_GAP` | 236 / 12 | 右栏宽 = 三颗 70 宽按钮 + 2 颗 4 间隙 = 218，余 18 给内边距 |
| `ROW_H` / 可见行 | 20 / 11 | 右栏滚动区高 236 |
| `WIN_W` | 888 | `PAD*2 + 616 + PANEL_GAP + PANEL_W` |
| `WIN_H` | 306 | `PAD + TITLE_H + NAV_H + WD_H + 6*CELL_H + PAD` |
| `MAX_WIN_H` | 612 | 预算不变（屏幕 720 高的 85%），306 远在其内 |

### 7.3 流水弹窗（新文件 `Flows.lua`，帧名 `GCLFlowFrame`）

```
┌────────────────────────────────────────────────────┐
│ 流水 · 技能12-奥达曼 · 2026-11-10        [全部号] [×]│
│ 时间          来源              金额                 │
│ 17:08         卖店              -18●94  （滚动，12 行）│
│ ── 分类小计（点一行 = 只看这类；再点 = 全部）──        │
│ 任务    +4066●67   卖店  -166●92   …（最多 10 行）    │
└────────────────────────────────────────────────────┘
```

- **范围** = 打开它时右侧选中的那段，标题行写清是哪段（`2026-11-10` / `本周 11-06..11-12` / `本月 2026-11`），
  换档或换日后重开弹窗即跟新范围走；弹窗开着时不自动刷新（不挂 OnUpdate）。
- **角色**：一颗两态按钮，默认"只看这个号"（点进来的那个），点成"全部号"后列该范围全部号的流水。
  底下靠 `Ledger.GetCharEntries(nil, startKey, endKey, page, perPage)` —— **`charKey` 传 nil 表示不限号**，
  这是 T2 那条有界扫描的唯一放宽，`bySource` 跟着覆盖同范围全部号（判据：小计与流水对得上，跨号求和一致）。
- **类型**：不给下拉（本仓库没有 `UIDropDownMenu_*` 的已验证调用点，12.x 是否还留着没证过；
  生态里只有 `AutoTurnIn` 这类第三方在用旧全局 ⇒ 不赌）。改成**点分类小计那一行**筛：
  点一行只看该类，再点同一行回全部，当前类琥珀色。小计行永远列全（来源枚举就 10 类，天然封顶，
  不需要"另有 N 类未列出"），也不随筛选缩水——缩了就只剩一行，没意义。
- **滚动**：`CreateFrame("ScrollFrame", nil, parent, "UIPanelScrollFrameTemplate")` + 内容帧 + `SetScrollChild`，
  形状照已验证的 `GoldLedger/UI/UI.lua:444-465`；行数变了就 `child:SetHeight(n * ROW_H)`。
  **每一行按钮也要挂 `OnMouseWheel`**（`GoldQuestLedger/Matrix.lua:1546` 实测"滚轮落在按钮上不吃事件"，处理函数在 1579）——
  只挂滚动区等于滚不动。
- 关闭 = `UIPanelCloseButton` 模板 + 层号钉回主帧 +1（模板自带 510 会浮到别人上面），并把它自己的全局名挂进 `UISpecialFrames`，`ESC` 能关。
- 拍卖行那类仍**不承诺物品名与数量**（T1 口径不变），只带真读到的发件人 / 邮件标题。

### 7.4 同类缺陷盘点（他点一个，扫一整类）

| 类 | 现状 | 判定 |
|---|---|---|
| 界面串里用了字体没有的字形 | 全插件扫过：`§ ⇒ 「」 → × − ★` 25+ 处**全在注释里**，上界面的只有 `Locale.lua` 的 `‹ ›` 四处 | T6 改 `<` `>`，并加一条静态用例：两档语言表的**值**只允许 ASCII + CJK + 中文标点白名单，出现表外字形就 FAIL |
| 两页内容画在同一块地上（截图里号列表行和流水行叠在一起） | 下半区两页共用槽位、切页没把另一页的行 Hide | T7 整块移除 ⇒ 现场消失；新用例补一条"面板列表与格子提示互不残留"，弹窗另钉"筛选只换内容、不换行槽位" |
| 控件删了但指向它的文案还留着 | 主窗里"另有 N 个号未列出""分类小计""返回"等串随下半区一起作废 | T7/T8 拆控件的同批扫掉：`Locale` 表里不再被任何 `T()` 引用的键一律删（不留空壳串） |
| 溢出没有出口（号比可见行多就没辙） | 旧版靠"另有 N 个号未列出"，等于看不见 | T7 滚动区取代它 |

### 7.5 任务分解

- **T6 文本与字形**（`Locale.lua` + 用例）：`RANGE_WEEK` 改"本周"（口径仍是周 CD）、`MO_PREV/MO_NEXT` 改 `<` `>`。
  新静态用例：界面串值只允许白名单字形（ASCII + CJK + 中文标点），出现表外字形就 FAIL。
  **右栏/弹窗的新串不在这一轮加**：`test_locale_tables_are_two_language_complete` 有一条"表里留着但界面不再用的键"
  的守卫，提前加串会被它判成死键 ⇒ 新串随 T7/T8 接线同批进表（右栏列头直接复用 `COL_CHAR`/`COL_NET`，不另造同值键）。
  **作废串的删除放在 T7/T8**——先拆控件再删串，反过来界面会直接露出键名。
- **T7 主窗重排**（`Calendar.lua` + 用例）：双栏 `GEOM`、格子两行、hover 给收支、右栏三档 + 滚动号列表 + 整行按钮开弹窗；
  下半区（号列表页 / 流水页 / 分类小计 / 翻页 / 返回 / 四档按钮）连控件带渲染函数全部拆掉。
  **旧用例不许静默降级**：钉"号列表在主窗"的那批迁到"右栏列表"、钉"流水页/翻页/返回"的那批迁到弹窗（T8 落），判据一条不删只换落点。
- **T8 流水弹窗**（新 `Flows.lua` + toc 一行 + `Ledger.GetCharEntries` 支持 nil + 用例）：范围标题、两态角色按钮、
  滚动流水、点小计行筛类型、`UISpecialFrames` 与 `/gcl flow` 兜底命令（`/gcl flow [号名]`，弹窗能开就说明右栏那颗按钮不是唯一入口）。
- **T9 取证分段 + 装机复测**：`/gcl probe ui` 拆成 `frame` / `grid` / `fonts` / `tip` 四段（**每段不超过 12 行**，
  `grid` 只打第一行 7 格 + 今天那一格，其余靠 `all` 参数才全打），解决"聊天窗口装不下、截图截不全"；
  拷进 AddOns（`diff -r` 必须空）、`/reload`，按实测宽度回填 `GEOM`。

### 7.6 本轮不做

- 起止日期自选器、趋势"按周列出近 10 周"、下拉控件、弹窗内再弹窗 —— 都不在 6 条里，先不加。
- 主窗可拖拽调宽 / 记忆上次档位：档位每次开窗回到"本日 + 今天"，改动要他点名。

#### T6 / T7 / T8 完成记录（2026-11-10，界面重排第 2 轮）

落地：`Locale.lua`（T6 已提交 `9bfb662`：`RANGE_WEEK` 显示"本周"、翻月按钮 `<` `>`；本轮再随控件一起删死键）、
`Calendar.lua`（双栏 888×306、格子两行、右栏三档 + 滚动号列表 + 整行按钮开弹窗）、
`Flows.lua`（新，独立流水弹窗 + 两态角色按钮 + 点小计行筛类型）、`Ledger.lua`/toc/用例。
离线 `tests/run_gcl_tests.py` **62/62 全绿**；标"号列表在主窗 / 流水页翻页 / 返回"的旧用例全部换落点到右栏与弹窗，
判据一条没删（分页 ⇒ 弹窗一次列全靠滚动；返回 ⇒ 点 × 关弹窗、主窗状态不动）。

与计划的出入 / 实现时改掉的：

1. **右栏"所有角色"的口径**：他 2026-11-10 明确选"只列有变动的号"，沿用 `Ledger.GetRangeChars`；
   0 变动的小号不占行，也不写"另有 N 个号未列出"——出口改滚动区。
2. **`PANEL_LIST_H` 由窗内剩余高度算出来**（`WIN_H - 2×PAD - 26 - 20 = 236`），`PANEL_ROWS = 236 / 20 = 11`；
   可见行取整行，不画半行。
3. **加了选中日底色**（今天仍用描边纹理，两件事不混用）：不标的话三档锚在哪一天看不出来。
4. **顶栏读数与右栏范围拆成两套**：`GetRange` 只伺候 本周（CD 至今）/ 本月（跟显示月）/ 全部（累计）；
   `GetPanelRange` 锚在 `selKey`，本周给那天的**整周** CD（周四起，按整周平移接口锚点，接口问不出退化日历周周一）。
5. **修掉两处 `cond and nil or x`**：`Flows.Load` 放开"全部号"时照样传了 charKey、`SetSource` 再点同一行清不掉筛选 ——
   Lua 里 `true and nil` 是 nil，`nil or x` 又把它接回来。这两条是 T8 遗留的红用例。
6. **弹窗的角色筛选只有两态**（只看这个号 / 全部号），不引下拉模板（§7.6）；要看别的小号，点右栏它那一行重开弹窗。
7. T9（`/gcl probe ui` 分段 + 装机 `/reload` 复测、`diff -r` 拷进 AddOns）**未做**：本机没有客户端，
   格宽字号仍以真机探针量回来的数为准。

### 8. 第 3 轮（2026-11-10，他回传第 2 张截图后的 5 条）

定稿三项（本轮问过并选定）：**金额只显示整数金**（1234567 铜 ⇒ `123` + 金图标，尾数丢掉，
不足 1 金显示 `0`，`-0` 不写负号）；**日期档一律锚在日历点选的那天**（"根据日历点击的日期来，
这样不容易出错"，点历史日开弹窗不跳回今天）；**真分页每页 20 行**（上一页/下一页 + 页码，
400 上限与"另有 N 笔未列出"整条下线）。

| # | 落点 |
|---|---|
| 1 | 角色名一律 `Ledger.CharLabel`（`ShortCharKey` 去服名 + 职业色 `\|cff…\|r`）。职业只在客户端拿得到：`UnitClass` 第二返回值在 `PLAYER_LOGIN` 与首次 `AddEntry` 时写进 `db.chars[key].class`，照 `GoldQuestLedger/Sync.lua:1178-1181`；`RAID_CLASS_COLORS` 只用 `.r/.g/.b`（没有 `.colorHex`，`Matrix.lua:145-162`）。没登录过的旧号中性灰兜底，**不编色**；当前号只靠置顶区分，不再盖琥珀 |
| 2 | `Calendar.FormatMoneyIcon` 一处改契约，顶栏/格子净/右栏净/两处 hover/弹窗行/小计全跟着变；银铜图标与纯文本死函数下线 |
| 3 | 遮挡照 `GoldQuestLedger/Options.lua:889-899` 的 `RaiseAboveMatrix`：**两窗都是 `UIParent` 平级 MEDIUM，跨 strata 比层号无效** ⇒ 打开时让主窗退回 MEDIUM、弹窗 `SetFrameLevel(主窗 + 10)`，再按新层号把 × 重钉回本窗主帧 +1（模板自带 510）。不隐藏/禁用主窗、不加 `SetToplevel`/`HIGH`。`Calendar.GetFrame` 走模块引用，不查 `_G`（12.x addon 帧名不再注册） |
| 4 | 弹窗四个自绘下拉（角色/类型/日期/收支方向）+ 真分页；日期五档本日/昨日/本周CD/本月/全部由 `Calendar.PopupRange` 统一算，下拉标签把档位词和确切日期一起写（如「本日 11-05」）；收支方向在 `Ledger.GetCharEntries` 上过滤，`bySource` 仍覆盖全范围不缩水 |
| 5 | 分类汇总移到弹窗右侧 190px 竖列，列头与流水列头同高、行与流水行同起点，窗高不再随汇总行数变 |

**推翻 §7.3/§7.6「不做下拉」**：他本轮点名要下拉。用 `GoldLedger/UI/UI.lua:769-859` 的自绘
（Button + `TOOLTIP` strata 弹出 Frame），不引 `UIDropDownMenu`（本仓库仍没有 12.x 的已验证调用点）。
照抄时去掉它用的 `HookScript` / `SetAllPoints`：这两个不在离线替身与已证清单里。

`tests/run_gcl_tests.py` **63/63 全绿**；旧用例里钉「两态全部号按钮 / 400 上限 / 滚动不分页 /
完整号名标题 / 金+银图标」的判据全部换落点，一条没删；新增汇总侧边锚点、弹窗层号高于主窗、
四下拉筛选、五档日期锚、换语言弹窗重画等判据。替身补了 `UnitClass` 与 `RAID_CLASS_COLORS`。

**仍待真机**：下拉弹出层的实际遮挡、`\|cff` 职业色在 12.1.5 中文字体下的渲染、
整数金截断后的读数观感；这三条离线演不出来，装机 `/reload` 后按 `/gcl probe ui` 与他截图复测。

### 9. 第 4 轮（2026-11-10，两张真机截图 + 11 条）

| # | 落点 |
|---|---|
| 主窗 1/2 | `GEOM.CELL_H 36→44`；格子日号 / 净从 `FONT_BODY` 换 `FONT_CELL = GameFontNormalSmall`（与悬停提示正文同量级） |
| 主窗 3 + 弹窗 1 | 职业色兜底 `NEUTRAL_RGB` 由 `{0.62,0.62,0.62}` 改 `{1,1,1}`（照 `GoldQuestLedger/Matrix.lua:149-162` 退白，不编色）；`Flows` 下拉选项 / 分类名常态色另立 `C_ITEM` 白，灰只留给净为 0 |
| 主窗 4 | 两处 hover 统一走 `TipStyle`（`SetAlpha(1)` + `SetBackdropColor(...,1)`，不半透明）与 `TipLine`（每行都显式带颜色参数；不带色的第一行在部分客户端会走更大的 header 字体，这正是"收入/支出数字大小不一样"的可修形状） |
| 主窗 5 | `PANEL_W 236→264`：× 锚窗右上角随 `WIN_W` 右移，右栏三档按钮原地不动，几何断言钉"本月"右沿 < × 左沿 |
| 弹窗 2 | 日期下拉不再套 `FILTER_LABEL`（删 `FILTER_RANGE` 键），按钮直写 `currentLabel`；四个下拉字串定宽 `DD_W[i]-12`，日期档用 `FONT_SMALL`，`DD_W` 重排 `{150,140,200,130}`，四格连间隙仍排得进 `WIN_W` |
| 弹窗 3 | 汇总 `row.inc/exp` 从 `SetPoint("RIGHT", b, "TOPLEFT", col, 0)` 改成右锚 `b` 的 `RIGHT` + 负偏移 `-(SUB_W - col)`，三列都落在行按钮垂直中心，与列头 x 一致 |
| 弹窗 4 | `SRC_WARBAND`「号间搬钱」→「战团银行」；类型本来就有 `warband`（`SOURCES` 十类齐全），只是名字不像。**公会 / 战团银行口径不变**：取出 = 角色进钱、存入 = 角色出钱，两向都 `transfer=true`，流水里列得出但**不进 daily / byDayChar 收支总量**（搬钱不改变财富，`Money.lua:303-324` 与 `Ledger.lua:33-37`） |
| 弹窗 5 | `SRC_UNKNOWN`「未知」→「其他」（enUS `Other`），`Locale.SrcName` 的回退值同步 |
| 弹窗 6 | 卖店连发**读出层**合并：`Ledger.GetCharEntries` 里 `MergeVendorRuns`，同号 / 同日 / 同方向 / 非搬钱 / 相邻 ≤3 秒的连续 `vendor` 并成一行，带 `mergedCount`，界面写「卖店 ·N笔」。`db.entries` 明细一条不删、`daily` 与 `bySource` 仍按原始条目算、总量不变；别的来源连发不合并（连收几封邮件、连交几个任务在语义上各自一笔） |

与"写入层合并"的取舍：写入层会永久丢明细，账本不该这么干；读出层只改玩家看得见的行数，
分页 `total / totalPages / rows` 按合并后的行算，随时能回溯。

`tests/run_gcl_tests.py` **70/70 全绿**、lint 0。新增 7 例：格高 / 字号 / 右栏不压 ×、
两处提示不透明且行形状一致、职业色兜底不灰（含弹窗侧）、来源改名、日期下拉布局、
汇总三列对齐、卖店连发合并（含方向 / 换号 / 超窗口 / 夹别的来源 / 别类连发五条边界）。
既有分页用例的夹具换成 `loot`：同秒 25 笔 `vendor` 现在会被并成一行，量分页就不能用它。
替身补 `GameTooltip.SetAlpha` / `SetBackdropColor`（`Matrix.lua:1585` 同一对 Frame 方法）。

**仍待真机**：格子小字与金图标 `:12:12` 的视觉比例、右栏加宽后整窗观感、
日期下拉 200px 在 12.1.5 中文字体下的实际排版（`SetWidth` 后长文案会不会折行），
以及 3 秒窗口是否覆盖他一次卖几十件的真实节奏（真机若还漏并，调 `VENDOR_MERGE_SECONDS`）。

### 10. 第 5 轮（2026-11-10，第 3 张截图 + 3 条）

| # | 落点 |
|---|---|
| 1 职业色 | 取证结论：**GQL 也没有运行时跨号职业接口**。它的 `GQLDB` 是 account-wide，`Sync.lua:1173-1180` 在每个号登录跑快照时把 `UnitClass` 的 token 写进 `chars[名字-服务器].class`，别的号登录读同一份存档 —— "登录了谁谁的颜色才对"是账号级存档累积的结果，不是 GQL 会批量查。GCL 照这个机制补三层：`Core.lua` 新增 `classByChar`（登录即留档、且不造 `chars` 空号）；`BorrowClass` 只读借用 `GQLDB.chars[key].class` 并缓存进自己的库（不写 GQLDB、不编色、形状不对退白）；`CharColor` 兜底链 `chars[key].class → classByChar[key] → GQLDB → 白色`。历史号立刻拿到真实职业色，以后每个号登录一次也自动补上 |
| 2 提示 | 上一轮的 `SetAlpha(1)` + `SetBackdropColor(...,1)` 在真机压不住：GameTooltip 的背板不是普通 backdrop，且第一行走 header 字体、后续走正文，收入/支出天生一大一小。改成**自绘提示框** `GCLCalTipFrame`（`BackdropTemplate` + `TOOLTIP` strata + 背板 alpha=1），四行全用同一个 `FONT_TIP = GameFontNormalSmall`、等行高；格子给 4 行、右栏给 2 行。关窗 / 开流水弹窗都主动收框。日历主窗不再碰 GameTooltip（Minimap 与流水弹窗的 RowTip 仍用它） |
| 3 卖店合并 | 用户定稿"记录的时候就记录一条"。合并从读出层移到**写入层** `Ledger.AddEntry`：与紧挨着的上一笔同号 / 同日 / 同 vendor / 同方向 / 非搬钱 / ≤3 秒时，直接累加到那条 `amt`、更新 `t` 与 `mergedCount`，不新增 entry；`daily` / `byDayChar` 仍按本次金额逐笔累加，总量一次不少。读出层 `MergeVendorRuns` 保留，专门兼容**旧存档里已经散开**的明细，并把 `mergedCount` 累加（否则两组再并一次只会报 2 笔）。取舍：写入层会永久丢掉逐件明细，但存档原本只记金额、不记物品名，这些明细没有额外信息；换来流水天然干净、不占 entries 配额 |

`tests/run_gcl_tests.py` **71/71 全绿**、lint 0。用例迁移：日历两处 hover 改读 `cal_tip_frame_lines`，
"GameTooltip 缺席"从"记证据"改成"把它置 nil 也照样弹"（更强的判据）；
`calendar_round4_tooltip_opaque_and_uniform` → `calendar_round5_tip_frame_uniform_and_opaque`
（四行同字体 / 等行高 / 背板 alpha=1 / TOOLTIP strata / 关窗收框）；
`ledger_round4_vendor_burst_merged` → `ledger_round5_vendor_merge_writes_one_entry`
（存档恰 1 条 + `mergedCount=30` + 旧存档兼容 + 已并条目与旧明细混排时笔数累加）；
新增 `calendar_round5_char_class_sources`（三层来源 + 不造空号 + 右栏真职业色 + 坏 token 退白）。

**仍待真机**：自绘提示框在 TOOLTIP strata 下会不会盖住流水弹窗的下拉（已加收框，未实测）；
`TIP_W=176` 够不够放下最长的金额行；3 秒窗口是否覆盖他一次卖几十件的真实节奏。

### 11. 第 6 轮（2026-11-10，第 4 张截图：下拉折行）

第 4 轮只给日期档去掉了筛子名前缀，另外三个仍写「角色 · 值 / 类型 · 值 / 收支 · 值」；
前缀 + 长号名把定宽字串挤到第二行（截图里「风行者·麦德」断成两行）。

- `Flows.RefreshDropdowns`：四个按钮一律只写 `currentLabel`，不再套 `FILTER_LABEL`。
- 新增 `ClipLabel(s, maxBytes)`：按字节预算裁断，遇到 `|cffRRGGBB…|r` 先剥出颜色、
  只裁名字再套回去 —— 转义不占显示宽度、更不能截在中间；`Clip` 仍按 UTF-8
  字符边界回退，不劈半个字。预算在 `MakeDropdown` 里按 `(width-12)/4` 记进
  `dd.clipMax`（中文约 4px/字节的保守估算）。
- `Locale.lua`：`FILTER_LABEL` / `FILTER_CHAR` / `FILTER_TYPE` / `FILTER_DIR`
  四个键两表一起删（静态守卫钉"表里不留没用的键"）。
- 用例：四个下拉的旧断言从"必须带筛子名"翻成"必须等于选中值、且不以筛子名开头"；
  换语言重画改钉 `type` 下拉变 `All types`；新增
  `flows_round6_dropdown_value_only_and_clipped`（四档无前缀、无换行、职业色转义完整、
  按预算裁断、收紧 `clipMax` 后真走到裁断、UTF-8 不劈字、切档后按钮跟着变）。

`tests/run_gcl_tests.py` **72/72 全绿**、lint 0。

### 12. 第 7 轮（2026-11-10，读数条 / 分类总计 / 跨插件层序）

| # | 落点 |
|---|---|
| 1 读数条 | 顶栏三个读数整段拆掉，搬到**日历网格下面**：`SUMMARY_H=52`、`SUMMARY_TOP` 从窗高公式里算出来，`WIN_H` 因此 354→406（仍在 612 预算内）。改成四个等宽方块 `BAND_W=GRID_W/4=154`：本周 / 上周 / 本月 / 全部。`ui.totals` 的形状保留（`slot.label/value`），所以既有读数用例一行没改；新增 `slot.host/label/value/income/expense/startKey/endKey`。`Calendar.GetRange("lastweek")` = 本周 CD 起点往回 7 天到前一天（没墙上钟就跟着读不出，不拿显示月凑假范围）。悬停走 `Calendar.ShowTotalsTip`，用同一扇自绘不透明提示框给三行：档位+确切范围 / 收入 / 支出。分割线照 `Options.lua:695-699` + `Matrix.lua:1610-1640` 的 `CreateTexture + SetColorTexture`：网格底一根横线（`SUMMARY_DIV_GAP=6`）、方块间三根竖线，文字上沿 `BLOCK_PAD=10` 才起，不贴线 |
| 2 分类列 | `COL_SUBTOTALS`「分类小计」→「分类」（en `Category`）；新增 `COL_TOTAL`「总计」/ `Total`。`ui.subTotal` 贴分类列底部（`SUB_TOTAL_Y = LIST_H - SUB_ROW_H`），上方一根 1px 横线；总额按完整 `bySource` 求和，不跟着类型/收支筛选收窄。列表与分类之间加 `ui.subDiv` 竖线（与日历主窗 `ui.panelDiv` 同一形状）。四个下拉 `DD_W` 从 `{150,140,200,130}` 收到 `{132,120,176,112}`，`ClipLabel` 的字节预算随宽度自动重算 |
| 3 层序 | 取证结论：**两边都要改**。GQL 与 GCL 各自都是 `UIParent` 平级 `MEDIUM`，GQL 只在 `OnShow` 那一下升 `DIALOG + Raise`（`Matrix.lua:1739-1743`），让层钩子只认三个暴雪帧（`:1157`）；GCL 开窗/点窗原来完全不动 strata/level。两插件之间**没有任何协调代码**（互搜 0 命中）。<br>GCL 主改：`Core.lua` 新增 `GCL.RaiseFront(f, close)` —— 只走模块引用（`GQL.Matrix.Frame` / `GQL.Options.Frame` / `GQL.LogWindow.Frame` / `GCL.Calendar.GetFrame`，12.x 帧名不进 `_G`）把已知 peer 拉回 `MEDIUM`，取最高层号 +10 压本窗，× 再钉回本窗 +1；`Calendar.Build` / `Flows.Build` 挂 `OnMouseDown` + `OnShow`，`Calendar.Open` 显式补一次（离线替身不派发 OnShow），`RaiseAboveCalendar` 改走这个统一入口。<br>GQL 只补一行：`Matrix.lua` 的 `frame:SetScript("OnMouseDown", ActivateMatrix)` —— 点矩阵任意一处把 DIALOG 拿回来。仍是"点谁谁在上"而非常驻置顶：暴雪后开窗口照样由 `YieldToWindow` 让回。没用 `SetToplevel` / 常驻 `HIGH` / GCL 侧 `Raise()`（GCL 替身没有，计划 §7.6 与 `Flows.lua` 注释明确禁用） |

验证：`tests/run_gcl_tests.py` **75/75 全绿**、`tests/run_gql_tests.py --syntax` **543/543 全绿**（含逐文件 syntax / upvalues 静态检查）、lint 0。
新增 GCL 三例：`calendar_round7_summary_strip`（四档净额 / 上周范围 / 横线竖线 / 留白 / 三行悬停）、
`flows_round7_category_total_and_dividers`（改名 / 总计金额 / 两根线 / 下拉收短）、
`gcl_raise_front_above_gql_peers`（注入假 GQL 帧验 DIALOG→MEDIUM、主窗与弹窗都压住对方、× 重钉、点主窗重新上前、GQL 缺席不报错）。
新增 GQL 一例 `matrix_click_reclaims_front_from_peer_addon`（点矩阵重回 DIALOG、不是常驻置顶）。
几何用例 `calendar_geometry_budget` 的顶栏断言翻成"四个方块正好铺满左栏且算进窗高"。

**仍待真机**：读数带 52px 在 12.1.5 中文字体下两行字会不会挤；GQL 设置窗/日志窗点上去时不会自动上前
（这轮只给 GQL 矩阵挂了点击抬层，其余 GQL 子窗要对称支持得让 GQL 也读 GCL 帧）；
两插件同时开着反复互点后的层号会不会被 `Raise()` 递增推高（真机 `Raise` 是同层重排，不是无限递增，离线替身用序号表达）。
