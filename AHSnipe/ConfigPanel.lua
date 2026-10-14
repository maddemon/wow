-- 配置面板：物品列表（门槛/启用/删除）、添加悬停物品、Shift+点击背包物品快速添加、间隔/折扣设置。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local PC = AHSnipe.PriceCluster

AHSnipe.ConfigPanel = {}
local Panel = AHSnipe.ConfigPanel

local frame = nil
local lastHoverItemID = nil   -- 最近悬停物品（供"添加悬停物品"按钮使用）

------------------------------------------------------------------------
-- 物品信息异步加载：名称/品质未缓存时请求加载，ITEM_DATA_LOAD_RESULT 回调后更新并刷新。
------------------------------------------------------------------------
local pendingInfo = {}         -- [itemID] = true 表示正在等待加载
local infoFrame = nil

local function RequestItemInfo(itemID)
    if pendingInfo[itemID] then return end
    local name, _, quality = GetItemInfo(itemID)
    if name then
        -- ★ 星级：正式服材料星级与 itemQuality 无关，必须用 reagent quality API 取 1-3
        local rq = C_TradeSkillUI and C_TradeSkillUI.GetItemReagentQualityByItemInfo
            and C_TradeSkillUI.GetItemReagentQualityByItemInfo(itemID)
        Config.UpdateItemInfo(itemID, name, quality, rq)
        if frame and frame:IsShown() then Panel.RefreshList() end
        return
    end
    pendingInfo[itemID] = true
    C_Item.RequestLoadItemDataByID(itemID)
end

-- 批量补全：打开面板时为缺少名称或星级的物品发起加载
local function RequestMissingInfo()
    for _, it in ipairs(Config.GetItems()) do
        if not it.name or it.reagentQuality == nil then
            RequestItemInfo(it.itemID)
        end
    end
end

------------------------------------------------------------------------
-- 解析金额输入：支持 "6g50s"、"6g"、"50s" 或纯数字（按金处理），返回铜。
------------------------------------------------------------------------
local function ParseMoney(text)
    if not text or text == "" then return nil end
    if text:match("^%d+$") then
        return tonumber(text) * 10000
    end
    local g, s = text:match("^(%d+)g(%d*)s?$")
    if g then
        return tonumber(g) * 10000 + (tonumber(s) or 0) * 100
    end
    local onlyS = text:match("^(%d+)s$")
    if onlyS then
        return tonumber(onlyS) * 100
    end
    return nil
end

------------------------------------------------------------------------
-- 悬停跟踪
------------------------------------------------------------------------
local function TrackHover()
    TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Item, function(tooltip, data)
        if tooltip ~= GameTooltip then return end
        if type(data.id) == "number" then
            lastHoverItemID = data.id
        end
    end)
end

-- Shift+点击背包物品快速添加
-- 12.x 背包 Shift+点击走 ChatFrameUtil.InsertLink（AH 打开时内部直接设 AH 搜索框并返回，
-- 旧函数 ChatEdit_InsertLink 不再被调用），故 hook 新函数。
local function HookBagClick()
    if ChatFrameUtil and ChatFrameUtil.InsertLink then
        hooksecurefunc(ChatFrameUtil, "InsertLink", function(link)
            if not frame or not frame:IsShown() then return end
            local itemID = link and link:match("item:(%d+)")
            if itemID then
                local id = tonumber(itemID)
                if Config.AddItem(id) then
                    RequestItemInfo(id)  -- 异步加载名称/品质
                    print(("|cff33FF99AHSnipe|r: 已添加监控：%s"):format(Config.GetItemDisplayName(id)))
                end
                Panel.RefreshList()
            end
        end)
    end
end

------------------------------------------------------------------------
-- 刷新列表
------------------------------------------------------------------------
local priceFSs = {}       -- [itemID] = 价格列 FontString（供扫描后轻量更新）

function Panel.RefreshList()
    if not frame then return end
    local scrollChild = frame.scrollChild
    -- 清空旧行
    for _, child in ipairs({ scrollChild:GetChildren() }) do
        if child.itemRow then
            child:Hide()
            child:SetParent(nil)
        end
    end
    priceFSs = {}

    local items = Config.GetItems()
    local y = 0
    for _, it in ipairs(items) do
        local row = CreateFrame("Frame", nil, scrollChild)
        row.itemRow = true
        row:SetSize(420, 24)
        row:SetPoint("TOPLEFT", 0, -y)

        -- 启用开关
        local cb = CreateFrame("CheckButton", nil, row, "UICheckButtonTemplate")
        cb:SetSize(24, 24)
        cb:SetPoint("LEFT", 4, 0)
        cb:SetChecked(it.enabled)
        cb:SetScript("OnClick", function(self)
            Config.SetEnabled(it.itemID, self:GetChecked())
        end)

        -- 物品名（品质颜色 + 星级标识，见 Config.GetItemDisplayName），过长截断
        local nameFS = row:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        nameFS:SetPoint("LEFT", cb, "RIGHT", 4, 0)
        nameFS:SetWidth(112)
        nameFS:SetJustifyH("LEFT")
        nameFS:SetWordWrap(false)
        nameFS:SetText(Config.GetItemDisplayName(it.itemID))

        -- 上次扫描最低价快照（扫描中实时刷新）
        local lastPrice = Config.GetLastPrice(it.itemID)
        local priceFS = row:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        priceFS:SetPoint("LEFT", nameFS, "RIGHT", 6, 0)
        priceFS:SetWidth(86)
        priceFS:SetJustifyH("RIGHT")
        if lastPrice and lastPrice.minUnit then
            priceFS:SetText(("%.2fg"):format(lastPrice.minUnit / 10000))
        else
            priceFS:SetText("|cff808080--|r")
        end
        priceFSs[it.itemID] = priceFS

        -- 门槛输入（空=自动）
        local eb = CreateFrame("EditBox", nil, row, "InputBoxTemplate")
        eb:SetSize(80, 22)
        eb:SetPoint("LEFT", priceFS, "RIGHT", 6, 0)
        eb:SetAutoFocus(false)
        if it.threshold then
            eb:SetText(("%.2fg"):format(it.threshold / 10000))
        else
            eb:SetText("")
        end
        eb:SetScript("OnEnterPressed", function(self)
            local text = self:GetText()
            if text == "" then
                -- 空输入：恢复自动计算
                Config.SetThreshold(it.itemID, nil)
                self:SetText("")
            else
                local copper = ParseMoney(text)
                if copper then
                    Config.SetThreshold(it.itemID, copper)
                    self:SetText(("%.2fg"):format(copper / 10000))
                else
                    -- 非法输入：保留原值并提示，不清空已有配置
                    print("|cffFF4040AHSnipe|r: 金额格式无效，支持如 6g50s / 6g / 50s / 纯数字(按金)。")
                    if it.threshold then
                        self:SetText(("%.2fg"):format(it.threshold / 10000))
                    else
                        self:SetText("")
                    end
                end
            end
            self:ClearFocus()
        end)
        eb:SetScript("OnEscapePressed", function(self) self:ClearFocus() end)

        -- 自动门槛提示
        if not it.threshold then
            local hint = row:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
            hint:SetPoint("LEFT", eb, "RIGHT", 6, 0)
            hint:SetText("(自动)")
        end

        -- 删除按钮
        local delBtn = CreateFrame("Button", nil, row, "UIPanelButtonTemplate")
        delBtn:SetSize(52, 20)
        delBtn:SetPoint("RIGHT", row, "RIGHT", -4, 0)
        delBtn:SetText("删除")
        delBtn:SetScript("OnClick", function()
            Config.RemoveItem(it.itemID)
            Panel.RefreshList()
        end)

        y = y + 28
    end

    scrollChild:SetHeight(math.max(y, 120))
end

------------------------------------------------------------------------
-- 创建 UI
------------------------------------------------------------------------
local function CreateFrameUI()
    if frame then return end

    local f = CreateFrame("Frame", "AHSnipeConfigFrame", UIParent, "BasicFrameTemplateWithInset")
    f:SetSize(460, 420)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:SetMovable(true)
    f:EnableMouse(true)
    -- 开启鼠标滚轮：滚动列表需要窗体先接收到鼠标事件（BasicFrame 模板默认不开启）
    f:EnableMouseWheel(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    f:Hide()

    -- 标题
    local title = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
    title:SetPoint("TOP", 0, -12)
    title:SetText("AHSnipe 配置")

    -- 全局设置
    local intervalLabel = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    intervalLabel:SetPoint("TOPLEFT", 16, -40)
    intervalLabel:SetText("轮间歇（秒，0=连续）：")
    local intervalEB = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
    intervalEB:SetSize(60, 22)
    intervalEB:SetPoint("LEFT", intervalLabel, "RIGHT", 8, 0)
    intervalEB:SetAutoFocus(false)
    intervalEB:SetText(tostring(Config.GetInterval()))
    intervalEB:SetScript("OnEnterPressed", function(self)
        local n = tonumber(self:GetText())
        -- 0=连续轮询（节流闸门天然限速），负数非法
        if n and n >= 0 then Config.SetInterval(n) end
        self:SetText(tostring(Config.GetInterval()))
        self:ClearFocus()
    end)
    intervalEB:SetScript("OnEscapePressed", function(self) self:ClearFocus() end)

    local discountLabel = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    discountLabel:SetPoint("LEFT", intervalLabel, "RIGHT", 160, 0)
    discountLabel:SetText("折扣系数：")
    local discountEB = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
    discountEB:SetSize(60, 22)
    discountEB:SetPoint("LEFT", discountLabel, "RIGHT", 8, 0)
    discountEB:SetAutoFocus(false)
    discountEB:SetText(tostring(Config.GetDiscount()))
    discountEB:SetScript("OnEnterPressed", function(self)
        local n = tonumber(self:GetText())
        if n and n > 0 and n < 1 then Config.SetDiscount(n) end
        self:SetText(tostring(Config.GetDiscount()))
        self:ClearFocus()
    end)
    discountEB:SetScript("OnEscapePressed", function(self) self:ClearFocus() end)

    -- 添加按钮行
    local hoverBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    hoverBtn:SetSize(140, 24)
    hoverBtn:SetPoint("TOPLEFT", 16, -74)
    hoverBtn:SetText("添加悬停物品")
    hoverBtn:SetScript("OnClick", function()
        if lastHoverItemID then
            if Config.AddItem(lastHoverItemID) then
                RequestItemInfo(lastHoverItemID)  -- 异步加载名称/品质
            end
            Panel.RefreshList()
        else
            print("|cffFF4040AHSnipe|r: 先把鼠标悬停到物品上再点击。")
        end
    end)

    local hintFS = f:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    hintFS:SetPoint("LEFT", hoverBtn, "RIGHT", 8, 0)
    hintFS:SetText("或 Shift+点击背包物品")

    -- 从背包选择可拍卖物品（弹出选择列表）
    local bagBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    bagBtn:SetSize(110, 24)
    bagBtn:SetPoint("TOPLEFT", 16, -102)
    bagBtn:SetText("从背包选择")
    bagBtn:SetScript("OnClick", function()
        Panel.ToggleBagPicker()
    end)

    -- 手动输入物品ID添加（备选方式）
    local idLabel = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    idLabel:SetPoint("LEFT", bagBtn, "RIGHT", 16, 0)
    idLabel:SetText("输入物品ID：")
    local idEB = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
    idEB:SetSize(90, 22)
    idEB:SetPoint("LEFT", idLabel, "RIGHT", 4, 0)
    idEB:SetAutoFocus(false)
    idEB:SetScript("OnEscapePressed", function(self) self:ClearFocus() end)
    idEB:SetScript("OnEnterPressed", function(self)
        local id = tonumber(self:GetText():match("%d+"))
        if id then
            if Config.AddItem(id) then
                RequestItemInfo(id)
            end
            self:SetText("")
            Panel.RefreshList()
        else
            print("|cffFF4040AHSnipe|r: 请输入有效数字物品ID。")
        end
        self:ClearFocus()
    end)

    -- 手动扫描：立即扫描一轮，并重置自动扫描计时
    -- 注意：从顶部锚定（TOPRIGHT），相对垂直中心的 RIGHT 锚点会把按钮落到滚动列表内被遮挡
    local scanBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    scanBtn:SetSize(90, 24)
    scanBtn:SetPoint("TOPRIGHT", f, "TOPRIGHT", -110, -102)
    scanBtn:SetText("手动扫描")
    scanBtn:SetScript("OnClick", function()
        if AHSnipe.Scanner and AHSnipe.Scanner.ScanNow() then
            print("|cff33FF99AHSnipe|r: 已立即扫描，自动计时从现在重新开始。")
        else
            print("|cffFF4040AHSnipe|r: 扫描未启用（需先打开拍卖行，或已暂停）。")
        end
    end)

    -- 暂停/继续：暂停后 AH 打开也不再扫描（持久化，跨会话保持）
    local pauseBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    pauseBtn:SetSize(68, 24)
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

    -- 物品列表滚动区
    local scroll = CreateFrame("ScrollFrame", nil, f, "UIPanelScrollFrameTemplate")
    scroll:SetPoint("TOPLEFT", 16, -134)
    scroll:SetPoint("BOTTOMRIGHT", -36, 16)
    local scrollChild = CreateFrame("Frame", nil, scroll)
    scroll:SetScrollChild(scrollChild)
    scrollChild:SetWidth(420)
    f.scrollChild = scrollChild

    frame = f
end

-- 仅更新价格列（扫描中调用，避免重建列表打断用户输入门槛）
function Panel.UpdatePrices(itemID)
    if not frame or not frame:IsShown() then return end
    local function Apply(id)
        local fs = priceFSs[id]
        if not fs then return end
        local lastPrice = Config.GetLastPrice(id)
        if lastPrice and lastPrice.minUnit then
            fs:SetText(("%.2fg"):format(lastPrice.minUnit / 10000))
        else
            fs:SetText("|cff808080--|r")
        end
    end
    if itemID then
        Apply(itemID)
    else
        for id in pairs(priceFSs) do
            Apply(id)
        end
    end
end

------------------------------------------------------------------------
-- 背包可拍卖物品选择器：遍历背包与材料包，列出已能识别的可拍卖物品，点击即添加监控。
------------------------------------------------------------------------
-- 可拍卖判定：直接用 GetContainerItemInfo 返回的 isBound（官方字段，无需异步加载）
-- 按格过滤：同一物品存在已绑定/未绑定堆叠时，仅排除已绑定的堆叠
local function CollectBagItems()
    local found = {}    -- [itemID] = totalQuantity
    local order = {}    -- 物品ID出现顺序
    for bagIndex = Enum.BagIndex.Backpack, Enum.BagIndex.ReagentBag do
        local numSlots = C_Container.GetContainerNumSlots(bagIndex) or 0
        for slot = 1, numSlots do
            local info = C_Container.GetContainerItemInfo(bagIndex, slot)
            if info and info.itemID and not info.isBound then
                local itemID = info.itemID
                if not found[itemID] then
                    found[itemID] = 0
                    order[#order + 1] = itemID
                end
                found[itemID] = found[itemID] + (info.stackCount or 1)
            end
        end
    end
    local list = {}
    for _, itemID in ipairs(order) do
        list[#list + 1] = { itemID = itemID, count = found[itemID] }
    end
    return list
end

local bagListFrame = nil

local function FillBagList()
    if not bagListFrame then return end
    -- 填充列表
    local scrollChild = bagListFrame.scrollChild
    for _, child in ipairs({ scrollChild:GetChildren() }) do
        if child.bagRow then
            child:Hide()
            child:SetParent(nil)
        end
    end
    local list = CollectBagItems()
    local y = 0
    if #list == 0 then
        local emptyFS = scrollChild:CreateFontString(nil, "OVERLAY", "GameFontDisable")
        emptyFS.bagRow = true   -- 标记，便于下次填充时清理
        emptyFS:SetPoint("TOPLEFT", 8, -4)
        emptyFS:SetText("背包中没有可拍卖的未绑定物品。")
    end
    for _, entry in ipairs(list) do
        local itemID = entry.itemID
        local row = CreateFrame("Button", nil, scrollChild)
        row.bagRow = true
        row:SetSize(260, 22)
        row:SetPoint("TOPLEFT", 0, -y)
        row:EnableMouse(true)

        local already = Config.GetItem(itemID) ~= nil
        local fs = row:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        fs:SetPoint("LEFT", 4, 0)
        fs:SetWidth(248)
        fs:SetJustifyH("LEFT")
        fs:SetWordWrap(false)
        -- 品质颜色优先取 GetItemInfo，未加载时异步补全
        local name, _, quality = GetItemInfo(itemID)
        if name then
            -- ★ 星级：与 RequestItemInfo 同源，用 reagent quality API 而非 itemQuality
            local rq = C_TradeSkillUI and C_TradeSkillUI.GetItemReagentQualityByItemInfo
                and C_TradeSkillUI.GetItemReagentQualityByItemInfo(itemID)
            Config.UpdateItemInfo(itemID, name, quality, rq)
            fs:SetText(Config.GetItemDisplayName(itemID))
            if already then
                fs:SetText(fs:GetText() .. " |cff808080(已监控)|r")
            end
        else
            fs:SetText("|cff808080加载中...|r")
            C_Item.RequestLoadItemDataByID(itemID)
        end

        local countFS = row:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
        countFS:SetPoint("RIGHT", row, "RIGHT", -4, 0)
        countFS:SetText("x" .. entry.count)

        row:SetScript("OnClick", function()
            if Config.AddItem(itemID) then
                RequestItemInfo(itemID)
                print(("|cff33FF99AHSnipe|r: 已添加监控：%s"):format(Config.GetItemDisplayName(itemID)))
            end
            Panel.RefreshList()
            Panel.ToggleBagPicker()  -- 关闭选择器，回到主列表
        end)
        row:SetScript("OnEnter", function(self)
            GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
            GameTooltip:SetItemByID(itemID)
            GameTooltip:Show()
        end)
        row:SetScript("OnLeave", function() GameTooltip:Hide() end)

        y = y + 24
    end
    scrollChild:SetHeight(math.max(y, 120))
end

function Panel.ToggleBagPicker()
    if bagListFrame and bagListFrame:IsShown() then
        bagListFrame:Hide()
        return
    end
    CreateFrameUI()

    if not bagListFrame then
        local bf = CreateFrame("Frame", "AHSnipeBagPickerFrame", frame, "BasicFrameTemplateWithInset")
        bf:SetSize(300, 360)
        bf:SetFrameStrata("DIALOG")
        bf:SetMovable(true)
        bf:EnableMouse(true)
        bf:EnableMouseWheel(true)
        bf:RegisterForDrag("LeftButton")
        bf:SetScript("OnDragStart", function(self) self:StartMoving() end)
        bf:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
        bf:Hide()

        local bfTitle = bf:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
        bfTitle:SetPoint("TOP", 0, -12)
        bfTitle:SetText("背包可拍卖物品")

        local hint = bf:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
        hint:SetPoint("TOP", bfTitle, "BOTTOM", 0, -4)
        hint:SetText("点击条目即添加监控")

        local scroll = CreateFrame("ScrollFrame", nil, bf, "UIPanelScrollFrameTemplate")
        scroll:SetPoint("TOPLEFT", 8, -44)
        scroll:SetPoint("BOTTOMRIGHT", -28, 8)
        local scrollChild = CreateFrame("Frame", nil, scroll)
        scroll:SetScrollChild(scrollChild)
        scrollChild:SetWidth(260)
        bf.scrollChild = scrollChild

        bagListFrame = bf
    end

    FillBagList()

    -- 按屏幕剩余空间选边：面板贴近右边缘时选择器放左侧，避免被推出屏幕
    local left = frame:GetLeft()
    local right = frame:GetRight()
    local sLeft = UIParent:GetLeft()
    local sRight = UIParent:GetRight()
    if left and right and sLeft and sRight and right + 308 <= sRight then
        bagListFrame:ClearAllPoints()
        bagListFrame:SetPoint("LEFT", frame, "RIGHT", 4, 0)
    elseif left and right and sLeft and left - 308 >= sLeft then
        bagListFrame:ClearAllPoints()
        bagListFrame:SetPoint("RIGHT", frame, "LEFT", -4, 0)
    else
        -- 两侧都不够：居中叠加在面板上
        bagListFrame:ClearAllPoints()
        bagListFrame:SetPoint("CENTER", frame, "CENTER", 0, 0)
    end

    bagListFrame:Show()
    bagListFrame:Raise()
end

------------------------------------------------------------------------
-- 开/关控制（AUCTION_HOUSE_SHOW/CLOSED 事件驱动）
------------------------------------------------------------------------
function Panel.GetFrame()
    CreateFrameUI()
    return frame
end

function Panel.Open()
    CreateFrameUI()
    if frame:IsShown() then return end
    RequestMissingInfo()
    Panel.RefreshList()
    frame:Show()
end

function Panel.Close()
    if frame then
        frame:Hide()
        frame:SetUserPlaced(false)   -- 丢弃位置，下次打开回到屏幕中央
    end
    if bagListFrame then
        bagListFrame:Hide()
    end
end

function Panel.Toggle()
    CreateFrameUI()
    if frame:IsShown() then
        frame:Hide()
    else
        RequestMissingInfo()   -- 打开时补全缺失的物品名称/品质
        Panel.RefreshList()
        frame:Show()
    end
end

function Panel.Init()
    TrackHover()
    HookBagClick()

    -- 物品数据加载完成回调：更新信息并刷新列表
    infoFrame = CreateFrame("Frame", "AHSnipeInfoFrame")
    infoFrame:RegisterEvent("ITEM_DATA_LOAD_RESULT")
    infoFrame:SetScript("OnEvent", function(self, event, itemID, success)
        pendingInfo[itemID] = nil
        if success then
            RequestItemInfo(itemID)  -- GetItemInfo 现已可用，写入并刷新
            -- 背包选择器开着时同步重绘，替换"加载中..."占位
            if bagListFrame and bagListFrame:IsShown() then
                FillBagList()
            end
        end
    end)
end
