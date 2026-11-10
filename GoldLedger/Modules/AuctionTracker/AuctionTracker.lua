--[[
    GoldLedger / AuctionTracker: AuctionTracker.lua
    Feature module entry point.

    Uses core's modular API:
      - GoldLedger:RegisterFeature("auction", module)     — lifecycle
      - GoldLedger:RegisterHeaderButton(name, label, fn)  — button in main frame
      - GoldLedger:RegisterSlashCommand("auction", fn)    — /gl auction
      - Data:RegisterNamespace("auction", defaults)        — namespaced data in shared DB
      - UI:CreateDropdown / CreatePaginationBar / etc.     — shared UI components
      - Tracker:OnGoldChanged(callback)                    — amend entries

    To uninstall: delete Modules/AuctionTracker/ + 5 lines from .toc.
    Core continues working unchanged.
]]

local ADDON_NAME, ns = ...
ns.AuctionTracker = ns.AuctionTracker or {}

local GoldLedger = ns.GoldLedger
local Module = {}
ns.AuctionTracker.Module = Module

-------------------------------------------------------------------------------
-- Feature registration (uses new Core API)
-------------------------------------------------------------------------------
if GoldLedger and GoldLedger.RegisterFeature then
    GoldLedger:RegisterFeature("auction", Module)
end

-------------------------------------------------------------------------------
-- Lifecycle (called by Core via CallFeatures)
-------------------------------------------------------------------------------
function Module:OnInitialize()
    -- Register namespaced data in shared DB
    local Data = GoldLedger:GetModule("Data")
    if Data and Data.RegisterNamespace then
        Data:RegisterNamespace("auction", {
            settings = {},  -- module-specific settings (future use)
        })
    end

    -- Initialize tracker (amender pattern)
    local Tracker = ns.AuctionTracker.Tracker
    if Tracker and Tracker.OnInitialize then Tracker:OnInitialize() end
end

function Module:OnEnable()
    -- Register header button (dynamic, core creates it in MainFrame)
    if GoldLedger.RegisterHeaderButton then
        GoldLedger:RegisterHeaderButton("auction", function()
            local L = ns.AuctionTracker.L
            return L and L["AUCTION_BUTTON"] or "Auction"
        end, function()
            local Frame = ns.AuctionTracker.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end

    -- Register slash subcommand: /gl auction
    if GoldLedger.RegisterSlashCommand then
        GoldLedger:RegisterSlashCommand("auction", function()
            local Frame = ns.AuctionTracker.Frame
            if Frame and Frame.Toggle then Frame:Toggle() end
        end)
    end
end

-------------------------------------------------------------------------------
-- Standalone slash commands (backup, zero coupling)
-------------------------------------------------------------------------------
_G.SLASH_GOLDLEDGERAUCTION1 = "/gla"

SlashCmdList = SlashCmdList or {}
SlashCmdList["GOLDLEDGERAUCTION"] = function(msg)
    local cmd = strtrim(msg or ""):lower()

    if cmd == "seed" then
        -- Debug: inject fake AH entries into core Data
        local Data = GoldLedger and GoldLedger:GetModule("Data")
        if not Data then print("|cffff4444GoldLedger Data not loaded|r") return end

        local now = time()
        local DAY = 86400

        -- Sales (income, ahType="sale")
        Data:AddEntry(1900000, "ah", { ahType = "sale", itemName = "Black Lotus", grossAmount = 2000000, cutAmount = 100000 })
        Data:AddEntry(500000, "ah", { ahType = "sale", itemName = "Flask of Power", grossAmount = 526315, cutAmount = 26315, quantity = 5, unitPrice = 100000 })
        Data:AddEntry(120000, "ah", { ahType = "sale", itemName = "Linen Cloth", grossAmount = 126315, cutAmount = 6315, quantity = 200, unitPrice = 600 })
        Data:AddEntry(50000, "ah", { ahType = "sale", itemName = "Copper Ore", grossAmount = 52631, cutAmount = 2631, quantity = 100, unitPrice = 500 })

        -- Purchases (expense, ahType="purchase")
        Data:AddEntry(-250000, "ah", { ahType = "purchase", itemName = "Iron Ore", quantity = 50, unitPrice = 5000 })
        Data:AddEntry(-1400000, "ah", { ahType = "purchase", itemName = "Phial Cauldron", quantity = 1, unitPrice = 1400000 })

        -- Deposits (expense, ahType="deposit")
        Data:AddEntry(-5000, "ah", { ahType = "deposit", itemName = "Heavy Leather" })
        Data:AddEntry(-12000, "ah", { ahType = "deposit", itemName = "Mooncloth" })

        -- Plain "ah" entries (no ahType, should be hidden in Auction popup)
        Data:AddEntry(30000, "ah")
        Data:AddEntry(-8000, "ah")

        print("|cff00ff00GoldLedger:|r Seeded 10 fake AH entries. Open /gla to see them.")
        return
    end

    if cmd == "clear" then
        -- Debug: remove all AH entries
        local Data = GoldLedger and GoldLedger:GetModule("Data")
        if Data then
            local charData = Data:EnsureCharacterData()
            local cleaned = {}
            for _, e in ipairs(charData.entries) do
                if e.source ~= "ah" then
                    cleaned[#cleaned + 1] = e
                end
            end
            charData.entries = cleaned
            print("|cff00ff00GoldLedger:|r Cleared all AH entries.")
        end
        return
    end

    local Frame = ns.AuctionTracker.Frame
    if Frame and Frame.Toggle then Frame:Toggle() end
end
