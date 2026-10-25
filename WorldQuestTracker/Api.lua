-- WoW API 适配层：插件里唯一读世界任务/奖励/完成状态接口的地方。
-- 12.x 接口形态未经实测：每个候选 pcall，字段名按候选列表试，
-- 取不到就返回原因并把功能降级为"未知"。降级原因集中存 LastDegrade，
-- 由 /wqt dump 与（阶段二）矩阵列头如实显示。

WQT.Api = {}
local Api = WQT.Api

local lastDegrade = "none"

function Api.LastDegrade() return lastDegrade end

local function Degrade(why)
    lastDegrade = why
    if WQT.Config and WQT.Config.Log then WQT.Config.Log("API 降级: " .. why) end
end

--- 地图来源标签的唯一出处是 Config.MAP_SRC（本文件写进 mapSrc/srcOf 的那几串、
--- Ledger 那两张判定表比的都是同一批串，抄第二份就必然分岔）。取不到时回 ""，
--- 不回 nil：nil 会把下面 "前缀 .. 接口名" 的拼接直接炸掉（整条池快照跟着没）。
local function MapSrc(key)
    local M = WQT.Config and WQT.Config.MAP_SRC
    return M and M[key] or ""
end

function Api.Resolve(path)
    local node = _G
    for part in path:gmatch("[^%.]+") do
        if type(node) ~= "table" then return nil end
        node = node[part]
    end
    return node
end

-- 判"可调用"用真值而不是 type(fn) == "function"：游戏里内置函数 type 确实是
-- "function"，但离线测试里 Python 回调被 lupa 包成 "userdata"，用 type 判会把
-- 可用的回调当成缺失（与 Config.RefreshCycle 同款处理）。
function Api.Callable(fn) return fn ~= nil end
local Callable = Api.Callable

local function FirstField(row, names)
    for i = 1, #names do
        local v = row[names[i]]
        if v ~= nil then return v end
    end
    return nil
end

-- 奖励信息优先取池行自带的字段（12.x 的 WorldQuestInfo 可能已经带奖励摘要），
-- 取不到再单独问奖励接口。两条路都没有才返回 nil（"未判定"）。
-- **只认 >0**：本机 WorldQuestTab 的同一处判据也是 `GetQuestLogRewardMoney(id) > 0`，
-- 而奖励数据没预加载时这些字段/接口回的是 0 —— 上一版 `if v then return v end`
-- 把 0 当成读到了，直接不再问接口，12.1.5 反馈③"蛇岛的松露大乱斗是金币任务，
-- 矩阵里却没有"就这么卡在 100g 门槛下（/wqt quest 全程 上次金额=?）。
local function RewardMoney(row, questID)
    local v = tonumber(FirstField(row, { "money", "rewardMoney", "QuestMoneyToGo" }))
    if v and v > 0 then return v end
    return Api.QuestRewardMoney(questID)
end

local function RewardItems(row, questID)
    local v = FirstField(row, { "items", "rewardItems" })
    if type(v) == "table" and #v > 0 then return v end
    return Api.QuestRewardItems(questID)
end

-- 池源分两类：全量源（老版本一次拿全部世界任务）与按图源（要 mapID，12.x 的
-- 世界地图数据提供者 WorldMap_WorldQuestDataProviderMixin 走的就是这条路 —— 用户
-- 实测地图上有彩色环+奖励图标，说明源一定在，只是入口不叫 C_WorldQuests）。
-- 按图源只覆盖当前地图这一片，是【部分池】：GetPool 把 partial=true 放进 meta 返回，
-- 调用方绝不能拿部分池当全量去 diff，否则会把别的地图的任务误判成"全部消失"。
local POOL_SOURCES = {
    { path = "C_WorldQuests.GetWorldQuests" },
    { path = "C_WorldQuests.GetAllWorldQuests" },
    -- 12.1.5 实测上面两个都不存在，但名字是猜的。这几个是历史版本用过的入口，
    -- 一起 pcall 一遍才问得出真名；每个都存在候选报告里。
    { path = "GetWorldQuests" },
    { path = "C_WorldQuestDB.GetWorldQuests" },
    -- 按图源：C_TaskQuest 一组上一轮没测（盲区），GetQuestsOnMap 要 mapID 才问得出形状
    -- （probe 实测不带参会报 Usage）。真正的可用性由 /wqt probe 带真 mapID 验证。
    { path = "C_TaskQuest.GetQuestsForPlayerByMapID", needsMapID = true },
    { path = "C_TaskQuest.GetQuestsOnMap", needsMapID = true },
    { path = "C_QuestLog.GetQuestsOnMap", needsMapID = true },
}

-- 按图源返回的是"地图上的任务"，混着普通任务；不筛会把普通任务灌进账本。
-- QuestUtils_IsQuestWorldQuest 是暴雪自己的判别（probe 在 _G 命中过）。
-- 返回 nil = 无从判别（接口缺失或对这行报错）：此时宁可放弃整个源，
-- 也别把普通任务当世界任务收下。
-- 会话内静态属性缓存：某个 questID 是不是世界任务、叫什么名字、挂在哪张图，
-- 一个会话里不会变，问出来一次就该记住。之前每次快照都按候选名单逐个再问一遍
-- （每个任务 6~7 次接口调用 × 池里几十个任务），矩阵开着做任务时的周期性卡顿
-- 有一半是这份白烧。只缓存问出来的结果（nil/报错不缓存）：
-- 奖励金额、倒计时、完成状态会随进度变，绝不进这三张表。
local wqVerdict, titleCache, mapCache = {}, {}, {}

local function IsWorldQuest(questID)
    local cached = wqVerdict[questID]
    if cached ~= nil then return cached end
    local fn = Api.Resolve("QuestUtils_IsQuestWorldQuest")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn, questID)
    -- 报错与"答 nil"都算判不了：把 nil 折成 false，等于把"这台客户端没说"说成
    -- "客户端说它不是世界任务"，那条行会被顺手归进周常组（判据只认 false）。
    if not okCall or v == nil then return nil end
    local verdict = v == true
    wqVerdict[questID] = verdict
    return verdict
end

--- 探针/取证用：这任务是不是世界任务（nil = 判不了，接口缺失或报错）。
function Api.IsWorldQuest(questID) return IsWorldQuest(tonumber(questID)) end

--- 一张地图的原始行 → 池行。partial=true 时逐个过世界任务判别，
--- 只要有一行判不了就整体报 unclassified（调用方放弃该源，不收半懂的数据）。
--- askedMapID = 这次是拿着哪张图问出来的，只作为"不一致时留痕"用（写进 rec.mapAsked 供
--- 取证），**不再拿来当归属地图**：12.1.5 第九轮实测证明按图询问本来就会跨图列任务
--- （问 2395 回来的行写着 mapID=2393、childDepth=1），"问的是哪张图"于是根本不是证据。
--- 行自己不报地图、补查也问不出时留 nil，交给 Sync.HealMissingMaps 与反向索引去补，
--- 补不出就留在"未知地图"分组 —— 编一张图比显示未知更糟（12.1.5 实测：好几个特别任务
--- 因为这份兜底全被钉进盘卷蛇岛，而它们其中一条明明挂在永歌森林>银月城）。
local function RowsToPool(raw, partial, askedMapID)
    local out, unclassified = {}, false
    for j = 1, #raw do
        local row = raw[j]
        local questID
        if type(row) == "number" then
            questID = row                       -- 老接口直接回 questID 数组
        elseif type(row) == "table" then
            questID = tonumber(FirstField(row, { "questID", "qID", "id" }))
        end
        if questID then
            local keep = true
            if partial then
                local wq = IsWorldQuest(questID)
                if wq == nil then unclassified = true end
                keep = (wq == true)
            end
            if keep then
                local t = type(row) == "table" and row or {}
                -- 地图号的来源要留名（"池行自带"/"补问 <接口>"）：账本里的 2512(盘卷蛇岛)
                -- 到底是行自己报的还是我们兜底套的，上一版三种长得一模一样
                -- （mapSrc 被 DiffPool 无条件清成 nil），"客户端跨图列任务"和
                -- "我们兜底兜错了"这两份嫌疑就永远分不开
                local rowMapID = tonumber(FirstField(t, { "mapID", "zoneID", "areaID" }))
                -- 这三串标签一律从 Config.MAP_SRC 取（见文件头的 MapSrc）：写死在这里
                -- 就是第二处出处，Ledger 那两张判定表会悄悄和写入方分岔
                local mapID, mapSrc = rowMapID, rowMapID and MapSrc("pool") or nil
                if not mapID then
                    local asked, src = Api.QuestMapID(questID)
                    mapID, mapSrc = asked, asked and (MapSrc("healPrefix") .. tostring(src)) or nil
                end
                if not mapID and tonumber(askedMapID) then
                    -- 这条兜底留着是用户实测定的口径（"压根不应该存在没有所属地图的任务"），
                    -- 但它只是"我们拿着这张图去问的"，不是客户端说它在这张图上 ——
                    -- 必须带来源名字，否则下一轮报告里"客户端跨图列任务"和
                    -- "我们兜底兜错了"两份嫌疑还是分不开（12.1.5 盘卷蛇岛那批就这么悬着）
                    mapID, mapSrc = tonumber(askedMapID), MapSrc("asked")
                end
                local added = {
                    questID = questID,
                    mapID = mapID,
                    mapSrc = mapSrc,
                    -- 扫的是哪张图也记一笔：12.1.5 实测问 2395 回来的行自带 mapID=2393，
                    -- 两个号哪个才是"这任务归属的地图"现在还没有依据，先把不一致留下痕，
                    -- /wqt probe 的地图取证段会连着父图链把两条都报出来
                    askedMapID = tonumber(askedMapID),
                    name = FirstField(t, { "name", "title", "questName" })
                        or Api.QuestTitle(questID),
                    money = RewardMoney(t, questID),
                    items = RewardItems(t, questID),
                }
                out[#out + 1] = added
                -- 金额读空多半是奖励数据没到（12.x 要预加载），要一次，下轮快照补上。
                -- 但预加载入口自己先问过 HaveQuestRewardData：它答"数据已经在本地"而金额链
                -- 还是一个字都没答出来 = 客户端明说这一轮不给金币 → 写 0（一个真数），
                -- 不许留 nil。留 nil 会被 DiffPool 当成"读闪"，把上个 CD 读到的金币一路
                -- 延续到本轮（12.1.5 反馈"意外遭遇以前是金币任务，现在不是了还在矩阵里"，
                -- 悬停那个 1064 金就是那么活下来的）。
                if added.money == nil and Api.PreloadRewardData(questID) then
                    added.money = 0
                end
            end
        end
    end
    return out, unclassified
end

--- 抓当前角色的世界任务池。返回 nil 时调用方必须保持账本不变，
--- 绝不能把"读不到"当成"池子为空"（那会把整张账本判成全部消失）。
--- 返回空表则是真数据：世界任务全清完了，账本要走"全部消失"这一转移 ——
--- 但【部分池的空表】只说明当前地图没有，不说明别的地图没有，meta.partial 会标明。
--- 地图来源与人物位置无关（用户实测"人在哪不该影响看到哪些任务"）：
--- 首选 Api.MidnightMapIDs() 按名字匹配出的本资料片地图（学 Tercioo WQT 的
--- 硬编码表思路，但按本地化名动态匹配并记进账本）；匹配不到才退回
--- WQMapList() 全量枚举，再不行才用脚下所在大陆。
local function CollectContinentMaps(mapID)
    local out = {}
    local infoFn = Api.Resolve("C_Map.GetMapInfo")
    local childrenFn = Api.Resolve("C_Map.GetMapChildrenInfo")
    -- 向上走到没有父图的顶层（即大陆），最多 6 层防环
    local chain, cur = { mapID }, mapID
    for _ = 1, 6 do
        if not Callable(infoFn) then break end
        local okC, mi = pcall(infoFn, cur)
        if not (okC and type(mi) == "table") then break end
        local parent = tonumber(mi.parentMapID)
        if not parent or parent == cur then break end
        chain[#chain + 1] = parent
        cur = parent
    end
    local continent = chain[#chain]
    local function addChildren(mid)
        if not Callable(childrenFn) then return end
        local okC, kids = pcall(childrenFn, mid)
        if not (okC and type(kids) == "table") then return end
        for j = 1, #kids do
            local kid = kids[j]
            local id = tonumber(type(kid) == "table" and (kid.mapID or kid.uiMapID) or kid)
            if id and not out[id] then
                out[id] = true
                addChildren(id)
            end
        end
    end
    if continent then addChildren(continent) end
    out[mapID] = true
    local list = {}
    for id in pairs(out) do list[#list + 1] = id end
    return list
end

--- 全量世界任务地图表（位置无关）。学 Tercioo/World-Quest-Tracker：它是硬编码
--- mapTables（手工维护 zone mapID 表，所以人物在哪都能看到全量任务）；我们 12.1.5
--- 没有现成表，改为会话内动态枚举一次 —— 从世界根图沿 GetMapChildrenInfo 递归
--- 收集所有子图，缓存复用（地图树一个会话内不变）。根图拿不到才退回
--- CollectContinentMaps（位置相关，聊胜于无）。上限 800 张防失控。
local wqMapCache
function Api.WQMapList()
    if wqMapCache then return wqMapCache end
    local childrenFn = Api.Resolve("C_Map.GetMapChildrenInfo")
    if not Callable(childrenFn) then return nil end
    local root
    local fbFn = Api.Resolve("C_Map.GetFallbackWorldMapID")
    if Callable(fbFn) then
        local okF, v = pcall(fbFn)
        if okF and tonumber(v) then root = tonumber(v) end
    end
    if not root then
        local cur = Api.CurrentMapID()
        if not cur then return nil end
        local infoFn = Api.Resolve("C_Map.GetMapInfo")
        if Callable(infoFn) then
            for _ = 1, 6 do
                local okI, mi = pcall(infoFn, cur)
                if not (okI and type(mi) == "table") then break end
                local parent = tonumber(mi.parentMapID)
                if not parent or parent == cur then break end
                cur = parent
            end
        end
        root = cur
    end
    local list, seen = {}, {}
    local function walk(mid, depth)
        if depth > 5 or #list >= 800 then return end
        local okC, kids = pcall(childrenFn, mid)
        if not (okC and type(kids) == "table") then return end
        for j = 1, #kids do
            local kid = kids[j]
            local id = tonumber(type(kid) == "table" and (kid.mapID or kid.uiMapID) or kid)
            if id and not seen[id] then
                seen[id] = true
                list[#list + 1] = id
                walk(id, depth + 1)
            end
        end
    end
    seen[root] = true
    walk(root, 0)
    if #list == 0 then return nil end
    wqMapCache = list
    return list
end

--- 奖励数据预加载（学 Tercioo WQT 的 RequestPreloadRewardData + 重试）：
--- 12.x 奖励数据不在本地时 GetQuestLogRewardMoney 读出来是 0 —— 看着像"非金币任务"，
--- 其实是数据没到。读空时向服务器要一次，下一轮快照（约 1 分钟后）就能读到真金额。
--- 返回 true = 客户端说奖励数据**已经在本地**（不用再去要，也就等于它答了"这条不给金币"）；
--- false/nil = 数据没到或本机没这个入口，判不了。调用方（RowsToPool）拿这个答案决定
--- 金额写 0 还是留 nil，所以这一句一次快照里每条任务只许问一次 —— 多问就是白烧接口调用。
function Api.PreloadRewardData(questID)
    if Api.HasRewardData(questID) == true then return true end
    local fn = Api.Resolve("C_TaskQuest.RequestPreloadRewardData")
    if Callable(fn) then pcall(fn, questID) end
    return false
end

--- 至暗之夜（12.x 资料片）区域白名单。用户实测要求：非本资料片的旧图
--- （苏拉玛、瓦尔莎拉、阿苏纳这些 Legion/BfA/DF 图）整组不列。
--- 至暗之夜区域（暴雪官网 + 12.1.5 更新说明）：永歌森林（合并旧永歌森林+幽魂之地）、
--- 祖阿曼（含子区域阿塔阿曼）、哈籁恩达尔、虚影风暴，以及 12.1.5 新增的盘卷蛇岛
--- （"乌拉特克的诅咒"：基希克斯破封团本所在地）；银月城是永歌森林内的主城，
--- 万一有任务挂在它名下也属于本资料片，一并放行。
--- 为什么不按 mapID 白名单：mapID 跨小版本会变（盘卷蛇岛就是 12.1.5 新加的），
--- 而客户端本地化地图名是稳定的（矩阵分组头本来就用 MapName）；按名匹配
--- 还能兜住子区域图。
local MIDNIGHT_ZONE_NAMES = {
    ["永歌森林"] = true,
    ["虚影风暴"] = true,
    ["祖阿曼"] = true,
    ["阿塔阿曼"] = true,
    ["哈籁恩达尔"] = true,
    ["盘卷蛇岛"] = true,
    ["银月城"] = true,
}
-- 每张图的判定缓存：一次登录只问一次 C_Map
local midnightMapCache = {}

--- 内置"周常家族"表：这几条不是世界任务，世界任务池和任务日志两条来源都问不到它们
--- （12.1.5 用户实测：配了关键词"丰饶"，丰饶供品哪怕有角色做完了也不进矩阵）。
--- 只存数字 —— 12.1.5 实测 C_TaskQuest.GetQuestInfoByQuestID 拿着本角色从没见过的
--- 老 questID（42859）照样回国服中文标题，所以名字与地图一律由客户端答，绝不写死译名。
--- ids 是一串而不是一个：轮换/事件型任务每周换 questID（参照 SavedInstances 的
--- type="any"，Aiding the Accord 一条挂 18 个 ID），新周期发现的新 ID 并进同一族。
--- match = 这一族的名字用词：池里/图上扫到的同族子任务（辉耀丰饶、下周换到别图的那条）
--- 靠它认亲 —— 玩家不可能知道 ID，族身份只能落在名字上（Ledger.FamilyKey）。
--- 族词会撞名的族（加固符文石 vs 特别任务：加固符文石）宁可不给 match，只按 ID 认。
--- 没把握的 ID / 词一律不写：虚空侵袭还没有可靠来源，猜一个数字比不写更糟。
Api.WEEKLY_PRESETS = {
    { key = "abundant-offerings", ids = { 89507 }, match = "丰饶" },   -- 丰饶供品
    -- 加固符文石（12.1.5 实测反馈⑦"所有角色都做完了，矩阵里没有"）。四条 ID 是同一个
    -- 周常的四个骑士团版本，口径照 SavedInstances 的 type="any"（Progress.lua:1673-1687）
    -- —— 任一即算本周做过。本轮 probe 实测：90573 名=加固符文石：魔导师、剩余=118.1时、
    -- 做过=done；90574-90576 名问不出（IsQuestFlaggedCompleted 对客户端不认识的 ID
    -- 恒回 false，那是"问不出"不是"没做"）。所以占行的是 ids[1] 这条问得出名字的，
    -- 另外三条只参与"全族取最强状态"。
    -- **故意不给 match**：这一族只按声明的四条 ID 认亲。写了"加固符文石"这个族词，
    -- 名字里带它的另一族周常（账本里那条"特别任务：加固符文石"）会被拖进族待遇 ——
    -- 非锚点不占行，整行凭空消失（就是上一轮"符文石、特别任务被拖进族待遇"那个病因，
    -- 用例 pool_countdown_does_not_kill_a_registered_weekly 把它钉住了）。
    { key = "fortify-the-runestones", ids = { 90573, 90574, 90575, 90576 } },
}

--- 玩家口述的地图归属（第十七轮原话："我知道每个任务属于哪个地图"）。
--- 为什么需要这张表：这批任务在这台客户端**问不出**地图 —— 换轮换出板的世界任务，
--- 板面三条来源只认当下还在板上的；压根不上板的特别任务，逐条问的两条 MISSING 或答 nil。
--- 而上一版清图把 SavedVariables 里唯一的旧答案也清掉了，接口本周没有出路。
--- **按名字配、地图也按名字配**：玩家读得到的就是矩阵里那两列文字，questID/地图号是我们
--- 内部的东西，不许反过来问玩家要（硬约束）。地图名只认**跟踪清单**里的（=矩阵里出现过的
--- 组头），配不上就不动，日志点名说清缺哪张图。
--- 只在"这条的地图还没落实"时生效（nil / 旧记录未标来源 / 存疑），真凭据（池行自带、
--- 清单反查…）一律压不过；表改了下一轮就重配（这张表是"玩家指定"这一类的唯一编辑入口）。
Api.MAP_MEMORY = {
    { name = "幽魂角斗士", map = "盘卷蛇岛" },          -- 96625，用户第 16 轮反馈
    { name = "太阳的支持", map = "永歌森林" },
    { name = "普雷达萨斯", map = "虚影风暴" },
    { questID = 90573, map = "永歌森林" },              -- 加固符文石：魔导师（矩阵里名字被截断，按 ID 配稳）
    -- 以下 6 条 = 用户 2026-10-24 口述（第十七轮那份"未知地图"清单）。
    -- **要的是账本里的完整标题，不是屏幕上那几个字**：照矩阵截断的样子抄"特别任务：怨灵"，
    -- 真标题是「特别任务：怨灵之怒」→ 配不上（靠那句"账本里没有叫…"的日志才查出来）。
    { name = "漫天烈火", map = "盘卷蛇岛" },
    -- 用户原话"永歌森林>银月城"。先按下面那张（更准，会自己成一格组头）；
    -- 若日志报「银月城」不在跟踪清单，把 map 改成 "永歌森林" 即可（这张表就是唯一编辑入口）。
    { name = "追踪萨拉塔斯", map = "银月城" },          -- 98172
    { name = "斯托玛兰突袭战", map = "虚影风暴" },
    { name = "特别任务：击退圣光", map = "哈籁恩达尔" },
    { name = "特别任务：神盾密探", map = "虚影风暴" },
    { name = "特别任务：怨灵之怒", map = "盘卷蛇岛" },
}

--- 这张图是不是本资料片区域。
--- true=是；false=确定不是（渲染层要滤掉整组）；
--- nil=判不了（地图名取不到）—— 调用方按"不滤"处理，宁多勿漏。
function Api.IsMidnightMap(mapID)
    mapID = tonumber(mapID)
    if not mapID or mapID <= 0 then return nil end
    local cached = midnightMapCache[mapID]
    if cached ~= nil then return cached end
    local name = Api.MapName(mapID)
    -- 注意别写成 `name and (X and true or false) or nil`：X 为假时整式是
    -- false or nil = nil，"确定不是"会退化成"判不了"，旧资料片图就滤不掉了
    local verdict
    if not name then
        verdict = nil
    elseif MIDNIGHT_ZONE_NAMES[name] then
        verdict = true
    else
        verdict = false
    end
    midnightMapCache[mapID] = verdict
    return verdict
end

--- 本资料片地图 ID 清单（按名字匹配 + 记住）。用户实测要求：只查这几个地图，
--- 不用每次快照把几百张图全问一遍（那是卡顿源）；下个版本地图变了，
--- 改上面的名字清单重新匹配即可，匹配到的 ID 写进账本记住，下次登录先用。
--- 记住的 ID 逐个验名字：对不上说明地图改版/ID 漂移，自动重新全图扫描。
--- 附带把每张命中图的直接子图并进来（阿塔阿曼这类子区域任务挂在自己图下）。
--- 一张都没匹配到 → nil，调用方退回全图枚举（宁多勿漏，绝不返回空池）。
function Api.MidnightMapIDs()
    -- 存档只由 Config.DB() 取：直读全局等于绕过它的判空与 Init 顺序约束
    -- （本函数是运行时调用，那时 Config 早已 Init；取不到就照旧走"没记住的 ID"分支）
    local db = WQT.Config and WQT.Config.DB()
    local remembered = db and db.midnightMapIds
    if type(remembered) == "table" and #remembered > 0 then
        local ok = {}
        for i = 1, #remembered do
            local id = tonumber(remembered[i])
            if id and Api.IsMidnightMap(id) == true then ok[#ok + 1] = id end
        end
        if #ok > 0 then return ok end
    end
    local all = Api.WQMapList()
    if not all then return nil end
    local childrenFn = Api.Resolve("C_Map.GetMapChildrenInfo")
    local found, seen = {}, {}
    local function add(id)
        if id and not seen[id] then
            seen[id] = true
            found[#found + 1] = id
        end
    end
    for i = 1, #all do
        local id = all[i]
        if Api.IsMidnightMap(id) == true then
            add(id)
            if Callable(childrenFn) then
                local okC, kids = pcall(childrenFn, id)
                if okC and type(kids) == "table" then
                    for j = 1, #kids do
                        local kid = kids[j]
                        add(tonumber(type(kid) == "table" and (kid.mapID or kid.uiMapID) or kid))
                    end
                end
            end
        end
    end
    if #found == 0 then return nil end
    if db then db.midnightMapIds = found end
    return found
end

--- 任务奖励金币（copper）。候选按"哪个接口答得出正数"排，第一个答出来的就用。
--- · GetQuestLogRewardMoney：12.1.5 实测存在（问 92120 回 0）。本机装的 WorldQuestTab
---   读世界任务金币用的正是它（`Dataprovider.lua:325` 的 `> 0` 判据），所以它一定在名单里；
---   但它对**没预加载奖励数据**的任务回 0，0 不等于"这条不给金币"（见下面 HaveQuestRewardData）。
--- · 后两条是按接口命名规律猜的，**这台客户端认不认还没实测**，所以只当候选：
---   答不出就自动跳过（pcall + tonumber），/wqt quest 的"金额是谁答的"段会逐条报
---   AVAILABLE/MISSING，等回传再决定留哪条。
local MONEY_SOURCES = { "GetQuestLogRewardMoney",
                        "C_QuestRewards.GetCachedQuestMoney",
                        "C_WorldQuestRewards.GetWorldQuestRewards" }

--- 给取证用：/wqt quest 不许自己抄一份名单，抄了就会和生产链悄悄不一致
function Api.MoneySources()
    local out = {}
    for i = 1, #MONEY_SOURCES do out[#out + 1] = { path = MONEY_SOURCES[i] } end
    return out
end

--- 奖励数据到底在不在本地。这是"0 是真不给金币"还是"数据还没到"的唯一分岔，
--- 上一版没有这一列，松露大乱斗那种"在池里却永远没金额"根本无从判起。
--- 返回 nil = 这客户端没这个入口（判不了），true/false = 它的答案。
function Api.HasRewardData(questID)
    local fn = Api.Resolve("HaveQuestRewardData")
    if not Callable(fn) then return nil end
    local okCall, has = pcall(fn, tonumber(questID) or questID)
    if not okCall or type(has) ~= "boolean" then return nil end
    return has
end

--- 返回 nil = 这一条金额问不出（非金币任务、或奖励数据还没到）。
--- 矩阵照常显示、只是进"未判定"组，绝不把 nil 当 0 参与门槛比较。
function Api.QuestRewardMoney(questID)
    questID = tonumber(questID)
    if not questID then return nil end
    for i = 1, #MONEY_SOURCES do
        local fn = Api.Resolve(MONEY_SOURCES[i])
        if Callable(fn) then
            local res = { pcall(fn, questID) }
            if res[1] then
                -- 返回值逐个看：GetQuestLogRewardMoney 的第 1 个就是 copper，
                -- 有的入口把它排在后面或包在表里，只取第 1 个会漏
                for v = 2, #res do
                    local n = tonumber(res[v])
                    if n and n > 0 then return n end
                end
                local payload = res[2]
                if type(payload) == "table" then
                    local n = tonumber(payload.money or payload.rewardMoney
                        or payload.moneyReward)
                    if n and n > 0 then return n end
                end
            end
        end
    end
    return nil
end

--- 日志里这一行的分区地图号。特别任务/战役任务不上世界任务板，板面那三条来源
--- （池行 / 跟踪清单 / 清单反查）对它全答不出，逐条问的两条入口在本机一条 MISSING
--- 一条答 nil（12.1.5 实测 96625、82157）—— 已接任务的归属图只能问日志本身。
--- 这个入口在 12.x 存不存在**还没实测**：缺失、报错、答 0 一律回 nil，
--- 绝不拿玩家脚下那张图顶（GetMapForQuestPOIs 就是栽在这上面）。
function Api.QuestLogZoneMapID(logIndex)
    logIndex = tonumber(logIndex)
    if not logIndex then return nil end
    local fn = Api.Resolve("C_QuestLog.GetQuestLogZoneMapID")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn, logIndex)
    if not okCall then return nil end
    v = tonumber(v)
    if v and v > 0 and math.floor(v) == v then return v end
    return nil
end

--- 当前角色任务日志里已接的任务：{ {questID=, name=, mapID=}, ... }。
--- 关键词任务的第二条来源：世界任务池是按图问的，城市日常/战役任务根本不在池里
--- （12.1.5 实测反馈："追踪萨拉塔斯"就在任务栏第一个，账本命中却是 0）。
--- 只用 warcraft.wiki.gg 上现役的 C_QuestLog.GetInfo(questLogIndex)（返回带
--- title/questID/isHeader 的表）；GetQuestLogTitle 9.0.1 起已废弃、返回值位置还跨
--- 版本变过，拿位置猜 questID 会把"任务等级"当成任务号记进账本，不接。
--- 条数上限 200：日志正常只有几十条，触顶说明接口对越界索引不报错，得有个刹车。
--- 返回 nil = 一条都没问到（接口缺失或首条就报错，"日志空"和"问不出"分不清时按后者）；
--- 空表 = 问到了，只是全是要略过的分组头。
function Api.AcceptedQuests()
    local fn = Api.Resolve("C_QuestLog.GetInfo")
    if not Callable(fn) then
        Degrade("任务日志入口 C_QuestLog.GetInfo 不可用")
        return nil
    end
    local out, asked = {}, false
    for i = 1, 200 do
        local okCall, info = pcall(fn, i)
        if not okCall or type(info) ~= "table" then break end
        asked = true
        -- isHidden 必须滤：12.1.5 实测 82157「特别任务：巨兽崛起」是一条
        -- isHidden=true、isOnMap=false 的内部行，玩家本周根本没有它，
        -- 却因为命中关键词被从日志收进矩阵、每轮按周重置续命。上一版只滤分组头。
        if not info.isHeader and not info.isHidden then
            local qid = tonumber(info.questID)
            local name = info.title
            if qid and qid > 0 and type(name) == "string" and name ~= "" then
                out[#out + 1] = {
                    questID = qid, name = name,
                    mapID = Api.QuestLogZoneMapID(tonumber(info.questLogIndex) or i),
                }
            end
        end
    end
    if not asked then
        Degrade("C_QuestLog.GetInfo 首条就问不出（索引从 1 起？）")
        return nil
    end
    return out
end

--- 取证用：任务日志原始行的**未归一化**字段（前 limit 条非分组头）。
--- AcceptedQuests 只挑 questID/title 两个字段，"日志行里到底还有没有可用的地图字段"
--- 这件事只有原样摊开才答得出：12.1.5 实测反馈"追踪萨拉塔斯并不是蛇岛的任务……
--- 它应该是银月城的，或者永歌森林的"，而生产链那两条入口对它全答不出，
--- 下一份地图凭据只能从这批字段里找（没找到之前一律落"未知地图"，不猜）。
--- 返回 nil = 入口问不出（与"日志里真没任务"分开）；空表 = 问到了，日志确实是空的。
function Api.AcceptedQuestRawRows(limit)
    local fn = Api.Resolve("C_QuestLog.GetInfo")
    if not Callable(fn) then return nil end
    local cap = tonumber(limit) or 6
    local out = {}
    for i = 1, 200 do
        local okCall, info = pcall(fn, i)
        if not okCall or type(info) ~= "table" then break end
        if not info.isHeader then
            out[#out + 1] = info
            if #out >= cap then break end
        end
    end
    return out
end

--- 世界任务剩余时间（秒）。探针枚举 C_TaskQuest 成员发现 GetQuestTimeLeftSeconds /
--- GetQuestTimeLeftMinutes（"GetQuestTimeLeft" 反而是 MISSING 的）；地图 pin 倒计时同源。
--- Minutes 源给的是分钟，换算成秒，和 Seconds 源统一口径。
--- **只准登记"收 questID 的入口"**：上一版名单里混着全局 GetQuestLogTimeLeft，
--- 它压根没有 questID 参数，答的是"任务日志里当前选中那条限时任务还剩多久"，
--- 我们却把它的返回值写成 db.quests[qid].expiresAt 并清了 expiresGuessed ——
--- 于是矩阵给一条周常涂红/绿、悬停报"剩余 X 分"，那个 X 跟这条任务毫无关系
--- （12.1.5 用户实测："追踪萨拉塔斯……马上就要结束了"，可它是周常）。
--- 判据就一条：入口必须进 ARG_HINTS 且参数名是 questID，否则取证也取证不出归属。
local TIMELEFT_SOURCES = {
    "C_TaskQuest.GetQuestTimeLeftSeconds",
    "C_TaskQuest.GetQuestTimeLeftMinutes",
}

--- 取证用的同一份名单（连"这个入口吃什么参数"一起给）：/wqt probe 与 /wqt quest
--- 必须问生产代码问过的同一批入口，报告才谈得上"证据"。
function Api.TimeLeftSources()
    local out = {}
    for i = 1, #TIMELEFT_SOURCES do
        local path = TIMELEFT_SOURCES[i]
        out[i] = {
            path = path,
            arg = Api.ArgHint(path),
            scale = path:match("Minutes$") and 60 or 1,
        }
    end
    return out
end

function Api.QuestTimeLeft(questID)
    questID = tonumber(questID)
    if not questID then return nil end
    for i = 1, #TIMELEFT_SOURCES do
        local path = TIMELEFT_SOURCES[i]
        local fn = Api.Resolve(path)
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            if okCall then
                local n = tonumber(v)
                if n and n > 0 then
                    return n * (path:match("Minutes$") and 60 or 1)
                end
            end
        end
    end
    return nil
end

--- 第三条路必须分清：接口返回了 N 条却一条 questID 都认不出来，那是形状不符（nil），
--- 不是空池 —— 当成空池会把所有角色的轮次一起毁掉。
--- 第三个返回值 meta = { partial, source, maps=询问的地图数 }，只有成功时才有。
function Api.GetPool()
    local emptyFrom, shapeFrom
    for i = 1, #POOL_SOURCES do
        local src = POOL_SOURCES[i]
        local fn = Api.Resolve(src.path)
        if Callable(fn) then
            local raw, okCall, mapIDs
            if src.needsMapID then
                -- 只查"默认(本资料片) + 用户在设置里额外关注的"地图：省掉几百张图的全图扫描；
                -- 匹配不到才退回全量枚举，宁多勿漏
                mapIDs = (WQT.Config and WQT.Config.TrackedMapIDs
                    and WQT.Config.TrackedMapIDs()) or Api.MidnightMapIDs() or Api.WQMapList()
                if not mapIDs then
                    local mapID = Api.CurrentMapID()
                    if not mapID then
                        Degrade(src.path .. " 跳过：拿不到当前 mapID")
                    else
                        -- 整片大陆各区域都问：脚下小图只挂本区域任务，同大陆其它区任务挂在各自区域图上
                        mapIDs = CollectContinentMaps(mapID)
                    end
                end
                if mapIDs then
                    local merged, seen, anyTable, gaveUp = {}, {}, false, false
                    for k = 1, #mapIDs do
                        local okMap, rows = pcall(fn, mapIDs[k])
                        if okMap and type(rows) == "table" then
                            anyTable = true
                            local out, unclassified = RowsToPool(rows, true, mapIDs[k])
                            if unclassified then
                                merged, anyTable, gaveUp = nil, false, true
                                Degrade(src.path .. " 无法判别世界任务（判别接口缺失/报错），放弃该源")
                                break
                            end
                            for n = 1, #out do
                                local qid = out[n].questID
                                if not seen[qid] then
                                    seen[qid] = true
                                    merged = merged or {}
                                    merged[#merged + 1] = out[n]
                                end
                            end
                        end
                    end
                    if merged ~= nil and anyTable then
                        -- 部分池即使 0 条也是真数据（这张图真没有），如实返回让调用方按部分池处理
                        return merged, nil, { partial = true, source = src.path,
                                              maps = #mapIDs }
                    elseif gaveUp then
                        -- 降级原因已写明"无法判别"，不许被下面的笼统原因覆盖
                    elseif anyTable == false then
                        Degrade(src.path .. " 调用失败（mapID=" .. table.concat(mapIDs, ",") .. "）")
                    end
                end
            else
                local okRaw
                okRaw, raw = pcall(fn)
                okCall = okRaw
            end
            if okCall and type(raw) == "table" then
                local out = RowsToPool(raw, false)
                if #out > 0 then
                    return out, nil, { partial = false, source = src.path }
                end
                if #raw == 0 then
                    -- 空结果继续试下一个候选：某个接口存在但恒返回空，不该遮住另一个的真数据。
                    emptyFrom = src.path
                else
                    shapeFrom = src.path .. " 返回 " .. #raw .. " 条但认不出 questID（形状不符）"
                    Degrade(shapeFrom)
                end
            elseif okCall == false and raw ~= nil and src.needsMapID ~= true then
                Degrade(src.path .. " 调用失败: " .. tostring(raw))
            elseif okCall == false and src.needsMapID == true and mapIDs then
                Degrade(src.path .. " 调用失败: " .. tostring(raw))
            end
        end
    end
    if emptyFrom then
        Degrade(emptyFrom .. " 返回空表")
        return {}, nil, { partial = false, source = emptyFrom }
    end
    if shapeFrom then return nil, shapeFrom, nil end
    return nil, "none of " .. table.concat((function()
        local t = {}
        for i = 1, #POOL_SOURCES do t[i] = POOL_SOURCES[i].path end
        return t
    end)(), ", ") .. " usable", nil
end

--- 只问一张图的池（按图补问用；关键词补扫走的是 MapQuestRows，不是这个）。和 GetPool 走同一批按图候选、同一个
--- RowsToPool 归一化，所以补扫认得的 questID/名字/金额口径与普通快照完全一致。
--- 返回 nil = 这张图问不出数据（接口缺失或报错），调用方跳过它。
function Api.PoolForMapID(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    for i = 1, #POOL_SOURCES do
        local src = POOL_SOURCES[i]
        if src.needsMapID then
            local fn = Api.Resolve(src.path)
            if Callable(fn) then
                local okCall, rows = pcall(fn, mapID)
                if okCall and type(rows) == "table" then
                    local out, unclassified = RowsToPool(rows, true, mapID)
                    if unclassified then return nil end
                    return out
                end
            end
        end
    end
    return nil
end

-- 奖励物品。C_QuestLog.GetQuestRewardItems 等接口缺失时返回 nil。
function Api.QuestRewardItems(questID)
    local fn = Api.Resolve("C_QuestLog.GetQuestRewardItems")
    if not Callable(fn) then return nil end
    local okCall, items = pcall(fn, questID)
    if not okCall or type(items) ~= "table" then return nil end
    local out = {}
    for i = 1, #items do
        local it = items[i]
        local itemID = tonumber(type(it) == "table" and (it.itemID or it.itemid) or it)
        if itemID then out[#out + 1] = itemID end
    end
    return out
end

-- 实测 C_QuestLog.IsQuestCompleted / IsQuestCompleted 在 12.1.5 都不存在；
-- IsQuestFlaggedCompleted 是历史版本里问"这个 CD 做过没"的入口，一起问一遍。
local DONE_SOURCES = { "C_QuestLog.IsQuestCompleted", "IsQuestCompleted",
                       "C_QuestLog.IsQuestFlaggedCompleted" }
local PROGRESS_SOURCES = { "C_QuestLog.IsQuestStarted", "C_QuestLog.IsOnQuest", "QuestLog_IsOnQuest" }

-- 返回 (布尔值或nil, 该接口是否真的可用)
local function TryState(paths, questID)
    for i = 1, #paths do
        local fn = Api.Resolve(paths[i])
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            if okCall then return v == true, true end
        end
    end
    return nil, false
end

--- 只回答"当前角色"的状态。其它角色的状态来自账本，绝不由这里推断。
function Api.QuestState(questID)
    local done, okDone = TryState(DONE_SOURCES, questID)
    local progress, okProgress = TryState(PROGRESS_SOURCES, questID)
    if not okDone and not okProgress then
        Degrade("完成/接取状态接口均不可用")
        return "unknown", "state api unavailable"
    end
    if done then return "done", nil end
    if okProgress and progress then return "progress", nil end
    if not okDone then
        Degrade("完成状态接口不可用")
        return "unknown", "completion api unavailable"
    end
    return "open", nil
end

--- 取证用：状态判据的同一份名单（连"它是哪一档"一起给）。/wqt probe 要报出
--- "这一格是谁答的"，只能拿生产代码真问的那批入口按同一条顺序再问一遍 ——
--- 另列一份名单就等于两套口径，报告里的证据下次改版就会变成假证据。
function Api.StateSources()
    local out = {}
    for i = 1, #DONE_SOURCES do out[#out + 1] = { path = DONE_SOURCES[i], kind = "done" } end
    for i = 1, #PROGRESS_SOURCES do out[#out + 1] = { path = PROGRESS_SOURCES[i], kind = "progress" } end
    return out
end

-- 地图名。矩阵按地图分组时用它把 mapID 变成人看得懂的字。
-- 12.x 的入口名与返回字段都没实测过：候选逐个 pcall，字段按候选列表试，
-- 拿不到就返回 nil 让调用方回退"地图 <id>"—— 编一个地图名比显示地图号更糟。
local MAP_NAME_SOURCES = { "C_Map.GetMapInfo", "C_Map.GetAreaInfo", "GetMapNameByID" }

-- 任务名与所在地图。12.1.5 实测 C_TaskQuest.GetQuestsOnMap 只回一串纯 questID
-- （池里 6 条全是数字行），名字和地图必须拿着 questID 单独问。
-- 入口名跨版本不一：字符串结果直接当名字，表结果按候选字段挑；都取不到返回 nil，
-- 矩阵回退显示 questID —— 编一个名字比显示编号更糟。
local TITLE_SOURCES = {
    "C_QuestLog.GetTitleForQuestID",
    "C_TaskQuest.GetQuestInfoByQuestID",
    "C_QuestLog.GetQuestInfo",
}
local TITLE_FIELDS = { "name", "title", "questName" }
-- 每条写死"地图号在哪个位置"（ret = 第几个返回值，field = 表结果的哪个字段）。
-- 之前是"前两个返回值里挑第一个数字"，那是错的：GetQuestLocation 回的是 x, y, mapID，
-- 第一个数字是横坐标；GetQuestInfo 的数字返回值是任务等级/类型 —— 拿它们当地图号，
-- 12.1.5 第五轮实测（用户回传 probe）之后只剩这两条，摘掉的三条各带一份证据，
-- 别按"接口名像"再加回来：
-- · C_QuestLog.GetMapForQuestPOIs：对 91805/91808/91811（三条都在祖阿曼的板清单上）
--   和 89507 一律回 **2444 = 玩家脚下这张图**。它排在链尾，只在别的接口都答不出时才被问到，
--   于是每次答的都是"你站哪儿"：12.1.5 实测反馈"丰饶出现在了屠戮者高地（当前我的角色
--   就在屠戮者高地，这个不对的）"、"追踪萨拉塔斯第一次获取到就放在了蛇岛"全是它。
-- · C_TaskQuest.GetQuestInfoByQuestID 第 2 返回值：91805 给 2696、42859 给 1859，
--   而这两个号的地图名问出来是 nil（probe 原文"2696(number) 地图名=nil"）—— 不是地图号。
-- · C_TaskQuest.GetQuestLocation：真 Usage 是 (questID, uiMapID) 两参、只回 x,y，
--   我们单参调用必抛错，第 3 个返回值从来不存在（probe 每行都打了那条 ERROR）。
-- 三条连同两条还没实测的新候选都留在 Api.MapProbeSources 里，/wqt probe 与 /wqt quest
-- 继续拿着真 questID 逐个问给用户看；生产链（QuestMapID）只读上面这份，绝不读那份。
local QUEST_MAP_SOURCES = {
    { path = "C_QuestLog.GetQuestInfo", field = { "mapID", "uiMapID", "zoneID" } },
    -- 12.1.5 实测唯一对得上板清单的一条：91805/91808/91811 全答 2437，
    -- 与它们所在那张图一致（89507 它答 nil，宁缺不编）。
    { path = "C_TaskQuest.GetQuestZoneID", ret = 1 },
}

function Api.QuestTitle(questID)
    questID = tonumber(questID)
    if not questID then return nil end
    local cached = titleCache[questID]
    if cached ~= nil then return cached end
    for i = 1, #TITLE_SOURCES do
        local fn = Api.Resolve(TITLE_SOURCES[i])
        if Callable(fn) then
            local okCall, v = pcall(fn, questID)
            if okCall then
                if type(v) == "string" and v ~= "" then
                    titleCache[questID] = v
                    return v
                end
                if type(v) == "table" then
                    local name = FirstField(v, TITLE_FIELDS)
                    if type(name) == "string" and name ~= "" then
                        titleCache[questID] = name
                        return name
                    end
                end
            end
        end
    end
    return nil
end

--- 生产代码 QuestTitle 真正会逐个问的候选名单。取证段必须问同一批入口：
--- 报告里写"没有"而代码里试过（或反之），证据和实现就分成两套了（同 MapSources 的规矩）。
function Api.TitleSources()
    local out = {}
    for i = 1, #TITLE_SOURCES do out[i] = TITLE_SOURCES[i] end
    return out
end

--- 母任务的"任务追踪"清单 = 这条任务自己摊开的目标行。
--- 用户 2026-10-24 实测：丰饶供品(89507) 的任务追踪里能直接看到子任务的内容（辉耀丰饶），
--- 而反过来 GetParentQuestForChild(89507) 回 nil —— 子任务反查不到母任务，
--- 这个方向是唯一能把"母任务 → 这一族的子任务 ID"问出来的入口。
--- 只认行自己带的数字 questID（12.x 字段名没实测过，r.questID / r.associatedQuestID
--- 两条都试；绝不编 ID，也不拿文本当 ID）。行回填母任务自己不算子任务。
--- 只有**当前角色接了**这条任务才问得出（任务日志系接口）→ 问不出回 nil，
--- 调用方必须能分清"没有目标行"和"这条角色没接、问不出"。
function Api.QuestObjectives(questID)
    questID = tonumber(questID)
    if not questID then return nil end
    local fn = Api.Resolve("C_QuestLog.GetQuestObjectives")
    if not Callable(fn) then return nil end
    local okCall, rows = pcall(fn, questID)
    if not okCall or type(rows) ~= "table" then return nil end
    local out = {}
    for i = 1, #rows do
        local r = rows[i]
        if type(r) == "table" then
            local child = tonumber(r.questID) or tonumber(r.associatedQuestID)
            out[#out + 1] = {
                questID = (child and child > 0 and child ~= questID) and child or nil,
                text = type(r.text) == "string" and r.text or nil,
            }
        end
    end
    if #out == 0 then return nil end
    return out
end

--- 生产代码 QuestMapID 真正会逐个问的候选名单（含"地图号在第几个返回值"），
--- 原样交给 /wqt probe。取证段必须问同一批入口、并按同一位置口径报：
--- 报告里写"没有"而代码里其实试过（或取的位不一样），等于把证据和实现分成两套。
function Api.MapSources()
    local out = {}
    for i = 1, #QUEST_MAP_SOURCES do
        local src = QUEST_MAP_SOURCES[i]
        out[i] = { path = src.path, ret = src.ret, field = src.field }
    end
    return out
end

--- 取证用的完整名单 = 生产链 + 已经摘掉/还没接进去的候选，每条带一句"为什么不在链里"。
--- 为什么要把停用的也问：摘掉三条后 89507、追踪萨拉塔斯 在本机就成了"没有地图来源"，
--- 下一份报告要看得出的是"这三条本来就答错"还是"链短了问不出"，也要看新候选
--- （GetNextWaypoint / GetQuestTagInfo）答不答得出 —— 答得出才谈接线。
--- 生产代码（QuestMapID）绝不读这份，只读 QUEST_MAP_SOURCES；两条线分开写死在这里，
--- 免得取证名单里的撒谎接口哪天被顺手接回生产。
local MAP_PROBE_EXTRA = {
    { path = "C_QuestLog.GetMapForQuestPOIs",
      note = "已停用：实测回的是玩家脚下这张图（站 2444 时 89507 也答 2444）" },
    { path = "C_TaskQuest.GetQuestInfoByQuestID", ret = 2,
      note = "已停用：第 2 返回值不是地图号（2696/1859 的地图名问出来是 nil）" },
    { path = "C_TaskQuest.GetQuestLocation", ret = 3,
      note = "已停用：真 Usage 是 (questID, uiMapID) 两参，单参必抛错、只回 x,y" },
    { path = "C_QuestLog.GetNextWaypoint",
      note = "未接线的新候选：日志口径的下一跳，答得出才接" },
    { path = "C_QuestLog.GetQuestTagInfo",
      note = "未接线的新候选：任务标签表里可能有地图字段" },
}

function Api.MapProbeSources()
    local out = Api.MapSources()
    for i = 1, #MAP_PROBE_EXTRA do
        local src = MAP_PROBE_EXTRA[i]
        out[#out + 1] = { path = src.path, ret = src.ret, field = src.field, note = src.note }
    end
    return out
end

--- 已经从生产链里摘掉的入口名。账本里那些"补问 C_QuestLog.GetMapForQuestPOIs"写进来的
--- 地图号要按这份名单一起清掉：接口不问了，错地图却留在 SavedVariables 里继续归组，
--- 用户下次开矩阵看到的还是"丰饶在屠戮者高地"（12.1.5 反馈⑥），比没有地图更糟。
function Api.RetiredMapSources()
    local out = {}
    for i = 1, #MAP_PROBE_EXTRA do
        local src = MAP_PROBE_EXTRA[i]
        if type(src.note) == "string" and src.note:find("已停用", 1, true) then
            out[#out + 1] = src.path
        end
    end
    return out
end

--- 第二个返回值 src = 给出这个地图号的接口名（矩阵不显示，probe/dump 取证用）：
--- 归错组时要知道是哪条接口答的，光看账本里的数字判不出来。
function Api.QuestMapID(questID)
    questID = tonumber(questID)
    if not questID then return nil end
    local cached = mapCache[questID]
    if cached then return cached.id, cached.src end
    for i = 1, #QUEST_MAP_SOURCES do
        local src = QUEST_MAP_SOURCES[i]
        local fn = Api.Resolve(src.path)
        if Callable(fn) then
            local okCall, v1, v2, v3, v4 = pcall(fn, questID)
            if okCall then
                local vals = { v1, v2, v3, v4 }
                local m
                if src.ret then m = tonumber(vals[src.ret]) end
                if not m and type(v1) == "table" then
                    m = tonumber(FirstField(v1, src.field or { "mapID", "uiMapID", "zoneID" }))
                end
                -- 地图号是整数：坐标/等级这类小数、0 与负数一律不算证据
                if m and m > 0 and math.floor(m) == m then
                    mapCache[questID] = { id = m, src = src.path }
                    return m, src.path
                end
            end
        end
    end
    return nil
end

--- 一张图上的任务行（**不做世界任务判别**）。GetPool 走的是 RowsToPool，它会把
--- 非世界任务当场丢掉，所以"地图任务反向索引"和"关键词候选"都不能复用它。
--- 候选名单的顺序按 12.1.5 实测结果定：C_QuestLog.GetQuestsOnMap(2444) 回的是
--- table[#0]（空表），行数据在 C_TaskQuest.GetQuestsOnMap 里
--- （行字段 questID,mapID,isDaily,inProgress,numObjectives,...）—— 先问答得出的那条。
--- 之前 MapQuestIndex 只问 C_QuestLog.GetQuestsOnMap，等于一直在拿空接口补地图，
--- "未知地图"那批就是这么来的。
local MAP_ROW_SOURCES = { "C_TaskQuest.GetQuestsOnMap", "C_QuestLog.GetQuestsOnMap" }

--- 问一张图的任务行。返回 (rows, source)：rows=nil 是这张图问不出（接口全缺失或
--- 报错），空表是"问到了，这张图真没有"——两者绝不能混，把"问不出"当"没有"会把
--- 反向索引整个判成空。行只认自己报的 mapID：上一版拿"这次问的是哪张图"当兜底答案，
--- 结果 12.1.5 实测一整批任务被归进同一张图（用户反馈"都跑到了盘卷蛇岛里"）。
--- includeLog=true 时两块清单并起来（用户 2026-10-23："日志里面有任务时，
--- 接口查不到地图归属的，从任务列表里找"）：C_TaskQuest 那份只有世界任务板上的行，
--- 已经接进日志的那条（丰饶供品母任务、做到一半的辉耀丰饶）只在 C_QuestLog 那份里，
--- 先问答得出的那条就永远补不齐这类任务的地图。
--- 默认不并 —— "这一轮在不在图上"是家族行的准入判据，把日志清单并进来等于
--- 让母任务也拿到板面证据，用户报的"有 2 个丰饶"就是这么来的。
function Api.MapQuestRows(mapID, includeLog)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    local function read(fn)
        if not Callable(fn) then return nil end
        local okCall, rows = pcall(fn, mapID)
        if not okCall or type(rows) ~= "table" then return nil end
        local out = {}
        for j = 1, #rows do
            local row = rows[j]
            -- 老客户端直接回 questID 数组
            local questID = type(row) == "number" and row
                or (type(row) == "table" and tonumber(row.questID) or nil)
            if questID and questID > 0 then
                out[#out + 1] = {
                    questID = questID,
                    mapID = type(row) == "table" and (tonumber(row.mapID) or nil) or nil,
                    isDaily = (type(row) == "table" and row.isDaily == true) or nil,
                    inProgress = (type(row) == "table" and row.inProgress == true) or nil,
                    numObjectives = type(row) == "table" and (tonumber(row.numObjectives) or nil) or nil,
                }
            end
        end
        return out
    end
    local merged, srcs, byID, answered = {}, {}, nil, false
    for i = 1, #MAP_ROW_SOURCES do
        if includeLog or not answered then
            local rows = read(Api.Resolve(MAP_ROW_SOURCES[i]))
            if rows then
                answered = true
                srcs[#srcs + 1] = MAP_ROW_SOURCES[i]
                for j = 1, #rows do
                    byID = byID or {}
                    if not byID[rows[j].questID] then
                        byID[rows[j].questID] = true
                        merged[#merged + 1] = rows[j]
                    end
                end
            end
        end
    end
    if #srcs == 0 then return nil end
    return merged, table.concat(srcs, " + ")
end

--- 反向索引/候选枚举真正会逐个问的入口名单，原样交给 /wqt probe：
--- 报告里问的必须和生产代码问的是同一批，否则证据和实现两套。
function Api.MapRowSources()
    local out = {}
    for i = 1, #MAP_ROW_SOURCES do out[i] = MAP_ROW_SOURCES[i] end
    return out
end

--- 一张图的悬赏/特别任务清单（12.1.5 实测 C_QuestLog.GetBountiesForMapID 行带 questID，
--- 见 Probe.BountyIDsForMap 那份取证实现）。特别任务压根不在世界任务板上，板面三条
--- 来源对它全答不出，逐条问的两条也答 nil —— 所以它是我们唯一一条**可能**给特别任务配图的凭据。
--- **但它按不按图区分还没定论**：第十二轮那份回传看着像"每张图回同样 5 条"，
--- MapQuestIndex 里那道跨图自检就是拿来否掉这种答案的（宁可落未知地图，也不归错组）。
--- 回 nil = 这张图问不出（入口缺失或报错）；空表 = 问到了，这张图真没有悬赏。
function Api.BountyQuestIDs(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    local fn = Api.Resolve("C_QuestLog.GetBountiesForMapID")
    if not Callable(fn) then return nil end
    local okCall, rows = pcall(fn, mapID)
    if not okCall or type(rows) ~= "table" then return nil end
    local out = {}
    for j = 1, #rows do
        local row = rows[j]
        local questID = type(row) == "number" and row
            or (type(row) == "table" and (tonumber(row.questID) or tonumber(row.qID)) or nil)
        if questID and questID > 0 then out[#out + 1] = questID end
    end
    return out
end

--- questID → mapID 的反向索引：逐张地图问"这张图上挂着哪些任务"。
--- 逐条问任务的接口（QuestMapID）全是世界任务口径，已接的日常/战役任务问不出地图
--- （12.1.5 实测"追踪萨拉塔斯"明明在永歌森林>银月城，账本里却 地图=nil，落进底部"未知地图"）。
--- 这里并上任务日志那份清单（用户："日志里面有任务时，接口查不到地图归属的，从任务列表里找"）
--- —— 只补地图，不参与"在不在图上"的判据（那个必须只看世界任务板，见 MapQuestRows 注释）。
--- 板面行只认它自己报的 mapID，问的是哪张图不算证据；悬赏清单反过来，询问图就是答案
--- —— 但**只在这条悬赏真的按图区分时才成立**，见下面第二遍里那道跨图自检。
--- 第三个返回值 srcOf[questID] = 这条凭据的名字，Sync 要按它写账本：
--- 特别任务与板上任务共用一份索引时，来源标签混成一个就没法复盘是谁答的。
function Api.MapQuestIndex(mapIDs)
    local out, asked, answeredBy, srcOf = {}, 0, nil, {}
    -- 这两个标签是 Sync 写进 rec.mapSrc 的那一份，出处同样只在 Config.MAP_SRC
    local ids = mapIDs or {}
    for i = 1, #ids do
        local rows, src = Api.MapQuestRows(ids[i], true)
        if rows then
            asked = asked + 1
            answeredBy = answeredBy or src
            for j = 1, #rows do
                local r = rows[j]
                if r.mapID and r.mapID > 0 and not out[r.questID] then
                    out[r.questID] = r.mapID
                    srcOf[r.questID] = MapSrc("boardReverse")
                end
            end
        end
    end
    -- 悬赏第二遍并：板面行永远优先（那是客户端在行里直答的图号）。
    -- 同一条挂在**两张**被询问图的悬赏清单里 = 这份清单根本不按图区分
    -- （12.1.5 第十二轮实测：逐张问 C_QuestLog.GetBountiesForMapID，每张回同样那 5 条
    -- 使者任务）。那类号一张都不许归 —— 归错组比没图更糟（第十四轮"好几个特别任务
    -- 都跑到了盘卷蛇岛里"）。问不出的图（nil）不参与计数，别把"没答"当成"跨图"。
    local bountyOf, crossMap = {}, {}
    for i = 1, #ids do
        local bounties = Api.BountyQuestIDs(ids[i])
        if bounties then
            asked = asked + 1
            answeredBy = answeredBy or "C_QuestLog.GetBountiesForMapID"
            for j = 1, #bounties do
                local questID = bounties[j]
                if bountyOf[questID] ~= ids[i] then
                    if bountyOf[questID] ~= nil then crossMap[questID] = true end
                    bountyOf[questID] = ids[i]
                end
            end
        end
    end
    for questID, mapID in pairs(bountyOf) do
        if not crossMap[questID] and not out[questID] then
            out[questID] = mapID
            srcOf[questID] = MapSrc("bountyReverse")
        end
    end
    -- 一张图都没问到 = 没证据，回 nil；问到表但里面没任务才是真的空索引
    if asked == 0 then
        Degrade("地图任务反向索引每张图都问不出（" .. table.concat(MAP_ROW_SOURCES, " / ")
            .. " / C_QuestLog.GetBountiesForMapID）")
        return nil
    end
    return out, answeredBy, srcOf
end

--- 关键词候选枚举：把跟踪图上的**全部**任务行摊成 { questID, mapID, name, ... }。
--- 名字逐条问（QuestTitle 有会话内缓存），问不出留 nil —— 候选是"拿去匹配的名字表"，
--- 编一个名字比留空更糟。这张表**不参与池 diff、不写 observation**：它天生含非金币、
--- 非世界任务，混进池会把金币判定和消失判定一起污染。
--- 第二个返回值 stats = { maps=问了几张, answered=答出几张, rows=摊出行数 }，
--- 全问不出时 rows=0 且 answered=0，调用方要能分清"没有候选"和"问不出候选"。
function Api.CandidateQuests(mapIDs)
    local out, stats = {}, { maps = 0, answered = 0, rows = 0 }
    local seen = {}
    for i = 1, #(mapIDs or {}) do
        local rows = Api.MapQuestRows(mapIDs[i])
        stats.maps = stats.maps + 1
        if rows then
            stats.answered = stats.answered + 1
            for j = 1, #rows do
                local r = rows[j]
                stats.rows = stats.rows + 1
                if not seen[r.questID] then
                    seen[r.questID] = true
                    r.name = Api.QuestTitle(r.questID)
                    out[#out + 1] = r
                end
            end
        end
    end
    return out, stats
end

--- 本角色"已 flagged 完成"的 questID 清单。周常交掉以后既不在池、也不在任务日志里，
--- 只有这份清单还认得它 —— 关键词反查靠它把 ID 找出来。
--- 12.1.5 尚未实测（接口名进候选名单让 /wqt probe 问形状）。返回 nil = 问不出，
--- 空表 = 问到了但清单为空：两者必须分得开，否则一次接口报错就够把整周记录判光。
function Api.CompletedQuestIDs()
    local fn = Api.Resolve("C_QuestLog.GetAllCompletedQuestIDs")
    if not Callable(fn) then
        Degrade("已完成清单入口 C_QuestLog.GetAllCompletedQuestIDs 不可用")
        return nil
    end
    local okCall, v = pcall(fn)
    if not okCall or type(v) ~= "table" then
        Degrade("GetAllCompletedQuestIDs 调用失败或返回非表: " .. tostring(v))
        return nil
    end
    local out = {}
    for i = 1, #v do
        local row = v[i]
        local questID = type(row) == "number" and row
            or (type(row) == "table" and (tonumber(row.questID) or tonumber(row.ID)) or nil)
        if questID and questID > 0 then out[#out + 1] = questID end
    end
    if #out == 0 and #v > 0 then
        Degrade(("GetAllCompletedQuestIDs 返回 %d 条但认不出 questID（形状不符）"):format(#v))
        return nil
    end
    return out
end

function Api.MapName(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    for i = 1, #MAP_NAME_SOURCES do
        local fn = Api.Resolve(MAP_NAME_SOURCES[i])
        if Callable(fn) then
            local okCall, info = pcall(fn, mapID)
            if okCall then
                local name
                if type(info) == "table" then
                    name = FirstField(info, { "name", "mapName", "areaName" })
                elseif type(info) == "string" then
                    name = info
                end
                if type(name) == "string" and name ~= "" then return name end
            end
        end
    end
    return nil
end

--- 地图事件 pin（按 M 在图上看到的那块板）= "这一轮去哪办"的唯一自动凭据。
--- 丰饶供品、亚基入侵这类压根不上世界任务板的东西，任务的三块清单（板/日志/悬赏）
--- 全都问不到它，但地图上那个 pin 是 C_AreaPoiInfo 给的 —— 人不用跑过去（用户
--- 2026-10-24："按 M 打开地图就能看到事件，为什么要跑过去呢？"）。
--- 12.1.5 实测：GetEventsForMap(mapID) 回 poiID 名单（形状没保证，{1,2} 与
--- {areaPoiID=1} 两种都认），GetAreaPOIInfo(uiMapID, poiID) 给 name/isCurrentEvent/
--- isPrimaryMapForPOI。认不出形状的 ID 一律丢掉，不猜（编一个 poiID 会让下面所有
--- GetAreaPOIInfo 拿到"看着可用其实是错 pin"的废证据）。
--- **倒计时不从这层拿**：GetAreaPOISecondsLeft 同一条 pin 上一轮答 407906、
--- 这一轮跨图问整轮回 nil，而 IsAreaPOITimed 照样答 true ⇒ 留着上一轮的数就是谎报。
--- 只认客户端自己标 isCurrentEvent 的那几条：GetAreaPOIForMap 会把飞行点/副本口一起捞进来。
function Api.EventPins(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    local listFn = Api.Resolve("C_AreaPoiInfo.GetEventsForMap")
    local infoFn = Api.Resolve("C_AreaPoiInfo.GetAreaPOIInfo")
    if not (Callable(listFn) and Callable(infoFn)) then return nil end
    local okList, v = pcall(listFn, mapID)
    if not okList then return nil end
    if type(v) ~= "table" then return nil end
    local out = {}
    for i = 1, #v do
        local row = v[i]
        local id = tonumber(type(row) == "table"
            and FirstField(row, { "areaPoiID", "id", "poiID" }) or row)
        if id and id > 0 then
            local okInfo, info = pcall(infoFn, mapID, id)
            if okInfo and type(info) == "table" and info.isCurrentEvent ~= false then
                local name = info.name
                -- pin 背后有没有任务号：**12.x 未实测**。答得出（亚基入侵那种事件其实挂着一条
                -- 可接/可完成的任務）就能按关键词注册成行；答不出这行就是纯地标，
                -- 矩阵里做不出"哪个角色做过"。/wqt probe 的"任务号="那列就是来结这个案的。
                local qid = tonumber(info.questID)
                out[#out + 1] = {
                    id = id,
                    mapID = mapID,
                    name = (type(name) == "string" and name ~= "") and name or nil,
                    primary = (info.isPrimaryMapForPOI == true) and true or nil,
                    questID = (qid and qid > 0) and qid or nil,
                }
            end
        end
    end
    return out
end

--- 父图号。同名 pin 会在子图和父图各挂一条（实测丰饶 = 8527@2413 哈籁恩达尔
--- + 8676@2537 奎尔萨拉斯，两条 isPrimaryMapForPOI 都是 false）：拿父图当
--- "去哪办"等于让玩家去整块大陆找，所以归属图要能把父图排掉。
function Api.MapParentID(mapID)
    mapID = tonumber(mapID)
    if not mapID then return nil end
    local fn = Api.Resolve("C_Map.GetMapInfo")
    if not Callable(fn) then return nil end
    local okCall, info = pcall(fn, mapID)
    if okCall and type(info) == "table" then return tonumber(info.parentMapID) end
    return nil
end

function Api.Money()
    local fn = Api.Resolve("GetMoney")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

function Api.ResetTime()
    local fn = Api.Resolve("GetQuestResetTime")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and v or nil
end

--- 下次**周**重置的倒计时（秒）。GetQuestResetTime 不能拿来当周口径用：
--- 12.1.5 用户实测（周五，国服周重置在下周四早上 7 点）矩阵悬停写着"距下次周重置 12 时 6 分"，
--- 那正好是明天早上 7 点 —— 它答的是每日那档。拿每日倒计时减一周反推会把"本周"窗口
--- 每天往前挪 24 小时，周统计跟着错位。所以单独问周口径的入口，谁先给出 (0, 一周] 用谁。
--- 全问不出返回 nil：调用方要么退回每日口径并如实标注，要么报"不知道"，绝不硬当周用。
local WEEK_RESET_SOURCES = {
    "C_DateAndTime.GetSecondsUntilWeeklyReset",
    "GetWeeklyQuestResetTime",
}

function Api.WeeklyResetCountdown()
    for i = 1, #WEEK_RESET_SOURCES do
        local fn = Api.Resolve(WEEK_RESET_SOURCES[i])
        if Callable(fn) then
            local okCall, v = pcall(fn)
            local n = okCall and tonumber(v) or nil
            if n and n > 0 and n <= 7 * 86400 then return n, WEEK_RESET_SOURCES[i] end
        end
    end
    return nil
end

--- 生产代码真会问的周口径入口名单（probe 按同一份逐个报，证据与实现不分家）
function Api.WeekResetSources()
    local out = {}
    for i = 1, #WEEK_RESET_SOURCES do out[i] = WEEK_RESET_SOURCES[i] end
    return out
end

-- 墙上时钟（纪元秒）。周 CD 锚点与交任务时间戳必须落在同一个可比域里，否则下次开机
-- GetTime() 归零，账本里的时间戳跟锚点就成了两个世界的数。候选谁先返回 > 1e9 的数就用谁。
-- 形态未经实测，所以 /wqt probe 会把三个值并排打出来，用游戏内证据定论。
local WALL_SOURCES = { "time", "GetServerTime", "GetLocalTime" }

function Api.WallClock()
    for i = 1, #WALL_SOURCES do
        local fn = Api.Resolve(WALL_SOURCES[i])
        if Callable(fn) then
            local okCall, v = pcall(fn)
            local n = okCall and tonumber(v) or nil
            if n and n > 1e9 then return n, WALL_SOURCES[i] end
        end
    end
    return nil, "no usable wall clock"
end

-- 开机后秒数（会话域，重启归零）。只用来跟 GetQuestResetTime 做差，绝不入库。
function Api.Uptime()
    local fn = Api.Resolve("GetTime")
    if not Callable(fn) then return nil end
    local okCall, v = pcall(fn)
    return okCall and tonumber(v) or nil
end

--- 帧是否可见。交任务钱差窗口用它判断这 15 秒里有没有开邮箱/拍卖行/交易，
--- 形态未证实所以两条候选都试：先问全局函数，再回退 _G[名字]:IsShown()。
--- 两条都不通只能返回 false —— 那意味着窗口校验失效，必须记进降级原因让 /wqt probe 看得见，
--- 不能假装校验通过继续记一笔可疑金币。
function Api.FrameVisible(name)
    local fn = Api.Resolve("FrameIsVisible")
    if Callable(fn) then
        local okCall, v = pcall(fn, name)
        if okCall then return v == true end
    end
    local frame = Api.Resolve(name)
    -- 不判 type(frame) == "table"：帧对象由引擎造，Lua 看到的类型不保证是 table，
    -- 判错会让"开过邮箱就作废"整条静默失效（FrameIsVisible 在 12.1.5 已实测 MISSING，
    -- 回退是唯一一条路，它再失效就没人兜底了）。
    if frame ~= nil and Callable(frame.IsShown) then
        local okCall, v = pcall(frame.IsShown, frame)
        if okCall then return v == true end
    end
    Degrade("帧可见性接口不可用: " .. tostring(name))
    return false
end

--- 一个帧的子帧列表（两种真实形态都收）。
--- 现代客户端 EnumerateChildren 回**迭代器函数**、GetChildren 回**一张表**；
--- 老客户端/离线替身是一个个返回值。只认其中一种的代码会在真客户端上静默扫不到
--- 任何东西（Track 的兜底遍历就是这个形状：`Callable(it.Next)` 恒假）。
--- 返回**新表**：每种形态都摊平成一张列表，代价跟原来每层 `{pcall(GetChildren)}`
--- 一样（每层一个小表，扫完即弃），别在轮询里调它。
function Api.ChildFrames(frame)
    if not frame then return nil end
    if Api.Callable(frame.EnumerateChildren) then
        local okIt, it = pcall(frame.EnumerateChildren, frame)
        -- 带 Next 的对象形态：本资料片没实测过，收着但绝当唯一路径。
        -- 必须排在迭代器分支前面判：Api.Callable(表) 也为真（它只判非 nil），
        -- 反过来的顺序这条分支永远进不去，那段代码就成了写而没跑的。
        if okIt and type(it) == "table" and Api.Callable(it.Next) then
            local out = {}
            while #out < 2000 do
                local okN, _, kid = pcall(it.Next, it)
                if not okN or kid == nil then break end
                out[#out + 1] = kid
            end
            return out
        end
        if okIt and Api.Callable(it) then
            local out = {}
            while #out < 2000 do
                local okN, kid = pcall(it)
                if not okN or kid == nil then break end
                out[#out + 1] = kid
            end
            return out
        end
    end
    if not Api.Callable(frame.GetChildren) then return nil end
    local call = { pcall(frame.GetChildren, frame) }
    if not call[1] then return nil end
    local out = {}
    for i = 2, #call do
        local v = call[i]
        if type(v) == "table" then
            if Api.Callable(v.GetName) then
                out[#out + 1] = v                     -- 是个帧：多返回值形态
            else
                for _, f in pairs(v) do               -- 是一张表：现代形态
                    if type(f) == "table" then out[#out + 1] = f end
                end
            end
        end
    end
    return out
end

--- 这些候选需要 questID 才能问出形状；零参调用只会拿到 "bad argument #1"，
--- 那种错误行既不能算可用也不能算缺失，探测里是废证据。
--- 上一轮 GetQuestsOnMap 报回 "Usage: local quests = C_QuestLog.GetQuestsOnMap(uiMapID)"，
--- 说明它要的是 mapID 不是 questID，所以这里得分参数类型。
local ARG_HINTS = {
    ["C_QuestLog.IsQuestCompleted"] = "questID",
    ["IsQuestCompleted"] = "questID",
    ["C_QuestLog.IsQuestFlaggedCompleted"] = "questID",
    ["C_QuestLog.IsQuestStarted"] = "questID",
    ["C_QuestLog.IsOnQuest"] = "questID",
    ["QuestLog_IsOnQuest"] = "questID",
    ["C_QuestLog.GetQuestRewardMoney"] = "questID",
    ["C_QuestLog.GetQuestRewardItems"] = "questID",
    ["GetQuestRewardValues"] = "questID",
    ["C_QuestRewards.FetchData"] = "questID",
    ["C_QuestLog.GetQuestsOnMap"] = "mapID",
    ["C_TaskQuest.GetQuestsForPlayerByMapID"] = "mapID",
    ["C_TaskQuest.GetQuestsOnMap"] = "mapID",
    ["C_QuestLog.GetQuestRewardMoney"] = "questID",
    ["C_TaskQuest.GetQuestInfoByQuestID"] = "questID",
    ["C_TaskQuest.GetQuestTimeLeftSeconds"] = "questID",
    ["C_TaskQuest.GetQuestTimeLeftMinutes"] = "questID",
    ["WorldMap_GetWorldQuestRewardType"] = "questID",
    ["C_QuestLog.GetQuestRewardCurrencies"] = "questID",
    ["C_QuestLog.GetQuestRewardCurrencyInfo"] = "questID",
    ["GetQuestLogRewardMoney"] = "questID",
    -- 金额候选与"奖励数据到没到"（/wqt quest 的"金额是谁答的"段要拿真 questID 问，
    -- 不进这份名单就只能空调用，问出 nil 说明不了接口不行）
    ["C_QuestRewards.GetCachedQuestMoney"] = "questID",
    ["C_WorldQuestRewards.GetWorldQuestRewards"] = "questID",
    ["HaveQuestRewardData"] = "questID",
    ["C_TaskQuest.RequestPreloadRewardData"] = "questID",
    ["C_QuestLog.GetLogIndexForQuestID"] = "questID",
    -- 已接任务的归属图（AcceptedQuests 现在拿它给日志行配地图号）：12.x 有没有这个入口
    -- 还没实测，进名单就是让 /wqt probe 拿真日志索引问一次，答得出才谈得上覆盖多少行
    ["C_QuestLog.GetQuestLogZoneMapID"] = "logIndex",
    -- QuestMapID 的候选入口（/wqt probe 的"缺地图取证"段要拿着真 questID 逐个问一遍，
    -- 没有这条参数口径就只能空调用，问出 nil 也说明不了接口不行）
    ["C_TaskQuest.GetQuestLocation"] = "questID",
    ["C_TaskQuest.GetQuestZoneID"] = "questID",
    ["C_QuestLog.GetMapForQuestPOIs"] = "questID",
    -- 日志口径的"下一跳在哪张图"：摘掉 GetMapForQuestPOIs 之后，战役/日常任务
    -- （追踪萨拉塔斯那种）在本机就没有地图来源了。这条是唯一还没实测过的候选，
    -- 进 ARG_HINTS 就是让 /wqt probe 拿着真 questID 问一次，答得出才谈接线。
    ["C_QuestLog.GetNextWaypoint"] = "questID",
    ["C_QuestLog.GetQuestTagInfo"] = "questID",
    ["C_QuestLog.GetQuestInfo"] = "questID",
    ["C_QuestLog.GetBountiesForMapID"] = "mapID",
    ["C_QuestLog.GetBountySetInfoForMapID"] = "mapID",
    -- 注册周常与关键词候选要用到的两条（12.1.5 还没实测，进候选名单就是为了让 probe 问得出）：
    -- GetTitleForQuestID 决定"拿着枚举出的 ID 能不能问出名字"，
    -- GetAllCompletedQuestIDs 决定"做完就消失的周常能不能反查回 ID"
    ["C_QuestLog.GetTitleForQuestID"] = "questID",
    -- 轮换计时候选（只给取证，没接进生产）：12.1.5 实测 C_TaskQuest 那两个入口
    -- 拿着 89507 问不出数（导出写着"轮换=问不出"），换这条再问一遍才知道有没有别的入口答话
    ["C_QuestLog.GetTimeAllowed"] = "questID",
    ["C_Map.GetMapInfo"] = "mapID",
    ["C_Map.GetAreaInfo"] = "mapID",
    -- 地图事件 pin（按 M 打开地图看到的那些 pin 就是这份数据，8.0.1 起取代
    -- C_WorldMap.GetMapLandmarkInfo）。用户 2026-10-24 的方向：丰饶"现在在哪个区"、
    -- 亚基入侵在哪个区根本不是任务，拿任务的清单永远找不到 —— 换数据源才谈得上。
    -- 12.x 有没有"当前事件"这一层一点没实测过，先进取证名单问得出再说接线。
    -- 要 poiID 的三条（GetAreaPOIInfo / GetAreaPOISecondsLeft / IsAreaPOITimed）
    -- 这里传不了第二个参数，由 Probe 的事件段按图逐个真问。
    ["C_AreaPoiInfo.GetAreaPOIForMap"] = "mapID",
    ["C_AreaPoiInfo.GetEventsForMap"] = "mapID",
    ["C_AreaPoiInfo.GetQuestHubsForMap"] = "mapID",
    ["C_AreaPoiInfo.GetDelvesForMap"] = "mapID",
    -- 永不上板的城市周常（追踪萨拉塔斯那种）：世界任务口径的入口对它恒 nil，
    -- 只剩"发布者在哪"这一条没实测过。答得出才谈得上给这类行一个真凭据地图。
    ["C_QuestLog.GetQuestGiverMapPosition"] = "questID",
}

function Api.ArgHint(path) return ARG_HINTS[path] end

--- 探测用的 mapID 样本。候选名全是猜的（上一轮实测 C_Map.GetWorldMapID 不存在），
--- 所以带参与不带参各 pcall 一次，并且只认真正像地图号的正整数：
--- 编一个地图号会让下面所有带 mapID 的探测拿到"看着可用其实是错图"的数据。
local MAP_ID_SOURCES = {
    { path = "C_Map.GetBestMapForUnit", arg = "player" },
    { path = "C_Map.GetDisplayedMapID" },
    { path = "C_Map.GetWorldMapID" },
    { path = "GetUIMapID" },
}

local function AsMapID(v)
    local n = tonumber(v)
    if n and n > 0 then return n end
    if type(v) == "table" then
        local m = tonumber(FirstField(v, { "mapID", "uiMapID", "displayedMapID", "bestMapID" }))
        if m and m > 0 then return m end
    end
    return nil
end

function Api.CurrentMapID()
    for i = 1, #MAP_ID_SOURCES do
        local src = MAP_ID_SOURCES[i]
        local fn = Api.Resolve(src.path)
        if Callable(fn) then
            local v
            if src.arg then
                local okCall, got = pcall(fn, src.arg)
                if okCall then v = AsMapID(got) end
            end
            if not v then
                local okCall, got = pcall(fn)
                if okCall then v = AsMapID(got) end
            end
            if v then return v end
        end
    end
    return nil
end

--- 世界任务在 modern 客户端是按地图给的：C_QuestLog.GetQuestsOnMap /
--- GetBountiesForMapID 这类才是候选池源，但它们都要 mapID。
--- 这里只列只读信息接口（Probe 拿真参数问形状），不含任何改变玩家状态的入口。
local MAP_CANDIDATE_PATHS = {
    "C_QuestLog.GetQuestsOnMap",
    -- 12.x 世界地图数据提供者在用的按图源（上一轮探测的盲区，必须带真 mapID 问）：
    "C_TaskQuest.GetQuestsForPlayerByMapID",
    "C_TaskQuest.GetQuestsOnMap",
    -- 任务奖励金额与剩余时间：地图 pin 上能看到金币数和倒计时，说明接口在。
    -- 探针实测：GetQuestLogRewardMoney 返回 copper（金额源）；时间取自 C_TaskQuest.GetQuestTimeLeftSeconds。
    "C_QuestLog.GetQuestRewardMoney",
    "C_TaskQuest.GetQuestInfoByQuestID",
    "C_TaskQuest.GetQuestTimeLeftSeconds",
    "C_TaskQuest.GetQuestTimeLeftMinutes",
    -- pin 奖励图标的实际数据路（_G 实测命中 WorldMap_GetWorldQuestRewardType）：
    -- 奖励金额很可能要从币种奖励列表里挑，逐个带 questID 问形状
    "WorldMap_GetWorldQuestRewardType",
    "C_QuestLog.GetQuestRewardCurrencies",
    "C_QuestLog.GetQuestRewardCurrencyInfo",
    "GetQuestLogRewardMoney",
    "C_QuestLog.GetLogIndexForQuestID",
    "C_QuestLog.GetBountiesForMapID",
    "C_QuestLog.GetBountySetInfoForMapID",
    "C_Map.GetMapInfo",
    "C_Map.GetAreaInfo",
    "C_Map.GetDisplayedMapID",
    "C_Map.GetBestMapForUnit",
}

function Api.MapCandidatePaths() return MAP_CANDIDATE_PATHS end

--- 阶段二（地图按钮 + 矩阵）要用到的入口。这里只判存在、绝不真调用：
--- 地图相关的 FrameXML 接口有副作用（开图、切图），拿探测报告换证据不该改变玩家状态。
--- 地图按钮本身只需要 CreateFrame + SetPoint（父层挂 WorldMapFrame，跟着地图显示/隐藏），
--- 不依赖 RegisterButton 那类内部接口，所以下面这几条查不到也不阻塞按钮，只影响分组与气泡坐标。
local STAGE2_ENTRIES = {
    "WorldMapFrame",
    "WorldMapFrame.ScrollContainer.Child.GridCanvas",
    "WorldMapFrame.AddButton",
    "WorldMapFrame.RegisterButton",
    -- 阶段二按钮现在挂 Minimap（环上图标，比世界地图内部帧可靠），列出来让 /wqt probe
    -- 如实报 PRESENT/MISSING，缺失时按钮走降级（/wqt matrix 仍可用）。
    "Minimap",
    -- 任务池的按图源与世界任务判别（GetPool 依赖它们，缺失即走全量降级）：
    "C_TaskQuest.GetQuestsForPlayerByMapID",
    "C_TaskQuest.GetQuestsOnMap",
    "QuestUtils_IsQuestWorldQuest",
    "C_Map.GetMapInfo",
    "C_Map.GetAreaInfo",
    "GetMapNameByID",
    "C_Map.GetWorldMapID",
    "C_WorldMap.GetWorldMapID",
}

function Api.Stage2Entries() return STAGE2_ENTRIES end

function Api.CandidatePaths()
    local list = {}
    for i = 1, #POOL_SOURCES do list[#list + 1] = POOL_SOURCES[i].path end
    for i = 1, #DONE_SOURCES do list[#list + 1] = DONE_SOURCES[i] end
    for i = 1, #PROGRESS_SOURCES do list[#list + 1] = PROGRESS_SOURCES[i] end
    list[#list + 1] = "C_QuestLog.GetQuestRewardMoney"
    list[#list + 1] = "C_QuestLog.GetQuestRewardItems"
    list[#list + 1] = "GetQuestRewardValues"
    list[#list + 1] = "C_QuestRewards.FetchData"
    list[#list + 1] = "GetQuestResetTime"
    -- 零参：周口径的重置倒计时（周统计与 cycleAnchor 只认这一档，见 Api.WeeklyResetCountdown）
    for i = 1, #WEEK_RESET_SOURCES do list[#list + 1] = WEEK_RESET_SOURCES[i] end
    list[#list + 1] = "GetMoney"
    list[#list + 1] = "FrameIsVisible"
    -- 零参：注册周常的反查来源（形状由 /wqt probe 报，见 Api.CompletedQuestIDs）
    list[#list + 1] = "C_QuestLog.GetAllCompletedQuestIDs"
    list[#list + 1] = "C_QuestLog.GetTitleForQuestID"
    -- 需要日志索引（不是 questID）：Probe.Run 拿真 questID 反查一个索引再问
    list[#list + 1] = "C_QuestLog.GetQuestLogZoneMapID"
    for i = 1, #WALL_SOURCES do list[#list + 1] = WALL_SOURCES[i] end
    return list
end
