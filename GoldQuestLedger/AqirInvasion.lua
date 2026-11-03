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
local WEEKLY_MARK = "每周仅可获得一次"
-- "本周已领"的**正向**字样（T15，用户 2026-11-02 现场事实）：判据里只许出现真客户端
-- 答过的文本。这一串是本文件头记的 2026-11-01 双角色对照实测里"做过的角色"的徽章文案
-- 前缀（"获得一枚勋章…可换取任意至暗之夜阵营的声望"）；除它以外不许再凭想象加
-- "已领取/已完成/不可再获得"这类没在这台客户端验证过的字样。两串都仍需游戏内复核
-- （/gql probe 的事件 pin 段已逐 widget 摊文本，对答案就是这两串）。
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

--- widgetSet → "open"（本周宝箱还在）/ "done"（读到明确已领字样）/ nil（这轮问不出）。
--- 判据形状（T15，用户 2026-11-02 现场事实推翻了旧口径）：做完之后客户端**不即时**——
---   ① pin 图标先消失；② 下次上线先呈一段既不是"每周仅可获得一次"也不是正常奖励段的
---   中间态，约十来分钟，有的角色要等下次整点更新才看到奖励段变化；③ 做完立即退游戏，
---   别的号在这段时间根本读不到"已完成"。⇒ 中间态是**常态**，"没看到那句周常文案"
---   根本不是已领的证据。旧版"第一个答出表且文本非空的 item-display 不是周常文案就
---   return done"会把中间态写成假绿勾，整删。
--- 新口径：扫完整个 set（不许第一个 widget 定终身），done 只认 DONE_MARKS 里的正向
---   字样；open 只认 WEEKLY_MARK；两条同时命中 = 矛盾形状，答 nil；什么都不命中 = nil。
---   nil 一路不写任何凭据（OnEventPins 里 nil → 留上一结论，从没答出过就交虚拟探测器
---   答 "unknown" → 矩阵显示"读不到"），宁显示读不到也不画勾。
--- 2026-11-01 实测：同一 widgetSet 里只有奖励 widget 对 GetItemDisplayVisualizationInfo
--- 答得出表（type=8 的文本 widget 对它不答），所以逐 widget 试、widget 号与 widgetSet 号
--- 都不硬编码（实测 widgetSet 已轮换过 1981 → 2253）。已见第三种形状（2026-11-01 用户
--- 回传）：某角色 set 里奖励段整个消失（无 ItemDisplay widget 答话）——在新判据下它
--- 就是一条都不命中 ⇒ nil，与 2026-11-02 的中间态同归"读不到"，不写凭据。
local function WidgetChestState(widgetSet)
    widgetSet = tonumber(widgetSet)
    if not widgetSet then return nil end
    local listFn = GQL.Api.Resolve("C_UIWidgetManager.GetAllWidgetsBySetID")
    local itemFn = GQL.Api.Resolve("C_UIWidgetManager.GetItemDisplayVisualizationInfo")
    if not (GQL.Api.Callable(listFn) and GQL.Api.Callable(itemFn)) then return nil end
    local okCall, widgets = pcall(listFn, widgetSet)
    if not (okCall and type(widgets) == "table") then return nil end
    local sawOpen, sawDone = false, false
    for i = 1, #widgets do
        local wid = type(widgets[i]) == "table" and tonumber(widgets[i].widgetID) or nil
        if wid then
            local okI, r = pcall(itemFn, wid)
            if okI and type(r) == "table" then
                local texts = {}
                CollectStrings(r, texts, 0)
                local joined = table.concat(texts, " ")
                if joined:find(WEEKLY_MARK, 1, true) then
                    sawOpen = true
                else
                    for k = 1, #DONE_MARKS do
                        if joined:find(DONE_MARKS[k], 1, true) then sawDone = true break end
                    end
                end
            end
        end
    end
    -- 全 set 扫完才裁决：两条正向字样同轮出现是没见过的矛盾形状，判不出（nil），
    -- 两边都不许抢；只有单边命中才落那一边。空文本的 item-display 不进判据。
    if sawOpen and sawDone then return nil end
    if sawOpen then return "open" end
    if sawDone then return "done" end
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
    if lastWidgetSet then
        local st = WidgetChestState(lastWidgetSet)
        if st then lastState = st end
    end
    if lastState then return lastState, nil end
    return "unknown", "亚基入侵 pin/widget 还没答过"
end)
