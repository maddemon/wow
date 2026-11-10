--[[
    GoldLedger / AuctionTracker: Frame.lua
    Auction History popup UI.
    Layout inspired by GoldPulse mockup:
      [Period ▼] [Type ▼] [Search______]
      Time | Type | Item | Qty | Price | Total
      ...
      Records: N  Income: X  Expense: Y  Net: Z
      [< Prev]              [Next >]
]]

local ADDON_NAME, ns = ...
ns.AuctionTracker = ns.AuctionTracker or {}

local Frame = {}
ns.AuctionTracker.Frame = Frame

local auctionFrame
local state = {
    page     = 1,
    perPage  = 50,
    period   = "all",
    ahType   = "all",
    search   = "",
}

local ROW_HEIGHT = 20

-------------------------------------------------------------------------------
-- Helpers (read from core public API — no coupling)
-------------------------------------------------------------------------------
local function H() return ns.UI_Helpers end
local function L() return ns.AuctionTracker.L end

local function tc(key)
    local helpers = H()
    if helpers and helpers.TC then return helpers.TC(key) end
    return 1, 1, 1, 1
end

local function formatGold(copper)
    local helpers = H()
    if helpers and helpers.GoldFormatter then
        return helpers.GoldFormatter.Short(copper or 0)
    end
    return tostring(math.floor((copper or 0) / 10000)) .. "g"
end

local function formatFull(copper, entryType)
    local helpers = H()
    if helpers and helpers.GoldFormatter then
        return helpers.GoldFormatter.Colored(copper or 0, entryType or "income")
    end
    return tostring(copper or 0)
end

-------------------------------------------------------------------------------
-- Transaction row
-------------------------------------------------------------------------------
local function CreateRow(parent, index)
    local coreUI = ns.GoldLedger and ns.GoldLedger:GetModule("UI")
    local row = coreUI:CreateListRow(parent, {
        index = index, height = ROW_HEIGHT, altBg = "ROW_ALT",
    })

    row.timeText = coreUI:CreateLabel(row, { width = 46, justify = "LEFT", color = "TEXT_TIME" })
    row.timeText:SetPoint("LEFT", row, "LEFT", 6, 0)

    row.typeText = coreUI:CreateLabel(row, { width = 80, justify = "LEFT" })
    row.typeText:SetPoint("LEFT", row.timeText, "RIGHT", 4, 0)

    row.itemText = coreUI:CreateLabel(row, { width = 240, justify = "LEFT", color = "TEXT_WHITE" })
    row.itemText:SetPoint("LEFT", row.typeText, "RIGHT", 4, 0)

    row.qtyText = coreUI:CreateLabel(row, { width = 40, justify = "RIGHT", color = "LABEL" })
    row.qtyText:SetPoint("LEFT", row.itemText, "RIGHT", 4, 0)

    row.priceText = coreUI:CreateLabel(row, { width = 70, justify = "RIGHT", color = "LABEL" })
    row.priceText:SetPoint("LEFT", row.qtyText, "RIGHT", 8, 0)

    row.totalText = coreUI:CreateLabel(row, { width = 80, justify = "RIGHT" })
    row.totalText:SetPoint("RIGHT", row, "RIGHT", -8, 0)

    function row:SetEntry(tx)
        local l = L()
        self.timeText:SetText(date("%H:%M", tx.timestamp))

        -- Type label + color
        local typeLabel, typeColor
        if tx.ahType == "sale" then
            typeLabel = l["AUCTION_TYPE_SALE"]
            typeColor = { tc("INCOME") }
        elseif tx.ahType == "purchase" then
            typeLabel = l["AUCTION_TYPE_PURCHASE"]
            typeColor = { tc("EXPENSE") }
        elseif tx.ahType == "cut" then
            typeLabel = l["AUCTION_TYPE_CUT"]
            typeColor = { tc("EXPENSE") }
        elseif tx.ahType == "deposit" then
            typeLabel = l["AUCTION_TYPE_DEPOSIT"]
            typeColor = { tc("LABEL") }
        else
            typeLabel = tx.ahType or "?"
            typeColor = { tc("TEXT_DIM") }
        end
        self.typeText:SetText(typeLabel)
        self.typeText:SetTextColor(typeColor[1] or 1, typeColor[2] or 1, typeColor[3] or 1)

        self.itemText:SetText(tx.itemName or l["AUCTION_UNKNOWN_ITEM"])
        self.qtyText:SetText(tx.quantity and tostring(tx.quantity) or "—")
        self.priceText:SetText(tx.unitPrice and formatGold(tx.unitPrice) or "—")

        -- Total: sales are income (green +), others are expense (red -)
        local entryType = (tx.ahType == "sale") and "income" or "expense"
        self.totalText:SetText(formatFull(tx.amount, entryType))
    end

    return row
end

-------------------------------------------------------------------------------
-- Main frame
-------------------------------------------------------------------------------
function Frame:Create()
    local helpers = H()
    local l = L()
    local coreUI = ns.GoldLedger and ns.GoldLedger:GetModule("UI")

    -- Standardized popup (title bar, close button, drag, backdrop, ESC-close, BringToFront on show)
    local f = coreUI:CreatePopup({
        name   = "GoldLedgerAuctionFrame",
        title  = l["AUCTION_TITLE"],
        width  = 720,
        height = 500,
    })

    -- Filter row
    local filterY = -40

    local periodLabel = coreUI:CreateLabel(f, {
        text = l["AUCTION_PERIOD"], color = "LABEL",
        anchor = { point = "TOPLEFT", x = 12, y = filterY - 2 },
    })

    local periodDD = coreUI:CreateDropdown(f, 110, {
        { value = "today", label = l["AUCTION_PERIOD_TODAY"] },
        { value = "week",  label = l["AUCTION_PERIOD_WEEK"]  },
        { value = "month", label = l["AUCTION_PERIOD_MONTH"] },
        { value = "all",   label = l["AUCTION_PERIOD_ALL"]   },
    }, function(value)
        state.period = value
        state.page = 1
        Frame:Update()
    end, state.period)
    periodDD:SetPoint("LEFT", periodLabel, "RIGHT", 8, 0)

    local typeLabel = coreUI:CreateLabel(f, { text = l["AUCTION_TYPE"], color = "LABEL" })
    typeLabel:SetPoint("LEFT", periodDD, "RIGHT", 16, 0)

    local typeDD = coreUI:CreateDropdown(f, 120, {
        { value = "all",      label = l["AUCTION_TYPE_ALL"]      },
        { value = "sale",     label = l["AUCTION_TYPE_SALE"]     },
        { value = "purchase", label = l["AUCTION_TYPE_PURCHASE"] },
        { value = "cut",      label = l["AUCTION_TYPE_CUT"]      },
        { value = "deposit",  label = l["AUCTION_TYPE_DEPOSIT"]  },
    }, function(value)
        state.ahType = value
        state.page = 1
        Frame:Update()
    end, state.ahType)
    typeDD:SetPoint("LEFT", typeLabel, "RIGHT", 6, 0)

    local searchLabel = coreUI:CreateLabel(f, { text = l["AUCTION_SEARCH"], color = "LABEL" })
    searchLabel:SetPoint("LEFT", typeDD, "RIGHT", 16, 0)

    local searchEdit = coreUI:CreateEditBox(f, {
        width = 170, height = 20, maxLetters = 64,
        onChange = function(self, text)
            state.search = text
            state.page = 1
            Frame:Update()
        end,
    })
    searchEdit:SetPoint("LEFT", searchLabel, "RIGHT", 8, 0)
    f.searchEdit = searchEdit

    -- Column headers
    local headerY = filterY - 32

    local hTime = coreUI:CreateLabel(f, {
        text = l["AUCTION_COL_TIME"], color = "LABEL",
        anchor = { point = "TOPLEFT", x = 18, y = headerY },
    })

    local hType = coreUI:CreateLabel(f, { text = l["AUCTION_COL_TYPE"], color = "LABEL" })
    hType:SetPoint("LEFT", hTime, "LEFT", 52, 0)

    local hItem = coreUI:CreateLabel(f, { text = l["AUCTION_COL_ITEM"], color = "LABEL" })
    hItem:SetPoint("LEFT", hType, "LEFT", 85, 0)

    local hQty = coreUI:CreateLabel(f, { text = l["AUCTION_COL_QTY"], color = "LABEL" })
    hQty:SetPoint("LEFT", hItem, "LEFT", 245, 0)

    local hPrice = coreUI:CreateLabel(f, { text = l["AUCTION_COL_PRICE"], color = "LABEL" })
    hPrice:SetPoint("LEFT", hQty, "LEFT", 45, 0)

    local hTotal = coreUI:CreateLabel(f, { text = l["AUCTION_COL_TOTAL"], color = "LABEL" })
    hTotal:SetPoint("TOPRIGHT", f, "TOPRIGHT", -30, headerY)

    -- Scroll area
    local scrollFrame = coreUI:CreateScrollFrame(f, {
        childWidth = 680,
        bgColor = "SCROLL_BG",
    })
    scrollFrame:SetPoint("TOPLEFT", f, "TOPLEFT", 8, headerY - 16)
    scrollFrame:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -28, 60)

    f.scrollChild = scrollFrame.child
    f.entryRows = {}
    local scrollChild = f.scrollChild

    f.noDataLabel = coreUI:CreateLabel(scrollChild, { text = l["AUCTION_NO_RESULTS"], font = "GameFontDisable" })
    f.noDataLabel:SetPoint("TOP", scrollChild, "TOP", 0, -20)

    -- Status bar
    f.statusRecords = coreUI:CreateLabel(f, { color = "LABEL" })
    f.statusRecords:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 14, 36)

    f.statusIncome = coreUI:CreateLabel(f, {})
    f.statusIncome:SetPoint("LEFT", f.statusRecords, "RIGHT", 16, 0)

    f.statusExpense = coreUI:CreateLabel(f, {})
    f.statusExpense:SetPoint("LEFT", f.statusIncome, "RIGHT", 16, 0)

    f.statusNet = coreUI:CreateLabel(f, {})
    f.statusNet:SetPoint("LEFT", f.statusExpense, "RIGHT", 16, 0)

    -- Pagination
    f.prevBtn = coreUI:CreateButton(f, {
        label = l["AUCTION_PREV"], width = 64, height = 22,
        onClick = function()
            if state.page > 1 then
                state.page = state.page - 1
                Frame:Update()
            end
        end,
    })
    f.prevBtn:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 10, 10)

    f.nextBtn = coreUI:CreateButton(f, {
        label = l["AUCTION_NEXT"], width = 64, height = 22,
        onClick = function()
            if state.page < (f.totalPages or 1) then
                state.page = state.page + 1
                Frame:Update()
            end
        end,
    })
    f.nextBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -10, 10)

    f.pageLabel = coreUI:CreateLabel(f, { color = "LABEL" })
    f.pageLabel:SetPoint("BOTTOM", f, "BOTTOM", 0, 15)

    auctionFrame = f
    f:Hide()
    return f
end

-------------------------------------------------------------------------------
-- Update (query Storage and refresh rows)
-------------------------------------------------------------------------------
function Frame:Update()
    if not auctionFrame or not auctionFrame:IsShown() then return end
    local Query = ns.AuctionTracker.Query
    if not Query then return end
    local l = L()

    local result = Query:Run({
        page    = state.page,
        perPage = state.perPage,
        period  = state.period,
        ahType  = state.ahType,
        search  = state.search,
    })

    auctionFrame.totalPages = result.totalPages
    auctionFrame.noDataLabel:SetShown(#result.entries == 0)

    for i, tx in ipairs(result.entries) do
        local row = auctionFrame.entryRows[i]
        if not row then
            row = CreateRow(auctionFrame.scrollChild, i)
            auctionFrame.entryRows[i] = row
        end
        row:SetEntry(tx)
        row:Show()
    end
    for i = #result.entries + 1, #auctionFrame.entryRows do
        auctionFrame.entryRows[i]:Hide()
    end
    auctionFrame.scrollChild:SetHeight(math.max(1, #result.entries * ROW_HEIGHT))

    -- Status bar
    auctionFrame.statusRecords:SetText(l["AUCTION_STATUS_RECORDS"]:format(result.recordCount))
    auctionFrame.statusIncome:SetText(l["AUCTION_STATUS_INCOME"]:format(formatGold(result.totalIncome)))
    local r, g, b = tc("INCOME")
    auctionFrame.statusIncome:SetTextColor(r, g, b)
    auctionFrame.statusExpense:SetText(l["AUCTION_STATUS_EXPENSE"]:format(formatGold(result.totalExpense)))
    r, g, b = tc("EXPENSE")
    auctionFrame.statusExpense:SetTextColor(r, g, b)
    auctionFrame.statusNet:SetText(l["AUCTION_STATUS_NET"]:format(formatGold(result.net)))
    if result.net >= 0 then
        r, g, b = tc("BALANCE_POS")
    else
        r, g, b = tc("BALANCE_NEG")
    end
    auctionFrame.statusNet:SetTextColor(r, g, b)

    -- Pagination labels
    if result.totalPages > 0 then
        auctionFrame.pageLabel:SetText(l["AUCTION_PAGE"]:format(result.page, result.totalPages))
    else
        auctionFrame.pageLabel:SetText("")
    end
    auctionFrame.prevBtn:SetAlpha(state.page > 1 and 1 or 0.4)
    auctionFrame.nextBtn:SetAlpha(state.page < result.totalPages and 1 or 0.4)
end

-------------------------------------------------------------------------------
-- Public
-------------------------------------------------------------------------------
function Frame:Toggle()
    if not auctionFrame then
        self:Create()
    end
    if auctionFrame:IsShown() then
        auctionFrame:Hide()
    else
        -- Reset state to defaults on fresh open (keeps UX predictable)
        state.page = 1
        -- Snap to main frame (same pattern as all other popups)
        local helpers = H()
        if helpers and helpers.SnapToMain then
            helpers.SnapToMain(auctionFrame)
        end
        auctionFrame:Show()
        self:Update()
    end
end

function Frame:GetFrame()
    return auctionFrame
end

function Frame:SetFrame(f)
    auctionFrame = f
end
