-- 亚基入侵（Aqir Invasion）虚拟周常行 —— 12.1.x 版本闸挂法，单独成文件（用户 2026-11-01 定）：
-- "这是 12.1.5 的特殊事件类型，有周奖励和每次奖励（类似斯托玛兰），但是没有任务，
--  最好写一个单独的 lua 文件，标记版本号 ^12.1.5，以后 13.x 就不挂了
-- （除非用户有关键词 13.0 的亚基）"。
--
-- 为什么没有任务（2026-11-01 多轮 probe 定案，见 /gql probe「地图事件 pin」段）：
--   事件 pin（C_AreaPoiInfo）的任务号永远"客户端没给"，任务日志/已完成清单/
--   C_WeeklyRewards 全问不到 ⇒ Sync.RefreshEventPins 的"pin 挂 questID 才收行"那条路
--   对它永远收不进来，只能由本文件虚拟一条。
--
-- 完成凭据（同日双角色对照实测）：pin 的 tooltipWidgetSet 里有一条 widget，
--   C_UIWidgetManager.GetItemDisplayVisualizationInfo 答得出它的奖励文本 ——
--   本周没做的角色是宝箱文案（带"每周仅可获得一次"），做过的角色变成徽章文案
--   （"获得一枚勋章…可换取任意至暗之夜阵营的声望"）。widget 是服务器按玩家下发的，
--   天然按角色；pin 表的 description 是空串、tooltip 正文全部来自这一层。
--
-- 三条用户定的口径（2026-11-01 注意点）：
--   ① 行只给点了关键词的玩家看：没配命中"亚基入侵"的关键词就一行都不注册。
--      regKey 走 kw: 前缀，玩家删关键词时 Sync.UnregisterKeyword 照既有流程退行，
--      本模块不需要自己的注销路；
--   ② 不许拖矩阵：状态查询 = 每轮快照对缓存的 widgetSet 枚举一次（约 6 个 widget
--      各一次 pcall，微秒级）；矩阵渲染只读 views 不触发本模块；pin 证据复用
--      RefreshEventPins 那一趟扫描，本模块不重复扫图；
--   ③ 版本闸：本模块按 12.1.x 实测的形状写，13.x 没实测不自动挂 —— 而行的唯一
--      准入是玩家亲手配的关键词，13.x 默认配置包不会有这条词，等于自动不挂；
--      玩家在 13.x 亲手加了词，那是他点名要的（"除非用户有关键词"），照挂。
--
-- 挂法：
--   · 占位 questID 99000001（真实 questID 到不了这个量级；它绝不喂任务接口，
--     状态由 Api.RegisterVirtualState 挂的本文件探测器答）；
--   · CD = 周 CD：注册行走 Ledger.SetExpiry 的 CD 尾巴口径，与其它注册周常同源；
--   · 证据来源 = Sync.RefreshEventPins 每轮快照扫到的事件 pin（扩展点 EventPinExtras），
--     本文件不重复扫跟踪清单的图。

GQL.AqirInvasion = {}
local M = GQL.AqirInvasion

local FAKE_QUEST_ID = 99000001
local ROW_NAME = "亚基入侵"   -- 行名固定不带区名（pin 名是"亚基入侵：祖阿曼"这种轮换后缀）
local PIN_WORD = "亚基入侵"
-- 判据文案（2026-11-14 双形状对照定案，用户两份现场证据）：没做的号 tooltip 必有宝箱段
-- 「击败亚基入侵的最终首领后可获得：亚基战地行囊…每周仅可获得一次」；做过的号这一段整个消失
-- （声望未满的号改为「获得一枚勋章…」文案，声望已满的号只剩奖励物、无勋章文案）。
-- 所以「每周仅可获得一次」在不在就是 open/done 分界；勋章文案是做过（声望未满）的补充正向字样。
-- 注意「亚基研究背包」是场景奖励/战利品名，做没做都照样列着，与宝箱段（亚基战地行囊）无关，不作判据。
local WEEKLY_MARK = "每周仅可获得一次"
local DONE_MARKS = { "获得一枚勋章" }

-- 上一轮探测结论的缓存：pin 这轮扫不到（事件轮换到没跟踪的图）≠ 状态变了，照答上一结论。
local lastWidgetSet, lastState
local loggedThisSession = false

--- 玩家有没有点名要这个事件：关键词命中 pin 名才算（命中词原样返回，注册进 kw: 前缀）。
local function KeywordFor(pinName)
    local kh = GQL.Config and GQL.Config.KeywordHit
    return (kh and type(pinName) == "string") and kh(pinName) or nil
end

local function CollectStrings(v, acc, depth)
    if type(v) == "string" then
        if v ~= "" then acc[#acc + 1] = v end
    elseif type(v) == "table" and depth < 4 then
        for _, sub in pairs(v) do CollectStrings(sub, acc, depth + 1) end
    end
end

--- widgetSet → "open"（宝箱段还在 = 本周没领）/ "done"（宝箱段已不在 = 做过）/ nil（set 没答话）。
--- 判据（2026-11-14 双形状对照定案，用户两份现场证据）：
---   没做：tooltip 必有宝箱段「击败亚基入侵的最终首领后可获得：亚基战地行囊…每周仅可获得一次」；
---   做过：这一段整个消失——声望未满的号改为「获得一枚勋章…」文案，声望已满的号没有勋章文案、
---   只剩「完成…可获得」+ 奖励物。做过/没做过描述完全不同，分界就是「每周仅可获得一次」在不在。
---   ① 含「每周仅可获得一次」⇒ open；
---   ② 含「获得一枚勋章」⇒ done（做过且声望未满）；
---   ③ 两条都没有、但有奖励 widget（ItemDisplay）答过表 ⇒ done（做过且声望已满：宝箱段没了
---      而奖励物还在列；没做的号必有宝箱段，不会落到这条）；
---   ④ 整个 set 没有 widget 答话（中间态/数据未预加载）⇒ nil，fail-closed 不写凭据，宁显示读不到。
--- 文本在哪（2026-11-14 探针实锤）：描述文案在 type=8 文本 widget 里，必须调
---   GetTextWithStateWidgetVisualizationInfo 才拿得到；GetItemDisplayVisualizationInfo 只对
---   type=27 奖励 widget 答表、且表里只有 itemInfo 没有名字字符串。旧版只问 item 一类，
---   等于永远读不到判据文案——这是"写了判据却从不命中"的根因。widget 号与 widgetSet 号
---   都不硬编码（实测 widgetSet 已轮换过 1981 → 2253）。
local function WidgetChestState(widgetSet)
    widgetSet = tonumber(widgetSet)
    if not widgetSet then return nil end
    local listFn = GQL.Api.Resolve("C_UIWidgetManager.GetAllWidgetsBySetID")
    local itemFn = GQL.Api.Resolve("C_UIWidgetManager.GetItemDisplayVisualizationInfo")
    local textFn = GQL.Api.Resolve("C_UIWidgetManager.GetTextWithStateWidgetVisualizationInfo")
    if not (GQL.Api.Callable(listFn) and GQL.Api.Callable(itemFn) and GQL.Api.Callable(textFn)) then
        return nil
    end
    local okCall, widgets = pcall(listFn, widgetSet)
    if not (okCall and type(widgets) == "table") then return nil end
    local sawWeekly, sawMedal, sawItem = false, false, false
    for i = 1, #widgets do
        local wid = type(widgets[i]) == "table" and tonumber(widgets[i].widgetID) or nil
        if wid then
            local texts = {}
            local okI, rI = pcall(itemFn, wid)
            if okI and type(rI) == "table" then
                sawItem = true
                CollectStrings(rI, texts, 0)
            end
            local okT, rT = pcall(textFn, wid)
            if okT and type(rT) == "table" then
                CollectStrings(rT, texts, 0)
            end
            local joined = table.concat(texts, " ")
            if joined:find(WEEKLY_MARK, 1, true) then sawWeekly = true end
            for k = 1, #DONE_MARKS do
                if joined:find(DONE_MARKS[k], 1, true) then sawMedal = true break end
            end
        end
    end
    -- 全 set 扫完才裁决（不许第一个 widget 定终身）。矛盾形状（周常文案与勋章同现）判不出。
    if sawWeekly and sawMedal then return nil end
    if sawWeekly then return "open" end
    if sawMedal then return "done" end
    if sawItem then return "done" end   -- 做过且声望已满：宝箱段没了、奖励物还在列
    return nil
end

--- Sync.RefreshEventPins 的事件扩展：byName/chosen 是生产那一趟真问到的证据，
--- 本函数只认名字带「亚基入侵」且被玩家关键词点名的 pin，别的 pin 一律不碰。
local function OnEventPins(db, now, byName, chosen)
    local mapID, widgetSet, kw
    for name, cands in pairs(byName or {}) do
        if type(name) == "string" and name:find(PIN_WORD, 1, true) then
            local hit = KeywordFor(name)
            if hit then
                kw = kw or hit
                mapID = mapID or (chosen and chosen[name]) or nil
                widgetSet = widgetSet or tonumber(cands.widgetSet) or nil
            end
        end
    end
    -- 行只给点名要它的玩家（注意点①）：没配关键词，一行都不注册。
    if not kw then return end
    -- 这轮没有亚基 pin（事件轮换到没跟踪的图）：不清不删，照留上一结论。
    if not (mapID or widgetSet) then return end
    if widgetSet then lastWidgetSet = widgetSet end
    local state = widgetSet and WidgetChestState(widgetSet) or nil
    if state then lastState = state end
    if not (GQL.Ledger and GQL.Ledger.EnsureRegistered) then return end
    -- chosen 对这 pin 可能"选不出"（多张图都报它、primary 标记随轮换飘，PickPinMap
    -- 宁缺不猜）—— mapID 传 nil 就留在周常组；关键词在时生产的 evMapID 归属循环
    -- 会把它挂到事件图分组下（与丰饶同一条判据），分组照样跟着事件走。
    local kwPrefix = (GQL.Ledger.KW_PREFIX) or "kw:"
    -- seen = true：走到这里只可能是**这一轮真扫到了带「亚基入侵」的 pin**（上面两道 return
    -- 拦着），那是客户端答过的真事实，EnsureRegistered 据此续 lastSeenAt。散场/轮换到
    -- 没跟踪的图那一轮起没有 entry ⇒ 断供；本行的结束时刻走②档周 CD 尾巴（D7⑤：它问不出
    -- 自己的结束时间），换 CD 没再被 pin 报上来就到期下板，再出现是新 CD 新编号、旧勾不带。
    GQL.Ledger.EnsureRegistered(db, {
        { key = kwPrefix .. kw, questID = FAKE_QUEST_ID, name = ROW_NAME,
          mapID = mapID, state = state or lastState, seen = true },
    }, GQL.Sync.CurrentCharKey(), now)
    if not loggedThisSession and GQL.Config and GQL.Config.Log then
        loggedThisSession = true
        GQL.Config.Log(("亚基入侵（虚拟行 %d）：关键词「%s」点名，图=%s widgetSet=%s 状态=%s")
            :format(FAKE_QUEST_ID, tostring(kw), tostring(mapID or "选不出"),
                tostring(widgetSet), tostring(state or lastState)))
    end
end

GQL.EventPinExtras = GQL.EventPinExtras or {}
GQL.EventPinExtras[#GQL.EventPinExtras + 1] = OnEventPins

-- 快照每轮按 RegisteredIndex 重问状态时会走到这里（Api.QuestState 的虚拟探测器）：
-- widgetSet 是 RefreshEventPins 那趟刚缓存的，不重复扫图；widget 这层每轮重读一次
-- （打完最终首领、宝箱被领走的瞬间它就会变），问不出时照答上一结论。
-- 每次调用 = 一次 GetAllWidgetsBySetID + 每 widget 一次 pcall（µs 级，10 秒一轮），
-- 矩阵渲染只读 views 不走这里（注意点②）。
GQL.Api.RegisterVirtualState(FAKE_QUEST_ID, function()
    -- 开箱战利品已确认本轮完成（aqirLootDoneKey 命中当前实例）：优先回 done，
    -- 盖掉 widget 尚未翻转时的 stale open/nil，避免把快路径的勾反写成没做。
    if aqirLootDoneKey and aqirLootDoneKey == GQL.Ledger.InstanceKey(GQL.Config.DB(),
            FAKE_QUEST_ID, (GQL.Config.DB().quests or {})[FAKE_QUEST_ID], LocalNow()) then
        return "done", nil
    end
    if lastWidgetSet then
        local st = WidgetChestState(lastWidgetSet)
        if st then lastState = st end
    end
    if lastState then return lastState, nil end
    return "unknown", "亚基入侵 pin/widget 还没答过"
end)

-- ===== 击杀/开箱即时判定（2026-11-14 落地）=====
-- 绕开 pin widget 的服务端延迟：widget 文本是服务器延迟推送的，你开箱捡到周常奖励的
-- 那一刻客户端早把奖励发下来了。证据 = 亚基场景刚完成后的那一下 CHAT_MSG_LOOT，
-- 物品名带「亚基」（实测周常宝箱掉「亚基研究背包」）。两段事件驱动、零轮询。
-- 与文件头那套 widget 判定是互补关系、写同一份凭据（db.views[99000001]），不冲突：
-- widget 路径作为兜底/补证保留（装这逻辑前打完的、loot 没捕获到的、跨号重登缓存清空的，
-- 都靠它补）。本段同文件、同版本闸，只服务虚拟行 99000001，不碰任何其它任务。
local AQIR_KEY = "亚基"
local LOOT_WINDOW = 120          -- 场景完成后 120 秒内出现的亚基战利品才算周常奖励
local aqirScenarioDoneAt = 0     -- 最近一次 SCENARIO_COMPLETED 的 GetTime() 时刻
local aqirLootDoneKey = nil      -- 开箱确认完成那一刻的本实例编号；命中当前实例才压过 widget

-- 与 Ledger.LedgerNow 同源的本地时钟（LedgerNow 是 local 没导出）：优先墙上秒、退回 uptime。
local function LocalNow()
    local wall = GQL.Api and GQL.Api.WallClock and GQL.Api.WallClock() or nil
    if type(wall) == "number" then return wall end
    local up = GQL.Api and GQL.Api.Uptime and GQL.Api.Uptime() or nil
    return type(up) == "number" and up or nil
end

-- 当前这一条实例的编号（与快照 SetView 算的是同一把，才对得上 SameInstanceKey）。
local function CurrentInstanceKey()
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[FAKE_QUEST_ID]
    return GQL.Ledger.InstanceKey(db, FAKE_QUEST_ID, rec, LocalNow())
end

-- 从战利品消息里取物品显示名（|h[name]|h），不含链接代码；任一含「亚基」即判为亚基奖励。
local function LootHasAqirName(msg)
    if type(msg) ~= "string" then return false end
    for name in string.gmatch(msg, "%|h([^%|]+)%|h") do
        if name:find(AQIR_KEY, 1, true) then return true end
    end
    return msg:find(AQIR_KEY, 1, true)   -- 兜底：链接解析不到就直接看原文
end

local function MarkAqirDone()
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[FAKE_QUEST_ID]
    if not rec then return end  -- 没点名要这个事件就不强行建行（符合"只给点名玩家"口径）
    aqirLootDoneKey = CurrentInstanceKey()
    GQL.Ledger.SetView(db, GQL.Sync.CurrentCharKey(), FAKE_QUEST_ID, "done", LocalNow())
    if GQL.Matrix and GQL.Matrix.Refresh then pcall(GQL.Matrix.Refresh) end
    GQL.Config.Log("亚基入侵：据开箱战利品即时判定为已完成")
end

do
    local frame = CreateFrame("Frame")
    local ok1 = pcall(frame.RegisterEvent, frame, "SCENARIO_COMPLETED")
    local ok2 = pcall(frame.RegisterEvent, frame, "CHAT_MSG_LOOT")
    if not ok1 then GQL.Config.Log("AQIR 注册 SCENARIO_COMPLETED 失败") end
    if not ok2 then GQL.Config.Log("AQIR 注册 CHAT_MSG_LOOT 失败") end
    frame:SetScript("OnEvent", function(_, event, a1)
        if event == "SCENARIO_COMPLETED" then
            -- 任意场景完成都开窗（不依赖场景名查询，避免 API 不确定性）；真正的闸门是下面 loot 名。
            aqirScenarioDoneAt = GetTime()
        elseif event == "CHAT_MSG_LOOT" then
            if aqirScenarioDoneAt > 0 and (GetTime() - aqirScenarioDoneAt) <= LOOT_WINDOW
                and LootHasAqirName(a1) then
                MarkAqirDone()
            end
        end
    end)
end
