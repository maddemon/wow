# WorldQuestTracker 阶段二实现计划（地图按钮 + 任务×角色矩阵）

> 旧名横幅：本文件写成时插件还叫 WorldQuestTracker（全局 WQT / 存档 WQTDB / 命令 /wqt / 测试 run_wqt_tests.py）；2026-10-27 起改名 GoldQuestLedger（GQL / GQLDB / /gql / run_gql_tests.py）。逐轮决策记录按当时口径保留，不追改。

> **For agentic workers:** 每个任务按 RED → GREEN → 提交三步走，步骤用 `- [ ]` 勾选。执行前先读
> `docs/superpowers/specs/2026-10-21-worldquesttracker-design.md` 第 8/9/10/12 节与
> `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md`。

**Goal:** 打开世界地图即出现一个按钮，点它打开"金币任务 × 角色"矩阵；矩阵每格状态来自账本，绝不把"取不到"显示成"没做"。

**Architecture:** 延续阶段一分层。`Ledger.lua` 加一个纯函数 `L.MatrixRows` 把账本摊成"可按地图分组的行"，不碰任何 WoW 接口，因此整张矩阵的内容能在 lupa 里离线验证；`Matrix.lua` 只负责把这份数据画成帧、把点击转成 `L.SetManual`；`MapButton` 挂在 `Matrix.lua` 里（同一个宿主帧，分开两个文件只会让接线更容易漏）。所有 12.x 形态未知的问题继续关在 `Api.lua` + `/wqt probe`。

**Tech Stack:** WoW 12.1.5（Interface 120105）纯 Lua，零依赖；离线单测 Python 3 + lupa（`tests/run_wqt_tests.py`）。

## Global Constraints

- 只记录金币任务；矩阵行 = 账本里当前签名给金币的任务，未判定任务单列一组（spec 第 12 节退化 2）。
- 单元格四态照 spec 8.1：`已交 / 已接未交 / 待做 / 本轮已结束`，外加 `?`（没有任何来源）。`?` 与"待做"必须肉眼能分。
- 补录一律点击，禁止斜杠命令或输入任务名（用户硬约束）。
- 未选任何角色列时格子只读，防误改。
- 每个 12.x 接口调用都 `pcall`，取不到就降级为"未知"并记 `Api.LastDegrade`，不许假设接口形态。
- 周 CD 锚点一律从服务端接口反推，不写死星期几。
- `nil` 金币绝不显示成 `0`；本周统计不可信时显示 `?`（阶段一已定的 `-1` 契约）。
- 时间戳与锚点同域才可比：`db.clockDomain == "uptime"` 且锚点为纪元秒时整列合计显示 `?`。
- 提交只用 pathspec 指定文件，禁止 `git add -A` / `git commit -a`；`AHSnipe/`、`tests/run_tests.py` 由并行会话负责，本计划一律不碰。
- 阶段二 UI 只允许接 `/wqt probe` 实测过的入口（spec 第 12 节）。本计划里"存在性未知"的入口全部走
  `Api.Resolve` + 判空后降级，绝不在加载期硬索引。

## 前置证据（Task 1–4 可先行，Task 5–6 必须等回传）

第一轮 12.1.5 实测已回来，结论写进清单第 1 节：`C_WorldQuests.*`、`C_QuestLog.IsQuestCompleted/
IsQuestStarted/GetQuestRewardMoney/GetQuestRewardItems`、`FrameIsVisible` 全 MISSING，
`GetQuestResetTime` 是倒计时，`time`/`GetServerTime` 可用。
新构建的 probe 多了两段——`---- 接口发现`（真名反查）与 `---- 阶段二入口（只判存在）`
（`WorldMapFrame`、`WorldMapFrame.ScrollContainer.Child.GridCanvas`、`C_Map.GetMapInfo` 等）。
**这两段没回传之前，不做 Task 5/6**：气泡头像的坐标全靠 `GridCanvas` 与地图接口，猜出来的挂点只会是一张盖住地图的空层。

---

### Task 1: 矩阵行与列构建（纯数据，离线可测）— 已落盘 `6403034`

**Files:** Modify `WorldQuestTracker/Ledger.lua`、`tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `L.Characters`、`L.State`、`L.GetObs`、`L.IsHighFreq`、`L.QuestMoney`、`L.WeekStats`
- Produces: `L.QuestMoney(db, questID)` → 当前签名金币或 `nil`；
  `L.MatrixRows(db)` → `{ { mapID, unknown?, rows = { {questID,name,mapID,money,highFreq,state={charKey->状态}} } } }`；
  `L.MatrixColumns(db, now)` → `{ {key,lastSync,count,gold,stale?} }`

分组口径（与 `/wqt dump` 必须一致）：金币任务按地图分组、组内按收益降序、组顺序 = 该图最赚钱任务的排名；
奖励从未判定过的单独一组置末；**明确不给金币的一律不收**（本插件只记金币任务）；
只剩手动记录、已从池里消失的任务留在未判定组，手动补录不能因为任务刷没就凭空蒸发。
`count/gold` 在无锚点或时钟域分裂时为 -1 → UI 显示 `?`。

落盘代码（与 `WorldQuestTracker/Ledger.lua` 逐字节一致）：

```lua
local STALE_SECONDS = 7 * 86400

function L.QuestMoney(db, questID)
    local obs = L.GetObs(db, questID)
    return obs and obs.money or nil
end

--- gold = 任一 observation 给过金币（跨轮、跨模式都算，任务这轮刷没刷不影响）；
--- nongold = 奖励确实读到了且不给金币；
--- unknown = 奖励接口从没给过数（退化 2），做过一次实测收款即自动升为 gold。
local function Classify(db, questID)
    local rec = db.quests and db.quests[questID]
    local known = false
    if rec and rec.observations then
        for _, obs in pairs(rec.observations) do
            if obs.gold then return "gold" end
            if type(obs.money) == "number" then known = true end
        end
    end
    return known and "nongold" or "unknown"
end

--- 金币任务按地图分组，组内按收益从高到低；组顺序 = 该图最赚钱任务的排名。
--- 未判定单独一组放最后，明确不给金币的一律不收（本插件只记金币任务）。
--- 只剩手动记录、已从池里消失的任务留在未判定组：手动补录不能因为任务刷没就凭空蒸发。
function L.MatrixRows(db)
    local gold, unknown = {}, {}
    local chars = L.Characters(db)
    local ids = {}
    for questID in pairs(db.quests or {}) do ids[#ids + 1] = questID end
    table.sort(ids, function(a, b)
        return (L.QuestMoney(db, a) or -1) > (L.QuestMoney(db, b) or -1)
    end)
    for i = 1, #ids do
        local questID = ids[i]
        local kind = Classify(db, questID)
        if kind ~= "nongold" then
            local rec = db.quests[questID]
            local row = {
                questID = questID,
                name = rec.name or tostring(questID),
                mapID = rec.mapID,
                money = L.QuestMoney(db, questID),
                highFreq = L.IsHighFreq(db, questID),
                state = {},
            }
            for c = 1, #chars do
                -- 点号调用而不是冒号：冒号会把 L 塞进 self，charKey 反而挤到第 3 个参数
                row.state[chars[c]] = L.State(db, chars[c], questID)
            end
            local list = (kind == "gold") and gold or unknown
            list[#list + 1] = row
        end
    end

    local out, byMap = {}, {}
    for i = 1, #gold do
        local row = gold[i]
        local mapID = row.mapID or -1
        local g = byMap[mapID]
        if not g then
            g = { mapID = mapID, rows = {} }
            byMap[mapID] = g
            out[#out + 1] = g
        end
        g.rows[#g.rows + 1] = row
    end
    if #unknown > 0 then
        out[#out + 1] = { mapID = -1, unknown = true, rows = unknown }
    end
    return out
end

--- 列 = 角色：本周完成数/金币 + 最后同步时间 + 是否过期。
--- 锚点定不出来（0）或时钟域分裂时 count/gold 一律 -1，UI 显示 ?，
--- 口径与 Dump 的本周汇总必须一致——同一份账本不许两处报两个数。
function L.MatrixColumns(db, now)
    local chars = L.Characters(db)
    local anchor = db.cycleAnchor or 0
    local out = {}
    for i = 1, #chars do
        local charKey = chars[i]
        local count, gold = -1, -1
        if anchor > 0 then count, gold = L.WeekStats(db, charKey, anchor) end
        local lastSync = (db.chars[charKey] or {}).lastSync or 0
        out[#out + 1] = {
            key = charKey,
            lastSync = lastSync,
            count = count,
            gold = gold,
            stale = (now and lastSync > 0 and (now - lastSync) > STALE_SECONDS) and true or nil,
        }
    end
    return out
end
```

测试：`matrix_rows_split_gold_unknown_and_drop_nongold`、
`matrix_columns_stale_and_manual_only_quest_kept`（含"没同步过的列不该标过期"这一条：
`lastSync == 0` 是没同步过，不是快照过期，两种状态在 UI 上必须分开）。

---

### Task 2: 矩阵窗口 + 地图按钮 — 已落盘

**Files:** Create `WorldQuestTracker/Matrix.lua`；Modify `Api.lua`（`Api.MapName`）、
`Config.lua`（`FormatGold` 判负）、`Dump.lua`（去掉 `and nil or` 陷阱）、`WorldQuestTracker.toc`、`tests/run_wqt_tests.py`

**Interfaces:**
- Consumes: `L.MatrixRows`、`L.MatrixColumns`、`Api.MapName`、`Api.Resolve`、`Config.FormatGold`
- Produces: `WQT.Matrix.Ensure/Render/Refresh/Toggle/EnsureMapButton`、`WQT.Matrix.rowCount`、`/wqt matrix`

三条设计判断：
1. 帧**惰性创建**，加载期不碰 `CreateFrame`（沿用 `LogWindow.lua` 的做法），离线单测才能加载本文件。
2. 按钮父层直接挂 `WorldMapFrame`，只用 `CreateFrame` + `SetPoint`，**不依赖 `WorldMapFrame.RegisterButton`**
   （12.x 是否存在还没实测）；宿主取不到就记 `地图按钮降级：WorldMapFrame 不可用` 并返回 nil，
   矩阵仍有 `/wqt matrix`。判宿主只判真假不判 `type` —— 帧对象由引擎造，Lua 看到的类型不保证是 table，
   同一个坑（`Api.FrameVisible`）在 12.1.5 已经踩过一次。
3. 文本对象池复用（`Put`），每轮 Render 只改文案与挂点、多余收起，不重复造帧。

**过程中抓出的真 bug（已先写失败用例再修）**：`gold < 0 and nil or gold` 这种写法在 Lua 里
恒等于 `gold`（`X and nil` 永远假），于是"本周统计不可信"的 -1 哨兵被原样格式化成
`-1g99s99c`。`Dump` 和 `Matrix` 两处都中招，而阶段一的用例只断言了 `完成 ?` 没断言金币栏，
所以一直没被抓到。修法是把判负收进 `FormatGold`（钱永远不可能为负），调用点不再各自判：

```lua
-- 金币格式化。nil → "?" 表示"未知"，与 0（真没赚钱）严格区分。
-- 负数同样显示 "?"：Ledger.WeekStats 用 -1 当"本周统计不可信"的哨兵，而钱永远不可能为负，
-- 判负收在这里就不用每个调用点各写一遍 —— 调用点写 `gold < 0 and nil or gold` 会被
-- Lua 的 `X and nil or Y` 恒等于 Y 坑掉，把 -1 原样格式化成 "-1g99s99c" 那种荒唐数。
function Config.FormatGold(copper)
    if type(copper) ~= "number" or copper < 0 then return "?" end
    return string.format("%dg%ds%dc",
        math.floor(copper / 10000), math.floor((copper % 10000) / 100), copper % 100)
end
```

`Api.MapName` 是矩阵唯一需要的新接口，取不到返回 nil 让调用方回退"地图 <id>"：

```lua
-- 地图名。矩阵按地图分组时用它把 mapID 变成人看得懂的字。
-- 12.x 的入口名与返回字段都没实测过：候选逐个 pcall，字段按候选列表试，
-- 拿不到就返回 nil 让调用方回退"地图 <id>"—— 编一个地图名比显示地图号更糟。
local MAP_NAME_SOURCES = { "C_Map.GetMapInfo", "C_Map.GetAreaInfo", "GetMapNameByID" }

function Api.MapName(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    for i = 1, #MAP_NAME_SOURCES do
        local fn = Api.Resolve(MAP_NAME_SOURCES[i])
        if Callable(fn) then
            local okCall, info = pcall(fn, mapID)
            if okCall then
                local name
                if type(info) == "table" then
                    name = FirstField(info, { "name", "mapName", "areaName" })
                elseif type(info) == "string" then
                    name = info
                end
                if type(name) == "string" and name ~= "" then return name end
            end
        end
    end
    return nil
end
```

`Matrix.lua` 落盘全文：

```lua
-- 任务×角色矩阵（spec 8.1）+ 打开它的地图按钮。
-- 这个文件只做两件事：把 Ledger.MatrixRows/MatrixColumns 摊好的数据画成帧、
-- 把 12.x 宿主取不到这件事变成"降级 + 日志"，而不是加载期报错。
-- 状态文案照 spec 的五个符号，"取不到"(? 未知) 与"没做"(· 待做) 必须一眼能分。

WQT.Matrix = {}
local Mx = WQT.Matrix

local NAME_W, COL_W, ROW_H = 260, 78, 20

local frame, scroll, texts, button
local used

local STATE_TEXT = {
    done = "✓ 已交",
    progress = "● 已接",
    open = "· 待做",
    ended = "⊘ 本轮结束",
    unknown = "? 未知",
}

--- 文本对象池：每次 Render 只改文案和挂点，多余的收起来。
--- 矩阵要随登录/同步反复重画，每轮都 CreateFontString 会一路堆帧。
local function Put(x, y, text, font)
    used = used + 1
    local t = texts[used]
    if not t then
        t = scroll:CreateFontString(nil, "OVERLAY", font or "GameFontNormalSmall")
        texts[used] = t
    end
    t:SetText(text)
    t:ClearAllPoints()
    t:SetPoint("TOPLEFT", scroll, "TOPLEFT", x, -y)
    return t
end

function Mx.Ensure()
    if frame then return frame end
    frame = CreateFrame("Frame", "WQTMatrixFrame", UIParent, "BackdropTemplate")
    frame:SetSize(820, 480)
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
    title:SetText("WorldQuestTracker 任务×角色矩阵（只读）")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -6, -6)
    close:SetScript("OnClick", function() frame:Hide() end)

    scroll = CreateFrame("Frame", "WQTMatrixScroll", frame)
    scroll:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -10)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -8, 8)
    texts = {}

    frame:Hide()
    return frame
end

--- 只读渲染，返回渲染的任务行数。
--- 合计行的口径必须和 /wqt dump 完全一致：同一个锚点、同一份 WeekStats，
--- 无锚点或时钟域分裂时 Ledger 给 -1，这里显示 ? 而不是 0。
function Mx.Render()
    local f = Mx.Ensure()
    used = 0
    local db = WQT.Config.DB() or {}
    local now = (WQT.Api and WQT.Api.WallClock())
        or math.floor((WQT.Api and WQT.Api.Uptime()) or 0)
    local groups = WQT.Ledger.MatrixRows(db)
    local cols = WQT.Ledger.MatrixColumns(db, now)
    local rowCount = 0

    Put(0, 0, "角色 " .. #cols .. " 个", "GameFontHighlightSmall")
    local y = ROW_H
    for i = 1, #cols do
        Put(NAME_W + (i - 1) * COL_W, 0,
            cols[i].key .. (cols[i].stale and "（快照过期）" or ""), "GameFontNormalSmall")
    end

    for gi = 1, #groups do
        local g = groups[gi]
        local label
        if g.unknown then
            label = "未判定（奖励接口没给过数，做过一次实测收款才自动转正）"
        else
            local name = WQT.Api and WQT.Api.MapName and WQT.Api.MapName(g.mapID)
            label = (name or ("地图 " .. tostring(g.mapID)))
        end
        Put(0, y, ("▼ %s（%d）"):format(label, #g.rows), "GameFontHighlightSmall")
        y = y + ROW_H
        for ri = 1, #g.rows do
            local row = g.rows[ri]
            rowCount = rowCount + 1
            local money = row.money and (" ~" .. WQT.Config.FormatGold(row.money)) or ""
            Put(0, y, ("%d. %s%s%s"):format(row.questID, row.name, money,
                row.highFreq and "（高频）" or ""))
            for ci = 1, #cols do
                Put(NAME_W + (ci - 1) * COL_W, y,
                    STATE_TEXT[row.state[cols[ci].key]] or "? 未知")
            end
            y = y + ROW_H
        end
    end

    if #groups == 0 then
        Put(0, y, "（账本里还没有金币任务：/wqt probe 定不到世界任务池接口时，这里就是空的）")
        y = y + ROW_H
    end
    if #cols == 0 then
        Put(0, y, "（还没有任何角色同步过：任意角色登录一次才会出现列）")
        y = y + ROW_H
    else
        y = y + ROW_H
        Put(0, y, "== 本周汇总 ==")
        for i = 1, #cols do
            local c = cols[i]
            Put(0, y + i * ROW_H, ("%s  完成 %s  金币 %s  同步于 %s"):format(
                c.key, c.count < 0 and "?" or tostring(c.count),
                WQT.Config.FormatGold(c.gold), tostring(c.lastSync)))
        end
    end

    for i = used + 1, #texts do texts[i]:Hide() end
    Mx.rowCount = rowCount
    return rowCount
end

function Mx.Refresh()
    if frame and frame:IsShown() then Mx.Render() end
end

function Mx.Toggle()
    local f = Mx.Ensure()
    if f:IsShown() then
        f:Hide()
    else
        Mx.Render()
        f:Show()
    end
end

--- 按钮父层直接挂 WorldMapFrame，跟着地图一起显隐，不依赖 WorldMapFrame.RegisterButton
--- 那类内部接口（12.x 是否存在还没实测）。宿主取不到就不建按钮并记降级：
--- 矩阵还有 /wqt matrix 可开，但用户在游戏里看不见"为什么没按钮"，所以日志必须有这一行。
function Mx.EnsureMapButton()
    if button then return button end
    local host = WQT.Api and WQT.Api.Resolve("WorldMapFrame")
    -- 只判真假不判 type：帧对象由引擎造，Lua 看到的类型不保证是 table
    -- （Api.FrameVisible 在 12.1.5 就因为这条回退失效过，同一个坑不踩第二次）
    if not host then
        WQT.Config.Log("地图按钮降级：WorldMapFrame 不可用（矩阵改用 /wqt matrix）")
        return nil
    end
    button = CreateFrame("Button", "WQTMapButton", host, "UIPanelButtonTemplate")
    button:SetSize(190, 24)
    button:SetPoint("BOTTOMLEFT", host, "BOTTOMLEFT", 12, 46)
    -- 不写死 SetFrameStrata：子帧默认跟随父层，写 MEDIUM 反而可能被地图内容盖住
    -- （12.x 的 WorldMapFrame 用哪一层还没实测，继承是唯一不猜的写法）
    button:SetText("世界任务金币矩阵")
    button:SetScript("OnClick", function() Mx.Toggle() end)
    -- 建没建成、宿主多大，是"按钮看不见"唯一的排查线索；取不到就写取不到，不编数
    local okCall, w, h = pcall(function() return host:GetWidth(), host:GetHeight() end)
    WQT.Config.Log(("地图按钮已建 宿主显隐=%s 尺寸=%s"):format(
        tostring(WQT.Api.FrameVisible("WorldMapFrame")),
        (okCall and w) and ("%dx%d"):format(w, h or 0) or "取不到"))
    return button
end

--- 探测报告用这一行区分"没建"和"建了但看不见"，两者修法完全不同。
function Mx.ButtonInfo()
    if not button then return "未建" end
    local okCall, shown = pcall(function() return button:IsShown() end)
    if not okCall then return "已建（显隐取不到）" end
    return shown and "已建（可见）" or "已建（随地图隐藏中）"
end

WQT.RegisterCommand("matrix", Mx.Toggle, "打开/关闭任务×角色矩阵")

```

测试 4 条：`matrix_renders_rows_and_week_totals`（含"钱差不可信时合计显示 ?"）、
`matrix_empty_ledger_shows_placeholder`（空账本不能画成一片空白，要分得清"没数据"和"渲染坏了"）、
`map_button_opens_matrix_from_world_map`、`map_button_degrades_without_world_map_frame`。
替身侧配套：`CreateFrame` 补齐 `SetSize/SetPoint/CreateFontString/SetText/...` 并把子帧记进
`__children`、带名字的 CreateFrame 注册全局（游戏就是这么做的），`_G.WorldMapFrame` 默认可用、
降级用例把它置 nil；`frame_texts()` 走帧树收文本，断言只看用户真看得见的字。

**已知待办（本次没做）**：`同步于 100000` 这类裸时间戳（`Dump` 同样有）对用户不可读，
需要 `Config.FormatTime` 走 `date()` + pcall 降级；矩阵这次是只读的，格子点击/目标组/撤销在 Task 3。

#### 第二轮回传后的实测修正（commit `1263e9c`）

用户回传"打开地图没看到按钮"，查出来是两个各自独立的真 bug：

1. **`Mx.EnsureMapButton()` 压根没人调用** —— 只有 `/wqt matrix` 这条命令路径存在。修法是接到
   `Core.lua` 的 `PLAYER_ENTERING_WORLD` 上并 `pcall`，每次进入世界都补一次（`WorldMapFrame`
   是不是登录时就已建出来还没有实测依据，所以按幂等重试试）。同时去掉 `SetFrameStrata("MEDIUM")`：
   子帧默认跟随父层，写死一层反而可能被地图内容盖住 —— 继承是唯一不猜的写法。
2. **`Sync.OnWorldEnter` 被未知事件名炸掉** —— 12.1.5 的 `RegisterEvent` 对不认识的事件名直接抛错，
   `WORLD_QUEST_LOG_UPDATE` 正炸在第一行，于是登录同步整条没跑（日志里就是那行
   `Sync.OnWorldEnter 出错: ... Attempt to register unknown event`）。改成候选名单
   `{WORLD_QUEST_LOG_UPDATE, QUEST_LOG_UPDATE}` 逐个 `pcall` 注册，全都不认识时记
   `池变化事件全部不可用：只在登录与 /wqt sync 时更新池`；事件分发也按名单判断，不再写死单个名字。
   替身侧配套把 `RegisterEvent` 改成"名单外即抛错"，否则这类事故在离线单测里永远复现不了。

探测这条线按上一轮拿到的 `Usage: C_QuestLog.GetQuestsOnMap(uiMapID)` 往前推了一步：
`Api.ArgHint` 从布尔改成参数名（`questID` / `mapID`），新增 `Api.CurrentMapID()`（候选逐个 pcall，
只认真正像地图号的正整数）与 `Api.MapCandidatePaths()`，报告多出 `---- 地图/bounty 候选`
（带真 mapID 调用，拿不到就 `SKIP ... 无 mapID`，不再产出 bad argument 废证据）和
`---- 地图按钮宿主`（显隐/尺寸/按钮状态，`Mx.ButtonInfo()` 用来区分"没建"和"建了但看不见"）；
`---- 接口发现` 补枚举 `C_Map`/`C_WorldMap` —— 池接口按月历/bounty 给，缺的正是"枚举地图"的入口。

用例累计到 67 条（新增 `sync_survives_unknown_event_name`、`sync_logs_when_no_pool_event_registered`、
`login_creates_map_button`、`login_retries_button_when_map_frame_appears_later`、
`map_button_inherits_host_strata`、`probe_reports_map_button_host`、
`probe_distinguishes_unbuilt_from_invisible_button`、
`probe_map_candidates_use_map_id_and_skip_without_one`、`api_current_map_id_returns_number_or_nil`），
跑测器另加两条纪律：一条用例抛错不再吞掉后面全部结果（`try/except` 记 FAIL），
控制台输出按本地码页做无损降级替换。

---

### Task 3: 目标组点击补录 + 撤销（spec 8.1/8.3）

**Files:** Modify `WorldQuestTracker/Matrix.lua`、`WorldQuestTracker/Ledger.lua`（撤销栈 `L.PushUndo`/`L.PopUndo`）、`tests/run_wqt_tests.py`
**前置：** 已回传 `---- 阶段二入口`，确认 `WorldMapFrame` 存在（否则补录只能靠矩阵，气泡那条路作废）。

- [ ] Step 1: RED —— 未选列时点格子不写账本；选列后点格子循环 `open→done→open` 且进撤销栈；底部`撤销`按钮回退最近 20 步（补录与撤销都只许点击，不做斜杠命令）。
- [ ] Step 2: 跑测试确认失败。
- [ ] Step 3: 实现 `Mx.OnCellClick` + `L.PushUndo/PopUndo`（`db.undo` 环形 20 条）+ 列头点击切目标组 + 底部`撤销`按钮。
- [ ] Step 4: GREEN + syntax。
- [ ] Step 5: pathspec 提交。

### Task 4: 行虚拟化与列宽/滚动

**Files:** Modify `WorldQuestTracker/Matrix.lua`、`tests/run_wqt_tests.py`
- [ ] Step 1: RED —— 200 行账本只渲染视口内行（`#cells <= 视口行数 + 缓冲`），滚动位置改变时行内容跟着变。
- [ ] Step 2~5: 同上节奏。

### Task 5: 地图气泡头像叠层（需实测）

**Files:** Create `WorldQuestTracker/MapOverlay.lua`；Modify `Api.lua`（气泡命中/坐标候选入口）、`Probe.lua`
**阻塞：** 必须等 `---- 接口发现` 与 `---- 阶段二入口` 回传，据实定 `WorldMapFrame.ScrollContainer.Child.GridCanvas`、`C_Map.GetWorldMapID`、气泡几何接口后再写。点气泡头像 = 切换该角色该任务（写 `manual`，不被登录同步覆盖）。

### Task 6: Tooltip（spec 8.4）

**Files:** Create `WorldQuestTracker/Tooltip.lua`；Modify `Api.lua`（奖励查询候选只从实测过的入口里选）
多签名分模式显示（"普通模式 250G / 狩猎模式 无金币"），金币步进 `−100 −10 +10 +100` 全点击、无输入框。

### Task 7: 数据源接通后的回归

**Files:** Modify `Api.lua`（`GetPool` 换成实测真名）、`tests/run_wqt_tests.py`（把真名样本做成固定 fixture 回归）
把第二轮游戏内数据回写阶段一清单，并跑 `docs/superpowers/tasks/2026-10-21-wqt-phase1-checklist.md` 第 2–6 步。

---

## 复核要点（每个任务收尾自问）

- 有没有把 `nil` 金币显示成 `0`？有没有把"取不到状态"显示成"待做"？
- 矩阵里任何一格的状态，能不能在 `/wqt dump` 里找到同一个数的来源？两者口径必须一致。
- 新增的 12.x 调用是否都在 `Api.lua` 且 `pcall` 包过？UI 层不许直接碰全局接口。
- 离线单测是否覆盖了"降级分支"（宿主缺失、锚点未知、时钟域分裂），而不只覆盖顺利路径？
