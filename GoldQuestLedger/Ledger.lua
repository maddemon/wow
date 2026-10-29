-- 纯账本逻辑：世界任务池 diff、奖励签名。不碰 WoW 全局 API（时间戳由调用方传入），
-- 因此可在 lupa 里离线跑。

GQL.Ledger = {}
local L = GQL.Ledger

local MAX_OBS_PER_QUEST = 4
-- 消失时长小于此值 = 高频区（屠戮者高地/虚影风暴 12 小时级），不写死区域名单
local HIGH_FREQ_GAP = 129600
-- 第 36 轮 T4b 删掉的两道"轮次"常量（ROUND_END_SLACK / ROUND_CHANGE_TOLERANCE）原本是给
-- "换轮推起点"那套检测器用的：容差 900 秒 = 结束时刻差出 15 分钟就推一次起点。
-- 编号模型里不需要推起点 —— 结束时刻一变编号自然变，而"15 分钟"这条边界已经挪进
-- L.InstanceKey 的分桶（±1 秒抖动落同一桶，见那一段的 +450 半桶偏移）。
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
--- 抄在文件级会静默拿到 nil 标签，判据当场失真。
--- 取不到时回 ""："" 不是任何一个合法标签，而下面两张判定表都不把 "" 收进键
--- （构造时跳过空串），所以拿不到标签只会走"没凭据"那一边，不会把猜的当客户端答复。
local function MapSrc(key)
    local M = GQL.Config and GQL.Config.MAP_SRC
    return M and M[key] or ""
end
local function LEGACY_MAP_SRC() return MapSrc("legacy") end
local function SUSPECT_MAP_SRC() return MapSrc("suspect") end

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
            MapSrc("bountyReverse"), MapSrc("logZone"),
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

--- 客户端答过的图。**"玩家指定"也在黑名单里**：口述机制（Api.MAP_MEMORY）2026-10-29
--- 已整门下线，写入点全拆了；这格只管两件事 —— v8 迁移把老存档的口述图清掉、
--- 迁移失败停档重试的窗口期里残留的那份照旧不当客户端图（口径没变：口述只当备注）。
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
            MapSrc("manual"),     -- 玩家口述（机制已下线，只剩老存档残留）
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
    -- mapManual 从 2026-10-29 起**已无写点**（口述机制 Api.MAP_MEMORY 整门下线，写入点
    -- SetManualMap/ApplyMapMemory 全删），只剩 v8 迁移抹老档残留（`rec.mapManual = nil`）
    -- 和三个读者（NOT_CLIENT_SRC / QuestHasCredential / Options.evidence）按 false 处理。
    -- 留在清单里同 manualAdded 的理由：删掉这项，迁移函数自己就会被字段守卫判成清单外写法报红。
    name = true, mapID = true, mapSrc = true, mapAsked = true, mapManual = true,
    poolSeen = true, onBoard = true, lastSeenAt = true,
    -- 奖励与金币：金额的唯一读法是 L.QuestMoney（"这一条实例读到过金币"= goldMoney + goldKey 一对）
    -- 别处不许再各拼一套判据 —— 上一版就是 obs.gold、lastMoney、goldLock 三处各读各的
    -- goldKey = 第 36 轮"任务号+结束时刻"唯一编号（L.InstanceKey），只由 L.NoteGoldThisInstance
    -- 在 Sync 池循环里写（SetExpiry 之后才用得上这一条实例自己的结束时刻），与 goldMoney 同进同退：
    -- 编号换了旧的 goldMoney 一起作废（§六 1(c) 第 4 步），读侧（L.QuestMoney / GoldFromOtherMode）
    -- 只认 sameKey(rec.goldKey, 当前编号) 的那一份。
    -- goldRound = 老"轮次起点"那一份粘滞标记，T4b 起**没有任何判据读它**了；
    -- 只剩历史清理 L.ResetStickyGoldOnce 会清它（清单留着就是为了字段守卫不许谎报）。
    observations = true, currentSig = true, lastMoney = true,
    goldMoney = true, goldRound = true, goldLock = true, goldKey = true,
    -- 到期：roundEnd = 这一条实例自己答出的结束时刻（身份与到期共用这一个真源，
    -- 唯一写点 L.SetExpiry；§五 的命名决定：本轮不改名）。
    -- 老那一组 expiresAt/expiresSrc/expiresGuessed（含 24 小时排程值）T5 起**已整组退役**：
    -- 无写点、无读者，连取证面也不再问来源；老档里残留的值原地留着，谁也不读它。
    -- 所以它们不在清单里 —— 字段守卫扫到任何 `rec.expiresAt` 一类读写都会报红，这正是想要的。
    -- roundAt = 老"本轮起点"，T4b 起**没有任何判据读它**（L.RoundAt 整套已删 —— 它判不出时
    -- 把兜底值写回 rec.roundAt，那句"判不出就当本周初一直在"就是 94612 十个假勾的根因）。
    -- 只剩 §九 的 v6 迁移拿它当"这条记录滚没滚过轮"的取证依据，以及老档残留等迁移清。
    roundAt = true, roundEnd = true, goneAt = true, highFreq = true,
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

--- 调用方手边没有 now 时的兜底时钟：与 Sync.Clock() 同一条口径（有墙上时钟就用它，
--- 否则用开机秒），因为账本里的时刻就是照 Sync.Clock() 那个域写下的 —— 域挑错等于
--- 跨域读，L.InstanceEnd 判不出"活着没"就回 nil（fail-closed：整排 ?，不造假勾）。
--- 【这条兜底只给取证面与老调用点】矩阵热路径一律由 L.MatrixRows 在循环外把 now 定好
--- 逐行传进来（§十一：每行每帧最多算一次编号），一次都不走到这里；
--- 走到的话也只多问一次本地时钟读数，不是新增接口调用（没有 C_/GetQuest 那一类）。
local function LedgerNow(db)
    local wall = GQL.Api and GQL.Api.WallClock and GQL.Api.WallClock() or nil
    if type(wall) == "number" then return wall end
    local up = GQL.Api and GQL.Api.Uptime and GQL.Api.Uptime() or nil
    return type(up) == "number" and up or nil
end

--- 这个角色这一格的"完成"是不是落在**这一条任务这一条实例**上：判据只有一个形状 ——
--- 这笔流水写下的编号 == 这条任务当前的编号（L.SameInstanceKey，§六 1(d)）。
--- 手动补录那一步的"接口核验变绿"只有在这条为真时才成立 ——
--- 客户端 Api.QuestState 回的 done 可能是上一轮的历史 flag（固定 ID 周常 / 完成标记残留），
--- 那种不是这一条实例的凭据，不能把玩家手点的那一笔顶成绿色。
--- 编号对不上（或那一笔压根没烙上编号 = 身份还没定下来时写的）一律回 false，
--- 手动点的那笔保持琥珀。注册周常不用另走 CD 门：它的编号本来就是周 CD 尾巴那一档
--- （L.InstanceEnd ②），比编号与比 CD 是同一件事（第 36 轮 T4b 把这一岔吸收掉了）。
--- curKey 由调用方传（同一行 14 个号只算一次，§十一）；不传时这里按 now 算一次。
function L.CompletedThisRound(db, charKey, questID, now, curKey)
    local rec = db and db.quests and db.quests[tonumber(questID)]
    local rows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    if not last then return false end
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, rec, now ~= nil and tonumber(now) or LedgerNow(db))
    end
    return L.SameInstanceKey(last.k, curKey)
end

--- 比编号唯一的写法（计划 §六 1(d)，R1-R13 十三处判据一律走它）：
--- **两边任一是 nil 就不成立**。裸写 `node.k == curKey` 在两边都是 nil 时**相等**
--- ⇒ "身份未定"反而全盘放行，正是 §五 那条"没编号 = 不匹配"的反面（94612 那批假勾
--- 换个形状复发）。放在 Ledger 里导出给 Sync.lua 用，13 处不许各拼一遍这个守卫。
function L.SameInstanceKey(a, b)
    return a ~= nil and b ~= nil and a == b
end

--- 单条任务的**角色级凭据清单**（取证用，2026-10-29 回响复燃 92713 那单）：
--- 一格的绿/琥珀只可能来自三份按角色存的凭据 —— 快照 views、交割流水 turnins、
--- 补录 manual。Dump 的 /gql quest 要摊开它们各自记的编号跟当前编号对答案，
--- 但 Dump 不许自己翻 db.turnins（turnin_both_zero_gate_has_one_home 那道守卫），
--- 三份凭据的字段形状也只有账本这一处认得，所以收集住在这里，Dump 只管格式化。
--- 返回数组，每项 {charKey=, view=, turninCount=, lastTurnin=, mark=}，
--- 只列"这条任务上至少有一份凭据"的角色；顺序不保证（诊断报告不挑顺序）。
function L.CredentialsOf(db, questID)
    local out = {}
    for charKey, ch in pairs(db and db.chars or {}) do
        if type(ch) == "table" then
            local view = ch.views and ch.views[questID]
            local rows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
            local mark = db.manual and db.manual[charKey] and db.manual[charKey][questID]
            if type(view) == "table" or type(rows) == "table" or type(mark) == "table" then
                out[#out + 1] = {
                    charKey = charKey,
                    view = type(view) == "table" and view or nil,
                    turninCount = type(rows) == "table" and #rows or 0,
                    lastTurnin = type(rows) == "table" and rows[#rows] or nil,
                    mark = type(mark) == "table" and mark or nil,
                }
            end
        end
    end
    return out
end

--- 这一条实例**自己的**结束时刻，两档来源与优先级见下面 L.InstanceEnd 那一段（那里是全文）。
--- 与 L.InstanceEnd 的唯一差别是"过滤"：这里**不卡"还活着没"、也不卡时钟域**，
--- 因为两件事要的数正好相反 ——
--- · 身份（L.InstanceEnd → L.InstanceKey）：已经过去的那一条是上一条实例的墓碑，
---   一律不收，编号不许挂在死掉的时刻上（94612 十个假勾就是这么来的）；
--- · 到期门（L.InstanceExpired）：要的恰恰是"它几点结束、那个点过没过"这个事实，
---   不收过去值就等于永远判不出到期，96718 那句"时间结束后都要移除"会当场失效。
--- 两处共用这一份来源选择，不要第二套：字段退役（T5 已去掉 expiresSrc 守卫，
--- `rec.roundEnd` 在场就是板面答出的结束时刻，唯一写点 L.SetExpiry）只改这里一处。
--- 照旧是**纯账本算式**：不许在这条路上问 Config.NextCycleEnd / Api.WeeklyResetCountdown
--- （矩阵每帧 20 行 × 每帧 = 新增 20 次接口调用，用户 10-27 明令禁止）。
local function InstanceEndRaw(db, rec)
    -- 没有这条记录 = 没有任何实例可谈：尾巴档是"这条记录问不出倒计时"时的兜底，
    -- 不是"账本空着时就报本 CD 的尾巴"，否则调用方传 nil 也能拿到一个结束时刻。
    if not rec then return nil end
    local e = tonumber(rec.roundEnd)
    if e then return e end
    local anchor = tonumber(db and db.cycleAnchor)
    if anchor and anchor > 0 then return anchor + 604800 end
    return nil
end

--- 这一条**实例**的结束时刻（秒），nil = 身份判不出来。用户 2026-10-28 定案
--- （"我从很早就说了，一个任务id+结束时间，算他的唯一标识"）：身份 = questID + 结束时刻，
--- 不再有"本轮起点"。两档来源，优先级固定：
--- ① 板面倒计时：rec.roundEnd（唯一写点 L.SetExpiry，只由板面剩余秒数算出；
---    24 小时排程值随 T5 已整组退役，如今没有别的来源）且**还活着**（> now）。已经过去的是上一条实例留下的墓碑，
---    绝不再当身份 —— 第 34 轮 89267 那条"上一条实例的过期值吃掉整行"就是拿它当了判据。
--- ② 周 CD 尾巴（用户点名的周常组，原话"周常（获取不到结束时间的任务组）先按周CD的时间
---    算结束时间"）：db.cycleAnchor + 604800。**只在①压根没答过**（`rec.roundEnd` 为空）
---    时才走这一档：锚点本来就是 Config.RefreshCycle 用周重置倒计时算出来的
---    （anchor = 下次周重置 - 604800），加回一周就是服务器那头的轮换点。
---    "答过、但那条已经过去"不许退到②：那说明这是 12 小时区的条实例，一周的尾巴会把
---    同一周里的上下两条并成同一个编号 = 上一轮的 ✓ 冒充本轮（第 25 轮真档实测
---    89377「秘密狩猎」全角色交完之后再也读不到倒计时，就是这个形状）。这种一律 nil。
--- **这里是纯账本算式，不许在这条路上调 Config.NextCycleEnd / Api.WeeklyResetCountdown**：
--- 它每次都要问一遍接口，而矩阵每帧 20 行都要问一次身份 = 每帧新增 20 次接口调用，
--- 正是用户 10-27 明令禁止的那类修法（修法零新增接口调用、不轮询、不扫描）。
--- 时钟域：账本里的时刻可能是墙上秒（epoch，>1e9）也可能是开机秒（uptime），
--- rec.roundEnd 与 now 完全可能分属两域。沿用 ThisCycle / EffectiveExpiry / WeekStats
--- 的同一条 house rule：(a > 1e9) ~= (b > 1e9) 即判成跨域，**跨域不比较**。
--- 于是板面值只在"与 now 同域"时才拿来判断"还活着没"；异域时活没活压根判不出来，
--- 按本轮唯一的降级方向（判不出来 ⇒ 不成立，fail-closed）不收板面值 —— 也**不许**因此
--- 退到②：roundEnd 有值就说明这条是靠板面倒计时定身份的，退回周尾巴等于凭空放宽一轮。
--- 尾巴不和 now 比大小（它只进 L.InstanceKey 的分桶），epoch/uptime 各自都是
--- 单调时间线，锚点+一周在哪个域里都稳定，所以尾巴这一档不卡域。
--- now 取不到（nil）同理：证明不了"活着"，不收板面值。
function L.InstanceEnd(db, rec, now)
    local n = tonumber(now)
    local e = InstanceEndRaw(db, rec)
    if not e then return nil end
    -- 只有板面档要"活着没"这道过滤（来源是 rec.roundEnd 在场 = 这条靠板面倒计时定身份）；
    -- 尾巴档不卡域也不卡大小：它只进 L.InstanceKey 的分桶，见上面那段。
    if rec and tonumber(rec.roundEnd) then
        return (n and (e > 1e9) == (n > 1e9) and e > n) and e or nil
    end
    return e
end

--- 实例唯一编号 = questID .. ":" .. 按 15 分钟分桶的结束时刻。写侧在烙下完成
--- 凭据的那一刻把当时的编号存进记录，读侧只问"这笔记录的编号 == 当前编号吗"，
--- 不再拿时间戳跟任何"起点"反推（反推会被陈旧 roundAt / 时钟域错乱带偏，
--- 12.1.5 的 94612 十个假勾就是这么来的）。
--- questID 显式作参数：rec 自己不存 id（它就是 db.quests 的表键），调用方两个值
--- 都在手边，不为此做表扫反查。
--- **+450 半桶偏移是必须的**：真板面的结束时刻都落在整点/刻钟上（12.1.5 真档实测
--- 19:00 / 07:00 / 15:00 / 11:00），而接口答的是"剩余秒数"，now + remaining 只会比
--- 真值小 0~1 秒。裸 floor(endAt/900) 的桶边界正压在整点上 ⇒ 同一条实例的两次读数
--- 有接近一半概率落进相邻桶 = 凭空伪造一条新实例、把本周做过的 ✓ 全判成上一轮
--- （第 25 轮"切模式翻奖励不该丢勾"教的正是这个方向）。偏半桶后 ±1 秒抖动必然
--- 落同一桶，真换实例（整段挪 12 小时/一周）照旧换桶。
function L.InstanceKey(db, questID, rec, now)
    local endAt = L.InstanceEnd(db, rec, now)
    if not endAt then return nil end
    return tostring(questID) .. ":" .. tostring(math.floor((endAt + 450) / 900))
end

--- 金币粘滞与 observation 按**实例编号**记账（计划 §四 W5 / §六 1(b)(c)）：
--- 板上每看见一次这条任务，就把这一条实例的编号烙进 rec.goldKey，并给当前签名那份
--- observation 补一份 obs.key。为什么写在 Sync 的池循环、而不是 DiffPool 里：
--- DiffPool 跑在逐条倒计时读取之前，那次算编号只能用"上一次读到的结束时刻"，
--- 板上第一次见到这条任务时那个值根本还不存在 ⇒ 烙出来是 nil（§六 1(a) 同一件事）。
--- 调用点因此排在 L.SetExpiry 之后，一次接口调用都不多（复用的是同一条已经问出来的倒计时）。
--- 四步的形状是 §六 1(c) review 时定死的，实施不许自创：
---   1. 算编号；nil ⇒ **一个字不写、直接回 nil**（旧键留着也匹配不上任何人 = fail-closed）。
---   2. `obs.key` 对不上这次 ⇒ 这份观察是按上一条实例记的账，**作废它的金额**
---      （老那句 `if rolled then reuse.money/reuse.gold = nil` 的编号版，而且时刻更准）。
---      不写这一步 = 上一条实例的钱被新键认领 = 凭空多一行金币任务 —— **假勾方向，最要紧**。
---   3. 烙上新键：`obs.key = key`、`rec.goldKey = key`。
---   4. 金额用**池行这一读的原始读数**（不是 observation 里搬来的延续数）：是数且 > 0 才写
---      `rec.goldMoney`。老 DiffPool 那句 `rec.goldMoney, rec.goldRound = cur.money, at` 归这一笔替换。
--- 第 3 步换键时把 `rec.goldMoney` 一起作废：粘滞的语义是"**这一条**实例里任意一档读到过钱"，
--- 键换了、钱还留着，等于让新实例认领上一条实例的钱 —— 正是第 2 步要拦住的那个假勾形状。
--- 代价照实（计划 §六 1(c) 末句）：换实例那一次同步里这份金额被第 2 步清空、靠第 4 步的
--- 原始读数续上；那一读恰好问不出金额 ⇒ 这一轮少报一笔钱（少报可以，假勾不行）。
--- 编号算不出来（身份未定）时无事可烙，直接回 nil —— 不许兜底成周初那类"判不出就当一直在"
--- （§三第 1 条的取向：判不出 = 不成立）。字段里留着的是上一条实例的编号，**不清它**：
--- 读侧拿它跟"当前编号"比，当前算不出就谁都配不上 ⇒ 金币粘滞与读闪延续一起失效，
--- 界面对这条就是问不出，等于如实报"这一条现在挂不上编号"。
--- obs.key 不分给不给金币都要写：§六 1(b) 那条"读闪延续只许从同一条实例那份搬"
--- 问的是"这份观察属于哪条实例"，跟那副奖励值多少钱无关。
function L.NoteGoldThisInstance(db, questID, money, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local key = curKey
    if key == nil then key = L.InstanceKey(db, questID, rec, now) end
    if not key then return nil end
    local obs = L.GetObs(db, questID)
    if obs then
        if not L.SameInstanceKey(obs.key, key) then
            obs.money, obs.gold = nil, nil
        end
        obs.key = key
    end
    if rec.goldKey ~= key then rec.goldMoney = nil end
    rec.goldKey = key
    if type(money) == "number" and money > 0 then rec.goldMoney = money end
    return key
end

--- 界面/定时器读的"这条现在还活着的话几点结束"：**只回这条实例自己的结束时刻，
--- 而且只在它还没过去（> now）时回**，绝不返回过去值（第 36 轮 T4a 换成编号模型，计划 §七）。
--- 过去值一旦从这里漏出去，"剩余 已到期"就会挂在一条活行上（用户 10-27 的 89267 那句谎报），
--- 所以下面 7 处调用点（矩阵到期门 / row.remaining / 悬停倒计时 / 右栏 keepable /
--- 右栏同名去重打分 / NextRefreshAt / /gql quest）一律传自己手边已有的 now，
--- **不许为了拿 now 在同一个函数里再问一次时钟接口**（用户 10-27 定的"修法零新增接口调用"）。
--- 三条边界，一条都不能省：
--- · 来源只有 `L.InstanceEnd` 那一套（板面倒计时优先，问不出才用周 CD 尾巴），
---   这里不许再自拼第二个到期字段：老 `rec.expiresAt` 那组（含我们编的 24 小时排程值）
---   随 T5 已整组退役 —— 编的数参与判据就是每天造一条新实例。
--- · 跨时钟域不报（ThisCycle / L.Prune / WeekStats 同一条 house rule）：尾巴档给的是
---   `cycleAnchor + 604800`（墙上秒），矩阵那侧的 now 可能是开机秒，异域时"还剩多久"根本
---   算不出来，报出来就是一个几万天的假倒计时。
--- · now 取不到（nil）⇒ 什么都不报：证明不了"还活着"就当作不知道（fail-closed），
---   界面对它是剩余空着、悬停不报倒计时，`TimeColor` 对 nil 与 ≤0 同色，不会误红。
--- 代价照实：换轮后到倒计时读到之间这一行不再被"到期"吃掉（那由 L.InstanceExpired 判），
--- 这一段里剩余那格是空的 —— 本来也问不出。
function L.EffectiveExpiry(db, rec, now)
    local n = tonumber(now)
    local endAt = L.InstanceEnd(db, rec, n)
    if not endAt or not n then return nil end
    if (endAt > 1e9) ~= (n > 1e9) then return nil end
    return endAt > n and endAt or nil
end

--- 矩阵/右栏/`/gql quest` 共用的那一道**到期门**（计划 §七）：
--- `expired = instEnd ~= nil and instEnd <= now and not (与 instEnd 同域的 rec.lastSeenAt > instEnd)`
--- 两条既有保证各钉一头，谁也不许被另一头吃掉：
--- · 96718「时间结束后都要移除」（用户 10-24 定）：这一条实例自己的结束时刻已过、
---   又没有更晚的"板上还看得见"证据 ⇒ 死行。
--- · 89267「上一条实例的过期值不许吃掉整行」（第 34 轮，用户实测）：换轮后倒计时问不出、
---   `rec.roundEnd` 停在上一条实例的过去值，可这条任务还在板上被看见过
---   （每次 DiffPool 都写 `lastSeenAt`，Ledger.lua/Sync.lua 那几处现字段）⇒ 第三个条件
---   把它顶回去 ⇒ **不判到期**，行留着、剩余空着，等接口自己写新的结束时刻。
---   这一保证从此由编号模型直接推出，不再需要老那句 `exp < 本轮起点 ⇒ nil` 的特例。
--- 跨时钟域一律不判（同 EffectiveExpiry / ThisCycle / L.Prune 那条 house rule）：
--- `lastSeenAt` 与 `instEnd` 一个是墙上秒一个是开机秒时，"晚于"根本判不出来，
--- 而判错的代价是把一条还活着的行吃掉 —— 方向与 89267 那条一致：**判不出就不判**。
--- `instEnd` 与 `now` 跨域同理不判（尾巴档的锚点是墙上秒、开机域的 now 比它小得多，
--- 硬比就是把每条常驻周常当场砍掉）。
--- 纯读账本：一次接口调用都不多，lastSeenAt / roundEnd / cycleAnchor 都是现字段。
function L.InstanceExpired(db, rec, now)
    local n = tonumber(now)
    local endAt = rec and InstanceEndRaw(db, rec)
    if not rec or not n or not endAt then return false end
    if (endAt > 1e9) ~= (n > 1e9) then return false end
    if endAt > n then return false end
    local seen = tonumber(rec.lastSeenAt)
    if not seen then return true end
    if (seen > 1e9) ~= (endAt > 1e9) then return false end
    return seen <= endAt
end

--- 用当前角色的世界任务池快照跟账本 diff。
--- "本轮"= 连续存在于两次快照之间，不设时间阈值：任务消失后再出现即新一轮
--- （无论隔 12 小时还是 4 天），高频区与周常区共用同一套逻辑，插件不需要知道任何刷新周期。
--- 【第 36 轮 T4b：这里没有"轮次"了】以前这里有一套换轮机器
--- （`RoundEnded` 检测 + `RollRound` 把 `rec.roundAt` 推到当下、顺手清 `goldRound`）。
--- 整块删除（计划 §五）：身份 = 「任务号 + 结束时刻」的唯一编号，结束时刻一变编号自然变，
--- 不需要"推起点"这个动作，也不需要 15 分钟容差那个检测器（容差挪进了 L.InstanceKey
--- 的半桶分档）。下面 `rolled` 只剩两件**取证**事：`res.renewed`（面板提示"这条换轮了"）
--- 与高频区学习（`rec.goneAt` / `rec.highFreq`），都不参与身份。
--- 金额也不在这趟里记：钱归 L.NoteGoldThisInstance（计划 §六 1(c) 第 4 步），
--- 原因见那里的 §六 1(a) —— DiffPool 跑在逐条倒计时读取之前，这里算出的编号是**旧键**。
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

            -- `rolled`（上一条注释）只做取证与高频学习，不再推任何起点。
            -- 这一趟算出的编号是**旧键**（计划 §六 1(a)：DiffPool 排在逐条倒计时读取之前），
            -- 同一次同步里排在后面的 L.NoteGoldThisInstance 会拿读完倒计时以后的新键把 obs.key
            -- 纠正过来（§六 1(c) 第 2/3 步），所以这里只用它做"同一条实例之内才许搬钱"那道门。
            local curKey = L.InstanceKey(db, questID, rec, now)
            -- 换轮先把这份复用的 observation 里的旧数作废：observation 是按奖励签名分的，
            -- 新一轮如果又读回同一副签名（12 小时区里同一副奖励隔一轮重现很常见），
            -- 签名那份记录还留着上一轮的金额。
            -- 不清它，下面的读闪延续就等于"这一轮一个字没答，拿上一轮的金币数冒充本轮读到的"
            -- —— 用户 12.1.5 实测"神秘身影之前是金币任务，现在不是了，但还在矩阵里"就是这条链。
            -- （编号版的作废在 NoteGoldThisInstance 第 2 步，时刻更准；这一支按 isNew/goneAt
            --   作废，两条同向、都只往"少报"那边走，留着不冲突。）
            if rolled then
                local reuse = rec.observations[sig]
                if reuse then reuse.money, reuse.gold = nil, nil end
            end

            local obs = rec.observations[sig]
            if obs then
                obs.goneSince = nil
                -- firstSeen 从这一版起**降级成纯取证**（计划 §六 第 3 条）：
                -- "这一副奖励第一次读到的时刻"，建的那一刻写一次、以后只更 lastSeen。
                -- T4b 复核照实：换成编号模型后**连取证读者也没有了**（Dump 那句"上次金额"
                -- 读的是 rec.lastMoney，不是这一格）⇒ 留它只为 §八 措辞那一刀决定要不要
                -- 把它印进 /gql quest；判据一律不许再碰它（计划 §六 第 3 条末句）。
                obs.lastSeen = now
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
                    -- 只许从**同一条实例**那份搬（§六 1(c)：老那句"只在本轮之内搬"的编号版，
                    -- 语义等价）：上一份签名里的数属于上一条实例，搬过来等于给这一条
                    -- 凭空造一份金币凭据 = 假勾方向。没烙过编号的一份永远搬不过去。
                    for _, other in pairs(rec.observations) do
                        if type(other.money) == "number"
                            and L.SameInstanceKey(other.key, curKey) then
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
                -- 同样只认这一条实例的那一份：换实例后 prevSig 那份是旧奖励的账，跟这条无关。
                local prev = prevSig and rec.observations[prevSig]
                local carried
                if type(q.money) ~= "number" and prev
                    and type(prev.money) == "number"
                    and L.SameInstanceKey(prev.key, curKey) then
                    carried = prev.money
                end
                rec.observations[sig] = {
                    gold = L.IsGoldSig(sig) or ((carried or 0) > 0),
                    money = (type(q.money) == "number") and q.money or carried,
                    items = q.items and CopyArray(q.items) or nil,
                    key = curKey,
                    firstSeen = now,
                    lastSeen = now,
                }
                TrimObservations(rec, sig)
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

--- 写一笔交割流水。**返回值是刚写下的那一行**（2026-10-29 刀 2 从 true 改的）：
--- QUEST_TURNED_IN 到达时 Sync 当场调这一笔记下"完成"，行引用留在窗口上，
--- 钱窗到点后只往同一行补金额（L.SettleTurnInGain），不再追加第二笔 ——
--- 周统计一笔交任务只能算一次。唯一调用方是 Sync；直调的老用例只读账本不读返回值，不受影响。
function L.OnTurnIn(db, charKey, questID, t, g, sig)
    db.turnins[charKey] = db.turnins[charKey] or {}
    local log = db.turnins[charKey]
    log[questID] = log[questID] or {}
    local rows = log[questID]
    -- 烙上当时的**唯一编号**（计划 §四 W1）：L.State / TurnInCred 之后只比这个键，
    -- 既不拿 t 反推轮次，也不看 CD 锚点 —— 反推会被陈旧起点 / 时钟域错乱带偏，
    -- 锚点是 0 时那道 `cycle == cycleAnchor` 一进门就放行（12.1.5 的 94612 十个假勾
    -- 两个方向都占上了）。用户 2026-10-27 的轮次 Id 提案 + 2026-10-28 定案
    -- "一个任务id+结束时间，算他的唯一标识，完成时都要存这个任务唯一编号"。
    -- 老那一份 `round = L.RoundKey(...)`（挂在可变的"本轮起点"上、起点判不出时兜底成周初
    -- 的 fail-open 写点）随 T4b 的读侧切换一起删（§五："删字段与删它的读点是同一件事"）。
    -- 编号算不出来时写下的是 nil ⇒ 读侧永不匹配（§五"没编号 = 不匹配"），
    -- `cycle` 那格留着**只给 /gql quest 取证**用（§六 第 2 条），没有任何判据读它。
    local rec = db.quests and db.quests[questID]
    rows[#rows + 1] = { t = t, g = g, sig = sig, cycle = db.cycleAnchor or 0,
        k = L.InstanceKey(db, questID, rec, t) }
    while #rows > MAX_TURNINS_PER_QUEST do table.remove(rows, 1) end
    if type(g) == "number" and g > 0 then
        L.MarkGoldByObservation(db, questID, sig)
    end
    return rows[#rows]
end

--- 刀 2 的配套：交割已经当场落账（QUEST_TURNED_IN 到达时写过流水），钱窗到点 /
--- 换任务 / 登出冲窗时**只把金额补进那一行**，不再追加第二笔 —— 以前"记完成"和
--- "量钱"绑在同一个窗口末尾（Sync.FinishWindow），窗口一死两样一起丢，昨天那单
--- 全靠登出冲窗补救；拆开后任何一侧都丢不掉另一侧。
--- gain 的语义与窗口末尾传给 OnTurnIn 的 g 一字不差：nil = 窗口被污染/量不出
--- （那一行保持"没量到"，周统计如实报 ?，不许拿脏数顶上），数字 = 实测净增量。
--- 出现正数才 MarkGoldByObservation（"只认进账"的口径与 OnTurnIn 同一份）。
function L.SettleTurnInGain(db, questID, row, gain, sig)
    if not row then return false end
    if type(gain) == "number" then
        row.g = gain
        if gain > 0 then
            L.MarkGoldByObservation(db, questID, sig)
        end
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
--- · 玩家/插件认定的**常驻周常**（带 regKey，或从任务日志收进来的 kwLogAdded）→ 板面倒计时
---   一律不许写进 roundEnd：图上一换轮，这些任务的**板面实例**只剩几小时，可它们的每周
---   lockout 还活着，拿板面倒计时当到期等于当天下午就把整行抹掉（12.1.5 实测：加固符文石
---   这种注册周常"这个任务没了"，用户："可能和倒计时有关系，倒计时不准"；上一轮同类反馈是
---   "符文石和特别任务…这两个都是周常任务，不应该到期了"）。它们的结束时刻走
---   L.InstanceEnd 的②档（周 CD 尾巴 = cycleAnchor + 604800），roundEnd 不写、自然落到尾巴。
--- · 没注册的池内世界任务 → 用它自己问出来的板面倒计时，到期即消失（"时间结束后都要移除"）。
--- T5 起这里只写 roundEnd 这一个字段：老那一组 expiresAt/expiresSrc（含"接口倒计时"来源
--- 标记）连同 24 小时排程值一起退役，从此没有第二份到期账。
function L.SetExpiry(rec, now, remaining)
    if rec.regKey or rec.kwLogAdded then
        return
    end
    if type(remaining) == "number" and remaining > 0 then
        local endAt = now + remaining
        -- 结束时间变了 = 板面上换成了另一条任务（用户 2026-10-25："是不是新任务，
        -- 看结束时间是不是新日期就够了"）。老版本这里要判"差出 15 分钟容差才换轮"，
        -- 再替整条记录推一次起点 —— 编号模型里这件事是**自然发生的**：身份就是
        -- 「任务号 + 结束时刻」，endAt 一变 L.InstanceKey 就变，旧凭据自己匹配不上，
        -- 不需要容差常量、也不需要任何"推起点"的写（计划 §五 整块删除那一条）。
        rec.roundEnd = endAt
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
        -- 烙上当时的唯一编号：L.State 之后只比键，换了一条实例自动作废
        -- （用户 2026-10-27 轮次 Id 提案 + 2026-10-28 定案"完成时都要存这个任务唯一编号"）。
        -- 老那一份 `round = L.RoundKey(...)` 与读侧的"本轮起点"一起删（§五）。
        local rec = db.quests and db.quests[questID]
        db.manual[charKey][questID] = { state = state, t = t, g = g, v = v,
            k = L.InstanceKey(db, questID, rec, t) }
    end
    return old
end

function L.ClearManual(db, charKey, questID)
    if db.manual[charKey] then db.manual[charKey][questID] = nil end
end

--- 手动添加任务（点地图 pin / 矩阵"添加任务"打开的两栏"关键词 · 屏蔽管理"窗口走这条）。
--- **玩家侧的含义只有一句话：撤销"别再显示这一行"的意图**（用户 2026-10-27 的原话：
--- "手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了"）。
--- 上一版这里写 `manualAdded = true`，那道标记是矩阵六道准入门之一，还单独撑破了同名
--- 去重豁免、一族一行的破例、"本资料片过滤"跳过 —— 等于把一次撤忽略当成了一张上板
--- 通行证，而玩家从没做过"标记这条要强制上板"这个动作。字段本轮整体下线，这条路径
--- 现在只做账：建记录（没有的话）、问名字/地图/金额、写一条 observation、续 lastSeenAt、
--- 清 ignored。不再"定个默认到期"——编号模型里新记录没有 roundEnd，到期门自然走周 CD 尾巴
--- 那一档，再加上新写的 lastSeenAt 顶着（L.InstanceExpired 的 89267 保护），行不会被立刻吃掉。
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

--- 板上"这条我做到哪一步了"那份快照。第 36 轮以前这里只写 { s, t }、一份身份都不带，
--- 于是 L.State 只能拿 t 跟"本轮起点"反推这算不算本轮 —— 起点判不出时兜底成周初，
--- 12.1.5 的 94612 举钳欢庆那一行 7 笔 10-22 快照就是这么在"本轮所有人都没做"的一周里
--- 长出 7 个勾的（roundAt 与 cycleAnchor 逐秒相同 = 这条记录从没滚过轮，t >= roundAt 恒成立）。
--- 从现在起写下的每一份快照都烙上当时算得出的唯一编号；判不出身份（板面倒计时没读到、
--- 周 CD 尾巴也定不出来）就留 nil —— 读侧（L.State / L.DoneByAnyCharThisRound）只比编号，
--- "没编号"直接不匹配，fail-closed（§五：不许退回"拿 t 跟起点反推"那一支）。
--- 编号要用**本次**的结束时刻，所以 Sync 那边把逐条倒计时读取提到了这里之前
--- （计划 §六 1(a)）：不然第一次上板的那笔快照烙的是"上一条实例"的编号、甚至 nil，
--- ✓ 要等下一次同步才长得出来。
--- 末位 curKey 可选 = Sync 在这一行的池循环里算好的那一份（同一把键要用到底：状态覆盖、
--- goldKey、obs.key、view.k、reads.k 五处若是各算各的，倒计时刚写进账本的那一刻就会出现
--- "判据拿旧键放行、烙进去的是新键"）。不传就自己按 t 算一次，判据一字不差。
function L.SetView(db, charKey, questID, state, t, curKey)
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    if curKey == nil then
        local rec = db.quests and db.quests[questID]
        curKey = L.InstanceKey(db, questID, rec, t)
    end
    db.chars[charKey].views[questID] = { s = state, t = t, k = curKey }
end

--- 按号记一笔"这个号自己读到这条任务的奖励是多少钱"。
--- 为什么必须按号：世界任务的奖励是**各号各自**的 —— 同一个 questID，一个号读到金币、
--- 另一个号读到物品（12.1.5 实测 97128「巢穴：尼姆瑞莎·唤波者」：Mad 给 1090 金、
--- 爱情之力给 150 宝匣钥匙碎片）。账本里原来只有 rec.currentSig / goldMoney 这一份
--- "最后一次同步的那个号看到的那一副"，于是给金币的号全刷完了，矩阵却还对没金币的号
--- 说"这一档不给金币"，对还没同步的号说"这一轮不给金币" —— 那句话根本不是它答的。
--- 只在 Sync 的池循环这一个写点落账：金额读不到（nil = 数据没到，不是"不给金币"）
--- 就不动旧数，0 是客户端明说的"这一副奖励没有金币"，照写。
--- 烙的编号用**本次**的结束时刻（写点排在 SetExpiry 之后，§六 1(a)），所以这一笔从写下
--- 那一刻起就挂在正确的实例上；判不出的那一刻留 nil ⇒ 读侧永不认（§四 R8/R9 已换编号）。
--- 末位 curKey 同 L.SetView：Sync 那一行算好的那一份，不传就自己算。
function L.SetRewardRead(db, charKey, questID, money, t, curKey)
    if type(money) ~= "number" then return false end
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    local row = db.chars[charKey]
    row.reads = row.reads or {}
    if curKey == nil then
        local rec = db.quests and db.quests[questID]
        curKey = L.InstanceKey(db, questID, rec, t)
    end
    row.reads[questID] = { g = money, t = t, k = curKey }
    return true
end

--- 这一号**这一条实例**读到的是多少金币；nil = 这一号在这条实例上没读到过它的奖励
--- （**不等于**"不给金币"，UI 显示 ?）。
--- 门与 L.State 的视图门同一口径（计划 §四 R8）：认这笔读数写下的编号。上一轮那份钱
--- 不许冒充本轮 —— 换 CD / 换实例后 reads 里那一条自然匹配不上，下一次同步自己重写。
--- 【不退回时间戳那支整删】老代码是 `t < L.RoundAt(db, rec)`：起点判不出时兜底成周初，
--- 于是上周的钱能给这一轮续命，而且开机秒 / 纪元秒跨域时"比大小"根本没意义。
--- 编号只可能由同一条时间线里的结束时刻算出来 ⇒ 时钟域分裂在这里读不出"通过"。
--- now / curKey 可选（同 L.State，§十一）：界面传自己手边的 now，取证面不传。
function L.CharRewardMoney(db, charKey, questID, now, curKey)
    local row = db and db.chars and db.chars[charKey]
    local r = row and row.reads and row.reads[questID]
    -- 没烙编号的一笔（写下那一刻身份还没定）永远不匹配（§五），连编号都不用算。
    if not r or r.k == nil then return nil end
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, db.quests and db.quests[questID],
            now ~= nil and tonumber(now) or LedgerNow(db))
    end
    if not L.SameInstanceKey(r.k, curKey) then return nil end
    return tonumber(r.g)
end

--- 这一条实例上读到过金币的号（按 charKey 排好序，UI 用它说清"上面那个钱是谁读的"）。
--- 整表一次扫角色行：只有悬停和导出会问，渲染循环里不许逐条调（同 ManualMoneyByQuest
--- 的理由 —— 14 个号 × 每行一次会把重绘拖成卡顿）。
--- 门与 L.CharRewardMoney 同一份编号（§四 R9）：上一轮那些号的钱不许凑成"本轮有人读到金币"。
--- now / curKey 可选；curKey 一算出来这一趟扫描 14 个号共用，不按号各算一次。
function L.GoldReaders(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, rec, now ~= nil and tonumber(now) or LedgerNow(db))
    end
    if not curKey then return nil end
    local out
    for charKey, row in pairs(db.chars or {}) do
        local r = row and row.reads and row.reads[questID]
        local g = r and tonumber(r.g)
        if g and g > 0 and L.SameInstanceKey(r.k, curKey) then
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

--- 注册行的角色状态：判据与 L.State 一字不差（第 36 轮 T4b 之后注册行没有第二套口径了）。
--- 常驻周常的"这一条实例"就是整个 CD —— L.InstanceEnd 的②档给的尾巴编号 = 周 CD 尾巴那一桶，
--- 所以"比编号"本身就等于以前那句"只认本 CD 内写下的证据"，锚点定不出来（0）时编号 nil
--- ⇒ 这一格如实报 ?（以前的 ThisCycle 在锚点 0 时是一进门就放行，本轮删掉的就是那个方向）。
--- now / curKey 原样透给 L.State（每行一次编号，§十一）。
function L.RegisteredState(db, charKey, questID, now, curKey)
    return L.State(db, charKey, questID, "cycle", now, curKey)
end
--- 同一族里多个 questID 取最强的一个：做过 > 在做 > 可做 > 问不出。
--- "任一 ID 答得出 done"就算 done —— 轮换任务本周换了 ID，老 ID 仍可能回 true。
--- 第 5 个参数 keyOf 是 MatrixRows 预扫那张"每条任务的编号"表（§十一）：族里每一条
--- 有自己的编号，取表里那一格传下去，一条都不许在这一格里重算。缺表（取证面）就传 nil，
--- 由 L.State 拿 now 自己算一次 —— 纯算术，且只发生在非帧路径。
local STATE_RANK = { done = 4, progress = 3, open = 2, ended = 1, unknown = 0 }

function L.BestRegisteredState(db, charKey, ids, now, keyOf)
    local best, bestRank, bestSrc = "unknown", 0, nil
    for i = 1, #(ids or {}) do
        local questID = tonumber(ids[i])
        local st, src = L.RegisteredState(db, charKey, questID, now,
            keyOf and keyOf[questID])
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
--- 刀 1（2026-10-29 回响复燃 92713 那单）加一条身份：**记录的名字命中玩家自己的
--- 关键词**。根因是两道门不同源：矩阵准入门 QuestHasCredential 认"本轮有号交过"，
--- 这一门不认 ⇒ 那行显示得出来、小号交割却整笔不记（玩家的 /gql quest 报告里 14 个号
--- 只有圣光之力有流水，就是这一岔）。关键词是玩家亲手声明"这类任务我要跟踪"，
--- 而交割事件本身是客户端答的凭据 —— 与第 32 轮"撞关键词只免金币门槛、还要一条
--- 客户端凭据"不冲突，反而补上它漏的那半。判据只认 Config.KeywordMatch 这一个入口，
--- 不许在这里再拼一遍子串匹配；玩家删了关键词这道门自动关闭（不引入新字段、不留新状态）。
--- 代价照实：DropTurninsWithoutEvidence（v3 迁移）也读这里，从此"名字撞关键词但从没
--- 上过板"的老流水不再被当"无凭据"清掉 —— 方向是要保的（那些是真交割）。
--- **不许**放宽成"账本里有记录就开门"：那正是 test_turnin_of_unknown_quest 守的那条
--- （交一条职业/战役任务凭空造行）；记录能进账本只可能来自池/日志/注册/关键词四条路，
--- 名字撞关键词是其中玩家亲手授权的那条。
function L.QuestKnown(db, questID)
    local rec = db and db.quests and db.quests[tonumber(questID)]
    if not rec then return false end
    return not not (rec.poolSeen or rec.onBoard or rec.regKey
        or rec.kwLogAdded or rec.ignored
        or (GQL.Config and GQL.Config.KeywordMatch and GQL.Config.KeywordMatch(rec.name)))
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

--- 状态判定唯一入口。**身份只有一条判据**：这一份证据写下的编号 == 这条任务当前的编号
--- （L.SameInstanceKey，§六 1(d)），没有第二道"新鲜度门"了 —— 以前 gate 选的是
--- "世界任务跟这一条实例的起点比 / 注册周常跟本周 CD 比"，编号模型里这两档合成一件事：
--- 常驻周常问不出板面倒计时，它的编号就是周 CD 尾巴那一桶（L.InstanceEnd ②）。
--- gate 因此**只剩两件事**（都不是"哪道新鲜度门"，判据一律比编号）：
---   ① 这一条一份证据都对不上时该如实报 `?` 还是报"可做/已结束"；
---   ② 注册行不要求"此刻还在池里"（锚点那条本来就不靠板面存在，池里没有它不等于没交）。
---   "cycle" = 常驻周常：报 "unknown"（UI 显示 ?）。不许退化成灰的 "—"，
---             客户端压根没答过这一条，说"没做"和说"做完了"同样是谎报。
---   其余（世界任务行） = 报 "open"（还在池里、可做）或 "ended"（不在池里、这一轮结束了）。
--- 优先级：这一条实例的手动补录 > 这一条实例的实测交任务 > 本角色这一条实例的快照 > 兜底。
--- 上一版这两份各写一遍，改一处忘一处（last.t 空值缺陷只在一半里）。
--- "unknown" 只在完全没有来源时出现，UI 必须如实显示，不得当成"没做"。
--- 注意 currentSig / 池成员关系只描述"最后一次同步的那个角色"看到的池：世界任务池
--- 是按角色各自的（狩猎模式、个人任务线都会改变它）。所以 currentSig 为空绝不等于
--- "本角色的这个任务结束了"，本角色说得出话时一律以它自己的快照为准。
--- 身份未定（curKey 算不出来 = 板面倒计时没读到、周 CD 尾巴也定不出来）⇒ **一律 "unknown"**：
--- 这是 §二 那条唯一的降级方向（判不出来 ⇒ 不成立），也是计划 §十用例 5 要钉的两面 ——
--- 格子全 `?`，但行不砍、名字与金额照旧（准入门不认 L.State 这一格）。
--- 绝不跟奖励签名比：切一次狩猎模式就翻一次签名，12.1.5 反馈"做过这个任务的，现在标记成了
--- 没做过"就是这么来的（用户 2026-10-25：这个任务并没有结束，并不是新一轮的刷新）。
--- 第二个返回值 = **这一格的答案是谁给的**：玩家手点的补录回 "manual"，点的时候客户端
--- 自己答了已完成回 "verified"，其余（实测流水 / 快照 / 池推断）一律 nil。
--- 界面靠它给手动格换颜色（用户 2026-10-25："手动补录这个，换个 √ 的颜色，
--- 一目了然哪个是手动的"）—— 只有这一处判据，别在显示层再翻一遍 db.manual。
--- 唯一的例外（2026-10-29 刀 3）：补录在前、**同一条实例的实测流水也在**时回 done + nil
--- —— 流水是客户端答的凭据，颜色映射"这一格能不能当凭据"，不许被后到的真凭据压成琥珀。
--- now / curKey 都是可选的：矩阵每行算一次编号传进来（§十一，14 个号共用同一份），
--- 右栏/Dump/用例这些取证面传 now（或都不传，走 LedgerNow 兜底那一次本地时钟读数）。
--- curKey 传 `false` = 调用方**已经算过而且算不出**（MatrixRows 预扫那张表的哨兵）：
--- 这里不再重算，直接按"身份未定 ⇒ ?"那一条走。
function L.State(db, charKey, questID, gate, now, curKey)
    local byCycle = gate == "cycle"
    local rec = db.quests and db.quests[questID]
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, rec, now ~= nil and tonumber(now) or LedgerNow(db))
    end
    -- 补录这一格也认编号（用户 2026-10-27："手动标记也是要分轮次的，这个勾不补不代表
    -- 永远完成了，世界任务都是不断轮换的"）。12.1.5 实测：89347「战胜未知：怒火攻心的漂流者」
    -- 屠戮者高地 12 小时区，10-26 14:01 换了实例，可 10-25 04:22 点的那笔补录还带着 done
    -- ⇒ 本轮一格没勾、界面照样打勾。编号是 L.SetManual 写那一刻画进记录的，这里只比它；
    -- **老存档没烙上编号的补录不匹配**（§五：不许退回时间戳门槛 —— 那一支是 fail-open
    -- 的第二张脸，`t >= 起点` 对上一条实例的证据永远成立），回填归 §九 那趟 v6 迁移。
    local manual = db.manual and db.manual[charKey]
    local m = manual and manual[questID]
    local hasM = m ~= nil
    if hasM and L.SameInstanceKey(m.k, curKey) then
        -- 刀 3（2026-10-29 回响复燃 92713 报告）：颜色的语义是用户 10-25/10-27 定的
        -- "绿=客户端/流水认的、琥珀=我自己记的"。这一支以前先看补录、后看流水，
        -- "先点一下、流水后到"的格子就永久停在琥珀 —— 实测流水在账上却报琥珀，
        -- 是映射错了。只在**补录未核验 + 补录状态是 done + 同一条实例的实测流水在**
        -- （判据与下面那支一字不差：SameInstanceKey(last.k, curKey) 且 byCycle or obs）
        -- 时改回 done + nil（绿）。其余分支一字不改：核验过的仍回 verified，没有流水
        -- 背书的仍回 manual（琥珀），test_manual_beats_auto_and_survives_sync
        -- （手动不许被自动 open 顶掉）照旧成立。小号格子点不出绿色不在这一刀范围
        -- （客户端只答当前角色），这里只保证"账本里有真凭据时颜色不许降级"。
        if not m.v and m.state == "done" then
            local tRows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
            local tLast = tRows and tRows[#tRows]
            if tLast and L.SameInstanceKey(tLast.k, curKey)
                and (byCycle or L.GetObs(db, questID)) then
                return "done", nil
            end
        end
        return m.state, m.v and "verified" or "manual"
    end
    if not rec then return "unknown" end
    if not curKey then return "unknown" end

    local obs = L.GetObs(db, questID)

    local rows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local last = rows and rows[#rows]
    -- 实测流水认编号：这一笔写下时挂的是哪条实例的编号，就只在那条实例这一轮算"实测交过"。
    -- 说不出它属于哪一条实例的流水（没烙编号 = 写下时身份还没定）绝不许冒充"实测交过"，
    -- 也不许拿 t 跟任何起点反推 —— 老档那三处反推正是 94612 十个假勾的入口。
    local fresh = last ~= nil and L.SameInstanceKey(last.k, curKey)
    -- 世界任务行要求"这一条此刻还在池里"（obs）才算实测交任务；
    -- 注册行不要求：锚点那条本来就不靠板面存在。
    if fresh and (byCycle or obs) then return "done" end

    local chars = db.chars and db.chars[charKey]
    local view = chars and chars.views and chars.views[questID]
    local viewMine = view ~= nil and L.SameInstanceKey(view.k, curKey)
    if viewMine then
        -- 快照只在覆盖到当前这一条实例时才算数，否则上一轮的"已交"会跨轮冒充本轮
        -- （这一句就是 94612 那 7 笔 10-22 快照被批掉的地方）
        return view.s
    end
    if byCycle then return "unknown" end
    -- 玩家点过补录、可它对不上这一条实例，而客户端这一份证据都没答过（不在池里、
    -- 这一条实例没有实测流水、没有这一条实例的快照）：报"没做"和报"永远已做"是同一个错的两侧
    -- —— 这一格如实就是问不出（UI 显示 ?），玩家照着 ? 再点一次就补上。
    -- 只要有任意一份对得上的凭据在，就照那份走（过期补录不许压住本轮真交过的那一笔）。
    if hasM and not obs and not fresh and not viewMine then
        return "unknown"
    end
    return obs and "open" or "ended"
end

--- 本 CD 汇总。手动已交若与实测交任务是同一条就不重复计数；
--- 有完成记录但一条实测金币都没有时 gold 返回 -1，UI 显示 "?"，绝不显示 0。
--- count 返回 -1 表示本周统计整体不可信（见下面域分裂判断），UI 同样显示 "?"。
--- cycleStart 传 0 是"不加过滤"（Ledger 不判断锚点是否存在，那是调用方的事，见 Dump）。
--- 补录那一岔的计划 §四 R12：认编号不认时间戳反推。常驻周常的编号 = 周 CD 尾巴那一桶，
--- 所以"比编号"与上面那道 CD 门是同一件事（以前那句"注册行不设这道门"就是这么来的）；
--- 世界任务行的补录跟格子的显示同一道门（见 L.State）—— 占行与统计不许两处两个口径，
--- 否则"完成 8"里混着上一轮那笔没补的勾。
function L.WeekStats(db, charKey, cycleStart, now)
    -- 锚点落在纪元秒、事件时间戳落在"本次开机秒"时两者不可比：照大小过滤会把
    -- 每条记录都判成本周之外，汇总静默变成"完成 0"。宁可报未知，也不报一个看着正常的假数。
    if cycleStart and cycleStart > 1e9 and db.clockDomain == "uptime" then
        return -1, -1
    end
    local count, gold, goldKnown = 0, 0, false
    local counted = {}
    -- 补录那一岔要比编号，而编号要问"这条实例还活着没" ⇒ 这一趟只取一次 now（一次都不许多问）
    local nowN = now ~= nil and tonumber(now) or LedgerNow(db)
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
            -- 补录跟格子的显示同一道门（见 L.State）：认这笔补录写下的编号，
            -- 不再拿 m.t 跟"本轮起点"反推（§四 R12）。注册行也不用另设豁免 ——
            -- 常驻周常的编号就是周 CD 尾巴那一桶，比编号 == 比 CD，同一件事。
            local rec = (db.quests or {})[questID]
            local stale = not L.SameInstanceKey(m.k,
                L.InstanceKey(db, questID, rec, nowN))
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

--- 金币任务清单（导出用）。判据一律走 L.GoldVerdict —— 不各写一份"只看 obs.gold"的判据。
--- now 由调用方给（Dump/Probe 手边就有）：不传的话每一条任务都要走一次 LedgerNow 兜底，
--- 一份报告问几十次本地时钟，纯属浪费（§十一那条"修法零新增接口调用"的口径也管取证面）。
function L.GoldQuestIDs(db, now)
    local out = {}
    for questID in pairs(db.quests or {}) do
        -- 走 GoldVerdict，不各写一份"只看 obs.gold"的判据：同一条实例里另一档读到过金币的
        -- 双模式任务，矩阵里有行、导出里却没有，两处报两个数最难查。
        if L.GoldVerdict(db, questID, now) == true then out[#out + 1] = questID end
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

--- 这条任务**这一条实例**报多少金币。金额优先来自当前签名的 observation，
--- 其次来自这一条实例的金币粘滞（`rec.goldMoney` + `rec.goldKey` 一对，写点是 NoteGoldThisInstance）。
--- now / curKey 可选（§十一）：矩阵每行算一次编号传进来，这里不再算第二次；
--- 没有粘滞可问的时候连编号都不算（少一次纯算术，也少一次时钟兜底查询）。
function L.QuestMoney(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    local obs = L.GetObs(db, questID)
    local m = obs and obs.money
    if type(m) == "number" and m > 0 then return m end
    -- 双模式的任务：普通模式给金币、狩猎模式换成物品（或反过来）。这一条实例里任意一档
    -- 读到过金币就一直按金币算，切档读到"不给金币"不许把行弄掉
    -- （用户 2026-10-25："之前是金币，那么也不应该移除（结束时间没变）"）。
    -- 换了一条实例这份粘滞作废，新实例重新读、重新判 —— 上个 CD 的金币赖着不走（意外遭遇）
    -- 走的就是这一条退出路，不是"当场读到非金币就删行"。
    -- 【比编号，不比起点】§四 R10：老那句 `rec.goldRound == L.RoundAt(db, rec)` 整删 ——
    -- 起点判不出时兜底成周初，上个 CD 那笔钱就永远"等于本轮的起点"（粘滞失效那条退出路
    -- 根本没在工作）。编号换了 ⇒ 粘滞自己匹配不上，写侧 NoteGoldThisInstance 第 4 步重新记。
    if rec and rec.goldMoney then
        if curKey == nil then
            curKey = L.InstanceKey(db, questID, rec,
                now ~= nil and tonumber(now) or LedgerNow(db))
        end
        if L.SameInstanceKey(rec.goldKey, curKey) then return rec.goldMoney end
    end
    return m
end

--- "这条给不给金币"的唯一口径。返回 true=这一条实例读到过金币（含双模式另一档的粘滞，
--- 见 L.QuestMoney），false=客户端明确答过非金币，nil=压根问不出（UI 显示 ?）。
--- 绝不把 nil 折成 false：那等于替客户端回答"它不给钱"，行会当场消失，
--- 而它只是这一轮数据没到（12.x 奖励数据要几十秒才回来）。
--- now / curKey 透给 L.QuestMoney（同一行只算一次编号，§十一）。
function L.GoldVerdict(db, questID, now, curKey)
    local money = L.QuestMoney(db, questID, now, curKey)
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
    -- 只剩"上一条实例读到过金币、这一条还没读到"这一种：L.QuestMoney 已经按编号拒了那份
    -- 粘滞，所以这里不会翻回"是"；也不许报"否" —— 客户端这一条根本没说它不给钱，
    -- 折成 false 等于替它答，行会当场消失（UI 该显示 ?）。
    return nil
end

--- 界面要分清话是谁说的：这笔金币是当前这一读到，还是同一条实例的另一档读到的。
--- 现在这一档不给金币却还按金币占行时返回金额，否则返回 nil。
--- §四 R11：与 L.QuestMoney 同一道编号门，两处不许两个口径 —— 一处认粘滞一处不认，
--- 界面就会出现"钱在行上、'这钱是谁给的'那一行却说没有"。now / curKey 透下去。
function L.GoldFromOtherMode(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    local obs = L.GetObs(db, questID)
    local m = obs and obs.money
    if type(m) == "number" and m > 0 then return nil end
    if rec and rec.goldMoney then
        if curKey == nil then
            curKey = L.InstanceKey(db, questID, rec,
                now ~= nil and tonumber(now) or LedgerNow(db))
        end
        if L.SameInstanceKey(rec.goldKey, curKey) then return rec.goldMoney end
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

--- 这一条实例有没有任一角色被客户端答成"已完成"（= 矩阵那一行里那些 ✓ 格）。命中任一即算。
--- 只认客户端答的那几路（池/注册行的状态读、交任务实测流水），**不认玩家手点的补录**：
--- 补录在 `db.manual` 里、`L.State` 会标 "manual"，这一处根本不读那张表 ——
--- 与"玩家口述不算凭据"同一条口径。
--- 为什么必须补这一条（用户 2026-10-27 的反弹）：96718 奈格塔尔虚空决战（英雄）真存在、
--- 他 14 个号本轮都交掉了（存档里 14 格 done 快照，时刻全在本轮起点之后），
--- 可它的上板凭据只有日志那一道 `kwLogAdded`，而那道标记的设计是"还挂在任务日志里才每轮续期"
--- —— 交完就离开日志 → 下一轮标记被撤 → 零凭据 → 行消失。**"做掉了"反而把凭据做没了。**
--- 边界只有一条（§四 R6）：**这一份快照写下的编号 == 这条任务当前的编号**。
--- 以前这里是两道门叠着（`t >= L.RoundAt` 的本轮门 + 这条记录自己的到期门），
--- 而起点判不出时兜底成周初 ⇒ 第一道门恒成立，"只在当轮有效"这句其实一直没在工作。
--- 编号自己就带"换了一条实例"：结束时刻一变旧快照立刻匹配不上，所以老那句
--- `RollRound` 推起点在这里不再有任何职责；到期门也归 §七 那条 `L.InstanceExpired`
--- （12.1.5 真存档重放实测：96718 在 10-29 06:59 那一格随到期掉出矩阵，此刻这条凭据仍答 true）。
--- 所以"上上世代那种没人碰过的垃圾行"不会因为这条活过一轮：82157/86174 实测 `done=0`
--- （只有 progress 视图），本来就不命中；而真命中过的行最长寿到自己的倒计时为止。
--- now / curKey 可选，矩阵每行只算一次编号传进来（§十一）：这一处是 14 个号共用同一份。
function L.DoneByAnyCharThisRound(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, rec, now ~= nil and tonumber(now) or LedgerNow(db))
    end
    -- 身份未定（算不出编号）⇒ 一份证据都认不出来，返回 false（不成立，fail-closed）。
    -- 行因此少一次"本轮已交"凭据 —— 掉不掉行由准入门那五道决定，不是这里。
    if not curKey then return false end
    for _, row in pairs(db.chars or {}) do
        local view = row.views and row.views[questID]
        if view and view.s == "done" and L.SameInstanceKey(view.k, curKey) then return true end
    end
    return false
end

--- 这条记录有没有"客户端今天还答得出它"的凭据（命中任一即算）。第 32 轮准入门的唯一真源：
--- 名字撞关键词只是玩家说"这个名字我要看"，不构成"这条任务还真存在"——12.1.5 实测
--- 82157 特别任务：巨兽崛起 / 86174 特别任务：风暴在即 两条上上世代的垃圾行就是靠 kwHit
--- 一张名字通行证挂在矩阵里，还顶着当时 SetCycleExpiry 给的"下次周重置"倒计时
--- （那一组排程字段已在 T5 整组退役；计划 §2.1）。
--- 凭据集合全部是账本里已有的字段，零新增接口调用：进过世界任务池（poolSeen）/
--- 关键词+任务日志收进账本（kwLogAdded）/ 注册过周常档（regKey）/ 读到过奖励金币
--- （lastMoney 是数——含 0：客户端答过"这条不给金币"也是答过）/ 客户端自己答的图
--- （mapID 且非玩家口述）/ **这一条实例上任一角色被客户端答成已完成**
--- （L.DoneByAnyCharThisRound，§四 R6：认编号，不认"时刻在本轮起点之后"）。
--- **玩家口述的地图（mapManual / mapSrc=玩家指定）不算**：那是"在哪办"的备注，
--- 不是"这条任务还真存在"的证据（12.1.5 实测：93013/94391/94795/96307 四条旧特别任务
--- 就是靠 mapSrc=玩家指定 + 名字撞关键词赖在界面上，计划 §2.2）。口述机制 2026-10-29
--- 已下线、老档由 v8 迁移清；这条拦截留给迁移失败重试的窗口期，口径不动。
--- now / curKey 可选，透给最后那一岔（前面几支都是现字段，不需要编号）。
function L.QuestHasCredential(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    if rec.poolSeen or rec.kwLogAdded or rec.regKey then return true end
    if type(rec.lastMoney) == "number" then return true end
    if rec.mapID and not rec.mapManual then return true end
    return L.DoneByAnyCharThisRound(db, questID, now, curKey)
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

--- 任一角色**这一条实例**交过这条 = 玩家亲手拿金币换过它，比任何接口答的金额都硬。
--- 12.1.5 反馈④"松露大乱斗只有在狩猎模式下看才是金币任务，所以当一个角色完成一个符合条件
--- 的金币任务时，至少要加入矩阵"：那条的 962 金只有鼠标悬停在地图上看得到，接口侧
--- GetQuestLogRewardMoney 回 0、HaveQuestRewardData=false、另两个候选入口 MISSING ——
--- 金额这道闸门永远判不出它，于是"做过的角色连行都看不见"。
--- 这道门从第 35 轮的"按轮次收口"再收一次（§四 R7）：**只认这笔流水写下的编号**。
--- 为什么"按轮次"那句在这里其实一直没生效：轮次起点判不出时兜底成周初，于是
--- `t >= 起点` 对本 CD 任何一笔都成立；另一半 `cycle == db.cycleAnchor` 在锚点定不出来
--- （=0）时一进门就放行 —— 94612 那十个假勾的两个入口都在这一段里。
--- 现在这两道门合成一道比编号：编号跟着结束时刻走，换了一条实例旧流水立刻匹配不上；
--- `cycle` 那一格**留着只给 /gql quest 取证**（计划 §六 第 2 条），判据一字不读它。
--- 周常照旧不受影响：常驻周常问不出板面倒计时，它的编号就是周 CD 尾巴那一桶
--- —— 比编号 == 比 CD，同一件事，不需要另设豁免。
--- 第 25 轮起这道门故意不看金额；第 35 轮用户把口子收窄成"接和交两处客户端都答了 0 金币"
--- 才不放行，任何一处答不出照旧放行。放不放行一律问 L.TurnInAdmits（唯一真源），这里只把
--- "这一条实例交过"（on）与"那几笔到手金额"（gain）从同一次遍历里聚出来，别处不许再翻账本。
--- gain 的优先级三条一条都不能反（反一条就等于"一个号没量到钱"替全队否决这一行）：
---   任一笔 g>0 → 那个正数（本来就该在列，不能被别号的 0/nil 顶掉）；
---   否则有一笔 g 不是数字 → nil（判不了——窗口被邮箱/拍卖污染时 Sync 传的就是 nil，
---     当成 0 会把松露大乱斗那种读数失败直接掉板）；
---   否则每笔都是数字 0 → 0（"接和交都答 0"才可能否决的唯一一档）。
--- on 的语义与第 25 轮那份逐字相同，只是不再提前 break：金额要扫全表才聚得出。
--- 性能（§十一）：`now` 在循环外定一次，每条任务的编号只算一次（同一条任务的流水散在
--- 14 个号里，逐笔重问就是把纯算术乘上角色数）；调用方不传 now 才走 LedgerNow 那一次本地时钟。
--- keyOf：矩阵那一趟已经把整张「任务 → 编号」的表预扫好了，直接传进来这一趟一次都不重算；
--- 别的调用方不传，就在这里自建同一张表（false = 算过、算不出）。
local function TurnInCred(db, now, keyOf)
    local on, gain = {}, {}
    local pos, unknown = {}, {}
    local nowN = now ~= nil and tonumber(now) or LedgerNow(db)
    local memo = keyOf or {}
    for _, byQuest in pairs(db.turnins or {}) do
        if type(byQuest) == "table" then
            for questID, rows in pairs(byQuest) do
                if type(rows) == "table" then
                    -- false = 算过、算不出（身份未定）；拿它当哨兵免得每条任务重问一次时钟。
                    local curKey = memo[questID]
                    if curKey == nil then
                        curKey = L.InstanceKey(db, questID, (db.quests or {})[questID], nowN)
                        memo[questID] = curKey or false
                    elseif curKey == false then
                        curKey = nil
                    end
                    for i = 1, #rows do
                        local r = rows[i]
                        if type(r) == "table" and L.SameInstanceKey(r.k, curKey) then
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

--- 同一件事的 CD 口径，**只给取证用**（§六 第 2 条：`cycle` 降级成报告字段）：
--- 报告要分得清"这一条实例交过所以占行"和"本 CD 早前那几轮交过、这条匹配不上 ⇒ 已经不占行"，
--- 后者正是 12.1.5 截图那一条的形状。占行一律问上面那份编号口径，这里不参与任何判据。
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
-- 两份都导：占行只认编号那份（TurnInCred），CD 那份（TurnedInThisCycle）留给报告解释
-- "本 CD 交过但这条实例匹配不上"。
-- TurnedInThisRound 只是 TurnInCred 的 .on 薄壳：返回形状（{[questID]=true}）一字不许变，
-- Dump、Options、用例都在读它；金额那份另开 L.TurnInGainsThisRound，同一份表同一趟遍历。
-- now 透进去（取证面手边就有）：不传的话 TurnInCred 自己问一次本地时钟兜底。
L.TurnedInThisRound = function(db, now) return TurnInCred(db, now).on end
L.TurnInGainsThisRound = function(db, now) return TurnInCred(db, now).gain end
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
--- 【第 36 轮 T4b 之后这一段还剩什么用】粘滞的读法已经换成编号（L.QuestMoney 只认
--- `rec.goldKey` 对得上的那一份），这里清的 `goldRound` 从此没有任何读者 ——
--- 档位闸门（stickyGoldReset>=2）决定它今天基本不再跑，留着只为 v1/v2 存档能连续升上来；
--- 那份 `-1` 占位签名的清理（来源侧）仍然是真判据要的，别把它当历史代码删了。
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
--- 判据当时改成"本轮点过才算"（换轮清 —— 那套机器第 36 轮 T4b 已整删），但**已经写进存档的那一份不会自愈**：
--- 换轮只在"这一条实例换了一条"那一刻发生，不清就得等下一次 12 小时刷新才看得见效果，
--- 玩家会当成"改了没生效"二次报修（上一版 ResetStickyGoldOnce 就是这么栽的）。
--- 只清标记，不替准入门判活：本轮读到过金币的、命中关键词的、注册周常、本 CD 交过的
--- 照旧在列。代价照实：只剩这一条凭据的行清完就掉出矩阵，玩家还想要就得再点一次添加
--- （本轮有效）。真存档那 5 条 manualAdded 逐条查过 —— 只掉「意外遭遇」一行，
--- 另外 4 条靠关键词/本轮金币撑着，或到期时刻早过了。
--- （本档口径已被 v5 取代一轮：2026-10-27 `manualAdded` 整个字段下线，
--- "本轮点过才算"改成"点了什么都不算"，换轮也不再清它。这段留着是历史原貌 ——
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

--- 一次性清掉老存档里**玩家口述的地图**（schema v8）：口述机制（Api.MAP_MEMORY）
--- 2026-10-29 整门下线 —— 口述表是硬编码在源码里的映射，没有配置入口，下个资料片
--- 必然整体失效成垃圾代码片段（用户原话："我们尽量减少配置，用代码去规范数据"）。
--- 写入点已全删（SetManualMap / ApplyMapMemory），这趟迁移负责把已经写进 SavedVariables
--- 的那份也清干净，不然那批行会顶着"玩家指定"的图号用到永远：
--- ① mapSrc 是"玩家指定"的：mapID/mapSrc 一起清 —— 口述图不是客户端答复，
---    清掉之后该落"周常"组的落"周常"组、该落"未知地图"的落"未知地图"，宁缺不编。
--- ② 只残留 mapManual 标记、mapSrc 已被客户端来源覆盖的：**图保留**（客户端真凭据
---    比口述新，是 DiffPool/补问后来重写的），只抹掉没人再写的旧标记。
--- 返回值 = 清掉的地图条数（①的条数；②只算标记不算图）。
function L.ClearManualMaps(db)
    if not db or not db.quests then return 0 end
    local manual = (GQL.Config and GQL.Config.MAP_SRC and GQL.Config.MAP_SRC.manual) or ""
    -- 标签只认 Config.MAP_SRC 那一份出处（取不到就回 ""，"" 不匹配任何来源 =
    -- 迁移静默不生效，跟 MapSrc 的口径一致）；来源串禁令的静态扫描也对 Config.lua 豁免，
    -- 这里**不许**抄一份字面量兜底 —— 抄了哪天标签改了，这条迁移就永远认不出老形状。
    local n = 0
    for _, rec in pairs(db.quests) do
        if type(rec) == "table" and rec.mapManual then
            rec.mapManual = nil
            if rec.mapSrc == manual then
                rec.mapID, rec.mapSrc = nil, nil
                n = n + 1
            end
        elseif type(rec) == "table" and rec.mapSrc == manual then
            rec.mapID, rec.mapSrc = nil, nil
            n = n + 1
        end
    end
    return n
end

--- 一次性清掉**池任务上没有流水背书的"已完成"快照**（schema v9，2026-10-29 健步如飞那单）：
--- 固定 ID 任务的客户端完成 flag 会跨轮残留，快照曾把这份答案原样写进各号 views；
--- 任务结束退出池子后（kwLogAdded 行还豁免"写 ended"的清扫），旧 done 快照冻结、
--- 编号仍匹配，矩阵就永远绿着 —— 9 个本周完成数 0 的号全亮"已交"就是这么来的。
--- 判据与快照写入的门（Sync 同轮加的那道）/点击核验同一形状：view=done 且该号名下
--- **最新一笔流水的编号对不上这份快照** → 删快照（比最新一笔与 State 的实测判定同口径）。
--- 范围只动 rec.poolSeen 且非 rec.regKey 的任务：注册周常的状态另有回问路径，
--- 不在这档范围；非 done 快照、有流水背书的（真交过）、非池任务一律不动。
--- 返回值 = 删除的快照条数。
function L.DropUnbackedDoneViews(db)
    if not db or not db.quests or not db.chars then return 0 end
    local n = 0
    for charKey, ch in pairs(db.chars) do
        if type(ch) == "table" and type(ch.views) == "table" then
            for questID, view in pairs(ch.views) do
                if type(view) == "table" and view.s == "done" then
                    local rec = db.quests[questID]
                    if type(rec) == "table" and rec.poolSeen and not rec.regKey then
                        local rows = db.turnins and db.turnins[charKey]
                            and db.turnins[charKey][questID]
                        local last = rows and rows[#rows]
                        if not (last and L.SameInstanceKey(last.k, view.k)) then
                            ch.views[questID] = nil
                            n = n + 1
                        end
                    end
                end
            end
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

--- 一次性给老存档里**没烙过编号**的完成凭据补上「任务号 + 结束时刻」的唯一编号（schema v6，
--- 计划 §九；用户 2026-10-28 原话"旧的数据如果没存，都要修改"）。
--- 为什么这一刀必须排在读侧前面（T3 → T4，顺序定死）：§五 把"没键才退回时间戳"那一支整支删掉，
--- 老存档里每一笔凭据都没有 k ⇒ 读侧一比就不等 ⇒ 部署那一次同步会把真档 20 行里 19 行的好勾
--- 连带 94612 那 10 笔假勾一起清光，而他只要求"挂不上的删掉"。这一趟先配好钥匙，
--- 读侧切换（T4b）落地时数据已经全带编号 —— 换门那一次界面才会"只有该掉的掉"。
---
--- 【只烙不删：一个老字段都不许在这里清】§九第 4 条。T3 写这一段时 rec.roundAt / goldMoney /
--- goldRound / obs.firstSeen **还是**读侧的判据，那里删它们 = 让还没换的读侧当场退回
--- "判不出这一条什么时候开始 ⇒ 就当本周初一直都在"的 fail-open ⇒ 94612 那十个假勾原地复活。
--- T4b 把读点删干净之后这里照旧不删，理由换了两条，一条都不能省：
---   · 起点边界（下面那段）还要拿 `rec.roundAt` 当"这条记录自己滚过轮没有"的取证依据；
---   · 用例 test_v6_backfill_drops_only_the_unprovable_marks 钉着"v6 只烙不删"（§十 第 8 条②），
---     它是那条越界删除的变异检查。§九 4 说 T4 可以顺手清老档残留，本刀没做（清了既过不了
---     上面那条、也让变异检查失去意义）；真要清，排在下面那次取证用完之后。
---
--- 【起点读原始字段，绝不问"兜底过的起点"】老 `L.RoundAt`（第 36 轮 T4b 已整删）判不出时
--- 兜底成 db.cycleAnchor **并把这个兜底值写回 rec.roundAt**。迁移要是经过那种带兜底的读法，
--- "这条记录从没滚过轮"就被做成"从周初一直在"的既成事实落进存档：正是要判掉的那一支
--- 被自己造了出来，而且下次登录连证据都没了。今天读的是**原始字段**，没有兜底那一半。
---
--- 【两个 setup，Config.Init 现有的顺序给不了】§九第 0 步：
--- ① 锚点必须是本 CD 的：Init 只在 cycleAnchor==0 时才 RefreshCycle，老存档带着**上一个 CD** 的
---    锚点进来（周四刚过、代码刚更新的那一次登录最典型）⇒ 尾巴档那 9 行（真档实测 92636/90962/
---    93244/97128/95918/90573/89507/98172/96718，全都没有 roundEnd）会全部烙成上周的编号，
---    常驻周常的勾每周四掉一次。⇒ 锚点已经过去（anchor + 604800 <= 同域的 now）时补问一次
---    Config.RefreshCycle。这是全仓唯一一处允许在身份路上问接口的地方：**只在"有档要升 +
---    锚点确实过期"那一次登录问一次**，RefreshCycle 本身单调（只把锚点往前推，绝不回退），
---    稳态一次都不问。矩阵每帧那条路（L.InstanceEnd / L.InstanceKey）照旧零接口调用 ——
---    用户 10-27 的硬规矩（修法不许加接口调用、不许轮询、不许扫描）。
--- ② now 要与 rec.roundEnd 同域：L.InstanceEnd 跨域不收板面值（判不出就 nil），域挑错是
---    fail-closed —— 不造假勾，但会**白掉真勾**，这种损失没有日志、只能靠这条注释拦住下一个人。
---    所以 Api.WallClock / Api.Uptime 在迁移开头**各取一次**（一次性升级里总共两次），逐条记录
---    按 rec.roundEnd > 1e9 挑同域那个传进去；尾巴档不跟 now 比大小，传哪个都一样。
---    调用方没给时钟才自己问（与 Config.RefreshCycle 同一口径）：Ledger 平时是纯账本，
---    这两次查询只存在于这一趟升级里。
---
--- 【起点边界：这一笔凭什么算这一条实例的】
--- · 板面档（rec.roundEnd 有值 = 这条答出过板面倒计时）⇒ 只认这条记录**自己滚过的** roundAt，
---   即存在且 **既不等于补问后的锚点、也不等于补问前的那个锚点**（下面 setup ① 会补问一次，
---   只比新锚点会把上周的污染值当成"自己滚过的起点"）。等于锚点或压根没有 = 从没滚过轮
---   = 老档里没存过这一条实例几点开始
---   ⇒ 判为挂不上，一笔不烙。真档证据：94612 举钳欢庆的 roundAt 与 cycleAnchor 逐秒相同，
---   它那 7 笔 10-22 快照 + 3 笔 10-22 补录全是上一条实例的证据，用户已批准
---   （"举钳欢庆 挂不上才是正常的，因为我最开始就说了 本轮次还没人做过"）。
--- · 尾巴档（rec.roundEnd 为空 = 压根没答出过倒计时的常驻周常组，用户点名"先按周CD的时间算"）
---   ⇒ 起点 = db.cycleAnchor，这一档的本轮就是整个 CD ⇒ 上面那 9 行的勾一笔都不许掉。
--- 时钟域不可比（t 是开机秒、起点是纪元秒）时**不烙**：宁缺不编。
---
--- 【数据一条都不删】用户批准的是"勾挂不上"，不是"抹掉流水"：db.turnins 是金币账本，94612 那些笔
--- （如果有）真在这个 CD 里拿到过钱，删了"本周金币"就少报。无编号 = T4 之后不再产生 ✓、
--- 不再算"本轮交过"、不再算上板凭据，效果与他要的"全部删掉"一致而代价小一档。
--- 身份未定（curKey 算不出来）的记录一笔都不回填、也不计入下面那两笔账：那不是"判为更早"，
--- 是另一档"挂不上编号"，Dump/Probe 侧单独报（§八）。
--- 返回值 = 烙上的笔数（Config.Migrate 那句通用日志用它）；两笔账另外单独记一行，
--- 因为"判为更早 Y 笔"必须能在 /gql quest 里逐笔追认是谁，只看总条数查不出来。
function L.BackfillInstanceKeysOnce(db, wallNow, uptimeNow)
    if not db then return 0 end
    local wall, up = wallNow, uptimeNow
    if wall == nil and GQL.Api and GQL.Api.WallClock then wall = GQL.Api.WallClock() end
    if up == nil and GQL.Api and GQL.Api.Uptime then up = GQL.Api.Uptime() end
    wall, up = tonumber(wall), tonumber(up)

    local anchor = tonumber(db.cycleAnchor)
    local anchorBefore = anchor        -- 补问之前那个锚点也要拿来认污染值，见下面"起点"那一段
    if anchor and anchor > 0 then
        local ref = (anchor > 1e9) and wall or up
        if ref and (ref > 1e9) == (anchor > 1e9) and anchor + 604800 <= ref
            and GQL.Config and GQL.Config.RefreshCycle then
            GQL.Config.RefreshCycle(wall, up)
            anchor = tonumber(db.cycleAnchor) or anchor
        end
    end

    local stamped, judgedEarlier = 0, 0
    for questID, rec in pairs(db.quests or {}) do
        if type(rec) == "table" then
            local endAt = tonumber(rec.roundEnd)
            local now = endAt and ((endAt > 1e9) and wall or up) or (wall or up)
            local curKey = L.InstanceKey(db, questID, rec, now)
            local startAt
            if curKey then
                if endAt then
                    local rolled = tonumber(rec.roundAt)
                    -- 两个锚点都不许当起点：L.RoundAt 判不出时把"当时账本里的 cycleAnchor"
                    -- 写回 roundAt，那是"就当本周初一直在"的污染值，不是这条实例的起点。
                    -- 补问 RefreshCycle 之后锚点前进了一个 CD，老污染值还停在上一周 ⇒
                    -- 只比"≠ 新锚点"会把上周锚点当成"自己滚过的起点"，把上周的勾烙进本周的
                    -- 编号 = 本轮要消灭的那类假勾，而且是周四刚过、代码刚更新那一次登录才发作。
                    if rolled and rolled > 0 and rolled ~= anchor and rolled ~= anchorBefore then
                        startAt = rolled
                    end
                elseif anchor and anchor > 0 then
                    startAt = anchor
                end
            end
            if curKey then
                local function mark(node)
                    if type(node) ~= "table" or node.k ~= nil then return end
                    local t = tonumber(node.t)
                    if startAt and t and (t > 1e9) == (startAt > 1e9) and t >= startAt then
                        node.k = curKey
                        stamped = stamped + 1
                    else
                        judgedEarlier = judgedEarlier + 1
                    end
                end
                for _, byQuest in pairs(db.turnins or {}) do
                    local rows = type(byQuest) == "table" and byQuest[questID] or nil
                    if type(rows) == "table" then
                        for i = 1, #rows do mark(rows[i]) end
                    end
                end
                for _, byQuest in pairs(db.manual or {}) do
                    if type(byQuest) == "table" then mark(byQuest[questID]) end
                end
                for _, row in pairs(db.chars or {}) do
                    if type(row) == "table" then
                        if type(row.views) == "table" then mark(row.views[questID]) end
                        if type(row.reads) == "table" then mark(row.reads[questID]) end
                    end
                end
            end
        end
    end
    if GQL.Config and GQL.Config.Log then
        GQL.Config.Log(("v6 回填 %d 笔 / 判为更早 %d 笔"):format(stamped, judgedEarlier))
    end
    return stamped
end

--- v7 一次性清理（用户 2026-10-28："哪些 expiresAt 的老任务是不是早就到期了，那就全部删除吧"）：
--- T4a 换门后新模型只认 roundEnd/周 CD 尾巴，老 expiresAt 那组在真档里 218/310 条还在，
--- 过期值全成了"尾巴档"复活的燃料（真档重放 19 行 → 27 行，死了几天的任务顶着
--- "下周重置才到期"的假倒计时回来）。这一趟把遗产清干净，之后存档里不再有
--- "只剩过期 expiresAt"的记录：
--- ① 已过期 + 非 regKey/kwLogAdded/ignored ⇒ 整条删除（db.quests[questID] = nil，
---    与 L.Prune 同一道口径：turnins/views 金币流水一条不删；ignored 删了
---    "别再显示"的意图就丢了，三周后它自己刷回来）；
--- ② 未过期且没有 roundEnd ⇒ roundEnd 盖章为该 expiresAt（一次性转成新模型唯一真源，
---    96718"时间结束后都要移除"在周重置那一刻照旧成立）；
--- ③ regKey/kwLogAdded 不转也不删（按 T5 设计走周 CD 尾巴；盖了章，下周重置一过它
---    就是过期墓碑，会把用户点名要的周常压死）；已有 roundEnd 的未过期记录不动
---    （roundEnd 是唯一真源，expiresAt 只是随行老值，94612 形状）。
--- 时钟只认墙上域（expiresAt 是纪元秒）：调用方没给就问一次 Api.WallClock（v6 同款，
--- 一次性升级里的一次查询）；拿不到或跨域 ⇒ 一条不动返回 0，宁缺不编
--- （与 Prune 的同域守卫同一句话）。幂等：清完的档重跑 0 条，转换只认没有 roundEnd 的，
--- 不会盖第二次。返回 = 删除的条数（进 Migrate 的通用日志），转换条数单独记一行。
function L.PurgeExpiredLegacyOnce(db, wallNow)
    if not db or type(db.quests) ~= "table" then return 0 end
    local wall = tonumber(wallNow)
    if wall == nil and GQL.Api and GQL.Api.WallClock then
        local okCall, v = pcall(GQL.Api.WallClock)
        wall = okCall and tonumber(v) or nil
    end
    if not wall then return 0 end
    local purged, converted = 0, 0
    -- 循环变量不叫 rec：这里读的 expiresAt 是退役字段（不在 QuestRecordFields 清单里），
    -- 本函数是它唯一的合法读点，按工厂守卫的口径用局部变量名豁免，不把退役字段塞回清单。
    for questID, legacyRec in pairs(db.quests) do
        if type(legacyRec) == "table" then
            local exp = tonumber(legacyRec.expiresAt)
            if exp and exp > 0 and (exp > 1e9) == (wall > 1e9) then
                if exp < wall then
                    if not legacyRec.regKey and not legacyRec.kwLogAdded and not legacyRec.ignored then
                        db.quests[questID] = nil
                        purged = purged + 1
                    end
                elseif not legacyRec.roundEnd and not legacyRec.regKey and not legacyRec.kwLogAdded then
                    legacyRec.roundEnd = exp
                    converted = converted + 1
                end
            end
        end
    end
    if GQL.Config and GQL.Config.Log then
        GQL.Config.Log(("v7 清理过期 %d 条 / 转换 roundEnd %d 条"):format(purged, converted))
    end
    return purged
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
--- 只有"接（奖励读）"和"交（到手金额）"两处客户端都答了 0 金币才砍；任何一处答不出照旧放行。
--- now / curKey 末两位是第 36 轮加的：这两道门里"金币"和"凭据"都改问编号（§四 R10/R6），
--- 而编号每行只许算一次（§十一）⇒ 由 MatrixRows 的预扫传进来，这里只往下透。
function L.RowAdmitted(db, questID, rec, money, kwHit, turnedIn, floorMoney, gainOf, now, curKey)
    if not rec then return false end
    if rec.kwLogAdded or rec.regKey then return true end
    if L.TurnInAdmits(turnedIn and turnedIn[questID], gainOf and gainOf[questID],
        L.GoldVerdict(db, questID, now, curKey) == false) then return true end
    if money and money >= (floorMoney or L.MatrixMoneyFloor()) then return true end
    return not not (kwHit and L.QuestHasCredential(db, questID, now, curKey))
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
    -- 编号要用那一刻的时钟，**在循环外定这一次**（§十一"MatrixRows 在循环外把 now 定下来"）：
    -- 重绘路径调用方就传了 now（矩阵自己先问的那一次），这里等于不花任何新接口调用；
    -- 只有不带 now 的取证跑法会兜一次本地时钟 —— 每趟一次，不是每行一次。
    local keyNow = tonumber(now) or LedgerNow(db)
    -- 身份编号和金额/名字一起预扫：**每行每帧只算一次**（§十一 —— 14 个号 × 20 行 × 每帧
    -- 是 280 次，行内一次是 20 次；L.InstanceKey 虽是纯算术，也不许按格子乘）。
    -- `false` 是哨兵"这一条算不出编号（身份未定）"，往下传给 L.State 时它不再重算，
    -- 直接按 fail-closed 报 ?；表里没这个 id 才是"没算过"（族兄弟不在 db.quests 里那种，
    -- 理论上没有，防一手），由被调方拿 now 自己算一次。
    local moneyOf, nameOf, keyOf = {}, {}, {}
    for i = 1, #ids do
        local questID = ids[i]
        keyOf[questID] = L.InstanceKey(db, questID,
            db.quests and db.quests[questID], keyNow) or false
        moneyOf[questID] = L.QuestMoney(db, questID, keyNow, keyOf[questID]) or manualOf[questID]
        nameOf[questID] = L.QuestName(db, questID)
    end
    -- 交过的两份表同一趟遍历出来：on 占行、gain 给第 35 轮那道"两处 0 才否决"的门用。
    -- 每帧不许各翻一遍 db.turnins（T1 立这一处遍历就是为了这条）—— 编号也一样：
    -- 预扫好的 keyOf 直接传给它，这一趟对同一行不会二次算编号。
    local turnCred = TurnInCred(db, keyNow, keyOf)
    local turnInOf, gainOf = turnCred.on, turnCred.gain
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
        -- 这一行的身份：预扫里算好的那一份（false = 算不出 = 身份未定），整行 14 个格子共用。
        local curKey = keyOf[questID]
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
            and L.RowAdmitted(db, questID, rec, money, kwHit, turnInOf, floorMoney, gainOf,
                keyNow, curKey) then
            -- 到期即移除：账本里的记录仍在（快照/prune 的事），只是不再显示。
            -- 判据换成这条实例自己的结束时刻（L.InstanceExpired，计划 §七）：
            -- 一个已经过去的到期时刻是**上一条实例**留下的，这一条不作数
            -- （89267 那句"第二个玩家登录了几分钟，矩阵出现了"就是它把整行吃掉的），
            -- 而这一条自己到点照旧死行（96718"时间结束后都要移除"）。
            -- 两道门都只问 `L.InstanceEnd` 那一个真源，各自再挑自己要的那半：
            -- 到期门要"它几点结束、过没过"（过去值也算），remaining 只准报还没过去的
            -- —— 两处要拼成同一句答案，不许谁自己去拼第二份到期账（老 rec.expiresAt
            -- 那一组含编的排程值，T5 已整组退役），那会出现
            -- "行还在、悬停写剩余 已到期"这种自己跟自己对不上（本轮要消灭的谎报之一）。
            local expired = L.InstanceExpired(db, rec, now)
            local exp
            if not expired then
                exp = L.EffectiveExpiry(db, rec, now)
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
                    remaining = (now and type(exp) == "number")
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
                        st, src = L.BestRegisteredState(db, chars[c], siblings, keyNow, keyOf)
                    else
                        st, src = L.State(db, chars[c], questID, nil, keyNow, curKey)
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

--- 下次池刷新时刻 = 账本里**这一条实例自己还没过去的**结束时刻里最早的那一个（世界任务到点才换：
--- 一个都没到期 = 池子不可能变，没必要去抓）。一条到期时间都没有
--- （从没同步过、或奖励/时间接口没给过数）→ nil，调用方按"该抓了"处理。
--- 上一条实例留下的那条过期值不参与（同一个真源 L.EffectiveExpiry，只回 > now 的那一个）：
--- 它会把"最早到期"永久钉在过去，等于定点唤醒的兜底整条停摆 —— 全是过期值时照样回 nil，
--- 调用方（Sync 那句"到期已过/取不到 ⇒ 不自动安排"）一个字不改，不新增任何定时器。
--- `now` 由调用方给（Sync 那两个调用点本来就先问了 Clock()）：这里不许自己再问一次时钟，
--- 零新增接口调用是用户 10-27 定的硬约束。
function L.NextRefreshAt(db, now)
    local best
    local quests = db and db.quests or {}
    for _, rec in pairs(quests) do
        if type(rec) == "table" then
            local exp = L.EffectiveExpiry(db, rec, now)
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
