-- 配置面板：物品列表（启用/删除）、Shift+点击背包物品快速添加、轮间歇/折扣设置。

local AHSnipe = AHSnipe
local Config = AHSnipe.Config
local PC = AHSnipe.PriceCluster

AHSnipe.ConfigPanel = {}
local Panel = AHSnipe.ConfigPanel

local frame = nil

-- 界面步骤安全包装：出错写日志、不中断（版本变动导致 API 失效时，
-- 未捕获错误会让面板直接不出现；列表刷新失败也不挡面板显示）
local function SafeUI(desc, fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then Config.Log(("界面步骤失败 %s：%s"):format(desc, tostring(err))) end
    return ok
end

------------------------------------------------------------------------
-- 物品信息异步加载：名称/品质未缓存时请求加载，ITEM_DATA_LOAD_RESULT 回调后更新并刷新。
------------------------------------------------------------------------
local pendingInfo = {}         -- [itemID] = true 表示正在等待加载
local loadAttempts = {}        -- [itemID] = 异步加载尝试次数（超限停止，防静默死循环）
local MAX_LOAD_ATTEMPTS = 3
local infoFrame = nil

-- 材料星级（1-3）：不同星级是独立 itemID 但同名，面板必须显示星级区分。
-- API 随游戏版本有变动（2026-10-15：原 itemID 形态调用直接报错，曾把整个面板搞崩），
-- 因此逐个候选形态尝试、失败记日志（不再静默吞掉）。
-- 最终兜底：解析系统物品链接里自带的星级贴图（|A:Professions-Icon-Quality-TierN...|a，
-- 即聊天/AH 中名字后面的五角星图标），这是与系统渲染完全一致的口径。
-- 返回 tier, texture：texture 为链接内贴图名（可能为 nil）。
-- 诊断噪音控制（2026-10-16）：这些行曾在 200 条日志环里挤掉"弹窗/购买"关键取证，
-- API 形态已实证，同一形态/同一贴图组合一个会话只记第一行（保留首个 itemID 样本）。
local diagOnce = {}
local function DiagOnce(key, msg)
    if diagOnce[key] then return end
    diagOnce[key] = true
    Config.Log(msg)
end

local function QueryReagentQuality(itemID, itemLink)
    -- 链接内贴图：放宽为取链接中第一个 |A:贴图名|a 的贴图名（Midnight 起命名可能不含
    -- "Quality-Tier"，2026-10-15 日志证实旧模式匹配不到），星级数字从名字中的 TierN 解析
    local texture = itemLink and itemLink:match("|A:([^|:]+):")
    local tierFromLink = texture and tonumber(texture:match("Tier(%d)"))
    if C_TradeSkillUI and C_TradeSkillUI.GetItemReagentQualityByItemInfo then
        -- 候选1：itemID 形态（旧版可用）
        local ok, result = pcall(C_TradeSkillUI.GetItemReagentQualityByItemInfo, itemID)
        if ok then
            if type(result) == "number" and result > 0 then return result, texture end
            DiagOnce("star-id-nil", ("星级API(itemID)返回不可用 %s：%s(%s)"):format(itemID, type(result), tostring(result)))
        else
            DiagOnce("star-id-err", ("星级API(itemID)报错 %s：%s"):format(itemID, tostring(result)))
        end
        -- 候选2：itemLink 形态（版本变动后的可能签名）
        if itemLink then
            local ok2, result2 = pcall(C_TradeSkillUI.GetItemReagentQualityByItemInfo, itemLink)
            if ok2 then
                if type(result2) == "number" and result2 > 0 then return result2, texture end
                DiagOnce("star-link-nil", ("星级API(itemLink)返回不可用 %s：%s(%s)"):format(itemID, type(result2), tostring(result2)))
            else
                DiagOnce("star-link-err", ("星级API(itemLink)报错 %s：%s"):format(itemID, tostring(result2)))
            end
        end
    else
        DiagOnce("star-api-missing", "星级API不存在：C_TradeSkillUI.GetItemReagentQualityByItemInfo")
    end
    -- 候选3：链接内贴图（系统自己渲染用的就是它）
    if tierFromLink then return tierFromLink, texture end
    -- 全失败：把链接里实际出现的 |A:...|a 标记记入日志，便于核对当前版本的真实贴图命名
    DiagOnce("star-none", ("星级取不到 %s 链接标记=%s"):format(itemID, tostring(itemLink and itemLink:match("|A:[^|]-|a"))))
    return nil
end

-- 12.x 客户端已移除全局 GetItemInfo（2026-10-15 日志：nil 调用崩掉 RequestMissingInfo）。
-- 各形态的确切可用性尚未实证（2026-10-15 日志：C_Item 方法形态静默失败、零日志死循环），
-- 因此逐个尝试并在全部失败时把各形态存在性写入日志（每物品一次），定位当前版本真实 API。
local infoApiLogged = {}   -- [itemID] 失败日志去重
local function GetItemInfoCompat(itemID)
    -- 形态1：C_Item 静态调用
    if C_Item and C_Item.GetItemInfo then
        local ok, name, link, quality = pcall(C_Item.GetItemInfo, itemID)
        if ok and name then return name, link, quality end
        -- 形态1b：C_Item 方法形态（self 调用）
        local ok2, n2, l2, q2 = pcall(C_Item.GetItemInfo, C_Item, itemID)
        if ok2 and n2 then return n2, l2, q2 end
    end
    -- 形态2：Item 对象（10.x 起的面向对象口径）
    if Item and Item.CreateFromItemID then
        local ok, obj = pcall(Item.CreateFromItemID, Item, itemID)
        if ok and obj and obj.GetLink then
            local ok2, link = pcall(obj.GetLink, obj)
            if ok2 and link then
                local n = link:match("%[(.-)%]")
                if n then return n, link, nil end
            end
        end
    end
    -- 形态3：旧全局 GetItemInfo（兼容旧客户端/测试环境）
    if GetItemInfo then
        local ok, name, link, quality = pcall(GetItemInfo, itemID)
        if ok and name then return name, link, quality end
    end
    if not infoApiLogged[itemID] then
        infoApiLogged[itemID] = true
        Config.Log(("物品信息API全部失败 %s：C_Item.GetItemInfo=%s Item=%s 全局GetItemInfo=%s"):format(
            itemID, tostring(C_Item and C_Item.GetItemInfo ~= nil),
            tostring(Item and Item.CreateFromItemID ~= nil),
            tostring(GetItemInfo ~= nil)))
    end
    return nil
end

local function RequestItemInfo(itemID)
    if pendingInfo[itemID] then return end
    local name, link, quality = GetItemInfoCompat(itemID)
    if name then
        -- ★ 星级：正式服材料星级与 itemQuality 无关，用星级 API + 链接贴图兜底
        local rq, starTex = QueryReagentQuality(itemID, link)
        Config.UpdateItemInfo(itemID, name, quality, rq, starTex)
        if rq then
            -- 记录链接里的真实贴图名，核对系统当前版本用的图标资源（每种贴图组合只记一次）
            DiagOnce("iteminfo:"..tostring(starTex)..":"..tostring(rq),
                ("物品信息 %s 星级=%d 链接贴图=%s"):format(itemID, rq, tostring(starTex)))
        end
        if frame and frame:IsShown() then Panel.RefreshList() end
        return
    end
    pendingInfo[itemID] = true
    loadAttempts[itemID] = (loadAttempts[itemID] or 0) + 1
    if loadAttempts[itemID] > MAX_LOAD_ATTEMPTS then return end
    local ok, err = pcall(C_Item.RequestLoadItemDataByID, itemID)
    if not ok then
        Config.Log(("请求物品数据失败 %s：%s"):format(itemID, tostring(err)))
    end
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
    -- 同名统计：不同星级材料是独立 itemID 但同名——即使星级 API 失效，
    -- Config.GetItemDisplayName 也会按 itemID 顺序推断星级并用 ♦ 标识。
    -- 此表记录极少数仍冲突的物品在排序后的序号，用于短标识兜底。
    local nameCount = {}
    local nameRanks = {}   -- [displayName] = { [itemID] = rank }
    for _, it in ipairs(items) do
        local n = Config.GetItemDisplayName(it.itemID)
        nameCount[n] = (nameCount[n] or 0) + 1
    end
    for n, c in pairs(nameCount) do
        if c > 1 then
            local ids = {}
            for _, it2 in ipairs(items) do
                if Config.GetItemDisplayName(it2.itemID) == n then
                    ids[#ids + 1] = it2.itemID
                end
            end
            table.sort(ids)
            nameRanks[n] = {}
            for i, id in ipairs(ids) do
                nameRanks[n][id] = i
            end
        end
    end
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

        -- 物品名（品质颜色 + 星级标识，见 Config.GetItemDisplayName），过长截断；
        -- 极少数仍冲突的物品用 ★ 数量做短兜底标识，不再显示冗长 itemID
        local displayName = Config.GetItemDisplayName(it.itemID)
        local rank = nameRanks[displayName] and nameRanks[displayName][it.itemID]
        if rank then
            displayName = displayName .. " |cff808080" .. string.rep("★", rank) .. "|r"
        end
        local nameFS = row:CreateFontString(nil, "OVERLAY", "GameFontNormal")
        nameFS:SetPoint("LEFT", cb, "RIGHT", 4, 0)
        nameFS:SetWidth(300)
        nameFS:SetJustifyH("LEFT")
        nameFS:SetWordWrap(false)
        nameFS:SetText(displayName)

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
    f:SetToplevel(true)   -- 保证压在拍卖行窗口之上（层级策略随版本可能变动）
    f:SetMovable(true)
    f:EnableMouse(true)
    -- 开启鼠标滚轮：滚动列表需要窗体先接收到鼠标事件（BasicFrame 模板默认不开启）
    f:EnableMouseWheel(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    f:Hide()

    -- 标题：放进模板自带的标题栏（BasicFrameTemplate.TitleText）
    if f.TitleText then
        f.TitleText:SetText("AHSnipe 配置")
    else
        -- 老模板无 TitleText 时的回退
        local title = f:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
        title:SetPoint("TOP", 0, -12)
        title:SetText("AHSnipe 配置")
    end

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

    -- 添加提示：Shift+点击背包物品是唯一的添加方式（hook 见 HookBagClick）
    local hintFS = f:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    hintFS:SetPoint("TOPLEFT", 16, -74)
    hintFS:SetText("Shift+点击背包物品添加监控")

    -- 手动扫描：立即扫描一轮，并重置自动扫描计时
    -- 注意：从顶部锚定（TOPRIGHT），相对垂直中心的 RIGHT 锚点会把按钮落到滚动列表内被遮挡
    local scanBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    scanBtn:SetSize(90, 24)
    scanBtn:SetPoint("TOPRIGHT", f, "TOPRIGHT", -110, -74)
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
    scroll:SetPoint("TOPLEFT", 16, -128)
    scroll:SetPoint("BOTTOMRIGHT", -36, 16)
    local scrollChild = CreateFrame("Frame", nil, scroll)
    scroll:SetScrollChild(scrollChild)
    scrollChild:SetWidth(420)
    f.scrollChild = scrollChild

    frame = f
end

------------------------------------------------------------------------
-- 开/关控制（AUCTION_HOUSE_SHOW/CLOSED 事件驱动）
------------------------------------------------------------------------
function Panel.GetFrame()
    CreateFrameUI()
    return frame
end

function Panel.Open()
    if not frame then
        -- 创建失败（模板/API 版本变动）时明确提示，不再静默无界面
        local ok, err = pcall(CreateFrameUI)
        if not ok or not frame then
            Config.Log(("配置面板创建失败：%s"):format(tostring(err)))
            print("|cffFF4040AHSnipe|r: 配置面板打开失败，请 /reload 重试（详见 /ahsnipe log）。")
            return
        end
    end
    if frame:IsShown() then return end
    SafeUI("RequestMissingInfo", RequestMissingInfo)
    SafeUI("RefreshList", Panel.RefreshList)   -- 列表刷新失败不挡面板显示
    frame:Show()
end

function Panel.Close()
    if frame then
        frame:Hide()
        frame:SetUserPlaced(false)   -- 丢弃位置，下次打开回到屏幕中央
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
