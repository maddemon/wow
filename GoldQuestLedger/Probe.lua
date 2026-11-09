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

--- 板面清单里**这一条**的原始行（全部字段）+ 同图倒计时对照。
--- 用户 11-04 的那一问："你可以说任务列表给的数据不带结束时间……只有按 M 才能获得结束时间吗？"
--- 现有报告答不了：生产代码从板面行只取 questID / mapID / 名字 / 金额 / 物品
--- （Api.RowsToPool、Api.MapQuestRows 两处都没看过任何一个时间字段），/gql probe 只摊过
--- 某张图的第一条行，不是这一条。这一问只有把这一条的原始行整个摊出来才定得了案。
--- 三种形状是三种不同的病，必须一次摊全：
---   ① 这张图问出来 0 条 = 板面数据此刻根本不在客户端；
---   ② 行摊出来了、同图别的任务答得出剩余时间、只有这一条答不出 = 这条任务自己不给；
---   ③ 行里带着结束时刻的字段 = 读错了地方，跟按不按 M 没关系。
--- "世界地图界面此刻开没开"必须一起印出来：①那一档的嫌疑人就是它（这一版之前所有
--- 结束时刻都是在矩阵开着的那次遍历里读到的，报告里没有一次把"当时开没开"记下来过）。
--- 只读、不写账本。一次报告问 图×2 张清单 + 3 条剩余时间，全在取证入口里，生产路径零新增调用。
--- 取证段共用的候选图名单：先问这条自己报过的图（接口图指针 / 账本图 / 它的父图），
--- 再问玩家跟踪清单。EvidenceBoardRow（板上有它吗）和 EvidenceMapRenders（图上画它吗）
--- 必须问同一份名单，否则两段说的不是同一张图 —— 这正是"矩阵有、地图没有"最难查的地方。
local MAP_CAP = 12
local function CandidateMapsForQuest(questID)
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[questID]
    local maps, pushed = {}, {}
    local function push(m)
        m = tonumber(m)
        if m and m > 0 and not pushed[m] then
            pushed[m] = true
            maps[#maps + 1] = m
        end
    end
    push(GQL.Api.QuestMapID and GQL.Api.QuestMapID(questID))
    push(rec and rec.mapID)
    push(MapParent(tonumber(rec and rec.mapID)))
    push(rec and rec.evMapID)
    local tracked = (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
    for i = 1, #tracked do push(tracked[i]) end
    return maps, rec
end

local function EvidenceBoardRow(questID)
    local out = {}
    local frame = GQL.Api.Resolve("WorldMapFrame")
    out[#out + 1] = ("世界地图界面此刻=%s（这份答案是%s问出来的；上一版报告从没记过这一项）"):format(
        frame and frame.IsShown and (frame:IsShown() and "开着" or "没开") or "判不了（没有 WorldMapFrame）",
        frame and frame.IsShown and (frame:IsShown() and "开着它" or "没开它") or "帧问不出")
    local maps = CandidateMapsForQuest(questID)
    local srcs = (GQL.Api.MapRowSources and GQL.Api.MapRowSources())
        or { "C_TaskQuest.GetQuestsOnMap", "C_QuestLog.GetQuestsOnMap" }
    local names = {}
    for i = 1, math.min(MAP_CAP, #maps) do names[#names + 1] = MapTag(maps[i]) end
    out[#out + 1] = "询问顺序：" .. table.concat(names, " → ")
        .. (#maps > MAP_CAP and ("（跟踪清单 %d 张，最多问 %d 张）"):format(#maps, MAP_CAP) or "")
    local hit, siblings = nil, {}
    for m = 1, math.min(MAP_CAP, #maps) do
        local mapID = maps[m]
        for s = 1, #srcs do
            local path = srcs[s]
            local fn = GQL.Api.Resolve(path)
            if not GQL.Api.Callable(fn) then
                out[#out + 1] = ("%s 这台客户端没有"):format(path)
            else
                local okCall, rows = pcall(fn, mapID)
                if not okCall then
                    out[#out + 1] = ("%s(%s) 报错：%s"):format(path, tostring(mapID), tostring(rows))
                elseif type(rows) ~= "table" then
                    out[#out + 1] = ("%s(%s) 答 %s（不是表）"):format(
                        path, tostring(mapID), tostring(rows))
                else
                    local found
                    for j = 1, #rows do
                        local r = rows[j]
                        local qid = type(r) == "number" and r
                            or (type(r) == "table" and tonumber(r.questID) or nil)
                        if qid == questID then found = r end
                    end
                    out[#out + 1] = ("%s(%s)=%d 条%s"):format(path, MapTag(mapID), #rows,
                        found and ("  ← 这一条的原始行（一个字段都不漏）：" .. FlatDescribe(found)) or "")
                    if found then
                        hit = true
                        for j = 1, #rows do
                            if #siblings >= 3 then break end
                            local r = rows[j]
                            local qid = type(r) == "number" and r
                                or (type(r) == "table" and tonumber(r.questID) or nil)
                            if qid and qid ~= questID then siblings[#siblings + 1] = qid end
                        end
                        break
                    end
                end
            end
        end
        if hit then break end
    end
    if not hit then
        out[#out + 1] = ("上面问过的 %d 张图里都没有这一条 ⇒ 这份报告无法区分①②："
            .. "要么这条此刻真不在板上，要么图没问对（把整段回传，图指针那一行有线索）")
            :format(math.min(MAP_CAP, #maps))
    else
        local mine = GQL.Api.QuestTimeLeft(questID)
        local parts = {}
        for i = 1, #siblings do
            parts[#parts + 1] = ("%s「%s」=%s"):format(tostring(siblings[i]),
                tostring((GQL.Api.QuestTitle and GQL.Api.QuestTitle(siblings[i])) or "?"),
                tostring(GQL.Api.QuestTimeLeft(siblings[i])))
        end
        out[#out + 1] = ("剩余时间对照：这一条 %s=%s   同板另 %d 条：%s"):format(
            tostring(questID), tostring(mine), #parts,
            #parts > 0 and table.concat(parts, "  ") or "（这张图上没有别条可比）")
        out[#out + 1] = ("这一条的板面行里凡是名字带时间/倒计时的字段全在上面那份原文里；"
            .. "生产代码（Api.RowsToPool / Api.MapQuestRows）一个时间字段都没读过，"
            .. "结束时刻只来自 Api.QuestTimeLeft 那一问")
    end
    return out
end

--- 报错要抄调用链：只抄第一行看不出是谁把什么当成了什么 —— 上一版 provider 那两处
--- 报错（`info` 是个数 / `questInfo` 是 nil）就是这个信息断的。
--- 【为什么非得用 xpcall 的 handler】handler 还在错误帧里跑，那时栈上带着暴雪那一层的
--- 文件名与行号；pcall 返回之后再调 `debug.traceback` 只能抄到**我们自己**的
--- `ErrTrace`/`EvidenceMapRenders` 两帧（11-07 那版就抄成这样，等于没抄）。
--- 参数一律用闭包带：WoW 是 Lua 5.1，它的 xpcall 不接受额外参数。
local function TraceHandler(e)
    local m = tostring(e)
    local dbg = rawget(_G or {}, "debug")
    if type(dbg) ~= "table" or type(dbg.traceback) ~= "function" then return m end
    local okT, tb = pcall(debug.traceback, m, 2)
    return okT and tb or m
end

local function ErrTrace(s)
    local kept, n = {}, 0
    for raw in tostring(s):gmatch("[^\n]+") do
        -- 循环变量在 5.4 里是只读的，改行首空格要另开一个 local
        local trimmed = raw:gsub("^%s+", "")
        -- 我们自己那几帧、以及 traceback 的标题行都不是证据，滤掉才看得见真凶
        if trimmed ~= "" and trimmed ~= "stack traceback:"
            and not trimmed:find("TraceHandler", 1, true)
            and not trimmed:find("ErrTrace", 1, true)
            and not trimmed:find("TryCall", 1, true) then
            n = n + 1
            kept[n] = trimmed
            if n >= 4 then break end
        end
    end
    return table.concat(kept, "  <-  ")
end

local function TryCall(pack)
    local ok, r1, r2, r3, r4, r5 = xpcall(pack, TraceHandler)
    if ok then return true, r1, r2, r3, r4, r5 end
    return false, ErrTrace(r1)
end

--- 函数的入参形状（`debug.getinfo(f,'u').nparams` + 前几个形参名）：方法第一个参数
--- 叫 `self`、第二个叫 `questID` 还是 `worldQuestInfo`，直接把"该递号还是递表"说死。
--- 本机拿不到 debug 库/拿不到名字时回 nil，报告照实写"签名问不出"，不许拿猜测冒充答案。

--- 参数的真形状不靠猜：暴雪的 C 函数被喂错参数会回一句 "Usage: ..."，那句就是签名
--- （`C_TaskQuest.GetQuestLocation` 的停用理由就是这么撞出来的）。0 参撞一次即可，
--- 撞不出来（它 0 参也答得好好的）就照实说"用法问不出"，两种形状都试一遍再印。
--- 【为什么非撞不可】上一版这里把两条入口都按 (questID, uiMapID) 两参问，11-07 那份报告
--- 得到"两个入口 8 张图全答 false"，看起来像铁证；可 docs/probe.txt:470 里这两条入口
--- 对**每台样本任务**都是"没有一张说在" —— 一个从不说 true 的入口，就是被喂了
--- 认不进去的第一个参数（DoesMapShowTaskQuestObjectives 真签名只要 uiMapID，
--- 我们递过去的第一个值是 questID=96329，那不是一个图号）。
local function UsageOf(fn)
    local okCall, err = pcall(fn)
    if okCall then return nil end
    local s = tostring(err)
    return s:match("Usage:[^\n]*") or s:match("[^\n]+")
end

local function ShapeOfUsage(usage)
    local name = type(usage) == "string" and usage or ""
    -- Usage 那句长这样：`Usage: local isOnMap, uiMapID = C_QuestLog.IsOnMap(questID, uiMapID)`
    -- ⇒ 只认**调用括号里**那份名单，整句来找会先撞上返回值里的 uiMapID，形状就颠倒成
    -- (uiMapID, questID) —— 反着喂参数比不喂更坏：它不报错，只答 false。
    local args = name:match("%(([^()]*)%)%s*$") or name:match("%(([^()]*)%)") or name
    local qi = args:find("questID", 1, true)
    local mi = args:find("uiMapID", 1, true)
    if qi and mi then return qi < mi and "q_map" or "map_q" end
    if mi then return "map_only" end
    if qi then return "quest_only" end
    return nil
end

local function ShapeText(shape)
    return (shape == "map_only" and "(uiMapID)"
        or shape == "quest_only" and "(questID)"
        or shape == "map_q" and "(uiMapID, questID)"
        or shape == "q_map" and "(questID, uiMapID)"
        or "(用法撞不出来：按两参试)")
end

local function CallShape(fn, shape, questID, mapID)
    if shape == "map_only" then return TryCall(function() return fn(mapID) end) end
    if shape == "quest_only" then return TryCall(function() return fn(questID) end) end
    if shape == "map_q" then return TryCall(function() return fn(mapID, questID) end) end
    return TryCall(function() return fn(questID, mapID) end)
end

local function FnShape(fn)
    local dbg = rawget(_G or {}, "debug")
    if type(fn) ~= "function" or type(dbg) ~= "table" or type(dbg.getinfo) ~= "function" then
        return nil
    end
    local okI, i = pcall(dbg.getinfo, fn, "uS")
    if not okI or type(i) ~= "table" then return nil end
    local n = tonumber(i.nparams)
    local names = {}
    if n and n > 0 and type(dbg.getlocal) == "function" then
        for k = 1, math.min(n, 3) do
            local okL, ln = pcall(dbg.getlocal, fn, k)
            names[#names + 1] = (okL and type(ln) == "string" and ln or "?")
        end
    end
    local src = type(i.source) == "string" and i.source:match("[^/\\]+$") or "?"
    return ("nparams=%s 形参=%s 定义于 %s:%s")
        :format(tostring(n or "?"), table.concat(names, ","), src,
            tostring(i.linedefined or "?"))
end

--- 帧在真客户端里可能是 table 也可能是 userdata（离线替身只有 table），
--- 只认 table 的遍历在 game 里会一个帧都扫不到 —— 这一格就白取了。
local function Frameish(f)
    local t = type(f)
    return t == "table" or t == "userdata"
end

--- 读帧上的字段一律包 pcall：数字/字符串这类值取下标会直接抛错，
--- 取证段抛出去就把后面所有段一起吞掉（Dump 那边是整个 pcall 兜住的）。
local function ReadField(f, key)
    local okF, v = pcall(function() return f[key] end)
    if okF then return v end
    return nil
end

--- 【11-07 取证：矩阵里有这一行、用户在地图上看不到它】--- 上一段"按图反查"里 `C_QuestLog.GetQuestsOnMap` 0 命中**不能当证据**：那份清单答的是
--- 日志里的任务，而这一条压根没接（同一份报告就写着"当前角色日志：没接"）—— 0 命中是预期，
--- 既定不了罪也洗不清。客户端到底画不画这一条，四份说法一次问全：
---   ① 天生带 mapID 的入口逐张问，**先撞 Usage 再按真形状问**（上一版按两参问，
---      拿到一份"全答 false"的假铁证，见 UsageOf 上面那段）。撞出来只有一个参数的
---      （11-07 实测 `C_QuestLog.IsOnMap(questID)` 就是）**只问一次**：它答的是屏幕上
---      那张图，逐张问只是把同一问问 8 遍；这一条没进日志时它结构上就判不了，照实写
---      "定不了罪也洗不清"。图级入口全答 false 与全答 true 一样是没有区分度 ⇒ 不作证据；
---   ② 这张图是什么**类型**：GetMapInfo 整行摊平，顺手把 mapType/parentMapID 存下来给
---      下面两段用（同一个数不许问第二遍）；
---   ②′ 拼接图：子图在玩家真能打开的那张图上占不占一块格子。11-07 实测本机
---      `GetMapGroupID` 对所有图都答 nil、而 `GetMapRectOnMap(2642, 2512)` 答得出真方块
---      ⇒ 拼接关系在这台客户端是 **rect** 给的，所以这一格的话术以 rect 为主、
---      子图名单（`GetMapChildrenInfo`）为辅，分组答不出只当旁注，不许再当"问不出"的标题；
---   ③ 帧侧活体：**根帧先报它自己有哪些表字段**（下一版写死容器字段名照这一串写），
---      再走子帧 + 猜的容器字段 + 自己报出来且"像 provider/像 pin 池"的字段；provider 的
---      **身份 + 它自己报名字（pairs 走自身字段）+ 它自己的任务集合
---      + 三个判据按"号"和按"它集合里那一行"各问一遍**，再把帧上**已经画出来**的 questID
---      名单摊出来。名字不再靠猜、形状不再靠 debug 库（本机 11-07 实测两句都落空）：
---      暴雪报错里的局部名（`info` 是个数 / `questInfo` 是 nil）说它要一行数据，
---      那就用它自己集合里那一行再问一遍，让客户端答"该递号还是递表"。
---      名单只有在"屏幕上就是这一条的图或它的父图"时才许下结论 —— 11-07 那份是在
---      2393(银月城) 上取的名单，拿它说"渲染这层没有它"是我唬人，这一版必须自己拦住。
--- 代价：① 每张图 1 次调用（≤12 张；单参入口每条只 1 次）、② 每张 1 次 GetMapInfo、
--- ②′ 每张 ≤2 次分组/格子 + 1 次子图名单，全在这条命令上；③ 每个 provider 一次 pairs
--- + ≤6 个表字段 + 判据×2 递法，只走**已存在**的对象（≤1200 个、深度 ≤6：子帧 +
--- 下面那串容器字段），不建帧、不拉数据、不轮询、不写账本。
--- 地图没开就照实写"这一格取不到"，让用户打开那张图再敲一次命令。
local RENDER_PATHS = {
    { path = "C_QuestLog.IsOnMap", scope = "q_map",
      note = "它判的是「日志里已接任务的目标点在不在这张图」，这条没接时结构上就判不了它" },
    { path = "C_TaskQuest.DoesMapShowTaskQuestObjectives", scope = "map",
      note = "它的 Usage 只吃 uiMapID ⇒ 答的是**这张图显不显示任务目标点**，"
          .. "跟是哪一条无关；本条借它只判得了这张图有没有那一类 pin" },
}
local PROVIDER_METHODS = { "ShouldShowQuest", "ShouldMapShowQuest", "IsQuestSuppressed" }
--- provider 手里的集合比它的判据更硬：判据的入参形状还得猜（上一版就是这么同时拿到
--- `false` 和两处报错的），集合是它自己刷出来、准备照着画 pin 的东西。
--- 这些名字没有依据，逐个试；答没答都照实印。
local PROVIDER_SETS = {
    "GetWorldQuests", "GetQuests", "GetBounties", "worldQuests", "quests",
    "filteredQuestIDs", "activeObjects", "questList",
}
local PIN_FIELDS = { "questID", "qID" }
--- 容器字段名：现客户端把数据提供者与 pin 池挂在**帧字段**上，只走 GetChildren 的扫描
--- 一个 provider 都碰不到（上一版就是这么"问不出"的，11-07 实测答出的是
--- `WorldMapFrame.dataProviders`）。这些名字没有依据，所以逐个试、
--- 答没答都照实报 —— 那一格就是下一版可以写死的真凭据。
local PROVIDER_FIELD_NAMES = {
    "questsDataProvider", "worldQuestDataProvider", "dataProviders", "dataProviderFrames",
    "providerData", "pools", "activeObjects", "pinTemplate", "PinTemplate",
    "questPins", "worldQuestPins", "hub", "canvas",
}

--- 帧自己报出的字段名里，哪些最可能装着 provider 或 pin 池。11-07 那份报告按字母序取前 12
--- 个走查，结果 `pinPools`/`pinSuppressors`/`pinsToNudge` 全被 `parentMapID` 之类挤掉了：
--- 走查顺序要按"像不像"排，不是按 a-z 排。
local FRAME_FIELD_HINTS = { "pin", "quest", "provider", "suppress", "filter", "pool", "worldquest" }

--- 一个表里把 questID 抠出来。四种形状都得认，认错了就是把索引/计数当任务号：
---   · 行数组 `{ {questID=..}, .. }` —— 键是 1..n 的索引，**不是**任务号；
---   · 纯号数组 `{ 90777, 93418 }` —— 值才是任务号，而且它的键必须是下标；
---   · TableUtil 的池子 `{ [对象] = true }` —— 对象当**键**用，值是个布尔；
---   · 哈希字段 `{ __enumCalls = 1, info = {...} }` —— 挂在字符串键上的数（计数、图号、
---     zoom 档）不是任务号。11-07 那版就是把帧对象自己的计数字段收成"任务 1"，
---     每个 provider 的集合名单前面都多出一个假号。
local function QuestIDOfEntry(k, val)
    local t = type(val)
    if t == "number" then return type(k) == "number" and val or nil end
    if t == "table" then return tonumber(val.questID or val.qID) end
    if t == "userdata" then
        return tonumber(ReadField(val, "questID") or ReadField(val, "qID"))
    end
    if val == true then
        local kt = type(k)
        if kt == "number" then return k end
        if kt == "table" then return tonumber(k.questID or k.qID) end
        if kt == "userdata" then return tonumber(ReadField(k, "questID") or ReadField(k, "qID")) end
    end
    return nil
end

local function CollectQuestIDs(v, ids, cap)
    local n = 0
    for k, val in pairs(v) do
        n = n + 1
        if n > 200 then break end
        local id = QuestIDOfEntry(k, val)
        if id and id > 0 and (#ids == 0 or ids[#ids] ~= id) and #ids < (cap or 40) then
            ids[#ids + 1] = id
        end
    end
    return #ids
end

--- provider 自己报名字。11-07 那份报告里每个方法的"签名"那一行全是"问不出（本机没有
--- debug 库）"，而猜集合名单（GetWorldQuests/quests/…）八条全部落空 —— 两条路都堵着时
--- 别再猜第三轮：对象自己就能报（Mixin 是把方法 rawset 在对象身上的，pairs 走得通）。
--- 这一格印出来的名单是下一版可以写死的真凭据。
--- pairs 走不动（受保护的 userdata 会抛）时照实写"走不动"，不许把"没扫到"说成"它没有"。
local function OwnNames(node)
    local fns, vals, n = {}, {}, 0
    local okIt, why = pcall(function()
        for k, v in pairs(node) do
            n = n + 1
            if type(k) == "string" then
                if type(v) == "function" then fns[#fns + 1] = k
                else vals[#vals + 1] = { name = k, kind = type(v), value = v } end
            end
        end
    end)
    table.sort(fns)
    table.sort(vals, function(a, b) return a.name < b.name end)
    return fns, vals, n, okIt, why
end

--- 名单太长会撑爆日志窗口：截断要说清截了多少，别让人以为那就是全部。
local function Capped(arr, cap)
    local keep = math.min(#arr, cap or 30)
    local parts = {}
    for i = 1, keep do parts[i] = tostring(arr[i]) end
    return table.concat(parts, " ")
        .. (#arr > keep and (" …其余 " .. (#arr - keep) .. " 个未列") or "")
end

--- 一份"按任务 indexed 的世界任务名单"里到底有什么：条数、有没有这一条、这一条那一行原样、
--- 以及有几个对象的 questID 是 0/没有（池子里那些没挂任务的空对象就是这一档 —— 上一版
--- `activePins=0 条` 看着像"池子是空的"，其实里面可能躺着几个 questID=0 的空 pin）。
local function ScanInfoTable(v, questID, ownMap, ownParent)
    local st = { total = 0, objs = 0, zeroID = 0, mine = nil, onMine = 0, onParent = 0, ids = {} }
    pcall(function()
        for k, row in pairs(v) do
            local r = (type(row) == "table" and row) or (type(k) == "table" and k)
            if r then
                st.total = st.total + 1
                if st.total > 200 then break end
                local rid = tonumber(ReadField(r, "questID") or ReadField(r, "qID"))
                if rid or ReadField(r, "GetName") then
                    st.objs = st.objs + 1
                    if not rid or rid == 0 then st.zeroID = st.zeroID + 1 end
                end
                if rid and rid ~= 0 then
                    if #st.ids < 12 then st.ids[#st.ids + 1] = rid end
                    if rid == questID and not st.mine then st.mine = r end
                end
                local mid = tonumber(ReadField(r, "mapID") or ReadField(r, "uiMapID")
                    or ReadField(r, "zoneID"))
                if mid and mid == ownMap then st.onMine = st.onMine + 1 end
                if mid and ownParent and mid == ownParent then st.onParent = st.onParent + 1 end
            end
        end
    end)
    return st
end

--- pin 是照着哪一份名单画的。11-07 实测这台客户端 `C_WorldQuests / C_WorldQuestDB /
--- C_WorldQuestFrame / C_WorldQuestUtils` 全 MISSING，所以客户端自己答得出来的"渲染层名单"
--- 只剩 `C_TaskQuest.GetQuestsForWorldMap` 这一处（0 参，答按任务号索引的 worldQuestInfo 表）。
--- 前面 ①②′ 那些段全是绕着问，这一份里有它 / 没它才是正面答案：
---   没它 ⇒ 地图不画它是**数据源就没有这一条**，跟过滤、参数形状、拼接格子都无关；
---   有它 ⇒ 才许把答出来的那一行原样递给 ShouldShowQuest / ShouldMapShowQuest
---          （这才是"不递假表"：行是客户端自己给的，不是我们造的）。
--- 别的插件自己那份任务表不查：那是别人的数据，答出来也不能当这一条的凭据。
--- `interiorNote` 非空 = 这一条的图和玩家能打开的那张不是同一类图（11-07 用户人到场实测：
--- 这种任务本来就**不画 pin**，矩阵有它是对的）。没有它才许把"没有 pin"往缺陷那格写。
--- 返回值第二个是要递给判据的那一行（nil = 这份名单里没有它，或名单问不出）。
local function EvidencePinSource(questID, ownMap, ownParent, dispID, interiorNote)
    local out, info, infoFrom = {}, nil, nil
    out[#out + 1] = "---- pin 是照着哪一份名单画的（渲染层的数据源，不是板面清单）"
    local path = "C_TaskQuest.GetQuestsForWorldMap"
    local fn = GQL.Api.Resolve(path)
    if not GQL.Api.Callable(fn) then
        out[#out + 1] = ("  MISSING   %s ⇒ 这份名单问不出"):format(path)
    else
        local okCall, v = TryCall(function() return fn() end)
        if not okCall then
            out[#out + 1] = ("  %s() 报错：%s"):format(path, ErrTrace(v))
        elseif type(v) ~= "table" then
            out[#out + 1] = ("  %s() 答 %s（不是表 ⇒ 这份名单问不出）")
                :format(path, tostring(v))
        else
            local st = ScanInfoTable(v, questID, ownMap, ownParent)
            out[#out + 1] = ("  %s()=%d 项 带任务号的 %d 个  questID=0/没有的 %d 个"
                .. "（这些是池子里没挂上任务的空对象，别把 0 个任务读成「池子是空的」）"):format(
                path, st.total, st.objs - st.zeroID, st.zeroID)
            out[#out + 1] = ("  这一条 %s 在这份名单里=%s   名单里报 %s 的有 %d 条、报它父图 %s 的有 %d 条  前 %d 条：%s")
                :format(tostring(questID), st.mine and "是" or "否",
                    MapTag(ownMap), st.onMine, ownParent and MapTag(ownParent) or "（无父图）",
                    st.onParent, math.min(#st.ids, 12), table.concat(st.ids, " ", 1, math.min(#st.ids, 12)))
            if st.mine then
                info, infoFrom = st.mine, path
                out[#out + 1] = ("  这一条在那份名单里的原始行（一个字段都不漏）：%s")
                    :format(FlatDescribe(st.mine))
                if interiorNote then
                    out[#out + 1] = "  ⇒ 数据源有它，而它仍不画 pin 是**预期**，不是缺陷：" .. interiorNote
                        .. "。本机 C_WorldQuests/C_WorldQuestDB 全 MISSING ⇒「隐藏」这一面客户端答不出来，"
                        .. "只能记成人到场实测的结论；这一格不许再当病查，下面 provider 段只是顺手留个形状答案"
                else
                    out[#out + 1] = "  ⇒ 数据源有它 ⇒ 地图却不画，才轮到 provider 那一层："
                        .. "看下面 provider 段按行再问的那几行"
                end
            elseif st.total > 0 then
                out[#out + 1] = ("  ⇒ 板面清单（GetQuestsOnMap）有这一条、渲染层那份名单没有"
                    .. " ⇒ 「矩阵有、地图没有」到这里是两份**互不相干的客户端数据**，"
                    .. "不是渲染判据把它挡了：判据再问也是白问（它手里压根没有这一条）"
                    .. (interiorNote and ("；另外 %s ⇒ 这一类任务不画 pin 本来就是预期")
                            :format(interiorNote) or ""))
            else
                out[#out + 1] = "  ⇒ 这份名单此刻是空的（0 项）⇒ 屏幕上本来就没有任何一条 pin 可画"
            end
        end
    end
    out[#out + 1] = ("  屏幕上此刻那张图=%s（这份名单不区分图，所以「名单里没有它」这句"
        .. "要和上面 pin 名单那一段对着看）"):format(dispID and MapTag(dispID) or "问不出")
    return out, info, infoFrom
end

local function EvidenceMapRenders(questID)
    local out = {}
    local maps, rec = CandidateMapsForQuest(questID)
    local asked = math.min(MAP_CAP, #maps)
    local ownMap = tonumber(GQL.Api.QuestMapID and GQL.Api.QuestMapID(questID))
        or (rec and tonumber(rec.mapID)) or maps[1]
    -- 屏幕此刻显示哪张图、这一条此刻在不在日志里：① 的单参入口和 ③ 的 pin 名单都要拿它
    -- 说话 ⇒ 在这里问一次、两段共用。分两处各问一遍，两段就能对同一个问题给出两个答案。
    local frame = GQL.Api.Resolve("WorldMapFrame")
    local dispID
    local okDisp, dispV = pcall(function()
        return Frameish(frame) and frame.GetMapID and frame:GetMapID() end)
    if okDisp then dispID = tonumber(dispV) end
    local logIdx
    local logIdxFn = GQL.Api.Resolve("C_QuestLog.GetLogIndexForQuestID")
    if GQL.Api.Callable(logIdxFn) then
        local okL, vL = pcall(logIdxFn, questID)
        if okL then logIdx = tonumber(vL) end
    end

    -- ② 图类型：把候选图的 GetMapInfo 整行摊平，顺手存下 mapType/父图给下面两段用
    local infoOf = {}
    local infoFn = GQL.Api.Resolve("C_Map.GetMapInfo")
    if not GQL.Api.Callable(infoFn) then
        out[#out + 1] = "  MISSING   C_Map.GetMapInfo ⇒ 图类型这一格问不出"
    else
        for m = 1, math.min(asked, 6) do
            local mapID = maps[m]
            local okCall, mi = pcall(infoFn, mapID)
            if not okCall then
                out[#out + 1] = ("  %s GetMapInfo 报错：%s"):format(MapTag(mapID), ErrTrace(mi))
            elseif type(mi) ~= "table" then
                out[#out + 1] = ("  %s GetMapInfo 答 %s（不是表）"):format(MapTag(mapID), tostring(mi))
            else
                infoOf[mapID] = {
                    mapType = tonumber(mi.mapType),
                    parentMapID = tonumber(mi.parentMapID),
                }
                out[#out + 1] = ("  %s 图信息（整行，一个字段都不漏）：%s")
                    :format(MapTag(mapID), FlatDescribe(mi))
            end
        end
    end
    local ownType = infoOf[ownMap] and infoOf[ownMap].mapType
    local otherTypes, seenType, ownParent = {}, {}, nil
    for m = 1, asked do
        local i = infoOf[maps[m]]
        if i and i.mapType and i.mapType ~= ownType and not seenType[i.mapType] then
            seenType[i.mapType] = true
            otherTypes[#otherTypes + 1] = tostring(i.mapType)
        end
    end
    local parOfOwn = infoOf[ownMap] and infoOf[ownMap].parentMapID
    if parOfOwn and parOfOwn > 0 then ownParent = parOfOwn end
    if ownType then
        out[#out + 1] = ("  mapType：这一条报的图=%s  别张候选图=%s（本机没有枚举表，"
            .. "两个数不一样只说明它们不是同一类图，含义不替它编）")
            :format(tostring(ownType), #otherTypes > 0 and table.concat(otherTypes, "/") or "全都一样")
    end
    -- 11-07 用户人到场实测：这一条（迷失祭司之墓 2642，mapType 5）任务点就在里面，盘卷蛇岛那张
    -- 拼起来的图（mapType 3）上**本来就没有它的 pin**。所以"图上看不见"这一格，只有在它和
    -- 能打开的那张是同一类图时才算缺陷；不是同一类就是预期，别再照着它找凶手。
    local parType = ownParent and infoOf[ownParent] and infoOf[ownParent].mapType
    local interiorNote
    if ownType and parType and ownType ~= parType then
        interiorNote = ("这一条的图 %s 报 mapType=%s，玩家能打开的那张 %s 报 mapType=%s"
            .. "（不是同一类图）")
            :format(MapTag(ownMap), tostring(ownType), MapTag(ownParent), tostring(parType))
    end

    -- ① 带 mapID 的入口：先撞 Usage，再按真形状逐张问
    for p = 1, #RENDER_PATHS do
        local entry = RENDER_PATHS[p]
        local fn = GQL.Api.Resolve(entry.path)
        if not GQL.Api.Callable(fn) then
            out[#out + 1] = ("  MISSING   %s ⇒ 这入口本机没有"):format(entry.path)
        else
            local usage = UsageOf(fn)
            local shape = ShapeOfUsage(usage)
            out[#out + 1] = ("  入口 %s  用法=%s  本次按%s问")
                :format(entry.path, tostring(usage or "0 参没报错（用法撞不出来）"),
                    ShapeText(shape))
            if shape == "quest_only" then
                -- 用法里没有图这一维 ⇒ 逐张问就是把同一个问题问 8 遍。11-07 那份报告印出
                -- 8 行一模一样的 false，看起来像"8 张图全答不在"的铁证，其实只有 1 问。
                local okCall, v = CallShape(fn, shape, questID, nil)
                out[#out + 1] = ("    %s(%s) -> %s（问一次就够：它的用法里没有图这一维，"
                    .. "它答的图是**此刻屏幕上那张**=%s）"):format(entry.path, tostring(questID),
                    okCall and tostring(v) or ("报错：" .. ErrTrace(v)),
                    dispID and MapTag(dispID) or "问不出")
                if not okCall then
                    out[#out + 1] = ("  这一条 %s：报错 ⇒ 这一格判不了"):format(entry.path)
                elseif logIdx and logIdx > 0 then
                    out[#out + 1] = ("  这一条 %s：它此刻在日志里（索引 %s）⇒ 答 true/false 说的就是"
                        .. "「屏幕上这张 %s 有没有它的目标点」，与这一条自己的图 %s 无关")
                        :format(entry.path, tostring(logIdx),
                            dispID and MapTag(dispID) or "问不出", MapTag(ownMap))
                else
                    out[#out + 1] = ("  这一条 %s：%s。%s ⇒ 答 %s 是**结构性的**（没接的任务它无从可查）"
                        .. "，这一格**定不了罪也洗不清**，看下面 provider 与 pin 两段")
                        :format(entry.path,
                            logIdx == 0 and "日志索引答 0（没接）"
                                or "日志索引问不出（入口缺失或答不出，不许据此当成「没接」）",
                            entry.note, tostring(v))
                end
            else
                local yes, errs, answered = {}, 0, 0
                for m = 1, asked do
                    local mapID = maps[m]
                    local okCall, v = CallShape(fn, shape, questID, mapID)
                    if not okCall then
                        errs = errs + 1
                        out[#out + 1] = ("    %s 报错：%s"):format(MapTag(mapID), ErrTrace(v))
                    else
                        answered = answered + 1
                        if v == true then yes[#yes + 1] = mapID end
                        out[#out + 1] = ("    %s=%s"):format(MapTag(mapID), tostring(v))
                    end
                end
                if answered == 0 then
                    out[#out + 1] = ("  %s：全报错 ⇒ 判不了"):format(entry.path)
                elseif entry.scope == "map" then
                    -- 图级入口答的不是"这一条在不在这张图"，是"这张图会不会画任务目标点那一类 pin"。
                    -- 上一版把它按条读，等于把图级开关冒充成单条凭据 —— 那句"客户端认这些图会画它"
                    -- 就是这么来的假话。这里照图级说，并且只回答一个问题：这一条的图/父图在不在里面。
                    local parts = {}
                    for i = 1, #yes do parts[i] = MapTag(yes[i]) end
                    local mine = nil
                    for i = 1, #yes do
                        if yes[i] == ownMap or (ownParent and yes[i] == ownParent) then mine = true end
                    end
                    out[#out + 1] = ("  %s（图级入口，按整张图答，不按这一条答）：答「会画」的图=%s。%s"):format(
                        entry.path, #parts > 0 and table.concat(parts, " ") or "一张都没有", entry.note)
                    if #yes == 0 then
                        -- 与上面"每张都说在"是同一个病的另一头：一个从不说 true 的入口没区分度。
                        -- 11-07 那份报告问的 8 张里就有玩家明明看得见任务 pin 的图，它照样全答
                        -- false ⇒ 那句"不是这一条的病，是整张图的病"是拿假铁证下的结论，拦住。
                        out[#out + 1] = ("  ⇒ 问过的 %d 张里一张都没答「会画」⇒ 这个入口在本机"
                            .. "不区分图，**不作证据**：既不能说「整张图的病」，也不能洗清渲染这层"
                            .. "（下面 provider 与 pin 两段才是这一格的凭据）"):format(asked)
                    elseif mine then
                        out[#out + 1] = ("  ⇒ 这一条的图或它的父图**在**那份名单里 ⇒ 这张图本来会画"
                            .. "那一类 pin，看不见它就只能是没有进它的数据集：看下面 provider 与 pin 两段")
                    else
                        out[#out + 1] = ("  ⇒ 这一条的图和它的父图都不在那份名单里，而名单里另有 %d 张图"
                            .. "（它认得别的图会画，唯独不认这一条的图和父图）⇒ 客户端说这几张图不画"
                            .. "任务目标点那一类 pin（那「地图上看不到」就不是这一条的病，是整张图的病）")
                            :format(#yes)
                    end
                elseif #yes == 0 then
                    out[#out + 1] = ("  这一条 %s：答了 %d 张，没有一张说在。%s"
                        .. " ⇒ 这一格**定不了罪**：它可能只是对世界任务一律答 false 的那类入口")
                        :format(entry.path, answered, entry.note)
                elseif #yes == asked then
                    out[#out + 1] = ("  这一条 %s：每张都说在 = 不按图区分，这入口不作证据"):format(entry.path)
                else
                    local parts = {}
                    for i = 1, #yes do parts[i] = MapTag(yes[i]) end
                    out[#out + 1] = ("  这一条 %s：说在的图=%s ⇒ 客户端认这些图会画它")
                        :format(entry.path, table.concat(parts, " "))
                end
            end
        end
    end

    -- ②′ 拼接图里到底有没有这块格子（用户 11-07 原话："盘卷蛇岛是多个地图拼接在一起的，
    -- 没法单独打开哪个子地图"）。主证据是 **rect**：它直接答"子图在这张能打开的图上
    -- 占哪一块"；分组（GetMapGroupID）本机全答 nil，只配当旁注。
    out[#out + 1] = ("---- 拼接图：这一条报的图=%s 在你能打开的那些图上有没有一块格子")
        :format(MapTag(ownMap))
    local rectFn = GQL.Api.Resolve("C_Map.GetMapRectOnMap")
    local kidsFn = GQL.Api.Resolve("C_Map.GetMapChildrenInfo")
    if not GQL.Api.Callable(rectFn) then
        out[#out + 1] = "  MISSING   C_Map.GetMapRectOnMap ⇒ 格子位置这一格问不出"
    else
        local withRect, empty = {}, 0
        for m = 1, asked do
            local mapID = maps[m]
            if mapID ~= ownMap then
                local okCall, l, b, w, h = pcall(rectFn, ownMap, mapID)
                local has = okCall and tonumber(w) and tonumber(w) > 0 and tonumber(h) and tonumber(h) > 0
                if has then withRect[#withRect + 1] = { mapID = mapID, wh = tostring(w) .. "x" .. tostring(h) } end
                if okCall then empty = empty + 1 end
                out[#out + 1] = ("  GetMapRectOnMap(%s 放在 %s 上) -> %s"):format(
                    MapTag(ownMap), MapTag(mapID),
                    (not okCall) and ("报错：" .. ErrTrace(l))
                        or (l ~= nil and ("left=%s bottom=%s width=%s height=%s")
                                :format(tostring(l), tostring(b), tostring(w), tostring(h))
                            or "nil（这张图答不出位置）"))
            end
        end
        if #withRect > 0 then
            local parts = {}
            for i = 1, #withRect do
                parts[i] = ("%s %s"):format(MapTag(withRect[i].mapID), withRect[i].wh)
            end
            out[#out + 1] = ("  ⇒ 格子里有它的位置（%s）⇒ 不是「客户端没给它留地方」：%s")
                :format(table.concat(parts, "、"),
                    interiorNote and (interiorNote .. " ⇒ 有格子也不等于要画 pin，看不见它可能是预期")
                        or "得往渲染判据那一层查（看下面 provider 与 pin 两段）")
        elseif empty > 0 then
            out[#out + 1] = ("  ⇒ 问过的 %d 张图都没给它留格子 ⇒ pin 无处可画，与用户所见一致"
                ):format(empty)
        else
            out[#out + 1] = "  ⇒ 一张位置都没问到（全报错或全 nil）⇒ 这一格判不了，看下面两段"
        end
    end
    if ownParent and GQL.Api.Callable(kidsFn) then
        local okC, kids = pcall(kidsFn, ownParent)
        local own, listed = nil, {}
        if okC and type(kids) == "table" then
            for i = 1, math.min(#kids, 40) do
                local kid = type(kids[i]) == "table"
                    and tonumber(kids[i].mapID or kids[i].uiMapID) or tonumber(kids[i])
                if kid then
                    listed[#listed + 1] = tostring(kid)
                    if kid == ownMap then own = true end
                end
            end
        end
        out[#out + 1] = ("  %s 的子图名单=%s 含这一条的图=%s"):format(MapTag(ownParent),
            (not okC) and ("报错：" .. ErrTrace(kids))
                or (type(kids) ~= "table" and tostring(kids)
                    or (#kids .. " 张：" .. table.concat(listed, " "))),
            own == true and "是（拼接关系成立，格子与名字都对得上才画得出）"
                or "否（这张父图的孩子名单里没有它 ⇒ 它压根不是拼进这张图的，玩家自然找不到）")
    else
        out[#out + 1] = "  C_Map.GetMapChildrenInfo 或父图号问不出 ⇒ 子图名单这一格缺"
    end
    local groupFn = GQL.Api.Resolve("C_Map.GetMapGroupID")
    local membersFn = GQL.Api.Resolve("C_Map.GetMapGroupMembersInfo")
    if GQL.Api.Callable(groupFn) then
        local gAsked, gHit = 0, nil
        for m = 1, math.min(asked, 6) do
            local okCall, g = pcall(groupFn, maps[m])
            if okCall and tonumber(g) then
                gAsked = gAsked + 1
                if gHit ~= true and GQL.Api.Callable(membersFn) then
                    local okMem, rows = pcall(membersFn, tonumber(g))
                    if okMem and type(rows) == "table" then
                        for j = 1, math.min(#rows, 20) do
                            local mid = type(rows[j]) == "table"
                                and tonumber(rows[j].mapID or rows[j].uiMapID) or tonumber(rows[j])
                            if mid == ownMap then gHit = true break end
                        end
                    end
                end
            end
        end
        out[#out + 1] = ("  旁注：这台客户端用不用「分组」这套口径=%s（问到 %d 张图的分组）")
            :format(gAsked == 0 and "不用（全答 nil，所以上面以 rect 为准）"
                or (gHit == true and "用，且分组里也有这一条的图" or "用，但成员里没有这一条的图"),
                gAsked)
    end
    local dispFn = GQL.Api.Resolve("C_Map.GetMapDisplayInfo")
    if GQL.Api.Callable(dispFn) then
        for i = 1, 2 do
            local mapID = maps[i]
            if mapID then
                local okCall, d = pcall(dispFn, mapID)
                out[#out + 1] = ("  %s 显示信息：%s"):format(MapTag(mapID),
                    (not okCall) and ("报错：" .. ErrTrace(d))
                        or (type(d) == "table" and FlatDescribe(d)
                            or ("答 " .. tostring(d) .. "（不是表 ⇒ 这入口答不了「能不能单独打开」）")))
            end
        end
    else
        out[#out + 1] = "  MISSING   C_Map.GetMapDisplayInfo ⇒ 这图能不能单独打开问不出"
    end

    -- ②″ pin 到底照着哪一份名单画：这是渲染层的**数据源**，前面 ①②′ 全是绕着问。
    -- 这份名单里有它 ⇒ 才轮到 provider 判据；没有它 ⇒ 地图压根没得画，判据问了也是白问。
    local pinLines, pinInfo, pinInfoFrom = EvidencePinSource(questID, ownMap, ownParent, dispID,
        interiorNote)
    for i = 1, #pinLines do out[#out + 1] = pinLines[i] end

    -- ③ 帧侧活体：地图开着才有这一格（frame/dispID 在函数开头已问过，这里直接用）
    if not Frameish(frame) then
        out[#out + 1] = "  WorldMapFrame 问不到（从没按过 M？）⇒ 帧侧判据与当前 pin 名单本轮取不到：打开那张图再敲一次这条命令"
        return out
    end
    local okShown, shown = pcall(function() return frame.IsShown and frame:IsShown() end)
    local filterParts = {}
    for i, key in ipairs({ "filter", "subFilter", "searchText" }) do
        local fv = ReadField(frame, key)
        if fv ~= nil then
            filterParts[#filterParts + 1] = ("%s=%s"):format(key,
                (type(fv) == "table" and Describe(fv, 1) or tostring(fv)))
        end
    end
    out[#out + 1] = ("  世界地图帧：存在  开着=%s  当前显示=%s  界面筛选状态=%s")
        :format(okShown and tostring(shown) or ("报错:" .. tostring(shown)),
            dispID and MapTag(dispID) or "问不出",
            #filterParts > 0 and table.concat(filterParts, " ") or "帧上没有 filter/subFilter/searchText 字段")

    local provNodes, pins, hosts, fieldHits, seen = {}, {}, {}, {}, {}
    local fieldSeen, hostSeen, nodeSeen, scanned = {}, {}, {}, 0
    local frameFields, frameFieldsOK, frameFieldsWhy = {}, true, nil
    --- 这个字段像不像装着 provider 或 pin 池：它自己有判据方法、或它的成员里有带 questID
    --- 的对象。只认这两样，免得把 UIParent 那种"什么都是子帧"的字段整个拖进来扫。
    local function Providerish(o)
        if not (Frameish(o) or type(o) == "table") then return false end
        for p = 1, #PROVIDER_METHODS do
            if type(ReadField(o, PROVIDER_METHODS[p])) == "function" then return true end
        end
        if tonumber(ReadField(o, "questID") or ReadField(o, "qID")) then return true end
        return false
    end
    local function Poolish(v)
        if not (Frameish(v) or type(v) == "table") then return false end
        if Providerish(v) then return true end
        local found = false
        pcall(function()
            local c = 0
            for _, item in pairs(v) do
                c = c + 1
                if c > 20 then break end
                if Providerish(item) then found = true break end
            end
        end)
        return found
    end
    local function labelOf(node, path)
        local okN, n = pcall(function() return node.GetName and node:GetName() end)
        if okN and type(n) == "string" and n ~= "" then return n end
        return path
    end
    local walkContainer   -- 先声明：walk 的 depth==0 分支要用它，写在 walk 之后会绑成全局
    local function walk(node, path, depth)
        if not Frameish(node) or seen[node] or depth > 6 or scanned >= 1200 then return end
        seen[node] = true
        scanned = scanned + 1
        local here = labelOf(node, path)
        for p = 1, #PROVIDER_METHODS do
            if type(ReadField(node, PROVIDER_METHODS[p])) == "function" then
                if #provNodes < 8 and not nodeSeen[node] then
                    nodeSeen[node] = true
                    provNodes[#provNodes + 1] = { node = node, at = here }
                end
                break
            end
        end
        for i = 1, #PIN_FIELDS do
            local qid = tonumber(ReadField(node, PIN_FIELDS[i]))
            if qid then
                if #pins < 60 then pins[#pins + 1] = { id = qid, at = here } end
                if #hosts < 8 and not hostSeen[here] then
                    hostSeen[here] = true
                    hosts[#hosts + 1] = here
                end
                break
            end
        end
        local kids = GQL.Api.ChildFrames and GQL.Api.ChildFrames(node)
        if type(kids) == "table" then
            for j = 1, math.min(#kids, 60) do walk(kids[j], here .. "#" .. j, depth + 1) end
        end
        -- 容器字段也要走：现客户端把数据提供者和 pin 池挂在**帧字段**上，不是子帧
        for f = 1, #PROVIDER_FIELD_NAMES do
            local key = PROVIDER_FIELD_NAMES[f]
            local v = ReadField(node, key)
            if v ~= nil and (Frameish(v) or type(v) == "table") then
                if not fieldSeen[here .. "." .. key] and #fieldHits < 12 then
                    fieldSeen[here .. "." .. key] = true
                    fieldHits[#fieldHits + 1] = here .. "." .. key
                end
                walkContainer(v, here, key, depth)
            end
        end
        -- 根帧还要报它自己有哪些字段：上面那串容器字段名全是猜的，猜中了才走得进去，
        -- 猜不中就只能印出一句"一个都没答"。这份名单是下一版可以写死的真凭据。
        -- 走进去的条件收紧成"像 provider 或像 pin 池"，否则 UIParent 那种字段会把
        -- 整棵帧树拖进这一次取证（扫到别的插件的 questID 还会把 pin 名单搅脏）。
        if depth == 0 then
            local _, vals, _, okIt, why = OwnNames(node)
            frameFieldsOK, frameFieldsWhy = okIt, why
            for j = 1, #vals do
                if vals[j].kind == "table" then
                    frameFields[#frameFields + 1] = vals[j].name
                end
            end
            local function looksLikeBag(nm)
                local low = tostring(nm):lower()
                for i = 1, #FRAME_FIELD_HINTS do
                    if low:find(FRAME_FIELD_HINTS[i], 1, true) then return true end
                end
                return false
            end
            table.sort(frameFields, function(a, b)
                local ha, hb = looksLikeBag(a), looksLikeBag(b)
                if ha ~= hb then return ha end
                return a < b
            end)
            local walked = 0
            for j = 1, #frameFields do
                if walked >= 24 then break end
                local key = frameFields[j]
                local v = ReadField(node, key)
                if Poolish(v) and not fieldSeen[here .. "." .. key] then
                    fieldSeen[here .. "." .. key] = true
                    walked = walked + 1
                    walkContainer(v, here, key, depth)
                end
            end
        end
    end
    --- 容器字段怎么走下去：真对象既可能在值里、也可能当键用（TableUtil 的 activeObjects
    --- 就是键），两边都要下去，只认值就扫不到。猜的那串名字和自己报的这串共用这条路。
    function walkContainer(v, here, key, depth)
        walk(v, here .. "." .. key, depth + 1)
        if type(v) == "table" then
            local n = 0
            for k2, v2 in pairs(v) do
                n = n + 1
                if n > 60 then break end
                walk(k2, here .. "." .. key .. "[]", depth + 1)
                walk(v2, here .. "." .. key .. "[ " .. tostring(k2) .. " ]", depth + 1)
            end
        end
    end
    walk(frame, "WorldMapFrame", 0)

    out[#out + 1] = ("  帧侧走查：扫过 %d 个对象  容器字段答出=%s")
        :format(scanned, #fieldHits > 0 and table.concat(fieldHits, " ")
            or "一个都没答（这客户端把 provider 藏在别处，只能靠 ① 那两个入口）")
    out[#out + 1] = ("  世界地图帧自己报出的表字段名=%s%s")
        :format(frameFieldsOK
            and (#frameFields > 0 and Capped(frameFields, 40)
                or "（pairs 走通了，可帧上一个表字段都没有 ⇒ provider 不在帧字段这条路上）")
            or ("问不出：pairs 走不动（" .. tostring(frameFieldsWhy) .. "）"),
            #frameFields > 0 and
                "（上面那串是猜的名字，这一串是它自己报的 —— 下一版写死容器字段名照这一串写）"
                or "")

    -- ③a provider 自己的说法：身份 → 它手里准备画的那份集合 → 三个判据按签名问。
    -- 顺序不能倒过来：上一版只有判据那一问，于是同一句 `ShouldShowQuest` 一次答 false、
    -- 一次报 `info` 是个数，两份答案谁也不认谁 —— 集合与签名先把"该递什么"钉下来。
    if #provNodes == 0 then
        out[#out + 1] = ("  帧侧判据：没有任何对象带 ShouldShowQuest / ShouldMapShowQuest / IsQuestSuppressed"
            .. "（扫了 %d 个）⇒ 渲染侧那道门本轮问不出，地图没开时这些 provider 多半还不存在"
            .. "：请把 %s 打开、停在屏幕上再敲一次这条命令")
            :format(scanned, MapTag(ownMap))
    else
        out[#out + 1] = ("  帧侧 provider %d 个（每个都问身份、它自己的集合、三个判据）：")
            :format(#provNodes)
        for i = 1, #provNodes do
            local node, at = provNodes[i].node, provNodes[i].at
            local idParts = {}
            local gm = ReadField(node, "GetMap")
            if type(gm) == "function" then
                local okG, mv = TryCall(function() return gm(node) end)
                idParts[#idParts + 1] = "GetMap=" .. tostring(tonumber(mv) or mv)
            end
            local gpt = ReadField(node, "GetPinTemplate")
            if type(gpt) == "function" then
                local okG, tv = TryCall(function() return gpt(node) end)
                idParts[#idParts + 1] = "pin模板=" .. tostring(okG and tv or ("报错:" .. tostring(tv)))
            end
            local mf = ReadField(node, "mapID")
            if mf ~= nil then idParts[#idParts + 1] = "mapID=" .. tostring(mf) end
            out[#out + 1] = ("    %s  身份：%s"):format(at,
                #idParts > 0 and table.concat(idParts, "  ") or "（GetMap/GetPinTemplate/mapID 都没有）")

            -- 它自己报名字（先于集合）：上一版这里只会说"试了 GetWorldQuests/quests/… 都没答出表"，
            -- 名单是我们猜的，猜空了就等于没问。
            local fns, vals, ownN, okIt, why = OwnNames(node)
            out[#out + 1] = ("      它自己身上的名字（pairs 走自身字段，共 %d 项）函数名=%s")
                :format(ownN, okIt
                    and (#fns > 0 and Capped(fns, 30) or "（一个函数名都没扫到）")
                    or ("问不出：pairs 走不动（" .. tostring(why) .. "）"))
            local valNames = {}
            for j = 1, #vals do
                local kind = vals[j].kind
                -- 表只报类型（值是一大坨，摊开会挤爆窗口）；布尔/数字/字符串就把值印出来：
                -- 上一版 `matchWorldMapFilters:boolean` 让人以为问了个方法，其实它是个开关，
                -- 而开关此刻是 true 还是 false 才是这一格的答案。
                if kind == "boolean" or kind == "number" or kind == "string" then
                    valNames[j] = ("%s=%s"):format(vals[j].name, tostring(vals[j].value))
                else
                    valNames[j] = vals[j].name .. ":" .. kind
                end
            end
            out[#out + 1] = ("      值字段=%s"):format(#valNames > 0 and Capped(valNames, 30)
                or (okIt and "（身上除了函数就只剩数字/布尔，没有表可当集合）" or "（同样问不出）"))

            -- 它自己的集合：这才叫"渲染这层有没有它"。行也顺手留着 —— 暴雪报错里的局部名
            -- （`info` 是个数 / `questInfo` 是 nil）说这些方法要的是**一行数据**，
            -- 拿它自己集合里那一行再问一遍，才算把"该递号还是该递表"交给客户端答。
            local gotSet, rowFor, firstRow, tried = false, {}, nil, {}
            local function noteRows(tbl)
                pcall(function()
                    local cnt = 0
                    for k, v in pairs(tbl) do
                        cnt = cnt + 1
                        if cnt > 60 then break end
                        local row = (type(v) == "table" and v) or (type(k) == "table" and k)
                        local rid = row and tonumber(ReadField(row, "questID")
                            or ReadField(row, "qID"))
                        if rid and rid ~= 0 then
                            if not rowFor[rid] then rowFor[rid] = row end
                            if not firstRow then firstRow = row end
                        end
                    end
                end)
            end
            local function reportSet(key, tbl, label)
                local ids = {}
                CollectQuestIDs(tbl, ids, 40)
                noteRows(tbl)
                local st = ScanInfoTable(tbl, questID, ownMap, ownParent)
                local mine = nil
                for k = 1, #ids do if ids[k] == questID then mine = true break end end
                out[#out + 1] = ("      %s %s=%d 条 含这一条=%s  前 %d 条：%s%s")
                    :format(label, key, #ids, mine and "是" or "否",
                        math.min(#ids, 12), table.concat(ids, " ", 1, math.min(#ids, 12)),
                        st.zeroID > 0 and ("  池里另有 " .. st.zeroID
                            .. " 个对象没挂任务号（questID=0/nil ⇒ 这是空 pin，不是池子是空的）") or "")
                return mine
            end
            for s = 1, #PROVIDER_SETS do
                local key = PROVIDER_SETS[s]
                local v = ReadField(node, key)
                local tbl = nil
                -- 注意这里不能用 Api.Callable：它是 `fn ~= nil`（判接口存在用的），
                -- 拿来判字段会把**表**也当函数调 —— 上一版 `quests() 报错：attempt to call a table value`
                -- 就是这么来的，而且它一报错就把这一格的集合整个吞掉，看着像"provider 没有集合"。
                if type(v) == "function" then
                    tried[key] = true
                    local okC, r = TryCall(function() return v(node) end)
                    if okC and type(r) == "table" then tbl = r
                    elseif not okC then gotSet = true
                        out[#out + 1] = ("      %s() 报错：%s"):format(key, r) end
                elseif type(v) == "table" then
                    tbl = v
                end
                if tbl then
                    gotSet = true
                    tried[key] = true
                    reportSet(key, tbl, "它自己的集合")
                end
            end
            -- 猜的名单没答出来的那些表字段，照对象自己报的名单再走一遍（只读字段，不调方法：
            -- 随机调一个暴雪的 Get 方法可能带副作用，读值不会）
            local extra = 0
            for j = 1, #vals do
                if vals[j].kind == "table" and not tried[vals[j].name] and extra < 6 then
                    local v = ReadField(node, vals[j].name)
                    if type(v) == "table" then
                        tried[vals[j].name] = true
                        extra = extra + 1
                        gotSet = true
                        reportSet(vals[j].name, v, "它自己报出的表字段")
                    end
                end
            end
            if not gotSet then
                out[#out + 1] = ("      集合问不出：猜的名单（%s）与它自己报出的字段里都没有表"
                    .. " ⇒ 这一格只能靠下面的判据和帧上的 pin 名单")
                    :format(table.concat(PROVIDER_SETS, "/"))
            end

            local candID, candRow = nil, rowFor[questID]
            local candFrom = "它自己集合里"
            if not candRow and pinInfo then
                -- 帧上那份集合是空的（11-07 实测 provider 的 activePins/owningMap 全 0 条），
                -- 这时唯一不是我们造的行，就是渲染层数据源里那一行。
                candRow, candFrom = pinInfo, pinInfoFrom
            end
            if not candRow and firstRow then
                candRow = firstRow
                candID = tonumber(ReadField(candRow, "questID") or ReadField(candRow, "qID"))
            end
            local answeredByID, answeredByRow = 0, 0
            for p = 1, #PROVIDER_METHODS do
                local mname = PROVIDER_METHODS[p]
                local mfn = ReadField(node, mname)
                if type(mfn) == "function" then
                    local shape = FnShape(mfn)
                    local okCall, r = TryCall(function() return mfn(node, questID) end)
                    out[#out + 1] = ("      %s(%s) -> %s"):format(mname, tostring(questID),
                        okCall and tostring(r) or ("报错：" .. ErrTrace(r)))
                    if okCall then answeredByID = answeredByID + 1 end
                    out[#out + 1] = ("        签名=%s"):format(
                        shape or "问不出（本机没有 debug 库或被保护）⇒ 它要号还是要表看下面两行")
                    if candRow then
                        local rowLabel = candFrom == "它自己集合里"
                            and ("递集合里 %s 那一行"):format(tostring(candID or questID))
                            or ("递 %s 里 %s 那一行"):format(candFrom, tostring(candID or questID))
                        local okR, rr = TryCall(function() return mfn(node, candRow) end)
                        out[#out + 1] = ("      对照 %s(%s) -> %s"):format(mname, rowLabel,
                            okR and tostring(rr) or ("报错：" .. ErrTrace(rr)))
                        if okR then answeredByRow = answeredByRow + 1 end
                    end
                end
            end
            if candRow and answeredByID == 0 and answeredByRow > 0 then
                out[#out + 1] = "      ⇒ 按任务号问全报错、按**它自己集合里那一行**问答得出"
                    .. " ⇒ 这些方法要的是世界任务信息那一行，不是任务号：上一版那两处报错"
                    .. "（`info` 是个数 / `questInfo` 是 nil）不是证据，这一版这两行才是"
            elseif candRow and answeredByID > 0 then
                out[#out + 1] = ("      ⇒ 两种递法各有答得出的（按号 %d 个、按行 %d 个）"
                    .. " ⇒ 号这一递不是全错，下面那份 pin 名单仍是主要凭据")
                    :format(answeredByID, answeredByRow)
            elseif not candRow then
                out[#out + 1] = "      它自己的集合里没扫出带 questID 的行、渲染层名单那一段也没给出行"
                    .. " ⇒ 没法按「一行数据」再问一遍，形状这一问本轮仍未决（照上面两行的报错形状回传即可）"
            end
        end
        out[#out + 1] = ("      对照：QuestUtils_IsQuestWorldQuest(%s)=%s（生产代码筛池子用的就是它，"
            .. "它答是、provider 却问不出集合 ⇒ 「板面认它是世界任务、渲染层没有它」就成立了）")
            :format(tostring(questID), tostring((function()
                local fn = GQL.Api.Resolve("QuestUtils_IsQuestWorldQuest")
                if not GQL.Api.Callable(fn) then return "本机没这条" end
                local ok, v = pcall(fn, questID)
                return ok and tostring(v) or ("报错:" .. tostring(v))
            end)()))
        out[#out + 1] = "      不递假表：WorldMap_DoesWorldQuestInfoPassFilters 之流要的是真 worldQuestInfo，"
            .. "本机 C_WorldQuests/C_WorldQuestDB 全 MISSING、拿不到这份表 ⇒ 硬造一份就是自己骗自己，这里宁可不问"
            .. "（上面那些「对照」递的行，要么来自 provider 自己集合里扫出来的，要么来自渲染层名单答出来的，不是造的）"
    end

    -- ③b 帧上已画出的 pin：只有"屏幕上就是这一条的图（或它的父图）"时才许下结论。
    -- 11-07 那份报告拿 2393(银月城) 上的名单说"渲染这层没有它"，是我唬人 —— 拦下来。
    if #pins == 0 then
        out[#out + 1] = ("  此刻帧上带 questID 的 pin=0 个（扫 %d 个对象）⇒ 地图没开、这张图上没画任何任务，"
            .. "或者 pin 挂在走查没走到的容器里（看上面「容器字段答出」那格）。"
            .. "请把 %s 这张图打开、让它停在屏幕上再敲一次这条命令")
            :format(scanned, ownParent and MapTag(ownParent) or MapTag(ownMap))
        return out
    end
    local list, inPins, zeroPins = {}, nil, 0
    for i = 1, #pins do
        if pins[i].id == 0 then zeroPins = zeroPins + 1 end
        if i <= 25 then list[i] = tostring(pins[i].id) end
        if pins[i].id == questID then inPins = true end
    end
    local onOwnMap = dispID ~= nil and (dispID == ownMap or (ownParent and dispID == ownParent))
    out[#out + 1] = ("  此刻帧上带 questID 的 pin=%d 个（显示图=%s，最多列 25）：%s%s"):format(
        #pins, dispID and MapTag(dispID) or "问不出", table.concat(list, " "),
        zeroPins > 0 and ("  其中 questID=0 的 %d 个是池子里没挂任务的壳，不算画了任务")
            :format(zeroPins) or "")
    if zeroPins == #pins then
        out[#out + 1] = ("  ⇒ 这份名单一条真任务号都没有 ⇒ 「这一条不在名单里」这句就没有区分度了"
            .. "（全场都是空壳）：要么这张图此刻确实没画任何任务，要么任务号不放在 questID/qID 这两个字段上")
    end
    out[#out + 1] = "  pin 挂在这些对象上（下一版要写死容器名就照这份写）：" .. table.concat(hosts, " ")
        .. "；带 `.` 的是走容器字段走到的，不一定是地图帧里的东西"
    if not onOwnMap then
        out[#out + 1] = ("  ⇒ 这份名单是在 %s 上取的，而这一条的图是 %s（父图 %s）"
            .. " ⇒ 它只能证明「扫到的对象是活的」，**不能**拿来下"
            .. "「渲染这层没有它」的结论。请把 %s 打开、停在屏幕上再敲一次这条命令，"
            .. "那一份名单才算数")
            :format(dispID and MapTag(dispID) or "显示图问不出", MapTag(ownMap),
                ownParent and MapTag(ownParent) or "问不出",
                ownParent and MapTag(ownParent) or MapTag(ownMap))
    else
        if inPins then
            out[#out + 1] = ("  ⇒ 这一条 %s 在这份 pin 名单里 ⇒ 用户看到的「看不到」就该是位置/遮挡/缩放，不是客户端不给画")
                :format(tostring(questID))
        elseif zeroPins == #pins then
            out[#out + 1] = ("  ⇒ 这一条 %s 不在名单里，但这份名单里也没有**任何**一条真任务号（全是 questID=0 的空壳）"
                .. " ⇒ 这句没有区分度，不能定罪：屏幕上若还看得见别的任务 pin，就是走查没走到它们身上那个字段，"
                .. "屏幕上本来就空 = 这一整张图都没画任务")
                :format(tostring(questID))
        else
            out[#out + 1] = interiorNote
                and ("  ⇒ 这一条 %s 不在这份 pin 名单里，但 %s ⇒ 「地图上看不到 pin」是这一类任务的"
                    .. "**预期**，不是缺陷（11-07 用户人到场实测：任务确实在，pin 本来就不画）。"
                    .. "矩阵有它没有错，别拿这一格当病查")
                    :format(tostring(questID), interiorNote)
                or ("  ⇒ 这一条 %s 不在**当前显示这张图**的 pin 名单里 ⇒ 与用户所见一致："
                    .. "板面清单在列它、渲染这层却没有它。同屏其它 pin 都在 = 这份名单是真画出来的东西，"
                    .. "不是又一个接口口径")
                    :format(tostring(questID))
        end
    end
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
    -- 矩阵悬停里那句"这行还在列是因为 X 本轮读到了金币 Y"第 33 轮删了（界面只放玩家
    -- 这一眼要用的东西），这份归属只在这里报：哪些号本轮真读到过金币、各读到多少。
    -- GoldReaders / CharRewardMoney 都只读账本，零接口调用。
    local readers = GQL.Ledger.GoldReaders and GQL.Ledger.GoldReaders(db, questID)
    if readers and #readers > 0 then
        local parts = {}
        for i = 1, #readers do
            parts[#parts + 1] = ("%s=%s 铜"):format(readers[i],
                tostring(GQL.Ledger.CharRewardMoney(db, readers[i], questID)))
        end
        -- 【第 39 轮改口】这句括号以前无条件写"矩阵那一行就是靠它们进列的"，是谎报：
        -- 读到过金币 ≠ 过了金币门（91594 真档读到 34 金、门槛 500 金，行是靠"本轮交过"
        -- 那道门顶着的，跟这些读者一点因果关系都没有）。现在只有金额真够门槛时才声称因果，
        -- 否则只报"本轮读到金币的号：…"这份事实，判据的因果不归这里编。
        local money = GQL.Ledger.QuestMoney(db, questID)
            or (GQL.Ledger.ManualMoneyByQuest and GQL.Ledger.ManualMoneyByQuest(db) or {})[questID]
        local goldDoor = type(money) == "number" and money >= floorMoney
        out[#out + 1] = "金币读者：" .. table.concat(parts, "  ")
            .. "（本轮读到金币的号"
            .. (goldDoor and "：矩阵那一行就是靠它们进列的"
                or "；金额没到门槛，这一行不是靠金币进列的") .. "）"
    else
        out[#out + 1] = "金币读者：无（本轮没有任何号读到过这条的金币）"
    end
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
    local mapIDs = (GQL.Config and GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
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
    local mapIDs = (GQL.Config and GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
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
    -- 三把键的**到期待遇**不是一件事，头一句就得说清（用户 2026-11-02 定稿 + T6b）：旧那句
    -- "账本里的注册周常共 N 条"把 pin: 那一路也算成常驻周常，取证的人照着它会去问"为什么它
    -- 不到周重置不消失"，而它本来就该到点消失 —— 口径按 L.IsPinRegKey 分岔。
    -- 界面措辞保持玩家语言，这些细节只写在 dump/probe 两个取证入口。
    local isPin = GQL.Ledger.IsPinRegKey or function() return false end
    local kwPre = GQL.Ledger.KW_PREFIX or "kw:"
    local nPin, nKw, nPreset = 0, 0, 0
    for i = 1, #reg do
        local k = reg[i].key
        if isPin(k) then
            nPin = nPin + 1
        elseif type(k) == "string" and k:sub(1, #kwPre) == kwPre then
            nKw = nKw + 1
        else
            nPreset = nPreset + 1
        end
    end
    out[#out + 1] = ("账本里的注册行共 %d 条：内置周常 %d、关键词点名（kw:）%d、"
        .. "金币门从事件 pin 收的普通世界金币任务（pin:）%d —— 前两类是常驻周常，到期只按 CD "
        .. "尾巴算；pin: 那一路**不常驻**，上了板的按自己那条实例的板面结束时刻到点即到期，"
        .. "压根没上过板的按「本周没被看见过 ⇒ 到期」清")
        :format(#reg, nPreset, nKw, nPin)
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
        out[#out + 1] = ("  regKey=%s 族=%s questID=%s 名=%s 地图=%s 分组=%s 归属=%s 占行=%s 在图上=%s 本周状态=%s 到期=%s"):format(
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
            tostring(rec and GQL.Ledger.RegisteredState(db, charKey, reg[i].questID)),
            -- 这一行的到期口径直接印出来，玩家/维护者不必再回去推前缀（判据只有 L.IsPinRegKey
            -- 与 L.SetExpiry 那两处，报告只是把结论念一遍，不另算一套）
            isPin(reg[i].key)
                and (type(rec) == "table" and type(rec.roundEnd) == "number"
                    and ("自己那条实例的板面结束时刻 " .. tostring(rec.roundEnd))
                    or "问不出自己的结束时刻：本周没被看见过就到期（最坏下次周重置）")
                or "常驻：只按 CD 尾巴算")
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
--- · 到期时刻从哪来（这条自己的板面倒计时 / 借不到的话就是周 CD 尾巴 ——
---   老"排程猜的"那 24 小时随 T5 整组退役，从此没有第三种）。
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
        local exp = GQL.Ledger.EffectiveExpiry(db, rec, now)
        local expText
        if type(exp) == "number" then
            -- 数跟着来源说：roundEnd 在场 = 这条自己答出的板面倒计时，
            -- 没有 = 按周 CD 尾巴（cycleAnchor + 604800）算的结束时刻。
            expText = ("%s（%s）"):format(GQL.Config.FormatTimeLeft(exp - now),
                tonumber(rec.roundEnd) and "板面倒计时" or "周 CD 尾巴")
        elseif tonumber(rec.roundEnd) then
            expText = "无（本轮还没读到，账本里那条是上一条实例留下的）"
        else
            expText = "无（接口从没答过它的剩余）"
        end
        out[#out + 1] = ("  #%s %s 图=%s 这轮在板上=%s 在池=%s 状态=%s 谁答的=%s 到期=%s"):format(
            tostring(questID), tostring(GQL.Ledger.QuestName(db, questID)),
            MapTag(rec.mapID),
            tostring(not not rec.onBoard),
            -- 池问不出必须写判不了：空表读成"全都不在池里"会把每条都冤枉成上周的残留
            (type(inPool) == "table") and tostring(not not inPool[questID]) or "判不了",
            tostring(GQL.Ledger.State(db, charKey, questID)),
            who,
            expText)
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

-- tooltipWidgetSet 的 widget 文本取证（2026-11-01 用户悬停实测：亚基入侵做过本周宝箱的角色，
-- 地图悬停奖励行只剩徽章、不再显示宝箱；而 pin 表的 description 是空串、正文却显示在
-- tooltip 里 ⇒ 正文+奖励行全部来自 tooltipWidgetSet。UI widget 是服务器按玩家下发的，
-- 这可能是"本角色本周做过没"的凭据。这里只摊数据，判定留给生产。
-- 2026-11-01 双角色对照实测：差异全在一条 type=27 的 widget 上（做过的 #8404 / 没做过的
-- #8235，在清单里的位置也不同），手抄的 8 个常用 getter 对它全问不出 ⇒ getter 名单不
-- 手抄，把 C_UIWidgetManager 里所有 Get*VisualizationInfo 全枚举试一遍，答得出就摊。）
local function WidgetStrings(v, acc, depth)
    if type(v) == "string" then
        if v ~= "" then acc[#acc + 1] = v end
    elseif type(v) == "table" and depth < 4 then
        for _, sub in pairs(v) do WidgetStrings(sub, acc, depth + 1) end
    end
end

local function WidgetTextLines(widgetSetID)
    local out = {}
    local listFn = GQL.Api.Resolve("C_UIWidgetManager.GetAllWidgetsBySetID")
    if not GQL.Api.Callable(listFn) then
        out[#out + 1] = "    widget集=" .. tostring(widgetSetID) .. " 入口 C_UIWidgetManager.GetAllWidgetsBySetID 缺失"
        return out
    end
    local okCall, widgets = pcall(listFn, widgetSetID)
    if not (okCall and type(widgets) == "table" and #widgets > 0) then
        out[#out + 1] = "    widget集=" .. tostring(widgetSetID) .. " 问不出（事件换阶段/换图时 widgetSet 号会变）"
        return out
    end
    local getters = {}
    local mgr = GQL.Api.Resolve("C_UIWidgetManager")
    if type(mgr) == "table" then
        for name, fn in pairs(mgr) do
            if type(fn) == "function" and name:match("^Get.+VisualizationInfo$") then
                getters[#getters + 1] = { name = name, fn = fn }
            end
        end
        table.sort(getters, function(a, b) return a.name < b.name end)
    end
    for w = 1, #widgets do
        local wi = widgets[w]
        local wid = type(wi) == "table" and tonumber(wi.widgetID) or nil
        out[#out + 1] = ("    widget#%s type=%s"):format(
            tostring(wid), tostring(type(wi) == "table" and wi.widgetType or nil))
        if wid then
            for g = 1, #getters do
                local okG, r = pcall(getters[g].fn, wid)
                if okG and type(r) == "table" then
                    out[#out + 1] = ("      %s → %s"):format(getters[g].name, FlatDescribe(r))
                    local texts = {}
                    WidgetStrings(r, texts, 0)
                    if #texts > 0 then
                        out[#out + 1] = "      文本=" .. table.concat(texts, " ｜ ")
                    end
                end
            end
        end
    end
    return out
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
        or (GQL.Config.TrackedMapIDs and GQL.Config.TrackedMapIDs()) or {}
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
                    local ws = type(info) == "table" and tonumber(info.tooltipWidgetSet) or nil
                    if ws and ws > 0 then
                        local wls = WidgetTextLines(ws)
                        for wi = 1, #wls do out[#out + 1] = wls[wi] end
                    end
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
        -- 常驻周常那两道凭据与矩阵同源（布尔位 + 这一条实例的日志编号，唯一读点
        -- Ledger.KeywordLogAdmits）：取证面只认布尔位的话，换个号登录之后这里就报"没有常驻
        -- 周常"，而界面上那行还顶着编号在列 —— 第 34 轮的教训就是两边口径分岔。
        if rec.regKey or rec.kwLogAdded
            or (GQL.Ledger.KeywordLogAdmits and GQL.Ledger.KeywordLogAdmits(pdl, questID, rec)) then
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
            if #picks < 3 and rec and (rec.regKey or rec.kwLogAdded
                    or (GQL.Ledger.KeywordLogAdmits
                        and GQL.Ledger.KeywordLogAdmits(db, questID, rec)))
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
    -- 【11-07 改】这两条以前一律按 (questID, uiMapID) 两参问，结果本机所有样本、所有图
    -- 都是"没有一张说在"（docs/probe.txt:470）—— 一个从不说 true 的入口不是"证据不足"，
    -- 是形状喂错了（DoesMapShowTaskQuestObjectives 真签名只要 uiMapID，第一个参数递号
    -- 进去它就是拿 96329 当图号查）。现在先用 0 参撞 Usage、再按撞出来的形状问。
    local onMapPaths = { "C_QuestLog.IsOnMap", "C_TaskQuest.DoesMapShowTaskQuestObjectives" }
    out[#out + 1] = "---- 按图问\"这条在不在这张图\"（先撞 Usage，再按真形状问；单参入口只问一次）"
    for p = 1, #onMapPaths do
        local path = onMapPaths[p]
        local fn = Api.Resolve(path)
        if not GQL.Api.Callable(fn) then
            out[#out + 1] = ("  %s MISSING"):format(path)
        else
            local usage = UsageOf(fn)
            local shape = ShapeOfUsage(usage)
            out[#out + 1] = ("  %s  用法=%s  按%s问")
                :format(path, tostring(usage or "0 参没报错（用法撞不出来）"), ShapeText(shape))
            local noneYes, tagged = 0, 0
            for i = 1, #picks do
                local q = tonumber(picks[i].questID)
                if q then
                    local yes, errs, answered, single = {}, 0, 0, nil
                    if shape == "quest_only" then
                        -- 用法里没有图这一维 ⇒ 逐张问只是把同一个问题问 N 遍（11-07 实测
                        -- `C_QuestLog.IsOnMap` 就是这个形状，那份报告印出 8 行一样的 false，
                        -- 看着像"8 张图全答不在"）。问一次，并照实说它定不了图。
                        local okCall, v = CallShape(fn, shape, q, nil)
                        answered = okCall and 1 or 0
                        errs = okCall and 0 or 1
                        single = tostring(v)
                    else
                        for m = 1, #maps do
                            local mapID = tonumber(maps[m])
                            if mapID then
                                local okCall, v = CallShape(fn, shape, q, mapID)
                                if not okCall then errs = errs + 1
                                else
                                    answered = answered + 1
                                    if v == true then yes[#yes + 1] = mapID end
                                end
                            end
                        end
                    end
                    local tag
                    if single then
                        tag = ("单参入口不随图变（问一次就够）答 %s ⇒ 它答不出「在哪张图」，"
                            .. "这一条不能当图凭据"):format(single)
                    elseif answered == 0 then tag = ("全报错（%d 张）⇒ 形状还是不对"):format(errs)
                    elseif #yes == 0 then
                        noneYes = noneYes + 1
                        tag = ("没有一张说在（答了 %d 张）"):format(answered)
                    elseif #yes == answered then tag = ("每张都说在 = 不按图区分，这条凭据作废")
                    else
                        local parts = {}
                        for k = 1, #yes do parts[k] = MapTag(yes[k]) end
                        tag = ("说在的图 " .. table.concat(parts, " "))
                    end
                    tagged = tagged + 1
                    out[#out + 1] = ("  「%s」-> %s"):format(picks[i].name, tag)
                end
            end
            if shape == "map_only" and tagged > 0 and noneYes == tagged then
                -- docs/probe.txt:470 那份回传里，这个入口对**每条样本任务**都是"没有一张说在"。
                -- 一个从不说 true 的图级入口没有区分度：那一句不是"这条不在这张图"的证据，
                -- 拿它去归组就是把接口形状问题记成数据问题。
                out[#out + 1] = ("  ⇒ 上面 %d 条全答「没有一张说在」⇒ 这个入口在本机不区分图，"
                    .. "那一句不是图凭据（既不能定罪也不能洗清），别记进判据")
                    :format(tagged)
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
    add({ "---- 板面清单里这一条的原始行（行里有没有时间字段 / 这张图的板面数据到没到）" })
    add(EvidenceBoardRow(questID))
    add({ "---- 地图到底画不画这一条（客户端自己的说法，不是板面清单的说法）" })
    add(EvidenceMapRenders(questID))
    add({ "---- 金额是谁答的（矩阵金币门槛的输入）" }); add(EvidenceMoney(questID))
    add({ "---- 地图是谁答的" }); add(EvidenceMap(questID))
    add({ "---- 周期判据候选（这条到底算不算周常）" }); add(EvidenceCadence(questID))
    return lines
end

--- 【11-04 取证】`/gql probe fetchdata`：不按 M，能不能当场把这一条任务的金币问出来？
--- 用户 11-04 定的取证方向："我知道任务名的前提就是先按 M，我觉得更应该取证的是你说的
--- 通过代码达到按 M 的效果，不要用你说的任务池，就直接读数据。"
--- ⇒ 这一趟**只看"直接问这一条任务要数据"这一条路**，不看那张图的清单；
---   任务号从"刚刚金额问不出"的那一条自己拿（`GQL.Sync.LastMoneyMiss`），
---   所以玩家不用知道任务名、不用开地图、不用跑任何路 —— 敲一条命令就行。
--- **这一轮只取证**：一个字不写账本、一个字不动判据（读出来的金额只进日志）。
---   等回传定案"哪个动作问得出来"，再决定把谁放进生产链。
--- 名字一个都不许当成已确认：`C_QuestRewards.FetchData` 的参数个数没有任何可靠依据
---   （warcraft.wiki 与 wiki.gg 都没有这一页，本机装的四十多个插件里除了咱们自己没一个调用点），
---   所以两种写法都试，把客户端自己的报错原文抄进日志 —— 它说怎么给就是怎么给。
--- 代价：只在这条命令上发生。≤5 次"要数据/改选中"调用 + ≤10 次复看
---   （起点 1、每个动作之后各 1、延时里 5，每次 1×HaveQuestRewardData + 金额候选链）
---   + ≤6 次选中项读口调用（起点挑读口 1、动作3 每按一下各 1、恢复每候选各 1；
---     只在读回对不上时才继续按下一候选，这台 12.1.5 实测 3 下）+ 1 次等效按 M
---   （与玩家自己按一次 M 一模一样）+ 4 次事件名注册试探。
---   延时是一次性的 6 条（+1/+3/+8 直读复看、+12 等效按 M、+14/+18 对照复看），
---   跑完即散，无常驻定时器、无轮询、无扫图；事件名只在这 18 秒窗口里挂着，窗口末尾注销
---   （连敲两次按次数计数，最后一次收尾才注销，别把第二次那趟的窗口提前掐掉）。
---
--- 【11-04 第一次回传把机械那半问死了】（12.1.5 build 70688，敲的是 `/gql probe fetchdata 89267`）：
---   · `C_QuestRewards.FetchData` **本机根本没有**（入口缺失 MISSING）⇒ 参数个数那一问作废，别再试；
---   · `QUEST_REWARD_DATA_RECEIVED` / `QUEST_REWARD_SET_CHANGED` / `TASKPOOLS_LOADED` 三个名字
---     客户端原话 `Attempt to register unknown event` ⇒ **没有"奖励数据到了"的通知**；
---     对照组 `QUEST_LOG_UPDATE` 注册=有、并且真收到（后面读回那几行就是它带的）⇒ 补读只能继续搭它；
---   · `C_TaskQuest.RequestPreloadRewardData` 存在、不抛错；那一趟起点 奖励数据=false，
---     一秒后就成 true，而等效按 M 排在第 12 秒才发起 ⇒ **数据是自己回来的，不是按 M 拉回来的**；
---   · 唯独金额没跟着来 —— 挑中的这条（89267 神秘身影）**本来就不给金币**，生产那一路同一趟已经
---     写明"奖励数据本来就在本地，这一档客户端答的是不给金币"。
---     ⇒ "金额能不能不按 M 读出来"要换一条真给钱的再看；而日志 44212 / 44289 那两行
---     （`补读到金额 …（矩阵没开过也进账了）`）其实已经答过同一件事。
---   · 选中项那一头露出两个真缺陷：`ClearQuestLogSelection` 本机没有、`GetQuestLogSelection` 答 nil
---     ⇒ 上一版把 89267 留在任务日志里没撤掉；顺带"已调用"并不等于真选中。
---     下面这一版改成"每按一下都读回来对一遍、对不上就照实说"，并按读口的种类配写口。
---
--- 【11-04 第二次回传（`/gql probe fetchdata 92140`，12.1.5 70688）】恢复那一段照计划走通了，
---   两件事定案、一件照实说：
---   · **不按 M 也把金额问得出来**：日志 59151 那条 92140 接任务时 奖励数据=false、金额=nil，
---     生产路径只干了一件事——向服务器要这份数据（那行括号里写的就是它）；24 秒后探针起点已经
---     读到 奖励数据=true 金额=9664484，而这一整趟 `世界地图=没开`、等效按 M 排在探针第 12 秒
---     才发起 ⇒ 用户问的那条路成立。同一趟 59187 又写了一行 `补读到金额 …（矩阵没开过也进账了）`，
---     加上 44212 / 44289 / 55783（92576 那笔今天也进了）—— **生产链已经在这么干了，一个字不用改**。
---   · `C_QuestLog.SetSelectedQuest` 这台客户端收的是**任务号**不是日志序号
---     （`按日志序号 23=已调用→不是这条(读回0)`、`按任务号 92140=已调用→就是这条`）
---     ⇒ "先试序号"这一问的来路（AutoTurnIn 传日志序号）被客户端自己的答案否掉，改成按读口种类配先后，
---       省一次白烧的接口调用；恢复那三个候选同理，本机第一下就退成"没选中"（`读回0`），
---       上一版遗留的"把任务留在玩家日志里"这个缺陷已经收干净。
---   · 照实说：92140 起点就读得出 ⇒ "探针里哪一个动作问出来的"这一问在这趟**没有答案**（收尾写的是
---     "起点本来就读得出（这一条不用取证）"）。用户 11-04 也确认"目前没这种任务"——
---     金额问不出、又真给金币、还不在矩阵里的对象当下拿不到 ⇒ 这一问留档，不再让他满世界找靶子。
local REWARD_PROBE_EVENTS = {
    -- 前三个是**待验候选**（名字来路：按接口命名规律猜的，本机认不认由这一行的答案说了算）；
    -- QUEST_LOG_UPDATE 是已知对照组 —— 它必须报"有"，报"无"就说明这一段本身跑歪了，
    -- 不能拿它的 MISSING 当"这台客户端没有数据到了的通知"的证据。
    "QUEST_REWARD_DATA_RECEIVED", "QUEST_REWARD_SET_CHANGED", "TASKPOOLS_LOADED",
    "QUEST_LOG_UPDATE",
}
local rewardProbeFrame, rewardProbeRegistered, rewardProbeRuns
local function TryProbeEvent(name)
    if not CreateFrame then return "判不了(本机没 CreateFrame)" end
    if not rewardProbeFrame then
        rewardProbeFrame = CreateFrame("Frame")
        rewardProbeRegistered = {}
        rewardProbeFrame:SetScript("OnEvent", function(_, ev)
            GQL.Config.Log("直读试探 事件到了 " .. tostring(ev))
        end)
    end
    local okCall, err = pcall(rewardProbeFrame.RegisterEvent, rewardProbeFrame, name)
    if not okCall then return ("MISSING(%s)"):format(tostring(err)) end
    rewardProbeRegistered[#rewardProbeRegistered + 1] = name
    return "有"
end
--- 收尾时按"还差几趟没收尾"计数注销：连敲两次时第一次的 +18 秒不许把第二次那趟的窗口掐掉
--- （QUEST_LOG_UPDATE 挂久了每次任务日志变化都往日志里塞一行，是噪音，所以窗口必须关）。
local function DropProbeEvents()
    rewardProbeRuns = (rewardProbeRuns or 1) - 1
    if rewardProbeRuns > 0 then return end
    rewardProbeRuns = 0
    if not rewardProbeFrame then return end
    for i = 1, #(rewardProbeRegistered or {}) do
        pcall(rewardProbeFrame.UnregisterEvent, rewardProbeFrame, rewardProbeRegistered[i])
    end
    rewardProbeRegistered = {}
    GQL.Config.Log("直读试探 事件名试探已注销（只在这 18 秒窗口里挂着）")
end

--- 地图到底开没开：帧问不到就写"判不了"，不许把"没这个帧"说成"没开"（谎报现状）。
local function WorldMapShownText()
    local wmf = GQL.Api and GQL.Api.Resolve and GQL.Api.Resolve("WorldMapFrame")
    if not wmf or not wmf.IsShown then return "判不了(没 WorldMapFrame)" end
    local okCall, shown = pcall(wmf.IsShown, wmf)
    if not okCall then return "判不了" end
    return shown and "开着" or "没开"
end

--- 日志序号：本仓库已经在用这个名字（上面 SampleLogIndex 拿它反查），
--- 老名 GetQuestLogIndexByID 本机存不存在由这一行的答案说，不许当前提。
--- 第二个返回值是"判不了的原因"（11-04 回传只有光秃秃一个"判不了"，分不清是入口不在还是这条没进日志）。
local function LogIndexOf(questID)
    local fn = GQL.Api.Resolve("C_QuestLog.GetLogIndexForQuestID")
    if not (GQL.Api.Callable and GQL.Api.Callable(fn)) then return nil, "入口缺失" end
    local okCall, idx = pcall(fn, questID)
    if not okCall then return nil, "抛错" end
    local num = tonumber(idx)
    if not num then return nil, "答不出" end
    if num == 0 then return nil, "答 0(没在日志里)" end
    return num
end

--- 现在任务日志里选中的是哪一条：`GetQuestLogSelection` 11-04 实测在这台客户端答 nil（问不出东西），
--- 而 Pawn / TipTac 用的是 `C_QuestLog.GetSelectedQuest` ⇒ 两个读口都问，只把**真答出来的那一个**当数。
--- 这一步不是取证的花边：动作 3 要改的是玩家正在看的选中项，读不回原值就等于撤不干净。
local function SelectedNow()
    local paths = { "C_QuestLog.GetSelectedQuest", "GetQuestLogSelection" }
    for i = 1, #paths do
        local fn = GQL.Api.Resolve(paths[i])
        if GQL.Api.Callable(fn) then
            local okCall, v = pcall(fn)
            if okCall and tonumber(v) then return paths[i], tonumber(v) end
        end
    end
    return nil, nil
end

--- 复看一次。金额候选一律走 GQL.Api.QuestRewardMoney（生产同一份名单，探针不许另抄一份）。
local function ReadReward(questID)
    local has = GQL.Api.HasRewardData and GQL.Api.HasRewardData(questID)
    local money = GQL.Api.QuestRewardMoney and GQL.Api.QuestRewardMoney(questID)
    return has, money
end

--- 挑哪一条做试验：优先"数据还不在本地"的那一条（那才是金额问不出、有得问的一档）。
--- 只剩客户端答过"数据在本地"的那几条时也照用，但话要说在前面（见下面那句提示）。
local function PickMiss()
    local misses = GQL.Sync and GQL.Sync.RecentMoneyMisses and GQL.Sync.RecentMoneyMisses()
    if type(misses) ~= "table" then return nil end
    for i = #misses, 1, -1 do
        if not misses[i].hasData then return misses[i] end
    end
    return misses[#misses]
end

--- 调用一个"向服务器要数据"的入口，只报这台客户端自己的回答，不替它圆场。
local function TryFetch(path, ...)
    local fn = GQL.Api and GQL.Api.Resolve and GQL.Api.Resolve(path)
    if not (GQL.Api and GQL.Api.Callable and GQL.Api.Callable(fn)) then return "入口缺失(MISSING)" end
    local okCall, err = pcall(fn, ...)
    if okCall then return "已调用" end
    return ("抛错:%s"):format(tostring(err))
end

function Probe.RewardDataFetch(arg)
    local out = {}
    local function emit(line)
        out[#out + 1] = line
        GQL.Config.Log(line)
    end
    local digits = tonumber(tostring(arg or ""):match("%d+"))
    local miss = PickMiss() or (GQL.Sync and GQL.Sync.LastMoneyMiss and GQL.Sync.LastMoneyMiss())
    local questID = digits or (miss and tonumber(miss.questID))
    if not questID then
        emit("直读试探 没得试：这次开机里还没有'金额问不出'的记录。接一条或交一条世界任务，"
            .. "再敲 /gql probe fetchdata（想指定某一条就敲 /gql probe fetchdata <任务号>）")
        return out
    end
    rewardProbeRuns = (rewardProbeRuns or 0) + 1
    local baseHas, baseMoney = ReadReward(questID)
    local logIdx, logIdxWhy = LogIndexOf(questID)
    emit(("直读试探 版本=%s quest=%s 世界地图=%s 日志序号=%s 奖励数据=%s 金额=%s")
        :format(BuildInfo(), tostring(questID), WorldMapShownText(),
            tostring(logIdx or ("判不了" .. (logIdxWhy and ("(" .. logIdxWhy .. ")") or ""))),
            tostring(baseHas), tostring(baseMoney)))
    local selReader, selBefore = SelectedNow()
    emit(("直读试探 选中项读口=%s 值=%s（动作 3 要按这个原样退回去）")
        :format(tostring(selReader or "两个读口都答不出"), tostring(selBefore)))
    -- 11-04 回传那三趟挑中的是 89267：数据在本地、金额永远空 ⇒ 客户端答的是"这条不给金币"，
    -- 拿它问不出"金额能不能不按 M 读出来"这个结论。自己挑的时候先把这话说明白。
    if baseHas == true and type(baseMoney) ~= "number" and not digits then
        emit("直读试探 这一条客户端已经答过了：数据在本地、金额就是空 ⇒ 它不给金币，"
            .. "拿它问不出新东西（想试一条真给钱的就敲 /gql probe fetchdata <任务号>）")
    end

    -- 结论由第一个"把金额从问不出变成正数"的动作认领。起点本来就读得出的，后面每一步都不许冒领
    -- （不然一条本来就有钱的任务会被说成是探针问出来的，这份证据就作废了）。
    local answered, answeredBy = false, nil
    if type(baseMoney) == "number" then
        answered = true
        answeredBy = "起点本来就读得出（这一条不用取证）"
    end
    local function note(tag, money)
        if answered or type(money) ~= "number" then return end
        answered = true
        answeredBy = tag
        emit(("直读试探结论 quest=%s 哪一个动作把金额问出来了=%s")
            :format(tostring(questID), tag))
    end
    local function readAndNote(tag)
        local has, money = ReadReward(questID)
        emit(("直读试探 %s quest=%s 奖励数据=%s 金额=%s（账本一个字没写）")
            :format(tag, tostring(questID), tostring(has), tostring(money)))
        note(tag, money)
    end

    -- 动作 1：向服务器要这一条任务的奖励数据（生产链已经在用这一个，`Api.lua:299`）。
    local s1 = TryFetch("C_TaskQuest.RequestPreloadRewardData", questID)
    emit(("直读试探 动作1=C_TaskQuest.RequestPreloadRewardData 状态=%s"):format(s1))
    readAndNote("动作1之后")

    -- 动作 2：C_QuestRewards.FetchData。参数个数只能让客户端自己回答 ⇒ 抛错就再试宽写法。
    local s2 = TryFetch("C_QuestRewards.FetchData", questID)
    emit(("直读试探 动作2=C_QuestRewards.FetchData(只给任务号) 状态=%s"):format(s2))
    readAndNote("动作2之后")
    if s2:find("抛错", 1, true) then
        local s2b = TryFetch("C_QuestRewards.FetchData", questID, true, true, true, true)
        emit(("直读试探 动作2b=C_QuestRewards.FetchData(带上四个 true) 状态=%s")
            :format(s2b))
        readAndNote("动作2b之后")
    end

    -- 动作 3：把这条在任务日志里选中（任务界面自己就是这么干的，AutoTurnIn 有现成调用点），
    -- 探完**立刻恢复** —— 用户 11-04 点头："放进探针，用完恢复"。只在这条手动命令里发生，
    -- 生产路径一次都不许碰这个副作用。
    -- 11-04 第一次回传把这一段的两处窟窿照出来了：`ClearQuestLogSelection` 这台客户端根本没有、
    -- `GetQuestLogSelection` 答 nil ⇒ 选中项改完撤不回去；而"已调用"根本不等于真选中了
    -- （上一版照着 AutoTurnIn 的写法推断 —— 它拿 QUEST_ACCEPTED 的第一个参数（日志序号）调那个入口；
    --  咱们那一趟给的是任务号）。所以这一版改三处：
    --   · 每一下都读回来对一遍，对不上就照实写"不是这条"；
    --   · 恢复单独一段，选没选成都要走；写口按"原值是从哪个读口拿到的"配同一种值
    --     （序号读口配序号写口、任务号读口配任务号写口 —— 串档就是把玩家选中项挪到别条任务上）。
    -- 【11-04 第二次回传（92140）把两下尝试的先后问实了】这台客户端 `SetSelectedQuest` 收的是
    -- **任务号**：`按日志序号 23=已调用→不是这条(读回0)`、`按任务号 92140=已调用→就是这条`
    -- ⇒ 前一版"先试序号"在这台机器上是白烧一次接口调用；现在先后按读口的种类配（任务号读口先按任务号、
    --   日志序号读口先按日志序号），第一下读回对不上才补另一域那一下 —— "已调用"仍可能没真选中，那就两行都留。
    local function selectedState()
        local reader, now = SelectedNow()
        if not reader then return "判不了(两个读口都答不出)" end
        local want = reader == "C_QuestLog.GetSelectedQuest" and questID or logIdx
        if want and now == want then return "就是这条" end
        return ("不是这条(读回%s)"):format(tostring(now))
    end
    local s3 = {}
    -- 先后按读口的种类配：读口答的是任务号就先按任务号写，答的是日志序号就先按序号写；
    -- 第一下没读回"就是这条"才补另一域那一下（入口在、但真没选中，也是走这一步把两行都留在日志里）。
    local tries
    if selReader == nil or selReader == "C_QuestLog.GetSelectedQuest" then
        tries = { { questID, "按任务号" }, { logIdx, "按日志序号" } }
    else
        tries = { { logIdx, "按日志序号" }, { questID, "按任务号" } }
    end
    for i = 1, #tries do
        local value, label = tries[i][1], tries[i][2]
        if value then
            s3[#s3 + 1] = ("%s %s=%s→%s")
                :format(label, tostring(value), TryFetch("C_QuestLog.SetSelectedQuest", value), selectedState())
            if selectedState() == "就是这条" then break end
        end
    end
    emit(("直读试探 动作3=选中这条 %s"):format(table.concat(s3, "；")))
    readAndNote("动作3之后")

    local restored = {}
    local function attemptRestore(path, value)
        local status = value == nil and TryFetch(path) or TryFetch(path, value)
        local _, now = SelectedNow()
        restored[#restored + 1] = ("%s=%s→读回%s"):format(path, status, tostring(now))
        return status, now
    end
    if type(selBefore) == "number" and selBefore > 0 then
        local writer = selReader == "C_QuestLog.GetSelectedQuest"
            and "C_QuestLog.SetSelectedQuest" or "SelectQuestLogEntry"
        local _, now = attemptRestore(writer, selBefore)
        if now ~= selBefore then
            emit(("直读试探 动作3=没退回原选中（原来是 %s，现在读回 %s）"
                .. "—— 这一下是我这边的锅，重开任务界面点一条别的就干净了")
                :format(tostring(selBefore), tostring(now)))
        end
    else
        -- 原本没选中 ⇒ 要退成"没选中"。候选挨个试，读回空了才停；读口全程答不出就别提前停，
        -- 三个都试完（MISSING 的名字只多一行日志，不花接口调用）。
        -- 11-04 第二次回传：这台客户端前两个候选都是 MISSING，按一下各带一次读回 ⇒ 白多两行两下。
        -- 所以先后按读口的种类配（12.x 读口 ⇒ 先用 12.x 的写口），试到空了就停，试不到照旧往下走。
        local clears
        if selReader == "C_QuestLog.GetSelectedQuest" then
            clears = { { "C_QuestLog.SetSelectedQuest", 0 }, { "ClearQuestLogSelection", nil },
                       { "SelectQuestLogEntry", 0 } }
        else
            clears = { { "ClearQuestLogSelection", nil }, { "SelectQuestLogEntry", 0 },
                       { "C_QuestLog.SetSelectedQuest", 0 } }
        end
        for i = 1, #clears do
            local status, now = attemptRestore(clears[i][1], clears[i][2])
            if selReader and status ~= "入口缺失(MISSING)"
                and (now == nil or now == 0 or now == -1) then break end
        end
    end
    emit(("直读试探 动作3=退回原来选中的 %s（原来选中的=%s，读口=%s）")
        :format(table.concat(restored, "；"), tostring(selBefore), tostring(selReader)))

    -- 事件名试探：这台客户端 RegisterEvent 遇到不认识的名字直接抛错
    -- （`Sync.lua` 的 TryRegister 那段注释就是这么否掉两个名字的）⇒ 有就有、没就 MISSING。
    for i = 1, #REWARD_PROBE_EVENTS do
        emit(("直读试探 事件名=%s 注册=%s（只在这 18 秒窗口里挂着）")
            :format(REWARD_PROBE_EVENTS[i], TryProbeEvent(REWARD_PROBE_EVENTS[i])))
    end

    -- 异步回包靠这几条一次性延时接住。对照（等效按 M）排在直读全部结束**之后**，
    -- 不然分不清那个数到底是直读问出来的还是地图拉回来的。
    if C_Timer and C_Timer.After then
        for _, d in ipairs({ 1, 3, 8 }) do
            C_Timer.After(d, function() readAndNote(("稍后+%d秒"):format(d)) end)
        end
        C_Timer.After(12, function()
            GQL.Config.Log(("直读试探 对照=等效按M 已发起 quest=%s"
                .. "（地图开着、或 15 秒内刚做过，它一动不动）"):format(tostring(questID)))
            if GQL.Sync and GQL.Sync.ForceMapDataLoad then
                pcall(GQL.Sync.ForceMapDataLoad)
            end
        end)
        for _, d in ipairs({ 14, 18 }) do
            C_Timer.After(d, function()
                readAndNote(("对照复看+%d秒"):format(d))
                if d == 18 then
                    DropProbeEvents()
                    GQL.Config.Log(("直读试探收尾 quest=%s 哪一个动作把金额问出来了=%s")
                        :format(tostring(questID), tostring(answeredBy or "都没问出来")))
                end
            end)
        end
        emit(("直读试探 已排 6 条一次性延时（+1/+3/+8 秒各复看一次直读，+12 秒发起等效按 M，"
            .. "+14/+18 秒各复看一次对照）；那几行稍后自己进日志，等 20 秒再 `/gql log` 一起发回来。现在%s"):format(
                answered and ("已经问出来了：" .. tostring(answeredBy))
                    or "直读这几下还没问出来"))
    else
        -- 没有延时就没人收尾：当场注销，别让事件白挂到下次登录（QUEST_LOG_UPDATE 每次都塞一行噪音）
        DropProbeEvents()
        emit("直读试探 本机没 C_Timer：延时那几行和对照都没排上，只有上面这几行是证据")
    end
    return out
end

function Probe.Run(arg)
    -- `/gql probe fetchdata [任务号]`：只走"直接问这一条要数据"那一段取证（见上面那段说明），
    -- 不再打整份报告 —— 那段本身就是十几行,混在整份报告里玩家挑不出来。
    if type(arg) == "string" and arg:find("fetchdata", 1, true) then
        return table.concat(Probe.RewardDataFetch(arg), "\n")
    end
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
                        -- 两份日志标记分开报（判据只问 Ledger.KeywordLogAdmits 那一处真源）：
                        -- 布尔位 = 当前这个号日志里挂着；编号 = 这一条实例被某个号的日志证明
                        -- 存在过（换一个没接它的号登录布尔位就被撤、行照旧靠它顶在板上）。
                        -- 这里要是只认布尔位，取证面就会说"不是日志收的"，而界面那行正是
                        -- 日志那份编号撑起来的（第 34 轮"两边口径分岔"的教训）。
                        if rec.kwLogAdded then
                            src = "日志"
                        elseif GQL.Ledger.KeywordLogAdmits
                            and GQL.Ledger.KeywordLogAdmits(db, acc[i].questID, rec) then
                            src = "日志(本周别的号挂过)"
                        else
                            src = "池"
                        end
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
            -- 两份日志标记分开报（判据只问 Ledger.KeywordLogAdmits 一处，与矩阵同源）：
            -- 编号在而布尔位被撤 = 这一条实例这周被**别的一个号**的日志证明存在过，
            -- 当前这个号日志里没有它 —— 报成"日志"是把两份事实混成一份（第 34 轮教训）。
            local logAdmits = GQL.Ledger.KeywordLogAdmits
                and GQL.Ledger.KeywordLogAdmits(db, mid, rec)
            lines[#lines + 1] = ("questID=%s 名=%s 来源=%s 账本地图=%s%s%s%s"):format(
                tostring(mid), tostring(rec.name),
                tostring((rec.kwLogAdded and "日志")
                    or (logAdmits and "日志(本周别的号挂过)")
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
end, "输出 12.x 接口实测报告（加 all = 地图事件段连全图树一起问；"
    .. "加 fetchdata = 试\"不按 M 直接把这一条的金币问出来\"，可带任务号）")
