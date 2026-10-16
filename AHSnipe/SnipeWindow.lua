-- 模态狙击弹窗：物品名、单价/数量/总价、诱饵标注、【全部购买】【忽略】。
-- 多命中排队：弹窗期间新命中入队，关闭后依次处理；同一物品只保留最新一份命中。
-- 键盘购买：命名安全按钮 AHSnipeKeyBuy（宏 /click 或 /ahsnipe bind 绑定），
-- 把"移鼠标+瞄准+点击"压成一次按键；购买 API 受硬件事件保护，这是插件侧唯一合规路径。
-- 命中提醒：整屏闪烁 + 屏幕中央横幅 + 弹窗期间连续提示音，防止注意力在别处时漏看一角弹窗。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config

local Log = Config.Log   -- 诊断日志（/ahsnipe log 输出）

AHSnipe.SnipeWindow = {}
local Win = AHSnipe.SnipeWindow

local queue = {}                 -- 待展示的命中队列（按物品去重）
local current = nil              -- 当前展示的命中
local lastShownSig = {}          -- [itemID] = 上次展示命中的挂单签名：内容不变不重复弹窗
local frame = nil
local keyBtn = nil               -- 键盘购买按钮（SecureActionButtonTemplate）
local flash = nil                -- 整屏闪烁遮罩
local banner = nil               -- 屏幕中央横幅
local KEY_BTN_NAME = "AHSnipeKeyBuy"
local PING_INTERVAL = 1.5        -- 弹窗未关闭时每隔多少秒重播一次提示音
local coin = function(c) return C_CurrencyInfo.GetCoinTextureString(c or 0) end

------------------------------------------------------------------------
-- 购买动作（鼠标点击与键盘绑定共用同一入口）
------------------------------------------------------------------------
-- source 仅用于日志区分触发方式（"点击" / "按键"）。
-- 关键纪律：本函数必须在硬件事件栈内被直接调用，内部不得包任何计时器延迟，
-- 否则购买消息被客户端静默丢弃（见 BuyQueue 发送时机说明）。
function Win.TriggerBuy(source)
    if not current then
        Log(("%s购买：当前无弹窗命中，忽略"):format(source))
        return false
    end
    local hit = current
    Log(("%s购买 %s isCommodity=%s 命中%d条"):format(
        source, hit.itemID, tostring(hit.isCommodity), #hit.matched))
    Win.Hide()  -- 购买进行中关闭弹窗，汇总由聊天框输出
    if hit.isCommodity then
        -- commodity：直接按扫描挂单分块下单（BuyQueue 内部逐块门槛复核）。
        -- 不做购买前刷新——刷新查询会挤占 AH 消息通道，且购买调用若发生在刷新的
        -- 结果事件栈内，会被节流系统推迟约 2 秒才送达服务器，低价簇等不到那一刻；
        -- 报价超门槛时 BuyQueue 立即收尾并清除弹窗抑制，≤1 秒后的聚焦扫描会带着
        -- 当前真实数量重新弹窗（比在点击栈里插一次刷新查询更快，发送也更可靠）。
        AHSnipe.BuyQueue.Buy(hit)
    else
        local function ReportFail(reason)
            print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(
                Config.GetItemDisplayName(hit.itemID), reason))
        end
        -- 物品：auctionID 会过期（提示后迟疑几秒就可能被买走），
        -- 先刷新最新挂单再下单，避免"能买时没提示、提示时已失效"
        AHSnipe.Scanner.RefreshHit(hit, function(fresh, reason)
            -- 直接在刷新结果事件栈内购买（不得包计时器延迟，见 BuyQueue 发送时机说明）
            if fresh then
                AHSnipe.BuyQueue.Buy(fresh)
            else
                Log(("购买前刷新失败 %s：%s"):format(hit.itemID, reason))
                ReportFail(reason)
            end
        end)
    end
    return true
end

------------------------------------------------------------------------
-- 构建 UI（延迟创建）
------------------------------------------------------------------------
local function CreateFrameUI()
    if frame then return end

    local f = CreateFrame("Frame", "AHSnipeSnipeFrame", UIParent, "BasicFrameTemplateWithInset")
    f:SetSize(360, 260)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:SetToplevel(true)
    f:SetMovable(true)
    f:EnableMouse(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    f:Hide()

    -- 模态遮罩：点击遮罩不关闭，必须点按钮
    local mask = CreateFrame("Frame", nil, UIParent)
    mask:SetAllPoints(UIParent)
    mask:EnableMouse(true)
    mask:SetFrameStrata("DIALOG")
    mask:SetFrameLevel(f:GetFrameLevel() - 1)
    mask:Hide()
    f.mask = mask

    -- 标题
    local title = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
    title:SetPoint("TOP", 0, -12)
    title:SetText("拍卖行狙击")
    f.title = title

    -- 物品名
    local nameFS = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    nameFS:SetPoint("TOP", title, "BOTTOM", 0, -14)
    f.nameFS = nameFS

    -- 价格明细（单价/数量/总价）
    local detailFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    detailFS:SetPoint("TOP", nameFS, "BOTTOM", 0, -10)
    detailFS:SetWidth(320)
    detailFS:SetJustifyH("LEFT")
    f.detailFS = detailFS

    -- 诱饵标注（红色）
    local baitFS = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    baitFS:SetPoint("TOP", detailFS, "BOTTOM", 0, -8)
    baitFS:SetTextColor(1, 0.2, 0.2)
    baitFS:Hide()
    f.baitFS = baitFS

    -- 全部购买按钮
    local buyBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    buyBtn:SetSize(120, 30)
    buyBtn:SetPoint("BOTTOMLEFT", f, "BOTTOM", -130, 14)
    buyBtn:SetText("全部购买")
    buyBtn:SetScript("OnClick", function() Win.TriggerBuy("点击") end)
    f.buyBtn = buyBtn

    -- 忽略按钮
    local ignoreBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    ignoreBtn:SetSize(120, 30)
    ignoreBtn:SetPoint("BOTTOMRIGHT", f, "BOTTOM", 130, 14)
    ignoreBtn:SetText("忽略")
    ignoreBtn:SetScript("OnClick", function()
        Win.Hide()
    end)
    f.ignoreBtn = ignoreBtn

    frame = f

    -- 模板内置关闭按钮：与"忽略"一致走 Win.Hide（处理排队弹窗）
    local closeBtn = _G["AHSnipeSnipeFrameClose"]
    if closeBtn then
        closeBtn:HookScript("OnClick", function() Win.Hide() end)
    end
end

------------------------------------------------------------------------
-- 命中提醒：整屏闪烁 + 屏幕中央横幅 + 弹窗期间连续提示音
-- 弹窗只占屏幕一角，注意力在动作条/战斗时容易漏看；这三项把提醒推到视野中心。
------------------------------------------------------------------------
local pingTicker = nil
local bannerTimer = nil
local pingLogged = false
local bannerFontLogged = nil     -- 已记日志的横幅字体名（探测成功时只写一次，避免刷屏）
local BANNER_DURATION = 6        -- 中央横幅停留秒数（弹窗关闭即撤，超时也不留残余）

local function PlayPing()
    -- 沿用改版前的提示音（游戏内已验证会响）；SOUNDKIT 字段名随版本变动，失败只记一次日志
    local ok, err = pcall(PlaySound, (SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING) or 882)
    if not ok and not pingLogged then
        pingLogged = true
        Log(("提示音播放失败：%s"):format(tostring(err)))
    end
end

-- 大号字体名在 12.x 未核实，逐个探测；全部失败则只有闪烁没有横幅（不阻断弹窗与购买链路）
local BANNER_FONTS = { "GameFontNormalHuge", "GameFontNormalLarge", "GameFontNormal" }

local function CreateBanner()
    local b = CreateFrame("Frame", nil, UIParent)
    b:SetSize(760, 140)
    b:SetPoint("CENTER", UIParent, "TOP", 0, -110)   -- 屏幕上半部，不与中央弹窗重叠
    b:SetFrameStrata("FULLSCREEN_DIALOG")
    for _, font in ipairs(BANNER_FONTS) do
        local ok, fs = pcall(function() return b:CreateFontString(nil, "OVERLAY", font) end)
        if ok and fs then
            fs:SetPoint("CENTER")
            fs:SetJustifyH("CENTER")
            fs:SetJustifyV("MIDDLE")
            fs:SetTextColor(1, 0.25, 0.15)
            b.fs = fs
            if bannerFontLogged ~= font then
                bannerFontLogged = font
                Log(("横幅字体：%s"):format(font))
            end
            b:Hide()
            return b
        end
    end
    return nil
end

local function EnsureAlertUI()
    if flash then return end
    local f = CreateFrame("Frame", nil, UIParent)
    f:SetAllPoints(UIParent)
    f:SetFrameStrata("FULLSCREEN_DIALOG")
    f:EnableMouse(false)          -- 闪烁层绝不接收鼠标，避免挡住按钮
    f:SetAlpha(0)
    local tex = f:CreateTexture(nil, "OVERLAY")
    tex:SetAllPoints(f)
    tex:SetColorTexture(1, 0.12, 0.05)
    f:Hide()
    flash = f
    banner = CreateBanner()
end

local function PulseFlash()
    flash.t = 0
    flash:SetAlpha(0)
    flash:Show()
    flash:SetScript("OnUpdate", function(self, elapsed)
        self.t = self.t + elapsed
        if self.t >= 0.8 then
            self:SetScript("OnUpdate", nil)
            self:SetAlpha(0)
            self:Hide()
            return
        end
        -- 快速冲顶后缓慢淡出：上升沿比渐入更容易被余光捕捉
        local k = self.t < 0.2 and (self.t / 0.2) or (1 - (self.t - 0.2) / 0.6)
        self:SetAlpha(k * 0.3)
    end)
end

local function BannerText(hit)
    local key = Config.GetBindKey()
    local how = key and ("按 " .. key) or "点击【全部购买】"
    return ("%s\n%d 个合计 %s —— %s"):format(
        hit.name or Config.GetItemDisplayName(hit.itemID),
        hit.totalQuantity, coin(hit.totalPrice), how)
end

local function FireAlert(hit)
    local ok, err = pcall(function()
        EnsureAlertUI()
        if Config.IsFlashAlert() then
            PulseFlash()
            if banner then
                banner.fs:SetText(BannerText(hit))
                banner:Show()
                if bannerTimer then bannerTimer:Cancel() end
                bannerTimer = C_Timer.NewTimer(BANNER_DURATION, function() banner:Hide() end)
            end
        end
        PlayPing()
        -- 弹窗一直挂着说明人没在跟前/没看见：按间隔重播提示音直到关闭
        if Config.IsRepeatPing() then
            if pingTicker then pingTicker:Cancel() end
            pingTicker = C_Timer.NewTicker(PING_INTERVAL, PlayPing)
        end
    end)
    if not ok then
        Log(("命中提醒失败：%s"):format(tostring(err)))
    end
end

local function StopAlert()
    if pingTicker then pingTicker:Cancel(); pingTicker = nil end
    if bannerTimer then bannerTimer:Cancel(); bannerTimer = nil end
    if banner then banner:Hide() end
    if flash then
        flash:SetScript("OnUpdate", nil)
        flash:SetAlpha(0)
        flash:Hide()
    end
end

------------------------------------------------------------------------
-- 展示
------------------------------------------------------------------------
-- 刷新弹窗内容（名称/明细/诱饵标注）
local function UpdateContents(hit)
    frame.nameFS:SetText(hit.name)
    -- 单价展示取命中最低单价（多数诱饵场景），明细展示数量与总价
    local minUnit = nil
    for _, l in ipairs(hit.matched) do
        if not minUnit or l.unitPrice < minUnit then
            minUnit = l.unitPrice
        end
    end
    frame.detailFS:SetText(("单价：%s\n数量：%d\n总价：%s")
        :format(coin(math.floor(minUnit)), hit.totalQuantity, coin(hit.totalPrice)))
    if hit.isBaitLike then
        frame.baitFS:SetText("⚠ 疑似诱饵，注意秒删")
        frame.baitFS:Show()
    else
        frame.baitFS:Hide()
    end
    -- 已绑定按键时把按键写在按钮上，用户不必记命令
    local key = Config.GetBindKey()
    frame.buyBtn:SetText(key and ("全部购买 [" .. key .. "]") or "全部购买")
end

-- 挂单签名：标识一次命中对应的具体挂单集合。
-- 物品用 auctionID 集合；commodity 无 auctionID，用 单价x数量 集合。
-- 签名不变 = 还是用户已看过/已忽略的那批挂单（诱饵挂着没被买走会被反复扫到），不重复弹窗；
-- 签名变化（新挂单、部分被买走、价格变动）= 有新情况，立即重新提示。
local function HitSignature(hit)
    local parts = {}
    if hit.isCommodity then
        for _, l in ipairs(hit.matched) do
            parts[#parts + 1] = tostring(l.unitPrice) .. "x" .. tostring(l.quantity)
        end
    else
        for _, l in ipairs(hit.matched) do
            if l.auctionID then parts[#parts + 1] = tostring(l.auctionID) end
        end
    end
    table.sort(parts)
    return table.concat(parts, ",")
end

function Win.Show(hit)
    if not frame then
        local ok, err = pcall(CreateFrameUI)
        if not ok or not frame then
            Log(("狙击窗创建失败：%s"):format(tostring(err)))
            return
        end
    end
    if frame:IsShown() then
        if current and current.itemID == hit.itemID then
            -- 同一物品新命中：原地更新数据（连续轮询下价格/数量会变），不入队、不重播提示音
            current = hit
            lastShownSig[hit.itemID] = HitSignature(hit)
            UpdateContents(hit)
            if banner and banner:IsShown() then banner.fs:SetText(BannerText(hit)) end
            return
        end
        -- 不同物品：排队等待关闭后展示；同一物品只保留最新一份（防连续轮询下重复弹窗）
        for i, q in ipairs(queue) do
            if q.itemID == hit.itemID then
                table.remove(queue, i)
                break
            end
        end
        queue[#queue + 1] = hit
        return
    end
    -- 同物品挂单内容与已展示一致（用户已看过/已忽略的那批）：不重复弹窗；
    -- 内容有变（新挂单、部分被买走、价格变动）则立即重新提示
    local sig = HitSignature(hit)
    if lastShownSig[hit.itemID] == sig then
        Log(("弹窗抑制 %s（挂单内容与已展示一致，无变化）"):format(hit.itemID))
        return
    end
    current = hit
    lastShownSig[hit.itemID] = sig
    -- 数据年龄三段分解：排队（本轮起→发查询）+ 回包（发查询→读到结果）+ 上台（读到→此刻展示）
    -- "提醒晚了"的取证就靠这三个数：排队段由轮次周期决定，是最主要延迟
    local now = GetTime()
    Log(("弹窗展示 %s 门槛%.2fg 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs+上台%.1fs）"):format(
        hit.itemID, hit.threshold / 10000, hit.totalPrice / 10000,
        now - hit.roundStartAt, hit.querySendAt - hit.roundStartAt,
        hit.seenAt - hit.querySendAt, now - hit.seenAt))
    UpdateContents(hit)
    frame.mask:SetFrameLevel(frame:GetFrameLevel() - 1)
    frame:Show()
    frame.mask:Show()

    FireAlert(hit)
end

function Win.Hide()
    StopAlert()
    if frame then
        frame:Hide()
        frame.mask:Hide()
    end
    current = nil
    -- 处理排队的下一个命中
    if #queue > 0 then
        local nextHit = table.remove(queue, 1)
        C_Timer.After(0.3, function()
            -- 0.3s 窗口内同物品已有更新命中上台（current）：旧快照不再弹出，
            -- 防止旧数据覆盖 current 导致展示与购买用过期 matched/auctionID
            if current and current.itemID == nextHit.itemID then return end
            Win.Show(nextHit)
        end)
    end
end

-- 清除某物品的弹窗抑制：购买出现失败后由 BuyQueue 调用，
-- 允许下一轮扫描立即重新提示（剩余挂单可能仍可购买）
function Win.ClearSuppression(itemID)
    lastShownSig[itemID] = nil
end

-- 购买完成回调：汇总已在聊天框输出，不再自动重弹弹窗打扰
-- （弹窗空闲时什么都不做；正展示排队命中时也不追加，避免文字错位）
function Win.OnBuyFinished(summary)
    -- 仅记录日志供诊断
    Log(("购买完成 成功%d 失败%d 花费%d铜"):format(
        summary.bought, summary.failed, summary.spent or 0))
end

------------------------------------------------------------------------
-- 键盘购买
------------------------------------------------------------------------
-- SecureActionButtonTemplate + 全局命名按钮：宏 /click AHSnipeKeyBuy 与 SetBindingClick
-- 都把一次真实按键的硬件事件上下文带进 OnClick，购买 API 的受保护调用才不会被丢弃。
-- 这是插件侧合规的"按键即购买"，与外部程序注入点击无关（后者违反 ToS 且必被封号）。
-- 按钮必须保持 Show 状态（隐藏帧收不到 Click），故用 alpha 0 + 不接收鼠标做成隐形常驻。
function Win.EnsureKeyButton()
    if keyBtn then return keyBtn end
    local ok, b = pcall(CreateFrame, "Button", KEY_BTN_NAME, UIParent, "SecureActionButtonTemplate")
    if not ok or not b then
        Log(("键盘购买按钮创建失败：%s"):format(tostring(b)))
        return nil
    end
    b:SetSize(1, 1)
    b:SetPoint("CENTER")
    b:SetFrameStrata("LOW")
    -- 按键绑定派发的是 Down 还是 Up 点击在 12.x 未核实，两种都注册。
    -- 不会因此买两次：Down 那次已走 Win.Hide 清空 current，Up 那次只会记"无命中"日志。
    b:RegisterForClicks("AnyDown", "AnyUp")
    b:EnableMouse(false)
    b:SetAlpha(0)
    b:SetScript("OnClick", function() Win.TriggerBuy("按键") end)
    b:Show()
    keyBtn = b
    return b
end

function Win.KeyButtonName()
    return KEY_BTN_NAME
end

-- 绑定按键到购买按钮（持久化，登录后自动恢复）。
-- SetBinding* 在战斗中受保护：失败返回原因，由调用方提示用户。
function Win.BindKey(key)
    if not Win.EnsureKeyButton() then return false, "购买按钮未创建" end
    local ok, err = pcall(SetBindingClick, key, KEY_BTN_NAME, "LeftButton")
    if not ok then return false, tostring(err) end
    Config.SetBindKey(key)
    Log(("按键购买绑定：%s -> /click %s"):format(key, KEY_BTN_NAME))
    return true
end

function Win.UnbindKey()
    local key = Config.GetBindKey()
    if not key then return false, "未记录绑定按键" end
    local ok, err = pcall(SetBinding, key)
    if not ok then return false, tostring(err) end
    Config.SetBindKey(nil)
    Log(("按键购买解绑：%s"):format(key))
    return true
end

function Win.Init()
    -- 键盘购买按钮常驻创建（弹窗 UI 仍是延迟创建），并恢复上次保存的绑定按键
    Win.EnsureKeyButton()
    local key = Config.GetBindKey()
    if key then
        local ok, err = Win.BindKey(key)
        if not ok then
            Log(("恢复按键绑定失败 %s：%s"):format(key, tostring(err)))
        end
    end
end
