-- 可购买列表（替代旧狙击弹窗）。规格：docs/superpowers/specs/2026-10-16-ahsnipe-listing-rows-design.md
-- 一行 = 一个可买批次 = 一次点击一个请求：commodity 按单价档位成行（服务器报价只按数量取最便宜的
-- 若干个单位，同价位的几条挂单在请求里不可区分），非 commodity 按 auctionID 一条一行。
-- 点【立即购买】买队首（数据最新鲜；同一次命中内按单价升序 → 连点从最便宜档开始）。
-- 任何购买结局（成交/均价否决/价格不可用/超时/服务器失败）只删被点那一行，其余行不受影响。
-- 不做"记下点击稍后自动买下一条"：计时器/事件上下文的购买消息会被客户端静默丢弃（已实证）。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local Log = Config.Log

AHSnipe.SnipeList = {}
local List = AHSnipe.SnipeList

local frame = nil
local rows = {}                  -- 行数组，恒按 NewerThan 排序（[1]=队首）
local buyingItemID = nil         -- 购买在途物品ID（行前缀"购买中"）
local ahOpen = false             -- 拍卖行是否打开（决定窗口常驻/收起）
local pingLogged = false
local iconLogged = false
local lastPingRound = nil        -- 已响过的轮次身份（同一轮新增多行只响一次）

local ROW_H = 19
local ROW0_Y = -78     -- 第一行偏移：标题栏 + 扫描/暂停行 + 列头之下
local FOOT_H = 52      -- 底部按钮区高度
local MIN_H = 260      -- 首页高度：空列表也留出多行视野
local MAX_ROWS = 60    -- 帧池上限（每行 1 图标 + 4 FontString）：超限时淘汰排序末尾
local coin = function(c) return C_CurrencyInfo.GetCoinTextureString(c or 0) end
local VIS_H = MIN_H - (-ROW0_Y) - FOOT_H   -- 滚动视口高度：260-78-52 = 130 ≈ 6.8 行

-- 滚动范围自己算：12.x 的 GetVerticalRange/GetVerticalScroll 返回形态未实证，不许拿它们做算术
local function ContentH()
    return math.max(VIS_H, #rows * ROW_H)
end

local function PlayPing()
    -- 沿用旧弹窗提示音（游戏内已验证会响）；SOUNDKIT 字段名随版本变动，失败只记一次
    local ok, err = pcall(PlaySound, (SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING) or 882)
    if not ok and not pingLogged then
        pingLogged = true
        Log(("提示音播放失败：%s"):format(tostring(err)))
    end
end

-- 行排序：数据新鲜度降序为第一键（队首永远是刚扫到的）；同一次命中的多行按单价升序 → 连点先买最便宜档
function List.NewerThan(a, b)
    local sa, sb = a.seenAt or 0, b.seenAt or 0
    if sa ~= sb then return sa > sb end
    local ua, ub = a.unitPrice or 0, b.unitPrice or 0
    if ua ~= ub then return ua < ub end
    return (a.quantity or 0) < (b.quantity or 0)
end

-- 命中摊成档位行（纯函数，可单测）：commodity 同单价合计成一行，非 commodity 一条 auctionID 一行
function List.BuildRows(hit)
    local byKey, order = {}, {}
    for _, l in ipairs(hit.matched or {}) do
        local key
        if not l.quantity or l.quantity <= 0 or not l.unitPrice then
            key = nil                                   -- 无单价/数量的挂单不成行
        elseif hit.isCommodity then
            key = ("C:%s:%s"):format(hit.itemID, math.floor(l.unitPrice + 0.5))
        elseif l.auctionID then
            key = ("A:%s"):format(l.auctionID)
        else
            key = nil                                   -- 非 commodity 缺 auctionID 无法下单，不入列
        end
        if key then
            local row = byKey[key]
            if not row then
                row = { key = key, unitPrice = l.unitPrice, quantity = 0, totalPrice = 0,
                        auctionID = l.auctionID, buyoutAmount = l.buyoutAmount }
                byKey[key] = row
                order[#order + 1] = key
            end
            row.quantity = row.quantity + l.quantity
            row.totalPrice = row.totalPrice + l.unitPrice * l.quantity
            row.itemID = hit.itemID
            row.name = hit.name or Config.GetItemDisplayName(hit.itemID)
            row.threshold = hit.threshold
            row.isCommodity = hit.isCommodity and true or false
            row.seenAt = hit.seenAt
            row.roundStartAt = hit.roundStartAt
            row.querySendAt = hit.querySendAt
        end
    end
    local out = {}
    for _, key in ipairs(order) do out[#out + 1] = byKey[key] end
    table.sort(out, function(a, b)
        if a.unitPrice == b.unitPrice then return a.quantity < b.quantity end
        return a.unitPrice < b.unitPrice
    end)
    return out
end

local function EnsureRowPool(f, n)
    -- 不用 `f.rowFS or {}`：帧对象上读到非表值（测试 mock 帧给未知键返回函数）会毁掉池
    if type(f.rowFS) ~= "table" then f.rowFS = {} end
    -- 行钉在滚动内容帧上（窗口高度固定，行多时靠滚动看），内容帧缺失时退回窗口保证渲染不炸
    local host = type(f.content) == "table" and f.content or f
    while #f.rowFS < n do
        local w = {}
        w.icon = host:CreateTexture(nil, "OVERLAY")
        w.name = host:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.unit = host:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.qty = host:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.total = host:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        f.rowFS[#f.rowFS + 1] = w
    end
end

function List.UpdateBuyButton()
    if not frame or not frame.buyBtn then return end
    local BQ = AHSnipe.BuyQueue
    if BQ and BQ.IsBusy() then
        frame.buyBtn:Disable()
        frame.buyBtn:SetText("购买中…")
    elseif #rows > 0 then
        frame.buyBtn:Enable()
        frame.buyBtn:SetText("立即购买")
    else
        frame.buyBtn:Disable()
        frame.buyBtn:SetText("立即购买")
    end
end

local function Render()
    if not frame then return end
    table.sort(rows, List.NewerThan)
    local f = frame
    EnsureRowPool(f, #rows)
    local host = type(f.content) == "table" and f.content or f
    -- 上一轮的行也要走一遍才能隐藏，否则缩表时旧行会留在屏幕上
    for i = 1, math.max(#rows, tonumber(f.lastRows) or 0) do
        local w, row = f.rowFS[i], rows[i]
        if not row then
            w.icon:Hide(); w.name:Hide(); w.unit:Hide(); w.qty:Hide(); w.total:Hide()
        else
            -- 内容帧坐标系：行位置只跟行号有关，窗口滚动由 scrollY 统一偏移
            local y = -((i - 1) * ROW_H)
            local label = row.name or Config.GetItemDisplayName(row.itemID)
            if buyingItemID == row.itemID then label = "|cffffcc00购买中|r " .. label end
            w.name:ClearAllPoints(); w.name:SetPoint("LEFT", host, "TOPLEFT", 30, y)
            w.name:SetText(label); w.name:Show()
            -- 图标：12.x API 形态未全实证，pcall 尽力而为，取不到只显名称（记一次日志）
            local oki, icon = pcall(function()
                return C_Item and C_Item.GetItemIconByID and C_Item.GetItemIconByID(row.itemID)
            end)
            if oki and icon then
                w.icon:ClearAllPoints(); w.icon:SetPoint("LEFT", host, "TOPLEFT", 13, y + 4)
                w.icon:SetSize(14, 14)
                local okr = pcall(w.icon.SetTexture, w.icon, icon)
                if okr then w.icon:Show() else w.icon:Hide() end
            else
                w.icon:Hide()
                if not iconLogged then
                    iconLogged = true
                    Log("列表图标不可用（C_Item.GetItemIconByID 形态无效），仅显名称")
                end
            end
            w.unit:ClearAllPoints(); w.unit:SetPoint("LEFT", host, "TOPLEFT", 190, y)
            w.unit:SetText(coin(math.floor(row.unitPrice))); w.unit:Show()
            w.qty:ClearAllPoints(); w.qty:SetPoint("LEFT", host, "TOPLEFT", 262, y)
            w.qty:SetText(tostring(row.quantity)); w.qty:Show()
            w.total:ClearAllPoints(); w.total:SetPoint("LEFT", host, "TOPLEFT", 300, y)
            w.total:SetText(coin(row.totalPrice)); w.total:Show()
        end
    end
    f.lastRows = #rows
    f:SetHeight(MIN_H)          -- 窗口高度恒定：行数变化只体现在内容高度上，不该让主界面尺寸跳动
    local ch = ContentH()
    if type(f.content) == "table" then f.content:SetHeight(ch) end
    -- 行数变少时把越界的滚动偏移拉回来（可视范围自己算，不依赖 GetVerticalRange 的返回形态）
    local y = math.max(0, math.min(tonumber(f.scrollY) or 0, ch - VIS_H))
    f.scrollY = y
    if type(f.scroll) == "table" then pcall(f.scroll.SetVerticalScroll, f.scroll, y) end
    if type(f.emptyFS) == "table" then
        if #rows == 0 then f.emptyFS:Show() else f.emptyFS:Hide() end
    end
    -- 拍卖行打开期间窗口常驻（列表即主界面，空列表也留入口）；仅关窗时收起
    if ahOpen then f:Show() else f:Hide() end
    List.UpdateBuyButton()
end

local function EnsureUI()
    if frame then return frame end
    local ok, err = pcall(function()
        local f = CreateFrame("Frame", "AHSnipeListFrame", UIParent, "BasicFrameTemplateWithInset")
        f:SetSize(430, MIN_H)
        if f.TitleText then f.TitleText:SetText("可购买列表") end
        f:SetFrameStrata("DIALOG")
        f:SetToplevel(true)
        f:SetMovable(true)
        f:EnableMouse(true)
        f:RegisterForDrag("LeftButton")
        f:SetScript("OnDragStart", function(self) self:StartMoving() end)
        f:SetScript("OnDragStop", function(self)
            self:StopMovingOrSizing()
            -- GetPoint 返回 point, relativeTo, relativePoint, x, y（漏一位会把 relativePoint 当 x 存）
            local point, _, _, x, y = self:GetPoint()
            if point then Config.SetListPos(point, x, y) end
        end)
        local pos = Config.GetListPos()
        local anchored = false
        if pos then
            anchored = pcall(f.SetPoint, f, pos.point, UIParent, pos.point, pos.x, pos.y)
        end
        if not anchored then
            -- 默认锚点一律用 UIParent：和上面存/取的坐标同一个坐标系，
            -- 若默认钉在拍卖行框体上，存成 UIParent 相对后再开就会整体偏移
            f:SetPoint("CENTER", UIParent, "CENTER", 240, 40)
        end

        -- 扫描控制从配置面板上移到首页（列表即主界面，开关不该藏在二级窗里）
        local scanBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        scanBtn:SetSize(84, 22)
        scanBtn:SetPoint("TOPLEFT", f, "TOPLEFT", 14, -34)
        scanBtn:SetText("手动扫描")
        scanBtn:SetScript("OnClick", function()
            if AHSnipe.Scanner and AHSnipe.Scanner.ScanNow() then
                print("|cff33FF99AHSnipe|r: 已立即扫描，自动计时从现在重新开始。")
            else
                print("|cffFF4040AHSnipe|r: 扫描未启用（需先打开拍卖行，或已暂停）。")
            end
        end)

        local pauseBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        pauseBtn:SetSize(64, 22)
        pauseBtn:SetPoint("LEFT", scanBtn, "RIGHT", 4, 0)
        pauseBtn:SetScript("OnClick", function()
            if not AHSnipe.Scanner then return end
            local paused = AHSnipe.Scanner.SetPaused(not AHSnipe.Scanner.IsPaused())
            pauseBtn:SetText(paused and "继续" or "暂停")
            if paused then
                print("|cffFF4040AHSnipe|r: 扫描已暂停。")
            else
                print("|cff33FF99AHSnipe|r: 扫描已恢复。")
            end
        end)
        pauseBtn:SetText(AHSnipe.Scanner and AHSnipe.Scanner.IsPaused() and "继续" or "暂停")

        -- 滚动区：窗口高度固定，行多时滚动而非把窗口拉长（列表常驻主界面，尺寸不该跳动）。
        -- 滚轮回调自己算范围（ContentH() - VIS_H），GetVerticalScroll 的返回形态未实证，不作依据。
        local scroll = CreateFrame("ScrollFrame", "AHSnipeListScroll", f, "UIPanelScrollFrameTemplate")
        scroll:SetPoint("TOPLEFT", f, "TOPLEFT", 4, ROW0_Y)
        scroll:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -26, FOOT_H)
        local content = CreateFrame("Frame", "AHSnipeListContent", f)
        content:SetWidth(392)
        scroll:SetScrollChild(content)
        f.scroll, f.content = scroll, content
        scroll:EnableMouseWheel(true)
        scroll:SetScript("OnMouseWheel", function(_, delta)
            local y = math.max(0, math.min(ContentH() - VIS_H, (tonumber(f.scrollY) or 0) - delta * ROW_H))
            f.scrollY = y
            pcall(scroll.SetVerticalScroll, scroll, y)
        end)

        -- 列头与数据列同 x 对齐（单价 190 / 数量 262 / 总价 300）
        local headers = { { "单价", 190 }, { "数量", 262 }, { "总价", 300 } }
        for _, h in ipairs(headers) do
            local fs = f:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
            fs:SetPoint("LEFT", f, "TOPLEFT", h[2], ROW0_Y + ROW_H)
            fs:SetText(h[1])
        end

        local cfgBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        cfgBtn:SetSize(70, 22)
        cfgBtn:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 14, 18)
        cfgBtn:SetText("配置")
        cfgBtn:SetScript("OnClick", function()
            -- 列表即主界面：配置面板由此按需打开（Core 已不再随拍卖行自动弹出）
            if AHSnipe.ConfigPanel and AHSnipe.ConfigPanel.Open then
                AHSnipe.ConfigPanel.Open()
            end
        end)

        local buyBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        buyBtn:SetSize(200, 34)
        buyBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -14, 12)
        buyBtn:SetScript("OnClick", function() List.TriggerBuy() end)
        f.buyBtn = buyBtn

        local emptyFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        emptyFS:SetPoint("TOPLEFT", f, "TOPLEFT", 30, ROW0_Y)
        emptyFS:SetText("暂无可购买物品（命中后自动入列）")
        emptyFS:SetTextColor(0.6, 0.6, 0.6)
        f.emptyFS = emptyFS

        f.lastRows = 0             -- 隐藏上一轮行用的计数，必须建窗就有初值
        f.scrollY = 0              -- 滚动偏移：mock/未初始化帧上读到函数会让渲染期算术报错
        frame = f
    end)
    if not ok then
        Log(("列表窗创建失败：%s"):format(tostring(err)))
        return nil
    end
    return frame
end

function List.Add(hit)
    if not EnsureUI() then return end
    local fresh = List.BuildRows(hit)
    local keep = {}
    for _, r in ipairs(fresh) do keep[r.key] = r end
    local nextRows, added = {}, 0
    -- 该 itemID 的现有行按 key 对齐：命中→原地刷新（静默），新数据里没这个档位了→删行（新鲜证据）
    for _, r in ipairs(rows) do
        if r.itemID ~= hit.itemID then
            nextRows[#nextRows + 1] = r
        else
            local f = keep[r.key]
            if f then
                nextRows[#nextRows + 1] = f
                keep[r.key] = nil
            else
                Log(("除行 %s（该档位已无低于门槛挂单）"):format(r.key))
            end
        end
    end
    for _, r in ipairs(fresh) do
        if keep[r.key] then
            nextRows[#nextRows + 1] = r
            added = added + 1
            Log(("入列 %s 单价%.2fg 数量%d 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs）"):format(
                r.itemID, (r.unitPrice or 0) / 10000, r.quantity or 0,
                (r.totalPrice or 0) / 10000,
                (r.seenAt or 0) - (r.roundStartAt or 0),
                (r.querySendAt or 0) - (r.roundStartAt or 0),
                (r.seenAt or 0) - (r.querySendAt or 0)))
        end
    end
    rows = nextRows
    table.sort(rows, List.NewerThan)
    while #rows > MAX_ROWS do
        local dropped = table.remove(rows)
        Log(("除行 %s（超过%d行上限，淘汰最旧）"):format(dropped.key, MAX_ROWS))
    end
    -- 同一轮里多个新档位只响一次（roundStartAt 即轮次身份）
    if added > 0 and hit.roundStartAt ~= lastPingRound then
        lastPingRound = hit.roundStartAt
        PlayPing()
    end
    Render()
end

-- 该 itemID 的全部行出门（扫描用新鲜数据证实整件已无低价挂单）
function List.MarkGone(itemID, reason)
    local removed, nextRows = 0, {}
    for _, r in ipairs(rows) do
        if r.itemID == itemID then
            removed = removed + 1
            Log(("除行 %s（%s）"):format(r.key, reason or "扫描证实无低价"))
        else
            nextRows[#nextRows + 1] = r
        end
    end
    if removed == 0 then return end
    rows = nextRows
    Render()
end

-- 按 key 删单行（购买结局、挂单失效都走这里）；行已被新鲜扫描删掉时静默返回 false
function List.DropRow(key, reason)
    for i, r in ipairs(rows) do
        if r.key == key then
            table.remove(rows, i)
            Log(("除行 %s（%s）"):format(key, reason or "已结束"))
            return true
        end
    end
    return false
end

-- BuyQueue 结算回调：整单全部成功才删行；有失败/剩余保留行，
-- 等下轮扫描刷新数字、或证实无低价由 Scanner 删行。同时清"购买中"标记、恢复按钮。
function List.OnBuyFinished(s)
    buyingItemID = nil
    if s and s.itemID and (s.bought or 0) > 0
        and (s.failed or 0) == 0 and not s.deferred then
        List.MarkGone(s.itemID, "购买完成")
    end
    Render()
end

-- 行 → 交给 BuyQueue 的 hit：matched 只含这一批，服务器报价就按这一档的数量取最便宜的若干个
local function RowToHit(r)
    local l
    if r.isCommodity then
        l = { unitPrice = r.unitPrice, quantity = r.quantity }
    else
        l = { unitPrice = r.unitPrice, quantity = r.quantity,
              auctionID = r.auctionID, buyoutAmount = r.buyoutAmount }
    end
    return {
        itemID = r.itemID, name = r.name, threshold = r.threshold,
        isCommodity = r.isCommodity, rowKey = r.key,
        matched = { l }, totalQuantity = r.quantity, totalPrice = r.totalPrice,
        seenAt = r.seenAt, roundStartAt = r.roundStartAt, querySendAt = r.querySendAt,
    }
end

-- 唯一购买入口：必须在按钮 OnClick 的硬件点击栈内直调（或其下游刷新回调栈内）。
function List.TriggerBuy()
    local BQ = AHSnipe.BuyQueue
    local row = rows[1]
    if not row or (BQ and BQ.IsBusy()) then return false end
    Log(("点击购买 %s %s isCommodity=%s 数量%d"):format(
        row.itemID, row.key, tostring(row.isCommodity), row.quantity or 0))
    if row.isCommodity then
        -- commodity 不做购买前刷新：刷新查询挤占节流通道且服务器报价本身就是实时价
        if BQ.Buy(RowToHit(row)) then
            buyingItemID = row.itemID
            Render()
        end
    else
        -- 非 commodity 的 auctionID 会过期：先刷新最新挂单，回调栈内直调购买（不得包计时器）
        AHSnipe.Scanner.RefreshHit(RowToHit(row), function(fresh, reason)
            if fresh then
                local hit = RowToHit(row)
                hit.matched = { fresh.matched[1] }   -- Task 3 改为按 auctionID 精确取本行挂单
                if BQ.Buy(hit) then
                    buyingItemID = row.itemID
                    Render()
                end
            else
                Log(("购买前刷新失败 %s：%s"):format(row.itemID, reason))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(row.name, reason))
            end
        end)
    end
    return true
end

------------------------------------------------------------------------
-- 生命周期：跟随拍卖行开合（开=备好窗体并常驻；关=清空并收起，过期快照不许再点）
------------------------------------------------------------------------
function List.Init()
    local f = CreateFrame("Frame", "AHSnipeListEvents")
    f:RegisterEvent("AUCTION_HOUSE_SHOW")
    f:RegisterEvent("AUCTION_HOUSE_CLOSED")
    f:SetScript("OnEvent", function(_, event)
        if event == "AUCTION_HOUSE_SHOW" then
            ahOpen = true
            EnsureUI()
            Render()
        else
            ahOpen = false
            rows = {}
            buyingItemID = nil
            if frame then frame:Hide() end
        end
    end)
    List.frame = f
end

-- 测试/诊断访问器
function List.RowCount() return #rows end
function List.GetFront() return rows[1] end
