-- 诊断日志窗口：/ahsnipe log 打开。
-- 游戏内无法直接复制聊天框文本，用多行 EditBox 承载日志——点击框内 Ctrl+A 全选、
-- Ctrl+C 复制，即可粘贴给开发者（不再需要截图）。最新条目在底部，打开时自动滚动到底。

local AHSnipe = AHSnipe

AHSnipe.LogWindow = {}
local Win = AHSnipe.LogWindow

local frame = nil

local function BuildFrame()
    if frame then return end

    local f = CreateFrame("Frame", "AHSnipeLogFrame", UIParent, "BasicFrameTemplateWithInset")
    f:SetSize(580, 460)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:SetMovable(true)
    f:EnableMouse(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    f:Hide()
    if f.TitleText then
        f.TitleText:SetText("AHSnipe 诊断日志")
    end

    -- 操作提示
    local hint = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    hint:SetPoint("TOPLEFT", 14, -30)
    hint:SetText("点击下面文本框 → Ctrl+A 全选 → Ctrl+C 复制 → 粘贴发给开发者（最新在底部）")

    -- 滚动区 + 多行 EditBox（支持全选复制）
    local scroll = CreateFrame("ScrollFrame", nil, f, "UIPanelScrollFrameTemplate")
    scroll:SetPoint("TOPLEFT", 14, -52)
    scroll:SetPoint("BOTTOMRIGHT", -32, 14)

    local edit = CreateFrame("EditBox", nil, scroll)
    edit:SetMultiLine(true)
    edit:SetFontObject(GameFontHighlightSmall)
    edit:SetAutoFocus(false)
    edit:SetWidth(520)
    scroll:SetScrollChild(edit)

    f.scroll = scroll
    f.edit = edit
    frame = f
end

function Win.Show()
    local ok, err = pcall(BuildFrame)
    if not ok or not frame then
        AHSnipe.Config.Log(("日志窗创建失败：%s"):format(tostring(err)))
        return
    end
    local logs = AHSnipe.DB().logs or {}
    local text
    if #logs == 0 then
        text = "（暂无日志）"
    else
        text = table.concat(logs, "\n")
    end
    frame.edit:SetHeight(math.max(200, 13 * (#logs + 2)))
    frame.edit:SetText(text)
    -- 打开时滚动到底部（最新条目）
    frame.scroll:SetVerticalScroll(math.max(0, frame.edit:GetHeight() - frame.scroll:GetHeight()))
    frame:Show()
end

function Win.Hide()
    if frame then frame:Hide() end
end

function Win.Toggle()
    BuildFrame()
    if frame:IsShown() then
        Win.Hide()
    else
        Win.Show()
    end
end

function Win.Init()
    -- 无额外初始化，UI 延迟创建
end
