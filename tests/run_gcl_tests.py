# GoldCalendarLedger 离线单测（T0 起跑）：toc 静态守卫 + Core 骨架行为。
# 用法：python tests/run_gcl_tests.py   —— 全绿退出码 0，有 FAIL 退出码 1。
# 替身规则原样继承 GQL 的三条教训：只给 Core 真碰到的入口、不许凭空造客户端没有的
# 方法（取用即抛错）、生命周期用例必须走真实事件入口而不是直接调内部函数。
import os
import re
import sys
from datetime import datetime, timezone

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "GoldCalendarLedger")
TOC_PATH = os.path.join(PLUGIN_DIR, "GoldCalendarLedger.toc")
ADDON_NAME = "GoldCalendarLedger"
FAILED = []


def fail(name, msg):
    FAILED.append(name)
    # Windows 控制台默认 cp936，不sanitize的中文/符号会直接把 print 炸掉，
    # 失败信息看不见了比失败本身更糟 —— 按控制台编码做无损降级替换。
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(msg).encode(enc, "replace").decode(enc, "replace")
    print(f"FAIL {name}: {safe}")


def ok(name):
    print(f"PASS {name}")


# WoW 环境替身：只列 Core.lua / Money.lua 真正碰到的入口。GetTime 留在名单里是给
# T1 的 transfer TTL（客户端 API，GQL 侧已实测存在）；其余一律不加。
#
# T1 新增的每一个入口都要能指到"存在性依据"，否则不许进替身（上一轮矩阵静默炸的教训）：
#   GetMoney / PLAYER_MONEY / MERCHANT_* / AUCTION_HOUSE_* / MAIL_* / TRADE_* / LOOT_* /
#   QUEST_TURNED_IN / hooksecurefunc / RepairAllItems / TakeInboxMoney / GetInboxHeaderInfo /
#   C_Bank.WithdrawMoney / C_Bank.DepositMoney / WithdrawGuildBankMoney / DepositGuildBankMoney
#     —— 以上全部是 GoldLedger/Tracker.lua 在用户本机 12.1.5 客户端上实际调过的名字
#     （那包是 vendored 参考，只借做法不借码），本机已跑通即视为存在；
#     GetMoney 另有 GQL 用例先例（run_gql_tests.py 的 GQL_MONEY/GetMoney 替身）。
#   issecretvalue —— 故意**默认不提供**：Tracker.lua:149 直接裸调它，而计划 §1 #5 要求
#     我们先判存在再 pcall，所以"缺席"本身就是要测的那条路；需要时由用例自己注入。
#   AUCTION_HOUSE_MAIL_SELLER —— 同样默认不提供（计划 §6：国服能不能拿到没验过）。
#
# T2 存储层新增的替身，每个都指得出"这仓库里已经证过它存在"的地方，否则不许进：
#   time / GetServerTime / GetLocalTime —— Ledger 的墙上时钟候选名单（照搬 GQL
#     Api.lua:1435 那份名字，GQL 套件用 GetServerTime/time 作为时钟源已实测通过）；
#     time 支持无参（返回注入的 GCL_EPOCH）与带日期表（GCL_DaysFromCivil 换算）两种签名，
#     这正是标准 os.time 的契约、WoW 的 time 复刻它。用例可按候选逐个置 nil 来验优先级/缺席。
#   date —— GoldLedger/Data.lua:55,69,272,487 就靠它写 %Y-%m-%d / %Y-%m / *t 的键，
#     在他 12.1.5 真机记账跑通即视为存在。这里按 GCL_EPOCH 做**确定性 UTC 拆分**
#     （GCL_CivilFromDays 的日历算法），绝不走 Python/os 的本地时区 —— 否则本机能过、
#     别的机器语义就变，用例等于没测。支持 "%Y-%m-%d"、"%Y-%m"、"*t"。
#   UnitName / GetRealmName —— GoldLedger/Data.lua:47-48 拼 "Name-Realm" 用的就是这两个
#     全局（GQL 套件同样用它们做角色键）；默认给非空串，用例可置 nil 验"读不出来不写假号"。
# 帧方法仍旧只给 Core 用到的那几个；下面这些替身都不新建帧，只补全局函数与表方法。
GCL_MOCK_ENV = r"""
UIParent = {}
SlashCmdList = {}
SLASH_GCL1 = nil
GCL_NOW = 0
GCL_OUTPUT = {}
GCL_FRAMES = {}
GCL_MONEY = 0
-- 墙上时钟注入点 + 角色键注入点（默认值是 2026-11-10 12:00 UTC，用例随时改）。
GCL_EPOCH = 1794312000
GCL_UNIT_NAME = "冒险者"
GCL_REALM_NAME = "暴龙王卡尔波斯"
-- 只列 Core.lua / Money.lua 注册的事件；真客户端对未知事件名是直接抛错
-- （12.1.5 实测 "Attempt to register unknown event"），替身照做。
GCL_KNOWN_EVENTS = {
    ADDON_LOADED = true, PLAYER_LOGIN = true,
    PLAYER_MONEY = true,
    MERCHANT_SHOW = true, MERCHANT_CLOSED = true,
    AUCTION_HOUSE_SHOW = true, AUCTION_HOUSE_CLOSED = true,
    MAIL_SHOW = true, MAIL_CLOSED = true,
    TRADE_SHOW = true, TRADE_CLOSED = true,
    LOOT_OPENED = true, LOOT_CLOSED = true,
    QUEST_TURNED_IN = true,
}
function GetTime() return GCL_NOW or 0 end
function GetMoney() return GCL_MONEY end
-- 确定性 UTC 日历算法（Howard Hinnant civil<->days）：不借 Python 的本地时区，
-- 否则这台机跑绿、那台机语义就变了，等于没测。date/time 全靠它把注入的纪元秒拆成键。
local function GCL_DaysFromCivil(y, m, d)
    y = y - ((m <= 2) and 1 or 0)
    local era = math.floor((y >= 0 and y or y - 399) / 400)
    local yoe = y - era * 400
    local mp = m + ((m > 2) and -3 or 9)
    local doy = math.floor((153 * mp + 2) / 5) + d - 1
    local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
    return era * 146097 + doe - 719468
end
local function GCL_CivilFromDays(z)
    z = z + 719468
    local era = math.floor((z >= 0 and z or z - 146096) / 146097)
    local doe = z - era * 146097
    local yoe = math.floor((doe - math.floor(doe / 1460) + math.floor(doe / 36524) - math.floor(doe / 146096)) / 365)
    local y = yoe + era * 400
    local doy = doe - (365 * yoe + math.floor(yoe / 4) - math.floor(yoe / 100))
    local mp = math.floor((5 * doy + 2) / 153)
    local d = doy - math.floor((153 * mp + 2) / 5) + 1
    local m = mp + ((mp < 10) and 3 or -9)
    return ((m <= 2) and (y + 1) or y), m, d
end
function time(t)
    if type(t) == "table" then
        local days = GCL_DaysFromCivil(tonumber(t.year) or 1970, tonumber(t.month) or 1, tonumber(t.day) or 1)
        return days * 86400 + (tonumber(t.hour) or 12) * 3600 + (tonumber(t.min) or 0) * 60 + (tonumber(t.sec) or 0)
    end
    return GCL_EPOCH or 0
end
function GetServerTime() return GCL_EPOCH or 0 end
function GetLocalTime() return GCL_EPOCH or 0 end
function date(fmt, t)
    fmt = fmt or "%Y-%m-%d"
    local sec = (type(t) == "number") and t or (GCL_EPOCH or 0)
    if type(sec) ~= "number" then return nil end
    local days = math.floor(sec / 86400)
    local rem = sec - days * 86400
    if rem < 0 then rem = rem + 86400 ; days = days - 1 end
    local hh = math.floor(rem / 3600)
    local mi = math.floor((rem % 3600) / 60)
    local ss = rem % 60
    local y, mo, d = GCL_CivilFromDays(days)
    local wday = ((days + 4) % 7) + 1     -- 1=周日（同 Lua *t.wday）；1970-01-01 days=0 = 周四
    if fmt == "*t" then
        return { year = y, month = mo, day = d, hour = hh, min = mi, sec = ss, wday = wday,
                 yday = (days - GCL_DaysFromCivil(y, 1, 1)) + 1 }
    end
    if fmt == "%Y-%m-%d" then return string.format("%04d-%02d-%02d", y, mo, d) end
    if fmt == "%Y-%m" then return string.format("%04d-%02d", y, mo) end
    return nil    -- 名单外的格式返回 nil：Ledger 的 safeDate 会当"读不出来"，绝不拿它当键
end
function UnitName(unit)
    return (type(GCL_UNIT_NAME) == "string" and GCL_UNIT_NAME ~= "") and GCL_UNIT_NAME or nil
end
function GetRealmName()
    return (type(GCL_REALM_NAME) == "string" and GCL_REALM_NAME ~= "") and GCL_REALM_NAME or nil
end
function print(...)
    for i = 1, select("#", ...) do
        GCL_OUTPUT[#GCL_OUTPUT + 1] = tostring((select(i, ...)))
    end
    return ...
end
-- 后置钩子的替身：按名字记下回调，"原生函数"被调用时才跑（客户端语义）。
-- 全局函数与表方法两种签名都要支持：hooksecurefunc("Name", fn) /
-- hooksecurefunc(C_Bank, "WithdrawMoney", fn)。
GCL_GLOBAL_HOOKS = {}
GCL_TABLE_HOOKS = {}
local function AppendHook(bucket, key, fn)
    local list = bucket[key]
    if not list then list = {} ; bucket[key] = list end
    list[#list + 1] = fn
    return fn
end
function hooksecurefunc(a, b, c)
    if type(a) == "string" then
        if type(b) ~= "function" then error("Usage: hooksecurefunc(name, func)") end
        return AppendHook(GCL_GLOBAL_HOOKS, a, b)
    end
    if type(a) ~= "table" or type(b) ~= "string" or type(c) ~= "function" then
        error("Usage: hooksecurefunc(table, key, func)")
    end
    local t = GCL_TABLE_HOOKS[a]
    if not t then t = {} ; GCL_TABLE_HOOKS[a] = t end
    return AppendHook(t, b, c)
end
function GCL_RunGlobal(name, ...)
    local list = GCL_GLOBAL_HOOKS[name]
    if list then for i = 1, #list do list[i](...) end end
end
function GCL_RunMethod(tbl, name, ...)
    local t = GCL_TABLE_HOOKS[tbl]
    local list = t and t[name]
    if list then for i = 1, #list do list[i](...) end end
end
-- 原生函数替身：函数体是空的（我们只关心后置钩子被叫醒），调用签名照客户端。
RepairAllItems = function(p1, p2, p3) return GCL_RunGlobal("RepairAllItems", p1, p2, p3) end
TakeInboxMoney = function(index) return GCL_RunGlobal("TakeInboxMoney", index) end
WithdrawGuildBankMoney = function(amount) return GCL_RunGlobal("WithdrawGuildBankMoney", amount) end
DepositGuildBankMoney = function(amount) return GCL_RunGlobal("DepositGuildBankMoney", amount) end
C_Bank = {
    WithdrawMoney = function(bankID, amount) return GCL_RunMethod(C_Bank, "WithdrawMoney", bankID, amount) end,
    DepositMoney = function(bankID, amount) return GCL_RunMethod(C_Bank, "DepositMoney", bankID, amount) end,
}
-- 收件箱夹具：用例往里塞 {sender, subject, money}，GetInboxHeaderInfo 按客户端
-- 的多返回值形状回（记账只取第 3、4、5 个）。没有这一条就无法复现
-- "TakeInboxMoney 当场抓快照" 这条实测做法（Tracker.lua:275-285）。
GCL_INBOX = {}
function GetInboxHeaderInfo(index)
    local m = GCL_INBOX[index]
    if type(m) ~= "table" then return nil end
    return nil, nil, m.sender, m.subject, m.money
end
function CreateFrame(frameType, name, parent, template)
    local f = { __events = {}, __scripts = {}, __shown = false, __type = frameType }
    f.SetScript = function(self, k, fn) self.__scripts[k] = fn end
    f.GetScript = function(self, k) return self.__scripts[k] end
    f.IsShown = function(self) return self.__shown end
    f.Show = function(self) self.__shown = true end
    f.Hide = function(self) self.__shown = false end
    f.RegisterEvent = function(self, e)
        if GCL_KNOWN_EVENTS[e] ~= true then
            error(('Attempt to register unknown event "%s"'):format(tostring(e)))
        end
        self.__events[e] = true
    end
    -- 取用名单外的任何方法名立刻抛错：上一轮矩阵静默炸的根因就是生产代码照着
    -- 替身里凭空造的方法写（"替身不许发明客户端没有的方法"）。
    setmetatable(f, { __index = function(_, k)
        error("GCL mock frame: 没有这个方法 " .. tostring(k))
    end })
    if name then _G[name] = f end
    GCL_FRAMES[#GCL_FRAMES + 1] = f
    return f
end
-- 真实事件入口：只有注册过该事件的帧才收到派发（客户端语义）。
-- 生命周期用例必须从这里进，不许直接调 Core 的内部函数。
function GCL_FireEvent(event, ...)
    local n = 0
    for i = 1, #GCL_FRAMES do
        local f = GCL_FRAMES[i]
        if f.__events[event] ~= nil and f.__scripts["OnEvent"] then
            f.__scripts["OnEvent"](f, event, ...)
            n = n + 1
        end
    end
    return n
end
"""


def toc_lines():
    with open(TOC_PATH, encoding="utf-8") as f:
        return f.read().splitlines()


def toc_lua_files():
    """toc 清单里非 `##` 头、非注释的行就是加载顺序（暴雪口径）。"""
    return [ln.strip() for ln in toc_lines()
            if ln.strip() and not ln.strip().startswith("##")
            and not ln.strip().startswith("--")]


def load_lua(*names):
    """按 toc 顺序把清单里的 Lua 全部加载进一个全新的带替身 LuaRuntime。
    以 toc 为唯一加载真源：T1/T2/T3 往 toc 里加文件后，这里一行不改。
    names 只用来断言"用例点名的模块确实在 toc 清单里"（防 toc 漏列时离线照跑、
    游戏里静默失效）。vararg 与游戏一致传 (插件名, 命名空间)。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(GCL_MOCK_ENV)
    lua.execute("GCL = GCL or {}")   # 命名空间：游戏里由 vararg 提供，测试预置
    files = toc_lua_files()
    for n in names:
        if n + ".lua" not in files:
            raise AssertionError("load_lua(%s)：该文件不在 toc 清单里" % n)
    for fn in files:
        with open(os.path.join(PLUGIN_DIR, fn), encoding="utf-8") as f:
            lua.execute(f.read(), ADDON_NAME, None)
    return lua, lua.globals().GCL


def fire(lua, event, *args):
    return lua.globals()["GCL_FireEvent"](event, *args)


def slash(lua, text):
    """走 SlashCmdList 真入口跑一条命令，返回这次 print 出来的行。"""
    out = lua.globals()["GCL_OUTPUT"]
    start = len(out)
    lua.globals()["SlashCmdList"]["GCL"](text)
    return [str(out[i]) for i in range(start + 1, len(out) + 1)]


def printed_lines(lua):
    out = lua.globals()["GCL_OUTPUT"]
    return [str(out[i]) for i in range(1, len(out) + 1)]


# ---------------------------------------------------------------------------
# T1 记账层的夹具：全部走真入口（事件从 GCL_FireEvent 进、钩子从客户端全局函数进、
# 命令从 SlashCmdList 进），一行都不直接调 Money 的内部函数。
# ---------------------------------------------------------------------------

def as_number(v):
    return None if v is None else int(round(float(v)))


def money_env(ledger=True):
    """装一个带假账本的干净环境（假账本把 AddEntry 的调用录进 GCL_CALLS）。
    ledger=False 用来验"账本还没注册时不许报错"那条降级 —— T2 起 Ledger.lua 进了 toc，
    load_lua 会把它也加载进来，所以这一档必须把真 Ledger 摘掉，才还原"账本缺席"。"""
    lua, _ = load_lua("Core", "Money")
    lua.execute("GCL_CALLS = {}")
    if ledger:
        lua.execute(r"""
            FakeLedger = { AddEntry = function(self, amount, source, meta)
                GCL_CALLS[#GCL_CALLS + 1] = { amount = amount, source = tostring(source),
                                              meta = meta }
            end }
            GCL.RegisterModule("Ledger", FakeLedger)
        """)
    else:
        lua.execute(r"""
            GCL.modules["Ledger"] = nil
            GCL.Ledger = nil
        """)
    return lua


def set_money(lua, amount):
    lua.execute("GCL_MONEY = %d" % int(amount))


def set_now(lua, seconds):
    lua.execute("GCL_NOW = %d" % int(seconds))


def set_seller(lua, value):
    """AUCTION_HOUSE_MAIL_SELLER：给 nil 就是"国服拿不到这个常量"那种情形。"""
    if value is None:
        lua.execute("AUCTION_HOUSE_MAIL_SELLER = nil")
    else:
        lua.execute("AUCTION_HOUSE_MAIL_SELLER = %r" % value)


def set_inbox(lua, index, sender, subject, money):
    lua.execute("GCL_INBOX[%d] = { sender = %s, subject = %s, money = %d }"
                % (int(index), ("%r" % sender) if sender is not None else "nil",
                   ("%r" % subject) if subject is not None else "nil", int(money)))


def boot(lua, money=1000):
    """真实启动顺序：ADDON_LOADED（挂钩子、订事件）→ PLAYER_LOGIN（取金币基准）。"""
    set_money(lua, money)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    fire(lua, "PLAYER_LOGIN")


def change_money(lua, amount):
    """改总额后发一笔 PLAYER_MONEY（客户端就是这么工作的：先变钱、再发事件）。"""
    set_money(lua, amount)
    fire(lua, "PLAYER_MONEY")


def calls(lua):
    """账本收到的每一笔：[(带符号金额, 来源), ...]。"""
    rows = lua.globals()["GCL_CALLS"]
    out, i = [], 1
    while True:
        row = rows[i]
        if row is None:
            break
        out.append((as_number(row.amount), str(row["source"])))
        i += 1
    return out


def call_meta(lua, index, key):
    row = lua.globals()["GCL_CALLS"][index]
    if row is None:
        return None
    meta = row.meta
    if meta is None:
        return None
    return str(meta[key]) if meta[key] is not None else None


def session_stats(lua):
    s = lua.globals()["GCL"].Money.GetSessionStats()
    return as_number(s.income), as_number(s.expense), as_number(s.net)


# ---------------------------------------------------------------------------
# T2 存储层的夹具：真 Ledger（不挂假账本），移动全部走 Money 的真实路径（change_money
# 带正确的上下文/号名/墙上钟），只有需要"另一个号的键"或复现某号流水时才直接 AddEntry。
# 一致性核对的算式放在 Lua 侧、贴着数据跑，Python 只收结论字符串。
# ---------------------------------------------------------------------------

def ledger_env():
    return load_lua("Core", "Money", "Ledger")[0]


def set_epoch(lua, seconds):
    lua.execute("GCL_EPOCH = %d" % int(seconds))


def set_char(lua, name, realm="暴龙王卡尔波斯"):
    lua.execute("GCL_UNIT_NAME = %s; GCL_REALM_NAME = %s"
                % (("%r" % name) if name is not None else "nil",
                   ("%r" % realm) if realm is not None else "nil"))


def set_clock_none(lua):
    """三个墙上钟候选全置 nil（用来验 /gcl ledger 在没钟时不报错）。"""
    lua.execute("time = nil GetServerTime = nil GetLocalTime = nil")


def ledger_eval(lua, expr):
    """跑一段 Lua **表达式**，返回结果（供各用例现取现用）。"""
    return lua.eval("(function() return (%s) end)()" % expr)


def ledger_block(lua, block):
    """跑一段 Lua **语句块**（自带 return），返回其 return 的值。
    多语句块不能塞进 ledger_eval 的 `return (...)`（那是非法 Lua）。"""
    return lua.eval("(function() %s end)()" % block)


def add_entry(lua, amount, source, meta_expr="nil"):
    """直接走唯一写入路径（不经过 Money）：用于另一号的键、或需要精确 meta 的用例。"""
    lua.execute("GCL.Ledger:AddEntry(%d, %s, %s)"
                % (int(amount), ("%r" % source), meta_expr))


# 一段贴数据的核对：daily == Σ byDayChar == Σ entries(排除 transfer)，且全范围合计 == Σ 天格。
# 返回问题串（空 = 一致）。GCL_CONSISTENCY 只读不写。
GCL_CONSISTENCY = r"""
function GCL_CONSISTENCY()
    local L = GCL.Ledger
    local db = GCLDB
    local bad = {}
    local dayKeys = {}
    for k in pairs(db.daily) do dayKeys[#dayKeys + 1] = k end
    table.sort(dayKeys)
    local minKey, maxKey = dayKeys[1], dayKeys[#dayKeys]

    -- 三处冗余互相对得上
    for _, d in ipairs(dayKeys) do
        local cell = db.daily[d]
        local bcInc, bcExp, eInc, eExp = 0, 0, 0, 0
        for _, c in pairs(db.byDayChar[d] or {}) do
            bcInc = bcInc + (c.income or 0)
            bcExp = bcExp + (c.expense or 0)
        end
        for i = 1, #(db.entries or {}) do
            local e = db.entries[i]
            if e.dateKey == d and not e.transfer then
                if e.amt > 0 then eInc = eInc + e.amt else eExp = eExp - e.amt end
            end
        end
        if cell.income ~= bcInc or cell.expense ~= bcExp then
            bad[#bad + 1] = ("day %s: daily(%d,%d) ~= ΣbyDayChar(%d,%d)")
                :format(d, cell.income, cell.expense, bcInc, bcExp)
        end
        if cell.income ~= eInc or cell.expense ~= eExp then
            bad[#bad + 1] = ("day %s: daily(%d,%d) ~= Σentries非转账(%d,%d)")
                :format(d, cell.income, cell.expense, eInc, eExp)
        end
    end

    -- 全账号范围合计 == 逐天格相加（就是日历一格一格的和）
    local sumInc, sumExp = 0, 0
    for _, d in ipairs(dayKeys) do
        sumInc = sumInc + db.daily[d].income
        sumExp = sumExp + db.daily[d].expense
    end
    if minKey then
        local rt = L.GetRangeTotals(minKey, maxKey)
        if rt.income ~= sumInc or rt.expense ~= sumExp then
            bad[#bad + 1] = ("GetRangeTotals(%d,%d) ~= Σ天格(%d,%d)")
                :format(rt.income, rt.expense, sumInc, sumExp)
        end
    end

    -- 转账绝不改任何总量：Σ daily == Σ 非转账 entries（跨全表）
    local allInc, allExp = 0, 0
    for _, d in ipairs(dayKeys) do allInc = allInc + db.daily[d].income; allExp = allExp + db.daily[d].expense end
    local enInc, enExp = 0, 0
    for i = 1, #(db.entries or {}) do
        local e = db.entries[i]
        if not e.transfer then
            if e.amt > 0 then enInc = enInc + e.amt else enExp = enExp - e.amt end
        end
    end
    if allInc ~= enInc or allExp ~= enExp then
        bad[#bad + 1] = ("Σdaily(%d,%d) ~= Σ非转账entries(%d,%d)")
            :format(allInc, allExp, enInc, enExp)
    end

    return table.concat(bad, " ;; ")
end
"""


def expect(name, problems):
    """用例末尾统一收口：把攒下的不符项一次报全（只报第一条会让人以为其余都对）。"""
    if problems:
        return fail(name, "；".join(problems))
    return ok(name)



def test_toc_lists_only_existing_files_and_covers_every_lua():
    """静态守卫：toc 列的文件必须都在，目录里的 .lua 必须都在 toc 里 ——
    漏列的模块游戏里根本不加载（静默失效），列了不存在的文件整包报错。"""
    listed = toc_lua_files()
    missing = [fn for fn in listed if not os.path.isfile(os.path.join(PLUGIN_DIR, fn))]
    actual = sorted(fn for fn in os.listdir(PLUGIN_DIR) if fn.endswith(".lua"))
    unlisted = [fn for fn in actual if fn not in listed]
    if missing or unlisted:
        return fail("toc_lists_only_existing_files_and_covers_every_lua",
                    "toc 列了但不存在：%s；目录里有但 toc 没列：%s"
                    % ("、".join(missing) or "无", "、".join(unlisted) or "无"))
    ok("toc_lists_only_existing_files_and_covers_every_lua")


def test_version_matches_toc():
    """版本号两处一致（toc 的 ## Version 与运行时的 GCL.VERSION）。
    本插件尚无 CHANGELOG 条目（第 D 项口径：不新建、不改 CHANGELOG.md），
    所以是三处里的两处；第一处进 CHANGELOG 时再补第三处。"""
    m = None
    for ln in toc_lines():
        h = re.match(r"^##\s*Version:\s*(\S+)\s*$", ln)
        if h:
            m = h.group(1)
    if m is None:
        return fail("version_matches_toc", "toc 里没有 ## Version 行")
    lua, _ = load_lua("Core")
    runtime = str(lua.globals()["GCL"]["VERSION"])
    if runtime != m:
        return fail("version_matches_toc", "toc=%r 运行时 GCL.VERSION=%r 不一致" % (m, runtime))
    ok("version_matches_toc")


def test_slash_command_registered():
    """斜杠命令两处注册耦合（SLASH_GCL1 全局 + SlashCmdList 的大写键名），
    改错任何一处聊天里命令直接消失且不报错 —— 只能行为地钉这两处。"""
    lua, _ = load_lua("Core")
    g = lua.globals()
    handler = g["SlashCmdList"]["GCL"]
    if not callable(handler):
        return fail("slash_command_registered",
                    "SlashCmdList['GCL'] 应为 function，实际 %r" % (handler,))
    if str(g["SLASH_GCL1"]) != "/gcl":
        return fail("slash_command_registered", "SLASH_GCL1=%r，应为 '/gcl'" % (str(g["SLASH_GCL1"]),))
    ok("slash_command_registered")


def test_module_registry_and_lifecycle():
    """模块注册表 + 生命周期：注册/查询/经真实事件入口跑 OnInitialize、OnLogin；
    一个模块自爆不许连坐；别人的 ADDON_LOADED 不许触发我们的初始化。"""
    lua, gcl = load_lua("Core")
    lua.execute(r"""
        GCL_INIT_COUNT, GCL_LOGIN_COUNT = 0, 0
        GCLGood = { OnInitialize = function() GCL_INIT_COUNT = GCL_INIT_COUNT + 1 end,
                    OnLogin = function() GCL_LOGIN_COUNT = GCL_LOGIN_COUNT + 1 end }
        GCLBoom = { OnInitialize = function() error("模块自爆") end,
                    OnLogin = function() GCL_LOGIN_COUNT = GCL_LOGIN_COUNT + 1 end }
        GCL.RegisterModule("good", GCLGood)
        GCL.RegisterModule("boom", GCLBoom)
    """)
    if not lua.eval('GCL.modules.good == GCLGood and GCL.good == GCLGood'):
        return fail("module_registry_and_lifecycle", "RegisterModule 没同时挂进 modules 和命名空间")
    if not lua.eval('GCL:GetModule("good") == GCLGood'):
        return fail("module_registry_and_lifecycle", "GetModule('good') 取回的不是注册的模块")
    if not lua.eval('GCL:GetModule("nope") == nil'):
        return fail("module_registry_and_lifecycle", "GetModule 未注册名应返回 nil")
    # 别的插件的 ADDON_LOADED：一个初始化都不许发生
    fire(lua, "ADDON_LOADED", "SomeOtherAddon")
    if lua.eval("GCL_INIT_COUNT") != 0 or lua.globals()["GCLDB"] is not None:
        return fail("module_registry_and_lifecycle", "别人的 ADDON_LOADED 触发了我们的初始化")
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    # boom 与 good 都在模块表里；pairs 顺序不定，但 pcall 逐个包 => good 必跑
    if lua.eval("GCL_INIT_COUNT") != 1:
        return fail("module_registry_and_lifecycle",
                    "OnInitialize 应恰好跑 good 一次（boom 的错被隔离），实际 %r"
                    % (lua.eval("GCL_INIT_COUNT"),))
    if not any("boom" in ln and "出错" in ln for ln in printed_lines(lua)):
        return fail("module_registry_and_lifecycle", "boom 模块的自爆没被报出来（静默吞错更糟）")
    fire(lua, "PLAYER_LOGIN")
    if lua.eval("GCL_LOGIN_COUNT") != 2:
        return fail("module_registry_and_lifecycle",
                    "PLAYER_LOGIN 应让两个模块的 OnLogin 都跑（T1 的金币基线钩这一步）")
    ok("module_registry_and_lifecycle")


def test_savedvariables_defaults_merge_without_overwrite():
    """GCLDB 合并只补缺失键：活档里已有的值（哪怕是旧 schema）一个都不许抹。"""
    lua, _ = load_lua("Core")
    lua.execute(r"""
        GCLDB = { schema = 7,
                  daily = { ["2026-11-10"] = { income = 5, expense = 0 } },
                  entries = { { t = time(), amt = 5 } } }
    """)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    checks = [
        ('GCLDB.schema', 7, "已有 schema 不许覆盖（迁移表 T2 落地，这里只立缺口）"),
        ('GCLDB.daily["2026-11-10"].income', 5, "已有流水不许被抹"),
        ('#GCLDB.entries', 1, "已有 entries 不许被清空"),
        ('GCLDB.settings ~= nil', True, "缺失键应被补上"),
        ('GCLDB.byDayChar ~= nil', True, "缺失键应被补上"),
        ('GCLDB.chars ~= nil', True, "缺失键应被补上"),
    ]
    for expr, want, why in checks:
        got = lua.eval(expr)
        if got != want:
            return fail("savedvariables_defaults_merge_without_overwrite",
                        "%s：%s（期望 %r 实际 %r）" % (why, expr, want, got))
    # 再喂一次（UI reload 口径）：合并必须幂等，不许把刚补的表换成新的
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    if lua.eval('GCLDB.daily["2026-11-10"].income') != 5:
        return fail("savedvariables_defaults_merge_without_overwrite", "二次初始化抹掉了已有数据")
    ok("savedvariables_defaults_merge_without_overwrite")


def test_dump_and_version_commands_do_not_error():
    """version / dump 走 /gcl 真入口：空库、有库都不许报错；
    空子命令与未知子命令打印排序后的命令清单。"""
    lua, _ = load_lua("Core")
    lines = slash(lua, "version")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "version 报错：%s" % lines)
    if not any("GCL 0.1.0" in ln and ADDON_NAME in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error",
                    "version 没打出 'GCL <版本> + toc 名'：%s" % lines)
    # 空库（还没跑过 ADDON_LOADED）dump 也不许炸
    lines = slash(lua, "dump")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "空库 dump 报错：%s" % lines)
    if not any("0 天" in ln and "0 笔流水" in ln and "0 个角色" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "空库 dump 应报全 0：%s" % lines)
    lua.execute(r"""
        GCLDB = { daily = { ["2026-11-10"] = {}, ["2026-11-11"] = {} },
                  entries = { { t = 1 }, { t = 2 }, { t = 3 } },
                  chars = { ["甲-暴龙王卡尔波斯"] = {} } }
    """)
    lines = slash(lua, "dump")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "有库 dump 报错：%s" % lines)
    if not any("2 天" in ln and "3 笔流水" in ln and "1 个角色" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error",
                    "有库 dump 计数不对：%s" % lines)
    want_cmds = sorted(str(k) for k in lua.globals()["GCL"]["commands"].keys())
    for probe in ("", "nosuchcommand"):
        lines = slash(lua, probe)
        joined = " / ".join(lines)
        if "出错" in joined:
            return fail("dump_and_version_commands_do_not_error", "%r 路径报错：%s" % (probe, lines))
        # 清单按注册表算，不写死名字：模块往 /gcl 里加子命令是常态，
        # 写死 "dump | version" 那种断言会在别人正常加命令时假红（T1 的 probe 就撞上过一次）。
        if not any(joined_line.endswith(" | ".join(want_cmds))
                   for joined_line in lines):
            return fail("dump_and_version_commands_do_not_error",
                        "%r 应打印排序后的命令清单 %s：%s" % (probe, want_cmds, lines))
    ok("dump_and_version_commands_do_not_error")


def test_money_baseline_and_delta_rules():
    """计划 §1 #1 #2：基线只在 PLAYER_LOGIN 之后取，delta==0 只刷基准，
    连续两笔各自独立（不并、不串），每笔恰好一条流水。"""
    name = "money_baseline_and_delta_rules"
    lua = money_env()
    problems = []
    # 初始化之前 PLAYER_MONEY 连事件都没订 ⇒ 一个帧都收不到
    set_money(lua, 5000)
    if fire(lua, "PLAYER_MONEY") != 0:
        problems.append("ADDON_LOADED 之前根帧就该收到 PLAYER_MONEY（说明订阅时机不对）")
    # 只跑 ADDON_LOADED（钩子与订阅已就位）就变钱：基准还没取，不许成收入
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    if fire(lua, "PLAYER_MONEY") < 1:
        problems.append("ADDON_LOADED 之后根帧应已订上 PLAYER_MONEY")
    change_money(lua, 6000)
    if calls(lua):
        problems.append("基线必须在 PLAYER_LOGIN 取；ADDON_LOADED 阶段的变动进了账")
    boot(lua, 1000)
    if calls(lua):
        problems.append("登录那一刻钱包里的钱被判成了收入")
    # delta == 0：刷基准、不发流水
    change_money(lua, 1000)
    if calls(lua):
        problems.append("delta==0 也发了一条流水")
    # 连续两笔
    change_money(lua, 1500)
    change_money(lua, 1200)
    if calls(lua) != [(500, "unknown"), (-300, "unknown")]:
        problems.append("连续两笔应为 +500/-300 各一条，实际 %s" % (calls(lua),))
    if session_stats(lua) != (500, 300, 200):
        problems.append("会话统计应为 (500, 300, 200)，实际 %s" % (session_stats(lua),))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("回调报错：%s" % bad)
    return expect(name, problems)


def test_money_context_events_map_and_clear():
    """计划 §1 #3：各 *_SHOW / LOOT_OPENED 设上下文，各 *_CLOSED 清回 unknown，
    任务上下文一笔就清。"""
    name = "money_context_events_map_and_clear"
    lua = money_env()
    boot(lua, 100000)
    # (事件, 这笔变动应判成的来源)：每步都是"开/关窗口 → 一笔 +100"
    script = [
        ("MERCHANT_SHOW", "vendor"), ("MERCHANT_CLOSED", "unknown"),
        ("AUCTION_HOUSE_SHOW", "ah"), ("AUCTION_HOUSE_CLOSED", "unknown"),
        ("MAIL_SHOW", "mail"), ("MAIL_CLOSED", "unknown"),
        ("TRADE_SHOW", "trade"), ("TRADE_CLOSED", "unknown"),
        ("LOOT_OPENED", "loot"), ("LOOT_CLOSED", "unknown"),
        ("QUEST_TURNED_IN", "quest"),
    ]
    money = 100000
    got = []
    for event, want in script:
        fire(lua, event)
        money += 100
        change_money(lua, money)
        got.append(calls(lua)[-1])
    # 任务上下文一笔就清：下一笔回 unknown
    money += 100
    change_money(lua, money)
    got.append(calls(lua)[-1])
    want = [(100, src) for src in [w for _, w in script]] + [(100, "unknown")]
    problems = []
    if got != want:
        problems.append("逐笔归属应为 %s，实际 %s" % (want, got))
    if len(calls(lua)) != len(script) + 1:
        problems.append("应有 %d 条流水，实际 %d 条" % (len(script) + 1, len(calls(lua))))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("回调报错：%s" % bad)
    return expect(name, problems)


def test_money_transfer_queue_rules():
    """计划 §1 #4：同额同向消费掉、不计收支、优先于开着的窗口；
    被拒绝的请求（没有 PLAYER_MONEY）过 TTL 后不许吃掉后面同额的真实变动；
    队列超上限丢最老；warband / guildbank 两个来源名。"""
    name = "money_transfer_queue_rules"
    problems = []

    # ① 取出=进钱、存入=出钱，两向都不算收支
    lua = money_env()
    boot(lua, 1000)
    lua.execute("C_Bank.WithdrawMoney(1, 500)")
    change_money(lua, 1500)
    lua.execute("C_Bank.DepositMoney(1, 200)")
    change_money(lua, 1300)
    lua.execute("WithdrawGuildBankMoney(70)")
    change_money(lua, 1370)
    if calls(lua) != [(500, "warband"), (-200, "warband"), (70, "guildbank")]:
        problems.append("搬钱三笔应为 warband/warband/guildbank，实际 %s" % (calls(lua),))
    if session_stats(lua) != (0, 0, 0):
        problems.append("搬钱不该进会话统计，实际 %s" % (session_stats(lua),))

    # ② 被拒绝的请求：30 秒后过期，不许吃掉后面同额的真实拾取
    lua = money_env()
    boot(lua, 1000)
    set_now(lua, 0)
    lua.execute("C_Bank.WithdrawMoney(1, 500)")      # 服务器拒绝了：永远等不到 PLAYER_MONEY
    set_now(lua, 31)
    fire(lua, "LOOT_OPENED")
    change_money(lua, 1500)
    if calls(lua) != [(500, "loot")]:
        problems.append("过期的搬钱请求吃掉了同额的真实拾取收入：%s" % (calls(lua),))
    if session_stats(lua) != (500, 0, 500):
        problems.append("过期的 500 应算真实收入，实际 %s" % (session_stats(lua),))

    # ③ 队列上限 10：最老那条被丢掉，于是它对应的金额按普通收支判
    lua = money_env()
    boot(lua, 1000)
    for amount in range(101, 112):                   # 11 条：101 被挤出去
        lua.execute("C_Bank.WithdrawMoney(1, %d)" % amount)
    change_money(lua, 1101)                          # +101（最老的已不在队列里）
    change_money(lua, 1212)                          # +111（还在队列里）
    if calls(lua) != [(101, "unknown"), (111, "warband")]:
        problems.append("超上限应丢最老（101 变真实收入、111 仍是搬钱），实际 %s" % (calls(lua),))

    # ④ 搬钱优先于开着的卖店窗口，且不把 vendor 上下文弄脏
    lua = money_env()
    boot(lua, 1000)
    fire(lua, "MERCHANT_SHOW")
    lua.execute("C_Bank.WithdrawMoney(1, 300)")
    change_money(lua, 1300)
    change_money(lua, 1250)                          # 紧接着真在卖店花钱
    if calls(lua) != [(300, "warband"), (-50, "vendor")]:
        problems.append("搬钱要压过 vendor 上下文，且后续支出仍是 vendor，实际 %s" % (calls(lua),))
    if session_stats(lua) != (0, 50, -50):
        problems.append("搬钱不计收支、vendor 支出要计，实际 %s" % (session_stats(lua),))

    # ⑤ 方向也要对上：排着"存入 400"（出钱）时，一笔同额的进钱绝不是搬钱
    lua = money_env()
    boot(lua, 1000)
    fire(lua, "LOOT_OPENED")
    lua.execute("C_Bank.DepositMoney(1, 400)")       # 请求存进战团银行 ⇒ 角色这边出钱
    change_money(lua, 1400)                          # 来的却是 400 收入（拾取）
    if calls(lua) != [(400, "loot")]:
        problems.append("同额但方向相反的搬钱请求不该被消费，实际 %s" % (calls(lua),))
    if session_stats(lua) != (400, 0, 400):
        problems.append("上面那笔是真实拾取收入，要计收支，实际 %s" % (session_stats(lua),))
    change_money(lua, 1000)                          # 现在才轮到那笔存入
    if calls(lua)[-1] != (-400, "warband"):
        problems.append("方向对上后该消费掉排队那条，实际 %s" % (calls(lua)[-1],))

    return expect(name, problems)


def test_money_repair_flag():
    """计划 §1 #6：RepairAllItems 之后第一笔卖店支出改判 repair，第二笔回 vendor；
    收入不消耗标志；搬钱也不消耗标志。"""
    name = "money_repair_flag"
    problems = []

    lua = money_env()
    boot(lua, 1000)
    fire(lua, "MERCHANT_SHOW")
    lua.execute("RepairAllItems()")
    change_money(lua, 900)
    change_money(lua, 850)
    if calls(lua) != [(-100, "repair"), (-50, "vendor")]:
        problems.append("修理后第一笔应为 repair、第二笔回 vendor，实际 %s" % (calls(lua),))

    # 收入不消耗标志（修理只可能是一笔支出）
    lua = money_env()
    boot(lua, 1000)
    fire(lua, "MERCHANT_SHOW")
    lua.execute("RepairAllItems()")
    change_money(lua, 1060)
    change_money(lua, 1000)
    if calls(lua) != [(60, "vendor"), (-60, "repair")]:
        problems.append("卖店收入不该把修理标志吃掉，实际 %s" % (calls(lua),))

    # 搬钱优先，且不许顺手把修理标志用掉（否则紧随的修费会漏判）
    lua = money_env()
    boot(lua, 1000)
    fire(lua, "MERCHANT_SHOW")
    lua.execute("RepairAllItems()")
    lua.execute("C_Bank.WithdrawMoney(1, 200)")
    change_money(lua, 1200)
    change_money(lua, 1110)
    if calls(lua) != [(200, "warband"), (-90, "repair")]:
        problems.append("搬钱之后修费仍应是 repair，实际 %s" % (calls(lua),))

    return expect(name, problems)


def test_money_ah_mail_attribution():
    """计划 §1 #7 + §6：拍卖行的钱靠 TakeInboxMoney 快照认领，
    金额精确相等**且**发件人等于拍卖行常量才归 ah，其余一律老实留 mail（宁可不归因）；
    连续收取走 FIFO。"""
    name = "money_ah_mail_attribution"
    problems = []
    seller = "拍卖行"

    # ① 金额相等 + 发件人匹配 → ah，meta 带真读到的 sender/subject
    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "出售: 白银护臂", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1500)
    if calls(lua) != [(500, "ah")]:
        problems.append("相等且发件人匹配应归 ah，实际 %s" % (calls(lua),))
    if call_meta(lua, 1, "sender") != seller or call_meta(lua, 1, "subject") != "出售: 白银护臂":
        problems.append("ah 那笔的 meta 没带上真读到的 sender/subject：%r / %r"
                        % (call_meta(lua, 1, "sender"), call_meta(lua, 1, "subject")))
    if session_stats(lua) != (500, 0, 500):
        problems.append("ah 收入要计会话统计，实际 %s" % (session_stats(lua),))

    # ② 金额不等 → 留 mail
    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "出售: 白银护臂", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1450)
    if calls(lua) != [(450, "mail")]:
        problems.append("金额不等必须留 mail（不许假归因），实际 %s" % (calls(lua),))

    # ③ 快照队列空（过期自动到账之类）→ 留 mail
    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    change_money(lua, 1300)
    if calls(lua) != [(300, "mail")]:
        problems.append("队列为空应留 mail，实际 %s" % (calls(lua),))
    if call_meta(lua, 1, "sender") is not None:
        problems.append("没读到的邮件不该编出 sender")

    # ④ 发件人常量拿不到（§6 未验证）→ 留 mail；绝不当"匹配任何发件人"
    lua = money_env()
    set_seller(lua, None)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "出售: 白银护臂", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1500)
    if calls(lua) != [(500, "mail")]:
        problems.append("AUCTION_HOUSE_MAIL_SELLER 为 nil 时必须留 mail，实际 %s" % (calls(lua),))

    # ⑤ 发件人对不上 → 留 mail
    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, "陌生人", "还你点钱", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1500)
    if calls(lua) != [(500, "mail")]:
        problems.append("发件人不是拍卖行必须留 mail，实际 %s" % (calls(lua),))

    # ⑥ "全部收取"：连着两次 TakeInboxMoney，随后两笔 PLAYER_MONEY 按 FIFO 各自认领
    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 7, seller, "出售: 第一件", 100)
    set_inbox(lua, 8, seller, "出售: 第二件", 200)
    lua.execute("TakeInboxMoney(7)")
    lua.execute("TakeInboxMoney(8)")
    change_money(lua, 1100)
    change_money(lua, 1300)
    if calls(lua) != [(100, "ah"), (200, "ah")]:
        problems.append("全部收取应按 FIFO 归两笔 ah，实际 %s" % (calls(lua),))
    if call_meta(lua, 1, "subject") != "出售: 第一件" or call_meta(lua, 2, "subject") != "出售: 第二件":
        problems.append("FIFO 顺序错了：%s / %s"
                        % (call_meta(lua, 1, "subject"), call_meta(lua, 2, "subject")))

    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("回调报错：%s" % bad)
    return expect(name, problems)


def test_money_secret_value_guard():
    """计划 §1 #5：issecretvalue 可能压根没有、也可能对金额返真。
    缺席不许报错；返真的那一笔不比大小、不发流水，但基准照推，
    下一笔真实变动照样量得准；秘密金额不进搬钱队列。"""
    name = "money_secret_value_guard"
    problems = []

    # ① 接口缺席（替身默认就不给）：全程不许报错，正常判归属
    lua = money_env()
    boot(lua, 1000)
    lua.execute("C_Bank.WithdrawMoney(1, 400)")
    change_money(lua, 1400)
    fire(lua, "MERCHANT_SHOW")
    change_money(lua, 1300)
    if calls(lua) != [(400, "warband"), (-100, "vendor")]:
        problems.append("没有 issecretvalue 时归属应照常，实际 %s" % (calls(lua),))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln or "attempt to" in ln.lower()]
    if bad:
        problems.append("issecretvalue 缺席时报错：%s" % bad)

    # ② 秘密总额：不比大小、不发流水，而且基准当场作废（秘密值一旦留在基准里，
    #    下一笔的"总额-基准"会一直抛错、基准再也推不动）⇒ 下一笔只重立基准，
    #    再下一笔才照常量得准。宁可少记一笔，也不记错一笔。
    lua = money_env()
    lua.execute("issecretvalue = function(v) return v == 5000 end")
    boot(lua, 1000)
    change_money(lua, 5000)
    if calls(lua):
        problems.append("秘密值那笔不该发流水（更不许当 0 用）：%s" % (calls(lua),))
    change_money(lua, 6000)
    if calls(lua):
        problems.append("秘密值之后的第一笔只该重立基准，不该拿秘密值做减法：%s" % (calls(lua),))
    change_money(lua, 6500)
    if calls(lua) != [(500, "unknown")]:
        problems.append("重立基准后下一笔真实变动应量得准（+500），实际 %s" % (calls(lua),))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln or "attempt to" in ln.lower()]
    if bad:
        problems.append("issecretvalue 存在时报错：%s" % bad)

    # ③ 秘密金额不排队：否则日后拿它做精确比对会误吃一笔真实收支
    lua = money_env()
    lua.execute("issecretvalue = function(v) return v == 5000 end")
    boot(lua, 1000)
    lua.execute("C_Bank.WithdrawMoney(1, 5000)")
    change_money(lua, 6000)
    if calls(lua) != [(5000, "unknown")]:
        problems.append("秘密额度的搬钱请求不该排队，实际 %s" % (calls(lua),))
    if session_stats(lua) != (5000, 0, 5000):
        problems.append("这笔应算真实收入，实际 %s" % (session_stats(lua),))
    return expect(name, problems)


def test_money_precedence_and_single_emission():
    """计划 §4 T1 判据：优先级 transfer > repair > ah-mail > 上下文 > unknown，
    且每笔 PLAYER_MONEY 只调用账本一次。"""
    name = "money_precedence_and_single_emission"
    lua = money_env()
    boot(lua, 1000)
    # 卖店窗口 + 修理标志 ⇒ repair（第 2 优先级）
    fire(lua, "MERCHANT_SHOW")
    lua.execute("RepairAllItems()")
    change_money(lua, 900)
    # AH 窗口开着 + 排着搬钱请求 ⇒ 搬钱压过上下文（第 1 优先级）
    fire(lua, "AUCTION_HOUSE_SHOW")
    lua.execute("C_Bank.WithdrawMoney(1, 400)")
    change_money(lua, 1300)
    problems = []
    if calls(lua) != [(-100, "repair"), (400, "warband")]:
        problems.append("两笔应分别是 repair / warband，实际 %s" % (calls(lua),))
    if len(calls(lua)) != 2:
        problems.append("两笔 PLAYER_MONEY 应恰好两次写账本，实际 %d 次" % len(calls(lua)))
    if session_stats(lua) != (0, 100, -100):
        problems.append("修费计入支出、搬钱不计收支，实际 %s" % (session_stats(lua),))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("回调报错：%s" % bad)
    return expect(name, problems)


def test_money_probe_command_without_optional_api():
    """`/gcl probe` 是给他日后在真机取证用的：所有可选接口都缺席时不许报错，
    取不到的逐项印"没有"，并能列出最近几笔（上限 5）。"""
    name = "money_probe_command_without_optional_api"
    lua = money_env()
    lua.execute(r"""
        C_Bank = nil issecretvalue = nil GetInboxHeaderInfo = nil TakeInboxMoney = nil
        RepairAllItems = nil WithdrawGuildBankMoney = nil DepositGuildBankMoney = nil
        AUCTION_HOUSE_MAIL_SELLER = nil
    """)
    boot(lua, 1000)
    money = 1000
    steps = [100, 200, 300, 400, 500, 600, 700]      # 7 笔各不相同：probe 只该报最近 5 笔
    for step in steps:
        money += step
        change_money(lua, money)
    lines = slash(lua, "probe")
    problems = []
    if any("出错" in ln for ln in lines):
        problems.append("接口全缺时 probe 报错：%s" % lines)
    joined = "\n".join(lines)
    for want in ("AUCTION_HOUSE_MAIL_SELLER: 没有",
                 "C_Bank.WithdrawMoney: 没有",
                 "issecretvalue: 没有",
                 "GetInboxHeaderInfo: 没有",
                 "WithdrawGuildBankMoney: 没有"):
        if want not in joined:
            problems.append("probe 没印出 %r，实际：%s" % (want, lines))
    moves = [ln for ln in lines if ln.startswith("  ")]
    want_moves = ["  unknown %+d" % step for step in steps[-5:]]
    if moves != want_moves:
        problems.append("最近变动应只报 5 笔且顺序为 %s，实际 %s" % (want_moves, moves))
    # 有接口的时候 probe 要印"有"，并带上常量值
    lua2 = money_env()
    set_seller(lua2, "拍卖行")
    boot(lua2, 1000)
    lines2 = slash(lua2, "probe")
    joined2 = "\n".join(lines2)
    if "AUCTION_HOUSE_MAIL_SELLER: 拍卖行" not in joined2:
        problems.append("常量在时应印出实际值供取证：%s" % lines2)
    if "C_Bank.WithdrawMoney: 有" not in joined2:
        problems.append("C_Bank 在时应印‘有’：%s" % lines2)
    return expect(name, problems)


def test_money_without_ledger_logs_once():
    """账本模块（T2）还没注册时不许报错：只 print 第一条，其余靠内部计数看出规模，
    会话统计照常在内存里累加。"""
    name = "money_without_ledger_logs_once"
    lua = money_env(ledger=False)
    boot(lua, 1000)
    change_money(lua, 1500)
    change_money(lua, 1200)
    lines = printed_lines(lua)
    problems = []
    if any("出错" in ln for ln in lines):
        problems.append("账本缺席时报错了：%s" % lines)
    warned = [ln for ln in lines if "账本" in ln]
    if len(warned) != 1:
        problems.append("账本缺席只该 print 一次，实际 %d 次：%s" % (len(warned), warned))
    if as_number(lua.globals()["GCL"].Money.GetDroppedCount()) != 2:
        problems.append("内部计数应记满 2 笔丢失，实际 %s"
                        % (lua.globals()["GCL"].Money.GetDroppedCount(),))
    if session_stats(lua) != (500, 300, 200):
        problems.append("账本缺席也要累会话统计，实际 %s" % (session_stats(lua),))
    if calls(lua):
        problems.append("假账本没注册却收到了调用？%s" % (calls(lua),))
    return expect(name, problems)


def ep(y, mo, d, h=12, mi=0, s=0):
    """UTC 纪元秒：只用来给替身挑一个绝对时间点（epoch 与时区无关），
    格式化/断言全走替身里的确定性 UTC 算法，不借本地时区。"""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


REALM = "暴龙王卡尔波斯"


def test_ledger_consistency_stores():
    """计划 §5 组 5：约 200 笔跨 3 号跨 6 天 + 若干搬钱后，钉死三处冗余一致——
    daily[d] == Σ byDayChar[d][*] == Σ 当天非转账 entries，且全范围合计 == Σ 天格，
    搬钱绝不进任何总量。每笔 PLAYER_MONEY 恰好一条 entry。"""
    name = "ledger_consistency_stores"
    lua = ledger_env()
    lua.execute(GCL_CONSISTENCY)
    boot(lua, 1000000)
    money = 1000000
    base = ep(2026, 4, 24)          # 远在 400 天窗口内，别被清理顺带剪了
    events = ["MERCHANT_SHOW", "LOOT_OPENED", "TRADE_SHOW", "MAIL_SHOW",
              "AUCTION_HOUSE_SHOW", "QUEST_TURNED_IN"]
    movements = transfers = 0
    for day in range(6):
        set_epoch(lua, base + day * 86400)
        for ci in range(3):
            set_char(lua, "号%d" % (ci + 1))
            for k in range(12):
                fire(lua, events[(day + ci + k) % len(events)])
                delta = (100 + k * 7) if k % 2 == 0 else -(50 + k * 3)
                money += delta
                change_money(lua, money)
                movements += 1
                if (day + ci + k) % 7 == 0:                 # 撒一点搬钱
                    amt = 300 + k
                    if k % 2 == 0:
                        lua.execute("C_Bank.WithdrawMoney(1, %d)" % amt)   # 取出=进钱
                        money += amt
                    else:
                        lua.execute("DepositGuildBankMoney(%d)" % amt)       # 存入=出钱
                        money -= amt
                    change_money(lua, money)
                    transfers += 1
    problems = []
    if transfers == 0:
        problems.append("脚本没造出搬钱，一致性对 transfer 的排除没测到")
    res = str(ledger_eval(lua, "GCL_CONSISTENCY()"))
    if res:
        problems.append("三处冗余/范围合计不一致：%s" % res)
    n_entries = as_number(ledger_eval(lua, "#GCLDB.entries"))
    if n_entries != movements + transfers:
        problems.append("entries 应恰有 %d 条（每笔一条），实际 %d" % (movements + transfers, n_entries))
    if movements < 200:
        problems.append("非转账笔数应铺到 200 左右，实际 %d" % movements)
    n_chars = as_number(ledger_eval(lua, "(function() local c=0 for _ in pairs(GCLDB.chars) do c=c+1 end return c end)()"))
    if n_chars != 3:
        problems.append("应认出 3 个号，实际 %d" % n_chars)
    # GetCharKeys / GetLastSeen：全集 3 个，且最近写过的一次就是墙上钟那一刻
    keys = str(ledger_eval(lua, "table.concat(GCL.Ledger.GetCharKeys(), '|')"))
    if keys.count("|") != 2 or "号1-" not in keys:
        problems.append("GetCharKeys 应列出 3 个号，实际 %r" % keys)
    seen = as_number(ledger_eval(lua, "GCL.Ledger.GetLastSeen('号3-%s')" % REALM))
    if seen != base + 5 * 86400:
        problems.append("GetLastSeen 应为号3最近一次写入 %d，实际 %s" % (base + 5 * 86400, seen))
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("报错：%s" % bad)
    return expect(name, problems)


def test_ledger_transfers_flow_but_not_totals():
    """计划 §5 组 6：warband / guildbank 那两笔进 entries、进流水页且带 transfer 标，
    但不改 daily / byDayChar / 范围合计。"""
    name = "ledger_transfers_flow_but_not_totals"
    lua = ledger_env()
    day = ep(2026, 11, 10)
    boot(lua, 1000)
    set_epoch(lua, day)
    set_char(lua, "阿尔萨斯")
    problems = []
    change_money(lua, 1500)                        # 垫底：+500 unknown 收入
    if as_number(ledger_eval(lua, "GCLDB.daily['2026-11-10'].income")) != 500:
        problems.append("垫底收入没进 daily")
    lua.execute("C_Bank.WithdrawMoney(1, 500)")     # 取出=进钱（搬钱）
    change_money(lua, 2000)
    lua.execute("DepositGuildBankMoney(300)")       # 存入=出钱（搬钱）
    change_money(lua, 1700)
    if as_number(ledger_eval(lua, "GCLDB.daily['2026-11-10'].income")) != 500 or \
       as_number(ledger_eval(lua, "GCLDB.daily['2026-11-10'].expense")) != 0:
        problems.append("搬钱改了 daily")
    if as_number(ledger_eval(lua, "GCLDB.byDayChar['2026-11-10']['阿尔萨斯-%s'].income" % REALM)) != 500:
        problems.append("搬钱改了 byDayChar")
    rt = as_number(ledger_eval(lua, "GCL.Ledger.GetRangeTotals('2026-11-10','2026-11-10').income"))
    if rt != 500:
        problems.append("搬钱改了范围合计：%s" % rt)
    if as_number(ledger_eval(lua, "#GCLDB.entries")) != 3:
        problems.append("三笔（含两笔搬钱）都该进 entries")
    tf = as_number(ledger_eval(lua, "(function() local c=0 for i=1,#GCLDB.entries do if GCLDB.entries[i].transfer then c=c+1 end end return c end)()"))
    if tf != 2:
        problems.append("应有 2 条 transfer 标记，实际 %s" % tf)
    flow = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('阿尔萨斯-%s', '2026-11-10', '2026-11-10', 1, 20)
        local s = {}
        for i=1,#r.rows do s[#s+1] = r.rows[i].src .. (r.rows[i].transfer and '*' or '') end
        return table.concat(s, ',') .. '|' .. tostring(r.bySource.warband and r.bySource.warband.income)
             .. '|' .. tostring(r.bySource.guildbank and r.bySource.guildbank.expense)
    """ % REALM))
    # 新在前：最后写的 guildbank 在最前，unknown 垫底在最后
    if flow != "guildbank*,warband*,unknown|500|300":
        problems.append("流水页/来源小计不符（搬钱要列出并打标记、小计含搬钱桶）：%s" % flow)
    return expect(name, problems)


def test_ledger_day_boundary():
    """计划 §5 组 6：23:59:59 与次日 00:00:00 落不同日期键；跨月、跨年、同月跨日都覆盖，
    并核对替身的 UTC 格式化 == Ledger 的键。"""
    name = "ledger_day_boundary"
    cases = [
        ("同月跨日", ep(2024, 6, 10, 23, 59, 59), ep(2024, 6, 11, 0, 0, 0), "2024-06-10", "2024-06-11", "2024-06", "2024-06"),
        ("闰2月末跨月", ep(2024, 2, 29, 23, 59, 59), ep(2024, 3, 1, 0, 0, 0), "2024-02-29", "2024-03-01", "2024-02", "2024-03"),
        ("跨年", ep(2023, 12, 31, 23, 59, 59), ep(2024, 1, 1, 0, 0, 0), "2023-12-31", "2024-01-01", "2023-12", "2024-01"),
    ]
    problems = []
    for label, e1, e2, d1, d2, m1, m2 in cases:
        lua = ledger_env()
        lua.execute("time = nil")          # 逼 GetServerTime 应答，顺带验候选顺位
        fire(lua, "ADDON_LOADED", ADDON_NAME)
        set_char(lua, "边界")
        set_epoch(lua, e1)
        add_entry(lua, 111, "unknown")
        set_epoch(lua, e2)
        add_entry(lua, 222, "unknown")
        got_d1 = str(ledger_eval(lua, "GCL.Ledger.DateKey(%d)" % e1))
        got_d2 = str(ledger_eval(lua, "GCL.Ledger.DateKey(%d)" % e2))
        got_m1 = str(ledger_eval(lua, "GCL.Ledger.MonthKey(%d)" % e1))
        got_m2 = str(ledger_eval(lua, "GCL.Ledger.MonthKey(%d)" % e2))
        if (got_d1, got_d2, got_m1, got_m2) != (d1, d2, m1, m2):
            problems.append("%s：键应为 (%s,%s,%s,%s)，实际 (%s,%s,%s,%s)"
                            % (label, d1, d2, m1, m2, got_d1, got_d2, got_m1, got_m2))
        if as_number(ledger_eval(lua, "GCL.Ledger.GetDay('%s').income" % d1)) != 111 or \
           as_number(ledger_eval(lua, "GCL.Ledger.GetDay('%s').income" % d2)) != 222:
            problems.append("%s：两笔没落到两个独立天格" % label)
        if d1 == d2:
            problems.append("%s：跨日却没换日期键" % label)
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("报错：%s" % bad)
    return expect(name, problems)


def test_ledger_month_math():
    """计划 §5 组 6：闰年 2 月、平年 2 月、30/31 号月、已知日期星期、空月渲染路径。
    DaysInMonth / WeekdayOfDateKey 是纯数学（不碰事件接口），且星期要和 date('*t') 对齐。"""
    name = "ledger_month_math"
    lua = ledger_env()
    problems = []
    want_days = {(2024, 2): 29, (2025, 2): 28, (1900, 2): 28, (2000, 2): 29, (2100, 2): 28,
                 (2024, 4): 30, (2024, 6): 30, (2024, 12): 31, (2024, 1): 31}
    for (y, m), want in want_days.items():
        got = ledger_eval(lua, "GCL.Ledger.DaysInMonth(%d, %d)" % (y, m))
        if got is None or int(got) != want:
            problems.append("DaysInMonth(%d,%d) 应为 %d，实际 %r" % (y, m, want, got))
    # 已知日期的星期：*t.wday 口径（1=周日..7=周六），用 Python 反查当基准
    for iso in ["2024-06-10", "2023-01-01", "2024-12-25", "2026-11-10", "2025-02-28"]:
        py = datetime.strptime(iso, "%Y-%m-%d").weekday()       # Mon=0..Sun=6
        want_wday = ((py + 1) % 7) + 1                          # 换成 Sun=1..Sat=7
        got = ledger_eval(lua, "GCL.Ledger.WeekdayOfDateKey('%s')" % iso)
        if got is None or int(got) != want_wday:
            problems.append("WeekdayOfDateKey(%s) 应为 %d，实际 %r" % (iso, want_wday, got))
        # 和替身 date('*t') 的 wday 对齐（两套实现必须给同一个星期几）
        secs = ep(int(iso[:4]), int(iso[5:7]), int(iso[8:10]), 12)
        star = ledger_eval(lua, "date('*t', %d).wday" % secs)
        if int(star) != want_wday:
            problems.append("date('*t').wday(%s)=%r 与算法 %d 不符" % (iso, star, want_wday))
    # 空月渲染：只有 DaysInMonth + WeekdayOfDateKey 参与，daily 里没有任何这月的天
    grid_ok = ledger_block(lua, r"""
        local y, mo = 2099, 2
        local n = GCL.Ledger.DaysInMonth(y, mo)
        for d = 1, n do
            local key = string.format("%04d-%02d-%02d", y, mo, d)
            local wd = GCL.Ledger.WeekdayOfDateKey(key)
            if not wd or wd < 1 or wd > 7 then return 'badwday:' .. key end
            local day = GCL.Ledger.GetDay(key)
            if day.income ~= 0 or day.expense ~= 0 then return 'notempty:' .. key end
        end
        return n == 28 and 'ok' or 'days:' .. tostring(n)
    """)
    if str(grid_ok) != "ok":
        problems.append("空月渲染路径异常：%s" % grid_ok)
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("报错：%s" % bad)
    return expect(name, problems)


def test_ledger_cleanup_all_chars():
    """计划 §5 组 7 + §2：过期天从 daily 和 byDayChar 两边都剪；从没上过线的号的老条目也照剪
    （GoldLedger 只清当前号的坑）；entries 上限只丢最老；被引用的 chars 行留住。"""
    name = "ledger_cleanup_all_chars"
    lua = ledger_env()
    now = ep(2026, 11, 10)
    old = now - 500 * 86400          # 超过 400 天
    recent = now - 10 * 86400        # 窗口内
    oldKey = datetime.fromtimestamp(old, timezone.utc).strftime("%Y-%m-%d")
    recentKey = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%d")
    cur = "在场号-%s" % REALM
    ghost = "幽灵号-%s" % REALM
    gone = "路人号-%s" % REALM
    fire(lua, "ADDON_LOADED", ADDON_NAME)                       # 建库
    lua.execute("GCL_OLD = %d GCL_RECENT = %d GCL_OLDKEY = %r GCL_RECENTKEY = %r"
                % (old, recent, oldKey, recentKey))
    lua.execute("GCL_CUR = %r GCL_GHOST = %r GCL_GONE = %r" % (cur, ghost, gone))
    lua.execute(r"""
        local oldKey, recentKey = GCL_OLDKEY, GCL_RECENTKEY
        local cur, ghost, gone = GCL_CUR, GCL_GHOST, GCL_GONE
        GCLDB.daily = {
            [oldKey] = { income = 11, expense = 3 },
            [recentKey] = { income = 9, expense = 0 },
        }
        GCLDB.byDayChar = {
            [oldKey] = { [cur] = { income = 5, expense = 3 }, [ghost] = { income = 6, expense = 0 } },
            [recentKey] = { [cur] = { income = 9, expense = 0 } },
        }
        GCLDB.entries = {
            { t = GCL_OLD, dateKey = oldKey, char = cur, src = 'unknown', amt = 5, transfer = false },
            { t = GCL_OLD, dateKey = oldKey, char = ghost, src = 'unknown', amt = 6, transfer = false },
            { t = GCL_RECENT, dateKey = recentKey, char = cur, src = 'unknown', amt = 9, transfer = false },
        }
        GCLDB.chars = {
            [cur] = { firstSeen = GCL_OLD, lastSeen = GCL_RECENT },
            [ghost] = { firstSeen = GCL_OLD, lastSeen = GCL_OLD },
            [gone] = { firstSeen = GCL_OLD, lastSeen = GCL_OLD },   -- 没被任何条目引用
        }
    """)
    set_epoch(lua, now)
    fire(lua, "ADDON_LOADED", ADDON_NAME)                       # 再初始化一次 ⇒ 跑 Cleanup
    problems = []
    checks = [
        ("GCLDB.daily['%s'] == nil" % oldKey, True, "过期天没从 daily 剪掉"),
        ("GCLDB.byDayChar['%s'] == nil" % oldKey, True, "过期天的 byDayChar 没剪"),
        ("GCLDB.daily['%s'] ~= nil" % recentKey, True, "窗口内的天被误删"),
        ("#GCLDB.entries == 1", True, "entries 应按时间剪掉两笔过期的，留窗口内那笔"),
        ("GCLDB.entries[1].char == %s" % repr(cur), True, "留下的应是窗口内当前号那笔"),
        ("GCLDB.chars[%s] == nil" % repr(ghost), True, "幽灵号只剩过期条目，引用没了就该被清"),
        ("GCLDB.chars[%s] ~= nil" % repr(cur), True, "当前号仍被引用，chars 行要留住"),
        ("GCLDB.chars[%s] == nil" % repr(gone), True, "没被引用的 chars 行要清掉"),
    ]
    for expr, want, why in checks:
        got = lua.eval(expr)
        if got != want:
            problems.append("%s（表达式 %s，期望 %r 实际 %r）" % (why, expr, want, got))
    # 幽灵号的过期条目确实被"全号遍历"清掉了（不是只清当前号）：ghost 一条都不剩
    ghost_left = as_number(ledger_eval(lua, "(function() local c=0 for i=1,#GCLDB.entries do if GCLDB.entries[i].char==%r then c=c+1 end end return c end)()" % ghost))
    if ghost_left != 0:
        problems.append("没上过线的幽灵号的老条目没被清理（%d 条残留）" % ghost_left)
    bad = [ln for ln in printed_lines(lua) if "出错" in ln]
    if bad:
        problems.append("报错：%s" % bad)
    return expect(name, problems)


def test_ledger_entries_cap_drops_oldest():
    """entries 全局上限 20000：超了只从最老（下标 1，追加顺序的最前）开始丢。"""
    name = "ledger_entries_cap_drops_oldest"
    lua = ledger_env()
    now = ep(2026, 11, 10)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    lua.execute(r"""
        GCLDB.entries = {}
        local base = %d - 100000        -- 都在 400 天窗口内，只让上限触发
        for i = 1, 20005 do
            GCLDB.entries[i] = { t = base + i, dateKey = date('%%Y-%%m-%%d', base + i),
                                 char = '甲-%s', src = 'unknown', amt = 1, transfer = false }
        end
        GCLDB.chars = { ['甲-%s'] = { firstSeen = base + 1, lastSeen = base + 20005 } }
    """ % (now, REALM, REALM))
    set_epoch(lua, now)
    fire(lua, "ADDON_LOADED", ADDON_NAME)     # Cleanup ⇒ 丢最老 5 条
    problems = []
    n = as_number(ledger_eval(lua, "#GCLDB.entries"))
    if n != 20000:
        problems.append("上限应砍到 20000，实际 %s" % n)
    first_t = as_number(ledger_eval(lua, "GCLDB.entries[1].t"))
    last_t = as_number(ledger_eval(lua, "GCLDB.entries[20000].t"))
    if first_t != (now - 100000 + 6):        # 最老的 5 条（base+1..base+5）被丢
        problems.append("丢的应是最老的 5 条：首条 t 应为 %d，实际 %d" % (now - 100000 + 6, first_t))
    if last_t != (now - 100000 + 20005):
        problems.append("新条目不许被丢：末条 t 应为 %d，实际 %d" % (now - 100000 + 20005, last_t))
    if as_number(ledger_eval(lua, "GCLDB.chars['甲-%s'] and 1 or 0" % REALM)) != 1:
        problems.append("甲仍被引用，chars 行要留住")
    return expect(name, problems)


def test_ledger_char_key_unreadable_drops_write():
    """计划 §2 / 任务 #4：UnitName 或 GetRealmName 任一读不出来 ⇒ 这笔不写、计数、
    只提醒一次，且绝不凭空造出角色键/日格。"""
    name = "ledger_char_key_unreadable_drops_write"
    lua = ledger_env()
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    set_epoch(lua, ep(2026, 11, 10))
    problems = []
    # ① realm 读不出来
    set_char(lua, "有号", None)
    add_entry(lua, 500, "unknown")
    add_entry(lua, 300, "unknown")
    add_entry(lua, 300, "unknown")
    warned = len([ln for ln in printed_lines(lua) if "没写进账本" in ln])
    if warned != 1:
        problems.append("号名缺失只该 print 一次，实际 %d 次" % warned)
    if as_number(ledger_eval(lua, "GCL.Ledger.GetDroppedWrites()")) != 3:
        problems.append("三次缺失应计三次丢弃，实际 %s" % as_number(ledger_eval(lua, "GCL.Ledger.GetDroppedWrites()")))
    for probe in ("#GCLDB.entries",
                  "(function() local c=0 for _ in pairs(GCLDB.chars) do c=c+1 end return c end)()",
                  "(function() local c=0 for _ in pairs(GCLDB.daily) do c=c+1 end return c end)()",
                  "(function() local c=0 for _ in pairs(GCLDB.byDayChar) do c=c+1 end return c end)()"):
        if as_number(ledger_eval(lua, probe)) != 0:
            problems.append("realm 缺失时 %s 不该有内容" % probe)
    # ② name 读不出来（realm 正常）
    set_char(lua, None, REALM)
    add_entry(lua, 100, "unknown")
    if as_number(ledger_eval(lua, "GCL.Ledger.GetDroppedWrites()")) != 4:
        problems.append("name 缺失也该计入丢弃，累计应为 4")
    if as_number(ledger_eval(lua, "#GCLDB.entries")) != 0:
        problems.append("name 缺失却写了 entries")
    # ③ 恢复正常：能写成一笔
    set_char(lua, "终于好了")
    add_entry(lua, 42, "loot")
    if as_number(ledger_eval(lua, "#GCLDB.entries")) != 1:
        problems.append("号名可读后应正常写入一笔")
    if str(ledger_eval(lua, "GCLDB.entries[1].char")) != "终于好了-%s" % REALM:
        problems.append("写进去的角色键不对")
    return expect(name, problems)


def test_ledger_get_char_entries_paging():
    """GetCharEntries：新在前、末页不满时 totalPages 正确、来源小计覆盖整段且与列出的行对得上、
    搬钱在流水里列出并打标、范围外的条目排除。"""
    name = "ledger_get_char_entries_paging"
    lua = ledger_env()
    day = ep(2026, 11, 10)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    set_char(lua, "翻页号")
    # 乱序写入（靠 t 排序）：t 依次 0,20,10,50,30，外加次日一笔
    set_epoch(lua, day + 0);  add_entry(lua, 100, "vendor")
    set_epoch(lua, day + 20); add_entry(lua, 200, "ah", "{ subject = '出售: 甲', sender = '拍卖行' }")
    set_epoch(lua, day + 10); add_entry(lua, -40, "repair")
    set_epoch(lua, day + 50); add_entry(lua, 300, "warband")     # 搬钱（源名触发）
    set_epoch(lua, day + 30); add_entry(lua, -70, "loot")
    set_epoch(lua, day + 86400); add_entry(lua, 999, "loot")     # 次日：应被单日范围排除
    charkey = "翻页号-%s" % REALM
    problems = []
    meta = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 1, 2)
        return r.total .. '/' .. r.totalPages .. '/' .. #r.rows .. '/' .. r.rows[1].src .. '/'
             .. tostring(r.rows[1].transfer) .. '/' .. r.rows[2].src
    """ % charkey))
    if meta != "5/3/2/warband/true/loot":
        problems.append("首页应 5 笔/3 页/每页2/新在前[warband(transfer),loot]，实际 %s" % meta)
    p3 = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 3, 2)
        return #r.rows .. '/' .. r.rows[1].src
    """ % charkey))
    if p3 != "1/vendor":
        problems.append("末页(第3页)应只剩 1 行 vendor，实际 %s" % p3)
    subject = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 2, 2)
        return tostring(r.rows[1].subject)
    """ % charkey))
    if subject != "出售: 甲":
        problems.append("第二页首行(ah)应带真读到的邮件标题，实际 %s" % subject)
    sub = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 1, 100)
        local bs = r.bySource
        local sumInc, sumExp = 0, 0
        for _, b in pairs(bs) do sumInc = sumInc + b.income; sumExp = sumExp + b.expense end
        local rowInc, rowExp = 0, 0
        for i=1,#r.rows do local a=r.rows[i].amt if a>0 then rowInc=rowInc+a else rowExp=rowExp-a end end
        return (bs.vendor.income)..','..(bs.ah.income)..','..(bs.repair.expense)..','
             ..(bs.warband.income)..','..(bs.loot.expense)..','
             ..((sumInc==rowInc) and ((sumExp==rowExp) and 'eq' or 'exp') or 'inc')
    """ % charkey))
    if sub != "100,200,40,300,70,eq":
        problems.append("来源小计/与小计对行不符（期望 100,200,40,300,70,eq）：%s" % sub)
    rng = str(ledger_block(lua, r"""
        local r = GCL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-11', 1, 100)
        return r.total
    """ % charkey))
    if rng != "6":
        problems.append("放宽到次日应含 6 笔，实际 %s" % rng)
    return expect(name, problems)


def test_ledger_wallclock_sources_and_command():
    """/gcl ledger 取证 + 墙上钟候选顺位：都在时 time 优先应答；time 值不可信(<1e9)换
    GetServerTime；只剩 GetLocalTime 用它；三候选全缺也不报错、明说没有可用墙上钟。"""
    name = "ledger_wallclock_sources_and_command"
    problems = []
    lua = ledger_env()
    fire(lua, "ADDON_LOADED", ADDON_NAME)      # 三候选都在时正常建库

    def run():
        lines = slash(lua, "ledger")
        return lines, "\n".join(lines)

    # ① 三候选都在：time 优先
    if str(ledger_eval(lua, "type(time)")) != "function":
        problems.append("默认替身应提供 time 作为墙上钟候选")
    lines, joined = run()
    if any("出错" in ln for ln in lines):
        problems.append("全有时 /gcl ledger 报错：%s" % lines)
    if "墙上时钟来源 time" not in joined:
        problems.append("time 在时应优先用它：%s" % lines)

    # ② time 回个不像钟的数（<1e9）：跳到 GetServerTime
    lua.execute("time = function() return 5 end")
    lines, joined = run()
    if "墙上时钟来源 GetServerTime" not in joined:
        problems.append("time 值不可信时应换 GetServerTime：%s" % lines)

    # ③ 只剩 GetLocalTime
    lua.execute("time = nil GetServerTime = nil")     # GetLocalTime 仍默认在
    lines, joined = run()
    if any("出错" in ln for ln in lines):
        problems.append("只 GetLocalTime 时报错：%s" % lines)
    if "GetLocalTime" not in joined or "没有可用的墙上时钟" in joined:
        problems.append("应答来源应是 GetLocalTime：%s" % lines)

    # ④ 三候选全缺：不报错，明说没有可用墙上钟
    set_clock_none(lua)
    lines, joined = run()
    if any("出错" in ln for ln in lines):
        problems.append("无墙上钟时 /gcl ledger 报错：%s" % lines)
    if "没有可用的墙上时钟" not in joined:
        problems.append("无墙上钟时应报'没有可用的墙上时钟'：%s" % lines)
    return expect(name, problems)


def test_every_defined_case_is_registered_in_the_runner():
    """静态守卫（原样继承 GQL 第 36 轮抓出来的洞）：`def test_*` 定义了但没写进
    下面人手维护的 `tests = [...]` 清单 => 这条一行都不跑且一句错都不报。
    只认清单那一段：docstring 里出现用例名不算注册。"""
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    block = re.search(r"^    tests = \[\n(.*?)^    \]\n", src, re.M | re.S)
    if not block:
        return fail("every_defined_case_is_registered_in_the_runner",
                    "找不到 __main__ 里那份 tests = [...] 清单（清单形状变了，守卫跟着改）")
    listed = set(re.findall(r"\b(test_\w+),", block.group(1)))
    defined = set(re.findall(r"^def (test_\w+)", src, re.M))
    orphans = sorted(defined - listed)
    ghost = sorted(n for n in listed if n not in defined)
    if orphans or ghost:
        return fail("every_defined_case_is_registered_in_the_runner",
                    "定义了但清单里没跑（一条都不会执行）：%s\n清单里有但没定义（跑它会 NameError）：%s"
                    % ("、".join(orphans) or "无", "、".join(ghost) or "无"))
    ok("every_defined_case_is_registered_in_the_runner")


if __name__ == "__main__":
    tests = [
        test_toc_lists_only_existing_files_and_covers_every_lua,
        test_version_matches_toc,
        test_slash_command_registered,
        test_module_registry_and_lifecycle,
        test_savedvariables_defaults_merge_without_overwrite,
        test_dump_and_version_commands_do_not_error,
        test_money_baseline_and_delta_rules,
        test_money_context_events_map_and_clear,
        test_money_transfer_queue_rules,
        test_money_repair_flag,
        test_money_ah_mail_attribution,
        test_money_secret_value_guard,
        test_money_precedence_and_single_emission,
        test_money_probe_command_without_optional_api,
        test_money_without_ledger_logs_once,
        test_ledger_consistency_stores,
        test_ledger_transfers_flow_but_not_totals,
        test_ledger_day_boundary,
        test_ledger_month_math,
        test_ledger_cleanup_all_chars,
        test_ledger_entries_cap_drops_oldest,
        test_ledger_char_key_unreadable_drops_write,
        test_ledger_get_char_entries_paging,
        test_ledger_wallclock_sources_and_command,
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:      # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    print(f"\n{len(tests) - len(FAILED)}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
