-- 设置面板：可配置"跟踪哪些地图"与"关键词"。替代代码写死的地图白名单，
-- 让玩家按自己所在版本/关注点挑选地图，并让名字含关键词的世界任务绕过金币门槛进矩阵。
-- 纯帧实现（不依赖 Blizzard Settings API），/wqt config 或矩阵"设置"按钮打开。
--
-- 交互约定（用户实测反馈）：
--   * 窗口可拖动；标题栏固定在顶部不随内容滚动。
--   * 窗口高度自适应内容，只有超过屏幕 85% 才出现竖向滚动。
--   * 右侧是标准可拖动滚动条，滚轮方向与矩阵保持一致（同一套手感）。

WQT.Options = {}
local Options = WQT.Options

local PANEL_W = 500
local LEFT_PAD = 14
local RIGHT_PAD = 14
local SCROLLBAR_W = 16
local SCROLLBAR_GAP = 6
local HEADER_H = 34
local BOTTOM_PAD = 12
local ROW_H = 20
local BTN_H = 22
local STEP = 30
local MAX_RATIO = 0.85
local BODY_W = PANEL_W - LEFT_PAD - RIGHT_PAD - SCROLLBAR_W - SCROLLBAR_GAP

local frame, scroll, content, scrollbar
local sec1, sec2, sec3, addCur, mapEdit, addByName, kwEdit, addKw, huntBtn, refreshBtn, kwHint
local goldEdit, goldBtn, goldHint
local secLogin, loginChk
local mapRows, kwRows = {}, {}

local function SafeCall(fn, ...)
    if not fn then return end
    local ok, err = pcall(fn, ...)
    if not ok then WQT.Config.Log("Options 出错: " .. tostring(err)) end
end

-- 地图变了要重抓池；关键词变了只需重画矩阵
local function RefreshMatrixAndPool()
    SafeCall(WQT.Sync and WQT.Sync.SnapshotNow)
    SafeCall(WQT.Matrix and WQT.Matrix.Refresh)
end

local function MapName(id)
    return (WQT.Api and WQT.Api.MapName and WQT.Api.MapName(id)) or ("地图#" .. tostring(id))
end

-- 账本里有几条任务名含这个关键词。命中来源有两条：跟踪地图的世界任务池 + 已接的任务日志
-- （城市日常/战役任务不在池里，靠日志那条收进来）。仍然是 0 条 = 这个名字既没出现在
-- 跟踪图的世界任务里，也没在任务日志里，多半是任务还没接 ——
-- 用户实测加了"符文石/追踪萨拉塔斯"两个关键词却看不到任务，分不清是哪种情况，
-- 所以把命中数直接摊在每一行后面。
local function KeywordHits(kw)
    local db = WQT.Config.DB()
    local low = (type(kw) == "string" and kw:lower()) or ""
    local n = 0
    if low == "" then return 0 end
    for questID in pairs(db and db.quests or {}) do
        local name = WQT.Ledger and WQT.Ledger.QuestName
            and WQT.Ledger.QuestName(db, questID)
        if name and tostring(name):lower():find(low, 1, true) then n = n + 1 end
    end
    return n
end

-- 滚轮：方向与矩阵一致 —— 这个客户端往下滚给的是负 delta，矩阵用的是 offY - delta*40
-- （见 Matrix.lua 的 OnMouseWheel，注释写着"加 delta 的手感是反的"，上一版这里注释照抄
-- 却写成加，两个面板方向相反）。滚动条值越大越靠下，所以这里同样减 delta。
-- 滚轮只改滚动条值，真正的滚动由 OnValueChanged 统一驱动，避免两处来回写造成抖动。
local function OnWheel(_, delta)
    if not scrollbar or not scrollbar:IsShown() then return end
    local minV, maxV = scrollbar:GetMinMaxValues()
    local v = (scrollbar:GetValue() or 0) - delta * STEP
    if v < minV then v = minV elseif v > maxV then v = maxV end
    scrollbar:SetValue(v)
end

local function PlaceRow(pool, key, y, text, btnText, enabled, onClick)
    local r = pool[key]
    if not r then
        r = CreateFrame("Frame", nil, content)
        r.label = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
        r.label:SetPoint("LEFT", r, "LEFT", 4, 0)
        r.label:SetJustifyH("LEFT")
        r.btn = CreateFrame("Button", nil, r, "UIPanelButtonTemplate")
        r.btn:SetSize(56, 18)
        r.btn:SetPoint("RIGHT", r, "RIGHT", -4, 0)
        pool[key] = r
    end
    r:ClearAllPoints()
    r:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    r:SetSize(BODY_W, ROW_H)
    r.label:SetText(text)
    if r.label.SetTextColor then
        if enabled then r.label:SetTextColor(1, 1, 1, 1) else r.label:SetTextColor(0.65, 0.65, 0.65, 1) end
    end
    if btnText then
        r.btn:Show()
        r.btn:SetText(btnText)
        r.btn:SetScript("OnClick", onClick)
        r.btn:SetScript("OnMouseWheel", OnWheel)
        if enabled then r.btn:Enable() else r.btn:Disable() end
    else
        r.btn:Hide()
    end
    r:Show()
    return r
end

local function Build()
    if frame then return end
    frame = CreateFrame("Frame", "WQTOptionsFrame", UIParent, "BackdropTemplate")
    frame:SetSize(PANEL_W, 300)
    frame:SetPoint("CENTER")
    frame:SetFrameStrata("DIALOG")
    frame:SetClampedToScreen(true)
    -- 可拖动（用户实测要求）
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
    frame:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    frame:SetBackdrop({
        -- 不透明深灰：原来的对话框纹理是半透明的，底下的矩阵会透上来（用户实测
        -- "不需要半透明，没有意义"），透明还让鼠标在设置窗里时矩阵 hover 跟着亮
        bgFile = "Interface/Buttons/WHITE8X8",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 16, insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })
    pcall(frame.SetBackdropColor, frame, 0.07, 0.07, 0.09, 1)

    -- 固定表头（不随内容滚动）
    local title = frame:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    title:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -10)
    title:SetText("世界任务追踪 · 设置")

    local closeBtn = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    closeBtn:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -4, -4)
    closeBtn:SetScript("OnClick", function() frame:Hide() end)
    -- ESC 关闭：和矩阵一样挂暴雪名单（帧必须有全局名，见 Config.RegisterEscapeClose）。
    -- 两个窗口都开着时一次 ESC 关掉几个由客户端的扫描顺序决定，本机没实测过，
    -- 不在这里猜；真不符合手感再改成只在 Open/Hide 时动态挂撤。
    WQT.Config.RegisterEscapeClose("WQTOptionsFrame")

    local line = frame:CreateTexture(nil, "ARTWORK")
    line:SetColorTexture(1, 1, 1, 0.15)
    line:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H + 4)
    line:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -RIGHT_PAD, -HEADER_H + 4)
    line:SetHeight(1)

    -- 滚动区只承载"身体"，表头在上面
    scroll = CreateFrame("ScrollFrame", "WQTOptScroll", frame)
    scroll:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -(RIGHT_PAD + SCROLLBAR_W + SCROLLBAR_GAP), BOTTOM_PAD)
    scroll:EnableMouse(true)
    scroll:SetScript("OnMouseWheel", OnWheel)
    content = CreateFrame("Frame", "WQTOptContent", scroll)
    content:SetSize(BODY_W, 1)
    content:EnableMouse(true)
    content:SetScript("OnMouseWheel", OnWheel)
    scroll:SetScrollChild(content)

    -- 标准可拖动滚动条（右侧）
    scrollbar = CreateFrame("Slider", "WQTOptScrollBar", frame, "UIPanelScrollBarTemplate")
    scrollbar:SetWidth(SCROLLBAR_W)
    scrollbar:SetPoint("TOPLEFT", scroll, "TOPRIGHT", SCROLLBAR_GAP, -16)
    scrollbar:SetPoint("BOTTOMLEFT", scroll, "BOTTOMRIGHT", SCROLLBAR_GAP, 16)
    scrollbar:SetMinMaxValues(0, 0)
    scrollbar:SetValueStep(1)
    scrollbar:SetScript("OnValueChanged", function(self, value)
        scroll:SetVerticalScroll(value)
    end)

    sec1 = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    sec1:SetText("一、跟踪地图（默认=本资料片区域；可额外关注自己的地图）")
    sec2 = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    sec2:SetText("二、关键词（任务名含任一关键词即纳入矩阵，绕过金币门槛）")

    -- 命中 0 时的解释（用户实测"加了关键词没有对应任务"却不知道为什么）
    kwHint = content:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    kwHint:SetJustifyH("LEFT")
    kwHint:SetWidth(BODY_W)
    kwHint:SetText("命中 0 = 跟踪地图里没有这个名字的任务。虚空侵袭、亚基入侵这类周常"
        .. "本来就不在世界任务池里：先加关键词，再点下面的\"补扫跟踪地图\"，"
        .. "插件把清单里每张图（默认=本资料片 + 你添加的地图）都问一遍\"这图上挂着哪些任务\"，"
        .. "名字对上的自动注册成一行 —— 不需要知道任务 ID。"
        .. "任务挂在没跟踪的图上时扫不到，站到那张图上点\"＋添加当前所在地图\"再补扫。")
    if kwHint.SetTextColor then kwHint:SetTextColor(0.75, 0.62, 0.30, 1) end
    kwHint:Hide()

    addCur = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    addCur:SetSize(180, BTN_H)
    addCur:SetText("＋添加当前所在地图")
    addCur:SetScript("OnMouseWheel", OnWheel)
    addCur:SetScript("OnClick", function()
        local id = WQT.Api and WQT.Api.CurrentMapID and WQT.Api.CurrentMapID()
        if not id then print("WQT: 取不到当前地图") return end
        local _, msg = WQT.Config.AddMap(id)
        print("WQT: " .. (msg or ""))
        RefreshMatrixAndPool(); Options.Refresh()
    end)

    mapEdit = CreateFrame("EditBox", nil, content, "InputBoxTemplate")
    mapEdit:SetSize(240, 24)
    mapEdit:SetAutoFocus(false)
    mapEdit:SetScript("OnMouseWheel", OnWheel)

    addByName = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    addByName:SetSize(120, BTN_H)
    addByName:SetText("按名称添加")
    addByName:SetScript("OnMouseWheel", OnWheel)
    addByName:SetScript("OnClick", function()
        local _, msg = WQT.Config.AddMapByName(mapEdit:GetText() or "")
        print("WQT: " .. (msg or ""))
        mapEdit:SetText("")
        RefreshMatrixAndPool(); Options.Refresh()
    end)
    mapEdit:SetScript("OnEnterPressed", function() addByName:Click() end)

    kwEdit = CreateFrame("EditBox", nil, content, "InputBoxTemplate")
    kwEdit:SetSize(240, 24)
    kwEdit:SetAutoFocus(false)
    kwEdit:SetScript("OnMouseWheel", OnWheel)

    addKw = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    addKw:SetSize(120, BTN_H)
    addKw:SetText("添加关键词")
    addKw:SetScript("OnMouseWheel", OnWheel)
    addKw:SetScript("OnClick", function()
        local _, msg = WQT.Config.AddKeyword(kwEdit:GetText() or "")
        print("WQT: " .. (msg or ""))
        kwEdit:SetText("")
        -- 先重抓再刷新面板：Options.Refresh 里的"账本命中"读的就是刚才那次快照写进账本的行，
        -- 反过来写会让用户点完仍看到 0（快照是异步的，面板却是旧数据）
        RefreshMatrixAndPool()
        Options.Refresh()
    end)
    kwEdit:SetScript("OnEnterPressed", function() addKw:Click() end)

    -- 关键词补扫：这类周常不在世界任务池里，光加关键词永远不会出现
    -- （用户实测"加了关键词没有对应任务"）—— 点一次把跟踪清单问一遍，
    -- 命中的任务自动注册成一行；不扫整棵地图树（用户口径：只扫用户添加的图 + 默认图）。
    huntBtn = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    huntBtn:SetSize(160, BTN_H)
    huntBtn:SetText("补扫跟踪地图")
    huntBtn:SetScript("OnMouseWheel", OnWheel)
    huntBtn:SetScript("OnClick", function()
        -- 判空要写在 pcall 之前：被 pcall 的 `WQT.Sync.HuntKeywordMaps` 本身是在保护
        -- **外**求值的，Sync 没加载（Core 报错中断、或用户单发这个面板）时
        -- "attempt to index a nil value" 直接炸出 OnClick，等于没护。
        if not (WQT.Sync and WQT.Sync.HuntKeywordMaps) then
            print("WQT: 同步模块没加载，补扫用不了（/wqt log 看原因）")
            return
        end
        local okCall, done, msg = pcall(WQT.Sync.HuntKeywordMaps)
        print("WQT: " .. tostring(not okCall and "补扫出错: " .. done or msg))
    end)

    -- 登录抓一次池（默认开）：不开的话登录后进矩阵只看到上个 CD 的旧板面。
    -- 代价写在文案里，让玩家自己决定：只问跟踪清单里的图，关上面板之后完全静默。
    secLogin = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    secLogin:SetText("二点五、登录时抓一次世界任务池")
    loginChk = CreateFrame("CheckButton", nil, content, "UICheckButtonTemplate")
    loginChk:SetText("登录时同步（关掉后只有打开矩阵或 /wqt sync 才更新）")
    loginChk:SetScript("OnMouseWheel", OnWheel)
    loginChk:SetScript("OnClick", function(self)
        -- GetChecked 现代客户端回布尔；1/nil 是历史形态，两种都认，
        -- 别把"点掉了却存进去还是开"这种错位留给玩家
        local v = self:GetChecked()
        WQT.Config.SetSyncOnLogin(v == true or v == 1)
        Options.Refresh()
    end)

    -- 金币门槛（12.1.5 第 15 轮反馈："出来了一堆狩猎的 >100g 的已做任务，
    -- 门槛就要提到 500g 或者可以配置"）：写死一个数等于替 14 个小号决定什么值得刷。
    -- 单位是"金"不是铜，账本里也只存金（Config.MinMatrixMoney 负责换算）。
    sec3 = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    sec3:SetText("三、金币门槛（奖励金币不低于这个数的任务才列出来，单位=金）")
    goldEdit = CreateFrame("EditBox", nil, content, "InputBoxTemplate")
    goldEdit:SetSize(70, 24)
    goldEdit:SetAutoFocus(false)
    -- SetNumeric 只影响输入框能不能敲字母，属可读性、不属正确性：门槛值最终由
    -- Config.SetMinGold 自己 tonumber 校验。12.x 有没有这个方法本机没实测过，
    -- 直接调用一旦是 nil 会把整个设置面板建坏（第十四轮矩阵整屏黑就是这个形状），
    -- 所以走 SafeCall：缺了就跳过并留一条日志，面板照常能开。
    SafeCall(goldEdit.SetNumeric, goldEdit, true)
    goldEdit:SetScript("OnMouseWheel", OnWheel)
    goldBtn = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    goldBtn:SetSize(72, BTN_H)
    goldBtn:SetText("确定")
    goldBtn:SetScript("OnMouseWheel", OnWheel)
    goldBtn:SetScript("OnClick", function()
        local ok, msg = WQT.Config.SetMinGold(goldEdit:GetText() or "")
        if not ok then
            print("WQT: 门槛没改（" .. tostring(msg) .. "），现在是 "
                .. tostring(WQT.Config.GetMinGold()) .. " 金")
            return
        end
        goldEdit:SetText("")
        print("WQT: 金币门槛已改为 " .. tostring(msg) .. " 金")
        RefreshMatrixAndPool()
        Options.Refresh()
    end)
    goldEdit:SetScript("OnEnterPressed", function() goldBtn:Click() end)
    goldHint = content:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    goldHint:SetJustifyH("LEFT")
    goldHint:SetWidth(300)

    -- 手动刷新（用户提的）：改完地图/关键词想立刻看矩阵效果，不该靠关窗重开或重登。
    -- 挂在补扫右边、跟着它走，所以只在这里锚一次，下面的重排不用管它。
    refreshBtn = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    refreshBtn:SetSize(100, BTN_H)
    refreshBtn:SetText("刷新")
    refreshBtn:SetPoint("LEFT", huntBtn, "RIGHT", 6, 0)
    refreshBtn:SetScript("OnMouseWheel", OnWheel)
    refreshBtn:SetScript("OnClick", function()
        RefreshMatrixAndPool()
        Options.Refresh()
        print("WQT: 已刷新（重抓池子并重画矩阵）")
    end)
end

function Options.Refresh()
    if not frame then return end
    local base = (WQT.Api and WQT.Api.MidnightMapIDs and WQT.Api.MidnightMapIDs()) or {}
    local db = WQT.Config.DB()
    local extra = (db and db.config and db.config.extraMaps) or {}

    -- 默认地图：按"名字"去重。Api 返回的多个 mapID 可能是同一 zone 的不同层级图
    -- （父子图同名），直接列出会出现"永歌森林 / 永歌森林"这种重复（用户实测）。
    local baseNames, baseList = {}, {}
    for i = 1, #base do
        local id = base[i]
        local name = MapName(id)
        if not baseNames[name] then
            baseNames[name] = true
            baseList[#baseList + 1] = { id = id, name = name }
        end
    end

    -- 额外地图：按名字排序 + 去重；与默认同名的直接略过（已经在跟踪，避免重复显示）
    local extraList = {}
    for id in pairs(extra) do
        extraList[#extraList + 1] = { id = id, name = MapName(id) }
    end
    table.sort(extraList, function(a, b)
        if a.name == b.name then return a.id < b.id end
        return a.name < b.name
    end)
    local extraShown, extraSeen = {}, {}
    for i = 1, #extraList do
        local e = extraList[i]
        if not baseNames[e.name] and not extraSeen[e.name] then
            extraSeen[e.name] = true
            extraShown[#extraShown + 1] = e
        end
    end

    local y, n = 0, 0
    sec1:ClearAllPoints()
    sec1:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + 22

    for i = 1, #baseList do
        n = n + 1
        PlaceRow(mapRows, n, y, baseList[i].name .. "  (默认)", "默认", false, nil)
        y = y + ROW_H
    end
    for i = 1, #extraShown do
        n = n + 1
        local e = extraShown[i]
        PlaceRow(mapRows, n, y, e.name, "移除", true, function()
            WQT.Config.RemoveMap(e.id)
            Options.Refresh(); RefreshMatrixAndPool()
        end)
        y = y + ROW_H
    end
    for i = n + 1, #mapRows do mapRows[i]:Hide() end

    addCur:ClearAllPoints()
    addCur:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + BTN_H + 8

    mapEdit:ClearAllPoints()
    mapEdit:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    addByName:ClearAllPoints()
    addByName:SetPoint("LEFT", mapEdit, "RIGHT", 6, 0)
    y = y + 28 + 14

    sec2:ClearAllPoints()
    sec2:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + 22

    local kws = WQT.Config.GetKeywords()
    local kn = 0
    local anyMiss = false
    for i = 1, #kws do
        kn = kn + 1
        local kw = kws[i]
        local hits = KeywordHits(kw)
        if hits == 0 then anyMiss = true end
        -- 命中数摊在关键词后面：0 = 账本里没有这个名字的世界任务（看图没跟踪到，
        -- 或它不是世界任务），不是"插件坏了"（用户实测分不清这两种）
        PlaceRow(kwRows, kn, y, kw .. "  （账本命中 " .. tostring(hits) .. "）", "移除", true, function()
            WQT.Config.RemoveKeyword(kw)
            -- 由这个关键词注册出来的周常行要跟着退：不删掉 regKey，
            -- 它会顶着"每轮快照重问状态"的豁免一直占一行，可玩家已经说不要这个名字了
            SafeCall(WQT.Sync and WQT.Sync.UnregisterKeyword, kw)
            Options.Refresh()
            SafeCall(WQT.Matrix and WQT.Matrix.Refresh)
        end)
        y = y + ROW_H
    end
    for i = kn + 1, #kwRows do kwRows[i]:Hide() end

    if anyMiss then
        kwHint:ClearAllPoints()
        kwHint:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
        kwHint:Show()
        y = y + 30
    else
        kwHint:Hide()
    end

    kwEdit:ClearAllPoints()
    kwEdit:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    addKw:ClearAllPoints()
    addKw:SetPoint("LEFT", kwEdit, "RIGHT", 6, 0)
    y = y + 28 + 6

    huntBtn:ClearAllPoints()
    huntBtn:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + BTN_H + 14

    secLogin:ClearAllPoints()
    secLogin:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + 22
    loginChk:ClearAllPoints()
    loginChk:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + ROW_H + 14

    sec3:ClearAllPoints()
    sec3:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    y = y + 22
    goldEdit:ClearAllPoints()
    goldEdit:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    goldBtn:ClearAllPoints()
    goldBtn:SetPoint("LEFT", goldEdit, "RIGHT", 6, 0)
    goldHint:ClearAllPoints()
    goldHint:SetPoint("LEFT", goldBtn, "RIGHT", 8, 0)
    -- 门槛现值摊在面板上：/wqt quest 的"金币也没到 X"报的是同一个数，
    -- 两边不一致时用户就没法对账了
    goldHint:SetText("现在 = " .. tostring(WQT.Config.GetMinGold()) .. " 金\n"
        .. "本轮有角色交过的任务不看门槛，直接列出来（12 小时区换了一条实例就不算本轮）")
    y = y + 28 + 6

    local contentH = math.max(y, 1)
    content:SetSize(BODY_W, contentH)

    -- 自适应高度：内容多高窗口多高，但不超过屏幕 85%
    local screenH = 720
    if UIParent and UIParent.GetHeight then
        local okH, h = pcall(UIParent.GetHeight, UIParent)
        if okH and tonumber(h) and tonumber(h) > 0 then screenH = tonumber(h) end
    end
    local maxH = math.floor(screenH * MAX_RATIO)
    local chrome = HEADER_H + BOTTOM_PAD
    local frameH = math.min(chrome + contentH, maxH)
    if frameH < chrome + 80 then frameH = chrome + 80 end
    frame:SetHeight(frameH)

    -- 超出可视区才显示滚动条
    local viewH = frameH - chrome
    local maxScroll = math.max(0, contentH - viewH)
    if maxScroll > 0.5 then
        scrollbar:Show()
        scrollbar:SetMinMaxValues(0, maxScroll)
        local v = scrollbar:GetValue() or 0
        if v > maxScroll then v = maxScroll end
        scrollbar:SetValue(v)
    else
        scrollbar:Hide()
        scrollbar:SetMinMaxValues(0, 0)
        scrollbar:SetValue(0)
        scroll:SetVerticalScroll(0)
    end

    -- 勾选态跟着存档走：面板开着时别的号/命令改过开关，这里读的是唯一真源
    if loginChk then loginChk:SetChecked(WQT.Config.SyncOnLogin() == true) end
end

function Options.Open()
    Build()
    Options.Refresh()
    if scrollbar then scrollbar:SetValue(0) end
    if scroll then scroll:SetVerticalScroll(0) end
    frame:Show()
end
