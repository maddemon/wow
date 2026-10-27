# WorldQuestTracker：手动添加只算"这一条实例"——实施计划

> 旧名横幅：本文件写成时插件还叫 WorldQuestTracker（全局 WQT / 存档 WQTDB / 命令 /wqt / 测试 run_wqt_tests.py）；2026-10-27 起改名 GoldQuestLedger（GQL / GQLDB / /gql / run_gql_tests.py）。逐轮决策记录按当时口径保留，不追改。

> **For agentic workers:** 逐任务实施，每个改动步骤先跑 `python tests/run_wqt_tests.py` 看到 RED，再实现，再跑到全绿。步骤用 `- [ ]` 勾选。

**Goal:** 让玩家点过一次"添加任务"的非金币任务，在它刷出**新的一条实例**之后自动掉出矩阵；老存档里已经躺着的这类标记一次性作废。行为改动全部由 `tests/run_wqt_tests.py` 钉住。

**Architecture:** 分层不动。改动只落在 `Ledger.lua`（换轮作废 + 一次性迁移）、`Config.lua`（迁移档位）、`tests/run_wqt_tests.py`（用例）、`docs/`（口径）。**界面与取证层一行不改** —— 见下面"为什么选清标记而不是加时间戳"。

**Tech Stack:** WoW 12.1.5 内嵌 Lua（5.1 语义）、SavedVariables `WQTDB`、离线测试 Python 3 + lupa（`tests/run_wqt_tests.py`）。

---

## 一、证据（真存档，不是推的）

`D:/games/World of Warcraft/_retail_/WTF/Account/BLUEFANE/SavedVariables/WorldQuestTracker.lua`：

```lua
[88679] = {  -- 意外遭遇，玩家 2026-10-26 报修的那一行
  ["roundAt"]    = 1793000153,          -- 今天这一条实例的起点
  ["roundEnd"]   = 1793041245,
  ["expiresAt"]  = 1793041245, ["expiresSrc"] = "接口倒计时",
  ["currentSig"] = "0",                 -- 客户端本轮明说：这副奖励没有金币
  ["observations"]["0"]  = { money = 0 },
  ["lastMoney"]  = 0,
  -- 没有 goldMoney / goldRound ⇒ 上一轮的金币粘滞已经按轮次作废（上一版修法在生效）
  ["poolSeen"]   = true,
  ["manualAdded"] = true,               -- ← 这一行还留在矩阵里的唯一原因
}
```

- `db.cycleAnchor = 1792623599`（本周四），`db.config.minGold = 500`，`db.stickyGoldReset = 2`，`db.schema = 3`。
- 88679 的交任务流水：各号最后一条 `t` 最大 `1792936528` < `roundAt` ⇒ **本轮没人交过**。
- 关键词表 = `特别任务 / 符文石 / 斯托玛兰 / 追踪萨拉塔斯 / 亚基 / 丰饶 / 虚空决战`，"意外遭遇"一条都不命中。
- 部署目录 `_retail_/Interface/AddOns/WorldQuestTracker/` 与仓库工作树**逐文件 diff 全 same** ⇒ 玩家跑的就是最新代码，这次不是"上一版没修好"，是**另一道门**。

## 二、六道准入门逐条判定（用户口径：新实例里"没人交过门槛金币 + 自己也问不出金币"就不该在列）

| # | 门（`Ledger.MatrixRows`） | 现状 | 违反新规则？ | 本轮 |
|---|---|---|---|---|
| 1 | `money >= floorMoney` | 金额只在本轮之内延续（`goldRound == RoundAt` 才认），换轮作废 | 否 | 不动 |
| 2 | `turnInOf[questID]` | 已按轮次收口：`t >= RoundAt` 且同 CD | 否（换实例自己就掉） | 不动 |
| 3 | `kwHit`（名字含关键词） | 玩家自己配的永久绕门槛 | 否：这是配置本意 | 不动 |
| 4 | `rec.kwLogAdded`（日志收的） | 关键词删掉即撤标记（`Sync.lua` 两处） | 否：跟着配置走 | 不动 |
| 5 | `rec.regKey`（注册周常） | 常驻周常，本就不靠板子换轮 | 否 | 不动 |
| 6 | **`rec.manualAdded`** | **点一次"添加"永久有效，跨轮、跨 CD 都不作废** | **是 —— 唯一违规的一条** | **本轮改它** |

门 2 的"交过但金额不参与判定"是 12.1.5 反馈④（松露大乱斗 962 金只有悬停看得到、三个金额入口全答 0）要的，**不要顺手改成看金额**，那会把那条反馈改回去。

## 三、修法与取舍

**改法**：换轮 = 起点被往前推的那一刻，`manualAdded` 与 `goldRound` 一起作废。落点 `Ledger.RollRound()` —— 它是"这一条实例换了一条"的唯一写点，两个调用方都覆盖到：`DiffPool`（到期时刻已过 + 又挂回池里）与 `SetExpiry`（接口倒计时挪出 15 分钟以上 = 板上换成另一条）。

**为什么选清标记，不给它加时间戳**：读 `manualAdded` 的地方有 8 处 —— 矩阵准入门（`MatrixRows`）、族占行（`OccupiesRow`）、`QuestKnown`、弹框"已加"标签与排序（`Matrix.RefreshAddList`）、`row.manual` / `row.watched`、`Dump` 的"为什么在列"、`Probe` 的来源列。加时间戳要 8 处一起换成"判活"口径，漏一处就是"标记还在、行却不在列"两处各说各话（这个仓库反复踩过的同类缺陷）。清掉标记则所有读点自动一致，取证层零改动。

**代价（照实）**：换轮之后这一行不再自动在列，玩家还想要就再点一次"添加任务"（本轮有效）。`QuestKnown` 少一份身份 ⇒ 只影响"从没上过板的任务"的交任务入账，而那条路还有 `Api.IsWorldQuest(questID) == true` 兜底（`Sync.BeginCapture`），池内任务换轮后本来就有 `poolSeen`。

**老存档不自愈**：`manualAdded` 只在换轮时清，光改代码的话玩家要等下一次 12 小时刷新才看得见效果（上一版"代码改完、矩阵一点变化没有"就是这么来的）。所以配一次性迁移 schema v4。

**v4 迁移对现有存档的影响（逐条查过真存档那 5 条 `manualAdded`）**：

| questID | 名字 | 清掉 `manualAdded` 之后 |
|---|---|---|
| 98172 | 追踪萨拉塔斯 | 仍在列：关键词"追踪萨拉塔斯"命中（门 3） |
| 95429 | 健步如飞 | 仍在列：`goldMoney 1072金` 且 `goldRound == roundAt`（门 1） |
| 89377 | 秘密狩猎 | 不列：`expiresAt 1792969241` 已过（到期过滤，与标记无关） |
| 89347 | 战胜未知：怒火攻心的漂流者 | 不列：`expiresAt 1792983643` 已过（同上） |
| 88679 | 意外遭遇 | **掉出矩阵 —— 正是玩家报修的那一行** |

⇒ 这次迁移在真存档上的可见变化 = 少一行，且恰好是要求消失的那一行。

---

## Global Constraints

- **零新增接口调用**：不许增加 WoW API 调用次数、不许引入轮询（`OnUpdate`、秒级定时器）、不许扫地图树。动手前先自问"这个改动多花了什么"，并把代价写进注释。
- **不许假设 12.x 接口形态**；离线替身**不许凭空造客户端没有的方法或返回形态**。
- **报不出就说报不出**：`nil` / `unknown` / `"?"` 优先于 0；宁可行少一格，不许假数冒充真数。
- **Lua 5.1 语义**：`0` 与 `""` 是真值；`x and A or B` 在 `A` 为 false/nil 时塌到 `B`；一个函数原型 upvalue 上限 60（`test_lua_upvalue_budget` 把守）；跨文件循环用前向 `local` 声明破环。
- **测试门禁**：基线 **287/287**（条数以实跑输出为准，别把数字写进断言）。每个 RED 步骤先跑看到失败，再实现，再跑到全绿；**不许改断言迁就实现**。
- **提交纪律**：这是**共享工作树、共享 git index** —— 并行会话已经把 `AHSnipe/*`、`tests/run_tests.py`、`docs/tasks/2026-10-13-ahsnipe-plan.md` 的部分改动 stage 进 index（`git status --short` 里是 `MM`）。所有提交一律用**带 pathspec 的 commit**：

  ```bash
  git commit -m "$(cat <<'EOF' ... EOF)" -- <这一任务的几个路径>
  ```

  **禁止 `git add -A` / `git add .` / 裸 `git commit`**。提交前 `git status --short` 复核。
- **提交信息**：中文，沿用仓库风格（`fix:` / `test:` / `docs:`，说清改了什么 + 为什么）。
- **测试环境约定**（写用例照这些，别自造替身）：`sync_env([(questID, money)], extra=("Matrix",))` 返回 `(lua, wqt, g)`；`g = lua.globals()`；`g.WQT_NOW` 是会话秒；`lua.execute("...")` 跑 Lua 片段；`fail(name, msg)` / `ok(name)`；新用例必须加进文件末尾 `tests = [ ... ]` 名单。`Ledger.AddQuest(db, questID, now, addedBy)` 可直接调（离线用例里就这么用，见 `run_wqt_tests.py:6444`）。

## 本轮不做（明确留在范围外）

- `AHSnipe/` 全部改动（并行会话拥有）。
- 门 2（`turnInOf`）改成看金额 —— 见第二节表格最后一句。
- 悬停里加一句"这行还在列是因为你手动加过" —— 手动添加现在只在本轮有效，玩家自己刚点过的那次不需要插件提醒。
- `Api.MAP_MEMORY`（玩家口述的名字→地图）搬进存档、`ignored` 与 `manualAdded` 的存储形态合并。

---

### Task 1: 换轮作废 `manualAdded`

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（`RollRound`、`AddQuest` 文件头注释、`MatrixRows` 准入门注释）
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `L.RoundAt`、`L.DiffPool`、`L.SetExpiry`、`L.MatrixRows`（全部现有）
- Produces: 无新导出。`rec.manualAdded` 的语义收窄成"本轮点过"。

- [ ] **Step 1: 写 RED 用例**

新增 `test_manual_add_expires_when_the_instance_rolls`（放在 `tests = [...]` 名单前并登记）。形状照真存档 88679 造，**不许只造理想化字段**：

1. `sync_env` 建一条金币世界任务（`money = 10 * 10000 * 100` 级别，倒计时读得出），快照一次让它上板；
2. `WQT.Ledger.AddQuest(db, qid, now, nil)` ⇒ 断言 `MatrixRows` 里有这一行；
3. 换轮：把这条任务的到期时刻挪过去（下一次快照 `money = 0`、倒计时重新问出一个**新的**结束时间，与上一轮差 > 15 分钟），再跑一趟快照；
4. 断言：`rec.manualAdded == nil`、`L.QuestMoney` 不为正、`MatrixRows` **不再包含这一行**；
5. 断言对照组没被误伤：同一次快照里另造一条本轮读到金币（≥ 门槛）的任务，换轮后仍在列。

再新增 `test_manual_add_survives_within_the_same_instance`：同一条实例内（结束时间不变、只是奖励签名从金币档切到非金币档）跑第二次快照，断言 `manualAdded` 还在、行还在 —— 这一条钉的是"别把换轮判据放宽成每次快照都清"，与上一版"双模式切档不许掉行"成对。

Run: `python tests/run_wqt_tests.py`
Expected: 两条新用例红（换轮那条报"manualAdded 还在"），其余 287 条全绿。

- [ ] **Step 2: 实现**

`Ledger.lua` `RollRound`：

```lua
--- 换轮 = 起点推到当下，两份"本轮之内才算数"的凭据一起作废：
--- · 金币粘滞（goldRound）：新实例重新判给不给金币；
--- · 玩家那次"添加任务"（manualAdded）：用户 2026-10-26 的口径 —— 新的一条实例里，
---   没人交过门槛金币、自己也问不出金币奖励，就不该还占着一行。真存档 88679「意外遭遇」
---   就是这么赖着的：本轮 `currentSig = "0"`（客户端明说不给金币）、本轮没人交过，
---   行还在列只因为玩家上个 CD 点过一次添加。
--- 清标记而不是加时间戳：读 manualAdded 的 8 处（矩阵门/族占行/QuestKnown/弹框"已加"/
--- row.manual/row.watched/Dump/Probe）要一起改判活口径，漏一处就是"标记还在、行不在列"。
local function RollRound(rec, now)
    rec.roundAt = now
    rec.goldRound = nil
    rec.manualAdded = nil
end
```

同步改两处注释（文字与现状必须一致）：
- `AddQuest` 文件头那句"标 manualAdded 强制进矩阵（绕过金币门槛）"后面补清"只到这一条实例结束"；
- `MatrixRows` 里"显示条件：手动加的 / …"那段，注明手动那条是**本轮**的。

- [ ] **Step 3: 跑到全绿 + 变异验证**

Run: `python tests/run_wqt_tests.py` → 全绿。
变异：把 `rec.manualAdded = nil` 摘掉 → 只有换轮用例红；把 `RoundEnded` 的 `at < exp` 守卫改成恒真（每轮都滚）→ 同一条实例那用例必须红。两条变异都验，结果写进提交信息外的汇报里。

- [ ] **Step 4: 提交**

```bash
git status --short
git commit -m "$(cat <<'EOF'
fix: 手动添加只算这一条实例，换轮作废（意外遭遇赖在矩阵里）
EOF
)" -- WorldQuestTracker/Ledger.lua tests/run_wqt_tests.py
```

---

### Task 2: schema v4 一次性作废老存档里的 `manualAdded`

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（新增 `L.ClearManualAddsOnce`）
- Modify: `WorldQuestTracker/Config.lua`（`SCHEMA_VERSION = 4`、`MIGRATIONS[4]`）
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `Config.Migrate` 的逐档机制（已有）、`QUEST_RECORD_FIELDS`（已有，`manualAdded` 在册）
- Produces: `L.ClearManualAddsOnce(db) -> 清掉条数`；`Config.SCHEMA_VERSION == 4`

- [ ] **Step 1: 写 RED 用例**

`test_migrate_clears_manual_added_from_old_saves`：
- 造 `db.schema = 3`，三条记录：A 只有 `manualAdded`（金额 0）、B `manualAdded` + 本轮金币粘滞、C `manualAdded` + 名字命中关键词（关键词用 `db.config.keywords` 现造一条，别依赖默认值）；
- `WQT.Config.Migrate(WQT.Config.DB())` 返回 true、`db.schema == 4`；
- 断言 A/B/C 的 `manualAdded` 全被清（迁移只管标记，不替门槛判活），但 **B、C 换轮前仍在 `MatrixRows` 里**（B 靠金额、C 靠关键词），A 不在 —— 这一条断言才是玩家真正会看见的差别。

Run: 期望红（`Migrate` 没跑 v4 / schema 停在 3）。

- [ ] **Step 2: 实现**

`Ledger.lua`（放在 `ResetStickyGoldOnce` 旁边，共用"一次性迁移"这一节）：

```lua
--- 一次性作废老存档里的"玩家手动添加"标记（schema v4）。
--- 上一版这个标记点一次就永久有效：换轮、换 CD 都不作废，于是玩家上个 CD 为了看一眼
--- 状态点过添加的非金币任务，之后每次刷新都赖在矩阵里（12.1.5 真存档 88679「意外遭遇」：
--- 本轮 currentSig = "0" 客户端明说不给金币、本轮没人交过，行还在列）。
--- 判据改成"本轮点过才算"（RollRound 清），但已经写进存档的那一份不会自己长回来 ——
--- 不清就得等下一次 12 小时刷新才见效，玩家会当成"改了没生效"（上一版踩过）。
--- 代价：玩家还想要的行要再点一次添加。真存档那 5 条逐条查过：只掉「意外遭遇」一行，
--- 另外 4 条要么靠关键词命中、要么靠本轮金币、要么到期时刻早过了。
function L.ClearManualAddsOnce(db)
    ...遍历 db.quests，清 rec.manualAdded，返回清掉条数...
end
```

`Config.lua`：`SCHEMA_VERSION = 4`，`MIGRATIONS[4]` 按 `MIGRATIONS[2]`/`[3]` 同样的 `WQT.Ledger and ... and fn(db) or 0` 形状接上（**别用"标记非空即跳过"**，档位就是为这个准备的）。

- [ ] **Step 3: 跑到全绿 + 变异**

摘掉 `MIGRATIONS[4]` → 只有迁移用例红；把 `ClearManualAddsOnce` 写成"连 `regKey` 一起清"→ 既有用例里注册周常那批必须红（确认没误伤）。

- [ ] **Step 4: 提交**

```bash
git commit -m "$(cat <<'EOF'
fix: 加 schema v4，一次性作废老存档里永久有效的手动添加标记
EOF
)" -- WorldQuestTracker/Ledger.lua WorldQuestTracker/Config.lua tests/run_wqt_tests.py
```

---

### Task 3: 真存档回放（验证，不留脚本）

**Files:** 无（临时脚本放 `D:/Repos/wow/.tmp-replay.py`，跑完删）

- [ ] 用 lupa 读 `WTF/Account/BLUEFANE/SavedVariables/WorldQuestTracker.lua` 的 `WQTDB`，套上离线替身，**迁移前**跑一遍 `L.MatrixRows(db, now)` 记下行集，再跑 `Config.Migrate(db)` + 一趟 `DiffPool`（池按存档里 `poolSeen` 的任务造，金额取各号 `reads` 里最新那份），**迁移后**再记行集。
- [ ] 断言两件事：① 差集**只有** 88679；② 88679 的 `manualAdded` 为 nil、`GoldVerdict` 不为 true。
- [ ] 结果（掉了哪几行、有没有误伤）写进最终汇报，不要只说"验证通过"。跑完 `rm .tmp-replay.py`。
- [ ] lupa 三个已知坑（每次都会踩）：`LuaTable` 没有 `.get()`（用 `t["k"]`）；**空 LuaTable 在 Python 里也是真值**；`for k in t` 迭代出来的是键。

---

### Task 4: 文档口径跟上

**Files:**
- Modify: `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`（第 264、473 行附近）
- Modify: `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`（§8.1 手动加行那句）

- [ ] 三处文字按新口径改：手动添加**只在本条实例内**占行，换轮重新判；老存档由 schema v4 作废；还想要就再点一次。第 473 行原来写"`manualAdded`/`ignored` 是持久意图，代码不替他改主意" —— 这句现在只对 `ignored` 成立，照实改。
- [ ] 补一条游戏内验收：`/reload` → 点"刷新" → 「意外遭遇」那行消失；`/wqt quest 意外遭遇` 的"在列靠"不再报手动添加；再点一次"添加任务"里那条 → 本轮回来，下一轮自动走。
- [ ] Run: `python tests/run_wqt_tests.py` 全绿后提交。

```bash
git commit -m "$(cat <<'EOF'
docs: 手动添加改成"只算这一条实例"，验收清单与设计文档口径跟上
EOF
)" -- docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md docs/superpowers/specs/2026-10-21-worldquesttracker-design.md
```
