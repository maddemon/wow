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

local ruRU = {
    CALC_BUTTON    = "Калькулятор",
    CALC_TITLE     = "Калькулятор",
    CALC_CLEAR     = "C",
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

ns.Calculator.L = L
