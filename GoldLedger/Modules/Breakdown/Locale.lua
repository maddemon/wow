local ADDON_NAME, ns = ...
ns.Breakdown = ns.Breakdown or {}

local enUS = {
    BREAKDOWN_BUTTON = "Summary",
    BREAKDOWN_TITLE  = "Source Breakdown",
    BREAKDOWN_TODAY  = "Today",
    BREAKDOWN_WEEK   = "Week",
    BREAKDOWN_MONTH  = "Month",
    BREAKDOWN_ALL    = "All",
    BREAKDOWN_TOTAL  = "Total",
    BREAKDOWN_SOURCE = "Source",
}

local zhCN = {
    BREAKDOWN_BUTTON = "汇总",
    BREAKDOWN_TITLE  = "按来源汇总",
    BREAKDOWN_TODAY  = "今天",
    BREAKDOWN_WEEK   = "本周",
    BREAKDOWN_MONTH  = "本月",
    BREAKDOWN_ALL    = "全部",
    BREAKDOWN_TOTAL  = "合计",
    BREAKDOWN_SOURCE = "来源",
}

local L = {}
local function getLocale()
    if ns.L and ns.L.GetLocale then return ns.L:GetLocale() end
    return GetLocale and GetLocale() == "zhCN" and "zhCN" or "enUS"
end
setmetatable(L, { __index = function(_, key)
    if getLocale() == "zhCN" then return zhCN[key] or enUS[key] or key end
    return enUS[key] or key
end })

ns.Breakdown.L = L
