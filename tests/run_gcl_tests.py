# GoldDailyLedger 离线单测（T0 起跑）：toc 静态守卫 + Core 骨架行为。
# 用法：python tests/run_gcl_tests.py   —— 全绿退出码 0，有 FAIL 退出码 1。
# 替身规则原样继承 GQL 的三条教训：只给 Core 真碰到的入口、不许凭空造客户端没有的
# 方法（取用即抛错）、生命周期用例必须走真实事件入口而不是直接调内部函数。
import os
import re
import sys
from calendar import monthrange
from datetime import datetime, timezone

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "GoldDailyLedger")
TOC_PATH = os.path.join(PLUGIN_DIR, "GoldDailyLedger.toc")
ADDON_NAME = "GoldDailyLedger"
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
#     time 支持无参（返回注入的 GDL_EPOCH）与带日期表（GDL_DaysFromCivil 换算）两种签名，
#     这正是标准 os.time 的契约、WoW 的 time 复刻它。用例可按候选逐个置 nil 来验优先级/缺席。
#   date —— GoldLedger/Data.lua:55,69,272,487 就靠它写 %Y-%m-%d / %Y-%m / *t 的键，
#     在他 12.1.5 真机记账跑通即视为存在。这里按 GDL_EPOCH 做**确定性 UTC 拆分**
#     （GDL_CivilFromDays 的日历算法），绝不走 Python/os 的本地时区 —— 否则本机能过、
#     别的机器语义就变，用例等于没测。支持 "%Y-%m-%d"、"%Y-%m"、"*t"。
#   UnitName / GetRealmName —— GoldLedger/Data.lua:47-48 拼 "Name-Realm" 用的就是这两个
#     全局（GQL 套件同样用它们做角色键）；默认给非空串，用例可置 nil 验"读不出来不写假号"。
# 帧方法仍旧只给 Core 用到的那几个；下面这些替身都不新建帧，只补全局函数与表方法。
GDL_MOCK_ENV = r"""
UIParent = {}
SlashCmdList = {}
SLASH_GDL1 = nil
GDL_NOW = 0
GDL_OUTPUT = {}
GDL_FRAMES = {}
GDL_MONEY = 0
-- 战团银行金币夹具：nil = 这台客户端/这个号还没"访问过"战团银行，读不出来。
-- 依据：C_Bank.FetchDepositedMoney(Enum.BankType.Account) 是 11.x 起官方接口，
-- 未打开过战团银行时可能返回 nil（Blizzard 论坛 "How do I get the gold in a warband bank"
-- 的示例就是 RegisterEvent("ACCOUNT_MONEY") + FetchDepositedMoney(Enum.BankType.Account)）。
GDL_WARBAND_GOLD = nil
-- 墙上时钟注入点 + 角色键注入点（默认值是 2026-11-10 12:00 UTC，用例随时改）。
GDL_EPOCH = 1794312000
GDL_UNIT_NAME = "冒险者"
GDL_REALM_NAME = "暴龙王卡尔波斯"
-- 只列 Core.lua / Money.lua 注册的事件；真客户端对未知事件名是直接抛错
-- （12.1.5 实测 "Attempt to register unknown event"），替身照做。
GDL_KNOWN_EVENTS = {
    ADDON_LOADED = true, PLAYER_LOGIN = true,
    PLAYER_MONEY = true,
    MERCHANT_SHOW = true, MERCHANT_CLOSED = true,
    AUCTION_HOUSE_SHOW = true, AUCTION_HOUSE_CLOSED = true,
    MAIL_SHOW = true, MAIL_CLOSED = true,
    TRADE_SHOW = true, TRADE_CLOSED = true,
    LOOT_OPENED = true, LOOT_CLOSED = true,
    QUEST_TURNED_IN = true,
    -- 战团银行金币：ACCOUNT_MONEY 见 Blizzard 论坛官方示例（同上）；
    -- BANKFRAME_OPENED 是银行界面打开事件，用来补"访问过战团银行"后的第一次读数。
    ACCOUNT_MONEY = true, BANKFRAME_OPENED = true,
}
function GetTime() return GDL_NOW or 0 end
function GetMoney() return GDL_MONEY end
-- 确定性 UTC 日历算法（Howard Hinnant civil<->days）：不借 Python 的本地时区，
-- 否则这台机跑绿、那台机语义就变了，等于没测。date/time 全靠它把注入的纪元秒拆成键。
local function GDL_DaysFromCivil(y, m, d)
    y = y - ((m <= 2) and 1 or 0)
    local era = math.floor((y >= 0 and y or y - 399) / 400)
    local yoe = y - era * 400
    local mp = m + ((m > 2) and -3 or 9)
    local doy = math.floor((153 * mp + 2) / 5) + d - 1
    local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
    return era * 146097 + doe - 719468
end
local function GDL_CivilFromDays(z)
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
        local days = GDL_DaysFromCivil(tonumber(t.year) or 1970, tonumber(t.month) or 1, tonumber(t.day) or 1)
        return days * 86400 + (tonumber(t.hour) or 12) * 3600 + (tonumber(t.min) or 0) * 60 + (tonumber(t.sec) or 0)
    end
    return GDL_EPOCH or 0
end
function GetServerTime() return GDL_EPOCH or 0 end
function GetLocalTime() return GDL_EPOCH or 0 end
function date(fmt, t)
    fmt = fmt or "%Y-%m-%d"
    local sec = (type(t) == "number") and t or (GDL_EPOCH or 0)
    if type(sec) ~= "number" then return nil end
    local days = math.floor(sec / 86400)
    local rem = sec - days * 86400
    if rem < 0 then rem = rem + 86400 ; days = days - 1 end
    local hh = math.floor(rem / 3600)
    local mi = math.floor((rem % 3600) / 60)
    local ss = rem % 60
    local y, mo, d = GDL_CivilFromDays(days)
    local wday = ((days + 4) % 7) + 1     -- 1=周日（同 Lua *t.wday）；1970-01-01 days=0 = 周四
    if fmt == "*t" then
        return { year = y, month = mo, day = d, hour = hh, min = mi, sec = ss, wday = wday,
                 yday = (days - GDL_DaysFromCivil(y, 1, 1)) + 1 }
    end
    if fmt == "%Y-%m-%d" then return string.format("%04d-%02d-%02d", y, mo, d) end
    if fmt == "%Y-%m" then return string.format("%04d-%02d", y, mo) end
    return nil    -- 名单外的格式返回 nil：Ledger 的 safeDate 会当"读不出来"，绝不拿它当键
end
function UnitName(unit)
    return (type(GDL_UNIT_NAME) == "string" and GDL_UNIT_NAME ~= "") and GDL_UNIT_NAME or nil
end
function GetRealmName()
    return (type(GDL_REALM_NAME) == "string" and GDL_REALM_NAME ~= "") and GDL_REALM_NAME or nil
end
-- 职业色取证：UnitClass 第二返回值是职业 token（GoldQuestLedger/Sync.lua:1178-1181
-- 在用户 12.1.5 客户端跑过的取法），颜色表形态照 Matrix.lua:145-162。
-- 只复刻 .r/.g/.b 与 .colorStr（那张表**没有** .colorHex），别给替身发明字段。
GDL_UNIT_CLASS = "MAGE"
RAID_CLASS_COLORS = {
    MAGE = { r = 0.25, g = 0.78, b = 0.92, colorStr = "ff40c7eb" },
    PALADIN = { r = 0.96, g = 0.55, b = 0.73, colorStr = "fff58cba" },
    WARRIOR = { r = 0.78, g = 0.61, b = 0.43, colorStr = "ffc79c6e" },
    ROGUE = { r = 1.00, g = 0.96, b = 0.41, colorStr = "fffff569" },
    PRIEST = { r = 1.00, g = 1.00, b = 1.00, colorStr = "ffffffff" },
    HUNTER = { r = 0.67, g = 0.83, b = 0.45, colorStr = "ffabd473" },
    WARLOCK = { r = 0.58, g = 0.51, b = 0.79, colorStr = "ff9482c9" },
    SHAMAN = { r = 0.00, g = 0.44, b = 0.87, colorStr = "ff0070de" },
    MONK = { r = 0.00, g = 1.00, b = 0.59, colorStr = "ff00ff96" },
    DRUID = { r = 1.00, g = 0.49, b = 0.04, colorStr = "ffff7d0a" },
    DEATHKNIGHT = { r = 0.77, g = 0.12, b = 0.23, colorStr = "ffc41f3b" },
    DEMONHUNTER = { r = 0.64, g = 0.19, b = 0.79, colorStr = "ffa330c9" },
    EVOKER = { r = 0.20, g = 0.58, b = 0.50, colorStr = "ff33937f" },
}
function UnitClass(unit)
    if unit == "player" and type(GDL_UNIT_CLASS) == "string" and GDL_UNIT_CLASS ~= "" then
        return "职业", GDL_UNIT_CLASS
    end
    return nil, nil
end
function print(...)
    for i = 1, select("#", ...) do
        GDL_OUTPUT[#GDL_OUTPUT + 1] = tostring((select(i, ...)))
    end
    return ...
end
-- 后置钩子的替身：按名字记下回调，"原生函数"被调用时才跑（客户端语义）。
-- 全局函数与表方法两种签名都要支持：hooksecurefunc("Name", fn) /
-- hooksecurefunc(C_Bank, "WithdrawMoney", fn)。
GDL_GLOBAL_HOOKS = {}
GDL_TABLE_HOOKS = {}
local function AppendHook(bucket, key, fn)
    local list = bucket[key]
    if not list then list = {} ; bucket[key] = list end
    list[#list + 1] = fn
    return fn
end
function hooksecurefunc(a, b, c)
    if type(a) == "string" then
        if type(b) ~= "function" then error("Usage: hooksecurefunc(name, func)") end
        return AppendHook(GDL_GLOBAL_HOOKS, a, b)
    end
    if type(a) ~= "table" or type(b) ~= "string" or type(c) ~= "function" then
        error("Usage: hooksecurefunc(table, key, func)")
    end
    local t = GDL_TABLE_HOOKS[a]
    if not t then t = {} ; GDL_TABLE_HOOKS[a] = t end
    return AppendHook(t, b, c)
end
function GDL_RunGlobal(name, ...)
    local list = GDL_GLOBAL_HOOKS[name]
    if list then for i = 1, #list do list[i](...) end end
end
function GDL_RunMethod(tbl, name, ...)
    local t = GDL_TABLE_HOOKS[tbl]
    local list = t and t[name]
    if list then for i = 1, #list do list[i](...) end end
end
-- 原生函数替身：函数体是空的（我们只关心后置钩子被叫醒），调用签名照客户端。
RepairAllItems = function(p1, p2, p3) return GDL_RunGlobal("RepairAllItems", p1, p2, p3) end
TakeInboxMoney = function(index) return GDL_RunGlobal("TakeInboxMoney", index) end
WithdrawGuildBankMoney = function(amount) return GDL_RunGlobal("WithdrawGuildBankMoney", amount) end
DepositGuildBankMoney = function(amount) return GDL_RunGlobal("DepositGuildBankMoney", amount) end
C_Bank = {
    WithdrawMoney = function(bankID, amount) return GDL_RunMethod(C_Bank, "WithdrawMoney", bankID, amount) end,
    DepositMoney = function(bankID, amount) return GDL_RunMethod(C_Bank, "DepositMoney", bankID, amount) end,
    -- 战团银行余额：账号级接口，未访问过时返回 nil（见 GDL_WARBAND_GOLD 注释）。
    FetchDepositedMoney = function(bankID)
        if GDL_WARBAND_GOLD == nil then return nil end
        if Enum and Enum.BankType and bankID == Enum.BankType.Account then return GDL_WARBAND_GOLD end
        return nil
    end,
}
Enum = { BankType = { Character = 1, Guild = 2, Account = 3 } }
-- 时光徽章（warcraft.wiki.gg C_WowTokenPublic）：BuyToken() 无参；GetCurrentMarketPrice()
-- 回当前市价（铜）；GetItemInfo(WOW_TOKEN_ITEM_ID) 取本地化物品名，暴雪自己的
-- BrowseWowTokenResults_Update 就是这么取名的。GDL_TOKEN_NAME = nil 用来验"名字读不到不猜"。
GDL_TOKEN_PRICE = 6800000000      -- 时光徽章市价（铜）：68 万金
GDL_TOKEN_NAME = "时光徽章"
WOW_TOKEN_ITEM_ID = 122284
C_WowTokenPublic = {
    BuyToken = function() return GDL_RunMethod(C_WowTokenPublic, "BuyToken") end,
    GetCurrentMarketPrice = function() return GDL_TOKEN_PRICE end,
}
function GetItemInfo(itemID)
    if GDL_TOKEN_NAME ~= nil and itemID == WOW_TOKEN_ITEM_ID then return GDL_TOKEN_NAME end
    return nil
end
-- 收件箱夹具：用例往里塞 {sender, subject, money}，GetInboxHeaderInfo 按客户端
-- 的多返回值形状回（记账只取第 3、4、5 个）。没有这一条就无法复现
-- "TakeInboxMoney 当场抓快照" 这条实测做法（Tracker.lua:275-285）。
GDL_INBOX = {}
function GetInboxHeaderInfo(index)
    local m = GDL_INBOX[index]
    if type(m) ~= "table" then return nil end
    return nil, nil, m.sender, m.subject, m.money
end
-- 确定性估宽（ASCII 6px / 其它按 UTF-8 码点 12px）：客户端按字体排版回真像素，离线没有
-- 排版器。这个估宽只用来让"文字盖过列位""提示别宽过窗口"这类用例算得成账，
-- **不代表客户端实测值** —— 真值必须由用户跑 /gdl probe ui 量回来。
local function GDL_EstimateRunes(s)
    local n, i = 0, 1
    s = s or ""
    while i <= #s do
        local b = s:byte(i)
        if b < 0x80 then n = n + 6 ; i = i + 1
        elseif b < 0xE0 then n = n + 12 ; i = i + 2
        elseif b < 0xF0 then n = n + 12 ; i = i + 3
        else n = n + 12 ; i = i + 4 end
    end
    return n
end
--- |T路径:高:宽|t 在客户端画出来是一个图标（不是那串字），按转义里写的**宽**计。
--- 只有"高"没写"宽"的两段式（|T路径:12|t）客户端不认，会把整串原样印成文本
--- （Config.lua:66-73 记的用户截图那一幕）⇒ 这里就不给它图标宽、按普通字符算，
--- 于是格式化函数哪天退回两段式，宽度用例当场把它顶出来。
local function GDL_EstimateWidth(s)
    s = s or ""
    local total, pos = 0, 1
    while true do
        local a = s:find("|T", pos, true)
        if not a then return total + GDL_EstimateRunes(s:sub(pos)) end
        total = total + GDL_EstimateRunes(s:sub(pos, a - 1))
        local e = s:find("|t", a + 2, true)
        if not e then return total + GDL_EstimateRunes(s:sub(a)) end
        local w = s:sub(a, e - 1):match(":%d+:(%d+)$")
        if w then
            total = total + tonumber(w)
        else
            total = total + GDL_EstimateRunes(s:sub(a, e + 1))
        end
        pos = e + 2
    end
end
function CreateFrame(frameType, name, parent, template)
    local f = { __events = {}, __scripts = {}, __shown = false, __type = frameType,
                __parent = parent, __children = {}, __template = template }
    f.SetScript = function(self, k, fn) self.__scripts[k] = fn end
    f.GetScript = function(self, k) return self.__scripts[k] end
    f.IsShown = function(self) return self.__shown end
    f.Show = function(self) self.__shown = true end
    f.Hide = function(self) self.__shown = false end
    f.RegisterEvent = function(self, e)
        if GDL_KNOWN_EVENTS[e] ~= true then
            error(('Attempt to register unknown event "%s"'):format(tostring(e)))
        end
        self.__events[e] = true
    end
    -- ==== T3 界面替身：下面每一条都指得出"本仓库哪个生产文件在用户 12.1.5 客户端上
    -- 已经这样调过"（行号在下面），指不出的不进名单。纪律和上面一样：
    -- 名单外的方法取用即抛错 —— 替身凭空造一个方法，生产代码照着写，客户端没有，
    -- 就是上一轮矩阵"离线全绿、开机一篇漆黑"的根因。
    -- 记点数而不是只记"调过"：版式用例要查锚点参数（同一控件反复换锚是本项目踩过的坑）。
    f.SetPoint = function(self, a, b, c, d, e)              -- Matrix.lua:1550
        self.__points = self.__points or {}
        self.__points[#self.__points + 1] = { a, b, c, d, e }
    end
    f.ClearAllPoints = function(self) self.__points = nil end -- Matrix.lua:367
    f.SetSize = function(self, w, h) self.__w, self.__h = w, h end   -- Matrix.lua:1549
    f.SetWidth = function(self, w) self.__w = w end          -- LogWindow.lua:100
    f.SetHeight = function(self, h) self.__h = h end         -- Matrix.lua:1702
    f.GetWidth = function(self) return self.__w or 0 end     -- Matrix.lua:1251
    f.GetHeight = function(self) return self.__h or 0 end    -- Matrix.lua:1252
    f.GetCenter = function(self) return self.__cx or 0, self.__cy or 0 end -- Matrix.lua:956
    f.GetEffectiveScale = function(self) return self.__scale or 1 end      -- Matrix.lua:917
    f.SetFrameStrata = function(self, s) self.__strata = s end              -- Matrix.lua:1560
    f.GetFrameStrata = function(self) return self.__strata end              -- Probe.lua:3072
    f.SetFrameLevel = function(self, l) self.__framelevel = l end           -- Options.lua:689
    -- 模板自带的层号只复刻查得到出处的那一条：UIPanelCloseButton 的 frameLevel=510
    -- （Matrix.lua:1600-1605 记的用户 2026-11-09 截图那条）。别的模板没有证据 ⇒ 不给层号。
    f.GetFrameLevel = function(self) return self.__framelevel or 1 end      -- Options.lua:689
    f.SetToplevel = function(self, v) self.__toplevel = v end               -- AHSnipe/SnipeList 同款：原生点击置顶
    f.Raise = function(self) self.__framelevel = 1000 end                   -- 同 strata 里抬到最上（toplevel 的客户端行为替身）
    f.Lower = function(self) self.__framelevel = 1 end
    f.SetMovable = function(self, v) self.__movable = v end                 -- Matrix.lua:1571
    f.SetClampedToScreen = function(self, v) self.__clamped = v end          -- Options.lua:662
    f.EnableMouse = function(self, v) self.__mouse = v end                   -- Matrix.lua:1572
    f.RegisterForDrag = function(self, b) self.__drag = b end                -- Matrix.lua:1573
    -- 滚动三件套（流水弹窗 T8 / 右栏 T7）：出处都是用户 12.1.5 真机跑过的 AHSnipe 列表。
    -- 只记"被叫过/叫了什么"：偏移与范围由生产代码自己算（GetVerticalScroll 返回形态未实证）。
    f.EnableMouseWheel = function(self, v) self.__wheel = v end              -- SnipeList.lua:356
    f.SetScrollChild = function(self, c) self.__scrollchild = c end          -- SnipeList.lua:354
    f.SetVerticalScroll = function(self, y) self.__scrolly = y end           -- SnipeList.lua:360
    -- 拖动只记"被叫过"：客户端的 StartMoving 之后必须有 StopMovingOrSizing，
    -- 这两个成对出现是窗口能拖动而不"粘住"的全部条件（Matrix.lua:1574-1575）。
    f.StartMoving = function(self) self.__moving = true end                  -- Matrix.lua:1574
    f.StopMovingOrSizing = function(self) self.__moving = false end          -- Matrix.lua:1575
    f.SetBackdrop = function(self, b) self.__backdrop = b end                -- Matrix.lua:1576
    f.GetBackdrop = function(self) return self.__backdrop end                -- Matrix.lua:888
    f.SetBackdropColor = function(self, r, g, b, a)                          -- Matrix.lua:1585
        self.__bdcolor = { r, g, b, a }
    end
    f.SetBackdropBorderColor = function(self, r, g, b, a) self.__bdedge = { r, g, b, a } end
    f.RegisterForClicks = function(self, ...)                                -- Matrix.lua:364
        self.__clicks = {}
        for i = 1, select("#", ...) do self.__clicks[i] = tostring(select(i, ...)) end
    end
    f.CreateFontString = function(self, _name, _layer, font)                  -- Options.lua:261
        local c = CreateFrame("FontString", nil, self)
        -- 建串时传的字体对象名记下来：/gdl probe ui 要报"这条字用的是哪个字体"，
        -- 真客户端第 3 参就是它（Matrix.lua:1587 同一条）。
        c.__font = font
        return c
    end
    f.CreateTexture = function(self, _name, _layer)                           -- Matrix.lua:1698
        return CreateFrame("Texture", nil, self)
    end
    f.SetText = function(self, t) self.__text = tostring(t) end               -- Matrix.lua:1595
    f.GetText = function(self) return self.__text or "" end                   -- LogWindow.lua:137
    f.SetJustifyH = function(self, h) self.__justifyH = h end                 -- Options.lua:263
    f.SetTextColor = function(self, r, g, b, a) self.__color = { r, g, b, a } end -- Options.lua:274
    f.SetAlpha = function(self, a) self.__alpha = a end                       -- LogWindow.lua:101
    f.SetColorTexture = function(self, r, g, b, a) self.__color = { r, g, b, a } end -- Matrix.lua:1699
    -- 按钮贴图：小地图按钮（T4）用 SetNormalTexture/SetHighlightTexture 各挂一张。
    -- 存在性出处 = GoldQuestLedger/Matrix.lua:2447 与 2449（用户 12.1.5 真机上那颗按钮
    -- 就是这么挂的）；这里**记下路径串本身**，因为"toc 声明的图标"与"按钮用的图标"
    -- 是两处、改一处漏一处就会两个地方两颗图（同仓库 2026-11-10 真踩过）。
    f.SetNormalTexture = function(self, t) self.__normal = t end
    f.SetHighlightTexture = function(self, t) self.__highlight = t end
    -- EditBox 专用方法：反馈网址弹窗要"聚焦 + 全选"才按得动 Ctrl+C。
    -- 出处 = GoldLedger/Modules/Export/Frame.lua:40-72 与 Goal/Frame.lua:105-106
    -- （同仓库生产代码，已在用户 12.1.5 客户端上跑过）。
    f.SetAutoFocus = function(self, v) self.__autofocus = v end
    f.SetFocus = function(self) self.__focused = true end
    f.ClearFocus = function(self) self.__focused = false end
    f.HighlightText = function(self) self.__highlighted = true end
    -- 量宽：真客户端按字体排版回像素，离线没有排版器。这里回**确定性**的估宽
    -- （GDL_EstimateWidth，存在性出处 Options.lua:324），只用来让"文字盖过列位"这类
    -- 用例能算账，不代表客户端实测值 —— 真值必须由用户跑 /gdl probe ui 量。
    f.GetStringWidth = function(self) return GDL_EstimateWidth(self.__text) end
    f.Enable = function(self) self.__enabled = true end                       -- Options.lua:281
    f.Disable = function(self) self.__enabled = false end                     -- Options.lua:281
    f.IsEnabled = function(self) return self.__enabled ~= false end           -- Options.lua:281 同一对
    if template == "UIPanelCloseButton" then
        -- 暴雪模板自带 frameLevel=510（FrameXML SharedUIPanelTemplates.xml，Matrix.lua:1600-1605
        -- 记的就是用户截图里"× 浮在别人窗子上面"那一幕）。不复刻这条，离线永远演不出
        -- 来，也就永远验不到"生产代码把它钉回主帧+1"这一句到底生没生效。
        f.__framelevel = 510
    end
    -- 取用名单外的任何方法名立刻抛错：上一轮矩阵静默炸的根因就是生产代码照着
    -- 替身里凭空造的方法写（"替身不许发明客户端没有的方法"）。
    -- 双下划线开头的是替身自己的记账字段（客户端没有这种名字的方法），读它们回 nil；
    -- 名单外的**方法名**照旧抛错，这条纪律不打折。
    setmetatable(f, { __index = function(_, k)
        if type(k) == "string" and k:sub(1, 2) == "__" then return nil end
        error("GDL mock frame: 没有这个方法 " .. tostring(k))
    end })
    if type(parent) == "table" then
        parent.__children = parent.__children or {}
        parent.__children[#parent.__children + 1] = f
    end
    if name then _G[name] = f end
    GDL_FRAMES[#GDL_FRAMES + 1] = f
    return f
end
-- 暴雪的 ESC 关闭名单：里面存的是**全局帧名字符串**，客户端拿它去 _G 找帧
-- （GoldQuestLedger/Config.lua:273-283 注册的就是这个表，按名字插，重复不插）。
UISpecialFrames = {}
-- GameTooltip：界面唯一的悬停出口。只给本仓库生产代码真用过的这几个方法
-- （Matrix.lua:494 SetOwner / 495 ClearLines / 548 AddLine / 668 Show / 2465 Hide），
-- 名单外取用照旧抛错；GetWidth/GetHeight 是 Frame 共有方法（Matrix.lua:1251 同类），
-- probe 报"提示现在多宽"要用。SetAlpha / SetBackdropColor 是第 4 轮"提示不许半透明"
-- 加的两个 Frame 共有方法（Matrix.lua:1585 同一对）。
GDL_TIP_LINES = {}
GameTooltip = { __shown = false, __owner = nil }
function GameTooltip.SetOwner(self, owner, anchor) self.__owner, self.__anchor = owner, anchor end
function GameTooltip.SetAlpha(self, a) self.__alpha = a end
function GameTooltip.SetBackdropColor(self, r, g, b, a) self.__bdcolor = { r, g, b, a } end
function GameTooltip.ClearLines(self)
    for i = #GDL_TIP_LINES, 1, -1 do GDL_TIP_LINES[i] = nil end
end
function GameTooltip.AddLine(self, text, r, g, b)
    GDL_TIP_LINES[#GDL_TIP_LINES + 1] = { text = tostring(text), r = r, g = g, b = b }
end
function GameTooltip.AddDoubleLine(self, left, right, lr, lg, lb, rr, rg, rb)
    -- 双列行（Minimap.lua 悬停收支段用，形状照 GoldLedger）：替身拼一行文本，
    -- tip_texts 读 .text；真机上是左右两列，替身里用单空格连。
    GDL_TIP_LINES[#GDL_TIP_LINES + 1] =
        { text = tostring(left) .. " " .. tostring(right) }
end
function GameTooltip.Show(self) self.__shown = true end
function GameTooltip.Hide(self) self.__shown = false end
function GameTooltip.GetWidth(self)
    local w = 0
    for i = 1, #GDL_TIP_LINES do
        local line = GDL_EstimateWidth(GDL_TIP_LINES[i].text)
        if line > w then w = line end
    end
    return w
end
function GameTooltip.GetHeight(self) return #GDL_TIP_LINES * 14 end
setmetatable(GameTooltip, { __index = function(_, k)
    error("GDL mock GameTooltip: 没有这个方法 " .. tostring(k))
end })
-- 周 CD 倒计时（计划 §3 的周口径来源）：存在性与返回值形状都指得出真机证据
-- （GoldQuestLedger/Api.lua:1408-1423 用它，12.1.5 真机回传 AVAILABLE -> 511085，docs/log.txt:19）。
-- 默认值先给 nil："接口问不出"才是需要退化的那一档，用例自己注入秒数。
GDL_WEEK_RESET_SECONDS = nil
C_DateAndTime = {
    GetSecondsUntilWeeklyReset = function() return GDL_WEEK_RESET_SECONDS end,
}
-- 小地图宿主与光标（T4 的小地图按钮用）：两个名字的存在性都指得出本仓库的生产调用点 ——
--   Minimap 是全局帧（GoldQuestLedger/Matrix.lua:2431 取的就是它，12.1.5 真机建过按钮）；
--   GetCursorPosition 是 Matrix.lua:1243 / 2479 拖动算角度用的那个全局，回物理像素。
-- 宿主给 160x160、中心 (80,80)：暴雪小地图的常规边长，环上算式才有意义（不是编方法，是编尺寸）。
-- 用例把 Minimap 置 nil 就还原"宿主取不到"那一档降级。
GDL_CURSOR = { 0, 0 }
function GetCursorPosition() return GDL_CURSOR[1], GDL_CURSOR[2] end
Minimap = CreateFrame("Frame", "Minimap", UIParent)
Minimap:SetSize(160, 160)
Minimap.__cx, Minimap.__cy = 80, 80
-- 用例入口（和 GDL_FireEvent 同一类测试侧入口，不是客户端 API）：
--   GDL_CallScript 按客户端那样调挂在帧上的脚本，没挂着就回 false ——
--     那个 false 本身就是判据（"空闲状态不许挂 OnUpdate"靠它读）；
--   GDL_Click 反过来，没挂 OnClick 直接抛错 —— 点了个不存在的东西不该算绿。
function GDL_CallScript(f, name, ...)
    local s = f.__scripts[name]
    if not s then return false end
    s(f, ...)
    return true
end
function GDL_Click(f, ...)
    if not f.__scripts["OnClick"] then
        error("GDL mock: 这帧没挂 OnClick（按钮 " .. tostring(f.__text) .. "）")
    end
    return f.__scripts["OnClick"](f, ...)
end
-- 真实事件入口：只有注册过该事件的帧才收到派发（客户端语义）。
-- 生命周期用例必须从这里进，不许直接调 Core 的内部函数。
function GDL_FireEvent(event, ...)
    local n = 0
    for i = 1, #GDL_FRAMES do
        local f = GDL_FRAMES[i]
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
    lua.execute(GDL_MOCK_ENV)
    lua.execute("GDL = GDL or {}")   # 命名空间：游戏里由 vararg 提供，测试预置
    files = toc_lua_files()
    for n in names:
        if n + ".lua" not in files:
            raise AssertionError("load_lua(%s)：该文件不在 toc 清单里" % n)
    for fn in files:
        with open(os.path.join(PLUGIN_DIR, fn), encoding="utf-8") as f:
            lua.execute(f.read(), ADDON_NAME, None)
    return lua, lua.globals().GDL


def fire(lua, event, *args):
    return lua.globals()["GDL_FireEvent"](event, *args)


def slash(lua, text):
    """走 SlashCmdList 真入口跑一条命令，返回这次 print 出来的行。"""
    out = lua.globals()["GDL_OUTPUT"]
    start = len(out)
    lua.globals()["SlashCmdList"]["GDL"](text)
    return [str(out[i]) for i in range(start + 1, len(out) + 1)]


def printed_lines(lua):
    out = lua.globals()["GDL_OUTPUT"]
    return [str(out[i]) for i in range(1, len(out) + 1)]


# ---------------------------------------------------------------------------
# T1 记账层的夹具：全部走真入口（事件从 GDL_FireEvent 进、钩子从客户端全局函数进、
# 命令从 SlashCmdList 进），一行都不直接调 Money 的内部函数。
# ---------------------------------------------------------------------------

def as_number(v):
    return None if v is None else int(round(float(v)))


def money_env(ledger=True):
    """装一个带假账本的干净环境（假账本把 AddEntry 的调用录进 GDL_CALLS）。
    ledger=False 用来验"账本还没注册时不许报错"那条降级 —— T2 起 Ledger.lua 进了 toc，
    load_lua 会把它也加载进来，所以这一档必须把真 Ledger 摘掉，才还原"账本缺席"。"""
    lua, _ = load_lua("Core", "Money")
    lua.execute("GDL_CALLS = {}")
    if ledger:
        lua.execute(r"""
            FakeLedger = { AddEntry = function(self, amount, source, meta)
                GDL_CALLS[#GDL_CALLS + 1] = { amount = amount, source = tostring(source),
                                              meta = meta }
            end }
            GDL.RegisterModule("Ledger", FakeLedger)
        """)
    else:
        lua.execute(r"""
            GDL.modules["Ledger"] = nil
            GDL.Ledger = nil
        """)
    return lua


def set_money(lua, amount):
    lua.execute("GDL_MONEY = %d" % int(amount))


def set_now(lua, seconds):
    lua.execute("GDL_NOW = %d" % int(seconds))


def set_seller(lua, value):
    """AUCTION_HOUSE_MAIL_SELLER：给 nil 就是"国服拿不到这个常量"那种情形。"""
    if value is None:
        lua.execute("AUCTION_HOUSE_MAIL_SELLER = nil")
    else:
        lua.execute("AUCTION_HOUSE_MAIL_SELLER = %r" % value)


def set_inbox(lua, index, sender, subject, money):
    lua.execute("GDL_INBOX[%d] = { sender = %s, subject = %s, money = %d }"
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
    rows = lua.globals()["GDL_CALLS"]
    out, i = [], 1
    while True:
        row = rows[i]
        if row is None:
            break
        out.append((as_number(row.amount), str(row["source"])))
        i += 1
    return out


def call_meta(lua, index, key):
    row = lua.globals()["GDL_CALLS"][index]
    if row is None:
        return None
    meta = row.meta
    if meta is None:
        return None
    return str(meta[key]) if meta[key] is not None else None


def session_stats(lua):
    s = lua.globals()["GDL"].Money.GetSessionStats()
    return as_number(s.income), as_number(s.expense), as_number(s.net)


# ---------------------------------------------------------------------------
# T2 存储层的夹具：真 Ledger（不挂假账本），移动全部走 Money 的真实路径（change_money
# 带正确的上下文/号名/墙上钟），只有需要"另一个号的键"或复现某号流水时才直接 AddEntry。
# 一致性核对的算式放在 Lua 侧、贴着数据跑，Python 只收结论字符串。
# ---------------------------------------------------------------------------

def ledger_env():
    return load_lua("Core", "Money", "Ledger")[0]


def set_epoch(lua, seconds):
    lua.execute("GDL_EPOCH = %d" % int(seconds))


def set_char(lua, name, realm="暴龙王卡尔波斯"):
    lua.execute("GDL_UNIT_NAME = %s; GDL_REALM_NAME = %s"
                % (("%r" % name) if name is not None else "nil",
                   ("%r" % realm) if realm is not None else "nil"))


def set_clock_none(lua):
    """三个墙上钟候选全置 nil（用来验 /gdl ledger 在没钟时不报错）。"""
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
    lua.execute("GDL.Ledger:AddEntry(%d, %s, %s)"
                % (int(amount), ("%r" % source), meta_expr))


# 一段贴数据的核对：daily == Σ byDayChar == Σ entries(排除 transfer)，且全范围合计 == Σ 天格。
# 返回问题串（空 = 一致）。GDL_CONSISTENCY 只读不写。
GDL_CONSISTENCY = r"""
function GDL_CONSISTENCY()
    local L = GDL.Ledger
    local db = GDLDB
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
    """版本号两处一致（toc 的 ## Version 与运行时的 GDL.VERSION）。
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
    runtime = str(lua.globals()["GDL"]["VERSION"])
    if runtime != m:
        return fail("version_matches_toc", "toc=%r 运行时 GDL.VERSION=%r 不一致" % (m, runtime))
    ok("version_matches_toc")


def test_slash_command_registered():
    """斜杠命令两处注册耦合（SLASH_GDL1 全局 + SlashCmdList 的大写键名），
    改错任何一处聊天里命令直接消失且不报错 —— 只能行为地钉这两处。"""
    lua, _ = load_lua("Core")
    g = lua.globals()
    handler = g["SlashCmdList"]["GDL"]
    if not callable(handler):
        return fail("slash_command_registered",
                    "SlashCmdList['GDL'] 应为 function，实际 %r" % (handler,))
    if str(g["SLASH_GDL1"]) != "/gdl":
        return fail("slash_command_registered", "SLASH_GDL1=%r，应为 '/gdl'" % (str(g["SLASH_GDL1"]),))
    ok("slash_command_registered")


def test_module_registry_and_lifecycle():
    """模块注册表 + 生命周期：注册/查询/经真实事件入口跑 OnInitialize、OnLogin；
    一个模块自爆不许连坐；别人的 ADDON_LOADED 不许触发我们的初始化。"""
    lua, gcl = load_lua("Core")
    lua.execute(r"""
        GDL_INIT_COUNT, GDL_LOGIN_COUNT = 0, 0
        GDLGood = { OnInitialize = function() GDL_INIT_COUNT = GDL_INIT_COUNT + 1 end,
                    OnLogin = function() GDL_LOGIN_COUNT = GDL_LOGIN_COUNT + 1 end }
        GDLBoom = { OnInitialize = function() error("模块自爆") end,
                    OnLogin = function() GDL_LOGIN_COUNT = GDL_LOGIN_COUNT + 1 end }
        GDL.RegisterModule("good", GDLGood)
        GDL.RegisterModule("boom", GDLBoom)
    """)
    if not lua.eval('GDL.modules.good == GDLGood and GDL.good == GDLGood'):
        return fail("module_registry_and_lifecycle", "RegisterModule 没同时挂进 modules 和命名空间")
    if not lua.eval('GDL:GetModule("good") == GDLGood'):
        return fail("module_registry_and_lifecycle", "GetModule('good') 取回的不是注册的模块")
    if not lua.eval('GDL:GetModule("nope") == nil'):
        return fail("module_registry_and_lifecycle", "GetModule 未注册名应返回 nil")
    # 别的插件的 ADDON_LOADED：一个初始化都不许发生
    fire(lua, "ADDON_LOADED", "SomeOtherAddon")
    if lua.eval("GDL_INIT_COUNT") != 0 or lua.globals()["GDLDB"] is not None:
        return fail("module_registry_and_lifecycle", "别人的 ADDON_LOADED 触发了我们的初始化")
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    # boom 与 good 都在模块表里；pairs 顺序不定，但 pcall 逐个包 => good 必跑
    if lua.eval("GDL_INIT_COUNT") != 1:
        return fail("module_registry_and_lifecycle",
                    "OnInitialize 应恰好跑 good 一次（boom 的错被隔离），实际 %r"
                    % (lua.eval("GDL_INIT_COUNT"),))
    if not any("boom" in ln and "出错" in ln for ln in printed_lines(lua)):
        return fail("module_registry_and_lifecycle", "boom 模块的自爆没被报出来（静默吞错更糟）")
    fire(lua, "PLAYER_LOGIN")
    if lua.eval("GDL_LOGIN_COUNT") != 2:
        return fail("module_registry_and_lifecycle",
                    "PLAYER_LOGIN 应让两个模块的 OnLogin 都跑（T1 的金币基线钩这一步）")
    ok("module_registry_and_lifecycle")


def test_savedvariables_defaults_merge_without_overwrite():
    """GDLDB 合并只补缺失键：活档里已有的值（哪怕是旧 schema）一个都不许抹。"""
    lua, _ = load_lua("Core")
    lua.execute(r"""
        GDLDB = { schema = 7,
                  daily = { ["2026-11-10"] = { income = 5, expense = 0 } },
                  entries = { { t = time(), amt = 5 } } }
    """)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    checks = [
        ('GDLDB.schema', 7, "已有 schema 不许覆盖（迁移表 T2 落地，这里只立缺口）"),
        ('GDLDB.daily["2026-11-10"].income', 5, "已有流水不许被抹"),
        ('#GDLDB.entries', 1, "已有 entries 不许被清空"),
        ('GDLDB.settings ~= nil', True, "缺失键应被补上"),
        ('GDLDB.byDayChar ~= nil', True, "缺失键应被补上"),
        ('GDLDB.chars ~= nil', True, "缺失键应被补上"),
    ]
    for expr, want, why in checks:
        got = lua.eval(expr)
        if got != want:
            return fail("savedvariables_defaults_merge_without_overwrite",
                        "%s：%s（期望 %r 实际 %r）" % (why, expr, want, got))
    # 再喂一次（UI reload 口径）：合并必须幂等，不许把刚补的表换成新的
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    if lua.eval('GDLDB.daily["2026-11-10"].income') != 5:
        return fail("savedvariables_defaults_merge_without_overwrite", "二次初始化抹掉了已有数据")
    ok("savedvariables_defaults_merge_without_overwrite")


def test_dump_and_version_commands_do_not_error():
    """version / dump 走 /gdl 真入口：空库、有库都不许报错；
    空子命令与未知子命令打印排序后的命令清单。"""
    lua, _ = load_lua("Core")
    lines = slash(lua, "version")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "version 报错：%s" % lines)
    ver = str(lua.execute("return tostring(GDL.VERSION)"))
    if not any(("GDL " + ver) in ln and ADDON_NAME in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error",
                    "version 没打出 'GDL <版本> + toc 名'：%s" % lines)
    # 空库（还没跑过 ADDON_LOADED）dump 也不许炸
    lines = slash(lua, "dump")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "空库 dump 报错：%s" % lines)
    if not any("0 天" in ln and "0 笔流水" in ln and "0 个角色" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "空库 dump 应报全 0：%s" % lines)
    lua.execute(r"""
        GDLDB = { daily = { ["2026-11-10"] = {}, ["2026-11-11"] = {} },
                  entries = { { t = 1 }, { t = 2 }, { t = 3 } },
                  chars = { ["甲-暴龙王卡尔波斯"] = {} } }
    """)
    lines = slash(lua, "dump")
    if any("出错" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error", "有库 dump 报错：%s" % lines)
    if not any("2 天" in ln and "3 笔流水" in ln and "1 个角色" in ln for ln in lines):
        return fail("dump_and_version_commands_do_not_error",
                    "有库 dump 计数不对：%s" % lines)
    want_cmds = sorted(str(k) for k in lua.globals()["GDL"]["commands"].keys())
    for probe in ("", "nosuchcommand"):
        lines = slash(lua, probe)
        joined = " / ".join(lines)
        if "出错" in joined:
            return fail("dump_and_version_commands_do_not_error", "%r 路径报错：%s" % (probe, lines))
        # 清单按注册表算，不写死名字：模块往 /gdl 里加子命令是常态，
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


def test_money_quest_ttl_recovers_after_loot_window():
    """2026-11-11 三号对比实测（用户报：同一笔 992金34银65铜 的任务奖励，月之泪/Mad
    记成任务、群山之力/回到最初/爱情之力记成"其他"）：交任务与钱到账之间隔着一个
    拾取窗 —— QUEST_TURNED_IN 立起的 quest 上下文被 LOOT_OPENED 盖成 loot，
    LOOT_CLOSED 又清回 unknown，任务金到了就没上下文。
    补归窗：QUEST_TURNED_IN 后 30 秒内的界面外收入补归任务，命中即耗尽；
    过期不补、支出不补、真开着的窗口上下文优先（vendor 那笔不耗补归窗）。"""
    name = "money_quest_ttl_recovers_after_loot_window"
    lua = money_env()
    boot(lua, 100000)
    problems = []
    set_now(lua, 0)
    fire(lua, "QUEST_TURNED_IN")           # 交任务，钱还没到（奖励宝箱/结算动画隔开）
    fire(lua, "LOOT_OPENED")               # 奖励宝箱弹出战利品窗：quest 被盖成 loot
    change_money(lua, 105383)              # +5金38银83铜：战利品窗里的钱 → loot
    fire(lua, "LOOT_CLOSED")               # 关窗 → unknown
    change_money(lua, 10028848)            # +992金34银65铜：任务金这时才到 → quest（补归）
    change_money(lua, 10029102)            # +2金4银52铜：补归已耗尽 → unknown
    set_now(lua, 31)
    fire(lua, "QUEST_TURNED_IN")           # 直接 quest 上下文 + 补归窗武装到 61 秒
    change_money(lua, 10039102)            # +10000：直接上下文一笔就清（不随时间过期）→ quest
    fire(lua, "QUEST_TURNED_IN")           # 重新武装到 61 秒
    fire(lua, "LOOT_OPENED")               # 又一个拾取窗盖掉 quest（没动钱）
    fire(lua, "LOOT_CLOSED")
    set_now(lua, 62)
    change_money(lua, 10040102)            # +1000：补归窗过期不补 → unknown
    fire(lua, "QUEST_TURNED_IN")           # 重新武装到 92 秒
    fire(lua, "LOOT_OPENED")               # 拾取窗把 quest 上下文盖掉（没动钱）
    fire(lua, "LOOT_CLOSED")
    change_money(lua, 10039102)            # -1000：支出不补 → unknown（补归窗还在）
    change_money(lua, 10049102)            # +10000：补归窗还在 → quest
    set_now(lua, 100)
    fire(lua, "QUEST_TURNED_IN")           # 武装到 130 秒
    fire(lua, "MERCHANT_SHOW")
    change_money(lua, 10060102)            # +11000：真开着的窗口优先 → vendor（不耗补归窗）
    fire(lua, "MERCHANT_CLOSED")
    change_money(lua, 10071102)            # +11000：关窗后命中 → quest
    want = [(5383, "loot"), (9923465, "quest"), (254, "unknown"),
            (10000, "quest"), (1000, "unknown"), (-1000, "unknown"),
            (10000, "quest"), (11000, "vendor"), (11000, "quest")]
    got = calls(lua)
    if got != want:
        problems.append("逐笔归属应为 %s，实际 %s" % (want, got))
    if session_stats(lua) != (9972102, 1000, 9971102):
        problems.append("会话统计应为 (9972102, 1000, 9971102)，实际 %s" % (session_stats(lua),))
    lines = slash(lua, "probe")
    if "任务补归窗" not in "\n".join(lines):
        problems.append("probe 没报任务补归窗状态：%s" % lines[-4:])
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


def test_money_token_buy_marks_token():
    """BuyToken 当场抓的市价，下一笔精确同额出账记 token（照常计入收支）。"""
    name = "money_token_buy_marks_token"
    lua = money_env()
    boot(lua, 10000000000)                       # 100 万金
    lua.execute("C_WowTokenPublic.BuyToken()")
    change_money(lua, 3200000000)                # 花掉 68 万金（68 万 × 10000 铜）
    problems = []
    if calls(lua) != [(-6800000000, "token")]:
        problems.append("应记 token -6800000000，实际 %s" % (calls(lua),))
    if session_stats(lua) != (0, 6800000000, -6800000000):
        problems.append("徽章支出要计会话统计，实际 %s" % (session_stats(lua),))
    return expect(name, problems)


def test_money_token_buy_amount_mismatch():
    """金额对不上（取消购买/价格变了）→ 回落上下文，不标 token。"""
    name = "money_token_buy_amount_mismatch"
    lua = money_env()
    boot(lua, 10000000000)
    lua.execute("C_WowTokenPublic.BuyToken()")
    change_money(lua, 3200000001)
    problems = []
    if calls(lua) != [(-6799999999, "unknown")]:
        problems.append("对不上应回落 unknown，实际 %s" % (calls(lua),))
    return expect(name, problems)


def test_money_token_sell_mail():
    """卖出徽章的金币由拍卖行邮件送来：标题含徽章物品名 → token；普通物品仍 ah。"""
    name = "money_token_sell_mail"
    seller = "拍卖行"
    problems = []

    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "时光徽章", 6800000000)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 6800001000)
    if calls(lua) != [(6800000000, "token")]:
        problems.append("徽章邮件应记 token，实际 %s" % (calls(lua),))

    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "出售: 白银护臂", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1500)
    if calls(lua) != [(500, "ah")]:
        problems.append("普通物品仍应 ah，实际 %s" % (calls(lua),))
    return expect(name, problems)


def test_money_token_name_unavailable_uses_locale():
    """物品名读不到时退回语言表的「时光徽章」，卖出仍认得出来。"""
    name = "money_token_name_unavailable_uses_locale"
    lua = money_env()
    lua.execute("GDL_TOKEN_NAME = nil")
    set_seller(lua, "拍卖行")
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, "拍卖行", "时光徽章", 6800000000)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 6800001000)
    problems = []
    if calls(lua) != [(6800000000, "token")]:
        problems.append("名字读不到应退回语言表，实际 %s" % (calls(lua),))
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
    """`/gdl probe` 是给他日后在真机取证用的：所有可选接口都缺席时不许报错，
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
    if as_number(lua.globals()["GDL"].Money.GetDroppedCount()) != 2:
        problems.append("内部计数应记满 2 笔丢失，实际 %s"
                        % (lua.globals()["GDL"].Money.GetDroppedCount(),))
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
    lua.execute(GDL_CONSISTENCY)
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
    res = str(ledger_eval(lua, "GDL_CONSISTENCY()"))
    if res:
        problems.append("三处冗余/范围合计不一致：%s" % res)
    n_entries = as_number(ledger_eval(lua, "#GDLDB.entries"))
    if n_entries != movements + transfers:
        problems.append("entries 应恰有 %d 条（每笔一条），实际 %d" % (movements + transfers, n_entries))
    if movements < 200:
        problems.append("非转账笔数应铺到 200 左右，实际 %d" % movements)
    n_chars = as_number(ledger_eval(lua, "(function() local c=0 for _ in pairs(GDLDB.chars) do c=c+1 end return c end)()"))
    if n_chars != 3:
        problems.append("应认出 3 个号，实际 %d" % n_chars)
    # GetCharKeys / GetLastSeen：全集 3 个，且最近写过的一次就是墙上钟那一刻
    keys = str(ledger_eval(lua, "table.concat(GDL.Ledger.GetCharKeys(), '|')"))
    if keys.count("|") != 2 or "号1-" not in keys:
        problems.append("GetCharKeys 应列出 3 个号，实际 %r" % keys)
    seen = as_number(ledger_eval(lua, "GDL.Ledger.GetLastSeen('号3-%s')" % REALM))
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
    if as_number(ledger_eval(lua, "GDLDB.daily['2026-11-10'].income")) != 500:
        problems.append("垫底收入没进 daily")
    lua.execute("C_Bank.WithdrawMoney(1, 500)")     # 取出=进钱（搬钱）
    change_money(lua, 2000)
    lua.execute("DepositGuildBankMoney(300)")       # 存入=出钱（搬钱）
    change_money(lua, 1700)
    if as_number(ledger_eval(lua, "GDLDB.daily['2026-11-10'].income")) != 500 or \
       as_number(ledger_eval(lua, "GDLDB.daily['2026-11-10'].expense")) != 0:
        problems.append("搬钱改了 daily")
    if as_number(ledger_eval(lua, "GDLDB.byDayChar['2026-11-10']['阿尔萨斯-%s'].income" % REALM)) != 500:
        problems.append("搬钱改了 byDayChar")
    rt = as_number(ledger_eval(lua, "GDL.Ledger.GetRangeTotals('2026-11-10','2026-11-10').income"))
    if rt != 500:
        problems.append("搬钱改了范围合计：%s" % rt)
    if as_number(ledger_eval(lua, "#GDLDB.entries")) != 3:
        problems.append("三笔（含两笔搬钱）都该进 entries")
    tf = as_number(ledger_eval(lua, "(function() local c=0 for i=1,#GDLDB.entries do if GDLDB.entries[i].transfer then c=c+1 end end return c end)()"))
    if tf != 2:
        problems.append("应有 2 条 transfer 标记，实际 %s" % tf)
    flow = str(ledger_block(lua, r"""
        local r = GDL.Ledger.GetCharEntries('阿尔萨斯-%s', '2026-11-10', '2026-11-10', 1, 20)
        local s = {}
        for i=1,#r.rows do s[#s+1] = r.rows[i].src .. (r.rows[i].transfer and '*' or '') end
        return table.concat(s, ',') .. '|' .. tostring(r.bySource.warband and r.bySource.warband.income)
             .. '|' .. tostring(r.bySource.guildbank and r.bySource.guildbank.expense)
    """ % REALM))
    # 新在前：最后写的 guildbank 在最前，unknown 垫底在最后
    if flow != "guildbank*,warband*,unknown|500|300":
        problems.append("流水页/来源小计不符（搬钱要列出并打标记、小计含搬钱桶）：%s" % flow)
    return expect(name, problems)


def test_ledger_token_counts_normally():
    """token 不是搬钱：进 daily / byDayChar / 范围合计，transfer=false。"""
    name = "ledger_token_counts_normally"
    lua = ledger_env()
    set_epoch(lua, ep(2026, 11, 10))
    boot(lua, 1000000)
    set_char(lua, "爱情之力")
    add_entry(lua, -6800000000, "token")          # -68 万金
    add_entry(lua, 500000000, "token")            # +5 万金
    problems = []
    day = cal_eval(lua, "GDL.Ledger.GetDay('2026-11-10')")
    if as_number(day.expense) != 6800000000 or as_number(day.income) != 500000000:
        problems.append("token 没进 daily：%s/%s" % (as_number(day.income), as_number(day.expense)))
    if bool(cal_eval(lua, "GDLDB.entries[1].transfer")):
        problems.append("token 被误标成 transfer")
    rt = cal_eval(lua, "GDL.Ledger.GetRangeTotals('2026-11-10','2026-11-10')")
    if as_number(rt.net) != -6300000000:
        problems.append("token 没进范围合计：%s" % as_number(rt.net))
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
        got_d1 = str(ledger_eval(lua, "GDL.Ledger.DateKey(%d)" % e1))
        got_d2 = str(ledger_eval(lua, "GDL.Ledger.DateKey(%d)" % e2))
        got_m1 = str(ledger_eval(lua, "GDL.Ledger.MonthKey(%d)" % e1))
        got_m2 = str(ledger_eval(lua, "GDL.Ledger.MonthKey(%d)" % e2))
        if (got_d1, got_d2, got_m1, got_m2) != (d1, d2, m1, m2):
            problems.append("%s：键应为 (%s,%s,%s,%s)，实际 (%s,%s,%s,%s)"
                            % (label, d1, d2, m1, m2, got_d1, got_d2, got_m1, got_m2))
        if as_number(ledger_eval(lua, "GDL.Ledger.GetDay('%s').income" % d1)) != 111 or \
           as_number(ledger_eval(lua, "GDL.Ledger.GetDay('%s').income" % d2)) != 222:
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
        got = ledger_eval(lua, "GDL.Ledger.DaysInMonth(%d, %d)" % (y, m))
        if got is None or int(got) != want:
            problems.append("DaysInMonth(%d,%d) 应为 %d，实际 %r" % (y, m, want, got))
    # 已知日期的星期：*t.wday 口径（1=周日..7=周六），用 Python 反查当基准
    for iso in ["2024-06-10", "2023-01-01", "2024-12-25", "2026-11-10", "2025-02-28"]:
        py = datetime.strptime(iso, "%Y-%m-%d").weekday()       # Mon=0..Sun=6
        want_wday = ((py + 1) % 7) + 1                          # 换成 Sun=1..Sat=7
        got = ledger_eval(lua, "GDL.Ledger.WeekdayOfDateKey('%s')" % iso)
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
        local n = GDL.Ledger.DaysInMonth(y, mo)
        for d = 1, n do
            local key = string.format("%04d-%02d-%02d", y, mo, d)
            local wd = GDL.Ledger.WeekdayOfDateKey(key)
            if not wd or wd < 1 or wd > 7 then return 'badwday:' .. key end
            local day = GDL.Ledger.GetDay(key)
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
    lua.execute("GDL_OLD = %d GDL_RECENT = %d GDL_OLDKEY = %r GDL_RECENTKEY = %r"
                % (old, recent, oldKey, recentKey))
    lua.execute("GDL_CUR = %r GDL_GHOST = %r GDL_GONE = %r" % (cur, ghost, gone))
    lua.execute(r"""
        local oldKey, recentKey = GDL_OLDKEY, GDL_RECENTKEY
        local cur, ghost, gone = GDL_CUR, GDL_GHOST, GDL_GONE
        GDLDB.daily = {
            [oldKey] = { income = 11, expense = 3 },
            [recentKey] = { income = 9, expense = 0 },
        }
        GDLDB.byDayChar = {
            [oldKey] = { [cur] = { income = 5, expense = 3 }, [ghost] = { income = 6, expense = 0 } },
            [recentKey] = { [cur] = { income = 9, expense = 0 } },
        }
        GDLDB.entries = {
            { t = GDL_OLD, dateKey = oldKey, char = cur, src = 'unknown', amt = 5, transfer = false },
            { t = GDL_OLD, dateKey = oldKey, char = ghost, src = 'unknown', amt = 6, transfer = false },
            { t = GDL_RECENT, dateKey = recentKey, char = cur, src = 'unknown', amt = 9, transfer = false },
        }
        GDLDB.chars = {
            [cur] = { firstSeen = GDL_OLD, lastSeen = GDL_RECENT },
            [ghost] = { firstSeen = GDL_OLD, lastSeen = GDL_OLD },
            [gone] = { firstSeen = GDL_OLD, lastSeen = GDL_OLD },   -- 没被任何条目引用
        }
    """)
    set_epoch(lua, now)
    fire(lua, "ADDON_LOADED", ADDON_NAME)                       # 再初始化一次 ⇒ 跑 Cleanup
    problems = []
    checks = [
        ("GDLDB.daily['%s'] == nil" % oldKey, True, "过期天没从 daily 剪掉"),
        ("GDLDB.byDayChar['%s'] == nil" % oldKey, True, "过期天的 byDayChar 没剪"),
        ("GDLDB.daily['%s'] ~= nil" % recentKey, True, "窗口内的天被误删"),
        ("#GDLDB.entries == 1", True, "entries 应按时间剪掉两笔过期的，留窗口内那笔"),
        ("GDLDB.entries[1].char == %s" % repr(cur), True, "留下的应是窗口内当前号那笔"),
        ("GDLDB.chars[%s] == nil" % repr(ghost), True, "幽灵号只剩过期条目，引用没了就该被清"),
        ("GDLDB.chars[%s] ~= nil" % repr(cur), True, "当前号仍被引用，chars 行要留住"),
        ("GDLDB.chars[%s] == nil" % repr(gone), True, "没被引用的 chars 行要清掉"),
    ]
    for expr, want, why in checks:
        got = lua.eval(expr)
        if got != want:
            problems.append("%s（表达式 %s，期望 %r 实际 %r）" % (why, expr, want, got))
    # 幽灵号的过期条目确实被"全号遍历"清掉了（不是只清当前号）：ghost 一条都不剩
    ghost_left = as_number(ledger_eval(lua, "(function() local c=0 for i=1,#GDLDB.entries do if GDLDB.entries[i].char==%r then c=c+1 end end return c end)()" % ghost))
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
        GDLDB.entries = {}
        local base = %d - 100000        -- 都在 400 天窗口内，只让上限触发
        for i = 1, 20005 do
            GDLDB.entries[i] = { t = base + i, dateKey = date('%%Y-%%m-%%d', base + i),
                                 char = '甲-%s', src = 'unknown', amt = 1, transfer = false }
        end
        GDLDB.chars = { ['甲-%s'] = { firstSeen = base + 1, lastSeen = base + 20005 } }
    """ % (now, REALM, REALM))
    set_epoch(lua, now)
    fire(lua, "ADDON_LOADED", ADDON_NAME)     # Cleanup ⇒ 丢最老 5 条
    problems = []
    n = as_number(ledger_eval(lua, "#GDLDB.entries"))
    if n != 20000:
        problems.append("上限应砍到 20000，实际 %s" % n)
    first_t = as_number(ledger_eval(lua, "GDLDB.entries[1].t"))
    last_t = as_number(ledger_eval(lua, "GDLDB.entries[20000].t"))
    if first_t != (now - 100000 + 6):        # 最老的 5 条（base+1..base+5）被丢
        problems.append("丢的应是最老的 5 条：首条 t 应为 %d，实际 %d" % (now - 100000 + 6, first_t))
    if last_t != (now - 100000 + 20005):
        problems.append("新条目不许被丢：末条 t 应为 %d，实际 %d" % (now - 100000 + 20005, last_t))
    if as_number(ledger_eval(lua, "GDLDB.chars['甲-%s'] and 1 or 0" % REALM)) != 1:
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
    if as_number(ledger_eval(lua, "GDL.Ledger.GetDroppedWrites()")) != 3:
        problems.append("三次缺失应计三次丢弃，实际 %s" % as_number(ledger_eval(lua, "GDL.Ledger.GetDroppedWrites()")))
    for probe in ("#GDLDB.entries",
                  "(function() local c=0 for _ in pairs(GDLDB.chars) do c=c+1 end return c end)()",
                  "(function() local c=0 for _ in pairs(GDLDB.daily) do c=c+1 end return c end)()",
                  "(function() local c=0 for _ in pairs(GDLDB.byDayChar) do c=c+1 end return c end)()"):
        if as_number(ledger_eval(lua, probe)) != 0:
            problems.append("realm 缺失时 %s 不该有内容" % probe)
    # ② name 读不出来（realm 正常）
    set_char(lua, None, REALM)
    add_entry(lua, 100, "unknown")
    if as_number(ledger_eval(lua, "GDL.Ledger.GetDroppedWrites()")) != 4:
        problems.append("name 缺失也该计入丢弃，累计应为 4")
    if as_number(ledger_eval(lua, "#GDLDB.entries")) != 0:
        problems.append("name 缺失却写了 entries")
    # ③ 恢复正常：能写成一笔
    set_char(lua, "终于好了")
    add_entry(lua, 42, "loot")
    if as_number(ledger_eval(lua, "#GDLDB.entries")) != 1:
        problems.append("号名可读后应正常写入一笔")
    if str(ledger_eval(lua, "GDLDB.entries[1].char")) != "终于好了-%s" % REALM:
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
        local r = GDL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 1, 2)
        return r.total .. '/' .. r.totalPages .. '/' .. #r.rows .. '/' .. r.rows[1].src .. '/'
             .. tostring(r.rows[1].transfer) .. '/' .. r.rows[2].src
    """ % charkey))
    if meta != "5/3/2/warband/true/loot":
        problems.append("首页应 5 笔/3 页/每页2/新在前[warband(transfer),loot]，实际 %s" % meta)
    p3 = str(ledger_block(lua, r"""
        local r = GDL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 3, 2)
        return #r.rows .. '/' .. r.rows[1].src
    """ % charkey))
    if p3 != "1/vendor":
        problems.append("末页(第3页)应只剩 1 行 vendor，实际 %s" % p3)
    subject = str(ledger_block(lua, r"""
        local r = GDL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 2, 2)
        return tostring(r.rows[1].subject)
    """ % charkey))
    if subject != "出售: 甲":
        problems.append("第二页首行(ah)应带真读到的邮件标题，实际 %s" % subject)
    sub = str(ledger_block(lua, r"""
        local r = GDL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-10', 1, 100)
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
        local r = GDL.Ledger.GetCharEntries('%s', '2026-11-10', '2026-11-11', 1, 100)
        return r.total
    """ % charkey))
    if rng != "6":
        problems.append("放宽到次日应含 6 笔，实际 %s" % rng)
    return expect(name, problems)


def test_ledger_wallclock_sources_and_command():
    """/gdl ledger 取证 + 墙上钟候选顺位：都在时 time 优先应答；time 值不可信(<1e9)换
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
        problems.append("全有时 /gdl ledger 报错：%s" % lines)
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
        problems.append("无墙上钟时 /gdl ledger 报错：%s" % lines)
    if "没有可用的墙上时钟" not in joined:
        problems.append("无墙上钟时应报'没有可用的墙上时钟'：%s" % lines)
    return expect(name, problems)


# ---------------------------------------------------------------------------
# T3 界面夹具：全部走真入口 —— 开窗与翻页从 SlashCmdList / 帧上挂的 OnClick 进，
# 悬停从 OnEnter/OnLeave 进。Calendar 的 GetState()/GetUI() 只当取证件读，
# 用例一行都不往里写（写进去就是绕过生产路径自证，上一轮矩阵就是这么骗过我们的）。
# 界面上每一个数字都另算一遍期望值（Python 侧 datetime + 手抄的格式化契约），
# 不拿被测函数自己当标准答案。
# ---------------------------------------------------------------------------

ICON_GOLD = "|TInterface\\MoneyFrame\\UI-GoldIcon:12:12|t"
ICON_SILVER = "|TInterface\\MoneyFrame\\UI-SilverIcon:12:12|t"
ICON_COPPER = "|TInterface\\MoneyFrame\\UI-CopperIcon:12:12|t"

# Money 实际会写进条目的全部来源名（CONTEXT_SET 的 6 个 + repair + unknown + 2 个搬钱）。
CAL_ALL_SOURCES = ["vendor", "ah", "token", "mail", "trade", "loot", "quest",
                   "repair", "unknown", "warband", "guildbank"]


def calendar_env(day=(2026, 11, 10), char="当前号"):
    lua = ledger_env()          # toc 全量：Core / Money / Ledger / Locale / Calendar
    set_char(lua, char)
    set_epoch(lua, ep(*day))
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    fire(lua, "PLAYER_LOGIN")
    return lua


def cal_eval(lua, expr):
    return lua.eval("(function() return (%s) end)()" % expr)


def cal_open(lua):
    """走 /gdl open（真命令入口），返回窗开没开。"""
    slash(lua, "open")
    return cal_eval(lua, "GDLCalFrame and GDLCalFrame:IsShown() or false")


def cal_click(lua, expr):
    lua.globals()["GDL_Click"](cal_eval(lua, expr))


def cal_script(lua, expr, name):
    lua.globals()["GDL_CallScript"](cal_eval(lua, expr), name)


def cal_geom(lua, key):
    return as_number(cal_eval(lua, "GDL.Calendar.GEOM.%s" % key))


def cal_state(lua):
    def g(path):
        return cal_eval(lua, "GDL.Calendar.GetState().%s" % path)
    return {
        "year": as_number(g("year")), "month": as_number(g("month")),
        "preset": None if g("preset") is None else str(g("preset")),
        "startKey": None if g("startKey") is None else str(g("startKey")),
        "endKey": None if g("endKey") is None else str(g("endKey")),
        "selKey": None if g("selKey") is None else str(g("selKey")),
        "panelCount": as_number(g("panelCount")),
        "scrollY": as_number(g("scrollY")),
        "weekSource": str(g("weekCD.source")),
        "weekReason": None if g("weekCD.reason") is None else str(g("weekCD.reason")),
        "tipShown": bool(g("tip.shown")),
        "tipError": None if g("tip.error") is None else str(g("tip.error")),
        "errors": as_number(cal_eval(lua, "#GDL.Calendar.GetState().errors")),
    }


def cal_cells(lua):
    """42 格全量快照：键、月内/用到/今天/选中/可见 + 两行字 + 边框纹理与选中底色。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for i, c in ipairs(ui.cells) do
            out[#out + 1] = {
                key = tostring(c.dateKey or ""), inM = tostring(c.inMonth and 1 or 0),
                used = tostring(c.used and 1 or 0), today = tostring(c.today and 1 or 0),
                sel = tostring(c.selected and 1 or 0),
                shown = tostring(c.host:IsShown() and 1 or 0),
                num = tostring(c.num:GetText() or ""), net = tostring(c.net:GetText() or ""),
                edge = tostring((c.host.__backdrop and c.host.__backdrop.edgeFile) or ""),
                netR = (c.net.__color and ("%.4f"):format(c.net.__color[1])) or "",
                bdR = (c.host.__bdcolor and ("%.2f"):format(c.host.__bdcolor[1])) or "",
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("key", "inM", "used", "today", "sel", "shown",
                          "num", "net", "edge", "netR", "bdR"))


def cal_panel_rows(lua):
    """右栏行池全槽快照：号名 / 净 + 颜色 + 收入支出（悬停用） + 整行按钮与滚轮。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for i, r in ipairs(ui.charRows) do
            out[#out + 1] = {
                name = tostring(r.name:GetText() or ""), net = tostring(r.net:GetText() or ""),
                shown = tostring(r.host:IsShown() and 1 or 0), key = tostring(r.charKey or ""),
                income = tostring(r.income or 0), expense = tostring(r.expense or 0),
                nameR = (r.name.__color and ("%.4f"):format(r.name.__color[1])) or "",
                netR = (r.net.__color and ("%.4f"):format(r.net.__color[1])) or "",
                wheel = tostring((r.host.__wheel and r.host:GetScript("OnMouseWheel")) and 1 or 0),
                onclick = tostring(r.host:GetScript("OnClick") and 1 or 0),
                isButton = tostring(r.host.__type == "Button" and 1 or 0),
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("name", "net", "shown", "key", "income", "expense",
                          "nameR", "netR", "wheel", "onclick", "isButton"))


def cal_tabs(lua):
    """右栏三个页签快照：选中记号 / 高矮 / 底沿金线 / 底色字色 / 底沿锚点 + 字与量宽 + 接线。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for _, p in ipairs({ "day", "week", "month" }) do
            local t = ui.presets[p]
            local pt = (t.__points and t.__points[1]) or {}
            out[#out + 1] = {
                preset = p,
                active = tostring(t.active and 1 or 0),
                h = tostring(t:GetHeight()),
                mark = tostring(t.mark:IsShown() and 1 or 0),
                markH = tostring(t.mark:GetHeight()),
                bg = (t.__bdcolor and ("%.2f,%.2f,%.2f"):format(
                    t.__bdcolor[1], t.__bdcolor[2], t.__bdcolor[3])) or "",
                fg = (t.label.__color and ("%.2f,%.2f,%.2f"):format(
                    t.label.__color[1], t.label.__color[2], t.label.__color[3])) or "",
                text = tostring(t.label:GetText() or ""),
                textW = tostring(t.label:GetStringWidth()),
                anchor = tostring(pt[1] or ""),
                x = tostring(pt[4] or ""), y = tostring(pt[5] or ""),
                wired = tostring((t:GetScript("OnClick") and t:GetScript("OnEnter")
                    and t:GetScript("OnLeave")) and 1 or 0),
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("preset", "active", "h", "mark", "markH", "bg", "fg",
                          "text", "textW", "anchor", "x", "y", "wired"))


def lua_rows(tbl, fields):
    """把 Lua 侧序列化成"一字段一行"的快照表读回 Python（值一律 tostring 过）。"""
    if tbl is None:
        return None
    out, i = [], 1
    while True:
        row = tbl[i]
        if row is None:
            break
        out.append(dict((k, str(row[k])) for k in fields))
        i += 1
    return out


def cal_text(lua, expr):
    """读一帧/一条字串的当前文本（GetText 是替身名单里的客户端方法）。"""
    v = cal_eval(lua, "(%s):GetText()" % expr)
    return "" if v is None else str(v)


def cal_tip_texts(lua):
    """读 GameTooltip 替身里刚画上去的几行字（ClearLines 会清，Hide 不清）。
    日历主窗第 5 轮起改自绘提示框，Minimap / 流水弹窗仍用这一条。"""
    tips = lua.globals()["GDL_TIP_LINES"]
    out, i = [], 1
    while True:
        row = tips[i]
        if row is None:
            break
        out.append(str(row.text))
        i += 1
    return out


def cal_tip_frame_lines(lua):
    """读日历主窗那扇自绘提示框里当前可见的几行（按行号顺序）。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Calendar.GetUI()
        if not ui or not ui.tipLines then return nil end
        local out = {}
        for i, fs in ipairs(ui.tipLines) do
            if fs:IsShown() then out[#out + 1] = tostring(fs:GetText() or "") end
        end
        return out
    end)()""")
    return [] if tbl is None else [str(tbl[i]) for i in range(1, len(tbl) + 1)]


def cal_month_shape(y, mo):
    """（首日前的空格数, 月长, 需要的行数）—— 用 Python 的日历独立算一遍，
    专门用来核对 Ledger 那套 Sakamoto/闰年数学。"""
    offset = datetime(y, mo, 1, tzinfo=timezone.utc).isoweekday() % 7   # 周日 = 0
    dim = monthrange(y, mo)[1]
    return offset, dim, (offset + dim + 6) // 7


def cal_shift_month(lua, delta):
    expr = "GDL.Calendar.GetUI().btnNextMo" if delta > 0 else "GDL.Calendar.GetUI().btnPrevMo"
    for _ in range(abs(delta)):
        cal_click(lua, expr)


def _trim_decimal(whole, frac, digits):
    if digits <= 0:
        return str(whole)
    s = str(frac).rjust(digits, "0")[:digits].rstrip("0")
    return str(whole) if not s else "%d.%s" % (whole, s)


def cal_money_icon(copper):
    """手抄 Calendar.FormatMoneyIcon 的契约（2026-11-11 定稿）：只显示金币，
    银/铜尾数直接丢掉；大额用「万 / 亿」压缩（52.8万 / 1.05亿），
    万/亿以内最多两位小数、末尾 0 不写，超过 100 万/亿取整；
    不足 1 金显示 0 金，-0 不写负号；非数字回 "?"。"""
    if not isinstance(copper, (int, float)):
        return "?"
    gold = int(abs(copper)) // 10000
    sign = "-" if copper < 0 and gold > 0 else ""
    if gold >= 100000000:
        whole, rem = gold // 100000000, gold % 100000000
        digits = 0 if whole >= 100 else (1 if whole >= 10 else 2)
        frac = rem // (10 ** (8 - digits)) if digits else 0
        return sign + _trim_decimal(whole, frac, digits) + "亿" + ICON_GOLD
    if gold >= 10000:
        whole, rem = gold // 10000, gold % 10000
        digits = 0 if whole >= 100 else (1 if whole >= 10 else 2)
        frac = rem // (10 ** (4 - digits)) if digits else 0
        return sign + _trim_decimal(whole, frac, digits) + "万" + ICON_GOLD
    return "%s%d%s" % (sign, gold, ICON_GOLD)


def test_calendar_builds_nothing_until_open():
    """计划 §3「只在开窗和点击时算」：加载 + boot 完一个界面帧都不许造；
    /gdl open 才建，第二次 open 只关窗、绝不重建控件（重建=帧越开越多）。"""
    name = "calendar_builds_nothing_until_open"
    lua = calendar_env()
    problems = []
    if cal_eval(lua, "GDL.Calendar.GetUI()") is not None:
        problems.append("还没开窗就建了界面")
    if cal_eval(lua, "GDLCalFrame") is not None:
        problems.append("还没开窗就建了主帧")
    n0 = as_number(cal_eval(lua, "#GDL_FRAMES"))
    if cal_open(lua) is not True and str(cal_open(lua)) != "True":
        problems.append("/gdl open 没把窗开起来")
    n1 = as_number(cal_eval(lua, "#GDL_FRAMES"))
    if n1 <= n0:
        problems.append("开窗后一个控件都没建（帧数仍为 %s）" % n1)
    if cal_eval(lua, "GDLCalFrame:IsShown()") is not True:
        problems.append("开窗后主帧没显示")
    slash(lua, "open")                       # 再点一次 = 关
    if cal_eval(lua, "GDLCalFrame:IsShown()") is not False:
        problems.append("第二次 /gdl open 应把窗关掉")
    if as_number(cal_eval(lua, "#GDL_FRAMES")) != n1:
        problems.append("重复开窗重建了控件：帧数 %s → %s" % (n1, cal_eval(lua, "#GDL_FRAMES")))
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错 %d 次" % cal_state(lua)["errors"])
    return expect(name, problems)


def test_calendar_static_guards():
    """计划 §3 / 用户硬规矩（离线可钉的那部分）：界面不轮询、不订事件、不碰已删的接口，
    组件只用本仓库在 12.1.5 真机跑通过的模板，钱币图标一律三段式转义。"""
    name = "calendar_static_guards"
    with open(os.path.join(PLUGIN_DIR, "Calendar.lua"), encoding="utf-8") as fh:
        src = fh.read()
    problems = []
    for banned in ("OnUpdate", "C_Timer", "SetTicker", "RegisterEvent",
                   "GetItemInfo", "GetCoinText", "GetCoinIcon", "GetCoinTextureString",
                   "os.time", "os.date", "GetTime()"):
        if banned in src:
            problems.append("界面里出现了 %s（不轮询/不订事件/12.x 已删的接口不许碰）" % banned)
    # 注释里会写"没有每帧脚本"这类话，上面这些串只允许出现在代码侧；
    # 逐行剔掉 -- 开头的注释再查一遍，才是真判据。
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("--"))
    for banned in ("OnUpdate", "C_Timer", "SetTicker", "RegisterEvent", "GetTime"):
        if banned in code:
            problems.append("代码里出现了 %s" % banned)
    templates = set(re.findall(r'"([A-Za-z0-9_]*Template)"', code))
    allowed = {"BackdropTemplate", "UIPanelButtonTemplate", "UIPanelScrollFrameTemplate"}
    if not templates <= allowed:
        problems.append("用了本仓库没证过的模板：%s（只许 %s）"
                        % (sorted(templates - allowed), sorted(allowed)))
    if code.count('"UIPanelScrollFrameTemplate"') != 1:
        problems.append("右栏只该有一个滚动区（滚动区模板引用出现 %d 次）"
                        % code.count('"UIPanelScrollFrameTemplate"'))
    closes = re.findall(r'CreateFrame\("Button", nil, [A-Za-z_.]+, "(UIPanelCloseButton)"\)', code)
    if len(closes) != 1:
        problems.append("关闭按钮模板的用量应为 1 处，实际 %d 处" % len(closes))
    # T7：下半区整块拆掉，不许留着"两页共用槽位"那批控件或内嵌流水页的状态
    for dead in ("ui.listHead", "ui.flowRows", "ui.subRows", "ui.totalRow", "ui.moreChars",
                 "ui.moreSources", "ui.pageInfo", "ui.nav", "state.page", "flowChar",
                 "RenderLowerHead", "CELL_SUB", "Calendar.OpenFlow"):
        if dead in code:
            problems.append("下半区/内嵌流水页的残留：%s" % dead)
    if "flows.Open" not in code or "flows.RangeText" not in code:
        problems.append("右栏没接到流水弹窗（Flows.Open / RangeText）")
    # 右栏三档，顶栏读数仍含「全部」
    if 'ipairs({ "day", "week", "month", "all" })' in code:
        problems.append("右栏不该还有「全部」档（全部只当顶栏读数）")
    escapes = re.findall(r"\|T[^|\n]*\|t", code)
    if not escapes:
        problems.append("没找到任何 |T 图标转义（金额图标 formatter 被删了？）")
    for esc in escapes:
        if not re.search(r":\d+:\d+\|t$", esc):
            problems.append("图标转义不是三段式（客户端会原样印出这串字）：%s" % esc)
    if "GEOM.GRID_ROWS * 7" not in code:
        problems.append("网格没按 GRID_ROWS × 7 建满 42 格")
    # 只跟语言走的静态串不许在 Build 里摆：那样换语言后不重画就是"半中半英"（上一轮真踩过）。
    build_body = code.split("local function Build()")[1].split("    built = true")[0]
    stray = sorted(set(re.findall(r'T\("([A-Z_0-9]+)"', build_body)))
    if stray:
        problems.append("Build 里摆了静态字：%s（一律归 ApplyStaticTexts，否则换语言不重画）" % stray)
    if "ApplyStaticTexts()" not in build_body:
        problems.append("Build 尾没调 ApplyStaticTexts，静态字一个都没摆")
    refresh_body = code.split("function Calendar.Refresh()")[1].split("function Calendar.ShiftMonth")[0]
    if "ApplyStaticTexts()" not in refresh_body:
        problems.append("Refresh 没重摆静态字，换语言要重开窗口才生效")
    return expect(name, problems)


def test_calendar_money_format_compact_gold_only():
    """2026-11-11：只有流水行保留银/铜；其余读数与所有悬停只写金币。
    大额金币用「万 / 亿」压缩，方块和提示都摆得下（玩家几千万/上亿也常见）。"""
    name = "calendar_money_format_compact_gold_only"
    problems = []
    lua = calendar_env()
    cases = [
        (0, "0" + ICON_GOLD),
        (9999, "0" + ICON_GOLD),                       # 不足 1 金按 0 金
        (10000, "1" + ICON_GOLD),
        (12345 * 10000, "1.23万" + ICON_GOLD),         # 1.23 万金
        (528108 * 10000, "52.8万" + ICON_GOLD),        # 52.8 万金
        (99999999 * 10000, "9999万" + ICON_GOLD),
        (100000000 * 10000, "1亿" + ICON_GOLD),
        (105000000 * 10000, "1.05亿" + ICON_GOLD),
        (1234567890 * 10000, "12.3亿" + ICON_GOLD),
        (123456789012 * 10000, "1234亿" + ICON_GOLD),
        (-12345 * 10000, "-1.23万" + ICON_GOLD),
        (-9999, "0" + ICON_GOLD),                      # -0 不写负号
    ]
    for copper, want in cases:
        got = str(cal_eval(lua, "GDL.Calendar.FormatMoneyIcon(%d)" % copper))
        if got != want:
            problems.append("FormatMoneyIcon(%d) 应为 %r，实际 %r" % (copper, want, got))
    if str(cal_eval(lua, "GDL.Calendar.FormatMoneyIcon(nil)")) != "?":
        problems.append("非数字金额应回 ?，不能冒充 0")
    # 流水行仍走全量：金/银/铜都要在
    got = str(cal_eval(lua, "GDL.Calendar.FormatMoneyFull(12345678)"))
    want = "1234%s 56%s 78%s" % (ICON_GOLD, ICON_SILVER, ICON_COPPER)
    if got != want:
        problems.append("流水行全量金额被改了：应为 %r，实际 %r" % (want, got))
    return expect(name, problems)


def test_calendar_geometry_budget():
    """计划 §7.2 + Matrix.lua:1535-1537 那条真机要求：窗不许高过屏幕 85%（720 档 ⇒ 612）。
    双栏后左栏仍是 7×6 格；右栏到网格底为止，窗底整条留给六个读数方块。"""
    name = "calendar_geometry_budget"
    lua = calendar_env()
    cal_open(lua)
    problems = []
    win_h, max_h = cal_geom(lua, "WIN_H"), cal_geom(lua, "MAX_WIN_H")
    if max_h != int(720 * 0.85):
        problems.append("高度上限应是 720 的 85%%=%d，实际 %s" % (int(720 * 0.85), max_h))
    if win_h > max_h:
        problems.append("窗高 %d 超了预算 %d" % (win_h, max_h))
    want_w = (2 * cal_geom(lua, "PAD") + 7 * cal_geom(lua, "CELL_W")
              + cal_geom(lua, "PANEL_GAP") + cal_geom(lua, "PANEL_W"))
    if cal_geom(lua, "WIN_W") != want_w:
        problems.append("窗宽 %s 不等于 左栏 + 间隔 + 右栏 = %d"
                        % (cal_geom(lua, "WIN_W"), want_w))
    if cal_geom(lua, "GRID_ROWS") * 7 != len(cal_cells(lua)):
        problems.append("格子数 %d 与 GRID_ROWS×7 不符" % len(cal_cells(lua)))
    if cal_geom(lua, "CELL_H") < 2 * 12:
        problems.append("格高 %s 放不下两行 12 号字" % cal_geom(lua, "CELL_H"))
    list_h = cal_geom(lua, "PANEL_LIST_H")
    panel_h = cal_geom(lua, "SUMMARY_TOP") - cal_geom(lua, "PAD")
    if panel_h != win_h - 2 * cal_geom(lua, "PAD") - cal_geom(lua, "SUMMARY_H"):
        problems.append("右栏高度没按「窗内高 - 读数带」算：%s" % panel_h)
    if list_h != panel_h - cal_geom(lua, "PANEL_PRESET_H") \
            - cal_geom(lua, "PANEL_HEAD_H"):
        problems.append("右栏滚动区高 %s 没吃满右栏剩余高度" % list_h)
    if cal_geom(lua, "PANEL_ROWS") != int(list_h // cal_geom(lua, "ROW_H")):
        problems.append("右栏可见行数不是滚动区高 / 行高的整数部分")
    if cal_geom(lua, "PANEL_ROWS") < 11:
        problems.append("右栏可见行少于 11 行（小号多的形状就没出口了）")
    if as_number(cal_eval(lua, "GDL.Calendar.GetUI().scroll:GetHeight()")) != list_h:
        problems.append("滚动帧实高与 PANEL_LIST_H 不一致")
    if as_number(cal_eval(lua, "GDL.Calendar.GetUI().content:GetWidth()")) \
            != cal_geom(lua, "PANEL_W") - cal_geom(lua, "SCROLL_W"):
        problems.append("内容宽没给滚动条留位")
    if cal_geom(lua, "PRESET_STRIP_W") > cal_geom(lua, "PANEL_W"):
        problems.append("右栏三个页签排不进面板宽度")
    if cal_geom(lua, "PRESET_STRIP_W") != 3 * cal_geom(lua, "PRESET_W") \
            + 2 * cal_geom(lua, "PRESET_GAP"):
        problems.append("页签整条宽不是「三格 + 两条缝」")
    # 日历下方六个读数方块贯穿整个窗底（最后一个方块吸收除不尽的 4px）
    summary_w = cal_geom(lua, "SUMMARY_W")
    band_w = cal_geom(lua, "BAND_W")
    last_w = cal_geom(lua, "BAND_LAST_W")
    left = cal_geom(lua, "TOTALS_LEFT")
    right = left + 5 * band_w + last_w
    if summary_w != cal_geom(lua, "WIN_W") - 2 * cal_geom(lua, "PAD"):
        problems.append("读数带宽度没铺满窗内宽度")
    if right - left != summary_w or right != cal_geom(lua, "WIN_W") - cal_geom(lua, "PAD"):
        problems.append("六个方块没铺满整个读数带（右沿 %d）" % right)
    if cal_geom(lua, "PANEL_H") + 2 * cal_geom(lua, "PAD") + cal_geom(lua, "SUMMARY_H") \
            != win_h:
        problems.append("右栏底 + 读数带不等于窗高")
    if cal_geom(lua, "SUMMARY_TOP") + cal_geom(lua, "SUMMARY_H") \
            + cal_geom(lua, "PAD") != win_h:
        problems.append("读数带没算进窗高（会被切掉）")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_grid_shapes_and_weekday_math():
    """4 行 / 5 行 / 6 行三种月份形状（2026-02、2026-11、2027-01）逐格核对：
    每格的键与列位都用 Python 的日历独立算一遍 —— Sakamoto 星期数学错一天，这里当场错位。
    月外格只画底不摆字，超出本月的整行 Hide。"""
    name = "calendar_grid_shapes_and_weekday_math"
    lua = calendar_env()
    cal_open(lua)
    problems = []
    prev = (2026, 11)                       # 开窗落在今天那个月
    for (ty, tm), rows_exp in (((2026, 2), 4), ((2026, 11), 5), ((2027, 1), 6)):
        cal_shift_month(lua, (ty * 12 + tm) - (prev[0] * 12 + prev[1]))
        prev = (ty, tm)
        offset, dim, rows = cal_month_shape(ty, tm)
        if rows != rows_exp:
            problems.append("夹具算出的行数 %d 与用例期望 %d 不符（%d-%02d）" % (rows, rows_exp, ty, tm))
        cells = cal_cells(lua)
        info_rows = as_number(cal_eval(lua, "GDL.Calendar.GetUI().gridInfo.rowsNeeded"))
        if info_rows != rows:
            problems.append("%d-%02d 应需 %d 行，界面算出 %d 行" % (ty, tm, rows, info_rows))
        in_month = [c for c in cells if c["inM"] == "1"]
        if len(in_month) != dim:
            problems.append("%d-%02d 月内格 %d 个，应为 %d" % (ty, tm, len(in_month), dim))
        expect_keys = set("%04d-%02d-%02d" % (ty, tm, d) for d in range(1, dim + 1))
        got_keys = set(c["key"] for c in in_month)
        if got_keys != expect_keys:
            problems.append("%d-%02d 月内键对不上：缺 %s 多 %s"
                            % (ty, tm, sorted(expect_keys - got_keys), sorted(got_keys - expect_keys)))
        for i, c in enumerate(cells):
            row_used = (i // 7 + 1) <= rows
            if c["used"] != ("1" if row_used else "0"):
                problems.append("%d-%02d 第 %d 格 used=%s（应 %s）" % (ty, tm, i + 1, c["used"], row_used))
            if c["shown"] != c["used"]:
                problems.append("%d-%02d 第 %d 格可见性 %s 与 used %s 不符" % (ty, tm, i + 1, c["shown"], c["used"]))
            if not row_used:
                continue
            day = i + 1 - offset
            want_key = cal_eval(lua, "GDL.Calendar.KeyOf(%d, %d, %d)" % (ty, tm, day))
            if c["key"] != str(want_key):
                problems.append("%d-%02d 第 %d 格键 %s 与 KeyOf(%d,%d,%d)=%s 不符"
                                % (ty, tm, i + 1, c["key"], ty, tm, day, want_key))
            # 列位 = 星期：Python 独立算一遍，专门核 Ledger 的 Sakamoto 数学
            y_, m_, d_ = int(str(want_key)[:4]), int(str(want_key)[5:7]), int(str(want_key)[8:10])
            py_wday = datetime(y_, m_, d_, tzinfo=timezone.utc).isoweekday() % 7
            if py_wday != i % 7:
                problems.append("%s 落在第 %d 列，Python 日历判它应是第 %d 列"
                                % (want_key, i % 7 + 1, py_wday + 1))
            wd = as_number(cal_eval(lua, 'GDL.Ledger.WeekdayOfDateKey(%r)' % str(want_key)))
            if wd - 1 != i % 7:
                problems.append("Ledger 判 %s 的星期为 %s，与列位 %s 不符" % (want_key, wd, i % 7 + 1))
            if c["inM"] == "1":
                if c["num"] != str(d_):
                    problems.append("%s 的日号文本 %r 应为 %d" % (want_key, c["num"], d_))
            else:
                for k in ("num", "net"):
                    if cells[i][k] != "":
                        problems.append("月外格 %s 还摆了 %s=%r（草图要求只画底）" % (want_key, k, cells[i][k]))
        if problems and len(problems) > 12:
            break
    return expect(name, problems)


def test_calendar_cell_content_today_and_colors():
    """格子只两行（日号 / 净），净 = 图标金额（正绿负红）；灰色收·支小字彻底下线。
    零变动只留日号；只有今天带边框纹理，选中的那天另外用底色标出来。"""
    name = "calendar_cell_content_today_and_colors"
    lua = calendar_env()
    add_entry(lua, 1234567, "quest")            # 2026-11-10 收
    add_entry(lua, -500000, "vendor")           # 同天支
    set_epoch(lua, ep(2026, 11, 12))
    add_entry(lua, -250000, "repair")           # 2026-11-12 只有支出
    set_epoch(lua, ep(2026, 11, 10))            # 今天仍是 11-10
    cal_open(lua)
    cells = cal_cells(lua)
    problems = []
    if len(cells) != 42:
        problems.append("网格不是 42 格")
        return expect(name, problems)
    today, neg, zero = cells[9], cells[11], cells[4]     # 11-10 / 11-12 / 11-05（11-01 是周日）
    if today["key"] != "2026-11-10" or today["today"] != "1":
        problems.append("今天那格没对上：%s today=%s" % (today["key"], today["today"]))
    if today["net"] != cal_money_icon(1234567 - 500000):
        problems.append("今天净文本 %r 应为 %r" % (today["net"], cal_money_icon(734567)))
    if today["netR"] != "0.2400":
        problems.append("正净应绿（首分量 0.24），实际 %s" % today["netR"])
    if cal_eval(lua, "GDL.Calendar.GetUI().cells[10].sub") is not None:
        problems.append("格子还留着收·支小字控件（第三行该整行下线）")
    if neg["net"] != cal_money_icon(-250000):
        problems.append("负净文本 %r 应为 %r" % (neg["net"], cal_money_icon(-250000)))
    if neg["netR"] != "0.8600":
        problems.append("负净应红（首分量 0.86），实际 %s" % neg["netR"])
    if zero["net"] != "":
        problems.append("零变动的日子不该摆净：net=%r" % zero["net"])
    if zero["netR"] != "0.6200":
        problems.append("零变动应灰（首分量 0.62），实际 %s" % zero["netR"])
    outlined = [i for i, c in enumerate(cells) if c["edge"] != ""]
    if outlined != [9]:
        problems.append("带边框纹理的格子应是今天那一格（下标 9），实际 %s" % outlined)
    if today["sel"] != "1":
        problems.append("开窗选中的今天没标成选中")
    # 换选另一天：选中标跟着走，今天的描边不动；底色与描边是两件事
    cal_click(lua, "GDL.Calendar.GetUI().cells[12].host")
    cells2 = cal_cells(lua)
    if cells2[11]["sel"] != "1" or cells2[9]["sel"] != "0":
        problems.append("点 11-12 后选中标没跟着走：%s / %s" % (cells2[11]["sel"], cells2[9]["sel"]))
    if cells2[11]["bdR"] != "0.17":
        problems.append("选中格应用琥珀底色（首分量 0.17），实际 %s" % cells2[11]["bdR"])
    if cells2[9]["edge"] == "":
        problems.append("今天那格的描边不该因为换了选中日就消失")
    if cal_state(lua)["selKey"] != "2026-11-12":
        problems.append("点日号没把 selKey 挪到那天")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_text_clipping_is_utf8_safe():
    """右栏号名 = 去服名的短名 + 职业色转义，按**字节预算**裁断且不劈开 UTF-8 字符
    （半个字符客户端会画成方块）。像素级"有没有盖到净那列"不在离线断言：
    真值由 /gdl probe ui 量回来。格子里的收·支小字与缩写金额已下线。"""
    name = "calendar_text_clipping_is_utf8_safe"
    long_name = "一个非常非常长的号名用来考验截断是否劈字符"
    lua = calendar_env(char=long_name)
    add_entry(lua, 123456789, "quest")
    cal_open(lua)
    problems = []
    txt = cal_text(lua, "GDL.Calendar.GetUI().charRows[1].name")
    if REALM in txt:
        problems.append("角色名还带着服务器名：%r" % txt)
    m = re.match(r"^\|cff([0-9a-f]{6})(.*)\|r$", txt)
    if not m:
        problems.append("角色名没按 |cffRRGGBB...|r 上职业色：%r" % txt)
    else:
        if m.group(1) != "40c7eb":        # 替身的 MAGE 职业色
            problems.append("职业色不是 MAGE 的 40c7eb：%s" % m.group(1))
        body = m.group(2)
        if len(body.encode("utf-8")) > 24:
            problems.append("短名没按 24 字节预算截断：%r" % body)
        if not long_name.startswith(body):
            problems.append("截出来的不是号名开头：%r" % body)
        try:
            body.encode("utf-8").decode("utf-8")
        except UnicodeDecodeError:
            problems.append("截断劈开了 UTF-8 字符：%r" % body)
        if len(body) >= len(long_name):
            problems.append("夹具的号名没走到截断这条路")
    if cal_eval(lua, "GDL.Calendar.GetUI().cells[10].sub") is not None:
        problems.append("格子第三行控件没拆干净（收·支小字已下线）")
    if cal_text(lua, "GDL.Calendar.GetUI().cells[10].net") != cal_money_icon(123456789):
        problems.append("格子只该留净，且金额用整数金图标：%r"
                        % cal_text(lua, "GDL.Calendar.GetUI().cells[10].net"))
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_panel_rows_and_row_click():
    """点日号：右栏列**这天**有变动的号，当前号置顶；一行只有 号名 / 净 两列，
    号名去服名 + 职业色；整行就是那一颗按钮（点它开该号该范围的流水弹窗），
    悬停给收入 / 支出 / 金币（金币读不到写「—」）。0 变动的号不列 —— 玩家 2026-11-10 定的口径。"""
    name = "calendar_panel_rows_and_row_click"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")                        # 当前号 净 100 金
    set_char(lua, "甲")
    add_entry(lua, 300000, "vendor")
    add_entry(lua, -50000, "repair")                        # 甲 净 25 金
    set_char(lua, "乙")
    add_entry(lua, 2000000, "ah")                           # 乙 净 200 金（最大）
    set_char(lua, "当前号")
    cal_open(lua)
    problems = []
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"], st["selKey"]) \
            != ("day", "2026-11-10", "2026-11-10", "2026-11-10"):
        problems.append("开窗没落在「本日 + 今天」：%s" % st)
    rows = cal_panel_rows(lua)
    cur = "当前号-%s" % REALM
    if rows[0]["key"] != cur:
        problems.append("第一行应是当前号（哪怕它不是最富的），实际 %s" % rows[0]["key"])
    if not rows[0]["name"].startswith("|cff40c7eb") or not rows[0]["name"].endswith("|r"):
        problems.append("当前号那行该用 MAGE 职业色转义，实际 %r" % rows[0]["name"])
    if REALM in rows[0]["name"]:
        problems.append("当前号那行还带着服务器名：%r" % rows[0]["name"])
    if rows[1]["key"] != "乙-%s" % REALM or rows[2]["key"] != "甲-%s" % REALM:
        problems.append("其余号没按净降序：%s" % [r["key"] for r in rows])
    if rows[0]["net"] != cal_money_icon(1000000) or rows[0]["netR"] != "0.2400":
        problems.append("当前号行的净 %r / 色 %s 不对" % (rows[0]["net"], rows[0]["netR"]))
    if rows[2]["net"] != cal_money_icon(250000):
        problems.append("甲号净应是 25 金，实际 %r" % rows[2]["net"])
    if len([r for r in rows if r["shown"] == "1"]) != 3:
        problems.append("只该列 3 个有变动的号，实际 %d 行可见"
                        % len([r for r in rows if r["shown"] == "1"]))
    for i, r in enumerate(rows[:3]):
        if r["isButton"] != "1" or r["onclick"] != "1" or r["wheel"] != "1":
            problems.append("第 %d 行不是整行按钮 / 没挂点击 / 没挂滚轮：%s" % (i + 1, r))
    if cal_eval(lua, "GDL.Calendar.GetUI().charRows[1].inc") is not None:
        problems.append("右栏还留着收入那一列（应只有 号名 / 净）")
    for dead in ("moreChars", "totalRow", "pageInfo", "flowRows", "subRows", "listHead"):
        if cal_eval(lua, "GDL.Calendar.GetUI().%s" % dead) is not None:
            problems.append("下半区控件没拆干净：%s" % dead)
    if cal_eval(lua, "GDL.Calendar.GetUI().empty:IsShown()"):
        problems.append("有数据还显示空态")
    # 换一个没数据的日子 ⇒ 档位落本日、范围只问那一天、右栏空态
    cal_click(lua, "GDL.Calendar.GetUI().cells[12].host")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"], st["selKey"]) \
            != ("day", "2026-11-12", "2026-11-12", "2026-11-12"):
        problems.append("点别的日子没收成那天单日：%s" % st)
    if cal_text(lua, "GDL.Calendar.GetUI().empty") != "这个范围没有变动":
        problems.append("空范围文案 %r" % cal_text(lua, "GDL.Calendar.GetUI().empty"))
    if not cal_eval(lua, "GDL.Calendar.GetUI().empty:IsShown()"):
        problems.append("空范围没显示空态")
    # 点回有数据那天：悬停那一行给 收入 / 支出 / 金币（当前号登录时读到 0，照实写 0）
    cal_click(lua, "GDL.Calendar.GetUI().cells[10].host")
    cal_script(lua, "GDL.Calendar.GetUI().charRows[1].host", "OnEnter")
    tips = cal_tip_frame_lines(lua)
    if len(tips) != 3 or tips[0] != "收入 %s" % cal_money_icon(1000000) \
            or tips[1] != "支出 %s" % cal_money_icon(0) \
            or tips[2] != "金币 %s" % cal_money_icon(0):
        problems.append("右栏行提示应是三行 收入 / 支出 / 金币，实际 %r" % tips)
    if any("净" in ln for ln in tips):
        problems.append("右栏提示里不该再写净（净已经写在行上）：%r" % tips)
    cal_script(lua, "GDL.Calendar.GetUI().charRows[1].host", "OnLeave")
    if cal_state(lua)["tipShown"]:
        problems.append("OnLeave 没收行提示")
    # 点整行 ⇒ 该号该范围的独立弹窗
    cal_click(lua, "GDL.Calendar.GetUI().charRows[1].host")
    if not bool(cal_eval(lua, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("点右栏整行没开出流水弹窗")
    if str(cal_eval(lua, "GDL.Flows.GetState().charKey")) != cur:
        problems.append("弹窗锁的号不对：%s" % cal_eval(lua, "GDL.Flows.GetState().charKey"))
    if (str(cal_eval(lua, "GDL.Flows.GetState().startKey")),
            str(cal_eval(lua, "GDL.Flows.GetState().endKey"))) != ("2026-11-10", "2026-11-10"):
        problems.append("弹窗范围没跟着右栏走")
    want_title = "流水 · |cff40c7eb当前号|r · 本日 2026-11-10"
    if cal_text(lua, "GDL.Flows.GetUI().title") != want_title:
        problems.append("弹窗标题 %r 应为 %r" % (cal_text(lua, "GDL.Flows.GetUI().title"), want_title))
    if cal_eval(lua, "GDL.Flows.GetUI().btnChar") is not None:
        problems.append("两态「全部号」按钮没拆干净（角色改走下拉）")
    for key in ("char", "type", "range", "dir"):
        if cal_eval(lua, "GDL.Flows.GetUI().dropdowns.%s" % key) is None:
            problems.append("弹窗少了 %s 筛选下拉" % key)
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 1 页":
        problems.append("没有真分页页码：%r" % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    if len([s for s in flow_subs(lua) if s["shown"] == "1"]) < 1:
        problems.append("弹窗里没有可点选类型的分类小计")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_popup_replaces_embedded_flow_page():
    """点右栏行开出独立弹窗：12 笔在第 1 页一次列全（每页 20 行）、角色走下拉、
    点分类汇总行筛类型、× 关弹窗（旧「返回」的落点）；并钉住用户报的遮挡：
    弹窗层号必须高于主窗，× 仍钉在本窗主帧 +1。"""
    name = "calendar_popup_replaces_embedded_flow_page"
    lua = calendar_env()
    for k in range(12):                                    # 同一号同一天 12 笔，分钟递增
        set_epoch(lua, ep(2026, 11, 10, 9, k))
        add_entry(lua, -(1000 * (k + 1)), "vendor")
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    problems = []
    cal_click(lua, "GDL.Calendar.GetUI().charRows[1].host")
    if not bool(cal_eval(lua, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("点右栏整行没开出流水弹窗")
    shown_rows = [r for r in flow_rows(lua) if r["shown"] == "1"]
    if len(shown_rows) != 12:
        problems.append("12 笔应落在第 1 页（每页 20 行），实际 %d 行" % len(shown_rows))
    if shown_rows and shown_rows[0]["time"] != "2026-11-10 09:11":
        problems.append("第一行应是最新那笔（09:11），实际 %r" % shown_rows[0]["time"])
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 1 页":
        problems.append("真分页页码不对：%r" % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    if cal_eval(lua, "GDL.Flows.GetState().charKey") is None:
        problems.append("从右栏行进来应默认锁这个号")
    # 角色下拉第一项「全部角色」= 放开到跨号；不是两态按钮
    if not flow_dd_select(lua, "char", ""):
        problems.append("角色下拉里没有「全部角色」选项")
    if cal_eval(lua, "GDL.Flows.GetState().charKey") is not None:
        problems.append("选了全部角色却还锁着号")
    # 点分类汇总行筛类型，下拉选中值要同步（两个入口是同一份 state）
    vi = flow_sub_index(lua, "vendor")
    if vi is None:
        problems.append("小计里找不到 vendor 行（类型筛选没有入口）")
    else:
        cal_click(lua, "GDL.Flows.GetUI().subRows[%d].btn" % vi)
        if str(cal_eval(lua, "GDL.Flows.GetState().source")) != "vendor":
            problems.append("点小计行没把类型筛成 vendor")
        if str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.type.value")) != "vendor":
            problems.append("类型下拉没跟着汇总行同步")
    # 遮挡（2026-11-11 统一协议）：两窗 DIALOG + SetToplevel，开窗时各自 Raise 自抬
    # （mock 替身把层号抬到 1000），层序交给 WoW 的点击置顶，不再按层号压对端窗。
    # 这里钉：弹窗确实自抬过层（不低于主窗）、× 钉回本窗主帧 +1。
    flow_lvl = as_number(cal_eval(lua, "GDLFlowFrame:GetFrameLevel()"))
    cal_lvl = as_number(cal_eval(lua, "GDL.Calendar.GetFrame():GetFrameLevel()"))
    if not (flow_lvl is not None and cal_lvl is not None and flow_lvl >= cal_lvl):
        problems.append("弹窗层号 %s 没自抬到主窗 %s 之上（Raise 替身应抬到 1000）"
                        % (flow_lvl, cal_lvl))
    if as_number(cal_eval(lua, "GDL.Flows.GetUI().close:GetFrameLevel()")) != flow_lvl + 1:
        problems.append("× 抬层后没重钉回本窗主帧 +1")
    # 关闭弹窗 = 旧「返回」的落点：主窗还在、档位没被弹窗改掉
    cal_click(lua, "GDL.Flows.GetUI().close")
    if cal_eval(lua, "GDLFlowFrame:IsShown()"):
        problems.append("点 × 没关掉弹窗")
    if not cal_eval(lua, "GDLCalFrame:IsShown()"):
        problems.append("关弹窗把主窗一起关了")
    if cal_state(lua)["preset"] != "day" or cal_state(lua)["selKey"] != "2026-11-10":
        problems.append("开/关弹窗动了主窗的选中日或档位")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_panel_scroll_wheel_and_row_pool():
    """小号多：右栏内容高按行数长（不截断、不写"另有 N 个号未列出"），
    滚轮偏移自己记、两端夹住；每行按钮也挂 OnMouseWheel（Matrix.lua:1546 的教训）。"""
    name = "calendar_panel_scroll_wheel_and_row_pool"
    lua = calendar_env()
    for i in range(20):
        set_char(lua, "小号%d" % i)
        add_entry(lua, 1000 + i, "loot")
    set_char(lua, "当前号")
    cal_open(lua)
    problems = []
    row_h, list_h = cal_geom(lua, "ROW_H"), cal_geom(lua, "PANEL_LIST_H")
    if cal_state(lua)["panelCount"] != 20:
        problems.append("20 个小号应全列，实际 panelCount=%s" % cal_state(lua)["panelCount"])
    if as_number(cal_eval(lua, "GDLCalPanelContent:GetHeight()")) != 20 * row_h:
        problems.append("内容高应是 20 行 × %d，实际 %s"
                        % (row_h, cal_eval(lua, "GDLCalPanelContent:GetHeight()")))
    if not bool(cal_eval(lua, "GDLCalPanelScroll.__wheel")):
        problems.append("滚动帧没开 EnableMouseWheel")
    bad = [r for r in cal_panel_rows(lua) if r["wheel"] != "1"]
    if bad:
        problems.append("%d 个行按钮没挂滚轮" % len(bad))
    if cal_eval(lua, "GDL.Calendar.GetUI().moreChars") is not None:
        problems.append("号多时还在用「另有 N 个号未列出」而不是滚动")

    def wheel(expr, delta):
        lua.globals()["GDL_CallScript"](cal_eval(lua, expr), "OnMouseWheel", delta)
        return (as_number(cal_eval(lua, "GDL.Calendar.GetState().scrollY")),
                as_number(cal_eval(lua, "GDLCalPanelScroll.__scrolly")))

    y, written = wheel("GDLCalPanelScroll", -1)
    if (y, written) != (row_h, row_h):
        problems.append("轮 -1 应下一行(%d)并写出去，实际 %s/%s" % (row_h, y, written))
    y, _ = wheel("GDLCalPanelScroll", 1)
    if y != 0:
        problems.append("轮 +1 应回到顶 0，实际 %s" % y)
    top = 20 * row_h - list_h
    y, written = wheel("GDLCalPanelScroll", -100)
    if (y, written) != (top, top):
        problems.append("应夹在底 %d，实际 %s/%s" % (top, y, written))
    y, written = wheel("GDLCalPanelScroll", 100)
    if (y, written) != (0, 0):
        problems.append("应夹在顶 0，实际 %s/%s" % (y, written))
    y, written = wheel("GDL.Calendar.GetUI().charRows[1].host", -1)
    if (y, written) != (row_h, row_h):
        problems.append("滚轮落在行按钮上也该滚动，实际 %s/%s" % (y, written))
    if as_number(cal_eval(lua, "GDLCalFrame:GetHeight()")) != cal_geom(lua, "WIN_H"):
        problems.append("行数变了窗高不该跳")
    # 换档回到顶：滚动偏移不许带到新列表半腰
    cal_click(lua, "GDL.Calendar.GetUI().presets.month")
    if cal_state(lua)["scrollY"] != 0:
        problems.append("切档没把滚动偏移归零")
    return expect(name, problems)


def test_calendar_range_presets():
    """右栏三档（本日 / 本周 / 本月）都锚在**选中的那天**，且没有「全部」档（累计只在顶栏读数）。
    本周仍是周四起的周 CD：接口给当前 CD 锚点，按整周平移到包住选中日那一周；
    接口问不出就退化日历周周一，原因只进 probe。顶栏读数不随选中日/档位变。"""
    name = "calendar_range_presets"
    problems = []
    lua = calendar_env(day=(2026, 11, 10))
    cal_open(lua)
    if cal_eval(lua, "GDL.Calendar.GetUI().presets.all") is not None:
        problems.append("右栏不该还有「全部」档")
    if cal_text(lua, "GDL.Calendar.GetUI().totals.all.label") != "全部":
        problems.append("顶栏「全部」累计读数还在（只是不再当档位）")
    cal_click(lua, "GDL.Calendar.GetUI().presets.day")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"], st["selKey"]) \
            != ("day", "2026-11-10", "2026-11-10", "2026-11-10"):
        problems.append("本日 %s %s..%s 选中=%s"
                        % (st["preset"], st["startKey"], st["endKey"], st["selKey"]))
    # 点 11-05（周四）⇒ 选中日跟着走，本日档只问这一天
    cal_click(lua, "GDL.Calendar.GetUI().cells[5].host")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"], st["selKey"]) \
            != ("day", "2026-11-05", "2026-11-05", "2026-11-05"):
        problems.append("点 11-05 没收成那天的本日档：%s" % st)
    cal_click(lua, "GDL.Calendar.GetUI().presets.month")
    st = cal_state(lua)
    if (st["startKey"], st["endKey"], st["selKey"]) != ("2026-11-01", "2026-11-30", "2026-11-05"):
        problems.append("本月应锚在选中日所在月且不动选中日：%s" % st)
    # 本周 CD：倒计时 2 天 ⇒ 当前 CD 锚点 = 今天 + 2 天 - 7 天 = 2026-11-05（周四）
    lua.execute("GDL_WEEK_RESET_SECONDS = 172800")
    cal_click(lua, "GDL.Calendar.GetUI().presets.week")
    st = cal_state(lua)
    if (st["startKey"], st["endKey"]) != ("2026-11-05", "2026-11-11"):
        problems.append("本周应给 11-05..11-11 整周 CD，实际 %s..%s"
                        % (st["startKey"], st["endKey"]))
    if st["weekSource"] != "C_DateAndTime.GetSecondsUntilWeeklyReset":
        problems.append("接口答得出还走了别的来源：%s" % st["weekSource"])
    # 接口答非周口径 ⇒ 退化成选中日所在的日历周（周一~周日）
    lua.execute("GDL_WEEK_RESET_SECONDS = 700000")
    cal_click(lua, "GDL.Calendar.GetUI().presets.week")
    st = cal_state(lua)
    if st["weekSource"] != "monday-fallback":
        problems.append("接口答的不是周口径时应退化成日历周，实际 %s" % st["weekSource"])
    if (st["startKey"], st["endKey"]) != ("2026-11-02", "2026-11-08"):
        problems.append("2026-11-05 所在日历周应是 11-02..11-08，实际 %s..%s"
                        % (st["startKey"], st["endKey"]))
    if "周口径" not in (st["weekReason"] or ""):
        problems.append("退化原因没记进 probe：%s" % st["weekReason"])
    # 接口整个不存在 ⇒ 同样退化，且不许报错
    lua.execute("C_DateAndTime = nil")
    cal_click(lua, "GDL.Calendar.GetUI().presets.week")
    if cal_state(lua)["weekSource"] != "monday-fallback" or cal_state(lua)["errors"] != 0:
        problems.append("周接口缺席时没退化或报错了")
    # 顶栏「本月」跟着**显示月**走；右栏范围仍锚在选中那天，不跟翻月跑
    lua.execute("C_DateAndTime = { GetSecondsUntilWeeklyReset = function() return 172800 end }")
    set_epoch(lua, ep(2026, 11, 5))
    add_entry(lua, 1000000, "quest")         # 11-05：100 金（整数金口径下不能选不足 1 金的夹具）
    set_epoch(lua, ep(2026, 12, 3))
    add_entry(lua, 2000000, "loot")          # 12-03：200 金
    set_epoch(lua, ep(2026, 11, 10))
    cal_click(lua, "GDL.Calendar.GetUI().presets.day")
    if cal_text(lua, "GDL.Calendar.GetUI().totals.month.value") != cal_money_icon(1000000):
        problems.append("顶栏本月读数只该含 11 月那笔：%r"
                        % cal_text(lua, "GDL.Calendar.GetUI().totals.month.value"))
    cal_shift_month(lua, 1)
    if cal_text(lua, "GDL.Calendar.GetUI().totals.month.value") != cal_money_icon(2000000):
        problems.append("翻到 12 月后顶栏本月读数应换成 12 月那笔：%r"
                        % cal_text(lua, "GDL.Calendar.GetUI().totals.month.value"))
    st = cal_state(lua)
    if (st["startKey"], st["endKey"], st["selKey"]) != ("2026-11-05", "2026-11-05", "2026-11-05"):
        problems.append("翻月不该把右栏范围/选中日带走：%s" % st)
    if cal_eval(lua, "GDL.Calendar.GetUI().btnNextMo:GetText()") != ">":
        problems.append("换月按钮文本丢了")
    if cal_eval(lua, "GDL.Calendar.GetUI().btnPrevMo:GetText()") != "<":
        problems.append("上一月按钮文本丢了")
    return expect(name, problems)


def test_calendar_range_tabs_mark_active():
    """2026-11-13：本日 / 本周 / 本月 从三颗普通按钮改成页签 —— 原先看不出当前是哪一档。
    任何时刻恰好一格选中：更高 + 暖底 + 金字 + 底沿金线；其余矮一截 + 冷灰底 + 灰字 + 无金线，
    三格底沿钉在同一条基线上。悬停只亮未选中那格，绝不冒充选中；点日号 / 重开窗回「本日」；
    换语言只换字、不动选中态。范围口径一个字不改（仍由 GetPanelRange 算）。"""
    name = "calendar_range_tabs_mark_active"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    cal_open(lua)
    problems = []
    tab_h, lip = cal_geom(lua, "TAB_H"), cal_geom(lua, "TAB_LIP")
    on_bg, off_bg, hover_bg = "0.17,0.15,0.09", "0.10,0.10,0.13", "0.14,0.14,0.18"
    on_fg, off_fg, hover_fg = "1.00,0.82,0.00", "0.62,0.62,0.62", "0.88,0.88,0.92"

    def check(want, why):
        tabs = cal_tabs(lua)
        if [t["preset"] for t in tabs] != ["day", "week", "month"]:
            problems.append("%s：页签不是 本日/本周/本月 三格：%s" % (why, tabs))
            return
        if cal_state(lua)["preset"] != want:
            problems.append("%s：档位应是 %s，实际 %s" % (why, want, cal_state(lua)["preset"]))
        if sum(1 for t in tabs if t["active"] == "1") != 1:
            problems.append("%s：选中态不是恰好一格：%s"
                            % (why, [(t["preset"], t["active"]) for t in tabs]))
        for t in tabs:
            on = t["preset"] == want
            tag = "%s·%s" % (why, t["preset"])
            if t["active"] != ("1" if on else "0"):
                problems.append("%s：选中记号反了（%s）" % (tag, t["active"]))
            want_h = tab_h if on else tab_h - lip
            if as_number(t["h"]) != want_h:
                problems.append("%s：高应 %d，实际 %s" % (tag, want_h, t["h"]))
            if t["mark"] != ("1" if on else "0"):
                problems.append("%s：底沿金线该%s，实际 %s"
                                % (tag, "亮" if on else "灭", t["mark"]))
            if t["bg"] != (on_bg if on else off_bg):
                problems.append("%s：底色应是 %s，实际 %s"
                                % (tag, on_bg if on else off_bg, t["bg"]))
            if t["fg"] != (on_fg if on else off_fg):
                problems.append("%s：字色应是 %s，实际 %s"
                                % (tag, on_fg if on else off_fg, t["fg"]))
            if t["anchor"] != "BOTTOMLEFT" or as_number(t["y"]) != -tab_h:
                problems.append("%s：底沿没钉在基线上（锚=%s y=%s）" % (tag, t["anchor"], t["y"]))
            if t["wired"] != "1":
                problems.append("%s：点击 / 进出悬停没接全" % tag)
            if as_number(t["markH"]) != cal_geom(lua, "TAB_MARK_H"):
                problems.append("%s：金线厚度 %s" % (tag, t["markH"]))

    tabs = cal_tabs(lua)
    check("day", "开窗")
    step = cal_geom(lua, "PRESET_W") + cal_geom(lua, "PRESET_GAP")
    if [as_number(t["x"]) for t in tabs] != [0, step, 2 * step]:
        problems.append("三格没从左到右贴着排：%s" % [t["x"] for t in tabs])
    if [t["text"] for t in tabs] != ["本日", "本周", "本月"]:
        problems.append("页签字不对：%s" % [t["text"] for t in tabs])
    # 基线：一条压在三格底沿下的横线，宽度就是整条页签
    rule_w = as_number(cal_eval(lua, "GDL.Calendar.GetUI().presetRule:GetWidth()"))
    rule_y = as_number(cal_eval(lua, "GDL.Calendar.GetUI().presetRule.__points[1][5]"))
    if rule_w != cal_geom(lua, "PRESET_STRIP_W") or rule_y != -tab_h:
        problems.append("页签基线宽 %s / 位置 %s 不对" % (rule_w, rule_y))

    cal_click(lua, "GDL.Calendar.GetUI().presets.week")
    check("week", "点本周")
    cal_click(lua, "GDL.Calendar.GetUI().presets.month")
    check("month", "点本月")

    # 悬停未选中那格：只亮底与字，高矮 / 金线 / 选中记号一个都不许变
    cal_script(lua, "GDL.Calendar.GetUI().presets.day", "OnEnter")
    tabs = {t["preset"]: t for t in cal_tabs(lua)}
    if (tabs["day"]["bg"], tabs["day"]["fg"]) != (hover_bg, hover_fg):
        problems.append("悬停未选中格没亮底亮字：%s / %s" % (tabs["day"]["bg"], tabs["day"]["fg"]))
    if (tabs["day"]["active"], tabs["day"]["mark"], as_number(tabs["day"]["h"])) \
            != ("0", "0", tab_h - lip):
        problems.append("悬停冒充了选中态：%s" % tabs["day"])
    if (tabs["month"]["bg"], tabs["month"]["fg"], tabs["month"]["mark"]) \
            != (on_bg, on_fg, "1"):
        problems.append("悬停别的格把选中那格也改了：%s" % tabs["month"])
    cal_script(lua, "GDL.Calendar.GetUI().presets.day", "OnLeave")
    check("month", "鼠标离开")
    # 悬停选中那格：不许被灰字灰底盖掉
    cal_script(lua, "GDL.Calendar.GetUI().presets.month", "OnEnter")
    tabs = {t["preset"]: t for t in cal_tabs(lua)}
    if (tabs["month"]["bg"], tabs["month"]["fg"], tabs["month"]["mark"],
            as_number(tabs["month"]["h"])) != (on_bg, on_fg, "1", tab_h):
        problems.append("悬停把选中那格刷成了未选中：%s" % tabs["month"])
    cal_script(lua, "GDL.Calendar.GetUI().presets.month", "OnLeave")
    check("month", "离开选中格")

    # 点别的日子 ⇒ 档位回「本日」，页签跟着回第一格
    cal_click(lua, "GDL.Calendar.GetUI().cells[5].host")
    check("day", "点日号")
    # 换语言：字换成英文、选中态不动；英文最长那格也不许撑破页签
    lua.globals()["GDL"].Locale.Set("enUS")
    cal_click(lua, "GDL.Calendar.GetUI().presets.month")
    check("month", "英文档")
    tabs = cal_tabs(lua)
    if [t["text"] for t in tabs] != ["Day", "This Week", "Month"]:
        problems.append("英文档页签字不对：%s" % [t["text"] for t in tabs])
    for t in tabs:
        if as_number(t["textW"]) > cal_geom(lua, "PRESET_W"):
            problems.append("英文 %r 量宽 %s 撑破了页签宽 %d"
                            % (t["text"], t["textW"], cal_geom(lua, "PRESET_W")))
    lua.globals()["GDL"].Locale.Set("zhCN")
    # 关窗再开：回「本日 + 今天」，页签不许留着上次的选中态
    lua.execute("GDL.Calendar.Close()")
    cal_open(lua)
    check("day", "重开窗")
    if cal_state(lua)["errors"] != 0:
        problems.append("页签重画报错：%s" % cal_eval(lua, "GDL.Calendar.GetState().errors[1]"))
    return expect(name, problems)


def test_calendar_top_totals_and_missing_clock():
    """顶栏三个读数是查表结果；墙上钟整个读不出时窗照样开、读数降级成 "?"、不许报错。"""
    name = "calendar_top_totals_and_missing_clock"
    lua = calendar_env()
    add_entry(lua, 200000, "quest")
    set_epoch(lua, ep(2026, 10, 5))
    add_entry(lua, 300000, "loot")
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    problems = []
    got = {p: cal_text(lua, "GDL.Calendar.GetUI().totals.%s.value" % p)
           for p in ("week", "month", "all")}
    if got["week"] != cal_money_icon(200000):
        problems.append("本周CD 读数 %r 应为 %r" % (got["week"], cal_money_icon(200000)))
    if got["month"] != cal_money_icon(200000):
        problems.append("本月读数 %r 只该含 11 月那笔" % got["month"])
    if got["all"] != cal_money_icon(500000):
        problems.append("全部读数 %r 应为 %r" % (got["all"], cal_money_icon(500000)))
    # 搬钱不进任何总量
    add_entry(lua, -150000, "warband")
    if cal_text(lua, "GDL.Calendar.GetUI().totals.all.value") != cal_money_icon(500000):
        problems.append("号间搬钱被算进了总读数")
    if cal_text(lua, "GDL.Calendar.GetUI().totals.week.label") != "本周":
        problems.append("读数标签丢了：%r" % cal_text(lua, "GDL.Calendar.GetUI().totals.week.label"))
    # 没墙上钟：全新环境、开窗走的就是"TodayKey 全程读不出"那一路
    lua3 = calendar_env()
    set_clock_none(lua3)
    cal_open(lua3)
    st = cal_state(lua3)
    if st["errors"] != 0:
        problems.append("没墙上钟时开窗报错：%d 次" % st["errors"])
    if cal_text(lua3, "GDL.Calendar.GetUI().totals.week.value") != "?":
        problems.append("没墙上钟时周读数应降级成 ?，实际 %r"
                        % cal_text(lua3, "GDL.Calendar.GetUI().totals.week.value"))
    if st["startKey"] is not None:
        problems.append("没墙上钟却编出了范围键：%s" % st["startKey"])
    if not cal_eval(lua3, "GDLCalFrame:IsShown()"):
        problems.append("没墙上钟时窗开不起来")
    if not cal_eval(lua3, "GDL.Calendar.GetUI().empty:IsShown()"):
        problems.append("没墙上钟时右栏没落到空态")
    return expect(name, problems)


def test_calendar_tooltip_only_in_month_cells():
    """格子悬停 = 日期 + 收入 / 支出 + 几个号有变动，四行整整齐齐；
    净不再进提示（格子上已经写着净），也不给来源明细（那属于点进去的弹窗）。
    月外格什么都不设，且必须先把上一格的提示收掉（挂着不撤就是"提示赖在脸上"）。"""
    name = "calendar_tooltip_only_in_month_cells"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    set_char(lua, "甲")
    add_entry(lua, 250000, "vendor")
    set_char(lua, "乙")
    add_entry(lua, -50000, "repair")
    set_char(lua, "当前号")
    cal_open(lua)
    cal_script(lua, "GDL.Calendar.GetUI().cells[10].host", "OnEnter")
    st = cal_state(lua)
    problems = []
    lines = cal_tip_frame_lines(lua)
    if not st["tipShown"]:
        problems.append("月内格悬停没把提示标成已弹")
    if len(lines) != 4:
        problems.append("提示应是 4 行（日期/收/支/N 个号），实际 %d 行：%s" % (len(lines), lines))
    else:
        if lines[0] != "2026-11-10":
            problems.append("第一行应是日期键，实际 %r" % lines[0])
        if lines[1] != "收入 %s" % cal_money_icon(1250000):
            problems.append("收入行 %r" % lines[1])
        if lines[2] != "支出 %s" % cal_money_icon(50000):
            problems.append("支出行 %r" % lines[2])
        if lines[3] != "3 个号有变动":
            problems.append("号数行 %r 应为「3 个号有变动」" % lines[3])
        for ln in lines:
            if "净" in ln:
                problems.append("提示里不该再写净（格子上已经有了）：%r" % ln)
            if any(word in ln for word in ("卖店", "修理", "拍卖行", "任务", "拾取", "交易")):
                problems.append("提示里出现了来源明细（那属于点进去那页）：%r" % ln)
    cal_script(lua, "GDL.Calendar.GetUI().cells[30].host", "OnLeave")
    if cal_state(lua)["tipShown"]:
        problems.append("OnLeave 没收提示")
    # 月外格：先弹月内格，再直接悬到月外格（客户端不保证先发 OnLeave），提示必须被收掉
    cal_script(lua, "GDL.Calendar.GetUI().cells[10].host", "OnEnter")
    cal_script(lua, "GDL.Calendar.GetUI().cells[31].host", "OnEnter")
    st = cal_state(lua)
    if st["tipShown"]:
        problems.append("月外格把提示标成了已弹")
    if cal_eval(lua, "GDL.Calendar.GetUI().tip:IsShown()") is True:
        problems.append("从月内格直接移到月外格，上一格的提示还挂在脸上")
    # 提示 Hide 后行还留在控件里是正常行为（不 Show 就看不见）：
    # 这里要钉的是月外格**一行都不新画**（行数还是上一格那 4 行）。
    if len(cal_tip_frame_lines(lua)) != 4:
        problems.append("月外格改写了提示行（该只收不画）：%s" % cal_tip_frame_lines(lua))
    # 提示改成自绘框后不依赖 GameTooltip：把它整个置 nil 也照样弹得出来
    lua.execute("GameTooltip = nil")
    cal_script(lua, "GDL.Calendar.GetUI().cells[10].host", "OnEnter")
    st = cal_state(lua)
    if not st["tipShown"] or len(cal_tip_frame_lines(lua)) != 4:
        problems.append("GameTooltip 缺席时自绘提示框没弹出来：%s" % cal_tip_frame_lines(lua))
    if st["errors"] != 0:
        problems.append("GameTooltip 缺席把绘制带崩了：%d 次" % st["errors"])
    return expect(name, problems)


def test_calendar_esc_list_and_close_button_level():
    """ESC 名单里存的是帧的**全局名**（客户端拿它去 _G 找），重复开窗不许挂两遍；
    × 用 UIPanelCloseButton 模板（自带 frameLevel=510），必须钉回主帧 +1。"""
    name = "calendar_esc_list_and_close_button_level"
    lua = calendar_env()
    cal_open(lua)
    slash(lua, "open")          # 关
    cal_open(lua)               # 再开：不许重建、也不许把名字再塞一遍名单
    problems = []
    names = cal_eval(lua, "(function() local n = 0 for i = 1, #UISpecialFrames do "
                          "if UISpecialFrames[i] == 'GDLCalFrame' then n = n + 1 end end return n end)()")
    if as_number(names) != 1:
        problems.append("UISpecialFrames 里 GDLCalFrame 出现 %s 次（应为 1）" % names)
    if cal_eval(lua, "GDLCalFrame == nil") is not False:
        problems.append("全局名 GDLCalFrame 没解析到帧（ESC 名单靠它找窗）")
    lvl = as_number(cal_eval(lua, "GDL.Calendar.GetUI().close:GetFrameLevel()"))
    main = as_number(cal_eval(lua, "GDLCalFrame:GetFrameLevel()"))
    if lvl != main + 1:
        problems.append("× 的层号 %s 应 = 主帧 %s + 1（模板自带 510 会浮到别人窗子上面）" % (lvl, main))
    if main == 510:
        problems.append("主帧层号被判成模板的 510")
    cal_click(lua, "GDL.Calendar.GetUI().close")
    if cal_eval(lua, "GDLCalFrame:IsShown()"):
        problems.append("点 × 没关窗")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_probe_ui_is_read_only():
    """`/gdl probe ui` 只读取证：常量、周 CD 来源与退化原因、帧几何、每格矩形、
    字体量宽、提示行宽、× 层号、绘制报错计数都要报，且一条都不改界面状态；
    `/gdl probe`（记账层取证）不许被这条链挡掉。"""
    name = "calendar_probe_ui_is_read_only"
    lua = calendar_env()
    add_entry(lua, 500000, "quest")
    cal_open(lua)
    cal_script(lua, "GDL.Calendar.GetUI().cells[10].host", "OnEnter")
    before = cal_state(lua)
    lines = slash(lua, "probe ui")
    joined = "\n".join(lines)
    after = cal_state(lua)
    problems = []
    for want in ("界面几何取证", "常量 WIN_W=", "周CD接口", "帧 GDLCalFrame",
                 "格10 ", "右栏 滚动区高=", "字体 角色=", "提示行1", "绘制没报错"):
        if want not in joined:
            problems.append("probe ui 少了 %r：%s" % (want, joined[:400]))
    if "提示行4" not in joined or "个号有变动" not in joined:
        problems.append("probe ui 没报四行提示的宽度证据")
    for k in ("preset", "startKey", "endKey", "selKey", "panelCount", "scrollY", "tipShown"):
        if before[k] != after[k]:
            problems.append("probe ui 改了状态 %s：%s → %s" % (k, before[k], after[k]))
    money_lines = slash(lua, "probe")
    if not any("记账层依赖的接口取证" in ln for ln in money_lines):
        problems.append("/gdl probe 被界面链吃掉了我：%s" % money_lines)
    # 没开窗也不许报错
    lua2 = calendar_env()
    lines2 = slash(lua2, "probe ui")
    if not any("窗口还没建" in ln for ln in lines2):
        problems.append("没开窗时 probe ui 没指路：%s" % lines2)
    if cal_state(lua2)["errors"] != 0:
        problems.append("没开窗跑 probe ui 把绘制带崩了")
    return expect(name, problems)


def test_calendar_open_without_ledger_does_not_error():
    """账本模块缺席（T2 之前的降级形状）时界面不许崩：读数与格子一律空/问号。"""
    name = "calendar_open_without_ledger_does_not_error"
    lua = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")[0]
    lua.execute('GDL.modules["Ledger"] = nil GDL.Ledger = nil')
    set_epoch(lua, ep(2026, 11, 10))
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    fire(lua, "PLAYER_LOGIN")
    cal_open(lua)
    problems = []
    if cal_state(lua)["errors"] != 0:
        problems.append("Ledger 缺席时绘制报错 %d 次" % cal_state(lua)["errors"])
    if cal_text(lua, "GDL.Calendar.GetUI().totals.all.value") != "?":
        problems.append("Ledger 缺席时读数应是 ?，实际 %r"
                        % cal_text(lua, "GDL.Calendar.GetUI().totals.all.value"))
    if not cal_eval(lua, "GDLCalFrame:IsShown()"):
        problems.append("Ledger 缺席时窗开不起来")
    return expect(name, problems)


def test_locale_tables_are_two_language_complete():
    """键的纪律：zhCN 与 enUS 两张表键集相等，且每条串 %d/%s 占位符**序列**一致；
    界面里每一个 T("…")/Fmt("…") 的字面键（Calendar.lua 的窗口 + Minimap.lua 的悬停提示）、
    以及拼出来的动态家族（WD_1..7、RANGE_*、SRC_*）都必须在两张表里都点得出货。
    查表文件名单也要跟着界面长：漏一个文件 = 那个文件的键没人管，缺一种语言就没人报。"""
    name = "locale_tables_are_two_language_complete"
    lua, _ = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")
    problems = []

    def keys(code):
        v = cal_eval(lua, "(function() local n = {} for k in pairs(GDL.Locale.L['%s']) do "
                          "n[#n + 1] = k end table.sort(n) return table.concat(n, ',') end)()" % code)
        return set(str(v).split(","))

    zh, en = keys("zhCN"), keys("enUS")
    if zh != en:
        problems.append("两语言键集不等：只中文有 %s；只英文有 %s" % (sorted(zh - en), sorted(en - zh)))
    src = ""
    for fn in ("Calendar.lua", "Flows.lua", "Minimap.lua"):
        with open(os.path.join(PLUGIN_DIR, fn), encoding="utf-8") as fh:
            src += fh.read()
    used = set(k for k in re.findall(r'\b(?:T|Fmt)\("([A-Z][A-Z0-9_]+)"', src) if not k.endswith("_"))
    used |= set("WD_%d" % i for i in range(1, 8))
    used |= set("RANGE_%s" % p for p in ("DAY", "WEEK", "MONTH", "ALL"))
    used |= set("SRC_%s" % s.upper() for s in CAL_ALL_SOURCES)
    missing = sorted(k for k in used if k not in zh)
    if missing:
        problems.append("界面用了但中文表没有的键：%s" % missing)
    unused = sorted(k for k in zh if k not in used and not k.startswith("SRC_"))
    if unused:
        problems.append("表里留着但界面不再用的键：%s" % unused)

    def directives(code, key):
        v = cal_eval(lua, "GDL.Locale.L['%s'][%r]" % (code, key))
        if v is None:
            return None
        return re.findall(r"%[-+ #0-9.]*[dDs]", str(v))

    for key in sorted(zh & en):
        dz, de = directives("zhCN", key), directives("enUS", key)
        if dz != de:
            problems.append("%s 的占位符序列不等：中文 %s vs 英文 %s" % (key, dz, de))
    # 来源名全部有译名（不许回落到原始英文键）
    for src_name in CAL_ALL_SOURCES:
        for code in ("zhCN", "enUS"):
            lua.globals()["GDL"].Locale.Set(code)
            got = str(lua.globals()["GDL"].Locale.SrcName(src_name))
            if got == src_name:
                problems.append("%s 语言下来源 %s 没译名（原样回落）" % (code, src_name))
    lua.globals()["GDL"].Locale.Set("zhCN")
    return expect(name, problems)


def test_locale_values_use_only_font_safe_glyphs():
    """字形白名单：两张语言表的**值**只允许 可打印 ASCII + CJK + 中文标点。
    12.1.5 的中文字体没有 ‹ ›（U+2039/203A）这类字形，换月按钮会画成空白（用户实测报过）；
    这条把整类缺陷钉死：再往界面串里塞表外字符就 FAIL，并点出 语言.键名 + 码点。
    只扫表的值，不扫注释——注释里的 ⇒ § 「」 不上界面，不该被这条管。"""
    name = "locale_values_use_only_font_safe_glyphs"
    lua, _ = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")
    raw = cal_eval(lua, "(function() local out = {} "
                        "for _, code in ipairs({'zhCN', 'enUS'}) do "
                        "for k, v in pairs(GDL.Locale.L[code]) do "
                        "if type(v) == 'string' then out[#out + 1] = code .. '\\t' .. k .. '\\t' .. v end "
                        "end end return table.concat(out, '\\n') end)()")
    allowed_punct = set("，。：；、！？（）“”‘’—…·《》")
    problems = []
    for line in str(raw).split("\n"):
        if not line:
            continue
        code, key, value = line.split("\t", 2)
        for ch in value:
            o = ord(ch)
            if 0x20 <= o <= 0x7E or 0x4E00 <= o <= 0x9FFF or ch in allowed_punct:
                continue
            problems.append("%s.%s 里有字体可能没有的字形 U+%04X %r" % (code, key, o, ch))
    return expect(name, problems)


def test_locale_selection_and_fallbacks():
    """只留中英两档：认得的代码切过去，其它（含 nil / "auto" / 拼错）回落默认档不报错；
    切语言后界面词跟着变，取证措辞不受影响。"""
    name = "locale_selection_and_fallbacks"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")                         # 今天这格要有净
    add_entry(lua, -50000, "vendor")
    cal_open(lua)
    problems = []
    loc = lua.globals()["GDL"].Locale
    if str(loc.GetLocale()) != "zhCN":
        problems.append("默认档应为 zhCN，实际 %s" % loc.GetLocale())
    if str(loc.Set("deDE")) != "zhCN":
        problems.append("没证的语种代码应回落默认档，实际 %s" % loc.Set("deDE"))
    if str(loc.ApplySetting("auto")) != "zhCN":
        problems.append('"auto" 应落默认档（T4 才接自动判定）')
    if str(loc.ApplySetting("enUS")) != "enUS":
        problems.append("ApplySetting(enUS) 没切过去")
    cal_script(lua, "GDL.Calendar.GetUI().presets.day", "OnClick")   # 重画一遍
    if cal_text(lua, "GDL.Calendar.GetUI().title") != "Gold Daily Ledger":
        problems.append("英文档标题 %r" % cal_text(lua, "GDL.Calendar.GetUI().title"))
    # 只在 Build 里摆一次的静态字（右栏列头 / 三档 / 星期表头 / 顶栏读数）换档后必须跟着变
    for expr, want in (("presets.day.label", "Day"), ("presets.week.label", "This Week"),
                       ("charHead.name", "Character"), ("charHead.net", "Net"),
                       ("weekdays[1]", "Sun"), ("totals.all.label", "All")):
        got = cal_text(lua, "GDL.Calendar.GetUI().%s" % expr)
        if got != want:
            problems.append("英文档 %s 应是 %r，实际 %r" % (expr, want, got))
    # 格子小字与它的 CELL_SUB 串已下线，不该还有第三条字串控件
    if cal_eval(lua, "GDL.Calendar.GetUI().cells[10].sub") is not None:
        problems.append("格子里还有收·支小字控件（应整行下线）")
    loc.Set("zhCN")
    cal_script(lua, "GDL.Calendar.GetUI().presets.day", "OnClick")   # 再重画一遍
    for expr, want in (("presets.day.label", "本日"), ("presets.week.label", "本周"),
                       ("charHead.name", "号名"), ("charHead.net", "净"),
                       ("totals.all.label", "全部")):
        got = cal_text(lua, "GDL.Calendar.GetUI().%s" % expr)
        if got != want:
            problems.append("切回中文档 %s 应是 %r，实际 %r" % (expr, want, got))
    if str(loc.Get("NO_SUCH_KEY")) != "NO_SUCH_KEY":
        problems.append("缺串时应回键本身，实际 %s" % loc.Get("NO_SUCH_KEY"))
    if cal_state(lua)["errors"] != 0:
        problems.append("切语言把绘制带崩了")
    return expect(name, problems)


def test_locale_detect_reads_realm_bytes_not_text_locale():
    """判国服不能只看 GetLocale()（那是文本语言，国服客户端能回 enUS）：
    服名里有 CJK 字节才算中文（GoldLedger 那条补丁 b371a91 的同一判据）。"""
    name = "locale_detect_reads_realm_bytes_not_text_locale"
    lua, _ = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")
    loc = lua.globals()["GDL"].Locale
    problems = []
    if not bool(cal_eval(lua, "GDL.Locale.RealmHasCJK('暴龙王卡尔波斯')")):
        problems.append("国服服名没判出 CJK")
    if bool(cal_eval(lua, "GDL.Locale.RealmHasCJK('Stormrage')")):
        problems.append("英文服名被判成 CJK")
    if bool(cal_eval(lua, "GDL.Locale.RealmHasCJK(nil)")):
        problems.append("nil 服名被判成 CJK")
    # GetLocale = enUS 但服名是中文 ⇒ 仍判中文（上一轮就是这么修的）
    lua.execute("GetLocale = function() return 'enUS' end")
    lua.execute("GetRealmName = function() return '暴龙王卡尔波斯' end")
    if str(loc.Detect()) != "zhCN":
        problems.append("文本语言 enUS + 中文服名应判 zhCN，实际 %s" % loc.Detect())
    lua.execute("GetRealmName = function() return 'Stormrage' end")
    if str(loc.Detect()) != "enUS":
        problems.append("两个信号都是英文时应判 enUS，实际 %s" % loc.Detect())
    lua.execute("GetLocale = function() return 'zhCN' end")
    lua.execute("GetRealmName = function() return 'Stormrage' end")
    if str(loc.Detect()) != "zhCN":
        problems.append("GetLocale 回 zhCN 时该直接判中文")
    lua.execute("GetLocale = nil GetRealmName = nil")
    if cal_eval(lua, "GDL.Locale.Detect()") is not None:
        problems.append("两个信号都读不出时应回 nil（保持默认档）")
    return expect(name, problems)


# ---------------------------------------------------------------------------
# T4 夹具：语言判定 + 自带图标 + 小地图按钮。口径与上面一致 —— 事件从 GDL_FireEvent 进、
# 命令从 SlashCmdList 进、按钮上的脚本用 GDL_CallScript 调（没挂着就回 false，
# 那个 false 本身就是"空闲没有每帧脚本"的判据）；用例一行都不往生产状态里写。
# ---------------------------------------------------------------------------

RING_RADIUS = 89.0      # 替身宿主 160 宽 / 按钮 26 宽 ⇒ 160/2 + 26/2 - 4（算式另算一遍，不抄生产代码）
DEFAULT_ANGLE = 200     # Minimap.lua 的默认角度（用例要靠"没回到这一档"来证明读的是存档）


def t4_env(realm="暴龙王卡尔波斯", text_locale=None, login=True, host=True):
    """把两个语言信号与小地图宿主按参数摆好，再照真客户端顺序 boot。
    login=False 停在 ADDON_LOADED —— 判定时序那一条要的就是这一档。"""
    lua = ledger_env()
    set_char(lua, "当前号")
    set_epoch(lua, ep(2026, 11, 10))
    lua.execute("GDL_REALM_NAME = %s" % (repr(realm) if realm is not None else "nil"))
    lua.execute("GetLocale = %s"
                % (("function() return %s end" % repr(text_locale)) if text_locale else "nil"))
    if not host:
        lua.execute("Minimap = nil")
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    if login:
        fire(lua, "PLAYER_LOGIN")
    return lua


def lang(lua):
    return str(cal_eval(lua, "GDL.Locale.GetLocale()"))


def decision(lua):
    """(来源, 用的档, 依据)：判定记录只读，界面一个字都不显示它。"""
    d = cal_eval(lua, "GDL.Locale.GetDecision()")
    if d is None:
        return None, None, None
    return str(d.source), str(d.code), None if d.reason is None else str(d.reason)


def stored_lang(lua):
    v = cal_eval(lua, "GDLDB and GDLDB.settings and GDLDB.settings.language")
    return None if v is None else str(v)


def stored_angle(lua):
    return cal_eval(lua, "GDLDB and GDLDB.settings and GDLDB.settings.minimapAngle")


def set_cursor(lua, x, y):
    lua.execute("GDL_CURSOR = { %d, %d }" % (int(x), int(y)))


def mm_script(lua, name):
    """调按钮上挂着的脚本；没挂着回 False。"""
    return bool(lua.globals()["GDL_CallScript"](cal_eval(lua, "GDLMapButton"), name))


def mm_point(lua):
    """按钮最后一次 SetPoint 的形状 "锚点|对端锚点|dx|dy"。
    偏移按 %.3f 比：lupa 回 92.0、真客户端回 92，整数/浮点写法不该左右结论。"""
    v = cal_eval(lua, r"""(function()
        local p = GDLMapButton and GDLMapButton.__points
        if not p or #p == 0 then return nil end
        local last = p[#p]
        return ("%s|%s|%.3f|%.3f"):format(tostring(last[1]), tostring(last[3]),
                                          tonumber(last[4]) or -1, tonumber(last[5]) or -1)
    end)()""")
    return None if v is None else str(v)


def tip_texts(lua):
    tips = lua.globals()["GDL_TIP_LINES"]
    out, i = [], 1
    while True:
        row = tips[i]
        if row is None:
            break
        out.append(str(row.text))
        i += 1
    return out


def toc_icon_path():
    m = re.search(r"^##\s*IconTexture:\s*(\S+)\s*$", "\n".join(toc_lines()), re.M)
    return None if m is None else m.group(1)


def test_locale_auto_detect_wired_at_login():
    """判定挂在 PLAYER_LOGIN 而不是 ADDON_LOADED：那会儿 GetRealmName 还答不出（真机时序），
    提前判等于把"读不出"当结论；判出来的档也一律不许写进存档 —— 没选过就得每次登录重新判，
    写档等于把某一次登录看到的服永久当成玩家的决定。两个信号都没答时要把"为什么判不出"记下来。"""
    name = "locale_auto_detect_wired_at_login"
    problems = []
    lua = t4_env(realm=None, login=False)
    if lang(lua) != "zhCN":
        problems.append("ADDON_LOADED 阶段该吃默认档 zhCN，实际 %s" % lang(lua))
    if decision(lua)[0] != "init":
        problems.append("还没登录时判定来源该记 init，实际 %s" % (decision(lua),))
    if stored_lang(lua) is not None:
        problems.append("还没登录就往存档写了语言：%s" % stored_lang(lua))
    # 登录：这一刻服名才答得出 ⇒ 判定在这里发生，判成英文服
    lua.execute("GDL_REALM_NAME = 'Stormrage'")
    fire(lua, "PLAYER_LOGIN")
    if lang(lua) != "enUS":
        problems.append("PLAYER_LOGIN 后该按服名判成 enUS，实际 %s" % lang(lua))
    src, _, why = decision(lua)
    if src != "auto":
        problems.append("判定来源该记 auto，实际 %s" % src)
    if not why or "服名" not in why:
        problems.append("依据没记下是谁定的（/gdl probe 就念不出）：%r" % why)
    if stored_lang(lua) is not None:
        problems.append("自动判定的结果被写进存档了：%s（没选过就得每次重新判）" % stored_lang(lua))
    # 两个信号都问不出：吃默认档 + 依据写清为什么，不许猜一个交差
    lua2 = t4_env(realm=None, text_locale=None)
    src2, code2, why2 = decision(lua2)
    if lang(lua2) != "zhCN" or src2 != "default":
        problems.append("两个信号都没答时该吃默认档并记 default，实际 %s / %s" % (lang(lua2), src2))
    if not why2 or "两个信号" not in why2:
        problems.append("判不出时依据要写清为什么判不出：%r" % why2)
    return expect(name, problems)


def test_locale_login_reads_realm_bytes_and_choice_wins():
    """判国服看的是服名字节不是文本语言（国服客户端能回 enUS —— b371a91 那条补丁的同一判据）；
    玩家用 /gdl lang 选过的压过判定，而且二次登录（/reload 口径）还得是他选的那档。"""
    name = "locale_login_reads_realm_bytes_and_choice_wins"
    problems = []
    lua = t4_env(realm="暴龙王卡尔波斯", text_locale="enUS")
    if lang(lua) != "zhCN":
        problems.append("文本语言 enUS + 中文服名应判成 zhCN，实际 %s" % lang(lua))
    if "字节" not in (decision(lua)[2] or ""):
        problems.append("依据应写明是服名字节定的：%r" % decision(lua)[2])
    lua2 = t4_env(realm="Stormrage", text_locale="enUS")
    if lang(lua2) != "enUS":
        problems.append("两个信号都是英文该判 enUS，实际 %s" % lang(lua2))
    lua3 = t4_env(realm="Stormrage", login=False)
    lua3.execute("GDLDB.settings.language = 'zhCN'")
    fire(lua3, "PLAYER_LOGIN")
    if lang(lua3) != "zhCN" or decision(lua3)[0] != "chosen":
        problems.append("存档选过的该压过判定并记 chosen，实际 %s / %s"
                        % (lang(lua3), decision(lua3)[0]))
    fire(lua3, "PLAYER_LOGIN")
    if lang(lua3) != "zhCN" or decision(lua3)[0] != "chosen":
        problems.append("二次登录后选择被判定顶掉了：%s / %s" % (lang(lua3), decision(lua3)[0]))
    if stored_lang(lua3) != "zhCN":
        problems.append("选过的语言没留在存档：%s" % stored_lang(lua3))
    return expect(name, problems)


def test_gcl_lang_stores_applies_and_repaints():
    """/gdl lang zhCN|enUS：写存档 + 当场换字（窗口开着、不重开就得全变），
    空参数那一行要念得出"用的是哪档、是谁定的"。换档只改内部状态、等人重开才生效
    是这类改动最容易漏的一半（T3 那一轮真漏过一次半中半英）。"""
    name = "gcl_lang_stores_applies_and_repaints"
    lua = t4_env()
    cal_open(lua)
    problems = []
    if cal_text(lua, "GDL.Calendar.GetUI().title") != "金币日记账":
        problems.append("中文服登录后标题该是中文：%r" % cal_text(lua, "GDL.Calendar.GetUI().title"))
    lines = slash(lua, "lang enUS")
    if any("出错" in ln for ln in lines):
        problems.append("/gdl lang enUS 报错：%s" % lines)
    if lang(lua) != "enUS":
        problems.append("界面语言没切过去：%s" % lang(lua))
    if stored_lang(lua) != "enUS":
        problems.append("没写进存档 settings.language：%s" % stored_lang(lua))
    if decision(lua)[0] != "chosen":
        problems.append("玩家选的该记 chosen，实际 %s" % decision(lua)[0])
    for expr, want in (("GDL.Calendar.GetUI().title", "Gold Daily Ledger"),
                       ("GDL.Calendar.GetUI().presets.day.label", "Day"),
                       ("GDL.Calendar.GetUI().charHead.name", "Character"),
                       ("GDL.Calendar.GetUI().weekdays[1]", "Sun")):
        if cal_text(lua, expr) != want:
            problems.append("窗还开着却没重画 %s：应为 %r 实际 %r" % (expr, want, cal_text(lua, expr)))
    if cal_state(lua)["errors"] != 0:
        problems.append("换语言把绘制画崩了")
    joined = "\n".join(slash(lua, "lang"))
    if "enUS" not in joined or "你选的" not in joined:
        problems.append("空参数该念出当前档 + 谁定的：%s" % joined)
    slash(lua, "lang zhCN")
    if cal_text(lua, "GDL.Calendar.GetUI().title") != "金币日记账":
        problems.append("切回中文后标题 %r" % cal_text(lua, "GDL.Calendar.GetUI().title"))
    if stored_lang(lua) != "zhCN":
        problems.append("切回中文没写档：%s" % stored_lang(lua))
    # 存档还没建好（ADDON_LOADED 之前）也要能改，只是得说清"这一改没落盘"，不许报错也不许假装存上了
    lua2 = ledger_env()
    lines = slash(lua2, "lang enUS")
    if any("出错" in ln for ln in lines):
        problems.append("没存档时 /gdl lang 报错：%s" % lines)
    if lang(lua2) != "enUS":
        problems.append("没存档时界面语言没切过去：%s" % lang(lua2))
    if not any("只在本场会话" in ln for ln in lines):
        problems.append("没存上却要吭一声，不许假装写进档了：%s" % lines)
    return expect(name, problems)


def test_gcl_lang_auto_clears_choice_and_bad_code_is_rejected():
    """/gdl lang auto = 清掉选择并**当场**重新判定（判出的档仍不落盘）；
    认错的代码一律当场回绝、把能写的值念出来，存档与界面一个字节都不许动 ——
    玩家打的字没生效又不吭声，比报错更难查（用户的硬规矩）。"""
    name = "gcl_lang_auto_clears_choice_and_bad_code_is_rejected"
    lua = t4_env(realm="Stormrage")
    slash(lua, "lang zhCN")
    problems = []
    if stored_lang(lua) != "zhCN":
        problems.append("前置没把 zhCN 存进档：%s" % stored_lang(lua))
    lines = slash(lua, "lang auto")
    if any("出错" in ln for ln in lines):
        problems.append("/gdl lang auto 报错：%s" % lines)
    if stored_lang(lua) is not None:
        problems.append("auto 没把选择清掉：存档还剩 %s" % stored_lang(lua))
    if lang(lua) != "enUS" or decision(lua)[0] != "auto":
        problems.append("auto 该当场按英文服重判成 enUS，实际 %s / %s" % (lang(lua), decision(lua)[0]))
    if stored_lang(lua) is not None:
        problems.append("auto 把判出来的档又写回存档了：%s" % stored_lang(lua))
    # 换了服名再问一次 auto：跟着重新判，而不是留着上一次的结论
    lua.execute("GDL_REALM_NAME = '暴龙王卡尔波斯'")
    slash(lua, "lang auto")
    if lang(lua) != "zhCN" or stored_lang(lua) is not None:
        problems.append("换服后 auto 没重判：%s / 存档 %s" % (lang(lua), stored_lang(lua)))
    for bad in ("deDE", "中文", "zhCNx"):
        before = (lang(lua), stored_lang(lua))
        lines = slash(lua, "lang " + bad)
        joined = "\n".join(lines)
        if "不认识" not in joined:
            problems.append("%r 该被当场回绝：%s" % (bad, lines))
        for want in ("zhCN", "enUS", "auto"):
            if want not in joined:
                problems.append("回绝那行要把能写的值念出来（%s 没出现）：%s" % (want, lines))
        if (lang(lua), stored_lang(lua)) != before:
            problems.append("%r 被拒后还动了界面/存档：%s → %s"
                            % (bad, before, (lang(lua), stored_lang(lua))))
    return expect(name, problems)


def test_probe_reports_t4_language_and_art_evidence():
    """取证口径（"我们怎么知道的"只进 /gdl probe，一句不上窗口）：
    哪一路信号定的语言、图标两处路径一不一致、小地图宿主在不在与多大，都要念得出来；
    同时 /gdl probe 原有那几段（记账层）不许被新链吃掉，probe 本身一个状态都不改。"""
    name = "probe_reports_t4_language_and_art_evidence"
    lua = t4_env(realm="Stormrage", text_locale="enUS")
    before = decision(lua)
    joined = "\n".join(slash(lua, "probe"))
    problems = []
    if "记账层依赖的接口取证" not in joined:
        problems.append("记账层那段取证被新链吃掉了：%s" % joined[:300])
    if "语言 界面用=enUS 来源=auto" not in joined:
        problems.append("没报语言与定它的来源：%s" % joined)
    if "GetLocale=enUS" not in joined or "GetRealmName=Stormrage" not in joined:
        problems.append("没报两个信号各答了什么：%s" % joined)
    icon = toc_icon_path()
    if "按钮用的=" + icon not in joined:
        problems.append("没报按钮用的图标路径：%s" % joined)
    if "随包 tga 在不在 Lua 读不出" not in joined:
        problems.append("素材在不在客户端读不出要老实说，不许猜：%s" % joined)
    if "小地图宿主 Minimap=有 尺寸=160x160" not in joined:
        problems.append("没报宿主在不在与尺寸：%s" % joined)
    if "按钮=GDLMapButton" not in joined or "空闲每帧脚本=无" not in joined:
        problems.append("没报按钮现状与每帧脚本：%s" % joined)
    if decision(lua) != before:
        problems.append("probe 改了判定记录：%s → %s" % (before, decision(lua)))
    # toc 那边读得到声明时，两处一致/不一致要直接念出来（C_AddOns 缺席时也要念得出缺席原因）
    lua.execute(r"""C_AddOns = { GetAddOnMetadata = function(name, field)
        if name == GDL.addonName and field == "IconTexture" then
            return "Interface\\AddOns\\GoldDailyLedger\\GoldDailyLedger"
        end
        return nil
    end }""")
    joined = "\n".join(slash(lua, "probe"))
    if "toc声明的=" + icon not in joined or "两处一致" not in joined:
        problems.append("读得到 toc 声明却没对照：%s" % joined)
    lua2 = t4_env(host=False)
    joined2 = "\n".join(slash(lua2, "probe"))
    if "小地图宿主 Minimap=没有这个全局" not in joined2:
        problems.append("宿主缺席时没念出降级：%s" % joined2)
    if "出错" in joined2:
        problems.append("宿主缺席时 probe 报错：%s" % joined2)
    return expect(name, problems)


def test_bundled_icon_tga_is_shipped_and_valid():
    """素材要真躺在目录里、而且是客户端读得动的形状：路径写错 = 一片空白；
    空壳（本仓库就有一颗 18 字节头 + 全零、声明尺寸远大于实际字节数的）= 同样空白；
    铺满一个色 = 空白换了张脸。三种都只能等玩家在游戏里发现，所以离线全钉一遍。
    toc 里还不许出现两颗被否过的暴雪素材与兄弟插件那张清单图。"""
    name = "bundled_icon_tga_is_shipped_and_valid"
    import struct
    toc = "\n".join(toc_lines())
    declared = toc_icon_path()
    if declared is None:
        return fail(name, "toc 读不到 ## IconTexture 行")
    problems = []
    if not declared.lower().startswith("interface\\addons\\golddailyledger\\"):
        problems.append("图标路径要指回本插件目录，实际 %s" % declared)
    for banned in ("inv_misc_coin", "ui-questlog-bookicon", "goldquestledger"):
        if banned in toc.lower():
            problems.append("toc 里出现了 %s（金币=GoldLedger 在用、书本=BtWQuests 那族在用、"
                            "清单=兄弟插件自带美术）" % banned)
    tga = os.path.join(PLUGIN_DIR, declared.split("\\")[-1] + ".tga")
    if not os.path.exists(tga):
        return fail(name, "toc 指的素材没随插件发出：%s" % tga)
    with open(tga, "rb") as fh:
        d = fh.read()
    idlen, cmaptype, imgtype = struct.unpack("<BBB", d[:3])
    w, h = struct.unpack("<HH", d[12:16])
    bpp, desc = d[16], d[17]
    if (idlen, cmaptype, imgtype) != (0, 0, 2):
        problems.append("要未压缩真彩色（idlen=0 cmapType=0 imgType=2），实际 %s"
                        % ((idlen, cmaptype, imgtype),))
    if (bpp, desc) != (32, 0x08):
        problems.append("要 32bpp + descriptor 0x08（带 8 位 alpha、bottom-up），实际 %s"
                        % ((bpp, desc),))
    if w != h or not (64 <= w <= 512):
        problems.append("尺寸要正方形 64~512，实际 %dx%d" % (w, h))
    want = 18 + idlen + w * h * (bpp // 8)
    if len(d) != want:
        return fail(name, "素材是空壳：声明 %dx%d bpp%d 该有 %d 字节，实际 %d"
                    % (w, h, bpp, want, len(d)))
    body = d[18:]
    # 2026-11-11 起金币四周留呼吸边，文件头几行（= 图像底部几行）合法地全透明，
    # 不能再拿"开头一片全零"当空壳指纹；空壳本身已由上面的字节数对账拦住。
    # 改钉两点：圆心必须实心（金币在），不透明占比不许低过"圆面"的合理下界。
    mid = ((h // 2) * w + (w // 2)) * 4
    if body[mid + 3] != 255 or body[mid:mid + 3] == b"\x00\x00\x00":
        problems.append("圆心应是实心金币（alpha=255、非全零），实际 %s"
                        % (body[mid:mid + 4].hex(),))
    opaque = sum(1 for i in range(3, len(body), 4) if body[i] == 255)
    if opaque < w * h // 4:
        problems.append("不透明像素只占 %d/%d，圆面没画出来" % (opaque, w * h))
    rgb = [body[i:i + 3] for i in range(0, len(body), 4)]
    if len(set(rgb)) < 4:
        problems.append("像素只有 %d 种颜色（%s）—— 铺满一个色就是空白的另一张脸"
                        % (len(set(rgb)), sorted(set(rgb))))
    alphas = set(body[i] for i in range(3, len(body), 4))
    if max(alphas) != 255 or min(alphas) >= 255:
        problems.append("alpha 该有满档 255 也有透明（圆角抠图，四角必须透），实际 %s"
                        % sorted(alphas)[:6])
    # 圆形日历图标（2026-11-10 换：整块方图在小地图上比别的插件大一圈，用户反馈
    # "像计算器而且太大"）：四角必须全透明，圆心必须不透明。bottom-up 存储里
    # 首像素在左下角、末像素在右上角，各退半格取圆外/圆内的点。
    def px(x, y):
        return body[((h - 1 - y) * w + x) * 4 + 3]
    if px(1, 1) != 0 or px(w - 2, 1) != 0 or px(1, h - 2) != 0 or px(w - 2, h - 2) != 0:
        problems.append("四角该全透明（圆形图标），实际 %s/%s/%s/%s"
                        % (px(1, 1), px(w - 2, 1), px(1, h - 2), px(w - 2, h - 2)))
    if px(w // 2, h // 2) != 255:
        problems.append("圆心该不透明，实际 alpha=%s" % px(w // 2, h // 2))
    return expect(name, problems)


def test_addon_list_icon_and_minimap_icon_match():
    """图标两处：toc 的 ## IconTexture（插件列表）与小地图按钮的 normal texture。
    两处各写一份、改一处漏一处就是"列表一个图标、地图另一图标"（兄弟插件 2026-11-10 真踩过）。
    路径一律由 toc 名拼出：目录改名后按钮不许留在旧路径上画成空白。"""
    name = "addon_list_icon_and_minimap_icon_match"
    declared = toc_icon_path()
    if declared is None:
        return fail(name, "toc 读不到 ## IconTexture 行")
    lua = t4_env()
    normal = str(cal_eval(lua, "GDLMapButton.__normal"))

    def norm(p):
        return p.replace("\\", "/").lower()

    problems = []
    if norm(declared) != norm(normal):
        problems.append("两处图标不一致：toc=%s 按钮=%s" % (declared, normal))
    for taken in ("inv_misc_coin", "ui-questlog-bookicon"):
        if taken in norm(declared) or taken in norm(normal):
            problems.append("%s 是别的插件在用的图标，不许放回来" % taken)
    hl = str(cal_eval(lua, "GDLMapButton.__highlight"))
    if hl != "Interface\\Buttons\\UI-Common-MouseHilight":
        problems.append("高亮要用暴雪那颗圆形外圈（方形素材按下会又变方块），实际 %s" % hl)
    lua2 = t4_env(login=False)
    lua2.execute("GDL.addonName = '换个目录名'")
    fire(lua2, "PLAYER_LOGIN")
    renamed = str(cal_eval(lua2, "GDLMapButton.__normal"))
    if renamed != "Interface\\AddOns\\换个目录名\\换个目录名":
        problems.append("图标路径没跟着 GDL.addonName 走：%s（写死目录名改名后就是空白）" % renamed)
    return expect(name, problems)


def test_minimap_button_built_only_at_login():
    """按钮只在 PLAYER_LOGIN 建、且只建一次：加载期与 ADDON_LOADED 期一个帧都不许多造
    （那时宿主尺寸与角度都没处问，建了就是按默认值摆位）；层级要压过宿主否则被环上图标盖住；
    点击/悬停/拖动五样接线在建的那一刻就得齐，空闲状态不许挂着每帧脚本。"""
    name = "minimap_button_built_only_at_login"
    lua = ledger_env()                    # 只加载，事件一个都没跑
    set_char(lua, "当前号")
    set_epoch(lua, ep(2026, 11, 10))
    problems = []
    n0 = as_number(cal_eval(lua, "#GDL_FRAMES"))
    if cal_eval(lua, "GDLMapButton") is not None:
        problems.append("加载阶段就造了按钮")
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    if cal_eval(lua, "GDLMapButton") is not None:
        problems.append("ADDON_LOADED 就造了按钮：建按钮归 PLAYER_LOGIN")
    if as_number(cal_eval(lua, "#GDL_FRAMES")) != n0:
        problems.append("到 ADDON_LOADED 为止多造了帧：%s → %s" % (n0, cal_eval(lua, "#GDL_FRAMES")))
    lua.execute("Minimap.__framelevel = 500")
    fire(lua, "PLAYER_LOGIN")
    if cal_eval(lua, "GDLMapButton") is None:
        return expect(name, problems + ["PLAYER_LOGIN 之后按钮还没建"])
    if not bool(cal_eval(lua, "GDLMapButton.__parent == Minimap")):
        problems.append("按钮没挂在小地图宿主上")
    w = as_number(cal_eval(lua, "GDLMapButton:GetWidth()"))
    h = as_number(cal_eval(lua, "GDLMapButton:GetHeight()"))
    if as_number(w) != 26 or as_number(h) != 26:
        problems.append("按钮尺寸应为 26（用户 2026-11-11：32 偏大），实际 %sx%s" % (w, h))
    if as_number(cal_eval(lua, "GDLMapButton.__framelevel")) <= 500:
        problems.append("层级没压过宿主（环上插件一堆，会被盖住）")
    for script in ("OnClick", "OnEnter", "OnLeave", "OnDragStart", "OnDragStop"):
        if cal_eval(lua, "GDLMapButton:GetScript(%r)" % script) is None:
            problems.append("按钮没挂 %s" % script)
    if cal_eval(lua, "GDLMapButton:GetScript('OnUpdate')") is not None:
        problems.append("空闲状态就挂着每帧脚本（不许轮询）")
    if str(cal_eval(lua, "GDLMapButton.__drag")) != "LeftButton":
        problems.append("没 RegisterForDrag('LeftButton')：按钮拖不动")
    n1 = as_number(cal_eval(lua, "#GDL_FRAMES"))
    fire(lua, "PLAYER_LOGIN")                       # /reload 口径：不许造第二颗
    if as_number(cal_eval(lua, "#GDL_FRAMES")) != n1:
        problems.append("二次登录又造了一颗按钮：%s → %s" % (n1, cal_eval(lua, "#GDL_FRAMES")))
    if not bool(cal_eval(lua, "GDL.Minimap.GetButton() == GDLMapButton")):
        problems.append("模块上的按钮引用与全局帧不是同一个")
    return expect(name, problems)


def test_minimap_button_degrades_without_host():
    """取不到宿主（暴雪挪过小地图那一帧，12.x 理论上不会）：一个帧都不造、不许抛错，
    print 一条明说"窗口照旧用 /gdl open 打开"的降级；日志必须留痕，
    否则玩家只会看到"怎么没有按钮"而查不出原因。"""
    name = "minimap_button_degrades_without_host"
    lua = t4_env(login=False, host=False)
    n0 = as_number(cal_eval(lua, "#GDL_FRAMES"))
    fire(lua, "PLAYER_LOGIN")
    problems = []
    if cal_eval(lua, "GDLMapButton") is not None:
        problems.append("宿主没有却建了按钮（父帧都没处找）")
    if as_number(cal_eval(lua, "#GDL_FRAMES")) != n0:
        problems.append("降级路上还造了帧：%s → %s" % (n0, cal_eval(lua, "#GDL_FRAMES")))
    lines = printed_lines(lua)
    if any("出错" in ln for ln in lines):
        problems.append("宿主缺席把登录链报错了：%s" % lines)
    if not any("小地图按钮没建" in ln and "/gdl open" in ln for ln in lines):
        problems.append("没留一条指路的降级日志（玩家查不出原因）：%s" % lines)
    if cal_open(lua) is not True:
        problems.append("按钮降级之后 /gdl open 照样开不起来")
    if cal_state(lua)["errors"] != 0:
        problems.append("降级路上把绘制画崩了")
    return expect(name, problems)


def test_minimap_click_toggles_and_tooltip_is_player_facing():
    """点击 = 开/关日历窗（第二下必须关掉，不能越点越多）；悬停三行全是玩家要的
    （名字、点了会怎样、哪个版本），一句"接口读不出"这类证据都不许上提示，
    换档后提示也要跟着换字（每次现取，不留中文残句）。"""
    name = "minimap_click_toggles_and_tooltip_is_player_facing"
    lua = t4_env()
    problems = []
    if cal_eval(lua, "GDLCalFrame") is not None:
        problems.append("登录就把日历窗开出来了（按钮只该在点的时候开）")
    mm_script(lua, "OnClick")
    if not bool(cal_eval(lua, "GDLCalFrame:IsShown()")):
        problems.append("点按钮没把窗开起来")
    mm_script(lua, "OnClick")
    if bool(cal_eval(lua, "GDLCalFrame:IsShown()")):
        problems.append("第二下没把窗关掉（Toggle 写成了 Open）")
    mm_script(lua, "OnEnter")
    lines = tip_texts(lua)
    # 悬停形状（2026-11-11 玩家定）：标题 / 今日 / 昨日 / 本周（只显示净收入）/ 点击 / 版本。
    # 本环境没记账 → 三段都是 0 金；只写金币，不带银/铜。
    want_zero = cal_money_icon(0)
    if len(lines) != 6:
        problems.append("提示应六行（标题/今日/昨日/本周/点击/版本），实际 %d 行：%s"
                        % (len(lines), lines))
    else:
        if lines[0] != "金币日记账":
            problems.append("第一行该是玩家看得懂的名字：%r" % lines[0])
        want_periods = ("今日 " + want_zero, "昨日 " + want_zero, "本周 " + want_zero)
        if tuple(lines[1:4]) != want_periods:
            problems.append("净收入段不对：%s" % lines[1:4])
        if "点击打开" not in lines[4]:
            problems.append("点击说明该在收支段后面：%r" % lines[4])
        ver = str(lua.execute("return tostring(GDL.VERSION)"))
        if lines[5] != "v" + ver:
            problems.append("末行该念出版本（共享出去对方第一句就问这个）：%r" % lines[5])
    for banned in (ICON_SILVER, ICON_COPPER):
        if any(banned in ln for ln in lines):
            problems.append("小地图悬停不该出现银/铜：%r" % banned)
    for banned in ("接口", "读不出", "GetLocale", "Minimap"):
        if any(banned in ln for ln in lines):
            problems.append("提示里混进了取证措辞 %r：%s" % (banned, lines))
    if not bool(cal_eval(lua, "GameTooltip.__shown")):
        problems.append("悬停没把提示 Show 出来")
    mm_script(lua, "OnLeave")
    if bool(cal_eval(lua, "GameTooltip.__shown")):
        problems.append("移开没收提示")
    slash(lua, "lang enUS")
    mm_script(lua, "OnEnter")
    lines = tip_texts(lua)
    if not lines or lines[0] != "Gold Daily Ledger" \
            or not lines[1].startswith("Today ") \
            or not lines[2].startswith("Yesterday ") \
            or not lines[3].startswith("This Week ") \
            or "Click to open" not in lines[4]:
        problems.append("enUS 档下提示还是上一档的字：%s" % lines)
    return expect(name, problems)


def test_minimap_tooltip_net_today_yesterday_week():
    """2026-11-11：小地图悬停只统计净收入，展示今日 / 昨日 / 本周，且只写金币。
    今日净 = +500 -200 = 300 金；昨日 = -150 金；本周（含这两天）= 150 金。"""
    name = "minimap_tooltip_net_today_yesterday_week"
    lua = t4_env()
    problems = []
    add_entry(lua, 5000000, "quest")        # 今日 +500 金
    add_entry(lua, -2000000, "repair")      # 今日 -200 金
    set_epoch(lua, ep(2026, 11, 9))
    add_entry(lua, -1500000, "repair")      # 昨日 -150 金
    set_epoch(lua, ep(2026, 11, 10))        # 回到今天再开提示
    mm_script(lua, "OnEnter")
    lines = tip_texts(lua)
    want = ["金币日记账",
            "今日 %s" % cal_money_icon(3000000),
            "昨日 %s" % cal_money_icon(-1500000),
            "本周 %s" % cal_money_icon(1500000)]
    if lines[:4] != want:
        problems.append("净收入段不对：应为 %s，实际 %s" % (want, lines[:4]))
    for banned in (ICON_SILVER, ICON_COPPER):
        if any(banned in ln for ln in lines):
            problems.append("悬停不该出现银/铜：%r" % banned)
    return expect(name, problems)


def test_minimap_drag_writes_angle_and_detaches_onupdate():
    """沿环拖动：按住才挂 OnUpdate（算角度 → 写 settings.minimapAngle → 摆位），
    松手必须摘掉 —— 空闲零每帧脚本是用户的硬规矩，这条只能行为地钉。
    角度存在档里，下一次登录按存的摆而不是回默认 200°；
    静态那一关同时盯住 OnUpdate 只许出现在拖动那一进一出两处。"""
    name = "minimap_drag_writes_angle_and_detaches_onupdate"
    import math
    lua = t4_env()
    problems = []
    if mm_script(lua, "OnUpdate") is not False:
        problems.append("没拖动就有 OnUpdate 可跑（这就是轮询）")
    set_cursor(lua, 172, 80)          # 宿主中心 (80,80)、缩放 1 ⇒ 正右方 = 0°
    mm_script(lua, "OnDragStart")
    if mm_script(lua, "OnUpdate") is not True:
        problems.append("OnDragStart 没挂上 OnUpdate：拖动不会动")
    mm_script(lua, "OnUpdate")
    deg = cal_eval(lua, "GDLDB.settings.minimapAngle")
    if deg is None or abs(float(deg)) > 0.001:
        problems.append("拖动没把角度写进存档：%r（光标在宿主正右方，应为 0）" % deg)
    want = "CENTER|CENTER|%.3f|0.000" % RING_RADIUS
    if mm_point(lua) != want:
        problems.append("摆位没按 0° 落在环上：应为 %s 实际 %s" % (want, mm_point(lua)))
    mm_script(lua, "OnDragStop")
    if mm_script(lua, "OnUpdate") is not False:
        problems.append("OnDragStop 没把 OnUpdate 摘掉（松手还在每帧算）")
    # 二次登录：按钮原样留着，不许重新摆回默认（只清替身的摆位记录，生产状态一个都不写）
    lua.execute("GDLMapButton.__points = nil")
    fire(lua, "PLAYER_LOGIN")
    if mm_point(lua) is not None:
        problems.append("二次登录又摆了一次位：%s（角度该读存档，不是回默认）" % mm_point(lua))
    if stored_angle(lua) is None or abs(float(stored_angle(lua))) > 0.001:
        problems.append("角度没留在存档：%r" % stored_angle(lua))
    # 全新一次登录、档里已躺着一个角度：第一次摆位就得用它（默认是 200°，别搞混）
    lua2 = t4_env(login=False)
    lua2.execute("GDLDB.settings.minimapAngle = 0")
    fire(lua2, "PLAYER_LOGIN")
    if mm_point(lua2) != want:
        problems.append("登录没按存档里的角度摆位：应为 %s 实际 %s" % (want, mm_point(lua2)))
    if abs(math.cos(math.radians(DEFAULT_ANGLE)) * RING_RADIUS - RING_RADIUS) < 1:
        problems.append("默认角与 0° 算出来一样近，这条断言证不出任何东西")
    with open(os.path.join(PLUGIN_DIR, "Minimap.lua"), encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("--"))
    if code.count('SetScript("OnUpdate"') != 2:
        problems.append("OnUpdate 的挂/摘应各一处，实际 %d 处" % code.count('SetScript("OnUpdate"'))
    for banned in ("RegisterEvent", "C_Timer", "SetTicker", "GetTime"):
        if banned in code:
            problems.append("按钮文件里出现了 %s（不订事件、不挂定时器、不轮询）" % banned)
    return expect(name, problems)


# ---------------------------------------------------------------------------
# T8 流水弹窗夹具：开窗 / 筛选 / 滚动全走真入口（/gdl flow、帧上挂的 OnClick、
# OnMouseWheel），GetState()/GetUI() 只当取证件读，一行都不往里写。
# 流水行金额 = FormatMoneyFull（金银铜全量）；其余读数走 cal_money_icon（只写金币）。
# ---------------------------------------------------------------------------

def money_full(copper):
    """手抄 FormatMoneyFull 的契约（2026-11-11 定：只有流水**行**显示全量金银铜；
    悬停与分类小计/总计一律只写金币，走 cal_money_icon）：高位 0 跳段、全 0 显 0金、
    符号挂整串最前。不拿被测函数自己当标准答案。"""
    a = abs(int(copper))
    g, s, c = a // 10000, (a // 100) % 100, a % 100
    parts = []
    if g > 0 or (s == 0 and c == 0):
        parts.append("%d%s" % (g, ICON_GOLD))
    if s > 0:
        parts.append("%d%s" % (s, ICON_SILVER))
    if c > 0:
        parts.append("%d%s" % (c, ICON_COPPER))
    sign = "-" if copper < 0 and a > 0 else ""
    return sign + " ".join(parts)


def flow_rows(lua):
    """行池全槽快照：可见 + 三列字 + 金额色红通道。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Flows.GetUI()
        if not ui then return nil end
        local out = {}
        for i = 1, #ui.rows do
            local r = ui.rows[i]
            out[#out + 1] = {
                shown = tostring(r.host:IsShown() and 1 or 0),
                time = tostring(r.time:GetText() or ""),
                src = tostring(r.srcText:GetText() or ""),
                amt = tostring(r.amt:GetText() or ""),
                amtR = (r.amt.__color and ("%.4f"):format(r.amt.__color[1])) or "",
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("shown", "time", "src", "amt", "amtR"))


def flow_subs(lua):
    """十行分类小计快照：可见 + 哪类 + 三列字 + 类名色红通道（选中=金）。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GDL.Flows.GetUI()
        if not ui then return nil end
        local out = {}
        for i = 1, #ui.subRows do
            local r = ui.subRows[i]
            out[#out + 1] = {
                shown = tostring(r.btn:IsShown() and 1 or 0),
                src = tostring(r.src or ""),
                label = tostring(r.label:GetText() or ""),
                inc = tostring(r.inc:GetText() or ""),
                exp = tostring(r.exp:GetText() or ""),
                labelR = (r.label.__color and ("%.4f"):format(r.label.__color[1])) or "",
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("shown", "src", "label", "inc", "exp", "labelR"))


def flow_sub_index(lua, src):
    return as_number(cal_eval(lua, r"""(function()
        for i, r in ipairs(GDL.Flows.GetUI().subRows) do
            if r.src == %r then return i end
        end
        return nil
    end)()""" % src))


def flow_dd_index(lua, key, value):
    """找某个下拉里 value 对应的选项序号（走 GetUI 快照，不往 state 里写）。"""
    return as_number(cal_eval(lua, r"""(function()
        local dd = GDL.Flows.GetUI().dropdowns.%s
        if not dd then return nil end
        for i, o in ipairs(dd.options) do
            if o.value == %s then return i end
        end
        return nil
    end)()""" % (key, ("%r" % value) if value is not None else "nil")))


def flow_dd_select(lua, key, value):
    """点开下拉再点选项：两下都走帧上挂的 OnClick，不让用例直接改 state。"""
    dd = "GDL.Flows.GetUI().dropdowns.%s" % key
    cal_click(lua, dd)
    idx = flow_dd_index(lua, key, value)
    if idx is None:
        return False
    cal_click(lua, "%s.items[%d].btn" % (dd, idx))
    return True


def flow_dd_labels(lua, key):
    """下拉选项的标签清单（用来钉日期档位带确切日期、角色名不带服名）。"""
    tbl = cal_eval(lua, r"""(function()
        local dd = GDL.Flows.GetUI().dropdowns.%s
        if not dd then return nil end
        local out = {}
        for i, o in ipairs(dd.options) do out[i] = tostring(o.label) end
        return out
    end)()""" % key)
    return [] if tbl is None else [str(tbl[i]) for i in range(1, len(tbl) + 1)]


def test_ledger_char_entries_all_chars_and_source_filter():
    """GetCharEntries 两个新参：charKey=nil 聚全部号；source 只收窄 rows/total/页数，
    bySource 永远覆盖整个 char+range —— 弹窗里点任一小计行就切类，缩小计就只剩撤销换不了类。"""
    name = "ledger_char_entries_all_chars_and_source_filter"
    lua = ledger_env()
    day = ep(2026, 11, 10)
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    set_char(lua, "甲号")
    set_epoch(lua, day + 10); add_entry(lua, 100, "vendor")
    set_epoch(lua, day + 20); add_entry(lua, -30, "repair")
    set_char(lua, "乙号")
    set_epoch(lua, day + 30); add_entry(lua, 200, "vendor")
    set_epoch(lua, day + 40); add_entry(lua, 500, "ah")
    set_epoch(lua, day + 86400); add_entry(lua, 999, "loot")   # 次日：范围排除
    got = str(ledger_block(lua, r"""
        local L = GDL.Ledger
        local function bs(t)
            local keys = {}
            for k in pairs(t.bySource) do keys[#keys + 1] = k end
            table.sort(keys)
            local out = {}
            for _, k in ipairs(keys) do
                out[#out + 1] = k .. ':' .. t.bySource[k].income .. '/' .. t.bySource[k].expense
            end
            return table.concat(out, ',')
        end
        local a = L.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 100)
        local v = L.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 100, 'vendor')
        local vp = L.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 1, 'vendor')
        local r = L.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 100, 'repair')
        local j = L.GetCharEntries('甲号-%s', '2026-11-10', '2026-11-10', 1, 100)
        local bad = L.GetCharEntries(123, '2026-11-10', '2026-11-10', 1, 100)
        return a.total .. '|' .. a.rows[1].src .. '|' .. a.rows[4].char .. '|' .. bs(a)
            .. '||' .. v.total .. '|' .. v.rows[1].amt .. '|' .. v.rows[2].amt .. '|' .. bs(v)
            .. '||' .. vp.totalPages .. '|' .. bs(r) .. '|' .. r.total
            .. '||' .. j.total .. '|' .. bs(j) .. '||' .. bad.total
    """ % REALM))
    want = ("4|ah|甲号-%s|ah:500/0,repair:0/30,vendor:300/0"
            "||2|200|100|ah:500/0,repair:0/30,vendor:300/0"
            "||2|ah:500/0,repair:0/30,vendor:300/0|1"
            "||2|repair:0/30,vendor:100/0||0") % REALM
    if got != want:
        return fail(name, "全部号/source 筛选不符：\n期望 %s\n实际 %s" % (want, got))
    return ok(name)


def test_flows_popup_opens_from_command_and_lists_rows():
    """/gdl flow 不带参 = 全部角色，带号名 = 锁定该号（标题只写短名 + 职业色）。
    行字（时间/分类/整数金）、小计排序、悬停、四个筛选下拉与分页栏都在 Python 侧另算。"""
    name = "flows_popup_opens_from_command_and_lists_rows"
    lua = calendar_env()
    day = ep(2026, 11, 10)
    set_epoch(lua, day + 3600); add_entry(lua, 12345, "vendor")
    set_epoch(lua, day + 7200); add_entry(lua, -678, "repair")
    set_char(lua, "别的号")
    set_epoch(lua, day + 1800); add_entry(lua, 99999, "ah", "{ subject = '出售: 甲' }")
    problems = []
    slash(lua, "flow")
    if not bool(cal_eval(lua, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("/gdl flow 没把窗打开")
    title = str(cal_eval(lua, "GDL.Flows.GetUI().title:GetText()"))
    if title != "流水 · 全部角色 · 本日 2026-11-10":
        problems.append("全部角色标题应为 流水 · 全部角色 · 本日 2026-11-10，实际 %s" % title)
    if cal_eval(lua, "GDL.Flows.GetUI().btnChar") is not None:
        problems.append("两态「全部号」按钮没拆干净")
    # 四个下拉按钮只写选中的值，不写「角色 · / 类型 · / 收支 ·」筛子名前缀
    for key in ("char", "type", "range", "dir"):
        dd = "GDL.Flows.GetUI().dropdowns.%s" % key
        if cal_eval(lua, dd) is None:
            problems.append("缺 %s 筛选下拉" % key)
            continue
        want = str(cal_eval(lua, "%s.currentLabel" % dd))
        got = cal_text(lua, "%s.label" % dd)
        if got != want:
            problems.append("%s 下拉应只写选中值 %r，实际 %r" % (key, want, got))
        if any(got.startswith(p) for p in ("角色 ·", "类型 ·", "日期 ·", "收支 ·")):
            problems.append("%s 下拉还带着筛子名前缀：%r" % (key, got))
    if cal_text(lua, "GDL.Flows.GetUI().dropdowns.range.label") != "本日 2026-11-10":
        problems.append("日期下拉没写确切日期")
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 1 页":
        problems.append("分页页码不对：%r" % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    shown = [r for r in flow_rows(lua) if r["shown"] == "1"]
    if len(shown) != 3:
        problems.append("全部角色应列 3 行，实际 %d" % len(shown))
    else:
        if (shown[0]["time"], shown[0]["src"], shown[0]["amt"]) != ("2026-11-10 14:00", "修理", money_full(-678)):
            problems.append("首行(最新)应 14:00 修理 -6银 78铜：%r" % shown[0])
        if (shown[1]["time"], shown[1]["src"], shown[1]["amt"]) != ("2026-11-10 13:00", "卖店", money_full(12345)):
            problems.append("次行应 13:00 卖店 1金 23银 45铜：%r" % shown[1])
        if (shown[2]["time"], shown[2]["src"], shown[2]["amt"]) != ("2026-11-10 12:30", "拍卖行", money_full(99999)):
            problems.append("三行应 12:30 拍卖行 9金 99银 99铜：%r" % shown[2])
        if shown[0]["amtR"] != "0.8600":
            problems.append("支出金额应红(0.86)，实际 %s" % shown[0]["amtR"])
        if shown[1]["amtR"] != "0.2400":
            problems.append("收入金额应绿(0.24)，实际 %s" % shown[1]["amtR"])
    subs = [s for s in flow_subs(lua) if s["shown"] == "1"]
    if [s["src"] for s in subs] != ["ah", "vendor", "repair"]:
        problems.append("小计应按净降序 [ah,vendor,repair]，实际 %s" % [s["src"] for s in subs])
    elif subs[0]["inc"] != cal_money_icon(99999) or subs[2]["exp"] != cal_money_icon(678):
        problems.append("小计金额不符：%r / %r" % (subs[0], subs[2]))
    # 悬停：分类 + 条目自带的邮件标题（RowTip 走 OnEnter/OnLeave 真入口）
    lua.globals()["GDL_CallScript"](cal_eval(lua, "GDL.Flows.GetUI().rows[3].host"), "OnEnter")
    tips = lua.globals()["GDL_TIP_LINES"]
    lines = [str(tips[i].text) for i in range(1, len(tips) + 1)]
    if lines != ["拍卖行", "出售: 甲"]:
        problems.append("悬停应 [拍卖行, 出售: 甲]，实际 %s" % lines)
    if cal_eval(lua, "GameTooltip.__shown") is not True:
        problems.append("悬停没把提示亮出来")
    lua.globals()["GDL_CallScript"](cal_eval(lua, "GDL.Flows.GetUI().rows[3].host"), "OnLeave")
    if cal_eval(lua, "GameTooltip.__shown") is True:
        problems.append("离开没把提示收掉")
    # 锁定某号：标题只印短名+职业色，角色下拉当前值仍是完整 key
    slash(lua, "flow 当前号-%s" % REALM)
    title = str(cal_eval(lua, "GDL.Flows.GetUI().title:GetText()"))
    if title != "流水 · |cff40c7eb当前号|r · 本日 2026-11-10":
        problems.append("锁定标题应带职业色短名，实际 %s" % title)
    if str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.char.value")) != "当前号-%s" % REALM:
        problems.append("角色下拉没选中锁定的号")
    shown = [r for r in flow_rows(lua) if r["shown"] == "1"]
    if len(shown) != 2 or shown[0]["src"] != "修理" or shown[1]["src"] != "卖店":
        problems.append("锁定后应只剩当前号 2 行 [修理,卖店]，实际 %r" % shown)
    # 换语言时弹窗也要重画（选项文案、页码按钮、列头都是静态串）
    slash(lua, "lang enUS")
    if cal_text(lua, "GDL.Flows.GetUI().dropdowns.type.label") != "All types":
        problems.append("切 enUS 后类型下拉没重画：%r"
                        % cal_text(lua, "GDL.Flows.GetUI().dropdowns.type.label"))
    if cal_text(lua, "GDL.Flows.GetUI().page.prev") != "Prev":
        problems.append("切 enUS 后翻页按钮没重画：%r"
                        % cal_text(lua, "GDL.Flows.GetUI().page.prev"))
    slash(lua, "lang zhCN")
    return expect(name, problems)


def test_flows_dropdown_filters():
    """四个下拉各自只改自己那一项：角色（含全部角色）/ 类型 / 日期 / 收支方向；
    点分类汇总那一行也能切类型且下拉同步，筛选后小计仍列全。"""
    name = "flows_dropdown_filters"
    lua = calendar_env()
    day = ep(2026, 11, 10)
    set_epoch(lua, day + 10); add_entry(lua, 100, "vendor")
    set_epoch(lua, day + 20); add_entry(lua, -40, "repair")
    set_char(lua, "别的号")
    set_epoch(lua, day + 30); add_entry(lua, 900, "vendor")
    problems = []
    slash(lua, "flow 当前号-%s" % REALM)

    def n_rows():
        return len([r for r in flow_rows(lua) if r["shown"] == "1"])

    if n_rows() != 2:
        problems.append("带号名开应锁定该号(2行)，实际 %d" % n_rows())
    # 角色下拉：全部角色 → 3 行；选回具体号 → 2 行
    if not flow_dd_select(lua, "char", ""):
        problems.append("角色下拉里没有「全部角色」")
    if cal_eval(lua, "GDL.Flows.GetState().charKey") is not None or n_rows() != 3:
        problems.append("选全部角色应放开到 3 行，实际 %d" % n_rows())
    if not flow_dd_select(lua, "char", "当前号-%s" % REALM):
        problems.append("角色下拉里没有当前号")
    if str(cal_eval(lua, "GDL.Flows.GetState().charKey")) != "当前号-%s" % REALM or n_rows() != 2:
        problems.append("选回具体号应锁回 2 行")
    # 类型下拉选 vendor：只剩 1 行，汇总仍列全 2 行，下拉选中值同步
    if not flow_dd_select(lua, "type", "vendor"):
        problems.append("类型下拉里没有 vendor")
    if str(cal_eval(lua, "GDL.Flows.GetState().source")) != "vendor" or n_rows() != 1:
        problems.append("type=vendor 应只剩 1 行，实际 %d" % n_rows())
    subs = [s for s in flow_subs(lua) if s["shown"] == "1"]
    if len(subs) != 2:
        problems.append("筛选后小计应仍列全 2 行，实际 %d" % len(subs))
    if str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.type.value")) != "vendor":
        problems.append("类型下拉没同步选中值")
    # 点汇总里同一行 = 清筛选（下拉与汇总是同一份 state）
    vi = flow_sub_index(lua, "vendor")
    if vi is not None:
        cal_click(lua, "GDL.Flows.GetUI().subRows[%d].btn" % vi)
        if cal_eval(lua, "GDL.Flows.GetState().source") is not None or n_rows() != 2:
            problems.append("再点同一汇总行应清掉筛选回 2 行")
    # 收支方向：只看收入 1 行 / 只看支出 1 行 / 全部 2 行
    for value, want in (("in", 1), ("out", 1), ("all", 2)):
        if not flow_dd_select(lua, "dir", value):
            problems.append("收支下拉缺 %s 选项" % value)
        if str(cal_eval(lua, "GDL.Flows.GetState().direction")) != value or n_rows() != want:
            problems.append("direction=%s 应 %d 行，实际 %d" % (value, want, n_rows()))
    # 日期下拉六档，标签里都带确切日期（档位词不让人猜锚点）
    labels = flow_dd_labels(lua, "range")
    for want in ("本日 2026-11-10", "昨日 2026-11-09", "上周 11-02~11-08", "本月 2026-11", "全部"):
        if want not in labels:
            problems.append("日期下拉缺 %r，实际 %s" % (want, labels))
    if not any(ln.startswith("本周 ") and "~" in ln for ln in labels):
        problems.append("日期下拉的本周档没给起止 MM-DD：%s" % labels)
    if not flow_dd_select(lua, "range", "yesterday"):
        problems.append("日期下拉缺「昨日」")
    if str(cal_eval(lua, "GDL.Flows.GetState().startKey")) != "2026-11-09":
        problems.append("选昨日应锚到 11-09，实际 %s" % cal_eval(lua, "GDL.Flows.GetState().startKey"))
    if "昨日 2026-11-09" not in cal_text(lua, "GDL.Flows.GetUI().title"):
        problems.append("标题没跟着日期档走：%r" % cal_text(lua, "GDL.Flows.GetUI().title"))
    return expect(name, problems)


def test_flows_paging_and_scroll():
    """真分页每页 20 行：25 笔 ⇒ 第 1 页 20 行 / 第 2 页 5 行，页码与按钮启用态跟着走；
    页内滚轮偏移自己记、两端夹住，每行宿主都挂轮；溢出提示与 CAP 已下线，窗高恒定。"""
    name = "flows_paging_and_scroll"
    lua = calendar_env()
    # 夹具用 loot：同一秒连发的 vendor 会被读出层并成一行（另有专例钉），
    # 这里量的是分页本身，不能让合并把这 25 行吃掉。
    lua.execute("for i = 1, 25 do GDL.Ledger:AddEntry(100 + i, 'loot') end")
    problems = []
    slash(lua, "flow")
    row_h = as_number(cal_eval(lua, "GDL.Flows.GEOM.ROW_H"))
    list_h = as_number(cal_eval(lua, "GDL.Flows.GEOM.LIST_H"))
    win_h = as_number(cal_eval(lua, "GDLFlowFrame:GetHeight()"))
    per_page = as_number(cal_eval(lua, "GDL.Flows.PER_PAGE"))
    if per_page != 20:
        problems.append("每页应是 20 行，实际 %s" % per_page)
    if len([r for r in flow_rows(lua) if r["shown"] == "1"]) != 20:
        problems.append("第 1 页应正好 20 行，实际 %d" % len([r for r in flow_rows(lua) if r["shown"] == "1"]))
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 2 页":
        problems.append("第 1 页页码不对：%r" % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    if cal_eval(lua, "GDL.Flows.GetUI().page.prev:IsEnabled()") is not False:
        problems.append("第 1 页「上一页」应禁用")
    if cal_eval(lua, "GDL.Flows.GetUI().page.next:IsEnabled()") is not True:
        problems.append("第 1 页「下一页」应可用")
    cal_click(lua, "GDL.Flows.GetUI().page.next")
    if len([r for r in flow_rows(lua) if r["shown"] == "1"]) != 5:
        problems.append("第 2 页应只剩 5 行，实际 %d" % len([r for r in flow_rows(lua) if r["shown"] == "1"]))
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 2 / 2 页":
        problems.append("第 2 页页码不对：%r" % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    if cal_eval(lua, "GDL.Flows.GetUI().page.next:IsEnabled()") is not False:
        problems.append("末页「下一页」应禁用")
    cal_click(lua, "GDL.Flows.GetUI().page.prev")
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 2 页":
        problems.append("翻回第 1 页页码不对")
    if as_number(cal_eval(lua, "GDLFlowContent:GetHeight()")) != 20 * row_h:
        problems.append("一页 20 行内容高应 %d，实际 %s"
                        % (20 * row_h, cal_eval(lua, "GDLFlowContent:GetHeight()")))
    if not bool(cal_eval(lua, "GDLFlowScroll.__wheel")):
        problems.append("滚动帧没开 EnableMouseWheel")
    bad_wheel = as_number(cal_eval(lua, r"""(function()
        local n = 0
        for i, r in ipairs(GDL.Flows.GetUI().rows) do
            if r.host.__wheel ~= true or not r.host:GetScript("OnMouseWheel") then n = n + 1 end
        end
        return n
    end)()"""))
    if bad_wheel != 0:
        problems.append("%d 个行宿主没挂滚轮" % bad_wheel)

    def wheel(expr, delta):
        lua.globals()["GDL_CallScript"](cal_eval(lua, expr), "OnMouseWheel", delta)
        return (as_number(cal_eval(lua, "GDL.Flows.GetState().scrollY")),
                as_number(cal_eval(lua, "GDLFlowScroll.__scrolly")))

    y, written = wheel("GDLFlowScroll", -1)
    if (y, written) != (row_h, row_h):
        problems.append("轮 -1 应下一行(%d)，实际 %s/%s" % (row_h, y, written))
    top = 20 * row_h - list_h
    y, written = wheel("GDLFlowScroll", -100)
    if (y, written) != (top, top):
        problems.append("应夹在底 %d，实际 %s/%s" % (top, y, written))
    y, written = wheel("GDLFlowScroll", 100)
    if (y, written) != (0, 0):
        problems.append("应夹在顶 0，实际 %s/%s" % (y, written))
    y, written = wheel("GDL.Flows.GetUI().rows[1].host", -1)
    if (y, written) != (row_h, row_h):
        problems.append("行宿主上的滚轮也应滚动，实际 %s/%s" % (y, written))
    # 溢出提示与 400 上限整个下线：total 真值可超过一页，行数只受分页控制
    lua.execute("for i = 1, 405 do GDL.Ledger:AddEntry(1, 'loot') end")
    slash(lua, "flow")
    if cal_eval(lua, "GDL.Flows.GetUI().note") is not None:
        problems.append("「另有 N 笔未列出」提示没拆干净")
    if as_number(cal_eval(lua, "GDL.Flows.GetState().info.total")) != 430:
        problems.append("分页后 total 应是全量 430，实际 %s"
                        % cal_eval(lua, "GDL.Flows.GetState().info.total"))
    if cal_text(lua, "GDL.Flows.GetUI().page.label") != "第 1 / 22 页":
        problems.append("430 笔 / 每页 20 应 22 页，实际 %r"
                        % cal_text(lua, "GDL.Flows.GetUI().page.label"))
    if as_number(cal_eval(lua, "GDLFlowFrame:GetHeight()")) != win_h:
        problems.append("行数变化窗高应恒定 %d，实际 %s" % (win_h, cal_eval(lua, "GDLFlowFrame:GetHeight()")))
    return expect(name, problems)


def test_flows_summary_on_side_column():
    """分类汇总挪到弹窗右侧竖列并做到顶（2026-11-11 三次定稿）：列头与筛选下拉
    平级、行从流水列头那行起，不再挂在列表下面，窗高不随汇总行数变。"""
    name = "flows_summary_on_side_column"
    lua = calendar_env()
    problems = []
    slash(lua, "flow")
    sub_x = as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_X"))
    list_w = as_number(cal_eval(lua, "GDL.Flows.GEOM.LIST_W"))
    pad = as_number(cal_eval(lua, "GDL.Flows.GEOM.PAD"))
    if not (sub_x is not None and sub_x > pad + list_w):
        problems.append("汇总列的 x=%s 没落在流水列表右边" % sub_x)
    head_pt = cal_eval(lua, "GDL.Flows.GetUI().subHead.label.__points[1]")
    row_pt = cal_eval(lua, "GDL.Flows.GetUI().subRows[1].btn.__points[1]")
    filter_y = pad + as_number(cal_eval(lua, "GDL.Flows.GEOM.TITLE_H"))
    head_y = filter_y + as_number(cal_eval(lua, "GDL.Flows.GEOM.FILTER_H"))
    if as_number(head_pt[5]) != -filter_y:
        problems.append("汇总列头没顶到筛选行（与下拉平级）：%s vs -%s" % (head_pt[5], filter_y))
    if as_number(row_pt[5]) != -head_y:
        problems.append("汇总行没从流水列头那行起（还缩在列表腰上）：%s vs -%s" % (row_pt[5], head_y))
    if as_number(row_pt[4]) != sub_x:
        problems.append("汇总行没放在右侧那一列：x=%s vs %s" % (row_pt[4], sub_x))
    if as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_MAX")) != 10:
        problems.append("汇总可见行数变了")
    return expect(name, problems)


def test_flows_close_and_esc_and_level():
    """× 点了藏窗；模板自带 510 钉回主帧+1；ESC 名单存全局名且不重复；
    二次开窗复用已建的帧（一个新帧都不许多建）。"""
    name = "flows_close_and_esc_and_level"
    lua = calendar_env()
    problems = []

    def esc_count():
        return as_number(cal_eval(lua, r"""(function()
            local n = 0
            for i = 1, #UISpecialFrames do
                if UISpecialFrames[i] == "GDLFlowFrame" then n = n + 1 end
            end
            return n
        end)()"""))

    slash(lua, "flow")
    n_frames = as_number(cal_eval(lua, "#GDL_FRAMES"))
    if esc_count() != 1:
        problems.append("ESC 名单里 GDLFlowFrame 应恰好一条，实际 %d" % esc_count())
    lvl = as_number(cal_eval(lua, "GDL.Flows.GetUI().close:GetFrameLevel()"))
    main_lvl = as_number(cal_eval(lua, "GDLFlowFrame:GetFrameLevel()"))
    if lvl != main_lvl + 1:
        problems.append("× 应钉回主帧+1(%d)，实际 %d（模板自带 510 没钉回）" % (main_lvl + 1, lvl))
    cal_click(lua, "GDL.Flows.GetUI().close")
    if bool(cal_eval(lua, "GDLFlowFrame:IsShown()")):
        problems.append("点 × 应把窗藏掉")
    slash(lua, "flow")
    if not bool(cal_eval(lua, "GDLFlowFrame:IsShown()")):
        problems.append("二次 /gdl flow 应重新打开")
    if as_number(cal_eval(lua, "#GDL_FRAMES")) != n_frames:
        problems.append("二次开窗又建了新帧（应复用）")
    if esc_count() != 1:
        problems.append("二次开窗把 ESC 名单插重复了")
    return expect(name, problems)


def test_flows_range_text_six_presets():
    """范围说法只在一处拼（右栏、读数方块、下拉选项、/gdl flow 共用）：本日/昨日给 ISO 日键、
    本周/上周给起~止 MM-DD、本月给 YYYY-MM、全部只写档位词；切 enUS 后跟着换。
    并钉住用户定稿：锚是**日历点选的那天**，点 11-05 开弹窗不会跳回今天 11-10。"""
    name = "flows_range_text_six_presets"
    lua = calendar_env()
    problems = []
    cases = [
        ("day", "2026-11-10", "2026-11-10", "本日 2026-11-10"),
        ("yesterday", "2026-11-09", "2026-11-09", "昨日 2026-11-09"),
        ("week", "2026-11-05", "2026-11-11", "本周 11-05~11-11"),
        ("lastweek", "2026-10-29", "2026-11-04", "上周 10-29~11-04"),
        ("month", "2026-11-01", "2026-11-30", "本月 2026-11"),
        ("all", "0000-01-01", "9999-12-31", "全部"),
    ]
    for preset, s, e, want in cases:
        got = str(cal_eval(lua, "GDL.Flows.RangeText(%r, %r, %r)" % (preset, s, e)))
        if got != want:
            problems.append("zhCN %s 档应 %r，实际 %r" % (preset, want, got))
    # 日历点选 11-05 → 开该号流水，标题与范围都得锚在 11-05
    set_epoch(lua, ep(2026, 11, 5))
    add_entry(lua, 10000, "quest")
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    cal_click(lua, "GDL.Calendar.GetUI().cells[5].host")
    cal_click(lua, "GDL.Calendar.GetUI().charRows[1].host")
    title = cal_text(lua, "GDL.Flows.GetUI().title")
    if "本日 2026-11-05" not in title or "2026-11-10" in title:
        problems.append("点 11-05 开弹窗的标题没锚在那天：%r" % title)
    if (str(cal_eval(lua, "GDL.Flows.GetState().startKey")),
            str(cal_eval(lua, "GDL.Flows.GetState().endKey"))) != ("2026-11-05", "2026-11-05"):
        problems.append("弹窗范围没跟着日历选中日走")
    if not flow_dd_select(lua, "range", "yesterday"):
        problems.append("日期下拉缺「昨日」")
    if str(cal_eval(lua, "GDL.Flows.GetState().startKey")) != "2026-11-04":
        problems.append("锚在 11-05 时「昨日」应是 11-04，实际 %s"
                        % cal_eval(lua, "GDL.Flows.GetState().startKey"))
    slash(lua, "lang enUS")
    cases_en = [
        ("day", "2026-11-10", "2026-11-10", "Day 2026-11-10"),
        ("yesterday", "2026-11-09", "2026-11-09", "Yesterday 2026-11-09"),
        ("week", "2026-11-05", "2026-11-11", "This Week 11-05~11-11"),
        ("lastweek", "2026-10-29", "2026-11-04", "Last Week 10-29~11-04"),
        ("month", "2026-11-01", "2026-11-30", "Month 2026-11"),
        ("all", "0000-01-01", "9999-12-31", "All"),
    ]
    for preset, s, e, want in cases_en:
        got = str(cal_eval(lua, "GDL.Flows.RangeText(%r, %r, %r)" % (preset, s, e)))
        if got != want:
            problems.append("enUS %s 档应 %r，实际 %r" % (preset, want, got))
    slash(lua, "flow")
    title = str(cal_eval(lua, "GDL.Flows.GetUI().title:GetText()"))
    if title != "Flows · All characters · Day 2026-11-10":
        problems.append("enUS 开窗标题应 Flows · All characters · Day 2026-11-10，实际 %s" % title)
    return expect(name, problems)


def test_flows_empty_and_no_clock():
    """空态分两档：锁号没流水 = EMPTY_FLOW、全部角色没变动 = EMPTY_RANGE，空时小计头全藏；
    墙上钟读不出时 /gdl flow 报 NO_CLOCK 且不开窗。"""
    name = "flows_empty_and_no_clock"
    lua = calendar_env()
    problems = []
    slash(lua, "flow 当前号-%s" % REALM)
    empty = str(cal_eval(lua, "GDL.Flows.GetUI().empty:GetText()"))
    if not bool(cal_eval(lua, "GDL.Flows.GetUI().empty:IsShown()")) or empty != "这个号在这段没有流水":
        problems.append("锁号空态应写 这个号在这段没有流水，实际 %r" % empty)
    if bool(cal_eval(lua, "GDL.Flows.GetUI().subHead.label:IsShown()")):
        problems.append("空态小计头应藏着")
    if any(s["shown"] == "1" for s in flow_subs(lua)):
        problems.append("空态小计行应全藏")
    if not flow_dd_select(lua, "char", ""):
        problems.append("角色下拉缺「全部角色」")
    empty = str(cal_eval(lua, "GDL.Flows.GetUI().empty:GetText()"))
    if empty != "这个范围没有变动":
        problems.append("全部角色空态应写 这个范围没有变动，实际 %r" % empty)
    lua2 = calendar_env()
    set_clock_none(lua2)
    lines = slash(lua2, "flow")
    if not any("读不出今天" in ln for ln in lines):
        problems.append("无墙上钟应报 读不出今天…，实际 %s" % lines)
    if bool(cal_eval(lua2, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("无墙上钟不该把窗打开")
    return expect(name, problems)


def test_calendar_round4_geometry_and_cell_font():
    """第 4 轮主窗三条：格子加高；日号 / 净换成与悬停提示同量级的小字；
    右栏加宽到「本月」按钮不再压住右上角那颗 ×。"""
    name = "calendar_round4_geometry_and_cell_font"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    cal_open(lua)
    problems = []
    if cal_geom(lua, "CELL_H") < 40:
        problems.append("格高 %s 没按反馈加高（应 ≥ 40）" % cal_geom(lua, "CELL_H"))
    num_font = str(cal_eval(lua, "GDL.Calendar.GetUI().cells[10].num.__font"))
    net_font = str(cal_eval(lua, "GDL.Calendar.GetUI().cells[10].net.__font"))
    row_font = str(cal_eval(lua, "GDL.Calendar.GetUI().charRows[1].name.__font"))
    if num_font != "GameFontNormalSmall" or net_font != num_font:
        problems.append("格子日号/净没换成小字或两行字号不同：%s / %s" % (num_font, net_font))
    if row_font == num_font:
        problems.append("格子小字不该把右栏行也一起缩小：%s" % row_font)
    if cal_geom(lua, "PANEL_W") < 260:
        problems.append("右栏 %s 没按反馈加宽（应 ≥ 260）" % cal_geom(lua, "PANEL_W"))
    # 「本月」是右栏第三个页签；× 锚在窗右上角（模板按钮按 20px 宽估）。
    month_right = cal_geom(lua, "PANEL_X") + cal_geom(lua, "PRESET_STRIP_W")
    close_left = cal_geom(lua, "WIN_W") - cal_geom(lua, "CLOSE_INSET") - 20
    if month_right >= close_left:
        problems.append("「本月」右沿 %d 仍旧压到 × 左沿 %d" % (month_right, close_left))
    # × 真身可能比 20px 宽（UIPanelCloseButton 常见 32px）：按 32 再算一遍也要让开。
    if month_right >= cal_geom(lua, "WIN_W") - cal_geom(lua, "CLOSE_INSET") - 32:
        problems.append("「本月」右沿 %d 按 32px 宽的 × 算就压上了" % month_right)
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_round5_tip_frame_uniform_and_opaque():
    """第 5 轮：GameTooltip 换自绘提示框。四行同一个字体对象、等行高，
    背板 alpha=1、TOOLTIP strata；收入/支出不再一大一小，也不半透明。
    关窗必须把这扇 UIParent 子帧一起收掉。"""
    name = "calendar_round5_tip_frame_uniform_and_opaque"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    cal_open(lua)
    problems = []
    ui = "GDL.Calendar.GetUI()"
    fonts = cal_eval(lua, r"""(function()
        local ui = GDL.Calendar.GetUI()
        local out = {}
        for i, fs in ipairs(ui.tipLines) do out[i] = tostring(fs.__font) end
        return out
    end)()""")
    want_font = str(fonts[1])
    if want_font != "GameFontNormalSmall":
        problems.append("提示框字体应为 GameFontNormalSmall，实际 %s" % want_font)
    if any(str(fonts[i]) != want_font for i in range(1, 5)):
        problems.append("提示四行不是同一个字体对象：%s"
                        % [str(fonts[i]) for i in range(1, 5)])
    bd = cal_eval(lua, "%s.tip.__bdcolor" % ui)
    if bd is None or as_number(bd[4]) != 1:
        problems.append("提示背板 alpha 不是 1（还是半透明）")
    if str(cal_eval(lua, "%s.tip.__strata" % ui)) != "TOOLTIP":
        problems.append("提示框没挂在 TOOLTIP strata")
    row_h = as_number(cal_eval(lua, "GDL.Calendar.GEOM.TIP_ROW_H"))
    ys = [as_number(cal_eval(lua, "%s.tipLines[%d].__points[1][5]" % (ui, i)))
          for i in range(1, 5)]
    if any(ys[i] - ys[i + 1] != row_h for i in range(3)):
        problems.append("提示行不是等行高：%s" % ys)

    def check(count, tag):
        lines = cal_tip_frame_lines(lua)
        if len(lines) != count:
            problems.append("%s 应有 %d 行可见，实际 %d：%s" % (tag, count, len(lines), lines))
        if not bool(cal_eval(lua, "%s.tip:IsShown()" % ui)):
            problems.append("%s 没把提示框显示出来" % tag)

    cal_script(lua, "GDL.Calendar.GetUI().cells[10].host", "OnEnter")
    check(4, "格子提示")
    cal_script(lua, "GDL.Calendar.GetUI().charRows[1].host", "OnEnter")
    check(3, "右栏行提示")
    cal_click(lua, "GDL.Calendar.GetUI().close")
    if bool(cal_eval(lua, "%s.tip:IsShown()" % ui)):
        problems.append("关窗后提示框还浮在屏幕上")
    return expect(name, problems)


def test_ledger_round4_char_color_fallback_not_gray():
    """没存到 class 的旧号不再染中性灰：退白色（不编职业色）；有 class 的号仍走真实职业色。"""
    name = "ledger_round4_char_color_fallback_not_gray"
    lua = calendar_env()
    ghost = "旧号-%s" % REALM
    lua.execute("GDLDB.chars[%r] = { firstSeen = 1, lastSeen = 1 }" % ghost)
    problems = []
    rgb = cal_eval(lua, "GDL.Ledger.CharColor(%r)" % ghost)
    vals = [as_number(rgb[i]) for i in (1, 2, 3)]
    if vals != [1.0, 1.0, 1.0]:
        problems.append("旧号兜底色仍是 %s，应为白色 {1,1,1}" % vals)
    lab = str(cal_eval(lua, "GDL.Ledger.CharLabel(%r, 24)" % ghost))
    if not lab.startswith("|cffffffff") or REALM in lab:
        problems.append("旧号短名/兜底色不对：%r" % lab)
    add_entry(lua, 1000000, "quest")
    cur = "当前号-%s" % REALM
    if not str(cal_eval(lua, "GDL.Ledger.CharLabel(%r, 24)" % cur)).startswith("|cff40c7eb"):
        problems.append("有 class 的当前号没走 MAGE 职业色")
    # 流水弹窗侧同样不许灰：标题短名走白色兜底，角色下拉「全部角色」项也用白色
    slash(lua, "flow " + ghost)
    title = cal_text(lua, "GDL.Flows.GetUI().title")
    if "|cff9e9e9e" in title or "|cffffffff旧号|r" not in title:
        problems.append("弹窗标题里旧号仍被染灰：%r" % title)
    item_color = cal_eval(lua, "GDL.Flows.GetUI().dropdowns.char.items[1].fs.__color")
    if item_color is None or as_number(item_color[1]) != 1 or as_number(item_color[2]) != 1:
        problems.append("角色下拉的「全部角色」项仍是灰色")
    return expect(name, problems)


def test_locale_round4_source_names():
    """「未知」改名「其他」、战团银行正名；两个语言都点得出货，类型下拉里三档都在。"""
    name = "locale_round4_source_names"
    lua, _ = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")
    loc = lua.globals()["GDL"].Locale
    problems = []
    want = {
        "zhCN": {"unknown": "其他", "warband": "战团银行", "guildbank": "公会银行",
                 "token": "时光徽章"},
        "enUS": {"unknown": "Other", "warband": "Warband Bank", "guildbank": "Guild bank",
                 "token": "WoW Token"},
    }
    for code, table in want.items():
        loc.Set(code)
        for src, text in table.items():
            got = str(loc.SrcName(src))
            if got != text:
                problems.append("%s 的 %s 应显示 %r，实际 %r" % (code, src, text, got))
    loc.Set("zhCN")
    lua2 = calendar_env()
    slash(lua2, "flow")
    labels = flow_dd_labels(lua2, "type")
    for text in ("其他", "战团银行", "公会银行", "时光徽章"):
        if text not in labels:
            problems.append("类型下拉缺 %r：%s" % (text, labels))
    return expect(name, problems)


def test_flows_round4_range_dropdown_layout():
    """日期下拉去 label 前缀、直接写日期/日期范围；四个按钮的字串都定宽防溢出，
    日期档另用小一号字体，四格连间隙仍排得进窗宽。"""
    name = "flows_round4_range_dropdown_layout"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    slash(lua, "flow")
    problems = []
    keys = ("char", "type", "range", "dir")
    for i, key in enumerate(keys, start=1):
        w = as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_W[%d]" % i))
        got = as_number(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.%s.label.__w" % key))
        if got != w - 12:
            problems.append("%s 下拉字串没定宽：%s，应为 %s" % (key, got, w - 12))
    if str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.range.label.__font")) != "GameFontNormalSmall":
        problems.append("日期档没用小一号字体")
    widths = [as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_W[%d]" % i)) for i in range(1, 5)]
    if widths[2] < 96:
        problems.append("日期档宽 %s 放不下确切日期/日期范围" % widths[2])
    gap = as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_GAP"))
    pad = as_number(cal_eval(lua, "GDL.Flows.GEOM.PAD"))
    right = pad + sum(widths) + 3 * gap
    if right > as_number(cal_eval(lua, "GDL.Flows.GEOM.WIN_W")):
        problems.append("四个下拉右沿 %d 顶出了窗宽" % right)
    for preset in ("yesterday", "week", "lastweek", "month", "all", "day"):
        if not flow_dd_select(lua, "range", preset):
            problems.append("日期下拉缺 %s 档" % preset)
            continue
        want = str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.range.currentLabel"))
        got = cal_text(lua, "GDL.Flows.GetUI().dropdowns.range.label")
        if got != want or "·" in got:
            problems.append("%s 档按钮应只写 %r，不要 label 前缀，实际 %r" % (preset, want, got))
    return expect(name, problems)


def test_flows_round4_summary_columns_aligned():
    """分类小计三列同一行：inc/exp 与类型名都锚在行按钮垂直中心、右沿与列头
    的 x 对齐；类名常态色是白色而不是灰。"""
    name = "flows_round4_summary_columns_aligned"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    slash(lua, "flow")
    problems = []
    label_pt = cal_eval(lua, "GDL.Flows.GetUI().subRows[1].label.__points[1]")
    inc_pt = cal_eval(lua, "GDL.Flows.GetUI().subRows[1].inc.__points[1]")
    exp_pt = cal_eval(lua, "GDL.Flows.GetUI().subRows[1].exp.__points[1]")
    for tag, pt in (("收入", inc_pt), ("支出", exp_pt)):
        if str(pt[3]) != "RIGHT":
            problems.append("%s 列没用右锚对齐：%s" % (tag, pt[3]))
        if as_number(pt[5]) != as_number(label_pt[5]):
            problems.append("%s 列与类型名不在同一行（y=%s vs %s）"
                            % (tag, pt[5], label_pt[5]))
    sub_w = as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_W"))
    for tag, pt, col in (("收入", inc_pt, "subInc"), ("支出", exp_pt, "subExp")):
        x = as_number(cal_eval(lua, "GDL.Flows.GEOM.COLS.%s" % col))
        if as_number(pt[4]) != -(sub_w - x):
            problems.append("%s 列右沿没与列头对齐：%s vs %s" % (tag, pt[4], -(sub_w - x)))
    subs = [s for s in flow_subs(lua) if s["shown"] == "1"]
    if not subs or any(s["labelR"] != "1.0000" for s in subs):
        problems.append("分类名常态色不该是灰：%s" % [s["labelR"] for s in subs])
    return expect(name, problems)


def test_ledger_round5_vendor_merge_writes_one_entry():
    """第 5 轮：卖店连发在**写入层**就并成一条（用户要「记录的时候就记录一条」）。
    30 笔同秒 vendor ⇒ db.entries 恰 1 条、amt 累加、mergedCount=30；
    daily 与 bySource 总量一次不少；流水/弹窗自然只有一行。
    读出层 MergeVendorRuns 留着兼容旧存档里已经散开的明细，且笔数要累加。
    方向 / 号 / 窗口 / 来源一变就断开，别的来源连发不合并。"""
    name = "ledger_round5_vendor_merge_writes_one_entry"
    dk = "2026-11-10"
    lua = calendar_env()
    lua.execute("for i = 1, 30 do GDL.Ledger:AddEntry(10000, 'vendor') end")
    problems = []

    def n_rows(l):
        return as_number(cal_eval(
            l, "(function() local r=GDL.Ledger.GetCharEntries(nil,'%s','%s',1,20) "
               "return #r.rows end)()" % (dk, dk)))

    n_saved = as_number(cal_eval(lua, "#GDLDB.entries"))
    if n_saved != 1:
        problems.append("写入层应只落 1 条，实际 %s 条" % n_saved)
    if str(cal_eval(lua, "tostring(GDLDB.entries[1].mergedCount)")) != "30":
        problems.append("写入层没记下并了几笔（mergedCount）")
    meta = cal_eval(lua, r"""(function()
        local r = GDL.Ledger.GetCharEntries(nil, '%s', '%s', 1, 20)
        local e = r.rows[1]
        return tostring(#r.rows) .. '|' .. tostring(r.total) .. '|'
            .. tostring(e and e.amt) .. '|' .. tostring(e and e.mergedCount) .. '|'
            .. tostring(GDLDB.daily['%s'].income) .. '|'
            .. tostring(r.bySource.vendor.income)
    end)()""" % (dk, dk, dk))
    if str(meta) != "1|1|300000|30|300000|300000":
        problems.append("30 笔连发应并成 1 行、金额 300000、总量不变：%s" % meta)
    slash(lua, "flow")
    shown = [r for r in flow_rows(lua) if r["shown"] == "1"]
    if len(shown) != 1 or shown[0]["src"] != "卖店 ·30笔" \
            or shown[0]["amt"] != money_full(300000):
        problems.append("弹窗应只列一行「卖店 ·30笔 30金」：%r" % shown)

    # 边界：方向不同 / 换号 / 超出 3 秒窗口 / 中间夹别的来源 / 别的来源连发
    l2 = calendar_env()
    l2.execute("GDL.Ledger:AddEntry(10000,'vendor'); GDL.Ledger:AddEntry(-5000,'vendor')")
    if n_rows(l2) != 2:
        problems.append("正负两笔 vendor 不该并在一起")
    l3 = calendar_env()
    set_char(l3, "甲"); add_entry(l3, 10000, "vendor")
    set_char(l3, "乙"); add_entry(l3, 20000, "vendor")
    if n_rows(l3) != 2:
        problems.append("两个号的卖店不该并在一起")
    l4 = calendar_env()
    set_epoch(l4, ep(2026, 11, 10)); add_entry(l4, 10000, "vendor")
    set_epoch(l4, ep(2026, 11, 10) + 10); add_entry(l4, 20000, "vendor")
    if n_rows(l4) != 2:
        problems.append("隔了 10 秒的两次卖店不该并在一起")
    l5 = calendar_env()
    add_entry(l5, 10000, "vendor"); add_entry(l5, 5000, "loot"); add_entry(l5, 20000, "vendor")
    if n_rows(l5) != 3:
        problems.append("中间夹一笔拾取就该断开合并")
    l6 = calendar_env()
    l6.execute("for i = 1, 5 do GDL.Ledger:AddEntry(10000,'loot') end")
    if n_rows(l6) != 5:
        problems.append("拾取连发是各自一笔，不该跟着合并")

    # 旧存档兼容：直接往 entries 塞散开的明细（绕过 AddEntry），读出层仍并成一行。
    old = calendar_env()
    old.execute(r"""do
        local t = GDL_EPOCH
        for i = 1, 30 do
            GDLDB.entries[#GDLDB.entries + 1] = {
                t = t, dateKey = '2026-11-10', char = 'Old-Realm',
                src = 'vendor', amt = 10000, transfer = false }
        end
    end""")
    if n_rows(old) != 1:
        problems.append("旧存档里散开的 30 条没被读出层兼容合并")
    old_meta = cal_eval(old, r"""(function()
        local r = GDL.Ledger.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 20)
        return tostring(r.rows[1] and r.rows[1].mergedCount) .. '|'
            .. tostring(r.rows[1] and r.rows[1].amt) .. '|'
            .. tostring(r.bySource.vendor.income)
    end)()""")
    if str(old_meta) != "30|300000|300000":
        problems.append("旧明细合并后的笔数/金额不对：%s" % old_meta)

    # 已合并条目与旧明细混在一起：笔数要累加，不能被覆盖成 2
    mix = calendar_env()
    mix.execute("GDL.Ledger:AddEntry(10000,'vendor'); GDL.Ledger:AddEntry(10000,'vendor')")
    mix.execute(r"""do
        local t, c = GDL_EPOCH, GDLDB.entries[1].char
        for i = 1, 20 do
            GDLDB.entries[#GDLDB.entries + 1] = {
                t = t, dateKey = '2026-11-10', char = c,
                src = 'vendor', amt = 10000, transfer = false }
        end
    end""")
    mix_meta = cal_eval(mix, r"""(function()
        local r = GDL.Ledger.GetCharEntries(nil, '2026-11-10', '2026-11-10', 1, 20)
        return tostring(#r.rows) .. '|' .. tostring(r.rows[1] and r.rows[1].mergedCount)
            .. '|' .. tostring(r.rows[1] and r.rows[1].amt)
    end)()""")
    if str(mix_meta) != "1|22|220000":
        problems.append("已并条目与旧明细混排时笔数没累加：%s" % mix_meta)
    return expect(name, problems)


def test_calendar_round5_char_class_sources():
    """第 5 轮：职业色不再只认"当前登录号"。本插件登录过的号把 token 留在
    classByChar（且不造 chars 空号）；历史号只读借用 GQLDB.chars[key].class
    （GQL 是 account-wide 存档，Sync.lua:1173-1180）；两者都没有才退白色。
    右栏角色名也要跟着用借到的真实职业色，不是白色兜底。"""
    name = "calendar_round5_char_class_sources"
    lua = calendar_env()
    ghost = "老号-%s" % REALM
    cur = "当前号-%s" % REALM
    problems = []
    if str(cal_eval(lua, "tostring(GDLDB.classByChar[%r])" % cur)) != "MAGE":
        problems.append("登录没把当前号职业写进 classByChar")
    n_chars = as_number(cal_eval(lua, r"""(function()
        local n = 0 for _ in pairs(GDLDB.chars) do n = n + 1 end return n
    end)()"""))
    if n_chars != 0:
        problems.append("只登录没记账就造出了 %s 个 chars 空号" % n_chars)

    lua.execute("GQLDB = { chars = { [%r] = { class = 'ROGUE' } } }" % ghost)
    rgb = cal_eval(lua, "GDL.Ledger.CharColor(%r)" % ghost)
    vals = [round(float(rgb[i]), 2) for i in (1, 2, 3)]
    if vals != [1.0, 0.96, 0.41]:
        problems.append("没借到 GQLDB 的 ROGUE 职业色：%s" % vals)
    if str(cal_eval(lua, "tostring(GDLDB.classByChar[%r])" % ghost)) != "ROGUE":
        problems.append("借到的职业没缓存进自己的库")
    if not str(cal_eval(lua, "GDL.Ledger.CharLabel(%r, 24)" % ghost)).startswith("|cfffff569"):
        problems.append("旧号短名没按借到的职业上色")

    # 右栏：老号要显示 ROGUE 黄，而不是白色兜底
    add_entry(lua, 1000000, "quest")
    set_char(lua, "老号")
    add_entry(lua, 200000, "vendor")
    lua.execute("GDLDB.chars[%r].class = nil; GDLDB.classByChar[%r] = 'ROGUE'" % (ghost, ghost))
    cal_open(lua)
    names = [r["name"] for r in cal_panel_rows(lua) if r["shown"] == "1"]
    if not any(n.startswith("|cfffff569老号|r") for n in names):
        problems.append("右栏里老号仍是兜底色：%s" % names)

    # GQLDB 形状不对 / 号不在里面 / 整个不存在 ⇒ 白色，且一句错都不报
    lua.execute("GQLDB = { chars = { [%r] = { class = 'NOT_A_CLASS' } } }" % ghost)
    for tag, key in (("坏 token", "生人A-%s" % REALM), ("没这个号", "生人B-%s" % REALM)):
        c = cal_eval(lua, "GDL.Ledger.CharColor(%r)" % key)
        if [round(float(c[i]), 2) for i in (1, 2, 3)] != [1.0, 1.0, 1.0]:
            problems.append("%s 应退白色，不该编色" % tag)
    lua.execute("GQLDB = nil")
    c = cal_eval(lua, "GDL.Ledger.CharColor(%r)" % ("生人C-" + REALM))
    if [round(float(c[i]), 2) for i in (1, 2, 3)] != [1.0, 1.0, 1.0]:
        problems.append("没有 GQLDB 时应退白色")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_flows_round6_dropdown_value_only_and_clipped():
    """四个下拉按钮只写选中的值（没有「筛子名 · 」前缀）；长角色名按按钮字节预算
    裁断，且职业色转义完整、UTF-8 不劈半字，避免定宽字串折行。"""
    name = "flows_round6_dropdown_value_only_and_clipped"
    long_name = "一个非常非常长的号名用来考验下拉按钮会不会折行"
    lua = calendar_env(char=long_name)
    add_entry(lua, 1000000, "quest")
    slash(lua, "flow " + long_name + "-" + REALM)
    problems = []
    prefixes = ("角色 ·", "类型 ·", "日期 ·", "收支 ·",
                "Character ·", "Type ·", "Date ·", "Flow ·")
    for key in ("char", "type", "range", "dir"):
        got = cal_text(lua, "GDL.Flows.GetUI().dropdowns.%s.label" % key)
        if any(got.startswith(p) for p in prefixes):
            problems.append("%s 下拉还带筛子名前缀：%r" % (key, got))
        if "\n" in got or "\r" in got:
            problems.append("%s 下拉文本里出现换行：%r" % (key, got))
    got = cal_text(lua, "GDL.Flows.GetUI().dropdowns.char.label")
    m = re.match(r"^\|cff([0-9a-f]{6})(.*)\|r$", got)
    if not m:
        problems.append("角色下拉没保住完整的职业色转义：%r" % got)
    else:
        body = m.group(2)
        if not long_name.startswith(body) or len(body) >= len(long_name):
            problems.append("角色名没按原名开头裁断：%r" % body)
        budget = int((as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_W[1]")) - 12) // 4)
        if len(body.encode("utf-8")) > budget:
            problems.append("角色名没按 %d 字节预算裁断：%r" % (budget, body))
        try:
            body.encode("utf-8").decode("utf-8")
        except UnicodeDecodeError:
            problems.append("裁断劈开了 UTF-8 字符：%r" % body)

    # 把预算收紧，验裁断这条路真的会走：换语言触发 Repaint 重摆按钮
    lua.execute("GDL.Flows.GetUI().dropdowns.char.clipMax = 12")
    slash(lua, "lang enUS")
    got = cal_text(lua, "GDL.Flows.GetUI().dropdowns.char.label")
    m = re.match(r"^\|cff([0-9a-f]{6})(.*)\|r$", got)
    if not m:
        problems.append("裁断把职业色转义劈开了：%r" % got)
    elif len(m.group(2).encode("utf-8")) > 12:
        problems.append("收紧预算后角色名仍超长：%r" % m.group(2))
    else:
        try:
            m.group(2).encode("utf-8").decode("utf-8")
        except UnicodeDecodeError:
            problems.append("收紧预算后裁断劈开了 UTF-8：%r" % m.group(2))
    for key in ("char", "type", "range", "dir"):
        if any(cal_text(lua, "GDL.Flows.GetUI().dropdowns.%s.label" % key).startswith(p)
               for p in prefixes):
            problems.append("换 enUS 后 %s 下拉又冒出筛子名前缀" % key)
    # 切档后按钮跟着换成新值
    if not flow_dd_select(lua, "range", "week"):
        problems.append("日期下拉缺本周档")
    else:
        want = str(cal_eval(lua, "GDL.Flows.GetUI().dropdowns.range.currentLabel"))
        if cal_text(lua, "GDL.Flows.GetUI().dropdowns.range.label") != want:
            problems.append("切档后日期下拉没换成新值")
    slash(lua, "lang zhCN")
    return expect(name, problems)


def test_calendar_round7_summary_strip():
    """第 7 轮 + 2026-11-11：顶部三个读数搬到日历下面，改成六个方块贯穿窗底
    （本周/上周/本月/全部/战团/总计）；横线 + 五条竖分格，文字上沿不贴横线；
    悬停分别给确切范围 + 收入/支出、战团金币/未访问说明、角色+战团总计。"""
    name = "calendar_round7_summary_strip"
    lua = calendar_env(day=(2026, 11, 10))
    lua.execute("GDL_WEEK_RESET_SECONDS = 172800")   # 本周 CD 起点 2026-11-05（周四）
    set_epoch(lua, ep(2026, 11, 10))
    add_entry(lua, 1000000, "quest")                 # 11-10：100 金
    set_epoch(lua, ep(2026, 10, 30))
    add_entry(lua, 200000, "loot")                   # 10-30：20 金（落在上周）
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    problems = []
    ui = "GDL.Calendar.GetUI()"
    # 顶栏读数控件必须真的拆掉：导航行只剩 < 月份 >
    if cal_eval(lua, "%s.totals.week.host.__points[1]" % ui) is None:
        problems.append("读数方块没建出宿主按钮")
    div = "%s.summaryDiv" % ui
    if cal_eval(lua, div) is None:
        problems.append("缺日历与读数带之间的横线")
    else:
        if as_number(cal_eval(lua, "%s:GetWidth()" % div)) != cal_geom(lua, "SUMMARY_W"):
            problems.append("横线没铺满整个窗底")
        if as_number(cal_eval(lua, "%s:GetHeight()" % div)) != 1:
            problems.append("横线不是 1 像素")
        if as_number(cal_eval(lua, "%s.__points[1][5]" % div)) \
                != -(cal_geom(lua, "SUMMARY_TOP") + cal_geom(lua, "SUMMARY_DIV_GAP")):
            problems.append("横线没钉在网格底下方")
    if as_number(cal_eval(lua, "#%s.summaryVDivs" % ui)) != 5:
        problems.append("六个方块之间应有 5 条竖分格")
    if cal_geom(lua, "BLOCK_PAD") < 8:
        problems.append("文字上沿贴着分割线了（BLOCK_PAD=%s）" % cal_geom(lua, "BLOCK_PAD"))

    wants = {
        "week": ("本周", cal_money_icon(1000000)),
        "lastweek": ("上周", cal_money_icon(200000)),
        "month": ("本月", cal_money_icon(1000000)),
        "all": ("全部", cal_money_icon(1200000)),
    }
    for p, (label, value) in wants.items():
        slot = "%s.totals.%s" % (ui, p)
        if cal_text(lua, "%s.label" % slot) != label:
            problems.append("%s 方块标签应是 %s，实际 %r"
                            % (p, label, cal_text(lua, "%s.label" % slot)))
        if cal_text(lua, "%s.value" % slot) != value:
            problems.append("%s 方块净额应是 %r，实际 %r"
                            % (p, value, cal_text(lua, "%s.value" % slot)))
        host_y = -as_number(cal_eval(lua, "%s.host.__points[1][5]" % slot))
        if host_y < cal_geom(lua, "SUMMARY_TOP"):
            problems.append("%s 方块没在日历下方（y=%s）" % (p, host_y))
        lab_y = as_number(cal_eval(lua, "%s.label.__points[1][5]" % slot))
        if lab_y != -cal_geom(lua, "BLOCK_PAD"):
            problems.append("%s 文字没按 BLOCK_PAD 离分割线留白：%s" % (p, lab_y))

    # 六个方块从左到右排满整条读数带（最后一块吸收 892 除不尽的 4px）
    order = ["week", "lastweek", "month", "all", "warband", "assets"]
    for i, p in enumerate(order):
        slot = "%s.totals.%s" % (ui, p)
        want_left = cal_geom(lua, "TOTALS_LEFT") + i * cal_geom(lua, "BAND_W")
        if as_number(cal_eval(lua, "%s.left" % slot)) != want_left:
            problems.append("%s 方块左沿应是 %s" % (p, want_left))
        if p == "assets":
            if as_number(cal_eval(lua, "%s.right" % slot)) \
                    != cal_geom(lua, "WIN_W") - cal_geom(lua, "PAD"):
                problems.append("总计方块没贴住窗内右沿")
        elif as_number(cal_eval(lua, "%s.right" % slot)) != want_left + cal_geom(lua, "BAND_W"):
            problems.append("%s 方块宽度不是等分宽" % p)

    # 战团：本环境没读过战团银行 ⇒ 显示「—」；按要求不再挂悬停；不许写 0
    if cal_text(lua, "%s.totals.warband.label" % ui) != "战团":
        problems.append("战团方块标签不对：%r" % cal_text(lua, "%s.totals.warband.label" % ui))
    if cal_text(lua, "%s.totals.warband.value" % ui) != "—":
        problems.append("未访问战团银行时应显示 —，实际 %r"
                        % cal_text(lua, "%s.totals.warband.value" % ui))
    if cal_eval(lua, "%s.totals.warband.host:GetScript('OnEnter')" % ui) is not None:
        problems.append("战团方块不该再挂悬停")

    # 总计：登录时读到当前号 0 ⇒ 0 金（已知），角色 1 个、战团未读
    if cal_text(lua, "%s.totals.assets.label" % ui) != "总计":
        problems.append("总计方块标签不对")
    if cal_text(lua, "%s.totals.assets.value" % ui) != cal_money_icon(0):
        problems.append("总计应等于已知角色 0 金，实际 %r"
                        % cal_text(lua, "%s.totals.assets.value" % ui))
    cal_script(lua, "%s.totals.assets.host" % ui, "OnEnter")
    lines = cal_tip_frame_lines(lua)
    if len(lines) != 4 or lines[0] != "总计" \
            or lines[1] != "角色 %s" % cal_money_icon(0) \
            or lines[2] != "战团 —" \
            or lines[3] != "仅统计已登录过的角色":
        problems.append("总计方块悬停内容不对：%s" % lines)
    cal_script(lua, "%s.totals.assets.host" % ui, "OnLeave")

    # 上周范围 = 本周 CD 起点往前 7 天到前一天
    st_range = cal_eval(lua, r"""(function()
        local s, e = GDL.Calendar.GetRange('lastweek')
        return tostring(s) .. '|' .. tostring(e)
    end)()""")
    if str(st_range) != "2026-10-29|2026-11-04":
        problems.append("上周 CD 范围应是 10-29..11-04，实际 %s" % st_range)

    # 悬停：4 行 = 档位 / 确切范围 / 收入 / 支出（范围不再和档位词挤一行折行）
    for p, label, rng, inc, exp in (
        ("week", "本周", "2026-11-05~2026-11-10", 1000000, 0),
        ("lastweek", "上周", "2026-10-29~2026-11-04", 200000, 0),
    ):
        cal_script(lua, "%s.totals.%s.host" % (ui, p), "OnEnter")
        lines = cal_tip_frame_lines(lua)
        if len(lines) != 4 or lines[0] != label \
                or lines[1] != rng \
                or lines[2] != "收入 %s" % cal_money_icon(inc) \
                or lines[3] != "支出 %s" % cal_money_icon(exp):
            problems.append("%s 方块悬停内容不对：%s" % (p, lines))
        if not cal_state(lua)["tipShown"]:
            problems.append("%s 方块悬停没把提示框显示出来" % p)
        cal_script(lua, "%s.totals.%s.host" % (ui, p), "OnLeave")

    # 「全部」的日期行 = 真实记录首日 ~ 今天，不许再写哨兵 0000-01-01~9999-12-31
    #（用户 2026-11-10 截图：那串占位日期没有信息量）。本环境最早一条是「上周」
    # 那笔回填在 2026-10-30，起点就该是它 —— 正好证明用的是真实首日而不是今天。
    cal_script(lua, "%s.totals.all.host" % ui, "OnEnter")
    lines = cal_tip_frame_lines(lua)
    if len(lines) != 4 or lines[0] != "全部" \
            or lines[1] != "2026-10-30~2026-11-10" \
            or lines[2] != "收入 %s" % cal_money_icon(1200000) \
            or lines[3] != "支出 %s" % cal_money_icon(0):
        problems.append("全部 方块悬停日期该用真实记录起止：%s" % lines)
    cal_script(lua, "%s.totals.all.host" % ui, "OnLeave")

    # 空账本：「全部」连日期行都不要写（用户定稿：要么真实起止、要么不写日期）
    lua2 = calendar_env()
    cal_open(lua2)
    cal_script(lua2, "GDL.Calendar.GetUI().totals.all.host", "OnEnter")
    lines2 = cal_tip_frame_lines(lua2)
    if len(lines2) != 3 or lines2[0] != "全部" \
            or lines2[1] != "收入 %s" % cal_money_icon(0) \
            or lines2[2] != "支出 %s" % cal_money_icon(0):
        problems.append("空账本的「全部」悬停不该写日期行：%s" % lines2)
    cal_script(lua2, "GDL.Calendar.GetUI().totals.all.host", "OnLeave")
    if cal_state(lua2)["errors"] != 0:
        problems.append("空账本悬停报错")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_summary_blocks_open_range_flows():
    """2026-11-13 玩家需求：底部 本周 / 上周 / 本月 / 全部 四块也接点击，点了和点角色一样
    开流水，只是筛的维度换成时间 —— 范围就是方块上那个数算出来的那段（本周是 CD 至今，
    不许被重算成整周 CD），角色 / 类型 / 收支 / 页码一律回默认；战团 / 总计不是时间范围，
    不接点击。本月跟着日历显示月走，换语言后标题与日期下拉一起重拼。"""
    name = "calendar_summary_blocks_open_range_flows"
    lua = calendar_env(day=(2026, 11, 10))
    lua.execute("GDL_WEEK_RESET_SECONDS = 172800")   # 本周 CD 起点 2026-11-05（周四）
    set_epoch(lua, ep(2026, 11, 10)); add_entry(lua, 1000000, "quest")   # 本周 / 本月
    set_epoch(lua, ep(2026, 11, 9)); add_entry(lua, -20000, "repair")    # 本周 / 本月（支出）
    set_char(lua, "别的号")
    set_epoch(lua, ep(2026, 11, 2)); add_entry(lua, 300000, "loot")      # 上周 / 本月，另一个号
    set_char(lua, "当前号")
    set_epoch(lua, ep(2026, 10, 20)); add_entry(lua, -50000, "repair")   # 只进「全部」与 10 月
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    problems = []
    ui = "GDL.Calendar.GetUI()"
    fui = "GDL.Flows.GetUI()"

    def flow_state():
        keys = ("charKey", "source", "preset", "direction", "anchorKey",
                "startKey", "endKey", "rangeText", "page")
        out = {}
        for k in keys:
            v = cal_eval(lua, "GDL.Flows.GetState().%s" % k)
            out[k] = None if v is None else (as_number(v) if k == "page" else str(v))
        return out

    def n_rows():
        return len([r for r in flow_rows(lua) if r["shown"] == "1"])

    def flow_shown():
        return bool(cal_eval(lua, "GDLFlowFrame and GDLFlowFrame:IsShown() or false"))

    # 四块各自：范围 / 标题与下拉同一套说法 / 行数 / 收支总计与方块上那个净同源
    cases = [
        ("week", "2026-11-05", "2026-11-10", "本周 11-05~11-10", 2, 1000000, 20000),
        ("lastweek", "2026-10-29", "2026-11-04", "上周 10-29~11-04", 1, 300000, 0),
        ("month", "2026-11-01", "2026-11-30", "本月 2026-11", 3, 1300000, 20000),
        ("all", "0000-01-01", "9999-12-31", "全部", 4, 1300000, 70000),
    ]
    for preset, s, e, text, rows, inc, exp in cases:
        slot = "%s.totals.%s" % (ui, preset)
        # 先悬停把提示弹出来：点击必须把它收掉，别浮在刚开的弹窗脸上
        cal_script(lua, "%s.host" % slot, "OnEnter")
        cal_click(lua, "%s.host" % slot)
        if not flow_shown():
            problems.append("点 %s 方块没开流水弹窗" % preset)
            continue
        if cal_state(lua)["tipShown"]:
            problems.append("点 %s 方块没把悬停提示收掉" % preset)
        st = flow_state()
        if (st["startKey"], st["endKey"]) != (s, e):
            problems.append("%s 方块点开的范围应 %s..%s，实际 %s..%s"
                            % (preset, s, e, st["startKey"], st["endKey"]))
        if st["preset"] != preset:
            problems.append("%s 方块点开后日期档应是 %s，实际 %s" % (preset, preset, st["preset"]))
        # 其余筛子全回默认：全部角色 / 全部类型 / 收支全收 / 第 1 页
        if (st["charKey"], st["source"], st["direction"], st["page"]) != (None, None, "all", 1):
            problems.append("%s 方块点开没把其余筛子回默认：%s" % (preset, st))
        for key, want in (("char", "全部角色"), ("type", "全部类型"), ("dir", "全部")):
            got = cal_text(lua, "%s.dropdowns.%s.label" % (fui, key))
            if got != want:
                problems.append("%s 方块点开后 %s 下拉应写 %r，实际 %r" % (preset, key, want, got))
        title = cal_text(lua, "%s.title" % fui)
        if title != "流水 · 全部角色 · %s" % text:
            problems.append("%s 方块点开标题应是 流水 · 全部角色 · %s，实际 %r"
                            % (preset, text, title))
        dd = cal_text(lua, "%s.dropdowns.range.label" % fui)
        if dd != text:
            problems.append("%s 方块点开后日期下拉应写 %r（与标题同一套），实际 %r"
                            % (preset, text, dd))
        if n_rows() != rows:
            problems.append("%s 方块点开应列 %d 行，实际 %d" % (preset, rows, n_rows()))
        got_inc = cal_text(lua, "%s.subTotal.inc" % fui)
        got_exp = cal_text(lua, "%s.subTotal.exp" % fui)
        if got_inc != cal_money_icon(inc) or got_exp != cal_money_icon(exp):
            problems.append("%s 方块点开的收支总计应是 %s / %s，实际 %s / %s"
                            % (preset, cal_money_icon(inc), cal_money_icon(exp), got_inc, got_exp))
        if cal_text(lua, "%s.value" % slot) != cal_money_icon(inc - exp):
            problems.append("%s 方块上的净应是 %r，实际 %r"
                            % (preset, cal_money_icon(inc - exp), cal_text(lua, "%s.value" % slot)))

    # 日期下拉六档都在（上周是新加的），未选中的档按今天锚：本周是整周 CD
    labels = flow_dd_labels(lua, "range")
    for want in ("本日 2026-11-10", "昨日 2026-11-09", "本周 11-05~11-11",
                 "上周 10-29~11-04", "本月 2026-11", "全部"):
        if want not in labels:
            problems.append("日期下拉缺 %r：%s" % (want, labels))
    if not flow_dd_select(lua, "range", "lastweek"):
        problems.append("日期下拉点不动上周档")
    elif (str(cal_eval(lua, "GDL.Flows.GetState().startKey")),
          str(cal_eval(lua, "GDL.Flows.GetState().endKey"))) != ("2026-10-29", "2026-11-04"):
        problems.append("下拉选上周应按今天锚回 10-29..11-04，实际 %s..%s"
                        % (cal_eval(lua, "GDL.Flows.GetState().startKey"),
                           cal_eval(lua, "GDL.Flows.GetState().endKey")))

    # 本月跟着日历显示月：翻到 10 月再点，范围 / 标题 / 下拉里的本月都得是 10 月
    cal_click(lua, "%s.close" % fui)
    cal_click(lua, "%s.btnPrevMo" % ui)
    cal_click(lua, "%s.totals.month.host" % ui)
    st = flow_state()
    if (st["startKey"], st["endKey"]) != ("2026-10-01", "2026-10-31"):
        problems.append("本月方块应跟着显示月走（10 月），实际 %s..%s"
                        % (st["startKey"], st["endKey"]))
    if cal_text(lua, "%s.title" % fui) != "流水 · 全部角色 · 本月 2026-10":
        problems.append("10 月的本月方块标题不对：%r" % cal_text(lua, "%s.title" % fui))
    if n_rows() != 1:
        problems.append("10 月只有一笔，实际列了 %d 行" % n_rows())
    if "本月 2026-10" not in flow_dd_labels(lua, "range"):
        problems.append("日期下拉的本月档没跟着显示月：%s" % flow_dd_labels(lua, "range"))
    # 再点一次「本月」也不许跳回今天所在的 11 月：锚必须落在显示月里
    if not flow_dd_select(lua, "range", "month"):
        problems.append("日期下拉点不动本月档")
    elif str(cal_eval(lua, "GDL.Flows.GetState().startKey")) != "2026-10-01":
        problems.append("再点本月档跳回了别的月：%s"
                        % cal_eval(lua, "GDL.Flows.GetState().startKey"))

    # 战团 / 总计不是时间范围：不挂 OnClick，直接调也不开
    for p in ("warband", "assets"):
        if cal_eval(lua, "%s.totals.%s.host:GetScript('OnClick')" % (ui, p)) is not None:
            problems.append("%s 方块不该接点击（它不是时间范围）" % p)
        if bool(cal_eval(lua, "GDL.Calendar.OpenRangeFlow(%r)" % p)):
            problems.append("OpenRangeFlow(%s) 应返回 false" % p)
    if (str(cal_eval(lua, "GDL.Flows.GetState().startKey")), n_rows()) != ("2026-10-01", 1):
        problems.append("点战团/总计不该动已经开着的流水")

    # 换语言：范围说法是存下来的串，标题与日期下拉都得重拼，不许半中半英
    slash(lua, "lang enUS")
    if cal_text(lua, "%s.title" % fui) != "Flows · All characters · Month 2026-10":
        problems.append("换 enUS 后标题没重拼范围说法：%r" % cal_text(lua, "%s.title" % fui))
    if cal_text(lua, "%s.dropdowns.range.label" % fui) != "Month 2026-10":
        problems.append("换 enUS 后日期下拉没重拼范围说法：%r"
                        % cal_text(lua, "%s.dropdowns.range.label" % fui))
    slash(lua, "lang zhCN")

    # 页码也回默认：本周 25 笔 ⇒ 翻得到第 2 页，把四个筛子全拨离默认后再点方块，
    # 必须整套回默认（不是沿用上一次弹窗的筛选），再点同一块也稳定
    lua3 = calendar_env(day=(2026, 11, 10))
    lua3.execute("GDL_WEEK_RESET_SECONDS = 172800")
    for i in range(25):
        set_epoch(lua3, ep(2026, 11, 10, 12, i))
        add_entry(lua3, 10000, "quest")
    cal_open(lua3)
    ui3 = "GDL.Calendar.GetUI()"
    cal_click(lua3, "%s.totals.week.host" % ui3)
    if not flow_dd_select(lua3, "char", "当前号-%s" % REALM) \
            or not flow_dd_select(lua3, "type", "quest") \
            or not flow_dd_select(lua3, "dir", "in"):
        problems.append("拨筛选用的下拉选项缺了")
    cal_click(lua3, "%s.page.next" % fui)
    if as_number(cal_eval(lua3, "GDL.Flows.GetState().page")) != 2:
        problems.append("25 笔应翻得到第 2 页")
    cal_click(lua3, "%s.totals.week.host" % ui3)
    st3 = {k: cal_eval(lua3, "GDL.Flows.GetState().%s" % k)
           for k in ("charKey", "source", "direction", "page")}
    if (st3["charKey"], st3["source"], str(st3["direction"]), as_number(st3["page"])) \
            != (None, None, "all", 1):
        problems.append("点方块没把角色/类型/收支/页码回默认：%s" % st3)
    if len([r for r in flow_rows(lua3) if r["shown"] == "1"]) != 20:
        problems.append("回第 1 页应摆满 20 行")
    if cal_state(lua3)["errors"] != 0:
        problems.append("拨筛选后点方块报错")

    # 没墙上钟：本周 / 上周算不出范围 ⇒ 点了不开（不拿显示月凑一段）；「全部」不靠钟，照开
    lua2 = calendar_env()
    set_clock_none(lua2)
    cal_open(lua2)
    ui2 = "GDL.Calendar.GetUI()"
    for p in ("week", "lastweek"):
        if bool(cal_eval(lua2, "GDL.Calendar.OpenRangeFlow(%r)" % p)):
            problems.append("没墙上钟时 %s 方块不该开流水" % p)
    if bool(cal_eval(lua2, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("没墙上钟时点周方块把弹窗打开了")
    cal_click(lua2, "%s.totals.all.host" % ui2)
    if not bool(cal_eval(lua2, "GDLFlowFrame and GDLFlowFrame:IsShown() or false")):
        problems.append("没墙上钟时「全部」方块仍应能开流水（哨兵两端不靠钟）")
    elif str(cal_eval(lua2, "GDL.Flows.GetState().endKey")) != "9999-12-31":
        problems.append("没墙上钟时「全部」范围不对：%s"
                        % cal_eval(lua2, "GDL.Flows.GetState().endKey"))
    if cal_state(lua2)["errors"] != 0:
        problems.append("没墙上钟点方块报错")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_gold_snapshots_and_assets():
    """2026-11-11 玩家需求：登录过才有的角色金币快照 + 战团银行金币 + 两者总计。
    没快照的号悬停写「—」；总计只累加已知值并注明口径；战团读数缓存后不清空。"""
    name = "calendar_gold_snapshots_and_assets"
    problems = []
    lua = calendar_env()                 # 当前号登录时读到 0
    change_money(lua, 500000)            # 当前号快照 50 万
    set_char(lua, "甲")
    set_money(lua, 200000)
    fire(lua, "PLAYER_LOGIN")            # 甲登录 ⇒ 快照 20 万
    add_entry(lua, 300000, "quest")      # 甲今天有一笔，右栏才列得出来
    set_char(lua, "乙")
    add_entry(lua, 100000, "quest")      # 乙没登录过 ⇒ 没有金币快照
    lua.execute("GDL_WARBAND_GOLD = 1000000")
    fire(lua, "BANKFRAME_OPENED")        # 战团银行 100 万
    cal_open(lua)
    ui = "GDL.Calendar.GetUI()"

    vals = cal_eval(lua, r"""(function()
        local t = {}
        for _, v in pairs(GDLDB.goldByChar) do t[#t+1] = v end
        table.sort(t)
        return table.concat(t, ',')
    end)()""")
    if str(vals) != "200000,500000":
        problems.append("角色金币快照应只有 50 万 / 20 万，实际 %s" % vals)
    if as_number(cal_eval(lua, "GDL.Ledger.GetWarbandGold()")) != 1000000:
        problems.append("战团银行金币没落档")

    # 战团 / 总计（50 万 + 20 万 + 100 万；乙没快照不参与，也不编 0）
    if cal_text(lua, "%s.totals.warband.value" % ui) != cal_money_icon(1000000):
        problems.append("战团方块应显示 100 万，实际 %r"
                        % cal_text(lua, "%s.totals.warband.value" % ui))
    if cal_text(lua, "%s.totals.assets.value" % ui) != cal_money_icon(1700000):
        problems.append("总计应显示 170 万，实际 %r"
                        % cal_text(lua, "%s.totals.assets.value" % ui))
    cal_script(lua, "%s.totals.assets.host" % ui, "OnEnter")
    lines = cal_tip_frame_lines(lua)
    if len(lines) != 4 or lines[0] != "总计" \
            or lines[1] != "角色 %s" % cal_money_icon(700000) \
            or lines[2] != "战团 %s" % cal_money_icon(1000000) \
            or lines[3] != "仅统计已登录过的角色":
        problems.append("总计悬停口径不对：%s" % lines)
    cal_script(lua, "%s.totals.assets.host" % ui, "OnLeave")
    if cal_eval(lua, "%s.totals.warband.host:GetScript('OnEnter')" % ui) is not None:
        problems.append("战团方块不该再挂悬停")

    # 右栏：当前号乙无快照 ⇒ 金币 —；甲有 20 万快照 ⇒ 照实显示
    if not cal_text(lua, "%s.charRows[1].name" % ui).endswith("乙|r"):
        problems.append("当前号乙应排在右栏第一行")
    cal_script(lua, "%s.charRows[1].host" % ui, "OnEnter")
    lines = cal_tip_frame_lines(lua)
    if len(lines) != 3 or lines[2] != "金币 —":
        problems.append("没登录过的号应显示「金币 —」，实际 %s" % lines)
    cal_script(lua, "%s.charRows[1].host" % ui, "OnLeave")
    cal_script(lua, "%s.charRows[3].host" % ui, "OnEnter")
    lines = cal_tip_frame_lines(lua)
    if len(lines) != 3 or lines[2] != "金币 %s" % cal_money_icon(200000):
        problems.append("甲的行应显示 20 万金币，实际 %s" % lines)
    cal_script(lua, "%s.charRows[3].host" % ui, "OnLeave")

    # ACCOUNT_MONEY 变动刷新；之后读不到（nil）也不许把旧值清掉
    lua.execute("GDL_WARBAND_GOLD = 1200000")
    fire(lua, "ACCOUNT_MONEY")
    if as_number(cal_eval(lua, "GDL.Ledger.GetWarbandGold()")) != 1200000:
        problems.append("ACCOUNT_MONEY 没刷新战团金币")
    lua.execute("GDL_WARBAND_GOLD = nil")
    fire(lua, "BANKFRAME_OPENED")
    if as_number(cal_eval(lua, "GDL.Ledger.GetWarbandGold()")) != 1200000:
        problems.append("读不到战团金币时把缓存清掉了")

    # 没访问过战团银行：客户端就算答 0 也不落档；访问后 0 才是已知的 0
    lua2 = ledger_env()
    lua2.execute("GDL_WARBAND_GOLD = 0")
    boot(lua2, 1000)
    if cal_eval(lua2, "GDLDB.warbandGold") is not None:
        problems.append("没访问战团银行就把 0 落档了")
    fire(lua2, "BANKFRAME_OPENED")
    if as_number(cal_eval(lua2, "GDLDB.warbandGold")) != 0:
        problems.append("访问战团银行后应记录已知的 0")

    # 旧存档缺 goldByChar、且战团没数据 ⇒ 总计显示「—」，读数接口不报错
    lua3 = calendar_env()
    lua3.execute("GDLDB.goldByChar = nil; GDLDB.warbandGold = nil")
    cal_open(lua3)
    ui3 = "GDL.Calendar.GetUI()"
    if cal_text(lua3, "%s.totals.assets.value" % ui3) != "—":
        problems.append("没有任何已知金币时总计应显示 —")
    cal_script(lua3, "%s.totals.assets.host" % ui3, "OnEnter")
    lines = cal_tip_frame_lines(lua3)
    if len(lines) != 4 or lines[1] != "角色 —" or lines[2] != "战团 —":
        problems.append("空资产口径不对：%s" % lines)
    cal_script(lua3, "%s.totals.assets.host" % ui3, "OnLeave")
    if cal_eval(lua3, "GDL.Ledger.GetCharGold('x')") is not None:
        problems.append("旧存档缺 goldByChar 时 GetCharGold 应返回 nil")
    if cal_state(lua3)["errors"] != 0:
        problems.append("旧存档缺 goldByChar 时报错")

    if cal_state(lua)["errors"] != 0:
        problems.append("金币快照/战团读数路径报错")
    return expect(name, problems)


def test_flows_round7_category_total_and_dividers():
    """第 7 轮：分类列头「分类小计」→「分类」；底部加「总计」；列表与分类之间
    竖分割线、总计上方横分割线；四个下拉适当收短。"""
    name = "flows_round7_category_total_and_dividers"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")
    add_entry(lua, -200000, "repair")
    slash(lua, "flow")
    problems = []
    ui = "GDL.Flows.GetUI()"
    if cal_text(lua, "%s.subHead.label" % ui) != "分类":
        problems.append("分类列头没改名：%r" % cal_text(lua, "%s.subHead.label" % ui))
    if cal_eval(lua, "%s.subTotal" % ui) is None:
        problems.append("缺底部总计行")
    else:
        if cal_text(lua, "%s.subTotal.label" % ui) != "总计":
            problems.append("总计行标签不对：%r" % cal_text(lua, "%s.subTotal.label" % ui))
        if cal_text(lua, "%s.subTotal.inc" % ui) != cal_money_icon(1000000):
            problems.append("总计收入应是 %r，实际 %r"
                            % (cal_money_icon(1000000), cal_text(lua, "%s.subTotal.inc" % ui)))
        if cal_text(lua, "%s.subTotal.exp" % ui) != cal_money_icon(200000):
            problems.append("总计支出应是 %r，实际 %r"
                            % (cal_money_icon(200000), cal_text(lua, "%s.subTotal.exp" % ui)))
        if not bool(cal_eval(lua, "%s.subTotal.btn:IsShown()" % ui)):
            problems.append("有流水时总计行该显示")
        y = -as_number(cal_eval(lua, "%s.subTotal.btn.__points[1][5]" % ui))
        list_y = as_number(cal_eval(lua, "GDL.Flows.GEOM.PAD + GDL.Flows.GEOM.TITLE_H "
                                        "+ GDL.Flows.GEOM.FILTER_H + GDL.Flows.GEOM.HEAD_H"))
        if y != list_y + as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_TOTAL_Y")):
            problems.append("总计行没落在分类列底部")
    if cal_eval(lua, "%s.subDiv" % ui) is None:
        problems.append("缺列表/分类之间的竖分割线")
    else:
        if as_number(cal_eval(lua, "%s.subDiv:GetWidth()" % ui)) != 1:
            problems.append("竖分割线不是 1 像素")
        x = as_number(cal_eval(lua, "%s.subDiv.__points[1][4]" % ui))
        sub_x = as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_X"))
        gap = as_number(cal_eval(lua, "GDL.Flows.GEOM.SUB_GAP"))
        if x != sub_x - int(gap // 2):
            problems.append("竖分割线没落在两栏中间：x=%s vs %s" % (x, sub_x - int(gap // 2)))
    if cal_eval(lua, "%s.subTotalDiv" % ui) is None:
        problems.append("缺总计行上方的横分割线")
    elif as_number(cal_eval(lua, "%s.subTotalDiv:GetHeight()" % ui)) != 1:
        problems.append("总计横分割线不是 1 像素")
    widths = [as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_W[%d]" % i)) for i in range(1, 5)]
    if sum(widths) >= 620:
        problems.append("四个下拉没按反馈收短：%s" % widths)
    if sum(widths) + 3 * as_number(cal_eval(lua, "GDL.Flows.GEOM.DD_GAP")) \
            + 2 * as_number(cal_eval(lua, "GDL.Flows.GEOM.PAD")) \
            > as_number(cal_eval(lua, "GDL.Flows.GEOM.WIN_W")):
        problems.append("收短后四个下拉仍排不进窗宽")
    return expect(name, problems)


def test_gcl_raise_front_above_gql_peers():
    """跨插件层序（2026-11-11 统一协议）：GDL 与 GQL 的窗全是 DIALOG + SetToplevel，
    谁被点 / 谁最后开谁在最前 ⇒ RaiseFront 只 Raise 本窗、把 × 钉回本窗层号 +1，
    绝不再改写对端窗的 strata/层号（旧版拉对端回 MEDIUM 正是层序随机的根因）。
    GQL 缺席 / 对方没建窗都不许报错。"""
    name = "gcl_raise_front_above_gql_peers"
    lua = calendar_env()
    lua.execute(r"""
        GQL_FAKE_MATRIX = CreateFrame('Frame', 'GQLFakeMatrix', UIParent)
        GQL_FAKE_MATRIX:SetFrameStrata('DIALOG')
        GQL_FAKE_MATRIX:SetFrameLevel(40)
        GQL = { Matrix = { Frame = function() return GQL_FAKE_MATRIX end } }
    """)
    cal_open(lua)
    problems = []
    cal_lvl = as_number(cal_eval(lua, "GDLCalFrame:GetFrameLevel()"))
    if cal_lvl is None or cal_lvl < 40:
        problems.append("日历主窗开窗没把自己 Raise 到 GQL(40) 之上：层号 %s" % cal_lvl)
    if str(cal_eval(lua, "GQL_FAKE_MATRIX.__strata")) != "DIALOG":
        problems.append("统一协议不许碰对端窗：GQL 的 strata 被改了")
    if as_number(cal_eval(lua, "GQL_FAKE_MATRIX:GetFrameLevel()")) != 40:
        problems.append("统一协议不许碰对端窗：GQL 的层号被改了")
    if as_number(cal_eval(lua, "GDL.Calendar.GetUI().close:GetFrameLevel()")) != cal_lvl + 1:
        problems.append("主窗 × 没钉回本窗层号 +1")

    slash(lua, "flow")
    flow_lvl = as_number(cal_eval(lua, "GDLFlowFrame:GetFrameLevel()"))
    if flow_lvl is None or flow_lvl < cal_lvl:
        problems.append("流水弹窗没 Raise 到日历层号之上：%s vs %s" % (flow_lvl, cal_lvl))
    if as_number(cal_eval(lua, "GDL.Flows.GetUI().close:GetFrameLevel()")) != flow_lvl + 1:
        problems.append("弹窗 × 没钉回本窗层号 +1")

    # 点日历主窗：自抬一次，同样不碰对端
    cal_script(lua, "GDLCalFrame", "OnMouseDown")
    if as_number(cal_eval(lua, "GDLCalFrame:GetFrameLevel()")) < cal_lvl:
        problems.append("点日历主窗没把自己 Raise 回最上")
    if str(cal_eval(lua, "GQL_FAKE_MATRIX.__strata")) != "DIALOG":
        problems.append("点日历主窗改写了 GQL 的 strata")
    # GQL 没装时同样不许报错
    lua.execute("GQL = nil")
    cal_script(lua, "GDLFlowFrame", "OnMouseDown")
    if cal_state(lua)["errors"] != 0:
        problems.append("GQL 缺席时抬层把绘制带崩了")
    return expect(name, problems)


def test_gcl_windows_set_toplevel():
    """原生点击置顶：日历主窗与流水弹窗都要挂 SetToplevel(true)，且 strata 与 GQL 矩阵
    同为 DIALOG（同 strata 里 toplevel 才能互相压）。GQL 缺席时同样不许报错。"""
    name = "gcl_windows_set_toplevel"
    lua = calendar_env()
    cal_open(lua)
    problems = []
    if str(cal_eval(lua, "tostring(GDLCalFrame.__toplevel)")) != "true":
        problems.append("日历主窗没挂 SetToplevel(true)")
    if str(cal_eval(lua, "GDLCalFrame.__strata")) != "DIALOG":
        problems.append("日历主窗 strata 该是 DIALOG（与 GQL 同层才能点击置顶），实际 %s"
                        % cal_eval(lua, "GDLCalFrame.__strata"))
    slash(lua, "flow")
    if str(cal_eval(lua, "tostring(GDLFlowFrame.__toplevel)")) != "true":
        problems.append("流水弹窗没挂 SetToplevel(true)")
    if str(cal_eval(lua, "GDLFlowFrame.__strata")) != "DIALOG":
        problems.append("流水弹窗 strata 该是 DIALOG，实际 %s"
                        % cal_eval(lua, "GDLFlowFrame.__strata"))
    # GQL 缺席也要照常挂上
    lua.execute("GQL = nil")
    if cal_state(lua)["errors"] != 0:
        problems.append("GQL 缺席时挂 SetToplevel 报错")
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
        test_money_quest_ttl_recovers_after_loot_window,
        test_money_transfer_queue_rules,
        test_money_repair_flag,
        test_money_ah_mail_attribution,
        test_money_token_buy_marks_token,
        test_money_token_buy_amount_mismatch,
        test_money_token_sell_mail,
        test_money_token_name_unavailable_uses_locale,
        test_money_secret_value_guard,
        test_money_precedence_and_single_emission,
        test_money_probe_command_without_optional_api,
        test_money_without_ledger_logs_once,
        test_ledger_consistency_stores,
        test_ledger_transfers_flow_but_not_totals,
        test_ledger_token_counts_normally,
        test_ledger_day_boundary,
        test_ledger_month_math,
        test_ledger_cleanup_all_chars,
        test_ledger_entries_cap_drops_oldest,
        test_ledger_char_key_unreadable_drops_write,
        test_ledger_get_char_entries_paging,
        test_ledger_wallclock_sources_and_command,
        test_calendar_builds_nothing_until_open,
        test_calendar_static_guards,
        test_calendar_money_format_compact_gold_only,
        test_calendar_geometry_budget,
        test_calendar_grid_shapes_and_weekday_math,
        test_calendar_cell_content_today_and_colors,
        test_calendar_text_clipping_is_utf8_safe,
        test_calendar_panel_rows_and_row_click,
        test_calendar_popup_replaces_embedded_flow_page,
        test_calendar_panel_scroll_wheel_and_row_pool,
        test_calendar_range_presets,
        test_calendar_range_tabs_mark_active,
        test_calendar_top_totals_and_missing_clock,
        test_calendar_tooltip_only_in_month_cells,
        test_calendar_esc_list_and_close_button_level,
        test_calendar_probe_ui_is_read_only,
        test_calendar_open_without_ledger_does_not_error,
        test_locale_tables_are_two_language_complete,
        test_locale_values_use_only_font_safe_glyphs,
        test_locale_selection_and_fallbacks,
        test_locale_detect_reads_realm_bytes_not_text_locale,
        test_locale_auto_detect_wired_at_login,
        test_locale_login_reads_realm_bytes_and_choice_wins,
        test_gcl_lang_stores_applies_and_repaints,
        test_gcl_lang_auto_clears_choice_and_bad_code_is_rejected,
        test_probe_reports_t4_language_and_art_evidence,
        test_bundled_icon_tga_is_shipped_and_valid,
        test_addon_list_icon_and_minimap_icon_match,
        test_minimap_button_built_only_at_login,
        test_minimap_button_degrades_without_host,
        test_minimap_click_toggles_and_tooltip_is_player_facing,
        test_minimap_tooltip_net_today_yesterday_week,
        test_minimap_drag_writes_angle_and_detaches_onupdate,
        test_ledger_char_entries_all_chars_and_source_filter,
        test_flows_popup_opens_from_command_and_lists_rows,
        test_flows_dropdown_filters,
        test_flows_paging_and_scroll,
        test_flows_summary_on_side_column,
        test_flows_close_and_esc_and_level,
        test_flows_range_text_six_presets,
        test_flows_empty_and_no_clock,
        test_calendar_round4_geometry_and_cell_font,
        test_calendar_round5_tip_frame_uniform_and_opaque,
        test_ledger_round4_char_color_fallback_not_gray,
        test_locale_round4_source_names,
        test_flows_round4_range_dropdown_layout,
        test_flows_round4_summary_columns_aligned,
        test_ledger_round5_vendor_merge_writes_one_entry,
        test_calendar_round5_char_class_sources,
        test_flows_round6_dropdown_value_only_and_clipped,
        test_calendar_round7_summary_strip,
        test_calendar_summary_blocks_open_range_flows,
        test_calendar_gold_snapshots_and_assets,
        test_flows_round7_category_total_and_dividers,
        test_gcl_raise_front_above_gql_peers,
        test_gcl_windows_set_toplevel,
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:      # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    print(f"\n{len(tests) - len(FAILED)}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
