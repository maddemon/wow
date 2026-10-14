-- 模态狙击弹窗：物品名、单价/数量/总价、诱饵标注、【全部购买】【忽略】。
-- 多命中排队：弹窗期间新命中入队，关闭后依次处理。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config

local Log = Config.Log   -- 诊断日志（/ahsnipe log 输出）

AHSnipe.SnipeWindow = {}
local Win = AHSnipe.SnipeWindow

local queue = {}                 -- 待展示的命中队列
local current = nil              -- 当前展示的命中
local frame = nil
local coin = function(c) return C_CurrencyInfo.GetCoinTextureString(c or 0) end

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
    buyBtn:SetScript("OnClick", function()
        if current then
            Log(("点击全部购买 %s isCommodity=%s 命中%d条"):format(
                current.itemID, tostring(current.isCommodity), #current.matched))
            AHSnipe.BuyQueue.Buy(current)
        end
        Win.Hide()  -- 购买进行中关闭弹窗，完成后由 OnBuyFinished 重新展示结果
    end)
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
-- 展示
------------------------------------------------------------------------
function Win.Show(hit)
    CreateFrameUI()
    if frame:IsShown() then
        -- 当前弹窗未处理完：排队，关闭后依次展示
        queue[#queue + 1] = hit
        return
    end
    current = hit
    Log(("弹窗展示 %s 门槛%.2fg 总价%.2fg"):format(
        hit.itemID, hit.threshold / 10000, hit.totalPrice / 10000))
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

    frame.mask:SetFrameLevel(frame:GetFrameLevel() - 1)
    frame.mask:Show()
    frame:Show()

    -- 提示音
    PlaySound(SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING or 882)
end

function Win.Hide()
    if frame then
        frame:Hide()
        frame.mask:Hide()
    end
    current = nil
    -- 处理排队的下一个命中
    if #queue > 0 then
        local nextHit = table.remove(queue, 1)
        C_Timer.After(0.3, function() Win.Show(nextHit) end)
    end
end

-- 购买完成回调：汇总已在聊天框输出，不再自动重弹弹窗打扰
-- （弹窗空闲时什么都不做；正展示排队命中时也不追加，避免文字错位）
function Win.OnBuyFinished(summary)
    -- 仅记录日志供诊断
    Log(("购买完成 成功%d 失败%d 花费%d铜"):format(
        summary.bought, summary.failed, summary.spent or 0))
end

function Win.Init()
    -- 无额外初始化，UI 延迟创建
end
