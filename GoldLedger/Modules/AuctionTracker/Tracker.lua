--[[
    GoldLedger / AuctionTracker: Tracker.lua

    Architecture: AMENDER, not WRITER.
    Core Tracker.lua already records all gold changes (including AH context).
    This module subscribes to core's OnGoldChanged callback and ANNOTATES the
    last entry with metadata: ahType, itemName, quantity, unitPrice, grossAmount, cutAmount.

    No own Data:AddEntry calls — zero duplication, aggregates stay correct.

    Detection contexts:
      - Sale     : entryType=="income" + matching seller invoice in inbox
      - Purchase : entryType=="expense" + pendingPurchase set by PlaceBid/ConfirmCommodities hooks
      - Deposit  : entryType=="expense" + pendingDeposit set by PostItem/PostCommodity hooks
      - Other    : leave entry as plain "ah" (Query will hide it from Auction popup)
]]

local ADDON_NAME, ns = ...
ns.AuctionTracker = ns.AuctionTracker or {}

local Tracker = {}
ns.AuctionTracker.Tracker = Tracker

local AH_CUT_RATE = 0.05

-------------------------------------------------------------------------------
-- Private state
-------------------------------------------------------------------------------
local pendingPurchase = nil  -- { kind, auctionID/itemID, amount, quantity, unitPrice, itemName }
local pendingDeposit  = nil  -- { itemName, quantity, expectedAmount }
local itemNameCache   = {}   -- itemID/auctionID → name (best-effort)

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------
local function isAHSender(sender, subject)
    if _G.AUCTION_HOUSE_MAIL_SELLER and sender == _G.AUCTION_HOUSE_MAIL_SELLER then return true end
    if sender and sender:find("Auction House") then return true end
    if subject and (subject:find("Auction successful") or subject:find("Аукцион") or subject:find("успеш")) then return true end
    return false
end

function Tracker.CalculateCut(gross)
    return math.floor((gross or 0) * AH_CUT_RATE)
end

function Tracker.SetItemNameFor(key, name)
    if key then itemNameCache[key] = name end
end

function Tracker:GetPendingPurchase() return pendingPurchase end
function Tracker:GetPendingDeposit()  return pendingDeposit  end

--- Searches inbox for a seller invoice whose net (bid - consignment) matches the income amount.
--- @param netAmount number copper that arrived
--- @return string|nil itemName, number gross, number cut
function Tracker:FindMatchingSellerInvoice(netAmount)
    local num = (GetInboxNumItems and GetInboxNumItems()) or 0
    if num == 0 then return nil end

    for i = 1, num do
        local _, _, sender, subject = GetInboxHeaderInfo(i)
        if isAHSender(sender, subject) then
            local invoiceType, itemName, _, bid, buyout, _, consignment = GetInboxInvoiceInfo(i)
            if invoiceType == "seller" or invoiceType == "seller_temp_invoice" then
                local gross = bid or buyout or 0
                local cut = consignment or Tracker.CalculateCut(gross)
                local net = gross - cut
                if net == netAmount or gross == netAmount then
                    return itemName, gross, cut
                end
            end
        end
    end
    return nil
end

--- Fallback: scan inbox for AH sale mail and parse item name from subject.
--- Used when GetInboxInvoiceInfo returns nil (GoldPulse-style).
--- @param netAmount number copper received
--- @return string|nil itemName
function Tracker:FindSaleFromMailSubject(netAmount)
    local num = (GetInboxNumItems and GetInboxNumItems()) or 0
    if num == 0 then return nil end

    for i = 1, num do
        local _, _, sender, subject, money = GetInboxHeaderInfo(i)
        if isAHSender(sender, subject) and subject then
            -- Parse "Auction successful: Linen Cloth" or localized variants
            local itemName = subject:match("^[^:]+:%s*(.+)$")
            if itemName then
                return itemName
            end
        end
    end
    return nil
end

-------------------------------------------------------------------------------
-- Core callback: amend last entry with AH metadata
-------------------------------------------------------------------------------
function Tracker:OnCoreGoldChanged(amount, entryType, total, source)
    -- Handle both source="ah" (core detected) and source="mail" (core missed, we check ourselves)
    if source ~= "ah" and source ~= "mail" then return end

    local GL = ns.GoldLedger
    GL:Debug("AuctionTracker", "OnCoreGoldChanged | source:", source, "| type:", entryType, "| amount:", amount,
        "| pendingPurchase:", pendingPurchase and "YES" or "no",
        "| pendingDeposit:", pendingDeposit and "YES" or "no")

    local Data = GL and GL:GetModule("Data")
    if not Data then return end

    local recent = Data:GetRecentEntries(1)
    if #recent == 0 then return end
    local entry = recent[1]  -- reference, not copy

    -- Purchase context
    if pendingPurchase and entryType == "expense" then
        entry.source    = "ah"  -- ensure source is "ah"
        entry.ahType    = "purchase"
        entry.itemName  = pendingPurchase.itemName or itemNameCache[pendingPurchase.itemID or pendingPurchase.auctionID or 0]
        entry.quantity  = pendingPurchase.quantity
        entry.unitPrice = pendingPurchase.unitPrice
            or (pendingPurchase.quantity and pendingPurchase.quantity > 0
                and math.floor(amount / pendingPurchase.quantity) or nil)
        pendingPurchase = nil
        return
    end

    -- Deposit context
    if pendingDeposit and entryType == "expense" then
        entry.source   = "ah"  -- ensure source is "ah"
        entry.ahType   = "deposit"
        entry.itemName = pendingDeposit.itemName
        entry.quantity = pendingDeposit.quantity
        pendingDeposit = nil
        return
    end

    -- Sale: try to match against AH mail invoice
    if entryType == "income" then
        local itemName, gross, cut = self:FindMatchingSellerInvoice(amount)
        if itemName then
            entry.source      = "ah"  -- promote from "mail" to "ah" if core missed
            entry.ahType      = "sale"
            entry.itemName    = itemName
            entry.grossAmount = gross
            entry.cutAmount   = cut
            return
        end

        -- Fallback: no invoice data, try to parse item name from mail subject (like GoldPulse)
        local subjectName, netFromMail = self:FindSaleFromMailSubject(amount)
        if subjectName then
            local calcCut = math.floor(amount * AH_CUT_RATE / (1 - AH_CUT_RATE))
            local calcGross = amount + calcCut
            entry.source      = "ah"
            entry.ahType      = "sale"
            entry.itemName    = subjectName
            entry.grossAmount = calcGross
            entry.cutAmount   = calcCut
            return
        end
    end

    -- Else: leave as-is (Query hides entries without ahType from Auction popup)
end

-------------------------------------------------------------------------------
-- AH context hooks (set pending state, do NOT write entries)
-------------------------------------------------------------------------------
--- Resolve item name from itemLocation using C_Item API (like GoldPulse)
local function resolveItemName(itemLocation)
    if not itemLocation then return nil end
    -- Try C_Item.GetItemName first (works on item locations in bags)
    if _G.C_Item and _G.C_Item.DoesItemExist and _G.C_Item.DoesItemExist(itemLocation) then
        local name = _G.C_Item.GetItemName and _G.C_Item.GetItemName(itemLocation)
        if name then return name end
    end
    -- Fallback: itemID → C_Item.GetItemNameByID
    local itemID = itemLocation.itemID
        or (_G.C_Item and _G.C_Item.GetItemID and _G.C_Item.GetItemID(itemLocation))
    if itemID and _G.C_Item and _G.C_Item.GetItemNameByID then
        local name = _G.C_Item.GetItemNameByID(itemID)
        if name then return name end
    end
    return nil
end

function Tracker:OnPostItem(itemLocation, duration, quantity, bid, buyout)
    local deposit = 0
    if _G.C_AuctionHouse and _G.C_AuctionHouse.CalculateItemDeposit then
        deposit = _G.C_AuctionHouse.CalculateItemDeposit(itemLocation, duration, quantity) or 0
    end
    -- Always set pending — core will detect the gold change even if deposit API returns 0
    pendingDeposit = {
        itemName = resolveItemName(itemLocation),
        quantity = quantity,
        expectedAmount = deposit,
    }
end

function Tracker:OnPostCommodity(itemLocation, duration, quantity, unitPrice)
    local deposit = 0
    if _G.C_AuctionHouse and _G.C_AuctionHouse.CalculateCommodityDeposit then
        local itemID = itemLocation and itemLocation.itemID
            or (_G.C_Item and _G.C_Item.GetItemID and _G.C_Item.GetItemID(itemLocation))
            or 0
        deposit = _G.C_AuctionHouse.CalculateCommodityDeposit(itemID, duration, quantity) or 0
    end
    -- Always set pending
    pendingDeposit = {
        itemName = resolveItemName(itemLocation),
        quantity = quantity,
        expectedAmount = deposit,
    }
end

function Tracker:OnPlaceBid(auctionID, bidAmount)
    pendingPurchase = {
        kind      = "item",
        auctionID = auctionID,
        amount    = bidAmount,
        quantity  = 1,
        itemName  = itemNameCache[auctionID],
    }
end

function Tracker:OnConfirmCommoditiesPurchase(itemID, quantity, unitPrice)
    -- Resolve item name: C_Item.GetItemNameByID → C_Item.GetItemInfo → fallback (like GoldPulse)
    local name = itemNameCache[itemID]
    if not name and _G.C_Item and _G.C_Item.GetItemNameByID then
        name = _G.C_Item.GetItemNameByID(itemID)
    end
    if not name and _G.C_Item and _G.C_Item.GetItemInfo then
        name = _G.C_Item.GetItemInfo(itemID)
    end
    if not name then
        name = "Item " .. tostring(itemID)
    end
    pendingPurchase = {
        kind      = "commodity",
        itemID    = itemID,
        quantity  = quantity,
        unitPrice = unitPrice,
        itemName  = name,
    }
end

-------------------------------------------------------------------------------
-- Lifecycle: subscribe to core's gold change notification + register hooks
-------------------------------------------------------------------------------
function Tracker:OnInitialize()
    pendingPurchase = nil
    pendingDeposit  = nil
    itemNameCache   = {}

    local GL = ns.GoldLedger
    if not GL then return end

    -- Subscribe to core Tracker's gold change callback (amender pattern)
    local coreTracker = GL.GetModule and GL:GetModule("Tracker")
    if coreTracker and coreTracker.OnGoldChanged then
        coreTracker:OnGoldChanged(function(amount, entryType, total, source)
            Tracker:OnCoreGoldChanged(amount, entryType, total, source)
        end)
    end

    -- C_AuctionHouse hooks for context (best-effort, set pending state only)
    if _G.C_AuctionHouse and _G.hooksecurefunc then
        local ah = _G.C_AuctionHouse
        if ah.PostItem then
            hooksecurefunc(ah, "PostItem", function(...) Tracker:OnPostItem(...) end)
        end
        if ah.PostCommodity then
            hooksecurefunc(ah, "PostCommodity", function(...) Tracker:OnPostCommodity(...) end)
        end
        if ah.PlaceBid then
            hooksecurefunc(ah, "PlaceBid", function(...) Tracker:OnPlaceBid(...) end)
        end
        if ah.ConfirmCommoditiesPurchase then
            hooksecurefunc(ah, "ConfirmCommoditiesPurchase", function(...) Tracker:OnConfirmCommoditiesPurchase(...) end)
        end
    end
end
