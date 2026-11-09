# GQL 当前角色排首位 设计

**Goal:** 任务×角色矩阵里，**当前登录的号永远排在角色轴的第一个**（横排=最左一列、竖排=最上一行、
`/gql dump` 汇总=第一行），角色多到出滚动条时打开面板第一眼就是自己，不用拖拽去找。

**用户诉求原话**：「当前角色首位显示，比如角色横排的时候，当前角色在最左侧，这样角色多的时候
（滚动条）不至于还要拖拽看」。

**已确认的三条口径**（2026-11-09 逐条问答定稿）：

1. 生效范围：**横排列头 + 竖排左栏 + `/gql dump` 导出**，三处都要当前号在最前。
2. 当前号排第一之后，**其余照旧按名字**（不引入"按最近同步"、也不把"过期"号挑到尾巴）。
3. 横滚条不要留着上次拖的位置 —— **每次打开矩阵归零**；追问后扩到竖排：**两轴都归零**。

## 全局约束

- **顺序真源只有一处**：`Ledger.Characters`（[Ledger.lua:2029](file:///D:/Repos/wow/GoldQuestLedger/Ledger.lua#L2029)
  现在就是 `table.sort` 的名序），横排列、竖排行、导出三处全读它。判据不许在显示层各写一份
  （10-25 那轮定过"同一份账本两处不许报两个数"，顺序也算这一个数）。
- **Ledger 不碰任何 WoW 接口**：当前号由调用方传进 `curKey`，与已有
  `L.State(db, charKey, questID, gate, now, curKey)` 同一个先例。
- **重绘路径零新增接口调用**（既有纪律）：横排渲染本轮已经问了一次当前号
  （[Matrix.lua:1776](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1776)），只是位置在取列之后；
  把它挪到取列之前，一次问、两处用。
- **存档一个字段不动**：这是显示顺序，`db.chars` 的 key、数据、判据、schema 全不碰；
  不需要迁移，也不升 schema 版本。
- `curKey` 不传时行为**逐字等于现状**：现有矩阵/导出用例不该因为本轮集体变红。

## 排序口径

```
L.Characters(db, curKey) -> { charKey, ... }
```

- `curKey` 是字符串且**确实出现在 `db.chars` 里** → 它放第 1 位，其余按现在的名序原样跟在后面。
- `curKey` 为 `nil`、不是字符串、或账本里没这个号 → 逐字等于现状（纯名序）。
  当前号问不到时**不猜、不报错、不空列表**：读不到就按读不到的原样画。
- 不在账本这种情况是真的：新号第一次登录、`Sync` 还没写进 `db.chars` 之前，
  矩阵里本来就没有自己那一列（空态那句"还没有角色数据"），排序无从优先。

账本里的 key 与 `curKey` 同源（`Sync` 就是拿 `CurrentCharKey()` 当 key 写的，
[Sync.lua:1173](file:///D:/Repos/wow/GoldQuestLedger/Sync.lua#L1173)），所以比较用现成的
`tostring(a) == tostring(b)` 口径（横排认当前列 [:1859](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1859)
用的就是这条），不引第二套规范化。

## 改动点（四处，全是"多传一个参数"）

| 位置 | 改法 |
| --- | --- |
| `Ledger.Characters(db, curKey)` | 加可选入参 + 那一条优先规则 |
| `Ledger.MatrixColumns(db, now, curKey)` | 只把 `curKey` 透传给 `Characters`；`count`/`gold`/`stale` 判据一行不动 |
| `Matrix.Render` | 问当前号那句 pcall 从 :1776 提到取列 :1743 之前，`MatrixColumns(db, now, curKey)` |
| `Dump.Run` | 补问一次当前号（手敲命令，多问一次名字不心疼），`Characters`/`MatrixColumns` 两处都带上 |

- **竖排不改一行**：`Mx.RenderCharsInRows` 吃的是 `Render` 传下来的同一份 `cols`
  （[Matrix.lua:1748](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L1748)），当前号自然成为最上一行；
  它自己那套"当前号底色/表头底色"本来就按 key 找 `curI`，序变了 `curI` 跟着变，代码不动。
- **`L.MatrixRows` 里那句 `L.Characters(db)`（:2937）不传 `curKey`**：它只按 key 逐号取状态、
  不看顺序，保持逐字现状（传了也不多任何东西，但"哪里在乎序"这件事必须只有一处在说）。
- 竖排函数头那条"列序真源始终在 Ledger、不许重排"（[Matrix.lua:2026](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L2026)）
  **口径不变**，只补一句说明：那份真源里现在含"当前号优先"。

## 滚动位置：查完是现状已满足，零代码

开面板的唯一入口是 `Mx.Toggle`（`/gql matrix` [:2329](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L2329)、
小地图按钮 [:2450](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L2450) 都走它；× 和 ESC 只 `Hide`），
而它打开那一下已经做了 `offX, offY = 0, 0`（[Matrix.lua:2367](file:///D:/Repos/wow/GoldQuestLedger/Matrix.lua#L2367)）。

⇒ 本轮**不加代码**，只补一条用例把"重开矩阵两轴都在最左/最上"钉住：以后有人把"记住滚动位置"
当改进加回来时，用例先红，而不是玩家实测才发现当前号又被滚出视野了。

## 不做的事

- 不加设置开关。诉求是"不至于还要拖"，不是一个可选项；开关只会多一颗没人点的按钮。
- 不做点列头排序、不做角色置顶/收藏、不按职业或最近同步排。
- 不改 `stale`（过期）号的画法与判据。
- 不轮询、不新增事件注册、不往 `Geom`/`Head` 里加新状态。

## 测试与验收

单元（`python tests/run_gql_tests.py`，沿用现有离线替身）：

1. `characters_current_first` —— 三个号 + `curKey` 是中间那个 ⇒ 序 = [当前号, 名序其余两个]；
   `curKey=nil` ⇒ 逐字名序；`curKey` 不在账本 ⇒ 逐字名序；`curKey` 传非字符串（数字/`true`）⇒ 不报错、逐字名序。
2. `matrix_columns_current_first` —— 只换序：每个号的 `count`/`gold`/`stale`/`lastSync`/`class`
   与不传 `curKey` 时逐字段相等（判据被顺手改掉必须红）。
3. 横排渲染 —— 当前号那列的表头 x 原点 = 0（第一列），且 `offX=0` 时画得出来；
   整列底色帧 `curHi.__cx == 0`。
4. 竖排渲染 —— 当前号那行 y = 0（第一行），行底色帧挂在 y=0；
   `Mx.rowCount`（任务条数口径）不因换轴而变，与横排同值。
5. `dump_current_char_first_and_consistent` —— "本周汇总"第一行是当前号，
   且"金币任务"每行的格子顺序与汇总行的顺序**同一个序**（两处不同序正是这条要防的）。
6. `reopen_matrix_zeroes_both_offsets` —— 滚一下 → 关 → 重开 ⇒ `offX` 与 `offY` 都是 0（钉现状）。
7. 回归 —— 现有全部用例保持绿；`MatrixColumns(db, now)` 两参调用（老用例、Dump 之外的调用方）行为不变。

游戏内验收（用户实测，截图回传）：

- 角色 ≥ 视口宽度（横排 15 列以上）时，打开矩阵第一眼就是自己那一列，没拖过滚动条；
- 切到"角色竖排"设置后，打开矩阵第一眼就是自己那一行；
- 拖到右边关掉再重开，回到最左。
