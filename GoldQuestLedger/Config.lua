-- 账本数据层：SavedVariables 建库、周 CD 锚点、诊断日志环。
-- 数据结构（GQLDB，account-wide）：
--   schema = 5                         -- 存档结构档位：写了也读（Config.Migrate 按档跑一次性升级）
--   cycleAnchor = <本 CD 起点时间戳>
--   clockDomain = "epoch" | "uptime"   -- 时间戳存的是哪个时钟域，uptime 跨开机归零
--   stickyGoldReset = 2                -- 一次性迁移的标记（分档，见下）：老存档里被旧代码写坏的
--                                       -- "本轮读到过金币"已作废（Ledger.ResetStickyGoldOnce）。
--                                       -- 1 = 只清了粘滞那一半，被污染的 observation 还会把它长回来
--   quests[questID]     = { name, mapID, lastSeenAt, goldLock, currentSig, highFreq, goneAt,
--                           roundAt=<本轮起点>, roundEnd=<本轮到期时刻>,
--                           goldMoney/goldRound=<本轮读到过的金币与它属于哪一轮>,
--                           goldKey=<同一笔钱所属实例的唯一编号（新增，读者在 T4）>,
--                           ignored=<玩家在"关键词 · 屏蔽管理"点"忽略" = 永久屏蔽，只有点"取消忽略"才撤销>
--                           observations[sig] = { gold, money, items, firstSeen, lastSeen, goneSince,
--                                                 key=<这一副奖励属于哪条实例，见 Ledger.NoteGoldThisInstance> } }
--                           （firstSeen 只是 roundAt 的镜像，判据一律问 Ledger.RoundAt）
--                           （manualAdded 这一档 2026-10-27 整体下线：点"添加"只是撤销忽略，
--                            不再是矩阵的准入门。老存档残留由升级档 v4/v5 作废）
--   chars[角色-服务器]   = { class, level, lastSync, views[questID] = { s, t, k },
--                            reads[questID] = { g=<这个号自己读到的金币>, t, k } }
--                            （reads 是"奖励按号各不同"的凭据：同 questID 一个号给金币、
--                             另一个号给物品，见 Ledger.SetRewardRead）
--   turnins[角色][任务]  = { { t, g, sig, cycle, round, k }, ... }   -- 1-based 追加日志
--   manual[角色][任务]   = { state, t, g, v, round, k }
--   k = 第 36 轮"任务号 + 结束时刻"的唯一编号（Ledger.InstanceKey，判不出身份时不写 = nil）；
--       round = 上一档那个挂在"本轮起点"上的旧身份，两边并行写、读侧到 T4 才换成认 k。
--   logs                = { 最近 200 条 }

GQL.Config = {}
local Config = GQL.Config

local WEEK_SECONDS = 604800
local MAX_LOGS = 200
--- 金币门槛的默认值（单位=金）。门槛的唯一真源是 db.config.minGold（见 SetMinGold），
--- 这个默认只在账本里从没设过时生效；Init 在它上面，所以必须声明在文件头。
local DEFAULT_MIN_GOLD = 500

function Config.DB()
    return GQLDB
end

-- 金币格式化。nil → "?" 表示"未知"，与 0（真没赚钱）严格区分。
-- 负数同样显示 "?"：Ledger.WeekStats 用 -1 当"本周统计不可信"的哨兵，而钱永远不可能为负，
-- 判负收在这里就不用每个调用点各写一遍 —— 调用点写 `gold < 0 and nil or gold` 会被
-- Lua 的 `X and nil or Y` 恒等于 Y 坑掉，把 -1 原样格式化成 "-1g99s99c" 那种荒唐数。
function Config.FormatGold(copper)
    if type(copper) ~= "number" or copper < 0 then return "?" end
    return string.format("%dg%ds%dc",
        math.floor(copper / 10000), math.floor((copper % 10000) / 100), copper % 100)
end

-- 金额图标格式化：钱币用 WoW 的金银铜图标，矩阵界面用这个；
-- dump/日志等纯文本场景仍用 FormatGold。
-- |T 转义必须是 路径:高:宽|t 三段式 —— 只写高不写宽（:0|t）客户端不认，
-- 会把整串转义当普通字符原样印出来（用户截图里那一长串就是这事）。
-- 金>0 时只显示金银两位，<1 金才落银铜/铜，和系统金钱框的显示习惯一致。
local ICON_GOLD = "|TInterface\\MoneyFrame\\UI-GoldIcon:12:12|t"
local ICON_SILVER = "|TInterface\\MoneyFrame\\UI-SilverIcon:12:12|t"
local ICON_COPPER = "|TInterface\\MoneyFrame\\UI-CopperIcon:12:12|t"
function Config.FormatGoldIcon(copper)
    if type(copper) ~= "number" or copper < 0 then return "?" end
    local g = math.floor(copper / 10000)
    local s = math.floor((copper % 10000) / 100)
    local c = copper % 100
    if g > 0 then
        return string.format("%d%s%d%s", g, ICON_GOLD, s, ICON_SILVER)
    end
    if s > 0 then
        return string.format("%d%s%d%s", s, ICON_SILVER, c, ICON_COPPER)
    end
    return string.format("%d%s", c, ICON_COPPER)
end

-- 剩余时间格式化。地图 pin 上的世界任务倒计时就是这单位（秒）。nil/0 → "?" 表示未知。
function Config.FormatTimeLeft(seconds)
    seconds = tonumber(seconds)
    if not seconds or seconds <= 0 then return "?" end
    local d = math.floor(seconds / 86400)
    local h = math.floor((seconds % 86400) / 3600)
    local m = math.floor((seconds % 3600) / 60)
    if d > 0 then return ("%d天%d时"):format(d, h) end
    if h > 0 then return ("%d时%d分"):format(h, m) end
    return ("%d分"):format(math.max(m, 1))
end

--- 绝对时刻（"10-24 19:32"），只给**墙上时钟域**的时间戳用。开机秒（GetTime）跨重启归零，
--- 拿它格式化会印成 1970 年 —— 那不是"精度差一点"，是凭空造一个不存在的日期，所以直接回
--- nil 让调用方改口（矩阵悬停没数就不写那一行）。判域只看数量级：纪元秒 > 1e9，
--- 开机秒远小于它（与 L.WeekStats 那条哨兵同一口径）。
--- `date` 在客户端是全局，离线替身没有 → 退到 os.date，两条都不答也回 nil 不抛错。
function Config.FormatStamp(t)
    local n = tonumber(t)
    if not n or n < 1e9 then return nil end
    local fn = date
    if type(fn) ~= "function" and type(os) == "table" then fn = os.date end
    if type(fn) ~= "function" then return nil end
    local okCall, s = pcall(fn, "%m-%d %H:%M", n)
    if okCall and type(s) == "string" and s ~= "" then return s end
    return nil
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
-- 12.1.5 游戏内实测（/gql probe）：GetQuestResetTime=46509 而开机秒 GetTime=569772 ——
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

--- 周口径自检：真·周重置的"下一次结束时刻"必须落在 CD 锚点 + 整周的格子上
--- （endAt - anchor 是 604800 的整数倍，允许 2 分钟误差）。锚点未知或落在开机秒域
--- 时没有可比的格子，不设这道门（宁可不判，也不拿不可比的数判错）。
local weeklyMismatchLogged
local function WeeklyFitsAnchor(endAt)
    local db = Config.DB()
    local anchor = db and tonumber(db.cycleAnchor) or 0
    if anchor <= 1e9 then return true end
    local diff = (endAt - anchor) % 604800
    return diff <= 120 or diff >= 604680
end

--- 下次重置的时刻（跟 now 同一个时钟域）+ 第二个返回值是口径："weekly"=周重置接口答的，
--- "daily"=只有每日倒计时可用。定不出来返回 nil。
--- 周常任务"活到什么时候"要的就是这一刻，而且必须是**周**口径：12.1.5 用户实测
--- GetQuestResetTime 答的是每日 7 点那档（周五看到"距下次周重置 12 时 6 分"= 明早 7 点），
--- 拿它当 CD 结束会让周常行在第二天早上就被判过期（上一版"周常日期不对、马上就要结束"
--- 与矩阵悬停那个 12 时 6 分都是这么来的）。域对不上（纪元 vs 开机秒）也返回 nil，
--- 绝不跨域比大小，也绝不为了"界面有个数"编一个。
function Config.NextCycleEnd(now, uptimeNow)
    now = tonumber(now)
    if not now then return nil end
    if uptimeNow == nil and GQL.Api and GQL.Api.Uptime then
        uptimeNow = GQL.Api.Uptime()
    end
    local resetTime = (GQL.Api and GQL.Api.WeeklyResetCountdown)
        and GQL.Api.WeeklyResetCountdown() or nil
    local kind = "weekly"
    if type(resetTime) ~= "number" then
        -- 只剩每日档可用：上一版这里在 Api 答不出之后又自己去摸了一遍全局
        -- GetQuestResetTime，等于绕过 Api 的候选名单与容错；而运行时所有文件都加载完了
        -- （NextCycleEnd 只在 ADDON_LOADED 之后被叫），问 Api 一次就够 —— 它答不出就是
        -- 这台机器没有这个接口，再摸一次全局也不会多出答案。
        resetTime = (GQL.Api and GQL.Api.ResetTime) and GQL.Api.ResetTime() or nil
        kind = "daily"
    end
    local form = Config.ResetFormOf(resetTime, tonumber(uptimeNow))
    -- 时刻定不出来时口径照旧返回：调用方要分得清"周接口压根没答"和"答了但不可信"。
    if not form then return nil, kind end
    local endAt
    if form == "countdown" then
        -- 倒计时是一段"时长"，搭在开机秒还是纪元秒上都成立
        endAt = now + resetTime
    elseif form == "epoch" then
        -- 纪元绝对值：now 是本次开机秒时两者不可比，宁缺勿错
        endAt = now > 1e9 and resetTime or nil
    elseif now > 1e9 then
        -- sessionAbsolute：resetTime 本身就是开机域的绝对值
        local up = tonumber(uptimeNow)
        endAt = up and (now + (resetTime - up)) or nil
    else
        endAt = resetTime
    end
    if not endAt then return nil, kind end
    if kind == "weekly" and not WeeklyFitsAnchor(endAt) then
        if not weeklyMismatchLogged then
            weeklyMismatchLogged = true
            local db = Config.DB() or {}
            Config.Log("周口径自检失败：报出的下次结束=" .. tostring(endAt)
                .. " 与 CD 锚点=" .. tostring(db.cycleAnchor)
                .. " 不在同一周的格子上（每日档冒充周档）→ 一律按不知道处理")
        end
        return nil, "weekly"
    end
    return endAt, kind
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
    if wallNow == nil and GQL.Api and GQL.Api.WallClock then wallNow = GQL.Api.WallClock() end
    if uptimeNow == nil and GQL.Api and GQL.Api.Uptime then uptimeNow = GQL.Api.Uptime() end
    -- 只认**周**口径的倒计时：GetQuestResetTime 答的是每日那档（12.1.5 用户实测），
    -- 拿它减一周得到的锚点每天往前挪 24 小时，"本周完成数/金币"会静默算错。
    -- 周接口问不出就沿用旧锚点并降级报一句 —— 报"不知道"比报一个看着正常的错数好。
    local weekly = (GQL.Api and GQL.Api.WeeklyResetCountdown)
        and GQL.Api.WeeklyResetCountdown() or nil
    if type(weekly) ~= "number" then
        local srcs = (GQL.Api and GQL.Api.WeekResetSources) and GQL.Api.WeekResetSources() or {}
        Config.Log("RefreshCycle 降级：周重置接口不可用（" .. table.concat(srcs, " / ")
            .. "），沿用旧锚点；/gql probe 有逐条证据")
        return false
    end
    local anchor = Config.CycleAnchorOf(weekly, wallNow, uptimeNow)
    if anchor and anchor > (db.cycleAnchor or 0) then
        db.cycleAnchor = anchor
        return true
    end
    return false
end

--- 把窗口挂进暴雪的 ESC 关闭名单。口径来自 warcraft.wiki.gg
--- "Make frames closable with the Escape key"：UISpecialFrames 存的是**全局帧名字符串**，
--- 没名字的帧（CreateFrame 第二参传 nil）挂进去也没用，所以调用方必须传建帧时写死的名字。
--- 12.x 还认不认这张表没有实测依据：表取不到就如实降级记日志，
--- 别让"按了 ESC 没反应"变成一条查不出原因的玄学反馈。
function Config.RegisterEscapeClose(frameName)
    local list = UISpecialFrames
    if type(list) ~= "table" then
        Config.Log("ESC 关闭降级：UISpecialFrames 不可用（" .. tostring(frameName) .. "）")
        return false
    end
    for i = 1, #list do
        if list[i] == frameName then return true end   -- 幂等：重复建窗口的路径不许挂两遍
    end
    table.insert(list, frameName)
    return true
end

--- 存档结构版本与一次性升级。schema 以前只写不读（`math.max(db.schema or 0, 1)`），
--- 设计文档 §10 承诺的"版本不符走一次性升级函数"根本没实现，于是老存档的坏数据
--- 只能每次快照都判一遍（Sync 里那段 ResetStickyGoldOnce 就是这个补丁）。
--- 现在：一次登录一趟、失败停在上一档下次重试、绝不重建原始表
--- ——重建等于抹掉用户几个 CD 的金币记录，比坏数据严重得多。
Config.SCHEMA_VERSION = 5
Config.MIGRATIONS = {
    -- v2：作废旧版本写错的金币凭据（上一轮的金额记进了本轮；判据与代价见
    -- Ledger.ResetStickyGoldOnce 的注释——那一版只清了粘滞，没清来源，所以档位记 2）
    [2] = function(db) return GQL.Ledger and GQL.Ledger.ResetStickyGoldOnce
        and GQL.Ledger.ResetStickyGoldOnce(db) or 0 end,
    -- v3：清掉"账本里查不到可信身份"的交任务凭据。Task 2 之前交一条职业/战役任务
    -- 也会写 turnins，而这份凭据是矩阵准入门之一（能绕过金币门槛占一行）。
    -- 判据与代价见 Ledger.DropTurninsWithoutEvidence 的注释。
    [3] = function(db) return GQL.Ledger and GQL.Ledger.DropTurninsWithoutEvidence
        and GQL.Ledger.DropTurninsWithoutEvidence(db) or 0 end,
    -- v4：作废旧版本"点一次永久有效"的手动添加标记。判据改成"本轮点过才算"（RollRound）
    -- 只对下一次换轮生效，存档里已经躺着的那一份不自愈 —— 不清就得等下一次 12 小时刷新
    -- 才见效，玩家会当成"代码改了没生效"（判据与代价见 Ledger.ClearManualAddsOnce）。
    -- 本轮之后 `manualAdded` 字段整体下线（矩阵准入门里再没有"玩家点了一下"这一道），
    -- v4 留着只管一件事：升级阶梯连续 —— v3 存档要跑 4 再跑 5，抽掉中间档就断链。
    [4] = function(db) return GQL.Ledger and GQL.Ledger.ClearManualAddsOnce
        and GQL.Ledger.ClearManualAddsOnce(db) or 0 end,
    -- v5：把老存档里**两份**玩家标记一起作废（ignored + manualAdded）。"移除"从这一轮
    -- 起是**永久屏蔽**，上一版"只管本 CD"里随手点掉的那次不许在新语义里永久生效；
    -- 代价照实：真想永久屏蔽的那几条也得重新点一次（判据与代价见
    -- Ledger.ClearIgnoreAndAddMarksOnce，用户 2026-10-27 要求的就是这个顺序）。
    [5] = function(db) return GQL.Ledger and GQL.Ledger.ClearIgnoreAndAddMarksOnce
        and GQL.Ledger.ClearIgnoreAndAddMarksOnce(db) or 0 end,
}

--- 逐档升级。返回 true=有档被跑过，false=已是最新或失败。
--- 每档失败就停：后面的档依赖前面的形状，跳档跑会把账本弄成半新半旧。
function Config.Migrate(db)
    if not db then return false end
    local from = tonumber(db.schema) or 1
    if from >= Config.SCHEMA_VERSION then return false end
    for v = from + 1, Config.SCHEMA_VERSION do
        local fn = Config.MIGRATIONS[v]
        local okCall, n = pcall(function() return fn and fn(db) or 0 end)
        if not okCall then
            db.schema = v - 1          -- 停在成功的那一档，下次登录重试同一档
            Config.Log(("数据升级失败 v%d（下次登录重试）: %s"):format(v, tostring(n)))
            return false
        end
        Config.Log(("数据升级 v%d 完成（处理 %s 条）"):format(v, tostring(n)))
    end
    db.schema = Config.SCHEMA_VERSION
    return true
end

function Config.Init()
    if type(GQLDB) ~= "table" then GQLDB = {} end
    local db = GQLDB
    db.quests = db.quests or {}
    db.chars = db.chars or {}
    db.turnins = db.turnins or {}
    db.manual = db.manual or {}
    db.logs = db.logs or {}
    -- 配置：跟踪地图 + 关键词（替代代码写死的地图白名单）
    db.config = db.config or {}
    db.config.extraMaps = db.config.extraMaps or {}   -- { [mapID] = true } 用户额外关注的地图
    db.config.keywords = db.config.keywords or {}     -- { "关键词", ... } 命中即纳入矩阵
    -- 金币门槛（单位=金，不是铜）：12.1.5 第 15 轮反馈"出来了一堆狩猎的 >100g 的已做任务，
    -- 门槛就要提到 500g 或者可以配置" —— 写死 100g 时用户只能看着一屏不值得刷的行，
    -- 所以门槛是配置项，默认提到 500g。
    if type(db.config.minGold) ~= "number" then db.config.minGold = DEFAULT_MIN_GOLD end
    -- 登录抓一次池（用户 2026-10-25 定：默认开、可关）。代价照 Sync 注释讲清：
    -- 一次登录一趟快照，只问跟踪清单里的图，不是整棵地图树；关上面板后仍然完全静默。
    if db.config.syncOnLogin == nil then db.config.syncOnLogin = true end
    -- 先把档位补齐到"已知最低"（老到什么字段都没有的存档走这里），再跑升级。
    if (tonumber(db.schema) or 0) < 1 then db.schema = 1 end
    if not db.cycleAnchor or db.cycleAnchor == 0 then
        Config.RefreshCycle()
        db.cycleAnchor = db.cycleAnchor or 0
    end
    -- 一次性升级：这里跑，不在每轮快照里跑（那等于每次同步多一趟全账本扫描）
    Config.Migrate(db)
    Config.Log(("账本就绪 schema=%d cycleAnchor=%d"):format(db.schema, db.cycleAnchor))
end

--- 地图来源标签（写进 db.quests[id].mapSrc 的那串字）。唯一出处：这些串决定
--- 分组（Ledger.RowGroupMapID）、族搬运（TRUSTED 白名单）、清理（ClearRetiredMaps
--- 按子串认）、周常归组（ClientMapOf 的黑名单），散成裸字符串打错一个字就静默失效。
--- 放 Config 是因为它在加载顺序里早于所有使用方，且纯字符串、不碰接口。
--- 值就是玩家与 /gql probe 看到的证据标签，改一个字等于改判据，不许顺手"优化措辞"。
Config.MAP_SRC = {
    pool = "池行自带",                       -- 池行自己报的图：客户端直答，最硬
    boardList = "跟踪地图清单",
    boardReverse = "地图任务清单反查",
    bountyReverse = "悬赏清单反查",
    logZone = "日志分区",
    pin = "地图事件 pin",                     -- 地图 pin 上自己挂着 questID 的那批
    manual = "玩家指定",                      -- 第十七轮口述兜底，不当客户端凭据
    legacy = "旧记录未标来源",
    suspect = "存疑：谎报接口写的",
    asked = "询问用的图兜底",                  -- "我问了哪张图"≠"这任务在哪张图"
    healPrefix = "补问 ",                     -- 后接接口全名
    familyPrefix = "同族成员 ",                -- 后接成员 questID
}

-- ── 配置：跟踪地图 + 关键词 ──
-- 替代代码写死的地图白名单：默认=本资料片区域(Api.MidnightMapIDs)，玩家可在设置面板
-- 额外关注自己的地图；并可配置关键词，名字含关键词的世界任务绕过金币门槛进矩阵。

--- 矩阵准入门槛（铜）。设置面板存的是"金"，这里换算并夹到合法区间。
--- 导出成函数而不是常量：门槛一旦可配，Dump/Probe/矩阵各自抄一份数字就必然对不上，
--- 而"这条为什么没进矩阵"的整份取证都靠这个数。
function Config.MinMatrixMoney()
    local db = Config.DB()
    local g = tonumber(db and db.config and db.config.minGold)
    if not g then g = DEFAULT_MIN_GOLD end
    if g < 0 then g = 0 elseif g > 100000 then g = 100000 end
    return math.floor(g * 10000)
end

--- 设置面板/斜杠命令改门槛：只认数字，四舍五入到整数金。返回 (是否生效, 现在的金数)。
function Config.SetMinGold(gold)
    local n = tonumber(gold)
    if not n then return false, "不是数字" end
    n = math.floor(n + 0.5)
    if n < 0 then n = 0 elseif n > 100000 then n = 100000 end
    local db = Config.DB()
    if not db then return false, "账本未就绪" end
    db.config = db.config or {}
    db.config.minGold = n
    return true, n
end

--- 登录要不要抓一次池。缺省、以及"配置表还没建起来"（老存档、账本没就绪）都按开处理：
--- 只有玩家明确关过（false）才关。宁可按默认走，也不让 nil 悄悄变成"关"，
--- 那会让用户以为插件坏了（矩阵永远是上个 CD 的旧板面）。
function Config.SyncOnLogin()
    local db = Config.DB()
    local c = db and db.config
    if not c then return true end
    return c.syncOnLogin ~= false
end

function Config.SetSyncOnLogin(v)
    local db = Config.DB()
    if not db then return false end
    db.config = db.config or {}
    db.config.syncOnLogin = not not v
    return true
end

function Config.GetMinGold()
    local db = Config.DB()
    return tonumber(db and db.config and db.config.minGold) or DEFAULT_MIN_GOLD
end

function Config.GetKeywords()
    local db = Config.DB()
    return (db and db.config and db.config.keywords) or {}
end

--- 任务名命中了哪个关键词（大小写不敏感子串）；没命中返回 nil。
--- 命中词要能报出来，才知道"这条为什么进矩阵/那条为什么没进" ——
--- 判"命中没命中"只认这一个入口，KeywordMatch 是它的布尔壳。
function Config.KeywordHit(name)
    if type(name) ~= "string" or name == "" then return nil end
    local kws = Config.GetKeywords()
    if #kws == 0 then return nil end
    local low = name:lower()
    for i = 1, #kws do
        local k = tostring(kws[i]):lower()
        if k ~= "" and low:find(k, 1, true) then return tostring(kws[i]) end
    end
    return nil
end

--- 任务名是否命中任一关键词。无关键词时恒 false。
function Config.KeywordMatch(name)
    return Config.KeywordHit(name) ~= nil
end

function Config.AddKeyword(k)
    k = (type(k) == "string" and k:match("^%s*(.-)%s*$")) or ""
    if k == "" then return false, "关键词为空" end
    local db = Config.DB()
    if not db then return false, "账本未就绪" end
    db.config = db.config or {}
    db.config.keywords = db.config.keywords or {}
    for i = 1, #db.config.keywords do
        if db.config.keywords[i] == k then return false, "已存在" end
    end
    db.config.keywords[#db.config.keywords + 1] = k
    return true, "已添加"
end

function Config.RemoveKeyword(k)
    local db = Config.DB()
    if not db or not db.config or not db.config.keywords then return false end
    local kws = db.config.keywords
    for i = 1, #kws do
        if kws[i] == k then
            table.remove(kws, i)
            return true
        end
    end
    return false
end

--- 实际要抓池的地图：默认(本资料片 zones) ∪ 用户额外关注。
--- 去重：默认清单里那张图同时被写进 extraMaps 时（补扫命中行报的地图正好是默认图，
--- 或用户自己加了本资料片的区域）只留一份，否则按这张清单循环的调用方会把同一张图问两遍。
--- 返回 nil = 一套地图都没有（调用方退回全图枚举，宁多勿漏）。
function Config.TrackedMapIDs()
    local base = (GQL.Api and GQL.Api.MidnightMapIDs) and GQL.Api.MidnightMapIDs() or nil
    local db = Config.DB()
    local extra = (db and db.config and db.config.extraMaps) or nil
    local hasExtra = type(extra) == "table" and next(extra) ~= nil
    if type(base) ~= "table" then
        if not hasExtra then return nil end
        local out = {}
        for id in pairs(extra) do out[#out + 1] = tonumber(id) end
        return out
    end
    if not hasExtra then return base end
    local out, seen = {}, {}
    for i = 1, #base do
        local id = tonumber(base[i])
        if id and not seen[id] then seen[id] = true out[#out + 1] = id end
    end
    for k in pairs(extra) do
        local id2 = tonumber(k)
        if id2 and not seen[id2] then seen[id2] = true out[#out + 1] = id2 end
    end
    return out
end

function Config.AddMap(mapID)
    mapID = tonumber(mapID)
    if not mapID or mapID <= 0 then return false, "地图 ID 无效" end
    local db = Config.DB()
    if not db then return false, "账本未就绪" end
    db.config = db.config or {}
    db.config.extraMaps = db.config.extraMaps or {}
    if db.config.extraMaps[mapID] then return false, "已在跟踪列表" end
    db.config.extraMaps[mapID] = true
    return true, "已添加"
end

function Config.RemoveMap(mapID)
    mapID = tonumber(mapID)
    if not mapID then return false end
    local db = Config.DB()
    if not db or not db.config or not db.config.extraMaps then return false end
    db.config.extraMaps[mapID] = nil
    return true
end

--- 按名字模糊匹配地图并加入跟踪（设置面板"按名称添加"用）。
--- 遍历全图(WQMapList)取地名做大小写不敏感子串匹配，命中即加。
function Config.AddMapByName(fragment)
    fragment = (type(fragment) == "string" and fragment:match("^%s*(.-)%s*$")) or ""
    if fragment == "" then return false, "请输入地图名" end
    local list = (GQL.Api and GQL.Api.WQMapList and GQL.Api.WQMapList()) or nil
    if not list then return false, "地图列表不可用" end
    local low = fragment:lower()
    local added = 0
    for i = 1, #list do
        local name = GQL.Api.MapName and GQL.Api.MapName(list[i])
        if type(name) == "string" and name:lower():find(low, 1, true) then
            if Config.AddMap(list[i]) then added = added + 1 end
        end
    end
    if added > 0 then return true, ("已添加 %d 张地图"):format(added) end
    return false, "没找到匹配的地图"
end
