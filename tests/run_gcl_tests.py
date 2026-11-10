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


# WoW 环境替身：只列 Core.lua 真正碰到的入口。GetTime 留在名单里是给 T1 的
# transfer TTL（客户端 API，GQL 侧已实测存在）；其余一律不加。
GCL_MOCK_ENV = r"""
UIParent = {}
SlashCmdList = {}
SLASH_GCL1 = nil
GCL_NOW = 0
GCL_OUTPUT = {}
GCL_FRAMES = {}
-- 只列 Core.lua 现在注册的两个事件；真客户端对未知事件名是直接抛错
-- （12.1.5 实测 "Attempt to register unknown event"），替身照做。
-- T1 加 PLAYER_MONEY 等事件时必须同步扩这张表（存在性已在本仓库别处实测）。
GCL_KNOWN_EVENTS = { ADDON_LOADED = true, PLAYER_LOGIN = true }
function GetTime() return GCL_NOW or 0 end
function print(...)
    for i = 1, select("#", ...) do
        GCL_OUTPUT[#GCL_OUTPUT + 1] = tostring((select(i, ...)))
    end
    return ...
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
    for probe in ("", "nosuchcommand"):
        lines = slash(lua, probe)
        joined = " / ".join(lines)
        if "出错" in joined:
            return fail("dump_and_version_commands_do_not_error", "%r 路径报错：%s" % (probe, lines))
        if not any("dump | version" in ln for ln in lines):
            return fail("dump_and_version_commands_do_not_error",
                        "%r 应打印排序后的命令清单：%s" % (probe, lines))
    ok("dump_and_version_commands_do_not_error")


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
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:      # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    print(f"\n{len(tests) - len(FAILED)}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
