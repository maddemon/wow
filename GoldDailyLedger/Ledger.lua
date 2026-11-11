-- 存储层（T2）：全插件唯一的一处写入路径。三处冗余（daily / byDayChar / entries）
-- 在这里一次写齐，其余一切（日历格、钻取行、范围合计、流水页）都只是查表，绝不现算。
-- 计划第 2 节是这张表的数据模型；第 3 节的界面要什么，第 6 节的读数接口就给什么。
--
-- 两条从别的包里学到的硬规矩，都写在这里而不是散落到各处：
--   1. 时间戳必须落在同一个"墙上时钟"域里（GetTime 每次 reload/开机归零，存进去
--      就跟下一次开机的数不可比了 —— GQL 的实测教训，见 GoldQuestLedger/Api.lua:1432-1447）。
--   2. 清理必须遍历全部号，不能只清当前登录号（GoldLedger 的坑，见 GoldLedger/Data.lua:605-606）。

local GDL = _G.GDL
local Ledger = {}

if GDL and GDL.RegisterModule then
    GDL.RegisterModule("Ledger", Ledger)
else
    GDL = GDL or {}
    GDL.Ledger = Ledger
end

---------------------------------------------------------------------------
-- 常量（改动要连同用例一起改）
---------------------------------------------------------------------------

local SECONDS_PER_DAY = 86400
local DAILY_KEEP_DAYS = 400     -- daily/byDayChar 保 400 天（计划 §2）
local ENTRIES_CAP       = 20000 -- entries 全局上限，超了丢最老（计划 §2）
local CURRENT_SCHEMA    = 1     -- schema 已在 Core 的 DB_DEFAULTS 里立成 1
local DEFAULT_PER_PAGE  = 20

-- 一次把几十件垃圾卖掉会连发几十个 PLAYER_MONEY，流水里就占几十行。
-- 合并只发生在**读出层**：db.entries 每条明细都留着（可回溯），总量一次不少，
-- 只把"同一号、同一天、同一方向、相邻间隔 ≤ 3 秒"的连续 vendor 合成一行显示。
-- 别的来源不做这件事：连续收几封邮件、连着交几个任务在语义上本来就是各自一笔。
local VENDOR_MERGE_SOURCE  = "vendor"
local VENDOR_MERGE_SECONDS = 3

-- 号间搬钱的来源名：这是"这笔不算收支"的存储层真相。Money 探针里的 meta.transfer
-- 是它的显示字段，不参与这里的总量判定 —— 同一个动作不能因为"哪个调用方记得打标"
-- 而被分成两种口径（计划 §2 transfer 条目 / §1 #4）。
local TRANSFER_SOURCES = { warband = true, guildbank = true }

function Ledger.IsTransferSource(source)
    return type(source) == "string" and TRANSFER_SOURCES[source] == true
end

---------------------------------------------------------------------------
-- 状态（只放"读的时候要用、但读的时候不该再问接口"的两样）
---------------------------------------------------------------------------

local currentChar            -- 最近一次写入解析出来的 "Name-Realm"，供读数置顶当前号
local counters = { droppedWrites = 0, charWarned = false }

function Ledger.GetDroppedWrites()
    return counters.droppedWrites
end

---------------------------------------------------------------------------
-- 墙上时钟：候选名单逐个 Callable 检查 + pcall，第一个返回 > 1e9 的胜出。
-- 做法从 GoldQuestLedger/Api.lua:1432-1447 抄（那是 GQL 实测过"存进档的时间戳必须同域"
-- 之后定下来的顺序），但不 import 它的代码。time 是标准全局，且国服真机回 10 位纪元秒；
-- GetServerTime/GetLocalTime 是候选来源，谁在就用谁。
---------------------------------------------------------------------------

local WALL_SOURCES = { "time", "GetServerTime", "GetLocalTime" }

--- 判"可调用"用真值而不是 type == "function"：内置函数在 lupa 下会被包成 userdata，
--- 用 type 判会把可用的回调当成缺失（与 GQL 同款处理）。
local function Callable(fn)
    return fn ~= nil
end

function Ledger.WallClock()
    for i = 1, #WALL_SOURCES do
        local name = WALL_SOURCES[i]
        local fn = _G[name]
        if Callable(fn) then
            local okCall, v = pcall(fn)
            local n = okCall and tonumber(v) or nil
            if n and n > 1e9 then return n, name end
        end
    end
    return nil, "no usable wall clock"
end

---------------------------------------------------------------------------
-- 日期键：一律走客户端全局 date（GoldLedger/Data.lua 就是用这两个格式在他机上记账的，
-- 已被"能记账"证明存在）。任何 nil / 奇形返回值都不许变成存档里的键 —— 宁可这笔不写。
-- ISO "%Y-%m-%d" / "%Y-%m" 定宽零填充：字典序 == 时间序，所以清理能直接拿字符串比大小。
---------------------------------------------------------------------------

local ISO_DAY   = "^%d%d%d%d%-%d%d%-%d%d$"
local ISO_MONTH = "^%d%d%d%d%-%d%d$"

local function CallDate(fmt, seconds)
    local fn = _G.date
    if not Callable(fn) then return nil end
    if seconds ~= nil and type(seconds) ~= "number" then return nil end
    local okCall, r = pcall(fn, fmt, seconds)
    if not okCall then return nil end
    return r
end

--- @param seconds number 墙上时钟纪元秒
--- @return string|nil  "YYYY-MM-DD"；读不出来返回 nil（绝不返回占位键）
function Ledger.DateKey(seconds)
    if type(seconds) ~= "number" then return nil end
    local r = CallDate("%Y-%m-%d", seconds)
    if type(r) ~= "string" or not r:find(ISO_DAY) then return nil end
    return r
end

function Ledger.MonthKey(seconds)
    if type(seconds) ~= "number" then return nil end
    local r = CallDate("%Y-%m", seconds)
    if type(r) ~= "string" or not r:find(ISO_MONTH) then return nil end
    return r
end

local function IsIsoDay(k)
    return type(k) == "string" and k:find(ISO_DAY) ~= nil
end

---------------------------------------------------------------------------
-- 纯日期数学（计划 §6：日历 UI 要这些数，但读数接口不许再碰事件接口）
-- dateKey -> 年月日 的解析 + 自包含的日历算法，一个接口都不问。
---------------------------------------------------------------------------

local function IsLeap(year)
    return (year % 4 == 0 and year % 100 ~= 0) or (year % 400 == 0)
end

local MONTH_DAYS = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }

function Ledger.DaysInMonth(year, month)
    if type(year) ~= "number" or type(month) ~= "number" then return nil end
    -- math.floor 而不是 //：WoW 是 Lua 5.1 没有整除运算符（离线跑 5.5 也照样支持 floor）。
    year = math.floor(year)
    month = math.floor(month)
    if month < 1 or month > 12 then return nil end
    if month == 2 then return IsLeap(year) and 29 or 28 end
    return MONTH_DAYS[month]
end

--- Sakamoto：返回 0=周日..6=周六，再 +1 映射到 Lua "*t.wday"（1=周日）。
function Ledger.WeekdayOfDateKey(dateKey)
    if not IsIsoDay(dateKey) then return nil end
    local y, m, d = dateKey:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
    y, m, d = tonumber(y), tonumber(m), tonumber(d)
    if not (y and m and d) then return nil end
    if m < 1 or m > 12 then return nil end
    if d < 1 or d > Ledger.DaysInMonth(y, m) then return nil end
    local offsets = { 0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4 }
    if m < 3 then y = y - 1 end
    local dow = (y + math.floor(y / 4) - math.floor(y / 100) + math.floor(y / 400) + offsets[m] + d) % 7
    return dow + 1
end

function Ledger.TodayKey()
    local now = Ledger.WallClock()
    if not now then return nil end
    return Ledger.DateKey(now)
end

---------------------------------------------------------------------------
-- 存档访问
---------------------------------------------------------------------------

local function DB()
    if type(_G.GDLDB) ~= "table" then _G.GDLDB = {} end
    local db = _G.GDLDB
    if type(db.entries)   ~= "table" then db.entries = {} end
    if type(db.daily)     ~= "table" then db.daily = {} end
    if type(db.byDayChar) ~= "table" then db.byDayChar = {} end
    if type(db.chars)     ~= "table" then db.chars = {} end
    return db
end

local function Num(v)
    return type(v) == "number" and v or 0
end

--- 角色键：UnitName/GetRealmName 都是本仓库已实测存在的客户端全局
--- （GoldLedger/Data.lua:47-48 就是这么拼 "Name-Realm" 的）。
--- 两个里任何一个读不出来（nil / 非串 / 空串）就返回 nil —— 绝不写 "Unknown-Unknown"
--- 这种占位假号：一个假键会在他的月历里作为假角色行永远活着（计划 §2 明说这条）。
local function ReadString(fn, ...)
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn, ...)
    if not okCall or type(v) ~= "string" or v == "" then return nil end
    return v
end

local function CurrentCharKey()
    local name  = ReadString(_G.UnitName, "player")
    local realm = ReadString(_G.GetRealmName)
    if not name or not realm then return nil end
    return name .. "-" .. realm
end

local function TouchChar(db, key, ts)
    local c = db.chars[key]
    if not c then
        db.chars[key] = { firstSeen = ts, lastSeen = ts }
        return
    end
    if type(c.firstSeen) ~= "number" or ts < c.firstSeen then c.firstSeen = ts end
    if type(c.lastSeen)  ~= "number" or ts > c.lastSeen  then c.lastSeen  = ts end
end

--- 职业 token 只在客户端上拿得到（UnitClass 第二返回值，GoldQuestLedger/Sync.lua:1178-1181
--- 的实测取法）。写进独立的 classByChar：**这个号哪怕从没记过账也要留下颜色**，
--- 但绝不往 db.chars 里造空行（那会让角色清单多出一行永远没有变动的号）。
local function CaptureClass(db, key)
    local fn = _G.UnitClass
    if not key or type(fn) ~= "function" then return end
    local okCall, _, token = pcall(fn, "player")
    if not okCall or type(token) ~= "string" or token == "" then return end
    if type(db.classByChar) ~= "table" then db.classByChar = {} end
    db.classByChar[key] = token
    local c = db.chars[key]
    if type(c) == "table" then c.class = token end
end

--- 历史号的跨号职业来源：GQL 的 GQLDB 也是 account-wide SavedVariables，同一个号
--- 在 GQL 下登录跑过快照就留下 class（GoldQuestLedger/Sync.lua:1173-1180），
--- key 拼法与这里一样（Sync.lua:101-113 的 "名字-服务器"）。GDL 是后装的，
--- 自己的库里只有本轮登录过的号 —— 用户截图里"只有登录了谁谁的颜色才对"就是这道差。
--- 只读地借 token（绝不写 GQLDB），借到就缓存进自己的库；GQL 没装 / 形状不对 /
--- 号不在里面 / token 不是职业色表里认得的，一律退回白色，绝不编色、绝不报错。
local function BorrowClass(db, key)
    if type(db) ~= "table" or type(key) ~= "string" then return nil end
    local gql = _G.GQLDB
    if type(gql) ~= "table" or type(gql.chars) ~= "table" then return nil end
    local row = gql.chars[key]
    local token = (type(row) == "table") and row.class or nil
    if type(token) ~= "string" or token == "" then return nil end
    local cc = _G.RAID_CLASS_COLORS
    if type(cc) ~= "table" or type(cc[token]) ~= "table" then return nil end
    if type(db.classByChar) ~= "table" then db.classByChar = {} end
    db.classByChar[key] = token
    return token
end

---------------------------------------------------------------------------
-- 唯一写入路径
---------------------------------------------------------------------------

--- @param signedAmount number 带符号铜币（>0 收入，<0 支出，搬钱可正可负）
--- @param source string       来源名（Money 传进来；transfer 用它判，不看 meta.transfer）
--- @param meta table|nil      Money 的探针 meta：只把真读到的 sender/subject 留在条目上
--- @return boolean 是否写成了这一笔（false = 这笔没进账本，已计入 droppedWrites）
function Ledger:AddEntry(signedAmount, source, meta)
    if type(signedAmount) ~= "number" or type(source) ~= "string" then
        counters.droppedWrites = counters.droppedWrites + 1
        return false
    end

    local charKey = CurrentCharKey()
    if not charKey then
        -- 号名/服名读不出来：宁可不写，也绝不给全账号存档塞一个假号键。
        counters.droppedWrites = counters.droppedWrites + 1
        if not counters.charWarned then
            counters.charWarned = true
            print("GDL: 号名或服名读不出来，这笔没写进账本（只提醒一次，不写占位假号）")
        end
        return false
    end

    local now = Ledger.WallClock()
    local dateKey = now and Ledger.DateKey(now) or nil
    if not dateKey then
        -- 墙上时钟读不出合法 ISO 键：同样不写，免得造出一个错日期的死行。
        counters.droppedWrites = counters.droppedWrites + 1
        return false
    end

    currentChar = charKey
    local transfer = Ledger.IsTransferSource(source)
    local db = DB()

    local entry = {
        t        = now,
        dateKey  = dateKey,          -- 写时算好，流水页扫的时候不再重算日期
        char     = charKey,
        src      = source,
        amt      = signedAmount,
        transfer = transfer,
    }
    if type(meta) == "table" then
        if meta.sender  ~= nil then entry.sender  = meta.sender  end
        if meta.subject ~= nil then entry.subject = meta.subject end
    end
    -- 卖店连发在**写入层**并成一条（用户 2026-11-10：「记录的时候就记录一条」）。
    -- 只跟紧挨着的上一笔比：同号 / 同日 / 同来源 vendor / 同方向 / 非搬钱 / 3 秒内。
    -- 下面 daily 与 byDayChar 仍按本次 signedAmount 逐笔累加，总量一次不少；
    -- 读出层 MergeVendorRuns 留着兼容**旧存档里已经散开**的明细，对新数据是幂等的。
    local last = db.entries[#db.entries]
    local mergeable = not transfer and source:lower() == VENDOR_MERGE_SOURCE
        and signedAmount ~= 0 and type(last) == "table" and last.src == source
        and not last.transfer and last.char == charKey and last.dateKey == dateKey
        and type(last.t) == "number" and math.abs(now - last.t) <= VENDOR_MERGE_SECONDS
        and ((tonumber(last.amt) or 0) > 0) == (signedAmount > 0)
    if mergeable then
        last.amt = (tonumber(last.amt) or 0) + signedAmount
        last.t = now
        last.mergedCount = (tonumber(last.mergedCount) or 1) + 1
    else
        db.entries[#db.entries + 1] = entry
    end
    TouchChar(db, charKey, now)
    CaptureClass(db, charKey)

    if not transfer then
        local day = db.daily[dateKey]
        if not day then day = { income = 0, expense = 0 } ; db.daily[dateKey] = day end

        local byChar = db.byDayChar[dateKey]
        if not byChar then byChar = {} ; db.byDayChar[dateKey] = byChar end
        local cell = byChar[charKey]
        if not cell then cell = { income = 0, expense = 0 } ; byChar[charKey] = cell end

        if signedAmount > 0 then
            day.income  = day.income  + signedAmount
            cell.income = cell.income + signedAmount
        elseif signedAmount < 0 then
            local out = -signedAmount
            day.expense  = day.expense  + out
            cell.expense = cell.expense + out
        end
    end

    return true
end

---------------------------------------------------------------------------
-- 迁移表（schema 档位）：只留空档，日后加字段走这里，迁移必须在读之前跑完。
---------------------------------------------------------------------------

Ledger.MIGRATIONS = {
    -- [2] = function(db) ... end,   -- 日后加字段走这里，迁移必须在读之前跑完
}

function Ledger.RunMigrations(db)
    db = db or DB()
    local schema = tonumber(db.schema)
    if schema == nil then schema = CURRENT_SCHEMA end   -- 没标档当当前档（默认值已立 1）
    if schema < CURRENT_SCHEMA then
        for v = schema + 1, CURRENT_SCHEMA do           -- 逐档向前迁，不跳档
            local m = Ledger.MIGRATIONS[v]
            if m then m(db) end
        end
        db.schema = CURRENT_SCHEMA
    end
    -- schema > CURRENT_SCHEMA：存档来自更新版本，原样保留，绝不降级回写
end

---------------------------------------------------------------------------
-- 清理（OnInitialize 跑，先迁移后清理）
---------------------------------------------------------------------------

function Ledger.Cleanup(db)
    db = db or DB()
    local now = Ledger.WallClock()
    if now then
        -- 日期键剪枝选"字符串比大小"而非"墙上钟换算"：键已定宽零填充，字典序==时间序，
        -- 与 GoldLedger 一致且不用为每一天再换算一次时间戳（那才是真会算错的地方）。
        local cutoffKey = Ledger.DateKey(now - DAILY_KEEP_DAYS * SECONDS_PER_DAY)
        if cutoffKey then
            for dateKey in pairs(db.daily) do
                if IsIsoDay(dateKey) and dateKey < cutoffKey then
                    db.daily[dateKey] = nil
                    db.byDayChar[dateKey] = nil       -- 过期天的 byDayChar 行一起丢
                end
            end
            -- daily 里本就没有的天，byDayChar 也可能单独留着一行：同样按 ISO 键清掉
            for dateKey in pairs(db.byDayChar) do
                if IsIsoDay(dateKey) and dateKey < cutoffKey then
                    db.byDayChar[dateKey] = nil
                end
            end
        end

        -- 条目按时间清：遍历 entries 里引用到的每一个号（不是只清当前登录号 —— 这就是
        -- 不背 GoldLedger 那个坑的关键）。cutoffKey 之外，条目本身按 t 绝对秒数剪。
        local cutoffSec = now - DAILY_KEEP_DAYS * SECONDS_PER_DAY
        local kept = {}
        local entries = db.entries
        for i = 1, #entries do
            local e = entries[i]
            if type(e) == "table" and type(e.t) == "number" and e.t >= cutoffSec then
                kept[#kept + 1] = e
            end
        end
        for i = 1, #entries do entries[i] = nil end
        for i = 1, #kept do entries[i] = kept[i] end
    end

    -- 全局上限：entries 新在前（追加在尾），丢最老就是从 1 开始砍。
    local entries = db.entries
    local over = #entries - ENTRIES_CAP
    if over > 0 then
        for i = 1, over do table.remove(entries, 1) end
    end

    -- chars 行：只保留仍在被引用的号（entries 或 byDayChar 里还出现的）。
    local referenced = {}
    for i = 1, #db.entries do
        local e = db.entries[i]
        if type(e) == "table" and type(e.char) == "string" then referenced[e.char] = true end
    end
    for _, byChar in pairs(db.byDayChar) do
        if type(byChar) == "table" then
            for charKey in pairs(byChar) do referenced[charKey] = true end
        end
    end
    for charKey in pairs(db.chars) do
        if not referenced[charKey] then db.chars[charKey] = nil end
        local c = db.chars[charKey]
        if type(c) == "table" then
            if type(c.firstSeen) ~= "number" then c.firstSeen = nil end
            if type(c.lastSeen)  ~= "number" then c.lastSeen  = nil end
        end
    end
end

---------------------------------------------------------------------------
-- 生命周期
---------------------------------------------------------------------------

function Ledger:OnInitialize()
    local db = DB()
    Ledger.RunMigrations(db)     -- 迁移必须在读之前跑完（MIGRATIONS 表注释）
    Ledger.Cleanup(db)
    currentChar = CurrentCharKey()
    counters.charWarned = false  -- 每次开机给一次重新提醒的机会
end

function Ledger:OnLogin()
    local ck = CurrentCharKey()
    if ck then currentChar = ck end
    -- 每个号只要登录过一次，职业色就永久留下（classByChar 不造空号）；
    -- 首次记账那笔也会补（见 AddEntry），所以没登录事件的那条路径也拿得到。
    if ck then CaptureClass(DB(), ck) end
end

---------------------------------------------------------------------------
-- 读数接口（全部是纯查表，不碰任何事件接口）
---------------------------------------------------------------------------

local function netOf(cell)
    return Num(cell and cell.income) - Num(cell and cell.expense)
end

--- 一格 + 它的钻取行。chars 是按号取用的映射（计划原形），charOrder 是给界面按序渲染的行序：
--- 当前号在就置顶，其余按净降序、同名升序保证顺序稳定。零变动的号不列。
--- @return table { income, expense, net, chars = { [charKey] = {income, expense} }, charOrder = {charKey...} }
function Ledger.GetDay(dateKey)
    local out = { income = 0, expense = 0, net = 0, chars = {}, charOrder = {} }
    if not IsIsoDay(dateKey) then return out end
    local db = DB()
    local day = db.daily[dateKey]
    out.income  = Num(day and day.income)
    out.expense = Num(day and day.expense)
    out.net     = out.income - out.expense

    local byChar = db.byDayChar[dateKey]
    if type(byChar) == "table" then
        for charKey, cell in pairs(byChar) do
            local inc, exp = Num(cell.income), Num(cell.expense)
            if inc ~= 0 or exp ~= 0 then
                out.chars[charKey] = { income = inc, expense = exp }
                out.charOrder[#out.charOrder + 1] = charKey
            end
        end
        table.sort(out.charOrder, function(a, b)
            local na, nb = netOf(out.chars[a]), netOf(out.chars[b])
            if na ~= nb then return na > nb end
            if a == currentChar then return true end
            if b == currentChar then return false end
            return a < b
        end)
    end
    return out
end

--- 范围内的全账号收支合计（就是日历一格一格的和；纯字符串区间，因为键是定宽 ISO）。
--- @return table { income, expense, net }
function Ledger.GetRangeTotals(startKey, endKey)
    local out = { income = 0, expense = 0, net = 0 }
    if not IsIsoDay(startKey) or not IsIsoDay(endKey) or startKey > endKey then
        return out
    end
    local db = DB()
    for dateKey, day in pairs(db.daily) do
        if IsIsoDay(dateKey) and dateKey >= startKey and dateKey <= endKey then
            out.income  = out.income  + Num(day.income)
            out.expense = out.expense + Num(day.expense)
        end
    end
    out.net = out.income - out.expense
    return out
end

--- 账本里最早有记录的那天（ISO 键）；一条记录都没有时回 nil。
--- 「全部」档悬停的日期起点用它：哨兵 0000-01-01 没有信息量（用户 2026-11-10 截图反馈，
--- 定稿：要么从真实记录的日期算起到当前时间，要么就别写日期）。
--- 纯字符串比大小，键是定宽 ISO，字典序即时间序。
function Ledger.FirstDateKey()
    local db = DB()
    local best = nil
    for dateKey in pairs(db.daily) do
        if IsIsoDay(dateKey) and (best == nil or dateKey < best) then
            best = dateKey
        end
    end
    return best
end

--- 范围内每个号的合计行，净降序、并列时当前号置顶。零变动的号不列。
--- @return table[] { { name, income, expense, net }, ... }
function Ledger.GetRangeChars(startKey, endKey)
    local rows = {}
    if not IsIsoDay(startKey) or not IsIsoDay(endKey) or startKey > endKey then
        return rows
    end
    local db = DB()
    local acc = {}
    for dateKey, byChar in pairs(db.byDayChar) do
        if IsIsoDay(dateKey) and dateKey >= startKey and dateKey <= endKey
           and type(byChar) == "table" then
            for charKey, cell in pairs(byChar) do
                local a = acc[charKey]
                if not a then a = { income = 0, expense = 0 } ; acc[charKey] = a end
                a.income  = a.income  + Num(cell.income)
                a.expense = a.expense + Num(cell.expense)
            end
        end
    end
    for charKey, a in pairs(acc) do
        if a.income ~= 0 or a.expense ~= 0 then
            rows[#rows + 1] = { name = charKey, income = a.income, expense = a.expense,
                                net = a.income - a.expense }
        end
    end
    table.sort(rows, function(x, y)
        if x.net ~= y.net then return x.net > y.net end
        if x.name == currentChar then return true end
        if y.name == currentChar then return false end
        return x.name < y.name
    end)
    return rows
end

--- 卖店连发合并：只改"流水里列出什么"，不改 db.entries 里任何一条。
--- 条件全齐才并：同号、同一天、同一来源 vendor、同一方向、非搬钱、
--- 相邻两条间隔 ≤ VENDOR_MERGE_SECONDS；中间夹一笔别的来源/别的号就断开，
--- 免得把两次不同的卖东西（甚至两个号的）揉成一条。合并行带 mergedCount，
--- 界面据此标出这笔并了几条，玩家一眼看得出不是漏记。
local function MergeVendorRuns(matched)
    local out = {}
    local i = 1
    while i <= #matched do
        local head = matched[i]
        local e = head.entry
        local amt = Num(e.amt)
        local eligible = (type(e.src) == "string" and e.src:lower() == VENDOR_MERGE_SOURCE)
            and amt ~= 0 and not e.transfer and type(head.dk) == "string"
        if not eligible then
            out[#out + 1] = head
            i = i + 1
        else
            local sign = amt > 0 and 1 or -1
            -- 旧存档里可能已经有写入层并过的条目（自带 mergedCount）：笔数要累加，
            -- 否则两组再被读出层并一次就只会报"2 笔"。
            local sum, count = amt, tonumber(e.mergedCount) or 1
            local prevT = Num(head.t)
            local j = i + 1
            while j <= #matched do
                local nx = matched[j]
                local ne = nx.entry
                local na = Num(ne.amt)
                local nt = Num(nx.t)
                local same = (type(ne.src) == "string" and ne.src:lower() == VENDOR_MERGE_SOURCE)
                    and na * sign > 0 and not ne.transfer
                    and ne.char == e.char and nx.dk == head.dk
                    and math.abs(prevT - nt) <= VENDOR_MERGE_SECONDS
                if not same then break end
                sum, count = sum + na, count + (tonumber(ne.mergedCount) or 1)
                prevT = nt
                j = j + 1
            end
            if count > 1 then
                local merged = {}
                for k, v in pairs(e) do merged[k] = v end
                merged.amt = sum
                merged.mergedCount = count
                out[#out + 1] = { entry = merged, t = head.t, idx = head.idx, dk = head.dk }
            else
                out[#out + 1] = head
            end
            i = j
        end
    end
    return out
end

--- 某号某范围的流水分页（新在前）+ 该范围的来源小计。
--- 这一条按任务书是"点击时扫一遍"的有界扫描 —— 只有它扫 entries，日历和顶栏绝不扫。
--- charKey 传 nil = 不限号；source 传 nil = 不限类；direction = "in"/"out" 时只收正/负额。
--- bySource 覆盖整个 char+range、**不受 source/direction 筛选影响**：小计行永远列全，
--- 缩了就只剩一行没意义（用户 2026-11-10：分类汇总挪到侧边，仍要能点它换类）。
--- @return table { rows, total, totalPages, page, perPage, bySource = { [src] = {income, expense} } }
function Ledger.GetCharEntries(charKey, startKey, endKey, page, perPage, source, direction)
    local res = { rows = {}, total = 0, totalPages = 0, page = 1, perPage = DEFAULT_PER_PAGE,
                  bySource = {} }
    if (charKey ~= nil and type(charKey) ~= "string") or not IsIsoDay(startKey) or not IsIsoDay(endKey)
       or startKey > endKey then
        return res
    end
    if type(source) ~= "string" then source = nil end
    page    = (type(page)    == "number" and page    >= 1) and math.floor(page)    or 1
    perPage = (type(perPage) == "number" and perPage >= 1) and math.floor(perPage) or DEFAULT_PER_PAGE

    local db = DB()
    local matched = {}
    for i = 1, #db.entries do
        local e = db.entries[i]
        if type(e) == "table" and (charKey == nil or e.char == charKey) then
            local dk = e.dateKey
            if dk == nil and type(e.t) == "number" then dk = Ledger.DateKey(e.t) end
            if IsIsoDay(dk) and dk >= startKey and dk <= endKey then
                matched[#matched + 1] = { entry = e, t = Num(e.t), idx = i, dk = dk }
            end
        end
    end

    -- 新在前：按 t 降序，同一秒按写入顺序倒序（idx 大者新）。
    table.sort(matched, function(a, b)
        if a.t ~= b.t then return a.t > b.t end
        return a.idx > b.idx
    end)

    -- 来源小计覆盖整个 char+range（不是只有这一页，也不被 source 筛选缩小）：
    -- 弹窗里点任一小计行就切到那类，缩成一行就只剩"撤销"、换不了类了。
    -- 搬钱也在流水里列出并带 transfer 标，所以它自己的来源桶也照列，与小计对得上。
    for i = 1, #matched do
        local e = matched[i].entry
        local src = (type(e.src) == "string") and e.src or "unknown"
        local bucket = res.bySource[src]
        if not bucket then bucket = { income = 0, expense = 0 } ; res.bySource[src] = bucket end
        local amt = Num(e.amt)
        if amt > 0 then
            bucket.income = bucket.income + amt
        elseif amt < 0 then
            bucket.expense = bucket.expense - amt
        end
    end

    -- 卖店连发先并成一行再分页：total / 页数 / rows 都按"玩家看得见的行"算。
    -- bySource 上面已经按原始明细算完，合并只改展示、不改任何金额。
    local dir = (direction == "in" or direction == "out") and direction or nil
    local listed = MergeVendorRuns(matched)
    if source ~= nil or dir ~= nil then
        listed = {}
        for i = 1, #matched do
            local e = matched[i].entry
            local src = (type(e.src) == "string") and e.src or "unknown"
            local amt = Num(e.amt)
            local srcOK = (source == nil or src == source)
            local dirOK = (dir == nil) or (dir == "in" and amt > 0) or (dir == "out" and amt < 0)
            if srcOK and dirOK then listed[#listed + 1] = matched[i] end
        end
    end

    res.total = #listed
    res.page, res.perPage = page, perPage
    res.totalPages = #listed == 0 and 0 or math.ceil(#listed / perPage)

    local first = (page - 1) * perPage + 1
    for i = first, first + perPage - 1 do
        local m = listed[i]
        if not m then break end
        res.rows[#res.rows + 1] = m.entry
    end
    return res
end

--- 已知的所有角色键（chars 行在每次写入都会落，所以它就是全集）。
function Ledger.GetCharKeys()
    local db = DB()
    local out = {}
    for charKey in pairs(db.chars) do out[#out + 1] = charKey end
    table.sort(out)
    return out
end

---------------------------------------------------------------------------
-- 角色显示名 / 职业色（账本 key 永远是 "名字-服务器"，这里只切展示）
---------------------------------------------------------------------------

-- 没存到职业 token 的旧号退白色（GoldQuestLedger/Matrix.lua:149-162 同一口径）。
-- 原来退的是中性灰，用户 2026-11-10 截图里「其他角色名字都是灰的」就是这个兜底；
-- 白色是"颜色读不出来"，不是"这个号不重要"，更不冒充任何职业色。
local NEUTRAL_RGB = { 1, 1, 1 }

--- 只显示名字、不带服务器（用户 2026-11-10 定）。照 GoldQuestLedger/Matrix.lua:164-170
--- 的 ShortCharKey 口径：key 本身一个字不改，跨服不撞的保证不动。
function Ledger.ShortName(charKey)
    if type(charKey) ~= "string" then return "" end
    local name = charKey:match("^[^-]+")
    return (name and name ~= "") and name or charKey
end

--- RAID_CLASS_COLORS 只有 .r/.g/.b / .colorStr，没有 .colorHex
--- （GoldQuestLedger/Matrix.lua:145-162 记录的那次"全白"就是误用了后者）。
--- token 的兜底链：chars[key].class → classByChar[key]（本插件登录过）→
--- GQLDB.chars[key].class（GQL 登录过）→ 白色。三处都没有才退白，绝不编色。
function Ledger.CharColor(charKey)
    local db = DB()
    local c = (type(charKey) == "string") and db.chars[charKey] or nil
    local token = (type(c) == "table") and c.class or nil
    if type(token) ~= "string" and type(db.classByChar) == "table" then
        token = db.classByChar[charKey]
    end
    if type(token) ~= "string" then token = BorrowClass(db, charKey) end
    local cc = _G.RAID_CLASS_COLORS
    if type(token) == "string" and type(cc) == "table" and type(cc[token]) == "table" then
        local r, g, b = cc[token].r, cc[token].g, cc[token].b
        if type(r) == "number" and type(g) == "number" and type(b) == "number" then
            return { r, g, b }
        end
    end
    return { NEUTRAL_RGB[1], NEUTRAL_RGB[2], NEUTRAL_RGB[3] }
end

--- 带职业色转义的短名：右栏行、弹窗标题、下拉选项共用这一处，避免三处各染各的。
--- maxBytes 按 UTF-8 整字截断（截在半字符上客户端画方块）；截断不加省略号，
--- 跟着 GoldQuestLedger/Matrix.lua:175-192 的 ShortName 口径走。
function Ledger.CharLabel(charKey, maxBytes)
    local name = Ledger.ShortName(charKey)
    if type(maxBytes) == "number" and maxBytes > 0 and #name > maxBytes then
        local cut = maxBytes
        while cut > 0 do
            local b = name:byte(cut + 1)
            if not b or b < 0x80 or b >= 0xC0 then break end
            cut = cut - 1
        end
        name = name:sub(1, cut)
    end
    local c = Ledger.CharColor(charKey)
    return ("|cff%02x%02x%02x%s|r"):format(
        math.floor(c[1] * 255 + 0.5),
        math.floor(c[2] * 255 + 0.5),
        math.floor(c[3] * 255 + 0.5), name)
end

function Ledger.GetLastSeen(charKey)
    if type(charKey) ~= "string" then return nil end
    local c = DB().chars[charKey]
    return type(c) == "table" and tonumber(c.lastSeen) or nil
end

---------------------------------------------------------------------------
-- /gdl ledger：只读取证，不改任何状态。首要要求是绝不报错。
---------------------------------------------------------------------------

local function Count(t)
    local n = 0
    if type(t) == "table" then for _ in pairs(t) do n = n + 1 end end
    return n
end

local function PrintLedger()
    local now, srcName = Ledger.WallClock()
    if now then
        local key = Ledger.DateKey(now)
        print(("GDL ledger: 墙上时钟来源 %s = %d，今天键 %s")
              :format(tostring(srcName), now, tostring(key)))
    else
        print("GDL ledger: 没有可用的墙上时钟（" .. tostring(srcName) .. "）")
    end
    local db = type(_G.GDLDB) == "table" and _G.GDLDB or {}
    print(("GDL ledger: 存了 %d 天 / %d 笔流水 / %d 个角色 / 丢弃写入 %d 笔")
          :format(Count(db.daily), Count(db.entries), Count(db.chars), counters.droppedWrites))
end

if GDL.RegisterCommand then
    GDL.RegisterCommand("ledger", PrintLedger, "只读：账本规模与墙上时钟来源取证")
end

return Ledger
