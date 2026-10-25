-- 同步层：登录抓世界任务池并 diff；交任务事件 + 15 秒钱差窗口记账。
-- 所有 WoW API 经 WQT.Api，本文件只管编排与时序。

WQT.Sync = {}
local Sync = WQT.Sync

local L = WQT.Ledger
-- 事件去抖：QUEST_LOG_UPDATE 在任何任务状态变化时都发（每杀一个目标、每拾取一个
-- 物件都算），做任务时几乎连成一片。一次快照要把每个任务的奖励/时间/状态逐个问
-- 一遍，2 秒一发就是"开着矩阵做任务时时不时卡一下"。世界任务池只在到期时刻换
-- （另有 ScheduleNextRefresh 定点唤醒兜底），10 秒足够，面板上不会明显滞后。
local DEBOUNCE_SECONDS = 10
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
-- 后台轮询开关，默认关：12.x 一次池快照要把几百张图逐个问一遍（GetQuestsOnMap），
-- 用户实测"做什么都一卡一卡"，屏蔽插件就不卡 —— 罪魁就是平时也在抓池。
-- 只有矩阵打开时（Matrix.Toggle 调 SetPolling(true)）才轮询，关上面板立刻停。
-- /wqt sync 手动命令不走这个开关，随时能抓。
local polling = false
local emptyPending = nil     -- 首次读到空池时的会话秒数，nil = 没有待确认
local capture = nil          -- { questID, before, t0, seq, polluted }
local captureSeq = 0         -- 每开一窗 +1：seq 对不上就是上一个窗口遗留的定时器
local windowTimer = nil      -- 15 秒结算定时器句柄（换任务时要取消，防串台）
local sampleTimer = nil      -- 5 秒抽样定时器句柄（C_Timer.After 不给句柄，改用 NewTimer）
-- 登录那一次快照的门闩：OnWorldEnter 置位，去抖回调跑完立刻落下。
-- 不复用 polling：polling 的含义是"矩阵开着期间事件驱动也抓"，混用会让关掉面板后
-- 每次任务状态变化都抓一遍池（12.1.5 实测"做什么都一卡一卡"的成因）。
local loginPending = false

function Sync.ClickGuard() return clickGuard end
function Sync.SetClickGuard(v) clickGuard = not not v end

-- 前向声明：SetPolling 关轮询时要取消挂起的刷新定时器，真身在下面定义
-- （Lua 的 local 要先声明才能被上面的闭包捕获，直接引用会变成全局 nil）
local CancelRefreshTimer
-- 同理：关轮询时也要取消奖励预加载的补读定时器
local CancelPreloadRetry

--- 开/关后台池轮询。矩阵打开时开，关闭时停 —— 关的时候顺手取消
--- 挂起的刷新定时器，别留着到点还跑一趟。
function Sync.SetPolling(enabled)
    polling = not not enabled
    if not polling then
        if CancelRefreshTimer then CancelRefreshTimer() end
        if CancelPreloadRetry then CancelPreloadRetry() end
    end
end

function Sync.Polling() return polling end

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

-- 唤醒定时器封顶。到期时刻一旦被写得很远（脏数据、周常那类排程值），
-- 不封顶就等于"矩阵开着也再不会自己更新"；封顶后每 6 小时多问一次池，
-- 只在矩阵开着时发生，比"永远不刷新"便宜，也比玩家反复手点刷新省。
local MAX_WAKE_SECONDS = 6 * 3600
--- 按最早的任务到期时刻安排下一次刷新：世界任务到点才换，不用一直轮询。
--- 只在轮询中（矩阵开着）安排；平时一个定时器都没有，插件完全静默。
--- 到期时间已过或取不到 → 不自动安排（交给打开矩阵/事件/sync 命令触发）。
--- 关键：同一时刻只允许存在一个待触发的刷新定时器。每次快照都会走到这里，
--- 不取消旧的就会越排越多 —— 做任务时 QUEST_LOG_UPDATE 频繁触发快照，
--- 几十个定时器攒在同一时刻一起炸，每个都跑一遍全量抓池，那就是
--- "开着矩阵就定时卡一下"的元凶（用户实测）。
local refreshTimer = nil
CancelRefreshTimer = function()
    if refreshTimer then
        pcall(function() refreshTimer:Cancel() end)
        refreshTimer = nil
    end
end

local function ScheduleNextRefresh()
    CancelRefreshTimer()
    if not polling then return end
    local db = WQT.Config.DB()
    if not db then return end
    local now = select(1, Clock())
    local due = L.NextRefreshAt(db)
    if not due or due <= now then return end
    local delay = due - now
    if delay > MAX_WAKE_SECONDS then delay = MAX_WAKE_SECONDS end
    refreshTimer = C_Timer.NewTimer(math.max(5, delay), function()
        refreshTimer = nil
        if not polling then return end
        local okCall, err = pcall(Sync.SnapshotNow)
        if not okCall then WQT.Config.Log("快照异常: " .. tostring(err)) end
        ScheduleNextRefresh()
        if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
    end)
end

--- 奖励数据预加载补读。12.x 的奖励金额不在本地时 GetQuestLogRewardMoney
--- 读出来是 0（看着像非金币任务，其实是数据没到，已发 RequestPreloadRewardData）。
--- 服务器数据要几十秒才回来 —— 用户实测"第一次打开矩阵只显示一部分，
--- 关闭再打开才能看全"就是这么来的。矩阵开着时排几个短间隔补读，
--- 金额到了自动补上，不用手动开关面板。补读次数封顶，不会无限转。
local PRELOAD_RETRY_SECONDS = 5
local PRELOAD_MAX_RETRIES = 4
local preloadRetries = 0
local preloadTimer = nil
CancelPreloadRetry = function()
    if preloadTimer then
        pcall(function() preloadTimer:Cancel() end)
        preloadTimer = nil
    end
end

local function SchedulePreloadRetry()
    CancelPreloadRetry()
    if not polling then return end
    if preloadRetries >= PRELOAD_MAX_RETRIES then return end
    preloadTimer = C_Timer.NewTimer(PRELOAD_RETRY_SECONDS, function()
        preloadTimer = nil
        if not polling then return end
        pcall(Sync.SnapshotNow)
        -- 出错也要重排，见 ScheduleSnapshot：SnapshotNow 崩在半路就轮不到它自己末尾的重排
        pcall(ScheduleNextRefresh)
        if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
    end)
end

--- 关键词任务的第二条来源：任务日志。
--- 世界任务池按图问，城市日常/战役任务根本不在池里 —— 用户实测"追踪萨拉塔斯"
--- 就在任务栏第一个，设置里那条关键词的"账本命中"却是 0。日志里名字命中关键词的，
--- 照手动添加那条路建账本记录（kwLogAdded 标记，与玩家手点的区分开），
--- 矩阵的关键词门槛自然把它列出来。
--- 只动"这条不在本次池子里"的：池里已有的交给正常 diff，别拿日志去盖它的奖励签名。
--- 还挂在日志里就每轮续期：AddQuest 默认只给 24 小时，不续的话任务明明还接着一行就消失了。
--- keep 记的是"本轮确实由日志这条路维持"的 questID：还接在日志里但关键词已删、
--- 或者这轮它出现在池里了（改由池维护），都要把标记撤掉，否则这行会永久绕过消失判定。
local function SyncKeywordQuestsFromLog(db, charKey, now, inPool)
    local function clearAllFlags()
        for questID, rec in pairs(db.quests) do
            if rec.kwLogAdded then rec.kwLogAdded = nil end
        end
    end
    -- 关键词全删了：这些行再没有"绕过金币门槛"的理由，标记不清掉会在矩阵里一直赖着
    if #WQT.Config.GetKeywords() == 0 then
        clearAllFlags()
        return 0
    end
    local okCall, list = pcall(WQT.Api.AcceptedQuests)
    if not okCall or type(list) ~= "table" then return 0 end
    -- 同名不收：这个名字的世界任务那一条已经在账本里（有地图信息）时，日志里同名的这条
    -- 是同一个名字的第二份 questID，用户看到的只是矩阵底部多出一整行 "?"
    -- （12.1.5 实测："特别任务：击退/神盾/怨灵"在上面各地图分组里都有，底部又重复一份）
    -- 要按 questID 排除自己：上一版只记名字，于是**这条记录自己**（第二轮起带着地图）
    -- 把它自己判成"另一份同名"，keep 收不到它 → kwLogAdded 被撤 → 下面的消失判定把它
    -- 当成没人维护的记录。12.1.5 实测"加固符文石这个任务没了"走的就是这条：
    -- 还接在日志里、不在池里、第二轮起有地图 = 每轮自我取消。
    local mappedName = {}
    for questID, rec in pairs(db.quests) do
        if rec.mapID and type(rec.name) == "string" and rec.name ~= "" then
            local by = mappedName[rec.name]
            if not by then by = {} mappedName[rec.name] = by end
            by[questID] = true
        end
    end
    local function hasOtherCopy(name, questID)
        local by = name and mappedName[name]
        if not by then return false end
        for id in pairs(by) do
            if id ~= questID then return true end
        end
        return false
    end
    local keep, n = {}, 0
    -- "这条行的地图号有没有凭据"判据与 HealMissingMaps 同源：旧版没标来源 / 存疑标签
    -- 都算没凭据，否则日志这条新凭据永远挤不掉一张没人复核过的旧图
    local legacySrc = (WQT.Ledger and WQT.Ledger.LegacyMapSource
        and WQT.Ledger.LegacyMapSource()) or "\0"
    local suspectSrc = (WQT.Ledger and WQT.Ledger.SuspectMapSource
        and WQT.Ledger.SuspectMapSource()) or "\0"
    local function mapUnproven(rec)
        local src = rec.mapSrc
        return src == nil or src == "" or src == legacySrc or src == suspectSrc
    end
    for i = 1, #list do
        local q = list[i]
        local existing = db.quests[q.questID]
        -- 玩家右键移除过的不再收：矩阵按 rec.ignored 过滤，而这里重新收会把 kwLogAdded
        -- 写回去、等于当场把"别再显示"的意图抹掉（12.1.5 实测反馈"删不掉"）
        if not inPool[q.questID] and not hasOtherCopy(q.name, q.questID)
            and not (existing and existing.ignored)
            and WQT.Config.KeywordMatch(q.name) then
            keep[q.questID] = true
            local rec = existing
            if not rec then
                WQT.Ledger.AddQuest(db, q.questID, now, "questlog")
                rec = db.quests[q.questID]
                n = n + 1
            else
                rec.lastSeenAt = now
                -- 还接在日志里就每轮续期，否则任务明明还接着、行却按上一轮的到期时刻消失。
                -- 到期口径见 Ledger.SetExpiry：住在日志里的关键词任务是**常驻周常**，
                -- 只认 CD 尾巴（能问出下次周重置就用它），问不出才退回 24 小时排程值并标
                -- guessed —— 矩阵的名字颜色与悬停"剩余"只认接口读到的和 CD 结束那两种
                -- （12.1.5 实测反馈"这几个绿色的特别任务 颜色（倒计时）也不对"）。
                -- 上一版这里留着"已经写进去的接口倒计时"：那条倒计时是**板上实例**的轮换
                -- 寿命（几小时），于是 加固符文石 当天下午整行没了 —— 用户"倒计时不准"。
                WQT.Ledger.SetCycleExpiry(rec, now)
                rec.kwLogAdded = true
            end
            if rec and not rec.name then rec.name = q.name end
            -- 日志行自己报的分区图号（C_QuestLog.GetQuestLogZoneMapID 答不答得出**还没实测**，
            -- 取不到就什么都不写）：特别任务/战役任务在全世界任务口径的三条来源上就是问不出
            -- 地图（12.1.5 实测 82157：逐条问的两条一条 MISSING 一条 nil、12 张跟踪图的清单
            -- 反查也没有它），这是唯一一条按"这条任务自己"答的凭据。已经有凭据的行不动它。
            if rec and q.mapID and mapUnproven(rec) then
                rec.mapID, rec.mapSrc = q.mapID, "日志分区"
            end
            -- 在日志里 = 已接：状态记 progress，别让它看着像"没做"
            WQT.Ledger.SetView(db, charKey, q.questID, "progress", now)
        end
    end
    for questID, rec in pairs(db.quests) do
        if rec.kwLogAdded and not keep[questID] then rec.kwLogAdded = nil end
    end
    return n
end

--- 缺地图的记录逐条补问一次所属地图（候选名单与"地图号在第几个返回值"都写在
--- Api.QuestMapID 里，/wqt probe 的取证段按同一份名单逐个问给用户看）。
--- 为什么必须在同步里补：DiffPool 只给"这轮还在池里"的任务写地图，
--- 已接走/已离开池子的旧记录就永远停在 nil —— 而矩阵把 nil 全丢进底部"未知地图"分组，
--- 用户实测的"底部一堆没用的特别任务"就是这么堆出来的（其中不少其实是别的服务端数据
--- 补得出地图的世界任务）。Api 内部按会话缓存，问过不重复问。
--- 还缺的走反向索引兜底：用户诉求很明确——"压根不应该存在没有所属地图的任务"，
--- 而"追踪萨拉塔斯"这类已接任务在全世界任务口径的接口上就是问不出地图。
--- 只在仍缺的时候付这份代价（每张图一次调用），问不出的继续留 nil，不编。
local function HealMissingMaps(db)
    if not (WQT.Api and WQT.Api.QuestMapID) then return end
    -- 旧版留下的"有图号、没来源标签"（Ledger.ClearRetiredMaps 打的标记）也当缺地图重问一遍：
    -- 留着它是因为清掉会把整批行堆进"未知地图"（第 15 轮反馈②③），但不重问就等于
    -- 这份旧答案永远没人复核。问出新答案就覆盖，问不出原样保留。
    local legacy = (WQT.Ledger and WQT.Ledger.LegacyMapSource
        and WQT.Ledger.LegacyMapSource()) or "\0"
    -- 存疑标签（ClearRetiredMaps 给"板上出现过、图由已停用接口答的"行打的）同样要重问：
    -- 留着图号只是为了不让整批行塌进"未知地图"，不是认它当凭据
    local suspect = (WQT.Ledger and WQT.Ledger.SuspectMapSource
        and WQT.Ledger.SuspectMapSource()) or "\0"
    local function needAsk(rec)
        return rec.mapID == nil or rec.mapSrc == legacy or rec.mapSrc == suspect
    end
    local function unproven(rec)
        return rec.mapSrc == nil or rec.mapSrc == "" or rec.mapSrc == legacy
            or rec.mapSrc == suspect
    end
    -- 同名已经有一条带地图的记录 = 上面那个分组里已经有这个任务了，这一条是它的
    -- 第二份 questID（12.1.5 实测："好几个特别任务都跑到了盘卷蛇岛里，而且其他地图已经有了"）。
    -- 给这种行补地图纯属帮倒忙：补回来的地图是接口按另一份 questID 答的，
    -- 归进错分组不说，还会让矩阵的同名去重以为"两条都有地图"而各留一行。
    -- 所以只给"账本里独一份的名字"补问，补不出的留 nil。
    -- 这一份名单**只数本轮真有答复的行**（来源既不是 `旧记录未标来源` 也不是存疑标签）：
    -- 那些旧图号自己也属于要重问的一类，把它们算进名单等于"每条旧记录都因为自己的
    -- 旧图号而永不复核" —— 第十五轮改成"留着图号"之后就是这个形状，重问整段被自己挡死。
    local namedBy = {}
    for _, rec in pairs(db.quests) do
        if rec and rec.mapID and not unproven(rec)
            and type(rec.name) == "string" and rec.name ~= "" then
            namedBy[rec.name] = true
        end
    end
    local stillMissing = false
    for questID, rec in pairs(db.quests) do
        if rec and needAsk(rec)
            and not (type(rec.name) == "string" and namedBy[rec.name]) then
            local mapID, src = WQT.Api.QuestMapID(questID)
            if mapID then
                rec.mapID = mapID
                rec.mapSrc = "补问 " .. tostring(src)
            else
                stillMissing = true
            end
        end
    end
    if not (stillMissing and WQT.Api.MapQuestIndex) then return end
    local mapIDs = (WQT.Config and WQT.Config.TrackedMapIDs and WQT.Config.TrackedMapIDs())
        or (WQT.Api.MidnightMapIDs and WQT.Api.MidnightMapIDs())
    local index, _, srcOf = mapIDs and WQT.Api.MapQuestIndex(mapIDs)
    if not index then return end
    for questID, rec in pairs(db.quests) do
        if rec and needAsk(rec)
            and not (type(rec.name) == "string" and namedBy[rec.name])
            and index[questID] then
            -- 来源按索引那份逐条记：板面反查与悬赏反查是两条不同的凭据，
            -- 混成一个标签就没法复盘"这张图是谁说的"
            rec.mapID = index[questID]
            rec.mapSrc = (srcOf and srcOf[questID]) or "地图任务清单反查"
        end
    end
end

--- 玩家口述的地图（`Api.MAP_MEMORY`）落进账本。第十七轮："我知道每个任务属于哪个地图"。
--- 放在 HealMissingMaps **之前**：这条补得上的，就不用再让接口白问一轮；补不上的照旧
--- 走重问，最后才落"未知地图"。
--- 只补"地图还没落实"的那批（nil / 旧记录未标来源 / 存疑），真凭据一律压不过；
--- 已经标着 `玩家指定` 的那条**按表里的新值重配**（这张表就是这类记录的唯一编辑入口，
--- 我改完名字或图，下一轮生效，不需要玩家动 SavedVariables）。
--- 返回 (配上几条, 配不上几条)：配不上 = 表里的图名不在跟踪清单，或账本里没这个名字的行。
--- 挂到 Sync 上而不是留 local：单测要能直接问它一条口述记录到底配没配上、
--- 有没有静默被跳过（Task 4 修的就是这条静默路径）。
function Sync.ApplyMapMemory(db)
    local presets = WQT.Api and WQT.Api.MAP_MEMORY
    if type(presets) ~= "table" or not next(presets) then return 0, 0 end
    if not (WQT.Ledger and WQT.Ledger.SetManualMap) then return 0, 0 end
    local legacy = WQT.Ledger.LegacyMapSource and WQT.Ledger.LegacyMapSource() or "\0"
    local suspect = WQT.Ledger.SuspectMapSource and WQT.Ledger.SuspectMapSource() or "\0"
    local manual = WQT.Ledger.ManualMapSource and WQT.Ledger.ManualMapSource() or "\0"
    local mapNameOf = (WQT.Api and WQT.Api.MapName) or function() return nil end
    local byName = {}
    local tracked = (WQT.Config and WQT.Config.TrackedMapIDs and WQT.Config.TrackedMapIDs())
        or (WQT.Api.MidnightMapIDs and WQT.Api.MidnightMapIDs()) or {}
    for i = 1, #tracked do
        local n = mapNameOf(tracked[i])
        if n and not byName[n] then byName[n] = tonumber(tracked[i]) end
    end
    local byTitle = {}
    for questID, rec in pairs(db.quests or {}) do
        if type(rec) == "table" and type(rec.name) == "string" and rec.name ~= "" then
            local list = byTitle[rec.name]
            if not list then list = {} byTitle[rec.name] = list end
            list[#list + 1] = questID
        end
    end
    local function weak(rec)
        local src = rec.mapSrc
        return rec.mapID == nil or src == nil or src == "" or src == legacy or src == suspect
    end
    local done, missed = 0, 0
    for i = 1, #presets do
        local p = presets[i]
        local mapID = (type(p) == "table" and p.map) and byName[p.map] or nil
        -- **同名有几条就补几条**：账本里同名记录是常态（一族多 questID、轮换出的新旧两份），
        -- 而 pairs() 顺序随机 —— 第十八轮实测"特别任务：神盾密探 日志没抱怨、图却没归位"
        -- 就是这个形状：补到了没在矩阵里显示的那一条，看得见的那行照旧躺在未知地图。
        -- 先 tonumber 再决定走哪条：`p.questID and { tonumber(p.questID) } or ...`
        -- 在 Lua 里永远走前半（{ nil } 是真值），写错一个数字就静默跳过这一条，
        -- 玩家看到的是"日志没抱怨、图却没归位"。
        local qid = tonumber(p.questID)
        if p.questID ~= nil and not qid then
            WQT.Config.Log(("口述地图表的 questID 不是数字：%s（这条没生效）")
                :format(tostring(p.questID)))
        end
        local ids = qid and { qid } or (p.name and byTitle[p.name])
        if mapID and ids then
            for k = 1, #ids do
                local rec = db.quests and db.quests[ids[k]]
                if rec and (weak(rec) or (rec.mapSrc == manual and rec.mapID ~= mapID))
                    and L.SetManualMap(db, ids[k], mapID) then
                    done = done + 1
                end
            end
        end
        if p.map and not mapID then
            missed = missed + 1
            WQT.Config.Log(("补地图没配上：「%s」的图「%s」不在跟踪清单里"
                .. "（站到那张图上，设置里\"添加当前所在地图\"再刷新）")
                :format(tostring(p.name or p.questID), tostring(p.map)))
        elseif p.name and not ids then
            missed = missed + 1
            WQT.Config.Log(("补地图没配上：账本里没有叫「%s」的行"
                .. "（矩阵里那一行的完整名字发我，名字被截断也会配不上）"):format(p.name))
        end
    end
    return done, missed
end

--- 家族认亲的第二条来源：母任务自己的"任务追踪"清单里带着子任务 ID
--- （用户 2026-10-23："从丰饶供品的任务追踪里面可以看到子任务的内容"、"从子任务反查不到"）。
--- 名字认亲只能捞到标题里带同一个词的，"新的会出现，可能在其他地图"下周换的名字万一不带那个词
--- 就整族失联；目标行里的 questID 是客户端答的，不靠猜也不用玩家输 ID。
--- 只往**已经是家族成员**的那几条上扩（内置表锚点 + 关键词注册的那批），
--- 普通金币任务不去问目标行：那会把每条战役任务的步骤全注册进族里。
local FAMILY_EXPAND_LIMIT = 12

local function ExpandFamily(entries, seen)
    local Api = WQT.Api
    if not (Api and Api.QuestObjectives) then return 0 end
    local added = 0
    for i = 1, #entries do
        local e = entries[i]
        local rows = Api.QuestObjectives(e.questID)
        if rows then
            for j = 1, #rows do
                local child = tonumber(rows[j].questID)
                if child and child > 0 and not seen[child] and added < FAMILY_EXPAND_LIMIT then
                    seen[child] = true
                    added = added + 1
                    entries[#entries + 1] = { key = e.key, questID = child }
                end
            end
        end
    end
    return added
end

--- 注册周常逐个拿 questID 问一遍接口。名单两处来源：内置表 Api.WEEKLY_PRESETS
--- （丰饶供品这种玩家不可能知道 ID 的）+ 账本里已经带 regKey 的记录
--- （关键词补扫认出来的那些，见 HuntKeywordMaps）。
--- 这类任务不在世界任务池里，池子 diff 永远看不见它，做没做只能问角色本人：
--- 12.1.5 用户实测"配了关键词丰饶，丰饶供品有角色做完了也不进矩阵"，
--- 三条来源（池 / 任务日志 / 手动点）全都收不到它，光加关键词永远补不齐这一行。
--- 名字与地图也在这里现问：实测 C_TaskQuest.GetQuestInfoByQuestID 拿着角色从没见过
--- 的 questID 照样回中文标题 + mapID，所以玩家不需要知道 ID。
--- 状态只写进 db.chars[该角色].views —— 接口只回答"当前角色"，别的角色的列
--- 仍然只来自它们自己登录时刷的那一份。
local function RefreshRegistered(db, charKey, now)
    local Api = WQT.Api
    local presets = Api and Api.WEEKLY_PRESETS
    local entries, seen = {}, {}
    if type(presets) == "table" then
        for i = 1, #presets do
            local p = presets[i]
            if type(p.key) == "string" and p.key ~= "" then
                for j = 1, #(p.ids or {}) do
                    local questID = tonumber(p.ids[j])
                    if questID and questID > 0 and not seen[questID] then
                        seen[questID] = true
                        entries[#entries + 1] = { key = p.key, questID = questID }
                    end
                end
            end
        end
    end
    -- 关键词补扫注册过的行也照这份重问：它不在内置表里，只有账本知道它存在
    local known = (L.RegisteredIndex and L.RegisteredIndex(db)) or {}
    for i = 1, #known do
        local questID = tonumber(known[i].questID)
        if questID and not seen[questID] then
            seen[questID] = true
            entries[#entries + 1] = { key = known[i].key, questID = questID }
        end
    end
    -- 母任务的目标行里还藏着这一族的子任务 ID：先认亲，再一起去问名字/地图/状态
    local kin = ExpandFamily(entries, seen)
    if kin > 0 then
        WQT.Config.Log(("任务追踪认亲 +%d 条（母任务的目标行里问到的同族 ID）"):format(kin))
    end
    -- 名字与地图在这里现问：实测 C_TaskQuest.GetQuestInfoByQuestID 拿着角色从没见过
    -- 的 questID 照样回中文标题 + mapID，所以玩家不需要知道 ID
    for i = 1, #entries do
        local questID = entries[i].questID
        local mapID, mapSrc = Api.QuestMapID(questID)
        entries[i].name = Api.QuestTitle(questID)
        entries[i].mapID = mapID
        -- 来源标签必须带 `补问 ` 前缀，与 HealMissingMaps/DiffPool 写进账本的形状一致：
        -- Ledger.PropagateFamilyMaps 的可信白名单认的是 "补问 C_TaskQuest.GetQuestZoneID"，
        -- 这里原先把裸接口名直接落进 rec.mapSrc，于是注册周常自己问出来的图**永远
        -- 不算可信** —— 加固符文石(90573) 那条就是这么既不被族搬运认、又不被清理认的
        entries[i].mapSrc = mapID and ("补问 " .. tostring(mapSrc)) or nil
        entries[i].money = Api.QuestRewardMoney(questID)
        entries[i].state = select(1, Api.QuestState(questID))
        -- 这里**不问倒计时**：注册行是常驻周常，到期只认 CD 尾巴（Ledger.SetExpiry）。
        -- 上一版问出来的那条是它**板上实例**的轮换寿命（几小时），写成到期等于
        -- 当天下午就把这一行从矩阵里抹掉（12.1.5 实测："加固符文石这个任务没了"）。
    end
    if #entries == 0 then return 0 end
    -- 只在第一次建记录时报日志：这函数每轮快照都跑（矩阵开着时 10 秒一轮），
    -- 每轮报一条会把 /wqt log 刷满，真正要看的"这条 ID 本机问不出名字"另有 probe
    local before = 0
    for i = 1, #entries do
        if not (db.quests and db.quests[entries[i].questID]) then before = before + 1 end
    end
    local n = L.EnsureRegistered(db, entries, charKey, now)
    if before > 0 then
        WQT.Config.Log(("注册周常新入账 %d/%d 条"):format(before, n))
    end
    return n
end

--- 注册族这一轮还有哪些真的挂在跟踪地图上。用户 2026-10-24 定的模型：
--- **一族一行**，行由内置表那条 ID 顶着（SavedInstances 也只在 Progress.lua:1622-1632
--- 放了一条 type=single、questID=89507，板上那些"丰饶：草药洞穴"它根本不跟）；
--- 板上扫到的同名子任务只当两件事的证据：
--- ① 它自己这一轮的归属（占不占行由账本说了算，见 Ledger.OccupiesRow）；
--- ② 这一族本周还活着，并且本周它挂在哪张图 —— 把那张图写进族本体那条记录的 mapID。
---    （GetMapForQuestPOIs(89507) 猜过银月城，用户："丰饶任务不是银月城的"，
---    板面证据比它可靠；族本体自己就在图上时，第一条已经把它自己的地图改好了。）
--- 只认行自己报的 mapID；在没跟踪的图上 = 这轮拿不到证据，玩家把那张图加进跟踪清单就能看到。
local function ScanBoards(db)
    local member = {}
    for questID in pairs(db.quests or {}) do
        -- 认亲走 IsFamilyMember（= 内置表声明过的族：ID 在 ids 里，或名字命中 match 词）
        if L.IsFamilyMember(db, questID) then member[questID] = true end
    end
    if not next(member) then return 0 end
    local maps = (WQT.Config.TrackedMapIDs and WQT.Config.TrackedMapIDs()) or {}
    local hit = 0
    for k = 1, #maps do
        -- 单图问不出就跳过这张：拿它报错把整轮快照炸掉不值（池子那趟还要继续）
        local okRows, rows = pcall(WQT.Api.MapQuestRows, maps[k])
        if okRows and type(rows) == "table" then
            for n = 1, #rows do
                local questID = tonumber(rows[n].questID)
                if questID and member[questID] then
                    local rec = db.quests[questID]
                    rec.onBoard = true
                    -- 地图以行自己报的为准：换图之后这一行要跟着搬到新图上去
                    if rows[n].mapID and rec.mapID ~= rows[n].mapID then
                        rec.mapID, rec.mapSrc = rows[n].mapID, "跟踪地图清单"
                    end
                    -- 占行的那条是族本体，板上这条可能不是它：本体的地图跟着这轮的板面证据走
                    local preset = L.PresetFor(db, questID)
                    local anchorID = preset and L.IsPresetID(preset, questID)
                        and questID or (preset and preset.ids and tonumber(preset.ids[1]))
                    local anchor = anchorID and db.quests[anchorID]
                    if anchor and anchor ~= rec and rows[n].mapID
                        and anchor.mapID ~= rows[n].mapID then
                        anchor.mapID, anchor.mapSrc = rows[n].mapID, "跟踪地图清单"
                    end
                    hit = hit + 1
                end
            end
        end
    end
    return hit
end

--- 这条常驻周常该认哪块板：词 = 内置族词（丰饶那种）> kw: 前缀后面的那个关键词 >
--- 名字命中的关键词。玩家侧永远只输入过名字的一部分，所以这三样都是他亲手给的东西，
--- 不许在这里问 questID、也不许拿 pin 名字硬猜别的族。
function Sync.PinWordFor(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local preset = WQT.Ledger and WQT.Ledger.PresetFor and WQT.Ledger.PresetFor(db, questID)
    if preset and type(preset.match) == "string" and preset.match ~= "" then
        return preset.match
    end
    local key = rec.regKey
    if type(key) == "string" then
        local pre = (WQT.Ledger and WQT.Ledger.KW_PREFIX) or "kw:"
        if key:sub(1, #pre) == pre then return key:sub(#pre + 1) end
    end
    if type(rec.name) == "string" and WQT.Config.KeywordHit then
        return WQT.Config.KeywordHit(rec.name)
    end
    return nil
end

--- 同名 pin 挂在几张图上时选哪张（顺序不许换）：
--- ① 客户端自己标 isPrimaryMapForPOI 的那张（实测亚基入侵 8813 只在 2395 标 primary，
---    另外两张图也报它 —— 不认 primary 就会一张事件分出三个分组）；
--- ② 一条 primary 都没有 → 排掉"是别的候选图的父图"的那些（丰饶两条都 primary=false：
---    8527@2413 哈籁恩达尔 + 8676@2537 奎尔萨拉斯=父图，排完只剩真正办事的那张子图）；
--- ③ 还是多于一张 = 选不出，宁可不写。拿一张猜的图当分组依据，玩家会照着它跑错区。
local function PickPinMap(cands)
    local primary, count = nil, 0
    for mapID, isP in pairs(cands.primary) do
        if isP then count = count + 1 primary = mapID end
    end
    if count == 1 then return primary end
    if count > 1 then return nil end
    local kept = {}
    for mapID in pairs(cands.maps) do kept[#kept + 1] = mapID end
    if #kept == 1 then return kept[1] end
    table.sort(kept)
    local out = {}
    for i = 1, #kept do
        local orphan = true
        for j = 1, #kept do
            if i ~= j and WQT.Api.MapParentID(kept[j]) == kept[i] then
                orphan = false
                break
            end
        end
        if orphan then out[#out + 1] = kept[i] end
    end
    return #out == 1 and out[1] or nil
end

--- 地图事件 pin 的归属（用户 2026-10-24 定："如果知道去哪办 那就没必要放在祖阿曼了吧？
--- **事件在哪 就在哪个地图分组**"）。丰饶供品/亚基入侵这类压根不上世界任务板的东西，
--- 任务的三块清单全都问不到它，但按 M 看到的那块板（C_AreaPoiInfo）报得出名字和图。
--- 这一趟只写 evMapID/evName/evAt 三个派生字段，**不动 rec.mapID**：那是客户端给过的答案，
--- 清掉它等于把凭据一起扔了（pin 断了行会掉进"未知地图"，比留在旧分组更难查）。
--- 图源 = 跟踪清单（硬约束：不许遍历整棵地图树）。
--- 接口一次都没答（缺失或全报错）时不清上一轮的归属 —— "客户端没答"不等于"这块板没了"。
--- 答了但这一轮没有对应 pin 才清：pin 散了还挂着旧图 = 谎报一块不存在的板子。
--- 倒计时不从这里拿：12.1.5 实测同一条 pin 上一轮答 407906、这一轮跨图问整轮回 nil，
--- 而 IsAreaPOITimed 照样答 true，留着上一轮的数就是谎报。
function Sync.RefreshEventPins(db, now)
    if not (db and db.quests) or not (WQT.Api and WQT.Api.EventPins) then
        return 0, 0, false
    end
    local maps = (WQT.Config.TrackedMapIDs and WQT.Config.TrackedMapIDs())
        or (WQT.Api.MidnightMapIDs and WQT.Api.MidnightMapIDs()) or {}
    local byName, answered = {}, false
    for i = 1, #maps do
        local pins = WQT.Api.EventPins(tonumber(maps[i]))
        if pins then
            answered = true
            for j = 1, #pins do
                local name = pins[j].name
                if name then
                    local c = byName[name]
                    if not c then
                        c = { maps = {}, primary = {}, qids = {} }
                        byName[name] = c
                    end
                    c.maps[pins[j].mapID] = true
                    if pins[j].primary then c.primary[pins[j].mapID] = true end
                    if pins[j].questID then c.qids[pins[j].questID] = true end
                end
            end
        end
    end
    if not answered then return 0, 0, false end

    local chosen = {}
    for name, cands in pairs(byName) do chosen[name] = PickPinMap(cands) end

    -- 事件 pin 挂着真任务号时，关键词才有的行（用户 2026-10-24 第二十二轮："亚基入侵了，
    -- 关键字我已经加了，矩阵里还看不到"）：那种事件压根不在世界任务板上、也不在任务日志里，
    -- 关键词补扫（板面清单 + 日志）两条路都碰不到它。这里只认客户端自己写在 pin 上的
    -- questID —— 答不出就一行都不注册，绝不拿事件名去猜一个 ID（猜出来的行会把别的任务的
    -- 完成状态冒充成它的）。注册成 kw:<关键词> 与补扫同源：玩家删关键词就一起退掉。
    local entries = {}
    for name, cands in pairs(byName) do
        local mapID = chosen[name]
        local kw = mapID and WQT.Config.KeywordHit and WQT.Config.KeywordHit(name) or nil
        if kw then
            for qid in pairs(cands.qids) do
                local preset = WQT.Ledger.PresetByID and WQT.Ledger.PresetByID(qid)
                local title = WQT.Api.QuestTitle and WQT.Api.QuestTitle(qid) or name
                if not preset and not (WQT.Ledger.PresetByName
                    and WQT.Ledger.PresetByName(title)) then
                    entries[#entries + 1] = {
                        key = (WQT.Ledger.KW_PREFIX or "kw:") .. tostring(kw),
                        questID = qid, name = title, mapID = mapID,
                        mapSrc = "地图事件 pin",
                    }
                end
            end
        end
    end
    if #entries > 0 and WQT.Ledger.EnsureRegistered then
        local charKey = Sync.CurrentCharKey()
        for i = 1, #entries do
            local q = entries[i].questID
            entries[i].money = WQT.Api.QuestRewardMoney
                and WQT.Api.QuestRewardMoney(q) or nil
            entries[i].state = WQT.Api.QuestState
                and select(1, WQT.Api.QuestState(q)) or nil
        end
        local n = WQT.Ledger.EnsureRegistered(db, entries, charKey, now)
        if n > 0 then
            local names = {}
            for i = 1, #entries do names[i] = tostring(entries[i].name) end
            WQT.Config.Log(("地图事件里注册了 %d 条周常：%s（这几件不在世界任务板上，"
                .. "是任务号自己挂在地图 pin 上问出来的）")
                :format(n, table.concat(names, "、")))
        end
    end

    local written, cleared = 0, 0
    for questID, rec in pairs(db.quests) do
        local word = (rec.regKey or rec.kwLogAdded) and Sync.PinWordFor(db, questID) or nil
        local best
        if word then
            for name, mapID in pairs(chosen) do
                if mapID and name:find(word, 1, true)
                    and (not best or name < best.name) then
                    best = { name = name, mapID = mapID }
                end
            end
        end
        if best then
            if rec.evMapID ~= best.mapID or rec.evName ~= best.name then
                written = written + 1
            end
            rec.evMapID, rec.evName, rec.evAt = best.mapID, best.name, now
        elseif rec.evMapID then
            rec.evMapID, rec.evName, rec.evAt = nil, nil, nil
            cleared = cleared + 1
        end
    end
    return written, cleared, true
end

--- 池子的内容签名：这一轮看到的 questID 集合（排序后拼串）。
--- 只用来判断"板子换了没有"，不进任何显示。
local function PoolSignature(pool)
    local ids = {}
    for i = 1, #(pool or {}) do
        local questID = tonumber(pool[i] and pool[i].questID)
        if questID then ids[#ids + 1] = questID end
    end
    table.sort(ids)
    return table.concat(ids, ",")
end

--- 抓池 → diff → 写本角色快照。池不可用时直接返回，不做任何 diff：
--- 把"读不到"当成"全部消失"会清空整张账本，那比不更新严重得多。
--- 空池是数据（全清完了），但登录那一瞬接口可能还没就绪，所以要用会话时钟隔 30 秒
--- 二次确认为空才入库；期间任何一次非空读取都取消确认。
function Sync.SnapshotNow()
    local db = WQT.Config.DB()
    if not db then return nil end
    local now, uptime, domain = Clock()
    db.clockDomain = domain
    if domain ~= "epoch" then
        WQT.Config.Log("时钟域降级：无可用墙上时钟，时间戳只在本次开机内可比")
    end
    local rolled = WQT.Config.RefreshCycle(domain == "epoch" and now or nil, uptime)
    if rolled and WQT.Ledger and WQT.Ledger.ClearCycleIgnores then
        -- 奖励每周会变（用户 2026-10-24）：上个 CD 点的"这行别显示"不许再管这一周
        local lifted = WQT.Ledger.ClearCycleIgnores(db)
        if lifted > 0 then
            WQT.Config.Log(("换了 CD：解除 %d 条「本周别显示」"
                .. "（这轮该不该列，重新按在不在池里/金币门槛/关键词判）"):format(lifted))
        end
    end
    if db.cycleAnchor == 0 then
        WQT.Config.Log("cycleAnchor 未知：周统计口径可能不准")
    end
    if WQT.Ledger and WQT.Ledger.ResetStickyGoldOnce then
        -- 老存档里被旧代码写坏的金币凭据不会自愈（粘滞只在换轮时作废），
        -- 不清就得等下一次刷新才看得见修法生效 —— 一次性作废，本轮重新读。
        -- 同一份存档里旧版本还把金额搬进了"金额压根没读出来"那份记录，
        -- 光清粘滞会被下一次同步照着它再写一遍，所以那一半也在这里一起清。
        local cleared = WQT.Ledger.ResetStickyGoldOnce(db)
        if cleared > 0 then
            WQT.Config.Log(("作废 %d 条任务里旧版本写错的金币记录"
                .. "（上一轮的金额记进了本轮）：这一次同步重新读，"
                .. "真给金币的会自己回来"):format(cleared))
        end
    end
    -- 角色行在这里建，不跟到池后面：注册周常的状态也写进这一行的 views，
    -- 那几条压根不依赖池，池读不到时照样要有一列可写
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
    -- 注册周常排在取池之前：这一行不靠池、只靠接口，池不可用（12.x 常见降级）
    -- 时更不该跟着一起不刷 —— 丰饶供品那种任务本来就是池里问不到的
    RefreshRegistered(db, charKey, now)
    -- 地图事件的归属也排在取池之前：这块板压根不在世界任务池里，池不可用时更不该跟着一起瞎
    local pinWritten, pinCleared, pinAnswered = Sync.RefreshEventPins(db, now)
    if not pinAnswered then
        WQT.Config.Log("地图事件接口这轮没答（C_AreaPoiInfo 缺失或报错）：归属图沿用上一轮")
    elseif pinWritten > 0 or pinCleared > 0 then
        WQT.Config.Log(("地图事件归属：写入 %d 条、清掉 %d 条（这块板不在世界任务池里）")
            :format(pinWritten, pinCleared))
    end
    local pool, err, meta = WQT.Api.GetPool()
    -- 部分池（12.x 按图源只看到当前地图一片）：不在池里的任务不标消失，
    -- 空池二次确认也只对全量池有意义 —— 部分池为空只说明脚下这张图没有。
    local partial = not not (meta and meta.partial)
    if not pool then
        -- 板面证据问的是另一个接口（跟踪地图清单），池不可用时别跟着一起瞎：
        -- 只加不减 —— 上一轮的证据留着，这一轮新扫到的补上。
        ScanBoards(db)
        WQT.Config.Log("快照跳过：池不可用 " .. tostring(err))
        return { degrade = tostring(err) }
    end

    -- 板面证据每轮重问：上一轮挂在板上的这一轮可能已经换图/到期了。
    -- 部分池只看得见脚下这张图，清一次会把别的图上的证据抹掉 → 那一轮不清。
    -- 放在空池二次确认之前：清完就早退会把刚补扫到的行抹掉（30 秒后再问才有）。
    -- 注册族的板面证据必须在消失判定之前问完：这一轮还挂在图上的要留证。
    if not partial then
        for _, rec in pairs(db.quests or {}) do
            -- onBoard 是家族成员专用的字段（只有 ScanBoards/池/补扫会写），
            -- 所以清的时候不必再判一次认亲：有就清，这一轮的证据这一轮重问。
            if rec.onBoard then rec.onBoard = nil end
        end
    end
    ScanBoards(db)

    if #pool == 0 and not partial then
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

    local res = L.DiffPool(db, pool, now, partial)
    -- 体积清理只有登录这一个触发点：不接这里，Prune 就永远不跑。
    L.Prune(db, now)

    local inPool = {}
    local pendingMoney, pendingTime = 0, 0
    for i = 1, #pool do
        local questID = pool[i].questID
        inPool[questID] = true
        -- 池里出现 = 这一轮它确实挂在某张板上：族成员（含板上那条同名子任务）留下这份证据，
        -- 它参与"这族本周做过没有"，但占不占行由 Ledger.OccupiesRow 说（一族一行 = 族本体）
        local poolRec = db.quests[questID]
        if poolRec and L.IsFamilyMember(db, questID) then poolRec.onBoard = true end
        if type(pool[i].money) ~= "number" then pendingMoney = pendingMoney + 1 end
        -- 每条任务自己的接口读取单独容错：QuestState/QuestTimeLeft 在某一条上抛错
        -- 不该把整趟快照断在这儿——后面还有收日志、补地图、族搬运、消失判定，
        -- 以及"重排刷新定时器"。半途崩掉等于账本停在半更新、矩阵开着也不再刷新。
        -- 代价：每条多两次 pcall（Lua 里 pcall 不碰接口，只是调用壳，不多一次真接口调用）。
        local okCall, state1 = pcall(WQT.Api.QuestState, questID)
        if not okCall then
            WQT.Config.Log(("状态问不出 quest=%s: %s"):format(tostring(questID), tostring(state1)))
            state1 = nil
        end
        local rec = db.quests[questID]
        local rows = db.turnins[charKey] and db.turnins[charKey][questID]
        local last = rows and rows[#rows]
        -- 边界问 Ledger 拿（这一条实例自己的起点，见 L.RoundAt）：拿 obs.firstSeen
        -- 比就等于拿奖励签名当轮次身份，切一次狩猎模式这份交任务记录当场不算数了
        -- 本轮亲眼记到的交任务优先于查询接口。t 可能是老存档里缺字段的记录
        -- （L.WeekStats 自己就写着 if r.t and ...），拿 nil 比大小会当场抛错。
        local lastT = last and tonumber(last.t)
        if rec and lastT and lastT >= L.RoundAt(db, rec) then
            state1 = "done"
        end
        -- 视图统一走 Ledger.SetView 写，避免 { s, t } 这个形状在两处各拼一遍
        L.SetView(db, charKey, questID, state1, now)
        -- 记下到期时刻：世界任务到点才换，据此判断下次什么时候真需要刷新。
        -- 读不到（接口偶发 nil / 数据没加载 / 抛错）也计数：补读要连倒计时一起补，
        -- 否则有的任务永远没有剩余时间（用户实测"有个任务没有倒计时了"）
        local secs, timeFailed = nil, false
        if WQT.Api.QuestTimeLeft then
            local okT, v = pcall(WQT.Api.QuestTimeLeft, questID)
            if okT then secs = v else timeFailed = true end
        end
        if type(secs) == "number" and secs > 0 then
            -- 口径由 Ledger.SetExpiry 定：池里这条若是**注册过的常驻周常**（regKey/kwLogAdded），
            -- 问出来的倒计时是它板上实例的轮换寿命（几小时），不能当到期写 —— 12.1.5 实测
            -- "加固符文石这个任务没了"就是这么下午整行消失的；只有普通世界任务照旧用它自己的
            -- 倒计时，写来源、清掉排程值标记，矩阵才允许上色/报剩余。
            if rec then L.SetExpiry(rec, now, secs) end
        elseif not timeFailed then
            pendingTime = pendingTime + 1
        end
    end
    -- 关键词的第二条来源：先收任务日志，再判"不在池里"。
    -- 顺序反了会自我打架 —— 刚由日志收进来的行本来就不在世界任务池里，
    -- 先跑消失判定会把它当场标成 ended，等于白收一趟。
    local logAdded = SyncKeywordQuestsFromLog(db, charKey, now, inPool)
    -- 已停用的地图入口写进账本的旧地图号清掉：不清的话 HealMissingMaps 只看 mapID==nil，
    -- 永远不去重问，用户下次开矩阵看到的还是"丰饶在屠戮者高地"（12.1.5 反馈⑥）。
    -- 只清这三条谎报接口自己写的号；旧版没标来源的号由 ClearRetiredMaps 保留并打标，
    -- 别一并清 —— 清掉的整批任务全塌进"未知地图"（第十五轮反馈②③）。
    if WQT.Api and WQT.Api.RetiredMapSources then
        local cleared = L.ClearRetiredMaps(db, WQT.Api.RetiredMapSources())
        if cleared > 0 then
            WQT.Config.Log(("清掉 %d 条谎报接口的地图号（只认它自己写的来源），重新按可信链问")
                :format(cleared))
        end
    end
    -- 玩家口述的地图先补一轮（第十七轮）：这批任务接口答不出，等 HealMissingMaps 白问
    local fixedMaps = Sync.ApplyMapMemory(db)
    if fixedMaps > 0 then
        WQT.Config.Log(("按玩家口述补上 %d 条所属地图（标签=玩家指定，接口问得出真凭据会覆盖它）")
            :format(fixedMaps))
    end
    -- 补地图放在收日志之后：这一轮新收的那几条也要当场有地图，不然矩阵底部
    -- 先冒出一堆"未知地图"、要等下次同步才归位
    HealMissingMaps(db)
    -- 族本体的地图要在补问之后搬：成员自己那条的图可能正是上面这一步刚问出来的
    -- （判据与来源白名单见 Ledger.PropagateFamilyMaps）
    L.PropagateFamilyMaps(db)
    if not partial then
        for questID, rec in pairs(db.quests) do
            -- kwLogAdded = 这行来自任务日志（城市日常/战役任务），它天生不在世界任务池，
            -- 拿"不在池里"判它消失，等于每次快照都把刚收的任务踢出去。
            -- regKey 同理更要豁免：家族锚点是"这周做没做"的证据，刚从接口问出来的状态
            -- 被这句改成 ended 等于白问一趟（丰饶供品做完了也永远显示没做）。
            -- 它在不在界面上由 Ledger.OccupiesRow 说了算（一族一行、板上子任务只当证据），
            -- 不靠改状态。
            if not inPool[questID] and not rec.kwLogAdded and not rec.regKey then
                L.SetView(db, charKey, questID, "ended", now)
                -- 不在池里的任务不占着"最早到期"，否则刷新时刻永远被它拖着
                rec.expiresAt, rec.expiresSrc = nil, nil
            end
        end
    end

    res.poolCount = #pool
    res.partial = partial
    res.logAdded = logAdded
    -- 板子一换就重扫跟踪地图：轮换任务换了 questID，注册族里只存着老 ID 的话，
    -- 新 ID 的名字/地图/轮换倒计时永远进不了这一行。SavedInstances 的办法是让维护者
    -- 每赛季手抄新 ID（Modules/Progress.lua 的预设表就是那份手抄名单），我们没有这条路
    -- —— 玩家不可能知道 questID，只能自己扫。补扫现在只走跟踪地图，代价可控。
    -- 先落签名再扫：HuntKeywordMaps 末尾会再同步一次，不落账就是无限递归。
    local sig = PoolSignature(pool)
    if sig ~= "" and not partial and sig ~= db.lastPoolSig then
        db.lastPoolSig = sig
        if WQT.Config.GetKeywords and #WQT.Config.GetKeywords() > 0 then
            WQT.Config.Log("世界任务池已轮换：自动补扫跟踪地图")
            pcall(Sync.HuntKeywordMaps)
        end
    end
    WQT.Config.Log(("快照完成 池=%d 部分=%s 源=%s 新增=%d 消失=%d 重现=%d 奖励变化=%d 日志收=%d 锚点=%d 角色=%s")
        :format(#pool, tostring(partial),
            (meta and meta.source) or "全量源",
            #res.added, #res.gone, #res.renewed, #res.sigChanged, logAdded,
            db.cycleAnchor or 0, charKey))
    ScheduleNextRefresh()
    -- 有任务金额/倒计时还没读到：排一个短间隔补读（预加载数据几十秒后才到），
    -- 全读到了就清零计数。矩阵没开时不排（SchedulePreloadRetry 内部判断）
    if pendingMoney > 0 or pendingTime > 0 then
        preloadRetries = preloadRetries + 1
        SchedulePreloadRetry()
    else
        preloadRetries = 0
    end
    return res
end

local function ScheduleSnapshot()
    if debouncePending then return end
    -- 只有两趟会真的抓池：① 登录那一次（loginPending，设置里可关）；
    -- ② 矩阵开着期间（polling：打开时由 Matrix.Toggle 主动抓一次，
    -- 之后开着面板的时间里事件照常去抖触发）。其余一律静默——一次快照要把每条任务的
    -- 奖励/状态/倒计时逐个问一遍，做任务时事件连成一片，不设这道门就是卡顿本身。
    if not polling and not loginPending then return end
    debouncePending = true
    C_Timer.After(DEBOUNCE_SECONDS, function()
        debouncePending = false
        local wasLogin = loginPending
        loginPending = false
        local up0 = select(2, Clock())
        local okCall, err = pcall(Sync.SnapshotNow)
        if not okCall then WQT.Config.Log("快照异常: " .. tostring(err)) end
        if wasLogin then
            -- 耗时用会话秒（单调、与墙上时钟域无关）：这是"登录卡不卡"的唯一实测口径
            local spent = select(2, Clock()) - up0
            if spent < 0 then spent = 0 end
            WQT.Config.Log(("登录同步完成 用时%d秒（矩阵没开，之后不再自动抓池）"):format(spent))
        end
        -- 快照半路抛错时它自己末尾那句"按最早到期排下次刷新"跑不到：这里补一次，
        -- 否则矩阵开着却再也没有下一次刷新（12.1.5 遗留的"补读会话死锁"就是这个形状）。
        -- ScheduleNextRefresh 第一句就 Cancel 旧定时器，重复调用只会留一个待触发。
        pcall(ScheduleNextRefresh)
        -- 抓完必须喊面板重画：这条是日常唯一自动跑的路（做任务时事件连成一片），
        -- 少了这一句就是"账本里金额/状态都更新了，屏幕上还是上一份"——12.1.5 实测
        -- 89347 那行 967 金进账本 12 分钟了矩阵里还是没有。重画只把内存里的账本再抄
        -- 一遍，零接口调用，且 Mx.Refresh 自己判面板开没开（没开就直接返回）。
        if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
    end)
end

--- 关键词补扫（设置面板点一次）：只问"跟踪清单"里的图 = 默认(本资料片) ∪ 用户额外加的。
--- 普通快照按图收的是世界任务池，虚空侵袭/亚基入侵这类周常天生不在池里，
--- 光加关键词永远等不到那一行（用户实测"加了符文石/追踪萨拉塔斯没有对应任务"）。
--- 上一版这里遍历整棵地图树（最多 800 张）问，用户明确否决："应该不需要全图扫描吧，
--- 我觉得只需要扫描用户添加的地图就好了，包括默认地图" —— 任务挂在哪张图是玩家自己的事，
--- 没跟踪的图就不该替他猜；命中 0 时把这条原因写在回报里，指向"＋添加当前所在地图"。
--- 问图走 Api.MapQuestRows（C_TaskQuest.GetQuestsOnMap，不过世界任务滤）。
--- 命中做两件事：① 行自己报的地图写进 extraMaps，之后普通快照自动带上它；
--- ② 建一条注册周常（regKey = "kw:" .. 关键词），名字与地图都由接口答出来，
---    玩家只需要输入任务名的一部分 —— 之后每轮快照按账本里的 regKey 重问状态。
--- 清单就十几张图，一轮问完跟一次普通快照同量级，不需要再分批挂定时器。
local KW_KEY = L.KW_PREFIX
function Sync.HuntKeywordMaps()
    local kws = WQT.Config.GetKeywords()
    if #kws == 0 then return false, "先添加关键词" end
    local tracked = (WQT.Config.TrackedMapIDs and WQT.Config.TrackedMapIDs())
        or (WQT.Api and WQT.Api.MidnightMapIDs and WQT.Api.MidnightMapIDs())
    -- 图源再加一条：族锚点自己报的"地图指针"。12.1.5 实测 GetMapForQuestPOIs(89507)
    -- 上周答 2393（银月城）、本周答 2437（祖阿曼），与游戏自带的地图气泡同一张图
    -- —— 轮换型周常每周搬区，玩家没把新区加进跟踪清单时补扫必然命中 0 条
    -- （用户回传："按关键词…在候选里命中 0 条"，同时截图那条在祖阿曼）。
    -- 这仍然不是全图扫描：一条锚点多问一张它自己报出来的图，数量有界。
    local maps, pushed = {}, {}
    local function push(m)
        m = tonumber(m)
        if m and m > 0 and not pushed[m] then
            pushed[m] = true
            maps[#maps + 1] = m
        end
    end
    for i = 1, #(tracked or {}) do push(tracked[i]) end
    local dbNow = WQT.Config.DB()
    local anchors = (dbNow and L.RegisteredIndex and L.RegisteredIndex(dbNow)) or {}
    for i = 1, #anchors do push(WQT.Api.QuestMapID(anchors[i].questID)) end
    if #maps == 0 then
        return false, "跟踪清单是空的：先站到任务所在的地图上点\"＋添加当前所在地图\""
    end
    local hits, added, familySkipped = 0, 0, 0
    local found, seenID = {}, {}
    for k = 1, #maps do
        local mapID = maps[k]
        -- 单图问不出数据（接口报错/返回怪形状）就跳过这张，不许把整轮补扫炸掉
        local okRows, rows = pcall(WQT.Api.MapQuestRows, mapID)
        for n = 1, (okRows and type(rows) == "table" and #rows or 0) do
            local questID = rows[n].questID
            local name = questID and WQT.Api.QuestTitle(questID)
            local kw = name and WQT.Config.KeywordHit(name)
            -- 命中内置族词的不注册：那一行归内置表管（族本体占行）。玩家关键词写着"丰饶"时
            -- 把板上"丰饶：草药洞穴"注册成 kw:丰饶，等于又造出一行周常以外的任务
            -- （用户 2026-10-24："我鼠标悬停的丰饶 和 我真正要完成的 丰饶日常 不是一个任务"、
            -- "不显示它（它不是周常）"；上一轮的截图 bug 是"有 2 个丰饶"）。
            local preset = questID and (L.PresetByID(questID) or L.PresetByName(name))
            if kw and preset then
                if not seenID[questID] then
                    seenID[questID] = true
                    familySkipped = familySkipped + 1
                end
            end
            if kw and not preset and not seenID[questID] then
                seenID[questID] = true
                hits = hits + 1
                found[#found + 1] = {
                    key = KW_KEY .. tostring(kw), questID = questID, name = name,
                    -- 只认行自己报的地图；"这次问的是哪张图"不算证据
                    -- （上一版拿问的图兜底，一整批任务被归进同一张图）
                    mapID = rows[n].mapID,
                    -- 刚刚亲眼在图上看到的，这一轮就该成行（ScanBoards 之后每轮复查）
                    onBoard = true,
                }
                WQT.Config.Log(("补扫命中：%s（任务#%s，关键词「%s」，地图 %s）")
                    :format(tostring(name), tostring(questID), tostring(kw),
                        tostring(rows[n].mapID or mapID)))
                if WQT.Config.AddMap(rows[n].mapID or mapID) then added = added + 1 end
            end
        end
    end
    local reged = 0
    local db = WQT.Config.DB()
    if db and #found > 0 then
        local now = select(1, Clock())
        local charKey = Sync.CurrentCharKey()
        for j = 1, #found do
            local questID = found[j].questID
            found[j].money = WQT.Api.QuestRewardMoney(questID)
            found[j].state = select(1, WQT.Api.QuestState(questID))
        end
        reged = L.EnsureRegistered(db, found, charKey, now)
    end
    -- "注册周常"这一列是**新增/重问的 kw 行**，hits 里被族词跳过的那几条另报：
    -- 玩家看到"命中 0 条"会以为关键词写错了，实际是那条本来就由内置表管着
    -- （12.1.5 实测反馈"按关键词…命中 0 条"配着丰饶那条明明在做）。
    local skipNote = familySkipped > 0
        and ("；另有 %d 条命中内置族词，那一行由内置表管，不重复注册"):format(familySkipped) or ""
    WQT.Config.Log(("关键词补扫：%d 张图（跟踪清单 + 族锚点报的图）× %d 个关键词，命中 %d 条任务，"
        .. "新增跟踪 %d 张地图，注册 %d 条周常%s"):format(#maps, #kws, hits, added, reged, skipNote))
    pcall(Sync.SnapshotNow)
    if WQT.Matrix and WQT.Matrix.Refresh then pcall(WQT.Matrix.Refresh) end
    if hits == 0 then
        -- "命中 0 条"对玩家只有一句话：我加了关键词为什么没有行。原因分两种，得说清是哪种：
        -- ① 这名字压根不在跟踪清单的板上（图没加进来 → 站到那张图上点＋添加）；
        -- ② 它是**地图事件 pin**（亚基入侵那种），板面清单与任务日志两条路都碰不到它 ——
        --    能不能成行只看客户端给没给这个 pin 挂任务号（给了由 RefreshEventPins 注册）。
        local pinNote = ""
        if WQT.Api.EventPins then
            local hitNames = {}
            for k = 1, #maps do
                local okPins, pins = pcall(WQT.Api.EventPins, tonumber(maps[k]))
                for j = 1, (okPins and type(pins) == "table" and #pins or 0) do
                    local kw = pins[j].name and WQT.Config.KeywordHit(pins[j].name)
                    if kw and not hitNames[pins[j].name] then
                        hitNames[pins[j].name] = pins[j].questID or false
                    end
                end
            end
            local parts, plain = {}, {}
            for name, qid in pairs(hitNames) do
                if qid then
                    parts[#parts + 1] = ("「%s」任务号=%s"):format(name, tostring(qid))
                else
                    plain[#plain + 1] = "「" .. name .. "」"
                end
            end
            table.sort(parts); table.sort(plain)
            if #parts > 0 then
                pinNote = ("；地图事件 pin 里有 %d 条带任务号的已按关键词注册（%s）")
                    :format(#parts, table.concat(parts, " "))
            end
            if #plain > 0 then
                pinNote = pinNote .. ("；另有 %s 只在地图事件 pin 里，客户端没给它任务号 "
                    .. "⇒ 那不是任务，矩阵做不出「哪个角色做过」，取证看 /wqt probe 的事件段")
                    :format(table.concat(plain, "、"))
            end
        end
        return true, ("%d 张图没有命中：先确认关键词写的是任务名的一部分，"
            .. "任务在没跟踪的图上时站到那张图上点\"＋添加当前所在地图\"（或按名称添加）再补扫%s%s")
            :format(#maps, skipNote, pinNote)
    end
    return true, ("%d 张图扫完：命中 %d 条，注册 %d 条周常（新增跟踪 %d 张地图）%s")
        :format(#maps, hits, reged, added, skipNote)
end

--- 删关键词 → 退掉由它注册的行。内置预设那一行拆不到：它的 key 不带 kw: 前缀，
--- 玩家删"丰饶"不该把丰饶供品从内置表里摘掉。
function Sync.UnregisterKeyword(kw)
    local db = WQT.Config.DB()
    if not db or not kw then return 0 end
    local n = L.UnregisterByKey(db, KW_KEY .. tostring(kw))
    if n > 0 then
        WQT.Config.Log(("关键词「%s」已删除，退掉 %d 条注册周常"):format(tostring(kw), n))
    end
    return n
end

--- 池子还是不是新鲜的：账本里所有任务的到期时刻都没到 = 世界任务还没换。
--- 一条到期时间都没有（从没同步过/接口不给时间）→ false（该抓了）。
--- 时钟域与 SnapshotNow 写 expiresAt 时用的同一个 Clock，不会两边对不上。
function Sync.PoolFresh()
    local db = WQT.Config.DB()
    if not db then return false end
    local due = L.NextRefreshAt(db)
    if not due then return false end
    local now = select(1, Clock())
    return now < due
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
    -- 登录抓一次（默认开，设置面板与 Config.SetSyncOnLogin 可关）。只排这一趟，
    -- 之后没开面板就不再有抓池动作：门闩在去抖回调里落下，不会累积。
    loginPending = WQT.Config.SyncOnLogin() == true
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
--- 传入 seq 时必须仍是当前窗口，否则那是上一个窗口遗留的定时器：串台结算会
--- 拿新窗口的起点配旧刻度，钱算少。
function Sync.FinishWindow(seq)
    local c = capture
    if not c then return end
    if seq and c.seq ~= seq then return end
    local db = WQT.Config.DB()
    if not db then
        -- 账本没就绪：这一笔不能丢（丢了等于"做完了也不记"），窗口保留并再排一次。
        -- 只补一个定时器（有句柄才不重复排），不会滚成无限循环。
        WQT.Config.Log("交任务结算推迟：账本未就绪，窗口保留")
        if not windowTimer then
            local keep = c.seq
            windowTimer = C_Timer.NewTimer(MONEY_WINDOW_SECONDS, function()
                windowTimer = nil
                Sync.FinishWindow(keep)
            end)
        end
        return
    end
    capture = nil
    if windowTimer then pcall(function() windowTimer:Cancel() end) windowTimer = nil end
    if sampleTimer then pcall(function() sampleTimer:Cancel() end) sampleTimer = nil end
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
    local charKey = Sync.CurrentCharKey()
    local rec = db.quests[c.questID]
    L.OnTurnIn(db, charKey, c.questID, c.t0, gain, rec and rec.currentSig or nil)
    L.SetView(db, charKey, c.questID, "done", c.t0)
    WQT.Config.Log(("交任务 quest=%s 入账=%s 窗口污染=%s")
        :format(tostring(c.questID), WQT.Config.FormatGold(gain), tostring(polluted)))
    -- 记完这笔也要重画：本周交过 = 矩阵六道准入门里的硬凭据之一（Ledger.MatrixRows），
    -- 不重画的话玩家刚做完一条金币任务，面板上那一格还是旧的。零接口调用。
    if WQT.Matrix and WQT.Matrix.Refresh then WQT.Matrix.Refresh() end
end

--- 窗口内每隔几秒盯一次：交完任务顺手开邮箱捡钱再关掉，只在结算那一刻采样会漏判，
--- 把那笔外来入账记到任务头上。命中一次就记 polluted，结算时整笔作废。
--- 窗口没了就自己停：它不再无限递归，也不会面板关闭后仍每 5 秒问一次接口。
local function SampleWindow()
    if not capture then sampleTimer = nil return end
    if MoneyUntrusted() then capture.polluted = true end
    sampleTimer = C_Timer.NewTimer(SAMPLE_SECONDS, SampleWindow)
end

function Sync.BeginCapture(questID)
    questID = tonumber(questID)
    if not questID then return end
    local db = WQT.Config.DB()
    -- 只给"有可信身份"的任务开钱窗：账本里没这条、接口也不说它是世界任务 → 不记。
    -- 接口答 nil（问不出）时同样不记：少记一笔能忍，假凭据会把整行留在矩阵里骗人。
    if not (db and (L.QuestKnown(db, questID)
        or (WQT.Api.IsWorldQuest and WQT.Api.IsWorldQuest(questID) == true))) then
        return
    end
    if capture then
        -- 同一任务的 QUEST_COMPLETE 与 QUEST_TURNED_IN 会连着来：起点必须留在最早那次，
        -- 否则钱在两个事件之间到账，起点被覆盖成到账后的数，这笔金币会记成 0。
        if capture.questID == questID then return end
        -- 换任务了：先按已走过的窗口结算上一笔。窗口短一点只是近似（用户要的就是"记个大概"），
        -- 整笔丢掉会在账本上显示成"没做"，那才是真错。
        Sync.FinishWindow()
    end
    captureSeq = captureSeq + 1
    local seq = captureSeq
    capture = { questID = questID, before = WQT.Api.Money(), t0 = Clock(), seq = seq }
    if windowTimer then pcall(function() windowTimer:Cancel() end) end
    windowTimer = C_Timer.NewTimer(MONEY_WINDOW_SECONDS, function()
        windowTimer = nil
        Sync.FinishWindow(seq)
    end)
    if sampleTimer then pcall(function() sampleTimer:Cancel() end) end
    -- 首采样仍按 5 秒后排（和原 C_Timer.After 同口径）：BeginCapture 当场问一圈
    -- 帧可见性是无中生有的接口调用，也改掉了"开窗一刻邮箱正好开着"的既有判据。
    sampleTimer = C_Timer.NewTimer(SAMPLE_SECONDS, SampleWindow)
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

-- 只给离线用例读的钱窗状态（不调任何接口，零代价）
function Sync.__capturing() return capture ~= nil end
function Sync.__captureSeq() return captureSeq end

WQT.RegisterCommand("sync", function()
    local res = Sync.SnapshotNow()
    if not res then
        print("WQT: 同步失败，详情 /wqt log")
    elseif res.degrade then
        print("WQT: 池不可用（" .. res.degrade .. "），账本保持不变")
    elseif res.pendingEmpty then
        print("WQT: 池为空，30 秒后二次确认（先不动账本）")
    else
        -- 带上"日志收"：用户实测关键词命中 0，这一行就是判"第二条来源接通没"的现场证据
        print(("WQT: 池 %d 条，新增 %d，消失 %d，重现 %d，奖励变化 %d，日志收 %d")
            :format(res.poolCount, #res.added, #res.gone, #res.renewed, #res.sigChanged,
                res.logAdded or 0))
    end
end, "立即重跑一次池快照 diff")
