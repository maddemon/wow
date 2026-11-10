--[[
    GoldLedger / AuctionTracker: Locale.lua
    Self-contained localization for this module.
    Does not touch ns.L (core locale).
]]

local ADDON_NAME, ns = ...
ns.AuctionTracker = ns.AuctionTracker or {}

local enUS = {
    AUCTION_BUTTON          = "Auction",
    AUCTION_TITLE           = "GoldLedger — Auction History",

    -- Filter labels
    AUCTION_PERIOD          = "Period:",
    AUCTION_TYPE            = "Type:",
    AUCTION_SEARCH          = "Search:",

    -- Period options
    AUCTION_PERIOD_TODAY    = "Today",
    AUCTION_PERIOD_WEEK     = "Week",
    AUCTION_PERIOD_MONTH    = "Month",
    AUCTION_PERIOD_ALL      = "All",

    -- Type options
    AUCTION_TYPE_ALL        = "All",
    AUCTION_TYPE_SALE       = "Sale",
    AUCTION_TYPE_PURCHASE   = "Purchase",
    AUCTION_TYPE_CUT        = "AH Cut",
    AUCTION_TYPE_DEPOSIT    = "Deposit",

    -- Columns
    AUCTION_COL_TIME        = "Time",
    AUCTION_COL_TYPE        = "Type",
    AUCTION_COL_ITEM        = "Item",
    AUCTION_COL_QTY         = "Qty",
    AUCTION_COL_PRICE       = "Price",
    AUCTION_COL_TOTAL       = "Total",

    -- Status bar
    AUCTION_STATUS_RECORDS  = "Records: %d",
    AUCTION_STATUS_INCOME   = "Income: %s",
    AUCTION_STATUS_EXPENSE  = "Expense: %s",
    AUCTION_STATUS_NET      = "Net: %s",

    -- Misc
    AUCTION_NO_RESULTS      = "No auction transactions match filters.",
    AUCTION_UNKNOWN_ITEM    = "?",
    AUCTION_PAGE            = "Page %d/%d",
    AUCTION_SHOWING         = "Showing %d-%d of %d",
    AUCTION_PREV            = "< Prev",
    AUCTION_NEXT            = "Next >",
}

local zhCN = {
    AUCTION_BUTTON          = "拍卖行",
    AUCTION_TITLE           = "GoldLedger — 拍卖行流水",

    AUCTION_PERIOD          = "时间段：",
    AUCTION_TYPE            = "类型：",
    AUCTION_SEARCH          = "搜索：",

    AUCTION_PERIOD_TODAY    = "今天",
    AUCTION_PERIOD_WEEK     = "本周",
    AUCTION_PERIOD_MONTH    = "本月",
    AUCTION_PERIOD_ALL      = "全部",

    AUCTION_TYPE_ALL        = "全部",
    AUCTION_TYPE_SALE       = "卖出",
    AUCTION_TYPE_PURCHASE   = "买入",
    AUCTION_TYPE_CUT        = "拍卖行手续费",
    AUCTION_TYPE_DEPOSIT    = "押金",

    AUCTION_COL_TIME        = "时间",
    AUCTION_COL_TYPE        = "类型",
    AUCTION_COL_ITEM        = "物品",
    AUCTION_COL_QTY         = "数量",
    AUCTION_COL_PRICE       = "单价",
    AUCTION_COL_TOTAL       = "合计",

    AUCTION_STATUS_RECORDS  = "记录数：%d",
    AUCTION_STATUS_INCOME   = "收入：%s",
    AUCTION_STATUS_EXPENSE  = "支出：%s",
    AUCTION_STATUS_NET      = "净额：%s",

    AUCTION_NO_RESULTS      = "没有符合筛选条件的拍卖记录。",
    AUCTION_UNKNOWN_ITEM    = "?",
    AUCTION_PAGE            = "第 %d/%d 页",
    AUCTION_SHOWING         = "显示第 %d-%d 条，共 %d 条",
    AUCTION_PREV            = "上一页",
    AUCTION_NEXT            = "下一页",
}

local L = {}
local function getLocale()
    -- Respect core language setting (ns.L:GetLocale()), not system GetLocale()
    if ns.L and ns.L.GetLocale then
        return ns.L:GetLocale()
    end
    return GetLocale and GetLocale() == "zhCN" and "zhCN" or "enUS"
end

setmetatable(L, {
    __index = function(_, key)
        if getLocale() == "zhCN" then
            return zhCN[key] or enUS[key] or key
        end
        return enUS[key] or key
    end
})

ns.AuctionTracker.L = L
