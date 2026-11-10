local ADDON_NAME, ns = ...
ns.Goal = ns.Goal or {}

local enUS = {
    GOAL_INPUT_TEXT = "Enter goal amount in gold:",
    GOAL_CLEAR_BTN  = "Clear",
}

local ruRU = {
    GOAL_INPUT_TEXT = "Введите сумму цели (в голде):",
    GOAL_CLEAR_BTN  = "Сброс",
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

ns.Goal.L = L
