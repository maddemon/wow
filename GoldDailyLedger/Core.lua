-- 插件入口：命名空间、模块注册表、事件分发、/gdl 子命令注册表。
-- 命名空间游戏里由 WoW 传入 vararg，测试里由 mock 预置（照 GQL 的做法）。

local ADDON_NAME, NS = ...
GDL = GDL or NS or {}
_G.GDL = GDL
GDL.addonName = ADDON_NAME

--- 必须与 toc 的 ## Version 完全一致（用例 test_version_matches_toc 钉死）。
GDL.VERSION = "0.4.0"

-- 模块注册表：T1 Money / T2 Ledger / T3 Calendar 各自文件里注册进来。
-- 生命周期口径（计划第 1 节 #2：登录瞬间的钱不能算收入）：
--   OnInitialize 在 ADDON_LOADED 跑（建/合并存档、挂钩子）；
--   OnLogin 在 PLAYER_LOGIN 跑（Money 在这里取金币基线）。
if not GDL.modules then GDL.modules = {} end

function GDL.RegisterModule(name, mod)
    GDL.modules[name] = mod
    GDL[name] = mod
    return mod
end

function GDL:GetModule(name)
    return GDL.modules[name]
end

-- 事件分发：一张订阅表 + 单一根帧。模块不许自建帧重复监听，
-- 否则事件顺序不可控，也没地方统一 pcall 兜错。
if not GDL.subscriptions then GDL.subscriptions = {} end
local registeredEvents = {}

function GDL.RegisterEvent(name, fn)
    local list = GDL.subscriptions[name]
    if not list then list = {} ; GDL.subscriptions[name] = list end
    list[#list + 1] = fn
    -- 订阅了还不够：根帧必须向客户端注册，事件才会真发过来（12.1.5 实测口径）。
    -- 同一事件只注册一次；未知事件名照客户端行为直接抛错，不静默吞掉，
    -- 这样"替身里凭空造事件"和生产代码里拼错事件名都会立刻现形。
    if name and not registeredEvents[name] and GDL.frame then
        GDL.frame:RegisterEvent(name)
        registeredEvents[name] = true
    end
end

local function Dispatch(event, ...)
    local list = GDL.subscriptions[event]
    if not list then return end
    for i = 1, #list do
        local okCall, err = pcall(list[i], event, ...)
        if not okCall then print(("GDL: 事件 %s 的回调出错: %s"):format(event, tostring(err))) end
    end
end

-- 逐模块各包一层 pcall：一个模块炸不许连坐其他模块（骨架的第一条隔离原则）。
local function CallHook(hook)
    for name, mod in pairs(GDL.modules) do
        local fn = mod[hook]
        if fn then
            local okCall, err = pcall(fn, mod)
            if not okCall then
                print(("GDL: 模块 %s 的 %s 出错: %s"):format(name, hook, tostring(err)))
            end
        end
    end
end

-- 只补缺失键、不覆盖已有值：SavedVariables 是跨版本活档，覆盖等于抹掉用户的账。
-- 递归补嵌套层，并且默认值表只做"读数"、绝不把子表对象直接挂进存档 ——
-- 否则模块往 GDLDB.settings 写一笔就等于往 DB_DEFAULTS 写，二次合并时读到的已经不是默认值。
-- schema 档位与迁移表在 T2 落地，这里先立 1 档。
-- classByChar 单独放职业 token：chars 只收"真记过账的号"，登录一个从没花过钱的号
-- 不该凭空多出一行空号，但它的职业色该留下（用户：只有登录过的号颜色才对）。
-- goldByChar 同理：记的是"登录过的号"最近一次钱包快照，不往 chars 里造空号。
local DB_DEFAULTS = { schema = 1, settings = {}, daily = {}, byDayChar = {},
                      chars = {}, classByChar = {}, goldByChar = {}, entries = {} }

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
    if type(GDLDB) ~= "table" then GDLDB = {} end
    ApplyDefaults(GDLDB, DB_DEFAULTS)
end

local function InitAll()
    MergeDefaults()
    CallHook("OnInitialize")
end

GDL.frame = CreateFrame("Frame", "GDLFrame", UIParent)
GDL.frame:RegisterEvent("ADDON_LOADED")
GDL.frame:RegisterEvent("PLAYER_LOGIN")
GDL.frame:SetScript("OnEvent", function(self, event, arg1, ...)
    if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then
            local okInit, err = pcall(InitAll)
            if not okInit then print("GDL 初始化失败: " .. tostring(err)) end
        end
    elseif event == "PLAYER_LOGIN" then
        CallHook("OnLogin")
    end
    Dispatch(event, arg1, ...)
end)

--- 跨插件层序（2026-11-11 重写为统一协议）：GDL 流水/日历与 GQL 矩阵/设置/日志窗全部
--- 用 DIALOG + SetToplevel，靠 WoW 原生的"谁被点 / 谁最后开谁在最前"保证跨插件层序确定，
--- 不再互相改写对端窗的 strata/层号。旧版这里把 GQL 各窗强行拉回 MEDIUM 再按层号压本窗
--- 上去，和 GQL 在 OnShow 抬 DIALOG 的协议互相打架，谁在上完全看最后谁的动作生效
--- （用户实测"大概率在下 / 再点又可能在上面"）。现在只 Raise 本窗 + 钉 ×。
--- 全程 pcall：GQL 没装 / 对方还没建窗 / 方法读不出来，一律退回默认层序，不许连累开窗。
function GDL.RaiseFront(f, close)
    if type(f) ~= "table" then return false end
    -- 只把本窗 Raise 到 DIALOG 最上：同 strata 里 toplevel 帧由 WoW 在点击/显示时自动置顶，
    -- 不再碰对端窗（旧版拉对方回 MEDIUM 正是层序随机的根因）。
    pcall(f.Raise, f)
    -- × 是暴雪模板（自带 frameLevel=510），必须钉回本窗层号 +1，否则它会浮到对方窗上。
    if type(close) == "table" then
        local okLvl, lvl = pcall(f.GetFrameLevel, f)
        if okLvl and tonumber(lvl) then
            pcall(close.SetFrameLevel, close, tonumber(lvl) + 1)
        end
    end
    return true
end

if not GDL.commands then GDL.commands = {} end

function GDL.RegisterCommand(name, fn, help)
    GDL.commands[name] = { fn = fn, help = help or "" }
end

local function PrintHelp()
    local names = {}
    for name in pairs(GDL.commands) do names[#names + 1] = name end
    table.sort(names)
    print("/gdl 子命令: " .. table.concat(names, " | "))
end

local function OnSlash(text)
    local sub = (text or ""):match("^(%S*)")
    local cmd = GDL.commands[sub]
    if not cmd then return PrintHelp() end
    local okCall, err = pcall(cmd.fn, (text or ""):match("^%S+%s*(.*)$"))
    if not okCall then print("GDL: /gdl " .. sub .. " 出错: " .. tostring(err)) end
end

SLASH_GDL1 = "/gdl"
SlashCmdList["GDL"] = OnSlash

-- 共享出去之后"对方报的是哪个版本"就靠这一行；toc 名即文件夹名（装机验收要对它做 diff）。
GDL.RegisterCommand("version", function()
    print(("GDL %s (%s)"):format(tostring(GDL.VERSION or "?"), tostring(GDL.addonName or "?")))
end, "查看版本号与 toc 名")

--- 往 /gdl probe 的正文后面补一段取证（真身在 Money.lua，Calendar 又分流了 "probe ui"）。
--- 各模块按 toc 顺序往同一条命令上套，套不上（前面还没人注册 probe）就自己立一条 probe，
--- 总之取证段不许因为注册顺序变了就静默消失。"ui" 那一支归界面几何，别往那儿插话。
function GDL.ChainProbe(name, section)
    if type(name) ~= "string" or type(section) ~= "function" then return false end
    local cmd = GDL.commands["probe"]
    if type(cmd) ~= "table" or type(cmd.fn) ~= "function" then
        GDL.RegisterCommand("probe", function() return section() end, "取证：本客户端上的接口与素材现状")
        return true
    end
    if cmd["__section_" .. name] then return true end
    local prev = cmd.fn
    cmd.fn = function(rest)
        local r = prev(rest)
        if type(rest) ~= "string" or not rest:match("^ui") then
            local okCall, err = pcall(section)
            if not okCall then
                print("GDL: probe 的 " .. name .. " 段没打出来: " .. tostring(err))
            end
        end
        return r
    end
    cmd["__section_" .. name] = true
    return true
end

GDL.RegisterCommand("dump", function()
    local function Count(t)
        local n = 0
        if type(t) == "table" then for _ in pairs(t) do n = n + 1 end end
        return n
    end
    local db = type(GDLDB) == "table" and GDLDB or {}
    print(("GDL: 共 %d 天 / %d 笔流水 / %d 个角色"):format(Count(db.daily), Count(db.entries), Count(db.chars)))
end, "一句话汇总记账规模")
