-- 扫描调度状态机：AH 开启期间串行遍历监控物品，异步事件驱动，命中评估。
-- 仅查询一口价 listing；commodity 与非 commodity 走不同查询路径。
-- 超时/静默保护：单物品查询 10 秒未完成则跳过；commodity 分批结果静默 2 秒视为到齐。
-- 新鲜度门禁：读结果前要求本次查询收到过结果事件，否则读到的是上一轮缓存（见 TryFinishCommodity）。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local PC = AHSnipe.PriceCluster
local Log = Config.Log   -- 诊断日志（与 BuyQueue/SnipeWindow 一致取别名，勿裸用全局 Log）

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
local querySink = nil            -- 刷新模式：非 nil 时查询结果交给此回调（购买前 RefreshHit），不走轮次评估
-- 新鲜度门禁：HasFullCommoditySearchResults 是客户端缓存状态，上一轮查过的物品本轮
-- 一定仍为 true。回包晚于静默间隔时，"到齐判定"读到的是上一轮的挂单快照，却以最新数据
-- 的身份参与门槛计算与弹窗（用户反馈"提示时已是尾批"的怀疑形态）。因此规定：本次查询
-- 收到过结果事件才允许读结果；没收到就拦截并留痕。
local freshSeen = false          -- 本次查询是否收到过结果事件
local staleBlocked = false       -- 本次查询是否已记过拦截日志（防止每个静默周期刷一条）
local querySendTime = 0          -- 本次查询发出时刻（GetTime），迟到回包日志据此算延迟
local noEventStreak = 0          -- 连续"整次查询没收到任何结果事件"的次数
local freshGateDisabled = false  -- 自愈开关：连续多轮无回包说明门禁依赖的事件形态不成立，退回读缓存

-- 诱饵聚焦：命中（低价诱饵）商品的 itemID → 过期时刻（GetTime 绝对秒）。
-- 命中后 20 秒内高频重扫该商品，抓"同价补货"；WoW 单线程不支持真多线程，
-- 用聚焦集合 + 短轮间歇模拟"专线程关注"，多个诱饵同时出现时各自入集、轮内依次高频扫描。
local focus = {}                -- [itemID] = { expire = 过期时刻, miss = 连续无诱饵价次数 }
local focusSince = 0            -- 本次聚焦段起始时刻（诊断用：聚焦持续越久，其他物品被跳过越久）
local FOCUS_DURATION = 20        -- 单商品聚焦窗口上限（秒）：命中即续期，未命中累计提前退出
local FOCUS_GAP = 1              -- 聚焦模式轮间歇（秒）：窗口内对该商品每秒重扫一次
local FOCUS_MISS_LIMIT = 3       -- 聚焦窗口内连续 N 次"有结果但无便宜货"即提前退出聚焦（约 N 秒）
local manualOnce = false         -- 手动单次扫描标志：暂停态下绕开 enabled 跑一轮，且 EndRound 不续轮

-- 每轮扫描的上下文
local queue = nil                -- 本轮待扫描的物品ID数组
local queueIndex = 0
local roundStartTime = 0         -- 本轮起始时刻（GetTime）：弹窗数据年龄的"排队等待"基准

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
-- 门槛计算：手动门槛已废弃，一律用默认折扣系数（主簇最低单价 × discount）
------------------------------------------------------------------------
-- 从结果数组收集命中：单价 ≤ 门槛的 listing。mainMin = 主簇最低单价（诱饵标注判定用）
local function EvaluateListings(itemID, listings, threshold, mainMin)
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

-- "看得见挂单但一个都不命中"的留痕：这条路径是"手动打开能看到便宜货、插件却没提醒"
-- 唯一的静默出口（门槛由主簇算出，而错价大堆自己成为主簇时，门槛跟着它一起变低）。
-- 按签名去重：市场状态没变就不重复刷，变了（含价格/数量/主簇结构变动）才记一行。
local blindSig = {}
local function LogBlind(itemID, minUnit, threshold, main, listings)
    local allQty = 0
    for _, l in ipairs(listings) do allQty = allQty + l.quantity end
    local sig = table.concat({ minUnit or 0, threshold, main.minPrice,
        main.totalQuantity, allQty, #listings }, "/")
    if blindSig[itemID] == sig then return end
    blindSig[itemID] = sig
    -- 主簇占比是关键判据：占比高 = 错价大堆自己成了主簇，门槛被它自己拖低 → 全都不命中
    Log(("无命中 %s：最低%.2fg 门槛%.2fg 主簇%.2fg x%d（占%d%%，%d条挂单）"
        .."——主簇=数量最多的价格簇，门槛=主簇最低价×折扣"):format(
        Config.GetItemDisplayName(itemID),
        (minUnit or 0) / 10000, threshold / 10000,
        main.minPrice / 10000, main.totalQuantity,
        allQty > 0 and math.floor(main.totalQuantity * 100 / allQty) or 0,
        #listings))
end

------------------------------------------------------------------------
-- 命中评估（轮次 FinishItem 与购买前 RefreshHit 共用）：
-- 算门槛 → 记价格快照/刷面板 → 收集命中。返回 hit 或 nil（无门槛/无命中）
------------------------------------------------------------------------
local function EvaluateHit(itemID, listings)
    if not Config.GetItem(itemID) then return nil end
    local clusters = PC.BuildClusters(listings)
    local main = PC.GetMainCluster(clusters)
    if not main then return nil end
    local threshold = main.minPrice * Config.GetDiscount()

    -- 最低单价仅用于"扫到但看不见便宜货"的静默诊断日志；
    -- 价格快照/价格列随配置面板降级一并删除（价格只在命中行上看）
    local minUnit = nil
    for _, l in ipairs(listings) do
        if not minUnit or l.unitPrice < minUnit then
            minUnit = l.unitPrice
        end
    end

    local hit = EvaluateListings(itemID, listings, threshold, main.minPrice)
    if not hit then
        LogBlind(itemID, minUnit, threshold, main, listings)
        return nil
    end
    blindSig[itemID] = nil   -- 重新有命中：清除静默标记，下次再变瞎时能立即记录
    local total = 0
    for _, l in ipairs(hit.matched) do
        total = total + l.unitPrice * l.quantity
    end
    hit.totalPrice = total
    -- 数据年龄时间戳：弹窗侧据此量化"提醒晚了多久"（排队/回包/上台三段）
    local seen = GetTime()
    hit.roundStartAt = roundStartTime
    hit.querySendAt = querySendTime
    hit.seenAt = seen
    Config.Log(("命中 %s 单价%.2fg 门槛%.2fg 数量%d 总价%.2fg 主簇%.2fg x%d（本轮起%.1fs 回包%.1fs）"):format(
        Config.GetItemDisplayName(itemID), minUnit / 10000, threshold / 10000,
        hit.totalQuantity, total / 10000, main.minPrice / 10000, main.totalQuantity,
        querySendTime - roundStartTime, seen - querySendTime))
    return hit
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
    -- 门禁统计：整次查询未收到任何结果事件 = 读到的只能来自缓存
    -- （连续 3 次说明门禁依赖的事件形态不成立，自动退回读缓存，见顶部自愈开关）
    if freshSeen then
        noEventStreak = 0
    else
        noEventStreak = noEventStreak + 1
        if noEventStreak >= 3 and not freshGateDisabled then
            freshGateDisabled = true
            Log("新鲜度门禁自动关闭：连续3次查询未收到任何结果事件（事件形态与预期不符），退回读取缓存")
        end
    end
    -- 刷新模式（购买前 RefreshHit）：结果交给回调，不推进轮次
    if querySink then
        local sink = querySink
        querySink = nil
        sink(listings, timedOut)
        return
    end
    local n = listings and #listings or 0
    if n > 0 then
        local hit = EvaluateHit(itemID, listings)
        if hit then
            -- 命中即进入/延续聚焦窗口：诱饵出现后大概率还有同价补货，高频重扫优先于其他商品轮询；
            -- 续期并清零 miss 计数（只要还有便宜货就保持聚焦）
            focus[itemID] = { expire = GetTime() + FOCUS_DURATION, miss = 0 }
            if focusSince == 0 then focusSince = GetTime() end
            Log(("命中 %s：进入/延续聚焦窗口 %.0fs"):format(itemID, FOCUS_DURATION))
            -- 命中一律入列（不等轮次结束）：列表非模态不遮操作，购买在途也照常刷新行，
            -- 用户盯按钮复亮连点即可。旧"购买进行中不弹窗"分支随弹窗模型一起废弃。
            AHSnipe.SnipeList.Add(hit)
        else
            -- 新鲜数据证实无低价 = 行可删的唯一扫描证据。
            -- timedOut 时读到的可能是部分/缓存结果，不算证据，不删行（防误删仍可买的行）。
            if not timedOut then
                AHSnipe.SnipeList.MarkGone(itemID)
            end
            -- 有挂单但无便宜货（价格已回涨/被买光，不再是诱饵价）：累计 miss，
            -- 达上限即提前退出聚焦，避免诱饵消失后还空转占满扫描通道
            local f = focus[itemID]
            if f then
                f.miss = (f.miss or 0) + 1
                if f.miss >= FOCUS_MISS_LIMIT then
                    focus[itemID] = nil
                    Log(("聚焦退出 %s：连续 %d 次无诱饵价"):format(itemID, f.miss))
                end
            end
        end
    elseif timedOut then
        -- 超时/跳过：结果事件未回，读到 0 条不等于真无挂单，单独标记便于诊断
        Config.Log(("%s 查询超时，本轮跳过（结果事件未回%s）"):format(itemID,
            freshSeen and "" or "，旧缓存已按门禁跳过不读"))
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

-- 收到本次查询的结果事件：标记为新鲜；若此前拦截过旧缓存，补一条"迟到回包"量化真实延迟
local function MarkFresh(itemID, src)
    if waitItemID ~= itemID then return end
    if freshSeen then return end
    freshSeen = true
    if staleBlocked then
        Log(("迟到回包 %s（距发送%.1fs，来源%s）：先前静默到点时读到的是上一轮快照，已拦截"):format(
            itemID, GetTime() - querySendTime, src))
    end
end

-- commodity 到齐读取：HasFull 只是缓存状态，本物品上一轮查过就一定为 true，
-- 因此必须叠加"本次查询收到过结果事件"才允许读，否则读到的是旧快照却当最新数据用
local function TryFinishCommodity(itemID, src)
    if waitItemID ~= itemID then return end
    if not C_AuctionHouse.HasFullCommoditySearchResults(itemID) then return end
    if not freshSeen and not freshGateDisabled then
        if not staleBlocked then
            staleBlocked = true
            Log(("旧缓存拦截 %s（静默%.1fs到点仍无回包，来源%s，不读缓存）"):format(
                itemID, BATCH_SILENCE, src))
        end
        return
    end
    FinishItem(itemID, CollectCommodityListings(itemID))
end

-- 每次发起查询前重置新鲜度状态（先清后发：同步触发的事件会立刻把 freshSeen 置回）
local function BeginQuery(itemID)
    waitItemID = itemID
    lastQueriedItemID = itemID   -- 记录自身查询，供结果事件区分用户浏览
    freshSeen = false
    staleBlocked = false
    querySendTime = GetTime()
end

local function QueryCommodity(itemID)
    BeginQuery(itemID)
    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    -- 先建计时器再发查询：事件可能在查询 API 调用内同步触发，否则计时器被覆盖导致重复 FinishItem
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        -- 超时：有回包则读取已到达的部分结果；整次查询无回包时读到的是纯缓存，
        -- 弹窗会提示可能早已消失的诱饵，故跳过该物品不评估（门禁关闭时退回旧行为）
        if freshSeen or freshGateDisabled then
            FinishItem(itemID, CollectCommodityListings(itemID), true)
        else
            FinishItem(itemID, nil, true)
        end
    end)
    silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
        -- 校验 waitItemID：防止刷新前的旧缓存被误当作本轮结果
        TryFinishCommodity(itemID, "静默")
        -- 被拦截时重挂静默计时器：回包晚到也能第一时间读取，不必干等到 10 秒超时
        if waitItemID == itemID and not freshSeen and not freshGateDisabled then
            silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
                TryFinishCommodity(itemID, "静默重试")
            end)
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
    BeginQuery(itemID)
    local itemKey = C_AuctionHouse.MakeItemKey(itemID)
    -- 先建计时器再发查询（事件可能同步触发）
    itemTimer = C_Timer.NewTimer(ITEM_TIMEOUT, function()
        if freshSeen or freshGateDisabled then
            FinishItem(itemID, CollectItemListings(itemKey), true)
        else
            FinishItem(itemID, nil, true)
        end
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
function Scanner.EndRound()
    running = false
    ClearTimers()
    -- 命中弹窗已改为单物品命中当场触发（FinishItem），轮末不再展示。
    -- 安排下一轮：间隔 0 = 连续轮询（仅留极小让步间隙，速度由节流闸门天然限制）；
    -- 非 0 = 按用户配置的轮间歇等待
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    if enabled then
        -- 浏览触发的补轮：上一轮进行中用户浏览产生的 Kick 在此兑现，立即开一轮不等轮间歇
        if pendingKick then
            pendingKick = false
            Scanner.ScanRound()
            return
        end
        -- 聚焦模式用更短轮间歇（FOCUS_GAP），让命中商品每秒重扫；否则用用户配置的轮间歇
        local gap
        if next(focus) ~= nil then
            gap = FOCUS_GAP
        else
            gap = Config.GetInterval()
            if gap <= 0 then gap = 0.2 end
        end
        roundTimer = C_Timer.NewTimer(gap, function()
            if enabled then Scanner.ScanRound() end
        end)
    end
end

function Scanner.ScanRound()
    if (not enabled and not manualOnce) or running then return end
    running = true
    roundStartTime = GetTime()
    queue = {}
    local now = GetTime()
    -- 清理过期 / 已禁用聚焦项，并判断当前是否有有效聚焦
    for id, f in pairs(focus) do
        if f.expire <= now or not Config.GetItem(id) or not Config.GetItem(id).enabled then
            focus[id] = nil
        end
    end
    if next(focus) ~= nil then
        -- 聚焦模式：只高频重扫命中 / 诱饵商品（抓同价补货），暂停其他商品轮询
        for id in pairs(focus) do
            queue[#queue + 1] = id
        end
        table.sort(queue)
        -- 被聚焦挤出的物品数一并记录：命中会不断续期聚焦窗口，只要诱饵一直挂着（没人买走），
        -- 其他监控物品就持续不被查询——"首报即尾批"的延迟会随时长累积
        local skipped = 0
        for _, it in ipairs(Config.GetItems()) do
            if it.enabled and not focus[it.itemID] then skipped = skipped + 1 end
        end
        Log(("聚焦轮次：%d 个商品（%s），其余%d个本轮跳过（聚焦已持续%.0fs）"):format(
            #queue, table.concat(queue, ","), skipped, focusSince > 0 and GetTime() - focusSince or 0))
    else
        focusSince = 0
        for _, it in ipairs(Config.GetItems()) do
            if it.enabled then
                queue[#queue + 1] = it.itemID
            end
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
-- 暂停（用户手动暂停或拍卖行关闭；购买在途时也会被调用以让出 AH 消息通道）
function Scanner.Pause()
    roundGen = roundGen + 1      -- 屏蔽未跟踪的旧轮回调（间隙/节流重试）
    enabled = false
    running = false
    waitingForGate = false       -- 防止闸门事件在暂停后误唤醒
    pendingKick = false          -- 清除未兑现的浏览补轮，防止恢复后意外立即开轮
    if querySink then            -- 刷新被中断（如 AH 关闭/暂停）：通知回调失败，防止购买流程静默挂起
        local sink = querySink
        querySink = nil
        sink(nil, true)
    end
    if gateTimer then gateTimer:Cancel(); gateTimer = nil end
    ClearTimers()
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    queue = nil
    focus = {}                  -- 清空聚焦窗口（暂停 / 关闭时不应保留专注状态）
    focusSince = 0
    freshSeen = false           -- 无在途查询，防止残留标记让下一轮误读缓存
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

-- 恢复扫描（需 AH 处于打开状态；用户手动暂停中或购买在途时不恢复）
function Scanner.Resume()
    if enabled or not ahOpen then return end
    if Config.IsPaused() then return end
    enabled = true
    manualOnce = false       -- 恢复正常轮询，清除手动单次标志
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
    if not ahOpen then
        Log("手动扫描：拍卖行未打开，忽略")
        return false
    end
    -- 暂停态（enabled=false 但 AH 开着）允许手动扫一次；其余非运行态不扫
    if not enabled and not Config.IsPaused() then
        Log("手动扫描：扫描未启动（非暂停态），忽略")
        return false
    end
    roundGen = roundGen + 1      -- 屏蔽旧轮的间隙/节流重试回调，防止与新轮次并发
    waitingForGate = false       -- 新轮次自行检查闸门，防旧事件误唤醒
    pendingKick = false          -- 手动扫描立即开轮，无需再兑现补轮
    if querySink then            -- 手动扫描打断刷新：通知回调失败
        local sink = querySink
        querySink = nil
        sink(nil, true)
    end
    if gateTimer then gateTimer:Cancel(); gateTimer = nil end
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    running = false          -- 打断进行中的轮次，重新开始
    ClearTimers()
    queue = nil
    focus = {}              -- 手动扫描 = 全面轮询，清除所有聚焦窗口（不被之前的诱饵聚焦绑架）
    focusSince = 0
    manualOnce = not enabled   -- 暂停态手动扫描：只跑这一轮，EndRound 不续轮（enabled 仍 false）
    Scanner.ScanRound()
    return true
end

-- 购买前刷新：重新查询该物品，按当前挂单与门槛重建命中对象。
-- 解决"提示后迟疑几秒再买，挂单已变动导致按旧数据下单"的问题：
-- 非 commodity 的 auctionID 会过期（唯一使用者：SnipeWindow 的物品点击路径；
-- commodity 不刷新——服务器报价本身就是实时价，刷新查询反而会挤占 AH 消息通道）。
-- 注意：调用方须在刷新回调【栈内直接】调购买 API，不得包 C_Timer 延迟——
-- 计时器上下文发出的购买消息会被客户端静默丢弃（详见 BuyQueue 发送时机说明）。
-- 事件栈内下单约 2 秒才送达服务器（第十轮实证），这比点击直接下单慢，只用于 auctionID 会失效的物品。
-- 会打断当前轮次独占查询通道，回调后自动恢复轮询。
-- callback(freshHit, reason)：freshHit 非 nil 可直接购买；nil 时 reason 为失败原因
function Scanner.RefreshHit(hit, callback)
    if AHSnipe.BuyQueue and AHSnipe.BuyQueue.IsBusy() then
        callback(nil, "已有购买进行中，请稍候")   -- 购买在途时扫描已让位，直接说明真实原因
        return
    end
    if not enabled then callback(nil, "扫描未运行") return end
    local itemID = hit.itemID
    if commodityCache[itemID] == nil then
        callback(nil, "物品类型未知")
        return
    end
    -- 打断当前轮，独占查询通道（代际递增屏蔽旧轮的间隙/闸门重试回调）
    roundGen = roundGen + 1
    waitingForGate = false
    pendingKick = false
    if roundTimer then roundTimer:Cancel(); roundTimer = nil end
    running = false
    ClearTimers()
    queue = nil
    Log(("购买前刷新 %s"):format(itemID))
    querySink = function(listings, timedOut)
        local fresh, reason = nil, nil
        if not enabled then
            reason = "扫描已停止（拍卖行关闭）"
        elseif timedOut then
            -- 超时说明结果事件未回，此时读到的可能是上一轮旧缓存，不能当作最新挂单使用
            reason = "查询超时（未收到最新结果）"
        elseif not listings or #listings == 0 then
            reason = "诱饵已被买走或下架"
        else
            fresh = EvaluateHit(itemID, listings)
            if not fresh then reason = "价格已回涨，已无低于门槛的挂单" end
        end
        callback(fresh, reason)
        if enabled then Scanner.ScanRound() end   -- 恢复连续轮询
    end
    if commodityCache[itemID] then
        QueryCommodity(itemID)
    else
        QueryItem(itemID)
    end
end

-- 交互触发：用户在 AH 内浏览物品（点收藏/搜索）时顺带重启一轮扫描
-- 防抖：浏览列表会连续触发多个结果事件，短窗口内只重启一次
-- 连续轮询下轮次几乎总在进行中：running 中只置 pendingKick，EndRound 后立即补一轮，
-- 避免持续浏览导致每 0.5s 从 queue[1] 重启、排位靠后的物品被饿死 + 在途查询作废浪费闸门额度
local kickPending = false
local pendingKick = false
function Scanner.Kick()
    if not enabled or kickPending then return end
    kickPending = true
    C_Timer.After(0.5, function()
        kickPending = false
        if not enabled then return end
        if running then
            pendingKick = true
        else
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
        MarkFresh(itemID, "ADDED")
        -- 每批到货后重置静默计时器
        if silenceTimer then silenceTimer:Cancel() end
        silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
            TryFinishCommodity(itemID, "ADDED静默")
        end)
        -- 已到齐则立即结束
        TryFinishCommodity(itemID, "ADDED")
    elseif event == "COMMODITY_SEARCH_RESULTS_RECEIVED" then
        -- 官方同样依赖此事件：结果全部收齐（无 payload，按当前等待物品判断）
        local itemID = waitItemID
        if not enabled or not itemID then return end
        MarkFresh(itemID, "RECEIVED")
        TryFinishCommodity(itemID, "RECEIVED")
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
        MarkFresh(itemID, "UPDATED")
        if C_AuctionHouse.HasFullCommoditySearchResults(itemID) then
            TryFinishCommodity(itemID, "UPDATED")
        elseif silenceTimer then
            -- 未到齐：重新计静默，让已到齐的判定自然收敛
            silenceTimer:Cancel()
            silenceTimer = C_Timer.NewTimer(BATCH_SILENCE, function()
                TryFinishCommodity(itemID, "UPDATED静默")
            end)
        end
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        local itemKey = ...
        if not enabled or not itemKey then return end
        if itemKey.itemID == waitItemID then
            -- 用 waitItemID 防重入（itemTimer 可能已被同步事件的 ClearTimers 清空）
            MarkFresh(itemKey.itemID, "ITEM_UPDATED")
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
