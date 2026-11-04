# 2026-11-04 GQL：接任务/交割那一刻把数据收下（结束时刻 + 金额）

## 〇、这一轮改什么，一句话

矩阵关着的时候接世界任务，插件从来没有替它问过那两个数 —— 于是结束时刻空着、金额空着：
结束时刻空着 ⇒ 交割烙下的"这一条是哪一轮"只能猜 ⇒ 真值后来进来了对不上 ⇒ 你看到的"勾没了"；
金额空着 ⇒ 金币那道门槛永远过不了 ⇒ 你看到的"做了却没这一行，换个号按 M 才有"。
这一轮把这两个数改成**在事件上门的那一刻就问、问到了就收下**。

## 一、实测依据（不许靠猜，四条都有出处）

1. **你 11-04 回传的三行日志**（95794，世界地图=没开）：
   接任务 `倒计时=69110 金额=nil 奖励数据=false 账本已有结束时刻=1793833248 编号=95794:1993148`；
   交割 `倒计时=69031`（正常递减），金额仍 `nil`、`奖励数据=false`；随后 `交割当场落账 quest=95794`。
   ⇒ **接了、甚至做完了，结束时刻照样问得出**（本会话早先"接了就不答"的推断作废，已更正留档）。
   ⇒ **金额在这一刻问不出**，客户端自己说"奖励数据还不在本机"。
2. **真存档**（`WTF/Account/BLUEFANE/SavedVariables/GoldQuestLedger.lua`，schema=11，
   cycleAnchor=1793228399）里 95794 那条：`roundEnd=1793833248`、`goldMoney=10557708`、
   `goldKey=95794:1993148` —— 1793833248 = 当时那次"现在 + 剩余 69110 秒"，是清单里读到的真值。
   ⇒ 这条是**健康形状**：从接单到交割编号一路同一档，勾不会丢。它证明"金额读不到"跟"账本里有没有旧值"无关，
   只跟"这一刻本机有没有这份数据"有关。
3. **代码事实**：`Api.QuestRewardMoney` 只认正数，客户端对没预加载的任务回 0 ⇒ 判成问不出
   （`GoldQuestLedger/Api.lua:478-487`、`509-527`）。这一步是对的，不许改成"0 当金额"。
4. **代码事实**：全插件唯一向服务器要奖励数据的地方是
   `Api.PreloadRewardData`（`GoldQuestLedger/Api.lua:299`），唯一调用点在世界任务清单遍历里
   （`GoldQuestLedger/Api.lua:187`）；那段只在矩阵开着或登录那一次才跑
   （`GoldQuestLedger/Sync.lua:1205`）；连数据回来后的 5 秒补读也被同一道门挡着
   （`GoldQuestLedger/Sync.lua:150`）。⇒ **矩阵关着时既没人去要，要到了也没人去读**。

## 二、用户 11-04 定的三条（优先级高于此前一切留档）

1. 接任务/交割那一刻问出来的**结束时刻，当场写进这条记录**。
2. **金额**：那一刻向服务器要一次，数据回来就落账。
3. 已经被"猜的那一档"烙下的完成记录（你丢的那两格）**不改** —— 只保证以后新烙的编号是对的。

## 三、任务一（T1）：结束时刻当场收下 + 日志旁路的来源标签用错

### T1-a 把"只问不写"的那个函数改成"问 → 收 → 打日志"

`Sync.LogQuestRead`（`GoldQuestLedger/Sync.lua:277`）改名 `Sync.CaptureQuestData(questID, reason)`，
三个调用点跟着改名处调用（`QUEST_ACCEPTED` 那一路、`RecordTurnIn`、日志旁路那一趟）。
顺序固定：**先读倒计时 → 收下 → 再算编号 → 金额收下 → 最后打那一行日志**（编号必须在结束时刻落账之后算，
与清单遍历那一段同一个顺序，见 `GoldQuestLedger/Sync.lua:1045-1110` 的原注释）。

倒计时答得出（`type(secs)=="number" and secs>0`）就写，**照抄清单遍历那一句，不另发明写法**：

```lua
if rec then
    L.SetExpiry(rec, now, secs, L.KeywordLogAdmits(db, questID, rec, now))
end
```

- 不许搬 `SyncKeywordQuestsFromLog` 里那道"`secs<86400` 或≈周重置就不写"的门（`GoldQuestLedger/Sync.lua:399-403`）。
  那道门是给"常驻周常/关键词收进来的行"准备的；95794 这一条答出 69110 秒、真值就是"这一周结束的那一刻"，
  搬过来会把每一条 24 小时内到期世界任务的真实结束时刻一律拒收 —— 正是要治的那个形状。
  常驻周常那一族的保护由 `SetExpiry` 自己那三句早退负责（`GoldQuestLedger/Ledger.lua:1176`），不用这里加门。
- 答不出（nil）就一个字不写。日志行照打，写着 `倒计时=nil`。
- `rec` 为 nil（账本压根没这条）时：本轮**不建记录**（建记录是日志旁路那一趟的事，它自己带金币判据）。

### T1-b 日志旁路建记录时用对来源标签

`GoldQuestLedger/Sync.lua:248` 现在是 `GQL.Ledger.AddQuest(db, questID, now, "questlog")`。
"questlog" 这个标签的语义是"**关键词命中**、从任务日志收的"，是一道上板点名凭据
（`GoldQuestLedger/Ledger.lua:1274-1280`：烙 `kwLogAdded`，`SetExpiry` 对它早退）。
这一趟的准入判据是"客户端答得出是世界任务 + 当场读到正数金币"，跟关键词一个字没关系 ⇒ 标签用错：
既绕开 500 金门槛，又让这一条永久拿不到真结束时刻（正是 92576 的形状）。
改成 `GQL.Ledger.AddQuest(db, questID, now)`（无 addedBy，照 `GoldQuestLedger/Track.lua:132` 那一例）。

已存档里被烙错的 `kwLogAdded` **不需要迁移**：`SyncKeywordQuestsFromLog` 收尾那句
"关键词没命中就撤布尔位"（`GoldQuestLedger/Sync.lua:425-429`）本来就会在下次同步清掉它。

### T1 用例

- 改写 `test_event_moment_asks_time_and_money_without_writing` ⇒ `test_event_moment_writes_the_end_time_it_asks`。
  **改写理由要在用例文档字符串里写死**：用户 11-04 决定"当场写进这条记录"，
  原断言"一笔都不许写"是被这条决定取代的旧契约，不是为了让测试变绿。
  - 正向：88 这条夹具答得出倒计时 7200 ⇒ `roundEnd == 当时 now + 7200`，且日志那一行照打。
  - 对照组一：倒计时答不出 ⇒ `roundEnd` 仍 nil、其余字段一字不动、日志行写着 `倒计时=nil`。
  - 对照组二：这条记录带 `regKey="kw:..."`（注册周常）⇒ 即使答得出倒计时也不写（`SetExpiry` 自己的早退）。
  - 反向：`turnins` 里那一笔的 `k` 仍是交割那一刻算出来的编号；**已经存在的旧流水一笔都不许回写**（第 3 条决定）。
- 新增 `test_log_bypass_gold_quest_keeps_a_real_end_time`：
  日志旁路收进来的那条（关键词一个没配、客户端答"是世界任务"+金额>0）
  ⇒ 断言 `kwLogAdded` 为 nil，且随后一次 `QUEST_ACCEPTED` 的现场问答能把它自己的 `roundEnd` 写进去。
  对照组：关键词命中的那条照旧烙 `kwLogAdded`（`SyncKeywordQuestsFromLog` 那一路不许受影响）。

## 四、任务二（T2）：金额向服务器要一次，回来就落账

**落点选择（这条要先讲清，免得凭空造一个客户端没有的事件）**：
`C_TaskQuest.RequestPreloadRewardData` 的回包有没有对应事件名，这台客户端**还没实测**。
不许按名字猜着注册事件（上一次 `WORLD_QUEST_LOG_UPDATE` 就是这么炸掉登录同步的，
见 `GoldQuestLedger/Sync.lua:1394-1400`）。所以这一轮：
**发请求 + 搭本来就有的事件把补读带回来** —— 做任务时 `QUEST_LOG_UPDATE` 本来就成串触发，
日志旁路那一趟本来就有 10 秒节流，补读挂在它后面，零新增事件、零新增定时器。

### T2-a 问不出金额就发一次请求

`Sync.CaptureQuestData` 里金额那条分支（现在只把答案打进日志）加一句：

```lua
if money == nil and GQL.Api.PreloadRewardData then
    local alreadyAsked = askedRewardData[questID]
    if not alreadyAsked then
        askedRewardData[questID] = 0     -- 0 = 已发请求、还没补读
        GQL.Api.PreloadRewardData(questID)
    end
end
```

`askedRewardData` 是函数外的会话级表（键 questID）。`Api.PreloadRewardData` 自己会先问
`HaveQuestRewardData`，答"已经在本地"就不发请求（`GoldQuestLedger/Api.lua:299-304`）⇒ 一条任务每次开机最多要一次。
日志那一行末尾加一段 `金额=问不出（已向服务器要这份数据）` / `金额=问不出（早就问过服务器了）`，
让玩家看得出这不是插件在装死。

### T2-b 补读：数据到了就落账

`Sync.LogBypass` 开头（节流之后）加一小趟，只走 `askedRewardData` 里的几条：

```lua
for questID, tries in pairs(askedRewardData) do
    if tries >= 0 then                          -- -1 = 已了结，不再问
        local rec = db.quests[questID]
        local okM, m = pcall(GQL.Api.QuestRewardMoney, questID)
        if okM and type(m) == "number" and m > 0 then
            -- 落账：照清单遍历那三句的写法，顺序也一样（先结束时刻、再算编号、再钱）
            ... L.SetExpiry / curKey = L.InstanceKey / L.NoteGoldThisInstance / L.SetRewardRead ...
            askedRewardData[questID] = -1
            GQL.Config.Log(("补读到金额 quest=%s 钱=%s 编号=%s（矩阵没开过也进账了）"))
        else
            askedRewardData[questID] = tries + 1
            if askedRewardData[questID] >= GOLD_READ_MAX_TRIES then
                askedRewardData[questID] = -1
                GQL.Config.Log(("补读 %d 次仍问不出金额 quest=%s（服务器那份数据这一趟没到，不猜）"))
            end
        end
    end
end
```

- `GOLD_READ_MAX_TRIES = 6`：6 次 × 10 秒节流 ≈ 一次开机内每条最多 6 次只读接口，到顶就放手，不无限转。
- 记录不存在时走 `GQL.Ledger.AddQuest(db, questID, now)`（无 addedBy，与 T1-b 同一口径）再落这三笔；
  记录存在时**不新建、不覆盖已有真值**：`NoteGoldThisInstance` 自己带"正数才写"的保护
  （`GoldQuestLedger/Ledger.lua:740`），号级那份保护在 `L.SetRewardRead`。
- 这一趟的接口代价照实：每条待补读的任务每 10 秒 1 次只读金额调用，条数封顶 12，到顶摘表；
  零新增事件监听、零新增定时器、零扫图。

### T2 用例

- 新增 `test_event_capture_asks_the_server_once_for_reward_data`：
  夹具里替身记调用次数。第一次现场问答金额问不出 ⇒ 断言**恰好发了 1 次**预加载请求、
  日志写着"已向服务器要"；再触发两次日志事件（过节流）⇒ 请求次数仍是 1（不许重复要）。
  对照组：`HaveQuestRewardData` 答 true 的那条 ⇒ 一次请求都不发。
- 新增 `test_reward_data_arrival_settles_gold_without_the_matrix`（这一条是本轮的靶心）：
  矩阵关着接一条金币世界任务（金额先答 nil）→ 之后补读第 2 次开始答 10557708
  ⇒ 断言 `goldMoney/goldKey/lastMoney`、号级 `reads`、`observations` 那份都落进去了，
  且 `goldKey` 用的是**这条自己的**结束时刻那一档（先写 `roundEnd` 再算键，顺序错了这条就红）；
  矩阵的行仍由现有准入门判（本轮不碰门槛）。
  反向：补读到顶仍问不出 ⇒ 那几个字段全 nil、表里那条摘成"已了结"、日志写着"不猜"。
- 回归不许动：`test_passive_probe_logs_any_accepted_world_quest` 的夹具让 88 这条**从头到尾问不出金额**，
  那三条"不许写 roundEnd/goldMoney/goldKey/lastMoney"的断言照旧成立。
  若夹具改到金额能答出来，**停下来回报**，不许顺手放宽断言。

## 五、明确不做

- 不回写任何已有的完成记录编号（用户 11-04 第 3 条）。已丢的那两格这一轮不亮回来，下个 CD 自然好。
- 不动准入门、门槛值（500 金默认）、"这一条是哪一轮"的判据本身。
- 不新增事件注册、定时器、轮询、扫图。
- 不许把客户端的 0 当金额收下。

## 六、要游戏内实测（离线测不到）

1. 矩阵全程关着，接一条世界任务并做完 → `/gql log` 应出现：
   `接任务现场问答 …金额=nil（已向服务器要这份数据）`、若干秒后 `补读到金额 quest=… 钱=… 编号=…`、
   `交割现场问答 …金额=<正数>`。若 `补读 6 次仍问不出` = 这条路不通，得回来改设计（不猜数）。
2. 同一条在矩阵里出现没有 —— 出现了才叫"这个要处理好"落地。
3. 想单独验"是不是只有开地图才答得出"：`/gql quest <任务名>` 现在会把金额三条候选逐个问一遍，
   并写明 `HaveQuestRewardData` 与预加载入口在不在（`GoldQuestLedger/Probe.lua:431-435`）。
