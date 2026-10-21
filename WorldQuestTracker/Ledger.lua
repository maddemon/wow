-- 纯账本逻辑：世界任务池 diff、奖励签名。不碰 WoW 全局 API（时间戳由调用方传入），
-- 因此可在 lupa 里离线跑。

WQT.Ledger = {}
local L = WQT.Ledger

local MAX_OBS_PER_QUEST = 4
-- 消失时长小于此值 = 高频区（屠戮者高地/虚影风暴 12 小时级），不写死区域名单
local HIGH_FREQ_GAP = 129600

function L.MakeSig(money, items)
    local parts = { tostring(math.floor(type(money) == "number" and money or -1)) }
    if type(items) == "table" then
        local ids = {}
        for i = 1, #items do ids[#ids + 1] = tostring(items[i]) end
        table.sort(ids)
        parts[#parts + 1] = table.concat(ids, ",")
    end
    return table.concat(parts, "|")
end

function L.SigMoney(sig)
    local v = tonumber((tostring(sig)):match("^(%-?%d+)"))
    return (v and v > 0) and v or 0
end

function L.IsGoldSig(sig)
    return L.SigMoney(sig) > 0
end

local function Push(list, questID) list[#list + 1] = questID end

-- 逐项复制而不是 { unpack(...) }：lupa 侧的 Lua 与 WoW 内嵌 Lua 的 unpack
-- 位置不同（5.1 是全局，5.2+ 挪到 table.unpack），复制循环两边都稳。
local function CopyArray(src)
    local out = {}
    for i = 1, #src do out[i] = src[i] end
    return out
end

local function CountPairs(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

function L.CountObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.observations then return 0 end
    return CountPairs(rec.observations)
end

-- observation 超上限时按 lastSeen 淘汰最旧；当前签名永不淘汰。
local function TrimObservations(rec, keepSig)
    while CountPairs(rec.observations) > MAX_OBS_PER_QUEST do
        local oldestSig, oldestSeen
        for sig, obs in pairs(rec.observations) do
            if sig ~= keepSig and (not oldestSeen or obs.lastSeen < oldestSeen) then
                oldestSig, oldestSeen = sig, obs.lastSeen
            end
        end
        if not oldestSig then break end
        rec.observations[oldestSig] = nil
    end
end

--- 用当前角色的世界任务池快照跟账本 diff。
--- "本轮"= 连续存在于两次快照之间，不设时间阈值：任务消失后再出现即新一轮
--- （无论隔 12 小时还是 4 天），高频区与周常区共用同一套逻辑，插件不需要知道任何刷新周期。
function L.DiffPool(db, pool, now)
    local res = { added = {}, gone = {}, renewed = {}, sigChanged = {} }
    local seen = {}

    for i = 1, #pool do
        local q = pool[i]
        local questID = tonumber(q.questID)
        if questID then
            local sig = L.MakeSig(q.money, q.items)
            local rec = db.quests[questID]
            local isNew = not rec
            if isNew then
                rec = { observations = {} }
                db.quests[questID] = rec
                Push(res.added, questID)
            end
            local prevSig = rec.currentSig
            -- 高频只看"这个任务消失了多久"，与奖励签名无关：12 小时区每轮奖励可能都不同，
            -- 绑在签名上就永远学不到（obs.goneSince 只负责重置本轮 firstSeen）。
            if rec.goneAt then
                if now - rec.goneAt < HIGH_FREQ_GAP then rec.highFreq = true end
                rec.goneAt = nil
            end
            rec.name, rec.mapID, rec.lastSeenAt = q.name, q.mapID, now

            local obs = rec.observations[sig]
            if obs then
                if obs.goneSince then
                    -- 消失后又出现 = 新一轮：重置 firstSeen，使上一轮的已交不再计入本轮
                    obs.firstSeen = now
                    obs.goneSince = nil
                    Push(res.renewed, questID)
                end
                obs.lastSeen = now
            else
                rec.observations[sig] = {
                    gold = L.IsGoldSig(sig),
                    money = (type(q.money) == "number") and q.money or nil,
                    items = q.items and CopyArray(q.items) or nil,
                    firstSeen = now,
                    lastSeen = now,
                }
                TrimObservations(rec, sig)
                -- 原来就有签名 → 奖励变了；原来没签名且不是新面孔 → 消失后换了奖励回来，
                -- 同样是新一轮。漏掉后者会让 12 小时区的重现在汇总里凭空消失。
                if prevSig then
                    Push(res.sigChanged, questID)
                elseif not isNew then
                    Push(res.renewed, questID)
                end
            end

            if rec.currentSig ~= sig then
                rec.currentSig = sig
            end
            seen[questID] = true
        end
    end

    for questID, rec in pairs(db.quests) do
        if not seen[questID] and rec.currentSig then
            local obs = rec.observations[rec.currentSig]
            if obs then obs.goneSince = now end
            rec.goneAt = now
            rec.currentSig = nil
            Push(res.gone, questID)
        end
    end

    return res
end

-- 当前池内那份 observation；不在池中返回 nil（调用方据此显示"本轮已结束"）。
function L.GetObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.currentSig then return nil end
    return rec.observations[rec.currentSig]
end

function L.IsHighFreq(db, questID)
    local rec = db.quests and db.quests[questID]
    return not not (rec and rec.highFreq)
end

function L.GetQuestName(db, questID)
    local rec = db.quests and db.quests[questID]
    return rec and rec.name or nil
end

--------------------------------------------------------------------
-- 交任务、状态、周统计、撤销与体积清理
--------------------------------------------------------------------

local WEEK = 604800
local MAX_TURNINS_PER_QUEST = 8

function L.OnTurnIn(db, charKey, questID, t, g, sig)
    db.turnins[charKey] = db.turnins[charKey] or {}
    local log = db.turnins[charKey]
    log[questID] = log[questID] or {}
    local rows = log[questID]
    rows[#rows + 1] = { t = t, g = g, sig = sig, cycle = db.cycleAnchor or 0 }
    while #rows > MAX_TURNINS_PER_QUEST do table.remove(rows, 1) end
    if type(g) == "number" and g > 0 then
        L.MarkGoldByObservation(db, questID, sig)
    end
    return true
end

-- 手动补录。state == "open" 视为清除手动覆盖，回到自动判定。
function L.SetManual(db, charKey, questID, state, t, g)
    db.manual[charKey] = db.manual[charKey] or {}
    local old = db.manual[charKey][questID]
    if state == "open" then
        db.manual[charKey][questID] = nil
    else
        db.manual[charKey][questID] = { state = state, t = t, g = g }
    end
    return old
end

function L.ClearManual(db, charKey, questID)
    if db.manual[charKey] then db.manual[charKey][questID] = nil end
end

function L.SetView(db, charKey, questID, state, t)
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    db.chars[charKey].views[questID] = { s = state, t = t }
end

--- 状态优先级：手动补录 > 本轮实测交任务 > 本角色登录快照 > 池推断。
--- "unknown" 只在完全没有来源时出现，UI 必须如实显示，不得当成"没做"。
--- 注意 currentSig / 池成员关系只描述"最后一次同步的那个角色"看到的池：世界任务池
--- 是按角色各自的（狩猎模式、个人任务线都会改变它）。所以 currentSig 为空绝不等于
--- "本角色的这个任务结束了"，本角色说得出话时一律以它自己的快照为准。
function L.State(db, charKey, questID)
    local m = db.manual[charKey] and db.manual[charKey][questID]
    if m then return m.state end

    local rec = db.quests[questID]
    if not rec then return "unknown" end

    local obs = L.GetObs(db, questID)

    local rows = db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    if obs and last and last.t >= obs.firstSeen then return "done" end

    local view = db.chars[charKey] and db.chars[charKey].views
        and db.chars[charKey].views[questID]
    -- 快照只在覆盖到当前这一轮时才算数，否则上一轮的"已交"会跨轮冒充本轮
    if view and (not obs or (view.t or 0) >= obs.firstSeen) then return view.s end

    return obs and "open" or "ended"
end

--- 本 CD 汇总。手动已交若与实测交任务是同一条就不重复计数；
--- 有完成记录但一条实测金币都没有时 gold 返回 -1，UI 显示 "?"，绝不显示 0。
--- count 返回 -1 表示本周统计整体不可信（见下面域分裂判断），UI 同样显示 "?"。
function L.WeekStats(db, charKey, cycleStart)
    -- 锚点落在纪元秒、事件时间戳落在"本次开机秒"时两者不可比：照大小过滤会把
    -- 每条记录都判成本周之外，汇总静默变成"完成 0"。宁可报未知，也不报一个看着正常的假数。
    if cycleStart and cycleStart > 1e9 and db.clockDomain == "uptime" then
        return -1, -1
    end
    local count, gold, goldKnown = 0, 0, false
    local counted = {}
    for questID, rows in pairs(db.turnins[charKey] or {}) do
        for i = 1, #rows do
            local r = rows[i]
            if r.t and (not cycleStart or r.t >= cycleStart) then
                count = count + 1
                counted[questID] = true
                if type(r.g) == "number" then
                    gold = gold + r.g
                    goldKnown = true
                end
            end
        end
    end
    for questID, m in pairs(db.manual[charKey] or {}) do
        if m.state == "done" and not counted[questID]
            and (not cycleStart or (m.t or 0) >= cycleStart) then
            count = count + 1
            if type(m.g) == "number" then
                gold = gold + m.g
                goldKnown = true
            end
        end
    end
    return count, (count > 0 and not goldKnown) and -1 or gold
end

function L.MarkGoldByObservation(db, questID, sig)
    local rec = db.quests[questID]
    if not rec or rec.goldLock or not sig then return false end
    local obs = rec.observations[sig]
    if not obs then return false end
    obs.gold = true
    return true
end

function L.SetGoldLock(db, questID, locked)
    local rec = db.quests[questID]
    if not rec then return false end
    rec.goldLock = locked or nil
    return true
end

function L.GoldQuestIDs(db)
    local out = {}
    for questID, rec in pairs(db.quests or {}) do
        local sig = rec.currentSig
        local obs = sig and rec.observations and rec.observations[sig]
        if obs and obs.gold then out[#out + 1] = questID end
    end
    table.sort(out, function(a, b)
        return (db.quests[a].name or "") < (db.quests[b].name or "")
    end)
    return out
end

function L.Characters(db)
    local out = {}
    for charKey in pairs(db.chars or {}) do out[#out + 1] = charKey end
    table.sort(out)
    return out
end

function L.UndoApply(db, entry)
    if not entry then return false end
    if entry.kind == "manual" then
        if entry.old then
            db.manual[entry.charKey] = db.manual[entry.charKey] or {}
            db.manual[entry.charKey][entry.questID] = entry.old
        else
            L.ClearManual(db, entry.charKey, entry.questID)
        end
        return true
    elseif entry.kind == "turnin" then
        local rows = db.turnins[entry.charKey] and db.turnins[entry.charKey][entry.questID]
        if rows and #rows > 0 then table.remove(rows, #rows) end
        return true
    end
    return false
end

--- 控制 SavedVariables 体积：陈旧任务整条清出、轮数与观察数封顶。金币历史不删。
--- 判据是"距最后一次在池里出现"，不是"距 CD 起点"：周常任务本 CD 没刷新不代表过时。
function L.Prune(db, now)
    for questID, rec in pairs(db.quests or {}) do
        if (now or 0) - (rec.lastSeenAt or 0) > 3 * WEEK then
            db.quests[questID] = nil
        end
    end
    for _, rec in pairs(db.quests or {}) do
        if rec.observations then TrimObservations(rec, rec.currentSig) end
    end
    for _, rows in pairs(db.turnins or {}) do
        for _, list in pairs(rows) do
            while #list > MAX_TURNINS_PER_QUEST do table.remove(list, 1) end
        end
    end
end
