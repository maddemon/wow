-- 扫描调度状态机：AH 开启期间串行遍历监控物品，异步事件驱动，命中评估。
-- 仅查询一口价 listing；commodity 与非 commodity 走不同查询路径。
-- 超时/静默保护：单物品查询 10 秒未完成则跳过；commodity 分批结果静默 2 秒视为到齐。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local PC = AHSnipe.PriceCluster

AHSnipe.Scanner = {}
local Scanner = AHSnipe.Scanner

local ITEM_TIMEOUT = 10          -- 单物品查询超时（秒）
local BATCH_SILENCE = 2          -- commodity 分批结果静默间隔（秒）
local ITEM_GAP = 1               -- 物品切换间隔（秒）

local enabled = false            -- AH 窗口是否打开（扫描总开关）
local running = false            -- 当前是否正在一轮扫描中
local roundTimer = nil           -- 轮次间隔计时器
local itemTimer = nil            -- 单物品超时计时器
local silenceTimer = nil         -- commodity 静默计时器
local waitItemID = nil           -- 当前等待结果的物品ID

-- 每轮扫描的上下文
local queue = nil                -- 本轮待扫描的物品ID数组
local queueIndex = 0
local hits = nil                 -- 本轮命中的物品列表

local commodityCache = {}        -- [itemID] = true/false（是否 commodity）

local roundGen = 0               -- 轮次代际：ScanNow/Stop 时递增，屏蔽未跟踪的旧轮回调

------------------------------------------------------------------------
-- 计时器管理
------------------------------------------------------------------------
local function ClearTimers()
    if itemTimer then itemTimer:Cancel(); itemTimer = nil end
    if silenceTimer then silenceTimer:Cancel(); silenceTimer = nil end
    waitItemID = nil
end

------------------------------------------------------------------------
-- 门槛计算：手动值优先，否则自动（主簇最低单价 × 折扣）
------------------------------------------------------------------------
local function GetThreshold(itemID, listings)
    local it = Config.GetItem(itemID)
    if not it then return nil end
    if it.threshold then return it.threshold end
    return PC.GetThreshold(listings, Config.GetDiscount())
end

-- 从结果数组收集命中：单价 ≤ 门槛的 listing。
-- 返回 hits（含 threshold、isBaitLike）、主簇最低价（诱饵判定用）。
local function EvaluateListings(itemID, listings, threshold)
    local mainMin = nil
    local main = PC.GetMainCluster(PC.BuildClusters(listings))
    if main then mainMin = main.minPrice end

    local matched = {}
    for _, l in ipairs(listings) do
        if l.unitPrice <= threshold then
            matched[#matched + 1] = l
        end
    end
    if #matched == 0 then return nil end

    local totalQuantity = 0
    local anyBait = false
    for _, l in ipairs(matched) do
        totalQuantity = totalQuantity + l.quantity
        if mainMin and PC.IsBaitLike(l, mainMin) then
            anyBait = true
        end
    end
    return {
        itemID = itemID,
        name = Config.GetItemDisplayName(itemID),
        threshold = threshold,
        isCommodity = commodityCache[itemID] and true or false,
        matched = matched,
        totalQuantity = totalQuantity,
        totalPrice = 0,  -- 由调用方填充（单价 × 数量求和）
        isBaitLike = anyBait,
    }
end

------------------------------------------------------------------------
-- 结果读取
------------------------------------------------------------------------
-- 读取 commodity 全部结果，构造 { unitPrice, quantity } 列表。
local function CollectCommodityListings(itemID)
    local listings = {}
    local n = C_AuctionHouse.GetNumCommoditySearchResults(itemID) or 0
    for i = 1, n do
        local info = C_AuctionHouse.GetCommoditySearchResultInfo(itemID, i)
        if info and info.unitPrice and info.unitPrice > 0 then
            listings[#listings + 1] = { unitPrice = info.unitPrice, quantity = info.quantity }
        end
    end
    return listings
end

-- 读取非 commodity 搜索结果，仅保留有效一口价（含 auctionID/buyoutAmount 供购买用）。
-- unitPrice 不取整，避免 floor 偏低导致门槛比较偏差；总价展示侧另行计算。
local function CollectItemListings(itemKey)
    local listings = {}
    local n = C_AuctionHouse.GetNumItemSearchResults(itemKey) or 0
    for i = 1, n do
        local info = C_AuctionHouse.GetItemSearchResultInfo(itemKey, i)
        if info and info.buyoutAmount and info.buyoutAmount > 0 and info.quantity and info.quantity > 0 then
            listings[#listings + 1] = {
                unitPrice = info.buyoutAmount / info.quantity,
                quantity = info.quantity,
                auctionID = info.auctionID,
                buyoutAmount = info.buyoutAmount,
            }
        end
    end
    return listings
end

------------------------------------------------------------------------
-- 单物品查询
------------------------------------------------------------------------
local function FinishItem(itemID, listings)
    ClearTimers()
    if listings and #listings > 0 then
        local threshold = GetThreshold(itemID, listings)
        if threshold then
            -- 记录最低单价快照，供配置面板价格列显示
            local minUnit = nil
            for _, l in ipairs(listings) do
                if not minUnit or l.unitPrice < minUnit then
                    minUnit = l.unitPrice
                end
            end
            Config.SetLastPrice(itemID, minUnit, threshold)
            if AHSnipe.ConfigPanel and AHSnipe.ConfigPanel.UpdatePrices then
                AHSnipe.ConfigPanel.UpdatePrices(itemID)
            end

            local hit = EvaluateListings(itemID, listings, threshold)
            if hit then
                local total = 0
                for _, l in ipairs(hit.matched) do
                    total = total + l.unitPrice * l.quantity
                end
                hit.totalPrice = total
                hits[#hits + 1] = hit
                Config.Log(("命中 %s 单价%.2fg 门槛%.2fg 数量%d 总价%.2fg"):format(
                    Config.GetItemDisplayName(itemID), minUnit / 10000, threshold / 10000,
                    hit.totalQuantity, total / 10000))
            end
        end
    end
    queueIndex = queueIndex + 1
    if queueIndex <= #queue then
        local gen = roundGen     -- 代际快照：ScanNow 打断后旧轮间隙回调失效
        C_Timer.After(ITEM_GAP, function()
            if enabled and gen == roundGen then Scanner.QueryNext() end
        end)
    else
        Scanner.EndRound()
    end
end

local function QueryCommodity(itemID)
    waitItemID = itemID
    C_AuctionHouse.RefreshCommoditySearchResults(itemID)
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        -- 超时：读取已有数据，跳过该物品
        FinishItem(itemID, CollectCommodityListings(itemID))
    end)
    silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
        -- 静默：结果到齐，读取
        if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            FinishItem(itemID, CollectCommodityListings(itemID))
        end
    end)
end

local function QueryItem(itemID)
    waitItemID = itemID
    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    -- sorts 传空表使用默认排序，结果读取侧自行排序处理
    C_AuctionHouse.SendSearchQuery(itemKey, {}, false)
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        FinishItem(itemID, CollectItemListings(itemKey))
    end)
end

function Scanner.QueryNext()
    if not enabled then return end
    if queueIndex > #queue then return Scanner.EndRound() end

    -- 节流闸门未就绪：等待 THROTTLED_SYSTEM_READY 后重试
    if not C_AuctionHouse.IsThrottledMessageSystemReady() then
        local gen = roundGen     -- 代际快照：ScanNow 打断后旧轮重试失效
        C_Timer.After(2, function()
            if enabled and gen == roundGen then Scanner.QueryNext() end
        end)
        return
    end

    local itemID = queue[queueIndex]
    local cached = commodityCache[itemID]
    if cached == nil then
        -- 未知是否 commodity：先查物品信息（异步，事件回调后重试）
        local itemKey = C_AuctionHouse.MakeItemKey(itemID)
        local info = C_AuctionHouse.GetItemKeyInfo(itemKey)
        if info then
            commodityCache[itemID] = info.isCommodity and true or false
        else
            -- 等待 ITEM_KEY_ITEM_INFO_RECEIVED，超时兜底按非 commodity
            waitItemID = itemID
            itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
                commodityCache[itemID] = false
                Scanner.QueryNext()
            end)
            return
        end
    end

    if commodityCache[itemID] then
        QueryCommodity(itemID)
    else
        QueryItem(itemID)
    end
end

------------------------------------------------------------------------
-- 轮次管理
------------------------------------------------------------------------
local function PickBestHit()
    -- 多物品命中时只取门槛单价最高的
    local best = nil
    for _, h in ipairs(hits) do
        if not best or h.threshold > best.threshold then
            best = h
        end
    end
    return best
end

function Scanner.EndRound()
    running = false
    ClearTimers()
    local best = PickBestHit()
    if best and enabled then
        AHSnipe.SnipeWindow.Show(best)
    end
    -- 安排下一轮（先取消旧计时器，防止手动触发残留）
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    if enabled then
        roundTimer = C_Timer.NewTimer(Config.GetInterval(), function()
            if enabled then Scanner.ScanRound() end
        end)
    end
end

function Scanner.ScanRound()
    if not enabled or running then return end
    running = true
    hits = {}
    queue = {}
    for _, it in ipairs(Config.GetItems()) do
        if it.enabled then
            queue[#queue + 1] = it.itemID
        end
    end
    queueIndex = 1
    if #queue == 0 then
        return Scanner.EndRound()
    end
    Scanner.QueryNext()
end

------------------------------------------------------------------------
-- 开关
------------------------------------------------------------------------
function Scanner.Start()
    if enabled then return end
    enabled = true
    Scanner.ScanRound()
end

function Scanner.Stop()
    roundGen = roundGen + 1      -- 屏蔽未跟踪的旧轮回调（间隙/节流重试）
    enabled = false
    running = false
    ClearTimers()
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    queue = nil
    hits = nil
end

-- 手动扫描：立即触发一轮，并重置轮次计时器（自动扫描从本次手动起重新计时）
function Scanner.ScanNow()
    if not enabled then return false end
    roundGen = roundGen + 1      -- 屏蔽旧轮的间隙/节流重试回调，防止与新轮次并发
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    running = false          -- 打断进行中的轮次，重新开始
    ClearTimers()
    queue = nil
    hits = nil
    Scanner.ScanRound()
    return true
end

function Scanner.IsEnabled()
    return enabled
end

------------------------------------------------------------------------
-- 事件
------------------------------------------------------------------------
local function OnEvent(self, event, ...)
    if event == "COMMODITY_SEARCH_RESULTS_ADDED" then
        local itemID = ...
        if not enabled or itemID ~= waitItemID then return end
        -- 每批到货后重置静默计时器
        if silenceTimer then silenceTimer:Cancel() end
        silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
            if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
                FinishItem(itemID, CollectCommodityListings(itemID))
            end
        end)
        -- 已到齐则立即结束
        if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            FinishItem(itemID, CollectCommodityListings(itemID))
        end
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        local itemKey = ...
        if not enabled or not itemKey or itemKey.itemID ~= waitItemID then return end
        if itemTimer then
            itemTimer:Cancel(); itemTimer = nil
            FinishItem(itemKey.itemID, CollectItemListings(itemKey))
        end
    elseif event == "ITEM_KEY_ITEM_INFO_RECEIVED" then
        local itemID = ...
        if not enabled or itemID ~= waitItemID then return end
        local itemKey = C_AuctionHouse.MakeItemKey(itemID)
        local info = C_AuctionHouse.GetItemKeyInfo(itemKey)
        if info then
            commodityCache[itemID] = info.isCommodity and true or false
            if itemTimer then itemTimer:Cancel(); itemTimer = nil end
            waitItemID = nil
            Scanner.QueryNext()
        end
    end
end

function Scanner.Init()
    local f = CreateFrame("Frame", "AHSnipeScannerFrame")
    f:RegisterEvent("COMMODITY_SEARCH_RESULTS_ADDED")
    f:RegisterEvent("ITEM_SEARCH_RESULTS_UPDATED")
    f:RegisterEvent("ITEM_KEY_ITEM_INFO_RECEIVED")
    f:SetScript("OnEvent", OnEvent)
    Scanner.frame = f
end
