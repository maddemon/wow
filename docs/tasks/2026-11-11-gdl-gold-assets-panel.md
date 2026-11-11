# GDL 金币资产展示（2026-11-11）

## 目标

1. 日历右栏角色行悬停时，在「收入 / 支出」之外显示该角色的金币。
   - 当前号：随 PLAYER_MONEY 实时写快照。
   - 其他号：只能显示该号最近一次登录时的快照；统一文案「金币」。
   - 没有快照的号显示「金币 —」，不编造 0。
2. 日历下方读数带新增两个方块：
   - 战团：来源 `C_Bank.FetchDepositedMoney(Enum.BankType.Account)`；未访问过战团银行（读不到）时显示「—」，悬停说明「打开一次战团银行后记录」。
   - 总计：已记录角色金币快照之和 + 战团金币；悬停注明「仅统计已登录过的角色」；没有任何已知数据时显示「—」。
3. 读数带改为贯穿窗口底部（6 个等分方块），右栏（角色列表）底部对齐日历网格底部，列表滚动区相应缩短。

## 实现要点

- 存档：`GDLDB.goldByChar[角色键] = 铜币`；`GDLDB.warbandGold = 铜币`（可为 nil，表示没读到过）。
- 写入：Ledger 提供 `NoteCharGold / NoteWarbandGold`；Money 在登录、每次 PLAYER_MONEY、`ACCOUNT_MONEY`、`BANKFRAME_OPENED` 时调用。
- 读取：Ledger 提供 `GetCharGold / GetWarbandGold / GetKnownCharGoldTotal`。
- 界面：`TOTALS_PRESETS` 只保留四个范围档；新增 `SUMMARY_BLOCKS` 统一 6 个方块顺序（本周/上周/本月/全部/战团/总计）。
- 静态字全部走 `ApplyStaticTexts`，中英两张语言表同步补键。
- 金币/战团读不到时显示「—」，与真正的 0 区分。

## 验收

- [x] 右栏角色行悬停为 3 行：收入 / 支出 / 金币。
- [x] 战团与总计方块可显示；**战团不挂悬停**（悬停了也还是战团），总计悬停说明金币构成。
- [x] 只有流水明细行显示银/铜；其余读数与所有悬停一律只写金币。
- [x] 大额金币按「万 / 亿」压缩（52.8万 / 1.05亿），方块与提示都摆得下。
- [x] 读数带铺满窗口底部，右栏列表底部与日历网格底部对齐。
- [x] 中英文语言表键齐全、占位符一致、字形白名单通过。
- [x] `python tests/run_gcl_tests.py` 全绿（79/79）。

## 完成记录（2026-11-11）

- 存档：`goldByChar` 放入 DB 默认值；`warbandGold` 缺省即「未记录」。
- 写入：`Ledger.NoteCharGold / NoteWarbandGold`；Money 在 PLAYER_LOGIN、PLAYER_MONEY 写角色快照；
  `BANKFRAME_OPENED` 标记「访问过战团银行」并首次取数，`ACCOUNT_MONEY` 刷新；
  未访问前即使客户端答 0 也不落档，避免把「没读到」写成「战团没钱」。
- 读取：`GetCharGold / GetWarbandGold / GetKnownCharGoldTotal / GetGoldAssets`，缺失一律 nil。
- 界面：`SUMMARY_BLOCKS` 六块（本周/上周/本月/全部/战团/总计），宽度 148×5 + 152 = 892 铺满；
  右栏 `PANEL_H` 到网格底，列表可见行 16 → 14。
- 测试：新增 `calendar_gold_snapshots_and_assets`，覆盖快照、战团 0 与未访问、总计口径、
  旧存档缺字段、ACCOUNT_MONEY 刷新与读不到时不清缓存。
- Code Review 后调整：可选事件挪到核心事件之后注册；战团不在登录时刷新（防 0 覆盖旧缓存）；
  去掉 `assetsSlot.known` 死字段；快照写入不再改动当前号状态。

## 0.3.1 调整（2026-11-11 玩家截图反馈）

- 战团方块去掉悬停：本来就只是「战团 = 数字」，重复弹一行没有信息量。
- 只有流水明细行用 `FormatMoneyFull`（金/银/铜）；日历格子、右栏行、读数方块等
  所有悬停改用 `FormatMoneyIcon`，只写金币。
- `FormatMoneyIcon` 增加大额压缩：≥1万金用「万」、≥1亿金用「亿」，
  万/亿内最多两位小数、末尾 0 不写，超过 100 万/亿取整；
  解决几千万/上亿金币撑满方块与悬停折行的问题。
- 小地图悬停同样改用只显示金币的格式化。
- 新增 `calendar_money_format_compact_gold_only` 用例覆盖 0 / 万 / 亿 / 负数 / 非数字，
  并钉住流水行仍保留银/铜。

