# 当前号排角色轴第一位 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施本计划。步骤用 `- [ ]` 勾选跟踪。

**Goal:** 任务×角色矩阵与导出里，当前登录的号永远排在角色轴第一位（横排=最左一列、竖排=最上一行、`/gql dump` 汇总=第一行），角色多到出滚动条时打开面板第一眼就是自己。

**Architecture:** 角色顺序只有一个真源 `Ledger.Characters`，横排列头、竖排左栏、导出两节全读它 —— 所以「当前号优先」只加在那一个函数上，三个消费方各自把「当前号是谁」传进去（Ledger 自己不碰 WoW 接口）。矩阵重绘路径改成「一轮问一次当前号、横排竖排共用」。

**Tech Stack:** Lua 5.1（WoW 12.x 插件）+ Python/lupa 离线替身测试（`tests/run_gql_tests.py`）。

设计文档：[docs/superpowers/specs/2026-11-09-current-char-first-design.md](../superpowers/specs/2026-11-09-current-char-first-design.md)
用户原话：「当前角色首位显示，比如角色横排的时候，当前角色在最左侧，这样角色多的时候（滚动条）不至于还要拖拽看」。

## Global Constraints

- **顺序真源只有一处**：`Ledger.Characters`（[Ledger.lua:2029](../../GoldQuestLedger/Ledger.lua)）。判据/顺序不许在显示层各写一份。
- **Ledger 不碰任何 WoW 接口**：当前号由调用方传 `curKey`（先例：`L.State(db, charKey, questID, gate, now, curKey)`，[Ledger.lua:1853](../../GoldQuestLedger/Ledger.lua)）。
- **重绘路径零新增接口调用**：横排本来在 [:1776](../../GoldQuestLedger/Matrix.lua) 问一次当前号，本轮只是把它提前，不许多问。
- **`curKey` 不传时逐字等于现状**（纯名序）：现有 524 条用例不该集体变红。
- **存档一个字段不动**：`db.chars` 的 key、数据、schema、迁移全不碰；本轮纯显示顺序。
- **只本地提交，不 push**；一次任务一个提交，测试与代码同提交。
- 界面文案纪律：界面只放玩家这一眼要用的，取证措辞进 probe（本轮不新增任何文案）。

## 现状事实（2026-11-09 当场读过，行号是今天的）

| 事实 | 位置 |
| --- | --- |
| 顺序真源 = `pairs(db.chars)` + `table.sort` 名序 | [Ledger.lua:2029](../../GoldQuestLedger/Ledger.lua) |
| 矩阵列 = 上面那个序，另算 `count`/`gold`/`stale` | [Ledger.lua:3241](../../GoldQuestLedger/Ledger.lua) |
| `L.MatrixRows` 也调 `Characters`，但只按 key 取状态、不看序 | [Ledger.lua:2937](../../GoldQuestLedger/Ledger.lua) |
| 横排 `Render`：:1743 取列 → :1748 分派竖排 → :1776 才问当前号 | [Matrix.lua:1743](../../GoldQuestLedger/Matrix.lua) |
| 竖排 `RenderCharsInRows` 自己又问一次（找 `curI`） | [Matrix.lua:2089](../../GoldQuestLedger/Matrix.lua) |
| 导出两节：`chars`（:48）、`cols`（:56） | [Dump.lua:48](../../GoldQuestLedger/Dump.lua) |
| 账本角色 key 就是 `Sync.CurrentCharKey()` 写的 | [Sync.lua:1173](../../GoldQuestLedger/Sync.lua) |
| 开面板唯一入口 `Mx.Toggle` **已经**做了 `offX, offY = 0, 0` | [Matrix.lua:2367](../../GoldQuestLedger/Matrix.lua) |
| 基线：`python tests/run_gql_tests.py` → **524/524 绿**（当场跑过） | — |

**设计文档已更正的一处**：spec 原文写"竖排不改一行"。把横排那次问话提到取列之前后，竖排会问两次
（它 :1749 就 `return`，今天走不到 :1776）⇒ 等于给竖排重绘新增一次接口调用。Task 3 因此多改两处：
分派把 `okCur, curKey` 传下去、竖排签名收这两个参数并删掉自己那三行问话。口径不变，只改"谁去问"。

**夹具的一个既有巧合**（解释为什么大多数现成用例不受影响）：`sync_env` 的当前号是 `小号一-永恒之井`
（[tests:6367](../../tests/run_gql_tests.py)），`big_matrix_env` 补的角色叫 `打杂N-永恒之井`
（[tests:10892](../../tests/run_gql_tests.py)）—— `小`(E5 B0 8F) 按键名序本来就排在 `打`(E6 89 93) 前面，
所以那些夹具里当前号**已经在第一列**，本轮改动对它们零位移。新用例必须特意造一个序在前的号
（`0号-永恒之井`，`0`=0x30），否则等于写了条永真断言。

---

## Task 1: `Ledger.Characters` 加"当前号优先"

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:2029-2034`
- Test: `tests/run_gql_tests.py`（新用例插在 `def test_matrix_columns_stale_and_manual_only_quest_kept():`（:7550）之前）

**Interfaces:**
- Consumes：`db.chars`（键 = 角色 key，值含 `lastSync`/`class`/`views`）。
- Produces：`L.Characters(db, curKey) -> { charKey, ... }`。`curKey` 可选；Task 2/4 靠这两个入参。

- [ ] **Step 1: 写失败用例**（整段贴进 tests，位置见上）

```python
def test_characters_puts_current_char_first():
    """角色顺序的唯一真源：传了当前号就把它排第 1，其余照旧按键名序。
    横排列、竖排行、导出两节全读这一个函数 —— "当前号在最前"只能长在这里，
    在显示层各排一遍就会出现"矩阵一个序、导出另一个序"（10-25 定过的口径）。
    不传 / 不是字符串 / 账本里没这个号：逐字等于原来的纯名序（读不到不猜、不报错）。"""
    name = "characters_puts_current_char_first"
    lua, gql = load_lua("Config", "Ledger")
    L, db = gql.Ledger, ledger_db(lua)
    for k in ("A-永恒之井", "B-永恒之井", "C-永恒之井"):
        db["chars"][k] = lua.table_from({"lastSync": 1000, "views": lua.table_from({})})

    def order(cur):
        got = L.Characters(db, cur) if cur is not None else L.Characters(db)
        return [str(got[i]) for i in range(1, len(got) + 1)]

    base = ["A-永恒之井", "B-永恒之井", "C-永恒之井"]
    if order("B-永恒之井") != ["B-永恒之井", "A-永恒之井", "C-永恒之井"]:
        return fail(name, f"传 curKey 时该把当前号排第 1，实际 {order('B-永恒之井')}")
    if order(None) != base:
        return fail(name, f"不传 curKey 该逐字等于原来的名序，实际 {order(None)}")
    for cur, why in (("没这个号-永恒之井", "账本里没这个号"),
                     (7, "curKey 是数字"),
                     (True, "curKey 是布尔")):
        if order(cur) != base:
            return fail(name, f"{why}时该退回纯名序且不报错，实际 {order(cur)}")
    ok(name)
```

同一次改动里，把它登记进 `__main__` 那份清单（[tests:23414](../../tests/run_gql_tests.py)）：
在 `test_matrix_columns_stale_and_manual_only_quest_kept,`（:23533）**上一行**插入
`        test_characters_puts_current_char_first,`。漏登记会被 :529 那条静态守卫抓红。

- [ ] **Step 2: 跑，确认按预期红**

Run: `python tests/run_gql_tests.py -k characters_puts_current_char_first`
Expected: `FAIL characters_puts_current_char_first 传 curKey 时该把当前号排第 1，实际 ['A-永恒之井', 'B-永恒之井', 'C-永恒之井']`
（今天的实现忽略多出来的第二个实参）

- [ ] **Step 3: 改实现**（[Ledger.lua:2029](../../GoldQuestLedger/Ledger.lua) 整函数换成下面这份）

```lua
--- 角色顺序的唯一真源：横排列、竖排行、导出三处全读这里。
--- `curKey`（调用方现问的当前号，Ledger 自己不碰接口）在账本里时排第 1，
--- 其余照旧按键名序；不传 / 不是字符串 / 账本里没这个号 ⇒ 逐字等于纯名序。
function L.Characters(db, curKey)
    local out = {}
    for charKey in pairs(db.chars or {}) do out[#out + 1] = charKey end
    table.sort(out)
    if type(curKey) == "string" then
        for i = 2, #out do
            if out[i] == curKey then
                table.remove(out, i)
                table.insert(out, 1, curKey)
                break
            end
        end
    end
    return out
end
```

- 从 `i = 2` 起找：`out[1]` 已经命中时什么都不用动（省掉一次 remove+insert）。
- 比较用 `==`：两边同源（`db.chars` 的 key 就是 `CurrentCharKey()` 写的），不引第二套规范化。
- **`L.MatrixRows` 里那句 `L.Characters(db)`（:2937）本轮不传 `curKey`**：它只按 key 取状态、不看序。

- [ ] **Step 4: 跑，确认绿**

Run: `python tests/run_gql_tests.py -k characters_puts_current_char_first` → `PASS`
Run: `python tests/run_gql_tests.py` → `525/525 通过`

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Ledger.lua tests/run_gql_tests.py
git commit -m "ledger: 角色顺序真源加\"当前号优先\"（curKey 由调用方传，不传=纯名序）"
```

---

## Task 2: `Ledger.MatrixColumns` 透传（只换序，判据一字不动）

**Files:**
- Modify: `GoldQuestLedger/Ledger.lua:3241-3242`
- Test: `tests/run_gql_tests.py`（紧接 Task 1 那条用例之后）

**Interfaces:**
- Consumes：Task 1 的 `L.Characters(db, curKey)`。
- Produces：`L.MatrixColumns(db, now, curKey) -> { {key,class,lastSync,count,gold,stale?}, ... }`
  （Task 3 的横排、Task 4 的导出都按这三参调用）。

- [ ] **Step 1: 写失败用例**

```python
def test_matrix_columns_current_first_only_moves_the_order():
    """列头只许换序，不许顺手改判据：传/不传 curKey 各调一次，
    每个号的 count/gold/stale/lastSync/class 必须逐字段相等。
    判据被"顺手优化"掉时这条必须红（顺序与判据是两件事，本轮只动顺序）。"""
    name = "matrix_columns_current_first_only_moves_the_order"
    lua, gql = load_lua("Config", "Ledger")
    L, db = gql.Ledger, ledger_db(lua)
    db["cycleAnchor"] = 5000
    db["chars"] = lua.table_from({
        "A-永恒之井": lua.table_from({"lastSync": 860400, "class": "MAGE",
                                      "views": lua.table_from({})}),
        "B-永恒之井": lua.table_from({"lastSync": 864000, "class": "PRIEST",
                                      "views": lua.table_from({})}),
        "C-旧名字": lua.table_from({"lastSync": 864000 - 8 * 86400, "class": "HUNTER",
                                    "views": lua.table_from({})}),
    })

    def cols_of(cur):
        raw = L.MatrixColumns(db, 864000, cur) if cur else L.MatrixColumns(db, 864000)
        out = []
        for i in range(1, len(raw) + 1):
            c = raw[i]
            out.append("%s|%d|%d|%s|%s|%d" % (
                str(c["key"]), int(c["count"]), int(c["gold"]), str(c["class"]),
                "stale" if c["stale"] is not None else "-", int(c["lastSync"])))
        return out

    base, moved = cols_of(None), cols_of("B-永恒之井")
    if [r.split("|")[0] for r in base] != ["A-永恒之井", "B-永恒之井", "C-旧名字"]:
        return fail(name, f"前置：不传 curKey 该是按键名序，实际 {base}")
    if [r.split("|")[0] for r in moved] != ["B-永恒之井", "A-永恒之井", "C-旧名字"]:
        return fail(name, f"当前号该排第 1，实际 {moved}")
    if sorted(base) != sorted(moved):
        return fail(name, f"换序不许改判据，两次调用的字段对不上：\n{base}\n{moved}")
    if moved[0].find("stale") >= 0:
        return fail(name, f"刚同步的当前号不该标过期：{moved[0]}")
    if moved[2].find("stale") < 0:
        return fail(name, f"8 天没登录的号该照样标过期：{moved[2]}")
    ok(name)
```

登记：在 `test_matrix_columns_stale_and_manual_only_quest_kept,`（:23533）**上一行**再插
`        test_matrix_columns_current_first_only_moves_the_order,`（排在 Task 1 那条之前之后都行，
两行紧邻即可）。

- [ ] **Step 2: 跑，确认红**

Run: `python tests/run_gql_tests.py -k matrix_columns_current_first_only_moves`
Expected: `FAIL ... 当前号该排第 1，实际 ['A-永恒之井|...', ...]`（今天的 `MatrixColumns` 把第三个实参丢掉）

- [ ] **Step 3: 改实现**（[Ledger.lua:3241](../../GoldQuestLedger/Ledger.lua) 只动两行）

改前：

```lua
function L.MatrixColumns(db, now)
    local chars = L.Characters(db)
```

改后：

```lua
function L.MatrixColumns(db, now, curKey)
    local chars = L.Characters(db, curKey)
```

函数体其余一行不动（`count`/`gold`/`lastSync`/`stale`/`class` 全照旧）。
另把文件头那句列说明（[:3238](../../GoldQuestLedger/Ledger.lua) `--- 列 = 角色：…`）补一行：
`--- 列序 = Ledger.Characters（含"当前号优先"，curKey 由调用方给）—— 界面两排与导出共用它。`

- [ ] **Step 4: 跑，确认绿**

Run: `python tests/run_gql_tests.py -k matrix_columns_current_first_only_moves` → `PASS`
Run: `python tests/run_gql_tests.py` → `526/526 通过`

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Ledger.lua tests/run_gql_tests.py
git commit -m "ledger: 矩阵列头透传 curKey（只换序，count/gold/stale 判据一字不动）"
```

---

## Task 3: 矩阵接线 —— 一轮问一次当前号，横排最左列 / 竖排最上行

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua:1741-1750`（提前问话 + 传参 + 分派）、`:1775-1778`（删原位置的三行）、`:2053`（竖排签名）、`:2088-2091`（删竖排自己那次问）
- Test: `tests/run_gql_tests.py`（新增 2 条 + 改 1 条现成用例 :8916）

**Interfaces:**
- Consumes：Task 2 的 `L.MatrixColumns(db, now, curKey)`。
- Produces：`Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)`（**签名变了**，唯一调用点
  是 [Matrix.lua:1749](../../GoldQuestLedger/Matrix.lua) 那句分派）。

- [ ] **Step 1: 写两条失败用例**（插在 `def test_chars_in_rows_current_char_tint_reuses_one_frame():`（:8916）之前）

```python
def test_matrix_header_puts_current_char_in_the_first_column():
    """横排：当前号该是最左那一列（表头中心 x=COL_W/2、正文底色贴在 x=0）。
    夹具特意造一个按键序在前的号（'0'=0x30 < '小'=0xE5）—— 不加它，
    "小号一"本来就排第一，这条会变成永真的假过。"""
    name = "matrix_header_puts_current_char_in_the_first_column"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=43200)
    g = lua.globals()
    gql.Matrix.Toggle()
    g.GQLDB["chars"]["0号-永恒之井"] = lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Render()
    head = [(t, x) for t, _f, _y, x in _head_lines(lua) if x is not None]
    first = [t for t, x in head if abs(x - MX_COL_W / 2) < 0.01]
    second = [t for t, x in head if abs(x - (MX_COL_W + MX_COL_W / 2)) < 0.01]
    if len(first) != 1 or "小号一" not in first[0]:
        return fail(name, f"最左一列该是当前号（中心 x={MX_COL_W / 2}），实际 {head}")
    if len(second) != 1 or "0号" not in second[0]:
        return fail(name, f"第 2 列该是让位的「0号」，实际 {head}")
    tint = lua.execute(r"""
        for _, c in ipairs(GQLMatrixContent.__children) do
            if c.__type == "Frame" and c.__tex then
                local p = (c.__points and c.__points[1]) or {}
                return p[4]
            end
        end
        return nil
    """)
    if tint is None or float(tint) != 0:
        return fail(name, f"当前号整列底色该贴在 x=0，实际 {tint}")
    ok(name)


def test_chars_in_rows_puts_current_char_in_the_first_row():
    """竖排：角色是左栏的行，同一个口径落成"当前号排第一行"（底色也亮在 y=0）。
    另外钉住**一轮只向 Sync 问一次当前号**：横排那次问话提前之后，竖排自己那次必须删掉，
    不然换轴等于给重绘路径新增一次接口调用（"重绘零新增接口"这条纪律的落点）。"""
    name = "chars_in_rows_puts_current_char_in_the_first_row"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=43200)
    g = lua.globals()
    gql.Matrix.Toggle()
    _stack_env(lua, gql, g)                                   # 3 条任务 + 小号二
    g.GQLDB["chars"]["0号-永恒之井"] = lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})       # 序在当前号之前
    gql.Config.SetCharsInRows(True)
    asked = lua.execute(r"""
        local real = GQL.Sync.CurrentCharKey
        local n = 0
        GQL.Sync.CurrentCharKey = function(...) n = n + 1 return real(...) end
        GQL.Matrix.Render()
        GQL.Sync.CurrentCharKey = real
        return n
    """)
    if int(gql.Matrix.rowCount) != 3:
        return fail(name, f"前置：竖排该画 3 条任务列，实际 {gql.Matrix.rowCount}")
    rows = [(t, y) for t, _f, y in _left_lines(lua) if y is not None]
    top = [t for t, y in rows if y == 0]
    if len(top) != 1 or "小号一" not in top[0]:
        return fail(name, f"左栏第一行该是当前号，实际 {rows}")
    second = [t for t, y in rows if y == MX_ROW_H]
    if len(second) != 1 or "0号" not in second[0]:
        return fail(name, f"第 2 行该是让位的「0号」，实际 {rows}")
    _blocks, geo = _cur_tint(lua)
    if not geo or geo[0][0] != 0 or geo[0][2] != MX_ROW_H:
        return fail(name, f"当前号整行底色该亮在 y=0、高=ROW_H，实际 {geo}")
    if int(asked) != 1:
        return fail(name, f"竖排整轮该只问一次当前号（与横排共用那一次），实际 {asked} 次")
    ok(name)
```

登记：在 `test_chars_in_rows_current_char_tint_reuses_one_frame,`（:23562）**上一行**插入这两条
（`        test_matrix_header_puts_current_char_in_the_first_column,` 与
`        test_chars_in_rows_puts_current_char_in_the_first_row,`）。

- [ ] **Step 2: 跑，确认两条都红**

Run: `python tests/run_gql_tests.py -k puts_current_char_in_the_first`
Expected ①：`FAIL ... 最左一列该是当前号（中心 x=36.0），实际 [('|cffffffff0号|r', ..., -10.0, 36.0), ('|cffffffff小号一|r', ..., -10.0, 108.0)]`
（今天表头就是这个形状，2026-11-09 实测量出：`0号` 在 x=36、当前号在 x=108）
Expected ②：`FAIL ... 左栏第一行该是当前号，实际 [('|cffffffff0号|r', -0.0), ('|cffffffff小号一|r', 20.0), ('|cffffffff小号二|r', 40.0)]`
（`_left_lines` 回的是 (文本, 字体, 行顶 y)，用例只留文本与 y；今天当前号在第二行，2026-11-09 实测量出。
 同一轮量到当前号底色 `(y=20, w=224, h=20)` —— 本轮之后 y 该是 0）
（这两条今天红在顺序；`只问一次`那半段今天恰好是 1 次（实测 `asked=1`），它是给 Step 3 漏删时准备的。）
Step 1 那条底色 x 的读法也实测过：横排正文底色帧 `__points[1]` 的第 4 项就是列的 x，
今天当前号在第二列 ⇒ `p[4] == 72`，本轮之后该是 `0`。

- [ ] **Step 3: 改 Matrix 四处**

① [Matrix.lua:1741-1743](../../GoldQuestLedger/Matrix.lua) 改成（把原来 :1776-1778 那三行搬到这里，**整块一字不改**）：

```lua
    local view = GQL.MatrixView
    local groups = view.Groups(db, now)
    -- 当前号一轮只问这一次：既给列序优先用（Ledger 自己不碰接口），也给下面的
    -- 底色/表头高亮用；横排竖排共用它（11-09 定的口径）。
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
    local cols = GQL.Ledger.MatrixColumns(db, now, curKey)
```

② 删掉原 :1776-1778 那三行（`local rowCount = 0` 那行留着、位置不动）。

③ 分派那行（:1748-1750）改成把两份现成结果传下去：

```lua
    if GQL.Config and GQL.Config.CharsInRows and GQL.Config.CharsInRows() == true then
        return Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)
    end
```

④ 竖排签名（:2053）改成 `function Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)`，
并**删掉 :2088-2091**（含"只问一次 Sync 的名字"那句注释）：

```lua
    -- 当前角色：只问一次 Sync 的名字（与横排同一条 pcall 形状，零新增接口）。
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
```

下面的 `local curI` 与查找循环**一行不改**（判据不变，只是 `okCur`/`curKey` 从入参来）。

⑤ 注释跟上（口径不变，只说明真源里多了一条优先）：竖排函数头那句"不许再算一次列（矩阵列与
Ledger 同源同序）"（[:2026](../../GoldQuestLedger/Matrix.lua)、[:1747](../../GoldQuestLedger/Matrix.lua)）
各补一句 `序里含"当前号优先"，真源仍在 Ledger`；横排 :1739 那句"准入门与排序仍是 Ledger 给的"照旧成立。

- [ ] **Step 4: 跑，确认两条新用例绿**

Run: `python tests/run_gql_tests.py -k puts_current_char_in_the_first` → 两条 `PASS`

- [ ] **Step 5: 修那条被本轮推翻的现成用例**

先说清**范围是查过的、不是猜的**（2026-11-09）：`grep -n "0号-永恒之井" tests/run_gql_tests.py` 只有
[:8927](../../tests/run_gql_tests.py) 一处造了"排在当前号前面"的号；其余动到表头/左栏/汇总的用例
（:8813 截断、:9080 行高、:9090 表头字体、:9238 过期、:6872/:8639 导出结构）都是**存在性**断言，
不看谁排第一 ⇒ 不会集体变红。所以本轮预期只有下面这一条现成用例需要改。

`test_chars_in_rows_current_char_tint_reuses_one_frame`（[tests:8916](../../tests/run_gql_tests.py)）
特意加了个排在当前号前面的号，并断言底色「**不是第 0 行**」—— 新口径下当前号就该在第 0 行，这条必红。
它 :8943 那两行改成：

```python
    if y != rows[0][2] or y != 0:
        return fail(name, f"底色该亮在当前号那一行，而 11-09 起当前号就排角色轴第一位"
                          f"（左栏第一行行顶 {rows[0][2]}）: {y}")
```

文件头那句注释（:8920-8921「夹具特意加了一个排在当前号**前面**的号（底色行顶 y 因此是 20 不是 0），
免得"永远钉在第 0 行"这种写法蒙对」）改成实话：

```
    夹具特意加了一个按键序在前的号：11-09 起当前号排角色轴第一位，底色必须跟着搬到第 0 行。
    "亮在当前号那一行"（rows[0] 的文本含"小号一"）与"当前号在第 0 行"两条同时钉 ——
    光把 y 写死成 0 会被文本断言抓出来，蒙不对。
```

- [ ] **Step 6: 跑全量 + 语法 + upvalue 三门**

Run: `python tests/run_gql_tests.py` → `528/528 通过`
Run: `python tests/run_gql_tests.py --syntax` → 全绿
Run: `python tests/run_gql_tests.py -k lua_upvalue_budget` → `PASS`
（:1776 那三行是**搬**早不是复制，`Render` 的 upvalue 只可能持平或减少；红了说明搬成了两份。）
若还有别的用例红：逐条看是不是"当前号本来被断言在别的列/行"，是就按 Step 5 同样的方式把判据改成
跟着排序后的当前号走，**不许**改成放宽断言蒙过。

- [ ] **Step 7: 提交**

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "ui: 矩阵一轮只问一次当前号；横排最左列、竖排最上行都是它"
```

---

## Task 4: 导出两节共用同一个序

**Files:**
- Modify: `GoldQuestLedger/Dump.lua:48`、`:56`
- Test: `tests/run_gql_tests.py`（新增 1 条）

**Interfaces:**
- Consumes：Task 1 的 `L.Characters(db, curKey)`、Task 2 的 `L.MatrixColumns(db, now, curKey)`、
  `GQL.Sync.CurrentCharKey()`。
- Produces：无（终端节点）。

- [ ] **Step 1: 写失败用例**

```python
def test_dump_puts_current_char_first_in_both_sections():
    """导出两节必须共用同一个序：「本周汇总」把当前号放第一行，
    「金币任务」每一格的归属跟着一起走。两处各排一遍 = 同一份账本报两个序
    （`dump_uses_the_ledger_for_columns_and_time` 那条静态守卫生的就是这类分歧）。
    格子本身不写号名，所以拿"只有当前号那格是已交"去认位，别靠数竖线。"""
    name = "dump_puts_current_char_first_in_both_sections"
    lua, gql, _ = sync_env([(71, 30000000)], extra=("Dump",), board=43200)
    g = lua.globals()
    gql.Sync.SnapshotNow()
    cur = str(gql.Sync.CurrentCharKey())          # 小号一-永恒之井
    g.GQLDB["chars"]["0号-永恒之井"] = lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})   # 按键名序它在前
    L, db = gql.Ledger, g.GQLDB
    now = int(lua.execute("return GQLDB.chars[%r].lastSync" % cur))
    L.SetManual(db, cur, 71, "done", now, 30000000)        # 只有当前号交过这条
    text = str(gql.Dump.Run())

    body = text.split("== 本周汇总 ==")[1].split("==")[0].strip().splitlines()
    if len(body) != 2:
        return fail(name, f"前置：汇总该有 2 个号，实际 {body}")
    if cur not in body[0]:
        return fail(name, f"汇总第一行该是当前号 {cur}，实际 {body[0]}")
    if "0号-永恒之井" not in body[1]:
        return fail(name, f"汇总第 2 行该是让位的「0号」，实际 {body[1]}")

    rows = text.split("== 金币任务 ==")[1].split("==")[0].strip().splitlines()
    line = [r for r in rows if r.startswith("任务71")]
    if len(line) != 1:
        return fail(name, f"前置：金币任务该有「任务71」这一行，实际 {rows}")
    cells = [c.strip() for c in
             line[0][line[0].index("[") + 1:line[0].rindex("]")].split("|")]
    if len(cells) != len(body):
        return fail(name, f"金币任务格子数({cells})与汇总行数({body})对不上，两处两个序")
    if cells[0] != "已交" or "已交" in cells[1:]:
        return fail(name, f"唯一那格「已交」属于当前号，该排在第 1 格（汇总同一序），实际 {cells}")
    ok(name)
```

登记：在 `test_dump_shows_stale_char,`（:23519）**下一行**插入
`        test_dump_puts_current_char_first_in_both_sections,`。

- [ ] **Step 2: 跑，确认红**

Run: `python tests/run_gql_tests.py -k dump_puts_current_char_first`
Expected（2026-11-09 拿这套夹具实跑过的形状）：
`FAIL dump_puts_current_char_first 汇总第一行该是当前号 小号一-永恒之井，实际 0号-永恒之井  完成 ?  金币 ?  同步于 1000`
⇒ 汇总里 `完成/金币` 是 `?`、`同步于` 是存档原值，都属正常 —— 这条只认**行序**与格子文本。
金币任务那节今天这一行是 `任务71 [待做 | 已交]`（改完该变 `[已交 | 待做]`）；
`任务71` 这个前缀是夹具里 `C_WorldQuests` 给的名字，不是硬猜（同一轮实测）。

- [ ] **Step 3: 改实现**（[Dump.lua:48](../../GoldQuestLedger/Dump.lua) 之前 + :56）

```lua
    -- 当前号只问这一次，两节共用：汇总与"金币任务"的格子必须同一个序，
    -- 否则同一份账本在一份导出里报两个顺序（读的人会照着错的序去对格子）。
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
    curKey = (okCur and curKey) or nil

    local chars = L.Characters(db, curKey)
```

:56 改成 `local cols = GQL.Ledger.MatrixColumns(db, now, curKey)`。

- `okCur and curKey or nil` 这一步不能省：pcall 失败时 `okCur` 是 `false`，
  直接把 `false` 当 key 传下去会绕过 Task 1 那条 `type(curKey) == "string"` 门（虽然也走名序，
  但语义要说清 —— 失败就当没这个人）。
- 静态守卫 `dump_uses_the_ledger_for_columns_and_time`（:6843）认的是串 `GQL.Ledger.MatrixColumns`，
  :56 保持全限定写法，别改成 `L.MatrixColumns`。

- [ ] **Step 4: 跑，确认绿**

Run: `python tests/run_gql_tests.py -k dump` → 全部 `PASS`（含 :6827/:6862/:8608 那几条导出用例）
Run: `python tests/run_gql_tests.py` → `529/529 通过`

- [ ] **Step 5: 提交**

```bash
git add GoldQuestLedger/Dump.lua tests/run_gql_tests.py
git commit -m "dump: 导出两节共用同一个序，当前号排汇总第一行"
```

---

## Task 5: 钉住"重开矩阵两轴归零"（现状，零代码改动）

**Files:**
- Test only: `tests/run_gql_tests.py`（新增 1 条；**不许**改 `Mx.Toggle`）

**Interfaces:**
- Consumes：`Mx.Toggle`（[Matrix.lua:2361](../../GoldQuestLedger/Matrix.lua)，:2367 已含 `offX, offY = 0, 0`）、
  `big_matrix_env`（[tests:10882](../../tests/run_gql_tests.py)）。
- Produces：无。

- [ ] **Step 1: 写用例**（插在 `def test_matrix_scroll_keeps_first_cell_after_content_shrinks():`（:10978）之前）

```python
def test_reopen_matrix_zeroes_both_offsets():
    """重开矩阵时两轴滚动都归零 —— Mx.Toggle 的现状（Matrix.lua:2367），本轮没写代码。
    它必须有用例钉着：「当前号在最左列/最上行」只有在打开那一下停在原点时才等于
    "第一眼是自己"。以后谁把"记住滚动位置"当改进加回来，先让这条红，
    而不是让玩家实测才发现自己又被滚出视野了。"""
    name = "reopen_matrix_zeroes_both_offsets"
    lua, gql, _ = big_matrix_env(rows=120, cols=14)   # 14 列才真横滚得动（帧宽跟着列数长）
    # 11-09 实测这套夹具：刚画完 offY=offX=0；滚轮 -200 → offY=1890；按住 shift 再滚 → offX=84；
    # 关-开之后两轴回到 0。列数少于 13 时横轴压根滚不动（frameW 没封顶），前置会红。
    gql.Matrix.Render()
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != 0:
        return fail(name, "前置：刚画完该停在最上")
    lua.execute("GQLMatrixScroll:GetScript('OnMouseWheel')(GQLMatrixScroll, -200)")
    off_y = int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))
    lua.execute("IsShiftKeyDown = function() return true end "
                "GQLMatrixScroll:GetScript('OnMouseWheel')(GQLMatrixScroll, -200)")
    off_x = int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()"))
    lua.execute("IsShiftKeyDown = function() return false end")
    if off_y <= 0 or off_x <= 0:
        return fail(name, f"夹具没滚起来（offY={off_y} offX={off_x}，120 行 14 列该两轴都能滚）")
    gql.Matrix.Toggle()                      # 关
    gql.Matrix.Toggle()                      # 开：offX/offY 在 Toggle 里归零 + 首绘同步
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != 0 \
            or int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()")) != 0:
        return fail(name, "重开矩阵后两轴都该回到 0（当前号就在最左列/最上行）")
    ok(name)
```

登记：在 `test_matrix_scroll_keeps_first_cell_after_content_shrinks,`（:23906）**上一行**插入
`        test_reopen_matrix_zeroes_both_offsets,`。

- [ ] **Step 2: 跑，确认直接绿**

Run: `python tests/run_gql_tests.py -k reopen_matrix_zeroes_both_offsets` → `PASS`
（这条**不承诺新行为**，红了就不是本轮引入的：先查谁动了 `Mx.Toggle`。它也是本轮唯一一条允许
"写了就绿"的用例 —— 钉现状，别为凑红去改 `Toggle`。）

- [ ] **Step 3: 跑全量**

Run: `python tests/run_gql_tests.py` → `530/530 通过`

- [ ] **Step 4: 提交**

```bash
git add tests/run_gql_tests.py
git commit -m "test: 钉住\"重开矩阵两轴归零\"（现状），防被\"记住滚动位置\"顶掉"
```

---

## Task 6: 变异检查 + 完成记录

**Files:**
- Modify: `docs/tasks/2026-11-09-current-char-first.md`（本文件末尾「完成记录」）
- 只读检查：`GoldQuestLedger/Ledger.lua`、`Matrix.lua`、`Dump.lua`

**Interfaces:**
- Consumes：Task 1-5 全部落地、530/530 绿。
- Produces：入库记录 + 交给用户实机验收的卡口。

- [ ] **Step 1: 变异检查（逐条要求咬红；每处改完立刻用 md5 还原，别留下第二份）**

| # | 变异 | 期望 |
| --- | --- | --- |
| ① | 删掉 `L.Characters` 里的 `if type(curKey) == "string"` 整块 | `characters_puts_current_char_first`、`matrix_columns_current_first_only_moves_the_order`、两条渲染用例（横排 + 竖排）、`dump_puts_current_char_first_in_both_sections` **全红**（名字以计划里实际写的为准） |
| ② | 把 `Dump.lua:48` 的 `curKey` 去掉（:56 留着） | `dump_puts_current_char_first_in_both_sections` 红（「已交」落在第 2 格）= 两节两个序 |
| ③ | 把 Task 3 ④ 删掉的那三行问话加回竖排 | `chars_in_rows_puts_current_char_in_the_first_row` 红在"只问一次"（变 2 次） |
| ④ | 把 `L.MatrixRows` 里 `L.Characters(db)` 也改成传 `curKey`（探针，**不是**变异） | **应当全绿**：证明那一处真的不看序。若红了，说明判断错了，回来读 `MatrixRows` 用 chars 的方式再定 |

命令：`python tests/run_gql_tests.py -k current_char` / `-k chars_in_rows` / `-k dump`，
逐条记录实际红/绿文案进完成记录。二进制读写 + 逐条 md5 还原一致才算过。

- [ ] **Step 2: 三门收尾**

```bash
python tests/run_gql_tests.py            # 期望 530/530
python tests/run_gql_tests.py --syntax   # 期望全绿（含 530 条语法检查）
python tests/run_gql_tests.py -k lua_upvalue_budget
grep -rn "Characters(db" GoldQuestLedger/   # 期望 4 处：Ledger 两处 + Dump 一处 + MatrixRows 一处（后者不传 curKey）
```

- [ ] **Step 3: 写完成记录**（本文件末尾那节，逐条交代）

改了哪四处、没动什么（存档/schema/判据/`MatrixRows` 那句）、用例增减（新增 6 + 改 1，524 → 530，
先红的原始文案）、三门输出、变异检查四条各自咬红/绿、主 agent 逐行 review 结论、本地提交号列表。
测试若造过任何临时行/临时脚本，写清用什么语义删的。

- [ ] **Step 4: 提交**

```bash
git add docs/tasks/2026-11-09-current-char-first.md
git commit -m "docs: 当前号排首位这轮的完成记录"
```

- [ ] **Step 5: 交给用户的卡口**（不替他做部署，也不擅自发版）

实机 `/reload` 后：① 号多到出横滚条时，打开矩阵第一眼是自己那一列；② 设置里勾「角色竖排」后
打开矩阵第一眼是自己那一行；③ 把横滚条拖到最右、关掉、再打开 —— 回到最左。
截图回传。

## 本轮不动的

`db.chars` 的 key 与全部数据、schema 版本、迁移（本轮没有数据变更）；准入门/门槛/到期/编号判据；
`stale` 的判据与"2 字 + 过期"的写法；列宽 72、行高 20、帧宽/帧高公式；设置页两颗开关；
`Mx.Toggle` 的归零（已是现状，只加用例钉住）；不 push。

## 待他点头的一处

**CHANGELOG 与版本号**：上一轮并进 0.6.2 是因为「他还没发出去」。本轮挂哪个版本（继续并 0.6.2 /
开 0.6.3）与那句玩家话的措辞等他一句话再写；计划不擅自改 `Core.lua` 的 `Version`、toc，
也不擅自重打 zip / 覆盖 AddOns。
