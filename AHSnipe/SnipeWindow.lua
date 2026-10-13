-- 模态狙击弹窗：物品名、单价/数量/总价、诱饵标注、【全部购买】【忽略】。
-- 多命中排队：弹窗期间新命中入队，关闭后依次处理。

local AHSnipe = AHSnipe

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
    f:Hide()

    -- 模态遮罩：点击遮罩不关闭，必须点按钮
    local mask = CreateFrame("Frame", nil, f)
    mask:SetAllPoints(UIParent)
    mask:EnableMouse(true)
    mask:SetFrameStrata("DIALOG")
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
            AHSnipe.BuyQueue.Buy(current)
        end
        Win.Hide()
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

    -- 购买结果提示
    local resultFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    resultFS:SetPoint("BOTTOM", f, "BOTTOM", 0, 52)
    resultFS:SetTextColor(0.4, 1, 0.4)
    resultFS:Hide()
    f.resultFS = resultFS

    frame = f
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
    frame.nameFS:SetText(hit.name)

    -- 单价展示取命中最低单价（多数诱饵场景），明细展示数量与总价
    local minUnit = nil
    for _, l in ipairs(hit.matched) do
        if not minUnit or l.unitPrice < minUnit then
            minUnit = l.unitPrice
        end
    end
    frame.detailFS:SetText(("单价：%s\n数量：%d\n总价：%s")
        :format(coin(minUnit), hit.totalQuantity, coin(hit.totalPrice)))

    if hit.isBaitLike then
        frame.baitFS:SetText("⚠ 疑似诱饵，注意秒删")
        frame.baitFS:Show()
    else
        frame.baitFS:Hide()
    end

    frame.resultFS:Hide()
    frame.mask:Show()
    frame:Show()

    -- 提示音
    PlaySound(SOUNDKIT and SOUNDKIT.ACCOUNT_WARNING or 882)
end

function Win.Hide()
    if frame then
        frame:Hide()
        frame.mask:Hide()
        frame.resultFS:Hide()
    end
    current = nil
    -- 处理排队的下一个命中
    if #queue > 0 then
        local nextHit = table.remove(queue, 1)
        C_Timer.After(0.3, function() Win.Show(nextHit) end)
    end
end

-- 购买完成回调：在弹窗内展示汇总
function Win.OnBuyFinished(summary)
    if not frame then return end
    local coinStr = C_CurrencyInfo.GetCoinTextureString(summary.spent or 0)
    frame.resultFS:SetText(("购买完成：成功 %d 条，失败 %d 条，花费 %s")
        :format(summary.bought, summary.failed, coinStr))
    frame.resultFS:Show()
end

function Win.Init()
    -- 无额外初始化，UI 延迟创建
end
