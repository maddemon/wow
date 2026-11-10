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

local ruRU = {
    AUCTION_BUTTON          = "Аукцион",
    AUCTION_TITLE           = "GoldLedger — История аукциона",

    AUCTION_PERIOD          = "Период:",
    AUCTION_TYPE            = "Тип:",
    AUCTION_SEARCH          = "Поиск:",

    AUCTION_PERIOD_TODAY    = "Сегодня",
    AUCTION_PERIOD_WEEK     = "Неделя",
    AUCTION_PERIOD_MONTH    = "Месяц",
    AUCTION_PERIOD_ALL      = "Всё",

    AUCTION_TYPE_ALL        = "Все",
    AUCTION_TYPE_SALE       = "Продажа",
    AUCTION_TYPE_PURCHASE   = "Покупка",
    AUCTION_TYPE_CUT        = "AH Cut",
    AUCTION_TYPE_DEPOSIT    = "Депозит",

    AUCTION_COL_TIME        = "Время",
    AUCTION_COL_TYPE        = "Тип",
    AUCTION_COL_ITEM        = "Предмет",
    AUCTION_COL_QTY         = "Кол",
    AUCTION_COL_PRICE       = "Цена",
    AUCTION_COL_TOTAL       = "Итог",

    AUCTION_STATUS_RECORDS  = "Записей: %d",
    AUCTION_STATUS_INCOME   = "Доход: %s",
    AUCTION_STATUS_EXPENSE  = "Расход: %s",
    AUCTION_STATUS_NET      = "Net: %s",

    AUCTION_NO_RESULTS      = "Нет операций по фильтру.",
    AUCTION_UNKNOWN_ITEM    = "?",
    AUCTION_PAGE            = "Стр. %d/%d",
    AUCTION_SHOWING         = "Показано %d-%d из %d",
    AUCTION_PREV            = "< Назад",
    AUCTION_NEXT            = "Далее >",
}

local L = {}
local function getLocale()
    -- Respect core language setting (ns.L:GetLocale()), not system GetLocale()
    if ns.L and ns.L.GetLocale then
        return ns.L:GetLocale()
    end
    return GetLocale and GetLocale() == "ruRU" and "ruRU" or "enUS"
end

setmetatable(L, {
    __index = function(_, key)
        if getLocale() == "ruRU" then
            return ruRU[key] or enUS[key] or key
        end
        return enUS[key] or key
    end
})

ns.AuctionTracker.L = L
