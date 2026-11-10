-- 存储层（T2）：全插件唯一的一处写入路径。三处冗余（daily / byDayChar / entries）
-- 在这里一次写齐，其余一切（日历格、钻取行、范围合计、流水页）都只是查表，绝不现算。
-- 计划第 2 节是这张表的数据模型；第 3 节的界面要什么，第 6 节的读数接口就给什么。
--
-- 两条从别的包里学到的硬规矩，都写在这里而不是散落到各处：
--   1. 时间戳必须落在同一个"墙上时钟"域里（GetTime 每次 reload/开机归零，存进去
--      就跟下一次开机的数不可比了 —— GQL 的实测教训，见 GoldQuestLedger/Api.lua:1432-1447）。
--   2. 清理必须遍历全部号，不能只清当前登录号（GoldLedger 的坑，见 GoldLedger/Data.lua:605-606）。

local GCL = _G.GCL
local Ledger = {}

if GCL and GCL.RegisterModule then
    GCL.RegisterModule("Ledger", Ledger)
else
    GCL = GCL or {}
    GCL.Ledger = Ledger
end

---------------------------------------------------------------------------
-- 常量（改动要连同用例一起改）
---------------------------------------------------------------------------

local SECONDS_PER_DAY = 86400
local DAILY_KEEP_DAYS = 400     -- daily/byDayChar 保 400 天（计划 §2）
local ENTRIES_CAP       = 20000 -- entries 全局上限，超了丢最老（计划 §2）
local CURRENT_SCHEMA    = 1     -- schema 已在 Core 的 DB_DEFAULTS 里立成 1
local DEFAULT_PER_PAGE  = 20

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
    if type(_G.GCLDB) ~= "table" then _G.GCLDB = {} end
    local db = _G.GCLDB
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
            print("GCL: 号名或服名读不出来，这笔没写进账本（只提醒一次，不写占位假号）")
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
    db.entries[#db.entries + 1] = entry
    TouchChar(db, charKey, now)

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

--- 某号某范围的流水分页（新在前）+ 该范围的来源小计。
--- 这一条按任务书是"点击时扫一遍"的有界扫描 —— 只有它扫 entries，日历和顶栏绝不扫。
--- @return table { rows, total, totalPages, page, perPage, bySource = { [src] = {income, expense} } }
function Ledger.GetCharEntries(charKey, startKey, endKey, page, perPage)
    local res = { rows = {}, total = 0, totalPages = 0, page = 1, perPage = DEFAULT_PER_PAGE,
                  bySource = {} }
    if type(charKey) ~= "string" or not IsIsoDay(startKey) or not IsIsoDay(endKey)
       or startKey > endKey then
        return res
    end
    page    = (type(page)    == "number" and page    >= 1) and math.floor(page)    or 1
    perPage = (type(perPage) == "number" and perPage >= 1) and math.floor(perPage) or DEFAULT_PER_PAGE

    local db = DB()
    local matched = {}
    for i = 1, #db.entries do
        local e = db.entries[i]
        if type(e) == "table" and e.char == charKey then
            local dk = e.dateKey
            if dk == nil and type(e.t) == "number" then dk = Ledger.DateKey(e.t) end
            if IsIsoDay(dk) and dk >= startKey and dk <= endKey then
                matched[#matched + 1] = { entry = e, t = Num(e.t), idx = i }
            end
        end
    end

    -- 新在前：按 t 降序，同一秒按写入顺序倒序（idx 大者新）。
    table.sort(matched, function(a, b)
        if a.t ~= b.t then return a.t > b.t end
        return a.idx > b.idx
    end)

    res.total = #matched
    res.page, res.perPage = page, perPage
    res.totalPages = #matched == 0 and 0 or math.ceil(#matched / perPage)

    local first = (page - 1) * perPage + 1
    for i = first, first + perPage - 1 do
        local m = matched[i]
        if not m then break end
        res.rows[#res.rows + 1] = m.entry
    end

    -- 来源小计覆盖整个 char+range（不是只有这一页）—— 界面要的是"这堆流水按来源合计多少"，
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

function Ledger.GetLastSeen(charKey)
    if type(charKey) ~= "string" then return nil end
    local c = DB().chars[charKey]
    return type(c) == "table" and tonumber(c.lastSeen) or nil
end

---------------------------------------------------------------------------
-- /gcl ledger：只读取证，不改任何状态。首要要求是绝不报错。
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
        print(("GCL ledger: 墙上时钟来源 %s = %d，今天键 %s")
              :format(tostring(srcName), now, tostring(key)))
    else
        print("GCL ledger: 没有可用的墙上时钟（" .. tostring(srcName) .. "）")
    end
    local db = type(_G.GCLDB) == "table" and _G.GCLDB or {}
    print(("GCL ledger: 存了 %d 天 / %d 笔流水 / %d 个角色 / 丢弃写入 %d 笔")
          :format(Count(db.daily), Count(db.entries), Count(db.chars), counters.droppedWrites))
end

if GCL.RegisterCommand then
    GCL.RegisterCommand("ledger", PrintLedger, "只读：账本规模与墙上时钟来源取证")
end

return Ledger
