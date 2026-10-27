-- /gql probe：把 12.x 候选接口的实测结果输出成可复制文本。
-- 目的不是给用户看，是把"接口存不存在、返回什么形状"变成实证：
-- 阶段二的 UI 只允许接在这里验证过的入口。

GQL.Probe = {}
local Probe = GQL.Probe

local function Describe(v, depth)
    depth = depth or 0
    if type(v) ~= "table" then return string.format("%s(%s)", tostring(v), type(v)) end
    if depth >= 2 then return "table..." end
    local keys, n = {}, 0
    for k in pairs(v) do
        n = n + 1
        if n <= 12 then keys[#keys + 1] = tostring(k) end
    end
    table.sort(keys)
    return string.format("table[#%d]{%s}", n, table.concat(keys, ","))
end

-- GetBuildInfo 各个返回值哪个才是版本号没有实测依据，所以整行原样打出来，
-- 不在这里断言第几个是接口版本（报告是用来取证据的，不是用来猜的）。
local function BuildInfo()
    if not GetBuildInfo then return "?" end
    local okCall, v1, v2, v3, v4, v5, v6 = pcall(GetBuildInfo)
    if not okCall then return "error: " .. tostring(v1) end
    local parts = {}
    local vals = { v1, v2, v3, v4, v5, v6 }
    -- 按 1..6 取值而不是 ipairs：ipairs 会在第一个 nil 处停下，把后面的版本信息整段丢掉
    for i = 1, 6 do
        if vals[i] ~= nil then parts[#parts + 1] = tostring(vals[i]) end
    end
    if #parts == 0 then return "?" end
    return table.concat(parts, " ")
end

-- 候选名全部 MISSING 时不该再猜下一个名字，直接问游戏要：
-- 枚举已知命名表的成员 + 扫 _G 里带 WorldQuest 的名字。
-- 两条都跑是因为 WoW 的 API 未必挂在可迭代的 _G 上（_G 可能只有帧名），
-- 所以把 "_G成员数" 也如实打出来 —— 扫到 0 个和根本没得扫是两个结论。
local NAMESPACES = {
    "C_WorldQuests", "C_WorldQuestDB", "C_WorldQuestFrame", "C_WorldQuestUtils",
    "C_WorldQuestUtilities", "C_QuestLog", "C_QuestRewards", "C_QuestProgress",
    -- C_TaskQuest 是世界地图 pin 数据的实际来源（GetQuestsOnMap 实测在此），
    -- 上一轮没枚举它是个盲区：倒计时/奖励的真正入口十有八九就在这个表里
    "C_TaskQuest",
    -- 上一轮实测池接口都要 mapID，可枚举地图的入口只可能在这里
    "C_Map", "C_WorldMap",
}
-- mixin 是帧脚本的类定义表（_G 里能扫到），成员名 = 数据提供者真正调用的接口清单，
-- 照着它去 C_TaskQuest/C_QuestLog 里对号，比猜名字强得多。
local MIXIN_NODES = {
    "WorldMap_WorldQuestDataProviderMixin",
    "WorldQuestPinMixin",
}
local NAME_HINTS = { "WorldQuest", "Bounty", "GetQuestLogReward", "TimeLeft" }
local MAX_NAMES = 60

local function MemberNames(node)
    local names, count = {}, 0
    for k in pairs(node) do
        count = count + 1
        if type(k) == "string" then names[#names + 1] = k end
    end
    table.sort(names)
    if #names > MAX_NAMES then
        for i = #names, MAX_NAMES + 1, -1 do names[i] = nil end
    end
    return names, count
end

local function DiscoveryLines()
    local out = { "---- 接口发现" }
    for i = 1, #NAMESPACES do
        local node = GQL.Api.Resolve(NAMESPACES[i])
        if type(node) == "table" then
            local names, count = MemberNames(node)
            out[#out + 1] = ("%s 成员数=%d 方法=%s"):format(NAMESPACES[i], count, table.concat(names, ","))
        else
            out[#out + 1] = ("%s 不存在"):format(NAMESPACES[i])
        end
    end
    -- 数据提供者/图钉的 mixin：pin 上的奖励图标和倒计时是它们画出来的，
    -- 方法清单就是"这份数据从哪些接口来"的目录
    for i = 1, #MIXIN_NODES do
        local okNode, node = pcall(function() return _G[MIXIN_NODES[i]] end)
        if okNode and type(node) == "table" then
            local names = select(1, MemberNames(node))
            out[#out + 1] = ("%s 方法=%s"):format(MIXIN_NODES[i], table.concat(names, ","))
        else
            out[#out + 1] = ("%s 不存在"):format(MIXIN_NODES[i])
        end
    end
    -- 一趟 _G 扫完所有提示词：这棵树有 5 万多个成员，每加一个词就多扫一遍不值
    local hits, total = {}, 0
    local counts = {}
    for i = 1, #NAME_HINTS do counts[NAME_HINTS[i]] = 0 end
    for k in pairs(_G) do
        total = total + 1
        if type(k) == "string" then
            for i = 1, #NAME_HINTS do
                local hint = NAME_HINTS[i]
                if k:find(hint, 1, true) and counts[hint] < MAX_NAMES then
                    counts[hint] = counts[hint] + 1
                    hits[hint] = hits[hint] or {}
                    hits[hint][#hits[hint] + 1] = k
                end
            end
        end
    end
    for i = 1, #NAME_HINTS do
        local hint = NAME_HINTS[i]
        local list = hits[hint] or {}
        table.sort(list)
        out[#out + 1] = ("_G成员数=%d 命中 %s=%d: %s"):format(total, hint, #list, table.concat(list, ","))
    end
    return out
end

-- 带 questID 的候选得给个真参数才问得出形状。账本里有任务就用真的，
-- 没有就填 0 占位 —— 对只读接口来说 0 只会得到 false/nil，不会改动画像。
local function SampleQuestID()
    local db = GQL.Config.DB()
    if not db then return 0 end
    local okCall, ids = pcall(function() return GQL.Ledger.GoldQuestIDs(db) end)
    if okCall and ids and #ids > 0 then return tonumber(ids[1]) or 0 end
    local questID = db.quests and next(db.quests)
    return tonumber(questID) or 0
end

-- mapID 拿不到就是拿不到：填 0 会让 "bad argument #1" 冒充成接口可用性问题，
-- 上一轮报告里那一整行 ERROR 就是这么来的。宁可明写跳过，也不产出废证据。
local function SampleMapID()
    local okCall, v = pcall(GQL.Api.CurrentMapID)
    if okCall and v then return v end
    local db = GQL.Config.DB()
    if db and db.quests then
        for _, rec in pairs(db.quests) do
            local mapID = tonumber(rec.mapID)
            if mapID and mapID > 0 then return mapID end
        end
    end
    return nil
end

-- 日志索引类候选（GetQuestLogZoneMapID）要的是索引不是 questID：填 0 只会拿到越界 nil，
-- 那既不能算可用也不能算缺失。拿真 questID 反查一个真索引，反查不到就照实 SKIP。
local function SampleLogIndex(questID)
    local fn = GQL.Api.Resolve("C_QuestLog.GetLogIndexForQuestID")
    if not (questID and questID > 0 and GQL.Api.Callable(fn)) then return nil end
    local okCall, idx = pcall(fn, questID)
    if not okCall then return nil end
    return tonumber(idx)
end

-- 返回一串表时，光看 table[#40]{1,2,3...} 问不出字段名，接不上下一步。
-- 补一条首条目的形状，池接口才能照着写归一化。
local function FirstRow(v)
    if type(v) ~= "table" then return "" end
    local first = v[1]
    if type(first) ~= "table" then return "" end
    return " 首条=" .. Describe(first, 1)
end

local function PathLine(path, samples, nameFn)
    local fn = GQL.Api.Resolve(path)
    if not GQL.Api.Callable(fn) then
        return ("MISSING   %s"):format(path)
    end
    local hint = GQL.Api.ArgHint(path)
    local argVal = hint and samples[hint]
    if hint and argVal == nil then
        return ("SKIP      %s 需要 %s，本机拿不到（无 mapID 可用）"):format(path, hint)
    end
    -- 只抓前 4 个返回值：GetQuestInfoByQuestID 实测第 2 个就是 mapID，
    -- 第 3 个往后再多问一个，别把时间/奖励字段漏在半路
    local okCall, a, b, c, d
    if hint then
        okCall, a, b, c, d = pcall(fn, argVal)
    else
        okCall, a, b, c, d = pcall(fn)
    end
    if not okCall then
        return ("ERROR     %s -> %s"):format(path, tostring(a))
    end
    -- nameFn：地图候选专用，把数字返回值顺便翻成地图名。"第几个返回值是 mapID"这种事
    -- 光看号码判不出来（1859 和 2395 都是合法地图号），报上"这个号是哪张图"用户一眼定论。
    local function show(v)
        if nameFn and type(v) == "number" then
            return ("%s 地图名=%s"):format(Describe(v), tostring(nameFn(v)))
        end
        return Describe(v)
    end
    return ("AVAILABLE %s%s -> %s | %s | %s | %s%s"):format(
        path, hint and ("(%s=%s)"):format(hint, tostring(argVal)) or "",
        show(a), show(b), show(c), show(d), FirstRow(a))
end

-- 地图号一律带着名字进报告：上一轮回传里"地图=2512"这种裸号码用户读不出是哪张图，
-- 而"归错组"的判据恰恰是名字（盘卷蛇岛 vs 永歌森林）。名字取不到就印 ?，不许编。
local function MapTag(id)
    if id == nil then return "nil" end
    local n = GQL.Api.MapName and GQL.Api.MapName(id)
    return ("%s(%s)"):format(tostring(id), tostring(n or "?"))
end

--- 表结果摊成 k=v：光看 "table[#4]{questID,mapID,...}" 的键名清单，问不出值藏在哪个
--- 字段、也看不出 questType/freq 这类"到底是 0 还是 2" 的数。生产代码按候选字段取值，
--- 取证段把整个表平铺出来供人核对，两处用的必须是同一份解读，所以这个函数只有一份。
--- 必须**全打**：上一版截到 16 个键，12.1.5 那条日志行是 table[#25] —— 我们要找的地图
--- 凭据（uiMapChartValue / areaName / zone 一类）很可能就在被截掉的 9 个里，
--- 截断等于把下一轮唯一的线索自己扔了。键名排序后输出，两份报告才可比。
local function FlatDescribe(v)
    if type(v) ~= "table" then return Describe(v) end
    local parts, keys, n = {}, {}, 0
    for k in pairs(v) do
        n = n + 1
        -- 留住原始键：数组形状（{1,2,3}）的键是数字，按 tostring(k) 反查会取到 nil，
        -- 报成 "1=nil(nil)" 就是取证段自己在撒谎
        keys[#keys + 1] = { k = k, name = tostring(k) }
    end
    table.sort(keys, function(a, b) return a.name < b.name end)
    for i = 1, #keys do
        local val = v[keys[i].k]
        parts[#parts + 1] = ("%s=%s(%s)"):format(keys[i].name, tostring(val), type(val))
    end
    return ("table[#%d]{%s}"):format(n, table.concat(parts, ","))
end

Probe.FlatDescribe = FlatDescribe

--- 这张图的父图（C_Map.GetMapInfo 的 parentMapID，12.1.5 实测该字段在返回表里）。
--- 池行自带的 mapID 和我们询问用的地图号可以不同（实测：问 2395 回来的行写 2393），
--- 判"2393 是 2395 的子区域"还是"两个不相干的图"只能靠父图链。
local function MapParent(id)
    local fn = GQL.Api.Resolve("C_Map.GetMapInfo")
    if not (id and GQL.Api.Callable(fn)) then return nil end
    local ok, mi = pcall(fn, id)
    if not (ok and type(mi) == "table") then return nil end
    return tonumber(mi.parentMapID)
end

--- === 单条任务取证（/gql quest 用）===
-- 上一版只报账本里存着的结论（地图=2512、来源=池），可 2512 到底是客户端行里带的、
-- 补问出来的，还是我们拿"这次问的是哪张图"套上去的，报告里看不出来 —— 第九轮因此
-- 只能把"盘卷蛇岛"的嫌疑留在原地。倒计时同理：颜色读到的那个数是谁答的、
-- 到底属不属于这条任务，也得摊开问。所以这里把三件事一次摊平：
-- 地图是谁答的 / 倒计时是谁答的 / 金额是谁答的 / 本 CD 什么时候结束（+ 周期判据候选）。
-- 全部只读、全程 pcall：入口缺失照打 MISSING，取不到就写 nil，绝不编一个数。

--- 本 CD 的结束时刻与倒计时口径：这一行的数就是周常任务到期时刻的来源，
--- 读法必须跟 Config.NextCycleEnd / RefreshCycle 完全一致（判形态只认 ResetFormOf 一处）。
local function EvidenceCycle()
    local out = {}
    local reset = GQL.Api.ResetTime()
    local wall, wallSrc = GQL.Api.WallClock()
    local uptime = GQL.Api.Uptime()
    local now = wall or uptime
    local form, why = GQL.Config.ResetFormOf(reset, tonumber(uptime))
    out[#out + 1] = ("GetQuestResetTime=%s 墙上=%s(%s) 开机秒=%s 读法=%s（%s）"):format(
        tostring(reset), tostring(wall), tostring(wallSrc), tostring(uptime),
        tostring(form), tostring(why))
    -- 每日档 vs 周档并排：12.1.5 实测 GetQuestResetTime 在周五给 45607 秒（= 明早 7 点），
    -- 那是每日重置。周口径必须另找入口，这一行就是"该用哪个接口当本周尾巴"的证据。
    -- 两个返回值必须分开接：`local a, b = (X and X()) or nil` 的括号会把调用截断成
    -- 单值，第二个返回值当场丢掉 —— 12.1.5 实测报告里就是"周重置倒计时=467060
    -- 来源=无可用周入口"，数明明答出来了、来源却报成没有，取证报告自己在撒谎。
    local weekly, weeklySrc = nil, nil
    if GQL.Api.WeeklyResetCountdown then
        weekly, weeklySrc = GQL.Api.WeeklyResetCountdown()
    end
    out[#out + 1] = ("周重置倒计时=%s 来源=%s（只认 (0,604800] 秒；锚点与周统计只用这一档，"
        .. "GetQuestResetTime 答的每日档不算）"):format(
        tostring(weekly), tostring(weeklySrc or "无可用周入口"))
    local srcs = (GQL.Api.WeekResetSources and GQL.Api.WeekResetSources()) or {}
    for i = 1, #srcs do
        out[#out + 1] = ("%s ＜周口径候选＞"):format(PathLine(srcs[i], {}))
    end
    local endAt, kind = GQL.Config.NextCycleEnd(now, uptime)
    local db = GQL.Config.DB()
    out[#out + 1] = ("cycleAnchor=%s 下次重置(口径=%s)=%s 距它=%s 秒（日志/手动行的到期就用这个数，"
        .. "口径不是 weekly 时界面也不会说「周」字）")
        :format(tostring(db and db.cycleAnchor), tostring(kind or "nil"), tostring(endAt),
            (endAt and now) and math.floor(endAt - now) or "?")
    return out
end

--- 倒计时候选逐个问。名单就是 Api.QuestTimeLeft 问的那一份（TimeLeftSources），
--- 顺带把"上一版为什么剔掉无参入口"写进报告：它答的是日志里选中的那条限时任务，
--- 跟这里的 questID 无关，套到周常头上就成了"马上就要结束"的假颜色。
local function EvidenceTimeLeft(questID)
    local out = {}
    local srcs = (GQL.Api.TimeLeftSources and GQL.Api.TimeLeftSources()) or {}
    if #srcs == 0 then
        out[#out + 1] = "Api.TimeLeftSources 取不到（Api 版本不对）"
    end
    for i = 1, #srcs do
        local src = srcs[i]
        out[#out + 1] = PathLine(src.path, { questID = questID })
            .. (" ＜代码取它×%s 当这条任务的剩余秒数＞"):format(tostring(src.scale or 1))
    end
    out[#out + 1] = ("GetQuestLogTimeLeft %s（没有 questID 参数，答的是日志里当前选中那条限时任务"
        .. "→ 已剔出倒计时名单，不许再当任何一条任务的到期时刻）"):format(
        GQL.Api.Callable(GQL.Api.Resolve("GetQuestLogTimeLeft")) and "PRESENT" or "MISSING")
    local got = GQL.Api.QuestTimeLeft(questID)
    out[#out + 1] = ("Api.QuestTimeLeft(%s)=%s 秒"):format(tostring(questID), tostring(got))
    return out
end

--- 金额是谁答的。12.1.5 反馈③"蛇岛有个松露大乱斗的金币任务，我的角色做了，
--- 但点刷新矩阵没有这个任务"：`/gql quest` 报 `上次金额=?` + `金币也没到 1000`，
--- 也就是**矩阵的金币门槛从来没为它答过话**。光看这一条分不出三种完全不同的病：
--- ① 客户端根本不给我读金额的入口；② 入口在但这条的奖励数据还没预加载（回 0）；
--- ③ 它真的不给金币。所以这里把候选逐个问一遍、再单独报"奖励数据到没到"，
--- 三份答案一起才定得了案。缺接口必须写 MISSING，不许把"没答"说成"0 金币"。
local function EvidenceMoney(questID)
    local out = {}
    local srcs = (GQL.Api.MoneySources and GQL.Api.MoneySources()) or {}
    if #srcs == 0 then
        out[#out + 1] = "Api.MoneySources 取不到（Api 版本不对）：下面只能看总入口的返回值"
    end
    for i = 1, #srcs do
        out[#out + 1] = PathLine(srcs[i].path, { questID = questID })
            .. " ＜金额候选＞"
    end
    -- 数据到没到：这一行决定上面的 0 能不能当"这条不给金币"读
    local has = GQL.Api.HasRewardData and GQL.Api.HasRewardData(questID)
    out[#out + 1] = ("HaveQuestRewardData=%s（false=奖励数据还没到本地，上面的 0 不算"
        .. "「这条不给金币」；nil=这客户端没这个入口，判不了）"
        .. "  预加载入口=%s（C_TaskQuest.RequestPreloadRewardData / C_QuestRewards.FetchData）")
        :format(tostring(has),
            (GQL.Api.Callable(GQL.Api.Resolve("C_TaskQuest.RequestPreloadRewardData"))
                and "有" or "无")
                .. "/"
                .. (GQL.Api.Callable(GQL.Api.Resolve("C_QuestRewards.FetchData")) and "有" or "无"))
    local got = GQL.Api.QuestRewardMoney and GQL.Api.QuestRewardMoney(questID)
    out[#out + 1] = ("Api.QuestRewardMoney(%s)=%s copper"):format(
        tostring(questID), tostring(got))
    -- 账本里的两份金额：lastMoney=最近一次读到的（离开池子也不清），obs.money=本轮这条签名的
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[questID]
    local obs = rec and GQL.Ledger.GetObs(db, questID)
    local floorMoney = GQL.Ledger.MatrixMoneyFloor()
    out[#out + 1] = ("账本金额：lastMoney=%s  本轮 observation.money=%s  门槛=%s（不到门槛又不命中关键词就不列）%s"):format(
        tostring(rec and rec.lastMoney), tostring(obs and obs.money), tostring(floorMoney),
        (rec and not rec.lastMoney)
            and "  → 金额一次都没读到过，这条从来没靠金币进过矩阵" or "")
    return out
end

--- 地图候选的一条：把"代码怎么用这个返回值"或"为什么不在生产链里"写在同一行。
--- 名单用 Api.MapProbeSources：生产链（QuestMapID 真问的那份）在前、已摘掉的与新候选在后
--- 并带原因。只报生产那份的话，用户永远看不出"摘掉三条之后这条任务其实没人答得出"
--- ——那正是 12.1.5 反馈"丰饶出现在屠戮者高地"之后要盯的事。
local function MapSourceLine(src, questID, mapNameOf)
    local want = src.note
        or (src.ret and ("代码取第 " .. src.ret .. " 个返回值")
            or ("代码只取表字段 " .. table.concat(src.field or {}, "/")))
    return PathLine(src.path, { questID = questID }, mapNameOf) .. " ＜" .. want .. "＞"
end

--- 地图候选逐个问，再把"这张图被谁扫出来过"逐张反查一遍：客户端按图列任务时会不会把
--- 它跨图列进来，只有逐张问出来的行才说得出（行自带地图 vs 询问用的图 vs 指示器 pin）。
local function EvidenceMap(questID)
    local out = {}
    local function mapNameOf(id) return GQL.Api.MapName(id) end
    local sources = (GQL.Api.MapProbeSources and GQL.Api.MapProbeSources())
        or (GQL.Api.MapSources and GQL.Api.MapSources()) or {}
    for s = 1, #sources do
        out[#out + 1] = MapSourceLine(sources[s], questID, mapNameOf)
    end
    local logIndexFn = GQL.Api.Resolve("C_QuestLog.GetLogIndexForQuestID")
    local infoFn = GQL.Api.Resolve("C_QuestLog.GetInfo")
    if GQL.Api.Callable(logIndexFn) and GQL.Api.Callable(infoFn) then
        local okIdx, idx = pcall(logIndexFn, questID)
        if okIdx and idx then
            local okInfo, info = pcall(infoFn, idx)
            out[#out + 1] = okInfo
                and ("日志 GetInfo(索引 %s) -> %s"):format(tostring(idx), FlatDescribe(info))
                or ("GetInfo(%s) ERROR: %s"):format(tostring(idx), tostring(info))
            -- 日志自己报的分区图号：AcceptedQuests 现在就是拿它给已接任务配地图的，
            -- 这条答不答得出来决定"特别任务/战役任务还有没有救"，必须单独一行报
            out[#out + 1] = PathLine("C_QuestLog.GetQuestLogZoneMapID",
                { logIndex = tonumber(idx) }, mapNameOf)
                .. " ＜已接任务的归属图：日志口径＞"
        else
            out[#out + 1] = "GetLogIndexForQuestID -> 无日志索引（没接，或这接口不认 questID）"
        end
    end
    -- 逐张跟踪图反查它出现在谁的清单里
    local mapIDs = (GQL.Config and GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs())
        or (GQL.Api.MidnightMapIDs and GQL.Api.MidnightMapIDs()) or {}
    local paths = { "C_QuestLog.GetQuestsOnMap", "C_TaskQuest.GetQuestsOnMap" }
    local hits = 0
    for p = 1, #paths do
        local fn = GQL.Api.Resolve(paths[p])
        if GQL.Api.Callable(fn) then
            for m = 1, #mapIDs do
                local okCall, rows = pcall(fn, mapIDs[m])
                if okCall and type(rows) == "table" then
                    for r = 1, #rows do
                        local row = rows[r]
                        local rid = tonumber(type(row) == "table"
                            and (row.questID or row.qID) or row)
                        if rid == questID and hits < 8 then
                            hits = hits + 1
                            local t = type(row) == "table" and row or {}
                            out[#out + 1] = ("  %s 问 %s 命中：行自带地图=%s 父图=%s"
                                .. " childDepth=%s 只是指示器=%s 进行中=%s"):format(
                                paths[p], MapTag(mapIDs[m]), MapTag(tonumber(t.mapID)),
                                MapTag(MapParent(tonumber(t.mapID))),
                                tostring(t.childDepth), tostring(t.isMapIndicatorQuest),
                                tostring(t.inProgress))
                        end
                    end
                end
            end
        end
    end
    -- 悬赏清单逐张问：特别任务不上世界任务板，上面那两个入口对它全答不出，
    -- 而 MapQuestIndex 现在会用它兜地图 —— 取证段必须问同一条线，否则报告与实现两套。
    -- **MISSING 也照打**：本机到底有没有这条决定"特别任务还有没有救"，
    -- 只在有才报等于把"没有"这个结论藏起来（用户回传的那份就要靠它定案）。
    local bountyFn = GQL.Api.Resolve("C_QuestLog.GetBountiesForMapID")
    local bountyOk = GQL.Api.Callable(bountyFn)
    local bountyAsked, bountyRows, bountyUniq = 0, 0, {}
    if bountyOk then
        for m = 1, #mapIDs do
            local okCall, rows = pcall(bountyFn, mapIDs[m])
            if okCall and type(rows) == "table" then
                bountyAsked = bountyAsked + 1
                for r = 1, #rows do
                    local row = rows[r]
                    local rid = tonumber(type(row) == "table" and (row.questID or row.qID) or row)
                    if rid then
                        bountyRows = bountyRows + 1
                        if not bountyUniq[rid] then bountyUniq[rid] = true end
                    end
                    if rid == questID and hits < 8 then
                        hits = hits + 1
                        out[#out + 1] = ("  C_QuestLog.GetBountiesForMapID 问 %s 命中"
                            .. "（这条悬赏归这张图）"):format(MapTag(mapIDs[m]))
                    end
                end
            end
        end
        -- 这条凭据能不能用，全看它按不按图区分：行数明显多于去重条数 = 同一批又回来了，
        -- MapQuestIndex 里那道跨图自检会把这种答案整批否掉（生产侧宁缺不编）
        local uniq = 0
        for _ in pairs(bountyUniq) do uniq = uniq + 1 end
        out[#out + 1] = ("悬赏清单（MapQuestIndex 第二道凭据）：%d 张图答得出，共 %d 行、去重 %d 条 —— %s")
            :format(bountyAsked, bountyRows, uniq,
                bountyRows > uniq and "同一批重复出现 = 不按图区分，这条地图凭据本轮作废"
                    or "每条只在自己被问的那张图 = 可按图归组")
    else
        out[#out + 1] = "MISSING   C_QuestLog.GetBountiesForMapID "
            .. "＜悬赏清单反查：本机没这条 = 特别任务只剩日志分区一条路＞"
    end
    out[#out + 1] = ("按图反查（%s%s）：扫了 %d 张跟踪图，%s 条清单里有它"
        .. "（行自带地图与询问图不一致 = 客户端跨图列任务；一致 = 归属本来就报的是这张）"):format(
        table.concat(paths, " / "),
        bountyOk and " / C_QuestLog.GetBountiesForMapID" or "",
        #mapIDs, hits == 0 and "没有" or ("上述 " .. hits .. " 张"))
    return out
end

--- 周期判据候选：不猜接口名，直接把命名表里名字带 Week/Daily/Freq/Reset 的成员列出来，
--- 其中只读前缀（Is/Get/Has）的拿这条 questID 问一遍。
--- 下一版"这条到底是不是周常"就照这份名单接线，别再按记忆补名字。
local function EvidenceCadence(questID)
    local out = {}
    local HINTS = { "Week", "Daily", "Freq", "Reset", "Cadence" }
    local SAFE = { "^Is", "^Get", "^Has", "^Should", "^Can" }
    local nodes = { "C_QuestLog", "C_TaskQuest", "C_QuestProgress", "C_QuestRewards" }
    for n = 1, #nodes do
        local node = GQL.Api.Resolve(nodes[n])
        if type(node) == "table" then
            local names = {}
            for k in pairs(node) do
                if type(k) == "string" then
                    for h = 1, #HINTS do
                        if k:find(HINTS[h], 1, true) then names[#names + 1] = k break end
                    end
                end
            end
            table.sort(names)
            for i = 1, math.min(12, #names) do
                local nm = names[i]
                local safe = false
                for s = 1, #SAFE do
                    if nm:match(SAFE[s]) then safe = true break end
                end
                local line = ("  %s.%s%s"):format(nodes[n], nm, safe and "" or "（名字不是只读前缀，不调用）")
                if safe then
                    local fn = node[nm]
                    local okCall, v = pcall(fn, questID)
                    line = line .. (" 带 questID -> %s"):format(
                        okCall and FlatDescribe(v) or ("ERROR: " .. tostring(v)))
                end
                out[#out + 1] = line
            end
        end
    end
    if #out == 0 then out[#out + 1] = "  四个命名表里没有任何名字带 Week/Daily/Freq/Reset/Cadence 的成员" end
    return out
end

--- 一条任务的完整取证文本（不含标题行，调用方自己拼 questID/名字）。
--- 注册周常取证，回答两个还没实测过的问题：
--- ① 内置表里这几条 questID 在本机到底问得出什么（名字/地图/完成状态）。
---    全问不出的话，矩阵那一行是硬造出来的空壳，内置表就不该带这条。
--- ② "用户只有关键词、不知道 questID"这条路有没有接口能白捡 ID：
---    把跟踪图的任务行全枚举（Api.CandidateQuests，不经世界任务过滤），
---    看预设 ID 在不在候选里、候选里有没有名字命中用户关键词的；
---    再看"已完成清单"（C_QuestLog.GetAllCompletedQuestIDs）在这台客户端是什么形状、
---    里面有没有这条 ID。
--- 这两份答案决定下一版是继续手工扩内置表，还是能把"关键词→自动找 ID"接上。
local function EvidenceRegistered(db, inPool)
    local out = {}
    local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
    if #presets == 0 then
        out[#out + 1] = "Api.WEEKLY_PRESETS 空（内置表没接上？）"
        return out
    end
    local charKey = (GQL.Sync and GQL.Sync.CurrentCharKey) and GQL.Sync.CurrentCharKey() or "?"
    local titleSrcs = (GQL.Api.TitleSources and GQL.Api.TitleSources()) or {}
    local mapSrcs = (GQL.Api.MapSources and GQL.Api.MapSources()) or {}
    local doneSrcs = { "C_QuestLog.IsQuestFlaggedCompleted", "C_QuestLog.IsOnQuest" }
    -- 剩余计时的名单：生产代码真问的那两条（TimeLeftSources）+ 一条还没接进生产的候选。
    -- 留着是因为 12.1.5 的实测把这条线问死了：拿着 89507 问 C_TaskQuest 那两条，
    -- 答出的 464897 与同一刻的 GetSecondsUntilWeeklyReset 完全相同 —— 它对周常报的
    -- "剩余"就是下次周重置，不是"这块板子什么时候换"。所以界面只按子任务自己的
    -- 倒计时说话，这名单纯粹是取证；C_QuestLog.GetTimeAllowed 答得出什么形状也靠这份
    -- 报告定，没实测之前不许接进生产（拿"允许时长"当"还剩多久"就是谎报）。
    local rotSrcs = {}
    local tlSrcs = (GQL.Api.TimeLeftSources and GQL.Api.TimeLeftSources()) or {}
    for i = 1, #tlSrcs do rotSrcs[#rotSrcs + 1] = tlSrcs[i].path end
    rotSrcs[#rotSrcs + 1] = "C_QuestLog.GetTimeAllowed"
    local keywords = (GQL.Config and GQL.Config.GetKeywords and GQL.Config.GetKeywords()) or {}
    local function mapNameOf(id) return GQL.Api.MapName(id) end

    -- 候选枚举一次，供上面每条预设查（问几百张图，逐条预设各问一遍会重复花钱）
    local mapIDs = (GQL.Config and GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs())
        or (GQL.Api.MidnightMapIDs and GQL.Api.MidnightMapIDs()) or {}
    local cand, stats = {}, { maps = 0, answered = 0, rows = 0 }
    if GQL.Api.CandidateQuests then
        local c, s = GQL.Api.CandidateQuests(mapIDs)
        cand = c or {}
        stats = s or stats
    else
        out[#out + 1] = "Api.CandidateQuests 取不到（Api 版本不对）"
    end
    local rowSources = (GQL.Api.MapRowSources and GQL.Api.MapRowSources())
        or { "C_TaskQuest.GetQuestsOnMap", "C_QuestLog.GetQuestsOnMap" }
    out[#out + 1] = ("候选枚举（入口=%s）：问了 %d 张跟踪图，%d 张答得出，摊成 %d 条任务行；"
        .. "其中问得出名字的 %d 条"):format(
        table.concat(rowSources, " / "), stats.maps, stats.answered, stats.rows,
        (function()
            local n = 0
            for i = 1, #cand do if cand[i].name then n = n + 1 end end
            return n
        end)())
    -- 用户关键词在这批候选里能不能白捡到任务 —— 白捡得到才谈得上"设置里只填关键词"
    local kwHits, shown = 0, 0
    for i = 1, #cand do
        local c = cand[i]
        if c.name and GQL.Config.KeywordMatch and GQL.Config.KeywordMatch(c.name) then
            kwHits = kwHits + 1
            if shown < 10 then
                shown = shown + 1
                out[#out + 1] = ("  关键词命中候选 questID=%s 名=%s 地图=%s 日常=%s 进行中=%s"):format(
                    tostring(c.questID), tostring(c.name), MapTag(c.mapID),
                    tostring(c.isDaily), tostring(c.inProgress))
            end
        end
    end
    out[#out + 1] = ("按关键词（%s）在候选里命中 %d 条%s")
        :format(#keywords == 0 and "设置里没填" or table.concat(keywords, "、"),
            kwHits, kwHits > shown and ("（只列前 " .. shown .. " 条）") or "")

    local completed = GQL.Api.CompletedQuestIDs and GQL.Api.CompletedQuestIDs() or nil
    out[#out + 1] = PathLine("C_QuestLog.GetAllCompletedQuestIDs", { questID = 0 })
    out[#out + 1] = ("Api.CompletedQuestIDs() -> %s"):format(
        completed == nil and "问不出（nil，绝不等于\"这周什么都没做\"）"
            or (("共 %d 条"):format(#completed)))

    for p = 1, #presets do
        local e = presets[p]
        for j = 1, #(e.ids or {}) do
            local questID = tonumber(e.ids[j])
            if questID then
                local rec = db and db.quests and db.quests[questID]
                local inCand
                for i = 1, #cand do
                    if cand[i].questID == questID then inCand = cand[i] break end
                end
                local inCompleted
                if completed then
                    for i = 1, #completed do
                        if completed[i] == questID then inCompleted = true break end
                    end
                end
                out[#out + 1] = ("预设 %s questID=%s 账本=%s 名=%s 地图=%s 状态=%s"
                    .. " 在候选清单=%s 在已完成清单=%s"):format(
                    tostring(e.key), tostring(questID),
                    rec and (rec.regKey and "有(regKey)" or "有(非注册)") or "无",
                    tostring(rec and rec.name), MapTag(rec and rec.mapID),
                    tostring(rec and GQL.Ledger.RegisteredState(db, charKey, questID)),
                    inCand and ("是（行报地图 " .. MapTag(inCand.mapID) .. "）") or "否",
                    completed == nil and "问不出" or (inCompleted and "是" or "否"))
                for i = 1, #titleSrcs do
                    out[#out + 1] = "  " .. PathLine(titleSrcs[i], { questID = questID })
                end
                for i = 1, #mapSrcs do
                    local src = mapSrcs[i]
                    out[#out + 1] = "  " .. PathLine(src.path, { questID = questID }, mapNameOf)
                        .. (src.ret and (" ＜代码取第 " .. src.ret .. " 个返回值＞")
                            or (" ＜代码只取表字段 " .. table.concat(src.field or {}, "/") .. "＞"))
                end
                for i = 1, #doneSrcs do
                    out[#out + 1] = "  " .. PathLine(doneSrcs[i], { questID = questID })
                end
                for i = 1, #rotSrcs do
                    out[#out + 1] = "  " .. PathLine(rotSrcs[i], { questID = questID })
                        .. " ＜剩余计时候选＞"
                end
                local st, stErr = GQL.Api.QuestState(questID)
                out[#out + 1] = ("  Api.QuestState(%s)=%s%s"):format(
                    tostring(questID), tostring(st), stErr and (" 降级=" .. tostring(stErr)) or "")
            end
        end
    end
    -- 账本里真注册了哪些条：内置预设之外的都是关键词补扫认出来的。
    -- "占不占行"与矩阵同一个判据（一族一行，占行的是族本体；用户 2026-10-24 定口径），
    -- "在不在图上"另报：它是"这族本周还活着"的证据，也决定族本体那行顶哪张图。
    local reg = (GQL.Ledger.RegisteredIndex and GQL.Ledger.RegisteredIndex(db)) or {}
    out[#out + 1] = ("账本里的注册周常共 %d 条（key 带 kw: 前缀的是补扫按关键词认出来的）")
        :format(#reg)
    for i = 1, math.min(#reg, 12) do
        local rec = (db and db.quests) and db.quests[reg[i].questID]
        -- 分组/归属读 Ledger.RowGroupMapID 那一个判据（矩阵/导出同源）：
        -- probe 说"在哈籁恩达尔"而界面还在祖阿曼 = 两边各判一次，这种分歧最难查。
        local groupMap = GQL.Ledger.RowGroupMapID and GQL.Ledger.RowGroupMapID(db, reg[i].questID)
        local groupLabel
        if groupMap == nil then
            groupLabel = "未知地图"
        elseif GQL.Ledger.WeeklyGroupMapID and groupMap == GQL.Ledger.WeeklyGroupMapID() then
            groupLabel = "周常组"
        else
            groupLabel = MapTag(groupMap)
        end
        -- 没标签时报的那串字也必须从 Config.MAP_SRC 走（经 Ledger 的对外函数）：
        -- 过去这里抄了一份字面量，账本里的串一改，probe 就指着一条不存在的来源解释归属
        local legacyLabel = (GQL.Ledger.LegacyMapSource and GQL.Ledger.LegacyMapSource()) or ""
        local owner = (type(rec) == "table" and type(rec.evMapID) == "number")
            and ("地图事件「" .. tostring(rec.evName or rec.evMapID) .. "」")
            or ((GQL.Ledger.IsResidentWeekly and GQL.Ledger.IsResidentWeekly(db, reg[i].questID)
                and groupMap == GQL.Ledger.WeeklyGroupMapID())
                    and ("客户端没答过图，口述=" .. tostring(rec and rec.mapSrc or "无"))
                or tostring(rec and rec.mapSrc or legacyLabel))
        out[#out + 1] = ("  regKey=%s 族=%s questID=%s 名=%s 地图=%s 分组=%s 归属=%s 占行=%s 在图上=%s 本周状态=%s"):format(
            tostring(reg[i].key),
            tostring(GQL.Ledger.FamilyKey(db, reg[i].questID)),
            tostring(reg[i].questID),
            tostring(rec and rec.name), MapTag(rec and rec.mapID),
            groupLabel, owner,
            -- 读矩阵那同一个判据：probe 说"占行"而界面没这行 = 两边各判一次，
            -- 这种分歧最难查（手动加/关键词从日志收的那两条也算数）
            tostring(GQL.Ledger.OccupiesRow and GQL.Ledger.OccupiesRow(db, reg[i].questID)
                and "是" or "否"),
            tostring(rec and rec.onBoard and "是" or "否"),
            tostring(rec and GQL.Ledger.RegisteredState(db, charKey, reg[i].questID)))
    end
    if #reg > 12 then
        out[#out + 1] = ("  （其余 %d 条省略）"):format(#reg - 12)
    end
    -- 认亲那条线的原始证据：C_QuestLog.GetQuestObjectives 的行里到底有没有子任务 ID、
    -- 字段叫什么名字。用户实测"从丰饶供品的任务追踪里面可以看到子任务的内容"，
    -- 但 12.x 的行形状本机没验过 —— 代码只认数字 questID，认不认得到要看这段。
    out[#out + 1] = "  ---- 任务追踪认亲（母任务的目标行里有没有子任务 ID）"
    for i = 1, math.min(#reg, 4) do
        local questID = reg[i].questID
        local rows = (GQL.Api.QuestObjectives and GQL.Api.QuestObjectives(questID))
        local shape, withID = "问不出", 0
        if rows then
            for j = 1, #rows do if rows[j].questID then withID = withID + 1 end end
            shape = ("#%d 行，带 ID 的 %d 条"):format(#rows, withID)
        end
        out[#out + 1] = ("  Api.QuestObjectives(%s)=%s"):format(tostring(questID), shape)
        local fn = GQL.Api.Resolve("C_QuestLog.GetQuestObjectives")
        if GQL.Api.Callable(fn) then
            local okCall, raw = pcall(fn, questID)
            if okCall and type(raw) == "table" and raw[1] then
                out[#out + 1] = "    首条原始行: " .. FlatDescribe(raw[1])
                for k = 1, math.min(3, #raw) do
                    local r = raw[k]
                    if type(r) == "table" then
                        out[#out + 1] = ("    行#%d questID=%s 文本=%s"):format(
                            k, tostring(r.questID), tostring(r.text))
                    end
                end
            elseif not okCall then
                out[#out + 1] = "    GetQuestObjectives ERROR: " .. tostring(raw)
            end
        end
    end
    -- 加固符文石：SavedInstances 有这条预设（Modules/Progress.lua:1673-1687，
    -- type="any" + 四条 ID 90573/90574/90575/90576 = Magisters/Blood Knights/Farstriders/
    -- Shades of the Row，reset="weekly"、persists=false；国服名见 Locale.lua:609）。
    -- **已接进内置族表**（Api.WEEKLY_PRESETS 的 fortify-the-runestones，12.1.5 实测反馈
    -- "所有角色都做完了，矩阵里没有"）：占行的是 90573（本机唯一问得出名字的那条，
    -- 实测名=加固符文石：魔导师、剩余=118.1时），另外三条只参与"全族取最强状态"。
    -- 这一族**故意没有 match**：板上另有一条"特别任务：加固符文石"，加了族词会把不相干的
    -- 那条扫进族里，整行反而消失（用例 pool_countdown_does_not_kill_a_registered_weekly 钉住）。
    -- 这段取证现在只剩一件事要你回答：这四条到底是"任一即算本周做过"（SI 的 type=any 口径），
    -- 还是一个号每周能把四条各做一遍。判错会让小号那格打错勾，玩家据此少刷一天金币。
    out[#out + 1] = "  ---- 加固符文石（SavedInstances 手抄的四条 ID，已接进内置族表；90573 占行）"
    local runes = { 90573, 90574, 90575, 90576 }
    out[#out + 1] = "  四条 ID 本机各问一遍：名字问得出 = 国服认这几条；"
        .. "「任一即算本周做过」还是「一周各能做好几条」只有你在游戏里能定"
    for i = 1, #runes do
        local id = runes[i]
        local nm = GQL.Api.QuestTitle(id)
        local mapID, mapSrc = GQL.Api.QuestMapID(id)
        local secs = GQL.Api.QuestTimeLeft and GQL.Api.QuestTimeLeft(id)
        local st = select(1, GQL.Api.QuestState(id))
        out[#out + 1] = ("  #%s 名=%s 地图=%s(%s) 剩余=%s 做过=%s"):format(
            tostring(id), tostring(nm or "问不出"),
            tostring(mapID or "问不出"), tostring(mapSrc or "-"),
            (type(secs) == "number" and secs > 0) and (("%.1f时"):format(secs / 3600)) or "问不出",
            tostring(st or "问不出"))
    end
    -- "按关键词在候选里命中 0 条"到底卡在哪一步：把补扫真正会问的那批图、每张图
    -- 三块清单各几条、每条叫什么名字摊开。
    -- 结论只有两种，修法完全不同：① 图根本没进清单（→ 锚点报的图有没有并进来）；
    -- ② 图问到了、清单里名字都对得上却没有那条（→ 这条任务不走按图清单，得换入口）。
    -- 第十轮实测就是卡在 ②：祖阿曼 板清单=9 但命中 0，光看数字分不出
    -- "那 9 条是谁"和"名字问不出来"，所以名字必须逐条打出来。
    -- 第三块"悬赏"（C_QuestLog.GetBountiesForMapID）**只进取证、没接进生产**：
    -- 特别任务这种很可能只住在悬赏清单里，先拿它答一次，答得出再接线。
    out[#out + 1] = "  ---- 补扫图源（跟踪清单 + 族锚点报的图，逐张摊开三块清单的名字）"
    local tracked = (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
    local seenMap, scan, fromAnchor = {}, {}, {}
    local function pushMap(m, anchor)
        m = tonumber(m)
        if m and m > 0 and not seenMap[m] then
            seenMap[m] = true
            if anchor then fromAnchor[m] = true end
            scan[#scan + 1] = m
        end
    end
    for i = 1, #tracked do pushMap(tracked[i]) end
    for i = 1, #reg do pushMap(GQL.Api.QuestMapID(reg[i].questID), true) end
    out[#out + 1] = ("  跟踪清单 %d 张，加上锚点报的图共 %d 张（标 [锚] 的是族锚点报的图）")
        :format(#tracked, #scan)

    -- 一行里把 id=名字 打出来，命中关键词的标 *；超过 cap 条只报总数
    local function NameList(ids, cap)
        local parts, n = {}, 0
        for i = 1, #ids do
            if n >= cap then
                parts[#parts + 1] = ("…共%d条"):format(#ids)
                break
            end
            n = n + 1
            local nm = GQL.Api.QuestTitle(ids[i])
            local kw = nm and GQL.Config.KeywordHit(nm)
            parts[#parts + 1] = ("%s=%s%s"):format(
                tostring(ids[i]), tostring(nm or "问不出名字"), kw and "*" or "")
        end
        return table.concat(parts, " ")
    end
    local function CountHits(ids)
        local hits, n = {}, 0
        for i = 1, #ids do
            local nm = GQL.Api.QuestTitle(ids[i])
            local kw = nm and GQL.Config.KeywordHit(nm)
            if kw and n < 6 then
                n = n + 1
                hits[#hits + 1] = ("%s(#%s|%s)"):format(tostring(nm), tostring(ids[i]), tostring(kw))
            end
        end
        return n, hits
    end
    -- 板清单里的任务按 questID 去重留一份，供下面"板清单有、池里没有"那段逐条问三道闸。
    -- 这里只登记 ID，名字/判别/金额都到那一段再问 —— 每张图各问一遍会重复花钱。
    local boardQ, seenQ = {}, {}
    for i = 1, math.min(#scan, 16) do
        local m = scan[i]
        local okB, board = pcall(GQL.Api.MapQuestRows, m)
        local okL, merged = pcall(GQL.Api.MapQuestRows, m, true)
        local ids = {}
        if okL and type(merged) == "table" then
            for j = 1, #merged do ids[#ids + 1] = merged[j].questID end
        end
        for j = 1, #ids do
            local qid = tonumber(ids[j]) or ids[j]
            if not seenQ[qid] then
                seenQ[qid] = true
                boardQ[#boardQ + 1] = { questID = qid, mapID = m }
            end
        end
        local bounty = Probe.BountyIDsForMap(m)
        local hitN, hitNames = CountHits(ids)
        local bHitN, bHitNames = CountHits(bounty or {})
        for j = 1, #bHitNames do hitNames[#hitNames + 1] = bHitNames[j] end
        hitN = hitN + bHitN
        out[#out + 1] = ("  图 %s%s 板=%s 并日志=%s 悬赏=%s 命中关键词=%d%s%s"):format(
            MapTag(m), fromAnchor[m] and "[锚]" or "",
            (okB and type(board) == "table") and tostring(#board) or "问不出",
            (okL and type(merged) == "table") and tostring(#merged) or "问不出",
            bounty and tostring(#bounty) or "问不出",
            hitN,
            hitN > 0 and " → " or "",
            hitN > 0 and table.concat(hitNames, " ") or "")
        if #ids > 0 then out[#out + 1] = "      清单: " .. NameList(ids, 12) end
        if bounty and #bounty > 0 then
            out[#out + 1] = "      悬赏: " .. NameList(bounty, 12)
        end
    end
    -- 板清单有、池里没有：12.1.5 实测反馈③"蛇岛有个松露大乱斗的金币任务，我的角色做了，
    -- 但是点刷新，矩阵没有看到这个任务"。它就在那张图的板清单上（上面逐张摊开的名字里能翻到），
    -- 却进不了矩阵。中间只有三道闸，逐条把各闸的答复打出来才分得开是谁拦的：
    -- ① 世界任务判别 —— Api.RowsToPool 拿 QuestUtils_IsQuestWorldQuest 筛，回 false 当场丢，
    --    池根本收不到它（按图询问混着普通任务，这道闸拆不得）；
    -- ② 金额 —— 进了池但金币没到 L.MatrixMoneyFloor()、名字又不命中关键词，矩阵照样不占行；
    -- ③ 账本 —— 有记录 = 池收下了、卡在②；没记录 = 卡在①。
    -- "判不了"必须单独成一个值：把接口缺失写成 false 等于替客户端说话（"它说这条不是世界任务"）。
    out[#out + 1] = "  ---- 板清单有、池里没有（三道闸各答一次：世界任务判别 / 奖励金币 / 账本收没收）"
    if #boardQ == 0 then
        out[#out + 1] = "  板清单一条都没摊到（上面那些图全问不出任务行，这条线没有输入）"
    else
        if type(inPool) ~= "table" then
            out[#out + 1] = "  池问不出（GetPool 不可用）：下面这些只是「本轮没被确认在池里」，不等于「池里没有」"
        else
            out[#out + 1] = ("  池可用：本轮摊到 %d 条板清单任务，下面列的是池里没答到的那批")
                :format(#boardQ)
        end
        local floorMoney = (GQL.Ledger and GQL.Ledger.MatrixMoneyFloor
            and GQL.Ledger.MatrixMoneyFloor()) or 500 * 10000
        local missing, shown = 0, 0
        for i = 1, #boardQ do
            local b = boardQ[i]
            local id = b.questID
            if type(inPool) ~= "table" or not inPool[id] then
                missing = missing + 1
                if shown < 40 then
                    shown = shown + 1
                    local nm = GQL.Api.QuestTitle(id)
                    local rec = db and db.quests and db.quests[id]
                    local wq = GQL.Api.IsWorldQuest and GQL.Api.IsWorldQuest(id)
                    local money = GQL.Api.QuestRewardMoney and GQL.Api.QuestRewardMoney(id)
                    local kw = nm and GQL.Config.KeywordHit(nm)
                    out[#out + 1] = ("  #%s %s%s 图=%s 世界任务=%s 金币=%s 账本=%s 占行=%s"):format(
                        tostring(id), tostring(nm or "问不出名字"),
                        kw and ("＜命中词:" .. tostring(kw) .. "＞") or "",
                        MapTag(b.mapID),
                        tostring(wq == nil and "判不了" or wq),
                        (type(money) == "number")
                            and (tostring(money) .. (money < floorMoney and "（不到门槛）" or ""))
                            or "问不出",
                        rec and "有" or "无",
                        tostring((rec and GQL.Ledger.OccupiesRow
                            and GQL.Ledger.OccupiesRow(db, id)) or false))
                end
            end
        end
        out[#out + 1] = ("  板清单共 %d 条，池里没答到 %d 条%s；上面列出 %d 条"):format(
            #boardQ, missing,
            (type(inPool) ~= "table") and "（池问不出，判不了）" or "", shown)
        if missing > shown then
            out[#out + 1] = ("  …其余 %d 条未列（一次报告最多 40 条，免得把日志窗口撑爆）")
                :format(missing - shown)
        end
    end
    -- 族词反查已完成清单：回答"这一族在**这台客户端**到底查得到几条、查到的那些有没有地图与结束时间"。
    -- 第十一轮实测把按图清单这条路堵死了（祖阿曼 板=9、悬赏=5，摊开名字没有丰饶），
    -- 剩下唯一可能自己冒出子任务 ID 的面就是已完成清单（12.1.5 实测 10418 条，答得出）。
    -- 这条角色做过的那几条会在这里现出原形：名字、地图（谁答的）、剩余时间、本周状态。
    out[#out + 1] = "  ---- 族词反查已完成清单（这条线的成败决定丰饶那行能不能带真名真图真倒计时）"
    local words = {}
    for i = 1, #presets do
        local m = presets[i].match
        if type(m) == "string" and m ~= "" then words[#words + 1] = m end
    end
    if #words == 0 then
        out[#out + 1] = "  内置表没有族词（match 字段），反查无从下手"
    else
        local done = GQL.Api.CompletedQuestIDs and GQL.Api.CompletedQuestIDs()
        if not done then
            out[#out + 1] = "  已完成清单问不出（接口不可用或调用失败）"
        else
            out[#out + 1] = ("  已完成清单共 %d 条，逐条问名字，命中族词 [%s] 的列出来（上限 20 条）")
                :format(#done, table.concat(words, "/"))
            -- "做过"这一列别读成"本周做过"：完成判据里有 IsQuestFlaggedCompleted，
            -- 一次性任务做过一次就永远回做过。只有**同时**答得出剩余时间的才是每周 lockout
            -- （12.1.5 实测：#89507 剩余=119.4 时，丰饶大地/觉醒/荣耀/生命那四条剩余问不出
            -- —— 所以内置表只跟 89507，那四条纳进来会让每个号永久打勾）。
            local shown = 0
            for i = 1, #done do
                local id = tonumber(done[i])
                local nm = id and GQL.Api.QuestTitle(id) or nil
                if type(nm) == "string" then
                    for w = 1, #words do
                        if nm:find(words[w], 1, true) then
                            shown = shown + 1
                            if shown <= 20 then
                                local mapID, mapSrc = GQL.Api.QuestMapID(id)
                                local secs = GQL.Api.QuestTimeLeft and GQL.Api.QuestTimeLeft(id)
                                local st = select(1, GQL.Api.QuestState(id))
                                out[#out + 1] = ("  #%s %s 地图=%s(%s) 剩余=%s 做过=%s"):format(
                                    tostring(id), nm,
                                    tostring(mapID or "问不出"), tostring(mapSrc or "-"),
                                    (type(secs) == "number" and secs > 0)
                                        and (("%.1f时"):format(secs / 3600)) or "问不出",
                                    tostring(st or "问不出"))
                            end
                            break
                        end
                    end
                end
            end
            if shown == 0 then
                out[#out + 1] = "  清单里没有任何名字带族词的条目（这条角色从没完成过这一族的任何一条）"
            elseif shown > 20 then
                out[#out + 1] = ("  …同族共 %d 条，只列前 20 条"):format(shown)
            end
        end
    end
    return out
end

--- 矩阵里每一行的"本周"凭据。12.1.5 实测反馈④"蛇岛出现了 2 个不是本周的特别任务，
--- 本周的反而没出现，本周是怨灵"：这一轮到底算不算这周的，只有三件事分得开 ——
--- · 这轮在不在板上（rec.onBoard = 本轮按图清单答到过它）+ 在不在池里；
--- · 状态是谁答的（哪条判据接口回了布尔；全 false 只是"可做"，不是"本周做过"）；
--- · 到期时刻从哪来（这条自己的接口倒计时 / 借来的周重置 / 我们排程猜的）。
--- 上一版这段只打了状态，用户回传后还是分不出"上周那条为什么还在"，所以三列一起摊。
--- 挑行：账本 ID 升序前 12 条（这是玩家看得见的东西，逐条报才比对得上截图）。
local function EvidenceMatrixRows(db, charKey, inPool)
    local out = {}
    out[#out + 1] = "  ---- 矩阵行的本周凭据（这轮在不在板上 / 状态谁答的 / 到期哪来的）"
    local ids = {}
    for questID, rec in pairs((db and db.quests) or {}) do
        if rec and not rec.ignored and GQL.Ledger.OccupiesRow(db, questID) then
            ids[#ids + 1] = questID
        end
    end
    table.sort(ids, function(a, b) return (tonumber(a) or 0) < (tonumber(b) or 0) end)
    if #ids == 0 then
        out[#out + 1] = "  账本里没有占行的记录（先 /gql sync 一次再跑 probe）"
        return out
    end
    local srcs = (GQL.Api.StateSources and GQL.Api.StateSources()) or {}
    local now = (GQL.Api and GQL.Api.WallClock()) or math.floor((GQL.Api and GQL.Api.Uptime()) or 0)
    -- 没标签时报的兜底串同样不许手抄（出处只有 Config.MAP_SRC，经 Ledger 的对外函数）：
    -- 这里以前抄过一份"旧记录没标来源"，串一改 probe 就指着一条不存在的来源解释到期是谁给的
    local legacySrc = (GQL.Ledger.LegacyMapSource and GQL.Ledger.LegacyMapSource()) or "?"
    if #srcs == 0 then
        out[#out + 1] = "  Api.StateSources 取不到（Api 版本不对），下面「谁答的」一列只能空着"
    end
    for i = 1, math.min(#ids, 12) do
        local questID = ids[i]
        local rec = db.quests[questID]
        local who = "问不出（判据接口全缺失或全报错）"
        for s = 1, #srcs do
            local fn = GQL.Api.Resolve(srcs[s].path)
            if GQL.Api.Callable(fn) then
                local okCall, v = pcall(fn, questID)
                if okCall and type(v) == "boolean" then
                    who = ("%s 回 %s（%s 档）"):format(
                        srcs[s].path, tostring(v), srcs[s].kind)
                    break
                end
            end
        end
        local exp = rec.expiresAt
        out[#out + 1] = ("  #%s %s 图=%s 这轮在板上=%s 在池=%s 状态=%s 谁答的=%s 到期=%s"):format(
            tostring(questID), tostring(GQL.Ledger.QuestName(db, questID)),
            MapTag(rec.mapID),
            tostring(not not rec.onBoard),
            -- 池问不出必须写判不了：空表读成"全都不在池里"会把每条都冤枉成上周的残留
            (type(inPool) == "table") and tostring(not not inPool[questID]) or "判不了",
            tostring(GQL.Ledger.State(db, charKey, questID)),
            who,
            (type(exp) == "number")
                and ("%s（%s%s）"):format(
                    GQL.Config.FormatTimeLeft(exp - (now or 0)),
                    tostring(rec.expiresSrc or legacySrc),
                    rec.expiresGuessed and "，排程猜的" or "")
                or "无（接口从没答过它的剩余）")
    end
    if #ids > 12 then
        out[#out + 1] = ("  …账本里共 %d 行占着，只列前 12 条"):format(#ids)
    end
    return out
end

--- 任务日志原始行的未归一化字段。12.1.5 实测反馈⑤"追踪萨拉塔斯并不是蛇岛的任务，
--- 第一次获取到就放在了蛇岛，它应该是银月城的，或者永歌森林的"：生产链那两条入口
--- （C_QuestLog.GetQuestInfo 的 mapID/uiMapID/zoneID + C_TaskQuest.GetQuestZoneID）
--- 对它全答不出，那张蛇岛是历史残留（由已停用的 GetMapForQuestPOIs 写进来的）。
--- 下一份地图凭据只能从日志行自己的字段里找，所以这里把原样字段摊出来 ——
--- AcceptedQuests 只挑 questID/title，字段长什么样这段报告是唯一能看到的地方。
--- 优先摊"账本里没地图"或"账本地图与扫到它的那张图不一致"的那几条（正是归错组的症状），
--- 再补几条日志里的其他行当对照。
local function EvidenceLogRawRows(db)
    local out = {}
    out[#out + 1] = "  ---- 日志原始行字段（找地图凭据用：摊出 C_QuestLog.GetInfo 每条的全部字段）"
    local rows = GQL.Api.AcceptedQuestRawRows and GQL.Api.AcceptedQuestRawRows(6)
    if rows == nil then
        out[#out + 1] = "  C_QuestLog.GetInfo 问不出（入口缺失或首条就报错）：这条线没有输入"
        return out
    end
    if #rows == 0 then
        out[#out + 1] = "  日志里一条任务都没有（这条角色现在没接任务）"
        return out
    end
    for i = 1, #rows do
        local r = rows[i]
        local qid = tonumber(r.questID)
        local rec = qid and db and db.quests and db.quests[qid]
        out[#out + 1] = ("  日志第 %d 条 questID=%s 名=%s 账本地图=%s%s"):format(
            i, tostring(qid or "行里没有"), tostring(r.title or "行里没有 title"),
            MapTag(rec and rec.mapID),
            (rec and rec.mapAsked and rec.mapID and rec.mapAsked ~= rec.mapID)
                and ("（扫到它的图=" .. MapTag(rec.mapAsked) .. "）") or "")
        out[#out + 1] = "      原样字段: " .. FlatDescribe(r)
    end
    return out
end

--- 某张图的悬赏/特别任务清单（C_QuestLog.GetBountiesForMapID，12.1.5 实测行带 questID）。
--- 只给取证用：这条线还没接到生产路径上，等它真答得出丰饶那一条再说。
function Probe.BountyIDsForMap(mapID)
    local fn = GQL.Api.Resolve("C_QuestLog.GetBountiesForMapID")
    if not (mapID and GQL.Api.Callable(fn)) then return nil end
    local ok, rows = pcall(fn, mapID)
    if not (ok and type(rows) == "table") then return nil end
    local out = {}
    for i = 1, #rows do
        local r = rows[i]
        local id = tonumber(type(r) == "table" and r.questID or r)
        if id and id > 0 then out[#out + 1] = id end
    end
    return out
end

--- 地图事件 pin（C_AreaPoiInfo）取证。用户 2026-10-24 定的方向：
--- "丰饶现在在哪个区""亚基入侵在哪个区"压根不是任务 —— 按 M 打开地图看到的那个 pin 才是它，
--- 而 pin 的数据源就是 C_AreaPoiInfo（8.0.1 加进来取代 C_WorldMap.GetMapLandmarkInfo）。
--- 我们一直在拿任务的清单找不是任务的东西，所以 12 张跟踪图的 GetQuestsOnMap 永远"没有它"。
--- 上一版我说"要站在那张图上跑"是错的：地图界面本来就能看任意一张图的 pin，人不用动。
--- 这一段只取证，不写任何账本字段：入口在不在、答什么形状、哪张图报了"正在进行的事件"。
--- 摊开明细只给两种行：客户端自己标的 isCurrentEvent，和名字命中玩家关键词/内置族词的。
--- 其余地标（飞行点、副本口）只报个数 —— 12 张图 × 十几条会把报告淹掉。
local POI_LIST_PATHS = {
    "C_AreaPoiInfo.GetEventsForMap",       -- 只列事件，正是我们要的那一层
    "C_AreaPoiInfo.GetAreaPOIForMap",      -- 这张图的全部 pin（对照用）
    "C_AreaPoiInfo.GetQuestHubsForMap",
    "C_AreaPoiInfo.GetDelvesForMap",
}
-- 要 poiID 的三条：PathLine 只会传一个参数（mapID/questID），传错参数问出来的形状
-- 是废证据，所以这里只报存在性，真调用在下面按图逐个问。
local POI_ITEM_PATHS = {
    "C_AreaPoiInfo.GetAreaPOIInfo",        -- ([uiMapID], areaPoiID)
    "C_AreaPoiInfo.GetAreaPOISecondsLeft", -- (areaPoiID)
    "C_AreaPoiInfo.IsAreaPOITimed",        -- (areaPoiID)
}
local POI_PER_MAP = 12        -- 单张图最多问几条 pin
local POI_DETAIL_LIMIT = 40   -- 整段最多摊开几条的字段

--- pin 名单的形状没实测过：可能是 {1,2,3}，也可能是 [{areaPoiID=1},…]。
--- 认不出的一律丢掉、不猜（编一个 poiID 会让下面所有 GetAreaPOIInfo 拿到"看着可用其实是错 pin"）。
local function PoiIDList(v)
    if type(v) ~= "table" then return nil end
    local out = {}
    for i = 1, #v do
        local row = v[i]
        local id = tonumber(type(row) == "table"
            and (row.areaPoiID or row.id or row.poiID) or row)
        if id and id > 0 then out[#out + 1] = id end
    end
    return out
end

local function PoiIDsOf(path, mapID)
    local fn = GQL.Api.Resolve(path)
    if not (mapID and GQL.Api.Callable(fn)) then return nil end
    local okCall, v = pcall(fn, mapID)
    if not okCall then return nil end
    return PoiIDList(v)
end

--- 这条 pin 的名字值不值得摊开：玩家关键词 + 内置族词（丰饶那种）。
--- 关键词一个都没配时只剩 isCurrentEvent 一条判据，所以名字命中也照样摊。
local function PoiWordHit(name)
    if type(name) ~= "string" or name == "" then return nil end
    local hit = GQL.Config and GQL.Config.KeywordHit and GQL.Config.KeywordHit(name)
    if hit then return hit end
    local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
    for i = 1, #presets do
        local m = presets[i].match
        if type(m) == "string" and m ~= "" and name:find(m, 1, true) then return m end
    end
    return nil
end

local function EvidenceAreaPOI(samples, allMaps)
    local out = {}
    local Api = GQL.Api
    for i = 1, #POI_LIST_PATHS do
        out[#out + 1] = PathLine(POI_LIST_PATHS[i], samples)
    end
    for i = 1, #POI_ITEM_PATHS do
        local fn = Api.Resolve(POI_ITEM_PATHS[i])
        out[#out + 1] = ("%s %s ＜要 poiID，下面按图逐个真问＞")
            :format(fn ~= nil and "PRESENT  " or "MISSING  ", POI_ITEM_PATHS[i])
    end

    local maps = allMaps and (Api.WQMapList and Api.WQMapList())
        or (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs())
        or (Api.MidnightMapIDs and Api.MidnightMapIDs()) or {}
    out[#out + 1] = ("枚举范围=%s 共 %d 张图 当前图=%s")
        :format(allMaps and "全图树（一次性取证，没接进生产）" or "跟踪清单",
            #maps, MapTag(samples.mapID))
    if #maps == 0 then
        out[#out + 1] = "一张图都没有：先按 M 打开地图，或设置里\"＋添加当前所在地图\""
        return out
    end

    local infoFn = Api.Resolve("C_AreaPoiInfo.GetAreaPOIInfo")
    local leftFn = Api.Resolve("C_AreaPoiInfo.GetAreaPOISecondsLeft")
    local timedFn = Api.Resolve("C_AreaPoiInfo.IsAreaPOITimed")
    local detail, eventLines = 0, {}
    for m = 1, #maps do
        local mapID = tonumber(maps[m])
        local ev = mapID and PoiIDsOf("C_AreaPoiInfo.GetEventsForMap", mapID) or nil
        local all = mapID and PoiIDsOf("C_AreaPoiInfo.GetAreaPOIForMap", mapID) or nil
        out[#out + 1] = ("[%s] 事件=%s 全部pin=%s"):format(MapTag(mapID),
            ev and tostring(#ev) or "问不出", all and tostring(#all) or "问不出")
        local ids = {}
        for k = 1, #(ev or {}) do ids[#ids + 1] = { id = ev[k], isEvent = true } end
        if #ids == 0 then
            for k = 1, math.min(#(all or {}), POI_PER_MAP) do
                ids[#ids + 1] = { id = all[k] }
            end
        end
        for k = 1, #ids do
            local info = nil
            if GQL.Api.Callable(infoFn) then
                local okCall, v = pcall(infoFn, mapID, ids[k].id)
                if okCall then info = v end
            end
            local name = type(info) == "table" and info.name or nil
            local word = PoiWordHit(name)
            local current = type(info) == "table" and info.isCurrentEvent
            if type(info) ~= "table" or ids[k].isEvent or word or current then
                local left, timed = "?", "?"
                if GQL.Api.Callable(leftFn) then
                    local okCall, v = pcall(leftFn, ids[k].id)
                    left = okCall and tostring(v) or "报错"
                end
                if GQL.Api.Callable(timedFn) then
                    local okCall, v, v2 = pcall(timedFn, ids[k].id)
                    timed = okCall and (tostring(v) .. "/" .. tostring(v2)) or "报错"
                end
                local line = ("  poi#%s%s 剩余=%s 计时=%s 名字=%s%s"):format(
                    tostring(ids[k].id), ids[k].isEvent and "[事件表]" or "",
                    left, timed, tostring(name),
                    word and ("（命中「" .. word .. "」）") or "")
                out[#out + 1] = line
                if detail < POI_DETAIL_LIMIT then
                    detail = detail + 1
                    out[#out + 1] = "    全字段=" .. FlatDescribe(info)
                end
                -- 只数客户端自己说是事件的那两条（在 GetEventsForMap 名单里、或
                -- isCurrentEvent 为真）。光命中关键词就进这份名单，等于把"这名字里
                -- 带丰饶的地标"报成"丰饶事件正在这个区开着"——下一版会照着它改模型。
                -- "任务号="两种答法都要看得见：没这列 = 客户端没给 ⇒ 那名字不是任务，
                -- 矩阵里的行做不出来（用户 2026-10-24："亚基入侵…关键字我已经加了，矩阵里还看不到"）
                if ids[k].isEvent or current then
                    local pq = type(info) == "table" and tonumber(info.questID) or nil
                    eventLines[#eventLines + 1] = ("%s poi#%s 名字=%s%s%s 任务号=%s"):format(
                        MapTag(mapID), tostring(ids[k].id), tostring(name),
                        ids[k].isEvent and " [事件表]" or "",
                        current and " isCurrentEvent=真" or "",
                        (pq and pq > 0) and tostring(pq) or "客户端没给")
                end
            end
        end
    end

    out[#out + 1] = "---- 哪张图报了\"正在进行的事件\"（这条才是\"现在在哪个区\"）"
    if #eventLines == 0 then
        out[#out + 1] = "一条都没有：AreaPOI 在本机只答得出固定地标 ⇒ 换模型（行跟着事件走）这条路不通"
    else
        for i = 1, math.min(#eventLines, 30) do out[#out + 1] = "  " .. eventLines[i] end
    end

    -- 上面那段是"客户端答了什么形状"，这一段是"生产据此把行归到了哪张图"。
    -- 分开摊才看得出中间哪一层掉的：词没有（关键词/族词配不上 pin 名字）、
    -- 选不出图（同名 pin 挂两张互不为父图的图）、pin 散了（归属已清，行退回原分组）。
    -- 判据只认 Sync.PinWordFor + Ledger.RowGroupMapID，与矩阵同源，不许这里再写一遍。
    out[#out + 1] = "---- 事件 pin 的分组归属（生产真写进账本的那份，判据与矩阵同源）"
    local pdl = GQL.Config.DB() or {}
    local counted = 0
    for questID, rec in pairs(pdl.quests or {}) do
        if rec.regKey or rec.kwLogAdded then
            counted = counted + 1
            local word = GQL.Sync and GQL.Sync.PinWordFor and GQL.Sync.PinWordFor(pdl, questID)
            local groupMap = GQL.Ledger.RowGroupMapID
                and GQL.Ledger.RowGroupMapID(pdl, questID)
            local label
            if groupMap == nil then
                label = "未知地图"
            elseif GQL.Ledger.WeeklyGroupMapID
                and groupMap == GQL.Ledger.WeeklyGroupMapID() then
                label = "周常（答不出地图）"
            else
                label = MapTag(groupMap)
            end
            out[#out + 1] = ("  #%s %s 词=%s 归属=%s(%s) 账本图=%s")
                :format(tostring(questID), tostring(rec.name or "问不出名字"),
                    tostring(word or "没有（族词/关键词配不上任何 pin 名字）"),
                    label, tostring(rec.evName or "客户端答的图/周常组"),
                    MapTag(rec.mapID))
        end
    end
    if counted == 0 then
        out[#out + 1] = "  账本里一条常驻周常都没有：先跑一次同步（/gql sync）或设置里点刷新"
    end

    out[#out + 1] = "---- 入侵事件（C_InvasionInfo：亚基入侵/虚空侵袭这类，Legion 时代的入口，12.x 还在不在未知）"
    out[#out + 1] = PathLine("C_InvasionInfo.GetNumInvasions", {})
    local numFn, rowFn = Api.Resolve("C_InvasionInfo.GetNumInvasions"),
        Api.Resolve("C_InvasionInfo.GetInvasionInfoByIndex")
    if GQL.Api.Callable(numFn) and GQL.Api.Callable(rowFn) then
        local okN, n = pcall(numFn)
        n = (okN and tonumber(n)) or 0
        for i = 1, math.min(n, 6) do
            local okCall, v = pcall(rowFn, i)
            out[#out + 1] = ("C_InvasionInfo.GetInvasionInfoByIndex(%d) -> %s")
                :format(i, okCall and FlatDescribe(v) or ("ERROR " .. tostring(v)))
        end
        if n > 6 then out[#out + 1] = ("（共 %d 条，只细查前 6 条）"):format(n) end
    end

    -- 城市 NPC 周常的"在哪办"：世界任务口径的入口对追踪萨拉塔斯那种恒 nil，
    -- 只剩"发布者在哪"这一条没实测过。名单从账本里现挑（常驻周常且没有可信地图的），
    -- 不写死 questID —— ID 每周换，写死的样本下一轮就取不到证。
    out[#out + 1] = "---- 发布者在哪（永不上板的城市周常，问任务发布者而不是问任务）"
    out[#out + 1] = PathLine("GetQuestLogZoneText", {})
        .. " ＜零参：答的是日志里当前选中那条的分区名，按需要先在日志里点中那条＞"
    local db = GQL.Config.DB()
    local picks, seenQ = {}, {}
    if db and db.quests then
        for questID, rec in pairs(db.quests) do
            if #picks < 3 and rec and (rec.regKey or rec.kwLogAdded)
                and type(rec.name) == "string" and rec.name ~= "" and not seenQ[rec.name] then
                seenQ[rec.name] = true
                picks[#picks + 1] = { questID = questID, name = rec.name }
            end
        end
        table.sort(picks, function(a, b) return a.questID < b.questID end)
    end
    -- 样本 questID=0（本机一条任务都没接到）拿去问发布者只会问出一行废证据
    local sampleQ = tonumber(samples.questID)
    if sampleQ and sampleQ > 0 then
        picks[#picks + 1] = { questID = sampleQ, name = "probe 样本" }
    end
    if #picks == 0 then
        out[#out + 1] = "账本里没有常驻周常可问（关键词补扫或注册周常先跑一轮）"
    end
    for i = 1, #picks do
        out[#out + 1] = ("  「%s」questID=%s"):format(picks[i].name, tostring(picks[i].questID))
        out[#out + 1] = "    " .. PathLine("C_QuestLog.GetQuestGiverMapPosition",
            { questID = picks[i].questID })
    end

    -- 12.1.5 实测：GetQuestGiverMapPosition / GetQuestLogZoneText 这两条都不存在，
    -- "问发布者"这条线死了。成员名单里还剩两条天生带 mapID 的入口没实测过，
    -- 而且都不用跑过去（拿 mapID 逐张问即可）。它们答不答得出，决定
    -- 追踪萨拉塔斯那种行是"有真凭据地图"还是只能进周常组，所以逐张问一遍。
    -- 判据要跟着报出来：所有图都答"在"= 不按图区分，等于没答，不能当真凭据。
    local onMapPaths = {
        "C_QuestLog.IsOnMap",                          -- (questID, uiMapID)
        "C_TaskQuest.DoesMapShowTaskQuestObjectives",  -- (questID, uiMapID)
    }
    out[#out + 1] = "---- 按图问\"这条在不在这张图\"（两参入口，逐张问，人不用跑）"
    for i = 1, #picks do
        local q = tonumber(picks[i].questID)
        for p = 1, #onMapPaths do
            local path = onMapPaths[p]
            local fn = Api.Resolve(path)
            if not GQL.Api.Callable(fn) then
                out[#out + 1] = ("  「%s」%s MISSING"):format(picks[i].name, path)
            elseif q then
                local yes, asked, errs = {}, 0, 0
                for m = 1, #maps do
                    local mapID = tonumber(maps[m])
                    if mapID then
                        asked = asked + 1
                        local okCall, v = pcall(fn, q, mapID)
                        if not okCall then errs = errs + 1 elseif v == true then yes[#yes + 1] = mapID end
                    end
                end
                local tag
                if errs == asked then tag = ("全报错（%d 张）"):format(errs)
                elseif #yes == 0 then tag = ("没有一张说在（问过 %d 张）"):format(asked)
                elseif #yes == asked then tag = ("每张都说在 = 不按图区分，这条凭据作废")
                else
                    local parts = {}
                    for k = 1, #yes do parts[k] = MapTag(yes[k]) end
                    tag = ("说在的图 " .. table.concat(parts, " "))
                end
                out[#out + 1] = ("  「%s」%s -> %s"):format(picks[i].name, path, tag)
            end
        end
    end

    -- 亚基入侵/虚空侵袭这种"正在打的"在客户端里叫 threat（12.x 成员名单里
    -- GetActiveThreatMaps / GetThreatQuests / GetNextWaypointForMap 三条成组出现）。
    -- 零参问得到就比 AreaPOI 那层更直接：它给的是图号，不用猜哪个 pin 算事件。
    out[#out + 1] = "---- 威胁清单（GetActiveThreatMaps / GetThreatQuests：入侵类事件的另一种口径）"
    local function ZeroResult(path)
        local fn = Api.Resolve(path)
        if not GQL.Api.Callable(fn) then return "MISSING" end
        local okCall, v = pcall(fn)
        if not okCall then return "报错：" .. tostring(v) end
        return FlatDescribe(v)
    end
    out[#out + 1] = "  C_QuestLog.HasActiveThreats -> " .. ZeroResult("C_QuestLog.HasActiveThreats")
    out[#out + 1] = "  C_QuestLog.GetActiveThreatMaps -> " .. ZeroResult("C_QuestLog.GetActiveThreatMaps")
    out[#out + 1] = "  C_TaskQuest.GetThreatQuests -> " .. ZeroResult("C_TaskQuest.GetThreatQuests")
    local threatMaps = Api.Resolve("C_QuestLog.GetActiveThreatMaps")
    if GQL.Api.Callable(threatMaps) then
        local okCall, v = pcall(threatMaps)
        local wayFn = Api.Resolve("C_QuestLog.GetNextWaypointForMap")
        local okQ, tq = pcall(function()
            local f = Api.Resolve("C_TaskQuest.GetThreatQuests")
            return GQL.Api.Callable(f) and f() or nil
        end)
        if okCall and type(v) == "table" and GQL.Api.Callable(wayFn) then
            for i = 1, math.min(#v, 6) do
                local mapID = tonumber(v[i])
                -- 真 Usage 是 (questID, uiMapID) 两参：上一版只传图号，自己造了一条 ERROR 出来。
                -- 威胁任务与威胁图不一定同序配对，配不上就拿第一条 —— 这段只要问出形状。
                local questID = (okQ and type(tq) == "table" and
                    (tonumber(tq[i]) or tonumber(tq[1]))) or nil
                if mapID and questID then
                    local okW, w1, w2, w3 = pcall(wayFn, questID, mapID)
                    out[#out + 1] = ("  GetNextWaypointForMap(%s, %s) -> %s"):format(questID, MapTag(mapID),
                        okW and (tostring(w1) .. " | " .. tostring(w2) .. " | " .. tostring(w3))
                            or ("报错 " .. tostring(w1)))
                elseif mapID then
                    out[#out + 1] = ("  GetNextWaypointForMap(%s) -> SKIP（清单没给可配的任务号）")
                        :format(MapTag(mapID))
                end
            end
        end
    end
    -- 亚基入侵这种公共事件**没有任务**（12.1.5 外部口径：每小时轮次的地图公共事件，
    -- 奖励是声望 + 虚空光盐 + 一个研究包，压根不发任务；本机 poiInfo 15 个字段里也没有
    -- questID，`C_AreaPoiInfo` 全族更没有拿任务号的入口）⇒ 我们那套"问任务做没做"的
    -- 凭据（IsQuestFlaggedCompleted / 日志 / 板面）对它天生全废。还剩一条没实测的口径：
    -- 每周奖励面板那份**活动清单**（C_WeeklyRewards.GetActivities，TWW 形状每条带 questID
    -- 与进度）。这台客户端把入侵算成一条活动，"这个号本周参与过没有"就当场问得出；
    -- 算不出就只剩声望/货币增量 —— 那种归因不干净（同一份声望研究营地别处也给），
    -- 宁可照实说"问不出"，也不拿增量猜成"参与过"。
    out[#out + 1] = "---- 每周奖励面板的活动清单（事件参与记录的最后一条候选凭据）"
    out[#out + 1] = "  C_WeeklyRewards.AreRewardsForCurrentRewardPeriod -> "
        .. ZeroResult("C_WeeklyRewards.AreRewardsForCurrentRewardPeriod")
    out[#out + 1] = "  C_WeeklyRewards.HasAvailableRewards -> "
        .. ZeroResult("C_WeeklyRewards.HasAvailableRewards")
    out[#out + 1] = PathLine("C_WeeklyRewards.GetActivities", {})
    local actFn = Api.Resolve("C_WeeklyRewards.GetActivities")
    if not GQL.Api.Callable(actFn) then
        out[#out + 1] = "  ⇒ 这台客户端没有这层：事件参与记录只剩声望/货币增量（归因不干净，不接）"
    else
        local okA, acts = pcall(actFn)
        if not okA then
            out[#out + 1] = "  报错：" .. tostring(acts)
        elseif type(acts) ~= "table" or #acts == 0 then
            out[#out + 1] = ("  答出 %s ⇒ 这层给不出活动清单")
                :format(type(acts) == "table" and "空表" or tostring(acts))
        else
            for i = 1, math.min(#acts, 12) do
                local a = acts[i]
                local qid = type(a) == "table" and tonumber(a.questID) or nil
                out[#out + 1] = ("  [%d] %s%s"):format(i, FlatDescribe(a),
                    qid and ("  ← 任务名=" .. tostring(
                        (GQL.Api.QuestTitle and GQL.Api.QuestTitle(qid)) or "问不出")) or "")
            end
            if #acts > 12 then
                out[#out + 1] = ("（共 %d 条活动，只细查前 12 条）"):format(#acts)
            end
        end
    end
    return out
end

function Probe.QuestEvidence(questID)
    local lines = {}
    local function add(arr)
        for i = 1, #arr do lines[#lines + 1] = arr[i] end
    end
    add({ "---- 本 CD 结束时刻" }); add(EvidenceCycle())
    add({ "---- 倒计时是谁答的" }); add(EvidenceTimeLeft(questID))
    add({ "---- 金额是谁答的（矩阵金币门槛的输入）" }); add(EvidenceMoney(questID))
    add({ "---- 地图是谁答的" }); add(EvidenceMap(questID))
    add({ "---- 周期判据候选（这条到底算不算周常）" }); add(EvidenceCadence(questID))
    return lines
end

function Probe.Run(arg)
    local lines = { "GQL 12.x 接口探测报告", "GetBuildInfo: " .. BuildInfo(), "---- 候选接口" }
    -- `/gql probe all` = 地图事件段把整棵地图树都问一遍（一次性取证）。
    -- 只有事件段认这个开关：关键词补扫那条线用户明确否决过全图扫描，生产路径不跟着放宽。
    local poiAllMaps = type(arg) == "string" and arg:find("all", 1, true) ~= nil

    local samples = { questID = SampleQuestID() }
    samples.mapID = SampleMapID()
    samples.logIndex = SampleLogIndex(samples.questID)
    local paths = GQL.Api.CandidatePaths()
    for i = 1, #paths do
        -- mapID 类候选统一挪到下面的"地图/bounty"段报，这里再打一遍只是重复行
        if GQL.Api.ArgHint(paths[i]) ~= "mapID" then
            lines[#lines + 1] = PathLine(paths[i], samples)
        end
    end

    lines[#lines + 1] = "---- 时钟域"
    local reset = GQL.Api.ResetTime()
    local uptime = GQL.Api.Uptime()
    local wall, wallSrc = GQL.Api.WallClock()
    lines[#lines + 1] = ("GetQuestResetTime=%s GetTime=%s WallClock=%s(%s)")
        :format(tostring(reset), tostring(uptime), tostring(wall), tostring(wallSrc))
    -- 形态判读只认 Config.ResetFormOf 一处，锚点换算用的就是它，不许这里另猜一套
    local form, why = GQL.Config.ResetFormOf(reset, tonumber(uptime))
    lines[#lines + 1] = ("GetQuestResetTime 读法=%s（%s）"):format(tostring(form), tostring(why))
    -- RefreshCycle 只拿周口径算锚点：每日档减一周得到的"本周起点"每天往前挪 24 小时，
    -- 周统计会静默算错。这里两档都打出来，让玩家一眼看出上一版错在哪。
    -- 两个返回值必须分开接：`local a, b = (X and X()) or nil` 的括号会把调用截断成
    -- 单值，第二个返回值当场丢掉 —— 12.1.5 实测报告里就是"周重置倒计时=467060
    -- 来源=无可用周入口"，数明明答出来了、来源却报成没有，取证报告自己在撒谎。
    local weekly, weeklySrc = nil, nil
    if GQL.Api.WeeklyResetCountdown then
        weekly, weeklySrc = GQL.Api.WeeklyResetCountdown()
    end
    lines[#lines + 1] = ("周重置倒计时=%s 来源=%s"):format(
        tostring(weekly), tostring(weeklySrc or "无可用周入口"))
    lines[#lines + 1] = ("按周口径算出的 CD 锚点=%s（这才是 RefreshCycle 写进账本的那个数）")
        :format(tostring(GQL.Config.CycleAnchorOf(weekly, wall, tonumber(uptime))))
    lines[#lines + 1] = ("对照：拿每日档 GetQuestResetTime 当周口径算出的锚点=%s")
        :format(tostring(GQL.Config.CycleAnchorOf(reset, wall, tonumber(uptime))))

    lines[#lines + 1] = "---- 阶段二入口（只判存在，不真调用）"
    local entries = GQL.Api.Stage2Entries()
    for i = 1, #entries do
        local v = GQL.Api.Resolve(entries[i])
        lines[#lines + 1] = ("%s %s"):format(v ~= nil and "PRESENT  " or "MISSING  ", entries[i])
    end

    -- 这一段的产出决定下一版怎么抓池：上一轮只拿到 "GetQuestsOnMap(uiMapID)" 这条用法提示，
    -- 说明池接口是按月历/bounty 给的，缺的就是 mapID 从哪儿枚举。
    lines[#lines + 1] = "---- 地图/bounty 候选"
    lines[#lines + 1] = ("mapID 样本=%s"):format(tostring(samples.mapID))
    local mapPaths = GQL.Api.MapCandidatePaths()
    for i = 1, #mapPaths do
        lines[#lines + 1] = PathLine(mapPaths[i], samples)
    end

    lines[#lines + 1] = "---- 任务详情链（奖励/倒计时取证）"
    -- 池样本 money/timeLeft 全 nil，而地图 pin 上明明有金额和倒计时：
    -- 数据一定在某个接口里。这里拿着真 questID 把可疑入口全问一遍，
    -- 表按 k=v 平铺（键名清单看不出值藏在哪个字段里）。
    local qid = samples.questID
    if not qid or qid == 0 then
        lines[#lines + 1] = "无 questID 样本（先随便做个世界任务同步一次再跑 probe）"
    else
        -- 1. GetQuestInfoByQuestID 全返回值：第 2 个已实测是 mapID，后面可能还有时间
        local fnTQ = GQL.Api.Resolve("C_TaskQuest.GetQuestInfoByQuestID")
        if GQL.Api.Callable(fnTQ) then
            local okT, v1, v2, v3, v4 = pcall(fnTQ, qid)
            if okT then
                lines[#lines + 1] = ("GetQuestInfoByQuestID(%s) 全返回值: %s | %s | %s | %s")
                    :format(tostring(qid), Describe(v1), Describe(v2), Describe(v3), Describe(v4))
            else
                lines[#lines + 1] = "GetQuestInfoByQuestID ERROR: " .. tostring(v1)
            end
        end
        -- 2. 任务日志链：GetInfo 的返回表字段最全（奖励/时间常在这），但入口要日志索引
        local fnLI = GQL.Api.Resolve("C_QuestLog.GetLogIndexForQuestID")
        local fnInfo = GQL.Api.Resolve("C_QuestLog.GetInfo")
        if GQL.Api.Callable(fnLI) and GQL.Api.Callable(fnInfo) then
            local okLI, idx = pcall(fnLI, qid)
            if okLI and idx then
                local okInfo, info = pcall(fnInfo, idx)
                lines[#lines + 1] = okInfo
                    and ("GetLogIndexForQuestID=%d GetInfo -> %s"):format(
                        tonumber(idx) or -1, FlatDescribe(info))
                    or ("GetInfo(%s) ERROR: %s"):format(tostring(idx), tostring(info))
            else
                lines[#lines + 1] = "GetLogIndexForQuestID -> 无日志索引（世界任务不在任务日志里，日志链不可用）"
            end
        end
        -- 3. pin 的奖励类型与奖励币种：WorldMap_GetWorldQuestRewardType 在 _G 实测命中过
        local fnRT = GQL.Api.Resolve("WorldMap_GetWorldQuestRewardType")
        if GQL.Api.Callable(fnRT) then
            local okRT, rt = pcall(fnRT, qid)
            lines[#lines + 1] = ("WorldMap_GetWorldQuestRewardType(%s) -> %s")
                :format(tostring(qid), okRT and Describe(rt) or ("ERROR: " .. tostring(rt)))
        end
        local fnRC = GQL.Api.Resolve("C_QuestLog.GetQuestRewardCurrencies")
        if GQL.Api.Callable(fnRC) then
            local okRC, rc = pcall(fnRC, qid)
            lines[#lines + 1] = ("GetQuestRewardCurrencies(%s) -> %s")
                :format(tostring(qid), okRC and FlatDescribe(rc) or ("ERROR: " .. tostring(rc)))
        end
        -- 4. 按图源首条完整 k=v：确认行里到底有没有时间/奖励字段（上轮只打了键名）
        local fnOnMap = GQL.Api.Resolve("C_TaskQuest.GetQuestsOnMap")
        if GQL.Api.Callable(fnOnMap) and samples.mapID then
            local okMap, rows = pcall(fnOnMap, samples.mapID)
            if okMap and type(rows) == "table" and rows[1] then
                lines[#lines + 1] = ("GetQuestsOnMap(%s) 首条完整值: %s")
                    :format(tostring(samples.mapID), FlatDescribe(rows[1]))
                -- 上一轮实测：问 2395 回来的第一条行里写着 mapID=2393。矩阵按行内地图归组，
                -- 于是"同一个任务在两张图各冒一行"（用户："其他地图已经有了"）。
                -- 光看号码判不出 2393 是 2395 的子区域还是不相干的图，父图链才是依据。
                local rowMap = type(rows[1]) == "table" and tonumber(rows[1].mapID)
                lines[#lines + 1] = ("按图询问 vs 行自带：查询地图=%s 行内地图=%s 行内父图=%s%s"):format(
                    MapTag(samples.mapID), MapTag(rowMap), MapTag(MapParent(rowMap)),
                    (rowMap and rowMap ~= samples.mapID)
                        and " ＜不同：归组该信哪个，看父图链＞" or "")
                -- 上一轮回传里 childDepth=1、isMapIndicatorQuest 这两个字段就在行里，
                -- 它们才分得开"这是子区域的真任务"和"只是别的图上的指示器 pin"——
                -- 而后者正是"任务凭空出现在盘卷蛇岛"的症状。逐行摊开，不靠猜。
                for k = 1, math.min(3, #rows) do
                    local r = rows[k]
                    if type(r) == "table" then
                        lines[#lines + 1] = ("  行#%d questID=%s 行内地图=%s"
                            .. " childDepth=%s 只是指示器=%s 进行中=%s"):format(
                            k, tostring(r.questID), MapTag(tonumber(r.mapID)),
                            tostring(r.childDepth), tostring(r.isMapIndicatorQuest),
                            tostring(r.inProgress))
                    end
                end
            end
        end
    end

    -- 关键词的第二条来源（城市日常/战役任务不在世界任务池里，只能从任务日志取）。
    -- C_QuestLog.GetInfo 的索引口径在 12.x 没实测过，这里把它到底问得出几条、
    -- 名字字段对不对、有没有命中关键词如实摊开 —— 下一版判断"日志链可用不可用"就靠这段。
    lines[#lines + 1] = "---- 任务日志（关键词第二条来源）"
    local kws = GQL.Config.GetKeywords()
    lines[#lines + 1] = ("关键词清单=%s"):format(#kws == 0 and "（空）" or table.concat(kws, " / "))
    local db = GQL.Config.DB()
    -- 池成员要问一次才知道"这条到底还在不在这轮的池里"，而下面的池快照样本段本来也要这份
    -- 数据，就在那里取、这里复用（GetPool 一次几十次接口调用，别问两遍）
    local poolNow, poolErr = GQL.Api.GetPool()
    -- poolKnown 单独成变量：池问不出时传给取证的必须是 nil，不能是一个空表 ——
    -- 空表在下游读起来就是"池里一条都没有"，把"这台客户端没答"说成"客户端答了没有"
    local inPoolNow, poolKnown = {}, (type(poolNow) == "table")
    if poolKnown then
        for i = 1, #poolNow do
            local q = poolNow[i]
            if q and q.questID then inPoolNow[tonumber(q.questID) or q.questID] = true end
        end
    end
    local okAcc, acc = pcall(GQL.Api.AcceptedQuests)
    if not okAcc then
        lines[#lines + 1] = "AcceptedQuests ERROR: " .. tostring(acc)
    elseif type(acc) ~= "table" then
        lines[#lines + 1] = "AcceptedQuests -> 无数据（降级原因见 /gql log）"
    else
        -- 命中的逐条列出来（含"被哪个词命中"），没命中的只报个数：
        -- 用户实测"底部多了一堆没用的特别任务"，只有"哪个词把它们捞进来的"才判得清该删词还是该收口
        local charKey = GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey() or "?"
        local hits, miss = 0, 0
        for i = 1, #acc do
            local hit = GQL.Config.KeywordHit(acc[i].name)
            if hit then
                hits = hits + 1
                if hits <= 20 then
                    -- 状态一格是矩阵里显示 "?" 还是打勾的直接依据，账本里有没有这条也一起报：
                    -- 上一轮用户截图底部整组 "?"，光看名字判不出是没收进来还是收进来没状态。
                    -- 世界任务判别 + 奖励金币是这一轮要补的证据：底部那批"特别任务"里
                    -- 哪些其实是接了的世界任务（该留）、哪些是真杂项（该按金币门槛滤掉），
                    -- 只有这两列分得开，不能再靠猜。
                    -- "来源/在池"两列是修上一轮的 账本=有(日志/池) 口径：那串字其实只说明
                    -- kwLogAdded 是空的，用户读成"这条同时来自日志和池"，可它也可能两边都不在
                    -- （历史残留）。来源=日志/池 两选一、屏蔽过另外带上，在池单独问 ——
                    -- 判"这行到底该不该存在"要的是这几件事分开的答案。
                    -- （以前这里还有第三档"手动"，读 rec.manualAdded：那个字段 2026-10-27
                    --  整体下线，点"添加"只是撤销屏蔽，不再是这条任务的一种来源）
                    local rec = db and db.quests and db.quests[acc[i].questID]
                    local src = "无"
                    if rec then
                        src = (rec.kwLogAdded) and "日志" or "池"
                        if rec.ignored then src = src .. "+永久屏蔽" end
                    end
                    lines[#lines + 1] = ("  questID=%s 名=%s ＜命中：%s＞ 来源=%s 在池=%s 地图=%s%s 状态=%s 世界任务=%s 奖励金币=%s")
                        :format(tostring(acc[i].questID), tostring(acc[i].name), tostring(hit),
                            src, tostring((poolKnown
                                and inPoolNow[tonumber(acc[i].questID) or acc[i].questID])
                                or (poolKnown and false or "判不了")),
                            -- 地图号后面必须跟名字：上一轮这行印的是"地图=2512"，
                            -- 用户读不出那是不是他看到的"盘卷蛇岛"，证据等于没有
                            MapTag(rec and rec.mapID),
                            (rec and rec.mapID and rec.mapAsked and rec.mapAsked ~= rec.mapID)
                                and ("（扫到它的图=" .. MapTag(rec.mapAsked) .. "）") or "",
                            tostring(rec and GQL.Ledger.State(db, charKey, acc[i].questID)),
                            tostring(GQL.Api.IsWorldQuest and GQL.Api.IsWorldQuest(acc[i].questID)),
                            tostring(GQL.Api.QuestRewardMoney(acc[i].questID)))
                end
            else
                miss = miss + 1
            end
        end
        lines[#lines + 1] = ("AcceptedQuests 条数=%d 命中=%d 未命中=%d 当前角色=%s")
            :format(#acc, hits, miss, tostring(charKey))
    end

    -- 12.1.5 用户实测："压根不应该存在没有所属地图的任务"—— 追踪萨拉塔斯 明明在
    -- 永歌森林>银月城，账本里却是 地图=nil，于是落进矩阵底部"未知地图"。
    -- 光回一句"问不到"没用：这里拿着账本里的真 questID，把生产代码的候选名单
    -- （Api.MapSources，和 QuestMapID 问的同一份、连"取第几个返回值"都是同一份，
    --  不许两套口径）逐个问一遍，数字返回值一律带上"这个号是哪张图"，用户回传即可定论。
    -- 上一轮挑样本的口径是"地图=nil 或带补问标记"，结果回传写着"账本里没有缺地图的记录"：
    -- 自动补问早就把地图填满了，池给出来的地图又不带来源标记，于是"谁把任务答成盘卷蛇岛"
    -- 永远查不出来。这轮改成**不缺也要取证**——优先挑"账本地图 ≠ 扫到它的那张图"的记录
    -- （那正是归错组的症状），其次挑补问出来的，最后随便拿几条有地图的。
    lines[#lines + 1] = "---- 地图取证（账本里真任务的地图是谁答的）"
    local picks, rank = {}, {}
    for questID, rec in pairs((db and db.quests) or {}) do
        if rec then
            local r = 0
            if rec.mapID == nil then r = r + 16 end
            if rec.mapAsked and rec.mapAsked ~= rec.mapID then r = r + 8 end
            if rec.mapSrc then r = r + 4 end
            if rec.mapID then r = r + 1 end
            picks[#picks + 1] = questID
            rank[questID] = r
        end
    end
    table.sort(picks, function(a, b)
        if rank[a] ~= rank[b] then return rank[a] > rank[b] end
        return (tonumber(a) or 0) < (tonumber(b) or 0)
    end)
    if #picks == 0 then
        lines[#lines + 1] = "账本里没有记录（先 /gql sync 一次再跑 probe）"
    else
        local function mapNameOf(id) return GQL.Api.MapName(id) end
        local sources = (GQL.Api.MapProbeSources and GQL.Api.MapProbeSources())
            or (GQL.Api.MapSources and GQL.Api.MapSources()) or {}
        for i = 1, math.min(3, #picks) do
            local mid = picks[i]
            local rec = db.quests[mid]
            lines[#lines + 1] = ("questID=%s 名=%s 来源=%s 账本地图=%s%s%s%s"):format(
                tostring(mid), tostring(rec.name),
                tostring((rec.kwLogAdded and "日志")
                    or (inPoolNow[mid] and "池") or "都不在（历史残留）"),
                MapTag(rec.mapID),
                (rec.mapAsked and rec.mapAsked ~= rec.mapID)
                    and ("（扫到它的图=" .. MapTag(rec.mapAsked) .. "）") or "",
                rec.mapSrc and ("（补问自 " .. tostring(rec.mapSrc) .. "）") or "",
                rec.mapID and ("（父图=" .. MapTag(MapParent(rec.mapID)) .. "）") or "")
            if #sources == 0 then
                lines[#lines + 1] = "  Api.MapProbeSources 取不到（Api 版本不对）"
            end
            for s = 1, #sources do
                lines[#lines + 1] = "  " .. MapSourceLine(sources[s], mid, mapNameOf)
            end
        end
        if #picks > 3 then
            lines[#lines + 1] = ("（账本共 %d 条记录，只细查前 3 条：按"
                .. "「缺地图 > 扫描图与账本不符 > 补问来的 > 其它」排）"):format(#picks)
        end
    end

    lines[#lines + 1] = "---- 注册周常取证（内置表的 ID 本机问得出什么 + 关键词能否白捡）"
    -- 整段 pcall：这一段要问几百张图，任何一处接口返回怪东西都不该把后面的
    -- 地图按钮/悬停/池样本段一起吞掉（12.1.5 实测过"一处 format 报错，整份报告没了"）
    local okReg, regLines = pcall(EvidenceRegistered, db, poolKnown and inPoolNow or nil)
    if not okReg then
        lines[#lines + 1] = "本段取证异常：" .. tostring(regLines)
    else
        for i = 1, #(regLines or {}) do lines[#lines + 1] = regLines[i] end
    end

    -- 矩阵行的本周凭据 + 日志原始行字段：上一轮反馈④"蛇岛出现了 2 个不是本周的特别任务，
    -- 本周的反而没出现"与⑤"追踪萨拉塔斯放在了蛇岛"都还缺证据，这两段各自 pcall ——
    -- 一处接口返回怪东西不该把后面的地图按钮/悬停/池样本段一起吞掉。
    local charKeyNow = (GQL.Sync and GQL.Sync.CurrentCharKey) and GQL.Sync.CurrentCharKey() or "?"
    local okRows, rowLines = pcall(EvidenceMatrixRows, db, charKeyNow,
        poolKnown and inPoolNow or nil)
    if not okRows then
        lines[#lines + 1] = "矩阵行取证异常：" .. tostring(rowLines)
    else
        for i = 1, #(rowLines or {}) do lines[#lines + 1] = rowLines[i] end
    end
    local okRaw, rawLines = pcall(EvidenceLogRawRows, db)
    if not okRaw then
        lines[#lines + 1] = "日志原始行取证异常：" .. tostring(rawLines)
    else
        for i = 1, #(rawLines or {}) do lines[#lines + 1] = rawLines[i] end
    end

    -- 地图事件 pin / 入侵 / 发布者三条线一次问完：这一段决定"丰饶在哪个区""亚基入侵在哪个区"
    -- 到底有没有客户端数据源（用户 2026-10-24：那两件事压根不是任务，别再用任务接口找）。
    -- 单独 pcall：AreaPOI 的返回形状一点没实测过，任何一处怪东西都不该把后面的按钮段吞掉。
    lines[#lines + 1] = "---- 地图事件 pin（C_AreaPoiInfo：按 M 看到的就是这份数据，人不用跑过去）"
    local okPoi, poiLines = pcall(EvidenceAreaPOI, samples, poiAllMaps)
    if not okPoi then
        lines[#lines + 1] = "地图事件取证异常：" .. tostring(poiLines)
    else
        for i = 1, #(poiLines or {}) do lines[#lines + 1] = poiLines[i] end
    end

    lines[#lines + 1] = "---- 地图按钮宿主"
    local host = GQL.Api.Resolve("Minimap")
    if host == nil then
        lines[#lines + 1] = "Minimap=缺失（按钮只能降级，矩阵用 /gql matrix）"
    else
        local okCall, w, h = pcall(function() return host:GetWidth(), host:GetHeight() end)
        -- 按钮信息整段 pcall：Matrix 里任何一处取不到（纹理是对象、层级返回怪东西）
        -- 都不该把后面所有段的证据一起吞掉（12.1.5 实测：ButtonInfo 一处 format 报错，
        -- 整份报告没输出，用户只能回传一行错误）
        local okB, info = pcall(function()
            return GQL.Matrix and GQL.Matrix.ButtonInfo and GQL.Matrix.ButtonInfo()
        end)
        lines[#lines + 1] = ("Minimap=可用 显隐=%s 尺寸=%s 按钮=%s"):format(
            tostring(GQL.Api.FrameVisible("Minimap")),
            (okCall and w) and ("%dx%d"):format(w, h or 0) or "取不到",
            okB and tostring(info or "未建") or ("ButtonInfo 出错: " .. tostring(info)))
    end

    lines[#lines + 1] = "---- 悬停遮挡（矩阵 hover 被谁拦/没拦）"
    -- 12.1.5 实测：地图压着矩阵时日志只报 TimerTracker，名单判定形同虚设，
    -- 到底是哪一环把 WorldMapFrame 挡在候选之外只能靠这段字段分辨。
    local okH, hd = pcall(function()
        return GQL.Matrix and GQL.Matrix.HoverDebug and GQL.Matrix.HoverDebug()
    end)
    if not okH then
        lines[#lines + 1] = "HoverDebug 出错: " .. tostring(hd)
    elseif type(hd) ~= "table" then
        lines[#lines + 1] = "HoverDebug 不可用"
    else
        for i = 1, #hd do lines[#lines + 1] = "  " .. tostring(hd[i]) end
    end

    local discovery = DiscoveryLines()
    for i = 1, #discovery do lines[#lines + 1] = discovery[i] end

    lines[#lines + 1] = "---- 池快照样本"
    -- 复用关键词段那里取到的同一份池（上面已经为"在池=是/否"问过一遍，别再问第二遍）
    local pool, err = poolNow, poolErr
    if pool then
        lines[#lines + 1] = ("池条目数: %d"):format(#pool)
        for i = 1, math.min(5, #pool) do
            local q = pool[i]
            local state = { GQL.Api.QuestState(q.questID) }
            lines[#lines + 1] = ("  #%d questID=%s mapID=%s name=%s money=%s items=%s state=%s"):format(
                i, tostring(q.questID), MapTag(q.mapID), tostring(q.name),
                tostring(q.money), Describe(q.items), tostring(state[1]))
        end
    else
        lines[#lines + 1] = "池不可用: " .. tostring(err)
    end

    lines[#lines + 1] = "---- 其它"
    lines[#lines + 1] = "手动添加: " ..
        (GQL.Track and GQL.Track.Status and GQL.Track.Status() or "模块未加载")
    lines[#lines + 1] = "ResetTime: " .. tostring(GQL.Api.ResetTime())
    lines[#lines + 1] = "Money: " .. tostring(GQL.Api.Money())
    lines[#lines + 1] = "LastDegrade: " .. tostring(GQL.Api.LastDegrade())

    local report = table.concat(lines, "\n")
    GQL.Config.Log("probe 完成")
    return report
end

GQL.RegisterCommand("probe", function(arg)
    local report = Probe.Run(arg)
    if GQL.LogWindow and GQL.LogWindow.Show then GQL.LogWindow.Show(report) end
    print(report)
end, "输出 12.x 接口实测报告（加 all = 地图事件段连全图树一起问）")
