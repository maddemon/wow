# WorldQuestTracker 阶段一（探测 + 账本 + 同步）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可安装、可离线单测的 WorldQuestTracker 骨架：在真实游戏里抓世界任务池并 diff、按实测钱变化记交任务与金币、按周 CD 汇总每角色完成数与金币总额，并输出 12.x 接口探测报告。

**Architecture:** 四层，依赖单向。`Ledger.lua` 是纯数据逻辑（池 diff、奖励签名、状态、周统计、清理），不碰 WoW API 与 UI，时间戳全部由调用方传入，可离线单测；`Api.lua` 是插件里唯一读 WoW 世界任务/奖励/完成状态接口的地方，每个候选接口 `pcall` 包裹、失败返回原因、降级原因集中记录；`Sync.lua` 只做编排（登录快照、去抖、交任务事件 + 15 秒钱差窗口）；`Dump.lua` 是只读文本导出，用于在没有 UI 的阶段一验证账本真伪。矩阵窗口/地图气泡/tooltip 全在阶段二。

**Tech Stack:** 零依赖纯 Lua（WoW 12.x 插件）；Python 3 + lupa 离线单测。

## Global Constraints

- 设计依据：`docs/superpowers/specs/2026-10-21-worldquesttracker-design.md`。本计划实现 spec 第 4、5、6、7、9、10、12 节；第 8 节（UI）属阶段二，见文末。
- 插件目录 `WorldQuestTracker/`，与 `AHSnipe/` 并列。`.toc` 必须含 `## Interface: 120105,120100` 与 `## SavedVariables: WQTDB`。
- 零依赖纯 Lua；复制到 `_retail_/Interface/AddOns/` 即可用，不引入任何库、不依赖其它插件。
- 任何 12.x API 必须 `pcall` 包裹：接口缺失或报错 → 该功能降级为"未知"，主流程绝不报错，原因记入 `Api.LastDegrade` 并写日志。不按记忆断言 API 形态（仓库约定 `feedback-addon-debugging`）。
- 判定接口"可调用"一律用真值（`if not fn then` / `Api.Callable(fn)`），**不要**用 `type(fn) == "function"`。原因已实测：游戏里 WoW 内置函数 type 确实是 `"function"`，但离线测试里 Python 回调被 lupa 包成 `"userdata"`，用 type 判会把可用的回调当成缺失，测试直接跑不通。
- **取不到的数值不得当成 0**：`money = nil`（奖励接口不可用）= "未判定"，`g = nil`（钱差测量作废）= "未知"。两者都不能写成 0，否则会把金币任务误判为非金币任务、把未测金币误报成没赚钱。
- 不写死星期几。周 CD 锚点 = 服务端 `GetQuestResetTime() − 604800`，只前进不回退。
- 金币数额只用实测钱变化（交任务前后 `GetMoney()` 差），不查奖励接口。
- 手动补录必须点击操作，禁止斜杠命令输入任务名/数额。本阶段只开放 `/wqt`、`/wqt probe`、`/wqt sync`、`/wqt dump`、`/wqt log`。
- 单测文件独立新建 `tests/run_wqt_tests.py`。**不修改** `tests/run_tests.py`（其它并行会话正在编辑它）。
- 共享 worktree：提交前先 `git status --short`；`git add` 只加本任务文件；`git commit` 必须带 pathspec 限定。禁止 `git add -A` / `git commit -a`。
- Lua 表全部 1-based。lupa 侧取 Lua 数组长度用 Python `len(luaTable)`，**不要用 `.n()`**（lupa 的 LuaTable 没有该方法）；按下标取值用 `luaTable[1]`。pairs 语义的散列表不要用 Python 推导式遍历，改调用计划提供的 Lua 侧计数函数（`Ledger.CountObs`、`Ledger.State`）。

---

### Task 1: 插件骨架（toc、Core、Config、命令注册表）

**Files:**
- Create: `WorldQuestTracker/WorldQuestTracker.toc`
- Create: `WorldQuestTracker/Core.lua`
- Create: `WorldQuestTracker/Config.lua`
- Create: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - 全局命名空间 `WQT`（table）；子模块挂载点 `WQT.Config`、`WQT.Ledger`、`WQT.Api`、`WQT.Sync`、`WQT.Dump`
  - `WQT.RegisterCommand(name, fn, help)` 注册 `/wqt <name>` 子命令，注册表为 `WQT.commands[name] = { fn, help }`
  - `WQT.frame`：由 Core 创建的事件帧，`OnEvent` 处理器（测试用 `WQT.frame:GetScript("OnEvent")` 驱动事件）
  - `WQT.Config.DB() -> table`（返回 `WQTDB` 根表）
  - `WQT.Config.Init()`：建库 + schema，幂等
  - `WQT.Config.RefreshCycle() -> boolean`：锚点前进了返回 true
  - `WQT.Config.CycleAnchorOf(resetTime) -> number|nil` = `resetTime - 604800`
  - `WQT.Config.Log(msg)` / `WQT.Config.GetLogs() -> string`（环形保留最近 200 条）
  - `WQT.Config.FormatGold(copper) -> string`：`12345` 铜 → `"1g23s45c"`（1 金 = 100 银 = 10000 铜）；`nil` → `"?"`

- [ ] **Step 1: 写测试脚手架与失败测试**

创建 `tests/run_wqt_tests.py`。脚手架必须**先注入 WoW mock 再加载模块**（Core.lua 顶层会调用 `CreateFrame`）：

```python
# WorldQuestTracker 阶段一离线单测：纯数据逻辑 + API 适配降级 + 同步时序
# 用法：python tests/run_wqt_tests.py            —— 运行单测
#       python tests/run_wqt_tests.py --syntax    —— 额外对插件全部 Lua 做 loadfile 语法检查
import os
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "WorldQuestTracker")
FAILED = []


def fail(name, msg):
    FAILED.append(name)
    print(f"FAIL {name}: {msg}")


def ok(name):
    print(f"PASS {name}")


# WoW 环境替身：只提供本阶段真正用到的入口。
# C_Timer.After 不自动执行，测试用 WQT_FireTimers() 手动推进（配合 WQT_NOW），
# 否则每个用例都要真等 2 秒/15 秒。delay 未到期的定时器必须留在队列里。
WQT_MOCK_ENV = r"""
UIParent = {}
WQT_LOG = {}
WQT_NOW = 0
C_Timer = { After = function(delay, fn) table.insert(WQT_LOG, { delay = delay, fn = fn }) end }
function WQT_FireTimers()
    local pending, fired = WQT_LOG, 0
    WQT_LOG = {}
    for i = 1, #pending do
        if pending[i].delay <= WQT_NOW then
            fired = fired + 1
            pending[i].fn()
        else
            table.insert(WQT_LOG, pending[i])
        end
    end
    return fired
end
function GetTime() return WQT_NOW or 0 end
SlashCmdList = {}
SLASH_WQT1 = nil
function print(...) return ... end
"""


def load_lua(*names, mock=False):
    """按 toc 顺序加载 Lua 文件。mock=True 时先注入 WoW 替身（含 CreateFrame）。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    if mock:
        lua.execute(WQT_MOCK_ENV)
        lua.execute(r"""
        function CreateFrame(frameType, name, parent, template)
            return { __events = {}, __scripts = {}, __shown = false,
                     RegisterEvent = function(self, e) self.__events[e] = true end,
                     UnregisterEvent = function(self, e) self.__events[e] = nil end,
                     SetScript = function(self, k, fn) self.__scripts[k] = fn end,
                     GetScript = function(self, k) return self.__scripts[k] end,
                     IsShown = function(self) return self.__shown end,
                     Show = function(self) self.__shown = true end,
                     Hide = function(self) self.__shown = false end }
        end
        """)
    # 命名空间：游戏里由 Core.lua 的 vararg 提供，测试里预置空表
    lua.execute(r"""
        WQT = WQT or {}
        WQT.commands = {}
        function WQT.RegisterCommand(name, fn, help) WQT.commands[name] = { fn = fn, help = help or "" } end
    """)
    for name in names:
        with open(os.path.join(PLUGIN_DIR, name + ".lua"), encoding="utf-8") as f:
            lua.execute(f.read())
    return lua, lua.globals().WQT


def syntax_check():
    """WoW 专有全局在 loadfile 阶段不参与解析，所以这一步只抓语法错误。"""
    for name in sorted(os.listdir(PLUGIN_DIR)):
        if not name.endswith(".lua"):
            continue
        lua = LuaRuntime()
        # lupa 单值返回不套 tuple，成功时 loadfile 只回一个 chunk；失败时才回 (nil, err)。
        result = lua.globals().loadfile(os.path.join(PLUGIN_DIR, name).replace("\\", "/"))
        if isinstance(result, tuple):
            chunk, err = result
        else:
            chunk, err = result, None
        if chunk is None:
            fail("syntax:%s" % name, err)
        else:
            ok("syntax:%s" % name)


def test_config_init_builds_schema():
    """空库 Init 后建齐四张表并写入 schema 与 cycleAnchor。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    g.GetQuestResetTime = lambda: 1200000
    wqt.Config.Init()
    db = g.WQTDB
    for key in ("quests", "chars", "turnins", "manual"):
        if db[key] is None:
            return fail("config_init_builds_schema", f"{key} 表未建立")
    if int(db["schema"]) != 1:
        return fail("config_init_builds_schema", f"schema={db['schema']}, 期望 1")
    if int(db["cycleAnchor"]) != 1200000 - 604800:
        return fail("config_init_builds_schema", f"cycleAnchor={db['cycleAnchor']}")
    ok("config_init_builds_schema")


def test_config_init_idempotent():
    """重复 Init 不得清空已有账本。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    g.GetQuestResetTime = lambda: 1200000
    wqt.Config.Init()
    g.WQTDB["quests"][42] = lua.table_from({"name": "测试任务"})
    wqt.Config.Init()
    if g.WQTDB["quests"][42] is None:
        return fail("config_init_idempotent", "重复 Init 丢失已有任务记录")
    ok("config_init_idempotent")


def test_cycle_anchor_rolls_over():
    """重置时间前移（进入新 CD）→ 锚点更新；未到期 → 不回退。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({"cycleAnchor": 100})
    g.GetQuestResetTime = lambda: 700000 + 604800
    if wqt.Config.RefreshCycle() is not True:
        return fail("cycle_anchor_rolls_over", "新 CD 应返回 true")
    if int(g.WQTDB["cycleAnchor"]) != 700000:
        return fail("cycle_anchor_rolls_over", f"锚点应为 700000，实际 {g.WQTDB['cycleAnchor']}")
    g.GetQuestResetTime = lambda: 100 + 604800
    wqt.Config.RefreshCycle()
    if int(g.WQTDB["cycleAnchor"]) != 700000:
        return fail("cycle_anchor_rolls_over", "未到期不应回退锚点")
    ok("cycle_anchor_rolls_over")


def test_cycle_anchor_survives_missing_api():
    """GetQuestResetTime 缺失时沿用旧锚点，不报错。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({"cycleAnchor": 555})
    if wqt.Config.RefreshCycle() is not False:
        return fail("cycle_anchor_survives_missing_api", "接口缺失应返回 false")
    if int(g.WQTDB["cycleAnchor"]) != 555:
        return fail("cycle_anchor_survives_missing_api", "锚点不应被改")
    ok("cycle_anchor_survives_missing_api")


def test_format_gold():
    _, wqt = load_lua("Config")
    # 1 金 = 100 银 = 10000 铜
    if str(wqt.Config.FormatGold(12345)) != "1g23s45c":
        return fail("format_gold", f"12345 铜应显示 1g23s45c，实际 {wqt.Config.FormatGold(12345)}")
    if str(wqt.Config.FormatGold(123450)) != "12g34s50c":
        return fail("format_gold", f"123450 铜应显示 12g34s50c，实际 {wqt.Config.FormatGold(123450)}")
    if str(wqt.Config.FormatGold(None)) != "?":
        return fail("format_gold", "nil 必须显示 ?（未知），不能显示 0")
    if str(wqt.Config.FormatGold(0)) != "0g0s0c":
        return fail("format_gold", "0 应显示 0g0s0c（真的没赚钱）")
    ok("format_gold")


def test_log_ring_buffer():
    """日志超过 200 条丢最旧。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    for i in range(1, 206):
        wqt.Config.Log("行%d" % i)
    logs = g.WQTDB["logs"]
    if len(logs) != 200:
        return fail("log_ring_buffer", f"日志条数={len(logs)}, 期望 200")
    text = str(wqt.Config.GetLogs())
    if "行205" not in text or "行1 " in text:
        return fail("log_ring_buffer", "应保留最新 200 条")
    ok("log_ring_buffer")


if __name__ == "__main__":
    tests = [
        test_config_init_builds_schema,
        test_config_init_idempotent,
        test_cycle_anchor_rolls_over,
        test_cycle_anchor_survives_missing_api,
        test_format_gold,
        test_log_ring_buffer,
    ]
    for t in tests:
        t()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: `FileNotFoundError: ...WorldQuestTracker/Core.lua`

- [ ] **Step 3: 写 .toc**

创建 `WorldQuestTracker/WorldQuestTracker.toc`：

```
## Interface: 120105,120100
## Title: WorldQuestTracker 世界任务金币账本
## Notes: 多角色世界任务金币记录。登录后自动抓世界任务池并 diff，交任务时按实测钱变化记账（含随后开宝箱的金币），按周 CD 汇总每个角色的完成数与金币总额。金币判定自动为主、手动锁定兜底；查看用任务×角色矩阵与地图气泡头像
## Author: self
## Version: 0.1.0
## SavedVariables: WQTDB

Core.lua
Config.lua
```

- [ ] **Step 4: 写 Core.lua**

创建 `WorldQuestTracker/Core.lua`：

```lua
-- 插件入口：命名空间、/wqt 子命令注册表、事件分发。
-- 命名空间在测试里已由 mock 预置，游戏里由 WoW 传入的 vararg 提供。

local ADDON_NAME, NS = ...
WQT = WQT or NS or {}
_G.WQT = WQT
WQT.addonName = ADDON_NAME

if not WQT.commands then WQT.commands = {} end

function WQT.RegisterCommand(name, fn, help)
    WQT.commands[name] = { fn = fn, help = help or "" }
end

local function PrintHelp()
    print("WQT 命令：" .. table.concat((function()
        local out = {}
        for name in pairs(WQT.commands) do out[#out + 1] = name end
        table.sort(out)
        return out
    end)(), " | "))
end

local function OnSlash(text)
    local sub = (text or ""):match("^(%S*)")
    if sub == "" then return PrintHelp() end
    local cmd = WQT.commands[sub]
    if not cmd then return PrintHelp() end
    local okCall, err = pcall(cmd.fn, (text or ""):match("^%S+%s*(.*)$"))
    if not okCall then
        WQT.Config.Log(("/wqt %s 出错: %s"):format(sub, tostring(err)))
        print("WQT: /wqt " .. sub .. " 出错，详情 /wqt log")
    end
end

SLASH_WQT1 = "/wqt"
SlashCmdList["WQT"] = OnSlash

WQT.frame = CreateFrame("Frame", "WQTFrame", UIParent)
WQT.frame:RegisterEvent("ADDON_LOADED")
WQT.frame:RegisterEvent("PLAYER_LOGIN")
WQT.frame:SetScript("OnEvent", function(self, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then
            local okCall, err = pcall(WQT.Config.Init)
            if not okCall then print("WQT 初始化失败: " .. tostring(err)) end
        end
    elseif event == "PLAYER_LOGIN" then
        WQT.frame:RegisterEvent("PLAYER_ENTERING_WORLD")
    elseif event == "PLAYER_ENTERING_WORLD" then
        -- 世界任务池登录后数秒才就绪，交给 Sync 去抖
        if WQT.Sync and WQT.Sync.OnWorldEnter then
            local okCall, err = pcall(WQT.Sync.OnWorldEnter)
            if not okCall then WQT.Config.Log("Sync.OnWorldEnter 出错: " .. tostring(err)) end
        end
    end
end)
```

- [ ] **Step 5: 写 Config.lua**

创建 `WorldQuestTracker/Config.lua`：

```lua
-- 账本数据层：SavedVariables 建库、周 CD 锚点、诊断日志环。
-- 数据结构（WQTDB，account-wide）：
--   schema = 1
--   cycleAnchor = <本 CD 起点时间戳>
--   quests[questID]     = { name, mapID, lastSeenAt, goldLock, currentSig, highFreq,
--                           observations[sig] = { gold, money, items, firstSeen, lastSeen, goneSince } }
--   chars[角色-服务器]   = { class, level, lastSync, views[questID] = { s, t } }
--   turnins[角色][任务]  = { { t, g, sig, cycle }, ... }   -- 1-based 追加日志
--   manual[角色][任务]   = { state, t, g }
--   logs                = { 最近 200 条 }

WQT.Config = {}
local Config = WQT.Config

local WEEK_SECONDS = 604800
local MAX_LOGS = 200

function Config.DB()
    return WQTDB
end

-- 金币格式化。nil → "?" 表示"未知"，与 0（真没赚钱）严格区分。
function Config.FormatGold(copper)
    if type(copper) ~= "number" then return "?" end
    return string.format("%dg%ds%dc",
        math.floor(copper / 10000), math.floor((copper % 10000) / 100), copper % 100)
end

function Config.Log(msg)
    local db = Config.DB()
    if not db then return end
    db.logs = db.logs or {}
    local stamp = math.floor((GetTime and GetTime() or 0) % 100000)
    table.insert(db.logs, string.format("%d %s", stamp, tostring(msg)))
    while #db.logs > MAX_LOGS do table.remove(db.logs, 1) end
end

function Config.GetLogs()
    local db = Config.DB()
    if not db or not db.logs then return "" end
    return table.concat(db.logs, "\n")
end

-- 服务端下次周刷新时间反推本 CD 起点。不写死星期几（国服周四）。
function Config.CycleAnchorOf(resetTime)
    if type(resetTime) ~= "number" then return nil end
    return resetTime - WEEK_SECONDS
end

-- 锚点只在真的进入新 CD 时前进。接口缺失时沿用旧值并降级记日志。
-- 用 `not fn` + pcall 判可用性而不是 `type(fn) ~= "function"`：
-- 游戏里 WoW 内置函数 type 就是 "function"，但离线测试里 Python 回调
-- 被 lupa 包成 "userdata"，用 type 判会把可用的回调当成缺失。
function Config.RefreshCycle()
    local db = Config.DB()
    if not db then return false end
    local fn = GetQuestResetTime
    if not fn then
        Config.Log("RefreshCycle 降级：GetQuestResetTime 不可用，沿用旧锚点")
        return false
    end
    local okCall, resetTime = pcall(fn)
    if not okCall then
        Config.Log("RefreshCycle 降级：GetQuestResetTime 不可用，沿用旧锚点")
        return false
    end
    local anchor = Config.CycleAnchorOf(resetTime)
    if anchor and anchor > (db.cycleAnchor or 0) then
        db.cycleAnchor = anchor
        return true
    end
    return false
end

function Config.Init()
    if type(WQTDB) ~= "table" then WQTDB = {} end
    local db = WQTDB
    db.quests = db.quests or {}
    db.chars = db.chars or {}
    db.turnins = db.turnins or {}
    db.manual = db.manual or {}
    db.logs = db.logs or {}
    db.schema = math.max(db.schema or 0, 1)
    if not db.cycleAnchor or db.cycleAnchor == 0 then
        Config.RefreshCycle()
        db.cycleAnchor = db.cycleAnchor or 0
    end
    Config.Log(("账本就绪 schema=%d cycleAnchor=%d"):format(db.schema, db.cycleAnchor))
end
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python tests/run_wqt_tests.py`
Expected: `6/6 单测通过`，退出码 0

- [ ] **Step 7: 提交**

```bash
git status --short
git add WorldQuestTracker/WorldQuestTracker.toc WorldQuestTracker/Core.lua WorldQuestTracker/Config.lua tests/run_wqt_tests.py
git commit -m "feat: WorldQuestTracker 骨架——账本建库、周 CD 锚点从服务端重置时间反推" -- WorldQuestTracker tests/run_wqt_tests.py
```

Expected: 提交只含这 4 个新文件；`git status --short` 中其它会话的 `AHSnipe/*` 改动仍留在工作区未被带走。

---

### Task 2: Ledger —— 世界任务池 diff 与奖励签名

**Files:**
- Create: `WorldQuestTracker/Ledger.lua`
- Modify: `WorldQuestTracker/WorldQuestTracker.toc`（`Config.lua` 之后追加 `Ledger.lua`）
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `WQT.Config.Log`（仅日志，不参与判定）
- Produces（`WQT.Ledger`，全部纯函数，第一个参数都是 db 表）：
  - `L.MakeSig(money, items) -> string`：`money` 铜币数或 nil（nil 编码为 `-1`），`items` 为 itemID 数组或 nil；物品顺序无关
  - `L.SigMoney(sig) -> number`；`L.IsGoldSig(sig) -> boolean`（前段金币 > 0）
  - `L.DiffPool(db, pool, now) -> { added = {questID..}, gone = {questID..}, renewed = {questID..}, sigChanged = {questID..} }`
  - `L.GetObs(db, questID) -> obs|nil`（当前签名的 observation；任务不在池中返回 nil）
  - `L.CountObs(db, questID) -> number`
  - `L.IsHighFreq(db, questID) -> boolean`
  - `L.GetQuestName(db, questID) -> string|nil`
  - pool 条目契约（`Api.GetPool` 与测试都必须产出这个形状）：
    `{ questID = number, mapID = number|nil, name = string|nil, money = number|nil, items = {itemID..}|nil }`
  - 高频判定阈值常量 `HIGH_FREQ_GAP = 129600`（消失时长小于 1.5 天 → 高频），由本任务定义，Task 3 不再重复定义
  - `db.quests[questID]` 由本任务写入的字段：`name`、`mapID`、`lastSeenAt`、`currentSig`、`highFreq`、`goneAt`（任务级消失时刻，判高频用）、`observations[sig]`

- [ ] **Step 1: 写失败测试**

追加到 `tests/run_wqt_tests.py`（放在 `if __name__` 之前），并把函数名加进 `tests` 列表：

```python
def ledger_db(lua):
    """构造可直接喂给 Ledger 的账本。"""
    return lua.table_from({
        "schema": 1, "cycleAnchor": 0,
        "quests": lua.table_from({}), "chars": lua.table_from({}),
        "turnins": lua.table_from({}), "manual": lua.table_from({}),
        "logs": lua.table_from({}),
    })


def pool(lua, rows):
    """rows: [(questID, money, items)]；mapID 固定 2007，name 自动生成 '任务<id>'。"""
    arr = lua.table_from({})
    for i, (qid, money, items) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        if items is not None:
            entry["items"] = lua.table_from(items)
        arr[i] = lua.table_from(entry)
    return arr


def test_sig_stable_and_gold():
    """签名与物品顺序无关；有金币判金币任务，0 与 nil 都不判。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    a = str(L.MakeSig(123456, lua.table_from([7, 3, 9])))
    b = str(L.MakeSig(123456, lua.table_from([9, 7, 3])))
    if a != b:
        return fail("sig_stable_and_gold", f"物品顺序不应改变签名：{a} vs {b}")
    if not L.IsGoldSig(str(L.MakeSig(123456, lua.table_from({})))):
        return fail("sig_stable_and_gold", "含金币应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(0, lua.table_from([555])))):
        return fail("sig_stable_and_gold", "无金币只掉物品不应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(None, lua.table_from({})))):
        return fail("sig_stable_and_gold", "奖励未知不应判金币任务")
    if str(L.MakeSig(1, lua.table_from({}))) == str(L.MakeSig(2, lua.table_from({}))):
        return fail("sig_stable_and_gold", "金币不同应产生不同签名")
    ok("sig_stable_and_gold")


def test_diff_added_and_gone():
    """首次抓池全部 added；下次池中消失的任务置 gone 并清 currentSig。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    r1 = L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 0, [111])]), 1000)
    if len(r1["added"]) != 2 or len(r1["gone"]) != 0:
        return fail("diff_added_and_gone", f"首轮 added={len(r1['added'])} gone={len(r1['gone'])}，期望 2/0")
    sig1 = str(db["quests"][1]["currentSig"])
    if not L.IsGoldSig(sig1):
        return fail("diff_added_and_gone", "任务 1 应判为金币任务")
    if db["quests"][1]["observations"][sig1]["gold"] is not True:
        return fail("diff_added_and_gone", "observation.gold 应为 true")
    r2 = L.DiffPool(db, pool(lua, [(1, 500000, None)]), 2000)
    if len(r2["gone"]) != 1 or int(r2["gone"][1]) != 2:
        return fail("diff_added_and_gone", f"任务 2 应消失，实际 gone 条数={len(r2['gone'])} 首项={r2['gone'][1]}")
    if db["quests"][2]["currentSig"] is not None:
        return fail("diff_added_and_gone", "消失任务应清掉 currentSig")
    if L.GetObs(db, 2) is not None:
        return fail("diff_added_and_gone", "不在池中不应有当前 observation")
    if L.GetObs(db, 1) is None:
        return fail("diff_added_and_gone", "在池任务应有 observation")
    if L.GetQuestName(db, 1) != "任务1":
        return fail("diff_added_and_gone", f"任务名应可读，实际 {L.GetQuestName(db, 1)}")
    ok("diff_added_and_gone")


def test_diff_renewed_new_round():
    """12 小时任务消失后又出现 = 新一轮：firstSeen 重置为本轮起点，goneSince 清空。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r3 = L.DiffPool(db, one, 3000)
    if len(r3["renewed"]) != 1 or int(r3["renewed"][1]) != 7:
        return fail("diff_renewed_new_round", f"应报 renewed 任务 7，实际 {len(r3['renewed'])}")
    obs = L.GetObs(db, 7)
    if obs is None:
        return fail("diff_renewed_new_round", "重新出现后应能取到 observation")
    if int(obs["firstSeen"]) != 3000:
        return fail("diff_renewed_new_round", f"新一轮 firstSeen 应为 3000，实际 {obs['firstSeen']}")
    if obs["goneSince"] is not None:
        return fail("diff_renewed_new_round", "重新出现后 goneSince 应清空")
    ok("diff_renewed_new_round")


def test_diff_sig_changed_modes():
    """同一任务奖励变化（狩猎/普通模式差异）：两条 observation 并存，金币标记各算各的。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(9, 400000, None)]), 1000)
    r = L.DiffPool(db, pool(lua, [(9, 0, [777])]), 2000)
    if len(r["sigChanged"]) != 1 or int(r["sigChanged"][1]) != 9:
        return fail("diff_sig_changed_modes", f"应只报 sigChanged，实际 gone={len(r['gone'])} added={len(r['added'])}")
    if int(L.CountObs(db, 9)) != 2:
        return fail("diff_sig_changed_modes", f"应有 2 条 observation，实际 {L.CountObs(db, 9)}")
    if L.IsGoldSig(str(db["quests"][9]["currentSig"])):
        return fail("diff_sig_changed_modes", "当前签名（无金币）不应判金币任务")
    ok("diff_sig_changed_modes")


def test_high_freq_learning():
    """消失时长 < 1.5 天的学到高频；消失 4 天的周常不学。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    day = 86400
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)      # 12 小时后消失
    L.DiffPool(db, one, 1000 + 86400)                # 又出现
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_learning", "消失 12 小时应学到高频标记")
    db2 = ledger_db(lua)
    L.DiffPool(db2, one, 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, one, 1000 + 5 * day)             # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_learning", "消失 4 天的周常不应标为高频")
    ok("high_freq_learning")


def test_diff_renewed_with_new_reward():
    """消失后换奖励回来（12 小时区每轮奖励可能不同）：算新一轮，不算 added/sigChanged。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r = L.DiffPool(db, pool(lua, [(7, 0, [888])]), 3000)
    if len(r["renewed"]) != 1 or int(r["renewed"][1]) != 7:
        return fail("diff_renewed_with_new_reward",
                    f"换奖励重现应报 renewed，实际 renewed={len(r['renewed'])} added={len(r['added'])} sigChanged={len(r['sigChanged'])}")
    if len(r["added"]) != 0 or len(r["sigChanged"]) != 0:
        return fail("diff_renewed_with_new_reward", "重现不应同时报成 added 或 sigChanged")
    if int(L.CountObs(db, 7)) != 2:
        return fail("diff_renewed_with_new_reward", f"两份奖励应各留一条 observation，实际 {L.CountObs(db, 7)}")
    if L.IsGoldSig(str(db["quests"][7]["currentSig"])):
        return fail("diff_renewed_with_new_reward", "当前这轮无金币，不应判金币任务")
    ok("diff_renewed_with_new_reward")


def test_high_freq_after_reward_rotation():
    """高频标记只看任务消失多久，不看奖励签名：12 小时区每轮奖励不同，绑签名就永远学不到。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    day = 86400
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)          # 消失 12 小时
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 1000 + 86400)  # 换奖励回来
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_after_reward_rotation", "换奖励的 12 小时重现也应学到高频")
    db2 = ledger_db(lua)
    L.DiffPool(db2, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, pool(lua, [(7, 0, [888])]), 1000 + 5 * day)  # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_after_reward_rotation", "消失 4 天的周常不应标为高频")
    ok("high_freq_after_reward_rotation")
```

追加到 `tests` 列表：`test_sig_stable_and_gold`、`test_diff_added_and_gone`、`test_diff_renewed_new_round`、`test_diff_sig_changed_modes`、`test_high_freq_learning`、`test_diff_renewed_with_new_reward`、`test_high_freq_after_reward_rotation`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: `FileNotFoundError: ...Ledger.lua`

- [ ] **Step 3: 写 Ledger.lua**

创建 `WorldQuestTracker/Ledger.lua`：

```lua
-- 纯账本逻辑：世界任务池 diff、奖励签名。不碰 WoW 全局 API（时间戳由调用方传入），
-- 因此可在 lupa 里离线跑。

WQT.Ledger = {}
local L = WQT.Ledger

local MAX_OBS_PER_QUEST = 4
-- 消失时长小于此值 = 高频区（屠戮者高地/虚影风暴 12 小时级），不写死区域名单
local HIGH_FREQ_GAP = 129600

function L.MakeSig(money, items)
    local parts = { tostring(math.floor(type(money) == "number" and money or -1)) }
    if type(items) == "table" then
        local ids = {}
        for i = 1, #items do ids[#ids + 1] = tostring(items[i]) end
        table.sort(ids)
        parts[#parts + 1] = table.concat(ids, ",")
    end
    return table.concat(parts, "|")
end

function L.SigMoney(sig)
    local v = tonumber((tostring(sig)):match("^(%-?%d+)"))
    return (v and v > 0) and v or 0
end

function L.IsGoldSig(sig)
    return L.SigMoney(sig) > 0
end

local function Push(list, questID) list[#list + 1] = questID end

-- 逐项复制而不是 { unpack(...) }：lupa 侧的 Lua 与 WoW 内嵌 Lua 的 unpack
-- 位置不同（5.1 是全局，5.2+ 挪到 table.unpack），复制循环两边都稳。
local function CopyArray(src)
    local out = {}
    for i = 1, #src do out[i] = src[i] end
    return out
end

local function CountPairs(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

function L.CountObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.observations then return 0 end
    return CountPairs(rec.observations)
end

-- observation 超上限时按 lastSeen 淘汰最旧；当前签名永不淘汰。
local function TrimObservations(rec, keepSig)
    while CountPairs(rec.observations) > MAX_OBS_PER_QUEST do
        local oldestSig, oldestSeen
        for sig, obs in pairs(rec.observations) do
            if sig ~= keepSig and (not oldestSeen or obs.lastSeen < oldestSeen) then
                oldestSig, oldestSeen = sig, obs.lastSeen
            end
        end
        if not oldestSig then break end
        rec.observations[oldestSig] = nil
    end
end

--- 用当前角色的世界任务池快照跟账本 diff。
--- "本轮"= 连续存在于两次快照之间，不设时间阈值：任务消失后再出现即新一轮
--- （无论隔 12 小时还是 4 天），高频区与周常区共用同一套逻辑，插件不需要知道任何刷新周期。
function L.DiffPool(db, pool, now)
    local res = { added = {}, gone = {}, renewed = {}, sigChanged = {} }
    local seen = {}

    for i = 1, #pool do
        local q = pool[i]
        local questID = tonumber(q.questID)
        if questID then
            local sig = L.MakeSig(q.money, q.items)
            local rec = db.quests[questID]
            local isNew = not rec
            if isNew then
                rec = { observations = {} }
                db.quests[questID] = rec
                Push(res.added, questID)
            end
            local prevSig = rec.currentSig
            -- 高频只看"这个任务消失了多久"，与奖励签名无关：12 小时区每轮奖励可能都不同，
            -- 绑在签名上就永远学不到（obs.goneSince 只负责重置本轮 firstSeen）。
            if rec.goneAt then
                if now - rec.goneAt < HIGH_FREQ_GAP then rec.highFreq = true end
                rec.goneAt = nil
            end
            rec.name, rec.mapID, rec.lastSeenAt = q.name, q.mapID, now

            local obs = rec.observations[sig]
            if obs then
                if obs.goneSince then
                    -- 消失后又出现 = 新一轮：重置 firstSeen，使上一轮的已交不再计入本轮
                    obs.firstSeen = now
                    obs.goneSince = nil
                    Push(res.renewed, questID)
                end
                obs.lastSeen = now
            else
                rec.observations[sig] = {
                    gold = L.IsGoldSig(sig),
                    money = (type(q.money) == "number") and q.money or nil,
                    items = q.items and CopyArray(q.items) or nil,
                    firstSeen = now,
                    lastSeen = now,
                }
                TrimObservations(rec, sig)
                -- 原来就有签名 → 奖励变了；原来没签名且不是新面孔 → 消失后换了奖励回来，
                -- 同样是新一轮。漏掉后者会让 12 小时区的重现在汇总里凭空消失。
                if prevSig then
                    Push(res.sigChanged, questID)
                elseif not isNew then
                    Push(res.renewed, questID)
                end
            end

            if rec.currentSig ~= sig then
                rec.currentSig = sig
            end
            seen[questID] = true
        end
    end

    for questID, rec in pairs(db.quests) do
        if not seen[questID] and rec.currentSig then
            local obs = rec.observations[rec.currentSig]
            if obs then obs.goneSince = now end
            rec.goneAt = now
            rec.currentSig = nil
            Push(res.gone, questID)
        end
    end

    return res
end

-- 当前池内那份 observation；不在池中返回 nil（调用方据此显示"本轮已结束"）。
function L.GetObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.currentSig then return nil end
    return rec.observations[rec.currentSig]
end

function L.IsHighFreq(db, questID)
    local rec = db.quests and db.quests[questID]
    return not not (rec and rec.highFreq)
end

function L.GetQuestName(db, questID)
    local rec = db.quests and db.quests[questID]
    return rec and rec.name or nil
end
```

签名变化的记账点只有一处：新建 observation 的分支里，用进入本次循环前抓到的 `prevSig` 判断——`prevSig` 存在说明这个任务原来有另一份奖励，报 `sigChanged`；`prevSig` 为空且任务不是本次新建，说明它消失过又换了奖励回来，报 `renewed`。判断必须在 `rec.currentSig = sig` 之前完成，否则自己刚赋的值会被误读成"原来就有签名"。同一 questID 在一次 diff 里最多进 `sigChanged` / `renewed` 之一一次。

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/run_wqt_tests.py`
Expected: `13/13 单测通过`

- [ ] **Step 5: 更新 toc 并语法检查**

`WorldQuestTracker/WorldQuestTracker.toc` 末尾：

```
Core.lua
Config.lua
Ledger.lua
```

Run: `python tests/run_wqt_tests.py --syntax`
Expected: 全部 `PASS syntax:...`

- [ ] **Step 6: 提交**

```bash
git status --short
git add WorldQuestTracker/Ledger.lua WorldQuestTracker/WorldQuestTracker.toc tests/run_wqt_tests.py
git commit -m "feat: Ledger 池快照 diff——以出现/消失为唯一真相，12 小时任务重算新一轮" -- WorldQuestTracker tests/run_wqt_tests.py
```

---

### Task 3: Ledger —— 交任务日志、状态判定、周统计、体积清理

**Files:**
- Modify: `WorldQuestTracker/Ledger.lua`（文件末尾追加；沿用 Task 2 已有的 local `TrimObservations`，不要再定义一份）
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: Task 2 的 `L.GetObs`、`L.MakeSig`、`L.IsGoldSig`、`L.DiffPool`、local `TrimObservations`
- Produces（全部挂在 `WQT.Ledger`）：
  - `L.OnTurnIn(db, charKey, questID, t, g, sig) -> boolean`：追加一条交任务记录；`g` 可为 nil（未测到）；`g > 0` 时顺带把该 observation 转正为金币任务
  - `L.SetManual(db, charKey, questID, state, t, g) -> table|nil`：手动补录，返回被覆盖的旧值（供撤销栈）；`state` ∈ `"done"|"progress"|"open"`，传 `"open"` 等于清除手动记录
  - `L.ClearManual(db, charKey, questID)`
  - `L.SetView(db, charKey, questID, state, t)`：Sync 写入本角色的登录快照态
  - `L.State(db, charKey, questID) -> string`：`"done"|"progress"|"open"|"ended"|"unknown"`
  - `L.WeekStats(db, charKey, cycleStart) -> count, gold`
  - `L.MarkGoldByObservation(db, questID, sig) -> boolean`；`L.SetGoldLock(db, questID, locked) -> boolean`
  - `L.GoldQuestIDs(db) -> {questID,...}`（1-based 数组，只含**当前仍在池中且判定为金币**的任务，按任务名排序）
  - `L.Characters(db) -> {charKey,...}` 按名排序
  - `L.UndoApply(db, entry) -> boolean`：`entry = { kind = "manual"|"turnin", charKey, questID, old }`
  - `L.Prune(db, now, cycleStart)`
  - 常量 `MAX_TURNINS_PER_QUEST = 8`

- [ ] **Step 1: 写失败测试**

```python
def test_turnin_state_and_week_stats():
    """交任务入日志；本轮内显示已交；周统计只算本 CD。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 5000
    L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 300000, None)]), 6000)
    sig = str(db["quests"][1]["currentSig"])
    L.OnTurnIn(db, "A-永恒之井", 1, 6100, 500000, sig)
    if str(L.State(db, "A-永恒之井", 1)) != "done":
        return fail("turnin_state_and_week_stats", "本轮内已交应显示 done")
    if str(L.State(db, "A-永恒之井", 2)) != "open":
        return fail("turnin_state_and_week_stats", "池内未做应显示 open")
    count, gold = L.WeekStats(db, "A-永恒之井", 5000)
    if int(count) != 1 or int(gold) != 500000:
        return fail("turnin_state_and_week_stats", f"本周应 1 条/500000，实际 {count}/{gold}")
    count0, _ = L.WeekStats(db, "A-永恒之井", 7000)
    if int(count0) != 0:
        return fail("turnin_state_and_week_stats", "上个 CD 的记录不应计入本周")
    ok("turnin_state_and_week_stats")


def test_turnin_not_counted_across_rounds():
    """任务消失又出现 = 新一轮，上一轮的已交不再计入本轮。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.OnTurnIn(db, "A-永恒之井", 7, 1100, 300000, str(db["quests"][7]["currentSig"]))
    if str(L.State(db, "A-永恒之井", 7)) != "done":
        return fail("turnin_not_counted_across_rounds", "本轮做完应 done")
    L.DiffPool(db, pool(lua, []), 2000)
    if str(L.State(db, "A-永恒之井", 7)) != "ended":
        return fail("turnin_not_counted_across_rounds", "消失后应显示本轮已结束")
    L.DiffPool(db, one, 3000)
    st = str(L.State(db, "A-永恒之井", 7))
    if st != "open":
        return fail("turnin_not_counted_across_rounds", f"新一轮应回到待做，实际 {st}")
    count, _ = L.WeekStats(db, "A-永恒之井", 0)
    if int(count) != 1:
        return fail("turnin_not_counted_across_rounds", "金币历史仍留在库里（统计口径不变，本轮状态另算）")
    ok("turnin_not_counted_across_rounds")


def test_manual_beats_auto_and_survives_sync():
    """手动补录优先于自动快照；后续 SetView 不得覆盖它；撤销能还原。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(3, 200000, None)]), 1000)
    L.SetView(db, "B-永恒之井", 3, "open", 1000)
    old = L.SetManual(db, "B-永恒之井", 3, "done", 900, 150000)
    if str(L.State(db, "B-永恒之井", 3)) != "done":
        return fail("manual_beats_auto_and_survives_sync", "手动已交应覆盖自动 open")
    L.SetView(db, "B-永恒之井", 3, "open", 2000)
    if str(L.State(db, "B-永恒之井", 3)) != "done":
        return fail("manual_beats_auto_and_survives_sync", "登录同步不应覆盖手动记录")
    count, gold = L.WeekStats(db, "B-永恒之井", 0)
    if int(count) != 1 or int(gold) != 150000:
        return fail("manual_beats_auto_and_survives_sync", f"手动补录应计入统计，实际 {count}/{gold}")
    L.UndoApply(db, {"kind": "manual", "charKey": "B-永恒之井", "questID": 3, "old": old})
    if str(L.State(db, "B-永恒之井", 3)) != "open":
        return fail("manual_beats_auto_and_survives_sync", "撤销应回到自动判定的 open")
    ok("manual_beats_auto_and_survives_sync")


def test_manual_not_double_counted_with_turnin():
    """同一任务既有自动交任务记录又有手动已交：完成数只算一次。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(8, 250000, None)]), 1000)
    L.OnTurnIn(db, "E-永恒之井", 8, 1100, 250000, str(db["quests"][8]["currentSig"]))
    L.SetManual(db, "E-永恒之井", 8, "done", 1200, 250000)
    count, gold = L.WeekStats(db, "E-永恒之井", 0)
    if int(count) != 1 or int(gold) != 250000:
        return fail("manual_not_double_counted_with_turnin", f"不应重复计数，实际 {count}/{gold}")
    ok("manual_not_double_counted_with_turnin")


def test_gold_unmeasured_counts_not_sum():
    """钱差测不到（g=nil）：完成数照计，金币总额报未知（-1），不报 0。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(4, 100000, None)]), 1000)
    L.OnTurnIn(db, "C-永恒之井", 4, 1100, None, str(db["quests"][4]["currentSig"]))
    count, gold = L.WeekStats(db, "C-永恒之井", 0)
    if int(count) != 1:
        return fail("gold_unmeasured_counts_not_sum", f"count 应为 1，实际 {count}")
    if int(gold) != -1:
        return fail("gold_unmeasured_counts_not_sum", f"无实测值 gold 应为 -1，实际 {gold}")
    ok("gold_unmeasured_counts_not_sum")


def test_mark_gold_by_observation():
    """实测到账金币把未判定任务转正；手动锁定后拒绝自动改写。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(5, None, None)]), 1000)   # 奖励查不到 → 未判定
    sig = str(db["quests"][5]["currentSig"])
    if len(L.GoldQuestIDs(db)) != 0:
        return fail("mark_gold_by_observation", "未判定任务不应进金币列表")
    L.OnTurnIn(db, "F-永恒之井", 5, 1100, 300000, sig)
    if len(L.GoldQuestIDs(db)) != 1:
        return fail("mark_gold_by_observation", "实测到金币后应自动转正为金币任务")
    L.SetGoldLock(db, 5, True)
    db["quests"][5]["observations"][sig]["gold"] = False
    if L.MarkGoldByObservation(db, 5, sig) is not False:
        return fail("mark_gold_by_observation", "goldLock 时应拒绝自动改写")
    ok("mark_gold_by_observation")


def test_prune_bounds_size():
    """turnins 每任务上限 8 条；三个 CD 未出现的任务整条清出，金币历史保留。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(6, 100000, None)]), 1000)
    sig = str(db["quests"][6]["currentSig"])
    for i in range(1, 12):
        L.OnTurnIn(db, "D-永恒之井", 6, 1000 + i, 100000, sig)
    rows = db["turnins"]["D-永恒之井"][6]
    if len(rows) != 8:
        return fail("prune_bounds_size", f"turnins 应截到 8 条，实际 {len(rows)}")
    if int(rows[len(rows)]["t"]) != 1011:
        return fail("prune_bounds_size", "应丢最旧留最新")
    L.DiffPool(db, pool(lua, []), 1000)
    L.Prune(db, 1000 + 25 * 86400, 1000)
    if db["quests"][6] is not None:
        return fail("prune_bounds_size", "连续 3 个 CD 未出现应清出账本")
    if db["turnins"]["D-永恒之井"][6] is None:
        return fail("prune_bounds_size", "清理任务定义不应删掉金币历史")
    ok("prune_bounds_size")
```

追加这 7 个函数名到 `tests` 列表。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: Lua 侧 `attempt to call a nil value (field 'OnTurnIn')` 被 lupa 抛为 `AttributeError`/`LuaError`

- [ ] **Step 3: 实现 Ledger 剩余函数**

追加到 `WorldQuestTracker/Ledger.lua` 末尾：

```lua
--------------------------------------------------------------------
-- 交任务、状态、周统计、撤销与体积清理
--------------------------------------------------------------------

local WEEK = 604800
local MAX_TURNINS_PER_QUEST = 8

function L.OnTurnIn(db, charKey, questID, t, g, sig)
    db.turnins[charKey] = db.turnins[charKey] or {}
    local log = db.turnins[charKey]
    log[questID] = log[questID] or {}
    local rows = log[questID]
    rows[#rows + 1] = { t = t, g = g, sig = sig, cycle = db.cycleAnchor or 0 }
    while #rows > MAX_TURNINS_PER_QUEST do table.remove(rows, 1) end
    if type(g) == "number" and g > 0 then
        L.MarkGoldByObservation(db, questID, sig)
    end
    return true
end

-- 手动补录。state == "open" 视为清除手动覆盖，回到自动判定。
function L.SetManual(db, charKey, questID, state, t, g)
    db.manual[charKey] = db.manual[charKey] or {}
    local old = db.manual[charKey][questID]
    if state == "open" then
        db.manual[charKey][questID] = nil
    else
        db.manual[charKey][questID] = { state = state, t = t, g = g }
    end
    return old
end

function L.ClearManual(db, charKey, questID)
    if db.manual[charKey] then db.manual[charKey][questID] = nil end
end

function L.SetView(db, charKey, questID, state, t)
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    db.chars[charKey].views[questID] = { s = state, t = t }
end

--- 状态优先级：手动补录 > 本轮实测交任务 > 本角色登录快照 > 池推断。
--- "unknown" 只在完全没有来源时出现，UI 必须如实显示，不得当成"没做"。
function L.State(db, charKey, questID)
    local m = db.manual[charKey] and db.manual[charKey][questID]
    if m then return m.state end

    local rec = db.quests[questID]
    if not rec then return "unknown" end

    local obs = L.GetObs(db, questID)
    if not obs then return "ended" end   -- 当前池里没有 = 本轮已结束

    local rows = db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    if last and last.t >= obs.firstSeen then return "done" end

    local view = db.chars[charKey] and db.chars[charKey].views
        and db.chars[charKey].views[questID]
    if view and (view.t or 0) >= obs.firstSeen then
        if view.s == "done" then return "done" end
        if view.s == "progress" then return "progress" end
        if view.s == "unknown" then return "unknown" end
    end
    return "open"
end

--- 本 CD 汇总。手动已交若与实测交任务是同一条就不重复计数；
--- 有完成记录但一条实测金币都没有时 gold 返回 -1，UI 显示 "?"，绝不显示 0。
function L.WeekStats(db, charKey, cycleStart)
    local count, gold, goldKnown = 0, 0, false
    local counted = {}
    for questID, rows in pairs(db.turnins[charKey] or {}) do
        for i = 1, #rows do
            local r = rows[i]
            if r.t and (not cycleStart or r.t >= cycleStart) then
                count = count + 1
                counted[questID] = true
                if type(r.g) == "number" then
                    gold = gold + r.g
                    goldKnown = true
                end
            end
        end
    end
    for questID, m in pairs(db.manual[charKey] or {}) do
        if m.state == "done" and not counted[questID]
            and (not cycleStart or (m.t or 0) >= cycleStart) then
            count = count + 1
            if type(m.g) == "number" then
                gold = gold + m.g
                goldKnown = true
            end
        end
    end
    return count, (count > 0 and not goldKnown) and -1 or gold
end

function L.MarkGoldByObservation(db, questID, sig)
    local rec = db.quests[questID]
    if not rec or rec.goldLock or not sig then return false end
    local obs = rec.observations[sig]
    if not obs then return false end
    obs.gold = true
    return true
end

function L.SetGoldLock(db, questID, locked)
    local rec = db.quests[questID]
    if not rec then return false end
    rec.goldLock = locked or nil
    return true
end

function L.GoldQuestIDs(db)
    local out = {}
    for questID, rec in pairs(db.quests or {}) do
        local sig = rec.currentSig
        local obs = sig and rec.observations and rec.observations[sig]
        if obs and obs.gold then out[#out + 1] = questID end
    end
    table.sort(out, function(a, b)
        return (db.quests[a].name or "") < (db.quests[b].name or "")
    end)
    return out
end

function L.Characters(db)
    local out = {}
    for charKey in pairs(db.chars or {}) do out[#out + 1] = charKey end
    table.sort(out)
    return out
end

function L.UndoApply(db, entry)
    if not entry then return false end
    if entry.kind == "manual" then
        if entry.old then
            db.manual[entry.charKey] = db.manual[entry.charKey] or {}
            db.manual[entry.charKey][entry.questID] = entry.old
        else
            L.ClearManual(db, entry.charKey, entry.questID)
        end
        return true
    elseif entry.kind == "turnin" then
        local rows = db.turnins[entry.charKey] and db.turnins[entry.charKey][entry.questID]
        if rows and #rows > 0 then table.remove(rows, #rows) end
        return true
    end
    return false
end

--- 控制 SavedVariables 体积：陈旧任务整条清出、轮数与观察数封顶。金币历史不删。
function L.Prune(db, now, cycleStart)
    for questID, rec in pairs(db.quests or {}) do
        if (now or 0) - (rec.lastSeenAt or 0) > 3 * WEEK then
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

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/run_wqt_tests.py`
Expected: `20/20 单测通过`（Task 1 的 6 条 + Task 2 的 7 条 + 本任务 7 条）

- [ ] **Step 5: 提交**

```bash
python tests/run_wqt_tests.py --syntax
git status --short
git add WorldQuestTracker/Ledger.lua tests/run_wqt_tests.py
git commit -m "feat: Ledger 状态与周统计——本轮按时段判定、未实测金币报未知不报 0" -- WorldQuestTracker tests/run_wqt_tests.py
```

---

### Task 4: Api 适配层与 12.x 探测报告

**Files:**
- Create: `WorldQuestTracker/Api.lua`
- Create: `WorldQuestTracker/Probe.lua`
- Create: `WorldQuestTracker/LogWindow.lua`
- Modify: `WorldQuestTracker/WorldQuestTracker.toc`
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `WQT.Config.Log`、`WQT.RegisterCommand`、Task 2 的 pool 条目契约
- Produces:
  - `WQT.Api.GetPool() -> list|nil, err`：成功返回 pool 条目数组；接口缺失或返回空表返回 `nil, 原因`。奖励字段优先取池行自带（`money`/`rewardMoney`），缺失才问 `C_QuestLog` 奖励接口，两边都没有 → `money = nil`（未判定）
  - `WQT.Api.QuestState(questID) -> "open"|"progress"|"done"|"unknown", err`
  - `WQT.Api.QuestRewardMoney(questID) -> number|nil`（**不可用时必须返回 nil，不能返回 0**）
  - `WQT.Api.QuestRewardItems(questID) -> {itemID..}|nil`
  - `WQT.Api.Money() -> number|nil`；`WQT.Api.ResetTime() -> number|nil`
  - `WQT.Api.LastDegrade() -> string`；`WQT.Api.Resolve(dottedPath) -> any`；`WQT.Api.CandidatePaths() -> {path..}`
  - `WQT.Probe.Run() -> string`（整段报告文本）
  - `WQT.LogWindow.Show(text)` / `WQT.LogWindow.Toggle()`

- [ ] **Step 1: 写失败测试**

```python
def test_api_degrades_when_missing():
    """接口全缺失：GetPool 返回 nil + 原因，不抛错。"""
    _, wqt = load_lua("Config", "Ledger", "Api")
    rows, err = wqt.Api.GetPool()
    if rows is not None:
        return fail("api_degrades_when_missing", "无 API 时不应返回池数据")
    if err is None:
        return fail("api_degrades_when_missing", "必须报告降级原因")
    ok("api_degrades_when_missing")


def test_api_state_unknown_when_missing():
    """完成与接取接口都缺失：QuestState 返回 unknown，绝不返回 open。"""
    _, wqt = load_lua("Config", "Ledger", "Api")
    state, err = wqt.Api.QuestState(123)
    if str(state) != "unknown":
        return fail("api_state_unknown_when_missing", f"应返回 unknown，实际 {state}")
    if err is None:
        return fail("api_state_unknown_when_missing", "应带降级原因")
    ok("api_state_unknown_when_missing")


def test_api_normalizes_known_shape():
    """模拟 WorldQuestInfo 形状：字段名差异要归一，奖励接口缺失要留 nil。"""
    lua, wqt = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from({
            lua.table_from({"questID": 11, "mapID": 2007, "name": "补给车队"}),
            lua.table_from({"questID": 12, "zoneID": 2008, "title": "虚空突袭"}),
        })
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid == 11,
        "IsQuestCompleted": lambda qid: False,
    })
    rows, err = wqt.Api.GetPool()
    if rows is None:
        return fail("api_normalizes_known_shape", f"应取到池数据，err={err}")
    if len(rows) != 2:
        return fail("api_normalizes_known_shape", f"应有 2 条，实际 {len(rows)}")
    if int(rows[1]["questID"]) != 11 or int(rows[1]["mapID"]) != 2007:
        return fail("api_normalizes_known_shape", f"条目 1 归一错误：{rows[1]['questID']}/{rows[1]['mapID']}")
    if int(rows[2]["questID"]) != 12 or int(rows[2]["mapID"]) != 2008:
        return fail("api_normalizes_known_shape", f"zoneID 应可当 mapID，实际 {rows[2]['mapID']}")
    if str(rows[2]["name"]) != "虚空突袭":
        return fail("api_normalizes_known_shape", "title 应可当 name")
    if rows[1]["money"] is not None:
        return fail("api_normalizes_known_shape", "奖励接口缺失时 money 必须是 nil（未判定），不能当 0")
    started, _ = wqt.Api.QuestState(11)
    if str(started) != "progress":
        return fail("api_normalizes_known_shape", f"IsQuestStarted 为真应得 progress，实际 {started}")
    done, _ = wqt.Api.QuestState(999)
    if str(done) != "open":
        return fail("api_normalizes_known_shape", f"两个接口都为假应得 open，实际 {done}")
    ok("api_normalizes_known_shape")


def test_probe_report_and_command_registered():
    """探测报告列出每个候选的可用性；Probe/LogWindow 应注册 /wqt 子命令。"""
    lua, wqt = load_lua("Config", "Ledger", "Api", "Probe", "LogWindow", mock=True)
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})
    report = str(wqt.Probe.Run())
    if "C_WorldQuests.GetWorldQuests" not in report:
        return fail("probe_report_and_command_registered", "报告应含候选接口名")
    if "AVAILABLE" not in report or "MISSING" not in report:
        return fail("probe_report_and_command_registered", "报告应区分可用/缺失")
    if "LastDegrade" not in report:
        return fail("probe_report_and_command_registered", "报告应带降级原因")
    for cmd in ("probe", "log"):
        if wqt.commands[cmd] is None:
            return fail("probe_report_and_command_registered", f"/wqt {cmd} 未注册")
    ok("probe_report_and_command_registered")
```

追加 4 个函数名到 `tests` 列表。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: `FileNotFoundError: ...Api.lua`

- [ ] **Step 3: 写 Api.lua**

创建 `WorldQuestTracker/Api.lua`：

```lua
-- WoW API 适配层：插件里唯一读世界任务/奖励/完成状态接口的地方。
-- 12.x 接口形态未经实测：每个候选 pcall，字段名按候选列表试，
-- 取不到就返回原因并把功能降级为"未知"。降级原因集中存 LastDegrade，
-- 由 /wqt dump 与（阶段二）矩阵列头如实显示。

WQT.Api = {}
local Api = WQT.Api

local lastDegrade = "none"

function Api.LastDegrade() return lastDegrade end

local function Degrade(why)
    lastDegrade = why
    if WQT.Config and WQT.Config.Log then WQT.Config.Log("API 降级: " .. why) end
end

function Api.Resolve(path)
    local node = _G
    for part in path:gmatch("[^%.]+") do
        if type(node) ~= "table" then return nil end
        node = node[part]
    end
    return node
end

-- 判"可调用"用真值而不是 type(fn) == "function"：游戏里内置函数 type 确实是
-- "function"，但离线测试里 Python 回调被 lupa 包成 "userdata"，用 type 判会把
-- 可用的回调当成缺失（与 Config.RefreshCycle 同款处理）。
function Api.Callable(fn) return fn ~= nil end
local Callable = Api.Callable

local function FirstField(row, names)
    for i = 1, #names do
        local v = row[names[i]]
        if v ~= nil then return v end
    end
    return nil
end

-- 奖励信息优先取池行自带的字段（12.x 的 WorldQuestInfo 可能已经带奖励摘要），
-- 取不到再单独问奖励接口。两条路都没有才返回 nil（"未判定"）。
local function RewardMoney(row, questID)
    local v = tonumber(FirstField(row, { "money", "rewardMoney", "QuestMoneyToGo" }))
    if v then return v end
    return Api.QuestRewardMoney(questID)
end

local function RewardItems(row, questID)
    local v = FirstField(row, { "items", "rewardItems" })
    if type(v) == "table" and #v > 0 then return v end
    return Api.QuestRewardItems(questID)
end

local POOL_SOURCES = {
    "C_WorldQuests.GetWorldQuests",
    "C_WorldQuests.GetAllWorldQuests",
}

--- 抓当前角色的世界任务池。返回 nil 时调用方必须保持账本不变，
--- 绝不能把"读不到"当成"池子为空"（那会把整张账本判成全部消失）。
function Api.GetPool()
    for i = 1, #POOL_SOURCES do
        local path = POOL_SOURCES[i]
        local fn = Api.Resolve(path)
        if Callable(fn) then
            local okCall, raw = pcall(fn)
            if okCall and type(raw) == "table" then
                local out = {}
                for j = 1, #raw do
                    local row = raw[j]
                    if type(row) == "table" then
                        local questID = tonumber(FirstField(row, { "questID", "qID", "id" }))
                        if questID then
                            out[#out + 1] = {
                                questID = questID,
                                mapID = tonumber(FirstField(row, { "mapID", "zoneID", "areaID" })),
                                name = FirstField(row, { "name", "title", "questName" }),
                                money = RewardMoney(row, questID),
                                items = RewardItems(row, questID),
                            }
                        end
                    end
                end
                if #out > 0 then return out, nil end
                Degrade(path .. " 返回空表")
            else
                Degrade(path .. " 调用失败: " .. tostring(raw))
            end
        end
    end
    return nil, "none of " .. table.concat(POOL_SOURCES, ", ") .. " usable"
end

-- 奖励金币。接口缺失返回 nil（"未判定"）——返回 0 会被判成非金币任务。
function Api.QuestRewardMoney(questID)
    local fn = Api.Resolve("C_QuestLog.GetQuestRewardMoney")
    if not Callable(fn) then return nil end
    local okCall, money = pcall(fn, questID)
    if okCall and type(money) == "number" then return money end
    return nil
end

function Api.QuestRewardItems(questID)
    local fn = Api.Resolve("C_QuestLog.GetQuestRewardItems")
    if not Callable(fn) then return nil end
    local okCall, items = pcall(fn, questID)
    if not okCall or type(items) ~= "table" then return nil end
    local out = {}
    for i = 1, #items do
        local it = items[i]
        local itemID = tonumber(type(it) == "table" and (it.itemID or it.itemid) or it)
        if itemID then out[#out + 1] = itemID end
    end
    return out
end

local DONE_SOURCES = { "C_QuestLog.IsQuestCompleted", "IsQuestCompleted" }
local PROGRESS_SOURCES = { "C_QuestLog.IsQuestStarted", "C_QuestLog.IsOnQuest", "QuestLog_IsOnQuest" }

-- 返回 (布尔值或nil, 该接口是否真的可用)
local function TryState(paths, questID)
    for i = 1, #paths do
        local fn = Api.Resolve(paths[i])
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            if okCall then return v == true, true end
        end
    end
    return nil, false
end

--- 只回答"当前角色"的状态。其它角色的状态来自账本，绝不由这里推断。
function Api.QuestState(questID)
    local done, okDone = TryState(DONE_SOURCES, questID)
    local progress, okProgress = TryState(PROGRESS_SOURCES, questID)
    if not okDone and not okProgress then
        Degrade("完成/接取状态接口均不可用")
        return "unknown", "state api unavailable"
    end
    if done then return "done", nil end
    if okProgress and progress then return "progress", nil end
    if not okDone then
        Degrade("完成状态接口不可用")
        return "unknown", "completion api unavailable"
    end
    return "open", nil
end

function Api.Money()
    local fn = Api.Resolve("GetMoney")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

function Api.ResetTime()
    local fn = Api.Resolve("GetQuestResetTime")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

function Api.CandidatePaths()
    local list = {}
    for i = 1, #POOL_SOURCES do list[#list + 1] = POOL_SOURCES[i] end
    for i = 1, #DONE_SOURCES do list[#list + 1] = DONE_SOURCES[i] end
    for i = 1, #PROGRESS_SOURCES do list[#list + 1] = PROGRESS_SOURCES[i] end
    list[#list + 1] = "C_QuestLog.GetQuestRewardMoney"
    list[#list + 1] = "C_QuestLog.GetQuestRewardItems"
    list[#list + 1] = "GetQuestResetTime"
    list[#list + 1] = "GetMoney"
    return list
end
```

- [ ] **Step 4: 写 Probe.lua**

创建 `WorldQuestTracker/Probe.lua`：

```lua
-- /wqt probe：把 12.x 候选接口的实测结果输出成可复制文本。
-- 目的不是给用户看，是把"接口存不存在、返回什么形状"变成实证：
-- 阶段二的 UI 只允许接在这里验证过的入口。

WQT.Probe = {}
local Probe = WQT.Probe

local function Describe(v, depth)
    depth = depth or 0
    if type(v) ~= "table" then return string.format("%s(%s)", tostring(v), type(v)) end
    if depth >= 2 then return "table..." end
    local keys, n = {}, 0
    for k in pairs(v) do
        n = n + 1
        if n <= 12 then keys[#keys + 1] = tostring(k) end
    end
    table.sort(keys)
    return string.format("table[#%d]{%s}", n, table.concat(keys, ","))
end

function Probe.Run()
    local build = "?"
    if GetBuildInfo then
        local okCall, _, _, ver = pcall(GetBuildInfo)
        if okCall then build = tostring(ver) end
    end
    local lines = { "WQT 12.x 接口探测报告", "Interface: " .. build, "---- 候选接口" }

    local paths = WQT.Api.CandidatePaths()
    for i = 1, #paths do
        local path = paths[i]
        local fn = WQT.Api.Resolve(path)
        if not WQT.Api.Callable(fn) then
            lines[#lines + 1] = ("MISSING   %s"):format(path)
        else
            local okCall, a, b = pcall(fn)
            if okCall then
                lines[#lines + 1] = ("AVAILABLE %s -> %s | %s"):format(path, Describe(a), Describe(b))
            else
                lines[#lines + 1] = ("ERROR     %s -> %s"):format(path, tostring(a))
            end
        end
    end

    lines[#lines + 1] = "---- 池快照样本"
    local pool, err = WQT.Api.GetPool()
    if pool then
        lines[#lines + 1] = ("池条目数: %d"):format(#pool)
        for i = 1, math.min(5, #pool) do
            local q = pool[i]
            local state = { WQT.Api.QuestState(q.questID) }
            lines[#lines + 1] = ("  #%d questID=%s mapID=%s name=%s money=%s items=%s state=%s"):format(
                i, tostring(q.questID), tostring(q.mapID), tostring(q.name),
                tostring(q.money), Describe(q.items), tostring(state[1]))
        end
    else
        lines[#lines + 1] = "池不可用: " .. tostring(err)
    end

    lines[#lines + 1] = "---- 其它"
    lines[#lines + 1] = "ResetTime: " .. tostring(WQT.Api.ResetTime())
    lines[#lines + 1] = "Money: " .. tostring(WQT.Api.Money())
    lines[#lines + 1] = "LastDegrade: " .. tostring(WQT.Api.LastDegrade())

    local report = table.concat(lines, "\n")
    WQT.Config.Log("probe 完成")
    return report
end

WQT.RegisterCommand("probe", function()
    local report = Probe.Run()
    if WQT.LogWindow and WQT.LogWindow.Show then WQT.LogWindow.Show(report) end
    print(report)
end, "输出 12.x 接口实测报告")
```

- [ ] **Step 5: 写 LogWindow.lua**

创建 `WorldQuestTracker/LogWindow.lua`（沿用 AHSnipe 的"日志窗内 Ctrl+A/Ctrl+C 复制回传"做法，避免截图）：

```lua
-- 日志窗：多行 EditBox 承载探测报告与诊断日志。

WQT.LogWindow = {}
local Win = WQT.LogWindow

local frame

local function Ensure()
    if frame then return frame end
    frame = CreateFrame("Frame", "WQTLogFrame", UIParent, "BackdropTemplate")
    frame:SetSize(720, 480)
    frame:SetPoint("CENTER")
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
    frame:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    frame:SetBackdrop({
        bgFile = "Interface/DialogFrame/UI-DialogBox-Background-Dark",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 16,
        insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })

    local title = frame:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    title:SetPoint("TOPLEFT", frame, "TOPLEFT", 12, -10)
    title:SetText("WorldQuestTracker 日志（Ctrl+A 全选，Ctrl+C 复制）")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -6, -6)
    close:SetScript("OnClick", function() frame:Hide() end)

    local box = CreateFrame("EditBox", "WQTLogEditBox", frame)
    box:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -8)
    box:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -8, 8)
    box:SetMultiLine(true)
    box:SetFontObject("GameFontNormalSmall")
    box:SetAutoFocus(false)
    box:SetScript("OnEscapePressed", function(self) self:ClearFocus(); frame:Hide() end)
    frame.box = box

    frame:Hide()
    return frame
end

function Win.Show(text)
    local f = Ensure()
    local existing = f.box:GetText() or ""
    f.box:SetText((existing == "") and tostring(text or "") or (existing .. "\n\n" .. tostring(text or "")))
    f.box:SetCursorPosition(0)
    f:Show()
end

function Win.Toggle()
    local f = Ensure()
    if f:IsShown() then
        f:Hide()
    else
        Win.Show(WQT.Config.GetLogs())
    end
end

WQT.RegisterCommand("log", Win.Toggle, "打开/关闭日志窗")
```

- [ ] **Step 6: 更新 toc 并跑测试**

`WorldQuestTracker/WorldQuestTracker.toc` 末尾：

```
Core.lua
Config.lua
Ledger.lua
Api.lua
Probe.lua
LogWindow.lua
```

Run: `python tests/run_wqt_tests.py`
Expected: `24/24 单测通过`

Run: `python tests/run_wqt_tests.py --syntax`
Expected: 全部 `PASS syntax:...`（`LogWindow.lua` 顶层不调用 API，loadfile 只查语法；测试里通过 `mock=True` 提供 `CreateFrame`）

- [ ] **Step 7: 提交**

```bash
git status --short
git add WorldQuestTracker/Api.lua WorldQuestTracker/Probe.lua WorldQuestTracker/LogWindow.lua WorldQuestTracker/WorldQuestTracker.toc tests/run_wqt_tests.py
git commit -m "feat: API 适配层与 12.x 探测报告——接口缺失一律降级为未知，奖励取不到不当 0" -- WorldQuestTracker tests/run_wqt_tests.py
```

---

### Task 5: Sync —— 登录池快照与交任务钱差窗口

**Files:**
- Create: `WorldQuestTracker/Sync.lua`
- Modify: `WorldQuestTracker/WorldQuestTracker.toc`（末尾追加 `Sync.lua`）
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `WQT.Api.GetPool/QuestState/Money`、`WQT.Ledger.*`（Task 2–3）、`WQT.Config.DB/RefreshCycle/Log/FormatGold`、`WQT.RegisterCommand`
- Produces:
  - `WQT.Sync.frame`：Sync 自有事件帧（`WQTSyncFrame`），自己注册 `WORLD_QUEST_LOG_UPDATE`、`QUEST_COMPLETE`、`QUEST_TURNED_IN`
  - `WQT.Sync.OnWorldEnter()`：注册事件并安排一次去抖快照
  - `WQT.Sync.SnapshotNow() -> table|nil`：返回 `{ added, gone, renewed, sigChanged, poolCount, degrade }`；池不可用时只返回 `{ degrade = 原因 }` 且**不改账本**
  - `WQT.Sync.CurrentCharKey() -> "角色名-服务器"`
  - `WQT.Sync.BeginCapture(questID)` / `WQT.Sync.FinishWindow()`：钱差窗口，测试直接调 `FinishWindow` 不等 15 秒
  - `WQT.Sync.SetClickGuard(bool)` / `WQT.Sync.ClickGuard()`：阶段二手动补录开关，本阶段只提供状态存储，默认 false
  - 事件参数契约：`QUEST_COMPLETE` / `QUEST_TURNED_IN` 的首个参数为 questID。若 Task 6 实测发现不是，`BeginCapture(nil)` 会静默跳过、不影响其它流程，届时在 `BeginCapture` 里补回退取值。

- [ ] **Step 1: 写失败测试**

```python
def fire(wqt, event, *args):
    """经 Sync 自己的帧驱动事件，避免绕开真实分发路径。"""
    frame = wqt.Sync.frame
    return frame:GetScript("OnEvent")(frame, event, *args)


def sync_env(quests, started=None, completed=None, money=0, reset=700000,
             char=("小号一", "永恒之井"), visible=None):
    """搭好 Core+Config+Ledger+Api+Sync 的可控环境。

    quests: [(questID, money)]；money 为 None 表示奖励接口取不到。
    """
    lua, wqt = load_lua("Core", "Config", "Ledger", "Api", "Sync", mock=True)
    g = lua.globals()
    g.WQT_NOW = 0
    g.WQTDB = lua.table_from({})

    rows = lua.table_from({})
    for i, (qid, gold) in enumerate(quests, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if gold is not None:
            entry["money"] = gold
        rows[i] = lua.table_from(entry)
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    started = started or set()
    completed = completed or set()
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid in started,
        "IsQuestCompleted": lambda qid: qid in completed,
    })
    g.GetQuestResetTime = lambda: reset
    g.UnitName = lambda *a: char[0]
    g.GetRealmName = lambda: char[1]
    holder = lua.table_from({})
    g.WQT_MONEY = holder
    holder["v"] = money
    g.GetMoney = lambda: holder["v"]
    if visible:
        g.FrameIsVisible = lambda name: name in visible
    wqt.Config.Init()
    return lua, wqt, holder


def test_snapshot_records_added_and_views():
    """登录快照：新任务入表，本角色三态写进 views，只有真含金币的判金币任务。"""
    lua, wqt, _ = sync_env([(21, 400000), (22, 0)], started={22})
    res = wqt.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_records_added_and_views", f"快照应成功，实际 {res}")
    if len(res["added"]) != 2:
        return fail("snapshot_records_added_and_views", f"应有 2 个新任务，实际 {len(res['added'])}")
    key = str(wqt.Sync.CurrentCharKey())
    if key != "小号一-永恒之井":
        return fail("snapshot_records_added_and_views", f"角色 key 错误：{key}")
    views = lua.globals().WQTDB["chars"][key]["views"]
    if str(views[21]["s"]) != "open":
        return fail("snapshot_records_added_and_views", f"任务 21 应待做，实际 {views[21]['s']}")
    if str(views[22]["s"]) != "progress":
        return fail("snapshot_records_added_and_views", f"任务 22 应已接未交，实际 {views[22]['s']}")
    if len(wqt.Ledger.GoldQuestIDs(lua.globals().WQTDB)) != 1:
        return fail("snapshot_records_added_and_views", "只有任务 21 应判金币任务")
    ok("snapshot_records_added_and_views")


def test_snapshot_degrades_to_unknown():
    """完成状态接口缺失：views 写 unknown 而非 open，degrade 带原因。"""
    lua, wqt, _ = sync_env([(31, None)])
    g = lua.globals()
    g.C_QuestLog = None      # 状态接口整体不可用
    res = wqt.Sync.SnapshotNow()
    if res is None:
        return fail("snapshot_degrades_to_unknown", "缺状态接口不应整体失败")
    key = str(wqt.Sync.CurrentCharKey())
    st = str(g.WQTDB["chars"][key]["views"][31]["s"])
    if st != "unknown":
        return fail("snapshot_degrades_to_unknown", f"应写 unknown，实际 {st}")
    ok("snapshot_degrades_to_unknown")


def test_pool_unavailable_keeps_ledger():
    """池接口不可用：不动账本，不把全部任务判成消失。"""
    lua, wqt, _ = sync_env([(41, 500000)])
    g = lua.globals()
    wqt.Sync.SnapshotNow()
    before = int(g.WQTDB["quests"][41]["lastSeenAt"])
    g.C_WorldQuests = None           # 接口整个消失
    res = wqt.Sync.SnapshotNow()
    if res is None or res["degrade"] is None:
        return fail("pool_unavailable_keeps_ledger", "应返回降级原因")
    if int(g.WQTDB["quests"][41]["lastSeenAt"]) != before:
        return fail("pool_unavailable_keeps_ledger", "池不可用时账本不应变化")
    if g.WQTDB["quests"][41]["currentSig"] is None:
        return fail("pool_unavailable_keeps_ledger", "不应把读不到当成任务消失")
    ok("pool_unavailable_keeps_ledger")


def test_turnin_money_window_capture_and_abort():
    """交任务：窗口内钱差入账；期间开过邮箱则作废记 nil（未知），不记可疑数字。"""
    lua, wqt, money = sync_env([(51, 500000)], visible={"MailFrame"})
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    g = lua.globals()
    money["v"] = 1000000

    fire(wqt, "QUEST_COMPLETE", 51)
    money["v"] = 1600000
    g.FrameIsVisible = lambda name: False
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][51]
    if rows is None or len(rows) != 1:
        return fail("turnin_money_window_capture_and_abort", f"应写 1 条记录，实际 {rows}")
    if int(rows[1]["g"]) != 600000:
        return fail("turnin_money_window_capture_and_abort", f"增量应 600000，实际 {rows[1]['g']}")
    if str(wqt.Ledger.State(g.WQTDB, key, 51)) != "done":
        return fail("turnin_money_window_capture_and_abort", "交完应显示已交")

    fire(wqt, "QUEST_COMPLETE", 51)
    money["v"] = 2200000
    g.FrameIsVisible = lambda name: name == "MailFrame"
    g.WQT_NOW = 40
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][51]
    if len(rows) != 2:
        return fail("turnin_money_window_capture_and_abort", "第二次也应入账（新一轮）")
    if rows[2]["g"] is not None:
        return fail("turnin_money_window_capture_and_abort", "窗口内开过邮箱应放弃测量记 nil")
    ok("turnin_money_window_capture_and_abort")


def test_debounce_single_snapshot():
    """世界任务更新事件连发只抓一次池（2 秒去抖）。"""
    lua, wqt, _ = sync_env([(61, 100000)])
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    wqt.Sync.OnWorldEnter()
    for _ in range(5):
        fire(wqt, "WORLD_QUEST_LOG_UPDATE")
    g.WQT_NOW = 3
    lua.globals().WQT_FireTimers()
    if len(calls) != 1:
        return fail("debounce_single_snapshot", f"去抖后应只抓一次池，实际 {len(calls)}")
    ok("debounce_single_snapshot")
```

追加 5 个函数名到 `tests` 列表。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: `FileNotFoundError: ...Sync.lua`

- [ ] **Step 3: 写 Sync.lua**

创建 `WorldQuestTracker/Sync.lua`：

```lua
-- 同步层：登录抓世界任务池并 diff；交任务事件 + 15 秒钱差窗口记账。
-- 所有 WoW API 经 WQT.Api，本文件只管编排与时序。

WQT.Sync = {}
local Sync = WQT.Sync

local L = WQT.Ledger
local DEBOUNCE_SECONDS = 2
local MONEY_WINDOW_SECONDS = 15
-- 窗口内出现过这些帧，说明可能有别的资金进出，本次测量作废
local UNTRUSTED_FRAMES = { "MailFrame", "AuctionFrame", "GuildBankFrame", "TradeFrame" }

local clickGuard = false
local debouncePending = false
local capture = nil          -- { questID, before, t0 }

function Sync.ClickGuard() return clickGuard end
function Sync.SetClickGuard(v) clickGuard = not not v end

local function Now()
    return math.floor((GetTime and GetTime()) or 0)
end

function Sync.CurrentCharKey()
    local name = "player"
    if UnitName then
        local okCall, v = pcall(UnitName, "player")
        if okCall and v then name = tostring(v) end
    end
    local realm = ""
    if GetRealmName then
        local okCall, v = pcall(GetRealmName)
        if okCall and v then realm = tostring(v) end
    end
    return name .. "-" .. realm
end

--- 抓池 → diff → 写本角色快照。池不可用时直接返回，不做任何 diff：
--- 把"读不到"当成"全部消失"会清空整张账本，那比不更新严重得多。
function Sync.SnapshotNow()
    local db = WQT.Config.DB()
    if not db then return nil end
    local pool, err = WQT.Api.GetPool()
    if not pool then
        WQT.Config.Log("快照跳过：池不可用 " .. tostring(err))
        return { degrade = tostring(err) }
    end

    local now = Now()
    WQT.Config.RefreshCycle()
    if db.cycleAnchor == 0 then
        WQT.Config.Log("cycleAnchor 未知：周统计口径可能不准")
    end
    local res = L.DiffPool(db, pool, now)

    local charKey = Sync.CurrentCharKey()
    db.chars[charKey] = db.chars[charKey] or { views = {} }
    local row = db.chars[charKey]
    row.lastSync = now
    row.views = row.views or {}
    if UnitClass then
        local okCall, _, classToken = pcall(UnitClass, "player")
        if okCall and classToken then row.class = classToken end
    end
    if UnitLevel then
        local okCall, level = pcall(UnitLevel, "player")
        if okCall and level then row.level = level end
    end

    local inPool = {}
    for i = 1, #pool do
        local questID = pool[i].questID
        inPool[questID] = true
        local state = { WQT.Api.QuestState(questID) }
        local obs = L.GetObs(db, questID)
        local rows = db.turnins[charKey] and db.turnins[charKey][questID]
        local last = rows and rows[#rows]
        if obs and last and last.t >= obs.firstSeen then
            state[1] = "done"     -- 本轮亲眼记到的交任务优先于查询接口
        end
        row.views[questID] = { s = state[1], t = now }
    end
    for questID in pairs(db.quests) do
        if not inPool[questID] then
            row.views[questID] = { s = "ended", t = now }
        end
    end

    res.poolCount = #pool
    WQT.Config.Log(("快照完成 池=%d 新增=%d 消失=%d 重现=%d 奖励变化=%d 锚点=%d 角色=%s")
        :format(#pool, #res.added, #res.gone, #res.renewed, #res.sigChanged,
            db.cycleAnchor or 0, charKey))
    return res
end

local function ScheduleSnapshot()
    if debouncePending then return end
    debouncePending = true
    C_Timer.After(DEBOUNCE_SECONDS, function()
        debouncePending = false
        local okCall, err = pcall(Sync.SnapshotNow)
        if not okCall then WQT.Config.Log("快照异常: " .. tostring(err)) end
    end)
end

function Sync.OnWorldEnter()
    Sync.frame:RegisterEvent("WORLD_QUEST_LOG_UPDATE")
    Sync.frame:RegisterEvent("QUEST_COMPLETE")
    Sync.frame:RegisterEvent("QUEST_TURNED_IN")
    ScheduleSnapshot()
end

local function FrameVisible(name)
    if not FrameIsVisible then return false end
    local okCall, v = pcall(FrameIsVisible, name)
    return okCall and v == true
end

local function MoneyUntrusted()
    for i = 1, #UNTRUSTED_FRAMES do
        if FrameVisible(UNTRUSTED_FRAMES[i]) then return true end
    end
    return false
end

--- 窗口结束才结算：净增量归这个任务，于是任务直接给的金币和随后
--- 立刻开宝箱给的金币一次全收进来（宝箱数额随机，这里取实测近似）。
function Sync.FinishWindow()
    local c = capture
    capture = nil
    if not c then return end
    local after = WQT.Api.Money()
    local gain = nil
    if type(c.before) == "number" and type(after) == "number" and not MoneyUntrusted() then
        local delta = after - c.before
        -- 只认进账；同期有出账（买了东西）说明窗口被污染，整笔作废
        if delta >= 0 then gain = delta end
    end
    local db = WQT.Config.DB()
    if not db then return end
    local charKey = Sync.CurrentCharKey()
    local rec = db.quests[c.questID]
    L.OnTurnIn(db, charKey, c.questID, c.t0, gain, rec and rec.currentSig or nil)
    db.chars[charKey] = db.chars[charKey] or { views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    db.chars[charKey].views[c.questID] = { s = "done", t = c.t0 }
    WQT.Config.Log(("交任务 quest=%s 入账=%s 窗口污染=%s")
        :format(tostring(c.questID), WQT.Config.FormatGold(gain),
            tostring(gain == nil and (type(c.before) == "number") or true)))
end

function Sync.BeginCapture(questID)
    questID = tonumber(questID)
    if not questID then return end
    capture = { questID = questID, before = WQT.Api.Money(), t0 = Now() }
    C_Timer.After(MONEY_WINDOW_SECONDS, Sync.FinishWindow)
end

Sync.frame = CreateFrame("Frame", "WQTSyncFrame", UIParent)
Sync.frame:SetScript("OnEvent", function(self, event, arg1)
    if event == "WORLD_QUEST_LOG_UPDATE" then
        ScheduleSnapshot()
    elseif event == "QUEST_COMPLETE" or event == "QUEST_TURNED_IN" then
        Sync.BeginCapture(arg1)
    end
end)

WQT.RegisterCommand("sync", function()
    local res = Sync.SnapshotNow()
    if not res then
        print("WQT: 同步失败，详情 /wqt log")
    elseif res.degrade then
        print("WQT: 池不可用（" .. res.degrade .. "），账本保持不变")
    else
        print(("WQT: 池 %d 条，新增 %d，消失 %d，重现 %d，奖励变化 %d")
            :format(res.poolCount, #res.added, #res.gone, #res.renewed, #res.sigChanged))
    end
end, "立即重跑一次池快照 diff")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python tests/run_wqt_tests.py`
Expected: `29/29 单测通过`

若 `test_turnin_money_window_capture_and_abort` 报"第二次也应入账"失败：原因是 `QUEST_TURNED_IN` 与 `QUEST_COMPLETE` 会各自触发一次 `BeginCapture`，第二次覆盖 capture 后只入账一次。测试里只 fire `QUEST_COMPLETE`，不应出现该问题；若在真实游戏里两个事件都触发，保留 `BeginCapture` 里"已有未结算 capture 就直接返回"的守卫：

```lua
    if capture then return end
```

- [ ] **Step 5: 更新 toc、语法检查并提交**

`WorldQuestTracker/WorldQuestTracker.toc` 末尾追加 `Sync.lua`：

```
Core.lua
Config.lua
Ledger.lua
Api.lua
Probe.lua
LogWindow.lua
Sync.lua
```

```bash
python tests/run_wqt_tests.py --syntax
git status --short
git add WorldQuestTracker/Sync.lua WorldQuestTracker/WorldQuestTracker.toc tests/run_wqt_tests.py
git commit -m "feat: Sync 登录池快照与交任务钱差窗口——池不可用时保账本、窗口不净则记未知" -- WorldQuestTracker tests/run_wqt_tests.py
```

---

### Task 6: 只读导出与游戏内验收清单

**Files:**
- Create: `WorldQuestTracker/Dump.lua`
- Modify: `WorldQuestTracker/WorldQuestTracker.toc`（末尾追加 `Dump.lua`）
- Create: `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`
- Test: `tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: 前 5 个任务的全部产出
- Produces:
  - `WQT.Dump.Run() -> string`：三段文本 —— 每角色本周完成数与金币总额、金币任务 × 角色状态表、降级与未知计数
  - `/wqt dump`（注册到 `WQT.commands`）

- [ ] **Step 1: 写失败测试**

```python
def test_dump_sections():
    """dump 应含角色汇总、任务行、列头角色名、降级与未知计数。"""
    lua, wqt, _ = sync_env([(71, 250000), (72, None)])
    g = lua.globals()
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 5
    g.WQT_FireTimers()          # 走真实去抖路径，而不是直调 SnapshotNow
    text = str(wqt.Dump.Run())
    if "本周汇总" not in text:
        return fail("dump_sections", "缺少角色汇总段")
    if "任务71" not in text:
        return fail("dump_sections", "缺少金币任务行")
    if "小号一-永恒之井" not in text:
        return fail("dump_sections", "缺少列头角色名")
    if "未判定=1" not in text:
        return fail("dump_sections", "奖励接口取不到的任务 72 应计入未判定")
    if "降级" not in text:
        return fail("dump_sections", "缺少降级计数段")
    if wqt.commands["dump"] is None:
        return fail("dump_sections", "/wqt dump 未注册")
    ok("dump_sections")


def test_dump_shows_stale_char():
    """两周没上线的角色应标注快照过期，而不是显示成全部没做。"""
    lua, wqt, _ = sync_env([(71, 250000)])
    g = lua.globals()
    wqt.Sync.SnapshotNow()
    g.WQT_NOW = 20 * 86400
    g.UnitName = lambda *a: "挂机号"
    g.GetRealmName = lambda: "永恒之井"
    wqt.Sync.SnapshotNow()
    g.WQT_NOW = 20 * 86400 + 14 * 86400
    text = str(wqt.Dump.Run())
    if "过期" not in text:
        return fail("dump_shows_stale_char", "久未同步的角色应显示过期标记")
    ok("dump_shows_stale_char")
```

追加 2 个函数名到 `tests` 列表，并把 `"Dump"` 加进上面两个用例的 `load_lua` 模块序列（`sync_env` 需要多载一个模块，见 Step 3 对 `sync_env` 的修改）。

- [ ] **Step 2: 运行测试确认失败**

Run: `python tests/run_wqt_tests.py`
Expected: `FileNotFoundError: ...Dump.lua`

- [ ] **Step 3: 写 Dump.lua**

创建 `WorldQuestTracker/Dump.lua`：

```lua
-- 阶段一只读导出：矩阵 UI 属阶段二，本阶段用文本确认账本记录的是真实情况。

WQT.Dump = {}
local Dump = WQT.Dump

local STATUS_TEXT = {
    done = "已交", progress = "在办", open = "待做",
    ended = "本轮结束", unknown = "?",
}
local STALE_SECONDS = 7 * 86400

function Dump.Run()
    local db = WQT.Config.DB()
    local L = WQT.Ledger
    local now = math.floor((GetTime and GetTime()) or 0)
    local lines = { ("WQT 账本导出 cycleAnchor=%s now=%d"):format(tostring(db.cycleAnchor), now) }

    local chars = L.Characters(db)

    lines[#lines + 1] = "== 本周汇总 =="
    if #chars == 0 then lines[#lines + 1] = "（还没有任何角色同步过）" end
    for i = 1, #chars do
        local charKey = chars[i]
        local count, gold = L.WeekStats(db, charKey, db.cycleAnchor or 0)
        local lastSync = db.chars[charKey].lastSync or 0
        local stale = (now - lastSync) > STALE_SECONDS and "（快照过期）" or ""
        lines[#lines + 1] = ("%s  完成 %d  金币 %s  同步于 %d%s"):format(
            charKey, count, WQT.Config.FormatGold(gold < 0 and nil or gold), lastSync, stale)
    end

    lines[#lines + 1] = "== 金币任务 =="
    local quests = L.GoldQuestIDs(db)
    if #quests == 0 then
        lines[#lines + 1] = "（无金币任务：奖励接口可能不可用，见下方未判定计数）"
    end
    for i = 1, #quests do
        local questID = quests[i]
        local cells = {}
        for c = 1, #chars do
            local st = L.State(db, chars[c], questID)
            cells[#cells + 1] = STATUS_TEXT[st] or tostring(st)
        end
        lines[#lines + 1] = ("%s%s [%s]"):format(
            L.GetQuestName(db, questID) or tostring(questID),
            L.IsHighFreq(db, questID) and "（高频）" or "",
            table.concat(cells, " | "))
    end

    local undetermined, unknownCells = 0, 0
    for questID in pairs(db.quests) do
        local obs = L.GetObs(db, questID)
        if obs and not obs.gold and obs.money == nil then undetermined = undetermined + 1 end
    end
    for _, charKey in ipairs(chars) do
        for questID in pairs(db.quests) do
            if L.State(db, charKey, questID) == "unknown" then unknownCells = unknownCells + 1 end
        end
    end
    lines[#lines + 1] = ("== 降级与未知 == 未判定=%d 未知格=%d 最近降级=%s")
        :format(undetermined, unknownCells, tostring(WQT.Api.LastDegrade()))

    local text = table.concat(lines, "\n")
    WQT.Config.Log("dump 完成")
    if WQT.LogWindow and WQT.LogWindow.Show then WQT.LogWindow.Show(text) end
    print(text)
    return text
end

WQT.RegisterCommand("dump", Dump.Run, "文本导出账本（阶段一验收用）")
```

同时把 Task 5 的 `sync_env` 里那句 `load_lua(...)` 改成带可选模块的形态（Step 1 的两个用例依赖它）：

```python
def sync_env(quests, started=None, completed=None, money=0, reset=700000,
             char=("小号一", "永恒之井"), visible=None, extra=()):
    lua, wqt = load_lua("Core", "Config", "Ledger", "Api", "Sync", *extra, mock=True)
```

并把两个 dump 用例调用为 `sync_env([...], extra=("Dump",))`。

- [ ] **Step 4: 运行全部测试**

Run: `python tests/run_wqt_tests.py`
Expected: `31/31 单测通过`

Run: `python tests/run_wqt_tests.py --syntax`
Expected: 全部 `PASS syntax:...`

- [ ] **Step 5: 写游戏内验收清单**

创建 `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`：

```markdown
# WorldQuestTracker 阶段一游戏内验收

安装：把 `WorldQuestTracker` 整个文件夹复制到 `_retail_/Interface/AddOns/`，`/reload`。

回传方式统一：`/wqt log` 打开日志窗 → 点进文本框 → Ctrl+A → Ctrl+C → 粘贴。**不要截图**，日志窗里的文本直接可读。

## 1. `/wqt probe`

判据与分支：
- 候选里有 `AVAILABLE C_WorldQuests.GetWorldQuests -> AVAILABLE table[#N]{questID,...}` 且下面"池条目数" > 0 → 池这条路成立，继续第 2 步。
- 全部 `MISSING` → 12.x 换了别的入口。只需回传报告，我据 `table[...]{keys}` 里的键名定新接口；**不要继续后面的步骤**，后面每一步都依赖池数据。
- 样本行里 `money=nil` → 奖励金币接口不可用。这是 spec 第 12 节的退化 2：金币判定退化成"做过一次才自动转正"，未判定任务落在 dump 底部计数里。属预期结果，不是 bug，但要如实回传。
- `state=?` → 完成状态接口不可用（退化 1），矩阵将来会把这类格子显示成 `?` 而不是"待做"。

## 2. `/wqt sync`

打印"池 N 条，新增 X，消失 Y，重现 Z，奖励变化 W"。
- `新增` 应约等于你在地图上看得到金币世界任务的数量。
- `池 0 条` 或 `池不可用` → 接口存在但返回形状不对，回传第 1 步报告里的 keys 即可。

## 3. `/wqt dump`

判据：
- `== 本周汇总 ==` 出现你的角色，金币那栏**不是 `?`**（`?` 表示还没记到过任何实测值，属正常初始状态）。
- 接一个世界任务并交掉，等 20 秒（钱差窗口 15 秒），再 `/wqt dump`：该任务行该角色应变成 `已交`，本周汇总 `完成 1`，金币显示实际到账数额。
- 若交完仍显示 `待做`：说明 12.x 的 `QUEST_COMPLETE` 首个参数不再是 questID。回传日志（`BeginCapture` 拿到 nil 时是静默跳过的），我在 `BeginCapture` 里补回退取值。
- 若金币显示 `?`：确认这 15 秒里没打开邮箱/拍卖行；`GetMoney` 不可用时探测报告里 Money 会是 `nil(null)`，一并回传。

## 4. 换角色登录

判据：`== 本周汇总 ==` 出现第二行；第一个角色那列仍是他上次登录时的状态。这是 API 硬限制（spec 第 3 节），不是同步失败。超过 7 天未登录的角色会带 `（快照过期）`。

## 5. 高频区（屠戮者高地 / 虚影风暴）

等一个 12 小时任务刷新后再登录，`/wqt sync` 然后 `/wqt dump`。
判据：日志出现 `消失 n` 与 `重现 m`；该任务行出现 `（高频）`；**上一轮的已交不能显示成这一轮的已交**。

## 6. 狩猎 / 普通模式差异

同一个任务在两种模式下各登录一次。
判据：日志出现 `奖励变化 1`；dump 里该任务只要任一模式给过金币就仍留在金币任务表里（另一模式的 observation 保留着）。

## 回传内容

日志窗全文（含 probe 报告 + 每次 sync/dump 输出）。阶段二的 UI 能自动填哪些格子、哪些必须靠地图气泡点击补录，完全由这份实测结果决定。
```

- [ ] **Step 6: 更新 toc 并提交**

`WorldQuestTracker/WorldQuestTracker.toc` 末尾追加 `Dump.lua`：

```
Core.lua
Config.lua
Ledger.lua
Api.lua
Probe.lua
LogWindow.lua
Sync.lua
Dump.lua
```

```bash
git status --short
git add WorldQuestTracker/Dump.lua WorldQuestTracker/WorldQuestTracker.toc docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md tests/run_wqt_tests.py
git commit -m "feat: /wqt dump 只读导出与游戏内验收清单——阶段一先证实账本再接 UI" -- WorldQuestTracker docs/superpowers/tasks tests/run_wqt_tests.py
```

---

## 阶段二（不在本计划内，等 Task 6 的游戏内结果）

- `Matrix.lua`：任务 × 角色矩阵、列头本周完成数与金币合计、底部账号合计、"未判定"分组、只看未完成筛选
- `MapOverlay.lua`：世界任务气泡上叠角色头像层，点头像切状态（手动写入走 `Ledger.SetManual`，`source=manual` 不被登录同步覆盖）
- `Tooltip.lua`：完整奖励、剩余时间、分模式 observation、每角色最近一次实际到账金币
- 矩阵内批量与撤销 UI：点列头选目标组、整列一键标完、整行一键、撤销栈接 `Ledger.UndoApply`

这些任务的形态取决于 Task 6 实测：哪些格子能自动填、哪些只能靠点击。现在写死会返工，故不在本计划展开。

## 验收对照（spec → 本计划）

| spec 要求 | 落在哪个任务 |
|---|---|
| 第 4 节 钱差窗口记金币（含宝箱近似） | Task 5 |
| 第 5 节 周 CD 锚点从服务端反推、只前进 | Task 1 |
| 第 6 节 A 事件记账 | Task 5 |
| 第 6 节 B 登录池 diff（新出现/消失/签名变化） | Task 2 + Task 5 |
| 第 6 节 C 手动优先、自动不覆盖 | Task 3（`SetManual`/`State`/`goldLock`），UI 在阶段二 |
| 第 7 节 数据模型（observations 按签名、turnins 追加日志） | Task 2 + Task 3 |
| 第 10 节 API 缺失降级、体积清理、改名提示 | 降级 Task 4、清理 Task 3、改名提示属阶段二 |
| 第 11 节 验收 1/2/3/4/6/7/8 | Task 6 清单第 1–6 步 + 各任务单测 |
| 第 12 节 探测先行与两类退化 | Task 4 + Task 6 清单 |

