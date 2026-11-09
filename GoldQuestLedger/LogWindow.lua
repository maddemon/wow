-- 日志窗：多行 EditBox 承载探测报告与诊断日志。
-- 沿用 AHSnipe 的做法：报告在窗内 Ctrl+A/Ctrl+C 复制回传，不靠截图。
-- 窗口是惰性创建的（Ensure）：加载阶段不碰 CreateFrame，离线单测加载本文件才不会炸。
-- /gql probe、/gql quest、/gql dump、/gql log 四条都打进这一张窗。
--
-- 正文为什么在滚动区里（11-09 用户："你把 gql probe、gql quest 命令打开的弹窗加上滚动条"）：
-- 一次 probe 上百行，原来 EditBox 直接铺满窗口，超过一屏的那截既看不见也滚不出来。

GQL.LogWindow = {}
local Win = GQL.LogWindow

local PANEL_W, PANEL_H = 720, 480
local LEFT_PAD, RIGHT_PAD = 12, 8
-- 表头那一段（标题行 + 它下面到正文之间的缝）与正文下沿留白
local HEADER_H, BOTTOM_PAD = 34, 8
local SCROLLBAR_W, SCROLLBAR_GAP = 16, 6
-- 正文宽：窗口宽扣掉左右边距，再扣掉滚动条和它左边那道缝。滚动条挂在滚动区**外面**
-- （与设置面板同一套让位算法），不让位的话最后一列字压在滚动条底下。
local BODY_W = PANEL_W - LEFT_PAD - RIGHT_PAD - SCROLLBAR_W - SCROLLBAR_GAP
local VIEW_H = PANEL_H - HEADER_H - BOTTOM_PAD
-- 滚轮一格挪多少像素：与设置面板的 STEP 同值，两个窗口手感一致
local STEP = 30
local FONT = "GameFontNormalSmall"

local frame, scroll, box, bar, meas

--- 滚轮：方向与矩阵、设置面板一致 —— 这个客户端往下滚给的是负 delta，两处都是"减 delta"
--- （见 Options.lua 的 OnWheel：写成加的话两个面板方向相反）。滚轮只改滚动条的值，
--- 真正的滚动由 OnValueChanged 一处驱动，避免两个入口来回写造成抖动。
local function OnWheel(_, delta)
    if not bar or not bar:IsShown() then return end
    local minV, maxV = bar:GetMinMaxValues()
    local v = (bar:GetValue() or 0) - delta * STEP
    if v < minV then v = minV elseif v > maxV then v = maxV end
    bar:SetValue(v)
end

local function Ensure()
    if frame then return frame end
    frame = CreateFrame("Frame", "GQLLogFrame", UIParent, "BackdropTemplate")
    frame:SetSize(PANEL_W, PANEL_H)
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
    title:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -10)
    title:SetText("GoldQuestLedger 日志（Ctrl+A 全选，Ctrl+C 复制）")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -6, -6)
    close:SetScript("OnClick", function() frame:Hide() end)

    scroll = CreateFrame("ScrollFrame", "GQLLogScroll", frame)
    scroll:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT",
        -(RIGHT_PAD + SCROLLBAR_W + SCROLLBAR_GAP), BOTTOM_PAD)
    scroll:EnableMouse(true)
    scroll:SetScript("OnMouseWheel", OnWheel)

    -- SetScrollChild 会自己把正文钉在滚动区左上角，并裁掉超出可视区的那截。
    -- 正文挂滚轮是必须的：EditBox 会把滚轮事件吃掉，只挂滚动区等于鼠标停在字上滚不动。
    box = CreateFrame("EditBox", "GQLLogEditBox", scroll)
    box:SetMultiLine(true)
    box:SetFontObject(FONT)
    box:SetAutoFocus(false)
    box:SetWidth(BODY_W)
    box:SetHeight(VIEW_H)
    box:SetScript("OnMouseWheel", OnWheel)
    box:SetScript("OnEscapePressed", function(self) self:ClearFocus(); frame:Hide() end)
    scroll:SetScrollChild(box)

    -- 标准可拖动滚动条（在滚动区右边）
    bar = CreateFrame("Slider", "GQLLogScrollBar", frame, "UIPanelScrollBarTemplate")
    bar:SetWidth(SCROLLBAR_W)
    bar:SetPoint("TOPLEFT", scroll, "TOPRIGHT", SCROLLBAR_GAP, -16)
    bar:SetPoint("BOTTOMLEFT", scroll, "BOTTOMRIGHT", SCROLLBAR_GAP, 16)
    bar:SetMinMaxValues(0, 0)
    bar:SetValueStep(1)
    bar:SetScript("OnValueChanged", function(_, value)
        scroll:SetVerticalScroll(value)
    end)

    -- 暗尺：EditBox 不报"这些字画出来多高"，只能拿同字体、同宽度的字体串量一遍。
    -- 用 alpha=0 而不是 Hide —— 隐藏帧客户端不一定还给它排版，量回来会是 0。
    meas = frame:CreateFontString(nil, "OVERLAY", FONT)
    meas:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H)
    meas:SetWidth(BODY_W)
    meas:SetAlpha(0)

    frame:Hide()
    return frame
end

--- 报告排出来多高，并把滚动条的值域对上这个高差。
--- 量的是 GetStringHeight（文字本身多高），不是 GetHeight（帧矩形）—— 后者会被显式定高盖掉。
--- 暗尺答不上来（0 或抛错）时按"正好一屏"收 = 不出滚动条，并把这件事记进 /gql log：
--- 宁可承认"滚不动"，也不给一条拖得动、底下全是空白的滚动条（假象比缺功能更难查）。
local function Fit(text)
    local okSet = pcall(meas.SetText, meas, text)
    local okGet, got = pcall(meas.GetStringHeight, meas)
    pcall(meas.SetText, meas, "")
    local h = 0
    if okSet and okGet then h = math.floor(tonumber(got) or 0) end
    if h <= 0 then
        GQL.Config.Log("日志窗：暗尺量不出报告高度（客户端答 " .. tostring(got)
            .. "），本次不给滚动条")
    end
    -- 子帧不许比可视区矮：那样滚动区底下会露出一块空白
    if h < VIEW_H then h = VIEW_H end
    box:SetHeight(h)

    local maxScroll = h - VIEW_H
    if maxScroll > 0 then
        bar:Show()
        bar:SetMinMaxValues(0, maxScroll)
    else
        bar:Hide()
        bar:SetMinMaxValues(0, 0)
    end
end

function Win.Show(text)
    Ensure()
    local existing = box:GetText() or ""
    local full = (existing == "") and tostring(text or "")
        or (existing .. "\n\n" .. tostring(text or ""))
    box:SetText(full)
    Fit(full)
    -- 从第一行读起：原来正文铺满窗口时 SetCursorPosition(0) 也就是"看开头"，
    -- 有了滚动区还得把滚动位置一起归零，否则接着上次的偏移开在半截上。
    box:SetCursorPosition(0)
    bar:SetValue(0)
    scroll:SetVerticalScroll(0)
    frame:Show()
end

function Win.Toggle()
    Ensure()
    if frame:IsShown() then
        frame:Hide()
    else
        Win.Show(GQL.Config.GetLogs())
    end
end

GQL.RegisterCommand("log", Win.Toggle, "打开/关闭日志窗")
