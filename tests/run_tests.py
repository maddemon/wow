# AHSnipe 测试运行器：PriceCluster / BuyQueue 单测 + Lua 语法检查
# 用法：python tests/run_tests.py            —— 运行单测
#       python tests/run_tests.py --syntax    —— 额外对全部 Lua 文件做 loadfile 语法检查
import os
import re
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "AHSnipe")
PRICE_CLUSTER_PATH = os.path.join(PLUGIN_DIR, "PriceCluster.lua")
BUY_QUEUE_PATH = os.path.join(PLUGIN_DIR, "BuyQueue.lua")
SCANNER_PATH = os.path.join(PLUGIN_DIR, "Scanner.lua")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "Config.lua")
SNIPE_LIST_PATH = os.path.join(PLUGIN_DIR, "SnipeList.lua")
CONFIG_PANEL_PATH = os.path.join(PLUGIN_DIR, "ConfigPanel.lua")

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


def test_display_name_cache():
    """显示名缓存：AddItem/RemoveItem 改变同名组成员后缓存应失效，星级推断跟着更新。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    g.AHSnipe = lua.table_from({})
    lua.execute(
        "AHSnipeDB = { interval = 0, discount = 0.65, diagAH = false,"
        " paused = false, items = {}, logs = {} }"
        " AHSnipeDB.items[201] = { name = '法力百合', quality = 2, enabled = true }"
    )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    cfg = g.AHSnipe.Config
    # 同名组只有 1 个成员：GetEffectiveReagentQuality 返回 nil，无星级标识
    name1 = str(cfg.GetItemDisplayName(201))
    if "♦" in name1:
        return fail("display_name_cache", f"同名组单成员时不应有星级：{name1}")
    # 添加同名第二成员 → AddItem 令缓存整体失效；201 按 ID 升序推断为 tier 1，有星级
    cfg.AddItem(202, "法力百合")
    name2 = str(cfg.GetItemDisplayName(201))
    if "♦" not in name2:
        return fail("display_name_cache",
                    f"AddItem 后缓存应失效，201 应有星级推断，实际：{name2}")
    # 移除第二成员 → RemoveItem 令缓存再次失效；201 回到单成员，无星级
    cfg.RemoveItem(202)
    name3 = str(cfg.GetItemDisplayName(201))
    if "♦" in name3:
        return fail("display_name_cache",
                    f"RemoveItem 后缓存应失效，201 不应有星级，实际：{name3}")
    ok("display_name_cache")


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
    # 监控态开关：List.Add 会拒收"已移除/停用"的物品（测试用它模拟查询在途期间被停用）
    lua.execute("""
        monitored = true
        local cfg = AHSnipe.Config
        cfg.GetItem = function(id) if monitored then return {itemID=id, enabled=true} end end
    """)
    with open(SNIPE_LIST_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.SnipeList, logs, pings


def make_hit(lua, item_id, unit, qty, total, seen_at, commodity=True, threshold=None,
             market=None):
    hit = {
        "itemID": item_id, "name": "物品%s" % item_id,
        "threshold": unit * 3 if threshold is None else threshold,
        "isCommodity": commodity, "totalQuantity": qty, "totalPrice": total,
        "isBaitLike": False,
        "matched": lua.table_from([
            lua.table_from({"unitPrice": unit, "quantity": qty})]),
        "roundStartAt": seen_at - 3, "querySendAt": seen_at - 1, "seenAt": seen_at,
    }
    if market is not None:
        hit["marketUnitPrice"] = market
    return lua.table_from(hit)


def make_hit_rows(lua, item_id, pairs, seen_at, round_start=None, commodity=True, threshold=None,
                  market=None, spill=None):
    """构造含多条 matched 挂单的命中。pairs = [(unitPrice, quantity[, auctionID]), ...]"""
    entries = []
    for tup in pairs:
        unit, qty = tup[0], tup[1]
        row = {"unitPrice": unit, "quantity": qty}
        if len(tup) > 2 and tup[2] is not None:
            row["auctionID"] = tup[2]
            row["buyoutAmount"] = unit * qty
        entries.append(lua.table_from(row))
    units = [tup[0] for tup in pairs]
    hit = {
        "itemID": item_id, "name": "物品%s" % item_id,
        "threshold": min(units) * 3 if threshold is None else threshold,
        "isCommodity": commodity, "totalQuantity": sum(tup[1] for tup in pairs),
        "totalPrice": sum(tup[0] * tup[1] for tup in pairs),
        "matched": lua.table_from(entries),
        "roundStartAt": seen_at - 3 if round_start is None else round_start,
        "querySendAt": seen_at - 1, "seenAt": seen_at,
    }
    if market is not None:
        hit["marketUnitPrice"] = market
    if spill is not None:
        hit["spillPrice"] = spill
    return lua.table_from(hit)


def test_listing_rows_from_hit():
    """一次命中摊成档位行：同价位合计、不同价位分行；一轮多行只响一次。"""
    lua, lst, logs, pings = load_snipe_list()
    hit = make_hit_rows(lua, 101, [(148900, 2), (148900, 3), (156000, 4)], 97)
    lst.Add(hit)
    if lst.RowCount() != 2:
        return fail("listing_rows_from_hit", f"两个价位应有 2 行，实际 {lst.RowCount()}")
    if len(pings) != 1:
        return fail("listing_rows_from_hit", f"一轮新增多行只响一次，实际 {len(pings)}")
    front = lst.GetFront()                       # 同 seenAt 按单价升序 → 队首是最便宜档
    if int(front["unitPrice"]) != 148900 or int(front["quantity"]) != 5:
        return fail("listing_rows_from_hit", f"最便宜档应合计 5 个，实际 {front['unitPrice']}/{front['quantity']}")
    if int(front["totalPrice"]) != 148900 * 5:
        return fail("listing_rows_from_hit", f"总价按档位合计，实际 {front['totalPrice']}")
    if str(front["key"]) != "C:101:148900":
        return fail("listing_rows_from_hit", f"行 key 错误：{front['key']}")
    ok("listing_rows_from_hit")


def test_row_reconcile():
    """新鲜命中缺某档位 → 只删那一行；档位价格上移 → 旧行删、新行入列；MarkGone 删该物品全部行。"""
    lua, lst, logs, pings = load_snipe_list()
    lst.Add(make_hit_rows(lua, 101, [(148900, 2), (156000, 4)], 97))
    lst.Add(make_hit_rows(lua, 102, [(60000, 1)], 98))
    if lst.RowCount() != 3:
        return fail("row_reconcile", f"应有 3 行，实际 {lst.RowCount()}")
    # 101 的 148900 档被抢光：只删那一行，156000 行原地刷新（同轮不重响）
    lst.Add(make_hit_rows(lua, 101, [(156000, 4)], 99))
    if lst.RowCount() != 2:
        return fail("row_reconcile", f"档位消失应只删一行，实际 {lst.RowCount()}")
    if len(pings) != 2:
        return fail("row_reconcile", f"无新档位入列不应重响，实际 {len(pings)}")
    # 单价上移（156000→150000）：旧 key 消失即删、新 key 入列并响 → 行数回到 2
    lst.Add(make_hit_rows(lua, 101, [(150000, 3)], 100))
    if lst.RowCount() != 2 or len(pings) != 3:
        return fail("row_reconcile", f"新档位应入列且响一次，行数={lst.RowCount()} 响={len(pings)}")
    if str(lst.GetFront()["key"]) != "C:101:150000":
        return fail("row_reconcile", "队首应是 seenAt 最新的 150000 档")
    lst.MarkGone(101, "测试")
    if lst.RowCount() != 1 or int(lst.GetFront()["itemID"]) != 102:
        return fail("row_reconcile", "MarkGone 应删除该物品全部行")
    ok("row_reconcile")


def row_ids(lst):
    """lupa 迭代 Lua 数组拿到的是下标，所以显式按 1..n 取行。"""
    rows = lst.GetRows()
    return [int(rows[i]["itemID"]) for i in range(1, lst.RowCount() + 1)]


def test_row_cap():
    """超过 MAX_ROWS(60) 时淘汰数据最旧的一行（与它排在第几位无关）。"""
    lua, lst, logs, pings = load_snipe_list()
    for i in range(61):
        lst.Add(make_hit(lua, 300 + i, 50000, 1, 50000, 100 + i))
    if lst.RowCount() != 60:
        return fail("row_cap", f"应保留 60 行，实际 {lst.RowCount()}")
    if int(lst.GetFront()["itemID"]) != 360:
        return fail("row_cap", "队首应是最新鲜的 360")
    if 300 in row_ids(lst):
        return fail("row_cap", "300 是最旧的一行，应被淘汰")
    # 利润最大的行如果同时是最旧的，也必须先被淘汰（利润排序不该保护过期数据）
    lua2, lst2, _, _ = load_snipe_list()
    lst2.Add(make_hit(lua2, 900, 10000, 100, 1000000, 10, threshold=200000))
    for i in range(61):
        lst2.Add(make_hit(lua2, 300 + i, 50000, 1, 50000, 100 + i))
    if lst2.RowCount() != 60:
        return fail("row_cap", f"应保留 60 行，实际 {lst2.RowCount()}")
    if 900 in row_ids(lst2):
        return fail("row_cap", "利润最大但最旧的行应最先淘汰")
    if int(lst2.GetFront()["itemID"]) != 360:
        return fail("row_cap", "淘汰最旧后队首仍应是最新鲜的 360")
    ok("row_cap")


def test_profit_ranking():
    """两条都新鲜时按整单利润排（门槛差×数量），利润大的抢过最新鲜的。"""
    lua, lst, logs, pings = load_snipe_list()
    # 101：利润 (150000-50000)×10 = 1,000,000，数据较旧（seenAt 97）
    # 102：利润 (180000-60000)×4 = 480,000，数据较新（seenAt 99）
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    if int(lst.GetFront()["itemID"]) != 101:
        return fail("profit_ranking", "都新鲜时队首应是利润大的 101，不是最新鲜的 102")
    # 利润打平 → 回到新鲜度：新的赢
    lua2, lst2, _, _ = load_snipe_list()
    lst2.Add(make_hit(lua2, 201, 50000, 10, 500000, 97))
    lst2.Add(make_hit(lua2, 202, 50000, 10, 500000, 99))
    if int(lst2.GetFront()["itemID"]) != 202:
        return fail("profit_ranking", "利润相同应按 seenAt 取最新鲜的 202")
    # 缺 threshold 的行按 0 计，不会被误抬到队首
    lua3, lst3, _, _ = load_snipe_list()
    lst3.Add(make_hit(lua3, 301, 50000, 10, 500000, 99))
    no_thr = make_hit(lua3, 302, 50000, 90, 4500000, 98)
    no_thr["threshold"] = None
    lst3.Add(no_thr)
    if int(lst3.GetFront()["itemID"]) != 301:
        return fail("profit_ranking", "无门槛的行利润按 0 计，不得占队首")
    ok("profit_ranking")


def test_profit_fresh_gate():
    """新鲜度是闸门：过期大单必须让位给新鲜小单；全过期时退回按 seenAt 降序。"""
    lua, lst, logs, pings = load_snipe_list()      # GetTime() 恒为 100
    # 101 利润最大但已过期（seenAt 60 → 40s > 闸门 20s）
    lst.Add(make_hit(lua, 101, 10000, 100, 1000000, 60, threshold=200000))
    lst.Add(make_hit(lua, 102, 60000, 1, 60000, 99, threshold=70000))
    if int(lst.GetFront()["itemID"]) != 102:
        return fail("profit_fresh_gate", "过期大单不得占队首，应是新鲜小单 102")
    # 全部过期 → 利润不再参与比较，按新鲜度降序
    lua2, lst2, _, _ = load_snipe_list()
    lst2.Add(make_hit(lua2, 201, 10000, 100, 1000000, 50, threshold=200000))
    lst2.Add(make_hit(lua2, 202, 60000, 1, 60000, 60, threshold=70000))
    if int(lst2.GetFront()["itemID"]) != 202:
        return fail("profit_fresh_gate", "都过期时队首应是较新的 202")
    ok("profit_fresh_gate")


def test_profit_merged_key():
    """排序键 = 一次点击的收获：同物品全部档位利润合计（点击会合并成一枪）。"""
    lua, lst, logs, pings = load_snipe_list()
    # 101 两档各 100000/50000，合计 150000；102 单档 100000
    lst.Add(make_hit_rows(lua, 101, [(100000, 1), (150000, 1)], 97, threshold=200000))
    lst.Add(make_hit_rows(lua, 102, [(100000, 1)], 98, threshold=200000))
    front = lst.GetFront()
    if int(front["itemID"]) != 101:
        return fail("profit_merged_key", f"合并利润大的 101 应抢队首，实际 {front['itemID']}")
    if str(front["key"]) != "C:101:100000":
        return fail("profit_merged_key", f"同件多档并列时队首应是最便宜档，实际 {front['key']}")
    # 非 commodity：一行一枪 → 用本行利润，不得把同件其他行算进来
    lua2, lst2, _, _ = load_snipe_list()
    lst2.Add(make_hit_rows(lua2, 301, [(100000, 1, 11), (150000, 1, 12)], 97,
                           threshold=200000, commodity=False))
    lst2.Add(make_hit_rows(lua2, 302, [(100000, 2)], 98, threshold=200000))
    if int(lst2.GetFront()["itemID"]) != 302:
        return fail("profit_merged_key", "非 commodity 只按本行利润比较，302（200000）应赢 301（100000）")
    ok("profit_merged_key")


def test_profit_market_spread():
    """利润 = 主簇最低价（可变现价）− 单价，门槛差只在缺主簇价时兜底。

    2026-10-17 日志实锤门槛口径失真：幽影锡主簇 25.21g、门槛 15.13g、买到 13.89g，
    按钮显示"利润 2.47g"，真实利差是 11.32g/只——门槛≈0.6×市场价时低估 4 倍以上，
    队首会系统性偏袒"离门槛近"而不是"真赚钱多"的单子。
    """
    lua, lst, logs, pings = load_snipe_list()
    # 门槛口径：101 (60000-50000)×1=10000 < 102 (100000-60000)×10=400000
    # 主簇口径：101 (200000-50000)×1=150000 > 102 (70000-60000)×10=100000
    lst.Add(make_hit(lua, 101, 50000, 1, 50000, 97, threshold=60000, market=200000))
    lst.Add(make_hit(lua, 102, 60000, 10, 600000, 99, threshold=100000, market=70000))
    front = lst.GetFront()
    if int(front["itemID"]) != 101:
        return fail("profit_market_spread", f"应按主簇利差选 101，实际 {front['itemID']}")
    profit = lst.RequestProfit(lst.GetRows(), front)
    if int(profit) != 150000:
        return fail("profit_market_spread", f"整单主簇利差应为 150000，实际 {profit}")
    # 利差非正（主簇价不高于单价）按 0 计，不因门槛差巨大而占队首
    lua2, lst2, _, _ = load_snipe_list()
    lst2.Add(make_hit(lua2, 301, 100000, 1, 100000, 99, threshold=150000, market=100000))
    lst2.Add(make_hit(lua2, 302, 120000, 1, 120000, 97, threshold=130000, market=125000))
    if int(lst2.GetFront()["itemID"]) != 302:
        return fail("profit_market_spread", "主簇利差为 0 的行不得占队首，应选 302")
    # 缺 marketUnitPrice → 退回门槛口径（旧行为），既不清零也不报错
    lua3, lst3, _, _ = load_snipe_list()
    lst3.Add(make_hit(lua3, 201, 50000, 10, 500000, 97, threshold=150000))
    lst3.Add(make_hit(lua3, 202, 60000, 1, 60000, 99, threshold=200000, market=65000))
    if int(lst3.GetFront()["itemID"]) != 201:
        return fail("profit_market_spread", "无主簇价的行应按门槛口径比较，201 应赢")
    ok("profit_market_spread")


def test_scanner_market_anchor():
    """命中要带主簇最低价：行上的利润口径靠它，缺了就退回门槛。"""
    for commodity in (True, False):
        lua, sc, lst = load_scanner_flow(commodity)
        g = lua.globals()
        sc.Start()
        g.DeliverResults()
        row = lst.GetFront()                       # 快照：诱饵 10000×2，主簇 100000×100
        market = row["marketUnitPrice"]
        if market is None or int(market) != 100000:
            return fail("scanner_market_anchor",
                        f"commodity={commodity} 行应带主簇最低价 100000，实际 {market}")
        profit = lst.RowProfit(row)
        if int(profit) != (100000 - 10000) * 2:
            return fail("scanner_market_anchor", f"利润应按主簇利差算，实际 {profit}")
    ok("scanner_market_anchor")


def test_scanner_multi_tier():
    """两档诱饵价：合并循环一次算出 totalQuantity/totalPrice，两档各成一行、数量独立。"""
    lua, sc, lst = load_scanner_flow(True)
    g = lua.globals()
    # 门槛 = 100000 * 0.65 = 65000；8000 和 12000 均低于门槛，均入列
    lua.execute(
        "snapshot = {"
        " {unitPrice=8000, quantity=1},"
        " {unitPrice=12000, quantity=3},"
        " {unitPrice=100000, quantity=100},"
        "}"
    )
    sc.Start()
    g.DeliverResults()
    if lst.RowCount() != 2:
        return fail("scanner_multi_tier",
                    f"两档诱饵应有 2 行，实际 {lst.RowCount()}")
    rows = lst.GetRows()
    qty_by_price = {}
    for i in range(1, lst.RowCount() + 1):
        r = rows[i]
        qty_by_price[int(r["unitPrice"])] = int(r["quantity"])
    if qty_by_price.get(8000) != 1 or qty_by_price.get(12000) != 3:
        return fail("scanner_multi_tier", f"档位数量应独立，实际 {qty_by_price}")
    ok("scanner_multi_tier")


def test_snipe_list_front_after_add():
    """Add 结束后队首由 Render 内 OrderRows 维护，不在 Add 里再排一次也能正确排序。"""
    lua, lst, logs, pings = load_snipe_list()
    # 低利润行先入列
    lst.Add(make_hit(lua, 101, 60000, 1, 60000, 97, threshold=70000, market=70000))
    # 高利润行后入列，应抢过队首
    lst.Add(make_hit(lua, 102, 50000, 10, 500000, 98,
                     threshold=200000, market=200000))
    front = lst.GetFront()
    if int(front["itemID"]) != 102:
        return fail("snipe_list_front_after_add",
                    f"高利润行应排队首，实际 itemID={front['itemID']}")
    ok("snipe_list_front_after_add")


def test_snipe_list_scroll():
    """窗口高度固定不随行数增长；内容高度随行数增长（滚动区可见约 6 行）。"""
    lua, lst, logs, pings = load_snipe_list()
    for i in range(12):
        lst.Add(make_hit(lua, 400 + i, 50000, 1, 50000, 100 + i))
    if lst.RowCount() != 12:
        return fail("snipe_list_scroll", f"应 12 行，实际 {lst.RowCount()}")
    named = lua.globals().NAMED
    frame = named["AHSnipeListFrame"]
    if int(frame["__h"]) != 260:
        return fail("snipe_list_scroll", f"窗口高度应固定 260，实际 {frame['__h']}")
    content = named["AHSnipeListContent"]
    if int(content["__h"]) < 12 * 19:
        return fail("snipe_list_scroll", f"内容高度应 >= 行数*行高，实际 {content['__h']}")
    # 首行内缩半行高：ScrollFrame 裁剪视口，行中心压在视口上沿会被切掉半截（2026-10-16 用户截图）
    pts = frame["rowFS"][1]["name"]["__pts"]
    if int(pts[1][4]) != 30 or abs(float(pts[1][5]) + 9) > 0.01:
        return fail("snipe_list_scroll",
                    f"首行应内缩半行高，实际锚点 x={pts[1][4]} y={pts[1][5]}")
    if int(pts[2][4]) != 186:
        return fail("snipe_list_scroll", f"名称应有右界防止压到单价列，实际 {pts[2][4]}")
    ok("snipe_list_scroll")


def test_snipe_list():
    """加行/原地刷新/新鲜度排序/删行。"""
    lua, lst, logs, pings = load_snipe_list()
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:add", "首次命中应加一行并响一次提示音")
    lst.Add(make_hit(lua, 101, 50000, 9, 450000, 98))
    if lst.RowCount() != 1 or len(pings) != 1:
        return fail("snipe_list:refresh", "同物品应原地刷新且不重响")
    lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99))
    if lst.RowCount() != 2:
        return fail("snipe_list:sort", "两个物品应有两行")
    if int(lst.GetFront()["itemID"]) != 101:
        return fail("snipe_list:sort", "都新鲜时队首应是利润大的 101（1,000,000 > 480,000）")
    lst.MarkGone(102, "测试")
    if lst.RowCount() != 1 or int(lst.GetFront()["itemID"]) != 101:
        return fail("snipe_list:gone", "MarkGone 应删除指定行")
    ok("snipe_list")


def test_snipe_list_buy():
    """【立即购买】= 买队首（新鲜闸门内利润最大的一条）；忙时拒绝；整单成功删该件参与行。"""
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
    label = str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()"))
    if label != "立即购买":
        return fail("snipe_list_buy", f"按钮应只显示立即购买，实际 {label}")
    lst.TriggerBuy()
    if buys != [101]:
        return fail("snipe_list_buy", f"应买队首 101（利润最大），实际 {buys}")
    busy["v"] = True
    if lst.TriggerBuy() or buys != [101]:
        return fail("snipe_list_buy", "购买在途应拒绝再次点击")
    lst.UpdateBuyButton()      # 游戏里在途文案由点击后那次 Render 刷出来，这里显式走同一入口
    if str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()")) != "购买中…":
        return fail("snipe_list_buy", "在途时按钮文案应是购买中")
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 101, "bought": 1, "failed": 0, "rowKeys": lua.table_from(["C:101:50000"]),
         "reason": "购买成交"}))
    if lst.RowCount() != 1 or str(lst.GetFront()["key"]) != "C:102:60000":
        return fail("snipe_list_buy", "成交应只删该件参与行")
    busy["v"] = False
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 102, "bought": 0, "failed": 1, "rowKeys": lua.table_from(["C:102:60000"]),
         "reason": "购买超时"}))
    if lst.RowCount() != 0:
        return fail("snipe_list_buy", "失败结局同样删行，避免证伪过的行占住队首")
    if str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()")) != "立即购买":
        return fail("snipe_list_buy", "空列表按钮应回到立即购买文案")
    ok("snipe_list_buy")


def test_buy_button_before_list_render():
    lua, lst, bq, logs, g = load_buy_into_list()
    lst.Init()
    event_frame = lst.frame
    event_frame.GetScript(event_frame, "OnEvent")(event_frame, "AUCTION_HOUSE_SHOW")
    frame = g.NAMED["AHSnipeListFrame"]
    button = frame["buyBtn"]
    if button["__enabled"] or button["__text"] != "立即购买":
        return fail("buy_button_priority", "打开空列表时按钮应不可用")
    observed = []

    def record_button(*args):
        observed.append((bool(button["__enabled"]), str(button["__text"])))

    g.C_Item.GetItemIconByID = record_button
    empty = frame["emptyFS"]
    show_empty = empty.Show

    def show_empty_and_record(self):
        record_button()
        show_empty(self)

    empty.Show = show_empty_and_record
    on = bq.frame.GetScript(bq.frame, "OnEvent")

    def succeed():
        on(bq.frame, "COMMODITY_PRICE_UPDATED", 50000, 500000)
        on(bq.frame, "COMMODITY_PURCHASE_SUCCEEDED")

    steps = [
        ("首次入列", lambda: lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97)), True, "立即购买"),
        ("另一件入列", lambda: lst.Add(make_hit(lua, 102, 60000, 4, 240000, 99)), True, "立即购买"),
        ("开始购买", lst.TriggerBuy, False, "购买中…"),
        ("成交后还有下一件", succeed, True, "立即购买"),
        ("购买下一件", lst.TriggerBuy, False, "购买中…"),
        ("失败后列表清空", lambda: on(bq.frame, "COMMODITY_PRICE_UNAVAILABLE"), False, "立即购买"),
        ("重新入列", lambda: lst.Add(make_hit(lua, 103, 50000, 1, 50000, 99)), True, "立即购买"),
        ("扫描删除最后一行", lambda: lst.MarkGone(103, "已无低价"), False, "立即购买"),
    ]
    for name, action, enabled, text in steps:
        observed.clear()
        action()
        if not observed or any(state != (enabled, text) for state in observed):
            return fail("buy_button_priority",
                        f"{name}：列表绘制前按钮应为 {(enabled, text)}，实际 {observed}")
    ok("buy_button_priority")


def test_buy_chat_only_reports_outcome():
    for success in (True, False):
        lua, bq, finished = load_buy_queue()
        messages = []
        lua.globals().print = lambda message: messages.append(str(message))
        hit = make_hit(lua, 101, 50000, 2, 100000, 99)
        if not bq.Buy(hit):
            return fail("buy_chat_outcome", "购买应正常受理")
        if messages:
            return fail("buy_chat_outcome", f"发起购买不应打印过程提示：{messages}")
        on = bq.frame.GetScript(bq.frame, "OnEvent")
        if success:
            on(bq.frame, "COMMODITY_PRICE_UPDATED", 50000, 100000)
            on(bq.frame, "COMMODITY_PURCHASE_SUCCEEDED")
        else:
            on(bq.frame, "COMMODITY_PRICE_UNAVAILABLE")
        expected = "购买成功" if success else "购买失败"
        if len(messages) != 1 or expected not in messages[0]:
            return fail("buy_chat_outcome", f"应仅保留{expected}提示，实际 {messages}")
    ok("buy_chat_outcome")


def test_buqueue_notifies_list():
    """commodity 单请求结束后回调 OnBuyFinished，携带 rowKeys 与结局原因。"""
    lua, bq, finished = load_buy_queue()
    hit = lua.table_from({
        "itemID": 237361, "threshold": 156000, "isCommodity": True,
        "rowKeys": lua.table_from(["C:237361:148900"]),
        "matched": lua.table_from([
            lua.table_from({"unitPrice": 148900, "quantity": 1})]),
    })
    if not bq.Buy(hit):
        return fail("bq_notify", "Buy 应受理单条命中")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    on(f, "COMMODITY_PRICE_UPDATED", 148900, 148900)
    on(f, "COMMODITY_PURCHASE_SUCCEEDED")
    if len(finished) != 1:
        return fail("bq_notify", f"应恰好回调一次，实际 {len(finished)} 次")
    s = finished[0]
    if int(s["itemID"]) != 237361 or int(s["bought"]) != 1:
        return fail("bq_notify", "summary 字段不符")
    if str(s["rowKeys"][1]) != "C:237361:148900":
        return fail("bq_notify", f"rowKeys 未透传：{s['rowKeys']}")
    if str(s["reason"]) != "购买成交":
        return fail("bq_notify", f"reason 未写入：{s['reason']}")
    ok("bq_notify")


def load_buy_into_list():
    """真实 BuyQueue + 真实 SnipeList：驱动一次购买，观察合并请求与结局删行的范围。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    logs = []
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda m: logs.append(str(m)),
            "GetItemDisplayName": lambda i: "物品%s" % i,
            "GetListPos": lambda: None,
            "SetListPos": lambda *a: None,
            "IsDiagAH": lambda: False,
            "SetDiagAH": lambda *a: None,
            "IsPaused": lambda: False,
            "Resume": lambda: None,
        }),
    })
    g.AHSnipe["Scanner"] = lua.table_from({
        "IsPaused": lambda: False,
        "ScanNow": lambda: True,
        "SetPaused": lambda p: False,
        "Resume": lambda: None,
    })
    # 盲窗起点做成可拨的旋钮：用例用它模拟"闸门堵塞了十几秒"（见 Scanner.BlindFrom）。
    # watched 记录购后复查：买了就买了，列表端必须当场把那件续聚焦踢轮去问有没有新单。
    lua.execute("""
        blindFrom = 0
        watched = {}
        AHSnipe.Scanner.BlindFrom = function() return blindFrom end
        AHSnipe.Scanner.IsEnabled = function() return true end
        AHSnipe.Scanner.Watch = function(id)
            watched[#watched + 1] = id
            return true
        end
    """)
    # List.Add 会校验监控状态，这里让所有物品都处于监控中（本组用例只关心购买结局）
    lua.execute("""
        local cfg = AHSnipe.Config
        cfg.GetItem = function(id) return {itemID=id, enabled=true} end
    """)
    for path in (BUY_QUEUE_PATH, SNIPE_LIST_PATH):
        with open(path, encoding="utf-8") as f:
            lua.execute(f.read())
    g.AHSnipe.BuyQueue.Init()
    return lua, g.AHSnipe.SnipeList, g.AHSnipe.BuyQueue, logs, g


def test_buy_outcome_deletes_row():
    """合并请求的结局范围：均价否决 = 整件快照证伪，参与行一起出门，其他物品的行不动。"""
    lua, lst, bq, logs, g = load_buy_into_list()
    lst.Add(make_hit_rows(lua, 101, [(148900, 2), (156000, 4)], 98))
    lst.Add(make_hit_rows(lua, 102, [(60000, 1)], 97))
    if lst.RowCount() != 3:
        return fail("buy_outcome", f"前置：应 3 行，实际 {lst.RowCount()}")
    if str(lst.GetFront()["key"]) != "C:101:148900":
        return fail("buy_outcome", "队首应是最新鲜物品的最便宜档")
    if not lst.TriggerBuy():
        return fail("buy_outcome", "TriggerBuy 未受理")
    if not bq.IsBusy():
        return fail("buy_outcome", "点击后应有一个请求在途")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    # 均价超门槛 = 低价池已被吃掉一部分，这份快照整体过期 → 该件参与合并的行一起出门
    on(f, "COMMODITY_PRICE_UPDATED", 900000, 5400000)
    if lst.RowCount() != 1:
        return fail("buy_outcome", f"否决应删该件全部参与行，实际剩 {lst.RowCount()} 行")
    if int(lst.GetFront()["itemID"]) != 102:
        return fail("buy_outcome", "其他物品的行不得被连带删掉")
    if bq.IsBusy():
        return fail("buy_outcome", "否决后应已结束购买会话，不得占住通道")
    ok("buy_outcome")


def test_merge_request():
    """一次点击 = 队首物品的全部档位合成一个请求：数量取各档合计，成交后参与行一起出门。"""
    lua, lst, bq, logs, g = load_buy_into_list()
    sent = []
    g.C_AuctionHouse["StartCommoditiesPurchase"] = \
        lambda itemID, qty: sent.append((int(itemID), int(qty)))
    lst.Add(make_hit_rows(lua, 101, [(148900, 2), (156000, 4)], 98))
    lst.Add(make_hit_rows(lua, 102, [(60000, 1)], 97))
    lst.TriggerBuy()
    if sent != [(101, 6)]:
        return fail("merge_request", f"应发一个 x6 合并请求，实际 {sent}")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    on(f, "COMMODITY_PRICE_UPDATED", 153000, 918000)   # 均价 153000 ≤ 门槛 446700 → 确认购买
    on(f, "COMMODITY_PURCHASE_SUCCEEDED")
    if lst.RowCount() != 1 or int(lst.GetFront()["itemID"]) != 102:
        return fail("merge_request", f"成交应删该件全部参与行，实际剩 {lst.RowCount()} 行")
    ok("merge_request")


def test_veto_rewrites_qty():
    """均价否决：用服务器总价反推低价存量，那一档不出门、数量改成存量，第二次点击按存量下单。

    复算 2026-10-21 18:27 辉熠银矿石（237365）：扫描低价 12.89g×17、门槛 14.12g、
    门槛以上最近一档 21.73g，服务器对 x17 报总价 263.33g = 12×12.89 + 5×21.73。
    """
    lua, lst, bq, logs, g = load_buy_into_list()
    sent = []
    g.C_AuctionHouse["StartCommoditiesPurchase"] = \
        lambda itemID, qty: sent.append((int(itemID), int(qty)))
    lst.Add(make_hit_rows(lua, 101, [(128900, 17)], 98,
                          threshold=141200, market=217300, spill=217300))
    lst.Add(make_hit_rows(lua, 102, [(60000, 1)], 97))
    if not lst.TriggerBuy() or sent != [(101, 17)]:
        return fail("veto_rewrites_qty", f"前置：应发 x17 合并请求，实际 {sent}")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    on(f, "COMMODITY_PRICE_UPDATED", 154900, 2633300)
    if lst.RowCount() != 2:
        return fail("veto_rewrites_qty", f"反解出的那一档该留下（加另一件共 2 行），实际 {lst.RowCount()}")
    front = lst.GetFront()
    if str(front["key"]) != "C:101:128900" or int(front["quantity"]) != 12:
        return fail("veto_rewrites_qty",
                    f"队首该是 128900 档且数量改成实测 12，实际 {front['key']}/{front['quantity']}")
    if int(front["totalPrice"]) != 128900 * 12:
        return fail("veto_rewrites_qty", f"总价要跟着数量重算，实际 {front['totalPrice']}")
    if bq.IsBusy():
        return fail("veto_rewrites_qty", "否决后应已结束购买会话、不占住购买通道")
    if not any("均价否决反推" in m for m in logs):
        return fail("veto_rewrites_qty", f"日志缺少反推过程，末尾 {logs[-2:]}")
    watched = [int(g.watched[i]) for i in range(1, len(g.watched) + 1)]
    if watched != [101]:
        return fail("veto_rewrites_qty", f"购买结局要把刚买那件送去复查（有没有新单），实际 {watched}")
    # 购买占通道的那 10 秒就是盲窗：没被重新答复过的行一律失去证据，
    # 而反解改写的那一行证据时刻=报价到账那一刻（服务器实测），必须还能点
    g.blindFrom = 99.5
    if not lst.Proven(lst.GetFront()):
        return fail("veto_rewrites_qty", "反解改写出的实测存量是报价那一刻的服务器证据，不该被盲窗判死")
    lst.TriggerBuy()
    if sent != [(101, 17), (101, 12)]:
        return fail("veto_rewrites_qty", f"第二次点击该按实测存量 x12 下单，实际 {sent}")
    ok("veto_rewrites_qty")


def test_veto_not_reversible_drops_rows():
    """不可反解（多档合并 / 无门槛以上价格 / 低价真的一个不剩）→ 参与行一起出门，不硬猜存量。

    第三种复算 2026-10-21 19:12:55 幽影锡（237363）：低价 10.99g×2、溢出档 22.50g，
    服务器对 x2 报总价 45.00g，反解存量 = (2×22.50 − 45.00)/(22.50 − 10.99) = 0，
    即低价池已被抢光。这条日志必须说"一个不剩"，不能把原因推给多档合并。
    """
    for name, pairs, spill, quote, want in (
            ("多档合并", [(128900, 17), (135000, 20)], 217300, None, "多档合并"),
            ("无溢出档", [(128900, 17)], None, None, "没有门槛以上价格"),
            ("低价一个不剩", [(109900, 2)], 225000, 450000, "低价一个不剩")):
        lua, lst, bq, logs, g = load_buy_into_list()
        lst.Add(make_hit_rows(lua, 101, pairs, 98,
                              threshold=141200, market=217300, spill=spill))
        if not lst.TriggerBuy():
            return fail("veto_not_reversible", f"{name}：TriggerBuy 未受理")
        f = bq.frame
        on = f.GetScript(f, "OnEvent")
        on(f, "COMMODITY_PRICE_UPDATED", 154900,
           quote or 154900 * sum(q for _, q in pairs))
        if lst.RowCount() != 0:
            return fail("veto_not_reversible", f"{name}：参与行该一起出门，实际剩 {lst.RowCount()}")
        veto = [m for m in logs if "不可反推" in m]
        if not any(want in m for m in veto):
            return fail("veto_not_reversible",
                        f"{name}：日志该写明原因（要含「{want}」），实际 {veto}")
    ok("veto_not_reversible")


def test_blind_window_grays_buy_button():
    """盲窗之后没被重新答复过的行不许点亮【立即购买】，扫描重新命中后自己亮回来。

    复算 2026-10-21 19:12:52—19:12:53：第一枪超时（购买独占通道 10 秒，期间扫描一条都没答上，
    最久未查 17.0s），灰按钮一放开就点到一条 11.3 秒前的行上，报价 22.50g 全档否决、白占通道。
    11 秒的静默不是"还有货"，是"还没被证伪"——诱饵实测活不到 2 秒。
    """
    lua, lst, logs, pings = load_snipe_list()
    g = lua.globals()
    buys = []
    g.AHSnipe["BuyQueue"] = lua.table_from({
        "IsBusy": lambda: False,
        "Buy": lambda hit: (buys.append(int(hit["itemID"])) or True),
    })
    lua.execute("""
        blindFrom = 0
        watched = {}
        scanOn = true
        AHSnipe.Scanner = {
            BlindFrom = function() return blindFrom end,
            IsPaused = function() return false end,
            IsEnabled = function() return scanOn end,
            Watch = function(id) watched[#watched + 1] = id return true end,
        }
    """)
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 97))

    def button_state():
        return (bool(lua.eval("NAMED.AHSnipeListFrame.buyBtn.__enabled")),
                str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()")))

    checks = [
        (button_state() == (True, "立即购买"), f"没有盲窗时正常点亮，实际 {button_state()}"),
    ]
    lua.execute("blindFrom = 98")   # 断流 12 秒后恢复，第一次答复把盲窗起点钉在 98（行的证据是 97）
    lst.UpdateBuyButton()
    checks.extend([
        (button_state()[0] is False, "盲窗之前的证据不能点亮按钮"),
        (button_state()[1] == "查新单中…", f"灰按钮要说正在主动找新单，实际 {button_state()}"),
        (lst.TriggerBuy() is False and buys == [], "队首未经重新答复时点击必须被拒绝"),
        (any("队首证据过期" in m for m in logs), f"日志要说明拒绝原因，末尾 {logs[-2:]}"),
    ])
    lst.Add(make_hit(lua, 101, 50000, 10, 500000, 100))   # 扫描恢复后重新命中这一档
    lst.UpdateBuyButton()
    checks.append((button_state() == (True, "立即购买"), f"重新答复过的行该重新点亮，实际 {button_state()}"))
    lst.TriggerBuy()
    checks.append((buys == [101], f"点亮后点击应受理，实际 {buys}"))
    # 同一行证据重新变陈旧、但这次扫描根本没跑：灰字要区分"正在主动查新单"和"没人在查"，
    # 后者用户得自己启动扫描，不能误以为等一下就会亮。
    lua.execute("blindFrom = 101; scanOn = false")
    lst.UpdateBuyButton()
    checks.append((button_state() == (False, "扫描未运行"),
                   f"扫描没跑时要点名『扫描未运行』，实际 {button_state()}"))
    checks.append((lst.TriggerBuy() is False and buys == [101], "扫描没跑时点击同样必须被拒绝"))
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("blind_window_grays_buy_button", "；".join(errors))
    ok("blind_window_grays_buy_button")


def test_scanner_blind_window():
    """盲窗起点由扫描记：断流超过 BLIND_LIMIT 秒后的第一次答复，把起点钉在检出断流的当下；
    断流期间到账的超时同样当场盖戳（不等下一次答复）。

    起点若记成"断流前最后一次答复的时刻"，那条 15 秒前的旧行会恒通过 List.Proven
    （它的 seenAt 在 NoteAnswer 之后取，同一帧里总要晚几十微秒），盲窗后按钮照样能点亮。
    """
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True), (103, True)))
    g = lua.globals()
    checks = [(float(sc.BlindFrom()) == 0, "开局没有盲窗")]
    sc.Start()
    g.Advance(0.11)
    first = parallel_sent(g)[-1]
    g.Deliver(first)
    answered = float(g.now)
    checks.append((float(sc.BlindFrom()) == 0, "连续答复不该开出盲窗"))
    g.gateReady = False                     # 节流闸门关闭：后面的查询全被挡在门外
    g.Advance(12)
    # 闸门关闭期间 101（100 秒发出）那条在途查询自己超时了：断流不必等下一次答复才盖戳，
    # "问了没答"到账的那一刻就是盲窗起点（起点记答复时刻的旧口径让静默前的旧行恒点亮按钮）。
    timed_out_at = float(g.sent[1]["at"]) + 10
    checks.append((abs(float(sc.BlindFrom()) - timed_out_at) < 1e-6,
                   f"在途查询超时当下就要盖起点（{timed_out_at}），实际 {float(sc.BlindFrom())}"))
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    sent = parallel_sent(g)
    checks.append((len(sent) > 1, "闸门恢复后应继续发查询"))
    g.Deliver(sent[-1])
    detected = float(g.now)
    checks.append((float(sc.BlindFrom()) == detected,
                   f"恢复后第一次答复要把盲窗起点钉在检出断流的当下（{detected}），"
                   f"不能记断流前最后一次答复（{answered}）——旧行会凭它恒点亮按钮"))
    # 暂停/关 AH 是确定的断流：不等阈值，暂停那一刻就是起点（此后的答复才算重新证实）
    g.Advance(3)
    sc.Pause()
    checks.append((float(sc.BlindFrom()) == float(g.now),
                   "暂停要把起点推到暂停当下：暂停期间留在列表里的行恢复后必须被重新答复过才能再点"))
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("scanner_blind_window", "；".join(errors))
    ok("scanner_blind_window")


def test_blind_window_rolls_only_reanswered_rows():
    """断流之后：盲窗前那条命中行不许算证据，只有被重新答复过的那一行才许点亮【立即购买】。

    诱饵实测活不到 2 秒，闸门一堵 12 秒再凭 12 秒前的行点亮按钮就是白打一枪（不变量1）。
    那笔 lst.Add 注入是为了复现真实客户端的帧内时钟：同一帧里行上的 seenAt 总在写 lastAnswerAt
    那次 GetTime 之后取（晚几十微秒），off-by-one（把起点记成最后一次答复）正是靠这个微秒差
    恒放行盲窗前的旧行。盲窗起点必须是"检出断流的当下"。
    """
    lua, sc, lst = load_scanner_parallel()
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    g.Deliver(101)                              # 断流前最后一次答复
    answered = float(g.now)
    if 101 not in parallel_rows(lst):
        return fail("blind_window_row_evidence", "前置：答复到账应有一条命中行")
    lua.execute("now = now + 0.0002")           # 帧内微秒差：行证据略晚于答复时刻
    lst.Add(make_hit(lua, 104, 7000, 1, 7000, float(g.now)))
    g.gateReady = False                         # 节流闸门关闭：盲窗开始，没有一条查询答得上来
    g.Advance(12)
    g.gateReady = True
    # 盲窗起点此刻已被"在途那条超时的查询"当场盖过（102 于 100.1 发出 → 110.1 超时），
    # 不是非等恢复后第一答才盖戳——否则这 12 秒里静默前的旧行一直能点亮按钮。
    timeout_stamp = float(g.sent[2]["at"]) + 10
    checks = [(abs(float(sc.BlindFrom()) - timeout_stamp) < 1e-6,
               f"断流期间到账的超时要当场盖起点（{timeout_stamp}），实际 {float(sc.BlindFrom())}")]
    sc.ScanNow()                                # 恢复后的第一轮
    g.Deliver(101)                              # 正是这次答复检出断流
    detected = float(sc.BlindFrom())
    rows = parallel_rows(lst)
    checks.extend([
        (detected > answered,
         f"盲窗起点必须是检出断流的当下（要 >{answered}），实际 {detected}"),
        (lst.Proven(rows[104]) is False, "盲窗前那条行（证据时刻略晚于最后一次答复）不能算证据"),
    ])
    g.Advance(0.11)
    g.Deliver(102)                              # 盲窗之后被重新答复过的那一行
    rows = parallel_rows(lst)
    checks.extend([
        (lst.Proven(rows[102]) is True, "盲窗后被重新答复过的那一行必须重新算证据"),
        (lst.Proven(rows[104]) is False, "没被重新答复过的行不能跟着一起点亮"),
        (float(sc.BlindFrom()) == detected, "连续答复不该再开出新盲窗，起点保持在检出断流那一刻"),
    ])
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("blind_window_row_evidence", "；".join(errors))
    ok("blind_window_row_evidence")


def test_pause_grays_rows_until_reanswered():
    """暂停的当下就是盲窗起点：留在列表里的行一律点不了，恢复并重新查到之后才重新算证据。

    Pause 里若把起点记成"最后一次答复的时刻"，暂停前那一行的 seenAt（同帧后取）恒通过 Proven，
    暂停期间留下的旧行照样能点亮按钮——与 Scanner.Pause 注释里立的规矩正好相反。
    """
    lua, sc, lst = load_scanner_flow(True)
    g = lua.globals()
    sc.Start()
    g.DeliverResults()
    answered = float(g.now)
    if lst.RowCount() != 1:
        return fail("pause_grays_rows", f"前置：答复到账应有一条命中行，实际 {lst.RowCount()}")
    lua.execute("now = now + 0.0002")           # 真实客户端帧内时钟：行证据略晚于答复时刻
    lst.Add(make_hit(lua, 102, 7000, 1, 7000, float(g.now)))
    sc.Pause()
    pause_at = float(sc.BlindFrom())
    now_at = float(g.now)
    checks = [
        (pause_at == now_at,
         f"暂停当下就是盲窗起点（要等于 {now_at}），不能停在最后一次答复（{answered}），实际 {pause_at}"),
        (lst.RowCount() == 2, f"暂停不删行，只是让它们失去证据，实际 {lst.RowCount()} 行"),
        (lst.Proven(parallel_rows(lst)[101]) is False, "暂停前那一行恢复后必须被重新答复过才能再点"),
        (lst.Proven(parallel_rows(lst)[102]) is False, "seenAt 略晚于最后一次答复的那一行同样不算证据"),
    ]
    g.Advance(3)                                # 暂停期间没有任何答复
    sc.Resume()
    g.Advance(0.11)
    g.DeliverResults()                          # 恢复后重新查到 101
    rows = parallel_rows(lst)
    checks.extend([
        (lst.Proven(rows[101]) is True, "恢复后被重新答复过的那一行重新算证据"),
        (lst.Proven(rows[102]) is False, "仍然没被答复过的那一行不能跟着点亮"),
        (float(sc.BlindFrom()) == pause_at, "恢复后的连续答复不该把起点往后挪"),
    ])
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("pause_grays_rows", "；".join(errors))
    ok("pause_grays_rows")


def test_silence_stamped_by_timeout_not_only_by_answers():
    """闸门不开、问了没答：这段静默必须由"超时到账"自己盖起点，否则一条 60 秒前的旧行恒点亮按钮。

    blindFrom 原来只有 NoteAnswer（答复到账）和 Pause 两个写点 ⇒ 只要闸门一直不开、或者发了没人答，
    这段正在进行的静默永远没有起点，静默前那几条行无限期 Proven=true（FRESH_GATE 只管排序不管按钮，
    列表只剩旧行时它就是队首、按钮就是亮的）。实测盲窗=断流 5~17 秒、诱饵活不到 2 秒。
    断流的口径是"问了没答"：本轮自己发出的查询超时到账即盖戳；扫描器压根没在发由 Pause 负责。
    """
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True), (103, True), (104, True)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    g.Deliver(101)                              # 断流前最后一次答复
    g.Advance(0.11)
    g.Deliver(103)
    g.Advance(0.11)
    g.Deliver(104)
    answered = float(g.now)
    rows = parallel_rows(lst)
    checks = [
        (set(rows) == {101, 103, 104}, f"前置：三条答复应有三行，实际 {set(rows)}"),
        (float(sc.BlindFrom()) == 0, "连续答复不开盲窗"),
        (all(lst.Proven(rows[key]) for key in (101, 103, 104)), "刚被答复过的行都算证据"),
    ]
    pre_sends = parallel_sent(g)                # 102 已在途、始终没人答复
    checks.append((102 not in rows, "前置：在途未答的 102 还没有行"))
    g.gateReady = False                         # 节流闸门关闭：此后一条新查询都发不出去
    g.roundInterval = 0
    timed_out_at = float(g.sent[2]["at"]) + 10  # 102 发出 + ITEM_TIMEOUT
    g.Advance(10.2)                             # 刚跨过 102 的超时时点
    blind = float(sc.BlindFrom())
    checks.extend([
        (abs(blind - timed_out_at) < 1e-6,
         f"超时到账的当下就盖起点（{timed_out_at}），不能等下一答，实际 {blind}"),
        (blind > answered,
         f"起点要晚于最后一次答复（{answered}），否则静默前那条旧行恒点亮按钮，实际 {blind}"),
    ])
    g.Advance(50)                               # 闸门继续不开：整段静默里没有任何答复
    rows = parallel_rows(lst)
    checks.extend([
        (abs(float(sc.BlindFrom()) - blind) < 1e-6,
         f"静默期间没有新的超时/答复，起点不该自己往后挪，实际 {float(sc.BlindFrom())}"),
        (parallel_sent(g) == pre_sends, "闸门关闭期间不该有查询发出"),
        (all(lst.Proven(rows[key]) is False for key in (101, 103, 104)),
         "60 秒静默之后，静默前那几条命中行一律不能再算证据"),
    ])
    # 闸门重新开了：本轮重新问到的行才重新算证据，没重新问到的那条继续灰着。
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    resent = parallel_sent(g)[len(pre_sends):]
    if len(resent) < 2:
        return fail("silence_stamped_by_timeout", f"前置：恢复后应至少重新问两条，实际 {resent}")
    g.Deliver(resent[0])
    detected = float(sc.BlindFrom())
    checks.append((abs(detected - float(g.now)) < 1e-6,
                   f"恢复后第一答检出断流：起点挪到检出的当下（{float(g.now)}），实际 {detected}"))
    # 这一答自己也是"重新答复过"的，但夹具在一个 Advance 步里时钟是冻住的，它的 seenAt 恰等于起点
    # （真客户端在同一帧里晚几十微秒）；同帧微秒差由 test_blind_window_rolls_only_reanswered_rows 钉住，
    # 这里只断言严格晚于起点的那些行。
    g.Advance(0.11)
    g.Deliver(resent[1])
    rows = parallel_rows(lst)
    checks.extend([
        (lst.Proven(rows[resent[1]]) is True,
         f"盲窗后被重新答复过的那一行（{resent[1]}）必须重新算证据"),
        (lst.Proven(rows[104]) is False, "没被重新答复过的那一行不能跟着一起点亮"),
        (abs(float(sc.BlindFrom()) - detected) < 1e-6, "连续答复不该再开出新盲窗，起点保持在检出断流那一刻"),
    ])
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("silence_stamped_by_timeout", "；".join(errors))
    ok("silence_stamped_by_timeout")


def test_scanner_watch_reequeries_bought_item():
    """购后复查是主动的：Watch 把那一件拉进聚焦，下一轮第一条查询就是它；未监控的不受理。

    列表端不干等调度顺序——买了就买了，剩下唯一有意义的问题是"有没有新单"。
    """
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True), (103, True)))
    g = lua.globals()
    # 让所有结果都不命中：没有命中就没有聚焦，Watch 是把 103 拉到队首的唯一入口
    lua.execute("""
        for _, item in ipairs(parallel_items) do
            results[item.itemID] = { {unitPrice=1000000, quantity=50} }
        end
    """)
    checks = [(not sc.Watch(999), "未监控/已停用的物品不该被复查受理")]
    sc.Start()
    for _ in range(4):
        g.Advance(0.11)
        for item in (101, 102, 103):
            g.Deliver(item)
    before = parallel_sent(g)
    checks.append((len(before) == 3, f"前置：三件都该查完一轮，实际 {before}"))
    checks.append((bool(sc.Watch(103)), "监控中的物品要被复查受理"))
    g.Advance(0.6)                     # Kick 的 0.5s 计时器到点，新轮开跑
    after = parallel_sent(g)
    checks.append((len(after) > len(before) and int(after[len(before)]) == 103,
                   f"复查那一件要排在新轮第一条，实际 {after[len(before):]}"))
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("scanner_watch_reequeries", "；".join(errors))
    ok("scanner_watch_reequeries")


def test_scanner_yields_to_purchase():
    """购买在途扫描必须整段让路：一条都不发，购买结束后 0.5 秒内的重试自己接上。

    复算 2026-10-21 21:35:11—21:35:15：x1 报价 3 秒不回来被判超时，同期扫描在 12/13/14 连拿
    3 条答复（每条回包 0.1s）——槽位全被自己吃掉。紧接着 x2 报价 21.88 = 10.89+10.99，
    证明那 1 个 @10.89 根本没被买走，x1 那一枪纯粹白等 3 秒。
    """
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    busy = {"v": False}
    g.AHSnipe["BuyQueue"] = lua.table_from({"IsBusy": lambda: busy["v"]})
    sc.Start()
    g.Advance(0.05)                       # 开局第一条立即发出；第二条要等 ITEM_GAP=0.1
    checks = [(parallel_sent(g) == [101], f"前置：应先只发出一条，实际 {parallel_sent(g)}")]
    busy["v"] = True                      # 用户点击购买：通道全部留给购买自己的报价/确认
    sent_at_hold = len(g.sent)
    g.Advance(6)                          # 远超 BUY_RETRY 与 ITEM_GAP（仍短于 ITEM_TIMEOUT）
    checks.append((len(g.sent) == sent_at_hold,
                   f"购买在途不能再发查询，实际新增 {len(g.sent) - sent_at_hold} 条"))
    busy["v"] = False
    g.Advance(0.6)
    checks.append((parallel_sent(g) == [101, 102],
                   f"购买结束后要在重试节拍里接上，实际 {parallel_sent(g)}"))
    sc.Pause()                            # 强制结算统计：让路次数要能被回传日志看到
    holds = [m for m in g.scanner_logs if "购买让路" in str(m)]
    checks.append((len(holds) == 1 and "购买让路0次" not in str(holds[0]),
                   f"统计要单独记购买让路次数，实际 {holds}"))
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("scanner_yields_to_purchase", "；".join(errors))
    ok("scanner_yields_to_purchase")


def test_scanner_spill_price():
    """命中要带门槛以上最近一档的单价：均价否决反推存量用它，缺了就只能放弃反推。"""
    lua, sc, lst = load_scanner_flow(True)
    g = lua.globals()
    # 门槛 = 100000 × 0.65 = 65000；门槛以上只有 100000 这一档
    lua.execute("snapshot = { {unitPrice=8000, quantity=1},"
                " {unitPrice=12000, quantity=3}, {unitPrice=100000, quantity=100} }")
    sc.Start()
    g.DeliverResults()
    if lst.RowCount() != 2:
        return fail("scanner_spill_price", f"前置：应 2 行，实际 {lst.RowCount()}")
    for i in range(1, lst.RowCount() + 1):
        row = lst.GetRows()[i]
        if int(row["spillPrice"]) != 100000:
            return fail("scanner_spill_price", f"行 {row['key']} 该带溢出档 100000，实际 {row['spillPrice']}")
    ok("scanner_spill_price")


def load_scanner():
    """独立环境加载 Scanner.lua（供纯函数 BuildRoundQueue 单测）。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    logs = []
    g.scanner_logs = logs
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda message: logs.append(str(message)),
            "GetItemDisplayName": lambda i: "物品%s" % i,
            "GetItems": lambda: [],
            "GetItem": lambda i: None,
            "GetDiscount": lambda: 0.65,
            "GetInterval": lambda: 0,
            "IsPaused": lambda: False,
        }),
    })
    with open(PRICE_CLUSTER_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    g.AHSnipe["PriceCluster"] = g.AHSnipePriceCluster
    g.AHSnipe["SnipeList"] = lua.table_from({"Add": lambda *a: None, "MarkGone": lambda *a: None})
    g.AHSnipe["BuyQueue"] = lua.table_from({"IsBusy": lambda: False})
    with open(SCANNER_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.Scanner


def test_build_round_queue():
    """无聚焦=全量；有聚焦=聚焦项排前 + 超 15 秒补查预算项；全部刚查过=只聚焦项；从未查过的立刻补。"""
    lua, sc = load_scanner()
    items = lua.table_from([lua.table_from({"itemID": i, "enabled": True}) for i in (1, 2, 3, 4)])
    empty = lua.table_from({})

    q, st = sc.BuildRoundQueue(items, empty, empty, 100, 15)
    if len(q) != 4 or int(st["focus"]) != 0:
        return fail("build_round_queue", f"无聚焦应全量 4 项，实际 {len(q)}")

    focus = lua.table_from({1: lua.table_from({"expire": 120, "miss": 0})})
    queried = lua.table_from({1: 99.5, 2: 96, 3: 80, 4: 84})
    q, st = sc.BuildRoundQueue(items, focus, queried, 100, 15)
    got = [int(q[i]) for i in range(1, len(q) + 1)]
    if got != [1, 3, 4]:
        return fail("build_round_queue", f"聚焦项应排前且只补到点项，实际 {got}")
    if int(st["focus"]) != 1 or int(st["refill"]) != 2 or abs(float(st["oldest"]) - 20) > 0.01:
        return fail("build_round_queue", f"统计错误 focus/refill/oldest={st['focus']}/{st['refill']}/{st['oldest']}")

    q2, st2 = sc.BuildRoundQueue(items, focus,
                                 lua.table_from({1: 99.5, 2: 99, 3: 98, 4: 99}), 100, 15)
    if len(q2) != 1 or int(q2[1]) != 1:
        return fail("build_round_queue", f"全部刚查过应只查聚焦项，实际 {len(q2)}")

    q3, _ = sc.BuildRoundQueue(items, focus, lua.table_from({1: 99.5}), 100, 15)
    if len(q3) != 4:
        return fail("build_round_queue", f"从未查询的项应立刻补查，实际 {len(q3)}")

    off = lua.table_from([
        lua.table_from({"itemID": 1, "enabled": True}),
        lua.table_from({"itemID": 2, "enabled": False}),
    ])
    q4, _ = sc.BuildRoundQueue(off, empty, empty, 100, 15)
    if len(q4) != 1 or int(q4[1]) != 1:
        return fail("build_round_queue", "禁用项不得入列")
    ok("build_round_queue")


def load_scanner_flow(is_commodity=True):
    """扫描器 + 列表的联动夹具。时钟与延时回调用 MOCK_ENV 里那一份可推进队列，这里不再自带一套。"""
    lua, sc = load_scanner()
    g = lua.globals()
    g.commodity_mode = is_commodity
    lua.execute("""
        local cfg = AHSnipe.Config
        cfg.GetItems = function() return {{itemID=101, enabled=true}} end
        cfg.GetItem = function(id) return {itemID=id, enabled=true} end
        cfg.GetInterval = function() return 1000 end
        cfg.GetListPos = function() return nil end
        pings = 0
        PlaySound = function() pings = pings + 1 end
        snapshot = {
            {unitPrice=10000, quantity=2, auctionID=1001, buyoutAmount=20000},
            {unitPrice=100000, quantity=100, auctionID=1002, buyoutAmount=10000000},
        }
        result_reads = 0
        full_results = true
        C_AuctionHouse.GetItemKeyInfo = function() return {isCommodity=commodity_mode} end
        C_AuctionHouse.HasSearchResults = function() return true end
        C_AuctionHouse.HasFullCommoditySearchResults = function() return full_results end
        local function CountResults()
            result_reads = result_reads + 1
            return #snapshot
        end
        C_AuctionHouse.GetNumCommoditySearchResults = CountResults
        C_AuctionHouse.GetNumItemSearchResults = CountResults
        C_AuctionHouse.GetCommoditySearchResultInfo = function(id, i) return snapshot[i] end
        C_AuctionHouse.GetItemSearchResultInfo = function(key, i) return snapshot[i] end
        function DeliverResults()
            local f = AHSnipe.Scanner.frame
            local on = f:GetScript("OnEvent")
            if commodity_mode then
                on(f, "COMMODITY_SEARCH_RESULTS_UPDATED", 101)
            else
                on(f, "ITEM_SEARCH_RESULTS_UPDATED", {itemID=101})
            end
        end
    """)
    with open(SNIPE_LIST_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    sc.Init()
    lst = g.AHSnipe.SnipeList
    lst.Init()
    on = lst.frame.GetScript(lst.frame, "OnEvent")
    on(lst.frame, "AUCTION_HOUSE_SHOW")
    return lua, sc, lst


def test_scanner_commodity_no_event_guard():
    lua, sc, lst = load_scanner_flow()
    sc.Start()
    for attempt in range(4):
        if attempt:
            sc.ScanNow()
        lua.globals().Advance(10)
        if lst.RowCount() != 0 or lua.globals().result_reads != 0:
            return fail("commodity_no_event_guard", f"第 {attempt + 1} 次无回包查询不应读取缓存或入列")
    ok("commodity_no_event_guard")


def test_scanner_item_no_event_guard():
    lua, sc, lst = load_scanner_flow(False)
    sc.Start()
    for attempt in range(4):
        if attempt:
            sc.ScanNow()
        lua.globals().Advance(10)
        if lst.RowCount() != 0 or lua.globals().result_reads != 0:
            return fail("item_no_event_guard", f"第 {attempt + 1} 次无回包查询不应读取缓存或入列")
    ok("item_no_event_guard")


def test_scanner_reopen_no_event_guard():
    lua, sc, lst = load_scanner_flow()
    g = lua.globals()
    sc.Start()
    g.DeliverResults()
    if lst.RowCount() != 1:
        return fail("reopen_no_event_guard", "首次新鲜命中应入列")
    for _ in range(3):
        sc.ScanNow()
        g.Advance(10)
    sc.Stop()
    on = lst.frame.GetScript(lst.frame, "OnEvent")
    on(lst.frame, "AUCTION_HOUSE_CLOSED")
    g.Advance(60)
    if lst.RowCount() != 0:
        return fail("reopen_no_event_guard", "关闭拍卖行应清空列表")
    reads, pings = g.result_reads, g.pings
    sc.Start()
    on(lst.frame, "AUCTION_HOUSE_SHOW")
    g.Advance(2)
    if lst.RowCount() != 0 or g.result_reads != reads or g.pings != pings:
        return fail("reopen_no_event_guard", "重开2秒且无回包，不应读旧缓存或重新提醒")
    g.Advance(8)
    if lst.RowCount() != 0 or g.result_reads != reads:
        return fail("reopen_no_event_guard", "重开后的查询超时也不应读旧缓存")
    sc.ScanNow()
    g.DeliverResults()
    if lst.RowCount() != 1 or g.pings != pings + 1:
        return fail("reopen_no_event_guard", "新回包恢复后应正常入列并提醒")
    ok("reopen_no_event_guard")


def test_scanner_complete_empty_clears_rows():
    for commodity in (True, False):
        lua, sc, lst = load_scanner_flow(commodity)
        g = lua.globals()
        sc.Start()
        g.DeliverResults()
        lst.Add(make_hit(lua, 102, 10000, 1, 10000, 99))
        if lst.RowCount() != 2:
            return fail("complete_empty_clears_rows", "测试前应有两种物品的行")
        sc.ScanNow()
        g.Advance(0.11)
        lua.execute("snapshot = {}")
        g.DeliverResults()
        if lst.RowCount() != 1 or lst.GetFront()["itemID"] != 102:
            return fail("complete_empty_clears_rows", f"commodity={commodity} 完整空结果应只清除该物品的旧行")
    ok("complete_empty_clears_rows")


def test_scanner_timeout_keeps_rows():
    for partial_event in (False, True):
        lua, sc, lst = load_scanner_flow()
        g = lua.globals()
        sc.Start()
        g.DeliverResults()
        seen_at = lst.GetFront()["seenAt"]
        sc.ScanNow()
        g.Advance(0.11)
        lua.execute("snapshot = {}; full_results = false")
        if partial_event:
            g.DeliverResults()
        g.Advance(10)
        if lst.RowCount() != 1 or lst.GetFront()["seenAt"] != seen_at:
            return fail("timeout_keeps_rows", f"partial_event={partial_event} 超时空结果不能作为删行证据")
    ok("timeout_keeps_rows")


def test_scanner_late_result_refreshes_rows():
    lua, sc, lst = load_scanner_flow()
    g = lua.globals()
    sc.Start()
    g.Advance(2)
    if lst.RowCount() != 0 or g.result_reads != 0:
        return fail("late_result_refreshes_rows", "回包前应拦截缓存")
    lua.execute("snapshot[1].quantity = 5")
    g.DeliverResults()
    if lst.RowCount() != 1 or lst.GetFront()["quantity"] != 5 or lst.GetFront()["seenAt"] != g.now:
        return fail("late_result_refreshes_rows", "迟到新回包应立即按新数量入列")
    g.Advance(0.5)
    sc.ScanNow()
    g.DeliverResults()
    if g.pings != 1 or lst.GetFront()["seenAt"] != g.now:
        return fail("late_result_refreshes_rows", "同价档刷新应更新时刻且不重复响铃")
    ok("late_result_refreshes_rows")


def load_scanner_parallel(items=((101, True), (102, True), (103, True), (104, True))):
    lua, sc, lst = load_scanner_flow()
    g = lua.globals()
    g.parallel_items = lua.table_from([
        lua.table_from({"itemID": item_id, "enabled": True, "commodity": commodity})
        for item_id, commodity in items
    ])
    lua.execute("""
        results, full, types, reads, sent, pending, scan_timers = {}, {}, {}, {}, {}, {}, {}
        gateReady, paused, roundInterval = true, false, 1000
        syncResults, closeGateAfterFirst = false, false
        sendDepth, maxSendDepth, maxPending, duplicateSends = 0, 0, 0, 0
        for i, item in ipairs(parallel_items) do
            local id, price = item.itemID, i * 10000
            types[id], full[id], reads[id] = item.commodity, true, 0
            results[id] = {
                {unitPrice=price, quantity=i+1, auctionID=id*10+1, buyoutAmount=price*(i+1)},
                {unitPrice=price*10, quantity=100, auctionID=id*10+2, buyoutAmount=price*1000},
            }
        end
        local cfg = AHSnipe.Config
        cfg.GetItems = function() return parallel_items end
        cfg.GetItem = function(id)
            for _, item in ipairs(parallel_items) do
                if item.itemID == id then return item end
            end
        end
        cfg.GetInterval = function() return roundInterval end
        cfg.IsPaused = function() return paused end
        cfg.SetPaused = function(value) paused = value end
        local newTimer = C_Timer.NewTimer
        C_Timer.NewTimer = function(delay, cb)
            -- 计时器本体仍由 MOCK_ENV 那份唯一队列管理（Advance 才会真的跑回调），
            -- 这里只是再记一份引用给"轮次/静默计时器有没有被漏掉、取消干净"这类断言用。
            local timer = newTimer(delay, cb)
            scan_timers[#scan_timers+1] = timer
            return timer
        end
        C_Timer.After = C_Timer.NewTimer
        function Emit(event, ...)
            local frame = AHSnipe.Scanner.frame
            frame:GetScript("OnEvent")(frame, event, ...)
        end
        function Deliver(id)
            if full[id] or not types[id] then pending[id] = nil end
            if types[id] then
                Emit("COMMODITY_SEARCH_RESULTS_UPDATED", id)
            else
                Emit("ITEM_SEARCH_RESULTS_UPDATED", {itemID=id})
            end
        end
        local function RecordSend(id, api)
            sendDepth = sendDepth + 1
            maxSendDepth = math.max(maxSendDepth, sendDepth)
            if pending[id] then duplicateSends = duplicateSends + 1 end
            pending[id] = true
            local count = 0
            for _ in pairs(pending) do count = count + 1 end
            maxPending = math.max(maxPending, count)
            sent[#sent+1] = {itemID=id, at=now, api=api, ready=gateReady}
            if closeGateAfterFirst and #sent == 1 then gateReady = false end
            if syncResults then Deliver(id) end
            sendDepth = sendDepth - 1
        end
        C_AuctionHouse.IsThrottledMessageSystemReady = function() return gateReady end
        C_AuctionHouse.GetItemKeyInfo = function(key) return {isCommodity=types[key.itemID]} end
        C_AuctionHouse.HasSearchResults = function() return true end
        C_AuctionHouse.HasFullCommoditySearchResults = function(id) return full[id] end
        C_AuctionHouse.RefreshCommoditySearchResults = function(id) RecordSend(id, "refresh") end
        C_AuctionHouse.SendSearchQuery = function(key) RecordSend(key.itemID, "search") end
        C_AuctionHouse.GetNumCommoditySearchResults = function(id)
            assert(types[id] == true, "commodity reader used for another type")
            reads[id] = reads[id] + 1
            return #results[id]
        end
        C_AuctionHouse.GetNumItemSearchResults = function(key)
            local id = key.itemID
            assert(types[id] == false, "item reader used for another type")
            reads[id] = reads[id] + 1
            return #results[id]
        end
        C_AuctionHouse.GetCommoditySearchResultInfo = function(id, i) return results[id][i] end
        C_AuctionHouse.GetItemSearchResultInfo = function(key, i) return results[key.itemID][i] end
    """)
    return lua, sc, lst


def parallel_sent(g):
    return [int(g.sent[i]["itemID"]) for i in range(1, len(g.sent) + 1)]


def parallel_rows(lst):
    rows = lst.GetRows()
    return {int(rows[i]["itemID"]): rows[i] for i in range(1, len(rows) + 1)}


def parallel_check(name, checks):
    errors = [message for passed, message in checks if not passed]
    if errors:
        fail(name, "; ".join(errors))
    else:
        ok(name)


def test_scanner_parallel_capacity_refill():
    lua, sc, lst = load_scanner_parallel()
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    checks = [(parallel_sent(g) == [101, 102], f"初始应发 A/B，实际 {parallel_sent(g)}")]
    g.Advance(0.3)
    checks.append((len(g.sent) == 2, "A/B 未返回时不能发送 C"))
    g.Deliver(102)
    g.Advance(0.11)
    checks.append((parallel_sent(g) == [101, 102, 103], "B 先完成后 0.11s 内应补 C，不等 A"))
    g.Deliver(103)
    g.Advance(0.11)
    checks.append((parallel_sent(g) == [101, 102, 103, 104], "C 完成后应补 D，不等 A"))
    checks.extend([
        (g.reads[101] == 0 and bool(g.pending[101]), "A 应仍在途且未读缓存"),
        (g.maxPending <= 2 and g.duplicateSends == 0, "最多两件在途且不可重复发送"),
    ])
    g.Deliver(104)
    g.Deliver(101)
    checks.append((set(parallel_rows(lst)) == {101, 102, 103, 104}, "乱序完成后四件都应入列"))
    parallel_check("parallel_capacity_refill", checks)


def test_scanner_parallel_send_gap():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True), (103, True)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.05)
    g.Deliver(101)
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.049)
    checks = [(parallel_sent(g) == [101], "不足 0.1s，即使槽位空且 READY 也不可发送第二件")]
    g.Advance(0.011)
    checks.append((parallel_sent(g) == [101, 102], "首发后 0.11s 应填充第二件"))
    g.Deliver(102)
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.05)
    checks.append((len(g.sent) == 2, "完成回调不能绕过实际发送间隔"))
    g.Advance(0.06)
    times = [float(g.sent[i]["at"]) for i in range(1, len(g.sent) + 1)]
    checks.extend([
        (parallel_sent(g) == [101, 102, 103], "每次间隔到点应继续补位"),
        (all(b - a >= 0.1 - 1e-8 for a, b in zip(times, times[1:])), f"实际发送间隔不足 0.1s：{times}"),
    ])
    parallel_check("parallel_send_gap", checks)


def test_scanner_parallel_mixed_results():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, False)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    g.Deliver(102)
    first = parallel_rows(lst)
    checks = [(set(first) == {102}, "非 commodity B 先返回只能产生 B 的行")]
    g.Deliver(101)
    rows = parallel_rows(lst)
    for item_id, commodity, price, qty in ((101, True, 10000, 2), (102, False, 20000, 3)):
        row = rows.get(item_id)
        checks.append((row is not None and row["isCommodity"] == commodity
                       and row["unitPrice"] == price and row["quantity"] == qty,
                       f"{item_id} 的类型、价格、数量必须来自自己的结果"))
    row = rows.get(102)
    checks.append((row is not None and row["auctionID"] == 1021, "非 commodity 必须保留自己的 auctionID"))
    parallel_check("parallel_mixed_results", checks)


def test_scanner_parallel_gate_ready():
    lua, sc, _ = load_scanner_parallel()
    g = lua.globals()
    g.closeGateAfterFirst = True
    sc.Start()
    g.Advance(0.11)
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    checks = [(parallel_sent(g) == [101], "发 A 后 gate=false；虚假 READY 不可发 B")]
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    checks.extend([
        (parallel_sent(g) == [101, 102], "A 仍在途时 READY+gate=true 应填第二槽"),
        (g.reads[101] == 0, "填槽不能把 A 的旧缓存当回包"),
        (all(g.sent[i]["ready"] for i in range(1, len(g.sent) + 1)), "每次实际发送都必须 gate 就绪"),
    ])
    parallel_check("parallel_gate_ready", checks)


def test_scanner_parallel_send_timestamps():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, False)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    g.Advance(0.3)
    b_seen = float(g.now)
    g.Deliver(102)
    g.Advance(0.2)
    a_seen = float(g.now)
    g.Deliver(101)
    rows = parallel_rows(lst)
    send_times = {int(g.sent[i]["itemID"]): float(g.sent[i]["at"]) for i in range(1, len(g.sent) + 1)}
    checks = []
    for item_id, seen in ((101, a_seen), (102, b_seen)):
        row = rows.get(item_id)
        checks.append((row is not None and item_id in send_times
                       and abs(row["querySendAt"] - send_times[item_id]) < 1e-8
                       and abs(row["seenAt"] - seen) < 1e-8 and row["roundStartAt"] == 100,
                       f"{item_id} 应携带独立 querySendAt/seenAt 和共同 roundStartAt"))
    parallel_check("parallel_send_timestamps", checks)


def test_scanner_parallel_independent_timeout():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True), (103, True)))
    g = lua.globals()
    g.closeGateAfterFirst = True
    sc.Start()
    g.Advance(1.1)
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    checks = [(parallel_sent(g) == [101, 102], "B 应在延迟 gate 开启后独立发送")]
    g.Advance(8.78)
    checks.append((len(g.sent) == 2, "A 自发送未满 10s，不能提前超时放出 C"))
    g.Advance(0.12)
    checks.append((parallel_sent(g) == [101, 102, 103], "A 到 10s 应独立超时并在 0.11s 内补 C"))
    checks.append((g.reads[101] == 0 and g.reads[102] == 0, "无回包超时不可读取任何旧缓存"))
    g.Deliver(102)
    rows = parallel_rows(lst)
    checks.append((102 in rows, "B 距自身发送不足 10s，不能被 A 的超时清理"))
    parallel_check("parallel_independent_timeout", checks)


def test_scanner_parallel_round_drain_gap():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lua.execute("results[101] = {}; results[102] = {}; roundInterval = 0.5")
    sc.Start()
    g.Advance(0.11)
    g.Deliver(102)
    g.Advance(0.6)
    checks = [(parallel_sent(g) == [101, 102], "队列已发送但 A 未完成，不能结束并续轮")]
    g.Deliver(101)
    g.Advance(0.49)
    checks.append((len(g.sent) == 2, "全部返回后仍须保留配置的轮间歇"))
    g.Advance(0.02)
    checks.append((parallel_sent(g) == [101, 102, 101], "最后一件完成且轮间歇到点才开始下一轮"))
    parallel_check("parallel_round_drain_gap", checks)


def test_scanner_parallel_hit_not_freshen_peer():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 99))
    lua.execute("results[101] = {}")
    sc.Start()
    g.Advance(0.11)
    g.Deliver(102)
    checks = [(102 in parallel_rows(lst), "B 的有效回包应立即命中，不等待 A")]
    g.Advance(10)
    rows = parallel_rows(lst)
    row = rows.get(101)
    checks.extend([
        (g.reads[101] == 0, "B 命中不能令无回包 A 在静默/超时时读旧缓存"),
        (row is not None and row["seenAt"] == 99 and row["unitPrice"] == 7000,
         "无回包 A 的旧行不能被刷新或删除"),
    ])
    parallel_check("parallel_hit_not_freshen_peer", checks)


def test_scanner_parallel_noid_unproven():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    g.Emit("COMMODITY_SEARCH_RESULTS_RECEIVED")
    g.Advance(2.1)
    parallel_check("parallel_noid_unproven", [
        (g.reads[101] == 0 and g.reads[102] == 0 and lst.RowCount() == 0,
         "无 ID 的 RECEIVED 不得给任何无新鲜证据的在途请求赋 fresh 或读缓存"),
    ])


def test_scanner_parallel_noid_last_pending():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 99))
    lua.execute("results[101] = {}")
    sc.Start()
    g.Advance(0.11)
    g.Deliver(102)
    g.Emit("COMMODITY_SEARCH_RESULTS_RECEIVED")
    g.Advance(2.1)
    row = parallel_rows(lst).get(101)
    parallel_check("parallel_noid_last_pending", [
        (g.reads[101] == 0, "即使只剩 A，无 ID 信号也可能来自 B，不能赋予 A 新鲜度"),
        (row is not None and row["seenAt"] == 99, "迟到无 ID 信号不能删除或更新 A 的旧行"),
    ])


def test_scanner_parallel_noid_wake_proven():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lua.execute("full[101] = false; full[102] = false")
    sc.Start()
    g.Advance(0.11)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 101)
    checks = [(g.reads[101] == 0, "带 ID 的 ADDED 尚未 full 时不能读取完整结果")]
    lua.execute("full[101] = true; full[102] = true")
    g.Emit("COMMODITY_SEARCH_RESULTS_RECEIVED")
    checks.extend([
        (set(parallel_rows(lst)) == {101}, "无 ID 信号应立即唤醒已由 A 的 ADDED 证实 fresh 的完整结果"),
        (g.reads[102] == 0, "唤醒 A 不得顺带赋予 B 新鲜度"),
    ])
    parallel_check("parallel_noid_wake_proven", checks)


def test_scanner_parallel_partial_silence():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 99))
    lua.execute("full[101] = false; full[102] = false; results[101] = {}")
    sc.Start()
    g.Advance(0.11)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 101)
    g.Advance(1)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 102)
    g.full[102] = True
    g.Advance(1)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 101)
    g.Advance(1.01)
    rows = parallel_rows(lst)
    checks = [
        (102 in rows and g.reads[102] == 1, "A 的 ADDED 不得重置 B 的独立 2s 静默计时器"),
        (g.reads[101] == 0, "A 虽有 ADDED 但未 full，不得提前读取"),
    ]
    g.Advance(7)
    row = parallel_rows(lst).get(101)
    checks.extend([
        (row is not None and row["seenAt"] == 99, "A 部分空结果超时不能删行"),
        (g.reads[102] == 1, "A 的超时不能重复读取或完成 B"),
        (not any(not g.scan_timers[i]["cancelled"] and g.scan_timers[i]["delay"] == 10
                 for i in range(1, len(g.scan_timers) + 1)), "B 完成不能遗失 A 的独立超时计时器"),
    ])
    parallel_check("parallel_partial_silence", checks)


def check_scanner_parallel_cleanup(method):
    lua, sc, lst = load_scanner_parallel()
    g = lua.globals()
    lua.execute("full[101] = false; full[102] = false")
    sc.Start()
    g.Advance(0.11)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 101)
    g.Emit("COMMODITY_SEARCH_RESULTS_ADDED", 102)
    active = [g.scan_timers[i] for i in range(1, len(g.scan_timers) + 1)
              if not g.scan_timers[i]["cancelled"]]
    checks = [(sum(t["delay"] == 10 for t in active) == 2, "清理前应有两件独立超时计时器")]
    getattr(sc, method)()
    count = len(g.sent)
    checks.append((all(t["cancelled"] for t in active), f"{method} 必须取消两件的超时/静默及调度计时器"))
    for timer in active:
        timer["cb"]()
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    lua.execute("full[101] = true; full[102] = true")
    g.Deliver(101)
    g.Deliver(102)
    g.Advance(20)
    checks.extend([
        (len(g.sent) == count, f"{method} 后旧回调、READY、迟到回包不得再发送查询"),
        (g.reads[101] == 0 and g.reads[102] == 0 and lst.RowCount() == 0,
         f"{method} 后旧回调不得读取结果或入列"),
    ])
    parallel_check(f"parallel_{method.lower()}_cleanup", checks)


def test_scanner_parallel_stop_cleanup():
    check_scanner_parallel_cleanup("Stop")


def test_scanner_parallel_pause_cleanup():
    check_scanner_parallel_cleanup("Pause")


def test_scanner_parallel_paused_manual_round():
    lua, sc, lst = load_scanner_parallel()
    g = lua.globals()
    g.paused = True
    sc.Start()
    checks = [(len(g.sent) == 0, "暂停打开 AH 不自动查询"), (sc.ScanNow(), "暂停时应受理手动扫描")]
    g.Advance(0.11)
    g.Deliver(102)
    g.Advance(0.11)
    g.Deliver(103)
    g.Advance(0.11)
    g.Deliver(104)
    g.Deliver(101)
    checks.extend([
        (parallel_sent(g) == [101, 102, 103, 104], "暂停态手动扫描必须完整发完一轮"),
        (set(parallel_rows(lst)) == {101, 102, 103, 104}, "暂停态也必须处理这一轮的全部结果"),
    ])
    g.Advance(20)
    checks.extend([
        (parallel_sent(g) == [101, 102, 103, 104], "手动单轮完成后不能因聚焦或轮间歇自动续轮"),
        (sc.IsPaused() and not sc.IsEnabled(), "手动扫描不能改变暂停状态"),
        (all(g.scan_timers[i]["cancelled"] for i in range(1, len(g.scan_timers) + 1)),
         "手动单轮完成后不得残留请求或续轮计时器"),
    ])
    parallel_check("parallel_paused_manual_round", checks)


def test_scanner_parallel_synchronous_results():
    lua, sc, _ = load_scanner_parallel()
    g = lua.globals()
    lua.execute("for id in pairs(results) do results[id] = {} end; syncResults = true; roundInterval = 0.5")
    sc.Start()
    g.Advance(0.35)
    times = [float(g.sent[i]["at"]) for i in range(1, len(g.sent) + 1)]
    checks = [
        (parallel_sent(g) == [101, 102, 103, 104], "同步回包必须逐件发完且不能重复发送"),
        (g.maxSendDepth == 1 and g.maxPending <= 2 and g.duplicateSends == 0, "同步 API 回调不得重入发送或突破两槽"),
        (all(b - a >= 0.1 - 1e-8 for a, b in zip(times, times[1:])), "同步回包不能跳过 0.1s 发送间隔"),
        (all(g.reads[i] == 1 for i in (101, 102, 103, 104)), "每个同步结果必须只读取一次"),
    ]
    g.Advance(0.4)
    checks.append((len(g.sent) == 4, "同步完成也要保留轮间歇"))
    g.Advance(0.1)
    checks.append((parallel_sent(g) == [101, 102, 103, 104, 101], "同步完成后必须正常结束轮次并按间隔续轮"))
    parallel_check("parallel_synchronous_results", checks)


def test_scanner_parallel_refresh_exclusive():
    lua, sc, _ = load_scanner_parallel(((102, False), (101, True), (103, True)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    lua.execute("""
        refresh_callbacks = {}
        function OnRefresh(hit, reason)
            refresh_callbacks[#refresh_callbacks+1] = {hit=hit, reason=reason}
            gateReady = false
        end
    """)
    sc.RefreshHit(lua.table_from({"itemID": 102, "isCommodity": False}), g.OnRefresh)
    g.Advance(0.11)
    checks = [(parallel_sent(g) == [102, 101], "RefreshHit 应复用同物品在途查询，不发无法区分回包的新请求")]
    g.Deliver(101)
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.6)
    checks.extend([
        (parallel_sent(g) == [102, 101], "刷新等待时，后台回包/READY 不得补发 C 或启动新轮"),
        (g.maxPending <= 2 and g.duplicateSends == 0, "刷新不能突破两槽或重复发送同一物品"),
        (len(g.refresh_callbacks) == 0, "其他物品回包不得完成或打断刷新回调"),
    ])
    g.Deliver(102)
    callback = g.refresh_callbacks[1]
    hit = callback["hit"] if callback is not None else None
    checks.append((len(g.refresh_callbacks) == 1 and hit is not None and hit["itemID"] == 102
                   and hit["matched"][1]["auctionID"] == 1021, "刷新应恰好回调一次且返回目标物品的 auctionID"))
    g.Deliver(102)
    g.Advance(11)
    checks.append((len(g.refresh_callbacks) == 1, "重复事件和旧超时不能重复完成刷新"))
    sc.Stop()
    checks.append((all(g.scan_timers[i]["cancelled"] for i in range(1, len(g.scan_timers) + 1)),
                   "停止后后台与刷新计时器都应清理"))
    parallel_check("parallel_refresh_exclusive", checks)


def test_scanner_parallel_refresh_stop_cleanup():
    lua, sc, _ = load_scanner_parallel(((102, False), (101, True)))
    g = lua.globals()
    sc.Start()
    g.Advance(0.11)
    callbacks = []
    sc.RefreshHit(lua.table_from({"itemID": 102, "isCommodity": False}),
                  lambda hit, reason: callbacks.append((hit, reason)))
    g.Advance(0.11)
    sc.Stop()
    count = len(g.sent)
    g.Deliver(102)
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(20)
    parallel_check("parallel_refresh_stop_cleanup", [
        (len(callbacks) == 1 and callbacks[0][0] is None and bool(callbacks[0][1]),
         "Stop 打断刷新必须只回调一次失败"),
        (len(g.sent) == count and g.reads[102] == 0, "刷新停止后不能因迟到事件读缓存或发查询"),
        (all(g.scan_timers[i]["cancelled"] for i in range(1, len(g.scan_timers) + 1)),
         "刷新停止后必须清理全部计时器"),
    ])


def check_scanner_parallel_own_late(commodity):
    items = ((101, True), (102, False)) if commodity else ((102, False), (101, True))
    lua, sc, _ = load_scanner_parallel(items)
    g = lua.globals()
    lua.execute("results[101] = {}; results[102] = {}")
    sc.Start()
    g.Deliver(items[0][0])
    g.Advance(0.11)
    g.Deliver(items[1][0])
    g.Advance(0.11)
    count = len(g.sent)
    reads = int(g.reads[101] + g.reads[102])
    g.Deliver(items[0][0])
    g.Advance(0.6)
    checks = [
        (len(g.sent) == count, "非最后一件的本会话迟到回包也必须过滤，不得当用户浏览 Kick"),
        (int(g.reads[101] + g.reads[102]) == reads, "已完成请求的迟到回包不得重新读取"),
    ]
    g.Emit("COMMODITY_SEARCH_RESULTS_UPDATED", 999)
    g.Advance(0.6)
    checks.append((len(g.sent) > count, "真正未由本会话查询的用户浏览仍应触发 Kick"))
    parallel_check("parallel_own_late_commodity" if commodity else "parallel_own_late_item", checks)


def test_scanner_parallel_own_late_commodity():
    check_scanner_parallel_own_late(True)


def test_scanner_parallel_own_late_item():
    check_scanner_parallel_own_late(False)


def test_scanner_parallel_metrics():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lua.execute("""
        results[101], results[102], roundInterval = {}, {}, 0
        local send = C_AuctionHouse.RefreshCommoditySearchResults
        C_AuctionHouse.RefreshCommoditySearchResults = function(id)
            send(id)
            C_Timer.NewTimer(0.3, function() Deliver(id) end)
        end
    """)
    sc.Start()
    g.Advance(29)
    checks = [(not any("扫描统计" in msg for msg in g.scanner_logs), "统计不能每轮刷屏，应至少间隔30秒")]
    g.Advance(2)
    reports = [msg for msg in g.scanner_logs if "扫描统计" in msg]
    checks.extend([
        (len(reports) == 1, f"持续扫描31秒应有一次汇总，实际 {reports}"),
        (bool(reports) and all(text in reports[0] for text in (
            "峰值在途2", "超时/失败0", "平均轮耗时", "每分钟完成",
            "闸门阻塞0次/0.0s", "平均回包0.30s", "峰值回包0.30s", "静默收尾0")),
         f"汇总要能区分闸门卡、回包慢、本地串槽三种成因：{reports}"),
    ])
    sc.Stop()
    parallel_check("parallel_metrics", checks)


def test_scanner_parallel_metrics_timeouts():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    g.roundInterval = 0
    sc.Start()
    g.Emit("COMMODITY_SEARCH_RESULTS_RECEIVED")
    g.Advance(31)
    reports = [msg for msg in g.scanner_logs if "扫描统计" in msg]
    parallel_check("parallel_metrics_timeouts", [
        (len(reports) == 1 and all(text in reports[0] for text in (
            "完成0", "超时/失败6", "峰值在途2", "无编号信号1",
            "闸门阻塞0次/0.0s", "平均回包0.00s", "峰值回包0.00s", "静默收尾0")),
         f"无回包和无编号信号不能计入有效完成，应保留诊断计数：{reports}"),
    ])
    sc.Stop()


def test_scanner_parallel_metrics_gate_and_silence():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True)))
    g = lua.globals()
    lua.execute("""
        full[101], full[102], roundInterval = false, false, 0
        local send = C_AuctionHouse.RefreshCommoditySearchResults
        C_AuctionHouse.RefreshCommoditySearchResults = function(id)
            send(id)
            C_Timer.NewTimer(0.3, function() Deliver(id) end)
        end
    """)
    g.closeGateAfterFirst = True
    sc.Start()
    g.Advance(0.2)
    checks = [(parallel_sent(g) == [101], "闸门未就绪时第二件不能发出")]
    g.Advance(0.1)
    checks.append((parallel_sent(g) == [101], "0.3s 仍未 READY 不能偷发第二件"))
    g.full[101] = True
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    checks.append((parallel_sent(g) == [101, 102], "READY 后应立刻补第二件，与 101 同时在途"))
    g.Advance(0.3)
    g.full[102] = True
    g.Advance(2.1)
    checks.append((parallel_sent(g) == [101, 102], "两件都未收尾时不能提前结束轮次"))
    sc.Stop()
    reports = [msg for msg in g.scanner_logs if "扫描统计" in msg]
    checks.append((len(reports) == 1 and bool(reports) and all(text in reports[0] for text in (
        "峰值在途2", "闸门阻塞1次/0.2s", "平均回包2.30s", "峰值回包2.30s", "静默收尾2",
        "完成2", "超时/失败0")), f"阻塞时长与静默收尾必须分别记账：{reports}"))
    parallel_check("parallel_metrics_gate_and_silence", checks)


def test_scanner_parallel_info_wait():
    lua, sc, lst = load_scanner_parallel(((101, True), (102, False), (103, True)))
    g = lua.globals()
    lua.execute("""
        infoReady = false
        local info = C_AuctionHouse.GetItemKeyInfo
        C_AuctionHouse.GetItemKeyInfo = function(key)
            if key.itemID == 102 and not infoReady then return nil end
            return info(key)
        end
    """)
    sc.Start()
    g.Advance(0.11)
    g.Deliver(101)
    g.Advance(0.2)
    checks = [(parallel_sent(g) == [101], "元数据未到时不能发送未知类型或提前结束轮次")]
    g.infoReady = True
    g.Emit("ITEM_KEY_ITEM_INFO_RECEIVED", 102)
    g.Advance(0.11)
    g.Deliver(102)
    g.Deliver(103)
    checks.extend([
        (parallel_sent(g) == [101, 102, 103], "元数据到达应继续补位并按非commodity路径发送102"),
        (set(parallel_rows(lst)) == {101, 102, 103}, "元数据等待不能清理其他物品结果"),
    ])
    sc.Stop()
    parallel_check("parallel_info_wait", checks)


def test_scanner_parallel_refresh_waits_for_slot():
    lua, sc, _ = load_scanner_parallel(((101, True), (102, True), (103, False)))
    g = lua.globals()
    lua.execute("syncResults = true; for id in pairs(results) do results[id] = {} end")
    sc.Start()
    g.Advance(0.31)
    g.syncResults = False
    sc.ScanNow()
    g.Advance(0.11)
    callbacks = []
    sc.RefreshHit(lua.table_from({"itemID": 103}), lambda hit, reason: callbacks.append((hit, reason)))
    g.gateReady = False
    g.Deliver(101)
    g.Advance(0.11)
    checks = [(parallel_sent(g) == [101, 102, 103, 101, 102], "刷新等待槽位且不能绕过gate")]
    g.gateReady = True
    g.Emit("AUCTION_HOUSE_THROTTLED_SYSTEM_READY")
    g.Advance(0.11)
    checks.append((parallel_sent(g) == [101, 102, 103, 101, 102, 103], "槽位和gate可用时优先发刷新"))
    g.Deliver(103)
    checks.extend([
        (len(callbacks) == 1 and callbacks[0][1] == "诱饵已被买走或下架", "空结果刷新应按目标物品只结算一次"),
        (g.maxPending <= 2 and g.duplicateSends == 0, "刷新不能使实际在途突破两件"),
    ])
    sc.Stop()
    parallel_check("parallel_refresh_waits_for_slot", checks)


PLUGIN_ORDER = [
    "Core.lua", "Config.lua", "PriceCluster.lua", "Scanner.lua",
    "BuyQueue.lua", "SnipeList.lua", "ConfigPanel.lua", "LogWindow.lua",
]

# 模拟 WoW 客户端环境：语法检查抓不到运行期错误（nil 全局、帧方法签名、初始化链断裂等），
# 这里把所有 Lua 文件按 TOC 顺序真实执行一遍，并驱动 加载→开拍卖行→购买→事件→斜杠命令 全链路。
MOCK_ENV = r"""
-- ===== 帧替身：真客户端没有的方法名不许凭空出现（口径照 tests/run_gql_tests.py 的 CreateFrame）=====
-- 旧版 __index 给任何未知键都造一个"返回 Mock 的函数"，等于替身自己发明 API：产品代码打错接口名、
-- 或调到 12.x 已删除的接口，离线照样全绿，进游戏只是静默无反应（本仓库反复踩过这一面）。
-- 现在只认下面这张白名单，读到大写开头且不在名单里的键当场抛错。
-- 小写开头的未知键仍回 nil：那是插件自己挂在帧上的便签字段（buyBtn/rowFS/emptyFS/content…），
-- 真客户端的帧是 userdata，读没有的键就是 nil，不能跟着报错。
-- 名单里每一项都是 AHSnipe 实际调用过、且 12.x 客户端确有的方法；要加新项必须先确认客户端真有，
-- 禁止为了让某条测试变绿而扩名单。
local AH_FRAME_API   -- 先声明：CreateFontString/CreateTexture 要造同类替身
local AH_KNOWN_EVENTS

local function Mock(name)
    local obj = { __name = name or "obj", __shown = false, __enabled = true,
                  __scripts = {}, __events = {}, __h = 0, __w = 0, __text = "" }
    return setmetatable(obj, { __index = function(t, k)
        local api = AH_FRAME_API[k]
        if api ~= nil then return api end
        if type(k) == "string" and string.match(k, "^%u") then
            error(("帧替身里没有方法 %q（帧 %s）：真客户端 12.x 无此名即产品代码调了不存在的接口")
                :format(tostring(k), tostring(rawget(t, "__name"))), 2)
        end
        return nil
    end })
end

AH_FRAME_API = {
    Show = function(self) self.__shown = true end,
    Hide = function(self) self.__shown = false end,
    IsShown = function(self) return self.__shown == true end,
    SetScript = function(self, name, fn) self.__scripts[name] = fn end,
    GetScript = function(self, name) return self.__scripts[name] end,
    -- 锚点按真客户端语义存成列表：GetPoint(i) 回 point, relativeTo, relativePoint, x, y，
    -- 没锚点回 nil。布局回归（首行内缩、名称右界）和"拖窗存位置漏取一位把 relativePoint 当 x"
    -- 都只能从实参看出来，所以这里必须原样记下参数。
    SetPoint = function(self, ...)
        local ps = rawget(self, "__pts")
        if not ps then ps = {}; rawset(self, "__pts", ps) end
        ps[#ps + 1] = { ... }
    end,
    ClearAllPoints = function(self) rawset(self, "__pts", nil) end,
    GetPoint = function(self, index)
        local ps = rawget(self, "__pts")
        local p = ps and ps[index or 1]
        if not p then return nil end
        return p[1], p[2], p[3], p[4], p[5]
    end,
    SetSize = function(self, w, h) self.__w, self.__h = w, h end,
    SetWidth = function(self, w) self.__w = w end,
    GetWidth = function(self) return self.__w or 0 end,
    SetHeight = function(self, h) self.__h = h end,
    GetHeight = function(self) return self.__h or 0 end,
    SetText = function(self, s) self.__text = tostring(s) end,
    GetText = function(self) return self.__text or "" end,
    CreateFontString = function(self, ...) return Mock("FontString") end,
    CreateTexture = function(self, ...) return Mock("Texture") end,
    Enable = function(self) self.__enabled = true end,
    Disable = function(self) self.__enabled = false end,
    -- CheckButton：真客户端 SetChecked 收布尔（历史 1/nil 也认），GetChecked 现代回布尔。
    SetChecked = function(self, v) self.__checked = not not v end,
    GetChecked = function(self) return self.__checked == true end,
    SetFontObject = function(self, f) self.__font = f end,
    SetTextColor = function(self, r, g, b, a) self.__color = { r, g, b, a } end,
    SetJustifyH = function(self, h) self.__justifyH = h end,
    SetMultiLine = function(self, v) self.__multiline = not not v end,
    SetWordWrap = function(self, v) self.__wordwrap = not not v end,
    SetFrameStrata = function(self, s) self.__strata = s end,
    SetToplevel = function(self, v) self.__toplevel = not not v end,
    SetMovable = function(self, v) self.__movable = not not v end,
    SetUserPlaced = function(self, v) self.__userplaced = not not v end,
    EnableMouse = function(self, v) self.__mouse = not not v end,
    EnableMouseWheel = function(self, v) self.__mousewheel = not not v end,
    RegisterForDrag = function(self, button) self.__drag = button end,
    StartMoving = function(self) self.__moving = true end,
    StopMovingOrSizing = function(self) self.__moving = false end,
    SetAutoFocus = function(self, v) self.__autofocus = not not v end,
    ClearFocus = function(self) self.__focused = false end,
    SetScrollChild = function(self, child) self.__scrollchild = child end,
    SetVerticalScroll = function(self, v) self.__vscroll = v end,
    UnregisterAllEvents = function(self) self.__events = {} end,
    -- 真客户端 RegisterEvent 遇到不认识的事件名直接 error("Attempt to register unknown event")，
    -- 替身必须照做：否则"事件名写错/本版本已删该事件"这类事故离线永远复现不了
    -- （Core 的诊断帧正是靠逐个 pcall 注册，把 12.x 已移除的 AUCTION_HOUSE_ERROR 跳过去）。
    RegisterEvent = function(self, event)
        if AH_KNOWN_EVENTS[event] ~= true then
            error(('Attempt to register unknown event "%s"'):format(tostring(event)))
        end
        self.__events[event] = true
    end,
}

-- 名单只收 AHSnipe 真正注册过的事件名（逐个对着 12.x 客户端在用的接口写的）。
-- AUCTION_HOUSE_ERROR 故意不在名单里：12.x 已移除，Core 用 pcall 兜着，替身这里必须抛。
AH_KNOWN_EVENTS = {
    ADDON_LOADED = true,
    AUCTION_HOUSE_SHOW = true, AUCTION_HOUSE_CLOSED = true,
    AUCTION_HOUSE_THROTTLED_SYSTEM_READY = true,
    COMMODITY_SEARCH_RESULTS_ADDED = true, COMMODITY_SEARCH_RESULTS_RECEIVED = true,
    COMMODITY_SEARCH_RESULTS_UPDATED = true, ITEM_SEARCH_RESULTS_UPDATED = true,
    ITEM_KEY_ITEM_INFO_RECEIVED = true, ITEM_DATA_LOAD_RESULT = true,
    COMMODITY_PRICE_UPDATED = true, COMMODITY_PRICE_UNAVAILABLE = true,
    COMMODITY_PURCHASE_SUCCEEDED = true, COMMODITY_PURCHASE_FAILED = true,
    COMMODITY_PURCHASED = true, ITEM_PURCHASED = true,
}

UIParent = Mock("UIParent")
NAMED = {}
function CreateFrame(kind, name, parent, template)
    local f = Mock(name or kind)
    f.__type = kind
    -- 模板成员按真客户端给：BasicFrameTemplate 系列确有 TitleText（FontString），
    -- 无模板/旧模板的帧读它就是 nil，产品代码的自绘回退分支据此走。
    if template and string.find(template, "BasicFrameTemplate", 1, true) then
        f.TitleText = Mock("TitleText")
    end
    if name then NAMED[name] = f end
    return f
end
-- ===== 唯一的时钟夹具：可推进的延时回调队列（整个 runner 共用，冒烟也算在内）=====
-- 旧版这里 NewTimer/After 直接把回调丢掉（"不执行"），于是所有"到点才跑"的分支离线一次都没执行过：
-- BUY_RETRY 重试、报价/竞价/购买三类独立超时、轮次延时与闸门延时——SMOKE-OK 购买链路当时只覆盖了
-- 同步事件栈那一段。扫描器夹具另有一套能推进的队列，两条路径因此不是同一份语义。
-- 现在全 runner 只剩这一份队列：Advance(seconds) 按到期时刻依次真的执行回调，now 跟着走。
now = 100                       -- 夹具时钟；要挪帧只许调 Advance，别直接改这个值
timers = {}
function GetTime() return now end
local function Schedule(delay, cb)
    local timer = { at = now + delay, delay = delay, cb = cb, cancelled = false }
    timer.Cancel = function(self) self.cancelled = true end
    timers[#timers + 1] = timer
    return timer
end
C_Timer = { NewTimer = Schedule, After = Schedule }
function Advance(seconds)
    local target = now + seconds
    while true do
        local chosen = nil
        for _, t in ipairs(timers) do
            if not t.cancelled and t.at <= target and (not chosen or t.at < chosen.at) then
                chosen = t
            end
        end
        if not chosen then break end
        now = chosen.at
        chosen.cancelled = true
        chosen.cb()
    end
    now = target
end
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

    # 3) 一次 commodity 购买全流程（单请求两段式 + 事件驱动）
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

    # 3b) 延时分支：报价一直不来，3 秒超时计时器必须自己把这一单收尾（旧夹具丢了回调，这段从没跑过）
    try:
        silent = lua.table_from({
            "itemID": 237364, "name": "没人答的矿石", "threshold": 156000,
            "isCommodity": True, "totalQuantity": 1, "totalPrice": 148900,
            "matched": lua.table_from([lua.table_from({"unitPrice": 148900, "quantity": 1})]),
        })
        if not addon.BuyQueue.Buy(silent):
            return fail("smoke:buytimeout", "第二单应被受理")
        g.Advance(2.5)                      # 还没到点：不许提前结算、也不许提前释放通道
        if not addon.BuyQueue.IsBusy():
            return fail("smoke:buytimeout", "报价超时计时器提前到点了")
        g.Advance(1)
        if addon.BuyQueue.IsBusy():
            return fail("smoke:buytimeout", "报价 3s 超时到点后购买通道仍被占着：延时回调没执行")
    except Exception as e:
        return fail("smoke:buytimeout", str(e))
    print("SMOKE-OK 延时回调（推进时钟后超时分支真的执行）")

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

    # 5) 可购买列表：命中加行 → 点【立即购买】买入队首（新鲜闸门内利润最大的一条）→ 成功回调删行
    try:
        def fake_buy(hit):
            buys.append(int(hit["itemID"]))
            return True
        buys = []
        addon.BuyQueue.Buy = fake_buy
        # 列表行必须对应"仍在监控中"的物品（List.Add 会校验），先把它俩加进监控
        addon.Config.AddItem(237362, "旧")
        addon.Config.AddItem(237363, "新")
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
        # 两条都新鲜 → 利润大的 237362（门槛差 56000×3）抢过更新鲜但只有 1 个的 237363
        if int(addon.SnipeList.GetFront()["itemID"]) != 237362:
            return fail("smoke:list", "队首应是利润更大的 237362")
        if not addon.SnipeList.TriggerBuy():
            return fail("smoke:list", "TriggerBuy 未受理")
        if buys != [237362]:
            return fail("smoke:list", f"应买队首，实际 {buys}")
        addon.SnipeList.OnBuyFinished(lua.table_from(
            {"itemID": 237362, "bought": 1, "failed": 0,
             "rowKeys": lua.table_from(["C:237362:100000"]), "reason": "购买成交"}))
        if addon.SnipeList.RowCount() != 1:
            return fail("smoke:list", "成交应删该件参与行并保留另一行")
        if int(addon.SnipeList.GetFront()["itemID"]) != 237363:
            return fail("smoke:list", "剩下的应是 237363")
    except Exception as e:
        return fail("smoke:list", str(e))
    print("SMOKE-OK 可购买列表（加行/排序/立即购买/结局删行）")

    # 6) 推进时钟：扫描轮次的全部延时分支（轮间歇、单件 10s 超时、静默 2s、闸门唤醒）
    #    第一次在离线真的执行 —— 以前这些回调被夹具丢了，冒烟只走到同步事件栈。
    #    替身里 SendSearchQuery 是空函数、永远不会有回包，所以推进之后必定是"问了没答"：
    #    超时计时器到点执行 → 盲窗起点被盖上（Scanner.BlindFrom 由 0 变正），这就是延时真的跑了的凭据。
    try:
        if float(addon.Scanner.BlindFrom()) != 0:
            return fail("smoke:advance", "开局不该有盲窗起点")
        for _ in range(3):
            g.Advance(6)
        if float(addon.Scanner.BlindFrom()) <= 0:
            return fail("smoke:advance",
                        "推进 18 秒后超时计时器仍未执行（盲窗起点还是 0）：延时回调没跑")
    except Exception as e:
        return fail("smoke:advance", str(e))
    print("SMOKE-OK 推进时钟：扫描轮次延时分支执行（18 秒 / 多轮）")

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


# ------------------------------------------------------------------------
# 第二十七轮修复的回归用例（Bug1 残留行 / Bug2 pendingInfo / Perf4 空转 / Perf7 日志裁剪）
# ------------------------------------------------------------------------
def load_config_panel(fail_request=False):
    """独立 mock 环境：真实 Config + 真实 ConfigPanel，物品加载请求可计数、可令其报错。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    lua.execute(
        "AHSnipeDB = { interval = 0, discount = 0.65, diagAH = false,"
        " paused = false, items = {}, logs = {} }"
        " AHSnipeDB.items[101] = { enabled = true }"
    )
    g.AHSnipe = lua.table_from({})
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    g.AHSnipe.Config.Init()
    g.AHSnipe["PriceCluster"] = lua.table_from({})
    lua.execute("""
        request_calls = 0
        C_Item.RequestLoadItemDataByID = function(itemID)
            request_calls = request_calls + 1
            %s
        end
    """ % ("error('mock：加载请求失败')" if fail_request else "return"))
    with open(CONFIG_PANEL_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    return lua, g.AHSnipe.ConfigPanel, g


def test_snipe_orphan_rows_pruned():
    """监控被移除/停用后，列表里残留的行必须一起消失（否则仍能被【立即购买】点到）。"""
    lua, sc, lst = load_scanner_flow()
    g = lua.globals()
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 99))
    if lst.RowCount() != 1:
        return fail("snipe_orphan_rows_pruned", "测试前应有一行")
    lua.execute("""
        monitored = false
        local cfg = AHSnipe.Config
        cfg.GetItem = function(id) if monitored then return {itemID=id, enabled=true} end end
        cfg.GetItems = function()
            if monitored then return {{itemID=101, enabled=true}} end
            return {}
        end
    """)
    sc.Start()          # 队列为空的一轮：轮次收尾前先把已失效监控的残留行清掉
    g.Advance(0.5)
    if lst.RowCount() != 0:
        return fail("snipe_orphan_rows_pruned",
                    f"监控已移除后残留行仍留在列表里，实际 {lst.RowCount()} 行")
    ok("snipe_orphan_rows_pruned")


def test_list_rejects_unmonitored_hit():
    """查询在途期间被停用/移除的物品，迟到回包不许重新入列（并顺手清掉残留行）。"""
    lua, lst, logs, pings = load_snipe_list()
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 99))
    if lst.RowCount() != 1:
        return fail("list_rejects_unmonitored", "测试前应有一行")
    lua.execute("monitored = false")     # 模拟用户在查询在途期间停用/删除了该物品
    lst.Add(make_hit(lua, 101, 7000, 1, 7000, 100))
    if lst.RowCount() != 0:
        return fail("list_rejects_unmonitored",
                    f"已停用物品的迟到回包不应复活列表行，实际 {lst.RowCount()} 行")
    ok("list_rejects_unmonitored")


def test_config_panel_info_retry():
    """加载请求报错（pcall 失败）时必须清掉 pendingInfo，重开面板才能再试一次。"""
    lua, panel, g = load_config_panel(fail_request=True)
    panel.Open()
    panel.Close()
    panel.Open()
    calls = int(g.request_calls)
    if calls < 2:
        return fail("config_panel_info_retry",
                    f"重开面板应重新发起物品加载请求，实际只发了 {calls} 次")
    ok("config_panel_info_retry")


def test_scanner_idle_gap():
    """一个可查物品都没有时轮次是空转：间隔取 max(配置, 2s)，不再 0.2s 高频空跑。"""
    lua, sc, _ = load_scanner_parallel()
    g = lua.globals()
    lua.execute("parallel_items = {}; roundInterval = 0")
    sc.Start()
    g.Advance(0.1)
    delays = [float(g.scan_timers[i]["delay"]) for i in range(1, len(g.scan_timers) + 1)]
    if not any(abs(d - 2) < 1e-9 for d in delays):
        return fail("scanner_idle_gap", f"空转轮间歇应为 2s，实际计时器 {delays}")
    if any(abs(d - 0.2) < 1e-9 for d in delays):
        return fail("scanner_idle_gap", f"空转不应按 0.2s 高速轮询，实际计时器 {delays}")
    ok("scanner_idle_gap")


def test_config_log_trim():
    """日志裁剪：始终只保留最近 200~240 条，且保留的是连续的最新一段。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    lua.execute("AHSnipeDB = { interval = 0, discount = 0.65, diagAH = false,"
                " paused = false, items = {}, logs = {} }")
    g.AHSnipe = lua.table_from({})
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lua.execute(f.read())
    cfg = g.AHSnipe.Config
    for i in range(600):
        cfg.Log("m%d" % i)
    logs = g.AHSnipeDB.logs
    n = len(logs)
    if not (200 <= n <= 240):
        return fail("config_log_trim", f"日志应保留 200~240 条，实际 {n}")
    tail = [str(logs[i]).split("] ", 1)[-1] for i in range(1, n + 1)]
    expect = ["m%d" % i for i in range(600 - n, 600)]
    if tail != expect:
        return fail("config_log_trim",
                    f"裁剪后应保留最近连续的 {n} 条，实际末尾 {tail[-1]}、首条 {tail[0]}")
    ok("config_log_trim")


def test_delayed_callbacks_run_after_advance():
    """runner 只有一份计时器夹具，而且延时回调必须真的执行。

    MOCK_ENV 里 C_Timer.NewTimer/After 原来是 `return { Cancel = ... }` / 空函数——回调直接丢，
    于是 BUY_RETRY 重试、报价 3s/竞价 2s/购买 10s 三类独立超时在离线一次都没跑过，
    "SMOKE-OK 购买链路"覆盖到的只是同步事件栈，到点的分支全是死的（扫描夹具另有一套可推进的队列，
    两条路径连语义都不一样）。下面钉住最短的两条延时路径：没到点不许提前结算，到点必须结算。
    """
    cases = (
        (False, 2, "购买无响应", "竞价 2s 超时"),
        (True, 3, "购买超时（未收到价格响应）", "报价 3s 超时"),
    )
    for is_commodity, limit, reason_part, label in cases:
        lua, bq, finished = load_buy_queue()
        g = lua.globals()
        hit = make_hit_rows(lua, 101, [(50000, 2, 1234)], 100, commodity=is_commodity)
        if not bq.Buy(hit):
            return fail("delayed_callbacks_run", f"{label}：购买应被受理")
        if bq.IsBusy() is not True or finished:
            return fail("delayed_callbacks_run", f"{label}：发起后应在途且未结算")
        g.Advance(limit - 0.5)                # 还差半秒：计时器不许提前到点
        if not bq.IsBusy() or finished:
            return fail("delayed_callbacks_run", f"{label}：没到点就结算了 {finished}")
        g.Advance(1)
        if len(finished) != 1:
            return fail("delayed_callbacks_run",
                        f"{label}：到点后延时回调应把这次请求收尾，实际结算 {len(finished)} 次")
        reason = str(finished[0]["reason"])
        if reason_part not in reason:
            return fail("delayed_callbacks_run", f"{label}：收尾原因应含『{reason_part}』，实际 {reason}")
        if bq.IsBusy() is not False:
            return fail("delayed_callbacks_run", f"{label}：超时收尾后不应仍占着购买通道")
    ok("delayed_callbacks_run")


def test_double_rejects_invented_api():
    """替身不许发明 API：未知方法名、未知事件名都要当场抛错（口径照 run_gql_tests.py 的 CreateFrame）。

    旧版 __index 给任何未知键造一个"返回 Mock 的函数"：产品代码调了真客户端没有的接口，
    离线全绿、进游戏只是静默无反应——"离线复现不了"的根子就在这。RegisterEvent 同理，
    真客户端遇到不认识的事件名是直接 error 的。
    """
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)

    def raises(snippet):
        try:
            lua.execute(snippet)
            return False
        except Exception:
            return True

    checks = [
        (raises('local f = CreateFrame("Frame") return f:GetFrameLevel()'),
         "未知方法名必须抛错（AHSnipe 不用这个方法，夹具里也不该有）"),
        (raises('local f = CreateFrame("Frame") return f:SomeTypoMethod()'),
         "拼错的方法名必须抛错（旧版会静默返回一个 Mock）"),
        (raises('local f = CreateFrame("Frame") return f:RegisterEvent("AUCTION_HOUSE_ERROR")'),
         "12.x 已移除的事件名必须抛错（Core 的诊断帧正是靠逐事件 pcall 跳过它）"),
        (not raises('local f = CreateFrame("Frame") return f:RegisterEvent("COMMODITY_PURCHASED")'),
         "名单内的真实事件名要能注册"),
        (not raises('local f = CreateFrame("Frame") f.buyBtn = 1 return f.buyBtn'),
         "插件自挂的小写便签字段要照 userdata 那样读得到（不许跟着报错）"),
        (lua.execute('local f = CreateFrame("Frame") return f.someTypoField') is None,
         "小写未知键读出来是 nil（和真客户端的帧 userdata 一致），调用它才由 Lua 报『call a nil value』"),
        (not raises('local f = CreateFrame("Frame", "T", UIParent, "BasicFrameTemplateWithInset")'
                    ' return f.TitleText'),
         "BasicFrameTemplate 系列的模板成员 TitleText 要按真客户端给"),
        (raises('local f = CreateFrame("Frame") return f.TitleText'),
         "没有该模板的帧读 TitleText 属于调了不存在的东西，要抛错而不是凭空造一个"),
    ]
    # 锚点必须按真客户端形态回参数：GetPoint 漏取一位就是把 relativePoint 当成了 x
    lua.execute('listFrame = CreateFrame("Frame") listFrame:SetPoint("CENTER", UIParent, "CENTER", 240, 40)')
    point, relative_to, relative_point, x, y = lua.execute(
        'return listFrame:GetPoint()')
    checks.extend([
        (str(point) == "CENTER" and relative_to is not None and str(relative_point) == "CENTER"
         and int(x) == 240 and int(y) == 40,
         f"GetPoint 要回 point, relativeTo, relativePoint, x, y 五个值，实际 {(point, relative_to, relative_point, x, y)}"),
    ])
    lua.execute('bareFrame = CreateFrame("Frame")')
    checks.append((lua.execute('return bareFrame:GetPoint()') is None,
                   "没钉过锚点的帧 GetPoint 要回 nil（真客户端如此），产品代码的 if point then 才有意义"))
    # 以下是"替身按文档/记忆写的返回值"，本地无从证实，全部标出来等游戏内实测；
    # 实测若与这里不同，改的是替身+相应产品判断，不是把这条断言删掉。
    #   # 需游戏内实测：FontString 从未 SetText 过时 GetText 回 nil 还是 ""（替身按 ""，
    #   #   ConfigPanel 的 tonumber(self:GetText()) 两种都安全，但按钮文案比较 str(...) 时会不同）
    #   # 需游戏内实测：CheckButton 没被点过时 GetChecked 回 false 还是 nil（替身按 false）
    #   # 需游戏内实测：12.x 的 BasicFrameTemplateWithInset 是否仍提供 TitleText 成员
    #   #   （产品代码三处 if f.TitleText then 都写了自绘回退分支，取不到也不会崩，只是标题位置变）
    checks.append((str(lua.execute('return CreateFrame("FontString"):GetText()')) == "",
                   '# 需游戏内实测：未 SetText 的 GetText 替身按 ""，改了要同步这里'))
    checks.append((lua.execute('return CreateFrame("CheckButton"):GetChecked()') is False,
                   '# 需游戏内实测：未点过的 GetChecked 替身按 false，改了要同步这里'))
    errors = [message for passed, message in checks if not passed]
    if errors:
        return fail("double_rejects_invented_api", "；".join(errors))
    ok("double_rejects_invented_api")


def test_every_defined_case_is_registered_in_the_runner():
    """静态守卫（照 run_gql_tests.py 同一条）：定义了 `def test_*` 却没写进下面那份人手维护的
    `tests = [...]` 清单，这条就一行都不跑、而且一句错都不报——清单靠人加，改名时最容易漏。
    只认清单那一段，不能拿"名字在文件里出现过两次"当证据：docstring 和 fail() 消息里
    到处写着自己的名字，那样 orphan 照样漏过去。"""
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
        test_screenshot_scene,
        test_bait_cluster_together,
        test_single_listing,
        test_empty_listings,
        test_tie_takes_higher_cluster,
        test_uniform_price,
        test_is_bait_like,
        test_config_migration,
        test_display_name_cache,
        test_listing_rows_from_hit,
        test_row_reconcile,
        test_row_cap,
        test_profit_ranking,
        test_profit_fresh_gate,
        test_profit_merged_key,
        test_profit_market_spread,
        test_snipe_list_scroll,
        test_snipe_list,
        test_snipe_list_buy,
        test_buy_button_before_list_render,
        test_buy_chat_only_reports_outcome,
        test_buqueue_notifies_list,
        test_buy_outcome_deletes_row,
        test_merge_request,
        test_veto_rewrites_qty,
        test_veto_not_reversible_drops_rows,
        test_blind_window_grays_buy_button,
        test_scanner_blind_window,
        test_blind_window_rolls_only_reanswered_rows,
        test_pause_grays_rows_until_reanswered,
        test_silence_stamped_by_timeout_not_only_by_answers,
        test_scanner_watch_reequeries_bought_item,
        test_scanner_yields_to_purchase,
        test_scanner_spill_price,
        test_build_round_queue,
        test_scanner_commodity_no_event_guard,
        test_scanner_item_no_event_guard,
        test_scanner_reopen_no_event_guard,
        test_scanner_complete_empty_clears_rows,
        test_scanner_timeout_keeps_rows,
        test_scanner_late_result_refreshes_rows,
        test_scanner_market_anchor,
        test_scanner_multi_tier,
        test_snipe_list_front_after_add,
        test_scanner_parallel_capacity_refill,
        test_scanner_parallel_send_gap,
        test_scanner_parallel_mixed_results,
        test_scanner_parallel_gate_ready,
        test_scanner_parallel_send_timestamps,
        test_scanner_parallel_independent_timeout,
        test_scanner_parallel_round_drain_gap,
        test_scanner_parallel_hit_not_freshen_peer,
        test_scanner_parallel_noid_unproven,
        test_scanner_parallel_noid_last_pending,
        test_scanner_parallel_noid_wake_proven,
        test_scanner_parallel_partial_silence,
        test_scanner_parallel_stop_cleanup,
        test_scanner_parallel_pause_cleanup,
        test_scanner_parallel_paused_manual_round,
        test_scanner_parallel_synchronous_results,
        test_scanner_parallel_refresh_exclusive,
        test_scanner_parallel_refresh_stop_cleanup,
        test_scanner_parallel_own_late_commodity,
        test_scanner_parallel_own_late_item,
        test_scanner_parallel_metrics,
        test_scanner_parallel_metrics_timeouts,
        test_scanner_parallel_metrics_gate_and_silence,
        test_scanner_parallel_info_wait,
        test_scanner_parallel_refresh_waits_for_slot,
        test_snipe_orphan_rows_pruned,
        test_list_rejects_unmonitored_hit,
        test_config_panel_info_retry,
        test_scanner_idle_gap,
        test_config_log_trim,
        test_delayed_callbacks_run_after_advance,
        test_double_rejects_invented_api,
        test_every_defined_case_is_registered_in_the_runner,
    ]
    for t in tests:
        t()
    smoke_test()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
