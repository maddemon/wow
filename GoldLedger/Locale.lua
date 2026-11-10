--[[
    GoldLedger: Locale.lua
    Pattern: Registry with __index fallback

    Two languages only: enUS (base / fallback) and zhCN.
    Access: GoldLedger.L["KEY"]
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
    ["SETTINGS_LANG_AUTO"]   = "Auto",
    ["SETTINGS_LANG_EN"]     = "English",
    ["SETTINGS_LANG_ZH"]     = "简体中文",
    ["SETTINGS_RESET_SESSION"] = "Reset session",

    -- Shared UI widgets (pagination bar, status bar)
    ["UI_PREV"]              = "< Prev",
    ["UI_NEXT"]              = "Next >",
    ["UI_PAGE"]              = "Page %d/%d",
    ["UI_RECORDS"]           = "Records: %d",
    ["UI_INCOME"]            = "Income: %s",
    ["UI_EXPENSE"]           = "Expense: %s",
    ["UI_NET"]               = "Net: %s",
}

-------------------------------------------------------------------------------
-- Registry: zhCN strings
-------------------------------------------------------------------------------
local zhStrings = {
    -- General
    ["ADDON_LOADED"]        = "|cff00ff00GoldLedger|r 已加载。输入 |cfffff569/gl|r 打开窗口。",
    ["SLASH_HELP"]          = "用法：/gl 开关窗口，/gl reset 清空本角色数据",
    ["RESET_CONFIRM"]       = "本角色的数据已清空。",

    -- UI Headers
    ["HEADER_TODAY"]        = "今天",
    ["HEADER_YESTERDAY"]    = "昨天",
    ["HEADER_MONTH"]        = "本月",
    ["HEADER_RECENT"]       = "最近流水",
    ["HEADER_SESSION"]      = "本次登录",
    ["HEADER_INCOME"]       = "收入",
    ["HEADER_EXPENSE"]      = "支出",
    ["HEADER_BALANCE"]      = "余额",
    ["HEADER_NET"]          = "净额",
    ["HEADER_ON_HAND"]      = "手头",

    -- Tooltip
    ["TOOLTIP_HINT"]        = "|cffffffff点击|r 开关窗口",
    ["TOOLTIP_TODAY"]       = "今天",
    ["TOOLTIP_MONTH"]       = "本月",

    -- Gold formatting
    ["GOLD_ABBR"]           = "金",
    ["SILVER_ABBR"]         = "银",
    ["COPPER_ABBR"]         = "铜",

    -- Time: formats stay ISO — CleanupOldEntries compares date keys as strings

    -- Messages
    ["NO_DATA"]             = "还没有记录。",
    ["INCOME_LOGGED"]       = "已记入 +%s",
    ["EXPENSE_LOGGED"]      = "已记入 -%s",

    -- Chart
    ["HEADER_CHART"]        = "每日收支图",
    ["CHART_DAY"]           = "%d 日",
    ["CHART_7D"]            = "7 天",
    ["CHART_30D"]           = "30 天",
    ["CHART_ALL"]           = "全部",

    -- Filter
    ["FILTER_ALL"]          = "全部",

    -- Source categories
    ["SRC_VENDOR"]          = "商人",
    ["SRC_AH"]              = "拍卖行",
    ["SRC_MAIL"]            = "邮件",
    ["SRC_QUEST"]           = "任务",
    ["SRC_LOOT"]            = "拾取",
    ["SRC_TRADE"]           = "交易",
    ["SRC_REPAIR"]          = "修理",
    ["SRC_WARBAND"]         = "战团银行",
    ["SRC_GUILDBANK"]       = "公会银行",
    ["SRC_UNKNOWN"]         = "其他",

    -- Goal
    ["HEADER_GOAL"]          = "目标",
    ["GOAL_SET"]             = "目标已设为 %s",
    ["GOAL_CLEARED"]         = "目标已清除。",
    ["GOAL_REACHED"]         = "已达成目标！",
    ["GOAL_REMAINING"]       = "按当前速度约需 %d 天",
    ["GOAL_TOO_FAR"]         = "按当前速度遥遥无期",
    ["GOAL_SET_BUTTON"]      = "设目标",
    ["GOAL_CHANGE_BUTTON"]   = "改目标",
    ["GOAL_USAGE"]           = "用法：/gl goal <金币数> 或 /gl goal clear",
    ["GOAL_NONE"]            = "点击设置目标",
    ["GOAL_CLICK_HINT"]      = "|cffffffff点击|r 修改目标",

    -- Settings
    ["SETTINGS_BUTTON"]      = "设置",
    ["HEADER_SETTINGS"]      = "设置",
    ["SETTINGS_MINIMAP"]     = "显示小地图按钮",
    ["SETTINGS_LANGUAGE"]    = "语言",
    ["SETTINGS_LANG_AUTO"]   = "自动",
    ["SETTINGS_LANG_EN"]     = "English",
    ["SETTINGS_LANG_ZH"]     = "简体中文",
    ["SETTINGS_RESET_SESSION"] = "清空本次登录统计",

    -- Shared UI widgets
    ["UI_PREV"]              = "上一页",
    ["UI_NEXT"]              = "下一页",
    ["UI_PAGE"]              = "第 %d/%d 页",
    ["UI_RECORDS"]           = "记录数：%d",
    ["UI_INCOME"]            = "收入：%s",
    ["UI_EXPENSE"]           = "支出：%s",
    ["UI_NET"]               = "净额：%s",
}

-------------------------------------------------------------------------------
-- Fallback mechanism via __index metamethod
-------------------------------------------------------------------------------
local KNOWN_LOCALES = { enUS = "enUS", zhCN = "zhCN" }

-- GetLocale() is the client's TEXT language, not the server region: a CN
-- client can report enUS while its realms are Chinese. The realm name is the
-- second signal — [\228-\244] hits any UTF-8 lead byte in U+4000..U+FFFF,
-- which covers CJK. GetRealmName() is nil until PLAYER_LOGIN, so Core
-- re-resolves the auto setting at login.
local function isChineseClient()
    local locale = GetLocale and GetLocale() or ""
    if locale == "zhCN" or locale == "zhTW" then return true end
    local realm = GetRealmName and GetRealmName() or ""
    return realm:match("[\228-\244]") ~= nil
end

local function resolve(locale)
    if locale == nil then
        return isChineseClient() and "zhCN" or "enUS"
    end
    return KNOWN_LOCALES[locale] or "enUS"
end

local L = {}
local activeLocale = resolve(nil)

setmetatable(L, {
    __index = function(_, key)
        if activeLocale == "zhCN" then
            return zhStrings[key] or defaultStrings[key] or key
        end
        return defaultStrings[key] or key
    end
})

--- Switches language (called from Settings)
--- @param locale string "zhCN"|"enUS"|nil (nil = auto)
function L:SetLocale(locale)
    activeLocale = resolve(locale)
end

--- @return string
function L:GetLocale()
    return activeLocale
end

-------------------------------------------------------------------------------
-- Export to addon namespace
-------------------------------------------------------------------------------
ns.L = L
