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
        local isCraftingReagent = select(17, GetItemInfo(itemID))
        Config.UpdateItemInfo(itemID, name, quality, isCraftingReagent)
        if frame and frame:IsShown() then Panel.RefreshList() end
        return
    end
    pendingInfo[itemID] = true
    C_Item.RequestLoadItemDataByID(itemID)
end

-- 批量补全：打开面板时为缺少名称的物品发起加载
local function RequestMissingInfo()
    for _, it in ipairs(Config.GetItems()) do
        if not it.name then
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
local function HookBagClick()
    hooksecurefunc("ChatEdit_InsertLink", function(link)
        if not frame or not frame:IsShown() then return end
        local itemID = link and link:match("item:(%d+)")
        if itemID then
            local id = tonumber(itemID)
            if Config.AddItem(id) then
                RequestItemInfo(id)  -- 异步加载名称/品质
            end
            Panel.RefreshList()
        end
    end)
end

------------------------------------------------------------------------
-- 刷新列表
------------------------------------------------------------------------
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

        -- 物品名（品质颜色 + 星级标识，见 Config.GetItemDisplayName）
        local nameFS = row:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        nameFS:SetPoint("LEFT", cb, "RIGHT", 4, 0)
        nameFS:SetText(Config.GetItemDisplayName(it.itemID))

        -- 门槛输入（空=自动）
        local eb = CreateFrame("EditBox", nil, row, "InputBoxTemplate")
        eb:SetSize(100, 22)
        eb:SetPoint("LEFT", nameFS, "RIGHT", 8, 0)
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
    f:Hide()

    -- 标题
    local title = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
    title:SetPoint("TOP", 0, -12)
    title:SetText("AHSnipe 配置")

    -- 全局设置
    local intervalLabel = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    intervalLabel:SetPoint("TOPLEFT", 16, -40)
    intervalLabel:SetText("扫描间隔（秒）：")
    local intervalEB = CreateFrame("EditBox", nil, f, "InputBoxTemplate")
    intervalEB:SetSize(60, 22)
    intervalEB:SetPoint("LEFT", intervalLabel, "RIGHT", 8, 0)
    intervalEB:SetAutoFocus(false)
    intervalEB:SetText(tostring(Config.GetInterval()))
    intervalEB:SetScript("OnEnterPressed", function(self)
        local n = tonumber(self:GetText())
        if n and n >= 5 then Config.SetInterval(n) end
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

    -- 手动输入物品ID添加（备选方式）
    local idLabel = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    idLabel:SetPoint("TOPLEFT", 16, -102)
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

    -- 物品列表滚动区
    local scroll = CreateFrame("ScrollFrame", nil, f, "UIPanelScrollFrameTemplate")
    scroll:SetPoint("TOPLEFT", 16, -132)
    scroll:SetPoint("BOTTOMRIGHT", -36, 16)
    local scrollChild = CreateFrame("Frame", nil, scroll)
    scroll:SetScrollChild(scrollChild)
    scrollChild:SetWidth(420)
    f.scrollChild = scrollChild

    frame = f
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
        end
    end)
end
