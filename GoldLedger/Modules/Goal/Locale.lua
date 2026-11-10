local ADDON_NAME, ns = ...
ns.Goal = ns.Goal or {}

local enUS = {
    GOAL_INPUT_TEXT = "Enter goal amount in gold:",
    GOAL_CLEAR_BTN  = "Clear",
}

local zhCN = {
    GOAL_INPUT_TEXT = "输入目标金币数：",
    GOAL_CLEAR_BTN  = "清除",
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

ns.Goal.L = L
