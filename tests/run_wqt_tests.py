# WorldQuestTracker 阶段一离线单测：纯数据逻辑 + API 适配降级 + 同步时序
# 用法：python tests/run_wqt_tests.py            —— 运行单测
#       python tests/run_wqt_tests.py --syntax    —— 额外对插件全部 Lua 做 loadfile 语法检查
import os
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "WorldQuestTracker")
FAILED = []


def fail(name, msg):
    FAILED.append(name)
    print(f"FAIL {name}: {msg}")


def ok(name):
    print(f"PASS {name}")


# WoW 环境替身：只提供本阶段真正用到的入口。
# C_Timer.After 不自动执行，测试用 WQT_FireTimers() 手动推进（配合 WQT_NOW），
# 否则每个用例都要真等 2 秒/15 秒。delay 未到期的定时器必须留在队列里。
WQT_MOCK_ENV = r"""
UIParent = {}
WQT_LOG = {}
WQT_NOW = 0
C_Timer = { After = function(delay, fn) table.insert(WQT_LOG, { delay = delay, fn = fn }) end }
function WQT_FireTimers()
    local pending, fired = WQT_LOG, 0
    WQT_LOG = {}
    for i = 1, #pending do
        if pending[i].delay <= WQT_NOW then
            fired = fired + 1
            pending[i].fn()
        else
            table.insert(WQT_LOG, pending[i])
        end
    end
    return fired
end
function GetTime() return WQT_NOW or 0 end
SlashCmdList = {}
SLASH_WQT1 = nil
function print(...) return ... end
"""


def load_lua(*names, mock=False):
    """按 toc 顺序加载 Lua 文件。mock=True 时先注入 WoW 替身（含 CreateFrame）。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    if mock:
        lua.execute(WQT_MOCK_ENV)
        lua.execute(r"""
        function CreateFrame(frameType, name, parent, template)
            return { __events = {}, __scripts = {}, __shown = false,
                     RegisterEvent = function(self, e) self.__events[e] = true end,
                     UnregisterEvent = function(self, e) self.__events[e] = nil end,
                     SetScript = function(self, k, fn) self.__scripts[k] = fn end,
                     GetScript = function(self, k) return self.__scripts[k] end,
                     IsShown = function(self) return self.__shown end,
                     Show = function(self) self.__shown = true end,
                     Hide = function(self) self.__shown = false end }
        end
        """)
    # 命名空间：游戏里由 Core.lua 的 vararg 提供，测试里预置空表
    lua.execute(r"""
        WQT = WQT or {}
        WQT.commands = {}
        function WQT.RegisterCommand(name, fn, help) WQT.commands[name] = { fn = fn, help = help or "" } end
    """)
    for name in names:
        with open(os.path.join(PLUGIN_DIR, name + ".lua"), encoding="utf-8") as f:
            lua.execute(f.read())
    return lua, lua.globals().WQT


def syntax_check():
    """WoW 专有全局在 loadfile 阶段不参与解析，所以这一步只抓语法错误。"""
    for name in sorted(os.listdir(PLUGIN_DIR)):
        if not name.endswith(".lua"):
            continue
        lua = LuaRuntime()
        # lupa 单值返回不套 tuple，成功时 loadfile 只回一个 chunk；失败时才回 (nil, err)。
        result = lua.globals().loadfile(os.path.join(PLUGIN_DIR, name).replace("\\", "/"))
        if isinstance(result, tuple):
            chunk, err = result
        else:
            chunk, err = result, None
        if chunk is None:
            fail("syntax:%s" % name, err)
        else:
            ok("syntax:%s" % name)


def test_config_init_builds_schema():
    """空库 Init 后建齐四张表并写入 schema 与 cycleAnchor。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    g.GetQuestResetTime = lambda: 1200000
    wqt.Config.Init()
    db = g.WQTDB
    for key in ("quests", "chars", "turnins", "manual"):
        if db[key] is None:
            return fail("config_init_builds_schema", f"{key} 表未建立")
    if int(db["schema"]) != 1:
        return fail("config_init_builds_schema", f"schema={db['schema']}, 期望 1")
    if int(db["cycleAnchor"]) != 1200000 - 604800:
        return fail("config_init_builds_schema", f"cycleAnchor={db['cycleAnchor']}")
    ok("config_init_builds_schema")


def test_config_init_idempotent():
    """重复 Init 不得清空已有账本。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    g.GetQuestResetTime = lambda: 1200000
    wqt.Config.Init()
    g.WQTDB["quests"][42] = lua.table_from({"name": "测试任务"})
    wqt.Config.Init()
    if g.WQTDB["quests"][42] is None:
        return fail("config_init_idempotent", "重复 Init 丢失已有任务记录")
    ok("config_init_idempotent")


def test_cycle_anchor_rolls_over():
    """重置时间前移（进入新 CD）→ 锚点更新；未到期 → 不回退。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({"cycleAnchor": 100})
    g.GetQuestResetTime = lambda: 700000 + 604800
    if wqt.Config.RefreshCycle() is not True:
        return fail("cycle_anchor_rolls_over", "新 CD 应返回 true")
    if int(g.WQTDB["cycleAnchor"]) != 700000:
        return fail("cycle_anchor_rolls_over", f"锚点应为 700000，实际 {g.WQTDB['cycleAnchor']}")
    g.GetQuestResetTime = lambda: 100 + 604800
    wqt.Config.RefreshCycle()
    if int(g.WQTDB["cycleAnchor"]) != 700000:
        return fail("cycle_anchor_rolls_over", "未到期不应回退锚点")
    ok("cycle_anchor_rolls_over")


def test_cycle_anchor_survives_missing_api():
    """GetQuestResetTime 缺失时沿用旧锚点，不报错。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({"cycleAnchor": 555})
    if wqt.Config.RefreshCycle() is not False:
        return fail("cycle_anchor_survives_missing_api", "接口缺失应返回 false")
    if int(g.WQTDB["cycleAnchor"]) != 555:
        return fail("cycle_anchor_survives_missing_api", "锚点不应被改")
    ok("cycle_anchor_survives_missing_api")


def test_format_gold():
    _, wqt = load_lua("Config")
    # 1 金 = 100 银 = 10000 铜
    if str(wqt.Config.FormatGold(12345)) != "1g23s45c":
        return fail("format_gold", f"12345 铜应显示 1g23s45c，实际 {wqt.Config.FormatGold(12345)}")
    if str(wqt.Config.FormatGold(123450)) != "12g34s50c":
        return fail("format_gold", f"123450 铜应显示 12g34s50c，实际 {wqt.Config.FormatGold(123450)}")
    if str(wqt.Config.FormatGold(None)) != "?":
        return fail("format_gold", "nil 必须显示 ?（未知），不能显示 0")
    if str(wqt.Config.FormatGold(0)) != "0g0s0c":
        return fail("format_gold", "0 应显示 0g0s0c（真的没赚钱）")
    ok("format_gold")


def test_log_ring_buffer():
    """日志超过 200 条丢最旧。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.WQTDB = lua.table_from({})
    for i in range(1, 206):
        wqt.Config.Log("行%d" % i)
    logs = g.WQTDB["logs"]
    if len(logs) != 200:
        return fail("log_ring_buffer", f"日志条数={len(logs)}, 期望 200")
    text = str(wqt.Config.GetLogs())
    if "行205" not in text or "行1 " in text:
        return fail("log_ring_buffer", "应保留最新 200 条")
    ok("log_ring_buffer")


if __name__ == "__main__":
    tests = [
        test_config_init_builds_schema,
        test_config_init_idempotent,
        test_cycle_anchor_rolls_over,
        test_cycle_anchor_survives_missing_api,
        test_format_gold,
        test_log_ring_buffer,
    ]
    for t in tests:
        t()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
