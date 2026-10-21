-- 账本数据层：SavedVariables 建库、周 CD 锚点、诊断日志环。
-- 数据结构（WQTDB，account-wide）：
--   schema = 1
--   cycleAnchor = <本 CD 起点时间戳>
--   quests[questID]     = { name, mapID, lastSeenAt, goldLock, currentSig, highFreq, goneAt,
--                           observations[sig] = { gold, money, items, firstSeen, lastSeen, goneSince } }
--   chars[角色-服务器]   = { class, level, lastSync, views[questID] = { s, t } }
--   turnins[角色][任务]  = { { t, g, sig, cycle }, ... }   -- 1-based 追加日志
--   manual[角色][任务]   = { state, t, g }
--   logs                = { 最近 200 条 }

WQT.Config = {}
local Config = WQT.Config

local WEEK_SECONDS = 604800
local MAX_LOGS = 200

function Config.DB()
    return WQTDB
end

-- 金币格式化。nil → "?" 表示"未知"，与 0（真没赚钱）严格区分。
function Config.FormatGold(copper)
    if type(copper) ~= "number" then return "?" end
    return string.format("%dg%ds%dc",
        math.floor(copper / 10000), math.floor((copper % 10000) / 100), copper % 100)
end

function Config.Log(msg)
    local db = Config.DB()
    if not db then return end
    db.logs = db.logs or {}
    local stamp = math.floor((GetTime and GetTime() or 0) % 100000)
    table.insert(db.logs, string.format("%d %s", stamp, tostring(msg)))
    while #db.logs > MAX_LOGS do table.remove(db.logs, 1) end
end

function Config.GetLogs()
    local db = Config.DB()
    if not db or not db.logs then return "" end
    return table.concat(db.logs, "\n")
end

-- 服务端下次周刷新时间反推本 CD 起点。不写死星期几（国服周四）。
function Config.CycleAnchorOf(resetTime)
    if type(resetTime) ~= "number" then return nil end
    return resetTime - WEEK_SECONDS
end

-- 锚点只在真的进入新 CD 时前进。接口缺失时沿用旧值并降级记日志。
-- 用 `not fn` + pcall 判可用性而不是 `type(fn) ~= "function"`：
-- 游戏里 WoW 内置函数 type 就是 "function"，但离线测试里 Python 回调
-- 被 lupa 包成 "userdata"，用 type 判会把可用的回调当成缺失。
function Config.RefreshCycle()
    local db = Config.DB()
    if not db then return false end
    local fn = GetQuestResetTime
    if not fn then
        Config.Log("RefreshCycle 降级：GetQuestResetTime 不可用，沿用旧锚点")
        return false
    end
    local okCall, resetTime = pcall(fn)
    if not okCall then
        Config.Log("RefreshCycle 降级：GetQuestResetTime 不可用，沿用旧锚点")
        return false
    end
    local anchor = Config.CycleAnchorOf(resetTime)
    if anchor and anchor > (db.cycleAnchor or 0) then
        db.cycleAnchor = anchor
        return true
    end
    return false
end

function Config.Init()
    if type(WQTDB) ~= "table" then WQTDB = {} end
    local db = WQTDB
    db.quests = db.quests or {}
    db.chars = db.chars or {}
    db.turnins = db.turnins or {}
    db.manual = db.manual or {}
    db.logs = db.logs or {}
    db.schema = math.max(db.schema or 0, 1)
    if not db.cycleAnchor or db.cycleAnchor == 0 then
        Config.RefreshCycle()
        db.cycleAnchor = db.cycleAnchor or 0
    end
    Config.Log(("账本就绪 schema=%d cycleAnchor=%d"):format(db.schema, db.cycleAnchor))
end
