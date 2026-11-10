--[[
    GoldLedger / AuctionTracker: Query.lua

    Read-only query layer over core Data. NO own SavedVariables.
    - Pulls entries from GoldLedgerDB via Data:GetRecentEntries()
    - Filters: source=="ah", ahType set, period, ahType filter, search
    - Synthesizes virtual "cut" entries from sales with cutAmount > 0
    - Returns paginated result + totals (Income / Expense / Net)

    Plain "ah" entries (without ahType) are hidden — they appear in Recent Transactions
    in the main frame, but not in the Auction popup.
]]

local ADDON_NAME, ns = ...
ns.AuctionTracker = ns.AuctionTracker or {}

local Query = {}
ns.AuctionTracker.Query = Query

local SECONDS_PER_DAY = 86400
local QUERY_LIMIT     = 5000  -- Max entries pulled from core Data

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------
local function periodCutoff(period)
    if period == "today" then
        local d = date("*t")
        d.hour, d.min, d.sec = 0, 0, 0
        return time(d)
    elseif period == "week" then
        return time() - 7 * SECONDS_PER_DAY
    elseif period == "month" then
        return time() - 30 * SECONDS_PER_DAY
    end
    return 0  -- "all"
end

local function makeVirtualCut(saleEntry)
    return {
        timestamp = saleEntry.timestamp,
        amount    = saleEntry.cutAmount or 0,
        type      = "expense",
        source    = "ah",
        ahType    = "cut",
        itemName  = saleEntry.itemName,
        _virtual  = true,
    }
end

--- Run a query against core Data, with AH-specific filters and aggregates.
--- @param options table {
---   page=1, perPage=50,
---   period="all"|"today"|"week"|"month",
---   ahType="all"|"sale"|"purchase"|"cut"|"deposit",
---   search="" (case-insensitive substring on itemName)
--- }
function Query:Run(options)
    options = options or {}
    local page    = options.page or 1
    local perPage = options.perPage or 50
    local period  = options.period or "all"
    local ahType  = options.ahType or "all"
    local search  = (options.search or ""):lower()

    local empty = {
        entries = {}, total = 0, page = page, totalPages = 0,
        totalIncome = 0, totalExpense = 0, net = 0, recordCount = 0,
    }

    local GL = ns.GoldLedger
    local Data = GL and GL:GetModule("Data")
    if not Data or not Data.GetRecentEntries then return empty end

    local all = Data:GetRecentEntries(QUERY_LIMIT)  -- newest first
    local cutoff = periodCutoff(period)

    -- Pass 1: collect AH entries with ahType + synthesize virtual cuts
    local raw = {}
    for _, entry in ipairs(all) do
        if entry.source == "ah" and entry.ahType then
            -- Real entry
            if cutoff == 0 or entry.timestamp >= cutoff then
                raw[#raw + 1] = entry
            end
            -- Synthesize virtual cut for sales with cutAmount
            if entry.ahType == "sale" and (entry.cutAmount or 0) > 0 then
                if cutoff == 0 or entry.timestamp >= cutoff then
                    raw[#raw + 1] = makeVirtualCut(entry)
                end
            end
        end
    end

    -- Pass 2: apply ahType + search filters
    local filtered = {}
    for _, entry in ipairs(raw) do
        local match = true

        if ahType ~= "all" and entry.ahType ~= ahType then
            match = false
        end
        if search ~= "" then
            if not entry.itemName then
                match = false
            elseif not entry.itemName:lower():find(search, 1, true) then
                match = false
            end
        end

        if match then
            filtered[#filtered + 1] = entry
        end
    end

    -- Aggregates: sale → income (gross), cut/purchase/deposit → expense
    local totalIncome, totalExpense = 0, 0
    for _, e in ipairs(filtered) do
        if e.ahType == "sale" then
            -- Use grossAmount if present, else amount
            totalIncome = totalIncome + (e.grossAmount or e.amount or 0)
        else
            totalExpense = totalExpense + (e.amount or 0)
        end
    end

    local total = #filtered
    local totalPages = perPage > 0 and math.ceil(total / perPage) or 0

    local startIdx = (page - 1) * perPage + 1
    local endIdx = math.min(page * perPage, total)
    local entries = {}
    if startIdx <= total then
        for i = startIdx, endIdx do
            entries[#entries + 1] = filtered[i]
        end
    end

    return {
        entries      = entries,
        total        = total,
        page         = page,
        totalPages   = totalPages,
        totalIncome  = totalIncome,
        totalExpense = totalExpense,
        net          = totalIncome - totalExpense,
        recordCount  = total,
    }
end
