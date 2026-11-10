--[[
    GoldLedger / Calculator: Locale.lua
    Self-contained localization for this module.
]]

local ADDON_NAME, ns = ...
ns.Calculator = ns.Calculator or {}

local enUS = {
    CALC_BUTTON    = "Calculator",
    CALC_TITLE     = "Calculator",
    CALC_CLEAR     = "C",
}

local zhCN = {
    CALC_BUTTON    = "换算器",
    CALC_TITLE     = "金币换算",
    CALC_CLEAR     = "C",
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

ns.Calculator.L = L
