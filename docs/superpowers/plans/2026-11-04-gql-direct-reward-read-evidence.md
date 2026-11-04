# 11-04 取证：不按 M，能不能当场把一条任务的金币读出来

## 〇、这轮要回答的唯一问题

用户 11-04 原话："**我知道任务名的前提就是先按 M，我觉得更应该取证的是你说的通过代码达到按 M 的效果，不要用你说的任务池，就直接读数据**"

⇒ 取证必须挂在**事件上门那一刻**（接/交/日志里挂着，插件自己就知道这条任务的编号），
不能要求玩家先知道任务名或任务号；而且这一轮要试的是**"直接问这一条任务要数据"**这条路，
不是"去看那张图的任务清单"那条路。

**这一轮只取证：判据一个字不动、账本一个字不写、生产路径一个字不改。**
证据回来之后再决定把哪个入口放进生产链（那才是"代码替代按 M"）。

## 一、已经证实的（照抄，不用再试）

1. 金币数字有两个来路，之前混为一谈：
   - 来路 A：那张图的**任务清单里那一行自己带的钱**（`C_TaskQuest.GetQuestsOnMap` 系的行）。
     92576 的 11181872、94967 的 10090112 都是这条读到的。这一份要先有该图的板面数据 —— 这才是"按 M"的实效。
   - 来路 B：**当场问这一条任务的奖励数据**（`GetQuestLogRewardMoney` 那三个候选，
     名单在 `GoldQuestLedger/Api.lua:485-487`，只有这一份要 `HaveQuestRewardData`）。
     日志里 `奖励数据=false` 就是这一份的数据不在机器上。
2. 已证：**接/交那一刻倒计时答得出**（世界地图没开也答：95253 217370、94967 66307/66033、92576 293819、95794 69110→69031）。
   ⇒ 结束时刻这一半不依赖按 M，上一版已经落账（`2ad43d2`）。
3. 已证：`金额=nil` 分两种，靠 `奖励数据` 这一列分开 ——
   `true`+nil = 客户端已经答完"这一档不给金币"（95253 狩猎模式，同一条正常模式答 10137288）⇒ **不许再问、不许造数**；
   `false`+nil = 数据不在机器上（94967 没开地图）⇒ 这轮要试的就是能不能不靠按 M 把它要回来。
4. 已证：这台客户端 `RegisterEvent` 遇到不认识的事件名**直接抛错**
   （`GoldQuestLedger/Sync.lua:1521-1529` 靠这条否掉了 `WORLD_QUEST_LOG_UPDATE`、`Track.lua:195` 否掉了 `WORLD_MAP_UPDATE`）
   ⇒ 事件名可以逐个试探，有就有、没就 MISSING，不用猜。
5. 已证：程序化的"等效按 M"已经在代码里 —— 背靠背两次 `ToggleWorldMap`，屏幕上看不见
   （`GoldQuestLedger/Sync.lua:162-190` `Sync.ForceMapDataLoad`；15 秒冷却；地图已开着就不动）。
6. 已证：`C_QuestRewards.FetchData` 的参数个数**没有任何可靠依据** ——
   warcraft.wiki 与 wiki.gg 都没有这一页（404），本机装的四十多个插件里除了咱们自己没有任何调用点。
   ⇒ 参数只能让客户端自己回答：两种写法都试，把它的报错原文抄进日志。

## 二、明确不做（防越界）

- 不动判据、不动门槛、不动到期、不写账本（探针读到的金额只进日志，不进 `rec.lastMoney`）。
- 不新增常驻定时器/轮询/扫图。探针用的延时是**一次性**的，敲一次命令排一次，跑完自己没了。
- 生产路径（事件现场问答、补读）**这轮一个字不改**，不在这轮调用 `C_QuestLog.SetSelectedQuest`。
  选中任务日志这一条副作用只存在于手动探针里，且探完立刻恢复（用户 11-04 点头："放进探针，用完恢复"）。
- **不改 `GoldQuestLedger/Api.lua`**（工作树里有另一个会话未提交的改动），入口一律用现成的
  `GQL.Api.Resolve` / `GQL.Api.Callable` 从探针侧拿。

## 三、T1：让插件自己知道"该问哪一条"

`GoldQuestLedger/Sync.lua`：金额问不出的那几条，在内存里就近记着（会话级，不落档）。

```lua
--- 金额问不出的那几条，就近记着（只在这台客户端这次开机期间有效，不落档）。
--- `/gql probe fetchdata` 靠它决定"该问哪一条" —— 玩家要是得先知道任务名才能取证，
--- 那个前提本身就是按 M，取证不能建立在按 M 上（用户 11-04）。
local recentMoneyMiss = {}
local RECENT_MISS_MAX = 5
local function noteMoneyMiss(questID, now, hasData)
    for i = #recentMoneyMiss, 1, -1 do
        if recentMoneyMiss[i].questID == questID then table.remove(recentMoneyMiss, i) end
    end
    recentMoneyMiss[#recentMoneyMiss + 1] = { questID = questID, at = now, hasData = hasData }
    while #recentMoneyMiss > RECENT_MISS_MAX do table.remove(recentMoneyMiss, 1) end
end

function Sync.RecentMoneyMisses() return recentMoneyMiss end
function Sync.LastMoneyMiss() return recentMoneyMiss[#recentMoneyMiss] end
```

落点：`Sync.CaptureQuestData` 里金额已经问完、`hasData` 也已经问完那一段之后，
`money == nil` 且 `wq ~= false` 时调 `noteMoneyMiss(questID, now, hasData)`。

**RED 用例**（`tests/run_gql_tests.py`，全部走真实事件入口，不直接调内部函数）：
1. 接一条金额答不出的世界任务 ⇒ `Sync.LastMoneyMiss().questID` 是它、`hasData` 如实（false 就是 false）。
2. 金额答得出的那一条 ⇒ **不记**（`RecentMoneyMisses()` 长度不变）。
3. 同一条连着问不出两次 ⇒ 表里只有一条，且排在最后。
4. 连着 6 条都问不出 ⇒ 只留最近 5 条，最旧那条先丢。

## 四、T2：`/gql probe fetchdata` —— 三个动作逐个试，逐个报

`GoldQuestLedger/Probe.lua` 新增一段，`Probe.Run(arg)` 开头认这个开关（`arg` 里带 `fetchdata` 就走这一段，
不再打整份报告）。任务号来源：命令后面直接写数字就用那个数字；没写就取 `Sync.LastMoneyMiss()`；
两个都没有 ⇒ 只老实说一句"最近没有金额问不出的记录，接一条或交一条世界任务之后再敲这条命令"，
**一个接口都不碰**。

### 读一次是什么

```lua
local function ReadReward(questID)
    local has = GQL.Api.HasRewardData and GQL.Api.HasRewardData(questID)
    local money = GQL.Api.QuestRewardMoney and GQL.Api.QuestRewardMoney(questID)
    return has, money
end
```
金额候选一律取自 `GQL.Api.MoneySources()`（生产同一份名单，探针不许另抄一份）。

### 动作序列（每一步当场 `GQL.Config.Log` 一条，不等整段跑完）

| 序 | 日志里写的动作 | 干什么 |
|---|---|---|
| 0 | `起点` | 读一次 + 这条在不在任务日志里 + 世界地图开没开。日志序号一律用 `C_QuestLog.GetLogIndexForQuestID`（**本仓库已在用**，`GoldQuestLedger/Probe.lua:148-154`），反查不到就写"判不了"；`GetQuestLogIndexByID` 老名字，本机存不存在由这一行顺带报出来，不许当成前提 |
| 1 | `动作1=C_TaskQuest.RequestPreloadRewardData` | 入口不在 ⇒ `MISSING`；在 ⇒ pcall，报 `OK`/`抛错=原文`，紧跟着复看一次 |
| 2 | `动作2=C_QuestRewards.FetchData(只给任务号)` | 同上；**抛错就再试 `动作2b=…(带上四个 true)`**，两行都留，报错原文照抄 —— 客户端自己说参数怎么给 |
| 3 | `动作3=选中这条再退回` | 存旧选中项 `GetQuestLogSelection()` ⇒ `C_QuestLog.SetSelectedQuest(任务号)`（抛错则改用上面 `GetLogIndexForQuestID` 反查到的日志序号再试一次）⇒ 复看 ⇒ **恢复旧选中项**（旧值是数字用 `SelectQuestLogEntry`，nil 用 `ClearQuestLogSelection`），恢复成没成照实报 |
| 4 | `稍后复看 +1秒/+3秒/+8秒` | 三条一次性延时，专门接异步回包；到点各复看一次并打行 |
| 5 | `对照=等效按M` | `GQL.Sync.ForceMapDataLoad()` + `+2秒/+6秒` 各复看一次。这一项明确标"对照"，用来回答"直读到底能不能替代按 M" |
| 6 | `事件名试探` | 一个临时 frame，逐个 pcall `RegisterEvent` 试探候选名（`QUEST_REWARD_DATA_RECEIVED`、`QUEST_REWARD_SET_CHANGED`、`TASKPOOLS_LOADED`），有就写"有"、抛错就写 MISSING；`QUEST_LOG_UPDATE` 作已知对照组（它必须报"有"，用来证明这一段本身没跑歪）。段尾注销并丢弃这个临时 frame |

每行格式（照现在现场问答那一路的措辞，全中文、不带行话）：

```
直读试探 动作2=C_QuestRewards.FetchData(只给任务号) 状态=抛错:Wrong arguments 之后 奖励数据=false 金额=nil
```

末尾追加一条汇总：`直读试探结论 quest=<号> 哪一个动作把金额问出来了=<动作名或"都没问出来">`
—— 只有 `金额` 从 nil 变正数的那一步的动作名才算数；**没有就如实写"都没问出来"**。

### RED 用例

1. 三个入口全都不存在 ⇒ 三行状态都是 `MISSING`，延时行照排，`db.quests` 一个字段没动。
2. `RequestPreloadRewardData` 在 ⇒ 恰好被调用 **1 次**，紧跟一条复看行。
3. `FetchData` 只给任务号时抛错 ⇒ 自动再试带上四个 true 的那一行，**两行都在报告里**，报错原文进了日志。
4. `GetQuestLogSelection()` 返回 3 ⇒ 恢复时 `SelectQuestLogEntry` 收到的正是 **3**；返回 nil ⇒ 走 `ClearQuestLogSelection`。
   （这条专门钉"不许把玩家任务日志的选中项留在改过的状态"。）
5. 没写任务号且 `Sync.LastMoneyMiss()` 为空 ⇒ 只有一句实话，**零接口调用、零延时**。
6. 一次命令排下来的延时**恰好 5 条**（+1/+3/+8 与对照 +2/+6），推进 `GQL_FireTimers()` 后各跑一次、
   **不在回调里重排**（用例断言跑完后队列里没有新增）。
7. 复看时金额答出了正数 ⇒ 汇总行写"动作X问出来了"，但 `rec.lastMoney` / observation / 按号读数
   **全部保持原值**（这一轮不落账）。

harness 需要补的替身：`GetQuestLogSelection` / `SelectQuestLogEntry` / `ClearQuestLogSelection` /
`C_QuestLog.GetLogIndexForQuestID` / `C_QuestLog.SetSelectedQuest`，**每条用例按需给或不给**（不给就是测"入口缺失"那一路）。
替身不许凭空挂到常驻环境上当"客户端有"（见 10-27 矩阵砖掉那条教训）。

## 五、代价（用户 11-04 立的规矩：提方案先报代价）

- 平时：零。生产路径这轮不改，探针不敲就不跑。
- 敲一次命令：≤4 次"要数据"调用 + 6 次复看（每次 1×`HaveQuestRewardData` + 最多 3 个金额候选）
  + 1 次等效按 M（跟玩家自己按一次 M 一模一样）+ 4 次事件名注册试探。
- 一次性延时 5 条，最长 8 秒，跑完即散；无常驻定时器、无轮询、无扫图。

## 六、只有游戏内测得到的（这版部署后要回传的东西）

1. 面板全程关着，接一条世界任务（金额那一刻多半答不出）→ 立刻 `/gql probe fetchdata` →
   `/gql log` 全发回来。看两件事：
   - `动作2` 那两行的**报错原文**（定 `C_QuestRewards.FetchData` 的参数怎么给）；
   - `直读试探结论 …=` 到底是哪个动作、还是"都没问出来"。
2. `事件名试探` 那几行（有没有"数据到了"的通知；有的话下一轮把补读那 6 次换成事件上门直接落账）。
3. 同一条任务的上一轮遗留问题仍在看：`补读到金额 …` vs `补读 6 次仍问不出金额 …（不猜）`。
