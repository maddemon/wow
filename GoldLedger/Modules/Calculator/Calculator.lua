--[[
    GoldLedger / Calculator: Calculator.lua
    Feature module entry point.

    Uses core's modular API:
      - GoldLedger:RegisterFeature("calculator", module)  — lifecycle
      - GoldLedger:RegisterHeaderButton(name, label, fn)   — button in main frame
      - GoldLedger:RegisterSlashCommand("calc", fn)        — /gl calc

    To uninstall: delete Modules/Calculator/ + 3 lines from .toc.
]]

local ADDON_NAME, ns = ...
ns.Calculator = ns.Calculator or {}

local GoldLedger = ns.GoldLedger
local Module = {}
ns.Calculator.Module = Module

-------------------------------------------------------------------------------
-- Feature registration
-------------------------------------------------------------------------------
if GoldLedger and GoldLedger.RegisterFeature then
    GoldLedger:RegisterFeature("calculator", Module)
end

-------------------------------------------------------------------------------
-- Lifecycle
-------------------------------------------------------------------------------
function Module:OnInitialize()
    -- Nothing to init — frame is created lazily on first Toggle
end

function Module:OnEnable()
    -- Register header button (dynamic label — updates on language switch)
    if GoldLedger.RegisterHeaderButton then
        GoldLedger:RegisterHeaderButton("calculator", function()
            local L = ns.Calculator.L
            local label = L and L["CALC_BUTTON"] or "Calculator"
            -- Embed calculator icon in label
            return "|TInterface\\AddOns\\GoldLedger\\Modules\\Calculator\\calc_icon:12:12|t " .. label
        end, function()
            local Frame = ns.Calculator.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end

    -- Register slash subcommand: /gl calc
    if GoldLedger.RegisterSlashCommand then
        GoldLedger:RegisterSlashCommand("calc", function()
            local Frame = ns.Calculator.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end
end
