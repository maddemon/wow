# GoldQuestLedger 第 34 轮计划：换轮后上一条实例的到期时刻不许吃掉这一行

计划唯一真源。分支 master，动 `GoldQuestLedger/Ledger.lua`、`GoldQuestLedger/Matrix.lua`、
`GoldQuestLedger/Options.lua`、`GoldQuestLedger/Dump.lua`、`tests/run_gql_tests.py`。
基线 **321/321**（第 33 轮界面去取证化落地后的实测）。

## 一、用户原话与取证

12.1.5 实测（2026-10-27）：

> 刚刷新的神秘身影任务，已经有个角色做完了，没有上矩阵，什么情况？
> 报告1 当前角色刚才已经做完了这个任务，为什么记录没交过？ 截图是第二个没做过的角色看到的。
> 第二个玩家登录了几分钟，矩阵出现了，什么原因？
> 改吧

`/gql quest 神秘身影` 的关键形状（questID=89267，屠戮者高地/虚影风暴 12 小时区）：

```
矩阵：不在列（没命中关键词、本轮没人交过，金币也没到 5000）   ← 这句是写死的猜话，与下一行自相矛盾
在列靠：本轮有角色交过（玩家亲手换过金币，不看金额）            ← 准入门其实过了
倒计时：还剩 ?（来源=接口倒计时）
痕迹：本轮起点=13分前  结束时间=已到期（20小时前）
是世界任务=是  上过板=是
结论：这行界面已经没有了
```

## 二、定案（一句话）

**同一个 questID 跨实例复用；换轮只推轮次起点、不清到期时刻，而做完它的那个号身上客户端答不出新倒计时
（`Api.QuestTimeLeft=nil`、`HaveQuestRewardData=false`），于是到期时刻一直停在上一条实例（20 小时前），
矩阵"到期即移除"那道门把整行吃掉。** 第二个号没做过 ⇒ 这条还挂在他脚下那张图的板上 ⇒ 倒计时答得出
11 小时 37 分 ⇒ `Ledger.SetExpiry` 把到期时刻推到未来 ⇒ 放行。全程卡的是到期时刻，与"上过板/凭据"无关。

落点：`RollRound` 只写 `roundAt/goldRound`（`Ledger.lua:532-535`）；到期门 `Ledger.lua:1766-1768`；
`SetExpiry` 只在 `remaining > 0` 时才写（`Ledger.lua:784-795`），问不出就一个字不改。

## 三、本轮口径

**到期门只认"本轮之内读到的那条到期时刻"。** 一条到期时刻早于这条任务自己的本轮起点 ⇒ 它是上一条实例留下的，
本轮不作数，等接口自己写新的（零新增接口调用、不轮询、不扫图）。

代价照实：换轮后到倒计时读到之间，那一行不再被"到期"吃掉，但剩余时间那一格是空的、悬停不报倒计时
（本来也问不出），名字色走"读不到剩余时间"那一档（`TimeColor` 对 nil 与 ≤0 同色，不会误红）。
不动的：准入门五道、金币门槛、凭据判据、schema、`SetExpiry`/`RoundEnded` 的写入侧、
"到期已过不自动安排唤醒定时器"这条既有口径（`Sync.lua:99` 注释写明是有意的）。

## 四、任务

### T1 `Ledger.lua`：新唯一真源 `L.EffectiveExpiry(db, rec)`

放在 `L.RoundAt` 之后、`RollRound` 附近（与轮次身份同一节）。语义：

```lua
function L.EffectiveExpiry(db, rec)
    if not rec then return nil end
    local exp = tonumber(rec.expiresAt)
    if exp == nil then return nil end
    local at = tonumber(L.RoundAt(db, rec)) or 0
    if at > 0 and (exp > 1e9) == (at > 1e9) and exp < at then return nil end
    return exp
end
```

三条边界必须写进注释（注释只写"为什么"，不写"是什么"）：

- **`exp < at` 恰好就是"靠到期换的那一轮"**：`RoundEnded` 要求 `now >= exp + 松弛` 才 `RollRound`，
  所以到期换轮之后必然 `at > exp`。健康的同一轮里 `exp = 起点 + 倒计时 > at`，永远不受影响。
- **时钟域守卫 `(exp > 1e9) == (at > 1e9)`**：`L.RoundAt` 兜底会回退到 `db.cycleAnchor`（墙上秒），
  而 `expiresAt` 可能是开机秒；跨域比大小会把好好的行放回或吃掉，两种都是谎报。对不上就不判、原样返回。
  与 `L.Prune`（`Ledger.lua:1370-1371`）、`ThisCycle`、`WeekStats` 同一条既有写法。
- `at == 0`（锚点和本轮起点都没定出来）⇒ 原样返回，行为与今天一致，不借本轮之名新增放行。

副作用要如实说明：`L.RoundAt` 会顺手把 `rec.roundAt` 落账（既有行为，`MatrixRows` 每帧都在调它），
T5 的 `NextRefreshAt` 新调它一次，不新增接口调用。

### T2 `Ledger.MatrixRows`：到期门与 `remaining` 都改问 T1（`Ledger.lua:1766`、`1779`）

`local exp = L.EffectiveExpiry(db, rec)` 一处取，`expired` 与 `row.remaining` 共用它。
`row.remaining` 保持现有那条 `not rec.expiresGuessed` 判据，不许出现负数（负数会一路流到悬停，见 T3）。

### T3 `Matrix.lua`：悬停倒计时改问 T1（`Matrix.lua:416`）

现在这行是 `local exp = (q and not q.expiresGuessed) and q.expiresAt or nil`，
`FormatRemaining(≤0)` 回 `已到期`（`Matrix.lua:492-494`）⇒ 修完 T2 后这一行会带着"剩余 已到期"
挂在还活着的行上，正是本轮要消灭的那句谎报。改成问 `GQL.Ledger.EffectiveExpiry(db, q)`，
nil 就整行不显示（`FormatRemaining(nil)` 本来就回 nil，`if remStr then` 那道门已经会跳过）。

### T4 `Options.lua`：屏蔽管理右栏改问 T1（`Options.lua:94-102`）

`keepable` 里那句自拼的 `now >= rec.expiresAt` 换成"取 T1 的结果再比 now"。
这一栏是第 31 轮起唯一的屏蔽/找回入口，它和矩阵必须同一条到期口径（`Options.lua:92-93` 注释本来就要求同源）。
`ignored` 无条件留、凭据那道读 `Ledger.QuestHasCredential` 两条都不动。

### T5 `Ledger.NextRefreshAt`：跳过不生效的到期时刻（`Ledger.lua:1893-1905`）

逐条改问 T1，nil 不参与"最早到期"。效果：一条上一条实例留下的过期值不再把"最早到期"钉在过去
（那等于定点唤醒兜底永久停摆）。**不改调用方**：`Sync.lua:119` 的"到期已过/取不到 ⇒ 不自动安排"照旧。
全是过期值时返回 nil，与今天一致 ⇒ 不新增任何定时器。

### T6 `Dump.lua`：`/gql quest` 两处谎报（`Dump.lua:234-236`、`358-359`、`337-346`）

1. `矩阵：不在列（没命中关键词、本轮没人交过，金币也没到 X）` 是写死的猜话，本轮实测与它下面那行
   `在列靠：本轮有角色交过` 直接打架。改成按真判据分岔回话，两类原因分开说：
   - 被到期门吃掉 → `不在列（到期门：账本里的到期时刻 <now 且属于本轮）`——措辞照实，别再猜门槛；
   - 准入门没过 → `不在列（五道准入门都没过：没命中关键词/本轮没人交过/金币没到 X/没有客户端凭据）`
     ——把第 32 轮漏掉的"凭据"那半条补上（上一轮已向用户指出这个缺口）。
   判据一律问 `Ledger`（T1 + `QuestHasCredential` + `TurnedInThisRound`），Dump 不拼第二套。
2. `结论：这行界面已经没有了` 后面补一句是哪道门。
3. 痕迹行的"结束时间"：当 `L.EffectiveExpiry` 回 nil 而 `rec.expiresAt` 是个数时，
   照实标 `（上一条实例留下的，本轮还没读到倒计时）`。取证面要看得见污染源。

### T7 用例（`tests/run_gql_tests.py`，全部照 89267 真存档形状）

1. `a_stale_expiry_never_kills_a_row_that_a_hard_gate_holds`：第 1 轮 `DiffPool` 带 600 金 +
   `SetExpiry(1000, 43200)`（到期 44200）+ `OnTurnIn`；now=44400 再 `DiffPool` 一条金额问不出的
   （**不调 SetExpiry** = 做完的那个号答不出倒计时）⇒ 断言 `MatrixRows(db, 44400)` 有这一行。
2. 对照组 `row_still_dies_at_its_own_round_expiry`：本轮起点早于到期时刻（同一条实例）⇒ 到点仍掉出矩阵
   （96718 那条口径不许被绕掉）。
3. `a_stale_expiry_shows_no_countdown`：用例 1 的形状下 `row.remaining` 必须为 nil（不许负数、
   不许"已到期"流到界面）。
4. `panel_right_column_shares_the_round_expiry_gate`：现有 `panel_lists_no_expired_or_uncredentialed_rows`
   （`:6596-6633`）保持绿 = 已过期带凭据的旧 ID 仍不进右栏；另加一条：换轮后到期早于本轮起点的行
   **该**在右栏看得见（找回入口与矩阵同源）。
5. `next_refresh_skips_a_stale_expiry`：一条 `expiresAt=5000` 但 `roundAt=6000` + 一条 `expiresAt=9000`
   生效 ⇒ 回 9000；全是过期值 ⇒ nil。现有 `next_refresh_at_is_earliest_expiry`（`:4007`）必须保持绿
   （它的 `cycleAnchor=0` ⇒ T1 原样返回）。
6. `dump_names_the_gate_that_dropped_the_row`：跑 `Dump.Quest`，断言到期那种形状下报告里出现"到期"、
   **不出现**"本轮没人交过"这句与判据矛盾的话（负向）。
7. `cross_clock_domain_expiry_is_never_claimed_as_stale`：`roundAt` 是墙上秒、`expiresAt` 是开机秒
   ⇒ T1 不许判成"上一条实例"（原样返回到期值）。

### T8 文档连带

- `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 追加 §34（口径 + 落点 + 用例名）。
- `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md` 的到期/换轮段补一句：
  到期门只认本轮之内的到期时刻，唯一真源 `Ledger.EffectiveExpiry`。

## 五、验收

1. `PYTHONUTF8=1 python tests/run_gql_tests.py` 全绿（321 + 新增 7 条 = 328，含 `upvalues:Ledger.lua`
   等静态守卫）。
2. 逐行自查 `git diff`：`Sync.lua`、`Config.lua`、schema、准入门五道、金币门槛、`SetExpiry` 写入侧
   一个字没动；`grep -n "expiresAt" GoldQuestLedger/*.lua` 确认界面/右栏/定时器三处不再自拼判据。
3. 零新增接口调用（T1 纯读账本；`L.RoundAt` 的落账副作用照旧）。
4. 汇报用大白话：这条行为什么会自己好、哪些同类一起修了，并如实说明**没在真客户端验证**，
   让他 `/reload` 后自己看那条换轮的行。

## 六、工作区

共享工作区，并行会话在改 `AHSnipe/`、`tests/run_tests.py`、`docs/tasks/2026-10-13-ahsnipe-plan.md`
（本轮开工时 `git status` 实测这四个是脏的）。**只提交本轮这五个文件**，提交前 `git status` 复核。

## 七、落地修正（review 时改的，与上面 T 段不一致处以本节为准）

1. **T6 的分岔顺序原来排错了**（子代理实现的版本把到期门放在准入门之前）。矩阵里到期门排在准入门
   **之后**（`if ... and RowAdmitted then ... if expired then skip`），报告反过来就会把"既没过准入、
   又带着过期值"的行说成到期门砍的 —— 报错凶手。改成照矩阵同序：屏蔽 → 占行 → 准入 → 到期 → 同名去重。
2. **准入门抽成唯一真源 `L.RowAdmitted(db, questID, rec, money, kwHit, turnedIn, floorMoney)`**，
   矩阵与 `/gql quest` 读同一条：Dump 里再拼一套五道门的话，准入门一改口径这里就说瞎话
   （第 32 轮漏"客户端凭据"那半条就是这个价）。`floorMoney` 仍由 `MatrixRows` 在循环外读一次传入，
   不许在每帧每条都问 `Config`（那是 N 次账本查找）；诊断侧传 nil，函数自己回退到 `L.MatrixMoneyFloor()`。
   顺带导出 `L.ManualMoneyByQuest`，报告里"金币够不够"那道用与矩阵同源的 money（少这一份会误判凶手）。
3. **`Options.KeywordMatches` 的同名去重权重**（`evidence()`）改问 `L.EffectiveExpiry`：
   矩阵的 `r.remaining` 现在就是它算的，右栏再读裸 `rec.expiresAt` 会出现一条换轮的行在右栏算"有剩余"、
   在矩阵算"没有"。金币那一格的分歧（矩阵打 row 的钱、右栏打 record 的 `lastMoney`）是既有口径，本轮不动。
4. **用例 321 → 329**（不是 328）：T7 的 7 条之外补一条
   `test_dump_reports_the_admission_gate_before_the_expiry_gate` 钉住上面第 1 条的顺序 ——
   两条同样"本轮之内读到且已过去"的到期时刻，只差金币门槛过没过，必须报两道不同的门。
   这条做过反向验证：临时把 Dump 两道门调序 → 用例红（"没过准入门的行要先报准入门，不许报到期门"），
   改回 → 329/329。

