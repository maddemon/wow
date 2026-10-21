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


def test_cycle_anchor_clock_domain_bridge():
    """GetQuestResetTime 若给的是会话内秒数，必须换算到纪元，否则跨一次开机就没法比。"""
    lua, wqt = load_lua("Core", "Config", mock=True)
    week = 604800
    epoch = 1700000000
    # 纪元域（>1e9）直接用，不做换算
    if int(wqt.Config.CycleAnchorOf(epoch + 900000, None, None)) != epoch + 900000 - week:
        return fail("cycle_anchor_clock_domain_bridge", "纪元域应直接减一周")
    # 会话域：resetTime 与 uptimeNow 同域，用墙上时钟搭桥
    got = int(wqt.Config.CycleAnchorOf(900000, epoch, 100000))
    if got != epoch + (900000 - 100000) - week:
        return fail("cycle_anchor_clock_domain_bridge", f"会话域应换算，实际 {got}")
    # 没有可信墙上时钟时退回会话域：宁可只在本次开机内可比，也不要算出错的数
    if int(wqt.Config.CycleAnchorOf(900000, None, None)) != 900000 - week:
        return fail("cycle_anchor_clock_domain_bridge", "无墙上时钟应沿用旧口径")
    g = lua.globals()
    g.WQTDB = lua.table_from({"cycleAnchor": 0})
    g.GetQuestResetTime = lambda: 900000
    if wqt.Config.RefreshCycle(epoch, 100000) is not True:
        return fail("cycle_anchor_clock_domain_bridge", "带墙上时钟时锚点应前进")
    if int(g.WQTDB["cycleAnchor"]) != epoch + 800000 - week:
        return fail("cycle_anchor_clock_domain_bridge", f"锚点应在纪元域，实际 {g.WQTDB['cycleAnchor']}")
    ok("cycle_anchor_clock_domain_bridge")


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


def ledger_db(lua):
    """构造可直接喂给 Ledger 的账本。"""
    return lua.table_from({
        "schema": 1, "cycleAnchor": 0,
        "quests": lua.table_from({}), "chars": lua.table_from({}),
        "turnins": lua.table_from({}), "manual": lua.table_from({}),
        "logs": lua.table_from({}),
    })


def pool(lua, rows):
    """rows: [(questID, money, items)]；mapID 固定 2007，name 自动生成 '任务<id>'。"""
    arr = lua.table_from({})
    for i, (qid, money, items) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        if items is not None:
            entry["items"] = lua.table_from(items)
        arr[i] = lua.table_from(entry)
    return arr


def test_sig_stable_and_gold():
    """签名与物品顺序无关；有金币判金币任务，0 与 nil 都不判。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    a = str(L.MakeSig(123456, lua.table_from([7, 3, 9])))
    b = str(L.MakeSig(123456, lua.table_from([9, 7, 3])))
    if a != b:
        return fail("sig_stable_and_gold", f"物品顺序不应改变签名：{a} vs {b}")
    if not L.IsGoldSig(str(L.MakeSig(123456, lua.table_from({})))):
        return fail("sig_stable_and_gold", "含金币应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(0, lua.table_from([555])))):
        return fail("sig_stable_and_gold", "无金币只掉物品不应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(None, lua.table_from({})))):
        return fail("sig_stable_and_gold", "奖励未知不应判金币任务")
    if str(L.MakeSig(1, lua.table_from({}))) == str(L.MakeSig(2, lua.table_from({}))):
        return fail("sig_stable_and_gold", "金币不同应产生不同签名")
    ok("sig_stable_and_gold")


def test_diff_added_and_gone():
    """首次抓池全部 added；下次池中消失的任务置 gone 并清 currentSig。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    r1 = L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 0, [111])]), 1000)
    if len(r1["added"]) != 2 or len(r1["gone"]) != 0:
        return fail("diff_added_and_gone", f"首轮 added={len(r1['added'])} gone={len(r1['gone'])}，期望 2/0")
    sig1 = str(db["quests"][1]["currentSig"])
    if not L.IsGoldSig(sig1):
        return fail("diff_added_and_gone", "任务 1 应判为金币任务")
    if db["quests"][1]["observations"][sig1]["gold"] is not True:
        return fail("diff_added_and_gone", "observation.gold 应为 true")
    r2 = L.DiffPool(db, pool(lua, [(1, 500000, None)]), 2000)
    if len(r2["gone"]) != 1 or int(r2["gone"][1]) != 2:
        return fail("diff_added_and_gone", f"任务 2 应消失，实际 gone 条数={len(r2['gone'])} 首项={r2['gone'][1]}")
    if db["quests"][2]["currentSig"] is not None:
        return fail("diff_added_and_gone", "消失任务应清掉 currentSig")
    if L.GetObs(db, 2) is not None:
        return fail("diff_added_and_gone", "不在池中不应有当前 observation")
    if L.GetObs(db, 1) is None:
        return fail("diff_added_and_gone", "在池任务应有 observation")
    if L.GetQuestName(db, 1) != "任务1":
        return fail("diff_added_and_gone", f"任务名应可读，实际 {L.GetQuestName(db, 1)}")
    ok("diff_added_and_gone")


def test_diff_renewed_new_round():
    """12 小时任务消失后又出现 = 新一轮：firstSeen 重置为本轮起点，goneSince 清空。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r3 = L.DiffPool(db, one, 3000)
    if len(r3["renewed"]) != 1 or int(r3["renewed"][1]) != 7:
        return fail("diff_renewed_new_round", f"应报 renewed 任务 7，实际 {len(r3['renewed'])}")
    obs = L.GetObs(db, 7)
    if obs is None:
        return fail("diff_renewed_new_round", "重新出现后应能取到 observation")
    if int(obs["firstSeen"]) != 3000:
        return fail("diff_renewed_new_round", f"新一轮 firstSeen 应为 3000，实际 {obs['firstSeen']}")
    if obs["goneSince"] is not None:
        return fail("diff_renewed_new_round", "重新出现后 goneSince 应清空")
    ok("diff_renewed_new_round")


def test_diff_sig_changed_modes():
    """同一任务奖励变化（狩猎/普通模式差异）：两条 observation 并存，金币标记各算各的。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(9, 400000, None)]), 1000)
    r = L.DiffPool(db, pool(lua, [(9, 0, [777])]), 2000)
    if len(r["sigChanged"]) != 1 or int(r["sigChanged"][1]) != 9:
        return fail("diff_sig_changed_modes", f"应只报 sigChanged，实际 gone={len(r['gone'])} added={len(r['added'])}")
    if int(L.CountObs(db, 9)) != 2:
        return fail("diff_sig_changed_modes", f"应有 2 条 observation，实际 {L.CountObs(db, 9)}")
    if L.IsGoldSig(str(db["quests"][9]["currentSig"])):
        return fail("diff_sig_changed_modes", "当前签名（无金币）不应判金币任务")
    ok("diff_sig_changed_modes")


def test_high_freq_learning():
    """消失时长 < 1.5 天的学到高频；消失 4 天的周常不学。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    day = 86400
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)      # 12 小时后消失
    L.DiffPool(db, one, 1000 + 86400)                # 又出现
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_learning", "消失 12 小时应学到高频标记")
    db2 = ledger_db(lua)
    L.DiffPool(db2, one, 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, one, 1000 + 5 * day)             # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_learning", "消失 4 天的周常不应标为高频")
    ok("high_freq_learning")


def test_diff_renewed_with_new_reward():
    """消失后换奖励回来（12 小时区每轮奖励可能不同）：算新一轮，不算 added/sigChanged。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r = L.DiffPool(db, pool(lua, [(7, 0, [888])]), 3000)
    if len(r["renewed"]) != 1 or int(r["renewed"][1]) != 7:
        return fail("diff_renewed_with_new_reward",
                    f"换奖励重现应报 renewed，实际 renewed={len(r['renewed'])} added={len(r['added'])} sigChanged={len(r['sigChanged'])}")
    if len(r["added"]) != 0 or len(r["sigChanged"]) != 0:
        return fail("diff_renewed_with_new_reward", "重现不应同时报成 added 或 sigChanged")
    if int(L.CountObs(db, 7)) != 2:
        return fail("diff_renewed_with_new_reward", f"两份奖励应各留一条 observation，实际 {L.CountObs(db, 7)}")
    if L.IsGoldSig(str(db["quests"][7]["currentSig"])):
        return fail("diff_renewed_with_new_reward", "当前这轮无金币，不应判金币任务")
    ok("diff_renewed_with_new_reward")


def test_high_freq_after_reward_rotation():
    """高频标记只看任务消失多久，不看奖励签名：12 小时区每轮奖励不同，绑签名就永远学不到。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    day = 86400
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)          # 消失 12 小时
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 1000 + 86400)  # 换奖励回来
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_after_reward_rotation", "换奖励的 12 小时重现也应学到高频")
    db2 = ledger_db(lua)
    L.DiffPool(db2, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, pool(lua, [(7, 0, [888])]), 1000 + 5 * day)  # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_after_reward_rotation", "消失 4 天的周常不应标为高频")
    ok("high_freq_after_reward_rotation")


def test_turnin_state_and_week_stats():
    """交任务入日志；本轮内显示已交；周统计只算本 CD。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 5000
    L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 300000, None)]), 6000)
    sig = str(db["quests"][1]["currentSig"])
    L.OnTurnIn(db, "A-永恒之井", 1, 6100, 500000, sig)
    if str(L.State(db, "A-永恒之井", 1)) != "done":
        return fail("turnin_state_and_week_stats", "本轮内已交应显示 done")
    if str(L.State(db, "A-永恒之井", 2)) != "open":
        return fail("turnin_state_and_week_stats", "池内未做应显示 open")
    count, gold = L.WeekStats(db, "A-永恒之井", 5000)
    if int(count) != 1 or int(gold) != 500000:
        return fail("turnin_state_and_week_stats", f"本周应 1 条/500000，实际 {count}/{gold}")
    count0, _ = L.WeekStats(db, "A-永恒之井", 7000)
    if int(count0) != 0:
        return fail("turnin_state_and_week_stats", "上个 CD 的记录不应计入本周")
    ok("turnin_state_and_week_stats")


def test_turnin_not_counted_across_rounds():
    """任务消失又出现 = 新一轮，上一轮的已交不再计入本轮。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.OnTurnIn(db, "A-永恒之井", 7, 1100, 300000, str(db["quests"][7]["currentSig"]))
    if str(L.State(db, "A-永恒之井", 7)) != "done":
        return fail("turnin_not_counted_across_rounds", "本轮做完应 done")
    L.DiffPool(db, pool(lua, []), 2000)
    if str(L.State(db, "A-永恒之井", 7)) != "ended":
        return fail("turnin_not_counted_across_rounds", "消失后应显示本轮已结束")
    L.DiffPool(db, one, 3000)
    st = str(L.State(db, "A-永恒之井", 7))
    if st != "open":
        return fail("turnin_not_counted_across_rounds", f"新一轮应回到待做，实际 {st}")
    count, _ = L.WeekStats(db, "A-永恒之井", 0)
    if int(count) != 1:
        return fail("turnin_not_counted_across_rounds", "金币历史仍留在库里（统计口径不变，本轮状态另算）")
    ok("turnin_not_counted_across_rounds")


def test_manual_beats_auto_and_survives_sync():
    """手动补录优先于自动快照；后续 SetView 不得覆盖它；撤销能还原。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(3, 200000, None)]), 1000)
    L.SetView(db, "B-永恒之井", 3, "open", 1000)
    old = L.SetManual(db, "B-永恒之井", 3, "done", 900, 150000)
    if str(L.State(db, "B-永恒之井", 3)) != "done":
        return fail("manual_beats_auto_and_survives_sync", "手动已交应覆盖自动 open")
    L.SetView(db, "B-永恒之井", 3, "open", 2000)
    if str(L.State(db, "B-永恒之井", 3)) != "done":
        return fail("manual_beats_auto_and_survives_sync", "登录同步不应覆盖手动记录")
    count, gold = L.WeekStats(db, "B-永恒之井", 0)
    if int(count) != 1 or int(gold) != 150000:
        return fail("manual_beats_auto_and_survives_sync", f"手动补录应计入统计，实际 {count}/{gold}")
    L.UndoApply(db, {"kind": "manual", "charKey": "B-永恒之井", "questID": 3, "old": old})
    if str(L.State(db, "B-永恒之井", 3)) != "open":
        return fail("manual_beats_auto_and_survives_sync", "撤销应回到自动判定的 open")
    ok("manual_beats_auto_and_survives_sync")


def test_manual_not_double_counted_with_turnin():
    """同一任务既有自动交任务记录又有手动已交：完成数只算一次。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(8, 250000, None)]), 1000)
    L.OnTurnIn(db, "E-永恒之井", 8, 1100, 250000, str(db["quests"][8]["currentSig"]))
    L.SetManual(db, "E-永恒之井", 8, "done", 1200, 250000)
    count, gold = L.WeekStats(db, "E-永恒之井", 0)
    if int(count) != 1 or int(gold) != 250000:
        return fail("manual_not_double_counted_with_turnin", f"不应重复计数，实际 {count}/{gold}")
    ok("manual_not_double_counted_with_turnin")


def test_gold_unmeasured_counts_not_sum():
    """钱差测不到（g=nil）：完成数照计，金币总额报未知（-1），不报 0。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(4, 100000, None)]), 1000)
    L.OnTurnIn(db, "C-永恒之井", 4, 1100, None, str(db["quests"][4]["currentSig"]))
    count, gold = L.WeekStats(db, "C-永恒之井", 0)
    if int(count) != 1:
        return fail("gold_unmeasured_counts_not_sum", f"count 应为 1，实际 {count}")
    if int(gold) != -1:
        return fail("gold_unmeasured_counts_not_sum", f"无实测值 gold 应为 -1，实际 {gold}")
    ok("gold_unmeasured_counts_not_sum")


def test_mark_gold_by_observation():
    """实测到账金币把未判定任务转正；手动锁定后拒绝自动改写。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(5, None, None)]), 1000)   # 奖励查不到 → 未判定
    sig = str(db["quests"][5]["currentSig"])
    if len(L.GoldQuestIDs(db)) != 0:
        return fail("mark_gold_by_observation", "未判定任务不应进金币列表")
    L.OnTurnIn(db, "F-永恒之井", 5, 1100, 300000, sig)
    if len(L.GoldQuestIDs(db)) != 1:
        return fail("mark_gold_by_observation", "实测到金币后应自动转正为金币任务")
    L.SetGoldLock(db, 5, True)
    db["quests"][5]["observations"][sig]["gold"] = False
    if L.MarkGoldByObservation(db, 5, sig) is not False:
        return fail("mark_gold_by_observation", "goldLock 时应拒绝自动改写")
    ok("mark_gold_by_observation")


def test_prune_bounds_size():
    """turnins 每任务上限 8 条；三个 CD 未出现的任务整条清出，金币历史保留。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(6, 100000, None)]), 1000)
    sig = str(db["quests"][6]["currentSig"])
    for i in range(1, 12):
        L.OnTurnIn(db, "D-永恒之井", 6, 1000 + i, 100000, sig)
    rows = db["turnins"]["D-永恒之井"][6]
    if len(rows) != 8:
        return fail("prune_bounds_size", f"turnins 应截到 8 条，实际 {len(rows)}")
    if int(rows[len(rows)]["t"]) != 1011:
        return fail("prune_bounds_size", "应丢最旧留最新")
    L.DiffPool(db, pool(lua, []), 1000)
    L.Prune(db, 1000 + 25 * 86400)
    if db["quests"][6] is not None:
        return fail("prune_bounds_size", "连续 3 个 CD 未出现应清出账本")
    if db["turnins"]["D-永恒之井"][6] is None:
        return fail("prune_bounds_size", "清理任务定义不应删掉金币历史")
    ok("prune_bounds_size")


def test_state_uses_own_snapshot_across_characters():
    """池是按角色各自的：别的角色那边任务消失，不该改写本角色自己的快照结论。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(1, 500000, None)]), 1000)
    L.SetView(db, "B-永恒之井", 1, "progress", 1500)
    L.DiffPool(db, pool(lua, []), 2000)              # 当前角色的池里再也没有这个任务
    if str(L.State(db, "B-永恒之井", 1)) != "progress":
        return fail("state_uses_own_snapshot_across_characters",
                    f"B 自己说过进行中，不该被别人的池判成已结束，实际 {L.State(db, 'B-永恒之井', 1)}")
    if str(L.State(db, "A-永恒之井", 1)) != "ended":
        return fail("state_uses_own_snapshot_across_characters", "没有快照的角色仍应显示本轮已结束")
    ok("state_uses_own_snapshot_across_characters")


def test_stale_snapshot_not_trusted_in_new_round():
    """新一轮开始后，上一轮遗留的"已交"快照不得继续冒充本轮。"""
    lua, wqt = load_lua("Config", "Ledger")
    L = wqt.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.SetView(db, "B-永恒之井", 7, "done", 1500)
    L.DiffPool(db, pool(lua, []), 2000)
    L.DiffPool(db, one, 3000)                        # 同一签名重现 = 新一轮，firstSeen=3000
    if str(L.State(db, "B-永恒之井", 7)) != "open":
        return fail("stale_snapshot_not_trusted_in_new_round",
                    f"旧快照不应跨轮，实际 {L.State(db, 'B-永恒之井', 7)}")
    ok("stale_snapshot_not_trusted_in_new_round")


def test_api_degrades_when_missing():
    """接口全缺失：GetPool 返回 nil + 原因，不抛错。"""
    _, wqt = load_lua("Config", "Ledger", "Api")
    rows, err = wqt.Api.GetPool()
    if rows is not None:
        return fail("api_degrades_when_missing", "无 API 时不应返回池数据")
    if err is None:
        return fail("api_degrades_when_missing", "必须报告降级原因")
    ok("api_degrades_when_missing")


def test_api_empty_pool_is_data_not_failure():
    """候选接口都调用成功、池真的空了：要返回空表当数据，不能报"不可用"。
    否则全清完世界任务后账本会永久冻结，"全部消失"这个转移永远测不出来。"""
    lua, wqt = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})
    rows, err = wqt.Api.GetPool()
    if rows is None:
        return fail("api_empty_pool_is_data_not_failure", f"空池应是数据，实际 err={err}")
    if len(rows) != 0:
        return fail("api_empty_pool_is_data_not_failure", f"应为 0 条，实际 {len(rows)}")
    ok("api_empty_pool_is_data_not_failure")


def test_api_state_unknown_when_missing():
    """完成与接取接口都缺失：QuestState 返回 unknown，绝不返回 open。"""
    _, wqt = load_lua("Config", "Ledger", "Api")
    state, err = wqt.Api.QuestState(123)
    if str(state) != "unknown":
        return fail("api_state_unknown_when_missing", f"应返回 unknown，实际 {state}")
    if err is None:
        return fail("api_state_unknown_when_missing", "应带降级原因")
    ok("api_state_unknown_when_missing")


def test_api_normalizes_known_shape():
    """模拟 WorldQuestInfo 形状：字段名差异要归一，奖励接口缺失要留 nil。"""
    lua, wqt = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    # 用 list 而不是 set 字面量：lua.table_from(set) 的迭代顺序按 Python 对象哈希走，
    # 会随机交换两行，rows[1]/rows[2] 的断言就变成不稳定测试。
    g.C_WorldQuests = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from([
            lua.table_from({"questID": 11, "mapID": 2007, "name": "补给车队"}),
            lua.table_from({"questID": 12, "zoneID": 2008, "title": "虚空突袭"}),
        ])
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid == 11,
        "IsQuestCompleted": lambda qid: False,
    })
    rows, err = wqt.Api.GetPool()
    if rows is None:
        return fail("api_normalizes_known_shape", f"应取到池数据，err={err}")
    if len(rows) != 2:
        return fail("api_normalizes_known_shape", f"应有 2 条，实际 {len(rows)}")
    if int(rows[1]["questID"]) != 11 or int(rows[1]["mapID"]) != 2007:
        return fail("api_normalizes_known_shape", f"条目 1 归一错误：{rows[1]['questID']}/{rows[1]['mapID']}")
    if int(rows[2]["questID"]) != 12 or int(rows[2]["mapID"]) != 2008:
        return fail("api_normalizes_known_shape", f"zoneID 应可当 mapID，实际 {rows[2]['mapID']}")
    if str(rows[2]["name"]) != "虚空突袭":
        return fail("api_normalizes_known_shape", "title 应可当 name")
    if rows[1]["money"] is not None:
        return fail("api_normalizes_known_shape", "奖励接口缺失时 money 必须是 nil（未判定），不能当 0")
    started, _ = wqt.Api.QuestState(11)
    if str(started) != "progress":
        return fail("api_normalizes_known_shape", f"IsQuestStarted 为真应得 progress，实际 {started}")
    done, _ = wqt.Api.QuestState(999)
    if str(done) != "open":
        return fail("api_normalizes_known_shape", f"两个接口都为假应得 open，实际 {done}")
    ok("api_normalizes_known_shape")


def test_probe_report_and_command_registered():
    """探测报告列出每个候选的可用性；Probe/LogWindow 应注册 /wqt 子命令。"""
    lua, wqt = load_lua("Config", "Ledger", "Api", "Probe", "LogWindow", mock=True)
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})
    report = str(wqt.Probe.Run())
    if "C_WorldQuests.GetWorldQuests" not in report:
        return fail("probe_report_and_command_registered", "报告应含候选接口名")
    if "AVAILABLE" not in report or "MISSING" not in report:
        return fail("probe_report_and_command_registered", "报告应区分可用/缺失")
    if "LastDegrade" not in report:
        return fail("probe_report_and_command_registered", "报告应带降级原因")
    for cmd in ("probe", "log"):
        if wqt.commands[cmd] is None:
            return fail("probe_report_and_command_registered", f"/wqt {cmd} 未注册")
    ok("probe_report_and_command_registered")


def fire(wqt, event, *args):
    """经 Sync 自己的帧驱动事件，避免绕开真实分发路径。"""
    frame = wqt.Sync.frame
    handler = frame.GetScript(frame, "OnEvent")
    return handler(frame, event, *args)


def sync_env(quests, started=None, completed=None, money=0, reset=700000,
             char=("小号一", "永恒之井"), visible=None, extra=()):
    """搭好 Core+Config+Ledger+Api+Sync 的可控环境。

    quests: [(questID, money)]；money 为 None 表示奖励接口取不到。
    """
    lua, wqt = load_lua("Core", "Config", "Ledger", "Api", "Sync", *extra, mock=True)
    g = lua.globals()
    g.WQT_NOW = 0
    g.WQTDB = lua.table_from({})

    rows = lua.table_from({})
    for i, (qid, gold) in enumerate(quests, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if gold is not None:
            entry["money"] = gold
        rows[i] = lua.table_from(entry)
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    started = started or set()
    completed = completed or set()
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid in started,
        "IsQuestCompleted": lambda qid: qid in completed,
    })
    g.GetQuestResetTime = lambda: reset
    g.UnitName = lambda *a: char[0]
    g.GetRealmName = lambda: char[1]
    holder = lua.table_from({})
    g.WQT_MONEY = holder
    holder["v"] = money
    g.GetMoney = lambda: holder["v"]
    if visible:
        g.FrameIsVisible = lambda name: name in visible
    wqt.Config.Init()
    return lua, wqt, holder


def test_snapshot_records_added_and_views():
    """登录快照：新任务入表，本角色三态写进 views，只有真含金币的判金币任务。"""
    lua, wqt, _ = sync_env([(21, 400000), (22, 0)], started={22})
    res = wqt.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_records_added_and_views", f"快照应成功，实际 {res}")
    if len(res["added"]) != 2:
        return fail("snapshot_records_added_and_views", f"应有 2 个新任务，实际 {len(res['added'])}")
    key = str(wqt.Sync.CurrentCharKey())
    if key != "小号一-永恒之井":
        return fail("snapshot_records_added_and_views", f"角色 key 错误：{key}")
    views = lua.globals().WQTDB["chars"][key]["views"]
    if str(views[21]["s"]) != "open":
        return fail("snapshot_records_added_and_views", f"任务 21 应待做，实际 {views[21]['s']}")
    if str(views[22]["s"]) != "progress":
        return fail("snapshot_records_added_and_views", f"任务 22 应已接未交，实际 {views[22]['s']}")
    if len(wqt.Ledger.GoldQuestIDs(lua.globals().WQTDB)) != 1:
        return fail("snapshot_records_added_and_views", "只有任务 21 应判金币任务")
    ok("snapshot_records_added_and_views")


def test_snapshot_degrades_to_unknown():
    """完成状态接口缺失：views 写 unknown 而非 open，degrade 带原因。"""
    lua, wqt, _ = sync_env([(31, None)])
    g = lua.globals()
    g.C_QuestLog = None      # 状态接口整体不可用
    res = wqt.Sync.SnapshotNow()
    if res is None:
        return fail("snapshot_degrades_to_unknown", "缺状态接口不应整体失败")
    key = str(wqt.Sync.CurrentCharKey())
    st = str(g.WQTDB["chars"][key]["views"][31]["s"])
    if st != "unknown":
        return fail("snapshot_degrades_to_unknown", f"应写 unknown，实际 {st}")
    ok("snapshot_degrades_to_unknown")


def test_pool_unavailable_keeps_ledger():
    """池接口不可用：不动账本，不把全部任务判成消失。"""
    lua, wqt, _ = sync_env([(41, 500000)])
    g = lua.globals()
    wqt.Sync.SnapshotNow()
    before = int(g.WQTDB["quests"][41]["lastSeenAt"])
    g.C_WorldQuests = None           # 接口整个消失
    res = wqt.Sync.SnapshotNow()
    if res is None or res["degrade"] is None:
        return fail("pool_unavailable_keeps_ledger", "应返回降级原因")
    if int(g.WQTDB["quests"][41]["lastSeenAt"]) != before:
        return fail("pool_unavailable_keeps_ledger", "池不可用时账本不应变化")
    if g.WQTDB["quests"][41]["currentSig"] is None:
        return fail("pool_unavailable_keeps_ledger", "不应把读不到当成任务消失")
    ok("pool_unavailable_keeps_ledger")


def test_empty_pool_needs_second_read():
    """空池是数据，但登录那一瞬接口未必就绪。一次空读不许判全部消失：
    要 30 秒后第二次仍为空才入库，否则本轮已交会被一次误读毁掉。"""
    lua, wqt, _ = sync_env([(61, 500000)])
    g = lua.globals()
    wqt.Sync.SnapshotNow()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})

    res = wqt.Sync.SnapshotNow()
    if res is None or res["pendingEmpty"] is None:
        return fail("empty_pool_needs_second_read", f"首次空读应只等确认，实际 {res}")
    if g.WQTDB["quests"][61]["currentSig"] is None:
        return fail("empty_pool_needs_second_read", "首次空读不应把任务判成消失")

    g.WQT_NOW = 31
    res = wqt.Sync.SnapshotNow()
    if res is None or res["pendingEmpty"] is not None or int(res["poolCount"]) != 0:
        return fail("empty_pool_needs_second_read", f"二次空读应入库，实际 {res}")
    if g.WQTDB["quests"][61]["currentSig"] is not None:
        return fail("empty_pool_needs_second_read", "二次确认后应记为消失")
    ok("empty_pool_needs_second_read")


def test_turnin_money_window_capture_and_abort():
    """交任务：窗口内钱差入账；期间开过邮箱则作废记 nil（未知），不记可疑数字。"""
    lua, wqt, money = sync_env([(51, 500000)], visible={"MailFrame"})
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    g = lua.globals()
    money["v"] = 1000000

    fire(wqt, "QUEST_COMPLETE", 51)
    money["v"] = 1600000
    g.FrameIsVisible = lambda name: False
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][51]
    if rows is None or len(rows) != 1:
        return fail("turnin_money_window_capture_and_abort", f"应写 1 条记录，实际 {rows}")
    if int(rows[1]["g"]) != 600000:
        return fail("turnin_money_window_capture_and_abort", f"增量应 600000，实际 {rows[1]['g']}")
    if str(wqt.Ledger.State(g.WQTDB, key, 51)) != "done":
        return fail("turnin_money_window_capture_and_abort", "交完应显示已交")

    fire(wqt, "QUEST_COMPLETE", 51)
    money["v"] = 2200000
    g.FrameIsVisible = lambda name: name == "MailFrame"
    g.WQT_NOW = 40
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][51]
    if len(rows) != 2:
        return fail("turnin_money_window_capture_and_abort", "第二次也应入账（新一轮）")
    if rows[2]["g"] is not None:
        return fail("turnin_money_window_capture_and_abort", "窗口内开过邮箱应放弃测量记 nil")
    ok("turnin_money_window_capture_and_abort")


def test_api_frame_visible_candidates():
    """帧可见性必须走 Api：Sync 直接调全局函数，12.x 形态既测不到也报不进 probe，
    钱差窗口的"开过邮箱就作废"会静默失效。两条候选都要试，都没有时降级为 false。"""
    lua, wqt = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    if wqt.Api.FrameVisible is None:
        return fail("api_frame_visible_candidates", "Api.FrameVisible 尚未实现")
    if bool(wqt.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "两条候选都缺失应返回 false")
    # lupa 的 Python 迭代给的是键不是值，必须按 1-based 下标取值
    table = wqt.Api.CandidatePaths()
    paths = [str(table[i]) for i in range(1, len(table) + 1)]
    if "FrameIsVisible" not in paths:
        return fail("api_frame_visible_candidates", "probe 候选应含 FrameIsVisible")

    g.FrameIsVisible = lambda name: name == "MailFrame"
    if not bool(wqt.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "函数候选可用时应返回可见")
    g.FrameIsVisible = None

    frame = lua.table_from({"IsShown": lambda self: True})
    g.MailFrame = frame
    if not bool(wqt.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "应回退到 _G[名字]:IsShown()")
    if bool(wqt.Api.FrameVisible("AuctionFrame")):
        return fail("api_frame_visible_candidates", "不存在的帧应返回 false")
    ok("api_frame_visible_candidates")


def test_money_window_polluted_by_frame_object():
    """窗口里邮箱帧存在且 IsShown：这笔入账作废记 nil，不记可疑数字。"""
    lua, wqt, money = sync_env([(55, 500000)])
    g = lua.globals()
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(wqt, "QUEST_COMPLETE", 55)
    money["v"] = 1600000
    g.MailFrame = lua.table_from({"IsShown": lambda self: True})
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][55]
    if rows is None or len(rows) != 1:
        return fail("money_window_polluted_by_frame_object", f"应写 1 条记录，实际 {rows}")
    if rows[1]["g"] is not None:
        return fail("money_window_polluted_by_frame_object", f"邮箱帧开着应作废记 nil，实际 {rows[1]['g']}")
    ok("money_window_polluted_by_frame_object")


def test_second_turnin_event_does_not_reset_window():
    """QUEST_COMPLETE 与 QUEST_TURNED_IN 都会触发：窗口起点必须取第一个事件，
    否则钱已在两个事件之间到账，起点被覆盖成到账后的数，这笔金币会记成 0。"""
    lua, wqt, money = sync_env([(57, 500000)])
    g = lua.globals()
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(wqt, "QUEST_COMPLETE", 57)
    money["v"] = 1300000        # 任务直接给的钱在两个事件之间到账
    fire(wqt, "QUEST_TURNED_IN", 57)
    money["v"] = 1600000        # 随后开宝箱
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][57]
    if rows is None or len(rows) != 1:
        return fail("second_turnin_event_does_not_reset_window", f"应只写 1 条记录，实际 {rows}")
    if int(rows[1]["g"]) != 600000:
        return fail("second_turnin_event_does_not_reset_window",
                    f"窗口应覆盖两个事件的进账，合计 600000，实际 {rows[1]['g']}")
    ok("second_turnin_event_does_not_reset_window")


def test_back_to_back_turnins_both_recorded():
    """连着交两个任务：前一笔立刻结算，两个任务都要有记录。
    只保留第一个窗口的写法会把后一个任务整笔丢掉，账本上就变成"没做"。"""
    lua, wqt, money = sync_env([(58, 500000), (59, 400000)])
    g = lua.globals()
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(wqt, "QUEST_COMPLETE", 58)
    money["v"] = 1300000
    fire(wqt, "QUEST_COMPLETE", 59)
    money["v"] = 1700000
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rowsA = g.WQTDB["turnins"][key][58]
    rowsB = g.WQTDB["turnins"][key][59]
    if rowsA is None or len(rowsA) != 1 or int(rowsA[1]["g"]) != 300000:
        return fail("back_to_back_turnins_both_recorded", f"任务 58 应立刻结算 300000，实际 {rowsA}")
    if rowsB is None or len(rowsB) != 1 or int(rowsB[1]["g"]) != 400000:
        return fail("back_to_back_turnins_both_recorded", f"任务 59 应结算 400000，实际 {rowsB}")
    ok("back_to_back_turnins_both_recorded")


def test_money_window_samples_during_window():
    """污染要在整段窗口里盯，不能只在结算那一刻看：
    交完任务开邮箱捡钱再关掉，15 秒后结算那一刻帧已经隐了，只在结尾采样会把这笔外来入账记到任务头上。"""
    lua, wqt, money = sync_env([(61, 500000)])
    g = lua.globals()
    key = str(wqt.Sync.CurrentCharKey())
    wqt.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(wqt, "QUEST_COMPLETE", 61)
    g.MailFrame = lua.table_from({"IsShown": lambda self: True})
    money["v"] = 1400000
    g.WQT_NOW = 5
    lua.globals().WQT_FireTimers()      # 窗口内的那次采样
    g.MailFrame = None                  # 捡完钱关掉
    money["v"] = 1500000
    g.WQT_NOW = 20
    wqt.Sync.FinishWindow()
    rows = g.WQTDB["turnins"][key][61]
    if rows is None or len(rows) != 1:
        return fail("money_window_samples_during_window", f"应写 1 条记录，实际 {rows}")
    if rows[1]["g"] is not None:
        return fail("money_window_samples_during_window",
                    f"窗口中途开过邮箱就应整笔作废记 nil，实际 {rows[1]['g']}")
    ok("money_window_samples_during_window")


def test_debounce_single_snapshot():
    """世界任务更新事件连发只抓一次池（2 秒去抖）。"""
    lua, wqt, _ = sync_env([(61, 100000)])
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    wqt.Sync.OnWorldEnter()
    for _ in range(5):
        fire(wqt, "WORLD_QUEST_LOG_UPDATE")
    g.WQT_NOW = 3
    lua.globals().WQT_FireTimers()
    if len(calls) != 1:
        return fail("debounce_single_snapshot", f"去抖后应只抓一次池，实际 {len(calls)}")
    ok("debounce_single_snapshot")


def test_dump_sections():
    """dump 应含角色汇总、任务行、列头角色名、降级与未知计数。"""
    lua, wqt, _ = sync_env([(71, 250000), (72, None)], extra=("Dump",))
    g = lua.globals()
    wqt.Sync.OnWorldEnter()
    g.WQT_NOW = 5
    g.WQT_FireTimers()          # 走真实去抖路径，而不是直调 SnapshotNow
    text = str(wqt.Dump.Run())
    if "本周汇总" not in text:
        return fail("dump_sections", "缺少角色汇总段")
    if "任务71" not in text:
        return fail("dump_sections", "缺少金币任务行")
    if "小号一-永恒之井" not in text:
        return fail("dump_sections", "缺少列头角色名")
    if "未判定=1" not in text:
        return fail("dump_sections", "奖励接口取不到的任务 72 应计入未判定")
    if "降级" not in text:
        return fail("dump_sections", "缺少降级计数段")
    if wqt.commands["dump"] is None:
        return fail("dump_sections", "/wqt dump 未注册")
    ok("dump_sections")


def test_dump_shows_stale_char():
    """两周没上线的角色应标注快照过期，而不是显示成全部没做。"""
    lua, wqt, _ = sync_env([(71, 250000)], extra=("Dump",))
    g = lua.globals()
    wqt.Sync.SnapshotNow()
    g.WQT_NOW = 20 * 86400
    g.UnitName = lambda *a: "挂机号"
    g.GetRealmName = lambda: "永恒之井"
    wqt.Sync.SnapshotNow()
    g.WQT_NOW = 20 * 86400 + 14 * 86400
    text = str(wqt.Dump.Run())
    if "过期" not in text:
        return fail("dump_shows_stale_char", "久未同步的角色应显示过期标记")
    ok("dump_shows_stale_char")


if __name__ == "__main__":
    tests = [
        test_config_init_builds_schema,
        test_config_init_idempotent,
        test_cycle_anchor_rolls_over,
        test_cycle_anchor_survives_missing_api,
        test_cycle_anchor_clock_domain_bridge,
        test_format_gold,
        test_log_ring_buffer,
        test_sig_stable_and_gold,
        test_diff_added_and_gone,
        test_diff_renewed_new_round,
        test_diff_sig_changed_modes,
        test_high_freq_learning,
        test_diff_renewed_with_new_reward,
        test_high_freq_after_reward_rotation,
        test_turnin_state_and_week_stats,
        test_turnin_not_counted_across_rounds,
        test_manual_beats_auto_and_survives_sync,
        test_manual_not_double_counted_with_turnin,
        test_gold_unmeasured_counts_not_sum,
        test_mark_gold_by_observation,
        test_prune_bounds_size,
        test_state_uses_own_snapshot_across_characters,
        test_stale_snapshot_not_trusted_in_new_round,
        test_api_degrades_when_missing,
        test_api_empty_pool_is_data_not_failure,
        test_api_state_unknown_when_missing,
        test_api_normalizes_known_shape,
        test_probe_report_and_command_registered,
        test_snapshot_records_added_and_views,
        test_snapshot_degrades_to_unknown,
        test_pool_unavailable_keeps_ledger,
        test_empty_pool_needs_second_read,
        test_turnin_money_window_capture_and_abort,
        test_api_frame_visible_candidates,
        test_money_window_polluted_by_frame_object,
        test_second_turnin_event_does_not_reset_window,
        test_back_to_back_turnins_both_recorded,
        test_money_window_samples_during_window,
        test_debounce_single_snapshot,
        test_dump_sections,
        test_dump_shows_stale_char,
    ]
    for t in tests:
        t()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
