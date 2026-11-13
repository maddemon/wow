# GDL 时光徽章单独归类 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 金币买 / 卖时光徽章记成独立来源 `token`（界面「时光徽章」），收支照常计入，其余界面不动。

**Architecture:** 来源名只是 `src` 字符串 + 语言表一条词。买入靠钩 `C_WowTokenPublic.BuyToken` 抓市价、按精确金额认领；卖出靠拍卖行邮件标题匹配徽章物品名。`token` **不进** `TRANSFER_SOURCES`，所以 `daily` / `byDayChar` 照常累加。

**Tech Stack:** Lua 5.1（WoW 12.1.5）、Python + lupa 离线单测（`tests/run_gcl_tests.py`）。

## Global Constraints

- 版本 `0.3.2` → `0.4.0`，`GoldDailyLedger.toc` 与 `Core.lua` 的 `GDL.VERSION` 两处同步。
- `token` 不是搬钱：`transfer=false`，计入所有收支总量。
- 判不准就**不标**（回落 `ah` / `mail` / 上下文），绝不假归因。
- 不回溯历史存档、不加设置开关、不加存档字段、不升 schema。
- 现有 80 条用例全绿；新增用例必须注册进 `__main__` 的 `tests = [...]`。

---

### Task 1: 来源名与下拉（Locale + Flows）

**Files:**
- Modify: `GoldDailyLedger/Locale.lua:128-139`、`:217-226`
- Modify: `GoldDailyLedger/Flows.lua:59-60`
- Test: `tests/run_gcl_tests.py:1832-1833`（`CAL_ALL_SOURCES`）

**Interfaces:**
- Produces: `Locale.SrcName("token") == "时光徽章" / "WoW Token"`；`Flows` 的类型下拉含 `token`。

- [ ] **Step 1: 写失败断言**

在 `CAL_ALL_SOURCES` 的 `"ah"` 后插入 `"token"`：

```python
CAL_ALL_SOURCES = ["vendor", "ah", "token", "mail", "trade", "loot", "quest",
                   "repair", "unknown", "warband", "guildbank"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python tests/run_gcl_tests.py`
Expected: FAIL `locale_tables_are_two_language_complete`（缺 `SRC_TOKEN`）。

- [ ] **Step 3: 最小实现**

`Locale.lua` zhCN 段 `SRC_AH` 后加：

```lua
        SRC_TOKEN       = "时光徽章",
```

enUS 段 `SRC_AH` 后加：

```lua
        SRC_TOKEN       = "WoW Token",
```

`Flows.lua` 的 `SOURCES` 改为：

```lua
local SOURCES = { "vendor", "repair", "ah", "token", "mail", "quest", "loot", "trade",
                  "unknown", "warband", "guildbank" }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python tests/run_gcl_tests.py`
Expected: 全绿。

---

### Task 2: 买入识别（Money：待认领队列 + 钩子）

**Files:**
- Modify: `GoldDailyLedger/Money.lua:20-45`（常量/状态）、`:154-188`（队列区）、`:203-252`（Attribute）、`:358-402`（InstallHooks）
- Test: `tests/run_gcl_tests.py`（新增 `test_money_token_buy_marks_token`、`test_money_token_buy_amount_mismatch`）

**Interfaces:**
- Produces: `PLAYER_MONEY` 出账、金额精确等于 `GetCurrentMarketPrice()` 时写 `source="token"`。
- Consumes: `Ledger.AddEntry(amount, source, meta)`（现有）。

- [ ] **Step 1: 给替身加 `C_WowTokenPublic`**

在 `GDL_MOCK_ENV` 的 `C_Bank` 后加（依据：warcraft.wiki.gg `C_WowTokenPublic`，`BuyToken()` 无参）：

```lua
GDL_TOKEN_PRICE = 680000
GDL_TOKEN_NAME = "时光徽章"
WOW_TOKEN_ITEM_ID = 122284
C_WowTokenPublic = {
    BuyToken = function() return GDL_RunMethod(C_WowTokenPublic, "BuyToken") end,
    GetCurrentMarketPrice = function() return GDL_TOKEN_PRICE end,
}
function GetItemInfo(itemID)
    if GDL_TOKEN_NAME ~= nil and itemID == WOW_TOKEN_ITEM_ID then return GDL_TOKEN_NAME end
    return nil
end
```

- [ ] **Step 2: 写失败用例**

```python
def test_money_token_buy_marks_token():
    """BuyToken 当场抓的市价，下一笔精确同额出账记 token（照常计入收支）。"""
    name = "money_token_buy_marks_token"
    lua = money_env()
    boot(lua, 1000000)
    lua.execute("C_WowTokenPublic.BuyToken()")
    change_money(lua, 320000)
    problems = []
    if calls(lua) != [(-680000, "token")]:
        problems.append("应记 token -680000，实际 %s" % (calls(lua),))
    if session_stats(lua) != (0, 680000, -680000):
        problems.append("徽章支出要计会话统计，实际 %s" % (session_stats(lua),))
    return expect(name, problems)


def test_money_token_buy_amount_mismatch():
    """金额对不上（取消购买/价格变了）→ 回落上下文，不标 token。"""
    name = "money_token_buy_amount_mismatch"
    lua = money_env()
    boot(lua, 1000000)
    lua.execute("C_WowTokenPublic.BuyToken()")
    change_money(lua, 320001)
    problems = []
    if calls(lua) != [(-679999, "unknown")]:
        problems.append("对不上应回落 unknown，实际 %s" % (calls(lua),))
    return expect(name, problems)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python tests/run_gcl_tests.py`
Expected: 两条 FAIL（`unknown` 而不是 `token`）。

- [ ] **Step 4: 实现**

`Money.lua` 常量区加：

```lua
local TOKEN_TTL_SECONDS   = 30  -- 徽章买入请求的寿命（与搬钱同寿）
local TOKEN_QUEUE_LIMIT   = 10  -- 徽章买入队列上限
local TOKEN_ITEM_ID_FALLBACK = 122284  -- WoW Token（买家那份）物品 ID
```

状态区加：

```lua
local tokenBuys   = {}           -- FIFO：BuyToken 当场抓的市价 {amount, at}
```

队列区（搬钱队列之后）加：

```lua
--- 时光徽章物品名：先问客户端物品接口（暴雪自己的 BrowseWowTokenResults_Update
--- 就是 GetItemInfo(WOW_TOKEN_ITEM_ID) 取名字），问不到退回本插件语言表。
local function TokenName()
    local id = _G.WOW_TOKEN_ITEM_ID
    if type(id) ~= "number" then id = TOKEN_ITEM_ID_FALLBACK end
    local fn = _G.GetItemInfo
    if type(fn) == "function" then
        local okCall, name = pcall(fn, id)
        if okCall and type(name) == "string" and name ~= "" then return name end
    end
    local loc = GDL and GDL.Locale
    if type(loc) == "table" and type(loc.Get) == "function" then
        local okCall, s = pcall(loc.Get, "SRC_TOKEN")
        if okCall and type(s) == "string" and s ~= "" then return s end
    end
    return nil
end

local function QueueTokenBuy(amount)
    if type(amount) ~= "number" or amount <= 0 or IsSecretAmount(amount) then return end
    tokenBuys[#tokenBuys + 1] = { amount = amount, at = Uptime() }
    while #tokenBuys > TOKEN_QUEUE_LIMIT do table.remove(tokenBuys, 1) end
end

--- 只认出账、只认精确同额；过期条目顺手清掉。
local function TakeTokenBuy(absAmount, dir)
    if dir ~= MONEY_OUT then return false end
    local now = Uptime()
    for i = #tokenBuys, 1, -1 do
        local t = tokenBuys[i]
        if now and t.at and (now - t.at) > TOKEN_TTL_SECONDS then
            table.remove(tokenBuys, i)
        end
    end
    for i = 1, #tokenBuys do
        if tokenBuys[i].amount == absAmount then
            table.remove(tokenBuys, i)
            return true
        end
    end
    return false
end
```

`Attribute` 里搬钱命中之后、修理之前加：

```lua
    if TakeTokenBuy(absAmount, dir) then
        return "token", false, nil
    end
```

`InstallHooks` 里搬钱钩子后加：

```lua
    -- 时光徽章买入：客户端确认时调 BuyToken，那一刻的市价就是要扣的金币。
    local tokenApi = _G.C_WowTokenPublic
    if type(tokenApi) == "table" and type(tokenApi.BuyToken) == "function" then
        InstallHook(tokenApi, "BuyToken", function()
            local api = _G.C_WowTokenPublic
            local fn = type(api) == "table" and api.GetCurrentMarketPrice or nil
            if type(fn) ~= "function" then return end
            local okCall, price = pcall(fn)
            if okCall then QueueTokenBuy(price) end
        end)
    end
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python tests/run_gcl_tests.py`
Expected: 新增两条 PASS，其余全绿。

---

### Task 3: 卖出识别（Money：邮件标题）

**Files:**
- Modify: `GoldDailyLedger/Money.lua:203-252`（Attribute 的 mail 分支）
- Test: `tests/run_gcl_tests.py`（新增 `test_money_token_sell_mail`）

**Interfaces:**
- Produces: 拍卖行邮件标题含徽章物品名时，收入记 `source="token"`。

- [ ] **Step 1: 写失败用例**

```python
def test_money_token_sell_mail():
    """卖出徽章的金币由拍卖行邮件送来：标题含徽章物品名 → token；普通物品仍 ah。"""
    name = "money_token_sell_mail"
    seller = "拍卖行"
    problems = []

    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "时光徽章", 680000)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 681000)
    if calls(lua) != [(680000, "token")]:
        problems.append("徽章邮件应记 token，实际 %s" % (calls(lua),))

    lua = money_env()
    set_seller(lua, seller)
    boot(lua, 1000)
    fire(lua, "MAIL_SHOW")
    set_inbox(lua, 3, seller, "出售: 白银护臂", 500)
    lua.execute("TakeInboxMoney(3)")
    change_money(lua, 1500)
    if calls(lua) != [(500, "ah")]:
        problems.append("普通物品仍应 ah，实际 %s" % (calls(lua),))
    return expect(name, problems)
```

- [ ] **Step 2: 跑测试确认为失败**

Run: `python tests/run_gcl_tests.py`
Expected: FAIL（徽章邮件记成 `ah`）。

- [ ] **Step 3: 实现**

`Money.lua` 的 `Attribute` 里，mail 分支取到 `snap` 后、判 `seller` 之前加：

```lua
            if snap.money == absAmount and IsTokenMail(snap) then
                return "token", false, meta
            end
```

队列区加：

```lua
local function IsTokenMail(snap)
    if type(snap) ~= "table" or type(snap.subject) ~= "string" or snap.subject == "" then
        return false
    end
    local name = TokenName()
    if type(name) ~= "string" or name == "" then return false end
    return snap.subject == name or snap.subject:find(name, 1, true) ~= nil
end
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python tests/run_gcl_tests.py`
Expected: 新增 PASS，其余全绿。

---

### Task 4: 存储口径钉死 + 取证 + 版本

**Files:**
- Modify: `GoldDailyLedger/Money.lua`（`PrintProbe`）、`GoldDailyLedger/Ledger.lua:37-44`（注释）
- Modify: `GoldDailyLedger/Core.lua:10`、`GoldDailyLedger/GoldDailyLedger.toc:5`
- Test: `tests/run_gcl_tests.py`（新增 `test_ledger_token_counts_normally`）

**Interfaces:**
- Produces: `/gdl probe` 四行徽章取证；`token` 条目进 `daily` / `byDayChar`。

- [ ] **Step 1: 写失败用例**

```python
def test_ledger_token_counts_normally():
    """token 不是搬钱：进 daily / byDayChar / 范围合计，transfer=false。"""
    name = "ledger_token_counts_normally"
    lua = ledger_env()
    set_epoch(lua, ep(2026, 11, 10))
    boot(lua, 1000000)
    set_char(lua, "爱情之力")
    add_entry(lua, -680000, "token")
    add_entry(lua, 50000, "token")
    problems = []
    day = cal_eval(lua, "GDL.Ledger.GetDay('2026-11-10')")
    if as_number(day.expense) != 680000 or as_number(day.income) != 50000:
        problems.append("token 没进 daily：%s/%s" % (as_number(day.income), as_number(day.expense)))
    if as_number(ledger_eval(lua, "GDLDB.entries[1].transfer")) is not None:
        problems.append("token 被误标成 transfer")
    rt = cal_eval(lua, "GDL.Ledger.GetRangeTotals('2026-11-10','2026-11-10')")
    if as_number(rt.net) != -630000:
        problems.append("token 没进范围合计：%s" % as_number(rt.net))
    return expect(name, problems)
```

- [ ] **Step 2: 跑测试确认通过（存储层本来就对）**

Run: `python tests/run_gcl_tests.py`
Expected: PASS（若 FAIL 说明 `token` 被给成了 transfer）。

- [ ] **Step 3: 取证与注释**

`Money.lua` 的 `PrintProbe` 里 `C_Bank.DepositMoney` 那两行后加：

```lua
    print(("C_WowTokenPublic.BuyToken: %s"):format(DescribeMember(_G.C_WowTokenPublic, "BuyToken")))
    print(("C_WowTokenPublic.GetCurrentMarketPrice: %s"):format(DescribeMember(_G.C_WowTokenPublic, "GetCurrentMarketPrice")))
    print(("时光徽章物品名: %s"):format(TokenName() or "读不到"))
    print(("待认领的徽章买入: %d 条"):format(#tokenBuys))
```

`Ledger.lua` 的 `TRANSFER_SOURCES` 上方补一句：

```lua
-- 时光徽章（token）故意不在这里：它照常计入收支，只是单独一个分类。
```

- [ ] **Step 4: 版本号**

`GoldDailyLedger.toc` 的 `## Version: 0.3.2` → `0.4.0`；`Core.lua` 的 `GDL.VERSION = "0.3.2"` → `"0.4.0"`。

- [ ] **Step 5: 全量测试**

Run: `python tests/run_gcl_tests.py`
Expected: 全绿（80 + 新增 4 条）。
