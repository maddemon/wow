# WorldQuestTracker 代码质量修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把评审确认的同步层 4 个缺陷、账本数据模型的重复判定逻辑、UI/取证层小修与文档勘误逐项落地，行为改动全部由 `tests/run_wqt_tests.py` 钉住。

**Architecture:** 分层不动：`Ledger.lua` 纯数据（不碰 WoW 接口，时间戳由调用方传入）、`Api.lua` 唯一数据接口适配器（pcall + 候选名单 + 降级留痕）、`Config.lua` 存档与口径、`Sync.lua` 只管编排与时序、`Matrix.lua`/`Options.lua` 渲染、`Dump.lua`/`Probe.lua` 取证。本轮只做"同一事实只有一处判定"的收敛，不拆文件、不做行虚拟化。

**Tech Stack:** WoW 12.1.5 内嵌 Lua（5.1 语义）、SavedVariables `WQTDB`、离线测试 Python 3 + lupa（`tests/run_wqt_tests.py`）。

## Global Constraints

- **零新增接口调用**：修法不许增加 WoW API 调用次数、不许引入轮询（`OnUpdate`、秒级重复定时器）、不许扫整棵地图树。动手前先自问"这个改动多花了什么"，并把代价写进注释。
- **不许假设 12.x 接口形态**：新增/改动的每处客户端调用都 `pcall`，取不到就降级并 `WQT.Config.Log` 留痕。离线替身**不许凭空造客户端没有的方法或返回形态**（12.1.5 矩阵砖掉就是这么来的）；已知并存的两种形态（`GetChildren` 返回一张表 / 一个个返回值）都收下。
- **报不出就说报不出**：`unknown` / `-1` / `"?"` 优先于 0；宁可行少一格，不许假数冒充真数。
- **Lua 5.1 语义**：`0` 与 `""` 是真值；`x and A or B` 在 `A` 为 false/nil 时塌到 `B`；一个原型 upvalue 上限 60（`test_lua_upvalue_budget` 把守）；`#` 遇 nil 有洞；跨文件循环用前向 `local` 声明破环。
- **加载顺序**（`WorldQuestTracker.toc`）：Core → Config → Ledger → Api → Probe → LogWindow → Sync → Dump → Track → Matrix → Options。字符串常量放 Config（早于使用方）；使用方在**运行时**引用 `WQT.Api` 是安全的。
- **测试门禁**：每个 RED 步骤先跑 `python tests/run_wqt_tests.py` 看到失败，再实现，再跑到全绿；不许改断言迁就实现。基线 238/238（条数以实跑输出为准，别把数字写进断言）。
- **提交纪律**：这是**共享工作树、共享 git index**——并行会话已经把 `AHSnipe/Scanner.lua` 与 `tests/run_tests.py` 的部分改动 **stage 进 index** 了（`git status --short` 里显示 `MM`）。所以本计划所有提交一律用**带 pathspec 的 commit**形式：

  ```bash
  git commit -m "$(cat <<'EOF' ... EOF)" -- <这一任务的几个路径>
  ```

  它只提交列出的路径，别人 staged 的内容留在 index 里不动。**禁止 `git add -A` / `git add .`，禁止裸 `git commit`**（那会把并行会话的改动一起提交）。每个任务"提交"步骤里原来写的 `git add ... && git commit -m ...` 请照此改成 pathspec commit；先 `git status --short` 复核。
- **提交信息**：中文，沿用仓库风格（`fix:` / `feat:` / `docs:` / `test:`，说清改了什么 + 为什么）。
- **测试环境约定**（写用例时照这些，别自造替身）：`sync_env([(questID, money)], extra=("Matrix",))` 返回 `(lua, wqt, g)`；`g = lua.globals()`；`g.WQT_NOW` 是会话秒；`lua.globals().WQT_FireTimers()` 触发挂起定时器；`fire(wqt, "EVENT")` 走真实事件入口；`g.C_WorldQuests = lua.table_from({...})` 换池；`fail(name, msg)` / `ok(name)`；新用例必须加进文件末尾 `tests = [ ... ]` 名单。

## 本轮不做（明确留在范围外）

- `AHSnipe/` 全部评审结论（三角耦合、两套新鲜度口径、`C_Timer` 回调不 pcall、`Config.Init` 硬清用户值、死代码、浮点当 key）——并行会话拥有这些文件。
- 拆 `Matrix.lua`（遮挡/悬停子系统约 350 行、渲染约 220 行仍在同一文件）、矩阵行虚拟化（现在每次重画全量排版，帧已池化）。
- `obs.gold` 与 `rec.goldMoney` 的存储形态合并（本轮只统一**读**的口径，见 Task 7）。
- `Api.MAP_MEMORY`（玩家口述的 10 条名字→地图）搬进存档并做点击录入 UI——需要新交互，另开计划。

---

### Task 0: 现有 WIP 落成基线提交

**Files:**
- 提交：`WorldQuestTracker/`（含未跟踪的 `Options.lua`、`Track.lua`）、`tests/run_wqt_tests.py`、`docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`、`docs/wqt-weekly-quest-families.md`、`docs/superpowers/plans/2026-10-25-wqt-code-quality-fixes.md`（本计划）

**Interfaces:**
- Produces: 干净的 HEAD，之后每个任务都能用 `git diff HEAD~1` 单看自己那一笔。

- [ ] **Step 1: 确认测试基线全绿**

Run: `python tests/run_wqt_tests.py`
Expected: 末行 `238/238 单测通过`，退出码 0。有失败就先停下报告，不要在红线上盖改动。

- [ ] **Step 2: 复核待提交内容（共享工作树，禁止全加）**

Run: `git status --short`
Expected: 只有上面四组路径进下一步；`AHSnipe/*`、`tests/run_tests.py` 留在工作树不动。

- [ ] **Step 3: 提交基线**

```bash
git status --short
git commit -m "$(cat <<'EOF'
chore: 提交 WQT 现有 WIP 作为代码质量修复的基线

阶段一（矩阵、按钮、注册周常、取证）攒下的未提交改动先落一笔，
后面每个修复单独提交，出问题能一眼定位是哪一笔引来的。
EOF
)" -- WorldQuestTracker tests/run_wqt_tests.py \
  docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md \
  docs/wqt-weekly-quest-families.md \
  docs/superpowers/plans/2026-10-25-wqt-code-quality-fixes.md
git status --short
```
Expected: 第二条 `git status --short` 里 `AHSnipe/*` 与 `tests/run_tests.py` 仍在（它们的 staged 部分不该被带走）。若 `git commit -- <path>` 报"无已跟踪内容"，说明该路径下有未 staged 的新文件：只对这些新文件 `git add <具体文件>`，再重跑 pathspec commit，**不要**改回裸 `git commit`。

---

### Task 1: 登录抓一次池（默认开、设置里可关）

**Files:**
- Modify: `WorldQuestTracker/Sync.lua`（文件头 upvalue 区约 30、`ScheduleSnapshot` 约 917-933、`OnWorldEnter` 约 1117-1125）
- Modify: `WorldQuestTracker/Config.lua`（`Init` 约 264 之后、`SetMinGold` 之后约 299）
- Modify: `WorldQuestTracker/Options.lua`（新增一行勾选框 + `Refresh` 同步勾选态）
- Test: `tests/run_wqt_tests.py`（新增 2 条；改写 `test_matrix_toggle_gates_background_sync` 约 3567-3588；4 条用例删掉 `SetPolling(True)`）

**Interfaces:**
- Produces: `Config.SyncOnLogin()` → boolean（缺省 true）、`Config.SetSyncOnLogin(v)` → boolean；`Sync` 内部 `loginPending` 一次性门闩。
- Consumes: 既有 `Sync.SetPolling` / `Sync.Polling`、去抖路径 `ScheduleSnapshot`、`Clock()` 第二个返回值（会话秒，用它量耗时）。

**为什么复用原去抖路径而不是登录立刻抓**：立刻抓会多一次池读取，且 12.1.5 实测登录那一瞬接口可能没就绪（`Api.GetPool` 回 nil 时账本必须走"跳过"分支）。去抖 10 秒后抓一次，既满足"登录看到最新板面"，又不新增第二个定时器。

- [ ] **Step 1: 写 RED 用例**

```python
def test_login_snapshot_runs_without_polling():
    """用户 2026-10-25 定的口径：登录抓一次池（默认开），之后矩阵没打开就完全静默。
    抓池只经过去抖路径，仍是一次读取，不加第二个定时器。"""
    lua, wqt, _ = sync_env([(81, 30000000)])
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    wqt.Sync.OnWorldEnter()          # 不调 SetPolling：面板没开
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()
    if lua.execute("return next(WQTDB.chars) == nil"):
        return fail("login_snapshot_runs_without_polling",
                    f"登录那次快照没跑:\n{wqt.Config.GetLogs()}")
    if len(calls) != 1:
        return fail("login_snapshot_runs_without_polling",
                    f"登录同步应只抓一次池，实际 {len(calls)} 次")
    ok("login_snapshot_runs_without_polling")


def test_login_snapshot_switch_off():
    """设置里关掉开关 = 登录不抓池（用户实测后台抓池会卡，必须能自己关）。
    手动 /wqt sync 与打开矩阵那条路不受开关影响。"""
    lua, wqt, _ = sync_env([(81, 30000000)])
    g = lua.globals()
    if wqt.Config.SetSyncOnLogin(False) is not True:
        return fail("login_snapshot_switch_off", "开关写不进去")
    if wqt.Config.SyncOnLogin() is not False:
        return fail("login_snapshot_switch_off", "开关读不出来")
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()
    if not lua.execute("return next(WQTDB.chars) == nil"):
        return fail("login_snapshot_switch_off", "开关关了登录仍抓池")
    if lua.execute("return WQT.Sync.SnapshotNow() ~= nil") is not True:
        return fail("login_snapshot_switch_off", "关开关不许牵连手动快照")
    ok("login_snapshot_switch_off")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: `login_snapshot_runs_without_polling` 红（"登录那次快照没跑"）；`login_snapshot_switch_off` 红在 `Config.SetSyncOnLogin` 不存在（用例抛错也算红）。`matrix_toggle_gates_background_sync` 此时仍绿，Step 5 才改。

- [ ] **Step 3: Config 侧开关**

`WorldQuestTracker/Config.lua`，`Init` 里 `if type(db.config.minGold) ~= "number" then db.config.minGold = DEFAULT_MIN_GOLD end` 之后加：

```lua
    -- 登录抓一次池（用户 2026-10-25 定：默认开、可关）。代价照 Sync 注释讲清：
    -- 一次登录一趟快照，只问跟踪清单里的图，不是整棵地图树；关上面板后仍然完全静默。
    if db.config.syncOnLogin == nil then db.config.syncOnLogin = true end
```

`Config.SetMinGold` 之后加：

```lua
--- 登录要不要抓一次池。缺省、以及"配置表还没建起来"（老存档、账本没就绪）都按开处理：
--- 只有玩家明确关过（false）才关。宁可按默认走，也不让 nil 悄悄变成"关"，
--- 那会让用户以为插件坏了（矩阵永远是上个 CD 的旧板面）。
function Config.SyncOnLogin()
    local db = Config.DB()
    local c = db and db.config
    if not c then return true end
    return c.syncOnLogin ~= false
end

function Config.SetSyncOnLogin(v)
    local db = Config.DB()
    if not db then return false end
    db.config = db.config or {}
    db.config.syncOnLogin = not not v
    return true
end
```

- [ ] **Step 4: Sync 侧一次性门闩**

`WorldQuestTracker/Sync.lua`，文件头 `local capture = nil` 之后加：

```lua
-- 登录那一次快照的门闩：OnWorldEnter 置位，去抖回调跑完立刻落下。
-- 不复用 polling：polling 的含义是"矩阵开着期间事件驱动也抓"，混用会让关掉面板后
-- 每次任务状态变化都抓一遍池（12.1.5 实测"做什么都一卡一卡"的成因）。
local loginPending = false
```

`ScheduleSnapshot` 整段替换：

```lua
local function ScheduleSnapshot()
    if debouncePending then return end
    -- 只有两趟会真的抓池：① 登录那一次（loginPending，设置里可关）；
    -- ② 矩阵开着期间（polling）。其余一律静默——一次快照要把每条任务的
    -- 奖励/状态/倒计时逐个问一遍，做任务时事件连成一片，不设这道门就是卡顿本身。
    if not polling and not loginPending then return end
    debouncePending = true
    C_Timer.After(DEBOUNCE_SECONDS, function()
        debouncePending = false
        local wasLogin = loginPending
        loginPending = false
        local up0 = select(2, Clock())
        local okCall, err = pcall(Sync.SnapshotNow)
        if not okCall then WQT.Config.Log("快照异常: " .. tostring(err)) end
        if wasLogin then
            -- 耗时用会话秒（单调、与墙上时钟域无关）：这是"登录卡不卡"的唯一实测口径
            local spent = select(2, Clock()) - up0
            if spent < 0 then spent = 0 end
            WQT.Config.Log(("登录同步完成 用时%d秒（矩阵没开，之后不再自动抓池）"):format(spent))
        end
        -- 抓完必须喊面板重画：这条是日常唯一自动跑的路（做任务时事件连成一片），
        -- 少了这一句就是"账本里金额/状态都更新了，屏幕上还是上一份"——12.1.5 实测
        -- 89347 那行 967 金进账本 12 分钟了矩阵里还是没有。重画只把内存里的账本再抄
        -- 一遍，零接口调用，且 Mx.Refresh 自己判面板开没开（没开就直接返回）。
        if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
    end)
end
```

`Sync.OnWorldEnter` 里 `ScheduleSnapshot()` 之前插入：

```lua
    -- 登录抓一次（默认开，设置面板与 Config.SetSyncOnLogin 可关）。只排这一趟，
    -- 之后没开面板就不再有抓池动作：门闩在去抖回调里落下，不会累积。
    loginPending = WQT.Config.SyncOnLogin() == true
```

- [ ] **Step 5: 改写与新契约冲突的旧用例**

`test_matrix_toggle_gates_background_sync` 整段替换：

```python
def test_matrix_toggle_gates_background_sync():
    """登录抓一次（默认开），之后只有矩阵开着才继续抓池：关上面板后事件驱动的
    快照整个跳过（用户实测平时抓池会卡）。Toggle 打开=开轮询；关闭=停轮询。"""
    lua, wqt, _ = sync_env([(81, 30000000)], extra=("Matrix",))
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()
    if lua.execute("return next(WQTDB.chars) == nil"):
        return fail("matrix_toggle_gates_background_sync", "登录那一次快照没跑（默认开）")
    if len(calls) != 1:
        return fail("matrix_toggle_gates_background_sync",
                    f"登录同步只该抓一次，实际 {len(calls)}")
    wqt.Matrix.Toggle()
    if wqt.Sync.Polling() is not True:
        return fail("matrix_toggle_gates_background_sync", "矩阵开着时应轮询")
    wqt.Matrix.Toggle()
    if wqt.Sync.Polling() is not False:
        return fail("matrix_toggle_gates_background_sync", "关上面板应停轮询")
    fired = len(calls)
    fire(wqt, "QUEST_LOG_UPDATE")
    g.WQT_NOW = 60
    lua.globals().WQT_FireTimers()
    if len(calls) != fired:
        return fail("matrix_toggle_gates_background_sync",
                    f"关上面板后不该再抓池，多抓 {len(calls) - fired} 次")
    ok("matrix_toggle_gates_background_sync")
```

另外 4 条用例里的 `wqt.Sync.SetPolling(True)      # 模拟矩阵打开：平时（没打开）不抓池`（约 2711、2728、2755、2772）**整行删掉**，注释改成 `# 登录同步默认就抓，不需要先打开面板`——它们现在测的正是"不打开面板也能同步"。删完逐条确认仍绿。

- [ ] **Step 6: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿（含新增 2 条）。若 `test_closing_matrix_cancels_pending_refresh`（约 3731）变红，说明门闩留下了待触发定时器：`loginPending = false` 必须在快照跑完那一刻落下，不许重排。

- [ ] **Step 7: 设置面板加开关**

`WorldQuestTracker/Options.lua`：照文件现有分节写法（`CreateFontString` 标题 + 控件 + `SafeCall`），在 `sec3 = ...` "三、金币门槛"之前插入一段，控件变量必须**加进文件顶部已有的 `local` 声明区**（不许造新全局）：

```lua
    -- 登录抓一次池（默认开）：不开的话登录后进矩阵只看到上个 CD 的旧板面。
    -- 代价写在文案里，让玩家自己决定：只问跟踪清单里的图，关上面板之后完全静默。
    local secLogin = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    secLogin:SetText("二点五、登录时抓一次世界任务池")
    loginChk = CreateFrame("CheckButton", nil, content, "UICheckButtonTemplate")
    loginChk:SetText("登录时同步（关掉后只有打开矩阵或 /wqt sync 才更新）")
    loginChk:SetScript("OnClick", function(self)
        WQT.Config.SetSyncOnLogin(self:GetChecked() == true)
        Options.Refresh()
    end)
```
`local` 声明区加 `local loginChk`；`Options.Refresh()` 末尾同步勾选态：

```lua
    if loginChk then loginChk:SetChecked(WQT.Config.SyncOnLogin() == true) end
```

- [ ] **Step 8: 提交**

```bash
git add WorldQuestTracker/Sync.lua WorldQuestTracker/Config.lua WorldQuestTracker/Options.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
feat: 登录抓一次世界任务池——默认开、设置里可关

矩阵没开时不抓池这条保留，但恢复登录那一趟：不开的话玩家进矩阵只看到
上个 CD 的旧板面。走原有去抖路径（仍是一次读取、不加第二个定时器），
日志报实测用时，开关存 db.config.syncOnLogin。
EOF
)"
```

---

### Task 2: BeginCapture 只认"有可信身份的任务"

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（新增 `L.QuestKnown`，放在 `L.State` 之前约 985）
- Modify: `WorldQuestTracker/Sync.lua`（`BeginCapture` 约 1175-1189）
- Test: `tests/run_wqt_tests.py`

**缺陷**：`QUEST_COMPLETE` 的 `arg1` 是**任何**任务（职业任务、战役任务都算），`FinishWindow` 无条件写 `L.OnTurnIn(...)` + `L.SetView(..., "done")`。于是一条从没上过板、也不给金币的任务会凭空拿到"本周交过"这份凭据——而它正是矩阵六道准入门之一（`Ledger.MatrixRows` 的 `turnInOf[questID]` 绕过金币门槛）。这就是"非金币任务赖在矩阵里"的另一半成因。

- [ ] **Step 1: 写 RED 用例**

```python
def test_turnin_of_unknown_quest_never_creates_a_row():
    """交一条账本里没有、接口也说不是世界任务的普通任务（职业/战役任务），
    不许建记录、也不许写下"本周交过"的凭据。宁可不记，也不给矩阵造假行。"""
    lua, wqt, _ = sync_env([(130, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("WQT.Api.IsWorldQuest = function() return false end")
    wqt.Sync.BeginCapture(7777)          # 7777 从没上过板、不在账本
    g.WQT_NOW = 30
    lua.globals().WQT_FireTimers()
    if lua.execute("return WQT.Config.DB().quests[7777] ~= nil") is True:
        return fail("turnin_of_unknown_quest_never_creates_a_row",
                    "交普通任务凭空建了任务记录")
    if lua.execute("""
        local db = WQT.Config.DB()
        local key = WQT.Sync.CurrentCharKey()
        return db.turnins[key] ~= nil and db.turnins[key][7777] ~= nil
    """):
        return fail("turnin_of_unknown_quest_never_creates_a_row",
                    "交普通任务写下了交任务凭据")
    ok("turnin_of_unknown_quest_never_creates_a_row")


def test_turnin_of_pooled_quest_still_settles():
    """放宽判据的代价必须有人盯着：上过板的世界任务照常记账（这条不许红）。"""
    lua, wqt, _ = sync_env([(131, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("WQT.Api.IsWorldQuest = function() return nil end")   # 接口答不出
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()        # 先把 131 收进池（拿到 poolSeen）
    wqt.Sync.BeginCapture(131)
    g.GetMoney = lambda: 100500
    g.WQT_NOW = 40
    lua.globals().WQT_FireTimers()
    if lua.execute("""
        local db = WQT.Config.DB()
        local key = WQT.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][131]
        return rows ~= nil and rows[#rows].g == 5000000
    """) is not True:
        return fail("turnin_of_pooled_quest_still_settles",
                    f"在板任务交完没记到金币:\n{wqt.Config.GetLogs()}")
    ok("turnin_of_pooled_quest_still_settles")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红（记录被建 / 凭据被写）；第二条此时应当已绿（它测的是不能被砍掉的那一面）。

- [ ] **Step 3: 实现**

`WorldQuestTracker/Ledger.lua`，`L.State` 之前加：

```lua
--- 这条任务有没有"可信身份"：上过板（池/清单证据）、玩家点名要过（注册族、关键词、
--- 右键添加、右键移除）。交任务记账只认这几条——QUEST_COMPLETE 的第一参数是任何任务，
--- 无门收录等于给矩阵造一条"本周交过"的假凭据，那比少记一笔严重得多。
function L.QuestKnown(db, questID)
    local rec = db and db.quests and db.quests[tonumber(questID)]
    if not rec then return false end
    return not not (rec.poolSeen or rec.onBoard or rec.regKey
        or rec.kwLogAdded or rec.manualAdded or rec.ignored)
end
```

`WorldQuestTracker/Sync.lua` 的 `BeginCapture`，在 `if not questID then return end` 之后插入：

```lua
    local db = WQT.Config.DB()
    -- 只给"有可信身份"的任务开钱窗：账本里没这条、接口也不说它是世界任务 → 不记。
    -- 接口答 nil（问不出）时同样不记：少记一笔能忍，假凭据会把整行留在矩阵里骗人。
    if not (db and (L.QuestKnown(db, questID)
        or (WQT.Api.IsWorldQuest and WQT.Api.IsWorldQuest(questID) == true))) then
        return
    end
```

- [ ] **Step 4: 跑测试确认 GREEN，并检查真交任务路径没被砍**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。逐条确认既有交任务用例没崩：`grep -n "def test_turnin\|BeginCapture" tests/run_wqt_tests.py`。若某条用例走"没上板的手动添加"路径，`rec.manualAdded` 已覆盖；仍然红就停下来报告，**不许靠放宽判据（例如"任何 questID 都收"）蒙过去**。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Sync.lua WorldQuestTracker/Ledger.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 交任务记账先验身份——普通任务不再凭空造出矩阵行

QUEST_COMPLETE 对任何任务都发，无条件记账等于给没上过板、也不给金币的任务
造一条"本周交过"凭据（矩阵准入门之一）。现在只认池/清单证据、注册族、
关键词、玩家点名，以及接口明确答"是世界任务"。
EOF
)"
```

---

### Task 3: 钱窗生命周期（定时器句柄 + seq + 账本就绪）

**Files:**
- Modify: `WorldQuestTracker/Sync.lua`（文件头 `local capture` 约 30、`FinishWindow` 约 1140-1165、`SampleWindow` 约 1169-1173、`BeginCapture` 约 1175-1189、文件末尾探针）
- Test: `tests/run_wqt_tests.py`

**两个缺陷**：
1. `C_Timer.After(MONEY_WINDOW_SECONDS, Sync.FinishWindow)` 句柄没存 —— 窗口内换任务时，`BeginCapture` 虽取消了"整笔丢掉"，但**旧定时器仍在**，会提前结算**新**窗口：新窗口的起点被按只走掉几秒的旧定时器算，钱少记。
2. `FinishWindow` 里 `capture = nil` 在 `if not db then return end` **之前**执行 —— 账本没就绪时那笔钱直接蒸发，且没有一行日志。

- [ ] **Step 1: 写 RED 用例**

```python
def test_money_window_seq_rejects_stale_timer():
    """换任务时旧钱窗的定时器不许结算新窗口：每个窗口只认自己那一份 seq。
    串台结算会把新窗口的钱按已经走掉的一小截算少。"""
    lua, wqt, _ = sync_env([(140, 30000000), (141, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()          # 两条任务都进账本（拿到 poolSeen）
    wqt.Sync.BeginCapture(140)
    first = int(lua.execute("return WQT.Sync.__captureSeq()"))
    g.WQT_NOW = 13
    g.GetMoney = lambda: 100500
    wqt.Sync.BeginCapture(141)              # 换任务：旧窗口应先结算、旧定时器应作废
    second = int(lua.execute("return WQT.Sync.__captureSeq()"))
    if second <= first:
        return fail("money_window_seq_rejects_stale_timer", "窗口没有自己的 seq，迟到定时器无法作废")
    if not lua.execute("return WQT.Sync.__capturing()"):
        return fail("money_window_seq_rejects_stale_timer", "换任务后新窗口不该是空的")
    g.WQT_NOW = 60                          # 旧定时器的刻度到了：它不该再动新窗口
    lua.globals().WQT_FireTimers()
    if lua.execute("""
        local db = WQT.Config.DB()
        local key = WQT.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][140]
        return rows ~= nil and #rows == 1
    """) is not True:
        return fail("money_window_seq_rejects_stale_timer",
                    f"换任务时上一笔没结算成恰好一条:\n{wqt.Config.GetLogs()}")
    ok("money_window_seq_rejects_stale_timer")


def test_money_window_kept_when_ledger_missing():
    """账本没就绪时不许丢掉窗口：那笔钱留着等下一次结算触发。
    丢了就等于"做完了也不记"，玩家看到的是空白格。"""
    lua, wqt, _ = sync_env([(142, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()
    wqt.Sync.BeginCapture(142)
    lua.execute("WQT.Config.DB = function() return nil end")
    g.WQT_NOW = 40
    lua.globals().WQT_FireTimers()
    if not lua.execute("return WQT.Sync.__capturing()"):
        return fail("money_window_kept_when_ledger_missing",
                    "账本没就绪时窗口被清掉了（那笔钱丢了）")
    if "结算推迟" not in str(wqt.Config.GetLogs()):
        return fail("money_window_kept_when_ledger_missing", "推迟结算必须留日志")
    ok("money_window_kept_when_ledger_missing")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 两条都红在 `WQT.Sync.__captureSeq` / `__capturing` 是 nil（用例抛错）。

- [ ] **Step 3: 实现**

`WorldQuestTracker/Sync.lua` 文件头，`local capture = nil` 那行替换并补三个上值：

```lua
local capture = nil          -- { questID, before, t0, seq, polluted }
local captureSeq = 0         -- 每开一窗 +1：seq 对不上就是上一个窗口遗留的定时器
local windowTimer = nil      -- 15 秒结算定时器句柄（换任务时要取消，防串台）
local sampleTimer = nil      -- 5 秒抽样定时器句柄（C_Timer.After 不给句柄，改用 NewTimer）
```

`FinishWindow` 整段替换：

```lua
--- 窗口结束才结算：净增量归这个任务，于是任务直接给的金币和随后
--- 立刻开宝箱给的金币一次全收进来（宝箱数额随机，这里取实测近似）。
--- 传入 seq 时必须仍是当前窗口，否则那是上一个窗口遗留的定时器：串台结算会
--- 拿新窗口的起点配旧刻度，钱算少。
function Sync.FinishWindow(seq)
    local c = capture
    if not c then return end
    if seq and c.seq ~= seq then return end
    local db = WQT.Config.DB()
    if not db then
        -- 账本没就绪：这一笔不能丢（丢了等于"做完了也不记"），窗口保留并再排一次。
        -- 只补一个定时器（有句柄才不重复排），不会滚成无限循环。
        WQT.Config.Log("交任务结算推迟：账本未就绪，窗口保留")
        if not windowTimer then
            local keep = c.seq
            windowTimer = C_Timer.NewTimer(MONEY_WINDOW_SECONDS, function()
                windowTimer = nil
                Sync.FinishWindow(keep)
            end)
        end
        return
    end
    capture = nil
    if windowTimer then pcall(function() windowTimer:Cancel() end) windowTimer = nil end
    if sampleTimer then pcall(function() sampleTimer:Cancel() end) sampleTimer = nil end
    local after = WQT.Api.Money()
    local gain, polluted = nil, false
    if type(c.before) == "number" and type(after) == "number" then
        polluted = c.polluted == true or MoneyUntrusted()
        if not polluted then
            local delta = after - c.before
            -- 只认进账；同期有出账（买了东西）说明窗口被污染，整笔作废
            if delta >= 0 then gain = delta end
        end
    end
    local charKey = Sync.CurrentCharKey()
    local rec = db.quests[c.questID]
    L.OnTurnIn(db, charKey, c.questID, c.t0, gain, rec and rec.currentSig or nil)
    L.SetView(db, charKey, c.questID, "done", c.t0)
    WQT.Config.Log(("交任务 quest=%s 入账=%s 窗口污染=%s")
        :format(tostring(c.questID), WQT.Config.FormatGold(gain), tostring(polluted)))
    -- 记完这笔也要重画：本周交过 = 矩阵六道准入门里的硬凭据之一（Ledger.MatrixRows），
    -- 不重画的话玩家刚做完一条金币任务，面板上那一格还是旧的。零接口调用。
    if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
end
```

`SampleWindow` 整段替换（去掉自我递归的 `C_Timer.After`，改成可取消的 `NewTimer`；窗口没了就自己停）：

```lua
--- 窗口内每隔几秒盯一次：交完任务顺手开邮箱捡钱再关掉，只在结算那一刻采样会漏判，
--- 把那笔外来入账记到任务头上。命中一次就记 polluted，结算时整笔作废。
--- 窗口没了就自己停：它不再无限递归，也不会面板关闭后仍每 5 秒问一次接口。
local function SampleWindow()
    if not capture then sampleTimer = nil return end
    if MoneyUntrusted() then capture.polluted = true end
    sampleTimer = C_Timer.NewTimer(SAMPLE_SECONDS, SampleWindow)
end
```

`BeginCapture` 末尾三行（`capture = {...}` / 两个 `C_Timer.After`）替换：

```lua
    captureSeq = captureSeq + 1
    local seq = captureSeq
    capture = { questID = questID, before = WQT.Api.Money(), t0 = Clock(), seq = seq }
    if windowTimer then pcall(function() windowTimer:Cancel() end) end
    windowTimer = C_Timer.NewTimer(MONEY_WINDOW_SECONDS, function()
        windowTimer = nil
        Sync.FinishWindow(seq)
    end)
    if sampleTimer then pcall(function() sampleTimer:Cancel() end) end
    sampleTimer = nil
    SampleWindow()
```

文件末尾（`WQT.RegisterCommand("sync", ...)` 之前）加只读探针，供离线用例断言生命周期：

```lua
-- 只给离线用例读的钱窗状态（不调任何接口，零代价）
function Sync.__capturing() return capture ~= nil end
function Sync.__captureSeq() return captureSeq end
```

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。逐条复核既有钱窗用例：`grep -n "def test_money\|MONEY_WINDOW\|def test_turnin" tests/run_wqt_tests.py`。原来靠 `C_Timer.After` 排 15 秒的断言现在要看 `WQT_NEWTIMERS`（替身里 `NewTimer` 会记 `cancelled`）；**断言目标仍是"钱进账"**，不许只测定时器数量。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Sync.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 钱窗带 seq 与定时器句柄，账本没就绪时不再蒸发那笔钱

旧窗口的定时器会结算新窗口（起点串台、钱算少）；capture 在 DB() 判空之前
就被清掉，等于这笔金币从没进过账。现在换任务先取消旧定时器、按 seq 作废
迟到结算，账本没就绪就保留窗口并记一条推迟日志。
EOF
)"
```

### Task 4: 快照里每条任务单独容错，失败也要重排刷新

**Files:**
- Modify: `WorldQuestTracker/Sync.lua`（`SnapshotNow` 的逐条循环约 806-840、`ScheduleSnapshot` 回调、`ApplyMapMemory` 约 345-360）
- Modify: `WorldQuestTracker/Ledger.lua`（`L.State` 里 `last.t` 约 1005）
- Test: `tests/run_wqt_tests.py`

**三个缺陷**：
1. 逐条循环里 `WQT.Api.QuestState(questID)`、`WQT.Api.QuestTimeLeft(questID)` 没有 pcall —— 某一条抛错会把整趟快照断在半路：后面的收日志、补地图、族搬运、消失判定、`ScheduleNextRefresh` 全不跑，账本停在半更新状态，而且**没有待触发定时器**（矩阵开着也不再自己刷新）。
2. `last.t >= L.RoundAt(db, rec)`（Sync.lua:822、Ledger.lua:1005）在 `last.t` 为 nil 时是"跟 nil 比大小"→ 抛错，正好落进上面那条无保护的路径。老存档里 `t` 缺失的交任务记录不是假设：`L.WeekStats` 自己就写着 `if r.t and ...`。
3. `local ids = p.questID and { tonumber(p.questID) } or (p.name and byTitle[p.name])`（Sync.lua:351）：`{ nil }` 在 Lua 里是**真值**，所以 `p.questID` 写成非数字时不会退回名字那条路，而是拿到一张空表，静默跳过这条口述地图（玩家看到的是"日志没抱怨、图却没归位"）。

- [ ] **Step 1: 写 RED 用例**

```python
def test_snapshot_survives_a_quest_api_error():
    """某一条任务的状态接口抛错，不许炸掉整趟快照：别的任务照常入账，
    刷新定时器照常排，出错的事留一行日志。"""
    lua, wqt, _ = sync_env([(150, 30000000), (151, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute("""
        local real = WQT.Api.QuestState
        WQT.Api.QuestState = function(qid)
            if qid == 150 then error("模拟单条状态接口抛错") end
            return real(qid)
        end
    """)
    wqt.Sync.SetPolling(True)
    res = lua.execute("local ok = pcall(WQT.Sync.SnapshotNow) return ok")
    if res is not True:
        return fail("snapshot_survives_a_quest_api_error",
                    "单条接口抛错把整趟快照炸掉了（后面还有收日志/补地图/消失判定）")
    if lua.execute("return WQT.Config.DB().quests[151] ~= nil") is not True:
        return fail("snapshot_survives_a_quest_api_error", "一条报错把别的任务也带没了")
    if lua.execute("local key = WQT.Sync.CurrentCharKey() "
                   "return WQT.Config.DB().chars[key].views[151] ~= nil") is not True:
        return fail("snapshot_survives_a_quest_api_error", "出错之后没写完别的角色视图")
    pending = lua.execute("local n = 0 for _, t in ipairs(WQT_NEWTIMERS) do "
                          "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) < 1:
        return fail("snapshot_survives_a_quest_api_error",
                    "快照出错后没排刷新定时器：矩阵开着也不会再自己更新（会话死锁）")
    if "状态问不出" not in str(wqt.Config.GetLogs()):
        return fail("snapshot_survives_a_quest_api_error", "单条接口抛错必须留日志")
    ok("snapshot_survives_a_quest_api_error")


def test_map_memory_rejects_non_numeric_quest_id():
    """口述地图表里 questID 写错成非数字时，不许静默跳过：
    { tonumber(x) } 在 Lua 里永远是真值，得先 tonumber 再判，否则退回名字那条路走不到。"""
    lua, wqt, _ = sync_env([(152, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute([[
        WQT.Api.MAP_MEMORY = {
            { questID = "96625x", name = "幽魂角斗士", map = "盘卷蛇岛" },
            { name = "任务152", map = "虚影风暴" },
        }
        WQT.Config.DB().config.extraMaps = { [2009] = true }
    ]])
    lua.execute("WQT.Api.MapName = function(id) if id == 2009 then return '虚影风暴' end end")
    lua.execute("local ok = pcall(WQT.Sync.ApplyMapMemory, WQT.Config.DB()) return ok")
    logs = str(wqt.Config.GetLogs())
    if "口述地图表的 questID 不是数字" not in logs:
        return fail("map_memory_rejects_non_numeric_quest_id",
                    f"非数字 questID 该点名报出来:\n{logs}")
    ok("map_memory_rejects_non_numeric_quest_id")
```

> 这条用例要求 `ApplyMapMemory` 对外可见：把它从 `local function ApplyMapMemory(db)` 改成 `function Sync.ApplyMapMemory(db)`，`Sync.lua` 内唯一调用点同步改成 `Sync.ApplyMapMemory(db)`（改完 `grep -n "ApplyMapMemory" WorldQuestTracker/Sync.lua` 应该只剩定义 + 一处调用）。这条用例里用 `WQT.Sync.ApplyMapMemory(WQT.Config.DB())`。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红在 `pcall(WQT.Sync.SnapshotNow)` 返回 false；第二条红在日志里没有那句点名。

- [ ] **Step 3: 实现逐条容错**

`WorldQuestTracker/Sync.lua` 的逐条循环，把 `local state = { WQT.Api.QuestState(questID) }` 到 `L.SetExpiry` 那段替换：

```lua
        if type(pool[i].money) ~= "number" then pendingMoney = pendingMoney + 1 end
        -- 每条任务自己的接口读取单独容错：QuestState/QuestTimeLeft 在某一条上抛错
        -- 不该把整趟快照断在这儿——后面还有收日志、补地图、族搬运、消失判定，
        -- 以及"重排刷新定时器"。半途崩掉等于账本停在半更新、矩阵开着也不再刷新。
        -- 代价：每条多两次 pcall（ Lua 里 pcall 不碰接口，只是调用壳）。
        local okCall, state1 = pcall(WQT.Api.QuestState, questID)
        if not okCall then
            WQT.Config.Log(("状态问不出 quest=%s: %s"):format(tostring(questID), tostring(state1)))
            state1 = nil
        end
        local rec = db.quests[questID]
        local rows = db.turnins[charKey] and db.turnins[charKey][questID]
        local last = rows and rows[#rows]
        -- 本轮亲眼记到的交任务优先于查询接口。t 可能是老存档里缺字段的记录
        -- （L.WeekStats 自己就写着 if r.t and ...），拿 nil 比大小会当场抛错。
        local lastT = last and tonumber(last.t)
        if rec and lastT and lastT >= L.RoundAt(db, rec) then
            state1 = "done"
        end
        -- 视图统一走 Ledger.SetView 写，避免 { s, t } 这个形状在两处各拼一遍
        L.SetView(db, charKey, questID, state1, now)
        -- 记下到期时刻：世界任务到点才换，据此判断下次什么时候真需要刷新。
        -- 读不到（接口偶发 nil / 数据没加载 / 抛错）也计数：补读要连倒计时一起补，
        -- 否则有的任务永远没有剩余时间（用户实测"有个任务没有倒计时了"）
        local secs, timeFailed = nil, false
        if WQT.Api.QuestTimeLeft then
            local okT, v = pcall(WQT.Api.QuestTimeLeft, questID)
            if okT then secs = v else timeFailed = true end
        end
        if type(secs) == "number" and secs > 0 then
            -- 口径由 Ledger.SetExpiry 定：池里这条若是**注册过的常驻周常**，问出来的
            -- 倒计时是它板上实例的轮换寿命，不能当到期写（12.1.5 实测"加固符文石
            -- 这个任务没了"就是这么下午整行消失的）；普通世界任务照旧用它自己的倒计时。
            if rec then L.SetExpiry(rec, now, secs) end
        elseif not timeFailed then
            -- 抛错那一路已经计过，这里补"接口答 nil / 没这个接口"那一半：
            -- 漏计就不排补读，"有的任务永远没有剩余时间"会回来（用例盯的就是这条）。
            pendingTime = pendingTime + 1
        end
```
（保留原注释里关于 `SetExpiry` 口径那一段的实际文字，别缩略；上面这段是给形状用的，落地时把 806-840 区间里原有注释一并搬回来。）

> 落地时补的第三条用例：`test_missing_countdown_also_schedules_retry` —— 接口答 nil（不是抛错）也要排补读。这条是照上面那版 `elseif` 写不出来才加的：原写法在"答 nil"那一支不计数，跟注释自相矛盾，改成 `timeFailed` 标记才对得上。

`Ledger.lua` 的 `L.State`（约 1005）改成：

```lua
    local lastT = last and tonumber(last.t)
    if obs and lastT and lastT >= at then return "done" end
```

- [ ] **Step 4: 出错也要重排刷新**

`ScheduleSnapshot` 的去抖回调里，`pcall(Sync.SnapshotNow)` 之后、耗时日志之后加：

```lua
        -- 快照半路抛错时它自己末尾那句"按最早到期排下次刷新"跑不到：这里补一次，
        -- 否则矩阵开着却再也没有下一次刷新（12.1.5 遗留的"补读会话死锁"就是这个形状）。
        -- ScheduleNextRefresh 第一句就 Cancel 旧定时器，重复调用只会留一个待触发。
        pcall(ScheduleNextRefresh)
```
`SchedulePreloadRetry` 的回调里 `pcall(Sync.SnapshotNow)` 之后加同一句（注释写"出错也要重排，见 ScheduleSnapshot"）。

- [ ] **Step 5: 修口述地图表的静默跳过**

`Sync.lua` 约 351 那行替换：

```lua
        -- 先 tonumber 再决定走哪条：`p.questID and { tonumber(p.questID) } or ...`
        -- 在 Lua 里永远走前半（{ nil } 是真值），写错一个数字就静默跳过这一条，
        -- 玩家看到的是"日志没抱怨、图却没归位"。
        local qid = tonumber(p.questID)
        if p.questID ~= nil and not qid then
            WQT.Config.Log(("口述地图表的 questID 不是数字：%s（这条没生效）")
                :format(tostring(p.questID)))
        end
        local ids = qid and { qid } or (p.name and byTitle[p.name])
```

- [ ] **Step 6: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。若 `test_snapshot_heals_missing_map` 之类因为 `ApplyMapMemory` 改名而红，按 Step 1 注里的二选一收敛，别留两条路径。

- [ ] **Step 7: 提交**

```bash
git add WorldQuestTracker/Sync.lua WorldQuestTracker/Ledger.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 快照里每条任务单独容错，出错也照排刷新定时器

QuestState/QuestTimeLeft 在某一条上抛错会把整趟快照断在半路（收日志、补地图、
消失判定、重排刷新全不跑）；last.t 缺字段时跟 nil 比大小正是这条引爆路径。
顺带修口述地图表：questID 写成非数字时不再静默跳过。
EOF
)"
```

---

### Task 5: 跨时钟域的体积清理 + 唤醒定时器封顶

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（`L.Prune` 约 1109-1129）
- Modify: `WorldQuestTracker/Sync.lua`（`ScheduleNextRefresh` 约 92-108）
- Test: `tests/run_wqt_tests.py`

**两个缺陷**：
1. `Prune` 判据 `(now or 0) - (rec.lastSeenAt or 0) > 3 * WEEK` 不看时钟域。`lastSync/lastSeenAt` 可能来自上个 CD 的纪元秒（>1e9），而 `now` 是本次开机秒（几百）——`now - lastSeenAt` 是巨大负数，永远不清（体积失控）；反过来域切换后（上次 uptime、这次 epoch）差值是几十亿，**一次快照就把整张账本清空**。同一文件里 `ThisCycle` 与 `WeekStats` 都专门防了这一手，`Prune` 是唯一漏的那处。
2. `C_Timer.NewTimer(math.max(5, due - now), ...)` 没有上限：`expiresAt` 一旦被写进很远（脏数据、周常排程值），定时器就排到几周之后，矩阵开着也不再更新，直到玩家手动刷新。

- [ ] **Step 1: 写 RED 用例**

```python
def test_prune_never_mixes_clock_domains():
    """纪元秒与本次开机秒不可比：跨域时整条按时间清理必须跳过。
    混用的差值是几十亿，一次快照就能把整张账本清光（ThisCycle/WeekStats 都防了这手）。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 100000, None)]), 1000)
    db["clockDomain"] = "epoch"
    db["quests"][7]["lastSeenAt"] = 1_700_000_000   # 上个存档写的纪元秒
    L.Prune(db, 500)                                # now 是本次开机秒
    if db["quests"][7] is None:
        return fail("prune_never_mixes_clock_domains", "跨时钟域把任务整条清掉了")
    db["clockDomain"] = "uptime"
    L.Prune(db, 1_700_000_500)                      # 反过来：now 纪元、记录开机秒
    if db["quests"][7] is None:
        return fail("prune_never_mixes_clock_domains", "反向跨域同样不该清")
    db["clockDomain"] = "epoch"
    db["quests"][7]["lastSeenAt"] = 1_600_000_000
    L.Prune(db, 1_600_000_000 + 25 * 86400)         # 同域且真超三个 CD：要清
    if db["quests"][7] is not None:
        return fail("prune_never_mixes_clock_domains", "同域超期应清出，体积清理不能失效")
    ok("prune_never_mixes_clock_domains")


def test_refresh_wake_is_capped():
    """到期时刻再远，唤醒定时器也只排到封顶值：排到几周之后等于矩阵开着再也不更新，
    玩家只能手动点刷新。封顶之后的额外唤醒只多问一次池，比"永远不刷新"便宜。"""
    lua, wqt, _ = sync_env([(153, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute("WQT.Config.DB().quests[153].expiresAt = 1e12 "
                "WQT.Config.DB().quests[153].expiresSrc = '接口倒计时'")
    wqt.Sync.SetPolling(True)
    lua.execute("WQT.Sync.SnapshotNow()")
    delays = lua.execute("local s = '' for _, t in ipairs(WQT_NEWTIMERS) do "
                         "if not t.cancelled then s = s .. tostring(t.delay) .. ',' end end return s")
    if str(delays).strip(",") == "":
        return fail("refresh_wake_is_capped", "没排任何唤醒定时器")
    for part in str(delays).strip(",").split(","):
        if float(part) > 7 * 86400:
            return fail("refresh_wake_is_capped", f"唤醒排到 {part} 秒后，没有封顶")
    ok("refresh_wake_is_capped")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红在"反向跨域同样不该清"；第二条红在 delay 远超封顶。

- [ ] **Step 3: 实现域守卫**

`Ledger.lua` 的 `L.Prune`，把清理判据那段替换（保留原注释全文）：

```lua
function L.Prune(db, now)
    -- now 与 rec.lastSeenAt 必须同域才可比：上个存档留的纪元秒对上本次开机秒，
    -- 差值几十亿，一次快照就能把整张账本清光。同 ThisCycle / WeekStats 那两道守卫。
    local nowN = tonumber(now) or 0
    local nowEpoch = nowN > 1e9
    for questID, rec in pairs(db.quests or {}) do
        local seen = tonumber(rec.lastSeenAt)
        local comparable = seen ~= nil and ((seen > 1e9) == nowEpoch)
        if not rec.ignored and not rec.regKey and comparable
            and nowN - seen > 3 * WEEK then
            db.quests[questID] = nil
        end
    end
    for _, rec in pairs(db.quests or {}) do
        if rec.observations then TrimObservations(rec, rec.currentSig) end
    end
    for _, rows in pairs(db.turnins or {}) do
        for _, list in pairs(rows) do
            while #list > MAX_TURNINS_PER_QUEST do table.remove(list, 1) end
        end
    end
end
```

- [ ] **Step 4: 实现唤醒封顶**

`Sync.lua`，`local refreshTimer = nil` 之前加常量：

```lua
-- 唤醒定时器封顶。到期时刻一旦被写得很远（脏数据、周常那类排程值），
-- 不封顶就等于"矩阵开着也再不会自己更新"；封顶后每 6 小时多问一次池，
-- 只在矩阵开着时发生，比"永远不刷新"便宜，也比玩家反复手点刷新省。
local MAX_WAKE_SECONDS = 6 * 3600
```

`ScheduleNextRefresh` 里的排程行替换：

```lua
    local delay = due - now
    if delay > MAX_WAKE_SECONDS then delay = MAX_WAKE_SECONDS end
    refreshTimer = C_Timer.NewTimer(math.max(5, delay), function()
```

- [ ] **Step 5: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿；确认 `test_closing_matrix_cancels_pending_refresh`（关闭后待触发定时器必须为 0）仍然绿——封顶只改 delay，不新增定时器。

- [ ] **Step 6: 提交**

```bash
git add WorldQuestTracker/Ledger.lua WorldQuestTracker/Sync.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 体积清理先验时钟域，到期唤醒定时器封顶 6 小时

纪元秒对上本次开机秒的差值是几十亿，Prune 不看域就会一次清光整张账本
（ThisCycle/WeekStats 都防了这手，只有它漏了）；到期时刻被写远时，
不封顶等于矩阵开着也不再刷新。
EOF
)"
```

---

### Task 6: 任务记录只有一处形状定义

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（新增 `L.NewQuestRecord` / `L.QuestRecordFields`；替换 `DiffPool` 约 493、`AddQuest` 约 748、`EnsureRegistered` 约 861 三处构造）
- Modify: `WorldQuestTracker/Sync.lua`（若其中有 `db.quests[x] = {...}` 构造，一并换掉）
- Test: `tests/run_wqt_tests.py`

**为什么**：一条任务记录现在散着 28 个字段，由 6 个文件各自写、构造点有 3 处，每处只写 `observations = {}`，字段全靠"谁用到谁加"。这是本轮多个 bug 的共同根因（同一个事实存在多处、读法两套）。不动存储结构，先让**字段清单只有一个出处**，并让离线守卫盯住它。

- [ ] **Step 1: 写 RED 守卫用例**

```python
def test_quest_records_only_come_from_the_factory():
    """任务记录的形状只在 Ledger.NewQuestRecord 一处定义：别处写 db.quests[x] = {…}
    就是又开一份字段清单。字段散落 6 个文件是本轮多个 bug 的共同根因。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "WorldQuestTracker")
    lua, wqt = load_lua("Config", "Ledger")
    fields = lua.execute("""
        local t = {}
        for k in pairs(WQT.Ledger.QuestRecordFields()) do t[k] = true end
        return t
    """)
    declared = set(str(k) for k in fields.keys())
    # 角色行（db.chars[key]）在别处也用 rec. 这个变量名，守卫要把它的字段排除掉
    char_row = {"views", "lastSync", "class", "level"}
    direct, unknown = [], []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua"):
            continue
        src = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), start=1):
            code = line.split("--", 1)[0]        # 只看代码，注释里提字段名不算写入
            if re.search(r"db\.quests\[[^\]]+\]\s*=\s*\{", code):
                direct.append(f"{fn}:{i}")
            for m in re.finditer(r"\brec\.([A-Za-z_][A-Za-z0-9_]*)", code):
                if m.group(1) not in declared and m.group(1) not in char_row:
                    unknown.append(f"{fn}:{i} rec.{m.group(1)}")
    if direct:
        return fail("quest_records_only_come_from_the_factory",
                    "还在直接构造任务记录: " + ", ".join(direct))
    if unknown:
        return fail("quest_records_only_come_from_the_factory",
                    "字段清单缺这些（或那个 rec 不是任务记录，改局部变量名）: "
                    + ", ".join(sorted(set(unknown))))
    ok("quest_records_only_come_from_the_factory")
```
确认文件顶部已 `import os, re`（`test_lua_upvalue_budget` 已在读源码，照它用的那几个名字写，别重复 import）。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 红在"还在直接构造任务记录"（三处），以及 `QuestRecordFields` 不存在导致用例抛错。

- [ ] **Step 3: 实现工厂与清单**

`Ledger.lua`，在 `L.MakeSig` 之前加（清单按当前 6 个文件的实际写入点抄全，注释里说明每个字段归谁写）：

```lua
--- 任务记录的字段清单 = 唯一出处。写这张表而不是散在各处加字段：
--- 同一件事在 5 个字段里各存一份、读法有两套，是本轮金币赖行/状态串轮这批 bug 的共同根因。
--- 守卫用例（test_quest_records_only_come_from_the_factory）逐文件扫 rec.<字段>，
--- 清单外的写法一律报红——要么补进清单（并说清归谁写），要么那个 rec 根本不是任务记录。
local QUEST_RECORD_FIELDS = {
    -- 板面/接口答的
    name = true, mapID = true, mapSrc = true, mapAsked = true, mapManual = true,
    poolSeen = true, onBoard = true, lastSeenAt = true,
    -- 奖励与金币（读法一律走 L.QuestMoney / L.GoldVerdict）
    observations = true, currentSig = true, lastMoney = true,
    goldMoney = true, goldRound = true, goldLock = true,
    -- 轮次与到期
    roundAt = true, roundEnd = true, goneAt = true, highFreq = true,
    expiresAt = true, expiresSrc = true, expiresGuessed = true,
    -- 身份与准入
    regKey = true, kwLogAdded = true, manualAdded = true, ignored = true,
    -- 地图事件 pin 给的归属（"这一轮去哪办"）
    evMapID = true, evName = true, evAt = true,
}

function L.QuestRecordFields() return QUEST_RECORD_FIELDS end

--- 新建一条任务记录。只有这一处能造形状（观测表天生要在，别的字段都按需长）。
function L.NewQuestRecord()
    return { observations = {} }
end
```

把三处构造换成工厂：

- `DiffPool`：`rec = { observations = {} }` → `rec = L.NewQuestRecord()`
- `AddQuest`：同形替换
- `EnsureRegistered`：同形替换

然后跑 `grep -n "db\.quests\[[^]]*\] *= *{" WorldQuestTracker/*.lua`，把剩下的直接构造（若 Sync/Api 里有）一并换成 `L.NewQuestRecord()`；`db.quests[questID] = nil`（清理）不算构造，保留。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。若守卫报出清单外的 `rec.xxx`：先确认那个 `rec` 是不是任务记录 —— 是就补进清单并写归属，不是（比如是 `db.chars` 行）就把那个局部变量改名（例如 `charRow`），**别把字段硬塞进清单**。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Ledger.lua WorldQuestTracker/Sync.lua WorldQuestTracker/Api.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
refactor: 任务记录形状收成一处，字段清单有静态守卫

一条记录 28 个字段散在 6 个文件、构造点三处各写一份，是金币赖行与状态串轮
这批 bug 的共同根因。加 NewQuestRecord/QuestRecordFields 与守卫用例：
清单外的 rec.<字段> 一律报红。
EOF
)"
```

---

### Task 7: 状态判定与金币判定各收成一个入口

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（`L.State` 约 993-1013、`L.RegisteredState` 约 956-969、`L.GoldQuestIDs` 约 1069-1080，新增 `L.GoldVerdict`）
- Test: `tests/run_wqt_tests.py`

**为什么**：同一件"这个号这条任务什么状态"存在两份优先级代码——`L.State`（准入门按**本轮** `RoundAt`）和 `L.RegisteredState`（按**本 CD** `ThisCycle`）。两份的优先级顺序其实完全一样（手动 > 实测交任务 > 本角色快照 > 兜底），只有"证据算不算本轮/本周"这道新鲜度门不同。两份各写 → 改一处忘一处（本轮 Task 4 那个 `last.t` 空值缺陷就只在一份里；另一份用 `ThisCycle` 恰好躲过）。

- [ ] **Step 1: 写 RED 用例**

```python
def test_state_and_registered_state_share_precedence():
    """两份状态判定的优先级必须一致：只有新鲜度门不同（本轮 vs 本 CD）。
    这里用"手动补录被交任务凭据压过"这一条同形断言两处——上一版只有 State 检查
    db.manual 是否存在，注册行那条路径少一层判空，字段清单也各写一份。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(8, 100000, None)]), 1000)
    L.SetManual(db, "E-永恒之井", 8, "open", 1200, nil)
    L.OnTurnIn(db, "E-永恒之井", 8, 1150, 100000, str(db["quests"][8]["currentSig"]))
    if str(L.State(db, "E-永恒之井", 8)) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "世界任务行：手动补录应压过交任务凭据")
    if str(L.RegisteredState(db, "E-永恒之井", 8)) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "注册行：同一份手动凭据却读成别的——两份判定已经漂移")
    ok("state_and_registered_state_share_precedence")


def test_gold_verdict_is_the_single_gold_entry():
    """"这条给不给金币"只有一个口径：本轮读到过（含双模式另一档的粘滞）= 是；
    压根没读到 = nil（UI 显示 ?），绝不当成"客户端说它不给钱"。
    Dump 的"金币任务"节与矩阵行准入过去各判各的（前者只看 obs.gold，后者看粘滞金额），
    同一份账本两处报两个数。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(9, 100000, None)]), 1000)
    if L.GoldVerdict(db, 9) is not True:
        return fail("gold_verdict_is_the_single_gold_entry", "本轮读到金币应判是")
    # 双模式：当前这一档读到非金币，但本轮另一档读到过 → 仍然是金币任务
    db["quests"][9]["goldMoney"] = 100000
    db["quests"][9]["goldRound"] = L.RoundAt(db, db["quests"][9])
    obs_sig = str(db["quests"][9]["currentSig"])
    db["quests"][9]["observations"][obs_sig]["money"] = 0
    db["quests"][9]["observations"][obs_sig]["gold"] = False
    if L.GoldVerdict(db, 9) is not True:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "本轮另一档读到过金币，切档不许判成非金币")
    if len(L.GoldQuestIDs(db)) != 1:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "导出的金币任务节没走同一个口径")
    ok("gold_verdict_is_the_single_gold_entry")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红（`RegisteredState` 读到 `db.manual` 缺表或优先级不同 → 报"注册行…漂移"）；第二条红在 `GoldVerdict` 是 nil（函数还不存在，用例抛错）。

- [ ] **Step 3: 状态判定收口**

`Ledger.lua`，把 `L.State` 整段替换，`L.RegisteredState` 换成一行委托：

```lua
--- 状态判定唯一入口。gate 只决定"哪道新鲜度门"，优先级两边完全一样：
---   "round" = 世界任务，跟这一条实例自己的起点（L.RoundAt）比
---   "cycle" = 常驻周常，跟本周 CD 锚点（db.cycleAnchor）比
--- 优先级：手动补录 > 本轮/本周实测交任务 > 本角色登录快照 > 兜底。
--- 上一版这两份各写一遍，改一处忘一处（last.t 空值缺陷只在一半里）。
--- "unknown" 只在完全没有来源时出现，UI 必须如实显示，不得当成"没做"。
--- 注意 currentSig / 池成员关系只描述"最后一次同步的那个角色"看到的池：世界任务池
--- 是按角色各自的（狩猎模式、个人任务线都会改变它）。所以 currentSig 为空绝不等于
--- "本角色的这个任务结束了"，本角色说得出话时一律以它自己的快照为准。
--- "算不算本轮"只跟 L.RoundAt（这一条实例自己的起点）比，绝不跟奖励签名比：
--- 切一次狩猎模式就翻一次签名，12.1.5 反馈"做过这个任务的，现在标记成了没做过"
--- 就是这么来的（用户 2026-10-25：这个任务并没有结束，并不是新一轮的刷新）。
function L.State(db, charKey, questID, gate)
    local byCycle = gate == "cycle"
    local manual = db.manual and db.manual[charKey]
    local m = manual and manual[questID]
    -- 手动补录是玩家自己敲的：世界任务行不设时间门（他要的就是这一条一直算数），
    -- 注册行按 CD 门（上周点的"已做"不许冒充本周）
    if m and (not byCycle or ThisCycle(db, m.t)) then return m.state end

    local rec = db.quests and db.quests[questID]
    if not rec then return "unknown" end
    local obs = L.GetObs(db, questID)
    local at = L.RoundAt(db, rec)

    local rows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    local lastT = last and tonumber(last.t)
    local fresh
    if byCycle then fresh = lastT ~= nil and ThisCycle(db, lastT)
    else fresh = lastT ~= nil and lastT >= at end
    -- 世界任务行要求"这一条此刻还在池里"（obs）才算实测交任务；
    -- 注册行不要求：锚点那条本来就不靠板面存在。
    if fresh and (byCycle or obs) then return "done" end

    local chars = db.chars and db.chars[charKey]
    local view = chars and chars.views and chars.views[questID]
    if view then
        local vt = tonumber(view.t)
        if byCycle then
            if vt and ThisCycle(db, vt) then return view.s end
        elseif not obs or (vt and vt >= at) then
            -- 快照只在覆盖到当前这一条实例时才算数，否则上一轮的"已交"会跨轮冒充本轮
            return view.s
        end
    end
    if byCycle then return "unknown" end
    return obs and "open" or "ended"
end

--- 注册行读的是"本周 CD 内"的证据；判据与优先级见 L.State（唯一真源）。
function L.RegisteredState(db, charKey, questID)
    return L.State(db, charKey, questID, "cycle")
end
```
删掉原 `L.RegisteredState` 的函数体（`ThisCycle` 那三段落进 `L.State` 的 gate 分支），`ThisCycle` 保留原位。

- [ ] **Step 4: 金币判定收口**

`Ledger.lua`，`L.QuestMoney` 之后加：

```lua
--- "这条给不给金币"的唯一口径。返回 true=本轮读到过金币（含双模式另一档的粘滞，
--- 见 L.QuestMoney），false=客户端明确答过非金币，nil=压根问不出（UI 显示 ?）。
--- 绝不把 nil 折成 false：那等于替客户端回答"它不给钱"，行会当场消失，
--- 而它只是这一轮数据没到（12.x 奖励数据要几十秒才回来）。
function L.GoldVerdict(db, questID)
    local money = L.QuestMoney(db, questID)
    if type(money) == "number" and money > 0 then return true end
    local rec = db and db.quests and db.quests[questID]
    local obs = L.GetObs(db, questID)
    if obs and obs.gold == false then return false end
    if rec and rec.goldMoney then return false end
    return nil
end
```

`L.GoldQuestIDs` 的判据换成走同一个入口（保留排序与返回形状）：

```lua
function L.GoldQuestIDs(db)
    local out = {}
    for questID in pairs(db.quests or {}) do
        -- 走 GoldVerdict，不各写一份"只看 obs.gold"的判据：本轮另一档读到过金币的
        -- 双模式任务，矩阵里有行、导出里却没有，两处报两个数最难查。
        if L.GoldVerdict(db, questID) == true then out[#out + 1] = questID end
    end
    table.sort(out, function(a, b)
        return (db.quests[a].name or "") < (db.quests[b].name or "")
    end)
    return out
end
```

- [ ] **Step 5: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。**这一条是本轮回归风险最高的地方**：`grep -n "RegisteredState\|GoldQuestIDs\|L.State(" tests/run_wqt_tests.py` 找出的每条用例逐条看输出，确认语义变化只在两处：① 注册行的 `db.manual` 判空更严（少一层 nil 崩）；② 导出的金币节现在含粘滞金额。若某条用例断的是"上一轮的手动补录仍算 done"这类**旧行为**，停下来报告，别改判据。

- [ ] **Step 6: 提交**

```bash
git add WorldQuestTracker/Ledger.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
refactor: 状态判定与金币判定各收成一个入口

L.State 与 L.RegisteredState 的优先级本来一样，只有新鲜度门不同（本轮 vs 本 CD），
分成两份写就改一处忘一处（last.t 空值缺陷只在一半里）。现在 gate 选门、
优先级一份。另加 L.GoldVerdict，导出的金币节与矩阵行准入读同一个口径。
EOF
)"
```

### Task 8: 地图来源标签收成常量

**Files:**
- Modify: `WorldQuestTracker/Config.lua`（新增 `Config.MAP_SRC`）
- Modify: `WorldQuestTracker/Ledger.lua`（`LEGACY_MAP_SRC` 约 180、`SUSPECT_MAP_SRC` 约 191、`MANUAL_MAP_SRC` 约 226、`TRUSTED_MAP_SRC` 约 246-254、`NOT_CLIENT_SRC` 约 269-274、`PropagateFamilyMaps` 里的 `"同族成员 "..id`）
- Modify: `WorldQuestTracker/Api.lua`（`RowsToPool` 约 138/141/148 三处字面量）
- Test: `tests/run_wqt_tests.py`

**为什么**：`mapSrc` 是这套账本里最关键的证据标签（决定分组、族搬运、清理、周常归组），但它的取值是散在 3 个文件里的裸字符串。上一轮的"丰饶出现在屠戮者高地""整批行塌进未知地图"都靠这些串判分岔；串打错一个字，判据静默失效，而且没有任何测试能发现。放 Config 而不是 Ledger：Config 在加载顺序里早于所有使用方，纯字符串不碰接口。

- [ ] **Step 1: 写 RED 守卫用例**

```python
def test_map_source_labels_are_constants():
    """mapSrc 取值只有一个出处（Config.MAP_SRC）：这些串决定分组、族搬运、清理、
    周常归组，散成裸字符串打错一个字就静默失效，现有测试一条也发现不了。"""
    lua, wqt = load_lua("Config", "Ledger", "Api")
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "WorldQuestTracker")
    table = lua.execute("return WQT.Config.MAP_SRC")
    labels = set()
    for v in table.values():
        s = str(v)
        if not s.endswith(" "):          # healPrefix/familyPrefix 是前缀，不进精确匹配
            labels.add(s)
    offenders = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua") or fn == "Config.lua":
            continue
        src = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), start=1):
            code = line.split("--", 1)[0]
            for m in re.finditer(r'mapSrc\s*=\s*"([^"]*)"', code):
                if m.group(1) not in labels:
                    offenders.append(f"{fn}:{i} mapSrc 裸串")
            for m in re.finditer(r'"([^"]*池行自带[^"]*|[^"]*玩家指定[^"]*|'
                                 r'[^"]*旧记录未标来源[^"]*|[^"]*存疑：[^"]*)"', code):
                offenders.append(f"{fn}:{i} 来源串写死在判断里")
    offenders = [o for o in offenders if "MAP_SRC" not in o]
    if offenders:
        return fail("map_source_labels_are_constants",
                    "这些标签该走 Config.MAP_SRC: " + ", ".join(sorted(set(offenders))))
    ok("map_source_labels_are_constants")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 红在 `WQT.Config.MAP_SRC` 不存在（用例抛错）。

- [ ] **Step 3: 建常量表**

`Config.lua`，`Config.MinMatrixMoney` 之前加：

```lua
--- 地图来源标签（写进 db.quests[id].mapSrc 的那串字）。唯一出处：这些串决定
--- 分组（Ledger.RowGroupMapID）、族搬运（TRUSTED 白名单）、清理（ClearRetiredMaps
--- 按子串认）、周常归组（ClientMapOf 的黑名单），散成裸字符串打错一个字就静默失效。
--- 放 Config 是因为它在加载顺序里早于所有使用方，且纯字符串、不碰接口。
Config.MAP_SRC = {
    pool = "池行自带",                       -- 池行自己报的图：客户端直答，最硬
    boardList = "跟踪地图清单",
    boardReverse = "地图任务清单反查",
    bountyReverse = "悬赏清单反查",
    logZone = "日志分区",
    manual = "玩家指定",                      -- 第十七轮口述兜底，不当客户端凭据
    legacy = "旧记录未标来源",
    suspect = "存疑：谎报接口写的",
    asked = "询问用的图兜底",                  -- "我问了哪张图"≠"这任务在哪张图"
    healPrefix = "补问 ",                     -- 后接接口全名
    familyPrefix = "同族成员 ",                -- 后接成员 questID
}
```

- [ ] **Step 4: 使用方改读常量**

`Ledger.lua`：三处 `local XXX_MAP_SRC = "..."` 改成

```lua
local MAP_SRC = WQT.Config and WQT.Config.MAP_SRC or {}
local LEGACY_MAP_SRC = MAP_SRC.legacy
local SUSPECT_MAP_SRC = MAP_SRC.suspect
local MANUAL_MAP_SRC = MAP_SRC.manual
```
**注意加载时机**：`Ledger.lua` 在 `Config.lua` 之后加载（见 toc），所以文件级取值是安全的；但离线用例可能只 `load_lua("Ledger")` 不带 Config —— 为稳妥，把这些值改成**运行时取**的小函数，与文件里既有 `L.ManualMapSource()` 的写法一致：

```lua
local function MapSrc(key)
    local M = WQT.Config and WQT.Config.MAP_SRC
    return M and M[key] or ""
end
local function LEGACY_MAP_SRC() return MapSrc("legacy") end
local function SUSPECT_MAP_SRC() return MapSrc("suspect") end
local function MANUAL_MAP_SRC() return MapSrc("manual") end
```
`L.LegacyMapSource()` / `L.SuspectMapSource()` / `L.ManualMapSource()` 三个对外函数改为返回这三个函数的结果（对外签名不变，调用方零改动）。所有引用点同步加括号：`NOT_CLIENT_SRC` 表（约 269-274）改成运行时构造一次：

```lua
--- 客户端答图的**黑名单**（其余都算客户端答的）。用黑名单不用白名单：来源标签
--- 是带后缀的动态串（"同族成员 97625"、"补问 C_QuestLog.GetQuestLogZoneMapID"），
--- 上一版白名单没列全，客户端真答过的图被当成"没凭据"，整批丰饶/特别任务掉进周常组
--- （改完判据时 5 条用例一起红，包括"按板面证据归的图"那条老保证）。
local notClientSrc
local function NOT_CLIENT_SRC()
    if not notClientSrc then
        notClientSrc = {
            [MANUAL_MAP_SRC()] = true,      -- 玩家口述
            [LEGACY_MAP_SRC()] = true,      -- 旧记录没标来源
            [SUSPECT_MAP_SRC()] = true,     -- 已知会谎报的接口写的
            [MapSrc("asked")] = true,        -- "我问了哪张图"≠"这任务在哪张图"
        }
    end
    return notClientSrc
end
```
`L.ClientMapOf` 里 `NOT_CLIENT_SRC[src]` → `NOT_CLIENT_SRC()[src]`。
`TRUSTED_MAP_SRC`（约 246-254）同样收成运行时表：

```lua
--- 族本体搬地图只认这几类凭据——"询问用的图兜底"是我们自己套的，
--- 传过来等于把猜的当成本体的（口径原样保留，只是标签来自 Config.MAP_SRC）。
local trustedSrc
local function TRUSTED_MAP_SRC()
    if not trustedSrc then
        local M = WQT.Config and WQT.Config.MAP_SRC or {}
        trustedSrc = {
            [M.pool] = true,
            [M.boardList] = true,
            [M.boardReverse] = true,
            [M.bountyReverse] = true,
            [M.manual] = true,
            [M.logZone] = true,
            [M.healPrefix .. "C_TaskQuest.GetQuestZoneID"] = true,
        }
    end
    return trustedSrc
end
```
`trustedMapOf` 里 `TRUSTED_MAP_SRC[...]` → `TRUSTED_MAP_SRC()[...]`；`PropagateFamilyMaps` 里 `anchor.mapSrc = "同族成员 " .. tostring(srcID)` → `MapSrc("familyPrefix") .. tostring(srcID)`；`ClearRetiredMaps` 内 `src == LEGACY_MAP_SRC` → `src == LEGACY_MAP_SRC()`；`Sync.lua` 的 `weak()`（约 340）里 `local legacy/suspect/manual` 三个变量改从 `L.LegacyMapSource()` 等对外函数取（它们本来就是函数，别直连字符串）。

`Api.lua` `RowsToPool`：`mapSrc = "池行自带"` → `mapSrc = M.pool`，`"补问 " .. src` → `M.healPrefix .. src`，`"询问用的图兜底"` → `M.asked`，其中 `local M = WQT.Config.MAP_SRC or {}` 放在函数取用的位置（Api 在 Config 之后加载，运行时也在其后）。

- [ ] **Step 5: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。**这批改动碰的是判据本体**：`grep -n "def test_.*map\b\|RetiredMaps\|ClientMapOf\|PropagateFamilyMaps\|WeeklyGroup" tests/run_wqt_tests.py` 找出的用例逐条确认仍绿；任何一条红都要先确认是"我改错了标签来源"，不是"用例该改"。

- [ ] **Step 6: 提交**

```bash
git add WorldQuestTracker/Config.lua WorldQuestTracker/Ledger.lua WorldQuestTracker/Api.lua WorldQuestTracker/Sync.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
refactor: 地图来源标签收成 Config.MAP_SRC，判据读常量不读裸串

mapSrc 决定分组、族搬运、清理、周常归组，取值原来散在 Api/Ledger/Sync 三处
写成字面量。串打错一个字判据就静默失效，加静态守卫用例盯住。
EOF
)"
```

---

### Task 9: 一次性数据升级函数（schema 真用起来）

**Files:**
- Modify: `WorldQuestTracker/Config.lua`（`Init` 约 265、新增 `Config.SCHEMA_VERSION` / `Config.MIGRATIONS` / `Config.Migrate`）
- Modify: `WorldQuestTracker/Ledger.lua`（新增 `L.DropTurninsWithoutEvidence`，紧跟 Task 2 的 `L.QuestKnown`）
- Modify: `WorldQuestTracker/Sync.lua`（删掉 `SnapshotNow` 里那段每轮调 `ResetStickyGoldOnce` 的块，约 723-734）
- Test: `tests/run_wqt_tests.py`

**为什么**：设计文档 §10 写着"版本不符走一次性升级函数；升级失败先备份原表再重建"，实际代码只有 `db.schema = math.max(db.schema or 0, 1)` —— schema **写了从来没读**，也没有 `Migrate`。于是"老存档里的坏数据不自愈"只能塞进 `SnapshotNow` 每次快照都判一遍（`ResetStickyGoldOnce` 里那套 `>= 2` 分档标记就是为绕开这个问题打的补丁）。收成注册表：一次登录一趟、失败留证据、坏数据不复发。

- [ ] **Step 1: 写 RED 用例**

```python
def test_migrate_runs_once_per_schema():
    """schema 升级只在版本落后时跑，跑一次就把档位推上去：
    每轮快照都判一遍 = 每次同步多一趟全账本扫描，而且坏数据修复的语义变成"看运气"。"""
    lua, wqt = load_lua("Config", "Ledger")
    L, C = wqt.Ledger, wqt.Config
    db = lua.globals().WQTDB
    db["quests"] = lua.table_from({})
    db["quests"][3] = lua.table_from({
        "observations": lua.table_from({"-1|": lua.table_from({"money": 100000, "gold": True})}),
        "goldMoney": 100000, "goldRound": 500,
    })
    db["schema"] = 1
    db["stickyGoldReset"] = 1            # 上一版的档位：这一版必须重新清
    ran = lua.execute("return WQT.Config.Migrate(WQT.Config.DB())")
    if ran is not True:
        return fail("migrate_runs_once_per_schema", "Migrate 没跑或返回不对")
    if int(db["schema"]) != int(C.SCHEMA_VERSION):
        return fail("migrate_runs_once_per_schema",
                    f"跑完要推到 SCHEMA_VERSION，实际 {db['schema']}")
    if db["quests"][3]["goldMoney"] is not None:
        return fail("migrate_runs_once_per_schema", "坏金币凭据没被清（迁移没接到这一棒）")
    if lua.execute("return WQT.Config.Migrate(WQT.Config.DB())") is not False:
        return fail("migrate_runs_once_per_schema", "同一档不该再跑第二趟")
    ok("migrate_runs_once_per_schema")


def test_migrate_failure_keeps_ledger_usable():
    """升级函数抛错时：档位停在上一档（下次登录重试同一档）、原始表不重建、留一条日志。
    直接重建等于把用户三个 CD 的金币记录抹掉，比坏数据严重得多。"""
    lua, wqt = load_lua("Config", "Ledger")
    db = lua.globals().WQTDB
    db["schema"] = 1
    lua.execute("WQT.Config.MIGRATIONS[2] = function() error('模拟升级函数抛错') end")
    if lua.execute("return WQT.Config.Migrate(WQT.Config.DB())") is not False:
        return fail("migrate_failure_keeps_ledger_usable", "抛错要报失败")
    if int(db["schema"]) != 1:
        return fail("migrate_failure_keeps_ledger_usable", "失败后档位不该前进")
    if "升级失败" not in str(wqt.Config.GetLogs()):
        return fail("migrate_failure_keeps_ledger_usable", "失败必须留日志")
    ok("migrate_failure_keeps_ledger_usable")


def test_migrate_drops_turnins_with_no_evidence_behind_them():
    """Task 2 之前，交一条普通任务（职业/战役任务）也会写进 turnins —— 那些凭据
    是矩阵六道准入门之一，老存档里已经躺了一批。一次性清掉"账本里查不到可信身份"
    的凭据：清掉的代价是那一行少报一次（下一次上板就自己长回来），
    留着的代价是非金币任务永远赖在矩阵里骗玩家。"""
    lua, wqt = load_lua("Config", "Ledger")
    lua.execute("""
        local db = WQT.Config.DB()
        db.quests[5001] = { poolSeen = true, observations = {} }   -- 有身份：这条要留
        k = "角色|阵营|服"
        db.turnins = { [k] = {
            [5001] = { { t = 10, g = 30000 } },
            [7777] = { { t = 10, g = 0 } },                        -- 账本里根本没这条
        } }
        db.schema = 2
    """)
    if lua.execute("return WQT.Config.Migrate(WQT.Config.DB())") is not True:
        return fail("migrate_drops_turnins_with_no_evidence_behind_them", "v3 没跑")
    if lua.execute("return WQT.Config.DB().turnins[k][7777] ~= nil"):
        return fail("migrate_drops_turnins_with_no_evidence_behind_them",
                    "没身份支撑的交任务凭据没被清")
    if lua.execute("return WQT.Config.DB().turnins[k][5001] == nil"):
        return fail("migrate_drops_turnins_with_no_evidence_behind_them",
                    "有池证据的凭据被误伤（判据不能只看 questID 在不在）")
    ok("migrate_drops_turnins_with_no_evidence_behind_them")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 两条都红在 `Config.Migrate` / `Config.SCHEMA_VERSION` 不存在。

- [ ] **Step 3: 实现注册表**

`Config.lua`，`Config.Init` 之前加：

```lua
--- 存档结构版本与一次性升级。schema 以前只写不读（`math.max(db.schema or 0, 1)`），
--- 设计文档 §10 承诺的"版本不符走一次性升级函数"根本没实现，于是老存档的坏数据
--- 只能每次快照都判一遍（Sync 里那段 ResetStickyGoldOnce 就是这个补丁）。
--- 现在：一次登录一趟、失败停在上一档下次重试、绝不重建原始表
--- ——重建等于抹掉用户几个 CD 的金币记录，比坏数据严重得多。
Config.SCHEMA_VERSION = 3
Config.MIGRATIONS = {
    -- v2：作废旧版本写错的金币凭据（上一轮的金额记进了本轮；判据与代价见
    -- Ledger.ResetStickyGoldOnce 的注释——那一版只清了粘滞，没清来源，所以档位记 2）
    [2] = function(db) return WQT.Ledger and WQT.Ledger.ResetStickyGoldOnce
        and WQT.Ledger.ResetStickyGoldOnce(db) or 0 end,
    -- v3：清掉"账本里查不到可信身份"的交任务凭据。Task 2 之前交一条职业/战役任务
    -- 也会写 turnins，而这份凭据是矩阵准入门之一（能绕过金币门槛占一行）。
    -- 判据与代价见 Ledger.DropTurninsWithoutEvidence 的注释。
    [3] = function(db) return WQT.Ledger and WQT.Ledger.DropTurninsWithoutEvidence
        and WQT.Ledger.DropTurninsWithoutEvidence(db) or 0 end,
}

--- 逐档升级。返回 true=有档被跑过，false=已是最新或失败。
--- 每档失败就停：后面的档依赖前面的形状，跳档跑会把账本弄成半新半旧。
function Config.Migrate(db)
    if not db then return false end
    local from = tonumber(db.schema) or 1
    if from >= Config.SCHEMA_VERSION then return false end
    for v = from + 1, Config.SCHEMA_VERSION do
        local fn = Config.MIGRATIONS[v]
        local okCall, n = pcall(function() return fn and fn(db) or 0 end)
        if not okCall then
            db.schema = v - 1          -- 停在成功的那一档，下次登录重试同一档
            Config.Log(("数据升级失败 v%d（下次登录重试）: %s"):format(v, tostring(n)))
            return false
        end
        Config.Log(("数据升级 v%d 完成（处理 %s 条）"):format(v, tostring(n)))
    end
    db.schema = Config.SCHEMA_VERSION
    return true
end
```

`WorldQuestTracker/Ledger.lua`，紧跟 Task 2 的 `L.QuestKnown` 之后加：

```lua
--- 清掉"账本里查不到可信身份"的交任务凭据（一次性升级 v3 用）。
--- Task 2 之前交一条职业/战役任务也会写进 turnins，而这份凭据是矩阵六道准入门之一
--- ——它能绕过金币门槛，把一条从没上过板的任务永远留在矩阵里。
--- 只清凭据不清任务记录：清掉的代价是那一行少报一次，下一次同步真上板就自己长回来；
--- 记录本身可能带着玩家几个 CD 攒下来的金币观察，删了就回不来了。
function L.DropTurninsWithoutEvidence(db)
    if not db or not db.turnins then return 0 end
    local n = 0
    for _, log in pairs(db.turnins) do
        if type(log) == "table" then
            for questID in pairs(log) do
                if not L.QuestKnown(db, questID) then
                    log[questID] = nil
                    n = n + 1
                end
            end
        end
    end
    return n
end
```

`Config.Init` 里 `db.schema = math.max(db.schema or 0, 1)` 替换成：

```lua
    -- 先把档位补齐到"已知最低"（老到什么字段都没有的存档走这里），再跑升级。
    if (tonumber(db.schema) or 0) < 1 then db.schema = 1 end
```

并在 `Init` 末尾（`Config.Log(("账本就绪 ..."))` 之前）加：

```lua
    -- 一次性升级：这里跑，不在每轮快照里跑（那等于每次同步多一趟全账本扫描）
    Config.Migrate(db)
```

`Sync.lua`：删掉 `SnapshotNow` 里 `if WQT.Ledger and WQT.Ledger.ResetStickyGoldOnce then ... end` 整块（含里面那段注释与"作废 %d 条任务里旧版本写错的金币记录"日志）。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。约 1138/1187 那条老用例（`db["stickyGoldReset"] = 1` 起、断 `stickyGoldReset == 2`）现在走 `Config.Migrate`：把它的触发点从"调 SnapshotNow"改成 `WQT.Config.Migrate(WQT.Config.DB())`，**断言的清理结果一条不许放松**。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Config.lua WorldQuestTracker/Sync.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
feat: 补上一次性数据升级 Config.Migrate，坏金币凭据不再每轮快照重判

schema 以前只写不读，文档承诺的升级函数根本没实现，于是老存档的坏数据
只能在每次快照里重判一遍。改成注册表按档跑：一次登录一趟，失败停在上一档
下次重试，绝不重建原始表。v3 顺手清掉 Task 2 之前造出来的"没身份支撑"的
交任务凭据（只清凭据，不动任务记录）。
EOF
)"
```

---

### Task 10: 小地图拖动与设置面板的两处 UI 缺陷

**Files:**
- Modify: `WorldQuestTracker/Matrix.lua`（`EnsureMapButton` 的 `OnDragStart` 约 1834-1845、`OnDragStop` 约 1846-1849）
- Modify: `WorldQuestTracker/Options.lua`（`OnWheel` 约 65-73、补扫按钮 `OnClick` 约 244-247）
- Test: `tests/run_wqt_tests.py`

**三个缺陷**：
1. `Matrix.lua:1839` `(pcall(Minimap.GetEffectiveScale, Minimap) and Minimap.GetEffectiveScale()) or 1` —— 第二次是**点调用没带 self**；`pcall` 成功时它照样会抛（`attempt to index a nil value`），拖到这一帧就报错，且这句不在任何 pcall 里。
2. 同一段直接用全局 `Minimap`（1836/1839/1848），而 1802 已经用 `Api.Resolve("Minimap")` 拿到 host 并做过取不到就降级的判断。宿主被暴雪改名/挪走时（12.x 有过先例），`EnsureMapButton` 判的是 host、拖动读的是全局 —— host 为 nil 时按钮不建，但一旦建了，拖动仍可能踩到不存在的全局。
3. `Options.lua:65` 注释写"方向与矩阵一致（矩阵用 offY + delta*40）"，但矩阵实际是 `offY = offY - delta * 40`（Matrix.lua:1109，注释明确"这个客户端往下滚轮给的是负 delta，所以是减"）。设置面板用 `+ delta * STEP` → 两个面板滚轮方向相反。

- [ ] **Step 1: 写 RED 用例**

```python
def test_minimap_drag_survives_scale_call():
    """拖动那一帧读小地图缩放必须带 self 且取不到就退回 1：
    上一版 `pcall(Minimap.GetEffectiveScale, Minimap) and Minimap.GetEffectiveScale()`
    后半是点调用没带 self，pcall 成功时它当场抛错，且这句不在任何保护里。
    同时不许再直连全局 Minimap——宿主由 EnsureMapButton 用 Api.Resolve 拿。"""
    lua, wqt, _ = sync_env([(160, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.WQTMatrixButton = None
    lua.execute("Minimap = nil")        # 全局没了，但按钮早先已建好（走 host 上值）
    wqt.Matrix.EnsureMapButton()
    btn = g.WQTMapButton
    if btn is None:
        return fail("minimap_drag_survives_scale_call", "替身里按钮没建起来")
    lua.execute("local ok = pcall(function() "
                "  local d = btn.__scripts['OnDragStart'] "
                "  if d then d(btn) end "
                "  local u = btn.__scripts['OnUpdate'] "
                "  if u then u(btn, 0.1) end "
                "end) return ok")
    logs = str(wqt.Config.GetLogs())
    if "attempt to index" in logs.lower():
        return fail("minimap_drag_survives_scale_call", f"拖动帧抛错:\n{logs}")
    ok("minimap_drag_survives_scale_call")
```

> 这条用例的 `btn.__scripts['OnDragStart']` 写法要照本文件既有 UI 用例的实际形式改（`grep -n "__scripts\['OnDragStart'\]\|click_frame_by_text" tests/run_wqt_tests.py` 找现成写法）。若替身里没有 `WQTMapButton` 全局名，就改成拿 `EnsureMapButton()` 的返回值自己调 `OnDragStart`/`OnUpdate`——**目标是：把全局 `Minimap` 置 nil 后拖动帧不许抛错**。

```python
def test_options_wheel_matches_matrix_direction():
    """两个面板的滚轮方向必须一致：矩阵往下滚=看后面的行（用户实测加 delta 手感是反的），
    滚动条值随之下调（0=顶、越大越靠下），所以设置面板同样要减 delta。"""
    lua, wqt, _ = sync_env([(161, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    wqt.Options.Toggle()
    moved = lua.execute("""
        local f = WQTOptionsFrame
        local sb
        local function walk(node)
            if not node then return end
            for _, c in ipairs(node.__children or {}) do
                if c.__scrollbar then sb = c end
                walk(c)
            end
            return sb
        end
        local s = walk(f)
        if not s then return nil end
        s.__value = 500
        local wheel = s.__scripts and s.__scripts['OnMouseWheel']
        if not wheel then return nil end
        wheel(s, -1)                       -- 往下滚（这个客户端给负 delta）
        return s.__value
    """)
    if moved is None:
        return fail("options_wheel_matches_matrix_direction", "替身里找不到设置面板的滚动条")
    if float(moved) >= 500:
        return fail("options_wheel_matches_matrix_direction",
                    f"往下滚应让内容往后走，滚动条值变了 {moved}")
    ok("options_wheel_matches_matrix_direction")
```

> 第二条依赖替身暴露滚动条对象与 `__value`。落地前先跑 `grep -n "scrollbar\|__value" tests/run_wqt_tests.py` 与 `grep -n "SetVerticalScroll\|scrollbar" WorldQuestTracker/Options.lua` 确认替身形状；**如果替身里根本没有可断言的滚动条**，就把这条用例降级成静态断言：读 `Options.lua` 源码，要求 `OnWheel` 里是 `value - delta` 而不是 `value + delta`（照 `test_lua_upvalue_budget` 的源码扫描写法）。降级要在提交信息里说明。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红在拖动帧抛错（或全局 `Minimap` 为 nil 时抛 `attempt to index`）；第二条红在方向相反。

- [ ] **Step 3: 修 Matrix 拖动**

`Matrix.lua` 的 `OnDragStart` 整段替换（用 `EnsureMapButton` 已有的 `host` 上值，全程 pcall）：

```lua
    button:SetScript("OnDragStart", function(self)
        self:SetScript("OnUpdate", function()
            -- 宿主一律用上值 host，不直连全局 Minimap：EnsureMapButton 判降级用的
            -- 就是 Api.Resolve("Minimap")，读的时候换成全局等于绕过那道判断。
            local okC, cx, cy = pcall(host.GetCenter, host)
            if not okC then return end
            local okS, s = pcall(host.GetEffectiveScale, host)
            local scale = (okS and tonumber(s) and tonumber(s) > 0) and tonumber(s) or 1
            local okP, px, py = pcall(GetCursorPosition)
            if not okP then return end
            local angle = math.atan2((py / scale) - cy, (px / scale) - cx)
            local db = WQT.Config.DB()
            if db then db.minimapAngle = math.deg(angle) end
            PlaceOnMinimapRing(self)
        end)
    end)
```
`OnDragStop` 里的 `tostring(MinimapAngle())`：`MinimapAngle()` 内部若也读全局 `Minimap`，同样改成走 `button:GetCenter()` 一类的既有值；`grep -n "MinimapAngle" WorldQuestTracker/Matrix.lua` 确认它只在有按钮时调用。`PlaceOnMinimapRing` 里若有 `Minimap.` 直连，一并换成上值 host（`grep -n "\bMinimap\." WorldQuestTracker/Matrix.lua` 应该只剩 `Api.Resolve("Minimap")` 那一处字符串）。

- [ ] **Step 4: 修设置面板滚轮 + 补扫按钮判空**

`Options.lua` 的 `OnWheel` 替换：

```lua
-- 滚轮方向与矩阵一致：这个客户端往下滚给的是负 delta，矩阵用 offY - delta*40
-- （Matrix.lua 的 OnMouseWheel，用户实测"加 delta 的手感是反的"）。
-- 滚动条值越大越靠下，所以这里同样减。
local function OnWheel(_, delta)
    if not scrollbar or not scrollbar:IsShown() then return end
    local minV, maxV = scrollbar:GetMinMaxValues()
    local v = (scrollbar:GetValue() or 0) - delta * STEP
    if v < minV then v = minV elseif v > maxV then v = maxV end
    scrollbar:SetValue(v)
end
```
补扫按钮 `OnClick` 里 `pcall(WQT.Sync.HuntKeywordMaps)` 换成同文件别处已经在用的判空写法：

```lua
    huntBtn:SetScript("OnClick", function()
        if not (WQT.Sync and WQT.Sync.HuntKeywordMaps) then
            print("WQT: 同步模块没加载，补扫用不了（/wqt log 看原因）")
            return
        end
        local okCall, done, msg = pcall(WQT.Sync.HuntKeywordMaps)
        print("WQT: " .. tostring(not okCall and "补扫出错: " .. done or msg))
    end)
```

- [ ] **Step 5: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿；`grep -n "def test_.*button\|EnsureMapButton" tests/run_wqt_tests.py` 的按钮用例逐条确认没被 host 改造带崩。

- [ ] **Step 6: 提交**

```bash
git add WorldQuestTracker/Matrix.lua WorldQuestTracker/Options.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 小地图拖动带 self 走 host，设置面板滚轮方向跟矩阵一致

GetEffectiveScale 那点调用没带 self，pcall 成功时后半当场抛错且不在保护里；
拖动帧还直连全局 Minimap，绕过了 EnsureMapButton 的 Api.Resolve 降级判断。
设置面板注释写的"与矩阵一致"其实是反的（矩阵减 delta、面板加 delta）。
EOF
)"
```

### Task 11: 重绘合并（只在同一批更新里少画一次）

**Files:**
- Modify: `WorldQuestTracker/Matrix.lua`（`Mx.Refresh` 约 1752-1754）
- Test: `tests/run_wqt_tests.py`

**为什么**：`Mx.Refresh()` → `Mx.Render()` 每次重画都要走 `L.MatrixRows(db, now, …)`（每行每列问一遍状态、金额、名字并排序），一次快照能连着触发好几处：补读定时器、刷新定时器、交任务结算。帧是池化的（`Matrix.lua:984-1019`），贵的是**排版全量重算**，不是建帧。本轮不做行虚拟化（范围外），只把"同一批更新里的重复重绘"合并成一次。

**代价必须说清**：合并要一个小定时器（`C_Timer.NewTimer`，0.25 秒），它是**一次性**的（不重复、不轮询），只在矩阵开着且真有新重绘请求时存在。这是本轮唯一新增的定时器，若并行有面板用例断"调用后立刻读到新文案"，就得先让首次重绘保持同步。

- [ ] **Step 1: 写 RED 用例**

```python
def test_repaint_coalesces_within_a_burst():
    """同一批更新里连着来几次 Refresh，只重排一次版面：
    一次快照会同时触发补读、到期唤醒、交任务结算三处重绘，
    每次都全量排版（帧池化省的是建帧，不是排版）。
    首次重绘必须同步生效——用户点刷新后要立刻看到新数据。"""
    lua, wqt, _ = sync_env([(170, 30000000)], extra=("Matrix",))
    g = lua.globals()
    wqt.Matrix.Toggle()                       # 打开：立刻画一次
    renders = int(lua.execute("return WQT.Matrix.renderCount or 0"))
    if renders != 1:
        return fail("repaint_coalesces_within_a_burst",
                    f"打开面板应立刻画一次，实际 {renders}")
    for _ in range(4):
        wqt.Matrix.Refresh()                  # 同一批更新里连着要重绘
    after = int(lua.execute("return WQT.Matrix.renderCount or 0"))
    if after != renders:
        return fail("repaint_coalesces_within_a_burst",
                    f"合并没生效：立刻又画了 {after - renders} 次")
    g.WQT_NOW = 2
    lua.globals().WQT_FireTimers()             # 合并窗口到点：补画一次
    done = int(lua.execute("return WQT.Matrix.renderCount or 0"))
    if done != renders + 1:
        return fail("repaint_coalesces_within_a_burst",
                    f"合并窗口到点只该补画一次，实际画到 {done}")
    ok("repaint_coalesces_within_a_burst")
```

> 前提：替身里 `Mx.Render()` 要能被计数。若 `WQT.Matrix.renderCount` 不存在，就在 `Mx.Render()` 第一行加 `WQT.Matrix.renderCount = (WQT.Matrix.renderCount or 0) + 1`（内存计数、零接口调用，也方便 `/wqt` 排查"到底重绘了几次"）；加完确认它不会进 SavedVariables（挂在 `WQT.Matrix` 而不是 `db`）。

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 红在"合并没生效：立刻又画了 4 次"。

- [ ] **Step 3: 实现合并**

`Matrix.lua`，`Mx.Refresh` 替换并把计数器放到文件顶部上值区：

```lua
-- 重绘合并窗口（秒）。0.25 足够把"一次快照带来的多处 Refresh"并成一次排版：
-- 贵的是 MatrixRows 全量重算（每行×每列问状态、金额、名字再排序），不是建帧
-- （帧已经池化，见本文件的取用池）。这是**一次性**定时器，不轮询、面板关了就不存在。
local COALESCE_SECONDS = 0.25
local paintTimer = nil
local lastPaint = -COALESCE_SECONDS   -- 首次必画

--- 立刻重排版面（同步）：这是所有重绘的出口，只在这里记时间戳。
function Mx.RefreshNow()
    if paintTimer then
        pcall(function() paintTimer:Cancel() end)
        paintTimer = nil
    end
    lastPaint = (WQT.Api and WQT.Api.Uptime()) or 0
    WQT.Matrix.renderCount = (WQT.Matrix.renderCount or 0) + 1
    Mx.Render()
end

function Mx.Refresh()
    if not (frame and frame:IsShown()) then return end
    local now = (WQT.Api and WQT.Api.Uptime()) or 0
    if now - lastPaint >= COALESCE_SECONDS then
        Mx.RefreshNow()
        return
    end
    if paintTimer then return end          -- 这一批已经排了补画
    paintTimer = C_Timer.NewTimer(COALESCE_SECONDS, function()
        paintTimer = nil
        if frame and frame:IsShown() then Mx.RefreshNow() end
    end)
end
```
`Mx.Toggle()` 打开分支末尾保持直调 `Mx.Render()`（首绘同步，别改走 Refresh）；`OnHide`/ESC 关闭路径里补一句取消挂起的补画定时器：

```lua
    if paintTimer then pcall(function() paintTimer:Cancel() end) paintTimer = nil end
```

- [ ] **Step 4: 跑测试确认 GREEN（这一步有停止条件）**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。**如果超过 5 条用例变红**（都是"调完 Refresh 立刻断言文案"的形状），说明合并窗口跟现有测试契约冲突：立刻停手，报告红掉的用例清单，让主 agent 决定是"只在同步层合并"还是"接受这批延迟、逐条改用例并加 `WQT_FireTimers()`"。不要为了变绿去放松任何断言。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Matrix.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
perf: 矩阵重绘合并 0.25 秒窗口，一次快照的多处刷新只排版一次

帧已池化，贵的是 MatrixRows 全量重算；补读、到期唤醒、交任务结算会连着要重绘。
首绘保持同步（点刷新立刻看到新数据），合并定时器一次性、面板关了就不存在。
EOF
)"
```

---

### Task 12: 导出层不再自己算一遍账

**Files:**
- Modify: `WorldQuestTracker/Dump.lua`（约 10、23-32、53-80、280-300、355-365）
- Test: `tests/run_wqt_tests.py`

**为什么**：`Dump.Run` 自己写了一份 `STALE_SECONDS`（第 10 行，与 Ledger.lua:1136 同值两份）、自己算 `lastSync`/`stale`（第 74-80 行，`L.MatrixColumns` 已经算好这些），还自己写了第三份"秒数→文字"的 `SpanLeft`（`Config.FormatTimeLeft` 已有一份，只差分钟段）。同一份账本两处报两个数是最难查的错——Ledger 那句注释本来就是这么写的。

- [ ] **Step 1: 写 RED 用例**

```python
def test_dump_uses_the_ledger_for_columns_and_time():
    """导出必须读 Ledger.MatrixColumns 与 Config.FormatTimeLeft，不再自己抄一份判据：
    "快照过期"的阈值和剩余时间的读法在两处各写一遍，改一处就出现矩阵与导出对不上。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "WorldQuestTracker", "Dump.lua"), encoding="utf-8").read()
    stale = []
    if re.search(r"STALE_SECONDS\s*=\s*7\s*\*\s*86400", src):
        stale.append("自己写了一份 STALE_SECONDS")
    if re.search(r"local function SpanLeft", src):
        stale.append("自己写了一份秒数→文字")
    if re.search(r"\(now\s*-\s*lastSync\)\s*>\s*STALE_SECONDS", src):
        stale.append("自己算快照过期")
    if "WQT.Ledger.MatrixColumns" not in src:
        stale.append("列头没走 Ledger.MatrixColumns")
    if stale:
        return fail("dump_uses_the_ledger_for_columns_and_time", "；".join(stale))
    ok("dump_uses_the_ledger_for_columns_and_time")


def test_dump_sections_unchanged_after_dedupe():
    """合并判据后导出的内容口径不许变：这几节本来就是判"账本记的是不是真实情况"的。"""
    lua, wqt, _ = sync_env([(71, 250000), (72, None)], extra=("Dump",))
    g = lua.globals()
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 12
    lua.globals().WQT_FireTimers()
    text = str(wqt.Dump.Run())
    for need in ("本周汇总", "金币任务", "小号一-永恒之井", "完成 1", "未判定"):
        if need not in text:
            return fail("dump_sections_unchanged_after_dedupe",
                        f"导出缺少「{need}」:\n{text}")
    ok("dump_sections_unchanged_after_dedupe")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红（自己抄的那几份都在）；第二条应已绿——它是改动后的护栏，先确认它现在就是绿的。

- [ ] **Step 3: 实现合并**

`Dump.lua`：

1. 删掉 `local STALE_SECONDS = 7 * 86400` 与 `local function SpanLeft`；`WhenEnd` 与 280-300、355-365 三处 `SpanLeft(x)` 全换成 `WQT.Config.FormatTimeLeft(x)`。文案统一成"还有 X" / "已到期（X天前）"这套读法（`FormatTimeLeft` 报"1时2分"，不报分钟以下的位数，够用）。
2. 本周汇总那段（`for i = 1, #chars do ... L.WeekStats ... stale`）改成读列头同一份数据：

```lua
    lines[#lines + 1] = "== 本周汇总 =="
    local cols = L.MatrixColumns(db, now)
    if #cols == 0 then lines[#lines + 1] = "（还没有任何角色同步过）" end
    for i = 1, #cols do
        local c = cols[i]
        -- -1 = 本周统计不可信（时钟域分裂或锚点定不出来），显示 ? 而不是 0。
        -- 判据在 Ledger.MatrixColumns 里，与矩阵列头同源：两处不许报两个数。
        lines[#lines + 1] = ("%s  完成 %s  金币 %s  同步于 %s%s"):format(
            c.key, c.count < 0 and "?" or tostring(c.count),
            WQT.Config.FormatGold(c.gold < 0 and 0 or c.gold),
            tostring(c.lastSync), c.stale and "（快照过期）" or "")
    end
```
3. `Dump.Run` 开头 `local db = WQT.Config.DB()` 之后加一行兜底，账本没就绪时如实说而不是抛错：

```lua
    if not db then return "WQT: 账本还没就绪（登录后再试，详情 /wqt log）" end
```
4. `Dump.Quest` 里"既写日志又打印"那一处：只保留打印（`print`），日志留给真错误。原注释已经说明"聊天窗口会截断"，两条都写只会让 `/wqt log` 里全是重复行。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿；`grep -n "extra=(\"Dump\"" tests/run_wqt_tests.py` 找出的每条 Dump 用例逐条读输出，确认节标题、`?` 口径、`未判定=` 计数都没变。

- [ ] **Step 5: 提交**

```bash
git add WorldQuestTracker/Dump.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
refactor: 导出读 Ledger.MatrixColumns 与 Config.FormatTimeLeft

"快照过期"阈值、本周完成数/金币、剩余时间读法原来在 Dump 各抄了一份，
同一份账本两处算就是最难查的错。清理成同一个出处，并给账本没就绪兜底。
EOF
)"
```

---

### Task 13: 遍历形状与"接口只由 Api 读"这两条纪律

**Files:**
- Modify: `WorldQuestTracker/Api.lua`（新增 `Api.ChildFrames`；约 392 去掉直读 `WQTDB`）
- Modify: `WorldQuestTracker/Track.lua`（`Walk` 约 59-78 改用 `Api.ChildFrames`）
- Modify: `WorldQuestTracker/Config.lua`（约 162 去掉裸 `GetQuestResetTime`）
- Modify: `tests/run_wqt_tests.py`（`EnumerateChildren` 替身形态按真实接口收紧）
- Test: `tests/run_wqt_tests.py`

**三个缺陷**：
1. `Track.Walk`（约 62-71）假设 `EnumerateChildren` 回一个**带 `.Next` 方法的对象**；`Frame:EnumerateChildren()` 在现客户端回的是**迭代器函数**（`local next = f:EnumerateChildren(); child = next()` 那种用法），两种形态都得收，否则这条"快路径"永远走不到（`Callable(it.Next)` 恒假），退回 `GetChildren`；替身里造的正是 `{ Next = ... }` 这个形状（`tests/run_wqt_tests.py:234-238`），所以测试是绿的、真客户端上是死的。
2. `Track.Walk` 的退回路径只认 `{ pcall(frame.GetChildren, frame) }` 的"一个个返回值"形状（`kids[1] == true` 后从 2 开始），而 `Matrix.lua:610-630` 早就记录了"现代客户端 `GetChildren` 回一张表"——所以兜底遍历实际扫不到任何 pin（Shift+点击添加只剩 pinPools 那条快路径活着）。
3. 两处越过分层：`Api.lua:392` 直读全局 `WQTDB`（绕开 `Config.DB()` 的判空与 Init 顺序），`Config.lua:162` 直调全局 `GetQuestResetTime`（注释说"Api 可能在 Config 之后加载"，但 `RefreshCycle` 全部发生在**运行时**，那时所有文件都加载完了；而 12.1.5 实测 `GetQuestResetTime` 答的是**每日**档，绝不该由 Config 绕过 Api 去问）。

- [ ] **Step 1: 写 RED 用例**

```python
def test_child_frames_accepts_both_api_shapes():
    """子帧遍历同时收两种真实形态：EnumerateChildren 回迭代器函数（现客户端）
    与 GetChildren 回一张表（现客户端）/ 一个个返回值（老客户端）。
    上一版 Track 只认"带 .Next 的对象"，那条快路径在真客户端上永远走不到，
    兜底又只认多返回值形状 —— 于是整棵帧树扫不到 pin，Shift+点添加只剩 pinPools 活着。"""
    lua, wqt, g = track_env()
    got = lua.execute("""
        local f = CreateFrame("Frame", "WQKShapeA", UIParent)
        for i = 1, 3 do CreateFrame("Button", "WQKShapeA" .. i, f) end
        -- 形态①：EnumerateChildren 回迭代器函数
        f.EnumerateChildren = function(self)
            local kids, i = self.__children or {}, 0
            return function() i = i + 1 return kids[i] end
        end
        local n = 0
        for _, c in ipairs(WQT.Api.ChildFrames(f) or {}) do n = n + 1 end
        -- 形态②：GetChildren 回一张表
        f.EnumerateChildren = nil
        f.GetChildren = function(self) return self.__children or {} end
        local m = 0
        for _, c in ipairs(WQT.Api.ChildFrames(f) or {}) do m = m + 1 end
        return n * 100 + m
    """)
    if int(got) != 303:
        return fail("child_frames_accepts_both_api_shapes",
                    f"两种形态都该拿到 3 个子帧，实际 n,m = {int(got) // 100},{int(got) % 100}")
    ok("child_frames_accepts_both_api_shapes")


def test_only_api_reads_data_globals():
    """数据接口只由 Api 读、存档只由 Config.DB() 取：绕开这两条就绕开了
    pcall 容错、候选名单与判空（Api.lua 直读 WQTDB、Config.lua 直调 GetQuestResetTime
    就是这两条纪律各漏的一处）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "WorldQuestTracker")
    bad = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua"):
            continue
        src = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), start=1):
            code = line.split("--", 1)[0]
            if fn != "Config.lua" and re.search(r"\bWQTDB\b", code):
                bad.append(f"{fn}:{i} 直读 WQTDB（改用 WQT.Config.DB()）")
            if fn != "Api.lua" and re.search(r"\bGetQuestResetTime\b", code):
                bad.append(f"{fn}:{i} 直调数据接口（改用 WQT.Api.ResetTime()）")
    if bad:
        return fail("only_api_reads_data_globals", "；".join(bad))
    ok("only_api_reads_data_globals")
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `python tests/run_wqt_tests.py`
Expected: 第一条红在 `WQT.Api.ChildFrames` 不存在；第二条红在 `Api.lua:392` 与 `Config.lua:162` 两处。

- [ ] **Step 3: Api 收子帧两种形态**

`Api.lua`，`Api.FrameVisible` 附近加：

```lua
--- 一个帧的子帧列表（两种真实形态都收）。
--- 现代客户端 EnumerateChildren 回**迭代器函数**、GetChildren 回**一张表**；
--- 老客户端/离线替身是一个个返回值。只认其中一种的代码会在真客户端上静默扫不到
--- 任何东西（Track 的兜底遍历就是这个形状：`Callable(it.Next)` 恒假）。
--- 返回新表只在真需要遍历时造；迭代器那条路不建中间表。
function Api.ChildFrames(frame)
    if not frame then return nil end
    if Api.Callable(frame.EnumerateChildren) then
        local okIt, it = pcall(frame.EnumerateChildren, frame)
        if okIt and Api.Callable(it) then
            local out = {}
            while #out < 2000 do
                local okN, kid = pcall(it)
                if not okN or kid == nil then break end
                out[#out + 1] = kid
            end
            return out
        end
        -- 带 Next 的对象形态：本资料片没实测过，收着但绝当唯一路径
        if okIt and type(it) == "table" and Api.Callable(it.Next) then
            local out = {}
            while #out < 2000 do
                local okN, _, kid = pcall(it.Next, it)
                if not okN or kid == nil then break end
                out[#out + 1] = kid
            end
            return out
        end
    end
    if not Api.Callable(frame.GetChildren) then return nil end
    local call = { pcall(frame.GetChildren, frame) }
    if not call[1] then return nil end
    local out = {}
    for i = 2, #call do
        local v = call[i]
        if type(v) == "table" then
            if Api.Callable(v.GetName) then
                out[#out + 1] = v                     -- 是个帧：多返回值形态
            else
                for _, f in pairs(v) do               -- 是一张表：现代形态
                    if type(f) == "table" then out[#out + 1] = f end
                end
            end
        end
    end
    return out
end
```
同时把 `Api.lua:392` 的 `local db = WQTDB` 改成 `local db = WQT.Config and WQT.Config.DB()`（`MidnightMapIDs` 运行时调用，那时 Config 早已 Init；取不到就照现有分支走"没记住的 ID"）。

- [ ] **Step 4: Track 用同一份遍历**

`Track.lua` 的 `Walk` 整段替换（`Callable` 那个本地函数保留原样，或直接改用 `WQT.Api.Callable`）：

```lua
--- 子帧遍历一律走 Api.ChildFrames：迭代器函数那条快路径才有形状可走（本文件上一版
--- 认的是"带 .Next 的对象"，真客户端不这么回，于是这趟遍历实际一直在跑退路径；
--- 而退路径又只认多返回值形状，现代客户端回一张表 → 整棵帧树扫不到 pin，
--- Shift+点添加只剩 pinPools 那条快路径活着）。
--- 深度上限 10 层防环；世界地图那棵树几千帧，一次登录只扫一两趟，别加轮询。
local function Walk(frame, depth)
    if not frame or depth > 10 then return end
    HookPin(frame)
    local kids = WQT.Api and WQT.Api.ChildFrames(frame)
    if not kids then return end
    for i = 1, #kids do Walk(kids[i], depth + 1) end
end
```
并把 `tests/run_wqt_tests.py` 里替身的 `EnumerateChildren`（约 234-238）改成回**迭代器函数**（真客户端形态），保留一个单独的替身帧测"带 Next 的对象"与"GetChildren 回一张表"两种退路。`test_track_walk_prefers_child_iterator` 的断言不变（仍验"有 EnumerateChildren 时不建 GetChildren 临时表"），只是替身形态照真实接口改 —— **这是改替身去贴合客户端，不是改断言迁就实现**。

`Config.lua:162` 那段裸 `GetQuestResetTime` 兜底替换：

```lua
        if type(resetTime) ~= "number" then
            -- 运行时所有文件都加载完了（RefreshCycle 只在 ADDON_LOADED 之后被叫），
            -- 所以这里问 Api 就够：Config 自己调全局等于绕过 Api 的候选名单与容错，
            -- 而 Api.ResetTime 里已经记了 12.1.5 实测"它答的是每日档，只配当对照"。
            resetTime = (WQT.Api and WQT.Api.ResetTime) and WQT.Api.ResetTime() or nil
        end
```

- [ ] **Step 5: 跑测试确认 GREEN**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿。`test_track_hook_registers_once` / `test_track_scan_skips_hidden_map` / `test_track_walk_prefers_child_iterator` 三条逐个确认；若某条红是因为替身改成迭代器函数后 `__enumCalls` 计数变了，改替身计数的位置（`EnumerateChildren` 被调用时 +1），**别把断言放宽成"两种路径都算过"**。

- [ ] **Step 6: 提交**

```bash
git add WorldQuestTracker/Api.lua WorldQuestTracker/Track.lua WorldQuestTracker/Config.lua tests/run_wqt_tests.py
git commit -m "$(cat <<'EOF'
fix: 子帧遍历收两种真实形态，越层读全局的两处收回 Api/Config

Track 认的"带 .Next 的对象"真客户端不回（它回迭代器函数），退路径又只认
多返回值，于是兜底遍历整棵帧树扫不到 pin；替身造的正是那个不存在的形状，
所以测试一直绿。另收 Api 直读 WQTDB、Config 直调 GetQuestResetTime 两处越层。
EOF
)"
```

---

### Task 14: 文档勘误

**Files:**
- Modify: `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`（§9 模块清单、§10 错误处理与升级口径）
- Modify: `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`
- Modify: 本计划所在目录下的实施记录（新增一节"已完成/未完成"）

- [ ] **Step 1: 改 §9 模块清单**

现状：§9 列的是 `Core/Detect/Sync/Matrix/MapOverlay/Tooltip/Ledger/LogWindow/Commands`，实际是 `Core/Config/Ledger/Api/Probe/LogWindow/Sync/Dump/Track/Matrix/Options`（`Detect`、`MapOverlay`、`Tooltip` 从未独立成文件，`Commands` 在 `Core.lua` 里）。清单不照实，下一个读文档的人（或 subagent）会照不存在的模块找代码。把 §9 那段列表替换成实际 11 个文件，每行写"这个文件负责什么 + 不做什么"，例如：

```markdown
- `Sync.lua`：编排与时序（何时抓池、去抖、钱窗、注册族刷新）。不直接读任何 WoW 接口。
- `Api.lua`：唯一读 WoW 数据接口的层（候选名单 + pcall + 降级留痕）。不做判定、不写账本。
- `Ledger.lua`：纯数据层（池 diff、轮次身份、状态/金币判据、矩阵视图）。不碰任何接口，
  时间戳一律由调用方传入，因此可在 lupa 里离线跑。
- `Track.lua`：地图 pin 上 Shift+左键添加任务（pinPools 快路径 + 帧树兜底）。
- `Probe.lua` / `Dump.lua`：取证导出。阶段二矩阵没做之前，账本对不对只看这两份文本。
```

- [ ] **Step 2: 改 §10 升级口径**

§10 现在写的"版本不符走一次性升级函数；升级失败先备份原表再重建"在 Task 9 之前的代码里**根本没实现**（`db.schema` 只写不读）。Task 9 之后前半句成立、后半句**不成立**（我们故意不重建：重建等于抹掉用户几个 CD 的金币记录）。把这句改成实际口径：

```markdown
- 存档结构版本 `db.schema` 与一次性升级函数 `Config.Migrate`（按档注册在 `Config.MIGRATIONS`）。
- 升级失败：**停在上一档、下次登录重试同一档**，并写一条日志。绝不重建原始表——
  抹掉几个 CD 的金币记录比坏数据严重得多；坏数据由对应档的升级函数自己清（如 v2 清金币凭据）。
- 判据本身报不出时一律降级显示（`?` / 未判定计数），不猜。
```

- [ ] **Step 3: 改阶段一验收清单**

`docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 里跟本轮结论冲突的三条：
- "登录不抓池（防卡顿）"→ 改成"登录抓一次（默认开，设置里可关），之后矩阵没开就完全不抓池"。
- "重画走 `Mx.Refresh`（即时）"→ 补一句"同一批更新里的重复重绘合并 0.25 秒，首绘同步"。
- 验收判据里"已建/降级/无 mapID 三条"补第四条：**"Shift+左键添加在 pinPools 与帧树兜底两路都实测过"**（本轮 Task 13 之后兜底那路才真的活着，之前只有快路径）。

- [ ] **Step 4: 跑全套 + 静态检查**

Run: `python tests/run_wqt_tests.py`
Expected: 全绿，末行条数 = 起始 238 + 本轮新增（以实跑输出为准）。

- [ ] **Step 5: 提交**

```bash
git add docs/superpowers/specs/2026-10-21-worldquesttracker-design.md \
  docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md \
  docs/superpowers/plans/2026-10-25-wqt-code-quality-fixes.md
git commit -m "$(cat <<'EOF'
docs: 模块清单照实、升级口径改成"停在上一档重试"、验收加兜底遍历一条

§9 列的 Detect/MapOverlay/Tooltip/Commands 从来没独立成文件，读文档的人会找
不存在的代码；§10 承诺的"升级失败先备份再重建"根本没实现，Task 9 之后我们
故意不重建（抹掉几个 CD 的金币记录比坏数据严重）。
EOF
)"
```

---

## 实施顺序与执行注意

1. Task 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14，**串行**，不要并行：所有任务都改同一个 `tests/run_wqt_tests.py`，两个 subagent 同时写必然互相覆盖。
2. 每个任务开始时把该任务全文交给一个 fresh subagent（不给上下文，它只读计划），完成后主 agent 做三件事：`git diff HEAD~1` 逐行看、跑 `python tests/run_wqt_tests.py`、确认 `git status --short` 里没有 `AHSnipe/` 与 `tests/run_tests.py` 的内容被牵连提交。
3. 任何"要不要放松一条断言/要不要顺手改判据"的冲动 → 停下来报告。这份计划里的判据每一条都有实测出处，subagent 看不到那些实测。
4. 12.1.5 游戏内实测只能由玩家回传（截图 + `/wqt dump` + `/wqt probe`）。Task 1/11/13 三条属于"离线测得出、真机手感要实测"，实施完后需要一次游戏内验收：登录耗时、矩阵开着做任务卡不卡、Shift+点地图 pin 能不能添加任务。

## 自检（写完后回看）

- **覆盖**：评审结论里同步层 4 个缺陷 = Task 1/2/3/4（+5 的唤醒封顶）；数据模型收敛 = Task 6/7/8/9；UI/取证小修 = Task 10/11/12/13；文档 = Task 14。AHSnipe 的结论**故意不在**本轮（见"本轮不做"）。
- **占位符**：Task 1 Step 7、Task 4 Step 1、Task 10 Step 1、Task 11 Step 1 四处留了"落地前先 grep 确认替身形状/照现成写法"的说明——它们不是 TBD，是**明确的条件分支**（两个可选做法 + 选错就停手报告）。实施时必须在提交前把这四处收敛成一个做法，不许把两种都留在仓库里。
- **类型一致**：`L.State(db, charKey, questID, gate)` 的 gate 只认 `"cycle"`，其余一律按 `"round"`（Task 7）；`L.GoldVerdict` 返回 `true/false/nil` 三态（Task 7）；`Config.MAP_SRC` 的 key 名在 Task 8 的 Ledger/Api 两侧必须完全一致（`healPrefix`/`familyPrefix` 带尾空格，其余是完整串）；`Config.Migrate` 返回 boolean（Task 9）；`Api.ChildFrames` 返回表或 nil（Task 13）。



