-- 购买执行：一次点击一个请求。commodity 走两段式（StartCommoditiesPurchase → 报价均价门槛复核 →
-- ConfirmCommoditiesPurchase），请求数量 = 队首物品全部低于门槛档位的合计（列表合并，见 SnipeList.MergedHit）；
-- 非 commodity 对单条 auctionID 走 PlaceBid。
-- 任何结局（成交、均价否决、价格不可用、超时、服务器失败）都立即收尾并回调列表删掉本次请求覆盖的那批行——
-- 剩余挂单由 ≤1~2 秒后的扫描用当前数据重新入列，再由用户点击。
-- 唯一例外：均价否决且能反推出低价存量时，那一档的行不删、数量改写成存量（服务器白送的真实库存），
-- 让下一次点击要的量落在存量以内，不再重复一次注定被否决的请求（见 CheapSurvivors）。
-- 已废弃：按挂单分块串行购买（块推进的发送栈全部实证被丢弃，见下方发送时机说明）。
-- 汇总：每笔购买在聊天框报告"购买成功：物品名，单价 * 数量 = 总价"或"购买失败：物品名，失败原因"。

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

-- 汇总状态（非 nil 表示购买进行中，作为并发 guard）
-- summary = { bought, failed, spent, units, isCommodity, itemID, rowKeys, reason }
-- rowKeys 由 SnipeList 的行带来、BuyQueue 只透传（key 的含义只有列表懂），结算时列表据此删掉本次请求覆盖的那批行
local summary = nil
-- commodity 本次请求的计划（nil=无 commodity 购买在进行）：
-- { itemID, threshold, phase, quantity, tiers, scanUnitPrice, unitPrice, totalPrice }
-- phase: "price"=已发起等价格响应；"result"=已确认等购买结果；nil=未在途
-- quantity = 各档位合计，tiers = 参与档位数（日志用：一次请求吃掉了几个价位）
local commodityPlan = nil
local currentItemID = nil        -- 非 commodity 购买的目标物品ID（事件归因）
local pendingEntry = nil         -- 本次 PlaceBid 等待回包的挂单（nil=无在途）
local bidTimer = nil             -- 单条超时计时器
local commodityTimer = nil       -- commodity 单请求超时计时器
-- 购买期间强制开启 AH 事件诊断，结束后还原（无需用户手动 /ahsnipe diag，
-- 保证"发出去没响应"类问题一定能从 /ahsnipe log 看到服务器回了什么）
local diagRestore = nil

local function FinishSummary()
    local s = summary
    summary = nil
    commodityPlan = nil
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
    -- 列表结算联动：无论成交还是失败，本次请求覆盖的那批行都由列表按 rowKeys 删掉——
    -- 快照已证伪的行留在队首只会让用户再点一次失败并占住购买通道。
    if AHSnipe.SnipeList and AHSnipe.SnipeList.OnBuyFinished then
        AHSnipe.SnipeList.OnBuyFinished(s)
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
-- 购买成功事件栈与报价【否决】事件栈除外（见下方三处实证）。
-- 因此：购买 API 一律在 OnClick 或 搜索/报价结果事件回调栈内【直接】调用，
-- 严禁包一层 C_Timer 延迟/轮询等待再发（那正是丢弃的触发条件）。
-- 已废弃的历史形态：按挂单分块串行购买时，"上一块失败/成功 → 下一块发送"无论放在
-- C_Timer.After（计时器栈）、上一块的成功事件栈还是否决事件栈，第 2 块起一律零回包——
-- 日志里"提示数量 N 实际只买到第 1 块"即此因。
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
-- 新纪律（第二十一轮落地形态）：一次点击只有一个请求，因此【任何结局都只收尾这一次】——
-- 不再有"块推进"，也不再从购买生命周期事件栈/否决事件栈外发下一个请求（三处实证必被丢弃：
-- 计时器栈、成功事件栈、否决事件栈）。收尾路径只调 CancelCommoditiesPurchase（清理悬挂会话）
-- 与 FinishSummary（含 SnipeList.OnBuyFinished 的行删除），不外发任何购买消息。
-- 依旧严禁把购买 API 包进 C_Timer 延迟/轮询：那正是丢弃的触发条件。
------------------------------------------------------------------------

------------------------------------------------------------------------
-- commodity：单请求两段式购买
-- StartCommoditiesPurchase → 等 COMMODITY_PRICE_UPDATED →（均价门槛复核）→ ConfirmCommoditiesPurchase
--
-- 请求数量 = 队首物品全部低于门槛档位的合计（合并粒度见 SnipeList.MergedHit）。
-- 服务器把整个请求按"从当前最便宜挂单往上填"定价，只回均价：
--   · 快照没变 → 均价 ≤ 各档最高价 ≤ 门槛，一次点击吃下整件货；
--   · 低价池被抢走一部分 → 缺口按更贵的挂单补齐，均价上抬，超门槛即 Cancel 一票否决（一分不扣）。
-- 所以合并的代价是"这一枪白打"，均价门槛复核保证不会付到门槛以上。
-- 【2026-10-21 修正】旧注释写的"合并量越大越抗稀释"只在 请求量 ≤ 那一刻的真实存量 时成立；
-- 越过这条线是全有全无，不是渐变：均价 ≤ 门槛 等价于 N ≤ s×(溢出档价−低价)/(溢出档价−门槛)
-- （本案 21.73/12.89/14.12 → N ≤ 1.16s，即超出存量 16% 就整单作废，连本来能吃到的低价一起丢）。
-- 实测：扫描报低价 17 个、服务器报价 263.33g 反推只剩 12 个 → x17 被否，12×8.84g≈106g 利润陪葬；
-- 另一次报 6 个、实测只剩 1 个。所以请求量的正确来源是服务器报价反推的 s，不是快照数量。
-- 2026-10-15 20:01 连续四次否决不是合并本身的错，是那份快照当时已过期（逐档点击同样买不到）；
-- 真正的病灶是旧版"一次点击只成交分块第 1 块、剩余推进必被客户端丢弃"，见上方发送时机说明。
------------------------------------------------------------------------

-- 失败收尾（任意栈内都安全：只取消 + 结算，不外发购买消息）
local function CommodityFail(reason)
    local plan = commodityPlan
    if not plan or not summary then return end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    plan.phase = nil
    summary.failed = summary.failed + 1
    summary.reason = reason
    Log(("购买失败 commodity %s x%d：%s"):format(plan.itemID, plan.quantity or 0, reason))
    ReportBuy(false, plan.itemID, plan.unitPrice, plan.quantity, nil, reason)
    FinishSummary()
end

-- 按当前阶段挂超时计时器：报价阶段 3 秒、确认阶段 10 秒（见 COMMODITY_PRICE_TIMEOUT 说明）。
-- 报价到手后要立刻重新挂表——同一个计时器覆盖两阶段的话，报价吃掉的时间会挤压确认阶段。
local function ArmCommodityTimer(plan)
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    local waitingPrice = plan.phase ~= "result"
    local limit = waitingPrice and COMMODITY_PRICE_TIMEOUT or COMMODITY_TIMEOUT
    commodityTimer = C_Timer.NewTimer(limit, function()
        if commodityPlan ~= plan or not plan.phase then return end
        local pricePhase = plan.phase ~= "result"
        Log(("购买超时 commodity %s x%d（未收到%s，距发送%.1fs）"):format(
            plan.itemID, plan.quantity or 0, pricePhase and "价格响应" or "购买结果",
            lastCommoditySendTime > 0 and GetTime() - lastCommoditySendTime or -1))
        if pricePhase then
            SafeCall("CancelCommoditiesPurchase", C_AuctionHouse.CancelCommoditiesPurchase)   -- 清理悬挂的购买会话
        end
        -- 超时回调在计时器栈：从这里外发任何购买消息都必被丢弃，只收尾本次请求
        CommodityFail(pricePhase and "购买超时（未收到价格响应）" or "购买超时（购买结果未确认）")
    end)
end

-- 均价否决白送一个实测值：服务器按"从最便宜挂单往上填"定价，所以
--   总价 = s×低价 + (N−s)×溢出档价  ⟹  s = (N×溢出档价 − 总价)/(溢出档价 − 低价)
-- s 就是报价那一刻低价档还剩几个（2026-10-21 两个样本都反解成整数且与随后的扫描吻合：
-- x17/263.33g → 12，x6/121.54g → 1）。只在单档合并时可反解：多档合并的"低价"不是一个数。
-- 取 floor 不取四舍五入：按 s 下单自带 14% 余量，多要一个就把整枪赌掉。
-- 返回 (可下单数量, 反解原值, 不可反解的原因)：三个槽位固定，调用方按名取值
local function CheapSurvivors(plan, totalPrice)
    local p, spill, n = plan.scanUnitPrice, plan.spillPrice, plan.quantity
    if plan.tiers ~= 1 then return nil, nil, "多档合并，低价不是一个数" end
    if not p or not n or n <= 0 or not totalPrice then return nil, nil, "缺扫描低价或服务器总价" end
    if not spill then return nil, nil, "行上没有门槛以上价格" end
    if spill <= p then return nil, nil, "溢出档单价不高于低价" end
    local raw = (n * spill - totalPrice) / (spill - p)
    if raw >= 1 then return math.floor(raw), raw end
    -- 报价均价 == 溢出档 ⟹ 反解出 0：不是算不出来，是低价池真的一个不剩（2026-10-21 19:12:55 幽影锡）
    if raw >= 0 then return nil, raw, ("低价一个不剩（反解%.2f个：报价均价就等于溢出档价）"):format(raw) end
    return nil, raw, ("反解出负数%.2f个（报价比溢出档还贵，低价池跨了不止两档）"):format(raw)
end

-- 发起本次请求（一个物品 = 一次点击 = 一个合并请求）：plan 就位 → 挂超时 → 直接发送
local function CommodityStart(itemID, listings, threshold, rowKeys, spillPrice)
    local qty, minUnit, minIdx = 0, nil, 1
    for i, l in ipairs(listings) do
        qty = qty + l.quantity
        if not minUnit or l.unitPrice < minUnit then minUnit, minIdx = l.unitPrice, i end
    end
    summary = { bought = 0, failed = 0, spent = 0, units = 0,
                isCommodity = true, itemID = itemID, rowKeys = rowKeys, reason = nil }
    commodityPlan = {
        itemID = itemID, threshold = threshold, quantity = qty, tiers = #listings,
        scanUnitPrice = minUnit, unitPrice = nil, totalPrice = nil, phase = "price",
        spillPrice = spillPrice, rowKey = rowKeys and rowKeys[minIdx],
    }
    local plan = commodityPlan
    Log(("购买开始 commodity %s x%d（%d档合计）门槛%.2fg（扫描最低单价%.2fg）"):format(
        itemID, qty, #listings, (threshold or 0) / 10000, (minUnit or 0) / 10000))
    ArmCommodityTimer(plan)
    -- 直接发送（不得包计时器延迟，见上方发送时机说明）
    if not summary or commodityPlan ~= plan or not plan.phase then return end
    SendCommodityPurchase(itemID, qty)
end

function BuyQueue.BuyCommodity(hit)
    if summary then return false end       -- 客户端只有一个购买会话
    local listings, keys, qty = {}, {}, 0
    for i, l in ipairs(hit.matched or {}) do
        if l.quantity and l.quantity > 0 and l.unitPrice then
            listings[#listings + 1] = l
            -- key 与 listing 成对收集：否决后要靠"最便宜那一档的 key"改写数量，两表下标必须对齐
            keys[#keys + 1] = hit.rowKeys and hit.rowKeys[i]
            qty = qty + l.quantity
        end
    end
    if qty <= 0 then
        summary = { bought = 0, failed = 1, spent = 0, units = 0,
                    isCommodity = true, itemID = hit.itemID, rowKeys = hit.rowKeys,
                    reason = "无有效挂单（缺少数量）" }
        ReportBuy(false, hit.itemID, nil, nil, nil, "无有效挂单（缺少数量）")
        FinishSummary()
        return false
    end
    CommodityStart(hit.itemID, listings, hit.threshold, keys, hit.spillPrice)
    return true
end

------------------------------------------------------------------------
-- 非 commodity：单条 auctionID 的一口价购买（等待 ITEM_PURCHASED 回包，2 秒超时计失败）
------------------------------------------------------------------------
local function ItemFail(reason)
    if bidTimer then bidTimer:Cancel(); bidTimer = nil end
    if not summary then return end
    summary.failed = summary.failed + 1
    summary.reason = reason
    ReportBuy(false, currentItemID, nil, nil, nil, reason)
    pendingEntry = nil
    FinishSummary()
end

-- hit: SnipeList 合成的单条挂单命中（matched[1] 含 auctionID/buyoutAmount）
function BuyQueue.BuyItems(hit)
    if summary then return false end
    local listing = hit.matched and hit.matched[1]
    if not listing or not listing.auctionID or not listing.buyoutAmount then
        summary = { bought = 0, failed = 1, spent = 0, units = 0,
                    isCommodity = false, itemID = hit.itemID, rowKeys = hit.rowKeys,
                    reason = "无有效挂单（缺少 auctionID/一口价）" }
        ReportBuy(false, hit.itemID, nil, nil, nil, "无有效挂单（缺少 auctionID/一口价）")
        FinishSummary()
        return false
    end
    summary = { bought = 0, failed = 0, spent = 0, units = 0,
                isCommodity = false, itemID = hit.itemID, rowKeys = hit.rowKeys }
    currentItemID = hit.itemID
    pendingEntry = listing
    Log(("购买开始 item %s auctionID=%s 金额%.2fg"):format(
        hit.itemID, listing.auctionID, listing.buyoutAmount / 10000))
    bidTimer = C_Timer.NewTimer(BID_TIMEOUT, function()
        if not summary or pendingEntry ~= listing then return end
        Log(("PlaceBid 超时 auctionID=%s（无 ITEM_PURCHASED 回包）"):format(listing.auctionID))
        ItemFail("购买无响应（可能已被买走或金币不足）")   -- 计时器栈只收尾，不重发
    end)
    Log(("已发送 PlaceBid auctionID=%s（闸门就绪=%s）"):format(
        listing.auctionID, tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    SafeCall("PlaceBid", C_AuctionHouse.PlaceBid, listing.auctionID, listing.buyoutAmount)
    return true
end

-- 是否有购买在进行中（购买在途时扫描让路、并不暂停扫描；同时是并发 guard，
-- 供【购买】按钮与手动刷新入口据此给出"已有购买进行中"的准确原因）
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
            Log(("价格响应 x%d 均价%.2fg 总价%.2fg（距发送%.1fs，扫描最低单价%.2fg）"):format(
                plan.quantity or 0, unitPrice / 10000, totalPrice / 10000,
                lastCommoditySendTime > 0 and GetTime() - lastCommoditySendTime or -1,
                (plan.scanUnitPrice or 0) / 10000))
            -- 门槛复核：下单时刻的服务器实际价格可能已高于命中时的价格。
            -- 这是全链路唯一的价格保护（服务器没有限价单），超过门槛一律放弃本次请求。
            local th = plan.threshold
            if th and unitPrice > th then
                SafeCall("CancelCommoditiesPurchase", C_AuctionHouse.CancelCommoditiesPurchase)
                -- 报价超门槛 = 低价池被抢走了一部分，这份快照整体已过期
                -- （2026-10-15 法力百合：快照 20 单位，服务器对 x18 报 6.10g 否决，
                -- 同一秒扫描已显示只剩 7 单位）。合并请求下该件全部参与行的数据都不再成立，
                -- 收尾只取消会话、不再外发任何购买消息（否决事件栈发出必被丢弃）。
                -- 但总价本身能反解出低价还剩几个：把那一档的数量改成它，留在队首等第二次点击，
                -- 比让它出门、等下轮扫描用同一个过期数量再坑一次强（幸存挂单仍由扫描重新入列）。
                local survivors, raw, why = CheapSurvivors(plan, totalPrice)
                if survivors and plan.rowKey then
                    summary.serverQty = survivors
                    summary.serverQtyKey = plan.rowKey
                    Log(("均价否决反推 %s：低价只剩%.2f个（请求%d 均价%.2fg 低价%.2fg 溢出档%.2fg）→ 按%d个重下"):format(
                        plan.itemID, raw, plan.quantity, unitPrice / 10000,
                        plan.scanUnitPrice / 10000, plan.spillPrice / 10000, survivors))
                else
                    Log(("均价否决不可反推 %s：档位%d 溢出档%s（%s）"):format(
                        plan.itemID, plan.tiers or 0,
                        plan.spillPrice and ("%d"):format(plan.spillPrice) or "无",
                        why or "行上没有可改写的档位价"))
                end
                CommodityFail(("实际单价%.2fg 已超门槛%.2fg，放弃购买（价格已回涨）"):format(
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
            CommodityFail("价格不可用（可能已被买走或下架）")
        end
    elseif event == "COMMODITY_PURCHASE_SUCCEEDED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "result" then
            if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
            plan.phase = nil
            Log(("购买成功 commodity %s x%d"):format(plan.itemID, plan.quantity))
            ReportBuy(true, plan.itemID, plan.unitPrice, plan.quantity, plan.totalPrice)
            summary.bought = 1
            summary.spent = summary.spent + (plan.totalPrice or 0)
            summary.units = summary.units + (plan.quantity or 0)
            summary.reason = "购买成交"
            -- 成交即收尾：成功事件栈外发下一条必被丢弃（见发送时机说明），
            -- 同档剩余/相邻档位由下轮扫描带当前数据重新入列
            FinishSummary()
        end
    elseif event == "COMMODITY_PURCHASE_FAILED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "result" then
            -- 购买结果失败事件栈同属购买生命周期栈：只收尾，不外发
            CommodityFail("服务器购买失败")
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
            local entry = pendingEntry
            Log(("购买成功 item %s auctionID=%s"):format(itemID, entry.auctionID))
            ReportBuy(true, currentItemID, entry.unitPrice, entry.quantity, entry.buyoutAmount)
            summary.bought = 1
            summary.spent = summary.spent + (entry.buyoutAmount or 0)
            summary.units = summary.units + (entry.quantity or 0)
            summary.reason = "购买成交"
            pendingEntry = nil
            -- 同上：成功事件栈不再发下一条 PlaceBid，收尾并让本行出门
            FinishSummary()
        end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        -- AH 关闭：中止未完成的购买上下文
        if summary then
            if pendingEntry or commodityPlan then
                ReportBuy(false, currentItemID or (commodityPlan and commodityPlan.itemID),
                    nil, nil, nil, "拍卖行已关闭，购买中断")
                summary.failed = summary.failed + 1
                summary.reason = "拍卖行已关闭，购买中断"
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
