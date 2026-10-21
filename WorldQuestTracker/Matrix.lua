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
