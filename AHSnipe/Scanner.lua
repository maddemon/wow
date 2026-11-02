local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local PC = AHSnipe.PriceCluster
local Log = Config.Log

AHSnipe.Scanner = {}
local Scanner = AHSnipe.Scanner

local ITEM_TIMEOUT = 10
local BATCH_SILENCE = 2
local ITEM_GAP = 0.1
local IDLE_GAP = 2        -- 无启用物品时的最小轮间歇：空转一轮也要跑统计/收尾，0.2s 的频率纯属白费
local MAX_IN_FLIGHT = 2
local FOCUS_DURATION = 20
local FOCUS_GAP = 1
local FOCUS_MISS_LIMIT = 3
local STARVE_LIMIT = 15
-- 盲窗：连续 BLIND_LIMIT 秒没有任何查询拿到答复（节流闸门关闭、购买独占通道、暂停都算）。
-- 实测闸门一堵就是 5~17 秒（2026-10-21 19:12：闸门阻塞101次/16.5s，聚焦补查断档13s，
-- 最久未查17.0s；闸门关闭时的重试本身是 5 秒一跳）。盲窗期间看到的行是"还没被证伪"，
-- 不是"刚被证实"——诱饵实测活不到 2 秒，所以盲窗之后必须重新答复过才许下单。
local BLIND_LIMIT = 4
-- 购买在途时扫描一条都不发（BUY_RETRY 只是"购买结束了没"的复查节拍，不是发送节流）。
-- 实测 2026-10-21 21:35:11 那枪 x1：报价 3 秒没回来被判超时，而同一秒起扫描在 12/13/14 连拿
-- 3 条答复（每条回包 0.1s）——槽位全被自己吃掉了。旁证：紧接着 x2 的总价 21.88 = 10.89+10.99，
-- 说明那 1 个 @10.89 到 21:35:15 还在，x1 那一枪根本没成交，纯粹白等。
-- x11 那枪更极端：确认后 15 秒（21:35:23→38）通道一条 READY 都不给，回来已是 FAILED。
-- 这段静默本来就是盲窗（盲窗内的行点不了），所以扫描在购买期间产出的答案没有任何用途。
local BUY_RETRY = 0.5

local enabled = false
local ahOpen = false
local running = false
local manualOnce = false
local roundGen = 0
local roundTimer, pumpTimer, gateTimer, infoTimer, kickTimer, buyTimer
local infoPending = nil
local waitingForGate = false
local gateWaitSince = nil
local nextSendAt = 0
local requests = {}
local inFlight = 0
local refreshJob = nil
local ourQuery = false
local pendingKick = false
local focus = {}
local focusSince = 0
local lastQueried = {}      -- [itemID] = 最近一次发送查询的时刻；整会话累积、从不清除（判据见 QueriedThisRound）
local lastAnswerAt = 0     -- 最近一次"查询拿到答复"的时刻（超时/失败不算答复）
local blindFrom = 0        -- 最近一段盲窗的起点：此刻之前落下的行证据一律作废
local commodityCache = {}
local queue = nil
local queueIndex = 0
local roundStartTime = 0
local metrics = nil

local function EnsureMetrics()
    if not metrics then
        metrics = {
            since = GetTime(), sent = 0, completed = 0, skipped = 0,
            peak = 0, noID = 0, rounds = 0, roundTime = 0,
            gateWaits = 0, gateTime = 0, latencySum = 0, latencyMax = 0, silent = 0,
            buyHolds = 0,
        }
    end
    return metrics
end

local function ReportMetrics(force)
    if not metrics then return end
    local elapsed = GetTime() - metrics.since
    if not force and elapsed < 30 then return end
    if metrics.sent > 0 then
        -- 三种"慢"分开记：闸门卡（服务器节流）、回包慢（网络/服务器）、静默收尾（结果迟迟不完整）。
        -- 不分开就只会看到吞吐低，却不知道该动 ITEM_GAP 还是该动 MAX_IN_FLIGHT。
        Log(("扫描统计 %.1fs：发出%d 完成%d 超时/失败%d 峰值在途%d 无编号信号%d 平均轮耗时%.2fs 每分钟完成%.1f 闸门阻塞%d次/%.1fs 平均回包%.2fs 峰值回包%.2fs 静默收尾%d 购买让路%d次"):format(
            elapsed, metrics.sent, metrics.completed, metrics.skipped, metrics.peak, metrics.noID,
            metrics.rounds > 0 and metrics.roundTime / metrics.rounds or 0,
            elapsed > 0 and metrics.completed * 60 / elapsed or 0,
            metrics.gateWaits, metrics.gateTime,
            metrics.completed > 0 and metrics.latencySum / metrics.completed or 0,
            metrics.latencyMax, metrics.silent, metrics.buyHolds))
    end
    metrics = nil
end

local function IsActive()
    return ahOpen and (enabled or manualOnce)
end

-- 购买在途（从点击到结局事件/超时收尾）：summary 存在期间 BuyQueue 独占购买通道
local function BuyPending()
    local BQ = AHSnipe.BuyQueue
    return (BQ and BQ.IsBusy and BQ.IsBusy()) and true or false
end

local function IsCurrent(req)
    return IsActive() and req.gen == roundGen and requests[req.itemID] == req
end

local function ClearRequest(req)
    if req.itemTimer then req.itemTimer:Cancel(); req.itemTimer = nil end
    if req.silenceTimer then req.silenceTimer:Cancel(); req.silenceTimer = nil end
    if requests[req.itemID] == req then
        requests[req.itemID] = nil
        inFlight = inFlight - 1
    end
end

local function ClearInfo()
    if infoTimer then infoTimer:Cancel(); infoTimer = nil end
    infoPending = nil
end

local function ClearGate()
    if gateTimer then gateTimer:Cancel(); gateTimer = nil end
    waitingForGate = false
end

local function ClearBuy()
    if buyTimer then buyTimer:Cancel(); buyTimer = nil end
end

local function ResetWork()
    roundGen = roundGen + 1
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    if pumpTimer then pumpTimer:Cancel(); pumpTimer = nil end
    if kickTimer then kickTimer:Cancel(); kickTimer = nil end
    ClearGate()
    ClearBuy()
    gateWaitSince = nil
    ClearInfo()
    for _, req in pairs(requests) do
        if req.itemTimer then req.itemTimer:Cancel() end
        if req.silenceTimer then req.silenceTimer:Cancel() end
    end
    requests = {}
    inFlight = 0
    queue = nil
    running = false
    pendingKick = false
    local job = refreshJob
    refreshJob = nil
    return job
end

local function EvaluateListings(itemID, listings, threshold, mainMin)
    local matched = {}
    local totalQuantity, totalPrice, anyBait = 0, 0, false
    -- 门槛以上最近一档的单价：均价否决时要靠它反推"服务器那一刻低价还剩几个"。
    -- 不能用主簇最低价代替：低价与主簇之间若还有缓冲档，用主簇价反推会把存量高估好几倍
    local spill = nil
    for _, l in ipairs(listings) do
        if l.unitPrice <= threshold then
            matched[#matched + 1] = l
            totalQuantity = totalQuantity + l.quantity
            totalPrice = totalPrice + l.unitPrice * l.quantity
            if mainMin and PC.IsBaitLike(l, mainMin) then
                anyBait = true
            end
        elseif not spill or l.unitPrice < spill then
            spill = l.unitPrice
        end
    end
    if #matched == 0 then return nil end
    return {
        itemID = itemID,
        name = Config.GetItemDisplayName(itemID),
        threshold = threshold,
        marketUnitPrice = mainMin,
        isCommodity = commodityCache[itemID] and true or false,
        matched = matched,
        totalQuantity = totalQuantity,
        totalPrice = totalPrice,
        isBaitLike = anyBait,
        spillPrice = spill,
    }
end

local blindSig = {}
local function LogBlind(itemID, minUnit, threshold, main, listings)
    local allQty = 0
    for _, l in ipairs(listings) do allQty = allQty + l.quantity end
    local sig = table.concat({ minUnit or 0, threshold, main.minPrice,
        main.totalQuantity, allQty, #listings }, "/")
    if blindSig[itemID] == sig then return end
    blindSig[itemID] = sig
    Log(("无命中 %s：最低%.2fg 门槛%.2fg 主簇%.2fg x%d（占%d%%，%d条挂单）"
        .."——主簇=数量最多的价格簇，门槛=主簇最低价×折扣"):format(
        Config.GetItemDisplayName(itemID),
        (minUnit or 0) / 10000, threshold / 10000,
        main.minPrice / 10000, main.totalQuantity,
        allQty > 0 and math.floor(main.totalQuantity * 100 / allQty) or 0,
        #listings))
end

local function EvaluateHit(itemID, listings, req)
    if not Config.GetItem(itemID) then return nil end
    local clusters = PC.BuildClusters(listings)
    local main = PC.GetMainCluster(clusters)
    if not main then return nil end
    local threshold = main.minPrice * Config.GetDiscount()
    -- clusters 按升序切分（BuildClusters 先排序），首簇最低价即全局最低单价，不必再整表扫一遍
    local minUnit = clusters[1].minPrice

    local hit = EvaluateListings(itemID, listings, threshold, main.minPrice)
    if not hit then
        LogBlind(itemID, minUnit, threshold, main, listings)
        return nil
    end
    blindSig[itemID] = nil
    local seen = GetTime()
    hit.roundStartAt = req.roundStartAt
    hit.querySendAt = req.querySendAt
    hit.seenAt = seen
    Log(("命中 %s 单价%.2fg 门槛%.2fg 数量%d 总价%.2fg 主簇%.2fg x%d（本轮起%.1fs 回包%.1fs）"):format(
        Config.GetItemDisplayName(itemID), minUnit / 10000, threshold / 10000,
        hit.totalQuantity, hit.totalPrice / 10000, main.minPrice / 10000, main.totalQuantity,
        req.querySendAt - req.roundStartAt, seen - req.querySendAt))
    return hit
end

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

-- 一次答复到账：距上一次答复超过 BLIND_LIMIT 秒即判为断流，盲窗起点记成"检出断流的当下"。
-- 不能记成上一次答复的时刻：那一行的 seenAt 是在本函数之后才取的 GetTime，同一帧里总要晚那么
-- 几十微秒，于是"盲窗前最后答复的那一行"恒通过 List.Proven —— 断流 5~17 秒后按钮仍可凭 15 秒前
-- 的旧数字点亮（诱饵实测活不到 2 秒，违反"盲窗后必须被重新答复过才许下单"）。
-- 记成当下之后：盲窗前的行一律不通过；正是这次答复让我发现断流的那一行 seenAt 略晚于起点，仍算证据。
local function NoteAnswer()
    local now = GetTime()
    if lastAnswerAt > 0 and now - lastAnswerAt >= BLIND_LIMIT then
        blindFrom = now
    end
    lastAnswerAt = now
end

local function FinishItem(req, listings, timedOut)
    if not IsCurrent(req) then return end
    ClearRequest(req)
    local stats = EnsureMetrics()
    if timedOut then
        stats.skipped = stats.skipped + 1
    else
        stats.completed = stats.completed + 1
        local latency = GetTime() - req.querySendAt
        stats.latencySum = stats.latencySum + latency
        if latency > stats.latencyMax then stats.latencyMax = latency end
        if req.bySilence then stats.silent = stats.silent + 1 end
        NoteAnswer()
    end
    local itemID = req.itemID
    if req.refresh and refreshJob == req.refresh then
        local job = refreshJob
        refreshJob = nil
        local fresh, reason
        if timedOut then
            reason = "查询超时（未收到最新结果）"
        elseif not listings or #listings == 0 then
            reason = "诱饵已被买走或下架"
        else
            fresh = EvaluateHit(itemID, listings, req)
            if not fresh then reason = "价格已回涨，已无低于门槛的挂单" end
        end
        job.callback(fresh, reason)
        if enabled and req.gen == roundGen then
            if running then Scanner.QueryNext() else Scanner.ScanRound() end
        end
        return
    end

    if timedOut then
        -- 超时也要盖盲窗起点：blindFrom 原本只有两个写点（NoteAnswer=答复到账、Pause=通道确定停了），
        -- 于是"闸门一直不开/整轮全部超时"这段正在进行的静默永远没有起点，静默前那条行无限期保持
        -- Proven=true——60 秒前的旧数字照样能点亮【立即购买】（不变量1被绕过，FRESH_GATE 只管排序不管按钮）。
        -- 口径：断流=问了没答。这条查询是本轮自己发的（购买前刷新 req.refresh 已在上面返回，
        -- 那是用户点击引发的一次性查询，它的超时不代表通道断流；扫描器压根没在发的情况由 Pause 负责）。
        -- 起点取超时到账的当下，与 NoteAnswer 同一口径（都是检出时刻）：记成上一次答复的时刻，
        -- 那条行的 seenAt 在同一帧里总比它晚几十微秒，旧行照样恒通过 Proven。
        -- 实测盲窗=断流 5~17 秒、诱饵活不到 2 秒，所以从这一刻起先前落下的行一律要重新答复过才算证据。
        -- 发送当场报错（下面那个 pcall 失败也走 timedOut 分支）同样盖戳：那条查询确实没拿到答复，
        -- 盖了只会让按钮更保守，不会多点一枪。
        blindFrom = GetTime()
        Log(("%s 查询超时，本轮跳过（%s）"):format(itemID,
            req.freshSeen and "结果未完整" or "结果事件未回，旧缓存已按门禁跳过不读"))
    elseif listings and #listings > 0 then
        local hit = EvaluateHit(itemID, listings, req)
        if hit then
            focus[itemID] = { expire = GetTime() + FOCUS_DURATION, miss = 0 }
            if focusSince == 0 then focusSince = GetTime() end
            Log(("命中 %s：进入/延续聚焦窗口 %.0fs"):format(itemID, FOCUS_DURATION))
            AHSnipe.SnipeList.Add(hit)
        else
            AHSnipe.SnipeList.MarkGone(itemID)
            local f = focus[itemID]
            if f then
                f.miss = (f.miss or 0) + 1
                if f.miss >= FOCUS_MISS_LIMIT then
                    focus[itemID] = nil
                    Log(("聚焦退出 %s：连续 %d 次无诱饵价"):format(itemID, f.miss))
                end
            end
        end
    else
        AHSnipe.SnipeList.MarkGone(itemID)
    end
    if req.gen == roundGen then Scanner.QueryNext() end
end

local function MarkFresh(req, src)
    if not IsCurrent(req) or req.freshSeen then return end
    req.freshSeen = true
    if req.staleBlocked then
        Log(("迟到回包 %s（距发送%.1fs，来源%s）：先前静默到点时读到的是上一轮快照，已拦截"):format(
            req.itemID, GetTime() - req.querySendAt, src))
    end
end

local function TryFinishCommodity(req, src, canRefresh)
    if not IsCurrent(req) then return end
    if not C_AuctionHouse.HasFullCommoditySearchResults(req.itemID) then return end
    if not req.freshSeen then
        if not req.staleBlocked then
            req.staleBlocked = true
            Log(("旧缓存拦截 %s（距发送%.1fs仍无带物品编号回包，来源%s，不读缓存）"):format(
                req.itemID, GetTime() - req.querySendAt, src))
        end
        return
    end
    -- 购买回调只能由可归属的结果事件触发，不能落到计时器或无编号事件栈。
    if req.refresh and not canRefresh then return end
    if src == "静默" then req.bySilence = true end
    FinishItem(req, CollectCommodityListings(req.itemID))
end

local function ArmSilence(req)
    if req.silenceTimer then req.silenceTimer:Cancel() end
    req.silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
        if not IsCurrent(req) then return end
        req.silenceTimer = nil
        TryFinishCommodity(req, "静默", false)
    end)
end

local function SendQuery(itemID, job)
    local now = GetTime()
    local stats = EnsureMetrics()
    -- 闸门等待时长在真正发出的这一刻结算：READY 事件可能早于本地间隔到点
    if gateWaitSince then
        stats.gateTime = stats.gateTime + (now - gateWaitSince)
        gateWaitSince = nil
    end
    local req = {
        itemID = itemID, gen = roundGen,
        isCommodity = commodityCache[itemID],
        roundStartAt = job and job.startedAt or roundStartTime,
        querySendAt = now, freshSeen = false, staleBlocked = false,
        refresh = job,
    }
    requests[itemID] = req
    inFlight = inFlight + 1
    stats.sent = stats.sent + 1
    stats.peak = math.max(stats.peak, inFlight)
    if job then job.request = req else queueIndex = queueIndex + 1 end
    lastQueried[itemID] = now
    nextSendAt = now + ITEM_GAP
    req.itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        if IsCurrent(req) then FinishItem(req, nil, true) end
    end)
    if req.isCommodity then ArmSilence(req) end

    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    ourQuery = true
    local ok, err
    if req.isCommodity and C_AuctionHouse.HasSearchResults(itemKey) then
        ok, err = pcall(C_AuctionHouse.RefreshCommoditySearchResults, itemID)
    else
        ok, err = pcall(C_AuctionHouse.SendSearchQuery, itemKey, {}, false)
    end
    ourQuery = false
    if not ok and IsCurrent(req) then
        Log(("查询发送失败 %s：%s"):format(itemID, tostring(err)))
        FinishItem(req, nil, true)
    end
    if req.gen == roundGen then Scanner.QueryNext() end
end

function Scanner.QueryNext()
    if not IsActive() then return end
    if pumpTimer then pumpTimer:Cancel(); pumpTimer = nil end
    -- 购买在途就让路：这段时间的槽位全部留给购买自己的报价/确认（见 BUY_RETRY 注释）。
    if BuyPending() then
        ClearGate()
        local stats = EnsureMetrics()
        if not buyTimer then
            stats.buyHolds = stats.buyHolds + 1
            local gen = roundGen
            buyTimer = C_Timer.NewTimer(BUY_RETRY, function()
                if gen ~= roundGen then return end
                buyTimer = nil
                Scanner.QueryNext()
            end)
        end
        return
    end
    local itemID, job
    if refreshJob then
        if refreshJob.request then return end
        job = refreshJob
        itemID = job.itemID
    else
        if not running or not queue then return end
        if queueIndex > #queue then
            if inFlight == 0 and not infoPending then Scanner.EndRound() end
            return
        end
        itemID = queue[queueIndex]
    end
    if inFlight >= MAX_IN_FLIGHT or requests[itemID] or infoPending then return end

    local gen = roundGen
    local delay = nextSendAt - GetTime()
    if delay > 0 then
        pumpTimer = C_Timer.NewTimer(delay, function()
            if gen ~= roundGen then return end
            pumpTimer = nil
            Scanner.QueryNext()
        end)
        return
    end
    if not C_AuctionHouse.IsThrottledMessageSystemReady() then
        if not gateWaitSince then
            gateWaitSince = GetTime()
            local stats = EnsureMetrics()
            stats.gateWaits = stats.gateWaits + 1
        end
        waitingForGate = true
        if not gateTimer then
            gateTimer = C_Timer.NewTimer(5, function()
                if gen ~= roundGen then return end
                gateTimer = nil
                Scanner.QueryNext()
            end)
        end
        return
    end
    ClearGate()

    if commodityCache[itemID] == nil then
        local itemKey = C_AuctionHouse.MakeItemKey(itemID)
        local info = C_AuctionHouse.GetItemKeyInfo(itemKey)
        if info then
            commodityCache[itemID] = info.isCommodity and true or false
        else
            infoPending = itemID
            infoTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
                if gen ~= roundGen or infoPending ~= itemID then return end
                ClearInfo()
                commodityCache[itemID] = false
                Scanner.QueryNext()
            end)
            return
        end
    end
    SendQuery(itemID, job)
end

function Scanner.EndRound()
    if not running or inFlight > 0 or infoPending or refreshJob then return end
    running = false
    local stats = EnsureMetrics()
    stats.rounds = stats.rounds + 1
    stats.roundTime = stats.roundTime + GetTime() - roundStartTime
    ReportMetrics(false)
    ClearGate()
    if pumpTimer then pumpTimer:Cancel(); pumpTimer = nil end
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    if manualOnce then manualOnce = false; return end
    if not enabled then return end
    if pendingKick then
        pendingKick = false
        Scanner.ScanRound()
        return
    end
    local gap
    if next(focus) ~= nil then
        gap = FOCUS_GAP
    elseif not queue or #queue == 0 then
        -- 一个可查物品都没有：轮次本身就是空转，用 IDLE_GAP 兜底（用户设置更长时听用户的）
        gap = math.max(Config.GetInterval(), IDLE_GAP)
    else
        gap = Config.GetInterval()
        if gap <= 0 then gap = 0.2 end
    end
    local gen = roundGen
    roundTimer = C_Timer.NewTimer(gap, function()
        if enabled and gen == roundGen then
            roundTimer = nil
            Scanner.ScanRound()
        end
    end)
end

function Scanner.BuildRoundQueue(items, focusSet, queried, now, starveLimit)
    local queue = {}
    local stats = { focus = 0, refill = 0, oldest = 0 }
    for _, it in ipairs(items or {}) do
        if it.enabled and focusSet[it.itemID] then
            queue[#queue + 1] = it.itemID
            stats.focus = stats.focus + 1
        end
    end
    local focusMode = stats.focus > 0
    for _, it in ipairs(items or {}) do
        if it.enabled and not focusSet[it.itemID] then
            local at = queried[it.itemID]
            local age = at and (now - at) or starveLimit
            if age > stats.oldest then stats.oldest = age end
            if not focusMode or age >= starveLimit then
                queue[#queue + 1] = it.itemID
                stats.refill = stats.refill + 1
            end
        end
    end
    return queue, stats
end

function Scanner.ScanRound()
    if not IsActive() or running or refreshJob then return end
    running = true
    EnsureMetrics()
    roundStartTime = GetTime()
    local now = GetTime()
    for id, f in pairs(focus) do
        if f.expire <= now or not Config.GetItem(id) or not Config.GetItem(id).enabled then
            focus[id] = nil
        end
    end
    -- 监控已移除/停用的物品，列表里残留的行要一起清掉（残留行还能被【立即购买】点到）
    if AHSnipe.SnipeList and AHSnipe.SnipeList.PruneUnmonitored then
        AHSnipe.SnipeList.PruneUnmonitored()
    end
    local stats
    queue, stats = Scanner.BuildRoundQueue(Config.GetItems(), focus, lastQueried, now, STARVE_LIMIT)
    if stats.focus > 0 then
        if focusSince == 0 then focusSince = now end
        Log(("聚焦%d个+补查%d个（最久未查%.1fs，聚焦已持续%.0fs）"):format(
            stats.focus, stats.refill, stats.oldest, now - focusSince))
    else
        focusSince = 0
    end
    queueIndex = 1
    Scanner.QueryNext()
end

function Scanner.Pause()
    enabled = false
    manualOnce = false
    -- 通道确定停了，不等"下次答复才检出断流"：暂停的当下就是盲窗起点。
    -- 同样不能记最后一次答复的时刻——那一行的 seenAt 比它略晚（同一帧后取的 GetTime），
    -- 会被判成"暂停后被重新答复过"，暂停期间留下的旧行照样能点亮【立即购买】。
    blindFrom = GetTime()
    lastAnswerAt = 0
    ReportMetrics(true)
    local job = ResetWork()
    focus = {}
    focusSince = 0
    if job then job.callback(nil, "查询超时（未收到最新结果）") end
end

function Scanner.Start()
    ahOpen = true
    if Config.IsPaused() then return end
    Scanner.Resume()
end

function Scanner.Stop()
    ahOpen = false
    Scanner.Pause()
end

function Scanner.Resume()
    if enabled or not ahOpen or Config.IsPaused() then return end
    enabled = true
    manualOnce = false
    Scanner.ScanRound()
end

function Scanner.SetPaused(paused)
    Config.SetPaused(paused)
    if paused then
        Log("扫描已暂停")
        Scanner.Pause()
    else
        Log("扫描已恢复")
        Scanner.Resume()
    end
    return Config.IsPaused()
end

function Scanner.IsPaused()
    return Config.IsPaused()
end

function Scanner.ScanNow()
    if not ahOpen then
        Log("手动扫描：拍卖行未打开，忽略")
        return false
    end
    if not enabled and not Config.IsPaused() then
        Log("手动扫描：扫描未启动（非暂停态），忽略")
        return false
    end
    local job = ResetWork()
    focus = {}
    focusSince = 0
    manualOnce = not enabled
    if job then job.callback(nil, "查询超时（未收到最新结果）") end
    Scanner.ScanRound()
    return true
end

function Scanner.RefreshHit(hit, callback)
    if AHSnipe.BuyQueue and AHSnipe.BuyQueue.IsBusy() then
        callback(nil, "已有购买进行中，请稍候")
        return
    end
    if not enabled then callback(nil, "扫描未运行") return end
    if refreshJob then callback(nil, "已有刷新进行中，请稍候") return end
    local itemID = hit.itemID
    if commodityCache[itemID] == nil then
        callback(nil, "物品类型未知")
        return
    end
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    ClearInfo()
    Log(("购买前刷新 %s"):format(itemID))
    refreshJob = { itemID = itemID, callback = callback, startedAt = GetTime() }
    local req = requests[itemID]
    -- 回包没有请求编号，复用同物品的在途查询，避免两份响应无法区分。
    if req then
        req.refresh = refreshJob
        refreshJob.request = req
    end
    Scanner.QueryNext()
end

function Scanner.Kick()
    if not enabled or kickTimer then return end
    local gen = roundGen
    kickTimer = C_Timer.NewTimer(0.5, function()
        if gen ~= roundGen then return end
        kickTimer = nil
        if not enabled then return end
        if running or refreshJob then
            pendingKick = true
        else
            Scanner.ScanNow()
        end
    end)
end

function Scanner.IsEnabled()
    return enabled
end

-- 刚买过的那件最可能有续单，也最需要新证据：把它续进聚焦（BuildRoundQueue 排最前、轮间 1 秒）并当场起一轮。
-- 不走 Kick——Kick 的 0.5s 定时器最终调 ScanNow，那是"用户自己在翻拍卖行"的全量重扫，会清空聚焦，
-- 刚挂上去的复查目标会被冲掉。购买在途会把节流通道占住（实测断流 5~17 秒），
-- 所以本轮还在跑时只登记 pendingKick（收尾后走 ScanRound，聚焦保留）。
function Scanner.Watch(itemID)
    if not itemID then return false end
    local it = Config.GetItem(itemID)
    if not it or not it.enabled then return false end
    focus[itemID] = { expire = GetTime() + FOCUS_DURATION, miss = 0 }
    if focusSince == 0 then focusSince = GetTime() end
    Log(("购后复查 %s：续聚焦，下一轮第一条就是它"):format(itemID))
    if running or refreshJob then
        pendingKick = true
    else
        Scanner.ScanRound()
    end
    return true
end

-- 盲窗起点：此刻之前的行证据没有经过重新答复证实，不许点亮【立即购买】（见 BLIND_LIMIT）
function Scanner.BlindFrom()
    return blindFrom
end

-- 用户浏览（非本插件查询）要不要踢一轮：只排除"本轮已发出的那几次查询"的回包/迟到回包。
-- 判据不能写成 lastQueried[itemID] 的键存在性——那张表记的是每次发送查询的时刻、整会话从不清除，
-- 键存在 = "这件曾被扫描查过"，玩家之后浏览它就再也不踢轮（补轮永久失效，实测是功能回退）；
-- 拿发送时刻跟本轮起点比，才是"这一次是不是本轮在途/刚回的那一份"。
local function QueriedThisRound(itemID)
    local at = lastQueried[itemID]
    return at ~= nil and at >= roundStartTime
end

local function OnEvent(self, event, ...)
    if not IsActive() then return end
    if event == "COMMODITY_SEARCH_RESULTS_ADDED" or event == "COMMODITY_SEARCH_RESULTS_UPDATED" then
        local itemID = ...
        local req = requests[itemID]
        if req and req.isCommodity then
            MarkFresh(req, event)
            ArmSilence(req)
            TryFinishCommodity(req, event, true)
        elseif event == "COMMODITY_SEARCH_RESULTS_UPDATED" and itemID
            and not ourQuery and not QueriedThisRound(itemID) then
            Scanner.Kick()
        end
    elseif event == "COMMODITY_SEARCH_RESULTS_RECEIVED" then
        if metrics then metrics.noID = metrics.noID + 1 end
        -- 无编号信号可能来自已完成或手动查询，不能给任何请求赋予新鲜度。
        local ready = {}
        for _, req in pairs(requests) do
            if req.isCommodity and req.freshSeen then ready[#ready + 1] = req end
        end
        for _, req in ipairs(ready) do TryFinishCommodity(req, "RECEIVED", false) end
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        local itemKey = ...
        if not itemKey then return end
        local req = requests[itemKey.itemID]
        if req and not req.isCommodity then
            MarkFresh(req, "ITEM_UPDATED")
            FinishItem(req, CollectItemListings(itemKey))
        elseif not ourQuery and not QueriedThisRound(itemKey.itemID) then
            Scanner.Kick()
        end
    elseif event == "AUCTION_HOUSE_THROTTLED_SYSTEM_READY" then
        if waitingForGate then ClearGate() end
        Scanner.QueryNext()
    elseif event == "ITEM_KEY_ITEM_INFO_RECEIVED" then
        local itemID = ...
        if itemID ~= infoPending then return end
        local info = C_AuctionHouse.GetItemKeyInfo(C_AuctionHouse.MakeItemKey(itemID))
        if info then
            commodityCache[itemID] = info.isCommodity and true or false
            ClearInfo()
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
    f:RegisterEvent("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    f:RegisterEvent("ITEM_KEY_ITEM_INFO_RECEIVED")
    f:SetScript("OnEvent", OnEvent)
    Scanner.frame = f
end
