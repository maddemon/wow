--[[
    GoldLedger: Locale.lua
    Pattern: Registry with __index fallback

    Локализация загружается первой. Английский — базовый язык (fallback).
    Русские строки перекрывают английские через GetLocale().
    Доступ: GoldLedger.L["KEY"]
]]

local ADDON_NAME, ns = ...

-------------------------------------------------------------------------------
-- Registry: базовые строки (English fallback)
-------------------------------------------------------------------------------
local defaultStrings = {
    -- General
    ["ADDON_LOADED"]        = "|cff00ff00GoldLedger|r loaded. Type |cfffff569/gl|r to open.",
    ["ADDON_TITLE"]         = "GoldLedger",
    ["SLASH_HELP"]          = "Usage: /gl — toggle window, /gl reset — reset data",
    ["RESET_CONFIRM"]       = "All data for this character has been reset.",

    -- UI Headers
    ["HEADER_TODAY"]        = "Today",
    ["HEADER_YESTERDAY"]    = "Yesterday",
    ["HEADER_MONTH"]        = "This Month",
    ["HEADER_RECENT"]       = "Recent Transactions",
    ["HEADER_SESSION"]      = "Session",
    ["HEADER_INCOME"]       = "Income",
    ["HEADER_EXPENSE"]      = "Expense",
    ["HEADER_BALANCE"]      = "Balance",
    ["HEADER_NET"]          = "Net",
    ["HEADER_ON_HAND"]      = "On Hand",

    -- Tooltip
    ["TOOLTIP_TITLE"]       = "GoldLedger",
    ["TOOLTIP_HINT"]        = "|cffffffffClick|r to toggle window",
    ["TOOLTIP_TODAY"]       = "Today",
    ["TOOLTIP_MONTH"]       = "This Month",

    -- Gold formatting
    ["GOLD_ABBR"]           = "g",
    ["SILVER_ABBR"]         = "s",
    ["COPPER_ABBR"]         = "c",

    -- Time
    ["TIME_FORMAT"]         = "%H:%M",
    ["DATE_FORMAT"]         = "%Y-%m-%d",
    ["MONTH_FORMAT"]        = "%Y-%m",

    -- Messages
    ["NO_DATA"]             = "No transactions yet.",
    ["INCOME_LOGGED"]       = "+%s recorded",
    ["EXPENSE_LOGGED"]      = "-%s recorded",

    -- Chart
    ["HEADER_CHART"]        = "Daily Chart",
    ["CHART_DAY"]           = "Day %d",
    ["CHART_7D"]            = "7 days",
    ["CHART_30D"]           = "30 days",
    ["CHART_ALL"]           = "All",

    -- Filter
    ["FILTER_ALL"]          = "All",

    -- Source categories
    ["SRC_VENDOR"]          = "Vendor",
    ["SRC_AH"]              = "AH",
    ["SRC_MAIL"]            = "Mail",
    ["SRC_QUEST"]           = "Quest",
    ["SRC_LOOT"]            = "Loot",
    ["SRC_TRADE"]           = "Trade",
    ["SRC_REPAIR"]          = "Repair",
    ["SRC_WARBAND"]         = "Warband",
    ["SRC_GUILDBANK"]       = "Guild Bank",
    ["SRC_UNKNOWN"]         = "Other",

    -- Goal
    ["HEADER_GOAL"]          = "Goal",
    ["GOAL_SET"]             = "Goal set: %s",
    ["GOAL_CLEARED"]         = "Goal cleared.",
    ["GOAL_REACHED"]         = "Goal reached!",
    ["GOAL_REMAINING"]       = "~%d days left",
    ["GOAL_TOO_FAR"]         = "Too far at this rate",
    ["GOAL_SET_BUTTON"]      = "Set goal",
    ["GOAL_CHANGE_BUTTON"]   = "Change goal",
    ["GOAL_USAGE"]           = "Usage: /gl goal <gold> | /gl goal clear",
    ["GOAL_NONE"]            = "Click to set goal",
    ["GOAL_CLICK_HINT"]      = "|cffffffffClick|r to change goal",

    -- Multi-character

    -- Export

    -- Source Breakdown

    -- Characters tabs

    -- Settings
    ["SETTINGS_BUTTON"]      = "Settings",
    ["HEADER_SETTINGS"]      = "Settings",
    ["SETTINGS_MINIMAP"]     = "Show minimap button",
    ["SETTINGS_LANGUAGE"]    = "Language",
    ["SETTINGS_LANG_AUTO"]   = "Auto (game language)",
    ["SETTINGS_LANG_EN"]     = "English",
    ["SETTINGS_LANG_RU"]     = "Русский",
    ["SETTINGS_RESET_SESSION"] = "Reset session",

    -- History
}

-------------------------------------------------------------------------------
-- Registry: русские строки (ruRU override)
-------------------------------------------------------------------------------
local ruStrings = {
    -- General
    ["ADDON_LOADED"]        = "|cff00ff00GoldLedger|r загружен. Введите |cfffff569/gl|r для открытия.",
    ["SLASH_HELP"]          = "Использование: /gl — окно, /gl reset — сброс данных",
    ["RESET_CONFIRM"]       = "Все данные персонажа сброшены.",

    -- UI Headers
    ["HEADER_TODAY"]        = "Сегодня",
    ["HEADER_YESTERDAY"]    = "Вчера",
    ["HEADER_MONTH"]        = "Этот месяц",
    ["HEADER_RECENT"]       = "Последние транзакции",
    ["HEADER_SESSION"]      = "Сессия",
    ["HEADER_INCOME"]       = "Доход",
    ["HEADER_EXPENSE"]      = "Расход",
    ["HEADER_BALANCE"]      = "Баланс",
    ["HEADER_NET"]          = "Итого",
    ["HEADER_ON_HAND"]      = "На руках",

    -- Tooltip
    ["TOOLTIP_HINT"]        = "|cffffffffКлик|r — открыть окно",
    ["TOOLTIP_TODAY"]       = "Сегодня",
    ["TOOLTIP_MONTH"]       = "Этот месяц",

    -- Gold formatting
    ["GOLD_ABBR"]           = "з",
    ["SILVER_ABBR"]         = "с",
    ["COPPER_ABBR"]         = "м",

    -- Time
    ["TIME_FORMAT"]         = "%H:%M",
    ["DATE_FORMAT"]         = "%d.%m.%Y",
    ["MONTH_FORMAT"]        = "%Y-%m",

    -- Messages
    ["NO_DATA"]             = "Транзакций пока нет.",
    ["INCOME_LOGGED"]       = "+%s записан",
    ["EXPENSE_LOGGED"]      = "-%s записан",

    -- Chart
    ["HEADER_CHART"]        = "График по дням",
    ["CHART_DAY"]           = "День %d",
    ["CHART_7D"]            = "7 дней",
    ["CHART_30D"]           = "30 дней",
    ["CHART_ALL"]           = "Всё",

    -- Filter
    ["FILTER_ALL"]          = "Все",

    -- Source categories
    ["SRC_VENDOR"]          = "Вендор",
    ["SRC_AH"]              = "АХ",
    ["SRC_MAIL"]            = "Почта",
    ["SRC_QUEST"]           = "Квест",
    ["SRC_LOOT"]            = "Лут",
    ["SRC_TRADE"]           = "Обмен",
    ["SRC_REPAIR"]          = "Ремонт",
    ["SRC_WARBAND"]         = "Варбанд",
    ["SRC_GUILDBANK"]       = "Гильдбанк",
    ["SRC_UNKNOWN"]         = "Другое",

    -- Goal
    ["HEADER_GOAL"]          = "Цель",
    ["GOAL_SET"]             = "Цель: %s",
    ["GOAL_CLEARED"]         = "Цель сброшена.",
    ["GOAL_REACHED"]         = "Цель достигнута!",
    ["GOAL_REMAINING"]       = "~%d дн. осталось",
    ["GOAL_TOO_FAR"]         = "При текущем темпе — очень нескоро",
    ["GOAL_SET_BUTTON"]      = "Установить цель",
    ["GOAL_CHANGE_BUTTON"]   = "Изменить цель",
    ["GOAL_USAGE"]           = "/gl goal <сумма в голде> | /gl goal clear",
    ["GOAL_NONE"]            = "Нажмите для установки цели",
    ["GOAL_CLICK_HINT"]      = "|cffffffffКлик|r — изменить цель",

    -- Multi-character

    -- Export

    -- Source Breakdown

    -- Characters tabs

    -- Settings
    ["SETTINGS_BUTTON"]      = "Настройки",
    ["HEADER_SETTINGS"]      = "Настройки",
    ["SETTINGS_MINIMAP"]     = "Показывать кнопку на миникарте",
    ["SETTINGS_LANGUAGE"]    = "Язык",
    ["SETTINGS_LANG_AUTO"]   = "Авто (язык игры)",
    ["SETTINGS_LANG_EN"]     = "English",
    ["SETTINGS_LANG_RU"]     = "Русский",
    ["SETTINGS_RESET_SESSION"] = "Сбросить сессию",

    -- History
}

-------------------------------------------------------------------------------
-- Fallback mechanism via __index metamethod
-------------------------------------------------------------------------------
local L = {}
local activeLocale = GetLocale() == "ruRU" and "ruRU" or "enUS"

setmetatable(L, {
    __index = function(_, key)
        if activeLocale == "ruRU" then
            return ruStrings[key] or defaultStrings[key] or key
        else
            return defaultStrings[key] or key
        end
    end
})

--- Переключает язык (вызывается из Settings)
--- @param locale string "ruRU"|"enUS"|nil (nil = авто)
function L:SetLocale(locale)
    if locale == nil then
        activeLocale = GetLocale() == "ruRU" and "ruRU" or "enUS"
    else
        activeLocale = locale
    end
end

--- Возвращает текущий язык
--- @return string
function L:GetLocale()
    return activeLocale
end

-------------------------------------------------------------------------------
-- Export to addon namespace
-------------------------------------------------------------------------------
ns.L = L
