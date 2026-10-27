-- 日志窗：多行 EditBox 承载探测报告与诊断日志。
-- 沿用 AHSnipe 的做法：报告在窗内 Ctrl+A/Ctrl+C 复制回传，不靠截图。
-- 窗口是惰性创建的（Ensure）：加载阶段不碰 CreateFrame，离线单测加载本文件才不会炸。

GQL.LogWindow = {}
local Win = GQL.LogWindow

local frame

local function Ensure()
    if frame then return frame end
    frame = CreateFrame("Frame", "GQLLogFrame", UIParent, "BackdropTemplate")
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
    title:SetText("GoldQuestLedger 日志（Ctrl+A 全选，Ctrl+C 复制）")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -6, -6)
    close:SetScript("OnClick", function() frame:Hide() end)

    local box = CreateFrame("EditBox", "GQLLogEditBox", frame)
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
        Win.Show(GQL.Config.GetLogs())
    end
end

GQL.RegisterCommand("log", Win.Toggle, "打开/关闭日志窗")
