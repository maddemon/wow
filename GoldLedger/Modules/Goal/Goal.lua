--[[
    GoldLedger / Goal: Goal.lua
    Feature module entry point.

    Owns the /gl goal slash command (extracted from Core in the SDK cleanup):
        /gl goal           — print usage hint
        /gl goal <N>       — set goal to N gold (must be > 0)
        /gl goal clear     — clear current goal
        /gl goal reset     — alias for clear
]]

local ADDON_NAME, ns = ...
ns.Goal = ns.Goal or {}

local GoldLedger = ns.GoldLedger
local L = ns.L  -- core L (for shared strings like GOAL_USAGE)

local Module = {}
ns.Goal.Module = Module

if GoldLedger and GoldLedger.RegisterFeature then
    GoldLedger:RegisterFeature("goal", Module)
end

local function handleGoalSlash(arg)
    arg = arg or ""
    local Data = GoldLedger and GoldLedger:GetModule("Data")
    if not Data then return end

    if arg == "" then
        print("|cff00ff00GoldLedger:|r " .. L["GOAL_USAGE"])
    elseif arg == "clear" or arg == "reset" then
        Data:ClearGoal()
        print("|cff00ff00GoldLedger:|r " .. L["GOAL_CLEARED"])
    else
        local n = tonumber(arg)
        if n and n > 0 then
            Data:SetGoal(n * 10000)
            print("|cff00ff00GoldLedger:|r " .. L["GOAL_SET"]:format(n .. L["GOLD_ABBR"]))
        else
            print("|cff00ff00GoldLedger:|r " .. L["GOAL_USAGE"])
        end
    end
end

function Module:OnInitialize() end

function Module:OnEnable()
    if GoldLedger.RegisterSlashCommand then
        GoldLedger:RegisterSlashCommand("goal", handleGoalSlash)
    end
end
