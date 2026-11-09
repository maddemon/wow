# 2026-11-09 当前号排角色轴第一位（横排最左列 / 竖排最上行 / 导出第一行）

设计文档：[docs/superpowers/specs/2026-11-09-current-char-first-design.md](../superpowers/specs/2026-11-09-current-char-first-design.md)

用户 2026-11-09 提的新功能（原话）：「当前角色首位显示，比如角色横排的时候，当前角色在最左侧，
这样角色多的时候（滚动条）不至于还要拖拽看」。逐条问答定稿三条：三处都要（横排/竖排/导出）、
其余照旧按名字、每次打开矩阵滚动归零（追问后扩到两轴）。

## 一、现状事实（全部当场读过，行号是今天的）

| 事实 | 位置 |
| --- | --- |
| 角色顺序唯一真源：`pairs(db.chars)` + `table.sort` 名序 | [Ledger.lua:2029](../../GoldQuestLedger/Ledger.lua) |
| 矩阵列 = `Characters` 的序，另带 `count`/`gold`/`stale` 判据 | [Ledger.lua:3241](../../GoldQuestLedger/Ledger.lua) |
| `L.MatrixRows` 也调 `Characters`，但只按 key 取状态、不看序 | [Ledger.lua:2937](../../GoldQuestLedger/Ledger.lua) |
| 横排：`Render` 先取列（:1743），**之后**才问当前号（:1776） | [Matrix.lua:1743](../../GoldQuestLedger/Matrix.lua) |
| 竖排：`Render` 在 :1749 把整轮交给 `RenderCharsInRows`，`cols` 原样传下去 | [Matrix.lua:1748](../../GoldQuestLedger/Matrix.lua) |
| 竖排函数自己又问了一次当前号（用来找 `curI`） | [Matrix.lua:2089](../../GoldQuestLedger/Matrix.lua) |
| 导出：`chars`（:48）与 `cols`（:56）两份，同一个序 | [Dump.lua:48](../../GoldQuestLedger/Dump.lua) |
| 账本里的角色 key 就是 `Sync.CurrentCharKey()` 写的 | [Sync.lua:1173](../../GoldQuestLedger/Sync.lua) |
| 开面板唯一入口 `Mx.Toggle` **已经**做了 `offX, offY = 0, 0` | [Matrix.lua:2367](../../GoldQuestLedger/Matrix.lua) |
| 测试基线 | `python tests/run_gql_tests.py` → **524/524 绿**（2026-11-09 当场跑过） |

**设计文档里那句话要更正**：spec 写"竖排不改一行"。照 :1743 提前问当前号之后，竖排会走到两次问话
（提前那次 + 它自己 :2089 那次）—— 竖排今天走不到 :1776 是因为 :1749 就 `return` 了。
那等于给竖排重绘路径新增一次接口调用，违反"重绘零新增接口调用"。⇒ 按 T3 的 ③④ 把已问到的
`okCur, curKey` 传给竖排，并删掉它自己那三行。**口径不变，只改"谁去问"。**

## 二、任务

### T1 `Ledger.Characters` 加"当前号优先"

改前（[Ledger.lua:2029](../../GoldQuestLedger/Ledger.lua)）：

```lua
function L.Characters(db)
    local out = {}
    for charKey in pairs(db.chars or {}) do out[#out + 1] = charKey end
    table.sort(out)
    return out
end
```

改后：

```lua
--- 角色顺序的唯一真源：横排列、竖排行、导出三处全读这里。
--- `curKey`（调用方现问的当前号，Ledger 自己不碰接口）在账本里时排第 1，
--- 其余照旧按键名序；不传 / 不是字符串 / 账本里没这个号 ⇒ 逐字等于原来的纯名序。
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

- 从 `i = 2` 起找：`out[1]` 命中时什么都不用动（当前号本来就排第一，省掉一次 remove+insert）。
- 比较用 `==`：两边同源（`db.chars` 的 key 就是 `CurrentCharKey()` 写的），不引第二套规范化。
- **不动** `L.MatrixRows` 里那句 `L.Characters(db)`（:2937）：不传 `curKey`，逐字现状。

**验收**：`python tests/run_gql_tests.py -k characters_puts_current_char_first` 绿。
**边界**：不排序第二种口径、不加"过期沉底"、不动 `db.chars` 的任何字段。

### T2 `Ledger.MatrixColumns` 透传

改前（[Ledger.lua:3241](../../GoldQuestLedger/Ledger.lua)）：`function L.MatrixColumns(db, now)` / `local chars = L.Characters(db)`
改后：`function L.MatrixColumns(db, now, curKey)` / `local chars = L.Characters(db, curKey)`，函数体其余一行不动。

**验收**：`-k matrix_columns_current_first_only_moves_the_order` 绿（只换序，逐字段相等）。
**边界**：`count`/`gold`/`stale`/`lastSync`/`class` 判据不许顺手改。

### T3 三处接线

**① 横排**（[Matrix.lua:1743](../../GoldQuestLedger/Matrix.lua)）：把 :1776-1778 那三行 `local okCur, curKey = pcall(...)`
**整块搬到 :1743 之前**（一行不改地搬，`local rowCount = 0` 留在原地），并改成：

```lua
    local view = GQL.MatrixView
    local groups = view.Groups(db, now)
    -- 当前号先问这一次：既给列序优先用（Ledger 自己不碰接口），也给下面的
    -- 底色/表头高亮用 —— 一轮只问一次，横排竖排都是（11-09 定的口径）。
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
    local cols = GQL.Ledger.MatrixColumns(db, now, curKey)
```

**② 分派那行**（:1748-1750）改成把这两份传下去：`return Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)`。

**③ 竖排签名**（[Matrix.lua:2053](../../GoldQuestLedger/Matrix.lua)）改成
`function Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)`（唯一调用点就是 ②）。

**④ 删掉竖排自己那三行**（:2088-2091，含"只问一次 Sync 的名字"那句注释）：
`curI` 的查找循环保留、判据一字不改，只是改用传进来的 `okCur`/`curKey`。

**验收**：`-k matrix_header_puts_current_char_in_the_first_column` 与
`-k chars_in_rows_puts_current_char_in_the_first_row` 绿；后者额外断言**竖排整轮 `CurrentCharKey` 只被问 1 次**。
**边界**：不改底色/高亮三条路的判据，不改 `curI` 的找法，不动帧宽/帧高公式。

### T4 导出

[Dump.lua:48](../../GoldQuestLedger/Dump.lua) 之前补一次问话（手敲命令，多问一次名字不心疼，
且必须和 `Sync` 缺模块时一样不炸）：

```lua
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
    local chars = L.Characters(db, okCur and curKey or nil)
```

:56 改成 `local cols = GQL.Ledger.MatrixColumns(db, now, okCur and curKey or nil)`。

- `okCur and curKey or nil`：pcall 失败时**退回纯名序**，不把 `false` 当 key 传下去。
- 两处必须同一个 `curKey`：否则"本周汇总"和"金币任务"格子对不上（同一份账本不许两个序）。

**验收**：`-k dump_puts_current_char_first_in_both_sections` 绿。
**边界**：导出的文案、分节、判据、`?`/`（快照过期）`那几处口径一律不动。

### T5 用例（新增 6 条 + 改 1 条，先红后绿）

夹具口径：`sync_env` 的当前号 = `"小号一-永恒之井"`（[tests:6367](../../tests/run_gql_tests.py)），
替身屏幕 1440×720 ⇒ 横排要 ≥13 列才真滚得动（帧宽跟着列数长，`visW = 72n + 12 > contentW = 72n + 8`
在 `frameW` 封顶 1080 之前恒成立）。

1. `test_characters_puts_current_char_first`（挨着 :2029 那个函数写）——
   `db.chars` 放 `A-永恒之井`/`B-永恒之井`/`C-永恒之井`：
   `Characters(db, "B-永恒之井")` ⇒ `{"B…","A…","C…"}`；`Characters(db)` ⇒ `{"A…","B…","C…"}`；
   `Characters(db, "没这个号-永恒之井")`、`Characters(db, 7)`、`Characters(db, true)` ⇒ 逐字名序、不报错。
2. `test_matrix_columns_current_first_only_moves_the_order` —— 同一份 db 调两次
   （传/不传 `curKey`），按 key 取齐后断 `count`/`gold`/`stale`/`lastSync`/`class` 逐字段相等，
   只有数组序变了。**判据被顺手改掉时这条必须红。**
3. `test_matrix_header_puts_current_char_in_the_first_column` —— `sync_env([], extra=("Matrix",), board=43200)`
   + `Toggle`，再塞一个按键序在前的 `0号-永恒之井`（`0`=0x30 < `小`=0xE5）。读 `_head_lines(lua)`
   （[tests:8698](../../tests/run_gql_tests.py)）断：中心 x=`MX_COL_W/2`（36）那一列文本含"小号一"、
   x=36+`MX_COL_W` 那列含"0号"；再读当前号底色那块的锚 x（`GQLMatrixContent.__children` 里带 `__tex`
   的 Frame，取 `__points[1][4]`）= 0。**改前红在"小号一"在第 2 列。**
4. `test_chars_in_rows_puts_current_char_in_the_first_row` —— 同夹具 `SetCharsInRows(True)` + 同样那个
   排在前面的号：`_left_lines(lua)`（[tests:8715](../../tests/run_gql_tests.py)）第一行（行顶 y=0）文本含"小号一"，
   第二行（y=`MX_ROW_H`）含"0号"；`_cur_tint(lua)` 的 y=0。
   另外 spy 一次：`GQL.Sync.CurrentCharKey` 包一层计数，竖排整轮**恰好 1 次**（T3 ④ 漏删时这条红）。
5. `test_dump_puts_current_char_first_in_both_sections` —— `sync_env(..., extra=("Dump",))` + 排在当前号
   前面的号，`str(gql.Dump.Run())` 断："== 本周汇总 ==" 之后第一条是当前号；
   "金币任务"每行的格子数 == 汇总行数，且第 1 格属于当前号（两处不同序正是这条要防的）。
6. `test_reopen_matrix_zeroes_both_offsets` —— `big_matrix_env(rows=120, cols=14)`（[tests:10882](../../tests/run_gql_tests.py)）
   → `Render` → 普通滚轮若干下 + Shift+滚轮若干下 → 断两轴都 > 0（[tests:9372](../../tests/run_gql_tests.py) 同款读法）
   → `Toggle()`（关）→ `Toggle()`（开）→ `Render` → 两轴都 = 0。
   **这条钉的是现状，改了 T1-T4 之前就该绿**：它红了说明有人动了 `Mx.Toggle` 的归零，不是本轮引入的 bug。

**要改的一条现成用例**：`test_chars_in_rows_current_char_tint_reuses_one_frame`
（[tests:8916](../../tests/run_gql_tests.py)）。它特意加了个排在当前号前面的 `0号`，并断言底色
「**且不是第 0 行**」（:8943 `if y != rows[0][2] or y == 0:`）—— 新口径下当前号**就该**在第 0 行，
这条必红。改法（只动这一处判据和它的文件头那句"免得蒙对"）：

```lua
    if y != rows[0][2] or y ~= 0:
        return fail(name, f"底色该亮在当前号那一行，且当前号现在就该排第 0 行（行顶 {rows[0][2]}）: {y}")
```

同时把夹具注释改成实话：加这个号是为了钉"底色跟着**排序后的**当前号那一行走，不是钉死某个号"。
"不许蒙对"的精神由 T5-4 那条 y=0 + 第一行文本必须含当前号名共同承担（光硬编码 y 会被文本断言抓出来）。

**注册**：6 条都要写进 [tests:23414](../../tests/run_gql_tests.py) 那份 `tests = [...]` 清单
（:529 那条静态守卫会抓漏登记）。总数 524 → **530**。

### T6 三门 + 变异检查

- 三门：`python tests/run_gql_tests.py`（530/530）、`python tests/run_gql_tests.py --syntax`、
  `python tests/run_gql_tests.py -k lua_upvalue_budget`。
  T3 把 :1776 那三行搬早、T3 ④ 把竖排那三行删掉 —— `Render` 的 upvalue 只减不增（净变化 0 个新名字），
  `RenderCharsInRows` 少捕获 `GQL.Sync` 一处。静态守卫红了就说明搬错了地方。
- 变异检查（要求逐条咬红；二进制改完逐条 md5 还原）：
  ① 删掉 T1 的优先分支 → 用例 1/2/3/4/5 全红；
  ② 只改 `MatrixColumns`、把 `Dump.lua:48` 的 `curKey` 去掉 → 用例 5 红（两处不同序）；
  ③ 漏删 T3 ④（竖排继续自己问）→ 用例 4 的"只问 1 次"红；
  ④ 把 `L.MatrixRows` 里那句也改成 `L.Characters(db, curKey)` → **应当全绿**，
     这条是探针，用来证明"那一处真的不看序"（红了就说明我判断错了，得回头看它到底在不在乎序）。

## 三、本轮不动的

`db.chars` 的 key 与全部数据、schema 版本、迁移（本轮没有数据变更）；准入门/门槛/到期/编号判据；
`stale` 的判据与"2 字+过期"的写法；列宽 72、行高 20、帧宽/帧高公式；设置页两颗开关；
`Mx.Toggle` 的归零（已经是现状）；不 push。

## 四、待他点头的一处

**CHANGELOG 与版本号**：上一轮并进 0.6.2 是因为「他还没发出去」。本轮往哪个版本挂
（继续并 0.6.2 / 开 0.6.3），以及 CHANGELOG 那句玩家话的措辞，等他一句话再写；
计划不擅自改 `Core.lua` 的 `Version` 与 toc。

## 五、完成记录（做完再填）

**改了**：
**没改的**：
**用例增减**：
**三门**：
**变异检查**：
**主 agent 复核**：
**入库**：
**卡口（用户实机）**：号多的那个 CD 打开矩阵 —— 横排第一眼是自己那一列、竖排第一眼是自己那一行、
`/gql dump` 汇总第一行是自己；拖到最右边关掉再重开，回到最左。
