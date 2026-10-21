-- /wqt probe：把 12.x 候选接口的实测结果输出成可复制文本。
-- 目的不是给用户看，是把"接口存不存在、返回什么形状"变成实证：
-- 阶段二的 UI 只允许接在这里验证过的入口。

WQT.Probe = {}
local Probe = WQT.Probe

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
    -- 上一轮实测池接口都要 mapID，可枚举地图的入口只可能在这里
    "C_Map", "C_WorldMap",
}
local NAME_HINTS = { "WorldQuest", "Bounty" }
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
        local node = WQT.Api.Resolve(NAMESPACES[i])
        if type(node) == "table" then
            local names, count = MemberNames(node)
            out[#out + 1] = ("%s 成员数=%d 方法=%s"):format(NAMESPACES[i], count, table.concat(names, ","))
        else
            out[#out + 1] = ("%s 不存在"):format(NAMESPACES[i])
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
    local db = WQT.Config.DB()
    if not db then return 0 end
    local okCall, ids = pcall(function() return WQT.Ledger.GoldQuestIDs(db) end)
    if okCall and ids and #ids > 0 then return tonumber(ids[1]) or 0 end
    local questID = db.quests and next(db.quests)
    return tonumber(questID) or 0
end

-- mapID 拿不到就是拿不到：填 0 会让 "bad argument #1" 冒充成接口可用性问题，
-- 上一轮报告里那一整行 ERROR 就是这么来的。宁可明写跳过，也不产出废证据。
local function SampleMapID()
    local okCall, v = pcall(WQT.Api.CurrentMapID)
    if okCall and v then return v end
    local db = WQT.Config.DB()
    if db and db.quests then
        for _, rec in pairs(db.quests) do
            local mapID = tonumber(rec.mapID)
            if mapID and mapID > 0 then return mapID end
        end
    end
    return nil
end

-- 返回一串表时，光看 table[#40]{1,2,3...} 问不出字段名，接不上下一步。
-- 补一条首条目的形状，池接口才能照着写归一化。
local function FirstRow(v)
    if type(v) ~= "table" then return "" end
    local first = v[1]
    if type(first) ~= "table" then return "" end
    return " 首条=" .. Describe(first, 1)
end

local function PathLine(path, samples)
    local fn = WQT.Api.Resolve(path)
    if not WQT.Api.Callable(fn) then
        return ("MISSING   %s"):format(path)
    end
    local hint = WQT.Api.ArgHint(path)
    local argVal = hint and samples[hint]
    if hint and argVal == nil then
        return ("SKIP      %s 需要 %s，本机拿不到（无 mapID 可用）"):format(path, hint)
    end
    local okCall, a, b
    if hint then
        okCall, a, b = pcall(fn, argVal)
    else
        okCall, a, b = pcall(fn)
    end
    if not okCall then
        return ("ERROR     %s -> %s"):format(path, tostring(a))
    end
    return ("AVAILABLE %s%s -> %s | %s%s"):format(
        path, hint and ("(%s=%s)"):format(hint, tostring(argVal)) or "",
        Describe(a), Describe(b), FirstRow(a))
end

function Probe.Run()
    local lines = { "WQT 12.x 接口探测报告", "GetBuildInfo: " .. BuildInfo(), "---- 候选接口" }

    local samples = { questID = SampleQuestID() }
    samples.mapID = SampleMapID()
    local paths = WQT.Api.CandidatePaths()
    for i = 1, #paths do
        -- mapID 类候选统一挪到下面的"地图/bounty"段报，这里再打一遍只是重复行
        if WQT.Api.ArgHint(paths[i]) ~= "mapID" then
            lines[#lines + 1] = PathLine(paths[i], samples)
        end
    end

    lines[#lines + 1] = "---- 时钟域"
    local reset = WQT.Api.ResetTime()
    local uptime = WQT.Api.Uptime()
    local wall, wallSrc = WQT.Api.WallClock()
    lines[#lines + 1] = ("GetQuestResetTime=%s GetTime=%s WallClock=%s(%s)")
        :format(tostring(reset), tostring(uptime), tostring(wall), tostring(wallSrc))
    -- 形态判读只认 Config.ResetFormOf 一处，锚点换算用的就是它，不许这里另猜一套
    local form, why = WQT.Config.ResetFormOf(reset, tonumber(uptime))
    lines[#lines + 1] = ("GetQuestResetTime 读法=%s（%s）"):format(tostring(form), tostring(why))
    local anchor = WQT.Config.CycleAnchorOf(reset, wall, tonumber(uptime))
    lines[#lines + 1] = ("按此读法算出的 CD 锚点=%s 距下次重置=%s 秒")
        :format(tostring(anchor), tostring(reset))

    lines[#lines + 1] = "---- 阶段二入口（只判存在，不真调用）"
    local entries = WQT.Api.Stage2Entries()
    for i = 1, #entries do
        local v = WQT.Api.Resolve(entries[i])
        lines[#lines + 1] = ("%s %s"):format(v ~= nil and "PRESENT  " or "MISSING  ", entries[i])
    end

    -- 这一段的产出决定下一版怎么抓池：上一轮只拿到 "GetQuestsOnMap(uiMapID)" 这条用法提示，
    -- 说明池接口是按月历/bounty 给的，缺的就是 mapID 从哪儿枚举。
    lines[#lines + 1] = "---- 地图/bounty 候选"
    lines[#lines + 1] = ("mapID 样本=%s"):format(tostring(samples.mapID))
    local mapPaths = WQT.Api.MapCandidatePaths()
    for i = 1, #mapPaths do
        lines[#lines + 1] = PathLine(mapPaths[i], samples)
    end

    lines[#lines + 1] = "---- 地图按钮宿主"
    local host = WQT.Api.Resolve("WorldMapFrame")
    if host == nil then
        lines[#lines + 1] = "WorldMapFrame=缺失（按钮只能降级）"
    else
        local okCall, w, h = pcall(function() return host:GetWidth(), host:GetHeight() end)
        lines[#lines + 1] = ("WorldMapFrame=可用 显隐=%s 尺寸=%s 按钮=%s"):format(
            tostring(WQT.Api.FrameVisible("WorldMapFrame")),
            (okCall and w) and ("%dx%d"):format(w, h or 0) or "取不到",
            WQT.Matrix and WQT.Matrix.ButtonInfo and WQT.Matrix.ButtonInfo() or "未建")
    end

    local discovery = DiscoveryLines()
    for i = 1, #discovery do lines[#lines + 1] = discovery[i] end

    lines[#lines + 1] = "---- 池快照样本"
    local pool, err = WQT.Api.GetPool()
    if pool then
        lines[#lines + 1] = ("池条目数: %d"):format(#pool)
        for i = 1, math.min(5, #pool) do
            local q = pool[i]
            local state = { WQT.Api.QuestState(q.questID) }
            lines[#lines + 1] = ("  #%d questID=%s mapID=%s name=%s money=%s items=%s state=%s"):format(
                i, tostring(q.questID), tostring(q.mapID), tostring(q.name),
                tostring(q.money), Describe(q.items), tostring(state[1]))
        end
    else
        lines[#lines + 1] = "池不可用: " .. tostring(err)
    end

    lines[#lines + 1] = "---- 其它"
    lines[#lines + 1] = "ResetTime: " .. tostring(WQT.Api.ResetTime())
    lines[#lines + 1] = "Money: " .. tostring(WQT.Api.Money())
    lines[#lines + 1] = "LastDegrade: " .. tostring(WQT.Api.LastDegrade())

    local report = table.concat(lines, "\n")
    WQT.Config.Log("probe 完成")
    return report
end

WQT.RegisterCommand("probe", function()
    local report = Probe.Run()
    if WQT.LogWindow and WQT.LogWindow.Show then WQT.LogWindow.Show(report) end
    print(report)
end, "输出 12.x 接口实测报告")
