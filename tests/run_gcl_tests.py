# GoldCalendarLedger 离线单测（T0 起跑）：toc 静态守卫 + Core 骨架行为。
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
-- 确定性估宽（ASCII 6px / 其它按 UTF-8 码点 12px）：客户端按字体排版回真像素，离线没有
-- 排版器。这个估宽只用来让"文字盖过列位""提示别宽过窗口"这类用例算得成账，
-- **不代表客户端实测值** —— 真值必须由用户跑 /gcl probe ui 量回来。
local function GCL_EstimateRunes(s)
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
local function GCL_EstimateWidth(s)
    s = s or ""
    local total, pos = 0, 1
    while true do
        local a = s:find("|T", pos, true)
        if not a then return total + GCL_EstimateRunes(s:sub(pos)) end
        total = total + GCL_EstimateRunes(s:sub(pos, a - 1))
        local e = s:find("|t", a + 2, true)
        if not e then return total + GCL_EstimateRunes(s:sub(a)) end
        local w = s:sub(a, e - 1):match(":%d+:(%d+)$")
        if w then
            total = total + tonumber(w)
        else
            total = total + GCL_EstimateRunes(s:sub(a, e + 1))
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
        if GCL_KNOWN_EVENTS[e] ~= true then
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
    f.SetMovable = function(self, v) self.__movable = v end                 -- Matrix.lua:1571
    f.SetClampedToScreen = function(self, v) self.__clamped = v end          -- Options.lua:662
    f.EnableMouse = function(self, v) self.__mouse = v end                   -- Matrix.lua:1572
    f.RegisterForDrag = function(self, b) self.__drag = b end                -- Matrix.lua:1573
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
        -- 建串时传的字体对象名记下来：/gcl probe ui 要报"这条字用的是哪个字体"，
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
    -- 量宽：真客户端按字体排版回像素，离线没有排版器。这里回**确定性**的估宽
    -- （GCL_EstimateWidth，存在性出处 Options.lua:324），只用来让"文字盖过列位"这类
    -- 用例能算账，不代表客户端实测值 —— 真值必须由用户跑 /gcl probe ui 量。
    f.GetStringWidth = function(self) return GCL_EstimateWidth(self.__text) end
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
        error("GCL mock frame: 没有这个方法 " .. tostring(k))
    end })
    if type(parent) == "table" then
        parent.__children = parent.__children or {}
        parent.__children[#parent.__children + 1] = f
    end
    if name then _G[name] = f end
    GCL_FRAMES[#GCL_FRAMES + 1] = f
    return f
end
-- 暴雪的 ESC 关闭名单：里面存的是**全局帧名字符串**，客户端拿它去 _G 找帧
-- （GoldQuestLedger/Config.lua:273-283 注册的就是这个表，按名字插，重复不插）。
UISpecialFrames = {}
-- GameTooltip：界面唯一的悬停出口。只给本仓库生产代码真用过的这几个方法
-- （Matrix.lua:494 SetOwner / 495 ClearLines / 548 AddLine / 668 Show / 2465 Hide），
-- 名单外取用照旧抛错；GetWidth/GetHeight 是 Frame 共有方法（Matrix.lua:1251 同类），
-- probe 报"提示现在多宽"要用。
GCL_TIP_LINES = {}
GameTooltip = { __shown = false, __owner = nil }
function GameTooltip.SetOwner(self, owner, anchor) self.__owner, self.__anchor = owner, anchor end
function GameTooltip.ClearLines(self)
    for i = #GCL_TIP_LINES, 1, -1 do GCL_TIP_LINES[i] = nil end
end
function GameTooltip.AddLine(self, text, r, g, b)
    GCL_TIP_LINES[#GCL_TIP_LINES + 1] = { text = tostring(text), r = r, g = g, b = b }
end
function GameTooltip.Show(self) self.__shown = true end
function GameTooltip.Hide(self) self.__shown = false end
function GameTooltip.GetWidth(self)
    local w = 0
    for i = 1, #GCL_TIP_LINES do
        local line = GCL_EstimateWidth(GCL_TIP_LINES[i].text)
        if line > w then w = line end
    end
    return w
end
function GameTooltip.GetHeight(self) return #GCL_TIP_LINES * 14 end
setmetatable(GameTooltip, { __index = function(_, k)
    error("GCL mock GameTooltip: 没有这个方法 " .. tostring(k))
end })
-- 周 CD 倒计时（计划 §3 的周口径来源）：存在性与返回值形状都指得出真机证据
-- （GoldQuestLedger/Api.lua:1408-1423 用它，12.1.5 真机回传 AVAILABLE -> 511085，docs/log.txt:19）。
-- 默认值先给 nil："接口问不出"才是需要退化的那一档，用例自己注入秒数。
GCL_WEEK_RESET_SECONDS = nil
C_DateAndTime = {
    GetSecondsUntilWeeklyReset = function() return GCL_WEEK_RESET_SECONDS end,
}
-- 用例入口：像客户端那样把帧上挂着的脚本取回来调（脚本没挂就抛错，
-- 防"点了个根本不存在的按钮"这种假绿）。和 GCL_FireEvent 同一类测试侧入口，
-- 不是客户端 API。
function GCL_CallScript(f, name, ...)
    local s = f.__scripts[name]
    if not s then return false end
    s(f, ...)
    return true
end
function GCL_Click(f, ...)
    if not f.__scripts["OnClick"] then
        error("GCL mock: 这帧没挂 OnClick（按钮 " .. tostring(f.__text) .. "）")
    end
    return f.__scripts["OnClick"](f, ...)
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
CAL_ALL_SOURCES = ["vendor", "ah", "mail", "trade", "loot", "quest",
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
    """走 /gcl open（真命令入口），返回窗开没开。"""
    slash(lua, "open")
    return cal_eval(lua, "GCLCalFrame and GCLCalFrame:IsShown() or false")


def cal_click(lua, expr):
    lua.globals()["GCL_Click"](cal_eval(lua, expr))


def cal_script(lua, expr, name):
    lua.globals()["GCL_CallScript"](cal_eval(lua, expr), name)


def cal_geom(lua, key):
    return as_number(cal_eval(lua, "GCL.Calendar.GEOM.%s" % key))


def cal_state(lua):
    def g(path):
        return cal_eval(lua, "GCL.Calendar.GetState().%s" % path)
    return {
        "year": as_number(g("year")), "month": as_number(g("month")),
        "preset": None if g("preset") is None else str(g("preset")),
        "startKey": None if g("startKey") is None else str(g("startKey")),
        "endKey": None if g("endKey") is None else str(g("endKey")),
        "selKey": None if g("selKey") is None else str(g("selKey")),
        "page": str(g("page")),
        "flowChar": None if g("flowChar") is None else str(g("flowChar")),
        "flowPage": as_number(g("flowPage")),
        "weekSource": str(g("weekCD.source")),
        "weekReason": None if g("weekCD.reason") is None else str(g("weekCD.reason")),
        "tipShown": bool(g("tip.shown")),
        "tipError": None if g("tip.error") is None else str(g("tip.error")),
        "errors": as_number(cal_eval(lua, "#GCL.Calendar.GetState().errors")),
    }


def cal_cells(lua):
    """42 格全量快照：键、月内/用到/今天/可见 + 三行字的文本 + 今天那格的边框纹理。"""
    tbl = cal_eval(lua, r"""(function()
        local ui = GCL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for i, c in ipairs(ui.cells) do
            out[#out + 1] = {
                key = tostring(c.dateKey or ""), inM = tostring(c.inMonth and 1 or 0),
                used = tostring(c.used and 1 or 0), today = tostring(c.today and 1 or 0),
                shown = tostring(c.host:IsShown() and 1 or 0),
                num = tostring(c.num:GetText() or ""), net = tostring(c.net:GetText() or ""),
                sub = tostring(c.sub:GetText() or ""),
                edge = tostring((c.host.__backdrop and c.host.__backdrop.edgeFile) or ""),
                netR = (c.net.__color and ("%.4f"):format(c.net.__color[1])) or "",
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("key", "inM", "used", "today", "shown",
                          "num", "net", "sub", "edge", "netR"))


def cal_char_rows(lua):
    tbl = cal_eval(lua, r"""(function()
        local ui = GCL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for i, r in ipairs(ui.charRows) do
            local btns = 0
            for k = 1, #(r.host.__children or {}) do
                if r.host.__children[k].__template == "UIPanelButtonTemplate" then
                    btns = btns + 1
                end
            end
            out[#out + 1] = {
                name = tostring(r.name:GetText() or ""), inc = tostring(r.inc:GetText() or ""),
                exp = tostring(r.exp:GetText() or ""), net = tostring(r.net:GetText() or ""),
                shown = tostring(r.host:IsShown() and 1 or 0), key = tostring(r.charKey or ""),
                btnText = tostring(r.btn:GetText() or ""), btns = tostring(btns),
                nameR = (r.name.__color and ("%.4f"):format(r.name.__color[1])) or "",
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("name", "inc", "exp", "net", "shown", "key",
                          "btnText", "btns", "nameR"))


def cal_flow_rows(lua):
    tbl = cal_eval(lua, r"""(function()
        local ui = GCL.Calendar.GetUI()
        if not ui then return nil end
        local out = {}
        for i, r in ipairs(ui.flowRows) do
            out[#out + 1] = {
                time = tostring(r.time:GetText() or ""), src = tostring(r.src:GetText() or ""),
                amt = tostring(r.amt:GetText() or ""), note = tostring(r.note:GetText() or ""),
                shown = tostring(r.host:IsShown() and 1 or 0),
                noteShown = tostring(r.note:IsShown() and 1 or 0),
            }
        end
        return out
    end)()""")
    return lua_rows(tbl, ("time", "src", "amt", "note", "shown", "noteShown"))


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


def cal_month_shape(y, mo):
    """（首日前的空格数, 月长, 需要的行数）—— 用 Python 的日历独立算一遍，
    专门用来核对 Ledger 那套 Sakamoto/闰年数学。"""
    offset = datetime(y, mo, 1, tzinfo=timezone.utc).isoweekday() % 7   # 周日 = 0
    dim = monthrange(y, mo)[1]
    return offset, dim, (offset + dim + 6) // 7


def cal_shift_month(lua, delta):
    expr = "GCL.Calendar.GetUI().btnNextMo" if delta > 0 else "GCL.Calendar.GetUI().btnPrevMo"
    for _ in range(abs(delta)):
        cal_click(lua, expr)


def cal_money_icon(copper):
    """手抄 Calendar.FormatMoneyIcon 的契约（金>0 只给金银、<1 金给银铜、否则只铜）。"""
    if not isinstance(copper, (int, float)):
        return "?"
    sign = "-" if copper < 0 else ""
    v = int(abs(copper))
    g, s, c = v // 10000, (v % 10000) // 100, v % 100
    if g > 0:
        return "%s%d%s%d%s" % (sign, g, ICON_GOLD, s, ICON_SILVER)
    if s > 0:
        return "%s%d%s%d%s" % (sign, s, ICON_SILVER, c, ICON_COPPER)
    return "%s%d%s" % (sign, c, ICON_COPPER)


def test_calendar_builds_nothing_until_open():
    """计划 §3「只在开窗和点击时算」：加载 + boot 完一个界面帧都不许造；
    /gcl open 才建，第二次 open 只关窗、绝不重建控件（重建=帧越开越多）。"""
    name = "calendar_builds_nothing_until_open"
    lua = calendar_env()
    problems = []
    if cal_eval(lua, "GCL.Calendar.GetUI()") is not None:
        problems.append("还没开窗就建了界面")
    if cal_eval(lua, "GCLCalFrame") is not None:
        problems.append("还没开窗就建了主帧")
    n0 = as_number(cal_eval(lua, "#GCL_FRAMES"))
    if cal_open(lua) is not True and str(cal_open(lua)) != "True":
        problems.append("/gcl open 没把窗开起来")
    n1 = as_number(cal_eval(lua, "#GCL_FRAMES"))
    if n1 <= n0:
        problems.append("开窗后一个控件都没建（帧数仍为 %s）" % n1)
    if cal_eval(lua, "GCLCalFrame:IsShown()") is not True:
        problems.append("开窗后主帧没显示")
    slash(lua, "open")                       # 再点一次 = 关
    if cal_eval(lua, "GCLCalFrame:IsShown()") is not False:
        problems.append("第二次 /gcl open 应把窗关掉")
    if as_number(cal_eval(lua, "#GCL_FRAMES")) != n1:
        problems.append("重复开窗重建了控件：帧数 %s → %s" % (n1, cal_eval(lua, "#GCL_FRAMES")))
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
    allowed = {"BackdropTemplate", "UIPanelButtonTemplate"}
    if not templates <= allowed:
        problems.append("用了本仓库没证过的模板：%s（只许 %s）"
                        % (sorted(templates - allowed), sorted(allowed)))
    closes = re.findall(r'CreateFrame\("Button", nil, [A-Za-z_.]+, "(UIPanelCloseButton)"\)', code)
    if len(closes) != 1:
        problems.append("关闭按钮模板的用量应为 1 处，实际 %d 处" % len(closes))
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


def test_calendar_geometry_budget():
    """计划 §3 + Matrix.lua:1535-1537 那条真机要求：窗不许高过屏幕 85%（720 档 ⇒ 612）。
    动任何一个版式常量都会被这条顶出来，那时要么减可见行、要么进滚动区。"""
    name = "calendar_geometry_budget"
    lua = calendar_env()
    cal_open(lua)
    problems = []
    win_h, max_h = cal_geom(lua, "WIN_H"), cal_geom(lua, "MAX_WIN_H")
    if max_h != int(720 * 0.85):
        problems.append("高度上限应是 720 的 85%%=%d，实际 %s" % (int(720 * 0.85), max_h))
    if win_h > max_h:
        problems.append("窗高 %d 超了预算 %d" % (win_h, max_h))
    if cal_geom(lua, "WIN_W") != 2 * cal_geom(lua, "PAD") + 7 * cal_geom(lua, "CELL_W"):
        problems.append("窗宽不等于两侧留白 + 7 列格宽")
    if cal_geom(lua, "GRID_ROWS") * 7 != len(cal_cells(lua)):
        problems.append("格子数 %d 与 GRID_ROWS×7 不符" % len(cal_cells(lua)))
    if cal_geom(lua, "FLOW_ROWS") != as_number(cal_eval(lua, "GCL.Calendar.PER_PAGE")):
        problems.append("一页条数与 FLOW_ROWS 不一致（翻页按钮会对不上屏）")
    if cal_geom(lua, "CELL_H") < 3 * 12:
        problems.append("格高 %s 放不下三行 12 号字" % cal_geom(lua, "CELL_H"))
    # 顶栏三个读数不许越出窗宽
    left = cal_geom(lua, "TOTALS_LEFT")
    right = left + 3 * cal_geom(lua, "BAND_W") + 2 * cal_geom(lua, "BAND_GAP")
    if right > cal_geom(lua, "WIN_W") - cal_geom(lua, "PAD"):
        problems.append("顶栏读数右边界 %d 越出窗内 %d" % (right, cal_geom(lua, "WIN_W") - cal_geom(lua, "PAD")))
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
        info_rows = as_number(cal_eval(lua, "GCL.Calendar.GetUI().gridInfo.rowsNeeded"))
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
            want_key = cal_eval(lua, "GCL.Calendar.KeyOf(%d, %d, %d)" % (ty, tm, day))
            if c["key"] != str(want_key):
                problems.append("%d-%02d 第 %d 格键 %s 与 KeyOf(%d,%d,%d)=%s 不符"
                                % (ty, tm, i + 1, c["key"], ty, tm, day, want_key))
            # 列位 = 星期：Python 独立算一遍，专门核 Ledger 的 Sakamoto 数学
            y_, m_, d_ = int(str(want_key)[:4]), int(str(want_key)[5:7]), int(str(want_key)[8:10])
            py_wday = datetime(y_, m_, d_, tzinfo=timezone.utc).isoweekday() % 7
            if py_wday != i % 7:
                problems.append("%s 落在第 %d 列，Python 日历判它应是第 %d 列"
                                % (want_key, i % 7 + 1, py_wday + 1))
            wd = as_number(cal_eval(lua, 'GCL.Ledger.WeekdayOfDateKey(%r)' % str(want_key)))
            if wd - 1 != i % 7:
                problems.append("Ledger 判 %s 的星期为 %s，与列位 %s 不符" % (want_key, wd, i % 7 + 1))
            if c["inM"] == "1":
                if c["num"] != str(d_):
                    problems.append("%s 的日号文本 %r 应为 %d" % (want_key, c["num"], d_))
            else:
                for k in ("num", "net", "sub"):
                    if cells[i][k] != "":
                        problems.append("月外格 %s 还摆了 %s=%r（草图要求只画底）" % (want_key, k, cells[i][k]))
        if problems and len(problems) > 12:
            break
    return expect(name, problems)


def test_calendar_cell_content_today_and_colors():
    """格子里的三行字：净 = 图标金额（正绿负红），小字 = 收·支缩写；
    零变动的日子两行都空着；只有今天那一格带边框纹理（描边）。"""
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
    want_sub = "%s·%s" % ("123.5金", "50.0金")   # 1234567/10000、500000/10000 各取一位小数
    if today["sub"] != want_sub:
        problems.append("小字收·支 %r 应为 %r" % (today["sub"], want_sub))
    if neg["net"] != cal_money_icon(-250000):
        problems.append("负净文本 %r 应为 %r" % (neg["net"], cal_money_icon(-250000)))
    if neg["netR"] != "0.8600":
        problems.append("负净应红（首分量 0.86），实际 %s" % neg["netR"])
    if zero["net"] != "" or zero["sub"] != "":
        problems.append("零变动的日子不该摆字：net=%r sub=%r" % (zero["net"], zero["sub"]))
    if zero["netR"] != "0.6200":
        problems.append("零变动应灰（首分量 0.62），实际 %s" % zero["netR"])
    outlined = [i for i, c in enumerate(cells) if c["edge"] != ""]
    if outlined != [9]:
        problems.append("带边框纹理的格子应是今天那一格（下标 9），实际 %s" % outlined)
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_text_clipping_is_utf8_safe():
    """截断只按**字节预算**做，且绝不劈开一个 UTF-8 字符（客户端会把半个字符画成方块）。
    像素级"文字有没有盖过列位"不在这里断言：离线没有排版器，任何估宽都是我编的标准，
    真值由用户在真机跑 /gcl probe ui 量回来（计划 §3/T5）。"""
    name = "calendar_text_clipping_is_utf8_safe"
    long_name = "一个非常非常长的号名用来考验截断是否劈字符"
    lua = calendar_env(char=long_name)
    add_entry(lua, 123456789, "quest")
    cal_open(lua)
    cal_click(lua, "GCL.Calendar.GetUI().cells[10].host")
    problems = []
    txt = cal_text(lua, "GCL.Calendar.GetUI().charRows[1].name")
    cur_key = "%s-%s" % (long_name, REALM)
    if len(cur_key.encode("utf-8")) <= 34:
        problems.append("夹具的号名太短，没走到截断这条路")
    if not txt.endswith("…"):
        problems.append("超长号名没截断：%r" % txt)
    try:
        txt.encode("utf-8").decode("utf-8")
    except UnicodeDecodeError:
        problems.append("截断劈开了 UTF-8 字符：%r" % txt)
    if txt[:-1] not in cur_key:
        problems.append("截出来的前缀不是号名的开头：%r vs %r" % (txt, cur_key))
    # 格子里的收·支小字用缩写形式（图标形式并排两个一定超格）
    sub = cal_text(lua, "GCL.Calendar.GetUI().cells[10].sub")
    if "|T" in sub:
        problems.append("格子小字用了图标形式（应走缩写）：%r" % sub)
    if sub != "12345.7金·0铜":       # 123456789 铜 = 12345.6789 金 → 一位小数；支出 0
        problems.append("缩写金额 %r 应为 '12345.7金·0铜'" % sub)
    # 流水备注：条目真带了邮件标题才显示，且同样不劈字符
    lua2 = calendar_env()
    add_entry(lua2, -5000, "mail",
              "{ sender = '甲', subject = %r }" % ("这封邮件的标题非常长"*4))
    cal_open(lua2)
    cal_click(lua2, "GCL.Calendar.GetUI().cells[10].host")
    cal_click(lua2, "GCL.Calendar.GetUI().charRows[1].btn")
    row = cal_flow_rows(lua2)[0]
    if not row["note"]:
        problems.append("条目带了邮件标题却没显示备注列")
    try:
        row["note"].encode("utf-8").decode("utf-8")
    except UnicodeDecodeError:
        problems.append("备注截断劈开了 UTF-8 字符：%r" % row["note"])
    if not row["note"].startswith("这封邮件"):
        problems.append("备注文本 %r 不是标题开头" % row["note"])
    for k in ("errors",):
        if cal_state(lua2)[k] != 0:
            problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_char_drilldown_and_flow_button():
    """点日号：下半区换成**这一天**有变动的号，当前号置顶并标强调色，
    每行恰好一颗「流水」按钮（用户定的硬规矩），合计行是全量而不是可见行。"""
    name = "calendar_char_drilldown_and_flow_button"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")                        # 当前号 净 100 金
    set_char(lua, "甲")
    add_entry(lua, 300000, "vendor")
    add_entry(lua, -50000, "repair")                        # 甲 净 25 金
    set_char(lua, "乙")
    add_entry(lua, 2000000, "ah")                           # 乙 净 200 金（最大）
    set_char(lua, "当前号")
    cal_open(lua)
    cal_click(lua, "GCL.Calendar.GetUI().cells[10].host")
    st = cal_state(lua)
    problems = []
    if (st["startKey"], st["endKey"]) != ("2026-11-10", "2026-11-10"):
        problems.append("点日后范围没收成单日：%s..%s" % (st["startKey"], st["endKey"]))
    # 点的正是开窗已经选中的今天 ⇒ 范围本就单日、档位维持"本日"不用改（改了也只是换个高亮）
    if st["preset"] not in ("day", "sel"):
        problems.append("点今天后档位成了 %s" % st["preset"])
    if st["page"] != "chars":
        problems.append("点日后应停在号列表页，实际 %s" % st["page"])
    # 换一个日子（这天没数据）⇒ 档位落 sel、范围跟着收成那一天，再点回来仍然只问这一天
    cal_click(lua, "GCL.Calendar.GetUI().cells[11].host")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"]) != ("sel", "2026-11-11", "2026-11-11"):
        problems.append("点别的日子没收成 sel 单日：%s %s..%s" % (st["preset"], st["startKey"], st["endKey"]))
    cal_click(lua, "GCL.Calendar.GetUI().cells[10].host")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"]) != ("sel", "2026-11-10", "2026-11-10"):
        problems.append("点回这天没收成 sel 单日：%s %s..%s" % (st["preset"], st["startKey"], st["endKey"]))
    rows = cal_char_rows(lua)
    cur = "当前号-%s" % REALM
    if rows[0]["key"] != cur:
        problems.append("第一行应是当前号（哪怕它不是最富的），实际 %s" % rows[0]["key"])
    if rows[0]["nameR"] != "1.0000":
        problems.append("当前号那行的号名该用强调色（首分量 1.0），实际 %s" % rows[0]["nameR"])
    if rows[1]["key"] != "乙-%s" % REALM or rows[2]["key"] != "甲-%s" % REALM:
        problems.append("其余号没按净降序：%s" % [r["key"] for r in rows])
    if rows[3]["shown"] != "0":
        problems.append("只有 3 个号有变动，第 4 行该 Hide")
    for i, r in enumerate(rows[:3]):
        if r["btns"] != "1":
            problems.append("第 %d 行有 %s 颗按钮（每行只许一颗「流水」）" % (i + 1, r["btns"]))
        if r["btnText"] != "流水":
            problems.append("第 %d 行按钮文本 %r 应为「流水」" % (i + 1, r["btnText"]))
    if rows[0]["inc"] != cal_money_icon(1000000) or rows[0]["net"] != cal_money_icon(1000000):
        problems.append("当前号行金额 %r/%r 应为 %r" % (rows[0]["inc"], rows[0]["net"], cal_money_icon(1000000)))
    if cal_text(lua, "GCL.Calendar.GetUI().totalRow.inc") != cal_money_icon(3300000):
        problems.append("合计收入 %r 应为 %r"
                        % (cal_text(lua, "GCL.Calendar.GetUI().totalRow.inc"), cal_money_icon(3300000)))
    if cal_text(lua, "GCL.Calendar.GetUI().totalRow.exp") != cal_money_icon(50000):
        problems.append("合计支出对不上")
    if cal_eval(lua, "GCL.Calendar.GetUI().moreChars:IsShown()"):
        problems.append("3 个号没有溢出，还显示「另有 N 个号未列出」")
    if cal_eval(lua, "GCL.Calendar.GetUI().empty:IsShown()"):
        problems.append("有数据还显示空态")
    # 溢出提示：8 个号 ⇒ 可见 6 行 + "另有 2 个号未列出"
    for i in range(5):
        set_char(lua, "丙%d" % i)
        add_entry(lua, 1000 + i, "loot")
    set_char(lua, "当前号")
    cal_click(lua, "GCL.Calendar.GetUI().presets.day")      # 回本日再进同一天，重画
    cal_click(lua, "GCL.Calendar.GetUI().cells[10].host")
    if cal_text(lua, "GCL.Calendar.GetUI().moreChars") != "另有 2 个号未列出":
        problems.append("溢出提示 %r 应为「另有 2 个号未列出」"
                        % cal_text(lua, "GCL.Calendar.GetUI().moreChars"))
    if not cal_eval(lua, "GCL.Calendar.GetUI().moreChars:IsShown()"):
        problems.append("溢出号数超了还没显示提示")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_flow_page_paging_and_back():
    """「流水」分页：新在前、一页=一屏、页码到边夹住、空流水不打转，「返回」回号列表。"""
    name = "calendar_flow_page_paging_and_back"
    lua = calendar_env()
    for k in range(12):                                    # 同一号同一天 12 笔，分钟递增
        set_epoch(lua, ep(2026, 11, 10, 9, k))
        add_entry(lua, -(1000 * (k + 1)), "vendor")
    set_epoch(lua, ep(2026, 11, 10))
    cal_open(lua)
    cal_click(lua, "GCL.Calendar.GetUI().cells[10].host")
    cal_click(lua, "GCL.Calendar.GetUI().charRows[1].btn")
    st = cal_state(lua)
    problems = []
    if st["page"] != "flow" or st["flowChar"] != "当前号-%s" % REALM:
        problems.append("点「流水」没进流水页：%s %s" % (st["page"], st["flowChar"]))
    if as_number(cal_eval(lua, "GCL.Calendar.GetState().flowInfo.totalPages")) != 3:
        problems.append("12 笔 / 每页 %s 应 3 页" % cal_geom(lua, "FLOW_ROWS"))
    if cal_text(lua, "GCL.Calendar.GetUI().pageInfo") != "第 1 / 3 页":
        problems.append("页码文本 %r" % cal_text(lua, "GCL.Calendar.GetUI().pageInfo"))
    rows = cal_flow_rows(lua)
    if len(rows) != 5:
        problems.append("可见流水行不是 5 行")
    if rows[0]["time"] != "2026-11-10 09:11":
        problems.append("第一行应是最新那笔（09:11），实际 %r" % rows[0]["time"])
    if rows[0]["amt"] != cal_money_icon(-12000):
        problems.append("金额 %r 应为 %r" % (rows[0]["amt"], cal_money_icon(-12000)))
    if rows[0]["src"] != "卖店":
        problems.append("来源名 %r 应为「卖店」" % rows[0]["src"])
    if rows[0]["noteShown"] != "0":
        problems.append("条目没带邮件标题还显示备注列（编不出来的东西不许显示）")
    cal_click(lua, "GCL.Calendar.GetUI().nav.next")
    if cal_flow_rows(lua)[0]["time"] != "2026-11-10 09:06":
        problems.append("第二页首行应 09:06，实际 %r" % cal_flow_rows(lua)[0]["time"])
    cal_click(lua, "GCL.Calendar.GetUI().nav.next")
    cal_click(lua, "GCL.Calendar.GetUI().nav.next")          # 已在末页，再点夹住
    if cal_state(lua)["flowPage"] != 3:
        problems.append("末页再翻页页码被推出去了：%s" % cal_state(lua)["flowPage"])
    if len([r for r in cal_flow_rows(lua) if r["shown"] == "1"]) != 2:
        problems.append("第三页只剩 2 笔，其余行该 Hide")
    cal_click(lua, "GCL.Calendar.GetUI().nav.back")
    st = cal_state(lua)
    if st["page"] != "chars" or st["flowChar"] is not None:
        problems.append("「返回」没回到号列表：%s %s" % (st["page"], st["flowChar"]))
    # 返回只有一处控件（挂 ui.nav.back，别处不该再有第二颗），且在号列表页再点不改状态
    if cal_eval(lua, "GCL.Calendar.GetUI().back") is not None:
        problems.append("界面里有第二颗返回按钮")
    cal_click(lua, "GCL.Calendar.GetUI().nav.back")
    if cal_state(lua)["page"] != "chars":
        problems.append("号列表页再点返回把状态改坏了：%s" % cal_state(lua)["page"])
    # 分类小计：12 笔全是 vendor ⇒ 只有一行，提示不显示
    cal_click(lua, "GCL.Calendar.GetUI().charRows[1].btn")
    if cal_text(lua, "GCL.Calendar.GetUI().subRows[1].label") != "卖店":
        problems.append("分类小计第一行 %r 应为「卖店」" % cal_text(lua, "GCL.Calendar.GetUI().subRows[1].label"))
    if cal_eval(lua, "GCL.Calendar.GetUI().subRows[2].host:IsShown()"):
        problems.append("只有一类还画了第二行")
    if cal_eval(lua, "GCL.Calendar.GetUI().moreSources:IsShown()"):
        problems.append("没溢出还显示「另有 N 类未列出」")
    cal_click(lua, "GCL.Calendar.GetUI().nav.back")
    # 空流水：选一个完全没数据的日子
    cal_click(lua, "GCL.Calendar.GetUI().cells[15].host")
    if cal_text(lua, "GCL.Calendar.GetUI().empty") != "这个范围没有变动":
        problems.append("空范围文案 %r" % cal_text(lua, "GCL.Calendar.GetUI().empty"))
    if not cal_eval(lua, "GCL.Calendar.GetUI().empty:IsShown()"):
        problems.append("空范围没显示空态")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_range_presets():
    """四档范围（本日 / 本周CD / 本月 / 全部）都给出**含端点**的 ISO 键：
    周 CD 用真接口的倒计时推锚点（含跨月的周）、问不出就退日历周周一并只把原因留在 probe。"""
    name = "calendar_range_presets"
    problems = []
    lua = calendar_env(day=(2026, 11, 10))
    cal_open(lua)
    cal_click(lua, "GCL.Calendar.GetUI().presets.day")
    st = cal_state(lua)
    if (st["preset"], st["startKey"], st["endKey"]) != ("day", "2026-11-10", "2026-11-10"):
        problems.append("本日 %s %s..%s" % (st["preset"], st["startKey"], st["endKey"]))
    cal_click(lua, "GCL.Calendar.GetUI().presets.month")
    st = cal_state(lua)
    if (st["startKey"], st["endKey"]) != ("2026-11-01", "2026-11-30"):
        problems.append("本月 %s..%s" % (st["startKey"], st["endKey"]))
    cal_click(lua, "GCL.Calendar.GetUI().presets.all")
    st = cal_state(lua)
    if (st["startKey"], st["endKey"]) != ("0000-01-01", "9999-12-31"):
        problems.append("全部 %s..%s" % (st["startKey"], st["endKey"]))
    # 本周CD：倒计时 2 天 ⇒ 锚点 = 今天 + 2 天 - 7 天 = 2026-10-28（跨月）
    lua.execute("GCL_WEEK_RESET_SECONDS = 172800")
    cal_click(lua, "GCL.Calendar.GetUI().presets.week")
    st = cal_state(lua)
    want_start = datetime.fromtimestamp(ep(2026, 11, 10) + 172800 - 604800,
                                        timezone.utc).strftime("%Y-%m-%d")
    if (st["startKey"], st["endKey"]) != (want_start, "2026-11-10"):
        problems.append("本周CD %s..%s 应为 %s..2026-11-10" % (st["startKey"], st["endKey"], want_start))
    if st["weekSource"] != "C_DateAndTime.GetSecondsUntilWeeklyReset":
        problems.append("接口答得出还走了别的来源：%s" % st["weekSource"])
    if st["weekReason"] is not None:
        problems.append("没退化却记了退化原因：%s" % st["weekReason"])
    # 接口答非周口径（> 一周）⇒ 退化日历周周一，原因只进 probe
    lua.execute("GCL_WEEK_RESET_SECONDS = 700000")
    cal_click(lua, "GCL.Calendar.GetUI().presets.week")
    st = cal_state(lua)
    if st["weekSource"] != "monday-fallback":
        problems.append("接口答的不是周口径时应退化成日历周，实际 %s" % st["weekSource"])
    if st["startKey"] != "2026-11-09" or st["endKey"] != "2026-11-10":
        problems.append("2026-11-10（周二）所在日历周起点应是周一 2026-11-09，实际 %s..%s"
                        % (st["startKey"], st["endKey"]))
    if "周口径" not in (st["weekReason"] or ""):
        problems.append("退化原因没记进 probe：%s" % st["weekReason"])
    # 接口整个不存在 ⇒ 同样退化，且不许报错
    lua.execute("C_DateAndTime = nil")
    cal_click(lua, "GCL.Calendar.GetUI().presets.week")
    if cal_state(lua)["weekSource"] != "monday-fallback" or cal_state(lua)["errors"] != 0:
        problems.append("周接口缺席时没退化或报错了")
    # 「本月」预设跟着显示月走
    lua.execute("C_DateAndTime = { GetSecondsUntilWeeklyReset = function() return 172800 end }")
    cal_click(lua, "GCL.Calendar.GetUI().presets.month")
    cal_shift_month(lua, 1)
    st = cal_state(lua)
    if (st["startKey"], st["endKey"]) != ("2026-12-01", "2026-12-31"):
        problems.append("‹ › 换月后本月范围没跟上：%s..%s" % (st["startKey"], st["endKey"]))
    if cal_eval(lua, "GCL.Calendar.GetUI().btnNextMo:GetText()") != "›":
        problems.append("换月按钮文本丢了")
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
    got = {p: cal_text(lua, "GCL.Calendar.GetUI().totals.%s.value" % p)
           for p in ("week", "month", "all")}
    if got["week"] != cal_money_icon(200000):
        problems.append("本周CD 读数 %r 应为 %r" % (got["week"], cal_money_icon(200000)))
    if got["month"] != cal_money_icon(200000):
        problems.append("本月读数 %r 只该含 11 月那笔" % got["month"])
    if got["all"] != cal_money_icon(500000):
        problems.append("全部读数 %r 应为 %r" % (got["all"], cal_money_icon(500000)))
    # 搬钱不进任何总量
    add_entry(lua, -150000, "warband")
    if cal_text(lua, "GCL.Calendar.GetUI().totals.all.value") != cal_money_icon(500000):
        problems.append("号间搬钱被算进了总读数")
    if cal_text(lua, "GCL.Calendar.GetUI().totals.week.label") != "本周CD":
        problems.append("读数标签丢了：%r" % cal_text(lua, "GCL.Calendar.GetUI().totals.week.label"))
    # 没墙上钟：全新环境、开窗走的就是"TodayKey 全程读不出"那一路
    lua3 = calendar_env()
    set_clock_none(lua3)
    cal_open(lua3)
    st = cal_state(lua3)
    if st["errors"] != 0:
        problems.append("没墙上钟时开窗报错：%d 次" % st["errors"])
    if cal_text(lua3, "GCL.Calendar.GetUI().totals.week.value") != "?":
        problems.append("没墙上钟时周读数应降级成 ?，实际 %r"
                        % cal_text(lua3, "GCL.Calendar.GetUI().totals.week.value"))
    if st["startKey"] is not None:
        problems.append("没墙上钟却编出了范围键：%s" % st["startKey"])
    if not cal_eval(lua3, "GCLCalFrame:IsShown()"):
        problems.append("没墙上钟时窗开不起来")
    if not cal_eval(lua3, "GCL.Calendar.GetUI().empty:IsShown()"):
        problems.append("没墙上钟时下半区没落到空态")
    return expect(name, problems)


def test_calendar_tooltip_only_in_month_cells():
    """悬停 = 日期 + 收 / 支 / 净 + 几个号有变动，五行整整齐齐，不给来源明细；
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
    tips = lua.globals()["GCL_TIP_LINES"]

    def tip_texts():
        out, i = [], 1
        while True:
            row = tips[i]
            if row is None:
                break
            out.append(str(row.text))
            i += 1
        return out

    cal_script(lua, "GCL.Calendar.GetUI().cells[10].host", "OnEnter")
    st = cal_state(lua)
    problems = []
    lines = tip_texts()
    if not st["tipShown"]:
        problems.append("月内格悬停没把提示标成已弹")
    if len(lines) != 5:
        problems.append("提示应是 5 行（日期/收/支/净/N 个号），实际 %d 行：%s" % (len(lines), lines))
    else:
        if lines[0] != "2026-11-10":
            problems.append("第一行应是日期键，实际 %r" % lines[0])
        if lines[1] != "收入 %s" % cal_money_icon(1250000):
            problems.append("收入行 %r" % lines[1])
        if lines[2] != "支出 %s" % cal_money_icon(50000):
            problems.append("支出行 %r" % lines[2])
        if lines[3] != "净 %s" % cal_money_icon(1200000):
            problems.append("净行 %r" % lines[3])
        if lines[4] != "3 个号有变动":
            problems.append("号数行 %r 应为「3 个号有变动」" % lines[4])
        for ln in lines:
            if any(word in ln for word in ("卖店", "修理", "拍卖行", "任务", "拾取", "交易")):
                problems.append("提示里出现了来源明细（那属于点进去那页）：%r" % ln)
    cal_script(lua, "GCL.Calendar.GetUI().cells[30].host", "OnLeave")
    if cal_state(lua)["tipShown"]:
        problems.append("OnLeave 没收提示")
    # 月外格：先弹月内格，再直接悬到月外格（客户端不保证先发 OnLeave），提示必须被收掉
    cal_script(lua, "GCL.Calendar.GetUI().cells[10].host", "OnEnter")
    cal_script(lua, "GCL.Calendar.GetUI().cells[31].host", "OnEnter")
    st = cal_state(lua)
    if st["tipShown"]:
        problems.append("月外格把提示标成了已弹")
    if cal_eval(lua, "GameTooltip.__shown") is True:
        problems.append("从月内格直接移到月外格，上一格的提示还挂在脸上")
    # 提示 Hide 后行还留在控件里是客户端的正常行为（不 Show 就看不见）：
    # 这里要钉的是月外格**一行都不新画**（行数还是上一格那 5 行），而不是它把行清没。
    if len(tip_texts()) != 5:
        problems.append("月外格改写了提示行（该只收不画）：%s" % tip_texts())
    # GameTooltip 缺席：不许报错，只把证据留在 probe
    lua.execute("GameTooltip = nil")
    cal_script(lua, "GCL.Calendar.GetUI().cells[10].host", "OnEnter")
    st = cal_state(lua)
    if "GameTooltip" not in (st["tipError"] or ""):
        problems.append("GameTooltip 缺席时没记证据：%s" % st["tipError"])
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
                          "if UISpecialFrames[i] == 'GCLCalFrame' then n = n + 1 end end return n end)()")
    if as_number(names) != 1:
        problems.append("UISpecialFrames 里 GCLCalFrame 出现 %s 次（应为 1）" % names)
    if cal_eval(lua, "GCLCalFrame == nil") is not False:
        problems.append("全局名 GCLCalFrame 没解析到帧（ESC 名单靠它找窗）")
    lvl = as_number(cal_eval(lua, "GCL.Calendar.GetUI().close:GetFrameLevel()"))
    main = as_number(cal_eval(lua, "GCLCalFrame:GetFrameLevel()"))
    if lvl != main + 1:
        problems.append("× 的层号 %s 应 = 主帧 %s + 1（模板自带 510 会浮到别人窗子上面）" % (lvl, main))
    if main == 510:
        problems.append("主帧层号被判成模板的 510")
    cal_click(lua, "GCL.Calendar.GetUI().close")
    if cal_eval(lua, "GCLCalFrame:IsShown()"):
        problems.append("点 × 没关窗")
    if cal_state(lua)["errors"] != 0:
        problems.append("绘制报错")
    return expect(name, problems)


def test_calendar_probe_ui_is_read_only():
    """`/gcl probe ui` 只读取证：常量、周 CD 来源与退化原因、帧几何、每格矩形、
    字体量宽、提示行宽、× 层号、绘制报错计数都要报，且一条都不改界面状态；
    `/gcl probe`（记账层取证）不许被这条链挡掉。"""
    name = "calendar_probe_ui_is_read_only"
    lua = calendar_env()
    add_entry(lua, 500000, "quest")
    cal_open(lua)
    cal_script(lua, "GCL.Calendar.GetUI().cells[10].host", "OnEnter")
    before = cal_state(lua)
    lines = slash(lua, "probe ui")
    joined = "\n".join(lines)
    after = cal_state(lua)
    problems = []
    for want in ("界面几何取证", "常量 WIN_W=", "周CD接口", "帧 GCLCalFrame",
                 "格10 ", "字体 角色=", "提示行1", "绘制没报错"):
        if want not in joined:
            problems.append("probe ui 少了 %r：%s" % (want, joined[:400]))
    if "3 个号有变动" not in joined and "提示行5" not in joined:
        problems.append("probe ui 没报提示行的宽度证据")
    for k in ("preset", "startKey", "endKey", "page", "flowChar", "tipShown"):
        if before[k] != after[k]:
            problems.append("probe ui 改了状态 %s：%s → %s" % (k, before[k], after[k]))
    money_lines = slash(lua, "probe")
    if not any("记账层依赖的接口取证" in ln for ln in money_lines):
        problems.append("/gcl probe 被界面链吃掉了我：%s" % money_lines)
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
    lua.execute('GCL.modules["Ledger"] = nil GCL.Ledger = nil')
    set_epoch(lua, ep(2026, 11, 10))
    fire(lua, "ADDON_LOADED", ADDON_NAME)
    fire(lua, "PLAYER_LOGIN")
    cal_open(lua)
    problems = []
    if cal_state(lua)["errors"] != 0:
        problems.append("Ledger 缺席时绘制报错 %d 次" % cal_state(lua)["errors"])
    if cal_text(lua, "GCL.Calendar.GetUI().totals.all.value") != "?":
        problems.append("Ledger 缺席时读数应是 ?，实际 %r"
                        % cal_text(lua, "GCL.Calendar.GetUI().totals.all.value"))
    if not cal_eval(lua, "GCLCalFrame:IsShown()"):
        problems.append("Ledger 缺席时窗开不起来")
    return expect(name, problems)


def test_locale_tables_are_two_language_complete():
    """键的纪律：zhCN 与 enUS 两张表键集相等，且每条串 %d/%s 占位符**序列**一致；
    界面（Calendar.lua）里每一个 T("…")/Fmt("…") 的字面键、以及拼出来的动态家族
    （WD_1..7、RANGE_*、SRC_*）都必须在两张表里都点得出货。"""
    name = "locale_tables_are_two_language_complete"
    lua, _ = load_lua("Core", "Money", "Ledger", "Locale", "Calendar")
    problems = []

    def keys(code):
        v = cal_eval(lua, "(function() local n = {} for k in pairs(GCL.Locale.L['%s']) do "
                          "n[#n + 1] = k end table.sort(n) return table.concat(n, ',') end)()" % code)
        return set(str(v).split(","))

    zh, en = keys("zhCN"), keys("enUS")
    if zh != en:
        problems.append("两语言键集不等：只中文有 %s；只英文有 %s" % (sorted(zh - en), sorted(en - zh)))
    with open(os.path.join(PLUGIN_DIR, "Calendar.lua"), encoding="utf-8") as fh:
        src = fh.read()
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
        v = cal_eval(lua, "GCL.Locale.L['%s'][%r]" % (code, key))
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
            lua.globals()["GCL"].Locale.Set(code)
            got = str(lua.globals()["GCL"].Locale.SrcName(src_name))
            if got == src_name:
                problems.append("%s 语言下来源 %s 没译名（原样回落）" % (code, src_name))
    lua.globals()["GCL"].Locale.Set("zhCN")
    return expect(name, problems)


def test_locale_selection_and_fallbacks():
    """只留中英两档：认得的代码切过去，其它（含 nil / "auto" / 拼错）回落默认档不报错；
    切语言后界面词跟着变，取证措辞不受影响。"""
    name = "locale_selection_and_fallbacks"
    lua = calendar_env()
    add_entry(lua, 1000000, "quest")                         # 今天这格要有收也要有支，格子小字才画得出来
    add_entry(lua, -50000, "vendor")
    cal_open(lua)
    problems = []
    loc = lua.globals()["GCL"].Locale
    if str(loc.GetLocale()) != "zhCN":
        problems.append("默认档应为 zhCN，实际 %s" % loc.GetLocale())
    if str(loc.Set("deDE")) != "zhCN":
        problems.append("没证的语种代码应回落默认档，实际 %s" % loc.Set("deDE"))
    if str(loc.ApplySetting("auto")) != "zhCN":
        problems.append('"auto" 应落默认档（T4 才接自动判定）')
    if str(loc.ApplySetting("enUS")) != "enUS":
        problems.append("ApplySetting(enUS) 没切过去")
    cal_script(lua, "GCL.Calendar.GetUI().presets.day", "OnClick")   # 重画一遍
    if cal_text(lua, "GCL.Calendar.GetUI().title") != "Gold Calendar Ledger":
        problems.append("英文档标题 %r" % cal_text(lua, "GCL.Calendar.GetUI().title"))
    if cal_text(lua, "GCL.Calendar.GetUI().presets.day") != "Day":
        problems.append("英文档预设按钮 %r" % cal_text(lua, "GCL.Calendar.GetUI().presets.day"))
    # 只在 Build 里摆一次的静态字（列头、星期表头、每行那颗「流水」）换档后必须跟着变
    if cal_text(lua, "GCL.Calendar.GetUI().charHead.name") != "Character":
        problems.append("英文档列头 %r" % cal_text(lua, "GCL.Calendar.GetUI().charHead.name"))
    if cal_text(lua, "GCL.Calendar.GetUI().weekdays[1]") != "Sun":
        problems.append("英文档星期表头 %r" % cal_text(lua, "GCL.Calendar.GetUI().weekdays[1]"))
    if cal_text(lua, "GCL.Calendar.GetUI().charRows[1].btn") != "Flows":
        problems.append("英文档行内按钮 %r" % cal_text(lua, "GCL.Calendar.GetUI().charRows[1].btn"))
    sub_en = cal_text(lua, "GCL.Calendar.GetUI().cells[10].sub")
    if "/" not in sub_en:
        problems.append("英文档格子小字 %r 没走 enUS 的 CELL_SUB 串（该是「收/支」斜杠分隔）" % sub_en)
    loc.Set("zhCN")
    cal_script(lua, "GCL.Calendar.GetUI().presets.day", "OnClick")   # 再重画一遍
    sub_zh = cal_text(lua, "GCL.Calendar.GetUI().cells[10].sub")
    if "·" not in sub_zh:
        problems.append("切回中文档格子小字 %r 该用「·」分隔" % sub_zh)
    if cal_text(lua, "GCL.Calendar.GetUI().charHead.name") != "号名":
        problems.append("切回中文档列头 %r" % cal_text(lua, "GCL.Calendar.GetUI().charHead.name"))
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
    loc = lua.globals()["GCL"].Locale
    problems = []
    if not bool(cal_eval(lua, "GCL.Locale.RealmHasCJK('暴龙王卡尔波斯')")):
        problems.append("国服服名没判出 CJK")
    if bool(cal_eval(lua, "GCL.Locale.RealmHasCJK('Stormrage')")):
        problems.append("英文服名被判成 CJK")
    if bool(cal_eval(lua, "GCL.Locale.RealmHasCJK(nil)")):
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
    if cal_eval(lua, "GCL.Locale.Detect()") is not None:
        problems.append("两个信号都读不出时应回 nil（保持默认档）")
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
        test_calendar_builds_nothing_until_open,
        test_calendar_static_guards,
        test_calendar_geometry_budget,
        test_calendar_grid_shapes_and_weekday_math,
        test_calendar_cell_content_today_and_colors,
        test_calendar_text_clipping_is_utf8_safe,
        test_calendar_char_drilldown_and_flow_button,
        test_calendar_flow_page_paging_and_back,
        test_calendar_range_presets,
        test_calendar_top_totals_and_missing_clock,
        test_calendar_tooltip_only_in_month_cells,
        test_calendar_esc_list_and_close_button_level,
        test_calendar_probe_ui_is_read_only,
        test_calendar_open_without_ledger_does_not_error,
        test_locale_tables_are_two_language_complete,
        test_locale_selection_and_fallbacks,
        test_locale_detect_reads_realm_bytes_not_text_locale,
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:      # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    print(f"\n{len(tests) - len(FAILED)}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
