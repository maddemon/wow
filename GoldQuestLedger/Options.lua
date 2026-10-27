-- 设置面板 + "关键词 · 屏蔽管理"两栏窗口。
-- 设置面板（/gql config 或矩阵"设置"按钮打开）只管一件事：跟踪哪些地图。
-- "登录时抓一次世界任务池"那颗勾 2026-10-27 整段撤下（用户口径："默认就抓取 不用设置"），
-- 数据层没动：db.config.syncOnLogin 照旧读，登录照旧抓，想关只能改存档。
-- 关键词段/金币门槛段/面板内"刷新"按钮 2026-10-27 整体撤下（用户口径四）：
--   * 关键词的加/删/补扫搬进两栏窗口左栏（入口挪走，不是功能废了）；
--   * 金币门槛只是暂时不能从 UI 改，db.config.minGold 仍是唯一真源，
--     Config.SetMinGold/GetMinGold/MinMatrixMoney 三个函数与判据一个都不改；
--   * "刷新"入口矩阵标题栏早有同一颗按钮（用例 matrix_refresh_button_sits_by_settings 钉住）。
-- 纯帧实现（不依赖 Blizzard Settings API）。
--
-- 交互约定（用户实测反馈）：
--   * 窗口可拖动；标题栏固定在顶部不随内容滚动。
--   * 设置面板高度自适应内容，只有超过屏幕 85% 才出现竖向滚动。
--   * 右侧是标准可拖动滚动条，滚轮方向与矩阵保持一致（同一套手感）。

GQL.Options = {}
local Options = GQL.Options

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
-- 两栏窗口的列宽（BuildPanel 建滚动区、两处行池 SetSize、Refresh 重算内容高度都读同一对数）
local KW_LEFT_W, KW_RIGHT_W = 250, 350

local frame, scroll, content, scrollbar
local sec1, addCur, mapEdit, addByName
local mapRows = {}

local function SafeCall(fn, ...)
    if not fn then return end
    local ok, err = pcall(fn, ...)
    if not ok then GQL.Config.Log("Options 出错: " .. tostring(err)) end
end

-- 地图变了要重抓池；关键词变了只需重画矩阵
local function RefreshMatrixAndPool()
    SafeCall(GQL.Sync and GQL.Sync.SnapshotNow)
    SafeCall(GQL.Matrix and GQL.Matrix.Refresh)
end

local function MapName(id)
    return (GQL.Api and GQL.Api.MapName and GQL.Api.MapName(id)) or ("地图#" .. tostring(id))
end

--- 名字命中关键词、而且这个窗口「真管得着」的任务集合：
--- 命中来源两条都收：跟踪地图的世界任务池 + 已接的任务日志（城市日常/战役任务不在池里，
--- 靠账本那条收进来）；池里刚上板、账本还没落行的任务也要能点名到 —— 右栏管的就是这个。
--- 匹配口径 = 大小写不敏感**纯子串**：和 Config.KeywordHit 一样 find(low, 1, true)，
--- 不许用 Lua pattern —— 关键词里可能带 % 等魔法字符，pattern 匹配会把用户的话理解错。
--- 命中之后过两道过滤、再按名字去重（第 32 轮，计划 §2.2）：以前这里只按 questID 去重，
--- 既不看凭据也不看到期，同一个任务的新旧两个号各列一行 —— 用户实测"好几个特别任务
--- 都是重复的，不光怨灵之怒"（神盾密探 93244/94795、怨灵之怒 95918/96307、击退圣光
--- 93013/94391），而多出来的那条都是已过期、零客户端凭据的旧壳。矩阵一直按名字去重、
--- 只列一条，两处口径从此并成一条真源。**"同名多条"本身不是 bug**（轮换型任务每周换
--- ID 是实测事实），bug 从来是右栏用了另一套判据。
--- 这一栏不套矩阵的金币门槛（minGold）：门槛判的是"哪条值得刷"，这一栏管的是屏蔽/找回，
--- 拿它筛会把玩家明确点名要屏蔽的行藏掉。
function Options.KeywordMatches(kw)
    local list = {}
    local low = (type(kw) == "string" and tostring(kw):lower()) or ""
    if low == "" then return list end
    local db = GQL.Config.DB()
    -- 名字统一走 Ledger.QuestName（账本优先、问出就缓存回记录）；它问不出时回的是
    -- 裸 ID 字符串，那种行如果自带池里的名字就用池的，否则右栏会出现"190"这种玩家读不懂的行。
    local function nameOf(qid, poolName)
        local nm = GQL.Ledger and GQL.Ledger.QuestName and GQL.Ledger.QuestName(db, qid)
        if type(nm) == "string" and nm ~= tostring(qid) then return nm end
        return poolName or nm
    end
    -- 到期门用的"现在"：一次调用只读一次（同一屏不许混两种时钟口径）。
    -- 墙上时钟优先、问不到退回开机秒 —— 与 Track 的 NowSeconds、矩阵悬停同一条读法。
    -- 两个都问不出（now 不是数）就**只跳过到期这一道**，凭据那道照判：
    -- 宁可多列一行，也不许把能点的行藏掉 —— 这一栏是唯一的取消屏蔽入口。
    local now
    if GQL.Api and GQL.Api.WallClock then now = GQL.Api.WallClock() end
    if type(now) ~= "number" and GQL.Api and GQL.Api.Uptime then now = GQL.Api.Uptime() end
    if type(now) ~= "number" then now = nil end
    --- 两道过滤：
    ---   * 玩家屏蔽过的行**无条件留**：已过期/零凭据也一样 —— 右栏那个"取消忽略"按钮
    ---     是唯一的找回路，把它过滤掉等于把屏蔽做成单向门（计划 §四.3）；
    ---   * 其余只留"客户端今天还答得出它"**且**没到期的行：凭据那道读 T1 的唯一真源
    ---     Ledger.QuestHasCredential，这里不另拼第二套判据；到期那道与矩阵同一句
    ---     now >= rec.expiresAt。账本还没落行的池候选算有凭据（它就正挂在板上）。
    local function keepable(qid, rec)
        if rec and rec.ignored then return true end
        if rec and now and type(rec.expiresAt) == "number" and now >= rec.expiresAt then
            return false
        end
        if not rec then return true end
        return not not (GQL.Ledger and GQL.Ledger.QuestHasCredential
            and GQL.Ledger.QuestHasCredential(db, qid))
    end
    --- 同名去重比的是**账本记录**的证据强弱：池 > 读到过金币 > 客户端自己答的图 >
    --- 到期时刻晚 > questID 小。这一串就是矩阵同名去重 scoreOf 的同一条排序
    --- （池 8 / 读到过金币 4 / 有地图 2 / 有剩余 1，再取小 ID），只是矩阵打的是 row
    --- （r.money / r.remaining 来自 observation 与手动补录），这里打的是记录
    --- （rec.lastMoney / rec.expiresAt），两者不是同一批数 —— 所以镜像它的权重口径、
    --- 不调用那段代码，矩阵那一份本轮一个字不动。
    local function evidence(rec)
        if not rec then return true, false, false, -1 end
        return not not rec.poolSeen,
               (type(rec.lastMoney) == "number"),
               not not (rec.mapID and not rec.mapManual),
               (type(rec.expiresAt) == "number" and rec.expiresAt) or -1
    end
    local cand, seen = {}, {}
    local function addCand(qid, poolName)
        if not qid or seen[qid] then return end
        local nm = nameOf(qid, poolName)
        if not nm or not tostring(nm):lower():find(low, 1, true) then return end
        seen[qid] = true               -- 一个号只摊开一次（撞名与否都算这条候选）
        local rec = db and db.quests and db.quests[qid]
        if not keepable(qid, rec) then return end
        local poolSeen, hasMoney, hasMap, expiresAt = evidence(rec)
        cand[#cand + 1] = {
            questID = qid, name = tostring(nm), ignored = (rec and rec.ignored) or nil,
            poolSeen = poolSeen, hasMoney = hasMoney, hasMap = hasMap, expiresAt = expiresAt,
        }
    end
    local pool = GQL.Api and GQL.Api.GetPool()
    if type(pool) == "table" then
        for i = 1, #pool do
            local q = pool[i]
            if q then
                addCand(tonumber(q.questID),
                        (type(q.name) == "string" and q.name ~= "") and q.name or nil)
            end
        end
    end
    for qid in pairs(db and db.quests or {}) do
        addCand(qid, nil)
    end
    local function stronger(a, b)
        if a.poolSeen ~= b.poolSeen then return a.poolSeen end
        if a.hasMoney ~= b.hasMoney then return a.hasMoney end
        if a.hasMap ~= b.hasMap then return a.hasMap end
        if a.expiresAt ~= b.expiresAt then return a.expiresAt > b.expiresAt end
        return a.questID < b.questID
    end
    local best = {}
    for i = 1, #cand do
        local e = cand[i]
        local prev = best[e.name]
        if not prev or stronger(e, prev) then best[e.name] = e end
    end
    for i = 1, #cand do
        local e = cand[i]
        if best[e.name] == e then
            -- 只交渲染层真读的三个字段：金额那一列本轮撤下，e.money 与它的回填一起删
            -- （不留没人读的字段）。
            list[#list + 1] = { questID = e.questID, name = e.name, ignored = e.ignored }
        end
    end
    table.sort(list, function(a, b) return a.questID < b.questID end)
    return list
end

-- 左栏那行数 = 这个关键词在屏蔽管理里**管得着的行数**（就是 #matches 的大小）。
-- 为什么要把数摊在每行尾巴上（原样从旧口径带过来）：用户实测加了"符文石/追踪萨拉塔斯"
-- 两个关键词却看不到任务，分不清是关键词写错还是图没跟踪，0 与 N 都得报出来。
-- 第 32 轮口径变了：它不再是"账本里名字撞上的行数"——零凭据/已过期的旧 ID 不进列表、
-- 同名的只算一条，拿它当"有几条撞名记录"读会误读（计划 §四.2 写死的这条代价）。
local function KeywordHits(kw)
    return #Options.KeywordMatches(kw)
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
    frame = CreateFrame("Frame", "GQLOptionsFrame", UIParent, "BackdropTemplate")
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
    GQL.Config.RegisterEscapeClose("GQLOptionsFrame")

    local line = frame:CreateTexture(nil, "ARTWORK")
    line:SetColorTexture(1, 1, 1, 0.15)
    line:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H + 4)
    line:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -RIGHT_PAD, -HEADER_H + 4)
    line:SetHeight(1)

    -- 滚动区只承载"身体"，表头在上面
    scroll = CreateFrame("ScrollFrame", "GQLOptScroll", frame)
    scroll:SetPoint("TOPLEFT", frame, "TOPLEFT", LEFT_PAD, -HEADER_H)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -(RIGHT_PAD + SCROLLBAR_W + SCROLLBAR_GAP), BOTTOM_PAD)
    scroll:EnableMouse(true)
    scroll:SetScript("OnMouseWheel", OnWheel)
    content = CreateFrame("Frame", "GQLOptContent", scroll)
    content:SetSize(BODY_W, 1)
    content:EnableMouse(true)
    content:SetScript("OnMouseWheel", OnWheel)
    scroll:SetScrollChild(content)

    -- 标准可拖动滚动条（右侧）
    scrollbar = CreateFrame("Slider", "GQLOptScrollBar", frame, "UIPanelScrollBarTemplate")
    scrollbar:SetWidth(SCROLLBAR_W)
    scrollbar:SetPoint("TOPLEFT", scroll, "TOPRIGHT", SCROLLBAR_GAP, -16)
    scrollbar:SetPoint("BOTTOMLEFT", scroll, "BOTTOMRIGHT", SCROLLBAR_GAP, 16)
    scrollbar:SetMinMaxValues(0, 0)
    scrollbar:SetValueStep(1)
    scrollbar:SetScript("OnValueChanged", function(self, value)
        scroll:SetVerticalScroll(value)
    end)

    sec1 = content:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    sec1:SetText("跟踪地图（默认=本资料片区域；可额外关注自己的地图）")

    addCur = CreateFrame("Button", nil, content, "UIPanelButtonTemplate")
    addCur:SetSize(180, BTN_H)
    addCur:SetText("＋添加当前所在地图")
    addCur:SetScript("OnMouseWheel", OnWheel)
    addCur:SetScript("OnClick", function()
        local id = GQL.Api and GQL.Api.CurrentMapID and GQL.Api.CurrentMapID()
        if not id then print("GQL: 取不到当前地图") return end
        local _, msg = GQL.Config.AddMap(id)
        print("GQL: " .. (msg or ""))
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
        local _, msg = GQL.Config.AddMapByName(mapEdit:GetText() or "")
        print("GQL: " .. (msg or ""))
        mapEdit:SetText("")
        RefreshMatrixAndPool(); Options.Refresh()
    end)
    mapEdit:SetScript("OnEnterPressed", function() addByName:Click() end)

    -- "登录时抓一次世界任务池"那颗勾 2026-10-27 整段撤下（用户口径："登录抓取那个去掉，
    -- 默认就抓取 不用设置"）。只撤控件：db.config.syncOnLogin 与 Config.SyncOnLogin/
    -- SetSyncOnLogin、Sync.OnWorldEnter 的读档一句都留着 —— 存档里现在就是 true，
    -- 行为正是"登录默认抓"；想关只能改存档（代价与实测经过写在 Sync.lua 的那句注释里）。
end

function Options.Refresh()
    if not frame then return end
    local base = (GQL.Api and GQL.Api.MidnightMapIDs and GQL.Api.MidnightMapIDs()) or {}
    local db = GQL.Config.DB()
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
            GQL.Config.RemoveMap(e.id)
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
end

function Options.Open()
    Build()
    Options.Refresh()
    if scrollbar then scrollbar:SetValue(0) end
    if scroll then scroll:SetVerticalScroll(0) end
    frame:Show()
end

-- ============================================================================
-- 关键词 · 屏蔽管理（两栏窗口，矩阵"添加任务"按钮打开）
-- 用户口径二："左侧是关键词列表，选中关键词，右侧是命中的任务名称列表，
-- 该列表可以点按钮 忽略 或 取消忽略"；补充口径："关键词还能继续添加或删除——
-- 设置里移除掉是入口挪走，不是功能废了"。落点就在左栏：它本来就是关键词列表。
--
-- 从旧"添加任务"弹框（Matrix.OpenAddDialog）接过来的三条实测事实，别丢：
--   * 地图右键 pin（Shift+左键）装了画环插件就钩不到 —— 找回被屏蔽的行不能
--     依赖任何外部插件，命中集合只读账本 + Api.GetPool()；
--   * 金额 nil ≠ 0 金币：上一版把问不出的说成"非金币"，12.1.5 实测 89347 那条
--     967 金的任务被标错，玩家据此以为插件判它不该上板。**这条教训的归属是矩阵**
--     的金额列与悬停（Matrix.lua 那份本轮没动，仍按"问不出不写没有"）——
--     本窗口右栏第 32 轮起不再显示金额，那一列换成 questID
--     （用户 2026-10-27："最好加上questId，不用加金额"）；
--   * 点"取消忽略"只是撤销那条**永久**屏蔽，这一行会不会重新上板由五道自动判据
--     重算（在不在板上/金币/关键词/日志收的/注册周常）—— 窗口不承诺上板，
--     也不再造"矩阵已有/已加"那类状态标签（旧标签体系随 Mx.listed 一起下线）。
--
-- 父帧挂 UIParent 而不是矩阵主帧：主帧 Hide 不再捎带它，Matrix 的 FocusOnMatrix
-- 里"先撞到弹框算外部"的特判也因此整块下线（它是"别的窗口"，天然判外部）。
-- 帧/滚动区/行池/选中态全收进 Panel 这一个顶层 local：WoW 的 Lua 5.1 每个闭包
-- 最多 60 个 upvalue（12.1.5 实测超限=整份文件加载失败，lupa 抓不到，
-- 见 tests 的 test_lua_upvalue_budget），别把新控件一个个开顶层 local，
-- 也别把两栏的绘制塞进 Build() 的闭包里。
local Panel = {}

-- 滚轮方向与矩阵/设置面板一致（减 delta，见 OnWheel 的头注）；两栏各自滚各自的
-- ScrollFrame，没有额外滚动条：内容没超出可视区时 clamp 自动等于不滚。
local function PanelWheel(self, delta)
    local child = self.__gqlChild
    local maxS = math.max(0, (child and child:GetHeight() or 0) - (self:GetHeight() or 0))
    local v = (self:GetVerticalScroll() or 0) - delta * STEP
    if v < 0 then v = 0 elseif v > maxS then v = maxS end
    self:SetVerticalScroll(v)
end

local function PanelWheelLeft(_, delta)
    if Panel.scrollL then PanelWheel(Panel.scrollL, delta) end
end

local function BuildPanel()
    if Panel.frame then return end
    local PW, PH = 640, 420
    -- 页脚带（两排固定控件）整条不属于任何视口：输入+添加(24) / 补扫(22) + 留白。
    -- 两只视口（scrollL 与 scrollR 的 BOTTOMRIGHT）底边都让到 FOOT_H，谁也不许压住页脚。
    -- 锚点口径（12.1.5 实测，上一版四处写错的就是这个）：从窗口**底边**锚定时
    -- WoW 的 y 向上为正 —— 负数等于把控件往下甩到窗口外面。
    -- 代价（照实说）：右栏可视高度因此少 62px ≈ 少 3 行，多了靠滚轮 —— 这是有意的，
    -- 拿可视区换"添加"能被点到。
    local FOOT_H = 74
    local f = CreateFrame("Frame", "GQLKeywordPanel", UIParent, "BackdropTemplate")
    f:SetSize(PW, PH)
    f:SetPoint("CENTER")
    f:SetFrameStrata("DIALOG")
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    f:EnableMouse(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", function(self) self:StartMoving() end)
    f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    f:SetBackdrop({
        -- 不透明深灰：和矩阵/设置面板同一套（半透明没有意义，还让 hover 判定透穿）
        bgFile = "Interface/Buttons/WHITE8X8",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 16, insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })
    pcall(f.SetBackdropColor, f, 0.07, 0.07, 0.09, 1)

    local title = f:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    title:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -10)
    title:SetText("关键词 · 屏蔽管理")
    local closeBtn = CreateFrame("Button", nil, f, "UIPanelCloseButton")
    closeBtn:SetPoint("TOPRIGHT", f, "TOPRIGHT", -4, -4)
    closeBtn:SetScript("OnClick", function() f:Hide() end)
    -- 全局名 GQLKeywordPanel 就是挂 ESC 名单的凭据（见 Config.RegisterEscapeClose）
    GQL.Config.RegisterEscapeClose("GQLKeywordPanel")

    local line = f:CreateTexture(nil, "ARTWORK")
    line:SetColorTexture(1, 1, 1, 0.15)
    line:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -HEADER_H + 4)
    line:SetPoint("TOPRIGHT", f, "TOPRIGHT", -12, -HEADER_H + 4)
    line:SetHeight(1)

    Panel.colTitleL = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    Panel.colTitleL:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -HEADER_H - 6)
    Panel.colTitleL:SetText("关键词  命中")
    Panel.colTitleR = f:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    Panel.colTitleR:SetPoint("TOPLEFT", f, "TOPLEFT", 12 + KW_LEFT_W + 14, -HEADER_H - 6)
    Panel.colTitleR:SetText("命中的任务")

    -- 左栏：关键词行池（一行 = 整行可点=选中 + 行尾"删"小按钮）
    Panel.scrollL = CreateFrame("ScrollFrame", "GQLKeywordPanelScrollL", f)
    Panel.scrollL:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -HEADER_H - 22)
    -- 底边：相对点必须是窗口**底边**（上一版写成 TOPLEFT，视口高度算出来是负的，
    -- 客户端裁出来只剩几十像素 —— 用户截图里"左栏只显示 1.5 行"就是这个）。
    -- 自身锚点仍取 BOTTOMRIGHT 而不是 BOTTOMLEFT：两只锚点各钉一条竖边，
    -- 左右两边都给出 x 才有宽度（都用 LEFT 钉的话宽度无人定义，整栏会变成 0 宽看不见）。
    Panel.scrollL:SetPoint("BOTTOMRIGHT", f, "BOTTOMLEFT", 12 + KW_LEFT_W, FOOT_H)
    Panel.scrollL:EnableMouse(true)
    Panel.scrollL:SetScript("OnMouseWheel", PanelWheel)
    Panel.contentL = CreateFrame("Frame", "GQLKeywordPanelContentL", Panel.scrollL)
    Panel.contentL:SetSize(KW_LEFT_W, 1)
    Panel.contentL:EnableMouse(true)
    Panel.contentL:SetScript("OnMouseWheel", PanelWheel)
    Panel.scrollL.__gqlChild = Panel.contentL
    Panel.scrollL:SetScrollChild(Panel.contentL)

    -- 右栏：选中关键词的命中任务（一行 = 任务名 + questID + 忽略/取消忽略按钮）
    Panel.scrollR = CreateFrame("ScrollFrame", "GQLKeywordPanelScrollR", f)
    Panel.scrollR:SetPoint("TOPLEFT", f, "TOPLEFT", 12 + KW_LEFT_W + 14, -HEADER_H - 22)
    -- 底边让到 FOOT_H，与左栏（上面 scrollL 那条 BOTTOMRIGHT）同一条页脚带：
    -- 页脚带整条不属于任何视口，否则右栏视口会压住"添加"右边 38px（含文字中心），
    -- 点在"添加"上就落进了 EnableMouse 的视口 —— 玩家实测"添加点不动、回车有效"就是这么来的。
    Panel.scrollR:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -12, FOOT_H)
    Panel.scrollR:EnableMouse(true)
    Panel.scrollR:SetScript("OnMouseWheel", PanelWheel)
    Panel.contentR = CreateFrame("Frame", "GQLKeywordPanelContentR", Panel.scrollR)
    Panel.contentR:SetSize(KW_RIGHT_W, 1)
    Panel.contentR:EnableMouse(true)
    Panel.contentR:SetScript("OnMouseWheel", PanelWheel)
    Panel.scrollR.__gqlChild = Panel.contentR
    Panel.scrollR:SetScrollChild(Panel.contentR)

    -- 栏底固定件：输入框 + 添加 + 补扫（三个控件都是从设置面板整段搬过来的，行为不改；
    -- 挂窗口本体不埋滚动区——旧弹框的读法/click 辅助都按"窗口直接子帧"找得到它们）
    Panel.kwEdit = CreateFrame("EditBox", "GQLKeywordPanelKwEdit", f, "InputBoxTemplate")
    Panel.kwEdit:SetSize(240, 24)
    Panel.kwEdit:SetAutoFocus(false)
    -- 栏底两排固定控件从窗口底边往上排：补扫 12→34、输入框+添加 40→64，
    -- 两栏视口的底边都让到 FOOT_H(74) 以上 —— 页脚带这条谁都不占（"添加"点不动就是
    -- 右栏视口当年压到了 y=12 把这条占掉了，修法见上面 scrollR 的 BOTTOMRIGHT）。
    -- 上一版这两处的 y 写成了负数（-12 与 -52），从底边锚定负数=往下甩，于是
    -- "补扫半截溢出边框"和"输入框整排看不见、这一轮根本没法加关键词"是同一个符号错。
    Panel.kwEdit:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 12, 40)
    Panel.kwEdit:SetScript("OnMouseWheel", PanelWheelLeft)

    Panel.addBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    Panel.addBtn:SetSize(56, BTN_H)
    Panel.addBtn:SetPoint("LEFT", Panel.kwEdit, "RIGHT", 6, 0)
    Panel.addBtn:SetText("添加")
    Panel.addBtn:SetScript("OnMouseWheel", PanelWheelLeft)
    Panel.addBtn:SetScript("OnClick", function()
        local _, msg = GQL.Config.AddKeyword(Panel.kwEdit:GetText() or "")
        print("GQL: " .. (msg or ""))
        Panel.kwEdit:SetText("")
        -- 先重抓再刷新窗口：左栏命中数读的就是这次快照写进账本的行，
        -- 反过来写会让用户点完仍看到 0（快照是异步的，面板却是旧数据）——
        -- 这句顺序要求原样从设置面板的注释带过来
        RefreshMatrixAndPool()
        Options.RefreshKeywordPanel()
    end)
    Panel.kwEdit:SetScript("OnEnterPressed", function() Panel.addBtn:Click() end)

    -- 关键词补扫：这类周常不在世界任务池里，光加关键词永远不会出现
    -- （用户实测"加了关键词没有对应任务"）—— 点一次把跟踪清单问一遍，
    -- 命中的任务自动注册成一行；不扫整棵地图树（用户口径：只扫用户添加的图 + 默认图）。
    Panel.huntBtn = CreateFrame("Button", nil, f, "UIPanelButtonTemplate")
    Panel.huntBtn:SetSize(160, BTN_H)
    Panel.huntBtn:SetPoint("BOTTOMLEFT", f, "BOTTOMLEFT", 12, 12)
    Panel.huntBtn:SetText("补扫跟踪地图")
    Panel.huntBtn:SetScript("OnMouseWheel", PanelWheelLeft)
    Panel.huntBtn:SetScript("OnClick", function()
        -- 判空要写在 pcall 之前：被 pcall 的 `GQL.Sync.HuntKeywordMaps` 本身是在保护
        -- **外**求值的，Sync 没加载（Core 报错中断、或用户单发这个窗口）时
        -- "attempt to index a nil value" 直接炸出 OnClick，等于没护。
        if not (GQL.Sync and GQL.Sync.HuntKeywordMaps) then
            print("GQL: 同步模块没加载，补扫用不了（/gql log 看原因）")
            return
        end
        local okCall, done, msg = pcall(GQL.Sync.HuntKeywordMaps)
        print("GQL: " .. tostring(not okCall and "补扫出错: " .. done or msg))
        Options.RefreshKeywordPanel()
    end)

    Panel.kwPool = {}
    Panel.questPool = {}
    Panel.frame = f
end

local function PanelKwRow(i)
    local r = CreateFrame("Button", nil, Panel.contentL)
    r:SetSize(KW_LEFT_W, ROW_H)
    r.__label = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    r.__label:SetPoint("LEFT", r, "LEFT", 4, 0)
    r.__label:SetJustifyH("LEFT")
    r.__btn = CreateFrame("Button", nil, r, "UIPanelButtonTemplate")
    r.__btn:SetSize(28, 18)
    r.__btn:SetPoint("RIGHT", r, "RIGHT", -4, 0)
    r.__btn:SetText("删")
    r.__btn:SetScript("OnMouseWheel", PanelWheel)
    Panel.kwPool[i] = r
    return r
end

local function PanelQuestRow(i)
    local r = CreateFrame("Frame", nil, Panel.contentR)
    r:SetSize(KW_RIGHT_W, ROW_H)
    r.__label = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    r.__label:SetPoint("LEFT", r, "LEFT", 4, 0)
    r.__label:SetJustifyH("LEFT")
    -- 这一列是 questID（字段以前叫 __gold、装的是金额；第 32 轮整列换成编号，
    -- 名字跟着改口 —— 一个叫 __gold 的字段装 ID 是谎）
    r.__id = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    r.__id:SetPoint("RIGHT", r, "RIGHT", -74, 0)
    r.__id:SetJustifyH("RIGHT")
    r.__btn = CreateFrame("Button", nil, r, "UIPanelButtonTemplate")
    r.__btn:SetSize(64, 18)
    r.__btn:SetPoint("RIGHT", r, "RIGHT", -4, 0)
    r.__btn:SetScript("OnMouseWheel", PanelWheel)
    Panel.questPool[i] = r
    return r
end

-- 按钮只调既有入口，不新造数据层函数："忽略"=Track.RemoveQuest（这条就是唯一入口，
-- 矩阵右键那个入口 2026-10-27 起已整块下线），
-- "取消忽略"=Track.AddQuest（撤销永久屏蔽；上不上板由自动判据说，不承诺）。
-- Track 自己的提示负责说清"会不会回来由谁判"，这里点完只重刷窗口。
local function PanelToggleQuest(qid)
    if not qid or not GQL.Track then return end
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[qid]
    if rec and rec.ignored then
        GQL.Track.AddQuest(qid)
    else
        GQL.Track.RemoveQuest(qid)
    end
    Options.RefreshKeywordPanel()
end

function Options.RefreshKeywordPanel()
    local f = Panel.frame
    if not f then return end
    local kws = GQL.Config.GetKeywords()

    -- 选中态存在帧上（panel.__kw）：被删掉的关键词不再是选中项，右栏退回提示行
    local sel = f.__kw
    local keep = false
    for i = 1, #kws do
        if kws[i] == sel then keep = true end
    end
    if not keep then f.__kw = nil; sel = nil end

    -- 左栏：一行一个关键词，行文字 = "关键词  命中数"，点行=选中（换色，不弹任何窗）
    local pool = Panel.kwPool
    local y = 0
    for i = 1, #kws do
        local kw = kws[i]
        local r = pool[i] or PanelKwRow(i)
        r.__kw = kw
        r.__label:SetText(kw .. "  " .. KeywordHits(kw))
        if r.__label.SetTextColor then
            if kw == sel then r.__label:SetTextColor(1, 0.82, 0, 1)
            else r.__label:SetTextColor(1, 1, 1, 1) end
        end
        r.__btn:SetScript("OnClick", function()
            -- 与今天设置面板那一路逐句同口径：删配置 → 退掉由它注册的周常行
            -- （不删 regKey 它会顶着"每轮快照重问状态"的豁免一直占一行，可玩家已经
            -- 说不要这个名字了）→ 重画窗口和矩阵
            GQL.Config.RemoveKeyword(kw)
            SafeCall(GQL.Sync and GQL.Sync.UnregisterKeyword, kw)
            Options.RefreshKeywordPanel()
            SafeCall(GQL.Matrix and GQL.Matrix.Refresh)
        end)
        r:SetScript("OnClick", function()
            f.__kw = kw
            Options.RefreshKeywordPanel()
        end)
        r:ClearAllPoints()
        r:SetPoint("TOPLEFT", Panel.contentL, "TOPLEFT", 0, -y)
        -- Show 不可省：末尾只 Hide 掉这轮没用上的条目，下次列表变长复用到它们时
        -- 不 Show 就是一条既看不见也点不回来的记录（旧弹框 :1787 注释讲的单向门教训，
        -- 矩阵正文也栽过一次 —— 池子复用必须显式 Show）
        r:Show()
        y = y + ROW_H
    end
    for i = #kws + 1, #pool do pool[i]:Hide() end
    Panel.contentL:SetSize(KW_LEFT_W, math.max(y, 1))
    do
        local maxS = math.max(0, y - (Panel.scrollL:GetHeight() or 0))
        local v = Panel.scrollL:GetVerticalScroll() or 0
        if v > maxS then Panel.scrollL:SetVerticalScroll(maxS) end
    end

    Panel.colTitleR:SetText(sel and ("「" .. tostring(sel) .. "」命中的任务") or "命中的任务")

    -- 右栏：选中关键词的命中任务；数据只读不写（集合走 KeywordMatches）
    local matches = sel and Options.KeywordMatches(sel) or nil
    local qpool = Panel.questPool
    local n = 0
    y = 0
    local function putRow(e, hintText)
        n = n + 1
        local r = qpool[n] or PanelQuestRow(n)
        r.__questID = e and e.questID or nil
        r.__label:SetText(hintText or (e and e.name) or "")
        if r.__label.SetTextColor then
            if hintText then r.__label:SetTextColor(0.65, 0.65, 0.65, 1)
            else r.__label:SetTextColor(1, 1, 1, 1) end
        end
        -- 这一列写 questID：玩家点屏蔽要认得出自己点的是哪一条（用户 2026-10-27
        -- "最好加上questId，不用加金额"）。金额那一列整段撤下 —— `nil ≠ 0 金币`
        -- （问不出只显 "?"、不许说成"非金币"）那条 12.1.5 实测教训一个字没作废，
        -- 但它的归属是**矩阵**的金额列与悬停（Matrix.lua 那份本轮没动），
        -- 不是这个屏蔽管理窗口。提示行没有任务，编号留空串。
        r.__id:SetText(e and tostring(e.questID) or "")
        if e then
            r.__btn:Show()
            r.__btn:SetText(e.ignored and "取消忽略" or "忽略")
            r.__btn:SetScript("OnClick", function() PanelToggleQuest(e.questID) end)
        else
            r.__btn:Hide()
        end
        r:ClearAllPoints()
        r:SetPoint("TOPLEFT", Panel.contentR, "TOPLEFT", 0, -y)
        r:Show()   -- 同上：复用的行不 Show 就是看不见也点不着
        y = y + ROW_H
        return r
    end
    if not sel then
        putRow(nil, "点左边一个关键词")
    elseif #matches == 0 then
        -- 就这一句灰字，不写第二段解释（用户口径：命中 0 的说明搬进左栏行尾的数就够）
        putRow(nil, "（没有命中：这个名字不在跟踪地图的板上，也不在账本里）")
    else
        for i = 1, #matches do putRow(matches[i]) end
    end
    for i = n + 1, #qpool do qpool[i]:Hide() end
    Panel.contentR:SetSize(KW_RIGHT_W, math.max(y, 1))
    do
        local maxS = math.max(0, y - (Panel.scrollR:GetHeight() or 0))
        local v = Panel.scrollR:GetVerticalScroll() or 0
        if v > maxS then Panel.scrollR:SetVerticalScroll(maxS) end
    end
end

function Options.OpenKeywordPanel()
    BuildPanel()
    Options.RefreshKeywordPanel()
    Panel.frame:Show()
end
