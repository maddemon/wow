-- 购买执行：commodity 按总量一次性两段式购买；非 commodity 按单价升序逐条 PlaceBid。
-- 汇总：成功/失败数量与总花费，输出到聊天框。
-- 非 commodity 采用事件驱动：每条等待 ITEM_PURCHASED 事件推进，2 秒超时计失败。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config

local Log = Config.Log   -- 诊断日志（/ahsnipe log 输出）

AHSnipe.BuyQueue = {}
local BuyQueue = AHSnipe.BuyQueue

local BID_TIMEOUT = 2           -- 单条 PlaceBid 事件响应超时（秒）
local COMMODITY_TIMEOUT = 10    -- commodity 购买价格响应超时（秒）

-- 汇总状态（非 nil 表示购买进行中，作为并发 guard）
local summary = nil              -- { bought, failed, spent, isCommodity }
local commodityPending = nil     -- { itemID, quantity, expectedTotal }
local itemQueue = nil            -- 待购买 listing 数组（按 buyoutAmount 升序）
local itemIndex = 0
local currentItemID = nil        -- 非 commodity 购买的目标物品ID（事件归因）
local pendingEntry = nil         -- 当前等待事件的条目
local bidTimer = nil             -- 单条超时计时器
local commodityTimer = nil       -- commodity 购买超时计时器

local function FinishSummary()
    local s = summary
    summary = nil
    commodityPending = nil
    itemQueue = nil
    itemIndex = 0
    currentItemID = nil
    pendingEntry = nil
    if bidTimer then bidTimer:Cancel(); bidTimer = nil end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    if not s then return end
    Log(("购买结束：成功%d 失败%d 花费%.2fg"):format(s.bought, s.failed, (s.spent or 0) / 10000))
    local coin = C_CurrencyInfo.GetCoinTextureString(s.spent or 0)
    print(("|cff33FF99AHSnipe|r: 购买完成，成功 %d 条，失败 %d 条，共花费 %s")
        :format(s.bought, s.failed, coin))
    if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.OnBuyFinished then
        AHSnipe.SnipeWindow.OnBuyFinished(s)
    end
end

------------------------------------------------------------------------
-- commodity：两段式购买
-- StartCommoditiesPurchase → 等 COMMODITY_PRICE_UPDATED → ConfirmCommoditiesPurchase
------------------------------------------------------------------------
function BuyQueue.BuyCommodity(itemID, totalQuantity)
    if summary then return false end   -- 已有购买进行中
    summary = { bought = 0, failed = 0, spent = 0, isCommodity = true }
    commodityPending = { itemID = itemID, quantity = totalQuantity }
    Log(("购买开始 commodity %s x%d"):format(itemID, totalQuantity))
    C_AuctionHouse.StartCommoditiesPurchase(itemID, totalQuantity)
    -- 超时兜底：价格响应超时视为失败（结束时统一取消，防止残留计时器打断下一次购买）
    commodityTimer = C_Timer.NewTimer(COMMODITY_TIMEOUT, function()
        if commodityPending and commodityPending.itemID == itemID then
            Log(("购买超时 commodity %s（未收到价格响应/结果事件）"):format(itemID))
            summary.failed = 1
            FinishSummary()
        end
    end)
    return true
end

------------------------------------------------------------------------
-- 非 commodity：事件驱动逐条 PlaceBid（一口价购买）
------------------------------------------------------------------------
local function BuyNextItem()
    if not summary or not itemQueue then return end
    if itemIndex > #itemQueue then
        return FinishSummary()  -- 全部处理完
    end
    local entry = itemQueue[itemIndex]
    pendingEntry = entry
    Log(("PlaceBid auctionID=%s 金额%.2fg"):format(entry.auctionID, entry.buyoutAmount / 10000))
    bidTimer = C_Timer.NewTimer(BID_TIMEOUT, function()
        -- 超时：该条计为失败（已被买走/金币不足等），继续下一条
        Log(("PlaceBid 超时 auctionID=%s（无 ITEM_PURCHASED 回包）"):format(entry.auctionID))
        summary.failed = summary.failed + 1
        pendingEntry = nil
        bidTimer = nil
        itemIndex = itemIndex + 1
        BuyNextItem()
    end)
    C_AuctionHouse.PlaceBid(entry.auctionID, entry.buyoutAmount)
end

-- hit: Scanner 产出的命中对象（matched 每条含 unitPrice/quantity/auctionID/buyoutAmount）
function BuyQueue.BuyItems(hit)
    if summary then return false end   -- 已有购买进行中
    summary = { bought = 0, failed = 0, spent = 0, isCommodity = false }
    itemQueue = {}
    for _, l in ipairs(hit.matched) do
        if l.auctionID and l.buyoutAmount then
            itemQueue[#itemQueue + 1] = { auctionID = l.auctionID, buyoutAmount = l.buyoutAmount }
        end
    end
    table.sort(itemQueue, function(a, b) return a.buyoutAmount < b.buyoutAmount end)
    currentItemID = hit.itemID
    Log(("购买开始 item %s 待购%d条/命中%d条"):format(hit.itemID, #itemQueue, #hit.matched))
    if #itemQueue == 0 then
        summary.failed = #hit.matched
        return FinishSummary()
    end
    itemIndex = 1
    BuyNextItem()
    return true
end

-- 统一入口：按物品是否 commodity 分发
function BuyQueue.Buy(hit)
    if summary then
        Log("购买被拒绝：已有购买进行中")
        print("|cffFF4040AHSnipe|r: 已有购买进行中，请稍候。")
        return false
    end
    Log(("购买入口 item=%s commodity=%s 节流就绪=%s"):format(
        hit.itemID, tostring(hit.isCommodity), tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    print("|cff33FF99AHSnipe|r: 正在购买，请稍候…")
    if hit.isCommodity then
        return BuyQueue.BuyCommodity(hit.itemID, hit.totalQuantity)
    end
    return BuyQueue.BuyItems(hit)
end

------------------------------------------------------------------------
-- 事件
------------------------------------------------------------------------
local function OnEvent(self, event, ...)
    if event == "COMMODITY_PRICE_UPDATED" then
        if commodityPending then
            local unitPrice, totalPrice = ...
            commodityPending.expectedTotal = totalPrice
            Log(("价格响应 单价%.2fg 总价%.2fg"):format(unitPrice / 10000, totalPrice / 10000))
            -- 拿到实际价格后确认购买
            C_AuctionHouse.ConfirmCommoditiesPurchase(commodityPending.itemID, commodityPending.quantity)
            Log(("已确认购买 %s x%d"):format(commodityPending.itemID, commodityPending.quantity))
        end
    elseif event == "COMMODITY_PRICE_UNAVAILABLE" then
        if commodityPending then
            Log(("价格不可用 commodity %s（可能已被买走或下架）"):format(commodityPending.itemID))
            summary.failed = 1
            FinishSummary()
        end
    elseif event == "COMMODITY_PURCHASE_SUCCEEDED" then
        if commodityPending then
            Log(("购买成功 commodity %s x%d"):format(commodityPending.itemID, commodityPending.quantity))
            summary.bought = 1
            summary.spent = commodityPending.expectedTotal or 0
            FinishSummary()
        end
    elseif event == "COMMODITY_PURCHASE_FAILED" then
        if commodityPending then
            Log(("购买失败 commodity %s"):format(commodityPending.itemID))
            summary.failed = 1
            FinishSummary()
        end
    elseif event == "ITEM_PURCHASED" then
        local payload = ...
        -- 兼容 payload 为 itemID(number) 或 itemLink(string) 两种形式
        local itemID = payload
        if type(payload) == "string" then
            itemID = tonumber(payload:match("item:(%d+)"))
        end
        -- 仅接受当前非 commodity 购买目标物品的回包，其余（手动购买等）忽略
        if summary and not summary.isCommodity
            and pendingEntry and itemID and itemID == currentItemID then
            if bidTimer then bidTimer:Cancel(); bidTimer = nil end
            Log(("购买成功 item %s auctionID=%s"):format(itemID, pendingEntry.auctionID))
            summary.bought = summary.bought + 1
            summary.spent = summary.spent + (pendingEntry.buyoutAmount or 0)
            pendingEntry = nil
            itemIndex = itemIndex + 1
            C_Timer.After(0.1, BuyNextItem)
        end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        -- AH 关闭：中止未完成的购买上下文
        if summary then
            FinishSummary()
        end
    end
end

function BuyQueue.Init()
    local f = CreateFrame("Frame", "AHSnipeBuyFrame")
    f:RegisterEvent("COMMODITY_PRICE_UPDATED")
    f:RegisterEvent("COMMODITY_PRICE_UNAVAILABLE")
    f:RegisterEvent("COMMODITY_PURCHASE_SUCCEEDED")
    f:RegisterEvent("COMMODITY_PURCHASE_FAILED")
    f:RegisterEvent("ITEM_PURCHASED")
    f:RegisterEvent("AUCTION_HOUSE_CLOSED")
    f:SetScript("OnEvent", OnEvent)
    BuyQueue.frame = f
end
