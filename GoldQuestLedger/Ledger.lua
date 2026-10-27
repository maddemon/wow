-- 纯账本逻辑：世界任务池 diff、奖励签名。不碰 WoW 全局 API（时间戳由调用方传入），
-- 因此可在 lupa 里离线跑。

GQL.Ledger = {}
local L = GQL.Ledger

local MAX_OBS_PER_QUEST = 4
-- 消失时长小于此值 = 高频区（屠戮者高地/虚影风暴 12 小时级），不写死区域名单
local HIGH_FREQ_GAP = 129600
-- 判"上一轮已经结束"时给到期时刻留的余量。倒计时接口里有一条是按分钟给的
-- （GetQuestTimeLeftMinutes 在候选名单里），向下取整会让 expiresAt 比真到期早最多 59 秒；
-- 没有余量就会在池子还没换的那一刻先判成换轮，把刚交的那笔踢出本轮。
local ROUND_END_SLACK = 120
-- 两次问出来的到期时刻差出这么多才算"换了一条任务"。同一块板上的同一条实例，
-- 到期时刻是服务端定死的，只有分钟取整和问的时刻不同带来的几十秒抖动；
-- 12 小时区的换轮是整段挪 12 小时，15 分钟这道线谁也误伤不了。
local ROUND_CHANGE_TOLERANCE = 900
-- 家族 regKey 的前缀：带这个前缀的是"玩家自己敲关键词点名要的"那一族
-- （Sync 补扫写进账本），与内置预设那种我们猜出来的区分开。写在这里而不是散在
-- Sync/Ledger 两处：判据和写入方必须认同一个字符串。
L.KW_PREFIX = "kw:"

--- 一"族"= 同一件周事的多个子任务（丰饶供品 / 下周换到别的图的那条同名子任务）。
--- 用户 2026-10-24 定的模型：**一族一行**，行由族本体（内置表声明的那条 ID）占；
--- 家族只用来判"这个号本周做过没有"——族里任意一条做过，整族都算已完成。
--- 归属判据**只认代码里声明过的轮换族**（L.PresetFor：ID 在内置表里、或名字命中族词）。
--- 玩家自己敲的关键词不再是并族判据：那是一条筛选条件，不代表"这几个 ID 是同一件周事"。
--- 第十一轮实测踩过：把关键词当族，9 条不同的"特别任务：X"被并成一族，
--- 截图"本周同族 9 条，做过任意一条即算完成"——做过一条就把另外八条也打勾，是谎报。
function L.FamilyKey(db, questID)
    local preset = L.PresetFor(db, questID)
    return preset and tostring(preset.key) or nil
end

--- 内置表里声明过的一族。三条认法：① questID 就在 preset.ids 里（那是我们确知的
--- 周常本体，SavedInstances 也就是拿这一条的每周 lockout 打勾：Progress.lua:1622-1632
--- type=single、questID=89507，全表只此一条，板上那些"丰饶：草药洞穴"它根本不跟）；
--- ② 记录自己挂在某个内置 key 上（任务追踪认亲收进来的子任务名字可以完全不带族词）；
--- ③ 记录里已存的名字命中 preset.match 族词（同族子任务，只当完成证据）。
--- **玩家关键词注册的（kw: 前缀）不算**：第十一轮实测把带 regKey 的都当家族，
--- 永歌森林的"符文石""特别任务"被拖进族待遇，整批行凭空消失
--- （用户："这两个都是周常任务，不应该到期了"）。
--- 只看 rec.name 不看 L.QuestName：认亲每轮要跑在全部记录上，不该为它把标题接口问一遍。
function L.PresetFor(db, questID)
    local declared = L.PresetByID(questID)
    if declared then return declared end
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local key = rec.regKey
    if type(key) == "string" and key ~= "" and not key:find(L.KW_PREFIX, 1, true) then
        local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
        for i = 1, #presets do
            if presets[i].key == key then return presets[i] end
        end
    end
    return L.PresetByName(rec.name)
end

--- ① 只按内置表 ids 认：这条 ID 是不是某族的**族本体**。
--- 补扫要用它：玩家关键词写着"丰饶"时，板上那条同名子任务不能抢走这一行的归属。
function L.PresetByID(questID)
    local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
    for i = 1, #presets do
        local ids = presets[i].ids or {}
        for j = 1, #ids do
            if tonumber(ids[j]) == questID then return presets[i] end
        end
    end
    return nil
end

--- ③ 只按名字认：命中内置族词的那批都算同族（子任务只当完成证据，不占行）。
function L.PresetByName(name)
    if type(name) ~= "string" or name == "" then return nil end
    local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
    for i = 1, #presets do
        local m = presets[i].match
        if type(m) == "string" and m ~= "" and name:find(m, 1, true) then return presets[i] end
    end
    return nil
end

--- 这条 ID 是不是内置表**声明**的族本体（只按 ids 认，不按名字猜）。
--- 补扫要用它：玩家关键词写着"丰饶"时，板上那条同名子任务不能抢走这一行的归属。
function L.IsPresetID(preset, questID)
    local ids = preset and preset.ids or {}
    for j = 1, #ids do
        if tonumber(ids[j]) == questID then return true end
    end
    return false
end

--- 这一条是不是"某个**会轮换搬区**的周常家族里的一条"（= 内置表声明过的族成员）。
function L.IsFamilyMember(db, questID)
    return L.PresetFor(db, questID) ~= nil
end

--- 一族只占一行，代表这一行的是哪个 questID：内置 ids 的第一条优先（那才是周常本体，
--- 界面读它的名字、地图与"这周做没做"）；内置表没声明 ID 的族（测试/临时加的）退到
--- 族里注册过的那条，再退到 ID 最小的那条 —— 每轮必须选出同一条，否则行会在两条 ID 间跳。
--- 玩家在「关键词 · 屏蔽管理」点"忽略"过的那条例外：它仍是家族证据（族里任一做过即做过），但别再拿它当整族的门面，
--- 让位给同族另一条，否则整族会因为一条被删而彻底消失。
function L.FamilyAnchorID(db, preset)
    if not preset then return nil end
    local quests = (db and db.quests) or {}
    local ids = preset.ids or {}
    for j = 1, #ids do
        local declared = tonumber(ids[j])
        if declared and not (quests[declared] and quests[declared].ignored) then
            return declared
        end
    end
    local key = tostring(preset.key)
    local best, bestRank = nil, 9
    for questID in pairs(quests) do
        if L.FamilyKey(db, questID) == key then
            local rec = quests[questID]
            -- 排名：没被忽略的注册行 > 没被忽略的其它成员 > 玩家移除过的
            local rank = rec.ignored and 3 or (rec.regKey == key and 1 or 2)
            if best == nil or rank < bestRank or (rank == bestRank and questID < best) then
                best, bestRank = questID, rank
            end
        end
    end
    return best
end

--- 这一条这一轮该不该占矩阵一行。用户 2026-10-24 的口径（原话）：
--- "我鼠标悬停的丰饶 和 我真正要完成的 丰饶日常 不是一个任务。你反查完成的丰饶为准"、
--- 板上那条"不显示它（它不是周常）"。所以族成员里**只有族本体占行，而且整周常驻**：
--- 它是每周 lockout，不跟着板子换轮出现/消失（SavedInstances 同一族也才一条勾）。
--- 上一版正好反过来：只让拿到板面证据的那条占行，于是族本体永远不出行、
--- 短寿命的板上子任务反倒占了一行（"丰饶换地图了"那次截图量的就是它，剩余 7 小时）。
--- **一族一行没有例外**（2026-10-27）：以前这里让步两次，第二次让步是"玩家在屏蔽管理里点名
--- 要看的族子任务也占一行"（读 manualAdded）。那个豁免跟着字段一起删了 —— 玩家点
--- "添加"是撤销忽略、不是要求破例多占一行，一族两行会把用户 2026-10-24 那条硬口径
--- 从根上撬开。留下的这条不是让步，是判据本身：
--- · 族本体连名字都问不出时不占行 —— 那是一行"任务#89507"的裸 ID，玩家从它读不出
---   任何可行动的信息（与"颜色一对一映射下一步该做什么"同一条界面诚实原则）。
function L.OccupiesRow(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    local preset = L.PresetFor(db, questID)
    if not preset then return true end
    if L.FamilyAnchorID(db, preset) ~= questID then
        return false
    end
    return type(rec.name) == "string" and rec.name ~= ""
end

--- { questID -> 族 key } + { 族 key -> { questID... } }。一次算好，矩阵与导出共用，
--- 免得两处各判各的（界面上并成一族、导出里算两族是最难查的错）。
function L.Families(db)
    local ofQuest, members = {}, {}
    for questID in pairs((db and db.quests) or {}) do
        local key = L.FamilyKey(db, questID)
        if key then
            ofQuest[questID] = key
            local list = members[key]
            if not list then list = {} members[key] = list end
            list[#list + 1] = questID
        end
    end
    return ofQuest, members
end

--- 地图来源标签的唯一出处是 Config.MAP_SRC（值见那份表）。这里**运行时**取而不是
--- 文件级抄一份：Config 在加载顺序里确实在 Ledger 之前，但离线用例可能只加载 Ledger，
--- 抄在文件级会静默拿到 nil 标签，判据当场失真（同下面 L.ManualMapSource() 的写法）。
--- 取不到时回 ""："" 不是任何一个合法标签，而下面两张判定表都不把 "" 收进键
--- （构造时跳过空串），所以拿不到标签只会走"没凭据"那一边，不会把猜的当客户端答复。
local function MapSrc(key)
    local M = GQL.Config and GQL.Config.MAP_SRC
    return M and M[key] or ""
end
local function LEGACY_MAP_SRC() return MapSrc("legacy") end
local function SUSPECT_MAP_SRC() return MapSrc("suspect") end
local function MANUAL_MAP_SRC() return MapSrc("manual") end

--- 摘掉的接口不再问，可它写进账本的地图号还留在 SavedVariables 里：留着就继续按错图归组，
--- 用户下次开矩阵看到的还是"丰饶在屠戮者高地"（12.1.5 反馈⑥），比没有地图更糟。
--- 同步开头按 Api.RetiredMapSources 把这类号清成 nil，后面 HealMissingMaps 用剩下的可信链
--- 重问、PropagateFamilyMaps 用本轮同族证据搬 —— 问不出就落"未知地图"，宁缺不编。
--- 按子串认（"补问 <接口>"与旧版裸接口名两种写法都算），旧记录才清得干净。
--- 例外见下面 SUSPECT_MAP_SRC：真在板上出现过的行不清图，只降级标签。
--- 另一类**不许清，只许标注**：有 mapID 但 mapSrc 是空。上一版把这类一起清成了 nil，
--- 结果第 15 轮反馈②③"太阳的支持应该是属于永歌森林的，怎么改到别的地方了"、
--- "具体地图的特别任务没有了，没有地图的几个特别任务反而还在" —— 整批行堆进底部
--- "未知地图"，比原来更难看清楚。回头看证据：那三条谎报接口**都带自己的来源标签**，
--- 所以"没标签"根本推不出"是谎报接口写的"，它只是旧版没来得及记标签的**客户端答复**。
--- 现在改成：留着图号、打上 `旧记录未标来源`，本轮只要有新证据就照常覆盖
--- （在池的任务 DiffPool 每轮无条件重写 mapID/mapSrc，补问与族搬运也照旧）。
--- 这个标签不在 TRUSTED_MAP_SRC 白名单里：它不参与"族本体搬地图"，
--- 也不会被当成"客户端这轮说它在这张图上"。
function L.LegacyMapSource() return LEGACY_MAP_SRC() end

--- 第三种形状：**真在板上出现过**（poolSeen）却由已停用接口答出来的图号。
--- 这类不许清成 nil —— 12.1.5 实测塌法看这里：幽魂角斗士(96625) 一天前还在池里，
--- 那次池行自己不报地图、逐条问的两条入口一条 MISSING 一条答 nil，图号正是上一版
--- 还挂在链里的 GetMapForQuestPOIs 补出来的。清掉之后它换轮换出板，板面三条来源
--- （池 / 跟踪清单 / 清单反查）只认**当下还在板上**的任务，于是永远补不回来，
--- 整批 12 行堆进底部"未知地图"（用户："任务所属地图乱了…为什么他们没有了所属地图"）。
--- 留着图号 + 打上存疑标签：矩阵照旧分组，HealMissingMaps 每轮照旧重问，
--- 问出新答案就覆盖；它也不在 TRUSTED_MAP_SRC 白名单里，不参与族搬运。
function L.SuspectMapSource() return SUSPECT_MAP_SRC() end

function L.ClearRetiredMaps(db, paths)
    if not (db and db.quests) or type(paths) ~= "table" then return 0 end
    local cleared = 0
    for _, rec in pairs(db.quests) do
        local src = rec and rec.mapSrc
        if rec.mapID and (type(src) ~= "string" or src == "") then
            rec.mapSrc = LEGACY_MAP_SRC()
        elseif type(src) == "string" and src ~= "" and rec.mapID then
            for i = 1, #paths do
                if paths[i] and src:find(paths[i], 1, true) then
                    if rec.poolSeen then
                        rec.mapSrc = SUSPECT_MAP_SRC()
                    else
                        rec.mapID, rec.mapSrc = nil, nil
                    end
                    cleared = cleared + 1
                    break
                end
            end
        end
    end
    return cleared
end

--- 第四种形状：**玩家亲口说的**。这台客户端答不出所属地图的那批（换轮换出板的世界任务、
--- 压根不上板的特别任务），只有玩家知道它在哪张图（12.1.5 第十七轮："我知道每个任务属于
--- 哪个地图"）。前面几轮的"宁缺不编"是对的，但它治的是"别拿猜的写进去"，
--- 治不了"已经清掉的这周补不回来" —— 光靠接口没有出路，得给玩家一条录入的路。
--- 这个标签**故意不匹配任何清理规则**：`ClearRetiredMaps` 按接口名子串认、
--- `HealMissingMaps` 只重问 nil/旧记录/存疑，所以它不会被每轮快照抹掉；
--- 但**真凭据照样覆盖它**（在池的任务 DiffPool 无条件重写 mapID/mapSrc —— 客户端本轮直答
--- 的图比玩家上周的记忆新，这是对的：丰饶那种族本来就搬图）。
function L.ManualMapSource() return MANUAL_MAP_SRC() end

--- 只认整数地图号；地图名由调用方给（这里不碰客户端接口，账本层保持可离线验）。
function L.SetManualMap(db, questID, mapID)
    local rec = db and db.quests and tonumber(questID) and db.quests[tonumber(questID)]
    mapID = tonumber(mapID)
    if not rec or not mapID or mapID <= 0 or math.floor(mapID) ~= mapID then return false end
    rec.mapID, rec.mapSrc = mapID, MANUAL_MAP_SRC()
    rec.mapManual = true
    return true
end

--- 族本体那条记录的地图从同族成员里搬。12.1.5 实测反馈⑥"丰饶出现在了屠戮者高地
--- （当前我的角色就在屠戮者高地，这个不对的）"：那张图是 C_QuestLog.GetMapForQuestPOIs
--- 报的玩家脚下这张图，已从 Api 的地图链里摘掉；摘掉之后 89507 在本机**一个地图来源都没有**
--- （GetQuestZoneID 也答 nil），整行只能落在底部"未知地图"。
--- 同族那些真挂在板上/日志里的成员有客户端答的图，搬过来就是第八轮定的口径
--- （"子任务属于哪个地图，就在哪个地图显示"）：一族一行，行的地图跟本周那条走。
--- 只认下面这几类来源——"询问用的图兜底"是我们自己套的，
--- "同族成员 x"/"地图事件 pin"是搬过来或注册时贴的，传给自己等于把猜的当成本体的。
--- 名单**从 Config.MAP_SRC 现取**（键名对不上就是 nil，不会像抄死的字符串那样
--- 悄悄和写入方分岔）；取不到标签时那个键干脆不收，见上面 MapSrc 的说明。
local trustedSrc
local function TRUSTED_MAP_SRC()
    if not trustedSrc then
        trustedSrc = {}
        local labels = {
            MapSrc("pool"), MapSrc("boardList"), MapSrc("boardReverse"),
            MapSrc("bountyReverse"), MapSrc("manual"), MapSrc("logZone"),
            -- 注册周常/补问这条路径写的是带接口全名的动态串，只有这一条是确知
            -- 客户端直答的入口，所以按"前缀 + 名字"拼出来和写入方对齐
            MapSrc("healPrefix") .. "C_TaskQuest.GetQuestZoneID",
        }
        for i = 1, #labels do
            if labels[i] ~= "" then trustedSrc[labels[i]] = true end
        end
    end
    return trustedSrc
end

local function trustedMapOf(rec)
    if not (rec and rec.mapID) then return nil end
    return TRUSTED_MAP_SRC()[rec.mapSrc or ""] and rec.mapID or nil
end

--- 客户端答过的图。**"玩家指定"不在里面**：那是第十七轮玩家口述填的兜底，
--- 拿来分组就等于替客户端 answering —— 用户 2026-10-24 定的"只降周常"就是把这批
--- 永不上板的常驻周常从口述图里拿出来，口述只当悬停备注。
--- 这里用**黑名单**不用白名单：来源标签是带后缀的动态串（"同族成员 97625"、
--- "补问 C_QuestLog.GetQuestLogZoneMapID"），上一版那份白名单没列全，
--- 于是客户端真答过的图被当成"没凭据"、整批丰饶/特别任务掉进周常组
--- （改完判据时 5 条用例一起红，包括"按板面证据归的图"那条老保证）。
--- 剩下的都是客户端答的：板面/清单/反查/日志分区/同族搬过来的/补问出来的。
--- 这四个标签同样从 Config.MAP_SRC 现取（运行时构造一次）：手抄一份就等于把
--- "谁不算客户端答复"这件事又变成两处判据。
local notClientSrc
local function NOT_CLIENT_SRC()
    if not notClientSrc then
        notClientSrc = {}
        local labels = {
            MANUAL_MAP_SRC(),     -- 玩家口述
            LEGACY_MAP_SRC(),     -- 旧记录没标来源
            SUSPECT_MAP_SRC(),    -- 已知会谎报的接口写的
            MapSrc("asked"),      -- "我问了哪张图"≠"这任务在哪张图"
        }
        for i = 1, #labels do
            if labels[i] ~= "" then notClientSrc[labels[i]] = true end
        end
    end
    return notClientSrc
end
function L.ClientMapOf(rec)
    if not (rec and rec.mapID) then return nil end
    local src = rec.mapSrc
    if not src or src == "" then return nil end
    return not NOT_CLIENT_SRC()[src] and rec.mapID or nil
end

--- 周常组（用户 2026-10-24："如果实现没有地图，可以新增一个周常组，把它放进去"）。
--- 收的是"常驻周常 + 这台客户端一次都没答过它的地图"那批（追踪萨拉塔斯：发布者/日志区/
--- 悬赏/按图清单/IsOnMap/DoesMapShow/威胁清单七条凭据全灭）。
--- 与 -1「未知地图」必须分开：那批是世界任务换轮换出板，地图下一轮自己回来；
--- 这批天生没有地图凭据，混在一起玩家就分不清"等一等就有"和"这台客户端问不出"。
local WEEKLY_GROUP_MAP = -2
function L.WeeklyGroupMapID() return WEEKLY_GROUP_MAP end

--- 常驻周常：**只认客户端明确答"这不是世界任务"**（用户 2026-10-24 第二十二轮：
--- "追踪萨拉塔斯 也应该归入"周常组 —— 它既没注册、也没上板，靠 regKey 那道门永远进不来）。
--- 答 true 不算：它有板面凭据的份，归板。答 nil（接口没答/接口缺失）也不算：
--- "客户端没答"不等于"它不是世界任务"，宁可行留在原分组或落"未知地图"等下一轮，
--- 也不拿一次问不出把一批有板面凭据的任务扫进周常组 —— 第十一轮"符文石、特别任务整批消失"
--- 就是拿宽判据砍合法行的同一种错。
function L.IsResidentWeekly(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    return (GQL.Api and GQL.Api.IsWorldQuest
        and GQL.Api.IsWorldQuest(questID)) == false
end

--- 这一行归到哪个地图分组。三层，顺序不许换（用户 2026-10-24：
--- "如果知道去哪办 那就没必要放在祖阿曼了吧？事件在哪 就在哪个地图分组"）：
--- ① 地图事件 pin 报的"这一轮去哪办"（丰饶 → 哈籁恩达尔，不再挂客户端那个祖阿曼）；
--- ② 客户端答过的图（板面/清单，照旧按客户端来）；
--- ③ 常驻周常问不出图 → 周常组；其余 → 原 mapID（nil 由调用方落 -1「未知地图」）。
function L.RowGroupMapID(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    if type(rec.evMapID) == "number" and rec.evMapID > 0 then return rec.evMapID end
    if L.IsResidentWeekly(db, questID) and not L.ClientMapOf(rec) then
        return WEEKLY_GROUP_MAP
    end
    return rec.mapID
end

--- 成员按 questID 升序排：每轮必须选出同一条，否则行的地图会在两张图之间来回跳。
local function sortedIDs(list)
    local ids = {}
    for j = 1, #list do ids[#ids + 1] = list[j] end
    table.sort(ids)
    return ids
end

--- 返回改了几条。本体自己就有可信地图时不动它；一条成员都不可信时也不动
--- （宁可行落在"未知地图"，也不拿一张没凭据的图占位）。
--- 挑成员：先看这一轮真在板上的（onBoard 那条才是本周的归属），再看日志/池里带可信图的。
function L.PropagateFamilyMaps(db)
    if not (db and db.quests) then return 0 end
    local _, members = L.Families(db)
    local presets = (GQL.Api and GQL.Api.WEEKLY_PRESETS) or {}
    local moved = 0
    for i = 1, #presets do
        local preset = presets[i]
        local list = members[tostring(preset.key)]
        local anchorID = list and L.FamilyAnchorID(db, preset)
        local anchor = anchorID and db.quests[anchorID]
        if anchor and not trustedMapOf(anchor) then
            local ids = sortedIDs(list)
            local srcID, mapID
            -- 两趟：第一趟只看在板上的，第二趟放宽到全部成员
            for pass = 1, 2 do
                for j = 1, #ids do
                    local id = ids[j]
                    local rec = db.quests[id]
                    if id ~= anchorID and not mapID then
                        local m = (pass == 2 or rec.onBoard) and trustedMapOf(rec) or nil
                        if m then srcID, mapID = id, m end
                    end
                end
                if mapID then break end
            end
            if mapID and anchor.mapID ~= mapID then
                anchor.mapID, anchor.mapSrc = mapID, MapSrc("familyPrefix") .. tostring(srcID)
                moved = moved + 1
            end
        end
    end
    return moved
end

--- 任务记录的字段清单 = 唯一出处。字段写在这张表里而不是"谁用到谁加"：
--- 一条记录 28 个字段由 6 个文件各自写、构造点曾有 3 处各写一份 observations = {}，
--- 同一件事散在几个字段里、读法有两套 —— 这就是本轮"金币赖行""状态串轮"这批 bug 的共同根因。
--- 守卫用例 test_quest_records_only_come_from_the_factory 逐文件扫 rec.<字段>，
--- 清单外的写法一律报红：要么补进清单（并说清归谁写），要么那个 rec 根本不是任务记录
--- （角色行 db.chars、补录行 db.manual 都别起名叫 rec）。
--- 本表只声明"有哪些字段"，不动存储结构（本轮不合并、不改名、不挪位置）。
local QUEST_RECORD_FIELDS = {
    -- 板面/接口答的：DiffPool 的池行、AddQuest 的补问、EnsureRegistered 的注册锚点都写这几个
    name = true, mapID = true, mapSrc = true, mapAsked = true, mapManual = true,
    poolSeen = true, onBoard = true, lastSeenAt = true,
    -- 奖励与金币：金额的唯一读法是 L.QuestMoney（"这一轮读到过金币"是 goldMoney/goldRound），
    -- 别处不许再各拼一套判据 —— 上一版就是 obs.gold、lastMoney、goldLock 三处各读各的
    observations = true, currentSig = true, lastMoney = true,
    goldMoney = true, goldRound = true, goldLock = true,
    -- 轮次与到期：roundAt/roundEnd 由 RollRound 按换轮证据推进，到期时刻只认 SetExpiry/SetCycleExpiry
    roundAt = true, roundEnd = true, goneAt = true, highFreq = true,
    expiresAt = true, expiresSrc = true, expiresGuessed = true,
    -- 身份与准入：regKey = 注册周常的键；added 类标记分开存（玩家点过 vs 插件从日志收的）
    -- manualAdded 从 2026-10-27 起**已无读者**（准入门、族占行、同名去重、QuestKnown、
    -- 弹框标签、Dump/Probe 取证都不再认它），只有 v4/v5 迁移还会清它（`rec.manualAdded = nil`）。
    -- 留在清单里是为了字段清单守卫不许谎报：删掉这一项，迁移函数自己就会被判成
    -- "清单外的写法"而报红（test_quest_records_only_come_from_the_factory 扫的是 rec.<字段>）。
    regKey = true, kwLogAdded = true, manualAdded = true, ignored = true,
    -- 地图事件 pin 给的归属（"这一轮去哪办"），只有 Sync 的 pin 段写
    evMapID = true, evName = true, evAt = true,
}

function L.QuestRecordFields() return QUEST_RECORD_FIELDS end

--- 新建一条任务记录：只有这一处能造形状。
--- observations 是唯一天生要在的字段（到处直接 rec.observations[...] 索引，缺了就报错），
--- 其余字段一律"按需长出来"，读的时候自己判 nil —— nil = 没答过，别写 0/"" 冒充真值。
function L.NewQuestRecord()
    return { observations = {} }
end

function L.MakeSig(money, items)
    local parts = { tostring(math.floor(type(money) == "number" and money or -1)) }
    if type(items) == "table" then
        local ids = {}
        for i = 1, #items do ids[#ids + 1] = tostring(items[i]) end
        table.sort(ids)
        parts[#parts + 1] = table.concat(ids, ",")
    end
    return table.concat(parts, "|")
end

function L.SigMoney(sig)
    local v = tonumber((tostring(sig)):match("^(%-?%d+)"))
    return (v and v > 0) and v or 0
end

function L.IsGoldSig(sig)
    return L.SigMoney(sig) > 0
end

--- 签名的金额段是负数 = MakeSig 把"这一档金额压根没读出来"编成 -1 占位。
--- 客户端从没为这种签名答过一句话 ⇒ 它里面存的任何数字都是从别的签名搬来的，
--- 不是读到的（迁移拿它认"被污染的 observation"，见 L.ResetStickyGoldOnce）。
local function UnreadMoneySig(sig)
    local v = tonumber((tostring(sig or "")):match("^(%-?%d+)"))
    return v ~= nil and v < 0
end

local function Push(list, questID) list[#list + 1] = questID end

-- 逐项复制而不是 { unpack(...) }：lupa 侧的 Lua 与 WoW 内嵌 Lua 的 unpack
-- 位置不同（5.1 是全局，5.2+ 挪到 table.unpack），复制循环两边都稳。
local function CopyArray(src)
    local out = {}
    for i = 1, #src do out[i] = src[i] end
    return out
end

local function CountPairs(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

function L.CountObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.observations then return 0 end
    return CountPairs(rec.observations)
end

-- observation 超上限时按 lastSeen 淘汰最旧；当前签名永不淘汰。
local function TrimObservations(rec, keepSig)
    while CountPairs(rec.observations) > MAX_OBS_PER_QUEST do
        local oldestSig, oldestSeen
        for sig, obs in pairs(rec.observations) do
            if sig ~= keepSig and (not oldestSeen or obs.lastSeen < oldestSeen) then
                oldestSig, oldestSeen = sig, obs.lastSeen
            end
        end
        if not oldestSig then break end
        rec.observations[oldestSig] = nil
    end
end

--- 轮次身份 = 这条任务自己的到期时刻（用户 2026-10-25 定的口径："是不是新任务，
--- 看结束时间是不是新日期就够了"）。奖励变化不参与：切狩猎模式翻的是奖励，
--- 那是同一条任务换了一副奖励，不是新任务 —— 上一版把起点存在按奖励签名分的
--- observation 里，切一次模式就凭空造一个新起点，把本周做过的 ✓ 全部判成上一轮。
--- roundAt = 当前这一条实例被确认开始的时刻；"这条证据算不算本轮"只跟它比。
--- 老存档没这个字段时**只认当前签名那份记录的起点**，绝不退回"各份里最早的一次"：
--- 屠戮者高地这种 12 小时区每轮奖励都可能不同，最早那份属于上一轮，拿它补就把上一轮
--- 的已交补成了"本周全做完"（12.1.5 实测反馈 2026-10-25："意外遭遇现在不是金币任务，
--- 但是都标记全部做完了，实际上这一轮都没做过"）。代价照着说：切模式弄丢的那几格
--- 本周不回来，下个 CD 自动干净 —— 少一个勾可以忍，假勾是谎报。
function L.RoundAt(db, rec)
    if not rec then return 0 end
    local at = tonumber(rec.roundAt)
    if at then return at end
    local cur = rec.currentSig and rec.observations
        and rec.observations[rec.currentSig] or nil
    local start = tonumber(cur and cur.firstSeen)
    local anchor = tonumber(db and db.cycleAnchor) or 0
    if start and anchor > 0 and (start > 1e9) == (anchor > 1e9) and start < anchor then
        start = anchor
    end
    at = start or anchor or 0
    rec.roundAt = at
    return at
end

--- 到期门只认**本轮之内读到的那条到期时刻**（用户 2026-10-27 的 89267「神秘身影」：
--- 换轮后做完它的那个号身上 `Api.QuestTimeLeft=nil`、`HaveQuestRewardData=false`，
--- `SetExpiry` 问不出就一个字不写，而 `RollRound` 只推 `roundAt/goldRound`、不清到期时刻
--- ⇒ 到期时刻一直停在上一条实例（20 小时前）⇒ 矩阵"到期即移除"那道门把整行吃掉。
--- 他第二个号没做过 ⇒ 这条还挂在板上 ⇒ 倒计时答得出 11 时 37 分 ⇒ 到期时刻被推到未来 ⇒ 放行。
--- 全程卡的是到期时刻，与"上过板/凭据"无关。）
--- 三条边界，一条都不能省：
--- · `exp < at` 恰好就是"靠到期换的那一轮"：`RoundEnded` 要求 `now >= exp + 松弛` 才 `RollRound`，
---   所以到期换轮之后必然 `at > exp`。健康的同一轮里 `exp = 起点 + 倒计时 > at`，永远不受影响。
--- · 跨时钟域不判、原样返回（同 L.Prune / ThisCycle / WeekStats 那几道守卫）：`L.RoundAt` 兜底
---   会回退到 `db.cycleAnchor`（墙上秒），而 `expiresAt` 可能是开机秒，跨域比大小会把好好的行
---   放回或吃掉，两种都是谎报。
--- · `at == 0`（锚点和本轮起点都没定出来）⇒ 原样返回，行为与今天一致，不借本轮之名新增放行。
--- 代价照实：换轮后到倒计时读到之间这一行不再被"到期"吃掉，但剩余那格是空的、悬停不报倒计时
--- （本来也问不出），`TimeColor` 对 nil 与 ≤0 同色，不会误红。
--- 副作用照旧：`L.RoundAt` 会顺手把 `rec.roundAt` 落账（MatrixRows 每帧都在调它，纯读账本、
--- 不碰任何接口）。
function L.EffectiveExpiry(db, rec)
    if not rec then return nil end
    local exp = tonumber(rec.expiresAt)
    if exp == nil then return nil end
    local at = tonumber(L.RoundAt(db, rec)) or 0
    if at > 0 and (exp > 1e9) == (at > 1e9) and exp < at then return nil end
    return exp
end

--- 换轮 = 起点推到当下，作废一份"本轮之内才算数"的凭据：
--- · 金币粘滞（goldRound）：新实例重新判这一副给不给金币。
--- 以前这里还清第二份（manualAdded，用户 2026-10-26 的口径"点过一次添加只算这一条实例"）。
--- 那个字段 2026-10-27 整体下线（点"添加"是撤销忽略、不是标记要上板），字段没人写了也没人读了，
--- 这条清理自然不用再存在；行该不该在列回到五道自动门，见 MatrixRows 那段。
--- 落点只有这一处 = "这一条实例换了一条"的唯一写点，两个调用方（DiffPool 的到期换轮、
--- SetExpiry 的结束时间变化）都覆盖到，不多花一次接口调用、不加轮询。
local function RollRound(rec, now)
    rec.roundAt = now
    rec.goldRound = nil
end

--- 上一轮自己的到期时刻已过、这一轮它又挂在池里 = 新一轮。
--- "抓到他不在池里"这条边界在 12.x 基本抓不到：答得出数据的图源只有按图问的那条
--- （partial=true），消失判定整段被关掉，只能靠它自己的倒计时（12.1.5 实测：
--- 屠戮者高地"秘密狩猎"全角色 ✓，新刷出来的那一轮再也记不上）。就算拿到全量池，
--- 两次快照之间没开过面板同样会错过那个空档，所以这条判据对两种池都成立。
--- 只认 expiresSrc == "接口倒计时"（拿着这条任务的 questID 问出来的剩余时间）：
--- 排程值是我们编的 24 小时，"下次周/每日重置"是 CD 的尾巴，都不是这一轮的寿命。
--- `roundAt < 到期时刻` 让这条判据每轮只成立一次：到期时刻要等 Sync 那趟补读倒计时
--- 才往前挪，没有它，倒计时问不出的那几轮会把起点一路往前推，本轮刚交的会被自己踢出本轮。
local function RoundEnded(rec, at, now)
    local exp = tonumber(rec.expiresAt)
    return rec.expiresSrc == "接口倒计时" and exp ~= nil
        and (at or 0) < exp and now >= exp + ROUND_END_SLACK
end

--- 用当前角色的世界任务池快照跟账本 diff。
--- "本轮"= 连续存在于两次快照之间，不设时间阈值：任务消失后再出现即新一轮
--- （无论隔 12 小时还是 4 天），高频区与周常区共用同一套逻辑，插件不需要知道任何刷新周期。
--- 没被"看见消失"过的那一轮靠自己的到期时刻换轮，见 RoundEnded。
--- partial=true 表示这次只看到了一部分地图（12.x 按图源）：不在池里的任务绝不能标消失，
--- "没看见"和"看见它没了"是两回事，混起来会把别的地图的任务全误判成结束。
function L.DiffPool(db, pool, now, partial)
    partial = partial == true
    local res = { added = {}, gone = {}, renewed = {}, sigChanged = {} }
    local seen = {}

    for i = 1, #pool do
        local q = pool[i]
        local questID = tonumber(q.questID)
        if questID then
            local sig = L.MakeSig(q.money, q.items)
            local rec = db.quests[questID]
            local isNew = not rec
            if isNew then
                rec = L.NewQuestRecord()
                db.quests[questID] = rec
                Push(res.added, questID)
            end
            local prevSig = rec.currentSig
            -- 高频只看"这个任务消失了多久"，与奖励签名无关：12 小时区每轮奖励可能都不同，
            -- 绑在签名上就永远学不到。同一次"整块板没看见它、这次又出现"也是换轮的正证据
            -- （用户 2026-10-25 定的两级判据里，到期时刻问不出来时退回的那一条）。
            local rolled = isNew or rec.goneAt ~= nil
            if rec.goneAt then
                if now - rec.goneAt < HIGH_FREQ_GAP then rec.highFreq = true end
                rec.goneAt = nil
            end
            rec.name, rec.mapID, rec.lastSeenAt = q.name, q.mapID, now
            -- 池行 = 客户端直接说"这任务在这张图上"，是同名多份 questID 里最硬的一份证据；
            -- 记一笔 poolSeen 让矩阵的去重知道该留哪条
            rec.poolSeen = true
            -- 地图是谁答的原样带进账本（"池行自带"/"补问 C_xxx"/"询问用的图兜底"）。
            -- 上一版这里无条件清成 nil，于是 12.1.5 报告里的"地图=2512 来源=池、
            -- mapSrc 全空"根本分不出是客户端把任务跨图列进了 2512，还是我们兜底硬套的
            -- —— 而这正是"萨拉塔斯归错组"唯一的分岔口，取证段因此永远定不了案。
            rec.mapSrc = q.mapSrc
            -- 池行还带着"扫的是哪张图"：与 mapID 不同说明客户端把这个任务列进了另一张图
            -- 的清单（12.1.5 实测：问 2395 回来的行写着 mapID=2393）。归组暂时照旧按
            -- mapID，但这个不一致必须留在账本里 —— /gql probe 的地图取证段就是靠它挑样本，
            -- 没有这一笔，下一份报告还是"账本里没有缺地图的记录"，错分组永远查不出来源。
            rec.mapAsked = (q.askedMapID and q.askedMapID ~= q.mapID) and q.askedMapID or nil
            -- 记住最近一次读到的金额：任务离开池子时 currentSig 会被清空
            -- （本轮结束），没有这个兜底矩阵行会连同金额一起蒸发 ——
            -- 用户实测"点完完成，任务就消失了"就是这么来的
            if type(q.money) == "number" then rec.lastMoney = q.money end

            -- 换轮只认这两条：这一条实例自己的到期时刻变了（SetExpiry 那侧判），
            -- 或者上一轮的到期时刻已经过去、板面又挂着它。换奖励不在其中。
            if not rolled and RoundEnded(rec, L.RoundAt(db, rec), now) then rolled = true end
            if rolled then RollRound(rec, now) end
            local at = L.RoundAt(db, rec)
            -- 换轮先把这份复用的 observation 里的旧数作废：observation 是按奖励签名分的，
            -- 新一轮如果又读回同一副签名（12 小时区里同一副奖励隔一轮重现很常见），
            -- 上面 RollRound 只推了轮次起点，签名那份记录还留着上一轮的金额。
            -- 不清它，下面的读闪延续就等于"这一轮一个字没答，拿上一轮的金币数冒充本轮读到的"
            -- —— 用户 12.1.5 实测"神秘身影之前是金币任务，现在不是了，但还在矩阵里"就是这条链。
            if rolled then
                local reuse = rec.observations[sig]
                if reuse then reuse.money, reuse.gold = nil, nil end
            end

            local obs = rec.observations[sig]
            if obs then
                obs.goneSince = nil
                -- firstSeen 从这一版起只是 roundAt 的镜像（老报告、老存档都读它，
                -- 留着省得把 Dump 一次性改光），真源是 rec.roundAt，见 L.RoundAt
                obs.firstSeen, obs.lastSeen = at, now
                -- 这一读客户端答了金额就按它记账（签名里的数字本来就是那笔金额）：
                -- 上面换轮作废、迁移作废都可能把这份记录清空，不写回去等于
                -- "旧的清了、新的没记上"，同一副奖励隔一轮重现的金币任务白掉一行。
                if type(q.money) == "number" then
                    obs.money = q.money
                    -- 本轮直接读到 0 = 客户端明说这一档不给金币 ⇒ gold=false（第 35 轮）。
                    -- 以前 `> 0 or nil` 把读到的 0 折成 nil（"判不了"），和新建那支（签名 0 → false）
                    -- 两处写两个值，"接和交都答 0 才否决"那道门从此接这一头永远判不出。
                    -- 只改直接读到这一支：667 那支是从别的签名搬来的，搬来的 0 不是本轮客户端答的。
                    obs.gold = q.money > 0
                elseif type(obs.money) ~= "number" then
                    -- 读闪补救：这份 observation 金额是空的，但同任务别的签名读到过真金额
                    -- （预加载时差），延续过来，别让矩阵行忽隐忽现。
                    -- 只在本轮之内延续：firstSeen 是 roundAt 的镜像，上一轮那份签名里的数
                    -- 属于上一轮那条实例，搬过来等于给新一轮凭空造一份金币凭据。
                    for _, other in pairs(rec.observations) do
                        if type(other.money) == "number"
                            and (other.firstSeen or 0) >= at then
                            obs.money = other.money
                            -- 搬过来的 0 = 客户端明说那一档不给金币，不许标成金币任务
                            -- （/gql dump 的金币清单直接读 obs.gold）
                            obs.gold = other.money > 0 or nil
                            break
                        end
                    end
                end
            else
                -- 预加载时差会把同一任务的金额读闪：这次真金额、下次 0（数据没到）。
                -- 不延续的话 currentSig 会指到"金额为空"那份 observation 上，
                -- 矩阵行跟着忽隐忽现（用户实测"来回点几次列表就变、任务缺失"）。
                -- 上一份有金额就延续过来，宁可显示旧数也不能让行凭空消失。
                -- 同样只认本轮的上一份：换轮后 prevSig 那份是旧实例的奖励，跟本轮无关。
                local prev = prevSig and rec.observations[prevSig]
                local carried
                if type(q.money) ~= "number" and prev
                    and type(prev.money) == "number"
                    and (prev.firstSeen or 0) >= at then
                    carried = prev.money
                end
                rec.observations[sig] = {
                    gold = L.IsGoldSig(sig) or ((carried or 0) > 0),
                    money = (type(q.money) == "number") and q.money or carried,
                    items = q.items and CopyArray(q.items) or nil,
                    firstSeen = at,
                    lastSeen = now,
                }
                TrimObservations(rec, sig)
            end
            -- 本轮读到过金币就记一笔：双模式的任务切到不给金币那一档时不许掉出矩阵
            -- （用户 2026-10-25："之前是金币，那么也不应该移除（结束时间没变）"），
            -- 换轮时这份粘滞随 RollRound 一起作废，新实例重新判给不给金币
            local cur = rec.observations[sig]
            if cur and type(cur.money) == "number" and cur.money > 0 then
                rec.goldMoney, rec.goldRound = cur.money, at
            end
            -- 原来就有签名 → 奖励变了；消失后换了奖励回来 → 同样是新一轮。
            -- 到期换轮又同时换奖励的（切模式撞上 12 小时区刷新）两个都报：
            -- 只报 sigChanged 会让玩家看不懂"不过换了个奖励，怎么满屏 ✓ 没了"。
            if prevSig and prevSig ~= sig then Push(res.sigChanged, questID) end
            if not isNew and rolled then Push(res.renewed, questID) end

            if rec.currentSig ~= sig then
                rec.currentSig = sig
            end
            seen[questID] = true
        end
    end

    for questID, rec in pairs(db.quests) do
        -- kwLogAdded = 从任务日志收进来的（城市日常/战役任务），它本来就不在世界
        -- 任务池里，拿"没在池里出现"判它本轮结束是错的（一判就把 currentSig 清空、
        -- 矩阵行变成一道灰横线，用户看到的还是"我加了关键词却没有任务"）。
        if not partial and not seen[questID] and rec.currentSig and not rec.kwLogAdded then
            local obs = rec.observations[rec.currentSig]
            if obs then obs.goneSince = now end
            rec.goneAt = now
            rec.currentSig = nil
            Push(res.gone, questID)
        end
    end

    return res
end

-- 当前池内那份 observation；不在池中返回 nil（调用方据此显示"本轮已结束"）。
function L.GetObs(db, questID)
    local rec = db.quests and db.quests[questID]
    if not rec or not rec.currentSig then return nil end
    return rec.observations[rec.currentSig]
end

function L.IsHighFreq(db, questID)
    local rec = db.quests and db.quests[questID]
    return not not (rec and rec.highFreq)
end

function L.GetQuestName(db, questID)
    local rec = db.quests and db.quests[questID]
    return rec and rec.name or nil
end

--------------------------------------------------------------------
-- 交任务、状态、周统计、撤销与体积清理
--------------------------------------------------------------------

local WEEK = 604800
local MAX_TURNINS_PER_QUEST = 8

function L.OnTurnIn(db, charKey, questID, t, g, sig)
    db.turnins[charKey] = db.turnins[charKey] or {}
    local log = db.turnins[charKey]
    log[questID] = log[questID] or {}
    local rows = log[questID]
    rows[#rows + 1] = { t = t, g = g, sig = sig, cycle = db.cycleAnchor or 0 }
    while #rows > MAX_TURNINS_PER_QUEST do table.remove(rows, 1) end
    if type(g) == "number" and g > 0 then
        L.MarkGoldByObservation(db, questID, sig)
    end
    return true
end

--- 某个号"上一次交这条"是哪一刻、到手多少、是谁记的（实测流水 / 手动补录）。
--- 矩阵悬停与导出都问这一处：界面自己翻 db.turnins 就等于再写一套判据，
--- 流水的存法一改（第 20 轮那次 cycle 字段就是）报告会开始说瞎话。
--- 两份都在时取时刻新的那份；能不能报成日期由调用方问 Config.FormatStamp
--- （开机秒域的时间戳格式化出来是 1970 年，那一处才是判域的地方，这里不重复判）。
--- 返回 t, g, src；没记录回 nil。
function L.LastCompletion(db, charKey, questID)
    local best
    local rows = db and db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local r = rows and rows[#rows]
    local rt = r and tonumber(r.t)
    if rt then best = { t = rt, g = tonumber(r.g), src = "实测" } end
    local m = db and db.manual and db.manual[charKey] and db.manual[charKey][questID]
    local mt = m and tonumber(m.t)
    if mt and (not best or mt > best.t) then
        -- 说话按凭据说：点格子时客户端自己答了已完成的，不能报成"手动补录"
        -- （用户 2026-10-25：那等于单独查询一次、核验完成了）
        best = { t = mt, g = tonumber(m.g), src = m.v or "手动补录" }
    end
    if not best then return nil end
    return best.t, best.g, best.src
end

--- 这一格有没有玩家补录（不分新旧）。矩阵点格子要问它，不能问 L.State：
--- 补录过期以后 L.State 不再答 done，界面拿"当前状态"当撤销凭据，玩家就再也
--- 取消不掉自己点过的那一笔（再点一次等于重新补录）。
function L.HasManualMark(db, charKey, questID)
    return not not (db and db.manual and db.manual[charKey] and db.manual[charKey][questID])
end

--- 一条任务的到期时刻由**它是什么**决定，不由哪个接口先答出来决定。两种口径：
--- · 玩家/插件认定的**常驻周常**（带 regKey，或从任务日志收进来的 kwLogAdded）→ 只认
---   CD 尾巴（SetCycleExpiry：下次周重置）。板面倒计时一律不许写进来：图上一换轮，
---   这些任务的**板面实例**只剩几小时，可它们的每周 lockout 还活着，拿板的倒计时当到期
---   等于当天下午就把整行抹掉（12.1.5 实测：加固符文石这种注册周常"这个任务没了"，
---   用户："可能和倒计时有关系，倒计时不准"；上一轮同类反馈是"符文石和特别任务…
---   这两个都是周常任务，不应该到期了"）。
--- · 没注册的池内世界任务 → 用它自己问出来的板面倒计时，到期即消失（"时间结束后都要移除"）。
function L.SetExpiry(rec, now, remaining)
    if rec.regKey or rec.kwLogAdded then
        L.SetCycleExpiry(rec, now)
        return
    end
    if type(remaining) == "number" and remaining > 0 then
        local endAt = now + remaining
        -- 结束时间变了 = 板面上换成了另一条任务（用户 2026-10-25："是不是新任务，
        -- 看结束时间是不是新日期就够了"）。同一条实例的倒计时只会有分钟取整带来的
        -- 几十秒抖动，差出 15 分钟以上只能是换了一条 ⇒ 起点推前、金币判定重新读。
        local prevEnd = tonumber(rec.roundEnd)
        if prevEnd and math.abs(endAt - prevEnd) > ROUND_CHANGE_TOLERANCE then
            RollRound(rec, now)
        end
        rec.roundEnd = endAt
        rec.expiresAt, rec.expiresSrc, rec.expiresGuessed = endAt, "接口倒计时", nil
    end
end

-- 手动补录。state == "open" 视为清除手动覆盖，回到自动判定。
-- 第 7 个参数 v = 点这一格的时候**客户端自己答了**"已完成"（接口核验）。它跟玩家凭手感
-- 点的补录不是一件事：前者要用自动判定的颜色，后者要让玩家一眼看出"这格是我手点的"
-- （判据从 L.State 的第二返回值拿，别再在界面里翻一遍 db.manual 拼第二套口径）。
function L.SetManual(db, charKey, questID, state, t, g, v)
    db.manual[charKey] = db.manual[charKey] or {}
    local old = db.manual[charKey][questID]
    if state == "open" then
        db.manual[charKey][questID] = nil
    else
        db.manual[charKey][questID] = { state = state, t = t, g = g, v = v }
    end
    return old
end

function L.ClearManual(db, charKey, questID)
    if db.manual[charKey] then db.manual[charKey][questID] = nil end
end

--- 读不到"这条任务自己的倒计时"时给它一个到期时刻。
--- 优先用服务端下次周重置（跟 cycleAnchor 同一条接口，是实测值）：任务日志里接着的
--- 周常活整个 CD，拿 24 小时顶着的后果是这行隔天自己没了、/gql quest 还报
--- "最迟 X 后自动消失"（12.1.5 实测：追踪萨拉塔斯是周常，判出来的日期却"马上就要结束"）。
--- 下次重置也定不出来（接口缺失/时钟域对不上）才退回 24 小时排程值并标 guessed。
--- `expiresSrc` 是给显示层看的：接口倒计时=这条任务自己的剩余时间，下次周重置=本 CD
--- 结束，两者都不算谎报、可以上色，但话不能说成同一句；排程值一律不上色、不报剩余。
--- 12.1.5 实测 GetQuestResetTime 回的是**每日**重置（45607 秒 ≈ 12 小时6分，跟当时矩阵
--- 顶部的"距下次周重置·12时6分"完全对得上），所以 NextCycleEnd 会带上 kind：
--- 只有 weekly 才配叫"下次周重置"，退回每日时必须写成"距下次每日重置"，
--- 宁可话说小一点，也不能让玩家拿 12 小时当本周 CD 的尾巴。
function L.SetCycleExpiry(rec, now)
    local endAt, kind = nil, nil
    if GQL.Config and GQL.Config.NextCycleEnd then
        endAt, kind = GQL.Config.NextCycleEnd(now)
    end
    if type(endAt) == "number" and endAt > now then
        rec.expiresAt, rec.expiresSrc, rec.expiresGuessed =
            endAt, (kind == "daily") and "下次每日重置" or "下次周重置", nil
    elseif type(rec.expiresAt) == "number" and rec.expiresAt > now
        and rec.expiresSrc ~= "排程值" then
        -- 这一轮接口问不出（偶发 nil / 时钟域对不上）：账本里那条**真数**留着。
        -- 上一版这里无条件写 24 小时排程值，等于一有抖动就把接口读到的周尾巴
        -- 换成编的数，界面还照样报"剩余 X"——降级不该长得像真数。
    else
        rec.expiresAt, rec.expiresSrc, rec.expiresGuessed = now + 86400, "排程值", true
    end
end

--- 手动添加任务（点地图 pin / 矩阵"添加任务"打开的两栏"关键词 · 屏蔽管理"窗口走这条）。
--- **玩家侧的含义只有一句话：撤销"别再显示这一行"的意图**（用户 2026-10-27 的原话：
--- "手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了"）。
--- 上一版这里写 `manualAdded = true`，那道标记是矩阵六道准入门之一，还单独撑破了同名
--- 去重豁免、一族一行的破例、"本资料片过滤"跳过 —— 等于把一次撤忽略当成了一张上板
--- 通行证，而玩家从没做过"标记这条要强制上板"这个动作。字段本轮整体下线，这条路径
--- 现在只做账：建记录（没有的话）、问名字/地图/金额、写一条 observation、续 lastSeenAt、
--- 清 ignored、定个默认到期防止立刻被"到期移除"。
--- **它不承诺这一行会出现在矩阵上**：清完忽略之后该不该上板，由剩下的五道自动判据
--- 重算（在不在板上 / 命中关键词 / 日志收的 / 注册周常 / 本轮金币过门槛）。
--- 记录还是要建，因为快照与取证后面问得到它才有凭据可算。
--- addedBy == "questlog" 时记成 kwLogAdded：那是插件从任务日志自动收的（关键词命中 +
--- 任务日志里有），跟玩家点什么无关，是一道上板的自动凭据，保留。
function L.AddQuest(db, questID, now, addedBy)
    questID = tonumber(questID)
    if not db or not questID then return false, "questID 无效" end
    db.quests = db.quests or {}
    local rec = db.quests[questID]
    local isNew = not rec
    if isNew then rec = L.NewQuestRecord() end

    if not rec.name and GQL.Api and GQL.Api.QuestTitle then
        rec.name = GQL.Api.QuestTitle(questID)
    end
    if not rec.mapID and GQL.Api and GQL.Api.QuestMapID then
        local mapID, src = GQL.Api.QuestMapID(questID)
        rec.mapID = mapID
        rec.mapSrc = mapID and (MapSrc("healPrefix") .. tostring(src)) or nil
    end
    local money = (GQL.Api and GQL.Api.QuestRewardMoney)
        and GQL.Api.QuestRewardMoney(questID) or nil
    if type(money) == "number" then rec.lastMoney = money end

    -- 写/续一条 observation，使金额与状态在矩阵可见
    local sig = L.MakeSig(money, nil)
    local obs = rec.observations[sig]
    if obs then
        obs.lastSeen = now
    else
        rec.observations[sig] = {
            gold = (type(money) == "number" and money > 0) or false,
            money = money,
            items = nil,
            firstSeen = now,
            lastSeen = now,
        }
    end
    rec.currentSig = sig
    rec.lastSeenAt = now
    -- 玩家（或日志源）重新点名 = 撤销"别显示这行"的意图；不清掉的话窗口里点它没反应。
    -- 除了这一句什么都不做：上一版这里给玩家那一路写 manualAdded 当准入门，
    -- 那个字段本轮整体下线（见函数头），点一下不换来任何上板凭据。
    rec.ignored = nil
    if addedBy == "questlog" then
        -- 日志源那一路是一道上板凭据（关键词命中 + 任务日志里有这条），照旧记账
        rec.kwLogAdded = true
    end
    if not rec.expiresAt or rec.expiresSrc == "排程值" then
        -- 接口读到过真倒计时（或上一轮已按周重置定好）就别再覆盖，剩下的兜底
        -- 优先下次周重置，见 SetCycleExpiry
        L.SetCycleExpiry(rec, now)
    end
    db.quests[questID] = rec
    return true, isNew and "新增" or "已存在(刷新)"
end

--- 移除任务（点错了 / 关键词误命中了不想要的任务）。
--- **不能真删记录**：池里/日志里的任务每次同步都会被重新建出来，删了等于没删
--- （12.1.5 实测反馈："右键点击后还会刷出来"）。改成记一条"用户明确不要这行"的
--- 意图 `ignored`，记录本身留着（矩阵不列，「关键词 · 屏蔽管理」右栏点"取消忽略"可以恢复）。
--- **永久**：换 CD、换轮都不作废，只有玩家在"添加任务"里点回来（L.AddQuest 清 ignored）
--- 才撤。用户 2026-10-27 的原话把意图说得很死："我最开始 忽略 任务的意图 就是想把
--- 关键词匹配到了我不想要的任务，比如丰饶关键词，匹配到了辉煌丰饶，但是辉煌丰饶不是世界
--- 任务，我要屏蔽掉，那么我这个意图其实是想永久屏蔽的" —— 上一版（2026-10-24 那句
--- "下个 CD 是金币任务就该自动回来"）把它做成只管本 CD，是理解错了意图，`ClearCycleIgnores`
--- 整个删掉。老存档里那份按旧语义点的忽略由升级档 v5 一次性作废，玩家重新点出想要的。
--- 写 `true` 而不是时间戳：永久意图没有"到期时刻"，留个数字会被后来人读成会过期。
--- 忽略只改"显不显示"，不改账：角色级的手动补录（db.manual）与交任务流水（db.turnins）
--- 都留着，金币汇总不许因为藏了一行就凭空少一笔。
--- 行上的来源标记 `kwLogAdded` 要清掉 —— 屏蔽之后它不再是事实，留着恢复时会被误当成
--- 本来就在列；下一次同步日志源要是真命中了关键词会自己把它写回来（那条路认的是配置）。
--- `manualAdded` 那一句以前这里也清，随字段整体下线一起删了（v5 迁移负责清老存档残留）。
function L.RemoveQuest(db, questID)
    questID = tonumber(questID)
    if not db or not questID then return false, "questID 无效" end
    local rec = db.quests and db.quests[questID]
    if not rec then return false, "账本里没有这个任务" end
    rec.ignored = true
    rec.kwLogAdded = nil
    return true, "已屏蔽"
end

function L.SetView(db, charKey, questID, state, t)
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    db.chars[charKey].views[questID] = { s = state, t = t }
end

--- 按号记一笔"这个号自己读到这条任务的奖励是多少钱"。
--- 为什么必须按号：世界任务的奖励是**各号各自**的 —— 同一个 questID，一个号读到金币、
--- 另一个号读到物品（12.1.5 实测 97128「巢穴：尼姆瑞莎·唤波者」：Mad 给 1090 金、
--- 爱情之力给 150 宝匣钥匙碎片）。账本里原来只有 rec.currentSig / goldMoney 这一份
--- "最后一次同步的那个号看到的那一副"，于是给金币的号全刷完了，矩阵却还对没金币的号
--- 说"这一档不给金币"，对还没同步的号说"这一轮不给金币" —— 那句话根本不是它答的。
--- 只在 Sync 的池循环这一个写点落账：金额读不到（nil = 数据没到，不是"不给金币"）
--- 就不动旧数，0 是客户端明说的"这一副奖励没有金币"，照写。
function L.SetRewardRead(db, charKey, questID, money, t)
    if type(money) ~= "number" then return false end
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    local row = db.chars[charKey]
    row.reads = row.reads or {}
    row.reads[questID] = { g = money, t = t }
    return true
end

--- 这一号本轮读到的是多少金币；nil = 这一号本轮没读到过它的奖励（**不等于**"不给金币"）。
--- 轮次门与 L.State 的视图门同一口径：跟 L.RoundAt（这条实例自己的起点）比，
--- 上一轮那份钱不许冒充本轮（换 CD / 换实例后 reads 里那条自然失效，下一次同步重写）。
--- 时钟域分裂时（reads.t 是开机秒、roundAt 是纪元秒）判不出来 = 不认，宁缺不编。
function L.CharRewardMoney(db, charKey, questID)
    local row = db and db.chars and db.chars[charKey]
    local r = row and row.reads and row.reads[questID]
    local t = r and tonumber(r.t)
    if not t then return nil end
    local rec = db.quests and db.quests[questID]
    if rec and t < L.RoundAt(db, rec) then return nil end
    return tonumber(r.g)
end

--- 本轮读到过金币的号（按 charKey 排好序，UI 用它说清"上面那个钱是谁读的"）。
--- 整表一次扫角色行：只有悬停和导出会问，渲染循环里不许逐条调（同 ManualMoneyByQuest
--- 的理由 —— 14 个号 × 每行一次会把重绘拖成卡顿）。
function L.GoldReaders(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local at = L.RoundAt(db, rec)
    local out
    for charKey, row in pairs(db.chars or {}) do
        local r = row and row.reads and row.reads[questID]
        local t = r and tonumber(r.t)
        local g = r and tonumber(r.g)
        if t and g and g > 0 and t >= at then
            out = out or {}
            out[#out + 1] = charKey
        end
    end
    if not out then return nil end
    table.sort(out)
    return out
end

--------------------------------------------------------------------
-- 周常家族：一族 = 同一件周事的多个 questID（轮换任务每周换 ID、换地图）。
-- 数据由调用方（Sync 经 Api）问好后传进来，本模块只负责落账 ——
-- 保持"纯账本逻辑、可离线跑"的边界。
-- 矩阵里**一族只占一行**，由内置表声明的那条 ID 占（L.OccupiesRow / FamilyAnchorID）；
-- 板上轮换出来的同名子任务只当证据：族里任意一条本周做过，整族都算已完成。
--------------------------------------------------------------------

--- 落一条注册周常。entries = { { key, questID, name, mapID, mapSrc, money, state, onBoard }, ... }
--- 与 AddQuest 的区别刻意为之：
---   · 记 regKey（族身份/常驻周常身份，见上面），不记 kwLogAdded（那是关键词从日志
---     收进来的那道门；manualAdded 一线已随字段下线消失，见 AddQuest）；
---   · 到期只按 CD 尾巴写（SetExpiry），板面实例的轮换倒计时不算它的寿命；
---   · lastSeenAt 每轮续期，Prune 的三周时清规则永远碰不到它。
--- state=nil（完成接口问不出）时**不写视图**：留空 = "问不出"，写进去就变成"这周没做"。
function L.EnsureRegistered(db, entries, charKey, now)
    if not db or type(entries) ~= "table" then return 0 end
    db.quests = db.quests or {}
    local touched = 0
    for i = 1, #entries do
        local e = entries[i]
        local questID = tonumber(e and e.questID)
        if questID and questID > 0 and e.key then
            local rec = db.quests[questID]
            if not rec then
                rec = L.NewQuestRecord()
                db.quests[questID] = rec
            end
            rec.regKey = e.key
            -- 玩家点名屏蔽过就别再把他点回来：账照记、行不列
            -- （永久屏蔽，换 CD、换轮都不作废，见 RemoveQuest）
            if type(e.name) == "string" and e.name ~= "" then rec.name = e.name end
            if e.mapID and (not rec.mapID or rec.mapID ~= e.mapID) then
                -- 族锚点的地图是"这一周这族在哪个区"的**指针**（12.1.5 实测：
                -- 上周 GetMapForQuestPOIs(89507)=2393 银月城，本周答 2437 祖阿曼，
                -- 与游戏自带的地图气泡完全一致）。上一版只在"没有地图"时写一次，
                -- 结果换区之后账本还挂着上周那张图，补扫也就永远扫不到本周这条
                -- （用户："属于祖阿曼地图，而我们的任务…属于银月城，不知道怎么回事"）。
                -- 这条就是矩阵那一行顶着的地图（族本体占行，见 OccupiesRow）。
                -- 它仍然只是"这一周这族在哪个区"的指针：跟踪地图清单里真扫到同族子任务时，
                -- ScanBoards 会把板上那张图写过来覆盖它。
                rec.mapID, rec.mapSrc = e.mapID, e.mapSrc
            end
            local money = tonumber(e.money)
            if money then rec.lastMoney = money end
            local sig = L.MakeSig(money, nil)
            local obs = rec.observations[sig]
            if obs then
                obs.lastSeen = now
            else
                rec.observations[sig] = {
                    gold = (money and money > 0) or false,
                    money = money,
                    items = nil,
                    firstSeen = now,
                    lastSeen = now,
                }
                TrimObservations(rec, sig)
            end
            rec.currentSig = sig
            rec.lastSeenAt = now
            -- "在不在板上"只由调用方说：池里有 / 跟踪地图清单里扫到才传 true。
            -- 没有板面证据的锚点不成行（用户 2026-10-23 定的模型：行=子任务，各回各的图、
            -- 各带自己的倒计时；锚点只当"这族存在 + 这个号本周做过没有"的证据载体）。
            if e.onBoard then rec.onBoard = true end
            -- 到期口径走 SetExpiry：注册行是常驻周常，只认 CD 尾巴。
            -- 这里以前直接把拿着 ID 问出来的倒计时写成到期 —— 那条倒计时是**板上实例**的
            -- 轮换寿命（几小时），于是 加固符文石 这种注册周常当天下午就从矩阵消失。
            L.SetExpiry(rec, now, e.remaining)
            if e.state then L.SetView(db, charKey, questID, e.state, now) end
            touched = touched + 1
        end
    end
    return touched
end

--- 账本里所有带 regKey 的记录（内置预设 + 关键词补扫注册的），{ questID, key } 列表。
--- Sync 每轮快照按这份重问状态：补扫只在那一轮建过账，不照这份回问的话
--- "虚空侵袭"这行的红勾会永远停在补扫当天。ignored 的跳过（玩家永久屏蔽掉的这行，
--- 接口也不用再问一遍 —— 屏蔽只有玩家在"添加任务"里点回来才会撤）。
function L.RegisteredIndex(db)
    local out = {}
    for questID, rec in pairs((db and db.quests) or {}) do
        if type(rec) == "table" and rec.regKey and not rec.ignored then
            out[#out + 1] = { questID = questID, key = rec.regKey }
        end
    end
    return out
end

--- 关键词删掉 → 退掉由它注册的行（regKey = "kw:关键词"）。
--- 只清 regKey、不删记录：名字与地图是问一遍客户端才有的，玩家马上改回来不必重问。
--- 清完这行回到普通待遇，"不在池里 → 消失"那趟扫描会把它判掉，矩阵自然退出。
--- 内置预设的 key 不带 kw: 前缀，删关键词拆不到丰饶供品那一行。
function L.UnregisterByKey(db, key)
    if not db or not db.quests or not key then return 0 end
    local n = 0
    for _, rec in pairs(db.quests) do
        if type(rec) == "table" and rec.regKey == key then
            rec.regKey = nil
            n = n + 1
        end
    end
    return n
end

--- 注册行的角色状态：只认"本 CD 内写下的证据"。
--- 视图早于 cycleAnchor 说明那是上个 CD 的快照 —— 周常的完成标志是引擎按周清的，
--- 但我们存的那一笔不是，不拦就会拿上周的"已做"冒充本周（最坏的一种谎报）。
--- 角色本 CD 压根没上线同步过 → unknown，UI 显示 ?，绝不当成"没做"。
--- 锚点与时间戳分属两个时钟域时同样回 unknown：口径与 WeekStats 一致
--- （那边报 -1 让 UI 显示 ?，宁可不报也不报一个看着正常的假数）。
local function ThisCycle(db, t)
    local anchor = tonumber(db and db.cycleAnchor) or 0
    if anchor <= 0 then return true end
    t = tonumber(t)
    if not t then return false end
    if anchor > 1e9 and db.clockDomain == "uptime" then return false end
    return t >= anchor
end

--- 注册行读的是"本周 CD 内"的证据；判据与优先级见 L.State（唯一真源）。
--- 上面那段说明（视图早于 cycleAnchor = 上个 CD 的快照、本角色没同步过 → unknown、
--- 时钟域分裂 → unknown）现在全在 L.State 的 gate == "cycle" 分支里，一份代码两处读。
function L.RegisteredState(db, charKey, questID)
    return L.State(db, charKey, questID, "cycle")
end
--- 同一族里多个 questID 取最强的一个：做过 > 在做 > 可做 > 问不出。
--- "任一 ID 答得出 done"就算 done —— 轮换任务本周换了 ID，老 ID 仍可能回 true。
local STATE_RANK = { done = 4, progress = 3, open = 2, ended = 1, unknown = 0 }

function L.BestRegisteredState(db, charKey, ids)
    local best, bestRank, bestSrc = "unknown", 0, nil
    for i = 1, #(ids or {}) do
        local st, src = L.RegisteredState(db, charKey, tonumber(ids[i]))
        local rank = STATE_RANK[st] or 0
        if rank > bestRank then best, bestRank, bestSrc = st, rank, src end
    end
    return best, bestSrc
end

--- 这条任务有没有"可信身份"：上过板（池/清单证据）、玩家点名要过（注册族、关键词、
--- 屏蔽管理里点"忽略"）。交任务记账只认这几条——QUEST_COMPLETE 的第一参数是任何任务，
--- 无门收录等于给矩阵造一条"本周交过"的假凭据，那比少记一笔严重得多。
--- `rec.manualAdded` 这一支随字段下线删了（2026-10-27）：只靠点矩阵 pin 建出来、
--- 客户端一个字都没答过的任务，交任务不再记账 —— 符合"客户端答得出就按客户端、
--- 答不出就如实降级"。`ignored` 留着：玩家明确表过态的行，账还是要记（金币汇总不许
--- 因为藏了一行就凭空少一笔）。
function L.QuestKnown(db, questID)
    local rec = db and db.quests and db.quests[tonumber(questID)]
    if not rec then return false end
    return not not (rec.poolSeen or rec.onBoard or rec.regKey
        or rec.kwLogAdded or rec.ignored)
end

--- 清掉"账本里查不到可信身份"的交任务凭据（一次性升级 v3 用）。
--- Task 2 之前交一条职业/战役任务也会写进 turnins，而这份凭据是矩阵准入门之一
--- ——它能绕过金币门槛，把一条从没上过板的任务永远留在矩阵里。
--- 只清凭据不清任务记录：清掉的代价是那一行少报一次，下一次同步真上板就自己长回来；
--- 记录本身可能带着玩家几个 CD 攒下来的金币观察，删了就回不来了。
function L.DropTurninsWithoutEvidence(db)
    if not db or not db.turnins then return 0 end
    local n = 0
    for _, log in pairs(db.turnins) do
        if type(log) == "table" then
            for questID in pairs(log) do
                if not L.QuestKnown(db, questID) then
                    log[questID] = nil
                    n = n + 1
                end
            end
        end
    end
    return n
end

--- 状态判定唯一入口。gate 只决定"哪道新鲜度门"，优先级两边完全一样：
---   "round" = 世界任务，跟这一条实例自己的起点（L.RoundAt）比
---   "cycle" = 常驻周常，跟本周 CD 锚点（db.cycleAnchor）比
--- 优先级：本轮/本周内的手动补录 > 本轮/本周实测交任务 > 本角色登录快照 > 兜底。
--- 上一版这两份各写一遍，改一处忘一处（last.t 空值缺陷只在一半里）。
--- "unknown" 只在完全没有来源时出现，UI 必须如实显示，不得当成"没做"。
--- 注意 currentSig / 池成员关系只描述"最后一次同步的那个角色"看到的池：世界任务池
--- 是按角色各自的（狩猎模式、个人任务线都会改变它）。所以 currentSig 为空绝不等于
--- "本角色的这个任务结束了"，本角色说得出话时一律以它自己的快照为准。
--- "算不算本轮"只跟 L.RoundAt（这一条实例自己的起点）比，绝不跟奖励签名比：
--- 切一次狩猎模式就翻一次签名，12.1.5 反馈"做过这个任务的，现在标记成了没做过"
--- 就是这么来的（用户 2026-10-25：这个任务并没有结束，并不是新一轮的刷新）。
--- 第二个返回值 = **这一格的答案是谁给的**：玩家手点的补录回 "manual"，点的时候客户端
--- 自己答了已完成回 "verified"，其余（实测流水 / 快照 / 池推断）一律 nil。
--- 界面靠它给手动格换颜色（用户 2026-10-25："手动补录这个，换个 √ 的颜色，
--- 一目了然哪个是手动的"）—— 只有这一处判据，别在显示层再翻一遍 db.manual。
function L.State(db, charKey, questID, gate)
    local byCycle = gate == "cycle"
    -- 补录这一格两道门都要过（注册行按 CD 门、世界任务行按轮次门，见下面）；
    -- 缺 db.manual = 没有这条来源，不是当场抛错的理由（上一版只有注册行判了这一层）。
    local manual = db.manual and db.manual[charKey]
    local m = manual and manual[questID]
    local hasM = m ~= nil
    -- 补录也分轮次（用户 2026-10-27："手动标记也是要分轮次的，这个勾不补不代表
    -- 永远完成了，世界任务都是不断轮换的"）。12.1.5 实测：89347「战胜未知：怒火攻心的漂流者」
    -- 屠戮者高地 12 小时区，10-26 14:01 换轮（roundAt 已推进、到期 10-27 01:40），
    -- 可 10-25 04:22 点的那笔补录还带着 done —— 本轮一格没勾，界面照样打勾。
    -- 上一版写的是"世界任务行不设时间门（他要的就是这一条一直算数）"，那句在
    -- 轮次口径定下来之前成立，现在不成立了：玩家要的是"这一条算数"，不是"永远算数"。
    -- 注册行照旧按 CD 门（常驻周常本轮就是整个 CD，两个门是同一件事）。
    local manualFresh = hasM and (byCycle and ThisCycle(db, m.t)
        or ((tonumber(m.t) or 0) >= L.RoundAt(db, db.quests and db.quests[questID])))
    if manualFresh then
        return m.state, m.v and "verified" or "manual"
    end

    local rec = db.quests and db.quests[questID]
    if not rec then return "unknown" end
    local obs = L.GetObs(db, questID)
    local at = L.RoundAt(db, rec)

    local rows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    -- 老存档里的交任务记录可能压根没写 t（L.WeekStats 自己就写着 if r.t and ...），
    -- 拿 nil 跟起点比大小是当场抛错；取不到 t 就当这条不是凭据 —— 两道门同一口径：
    -- 说不出它属于哪一轮/哪一周的流水，绝不许冒充"实测交过"（注册行以前靠
    -- ThisCycle 挡，可锚点定不出来时 cycleAnchor 就是 0，那条门一进门就放行）。
    local lastT = last and tonumber(last.t)
    local fresh
    if byCycle then fresh = lastT ~= nil and ThisCycle(db, lastT)
    else fresh = lastT ~= nil and lastT >= at end
    -- 世界任务行要求"这一条此刻还在池里"（obs）才算实测交任务；
    -- 注册行不要求：锚点那条本来就不靠板面存在。
    if fresh and (byCycle or obs) then return "done" end

    local chars = db.chars and db.chars[charKey]
    local view = chars and chars.views and chars.views[questID]
    local viewT = view and tonumber(view.t)
    if view then
        if byCycle then
            if viewT and ThisCycle(db, viewT) then return view.s end
        elseif not obs or (viewT and viewT >= at) then
            -- 快照只在覆盖到当前这一条实例时才算数，否则上一轮的"已交"会跨轮冒充本轮
            return view.s
        end
    end
    if byCycle then return "unknown" end
    -- 玩家点过补录、可它过期了，而客户端这一轮**什么凭据都没答过**（不在池里、
    -- 本轮没有实测流水、没有本轮的快照）：报"没做"和报"永远已做"是同一个错的两侧 ——
    -- 这一格如实就是问不出（UI 显示 ?），玩家照着 ? 再点一次就补上。
    -- 只要本轮有任何一份凭据在，就照那份走（过期补录不许压住本轮真交过的那一笔）。
    if hasM and not obs and not fresh
        and not (viewT and viewT >= at) then
        return "unknown"
    end
    return obs and "open" or "ended"
end

--- 本 CD 汇总。手动已交若与实测交任务是同一条就不重复计数；
--- 有完成记录但一条实测金币都没有时 gold 返回 -1，UI 显示 "?"，绝不显示 0。
--- count 返回 -1 表示本周统计整体不可信（见下面域分裂判断），UI 同样显示 "?"。
--- cycleStart 传 0 是"不加过滤"（Ledger 不判断锚点是否存在，那是调用方的事，见 Dump）。
function L.WeekStats(db, charKey, cycleStart)
    -- 锚点落在纪元秒、事件时间戳落在"本次开机秒"时两者不可比：照大小过滤会把
    -- 每条记录都判成本周之外，汇总静默变成"完成 0"。宁可报未知，也不报一个看着正常的假数。
    if cycleStart and cycleStart > 1e9 and db.clockDomain == "uptime" then
        return -1, -1
    end
    local count, gold, goldKnown = 0, 0, false
    local counted = {}
    for questID, rows in pairs(db.turnins[charKey] or {}) do
        for i = 1, #rows do
            local r = rows[i]
            if r.t and (not cycleStart or r.t >= cycleStart) then
                count = count + 1
                counted[questID] = true
                if type(r.g) == "number" then
                    gold = gold + r.g
                    goldKnown = true
                end
            end
        end
    end
    for questID, m in pairs(db.manual[charKey] or {}) do
        if m.state == "done" and not counted[questID]
            and (not cycleStart or (m.t or 0) >= cycleStart) then
            -- 世界任务行的补录跟格子的显示同一道轮次门（见 L.State）：占行与统计
            -- 不许两处两个口径，否则"完成 8"里混着上一轮那笔没补的勾。
            -- 注册行（rec.regKey）不设这道门 —— 常驻周常本轮就是整个 CD。
            local rec = (db.quests or {})[questID]
            local stale = rec and not rec.regKey
                and (tonumber(m.t) or 0) < L.RoundAt(db, rec)
            if not stale then
                count = count + 1
                if type(m.g) == "number" then
                    gold = gold + m.g
                    goldKnown = true
                end
            end
        end
    end
    return count, (count > 0 and not goldKnown) and -1 or gold
end

function L.MarkGoldByObservation(db, questID, sig)
    local rec = db.quests[questID]
    if not rec or rec.goldLock or not sig then return false end
    local obs = rec.observations[sig]
    if not obs then return false end
    obs.gold = true
    return true
end

function L.SetGoldLock(db, questID, locked)
    local rec = db.quests[questID]
    if not rec then return false end
    rec.goldLock = locked or nil
    return true
end

function L.GoldQuestIDs(db)
    local out = {}
    for questID in pairs(db.quests or {}) do
        -- 走 GoldVerdict，不各写一份"只看 obs.gold"的判据：本轮另一档读到过金币的
        -- 双模式任务，矩阵里有行、导出里却没有，两处报两个数最难查。
        if L.GoldVerdict(db, questID) == true then out[#out + 1] = questID end
    end
    table.sort(out, function(a, b)
        return (db.quests[a].name or "") < (db.quests[b].name or "")
    end)
    return out
end

function L.Characters(db)
    local out = {}
    for charKey in pairs(db.chars or {}) do out[#out + 1] = charKey end
    table.sort(out)
    return out
end

function L.UndoApply(db, entry)
    if not entry then return false end
    if entry.kind == "manual" then
        if entry.old then
            db.manual[entry.charKey] = db.manual[entry.charKey] or {}
            db.manual[entry.charKey][entry.questID] = entry.old
        else
            L.ClearManual(db, entry.charKey, entry.questID)
        end
        return true
    elseif entry.kind == "turnin" then
        local rows = db.turnins[entry.charKey] and db.turnins[entry.charKey][entry.questID]
        if rows and #rows > 0 then table.remove(rows, #rows) end
        return true
    end
    return false
end

--- 控制 SavedVariables 体积：陈旧任务整条清出、轮数与观察数封顶。金币历史不删。
--- 判据是"距最后一次在池里出现"，不是"距 CD 起点"：周常任务本 CD 没刷新不代表过时。
function L.Prune(db, now)
    -- now 与 rec.lastSeenAt 必须同域才可比：上个存档留的纪元秒对上本次开机秒，
    -- 差值几十亿，一次快照就能把整张账本清光。同 ThisCycle / WeekStats 那两道守卫。
    -- 这里不猜两个域之间的换算桥（猜错了就把好好的任务抹了），只认"量级对不对得上"：
    -- 纪元秒恒 >1e9（1e9=2001 年）、开机秒是个几百到几千的小数，两者判据一致才算同域。
    local nowN = tonumber(now) or 0
    local nowEpoch = nowN > 1e9
    for questID, rec in pairs(db.quests or {}) do
        -- 被玩家点名屏蔽过的记录不许按时清掉：清掉就等于把"别再显示"的意图一起丢了，
        -- 三周后这行会自己刷回来（每条只有几个字段，量级是"用户手动删过几条"）。
        -- **永久屏蔽 = 记录永久保留，这是有意的**：屏蔽的语义本轮改成永久
        -- （见 RemoveQuest），按时清这条记录等于每三周替玩家撤销一次他的屏蔽 ——
        -- 撤销只能由他自己在屏蔽管理窗口的右栏点回来做。矩阵那一行不列、金币账照旧（Prune
        -- 只管体积），所以留着一条只有几个字段的记录不欠任何人一个解释。
        -- 注册周常也不按时清：它本来就不进世界任务池，lastSeenAt 只在矩阵打开时
        -- 被 EnsureRegistered 续期，三周不开面板就把用户点名要的任务删掉不合理
        -- （删了还要重新问一遍标题/地图接口才能刷回来）
        local seen = tonumber(rec.lastSeenAt)
        local comparable = seen ~= nil and ((seen > 1e9) == nowEpoch)
        if not rec.ignored and not rec.regKey and comparable
            and nowN - seen > 3 * WEEK then
            db.quests[questID] = nil
        end
    end
    for _, rec in pairs(db.quests or {}) do
        if rec.observations then TrimObservations(rec, rec.currentSig) end
    end
    for _, rows in pairs(db.turnins or {}) do
        for _, list in pairs(rows) do
            while #list > MAX_TURNINS_PER_QUEST do table.remove(list, 1) end
        end
    end
end

--------------------------------------------------------------------
-- 矩阵视图（spec 8.1）：把账本摊成"行=任务、列=角色"。纯数据，不碰任何 WoW 接口，
-- 地图名之类需要接口的东西由调用方（Matrix.lua 经 Api）补。
--------------------------------------------------------------------

local STALE_SECONDS = 7 * 86400

function L.QuestMoney(db, questID)
    local rec = db and db.quests and db.quests[questID]
    local obs = L.GetObs(db, questID)
    local m = obs and obs.money
    if type(m) == "number" and m > 0 then return m end
    -- 双模式的任务：普通模式给金币、狩猎模式换成物品（或反过来）。这一轮里任意一档
    -- 读到过金币就一直按金币算，切档读到"不给金币"不许把行弄掉
    -- （用户 2026-10-25："之前是金币，那么也不应该移除（结束时间没变）"）。
    -- 换了轮这份粘滞作废，新实例重新读、重新判 —— 上个 CD 的金币赖着不走（意外遭遇）
    -- 走的就是这一条退出路，不是"当场读到非金币就删行"。
    local at = L.RoundAt(db, rec)
    if rec and rec.goldMoney and rec.goldRound == at then return rec.goldMoney end
    return m
end

--- "这条给不给金币"的唯一口径。返回 true=本轮读到过金币（含双模式另一档的粘滞，
--- 见 L.QuestMoney），false=客户端明确答过非金币，nil=压根问不出（UI 显示 ?）。
--- 绝不把 nil 折成 false：那等于替客户端回答"它不给钱"，行会当场消失，
--- 而它只是这一轮数据没到（12.x 奖励数据要几十秒才回来）。
function L.GoldVerdict(db, questID)
    local money = L.QuestMoney(db, questID)
    if type(money) == "number" and money > 0 then return true end
    -- 本轮读到的金额是个数、却是 0 = 客户端明说这一档不给金币 ⇒ 否（第 35 轮）。
    -- 认这个数，不认 obs.gold 那面旗：同一个事实在存档里有两份写法（直接读到 0 那一支
    -- 当年折成 nil、签名 0 新建那一支写成 false），旗子中不中全看这条记录当初走哪支建的。
    -- 判据挂在旗上，12.1.5 实测沼地威胁 91803 部署后照样赖在板上 —— 正是"改完没变化"那个形状。
    if money == 0 then return false end
    local obs = L.GetObs(db, questID)
    -- 交任务实测把未判定那条转正过（L.MarkGoldByObservation）：金额接口问不出，
    -- 可"玩家亲手拿金币换过它"比接口答的金额更硬。漏这一笔，导出的金币节会把
    -- dump 里本来就有的那条判没 —— 又是两处报两个数。
    if obs and obs.gold == true then return true end
    if obs and obs.gold == false then return false end
    -- 只剩"上一轮读到过金币、本轮还没读到"这一种：L.QuestMoney 已经按轮次拒了那份
    -- 粘滞，所以这里不会翻回"是"；也不许报"否" —— 客户端本轮根本没说它不给钱，
    -- 折成 false 等于替它答，行会当场消失（UI 该显示 ?）。
    return nil
end

--- 界面要分清话是谁说的：这笔金币是当前这一读到的，还是本轮另一档读到的。
--- 现在这一档不给金币却还按金币占行时返回金额，否则返回 nil。
function L.GoldFromOtherMode(db, questID)
    local rec = db and db.quests and db.quests[questID]
    local obs = L.GetObs(db, questID)
    local m = obs and obs.money
    if type(m) == "number" and m > 0 then return nil end
    if rec and rec.goldMoney and rec.goldRound == L.RoundAt(db, rec) then
        return rec.goldMoney
    end
end

--- 矩阵门槛：默认只展示 ≥500 金的任务（12.1.5 第 15 轮反馈"出来了一堆狩猎的 >100g 的已做
--- 任务，门槛就要提到 500g 或者可以配置" —— 写死一个数等于替 14 个小号决定什么值得刷）。
--- 真源是设置面板里那条 `db.config.minGold`，下面这份常量只是 Config 没加载时的兜底；
--- 判门槛一律走 L.MatrixMoneyFloor()，**Dump/Probe 也走它**：门槛一改，
--- "这条为什么没进矩阵"的取证必须跟着变，抄一份数字的报告会指着旧门槛说瞎话。
--- 1 金 = 100 银 = 10000 铜。
local MIN_MATRIX_MONEY = 500 * 10000
function L.MatrixMoneyFloor()
    return (GQL.Config and GQL.Config.MinMatrixMoney and GQL.Config.MinMatrixMoney())
        or MIN_MATRIX_MONEY
end

--- 本轮有没有任一角色被客户端答成"已完成"（= 矩阵那一行里那些 ✓ 格）。命中任一即算。
--- 只认客户端答的那几路（池/注册行的状态读、交任务实测流水），**不认玩家手点的补录**：
--- 补录在 `db.manual` 里、`L.State` 会标 "manual"，这一处根本不读那张表 ——
--- 与"玩家口述不算凭据"同一条口径。
--- 为什么必须补这一条（用户 2026-10-27 的反弹）：96718 奈格塔尔虚空决战（英雄）真存在、
--- 他 14 个号本轮都交掉了（存档里 14 格 done 快照，时刻全在本轮起点之后），
--- 可它的上板凭据只有日志那一道 `kwLogAdded`，而那道标记的设计是"还挂在任务日志里才每轮续期"
--- —— 交完就离开日志 → 下一轮标记被撤 → 零凭据 → 行消失。**"做掉了"反而把凭据做没了。**
--- 两道边界把它压成"只在当轮有效"，都不靠新增接口调用：
---   * 本轮起点：`L.RoundAt` 一往前推（`RollRound` 只由池换轮 / 结束时间变化写）这份证据立刻闭嘴，
---     所以这一条不能把上一轮的勾补成本周做完（用例的对照组一钉住它）；
---   * 这条记录自己的到期门：不在池里的记录没人替它推 `roundAt`，真正让行消失的是
---     `expiresAt`（`SetCycleExpiry` 给的下次周重置尾巴）—— 12.1.5 真存档重放实测：
---     96718 在 10-29 06:59 那一格随到期掉出矩阵，此刻这条凭据**仍答 true**。
--- 所以"上上世代那种没人碰过的垃圾行"不会因为这条活过一轮：82157/86174 实测 `done=0`
--- （只有 progress 视图），本来就不命中；而真命中过的行最长寿到自己的倒计时为止。
function L.DoneByAnyCharThisRound(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    local at = L.RoundAt(db, rec)
    for _, row in pairs(db.chars or {}) do
        local view = row.views and row.views[questID]
        local t = view and tonumber(view.t)
        if view and view.s == "done" and t ~= nil and t >= at then return true end
    end
    return false
end

--- 这条记录有没有"客户端今天还答得出它"的凭据（命中任一即算）。第 32 轮准入门的唯一真源：
--- 名字撞关键词只是玩家说"这个名字我要看"，不构成"这条任务还真存在"——12.1.5 实测
--- 82157 特别任务：巨兽崛起 / 86174 特别任务：风暴在即 两条上上世代的垃圾行就是靠 kwHit
--- 一张名字通行证挂在矩阵里，还顶着
--- SetCycleExpiry 给的"下次周重置"倒计时（计划 §2.1）。
--- 凭据集合全部是账本里已有的字段，零新增接口调用：进过世界任务池（poolSeen）/
--- 关键词+任务日志收进账本（kwLogAdded）/ 注册过周常档（regKey）/ 读到过奖励金币
--- （lastMoney 是数——含 0：客户端答过"这条不给金币"也是答过）/ 客户端自己答的图
--- （mapID 且非玩家口述）/ **本轮任一角色被客户端答成已完成**（L.DoneByAnyCharThisRound）。
--- **玩家口述的地图（mapManual / mapSrc=玩家指定）不算**：那是"在哪办"的备注，
--- 不是"这条任务还真存在"的证据（12.1.5 实测：93013/94391/94795/96307 四条旧特别任务
--- 就是靠 mapSrc=玩家指定 + 名字撞关键词赖在界面上，计划 §2.2）。
function L.QuestHasCredential(db, questID)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    if rec.poolSeen or rec.kwLogAdded or rec.regKey then return true end
    if type(rec.lastMoney) == "number" then return true end
    if rec.mapID and not rec.mapManual then return true end
    return L.DoneByAnyCharThisRound(db, questID)
end

--- 手动补录记的金币（任何角色补录过都算），一次收成 questID → 金额 的表。
--- 只剩手动记录的任务没有 observations，金额只存在于 db.manual[charKey][questID].g ——
--- 不从这捞，补录过的任务会被金币门槛误滤掉（补录的记录不能凭空蒸发）。
--- 必须整表算一次：MatrixRows 拿它当排序键，逐次现扫会把 O(n log n) 次
--- "遍历所有角色"塞进比较函数里（角色一多每次重绘都卡一下）。
local function ManualMoneyByQuest(db)
    local out = {}
    local manual = db.manual or {}
    for _, rec in pairs(manual) do
        for questID, m in pairs(rec) do
            if type(m) == "table" and type(m.g) == "number" and out[questID] == nil then
                out[questID] = m.g
            end
        end
    end
    return out
end

-- 导出给 /gql quest：报告要拿与矩阵**同一条** money 口径复判准入门。少这份手动补录的数，
-- "靠补录金币占行"的那条会被误报成"准入门没过"——又是一句谎报，正是本轮要消灭的那类。
L.ManualMoneyByQuest = ManualMoneyByQuest

--- 任一角色**这一轮**交过这条 = 玩家亲手拿金币换过它，比任何接口答的金额都硬。
--- 12.1.5 反馈④"松露大乱斗只有在狩猎模式下看才是金币任务，所以当一个角色完成一个符合条件
--- 的金币任务时，至少要加入矩阵"：那条的 962 金只有鼠标悬停在地图上看得到，接口侧
--- GetQuestLogRewardMoney 回 0、HaveQuestRewardData=false、另两个候选入口 MISSING ——
--- 金额这道闸门永远判不出它，于是"做过的角色连行都看不见"。
--- 这道门从这一版起按轮次收口，不再按 CD（用户 2026-10-25 对着 89267「神秘身影」定的：
--- 换了一条全新实例、14 个号本轮一格没勾，行还靠上一轮的已交赖着）。两道门必须同一口径：
--- 格子里的 ✓ 早就是轮次口径（见 L.State），占行却数本 CD 会出现"全空的一行"。
--- 周常不受影响：注册周常的本轮就是整个 CD（它中途不换实例），周初交过照样占行。
--- 第 25 轮起这道门故意不看金额；第 35 轮用户把口子收窄成"接和交两处客户端都答了 0 金币"
--- 才不放行，任何一处答不出照旧放行。放不放行一律问 L.TurnInAdmits（唯一真源），这里只把
--- "本轮交过"（on）与"本轮那几笔到手金额"（gain）从同一次遍历里聚出来，别处不许再翻账本。
--- gain 的优先级三条一条都不能反（反一条就等于"一个号没量到钱"替全队否决这一行）：
---   任一笔 g>0 → 那个正数（本来就该在列，不能被别号的 0/nil 顶掉）；
---   否则有一笔 g 不是数字 → nil（判不了——窗口被邮箱/拍卖污染时 Sync 传的就是 nil，
---     当成 0 会把松露大乱斗那种读数失败直接掉板）；
---   否则本轮每笔都是数字 0 → 0（"接和交都答 0"才可能否决的唯一一档）。
--- on 的语义与第 25 轮那份逐字相同，只是不再提前 break：占行要扫全轮才聚得出金额。
local function TurnInCred(db)
    local on, gain = {}, {}
    local pos, unknown = {}, {}
    local cycle = tonumber(db.cycleAnchor) or 0
    for _, byQuest in pairs(db.turnins or {}) do
        if type(byQuest) == "table" then
            for questID, rows in pairs(byQuest) do
                if type(rows) == "table" then
                    local at = L.RoundAt(db, (db.quests or {})[questID])
                    for i = 1, #rows do
                        local r = rows[i]
                        if type(r) == "table"
                            and (tonumber(r.cycle) or 0) == cycle
                            and (tonumber(r.t) or 0) >= at then
                            on[questID] = true
                            local g = r.g
                            if type(g) == "number" and g > 0 then
                                if not pos[questID] then pos[questID] = g end
                            elseif type(g) ~= "number" then
                                unknown[questID] = true
                            end
                        end
                    end
                end
            end
        end
    end
    for questID in pairs(on) do
        if pos[questID] then
            gain[questID] = pos[questID]
        elseif not unknown[questID] then
            gain[questID] = 0
        end
    end
    return { on = on, gain = gain }
end

--- 同一件事的 CD 口径，只给取证用：报告要分得清"本轮交过所以占行"和
--- "本 CD 早前那几轮交过、本轮没交 ⇒ 已经不占行"，后者正是 12.1.5 截图那一条的形状。
local function TurnedInThisCycle(db)
    local out = {}
    local cycle = tonumber(db.cycleAnchor) or 0
    for _, byQuest in pairs(db.turnins or {}) do
        if type(byQuest) == "table" then
            for questID, rows in pairs(byQuest) do
                if type(rows) == "table" and not out[questID] then
                    for i = 1, #rows do
                        if type(rows[i]) == "table"
                            and (tonumber(rows[i].cycle) or 0) == cycle then
                            out[questID] = true
                            break
                        end
                    end
                end
            end
        end
    end
    return out
end

-- 导出给 /gql quest 用：诊断"这条为什么在列/不在列"必须复判同一道门，
-- 自己数一遍 db.turnins 的报告会在门槛判定改口径那天开始说瞎话。
-- 两个口径都导出：占行只认轮次那份，CD 那份留给报告解释"本 CD 交过但本轮没交"。
-- TurnedInThisRound 只是 TurnInCred 的 .on 薄壳：返回形状（{[questID]=true}）一字不许变，
-- Dump、Options、用例都在读它；金额那份另开 L.TurnInGainsThisRound，同一份表同一趟遍历。
L.TurnedInThisRound = function(db) return TurnInCred(db).on end
L.TurnInGainsThisRound = function(db) return TurnInCred(db).gain end
L.TurnedInThisCycle = TurnedInThisCycle

--- 交过这一道放不放行（第 35 轮）。矩阵准入门与 Dump 的"在列靠"都问它，别处不许再拼。
--- on=本轮有号交过；gain=本轮那几笔的到手金额（0=每笔都答了没拿到钱，nil=有一笔答不出）；
--- noGold=客户端这一轮明说过它不给金币。
--- 两处都答 0 才否决：用户 2026-10-27"接任务金币是0，交任务金币还是0…那不能上"。
--- 任何一处答不出都放行 —— 松露大乱斗那种"做了金币任务却读不到钱"正是要保的样本。
function L.TurnInAdmits(on, gain, noGold)
    if not on then return false end
    return not (noGold and gain == 0)
end

--- 一次性作废老存档里被写坏的"本轮读到过金币"（`goldMoney/goldRound`）。
--- 危害不是"新数据还会坏"——换轮清复用记录、延续只在本轮之内，那两处已经堵住了；
--- 而是**已经写进去的那一份不会自愈**：粘滞只在换轮时作废，玩家得等下一次 12 小时刷新
--- 才看得见修法生效（12.1.5 实测"代码改完、矩阵一点变化没有"，因为 15:02 那次同步
--- 写进去的 1075 金一路答到 03:00）。
--- 上一版只清粘滞这一层，15:50 那次同步又把它原样造了回来：旧代码把上一轮的金额搬进了
--- `-1` 那份 observation（`-1` = MakeSig 给"金额压根没读出来"编的占位，客户端从没为它
--- 答过一句话，所以那里面任何数字都是别处搬来的），DiffPool 结尾 `cur.money > 0` 照着它
--- 重新写一遍 goldMoney。清不到来源等于没清（用例照 89267 真存档的形状造）。
--- 真答过的签名一律不动：`0` 是客户端明说"这一档不给金币"，正数是它真读出来的金额。
--- 清掉的代价是少报不是多报：双模式任务这一轮可能掉一次行，下一次同步真读到金币那档
--- 就自己长回来（假金币会让玩家照它安排刷一轮，比晚一行严重得多）。
--- 标记分档：上一版只写了"清粘滞"这一半，存档里已经留着 stickyGoldReset=1 ——
--- 再拿"非空即跳过"当闸门，这一版就永远轮不到清来源，所以清完写 2、只认 2 以上跳过。
function L.ResetStickyGoldOnce(db)
    if not db or (tonumber(db.stickyGoldReset) or 0) >= 2 then return 0 end
    local n = 0
    for _, rec in pairs(db.quests or {}) do
        if type(rec) == "table" then
            local dirty = false
            if rec.goldMoney or rec.goldRound then
                rec.goldMoney, rec.goldRound = nil, nil
                dirty = true
            end
            for sig, obs in pairs(rec.observations or {}) do
                if type(obs) == "table" and UnreadMoneySig(sig)
                    and (obs.money ~= nil or obs.gold ~= nil) then
                    obs.money, obs.gold = nil, nil
                    dirty = true
                end
            end
            if dirty then n = n + 1 end
        end
    end
    db.stickyGoldReset = 2
    return n
end

--- 一次性作废老存档里的"玩家手动添加"标记（schema v4）。
--- 旧行为：manualAdded 点一次就永久有效，跨轮、换 CD 都不作废 —— 玩家上个 CD 为了
--- 看一眼状态点过添加的非金币任务，之后每次刷新都还占着一行（12.1.5 真存档 88679
--- 「意外遭遇」：本轮 currentSig = "0" 客户端明说不给金币、本轮各号都没交过，
--- 行还在列唯一原因就是它）。
--- 判据已经改成"本轮点过才算"（RollRound 清），但**已经写进存档的那一份不会自愈**：
--- 换轮只在"这一条实例换了一条"那一刻发生，不清就得等下一次 12 小时刷新才看得见效果，
--- 玩家会当成"改了没生效"二次报修（上一版 ResetStickyGoldOnce 就是这么栽的）。
--- 只清标记，不替准入门判活：本轮读到过金币的、命中关键词的、注册周常、本 CD 交过的
--- 照旧在列。代价照实：只剩这一条凭据的行清完就掉出矩阵，玩家还想要就得再点一次添加
--- （本轮有效）。真存档那 5 条 manualAdded 逐条查过 —— 只掉「意外遭遇」一行，
--- 另外 4 条靠关键词/本轮金币撑着，或到期时刻早过了。
--- （本档口径已被 v5 取代一轮：2026-10-27 `manualAdded` 整个字段下线，
--- "本轮点过才算"改成"点了什么都不算"，RollRound 也不再清它。这段留着是历史原貌 ——
--- 本函数仍在档位阶梯里，只管 v3 存档能连续升到 5。）
--- 闸门用档位（db.schema），不用"标记非空即跳过"：后者会让第二次修的档永远轮不到，
--- 见 ResetStickyGoldOnce 那句"标记分档"的教训。
--- 档位没接上时 Migrate 不报错（Config.MIGRATIONS[v] 取不到就当 0 条、照样把 schema 推上去），
--- 所以"标记清没清"必须由用例直说，不能只看档位前进没前进。
function L.ClearManualAddsOnce(db)
    if not db or not db.quests then return 0 end
    local n = 0
    for _, rec in pairs(db.quests) do
        if type(rec) == "table" and rec.manualAdded then
            rec.manualAdded = nil
            n = n + 1
        end
    end
    return n
end

--- 一次性作废老存档里**两份**玩家标记（schema v5）：`ignored` 与 `manualAdded`。
--- ① 为什么这次连老忽略也一起清（v4 只清单标记）：本轮把"移除"的语义从"只管本 CD"
--- 改成**永久屏蔽**（用户 2026-10-27 原话："我最开始 忽略 任务的意图 就是想把 关键词
--- 匹配到了我不想要的任务…我要屏蔽掉，那么我这个意图其实是想永久屏蔽的"）。语义一改，
--- 上一版那种"随手点掉看一眼"的移除就会在新规则里永久压着一行 —— 那是拿玩家从没有过的
--- 意图当真凭据。所以永久屏蔽必须从干净的一页开始，由玩家重新点出自己想要的那批
--- （用户同一轮的原话："所以你先把所有已经做了忽略 / 添加 的数据 清理掉，然后实现上面
--- 真正的意图"）。`manualAdded` 一起清是同一件事的另一半：那个字段本轮整体下线。
--- ② 代价照实（不写这句下次有人会以为又漏清了）：玩家真正想永久屏蔽的那几条
--- ——关键词"丰饶"误命中"辉煌丰饶"、而它压根不是世界任务那种——也会被这次迁移放回来，
--- 他得在屏蔽管理里重新点一次"忽略"。这是他自己要求的顺序，不是漏清。
--- ③ 闸门只有档位（db.schema），不用"标记非空即跳过"：后者会让下一次修的档永远轮不到，
--- 见 ResetStickyGoldOnce 那句"标记分档"的教训，也见 v4 那条说明。
--- v4 的 `ClearManualAddsOnce` 保留不删：档位阶梯必须连续 —— v3 存档要跑 4 再跑 5，
--- 抽掉中间那一档就把升级链断了（Migrate 是逐档跑的，跳档跑会把账本弄成半新半旧）。
function L.ClearIgnoreAndAddMarksOnce(db)
    if not db or not db.quests then return 0 end
    local n = 0
    for _, rec in pairs(db.quests) do
        if type(rec) == "table" and (rec.ignored or rec.manualAdded) then
            rec.ignored = nil
            rec.manualAdded = nil
            n = n + 1
        end
    end
    return n
end

--- 达标任务按地图分组，组内按收益从高到低；组顺序 = 该图最高收益任务的排名。
--- now 传进来时：到期的任务直接不列（用户实测"所有任务时间结束后都要移除"），
--- 并给每行算出剩余秒数（界面用颜色表示）和是否全员完成（置灰禁用态）。
--- now 不传（旧调用方/测试）时不过滤、不算剩余，保持只读账本的原样。
--- 任务展示名：优先账本里存的名字，没有再现场问 Api（并顺手缓存回 rec.name）。
--- 缓存避免每次渲染都去问一遍标题接口；关键词匹配也依赖它。
function L.QuestName(db, questID)
    local rec = db and db.quests and db.quests[questID]
    local n = rec and rec.name
    if type(n) == "string" and n ~= "" then return n end
    if GQL.Api and GQL.Api.QuestTitle then
        n = GQL.Api.QuestTitle(questID)
        if type(n) == "string" and n ~= "" then
            if rec then rec.name = n end
            return n
        end
    end
    return tostring(questID)
end

--- 五道准入门的唯一真源（判据的来历与逐条说明见 MatrixRows 里那一大段注释）。
--- 第 34 轮从 MatrixRows 里提出来，不是为了好看：`/gql quest` 以前自己拼一句写死的猜话
--- （"没命中关键词、本轮没人交过，金币也没到 X"），12.1.5 实测它和紧下面那句
--- "在列靠：本轮有角色交过"直接打架，真砍掉这一行的到期门一个字没提；第 32 轮把
--- "凭据"那半条加进门判时报告漏了一整轮，也是同一个价。
--- money / kwHit / turnedIn / gainOf / floorMoney 一律由调用方算好传进来：MatrixRows 每帧重画都要用
--- 同一批数，在这里重算等于给热路径加一遍账本遍历（手动金币那份表与问 Config 的门槛尤其贵，
--- 上一版为此专门留了"门槛一次读"那行）。诊断报告传 nil 门槛 = 每条现问一次，无所谓。
--- gainOf 末位可缺省 = 判不了 = 行为与第 25 轮一致（老调用方不传金额照样放行）。
--- "本轮交过"这一支从第 35 轮起不再无条件成立：放不放行交给 L.TurnInAdmits（唯一真源），
--- 只有"接（本轮奖励读）"和"交（到手金额）"两处客户端都答了 0 金币才砍；任何一处答不出照旧放行。
function L.RowAdmitted(db, questID, rec, money, kwHit, turnedIn, floorMoney, gainOf)
    if not rec then return false end
    if rec.kwLogAdded or rec.regKey then return true end
    if L.TurnInAdmits(turnedIn and turnedIn[questID], gainOf and gainOf[questID],
        L.GoldVerdict(db, questID) == false) then return true end
    if money and money >= (floorMoney or L.MatrixMoneyFloor()) then return true end
    return not not (kwHit and L.QuestHasCredential(db, questID))
end

function L.MatrixRows(db, now)
    local rows = {}
    local chars = L.Characters(db)
    local ids = {}
    for questID in pairs(db.quests or {}) do ids[#ids + 1] = questID end
    -- 名字与金额先一次算好：认族（PresetFor 只看记录里存的名字）和关键词匹配都依赖名字，
    -- 而 QuestName 把问到的标题缓存回 rec.name。放在家族计算之后，池里刚冒出来的
    -- 同族子任务第一轮就并不进家族（那一格的勾要等下一次重画才补上）。
    local manualOf = ManualMoneyByQuest(db)
    -- 交过的两份表同一趟遍历出来：on 占行、gain 给第 35 轮那道"两处 0 才否决"的门用。
    -- 每帧不许各翻一遍 db.turnins（T1 立这一处遍历就是为了这条）。
    local turnCred = TurnInCred(db)
    local turnInOf, gainOf = turnCred.on, turnCred.gain
    local moneyOf, nameOf = {}, {}
    for i = 1, #ids do
        local questID = ids[i]
        moneyOf[questID] = L.QuestMoney(db, questID) or manualOf[questID]
        nameOf[questID] = L.QuestName(db, questID)
    end
    -- 家族一次算好：一族的成员决定"这一格该显示做过没有"（族里任意一条本周做过即算）
    local famOf, famMembers = L.Families(db)
    -- 门槛一次读：循环里每条都去问一遍 Config 的话，重绘一次矩阵就多 N 次账本查找
    local floorMoney = L.MatrixMoneyFloor()
    -- 收益高者在前；同收益按 questID 排 —— table.sort 不稳定，
    -- 不给 tie-break 的话同额任务每次重画可能换位置（用户实测"列表有变动"）
    table.sort(ids, function(a, b)
        local ma, mb = moneyOf[a] or -1, moneyOf[b] or -1
        if ma == mb then return a < b end
        return ma > mb
    end)
    for i = 1, #ids do
        local questID = ids[i]
        local rec = db.quests and db.quests[questID]
        local money = moneyOf[questID]
        -- 名字解析（关键词匹配要用）：已在预扫里问过并缓存
        local name = nameOf[questID]
        -- 命中关键词的任务：kwHit **不再是独立准入门**（第 32 轮），只是"绕过金币门槛"的
        -- 标记——名字撞上关键词还得答得出至少一条客户端凭据（L.QuestHasCredential）才上板。
        -- 无关键词时 KeywordMatch 恒 false
        local kwHit = (GQL.Config and GQL.Config.KeywordMatch) and GQL.Config.KeywordMatch(name) or false
        -- 显示条件（准入门，全部来自客户端证据或玩家配置）：
        -- 关键词从日志收的（kwLogAdded）/ 注册周常（regKey）/ 本轮有角色交过（turnInOf，
        -- 第 35 轮起这一支要"接"和"交"两处客户端都答 0 金币才否决、任一处答不出照旧放行，判据在 L.TurnInAdmits）/
        -- 金币≥门槛（门槛=设置面板那条，默认 500g）/ 名字命中关键词**且**这条记录有
        -- 一条客户端凭据（kwHit and L.QuestHasCredential：池/日志/注册/读到过钱/客户端答的图/本轮已交）。
        -- 第 32 轮把以前"kwHit 单独放行"降成最后这一条：12.1.5 实测两条上上世代的垃圾行
        -- （82157 巨兽崛起 / 86174 风暴在即）零凭据、纯靠名字撞关键词上板，还顶着
        -- "下次周重置"的假倒计时（计划 §2.1）。同一批里 96718 奈格塔尔虚空决战（英雄）
        -- **不是**垃圾行：14 个号本轮都被客户端答成已完成，只是它交完就离开日志、
        -- 把日志那道凭据撤掉了 —— 所以"本轮任一角色被客户端答成已交"也在凭据集合里
        -- （用户 2026-10-27："这个不应该消失，这是我手动添加的关键词，正巧匹配到了我要的任务"）。
        -- 代价照实：真存在但客户端一条都答不出的特别任务，不接就不上板（§2.5，本轮不假装解决）。
        -- 永久否决只有一道：玩家在「关键词 · 屏蔽管理」点"忽略"过的（rec.ignored）永远不列 ——
        -- 池子和日志每轮都会重新建记录，只删不管用，必须认这条意图
        -- （12.1.5 实测反馈："删不掉，右键点击后还会刷出来"）。它**永久**有效，换 CD、
        -- 换轮都不作废，只有玩家在"添加任务"打开的屏蔽管理窗口里点回来才撤（见 L.RemoveQuest）。
        -- 这五道之外以前还有第六道 `rec.manualAdded`（点过一次添加就绕门槛，还连带
        -- 族占行破例、同名去重豁免、跳过本资料片过滤），2026-10-27 整体下线：
        -- 用户澄清他点那一下的意图是"取消忽略"，不是"标记这条要上板"
        -- ——「手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了」。
        -- 想跟踪门槛以下的任务，正路是加关键词或调门槛，两条都是配置项、都永久生效。
        -- 注册周常绕门槛：丰饶供品这种采集周常金额问不出、也不在世界任务池，
        -- 拿金币门槛筛等于内置表和关键词补扫白做。
        -- 占不占行再走 OccupiesRow 一道：**一族只占一行**，由内置表那条 ID 占，
        -- 板上轮换出来的同名子任务（"丰饶：草药洞穴"，剩余 7 小时那种）只当完成证据，
        -- 不占行（用户 2026-10-24："我鼠标悬停的丰饶 和 我真正要完成的 丰饶日常 不是一个
        -- 任务"、"不显示它（它不是周常）"）。
        if rec and not rec.ignored and L.OccupiesRow(db, questID)
            and L.RowAdmitted(db, questID, rec, money, kwHit, turnInOf, floorMoney, gainOf) then
            -- 到期即移除：账本里的记录仍在（快照/prune 的事），只是不再显示。
            -- 判据改成问唯一真源：一条比本轮起点还早的到期时刻是**上一条实例**留下的，
            -- 本轮不作数（89267 那句"第二个玩家登录了几分钟，矩阵出现了"就是它把整行吃掉的）。
            -- 到期门与下面的 remaining 共用同一个 exp，不许两处各读各的：那会出现
            -- "行还在、悬停写剩余 已到期"这种自己跟自己对不上（本轮要消灭的谎报之一）。
            local exp = L.EffectiveExpiry(db, rec)
            local expired = (now and type(exp) == "number" and exp <= now) and true or false
            if not expired then
                local row = {
                    questID = questID,
                    name = name,
                    mapID = rec.mapID,
                    -- 分组头用的图：与 mapID 分开的三层判据见 L.RowGroupMapID。
                    -- mapID 照旧是账本那条记录自己的答案（悬停、同名去重都读它），
                    -- 换了归属不许顺手改写账本 —— pin 断了行会掉进"未知地图"，
                    -- 那比留在旧分组更难查。
                    groupMapID = L.RowGroupMapID(db, questID),
                    money = money,
                    -- 本轮问不出倒计时时这一格是 nil（不是负数、不是"已到期"）：负数会一路
                    -- 流到悬停，玩家读到的是"这条还剩 已到期"挂在一条活着的行上。
                    remaining = (now and type(exp) == "number" and not rec.expiresGuessed)
                        and (exp - now) or nil,
                    highFreq = L.IsHighFreq(db, questID),
                    poolSeen = rec.poolSeen,
                    state = {},
                    -- 这一格的答案是谁给的（"manual"=玩家手点补录；nil=客户端/流水）。
                    -- 显示层拿它给手动格换颜色，判据只在 L.State 那一处。
                    src = {},
                }
                -- 这一格问谁：一族只有一条时问它自己；多条时问全族取最强的一条
                -- （做过 > 在做 > 可做 > 问不出）。这就是"特殊处理"的全部落点：
                -- 丰饶这周做了辉耀丰饶，那丰饶供品那格也得是勾，反之亦然。
                local fam = famOf[questID]
                local siblings = fam and famMembers[fam]
                row.family = fam
                row.familyCount = siblings and #siblings or 1
                local allDone = #chars > 0
                for c = 1, #chars do
                    -- 点号调用而不是冒号：冒号会把 L 塞进 self，charKey 反而挤到第 3 个参数。
                    -- 状态和"这一格是谁答的"要一起拿，所以写成显式分岔：
                    -- `local st, src = (cond) and f() or g()` 只会留第一个值（Lua 把
                    -- 中间表达式的返回值截成 1 个），第二格答案就丢了。
                    local st, src
                    if siblings and #siblings > 1 then
                        st, src = L.BestRegisteredState(db, chars[c], siblings)
                    else
                        st, src = L.State(db, chars[c], questID)
                    end
                    row.state[chars[c]] = st
                    row.src[chars[c]] = src
                    if st ~= "done" then allDone = false end
                end
                row.allDone = allDone
                -- 玩家点名要的（命中关键词 / 日志收的）单独记一笔：矩阵的"本资料片
                -- 过滤"会整组跳过旧资料片图，不记这一笔，用户在设置里额外跟踪的地图
                -- 哪怕真命中了关键词也会被滤掉（用户实测"加了关键词没有对应任务"）。
                -- 家族成员也算点名要的（fam = 这一行认出的族，认亲看名字不只看 regKey）：
                -- 用户 2026-10-23 的口径①是"子任务属于哪个地图
                -- 就在哪个地图显示…新的丰饶任务会出现，可能在其他地图"，
                -- 丰饶轮换到旧资料片图时按"本资料片过滤"砍掉 = 这条周本周直接消失。
                -- 这里不再有 `rec.manualAdded` 一支：那个字段 2026-10-27 下线，
                -- 玩家点"添加"只撤销屏蔽，不许顺手给自己一张跳过过滤的通行证。
                row.watched = (kwHit or rec.kwLogAdded or fam
                    or row.groupMapID == WEEKLY_GROUP_MAP)
                    and true or nil
                rows[#rows + 1] = row
            end
        end
    end

    -- 同一个名字只列一条：一个世界任务在客户端有两份 questID（池里带地图的那份 +
    -- 日志/补问出来的同名那份），两份都拿到地图时上一版会各列一行 ——
    -- 12.1.5 实测反馈"好几个特别任务都跑到了盘卷蛇岛里，而且其他地图已经有了"。
    -- 留哪一条按证据强弱：池里出现过 > 读到过真奖励金额 > 有地图 > questID 小的。
    -- 以前"玩家手动点名加的那条不参与竞争、永远保留"这道豁免（row.manual）随
    -- manualAdded 一起删了（2026-10-27）：那一戳只是撤销忽略，不构成"我明确要这条"，
    -- 同名两份 questID 里被更强证据挤掉是本来的规则，不是误伤。
    -- 名字问不出的行（nil）没法参与比较，原样保留。
    local function scoreOf(r)
        local s = 0
        if r.poolSeen then s = s + 8 end
        if type(r.money) == "number" then s = s + 4 end
        if r.mapID then s = s + 2 end
        if type(r.remaining) == "number" then s = s + 1 end
        return s
    end
    local best, bestScore = {}, {}
    for i = 1, #rows do
        local r = rows[i]
        if r.name then
            local s, cur = scoreOf(r), best[r.name]
            if not cur or s > bestScore[r.name]
                or (s == bestScore[r.name] and r.questID < cur.questID) then
                best[r.name], bestScore[r.name] = r, s
            end
        end
    end
    local kept = {}
    for i = 1, #rows do
        local r = rows[i]
        if not r.name or best[r.name] == r then
            kept[#kept + 1] = r
        end
    end
    rows = kept

    local out, byMap = {}, {}
    for i = 1, #rows do
        local row = rows[i]
        -- 分组头按 groupMapID（事件 pin > 客户端答的图 > 周常组），不是账本里那个 mapID
        local mapID = row.groupMapID or row.mapID or -1
        local g = byMap[mapID]
        if not g then
            g = { mapID = mapID, rows = {} }
            byMap[mapID] = g
            out[#out + 1] = g
        end
        g.rows[#g.rows + 1] = row
        if row.watched then g.watched = true end
    end
    -- 周常组置顶（用户 2026-10-24 第二十二轮："如果有周常，就周常分组放在第一，其他分组靠后"）：
    -- 这批天生没有地图凭据、也不会随换轮搬回家，玩家开矩阵要找的正是它，不该沉在底部找。
    for i = 2, #out do
        if out[i].mapID == WEEKLY_GROUP_MAP then
            table.insert(out, 1, table.remove(out, i))
            break
        end
    end
    return out
end

--- 下次池刷新时刻 = 账本里**本轮生效的**到期时刻里最早的那一个（世界任务到点才换：
--- 一个都没到期 = 池子不可能变，没必要去抓）。一条到期时间都没有
--- （从没同步过、或奖励/时间接口没给过数）→ nil，调用方按"该抓了"处理。
--- 上一条实例留下的那条过期值不参与（同一个真源 L.EffectiveExpiry）：它会把"最早到期"
--- 永久钉在过去，等于定点唤醒的兜底整条停摆 —— 全是过期值时照样回 nil，
--- 调用方（Sync 那句"到期已过/取不到 ⇒ 不自动安排"）一个字不改，不新增任何定时器。
function L.NextRefreshAt(db)
    local best
    local quests = db and db.quests or {}
    for _, rec in pairs(quests) do
        if type(rec) == "table" then
            local exp = L.EffectiveExpiry(db, rec)
            if type(exp) == "number" and exp > 0 and (not best or exp < best) then
                best = exp
            end
        end
    end
    return best
end

--- 列 = 角色：本周完成数/金币 + 最后同步时间 + 是否过期。
--- 锚点定不出来（0）或时钟域分裂时 count/gold 一律 -1，UI 显示 ?，
--- 口径与 Dump 的本周汇总必须一致——同一份账本不许两处报两个数。
function L.MatrixColumns(db, now)
    local chars = L.Characters(db)
    local anchor = db.cycleAnchor or 0
    local out = {}
    for i = 1, #chars do
        local charKey = chars[i]
        local count, gold = -1, -1
        if anchor > 0 then count, gold = L.WeekStats(db, charKey, anchor) end
        local lastSync = (db.chars[charKey] or {}).lastSync or 0
        out[#out + 1] = {
            key = charKey,
            class = (db.chars[charKey] or {}).class,
            lastSync = lastSync,
            count = count,
            gold = gold,
            stale = (now and lastSync > 0 and (now - lastSync) > STALE_SECONDS) and true or nil,
        }
    end
    return out
end
