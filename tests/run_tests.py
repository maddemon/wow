# AHSnipe 测试运行器：PriceCluster 单测 + Lua 语法检查
# 用法：python tests/run_tests.py            —— 运行单测
#       python tests/run_tests.py --syntax    —— 额外对全部 Lua 文件做 loadfile 语法检查
import os
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "AHSnipe")
PRICE_CLUSTER_PATH = os.path.join(PLUGIN_DIR, "PriceCluster.lua")

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
    ]
    for t in tests:
        t()
    if "--syntax" in sys.argv:
        syntax_check()
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith('syntax')])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
