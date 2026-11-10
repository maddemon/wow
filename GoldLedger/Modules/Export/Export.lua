local ADDON_NAME, ns = ...
ns.Export = ns.Export or {}

local GoldLedger = ns.GoldLedger
local Module = {}
ns.Export.Module = Module

if GoldLedger and GoldLedger.RegisterFeature then
    GoldLedger:RegisterFeature("export", Module)
end

function Module:OnInitialize() end

function Module:OnEnable()
    if GoldLedger.RegisterHeaderButton then
        GoldLedger:RegisterHeaderButton("export", function()
            local L = ns.Export.L
            return L and L["EXPORT_BUTTON"] or "Export"
        end, function()
            local Frame = ns.Export.Frame
            if Frame and Frame.Show then Frame:Show() end
        end)
    end

    if GoldLedger.RegisterSlashCommand then
        GoldLedger:RegisterSlashCommand("export", function()
            local Frame = ns.Export.Frame
            if Frame and Frame.Show then Frame:Show() end
        end)
    end
end
