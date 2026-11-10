--[[
    GoldLedger: UI_MainFrame.lua
    Main dashboard frame: cards, chart, transactions, goal
]]

local ADDON_NAME, ns = ...
local GoldLedger = ns.GoldLedger
local L = ns.L

-------------------------------------------------------------------------------
-- Wait for UI.lua helpers (loaded before this file)
-------------------------------------------------------------------------------
local H    -- will be set in init
local Anim -- animation helpers

local function Init()
    H = ns.UI_Helpers
    Anim = ns.UI_Animations
end

-------------------------------------------------------------------------------
-- Constants
-------------------------------------------------------------------------------
local FRAME_WIDTH = 720
local FRAME_HEIGHT = 520
local CHART_HEIGHT = 100
local CHART_PADDING_LEFT = 8
local CHART_PADDING_RIGHT = 8
local ROW_HEIGHT = 20
local MAX_VISIBLE = 50

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------
local mainFrame
local activeFilter = "all"
local activeChartPeriod = "7d"

-------------------------------------------------------------------------------
-- Forward declarations
-------------------------------------------------------------------------------
local UpdateEntryRows
local UpdateChart

-------------------------------------------------------------------------------
-- CreateMainFrame
-------------------------------------------------------------------------------
local function CreateMainFrame()
    Init()
    local TC, T = H.TC, H.T

    -- Main frame via UI API (CreatePopup with DIALOG strata so popups cover it)
    local coreUI = GoldLedger:GetModule("UI")
    local f = coreUI:CreatePopup({
        name   = "GoldLedgerMainFrame",
        title  = "GoldLedger",
        width  = FRAME_WIDTH,
        height = FRAME_HEIGHT,
        strata = "DIALOG",
    })
    f:ClearAllPoints()
    f:SetPoint("CENTER")

    -- Header buttons row — positioned BELOW native title bar (inset area top)
    -- Rightmost: Settings (⚙), then feature buttons growing leftward.
    local HEADER_BTN_H = 22
    local HEADER_Y = -28                 -- below native title bar (~22px + 4 padding)
    local headerRowRightOffset = -6      -- from frame's right edge

    -- Кнопка "Настройки" (⚙) — RIGHTMOST
    local settingsBtn = coreUI:CreateButton(f, {
        label   = "|TInterface\\Buttons\\UI-OptionsButton:14:14|t",
        width   = 28, height = HEADER_BTN_H,
        tooltip = L["SETTINGS_BUTTON"],
        onClick = function() coreUI:ToggleSettingsFrame() end,
    })
    settingsBtn:SetPoint("TOPRIGHT", f, "TOPRIGHT", headerRowRightOffset, HEADER_Y)

    -- Dynamic feature module buttons (registered via GoldLedger:RegisterHeaderButton)
    -- anchor to the LEFT of Settings, growing further left.
    local leftAnchor = settingsBtn
    local featureButtons = GoldLedger.GetHeaderButtons and GoldLedger:GetHeaderButtons() or {}
    for _, btnDef in ipairs(featureButtons) do
        local labelText = type(btnDef.label) == "function" and btnDef.label() or btnDef.label
        local featureBtn = coreUI:CreateButton(f, {
            label   = labelText,
            width   = 96, height = HEADER_BTN_H,
            onClick = btnDef.callback,
        })
        featureBtn:SetPoint("RIGHT", leftAnchor, "LEFT", -4, 0)
        leftAnchor = featureBtn
    end

    ---------------------------------------------------------------------------
    -- Row 1: Stat cards
    ---------------------------------------------------------------------------
    local CARD_W = 166
    local CARD_H = 80
    local CARD_GAP = 8
    local cardsY = -58  -- below native title bar (-22) + header button row (-28..-50) + padding

    -- 1st card: Today  (was "On Hand" — content moved to the Goal card below)
    local cardToday = coreUI:CreateCard(f, { title = L["HEADER_TODAY"], width = CARD_W, height = CARD_H })
    cardToday:SetPoint("TOPLEFT", f, "TOPLEFT", 10, cardsY)
    f.todayIncome  = coreUI:CreateCardRow(cardToday, { label = L["HEADER_INCOME"], yOffset = -22 })
    f.todayExpense = coreUI:CreateCardRow(cardToday, { label = L["HEADER_EXPENSE"], yOffset = -36 })
    f.todayBalance = coreUI:CreateCardRow(cardToday, { label = L["HEADER_BALANCE"], yOffset = -52 })

    -- 2nd card: Yesterday  (new — uses Data:GetYesterdayDateKey() in UpdateSummaries)
    local cardYesterday = coreUI:CreateCard(f, { title = L["HEADER_YESTERDAY"], width = CARD_W, height = CARD_H })
    cardYesterday:SetPoint("LEFT", cardToday, "RIGHT", CARD_GAP, 0)
    f.yesterdayIncome  = coreUI:CreateCardRow(cardYesterday, { label = L["HEADER_INCOME"], yOffset = -22 })
    f.yesterdayExpense = coreUI:CreateCardRow(cardYesterday, { label = L["HEADER_EXPENSE"], yOffset = -36 })
    f.yesterdayBalance = coreUI:CreateCardRow(cardYesterday, { label = L["HEADER_BALANCE"], yOffset = -52 })

    local cardSession = coreUI:CreateCard(f, { title = L["HEADER_SESSION"], width = CARD_W, height = CARD_H })
    cardSession:SetPoint("LEFT", cardYesterday, "RIGHT", CARD_GAP, 0)
    f.sessionIncome  = coreUI:CreateCardRow(cardSession, { label = L["HEADER_INCOME"], yOffset = -22 })
    f.sessionExpense = coreUI:CreateCardRow(cardSession, { label = L["HEADER_EXPENSE"], yOffset = -36 })
    f.sessionNet     = coreUI:CreateCardRow(cardSession, { label = L["HEADER_NET"], yOffset = -52 })

    local cardMonth = coreUI:CreateCard(f, { title = L["HEADER_MONTH"], width = CARD_W, height = CARD_H })
    cardMonth:SetPoint("LEFT", cardSession, "RIGHT", CARD_GAP, 0)
    f.monthIncome  = coreUI:CreateCardRow(cardMonth, { label = L["HEADER_INCOME"], yOffset = -22 })
    f.monthExpense = coreUI:CreateCardRow(cardMonth, { label = L["HEADER_EXPENSE"], yOffset = -36 })
    f.monthBalance = coreUI:CreateCardRow(cardMonth, { label = L["HEADER_BALANCE"], yOffset = -52 })

    -- store card refs for animations
    f._cards = { cardToday, cardYesterday, cardSession, cardMonth }

    ---------------------------------------------------------------------------
    -- Row 2: Chart + Goal
    ---------------------------------------------------------------------------
    local row2Y = cardsY - CARD_H - CARD_GAP
    local CHART_CARD_W = 460
    local GOAL_CARD_W = FRAME_WIDTH - CHART_CARD_W - 10 - 10 - CARD_GAP

    local chartCard = coreUI:CreateCard(f, { title = L["HEADER_CHART"], width = CHART_CARD_W, height = 160 })
    chartCard:SetPoint("TOPLEFT", f, "TOPLEFT", 10, row2Y)

    -- Chart period tabs via UI:CreateTabGroup
    local chartPeriodGroup = coreUI:CreateTabGroup(chartCard, {
        tabs = {
            { key = "7d",  label = L["CHART_7D"]  },
            { key = "30d", label = L["CHART_30D"] },
            { key = "all", label = L["CHART_ALL"] },
        },
        initial = activeChartPeriod,
        onSelect = function(key)
            activeChartPeriod = key
            UpdateChart()
        end,
    })
    -- Re-anchor to top-right of chart card
    for i, tab in ipairs(chartPeriodGroup.tabs) do
        tab:ClearAllPoints()
    end
    local anchorX = -6
    for i = #chartPeriodGroup.tabs, 1, -1 do
        local tab = chartPeriodGroup.tabs[i]
        tab:SetPoint("TOPRIGHT", chartCard, "TOPRIGHT", anchorX, -5)
        anchorX = anchorX - tab:GetWidth() - 4
    end
    f.chartPeriodGroup = chartPeriodGroup

    -- Legend
    -- Legend via SDK (labels only; color squares kept as internal texture detail)
    local legendIncome = chartCard:CreateTexture(nil, "ARTWORK")
    legendIncome:SetSize(8, 8)
    legendIncome:SetPoint("TOPLEFT", chartCard, "TOPLEFT", 8, -22)
    legendIncome:SetColorTexture(T("INCOME")[1], T("INCOME")[2], T("INCOME")[3], 0.9)

    local legendIncomeText = coreUI:CreateLabel(chartCard, { text = L["HEADER_INCOME"], color = "LABEL" })
    legendIncomeText:SetPoint("LEFT", legendIncome, "RIGHT", 3, 0)

    local legendExpense = chartCard:CreateTexture(nil, "ARTWORK")
    legendExpense:SetSize(8, 8)
    legendExpense:SetPoint("LEFT", legendIncomeText, "RIGHT", 8, 0)
    legendExpense:SetColorTexture(T("EXPENSE")[1], T("EXPENSE")[2], T("EXPENSE")[3], 0.9)

    local legendExpenseText = coreUI:CreateLabel(chartCard, { text = L["HEADER_EXPENSE"], color = "LABEL" })
    legendExpenseText:SetPoint("LEFT", legendExpense, "RIGHT", 3, 0)

    -- Chart container
    local chartContainer = CreateFrame("Frame", nil, chartCard)
    chartContainer:SetHeight(CHART_HEIGHT)
    chartContainer:SetPoint("TOPLEFT", chartCard, "TOPLEFT", CHART_PADDING_LEFT, -34)
    chartContainer:SetPoint("TOPRIGHT", chartCard, "TOPRIGHT", -CHART_PADDING_RIGHT, -34)

    local chartBg = chartContainer:CreateTexture(nil, "BACKGROUND")
    chartBg:SetAllPoints()
    chartBg:SetColorTexture(TC("CHART_BG"))

    local guide = chartContainer:CreateTexture(nil, "ARTWORK")
    guide:SetHeight(1)
    guide:SetPoint("BOTTOMLEFT", chartContainer, "BOTTOMLEFT", 0, CHART_HEIGHT * 0.5)
    guide:SetPoint("BOTTOMRIGHT", chartContainer, "BOTTOMRIGHT", 0, CHART_HEIGHT * 0.5)
    guide:SetColorTexture(TC("CHART_GUIDE"))

    f.chartMaxLabel = coreUI:CreateLabel(chartContainer, { color = "TEXT_DIM" })
    f.chartMaxLabel:SetPoint("TOPRIGHT", chartContainer, "TOPRIGHT", -2, -2)

    f.chartDayLabels = {}
    f.chartContainer = chartContainer
    f.chartBars = {}
    f.chartHitFrames = {}

    -- Goal card — vertical layout: On Hand (top) | divider | Goal (bottom)
    local goalCard = coreUI:CreateCard(f, { title = L["HEADER_GOAL"], width = GOAL_CARD_W, height = 160 })
    goalCard:SetPoint("LEFT", chartCard, "RIGHT", CARD_GAP, 0)

    ---- Section 1: On Hand (top) ---------------------------------------------
    f.onHandLabel = coreUI:CreateLabel(goalCard, {
        text = L["HEADER_ON_HAND"], color = "LABEL", justify = "CENTER",
    })
    f.onHandLabel:SetPoint("TOP", goalCard, "TOP", 0, -28)

    f.onHandValue = coreUI:CreateLabel(goalCard, {
        font = "GameFontNormalLarge", justify = "CENTER", color = "GOLD_TEXT",
    })
    f.onHandValue:SetPoint("TOP", f.onHandLabel, "BOTTOM", 0, -4)

    ---- Divider --------------------------------------------------------------
    f.goalDivider = H.MakeSeparator(goalCard, -78)

    ---- Section 2: Goal (bottom) — visible CTA button ------------------------
    -- Compact progress bar
    local goalBar = coreUI:CreateProgressBar(goalCard, {
        width = GOAL_CARD_W - 20, height = 14,
    })
    goalBar:SetPoint("TOPLEFT", goalCard, "TOPLEFT", 10, -88)
    f.goalBar = goalBar
    f.goalBarBg = goalBar.bg
    f.goalBarFill = goalBar.fill

    -- Progress + ETA text below the bar (was overlaid on the bar — too small
    -- and the bar fill made it muddy). Bigger fonts + brighter ETA color.
    f.goalText = coreUI:CreateLabel(goalCard, {
        font = "GameFontHighlight", color = "TEXT_WHITE", justify = "CENTER",
    })
    f.goalText:SetPoint("TOP", f.goalBarBg, "BOTTOM", 0, -3)

    f.goalEta = coreUI:CreateLabel(goalCard, {
        font = "GameFontHighlight", color = "LABEL", justify = "CENTER",
    })
    f.goalEta:SetPoint("TOP", f.goalText, "BOTTOM", 0, -1)

    -- Native button = obvious "click to set/change goal" CTA
    f.goalCtaBtn = coreUI:CreateButton(goalCard, {
        width  = GOAL_CARD_W - 30,
        height = 22,
        text   = L["GOAL_SET_BUTTON"],
        onClick = function()
            local Goal = ns.Goal and ns.Goal.Frame
            if Goal and Goal.Show then Goal:Show() end
        end,
    })
    f.goalCtaBtn:SetPoint("BOTTOM", goalCard, "BOTTOM", 0, 8)

    ---------------------------------------------------------------------------
    -- Row 3: Recent Transactions
    ---------------------------------------------------------------------------
    local row3Y = row2Y - 160 - CARD_GAP

    local txCard = coreUI:CreateCard(f, { title = L["HEADER_RECENT"], width = FRAME_WIDTH - 20, height = FRAME_HEIGHT - math.abs(row3Y) - 14 })
    txCard:SetPoint("TOPLEFT", f, "TOPLEFT", 10, row3Y)

    local BreakdownL = ns.Breakdown and ns.Breakdown.L
    local breakdownBtn = coreUI:CreateButton(txCard, {
        label   = (BreakdownL and BreakdownL["BREAKDOWN_BUTTON"]) or "Summary",
        width   = 80, height = 20,
        onClick = function()
            local Bd = ns.Breakdown and ns.Breakdown.Frame
            if Bd and Bd.Toggle then Bd:Toggle() end
        end,
    })
    breakdownBtn:SetPoint("TOPRIGHT", txCard, "TOPRIGHT", -8, -4)

    -- Filter buttons
    f.filterButtons = {}
    local FILTER_SOURCES = {
        { key = "all",     localeKey = "FILTER_ALL" },
        { key = "vendor",  localeKey = "SRC_VENDOR" },
        { key = "repair",  localeKey = "SRC_REPAIR" },
        { key = "ah",      localeKey = "SRC_AH" },
        { key = "mail",    localeKey = "SRC_MAIL" },
        { key = "quest",   localeKey = "SRC_QUEST" },
        { key = "loot",    localeKey = "SRC_LOOT" },
        { key = "trade",   localeKey = "SRC_TRADE" },
        { key = "unknown", localeKey = "SRC_UNKNOWN" },
    }

    -- Build filter options with localized labels and source colors
    local sc = H.GetSourceColors()
    local filterOptions = {}
    for _, info in ipairs(FILTER_SOURCES) do
        filterOptions[#filterOptions + 1] = {
            key   = info.key,
            label = L[info.localeKey],
            color = sc[info.key] or { 1, 1, 1 },
        }
    end

    local filterY = -22
    local filterBar = coreUI:CreateFilterBar(txCard, {
        options  = filterOptions,
        initial  = activeFilter,
        x        = 10,
        y        = filterY,
        onSelect = function(key)
            activeFilter = key
            UpdateEntryRows()
        end,
    })
    -- Re-anchor buttons onto txCard (CreateFilterBar uses parent TOPLEFT; here parent is txCard)
    f.filterButtons = filterBar.buttons
    f.filterBar = filterBar

    local scrollY = filterY - 22

    local scrollFrame = coreUI:CreateScrollFrame(txCard, {
        childWidth = FRAME_WIDTH - 60,
        bgColor    = "SCROLL_BG",
    })
    scrollFrame:SetPoint("TOPLEFT", txCard, "TOPLEFT", 4, scrollY)
    scrollFrame:SetPoint("BOTTOMRIGHT", txCard, "BOTTOMRIGHT", -24, 4)

    f.scrollChild = scrollFrame.child
    f.entryRows = {}

    f.noDataLabel = coreUI:CreateLabel(f.scrollChild, {
        text = L["NO_DATA"], font = "GameFontDisable",
    })
    f.noDataLabel:SetPoint("TOP", f.scrollChild, "TOP", 0, -20)

    ---------------------------------------------------------------------------
    -- Animations on show
    ---------------------------------------------------------------------------
    f:SetScript("OnShow", function(self)
        -- fade in whole frame
        Anim.FadeIn(self, 0.2)

        -- cascade fade-in cards. Без сдвига: WoW выравнивает текст в
        -- движущемся фрейме по пикселям, и заголовки карточек дрожат.
        if self._cards then
            for i, card in ipairs(self._cards) do
                Anim.FadeIn(card, 0.3, 0.05 * i)
            end
        end

        -- chart bars will animate via UpdateChart flag
        self._animateBars = true
    end)

    mainFrame = f
    f:Hide()
    return f
end

-------------------------------------------------------------------------------
-- Transaction rows
-------------------------------------------------------------------------------
local function CreateEntryRow(parent, index)
    local coreUI = GoldLedger:GetModule("UI")
    local row = coreUI:CreateListRow(parent, {
        index  = index,
        height = ROW_HEIGHT,
        altBg  = "ROW_ALT",
    })

    row.timeText = coreUI:CreateLabel(row, { width = 42, justify = "LEFT", color = "TEXT_TIME" })
    row.timeText:SetPoint("LEFT", row, "LEFT", 4, 0)

    row.sourceTag = coreUI:CreateLabel(row, { width = 70, justify = "LEFT" })
    row.sourceTag:SetPoint("LEFT", row.timeText, "RIGHT", 4, 0)

    row.amountText = coreUI:CreateLabel(row, { justify = "RIGHT" })
    row.amountText:SetPoint("RIGHT", row, "RIGHT", -4, 0)

    function row:SetEntry(entry)
        self.timeText:SetText(date(L["TIME_FORMAT"], entry.timestamp))
        self.amountText:SetText(H.GoldFormatter.Colored(entry.amount, entry.type))

        local source = entry.source or "unknown"
        local Tracker = GoldLedger:GetModule("Tracker")
        local localeKey = Tracker and Tracker:GetSourceLocaleKey(source) or "SRC_UNKNOWN"
        local sc = H.GetSourceColors()
        local color = sc[source] or sc.unknown
        self.sourceTag:SetText(L[localeKey])
        self.sourceTag:SetTextColor(color[1], color[2], color[3])
    end

    return row
end

UpdateEntryRows = function()
    if not mainFrame then return end

    local Data = GoldLedger:GetModule("Data")
    if not Data then return end

    local allEntries = Data:GetRecentEntries(MAX_VISIBLE)
    local scrollChild = mainFrame.scrollChild

    local entries = {}
    if activeFilter == "all" then
        entries = allEntries
    else
        for _, entry in ipairs(allEntries) do
            if (entry.source or "unknown") == activeFilter then
                entries[#entries + 1] = entry
            end
        end
    end

    mainFrame.noDataLabel:SetShown(#entries == 0)

    for i, entry in ipairs(entries) do
        local row = mainFrame.entryRows[i]
        if not row then
            row = CreateEntryRow(scrollChild, i)
            mainFrame.entryRows[i] = row
        end
        row:SetEntry(entry)
        row:Show()
    end

    for i = #entries + 1, #mainFrame.entryRows do
        mainFrame.entryRows[i]:Hide()
    end

    scrollChild:SetHeight(math.max(1, #entries * ROW_HEIGHT))
end

-------------------------------------------------------------------------------
-- Chart update
-------------------------------------------------------------------------------
UpdateChart = function()
    if not mainFrame or not mainFrame:IsShown() then return end

    local Data = GoldLedger:GetModule("Data")
    if not Data then return end
    local TC, T = H.TC, H.T

    local chartData, maxVal, totalBars = Data:GetChartData(activeChartPeriod)
    local container = mainFrame.chartContainer
    local chartWidth = container:GetWidth()

    if chartWidth < 10 then return end

    local barGroupWidth = chartWidth / totalBars
    local barWidth = math.max(2, (barGroupWidth - 2) / 2)
    local todayKey = date("%Y-%m-%d")

    mainFrame.chartMaxLabel:SetText(H.GoldFormatter.Short(maxVal))

    local labelStep = math.max(1, math.ceil(totalBars / 6))

    for _, lbl in pairs(mainFrame.chartDayLabels) do
        lbl:Hide()
    end

    for i, dayData in ipairs(chartData) do
        if not mainFrame.chartBars[i] then
            local incBar = container:CreateTexture(nil, "ARTWORK")
            local expBar = container:CreateTexture(nil, "ARTWORK")
            mainFrame.chartBars[i] = { income = incBar, expense = expBar }
        end

        if not mainFrame.chartDayLabels[i] then
            local coreUI = GoldLedger:GetModule("UI")
            local dayLabel = coreUI:CreateLabel(container, { color = "DAY_LABEL" })
            mainFrame.chartDayLabels[i] = dayLabel
        end

        local bars = mainFrame.chartBars[i]
        local xOffset = (i - 1) * barGroupWidth

        local incHeight = dayData.income > 0 and math.max(2, (dayData.income / maxVal) * CHART_HEIGHT) or 0
        bars.income:ClearAllPoints()
        bars.income:SetPoint("BOTTOMLEFT", container, "BOTTOMLEFT", xOffset + 1, 0)
        bars.income:SetShown(dayData.income > 0)

        local expHeight = dayData.expense > 0 and math.max(2, (dayData.expense / maxVal) * CHART_HEIGHT) or 0
        bars.expense:ClearAllPoints()
        bars.expense:SetPoint("BOTTOMLEFT", container, "BOTTOMLEFT", xOffset + 1 + barWidth, 0)
        bars.expense:SetShown(dayData.expense > 0)

        -- animate bars growing or set instantly
        if mainFrame._animateBars and Anim then
            local barDelay = 0.02 * i
            if incHeight > 0 then
                bars.income:SetSize(barWidth, 0.1)
                Anim.GrowBar(bars.income, incHeight, 0.4, barDelay)
            else
                bars.income:SetSize(barWidth, incHeight)
            end
            if expHeight > 0 then
                bars.expense:SetSize(barWidth, 0.1)
                Anim.GrowBar(bars.expense, expHeight, 0.4, barDelay)
            else
                bars.expense:SetSize(barWidth, expHeight)
            end
        else
            bars.income:SetSize(barWidth, incHeight)
            bars.expense:SetSize(barWidth, expHeight)
        end

        local isToday = dayData.dateKey == todayKey
        local alpha = isToday and 1 or 0.5
        local inc = T("INCOME")
        local exp = T("EXPENSE")
        bars.income:SetColorTexture(inc[1], inc[2], inc[3], alpha)
        bars.expense:SetColorTexture(exp[1], exp[2], exp[3], alpha)

        local lbl = mainFrame.chartDayLabels[i]
        if i == 1 or i % labelStep == 0 then
            lbl:ClearAllPoints()
            lbl:SetPoint("TOP", container, "BOTTOMLEFT", xOffset + barGroupWidth / 2, -1)
            lbl:SetText(dayData.label)
            lbl:Show()
        else
            lbl:Hide()
        end

        if not mainFrame.chartHitFrames[i] then
            local hit = CreateFrame("Frame", nil, container)
            hit:EnableMouse(true)
            hit:SetScript("OnEnter", function(self)
                GameTooltip:SetOwner(self, "ANCHOR_TOP")
                GameTooltip:AddLine(self.tipDate or "", 1, 1, 1)
                if self.tipIncome and self.tipIncome > 0 then
                    GameTooltip:AddLine(L["HEADER_INCOME"] .. ": " .. H.GoldFormatter.Full(self.tipIncome), TC("INCOME"))
                end
                if self.tipExpense and self.tipExpense > 0 then
                    GameTooltip:AddLine(L["HEADER_EXPENSE"] .. ": " .. H.GoldFormatter.Full(self.tipExpense), TC("EXPENSE"))
                end
                GameTooltip:Show()
            end)
            hit:SetScript("OnLeave", function() GameTooltip:Hide() end)
            mainFrame.chartHitFrames[i] = hit
        end

        local hit = mainFrame.chartHitFrames[i]
        hit:ClearAllPoints()
        hit:SetPoint("BOTTOMLEFT", container, "BOTTOMLEFT", xOffset, 0)
        hit:SetSize(barGroupWidth, CHART_HEIGHT)
        hit.tipDate = dayData.dateKey or dayData.label
        hit.tipIncome = dayData.income
        hit.tipExpense = dayData.expense
        hit:Show()
    end

    for i = #chartData + 1, #mainFrame.chartBars do
        mainFrame.chartBars[i].income:Hide()
        mainFrame.chartBars[i].expense:Hide()
    end
    for i = #chartData + 1, #mainFrame.chartHitFrames do
        mainFrame.chartHitFrames[i]:Hide()
    end

    mainFrame._animateBars = nil
end

-------------------------------------------------------------------------------
-- Goal update
-------------------------------------------------------------------------------
local function UpdateGoal()
    if not mainFrame or not mainFrame:IsShown() then return end

    local Data = GoldLedger:GetModule("Data")
    if not Data then return end
    local TC = H.TC

    local goalInfo = Data:GetGoalProgress()

    if not goalInfo then
        mainFrame.goalBarFill:SetWidth(1)
        mainFrame.goalBarFill:Hide()
        mainFrame.goalText:SetText(L["GOAL_NONE"])
        mainFrame.goalText:SetTextColor(TC("TEXT_DIM"))
        mainFrame.goalEta:SetText("")
        if mainFrame.goalCtaBtn and mainFrame.goalCtaBtn.SetText then
            mainFrame.goalCtaBtn:SetText(L["GOAL_SET_BUTTON"])
        end
        return
    end

    if mainFrame.goalCtaBtn and mainFrame.goalCtaBtn.SetText then
        mainFrame.goalCtaBtn:SetText(L["GOAL_CHANGE_BUTTON"])
    end

    local barWidth = mainFrame.goalBarBg:GetWidth()
    if barWidth < 1 then barWidth = 1 end
    local fillWidth = math.max(1, barWidth * goalInfo.progress)

    mainFrame.goalBarFill:SetWidth(fillWidth)
    mainFrame.goalBarFill:Show()

    if goalInfo.progress >= 1 then
        mainFrame.goalBarFill:SetColorTexture(TC("GOAL_DONE"))
        mainFrame.goalText:SetText(L["GOAL_REACHED"])
        mainFrame.goalText:SetTextColor(TC("GOAL_DONE"))
        mainFrame.goalEta:SetText("")
    else
        mainFrame.goalBarFill:SetColorTexture(TC("GOAL_FILL"))
        local pct = math.floor(goalInfo.progress * 100)
        mainFrame.goalText:SetText(
            H.GoldFormatter.Short(goalInfo.current) .. " / " ..
            H.GoldFormatter.Short(goalInfo.goal) .. " (" .. pct .. "%)")
        mainFrame.goalText:SetTextColor(TC("TEXT_WHITE"))

        if goalInfo.estDays then
            -- Cap astronomical ETAs (tiny daily gain vs huge goal would
            -- otherwise show ~14M+ days). Anything past 4 digits is
            -- "практически никогда" and just noisy in the UI.
            if goalInfo.estDays > 9999 then
                mainFrame.goalEta:SetText(L["GOAL_TOO_FAR"])
            else
                mainFrame.goalEta:SetText(L["GOAL_REMAINING"]:format(goalInfo.estDays))
            end
        else
            mainFrame.goalEta:SetText("")
        end
    end
end

-------------------------------------------------------------------------------
-- Summary update
-------------------------------------------------------------------------------
local function UpdateSummaries()
    if not mainFrame or not mainFrame:IsShown() then return end

    local Data = GoldLedger:GetModule("Data")
    if not Data then return end
    local TC, T = H.TC, H.T

    local currentGold = GetMoney() or 0
    mainFrame.onHandValue:SetText(H.GoldFormatter.Full(currentGold))

    local today = Data:GetDailySummary()
    mainFrame.todayIncome:SetValue(H.GoldFormatter.Abbrev(today.income), TC("INCOME"))
    mainFrame.todayExpense:SetValue(H.GoldFormatter.Abbrev(today.expense), TC("EXPENSE"))

    local todayNet = today.income - today.expense
    local tc = todayNet >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
    mainFrame.todayBalance:SetValue(
        (todayNet >= 0 and "+" or "-") .. H.GoldFormatter.Abbrev(math.abs(todayNet)),
        tc[1], tc[2], tc[3])

    local yesterday = Data:GetDailySummary(Data:GetYesterdayDateKey())
    mainFrame.yesterdayIncome:SetValue(H.GoldFormatter.Abbrev(yesterday.income), TC("INCOME"))
    mainFrame.yesterdayExpense:SetValue(H.GoldFormatter.Abbrev(yesterday.expense), TC("EXPENSE"))

    local yNet = yesterday.income - yesterday.expense
    local yc = yNet >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
    mainFrame.yesterdayBalance:SetValue(
        (yNet >= 0 and "+" or "-") .. H.GoldFormatter.Abbrev(math.abs(yNet)),
        yc[1], yc[2], yc[3])

    local month = Data:GetMonthlySummary()
    mainFrame.monthIncome:SetValue(H.GoldFormatter.Abbrev(month.income), TC("INCOME"))
    mainFrame.monthExpense:SetValue(H.GoldFormatter.Abbrev(month.expense), TC("EXPENSE"))

    local monthNet = month.income - month.expense
    local mc = monthNet >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
    mainFrame.monthBalance:SetValue(
        (monthNet >= 0 and "+" or "-") .. H.GoldFormatter.Abbrev(math.abs(monthNet)),
        mc[1], mc[2], mc[3])

    local TrackerMod = GoldLedger:GetModule("Tracker")
    if TrackerMod then
        local session = TrackerMod:GetSessionStats()
        mainFrame.sessionIncome:SetValue(H.GoldFormatter.Abbrev(session.income), TC("INCOME"))
        mainFrame.sessionExpense:SetValue(H.GoldFormatter.Abbrev(session.expense), TC("EXPENSE"))

        local sc = session.net >= 0 and T("BALANCE_POS") or T("BALANCE_NEG")
        mainFrame.sessionNet:SetValue(
            (session.net >= 0 and "+" or "-") .. H.GoldFormatter.Abbrev(math.abs(session.net)),
            sc[1], sc[2], sc[3])
    end

    UpdateGoal()
    UpdateChart()
    if mainFrame.filterBar and mainFrame.filterBar.SetActive then
        mainFrame.filterBar:SetActive(activeFilter)
    end
    UpdateEntryRows()
end

-------------------------------------------------------------------------------
-- Export
-------------------------------------------------------------------------------
ns.UI_MainFrame = {
    Create = CreateMainFrame,
    UpdateSummaries = UpdateSummaries,
    UpdateGoal = UpdateGoal,
    GetFrame = function() return mainFrame end,
    SetFrame = function(f) mainFrame = f end,
}
