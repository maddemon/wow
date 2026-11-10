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

local ruRU = {
    BREAKDOWN_BUTTON = "Сводка",
    BREAKDOWN_TITLE  = "По источникам",
    BREAKDOWN_TODAY  = "Сегодня",
    BREAKDOWN_WEEK   = "Неделя",
    BREAKDOWN_MONTH  = "Месяц",
    BREAKDOWN_ALL    = "Всё",
    BREAKDOWN_TOTAL  = "Итого",
    BREAKDOWN_SOURCE = "Источник",
}

local L = {}
local function getLocale()
    if ns.L and ns.L.GetLocale then return ns.L:GetLocale() end
    return GetLocale and GetLocale() == "ruRU" and "ruRU" or "enUS"
end
setmetatable(L, { __index = function(_, key)
    if getLocale() == "ruRU" then return ruRU[key] or enUS[key] or key end
    return enUS[key] or key
end })

ns.Breakdown.L = L
