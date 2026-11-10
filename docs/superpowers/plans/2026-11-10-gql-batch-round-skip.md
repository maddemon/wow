# 批量"本轮不做"（整行手势）+ 竖排角色名放开 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:executing-plans / subagent-driven-development 逐任务实施。
> 每个任务由**一个全新的 subagent** 独立完成，主 agent 逐行 review 后自己提交。步骤用 `- [ ]` 勾选。

**Goal:** 右键矩阵里"任务名那一格"一次把这一行所有还没做的角色标成"本轮不做"（含批量撤销），
两向都弹确认框；顺带把竖排左窗格被砍成 4 字的角色名放开。

**Architecture:** 全部落在显示层（`GoldQuestLedger/Matrix.lua`）——批量只是一次手势，
写的还是格子右键那同一笔记录（`Ledger.SetManual(..., "skip", ...)`，自带实例编号 `k`，
换轮在读侧自动作废）。Ledger / Sync / 存档结构 / schema 一个字不改。
确认框用客户端自带的 `StaticPopup`，这是插件里第一处确认框。

**Tech Stack:** 零依赖 Lua 5.1（WoW 12.1.5 客户端）；离线替身测试 = Python + lupa
（`tests/run_gql_tests.py`，24k 行、约 430 条用例、单次全量约 2.5 分钟）。

设计出处：[2026-11-10-batch-round-skip-design.md](../specs/2026-11-10-batch-round-skip-design.md)

## Global Constraints（每个任务都隐含这一节）

- **存档零变化**：不新增字段、不升 schema、不写迁移。写只能走
  [`Ledger.SetManual`](../../GoldQuestLedger/Ledger.lua)（`state` ∈ `done`/`skip`/`open`，
  `open` = 清除），判据只能走 `Ledger.State` / `Ledger.InstanceKey` / `Ledger.SameInstanceKey`
  / `Ledger.EffectiveExpiry`。
- **判据只有一处**：批量"按什么算没做"必须逐字等于格子上显示的那份状态
  （`row.state`，家族行已由 `Ledger.BestRegisteredState` 折成最强一条）。
  显示层**不许**再拼第二套 `db.manual` 遍历。
- **点击路径零新增接口调用**：从"右键"到"框弹出来"这一段，版面一次都不许重算
  （`MatrixView.Groups` 计数增 0），客户端接口只允许多问**一次时钟**
  （`Api.WallClock` → 退回 `Api.Uptime`）——这是设计文档"一次点击只取一次读数"那一行。
  悬停路径一题不许多问。只有"点确认"那一步允许重算版面（那是设计要求的安全门，见 §二）。
- **`Mx.Render` 贴着 Lua 5.1 的 60 upvalue 上限**（[Matrix.lua:56-58](../../GoldQuestLedger/Matrix.lua)）：
  新代码一律挂 `Mx` 模块表（`function Mx.XXX`），**不许新增文件级 local、也不许新增全局变量**，
  否则要么炸加载要么炸静态守卫（`test_lua_upvalue_budget` 每次都跑）。
- **界面文字不许出现取证措辞**：`客户端 / 问不出 / 读到 / 实测 / 接口 / 凭据 / 账本 / 补扫 /
  同族 / 去哪办` 这十个词禁止出现在版面与 GameTooltip（用例
  `test_ui_text_no_forensic_words` 扫这三幕）。**确认框的文案也算界面文字**，受同一条约束。
  取证细节一律进 `Config.Log`。
- **fail-closed**：算不出实例编号就不写、不弹框；确认期间实例换了就不写。
  绝不"先亮个 ×，等真值到了再撤"。
- **新用例必须登记**：`tests/run_gql_tests.py` 末尾那个 `tests = [...]` 列表里没写进去的函数，
  `test_every_defined_case_is_registered_in_the_runner` 会红。
- 每个任务收尾跑一次带 `-k` 的过滤集，**T6 必须回到全量**。
- 提交只在本地，**不 push**。

## 文件结构（本轮动到的东西）

| 文件 | 责任 | 本轮改什么 |
| --- | --- | --- |
| `GoldQuestLedger/Matrix.lua` | 矩阵显示层 | 新增 `Mx.BatchCounts/BatchRowOf/BatchText/ClickClock/ApplyBatch/EnsureBatchDialog/ShowBatchConfirm/RowBatchClick`；`RowHoverButton`、`Head.Button` 收 `row` 并注册右键；`ShowMoneyTooltip` 加一句方向提示；竖排左窗格角色名去掉 4 字刀 |
| `tests/run_gql_tests.py` | 离线替身 + 用例 | 替身补 `StaticPopupDialogs` / `StaticPopup_Show` / `StaticPopup_Hide` / `StaticPopup_Visible` + 取证口 `GQL_ClickPopup`；新用例族 `row_batch_*` / `batch_row_of_*` / `row_right_click_*` / `chars_in_rows_*char_name*`；改掉两条已被本轮推翻的旧断言 |
| `GoldQuestLedger/Core.lua`、`GoldQuestLedger.toc`、`CHANGELOG.md` | 版本三处一致 | `0.6.5 → 0.6.6` |
| `Ledger.lua` / `Sync.lua` / `Config.lua` / `MatrixView.lua` | —— | **一行都不改**（改了就说明走偏了） |

### 实施前已经用替身跑实定死的事实（不用猜，照抄）

```
sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
  ⇒ Api.WallClock() = 0（开机秒域）  L.InstanceKey = "195:48"
     row.state = { ["小号一-永恒之井"] = "open" }   row.name = "任务195"
  ⇒ 整行热区那颗按钮：__questID = 195、__clicks = nil（今天确实没注册任何点击）

sync_env([(195, 30000000)], extra=("Matrix",))          # 不给 board
  ⇒ L.InstanceKey(195) = nil  ← "身份未定"那一档就用这个夹具，前提成立

sync_env([(195, 30000000)], board=43200, wall=1793880000)
  ⇒ SnapshotNow 后 rec.roundEnd = 1793923200，EffectiveExpiry 同值
     Config.FormatStamp(它) = "11-06 08:00"
  ⇒ 只有这一档弹框里才有"本轮到 …"那句；开机秒域下 EffectiveExpiry 回的是 43200
     那种小数字，印成日期就是谎报（Mx.BatchText 的 `> 1e9` 那道门正是拦它的）

替身里 GQLMatrixFrame:GetFrameStrata() 存在，矩阵打开后回 "DIALOG"
替身里 StaticPopupDialogs 与 StaticPopup_Show 今天都是 nil ⇒ T1 要补
```

几何与行号锚点（实施前先 `grep -n` 复核，代码在动，行号会漂）：

```
Matrix.lua:32     NAME_W=120 COL_W=72 ROW_H=20 GROUP_H=26
Matrix.lua:260-350  CellClick（右键那支 277-294 是本功能的样板；now 那段内联要提取成 Mx.ClickClock）
Matrix.lua:355-356  文件头那句"左窗格整行热区不在此列"——本轮要改口（T3）
Matrix.lua:427    local rowBtns, usedRowBtns = {}, 0
Matrix.lua:428-444 RowHoverButton（现只挂 OnMouseWheel）
Matrix.lua:481    ShowMoneyTooltip(anchor, questID, charKey)
Matrix.lua:663-667  "左键 切换完成 · 右键 切换不做"那一句所在的 if charKey 支
Matrix.lua:1450-1477 Head.Button（注释写着"可悬停、不可点击"）
Matrix.lua:1513-1531  ActivateMatrix / YieldToWindow（层号口径：DIALOG 只是"打开那一下"的临时抬高）
Matrix.lua:1868-1869 横排列头角色名（本轮不动）
Matrix.lua:1915  RowHoverButton(y, row.questID)   横排调用点
Matrix.lua:2181  Head.Button(cx - offX, row.questID) 竖排调用点
Matrix.lua:2201-2203 竖排左窗格角色名（T5 改这里）
Matrix.lua:2210  RowHoverButton(y, nil)           竖排左窗格热区（角色行，questID 恒 nil）
```

---

### Task 1：替身补确认弹窗 + 批量判据三兄弟（只算不写）

做完这件事，"这一行该往哪个方向动、动几个格、弹框上写什么"三件事已经有唯一一份答案，
而且能用离线用例钉住。这一档**还不写账、还不弹框**——那是 T2。

**Files:**
- Modify: `tests/run_gql_tests.py`（替身块 `GQL_MOCK_ENV`，在 `UISpecialFrames = {}` 那行之后插入；
  新用例与助手放在 `test_right_click_marks_a_cell_as_skipped` 之后；`tests = [...]` 列表登记）
- Modify: `GoldQuestLedger/Matrix.lua`（`Mx.BatchCounts` / `Mx.BatchRowOf` / `Mx.BatchText`）
- Test: 同文件内新用例 4 条

**Interfaces:**
- Consumes: `Ledger.MatrixRows` 产出的 `row = {questID, name, state = {[charKey]=状态}, remaining, allDone}`
  （见 [Ledger.lua:3062-3110](../../GoldQuestLedger/Ledger.lua)）、
  `MatrixView.Groups(db, now)` → `{ {mapID=…, rows={row,…}}, … }`
  （[MatrixView.lua:140](../../GoldQuestLedger/MatrixView.lua)）、
  `Ledger.EffectiveExpiry(db, rec, now)`、`Config.DB()`、`Config.FormatStamp(t)`
  （[Config.lua:105](../../GoldQuestLedger/Config.lua)）。
- Produces（后续任务只认这些名字）:
  - `Mx.BatchCounts(row) -> { total, done, skip, writable, mode }`，`mode ∈ "set"|"clear"|nil`
  - `Mx.BatchRowOf(db, now, questID) -> row|nil`（**只有确认那一步用它**，点击与悬停都不许用）
  - `Mx.BatchText(row, counts, now) -> string`（多行、`\n` 分隔；不再自己取时钟）

- [ ] **Step 1：给替身补 StaticPopup 那三件套**

插在 `tests/run_gql_tests.py` 的 `GQL_MOCK_ENV` 字符串里 `UISpecialFrames = {}` 之后（约 :38）。
**只复刻本功能用到的语义**，别造客户端没有的方法：

```lua
-- StaticPopup 确认弹窗（真客户端由 FrameXML 提供）。这里只复刻本轮确实用到的四条语义：
-- ① dialog.text 是**格式串**，Show 时按 format(text, text1, text2) 展开 —— 这条必须照抄，
--    它正是"任务名里带 % 会不会炸"那颗雷的唯一裁判；
-- ② data 原样带到按钮回调；③ 可见状态可查；④ Hide 与点按钮都会清可见。
-- 返回值对齐真客户端（回那个弹窗对象），但插件代码**不许**依赖它的形状。
-- 不许在这里发明 Show/Hide/Visible/两枚按钮之外的接口。
StaticPopupDialogs = StaticPopupDialogs or {}
GQL_POPUPS = {}
function StaticPopup_Show(index, text1, text2, data)
    local dialog = StaticPopupDialogs[index]
    if not dialog then return nil end
    local st = GQL_POPUPS[index]
    if not st then st = {}; GQL_POPUPS[index] = st end
    st.text = dialog.text
        and string.format(dialog.text, text1 or "", text2 or "") or (text1 or "")
    st.shown, st.data = true, data
    if dialog.OnShow then dialog.OnShow(dialog, data) end
    return st
end
function StaticPopup_Hide(index)
    local st = GQL_POPUPS[index]
    if not st or not st.shown then return end
    st.shown = false
    local dialog = StaticPopupDialogs[index]
    if dialog and dialog.OnHide then dialog.OnHide(dialog) end
end
function StaticPopup_Visible(index)
    local st = GQL_POPUPS[index]
    return (st and st.shown) and index or nil
end
-- 取证口（只给用例用）：按真客户端的调用形状点那枚按钮 —— fn(dialog, data)
function GQL_ClickPopup(index, which)
    local st, dialog = GQL_POPUPS[index], StaticPopupDialogs[index]
    if not st or not st.shown or not dialog then return false end
    local fn = (which == 1) and dialog.OnButton1 or dialog.OnButton2
    st.shown = false
    if fn then fn(dialog, st.data) end
    return true
end
```

- [ ] **Step 2：加共用助手 + 写 4 条失败用例**

助手放在 `click_cell`（约 :10222）之后；T2、T3、T4 的用例也共用它们。

```python
ROW_SKIP_DIALOG = "GQL_ROW_BATCH_SKIP"


def popup_row_batch(lua):
    """确认框现在什么样：{shown, text, mode, k, questID}。没弹过回 None。"""
    got = lua.execute(
        "local st = GQL_POPUPS and GQL_POPUPS[%r] "
        "if not st then return nil end "
        "return st.shown and 1 or 0, st.text or '', "
        "tostring(st.data and st.data.mode), tostring(st.data and st.data.k), "
        "tostring(st.data and st.data.questID)" % ROW_SKIP_DIALOG)
    if got is None:
        return None
    shown, text, mode, key, qid = got
    return {"shown": int(shown), "text": str(text), "mode": str(mode),
            "k": str(key), "questID": str(qid)}


def manual_mark(lua, char_key, qid):
    """这一号这一格在账上的手动标记（'none' = 没写过）。
    走 Lua 侧取，避开 lupa 直接索引缺失那张表时抛错。"""
    return str(one(lua.execute(
        "local m = GQLDB.manual and GQLDB.manual[%r] "
        "local r = m and m[%d] "
        "return r and tostring(r.state) or 'none'" % (char_key, qid))))


def log_text(lua):
    """Config.Log 的环形缓冲拼成一串。取证面 —— 界面文案不许用它。"""
    return str(one(lua.execute(
        "local s = '' for _, m in ipairs((GQLDB.logs) or {}) do "
        "s = s .. tostring(m) .. '|' end return s")))


def click_row(lua, qid, button="RightButton"):
    """点任务名那一格（横排左窗格的整行热区）：走热区自己的 OnClick，也就是玩家真点下去那条路。
    只看可见那颗（池子里多余的热区带着旧 questID，点它测的是哑炮）。
    竖排左窗格那一排热区是角色行（__questID 恒 nil），竖排走 click_head。"""
    return bool(lua.execute(
        "for _, c in ipairs(GQLMatrixContentL.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s and c.__shown and c.__questID == %d then s(c, %r) return true end "
        "end return false" % (qid, button)))


def click_popup(lua, which=1):
    """which=1 点确认、2 点取消（真客户端那两枚按钮）。"""
    return bool(lua.execute("return GQL_ClickPopup(%r, %d)" % (ROW_SKIP_DIALOG, which)))
```

第 1 条（判据表 = 设计文档 §一 那张表，逐档钉）：

```python
def test_row_batch_counts_follow_the_displayed_row_state():
    """批量「按什么算没做」**只认格子上显示的那份 row.state**，判据表逐档钉死：
      done → 永不动 · skip → 记时不动（不重写）、撤时清掉 · 其余（open/未知/到期）→ 可写。
    方向只有一条：有可写的 ⇒ 记；没有可写的但有 × ⇒ 撤；两样都没有 ⇒ 无事可做（不弹框）。
    两面顺带钉住：
    ① 家族行不需要单独一套判据 —— 全族折出来的 done 在这里就是一格 done
      （折叠本身归 Ledger.BestRegisteredState，它有自己的用例；这里再拼一套就是两套判据）；
    ② 一个号都没有（矩阵空态）= 无事可做，不是「记」。"""
    name = "row_batch_counts_follow_the_displayed_row_state"
    lua, gql, _ = sync_env([], extra=("Matrix",))
    counts = gql.Matrix.BatchCounts

    def shape(state):
        row = lua.table_from({"questID": 195, "name": "任务195"})
        row["state"] = lua.table_from(state)
        return counts(row)

    cases = [
        # (格子状态表, 期望 总数/完成/不做/可写, 期望方向)
        ({"A": "done", "B": "skip", "C": "open", "D": "unknown"},
         (4, 1, 1, 2), "set"),
        ({"A": "done", "B": "skip", "C": "skip"}, (3, 1, 2, 0), "clear"),
        ({"A": "done", "B": "done"}, (2, 2, 0, 0), None),
        ({"A": "skip"}, (1, 0, 1, 0), "clear"),
        ({"A": "open"}, (1, 0, 0, 1), "set"),
        ({}, (0, 0, 0, 0), None),
    ]
    for state, want, mode in cases:
        c = shape(state)
        got = (int(c["total"]), int(c["done"]), int(c["skip"]), int(c["writable"]))
        if got != want or str(c["mode"]) != str(mode):
            return fail(name, "%s 期望 总数/完成/不做/可写 %s 方向 %s，实际 %s %s"
                              % (state, want, mode, got, c["mode"]))
    # 纯查表：判据一次都不许碰客户端
    if not bool(lua.execute(
            "local n = 0 GQL.Api.WallClock = function() n = n + 1 return 0 end "
            "local r = { questID = 1, name = 'x', state = { a = 'open' } } "
            "GQL.Matrix.BatchCounts(r) return n == 0")):
        return fail(name, "BatchCounts 不许问任何接口")
    ok(name)
```

第 2 条（弹框文案的数字与到期那一句）：

```python
def test_row_batch_text_states_the_affected_cells():
    """确认框那几句只说玩家这一眼要用的：哪条任务、本轮到几点、动了几个格。
    钉三面：① 数字与判据表同源（传同一份 counts，文案不许自己再数一遍）；
    ② 「本轮到 …」这一句**只在墙上钟域**才出现（开机秒 43200 印成日期是谎报，宁缺不编）；
    ③ 界面措辞：取证那十个词一个都不许出现在文案里。"""
    name = "row_batch_text_states_the_affected_cells"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, wall=1793880000,
                           extra=("Matrix",))
    gql.Sync.SnapshotNow()          # 让 195 这一条有 rec.roundEnd（到期那一句的来源）
    text = str(one(lua.execute(
        "local r = { questID = 195, name = '镜宫悬赏', "
        "  state = { A = 'done', B = 'skip', C = 'open' } } "
        "local c = GQL.Matrix.BatchCounts(r) "
        "return GQL.Matrix.BatchText(r, c, GQL.Api.WallClock())")))
    for want in ("镜宫悬赏", "1 格记不做", "1 格已完成保持 ✓", "1 格已是 × 不动"):
        if want not in text:
            return fail(name, "记那一句该有 %r，实际:\n%s" % (want, text))
    if "本轮到" not in text:
        return fail(name, f"墙上钟域该报出本轮到几点，实际:\n{text}")
    clear = str(one(lua.execute(
        "local r = { questID = 195, name = '镜宫悬赏', "
        "  state = { A = 'done', B = 'skip', C = 'skip' } } "
        "local c = GQL.Matrix.BatchCounts(r) "
        "return GQL.Matrix.BatchText(r, c, GQL.Api.WallClock())")))
    if "撤销" not in clear or "2 枚 ×" not in clear:
        return fail(name, f"撤那一句该说清撤几枚 ×，实际:\n{clear}")
    if "含你以前单独点的" not in clear:
        return fail(name, f"撤那一句要提前说清清的是全部 ×（含单独点过的），实际:\n{clear}")
    # 开机秒域：EffectiveExpiry 回的是 43200 那种小数字，印成日期 = 谎报 ⇒ 整句不写
    uptime = str(one(lua.execute(
        "local r = { questID = 195, name = '镜宫悬赏', state = { A = 'open' } } "
        "local c = GQL.Matrix.BatchCounts(r) "
        "return GQL.Matrix.BatchText(r, c, 0)")))
    if "本轮到" in uptime:
        return fail(name, f"开机秒不许印成日期，实际:\n{uptime}")
    for word in ("客户端", "问不出", "读到", "实测", "接口", "凭据", "账本",
                 "补扫", "同族", "去哪办"):
        if word in text or word in clear:
            return fail(name, f"确认框是界面，不许出现取证措辞 {word!r}")
    ok(name)
```

第 3 条（按任务号取当前版面那一行 —— 确认那一步要用它重新解析）：

```python
def test_batch_row_of_reads_the_row_currently_painted():
    """`Mx.BatchRowOf` 是**确认那一步**的重新解析：走 MatrixView.Groups（和渲染同一份，
    含关注地图与「隐藏全部完成」两道显示过滤），于是批量看到的格子状态与玩家眼前看到的逐字相同。
    钉两面：① 找得到的任务号回的就是版面里那一行本身（同一张表，不是复算的第二份）；
    ② 版面里没有的（被过滤掉的、压根没这个号）回 nil —— 由调用方走 fail-closed。"""
    name = "batch_row_of_reads_the_row_currently_painted"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    same = bool(lua.execute(
        "local now = GQL.Api.WallClock() or 0 "
        "local gs = GQL.MatrixView.Groups(GQLDB, now) local painted "
        "for gi = 1, #gs do for _, r in ipairs(gs[gi].rows or {}) do "
        "  if r.questID == 195 then painted = r end end end "
        "local r = GQL.Matrix.BatchRowOf(GQLDB, now, 195) "
        "return r ~= nil and r == painted"))
    if not same:
        return fail(name, "BatchRowOf 该回当前版面里那一行本身（同一份 state）")
    missing = str(one(lua.execute(
        "return tostring(GQL.Matrix.BatchRowOf(GQLDB, GQL.Api.WallClock() or 0, 999999))")))
    if missing != "nil":
        return fail(name, f"版面里没有的号该回 nil，实际 {missing}")
    ok(name)
```

第 4 条（文案里带 `%` 的任务名不许把弹框炸掉）：

```python
def test_row_batch_text_survives_a_percent_in_the_quest_name():
    """替身照抄了真 FrameXML 那一层 format(dialog.text, text1, text2) —— 所以这一条测的是
    客户端真会走的路：格式串必须是固定的 "%s"，内容走 text1。
    任务名里带 % 时若把名字拼进 dialog.text，format 会把它当格式串解释 = 报错或错字。"""
    name = "row_batch_text_survives_a_percent_in_the_quest_name"
    lua, gql, _ = sync_env([], extra=("Matrix",))
    shown = str(one(lua.execute(
        "local r = { questID = 7, name = '100% 奖励', state = { A = 'open' } } "
        "local c = GQL.Matrix.BatchCounts(r) "
        "local txt = GQL.Matrix.BatchText(r, c, 0) "
        "GQL.Matrix.EnsureBatchDialog() "
        "local st = StaticPopup_Show('GQL_ROW_BATCH_SKIP', txt, nil, "
        "  { questID = 7, k = '7:1', mode = c.mode }) "
        "return st and st.text or '<没弹出来>'")))
    if "100% 奖励" not in shown:
        return fail(name, f"名字里的百分号该原样显示，实际:\n{shown}")
    ok(name)
```

> 第 4 条要调 `GQL.Matrix.EnsureBatchDialog` —— 那个函数 T2 才写。本任务里它**故意**是红的：
> 预期报 `attempt to call a nil value`。四条 RED 的形状一样（`BatchCounts`/`BatchText`/
> `BatchRowOf` 还不存在）。不许为了少一条 RED 把 T2 的代码提前搬上来。

- [ ] **Step 3：跑用例确认它们红**

Run: `python tests/run_gql_tests.py -k row_batch_counts_follow,batch_text_states,batch_row_of_reads,batch_text_survives`
Expected: 四条全 FAIL（报 `attempt to call a nil value 'BatchCounts'` 这一类）

- [ ] **Step 4：实现三个判据函数**

4a. 在 `Matrix.lua` 的 `local rowBtns, usedRowBtns = {}, 0`（约 :427）**上一行**加注释
（只加注释，不新增 local —— 函数全挂 `Mx`）：

```lua
--- 批量"本轮不做"（2026-11-10）：入口是任务名那一格的右键，实现全挂在 Mx 模块表上
--- （见下面 FormatRemaining 之后的那一节）。这里**不新增文件级 local** —— 本文件的
--- Mx.Render 正贴着 60 upvalue / 主块 200 local 两道天花板，多一个就整文件加载失败。
```

4b. 在 `FormatRemaining = function(sec)` 那个定义**结束之后**（约 :700，紧跟它后面）插入：

```lua
-- ===================== 批量"本轮不做"（任务名那一格右键）=====================
-- 用户 2026-11-10：整条任务本轮所有角色都不做，可现在只能一个一个格子点。
-- 这一档只是**手势**：写的还是格子右键那同一笔记录（Ledger.SetManual 的 state="skip"，
-- 自带实例编号 k，换轮在读侧自动作废）。不新增存档字段、不新增判据。
-- 全挂 Mx：本文件的主块 local 数量与 Mx.Render 的 upvalue 都顶着上限，加文件级
-- local 会让整个插件加载失败（见文件顶部那两段说明与 test_lua_upvalue_budget）。

--- 这一行的批量方向与影响面：**只认格子上显示的那份状态**（row.state，家族行已由
--- Ledger.BestRegisteredState 折成最强一条）。done 不动；已是 skip 的不重写（一刷时间戳
--- 就把玩家单独点的那笔取证改成了"我批量点的"）；其余（空 / ? / 在做 / 已到期）算可写。
--- 有可写的 ⇒ 这一枪是"记"；没有可写的但还有 × ⇒ 这一枪是"撤"；两样都没有 ⇒ 无事可做。
--- 纯查表，零接口调用（判据表见 test_row_batch_counts_follow_the_displayed_row_state）。
function Mx.BatchCounts(row)
    local counts = { total = 0, done = 0, skip = 0, writable = 0 }
    local state = row and row.state
    if state then
        for _key, shown in pairs(state) do
            counts.total = counts.total + 1
            if shown == "done" then counts.done = counts.done + 1
            elseif shown == "skip" then counts.skip = counts.skip + 1
            else counts.writable = counts.writable + 1 end
        end
    end
    counts.mode = (counts.writable > 0 and "set")
        or (counts.skip > 0 and "clear") or nil
    return counts
end

--- 按任务号在**当前版面**里找那一行：走 MatrixView.Groups（和渲染同一份，含两道显示过滤），
--- 于是批量看到的格子状态与玩家眼前看到的逐字相同。
--- **只有点确认那一步用它**（框停在屏上期间玩家可能 /reload、可能换轮、这行可能消失，
--- 必须重新解析）；点击与悬停都不许用 —— 那两条路要零重算，见 Mx.RowBatchClick 与提示那一句。
function Mx.BatchRowOf(db, now, questID)
    local groups = (GQL.MatrixView and GQL.MatrixView.Groups)
        and GQL.MatrixView.Groups(db, now) or nil
    if not groups then return nil end
    for gi = 1, #groups do
        local rows = groups[gi].rows or {}
        for ri = 1, #rows do
            if rows[ri].questID == questID then return rows[ri] end
        end
    end
    return nil
end

--- 确认框那几句。界面只说玩家这一眼要用的：哪条任务、本轮到几点、动了几个格。
--- · 地图名那一句不做：行名本来就是按名字去重的（Ledger.MatrixRows 那张分数表），
---   而 GroupLabel/MapName 要为这一句问一次客户端 —— 一句话换一次接口，不划算。
--- · 到期只在**墙上钟域**才写：开机秒印成日期就是谎报（EffectiveExpiry 在开机秒域
---   回的是 43200 那种小数字）。问不到整句不写，宁缺不编。
--- · `now` 由调用方传进来：一次点击只取一次读数，文案不许再问一遍时钟。
--- · 措辞过 test_ui_text_no_forensic_words：那十个取证词一个都不许进来。
function Mx.BatchText(row, counts, now)
    local name = row.name or ("世界任务#" .. tostring(row.questID))
    local lines = {}
    if counts.mode == "clear" then
        lines[#lines + 1] = name .. "  撤销“这一条本轮所有角色都不做”？"
    else
        lines[#lines + 1] = name .. "  这一条本轮所有角色都不做？"
    end
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[row.questID]
    local exp = (GQL.Ledger and GQL.Ledger.EffectiveExpiry)
        and GQL.Ledger.EffectiveExpiry(db, rec, now) or nil
    if type(exp) == "number" and exp > 1e9 and GQL.Config.FormatStamp then
        local stamp = GQL.Config.FormatStamp(exp)
        if stamp then lines[#lines + 1] = "本轮到 " .. stamp end
    end
    if counts.mode == "clear" then
        lines[#lines + 1] = ("清掉这一行上 %d 枚 ×（含你以前单独点的），%d 格已完成保持 ✓ 不动")
            :format(counts.skip, counts.done)
    else
        lines[#lines + 1] = ("%d 个号：%d 格记不做，%d 格已完成保持 ✓，%d 格已是 × 不动")
            :format(counts.total, counts.writable, counts.done, counts.skip)
    end
    return table.concat(lines, "\n")
end
```

（`“ ”` 是字面 UTF-8 全角引号，与本文件其余中文文案同一写法。**Lua 5.1 不认 `\u{201c}`
那种转义**，别写成转义。）

- [ ] **Step 5：登记用例并跑绿**

`tests = [...]` 列表里 `test_right_click_marks_a_cell_as_skipped,` 之后加四行：

```python
    test_row_batch_counts_follow_the_displayed_row_state,
    test_row_batch_text_states_the_affected_cells,
    test_batch_row_of_reads_the_row_currently_painted,
    test_row_batch_text_survives_a_percent_in_the_quest_name,
```

Run: `python tests/run_gql_tests.py -k row_batch,batch_row_of,upvalue,every_defined_case`
Expected: 四条新用例 PASS，`test_every_defined_case_is_registered_in_the_runner` 与
`test_lua_upvalue_budget` 也 PASS

- [ ] **Step 6：提交**

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "feat: GQL 批量本轮不做 T1（判据只认版面显示的那份状态 + 弹框文案）"
```

---

### Task 2：横排整行右键全链（弹框 → 确认 → 批量写/清）

做完这件事，横排已经能用了：右键任务名 → 确认框 → 这一行没做的格子全变 ×；再右键 → 确认 → 全清。

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua`（`Mx.ClickClock/ApplyBatch/EnsureBatchDialog/ShowBatchConfirm/RowBatchClick`；
  `CellClick` 里内联取时钟那段改调 `Mx.ClickClock`；`RowHoverButton` 收 `row` 并注册右键；横排调用点）
- Modify: `tests/run_gql_tests.py`（新用例 6 条；`tests = [...]` 登记）

**Interfaces:**
- Consumes: T1 的 `Mx.BatchCounts(row)` / `Mx.BatchRowOf(db, now, questID)` /
  `Mx.BatchText(row, counts, now)`、`Ledger.SetManual(db, charKey, questID, state, t, g, v)`、
  `Ledger.InstanceKey(db, questID, rec, now)`、`Api.WallClock()` / `Api.Uptime()`、
  `Config.Log(msg)`、替身的 `StaticPopupDialogs` / `StaticPopup_Show` / `StaticPopup_Visible` /
  `GQL_ClickPopup`。
- Produces:
  - `Mx.ClickClock() -> now|nil`（格子那一路和整行那一路共用同一个 now 口径）
  - `Mx.ApplyBatch(data) -> written(number)`
  - `Mx.ShowBatchConfirm(text, data) -> shown(bool)`，`data = { questID, k, mode }`
  - `Mx.RowBatchClick(row) -> 无返回值`（两颗按钮 `OnClick` 的唯一入口；**收的是渲染时那份 row**）
  - 弹窗条目名字面量 `"GQL_ROW_BATCH_SKIP"`（**不新增全局变量**）

- [ ] **Step 1：写主用例**

（助手 `click_row` / `popup_row_batch` / `manual_mark` / `log_text` / `click_popup` 在 T1 已经建好。）

```python
def test_row_right_click_skips_and_restores_the_whole_row():
    """右键任务名 = 这一条本轮所有角色都不做（用户 2026-11-10：现在只能一个一个格子点）。
    钉六件事：
    ① 确认之后，这一行上「既不是 ✓ 也不是 ×」的格子全变 ×；
    ② 已完成那格**永远不动**（× 不许盖掉真凭据，同格子右键那条口径）；
    ③ 已经是 × 的那格不重写（时间戳一个字节都不许变，免得日志和取证被批量点击刷脏）；
    ④ 再右键一次 = 批量撤销，清掉这一行上**所有** ×，含以前单独点的（用户定的「常规思维」）；
    ⑤ 框没确认之前一格都不许动（取消那一档另有用例）；
    ⑥ 文案报的数字与真落账的格数一致（spec §六 7），层级那一笔留进日志。"""
    name = "row_right_click_skips_and_restores_the_whole_row"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    me = str(gql.Sync.CurrentCharKey())            # 小号一-永恒之井
    solo, did = "小号二-永恒之井", "小号三-永恒之井"
    for alt in (solo, did):
        lua.execute("GQLDB.chars[%r] = { lastSync = 1000, views = {} }" % alt)
    gql.Matrix.Render()
    # 前提形状：solo 以前单独点过 ×，did 已经做完了，me 还空着
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, solo, 195, "RightButton"):
        return fail(name, "前提：点不到 solo 那一格")
    if not click_cell(lua, did, 195):
        return fail(name, "前提：点不到 did 那一格（左键记完成）")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "前提：当前号这一格本来没有手动标记")
    solo_stamp = str(g.GQLDB["manual"][solo][195]["t"])
    gql.Matrix.Render()
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格（整行热区没注册右键？）")
    tip = popup_row_batch(lua)
    if not tip or tip["shown"] != 1:
        return fail(name, f"右键任务名该弹确认框，实际 {tip}")
    if tip["mode"] != "set":
        return fail(name, f"这一行还有没做的格子，方向该是 set，实际 {tip['mode']}")
    if "195" not in tip["questID"]:
        return fail(name, f"弹窗 data 要带任务号，实际 {tip['questID']}")
    for want in ("1 格记不做", "1 格已完成保持 ✓", "1 格已是 × 不动"):
        if want not in tip["text"]:
            return fail(name, f"文案要把影响面说清（缺 {want}），实际:\n{tip['text']}")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "确认之前不许提前落账")
    if not click_popup(lua, 1):
        return fail(name, "点不到确认按钮")
    st = {c: state1(gql.Ledger.State, g.GQLDB, c, 195) for c in (me, solo, did)}
    if st[me] != "skip" or st[solo] != "skip" or st[did] != "done":
        return fail(name, f"确认后该是 ×/×/✓，实际 {st}")
    if manual_mark(lua, me, 195) != "skip":
        return fail(name, "文案报「1 格记不做」，实际落账的不止那一格")
    if str(g.GQLDB["manual"][solo][195]["t"]) != solo_stamp:
        return fail(name, "已经是 × 的那格不许被重写（时间戳变了）")
    log = log_text(lua)
    if "改了 1 格" not in log or "1 格已完成未动" not in log:
        return fail(name, f"日志要报改动格数与跳过几格完成，实际:\n{log}")
    if "矩阵层=DIALOG→MEDIUM" not in log:
        return fail(name, f"弹框前矩阵该让回 MEDIUM，并把这一笔层号留证，实际:\n{log}")
    if str(one(lua.execute("return GQLMatrixFrame:GetFrameStrata()"))) != "MEDIUM":
        return fail(name, "确认框弹出来时矩阵还占着 DIALOG，框会被压在后面")
    # 再右键：这一行没有可写的了 ⇒ 方向翻成撤销，且撤销连以前单独点的那枚一起清
    if not click_row(lua, 195):
        return fail(name, "撤销时点不到任务名那一格")
    tip = popup_row_batch(lua)
    if tip["mode"] != "clear":
        return fail(name, f"全行都是终态时该是撤销，实际 {tip['mode']}")
    if "撤销" not in tip["text"] or "2 枚 ×" not in tip["text"]:
        return fail(name, f"撤销那句要报清撤几枚、含单独点过的，实际:\n{tip['text']}")
    if not click_popup(lua, 1):
        return fail(name, "撤销时点不到确认")
    st = {c: state1(gql.Ledger.State, g.GQLDB, c, 195) for c in (me, solo, did)}
    if st[me] != "open" or st[solo] != "open":
        return fail(name, f"撤销要把 ×× 全清（含以前单独点的），实际 {st}")
    if st[did] != "done":
        return fail(name, f"撤销不许动已完成那格，实际 {st}")
    ok(name)
```

- [ ] **Step 2：再写三条 fail-closed 与两条弹窗脾气**

```python
def test_row_right_click_cancel_writes_nothing():
    """取消（ESC 与点外面走的都是这同一枚按钮的回调）一个字不写，日志留一行。
    确认框的意义就是「这一枪还没打」—— 弹出来了但没确认，存档不许有任何变化。"""
    name = "row_right_click_cancel_writes_nothing"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    if popup_row_batch(lua) is None:
        return fail(name, "前提：确认框该弹出来")
    if not click_popup(lua, 2):
        return fail(name, "点不到取消按钮")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "取消这一枪一个字都不许写")
    if state1(gql.Ledger.State, gql.Config.DB(), me, 195) != "open":
        return fail(name, "取消后这一格的状态不许变")
    if popup_row_batch(lua)["shown"] != 0:
        return fail(name, "取消后框要收掉")
    if "取消" not in log_text(lua):
        return fail(name, f"取消也要留一行日志:\n{log_text(lua)}")
    ok(name)


def test_row_right_click_ignores_a_second_click_while_open():
    """同一格连点两次不许排两个框，也不许换掉第一个框的文案与编号
    （真客户端重放同一个条目会覆盖文案 —— 那时确认掉的可能是第二次那一枪）。"""
    name = "row_right_click_ignores_a_second_click_while_open"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_row(lua, 195) or not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    if "确认框还开着" not in log_text(lua):
        return fail(name, f"第二次点击该被忽略并留日志:\n{log_text(lua)}")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "被忽略那一枪不该已经落账")
    if not click_popup(lua, 1):
        return fail(name, "点不到确认")
    if manual_mark(lua, me, 195) != "skip":
        return fail(name, "第一次那一枪该还能确认")
    ok(name)


def test_row_right_click_refuses_when_the_row_is_all_done():
    """整行本来就已经全 ✓ ⇒ 无事可做：不弹框、不写账、日志一行说明（spec §一 与 §六 4）。
    注意反例：**整行全空不是无事可做** —— 全空 = 全是可写的 = 走「记」，那一档在主用例里。"""
    name = "row_right_click_refuses_when_the_row_is_all_done"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    if not click_cell(lua, me, 195):
        return fail(name, "前提：点不到那一格（左键记完成）")
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    if popup_row_batch(lua) is not None:
        return fail(name, "整行已完成不该弹确认框")
    if manual_mark(lua, me, 195) != "done":
        return fail(name, "无事可做这一枪不许改动任何一格")
    if "没有可改的格子" not in log_text(lua):
        return fail(name, f"要报得出为什么没弹框:\n{log_text(lua)}")
    ok(name)


def test_row_right_click_refuses_when_the_identity_is_undetermined():
    """算不出这一条的唯一编号（没有板面倒计时那一档）⇒ 连确认框都不弹、一个字不写。
    比格子右键**有意更严**（那一档照写、只是画不出来）：批量一枪十几格，
    不许往存档塞十几笔永远画不出来的隐形记录。实施不许为了「两处一致」把它对齐回去。"""
    name = "row_right_click_refuses_when_the_identity_is_undetermined"
    lua, gql, _ = sync_env([(195, 30000000)], extra=("Matrix",))   # 不给 board = 没有身份来源
    g = lua.globals()
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    now = int(one(lua.execute("return GQL.Api.WallClock() or 0")))
    if uid(gql.Ledger, g.GQLDB, 195, now) is not None:
        return fail(name, "前提：这一条该算不出编号（夹具变了）")
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    if popup_row_batch(lua) is not None:
        return fail(name, "身份未定不该弹确认框")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "身份未定不许落任何一笔不做（界面上画不出来的隐形记录）")
    if "身份未定" not in log_text(lua):
        return fail(name, f"要报得出为什么没写:\n{log_text(lua)}")
    ok(name)


def test_row_right_click_drops_the_batch_when_the_round_rolled():
    """确认框停在屏上期间板面换成另一条实例 ⇒ 整批作废。
    不卡这道门就等于把玩家对**上一条**实例的意向，静悄悄盖到刚刷新的新实例上。
    顺带钉 spec §六 8 的后半句：旧那一格也不许被顺手改动。"""
    name = "row_right_click_drops_the_batch_when_the_round_rolled"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    tip = popup_row_batch(lua)
    if not tip or tip["shown"] != 1:
        return fail(name, f"前提：确认框该弹出来，实际 {tip}")
    # 板面换成下一条实例（结束时刻整段挪走），走真实同步入口把新身份写进账
    lua.execute("GQL_BOARD['end'] = GQL_BOARD['end'] + 1800")
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 195,
           int(one(lua.execute("return GQL.Api.WallClock() or 0")))) == tip["k"]:
        return fail(name, "前提：编号该已经换了，不然这一条是空跑")
    if not click_popup(lua, 1):
        return fail(name, "点不到确认")
    if manual_mark(lua, me, 195) != "none":
        return fail(name, "换轮后确认这一格一个字都不许写")
    if "本轮已换" not in log_text(lua):
        return fail(name, f"要报得出为什么没写:\n{log_text(lua)}")
    ok(name)
```

- [ ] **Step 3：跑用例确认它们红**

Run: `python tests/run_gql_tests.py -k row_right_click`
Expected: 六条全 FAIL（主用例报"点不到任务名那一格（整行热区没注册右键？）"，
其余报 `attempt to call a nil value 'RowBatchClick'` 一类）；T1 那四条仍 PASS

- [ ] **Step 4：实现写入与入口**

4a. 把 `CellClick` 里内联的那段取时钟改成调 `Mx.ClickClock`（判据一字未动，DRY）：把

```lua
    local now = GQL.Api and GQL.Api.WallClock() or nil
    if not now and GQL.Api and GQL.Api.Uptime then
        now = math.floor(GQL.Api.Uptime())
    end
```

替换成

```lua
    local now = Mx.ClickClock()
```

4b. 在 T1 那一节（`Mx.BatchText` 之后）接着写：

```lua
--- 一次点击只问一次时钟：墙上钟优先，问不到退回开机秒。
--- 原来这段内联在 CellClick 里，现在格子那一路和整行那一路共用同一个 now 口径。
function Mx.ClickClock()
    local now = GQL.Api and GQL.Api.WallClock() or nil
    if not now and GQL.Api and GQL.Api.Uptime then
        now = math.floor(GQL.Api.Uptime())
    end
    return now
end

--- 确认之后那一批写。判据与方向都在 data 里带过来，但**状态一律重新解析**：
--- 框停在屏上这段时间里玩家可能 /reload、可能换轮、这行可能消失。
--- 三道门任意一道拦住就一个字都不写（fail-closed），并在日志里说清为什么。
function Mx.ApplyBatch(data)
    local db = GQL.Config.DB()
    if not db or not data or not data.questID then return 0 end
    local now = Mx.ClickClock()
    local row = Mx.BatchRowOf(db, now, data.questID)
    if not row then
        GQL.Config.Log(("批量不做：这行已不在矩阵，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    local rec = db.quests and db.quests[data.questID]
    local key = (GQL.Ledger and GQL.Ledger.InstanceKey)
        and GQL.Ledger.InstanceKey(db, data.questID, rec, now) or nil
    if not key then
        GQL.Config.Log(("批量不做：这一条身份未定，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    if key ~= data.k then
        -- 框开着的时候板面换成了另一条实例：这一枪许的是上一条，不许盖到新的上面
        GQL.Config.Log(("批量不做：本轮已换，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    local counts = Mx.BatchCounts(row)
    local written = 0
    for charKey, shown in pairs(row.state or {}) do
        if data.mode == "set" and shown ~= "done" and shown ~= "skip" then
            GQL.Ledger.SetManual(db, charKey, data.questID, "skip", now, nil)
            written = written + 1
        elseif data.mode == "clear" and shown == "skip" then
            GQL.Ledger.SetManual(db, charKey, data.questID, "open", now, nil)
            written = written + 1
        end
    end
    GQL.Config.Log(("批量%s quest=%s 改了 %d 格（共 %d 号，%d 格已完成未动，%s 编号）")
        :format(data.mode == "clear" and "撤销" or "不做", tostring(data.questID),
            written, counts.total, counts.done, tostring(key)))
    Mx.Render()
    return written
end

--- 弹窗条目只建一次（本插件第一处确认框）。两个方向复用同一条目、只换文案与 data。
--- text 固定 "%s"：StaticPopupDialogs[*].text 是**格式串**，真客户端会 format(text, text1, text2)，
--- 任务名里万一有个 % 直接被当格式串解释 = 报错或错字，所以内容一律走 text1。
function Mx.EnsureBatchDialog()
    if type(StaticPopupDialogs) ~= "table" then return false end
    if StaticPopupDialogs["GQL_ROW_BATCH_SKIP"] then return true end
    StaticPopupDialogs["GQL_ROW_BATCH_SKIP"] = {
        text = "%s",
        button1 = "确认",
        button2 = "取消",
        timeout = 0,
        persistent = true,
        hideOnEscape = true,
        OnButton1 = function(_dialog, data) Mx.ApplyBatch(data) end,
        OnButton2 = function(_dialog, data)
            GQL.Config.Log(("批量不做：取消 quest=%s")
                :format(tostring(data and data.questID)))
        end,
    }
    return true
end

--- 弹确认框。两件事要挡：叠框（连点两次不许排两个框、也不许换掉第一个框的文案）、层级。
--- 层级口径照本文件 ActivateMatrix 那两段：DIALOG 只是"打开这一下"的临时抬高，
--- 后开任何窗口都由 YieldToWindow 让回 MEDIUM —— 所以弹之前让回 MEDIUM 就是既有那一招，
--- 让回去之后**不需要还原**（MEDIUM 本来就是矩阵的常驻层）。
--- 静态弹窗在不在矩阵之上**没实测过**，所以把这一笔层号原样写进日志：玩家报"看不见框"时
--- 先看这一行，别凭猜改。
function Mx.ShowBatchConfirm(text, data)
    if not Mx.EnsureBatchDialog() or type(StaticPopup_Show) ~= "function" then
        GQL.Config.Log("批量不做：这台客户端没有确认弹窗，未改动")
        return false
    end
    if type(StaticPopup_Visible) == "function"
        and StaticPopup_Visible("GQL_ROW_BATCH_SKIP") then
        GQL.Config.Log("批量不做：确认框还开着，这次点击忽略")
        return false
    end
    local before = "?"
    if frame and frame.GetFrameStrata then
        local okS, s = pcall(frame.GetFrameStrata, frame)
        if okS then before = tostring(s) end
        if before == "DIALOG" then pcall(frame.SetFrameStrata, frame, "MEDIUM") end
    end
    local shown = pcall(StaticPopup_Show, "GQL_ROW_BATCH_SKIP", text, nil, data)
    GQL.Config.Log(("批量%s：弹确认 quest=%s 矩阵层=%s→%s%s")
        :format(data.mode == "clear" and "撤销" or "不做", tostring(data.questID),
            before, before == "DIALOG" and "MEDIUM" or before,
            shown and "" or "（弹窗没起来，未改动）"))
    return shown and true or false
end

--- 任务名那一格的右键（横排整行热区 / 竖排表头那一格，两排同一个入口）。
--- **只接右键**：这一格没有角色归属，格子那两档手动标记（完成/不做）搬过来就是谎报。
--- 收的是渲染时摆上去的那一份 row：判据、方向、文案全部就地读它，
--- 于是一枪从按下到弹框**零版面重算**、只问一次时钟（Global Constraints 那一条）。
--- 换轮/重载的安全门留在确认那一步（Mx.ApplyBatch 自己重新解析）。
function Mx.RowBatchClick(row)
    local db = GQL.Config.DB()
    if not db or not row or not row.questID then return end
    local counts = Mx.BatchCounts(row)
    if not counts.mode then
        GQL.Config.Log(("批量不做：这一行没有可改的格子 quest=%s（%d 号都在完成）")
            :format(tostring(row.questID), counts.total))
        return
    end
    local now = Mx.ClickClock()
    local rec = db.quests and db.quests[row.questID]
    local key = (GQL.Ledger and GQL.Ledger.InstanceKey)
        and GQL.Ledger.InstanceKey(db, row.questID, rec, now) or nil
    if not key then
        -- 比格子右键**有意更严**：格子那一档写下去只是暂时画不出来（读侧比不上键），
        -- 批量一枪十几格，等于往存档塞十几笔永远画不出来的记录。不许为「两处一致」改回去。
        GQL.Config.Log(("批量不做：这一条身份未定，未改动 quest=%s")
            :format(tostring(row.questID)))
        return
    end
    Mx.ShowBatchConfirm(Mx.BatchText(row, counts, now),
        { questID = row.questID, k = key, mode = counts.mode })
end
```

4c. `RowHoverButton`（约 :428）整段替换：

```lua
--- 整行悬停热区池（左窗格）：一行一条，盖满任务列宽度。横排传 row（那一行是哪个任务），
--- 竖排左窗格传 nil（那一行是角色，不属于任何任务）。
--- 横排那一格注册**右键**：整条任务本轮所有角色都不做（见 Mx.RowBatchClick）。
--- 左键不注册 —— 这一格没有角色归属，格子那两档手动标记搬过来就是谎报。
--- __row 存渲染时那一份行：点击与悬停就地读它，零重算（池子里复用，每轮排版都重写）。
local function RowHoverButton(y, row)
    usedRowBtns = usedRowBtns + 1
    local b = rowBtns[usedRowBtns]
    if not b then
        b = CreateFrame("Button", nil, contentL)
        b:EnableMouse(true)
        b:SetScript("OnMouseWheel", OnMouseWheel)
        pcall(b.RegisterForClicks, b, "RightButtonUp")
        b:SetScript("OnClick", function(self, button)
            if button == "RightButton" then Mx.RowBatchClick(self.__row) end
        end)
        rowBtns[usedRowBtns] = b
    end
    b:ClearAllPoints()
    b:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, -y)
    b:SetSize(NAME_W, ROW_H)
    b.__rowY = y
    b.__questID = row and row.questID or nil   -- 悬停提示与金额那一路认行靠它（老口径不动）
    b.__row = row                              -- 批量那一枪就地读这份
    b:Show()
    return b
end
```

4d. 横排调用点（约 :1915）`RowHoverButton(y, row.questID)` → `RowHoverButton(y, row)`。
竖排调用点 `RowHoverButton(y, nil)` 一字不改（它本来就传 nil）。

- [ ] **Step 5：登记用例并跑绿**

`tests = [...]` 列表里 T1 那四行之后加：

```python
    test_row_right_click_skips_and_restores_the_whole_row,
    test_row_right_click_cancel_writes_nothing,
    test_row_right_click_ignores_a_second_click_while_open,
    test_row_right_click_refuses_when_the_row_is_all_done,
    test_row_right_click_refuses_when_the_identity_is_undetermined,
    test_row_right_click_drops_the_batch_when_the_round_rolled,
```

Run: `python tests/run_gql_tests.py -k row_right_click,row_batch,batch_row_of,right_click_marks,upvalue,every_defined_case`
Expected: 全 PASS（格子右键那条老用例必须仍然 PASS —— 它测的是同一种记录）

- [ ] **Step 6：提交**

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "feat: GQL 批量本轮不做 T2（横排任务名右键一次标完全行 + 确认框 + 三道 fail-closed 门）"
```

**实施回写（2026-11-10，T1/T2 已入库）** —— 计划有三处与现场不符，按下面这份为准：

1. **T1 第 3 条用例**（`batch_row_of_reads_the_row_currently_painted`）原写"回同一张表"
   （`r == painted`）测不了：`MatrixView.Groups` 是纯函数、每次调用重建行表
   （实测两次调用 `ra == rb` 为 false，字段内容逐字相同）。改成**逐字段比内容**
   （含 `state`/`src` 两个方向），"判据同源"这条照样钉得住；表地址相同也算过。
2. **T2 第 6 条夹具**（`row_right_click_drops_the_batch_when_the_round_rolled`）少一行：
   `Sync` 只在账里没有 `roundEnd` 时才重问板面倒计时
   （[Sync.lua:345](../../GoldQuestLedger/Sync.lua) `needProbe = not (rec and tonumber(rec.roundEnd))`），
   所以光改 `GQL_BOARD['end']` 编号不会换，用例前提断言会红。补一行
   `g.GQLDB["quests"][195]["roundEnd"] = None` 再 `SnapshotNow()` ——
   与既有 11-04 口径用例同一造法，不是放宽断言。
3. **计划漏了一条会被本轮推翻的旧用例**：`test_matrix_right_click_blocks_nothing`
   （10-27"移除右键屏蔽"那一轮留下的，断言整行热区**没有** OnClick、**没注册过**
   RightButtonUp）。本轮有意在同一颗按钮上装回右键，但装的是**另一件事**（批量 skip，
   不下矩阵、不写 ignored）。已订正为：① 只许注册 `RightButtonUp`（左键没有角色归属）、
   OnClick 必须在；②③④ 三面（`ignored` 仍 nil、行仍在矩阵、无"已永久屏蔽"、
   不建 `GQLRemoveQuestDialog` 全局帧、hover/滚轮还在）**一字未动**。
   两个助手 `matrix_row_hotzone_state` / `click_matrix_row_right` 的注释同步改掉
   "第 31 轮起没有 OnClick"那句作废描述。

---

### Task 3：把入口说给玩家看（悬停提示）+ 拆掉旧留档里那句作废口径

判据与手势已落地，但玩家不可能猜到任务名可以右键。另外两处留档还在写"矩阵里不许再出现
右键屏蔽一行"——那正是本轮推翻的那句，留着它，下一个读代码的人会按旧规矩把这次改动删掉。

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua`（`ShowMoneyTooltip` 约 :663-667 那个 `if charKey` 支；文件头注释 :355-356）
- Modify: `tests/run_gql_tests.py`（新用例 2 条 + 助手 `hover_row` / `hover_away` / `cost_probe` /
  `tip_lines`；改 `test_right_click_marks_a_cell_as_skipped` 的文档句）

**Interfaces:**
- Consumes: T2 挂在热区上的 `__row`（提示那一句就地调 `Mx.BatchCounts` —— 纯表查、零接口）、
  `GameTooltip:AddLine`。
- Produces: 界面提示文案两串 —— `"右键 本任务本轮全号不做"` /
  `"右键 撤销：本任务本轮全号不做"`（T4 的竖排表头提示复用同一条分支，不另写）；
  助手 `cost_probe(lua)`（返回一个读 `(Groups 次数, Api 次数)` 的函数）。

- [ ] **Step 1：加助手 + 写失败用例（2 条）**

助手放 `click_row` 旁边。`hover_row` 的形状照
`test_chars_in_rows_head_button_hovers_without_row_highlight` 里那个嵌套 `hover`
（那一处是函数内的局部函数，本轮提到模块级共用）：

```python
def hover_row(lua, qid):
    """把光标停在**横排**任务名那一格（左窗格整行热区）上，跑一轮悬停扫描。"""
    return bool(lua.execute(
        " for _, parent in ipairs({ GQLMatrixContentL, GQLMatrixContent, GQLMatrixHeadR }) do"
        "   for _, c in ipairs(parent.__children) do c.__mouseover = false end end"
        " local hit"
        " for _, c in ipairs(GQLMatrixContentL.__children) do"
        "   if c.__shown and c.__questID == %d then hit = c end end"
        " if not hit then return false end"
        " hit.__mouseover = true GQL_FOCUS = hit"
        " GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame) return true" % qid))


def hover_away(lua):
    """光标离开整张列表（提示要跟走）。"""
    lua.execute(
        " for _, parent in ipairs({ GQLMatrixContentL, GQLMatrixContent, GQLMatrixHeadR }) do"
        "   for _, c in ipairs(parent.__children) do c.__mouseover = false end end"
        " GQL_FOCUS = nil"
        " GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")


def cost_probe(lua):
    """装两个计数器：版面重算次数（MatrixView.Groups）与 Api 调用总数。
    只包既有函数（同一张表换字段），不新增替身方法。
    插件代码都是 `GQL.Api.X(...)` 这种**调用时才查字段**的写法，所以包得住。"""
    lua.execute(r"""
        GQL_COST = { groups = 0, api = 0 }
        do
            local mv = GQL.MatrixView
            local orig = mv.Groups
            mv.Groups = function(db, now)
                GQL_COST.groups = GQL_COST.groups + 1
                return orig(db, now)
            end
        end
        do
            local A = GQL.Api
            local names, fns = {}, {}
            for name, fn in pairs(A) do
                if type(fn) == "function" then
                    names[#names + 1] = name
                    fns[#names + 1] = fn
                end
            end
            for i = 1, #names do
                local orig = fns[i]
                A[names[i]] = function(...)
                    GQL_COST.api = GQL_COST.api + 1
                    return orig(...)
                end
            end
        end
    """)
    return lambda: (int(one(lua.execute("return GQL_COST.groups"))),
                    int(one(lua.execute("return GQL_COST.api"))))


def tip_lines(lua, body):
    """把 GameTooltip 换成只记行的替身（形状照既有提示用例）。"""
    lua.globals().GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *c: body.append(str(text)),
        "AddDoubleLine": lambda self, l, r, *c: body.append("%s %s" % (l, r)),
        "Show": lambda *a: body.append("SHOW"),
        "Hide": lambda *a: body.append("HIDE"),
    })
```

第 1 条：

```python
def test_row_hotzone_hint_shows_the_batch_direction():
    """任务名那一格能右键，但玩家不会自己猜到 —— 提示里必须写明（既有口径：
    「右键那一路没有别的入口能发现，不写明就等于只有作者知道」）。
    两档方向两句话：行里还有没做的格子 ⇒ 说「全号不做」；全行已是终态 ⇒ 说「撤销」。
    格子那一句「左键 切换完成 · 右键 切换不做」一个字不动，两处不许互相盖。"""
    name = "row_hotzone_hint_shows_the_batch_direction"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    calls = []
    tip_lines(lua, calls)
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    if not hover_row(lua, 195):
        return fail(name, "前提：停不到任务名那一格")
    joined = "\n".join(calls)
    if "右键 本任务本轮全号不做" not in joined:
        return fail(name, f"这一行还有没做的格子，提示该说全号不做:\n{joined}")
    if "左键 切换完成" in joined:
        return fail(name, "整行热区没有角色归属，格子那两句不许出现在这里")
    # 全行做成终态 ⇒ 那句话翻成「撤销」
    calls.clear()
    click_cell(lua, str(gql.Sync.CurrentCharKey()), 195, "RightButton")
    gql.Matrix.Render()
    hover_row(lua, 195)
    joined = "\n".join(calls)
    if "右键 撤销：本任务本轮全号不做" not in joined:
        return fail(name, f"全行已是终态，提示该翻成撤销:\n{joined}")
    ok(name)
```

第 2 条（零重算 + 零接口，含"确认时才重算"那一半）：

```python
def test_row_batch_click_and_hover_cost_nothing_extra():
    """钉两条纪律（同 Global Constraints 那一坑位），外加它们的反面：
    ① **悬停**只读热区上现成的 __row：Groups 计数与 Api 计数一个都不许多；
    ② **点击到弹框**这一段：版面零重算，客户端只许多问一次时钟（一次点击只取一次读数）；
    ③ 反面也要成立：**点确认**那一步必须重新解析版面（确认期间可能换轮/重载），
       所以这一段 Groups 计数该涨 —— 不涨就是 ApplyBatch 偷偷复用了旧那一份 row。"""
    name = "row_batch_click_and_hover_cost_nothing_extra"
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    gql.Matrix.Toggle()
    gql.Matrix.Render()
    cost = cost_probe(lua)
    g_groups, g_api = cost()
    if not hover_row(lua, 195):
        return fail(name, "前提：停不到任务名那一格")
    hover_away(lua)
    if not hover_row(lua, 195):
        return fail(name, "前提：第二轮悬停没命中")
    hover_away(lua)
    groups, api = cost()
    if (groups, api) != (g_groups, g_api):
        return fail(name, "悬停不许重算版面、不许多问接口：%s → %s"
                          % ((g_groups, g_api), (groups, api)))
    # 点击 → 弹框：版面零重算，时钟最多一次
    if not click_row(lua, 195):
        return fail(name, "点不到任务名那一格")
    groups, api = cost()
    if groups != g_groups:
        return fail(name, f"点击到弹框这一段重算了版面（多 {groups - g_groups} 次）")
    if api - g_api > 1:
        return fail(name, f"点击到弹框多问了接口（{api - g_api} 次，只许一次时钟）")
    if popup_row_batch(lua) is None:
        return fail(name, "前提：扫描是空的，这一路没真弹过确认框")
    # 确认那一步要重新解析（拿旧 row 盖章 = 换轮那道门形同不存在）
    if not click_popup(lua, 1):
        return fail(name, "点不到确认")
    if cost()[0] <= groups:
        return fail(name, "点确认时没重新解析版面（ApplyBatch 该走 BatchRowOf）")
    ok(name)
```

- [ ] **Step 2：跑用例确认它们红**

Run: `python tests/run_gql_tests.py -k row_hotzone_hint,row_batch_click_and_hover`
Expected: 两条 FAIL（前一条报"提示该说全号不做"；后一条至少报"扫描是空的"或"没重新解析版面"）

- [ ] **Step 3：写提示那一句**

`ShowMoneyTooltip` 里这段（约 :663-667）：

```lua
        if charKey then
            GameTooltip:AddLine("左键 切换完成 · 右键 切换不做", 0.5, 0.5, 0.5)
        end
```

替换成：

```lua
        if charKey then
            GameTooltip:AddLine("左键 切换完成 · 右键 切换不做", 0.5, 0.5, 0.5)
        elseif anchor and anchor.__row then
            -- 任务名那一格（横排整行热区 / 竖排表头那格）：右键那一枪是整条任务的批量不做。
            -- 就地读渲染时那份 row（Mx.BatchCounts 是纯表查、零接口）—— 为这一句话
            -- 重算一遍版面是不划算的（零新增接口调用那条纪律同一坑位）。
            -- 无事可做的行没有这一句；格子那两句照旧不搬到这一格。
            local mode = Mx.BatchCounts(anchor.__row).mode
            if mode == "set" then
                GameTooltip:AddLine("右键 本任务本轮全号不做", 0.5, 0.5, 0.5)
            elseif mode == "clear" then
                GameTooltip:AddLine("右键 撤销：本任务本轮全号不做", 0.5, 0.5, 0.5)
            end
        end
```

- [ ] **Step 4：拆掉旧留档里那句作废口径 + 改文件头注释**

`test_right_click_marks_a_cell_as_skipped`（约 :10076）文档最后一行
`左窗格那排整行热区不参与 —— 矩阵里不许再出现"右键屏蔽一行"那种入口。` 替换成：

```
    这一条只管**格子**那一档。任务名那一格的整行批量是 2026-11-10 新加的另一档
    （见 test_row_right_click_skips_and_restores_the_whole_row），两者共用同一笔记录。"""
```

`Matrix.lua` 文件头那段注释（约 :355-356）
`左窗格那排整行热区不在此列 —— 矩阵里除了这两档手动标记不再有任何"屏蔽/忽略"入口。` 替换成：

```
--- 左窗格那排整行热区另有**一档手势**：右键任务名 = 整条任务本轮所有角色都不做
--- （批量写/清上面这两档记录，本身不是第三种标记，见 Mx.RowBatchClick）。
--- 永久屏蔽不在这 —— 它只长在设置里的「关键词 · 屏蔽管理」那一栏。
```

- [ ] **Step 5：跑绿 + 提交**

Run: `python tests/run_gql_tests.py -k row_hotzone,row_batch,row_right_click,right_click_marks,ui_text_no_forensic`
Expected: 全 PASS

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "feat: GQL 批量本轮不做 T3（任务名那格写明右键能干什么 + 零重算纪律 + 撤掉作废口径）"
```

---

### Task 4：竖排（角色在行那个模式）同一个入口

竖排的表头任务名那一格今天注释明写"可悬停、不可点击"，理由是"这一格没有角色归属，
格子那两档语义不成立"。批量这一档是按任务问所有号，正好不需要归属，所以解除这条限制。

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua`（`Head.Button` 约 :1458；竖排调用点约 :2181）
- Modify: `tests/run_gql_tests.py`（新用例 2 条 + 助手 `click_head` / `hover_head`；`tests` 登记）

**Interfaces:**
- Consumes: `Mx.RowBatchClick(row)` / `Mx.BatchCounts`（T2、T3 已产出，一字不改）、
  表头热区父帧全局名 `GQLMatrixHeadR`、夹具 `_stack_env`（`STACK_SPECS` = 91/92/93 三条任务、
  两个号 小号一 + 小号二）。
- Produces: `Head.Button(x, row)` 新签名（第 2 参从 questID 换成 row）；热区上同样带
  `__questID` 与 `__row`，所以 T3 那句提示在竖排自动生效。

- [ ] **Step 1：加助手 + 写失败用例（2 条）**

```python
def click_head(lua, qid, button="RightButton"):
    """点竖排表头那一格（任务名那一行）：走热区自己的 OnClick。只看可见那颗
    （滑出可视区的表头热区是手动藏起来的，点它测的是哑炮）。"""
    return bool(lua.execute(
        "for _, c in ipairs(GQLMatrixHeadR.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s and c.__shown and c.__type == 'Button' and c.__questID == %d then "
        "    s(c, %r) return true end "
        "end return false" % (qid, button)))


def hover_head(lua, qid):
    """把光标停在竖排表头那一格上（先清掉别处的 mouseover，否则 HoverTarget 按池序
    仍会先命中上一轮点亮的正文格 —— 那就测不到表头分支）。"""
    return bool(lua.execute(
        " for _, parent in ipairs({ GQLMatrixContentL, GQLMatrixContent, GQLMatrixHeadR }) do"
        "   for _, c in ipairs(parent.__children) do c.__mouseover = false end end"
        " local hit"
        " for _, c in ipairs(GQLMatrixHeadR.__children) do"
        "   if c.__type == 'Button' and c.__shown and c.__questID == %d then hit = c end end"
        " if not hit then return false end"
        " hit.__mouseover = true GQL_FOCUS = hit"
        " GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame) return true" % qid))
```

第 1 条：

```python
def test_chars_in_rows_head_right_click_batches_the_column():
    """竖排（角色在行）的表头任务名那一格，右键走的是和横排同一个入口、同一份判据
    （用户 2026-11-10 定：「两个模式都能批量」）。
    钉三面：① 真落账（这一列上没做的格子全 ×，已完成那格不动）；
    ② 表头热区原来那三处不许回归（可悬停出金额提示、滑出可视区要藏、
       切回横排整池收起来 —— 那三处是 0.6 T6 注释里列的原文）；
    ③ 滚轮仍然能穿过表头热区（热区盖在表头条上，不挂滚轮就是玩家实测过的
       「滚轮大部分情况无效」）。"""
    name = "chars_in_rows_head_right_click_batches_the_column"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=43200)
    g = lua.globals()
    gql.Matrix.Toggle()
    _stack_env(lua, gql, g)
    gql.Config.SetCharsInRows(True)
    if int(gql.Matrix.Render()) != 3:
        return fail(name, "前提：竖排画 3 列任务")
    key = str(gql.Sync.CurrentCharKey())
    other = "小号二-永恒之井"
    # 91 这一列：当前号做完（✓ 不许被批量盖掉），另一个号空着（该被批量写成 ×）
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, key, 91):
        return fail(name, "前提：点不到当前号在 91 那一格")
    if not click_head(lua, 91):
        return fail(name, "点不到表头那一格（表头没注册右键？）")
    tip = popup_row_batch(lua)
    if not tip or tip["shown"] != 1 or tip["mode"] != "set":
        return fail(name, f"表头右键该弹批量确认，实际 {tip}")
    if not click_popup(lua, 1):
        return fail(name, "点不到确认")
    if manual_mark(lua, key, 91) != "done":
        return fail(name, "已完成那格不许被批量改成不做")
    if manual_mark(lua, other, 91) != "skip":
        return fail(name, "这一列上没做的那个号该被批量记成不做")
    # ② 悬停那一路照旧：仍出金额提示，且提示带 T3 那句方向（整列已终态 ⇒ 撤销）
    calls = []
    tip_lines(lua, calls)
    gql.Matrix.Render()
    if not hover_head(lua, 91):
        return fail(name, "前提：停不到表头那一格")
    joined = "\n".join(calls)
    if "SHOW" not in calls:
        return fail(name, f"表头那一格仍要出金额提示:\n{joined}")
    if "右键 撤销：本任务本轮全号不做" not in joined:
        return fail(name, f"整列已是终态，提示该翻成撤销:\n{joined}")
    # 三处之二：滑出可视区的列要藏 —— 第一列没滑出去，仍该可见
    if not bool(lua.execute("for _, c in ipairs(GQLMatrixHeadR.__children) do "
                            "  if c.__questID == 91 then return c.__shown == true end end "
                            "return false")):
        return fail(name, "第一列的表头热区不该被藏掉")
    # 三处之三：切回横排后整池收起来
    gql.Config.SetCharsInRows(False)
    gql.Matrix.Render()
    if int(lua.execute("local n = 0 for _, c in ipairs(GQLMatrixHeadR.__children) do "
                       "  if c.__type == 'Button' and c.__shown then n = n + 1 end end "
                       "return n")) != 0:
        return fail(name, "切回横排后表头热区整池该收起来")
    # ③ 滚轮透传：热区自己的 OnMouseWheel 还在（注册点击不许把它顶掉）
    if not bool(lua.execute(
            "for _, c in ipairs(GQLMatrixHeadR.__children) do"
            "  if c.__type == 'Button' then return c.__scripts['OnMouseWheel'] ~= nil end end"
            " return false")):
        return fail(name, "表头热区的滚轮处理器被注册点击顶掉了")
    ok(name)
```

第 2 条：

```python
def test_chars_in_rows_head_click_left_does_nothing():
    """表头那一格只接右键。左键在这没有角色归属 —— 记「完成」要落到某个号身上，
    按下去只许什么都不发生（连确认框都不许弹）。"""
    name = "chars_in_rows_head_click_left_does_nothing"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=43200)
    gql.Matrix.Toggle()
    _stack_env(lua, gql, lua.globals())
    gql.Config.SetCharsInRows(True)
    gql.Matrix.Render()
    me = str(gql.Sync.CurrentCharKey())
    before = manual_mark(lua, me, 91)
    if not click_head(lua, 91, "LeftButton"):
        return fail(name, "点不到表头那一格")
    if popup_row_batch(lua) is not None:
        return fail(name, "左键不许弹批量确认框")
    if manual_mark(lua, me, 91) != before:
        return fail(name, "左键不该动任何一格")
    ok(name)
```

- [ ] **Step 2：跑用例确认它红**

Run: `python tests/run_gql_tests.py -k chars_in_rows_head_right_click,head_click_left`
Expected: 两条 FAIL（报"点不到表头那一格（表头没注册右键？）"一类）

- [ ] **Step 3：给表头热区挂上同一个入口**

`Head.Button`（约 :1458）整段替换 —— 注释里那"三处落点"的说明照抄保留，只改"不可点击"那句：

```lua
--- 竖排表头热区（0.6 T6，计划点 6 的「表头新增可悬停热区」）：盖在表头第二行
--- （任务名行）的每一列上。可悬停，另外**只接右键**：整条任务本轮所有角色都不做
--- （Mx.RowBatchClick，和横排任务名那一格同一个入口）。
--- 原来那句「这一格没有角色归属，CellClick 的两档手动标记语义在这里不成立」照旧成立 ——
--- 所以左键仍不注册；批量这一档是按任务问所有号，本来就不需要归属。
--- 要同时进三处：
---   ① HoverTarget 的池遍历（本池排在状态格之后；它与正文热区几何上不相交，顺序无关）
---   ② 末尾「多余热区收起来」的 Hide 循环（见 RenderCharsInRows 与 Mx.Render 分派处）
---   ③ ShowRowHighlight/金额提示的分岔（见 HoverScan：表头那格没有「行」可亮，只出提示）
--- Head.right 没有裁剪能力：滑出可视区的列必须手动藏（口径同 Head.Put），
--- 否则一只哑热区压在左窗格「角色」二字上，还会把滚轮之外的事件吞掉。
function Head.Button(x, row)
    Head.btnUsed = Head.btnUsed + 1
    local b = Head.btns[Head.btnUsed]
    if not b then
        b = CreateFrame("Button", nil, Head.right)
        b:EnableMouse(true)
        -- 滚轮透传必须挂：热区盖在表头条上，不挂就正好复现用户实测过的
        -- 「滚轮大部分情况无效」（按钮吃掉事件、OnMouseWheel 为 nil）。
        -- 注册点击不会顶掉它：两者挂的是不同的脚本槽。
        b:SetScript("OnMouseWheel", OnMouseWheel)
        pcall(b.RegisterForClicks, b, "RightButtonUp")
        b:SetScript("OnClick", function(self, button)
            if button == "RightButton" then Mx.RowBatchClick(self.__row) end
        end)
        Head.btns[Head.btnUsed] = b
    end
    b:ClearAllPoints()
    b:SetPoint("TOPLEFT", Head.right, "TOPLEFT", x, -ROW_H)
    b:SetSize(COL_W, ROW_H)
    b.__questID = row and row.questID or nil  -- 悬停金额提示用（行级口径，无角色归属）
    b.__row = row                            -- 批量与提示那一句就地读这份（零重算）
    b.__rowY = 0                    -- 表头没有正文行；HoverScan 的表头分岔不看它
    b.__head = true                 -- HoverScan 分岔的判据（池里复用，恒真不用清）
    if x + COL_W < 0 or x > visW then b:Hide() else b:Show() end
    return b
end
```

竖排调用点（约 :2181）`Head.Button(cx - offX, row.questID)` → `Head.Button(cx - offX, row)`。

- [ ] **Step 4：跑绿 + 全族回归**

Run: `python tests/run_gql_tests.py -k chars_in_rows,head_right_click,head_click_left,head_button`
Expected: 全 PASS（`chars_in_rows` 那一族把 0.6 T6 的几何用例一起扫一遍，
确认热区池/隐藏/悬停三处没回归）

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "feat: GQL 批量本轮不做 T4（竖排表头任务名同一个入口，左键仍不响应）"
```

---

### Task 5：竖排左窗格的角色名不再砍成 4 个字

独立小改。用户 2026-11-10 定：竖排左窗格全名直接写（角色名上限 7 个字，120px 放得下 10 个
中文字，护栏都不加）；横排列头那把 4 字刀照旧 —— 那里是 72px 窄列，放开就得改整张表的几何。

**Files:**
- Modify: `GoldQuestLedger/Matrix.lua`（约 :2201-2203）
- Modify: `tests/run_gql_tests.py`（新用例 2 条 + 改 `test_chars_in_rows_stale_char_label_in_left_pane`）

**Interfaces:**
- Consumes: `ShortCharKey`（取服务器名那刀照旧）、`Hex`/`ClassColor`、`PutL`、既有夹具
  `_stack_env` 与取证助手 `_left_lines` / `_head_lines` / `HEAD_SMALL`。
- Produces: 无新接口 —— 只有版面文字变长。**`ShortName` 本体一个字不动**
  （横排列头 :1868 与表头任务名 :2177 还在用它，那两处本轮不改）。

- [ ] **Step 1：写失败用例（2 条）**

```python
def test_chars_in_rows_left_pane_prints_the_whole_char_name():
    """竖排左窗格的角色名放开长度（用户 2026-11-10：「名字长度不要有限制了……角色名字最多
    7 个字，所以不用管，120 够了」）。钉两面：① 6 个字的号名整条画出，一列都不许砍；
    ② 横排列头那把 4 字刀一个字不动（两处是两处，别顺手对齐 —— 那是 72px 窄列）。"""
    name = "chars_in_rows_left_pane_prints_the_whole_char_name"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=43200)
    g = lua.globals()
    gql.Matrix.Toggle()
    _stack_env(lua, gql, g)
    long_key = "战歌之王萨鲁-永恒之井"           # 6 个字，超过原来那把 4 字刀
    long_name = "战歌之王萨鲁"
    lua.execute("GQLDB.chars[%r] = { lastSync = 1000, views = {} }" % long_key)
    gql.Config.SetCharsInRows(True)
    gql.Matrix.Render()
    vertical = [t for t, _f, _y in _left_lines(lua)]
    gql.Config.SetCharsInRows(False)
    gql.Matrix.Render()
    horizontal = [t for t, fo, _y, _x in _head_lines(lua) if fo == HEAD_SMALL]
    if not any(long_name in t for t in vertical):
        return fail(name, f"竖排左栏该写整条号名:\n{vertical}")
    if any("战歌之王" in t and long_name not in t for t in vertical):
        return fail(name, f"左栏里还有被砍成 4 字的那一份:\n{vertical}")
    if "…" in "".join(vertical):
        return fail(name, f"120px 放得下，不许补省略号:\n{vertical}")
    if not any("战歌之王" in t and long_name not in t for t in horizontal):
        return fail(name, f"横排列头照旧砍 4 字（本轮不动它）:\n{horizontal}")
    ok(name)


def test_chars_in_rows_stale_char_label_uses_the_whole_name():
    """「过期」那档跟着放开：原来是 2 字 + 「过期」（照抄横排列头那把窄列的写法），
    竖排左窗格现在该写**全名 + 过期**。判据本身（c.stale 来自 Ledger.MatrixColumns）
    换轴不许碰 —— 这一条只钉文字形状，顺带钉住「过期那号不许从版面里消失」。"""
    name = "chars_in_rows_stale_char_label_uses_the_whole_name"
    lua, gql, _ = sync_env([], extra=("Matrix",), board=2000000)
    g = lua.globals()
    gql.Matrix.Toggle()
    _stack_env(lua, gql, g)
    long_key = "战歌之王萨鲁-永恒之井"
    # lastSync 停在 100、墙上钟推到 700000 ⇒ 只有这一号判成「太久没同步」
    lua.execute("GQLDB.chars[%r] = { lastSync = 100, views = {} }" % long_key
                " GQL.Api.WallClock = function() return 700000 end")
    gql.Config.SetCharsInRows(True)
    if int(gql.Matrix.Render()) != 3:
        return fail(name, "前提：竖排画 3 列任务")
    if int(lua.execute("local n = 0 for _, c in ipairs(GQLMatrixContentL.__children) do "
                       "  if c.__type == 'Button' and c.__shown then n = n + 1 end end "
                       "return n")) != 3:
        return fail(name, "该有三个角色行（长名那一号没被算进列）")
    left = [t for t, _f, _y in _left_lines(lua) if "过期" in t]
    if len(left) != 1:
        return fail(name, f"该有一条带「过期」，实际:\n{left}")
    if "战歌之王萨鲁过期" not in left[0]:
        return fail(name, f"竖排该写全名 + 过期，实际:\n{left}")
    if int(gql.Matrix.Render()) != 3:
        return fail(name, "过期那号不许从竖排版面里消失")
    gql.Config.SetCharsInRows(False)
    gql.Matrix.Render()
    heads = [t for t, fo, _y, _x in _head_lines(lua) if fo == HEAD_SMALL and "过期" in t]
    if not heads or "战歌之王萨鲁" in heads[0]:
        return fail(name, f"横排列头照旧是砍过的写法:\n{heads}")
    ok(name)
```

- [ ] **Step 2：跑用例确认它红**

Run: `python tests/run_gql_tests.py -k left_pane_prints_the_whole,stale_char_label_uses_the_whole`
Expected: 两条 FAIL（前一条"该写整条号名"；后一条"该写全名 + 过期"）

- [ ] **Step 3：改那两行**

`Matrix.lua` 竖排左窗格那段（约 :2201-2203）：

```lua
            -- 左窗格：角色名（ShortName 4 字、职业色）；过期角色 = 2 字 + "过期"
            -- （写法沿用横排列头那条，计划点 7 说清它落在左栏）。
            local nm = ShortName(ShortCharKey(c.key))
            local label = c.stale and (ShortName(ShortCharKey(c.key), 2) .. "过期") or nm
```

替换成：

```lua
            -- 左窗格：角色名整条写出，不再砍 4 个字（用户 2026-11-10；120px 放得下
            -- 10 个中文字，而角色名上限 7 个字 ⇒ 连省略号这道护栏都不用加）。
            -- 「过期」那档同样全名打头。横排列头（Head.Put 那一处）照旧砍 4 字 ——
            -- 那是 72px 的窄列，两处不是同一个几何，别顺手对齐。
            local nm = ShortCharKey(c.key)
            local label = c.stale and (nm .. "过期") or nm
```

- [ ] **Step 4：改掉被本轮推翻的那句旧断言**

`test_chars_in_rows_stale_char_label_in_left_pane`（约 :9430）钉的是"两种排布写出的那串字
**逐字相同**"，本轮两处故意不同 —— 比较要降级成"同一个号在两处都报得出过期"。把那一段

```python
    left = [t for t, _f, _y in _left_lines(lua) if "过期" in t]
    if left != stale_head:
        return fail(name, f"竖排左栏该写同一串「过期」字（写法沿用）: "
                          f"横 {stale_head} vs 竖 {left}")
```

替换成：

```python
    left = [t for t, _f, _y in _left_lines(lua) if "过期" in t]
    # 2026-11-10：竖排左栏放开名字长度，两处**故意**不同形（横排是 72px 窄列）。
    # 这一条从此只钉「同一个号在两处都报得出过期」；完整形状归
    # test_chars_in_rows_stale_char_label_uses_the_whole_name。
    if len(left) != len(stale_head):
        return fail(name, f"过期那几号在两处都该报出来: "
                          f"横 {stale_head} vs 竖 {left}")
    if not all("过期" in t for t in left):
        return fail(name, f"竖排左栏那几行该带「过期」尾巴:\n{left}")
```

并把该用例文档里那句 `写法沿用（2 字 + "过期"）` 改成
`写法按各自几何：横排列头 2 字 + 「过期」（72px），竖排左栏全名 + 「过期」（120px，2026-11-10 放开）`；
文档里那句 `这里断的是"两种排布给出的那一串字逐字相同"` 改成
`这里断的是"同一个号在两处都报得出过期"（两处字数故意不同）`。

- [ ] **Step 5：跑绿 + 提交**

Run: `python tests/run_gql_tests.py -k chars_in_rows,left_pane_prints,stale_char_label,columns_show_char_name`
Expected: 全 PASS。`columns_show_char_name` 那一族测的是横排那把 4 字刀 —— 它必须仍按横排口径
成立（不许因为本轮改动跟着变成"竖排口径"）。

```bash
git add GoldQuestLedger/Matrix.lua tests/run_gql_tests.py
git commit -m "feat: GQL 批量本轮不做 T5（竖排左窗格角色名放开，横排列头照旧）"
```

---

### Task 6：版本三处一致 + 更新说明 + 全量回归 + 装到客户端

**Files:**
- Modify: `GoldQuestLedger/Core.lua:13`（`GQL.VERSION`）
- Modify: `GoldQuestLedger/GoldQuestLedger.toc`（`## Version:`）
- Modify: `CHANGELOG.md`（顶部新增 `## 0.6.6`）

**Interfaces:**
- Consumes: T1-T5 全部已提交的改动。
- Produces: 一个可装的 0.6.6；`test_changelog_mentions_the_shipped_version` 认这三处一致。

- [ ] **Step 1：版本三处**

```bash
grep -n "GQL.VERSION" GoldQuestLedger/Core.lua
grep -n "## Version" GoldQuestLedger/GoldQuestLedger.toc
```

两处 `0.6.5` → `0.6.6`。

- [ ] **Step 2：CHANGELOG 顶部插入**（紧跟 `# GoldQuestLedger 更新说明` 那一行之后）

```markdown
## 0.6.6
- **右键任务名 = 这一条本轮所有角色都不做**：原来只能一个一个格子点，现在整行一次点完。
  再右键一次是批量撤销，清掉这一行上所有 ×（含你以前单独点过的那几枚）。
  两向都弹确认框，框上写清楚动几个号、几个保持完成、本轮到几点。
  已完成的那几格永远不动 —— × 是意向，不许盖掉做过的凭据，钱也照旧统计。
  换轮自动作废：批量写的就是原来那种「这一号本轮不做」的记录。横排、竖排都能这么点。
- **竖排左窗格的角色名不再砍成 4 个字**：整条名直接写，「过期」那档也跟着改成全名 + 过期。
  设置里横排那个模式的列头照旧。
- 记录、金币门槛、到期时间、存档一律没动（这次一个字段都没加）。
```

- [ ] **Step 3：全量回归（这一步不许用 `-k`）**

Run: `python tests/run_gql_tests.py`
Expected: 末行形如 `444/444 单测通过`（分子=分母），**无一行 `FAIL`**

- [ ] **Step 4：装到客户端**

部署方向恒为 **仓库 → 游戏**。这一步先确认 Wow.exe 没在跑（存档只在游戏退出或 `/reload`
时刷盘，游戏开着覆盖会被下次写回盖掉）：

```bash
tasklist //FI "IMAGENAME eq Wow.exe" 2>/dev/null | grep -i wow.exe || echo "Wow.exe 没在跑，可以覆盖"
SRC="/d/Repos/wow/GoldQuestLedger"
DST="/d/games/World of Warcraft/_retail_/Interface/AddOns/GoldQuestLedger"
ls -d "$DST" && cp -f "$SRC"/*.lua "$SRC"/*.toc "$SRC"/*.tga "$DST"/
diff -r "$SRC" "$DST" && echo "NO DIFF"
```

Expected: `NO DIFF`。生效只需客户端 `/reload`（TOC 与 Lua 都是 `/reload` 就生效，不用重开游戏）。

- [ ] **Step 5：提交**

```bash
git add GoldQuestLedger/Core.lua GoldQuestLedger/GoldQuestLedger.toc CHANGELOG.md
git commit -m "feat: GQL 批量本轮不做 T6（0.6.6：版本三处一致 + 更新说明 + 装到客户端）"
```

---

## 装机后只有两条是静态定不死的

都不是逻辑，是界面层级与事件顺序，玩家正常用一次批量就同时看到结果（不用另外跑图、不用开别的界面）：

1. 确认框出现在矩阵**前面**，没被压在后面看不见 —— T2 已经把这一笔层号写进日志
   （`/gql log` 里"弹确认 … 矩阵层=DIALOG→MEDIUM"那条；离线用例已经断住这一行），
   看不见就把那一行报回来。
2. ESC 先关确认框、不顺手把矩阵一起关掉（矩阵自己挂了 ESC 关闭，`Config.RegisterEscapeClose`；
   弹窗那一条给的 `hideOnEscape = true` 就是为这一条）。

## 设计文档 §六 12 条测试 → 用例对账

| spec §六 那一条 | 落在哪条用例 |
| --- | --- |
| 1 批量记只碰非 done 非 skip；已是 skip 不重写（比 `t`） | T2 主用例 ①②③ + T1 判据表 |
| 2 批量撤清这一行所有 skip，含单独点的；done 不受影响 | T2 主用例 ④ |
| 3 方向判定：混合行→记，记完再点→撤 | T2 主用例 ①+④ + T1 判据表 |
| 4 整行全 done → 不弹框、不写账、日志一行 | T2 `refuses_when_the_row_is_all_done`（空态那一档在 T1 判据表里） |
| 5 `k` 算不出 → 不弹框、整批不写、不留隐形记录 | T2 `refuses_when_the_identity_is_undetermined` |
| 6 家族行不被单独一套判据误伤 | T1 `counts_follow_the_displayed_row_state`（折出来的 done 就是一格 done；折叠本身归 Ledger 自有用例） |
| 7 取消/ESC/点外面不写；确认后写入数与文案一致 | T2 `cancel_writes_nothing` + 主用例 ⑥ |
| 8 弹窗期间换轮 → 整批不写 + 日志说明 | T2 `drops_the_batch_when_the_round_rolled` |
| 9 同一弹窗在屏上时再点 → 忽略、不叠 | T2 `ignores_a_second_click_while_open` |
| 10 竖排表头同入口 + 仍可悬停 + 滚轮仍透传 | T4 两条用例 |
| 11 点击路径零重算、只问一次时钟 | T3 `click_and_hover_cost_nothing_extra` |
| 12 竖排左窗格角色名全名画出、过期=全名+过期、横排列头不变 | T5 两条用例 |
| （超出清单）文案格式串安全：名字里带 `%` | T1 `text_survives_a_percent_in_the_quest_name`（替身照抄 FrameXML 的 format 才测得到） |

## 与设计文档的两处偏差（交付时要报给用户）

1. **确认框里没有"地图名"那一行**。设计文档 §二 写的是"地图 + 本轮到几点"。实现只写
   "本轮到几点"：`GroupLabel`/`Api.MapName` 要为这一句问一次客户端接口，而矩阵的行名本来就是
   按名字去重出来的（`Ledger.MatrixRows` 那张分数表），一句话换一次接口不划算。
2. **点击到弹框那一段改成读渲染时那份 row**（设计文档 §一 只说"取当前这一行的每号显示状态"）。
   实现里热区只存渲染时那一份 `__row`，判据、方向、文案全部就地读它，悬停提示也读它 ——
   于是版面在点击与悬停时**一次都不重算**，客户端只多问一次时钟；换轮/重载的安全门
   留在"点确认"那一步（`Mx.ApplyBatch` 自己走 `Mx.BatchRowOf` 重新解析）。
   这比"点击时重算一遍"更贴 spec 的"点击路径零新增接口调用"，也更贴"数字与眼前那份一致"。
