local ADDON_NAME, ns = ...
ns.Characters = ns.Characters or {}

local enUS = {
    CHARS_BUTTON        = "Characters",
    CHARS_TITLE         = "Characters",
    CHARS_ACCOUNT_TOTAL = "Account Total",
    CHARS_DAY_LABEL     = "Today",
    CHARS_WEEK_LABEL    = "This Week",
    CHARS_MONTH_LABEL   = "This month",
}

local zhCN = {
    CHARS_BUTTON        = "角色",
    CHARS_TITLE         = "各角色汇总",
    CHARS_ACCOUNT_TOTAL = "账号合计",
    CHARS_DAY_LABEL     = "今天",
    CHARS_WEEK_LABEL    = "本周",
    CHARS_MONTH_LABEL   = "本月",
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

ns.Characters.L = L
