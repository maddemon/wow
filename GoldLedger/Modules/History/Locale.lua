--[[
    GoldLedger / History: Locale.lua
    Self-contained localization for this module.
]]

local ADDON_NAME, ns = ...
ns.History = ns.History or {}

local enUS = {
    HISTORY_BUTTON       = "History",
    HISTORY_TITLE        = "Transaction History",
    HISTORY_PAGE         = "Page %d/%d",
    HISTORY_SHOWING      = "Showing %d-%d of %d",
    HISTORY_MIN_AMOUNT   = "Min gold:",
    HISTORY_TYPE_ALL     = "All",
    HISTORY_TYPE_INCOME  = "Income",
    HISTORY_TYPE_EXPENSE = "Expense",
    HISTORY_NO_RESULTS   = "No transactions match filters.",
    HISTORY_PREV         = "< Prev",
    HISTORY_NEXT         = "Next >",
}

local ruRU = {
    HISTORY_BUTTON       = "История",
    HISTORY_TITLE        = "История транзакций",
    HISTORY_PAGE         = "Стр. %d/%d",
    HISTORY_SHOWING      = "Показано %d-%d из %d",
    HISTORY_MIN_AMOUNT   = "Мин. голда:",
    HISTORY_TYPE_ALL     = "Все",
    HISTORY_TYPE_INCOME  = "Доход",
    HISTORY_TYPE_EXPENSE = "Расход",
    HISTORY_NO_RESULTS   = "Нет транзакций по фильтру.",
    HISTORY_PREV         = "< Назад",
    HISTORY_NEXT         = "Далее >",
}

local L = {}
local function getLocale()
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

ns.History.L = L
