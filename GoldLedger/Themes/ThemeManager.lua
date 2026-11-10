--[[
    GoldLedger: Themes/ThemeManager.lua
    Native WoW color palette. The multi-theme system was removed when
    GoldLedger moved to native WoW UI templates — only one palette remains.

    Public API: GetColor(key) → table | C(key) → r,g,b,a | GetSourceColors().
    Module name "Themes" is kept for back-compat with feature modules that
    call `GoldLedger:GetModule("Themes"):C(...)`.
]]

local ADDON_NAME, ns = ...
local GoldLedger = ns.GoldLedger

local Themes = {}
GoldLedger:RegisterModule("Themes", Themes)

-------------------------------------------------------------------------------
-- Single native color palette
-------------------------------------------------------------------------------
local NATIVE = {
    -- Text / semantic
    INCOME                = { 0.20, 1.00, 0.20, 1 },
    EXPENSE               = { 1.00, 0.30, 0.30, 1 },
    GOLD_TEXT             = { 1.00, 0.82, 0.00, 1 },
    LABEL                 = { 0.85, 0.85, 0.85, 1 },
    TEXT_WHITE            = { 1.00, 1.00, 1.00, 1 },
    TEXT_DIM              = { 0.60, 0.60, 0.60, 1 },
    TEXT_HINT             = { 0.55, 0.55, 0.55, 1 },
    TEXT_TIME             = { 0.70, 0.70, 0.75, 1 },
    HEADER                = { 1.00, 0.82, 0.00, 1 },
    BALANCE_POS           = { 0.40, 1.00, 0.40, 1 },
    BALANCE_NEG           = { 1.00, 0.40, 0.40, 1 },
    DAY_LABEL             = { 0.60, 0.60, 0.60, 1 },
    ZERO_VALUE            = { 0.45, 0.45, 0.45, 1 },

    -- Frame shell (mostly native-template-controlled; kept for compat)
    FRAME_BG              = { 0.05, 0.05, 0.08, 0.95 },
    BORDER                = { 0.40, 0.40, 0.40, 1 },
    TITLE_BG              = { 0.10, 0.10, 0.15, 0.9 },
    CARD_BG               = { 0.10, 0.10, 0.12, 0.9 },
    CARD_BORDER           = { 0.30, 0.30, 0.35, 1 },
    CARD_TITLE            = { 1.00, 0.82, 0.00, 1 },

    -- Buttons (native template handles most, but legacy code still reads these)
    BTN_BG                = { 0.15, 0.15, 0.18, 1 },
    BTN_BG_HOVER          = { 0.25, 0.25, 0.30, 1 },
    BTN_BORDER            = { 0.35, 0.35, 0.40, 1 },
    BTN_ACTIVE            = { 0.40, 0.30, 0.10, 1 },
    BTN_INACTIVE          = { 0.10, 0.10, 0.12, 1 },
    ACCENT_BTN_BG         = { 0.55, 0.35, 0.10, 1 },
    ACCENT_BTN_BG_HOVER   = { 0.70, 0.45, 0.15, 1 },
    ACCENT_BTN_BORDER     = { 0.80, 0.60, 0.20, 1 },
    ACCENT_BTN_TEXT       = { 1.00, 0.90, 0.50, 1 },

    -- Filter bar
    FILTER_INACTIVE_BG    = { 0.10, 0.10, 0.12, 0.6 },
    FILTER_INACTIVE_TEXT  = { 0.60, 0.60, 0.60, 1 },

    -- Chart
    CHART_BG              = { 0.05, 0.05, 0.08, 0.7 },
    CHART_GUIDE           = { 0.30, 0.30, 0.35, 0.5 },

    -- Goal bar
    GOAL_BG               = { 0.10, 0.10, 0.12, 1 },
    GOAL_FILL             = { 0.30, 0.70, 0.30, 1 },

    -- Rows / scroll
    ROW_ALT               = { 1.00, 1.00, 1.00, 0.05 },
    SCROLL_BG             = { 0.02, 0.02, 0.04, 0.6 },

    -- Hairline separator inside cards
    SEPARATOR             = { 0.30, 0.30, 0.35, 0.6 },

    -- Sources
    SOURCE_COLORS = {
        vendor    = { 0.60, 0.80, 1.00 },
        repair    = { 1.00, 0.60, 0.40 },
        ah        = { 1.00, 0.84, 0.20 },
        mail      = { 0.70, 0.70, 1.00 },
        quest     = { 0.80, 0.60, 1.00 },
        loot      = { 0.40, 1.00, 0.40 },
        trade     = { 1.00, 0.40, 0.70 },
        -- Transfers (not income/expense)
        warband   = { 0.30, 0.80, 0.90 },
        guildbank = { 0.85, 0.75, 0.55 },
        unknown   = { 0.70, 0.70, 0.70 },
    },
}

-------------------------------------------------------------------------------
-- Lifecycle
-------------------------------------------------------------------------------
function Themes:OnInitialize() end

-------------------------------------------------------------------------------
-- Public API (compat with previous multi-theme system)
-------------------------------------------------------------------------------
function Themes:GetColor(key)  return NATIVE[key] end

function Themes:C(key)
    local c = NATIVE[key]
    if not c then return 1, 1, 1, 1 end
    return c[1], c[2], c[3], c[4]
end

function Themes:GetSourceColors() return NATIVE.SOURCE_COLORS end

function Themes:GetBgTexture()   return "Interface\\Tooltips\\UI-Tooltip-Background" end
function Themes:GetEdgeTexture() return "Interface\\Tooltips\\UI-Tooltip-Border" end
