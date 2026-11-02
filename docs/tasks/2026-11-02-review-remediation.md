# 计划：2026-11-02 代码审查整改（全量，按优先级排）

> 状态：**待实施**。
> 来源：本轮对未提交变更的四路并行审查 + 主 agent 逐条复核（红测试是我实跑的，行号是当场对的）。
> 用户口径（2026-11-02 定）：**不分高/中/低，有问题都要修，优先级只决定先后**。
> 执行纪律：一任务一 commit；每个 GQL 任务后跑 `python tests/run_gql_tests.py`、每个 AHSnipe 任务后跑 `python tests/run_tests.py`；不碰存档语义的任务不许写 SavedVariables（若要回放真档先查 Wow.exe 是否在跑）。

## 〇、三条贯穿全表的判定原则

1. **判不出 = 不成立（fail-closed）**，但"不成立"的方向不许吞掉玩家关注的行（`IsTrackedMap` 那一类是反例）。
2. **取证字段丢了也是 bug**：注释声称记了、实际恒 nil，等同于账本说谎（T3）。
3. **测试红了先分清是"用例过时"还是"真回归"**，逐条裁决，禁止为了绿灯批量删断言（T1）。

---

## 一、P0 —— 先做，做完才谈得上验证其余

### T1 GQL 测试套件清零（374/392 → 392/392）

- 证据：`python tests/run_gql_tests.py` 实跑 374/392。红名单（18 条）：
  `test_turnin_money_window_capture_and_abort`、`test_money_window_polluted_by_frame_object`、`test_second_turnin_event_does_not_reset_window`、`test_logout_within_money_window_still_records_turnin`、`test_back_to_back_turnins_both_recorded`、`test_money_window_samples_during_window`、`turned_in_event_records_before_the_window_settles`、`turnin_with_zero_reward_read_does_not_admit`、`turnin_state_and_week_stats`、`manual_not_double_counted_with_turnin`、`matrix_columns_stale_and_manual_only_quest_kept`、`turnin_settlement_repaints_matrix`、`last_completion_takes_the_newer_record`、`turned_in_with_gain_retracts_the_no_gold_line`、`zero_gain_turnin_keeps_the_no_gold_line`、`turnin_of_pooled_quest_still_settles`、`keyword_weekly_turnin_gets_recorded`、`weekstats_unknown_when_clock_domains_split`。
- 根因：[GoldQuestLedger/Sync.lua:1226-1253](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1226) 已按用户 2026-10-29 定稿把 15 秒钱窗整体退役（`RecordTurnIn` 只写 `k/t/cycle`，无 `g`），用例仍在断言窗口金额与窗口生命周期。
- 修法：逐条分诊，不批量删。
  - **A 类（测窗口本身）**：`test_money_window_*`、`test_second_turnin_event_does_not_reset_window`、`test_logout_within_money_window_still_records_turnin`、`turned_in_event_records_before_the_window_settles` —— 窗口这个概念已不存在，整条删除并在计划里留一句"退役原因=用户 10-29 定稿，金币改由奖励读取负责"。
  - **B 类（断言金额字段）**：`turnin_state_and_week_stats`、`matrix_columns_stale_and_manual_only_quest_kept`、`last_completion_takes_the_newer_record`、`turned_in_with_gain_retracts_the_no_gold_line`、`zero_gain_turnin_keeps_the_no_gold_line`、`a_turnin_with_zero_reward_read_does_not_admit` —— 改成新真源：金额只来自 `rec.goldMoney` + `rec.goldKey`（见 [Ledger.lua:1039-1065 LastCompletion](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1039)、[Ledger.lua:1640 WeekStats](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1640)），用例先用 `NoteGoldThisInstance` 造奖励读数，再验绿勾/统计。
  - **C 类（可能是真回归，不许改断言迁就）**：`turnin_settlement_repaints_matrix`（矩阵重画）、`turnin_of_pooled_quest_still_settles`、`keyword_weekly_turnin_gets_recorded`（记账门与准入门同源）、`manual_not_double_counted_with_turnin`（同一实例记两笔）、`weekstats_unknown_when_clock_domains_split`（跨域报未知）—— 先看红在哪一句，判是代码坏还是措辞坏。**`manual_not_double_counted_with_turnin` 与 T6 直接耦合**（nil 编号去重失效就会双记）。
- 验收：`python tests/run_gql_tests.py` 全绿；孤儿守卫（[tests/run_gql_tests.py:447-471](file:///D:/Repos/wow/tests/run_gql_tests.py#L447)）不许被绕过。

### T2 AHSnipe 盲窗判据 off-by-one（`Proven` 恒放行盲窗前最后一条答复）

- 证据：[AHSnipe/Scanner.lua:254-260](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L254) `blindFrom = lastAnswerAt`（=上一次答复时刻），但同一行的 `seenAt` 在 `NoteAnswer()`（:274）**之后**才取 `GetTime()`（[:212-215](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L212)）⇒ 那条旧行恒 `seenAt > blindFrom`，通过 [AHSnipe/SnipeList.lua:204-208](file:///D:/Repos/wow/AHSnipe/SnipeList.lua#L204)。断流 5~17 s 后按钮仍可凭 15 秒前的旧行点亮。
- 同一处写法还有一遍：[Scanner.lua:574](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L574) `Pause()` 里 `if lastAnswerAt > blindFrom then blindFrom = lastAnswerAt end`，而它上面的注释明说"暂停期间留下的行恢复后必须被重新答复过才能再点"——现在这条不成立。
- 修法：两处都改成 `blindFrom = GetTime()`（检出断流/暂停的当下时刻）。这样"当下这次答复"的行仍可通过（它是盲窗后重新答复的），盲窗前的行一律不通过。`lastAnswerAt = 0` 那句保持。
- 附带口径**已定**（用户 2026-11-02）：[SnipeList.lua:513-527](file:///D:/Repos/wow/AHSnipe/SnipeList.lua#L513) `RewriteRowQty` 在均价否决那一刻刷新的 `seenAt` **算证据、维持现状不动**——那一刻的数量是服务器当场给的实测存量，比旧数字新；否决后要能接着抢下一档。
- 验收：`python tests/run_tests.py` 全绿 + 新增用例「断流后旧行不可点、被重新答复后可点」；「Pause→Resume 期间留下的行不可点」。

### T3 `OnTurnIn` 实参错位 ⇒ 流水 `sig` 恒 nil

- 证据：[Sync.lua:1249](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1249) 5 参调用，签名是 [Ledger.lua:1012](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1012) `(db, charKey, questID, t, g, sig)` ⇒ `rec.currentSig` 落进注释里"函数内**忽略**"的 `g` 槽，写进流水的 `sig`（:1027）永远是 nil。与 [Ledger.lua:1006](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1006) "记完成时看到的奖励签名（sig，取证用）"自述矛盾。
- 现状全仓没有 `r.sig` 读者（grep 已确认）⇒ 判定不受影响，但这是一颗"改签名就炸"的雷。
- 修法：把已死的 `g` 形参**整体删掉**（不留签名兼容 shim，用户口径：不要向后兼容垫片），签名改 `(db, charKey, questID, t, sig)`，[Sync.lua:1249](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1249) 调用不动，同时删掉 :1008-1011 那段"5 参调用/兼容"的注释。离线用例里直调 `OnTurnIn` 的地方跟着改（属 T1 同一批 commit 可）。
- 验收：新增/改写一条用例钉「流水行里 `sig` 等于 `rec.currentSig`」，再跑全绿。

---

## 二、P1 —— 账本口径与生命周期

### T4 到期门跨时钟域守门回补

- 证据：本轮 diff 删掉了 [Ledger.lua:782-803 InstanceExpired](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L782) 开头的 `if (endAt > 1e9) ~= (n > 1e9) then return false end`；[Ledger.lua:800](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L800) 现在直接 `endAt <= n ⇒ return true`。
- 前提不成立：[Sync.lua:64-70 Clock()](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L64) **仍保留 uptime 兜底**（`WallClock()` 取不到就返回开机秒并记 `db.clockDomain="uptime"`），而 [Sync.lua:252](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L252) `rec.roundEnd = now + secs` 用的就是这个 `now` ⇒ 一次降级会话就能把开机秒域的 `roundEnd` 写进账本，下次登录拿 epoch 一比，所有带 `roundEnd` 的活行瞬间判到期从矩阵消失。
- 不一致：身份侧 [Ledger.lua:634-644 InstanceEnd](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L634) 仍在卡同域（:641）。两门现在一个卡一个不卡。
- 修法：**只回补 `endAt <= n` 这一支的域校验**（跨域 ⇒ `return false`，判不出就不判到期）；`lastSeenAt` 保活的删除是 10-30 的有意修法，保持删除不动。[tests/run_gql_tests.py:9041](file:///D:/Repos/wow/tests/run_gql_tests.py#L9041) 那条被削弱成 6 断言的用例补回跨域那一断（文档字符串同步改口：删的是保活，不是守门）。
- 验收：用例「`roundEnd` 写 uptime、`now` 传 epoch ⇒ 不判到期」+「同域且已过 ⇒ 照旧到期」两条都在。

### T5 身份算不出时交割整笔不记（完成计数丢）

- 证据：[Sync.lua:1244-1245](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1244) `local key = L.InstanceKey(...); if not key then return end`。被删的旧 `SettleTurnInNow` 是无条件落账（读侧不匹配而已）。`InstanceKey` 回 nil 有明确路径：板面值跨域/已过（[Ledger.lua:640-641](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L640)）、`cycleAnchor=0`。⇒ 边界时刻（板上倒计时刚过就去交）这一笔直接不记，周统计少一条。
- 修法：去掉这道 `return`，让流水照写（`k` 为 nil ⇒ 读侧永不匹配，符合"没编号=不匹配"）；但必须补上 **nil 编号的当场去重**，否则 `QUEST_COMPLETE` + `QUEST_TURNED_IN` 连着来会记两笔（`SameInstanceKey(nil,nil)` 恒 false，见 [Ledger.lua:555-557](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L555)）：去重条件改成 `last and (L.SameInstanceKey(last.k, key) or (key == nil and last.k == nil and last.t == t))`。
- 关联：T1 的 `manual_not_double_counted_with_turnin`、`turnin_of_pooled_quest_still_settles` 两条用例就是这个边界的守卫。

### T6 `pin:` 注册键没有退订路径，且会累积多把键

- 证据：写点 [Sync.lua:645-655](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L645)（金币门收的 pin 行注册成 `pin:<事件名>`）；退订只有 [Sync.lua:1160 UnregisterKeyword](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1160)，只清 `kw:`；[Ledger.lua:1749 Prune](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1749) 对 `rec.regKey` 永久豁免按时清；[Ledger.lua:789](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L789) 又让带 `regKey` 的行不参与"本周没被看见过 ⇒ 到期"。全仓 grep 无 `pin:` 的读者。⇒ 事件散场后该行走完整个 CD，pin 名轮换还会留下多把 `pin:*`。
- **用户 2026-11-02 定的口径（推翻原设计前提）**：拿"暗影汇聚之地"举例时用户明确 ——「它是普通的世界金币任务，在屠戮者高地（属于虚影风暴），当然要收录，**和其他普通金币任务一样。到期就要自动消失**」。⇒ pin 收进来的行**不配拥有任何特殊驻板豁免**。
- 修法：
  1. **到期不豁免**：`pin:` 前缀的 `regKey` 不再享受 [Ledger.lua:789](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L789) 那条"本周没被看见过"豁免（`kw:`/内置周常照旧豁免——那是玩家点名或整周常驻的身份）。判据用前缀，不新开字段。
  2. **散场退注册身份**：快照跑完后，若某行的 `regKey` 是 `pin:` 且**连续 3 次**快照都没再见到那个 questID（用户选的正是"扛得住提示不及时"那一档），退掉 `regKey`、记录保留，交给 1 的到期门处理。3 记成常量并写明它是为了扛 pin 延迟。
  3. **不累积多把键**：注册时若旧 `regKey` 是 `pin:` 且与新键不同，先退旧再挂新。
- **需游戏内验证（决定这条的实际效果）**：[Sync.lua:648-652](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L648) 注释记录的是"它不在世界任务池（按图过 `IsWorldQuest` 时被滤掉）"，而用户 11-02 说它就是普通世界金币任务。两者冲突 ⇒ 先现场确认屠戮者高地/虚影风暴那张图的池读取里到底有没有这条（`/gql probe` 按图列池 + `IsWorldQuest` 答案）。若它本该从池路收进来，那 pin 收行对它就是不需要的绕行，真正的病在**那张图没被读到**（关联 T13：`IsTrackedMap` 把"判不了"折成"确定不是"）。
- 验收：用例「`pin:` 行到期门照砍，不像周常那样驻板」「散场 3 次后 `regKey` 被退」「同一 quest 换 pin 名只剩一把键」。

### T7 AHSnipe Kick 条件收窄（曾查过的物品永不补轮）

- 证据：[Scanner.lua:712-714, 731-732](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L712) 改成 `not lastQueried[itemID]`（旧条件只排除最近一次）⇒ 玩家浏览过"曾被扫描查过"的物品后，该物品不再进补轮。属轻微功能回退。
- 修法：恢复"只排除当前在途那一次"的口径，同时保留本轮 diff 想解决的重复查询问题（用 `req.roundStartAt` 或 lastQueried 的时刻而不是键存在性）。

---

## 三、P2 —— 测试可信度 + 界面代价

### T8 AHSnipe 替身纪律（最危险的一条，因为它是"测不出来"的成因）

- 证据：[tests/run_tests.py:1961-1995](file:///D:/Repos/wow/tests/run_tests.py#L1961) 的 `Mock.__index` 对任意未知键自动造方法并返回 `Mock("ret")`；`RegisterEvent` 没实现（真客户端对未知事件名直接 error）；:1989-1991 把 `GetFrameLevel/GetEffectiveScale` 写死 10/1（真客户端 nil 时是 nil）。对照 GQL 侧 [tests/run_gql_tests.py:186-190](file:///D:/Repos/wow/tests/run_gql_tests.py#L186) 明令"不许出现真客户端没有的方法名"、`RegisterEvent` 抛错。⇒ [AHSnipe/Core.lua:90-92](file:///D:/Repos/wow/AHSnipe/Core.lua#L90) 事件名打错，离线 71/71 全绿、进游戏崩。
- 修法：AHSnipe 的 Mock 换成"未知键访问即抛错 + 已知 API 白名单 + `RegisterEvent` 校验事件名"，照 `run_gql_tests.py` 的口径来；写死的 `GetFrameLevel/GetEffectiveScale` 改成夹具可控（默认 nil），让依赖它的用例显式设置。
- 代价照实：这一改会一次性暴露若干"靠幻影方法跑绿"的用例，需要逐个补真接口形状。**这是 T2/T7 能被验证的前提，建议排在 T2 之后立刻做**。

### T9 两套计时语义合一

- 证据：[run_tests.py:2008-2012](file:///D:/Repos/wow/tests/run_tests.py#L2008) 的 `C_Timer.After/NewTimer` 直接丢回调，同文件 :1324-1331 的夹具却是可推进队列。smoke（:2053+）用前者 ⇒ `BUY_RETRY`、独立超时、闸门这些延时分支一条都不跑，"SMOKE-OK 购买链路"只覆盖同步事件栈。
- 修法：runner 内只留一个计时夹具（可推进队列），smoke 也用它；补一条"推进时钟后延时回调确实执行"的守卫用例。

### T10 星级/试剂品质链路零覆盖 + pending-issues bug1

- 证据：[AHSnipe/ConfigPanel.lua:48-68](file:///D:/Repos/wow/AHSnipe/ConfigPanel.lua#L48) `C_TradeSkillUI.GetItemReagentQualityByItemInfo` 在 `run_tests.py` 无替身、零用例；[docs/pending-issues.md](file:///D:/Repos/wow/docs/pending-issues.md) bug1：`Config.GetItems` 不返回 `reagentQuality` ⇒ [ConfigPanel.lua:147](file:///D:/Repos/wow/AHSnipe/ConfigPanel.lua#L147) 条件恒真。
- 修法：补替身（返回类型按真客户端：可能是 nil）+ 用例覆盖"读不到星级"和"读到 1~3 星"两岔；bug1 按 pending-issues 结案。

### T11 开面板同步抓池的代价收敛（**行为不改，只加护栏**）

- 证据：[Matrix.lua:1884-1892](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1884) 去掉 `Sync.PoolFresh()` 门、开板即 `SnapshotNow` —— 这是用户 2026-11-01 点名要的（"打开矩阵的时候最好能自动刷新"），**不许改回去**，但它与"默认零新增接口调用"相抵，且 [Sync.lua:1173 PoolFresh](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1173) 从此无调用方（只剩注释提到）。
- 修法：加节流（同一 CD 内距上次快照 < N 秒不重抓）—— **暂缓**：节流没问过用户，且它会把"点开就是现在"这个点名的行为打回去。本轮只清理 `PoolFresh` 死码（并入 T18），界面行为一行不动。若之后要加节流，先取口径。

### T12 tooltip 组装缺存在性守卫

- 证据：[Matrix.lua:558-559](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L558) 裸调 `GQL.Ledger.State` / `LastCompletion`，同函数 :585 却是 `GQL.Ledger and GQL.Ledger.LastCompletion and ...` 全守卫。缺模块时这里抛错被外层 pcall 吞 = 整条 tooltip 静默消失，玩家只看到"没悬停反应"。
- 修法：统一成同函数另一处的守卫写法（或统一都不写，二选一，别在同一条函数里两套）。

### T13 `IsTrackedMap` 把"判不了"折成"确定不是"（会吞整组行）

- 证据：[Api.lua:401-410](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L401) `WalkToOwner` 特意返回的第二值 `progress`（区分"问过没主"与"问不出"，:368-383）被弃用，`verdict = owner ~= nil` 直接缓存 false；渲染层 [Matrix.lua:1584](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1584) 按 `== false` 滤整组。被替换掉的 `IsMidnightMap` 头注释口径是"判不了按不滤、宁多勿漏"。
- 修法：三值化（true/false/nil），缓存只在有确定答案时写；`nil` 按不滤处理。
- 验收：用例「`C_Map` 报错时不滤任何组」。

### T14 默认包按名子串匹配可能多收图

- 证据：[Defaults.lua:134](file:///D:/Repos/wow/GoldQuestLedger/Defaults.lua#L134) → [Config.lua:616 AddMapByName](file:///D:/Repos/wow/GoldQuestLedger/Config.lua#L616) 大小写不敏感**子串**匹配；另外它对"已在跟踪列表"的图回 false，被 `ApplyPack` 并进"没匹配到的图名"日志（措辞失真，行为幂等）。
- 修法：匹配改成"先精确、无唯一命中再子串"；日志分三类（新加 / 已存在 / 没匹配到）。多收的图不自动退（用户口径：删数据要点名），但要在日志里如实列出。

---

## 四、P3 —— 亚基入侵（用户 2026-11-02 补充事实后重定性）

用户给的现场事实（**这是 T15/T16 的新前提，覆盖我原先的推测**）：

- 亚基这个提示**不及时**：刚做完，pin 图标会先消失；下次上线先有一段"第三种状态"的提示，约十来分钟、有的角色要等下次整点更新，奖励段才变化。
- 做完了**立即退出游戏** ⇒ 别的角色看不到是否完成。
- 结论：亚基行的问题主要不在我们判错，而在**客户端读数本身就是延迟且中间态多样** ⇒ 任何"当场定终身"的写法都不可靠。原审查里的"谎报 done 风险"优先级降到 P3，但方向不变：延迟期间要如实显示"读不到"，不许显示成完成。

### T15 亚基虚拟行状态判据：中间态不定终身

- 证据：[GoldQuestLedger/AqirInvasion.lua:77-91](file:///D:/Repos/wow/GoldQuestLedger/AqirInvasion.lua#L77) 遍历 `GetAllWidgetsBySetID`，第一个能答出表且文本非空的 item-display：含"每周仅可获得一次"→ `open`，否则**立即 `return "done"`**，其余 widget 不再看。文件头已记录"第三种形状：奖励段整个消失"。按用户新事实，这个"消失/异常"状态是**延迟期间的常态**，不是已领的证据。
- 修法：`done` 必须是**正向证据**（读到明确的"已领取/已完成"字样），"没看到那句"一律降级为 unknown（判不出），并把本轮读到的形状交给 `EnsureRegistered`/`SetView` 之前先过这道门；不再第一个 widget 定终身（要么扫完整个 set 找正向证据，要么按最保守的答）。
- 验收：离线用例三岔 —— ①含"每周仅可获得一次"⇒ open；②含明确已领字样 ⇒ done；③奖励段消失/杂文本 ⇒ unknown（**不许写绿勾凭据**）。

### T16 亚基跨号可见性：本号读到就当场落账，逐号状态不升成全账号凭据

- 现状依据：客户端本身延迟 ⇒ 别的角色在十来分钟/整点前读不到变化，这是**游戏事实，插件改不动**。而且亚基那个奖励是**逐号**的（每号各有"每周仅可获得一次"），所以"别的号看不到"不该用全账号凭据去绕——那正是第 38 轮 `kwLogAdded` 被批评过的形状（当前号的事实当全账号凭据）。
- 修法（只做插件侧能做的）：本号读到状态时**当场**按现有编号口径落该号的 `Ledger.SetView`（不等下一次快照、不攒着），使矩阵在客户端答不出来的那段时间里仍显示本号上一次的确切结论；界面措辞如实写"这一档客户端读数延迟，以本号读到的为准"级别的玩家语言（措辞归界面、取证归 probe）。**不做**任何轮询/定时重问（零新增接口调用原则）。
- 需游戏内验证：`/gql probe` 打印该 widgetSet 全部 item-display 文本，把延迟期间的真实形状抄进文件头注释（这条是 T15 用例的依据来源）。

---

## 五、P4 —— 卫生项（一次 commit 扫干净）

| # | 项 | 证据 | 处置 |
|---|---|---|---|
| T17 | 已删语义的注释残留 | [Ledger.lua:1141](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1141) 仍写"89267 保活"（分支已删于 :800-803）；[Dump.lua:256](file:///D:/Repos/wow/GoldQuestLedger/Dump.lua#L256) 同；[Config.lua:30](file:///D:/Repos/wow/GoldQuestLedger/Config.lua#L30) 流水 schema 仍列 `{t,g,sig,cycle,round}`；[Track.lua:4](file:///D:/Repos/wow/GoldQuestLedger/Track.lua#L4) 仍指已下线的"添加任务"按钮；[BuyQueue.lua:29](file:///D:/Repos/wow/AHSnipe/BuyQueue.lua#L29)、:301 仍写"扫描暂停"（新语义=让路不暂停） | 照现状改口，不动行为 |
| T18 | 死码 | `Ledger.MarkGoldByObservation`（[Ledger.lua:1690](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1690)）全仓无读者；`Sync.PoolFresh` 见 T11 | **已删（2026-11-02）**：连同一族里同样无生产读者的 `L.SetGoldLock` 与清单字段 `goldLock`（Config 头注释、字段清单、GoldVerdict 那句点名它的注释一起改口）；`NextRefreshAt` 仍有 Sync.lua:110 的唤醒排程在读，不动 |
| T19 | 任务文档过期 | [docs/tasks/2026-10-29-two-admission-doors.md:3](file:///D:/Repos/wow/docs/tasks/2026-10-29-two-admission-doors.md#L3) 写"待实施"，实际已落地（[Config.lua:284](file:///D:/Repos/wow/GoldQuestLedger/Config.lua#L284) `SCHEMA_VERSION=11`、[Ledger.lua:2536](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L2536) `ClearAdmittedKeysOnce`、`TurnInAdmits` 已删）；文档内行号全失效（`RowAdmitted` 2491→2594、`GoldVerdict` 1748→1826）；T6"全绿"未达成 | 状态改"已实施 + 落地修正"，行号重对 |
| T20 | `run_tests.py` 缺漏注册守卫 | GQL 侧有 [run_gql_tests.py:447-471](file:///D:/Repos/wow/tests/run_gql_tests.py#L447)，AHSnipe 侧没有 | 照搬同款静态守卫 |
| T21 | 测试残留文件 | `tests/_dbg.txt`、`_fail.txt`、`_names.txt`、`_fix_tests.py`（**会就地改写 run_gql_tests.py**）、`run_gql_tests.py.bak`（957KB，含已删的 `BeginCapture`/`FinishWindow`，是某中间态唯一副本）、`tmp_*.py` ×8（`tmp_kwonly_audit.py` 被已跟踪代码 [Ledger.lua:718](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L718) 引用；`tmp_94446_audit.py` **硬编码真档路径+账号 BLUEFANE**） | 见决策点 D4 |

## 六、需游戏内实测才能裁决（不在本轮离线修）

1. [Api.lua:1294](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L1294) `info.tooltipWidgetSet` 字段名（12.x 公开写法常见 `tooltipWidgetSetID`）——错则虚拟行永远 unknown（fail-closed 不假勾，但功能静默失效）。
2. [Matrix.lua:850-853](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L850) 抬 DIALOG 层后与 SettingsPanel/GameMenuFrame 的层序断言。
3. [AHSnipe.toc:6](file:///D:/Repos/wow/AHSnipe/AHSnipe.toc#L6) `IconTexture: Interface\MoneyFrame\UI-GoldIcon` 是否真出图标。
4. T16 的亚基 widgetSet 三态文本。
5. [BuyQueue.lua:313-315](file:///D:/Repos/wow/AHSnipe/BuyQueue.lua#L313) 记录的 10-15 实证（暂停扫描→购买消息躺节流队列→10s 超时）与 :712 一带 `BUY_RETRY` 注释的 10-21 反例相互矛盾，且 [Scanner.lua:401-418 BuyPending](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L401) 走的是"购买期零查询流量"——**要现场统计报价超时率**才能定哪个口径成立。

## 七、执行顺序与检查点

```
T1 → T2 → T3 → T8 → T9 → T4 → T5 → T6 → T7 → T10 → T11 → T12 → T13 → T14 → T15 → T16 → T17~T21
```

- T8 提前到 P0 尾巴：它决定 T2/T7 的修复能不能被测试真的看见。
- 每个 commit 前 `git status` + `git diff` 复核（本工作树多会话共用，先看是不是别人的改动，别顺手提交）。
- 每步跑对应 runner，红了先停下判断是"用例过时"还是"真回归"，禁止改断言迁就实现。

## 九、实施中新长出来的任务（2026-11-02 逐轮实施时确认，不在原审查清单里）

已完成（本轮落地并验证）：

- **T22（原 T2 的更深一层）**：`blindFrom` 原先只在"答复到账"时盖戳 ⇒ **正在进行的静默（闸门不开 / 整轮超时）永远不被盖戳**，静默前那条行无限期算证据，一条 60 秒前的旧行照样点亮【立即购买】。口径定为"**断流 = 问了没答**"：`FinishItem` 的超时分支也盖起点（取当下），手动点击引起的购买前刷新超时不算断流，扫描器压根没在问仍由 `Pause()` 负责。已落 [AHSnipe/Scanner.lua:299-312](file:///D:/Repos/wow/AHSnipe/Scanner.lua#L299)，用例 `test_silence_stamped_by_timeout_not_only_by_answers`。
- **T8/T9/T20**：AHSnipe 替身不再凭空造 API（未知大写键当场 error + `RegisterEvent` 按 16 项已知名单校验）；两份计时夹具合一（顺带揪出 MOCK_ENV 里 `GetTime = 100` 把时钟冻死、导致扫描超时分支从来没跑过）；补"定义了但没注册"静态守卫。`python tests/run_tests.py` = 77/77。
- **T12/T13/T14**：`IsTrackedMap` 三值化（判不出的组一行不吞）；tooltip 统一到"加守卫"那一岔（依据：Matrix.lua:460 外层是**静默** pcall，无 handler 不写日志，冒出来等于整段消失）；`AddMapByName` 全等优先、歧义不收、日志三分（新增/已在跟踪/没收进来看原因）。`python tests/run_gql_tests.py` = 399/399。

待做 / 已做与暂缓（按序，逐条标当前状态）：

- **T6b（2026-11-02 主体已落地，剩第三处按证据暂缓）**：`pin:` 行拿到板面倒计时的路已通 —— [Ledger.SetExpiry](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1156) 那句早退按前缀收窄成 `(rec.regKey and not L.IsPinRegKey(rec.regKey)) or rec.kwLogAdded or resident`，`pin:` 不再豁免、`kw:` 与内置周常照旧豁免（计划里那条"先查清加固符文石一族"的担心不成立：那一族走 `resident`/内置 `regKey` 两支，都不在 `pin:` 前缀里）。
  连带（同属"和普通金币任务一样"这句的其余落点）—— **①②已落**：新增一句 `L.IsResidentRegKey`（[Ledger.lua:43](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L43)，`L.IsPinRegKey` 的反面，只此一处，调用点不许再各拼一遍前缀判断）；[L.RowAdmitted](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L2723) ①那支与 [L.QuestHasCredential](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L2087) 都改问这一句。**这一步暴露出一个前提缺失**：② 那道金币门读的是按实例的 `L.QuestMoney`，而金币门从 pin 收行那一刻问到的金额只落在记录级的 `rec.lastMoney` ⇒ 收窄 ① 之后 pin 行"钱够门槛却进不了列"（红测试 `test_a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls` 当场抓到）。修法=补写点不补判据：[L.EnsureRegistered](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1490) 里 `if money and L.IsPinRegKey(e.key) then L.NoteGoldThisInstance(...)`，与普通池行同一原语、同一编号口径，零新增接口调用。取证面（[Dump.lua:325](file:///D:/Repos/wow/GoldQuestLedger/Dump.lua#L325)、[Probe.lua:659](file:///D:/Repos/wow/GoldQuestLedger/Probe.lua#L659)）此前已按 `IsPinRegKey` 分岔，"注册周常（常驻）"那句不再覆盖 pin 行。
  **③暂缓（要游戏内证据，不猜）**：[Sync.lua:962](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L962) "不在池里 ⇒ 把该号这一格刷成 `ended`"仍按**任何** `regKey` 豁免。收窄它的前提是"事件在场时这条任务**会**出现在世界任务池里"—— 这句正是 T27 清单里"暗影汇聚之地到底走不走池路"那一条，没实测过。若它其实不上板、只靠 pin 报任务号，这句一收窄就每次快照把**活着的事件行**刷成"已结束"，比现在"散场后多待一阵"重得多（现在这条行退出靠 ② 档到期 + Prune，D1 那一路已经生效，界面上不会赖到下周）。⇒ 取证回来再定这一处。用例：`a_pin_key_does_not_open_the_player_named_door`（①不认 pin、kw:/内置照旧、钱够门槛走②、凭据同向）与 `the_pin_rows_gold_is_stamped_on_its_own_instance`（收行即烙本条编号；`currentSig` 被池收尾抹掉后钱仍读得出、行从②进列；锚点前进一整周 ⇒ 那份粘滞自己作废 ⇒ 准入门关）。
- **T29（用户 2026-11-02 现场报的第二条 bug：亚基"一会儿属于周常、一会儿属于祖阿曼"）**：根因是**分组归属每轮重问、没有实例身份** —— [Sync.RefreshEventPins](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L695) 那段每轮快照都按"这轮报上来的 pin"重算 `rec.evMapID/evName`：地图上有 pin 就归祖阿曼，pin 消失（用户 11-02 说过的那段延迟，以及亚基这条本来就不上板）就整份抹掉 ⇒ 分组头退回"周常"，下周再反过来。虚拟行是我们自己造的，它属于哪张图**第一次问到就定了**。修法（用户口径照字面落）：归属烙上这一条实例自己的编号 —— 新增清单字段 `evMapKey`，同一条实例内不重算也不抹（`L.SameInstanceKey(rec.evMapKey, curKey)`），编号换了（下一条实例/换 CD）才作废、重新由当轮 pin 定一次；普通 pin 行同一套，不给亚基开特例。零新增接口调用（`curKey` 是同一段循环里已经算好的那份，顺手透给凭据判据免得重算）。用例 `event_pin_group_latches_for_the_instance`：①有 pin 时归祖阿曼；②pin 消失的那轮归属不变；③换 CD 且仍无 pin ⇒ 归属作废、退回周常组；④同一条实例里换了别的 pin ⇒ 不改（一轮定一次）；⑤`kw:` 点名行不受这一刀影响。`event_pin_ambiguous_or_gone_never_invents_a_group` 的②段配合改了前置（补 `cycleAnchor`，让"这轮没报"落在有编号的处境里），旧形状（有图没戳）仍照判清。
- **T26（2026-11-02 已做）**：四处把已退役的 `FinishWindow` 当现役入口的文档串改成 `Sync.RecordTurnIn` 的真实形状（全仓 `FinishWindow` 已归零）；用例 `a_turnin_with_unmeasured_gain_does_not_admit` 连注册表一起改名成 `a_turnin_row_that_stores_no_gold_does_not_admit`；Probe/Dump 里同类退役机制的措辞一并清掉。
- **T18（2026-11-02 已做）**：见上表那一行。**离线两套现均绿：`python tests/run_gql_tests.py` = 411/411、`python tests/run_tests.py` = 77/77。**
- **T23（2026-11-02 实施时否掉，改成取证任务并入 T27）**：[Api.TrackedOwnerMapID](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L404) 确实丢了 `WalkToOwner` 的第二返回值 `settled`、把"这轮没答"永久写成 `false`，但**"照 T13 不写缓存"这个修法两条实测都站不住**（实施 agent 跑离线替身取证，我逐条复核过）：
  ① 代价不合法：`Api.MapParentID`（[Api.lua:1343-1346](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L1343)）在 Resolve 取不到 / pcall 报错 / 回非表三条岔路上**直接 return nil、不写缓存**，所以"不写 `ownerMapCache` 等下一轮重问"= 每轮渲染 × 每一行各落一次客户端接口（`roll()` 是逐行调的：Ledger 分组、Dump 对账、Probe 三处），替身实测 5 次调用问 5 次；现状冻住只落 1 次。这与 [Api.lua:97-101](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L97) 设 `false` 哨兵的初衷、以及 T13 自己钉的"一张图一场最多落接口一次"用例正面冲突，也违反"修法默认零新增接口调用"这条硬口径。
  ② 治不到病：真客户端"图包还没加载"若回的是**一张表但没有 `parentMapID`**，`MapParentCache` 落 `false`（[:1348](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L1348)）⇒ `WalkToOwner` [:392](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L392) 判成 `settled = true` ⇒ 不写缓存那个分支压根不会触发，`IsTrackedMap` 也会一起答 `false`（那是比分组头更重的后果：整组从矩阵消失）。**到底是哪种形状没实测过**，不猜。
  ⇒ 归到 T27 取证：图包未加载 / 切换地图时 `C_Map.GetMapInfo` 对一张子图到底答什么（不答=抛错/nil，还是回表但缺 `parentMapID`）。取证回来再定修法；拿不到取证就保持现状（分组头顶多显示子图名，是可看到的错位，不是数据损失）。
- **T24（2026-11-02 结案为"不是缺陷"）**：按计划里那句前置判据查了 —— **`Mx.Render` 这条路径上没有外层 pcall**（调用点全是直接调：Matrix.lua 内 6 处 + Track.lua:142/:166，没有 handler 吞它），所以缺模块时冒错会带栈打进聊天框 = 可诊断，与 T12 那处 tooltip 的**静默** pcall 不是同一种处境；而 `.toc` 的加载顺序（Config→Ledger→Api→Sync→Matrix）保证渲染时这些模块必然在位。加守卫等于把可诊断的报错换成静默少画，方向反了 ⇒ **不改**。真要统一口径，该问的是 [:1586](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1586) 那个 `pcall(Sync.CurrentCharKey)` 的必要性，不是给 :1578/:1583 加门。
- **T25**：[BuyQueue.lua:186-190](file:///D:/Repos/wow/AHSnipe/BuyQueue.lua#L186) 在计时器回调栈里外发 `CancelCommoditiesPurchase`，而它自己下面一行就写着"从这里外发任何购买消息都必被丢弃" ⇒ 这条清理消息很可能压根没生效，悬挂的购买会话可能一直留着。**先取证再改**：这是"取消"不是"下单"，但要按项目实证口径确认它在计时器栈里到底能不能送达（游戏内看是否残留会话），不要凭猜改成轮询重发。**2026-11-02 归入取证清单（同 T27），离线裁决不了**：项目留档的"计时器/事件回调栈里外发购买消息必被丢弃"这条实证，说的是 `SendCommoditiesPurchase` 那一类**下单**消息；`CancelCommoditiesPurchase` 是不是同一处境**没实测过**。而且"悬挂的 commodity 会话"到底会造成什么玩家看得见的后果（挡住下一次下单？只是界面停留在选包状态？）也没证据 —— 在没有这两条证据之前，把 :187 挪出计时器栈属于凭猜改，且挪出去之后要在别处重发=新增外发消息，风险更大。
- **T26**：`tests/run_gql_tests.py` 里 962 / 2026 / 2393 / 11874 四处文档串仍把已退役的 `FinishWindow` 写成现役入口，2037 那条绿用例的**名字** `a_turnin_with_unmeasured_gain_does_not_admit` 里的 "unmeasured_gain" 指的还是退役的交割到手数。改口 + 连注册表一起改名。
- **T28（用户 2026-11-02 现场报的 bug，优先级最高，插在 T15 之后）**：亚基"做完的号重新登录又变问号"——根因是**判不出会把已存的结论抹掉**，四处写点共用同一个真源。
  证据链（逐条已核对）：
  1. [Ledger.lua:1301 SetView](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1301) 无条件覆盖：`views[questID] = { s = state, t = t, k = curKey }`，旧那份（`done`）当场没了，而且这是写进 SavedVariables 的永久覆盖。
  2. [Ledger.lua:1466](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L1466) `if e.state then L.SetView(...) end` —— `"unknown"` 是**非空字符串**，这道门拦不住它。
  3. [Sync.lua:445](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L445)（每轮快照重问注册行，矩阵开着 10 秒一轮）、[:682](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L682)、[:1108](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1108) 三处都是 `entries[i].state = Api.QuestState(...)`，而 [Api.lua:826-831 QuestState](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L826) 对虚拟行答不出时**明写返回 `"unknown"`**（探测器抛错也返回 `"unknown"`）。
  4. [AqirInvasion.lua:50](file:///D:/Repos/wow/GoldQuestLedger/AqirInvasion.lua#L50) 的 `lastWidgetSet, lastState` 是**模块内存变量** —— 重新登录 / `/reload` 归零；[:169-170](file:///D:/Repos/wow/GoldQuestLedger/AqirInvasion.lua#L169) 在没记住任何东西时答 `"unknown"`。
  ⇒ 拼起来正是用户那句现象：本号做完时客户端答出徽章文案 → 写下 `done`；重新登录撞上用户报的那段延迟（pin 先消失、中间态十来分钟甚至等整点）→ 探测器答 `unknown` → 第一轮快照就把这个号的 `done` **覆盖**掉，界面显示"?"，并且不会自己回来，除非客户端缓过来重新读到徽章。
  修法（一处收口，覆盖四个写点）：`L.SetView` 遇到 `state == "unknown"` 时，若该号该条**已有同一条实例（编号相同）的确定答案**，就**保留旧的那份、不覆盖**（"接口这轮没答 ≠ 状态变了"，这句本来就写在 [Api.lua:817-818](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L817) 的注释里，只是没落到判据上）；编号变了（换实例/换 CD）照旧覆盖，`unknown` 作为新实例的初始值无害。玩家主动清除（`open`）、手动补录、交割写 `done` 三条路一行不动。
  连带排查（同类都要盘，不许只修亚基那一格）：`Api.QuestState` 的普通行那一路在接口不可用时同样答 `unknown`（[Api.lua:833-845](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L833)），所以普通池行/注册周常也在这条覆盖路径上 —— 收口在 `SetView` 一处即同时治好；`Sync.lua:908` 池路的 `state1` 与 `:948` 的 `ended` 要逐条确认它们会不会拿到 `unknown`。
  测试：①虚拟行先答 `done`、下一轮答不出 ⇒ 视图仍是 `done`，界面不许变问号；②同一条实例已 `open`、这轮答不出 ⇒ 仍是 `open`；③编号变了（换 CD/换板面实例）⇒ `unknown` 允许写成新实例的初始值；④普通行（非虚拟）同样不许被 `unknown` 抹掉已存结论；⑤玩家点格子清除覆盖（`open`）、交割写 `done` 两条既有行为一字不变。
  另需如实告知用户：T15 把中间态从"算 done"改成"算读不到"，**读不到的次数变多了**，所以这个覆盖 bug 从"偶尔"变成"必然撞"——修的是覆盖本身，不是回退 T15。
- **T27（需游戏内实测，不能离线裁决）**：AHSnipe 新白名单里四处未实证形状 —— ①没 `SetText` 过的 FontString `GetText` 回 nil 还是 `""`；②没点过的 CheckButton `GetChecked` 回 `false` 还是 `nil`；③`BasicFrameTemplateWithInset` 是否仍提供 `TitleText`；④`C_TradeSkillUI.GetItemReagentQualityByItemInfo` 的入参/返回形状（T10 卡在这）。另加 [AHSnipe.toc:6](file:///D:/Repos/wow/AHSnipe/AHSnipe.toc#L6) 图标目录、[Matrix.lua:850-853](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L850) 层序、[Api.lua:1294](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L1294) `tooltipWidgetSet` 字段名、暗影汇聚之地到底走不走池路、报价超时率统计（[BuyQueue.lua:313-315](file:///D:/Repos/wow/AHSnipe/BuyQueue.lua#L313) 的 10-15 实证与 `BUY_RETRY` 注释的 10-21 反例相互矛盾）。**再加一条（T23 否掉修法后留下的唯一出路，2026-11-02）**：`C_Map.GetMapInfo` 在"图包还没加载/刚切图"那段时间对一张**已知存在的子图**到底答成什么形状 —— ①抛错或整个不答（`Api.MapParentID` 走 [:1346](file:///D:/Repos/wow/GoldQuestLedger/Api.lua#L1346)，不落缓存）；②回一张表但里面没有 `parentMapID`（落 `false` 哨兵，被判成"确定没有父图"）；③回 `0`。三种形状决定 T23 那个分组头错位到底存不存在、以及"下一轮重问"这条自愈路能不能零代价成立。取证不要玩家跑腿：`/gql probe` 的地图段已经逐张摊 `MapParentID` 的答案，登录后立刻跑一次、`/reload` 后再跑一次对差异即可。

## 十、决策记录（2026-11-02，全部已定，实施按这个走）

- **D1（T6）已定并落地**：用户口径 —— 暗影汇聚之地是普通世界金币任务（屠戮者高地/虚影风暴），**和其他普通金币任务一样，到期就要自动消失**。⇒ `pin:` 行不配任何驻板豁免：②档到期门、`Prune` 按时清、`RefreshRegistered` 的续命都不再认 `pin:` 键；换 pin 名只留最新一把。**实施时否掉了我原先的"连续 3 次快照未见才退"计数器** —— ②档那条 `lastSeenAt <= cycleAnchor` 本来就是"这一 CD 没见过就到期"的现成机制，再加状态是多余的（且扛不住 /reload）。残留的"最坏仍驻到下次周重置"归 T6b。
- **D2（T11）暂缓**：不给开板即抓加节流——节流会打回用户 11-01 点名的"点开就是现在"。本轮只清 `PoolFresh` 死码，界面行为一行不动；要加节流需另取口径。
- **D3（T2）已定**：`RewriteRowQty` 在均价否决那一刻刷新的 `seenAt` **算证据、维持现状**（服务器当场给的实测存量比旧数字新，否决后要能接着抢下一档）。
- **D4（T21）已定**：**全删调试残留，tmp 脚本移档** —— 删 `_dbg.txt`/`_fail.txt`/`_names.txt`/`_fix_tests.py`/`tmp_94446_audit.py`（含真实账号与存档路径）；其余 `tmp_*.py` 与 `run_gql_tests.py.bak` 移进 `tests/tools/` 且不加入 git；`tmp_kwonly_audit.py` 的注释引用路径同步改口。
- **D5（T5）已定（用户无偏好，按取证不丢账的方向定）**：算不出实例编号时**照记流水**（`k=nil` ⇒ 读侧永不匹配、绿勾不受影响，但周完成条数不丢），并补同刻去重防 `QUEST_COMPLETE`+`QUEST_TURNED_IN` 双记。完成条数与绿勾在边界时刻不一致是可接受的：前者是"做过几条"，后者是"这一条实例是不是做过"，本来就不是同一问句。
