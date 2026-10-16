# AHSnipe 行=挂单档位 与单条购买 实施计划（第二十一轮）

> **For agentic workers**: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把可购买列表从"一物品一行 + 分块串行购买"改为"一个单价档位一行 + 一次点击一个请求 + 任何结局只删被点那一行"，并修掉聚焦排他把其余物品整轮跳过的饥饿问题。

**Architecture:** SnipeList 持有"行"这一新数据对象（`BuildRows` 把命中摊成档位行并按 key 对齐刷新/删除），BuyQueue 退化为"单请求两段式"（删除 `BuildCommodityChunks`/`chunks`/`index`/`deferred` 整套分块机制，透传 `rowKey`+`reason`），Scanner 把"本轮查谁"抽成纯函数 `BuildRoundQueue`（聚焦项排前 + 非聚焦项按 15 秒补查预算入列）。

**Tech Stack:** WoW retail 12.x Lua addon（`BasicFrameTemplateWithInset`、`UIPanelScrollFrameTemplate`、`C_Timer`、`C_AuctionHouse`）；测试为 Python + lupa 直接执行 Lua 源（`tests/run_tests.py`，`MOCK_ENV` 提供 WoW 环境 mock）。

## Global Constraints

- **购买 API 发送时机（用户游戏内实证，绝不可违反）**：`StartCommoditiesPurchase`/`ConfirmCommoditiesPurchase`/`PlaceBid` 只能在**硬件点击栈内**或**搜索/报价结果事件栈内**直调；从 `C_Timer` 回调、购买生命周期事件栈（`COMMODITY_PURCHASE_SUCCEEDED`/`ITEM_PURCHASED`）、报价否决事件栈发出的购买消息会被客户端**静默丢弃**。因此本轮**不实现任何自动购买、自动重试、连续补发**，也不得引入外部键鼠注入。
- 未实证的 12.x API 形态必须 `pcall` 包裹，且同类失败每会话最多记一条日志。
- 门槛复核保留：`COMMODITY_PRICE_UPDATED` 的均价 > `threshold` 即 `CancelCommoditiesPurchase` 放弃（下单前那次复核是全链路唯一价格保护）。
- 超时参数不变：`COMMODITY_PRICE_TIMEOUT = 3`、`COMMODITY_TIMEOUT = 10`、`BID_TIMEOUT = 2`。
- 窗口文案与日志一律中文；聊天框报告沿用现有 `购买成功/购买失败` 格式。
- 测试门禁：`PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax` 必须全绿、exit 0。
- 部署方向恒为 **仓库 → 游戏**（`D:\Repos\wow\AHSnipe` → `D:\games\World of Warcraft\_retail_\Interface\AddOns\AHSnipe`），覆盖前先做时间戳备份。
- 提交信息用中文 conventional commits；仓库 `core.autocrlf=true`，"LF will be replaced by CRLF" 警告为良性。
- 本轮不做：行内按钮、"忽略"概念、购买结果保留（灰显/TTL）、按价值排序队首、同档位多条挂单拆行。

## File Structure

| 文件 | 职责变化 |
|---|---|
| `AHSnipe/SnipeList.lua` | 行数据对象（`BuildRows`/`NewerThan`/`DropRow`）+ 按 key 对齐的入列刷新 + 每轮一次提示音 + `MAX_ROWS=60` 淘汰 + 滚动容器 + 队首行的单条购买入口 + 按 `rowKey` 删行 |
| `AHSnipe/BuyQueue.lua` | 单请求两段式购买（commodity 一次报价→确认；非 commodity 一次 `PlaceBid`）；删除分块与 `deferred`；`summary.rowKey`/`summary.reason` 透传给列表 |
| `AHSnipe/Scanner.lua` | 新增 `lastQueried` + 纯函数 `BuildRoundQueue`（聚焦排前 + 15 秒补查预算）；聚焦日志改口径；`RefreshHit` 与命中评估逻辑不变 |
| `tests/run_tests.py` | 删 3 个 chunk 测试；新增 `test_listing_rows_from_hit`/`test_row_reconcile`/`test_row_cap`/`test_snipe_list_scroll`/`test_buy_outcome_deletes_row`/`test_build_round_queue`；改写 `test_snipe_list`/`test_snipe_list_buy`/`test_buqueue_notifies_list`/`smoke_test` |
| `docs/…` | 本轮记录写入 `docs/tasks/2026-10-13-ahsnipe-plan.md`；spec/plan 保持同步 |

---

## Task 1: SnipeList 行模型（一行 = 一个单价档位）

**Files:**
- Modify: `AHSnipe/SnipeList.lua`（L1-7 文件头注释、L17 `rows` 注释、L23-26 常量、L29-35 `MinUnit`、L75-123 `Render`、L223-252 `Add`/`MarkGone`、L266-294 `TriggerBuy`）
- Modify: `tests/run_tests.py`（新增 `make_hit_rows` 助手 + 3 个测试 + 调整 `test_snipe_list`；测试清单 L566-581）

**Interfaces:**
- Consumes: `hit`（Scanner 产出的命中对象，含 `matched` 数组、`threshold`、`isCommodity`、`seenAt`/`querySendAt`/`roundStartAt`）；`AHSnipe.BuyQueue.Buy`/`IsBusy`；`AHSnipe.Scanner.RefreshHit`。
- Produces:
  - `List.BuildRows(hit) -> rows`（纯函数，行数组按单价升序）
  - 行对象 `{ key, itemID, name, unitPrice, quantity, totalPrice, threshold, isCommodity, auctionID, buyoutAmount, seenAt, querySendAt, roundStartAt }`
  - `List.NewerThan(a, b) -> boolean`（排序比较器：`seenAt` 降序，同 `seenAt` 按 `unitPrice` 升序）
  - `List.DropRow(key, reason) -> boolean`
  - `List.RowCount()`/`List.GetFront()`（`GetFront()` 现返回**行**而非 hit）
  - `List.TriggerBuy()` 交给 BuyQueue 的 hit 带 `rowKey` 字段、`matched` 只含一条挂单

- [ ] **Step 1: 写失败测试（档位摊行 + 对齐 + 上限）**

在 `tests/run_tests.py` 的 `make_hit`（L258 之后）下方新增助手与 3 个测试，并把它们加入 `__main__` 的 `tests` 清单（插在 `test_snipe_list` 之前）：

```python
def make_hit_rows(lua, item_id, pairs, seen_at, round_start=None, commodity=True):
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
    return lua.table_from({
        "itemID": item_id, "name": "物品%s" % item_id, "threshold": min(units) * 3,
        "isCommodity": commodity, "totalQuantity": sum(tup[1] for tup in pairs),
        "matched": lua.table_from(entries),
        "roundStartAt": seen_at - 3 if round_start is None else round_start,
        "querySendAt": seen_at - 1, "seenAt": seen_at,
    })


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
    # 101 的 148900 档被抢光：只删那一行，156000 行不动（同轮不重响）
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
```


def test_row_cap():
    """超过 MAX_ROWS(60) 时淘汰排序末尾（最不新鲜）的行。"""
    lua, lst, logs, pings = load_snipe_list()
    for i in range(61):
        lst.Add(make_hit(lua, 300 + i, 50000, 1, 50000, 100 + i))
    if lst.RowCount() != 60:
        return fail("row_cap", f"应保留 60 行，实际 {lst.RowCount()}")
    if int(lst.GetFront()["itemID"]) != 360:
        return fail("row_cap", "队首应是最新鲜的 360")
    ok("row_cap")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py`
Expected: FAIL — `listing_rows_from_hit` 报"两个价位应有 2 行，实际 1"（当前 Add 按 itemID 一行），`row_cap` 报 61 行。

- [ ] **Step 3: 实现行模型**

`AHSnipe/SnipeList.lua` 文件头注释（L1-7）替换为：

```lua
-- 可购买列表（替代旧狙击弹窗）。规格：docs/superpowers/specs/2026-10-16-ahsnipe-listing-rows-design.md
-- 一行 = 一个可买批次 = 一次点击一个请求：commodity 按单价档位成行（服务器报价只按数量取最便宜的
-- 若干个单位，同价位的几条挂单在请求里不可区分），非 commodity 按 auctionID 一条一行。
-- 点【立即购买】买队首（数据最新鲜；同一次命中内按单价升序 → 连点从最便宜档开始）。
-- 任何购买结局（成交/均价否决/价格不可用/超时/服务器失败）只删被点那一行，其余行不受影响。
-- 不做"记下点击稍后自动买下一条"：计时器/事件上下文的购买消息会被客户端静默丢弃（已实证）。
```

删除 `MinUnit`（L29-35），在 `MAX_ROWS` 常量区（L23-26）追加：

```lua
local ROW_H = 19
local ROW0_Y = -78     -- 第一行偏移：标题栏 + 扫描/暂停行 + 列头之下
local FOOT_H = 52      -- 底部按钮区高度
local MIN_H = 260      -- 首页高度：空列表也留出多行视野
local MAX_ROWS = 60    -- 帧池上限（每行 1 图标 + 4 FontString）：超限时淘汰排序末尾
```

`rows` 声明注释改为 `local rows = {}   -- 行数组，恒按 NewerThan 排序（[1]=队首）`；新增模块级 `local lastPingRound = nil`（提示音的轮次身份）。

在 `PlayPing` 下方新增两个纯函数：

```lua
-- 行排序：数据新鲜度降序为第一键（队首永远是刚扫到的）；同一次命中的多行按单价升序 → 连点先买最便宜档
function List.NewerThan(a, b)
    local sa, sb = a.seenAt or 0, b.seenAt or 0
    if sa ~= sb then return sa > sb end
    local ua, ub = a.unitPrice or 0, b.unitPrice or 0
    if ua ~= ub then return ua < ub end
    return (a.quantity or 0) < (b.quantity or 0)
end

-- 命中摊成档位行（纯函数，可单测）：commodity 同单价合计成一行，非 commodity 一条 auctionID 一行
function List.BuildRows(hit)
    local byKey, order = {}, {}
    for _, l in ipairs(hit.matched or {}) do
        local key
        if not l.quantity or l.quantity <= 0 then
            key = nil                                   -- 无数量的挂单不成行
        elseif hit.isCommodity then
            key = ("C:%s:%s"):format(hit.itemID, math.floor(l.unitPrice + 0.5))
        elseif l.auctionID then
            key = ("A:%s"):format(l.auctionID)
        else
            key = nil                                   -- 非 commodity 缺 auctionID 无法下单，不入列
        end
        if key then
            local row = byKey[key]
            if not row then
                row = { key = key, unitPrice = l.unitPrice, quantity = 0, totalPrice = 0,
                        auctionID = l.auctionID, buyoutAmount = l.buyoutAmount }
                byKey[key] = row
                order[#order + 1] = key
            end
            row.quantity = row.quantity + l.quantity
            row.totalPrice = row.totalPrice + l.unitPrice * l.quantity
            row.itemID = hit.itemID
            row.name = hit.name or Config.GetItemDisplayName(hit.itemID)
            row.threshold = hit.threshold
            row.isCommodity = hit.isCommodity and true or false
            row.seenAt = hit.seenAt
            row.roundStartAt = hit.roundStartAt
            row.querySendAt = hit.querySendAt
        end
    end
    local out = {}
    for _, key in ipairs(order) do out[#out + 1] = byKey[key] end
    table.sort(out, function(a, b)
        if a.unitPrice == b.unitPrice then return a.quantity < b.quantity end
        return a.unitPrice < b.unitPrice
    end)
    return out
end
```

`Render`（L75-123）内层字段改用行字段：`table.sort(rows, List.NewerThan)` 取代原 `seenAt` 比较器；`hit`→`row`；`label` 取 `row.name`；`w.unit:SetText(coin(math.floor(row.unitPrice)))`；`w.qty:SetText(tostring(row.quantity))`；`w.total:SetText(coin(row.totalPrice))`；`buyingItemID == row.itemID` 判断保持不变（Task 3 改为按 key）。

`Add`/`MarkGone`（L223-252）整体替换为：

```lua
function List.Add(hit)
    if not EnsureUI() then return end
    local fresh = List.BuildRows(hit)
    local keep = {}
    for _, r in ipairs(fresh) do keep[r.key] = r end
    local nextRows, added = {}, 0
    -- 该 itemID 的现有行按 key 对齐：命中→原地刷新（静默），新数据里没这个档位了→删行（新鲜证据）
    for _, r in ipairs(rows) do
        if r.itemID ~= hit.itemID then
            nextRows[#nextRows + 1] = r
        else
            local f = keep[r.key]
            if f then
                nextRows[#nextRows + 1] = f
                keep[r.key] = nil
            else
                Log(("除行 %s（该档位已无低于门槛挂单）"):format(r.key))
            end
        end
    end
    for _, r in ipairs(fresh) do
        if keep[r.key] then
            nextRows[#nextRows + 1] = r
            added = added + 1
            Log(("入列 %s 单价%.2fg 数量%d 总价%.2fg（数据%.1fs=排队%.1fs+回包%.1fs）"):format(
                r.itemID, (r.unitPrice or 0) / 10000, r.quantity or 0,
                (r.totalPrice or 0) / 10000,
                (r.seenAt or 0) - (r.roundStartAt or 0),
                (r.querySendAt or 0) - (r.roundStartAt or 0),
                (r.seenAt or 0) - (r.querySendAt or 0)))
        end
    end
    rows = nextRows
    table.sort(rows, List.NewerThan)
    while #rows > MAX_ROWS do
        local dropped = table.remove(rows)
        Log(("除行 %s（超过%d行上限，淘汰最旧）"):format(dropped.key, MAX_ROWS))
    end
    -- 同一轮里多个新档位只响一次（roundStartAt 即轮次身份）
    if added > 0 and hit.roundStartAt ~= lastPingRound then
        lastPingRound = hit.roundStartAt
        PlayPing()
    end
    Render()
end

-- 该 itemID 的全部行出门（扫描用新鲜数据证实整件已无低价挂单）
function List.MarkGone(itemID, reason)
    local removed, nextRows = 0, {}
    for _, r in ipairs(rows) do
        if r.itemID == itemID then
            removed = removed + 1
            Log(("除行 %s（%s）"):format(r.key, reason or "扫描证实无低价"))
        else
            nextRows[#nextRows + 1] = r
        end
    end
    if removed == 0 then return end
    rows = nextRows
    Render()
end

-- 按 key 删单行（购买结局、挂单失效都走这里）；行已被新鲜扫描删掉时静默返回 false
function List.DropRow(key, reason)
    for i, r in ipairs(rows) do
        if r.key == key then
            table.remove(rows, i)
            Log(("除行 %s（%s）"):format(key, reason or "已结束"))
            return true
        end
    end
    return false
end
```

`TriggerBuy`（L266-294）改为从行合成"单条挂单的 hit"（一次点击一个请求）；`rowKey` 字段本轮先带上、Task 3 才由 BuyQueue 回传：

```lua
-- 行 → 交给 BuyQueue 的 hit：matched 只含这一批，服务器报价就按这一档的数量取最便宜的若干个
local function RowToHit(r)
    local l
    if r.isCommodity then
        l = { unitPrice = r.unitPrice, quantity = r.quantity }
    else
        l = { unitPrice = r.unitPrice, quantity = r.quantity,
              auctionID = r.auctionID, buyoutAmount = r.buyoutAmount }
    end
    return {
        itemID = r.itemID, name = r.name, threshold = r.threshold,
        isCommodity = r.isCommodity, rowKey = r.key,
        matched = { l }, totalQuantity = r.quantity, totalPrice = r.totalPrice,
        seenAt = r.seenAt, roundStartAt = r.roundStartAt, querySendAt = r.querySendAt,
    }
end

-- 唯一购买入口：必须在按钮 OnClick 的硬件点击栈内直调（或其下游刷新回调栈内）。
function List.TriggerBuy()
    local BQ = AHSnipe.BuyQueue
    local row = rows[1]
    if not row or (BQ and BQ.IsBusy()) then return false end
    Log(("点击购买 %s %s isCommodity=%s 数量%d"):format(
        row.itemID, row.key, tostring(row.isCommodity), row.quantity or 0))
    if row.isCommodity then
        -- commodity 不做购买前刷新：刷新查询挤占节流通道且服务器报价本身就是实时价
        if BQ.Buy(RowToHit(row)) then
            buyingItemID = row.itemID
            Render()
        end
    else
        -- 非 commodity 的 auctionID 会过期：先刷新最新挂单，回调栈内直调购买（不得包计时器）
        AHSnipe.Scanner.RefreshHit(RowToHit(row), function(fresh, reason)
            if fresh then
                local hit = RowToHit(row)
                hit.matched = { fresh.matched[1] }   -- Task 3 改为按 auctionID 精确取本行挂单
                if BQ.Buy(hit) then
                    buyingItemID = row.itemID
                    Render()
                end
            else
                Log(("购买前刷新失败 %s：%s"):format(row.itemID, reason))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(row.name, reason))
            end
        end)
    end
    return true
end
```

同步调整 `test_snipe_list`（L261-278）：原地刷新的第二次 `Add` 必须用**同单价**才命中同 key，改为
`lst.Add(make_hit(lua, 101, 50000, 9, 450000, 98))`，其余断言不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax`
Expected: `17/17 单测通过`、`SMOKE-OK` 全部行、exit 0（chunk 单测与 `test_buqueue_notifies_list` 不受影响：BuyQueue 收到的仍是带 `matched` 的 hit）。

- [ ] **Step 5: 提交**

```bash
git add AHSnipe/SnipeList.lua tests/run_tests.py
git commit -m "refactor: 可购买列表改为单价档位行（同价合计、按 key 对齐刷新、每轮一次提示音、60 行上限）"
```

---

## Task 2: 列表滚动容器（窗口固定高度）

**Files:**
- Modify: `AHSnipe/SnipeList.lua`（L46-58 `EnsureRowPool`、L75-123 `Render`、L125-221 `EnsureUI`、L23-26 常量）
- Modify: `tests/run_tests.py`（新增 `test_snipe_list_scroll` + 清单）

**Interfaces:**
- Consumes: Task 1 的行数组与 `List.NewerThan`。
- Produces: 命名帧 `AHSnipeListScroll`（`UIPanelScrollFrameTemplate`）与 `AHSnipeListContent`（行池宿主）；窗口高度恒为 `MIN_H=260`；`f.scrollY` 记录当前滚动偏移。

- [ ] **Step 1: 写失败测试**

在 `test_row_cap` 下方新增（并加入 `tests` 清单，位置紧随 `test_row_cap`）：

```python
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
    ok("snipe_list_scroll")
```

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py`
Expected: FAIL — `KeyError: 'AHSnipeListScroll'`/内容帧不存在（或高度随行数增长到 300+）。

- [ ] **Step 2: 建滚动容器**

`EnsureUI` 中，在列头 `headers` 循环**之前**插入（列头仍钉在窗口上，行改钉在内容帧上）：

```lua
        -- 滚动区：窗口高度固定，行多时滚动而非把窗口拉长（列表常驻主界面，尺寸不该跳动）
        local scroll = CreateFrame("ScrollFrame", "AHSnipeListScroll", f, "UIPanelScrollFrameTemplate")
        scroll:SetPoint("TOPLEFT", f, "TOPLEFT", 4, ROW0_Y)
        scroll:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -26, FOOT_H)
        local content = CreateFrame("Frame", "AHSnipeListContent", f)
        content:SetWidth(392)
        scroll:SetScrollChild(content)
        f.scroll, f.content = scroll, content
        scroll:EnableMouseWheel(true)
        scroll:SetScript("OnMouseWheel", function(_, delta)
            local y = math.max(0, math.min(ContentH() - VIS_H, (f.scrollY or 0) - delta * ROW_H))
            f.scrollY = y
            scroll:SetVerticalScroll(y)
        end)
```

常量区（Task 1 的 L23-27 附近）补两行，并把行高/滚动区高度计算抽成模块级函数（`Render` 与滚轮回调都要用）：

```lua
local VIS_H = MIN_H - (-ROW0_Y) - FOOT_H   -- 滚动视口高度：260-78-52 = 130 ≈ 6.8 行

local function ContentH()
    return math.max(VIS_H, #rows * ROW_H)
end
```

- [ ] **Step 3: 行池与渲染改挂内容帧**

`EnsureRowPool` 的三处 `f:CreateTexture`/`f:CreateFontString` 改为 `f.content:`（池仍存 `f.rowFS`）；
`Render` 内锚点从窗口换成内容帧、y 只跟行号有关：

```lua
            local y = -((i - 1) * ROW_H)
            ...
            w.name:ClearAllPoints(); w.name:SetPoint("LEFT", f.content, "TOPLEFT", 30, y)
            ...
            w.icon:ClearAllPoints(); w.icon:SetPoint("LEFT", f.content, "TOPLEFT", 13, y + 4)
            ...
            w.unit:ClearAllPoints(); w.unit:SetPoint("LEFT", f.content, "TOPLEFT", 190, y)
            w.qty:ClearAllPoints(); w.qty:SetPoint("LEFT", f.content, "TOPLEFT", 262, y)
            w.total:ClearAllPoints(); w.total:SetPoint("LEFT", f.content, "TOPLEFT", 300, y)
```

`Render` 末尾的高度与滚动钳制替换原 `f:SetHeight(math.max(MIN_H, ...))`：

```lua
    f.lastRows = #rows
    f:SetHeight(MIN_H)          -- 窗口高度恒定：行数变化只体现在内容高度上
    local ch = ContentH()
    f.content:SetHeight(ch)
    -- 行数变少时把越界的滚动偏移拉回来（自行算可视范围，不依赖 GetVerticalRange 的返回形态）
    local y = math.max(0, math.min(f.scrollY or 0, ch - VIS_H))
    f.scrollY = y
    pcall(f.scroll.SetVerticalScroll, f.scroll, y)
```

`EnsureUI` 里 `f:SetSize(430, MIN_H)` 保持；`emptyFS` 仍钉在窗口 `TOPLEFT 30, ROW0_Y`（盖在滚动区之上）。

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax`
Expected: `18/18 单测通过`、exit 0。

- [ ] **Step 5: 提交**

```bash
git add AHSnipe/SnipeList.lua tests/run_tests.py
git commit -m "feat: 可购买列表加滚动区，窗口高度固定 260 不随行数跳动"
```

---

## Task 3: 一次点击一个请求 + 结局即删行

**Files:**
- Modify: `AHSnipe/BuyQueue.lua`（L1-22 文件头与常量注释、L115-160 发送时机说明、L162-264 分块全套、L266-290 `BuyCommodity`、L295-345 `BuyNextItem`/`BuyItems`、L436-476 `ITEM_PURCHASED`/`COMMODITY_PURCHASE_FAILED` 分支）
- Modify: `AHSnipe/SnipeList.lua`（L18 `buyingItemID`、L225-232 `OnBuyFinished`、`Render` 的"购买中"前缀、`TriggerBuy` 非 commodity 分支）
- Modify: `tests/run_tests.py`（删 3 个 chunk 测试、`load_buy_into_list`、`test_buy_outcome_deletes_row`、改 `test_buqueue_notifies_list`/`test_snipe_list_buy`/`smoke_test` 第 5 段、清单同步）

**Interfaces:**
- Consumes: `List.DropRow(key, reason)`、行的 `auctionID`/`key`、`hit.rowKey`。
- Produces:
  - `summary = { bought, failed, spent, units, isCommodity, itemID, rowKey, reason }`（`deferred` 与 `chunks` 消失）
  - `List.OnBuyFinished(s)`：`s.rowKey` 存在即删那一行（不看成败），并清"购买中"标记
  - BuyQueue 不再有"下一块"：任何结局 → `FinishSummary()`

- [ ] **Step 1: 写失败测试（真实 BuyQueue + 真实 SnipeList 联动）**

删除 `test_commodity_chunks_sorted`、`test_commodity_chunks_same_price`、`test_commodity_chunks_filter` 三个函数，并从 `tests` 清单移除。新增合并环境与被抢/否决/成交三种结局断言：

```python
def load_buy_into_list():
    """真实 BuyQueue + 真实 SnipeList：驱动一次购买，观察只删被点那一行。"""
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
    return lua, g.AHSnipe.SnipeList, g.AHSnipe.BuyQueue, logs


def test_buy_outcome_deletes_row():
    """任意结局只删被点那一行：均价否决 → 该行出门、另一档仍可点；随后成交 → 该行也出门。"""
    lua, lst, bq, logs = load_buy_into_list()
    lst.Add(make_hit_rows(lua, 101, [(148900, 2), (156000, 4)], 97))
    if lst.RowCount() != 2:
        return fail("buy_outcome", f"前置：应 2 行，实际 {lst.RowCount()}")
    front_key = str(lst.GetFront()["key"])
    if front_key != "C:101:148900":
        return fail("buy_outcome", f"队首应是最便宜档，实际 {front_key}")
    if not lst.TriggerBuy():
        return fail("buy_outcome", "TriggerBuy 未受理")
    if not bq.IsBusy():
        return fail("buy_outcome", "点击后应有一个请求在途")
    f = bq.frame
    on = f.GetScript(f, "OnEvent")
    # 结局一：报价均价超门槛 → 否决收尾（threshold = 148900*3 = 446700，报 900000 必然否决）
    on(f, "COMMODITY_PRICE_UPDATED", 900000, 1800000)
    if lst.RowCount() != 1:
        return fail("buy_outcome", f"否决应删被点行，实际剩 {lst.RowCount()} 行")
    if str(lst.GetFront()["key"]) != "C:101:156000":
        return fail("buy_outcome", "另一档位行必须保留且可继续点")
    if bq.IsBusy():
        return fail("buy_outcome", "否决后应已结束购买会话，不得占住通道")
    # 结局二：再点一次，报价合规 → 成交
    lst.TriggerBuy()
    on(f, "COMMODITY_PRICE_UPDATED", 156000, 624000)
    on(f, "COMMODITY_PURCHASE_SUCCEEDED")
    if lst.RowCount() != 0:
        return fail("buy_outcome", f"成交应删被点行，实际剩 {lst.RowCount()} 行")
    ok("buy_outcome")
```

`test_buqueue_notifies_list`（L311-330）改为断言 `rowKey`/`reason` 透传、无 `deferred`：

```python
def test_buqueue_notifies_list():
    """commodity 单请求结束后回调 OnBuyFinished，携带 rowKey 与结局原因。"""
    lua, bq, finished = load_buy_queue()
    hit = lua.table_from({
        "itemID": 237361, "threshold": 156000, "isCommodity": True, "rowKey": "C:237361:148900",
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
    if str(s["rowKey"]) != "C:237361:148900":
        return fail("bq_notify", f"rowKey 未透传：{s['rowKey']}")
    if str(s["reason"]) != "购买成交":
        return fail("bq_notify", f"reason 未写入：{s['reason']}")
    ok("bq_notify")
```

`test_snipe_list_buy`（L281-308）的最后两条断言按新语义改写（失败也删行、按 key 删）：

```python
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 102, "bought": 1, "failed": 0, "rowKey": "C:102:60000", "reason": "购买成交"}))
    if lst.RowCount() != 1 or str(lst.GetFront()["key"]) != "C:101:50000":
        return fail("snipe_list_buy", "成交应只删被点那一行")
    busy["v"] = False
    lst.OnBuyFinished(lua.table_from(
        {"itemID": 101, "bought": 0, "failed": 1, "rowKey": "C:101:50000", "reason": "购买超时"}))
    if lst.RowCount() != 0:
        return fail("snipe_list_buy", "失败结局同样删行，避免证伪过的行占住队首")
```

`smoke_test` 第 5 段（L501-536）的最后一次 `OnBuyFinished` 调用补 `rowKey`/`reason`：

```python
        addon.SnipeList.OnBuyFinished(lua.table_from(
            {"itemID": 237363, "bought": 1, "failed": 0, "deferred": False,
             "rowKey": "C:237363:100000", "reason": "购买成交"}))
        if addon.SnipeList.RowCount() != 1:
            return fail("smoke:list", "成交应删被点行并保留另一行")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py`
Expected: FAIL — `bq_notify` 报"rowKey 未透传：nil"；`buy_outcome` 报否决后仍剩 2 行或 `IsBusy` 为真。

- [ ] **Step 3: BuyQueue 去分块**

`AHSnipe/BuyQueue.lua` 文件头（L1-6）替换为：

```lua
-- 购买执行：一次点击一个请求。commodity 走两段式（StartCommoditiesPurchase → 报价均价门槛复核 →
-- ConfirmCommoditiesPurchase）；非 commodity 对单条 auctionID 走 PlaceBid。
-- 任何结局（成交、均价否决、价格不可用、超时、服务器失败）都立即收尾并回调列表删掉被点那一行——
-- 剩余挂单由 ≤1~2 秒后的扫描用当前数据重新入列，再由用户点击。
-- 已废弃：按挂单分块串行购买（块推进的发送栈全部实证被丢弃，见下方发送时机说明）。
-- 汇总：每笔购买在聊天框报告"购买成功：物品名，单价 * 数量 = 总价"或"购买失败：物品名，失败原因"。
```

把"购买消息的发送时机（2026-10-15 多轮日志实证，改动前必读）"整段注释（L115-147）的**结论部分**替换为：

```lua
-- 新纪律（本轮落地形态）：一次点击只有一个请求，因此【任何结局都只收尾这一次】——
-- 不再有"块推进"，也不再从购买生命周期事件栈/否决事件栈外发下一个请求（三处实证必被丢弃：
-- 计时器栈、成功事件栈、否决事件栈）。收尾路径只调 CancelCommoditiesPurchase（清理悬挂会话）
-- 与 FinishSummary（含 SnipeList.OnBuyFinished 的行删除），不外发任何购买消息。
-- 依旧严禁把购买 API 包进 C_Timer 延迟/轮询：那正是丢弃的触发条件。
```

删除 `BuildCommodityChunks`（L162-174）、前向声明 3 行（L176-178）、`CommodityStartChunk`（L203-227）、`CommodityFailChunk`（L228-242）、`CommodityNextChunk`（L258-264）；`CommodityFailEnd` 重命名并简化为唯一的失败收尾（不带"剩余块"文案）：

```lua
-- 失败收尾（任意栈内都安全：只取消 + 结算，不外发购买消息）
local function CommodityFail(reason)
    local plan = commodityPlan
    if not plan or not summary then return end
    if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
    plan.phase = nil
    summary.failed = summary.failed + 1
    summary.reason = reason
    Log(("购买失败 commodity %s x%d：%s"):format(plan.itemID, plan.quantity or 0, reason))
    ReportBuy(false, plan.itemID, plan.unitPrice, plan.quantity, nil, reason)
    FinishSummary()
end

-- 发起本次请求（单条挂单 = 一次点击）：plan 就位 → 挂超时 → 直接发送
local function CommodityStart(itemID, listing, threshold, rowKey)
    summary = { bought = 0, failed = 0, spent = 0, units = 0,
                isCommodity = true, itemID = itemID, rowKey = rowKey, reason = nil }
    commodityPlan = {
        itemID = itemID, threshold = threshold, quantity = listing.quantity,
        scanUnitPrice = listing.unitPrice, unitPrice = nil, totalPrice = nil, phase = "price",
    }
    local plan = commodityPlan
    Log(("购买开始 commodity %s x%d 门槛%.2fg（扫描单价%.2fg）"):format(
        itemID, listing.quantity, (threshold or 0) / 10000, (listing.unitPrice or 0) / 10000))
    ArmCommodityTimer(plan)
    if not summary or commodityPlan ~= plan or not plan.phase then return end
    SendCommodityPurchase(itemID, listing.quantity)
end
```

`BuyCommodity`（L266-290）整体替换：

```lua
function BuyQueue.BuyCommodity(hit)
    if summary then return false end       -- 客户端只有一个购买会话
    local listing = hit.matched and hit.matched[1]
    if not listing or not listing.quantity or listing.quantity <= 0 then
        summary = { bought = 0, failed = 1, spent = 0, units = 0,
                    isCommodity = true, itemID = hit.itemID, rowKey = hit.rowKey,
                    reason = "无有效挂单（缺少数量）" }
        ReportBuy(false, hit.itemID, nil, nil, nil, "无有效挂单（缺少数量）")
        FinishSummary()
        return false
    end
    CommodityStart(hit.itemID, listing, hit.threshold, hit.rowKey)
    return true
end
```

`ArmCommodityTimer` 内的超时收尾改调 `CommodityFail`（文案不变：`购买超时（未收到价格响应）`/`购买超时（购买结果未确认）`）。事件分支：`COMMODITY_PRICE_UNAVAILABLE` 由 `CommodityFailChunk(...)` 改为 `CommodityFail("价格不可用（可能已被买走或下架）")`；均价否决与 `COMMODITY_PURCHASE_FAILED` 的 `CommodityFailEnd` 一并改成 `CommodityFail`；`COMMODITY_PURCHASE_SUCCEEDED` 去掉 `plan.index/#chunks` 与 `deferred` 分支：

```lua
    elseif event == "COMMODITY_PURCHASE_SUCCEEDED" then
        local plan = commodityPlan
        if plan and summary and summary.isCommodity and plan.phase == "result" then
            if commodityTimer then commodityTimer:Cancel(); commodityTimer = nil end
            plan.phase = nil
            Log(("购买成功 commodity %s x%d"):format(plan.itemID, plan.quantity))
            ReportBuy(true, plan.itemID, plan.unitPrice, plan.quantity, plan.totalPrice)
            summary.bought = 1
            summary.spent = summary.spent + (plan.totalPrice or 0)
            summary.units = summary.units + (plan.quantity or 0)
            summary.reason = "购买成交"
            FinishSummary()
        end
```

非 commodity 同样退化为单条（删 `itemQueue`/`itemIndex`）：

```lua
local pendingEntry = nil      -- 本次 PlaceBid 等待回包的挂单（nil=无在途）
local currentItemID = nil     -- 非 commodity 购买的目标物品ID（事件归因）
local bidTimer = nil          -- 单条超时计时器

local function ItemFail(reason)
    if bidTimer then bidTimer:Cancel(); bidTimer = nil end
    if not summary then return end
    summary.failed = summary.failed + 1
    summary.reason = reason
    ReportBuy(false, currentItemID, nil, nil, nil, reason)
    pendingEntry = nil
    FinishSummary()
end

function BuyQueue.BuyItems(hit)
    if summary then return false end
    local listing = hit.matched and hit.matched[1]
    if not listing or not listing.auctionID or not listing.buyoutAmount then
        summary = { bought = 0, failed = 1, spent = 0, units = 0,
                    isCommodity = false, itemID = hit.itemID, rowKey = hit.rowKey,
                    reason = "无有效挂单（缺少 auctionID/一口价）" }
        ReportBuy(false, hit.itemID, nil, nil, nil, "无有效挂单（缺少 auctionID/一口价）")
        FinishSummary()
        return false
    end
    summary = { bought = 0, failed = 0, spent = 0, units = 0,
                isCommodity = false, itemID = hit.itemID, rowKey = hit.rowKey }
    currentItemID = hit.itemID
    pendingEntry = listing
    Log(("购买开始 item %s auctionID=%s 金额%.2fg"):format(
        hit.itemID, listing.auctionID, listing.buyoutAmount / 10000))
    bidTimer = C_Timer.NewTimer(BID_TIMEOUT, function()
        if not summary or pendingEntry ~= listing then return end
        Log(("PlaceBid 超时 auctionID=%s（无 ITEM_PURCHASED 回包）"):format(listing.auctionID))
        ItemFail("购买无响应（可能已被买走或金币不足）")   -- 计时器栈只收尾，不重发
    end)
    Log(("已发送 PlaceBid auctionID=%s（闸门就绪=%s）"):format(
        listing.auctionID, tostring(C_AuctionHouse.IsThrottledMessageSystemReady())))
    SafeCall("PlaceBid", C_AuctionHouse.PlaceBid, listing.auctionID, listing.buyoutAmount)
    return true
end
```

`ITEM_PURCHASED` 分支去掉 `itemIndex`/`deferred`/剩余条目文案，成交后 `summary.bought = 1`、`summary.reason = "购买成交"`、`pendingEntry = nil`、`FinishSummary()`；`AUCTION_HOUSE_CLOSED` 分支同样保留（它已走 `FinishSummary`，只需把 `summary.failed` 计数与 `summary.reason = "拍卖行已关闭，购买中断"` 写上）。

`FinishSummary` 内 `itemQueue = nil; itemIndex = 0` 两行随之删除；文件底部 `Buy` 入口与 `IsBusy` 不变。

- [ ] **Step 4: SnipeList 按 key 删行**

`buyingItemID` → `buyingRowKey`（声明、`TriggerBuy` 赋值、`Render` 前缀判断三处同步：`if buyingRowKey == row.key then label = "|cffffcc00购买中|r " .. label end`）；`List.OnBuyFinished` 整体替换：

```lua
-- BuyQueue 结算回调：无论成交还是失败，被点那一行都出门——
-- 证伪过的行留在队首只会让用户再点一次失败并占住购买通道。
-- 幸存/剩余挂单由 ≤1~2 秒后的扫描用当前数据重新入列（还会响一声）。
function List.OnBuyFinished(s)
    buyingRowKey = nil
    if s and s.rowKey then
        List.DropRow(s.rowKey, s.reason or "购买结束")
    end
    Render()
end
```

`AUCTION_HOUSE_CLOSED` 的生命周期分支把 `buyingItemID = nil` 改为 `buyingRowKey = nil`。

`TriggerBuy` 非 commodity 分支改为按行的 `auctionID` 从刷新结果里精确取本行挂单，并对"证据类失败"删行（通道类失败保留行）：

```lua
-- 刷新失败原因分类：只有"新鲜数据证实这行没了"才删行；
-- 超时/暂停/在途属于通道问题，行留着等下一次点击。
local GONE_REASONS = {
    ["诱饵已被买走或下架"] = true,
    ["价格已回涨，已无低于门槛的挂单"] = true,
}

-- （TriggerBuy 内，RefreshHit 回调）
        AHSnipe.Scanner.RefreshHit(RowToHit(row), function(fresh, reason)
            if not fresh then
                Log(("购买前刷新失败 %s：%s"):format(row.itemID, reason))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(row.name, reason))
                if GONE_REASONS[reason] then List.DropRow(row.key, reason); Render() end
                return
            end
            local listing
            for _, l in ipairs(fresh.matched or {}) do
                if l.auctionID == row.auctionID then listing = l break end
            end
            if not listing then
                local why = "该挂单已被买走或下架"
                Log(("购买前刷新 %s：本行 auctionID=%s 已不在最新挂单"):format(row.itemID, tostring(row.auctionID)))
                print(("|cff33FF99AHSnipe|r: |cffFF4040购买失败|r：%s，%s"):format(row.name, why))
                List.DropRow(row.key, why)
                Render()
                return
            end
            local hit = RowToHit(row)
            hit.matched = { listing }
            if BQ.Buy(hit) then
                buyingRowKey = row.key
                Render()
            end
        end)
```

（Task 1 Step 3 里 `hit.matched = { fresh.matched[1] }` 的临时写法在本步被上面这段替换。）

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax`
Expected: `16/16 单测通过`、exit 0（删掉 3 个 chunk 测试、加 1 个联动测试后的总数）。

- [ ] **Step 6: 提交**

```bash
git add AHSnipe/BuyQueue.lua AHSnipe/SnipeList.lua tests/run_tests.py
git commit -m "refactor: 购买改为一次点击一个请求，任何结局只删被点那一行（移除分块/deferred）"
```

---

## Task 4: Scanner 聚焦不排他（补查预算）

**Files:**
- Modify: `AHSnipe/Scanner.lua`（L43-48 聚焦常量区、L51-53 轮次上下文、L439-478 `ScanRound`、L401-405 `QueryNext` 发送处、L484-502 `Pause`）
- Modify: `tests/run_tests.py`（`SCANNER_PATH` 常量、`load_scanner`、`test_build_round_queue`、清单）

**Interfaces:**
- Consumes: `Config.GetItems()`（`{ itemID, enabled }` 数组）、`focus`（`[itemID] = { expire, miss }`）。
- Produces: `Scanner.BuildRoundQueue(items, focusSet, queried, now, starveLimit) -> queue, stats`（`stats = { focus, refill, oldest }`）；模块内 `lastQueried[itemID]`、`STARVE_LIMIT = 15`。

- [ ] **Step 1: 写失败测试**

在 `BUY_QUEUE_PATH` 常量旁加 `SCANNER_PATH = os.path.join(PLUGIN_DIR, "Scanner.lua")`，在 `test_buy_outcome_deletes_row` 下方新增（并加入 `tests` 清单末尾）：

```python
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
```

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py`
Expected: FAIL — `attempt to call a nil value (field 'BuildRoundQueue')`。

- [ ] **Step 2: 实现纯函数**

`AHSnipe/Scanner.lua` 常量区（L43-48）追加：

```lua
local STARVE_LIMIT = 15          -- 非聚焦项补查预算（秒）：聚焦期间最迟这么久一定被查到一次
local lastQueried = {}           -- [itemID] = 上次发送查询的时刻（GetTime）
local roundStats = nil           -- 本轮调度统计（诊断：聚焦挤掉了多少项）
```

`ScanRound`（L439-478）之前插入：

```lua
------------------------------------------------------------------------
-- 本轮查询队列（纯函数，可单测）
-- 旧实现"聚焦期间其余物品全部跳过"会把列表压成一条：命中不断续期聚焦窗口，
-- 其他监控物品可能整轮整轮不被查询（2026-10-16 20:02 日志：聚焦1个、其余7个连续跳过 16 秒）。
-- 新规则：聚焦项排前保持秒级复查，非聚焦项按补查预算（默认 15 秒）插队进来。
-- items 为 Config.GetItems() 结果；focusSet 为 [itemID]={expire,miss}；queried 为 [itemID]=时刻。
-- 返回 queue（itemID 数组）与 stats（{focus=聚焦项数, refill=补查项数, oldest=最久未查秒数}）
------------------------------------------------------------------------
function Scanner.BuildRoundQueue(items, focusSet, queried, now, starveLimit)
    local queue = {}
    local stats = { focus = 0, refill = 0, oldest = 0 }
    for _, it in ipairs(items or {}) do
        if it.enabled and focusSet[it.itemID] then
            queue[#queue + 1] = it.itemID
            stats.focus = stats.focus + 1
        end
    end
    local focusMode = stats.focus > 0
    for _, it in ipairs(items or {}) do
        if it.enabled and not focusSet[it.itemID] then
            local at = queried[it.itemID]
            -- 从未查过：视为已到点（首轮/恢复扫描立刻全覆盖）
            local age = at and (now - at) or starveLimit
            if age > stats.oldest then stats.oldest = age end
            if not focusMode or age >= starveLimit then
                queue[#queue + 1] = it.itemID
                stats.refill = stats.refill + 1
            end
        end
    end
    return queue, stats
end
```

- [ ] **Step 3: 接线**

`ScanRound` 的聚焦排他分支（L451-472）整体替换为：

```lua
    queue, roundStats = Scanner.BuildRoundQueue(Config.GetItems(), focus, lastQueried, now, STARVE_LIMIT)
    if roundStats.focus > 0 then
        if focusSince == 0 then focusSince = now end
        -- 补查数与"最久未查"是判断饥饿是否修好的直接证据：refill 长期为 0 说明预算没起作用
        Log(("聚焦%d个+补查%d个（最久未查%.1fs，聚焦已持续%.0fs）"):format(
            roundStats.focus, roundStats.refill, roundStats.oldest, now - focusSince))
    else
        focusSince = 0
    end
```

（原 `table.sort(queue)` 一并删除：队列顺序现在承载优先级，聚焦项必须排在补查项之前。）

`QueryNext` 的实际发送处（L401-405）登记时刻：

```lua
    lastQueried[itemID] = GetTime()
    if commodityCache[itemID] then
        QueryCommodity(itemID)
    else
        QueryItem(itemID)
    end
```

`Pause`（L499-500）保留 `focus = {}`、`focusSince = 0` 不变；`lastQueried` 不清（恢复后到点项立刻优先补查，正是想要的行为）。

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax`
Expected: `17/17 单测通过`、`SMOKE-OK 加载 8 个文件`、exit 0。

- [ ] **Step 5: 提交**

```bash
git add AHSnipe/Scanner.lua tests/run_tests.py
git commit -m "fix: 聚焦不再排他，非聚焦项按 15 秒补查预算入列（修列表只剩一条）"
```

---

## Task 5: 文档同步、全量门禁与部署

**Files:**
- Modify: `docs/tasks/2026-10-13-ahsnipe-plan.md`（在"## Self-Review 结论"之前插入"## 第二十一轮：行=单价档位 与结局即删行"）
- Modify: `AHSnipe/AHSnipe.toc`（`## Notes` 文案随"一行=一个档位"更新）
- Read-only 校验：`docs/superpowers/specs/2026-10-16-ahsnipe-listing-rows-design.md`（已提交，勿重复改）

**Interfaces:**
- Consumes: Task 1-4 的最终代码。
- Produces: 本轮记录、同步到游戏目录的可用版本、交用户的 6 条验收清单。

- [ ] **Step 1: TOC 文案**

`AHSnipe/AHSnipe.toc` 的 `## Notes` 改为：

```
## Notes: 拍卖行低价狙击。打开拍卖行自动扫描监控物品，命中即入可购买列表（一行=一个单价档位），点【立即购买】买队首一条，结局后该行出门、剩余由下轮扫描重新入列。
```

- [ ] **Step 2: 本轮记录**

在 `docs/tasks/2026-10-13-ahsnipe-plan.md` 的"## Self-Review 结论"之前插入一节，逐条写实现与证据（保持既有轮次的行文格式：`## 第N轮：标题` + 实现要点 + 测试 + 待游戏内验证 + 遗留问题）：

```markdown
## 第二十一轮：行=单价档位 与结局即删行

**动因（2026-10-16 20:01—20:02 日志）**：① 行按物品显示合计数量、一次点击只成交分块的第 1 块（"显示 4 个只能 2 个 2 个买"）；
② 证伪过的行留在队首，连点四次 `块失败…实际单价5.80g 已超门槛3.49g`，每次点都占住购买会话（报价 3s / 成交 10s），
期间按钮置灰、真正的第二条没法点，等它自己超时；③ `聚焦轮次：1 个商品（237362），其余7个本轮跳过（聚焦已持续4s→16s）`
把列表压成一条。

**实现**：
1. `SnipeList` 行改为数据对象：commodity 按 `C:<itemID>:<单价>` 成行（同价位多条挂单单位合计），非 commodity 按 `A:<auctionID>` 成行；
   `Add(hit)` 用 `BuildRows` 摊行后按 key 与该物品现有行对齐（命中→原地刷新、缺失→删那一行、新 key→入列）；
   排序 `seenAt` 降序、同次命中按单价升序；提示音改为"一轮（`roundStartAt` 身份）最多响一次"；`MAX_ROWS=60` 淘汰排序末尾。
2. 窗口固定 260 高 + `UIPanelScrollFrameTemplate` 滚动区（可见约 6 行），行池挂在 `AHSnipeListContent` 上；
   滚动钳制自算 `#rows*ROW_H - VIS_H`，不调 `GetVerticalRange`（返回形态未实证）。
3. 购买退化为一次点击一个请求：`TriggerBuy` 把队首行合成 `matched` 只含一条挂单的 hit；
   `BuyQueue` 删除 `BuildCommodityChunks`/`chunks`/`index`/`CommodityNextChunk`/`CommodityFailChunk`/`summary.deferred`
   与"剩余%d块交给下次弹窗"文案；`summary` 新增 `rowKey`（由 `hit.rowKey` 透传）与 `reason`；
   任何结局统一走 `CommodityFail`/`FinishSummary`，收尾路径不外发购买消息（保留既有发送时机纪律）。
   `OnBuyFinished(s)` 改为按 `s.rowKey` 删那一行，不再判断成败组合；"购买中"标记由 `buyingItemID` 改为 `buyingRowKey`。
   非 commodity 刷新后按 `auctionID` 精确取本行挂单，证据类失败（`诱饵已被买走或下架`/`价格已回涨…`）删行、通道类失败保留行。
4. `Scanner.BuildRoundQueue(items, focusSet, queried, now, starveLimit)` 纯函数：聚焦项排前 + 非聚焦项超
   `STARVE_LIMIT=15` 秒补查；`lastQueried[itemID]` 在 `QueryNext` 发送处登记；日志改为
   `聚焦N个+补查M个（最久未查X.Xs，聚焦已持续Ys）`；删掉聚焦分支的 `table.sort(queue)`（顺序现在承载优先级）。
5. 已否决并记录在 spec：跨档位合计成一个请求（大单被抢后按市场价补量可能亏钱）、按数量静态切块
   （失败率取决于下单量÷低价池存活量，静态比例等于猜；分块数≈硬件点击数）。

**测试**：删 3 个 `test_commodity_chunks_*`；新增 `test_listing_rows_from_hit`、`test_row_reconcile`、`test_row_cap`、
`test_snipe_list_scroll`、`test_buy_outcome_deletes_row`（真实 BuyQueue + 真实 SnipeList，否决→删一行、成交→删另一行）、
`test_build_round_queue`；`test_buqueue_notifies_list` 断言 `rowKey`/`reason`；
`PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax` → 17/17 通过、exit 0。

**待游戏内验证**：见 spec"验收（游戏内）"6 条（两档位出两行、买一档只删一行、连点只失败一次、
聚焦 1 个时其余物品 15 秒内各被查到、12 行长高不变可滚动、关窗清空）。

**遗留**：① "手工看得见诱饵却没提醒"的漏报仍未定性，等本轮 `入列 …（数据X.Xs=排队+回包）` 日志取证；
② 按价值决定队首（用户提出"刚买 2 个鱼饵、突然出现大单，大单该抢队首"）留待单独一轮设计；
③ 聚焦补查项会拉长整轮（1 聚焦 + 若干补查 ≈ 每轮多 0.1s/项），若日志显示被抢货变慢可把 `STARVE_LIMIT` 上调。
```

- [ ] **Step 3: 全量门禁**

Run: `PYTHONIOENCODING=utf-8 python tests/run_tests.py --syntax`
Expected: 末尾 `17/17 单测通过`，无 `FAIL`，`echo $?` 为 0。

Run: `git status --short`
Expected: 仅本轮改动的文件为 modified/untracked，无意外文件（尤其确认没有 `AHSnipe/SnipeWindow.lua`）。

- [ ] **Step 4: 提交文档**

```bash
git add docs/tasks/2026-10-13-ahsnipe-plan.md AHSnipe/AHSnipe.toc
git commit -m "docs: 第二十一轮记录——行=单价档位、结局即删行、聚焦补查预算与游戏内验收清单"
```

- [ ] **Step 5: 同步到游戏目录（先备份，方向恒为仓库 → 游戏）**

```bash
G="/d/games/World of Warcraft/_retail_/Interface/AddOns/AHSnipe"
B="/tmp/game-addon-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$B" && cp -r "$G" "$B/" && echo "备份：$B"
cp /d/Repos/wow/AHSnipe/AHSnipe.toc /d/Repos/wow/AHSnipe/*.lua "$G/"
ls "$G" | grep -i snipewindow && mv "$G/SnipeWindow.lua" "$B/SnipeWindow.lua.removed" || true
for f in /d/Repos/wow/AHSnipe/*.lua /d/Repos/wow/AHSnipe/AHSnipe.toc; do
  diff -q "$f" "$G/$(basename "$f")" || echo "DIFF $(basename "$f")"
done
```

Expected: 备份目录名打印出来；`diff -q` 无输出（全部一致）；`SnipeWindow.lua` 不在游戏目录里。

- [ ] **Step 6: 交给用户验收**

在 `/reload` 后按 spec 的 6 条验收逐条对照（两档位两行 / 买一档只删一行 / 连点只失败一次 / `聚焦1个+补查M个` / 12 行可滚动不长高 / 关窗清空），并收集 `/ahsnipe log` 的 `入列`/`除行`/`聚焦…+补查…` 三类行。

---

## Self-Review

**1. Spec coverage**（对照 `2026-10-16-ahsnipe-listing-rows-design.md` 各节）：
行身份模型与对齐 → Task 1；每轮一次提示音与 `MAX_ROWS` → Task 1；窗口滚动 → Task 2；购买链路（`RowToHit`/`rowKey`/`OnBuyFinished`）与 BuyQueue 去分块 → Task 3；聚焦调度 → Task 4；错误处理（超时删行、`COMMODITY_PRICE_UNAVAILABLE` 删行、`auctionID` 失效删行、GONE/通道类失败分类）→ Task 3 Step 3/4；测试清单 → Task 1/2/3/4 各 Step 1；验收 → Task 5 Step 6；"已否决的分批方案"与"后续候选（按价值排队首）"作为非目标写入 Task 5 本轮记录的遗留项。无遗漏。

**2. Placeholder scan**：无 TBD/"适当处理"；每个改动步骤都给了可粘贴的 Lua/Python 片段与期望输出；Task 1 `TriggerBuy` 里 `hit.matched = { fresh.matched[1] }` 是刻意分步（Task 1 保持绿的必要过渡形态），Task 3 Step 4 已明确写出它被 auctionID 精确匹配替换。

**3. Type consistency**：行对象字段（`key`/`unitPrice`/`quantity`/`totalPrice`/`auctionID`/`buyoutAmount`/`seenAt`/`roundStartAt`/`querySendAt`/`threshold`/`isCommodity`/`itemID`/`name`）在 Task 1 `BuildRows` 定义，Task 2 渲染、Task 3 `RowToHit`/`DropRow` 使用同名键；`summary.rowKey`/`summary.reason` 由 Task 3 的 BuyQueue 写入、`OnBuyFinished` 读取；`BuildRoundQueue` 返回值在 Task 4 Step 3 按 `stats.focus/refill/oldest` 消费，与 Step 2 定义一致；`List.NewerThan` 同时用于 `Add` 与 `Render`。
