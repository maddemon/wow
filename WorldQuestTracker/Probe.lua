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

function Probe.Run()
    local lines = { "WQT 12.x 接口探测报告", "GetBuildInfo: " .. BuildInfo(), "---- 候选接口" }

    local paths = WQT.Api.CandidatePaths()
    for i = 1, #paths do
        local path = paths[i]
        local fn = WQT.Api.Resolve(path)
        if not WQT.Api.Callable(fn) then
            lines[#lines + 1] = ("MISSING   %s"):format(path)
        else
            local okCall, a, b = pcall(fn)
            if okCall then
                lines[#lines + 1] = ("AVAILABLE %s -> %s | %s"):format(path, Describe(a), Describe(b))
            else
                lines[#lines + 1] = ("ERROR     %s -> %s"):format(path, tostring(a))
            end
        end
    end

    lines[#lines + 1] = "---- 时钟域"
    local reset = WQT.Api.ResetTime()
    local uptime = WQT.Api.Uptime()
    local wall, wallSrc = WQT.Api.WallClock()
    lines[#lines + 1] = ("GetQuestResetTime=%s GetTime=%s WallClock=%s(%s)")
        :format(tostring(reset), tostring(uptime), tostring(wall), tostring(wallSrc))
    -- 判读：GetQuestResetTime 若与 GetTime 同量级（百万以内）就是会话域，与 WallClock 同量级
    -- （1.7e9 以上）就是纪元域。这一行决定账本里的时间戳该用哪个域，必须用实测结果回答。

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
