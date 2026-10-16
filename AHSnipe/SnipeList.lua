-- 可购买列表（替代旧狙击弹窗）。规格：docs/superpowers/specs/2026-10-16-ahsnipe-buylist-design.md
-- 命中加行、同物品原地静默刷新；操作只有底部【买队首】（购买在途置灰）与【配置】。
-- 拍卖行打开期间本窗常驻 = 插件主界面（空列表也显示，入口不能等命中才出现）；
-- 配置面板改为按需打开，不再随拍卖行自动弹出。
-- 删行依据：扫描用新鲜数据证实无低价（Scanner.MarkGone）、整单买成功（BuyQueue→OnBuyFinished）、
-- 或关闭拍卖行（过期快照不许再点）。
-- 不做"记下点击稍后自动买下一条"：计时器/事件上下文的购买消息会被客户端静默丢弃（已实证）。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local Log = Config.Log

AHSnipe.SnipeList = {}
local List = AHSnipe.SnipeList

local frame = nil
local rows = {}                  -- hit 数组，恒按 seenAt 降序（[1]=队首=最新鲜）
local buyingItemID = nil         -- 购买在途物品ID（行前缀"购买中"）
local ahOpen = false             -- 拍卖行是否打开（决定窗口常驻/收起）
local pingLogged = false
local iconLogged = false

local ROW_H = 19
local coin = function(c) return C_CurrencyInfo.GetCoinTextureString(c or 0) end

local function MinUnit(hit)
    local m = nil
    for _, l in ipairs(hit.matched) do
        if not m or l.unitPrice < m then m = l.unitPrice end
    end
    return m or 0
end

local function PlayPing()
    -- 沿用旧弹窗提示音（游戏内已验证会响）；SOUNDKIT 字段名随版本变动，失败只记一次
    local ok, err = pcall(PlaySound, (SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING) or 882)
    if not ok and not pingLogged then
        pingLogged = true
        Log(("提示音播放失败：%s"):format(tostring(err)))
    end
end

local function EnsureRowPool(f, n)
    -- 不用 `f.rowFS or {}`：帧对象上读到非表值（测试 mock 帧给未知键返回函数）会毁掉池
    if type(f.rowFS) ~= "table" then f.rowFS = {} end
    while #f.rowFS < n do
        local w = {}
        w.icon = f:CreateTexture(nil, "OVERLAY")
        w.name = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.unit = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.qty = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        w.total = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
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
        frame.buyBtn:SetText("买队首")
    else
        frame.buyBtn:Disable()
        frame.buyBtn:SetText("买队首")
    end
end

local function Render()
    if not frame then return end
    table.sort(rows, function(a, b) return (a.seenAt or 0) > (b.seenAt or 0) end)
    local f = frame
    EnsureRowPool(f, #rows)
    -- 上一轮的行也要走一遍才能隐藏，否则缩表时旧行会留在屏幕上
    for i = 1, math.max(#rows, tonumber(f.lastRows) or 0) do
        local w, hit = f.rowFS[i], rows[i]
        if not hit then
            w.icon:Hide(); w.name:Hide(); w.unit:Hide(); w.qty:Hide(); w.total:Hide()
        else
            local y = -44 - (i - 1) * ROW_H
            local label = hit.name or Config.GetItemDisplayName(hit.itemID)
            if buyingItemID == hit.itemID then label = "|cffffcc00购买中|r " .. label end
            w.name:ClearAllPoints(); w.name:SetPoint("LEFT", f, "TOPLEFT", 30, y)
            w.name:SetText(label); w.name:Show()
            -- 图标：12.x API 形态未全实证，pcall 尽力而为，取不到只显名称（记一次日志）
            local oki, icon = pcall(function()
                return C_Item and C_Item.GetItemIconByID and C_Item.GetItemIconByID(hit.itemID)
            end)
            if oki and icon then
                w.icon:ClearAllPoints(); w.icon:SetPoint("LEFT", f, "TOPLEFT", 13, y + 4)
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
            w.unit:ClearAllPoints(); w.unit:SetPoint("LEFT", f, "TOPLEFT", 190, y)
            w.unit:SetText(coin(math.floor(MinUnit(hit)))); w.unit:Show()
            w.qty:ClearAllPoints(); w.qty:SetPoint("LEFT", f, "TOPLEFT", 262, y)
            w.qty:SetText(tostring(hit.totalQuantity)); w.qty:Show()
            w.total:ClearAllPoints(); w.total:SetPoint("LEFT", f, "TOPLEFT", 300, y)
            w.total:SetText(coin(hit.totalPrice)); w.total:Show()
        end
    end
    f.lastRows = #rows
    f:SetHeight(math.max(110, 66 + #rows * ROW_H))
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
        f:SetSize(400, 130)
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

        local cfgBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        cfgBtn:SetSize(70, 24)
        cfgBtn:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 14, 14)
        cfgBtn:SetText("配置")
        cfgBtn:SetScript("OnClick", function()
            -- 列表即主界面：配置面板由此按需打开（Core 已不再随拍卖行自动弹出）
            if AHSnipe.ConfigPanel and AHSnipe.ConfigPanel.Open then
                AHSnipe.ConfigPanel.Open()
            end
        end)

        local buyBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
        buyBtn:SetSize(150, 24)
        buyBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -14, 14)
        buyBtn:SetScript("OnClick", function() List.TriggerBuy() end)
        f.buyBtn = buyBtn

        local hintFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        hintFS:SetPoint("BOTTOMRIGHT", f.buyBtn, "TOPRIGHT", 0, 6)
        hintFS:SetText("单价   数量   总价")
        hintFS:SetTextColor(0.6, 0.6, 0.6)

        local emptyFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        emptyFS:SetPoint("TOPLEFT", f, "TOPLEFT", 30, -44)
        emptyFS:SetText("暂无可购买物品（命中后自动入列）")
        emptyFS:SetTextColor(0.6, 0.6, 0.6)
        f.emptyFS = emptyFS

        f.lastRows = 0             -- 隐藏上一轮行用的计数，必须建窗就有初值
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
    local idx = nil
    for i, r in ipairs(rows) do
        if r.itemID == hit.itemID then idx = i end
    end
    if idx then
        rows[idx] = hit          -- 原地刷新数字与时间戳：连续轮询每秒都会到这里，静默不响
    else
        rows[#rows + 1] = hit
        local now = GetTime()
        Log(("入列 %s 单价%.2fg 数量%d 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs）"):format(
            hit.itemID, MinUnit(hit) / 10000, hit.totalQuantity, hit.totalPrice / 10000,
            now - hit.roundStartAt, hit.querySendAt - hit.roundStartAt,
            hit.seenAt - hit.querySendAt))
        PlayPing()
    end
    Render()
end

function List.MarkGone(itemID, reason)
    for i, r in ipairs(rows) do
        if r.itemID == itemID then
            table.remove(rows, i)
            Log(("除行 %s（%s）"):format(itemID, reason or "扫描证实无低价"))
            Render()
            return
        end
    end
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

-- 唯一购买入口：必须在按钮 OnClick 的硬件点击栈内直调（或其下游刷新回调栈内）。
function List.TriggerBuy()
    local BQ = AHSnipe.BuyQueue
    if not rows[1] or (BQ and BQ.IsBusy()) then return false end
    local hit = rows[1]
    Log(("点击购买 %s isCommodity=%s 命中%d条"):format(
        hit.itemID, tostring(hit.isCommodity), #hit.matched))
    if hit.isCommodity then
        -- commodity 不做购买前刷新：刷新查询挤占节流通道且报价本身实时
        if BQ.Buy(hit) then
            buyingItemID = hit.itemID
            Render()
        end
    else
        -- 非 commodity 的 auctionID 会过期：先刷新最新挂单，回调栈内直调购买（不得包计时器）
        AHSnipe.Scanner.RefreshHit(hit, function(fresh, reason)
            if fresh then
                if BQ.Buy(fresh) then
                    buyingItemID = fresh.itemID
                    Render()
                end
            else
                Log(("购买前刷新失败 %s：%s"):format(hit.itemID, reason))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(
                    hit.name or Config.GetItemDisplayName(hit.itemID), reason))
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
