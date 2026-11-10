# GoldCalendarLedger 离线单测（T0 起跑）：toc 静态守卫 + Core 骨架行为。
# 用法：python tests/run_gcl_tests.py   —— 全绿退出码 0，有 FAIL 退出码 1。
# 替身规则原样继承 GQL 的三条教训：只给 Core 真碰到的入口、不许凭空造客户端没有的
# 方法（取用即抛错）、生命周期用例必须走真实事件入口而不是直接调内部函数。
import os
import re
import sys

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
# 帧方法仍旧只给 Core 用到的那几个；下面这些替身都不新建帧，只补全局函数与表方法。
GCL_MOCK_ENV = r"""
UIParent = {}
SlashCmdList = {}
SLASH_GCL1 = nil
GCL_NOW = 0
GCL_OUTPUT = {}
GCL_FRAMES = {}
GCL_MONEY = 0
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
    ledger=False 用来验"账本还没注册时不许报错"那条降级。"""
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
                  entries = { { t = 1, amt = 5 } } }
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
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:      # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    print(f"\n{len(tests) - len(FAILED)}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
