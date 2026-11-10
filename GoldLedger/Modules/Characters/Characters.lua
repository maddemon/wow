local ADDON_NAME, ns = ...
ns.Characters = ns.Characters or {}

local GoldLedger = ns.GoldLedger
local Module = {}
ns.Characters.Module = Module

if GoldLedger and GoldLedger.RegisterFeature then
    GoldLedger:RegisterFeature("characters", Module)
end

function Module:OnInitialize() end

function Module:OnEnable()
    if GoldLedger.RegisterHeaderButton then
        GoldLedger:RegisterHeaderButton("characters", function()
            local L = ns.Characters.L
            return L and L["CHARS_BUTTON"] or "Characters"
        end, function()
            local Frame = ns.Characters.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end

    if GoldLedger.RegisterSlashCommand then
        GoldLedger:RegisterSlashCommand("chars", function()
            local Frame = ns.Characters.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end
end
