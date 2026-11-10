--[[
    GoldLedger / Goal: Frame.lua
    Goal amount dialog. Triggered by clicking the Goal card in MainFrame.
]]

local ADDON_NAME, ns = ...
ns.Goal = ns.Goal or {}

local Frame = {}
ns.Goal.Frame = Frame

local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.Goal.L end
local function H()  return ns.UI_Helpers end

local goalDialog

function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    if not ui then return end

    local f = ui:CreatePopup({
        name  = "GoldLedgerGoalDialog",
        title = "",                  -- dialog uses a centered title label instead of title bar text
        width = 280, height = 130,
        movable = true,
    })
    f:ClearAllPoints()
    f:SetPoint("CENTER", UIParent, "CENTER", 0, 100)

    -- Hide title bar text (we use our own centered prompt instead)
    if f.title then f.title:SetText("") end

    -- Centered prompt label
    local prompt = ui:CreateLabel(f, {
        text = l["GOAL_INPUT_TEXT"], font = "GameFontNormal", color = "GOLD_TEXT",
    })
    prompt:SetPoint("TOP", f, "TOP", 0, -12)

    -- Amount input
    local editBox = ui:CreateEditBox(f, {
        width = 140, height = 22, maxLetters = 10, autoFocus = true,
    })
    editBox:SetNumeric(true)
    editBox:SetPoint("TOP", prompt, "BOTTOM", 0, -10)
    f.editBox = editBox

    local function AcceptGoal()
        local text = editBox:GetText() or ""
        local amount = tonumber(text)
        if amount and amount > 0 then
            local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
            if Data then Data:SetGoal(amount * 10000) end
        end
        f:Hide()
    end

    local function CancelGoal() f:Hide() end

    local function ClearGoal()
        local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
        if Data then Data:ClearGoal() end
        f:Hide()
    end

    -- Clear button (top row, wide)
    f.clearBtn = ui:CreateButton(f, { label = l["GOAL_CLEAR_BTN"], width = 80, height = 22, onClick = ClearGoal })
    f.clearBtn:SetPoint("BOTTOM", f, "BOTTOM", 0, 34)

    -- OK / Cancel (bottom row)
    f.okBtn = ui:CreateButton(f, { label = ACCEPT or "OK", width = 80, height = 22, onClick = AcceptGoal })
    f.okBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOM", -4, 10)

    f.cancelBtn = ui:CreateButton(f, { label = CANCEL or "Cancel", width = 80, height = 22, onClick = CancelGoal })
    f.cancelBtn:SetPoint("BOTTOMLEFT", f, "BOTTOM", 4, 10)

    -- EditBox handlers
    editBox:SetScript("OnEnterPressed", AcceptGoal)
    editBox:SetScript("OnEscapePressed", CancelGoal)

    -- Refresh MainFrame goal display when dialog closes
    f:SetScript("OnHide", function()
        local coreUI = ns.GoldLedger and ns.GoldLedger:GetModule("UI")
        if coreUI and coreUI.RefreshGoal then coreUI:RefreshGoal() end
    end)

    goalDialog = f
    return f
end

function Frame:Show()
    if not goalDialog then self:Create() end
    local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
    if Data then
        local current = Data:GetGoal() or 0
        if current > 0 then
            goalDialog.editBox:SetText(tostring(math.floor(current / 10000)))
        else
            goalDialog.editBox:SetText("")
        end
    end
    goalDialog:Show()
    if goalDialog.editBox.HighlightText then goalDialog.editBox:HighlightText() end
    if goalDialog.editBox.SetFocus then goalDialog.editBox:SetFocus() end
end

function Frame:Toggle()
    if goalDialog and goalDialog:IsShown() then
        goalDialog:Hide()
    else
        self:Show()
    end
end

function Frame:GetFrame() return goalDialog end
function Frame:SetFrame(f) goalDialog = f end
