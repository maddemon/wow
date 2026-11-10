--[[
    GoldLedger / Breakdown: Frame.lua
    Source breakdown popup: income/expense by source with period filter.
]]

local ADDON_NAME, ns = ...
ns.Breakdown = ns.Breakdown or {}

local Frame = {}
ns.Breakdown.Frame = Frame

local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.Breakdown.L end
local function H()  return ns.UI_Helpers end

local breakdownFrame
local activePeriod = "today"

local function TC(key)
    local helpers = H()
    if helpers and helpers.TC then return helpers.TC(key) end
    return 1,1,1,1
end

local function UpdateData()
    if not breakdownFrame or not breakdownFrame:IsShown() then return end
    local helpers = H()
    if not helpers then return end

    local Data = ns.GoldLedger and ns.GoldLedger:GetModule("Data")
    if not Data then return end
    local sourceTotals, grandTotals = Data:GetSourceBreakdown(activePeriod)

    for key, btn in pairs(breakdownFrame.periodBtns) do
        if btn.SetActive then btn:SetActive(key == activePeriod) end
    end

    for src, row in pairs(breakdownFrame.sourceRows) do
        local data = sourceTotals[src] or { income = 0, expense = 0 }

        if data.income > 0 then
            row.income:SetText("+" .. helpers.GoldFormatter.Short(data.income))
            row.income:SetTextColor(TC("INCOME"))
        else
            row.income:SetText("0")
            row.income:SetTextColor(TC("ZERO_VALUE"))
        end

        if data.expense > 0 then
            row.expense:SetText("-" .. helpers.GoldFormatter.Short(data.expense))
            row.expense:SetTextColor(TC("EXPENSE"))
        else
            row.expense:SetText("0")
            row.expense:SetTextColor(TC("ZERO_VALUE"))
        end
    end

    if grandTotals.income > 0 then
        breakdownFrame.totalIncome:SetText("+" .. helpers.GoldFormatter.Short(grandTotals.income))
        breakdownFrame.totalIncome:SetTextColor(TC("INCOME"))
    else
        breakdownFrame.totalIncome:SetText("0")
        breakdownFrame.totalIncome:SetTextColor(TC("ZERO_VALUE"))
    end

    if grandTotals.expense > 0 then
        breakdownFrame.totalExpense:SetText("-" .. helpers.GoldFormatter.Short(grandTotals.expense))
        breakdownFrame.totalExpense:SetTextColor(TC("EXPENSE"))
    else
        breakdownFrame.totalExpense:SetText("0")
        breakdownFrame.totalExpense:SetTextColor(TC("ZERO_VALUE"))
    end
end

function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    if not ui then return end

    local GL = ns.GoldLedger
    local Data = GL:GetModule("Data")
    local Tracker = GL:GetModule("Tracker")
    local coreL = ns.L

    local f = ui:CreatePopup({
        name  = "GoldLedgerBreakdownFrame",
        title = l["BREAKDOWN_TITLE"],
        width = 380, height = 380,
    })

    -- Period buttons
    local periods = {
        { key = "today", label = l["BREAKDOWN_TODAY"] },
        { key = "week",  label = l["BREAKDOWN_WEEK"]  },
        { key = "month", label = l["BREAKDOWN_MONTH"] },
        { key = "all",   label = l["BREAKDOWN_ALL"]   },
    }

    local periodBtns = {}
    local btnX = -14
    for i = #periods, 1, -1 do
        local info = periods[i]
        local btn = ui:CreateButton(f, {
            label = info.label, width = 60, height = 18,
            onClick = function()
                activePeriod = info.key
                UpdateData()
            end,
        })
        btn:SetPoint("TOPRIGHT", f, "TOPRIGHT", btnX, -36)
        btn.key = info.key
        periodBtns[info.key] = btn
        btnX = btnX - 64
    end
    f.periodBtns = periodBtns

    -- Column headers
    local colY = -60

    local srcHeader = ui:CreateLabel(f, { text = l["BREAKDOWN_SOURCE"], color = "LABEL" })
    srcHeader:SetPoint("TOPLEFT", f, "TOPLEFT", 18, colY)

    local incHeader = ui:CreateLabel(f, { text = (coreL and coreL["HEADER_INCOME"]) or "Income", color = "LABEL", justify = "RIGHT" })
    incHeader:SetPoint("TOPRIGHT", f, "TOP", 40, colY)

    local expHeader = ui:CreateLabel(f, { text = (coreL and coreL["HEADER_EXPENSE"]) or "Expense", color = "LABEL", justify = "RIGHT" })
    expHeader:SetPoint("TOPRIGHT", f, "TOPRIGHT", -18, colY)

    if helpers and helpers.MakeSeparator then helpers.MakeSeparator(f, colY - 14) end

    -- Source rows
    local BD_ROW_HEIGHT = 26
    local rowY = colY - 20
    f.sourceRows = {}
    local sc = helpers and helpers.GetSourceColors and helpers.GetSourceColors() or {}

    for idx, src in ipairs(Data.ALL_SOURCES or {}) do
        local color = sc[src] or sc.unknown or { 1, 1, 1 }
        local localeKey = Tracker and Tracker:GetSourceLocaleKey(src) or "SRC_UNKNOWN"

        local srcName = ui:CreateLabel(f, {
            text = coreL and coreL[localeKey] or src,
            color = { color[1], color[2], color[3] },
        })
        srcName:SetPoint("TOPLEFT", f, "TOPLEFT", 18, rowY - 4)

        local incVal = ui:CreateLabel(f, { justify = "RIGHT" })
        incVal:SetPoint("TOPRIGHT", f, "TOP", 40, rowY - 4)

        local expVal = ui:CreateLabel(f, { justify = "RIGHT" })
        expVal:SetPoint("TOPRIGHT", f, "TOPRIGHT", -18, rowY - 4)

        f.sourceRows[src] = { income = incVal, expense = expVal }
        rowY = rowY - BD_ROW_HEIGHT
    end

    if helpers and helpers.MakeSeparator then helpers.MakeSeparator(f, rowY + 2) end

    local totalLabel = ui:CreateLabel(f, { text = l["BREAKDOWN_TOTAL"], font = "GameFontNormal", color = "HEADER" })
    totalLabel:SetPoint("TOPLEFT", f, "TOPLEFT", 18, rowY - 8)

    f.totalIncome = ui:CreateLabel(f, { font = "GameFontNormal", justify = "RIGHT" })
    f.totalIncome:SetPoint("TOPRIGHT", f, "TOP", 40, rowY - 8)

    f.totalExpense = ui:CreateLabel(f, { font = "GameFontNormal", justify = "RIGHT" })
    f.totalExpense:SetPoint("TOPRIGHT", f, "TOPRIGHT", -18, rowY - 8)

    breakdownFrame = f
    return f
end

function Frame:Toggle()
    if not breakdownFrame then self:Create() end
    if breakdownFrame:IsShown() then
        breakdownFrame:Hide()
    else
        local helpers = H()
        if helpers and helpers.SnapToMain then helpers.SnapToMain(breakdownFrame) end
        breakdownFrame:Show()
        UpdateData()
    end
end

function Frame:Update() UpdateData() end
function Frame:GetFrame() return breakdownFrame end
function Frame:SetFrame(f) breakdownFrame = f end
