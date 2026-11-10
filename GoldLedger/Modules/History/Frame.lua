--[[
    GoldLedger / History: Frame.lua
    Transaction history popup with pagination + filters (source, type, minAmount).
    Uses UI public API — avoids direct WoW calls where possible.
]]

local ADDON_NAME, ns = ...
ns.History = ns.History or {}

local Frame = {}
ns.History.Frame = Frame

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------
local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.History.L end
local function H()  return ns.UI_Helpers end

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------
local historyFrame
local state = {
    page      = 1,
    perPage   = 50,
    source    = "all",
    entryType = "all",
    minAmount = 0,
}

local ROW_HEIGHT = 20

-------------------------------------------------------------------------------
-- Entry row factory
-------------------------------------------------------------------------------
local function CreateEntryRow(parent, index)
    local ui = UI()
    local row = ui:CreateListRow(parent, { index = index, height = ROW_HEIGHT, altBg = "ROW_ALT" })

    row.dateText = ui:CreateLabel(row, { width = 75, justify = "LEFT", color = "TEXT_TIME" })
    row.dateText:SetPoint("LEFT", row, "LEFT", 4, 0)

    row.timeText = ui:CreateLabel(row, { width = 42, justify = "LEFT", color = "TEXT_TIME" })
    row.timeText:SetPoint("LEFT", row.dateText, "RIGHT", 4, 0)

    row.sourceTag = ui:CreateLabel(row, { width = 70, justify = "LEFT" })
    row.sourceTag:SetPoint("LEFT", row.timeText, "RIGHT", 4, 0)

    row.amountText = ui:CreateLabel(row, { justify = "RIGHT" })
    row.amountText:SetPoint("RIGHT", row, "RIGHT", -4, 0)

    function row:SetEntry(entry)
        local helpers = H()
        local GL = ns.GoldLedger
        local coreL = GL and ns.L
        self.dateText:SetText(date("%Y-%m-%d", entry.timestamp))
        self.timeText:SetText(date((coreL and coreL["TIME_FORMAT"]) or "%H:%M", entry.timestamp))
        if helpers and helpers.GoldFormatter then
            self.amountText:SetText(helpers.GoldFormatter.Colored(entry.amount, entry.type))
        end

        local source = entry.source or "unknown"
        local Tracker = GL and GL:GetModule("Tracker")
        local localeKey = Tracker and Tracker:GetSourceLocaleKey(source) or "SRC_UNKNOWN"
        local sc = helpers and helpers.GetSourceColors and helpers.GetSourceColors() or {}
        local color = sc[source] or sc.unknown or { 1, 1, 1 }
        if coreL then self.sourceTag:SetText(coreL[localeKey]) end
        self.sourceTag:SetTextColor(color[1], color[2], color[3])
    end

    return row
end

-------------------------------------------------------------------------------
-- Create popup
-------------------------------------------------------------------------------
function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    local coreL = ns.L
    if not ui then return end

    local f = ui:CreatePopup({
        name  = "GoldLedgerHistoryFrame",
        title = l["HISTORY_TITLE"],
        width = 600, height = 450,
    })

    ---------------------------------------------------------------------------
    -- Source filter buttons (colored tags)
    ---------------------------------------------------------------------------
    local filterY = -36

    local FILTER_SOURCES = {
        { key = "all",       localeKey = "FILTER_ALL" },
        { key = "vendor",    localeKey = "SRC_VENDOR" },
        { key = "repair",    localeKey = "SRC_REPAIR" },
        { key = "ah",        localeKey = "SRC_AH" },
        { key = "mail",      localeKey = "SRC_MAIL" },
        { key = "quest",     localeKey = "SRC_QUEST" },
        { key = "loot",      localeKey = "SRC_LOOT" },
        { key = "trade",     localeKey = "SRC_TRADE" },
        { key = "unknown",   localeKey = "SRC_UNKNOWN" },
        { key = "warband",   localeKey = "SRC_WARBAND" },
        { key = "guildbank", localeKey = "SRC_GUILDBANK" },
    }

    f.sourceButtons = {}
    local xOff = 0
    local sc = helpers and helpers.GetSourceColors and helpers.GetSourceColors() or {}

    for _, info in ipairs(FILTER_SOURCES) do
        local btn = CreateFrame("Button", nil, f)  -- bare button (custom styling below)
        btn:SetHeight(18)

        local btnText = ui:CreateLabel(btn, { text = coreL and coreL[info.localeKey] or info.key })
        btnText:SetPoint("CENTER", 0, 0)

        local textW = btnText:GetStringWidth()
        if textW < 5 then textW = 24 end
        btn:SetWidth(textW + 12)
        btn:SetPoint("TOPLEFT", f, "TOPLEFT", 10 + xOff, filterY)

        local bg = btn:CreateTexture(nil, "BACKGROUND")
        bg:SetAllPoints()

        btn.text = btnText
        btn.bg = bg
        btn.key = info.key
        btn.color = sc[info.key] or { 1, 1, 1 }

        btn:SetScript("OnClick", function()
            state.source = info.key
            state.page = 1
            Frame:Update()
        end)

        f.sourceButtons[#f.sourceButtons + 1] = btn
        xOff = xOff + btn:GetWidth() + 3
    end

    -- Кнопки меряются по реальной ширине текста (зависит от шрифта/локали):
    -- расширяем окно, если ряд фильтров с отступами по 10px не влезает
    local filterRowWidth = 10 + xOff + 10
    if filterRowWidth > f:GetWidth() then
        f:SetWidth(filterRowWidth)
    end

    ---------------------------------------------------------------------------
    -- Type filter buttons
    ---------------------------------------------------------------------------
    local typeY = filterY - 22
    local TYPE_FILTERS = {
        { key = "all",     text = l["HISTORY_TYPE_ALL"] },
        { key = "income",  text = l["HISTORY_TYPE_INCOME"] },
        { key = "expense", text = l["HISTORY_TYPE_EXPENSE"] },
    }

    f.typeButtons = {}
    xOff = 0

    for _, info in ipairs(TYPE_FILTERS) do
        local charLen = strlenutf8 and strlenutf8(info.text or "") or #(info.text or "")
        local width = math.max(50, charLen * 10 + 16)
        local btn = ui:CreateButton(f, {
            label = info.text, width = width, height = 18,
            onClick = function()
                state.entryType = info.key
                state.page = 1
                Frame:Update()
            end,
        })
        btn:SetPoint("TOPLEFT", f, "TOPLEFT", 10 + xOff, typeY)
        btn.key = info.key
        f.typeButtons[#f.typeButtons + 1] = btn
        xOff = xOff + btn:GetWidth() + 4
    end

    ---------------------------------------------------------------------------
    -- Min amount input
    ---------------------------------------------------------------------------
    local minLabel = ui:CreateLabel(f, { text = l["HISTORY_MIN_AMOUNT"], color = "LABEL" })
    minLabel:SetPoint("TOPLEFT", f, "TOPLEFT", 10 + xOff + 12, typeY - 2)

    local minEdit = ui:CreateEditBox(f, {
        width = 60, height = 18, maxLetters = 8,
        onEnterPressed = function(self)
            local val = tonumber(self:GetText()) or 0
            state.minAmount = val * 10000  -- gold → copper
            state.page = 1
            self:ClearFocus()
            Frame:Update()
        end,
    })
    minEdit:SetNumeric(true)
    minEdit:SetPoint("LEFT", minLabel, "RIGHT", 4, 0)
    f.minEdit = minEdit

    ---------------------------------------------------------------------------
    -- Scroll area
    ---------------------------------------------------------------------------
    local scrollY = typeY - 24
    local scrollFrame = ui:CreateScrollFrame(f, { childWidth = 560, bgColor = "SCROLL_BG" })
    scrollFrame:SetPoint("TOPLEFT", f, "TOPLEFT", 4, scrollY)
    scrollFrame:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -24, 40)

    f.scrollChild = scrollFrame.child
    f.entryRows = {}

    f.noDataLabel = ui:CreateLabel(f.scrollChild, { text = l["HISTORY_NO_RESULTS"], font = "GameFontDisable" })
    f.noDataLabel:SetPoint("TOP", f.scrollChild, "TOP", 0, -20)

    ---------------------------------------------------------------------------
    -- Pagination
    ---------------------------------------------------------------------------
    f.prevBtn = ui:CreateButton(f, {
        label = l["HISTORY_PREV"], width = 60, height = 22,
        onClick = function()
            if state.page > 1 then
                state.page = state.page - 1
                Frame:Update()
            end
        end,
    })
    f.prevBtn:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 10, 10)

    f.nextBtn = ui:CreateButton(f, {
        label = l["HISTORY_NEXT"], width = 60, height = 22,
        onClick = function()
            if state.page < (f.totalPages or 1) then
                state.page = state.page + 1
                Frame:Update()
            end
        end,
    })
    f.nextBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -10, 10)

    f.pageLabel = ui:CreateLabel(f, { color = "LABEL" })
    f.pageLabel:SetPoint("BOTTOM", f, "BOTTOM", -40, 15)

    f.showingLabel = ui:CreateLabel(f, { color = "TEXT_DIM" })
    f.showingLabel:SetPoint("BOTTOM", f, "BOTTOM", 40, 15)

    historyFrame = f
    return f
end

-------------------------------------------------------------------------------
-- Update
-------------------------------------------------------------------------------
function Frame:Update()
    if not historyFrame or not historyFrame:IsShown() then return end
    local l = L()
    local helpers = H()
    local TC = helpers and helpers.TC or function() return 1,1,1,1 end

    local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
    if not Data or not Data.GetFilteredEntries then return end

    local result = Data:GetFilteredEntries({
        page      = state.page,
        perPage   = state.perPage,
        source    = state.source,
        entryType = state.entryType,
        minAmount = state.minAmount,
    })

    historyFrame.totalPages = result.totalPages

    -- Update source buttons
    for _, btn in ipairs(historyFrame.sourceButtons) do
        if btn.key == state.source then
            btn.bg:SetColorTexture(btn.color[1], btn.color[2], btn.color[3], 0.3)
            btn.text:SetTextColor(btn.color[1], btn.color[2], btn.color[3])
        else
            btn.bg:SetColorTexture(TC("FILTER_INACTIVE_BG"))
            btn.text:SetTextColor(TC("FILTER_INACTIVE_TEXT"))
        end
    end

    -- Update type buttons via SDK SetActive method
    for _, btn in ipairs(historyFrame.typeButtons) do
        if btn.SetActive then btn:SetActive(btn.key == state.entryType) end
    end

    -- Rows
    historyFrame.noDataLabel:SetShown(#result.entries == 0)
    for i, entry in ipairs(result.entries) do
        local row = historyFrame.entryRows[i]
        if not row then
            row = CreateEntryRow(historyFrame.scrollChild, i)
            historyFrame.entryRows[i] = row
        end
        row:SetEntry(entry)
        row:Show()
    end
    for i = #result.entries + 1, #historyFrame.entryRows do
        historyFrame.entryRows[i]:Hide()
    end
    historyFrame.scrollChild:SetHeight(math.max(1, #result.entries * ROW_HEIGHT))

    -- Pagination labels
    if result.totalPages > 0 then
        historyFrame.pageLabel:SetText(l["HISTORY_PAGE"]:format(result.page, result.totalPages))
    else
        historyFrame.pageLabel:SetText("")
    end
    if result.total and result.total > 0 then
        local from = (result.page - 1) * state.perPage + 1
        local to = math.min(from + #result.entries - 1, result.total)
        historyFrame.showingLabel:SetText(l["HISTORY_SHOWING"]:format(from, to, result.total))
    else
        historyFrame.showingLabel:SetText("")
    end
    historyFrame.prevBtn:SetAlpha(state.page > 1 and 1 or 0.4)
    historyFrame.nextBtn:SetAlpha(state.page < result.totalPages and 1 or 0.4)
end

-------------------------------------------------------------------------------
-- Public
-------------------------------------------------------------------------------
function Frame:ResetState()
    state.page = 1
    state.source = "all"
    state.entryType = "all"
    state.minAmount = 0
    if historyFrame and historyFrame.minEdit then
        historyFrame.minEdit:SetText("")
    end
end

function Frame:Toggle()
    if not historyFrame then
        self:Create()
    end
    if historyFrame:IsShown() then
        historyFrame:Hide()
    else
        self:ResetState()
        local helpers = H()
        if helpers and helpers.SnapToMain then
            helpers.SnapToMain(historyFrame)
        end
        historyFrame:Show()
        self:Update()
    end
end

function Frame:GetFrame()
    return historyFrame
end

function Frame:SetFrame(f)
    historyFrame = f
end
