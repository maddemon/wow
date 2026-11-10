--[[
    GoldLedger / Export: Frame.lua
    Multi-line CSV export popup.
]]

local ADDON_NAME, ns = ...
ns.Export = ns.Export or {}

local Frame = {}
ns.Export.Frame = Frame

local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.Export.L end
local function H()  return ns.UI_Helpers end

local exportFrame

function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    if not ui then return end

    local f = ui:CreatePopup({
        name  = "GoldLedgerExportFrame",
        title = l["EXPORT_TITLE"],
        width = 500, height = 350,
    })

    local hint = ui:CreateLabel(f, { text = l["EXPORT_HINT"], color = "TEXT_HINT" })
    hint:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -36)

    -- Scroll area with multiline EditBox inside
    local scrollFrame = ui:CreateScrollFrame(f, { bgColor = "CHART_BG" })
    scrollFrame:SetPoint("TOPLEFT", f, "TOPLEFT", 8, -52)
    scrollFrame:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -28, 8)

    -- Replace scrollFrame.child with our multiline EditBox
    local editBox = CreateFrame("EditBox", "GoldLedgerExportEditBox", scrollFrame)
    editBox:SetMultiLine(true)
    editBox:SetAutoFocus(false)
    editBox:SetFontObject(ChatFontNormal)
    editBox:SetWidth(460)
    editBox:SetScript("OnEscapePressed", function(self)
        self:ClearFocus()
        f:Hide()
    end)
    scrollFrame:SetScrollChild(editBox)
    f.editBox = editBox

    exportFrame = f
    return f
end

function Frame:Show()
    if not exportFrame then self:Create() end
    if exportFrame:IsShown() then
        exportFrame:Hide()
        return
    end

    local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
    if Data and Data.GetExportCSV then
        exportFrame.editBox:SetText(Data:GetExportCSV())
    end

    local helpers = H()
    if helpers and helpers.SnapToMain then helpers.SnapToMain(exportFrame) end

    exportFrame:Show()
    exportFrame.editBox:HighlightText()
    exportFrame.editBox:SetFocus()
end

function Frame:Toggle() self:Show() end
function Frame:GetFrame() return exportFrame end
function Frame:SetFrame(f) exportFrame = f end
