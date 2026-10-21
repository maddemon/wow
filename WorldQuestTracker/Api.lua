-- WoW API 适配层：插件里唯一读世界任务/奖励/完成状态接口的地方。
-- 12.x 接口形态未经实测：每个候选 pcall，字段名按候选列表试，
-- 取不到就返回原因并把功能降级为"未知"。降级原因集中存 LastDegrade，
-- 由 /wqt dump 与（阶段二）矩阵列头如实显示。

WQT.Api = {}
local Api = WQT.Api

local lastDegrade = "none"

function Api.LastDegrade() return lastDegrade end

local function Degrade(why)
    lastDegrade = why
    if WQT.Config and WQT.Config.Log then WQT.Config.Log("API 降级: " .. why) end
end

function Api.Resolve(path)
    local node = _G
    for part in path:gmatch("[^%.]+") do
        if type(node) ~= "table" then return nil end
        node = node[part]
    end
    return node
end

-- 判"可调用"用真值而不是 type(fn) == "function"：游戏里内置函数 type 确实是
-- "function"，但离线测试里 Python 回调被 lupa 包成 "userdata"，用 type 判会把
-- 可用的回调当成缺失（与 Config.RefreshCycle 同款处理）。
function Api.Callable(fn) return fn ~= nil end
local Callable = Api.Callable

local function FirstField(row, names)
    for i = 1, #names do
        local v = row[names[i]]
        if v ~= nil then return v end
    end
    return nil
end

-- 奖励信息优先取池行自带的字段（12.x 的 WorldQuestInfo 可能已经带奖励摘要），
-- 取不到再单独问奖励接口。两条路都没有才返回 nil（"未判定"）。
local function RewardMoney(row, questID)
    local v = tonumber(FirstField(row, { "money", "rewardMoney", "QuestMoneyToGo" }))
    if v then return v end
    return Api.QuestRewardMoney(questID)
end

local function RewardItems(row, questID)
    local v = FirstField(row, { "items", "rewardItems" })
    if type(v) == "table" and #v > 0 then return v end
    return Api.QuestRewardItems(questID)
end

local POOL_SOURCES = {
    "C_WorldQuests.GetWorldQuests",
    "C_WorldQuests.GetAllWorldQuests",
    -- 12.1.5 实测上面两个都不存在，但名字是猜的。这几个是历史版本用过的入口，
    -- 一起 pcall 一遍才问得出真名；每个都存在候选报告里。
    "GetWorldQuests",
    "C_WorldQuestDB.GetWorldQuests",
    "C_QuestLog.GetQuestsOnMap",
}

--- 抓当前角色的世界任务池。返回 nil 时调用方必须保持账本不变，
--- 绝不能把"读不到"当成"池子为空"（那会把整张账本判成全部消失）。
--- 返回空表则是真数据：世界任务全清完了，账本要走"全部消失"这一转移。
--- 第三条路必须分清：接口返回了 N 条却一条 questID 都认不出来，那是形状不符（nil），
--- 不是空池 —— 当成空池会把所有角色的轮次一起毁掉。
function Api.GetPool()
    local emptyFrom, shapeFrom
    for i = 1, #POOL_SOURCES do
        local path = POOL_SOURCES[i]
        local fn = Api.Resolve(path)
        if Callable(fn) then
            local okCall, raw = pcall(fn)
            if okCall and type(raw) == "table" then
                local out = {}
                for j = 1, #raw do
                    local row = raw[j]
                    local questID
                    if type(row) == "number" then
                        questID = row                       -- 老接口直接回 questID 数组
                    elseif type(row) == "table" then
                        questID = tonumber(FirstField(row, { "questID", "qID", "id" }))
                    end
                    if questID then
                        local t = type(row) == "table" and row or {}
                        out[#out + 1] = {
                            questID = questID,
                            mapID = tonumber(FirstField(t, { "mapID", "zoneID", "areaID" })),
                            name = FirstField(t, { "name", "title", "questName" }),
                            money = RewardMoney(t, questID),
                            items = RewardItems(t, questID),
                        }
                    end
                end
                if #out > 0 then return out, nil end
                if #raw == 0 then
                    -- 空结果继续试下一个候选：某个接口存在但恒返回空，不该遮住另一个的真数据。
                    emptyFrom = path
                else
                    shapeFrom = path .. " 返回 " .. #raw .. " 条但认不出 questID（形状不符）"
                    Degrade(shapeFrom)
                end
            else
                Degrade(path .. " 调用失败: " .. tostring(raw))
            end
        end
    end
    if emptyFrom then
        Degrade(emptyFrom .. " 返回空表")
        return {}, nil
    end
    if shapeFrom then return nil, shapeFrom end
    return nil, "none of " .. table.concat(POOL_SOURCES, ", ") .. " usable"
end

-- 奖励金币。接口缺失返回 nil（"未判定"）——返回 0 会被判成非金币任务。
function Api.QuestRewardMoney(questID)
    local fn = Api.Resolve("C_QuestLog.GetQuestRewardMoney")
    if not Callable(fn) then return nil end
    local okCall, money = pcall(fn, questID)
    if okCall and type(money) == "number" then return money end
    return nil
end

function Api.QuestRewardItems(questID)
    local fn = Api.Resolve("C_QuestLog.GetQuestRewardItems")
    if not Callable(fn) then return nil end
    local okCall, items = pcall(fn, questID)
    if not okCall or type(items) ~= "table" then return nil end
    local out = {}
    for i = 1, #items do
        local it = items[i]
        local itemID = tonumber(type(it) == "table" and (it.itemID or it.itemid) or it)
        if itemID then out[#out + 1] = itemID end
    end
    return out
end

-- 实测 C_QuestLog.IsQuestCompleted / IsQuestCompleted 在 12.1.5 都不存在；
-- IsQuestFlaggedCompleted 是历史版本里问"这个 CD 做过没"的入口，一起问一遍。
local DONE_SOURCES = { "C_QuestLog.IsQuestCompleted", "IsQuestCompleted",
                       "C_QuestLog.IsQuestFlaggedCompleted" }
local PROGRESS_SOURCES = { "C_QuestLog.IsQuestStarted", "C_QuestLog.IsOnQuest", "QuestLog_IsOnQuest" }

-- 返回 (布尔值或nil, 该接口是否真的可用)
local function TryState(paths, questID)
    for i = 1, #paths do
        local fn = Api.Resolve(paths[i])
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            if okCall then return v == true, true end
        end
    end
    return nil, false
end

--- 只回答"当前角色"的状态。其它角色的状态来自账本，绝不由这里推断。
function Api.QuestState(questID)
    local done, okDone = TryState(DONE_SOURCES, questID)
    local progress, okProgress = TryState(PROGRESS_SOURCES, questID)
    if not okDone and not okProgress then
        Degrade("完成/接取状态接口均不可用")
        return "unknown", "state api unavailable"
    end
    if done then return "done", nil end
    if okProgress and progress then return "progress", nil end
    if not okDone then
        Degrade("完成状态接口不可用")
        return "unknown", "completion api unavailable"
    end
    return "open", nil
end

-- 地图名。矩阵按地图分组时用它把 mapID 变成人看得懂的字。
-- 12.x 的入口名与返回字段都没实测过：候选逐个 pcall，字段按候选列表试，
-- 拿不到就返回 nil 让调用方回退"地图 <id>"—— 编一个地图名比显示地图号更糟。
local MAP_NAME_SOURCES = { "C_Map.GetMapInfo", "C_Map.GetAreaInfo", "GetMapNameByID" }

function Api.MapName(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    for i = 1, #MAP_NAME_SOURCES do
        local fn = Api.Resolve(MAP_NAME_SOURCES[i])
        if Callable(fn) then
            local okCall, info = pcall(fn, mapID)
            if okCall then
                local name
                if type(info) == "table" then
                    name = FirstField(info, { "name", "mapName", "areaName" })
                elseif type(info) == "string" then
                    name = info
                end
                if type(name) == "string" and name ~= "" then return name end
            end
        end
    end
    return nil
end

function Api.Money()
    local fn = Api.Resolve("GetMoney")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

function Api.ResetTime()
    local fn = Api.Resolve("GetQuestResetTime")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

-- 墙上时钟（纪元秒）。周 CD 锚点与交任务时间戳必须落在同一个可比域里，否则下次开机
-- GetTime() 归零，账本里的时间戳跟锚点就成了两个世界的数。候选谁先返回 > 1e9 的数就用谁。
-- 形态未经实测，所以 /wqt probe 会把三个值并排打出来，用游戏内证据定论。
local WALL_SOURCES = { "time", "GetServerTime", "GetLocalTime" }

function Api.WallClock()
    for i = 1, #WALL_SOURCES do
        local fn = Api.Resolve(WALL_SOURCES[i])
        if Callable(fn) then
            local okCall, v = pcall(fn)
            local n = okCall and tonumber(v) or nil
            if n and n > 1e9 then return n, WALL_SOURCES[i] end
        end
    end
    return nil, "no usable wall clock"
end

-- 开机后秒数（会话域，重启归零）。只用来跟 GetQuestResetTime 做差，绝不入库。
function Api.Uptime()
    local fn = Api.Resolve("GetTime")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and tonumber(v) or nil
end

--- 帧是否可见。交任务钱差窗口用它判断这 15 秒里有没有开邮箱/拍卖行/交易，
--- 形态未证实所以两条候选都试：先问全局函数，再回退 _G[名字]:IsShown()。
--- 两条都不通只能返回 false —— 那意味着窗口校验失效，必须记进降级原因让 /wqt probe 看得见，
--- 不能假装校验通过继续记一笔可疑金币。
function Api.FrameVisible(name)
    local fn = Api.Resolve("FrameIsVisible")
    if Callable(fn) then
        local okCall, v = pcall(fn, name)
        if okCall then return v == true end
    end
    local frame = Api.Resolve(name)
    -- 不判 type(frame) == "table"：帧对象由引擎造，Lua 看到的类型不保证是 table，
    -- 判错会让"开过邮箱就作废"整条静默失效（FrameIsVisible 在 12.1.5 已实测 MISSING，
    -- 回退是唯一一条路，它再失效就没人兜底了）。
    if frame ~= nil and Callable(frame.IsShown) then
        local okCall, v = pcall(frame.IsShown, frame)
        if okCall then return v == true end
    end
    Degrade("帧可见性接口不可用: " .. tostring(name))
    return false
end

--- 这些候选需要 questID 才能问出形状；零参调用只会拿到 "bad argument #1"，
--- 那种错误行既不能算可用也不能算缺失，探测里是废证据。
local QUEST_ARG_PATHS = {
    ["C_QuestLog.IsQuestCompleted"] = true,
    ["IsQuestCompleted"] = true,
    ["C_QuestLog.IsQuestFlaggedCompleted"] = true,
    ["C_QuestLog.IsQuestStarted"] = true,
    ["C_QuestLog.IsOnQuest"] = true,
    ["QuestLog_IsOnQuest"] = true,
    ["C_QuestLog.GetQuestRewardMoney"] = true,
    ["C_QuestLog.GetQuestRewardItems"] = true,
    ["GetQuestRewardValues"] = true,
    ["C_QuestRewards.FetchData"] = true,
}

function Api.ArgHint(path) return QUEST_ARG_PATHS[path] == true end

--- 阶段二（地图按钮 + 矩阵）要用到的入口。这里只判存在、绝不真调用：
--- 地图相关的 FrameXML 接口有副作用（开图、切图），拿探测报告换证据不该改变玩家状态。
--- 地图按钮本身只需要 CreateFrame + SetPoint（父层挂 WorldMapFrame，跟着地图显示/隐藏），
--- 不依赖 RegisterButton 那类内部接口，所以下面这几条查不到也不阻塞按钮，只影响分组与气泡坐标。
local STAGE2_ENTRIES = {
    "WorldMapFrame",
    "WorldMapFrame.ScrollContainer.Child.GridCanvas",
    "WorldMapFrame.AddButton",
    "WorldMapFrame.RegisterButton",
    "C_Map.GetMapInfo",
    "C_Map.GetAreaInfo",
    "GetMapNameByID",
    "C_Map.GetWorldMapID",
    "C_WorldMap.GetWorldMapID",
}

function Api.Stage2Entries() return STAGE2_ENTRIES end

function Api.CandidatePaths()
    local list = {}
    for i = 1, #POOL_SOURCES do list[#list + 1] = POOL_SOURCES[i] end
    for i = 1, #DONE_SOURCES do list[#list + 1] = DONE_SOURCES[i] end
    for i = 1, #PROGRESS_SOURCES do list[#list + 1] = PROGRESS_SOURCES[i] end
    list[#list + 1] = "C_QuestLog.GetQuestRewardMoney"
    list[#list + 1] = "C_QuestLog.GetQuestRewardItems"
    list[#list + 1] = "GetQuestRewardValues"
    list[#list + 1] = "C_QuestRewards.FetchData"
    list[#list + 1] = "GetQuestResetTime"
    list[#list + 1] = "GetMoney"
    list[#list + 1] = "FrameIsVisible"
    for i = 1, #WALL_SOURCES do list[#list + 1] = WALL_SOURCES[i] end
    return list
end
