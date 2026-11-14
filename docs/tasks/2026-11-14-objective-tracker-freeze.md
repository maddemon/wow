# 任务列表卡进度：暴雪 bug 还是插件？（2026-11-14 调研）

## 结论

**是暴雪的 bug，但需要插件污染来触发。** 二者不矛盾：暴雪 12.1.x 新引入的
"secret aura" 限制让追踪器排版函数里的光环读取在污染执行下必然抛错，
一旦有插件（或宏）把污染带进这条更新链，任务进度就停止刷新，
直到 `/reload` 重建 `ObjectiveTrackerFrame` 才恢复。

- 上游 issue：<https://github.com/Stanzilla/WoWUIBugs/issues/882>（2026-09-13 提交，至今未关闭）
- 同源重复：<https://github.com/Stanzilla/WoWUIBugs/issues/890>
- 旁证：Aurora 12.1.0.8 changelog、Ace3 issue #713、本机 `!!!163UI!!!` 2026-10-23 更新说明（`ChangeLog.lua:221-222`）记录同一问题。

## 机理

报错原文：

```
GetAuraDataByIndex(): Auras cannot be accessed when secret while tainted by '...'
```

issue 里给的是两条交互触发路径，但**不需要任何操作也会触发**，本机源码核实如下。

### 路径 A：任何追踪器刷新（做任务时每秒都在发生）

```
QUEST_LOG_UPDATE / QUEST_WATCH_LIST_CHANGED / QUEST_TURNED_IN / QUEST_POI_UPDATE ...
  -> QuestObjectiveTracker:MarkDirty()
    -> ObjectiveTrackerFrame(container):Update()
      -> 按 uiOrder 遍历模块，第一个就是 ScenarioObjectiveTracker
        -> ObjectiveTrackerModuleMixin:Update()
          -> BeginLayout()        -- 先把 isDirty 置 false、state 置 Skipped
            -> LayoutContents()
              -> ShouldShowMawBuffs()   -- 12.1.x 新增，位于函数第 4 行
                -> C_UnitAuras.GetAuraDataByIndex("player", 1, "MAW")  -- 抛错
```

`ObjectiveTrackerContainerMixin:Update()` 里那个 `for i, module in ipairs(self.modules)` 循环
**没有 pcall**。ScenarioObjectiveTracker 排第一，它一抛错，后面所有模块
（任务、世界任务、奖励目标……）这一轮全部不更新。

而且抛错点在 `BeginLayout()` 之后、`EndLayout()` 之前：
- `isDirty` 已经被清掉，模块忘了自己脏；
- `state` 停在 `Skipped`、`hasContents=false`；
- `EndLayout()` 没跑，旧 block 既没回收也没重排。

结果就是**画面上停在最后一次成功排版的样子**，数据变了也不重画。

### 路径 B：光环变化（完全无操作）

`ScenarioObjectiveTracker` 的 events 里有 `UNIT_AURA`，它的 `OnEvent` 直接调
`ShouldShowMawBuffs()`。也就是说**任何 buff/debuff 变化**（吃食物、喝药、任务给的光环、
战斗开始结束）都会走一次那条裸读光环的调用。

### 共同前置条件

`C_Secrets.ShouldAurasBeSecret()` 为真 **且** 执行上下文被插件污染。
污染标记是粘在帧/表上的，一旦某个插件写过 `ScenarioObjectiveTracker` 相关对象，
**直到 `/reload` 之前都不会消失**——所以表现是"卡住就再也动不了"，而不是偶发一跳。

前置条件 `C_Secrets.ShouldAurasBeSecret()` 为真（12.1.x 起在特定场景成立）；
`/reload` 会重建追踪器帧并清空污染执行状态，所以"重载就好"。

## 为什么"以前没有"

1. `ShouldShowMawBuffs()` 这条路径是 12.1.x 才进的 `LayoutContents()`；更早版本排版不读光环，
   同类污染最多导致位置错乱，不会中断。
2. 需要"污染 + 追踪器刷新"同时成立才复现。追踪器刷新本身在正常做任务时就一直在发生
   （`QUEST_LOG_UPDATE`、`UNIT_AURA`），所以体感是"什么也没干，突然就卡了"，
   而不是只在折叠/展开时才出问题。
3. 插件生态里长期存在直接调用/替换追踪器更新函数的写法，暴雪这次把它们的代价放大了。

## 本机排查结果

### 截图里的三段 UI 分别来自

| 元素 | 来源 |
| --- | --- |
| "所有目标 / 已接 16/35 个任务" | `LNui/countQuests.lua:25`，写 `QuestObjectiveTracker.Header.Text` |
| 圆形数字序号 | `WorldQuestTab/WorldQuestTab_ObjectiveTracker.lua`（已针对 #882 改造） |
| "自动交接" 复选框 | `AutoTurnIn/CfgAutoTurnIn.lua`（已改为 UIParent 锚定，不再写进追踪器） |

也就是说，屏幕上看到的是**暴雪 `ObjectiveTrackerFrame` + 本地插件增强**，不是第三方独立追踪器。

### 已确认打过的本地补丁

- `WorldQuestTab/WorldQuestTab_ObjectiveTracker.lua`：不再 hook
  `ObjectiveTrackerManager.UpdateAll` / `ObjectiveTrackerFrame.Show` / `QuestMapFrame.Show`，
  也不再在插件侧调用任何会触发重排的暴雪函数。
- `WorldQuestTab/Templates.lua:923`、`AutoTurnIn/QuestLevel.lua`、`AutoTurnIn/CfgAutoTurnIn.lua`：
  均带 `12.1.5 追踪器污染（wowuibugs#882）` 改造注释，避开污染路径。
- `LNui/7.0/QuestWatchSort.lua:98-101`：改用 `securecall(ObjectiveTracker_Update, ...)`。

### 剩余嫌疑（尚未逐个排除）

- `LNui/countQuests.lua:29`：在暴雪 `Header.Text` 上 `SetText`，属写入暴雪帧（低风险，但算污染面）。
- `WorldQuestTab/WorldQuestTab_ObjectiveTracker.lua`：仍在暴雪 POI 按钮上 `CreateFontString`。
- `DFCN_DelveHelper/DFCN_DelveHelper.lua:236`：`HideBlizzTrackerPart(ObjectiveTrackerFrame.Header)`。
- `AngryKeystones/Persist.lua:227-292`：直接替换全局 `ObjectiveTracker_Update` 的高危写法；
  `Config.lua` 的 `configDefaults` 不含 `persistTracker/exclusiveTracker`，本机存档也未开启，**当前不生效**。
- `Plumber/Modules/ObjectiveTrackerModule.lua`：文件首行标 `-- Unused`，`SetModuleContainer` 调用被注释，不生效。
- `Auctionator` 追踪器块（未细查）。

### 日志状态

`!BaudErrorFrame` 从未保存错误（`SaveToVariable=false`, `Taint=false`），
`_retail_\Logs\taint.log` 不存在 —— 所以目前没有直接的 taint 证据，只有代码侧的触发条件。

## 如何确认（游戏内）

不需要复现动作，开着日志正常做任务即可：

```lua
/console taintLog 2          -- 打开 taint 日志（重载后生效）
/reload
/dump C_Secrets.ShouldAurasBeSecret()   -- 前置条件；为 false 则本 bug 不会发生
```

卡住之后查 `_retail_\Logs\taint.log`，找 `ShouldShowMawBuffs` /
`GetAuraDataByIndex` 附近的 `tainted by '插件名'`。对照实验：只开暴雪 UI 做同样的任务，不应卡住。

## 处置建议

**短期**：卡住就 `/reload`。

**兜底（推荐，一次装好永久生效）**：写一个只做一件事的小插件，把
`ShouldShowMawBuffs` 换成本地安全版：

```lua
local orig = ShouldShowMawBuffs
function ShouldShowMawBuffs()
    if IsInJailersTower() then return true end          -- 保留噬渊塔原逻辑
    local ok, auraData = pcall(C_UnitAuras.GetAuraDataByIndex, "player", 1, "MAW")
    if not ok then return false end                     -- 被污染时当作"没有 MAW buff"
    return (auraData and auraData.icon) and true or false
end
```

理由：这个函数只用来决定"要不要显示噬渊 buff 模块"，当前版本内容里永远是 false，
吞掉报错没有副作用。代价是它变成插件全局（Aurora 也这么干），但它不参与任何
受保护动作判定，风险仅在 UI 排版。装上后连 taint 都不需要再清——报错被就地吞掉，
`LayoutContents()` 不会中断。

已实现为独立插件：`MawBuffGuard/`（本仓库，有版本管理）→ 安装到
`_retail_\Interface\AddOns\MawBuffGuard\`。游戏内 `/mawbuff` 可查看是否接管成功。

**中期**：继续清理污染源，优先复查上表剩余嫌疑里对暴雪帧的写入
（`countQuests` 的 `Header.Text`、POI 按钮上的 `CreateFontString`、`DelveHelper` 的
`HideBlizzTrackerPart`）。原则：**只读不写暴雪追踪器帧，不在不安全上下文调用其更新函数**。

**长期**：等暴雪在 `ShouldShowMawBuffs()` 加保护或延迟求值（issue #882 仍未关闭）。
