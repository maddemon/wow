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
SCANNER_PATH = os.path.join(PLUGIN_DIR, "Scanner.lua")
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
                  market=None):
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
    label = str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()"))
    if "+150000" not in label:
        return fail("profit_market_spread", f"按钮应显示主簇利差 150000，实际 {label}")
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
    if not label.startswith("立即购买") or "+1000000" not in label:
        return fail("snipe_list_buy", f"按钮应带队首利润，实际 {label}")
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
    if not str(lua.eval("NAMED.AHSnipeListFrame.buyBtn:GetText()")).startswith("立即购买"):
        return fail("snipe_list_buy", "空列表按钮应回到立即购买文案")
    ok("snipe_list_buy")


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


def load_scanner():
    """独立环境加载 Scanner.lua（供纯函数 BuildRoundQueue 单测）。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    lua.execute(MOCK_ENV)
    g.AHSnipe = lua.table_from({
        "Config": lua.table_from({
            "Log": lambda *a: None,
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
    lua, sc = load_scanner()
    g = lua.globals()
    g.commodity_mode = is_commodity
    lua.execute("""
        now = 100
        GetTime = function() return now end
        local timers = {}
        C_Timer.NewTimer = function(delay, cb)
            local timer = { at = now + delay, cb = cb, cancelled = false }
            timer.Cancel = function(self) self.cancelled = true end
            timers[#timers + 1] = timer
            return timer
        end
        C_Timer.After = C_Timer.NewTimer
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
        -- 记录锚点参数：布局回归（首行内缩、名称右界）只能从 SetPoint 实参看出来。
        -- 必须 rawget：本 mock 的 __index 会给未知键自动造一个函数，直接读 t.__pts 永远非 nil
        if k == "SetPoint" then return function(self, ...)
            local ps = rawget(t, "__pts")
            if not ps then ps = {}; rawset(t, "__pts", ps) end
            ps[#ps + 1] = { ... }
        end end
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
        test_config_migration,
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
        test_buqueue_notifies_list,
        test_buy_outcome_deletes_row,
        test_merge_request,
        test_build_round_queue,
        test_scanner_commodity_no_event_guard,
        test_scanner_item_no_event_guard,
        test_scanner_reopen_no_event_guard,
        test_scanner_complete_empty_clears_rows,
        test_scanner_timeout_keeps_rows,
        test_scanner_late_result_refreshes_rows,
        test_scanner_market_anchor,
    ]
    for t in tests:
        t()
    smoke_test()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
