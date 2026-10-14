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
local ITEM_GAP = 0.1             -- 物品切换间隔（秒）：贴节流闸门跑，仅留极小间隙

local enabled = false            -- 扫描是否运行（AH 打开且未暂停）
local ahOpen = false             -- 拍卖行窗口是否打开（与暂停状态解耦）
local running = false            -- 当前是否正在一轮扫描中
local roundTimer = nil           -- 轮次间隔计时器
local itemTimer = nil            -- 单物品超时计时器
local silenceTimer = nil         -- commodity 静默计时器
local waitItemID = nil           -- 当前等待结果的物品ID
local waitingForGate = false     -- 正在等待节流闸门就绪
local gateTimer = nil            -- 闸门等待兜底计时器（事件丢失时自愈）
local lastQueriedItemID = nil    -- 最近查询过的物品ID（过滤自身查询的迟到结果事件）
local ourQuery = false           -- 区分自身查询与用户在 AH 内的手动浏览

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
local function FinishItem(itemID, listings, timedOut)
    -- 防重入：事件可能在查询 API 调用内同步触发（SynchronousEvent），已处理过则忽略
    if waitItemID ~= itemID then return end
    ClearTimers()
    local n = listings and #listings or 0
    if n > 0 then
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
        else
            Config.Log(("%s 有挂单但未算出门槛（自动门槛需挂单≥2且分布正常）"):format(itemID))
        end
    elseif timedOut then
        -- 超时/跳过：结果事件未回，读到 0 条不等于真无挂单，单独标记便于诊断
        Config.Log(("%s 查询超时，本轮跳过（结果事件未回）"):format(itemID))
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
    lastQueriedItemID = itemID   -- 记录自身查询，供结果事件区分用户浏览
    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    -- 先建计时器再发查询：事件可能在查询 API 调用内同步触发，否则计时器被覆盖导致重复 FinishItem
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        -- 超时：读取已有数据，跳过该物品
        FinishItem(itemID, CollectCommodityListings(itemID), true)
    end)
    silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
        -- 静默：结果到齐，读取（校验 waitItemID：防止刷新前的旧缓存被误当作本轮结果）
        if waitItemID == itemID and C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            FinishItem(itemID, CollectCommodityListings(itemID))
        end
    end)
    -- 对齐官方 RefreshSearchResults：RefreshCommoditySearchResults 仅对已有结果缓存的物品生效，
    -- 无缓存时需先 SendSearchQuery 发起完整查询（否则不产生请求 → 10秒超时读 0 条）
    -- ourQuery：标记自身查询，交互触发的 Kick 忽略这些 API 调用
    ourQuery = true
    if C_AuctionHouse.HasSearchResults(itemKey) then
        C_AuctionHouse.RefreshCommoditySearchResults(itemID)
    else
        C_AuctionHouse.SendSearchQuery(itemKey, {}, false)
    end
    ourQuery = false
end

local function QueryItem(itemID)
    waitItemID = itemID
    lastQueriedItemID = itemID   -- 记录自身查询，供结果事件区分用户浏览
    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    -- 先建计时器再发查询（事件可能同步触发）
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        FinishItem(itemID, CollectItemListings(itemKey), true)
    end)
    -- sorts 传空表使用默认排序，结果读取侧自行排序处理
    -- ourQuery：标记自身查询，交互触发的 Kick 忽略这些 API 调用
    ourQuery = true
    C_AuctionHouse.SendSearchQuery(itemKey, {}, false)
    ourQuery = false
end

function Scanner.QueryNext()
    if not enabled then return end
    if queueIndex > #queue then return Scanner.EndRound() end

    -- 节流闸门未就绪：等 AUCTION_HOUSE_THROTTLED_SYSTEM_READY 事件唤醒；
    -- 同时挂低频兜底计时器，事件丢失时自愈
    if not C_AuctionHouse.IsThrottledMessageSystemReady() then
        if not waitingForGate then
            waitingForGate = true
            local gen = roundGen
            gateTimer = C_Timer.NewTimer(5, function()
                if enabled and waitingForGate and gen == roundGen then
                    waitingForGate = false
                    Scanner.QueryNext()
                end
            end)
        end
        return
    end
    waitingForGate = false

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
                -- 超时：清 waitItemID，防止迟到的 ITEM_KEY 事件误取消下一次查询
                if waitItemID == itemID then waitItemID = nil end
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
-- 开关（enabled=扫描运行中，与 ahOpen 解耦：暂停时 AH 打开也不扫描）
------------------------------------------------------------------------
function Scanner.Pause()
    roundGen = roundGen + 1      -- 屏蔽未跟踪的旧轮回调（间隙/节流重试）
    enabled = false
    running = false
    waitingForGate = false       -- 防止闸门事件在暂停后误唤醒
    if gateTimer then gateTimer:Cancel(); gateTimer = nil end
    ClearTimers()
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    queue = nil
    hits = nil
end

function Scanner.Start()
    ahOpen = true
    if Config.IsPaused() then return end   -- 用户暂停期间 AH 打开不扫描
    Scanner.Resume()
end

function Scanner.Stop()
    ahOpen = false
    Scanner.Pause()
end

-- 恢复扫描（需 AH 处于打开状态）
function Scanner.Resume()
    if enabled or not ahOpen then return end
    enabled = true
    Scanner.ScanRound()
end

-- 切换/设置暂停状态（持久化）；恢复后若 AH 打开则立即开始扫描
function Scanner.SetPaused(paused)
    Config.SetPaused(paused)
    if paused then
        Config.Log("扫描已暂停")
        Scanner.Pause()
    else
        Config.Log("扫描已恢复")
        Scanner.Resume()
    end
    return Config.IsPaused()
end

function Scanner.IsPaused()
    return Config.IsPaused()
end

-- 手动扫描：立即触发一轮，并重置轮次计时器（自动扫描从本次手动起重新计时）
function Scanner.ScanNow()
    if not enabled then return false end
    roundGen = roundGen + 1      -- 屏蔽旧轮的间隙/节流重试回调，防止与新轮次并发
    waitingForGate = false       -- 新轮次自行检查闸门，防旧事件误唤醒
    if gateTimer then gateTimer:Cancel(); gateTimer = nil end
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    running = false          -- 打断进行中的轮次，重新开始
    ClearTimers()
    queue = nil
    hits = nil
    Scanner.ScanRound()
    return true
end

-- 交互触发：用户在 AH 内浏览物品（点收藏/搜索）时顺带重启一轮扫描
-- 防抖：浏览列表会连续触发多个结果事件，短窗口内只重启一次
local kickPending = false
function Scanner.Kick()
    if not enabled or kickPending then return end
    kickPending = true
    C_Timer.After(0.5, function()
        kickPending = false
        if not enabled then return end
        -- 扫描进行中不打断（浏览频繁时避免轮次永远无法完成），空闲时重置计时并立即开一轮
        if not running then
            Scanner.ScanNow()
        end
    end)
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
    elseif event == "COMMODITY_SEARCH_RESULTS_RECEIVED" then
        -- 官方同样依赖此事件：结果全部收齐（无 payload，按当前等待物品判断）
        local itemID = waitItemID
        if not enabled or not itemID then return end
        if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            FinishItem(itemID, CollectCommodityListings(itemID))
        end
    elseif event == "COMMODITY_SEARCH_RESULTS_UPDATED" then
        -- 结果集整体更新，按 payload 物品过滤（AH 自身浏览产生的更新不应干扰扫描）
        local itemID = ...
        if not enabled then return end
        if itemID ~= waitItemID then
            -- 用户在 AH 内浏览其他 commodity：顺带触发一轮扫描
            -- 排除自身查询的迟到结果（超时后返回的事件不应绕过轮间隔）
            if not ourQuery and itemID ~= lastQueriedItemID then Scanner.Kick() end
            return
        end
        if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            FinishItem(itemID, CollectCommodityListings(itemID))
        elseif silenceTimer then
            -- 未到齐：重新计静默，让已到齐的判定自然收敛
            silenceTimer:Cancel()
            silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
                if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
                    FinishItem(itemID, CollectCommodityListings(itemID))
                end
            end)
        end
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        local itemKey = ...
        if not enabled or not itemKey then return end
        if itemKey.itemID == waitItemID then
            -- 用 waitItemID 防重入（itemTimer 可能已被同步事件的 ClearTimers 清空）
            FinishItem(itemKey.itemID, CollectItemListings(itemKey))
        elseif not ourQuery and itemKey.itemID ~= lastQueriedItemID then
            -- 用户在 AH 内浏览其他物品（如点击收藏）：顺带触发一轮扫描
            -- 排除自身查询的迟到结果（超时后返回的事件不应绕过轮间隔）
            Scanner.Kick()
        end
    elseif event == "AUCTION_HOUSE_THROTTLED_SYSTEM_READY" then
        -- 节流闸门就绪：恢复等待中的扫描
        if enabled and waitingForGate then
            waitingForGate = false
            if gateTimer then gateTimer:Cancel(); gateTimer = nil end
            Scanner.QueryNext()
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
    f:RegisterEvent("COMMODITY_SEARCH_RESULTS_RECEIVED")
    f:RegisterEvent("COMMODITY_SEARCH_RESULTS_UPDATED")
    f:RegisterEvent("ITEM_SEARCH_RESULTS_UPDATED")
    f:RegisterEvent("ITEM_KEY_ITEM_INFO_RECEIVED")
    f:RegisterEvent("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    f:SetScript("OnEvent", OnEvent)
    Scanner.frame = f
end
