-- 购买执行：commodity 按命中挂单分块两段式购买（每块独立门槛复核；价格不可用→跳过该块，
-- 报价超门槛→否决即收尾，见发送时机说明纪律 2）；非 commodity 按单价升序逐条 PlaceBid。
-- 块/条目成功后不在购买成功事件栈续发下一条（实证必被丢弃）：结束本次购买，
-- 剩余挂单由聚焦扫描下一轮重新弹窗，用户再次点击继续（见发送时机说明）。
-- 汇总：每笔购买在聊天框报告"购买成功：物品名，单价 * 数量 = 总价"或"购买失败：物品名，失败原因"。
-- 非 commodity 采用事件驱动：每条等待 ITEM_PURCHASED 事件推进，2 秒超时计失败。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config

local Log = Config.Log   -- 诊断日志（/ahsnipe log 输出）

AHSnipe.BuyQueue = {}
local BuyQueue = AHSnipe.BuyQueue

local BID_TIMEOUT = 2           -- 单条 PlaceBid 事件响应超时（秒）
local COMMODITY_TIMEOUT = 10    -- commodity 确认购买后等待成交结果事件超时（秒）
-- 报价阶段单独用更短的超时：正常回包实测 0.3—0.5 秒（2026-10-15 法力百合日志 0.5s/0.4s），
-- 最坏记录 2.2 秒，10 秒只是"请求已被客户端丢弃"的死等。死等的真实代价不是等 10 秒本身，
-- 而是这 10 秒里扫描命中全部走"购买进行中，本轮不弹窗"——那次日志里 5—7 个 @2.89g 的便宜货
-- 被扫描每秒看见却压住了 8 秒，最后被别人买光（13:51:47—13:51:55）。
local COMMODITY_PRICE_TIMEOUT = 3
-- CHUNK_GAP 已移除：块推进不再经 C_Timer 延迟，改为在 AH 事件栈内直接发送（见上方发送时机说明）；
-- 块间天然的节流余量由"上块成功事件到达后再发下一块"的网络往返提供，无需人为 0.3s 计时器。

-- 汇总状态（非 nil 表示购买进行中，作为并发 guard）
local summary = nil              -- { bought, failed, spent, units, isCommodity, itemID }
-- commodity 分块计划（nil=无 commodity 购买在进行）：
-- { itemID, threshold, chunks={{quantity,unitPrice}}, index, phase, quantity, unitPrice, totalPrice }
-- phase: "price"=请求已发起等待价格响应；"result"=已确认等待购买结果；nil=块间间隙/未在途
local commodityPlan = nil
local itemQueue = nil            -- 待购买 listing 数组（按 buyoutAmount 升序）
local itemIndex = 0
local currentItemID = nil        -- 非 commodity 购买的目标物品ID（事件归因）
local pendingEntry = nil         -- 当前等待事件的条目
local bidTimer = nil             -- 单条超时计时器
local commodityTimer = nil       -- commodity 单块超时计时器
-- 购买期间强制开启 AH 事件诊断，结束后还原（无需用户手动 /ahsnipe diag，
-- 保证"发出去没响应"类问题一定能从 /ahsnipe log 看到服务器回了什么）
local diagRestore = nil

local function FinishSummary()
    local s = summary
    summary = nil
    commodityPlan = nil
    itemQueue = nil
    itemIndex = 0
    currentItemID = nil
    pendingEntry = nil
    if bidTimer then bidTimer:Cancel(); bidTimer = nil end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    if not s then return end
    -- 还原 AH 事件诊断开关（购买期间强制开启，见 Buy 入口）
    if diagRestore ~= nil and AHSnipe.Config and AHSnipe.Config.SetDiagAH then
        AHSnipe.Config.SetDiagAH(diagRestore)
        diagRestore = nil
    end
    -- 恢复扫描（安全网：正常购买不再暂停扫描，见 Buy 入口注释；
    -- 用户手动暂停/拍卖行关闭时 Resume 内有守护，不会误恢复）
    if AHSnipe.Scanner and AHSnipe.Scanner.Resume then AHSnipe.Scanner.Resume() end
    -- 聊天框逐条报告（ReportBuy）已完成，此处仅留诊断日志
    Log(("购买结束：成功%d 失败%d 数量%d 花费%.2fg"):format(
        s.bought, s.failed, s.units or 0, (s.spent or 0) / 10000))
    -- 有失败或有剩余块/条目未处理：清除该物品的弹窗抑制，允许下一轮扫描立即重新提示
    -- （剩余挂单可能仍可购买，由用户再次点击继续）
    if (s.failed > 0 or s.deferred) and s.itemID
        and AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.ClearSuppression then
        AHSnipe.SnipeWindow.ClearSuppression(s.itemID)
    end
    if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.OnBuyFinished then
        AHSnipe.SnipeWindow.OnBuyFinished(s)
    end
end

-- 聊天框报告单笔购买结果：
-- 成功=购买成功：物品名，单价 * 数量 = 总价（只显示数值，不带"单价/数量/总价"文字标签）；
-- 失败=购买失败：物品名，失败原因
local function ReportBuy(ok, itemID, unitPrice, quantity, totalPrice, reason)
    local name = itemID and Config.GetItemDisplayName(itemID) or "未知物品"
    if ok then
        print(("|cff33FF99AHSnipe|r: |cff33FF99购买成功|r：%s，%s * %d = %s"):format(
            name,
            C_CurrencyInfo.GetCoinTextureString(unitPrice or 0),
            quantity or 0,
            C_CurrencyInfo.GetCoinTextureString(totalPrice or 0)))
    else
        print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(name, reason or "未知原因"))
    end
end

-- 带错误捕获的 AH API 调用：购买 API 在特定客户端状态下可能直接报错，而计时器回调里的
-- Lua 错误会被静默吞掉（scriptErrors 关闭时零提示），表象就是"消息发出后 10 秒无响应"的
-- 假超时（2026-10-15 自动购买连续超时即按此排查）。必须 pcall 并把错误写进诊断日志。
local function SafeCall(desc, fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then
        Log(("AH API 报错 %s：%s"):format(desc, tostring(err)))
    end
    return ok
end

-- commodity 购买请求的发送时刻（GetTime）：价格响应/超时日志据此输出"距发送X.Xs"，
-- 用于精确度量消息投递延迟（时间戳是墙上时钟，无亚秒级精度）
local lastCommoditySendTime = 0

-- 发送购买请求并记录发送时刻（闸门状态一并记录，便于和响应时间对照）。
-- 先 Cancel 再 Start：若此前有被丢弃的购买消息卡住了购买会话（发出去永远等不到报价，
-- 2026-10-15 自动购买连续超时的实锤形态），Cancel 可清掉悬挂会话让新的 Start 被受理；
-- 无会话时 Cancel 是无害的空操作（报错也被 SafeCall 记录）。
local function SendCommodityPurchase(itemID, quantity)
    SafeCall("CancelCommoditiesPurchase(清会话)", C_AuctionHouse.CancelCommoditiesPurchase)
    lastCommoditySendTime = GetTime()
    Log(("已发送 StartCommoditiesPurchase %s x%d（闸门就绪=%s）"):format(
        itemID, quantity, tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    return SafeCall("StartCommoditiesPurchase", C_AuctionHouse.StartCommoditiesPurchase, itemID, quantity)
end

------------------------------------------------------------------------
-- 购买消息的发送时机（2026-10-15 多轮日志实证，改动前必读）：
-- 从计时器（OnUpdate）上下文发出的购买消息会被客户端静默丢弃——10 秒收不到任何
-- 响应事件（价格响应/不可用/失败都没有），pcall 也无报错，消息就是凭空消失。
-- 手动点击（OnClick）与搜索/报价结果事件栈内直接发送都能正常拿到报价（事件栈内约 2 秒）；
-- 购买成功事件栈除外（见下方新纪律）。报价【否决】事件栈同样存疑（见 13:51 证据 + 纪律 2）。
-- 因此：购买 API 一律在 OnClick 或 搜索/报价结果事件回调栈内【直接】调用，
-- 严禁包一层 C_Timer 延迟/轮询等待再发（那正是丢弃的触发条件）。
-- 块推进（上一块失败 → 下一块发送）须在【事件栈内】：先前用 C_Timer.After
-- 推进下一块，致使第 1 块（点击栈内发送）成功、第 2 块起（计时器栈内发送）全部 10s
-- 超时失败——日志里"提示数量 N 实际只买到第 1 块"即此因。
-- 【2026-10-15 11:41 日志新证据】在"购买成功事件栈"（COMMODITY_PURCHASE_SUCCEEDED /
-- ITEM_PURCHASED）内推进下一块【同样被静默丢弃】：第 1 块（点击栈）0.3s 拿到报价成交，
-- 第 2 块（成功事件栈）10s 零回包，计时器重发也零回包（计时器栈必丢），
-- 最终"成功1 失败1"而货明明还挂着（11:41:52 扫描仍命中该挂单）。
-- 购买生命周期事件栈与搜索/报价结果事件栈不是同一种通道：前者不可外发购买消息。
-- 【2026-10-15 13:51 日志新证据】"报价超门槛"的否决事件栈内推进下一块【也零回包】：
-- 第 1 块 x1、第 2 块 x18 均从点击栈发出，0.5s/0.4s 拿到报价；第 3 个请求 x2 从否决
-- 事件栈发出，10 秒内只有 AUCTION_HOUSE_THROTTLED_SYSTEM_READY，没有任何购买响应，最终超时。
-- 三次发送同处一个秒级窗口、同样"闸门就绪=false"，唯一区别就是发出所在的事件栈。
-- 另注：该快照本身已过期——下单用的 20 单位在报价回来的同一秒已被抢成 7 单位
-- （由服务器均价 6.10g 反推：18×(8.13−6.10)/(8.13−2.89)=6.97，与扫描的 7 吻合）。
-- 新纪律：
-- 1. 块/条目【成功】后不在成功事件栈发下一块——结束本次购买并清除弹窗抑制，
--    聚焦扫描下一秒按剩余挂单重新弹窗，由用户再次点击（硬件点击是唯一可靠通道）；
-- 2. 报价超门槛【否决即收尾】，不再推进剩余块：剩余块与当前块同源同份过期快照，
--    基于旧数量再发一次既大概率是死请求，又会把"购买进行中不弹窗"压满超时时长，
--    而 ≤1 秒后的聚焦扫描本来就带着当前数量重新弹窗（比继续啃旧快照更准）；
-- 3. COMMODITY_PRICE_UNAVAILABLE（挂单整体消失）仍在该事件栈内推进下一块——第十三轮记录
--    它当时可用，但按上面 13:51 的证据这条可靠性存疑，若日志再现"UNAVAILABLE 后下一块零回包"
--    就一并改为收尾；
-- 4. 超时（计时器栈）不重发、不推进下一块，直接结束本次购买，剩余交给下次弹窗；
--    购买结果失败事件（COMMODITY_PURCHASE_FAILED）同属生命周期栈，同样失败即收尾。
------------------------------------------------------------------------

------------------------------------------------------------------------
-- commodity：按命中挂单分块的两段式购买
-- StartCommoditiesPurchase → 等 COMMODITY_PRICE_UPDATED → ConfirmCommoditiesPurchase
--
-- 为什么分块：服务器把整个请求按"当前最便宜挂单"定价。低价诱饵簇只要被买走一部分，
-- 仍按扫描时的总量下单就会把剩余数量也拉到市场价，均价超门槛被一票否决——
-- 哪怕还有便宜单位剩着也全军覆没（总量越大，低价簇需完整存活的窗口越长，失败率越高）。
-- 分块后每块独立复核：块越小，请求越只落在最便宜的那批单位上，被抢穿的概率远低于整单
-- （13:51 日志里 x1 那块 0.5s 成交，同一份快照的 x18 被均价拉到 6.10g 否决）。
-- 但否决说明这份快照已经过期，剩余块不再推进——改由 ≤1 秒后的聚焦扫描带当前数量重新弹窗。
------------------------------------------------------------------------

-- 由命中挂单构造分块计划：按单价升序，每块取一条挂单的数量（纯逻辑，可单测）
function BuyQueue.BuildCommodityChunks(matched)
    local chunks = {}
    for _, l in ipairs(matched or {}) do
        if l.quantity and l.quantity > 0 then
            chunks[#chunks + 1] = { quantity = l.quantity, unitPrice = l.unitPrice }
        end
    end
    table.sort(chunks, function(a, b)
        if a.unitPrice == b.unitPrice then return a.quantity < b.quantity end
        return a.unitPrice < b.unitPrice
    end)
    return chunks
end

local CommodityNextChunk   -- 前向声明：CommodityFailChunk 推进下一块需要引用
local CommodityFailChunk   -- 前向声明：报价结果事件回调需要引用
local CommodityFailEnd     -- 前向声明：超时计时器/结果失败事件回调需要引用

-- 按当前阶段挂超时计时器：报价阶段 3 秒、确认阶段 10 秒（见 COMMODITY_PRICE_TIMEOUT 说明）。
-- 报价到手后要立刻重新挂表——同一个计时器覆盖两阶段的话，报价吃掉的时间会挤压确认阶段。
local function ArmCommodityTimer(plan)
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    local waitingPrice = plan.phase ~= "result"
    local limit = waitingPrice and COMMODITY_PRICE_TIMEOUT or COMMODITY_TIMEOUT
    commodityTimer = C_Timer.NewTimer(limit, function()
        if commodityPlan ~= plan or not plan.phase then return end
        local pricePhase = plan.phase ~= "result"
        Log(("购买超时 commodity %s x%d（未收到%s，距发送%.1fs，第%d/%d块）"):format(
            plan.itemID, plan.quantity or 0, pricePhase and "价格响应" or "购买结果",
            lastCommoditySendTime > 0 and GetTime() - lastCommoditySendTime or -1,
            plan.index, #plan.chunks))
        if pricePhase then
            SafeCall("CancelCommoditiesPurchase", C_AuctionHouse.CancelCommoditiesPurchase)   -- 清理悬挂的购买会话
        end
        -- 超时回调在计时器栈：重发/推进下一块都必被丢弃，直接结束本次购买，
        -- 剩余块交给下次弹窗（见发送时机说明新纪律 3）
        CommodityFailEnd(pricePhase and "购买超时（未收到价格响应）" or "购买超时（购买结果未确认）")
    end)
end

-- 发起当前块的购买请求（plan.index 已就位）
local function CommodityStartChunk()
    local plan = commodityPlan
    if not plan or not summary then return end
    if plan.index > #plan.chunks then
        return FinishSummary()  -- 全部块处理完
    end
    local chunk = plan.chunks[plan.index]
    plan.quantity = chunk.quantity
    plan.unitPrice = nil
    plan.totalPrice = nil
    plan.phase = "price"
    Log(("购买开始 commodity %s x%d 门槛%.2fg（第%d/%d块，扫描单价%.2fg）"):format(
        plan.itemID, chunk.quantity, (plan.threshold or 0) / 10000,
        plan.index, #plan.chunks, (chunk.unitPrice or 0) / 10000))
    -- 超时兜底：价格响应/结果事件超时视为该块失败，结束本次购买（剩余交给下次弹窗）
    -- （结束时统一取消，防止残留计时器打断下一次购买）
    ArmCommodityTimer(plan)
    -- 直接发送（不得包计时器延迟，见上方发送时机说明）
    if not summary or commodityPlan ~= plan or not plan.phase then return end
    SendCommodityPurchase(plan.itemID, chunk.quantity)
end

-- 当前块失败收尾（仅限【COMMODITY_PRICE_UNAVAILABLE 事件栈】内调用）：
-- 计数 + 聊天框报告 + 直接在同一事件栈内推进下一块。
-- 报价超门槛不走这里（快照已过期，剩余块照样是基于旧数量的请求，见 OnEvent 否决分支）；
-- 计时器栈超时与购买结果失败事件也不走这里（外发必被丢弃），见 CommodityFailEnd。
function CommodityFailChunk(reason)
    local plan = commodityPlan
    if not plan or not summary then return end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    plan.phase = nil
    summary.failed = summary.failed + 1
    Log(("块失败 %s x%d：%s（第%d/%d块）"):format(
        plan.itemID, plan.quantity or 0, reason, plan.index, #plan.chunks))
    ReportBuy(false, plan.itemID, plan.unitPrice, plan.quantity, nil, reason)
    CommodityNextChunk()
end

-- 失败即收尾（不推进下一块）：用于计时器栈超时与 COMMODITY_PURCHASE_FAILED
-- （购买结果事件栈同属购买生命周期，外发下一块必被丢弃，见发送时机说明新纪律 3）；
-- 结束本次购买，FinishSummary 会清除弹窗抑制，聚焦扫描下一轮按剩余挂单重新弹窗
function CommodityFailEnd(reason)
    local plan = commodityPlan
    if not plan or not summary then return end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    plan.phase = nil
    summary.failed = summary.failed + 1
    local remaining = #plan.chunks - plan.index
    Log(("块失败 %s x%d：%s（第%d/%d块%s）"):format(
        plan.itemID, plan.quantity or 0, reason, plan.index, #plan.chunks,
        remaining > 0 and ("，剩余%d块交给下次弹窗"):format(remaining) or ""))
    ReportBuy(false, plan.itemID, plan.unitPrice, plan.quantity, nil, reason)
    FinishSummary()
end

-- 当前块完成后推进到下一块（或收尾）
function CommodityNextChunk()
    local plan = commodityPlan
    if not plan or not summary then return end
    plan.index = plan.index + 1
    CommodityStartChunk()
end

function BuyQueue.BuyCommodity(hit)
    if summary then return false end   -- 已有购买进行中
    local itemID = hit.itemID
    summary = { bought = 0, failed = 0, spent = 0, units = 0, isCommodity = true, itemID = itemID }
    local chunks = BuyQueue.BuildCommodityChunks(hit.matched)
    if #chunks == 0 then
        ReportBuy(false, itemID, nil, nil, nil, "无有效挂单（缺少数量）")
        summary.failed = 1
        FinishSummary()
        return false
    end
    commodityPlan = {
        itemID = itemID,
        threshold = hit.threshold,
        chunks = chunks,
        index = 1,
        phase = nil,
    }
    local totalQty = 0
    for _, c in ipairs(chunks) do totalQty = totalQty + c.quantity end
    Log(("购买开始 commodity %s 共%d块/%d单位 门槛%.2fg"):format(
        itemID, #chunks, totalQty, (hit.threshold or 0) / 10000))
    CommodityStartChunk()
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
    -- 直接发送（不得包计时器延迟，见上方发送时机说明）
    if not summary or pendingEntry ~= entry then return end
    bidTimer = C_Timer.NewTimer(BID_TIMEOUT, function()
        -- 超时：该条计为失败（已被买走/金币不足等）。回调在计时器栈，
        -- 下一条 PlaceBid 从这里发出必被丢弃——结束本次购买，剩余交给下次弹窗
        Log(("PlaceBid 超时 auctionID=%s（无 ITEM_PURCHASED 回包）"):format(entry.auctionID))
        ReportBuy(false, currentItemID, nil, nil, nil, "购买无响应（可能已被买走或金币不足）")
        summary.failed = summary.failed + 1
        pendingEntry = nil
        bidTimer = nil
        itemIndex = itemIndex + 1
        FinishSummary()
    end)
    Log(("已发送 PlaceBid auctionID=%s（闸门就绪=%s）"):format(
        entry.auctionID, tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    SafeCall("PlaceBid", C_AuctionHouse.PlaceBid, entry.auctionID, entry.buyoutAmount)
end

-- hit: Scanner 产出的命中对象（matched 每条含 unitPrice/quantity/auctionID/buyoutAmount）
function BuyQueue.BuyItems(hit)
    if summary then return false end   -- 已有购买进行中
    summary = { bought = 0, failed = 0, spent = 0, units = 0, isCommodity = false, itemID = hit.itemID }
    itemQueue = {}
    for _, l in ipairs(hit.matched) do
        if l.auctionID and l.buyoutAmount then
            itemQueue[#itemQueue + 1] = {
                auctionID = l.auctionID,
                buyoutAmount = l.buyoutAmount,
                unitPrice = l.unitPrice,
                quantity = l.quantity,
            }
        end
    end
    table.sort(itemQueue, function(a, b) return a.buyoutAmount < b.buyoutAmount end)
    currentItemID = hit.itemID
    Log(("购买开始 item %s 待购%d条/命中%d条"):format(hit.itemID, #itemQueue, #hit.matched))
    if #itemQueue == 0 then
        ReportBuy(false, hit.itemID, nil, nil, nil, "无有效挂单（缺少 auctionID/一口价）")
        summary.failed = #hit.matched
        return FinishSummary()
    end
    itemIndex = 1
    BuyNextItem()
    return true
end

-- 是否有购买在进行中（扫描暂停的常见原因，供刷新入口给出准确原因）
function BuyQueue.IsBusy()
    return summary ~= nil
end

-- 统一入口：按物品是否 commodity 分发
function BuyQueue.Buy(hit)
    if summary then
        Log("购买被拒绝：已有购买进行中")
        print("|cffFF4040AHSnipe|r: 已有购买进行中，请稍候。")
        return false
    end
    -- 注意：购买在途【不要】暂停扫描！AH 消息队列靠持续的查询流量冲出去
    -- （实测：暂停后购买消息躺在节流队列里无人冲，10 秒超时；手动点击能成是因为
    -- 点击输入本身冲一次队列）。保持扫描继续查询，购买消息才能被及时送达。
    -- 购买期间强制开启 AH 事件诊断：自动抓取"服务器回了什么"的直接证据
    if AHSnipe.Config and AHSnipe.Config.IsDiagAH and AHSnipe.Config.SetDiagAH then
        diagRestore = AHSnipe.Config.IsDiagAH()
        AHSnipe.Config.SetDiagAH(true)
    end
    Log(("购买入口 item=%s commodity=%s 节流就绪=%s"):format(
        hit.itemID, tostring(hit.isCommodity), tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    print("|cff33FF99AHSnipe|r: 正在购买，请稍候…")
    if hit.isCommodity then
        return BuyQueue.BuyCommodity(hit)
    end
    return BuyQueue.BuyItems(hit)
end

------------------------------------------------------------------------
-- 事件
------------------------------------------------------------------------
local function OnEvent(self, event, ...)
    if event == "COMMODITY_PRICE_UPDATED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "price" then
            local unitPrice, totalPrice = ...
            plan.unitPrice = unitPrice
            plan.totalPrice = totalPrice
            Log(("价格响应 单价%.2fg 总价%.2fg（距发送%.1fs，第%d/%d块）"):format(
                unitPrice / 10000, totalPrice / 10000,
                lastCommoditySendTime > 0 and GetTime() - lastCommoditySendTime or -1,
                plan.index, #plan.chunks))
            -- 门槛复核：下单时刻的服务器实际价格可能已高于命中时的价格。
            -- 若超过门槛则放弃该块（仅此一块），避免把非钓鱼价买下来。
            local th = plan.threshold
            if th and unitPrice > th then
                SafeCall("CancelCommoditiesPurchase", C_AuctionHouse.CancelCommoditiesPurchase)
                -- 报价超门槛 = 低价簇已被抢走一部分，而本块和后续块用的都是【同一份过期快照】，
                -- 继续推进只会再发一个基于旧数量的请求（2026-10-15 法力百合：快照 20 单位，
                -- 服务器对 x18 报 6.10g 否决，同一秒扫描已显示只剩 7 单位；第 2 块 x2 从否决
                -- 事件栈发出后 10 秒零回包，把可见的便宜货弹窗压到被别人买光）。
                -- 结束本次购买 + 清弹窗抑制，聚焦扫描 1 秒内按当前数量重新弹窗，由点击发送。
                CommodityFailEnd(("实际单价%.2fg 已超门槛%.2fg，放弃购买（价格已回涨）"):format(
                    unitPrice / 10000, th / 10000))
                return
            end
            -- 拿到实际价格后确认购买：切入结果阶段并重新计时（报价已吃掉的时间不该挤压成交等待）
            plan.phase = "result"
            ArmCommodityTimer(plan)
            SafeCall("ConfirmCommoditiesPurchase", C_AuctionHouse.ConfirmCommoditiesPurchase, plan.itemID, plan.quantity)
            Log(("已确认购买 %s x%d"):format(plan.itemID, plan.quantity))
        end
    elseif event == "COMMODITY_PRICE_UNAVAILABLE" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "price" then
            CommodityFailChunk("价格不可用（可能已被买走或下架）")
        end
    elseif event == "COMMODITY_PURCHASE_SUCCEEDED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "result" then
            if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
            plan.phase = nil
            Log(("购买成功 commodity %s x%d（第%d/%d块）"):format(
                plan.itemID, plan.quantity, plan.index, #plan.chunks))
            ReportBuy(true, plan.itemID, plan.unitPrice, plan.quantity, plan.totalPrice)
            summary.bought = summary.bought + 1
            summary.spent = summary.spent + (plan.totalPrice or 0)
            summary.units = summary.units + (plan.quantity or 0)
            if plan.index < #plan.chunks then
                -- 剩余块：不在购买成功事件栈发下一块（2026-10-15 实证必被丢弃，
                -- 见发送时机说明新纪律 1）——结束本次购买并清除抑制，
                -- 聚焦扫描下一轮按剩余挂单重新弹窗，由用户点击继续
                summary.deferred = true
                Log(("已购第%d/%d块，剩余%d块交给下次弹窗"):format(
                    plan.index, #plan.chunks, #plan.chunks - plan.index))
                print(("|cff33FF99AHSnipe|r: 剩余%d块挂单，稍后将重新提示继续购买。"):format(#plan.chunks - plan.index))
                FinishSummary()
            else
                FinishSummary()
            end
        end
    elseif event == "COMMODITY_PURCHASE_FAILED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "result" then
            -- 购买结果失败事件栈同属购买生命周期，不外发下一块：失败即收尾，剩余交给下次弹窗
            CommodityFailEnd("服务器购买失败")
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
            ReportBuy(true, currentItemID, pendingEntry.unitPrice, pendingEntry.quantity, pendingEntry.buyoutAmount)
            summary.bought = summary.bought + 1
            summary.spent = summary.spent + (pendingEntry.buyoutAmount or 0)
            summary.units = summary.units + (pendingEntry.quantity or 0)
            pendingEntry = nil
            itemIndex = itemIndex + 1
            if itemIndex <= #itemQueue then
                -- 剩余条目：不在购买成功事件栈发下一条 PlaceBid（同 commodity 成功栈
                -- 丢弃实证，见发送时机说明新纪律 1）——结束本次购买，
                -- 下一轮扫描带最新 auctionID 重新弹窗
                summary.deferred = true
                Log(("已购%d/%d条，剩余条目交给下次弹窗"):format(itemIndex - 1, #itemQueue))
                print(("|cff33FF99AHSnipe|r: 剩余%d条挂单，稍后将重新提示继续购买。"):format(#itemQueue - itemIndex + 1))
                FinishSummary()
            else
                FinishSummary()
            end
        end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        -- AH 关闭：中止未完成的购买上下文
        if summary then
            if pendingEntry then
                -- 购买中被关闭：当前条目计失败
                ReportBuy(false, currentItemID, nil, nil, nil, "拍卖行已关闭，购买中断")
                summary.failed = summary.failed + 1
            elseif commodityPlan then
                ReportBuy(false, commodityPlan.itemID, nil, nil, nil, "拍卖行已关闭，购买中断")
                summary.failed = summary.failed + 1
            end
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
