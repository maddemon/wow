-- 任务耗时：只算"任务在游戏任务日志里、且游戏在线"的时长。
-- 离线（登出/挂机）自动暂停；放弃（任务从日志里删掉）这一次的在册时长作废，
-- 只有最终交成的那一条实例记耗时。设计口径（用户 2026-11-13 定稿）：
--   · 离线/挂机不算（只在任务在日志且在线时累计）；
--   · 中途放弃（从日志删除）又接回来，放弃前的那段不计入，只算最终成功那次；
--   · 展示放在矩阵任务名 hover（整行热区，无角色归属）与取证命令。
--
-- 数据：db.durations[charKey][questID] = { start=number|nil, total=number, leftOnline=bool }
--   start       当前在线在册区间的起点（nil = 当前不在计时）
--   total       本条尝试已累计的在线在册秒数（start 之外、已闭合的跨度之和）
--   leftOnline  上一次离开日志是否发生在"在线时"——重新接取时据此决定接续旧尝试
--               还是开新尝试（放弃的不计入，见 Tick）。
--
-- 全部纯账本、零接口：在册判定复用 Api.AcceptedQuests（它本身就是读任务日志），
-- 计时域与账本其它时间戳同域（调用方用 Sync.Clock 的墙上时钟读数传进来）。

GQL.Duration = {}
local Duration = GQL.Duration

local function Entry(db, charKey, questID, create)
    if not (db and charKey and questID) then return nil end
    db.durations = db.durations or {}
    db.durations[charKey] = db.durations[charKey] or {}
    local t = db.durations[charKey]
    local e = t[questID]
    if not e and create then
        e = { start = nil, total = 0, leftOnline = false }
        t[questID] = e
    end
    return e
end

--- 一个角色当前在日志里、且账本已知（db.quests 有这条）的任务集合。
--- 随手接的普通任务若没进账本，不单独计时（与账本只记世界任务/关键词/内置族的口径一致）。
local function InLogSet(db)
    local okCall, list = pcall(GQL.Api.AcceptedQuests)
    if not okCall or type(list) ~= "table" then return nil end   -- 问不出日志就不动作（宁可不计时，不许误判放弃）
    local inLog = {}
    for i = 1, #list do
        local qid = list[i] and list[i].questID
        if qid and db.quests and db.quests[qid] then inLog[qid] = true end
    end
    return inLog
end

--- 任务在日志里时推进计时：开区间 / 续区间 / 重新接取（放弃后再接开新尝试）。
function Duration.Tick(db, charKey, questID, now)
    local e = Entry(db, charKey, questID, true)
    if e.start then
        -- 接续当前在线区间：把上一拍到这一拍的跨度并入 total，起点后移
        e.total = e.total + (now - e.start)
        e.start = now
    else
        -- 之前没在计时：可能因登出暂停（leftOnline=false ⇒ 接续旧尝试，保留 total），
        -- 也可能之前放弃（leftOnline=true ⇒ 开新尝试，清零）。
        if e.leftOnline then e.total = 0 end
        e.leftOnline = false
        e.start = now
    end
end

--- 亚基入侵这类客户端不给任务号的虚拟行/连续入侵事件不计时（用户 2026-11-13：除它外矩阵里
--- 真实任务都要记）。按名称排除；只影响计时起算，不碰其它判据。
local function Excluded(db, questID)
    local rec = db and db.quests and db.quests[questID]
    local nm = rec and rec.name
    if type(nm) == "string" and nm:find("亚基入侵") then return true end
    return false
end

--- 每趟观测的收口：把"在册"集合（常规日志里的 ∪ 池里客户端答 progress 且不在常规日志里的
--- extraActive）推进计时；不在册但之前在线在册的标记为放弃（暂停并标 leftOnline）。
--- 不在 db.quests 的未知任务从来没建过 entry，不会进来；turn-in 已由 Finalize 收尾，不会误伤。
--- 【2026-11-13 修正】暂停权只属于"看得最全"的那一拍：只有调用方显式传入池视图（extraActive
--- 为 table，哪怕空表）才允许暂停。LogBypass 这类只扫日志的调用点看不到池，若让它暂停会把
--- 池里挂着、常规日志读不到的世界任务误判成"放弃"——随后池那一拍 Tick 又按"放弃重接"清零，
--- 两条通路反复打架，累计时长被清光，最后只剩最后一拍的间隙（实测被记成 7 秒/0 秒）。
--- extraActive 为 nil 时退化为只推进不暂停（等价 Advance），无权判定放弃。
function Duration.Reconcile(db, charKey, now, extraActive)
    if not (db and charKey and now) then return end
    local inLog = InLogSet(db)
    if not inLog then return end   -- 问不出日志就整体不动（宁可不计时，不许误判放弃）
    local active = {}
    for qid in pairs(inLog) do active[qid] = true end
    local fullView = (type(extraActive) == "table")
    if fullView then
        for qid in pairs(extraActive) do
            if not Excluded(db, qid) then active[qid] = true end
        end
    end
    for qid in pairs(active) do
        if not Excluded(db, qid) then
            Duration.Tick(db, charKey, qid, now)
        end
    end
    if not fullView then return end   -- 没给池视图 = 视野不全，无权暂停
    local t = db.durations and db.durations[charKey]
    if not t then return end
    for qid, e in pairs(t) do
        if e.start and not active[qid] then
            e.total = e.total + (now - e.start)
            e.start = nil
            e.leftOnline = true   -- 在线时离开 = 放弃；下次接取开新尝试，不累计这段
        end
    end
end

--- 只推进、永不暂停：给只扫日志、看不到池的调用点用（LogBypass / /gql sync 落账收尾）。
--- 见 Reconcile 注释——让视野不全的那一拍暂停，会把池型世界任务误判成放弃、随后清零。
function Duration.Advance(db, charKey, now)
    return Duration.Reconcile(db, charKey, now, nil)
end

--- 暴露给 Sync：当前角色常规任务日志里的任务集合（计时"在册"判据之一）。
function Duration.InLogSet(db)
    return InLogSet(db)
end

--- 暴露给 Sync：亚基入侵等不计时任务判定。
function Duration.Excluded(db, questID)
    return Excluded(db, questID)
end

--- 登出：在线区间闭合、保留 total（leftOnline=false ⇒ 下次登录接续旧尝试）。
function Duration.PauseAll(db, charKey, now)
    if not (db and charKey) then return end
    local t = db.durations and db.durations[charKey]
    if not t then return end
    for qid, e in pairs(t) do
        if e.start then
            e.total = e.total + (now - e.start)
            e.start = nil
            e.leftOnline = false
        end
    end
end

--- 交任务成功：把当前区间并入 total，记录这一条实例的耗时，清空 entry。返回秒数或 nil。
--- 调用方（Sync.RecordTurnIn）只在"这一实例确实记了一笔交割"时调它，去重由那边把关，
--- 同一实例不会重复 Finalize。清空 entry：这条实例已收尾，下次再接是新实例。
function Duration.Finalize(db, charKey, questID, now)
    local e = Entry(db, charKey, questID, false)
    if not e then return nil end
    local dur
    if e.start then
        dur = e.total + (now - e.start)
    else
        dur = e.total
    end
    if type(dur) ~= "number" or dur < 0 then dur = nil end
    db.durations[charKey][questID] = nil
    return dur
end

--- 一次性清掉旧版"挂板即起算"留下的全部耗时数据（用户 2026-11-13："全部清理了"）。
--- 清两处：
---   ① db.durations 里所有未收口的计时状态（start/total/leftOnline 都可能含挂板时间）；
---   ② turnins 流水行上的 dur 字段。
--- 只删耗时这一列：交割流水本身、编号 k、金币、时间戳一律不动 —— 耗时是附着在流水上的
--- 展示值，不是凭据。幂等，返回清掉的条数；由 Config.MIGRATIONS[12] 在升级时跑一次。
function Duration.ClearAllOnce(db)
    if type(db) ~= "table" then return 0 end
    local n = 0
    local turns = db.turnins
    if type(turns) == "table" then
        for _, byQ in pairs(turns) do
            if type(byQ) == "table" then
                for _, rows in pairs(byQ) do
                    if type(rows) == "table" then
                        for i = 1, #rows do
                            local r = rows[i]
                            if type(r) == "table" and r.dur ~= nil then
                                r.dur = nil
                                n = n + 1
                            end
                        end
                    end
                end
            end
        end
    end
    local timers = db.durations
    if type(timers) == "table" then
        for _, byQ in pairs(timers) do
            if type(byQ) == "table" then
                for _ in pairs(byQ) do n = n + 1 end
            end
        end
    end
    db.durations = {}
    return n
end

--- 某条任务所有已记录完成实例的耗时平均（跨全部角色）。返回 (平均秒数|nil, 次数)。
function Duration.Average(db, questID)
    if not (db and questID) then return nil, 0 end
    local sum, n = 0, 0
    local turns = db.turnins
    if type(turns) == "table" then
        for ckey, byQ in pairs(turns) do
            local rows = byQ and byQ[questID]
            if type(rows) == "table" then
                for i = 1, #rows do
                    local d = rows[i] and rows[i].dur
                    if type(d) == "number" and d > 0 then
                        sum = sum + d
                        n = n + 1
                    end
                end
            end
        end
    end
    if n == 0 then return nil, 0 end
    return sum / n, n
end

--- 某条任务最近一次记录的耗时（跨全部角色，取时刻最新的一笔 dur）。返回秒数或 nil。
function Duration.LastDurAll(db, questID)
    if not (db and questID) then return nil end
    local best, bestT
    local turns = db.turnins
    if type(turns) == "table" then
        for ckey, byQ in pairs(turns) do
            local rows = byQ and byQ[questID]
            if type(rows) == "table" then
                for i = 1, #rows do
                    local r = rows[i]
                    local d = r and r.dur
                    local rt = r and tonumber(r.t)
                    if type(d) == "number" and d > 0 and rt
                        and (bestT == nil or rt > bestT) then
                        best, bestT = d, rt
                    end
                end
            end
        end
    end
    return best
end

--- 秒数 → 易读耗时（"X分Y秒" / "Y秒"）。缺/非数返回 nil。
function Duration.FormatSpan(sec)
    if type(sec) ~= "number" or sec < 0 then return nil end
    local m = math.floor(sec / 60)
    local s = math.floor(sec % 60)
    if m > 0 then return ("%d分%d秒"):format(m, s) end
    return ("%d秒"):format(s)
end
