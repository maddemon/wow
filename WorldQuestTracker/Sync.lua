-- 同步层：登录抓世界任务池并 diff；交任务事件 + 15 秒钱差窗口记账。
-- 所有 WoW API 经 WQT.Api，本文件只管编排与时序。

WQT.Sync = {}
local Sync = WQT.Sync

local L = WQT.Ledger
local DEBOUNCE_SECONDS = 2
local MONEY_WINDOW_SECONDS = 15
local SAMPLE_SECONDS = 5
local EMPTY_CONFIRM_SECONDS = 30
-- 窗口内出现过这些帧，说明可能有别的资金进出，本次测量作废
local UNTRUSTED_FRAMES = { "MailFrame", "AuctionFrame", "GuildBankFrame", "TradeFrame" }
-- 事件名候选按"本机认不认识"逐个试，12.x 里哪个还在没有实测依据
local POOL_UPDATE_EVENTS = { "WORLD_QUEST_LOG_UPDATE", "QUEST_LOG_UPDATE" }
local TURN_IN_EVENTS = { "QUEST_COMPLETE", "QUEST_TURNED_IN" }

local clickGuard = false
local debouncePending = false
local emptyPending = nil     -- 首次读到空池时的会话秒数，nil = 没有待确认
local capture = nil          -- { questID, before, t0 }

function Sync.ClickGuard() return clickGuard end
function Sync.SetClickGuard(v) clickGuard = not not v end

-- 入库的时间戳一律用墙上时钟：GetTime() 每次开机归零，跟账本里跨开机存的 CD 锚点、
-- 历史交任务时间不是同一个域，混用会让周统计静默算错。没有可用墙上时钟时退回 GetTime()，
-- 并把用的哪个域记进 db.clockDomain，由 /wqt dump 如实标注"本次开机内才可比"。
local function Clock()
    local wall = WQT.Api and WQT.Api.WallClock() or nil
    local uptime = math.floor((WQT.Api and WQT.Api.Uptime()) or 0)
    if wall then return wall, uptime, "epoch" end
    return uptime, uptime, "uptime"
end

function Sync.CurrentCharKey()
    local name = "player"
    if UnitName then
        local okCall, v = pcall(UnitName, "player")
        if okCall and v then name = tostring(v) end
    end
    local realm = ""
    if GetRealmName then
        local okCall, v = pcall(GetRealmName)
        if okCall and v then realm = tostring(v) end
    end
    return name .. "-" .. realm
end

--- 抓池 → diff → 写本角色快照。池不可用时直接返回，不做任何 diff：
--- 把"读不到"当成"全部消失"会清空整张账本，那比不更新严重得多。
--- 空池是数据（全清完了），但登录那一瞬接口可能还没就绪，所以要用会话时钟隔 30 秒
--- 二次确认为空才入库；期间任何一次非空读取都取消确认。
function Sync.SnapshotNow()
    local db = WQT.Config.DB()
    if not db then return nil end
    local pool, err = WQT.Api.GetPool()
    if not pool then
        WQT.Config.Log("快照跳过：池不可用 " .. tostring(err))
        return { degrade = tostring(err) }
    end

    local now, uptime, domain = Clock()

    if #pool == 0 then
        if not emptyPending then
            emptyPending = uptime
            WQT.Config.Log("池为空：30 秒后二次确认再判消失")
            C_Timer.After(EMPTY_CONFIRM_SECONDS, function()
                pcall(Sync.SnapshotNow)
            end)
            return { poolCount = 0, pendingEmpty = true }
        elseif uptime - emptyPending < EMPTY_CONFIRM_SECONDS then
            return { poolCount = 0, pendingEmpty = true }
        end
        emptyPending = nil
    else
        emptyPending = nil
    end

    db.clockDomain = domain
    if domain ~= "epoch" then
        WQT.Config.Log("时钟域降级：无可用墙上时钟，时间戳只在本次开机内可比")
    end
    WQT.Config.RefreshCycle(domain == "epoch" and now or nil, uptime)
    if db.cycleAnchor == 0 then
        WQT.Config.Log("cycleAnchor 未知：周统计口径可能不准")
    end
    local res = L.DiffPool(db, pool, now)
    -- 体积清理只有登录这一个触发点：不接这里，Prune 就永远不跑。
    L.Prune(db, now)

    local charKey = Sync.CurrentCharKey()
    db.chars[charKey] = db.chars[charKey] or { views = {} }
    local row = db.chars[charKey]
    row.lastSync = now
    row.views = row.views or {}
    if UnitClass then
        local okCall, _, classToken = pcall(UnitClass, "player")
        if okCall and classToken then row.class = classToken end
    end
    if UnitLevel then
        local okCall, level = pcall(UnitLevel, "player")
        if okCall and level then row.level = level end
    end

    local inPool = {}
    for i = 1, #pool do
        local questID = pool[i].questID
        inPool[questID] = true
        local state = { WQT.Api.QuestState(questID) }
        local obs = L.GetObs(db, questID)
        local rows = db.turnins[charKey] and db.turnins[charKey][questID]
        local last = rows and rows[#rows]
        if obs and last and last.t >= obs.firstSeen then
            state[1] = "done"     -- 本轮亲眼记到的交任务优先于查询接口
        end
        -- 视图统一走 Ledger.SetView 写，避免 { s, t } 这个形状在两处各拼一遍
        L.SetView(db, charKey, questID, state[1], now)
    end
    for questID in pairs(db.quests) do
        if not inPool[questID] then
            L.SetView(db, charKey, questID, "ended", now)
        end
    end

    res.poolCount = #pool
    WQT.Config.Log(("快照完成 池=%d 新增=%d 消失=%d 重现=%d 奖励变化=%d 锚点=%d 角色=%s")
        :format(#pool, #res.added, #res.gone, #res.renewed, #res.sigChanged,
            db.cycleAnchor or 0, charKey))
    return res
end

local function ScheduleSnapshot()
    if debouncePending then return end
    debouncePending = true
    C_Timer.After(DEBOUNCE_SECONDS, function()
        debouncePending = false
        local okCall, err = pcall(Sync.SnapshotNow)
        if not okCall then WQT.Config.Log("快照异常: " .. tostring(err)) end
    end)
end

--- 逐个容错注册。12.1.5 实测 RegisterEvent 对客户端不认识的事件名直接抛错，
--- 而 WORLD_QUEST_LOG_UPDATE 正炸在 OnWorldEnter 第一行，登录同步整条没跑
--- （用户日志里那行 "Attempt to register unknown event" 就是这件事）。
--- 一个名字失败不许带累后面的名字。
local function TryRegister(name)
    local okCall, err = pcall(Sync.frame.RegisterEvent, Sync.frame, name)
    if not okCall then WQT.Config.Log("注册事件失败 " .. name .. ": " .. tostring(err)) end
    return okCall
end

local function AnyRegistered(names)
    for i = 1, #names do
        if TryRegister(names[i]) then return true end
    end
    return false
end

function Sync.OnWorldEnter()
    -- 池变化事件按候选退：QUEST_LOG_UPDATE 历史版本一直在，覆盖面也够
    -- （任何任务状态变化都会发，去抖已经把重复抓取压成一次）
    if not AnyRegistered(POOL_UPDATE_EVENTS) then
        WQT.Config.Log("池变化事件全部不可用：只在登录与 /wqt sync 时更新池")
    end
    for i = 1, #TURN_IN_EVENTS do TryRegister(TURN_IN_EVENTS[i]) end
    ScheduleSnapshot()
end

local function FrameVisible(name)
    return WQT.Api.FrameVisible(name) == true
end

local function MoneyUntrusted()
    for i = 1, #UNTRUSTED_FRAMES do
        if FrameVisible(UNTRUSTED_FRAMES[i]) then return true end
    end
    return false
end

--- 窗口结束才结算：净增量归这个任务，于是任务直接给的金币和随后
--- 立刻开宝箱给的金币一次全收进来（宝箱数额随机，这里取实测近似）。
function Sync.FinishWindow()
    local c = capture
    capture = nil
    if not c then return end
    local after = WQT.Api.Money()
    local gain, polluted = nil, false
    if type(c.before) == "number" and type(after) == "number" then
        polluted = c.polluted == true or MoneyUntrusted()
        if not polluted then
            local delta = after - c.before
            -- 只认进账；同期有出账（买了东西）说明窗口被污染，整笔作废
            if delta >= 0 then gain = delta end
        end
    end
    local db = WQT.Config.DB()
    if not db then return end
    local charKey = Sync.CurrentCharKey()
    local rec = db.quests[c.questID]
    L.OnTurnIn(db, charKey, c.questID, c.t0, gain, rec and rec.currentSig or nil)
    L.SetView(db, charKey, c.questID, "done", c.t0)
    WQT.Config.Log(("交任务 quest=%s 入账=%s 窗口污染=%s")
        :format(tostring(c.questID), WQT.Config.FormatGold(gain), tostring(polluted)))
end

--- 窗口内每隔几秒盯一次：交完任务顺手开邮箱捡钱再关掉，只在结算那一刻采样会漏判，
--- 把那笔外来入账记到任务头上。命中一次就记 polluted，结算时整笔作废。
local function SampleWindow()
    if not capture then return end
    if MoneyUntrusted() then capture.polluted = true end
    C_Timer.After(SAMPLE_SECONDS, SampleWindow)
end

function Sync.BeginCapture(questID)
    questID = tonumber(questID)
    if not questID then return end
    if capture then
        -- 同一任务的 QUEST_COMPLETE 与 QUEST_TURNED_IN 会连着来：起点必须留在最早那次，
        -- 否则钱在两个事件之间到账，起点被覆盖成到账后的数，这笔金币会记成 0。
        if capture.questID == questID then return end
        -- 换任务了：先按已走过的窗口结算上一笔。窗口短一点只是近似（用户要的就是"记个大概"），
        -- 整笔丢掉会在账本上显示成"没做"，那才是真错。
        Sync.FinishWindow()
    end
    capture = { questID = questID, before = WQT.Api.Money(), t0 = Clock() }
    C_Timer.After(MONEY_WINDOW_SECONDS, Sync.FinishWindow)
    C_Timer.After(SAMPLE_SECONDS, SampleWindow)
end

Sync.frame = CreateFrame("Frame", "WQTSyncFrame", UIParent)
Sync.frame:SetScript("OnEvent", function(self, event, arg1)
    -- 分发按候选名单判断，不写死单个名字：注册成功的可能只剩下面的备用事件
    for i = 1, #POOL_UPDATE_EVENTS do
        if event == POOL_UPDATE_EVENTS[i] then return ScheduleSnapshot() end
    end
    for i = 1, #TURN_IN_EVENTS do
        if event == TURN_IN_EVENTS[i] then return Sync.BeginCapture(arg1) end
    end
end)

WQT.RegisterCommand("sync", function()
    local res = Sync.SnapshotNow()
    if not res then
        print("WQT: 同步失败，详情 /wqt log")
    elseif res.degrade then
        print("WQT: 池不可用（" .. res.degrade .. "），账本保持不变")
    elseif res.pendingEmpty then
        print("WQT: 池为空，30 秒后二次确认（先不动账本）")
    else
        print(("WQT: 池 %d 条，新增 %d，消失 %d，重现 %d，奖励变化 %d")
            :format(res.poolCount, #res.added, #res.gone, #res.renewed, #res.sigChanged))
    end
end, "立即重跑一次池快照 diff")
