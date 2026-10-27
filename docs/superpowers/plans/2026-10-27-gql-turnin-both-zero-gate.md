# GoldQuestLedger 第 35 轮计划：接和交两处都答"0 金币"时，"本轮交过"不再算上板凭据

计划唯一真源。分支 master，动 `GoldQuestLedger/Ledger.lua`、`GoldQuestLedger/Dump.lua`、
`tests/run_gql_tests.py`，文档两份。基线 **329/329**（第 34 轮落地后的实测）。

## 一、用户原话与取证

12.1.5 实测（2026-10-27 晚）：截图里祖阿曼最下面一行 **沼地威胁 91803**，悬停写着
"这轮做它不给金币"、"爱情之力 交于 10-27 18:18 到手 0🪙"，只有一格 ✓。他的原话：

> 那必然是理解错了，分两个阶段说
> 1、接到任务，应该能查询到任务奖励，如果是金币满足条件 自动上，松露大乱斗就是双模式，
>    当我接到任务或者说交任务的时候，必然是金币模式，一定要上板
> 2、交任务，获得>500G，这个不准的，我发现很多记录 没获取到角色的金币奖励，这个不是太靠谱。
>    但大部分是获到了的。
> 如果接任务金币是0，交任务金币还是0，且不是关键字任务，那不能上，就比如这个 沼地威胁

**取证（真存档 `D:/Games/.../WTF/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua`，
mtime 10-27 16:58；18:18 那次交记录还没落盘，是补进内存重放的，档一个字没改）**

沼地威胁 91803 五道门逐条问：金币 0（门槛 500 金）不过、关键词不过、日志收的没有、
注册周常没有、**只有"本轮交过"过** —— 把那次交记录补进去，矩阵从 20 行变 21 行，
新增的就是它，位置在祖阿曼最下面，与截图一致。所以它不是金币上板的，是"交过"那道门放的，
而那道门第 25 轮起**故意不看金额**。

## 二、定案（一句话）

**"本轮交过"这道门，只在"接（本轮奖励读）"和"交（实际到手）"两处客户端都答了 0 金币时不再放行；
任何一处答不出，照旧放行。** 关键词任务不受影响（它走的是最后一道 `kwHit + 凭据`）。

## 三、本轮口径

- **两处 0 才算 0**：
  - 接 = `L.GoldVerdict(db, questID) == false`（客户端这一轮明说过不给金币）；
  - 交 = 本轮那几笔交任务的到手金额**每一笔都是数字 0**（`Ledger.OnTurnIn` 记的 `g`）。
- **三种"答不出"都必须保住行**（这是松露大乱斗那一类，第 25 轮立这道门就是为了它）：
  - 奖励读不出金额（`money = nil`）→ 真存档 95484 松露大乱斗、91808 资源回收都是这一档；
  - 到手金额没记上（`g = nil`，窗口被污染/采样失败时 `Sync.FinishWindow` 传的就是 nil）；
  - 任一号任一笔 `g > 0` → 更不用判，本来就该在列（松露大乱斗赤尾之寂那笔 962 金）。
- **代价照实**：双模式任务在"物品档"读到 0、而那次交任务的钱包窗口又恰好量出 0（不是 nil）
  ⇒ 这一行会掉。真存档里量出来的噪声是**往小里偏**而不是往 0 偏（94611 猎手与猎物 池里 107 金、
  到手记成 41 银；89267 神秘身影 池里 1022 金、到手记成 1 金 84 银），这些行都因为
  "接"那一处读到过金币而保住 —— 用户自己说"这个不是太靠谱，但大部分是获到了的"，
  所以只把 0 当否决、不当凭据。
- **不动的**：五道门的其余四道、金币门槛（默认 500 金）、`kwHit` 只免门槛不算凭据（第 32 轮）、
  `ignored` 永久否决、到期门与 `Ledger.EffectiveExpiry`（第 34 轮）、schema（本轮判据是读的时候判，
  见 §六的自愈说明）。

## 四、连带缺陷（同类一起盘出来的，本轮一并修）

`L.GoldVerdict` 的注释写着"false=客户端明确答过非金币"，但**直接读到 0 那一条路永远回不了 false**：

- `Ledger.lua:655`（本轮直接读到金额）`obs.gold = q.money > 0 or nil` ⇒ 读到 0 时 `gold = nil`；
- `Ledger.lua:686`（新建记录）`gold = L.IsGoldSig(sig) or …` ⇒ 签名 `0` 时 `gold = false`。

同一个事实（客户端答了 0）两处写两个值 ⇒ 沼地威胁实测 `GoldVerdict = nil`（"判不了"），
导出金币节与悬停三档从此各说各话。本轮把 655 改成 `obs.gold = q.money > 0`（**只改直接读到那一支**；
667 那支是从别的签名**搬**过来的，搬来的 0 不是本轮客户端答的，照旧 `> 0 or nil`）。

## 五、任务

### T1 `Ledger.lua`：交任务到手金额按轮次聚合（与 `TurnedInThisRound` 同一次遍历）

`TurnedInThisRound`（`Ledger.lua:1559-1583`）现在只回 `{[questID]=true}`。把它的循环体抽成一个
local `TurnInCred(db)`，一次遍历回两份表：

- `on[questID] = true` —— 语义与今天逐字相同（`cycle == db.cycleAnchor` 且 `t >= L.RoundAt(db, rec)`），
  导出的 `L.TurnedInThisRound` 改成它的薄壳，**返回形状不许变**（Dump、Options、用例都在读它）；
- `gain[questID]` = 本轮那几笔 `g` 聚合成一个值：**任一笔 `> 0` 就回那个正数**；
  否则**每一笔都是数字 0** 才回 `0`；只要有一笔 `g` 不是数字就回 `nil`（判不了）。
  这个优先级必须写进注释：反过来（先判 0 再判 nil）就等于"一个号没量到钱"能替全队否决这一行。

新增 `L.TurnInGainsThisRound(db)` 导出 `gain` 那份。两份都从同一次遍历出来，
**不许在别处再翻一遍 `db.turnins`**（`Ledger.lua:768`、`Dump.lua:206` 两处注释钉的就是这条）。

### T2 `Ledger.lua`：唯一真源 `L.TurnInAdmits(on, gain, noGold)`

```lua
--- 交过这一道放不放行（第 35 轮）。矩阵准入门与 Dump 的"在列靠"都问它，别处不许再拼。
--- on=本轮有号交过；gain=本轮那几笔的到手金额（0=每笔都答了没拿到钱，nil=有一笔答不出）；
--- noGold=客户端这一轮明说过它不给金币。
--- 两处都答 0 才否决：用户 2026-10-27"接任务金币是0，交任务金币还是0…那不能上"。
--- 任何一处答不出都放行 —— 松露大乱斗那种"做了金币任务却读不到钱"正是要保的样本。
function L.TurnInAdmits(on, gain, noGold)
    if not on then return false end
    return not (noGold and gain == 0)
end
```

### T3 `Ledger.lua`：`L.RowAdmitted` 收口（`Ledger.lua:1736-1742`）

新增末位参数 `gainOf`（可缺省 = 判不了 = 行为与今天一致），"本轮交过"那一支改成问 T2：

```lua
if L.TurnInAdmits(turnedIn and turnedIn[questID], gainOf and gainOf[questID],
    L.GoldVerdict(db, questID) == false) then return true end
```

`L.MatrixRows` 里与 `turnInOf` 一起算一次 `local gainOf = TurnInCred(db).gain`（或新导出的那份），
**每帧不重复遍历**，零接口调用。

### T4 `Ledger.lua:655`：直接读到 0 时把 `obs.gold` 写成 `false`（§四）

只改这一支；667（搬来的）与 686（新建）一个字不动。改完 `L.GoldVerdict(沼地威胁)` 必须从
`nil` 翻成 `false`，且 `Ledger.GoldList`（`Ledger.lua:1344` 只收 `== true`）不受影响。

### T5 `Dump.lua`：两处话必须跟着改口径

1. `offListReason`（`Dump.lua:233`）调 `RowAdmitted` 时把 `gainOf` 传进去；
   "五道准入门都没过"那句要能区分出**这一条是被本轮新收口砍的**：当 `turnedIn[questID]` 为真而
   `L.TurnInAdmits` 回 false 时，报
   `本轮交过不作数：接和交两处客户端都答了 0 金币（不是关键词任务）`，
   别让玩家以为"我做过它，报告在说谎"。
2. "在列靠"（`Dump.lua:312-314`）那句 `本轮有角色交过（玩家亲手换过金币，不看金额）`
   现在**不再无条件成立**：改成问 `L.TurnInAdmits`，放行时写
   `本轮有角色交过（到手 <金额或"没量到">）`，被否决时**整条不写**（它已经不是凭据了）。
   取证面要说清是哪一处答的 0：`接=0 金币、交=0 金币` 两个字面值都印出来。

### T6 用例（`tests/run_gql_tests.py`，全部照 91803 真存档形状）

1. `a_turnin_with_no_gold_on_both_sides_does_not_admit`：奖励读到 0 + 一笔 `g=0` ⇒ 不在矩阵。
2. 对照组 `a_turnin_with_unread_reward_money_still_admits`：`money=nil` + 一笔 `g=0` ⇒ 在矩阵
   （松露大乱斗/资源回收那一档）。
3. 对照组 `a_turnin_with_unmeasured_gain_still_admits`：`money=0` + 一笔 `g=nil` ⇒ 在矩阵。
4. 对照组 `a_turnin_that_paid_gold_admits_even_at_a_zero_reward_read`：`money=0` + 一笔 `g=0`
   + 另一号一笔 `g>0` ⇒ 在矩阵（钉 T1 的聚合优先级；把聚合改成"有 0 就 0"这条必须红）。
5. `a_zero_reward_read_is_a_verdict_not_a_mystery`：跑一次 `DiffPool` 读到 0 ⇒
   `L.GoldVerdict` 回 `false`（钉 T4）；再补一条搬来的 0 仍回 `nil`（钉 667 没被顺手改）。
6. `keyword_row_survives_the_both_zero_rule`：撞关键词 + 有凭据 + 两处 0 ⇒ 仍在矩阵。
7. `dump_names_the_both_zero_turnin_rejection`：跑 `Dump.Quest`，断言那种形状下报告里出现
   "接和交两处客户端都答了 0 金币"，且**不出现**"本轮有角色交过"（负向，防两处各说各话）。
8. 静态守卫：`grep` 式用例确认 `db.turnins` 没有新增第三处遍历、`TurnInAdmits` 是矩阵与 Dump 唯一的问法。

### T7 文档连带

- `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 追加 §35（原话 + 定案 + 落点 + 用例名 + 真存档影响面）。
- `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md` 准入门那段补一句：
  "本轮交过"从第 35 起要接、交两处都没答 0 才算凭据，唯一真源 `Ledger.TurnInAdmits`；
  `GoldVerdict` 的 false 档从此可达。

## 六、老存档要不要迁移

**不要。** T3/T2 的判据在读的时候算；唯一写侧的 T4 会在下一次池快照重读到那条 0 时当场补上
`gold=false`（面板一刷新就重读，不用等换 CD）。沼地威胁 91803 实测 `currentSig='0'`、
每轮快照都重读 ⇒ 部署后第一次刷新就掉。`Config.SCHEMA_VERSION` 不动。

## 七、验收

1. `PYTHONUTF8=1 python tests/run_gql_tests.py` 全绿（329 + 新增 ≈ 337）。
2. **真存档回放**（只读，`Wow.exe` 在跑，PID 46524，绝不写档）：补上 18:18 那次交记录，
   跑 `MatrixRows` ⇒ 断言**只掉沼地威胁这一行**，其余 20 行一字不变；
   并断言 95484 松露大乱斗、91808 资源回收这两条"接读不出金额"的**准入照旧为真**
   （它们今天本来就不在板上，是因为到期门，别把两件事混着报）。
3. 逐行自查 `git diff`：`Sync.lua`、`Config.lua`、schema、金币门槛、到期门、`SetExpiry` 写入侧
   一个字没动；`grep -n "db.turnins" GoldQuestLedger/*.lua` 确认没有第三处遍历。
4. 零新增接口调用（T1 纯读账本；`L.GoldVerdict` 也是纯读）。
5. 变异验证：T2 的三条件、T1 的聚合优先级、T4 的 `false` 各自单独摘一遍，
   每次只许对应那条用例红。
6. 汇报用大白话：为什么"到手 0"不能单独当凭据、哪两条被保住了；并如实说明
   **第 34、35 轮都还没在真客户端验证过**，让他 `/reload` 后刷新一次面板看沼地威胁掉不掉。

## 八、落地修正（实施后主 agent 逐行 review 改的，与上面不一致处以本节为准）

1. **"接"这一头改认本轮那个数，不认 `obs.gold` 那面旗。** §五 T2/T3 原设计让门槛问
   `L.GoldVerdict == false`，而 T4 只把**直接读到 0** 那一支写成 `false`。拿 12.1.5 活档
   （20:55 那份）跑下来**沼地威胁照旧在板上**：它 `currentSig="-1"`，那份 observation 里的 0
   是同轮另一副签名**搬**来的，旗子是空的 ⇒ `GoldVerdict` 回 nil。这就是 10-25 那次
   "没有变化，是我拷贝错了吗"的形状。所以判据落到 `L.GoldVerdict` 里加一句
   `if money == 0 then return false end`（`Ledger.lua:1441` 附近）：**本轮读到的金额是个数、
   却是 0 ⇒ 否**。搬来的 0 也算本轮答过 —— 延续那道门第 34 轮已经收成"只许同轮之内搬"，
   上一条实例的 0 搬不进来；用户 10-27 那句"仅限这个任务本次生命周期"就是这个意思。
   T4 那处写侧改动**保留**（同一个事实两处写两个值本来就是缺陷），但门槛不再依赖旗子；
   `obs.gold` 那面旗照旧不许为搬来的数立起来（用例单独钉）。
   摘掉这一句 ⇒ 只有 `the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read`
   与 `a_zero_reward_read_is_a_verdict_not_a_mystery` 两条红，其余全绿（变异验过）。
2. **§六"下一次刷新才生效"那句作废**：按 1 的口径，部署（`/reload`）后**当场**就掉，不用等重读。
3. **Dump 那句"且不是关键词任务"要分岔**：撞了关键词、客户端一条凭据都答不出的行也是被
   "两处 0"砍的，对它说"不是关键词任务"是谎报 ⇒ 分两支措辞，用例
   `test_dump_blames_the_missing_credential_not_the_keyword` 钉住（含负向断言）。
4. **用例账 329 → 339**（新增 10 条，比 §五 多 2 条：真存档原形状那条 + 关键词缺凭据措辞那条）。
5. **真存档全板对照**（只读，`Wow.exe` 在跑；新门 vs 旧的"不看金额"）：
   22 行 → 21 行，**掉出只有沼地威胁 91803，多进 0 行，其余 21 行的名字与金额一字未变**；
   要保的三条实测照旧放行：松露大乱斗 95484（verdict nil + 到手 962 金）、
   资源回收 91808（verdict nil + 到手 1118 金）、追踪萨拉塔斯 98172（verdict true）。
   注：95484/91808 今天本来就不在板上，是**到期门**（各自的结束时刻已过），与本轮无关，
   汇报时别把两件事混着说。

## 九、用户最后确认的口径（本轮判据的出处）

> 我记得我以前也说过，只要这个任务有角色能看到/接到/交任务是金币任务，不管其他角色，直接上矩阵。
> 但是这个沼地威胁可不满足，不应该上。当然 仅限这个任务本次生命周期。

- **"任一号答得出金币 = 全账号上板"**：账本 `db.quests[qid]` 本来就是账号级那一份，
  任何一个号的读数（池奖励、按号奖励、任一笔交任务到手 >0）都写进同一份记录 ⇒ 已经是这个行为，
  本轮没动它；用例 `a_turnin_that_paid_gold_admits_even_at_a_zero_reward_read` 钉的正是
  "一个号量到 962 金、另一个号量成 0 ⇒ 合成值取那个正数"。
- **"仅限本次生命周期"** = 第 25/34 轮立的轮次边界：换一条实例，粘滞、搬运、勾全部作废，重新读重新判。
- **沼地威胁不满足**：本轮它没有任何一处金币证据（接=0、交=0）⇒ 不该在列 ⇒ 本轮的修法就是让它掉。


## 十、工作区

共享工作区，并行会话在改 `AHSnipe/`、`tests/run_tests.py`、`docs/tasks/2026-10-13-ahsnipe-plan.md`
（本轮开工时 `git status` 实测这八个文件是脏的）。**只提交本轮这三个源文件 + 两份文档**，
提交前 `git status` 复核。
