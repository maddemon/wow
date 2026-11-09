# GoldQuestLedger 审查修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 2026-11-06 复核后确认的范围，修掉 GoldQuestLedger 的 4 条核心缺陷（登录门闩、空池降级、实例身份、滚动重绘）与 6 条数据/校验缺口，全部先补回归用例再动判据，收尾 459+ 条离线单测全绿。

**Architecture:** 只改现有 8 个 Lua 文件与 1 个 Python 测试 runner，不新增模块、不新增文件（除本计划）。数据正确性类改动全部落在 `Ledger.lua` / `Api.lua` / `Sync.lua` 的纯函数与降级分支；UI 性能改动只把滚动路径接到已有的 `Mx.Refresh` 合并窗口，不重构渲染。测试用 `lupa` 直接执行真实 Lua，WoW API 由 `tests/run_gql_tests.py` 的替身注入。

**Tech Stack:** WoW 12.1.5 Lua 5.1（60 upvalue 上限）、Python 3 + lupa（LuaJIT）、`tests/run_gql_tests.py` 自研 runner、PowerShell 工作目录 `d:/Repos/wow`。

## 决策记录（2026-11-06，用户定稿）

- **D1 = C**：本轮不动 S4 / Task 7。不写用例、不注册、不改 `Matrix.lua`；滚动卡顿与矩阵 O(Q²·P) 一起放到下一轮重构。
- **D2 = B**：本轮发 `0.5.3`。同步改 `GoldQuestLedger/Core.lua:13`、`GoldQuestLedger/GoldQuestLedger.toc:5`、`CHANGELOG.md`；CHANGELOG 先按 git 提交补 0.5.1/0.5.2 历史条目，再加 `## 0.5.3` 写本轮**实际完成**的改动（玩家话术）。
- **D3 = B**：不做孤儿过滤。`test_week_stats_ignores_orphan_turnins_but_keeps_history` 不注册、Step 2 的 `if not (db.quests ...)` 分支不写；Task 10 只做"跨域过滤 + `cycleStart=0` 放行"，玩家可见的完成数一个都不变。
- **游戏内取证**：用户未安排，按"先不跑"处理 → `Pew=0`（Task 2 仍实现"两参皆 nil 按旧行为"兜底，但不注册条件用例）、`Gate=0`（**Task 9 整任务 BLOCKED**，事件 pin 判据不收紧）、Q3 不阻塞 Task 6。
- **本轮执行范围**：Task 0（仅离线基线）、1、2、3、4、5、6、8、10（缩范围）、11、12；**跳过 Task 7、Task 9**。
- **计数**：收尾 `473/473`（459 + 4 + 1 + 2 + 2 + 2 + 1 + 2），14 行 `PASS syntax:` 与 14 行 `PASS upvalues:`。

**实施回写（2026-11-06）**：按上述范围已全部落地，`python tests/run_gql_tests.py --syntax` → `473/473 单测通过`，14 行 `PASS syntax:`，`test_lua_upvalue_budget` 绿，IDE 诊断 0。跳过 Task 7、Task 9；未注册 Task 2 的 PEW 条件用例与 Task 10 的孤儿用例。改动落在 10 个已跟踪文件，未自动 `git commit`。
偏差一处：计划写"Task 8 第二条用例修前应绿"，实际因修前 `Api.WQMapList()` 只返回一个值、Python 解包报 `too many values to unpack` 而 RED；实现后两条均绿，用例代码未改。
版本联动已完成：`Core.lua:13` / `.toc:5` / `CHANGELOG.md` 均为 `0.5.3`。仓库里的 `GoldQuestLedger_v0.5.2.zip` 是旧发布包，对外分发前需重新打包。

**取证结论补充（2026-11-06，来自仓库已有的 `docs/probe.txt` / `docs/log.txt`）**：用户指出"不用进具体地图"是对的 —— `/gql probe` 报告本身按 mapID 逐个真问（`probe.txt:436-443` 列出 2395 / 2437 / 2405 三张图的事件 pin），不需要玩家跑过去。

- **Task 9 Gate 已通过**：`probe.txt:399`「丰饶：采矿虚空地窟」全字段 `isCurrentEvent=true(boolean)`、`:329` / `:372`「亚基入侵：永歌森林」同样 `isCurrentEvent=true(boolean)`；`:436-443` 汇总 8 条事件 pin 全部标 `isCurrentEvent=真`，全文无 `false` / nil 反例。收紧成 `== true` 不会让这两条掉组。**Task 9 从 BLOCKED 解封，但用户此前选的是"先不改"，是否实施仍需他点头**。
- **Q3 已有答案**：`probe.txt:10,121,133,...,899` 与 `log.txt` 里 `C_QuestLog.IsQuestFlaggedCompleted` 一律答 `false(boolean)`，没有 nil 档；对应判读表第一行，Task 6 的修复只作换版本 / 接口异常防回归，不改变本机可见行为。
- **PEW 两参真值**：两个 txt 里都没有 `PLAYER_ENTERING_WORLD` 参数记录 → 保持 `Pew=0`，Task 2 的"两参皆 nil 按旧行为"兜底继续生效，不注册条件用例。
- **两个 `/run` 宏静默是命令写得不好，不是用户操作问题**：POI 宏用 `C_Map.GetBestMapForUnit("player")` 只问玩家当前所在图，不在事件图时 `GetEventsForMap` 返回空表，`for` 循环 0 次且宏里没有兜底 `print` → 一行不输出；PEW 宏是"注册后等下一次进世界事件"才打印，原地不动就永远静默。**这两个宏作废，不再要求用户跑**。
- 因此 Task 0 的 Step 2/3/4 全部由现有文件覆盖，不需要任何新的游戏内命令。

**Task 9 补充实施（2026-11-06，用户回「修」）**：Gate 通过后已按 Task 9 落地 —— 新增 `test_poi_without_current_event_flag_is_not_an_event_pin`（先 RED，再 GREEN），`Api.lua:1337` 收紧为 `info.isCurrentEvent == true`；pin 用例族 `-k` 11/11 全绿（现有夹具 `:3665/:3779/:4206` 本就带 `isCurrentEvent: True`，无需补字段）。`CHANGELOG.md` 的 0.5.3 补了对应条目。收尾全量由 `473/473` 变为 `474/474`。

## Global Constraints

- 基线：`python tests/run_gql_tests.py` → `459/459 单测通过`；每个任务收尾回到 `python tests/run_gql_tests.py --syntax` 全量，禁止拿 `-k` 的局部全绿当结论。
- 每个任务严格 RED → 最小实现 → GREEN；新增 `def test_xxx` 必须同时加进 `tests/run_gql_tests.py:20008` 的 `tests = [...]` 清单，否则 `test_every_defined_case_is_registered_in_the_runner`（runner:448）会红。
- **不自动 `git commit`**；每个任务以"检查点"收尾，提交需用户另行明确要求。
- 不新增任何 `C_*` 接口调用、不新增轮询（用户 2026-10-27 口径）。
- `Matrix.lua` 任何改动后必须过 `test_lua_upvalue_budget`（runner:390，Lua 5.1 每闭包 60 upvalue；lupa 的 LuaJIT 不报这个错）。
- 不改 `Api.QuestState` 对接口明确答 `false` 的语义（"确定没完成"是现状，不能折成 unknown 把整矩阵变问号）；只修"答 nil 被折成 false"和"第一个候选就短路"。
- 不改 `L.Prune` 删除金币流水/视图（v7 迁移 `Ledger.lua:2723` 与 `test_prune_bounds_size`:3176 定稿"一条不删"）；孤儿数据的修复只落在 `L.WeekStats` 统计侧。
- 不改 `Dump` / `Config.Log` / `Probe` 里的 `charKey` 明文输出（用户的取证回传工作流依赖它）；运行时脱敏需用户另行拍板，本轮只做仓库文档脱敏评估。
- 不改 `AqirInvasion.lua` 的关键词准入与"问不出答上一结论"口径；H4 仅列低优先观察。
- 不改 `Sync.lua:1223-1233` 的 30 秒空池二次确认、不改 `meta.partial` 语义、不改 `Sync.lua:1214-1219` 的 `onBoard` 每轮重问设计。
- 用户 2026-11-05 定稿：不要"常驻/resident"概念，不要为某个任务写特例，任务一致性处理；任何修复不得引入按 questID 的分支。**计划文本与 `-k` 过滤词里都不许出现 `resident` 作为概念或匹配键**（第二轮复核第 6 条）。
- `Core.lua` 的 PEW 布尔改动必须同步更新替身 `fire_pew`（runner:7077-7084），否则 15+ 条登录/去抖用例连锁失效。**两参皆 nil 时按旧行为处理，不许把"登录抓一次池"静默关掉**。
- `L.WeekStats` 的 `cycleStart = 0`（及 nil）= "不加过滤"是既定口径（`Ledger.lua:1933`、`docs/superpowers/plans/2026-10-21-wqt-weekstats-ux.md:3060`），10+ 处调用依赖它，任何同域判据都要显式放行 0。
- `CHANGELOG.md` 只记**已发布**的事实：0.5.0 = `e48fe16`、0.5.1 = `34001fa`、0.5.2 = `dccc646`（`git log --oneline` 核实）。本轮改动若要落新版本，是发布决策 D2，必须同时改 `Core.lua:13`、`GoldQuestLedger.toc:5`、`CHANGELOG.md` 三处。
- 深度到限（地图树合法 6 层）不等于"部分结果"，不许因此禁用 `wqMapCache` 导致每轮重扫整棵树（"零新增接口调用 / 不扫图"口径）。
- **三个决策点未拍板前，对应任务 BLOCKED，执行者不得临场默认选方案**：D1（Task 7 滚动夹法）、D2（Task 12 是否发 0.5.3）、D3（Task 10 孤儿过滤是否改变玩家可见数字）。**Task 9 另有硬 Gate**：未取得 12.1.5 丰饶/亚基 pin 的 `isCurrentEvent` 字段实况前，不得收紧判据。
- 所有行号以"内容锚点"为准：行号会随本计划改动漂移，替换时先按注释/代码片段定位，再核对行号。

## 文件结构

| 文件 | 本计划负责的改动 |
|---|---|
| `GoldQuestLedger/Core.lua` | PEW 事件签名接 `isInitialLogin`/`isReloadingUI`，三态传给 Sync；D2 选中才动版本号 |
| `GoldQuestLedger/Sync.lua` | `OnWorldEnter` 门闩改为"仅登录态置位"；不动去抖与轮询 |
| `GoldQuestLedger/Api.lua` | `GetPool` 降级优先级；`TryState` 三态；`WQMapList` 容量截断标记（深度到限照常缓存）；`EventPins` 事件标志（Task 9 有 Gate） |
| `GoldQuestLedger/Ledger.lua` | `InstanceEnd` nil 守卫；`StalePreviousCycleEnd` 的粘性 `poolSeen` 排除；`WeekStats` 跨域过滤与 `cycleStart=0` 放行，孤儿过滤待 D3 |
| `GoldQuestLedger/Matrix.lua` | **Task 7 待 D1，未选前不改** |
| `GoldQuestLedger/Config.lua` | `Init` 字段类型修复；`EscapeUI`；`AddKeyword` 长度上限 |
| `GoldQuestLedger/Options.lua` | `:513` / `:538` 关键词进 `SetText` 前转义（不是 `:590/:593`，那里是拖动） |
| `GoldQuestLedger/Dump.lua` | `print` 的玩家输入转义 |
| `tests/run_gql_tests.py` | 新增用例、注册清单、`fire_pew` 传布尔；BLOCKED 任务的用例不注册 |
| `CHANGELOG.md` | 按 git 提交补 0.5.1 / 0.5.2 **历史**条目；D2 选中才加新版本条目 |

---

### Task 0: 基线与游戏内取证（无代码改动）

**Files:**
- Read: `d:/Repos/wow/tests/run_gql_tests.py`、`d:/Repos/wow/GoldQuestLedger/Api.lua:808-822`、`d:/Repos/wow/GoldQuestLedger/Probe.lua:2375-2380`

**Interfaces:**
- Consumes: 无
- Produces: 三份取证记录（写进本任务的检查点）：Q3 状态判据返回值（供 Task 6 评估收益）、PEW 的 `arg1`/`arg2` 真值（供 Task 2 校验门闩）、丰饶/亚基 pin 的 `isCurrentEvent` 字段实况（**Task 9 的硬 Gate**）

- [ ] **Step 1: 确认离线基线**

Run:
```powershell
cd d:/Repos/wow
$env:PYTHONIOENCODING='utf-8'
python tests/run_gql_tests.py --syntax
```
Expected: `459/459 单测通过`，另有 14 行 `PASS syntax:*.lua` 与 14 行 `PASS upvalues:*.lua`（静态检查不占单测名额，runner:20491），退出码 0。若不是这个数，停止执行后续任务。

- [ ] **Step 2: Q3 — 完成态接口的返回形状（已由 `docs/probe.txt` 覆盖，不需再跑）**

在 12.1.5 客户端执行 `/gql probe` 与 `/gql log`。在报告的「状态判据」段找到 `C_QuestLog.IsQuestFlaggedCompleted`，记录它对三类任务号的返回：①本周已完成的世界任务；②本周未完成的世界任务；③一个不存在的 questID。

| 观察 | 含义 | 对 Task 6 的影响 |
|---|---|---|
| ②答 `false`、③答 `false` | 客户端把"没做"和"问不出"都答 false | Task 6 仍按计划修（不影响 false 语义），实效体现在换版本/接口异常时防回归 |
| ②或③答 `nil` | 客户端确有"没答"这一档 | Task 6 直接修掉当前把 nil 折成 false 的错误 |
| 该入口不存在 | `DONE_SOURCES` 全靠后备 | Task 6 的 `unknown` 分支生效 |

**Q3 不是 Task 6 的硬阻塞**：Task 6 不改动明确 `false` 的语义，取证只用于评估收益。

- [ ] **Step 3: PEW 的 `arg1`/`arg2` 真值（两个 txt 里没有记录，保持 `Pew=0`，宏作废）**

在聊天框粘贴下面这个一次性宏（**不改仓库代码**），然后分别真登录一次、过图一次，记录两次打印：
```
/run local f=CreateFrame("Frame") f:RegisterEvent("PLAYER_ENTERING_WORLD") f:SetScript("OnEvent",function(_,e,a,b) print("GQL_PEW",tostring(a),tostring(b)) f:UnregisterEvent(e) end)
```

| 观察 | 对 Task 2 的影响 |
|---|---|
| 登录 `true false`、过图 `false false` | 三态实现按计划生效 |
| 两次都是 `nil nil`（客户端没给这两个参数） | Task 2 的 Core 必须把"两参皆 nil"当 nil 传下去（旧行为 = 抓池），**不许静默关掉登录抓池**；登录态与过图态无法区分，S1 只能靠其它凭据（如 `isInitialLogin` 不可用时退回原行为） |
| 只给一个参数 | 按 Task 2 的 `arg1 ~= nil or arg2 ~= nil` 分支处理，记录实际给的是哪个 |

- [ ] **Step 4: 丰饶/亚基 pin 的 `isCurrentEvent` 字段实况（Task 9 的硬 Gate —— 已由 `docs/probe.txt` 取证通过，不需再跑）**

在事件发生的地图上执行：
```
/run local m=C_Map.GetBestMapForUnit("player") or 0;local ids=C_AreaPoiInfo.GetEventsForMap(m) or {};for _,id in ipairs(ids) do local i=C_AreaPoiInfo.GetAreaPOIInfo(m,id);print("GQL_POI",m,id,i and i.name,i and tostring(i.isCurrentEvent)) end
```
把丰饶供品与亚基入侵这两条 pin 的打印抄下来。仓库现状：`Api.lua:1295` 是设计注释、`Probe.lua:1371` 只摊字段、`run_gql_tests.py:3665/3779/4206` 的 `isCurrentEvent: True` 是**照现行判据造的夹具**，三处都不能自证真客户端有没有这个字段。

| 观察 | 对 Task 9 的影响 |
|---|---|
| 两条目标 pin 都打印 `true` | Gate 通过，Task 9 按计划收紧到 `== true` |
| 任一条缺字段/为 `false`/为 nil | **Gate 不过，Task 9 整任务 BLOCKED**：收紧会让整批事件行掉组。改列入附录 A 暂缓，"字段缺失时靠什么证据判事件"需用户另定方案 |
| 客户端当前没给这两个 pin | 取不到证据，Gate 不过，Task 9 BLOCKED |

- [ ] **Step 5: 检查点**

把 Q3 三个返回值、PEW 两次打印、pin 两条字段实况抄进检查点，并明确写出 **Task 9 Gate = 通过 / 未通过**。不提交、不改代码。

---

### Task 1: 四条核心回归用例（先补用例，不碰生产代码）

**Files:**
- Modify: `d:/Repos/wow/tests/run_gql_tests.py`（新用例函数定义插在 `if __name__ == "__main__":`（`:20007`）之前、最后一个 `def test_*` 定义之后；注册行追加到 `tests = [...]` 清单末尾，即 `:20467` 的 `test_tooltip_zero_read_prints_no_gold_line,` 之后、`:20468` 的 `]` 之前）

**Interfaces:**
- Consumes: `sync_env`（runner:5719）、`load_lua`（runner:108）、`ledger_db`（runner 现有夹具）、`fire`/`fire_pew`（runner:5712/7077）、`GQL_NOW` + `GQL_FireTimers()`（runner:32-79）
- Produces: 4 条用例 `test_zone_change_does_not_restart_login_snapshot`、`test_shape_mismatch_wins_over_a_constant_empty_source`、`test_instance_end_with_nil_now_does_not_throw`、`test_pool_seen_row_still_gets_previous_cycle_tombstone_exemption`，供 Task 2/3/4/5 直接转绿

- [ ] **Step 1: 写四条失败用例**

把下面整段插到 `tests/run_gql_tests.py` 的 `if __name__ == "__main__":`（`:20007`）之前，即最后一个 `def test_*` 定义之后（**不是** `:20467`，那里是 `tests = [...]` 里的注册行）：

```python
def test_zone_change_does_not_restart_login_snapshot():
    """过图触发的 PLAYER_ENTERING_WORLD 不许重新打开登录抓池门闩。"""
    lua, gql, _ = sync_env([(61, 100000)], wall=1700000000)
    g = lua.globals()
    calls = []
    orig = g.C_WorldQuests.GetWorldQuests

    def counted():
        calls.append(1)
        return orig()

    g.C_WorldQuests.GetWorldQuests = counted
    core = lua.execute("return GQLFrame")
    handler = core.__scripts["OnEvent"]
    handler(core, "PLAYER_LOGIN")
    handler(core, "PLAYER_ENTERING_WORLD", True, False)   # 真登录
    g.GQL_NOW = 12
    g.GQL_FireTimers()
    first = len(calls)
    if first < 1:
        return fail("zone_change_does_not_restart_login_snapshot",
                    "真登录那一趟没抓池")
    handler(core, "PLAYER_ENTERING_WORLD", False, False)  # 过图 / 传送
    g.GQL_NOW = 24
    g.GQL_FireTimers()
    if len(calls) != first:
        return fail("zone_change_does_not_restart_login_snapshot",
                    f"过图又抓了一次池：{first} -> {len(calls)}")
    ok("zone_change_does_not_restart_login_snapshot")


def test_shape_mismatch_wins_over_a_constant_empty_source():
    """某候选恒返回空表、另一候选形状不符时，不许把整池判成"确认 0 条"。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from({})})
    g.C_WorldQuestDB = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from({
            1: lua.table_from({"noQuestID": 1})})})
    rows, err, meta = gql.Api.GetPool()
    if rows is not None:
        return fail("shape_mismatch_wins_over_a_constant_empty_source",
                    f"形状不符应判 unknown（rows=nil），实际拿到 {rows}")
    if "形状" not in str(err):
        return fail("shape_mismatch_wins_over_a_constant_empty_source",
                    f"降级原因应点名形状不符，实际 {err}")
    ok("shape_mismatch_wins_over_a_constant_empty_source")


def test_instance_end_with_nil_now_does_not_throw():
    """两个时钟都取不到时 InstanceEnd 不许比较 nil，判不出就回 nil。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_NIL_DB = { cycleAnchor = 1793833199, quests = {}, turnins = {},
                       manual = {}, chars = {} }
        GQL_NIL_REC = { kwLogAdded = true, roundEnd = 1793229750,
                        lastSeenAt = 1793884533 }
        GQL_NIL_END = GQL.Ledger.InstanceEnd(GQL_NIL_DB, GQL_NIL_REC, nil)
    """)
    if not bool(lua.execute("return GQL_NIL_END == nil")):
        return fail("instance_end_with_nil_now_does_not_throw",
                    "now=nil 时证明不了实例还活着，不收板面值")
    ok("instance_end_with_nil_now_does_not_throw")


def test_pool_seen_row_still_gets_previous_cycle_tombstone_exemption():
    """poolSeen 是粘性历史标记，不能挡住上一条 CD 墓碑的周尾巴豁免。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_TOMB_DB = { cycleAnchor = 1793833199, quests = {}, turnins = {},
                        manual = {}, chars = {} }
        GQL_TOMB_REC = { name = "奈格塔尔虚空决战", roundEnd = 1793283333,
                         lastSeenAt = 1793884533, poolSeen = true,
                         observations = {} }
        GQL_TOMB_DB.quests[96718] = GQL_TOMB_REC
        GQL_TOMB_END = GQL.Ledger.InstanceEnd(GQL_TOMB_DB, GQL_TOMB_REC,
                                              1793888133)
    """)
    if bool(lua.execute("return GQL_TOMB_END == nil")):
        return fail("pool_seen_row_still_gets_previous_cycle_tombstone_exemption",
                    "本 CD 编号算不出：旧实例墓碑没换成周 CD 尾巴")
    if int(lua.execute("return GQL_TOMB_END")) != 1793833199 + 604800:
        return fail("pool_seen_row_still_gets_previous_cycle_tombstone_exemption",
                    "身份应落在 anchor+604800 这一档")
    ok("pool_seen_row_still_gets_previous_cycle_tombstone_exemption")
```

- [ ] **Step 2: 注册四条用例**

在 `tests/run_gql_tests.py` 的 `tests = [...]` 清单末尾（`:20467` 的 `test_tooltip_zero_read_prints_no_gold_line,` 之后）追加：

```python
        test_zone_change_does_not_restart_login_snapshot,
        test_shape_mismatch_wins_over_a_constant_empty_source,
        test_instance_end_with_nil_now_does_not_throw,
        test_pool_seen_row_still_gets_previous_cycle_tombstone_exemption,
```

- [ ] **Step 3: 验证四条都是 RED**

Run:
```powershell
python tests/run_gql_tests.py -k "zone_change_does_not_restart_login_snapshot,shape_mismatch_wins_over_a_constant_empty_source,instance_end_with_nil_now_does_not_throw,pool_seen_row_still_gets_previous_cycle_tombstone_exemption"
```
Expected: 4 条 FAIL，`0/4 单测通过`，退出码 1。其中 `instance_end_with_nil_now_does_not_throw` 以"用例抛错: attempt to compare number with nil"（`Ledger.lua:716` 的 `n > 1e9`，`n` 是 nil）失败；其余三条是断言失败。同时 `test_every_defined_case_is_registered_in_the_runner` 不在 `-k` 结果里，Step 5 全量时会验到。

已用真实 Lua 复核过修前行为（`python -c` 直接加载 `Config/Ledger/Api`）：
`H2_before=None`（墓碑身份算不出）、`H1_before=(False, '[string "<python>"]:716: attempt to compare number with nil')`、`S2_before` 返回非空 rows 表而非 `rows=nil`、`S3_before=('open', None)`。四个 RED 都能成立，不需要猜测。

- [ ] **Step 4: 全量确认只有这 4 条新用例红**

Run: `python tests/run_gql_tests.py`
Expected: `459/463 单测通过`，失败清单恰好是上述 4 条；若出现第 5 条，先停下核对本任务代码，不要继续。

- [ ] **Step 5: 检查点**

记录 4 条 RED 的失败信息。不提交。

---

### Task 2: S1 — 每次过图都重置登录抓池门闩

**Files:**
- Modify: `GoldQuestLedger/Core.lua:65,80-85`
- Modify: `GoldQuestLedger/Sync.lua:1666,1682`
- Test: `tests/run_gql_tests.py:7077-7084`（`fire_pew` 替身）

**Interfaces:**
- Consumes: Task 1 的 `test_zone_change_does_not_restart_login_snapshot`
- Produces: `Sync.OnWorldEnter(isLoginLike)`；`nil` = 旧行为（抓，含 Core 两参皆 nil 的形状），`true` = 登录/重载（抓），`false` = 过图（不抓）

- [ ] **Step 1: 改 `Core.lua` 的事件签名与 PEW 分支**

`:65` 改为：
```lua
GQL.frame:SetScript("OnEvent", function(self, event, arg1, arg2)
```

`:80-85` 整段替换为：
```lua
    elseif event == "PLAYER_ENTERING_WORLD" then
        -- 每次进世界都要补的幂等初始化（地图按钮 / pin hook / 默认配置包）照旧。
        -- 只有真登录或 UI 重载才开"登录抓池"门闩：过图、传送、位面切换同样发这个事件，
        -- 但 isInitialLogin=false 且 isReloadingUI=false，不该再跑一趟全量快照。
        -- 两个实参都取不到（旧客户端 / 替身只传 2 个参数）时留在 nil：
        -- Sync 侧把 nil 当旧行为（抓池），绝不静默关掉登录那一趟。
        local loginLike
        if arg1 ~= nil or arg2 ~= nil then
            loginLike = (arg1 == true or arg2 == true)
        end
        if GQL.Sync and GQL.Sync.OnWorldEnter then
            local okCall, err = pcall(GQL.Sync.OnWorldEnter, loginLike)
            if not okCall then GQL.Config.Log("Sync.OnWorldEnter 出错: " .. tostring(err)) end
        end
```

- [ ] **Step 2: 改 `Sync.OnWorldEnter`**

`Sync.lua:1666` 的函数头改为 `function Sync.OnWorldEnter(isLoginLike)`；`:1682` 一行替换为：
```lua
    -- nil = 旧调用方 / 离线用例直调，保持"登录那一趟"的旧行为（459 条用例依赖）；
    -- 只有显式 false（过图 / 传送）才落下门闩。
    loginPending = (isLoginLike ~= false) and (GQL.Config.SyncOnLogin() == true)
```
`ScheduleSnapshot()` 保持原样不动。

- [ ] **Step 3: 同步替身 `fire_pew`**

`tests/run_gql_tests.py:7084` 替换为：
```python
    handler(core, "PLAYER_ENTERING_WORLD", True, False)
```
理由：替身现在模拟"真登录"，否则 `test_login_creates_minimap_button` 等用例走的是"过图"分支，按钮/默认包虽然仍会补，但语义与用例名不符。其余直调 `gql.Sync.OnWorldEnter()` 的 15+ 条用例不传参 → `nil` → 旧行为，不受影响。

若 Task 0 Step 3 的取证是「两参皆 nil」，额外插入并注册下面这条用例（钉住 Core 不能把登录抓池静默关掉；**此时各检查点数字顺延 +1**）：
```python
def test_pew_without_booleans_still_runs_login_snapshot():
    """客户端不给 isInitialLogin/isReloadingUI 时，登录那一趟抓池不许失效。"""
    lua, gql, _ = sync_env([(61, 100000)], wall=1700000000)
    g = lua.globals()
    calls = []
    orig = g.C_WorldQuests.GetWorldQuests

    def counted():
        calls.append(1)
        return orig()

    g.C_WorldQuests.GetWorldQuests = counted
    core = lua.execute("return GQLFrame")
    handler = core.__scripts["OnEvent"]
    handler(core, "PLAYER_LOGIN")
    handler(core, "PLAYER_ENTERING_WORLD")     # 没有第三、第四个实参
    g.GQL_NOW = 12
    g.GQL_FireTimers()
    if len(calls) < 1:
        return fail("pew_without_booleans_still_runs_login_snapshot",
                    "两参皆 nil 时登录抓池被静默关掉了")
    ok("pew_without_booleans_still_runs_login_snapshot")
```
注册行：`test_pew_without_booleans_still_runs_login_snapshot,`（仅该取证结论成立时）。

- [ ] **Step 4: 逐条 GREEN**

Run:
```powershell
python tests/run_gql_tests.py -k "zone_change_does_not_restart_login_snapshot,login_snapshot,debounce_single_snapshot,login_creates_minimap_button,login_retries_button"
```
Expected: 全绿（`-k` 结果里 0 条 FAIL）。

- [ ] **Step 5: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `460/463 单测通过`，剩下 3 条 RED 属于 Task 3/4/5。不提交。

---

### Task 3: S2 — 形状不符被"恒空源"覆盖成确认空池

**Files:**
- Modify: `GoldQuestLedger/Api.lua:749-760`

**Interfaces:**
- Consumes: Task 1 的 `test_shape_mismatch_wins_over_a_constant_empty_source`
- Produces: `Api.GetPool()` 的降级优先级不变式——`shapeFrom`（unknown）优先于 `emptyFrom`（确认 0 条）

- [ ] **Step 1: 替换收尾段**

把 `Api.lua:749-760` 整段替换为：
```lua
            elseif okCall == false and raw ~= nil and src.needsMapID ~= true then
                Degrade(src.path .. " 调用失败: " .. tostring(raw))
            end
        end
    end
    -- 形状不符 = "判不了"，恒空表 = "确认 0 条"。只要有一个源形状不符，
    -- 就不能凭另一个源的空表宣布整池为空（Api.lua:664-665 那条红线）。
    -- 12.1.5 不带 mapID 的源实测都不存在，这条主要防换版本/第三方占用同名全局。
    if shapeFrom then return nil, shapeFrom, nil end
    if emptyFrom then
        Degrade(emptyFrom .. " 返回空表")
        return {}, nil, { partial = false, source = emptyFrom }
    end
```
同时删掉原 `:751-752` 那条 `okCall == false and src.needsMapID == true` 分支——`okCall` 在 `needsMapID` 分支从未赋值（`:673-674`），是死代码。保留 `:749-750`（`okCall` 在 `:735` 对不带 mapID 的源有赋值，会正常执行）。

- [ ] **Step 2: GREEN + 不破坏既有空池语义**

Run:
```powershell
python tests/run_gql_tests.py -k "shape_mismatch_wins_over_a_constant_empty_source,api_empty_pool_is_data_not_failure,api_pool_shape_mismatch_is_not_empty,empty_pool_needs_second_read,api_degrades_when_missing"
```
Expected: 全绿。`test_api_empty_pool_is_data_not_failure` 必须仍然拿到 `rows != None and len(rows)==0`（只有恒空源、没有形状不符时，空池语义保持）。

- [ ] **Step 3: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `461/463 单测通过`。不提交。

---

### Task 4: H1 — `InstanceEnd` 在 `now=nil` 时比较 nil

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:716`

**Interfaces:**
- Consumes: Task 1 的 `test_instance_end_with_nil_now_does_not_throw`
- Produces: `L.InstanceEnd(db, rec, nil)` 返回 nil，不抛错

- [ ] **Step 1: 补守卫**

`Ledger.lua:716` 替换为：
```lua
        local alive = n and (re > 1e9) == (n > 1e9) and re > n
```
与同函数 `:727` 的 `n and ...` 同口径；`n=nil` 时 `alive` 为 nil，`if not alive` 走②档周尾巴，随后 `:726-727` 的过滤再按"证明不了活着就不收板面值"回 nil，符合 `:703` 注释。这是防御性修复（真客户端 `GetTime` 恒在），不引入新的时间来源。

- [ ] **Step 2: GREEN**

Run:
```powershell
python tests/run_gql_tests.py -k "instance_end_with_nil_now_does_not_throw,an_identity_that_cannot_be_determined_is_nil_never_a_guess,resident_keyword_row_with_stale_roundEnd_still_admitted,cross_clock_domain_expiry"
```
Expected: 全绿。

- [ ] **Step 3: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `462/463 单测通过`。不提交。

---

### Task 5: H2 — `poolSeen` 粘性标记挡住上一条 CD 墓碑豁免

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:641-642`（注释）、`:655`（判据）
- Test: `tests/run_gql_tests.py`（新增 1 条对照用例）

**Interfaces:**
- Consumes: Task 1 的 `test_pool_seen_row_still_gets_previous_cycle_tombstone_exemption`
- Produces: `StalePreviousCycleEnd(db, rec, now)` 只靠 `e < anchor && seen > anchor && 同域` 判定，不再读 `rec.poolSeen`；`L.InstanceEnd` 与 `L.InstanceExpired` 仍共用 `InstanceEndRaw`（不再单列 H3）

**论证（必须写进代码注释）**：注释 `:641-642` 担心"12 小时区板面任务，roundEnd 是本 CD 内板面答出的，过期就是这一条实例真结束了，不许退回周尾巴"。但本 CD 内答出的结束时刻必然 `>= anchor`，而豁免的第一条判据就是 `e < anchor`，已经把这种形状挡在外面。`rec.poolSeen` 是 `DiffPool:980` 写下的永久布尔（全仓无清除点），用它二次排除会让所有进过池的记录永远失去豁免。删掉它不放宽①档，也不新增字段、不新增迁移、不写任务特例。

- [ ] **Step 1: 加对照用例（钉住①档没被放宽）**

插入 `tests/run_gql_tests.py`（紧接 Task 1 的四条之后）：
```python
def test_current_cycle_board_end_still_expires_on_its_own_time():
    """本 CD 内板面答出的过期 roundEnd 仍按①档判死，不因移除 poolSeen 排除而复活。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_BOARD_DB = { cycleAnchor = 1793833199, quests = {}, turnins = {},
                         manual = {}, chars = {} }
        GQL_BOARD_REC = { name = "12 小时板面任务", roundEnd = 1793880000,
                          lastSeenAt = 1793884533, poolSeen = true,
                          observations = {} }
        GQL_BOARD_DB.quests[88888] = GQL_BOARD_REC
        GQL_BOARD_EXPIRED = GQL.Ledger.InstanceExpired(GQL_BOARD_DB,
            GQL_BOARD_REC, 1793888133)
        GQL_BOARD_END = GQL.Ledger.InstanceEnd(GQL_BOARD_DB,
            GQL_BOARD_REC, 1793888133)
    """)
    if not bool(lua.execute("return GQL_BOARD_EXPIRED == true")):
        return fail("current_cycle_board_end_still_expires_on_its_own_time",
                    "本 CD 的板面结束时刻已过去，到期门必须判死")
    if not bool(lua.execute("return GQL_BOARD_END == nil")):
        return fail("current_cycle_board_end_still_expires_on_its_own_time",
                    "过期的板面时刻不许冒充活实例的身份")
    ok("current_cycle_board_end_still_expires_on_its_own_time")
```
并在 `tests = [...]` 清单追加 `test_current_cycle_board_end_still_expires_on_its_own_time,`。这条用例修前修后都应绿；若红，说明改动放宽了①档，立即回退。

- [ ] **Step 2: 删粘性排除并改注释**

`Ledger.lua:655` 的 `if rec.poolSeen then return false end` 整行删除。注释按**内容**定位：找到以 `· 从没上过板（not poolSeen）` 开头的整段两行 bullet 并替换成下面四行，不要只删某一行。

```lua
---   · e < 本 CD 锚点：这条结束时刻落在上一次 CD，不是本 CD 这条实例的。
---     「本 CD 内板面答出的结束时刻」必然 >= 锚点，第一条已经把它挡在外面，
---     所以不再用 rec.poolSeen 二次排除 —— 它是 DiffPool 写下的粘性历史标记，
---     全仓没有清它的地方，用它当"本 CD 从没上过板"会把所有进过池的行永久锁死。
```

**行号勘误（第二轮复核第 6 条）**：复核给的是"`poolSeen` 判据在 `:642-644`、`:641` 属 lastSeenAt 说明"，与当前文件不符。实际 `:638-640` 才是 `lastSeenAt > anchor` 那条，`:641-642` 是 `not poolSeen` 这个 bullet 的两行，`:643-644` 是"本 CD 内自己烙的真实到期日"另一条独立说明。按内容定位替换整段 bullet，正是为了避免按偏移行号删掉半截上下文。

- [ ] **Step 3: GREEN（含两门一致性回归）**

Run:
```powershell
python tests/run_gql_tests.py -k "pool_seen_row_still_gets_previous_cycle_tombstone_exemption,current_cycle_board_end_still_expires_on_its_own_time,stale_round_end_with_no_log_source_expires,quest_report_names_the_cd_tail,weekly_event_returns_to_matrix,disappear_gate,gone_pin_row,prune"
```
Expected: 全绿。这组用例同时覆盖 `L.InstanceEnd` 与 `L.InstanceExpired` 共用 `InstanceEndRaw` 的一致性（原报告 H3 并入此处，不另立改动）。

- [ ] **Step 4: 全量回归与失望面处理**

Run: `python tests/run_gql_tests.py`
Expected: `464/464 单测通过`。若出现红的用例且它的 docstring 声称"poolSeen 是永久凭据"，按 `:641-642` 的新论证更新该用例断言——**只允许改断言，不允许把 `e < anchor` 放宽，也不允许为单个 questID 加豁免**。记录被改用例名与理由。

- [ ] **Step 5: 检查点**

记录全量结果与（若有）被更新用例名。不提交。

---

### Task 6: S3 — `TryState` 把"接口没答"折成确定状态

**Files:**
- Modify: `GoldQuestLedger/Api.lua:813-822`
- Test: `tests/run_gql_tests.py`（新增 2 条）

**Interfaces:**
- Consumes: Task 0 的 Q3 取证记录（仅用于评估收益，不阻塞）
- Produces: `TryState(paths, questID)` 返回 `(boolean|nil, boolean)`；`Api.QuestState` 对"所有候选都没给出布尔答案"返回 `"unknown"`

**范围声明**：明确答 `false` 仍折成"确定没完成"（保持现状，避免全矩阵问号）；只修"答 nil 被当成 false"与"第一个存在的候选就短路，不再试后备入口"。`AbundantOfferings.M.Refresh` 的 `state == "unknown"` 门因此得以生效。

- [ ] **Step 1: 写两条失败用例**

插入 `tests/run_gql_tests.py`：
```python
def test_nil_completion_answer_keeps_looking_for_a_later_source():
    """第一个完成态入口答 nil 时，不许短路，要继续问后备入口。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    lua.execute("""
        C_QuestLog = {}
        C_QuestLog.IsQuestCompleted = function() return nil end
        C_QuestLog.IsQuestFlaggedCompleted = function() return true end
    """)
    st, err = gql.Api.QuestState(61)
    if st != "done":
        return fail("nil_completion_answer_keeps_looking_for_a_later_source",
                    f"后备入口答 done，实际 {st}（降级={err}）")
    ok("nil_completion_answer_keeps_looking_for_a_later_source")


def test_all_nil_completion_answers_report_unknown():
    """所有完成态入口都答 nil = 问不出，按 fail-closed 回 unknown，不是 open。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    lua.execute("""
        C_QuestLog = {}
        C_QuestLog.IsQuestCompleted = function() return nil end
    """)
    st, err = gql.Api.QuestState(61)
    if st != "unknown":
        return fail("all_nil_completion_answers_report_unknown",
                    f"没答就是没答，实际 {st}（降级={err}）")
    ok("all_nil_completion_answers_report_unknown")
```
注册进 `tests = [...]`：
```python
        test_nil_completion_answer_keeps_looking_for_a_later_source,
        test_all_nil_completion_answers_report_unknown,
```

- [ ] **Step 2: 验证 RED**

Run: `python tests/run_gql_tests.py -k "nil_completion_answer,all_nil_completion_answers"`
Expected: 2 条 FAIL（都拿到 `"open"`）。

- [ ] **Step 3: 最小实现**

`Api.lua:813-822` 整段替换为：
```lua
local function TryState(paths, questID)
    for i = 1, #paths do
        local fn = Api.Resolve(paths[i])
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            -- 只有明确的布尔答案才算"客户端答了"。nil / 非布尔 / 抛错都是
            -- "这个入口这轮没答"，继续问下一个候选，不许折成 false。
            if okCall and type(v) == "boolean" then
                return v, true
            end
        end
    end
    return nil, false
end
```
`Api.QuestState`（`:835-855`）不用改：两个 `TryState` 都返回 `false` 时，`:844` 的 `if not okDone and not okProgress` 或 `:850` 的 `if not okDone` 会回 `"unknown"`；`L.SetView:1433` 只对 unknown 保留旧 done；`AbundantOfferings.lua:45` 的"问不到不续命"门随之生效。`Sync.lua:1273` 对 `QuestState` 的 pcall 不用改。

- [ ] **Step 4: GREEN + 下游回归**

Run:
```powershell
python tests/run_gql_tests.py -k "nil_completion_answer,all_nil_completion_answers,snapshot_degrades_to_unknown,aqir_state_needs_positive_evidence,an_unanswerable_round,aqir_virtual_row,abundant_offerings_renews,follow_the_client_version,state_uses_own_snapshot"
```
Expected: 全绿。特别确认 `test_snapshot_degrades_to_unknown`（runner:5844）没有因为 `unknown` 变多而红；若红，只核对是否"接口整体缺失"这一形状，不得把 `unknown` 再折回 `open`。

**回归词勘误**：原计划那个拼出来的过滤词匹配不到任何用例名（等于这一族没跑），而且它含 11-05 口径已禁用的概念名。上面换成的都是 runner 里真实存在的用例：`test_aqir_state_needs_positive_evidence_not_the_absent_sentence`（`:4216`）、`test_an_unanswerable_round_never_erases_the_aqir_done_view`（`:4325`）、`test_the_aqir_virtual_row_lives_and_dies_on_the_cycle_tail`（`:5309`）、`test_abundant_offerings_renews_only_while_client_reports_it`（`:5172`）。

- [ ] **Step 5: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `466/466 单测通过`。把 Task 0 的 Q3 结论写进备注。不提交。

---

### Task 7: S4 —【BLOCKED / 决策点 D1】滚动合并前必须先定夹法

**状态**：**未拍板前整任务不执行**——不写用例、不注册、不改 `Matrix.lua`，也不把 `467/467` 写进任何检查点。计数顺延规则见文末自检。

**Files（D1 选定后才动）:**
- Modify: `GoldQuestLedger/Matrix.lua:70`（前向声明）、`:1366` 后（`ApplyScrollOffsets`）、`:1188-1211`（`HookThumb`）、`:1368-1381`（`OnMouseWheel`）
- Test: `tests/run_gql_tests.py`

**Interfaces:**
- Consumes: `Mx.Refresh`（`Matrix.lua:1905`）、`Mx.Render` 的 `renderCount`（`:1568`）、替身 `GQL_NOW`/`GQL_FireTimers`/`GetCursorPosition`
- Produces: 文件级 local `ApplyScrollOffsets()`；滚动路径只改偏移并立即写 ScrollFrame，重绘交给 `Mx.Refresh` 合并

**真因（第二轮复核第 1 条，已逐行核过）**：滚轮路径今天靠 `Mx.Render` 末尾才算内容高并夹偏移——`Matrix.lua:1802` 写 `contentH = math.max(y, 1)`、`:1812` 调 `ClampOffsets()`、`:1813-1816` 才把夹过的 offX/offY 写进两个 ScrollFrame；而替身 `tests/run_gql_tests.py:147-150` 的 `SetVerticalScroll` 是裸赋值、**不夹**。改成"只写偏移、重绘交给合并窗口"后必然撞掉三条用例：
- `tests/run_gql_tests.py:10114`「内容放得下就不该出滚动」拿到 120 → 红；
- `tests/run_gql_tests.py:10076-10079` 用 5 格滚轮回顶后 offY 为负 → 红，连带滑块跟手判断；
- `tests/run_gql_tests.py:7491` 反向滚回原位，同因。

原计划 Step 5 那句"检查 `ApplyScrollOffsets` 是否在 `Mx.Ensure()` 之后"不是真原因，已删除。

**玩家看得见的差别 —— 必须由用户点，执行者不得临场决定**：

| 方案 | 做法 | 代价 | 需要同步改的断言 |
|---|---|---|---|
| A：按上一轮内容尺寸全夹 | `ApplyScrollOffsets` 里先 `ClampOffsets()` 再写 ScrollFrame（用的是上一轮 `contentH`/`visH`） | 用户报过的"内容刚变多、第一格被吞"会回来：新高度的夹取滞后 0.25 秒 | `:10114`、`:10076-10079`、`:7491` 的预期仍为 0 / 夹到边界；补一条"内容变多后补画完成再滚动"用例 |
| B：只夹下界 | `offX = math.max(0, offX)`、`offY = math.max(0, offY)`，不按内容高夹上界 | 0.25 秒合并窗口内能滚出空洞（滚到内容以下），补画后弹回 | 同上三条预期为 0；补一条"窗口内不出现负滚动值"用例；上界空洞不测 |
| C：本轮不动 S4 | 保留每格即时 `Mx.Render()`，把 S4 与 M2（矩阵 O(Q²·P) 全表扫描、`Ledger.lua:3227` 丢 now）一起放到下一轮重构 | 卡顿不马上缓解；零回归风险 | 无 |

**建议（不替用户决定）**：若本轮目标是先把数据正确性修完、发布前不引入可见手感变化，选 C；若必须本轮修卡顿且能接受 0.25 秒短暂空洞，选 B；选 A 前需确认"第一格被吞"那次反馈是否可接受。

**D1 选定后**：下面 Step 3 的 `ApplyScrollOffsets` 骨架必须按所选方案补夹法（A 加 `ClampOffsets()`；B 加两次 `math.max(0, ...)`）；Step 5 的 `-k` 必须显式包含上述三条用例；`Matrix.lua` 改动后跑 `test_lua_upvalue_budget`。

- [ ] **Step 1（仅当 D1 选 A 或 B 才执行）: 写失败用例**

下面的用例与 Step 3-6 的代码是**方案骨架**：D1 选定后必须先按方案补上夹法再执行。选 C 或未拍板时，整段不写入 runner、不注册。

插入 `tests/run_gql_tests.py`：
```python
def test_wheel_and_thumb_share_the_coalesced_refresh():
    """连续滚轮不许每格全量重算 MatrixRows/MatrixColumns，窗口末尾补画一次。"""
    lua, gql, _ = sync_env([(109, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    base = int(lua.execute("return GQL.Matrix.renderCount or 0"))
    lua.execute("for i = 1, 10 do "
                "  local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "  f(GQLMatrixScroll, 1) end")
    burst = int(lua.execute("return GQL.Matrix.renderCount or 0"))
    if burst - base > 2:
        return fail("wheel_and_thumb_share_the_coalesced_refresh",
                    f"10 格滚轮重排了 {burst - base} 次，合并窗口没生效")
    g.GQL_NOW = float(g.GQL_NOW) + 1
    g.GQL_FireTimers()
    if int(lua.execute("return GQL.Matrix.renderCount or 0")) <= burst:
        return fail("wheel_and_thumb_share_the_coalesced_refresh",
                    "窗口结束应补画一次")
    ok("wheel_and_thumb_share_the_coalesced_refresh")
```
注册：`test_wheel_and_thumb_share_the_coalesced_refresh,`。

- [ ] **Step 2: 验证 RED**

Run: `python tests/run_gql_tests.py -k wheel_and_thumb_share_the_coalesced_refresh`
Expected: FAIL，10 格滚轮重排 10 次。

- [ ] **Step 3: 加前向声明与轻量偏移写入**

`Matrix.lua:70` 替换为：
```lua
local ClampOffsets, OnMouseWheel, ApplyScrollOffsets
```

在 `Matrix.lua:1366`（`ClampOffsets` 函数结束的 `end` 之后）插入：
```lua
-- 滚动只改偏移时，先把偏移写进两个 ScrollFrame，让滚动值当帧生效；
-- 全量排版（MatrixRows/MatrixColumns）交给 Mx.Refresh 的合并窗口。
-- 不用 Mx.Render 里的 SetVerticalScroll：那要等下一趟重排才跑。
ApplyScrollOffsets = function()
    if not (scroll and scrollL) then return end
    pcall(scrollL.SetHorizontalScroll, scrollL, 0)
    pcall(scrollL.SetVerticalScroll, scrollL, offY)
    pcall(scroll.SetHorizontalScroll, scroll, offX)
    pcall(scroll.SetVerticalScroll, scroll, offY)
end
```

- [ ] **Step 4: 两条滚动路径接合并窗口**

`Matrix.lua:1205-1206`（`HookThumb` 的 OnUpdate 尾部）替换为：
```lua
            ClampOffsets()
            ApplyScrollOffsets()
            Mx.Refresh()
```

`Matrix.lua:1379-1380`（`OnMouseWheel` 尾部）替换为：
```lua
    HideHiFrame()          -- 滚动了高亮就收起，免得停在错误的位置
    ApplyScrollOffsets()
    Mx.Refresh()
```
`Mx.Refresh` 已有 `frame:IsShown()` 门（`:1906`）、`paintTimer` 单次补画（`:1913-1918`）与 `OnHide` 取消（`:1444-1453`），不动。`Mx.Render` 不新增任何 upvalue。

- [ ] **Step 5: GREEN + 三条滚动用例回归 + upvalue 守卫**

Run:
```powershell
python tests/run_gql_tests.py -k "wheel_and_thumb_share_the_coalesced_refresh,matrix_max_height_follows_screen,matrix_thumb_drag_follows_cursor,matrix_scrolls_instead_of_scaling,wheel_works_over_cell_buttons,panel_columns_scroll_with_wheel,matrix_scroll_keeps_first_cell,repaint_coalesces_within_a_burst,lua_upvalue_budget"
```
Expected: 全绿，且 `upvalues:Matrix.lua` 一行 PASS。**`-k` 必须显式包含 `matrix_max_height_follows_screen`（`:10114`）、`matrix_thumb_drag_follows_cursor`（`:10076-10079`）、`matrix_scrolls_instead_of_scaling`（`:7491`）这三条**，否则等于没回归。若它们仍红，按 D1 选定方案的"需要同步改的断言"栏处理，不得改回每帧 `Mx.Render()`，也不得用"检查 `Mx.Ensure()` 时序"搪塞——真因是替身不夹 + 缺 `ClampOffsets()`。

- [ ] **Step 6: 检查点**

Run: `python tests/run_gql_tests.py --syntax`
Expected: 若 D1 选 A/B 且已执行，按当轮实际用例数（见文末自检表）+ 14 行 `PASS syntax:`；若 D1 选 C 或未拍板，**本任务不执行，全量仍 `466/466`**，绝不写 `467/467`。不提交。

---

### Task 8: M4 — 地图数量上限截断却当完整结果缓存

**Files:**
- Modify: `GoldQuestLedger/Api.lua:245`（缓存声明）、`:272-290`（`walk`/缓存）、`:680-681`（`GetPool` 取清单）、`:723-725`（meta）
- Test: `tests/run_gql_tests.py`（新增 2 条）

**Interfaces:**
- Consumes: `Api.Degrade`（`Api.lua:13`）、`Api.LastDegrade`（`:11`）
- Produces: `Api.WQMapList()` 返回 `(list, truncated)`；`GetPool` 的 `meta.truncated`（仅按图回退路径）

**两档必须分开（第二轮复核第 2 条）**：`Api.lua:273` 现在写的是 `depth > 5 or #list >= 800`，但两者语义不同——地图树真有 6 层是**合法形状**，按深度到限就停止遍历后照常缓存；只有 `#list >= 800`（容量上限）才说明本轮没枚举完，才算"部分"、才不缓存。把两者混成一档并统一禁用缓存，会让 6 层地图树每轮快照都重扫整棵树，正好撞"零新增接口调用 / 不扫图"口径。Task 8 因此补第二条用例钉住"深度到限仍缓存"。

- [ ] **Step 1: 写失败用例**

```python
def test_truncated_map_tree_is_flagged_and_not_cached():
    """地图数量撞 800 张上限才算"部分"，且不许当完整树缓存一整个会话。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    lua.execute("""
        C_Map = {}
        C_Map.GetFallbackWorldMapID = function() return 1 end
        C_Map.GetMapChildrenInfo = function(id)
            local out = {}
            for i = 1, 900 do out[i] = { mapID = 1000 + i } end
            return out
        end
    """)
    rows, truncated = gql.Api.WQMapList()
    if not bool(truncated):
        return fail("truncated_map_tree_is_flagged_and_not_cached",
                    "被 800 张上限截断却报了完整")
    rows2, truncated2 = gql.Api.WQMapList()
    if rows2 is None or not bool(truncated2):
        return fail("truncated_map_tree_is_flagged_and_not_cached",
                    "截断那一轮不该写缓存，下一轮要能重新枚举")
    if "截断" not in str(gql.Api.LastDegrade()):
        return fail("truncated_map_tree_is_flagged_and_not_cached",
                    "截断必须留一条降级日志")
    ok("truncated_map_tree_is_flagged_and_not_cached")
```
第二条用例（钉住"深度到限仍缓存"）：
```python
def test_deep_map_tree_is_still_cached():
    """6 层合法地图树按深度停遍历，但结果照常缓存，不许每轮重扫整棵树。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    lua.execute("""
        GQL_MAP_CALLS = 0
        C_Map = {}
        C_Map.GetFallbackWorldMapID = function() return 1 end
        C_Map.GetMapChildrenInfo = function(id)
            GQL_MAP_CALLS = GQL_MAP_CALLS + 1
            if id < 7 then return { { mapID = id + 1 } } end
            return {}
        end
    """)
    rows, truncated = gql.Api.WQMapList()
    if bool(truncated):
        return fail("deep_map_tree_is_still_cached",
                    "深度到限是合法形状，不该报部分")
    calls1 = int(lua.execute("return GQL_MAP_CALLS"))
    gql.Api.WQMapList()
    if int(lua.execute("return GQL_MAP_CALLS")) != calls1:
        return fail("deep_map_tree_is_still_cached",
                    "深度到限那一轮没写缓存，下一轮又扫了一遍树")
    ok("deep_map_tree_is_still_cached")
```
`Api.WQMapList` 当前 `:273` 会在 `walk(7, 6)` 时按 `depth > 5` 直接 return，本用例的替身只会被问 6 次；修前这轮会写缓存、用例绿，修后若把深度也当成截断就会红——它守的是"不许把深度并进 truncated"。

注册两行：`test_truncated_map_tree_is_flagged_and_not_cached,`、`test_deep_map_tree_is_still_cached,`。
验证 RED：`python tests/run_gql_tests.py -k truncated_map_tree,deep_map_tree_is_still_cached` → 第一条 FAIL（`truncated` 为 None），第二条应绿。

- [ ] **Step 2: 实现截断标记**

`Api.lua:245` 的 `local wqMapCache` 不变；在 `Api.WQMapList` 内：
- `:271` 的 `local list, seen = {}, {}` 改为 `local list, seen, truncated = {}, {}, false`
- `:273` 的单行守卫拆成两行，**只有容量上限才置 `truncated`**：
```lua
        if #list >= 800 then truncated = true; return end
        if depth > 5 then return end
```
  深度到限是地图树的合法形状：停掉这一枝的遍历即可，整份结果仍是完整的，必须照常进缓存。
- `:288-290` 整段替换为：
```lua
    if #list == 0 then return nil end
    if truncated then
        Degrade("地图数量上限（800）截断，本轮只拿到部分图")
        -- 容量截断结果不缓存：下一轮（地图数据就绪后）还能重新枚举完整树。
        return list, true
    end
    wqMapCache = list
    return list, false
```
`Api.lua:247` 的缓存命中返回改为 `if wqMapCache then return wqMapCache, false end`。

- [ ] **Step 3: 把标记带进 `GetPool` 的 meta**

`Api.lua:680-681` 替换为：
```lua
                local mapIDs, mapTruncated
                if GQL.Config and GQL.Config.TrackedMapIDs then
                    mapIDs = GQL.Config.TrackedMapIDs()
                end
                if not mapIDs then mapIDs, mapTruncated = Api.WQMapList() end
```
`Api.lua:723-725` 的返回表补一个字段：
```lua
                        return merged, nil, { partial = true, source = src.path,
                                              maps = #mapIDs,
                                              emptyMaps = (#em > 0 and em or nil),
                                              truncated = mapTruncated or nil }
```
`partial=true` 语义不动；`Sync.SnapshotNow` 不需要读 `truncated`（`/gql probe` 与日志能看见即可）。

- [ ] **Step 4: GREEN + 地图枚举回归**

Run:
```powershell
python tests/run_gql_tests.py -k "truncated_map_tree,deep_map_tree_is_still_cached,getpool_enumerates_all_maps_without_player_location,getpool_queries_only_tracked_maps,getpool_enumerates_continent_maps,keyword_hunt_scans_tracked_maps_only"
```
Expected: 全绿。`Api.WQMapList()` 的其它调用点（`Config.lua:634`、`Probe.lua:1336`、`Defaults.lua:137`）只取第一个返回值，多返回一个布尔不破坏它们。

- [ ] **Step 5: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `468/468 单测通过`。不提交。

---

### Task 9: M5 — 缺 `isCurrentEvent` 字段的普通 POI 被当事件 pin

**Files:**
- Modify: `GoldQuestLedger/Api.lua:1321`
- Test: `tests/run_gql_tests.py`（新增 1 条）

**Interfaces:**
- Consumes: `Api.EventPins(mapID)` → `table|nil`；**Task 0 Step 4 的 pin 字段取证**
- Produces: 只收 `info.isCurrentEvent == true` 的 pin

**Gate（第二轮复核第 3 条）**：`isCurrentEvent ~= false` 收紧成 `== true` 的依据只有报告 M5 的推论。仓库里 `Api.lua:1315` 记的是「12.1.5 实测 `GetAreaPOIInfo` 给 name/isCurrentEvent/isPrimaryMapForPOI」这份**字段清单**，但没记丰饶/亚基这两条 pin 上该字段的**实际取值**；`Probe.lua:1371` 只读取它、`Probe.lua:1389` 把全字段摊进报告、`run_gql_tests.py` 的 `isCurrentEvent: True` 是**照现行判据造的夹具**，都不能证明真客户端这两条 pin 答的是 `true`。要补的正是这个取值。**Task 0 Step 4 未打印出两条目标 pin 的 `true` 之前，本任务整任务 BLOCKED**：不写用例、不注册、不改 `Api.lua`，全量数字顺延 -1（见文末自检）。否则可能整批事件行掉组。

**2026-11-06 更新：Gate 已通过** —— 仓库里早有取证：`docs/probe.txt:399`（丰饶）、`:329` / `:372`（亚基）都是 `isCurrentEvent=true(boolean)`，`:436-443` 的 8 条事件 pin 汇总全部标「真」，无反例。不需要再跑任何命令，也不需要进具体地图；本任务从 BLOCKED 解封，但**是否实施仍由用户决定**（此前选的是"先不改"）。若实施，新增 1 条用例、改 `Api.lua:1321` 一行，全量由 `473/473` 变 `474/474`。

- [ ] **Step 1（Gate 已通过，待用户点头才执行）: 写失败用例**

```python
def test_poi_without_current_event_flag_is_not_an_event_pin():
    """客户端没标 isCurrentEvent 的飞行点/副本口不许混进事件 pin。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    lua.execute("""
        C_AreaPoiInfo = {}
        C_AreaPoiInfo.GetEventsForMap = function(mapID) return { 9001 } end
        C_AreaPoiInfo.GetAreaPOIInfo = function(mapID, id)
            return { name = "飞行点", isPrimaryMapForPOI = false }
        end
    """)
    pins = gql.Api.EventPins(2007)
    if pins is None:
        return fail("poi_without_current_event_flag_is_not_an_event_pin",
                    "接口答了空清单，不该回 nil")
    if len(pins) != 0:
        return fail("poi_without_current_event_flag_is_not_an_event_pin",
                    f"没标 isCurrentEvent 的 POI 被收了 {len(pins)} 条")
    ok("poi_without_current_event_flag_is_not_an_event_pin")
```
注册：`test_poi_without_current_event_flag_is_not_an_event_pin,`。验证 RED：`python tests/run_gql_tests.py -k poi_without_current_event_flag`（收到 1 条）。

- [ ] **Step 2: 收严判据**

`Api.lua:1321` 替换为：
```lua
            if okInfo and type(info) == "table" and info.isCurrentEvent == true then
```
与 `Api.lua:1295` 的注释"只认客户端自己标 isCurrentEvent 的那几条"一致；`Probe.lua:1371` 的取证列仍原样摊字段，不受影响。

- [ ] **Step 3: GREEN + pin 用例族回归**

Run:
```powershell
python tests/run_gql_tests.py -k "poi_without_current_event_flag,event_pin,area_poi,pin_registered,pin_that_carries"
```
Expected: 全绿。若有现成 pin 用例红，原因是它的夹具构造 POI 时没带 `isCurrentEvent`——**在夹具里补 `isCurrentEvent=True`（模拟客户端真标了事件），不得改回 `~= false`**，并在检查点记下被补的用例名与行号。

**夹具行号勘误**：原计划给的那个夹具行号不对，实际带 `isCurrentEvent` 的夹具是 `run_gql_tests.py:3665`、`:3779`、`:4206`（`:3763` 只是注释提到）。这三处都是**造出来的**，不能当作 12.1.5 真客户端的凭据；Gate 只认 Task 0 Step 4 的 `/run` 打印。

- [ ] **Step 4: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: Gate 通过时 `469/469 单测通过`；**Gate 未通过时本任务不执行，全量仍 `468/468`**，绝不写 `469/469`。不提交。

---

### Task 10: M9 — 周汇总把孤儿流水与跨域流水算进本周

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:1948-1970`（`turnins` 循环）、`:1971-1983`（`manual` 循环）
- Test: `tests/run_gql_tests.py`（新增 3 条，其中孤儿用例待 D3）

**Interfaces:**
- Consumes: `L.Prune`（`:2035`）删 `db.quests[questID]` 但保留所有流水（v7 定稿，不动）
- Produces: `L.WeekStats(db, charKey, cycleStart, now)` 只统计"与锚点同域"的流水；孤儿过滤是否启用取决于 D3

**范围声明**：按 Global Constraints，**不删任何 `db.turnins` / `views` / `reads` / `manual` 数据**（`test_prune_bounds_size`:3176 钉住"清理任务定义不应删掉金币历史"）。修复只发生在统计读侧。

**`cycleStart = 0` 口径（第二轮复核第 5 条）**：`Ledger.lua:1933` 明写"传 0 是不加过滤"，`run_gql_tests.py:3074/3517/9785/16461` 等 10+ 处用 `WeekStats(..., 0)`。原计划的 `not cycleStart or ((r.t > 1e9) == (cycleStart > 1e9) ...)` 会把 0 当成 uptime 域：这些用例的流水恰好都是开机秒所以还过，但纪元秒流水配 0 会静默算成"完成 0"。实现必须写成 `local filtered = cycleStart and cycleStart > 0`，`not filtered` 直接放行；并补第三条用例钉住这个意图。

**决策点 D3（玩家可见数字，需用户点头）**：`Ledger.lua:1134-1153` 的 `OnTurnIn` 只把信息记进 `db.turnins`，**不会补建 `db.quests[questID]`**。所以"面板没开、这条任务从没进过账本，但这一号真交过一次"的流水，一旦加上孤儿过滤就不再计入本周完成数。方向符合"只数账本里的"，但会改变玩家看得见的数字。
- **D3 点头**：执行 Step 1 的孤儿用例与 Step 2 的 `if not (db.quests and db.quests[questID])` 分支。
- **D3 未点头 / 未拍板**：孤儿用例不注册、孤儿分支不写；本任务只做"跨域过滤 + `cycleStart=0` 放行"两项，检查点数字顺延 -1。

- [ ] **Step 1: 写用例（2 条 RED + 1 条口径守卫）**

```python
def test_week_stats_ignores_orphan_turnins_but_keeps_history():
    """已被 Prune 清出账本的任务，流水留作历史但不许计进本周完成数。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_WS = { quests = {}, chars = {}, manual = {}, turnins = {},
                   cycleAnchor = 1000, clockDomain = "uptime" }
        GQL_WS.turnins["A-永恒之井"] = {}
        GQL_WS.turnins["A-永恒之井"][999] = { { t = 1200, k = "999:123" } }
        GQL_WS.chars["A-永恒之井"] = { views = {}, reads = {} }
    """)
    result = lua.execute(
        "return GQL.Ledger.WeekStats(GQL_WS, 'A-永恒之井', 1000, 1300)")
    if int(result[0]) != 0:
        return fail("week_stats_ignores_orphan_turnins_but_keeps_history",
                    f"孤儿流水被计进本周：{int(result[0])} 条")
    if lua.execute("return GQL_WS.turnins['A-永恒之井'][999] == nil"):
        return fail("week_stats_ignores_orphan_turnins_but_keeps_history",
                    "金币历史不许删")
    ok("week_stats_ignores_orphan_turnins_but_keeps_history")


def test_week_stats_skips_cross_domain_flow_entries():
    """锚点是开机秒、流水是纪元秒时不比较，不许把全部历史算进本周。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_XD = { quests = {}, chars = {}, manual = {}, turnins = {},
                   cycleAnchor = 1000, clockDomain = "uptime" }
        GQL_XD.turnins["A-永恒之井"] = {}
        GQL_XD.turnins["A-永恒之井"][999] = {
            { t = 1700000000, k = "999:123" } }
        GQL_XD.quests[999] = { name = "跨域样本" }
        GQL_XD.chars["A-永恒之井"] = { views = {}, reads = {} }
    """)
    result = lua.execute(
        "return GQL.Ledger.WeekStats(GQL_XD, 'A-永恒之井', 1000, 1300)")
    if int(result[0]) != 0:
        return fail("week_stats_skips_cross_domain_flow_entries",
                    f"跨域流水被计进本周：{int(result[0])} 条")
    ok("week_stats_skips_cross_domain_flow_entries")
```
第三条用例（钉住 `cycleStart = 0` 的"不加过滤"口径）：
```python
def test_week_stats_zero_cycle_start_does_not_filter_by_domain():
    """cycleStart=0 是"不加过滤"，纪元秒流水也必须照常计入。"""
    lua, gql = load_lua("Config", "Ledger")
    lua.execute("""
        GQL_Z = { quests = {}, chars = {}, manual = {}, turnins = {},
                  cycleAnchor = 1000, clockDomain = "uptime" }
        GQL_Z.turnins["A-永恒之井"] = {}
        GQL_Z.turnins["A-永恒之井"][999] = {
            { t = 1700000000, k = "999:123" } }
        GQL_Z.quests[999] = { name = "零锚点样本" }
        GQL_Z.chars["A-永恒之井"] = { views = {}, reads = {} }
    """)
    result = lua.execute(
        "return GQL.Ledger.WeekStats(GQL_Z, 'A-永恒之井', 0, 1300)")
    if int(result[0]) != 1:
        return fail("week_stats_zero_cycle_start_does_not_filter_by_domain",
                    f"0 = 不加过滤，实际计了 {int(result[0])} 条")
    ok("week_stats_zero_cycle_start_does_not_filter_by_domain")
```
这条修前修后都应绿（原代码 `r.t >= 0` 恒真），它是口径守卫，不要当 RED 用例期待失败；若它转红，说明同域判据把 0 当成 uptime 域了。

注册三行到 `tests = [...]`：`test_week_stats_ignores_orphan_turnins_but_keeps_history,`（**仅 D3 点头时注册**）、`test_week_stats_skips_cross_domain_flow_entries,`、`test_week_stats_zero_cycle_start_does_not_filter_by_domain,`。
验证 RED：`python tests/run_gql_tests.py -k week_stats_ignores_orphan_turnins_but_keeps_history,week_stats_skips_cross_domain_flow_entries,week_stats_zero_cycle_start_does_not_filter_by_domain` → 前两条拿到 1 条计数（FAIL），第三条绿。

- [ ] **Step 2: 统计侧过滤**

在 `Ledger.lua:1948` 循环之前先定一个口径变量（**这是第二轮复核第 5 条的核心修法**）：
```lua
    -- cycleStart = 0 / nil 是"不加过滤"（Ledger.lua:1933 既定口径），
    -- 不能拿 0 当 uptime 域去做同域比较。
    local filtered = cycleStart and cycleStart > 0
```
`Ledger.lua:1948-1970` 的循环替换为（保留 `:1941` 原有的 `db.clockDomain == "uptime"` 单向守卫不动；下面是 **D3 点头版**）：
```lua
    for questID, rows in pairs(db.turnins[charKey] or {}) do
        -- L.Prune 清出任务定义时按 v7 定稿保留流水（金币历史一条不删），
        -- 【D3】用户点头后：已不在账本里的任务不许再给本周完成数/金币背书。
        if not (db.quests and db.quests[questID]) then
            -- 孤儿历史：只留档，不统计
        else
            for i = 1, #rows do
                local r = rows[i]
                -- 逐笔同域比较：锚点是开机秒、这笔是纪元秒（或反过来）时
                -- 判不出"是不是本周"，按 fail-closed 不计入。
                -- not filtered 直接放行：0 / nil 不加任何过滤。
                if r.t and (not filtered or
                        ((r.t > 1e9) == (cycleStart > 1e9)
                         and r.t >= cycleStart)) then
                    count = count + 1
                    counted[questID] = true
                    if r.k ~= nil then
                        local g = L.CellRewardMoney(db, charKey, questID, nil, r.k)
                        if type(g) == "number" and g > 0 then
                            gold = gold + g
                            goldKnown = true
                        end
                    end
                end
            end
        end
    end
```
**D3 未点头时**：把上面 `if not (db.quests and db.quests[questID]) then`、`-- 孤儿历史：只留档，不统计`、`else` 三行，以及与之配对的倒数第二个 `end` 一起删掉，内层 `for` 缩进还原为 8 空格；其余逐字不变（纯删除，不需改写逻辑）。

`manual` 循环（`:1971-1973`）的条件同样改为（`not filtered` 放行 0/nil）：
```lua
    for questID, m in pairs(db.manual[charKey] or {}) do
        if m.state == "done" and not counted[questID] and (db.quests or {})[questID]
            and (not filtered or
                ((m.t or 0) > 1e9) == (cycleStart > 1e9)
                and (m.t or 0) >= cycleStart) then
```
`manual` 那行的 `(db.quests or {})[questID]` 同样只在 D3 点头时保留；未点头时删掉这个合取项。其余 `:1974-1983` 不动。

- [ ] **Step 3: GREEN + 周汇总用例族回归**

Run:
```powershell
python tests/run_gql_tests.py -k "week_stats,week_stats_zero_cycle_start_does_not_filter_by_domain,prune_bounds_size,prune_sweeps,dump_shows_stale_char,tooltip_cell_without_char_read"
```
Expected: 全绿。特别是 `test_prune_bounds_size` 必须仍断言到流水保留、`test_week_stats_zero_cycle_start_does_not_filter_by_domain` 必须绿；若后者红，说明 `filtered` 没放行 `cycleStart=0`。

- [ ] **Step 4: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `470 + Pew + Gate + D3` 条全绿（`Pew` = Task 2 的 PEW 条件用例、`Gate` = Task 9 是否通过、`D3` = 孤儿过滤是否点头，各取 0/1）：Pew=0 且后两者都通过时 `472/472`；只过一个 `471/471`；都不过 `470/470`。不提交。

---

### Task 11: H7 — SavedVariables 字段类型损坏时 `Config.Init` 静默放行

**Files:**
- Modify: `GoldQuestLedger/Config.lua:386-407`
- Test: `tests/run_gql_tests.py`（新增 1 条）

**Interfaces:**
- Consumes: `Config.Log`（`:116`）、`Config.Migrate`（`:368`）
- Produces: `Config.Init()` 把非表字段重建为空表并留一条日志

- [ ] **Step 1: 写失败用例**

```python
def test_config_init_repairs_non_table_fields():
    """老档/损坏档里字段是字符串或布尔时，Init 要重建，不许带病运行。"""
    lua, gql = load_lua("Core", "Config", "Api", mock=True)
    g = lua.globals()
    g.GetQuestResetTime = lambda: 1200000
    g.GQLDB = lua.table_from({
        "quests": True,
        "config": lua.table_from({"keywords": "坏词"}),
    })
    gql.Config.Init()
    if str(lua.execute("return type(GQLDB.quests)")) != "table":
        return fail("config_init_repairs_non_table_fields", "quests 没被重建")
    if str(lua.execute("return type(GQLDB.config.keywords)")) != "table":
        return fail("config_init_repairs_non_table_fields", "keywords 没被重建")
    logs = lua.execute(
        "return table.concat(GQLDB.logs or {}, '\\n')")
    if "类型" not in str(logs):
        return fail("config_init_repairs_non_table_fields", "重建没留日志")
    ok("config_init_repairs_non_table_fields")
```
注册：`test_config_init_repairs_non_table_fields,`。验证 RED：`python tests/run_gql_tests.py -k config_init_repairs_non_table_fields`（`quests` 仍是 boolean）。

- [ ] **Step 2: 类型校验实现**

`Config.lua:387-400` 整段替换为：
```lua
function Config.Init()
    if type(GQLDB) ~= "table" then GQLDB = {} end
    local db = GQLDB
    local bad = {}
    local function ensure(parent, key)
        local v = parent[key]
        if type(v) ~= "table" then
            if v ~= nil then bad[#bad + 1] = key end
            parent[key] = {}
        end
    end
    ensure(db, "quests")
    ensure(db, "chars")
    ensure(db, "turnins")
    ensure(db, "manual")
    ensure(db, "logs")
    ensure(db, "config")
    ensure(db.config, "extraMaps")
    ensure(db.config, "keywords")
    ensure(db.config, "defaults")
```
`:401-407` 的 `minGold` / `syncOnLogin` 兜底保持原样；在 `:409` 之前插入：
```lua
    if #bad > 0 then
        Config.Log("存档字段类型损坏已重建: " .. table.concat(bad, ","))
    end
```
`db.config.subMaps`、`db.minimapAngle` 仍由各自写入方补建（不动它们的所有权）。`Config.Migrate` 的档位 2..11 齐全，漏档位当成功只作未来健壮性观察，本轮不改。

- [ ] **Step 3: GREEN + Init 用例族回归**

Run:
```powershell
python tests/run_gql_tests.py -k "config_init,config_init_repairs_non_table_fields,cycle_anchor,log_ring_buffer,lua_upvalue_budget"
```
Expected: 全绿；`test_config_init_idempotent` 必须仍绿（第二次 Init 不产生 "类型" 日志）。

- [ ] **Step 4: 检查点**

Run: `python tests/run_gql_tests.py`
Expected: `471 + Pew + Gate + D3` 条全绿（Pew=0 且后两者都通过时 `473/473`）。不提交。

---

### Task 12: 发布前清理（L1 / L3）与驳回清单落档

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `GoldQuestLedger/Config.lua`（`EscapeUI`、`AddKeyword` 上限）
- Modify: `GoldQuestLedger/Options.lua:513,538`（**不是** `:590/:593`，那里是 `SetMovable` / `OnDragStart`，照旧行号改会改坏拖动）
- Modify: `GoldQuestLedger/Dump.lua:563`
- Modify（仅 D2 选中）: `GoldQuestLedger/Core.lua:13`、`GoldQuestLedger.toc:5`
- Test: `tests/run_gql_tests.py`（新增 2 条静态/输入用例）
- Read-only: `docs/superpowers/plans/2026-11-06-gql-review-remediation.md`（本计划附录 A）

**Interfaces:**
- Consumes: Task 11 的 `Config.Init`；`git log` 已核实的发布提交
- Produces: `Config.EscapeUI(s)`、`Config.AddKeyword` 的 64 字符上限、`CHANGELOG.md` 的 0.5.1/0.5.2 **历史**条目；D2 选中才产出 0.5.3 版本号

**决策点 D2（版本发布，需用户点头）**：`Core.lua:13` 与 `GoldQuestLedger.toc:5` 都是 `0.5.2`，而 `0.5.2` 早已发布（`dccc646`）。本轮改动若要以新版本发布，必须**同时**改 `Core.lua:13`、`.toc:5`、`CHANGELOG.md` 三处为 `0.5.3` 并写新条目；若只是补文档，就保持 `0.5.2` 不动，只补历史条目。两种情况下 `test_changelog_mentions_the_shipped_version` 都应绿（因为 CHANGELOG 里会有 `## 0.5.2`）。

- [ ] **Step 1: 版本文档守卫用例（先 RED）**

```python
def test_changelog_mentions_the_shipped_version():
    """CHANGELOG 必须写到 toc / Core.VERSION 那个版本，别停在 0.5.0。"""
    import re
    core = open(os.path.join(PLUGIN_DIR, "Core.lua"), encoding="utf-8").read()
    m = re.search(r'GQL\.VERSION\s*=\s*"([^"]+)"', core)
    if not m:
        return fail("changelog_mentions_the_shipped_version", "读不到 GQL.VERSION")
    ver = m.group(1)
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        changelog = f.read()
    if ("## " + ver) not in changelog:
        return fail("changelog_mentions_the_shipped_version",
                    f"CHANGELOG 缺 {ver} 条目")
    ok("changelog_mentions_the_shipped_version")


def test_keywords_are_escaped_and_length_bounded():
    """玩家输入的 |c/|H/|T 转义串不许直接进 FontString / 聊天框。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    escaped = str(gql.Config.EscapeUI("|cffff0000坏|r"))
    if not escaped.startswith("||c"):
        return fail("keywords_are_escaped_and_length_bounded",
                    f"转义没生效：{escaped}")
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    ok_add, msg = gql.Config.AddKeyword("x" * 65)
    if bool(ok_add):
        return fail("keywords_are_escaped_and_length_bounded",
                    "超长关键词应被拒")
    ok("keywords_are_escaped_and_length_bounded")
```
注册两行：`test_changelog_mentions_the_shipped_version,`、`test_keywords_are_escaped_and_length_bounded,`。
验证 RED：`python tests/run_gql_tests.py -k "changelog_mentions_the_shipped_version,keywords_are_escaped_and_length_bounded"` → 2 条 FAIL。

- [ ] **Step 2: 按已发布的提交补 CHANGELOG 历史条目**

在 `CHANGELOG.md` 的 `## 0.5.0` 之前插入。**只写已经发生的事**：`git log --oneline` 已核实 `e48fe16` = 0.5.0、`34001fa` = 0.5.1、`dccc646` = 0.5.2；措辞沿用 0.5.0 的玩家话术（加粗短标题 + 一句解释），不写"接口形状/实例编号/时钟域"这类取证术语。原计划把本轮**尚未实施**的 S1/S2 等改动记进 0.5.2，等于把没发生的事写进已发布版本，已整段删除。

```markdown
## 0.5.2
- **周常换 CD 不再掉行**：换周之后，其它角色的完成记录能正常留在清单里了。

## 0.5.1
- **丰饶事件跟着自己的时间走**：丰饶供品的事件标记按它自己的剩余时间轮换归属，换地图时不再记错地方。
```

**只有 D2 选中（发 0.5.3）时**才额外做：把 `Core.lua:13` 与 `.toc:5` 的 `0.5.2` 改成 `0.5.3`，并在 CHANGELOG 最上方加 `## 0.5.3` 段落——内容按本轮**实际完成**的任务写，同样用玩家话术。D2 未选中时三处版本号都不动。

- [ ] **Step 3: 输入转义与长度上限**

`Config.lua` 在 `Config.AddKeyword` 之前插入：
```lua
--- 纯文本进 FontString / 聊天框前把 | 转义：|c / |H / |T 会被客户端当
--- 颜色码 / 链接 / 纹理解析，玩家自己加的关键词不该改写 UI 或伪造链接。
function Config.EscapeUI(s)
    return (tostring(s or ""):gsub("|", "||"))
end
```
`Config.lua:527-528` 的 `AddKeyword` 开头替换为：
```lua
function Config.AddKeyword(k)
    k = (type(k) == "string" and k:match("^%s*(.-)%s*$")) or ""
    if k == "" then return false, "关键词为空" end
    if #k > 64 then return false, "关键词太长（上限 64 字）" end
```
`Options.lua:513`（`r.__label:SetText(kw .. "  " .. tostring(hits[kw] or 0))`）替换为：
```lua
        r.__label:SetText(GQL.Config.EscapeUI(kw) .. "  " .. tostring(hits[kw] or 0))
```
`Options.lua:538`（`pg.colR:SetText(...)`）替换为：
```lua
    pg.colR:SetText(sel and ("「" .. GQL.Config.EscapeUI(sel) .. "」匹配的任务") or "匹配的任务")
```
**行号勘误**：原计划写的 `:590/:593` 是 `frame:SetMovable(true)` 与 `OnDragStart` 的拖动处理，照它改会改坏面板拖动。按内容定位这两处 `SetText`。
`Dump.lua:563` 替换为：
```lua
        print("GQL: 账本里没有名字含「" .. GQL.Config.EscapeUI(arg) .. "」的任务（清出账本的老任务也查不到）")
```

- [ ] **Step 4: GREEN + 关键词/设置面板回归**

Run:
```powershell
python tests/run_gql_tests.py -k "changelog_mentions_the_shipped_version,keywords_are_escaped_and_length_bounded,panel_can_add_and_remove_keyword,panel_shows_keyword_hit_count,ui_text_no_forensic_words,version_has_one_readable_source"
```
Expected: 全绿。`test_ui_text_no_forensic_words`（runner:9437）明确不扫取证面，转义不触碰 `print` / `Config.Log` / `Dump` 的取证正文。

- [ ] **Step 5: 最终全量 + 语法 + 检查点**

Run:
```powershell
python tests/run_gql_tests.py --syntax
```
Expected: `473 + Pew + Gate + D3` 条全绿（Pew=0 且后两者都通过时 `475/475`），14 行 `PASS syntax:*.lua`，14 行 `PASS upvalues:*.lua`，退出码 0。D2 只改版本号与文案，不占用例名额；若 D2 选中，Step 4 的 `-k` 必须包含 `version_has_one_readable_source` 并确认 `Core.lua` / `.toc` 仍一致。把最终数字与 Task 0 基线（459）一起记进检查点。不提交。

- [ ] **Step 6: 落档驳回清单（附录 A）**

本计划附录 A 即驳回清单，不需要另建文件。确认附录 A 的"暂缓项"没有在本轮任何任务里被偷偷改掉：`AqirInvasion.lua`、`AbundantOfferings.lua`、`Dump.lua` 的 charKey 输出、`Config.Log` 的 charKey、`tests/tools/` 的本地残留，全部保持原样。

---

## 附录 A：驳回清单（本轮不改）

### A1. 原报告 8 处硬事实错误（已核，禁止按原条目动手）

| 条目 | 错在哪 | 正确事实 |
|---|---|---|
| S2 后半 | "749-752 两段降级都永不执行" | 只有 `Api.lua:751-752` 是死代码；`:749-750` 对不带 mapID 的源正常执行（`okCall` 在 `:735` 赋值） |
| H3 | "到期门直接用旧 roundEnd，两门不同源" | 两门都走 `InstanceEndRaw`（`Ledger.lua:659-673`），`:665` 已调同一份墓碑判据；分岔只是 H2 的下游，已并入 Task 5 |
| M3 | "trustedSrc 因加载顺序拿到空表、族搬图失效" | toc 里 Config 先于 Ledger，且表是运行时 lazy 构造，生产不可达；离线单文件加载是 `Ledger.lua:191-195` 明写的支持场景，仅属低优先健壮性 |
| M7 | "把 pcall 的 ok 当'地图开没开'" | `Sync.lua:170` 的 `and` 链末尾有真 `wmf:IsShown()`；属实的是 pcall 白包一次 + `:176` 第二次开图不检查结果 |
| H6 的 PreloadRewardData 子项 | "谎报已向服务器要数据" | `Sync.lua:456-460` 先查入口存在性；`:446` 注释说明 `byChar[charKey]=0` 是"这一号已了结"标记。H6 其余例证（pcall 吞返回值、`Probe.Run` 只保护 4 段、`Matrix.lua:1576` 裸调 WallClock）仍成立，列 A3 |
| M14 | "具名帧无前缀隔离" | `GQLFrame` / `GQLSyncFrame` / `GQLLogFrame` 都带 GQL 前缀；保留的是"`GQL = GQL or NS or {}` 共享同名全局表 + 加载期静态捕获 `GQL.Ledger`"这一条耦合观察，降为低 |
| L2 | "版本表不测低于 12.1.5 的构建" | `run_gql_tests.py:19614-19616` 显式有 `("11.2.0", False)`、`("13.0.0", False)`；但该表测的是 `Defaults.Matches`，不是 TOC 的 `120100` 组合，且无 `12.1.0` 条目 |
| M15 / L4 | "`1917.5` 属本插件测试；`SetMinGold` 全仓零调用" | `1917.5` 在 AHSnipe 的 `tests/run_tests.py`；`SetMinGold`/`GetMinGold` 被 `run_gql_tests.py:11903,11912,14209` 调用，是定稿的"门槛唯一合法写点"。另 `Api.Stage2Entries` 定义在 `Api.lua:1682`，原报告标的就是 Api.lua，`Probe.lua:2031` 只是调用点 |

体量口径更正：14 个 Lua 文件共 **14079 行**（`python -c` 逐行统计；PowerShell `Measure-Object -Line` 得 13469 是它不数空行），原报告写"约 1.1 万行"偏低。

### A2. 撞在已定稿口径上（判级过重，不是缺陷）

- **H4 亚基版本闸**：`AqirInvasion.lua:24-26` 记的是 11-01 原话"13.x 玩家亲手加词就照挂"，无 `GetBuildInfo` 是关键词准入的设计。唯一残留缺口是"12.x 老档带着旧词升到 13.x 仍会挂"，降为低优先观察；若要按 11-05 "独立文件 + 版本闸"口径对齐，补一个与 `AbundantOfferings.Active()` 对称的闸即可，属口径选择。
- **M6 补扫嵌套跑两趟**：`Sync.lua:1408` 注释已认"先落签名再扫：末尾会再同步一次"的成本。仅"`pcall(Sync.HuntKeywordMaps)` 吞掉 `:1492/:1512` 的 `return false, "…"` 原因"这一半保留为低优先观察。
- **M11 悬停收提示 / M12 hover 日志**：`Matrix.lua:677-682`、`:1109-1112` 作者知情且有 `moneyTipOn` 与 `__gqlShadowSeen` 防刷。保留的低优先项：`Matrix.lua:2022-2023` 按钮 `OnLeave` 无条件 `GameTooltip:Hide()`、`:1678-1695/1806-1811` 的 `curHi` 残留、`:1101-1103` cover 分支没有帧上标志（shadow 分支才有）。
- **M1 `MatrixColumns` 丢 now**：`Ledger.lua:550-552` 只约束 `MatrixRows` 每行编号，并明写兜底"只多问一次本地时钟、不是新增接口调用"。性能小项降为低，不扣"违反规则"。
- **S1 / S2 影响面**：S1 常态只问跟踪清单（`Config.TrackedMapIDs`），但空清单回退 `Api.WQMapList()` 全图枚举（`Config.lua:562` + `Api.lua:680-681`），代价要分档写，不能笼统说"整棵树"。S2 有 30 秒二次确认（`Sync.lua:1223-1233`）与"12.1.5 不带 mapID 源都不存在"（`Api.lua:75-76`）两道缓冲，逻辑错误成立但触发面在换客户端版本之后，判级由严重降为中；Task 3 仍修，因为 `:756` 空池优先于 `:760` 形状不符正面撞 `:664` 红线。
- **`Sync.lua:1214-1219` 清 `onBoard`**：原报告 S2 影响里已提到，且这是"板面证据每轮重问 + `ScanBoards` 紧跟重问"的设计，不单列新缺陷；它是 S2 空池误判链的下游。

### A3. 保留观察、暂缓实施（需用户另行拍板）

| 项 | 为什么缓 |
|---|---|
| H8 运行时 charKey 脱敏（`Dump.lua:518,569`、`Matrix.lua:263-328`、`Config.Log`、`Probe.lua:2378`） | 用户靠 `/gql dump` / `/gql log` 回传排障，脱敏会破坏取证工作流；`test_ui_text_no_forensic_words`:9442 明写取证面不在扫描范围。文档里的 `WTF/Account/BLUEFANE` 路径与角色名是否替换为占位符，同样需用户定（历史计划文档是否允许改写） |
| H6 其余错误处理项（pcall 吞返回值、`Probe.Run` 分段保护、`Matrix.lua:1576` 裸调、H7 之外的接口守卫） | 事实成立但分散，本轮不加范围；列入下一轮"错误处理一致性"专项 |
| M3 离线空表缓存、L5 `tests/tools/` 本地残留与 `Config.Migrate` 漏档位当成功 | 生产不可达 / 已被 `.gitignore` 的 `tests/tools/` 忽略 / 当前 2..11 档位齐全，仅未来健壮性 |
| Q2（`Matrix.lua:1209` 鼠标在滑块外松手是否收到 `OnMouseUp`）、Q4（toc 逗号分隔 Interface 是否被目标客户端支持） | 需游戏内复核，Task 7 不依赖它们的结论 |
| M2 矩阵 O(Q²·P) 全表扫描、`Ledger.lua:3227` 丢 now | 真实但需重构 `FamilyAnchorID` 的复用路径，超出本轮"最小修复"范围；Task 7 已把重绘频率压下来，二者可分开做 |

---

## 自检

**1. Spec coverage**：第一轮复核第三节确认的 S4→Task 7、S3+Q3→Task 0+6、H2（连带 H3）→Task 5、H1→Task 4、M4→Task 8、M5→Task 9、M9→Task 10、H7→Task 11、L1/L3→Task 12；补的 S1 门闩→Task 2、S2 空池优先→Task 3、"先补回归用例再动判据"→Task 1 先于所有生产改动；驳回清单→附录 A。第二轮复核 6 条全部有落点：① S4 三条用例与夹法→Task 7 的 D1；② 深度/容量分档→Task 8 的两档实现与第二条用例；③ pin 字段取证→Task 0 Step 4 + Task 9 Gate；④ CHANGELOG 事实与版本联动→Task 12 Step 2 + D2；⑤ `cycleStart=0` 与 OnTurnIn 副作用→Task 10 的 `filtered`、第三条用例、D3；⑥ 四处行号/名称→Task 12 的 `:513/:538`、Task 9 的 `:3665/:3779/:4206`、Task 6 的真实用例名、Task 5 的按内容定位，外加 Task 2 的 PEW 三态与 Task 0 Step 3 取证。

**2. Placeholder scan**：全文无占位式表述，也没有"参照其它任务"式的省略；所有代码步骤给出完整 Lua / Python 片段或精确替换行，所有命令给出预期输出。**三个决策点不是留白**：每个都给了方案表、代价、受影响用例、以及"未拍板时不写用例/不注册"的分档计数；Task 9 的 Gate 同样给了两种取值下的全量数字。唯一条件分支（Task 5 Step 4、Task 9 Step 3 的"若某用例红"）都给了明确处理指令与禁止事项。

**3. Type consistency**：`Sync.OnWorldEnter(isLoginLike)` 的 `nil/true/false` 三态在 Task 1 用例、Task 2 实现、`fire_pew` 替身、Core 的 `loginLike` 计算四处一致；`Api.WQMapList()` 的 `(list, truncated)` 在 Task 8 实现、meta、两条用例三处一致，且 `truncated` 只由容量上限置位；`Config.EscapeUI(s)` 在 Task 12 定义与 `:513`/`:538`/`Dump.lua:563` 三个调用点一致；`L.WeekStats` 的 `filtered = cycleStart and cycleStart > 0` 在 `turnins` / `manual` 两个循环与三条用例里一致；`(count, gold)` 在 Task 10 用例里按 `result[0]` 读取，与 `unpack_returned_tuples=True` 一致。新增用例名在正文、注册清单、命令里拼写一致。

**4. 计数**：设 `Pew` = Task 0 Step 3 取证为"两参皆 nil"时 Task 2 多注册的条件用例（0/1）、`Gate` = Task 9 是否通过（0/1）、`D3` = 孤儿过滤是否点头（0/1）。

| 阶段 | 全量分母 |
|---|---|
| Task 1 | 463 |
| Task 2 / 3 / 4 | 463 + Pew |
| Task 5 / 6 / 7（D1 未决时不增） | 464 / 466 / 466 + Pew |
| Task 8 | 468 + Pew |
| Task 9 | 468 + Pew + Gate |
| Task 10 | 470 + Pew + Gate + D3 |
| Task 11 | 471 + Pew + Gate + D3 |
| Task 12（收尾） | **473 + Pew + Gate + D3** |

全部闸门通过且 Pew=0 时收尾 `475/475`；D1 选 A/B 再加 Task 7 的 1 条 → `476/476`。各任务检查点段落里写的数字默认是 **Pew=0** 口径；Pew=1 时从 Task 2 起所有分子分母统一 +1，不改判据。

---

## 执行交接

**执行前必须先回答三个问题**（否则对应任务 BLOCKED）：

1. **D1**：S4 滚动合并选 A（全夹，可能吞第一格）、B（只夹下界，0.25 秒内可能滚出空洞）还是 C（本轮不动，与 M2 一起重构）？
2. **D2**：本轮改动是否发 `0.5.3`（要同步 `Core.lua:13` / `.toc:5` / CHANGELOG），还是只补 0.5.1/0.5.2 的历史条目？
3. **D3**：孤儿流水不再计入本周完成数（"面板没开但真交过"的数字会变小），是否接受？

另需用户配合完成 **Task 0 的三项游戏内取证**：Q3 状态判据（不阻塞）、PEW 两参真值（决定 Pew）、丰饶/亚基 pin 的 `isCurrentEvent` 字段（**Task 9 的硬 Gate**）。

两种执行方式：

1. **Subagent-Driven（推荐）** — 每个 Task 派新子代理，任务间由主代理复核（尤其 Task 1 的 4 条 RED、Task 8 的两档缓存、Task 10 的 `cycleStart=0` 守卫）。
2. **Inline Execution** — 本会话按 Task 顺序批量执行，每个检查点停下来给用户看全量结果。

两种方式都遵守 Global Constraints：**不自动 `git commit`**，收尾必须 `python tests/run_gql_tests.py --syntax` 全量全绿；BLOCKED 任务不偷偷执行。
