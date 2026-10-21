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
}

--- 抓当前角色的世界任务池。返回 nil 时调用方必须保持账本不变，
--- 绝不能把"读不到"当成"池子为空"（那会把整张账本判成全部消失）。
function Api.GetPool()
    for i = 1, #POOL_SOURCES do
        local path = POOL_SOURCES[i]
        local fn = Api.Resolve(path)
        if Callable(fn) then
            local okCall, raw = pcall(fn)
            if okCall and type(raw) == "table" then
                local out = {}
                for j = 1, #raw do
                    local row = raw[j]
                    if type(row) == "table" then
                        local questID = tonumber(FirstField(row, { "questID", "qID", "id" }))
                        if questID then
                            out[#out + 1] = {
                                questID = questID,
                                mapID = tonumber(FirstField(row, { "mapID", "zoneID", "areaID" })),
                                name = FirstField(row, { "name", "title", "questName" }),
                                money = RewardMoney(row, questID),
                                items = RewardItems(row, questID),
                            }
                        end
                    end
                end
                if #out > 0 then return out, nil end
                Degrade(path .. " 返回空表")
            else
                Degrade(path .. " 调用失败: " .. tostring(raw))
            end
        end
    end
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

local DONE_SOURCES = { "C_QuestLog.IsQuestCompleted", "IsQuestCompleted" }
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

function Api.CandidatePaths()
    local list = {}
    for i = 1, #POOL_SOURCES do list[#list + 1] = POOL_SOURCES[i] end
    for i = 1, #DONE_SOURCES do list[#list + 1] = DONE_SOURCES[i] end
    for i = 1, #PROGRESS_SOURCES do list[#list + 1] = PROGRESS_SOURCES[i] end
    list[#list + 1] = "C_QuestLog.GetQuestRewardMoney"
    list[#list + 1] = "C_QuestLog.GetQuestRewardItems"
    list[#list + 1] = "GetQuestResetTime"
    list[#list + 1] = "GetMoney"
    for i = 1, #WALL_SOURCES do list[#list + 1] = WALL_SOURCES[i] end
    return list
end
