-- 同步层：登录抓世界任务池并 diff；交任务事件当场记完成凭据（到账金币改由奖励读取负责）。
-- 所有 WoW API 经 GQL.Api，本文件只管编排与时序。

GQL.Sync = {}
local Sync = GQL.Sync

local L = GQL.Ledger

--- 地图来源标签（Config.MAP_SRC）的取法。写成函数而不是文件级常量：本文件里这几处
--- 写入点（日志分区/跟踪清单/补问/事件 pin）落进 rec.mapSrc 后，判据全在 Ledger 那两张
--- 表里按串比对，串散成两处就等于又有一份"谁说的这张图"各写各的。
local function mapSrcLabel(key)
    local M = GQL.Config and GQL.Config.MAP_SRC
    return M and M[key] or ""
end

-- 事件去抖：QUEST_LOG_UPDATE 在任何任务状态变化时都发（每杀一个目标、每拾取一个
-- 物件都算），做任务时几乎连成一片。一次快照要把每个任务的奖励/时间/状态逐个问
-- 一遍，2 秒一发就是"开着矩阵做任务时时不时卡一下"。世界任务池只在到期时刻换
-- （另有 ScheduleNextRefresh 定点唤醒兜底），10 秒足够，面板上不会明显滞后。
local DEBOUNCE_SECONDS = 10
local EMPTY_CONFIRM_SECONDS = 30
-- 事件名候选按"本机认不认识"逐个试，12.x 里哪个还在没有实测依据
local POOL_UPDATE_EVENTS = { "WORLD_QUEST_LOG_UPDATE", "QUEST_LOG_UPDATE" }
local TURN_IN_EVENTS = { "QUEST_COMPLETE", "QUEST_TURNED_IN" }

local clickGuard = false
local debouncePending = false
-- 后台轮询开关，默认关：12.x 一次池快照要把几百张图逐个问一遍（GetQuestsOnMap），
-- 用户实测"做什么都一卡一卡"，屏蔽插件就不卡 —— 罪魁就是平时也在抓池。
-- 只有矩阵打开时（Matrix.Toggle 调 SetPolling(true)）才轮询，关上面板立刻停。
-- /gql sync 手动命令不走这个开关，随时能抓。
local polling = false
--- "这一问是在世界地图开着的情况下问的吗"——取证行的固定一段。
--- 为什么问这个：11-04 之前所有结束时刻都是在矩阵开着的那次遍历里读到的，
--- 日志里没有一次记下"当时地图开没开"，所以分不出"这条自己不给"和"图数据没到"。
local function WorldMapOpenText()
    local frame = GQL.Api and GQL.Api.Resolve and GQL.Api.Resolve("WorldMapFrame")
    if not (frame and frame.IsShown) then return "判不了" end
    return frame:IsShown() and "开着" or "没开"
end
local emptyPending = nil     -- 首次读到空池时的会话秒数，nil = 没有待确认
-- 登录那一次快照的门闩：OnWorldEnter 置位，去抖回调跑完立刻落下。
-- 不复用 polling：polling 的含义是"矩阵开着期间事件驱动也抓"，混用会让关掉面板后
-- 每次任务状态变化都抓一遍池（12.1.5 实测"做什么都一卡一卡"的成因）。
local loginPending = false

function Sync.ClickGuard() return clickGuard end
function Sync.SetClickGuard(v) clickGuard = not not v end

-- 前向声明：SetPolling 关轮询时要取消挂起的刷新定时器，真身在下面定义
-- （Lua 的 local 要先声明才能被上面的闭包捕获，直接引用会变成全局 nil）
local CancelRefreshTimer

--- 开/关后台池轮询。矩阵打开时开，关闭时停 —— 关的时候顺手取消
--- 挂起的刷新定时器，别留着到点还跑一趟。
function Sync.SetPolling(enabled)
    polling = not not enabled
    if not polling then
        if CancelRefreshTimer then CancelRefreshTimer() end
    end
end

function Sync.Polling() return polling end

-- 入库的时间戳一律用墙上时钟：GetTime() 每次开机归零，跟账本里跨开机存的 CD 锚点、
-- 历史交任务时间不是同一个域，混用会让周统计静默算错。没有可用墙上时钟时退回 GetTime()，
-- 并把用的哪个域记进 db.clockDomain，由 /gql dump 如实标注"本次开机内才可比"。
local function Clock()
    local wall = GQL.Api and GQL.Api.WallClock() or nil
    local uptime = math.floor((GQL.Api and GQL.Api.Uptime()) or 0)
    if wall then return wall, uptime, "epoch" end
    return uptime, uptime, "uptime"
end

--- 【11-04 定稿 §二 1】账本里那条结束时刻还活着 = 板上还是同一条实例。
--- 最后一档（远过一个 CD + 一天）是脏数据门：写坏了的时刻必须判"不算活着"⇒ 照旧重问。
local function ExpiryIsLive(rec, now)
    local knownEnd = rec and tonumber(rec.roundEnd)
    return not not (knownEnd and (knownEnd > 1e9) == (now > 1e9)
        and knownEnd > now and (knownEnd - now) <= 691200)
end

--- 【11-04 定稿 §二 1/2】这一问只在"账本还没定下来"时问。
--- roundEnd 就是这条实例离开板面的那一刻：还在未来 ⇒ 板上还是同一条 ⇒ 再问只会问出
--- 同一个时刻。池循环与现场问答共用这一处判据；日志旁路只用上面那道"活没活"的门
--- （它有自己的 isRotation 闸门，且代码明确不按 regKey 排除特别任务，见 :539-542）。
--- 代价：L.KeywordLogAdmits 会问 Config.KeywordMatch（字符串匹配，随玩家配置变化），
--- 每行每趟最多多一次纯 Lua 匹配；不是接口调用，但也不是零代价。
local function ShouldAskExpiry(db, questID, rec, now)
    if ExpiryIsLive(rec, now) then
        return false                         -- 同一条实例，答案已经答过了
    end
    if rec and ((rec.regKey and not L.IsPinRegKey(rec.regKey)) or rec.kwLogAdded
        or L.KeywordLogAdmits(db, questID, rec, now)) then
        return false                         -- 常驻周常：SetExpiry 开头就丢，没人收的答案不问
    end
    return true                              -- 跨域/没有/已过期/写坏了：照旧问（往多问那边走）
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

-- 唤醒定时器封顶。到期时刻一旦被写得很远（脏数据那类），
-- 不封顶就等于"矩阵开着也再不会自己更新"；封顶后每 6 小时至少重画一次，
-- 只在矩阵开着时发生，比"永远不刷新"便宜，也比玩家反复手点刷新省。
-- 【11-04 §二 3】到点只重画（不抓池），这个封顶现在压的是"多久重画一次"，
-- 不再压接口成本。
local MAX_WAKE_SECONDS = 6 * 3600
--- 按最早的任务到期时刻安排下一次刷新：世界任务到点才换，不用一直轮询。
--- 只在轮询中（矩阵开着）安排；平时一个定时器都没有，插件完全静默。
--- 到期时间已过或取不到 → 不自动安排（交给打开矩阵/事件/sync 命令触发）。
--- 关键：同一时刻只允许存在一个待触发的刷新定时器。每次快照都会走到这里，
--- 不取消旧的就会越排越多 —— 做任务时 QUEST_LOG_UPDATE 频繁触发快照，
--- 几十个定时器攒在同一时刻一起炸，每个都要重排并重画一遍（11-04 之前每个
--- 还跑一趟全量抓池），那就是"开着矩阵就定时卡一下"的元凶（用户实测）。
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
    local db = GQL.Config.DB()
    if not db then return end
    local now = select(1, Clock())
    local due = L.NextRefreshAt(db, now)
    if not due or due <= now then return end
    local delay = due - now
    if delay > MAX_WAKE_SECONDS then delay = MAX_WAKE_SECONDS end
    refreshTimer = C_Timer.NewTimer(math.max(5, delay), function()
        refreshTimer = nil
        if not polling then return end
        -- 【11-04 定稿 §二 3：到点只重画】结束时刻在账本里，到没到一比就知道；
        -- 那一行该撤、颜色该变，全是账本算式。原来这里跑一趟 SnapshotNow =
        -- 几百张图逐个问一遍（用户点名的那笔白烧）。
        -- 重排下一颗照旧：ScheduleNextRefresh 第一句就取消旧的，同一时刻只留一个。
        ScheduleNextRefresh()
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
    end)
end

--- 强制客户端拉取世界任务数据 —— 等价于玩家按 M 打开世界地图再关掉。
--- 关注图整张返回空（Api.GetPool 的 meta.emptyMaps）大概率是"客户端这张图的任务
--- 还没拉到"，不是真没有；刷新/快照发现空图时主动开一下地图（背靠背关闭，不闪屏），
--- WorldMapFrame:OnShow 发起该大陆数据请求，数据回来后 POOL_UPDATE_EVENTS 触发下一轮快照，
--- 满足条件却还不在矩阵里的任务就自动进来了，不必让玩家自己按 M。
--- 冷却避免每张空图每 10 秒都开一次地图；地图已经开着就不必动。
local MAP_BUMP_COOLDOWN = 15
local lastMapBump = -1e9
function Sync.ForceMapDataLoad()
    local now = (GetTime and GetTime()) or 0
    if now - lastMapBump < MAP_BUMP_COOLDOWN then return end
    local wmf = GQL.Api and GQL.Api.Resolve and GQL.Api.Resolve("WorldMapFrame")
    if wmf and wmf.IsShown and pcall(wmf.IsShown, wmf) and wmf:IsShown() then return end
    if not ToggleWorldMap then return end
    -- 背靠背开+关：同一个 handler 内连续调用，渲染不会插进来，玩家看不到地图闪；
    -- 但 OnShow 已经把数据请求发出去了。
    local okOpen = pcall(ToggleWorldMap)
    if not okOpen then return end
    pcall(ToggleWorldMap)
    lastMapBump = now
    -- 数据可能慢一帧才到，补一次快照兜底（面板开着轮询在）。
    if polling then
        C_Timer.After(0.3, function()
            if not polling then return end
            pcall(Sync.SnapshotNow)
            if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
        end)
    end
end

--- 日志旁路（2026-11-03 定稿）：世界任务走近会自动挂进任务日志，但读日志的
--- SyncKeywordQuestsFromLog 在快照里、快照被轮询门挡着 —— 矩阵关着做完一个从没
--- 进过池的金币世界任务，账本连它的存在都不知道（实测：Mad 按M看到神秘身影、
--- 做完，换号矩阵里什么都没有，只剩交割流水和一格 done）。这里在
--- QUEST_ACCEPTED / QUEST_LOG_UPDATE 时做一次轻量增量：日志里新出现、判得出是
--- **世界任务**、且奖励读得出金币的任务，照日志路建账本记录（AddQuest 自带读
--- 名字/图号/奖励并写 observation 的 gold 旗，GoldVerdict 由它放行，交过门不再挡）。
--- 边界：判不出是不是世界任务（接口缺失/报错）就不收——宁缺毋滥，普通任务不许
--- 混进来；非金币的不收（矩阵本来就只列金币与点名）；关键词任务仍由快照里的日志
--- 扫描收（那边带 kwLogAdded 标记与续期逻辑，两边都建也只有一条记录）。
--- QUEST_LOG_UPDATE 做任务时连成一片，10 秒节流；一次旁路 = 一遍日志 GetInfo +
--- 每条新任务两三次接口，量级远小于池快照，不会重现"一卡一卡"。
--- 11-04 起这一趟还多干两件事：日志里挂着的世界任务只要账本还没有它的结束时刻，
--- 就当场把倒计时/金额问一遍，问出来的结束时刻写进这一条、答案进日志
--- （每条每次开机只被动问一次，一趟最多 12 条）——
--- 用户 11-04 的口径："不能指定这个任务 id 吧，这个我都做完了，应该任何世界任务的监听
--- 都打日志"，以及同日第 1 条"接任务/交割那一刻问出来的结束时刻，当场写进这条记录"。
--- 见下面 Sync.CaptureQuestData。第二件：把现场问答去要过奖励数据的那几条补读一遍，
--- 服务器那份金额到了就当场落账（同日第 2 条"金额：那一刻向服务器要一次，数据回来就落账"），
--- 见下面 SettleAskedRewardGold —— 这一趟排在读日志之前，日志清空的那一刻也要落。
local LOG_BYPASS_COOLDOWN = 10
local lastLogBypass = -1e9
-- 被动问答的去重表（键 = questID）：一条任务每次开机只被动问一次。
-- 放在函数外是因为这一趟 10 秒就会跑一遍，不去重的话日志里全是同一批重复行。
local probedPassive = {}
local PROBE_PER_PASS = 12
-- 【T2-a】金额问不出 ⇒ 向服务器要一次；按 11-04 定稿 §二 5 改成按号去重
-- （同一个号每次开机每条只发一次，换号登录照旧发），一条任务不再被一个号全局了结。
-- 会话级、放在函数外：现场问答那一趟写它、日志旁路那一趟读它，两边都不是 db 里的字段
-- （跨开机没有意义：数据的去重与到账都以这一次登录为界）。
-- 值：按号子表 `questID -> { [charKey] = tries }`；0 = 已发请求、还没补读，
-- 1..GOLD_READ_MAX_TRIES-1 = 补读失败的趟数，-1 = **这一号**已了结（钱到了 / 到顶 /
-- 标了不做），从此这一号一次接口都不许多烧。别的号那一笔留在表里，换号登录照旧补读。
local askedRewardData = {}
-- 补读的封顶（计划 §四 T2-b）：6 次 × 10 秒节流 ≈ 一次开机内每条最多 6 次只读金额调用。
local GOLD_READ_MAX_TRIES = 6
local GOLD_READ_PER_PASS = 12
--- 金额问不出的那几条，就近记着（只在这台客户端这次开机期间有效，不落档 —— 跨开机
--- 没有意义，取证说的是"刚刚那一条"）。
--- 为什么要这张表：下一轮的取证命令 `/gql probe fetchdata` 得知道"该拿哪一条做试验"，
--- 而玩家要能说出任务名或任务号，前提就是先按 M 打开世界地图 —— 取证要验证的恰恰是
--- "不按 M 行不行"，不许把按 M 当成取证的前提（用户 11-04）。
--- 只记问不出的（答得出的占位会让探针去试一条本来就有钱的任务），封顶 5 条、最旧先丢。
local recentMoneyMiss = {}
local RECENT_MISS_MAX = 5
local function noteMoneyMiss(questID, now, hasData)
    for i = #recentMoneyMiss, 1, -1 do
        if recentMoneyMiss[i].questID == questID then table.remove(recentMoneyMiss, i) end
    end
    recentMoneyMiss[#recentMoneyMiss + 1] = { questID = questID, at = now, hasData = hasData }
    while #recentMoneyMiss > RECENT_MISS_MAX do table.remove(recentMoneyMiss, 1) end
end

function Sync.RecentMoneyMisses() return recentMoneyMiss end
function Sync.LastMoneyMiss() return recentMoneyMiss[#recentMoneyMiss] end

--- 这一号在**这一条实例**上标了"本轮不做"没有（矩阵格子右键，见 Matrix.CellClick）。
--- 必须走 L.State：raw manual[].state 从不自动作废，作废只发生在 L.State:1732 读时比 m.k；
--- 裸读会把上一轮的 × 当本轮有效，永久掐掉新实例的补读。L.State 还会把
--- "标了不做又真做了"判成 done，与界面口径一致。纯账本，零接口。
local function SkipThisInstance(db, questID, charKey, now)
    local rec = db and db.quests and db.quests[questID]
    local curKey = L.InstanceKey(db, questID, rec, now)
    if not curKey then return false end
    return L.State(db, charKey, questID, nil, now, curKey) == "skip"
end

--- 【T2-b 落账】补读：只走上面那张表里"已经去要过数据"的几条，钱到了就落进这一条自己的记录。
--- 为什么不挂在"奖励数据回包"的事件上：这台客户端**没实测过**那个事件名，
--- 而按名字猜着注册炸过登录同步（WORLD_QUEST_LOG_UPDATE 正炸在 OnWorldEnter 第一行，
--- 见下面 TryRegister 那段注释）。做任务时 QUEST_LOG_UPDATE 本来就成串触发，
--- 日志旁路又自带 10 秒节流 ⇒ 借它跑：零新增事件监听、零新增定时器、零扫图。
--- 代价照实：每条待补读任务每趟 1 次金额只读调用，一趟最多 GOLD_READ_PER_PASS 条、
--- 每条最多 GOLD_READ_MAX_TRIES 趟，到顶就标成已了结放手，不无限转。
--- 落账写法照清单遍历那三句的**顺序与同一把键**（见 SnapshotNow 池循环那一段的原注释）：
--- 这一条自己的结束时刻由上面 Sync.CaptureQuestData 那一趟落进 L.SetExpiry，这里不重复实现，
--- 只排在它之后算键 —— 排在之前算出来的是"上一条实例/本 CD 尾巴"那一档，正是本轮要消灭的形状。
local function SettleAskedRewardGold(db, now)
    local changed, counted = 0, 0
    local charKey = Sync.CurrentCharKey()
    for questID, byChar in pairs(askedRewardData) do
        local tries = byChar[charKey]
        if tries ~= nil and tries >= 0 then      -- -1 = 这一号已了结，不再问
            if SkipThisInstance(db, questID, charKey, now) then
                -- 【11-04 定稿 §二 5】玩家明说这一号本轮不做它 ⇒ 只把这一号了结。
                -- 别的号那一笔留在表里，换号登录照旧补读。
                -- 次数封顶保留：挂机期间每次日志更新白问一条永远问不出的就是白烧接口调用。
                byChar[charKey] = -1
                GQL.Config.Log(("补读停：这一号标了不做 quest=%s 角色=%s")
                    :format(tostring(questID), tostring(charKey)))
            elseif counted >= GOLD_READ_PER_PASS then
                break
            else
                counted = counted + 1
                local okM, m = pcall(GQL.Api.QuestRewardMoney, questID)
                if okM and type(m) == "number" and m > 0 then
                    -- 落账走 L.AddQuest 这一个记录写点（不在这儿再拼一份观察/签名/lastMoney 的写法）：
                    -- 它自己读金额、按金额分观察桶，把刚问出来的这份直接递给它的 knownMoney ——
                    -- 两次读之间客户端换了答案的话，落账的数和记账的数会落进两个签名。
                    GQL.Ledger.AddQuest(db, questID, now, nil, m)
                    local rec = db.quests[questID]
                    if rec then
                        local curKey = L.InstanceKey(db, questID, rec, now)
                        -- 观察桶先烙上这一条实例自己的编号，再走 NoteGoldThisInstance：
                        -- 它对"编号对不上（含没编号）的那份观察"会把金额作废（那是上一条实例的账），
                        -- 刚建出来的观察还没有编号 ⇒ 排在它之后写就等于自己把刚读到的正数清成 nil。
                        local obs = L.GetObs(db, questID)
                        if obs then obs.key = curKey end
                        L.NoteGoldThisInstance(db, questID, m, now, curKey)
                        L.SetRewardRead(db, charKey, questID, m, now, curKey)
                        changed = changed + 1
                        GQL.Config.Log(("补读到金额 quest=%s 钱=%s 编号=%s（矩阵没开过也进账了）")
                            :format(tostring(questID), tostring(m), tostring(curKey or "算不出")))
                    end
                    byChar[charKey] = -1
                elseif tries + 1 >= GOLD_READ_MAX_TRIES then
                    byChar[charKey] = -1
                    GQL.Config.Log(("补读 %d 次仍问不出金额 quest=%s（服务器那份数据这一趟没到，不猜）")
                        :format(GOLD_READ_MAX_TRIES, tostring(questID)))
                else
                    byChar[charKey] = tries + 1
                end
            end
        end
    end
    return changed
end

function Sync.LogBypass()
    local tick = (GetTime and GetTime()) or 0
    if tick - lastLogBypass < LOG_BYPASS_COOLDOWN then return end
    lastLogBypass = tick
    local db = GQL.Config.DB()
    if not db then return end
    db.quests = db.quests or {}
    local now = select(1, Clock())
    -- 补读排在最前面：它只认上面那张表，与日志里此刻挂着什么无关（任务日志空了也要落账 ——
    -- 做完的那一下日志就清了，排在后面就正好漏掉用户点名的那个形状）。
    if SettleAskedRewardGold(db, now) > 0 then
        -- 矩阵关着接的任务，钱到了行就该自己出现（用户 11-04："接到任务后为什么没拿到正确的
        -- 信息然后进矩阵，这个要处理好"）。面板没开时 Matrix.Refresh 自己早退，不重排任何绘制。
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
    end
    local okCall, list = pcall(GQL.Api.AcceptedQuests)
    if not okCall or type(list) ~= "table" then return end
    local n, nKw, probedThisPass = 0, 0, 0
    for i = 1, #list do
        local q = list[i]
        local questID = q and q.questID
        if questID then
            local rec = db.quests[questID]
            -- 【11-04 取证，用户那句"你不能指定这个任务 id 吧…应该任何世界任务的监听都打日志"】
            -- 这一问不绑某一条任务号：日志里挂着的世界任务，只要账本还没有它的结束时刻
            -- （或者压根还没进账本），就问一遍、答案进日志，问出来的结束时刻写进这一条。
            -- 92576 那种"已经做完了、接单那一下再复现不出来"的情况靠这条路一样能拿到现场答案。
            -- 三道闸防刷屏：先查账本（有结束时刻的一条都不问）；每条每次开机只被动问一次；
            -- 一趟最多 12 条。
            local needProbe = not (rec and tonumber(rec.roundEnd))
                and not probedPassive[questID] and probedThisPass < PROBE_PER_PASS
            local wq = (needProbe or not rec)
                and GQL.Api.IsWorldQuest(questID) or nil
            if not rec and wq == true then
                local okMoney, money = pcall(GQL.Api.QuestRewardMoney, questID)
                if okMoney and type(money) == "number" and money > 0 then
                    -- 不传 addedBy：那一份 "questlog" 标签的语义是"**关键词命中**、从任务日志
                    -- 收的"（L.AddQuest 因此烙 kwLogAdded，而 L.SetExpiry 对带这个位的记录早退）。
                    -- 这一趟的准入是"客户端答得出是世界任务 + 当场读到正数金币"，跟关键词一个字
                    -- 无关 ⇒ 烙上它等于让这条金币世界任务永久拿不到自己的真结束时刻。
                    GQL.Ledger.AddQuest(db, questID, now)
                    n = n + 1
                end
            end
            -- 【11-07 根因修法：关键词任务的收录不许只住在快照里】
            -- 同一份事实（任务日志里挂着这条）以前有两个读取者：认关键词的
            -- SyncKeywordQuestsFromLog 只在 SnapshotNow 里跑，而事件路径上这个 LogBypass
            -- 只认"世界任务 + 当场读到正数金币"。快照被轮询门 + 去抖挡住时，接任务那一刻
            -- 的事实没有任何一个读取者记录 ⇒ 账本里连行都没有，交割又被 L.QuestKnown 挡掉。
            -- 修法不是再加一个读取者，而是让这唯一一个事件读取者也用
            -- SyncKeywordQuestsFromLog 的同一个判据（日志行 q.name + Config.KeywordMatch）、
            -- 同一个写入（L.AddQuest 的 "questlog"）。判据、写入各只有一处，不新增方法。
            if not db.quests[questID] and GQL.Config.KeywordMatch(q.name) then
                GQL.Ledger.AddQuest(db, questID, now, "questlog")
                nKw = nKw + 1
            end
            -- 【问答排在上面建记录之后】刚收进来的这一条本轮就该有自己的结束时刻：排在建记录之前
            -- 时 rec 还是 nil ⇒ 这一问一个字都不写，而下一次同一趟靠去重表不会再问 ⇒ 这条带着
            -- "账本有记录、没有真结束时刻"的形状过完整个开机（正是 92576 那个形状的一半）。
            if needProbe and wq == true then
                probedPassive[questID] = true
                probedThisPass = probedThisPass + 1
                Sync.CaptureQuestData(questID, "日志里挂着")
            end
        end
    end
    if n > 0 then
        GQL.Config.Log(("日志旁路：收 %d 条金币世界任务（矩阵关着也观测）"):format(n))
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
    end
    if nKw > 0 then
        GQL.Config.Log(("日志旁路：收 %d 条关键词任务（接任务当场，不等快照）"):format(nKw))
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
    end
end

--- 【11-04 取证 → 收下】接任务 / 交割的那一刻当场把这条的"还剩多久结束""给多少金币"
--- 问一遍。用户 11-04 的口径："能不能取到，你在事件监听的地方打日志就行了"
--- —— 先拿到"那一刻客户端到底答不答"的现场答案，再决定怎么改判据，不许靠猜。
--- 现场答案回来了（同日实测 95794 接任务那一刻答 69110 秒、交割答 69031 秒，
--- 金额仍问不出），于是用户 11-04 定的第 1 条落地：**问出来的结束时刻当场写进这一条**。
--- 为什么要在这一处写：这两个数在插件里原来**只有一个写点** —— 世界任务池的逐条遍历
--- （SnapshotNow 里 `for i = 1, #pool`），而那次遍历只在矩阵开着或登录那一次才跑。
--- 真档 92576「征服多曼纳尔」（蓝帆账号 11-04）：10:05:32 进账本（金额还是空）→
--- 10:10:11 交割 → 10:10:16 下板（池 26→25），中间一次遍历都没发生，金额从头到尾没读到；
--- 10:12:58 那次读到金额 11181872 铜、倒计时仍没读到 ⇒ 交割烙下的编号只能落在"本周尾巴"
--- 那一档，10:24:33 真实结束时刻（11-07 22:00:41）进来后前面的凭据全对不上号 ——
--- 玩家看到的"前两个号勾没了、第三个号有勾"就是这个形状。
--- 金额这一处**不收进账**，但要去要数据（用户 11-04 第 2 条"金额：那一刻向服务器要一次，
--- 数据回来就落账"）：问不出就向服务器要一次（每次开机每条只一次，见 askedRewardData），
--- 落账由补读那一趟负责（SettleAskedRewardGold）—— 这一处只收结束时刻 + 把请求发出去。
--- 范围（用户 11-04："你不能指定这个任务 id 吧，这个我都做完了，应该任何世界任务的监听
--- 都打日志"）：**事件带来哪条就问哪条**，代码里不写死任何 questID；只有客户端一口咬定
--- "这不是世界任务"（答 false）且账本也没这条的才跳过 —— 答"判不了"(nil) 的必须留行，
--- 那正是"接单那一刻客户端什么都没说"的证据，跳掉就等于把案发现场擦了。
--- 代价：一条任务 5~6 次只读接口（金额问不出时再多一次预加载**请求**，每次开机每条只一次），
--- 只在玩家自己接/交、或日志事件带到一条还没有结束时刻的世界任务时发生
--- （后者每次开机每条只问一次）。零新增定时器、零新增扫描。
function Sync.CaptureQuestData(questID, reason)
    questID = tonumber(questID)
    if not questID then return end
    local db = GQL.Config.DB()
    if not db then return end
    local rec = db.quests and db.quests[questID]
    local wq = GQL.Api.IsWorldQuest and GQL.Api.IsWorldQuest(questID)
    if wq == false and not rec then return end
    local hadEnd = rec and rec.roundEnd
    local now = Clock()
    -- 【11-04 定稿 §二 1】三处读取点共用同一道门：结束时刻还活着就不再向客户端重问。
    -- 没问时 secs 保持 nil，下面 SetExpiry 自然不写，现场取证日志照旧打 倒计时=nil。
    local secs, timeErr = nil, nil
    if ShouldAskExpiry(db, questID, rec, now) then
        local okT, v = pcall(GQL.Api.QuestTimeLeft, questID)
        if okT then secs = v else timeErr = tostring(v) end
    end
    -- 倒计时答得出就收下（口径与清单遍历那一例一字不差）。三道边界：
    -- · rec 为 nil（账本压根没这条）不建记录 —— 建记录是日志旁路那一趟的事，它自己带金币判据。
    -- · 不在这里加"剩余太短 / 与本周重置重合就不写"那道门（SyncKeywordQuestsFromLog 里的
    --   那道是给关键词从日志收进来的常驻行准备的）：95794 实测答 69110 秒，真值就是"这一周
    --   结束的那一刻"，搬过来等于把每一条 24 小时内到期的世界任务的真实结束时刻一律拒收。
    --   常驻周常那一族的保护由 L.SetExpiry 自己开头那三句早退负责。
    -- · 答不出（nil）一个字都不写，日志照打（写着 倒计时=nil）。
    if rec and type(secs) == "number" and secs > 0 then
        L.SetExpiry(rec, now, secs, L.KeywordLogAdmits(db, questID, rec, now))
    end
    -- 编号排在上面那一笔落账**之后**算（与清单遍历同一个顺序）：先收下结束时刻再算键，
    -- 报出来的才是这一条实例自己那一档；排在之前就是刚要消灭的那个尾巴档。
    local curKey = L.InstanceKey(db, questID, rec, now)
    local money, moneyErr = nil, nil
    local okM, m = pcall(GQL.Api.QuestRewardMoney, questID)
    if okM then money = m else moneyErr = tostring(m) end
    local hasData = GQL.Api.HasRewardData and GQL.Api.HasRewardData(questID)
    -- 【T2-a】金额问不出（含客户端对没预加载的任务回 0、被 Api.QuestRewardMoney 判成问不出）
    -- ⇒ 向服务器要一次。去重按 questID + 当前号、会话级（11-04 §二 5：换号登录照旧发）。
    -- HaveQuestRewardData 答"已经在本地"的那一条一次都不发（Api.PreloadRewardData 内部先问它，
    -- 这里不重复发），也**不进**补读表 —— 那条客户端已经答过"这一档不给金币"，
    -- 再去问六次只是白烧接口调用。日志末尾如实写这一件，玩家看得出插件没在装死。
    -- 【11-04 §二 5】这一号在本条实例上标了"不做"就整条不去要：这句取证放在外层，
    -- 挂进里面（或给外层条件加 not skip）会让日志连"为什么没要"都不写。
    local charKey = Sync.CurrentCharKey()
    local byChar = askedRewardData[questID]
    local askedText = ""
    if SkipThisInstance(db, questID, charKey, now) then
        askedText = "（这一号标了不做，没去要这份数据）"
    elseif money == nil and GQL.Api.PreloadRewardData then
        if byChar and byChar[charKey] then
            -- Lua 里 0 是真值，子表里有这一号就等于"这次开机这一号已经问过服务器了"
            -- （-1 也只是这一号已了结；别的号问过不算这一号问过）。
            askedText = "（早就问过服务器了）"
        elseif hasData == true then
            -- 客户端自己答"这份数据就在本地"却读不出金额 = 这一档它明说不给金币
            -- （11-04 实测：95253 在狩猎模式下 奖励数据=true 金额=nil，同一任务号在正常模式下
            --  读到 10137288）。再去要一次、再补读六次都是白烧接口调用，所以**不进**补读表。
            -- "那到底该不该给它记 0"是金币门槛那一侧的口径（清单遍历那边早就在写 0，
            -- 见 Api.RowsToPool 里 PreloadRewardData 那一句），这一处不跟着判。
            askedText = "（奖励数据本来就在本地，这一档客户端答的是不给金币）"
        elseif not (GQL.Api.Callable and GQL.Api.Callable(
                GQL.Api.Resolve("C_TaskQuest.RequestPreloadRewardData"))) then
            -- 本机压根没有预加载入口 ⇒ 这句"已经去要了"就是谎话。照实说，玩家才知道
            -- 这条路在这台客户端上不通，金额只能等开一次世界地图。
            askedText = "（本机没有向服务器要奖励数据的入口，只能等开一次世界地图）"
        else
            GQL.Api.PreloadRewardData(questID)
            byChar = byChar or {}
            byChar[charKey] = 0
            askedRewardData[questID] = byChar
            askedText = "（已向服务器要这份数据）"
        end
    end
    GQL.Config.Log(("%s现场问答 quest=%s 是世界任务=%s 倒计时=%s%s 金额=%s%s%s 奖励数据=%s "
        .. "账本已有结束时刻=%s 此刻按账本算的编号=%s 世界地图=%s")
        :format(tostring(reason or "事件"), tostring(questID), tostring(wq),
            tostring(secs), timeErr and ("（问出错：" .. timeErr .. "）") or "",
            tostring(money), moneyErr and ("（问出错：" .. moneyErr .. "）") or "", askedText,
            tostring(hasData),
            tostring(hadEnd or "无"),
            tostring(curKey or "算不出"),
            WorldMapOpenText()))
    -- 金额问不出的记一笔，给取证命令挑"该问哪一条"用（见上面 recentMoneyMiss 那段）。
    -- `wq ~= false`：客户端一口咬定"这不是世界任务"的不要（那张表只服务于世界任务取证），
    -- 判不了(nil)的照收 —— 那正是"这一刻客户端什么都没说"的那一条。
    if money == nil and wq ~= false then noteMoneyMiss(questID, now, hasData) end
    -- 把这把键交给调用方：交割那一路紧接着要用它烙流水（同一趟里算两遍，跨秒边界上
    -- 能算出两档编号 —— 日志里印的是一档、烙进流水的是另一档，正是本轮要消灭的形状）。
    return curKey
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
    if #GQL.Config.GetKeywords() == 0 then
        clearAllFlags()
        return 0
    end
    local okCall, list = pcall(GQL.Api.AcceptedQuests)
    if not okCall or type(list) ~= "table" then return 0 end
    -- 收录层按 questID 建账：db.quests 的键就是客户端报上来的 questID，代码不自己编 ID。
    -- 关键词命中就该收：建记录、烙 kwLogKey（AddQuest 的 questlog 来源自带凭据）。
    -- 「账本里已有一条同名带图记录」不构成「日志这条不该收」的理由：那只是名字撞了，
    -- 可能是上一轮换下来的旧壳、也可能是玩家指定过图的旧档，跟这次日志里这条是不是
    -- 同一条任务没有凭据关系 —— 这里不假定客户端会为同一任务报两个 ID。以前用
    -- hasOtherCopy 拦"新建记录"（怕矩阵底部多一行 ?），会把关键词命中的这条一起挡掉。
    -- "会不会列成两行"是**显示层**的事：L.MatrixRows 按名字只留一条（证据强者胜，带图的
    -- 留下、无图的藏掉），与收不收记录无关；防重复不该拿显示规则去否决数据收录。
    -- hasOtherCopy 现在只剩一个用处：拦"写地图"（见下面 mapUnproven 那道）—— 同名副本的日志图
    -- 会每轮自我取消（"加固符文石这个任务没了"），不许它拿这张图去覆盖/顶掉客户端与板给的图。
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
    local legacySrc = (GQL.Ledger and GQL.Ledger.LegacyMapSource
        and GQL.Ledger.LegacyMapSource()) or "\0"
    local suspectSrc = (GQL.Ledger and GQL.Ledger.SuspectMapSource
        and GQL.Ledger.SuspectMapSource()) or "\0"
    local function mapUnproven(rec)
        local src = rec.mapSrc
        return src == nil or src == "" or src == legacySrc or src == suspectSrc
    end
    for i = 1, #list do
        local q = list[i]
        local existing = db.quests[q.questID]
        -- 玩家屏蔽过的（rec.ignored）不再收：矩阵按 rec.ignored 过滤，而这里重新收会把 kwLogAdded
        -- 写回去、等于当场把"别再显示"的意图抹掉（12.1.5 实测反馈"删不掉"）。
        -- 这一跳过是**永久**的：屏蔽只由玩家在屏蔽管理窗口里点回来才撤（Ledger.AddQuest）
        -- 关键词命中就收，不按"账本里有没有同名带图记录"否决：名字相同不是同一条任务的
        -- 凭据。重复行由矩阵按名字去重，不在这里管。
        if not inPool[q.questID]
            and not (existing and existing.ignored)
            and GQL.Config.KeywordMatch(q.name) then
            keep[q.questID] = true
            local rec = existing
            if not rec then
                GQL.Ledger.AddQuest(db, q.questID, now, "questlog")
                rec = db.quests[q.questID]
                n = n + 1
            else
                rec.lastSeenAt = now
                -- 还接在日志里就每轮续 lastSeenAt，否则任务明明还接着、行却按上一轮的到期时刻消失。
                -- 【2026-11-01 修正：特别任务要存自己的结束时间，不要依赖周 CD】
                -- 还挂在任务日志里时，客户端能答出它**自己的**剩余时间（接到时就该读到了，
                -- 不是板面那几小时轮换寿命）。用这条真实剩余把 roundEnd 烙上（每轮还挂着就重算，
                -- 所以是真实剩余、不会越推越远；也顺手覆盖掉上一实例留下的过期 roundEnd）：
                -- 结束时刻变成这条实例自己的到期日，到期门走 ①档在点到的那一刻把它去掉，不再被
                -- 尾巴（cycleAnchor + 604800）拖着。有的特别任务周中周日开启、持续 7 天，跨两个
                -- 周 CD，尾巴永远指向下次周重置 ⇒ 行赖在矩阵（92139 暗影与利爪）。
                -- 只在"不在世界任务板上"（not inPool）的日志任务上写：上了板的周常走池循环那一路
                -- 的倒计时，不在这烙。守卫：C_TaskQuest 对"周常/轮换"任务答的是板上那几小时轮换
                -- 寿命甚至干脆等于下次周重置（12.1.5 实测 89507 = 周重置秒数），那不是这条任务自己
                -- 的剩余；用两道闸门挡掉：(1) 太短（< 1 天）的视为板面轮换寿命，不写 roundEnd；
                -- (2) 读出来和本周重置倒计时基本重合的视为回声，不写。
                -- 注意不按 regKey 排除：注册周常里也有暗影与利爪这种"有自己的 7 天寿命、不该按 CD
                -- 尾巴算"的特例 —— 它接在日志里时 QuestTimeLeft 答真实 7 天（> 1 天、不是重置回声）
                -- 会正常烙上自己的 roundEnd；而加固符文石那种板面轮换寿命 < 1 天，被闸门(1)挡掉，
                -- 仍走 CD 尾巴，不会当天下午整行没。
                -- 两道门分工：ExpiryIsLive 管"这一轮要不要问"（结束时刻还活着就不重问），
                -- 下面 isRotation 那道管"问出来的数配不配写"（< 1 天 / 周重置回声是板面轮换寿命）。
                -- 这里**不用** ShouldAskExpiry 的 resident 那一半：本段明确"不按 regKey 排除"
                -- （暗影与利爪那种注册周常也有自己 7 天寿命，要照写）；也不走 L.SetExpiry，
                -- 否则会把 resident 早退一并搬过来，改掉既有口径。
                if not inPool[q.questID] and not ExpiryIsLive(rec, now) then
                    local okT, secs = pcall(GQL.Api.QuestTimeLeft, q.questID)
                    if okT and type(secs) == "number" and secs > 0 then
                        local wk = (GQL.Api.WeeklyResetCountdown and GQL.Api.WeeklyResetCountdown())
                            or nil
                        local isRotation = (secs < 86400)
                            or (type(wk) == "number" and math.abs(secs - wk) <= 60)
                        if not isRotation then
                            rec.roundEnd = now + secs
                        end
                    end
                end
                -- 布尔位之外还要当场烙一份**这一条实例**的编号（Ledger.NoteKeywordLogThisInstance，
                -- 计划 §三 W1）：这份"还挂在日志里"是 live 事实、下面那道撤点会撤它，换一个没接
                -- 这条的号登录整行就对全账号消失（梦魇任务 94446，用户 10-29 点名）。这一句必须排
                -- 在 roundEnd 之后：凭据编号问的是 InstanceEnd，先烙上真 end 它才印对那一档的键。
                rec.kwLogAdded = true
                L.NoteKeywordLogThisInstance(db, q.questID, rec, now)
            end
            if rec and not rec.name then rec.name = q.name end
            -- 日志行自己报的分区图号（C_QuestLog.GetQuestLogZoneMapID 答不答得出**还没实测**，
            -- 取不到就什么都不写）：特别任务/战役任务在全世界任务口径的三条来源上就是问不出
            -- 地图（12.1.5 实测 82157：逐条问的两条一条 MISSING 一条 nil、12 张跟踪图的清单
            -- 反查也没有它），这是唯一一条按"这条任务自己"答的凭据。已经有凭据的行不动它。
            -- 同名副本的任务（特别任务/加固符文石那类）日志给的图会每轮自我取消，只在这道拦：
            -- 有同名另一份 questID 在账本里就不写日志图，让它继续用客户端/板的图。
            if rec and q.mapID and mapUnproven(rec)
                and not hasOtherCopy(q.name, q.questID) then
                rec.mapID, rec.mapSrc = q.mapID, mapSrcLabel("logZone")
            end
            -- 在日志里 = 已接：状态记 progress，别让它看着像"没做"
            GQL.Ledger.SetView(db, charKey, q.questID, "progress", now)
        end
    end
    for questID, rec in pairs(db.quests) do
        -- 只撤布尔位这一份 live 事实（当前号日志里还挂着没），**绝不撤 kwLogKey**：
        -- 那份是这一条实例的凭据，"有号接过"这件事不会因为换个号登录就变成没发生过
        -- （梦魇任务 94446 的整行消失就是这一句撤过头撤出来的）。
        if rec.kwLogAdded and not keep[questID] then rec.kwLogAdded = nil end
    end
    return n
end

--- 缺地图的记录逐条补问一次所属地图（候选名单与"地图号在第几个返回值"都写在
--- Api.QuestMapID 里，/gql probe 的取证段按同一份名单逐个问给用户看）。
--- 为什么必须在同步里补：DiffPool 只给"这轮还在池里"的任务写地图，
--- 已接走/已离开池子的旧记录就永远停在 nil —— 而矩阵把 nil 全丢进底部"未知地图"分组，
--- 用户实测的"底部一堆没用的特别任务"就是这么堆出来的（其中不少其实是别的服务端数据
--- 补得出地图的世界任务）。Api 内部按会话缓存，问过不重复问。
--- 还缺的走反向索引兜底：用户诉求很明确——"压根不应该存在没有所属地图的任务"，
--- 而"追踪萨拉塔斯"这类已接任务在全世界任务口径的接口上就是问不出地图。
--- 只在仍缺的时候付这份代价（每张图一次调用），问不出的继续留 nil，不编。
local function HealMissingMaps(db)
    if not (GQL.Api and GQL.Api.QuestMapID) then return end
    -- 旧版留下的"有图号、没来源标签"（Ledger.ClearRetiredMaps 打的标记）也当缺地图重问一遍：
    -- 留着它是因为清掉会把整批行堆进"未知地图"（第 15 轮反馈②③），但不重问就等于
    -- 这份旧答案永远没人复核。问出新答案就覆盖，问不出原样保留。
    local legacy = (GQL.Ledger and GQL.Ledger.LegacyMapSource
        and GQL.Ledger.LegacyMapSource()) or "\0"
    -- 存疑标签（ClearRetiredMaps 给"板上出现过、图由已停用接口答的"行打的）同样要重问：
    -- 留着图号只是为了不让整批行塌进"未知地图"，不是认它当凭据
    local suspect = (GQL.Ledger and GQL.Ledger.SuspectMapSource
        and GQL.Ledger.SuspectMapSource()) or "\0"
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
            local mapID, src = GQL.Api.QuestMapID(questID)
            if mapID then
                rec.mapID = mapID
                rec.mapSrc = mapSrcLabel("healPrefix") .. tostring(src)
            else
                stillMissing = true
            end
        end
    end
    if not (stillMissing and GQL.Api.MapQuestIndex) then return end
    local mapIDs = (GQL.Config and GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs())
    local index, _, srcOf = mapIDs and GQL.Api.MapQuestIndex(mapIDs)
    if not index then return end
    for questID, rec in pairs(db.quests) do
        if rec and needAsk(rec)
            and not (type(rec.name) == "string" and namedBy[rec.name])
            and index[questID] then
            -- 来源按索引那份逐条记：板面反查与悬赏反查是两条不同的凭据，
            -- 混成一个标签就没法复盘"这张图是谁说的"
            rec.mapID = index[questID]
            rec.mapSrc = (srcOf and srcOf[questID]) or mapSrcLabel("boardReverse")
        end
    end
end

--- 家族认亲的第二条来源：母任务自己的"任务追踪"清单里带着子任务 ID
--- （用户 2026-10-23："从丰饶供品的任务追踪里面可以看到子任务的内容"、"从子任务反查不到"）。
--- 名字认亲只能捞到标题里带同一个词的，"新的会出现，可能在其他地图"下周换的名字万一不带那个词
--- 就整族失联；目标行里的 questID 是客户端答的，不靠猜也不用玩家输 ID。
--- 只往**已经是家族成员**的那几条上扩（内置表锚点 + 关键词注册的那批），
--- 普通金币任务不去问目标行：那会把每条战役任务的步骤全注册进族里。
local FAMILY_EXPAND_LIMIT = 12

local function ExpandFamily(entries, seen)
    local Api = GQL.Api
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
    local Api = GQL.Api
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
    -- 关键词补扫注册过的行也照这份重问：它不在内置表里，只有账本知道它存在。
    -- **这一整趟都不带 onBoard/seen**（2026-11-02 D6/D7）：L.EnsureRegistered 的续命只认真
    -- 事实，这趟按账本里的注册键名单回问名字/地图/状态**不等于本轮又见到了它** ——
    -- 注册键本身不是事实，名单不是续期单。以前正是这里的白续让②档"这一 CD 没见过 ⇒ 到期"
    -- 必须挂着一整串身份豁免才不杀好人；现在白续撤了，豁免也整句删了（L.InstanceExpired）。
    -- 单 ID 的常住周常（丰饶供品 89507）不在这里续：它压根不是世界任务，池/日志/板上都问不到，
    -- 这份名单答不出它的真事实；它的续命由 AbundantOfferings.lua 每轮单独回问客户端状态走
    -- seen（与 AqirInvasion.lua 用 pin 报在场同一条路，无"常驻"特判，按版本闸挂、13.0 自动不挂）。
    -- pin: 那一路连这趟重问都不进（用户 2026-11-02 定稿："和其他普通金币任务一样，
    -- 到期就要自动消失"）：它的续命只许由下面 Sync.RefreshEventPins 在 pin 真在场的那一轮
    -- 付（seen = true，同一趟快照，零新增接口调用），事件一散就断供。行若本轮真在池里，
    -- DiffPool 那一路自己会续。
    local known = (L.RegisteredIndex and L.RegisteredIndex(db)) or {}
    for i = 1, #known do
        local questID = tonumber(known[i].questID)
        local key = known[i].key
        if questID and not seen[questID] and not L.IsPinRegKey(key) then
            seen[questID] = true
            entries[#entries + 1] = { key = key, questID = questID }
        end
    end
    -- 母任务的目标行里还藏着这一族的子任务 ID：先认亲，再一起去问名字/地图/状态
    local kin = ExpandFamily(entries, seen)
    if kin > 0 then
        GQL.Config.Log(("任务追踪认亲 +%d 条（母任务的目标行里问到的同族 ID）"):format(kin))
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
        entries[i].mapSrc = mapID and (mapSrcLabel("healPrefix") .. tostring(mapSrc)) or nil
        entries[i].money = Api.QuestRewardMoney(questID)
        entries[i].state = select(1, Api.QuestState(questID))
        -- 这里**不问倒计时**：这一趟只重问 kw:/内置那两把键（pin: 的行在上面按前缀挑出去了），
        -- 它们是常驻周常，到期只认 CD 尾巴（Ledger.SetExpiry）。
        -- 上一版问出来的那条是它**板上实例**的轮换寿命（几小时），写成到期等于
        -- 当天下午就把这一行从矩阵里抹掉（12.1.5 实测："加固符文石这个任务没了"）。
    end
    if #entries == 0 then return 0 end
    -- 只在第一次建记录时报日志：这函数每轮快照都跑（矩阵开着时 10 秒一轮），
    -- 每轮报一条会把 /gql log 刷满，真正要看的"这条 ID 本机问不出名字"另有 probe
    local before = 0
    for i = 1, #entries do
        if not (db.quests and db.quests[entries[i].questID]) then before = before + 1 end
    end
    local n = L.EnsureRegistered(db, entries, charKey, now)
    if before > 0 then
        GQL.Config.Log(("注册周常新入账 %d/%d 条"):format(before, n))
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
local function ScanBoards(db, now)
    local member = {}
    for questID in pairs(db.quests or {}) do
        -- 认亲走 IsFamilyMember（= 内置表声明过的族：ID 在 ids 里，或名字命中 match 词）
        if L.IsFamilyMember(db, questID) then member[questID] = true end
    end
    if not next(member) then return 0 end
    local maps = (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
    local hit = 0
    for k = 1, #maps do
        -- 单图问不出就跳过这张：拿它报错把整轮快照炸掉不值（池子那趟还要继续）
        local okRows, rows = pcall(GQL.Api.MapQuestRows, maps[k])
        if okRows and type(rows) == "table" then
            for n = 1, #rows do
                local questID = tonumber(rows[n].questID)
                if questID and member[questID] then
                    local rec = db.quests[questID]
                    rec.onBoard = true
                    -- 地图以行自己报的为准：换图之后这一行要跟着搬到新图上去
                    if rows[n].mapID and rec.mapID ~= rows[n].mapID then
                        rec.mapID, rec.mapSrc = rows[n].mapID, mapSrcLabel("boardList")
                    end
                    -- 占行的那条是族本体，板上这条可能不是它：本体的地图跟着这轮的板面证据走
                    local preset = L.PresetFor(db, questID)
                    local anchorID = preset and L.IsPresetID(preset, questID)
                        and questID or (preset and preset.ids and tonumber(preset.ids[1]))
                    local anchor = anchorID and db.quests[anchorID]
                    if anchor then
                        -- 本轮真在跟踪图上扫到族成员 = 这一族本周的**真事实**，锚点据此续 lastSeenAt。
                        -- D6/D7 撤掉②档按身份的豁免后，锚点活命只能靠这条真事实：
                        -- 丰饶子任务（"丰饶：草药洞穴"）是任务清单条目（C_TaskQuest）不是世界任务，
                        -- 进不了池循环那条续命路，只能在按图扫板的这一趟续。不续就在换 CD 那一刻
                        -- 被②档"本 CD 没见过 ⇒ 到期"判死、整族那行再也回不来（用户实测
                        -- "看不到丰饶供品"）。锚点自己（89507）被扫到时也同样续（rec == anchor）。
                        if now then anchor.lastSeenAt = now end
                        if anchor ~= rec and rows[n].mapID
                            and anchor.mapID ~= rows[n].mapID then
                            anchor.mapID, anchor.mapSrc = rows[n].mapID, mapSrcLabel("boardList")
                        end
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
    local preset = GQL.Ledger and GQL.Ledger.PresetFor and GQL.Ledger.PresetFor(db, questID)
    if preset and type(preset.match) == "string" and preset.match ~= "" then
        return preset.match
    end
    local key = rec.regKey
    if type(key) == "string" then
        local pre = (GQL.Ledger and GQL.Ledger.KW_PREFIX) or "kw:"
        if key:sub(1, #pre) == pre then return key:sub(#pre + 1) end
    end
    if type(rec.name) == "string" and GQL.Config.KeywordHit then
        return GQL.Config.KeywordHit(rec.name)
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
            if i ~= j and GQL.Api.MapParentID(kept[j]) == kept[i] then
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
    if not (db and db.quests) or not (GQL.Api and GQL.Api.EventPins) then
        return 0, 0, false
    end
    local maps = (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
    local byName, answered = {}, false
    for i = 1, #maps do
        local pins = GQL.Api.EventPins(tonumber(maps[i]))
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
                    if pins[j].widgetSet then c.widgetSet = pins[j].widgetSet end
                    if pins[j].secs ~= nil then
                        c.secs = c.secs or {}
                        c.secs[pins[j].mapID] = pins[j].secs
                    end
                end
            end
        end
    end
    if not answered then return 0, 0, false end

    local chosen = {}
    for name, cands in pairs(byName) do chosen[name] = PickPinMap(cands) end

    -- 事件扩展点（AqirInvasion.lua 那种"pin 上没有任务号、行照样要做"的版本闸挂法）：
    -- 生产只递这一轮真问到的证据（pin 名 → byName 里的归属候选/widgetSet、chosen 里挑出的图），
    -- 写什么行、状态怎么判全由挂上来的模块自己担；扩展报错不许炸掉这一趟归属刷新。
    -- answered=false 在上面已经 return，扩展拿到的永远是"客户端这轮真答了"的证据。
    local extras = GQL.EventPinExtras
    if type(extras) == "table" then
        for i = 1, #extras do
            if type(extras[i]) == "function" then
                local okEx, err = pcall(extras[i], db, now, byName, chosen)
                if not okEx then
                    GQL.Config.Log(("事件扩展 #%d 报错：%s"):format(i, tostring(err)))
                end
            end
        end
    end

    -- 事件 pin 挂着真任务号时，关键词才有的行（用户 2026-10-24 第二十二轮："亚基入侵了，
    -- 关键字我已经加了，矩阵里还看不到"）：那种事件压根不在世界任务板上、也不在任务日志里，
    -- 关键词补扫（板面清单 + 日志）两条路都碰不到它。这里只认客户端自己写在 pin 上的
    -- questID —— 答不出就一行都不注册，绝不拿事件名去猜一个 ID（猜出来的行会把别的任务的
    -- 完成状态冒充成它的）。注册成 kw:<关键词> 与补扫同源：玩家删关键词就一起退掉。
    local entries = {}
    -- 门槛一次读（与 MatrixRows 同一条 L.MatrixMoneyFloor，不许抄数字）：
    -- 金币门收 pin 要拿它判。
    local floorMoney = GQL.Ledger and GQL.Ledger.MatrixMoneyFloor
        and GQL.Ledger.MatrixMoneyFloor() or nil
    for name, cands in pairs(byName) do
        local mapID = chosen[name]
        if mapID then
            local kw = GQL.Config.KeywordHit and GQL.Config.KeywordHit(name) or nil
            for qid in pairs(cands.qids) do
                local preset = GQL.Ledger.PresetByID and GQL.Ledger.PresetByID(qid)
                local title = GQL.Api.QuestTitle and GQL.Api.QuestTitle(qid) or name
                if not preset and not (GQL.Ledger.PresetByName
                    and GQL.Ledger.PresetByName(title)) then
                    -- 金额在这里问：要不要收这条 pin 由它自己答出的金币定（下面那道门），
                    -- kw 那一路不在乎它，多问的只是同一批带任务号的 pin（个位数）。
                    local money = GQL.Api.QuestRewardMoney
                        and GQL.Api.QuestRewardMoney(qid) or nil
                    -- 收行两类依据（用户 2026-10-29 定稿的准入门在这条收路上的对应）：
                    -- ① 玩家点名（关键词命中，原样）；② 奖励金币过门槛 —— 2026-10-31 新增。
                    -- 用户实测"暗影汇聚之地"（战团周常 pin，奖励 956 金）怎么点刷新都不出现：
                    -- 它不在世界任务池（按图源逐行过 IsWorldQuest 时被滤掉）、日志里也没有，
                    -- 准入②的金币门永远没机会看到它 —— 卡的是收路不是准入门。pin 上
                    -- 客户端自己写了任务号（绝不拿事件名猜 ID，同上面那句口径），
                    -- 奖励金币读得到且够门槛，与池行是同一份证据强度，照收。
                    -- 注册键与 kw: 分开（L.PIN_PREFIX）：删关键词不许退掉金币门收的行。
                    -- 但这把键**不换来任何驻板待遇**（用户 2026-11-02 定稿："暗影汇聚之地
                    -- 是普通的世界金币任务…和其他普通金币任务一样。到期就要自动消失"）：
                    -- 上了板的按自己那条实例的板面结束时刻到期（①档：L.SetExpiry 对 pin: 不早退，
                    -- T6b），压根不上板的走②档"本周没被看见过 ⇒ 到期"（L.InstanceExpired 的豁免
                    -- 按前缀给）；散场后这一趟不再有它的 entry ⇒ 没人续 lastSeenAt，
                    -- 事件名周内轮换也只留最新那一把键。
                    if kw or (floorMoney and type(money) == "number"
                        and money >= floorMoney) then
                        -- seen = true：这块板的 pin 这一轮真报上来了，是关于它的真事实，
                        -- EnsureRegistered 据此续 lastSeenAt（散场那一轮起 entry 就没有它，断供）。
                        entries[#entries + 1] = {
                            key = kw and ((GQL.Ledger.KW_PREFIX or "kw:") .. tostring(kw))
                                or ((GQL.Ledger.PIN_PREFIX or "pin:") .. tostring(name)),
                            questID = qid, name = title, mapID = mapID,
                            mapSrc = mapSrcLabel("pin"),
                            money = money,
                            seen = true,
                        }
                    end
                end
            end
        end
    end
    if #entries > 0 and GQL.Ledger.EnsureRegistered then
        local charKey = Sync.CurrentCharKey()
        for i = 1, #entries do
            local q = entries[i].questID
            -- 金币门收进来的那批在门上已经问过金额，别再问一遍；kw 那批照旧补问。
            if entries[i].money == nil then
                entries[i].money = GQL.Api.QuestRewardMoney
                    and GQL.Api.QuestRewardMoney(q) or nil
            end
            entries[i].state = GQL.Api.QuestState
                and select(1, GQL.Api.QuestState(q)) or nil
        end
        local n = GQL.Ledger.EnsureRegistered(db, entries, charKey, now)
        if n > 0 then
            local names = {}
            for i = 1, #entries do names[i] = tostring(entries[i].name) end
            GQL.Config.Log(("地图事件里注册了 %d 条周常：%s（这几件不在世界任务板上，"
                .. "是任务号自己挂在地图 pin 上问出来的；奖励金币够门槛的没有关键词也照收）")
                :format(n, table.concat(names, "、")))
        end
    end

    local written, cleared = 0, 0
    for questID, rec in pairs(db.quests) do
        -- 这一条实例自己的编号：归属要烙在它上面（下面那段），顺手传给凭据判据免得重算
        local curKey = L.InstanceKey(db, questID, rec, now)
        -- 事件 pin 的归属跟着"这行是不是常驻周常"走：布尔位是当前号日志里挂着没（换号就撤），
        -- 这一条实例被日志证明存在过的那份凭据才是整周驻板的那一份 —— 两边都认，
        -- 分组才不会在一个没接这条的号登录后退回原分组（梦魇任务 94446 那一单，§三 R6）。
        local word = (rec.regKey or rec.kwLogAdded
            or L.KeywordLogAdmits(db, questID, rec, now, curKey)) and Sync.PinWordFor(db, questID) or nil
        local best
        if word then
            for name, mapID in pairs(chosen) do
                if mapID and name:find(word, 1, true)
                    and (not best or name < best.name) then
                    local cn = byName[name]
                    local secs = cn and cn.secs and cn.secs[mapID] or nil
                    best = { name = name, mapID = mapID, secs = secs }
                end
            end
        end
        -- 【归属一轮定一次，本轮不许再变】（2026-11-02 用户现场："亚基入侵一会儿属于周常
        -- （这时候地图上没 pin），一会儿属于祖阿曼，变来变去体验很不好"）：这一趟只扫跟踪
        -- 清单里的图，而事件 pin 会轮换到没跟踪的图、做完之后图标还会先消失一阵（见
        -- AqirInvasion 文件头记的实测延迟），旧写法把"这一轮没扫到"当成"归属没了"当场抹掉
        -- ⇒ 分组头掉进周常组，下一轮扫到又变回来，矩阵开着就是 10 秒一次的来回跳。
        -- 用户给的这一族的事实是**一周一个归属**：「第一次获取到 pin 的时候就知道他所属地图，
        -- 本轮次就不会再变更地图了，下周才会变」⇒ 归属烙上这条实例自己的编号（照 rec.goldKey
        -- / rec.kwLogKey 那份按实例记账的先例）：同一条实例已经定过 ⇒ 后来的答案不再覆盖
        -- （第一次见到那张图才算数）；这一轮没扫到 ⇒ 不清（"没问到"不是新事实，与
        -- L.SetView 那道 unknown 守卫同方向）；编号换了（周重置/换实例）⇒ 旧归属作废，
        -- 下一次扫到 pin 重新定。身份算不出（curKey 为 nil）时**照写**这条正面答案、只是烙
        -- 不上编号（新收的行这一趟还没法定身份，不写就等于分组永远出不来），但也**不清**旧
        -- 归属 —— 判不出就不动。
        -- 事件归属的“一轮”默认跟着每周锚点走（curKey）；但若这轮事件 pin 报得出自己的剩余时间
        -- （C_AreaPoiInfo.GetAreaPOISecondsLeft），就改以事件自身的轮换边界为轮次 —— 丰饶这类按
        -- 小时/天轮换的世界事件，每周锚点整周不变会把归属锁死在第一次见到的那张图（bug：丰饶归到
        -- 哈籁恩达尔）。pin 报得出剩余时间就用它，报不出回退每周锚点（旧行为不变，测试用的 mock
        -- 不含 secs 即走这条路，现有用例不受波及）。
        if best and best.secs and best.secs > 0 then
            rec.evRoundEnd = now + best.secs
        end
        local evKey
        if rec.evRoundEnd and rec.evRoundEnd > now then
            evKey = tostring(questID) .. ":e" .. math.floor((rec.evRoundEnd + 450) / 900)
        else
            evKey = curKey
        end
        if best then
            if not L.SameInstanceKey(rec.evMapKey, evKey) then
                rec.evMapID, rec.evName, rec.evAt = best.mapID, best.name, now
                rec.evMapKey = evKey or rec.evMapKey
                written = written + 1
            end
        elseif evKey and rec.evMapID and not L.SameInstanceKey(rec.evMapKey, evKey) then
            rec.evMapID, rec.evName, rec.evAt, rec.evMapKey = nil, nil, nil, nil
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
    local db = GQL.Config.DB()
    if not db then return nil end
    local now, uptime, domain = Clock()
    db.clockDomain = domain
    if domain ~= "epoch" then
        GQL.Config.Log("时钟域降级：无可用墙上时钟，时间戳只在本次开机内可比")
    end
    -- 调用本身要留（它自己会打日志、把 CD 锚点往前推），但返回值现在没人读：
    -- 上一版这里跟着 ClearCycleIgnores 解除上个 CD 的所有忽略，那句是误读玩家意图
    -- ——"移除"是永久屏蔽，换 CD 不作废（见 Ledger.RemoveQuest）。
    GQL.Config.RefreshCycle(domain == "epoch" and now or nil, uptime)
    if db.cycleAnchor == 0 then
        GQL.Config.Log("cycleAnchor 未知：周统计口径可能不准")
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
        GQL.Config.Log("地图事件接口这轮没答（C_AreaPoiInfo 缺失或报错）：归属图沿用上一轮")
    elseif pinWritten > 0 or pinCleared > 0 then
        GQL.Config.Log(("地图事件归属：写入 %d 条、清掉 %d 条（这块板不在世界任务池里）")
            :format(pinWritten, pinCleared))
    end
    -- 版本/事件闸挂的续命扩展点（AbundantOfferings.lua、以后别的版本专属周常都挂这里）：
    -- 每个函数拿 (db, now)，自己决定这轮要不要为某条任务续命（seen = true）。
    -- 走的是和 RefreshEventPins / DiffPool 同一套"本轮客户端回问到 ⇒ 续"的机制，无"常驻"特判。
    if type(GQL.SnapshotExtras) == "table" then
        for i = 1, #GQL.SnapshotExtras do
            local okE, errE = pcall(GQL.SnapshotExtras[i], db, now)
            if not okE and GQL.Config and GQL.Config.Log then
                GQL.Config.Log("快照扩展点 #" .. tostring(i) .. " 出错：" .. tostring(errE))
            end
        end
    end
    local pool, err, meta = GQL.Api.GetPool()
    -- 部分池（12.x 按图源只看到当前地图一片）：不在池里的任务不标消失，
    -- 空池二次确认也只对全量池有意义 —— 部分池为空只说明脚下这张图没有。
    local partial = not not (meta and meta.partial)
    if not pool then
        -- 板面证据问的是另一个接口（跟踪地图清单），池不可用时别跟着一起瞎：
        -- 只加不减 —— 上一轮的证据留着，这一轮新扫到的补上。
        ScanBoards(db, now)
        GQL.Config.Log("快照跳过：池不可用 " .. tostring(err))
        return { degrade = tostring(err) }
    end

    -- 有整张返回空的关注图：多半是客户端数据还没拉到（开一下地图就有了），
    -- 主动等效按 M 强制加载，数据回来后事件会触发下一轮快照自动补上。
    if meta and meta.emptyMaps then
        Sync.ForceMapDataLoad()
    end

    -- 板面证据每轮重问：上一轮挂在板上的这一轮可能已经换图/到期了。
    -- 部分池只看得见脚下这张图，清一次会把别的图上的证据抹掉 → 那一轮不清。
    -- 放在空池二次确认之前：清完就早退会把刚补扫到的行抹掉（30 秒后再问才有）。
    -- 注册族的板面证据必须在消失判定之前问完：这一轮还挂在图上的要留证。
    if not partial then
        for _, rec in pairs(db.quests or {}) do
            -- onBoard 是家族成员专用的字段（只有 ScanBoards/池/补扫会写），
            -- 所以清的时候不必再判一次认亲：有就清，这一轮的证据这一轮重问。
            if rec.onBoard then rec.onBoard = nil             end
        end
    end
    ScanBoards(db, now)

    if #pool == 0 and not partial then
        if not emptyPending then
            emptyPending = uptime
            GQL.Config.Log("池为空：30 秒后二次确认再判消失")
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
    -- 【11-04 取证，用户那句"检查为什么会读取不到"】上一版日志只留了一个笼统的计数，
    -- 答不出三个真问题：读不到的是**哪几条**、当时**世界地图开没开**、以及这几条的编号此刻
    -- 是不是被"本 CD 尾巴"顶出来的。真档 92576 就是这么卡住的：10:12:58 那次同步金额读到了
    -- （11181872 铜）、倒计时没读到，日志里连这条任务的名字都没留下，下一轮只能靠猜。
    -- 零新增接口调用：这里摊的全是同一次遍历已经问出来的读数。
    local noTimeGold, noTimeGoldMiss = {}, 0
    for i = 1, #pool do
        local questID = pool[i].questID
        inPool[questID] = true
        -- 池里出现 = 这一轮它确实挂在某张板上：族成员（含板上那条同名子任务）留下这份证据，
        -- 它参与"这族本周做过没有"，但占不占行由 Ledger.OccupiesRow 说（一族一行 = 族本体）
        local poolRec = db.quests[questID]
        -- PresetFor 一趟算完两用：非 nil = L.IsFamilyMember 成立，也是锚点解析的入口
        local preset = poolRec and L.PresetFor(db, questID) or nil
        if preset then
            poolRec.onBoard = true
            -- 锚点在同一趟续 lastSeenAt：族本体自己不上板（真档 89507「丰饶供品」与
            -- 90573-90576「加固符文石」那几条 poolSeen 与倒计时全是空），"本轮族里有成员
            -- 挂在板上"就是关于这一族的那条真事实。不接这一刀，EnsureRegistered 撤掉白续后
            -- 锚点会在换 CD 那一刻到期、整族当场死掉再也回不来。纯算术：锚点解析走现成的
            -- FamilyAnchorID（OccupiesRow 占行判据同一入口），零新增接口调用。
            local anchorID = L.FamilyAnchorID(db, preset)
            local anchor = anchorID and db.quests[anchorID]
            if anchor and anchor ~= poolRec then anchor.lastSeenAt = now end
        end
        -- 每条任务自己的接口读取单独容错：QuestState/QuestTimeLeft 在某一条上抛错
        -- 不该把整趟快照断在这儿——后面还有收日志、补地图、族搬运、消失判定，
        -- 以及"重排刷新定时器"。半途崩掉等于账本停在半更新、矩阵开着也不再刷新。
        -- 代价：每条多两次 pcall（Lua 里 pcall 不碰接口，只是调用壳，不多一次真接口调用）。
        local okCall, state1 = pcall(GQL.Api.QuestState, questID)
        if not okCall then
            GQL.Config.Log(("状态问不出 quest=%s: %s"):format(tostring(questID), tostring(state1)))
            state1 = nil
        end
        local rec = db.quests[questID]
        -- 记下到期时刻：世界任务到点才换，据此判断下次什么时候真需要刷新。
        -- 读不到（接口偶发 nil / 数据没加载 / 抛错）就只留取证，不排补读：这一条此刻的
        -- 编号按尾巴档算，下一次事件快照会照问（Task 1 那道门不成立 ⇒ 该问的照问），
        -- 用户实测"有个任务没有倒计时了"不会因此永久空着。
        -- 【整段排在 SetView/SetRewardRead 之前，计划 §六 1(a)】这三处都要烙"任务号+结束
        -- 时刻"的唯一编号（第 36 轮），而编号问的是 rec.roundEnd：**不挪的话**这条任务第一次
        -- 上板那一趟账本里还没有任何结束时刻 ⇒ 烙出来是 nil ⇒ 这一笔快照永久挂不上身份，
        -- ✓ 要等下一次同步才长得出来（用户 10-28 的靶心：94612 那 7 笔快照压根没有身份可判）。
        -- 挪了之后倒计时先落进 SetExpiry、下面算键用的就是这一条实例自己的结束时刻。
        -- 挪到这里不新增接口调用（还是同一条 pcall），pcall 容错一字未动。
        -- 【11-04 定稿 §二 1/2】三处读取点共用同一道门；没问就不许动结束时刻与取证。
        local asked = ShouldAskExpiry(db, questID, rec, now)
        local secs, timeFailed = nil, false
        if GQL.Api.QuestTimeLeft and asked then
            local okT, v = pcall(GQL.Api.QuestTimeLeft, questID)
            if okT then secs = v else timeFailed = true end
        end
        if not asked then
            -- 没问 ⇒ 结束时刻的写入、取证一个字都不动
            -- （那两处只属于"真问了"的分支；取证退化见 §二 2）
        elseif type(secs) == "number" and secs > 0 then
            -- 口径由 Ledger.SetExpiry 定：池里这条若是**常驻周常**（注册键不是 `pin:` 那一路 /
            -- kwLogAdded / 这一条实例被日志证明存在过），问出来的倒计时是它板上实例的轮换寿命
            -- （几小时），不能当到期写 roundEnd —— 12.1.5 实测"加固符文石这个任务没了"就是这么
            -- 下午整行消失的；普通世界任务、以及金币门从事件 pin 收的那一路（T6b 起不再早退）
            -- 照旧用自己那条倒计时写 roundEnd，矩阵才允许上色/报剩余（来源标记那一段随 T5 退役，
            -- roundEnd 在场就是板面读到的）。
            -- 【第 4 个参数】日志收进来那一路也算常驻周常：这一条实例被某个号的日志证明
            -- 存在过（编号 = 周 CD 尾巴档），给它写板上几小时的轮换寿命会把 roundEnd 落进账本
            -- ⇒ 编号从尾巴档挪到板面档 ⇒ 刚烙的那份凭据当场对不上、整行没（加固符文石
            -- "当天下午整行没了"那个形状）。这里手边没有本轮编号，让判据自己算一次纯算术
            -- （每趟同步每行一次，不在矩阵每帧那条路上）。
            if rec then
                L.SetExpiry(rec, now, secs, L.KeywordLogAdmits(db, questID, rec, now))
            end
        elseif not timeFailed then
            -- 金额问得出、倒计时问不出、而且这条**没有自己的板面结束时刻**（有 roundEnd 的
            -- 是上一条实例留下的墓碑，那是另一回事）⇒ 这一条此刻的编号只能按"本 CD 尾巴"算，
            -- 正是用户点名不许当万能兜底的那一档。把 ID 留在日志里，下一轮才分得清
            -- "图数据没到"和"这条自己不给"。
            if type(pool[i].money) == "number" and rec and not tonumber(rec.roundEnd) then
                noTimeGoldMiss = noTimeGoldMiss + 1
                if #noTimeGold < 12 then noTimeGold[#noTimeGold + 1] = questID end
            end
        end
        -- 这一条实例**现在**的编号：读完倒计时之后算这一次，下面四个用键的地方全共用它
        -- （§十一"每行只算一次"，Sync 这一趟是逐条问接口的路径，纯算术也不许多花几遍）。
        local curKey = L.InstanceKey(db, questID, rec, now)
        local rows = db.turnins[charKey] and db.turnins[charKey][questID]
        local last = rows and rows[#rows]
        -- 本轮亲眼记到的交任务优先于查询接口。t 可能是老存档里缺字段的记录
        -- （L.WeekStats 自己就写着 if r.t and ...），拿 nil 比大小会当场抛错 —— 现在更简单：
        -- 只比这一笔流水写下的编号（§四 R13），没编号 = 不匹配（§五）。
        -- 【为什么覆盖排在 SetExpiry 之后】同一个编号：state1 判"算不算这一条实例"用的键，
        -- 必须就是下面 SetView 要烙进快照的那一把。排在倒计时读取之前算，第一次上板的那一趟
        -- 会用"上一条实例/尾巴档"的旧键放行覆盖、却把新键烙进 view —— 等于拿旧证据给新实例
        -- 背书，正是本轮要消灭的那个形状。（计划 §六 1(a) 那句"覆盖仍排在 SetExpiry 之前"
        -- 是 T2 挪动时的现状描述，不是要求；换编号之后这个顺序必须跟着换，已回报。）
        if rec and last and L.SameInstanceKey(last.k, curKey) then
            state1 = "done"
        elseif state1 == "done" then
            -- 客户端答的 done 不许原样进快照（2026-10-29 健步如飞 ×14 假绿）：固定 ID 任务的
            -- 完成 flag 会跨轮/跨 CD 残留，分不清"这一轮做的"还是"上一轮剩的"。快照与点格子
            -- 的接口核验走同一道门：答 done 还要有编号对上的本轮流水背书，否则降级 open ——
            -- 真交过的号流水在，交割结算自己会补写 done。刀 2（2026-10-29）之后
            -- QUEST_TURNED_IN 到达当场就写下流水，之后的快照直接答得出 done；"先 open、
            -- 结算后自愈"的空档只剩一岔：只等到 QUEST_COMPLETE（开了对话框）那一下，
            -- RecordTurnIn 当场补记之后自愈（无 15 秒钱窗）。
            state1 = "open"
        end
        -- 金币粘滞按实例编号记账（Ledger.NoteGoldThisInstance，§四 W5 / §六 1(c) 四步）：
        -- 排在 SetExpiry 之后才用得上这一条实例自己的结束时刻，且 goldMoney 从此只在这里写
        -- —— DiffPool 那一趟不再碰金额（那处只剩"同一条实例才许搬钱"的读闪延续）。
        L.NoteGoldThisInstance(db, questID, pool[i].money, now, curKey)
        -- 视图统一走 Ledger.SetView 写，避免 { s, t } 这个形状在两处各拼一遍
        L.SetView(db, charKey, questID, state1, now, curKey)
        -- 顺手按号记一笔这一副奖励是多少钱（Ledger.SetRewardRead）：奖励是各号各自的，
        -- 而 rec.currentSig 只留"最后一次同步的那个号看到的那一副"。漏了这一笔，
        -- 给金币的号刷完之后矩阵会对所有号说"这一档不给金币"（12.1.5 实测 97128）。
        L.SetRewardRead(db, charKey, questID, pool[i].money, now, curKey)
    end
    -- 关键词的第二条来源：先收任务日志，再判"不在池里"。
    -- 顺序反了会自我打架 —— 刚由日志收进来的行本来就不在世界任务池里，
    -- 先跑消失判定会把它当场标成 ended，等于白收一趟。
    local logAdded = SyncKeywordQuestsFromLog(db, charKey, now, inPool)
    -- 已停用的地图入口写进账本的旧地图号清掉：不清的话 HealMissingMaps 只看 mapID==nil，
    -- 永远不去重问，用户下次开矩阵看到的还是"丰饶在屠戮者高地"（12.1.5 反馈⑥）。
    -- 只清这三条谎报接口自己写的号；旧版没标来源的号由 ClearRetiredMaps 保留并打标，
    -- 别一并清 —— 清掉的整批任务全塌进"未知地图"（第十五轮反馈②③）。
    if GQL.Api and GQL.Api.RetiredMapSources then
        local cleared = L.ClearRetiredMaps(db, GQL.Api.RetiredMapSources())
        if cleared > 0 then
            GQL.Config.Log(("清掉 %d 条谎报接口的地图号（只认它自己写的来源），重新按可信链问")
                :format(cleared))
        end
    end
    -- 补地图放在收日志之后：这一轮新收的那几条也要当场有地图，不然矩阵底部
    -- 先冒出一堆"未知地图"、要等下次同步才归位
    HealMissingMaps(db)
    -- 族本体的地图要在补问之后搬：成员自己那条的图可能正是上面这一步刚问出来的
    -- （判据与来源白名单见 Ledger.PropagateFamilyMaps）
    L.PropagateFamilyMaps(db)
    if not partial then
        for questID, rec in pairs(db.quests) do
            -- 整块板扫完（不是 partial）却没有这条 ⇒ 消不消失只问一个**事实**：这条 ID
            -- 上没上过板（poolSeen 只在 DiffPool 由池快照写、全仓没有清它的地方，语义就是
            -- "客户端确实答过它上过板"）。上过板而现在不在池里 = 真没了 ⇒ 这一格判 ended，
            -- 带什么注册键、撞没撞关键词都不改这一句（用户 2026-11-02 D6：上板的任务没有
            -- 一条可以豁免说不下板）。从没上过板的记录 —— 家族锚点、城市/战役日志任务、
            -- 亚基那种客户端永远不给任务号的虚拟行 —— "不在池里"是常态不是新闻，这句
            -- 一行不碰；它们的寿命由到期门 L.InstanceExpired 按各自那条结束时刻判。
            -- 不用 onBoard 当判据：它每轮快照清一次（上面 :823 那段），问的是"这一轮"不是"上过板"。
            if not inPool[questID] and rec.poolSeen then
                L.SetView(db, charKey, questID, "ended", now)
                -- 不在池里的任务过去值的到期不占着"最早到期"——这句老话以前靠
                -- 清 rec.expiresAt/expiresSrc 兑现；T5 起那一组字段整组退役，不清也不读。
                -- 现在的真源是 L.EffectiveExpiry（T4a 换的）：已经过去的一律不报，
                -- NextRefreshAt 天生不会被上一条实例的墓碑拖着。
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
        if GQL.Config.GetKeywords and #GQL.Config.GetKeywords() > 0 then
            GQL.Config.Log("世界任务池已轮换：自动补扫跟踪地图")
            pcall(Sync.HuntKeywordMaps)
        end
    end
    GQL.Config.Log(("快照完成 池=%d 部分=%s 源=%s 新增=%d 消失=%d 重现=%d 奖励变化=%d 日志收=%d 锚点=%d 角色=%s")
        :format(#pool, tostring(partial),
            (meta and meta.source) or "全量源",
            #res.added, #res.gone, #res.renewed, #res.sigChanged, logAdded,
            db.cycleAnchor or 0, charKey))
    -- 只在真出 anomaly 时多这一行：0 条时不写，日志不会被它灌满
    if noTimeGoldMiss > 0 then
        GQL.Config.Log(("金额读到了、倒计时读不出 %d 条[%s]（这一轮的世界地图=%s，"
            .. "本周尾巴锚点=%s；这几条此刻的编号是按尾巴算的）")
            :format(noTimeGoldMiss, table.concat(noTimeGold, ","), WorldMapOpenText(),
                tostring(db.cycleAnchor or 0)))
    end
    ScheduleNextRefresh()
    -- 11-04 定稿 §二 4：这里原来还挂着一套"金额/倒计时没读到就每 5 秒重问整板块、封顶 4 次"。
    -- 整板块重问 = 几百张图逐个问，是用户实测卡顿的同一个来源；服务器数据要几十秒才回来
    -- （用户实测"第一次打开矩阵只显示一部分"），缺的钱改走上面的定向补读
    -- （SettleAskedRewardGold：每条每趟 1 次只读、封顶 6 趟），缺的结束时刻走下一次事件快照
    -- （Task 1 那道门不成立 ⇒ 照问）。
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
        if not okCall then GQL.Config.Log("快照异常: " .. tostring(err)) end
        if wasLogin then
            -- 耗时用会话秒（单调、与墙上时钟域无关）：这是"登录卡不卡"的唯一实测口径。
            -- 这句不许再带"矩阵没开，之后不再自动抓池"那串尾巴：它是硬编码的，
            -- 而登录后立刻开矩阵的情况下一样会继续抓池 —— 回传时照着它判断就错了。
            local spent = select(2, Clock()) - up0
            if spent < 0 then spent = 0 end
            GQL.Config.Log(("登录同步完成 用时%d秒"):format(spent))
        end
        -- 快照半路抛错时它自己末尾那句"按最早到期排下次刷新"跑不到：这里补一次，
        -- 否则矩阵开着却再也没有下一次刷新（12.1.5 遗留的"补读会话死锁"就是这个形状）。
        -- ScheduleNextRefresh 第一句就 Cancel 旧定时器，重复调用只会留一个待触发。
        pcall(ScheduleNextRefresh)
        -- 抓完必须喊面板重画：这条是日常唯一自动跑的路（做任务时事件连成一片），
        -- 少了这一句就是"账本里金额/状态都更新了，屏幕上还是上一份"——12.1.5 实测
        -- 89347 那行 967 金进账本 12 分钟了矩阵里还是没有。重画只把内存里的账本再抄
        -- 一遍，零接口调用，且 Mx.Refresh 自己判面板开没开（没开就直接返回）。
        if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
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
    local kws = GQL.Config.GetKeywords()
    if #kws == 0 then return false, "先添加关键词" end
    local tracked = (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs())
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
    local dbNow = GQL.Config.DB()
    local anchors = (dbNow and L.RegisteredIndex and L.RegisteredIndex(dbNow)) or {}
    for i = 1, #anchors do push(GQL.Api.QuestMapID(anchors[i].questID)) end
    if #maps == 0 then
        return false, "跟踪清单是空的：先站到任务所在的地图上点\"＋添加当前所在地图\""
    end
    local hits, added, familySkipped = 0, 0, 0
    local found, seenID = {}, {}
    for k = 1, #maps do
        local mapID = maps[k]
        -- 单图问不出数据（接口报错/返回怪形状）就跳过这张，不许把整轮补扫炸掉
        local okRows, rows = pcall(GQL.Api.MapQuestRows, mapID)
        for n = 1, (okRows and type(rows) == "table" and #rows or 0) do
            local questID = rows[n].questID
            local name = questID and GQL.Api.QuestTitle(questID)
            local kw = name and GQL.Config.KeywordHit(name)
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
                GQL.Config.Log(("补扫命中：%s（任务#%s，关键词「%s」，地图 %s）")
                    :format(tostring(name), tostring(questID), tostring(kw),
                        tostring(rows[n].mapID or mapID)))
                if GQL.Config.AddMap(rows[n].mapID or mapID) then added = added + 1 end
            end
        end
    end
    local reged = 0
    local db = GQL.Config.DB()
    if db and #found > 0 then
        local now = select(1, Clock())
        local charKey = Sync.CurrentCharKey()
        for j = 1, #found do
            local questID = found[j].questID
            found[j].money = GQL.Api.QuestRewardMoney(questID)
            found[j].state = select(1, GQL.Api.QuestState(questID))
        end
        reged = L.EnsureRegistered(db, found, charKey, now)
    end
    -- "注册周常"这一列是**新增/重问的 kw 行**，hits 里被族词跳过的那几条另报：
    -- 玩家看到"命中 0 条"会以为关键词写错了，实际是那条本来就由内置表管着
    -- （12.1.5 实测反馈"按关键词…命中 0 条"配着丰饶那条明明在做）。
    local skipNote = familySkipped > 0
        and ("；另有 %d 条命中内置族词，那一行由内置表管，不重复注册"):format(familySkipped) or ""
    GQL.Config.Log(("关键词补扫：%d 张图（跟踪清单 + 族锚点报的图）× %d 个关键词，命中 %d 条任务，"
        .. "新增跟踪 %d 张地图，注册 %d 条周常%s"):format(#maps, #kws, hits, added, reged, skipNote))
    pcall(Sync.SnapshotNow)
    if GQL.Matrix and GQL.Matrix.Refresh then pcall(GQL.Matrix.Refresh) end
    local final
    if hits == 0 then
        -- "命中 0 条"对玩家只有一句话：我加了关键词为什么没有行。原因分两种，得说清是哪种：
        -- ① 这名字压根不在跟踪清单的板上（图没加进来 → 站到那张图上点＋添加）；
        -- ② 它是**地图事件 pin**（亚基入侵那种），板面清单与任务日志两条路都碰不到它 ——
        --    能不能成行只看客户端给没给这个 pin 挂任务号（给了由 RefreshEventPins 注册）。
        local pinNote = ""
        if GQL.Api.EventPins then
            local hitNames = {}
            for k = 1, #maps do
                local okPins, pins = pcall(GQL.Api.EventPins, tonumber(maps[k]))
                for j = 1, (okPins and type(pins) == "table" and #pins or 0) do
                    local kw = pins[j].name and GQL.Config.KeywordHit(pins[j].name)
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
                    .. "⇒ 那不是任务，矩阵做不出「哪个角色做过」，取证看 /gql probe 的事件段")
                    :format(table.concat(plain, "、"))
            end
        end
        final = ("%d 张图没有命中：先确认关键词写的是任务名的一部分，"
            .. "任务在没跟踪的图上时站到那张图上点\"＋添加当前所在地图\"（或按名称添加）再补扫%s%s")
            :format(#maps, skipNote, pinNote)
    else
        final = ("%d 张图扫完：命中 %d 条，注册 %d 条周常（新增跟踪 %d 张地图）%s")
            :format(#maps, hits, reged, added, skipNote)
    end
    -- 最终这句既回报聊天、也进 db.logs：聊天一滚过去就读不到，/gql log 要留得住
    -- "哪条名字只在事件 pin 里、客户端没给任务号"这种取证尾巴（pinNote 早算好在内存里，
    -- 零新增接口调用）。放在 SnapshotNow/Matrix.Refresh 之后 → 这条是本函数写进日志的最后一条，
    -- 日志句与聊天句是同一条字符串，不许各说各话。
    GQL.Config.Log(final)
    return true, final
end

--- 删关键词 → 退掉由它注册的行。内置预设那一行拆不到：它的 key 不带 kw: 前缀，
--- 玩家删"丰饶"不该把丰饶供品从内置表里摘掉。
function Sync.UnregisterKeyword(kw)
    local db = GQL.Config.DB()
    if not db or not kw then return 0 end
    local n = L.UnregisterByKey(db, KW_KEY .. tostring(kw))
    if n > 0 then
        GQL.Config.Log(("关键词「%s」已删除，退掉 %d 条注册周常"):format(tostring(kw), n))
    end
    return n
end

--- 逐个容错注册。12.1.5 实测 RegisterEvent 对客户端不认识的事件名直接抛错，
--- 而 WORLD_QUEST_LOG_UPDATE 正炸在 OnWorldEnter 第一行，登录同步整条没跑
--- （用户日志里那行 "Attempt to register unknown event" 就是这件事）。
--- 一个名字失败不许带累后面的名字。
--- 客户端不认的事件名记进这张表：同一会话内只试一次、只提示一次。
--- 12.1.5 里 WORLD_QUEST_LOG_UPDATE 根本不存在，而这段在每次 PLAYER_ENTERING_WORLD
--- 都会跑一遍 —— 不记死的话，每过一张图就往日志里刷一行一模一样的报错。
local eventDead

local function TryRegister(name)
    if eventDead and eventDead[name] then return false end
    local okCall, err = pcall(Sync.frame.RegisterEvent, Sync.frame, name)
    if not okCall then
        eventDead = eventDead or {}
        eventDead[name] = true
        GQL.Config.Log("注册事件失败 " .. name .. ": " .. tostring(err)
            .. "（此事件名本机不认，之后不再重试）")
    end
    return okCall
end

local function AnyRegistered(names)
    for i = 1, #names do
        if TryRegister(names[i]) then return true end
    end
    return false
end

function Sync.OnWorldEnter(isLoginLike)
    -- 池变化事件按候选退：QUEST_LOG_UPDATE 历史版本一直在，覆盖面也够
    -- （任何任务状态变化都会发，去抖已经把重复抓取压成一次）
    if not AnyRegistered(POOL_UPDATE_EVENTS) then
        GQL.Config.Log("池变化事件全部不可用：只在登录与 /gql sync 时更新池")
    end
    for i = 1, #TURN_IN_EVENTS do TryRegister(TURN_IN_EVENTS[i]) end
    -- 日志旁路的入口：世界任务走近自动挂进日志时客户端会发 QUEST_LOG_UPDATE
    -- （已在上面 AnyRegistered 注册）；QUEST_ACCEPTED 对世界任务发不发没有实测
    -- 依据，一并挂上，旁路自己 10 秒节流，多挂不贵。
    TryRegister("QUEST_ACCEPTED")
    -- 登录抓一次（默认开）。设置面板上那颗开关 2026-10-27 已撤下（用户口径："默认就抓取
    -- 不用设置"），现在只剩 Config.SetSyncOnLogin 这个入口 —— 想关就改存档
    -- ["syncOnLogin"] = false，别再往界面上加回来：用户 2026-10-24 实测过后台抓池会卡，
    -- 这颗开关不是白造的，真卡回来时按上面那条改存档。
    -- 只排这一趟，之后没开面板就不再有抓池动作：门闩在去抖回调里落下，不会累积。
    -- nil = 旧调用方 / 离线用例直调，保持"登录那一趟"的旧行为（459 条用例依赖）；
    -- 只有显式 false（过图 / 传送）才落下门闩。
    loginPending = (isLoginLike ~= false) and (GQL.Config.SyncOnLogin() == true)
    ScheduleSnapshot()
    -- 默认配置包的重试点（Defaults.lua）：包里的地图按名字解，登录那一刻地图树
    -- 常常还没就绪（Api.WQMapList 问不出），那一趟只落关键词、不记"套过"。
    -- 这里每次进世界补一次：已套过的包由 db.config.defaults 直接跳过，是零成本的。
    if GQL.Defaults and GQL.Defaults.Apply then
        local okDef, err = pcall(GQL.Defaults.Apply, GQL.Config.DB())
        if not okDef then GQL.Config.Log("默认配置包出错: " .. tostring(err)) end
    end
end

--- 交割完成凭据：QUEST_COMPLETE（打开交割对话框）与 QUEST_TURNED_IN（真按了完成任务）
--- 任一到达就**当场**记一笔，不依赖任何计时窗（2026-10-29 用户定稿：不要等 15 秒再更新、
--- 交任务获得的金币不准，整体退役）。两个事件常连着来，按"这一条实例的编号"去重：
--- 同一实例已经记过就跳过，不重复落账。到手金币不在这一处量 —— 奖励是不是金币改由
--- 奖励读取一路负责（DiffPool / NoteGoldThisInstance），周统计读 rec.goldMoney。
--- 编号算不出（板上倒计时刚跨过去 / 锚点还是 0）也照记这一笔、快照照写"done"：
--- "做过"这一事实不能因为身份没定就凭空消失（周统计数的是流水行）。这一笔的 k 是 nil，
--- 于是绿勾那一侧的 L.SameInstanceKey 永不匹配 = "没编号 = 不匹配"（§五）照旧成立。
--- 门挡在门外的那条路（陌生任务、没记录）照旧一笔不记、一行不造。
local function RecordTurnIn(questID)
    questID = tonumber(questID)
    if not questID then return end
    local db = GQL.Config.DB()
    if not db then return end
    if not (L.QuestKnown(db, questID)
        or (GQL.Api.IsWorldQuest and GQL.Api.IsWorldQuest(questID) == true)) then
        return
    end
    local charKey = Sync.CurrentCharKey()
    local rec = db.quests[questID]
    local t = Clock()
    -- 【11-04 取证 → 收下】交割这一刻先问一遍：答得出的结束时刻当场落进这条记录，
    -- 下面那把键才算得对（92576 那一单：矩阵关着时这一条从头到尾没有结束时刻，
    -- 于是交割烙下的编号落在"本周尾巴"那一档，真值后来进来全对不上 = 玩家看到的"勾没了"）。
    -- 它顺带把落账之后算好的键交回来，这一笔流水与那一行日志用的是同一把。
    local okCapture, curKey = pcall(Sync.CaptureQuestData, questID, "交割")
    local key = (okCapture and curKey) or L.InstanceKey(db, questID, rec, t)
    local rows = db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    -- 同一实例已记过的两种形状：编号相同（有身份），或**双方都没有编号且落在同一秒**
    -- （没身份时唯一的凭据就是时刻 —— Clock() 答整秒，同一秒里的 QUEST_COMPLETE 与
    -- QUEST_TURNED_IN 必然是同一笔交割，不是连着做的两条任务）。
    if last and (L.SameInstanceKey(last.k, key)
        or (last.k == nil and key == nil and last.t == t)) then return end
    L.OnTurnIn(db, charKey, questID, t, rec and rec.currentSig or nil)
    L.SetView(db, charKey, questID, "done", t)
    GQL.Config.Log(("交割当场落账 quest=%s"):format(tostring(questID)))
    if GQL.Matrix and GQL.Matrix.Refresh then GQL.Matrix.Refresh() end
end
Sync.RecordTurnIn = RecordTurnIn

Sync.frame = CreateFrame("Frame", "GQLSyncFrame", UIParent)
Sync.frame:SetScript("OnEvent", function(self, event, arg1)
    -- 分发按候选名单判断，不写死单个名字：注册成功的可能只剩下面的备用事件
    for i = 1, #POOL_UPDATE_EVENTS do
        if event == POOL_UPDATE_EVENTS[i] then
            -- 日志旁路不受轮询门管（它只翻一遍任务日志，量级小）：
            -- 矩阵关着时快照被挡，金币世界任务只有这条路能进账本
            Sync.LogBypass()
            return ScheduleSnapshot()
        end
    end
    if event == "QUEST_ACCEPTED" then
        -- 【11-04 取证 → 收下】接上的这一刻问一遍，答得出的结束时刻当场写进这一条，
        -- 并把答案打进日志（矩阵关着时这是这一条唯一来得及的写点）
        pcall(Sync.CaptureQuestData, arg1, "接任务")
        return Sync.LogBypass()
    end
    for i = 1, #TURN_IN_EVENTS do
        if event == TURN_IN_EVENTS[i] then
            -- QUEST_COMPLETE 与 QUEST_TURNED_IN 都当场记完成凭据（RecordTurnIn 内部
            -- 按实例去重），不再有任何跨事件的待结算窗口。
            Sync.RecordTurnIn(arg1)
            return
        end
    end
end)
TryRegister("PLAYER_LOGOUT")   -- 交任务凭据已在 RecordTurnIn 内当场落账，无待结算窗口

-- 离线的结算在 RecordTurnIn 内当场完成，不再有跨事件的待结算窗口。

GQL.RegisterCommand("sync", function()
    local res = Sync.SnapshotNow()
    if not res then
        print("GQL: 同步失败，详情 /gql log")
    elseif res.degrade then
        print("GQL: 池不可用（" .. res.degrade .. "），账本保持不变")
    elseif res.pendingEmpty then
        print("GQL: 池为空，30 秒后二次确认（先不动账本）")
    else
        -- 带上"日志收"：用户实测关键词命中 0，这一行就是判"第二条来源接通没"的现场证据
        print(("GQL: 池 %d 条，新增 %d，消失 %d，重现 %d，奖励变化 %d，日志收 %d")
            :format(res.poolCount, #res.added, #res.gone, #res.renewed, #res.sigChanged,
                res.logAdded or 0))
    end
end, "立即重跑一次池快照 diff")
