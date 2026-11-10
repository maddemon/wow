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

local zhCN = {
    HISTORY_BUTTON       = "流水",
    HISTORY_TITLE        = "金币流水",
    HISTORY_PAGE         = "第 %d/%d 页",
    HISTORY_SHOWING      = "显示第 %d-%d 条，共 %d 条",
    HISTORY_MIN_AMOUNT   = "最低金额：",
    HISTORY_TYPE_ALL     = "全部",
    HISTORY_TYPE_INCOME  = "收入",
    HISTORY_TYPE_EXPENSE = "支出",
    HISTORY_NO_RESULTS   = "没有符合筛选条件的记录。",
    HISTORY_PREV         = "上一页",
    HISTORY_NEXT         = "下一页",
}

local L = {}
local function getLocale()
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

ns.History.L = L
