# 矩阵闪现一行非金币任务（上一条实例的金币被新实例认领）

## 现象

用户 2026-11-11 反馈：打开矩阵看到「战胜未知：怒火攻心的漂流者」(quest 89347)，悬停发现奖励不是金币；
按 M 打开地图确认确实不是金币任务；再打开矩阵，这一行消失了。

等于"登录先凭空多一行 → 开图读到 0 后当场没"。

## 取证（用户真档 SavedVariables）

`db.quests[89347]`：

- `currentSig` 落盘时是 `"0"`（开图后读到的非金币），`lastMoney = 0`。
- `observations["10400409"] = { key = "89347:1993740", gold = true, money = 10400409, lastSeen = 昨天 }`
  —— **上一条实例**（ID 1993740）读到过 1040.04 金；今天这条任务换了非金币奖励。
- 今天这条实例的编号是 `"89347:1993820"`。
- `db.manual["魔瘾者丶麦德-奥达曼"][89347] = { state="done", t=10-24, g=9671825 }`（无 `k`）。

现象链：登录后这条还没进过池快照（世界地图没开 = 本图奖励数据没到），`currentSig` 还停在
上一条实例的签名 `"10400409"` 上。

1. `L.QuestMoney` 读 `obs.money = 10400409` **不校验 obs.key**，直接返回上一条实例的金币 ⇒
   行靠金币门进列（行里顶着昨天的 1040 金）。
2. 即使把 1 拒掉，`MatrixRows` 的 `QuestMoney(...) or manualOf[questID]` 还会拿
   `db.manual` 里 10-24 那笔 967 金（`ManualMoneyByQuest` 不分实例）继续顶行。
3. 开图后池快照读到这条实例的 0 金（`currentSig="0"`），`NoteGoldThisInstance` 第 2 步作废了
   旧签名的金额 ⇒ 金币门当场关上 ⇒ 行消失（与 `db.logs` 里"矩阵行消失 钱=0"一致）。

`NoteGoldThisInstance` 第 2 步（写侧）本来就有"编号对不上 ⇒ 作废旧金额"的规则，但**只在池快照
那一趟跑**；读侧 `L.QuestMoney` 没有同一把编号校验，登录到第一次读到这条之间存在空窗。

## 目标

1. `L.QuestMoney`：当前签名的 `obs.money` 只有在 `obs.key` 与本条实例编号一致时才作数；
   编号算不出时按现有"判不出 = 不成立"口径拒绝（`obs.key` 为空的老签名保持原判）。
   金币粘滞 `rec.goldMoney`（双模式不掉行）语义不变。
2. `ManualMoneyByQuest`：手动补录的金币也只认**这一条实例**（`m.k` 与当前编号一致）；
   没编号的老补录 = 永不匹配（与 Config 迁移 v6 的口径一致）。
3. 补回归用例：上一条实例读到金币、换实例后还没读到 → 矩阵不许出现这一行；
   同一实例的双模式粘滞照旧不掉行；本实例的手动补录照旧占行。

## 验收

- `python tests/run_gql_tests.py --syntax` 全绿（含新增用例）。
- 89347 场景：登录后（未开图）矩阵不再出现该行；开图读到 0 后也不出现（前后一致，无闪现）。

## 完成情况（已收尾）

- [x] 目标 1 `L.QuestMoney`：`obs.money` 带编号才作数；编号对不上 ⇒ 不认金额、不认 `goldMoney` 粘滞、
      返回 nil（不是 0）；`obs.key` 为空的老签名保持原判。
- [x] 目标 2 `ManualMoneyByQuest(db, now)`：只认 `m.k` 与当前实例编号一致的补录；`m.k` 为空永不匹配；
      编号按 questID 记忆一次。调用点 `L.MatrixRows` / `Dump.Quest` 均补传 `now`。
- [x] 目标 3 回归用例 `test_a_stale_signature_money_does_not_hold_the_next_instance`：
      昨天读到 1040.04 金（`obs.money` 带旧编号落盘）→ 换实例后未开图，`QuestMoney` 为 nil、矩阵 0 行；
      开图读到 0 后仍 0 行；对偶：本实例编号一致的补录照旧占行。
      反向验证：只撤掉 QuestMoney 的编号门 ⇒ 该用例恰在"旧实例金币被新实例认领"一句失败（553/554），
      说明目标 1 确实被覆盖。

### 顺带修的夹具（不是产品口径变化）

- `test_weekly_rows_only_show_the_cd_tail_never_a_guess`：金额读数变化会改变行序，重画后阶段①标记的
  悬停帧可能已被换下来；改为每趟重标**当前真画出来的**那格。
- `test_matrix_rows_money_and_order_unchanged_by_cache`：手拼的 manual 存档没有 `k`，改走真实写点
  `L.SetManual`（并补上身份锚点），与新口径一致。

### 已知边界（本次不改，记录备查）

- `L.GoldVerdict` 在 `QuestMoney` 返回 nil 后仍会认当前签名的 `obs.gold == true`。登录未开图的空窗里，
  `/gql dump` 的金币清单与 Probe 仍可能把这种行报成金币任务（矩阵本身已不再显示）。本任务目标只定在
  `obs.money` / 补录金额，未扩大改动面；如需一致再单独处理。
