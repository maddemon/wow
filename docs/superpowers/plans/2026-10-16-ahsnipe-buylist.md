# AHSnipe 可购买列表 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用常驻非模态"可购买列表"替代狙击弹窗：命中加行、重复命中静默刷新，唯一操作是【买队首】按钮。

**Architecture:** 新模块 `AHSnipe/SnipeList.lua`（行管理 + 渲染 + 买队首入口）；Scanner 命中处调用 `Add`、新鲜数据证实不命中处调用 `MarkGone`；BuyQueue 结算回调 `OnBuyFinished` 删行/恢复按钮；删除 SnipeWindow.lua 及按键绑定/闪烁/连响整套旧交互层。BuyQueue 购买内核不动。

**Tech Stack:** WoW 正式服 Lua（Interface 120100/120105）；测试为 Python+lupa（`python tests/run_tests.py --syntax`）。

规格：`docs/superpowers/specs/2026-10-16-ahsnipe-buylist-design.md`

## Global Constraints

- 仅正式服：`WOW_PROJECT_MAINLINE`（Core 已有守护，不动）。
- 购买 API 只允许在硬件事件栈（按钮 OnClick）或其直接下游的刷新回调栈内调用；**任何计时器/延迟上下文的购买调用都会被客户端静默丢弃（已实证）**。不做自动购买、不做"记下点击稍后补发"。
- 全部测试必须通过后才允许提交：`python tests/run_tests.py --syntax`（预期末行 `全部通过`、退出码 0）。
- 每个任务一次 git 提交，中文 conventional message（feat/fix/refactor/docs: 描述）。
- 日志一律 `Config.Log`；用户可见 print 文案颜色：成功 `|cff33FF99`、失败 `|cffFF4040`。
- 12.x API 形态不确定处必须 pcall 包裹并失败留痕（只记一次，防刷屏）。
- hit 数据结构（Scanner.EvaluateHit 产出，已有字段）：`itemID, name, threshold, isCommodity, matched[{unitPrice,quantity,(auctionID,buyoutAmount)}], totalQuantity, totalPrice, isBaitLike, roundStartAt, querySendAt, seenAt`。

---

### Task 1: Config 字段迁移（listPos 新增，bindKey/flashAlert/repeatPing 废弃）

**Files:**
- Modify: `AHSnipe/Config.lua`（头部注释 L6-8、DefaultDB L39-41、Init L65-67、getter/setter L126-150）
- Test: `tests/run_tests.py`（新增 `test_config_migration` 并登记到 `tests` 列表）

**Interfaces:**
- Consumes: 无
- Produces: `Config.GetListPos() -> nil | {point=string, x=number, y=number}`；`Config.SetListPos(point, x, y)`；DB 中不再存在 `bindKey/flashAlert/repeatPing`。Task 2/5 依赖这两个访问器。

- [ ] **Step 1: 写失败测试**

在 `tests/run_tests.py` 顶部常量区（`BUY_QUEUE_PATH` 之后）加：

```python
CONFIG_PATH = os.path.join(PLUGIN_DIR, "Config.lua")
```

在 `test_commodity_chunks_filter` 之后加：

```python
def test_config_migration():
    """旧提醒/绑定字段被清除，interval 旧值迁移为 0。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    g.AHSnipe = lua.table_from({})
    g.AHSnipeDB = lua.table_from({
        "interval": 8, "discount": 0.65,
        "items": lua.table_from({}), "logs": lua.table_from({}),
        "lastPrices": lua.table_from({}), "paused": False, "diagAH": False,
        "bindKey": "F", "flashAlert": True, "repeatPing": True,
    })
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    g.AHSnipe.Config.Init()
    db = g.AHSnipeDB
    if db["bindKey"] is not None or db["flashAlert"] is not None or db["repeatPing"] is not None:
        return fail("config_migration", "旧绑定/提醒字段应被清除")
    if db["interval"] != 0:
        return fail("config_migration", "旧 interval=8 应迁移为 0")
    ok("config_migration")
```

并把 `test_config_migration` 加进 `__main__` 的 `tests = [...]` 列表。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/run_tests.py`
Expected: `FAIL config_migration: 旧绑定/提醒字段应被清除`，退出码 1。

- [ ] **Step 3: 实现**

`Config.lua` 头部注释 L6-8 三行（bindKey/flashAlert/repeatPing 说明）替换为：

```lua
--   listPos    = nil       可购买列表窗位置（拖动后持久化 {point,x,y}）
```

DefaultDB 中删掉这三行：

```lua
        bindKey = nil,      -- 一键购买按键（/ahsnipe bind 设置，加载时重新 SetBindingClick）
        flashAlert = true,  -- 命中时整屏闪烁 + 屏幕中央横幅
        repeatPing = true,  -- 弹窗未关闭期间连续重播提示音
```

Init 中把这五行：

```lua
    if type(db.bindKey) ~= "string" then db.bindKey = nil end
    if type(db.flashAlert) ~= "boolean" then db.flashAlert = true end
    if type(db.repeatPing) ~= "boolean" then db.repeatPing = true end
```

（以及同段落对这三个字段的类型校验，若有）替换为：

```lua
    -- 旧弹窗时代的绑定/提醒字段整体废弃（可购买列表替代，规格 2026-10-16）
    db.bindKey = nil
    db.flashAlert = nil
    db.repeatPing = nil
    if type(db.listPos) == "table" and type(db.listPos.point) ~= "string" then
        db.listPos = nil
    end
```

把 L126-150 的 `GetBindKey/SetBindKey/IsFlashAlert/SetFlashAlert/IsRepeatPing/SetRepeatPing` 六个函数整段替换为：

```lua
-- 可购买列表窗位置（拖动结束由 SnipeList 写入；空则创建时用默认锚点）
function Config.GetListPos()
    local p = AHSnipeDB.listPos
    if type(p) == "table" and type(p.point) == "string" then return p end
    return nil
end

function Config.SetListPos(point, x, y)
    AHSnipeDB.listPos = { point = point, x = x, y = y }
end
```

- [ ] **Step 4: 运行确认通过**

Run: `python tests/run_tests.py --syntax`
Expected: `PASS config_migration`，全部 PASS，退出码 0。

- [ ] **Step 5: 提交**

```bash
git add AHSnipe/Config.lua tests/run_tests.py
git commit -m "feat(config): 新增列表窗位置字段，废弃绑定/闪烁/连响旧字段"
```

---

### Task 2: 新模块 SnipeList.lua（行管理 + 渲染 + 买队首）

**Files:**
- Create: `AHSnipe/SnipeList.lua`
- Modify: `AHSnipe/AHSnipe.toc`（`SnipeWindow.lua` 行后加一行 `SnipeList.lua`，本任务保留旧文件）
- Test: `tests/run_tests.py`（新增 `load_snipe_list`/`make_hit`/`test_snipe_list`/`test_snipe_list_buy`，登记到 `tests` 列表）

**Interfaces:**
- Consumes: Task 1 的 `Config.GetListPos/SetListPos`；`Config.Log`、`Config.GetItemDisplayName`；hit 结构见 Global Constraints。
- Produces（后续任务的调用面）：
  - `AHSnipe.SnipeList.Init()`（Task 5 由 Core 调用；注册 AUCTION_HOUSE_SHOW/CLOSED 自管理显隐）
  - `AHSnipe.SnipeList.Add(hit)`、`AHSnipe.SnipeList.MarkGone(itemID, reason|nil)`
  - `AHSnipe.SnipeList.OnBuyFinished(summary)`（Task 4 由 BuyQueue 调用；summary 字段 `itemID,bought,failed,deferred`）
  - `AHSnipe.SnipeList.TriggerBuy() -> boolean`
  - `AHSnipe.SnipeList.UpdateBuyButton()`
  - 测试用：`List.RowCount() -> number`、`List.GetFront() -> hit|nil`

- [ ] **Step 1: 写失败测试**

`tests/run_tests.py` 常量区加：

```python
SNIPE_LIST_PATH = os.path.join(PLUGIN_DIR, "SnipeList.lua")
```

在 `test_config_migration` 后加：

```python
def load_snipe_list():
    """独立 mock 环境加载 SnipeList.lua（复用 MOCK_ENV），返回模块与记录器。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    logs = []
    pings = []
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda m: logs.append(str(m)),
            "GetItemDisplayName": lambda i: "物品%s" % i,
            "GetListPos": lambda: None,
            "SetListPos": lambda *a: None,
        }),
    })
    g.PlaySound = lambda *a: pings.append(1)
    with open(SNIPE_LIST_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.SnipeList, logs, pings


def make_hit(lua, item_id, unit, qty, total, seen_at, commodity=True):
    return lua.table_from({
        "itemID": item_id, "name": "物品%s" % item_id, "threshold": unit * 3,
        "isCommodity": commodity, "totalQuantity": qty, "totalPrice": total,
        "isBaitLike": False,
        "matched": lua.table_from([
            lua.table_from({"unitPrice": unit, "quantity": qty})]),
        "roundStartAt": seen_at - 3, "querySendAt": seen_at - 1, "seenAt": seen_at,
    })


def test_snipe_list():
    """加行/原地刷新/新鲜度排序/删行。"""
    lua, lst, logs, pings = load_snipe_list()
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:add", "首次命中应加一行并响一次提示音")
    lst.Add(make_hit(lua, 101, 51000, 9, 459000, 98))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:refresh", "同物品应原地刷新且不重响")
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    if lst.RowCount() != 2:
        return fail("snipe_list:sort", "两个物品应有两行")
    if int(lst.GetFront()["itemID"]) != 102:
        return fail("snipe_list:sort", "队首应为 seenAt 最新的 102")
    lst.MarkGone(102, "测试")
    if lst.RowCount() != 1 or int(lst.GetFront()["itemID"]) != 101:
        return fail("snipe_list:gone", "MarkGone 应删除指定行")
    ok("snipe_list")


def test_snipe_list_buy():
    """买队首 = 买最新鲜一条；忙时拒绝；整单成功删行。"""
    lua, lst, logs, pings = load_snipe_list()
    g = lua.globals()
    buys = []
    busy = {"v": False}
    g.AHSnipe["BuyQueue"] = lua.table_from({
        "IsBusy": lambda: busy["v"],
        "Buy": lambda hit: (buys.append(int(hit["itemID"])) or True),
    })
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    lst.TriggerBuy()
    if buys != [102]:
        return fail("snipe_list_buy", f"应买队首102，实际 {buys}")
    busy["v"] = True
    if lst.TriggerBuy() or buys != [102]:
        return fail("snipe_list_buy", "购买在途应拒绝再次点击")
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 102, "bought": 1, "failed": 0, "deferred": False}))
    if lst.RowCount() != 1:
        return fail("snipe_list_buy", "整单成功后应删除该行")
    busy["v"] = False
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 101, "bought": 0, "failed": 1, "deferred": False}))
    if lst.RowCount() != 1:
        return fail("snipe_list_buy", "购买失败不应删行（等扫描刷新/证实失效）")
    ok("snipe_list_buy")
```

把两个测试函数登记进 `__main__` 的 `tests` 列表。

- [ ] **Step 2: 运行确认失败**

Run: `python tests/run_tests.py`
Expected: `FileNotFoundError`（SnipeList.lua 不存在）或 FAIL。

- [ ] **Step 3: 实现 AHSnipe/SnipeList.lua（完整文件）**

```lua
-- 可购买列表（替代旧狙击弹窗）。规格：docs/superpowers/specs/2026-10-16-ahsnipe-buylist-design.md
-- 命中加行、同物品原地静默刷新；唯一操作 = 【买队首】，购买在途按钮置灰。
-- 删行依据：扫描用新鲜数据证实无低价（Scanner.MarkGone）、整单买成功（BuyQueue→OnBuyFinished）、
-- 或关闭拍卖行（过期快照不许再点）。
-- 不做"记下点击稍后自动买下一条"：计时器/事件上下文的购买消息会被客户端静默丢弃（已实证）。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local Log = Config.Log

AHSnipe.SnipeList = {}
local List = AHSnipe.SnipeList

local frame = nil
local rows = {}                  -- hit 数组，恒按 seenAt 降序（[1]=队首=最新鲜）
local buyingItemID = nil         -- 购买在途物品ID（行前缀"购买中"）
local pingLogged = false
local iconLogged = false

local ROW_H = 19
local coin = function(c) return C_CurrencyInfo.GetCoinTextureString(c or 0) end

local function MinUnit(hit)
    local m = nil
    for _, l in ipairs(hit.matched) do
        if not m or l.unitPrice < m then m = l.unitPrice end
    end
    return m or 0
end

local function PlayPing()
    -- 沿用旧弹窗提示音（游戏内已验证会响）；SOUNDKIT 字段名随版本变动，失败只记一次
    local ok, err = pcall(PlaySound, (SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING) or 882)
    if not ok and not pingLogged then
        pingLogged = true
        Log(("提示音播放失败：%s"):format(tostring(err)))
    end
end

local function EnsureRowPool(f, n)
    f.rowFS = f.rowFS or {}
    while #f.rowFS < n do
        local w = {}
        w.icon = f:CreateTexture(nil, "OVERLAY")
        w.name = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.unit = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.qty = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.total = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        f.rowFS[#f.rowFS + 1] = w
    end
end

function List.UpdateBuyButton()
    if not frame or not frame.buyBtn then return end
    local BQ = AHSnipe.BuyQueue
    if BQ and BQ.IsBusy() then
        frame.buyBtn:Disable()
        frame.buyBtn:SetText("购买中…")
    elseif #rows > 0 then
        frame.buyBtn:Enable()
        frame.buyBtn:SetText("买队首")
    else
        frame.buyBtn:Disable()
        frame.buyBtn:SetText("买队首")
    end
end

local function Render()
    if not frame then return end
    table.sort(rows, function(a, b) return (a.seenAt or 0) > (b.seenAt or 0) end)
    local f = frame
    EnsureRowPool(f, #rows)
    for i = 1, math.max(#rows, f.lastRows or 0) do
        local w, hit = f.rowFS[i], rows[i]
        if not hit then
            w.icon:Hide(); w.name:Hide(); w.unit:Hide(); w.qty:Hide(); w.total:Hide()
        else
            local y = -44 - (i - 1) * ROW_H
            local label = hit.name or Config.GetItemDisplayName(hit.itemID)
            if buyingItemID == hit.itemID then label = "|cffffcc00购买中|r " .. label end
            w.name:ClearAllPoints(); w.name:SetPoint("LEFT", f, "TOPLEFT", 30, y)
            w.name:SetText(label); w.name:Show()
            -- 图标：12.x API 形态未全实证，pcall 尽力而为，取不到只显名称（记一次日志）
            local oki, icon = pcall(function()
                return C_Item and C_Item.GetItemIconByID and C_Item.GetItemIconByID(hit.itemID)
            end)
            if oki and icon then
                w.icon:ClearAllPoints(); w.icon:SetPoint("LEFT", f, "TOPLEFT", 13, y + 4)
                w.icon:SetSize(14, 14)
                local okr = pcall(w.icon.SetTexture, w.icon, icon)
                if okr then w.icon:Show() else w.icon:Hide() end
            else
                w.icon:Hide()
                if not iconLogged then
                    iconLogged = true
                    Log("列表图标不可用（C_Item.GetItemIconByID 形态无效），仅显名称")
                end
            end
            w.unit:ClearAllPoints(); w.unit:SetPoint("LEFT", f, "TOPLEFT", 190, y)
            w.unit:SetText(coin(math.floor(MinUnit(hit)))); w.unit:Show()
            w.qty:ClearAllPoints(); w.qty:SetPoint("LEFT", f, "TOPLEFT", 262, y)
            w.qty:SetText(tostring(hit.totalQuantity)); w.qty:Show()
            w.total:ClearAllPoints(); w.total:SetPoint("LEFT", f, "TOPLEFT", 300, y)
            w.total:SetText(coin(hit.totalPrice)); w.total:Show()
        end
    end
    f.lastRows = #rows
    f:SetHeight(math.max(110, 66 + #rows * ROW_H))
    if #rows == 0 then f:Hide() else f:Show() end
    List.UpdateBuyButton()
end

local function EnsureUI()
    if frame then return frame end
    local ok, err = pcall(function()
        local f = CreateFrame("Frame", "AHSnipeListFrame", UIParent, "BasicFrameTemplateWithInset")
        f:SetSize(400, 130)
        if f.TitleText then f.TitleText:SetText("可购买列表") end
        f:SetFrameStrata("DIALOG")
        f:SetToplevel(true)
        f:SetMovable(true)
        f:EnableMouse(true)
        f:RegisterForDrag("LeftButton")
        f:SetScript("OnDragStart", function(self) self:StartMoving() end)
        f:SetScript("OnDragStop", function(self)
            self:StopMovingOrSizing()
            local point, _, x, y = self:GetPoint()
            if point then Config.SetListPos(point, x, y) end
        end)
        local pos = Config.GetListPos()
        local anchored = false
        if pos then
            anchored = pcall(f.SetPoint, f, pos.point, UIParent, pos.point, pos.x, pos.y)
        end
        if not anchored then
            -- 默认钉在拍卖行框体右侧；AH 句柄形态未经核实，失败退回屏幕中心偏移
            anchored = pcall(f.SetPoint, f, "TOPLEFT", AuctionHouseFrame, "TOPRIGHT", 12, 0)
            if not anchored then f:SetPoint("CENTER", UIParent, "CENTER", 240, 40) end
        end

        local buyBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        buyBtn:SetSize(150, 24)
        buyBtn:SetPoint("BOTTOM", f, "BOTTOM", 0, 14)
        buyBtn:SetScript("OnClick", function() List.TriggerBuy() end)
        f.buyBtn = buyBtn

        local hintFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        hintFS:SetPoint("BOTTOMRIGHT", f.buyBtn, "TOPRIGHT", 0, 6)
        hintFS:SetText("单价   数量   总价")
        hintFS:SetTextColor(0.6, 0.6, 0.6)

        f:Hide()
        frame = f
    end)
    if not ok then
        Log(("列表窗创建失败：%s"):format(tostring(err)))
        return nil
    end
    return frame
end

function List.Add(hit)
    if not EnsureUI() then return end
    local idx = nil
    for i, r in ipairs(rows) do
        if r.itemID == hit.itemID then idx = i end
    end
    if idx then
        rows[idx] = hit          -- 原地刷新数字与时间戳：连续轮询每秒都会到这里，静默不响
    else
        rows[#rows + 1] = hit
        local now = GetTime()
        Log(("入列 %s 单价%.2fg 数量%d 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs）"):format(
            hit.itemID, MinUnit(hit) / 10000, hit.totalQuantity, hit.totalPrice / 10000,
            now - hit.roundStartAt, hit.querySendAt - hit.roundStartAt,
            hit.seenAt - hit.querySendAt))
        PlayPing()
    end
    Render()
end

function List.MarkGone(itemID, reason)
    for i, r in ipairs(rows) do
        if r.itemID == itemID then
            table.remove(rows, i)
            Log(("除行 %s（%s）"):format(itemID, reason or "扫描证实无低价"))
            Render()
            return
        end
    end
end

-- BuyQueue 结算回调：整单全部成功才删行；有失败/剩余保留行，
-- 等下轮扫描刷新数字、或证实无低价由 Scanner 删行。同时清"购买中"标记、恢复按钮。
function List.OnBuyFinished(s)
    buyingItemID = nil
    if s and s.itemID and (s.bought or 0) > 0
        and (s.failed or 0) == 0 and not s.deferred then
        List.MarkGone(s.itemID, "购买完成")
    end
    Render()
end

-- 唯一购买入口：必须在按钮 OnClick 的硬件点击栈内直调（或其下游刷新回调栈内）。
function List.TriggerBuy()
    local BQ = AHSnipe.BuyQueue
    if not rows[1] or (BQ and BQ.IsBusy()) then return false end
    local hit = rows[1]
    Log(("点击购买 %s isCommodity=%s 命中%d条"):format(
        hit.itemID, tostring(hit.isCommodity), #hit.matched))
    if hit.isCommodity then
        -- commodity 不做购买前刷新：刷新查询挤占节流通道且报价本身实时
        if BQ.Buy(hit) then
            buyingItemID = hit.itemID
            Render()
        end
    else
        -- 非 commodity 的 auctionID 会过期：先刷新最新挂单，回调栈内直调购买（不得包计时器）
        AHSnipe.Scanner.RefreshHit(hit, function(fresh, reason)
            if fresh then
                if BQ.Buy(fresh) then
                    buyingItemID = fresh.itemID
                    Render()
                end
            else
                Log(("购买前刷新失败 %s：%s"):format(hit.itemID, reason))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(
                    hit.name or Config.GetItemDisplayName(hit.itemID), reason))
            end
        end)
    end
    return true
end

------------------------------------------------------------------------
-- 生命周期：跟随拍卖行开合（开=备好窗体；关=清空并收起，过期快照不许再点）
------------------------------------------------------------------------
function List.Init()
    local f = CreateFrame("Frame", "AHSnipeListEvents")
    f:RegisterEvent("AUCTION_HOUSE_SHOW")
    f:RegisterEvent("AUCTION_HOUSE_CLOSED")
    f:SetScript("OnEvent", function(_, event)
        if event == "AUCTION_HOUSE_SHOW" then
            EnsureUI()
        else
            rows = {}
            buyingItemID = nil
            if frame then frame:Hide() end
        end
    end)
    List.frame = f
end

-- 测试/诊断访问器
function List.RowCount() return #rows end
function List.GetFront() return rows[1] end
```

- [ ] **Step 4: 登记 TOC**

`AHSnipe/AHSnipe.toc` 第 13 行 `SnipeWindow.lua` 之后加一行：

```
SnipeList.lua
```

- [ ] **Step 5: 运行确认通过**

Run: `python tests/run_tests.py --syntax`
Expected: `PASS snipe_list`、`PASS snipe_list_buy`，全绿，退出码 0。

- [ ] **Step 6: 提交**

```bash
git add AHSnipe/SnipeList.lua AHSnipe/AHSnipe.toc tests/run_tests.py
git commit -m "feat: 新增可购买列表模块（加行/静默刷新/新鲜度排序/买队首）"
```

---

### Task 3: Scanner 对接（命中入列、新鲜失效删行、删"购买在途不弹窗"分支）

**Files:**
- Modify: `AHSnipe/Scanner.lua`（`FinishItem` 命中/不命中分支，约 L222-258）

**Interfaces:**
- Consumes: Task 2 的 `SnipeList.Add(hit)` / `SnipeList.MarkGone(itemID, reason)`。
- Produces: 无新对外接口（Scanner 对列表的调用点在 FinishItem 内）。

- [ ] **Step 1: 命中分支替换**

把 `FinishItem` 中这段（含注释整块）：

```lua
            -- 购买进行中不弹窗：模态弹窗会挡住用户操作，且此时点击也会被"已有购买进行中"
            -- 拒绝（2026-10-15 日志实证：购买持续 20 秒，期间弹窗反复遮挡、用户点击全被拒）。
            -- 命中下一轮会重新扫到，购买结束后自然弹出。
            -- 自动购买已于 2026-10-15 移除：实测扫描器/计时器触发的购买消息会被客户端
            -- 静默丢弃（10 秒无响应），仅硬件点击能可靠成交，详见 docs 第十一/十三轮记录。
            if AHSnipe.BuyQueue and AHSnipe.BuyQueue.IsBusy() then
                Log(("命中 %s：购买进行中，本轮不弹窗（下轮重新扫到）"):format(itemID))
            else
                -- 当场弹窗（不等轮次结束）：诱饵窗口只有几秒，即时提示才能抢到跟风单。
                -- 弹窗侧对同物品去重排队，避免高速轮询下重复弹窗
                AHSnipe.SnipeWindow.Show(hit)
            end
```

替换为：

```lua
            -- 命中一律入列（不等轮次结束）：列表非模态不遮操作，购买在途也照常刷新行，
            -- 用户盯按钮复亮连点即可。旧"购买进行中不弹窗"分支随弹窗模型一起废弃。
            AHSnipe.SnipeList.Add(hit)
```

- [ ] **Step 2: 不命中分支加删行**

把 `FinishItem` 的 miss 分支开头（`local f = focus[itemID]` 之前）改为：

```lua
        else
            -- 新鲜数据证实无低价 = 行可删的唯一扫描证据。
            -- timedOut 时读到的可能是部分/缓存结果，不算证据，不删行（防误删仍可买的行）。
            if not timedOut then
                AHSnipe.SnipeList.MarkGone(itemID)
            end
            -- 有挂单但无便宜货（价格已回涨/被买走，不再是诱饵价）：累计 miss，
            -- 达上限即提前退出聚焦，避免诱饵消失后还空转占满扫描通道
            local f = focus[itemID]
```

（原有 focus miss 计数代码保持不动；`RefreshHit` 购买刷新 sink 不经过这里，天然不会误删行。）

- [ ] **Step 3: 回归**

Run: `python tests/run_tests.py --syntax`
Expected: 全绿（smoke 无扫描命中路径，本任务不新增断言；行为差异在游戏内验收，见 Task 6 清单）。

- [ ] **Step 4: 提交**

```bash
git add AHSnipe/Scanner.lua
git commit -m "feat: 扫描命中入列，新鲜数据证实无低价时删行（超时不算证据）"
```

---

### Task 4: BuyQueue 结算联动列表（删行/恢复按钮，移除 ClearSuppression）

**Files:**
- Modify: `AHSnipe/BuyQueue.lua`（`FinishSummary` L64-72）
- Modify: `tests/run_tests.py`（`load_buy_queue` 返回记录器 + 新测试 `test_buqueue_notifies_list`）

**Interfaces:**
- Consumes: Task 2 的 `SnipeList.OnBuyFinished(summary)`。
- Produces: `load_buy_queue()` 返回三元组 `(lua, BuyQueue, finishedList)`（同步更新全部既有调用点）。

- [ ] **Step 1: 写失败测试**

`tests/run_tests.py` 把 `load_buy_queue` 尾部：

```python
    with open(BUY_QUEUE_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.BuyQueue
```

改为：

```python
    finished = []
    g.AHSnipe["SnipeList"] = lua.table_from({
        "OnBuyFinished": lambda s: finished.append(s),
    })
    with open(BUY_QUEUE_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.BuyQueue, finished
```

既有四个调用点 `lua, bq = load_buy_queue()` 全部改为 `lua, bq, _ = load_buy_queue()`。

新测试（放 `test_snipe_list_buy` 之后，登记进 `tests` 列表）：

```python
def test_buqueue_notifies_list():
    """commodity 单块购买结束后回调 SnipeList.OnBuyFinished，携带 itemID/bought/failed。"""
    lua, bq, finished = load_buy_queue()
    hit = lua.table_from({
        "itemID": 237361, "threshold": 156000, "isCommodity": True,
        "matched": lua.table_from([
            lua.table_from({"unitPrice": 148900, "quantity": 1})]),
    })
    if not bq.Buy(hit):
        return fail("bq_notify", "Buy 应受理单块命中")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    on(f, "COMMODITY_PRICE_UPDATED", 148900, 148900)
    on(f, "COMMODITY_PURCHASE_SUCCEEDED")
    if len(finished) != 1:
        return fail("bq_notify", f"应恰好回调一次，实际 {len(finished)} 次")
    s = finished[0]
    if int(s["itemID"]) != 237361 or int(s["bought"]) != 1 or int(s["failed"]) != 0:
        return fail("bq_notify", "summary 字段不符")
    ok("bq_notify")
```

- [ ] **Step 2: 运行——此时 BuyQueue 仍调 SnipeWindow，`OnBuyFinished` 记录器收不到调用，测试应 FAIL**

Run: `python tests/run_tests.py`
Expected: `FAIL bq_notify: 应恰好回调一次，实际 0 次`。

- [ ] **Step 3: 实现**

`BuyQueue.lua` `FinishSummary` 中把这段：

```lua
    -- 有失败或有剩余块/条目未处理：清除该物品的弹窗抑制，允许下一轮扫描立即重新提示
    -- （剩余挂单可能仍可购买，由用户再次点击继续）
    if (s.failed > 0 or s.deferred) and s.itemID
        and AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.ClearSuppression then
        AHSnipe.SnipeWindow.ClearSuppression(s.itemID)
    end
    if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.OnBuyFinished then
        AHSnipe.SnipeWindow.OnBuyFinished(s)
    end
```

替换为：

```lua
    -- 列表结算联动：整单全成功由 SnipeList 删行；有失败/剩余保留行，
    -- 下轮扫描刷新数字或证实无低价再删。同时清"购买中"标记、恢复【买队首】。
    if AHSnipe.SnipeList and AHSnipe.SnipeList.OnBuyFinished then
        AHSnipe.SnipeList.OnBuyFinished(s)
    end
```

- [ ] **Step 4: 运行确认通过**

Run: `python tests/run_tests.py --syntax`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add AHSnipe/BuyQueue.lua tests/run_tests.py
git commit -m "feat: 购买结算联动可购买列表（成功删行/失败保留/恢复按钮）"
```

---

### Task 5: 移除 SnipeWindow 及绑定/闪烁/连响残留（Core/ConfigPanel/TOC/smoke）

**Files:**
- Delete: `AHSnipe/SnipeWindow.lua`
- Modify: `AHSnipe/AHSnipe.toc`（删 `SnipeWindow.lua` 行；Notes 文案改列表）
- Modify: `AHSnipe/Core.lua`（L70 Init 替换、L95-150 bind/unbind 删除）
- Modify: `AHSnipe/ConfigPanel.lua`（L275-283、L382-405、L417、L461、L479）
- Modify: `tests/run_tests.py`（PLUGIN_ORDER、smoke 步骤 5/6、slash 用例）

**Interfaces:**
- Consumes: Task 2 的 `SnipeList.Init/Add/GetFront/RowCount/TriggerBuy`。
- Produces: 代码库中不再存在 `SnipeWindow`、`bindKey`、`IsFlashAlert`、`IsRepeatPing` 的任何引用。

- [ ] **Step 1: 先改测试（失败）**

`tests/run_tests.py`：
1. `PLUGIN_ORDER` 中 `"SnipeWindow.lua"` 改为 `"SnipeList.lua"`。
2. smoke 步骤 4 `slash("bind")`/`slash("unbind")` 两行删除。
3. 整个步骤 5（`# 5) 一键购买` 注释起到其 `print("SMOKE-OK 一键购买…")` 止）替换为：

```python
    # 5) 可购买列表：命中加行 → 点【买队首】买入最新鲜一条 → 成功回调删行
    try:
        def fake_buy(hit):
            buys.append(int(hit["itemID"]))
            return True
        buys = []
        addon.BuyQueue.Buy = fake_buy
        h_old = lua.table_from({
            "itemID": 237362, "name": "旧", "threshold": 156000, "isCommodity": True,
            "totalQuantity": 3, "totalPrice": 300000,
            "matched": lua.table_from([lua.table_from({"unitPrice": 100000, "quantity": 3})]),
            "roundStartAt": 97, "querySendAt": 99, "seenAt": 99,
        })
        h_new = lua.table_from({
            "itemID": 237363, "name": "新", "threshold": 156000, "isCommodity": True,
            "totalQuantity": 1, "totalPrice": 100000,
            "matched": lua.table_from([lua.table_from({"unitPrice": 100000, "quantity": 1})]),
            "roundStartAt": 97, "querySendAt": 99.5, "seenAt": 99.5,
        })
        addon.SnipeList.Add(h_old)
        addon.SnipeList.Add(h_new)
        if addon.SnipeList.RowCount() != 2:
            return fail("smoke:list", "两条命中应有两行")
        if int(addon.SnipeList.GetFront()["itemID"]) != 237363:
            return fail("smoke:list", "队首应为 seenAt 更新的 237363")
        if not addon.SnipeList.TriggerBuy():
            return fail("smoke:list", "TriggerBuy 未受理")
        if buys != [237363]:
            return fail("smoke:list", f"应买队首，实际 {buys}")
        addon.SnipeList.OnBuyFinished(lua.table_from(
            {"itemID": 237363, "bought": 1, "failed": 0, "deferred": False}))
        if addon.SnipeList.RowCount() != 1:
            return fail("smoke:list", "整单成功应删行")
    except Exception as e:
        return fail("smoke:list", str(e))
    print("SMOKE-OK 可购买列表（加行/排序/买队首/成功删行）")
```

4. 步骤 6（`# 6) 命中提醒开关` 整段）删除（配置字段已在 Task 1 废弃）。
5. 步骤 3 的 `hit`（237361 购买）与既有 `test_buqueue_notifies_list` 无冲突，保持不动。

Run: `python tests/run_tests.py`
Expected: smoke FAIL（SnipeWindow.lua 仍被删除引用前 Core 还调它 / PLUGIN_ORDER 文件不存在，视编辑顺序——按上面顺序先会得到 FAIL，属预期红灯）。

- [ ] **Step 2: Core.lua**

L70：

```lua
        if AHSnipe.SnipeWindow then SafeStep("SnipeWindow.Init", AHSnipe.SnipeWindow.Init) end
```

改为：

```lua
        if AHSnipe.SnipeList then SafeStep("SnipeList.Init", AHSnipe.SnipeList.Init) end
```

删除斜杠命令注释里的 `-- /ahsnipe bind <按键> 一键购买，/ahsnipe unbind 解除该按键绑定`（L97），删除 `elseif msg == "bind" ... end` 与 `elseif msg == "unbind" ... end` 两个整块（L123-150）。

- [ ] **Step 3: ConfigPanel.lua**

删除：
- `RefreshOptionLabels` 函数整块（L275-283，含其上方注释行）；
- `flashBtn` 整块（L382-389）、`pingBtn` 整块（L391-398）、`bindFS` 整块（L401-405，含上方注释）；
- 三处调用：L417 `RefreshOptionLabels()`、L461 与 L479 的 `SafeUI("RefreshOptionLabels", RefreshOptionLabels)`。

（删除后按钮布局锚点：`hintFS` 与 `scanBtn` 不依赖这三个控件，滚动区 TOPLEFT -128 保持不变，无需改锚点。）

- [ ] **Step 4: TOC 与删除文件**

`AHSnipe.toc`：删 `SnipeWindow.lua` 行；Notes 行改为：

```
## Notes: 连续扫描指定物品挂单，低价诱饵命中入可购买列表，点【买队首】按新鲜度顺序购买
```

```bash
git rm AHSnipe/SnipeWindow.lua
```

- [ ] **Step 5: 运行确认通过**

Run: `python tests/run_tests.py --syntax`
Expected: 全绿（syntax 列表不再含 SnipeWindow.lua、含 SnipeList.lua）。

```bash
grep -rn "SnipeWindow\|bindKey\|FlashAlert\|RepeatPing\|KeyBuy" AHSnipe/ || echo CLEAN
```

Expected: `CLEAN`（若有残留按报错位置清除）。

- [ ] **Step 6: 提交**

```bash
git add AHSnipe/Core.lua AHSnipe/ConfigPanel.lua AHSnipe/AHSnipe.toc tests/run_tests.py
git commit -m "refactor: 移除狙击弹窗与按键绑定/闪烁/连响，全面切换可购买列表"
```

---

### Task 6: 文档同步与游戏内验收

**Files:**
- Modify: `docs/tasks/2026-10-13-ahsnipe-plan.md`（追加第八轮记录）

**Interfaces:**
- Consumes: Task 1-5 全部产出。
- Produces: 无代码接口。

- [ ] **Step 1: 追加轮次记录**

在 `docs/tasks/2026-10-13-ahsnipe-plan.md` 末尾按既有轮次格式追加一节"第八轮：弹窗改可购买列表（2026-10-16）"，内容要点：动机（弹窗一次一目标+抑制导致"提醒晚/看不见"；数据年龄实测排队段 4~10s）、决策（方案 A、去行内按钮、去快捷键层、购买中置灰不做点击延后补发）、实现（SnipeList 模块、Scanner/BuyQueue 联动、SnipeWindow/bind/闪烁/连响删除）、验收清单见下一步。

- [ ] **Step 2: 游戏内验收（用户执行，逐项核对）**

1. `/reload` → 打开拍卖行：列表窗出现在 AH 右侧（空列表时隐藏）；拖动后重开位置保持。
2. 造诱饵价 → ≤1 轮内加行+响一声；同物品连续命中只原地刷新不重响。
3. 诱饵被买走/回涨 → 行自动消失（日志"除行 …（扫描证实无低价）"）。
4. 点【买队首】→ 买的是 seenAt 最新一条；购买在途按钮置灰"购买中…"；结算复亮后再点轮到新队首。
5. 整单成功 → 该行立即消失（日志"除行 …（购买完成）"）。
6. 关闭拍卖行 → 列表清空收起；`/ahsnipe log` 无"步骤失败"、无星级/图标刷屏。

- [ ] **Step 3: 提交**

```bash
git add docs/tasks/2026-10-13-ahsnipe-plan.md
git commit -m "docs: 第八轮记录——狙击弹窗改可购买列表"
```

---

## Self-Review 结论

- 规格覆盖：列表交互/生命周期/Scanner/BuyQueue/废弃项/测试/验收均有对应任务；行图标为 best-effort（pcall+留痕），符合"形态未实证"约束。
- 类型一致性：`Add/MarkGone/OnBuyFinished/TriggerBuy/UpdateBuyButton/GetListPos/SetListPos` 各任务签名一致；hit 时间戳三字段与 Scanner 现实现一致。
- 无占位符：所有步骤含完整代码与预期输出。
