-- 插件入口：命名空间、模块注册表、事件分发、/gcl 子命令注册表。
-- 命名空间游戏里由 WoW 传入 vararg，测试里由 mock 预置（照 GQL 的做法）。

local ADDON_NAME, NS = ...
GCL = GCL or NS or {}
_G.GCL = GCL
GCL.addonName = ADDON_NAME

--- 必须与 toc 的 ## Version 完全一致（用例 test_version_matches_toc 钉死）。
GCL.VERSION = "0.1.0"

-- 模块注册表：T1 Money / T2 Ledger / T3 Calendar 各自文件里注册进来。
-- 生命周期口径（计划第 1 节 #2：登录瞬间的钱不能算收入）：
--   OnInitialize 在 ADDON_LOADED 跑（建/合并存档、挂钩子）；
--   OnLogin 在 PLAYER_LOGIN 跑（Money 在这里取金币基线）。
if not GCL.modules then GCL.modules = {} end

function GCL.RegisterModule(name, mod)
    GCL.modules[name] = mod
    GCL[name] = mod
    return mod
end

function GCL:GetModule(name)
    return GCL.modules[name]
end

-- 事件分发：一张订阅表 + 单一根帧。模块不许自建帧重复监听，
-- 否则事件顺序不可控，也没地方统一 pcall 兜错。
if not GCL.subscriptions then GCL.subscriptions = {} end
local registeredEvents = {}

function GCL.RegisterEvent(name, fn)
    local list = GCL.subscriptions[name]
    if not list then list = {} ; GCL.subscriptions[name] = list end
    list[#list + 1] = fn
    -- 订阅了还不够：根帧必须向客户端注册，事件才会真发过来（12.1.5 实测口径）。
    -- 同一事件只注册一次；未知事件名照客户端行为直接抛错，不静默吞掉，
    -- 这样"替身里凭空造事件"和生产代码里拼错事件名都会立刻现形。
    if name and not registeredEvents[name] and GCL.frame then
        GCL.frame:RegisterEvent(name)
        registeredEvents[name] = true
    end
end

local function Dispatch(event, ...)
    local list = GCL.subscriptions[event]
    if not list then return end
    for i = 1, #list do
        local okCall, err = pcall(list[i], event, ...)
        if not okCall then print(("GCL: 事件 %s 的回调出错: %s"):format(event, tostring(err))) end
    end
end

-- 逐模块各包一层 pcall：一个模块炸不许连坐其他模块（骨架的第一条隔离原则）。
local function CallHook(hook)
    for name, mod in pairs(GCL.modules) do
        local fn = mod[hook]
        if fn then
            local okCall, err = pcall(fn, mod)
            if not okCall then
                print(("GCL: 模块 %s 的 %s 出错: %s"):format(name, hook, tostring(err)))
            end
        end
    end
end

-- 只补缺失键、不覆盖已有值：SavedVariables 是跨版本活档，覆盖等于抹掉用户的账。
-- 递归补嵌套层，并且默认值表只做"读数"、绝不把子表对象直接挂进存档 ——
-- 否则模块往 GCLDB.settings 写一笔就等于往 DB_DEFAULTS 写，二次合并时读到的已经不是默认值。
-- schema 档位与迁移表在 T2 落地，这里先立 1 档。
local DB_DEFAULTS = { schema = 1, settings = {}, daily = {}, byDayChar = {}, chars = {}, entries = {} }

local function ApplyDefaults(target, defaults)
    for k, v in pairs(defaults) do
        if type(v) == "table" then
            if type(target[k]) ~= "table" then
                target[k] = {}
            end
            ApplyDefaults(target[k], v)
        elseif target[k] == nil then
            target[k] = v
        end
    end
end

local function MergeDefaults()
    if type(GCLDB) ~= "table" then GCLDB = {} end
    ApplyDefaults(GCLDB, DB_DEFAULTS)
end

local function InitAll()
    MergeDefaults()
    CallHook("OnInitialize")
end

GCL.frame = CreateFrame("Frame", "GCLFrame", UIParent)
GCL.frame:RegisterEvent("ADDON_LOADED")
GCL.frame:RegisterEvent("PLAYER_LOGIN")
GCL.frame:SetScript("OnEvent", function(self, event, arg1, ...)
    if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then
            local okInit, err = pcall(InitAll)
            if not okInit then print("GCL 初始化失败: " .. tostring(err)) end
        end
    elseif event == "PLAYER_LOGIN" then
        CallHook("OnLogin")
    end
    Dispatch(event, arg1, ...)
end)

if not GCL.commands then GCL.commands = {} end

function GCL.RegisterCommand(name, fn, help)
    GCL.commands[name] = { fn = fn, help = help or "" }
end

local function PrintHelp()
    local names = {}
    for name in pairs(GCL.commands) do names[#names + 1] = name end
    table.sort(names)
    print("/gcl 子命令: " .. table.concat(names, " | "))
end

local function OnSlash(text)
    local sub = (text or ""):match("^(%S*)")
    local cmd = GCL.commands[sub]
    if not cmd then return PrintHelp() end
    local okCall, err = pcall(cmd.fn, (text or ""):match("^%S+%s*(.*)$"))
    if not okCall then print("GCL: /gcl " .. sub .. " 出错: " .. tostring(err)) end
end

SLASH_GCL1 = "/gcl"
SlashCmdList["GCL"] = OnSlash

-- 共享出去之后"对方报的是哪个版本"就靠这一行；toc 名即文件夹名（装机验收要对它做 diff）。
GCL.RegisterCommand("version", function()
    print(("GCL %s (%s)"):format(tostring(GCL.VERSION or "?"), tostring(GCL.addonName or "?")))
end, "查看版本号与 toc 名")

GCL.RegisterCommand("dump", function()
    local function Count(t)
        local n = 0
        if type(t) == "table" then for _ in pairs(t) do n = n + 1 end end
        return n
    end
    local db = type(GCLDB) == "table" and GCLDB or {}
    print(("GCL: 共 %d 天 / %d 笔流水 / %d 个角色"):format(Count(db.daily), Count(db.entries), Count(db.chars)))
end, "一句话汇总记账规模")
