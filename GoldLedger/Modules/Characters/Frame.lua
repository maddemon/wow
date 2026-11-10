--[[
    GoldLedger / Characters: Frame.lua
    Multi-character summary popup with period tabs (day/week/month).
]]

local ADDON_NAME, ns = ...
ns.Characters = ns.Characters or {}

local Frame = {}
ns.Characters.Frame = Frame

local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.Characters.L end
local function H()  return ns.UI_Helpers end

local charsFrame
local CHAR_ROW_HEIGHT = 40

-------------------------------------------------------------------------------
-- Update
-------------------------------------------------------------------------------
local function UpdateList()
    if not charsFrame or not charsFrame:IsShown() then return end
    local ui = UI()
    local helpers = H()
    if not helpers then return end

    local coreL = ns.L
    local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
    if not Data or not Data.GetAllCharactersSummary then return end

    local TC = helpers.TC or function() return 1,1,1,1 end
    local T  = helpers.T  or function() return {1,1,1} end

    local period = charsFrame.activePeriod or "month"
    local chars = Data:GetAllCharactersSummary(period)
    local scrollChild = charsFrame.scrollChild

    for i, charInfo in ipairs(chars) do
        local row = charsFrame.charRows[i]
        if not row then
            row = ui:CreateListRow(scrollChild, { index = i, height = CHAR_ROW_HEIGHT, altBg = "ROW_ALT" })

            row.nameText = ui:CreateLabel(row, { font = "GameFontHighlight", justify = "LEFT" })
            row.nameText:SetPoint("TOPLEFT", row, "TOPLEFT", 8, -4)

            row.detailText = ui:CreateLabel(row, { justify = "LEFT", color = "LABEL" })
            row.detailText:SetPoint("BOTTOMLEFT", row, "BOTTOMLEFT", 8, 4)

            row.netText = ui:CreateLabel(row, { font = "GameFontHighlight", justify = "RIGHT" })
            row.netText:SetPoint("RIGHT", row, "RIGHT", -8, 0)

            charsFrame.charRows[i] = row
        end

        row.nameText:SetText(charInfo.name)

        local detail = (coreL and coreL["HEADER_INCOME"] or "Income") .. ": " ..
            helpers.GoldFormatter.Short(charInfo.income) ..
            "  " .. (coreL and coreL["HEADER_EXPENSE"] or "Expense") .. ": " ..
            helpers.GoldFormatter.Short(charInfo.expense)
        row.detailText:SetText(detail)

        local netColor = charInfo.net >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
        local netSign = charInfo.net >= 0 and "+" or "-"
        row.netText:SetText(netSign .. helpers.GoldFormatter.Full(math.abs(charInfo.net)))
        row.netText:SetTextColor(netColor[1], netColor[2], netColor[3])

        row:Show()
    end

    for i = #chars + 1, #charsFrame.charRows do
        charsFrame.charRows[i]:Hide()
    end
    scrollChild:SetHeight(math.max(1, #chars * CHAR_ROW_HEIGHT))

    local totalNet = 0
    for _, charInfo in ipairs(chars) do totalNet = totalNet + charInfo.net end
    local tc = totalNet >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
    charsFrame.totalValue:SetText((totalNet >= 0 and "+" or "-") ..
        helpers.GoldFormatter.Full(math.abs(totalNet)))
    charsFrame.totalValue:SetTextColor(tc[1], tc[2], tc[3])
end

-------------------------------------------------------------------------------
-- Create
-------------------------------------------------------------------------------
function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    if not ui then return end

    local f = ui:CreatePopup({
        name  = "GoldLedgerCharsFrame",
        title = l["CHARS_TITLE"],
        width = 340, height = 380,
    })

    -- Period tabs
    local tabDefs = {
        { period = "day",   label = l["CHARS_DAY_LABEL"]   },
        { period = "week",  label = l["CHARS_WEEK_LABEL"]  },
        { period = "month", label = l["CHARS_MONTH_LABEL"] },
    }
    f.tabButtons = {}
    f.activePeriod = "month"
    local tabX = 10
    for _, def in ipairs(tabDefs) do
        -- strlenutf8 counts characters, not bytes — important for Cyrillic labels
        local charLen = strlenutf8 and strlenutf8(def.label) or #def.label
        local width = math.max(60, charLen * 10 + 16)
        local tab = ui:CreateButton(f, {
            label = def.label, width = width, height = 18,
            onClick = function()
                f.activePeriod = def.period
                for _, tb in ipairs(f.tabButtons) do
                    if tb.SetActive then tb:SetActive(tb.period == f.activePeriod) end
                end
                UpdateList()
            end,
        })
        tab:SetPoint("TOPLEFT", f, "TOPLEFT", tabX, -34)
        tab.period = def.period
        tabX = tabX + tab:GetWidth() + 4

        if def.period == "month" and tab.SetActive then tab:SetActive(true) end
        table.insert(f.tabButtons, tab)
    end

    -- Scroll area
    local scrollFrame = ui:CreateScrollFrame(f, { childWidth = 300, bgColor = "SCROLL_BG" })
    scrollFrame:SetPoint("TOPLEFT", f, "TOPLEFT", 6, -56)
    scrollFrame:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -26, 44)

    f.scrollChild = scrollFrame.child
    f.charRows = {}

    if helpers and helpers.MakeSeparator then helpers.MakeSeparator(f, -340) end

    f.totalLabel = ui:CreateLabel(f, { text = l["CHARS_ACCOUNT_TOTAL"], font = "GameFontNormal", color = "HEADER" })
    f.totalLabel:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 14, 14)

    f.totalValue = ui:CreateLabel(f, { font = "GameFontNormal", justify = "RIGHT" })
    f.totalValue:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -14, 14)

    charsFrame = f
    return f
end

-------------------------------------------------------------------------------
-- Public
-------------------------------------------------------------------------------
function Frame:Toggle()
    if not charsFrame then
        self:Create()
    end
    if charsFrame:IsShown() then
        charsFrame:Hide()
    else
        local helpers = H()
        if helpers and helpers.SnapToMain then
            helpers.SnapToMain(charsFrame)
        end
        charsFrame:Show()
        UpdateList()
    end
end

function Frame:Update() UpdateList() end
function Frame:GetFrame() return charsFrame end
function Frame:SetFrame(f) charsFrame = f end
