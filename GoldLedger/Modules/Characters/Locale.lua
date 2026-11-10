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

local ruRU = {
    CHARS_BUTTON        = "Персонажи",
    CHARS_TITLE         = "Персонажи",
    CHARS_ACCOUNT_TOTAL = "Итого по аккаунту",
    CHARS_DAY_LABEL     = "Сегодня",
    CHARS_WEEK_LABEL    = "За неделю",
    CHARS_MONTH_LABEL   = "За месяц",
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

ns.Characters.L = L
