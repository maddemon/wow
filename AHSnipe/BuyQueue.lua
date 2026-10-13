-- 购买执行：commodity 按总量一次性两段式购买；非 commodity 按单价升序逐条 PlaceBid。
-- 汇总：成功/失败数量与总花费，输出到聊天框与弹窗。

local AHSnipe = AHSnipe

AHSnipe.BuyQueue = {}
local BuyQueue = AHSnipe.BuyQueue

local ITEM_GAP = 0.3             -- 非 commodity 逐条购买间隔（秒）

-- 汇总状态
local summary = nil              -- { bought=成功数, failed=失败数, spent=花费铜, isCommodity }

local function PrintSummary()
    if not summary then return end
    local s = summary
    summary = nil
    local coin = C_CurrencyInfo.GetCoinTextureString(s.spent or 0)
    print(("|cff33FF99AHSnipe|r: 购买完成，成功 %d 条，失败 %d 条，共花费 %s"):format(s.bought, s.failed, coin))
    if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.OnBuyFinished then
        AHSnipe.SnipeWindow.OnBuyFinished(s)
    end
end

------------------------------------------------------------------------
-- commodity：两段式购买
-- StartCommoditiesPurchase → 等 COMMODITY_PRICE_UPDATED → ConfirmCommoditiesPurchase
-- 等待事件确认成交；失败事件计入失败并汇总。
------------------------------------------------------------------------
local commodityPending = nil     -- { itemID, quantity }

function BuyQueue.BuyCommodity(itemID, totalQuantity)
    if commodityPending then return end
    summary = { bought = 0, failed = 0, spent = 0, isCommodity = true }
    commodityPending = { itemID = itemID, quantity = totalQuantity }
    C_AuctionHouse.StartCommoditiesPurchase(itemID, totalQuantity)
    -- 超时兜底：10 秒无价格响应视为失败
    C_Timer.After(10, function()
        if commodityPending then
            commodityPending = nil
            summary.failed = 1
            PrintSummary()
        end
    end)
end

------------------------------------------------------------------------
-- 非 commodity：按单价升序逐条 PlaceBid（一口价购买）
------------------------------------------------------------------------
local itemQueue = nil            -- 待购买 listing 数组（已按单价升序）
local itemIndex = 0

local function BuyNextItem()
    if not itemQueue or itemIndex > #itemQueue then
        -- 全部处理完，汇总
        local s = summary
        summary = nil
        itemQueue = nil
        itemIndex = 0
        if s then
            local coin = C_CurrencyInfo.GetCoinTextureString(s.spent or 0)
            print(("|cff33FF99AHSnipe|r: 购买完成，成功 %d 条，失败 %d 条，共花费 %s"):format(s.bought, s.failed, coin))
            if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.OnBuyFinished then
                AHSnipe.SnipeWindow.OnBuyFinished(s)
            end
        end
        return
    end
    local entry = itemQueue[itemIndex]
    -- PlaceBid 对一口价 auctionID 传 buyoutAmount 即直接购买
    C_AuctionHouse.PlaceBid(entry.auctionID, entry.buyoutAmount)
    itemIndex = itemIndex + 1
    C_Timer.After(ITEM_GAP, BuyNextItem)
end

-- hit: Scanner 产出的命中对象（matched 里每条含 unitPrice/quantity/auctionID/buyoutAmount）
function BuyQueue.BuyItems(hit)
    if itemQueue then return end  -- 已有购买进行中
    summary = { bought = 0, failed = 0, spent = 0, isCommodity = false }
    itemQueue = {}
    for _, l in ipairs(hit.matched) do
        if l.auctionID and l.buyoutAmount then
            itemQueue[#itemQueue + 1] = { auctionID = l.auctionID, buyoutAmount = l.buyoutAmount }
        end
    end
    table.sort(itemQueue, function(a, b) return a.buyoutAmount < b.buyoutAmount end)
    if #itemQueue == 0 then
        summary.failed = #hit.matched
        return PrintSummary()
    end
    itemIndex = 1
    BuyNextItem()
end

-- 供统一入口：按物品是否 commodity 分发
function BuyQueue.Buy(hit)
    if hit.isCommodity then
        BuyQueue.BuyCommodity(hit.itemID, hit.totalQuantity)
    else
        BuyQueue.BuyItems(hit)
    end
end

------------------------------------------------------------------------
-- 事件
------------------------------------------------------------------------
local function OnEvent(self, event, ...)
    if event == "COMMODITY_PRICE_UPDATED" and commodityPending then
        local unitPrice, totalPrice = ...
        -- 拿到实际价格后确认购买
        C_AuctionHouse.ConfirmCommoditiesPurchase(commodityPending.itemID, commodityPending.quantity)
        commodityPending.expectedTotal = totalPrice
    elseif event == "COMMODITY_PURCHASE_SUCCEEDED" and commodityPending then
        summary.bought = 1
        summary.spent = commodityPending.expectedTotal or 0
        commodityPending = nil
        PrintSummary()
    elseif event == "COMMODITY_PURCHASE_FAILED" and commodityPending then
        summary.failed = 1
        commodityPending = nil
        PrintSummary()
    elseif event == "ITEM_PURCHASED" then
        local itemID = ...
        if summary and not summary.isCommodity then
            summary.bought = summary.bought + 1
            -- 花费累加对应条目
            local entry = itemQueue and itemQueue[itemIndex - 1]
            if entry then
                summary.spent = summary.spent + entry.buyoutAmount
            end
        end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        -- AH 关闭：中止未完成的购买上下文
        commodityPending = nil
        itemQueue = nil
        summary = nil
    end
end

function BuyQueue.Init()
    local f = CreateFrame("Frame", "AHSnipeBuyFrame")
    f:RegisterEvent("COMMODITY_PRICE_UPDATED")
    f:RegisterEvent("COMMODITY_PURCHASE_SUCCEEDED")
    f:RegisterEvent("COMMODITY_PURCHASE_FAILED")
    f:RegisterEvent("ITEM_PURCHASED")
    f:RegisterEvent("AUCTION_HOUSE_CLOSED")
    f:SetScript("OnEvent", OnEvent)
    BuyQueue.frame = f
end
