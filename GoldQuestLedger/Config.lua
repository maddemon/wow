-- 账本数据层：SavedVariables 建库、周 CD 锚点、诊断日志环。
-- 数据结构（GQLDB，account-wide）：
--   schema = 11                        -- 存档结构档位：写了也读（Config.Migrate 按档跑一次性升级）
--   cycleAnchor = <本 CD 起点时间戳>
--   clockDomain = "epoch" | "uptime"   -- 时间戳存的是哪个时钟域，uptime 跨开机归零
--   stickyGoldReset = 2                -- 一次性迁移的标记（分档，见下）：老存档里被旧代码写坏的
--                                       -- "本轮读到过金币"已作废（Ledger.ResetStickyGoldOnce）。
--                                       -- 1 = 只清了粘滞那一半，被污染的 observation 还会把它长回来
--   quests[questID]     = { name, mapID, lastSeenAt, currentSig, highFreq, goneAt,
--                           roundAt=<老"本轮起点"，判据不读，只剩 v6 迁移取证>,
--                           roundEnd=<这一条实例自己的结束时刻，唯一写点 Ledger.SetExpiry>,
--                           goldMoney/goldRound=<本轮读到过的金币与它属于哪一轮>,
--                           goldKey=<同一笔钱所属实例的唯一编号（读者 T4 已上线）>,
--                           ignored=<玩家在"关键词 · 屏蔽管理"点"忽略" = 永久屏蔽，只有点"取消忽略"才撤销>
--                           observations[sig] = { gold, money, items, firstSeen, lastSeen, goneSince,
--                                                 key=<这一副奖励属于哪条实例，见 Ledger.NoteGoldThisInstance> } }
--                           （老"本轮起点"的判据机器 RoundAt/L.RoundKey 已整块删除（§五），
--                            身份一律问编号 k；老那一组到期字段 expiresAt/expiresSrc/
--                            expiresGuessed 连同 24 小时排程值随 T5 整组退役——无写点无读者，
--                            老存档里的残留原地留着，谁也不读它）
--                           （manualAdded 这一档 2026-10-27 整体下线：点"添加"只是撤销忽略，
--                            不再是矩阵的准入门。老存档残留由升级档 v4/v5 作废）
--                           （admittedKey 这一档 2026-10-29 第 39 轮整体下线：准入门只剩
--                            "玩家点名"与"奖励金币≥门槛"两类依据，"本轮交过"不作数、
--                            上过板也不再粘滞占行。老存档残留由升级档 v11 清）
--   chars[角色-服务器]   = { class, level, lastSync, views[questID] = { s, t, k },
--                            reads[questID] = { g=<这个号自己读到的金币>, t, k } }
--                            （reads 是"奖励按号各不同"的凭据：同 questID 一个号给金币、
--                             另一个号给物品，见 Ledger.SetRewardRead。
--                             g 三档语义：正数=这号读到给这么多；0=客户端明说这号做它不给
--                             金币；**没有这一笔**=这号在这条实例上从没读到过（≠不给金币）。
--                             2026-11-03 起同一 charKey + 同一 k 内正数不被新的 0 冲掉
--                             （切狩猎模式那一次同步读到 0，不许把普通模式读到的真数抹成
--                             "不给金币"；行级那份保护在 NoteGoldThisInstance，号级在
--                             SetRewardRead，形状一样），换 k 照旧整笔重写，不升 schema）
--   turnins[角色][任务]  = { { t, sig, cycle, k }, ... }   -- 1-based 追加日志
--   manual[角色][任务]   = { state, t, g, v, round, k }
--   k = 第 36 轮"任务号 + 结束时刻"的唯一编号（Ledger.InstanceKey，判不出身份时不写 = nil）；
--       round = 挂在"本轮起点"上的旧身份标记，写侧已停、读侧 T4 起全部换成认 k，只剩老档残留。
--       老存档里那些**没带 k** 的凭据由升级档 v6 一次性补上（起点重建不出来的不补 = 永不匹配）。
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
Config.SCHEMA_VERSION = 11
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
    -- v6：给老存档里**没烙过编号**的凭据（交任务流水 / 手动补录 / 板上快照 / 按号读数）一次性
    -- 补上「任务号 + 结束时刻」的唯一编号（用户 2026-10-28："旧的数据如果没存，都要修改"）。
    -- 这一档排在读侧切换之前跑：§五 删掉"没键才退回时间戳"那一支之后，没编号 = 永不匹配，
    -- 先换门会把真档 20 行里 19 行的好勾连同 94612 的 10 笔假勾一起清光。
    -- 只烙不删、数据一条不动（起点重建不出来的那几笔判为挂不上，代价与判据见
    -- Ledger.BackfillInstanceKeysOnce 的注释）。返回值 = 烙上的笔数。
    [6] = function(db) return GQL.Ledger and GQL.Ledger.BackfillInstanceKeysOnce
        and GQL.Ledger.BackfillInstanceKeysOnce(db) or 0 end,
    -- v7：清掉老 expiresAt 那组的遗产（用户 2026-10-28："哪些 expiresAt 的老任务是不是
    -- 早就到期了，那就全部删除吧"）。T4a 换门后新模型不认这组字段，真档 218/310 条
    -- 过期值全成了"周 CD 尾巴"复活的燃料。已过期且无注册/日志/屏蔽凭据的记录整条删除；
    -- 未过期且没有 roundEnd 的一次性转成 roundEnd（新模型唯一真源）。判据、保留口径与
    -- 代价见 Ledger.PurgeExpiredLegacyOnce 的注释。返回值 = 删除的条数。
    [7] = function(db) return GQL.Ledger and GQL.Ledger.PurgeExpiredLegacyOnce
        and GQL.Ledger.PurgeExpiredLegacyOnce(db) or 0 end,
    -- v8：清掉老存档里**玩家口述的地图**（用户 2026-10-29："口述这一块直接去掉…
    -- 等下个资料片，就成垃圾代码片段了"）。口述机制（Api.MAP_MEMORY / SetManualMap /
    -- Sync.ApplyMapMemory）同轮整门下线，写入点已删；这档把已经写进 SavedVariables 的
    -- mapID/mapSrc=玩家指定 一起清，残留的 mapManual 标记也抹掉（客户端来源的图保留）。
    -- 判据、保留口径与代价见 Ledger.ClearManualMaps 的注释。返回值 = 清掉的地图条数。
    [8] = function(db) return GQL.Ledger and GQL.Ledger.ClearManualMaps
        and GQL.Ledger.ClearManualMaps(db) or 0 end,
    -- v9：清掉老存档里**没有本轮流水背书的 done 快照**（健步如飞 ×14 假绿案，
    -- 用户 2026-10-29 批准"两刀"）。老代码把客户端的完成 flag 残留直接写进快照
    -- （固定 ID 任务跨轮不清 flag），凭空续命出"已交付"的绿勾；快照写入口已加
    -- "编号对上的本轮流水"门（Sync 池循环），这档把存档里躺着的那批清掉，
    -- 让 evidence 梯子降级回 unknown，FinishWindow 结算后自愈。
    -- 判据、保留口径与代价见 Ledger.DropUnbackedDoneViews 的注释。返回值 = 清掉的快照条数。
    [9] = function(db) return GQL.Ledger and GQL.Ledger.DropUnbackedDoneViews
        and GQL.Ledger.DropUnbackedDoneViews(db) or 0 end,
    -- v10：给老存档里**只挂着 kwLogAdded 布尔位**的那几条补上"这一条实例的日志凭据"编号
    -- （梦魇任务 94446 那一单，用户 2026-10-29："只要有一个号接了…不管是不是金币任务，
    -- 都要上矩阵"）。代码改完存档不自愈：更新完第一次登录若用的是"没接这条的号"，Sync
    -- 收尾那道当场把布尔位撤走，而这条记录没有第二份凭据 ⇒ 整行对全账号消失（= 用户会
    -- 读成"改完一点变化没有"）。这一档一次性烙上**当前**编号（周 CD 尾巴档，下周重置
    -- 自己作废，不需要清理代码）。只烙不删、只认 kwLogAdded 且没烙过的（幂等）、
    -- 不给任何一格凭空打勾。判据与代价见 Ledger.StampKeywordLogKeysOnce 的注释。
    -- 返回值 = 烙上的笔数。
    [10] = function(db) return GQL.Ledger and GQL.Ledger.StampKeywordLogKeysOnce
        and GQL.Ledger.StampKeywordLogKeysOnce(db) or 0 end,
    -- v11：清掉老存档里的**准入粘滞 admittedKey**（狩猎：坚韧猎人 91594 那一单，
    -- 用户 2026-10-29 定稿"什么时候也没说做过就是准入门槛。1、匹配关键词的任务
    -- 2、金币奖励满足的任务"）。判据那侧"本轮交过"整支、以及跟它组成"或"的粘滞都删了，
    -- 但已经烙进存档的那一份不自愈 ⇒ 不清的话 91594 还照样留板到它自己倒计时走完，
    -- 用户读成"改完一点变化没有"（v2/v6/v7/v9/v10 同一族教训）。
    -- 只清 admittedKey：金币粘滞 goldMoney/goldKey（双模式不掉行靠它）、日志凭据 kwLogKey、
    -- 角色快照与流水一字不碰。判据、保留口径与代价见 Ledger.ClearAdmittedKeysOnce 的注释。
    -- 返回值 = 清掉的笔数。
    [11] = function(db) return GQL.Ledger and GQL.Ledger.ClearAdmittedKeysOnce
        and GQL.Ledger.ClearAdmittedKeysOnce(db) or 0 end,
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
    local bad = {}
    local function ensure(parent, key)
        local v = parent[key]
        if type(v) ~= "table" then
            if v ~= nil then bad[#bad + 1] = key end
            parent[key] = {}
        end
    end
    ensure(db, "quests")
    ensure(db, "chars")
    ensure(db, "turnins")
    ensure(db, "manual")
    ensure(db, "logs")
    -- 配置：跟踪地图 + 关键词（替代代码写死的地图白名单）
    ensure(db, "config")
    ensure(db.config, "extraMaps")   -- { [mapID] = true } 用户额外关注的地图
    ensure(db.config, "keywords")    -- { "关键词", ... } 命中即纳入矩阵
    -- 已套过的默认配置包（Defaults.lua，key = 版本模式#rev）：一个包只套一次，
    -- 玩家删掉的默认地图/关键词不会自己长回来（口径与代价写在 Defaults.lua 文件头）
    ensure(db.config, "defaults")
    -- 金币门槛（单位=金，不是铜）：12.1.5 第 15 轮反馈"出来了一堆狩猎的 >100g 的已做任务，
    -- 门槛就要提到 500g 或者可以配置" —— 写死 100g 时用户只能看着一屏不值得刷的行，
    -- 所以门槛是配置项，默认提到 500g。
    if type(db.config.minGold) ~= "number" then db.config.minGold = DEFAULT_MIN_GOLD end
    -- 登录抓一次池（用户 2026-10-25 定：默认开、可关）。代价照 Sync 注释讲清：
    -- 一次登录一趟快照，只问跟踪清单里的图，不是整棵地图树；关上面板后仍然完全静默。
    if db.config.syncOnLogin == nil then db.config.syncOnLogin = true end
    -- 0.6 T4 两个显示开关（计划 docs/tasks/2026-11-08-0.6-matrix-layout.md 阶段二）：
    -- 默认都关 = 今天的 0.5.5 版面。这里显式写 false 是第一道保险；第二道在 getter：
    -- 只认真 true（见 Config.HideAllDone 那段"为什么不许照抄 syncOnLogin"），
    -- 兜住的是 Init 没跑过的路径（老存档、账本没就绪、config 表压根没建起来）。
    -- 方向与上面 syncOnLogin 的"缺省按开"相反，抄不得：那会让老存档（字段是 nil）
    -- 的这两个开关悄悄变成默认开 = 悄悄翻转用户用惯的界面（计划 T4 点名的坑）。
    if db.config.hideAllDone == nil then db.config.hideAllDone = false end
    if db.config.charsInRows == nil then db.config.charsInRows = false end
    -- 先把档位补齐到"已知最低"（老到什么字段都没有的存档走这里），再跑升级。
    if (tonumber(db.schema) or 0) < 1 then db.schema = 1 end
    if not db.cycleAnchor or db.cycleAnchor == 0 then
        Config.RefreshCycle()
        db.cycleAnchor = db.cycleAnchor or 0
    end
    -- 一次性升级：这里跑，不在每轮快照里跑（那等于每次同步多一趟全账本扫描）
    Config.Migrate(db)
    -- 默认配置包（地图 + 关键词，按客户端版本匹配，见 Defaults.lua）。放在升级之后：
    -- 包里加地图会连带写下子图清单，得先让账本处在最新形状。
    -- 地图树在登录早期可能还问不出来，那时这一趟只落关键词、不记"套过"，
    -- 由 Sync.OnWorldEnter 再试（幂等，见 Defaults.Apply）。
    if GQL.Defaults and GQL.Defaults.Apply then
        local okDef, err = pcall(GQL.Defaults.Apply, db)
        if not okDef then Config.Log("默认配置包出错: " .. tostring(err)) end
    end
    if #bad > 0 then
        Config.Log("存档字段类型损坏已重建: " .. table.concat(bad, ","))
    end
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
    manual = "玩家指定",                      -- 已退役（2026-10-29 口述机制下线）：只剩 v8 迁移按它认老存档残留
    legacy = "旧记录未标来源",
    suspect = "存疑：谎报接口写的",
    asked = "询问用的图兜底",                  -- "我问了哪张图"≠"这任务在哪张图"
    healPrefix = "补问 ",                     -- 后接接口全名
    familyPrefix = "同族成员 ",                -- 后接成员 questID
}

-- ── 配置：跟踪地图 + 关键词 ──
-- 关注地图只有玩家自己加的图（2026-10-31 起"默认=本资料片区域"那套撤了：
-- 默认图四十来张列满面版，子图归属也追不上 —— 用户原话"移除所有默认地图，
-- 改用用户添加的地图"）；玩家可在设置面板添加/移除，加图时子图自动记住
-- （db.config.subMaps，不进面板展示）。并可配置关键词，名字含关键词的
-- 世界任务绕过金币门槛进矩阵。

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

--- 0.6 T4 显示开关（都只影响界面，不进任何判据）。
--- 【为什么 getter 用 `== true` 而不是照抄 SyncOnLogin 的 `~= false`】：
--- `~= false` 的语义是"默认开"（nil 也算开），SyncOnLogin 要的就是这个方向；
--- 这两个开关要的是"默认关"——老存档字段是 nil、账本没就绪、config 表压根没建起来，
--- 三种缺表形状一律必须回 false，照抄就会把用户用惯的版面在升级那天悄悄翻转
--- （计划 2026-11-08 T4 点名"照抄是个坑"）。Init 里显式写 false 是第一道保险，
--- 管正常路径；这句 `== true` 是第二道，兜住压根没走 Init 的形状 —— 真正定方向的还是它。
--- 显示层唯一的读点是 MatrixView（取证面 AdmitOnly/DoneHidden 与渲染共用同一句），
--- 数据侧（准入门/排序/NextRefreshAt）永远不许读这两个开关。
function Config.HideAllDone()
    local db = Config.DB()
    local c = db and db.config
    if not c then return false end
    return c.hideAllDone == true
end

function Config.SetHideAllDone(v)
    local db = Config.DB()
    if not db then return false end
    db.config = db.config or {}
    db.config.hideAllDone = not not v
    return true
end

--- 角色竖排开关（0.6 T6 才消费这颗，本轮只把字段与读口备好，默认关=横排现状）。
--- getter/setter 形状与上面 HideAllDone 完全同一坑、同一修法（`== true`，缺表 false）。
function Config.CharsInRows()
    local db = Config.DB()
    local c = db and db.config
    if not c then return false end
    return c.charsInRows == true
end

function Config.SetCharsInRows(v)
    local db = Config.DB()
    if not db then return false end
    db.config = db.config or {}
    db.config.charsInRows = not not v
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

--- 纯文本进 FontString / 聊天框前把 | 转义：|c / |H / |T 会被客户端当
--- 颜色码 / 链接 / 纹理解析，玩家自己加的关键词不该改写 UI 或伪造链接。
function Config.EscapeUI(s)
    return (tostring(s or ""):gsub("|", "||"))
end

function Config.AddKeyword(k)
    k = (type(k) == "string" and k:match("^%s*(.-)%s*$")) or ""
    if k == "" then return false, "关键词为空" end
    if #k > 64 then return false, "关键词太长（上限 64 字）" end
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

--- 关注地图清单 = 玩家自己加的图 ∪ 加图时自动记住的子图（子图不进面板展示）。
--- 2026-10-31 起**没有默认清单**（原先并上 Api.MidnightMapIDs 的那半撤了）。
--- 空清单返回 nil 而不是空表：调用方各自有"没清单时的降级路"（池回脚下大陆、
--- 扫描跳过），而空清单在池那路会被当成"真·空池"把整轮轮次毁掉。
function Config.TrackedMapIDs()
    local db = Config.DB()
    local cfg = db and db.config or nil
    local extra = cfg and cfg.extraMaps or nil
    local subs = cfg and cfg.subMaps or nil
    if not ((extra and next(extra)) or (subs and next(subs))) then return nil end
    local out, seen = {}, {}
    local function push(id)
        id = tonumber(id)
        if id and id > 0 and not seen[id] then
            seen[id] = true
            out[#out + 1] = id
        end
    end
    for id in pairs(extra or {}) do push(id) end
    for id in pairs(subs or {}) do push(id) end
    return out
end

function Config.AddMap(mapID)
    mapID = tonumber(mapID)
    if not mapID or mapID <= 0 then return false, "地图 ID 无效" end
    local db = Config.DB()
    if not db then return false, "账本未就绪" end
    db.config = db.config or {}
    db.config.extraMaps = db.config.extraMaps or {}
    db.config.subMaps = db.config.subMaps or {}
    local added = not db.config.extraMaps[mapID]
    db.config.extraMaps[mapID] = true
    -- 子树整棵记下（向下递归 C_Map.GetMapChildrenInfo，Api.MapDescendants）：
    -- 之后的扫描清单把子图也带上（板面行/事件 pin 常挂在子图上，2026-10-31 实测
    -- "暗影汇聚之地"挂 2444 屠戮者高地），但面板不展示、归属滚动到玩家加的这张
    -- （Ledger.RowGroupMapID → Api.TrackedOwnerMapID）。已加过的图也刷新一遍：
    -- 版本新加的子图补进来（判定另有父图链兜底，这里补的是扫描范围）。
    if GQL.Api and GQL.Api.MapDescendants then
        local subs = GQL.Api.MapDescendants(mapID)
        if subs then
            for i = 1, #subs do db.config.subMaps[subs[i]] = mapID end
        end
    end
    -- 清单变了：判定/归属的会话缓存必须清（否则刚加的图拿旧结论卡着）
    if GQL.Api and GQL.Api.ResetTrackedMapCache then GQL.Api.ResetTrackedMapCache() end
    if not added then return false, "已在跟踪列表（子图清单已刷新）" end
    return true, "已添加"
end

function Config.RemoveMap(mapID)
    mapID = tonumber(mapID)
    if not mapID then return false end
    local db = Config.DB()
    if not db or not db.config or not db.config.extraMaps then return false end
    db.config.extraMaps[mapID] = nil
    -- 它名下自动记的子图一并退掉：清单 = 玩家加的图 + 它们的子图，父图没了
    -- 子图没理由留（子图是跟着父图进来的，不是玩家点名的）。
    if db.config.subMaps then
        for id, owner in pairs(db.config.subMaps) do
            if owner == mapID then db.config.subMaps[id] = nil end
        end
    end
    if GQL.Api and GQL.Api.ResetTrackedMapCache then GQL.Api.ResetTrackedMapCache() end
    return true
end

--- 按地图名加入跟踪（设置面板"按名称添加"与默认配置包 ApplyPack 共用）。
--- 匹配收紧口径（T14）：先**全等**（大小写不敏感），全等命中唯一一张才收；
--- 全等命中多张或零张才退到子串（全等本来也是子串的一种，两计数一起攒），
--- 子串命中多张**不收** —— 宁可不收，也不悄悄多收：默认包带 rev 记账，收错的图
--- 不会自动退，玩家删起来才知道来路（旧版子串扫全图，"祖阿曼"这类名字能同时
--- 扫进副本/同名旧图就是这病）。
--- 返回 ok, msg, added, already, reason：msg 一律给玩家读；ok=true 表示这个名字
--- 有确定归宿 —— 收进了一张图（added=1）或它本来就在跟踪里（already=1，幂等，
--- AddMap 顺带刷的子树照旧在）。reason 只在没收下时给："ambiguous" 命中多张、
--- "notfound" 一张没中、"unavailable" 图清单问不出、"addfail" 命中的图写不进账本 ——
--- ApplyPack 按这四个类别把日志如实分开，不许把"已在跟踪"混进"没匹配到"。
function Config.AddMapByName(fragment)
    fragment = (type(fragment) == "string" and fragment:match("^%s*(.-)%s*$")) or ""
    if fragment == "" then return false, "请输入地图名", 0, 0 end
    local list = (GQL.Api and GQL.Api.WQMapList and GQL.Api.WQMapList()) or nil
    if not list then return false, "地图列表不可用", 0, 0, "unavailable" end
    local low = fragment:lower()
    local eqId, eqN, subId, subN = nil, 0, nil, 0
    for i = 1, #list do
        local name = GQL.Api.MapName and GQL.Api.MapName(list[i])
        if type(name) == "string" then
            local lname = name:lower()
            if lname == low then eqN = eqN + 1; eqId = list[i] end
            if lname:find(low, 1, true) then subN = subN + 1; subId = list[i] end
        end
    end
    local hit
    if eqN == 1 then hit = eqId
    elseif subN == 1 then hit = subId end
    if not hit then
        local ambiguous = eqN > 1 or subN > 1
        return false, ambiguous
            and "这个名字同时命中几张图，不能替你选一张，请用完整图名"
            or "没找到匹配的地图", 0, 0, ambiguous and "ambiguous" or "notfound"
    end
    local db = Config.DB()
    local was = db and db.config and db.config.extraMaps
        and db.config.extraMaps[hit] ~= nil
    local okAdd, msgAdd = Config.AddMap(hit)
    if okAdd then return true, "已添加 1 张地图", 1, 0 end
    if was then return true, "已在跟踪列表", 0, 1 end
    return false, msgAdd or "这张图没能加入", 0, 0, "addfail"
end
