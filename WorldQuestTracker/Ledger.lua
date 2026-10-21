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
            rec.name, rec.mapID, rec.lastSeenAt = q.name, q.mapID, now

            local obs = rec.observations[sig]
            if obs then
                if obs.goneSince then
                    -- 消失后又出现 = 新一轮：重置 firstSeen，使上一轮的已交不再计入本轮
                    if now - obs.goneSince < HIGH_FREQ_GAP then rec.highFreq = true end
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
