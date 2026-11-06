-- 设置页：**一页**装完 跟踪地图 + 关键词 · 屏蔽管理（用户 2026-11-01 定稿："关键词也不到
-- 单独窗口了，和地图管理都放在一个页面里，这样矩阵里的添加任务按钮就不需要了"）。
-- 这一页 2026-11-01 起**同时出现在两处**（用户口径："我看其他的插件的配置是在这里的，
-- 我们的确实弹窗，我们能做到这里吗" —— 指的是 ESC → 选项 → 插件 那一列）：
--   * 暴雪设置面板里的一个分类（Settings.RegisterCanvasLayoutCategory，10.0+ 接口）；
--   * 自家那个可拖动窗口（老客户端/接口缺失时的兜底，也是矩阵"设置"按钮的落点 ——
--     走它不关矩阵；暴雪那边开面板会 CloseSpecialWindows，把挂 ESC 名单的矩阵一并关掉）。
-- 两处共用同一份数据与同一段排版代码（NewPage / LayoutPage），只是控件各一套 ——
-- 一个帧只能有一个父亲，共用控件是做不到的，共用**代码**才是这里的正解。
-- "登录时抓一次世界任务池"那颗勾 2026-10-27 整段撤下（用户口径："默认就抓取 不用设置"），
-- 数据层没动：db.config.syncOnLogin 照旧读，登录照旧抓，想关只能改存档。
-- 金币门槛段/面板内"刷新"按钮 2026-10-27 整体撤下（用户口径四）：
--   * 金币门槛只是暂时不能从 UI 改，db.config.minGold 仍是唯一真源，
--     Config.SetMinGold/GetMinGold/MinMatrixMoney 三个函数与判据一个都不改；
--   * 矩阵标题栏那颗"刷新"按钮 2026-11-04 也下线了：用户实测点它几乎没变化
--     （世界任务没变动 ⇒ 池签名相同、diff 为空、重画逐字相同），打开矩阵就是
--     唯一的手工刷新入口（用例 test_opening_matrix_resyncs_and_shows_last_sync 钉住）。
--
-- 交互约定（用户实测反馈）：
--   * 窗口可拖动；标题栏固定在顶部不随内容滚动。
--   * 设置面板高度自适应内容，只有超过屏幕 85% 才出现竖向滚动。
--   * 右侧是标准可拖动滚动条，滚轮方向与矩阵保持一致（同一套手感）。
--   * 窗口与矩阵同在 MEDIUM（用户 2026-11-06：不要固定置顶），靠层号压住矩阵
--     （见 RaiseAboveMatrix）；鼠标落在里面时矩阵整条不亮（口径在 PanelUnderMouse）。

GQL.Options = {}
local Options = GQL.Options

local PANEL_W = 600
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
-- 关键词两列的左列宽度（右列 = 行宽 - 左列 - 列间距，见 NewQuestRow 里的 pg.questW）。
-- 2026-11-01 起关键词与地图管理同页，窗口宽度从 500 加到 600：那一排四颗控件
-- （输入框/按名称添加/扫描地图/添加当前所在地图）要放得下。
local KW_LEFT_W = 250

local frame, scroll, content, scrollbar, settingsCanvas
-- 一页配置的两套控件（自家窗口 / 暴雪设置面板的画布），字段见 NewPage。
-- 收成两个表而不是摊成一堆顶层 local：Options 里每个闭包的 upvalue 已经不少，
-- WoW 的 Lua 5.1 每个闭包上限 60（超了整份文件加载失败，见 Matrix.lua 文件头那段）。
local WinPage, CanvasPage

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
--- 候选集合 = 这一栏**管得着的行**（池 ∪ 账本，经过两道过滤 + 同名去重，**还没按关键词筛**）。
--- 单独抽出来是因为这一趟很贵：遍历池 + 整张账本，逐条问名字/凭据/到期/到期时刻。
--- 2026-11-01 实测"点一个关键词就卡一下"卡的正是它 —— 当时每个关键词各跑一趟全表
--- 扫描（12 个关键词 = 同一张账本扫 12 遍，外加几千次问名字）。现在一次重排只扫一遍，
--- 左列的命中数与右列的行共用这同一份候选（数就是行数，不再两处各算一套）。
local function BuildCandidates(db)
    local list = {}
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
    ---   * 其余只留"客户端今天还答得出它"**且**没到期的行：凭据那道读唯一真源
    ---     Ledger.QuestHasCredential，这里不另拼第二套判据；到期那道与矩阵同一条真源
    ---     Ledger.InstanceExpired（这一条实例自己的结束时刻已过、且没有更晚还在板上被看见过
    ---     的证据 ⇒ 才算到期；上一条实例留下的过期值不吃行 ⇒ 这一行矩阵还在列，
    ---     右栏这个唯一的找回入口就不许先把它藏掉）。
    ---     账本还没落行的池候选算有凭据（它就正挂在板上）。
    local function keepable(qid, rec)
        if rec and rec.ignored then return true end
        if rec and GQL.Ledger and GQL.Ledger.InstanceExpired
            and GQL.Ledger.InstanceExpired(db, rec, now) then
            return false
        end
        if not rec then return true end
        return not not (GQL.Ledger and GQL.Ledger.QuestHasCredential
            and GQL.Ledger.QuestHasCredential(db, qid, now))
    end
    --- 同名去重比的是**账本记录**的证据强弱：池 > 读到过金币 > 客户端自己答的图 >
    --- 到期时刻晚 > questID 小。这一串就是矩阵同名去重 scoreOf 的同一条排序
    --- （池 8 / 读到过金币 4 / 有地图 2 / 有剩余 1，再取小 ID），只是矩阵打的是 row
    --- （r.money / r.remaining 来自 observation 与手动补录），这里打的是记录
    --- （rec.lastMoney / 本轮生效的到期时刻），两者不是同一批数 —— 所以镜像它的权重口径、
    --- 不调用那段代码，矩阵那一份本轮一个字不动。
    --- 到期那一格第 34 轮跟着改问真源：矩阵的 r.remaining 现在就是它算的，
    --- 这里再自拼第二份到期账会出现"一条换轮的行在右栏算有剩余、在矩阵算没有"，
    --- 同名两份 ID 谁留谁走就是这么翻的（金币那一格的分歧是既有口径，本轮不动；
    --- 老裸 rec.expiresAt 那一组连同排程值已在 T5 整组退役，更不可能回来）。
    local function evidence(rec)
        if not rec then return true, false, false, -1 end
        local eff = GQL.Ledger and GQL.Ledger.EffectiveExpiry
            and GQL.Ledger.EffectiveExpiry(db, rec, now) or nil
        return not not rec.poolSeen,
               (type(rec.lastMoney) == "number"),
               not not (rec.mapID and not rec.mapManual),
               (type(eff) == "number" and eff) or -1
    end
    local cand, seen = {}, {}
    local function addCand(qid, poolName)
        if not qid or seen[qid] then return end
        local nm = nameOf(qid, poolName)
        if not nm or nm == "" then return end
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
    return list
end

--- 某个关键词在候选集合里命中的行（按 questID 排好）。
--- 候选由调用方给：一次重排只建一次候选，命中数与右列都从这一份里筛。
function Options.KeywordMatches(kw, cand)
    local list = {}
    local low = (type(kw) == "string" and tostring(kw):lower()) or ""
    if low == "" then return list end
    if not cand then cand = BuildCandidates(GQL.Config.DB()) end
    for i = 1, #cand do
        local e = cand[i]
        if e.name and tostring(e.name):lower():find(low, 1, true) then
            list[#list + 1] = e
        end
    end
    table.sort(list, function(a, b) return a.questID < b.questID end)
    return list
end

--- 一次算出**所有**关键词的命中数 → { [关键词] = 行数 }。
--- 左栏那个数 = 这个关键词在屏蔽管理里管得着的行数（就是右列的行数）。
--- 为什么要把数摊在每行尾巴上（原样从旧口径带过来）：用户实测加了"符文石/追踪萨拉塔斯"
--- 两个关键词却看不到任务，分不清是关键词写错还是图没跟踪，0 与 N 都得报出来。
--- 第 32 轮口径变了：它不再是"账本里名字撞上的行数"——零凭据/已过期的旧 ID 不进列表、
--- 同名的只算一条，拿它当"有几条撞名记录"读会误读（计划 §四.2 写死的这条代价）。
--- 2026-11-01：原来是 `KeywordHits(kw) = #KeywordMatches(kw)` 逐行各扫一遍全表，
--- 12 个关键词就是 12 遍（玩家每点一次都卡），这里改成候选扫一遍、关键词各测一次子串。
local function HitCounts(cand, kws)
    local out = {}
    for i = 1, #kws do out[kws[i]] = 0 end
    for i = 1, #cand do
        local nm = tostring(cand[i].name or ""):lower()
        for j = 1, #kws do
            local kw = kws[j]
            if nm:find(tostring(kw):lower(), 1, true) then
                out[kw] = (out[kw] or 0) + 1
            end
        end
    end
    return out
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

--- 一行"地图名 + 移除"。parent/wheel 由调用方给：同一个排版函数既服务自家窗口
--- （parent = 弹窗的滚动内容区，wheel = 弹窗滚动条）也服务暴雪设置面板的画布
--- （parent = 画布内容区，wheel = 画布自己的滚动）—— 一个帧只能有一个父亲，
--- 所以两处各建一套行池，排版逻辑共用这一处。
local function PlaceRow(pool, key, parent, y, text, btnText, enabled, onClick, wheel, rowW)
    local r = pool[key]
    if not r then
        r = CreateFrame("Frame", nil, parent)
        r.label = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
        r.label:SetPoint("LEFT", r, "LEFT", 4, 0)
        r.label:SetJustifyH("LEFT")
        r.btn = CreateFrame("Button", nil, r, "UIPanelButtonTemplate")
        r.btn:SetSize(56, 18)
        r.btn:SetPoint("RIGHT", r, "RIGHT", -4, 0)
        pool[key] = r
    end
    r:ClearAllPoints()
    r:SetPoint("TOPLEFT", parent, "TOPLEFT", 0, -y)
    r:SetSize(rowW or BODY_W, ROW_H)
    r.label:SetText(text)
    if r.label.SetTextColor then
        if enabled then r.label:SetTextColor(1, 1, 1, 1) else r.label:SetTextColor(0.65, 0.65, 0.65, 1) end
    end
    if btnText then
        r.btn:Show()
        r.btn:SetText(btnText)
        r.btn:SetScript("OnClick", onClick)
        r.btn:SetScript("OnMouseWheel", wheel or OnWheel)
        if enabled then r.btn:Enable() else r.btn:Disable() end
    else
        r.btn:Hide()
    end
    r:Show()
    return r
end

--- 面板要列的地图清单：玩家自己加的图，按名字排序 + 去重（父子图同名只显示一张）。
--- 2026-10-31 起"默认地图"整段撤下（只用玩家自己添加的图）；自动记住的子图不展示。
local function TrackedExtraList(db)
    local extra = (db and db.config and db.config.extraMaps) or {}
    local extraList = {}
    for id in pairs(extra) do
        extraList[#extraList + 1] = { id = id, name = MapName(id) }
    end
    table.sort(extraList, function(a, b)
        if a.name == b.name then return a.id < b.id end
        return a.name < b.name
    end)
    local seen, out = {}, {}
    for i = 1, #extraList do
        local e = extraList[i]
        if not seen[e.name] then
            seen[e.name] = true
            out[#out + 1] = e
        end
    end
    return out
end

--- 一页配置的控件（跟踪地图 + 关键词 · 屏蔽管理）。
--- host = 这套控件挂的内容帧（弹窗的滚动内容 / 暴雪选项的画布内容）；
--- wheel = 这套控件被滚轮时该驱动谁；rowW = 行宽（弹窗按内容区宽度，画布按画布宽度）。
--- 用户 2026-11-01 定稿：关键词不再单独开窗，和地图管理在同一页；矩阵那颗"添加任务"
--- 按钮因此整体下线（它的唯一去处就是这个窗口的右栏）。一个帧只能有一个父亲，
--- 两处各建一套控件，排版逻辑共用 LayoutPage。
local function NewPage(host, wheel, rowW)
    local pg = { host = host, wheel = wheel, rowW = rowW,
                 mapRows = {}, kwRows = {}, questRows = {},
                 questW = rowW - KW_LEFT_W - 8 }

    -- ① 跟踪地图
    pg.mapTitle = host:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    pg.mapTitle:SetText("跟踪地图（自己添加；添加后其子地图自动包含，不单独展示）")
    -- 名称输入框（用户 2026-11-01："名称的输入框也不要太长"）：240 → 140。
    -- 地图名就两三个字，长框把后面三颗按钮全挤到半屏外。
    pg.edit = CreateFrame("EditBox", nil, host, "InputBoxTemplate")
    pg.edit:SetSize(140, 24)
    pg.edit:SetAutoFocus(false)
    pg.edit:SetScript("OnMouseWheel", wheel)
    pg.addByName = CreateFrame("Button", nil, host, "UIPanelButtonTemplate")
    pg.addByName:SetSize(110, BTN_H)
    pg.addByName:SetText("按名称添加")
    pg.addByName:SetScript("OnMouseWheel", wheel)
    pg.addByName:SetScript("OnClick", function()
        local _, msg = GQL.Config.AddMapByName(pg.edit:GetText() or "")
        print("GQL: " .. (msg or ""))
        pg.edit:SetText("")
        RefreshMatrixAndPool(); Options.RefreshAll()
    end)
    pg.edit:SetScript("OnEnterPressed", function() pg.addByName:Click() end)

    -- 扫描地图（原"按关键词重扫地图"）：这类周常不在世界任务池里，光加关键词永远不会
    -- 出现（用户实测"加了关键词没有对应任务"）—— 点一次把跟踪清单问一遍，命中的任务
    -- 自动注册成一行。名字与位置 2026-11-01 按用户口径改（原话："这个按钮放在添加后面，
    -- 改名 扫描地图"），现在与添加同排、紧跟"按名称添加"。
    pg.huntBtn = CreateFrame("Button", nil, host, "UIPanelButtonTemplate")
    pg.huntBtn:SetSize(88, BTN_H)
    pg.huntBtn:SetText("扫描地图")
    pg.huntBtn:SetScript("OnMouseWheel", wheel)
    pg.huntBtn:SetScript("OnClick", function()
        -- 判空要写在 pcall 之前：被 pcall 的 `GQL.Sync.HuntKeywordMaps` 本身是在保护
        -- **外**求值的，Sync 没加载时 "attempt to index a nil value" 直接炸出 OnClick。
        if not (GQL.Sync and GQL.Sync.HuntKeywordMaps) then
            print("GQL: 同步模块没加载，扫描用不了（/gql log 看原因）")
            return
        end
        local okCall, done, msg = pcall(GQL.Sync.HuntKeywordMaps)
        print("GQL: " .. tostring(not okCall and "扫描出错: " .. done or msg))
        Options.RefreshAll()
    end)

    -- 当前所在地图（用户 2026-11-01：也"放在 按名称添加 后面"，排在扫描地图之后）
    pg.addCur = CreateFrame("Button", nil, host, "UIPanelButtonTemplate")
    pg.addCur:SetSize(168, BTN_H)
    pg.addCur:SetText("＋添加当前所在地图")
    pg.addCur:SetScript("OnMouseWheel", wheel)
    pg.addCur:SetScript("OnClick", function()
        local id = GQL.Api and GQL.Api.CurrentMapID and GQL.Api.CurrentMapID()
        if not id then print("GQL: 取不到当前地图") return end
        local _, msg = GQL.Config.AddMap(id)
        print("GQL: " .. (msg or ""))
        RefreshMatrixAndPool(); Options.RefreshAll()
    end)

    -- ② 关键词 · 屏蔽管理（左列关键词 + 命中数，右列该关键词命中的任务与"忽略"开关）
    pg.kwTitle = host:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    pg.kwTitle:SetText("关键词 · 屏蔽管理（点左列一个关键词，右列是它命中的任务）")
    pg.kwEdit = CreateFrame("EditBox", nil, host, "InputBoxTemplate")
    pg.kwEdit:SetSize(240, 24)
    pg.kwEdit:SetAutoFocus(false)
    pg.kwEdit:SetScript("OnMouseWheel", wheel)
    pg.addKw = CreateFrame("Button", nil, host, "UIPanelButtonTemplate")
    pg.addKw:SetSize(56, BTN_H)
    pg.addKw:SetText("添加")
    pg.addKw:SetScript("OnMouseWheel", wheel)
    pg.addKw:SetScript("OnClick", function()
        local _, msg = GQL.Config.AddKeyword(pg.kwEdit:GetText() or "")
        print("GQL: " .. (msg or ""))
        pg.kwEdit:SetText("")
        -- 先重抓再刷新：左列命中数读的就是这次快照写进账本的行，
        -- 反过来写会让用户点完仍看到 0（快照是异步的，面板却是旧数据）
        RefreshMatrixAndPool()
        Options.RefreshAll()
    end)
    pg.kwEdit:SetScript("OnEnterPressed", function() pg.addKw:Click() end)
    pg.colL = host:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    pg.colL:SetText("关键词  匹配")
    pg.colR = host:CreateFontString(nil, "OVERLAY", "GameFontNormal")
    pg.colR:SetText("匹配的任务")

    return pg
end

--- 一列关键词行（整行可点 = 选中，行尾"删"）。
local function NewKwRow(pg, i)
    local r = CreateFrame("Button", nil, pg.host)
    r:SetSize(KW_LEFT_W, ROW_H)
    r.__label = r:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    r.__label:SetPoint("LEFT", r, "LEFT", 4, 0)
    r.__label:SetJustifyH("LEFT")
    r.__btn = CreateFrame("Button", nil, r, "UIPanelButtonTemplate")
    r.__btn:SetSize(28, 18)
    r.__btn:SetPoint("RIGHT", r, "RIGHT", -4, 0)
    r.__btn:SetText("删")
    r.__btn:SetScript("OnMouseWheel", pg.wheel)
    pg.kwRows[i] = r
    return r
end

--- 右列一行命中任务（任务名 + questID + 忽略/取消忽略）。
local function NewQuestRow(pg, i)
    local r = CreateFrame("Frame", nil, pg.host)
    r:SetSize(pg.questW, ROW_H)
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
    r.__btn:SetScript("OnMouseWheel", pg.wheel)
    pg.questRows[i] = r
    return r
end

--- 按钮只调既有入口，不新造数据层函数："忽略"=Track.RemoveQuest（这条就是唯一入口），
--- "取消忽略"=Track.AddQuest（撤销永久屏蔽；上不上板由自动判据说，不承诺）。
local function ToggleQuest(qid)
    if not qid or not GQL.Track then return end
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[qid]
    if rec and rec.ignored then
        GQL.Track.AddQuest(qid)
    else
        GQL.Track.RemoveQuest(qid)
    end
end

--- 排整页，返回内容总高。顺序 = 地图段（行 → 输入框/按名称添加/扫描地图/添加当前所在地图）
--- → 关键词段（标题 → 输入框+添加 → 列标题 → 左右两列）。
--- 关键词选中的那个存在 Options.selectedKeyword 上：两个页面（窗口/画布）共用一份，
--- 先点了哪个关键词，另一处打开时也是那一条。
local function LayoutPage(pg, db)
    local y = 0

    -- ① 跟踪地图
    pg.mapTitle:ClearAllPoints()
    pg.mapTitle:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -y)
    y = y + 22
    local list = TrackedExtraList(db)
    local n = 0
    for i = 1, #list do
        n = n + 1
        local e = list[i]
        PlaceRow(pg.mapRows, n, pg.host, y, e.name, "移除", true, function()
            GQL.Config.RemoveMap(e.id)
            Options.RefreshAll(); RefreshMatrixAndPool()
        end, pg.wheel, pg.rowW)
        y = y + ROW_H
    end
    for i = n + 1, #pg.mapRows do pg.mapRows[i]:Hide() end

    -- 那一排：输入框 → 按名称添加 → 扫描地图 → 添加当前所在地图（用户 2026-11-01 的顺序）
    pg.edit:ClearAllPoints()
    pg.edit:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -y)
    pg.addByName:ClearAllPoints()
    pg.addByName:SetPoint("LEFT", pg.edit, "RIGHT", 6, 0)
    pg.huntBtn:ClearAllPoints()
    pg.huntBtn:SetPoint("LEFT", pg.addByName, "RIGHT", 8, 0)
    pg.addCur:ClearAllPoints()
    pg.addCur:SetPoint("LEFT", pg.huntBtn, "RIGHT", 8, 0)
    y = y + 24 + 14

    -- ② 关键词 · 屏蔽管理
    pg.kwTitle:ClearAllPoints()
    pg.kwTitle:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -y)
    y = y + 22
    pg.kwEdit:ClearAllPoints()
    pg.kwEdit:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -y)
    pg.addKw:ClearAllPoints()
    pg.addKw:SetPoint("LEFT", pg.kwEdit, "RIGHT", 6, 0)
    y = y + 24 + 10
    pg.colL:ClearAllPoints()
    pg.colL:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -y)
    pg.colR:ClearAllPoints()
    pg.colR:SetPoint("TOPLEFT", pg.host, "TOPLEFT", KW_LEFT_W + 8, -y)
    y = y + 18

    local kws = (GQL.Config.GetKeywords and GQL.Config.GetKeywords()) or {}
    local sel = Options.selectedKeyword
    local kept = false
    for i = 1, #kws do
        if kws[i] == sel then kept = true end
    end
    if not kept then Options.selectedKeyword = nil; sel = nil end
    -- 一次重排只建一次候选（全表扫描就这一趟）：命中数与右列都从它筛 ——
    -- 每点一个关键词都重扫一遍全账本是 2026-11-01 那次"点一下卡一下"的根因。
    local cand = (#kws > 0) and BuildCandidates(db) or nil
    local hits = cand and HitCounts(cand, kws) or {}

    local top = y
    for i = 1, #kws do
        local kw = kws[i]
        local r = pg.kwRows[i] or NewKwRow(pg, i)
        r.__kw = kw
        r.__label:SetText(GQL.Config.EscapeUI(kw) .. "  " .. tostring(hits[kw] or 0))
        if r.__label.SetTextColor then
            if kw == sel then r.__label:SetTextColor(1, 0.82, 0, 1)
            else r.__label:SetTextColor(1, 1, 1, 1) end
        end
        r.__btn:SetScript("OnClick", function()
            -- 删配置 → 退掉由它注册的周常行（不删 regKey 它会顶着"每轮快照重问状态"的
            -- 豁免一直占一行，可玩家已经不要这个名字了）→ 重画
            GQL.Config.RemoveKeyword(kw)
            SafeCall(GQL.Sync and GQL.Sync.UnregisterKeyword, kw)
            Options.RefreshAll()
            SafeCall(GQL.Matrix and GQL.Matrix.Refresh)
        end)
        r:SetScript("OnClick", function()
            Options.selectedKeyword = kw
            Options.RefreshAll()
        end)
        r:ClearAllPoints()
        r:SetPoint("TOPLEFT", pg.host, "TOPLEFT", 0, -(top + (i - 1) * ROW_H))
        -- Show 不可省：末尾只 Hide 掉这轮没用上的条目，下次列表变长复用到它们时
        -- 不 Show 就是一条既看不见也点不回来的记录（池子复用必须显式 Show）
        r:Show()
    end
    for i = #kws + 1, #pg.kwRows do pg.kwRows[i]:Hide() end

    pg.colR:SetText(sel and ("「" .. GQL.Config.EscapeUI(sel) .. "」匹配的任务") or "匹配的任务")
    local matches = sel and Options.KeywordMatches(sel, cand) or nil
    local nq = 0
    local function putRow(e, hintText)
        nq = nq + 1
        local r = pg.questRows[nq] or NewQuestRow(pg, nq)
        r.__questID = e and e.questID or nil
        r.__label:SetText(hintText or (e and e.name) or "")
        if r.__label.SetTextColor then
            if hintText then r.__label:SetTextColor(0.65, 0.65, 0.65, 1)
            else r.__label:SetTextColor(1, 1, 1, 1) end
        end
        -- 这一列写 questID：玩家点屏蔽要认得出自己点的是哪一条（用户 2026-10-27
        -- "最好加上questId，不用加金额"）。金额那一列整段撤下 —— `nil ≠ 0 金币`
        -- 那条 12.1.5 实测教训仍然有效，但它的归属是**矩阵**的金额列与悬停。
        r.__id:SetText(e and tostring(e.questID) or "")
        if e then
            r.__btn:Show()
            r.__btn:SetText(e.ignored and "取消忽略" or "忽略")
            r.__btn:SetScript("OnClick", function()
                ToggleQuest(e.questID)
                Options.RefreshAll()
            end)
        else
            r.__btn:Hide()
        end
        r:ClearAllPoints()
        r:SetPoint("TOPLEFT", pg.host, "TOPLEFT", KW_LEFT_W + 8, -(top + (nq - 1) * ROW_H))
        r:Show()   -- 同上：复用的行不 Show 就是看不见也点不着
    end
    if not sel then
        putRow(nil, "点左边一个关键词")
    elseif #matches == 0 then
        -- 就这一句灰字，不写第二段解释（用户口径：命中 0 的说明搬进左列行尾的数就够）
        putRow(nil, "（没找到匹配的任务）")
    else
        for i = 1, #matches do putRow(matches[i]) end
    end
    for i = nq + 1, #pg.questRows do pg.questRows[i]:Hide() end

    y = top + math.max(#kws, nq) * ROW_H + 10
    return y
end

local function Build()
    if frame then return end
    frame = CreateFrame("Frame", "GQLOptionsFrame", UIParent, "BackdropTemplate")
    frame:SetSize(PANEL_W, 300)
    frame:SetPoint("CENTER")
    frame:SetFrameStrata("MEDIUM")
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
    title:SetText(GQL.VERSION and ("世界任务追踪 · 设置  v" .. tostring(GQL.VERSION))
        or "世界任务追踪 · 设置")

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

    -- 整页（跟踪地图 + 关键词 · 屏蔽管理）由 NewPage 建，与暴雪设置面板里的画布是
    -- 同一段代码、两套控件（见文件头）
    WinPage = NewPage(content, OnWheel, BODY_W)
    -- 给用例/排查用的把手：控件都在页面表上（行池 kwRows/questRows、输入框、按钮），
    -- 不再各自占一个全局名 —— 一页装了十几件控件，全局名会把 _G 变成垃圾场。
    Options.pages = Options.pages or {}
    Options.pages.win = WinPage

    -- "登录时抓一次世界任务池"那颗勾 2026-10-27 整段撤下（用户口径："登录抓取那个去掉，
    -- 默认就抓取 不用设置"）。只撤控件：db.config.syncOnLogin 与 Config.SyncOnLogin/
    -- SetSyncOnLogin、Sync.OnWorldEnter 的读档一句都留着 —— 存档里现在就是 true，
    -- 行为正是"登录默认抓"；想关只能改存档（代价与实测经过写在 Sync.lua 的那句注释里）。
end

--- 刷新自家设置窗口：内容高度、窗口自适应、滚动条。
function Options.Refresh()
    if not frame or not WinPage then return end
    -- 面板只列玩家加的图；自动记住的子图不在这里展示（db.config.subMaps，只进扫描
    -- 清单与判定）。2026-10-31 起"默认地图"整段撤下（用户口径：默认图四十来张列满
    -- 面版，全删，只用玩家自己添加的图）。
    local contentH = math.max(LayoutPage(WinPage, GQL.Config.DB()), 1)
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

--- 两处配置界面共用的一次重排：自家窗口 + 暴雪设置面板的画布（后者可能还没建）。
--- 按钮回调都调这一个入口，别各自记得去刷另一处 —— 两边数据同一份，刷漏就是"这边加了
--- 图、那边还挂着旧清单"。
function Options.RefreshAll()
    -- 只刷当前看得见的那一页：另一页（画布那段）通常关着，每次点关键词都顺带重排它是纯浪费
    -- （它 OnShow 时会自己刷）。两个页面数据同一份，漏刷看不见的页不会丢东西。
    if frame and frame:IsShown() then SafeCall(Options.Refresh) end
    if settingsCanvas and settingsCanvas:IsShown() then SafeCall(Options.RefreshCanvas) end
end

-- ============================================================================
-- 暴雪设置面板（ESC → 选项 → 插件）
--
-- 用户 2026-11-01："我看其他的插件的配置是在这里的，我们的确实弹窗，我们能做到这里吗"
-- —— 做到"在这里"靠的是 10.0 起的 Settings API：
--   Settings.RegisterCanvasLayoutCategory(canvas, 名字) → 分类
--   Settings.RegisterAddOnCategory(分类)               → 挂进"插件"那一列
--   Settings.OpenToCategory(id)                        → 打开到这一页
-- 老客户端/离线替身没有这张表（12.x 之前的 API 完全不同），整段跳过、退回自家窗口：
-- 宁可"还是弹窗"，也不许因为一个不存在的接口让 /gql config 打不开。
-- 画布内容与弹窗那一段**同一页**（NewPage/LayoutPage），只是控件各一套 ——
-- 2026-11-01 起关键词也并进这一页（用户口径："关键词也不到单独窗口了，和地图管理
-- 都放在一个页面里"），所以画布底部那颗"关键词 · 屏蔽管理"入口按钮整段撤下。
local function CanvasWheel(self, delta)
    local maxS = math.max(0, (self.__gqlChild and self.__gqlChild:GetHeight() or 0)
        - (self:GetHeight() or 0))
    local v = (self:GetVerticalScroll() or 0) - delta * STEP
    if v < 0 then v = 0 elseif v > maxS then v = maxS end
    self:SetVerticalScroll(v)
end

--- 建画布（只在第一次注册分类时跑一次）。返回画布帧。
local function BuildCanvas()
    local canvas = CreateFrame("Frame", "GQLSettingsCanvas", UIParent)
    canvas:SetSize(600, 440)

    -- 头一行写给共享出去的对方看：版本 + 默认配置包的口径（数据在 Defaults.lua）
    local head = canvas:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    head:SetPoint("TOPLEFT", canvas, "TOPLEFT", 4, -4)
    head:SetText(GQL.VERSION and ("GoldQuestLedger v" .. tostring(GQL.VERSION))
        or "GoldQuestLedger")

    local scroll = CreateFrame("ScrollFrame", "GQLSettingsCanvasScroll", canvas)
    scroll:SetPoint("TOPLEFT", canvas, "TOPLEFT", 4, -28)
    scroll:SetPoint("BOTTOMRIGHT", canvas, "BOTTOMRIGHT", -4, 4)
    scroll:EnableMouse(true)
    scroll:SetScript("OnMouseWheel", CanvasWheel)
    local content2 = CreateFrame("Frame", "GQLSettingsCanvasContent", scroll)
    content2:SetSize(560, 1)
    content2:EnableMouse(true)
    content2:SetScript("OnMouseWheel", CanvasWheel)
    scroll.__gqlChild = content2
    scroll:SetScrollChild(content2)

    CanvasPage = NewPage(content2, function(_, delta) CanvasWheel(scroll, delta) end, 540)
    Options.pages = Options.pages or {}
    Options.pages.canvas = CanvasPage
    settingsCanvas = canvas

    canvas:SetScript("OnShow", function() Options.RefreshCanvas() end)
    return canvas
end

--- 刷新画布：内容高度 + 滚到顶（换分类回来时旧偏移会停在半空）。
--- canvas.cat 由注册那边挂上来，只想刷内容时也允许没有分类。
function Options.RefreshCanvas()
    if not CanvasPage then return end
    local host = CanvasPage.host
    local h = math.max(LayoutPage(CanvasPage, GQL.Config.DB()), 1)
    host:SetSize(host:GetWidth() or 560, h)
end

--- 把分类挂进"选项 → 插件"。返回分类对象；接口不可用返回 nil（调用方走兜底窗口）。
--- 幂等：只注册一次，结果（含"不可用"）记在 Options.settingsCategory 上。
function Options.RegisterSettings()
    if Options.settingsCategory ~= nil then
        return Options.settingsCategory or nil
    end
    local S = Settings
    if type(S) ~= "table" or not S.RegisterCanvasLayoutCategory
        or not S.RegisterAddOnCategory then
        Options.settingsCategory = false
        if GQL.Config and GQL.Config.Log then
            GQL.Config.Log("设置面板降级：本机没有 Settings 接口，改用自家窗口（/gql config）")
        end
        return nil
    end
    local okBuild, canvas = pcall(BuildCanvas)
    if not okBuild or not canvas then
        Options.settingsCategory = false
        GQL.Config.Log("设置画布建不出来: " .. tostring(canvas))
        return nil
    end
    local okReg, category = pcall(S.RegisterCanvasLayoutCategory, canvas,
        "GoldQuestLedger 金币账本")
    if not okReg or not category then
        Options.settingsCategory = false
        GQL.Config.Log("设置分类注册失败: " .. tostring(category))
        return nil
    end
    pcall(S.RegisterAddOnCategory, category)
    Options.settingsCategory = category
    SafeCall(Options.RefreshCanvas)
    GQL.Config.Log("设置已挂进暴雪选项（ESC → 选项 → 插件）")
    return category
end

--- 把自家窗口抬到矩阵之上：两个窗口都在 MEDIUM（2026-11-06 起不再固定置顶），
--- 同层里谁在上面由层号定 —— 不显式抬，能不能盖住矩阵就取决于客户端给的层号，
--- 用户 2026-11-01 那张截图正是"窗口在前面、鼠标却在点亮矩阵"。
local function RaiseAboveMatrix(f)
    if not f then return end
    local getFrame = GQL.Matrix and GQL.Matrix.Frame
    if not getFrame then return end
    local okGet, mf = pcall(getFrame)
    if not okGet or not mf then return end
    local okLvl, lvl = pcall(mf.GetFrameLevel, mf)
    lvl = (okLvl and tonumber(lvl)) or 1
    pcall(f.SetFrameLevel, f, lvl + 10)
end

--- 打开自家那一页（可拖动窗口）。矩阵的"设置"走这条 —— 用户 2026-11-01：
--- "矩阵里点击设置后，矩阵就被关闭了，这是必须的吗"：不是。以前点设置直接开暴雪选项，
--- 而暴雪开面板会 CloseSpecialWindows，把挂 ESC 名单的矩阵一并关掉；走自家窗口
--- 两件事都不发生（矩阵留着、随时对照）。
function Options.OpenWindow()
    Build()
    Options.Refresh()
    RaiseAboveMatrix(frame)
    if scrollbar then scrollbar:SetValue(0) end
    if scroll then scroll:SetVerticalScroll(0) end
    frame:Show()
    return frame
end

--- 打开"选项 → 插件"里那一页。接口不可用时退回自家窗口（宁可还是弹窗，
--- 也不许因为一个不存在的接口让入口打不开）。
function Options.OpenSettingsCategory()
    local category = Options.RegisterSettings()
    if category and Settings and Settings.OpenToCategory then
        local okId, id = pcall(category.GetID, category)
        if okId and id and pcall(Settings.OpenToCategory, id) then return end
    end
    return Options.OpenWindow()
end

-- `/gql config` 的老落点：先给暴雪选项那一页（"其他插件的配置都在这里"），
-- 没有接口再退回弹窗。矩阵的"设置"按钮另走 OpenWindow（见上一条的理由）。
Options.Open = Options.OpenSettingsCategory

--- 老入口的两个别名：关键词 2026-11-01 起不再有独立窗口，这两颗现在就是"打开设置页"/
--- "重排设置页"。留着是因为矩阵与文档里还这么叫，删名字不删行为，少一处无谓的连带改动。
Options.OpenKeywordPanel = Options.OpenWindow
Options.RefreshKeywordPanel = Options.RefreshAll

--- 鼠标此刻是否压在我们自己的设置窗口里。矩阵的 hover 扫描用它当门
--- （Matrix.HoverScan 的第一道判据）：用户 2026-11-01 实测"窗口虽然在矩阵前面，
--- 但依然会触发矩阵的 hover"，窗口正文全是可点控件，焦点门不一定能从焦点反推回
--- "在窗口里"，所以直接按矩形判 —— 与层序、与焦点都无关。窗口没建过就是"没压着"。
function Options.PanelUnderMouse()
    local f = frame
    if not f then return false end
    if not (f.IsShown and f:IsShown()) then return false end
    return (f.IsMouseOver and f:IsMouseOver() == true) or false
end
