-- 账本数据层：SavedVariables 建库、周 CD 锚点、诊断日志环。
-- 数据结构（WQTDB，account-wide）：
--   schema = 1
--   cycleAnchor = <本 CD 起点时间戳>
--   clockDomain = "epoch" | "uptime"   -- 时间戳存的是哪个时钟域，uptime 跨开机归零
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

-- 服务端下次周刷新反推本 CD 起点。不写死星期几（国服周四）。
-- 12.1.5 游戏内实测（/wqt probe）：GetQuestResetTime=46509 而开机秒 GetTime=569772 ——
-- 下次重置不可能落在过去，所以它给的是"距重置还剩几秒"的倒计时，不是绝对时间戳。
-- 当成绝对值减一周会算出负锚点，周统计整段失真。形态判定只留一处（ResetFormOf），
-- 探测报告与锚点换算不许各自再猜一遍。
function Config.ResetFormOf(resetTime, uptimeNow)
    if type(resetTime) ~= "number" then return nil, "接口不可用" end
    if resetTime > 1e9 then return "epoch", "大于 1e9，按纪元绝对值处理" end
    if resetTime > WEEK_SECONDS then
        return "sessionAbsolute", ("超过一周（%d），只可能是开机秒绝对值"):format(resetTime)
    end
    if type(uptimeNow) == "number" and resetTime < uptimeNow then
        return "countdown", ("倒计时：%d < 开机秒 %d，当绝对值会落在过去"):format(resetTime, uptimeNow)
    end
    return "countdown", "不超过一周，按倒计时处理"
end

--- 返回本 CD 起点时间戳；定不出来时返回 nil（调用方沿用旧锚点，绝不编一个数）。
function Config.CycleAnchorOf(resetTime, wallNow, uptimeNow)
    local form = Config.ResetFormOf(resetTime, uptimeNow)
    if not form then return nil end
    local wall = (type(wallNow) == "number" and wallNow > 1e9) and wallNow or nil
    if form == "epoch" then return resetTime - WEEK_SECONDS end
    if form == "sessionAbsolute" then
        -- 会话域绝对值：有墙上时钟就搭桥到纪元，否则只能留在本次开机内可比
        if wall and type(uptimeNow) == "number" then
            return wall + (resetTime - uptimeNow) - WEEK_SECONDS
        end
        return resetTime - WEEK_SECONDS
    end
    -- 倒计时
    if wall then return wall + resetTime - WEEK_SECONDS end
    if type(uptimeNow) == "number" and uptimeNow + resetTime >= WEEK_SECONDS then
        return uptimeNow + resetTime - WEEK_SECONDS
    end
    return nil
end

-- 锚点只在真的进入新 CD 时前进。接口缺失时沿用旧值并降级记日志。
-- 用 `not fn` + pcall 判可用性而不是 `type(fn) ~= "function"`：
-- 游戏里 WoW 内置函数 type 就是 "function"，但离线测试里 Python 回调
-- 被 lupa 包成 "userdata"，用 type 判会把可用的回调当成缺失。
function Config.RefreshCycle(wallNow, uptimeNow)
    local db = Config.DB()
    if not db then return false end
    -- 没人给时钟就自己问：ADDON_LOADED 那条路是 Config.Init() 直调，此时还没有任何
    -- Sync 传值进来。锚点停在 0 的话，WeekStats 的 cycleStart 形同不存在，
    -- 历次 CD 的交任务记录会被全算成本周。
    if wallNow == nil and WQT.Api and WQT.Api.WallClock then wallNow = WQT.Api.WallClock() end
    if uptimeNow == nil and WQT.Api and WQT.Api.Uptime then uptimeNow = WQT.Api.Uptime() end
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
    local anchor = Config.CycleAnchorOf(resetTime, wallNow, uptimeNow)
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
