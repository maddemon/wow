# AHSnipe 测试运行器：PriceCluster / BuyQueue 单测 + Lua 语法检查
# 用法：python tests/run_tests.py            —— 运行单测
#       python tests/run_tests.py --syntax    —— 额外对全部 Lua 文件做 loadfile 语法检查
import os
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "AHSnipe")
PRICE_CLUSTER_PATH = os.path.join(PLUGIN_DIR, "PriceCluster.lua")
BUY_QUEUE_PATH = os.path.join(PLUGIN_DIR, "BuyQueue.lua")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "Config.lua")
SNIPE_LIST_PATH = os.path.join(PLUGIN_DIR, "SnipeList.lua")

FAILED = []


def fail(name, msg):
    FAILED.append(name)
    print(f"FAIL {name}: {msg}")


def ok(name):
    print(f"PASS {name}")


def load_cluster():
    """加载 PriceCluster.lua，返回 Lua Runtime 与其全局表。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    with open(PRICE_CLUSTER_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, lua.globals().AHSnipePriceCluster


def listings(lua, pairs):
    """由 [(unitPrice, quantity), ...] 在 Lua 侧构造 listing 数组。"""
    lua.execute("L = {}")
    tbl = lua.globals().L
    for i, (p, q) in enumerate(pairs, start=1):
        tbl[i] = lua.table_from({"unitPrice": p, "quantity": q})
    return tbl


def load_buy_queue():
    """加载 BuyQueue.lua（复用 MOCK_ENV 的 WoW mock），驱动购买链路，返回运行时、BuyQueue 表与列表结算记录器。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda *a: None,
            "GetItemDisplayName": lambda i: "物品%s" % i,
        }),
    })
    finished = []
    g.AHSnipe["SnipeList"] = lua.table_from({
        "OnBuyFinished": lambda s: finished.append(s),
    })
    with open(BUY_QUEUE_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    g.AHSnipe.BuyQueue.Init()
    return lua, g.AHSnipe.BuyQueue, finished


def matched(lua, pairs):
    """由 [(unitPrice, quantity), ...] 在 Lua 侧构造命中挂单（matched）数组。"""
    lua.execute("M = {}")
    tbl = lua.globals().M
    for i, (p, q) in enumerate(pairs, start=1):
        tbl[i] = lua.table_from({"unitPrice": p, "quantity": q})
    return tbl


def test_screenshot_scene():
    """截图场景：诱饵 1789/1799，主簇 2950 起。"""
    lua, pc = load_cluster()
    ls = listings(lua, [(1789, 1), (1799, 2), (2950, 340), (2953, 7922), (2955, 326)])
    threshold = pc.GetThreshold(ls, 0.65)
    if threshold is None:
        return fail("screenshot_scene", "threshold 为 nil")
    if abs(threshold - 1917.5) > 0.01:
        return fail("screenshot_scene", f"threshold={threshold}, 期望 1917.5")
    if not (1789 <= threshold and 1799 <= threshold):
        return fail("screenshot_scene", "诱饵价未落入门槛内")
    if 2950 <= threshold:
        return fail("screenshot_scene", "主簇最低价不应命中")
    ok("screenshot_scene")


def test_bait_cluster_together():
    """多条诱饵价应归入同一低价簇。"""
    lua, pc = load_cluster()
    ls = listings(lua, [(1789, 1), (1799, 2), (1810, 3), (2950, 100)])
    clusters = pc.BuildClusters(ls)
    if len(clusters) != 2:
        return fail("bait_cluster_together", f"簇数={len(clusters)}, 期望 2")
    low = clusters[1]  # lupa 中 Lua 数组为 1-based，第一个是最低价簇
    if low["totalQuantity"] != 6:
        return fail("bait_cluster_together", f"低价簇挂单总数={low['totalQuantity']}, 期望 6")
    if abs(low["minPrice"] - 1789) > 0.01 or abs(low["maxPrice"] - 1810) > 0.01:
        return fail("bait_cluster_together", f"低价簇区间 [{low['minPrice']},{low['maxPrice']}]")
    ok("bait_cluster_together")


def test_single_listing():
    """仅 1 条挂单：门槛 = 单价 × 0.65。"""
    lua, pc = load_cluster()
    ls = listings(lua, [(2950, 10)])
    threshold = pc.GetThreshold(ls, 0.65)
    if threshold is None or abs(threshold - 1917.5) > 0.01:
        return fail("single_listing", f"threshold={threshold}, 期望 1917.5")
    ok("single_listing")


def test_empty_listings():
    """空挂单：门槛 nil。"""
    lua, pc = load_cluster()
    if pc.GetThreshold(listings(lua, []), 0.65) is not None:
        return fail("empty_listings", "空列表应返回 nil")
    ok("empty_listings")


def test_tie_takes_higher_cluster():
    """挂单数并列：取价格更高的簇。"""
    lua, pc = load_cluster()
    ls = listings(lua, [(1000, 50), (1800, 50), (3000, 50), (3100, 50)])
    threshold = pc.GetThreshold(ls, 0.65)
    if threshold is None:
        return fail("tie_takes_higher_cluster", "threshold 为 nil")
    # 主簇应为 [3000,3100]（与低价簇并列 100 件，取更高价），门槛 = 3000×0.65=1950
    if abs(threshold - 1950) > 0.01:
        return fail("tie_takes_higher_cluster", f"threshold={threshold}, 期望 1950")
    ok("tie_takes_higher_cluster")


def test_uniform_price():
    """全同价：单一簇，门槛 = 单价 × 0.65。"""
    lua, pc = load_cluster()
    ls = listings(lua, [(2000, 30), (2000, 45)])
    clusters = pc.BuildClusters(ls)
    if len(clusters) != 1:
        return fail("uniform_price", f"簇数={len(clusters)}, 期望 1")
    threshold = pc.GetThreshold(ls, 0.65)
    if threshold is None or abs(threshold - 1300) > 0.01:
        return fail("uniform_price", f"threshold={threshold}, 期望 1300")
    ok("uniform_price")


def test_is_bait_like():
    """诱饵标注判定：数量 ≤ 10 且单价 ≤ 主簇最低价 × 0.65。"""
    lua, pc = load_cluster()
    if not pc.IsBaitLike(lua.table_from({"unitPrice": 1789, "quantity": 1}), 2950):
        return fail("is_bait_like", "1789/1 应判为诱饵")
    if pc.IsBaitLike(lua.table_from({"unitPrice": 1789, "quantity": 500}), 2950):
        return fail("is_bait_like", "数量 500 不应判为诱饵")
    if pc.IsBaitLike(lua.table_from({"unitPrice": 1950, "quantity": 1}), 2950):
        return fail("is_bait_like", "单价 1950(>0.65×2950) 不应判为诱饵")
    ok("is_bait_like")


def test_commodity_chunks_sorted():
    """commodity 分块计划：按单价升序，每块取一条挂单的数量。"""
    lua, bq, _ = load_buy_queue()
    ls = matched(lua, [(260000, 5), (136000, 12), (136200, 1)])
    chunks = bq.BuildCommodityChunks(ls)
    n = len(chunks)
    if n != 3:
        return fail("commodity_chunks_sorted", f"块数={n}, 期望 3")
    prices = [chunks[i]["unitPrice"] for i in range(1, n + 1)]
    qtys = [chunks[i]["quantity"] for i in range(1, n + 1)]
    if prices != [136000, 136200, 260000]:
        return fail("commodity_chunks_sorted", f"单价顺序={prices}, 期望升序")
    if qtys != [12, 1, 5]:
        return fail("commodity_chunks_sorted", f"数量={qtys}, 期望按所属挂单")
    ok("commodity_chunks_sorted")


def test_commodity_chunks_same_price():
    """同单价按数量升序，先买小堆（被抢光的概率低）。"""
    lua, bq, _ = load_buy_queue()
    ls = matched(lua, [(136000, 7), (136000, 3)])
    chunks = bq.BuildCommodityChunks(ls)
    if len(chunks) != 2 or chunks[1]["quantity"] != 3 or chunks[2]["quantity"] != 7:
        return fail("commodity_chunks_same_price", "同单价应按数量升序")
    ok("commodity_chunks_same_price")


def test_commodity_chunks_filter():
    """非法数量（0/负数/nil）被过滤；空命中返回空计划。"""
    lua, bq, _ = load_buy_queue()
    ls = matched(lua, [(136000, 0), (136000, -1), (136000, 2)])
    chunks = bq.BuildCommodityChunks(ls)
    if len(chunks) != 1 or chunks[1]["quantity"] != 2:
        return fail("commodity_chunks_filter", "非法数量应被过滤，仅剩 quantity=2")
    if len(bq.BuildCommodityChunks(matched(lua, []))) != 0:
        return fail("commodity_chunks_filter", "空命中应返回空计划")
    ok("commodity_chunks_filter")


def test_config_migration():
    """旧绑定/提醒字段被清除，interval 旧值迁移为 0。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    g.AHSnipe = lua.table_from({})
    g.AHSnipeDB = lua.table_from({
        "interval": 8, "discount": 0.65,
        "items": lua.table_from({}), "logs": lua.table_from({}),
        "lastPrices": lua.table_from({}), "paused": False, "diagAH": False,
        "bindKey": "F", "flashAlert": True, "repeatPing": True,
    })
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    g.AHSnipe.Config.Init()
    db = g.AHSnipeDB
    for k in ("bindKey", "flashAlert", "repeatPing"):
        if db[k] is not None:
            return fail("config_migration", ("旧字段 %s 应被清除") % k)
    if db["lastPrices"] is not None:
        return fail("config_migration", "lastPrices 快照应随价格列一并清除")
    cfg = g.AHSnipe.Config
    if cfg.SetLastPrice is not None or cfg.GetLastPrice is not None:
        return fail("config_migration", "价格快照访问器应随价格列删除")
    if db["interval"] != 0:
        return fail("config_migration", "旧 interval=8 应迁移为 0")
    ok("config_migration")


def load_snipe_list():
    """独立 mock 环境加载 SnipeList.lua（复用 MOCK_ENV），返回模块与记录器。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    logs = []
    pings = []
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda m: logs.append(str(m)),
            "GetItemDisplayName": lambda i: "物品%s" % i,
            "GetListPos": lambda: None,
            "SetListPos": lambda *a: None,
        }),
    })
    g.PlaySound = lambda *a: pings.append(1)
    with open(SNIPE_LIST_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.SnipeList, logs, pings


def make_hit(lua, item_id, unit, qty, total, seen_at, commodity=True):
    return lua.table_from({
        "itemID": item_id, "name": "物品%s" % item_id, "threshold": unit * 3,
        "isCommodity": commodity, "totalQuantity": qty, "totalPrice": total,
        "isBaitLike": False,
        "matched": lua.table_from([
            lua.table_from({"unitPrice": unit, "quantity": qty})]),
        "roundStartAt": seen_at - 3, "querySendAt": seen_at - 1, "seenAt": seen_at,
    })


def test_snipe_list():
    """加行/原地刷新/新鲜度排序/删行。"""
    lua, lst, logs, pings = load_snipe_list()
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:add", "首次命中应加一行并响一次提示音")
    lst.Add(make_hit(lua, 101, 51000, 9, 459000, 98))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:refresh", "同物品应原地刷新且不重响")
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    if lst.RowCount() != 2:
        return fail("snipe_list:sort", "两个物品应有两行")
    if int(lst.GetFront()["itemID"]) != 102:
        return fail("snipe_list:sort", "队首应为 seenAt 最新的 102")
    lst.MarkGone(102, "测试")
    if lst.RowCount() != 1 or int(lst.GetFront()["itemID"]) != 101:
        return fail("snipe_list:gone", "MarkGone 应删除指定行")
    ok("snipe_list")


def test_snipe_list_buy():
    """【立即购买】= 买最新鲜一条（队首）；忙时拒绝；整单成功删行。"""
    lua, lst, logs, pings = load_snipe_list()
    g = lua.globals()
    buys = []
    busy = {"v": False}
    g.AHSnipe["BuyQueue"] = lua.table_from({
        "IsBusy": lambda: busy["v"],
        "Buy": lambda hit: (buys.append(int(hit["itemID"])) or True),
    })
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    lst.TriggerBuy()
    if buys != [102]:
        return fail("snipe_list_buy", f"应买队首 102，实际 {buys}")
    busy["v"] = True
    if lst.TriggerBuy() or buys != [102]:
        return fail("snipe_list_buy", "购买在途应拒绝再次点击")
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 102, "bought": 1, "failed": 0, "deferred": False}))
    if lst.RowCount() != 1:
        return fail("snipe_list_buy", "整单成功后应删除该行")
    busy["v"] = False
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 101, "bought": 0, "failed": 1, "deferred": False}))
    if lst.RowCount() != 1:
        return fail("snipe_list_buy", "购买失败不应删行（等扫描刷新/证实失效）")
    ok("snipe_list_buy")


def test_buqueue_notifies_list():
    """commodity 单块购买结束后回调 SnipeList.OnBuyFinished，携带 itemID/bought/failed。"""
    lua, bq, finished = load_buy_queue()
    hit = lua.table_from({
        "itemID": 237361, "threshold": 156000, "isCommodity": True,
        "matched": lua.table_from([
            lua.table_from({"unitPrice": 148900, "quantity": 1})]),
    })
    if not bq.Buy(hit):
        return fail("bq_notify", "Buy 应受理单块命中")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    on(f, "COMMODITY_PRICE_UPDATED", 148900, 148900)
    on(f, "COMMODITY_PURCHASE_SUCCEEDED")
    if len(finished) != 1:
        return fail("bq_notify", f"应恰好回调一次，实际 {len(finished)} 次")
    s = finished[0]
    if int(s["itemID"]) != 237361 or int(s["bought"]) != 1 or int(s["failed"]) != 0:
        return fail("bq_notify", "summary 字段不符")
    ok("bq_notify")


PLUGIN_ORDER = [
    "Core.lua", "Config.lua", "PriceCluster.lua", "Scanner.lua",
    "BuyQueue.lua", "SnipeList.lua", "ConfigPanel.lua", "LogWindow.lua",
]

# 模拟 WoW 客户端环境：语法检查抓不到运行期错误（nil 全局、帧方法签名、初始化链断裂等），
# 这里把所有 Lua 文件按 TOC 顺序真实执行一遍，并驱动 加载→开拍卖行→购买→事件→斜杠命令 全链路。
MOCK_ENV = r"""
local function Mock(name)
    local obj = { __name = name or "obj", __shown = false, __scripts = {}, __h = 0, __w = 0, __text = "" }
    local mt = { __index = function(t, k)
        -- 模板提供的属性（未赋值时读不到）：返回 nil，让插件走自绘回退分支
        if k == "TitleText" then return nil end
        if k == "Show" then return function(self) t.__shown = true end end
        if k == "Hide" then return function(self) t.__shown = false end end
        if k == "IsShown" then return function(self) return t.__shown end end
        if k == "SetScript" then return function(self, n, fn) t.__scripts[n] = fn end end
        if k == "GetScript" then return function(self, n) return t.__scripts[n] end end
        if k == "CreateFontString" then return function(self, ...) return Mock("fontstring") end end
        if k == "GetChildren" then return function(self) return {} end end
        if k == "SetHeight" then return function(self, h) t.__h = h end end
        if k == "GetHeight" then return function(self) return t.__h end end
        if k == "SetWidth" then return function(self, w) t.__w = w end end
        if k == "GetWidth" then return function(self) return t.__w end end
        if k == "GetText" then return function(self) return t.__text end end
        if k == "SetText" then return function(self, s) t.__text = s end end
        if k == "GetChecked" then return function(self) return false end end
        -- 数值型 getter：UI 代码会拿它们做算术，返回 Mock 会让 pcall 里的建窗静默失败
        if k == "GetFrameLevel" then return function(self) return 10 end end
        if k == "GetEffectiveScale" then return function(self) return 1 end end
        local f = function(self, ...) return Mock("ret") end
        t[k] = f
        return f
    end }
    return setmetatable(obj, mt)
end

UIParent = Mock("UIParent")
NAMED = {}
function CreateFrame(kind, name, parent, template)
    local f = Mock(name or kind)
    if name then NAMED[name] = f end
    return f
end
SetBindingClick = function(key, name, button) return true end
SetBinding = function(key) return true end
C_Timer = {
    NewTimer = function(s, cb) return { Cancel = function() end } end,
    After = function(s, cb) end,
    NewTicker = function(s, cb) return { Cancel = function() end } end,
}
C_AuctionHouse = {
    MakeItemKey = function(id) return { itemID = id } end,
    GetItemKeyInfo = function(key) return { isCommodity = true } end,
    IsThrottledMessageSystemReady = function() return true end,
    HasSearchResults = function() return false end,
    HasFullCommoditySearchResults = function() return false end,
    RefreshCommoditySearchResults = function() end,
    SendSearchQuery = function() end,
    StartCommoditiesPurchase = function() end,
    ConfirmCommoditiesPurchase = function() end,
    CancelCommoditiesPurchase = function() end,
    PlaceBid = function() end,
    GetNumCommoditySearchResults = function() return 0 end,
    GetCommoditySearchResultInfo = function() return nil end,
    GetNumItemSearchResults = function() return 0 end,
    GetItemSearchResultInfo = function() return nil end,
}
C_CurrencyInfo = { GetCoinTextureString = function(c) return tostring(c) end }
C_Item = { RequestLoadItemDataByID = function() end }
GetItemInfo = function() return nil end
GetTime = function() return 100 end
date = function() return "10-15 12:00:00" end
time = function() return 1000000 end
WOW_PROJECT_MAINLINE = 1
WOW_PROJECT_ID = 1
PlaySound = function() end
SOUNDKIT = {}
hooksecurefunc = function() end
SlashCmdList = {}
GameFontNormal = "GameFontNormal"
GameFontHighlight = "GameFontHighlight"
GameFontHighlightSmall = "GameFontHighlightSmall"
GameFontNormalLarge = "GameFontNormalLarge"
string.trim = function(s)   -- WoW 在 string 库上提供的 trim（斜杠命令用到）
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end
"""


def smoke_test():
    """冒烟测试：mock WoW 环境真实执行全部插件文件，驱动核心链路，捕获运行期错误。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)

    addon_table = lua.table_from({})
    g.AHSnipe = addon_table

    for fn in PLUGIN_ORDER:
        path = os.path.join(PLUGIN_DIR, fn)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        try:
            if fn == "Core.lua":
                # Core 用 `local ADDON_NAME, AHSnipe = ...` 取参数，包一层函数调用传入
                lua.execute(f'(function(...) {src} end)("AHSnipe", AHSnipe)')
            else:
                lua.execute(src)
        except Exception as e:
            return fail(f"smoke:load:{fn}", str(e))
    print(f"SMOKE-OK 加载 {len(PLUGIN_ORDER)} 个文件")

    addon = g.AHSnipe
    frame = addon.frame
    on_event = frame.GetScript(frame, "OnEvent")

    # 1) ADDON_LOADED（初始化链：任一环报错后面全断，插件界面/扫描全废）
    try:
        on_event(frame, "ADDON_LOADED", "AHSnipe")
    except Exception as e:
        return fail("smoke:ADDON_LOADED", str(e))
    print("SMOKE-OK ADDON_LOADED")

    # 2) 打开拍卖行：扫描启动 + 可购买列表常驻；配置面板不再自动弹出
    try:
        on_event(frame, "AUCTION_HOUSE_SHOW")
        ev = g.NAMED["AHSnipeListEvents"]
        ev_on = ev.GetScript(ev, "OnEvent")
        ev_on(ev, "AUCTION_HOUSE_SHOW")
        if not g.NAMED["AHSnipeListFrame"]["__shown"]:
            return fail("smoke:listframe", "打开拍卖行后可购买列表未常驻")
    except Exception as e:
        return fail("smoke:AUCTION_HOUSE_SHOW", str(e))
    panel = addon.ConfigPanel.GetFrame()   # 仍创建：建窗本身要跑得通，只是不该被 Show
    if panel.IsShown(panel):
        return fail("smoke:panel", "配置面板不应随拍卖行自动弹出（列表才是主界面）")
    print("SMOKE-OK 打开拍卖行：列表常驻、配置面板不自动弹出")

    # 3) 一次 commodity 购买全流程（分块 + 两段式 + 事件驱动）
    try:
        hit = lua.table_from({
            "itemID": 237361, "name": "测试矿石", "threshold": 156000,
            "isCommodity": True, "totalQuantity": 1, "totalPrice": 148900,
            "matched": lua.table_from([lua.table_from({"unitPrice": 148900, "quantity": 1})]),
        })
        started = addon.BuyQueue.Buy(hit)
        if not started:
            return fail("smoke:buy", "BuyQueue.Buy 返回 false")
        bq_frame = addon.BuyQueue.frame
        bq_on = bq_frame.GetScript(bq_frame, "OnEvent")
        bq_on(bq_frame, "COMMODITY_PRICE_UPDATED", 148900, 148900)   # 报价
        bq_on(bq_frame, "COMMODITY_PURCHASE_SUCCEEDED")              # 成交
    except Exception as e:
        return fail("smoke:buy", str(e))
    print("SMOKE-OK 购买链路（报价→确认→成交）")

    # 4) 斜杠命令分支
    try:
        slash = g.SlashCmdList["AHSNIPE"]
        slash("")
        slash("scan")
        slash("log")
        slash("diag")
    except Exception as e:
        return fail("smoke:slash", str(e))
    print("SMOKE-OK 斜杠命令")

    # 5) 可购买列表：命中加行 → 点【立即购买】买入最新鲜一条（队首）→ 成功回调删行
    try:
        def fake_buy(hit):
            buys.append(int(hit["itemID"]))
            return True
        buys = []
        addon.BuyQueue.Buy = fake_buy
        h_old = lua.table_from({
            "itemID": 237362, "name": "旧", "threshold": 156000, "isCommodity": True,
            "totalQuantity": 3, "totalPrice": 300000,
            "matched": lua.table_from([lua.table_from({"unitPrice": 100000, "quantity": 3})]),
            "roundStartAt": 97, "querySendAt": 99, "seenAt": 99,
        })
        h_new = lua.table_from({
            "itemID": 237363, "name": "新", "threshold": 156000, "isCommodity": True,
            "totalQuantity": 1, "totalPrice": 100000,
            "matched": lua.table_from([lua.table_from({"unitPrice": 100000, "quantity": 1})]),
            "roundStartAt": 97, "querySendAt": 99.5, "seenAt": 99.5,
        })
        addon.SnipeList.Add(h_old)
        addon.SnipeList.Add(h_new)
        if addon.SnipeList.RowCount() != 2:
            return fail("smoke:list", "两条命中应有两行")
        if int(addon.SnipeList.GetFront()["itemID"]) != 237363:
            return fail("smoke:list", "队首应为 seenAt 更新的 237363")
        if not addon.SnipeList.TriggerBuy():
            return fail("smoke:list", "TriggerBuy 未受理")
        if buys != [237363]:
            return fail("smoke:list", f"应买队首，实际 {buys}")
        addon.SnipeList.OnBuyFinished(lua.table_from(
            {"itemID": 237363, "bought": 1, "failed": 0, "deferred": False}))
        if addon.SnipeList.RowCount() != 1:
            return fail("smoke:list", "整单成功应删行")
    except Exception as e:
        return fail("smoke:list", str(e))
    print("SMOKE-OK 可购买列表（加行/排序/立即购买/成功删行）")

    ok("smoke")
    return lua


def syntax_check():
    """对插件目录下所有 .lua 做 loadfile 语法检查。"""
    if not os.path.isdir(PLUGIN_DIR):
        return fail("syntax", f"插件目录不存在: {PLUGIN_DIR}")
    lua = LuaRuntime()
    checked = 0
    for fn in sorted(os.listdir(PLUGIN_DIR)):
        if not fn.endswith(".lua"):
            continue
        path = os.path.join(PLUGIN_DIR, fn)
        with open(path, encoding="utf-8") as f:
            source = f.read()
        try:
            lua.execute(f"local f, err = load([==[ {source} ]==], '@{fn}') if not f then error(err) end")
        except Exception as e:
            fail(f"syntax:{fn}", str(e))
            continue
        checked += 1
        print(f"SYNTAX-OK {fn}")
    if checked == 0:
        fail("syntax", "未找到任何 .lua 文件")


if __name__ == "__main__":
    tests = [
        test_screenshot_scene,
        test_bait_cluster_together,
        test_single_listing,
        test_empty_listings,
        test_tie_takes_higher_cluster,
        test_uniform_price,
        test_is_bait_like,
        test_commodity_chunks_sorted,
        test_commodity_chunks_same_price,
        test_commodity_chunks_filter,
        test_config_migration,
        test_snipe_list,
        test_snipe_list_buy,
        test_buqueue_notifies_list,
    ]
    for t in tests:
        t()
    smoke_test()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
