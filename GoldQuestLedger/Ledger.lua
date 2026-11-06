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
--- 事件 pin 上"客户端给了真任务号 + 奖励金币过了门槛"那一路（Sync.RefreshEventPins）
--- 的注册前缀。与 kw: 分开是刻意的：删关键词不许退掉金币门自己收进来的行 —— 它从来
--- 不是玩家点名要的，/gql quest 取证也能从 regKey 前缀一眼看出这条行是哪条路收的。
L.PIN_PREFIX = "pin:"

--- regKey 是不是事件 pin 那一路收进来的。**按位置比前缀**（sub），不用 find：find 不比
--- 位置，内置那种 `w:…` 之外还有谁手滑写了含 "pin:" 的键就会被判成 pin 行。
--- 这一句是"驻板豁免给不给"的唯一入口（到期门 L.InstanceExpired / 时清 L.Prune /
--- 注册换键 L.EnsureRegistered 三处都问它），判据和写入方必须认同一个串。
--- 为什么要按前缀切开：用户 2026-11-02 拿"暗影汇聚之地"（奖励 956 金）定的口径原话
--- 「它是普通的世界金币任务，在屠戮者高地（属于虚影风暴），当然要收录，
---  和其他普通金币任务一样。到期就要自动消失」⇒ pin 收的行不配任何特殊驻板待遇，
--- 而 kw:（玩家敲关键词点名）与内置周常（代码里声明的整周常驻族）继续豁免。
function L.IsPinRegKey(key)
    local pre = L.PIN_PREFIX
    return type(key) == "string" and key:sub(1, #pre) == pre
end

--- 这把注册键是不是"整周常驻"那一路（玩家敲的 `kw:` / 代码里声明的内置周常族）。
--- 它是 L.IsPinRegKey 的反面，收成一句话而不许在调用点各拼一遍 `regKey and not IsPin..`：
--- 用户 2026-11-02 那句「和其他普通金币任务一样。到期就要自动消失」落在判据上就是
--- "pin: 那一路不配任何常驻待遇"，而这份待遇今天散在两道门上（准入门 ① 那支白拿绕金币
--- 门槛、上板凭据把 regKey 算一份）。拼串分岔的症状就是同一行"一会儿在列一会儿不在列"，
--- 正是本轮一直在消灭的那类分歧。到期/时清那两处直接问 L.IsPinRegKey，语义是"豁免给不给"，
--- 不在这一句的复用范围里。
function L.IsResidentRegKey(key)
    return type(key) == "string" and key ~= "" and not L.IsPinRegKey(key)
end

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
    -- 归属滚动（2026-10-31 用户口径："任务归属不要展示子地图，展示用户添加地图"）：
    -- 任务挂在子图上时分组头用玩家加的那张图 —— "暗影汇聚之地"挂 2444 屠戮者高地、
    -- 玩家关注的是 2395 虚影风暴：面板冒出玩家没加过的"屠戮者高地"分组等于替他造
    -- 关注。TrackedOwnerMapID 对玩家直接加的图原样返回它自己，无主图返回 nil →
    -- 原图照旧（渲染层按"不在关注集合"滤整组，不会真冒出来）。会话缓存，零新增接口。
    local function roll(m)
        if m and GQL.Api and GQL.Api.TrackedOwnerMapID then
            local owner = GQL.Api.TrackedOwnerMapID(m)
            if owner then return owner end
        end
        return m
    end
    if type(rec.evMapID) == "number" and rec.evMapID > 0 then return roll(rec.evMapID) end
    if L.IsResidentWeekly(db, questID) and not L.ClientMapOf(rec) then
        return WEEKLY_GROUP_MAP
    end
    return roll(rec.mapID)
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
    -- 别处不许再各拼一套判据 —— 上一版就是 obs.gold、lastMoney 两处各读各的
    -- goldKey = 第 36 轮"任务号+结束时刻"唯一编号（L.InstanceKey），只由 L.NoteGoldThisInstance
    -- 在 Sync 池循环里写（SetExpiry 之后才用得上这一条实例自己的结束时刻），与 goldMoney 同进同退：
    -- 编号换了旧的 goldMoney 一起作废（§六 1(c) 第 4 步），读侧（L.QuestMoney / GoldFromOtherMode）
    -- 只认 sameKey(rec.goldKey, 当前编号) 的那一份。
    -- goldRound = 老"轮次起点"那一份粘滞标记，T4b 起**没有任何判据读它**了；
    -- 只剩历史清理 L.ResetStickyGoldOnce 会清它（清单留着就是为了字段守卫不许谎报）。
    observations = true, currentSig = true, lastMoney = true,
    goldMoney = true, goldRound = true, goldKey = true,
    -- 到期：roundEnd = 这一条实例自己答出的结束时刻（身份与到期共用这一个真源，
    -- 唯一写点 L.SetExpiry / Sync 日志路；§五 的命名决定：本轮不改名）。
    -- 老那一组 expiresAt/expiresSrc/expiresGuessed（含 24 小时排程值）T5 起**已整组退役**：
    -- 无写点、无读者，连取证面也不再问来源；老档里残留的值原地留着，谁也不读它。
    -- 所以它们不在清单里 —— 字段守卫扫到任何 `rec.expiresAt` 一类读写都会报红，这正是想要的。
    -- roundAt = 老"本轮起点"，T4b 起**没有任何判据读它**（L.RoundAt 整套已删 —— 它判不出时
    -- 把兜底值写回 rec.roundAt，那句"判不出就当本周初一直在"就是 94612 十个假勾的根因）。
    -- 只剩 §九 的 v6 迁移拿它当"这条记录滚没滚过轮"的取证依据，以及老档残留等迁移清。
    roundAt = true, roundEnd = true, goneAt = true, highFreq = true,
    -- 身份与准入：regKey = 注册周常的键；added 类标记分开存（玩家点过 vs 插件从日志收的）
    -- kwLogAdded = "**此刻同步的这个号**日志里挂着它"（live 事实，换号登录当场撤，Sync 收尾那道）；
    -- kwLogKey = "**这一条实例**被某个号的日志证明存在过"（第 36 轮那份编号，唯一写点
    -- L.NoteKeywordLogThisInstance，唯一读点 L.KeywordLogAdmits）—— 梦魇任务 94446 那一单：
    -- 只有前者时一个没接这条的号登录就把整行对全账号撤走，两者分开存才驻得住整周。
    -- manualAdded 从 2026-10-27 起**已无读者**（准入门、族占行、同名去重、QuestKnown、
    -- 弹框标签、Dump/Probe 取证都不再认它），只有 v4/v5 迁移还会清它（`rec.manualAdded = nil`）。
    -- 留在清单里是为了字段清单守卫不许谎报：删掉这一项，迁移函数自己就会被判成
    -- "清单外的写法"而报红（test_quest_records_only_come_from_the_factory 扫的是 rec.<字段>）。
    regKey = true, kwLogAdded = true, kwLogKey = true, manualAdded = true, ignored = true,
    -- 准入粘滞 admittedKey 从第 39 轮（2026-10-29）起**已无读者**：用户定稿"什么时候也没说
    -- 做过就是准入门槛。1、匹配关键词的任务 2、金币奖励满足的任务"，粘滞和准入门是"或"的
    -- 关系、又会把①那支的放行结果冻进存档（91594 靠它留板到到期），所以写点与读点一起删。
    -- 双模式不掉行不靠它 —— 那份由 L.QuestMoney 认 `rec.goldKey == 当前编号` 的按实例金币
    -- 粘滞负责（用例 dual_mode_row_survives_without_the_admission_sticky）。
    -- 留在清单里只为两件事：字段清单守卫不许谎报（v11 迁移自己还要写 `rec.admittedKey = nil`），
    -- 以及老存档残留等 v11 清（见 L.ClearAdmittedKeysOnce）。
    admittedKey = true,
    -- 地图事件 pin 给的归属（"这一轮去哪办"），只有 Sync 的 pin 段写。
    -- evMapKey = 这份归属是在哪一条实例上定的（2026-11-02 用户"亚基入侵一会儿周常一会儿
    -- 祖阿曼"那一单）：归属一轮定一次，同一条实例不再改也不抹，编号换了才作废重来。
    -- evRoundEnd = 事件 pin 自身轮换边界（GetAreaPOISecondsLeft 取到的"这轮还有多久"换算成
    -- 绝对到期）：实例键优先用它，没有（接口没答）才回退每周锚点；丰饶这类按小时/天轮换的
    -- 世界事件才不会整周锁死在第一次见到的那张图。
    evMapID = true, evName = true, evAt = true, evMapKey = true, evRoundEnd = true,
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
--- 唯一例外是 StalePreviousCycleEnd：它带 now 只为判"这张旧纸条是不是上一条 CD 的"
--- （内含时钟域比对），不判这条实例活没活；身份门与到期门必须同时认这一份结论。
--- 照旧是**纯账本算式**：不许在这条路上问 Config.NextCycleEnd / Api.WeeklyResetCountdown
--- （矩阵每帧 20 行 × 每帧 = 新增 20 次接口调用，用户 10-27 明令禁止）。
--- 账本里这条 roundEnd 是不是**上一条 CD** 留下的墓碑（不属于本 CD 这条实例）。
--- 12.1.5 真档 `/gql quest 96718`（用户 11-05 回传）：奈格塔尔虚空决战（英雄）是两份
--- 按周轮换的任务之一，从不上世界任务板、地图与倒计时都问不到 ⇒ 身份只能按本 CD 尾巴算；
--- 可账本里压着上一条实例 7 天前就过期的 roundEnd，当前号没接（kwLogAdded 被撤点清掉），
--- 于是身份判不出、本 CD 编号印不出、14 个号按编号存的凭据全"对不上"、整行进不了矩阵
--- —— 玩家看到的就是"新任务接到了却不上架"。
--- 身份的规矩一直是「任务号 + 结束时刻」：上一条实例那张旧纸条跟本 CD 这条不是同一个身份，
--- 不该拿来判它活没活。「这是上一条 CD 的东西」只认三个读数，缺一不可：
---   · e < 本 CD 锚点：这条结束时刻落在上一次 CD，不是本 CD 这条实例的；
---   · 本 CD 真见过（lastSeenAt > anchor）：这条记录确实是本 CD 的，不是纯旧垃圾行 ——
---     裸墓碑（89377「秘密狩猎」形状）没有 lastSeenAt，绝不能被周尾巴接住，
---     否则同周上下两条实例并成同一个编号 = 上一轮的 ✓ 冒充本轮；
--- 原本还有第三条「从没上过板（not poolSeen）」，现已撤掉：12 小时区板面任务
--- 本 CD 内答出的结束时刻必然 >= 锚点，第一条 e < anchor 已经把它挡在外面，
--- 不需要再靠 poolSeen 二次排除。而 rec.poolSeen 是 DiffPool 写下的粘性历史标记，
--- 全仓没有清它的地方，拿它当"本 CD 从没上过板"会把所有进过池的行永久锁死。
--- 本 CD 内自己烙的真实到期日（92139 暗影与利爪那种 >1 天的特别任务）晚于锚点，
--- 第一条就不成立，照旧到点即死。跨时钟域一律不判（同 ThisCycle / Prune 的 house rule）。
--- 身份门（L.InstanceEnd）与到期门（L.InstanceExpired）共用这一份判据：不许一处认旧纸条、
--- 一处认新尾巴，那样行照样会在到期门被抹掉。
local function StalePreviousCycleEnd(db, rec, now)
    local n, e = tonumber(now), rec and tonumber(rec.roundEnd)
    local anchor = tonumber(db and db.cycleAnchor)
    local seen = rec and tonumber(rec.lastSeenAt)
    if not n or not e or not anchor or anchor <= 0 or not seen then return false end
    if (e > 1e9) ~= (n > 1e9) then return false end
    if (anchor > 1e9) ~= (n > 1e9) then return false end
    if (seen > 1e9) ~= (anchor > 1e9) then return false end
    return e < anchor and seen > anchor
end

local function InstanceEndRaw(db, rec, now)
    -- 没有这条记录 = 没有任何实例可谈：尾巴档是"这条记录问不出倒计时"时的兜底，
    -- 不是"账本空着时就报本 CD 的尾巴"，否则调用方传 nil 也能拿到一个结束时刻。
    if not rec then return nil end
    -- 上一条 CD 的过期墓碑不属于本 CD 这条实例：身份门和到期门都不许拿它判当前这条，
    -- 一律改按②档周 CD 尾巴取（判据与真档出处见 StalePreviousCycleEnd 上面那段）。
    if StalePreviousCycleEnd(db, rec, now) then
        return tonumber(db.cycleAnchor) + 604800
    end
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
---    **唯一例外**（12.1.5 真档 `/gql quest 96718`，用户 11-05 定）：这张旧纸条整个落在
---    上一个 CD（结束时刻 < 本 CD 锚点），这一行本 CD 又真见过、且从没上过板 ⇒ 它是
---    上一条实例的墓碑，不是"本 CD 答过、后来过去了"的值，判据 StalePreviousCycleEnd。
---    此时按②档周尾巴定本 CD 这条的身份；其它形状一个字不放宽。
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
    local e = InstanceEndRaw(db, rec, n)
    if not e then return nil end
    -- 关键词常驻行（kwLogAdded）：从不上世界任务板，L.SetExpiry 对带它的记录早退不重写
    -- roundEnd（Sync.lua:1177），账本里残留的 roundEnd 只能是上一条实例的过期墓碑。
    -- 它还活着（真接到特别任务时烙的真实到期日，a_special_task_from_log... 那一条）就用它；
    -- 已过期则退到②档周 CD 尾巴，否则身份永远锁在那条旧实例上、编号算不出、状态全 ?，
    -- 已接且本周见过的行反被误删（玩家实测 98172「追踪萨拉塔斯」）。注册周常（regKey）不在此
    -- 列：它的过期 roundEnd 必须按①档判到期（stale_round_end_with_no_log_source_expires）。
    if rec and rec.kwLogAdded and tonumber(rec.roundEnd) then
        local re = tonumber(rec.roundEnd)
        local alive = n and (re > 1e9) == (n > 1e9) and re > n
        if not alive then
            local anchor = tonumber(db and db.cycleAnchor)
            if anchor and anchor > 0 then e = anchor + 604800 end
        end
    end
    -- 只有板面档要"活着没"这道过滤（来源是 rec.roundEnd 在场 = 这条靠板面倒计时定身份）；
    -- 尾巴档不卡域也不卡大小：它只进 L.InstanceKey 的分桶，见上面那段。
    -- 上一条 CD 的墓碑已被 InstanceEndRaw 换成②档尾巴，别再因"roundEnd 在场"
    -- 把它当①档去卡大小 —— 那正是"新任务接到了却算不出编号"的最后一环。
    if rec and tonumber(rec.roundEnd) and not StalePreviousCycleEnd(db, rec, n) then
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
--- 【写侧说明（2026-11-03，计划 §二第 3 条）：这一份是**行级**金额，不是某一号的到手】
--- 语义就一句："这一条实例里任意一个号读到过的正数，谁最后同步就覆盖成谁的"。
--- 它继续负责自己本来的两件事 —— 切模式不掉行（第 4 步只在 `money > 0` 时写，这份
--- 正数保护让狩猎模式那一次读到 0 冲不掉普通模式读到的钱）与金币门槛；
--- 界面问"这一号挣了多少"一律改问号级那份（`SetRewardRead` 写的 reads），
--- 老代码拿这一份冒充某一号的到手，就是用户 11-03 截图那句
--- 「风行者 · 麦德 交于 11-03 09:16 到手 1089🪙」—— 麦德做它不给金币，1089 是别的号读的。
--- 号级以前**没有**这道正数保护（切模式会把真读数冲成 0），2026-11-03 起补齐，
--- 形状与第 4 步一样，只是保护范围是"同一 charKey + 同一条实例编号"，见 `L.SetRewardRead`。
--- 两处写点都吃 Sync 池循环里同一条 `pool[i].money`（同一次同步、同一个编号），
--- 改判据不新增任何接口调用。
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

--- "某个号的日志挂着这一条"烙成**这一条实例的凭据**（梦魇任务 94446 那一单，计划
--- docs/tasks/2026-10-29-keyword-log-weekly-resident.md §三第 3 条；用户定稿口径原话：
--- "梦魇任务是关键词任务，只要有一个号接了，那就意味着我要关注这个任务，
---  不管是不是金币任务，都要上矩阵"）。形状照 L.NoteGoldThisInstance 的先例：
--- 内部算编号，**算不出就一个字不写、返回 nil** —— 不许兜底成"周初那条"（判不出身份就当
--- 一直在 = §三第 1 条要消灭的那个 fail-open 方向），也不许在调用方各自拼 L.InstanceKey
--- （两处各算一遍迟早分岔，而这一族分岔的症状是"整行对全账号消失"）。
--- 为什么凭据必须**按实例**存而不是只存 kwLogAdded 那个布尔位：布尔位的语义是"此刻正在
--- 同步的这个号日志里挂着它"（消失判定、事件 pin 用词都靠这份 live 事实），于是
--- Sync 收尾那道撤点会换一个从没接这条的号登录就当场把它撤掉 —— 12.1.5 真档只读回放实测
--- （tests/tmp_kwonly_audit.py）：清掉布尔位之后面板 15 行变 14 行，掉的正是 94446，
--- 因为这条记录的上板凭据**只有那一份**。
--- 这份编号自己会死：这条问不出板面倒计时（没有 roundEnd）⇒ L.InstanceEnd 落②档
--- 周 CD 尾巴（cycleAnchor + 604800 按 15 分钟分桶）⇒ 下周重置一到尾巴整段挪一档、
--- 编号自然对不上、凭据自己作废，不需要任何"清理"代码（第 36 轮编号模型的全部意义，
--- 也是"驻整周不是驻永久"那一刀的落点）。
--- 它**不给任何一格凭空打勾**：勾仍只来自 views/turnins/manual 那三份带编号的凭据；
--- 这里只让行留在板上，客户端只答当前角色，没接过的号那一格继续 ?。
function L.NoteKeywordLogThisInstance(db, questID, rec, now)
    if not rec then return nil end
    local key = L.InstanceKey(db, questID, rec, now)
    if not key then return nil end
    rec.kwLogKey = key
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

--- 矩阵/右栏/`/gql quest` 共用的那一道**到期门**（2026-11-02 D6/D7 定稿口径，T30）：
--- 每一行都有一条**会过去的**结束时刻，取法有序、不许跳序 ——
--- ①这一条实例自己问得到的结束时刻（池里读到的板面倒计时 = rec.roundEnd；日志行问得到的
---   真实剩余，同一格字段）；
--- ②问不到 ⇒ **周 CD 尾巴**（cycleAnchor + 604800，读数时现算 —— 换 CD 那一刻 anchor 前挪、
---   尾巴整档挪一格 ⇒ 身份 L.InstanceKey（= questID + 按 15 分钟分桶的结束时刻）自然换档，
---   旧的"本轮做过"的勾一概对不上号 —— 用户要的"下板再上、标记不带走"就是这一句。
---   不需要新增字段、不需要烙进 roundEnd：会过去的不是 endAt 这个数本身，而是"这一档尾巴
---   属于这一个 CD"这件事，锚点一挪它就换档）。
--- `expired = endAt <= now（到点）；②档再问一句"这一 CD 真见过没有"`。两条既有保证各钉一头：
--- · 96718「时间结束后都要移除」（用户 10-24 定）：①档这条实例自己的结束时刻已过 ⇒ 当场死行。
--- · 89267「上一条实例的过期值不许吃掉整行」（第 34 轮，用户实测）：这条保证落在**身份那一路**
---   ——`L.InstanceEnd` 对已过去的 `roundEnd` 一律回 nil（见上面 :626），换轮后倒计时一读回来
---   就把 `roundEnd` 刷成新实例的、行自然续住。到期门这边 `endAt <= n` 一律判死、不问
---   `lastSeenAt`：关注图的任务每轮都正常刷新结束时刻，一条停在上一条实例的 stale 过去值
---   到期就是到期，不该把行赖着。
--- 跨时钟域一律不判（同 EffectiveExpiry / ThisCycle / L.Prune 那条 house rule）：`endAt` 与 `now`
--- 不同域时大小根本判不出来，判错的代价是把一条还活着的行吃掉 —— **判不出就不判**
--- （fail-closed）；具体守卫见函数体开头那段与②档 `seen`/`anchor` 的同域比较。
--- · 92139 旧修法作废（2026-11-02 D6 定稿推翻）：以前②档带一句按**身份**的豁免
---   （kwLogAdded / regKey / IsPinRegKey），钉住"尾巴现算永远在未来"的行不被"本周没见过
---   ⇒ 到期"误杀 —— 而 lastSeenAt 又由 EnsureRegistered 每轮白续（注册键名单喂一遍就盖一个
---   "这轮见过"），豁免与白续互为因果，结果就是"永不下板"这个病根。现在两句一起改：
---   白续撤了（续期只认本轮真事实：池快照 DiffPool / 当前号日志挂着 Sync 那一路 /
---   调用方传的 onBoard、seen），这道豁免整句删掉 —— "这一 CD 一次都没真见过 ⇒ 到期"
---   对**任何行**都问，注册键、内置表、关键词命中都不是留在矩阵的理由。
---   行活着只能靠它这一轮真的在板上/真的被客户端答到；下周再出现就是新的尾巴、新的编号。
function L.InstanceExpired(db, rec, now)
    local n = tonumber(now)
    local endAt = rec and InstanceEndRaw(db, rec, n)
    if not rec or not n or not endAt then return false end
    -- 跨时钟域一律不判（2026-11-02 回补）：`rec.roundEnd` 完全可能是 uptime 域写下的 ——
    -- Sync.Clock()（Sync.lua:64-70）在 `WallClock()` 答不出时明写"退回 GetTime()"并把域记进
    -- db.clockDomain，而烙 roundEnd 用的就是这个 now（Sync.lua:252、Ledger.SetExpiry）。
    -- 那种行下次登录拿墙上秒一比，endAt(小) <= n(大) 恒成立 ⇒ 所有带 roundEnd 的活行会被
    -- 整批判成到期、当场从矩阵消失。身份那一路（L.InstanceEnd:641）卡的就是同域，两门必须
    -- 同一个口径；判不出就不判（与 L.Prune 同一条 house rule）。
    if (endAt > 1e9) ~= (n > 1e9) then return false end
    if endAt > n then
        -- endAt 在未来 = ①档的活实例，或②档这一档的周 CD 尾巴。只有②档需要往下问
        -- "这一 CD 真见过没有"——尾巴落进过去的瞬间锚点还没挪（挪了尾巴就换到新档、这条
        -- 记录的 endAt 跟着换），中间这段窗口靠 seen <= anchor 兜住：本周一次真事实都没续上
        -- ⇒ 它挂着的只能是上一周那条实例的账 ⇒ 到期。
        -- 上一条 CD 的墓碑已被 InstanceEndRaw 换成②档尾巴，同样要问"这一 CD 真见过没有"；
        -- helper 已把"见过"钉死，这里仍走同一道判据，免得身份门认新尾巴、到期门认旧纸条。
        if not tonumber(rec.roundEnd) or StalePreviousCycleEnd(db, rec, n) then
            local anchor = tonumber(db and db.cycleAnchor)
            local seen = tonumber(rec.lastSeenAt)
            if anchor and anchor > 0 and seen and (seen > 1e9) == (anchor > 1e9) then
                return seen <= anchor
            end
            -- 锚点没立起来 / lastSeenAt 缺失 / 跨时钟域：判不出 ⇒ 不动（fail-closed，
            -- 维持"行留着"的现状），等下一次真事实把 lastSeenAt 续上或把锚点立起来。
        end
        return false
    end
    -- endAt <= n：记录的结束时刻已经过去了 ⇒ 到期，删除。
    -- （不再用"最近一次见到它比结束时刻晚"保活：关注图的任务每轮都正常刷结束时间，
    -- 过期的 stale roundEnd 不该再把行赖着——到期就是到期。）
    -- 关键词常驻行（kwLogAdded）的过期 roundEnd 是上一条实例的墓碑，不当①档短路；
    -- 退到②档按 lastSeenAt 判活（玩家实测 98172「追踪萨拉塔斯」：已接且本周见过就留）。
    -- 注册周常（regKey）的过期 roundEnd 保持①档判到期
    -- （stale_round_end_with_no_log_source_expires）。只有"明确本周见过"才保活——
    -- 没见过 / 上周见过 / 跨域 / lastSeenAt 缺失一律到期（无本周证据即无身份）。
    if rec and rec.kwLogAdded then
        local anchor = tonumber(db and db.cycleAnchor)
        local seen = tonumber(rec.lastSeenAt)
        if anchor and anchor > 0 and seen and (seen > 1e9) == (anchor > 1e9) and seen > anchor then
            return false
        end
        return true
    end
    return true
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
            -- 名字与地图只许被**非空的新答案**覆盖（用户 2026-10-31 的"出现2次消失2次"
            -- 嫌疑门之一）：名字没问出来的一读（问标题要预加载数据，有先后）把上一读
            -- 客户端确认过的名字冲成 nil，关键词命中（kwHit 靠名字）当场翻 false ——
            -- 命中关键词的行闪现一回，下一读名字回来又出现。地图同理：mapID 被冲掉
            -- 不光塌进"未知地图"分组，还丢一份"客户端答过图"的凭据
            -- （L.QuestHasCredential 的 mapID 那一支）。真换图/真改名的新答案非空，照旧覆盖。
            rec.lastSeenAt = now
            if type(q.name) == "string" and q.name ~= "" then rec.name = q.name end
            if q.mapID then rec.mapID = q.mapID end
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
        -- 布尔位之外的这一条实例凭据同样豁免：布尔位换一个没接它的号登录就被撤，
        -- 撤完这句就会把梦魇任务 94446 的 currentSig 清空 —— 灰横线那一族原地复发
        -- （计划 §三 R3；本轮只做放宽，条件一个字没删）。
        if not partial and not seen[questID] and rec.currentSig and not rec.kwLogAdded
            and not L.KeywordLogAdmits(db, questID, rec, now) then
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

--- 写一笔交割流水（完成凭据）。**返回值是刚写下的那一行**。
--- 只记"这个角色这一刻交了这条任务"（t）+ 这一条实例的编号（k）+ 完成时看到的奖励
--- 签名（sig，取证用）。到手金币自 2026-10-29 起不再在这一处量、也不再存、也不再用于
--- 任何判据（砍掉"交任务获得金币"），所以这一处压根没有金额形参：奖励是不是金币、值多少
--- 只来自奖励读取（DiffPool / NoteGoldThisInstance），周统计改读 rec.goldMoney（见 L.WeekStats）。
function L.OnTurnIn(db, charKey, questID, t, sig)
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
    rows[#rows + 1] = { t = t, sig = sig, cycle = db.cycleAnchor or 0,
        k = L.InstanceKey(db, questID, rec, t) }
    while #rows > MAX_TURNINS_PER_QUEST do table.remove(rows, 1) end
    return rows[#rows]
end


--- 某个号"上一次交这条"是哪一刻、到手多少、是谁记的（实测流水 / 手动补录）。
--- 矩阵悬停与导出都问这一处：界面自己翻 db.turnins 就等于再写一套判据，
--- 流水的存法一改（第 20 轮那次 cycle 字段就是）报告会开始说瞎话。
--- 两份都在时取时刻新的那份；能不能报成日期由调用方问 Config.FormatStamp
--- （开机秒域的时间戳格式化出来是 1970 年，那一处才是判域的地方，这里不重复判）。
--- 返回 t, g, src；没记录回 nil。
--- 【金额是按号的】（2026-11-03 计划 §二第 2 条）：以前这一句比的是
--- `rec.goldKey == r.k` 然后回 `rec.goldMoney` —— 那份是"这一条实例里**任意一个号**
--- 最后同步读到的钱"，跟眼前这一号没关系。用户 11-03 的截图就是它的现场：97128 的
--- 麦德那一格印「风行者 · 麦德 交于 11-03 09:16 到手 1089🪙」，而麦德做它**不给金币**
--- （给物品，它的 `reads[97128].g` 是 0），1089 是另一个号本轮读到的钱 —— 借号挂名。
--- 现在实测分支只问这一号自己的读数（`L.CharRewardMoney`），**编号用这笔流水自己的
--- `r.k`**（不是"当前编号"）：交任务那一刻挂在哪条实例，就取那一条实例上这号读到的数。
--- · 这号在这条实例上没读过 ⇒ nil（宁缺不编，界面整段不印「到手」）；
--- · 读到 0 ⇒ 0 —— 那是客户端明说这号做它不给金币，是真答案不是"没数"，
---   由调用方（Matrix 的交割行）决定 0 不拼成「到手 0」。
--- `r.k` 为 nil 的那一笔（写下时身份未定）按 §五「没编号 = 不匹配」回 nil，
--- 不许拿"当前编号"兜底 —— 那等于让别的实例的读数给这一笔背书。
--- 补录那一岔读的是这一号自己写下的 `m.g`，本来就按号，一个字不动。
function L.LastCompletion(db, charKey, questID)
    local best
    local rows = db and db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
    local r = rows and rows[#rows]
    local rt = r and tonumber(r.t)
    if rt then
        local gold
        -- 金币默认按行借这一条实例读到的那份；只有这条实例上真判出"按号分岔"时才
        -- 只认这一号自己的读数（L.CellRewardMoney，11-04 定稿 §二 6）。
        -- 交任务到手数已退役，编号仍用这笔流水自己的 r.k。
        if r.k ~= nil then
            gold = L.CellRewardMoney(db, charKey, questID, nil, r.k)
        end
        best = { t = rt, g = gold, src = "实测" }
    end
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

--- 一条任务的结束时刻由**它是什么**决定，不由哪个接口先答出来决定。两种口径：
--- · **常驻周常**：注册键不是 `pin:` 那一路（`kw:` = 玩家敲关键词点名的、不带前缀的 = 内置表
---   声明的整周常驻族，前缀判据只在 L.IsPinRegKey 一处），或从任务日志收进来的 kwLogAdded，
---   或这一条实例被某个号的日志证明存在过（末位 resident 就是那一份，判据只在
---   L.KeywordLogAdmits 一处）→ 板面倒计时一律不许写进 roundEnd：**板上那几小时是同一块板
---   的轮换，不是这条周常这一周的命**（用户 2026-11-02 拿斯托玛兰举的例子：它是周常，
---   当天下午整行没了就是拿轮换寿命当结束时间烙出来的；12.1.5 同类实测"加固符文石
---   这个任务没了"、上一轮"符文石和特别任务…这两个都是周常任务，不应该到期了"）。
---   注意这不等于"豁免到期"——它的结束时刻就是 L.InstanceEnd 的②档周 CD 尾巴，
---   换 CD 那一刻尾巴整档挪一格、到期门②档"这一 CD 没真见过 ⇒ 到期"当场接住，
---   旧的"本轮做过"标记一概不带走（D6/D7 定稿：没有任何一条可以豁免说不下板）。
--- · 没注册的池内世界任务 + 金币门从事件 pin 收的那一路（`pin:`）→ 用它自己问出来的板面
---   倒计时，到期即消失（"时间结束后都要移除"）。
--- T5 起这里只写 roundEnd 这一个字段：老那一组 expiresAt/expiresSrc（含"接口倒计时"来源
--- 标记）连同 24 小时排程值一起退役，从此没有第二份到期账。
--- 【末位 resident 为什么必须有，2026-10-29 梦魇任务 94446 那一单】前三位一个不动
--- （tests 里 20 处按 L.SetExpiry(rec, now, remaining) 调用），第 4 位可选：
--- 一条日志维持的行这周之后重新上板时，布尔位会被 Sync 当场撤掉（在池里的行由池维护），
--- 于是这句会把板上**几小时**的轮换寿命写进 roundEnd ⇒ L.InstanceEnd 从②档挪到①档
--- ⇒ 编号跟着换档 ⇒ 刚烙上的那份日志凭据立刻对不上、整行当场没 —— 正是加固符文石
--- "当天下午整行没了"那个形状，只是这次咬掉的是新凭据。resident 由两个调用方各自算
--- （Sync 池循环、EnsureRegistered；这两处手上没有本轮编号，让判据算一次纯算术，
--- 每趟同步每行一次，不在矩阵每帧那条路上）。
--- 【T6b（用户 2026-11-02 定稿）：这句早退按注册前缀收窄，`pin:` 那一路不再早退】
--- 上一轮 T6 只把 pin 行从"驻板豁免"里摘出来，可①档对它仍然是空的 —— 就是这句对**任何**
--- regKey 都早退，而它没有第二个 roundEnd 写点（日志那一路 Sync.lua:244 只收"撞了关键词 +
--- 挂在当前号任务日志里 + 不在池里"的名字，金币门收的那批三条都不满足）⇒ 它只能走②档
--- "本周没被看见过 ⇒ 到期"，最坏驻板到下一次周重置，离用户那句「到期就要自动消失」还差一截。
--- 收窄之后：金币门从事件 pin 收的行跟普通池行拿**同一条**倒计时（池循环 Sync.lua:868 本来
--- 就在读的 secs，零新增接口调用），按自己那条实例的结束时刻到点即到期。
--- 为什么这一刀不伤"加固符文石"那一族（本句早退存在的全部理由）：
---   ① 它是**内置表声明的族**（Api.lua:335 key="fortify-the-runestones"、ids=90573-90576），
---      注册键不带 kw:/pin: 前缀 ⇒ 走的还是这句早退，一个字节没变；
---   ② 它拿不到 `pin:` 键：收 pin 那一趟先把内置族的 ID 与族词排掉
---      （Sync.lua:636-639 PresetByID/PresetByName 双查，L.PresetByID 认 ids），
---      90573-90576 压根进不了 entries；
---   ③ 它被日志收进来那一趟另有一道自己的闸门（Sync.lua:249 "< 1 天视为板面轮换寿命、与本周
---      重置倒计时重合视为回声，都不写 roundEnd"），那道门在写点自己手里，与本句无关。
--- ⇒ 不把那道 <1 天闸门搬进本函数：搬进来正好废掉 T6b —— 屠戮者高地那种 12 小时区的 pin 行
--- 答出来的就是几小时，"不烙"就又退回②档等下一次周重置。取向与用户那句"和其他普通金币任务
--- 一样"对齐：普通池行本来就用它自己那几小时的板面倒计时到期。
--- 代价照实：压根不上板的事件 pin 行（RefreshEventPins 明写"倒计时不从这里拿"，
--- Sync.lua:572-573）这一轮仍然只有②档那一条退出路，本轮不为此多问一次接口。
function L.SetExpiry(rec, now, remaining, resident)
    if (rec.regKey and not L.IsPinRegKey(rec.regKey)) or rec.kwLogAdded or resident then
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
-- state 只有三档：done（做了）、skip（这一号这轮不做，2026-10-30 右键那一路）、open（清）。
-- skip 与 done 共用同一份记录与同一道编号门 —— 换轮以后自己作废，不必另开字段。
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

--- 手动添加任务（点地图 pin / 设置页关键词段的"取消忽略"走这条）。
--- **玩家侧的含义只有一句话：撤销"别再显示这一行"的意图**（用户 2026-10-27 的原话：
--- "手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了"）。
--- 上一版这里写 `manualAdded = true`，那道标记当年是矩阵准入门之一（那时还列着五道、六道），
--- 还单独撑破了同名
--- 去重豁免、一族一行的破例、"本资料片过滤"跳过 —— 等于把一次撤忽略当成了一张上板
--- 通行证，而玩家从没做过"标记这条要强制上板"这个动作。字段本轮整体下线，这条路径
--- 现在只做账：建记录（没有的话）、问名字/地图/金额、写一条 observation、续 lastSeenAt、
--- 清 ignored。不再"定个默认到期"——编号模型里新记录没有 roundEnd，到期门自然走周 CD 尾巴
--- 那一档，而刚续上的 lastSeenAt 落在本周锚点之后 ⇒ ②档那句"本周一次都没被看见过 ⇒ 到期"
--- 不成立，行不会被立刻吃掉。
--- **它不承诺这一行会出现在矩阵上**：清完忽略之后该不该上板，由准入门那两类依据
--- 重算（玩家点名：命中关键词+客户端凭据 / 关键词从日志收的 / 注册周常；奖励金币过门槛）。
--- 记录还是要建，因为快照与取证后面问得到它才有凭据可算。
--- addedBy == "questlog" 时记成 kwLogAdded：那是插件从任务日志自动收的（关键词命中 +
--- 任务日志里有），跟玩家点什么无关，是一道上板的自动凭据，保留。
--- 第 5 个参数 knownMoney = 调用方手里已经问到的金额（补读那一趟）。不传 = 自己问一次（老行为）。
function L.AddQuest(db, questID, now, addedBy, knownMoney)
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
    -- 调用方手里已经有金额（补读那一趟：钱刚问出来，正要落进这一条）就别再读第二遍 ——
    -- 不是省一次调用的小事：这两次读之间只要客户端换了答案，下面按金额分的观察桶
    -- 会落进另一个签名，落账的数和记账的数对不上。不传 = 老行为，自己问一次。
    local money = (type(knownMoney) == "number" and knownMoney)
        or ((GQL.Api and GQL.Api.QuestRewardMoney)
        and GQL.Api.QuestRewardMoney(questID) or nil)
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
        -- 日志源那一路是一道上板凭据（关键词命中 + 任务日志里有这条），照旧记账。
        -- 紧下面这一句把同一件事烙成**这一条实例**的编号：布尔位是"此刻这个号挂着没"的
        -- live 事实，换一个没接它的号登录就被 Sync 收尾那道撤走，只靠它整行对全账号消失
        -- （梦魇任务 94446）。两个写点（这里 + Sync 收日志那条）都走同一个函数，
        -- 编号不许在调用方各自拼。
        rec.kwLogAdded = true
        L.NoteKeywordLogThisInstance(db, questID, rec, now)
    end
    db.quests[questID] = rec
    return true, isNew and "新增" or "已存在(刷新)"
end

--- 移除任务（点错了 / 关键词误命中了不想要的任务）。
--- **不能真删记录**：池里/日志里的任务每次同步都会被重新建出来，删了等于没删
--- （12.1.5 实测反馈："右键点击后还会刷出来"）。改成记一条"用户明确不要这行"的
--- 意图 `ignored`，记录本身留着（矩阵不列，「关键词 · 屏蔽管理」右栏点"取消忽略"可以恢复）。
--- **永久**：换 CD、换轮都不作废，只有玩家在设置页关键词段里点回来（L.AddQuest 清 ignored）
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
--- 同一句理由对 `kwLogKey`（这一条实例的日志凭据）同样成立，两份一起清：只清布尔位的话
--- "取消忽略"会拿那份编号把玩家明确不要的那一行当场顶回矩阵。
--- `manualAdded` 那一句以前这里也清，随字段整体下线一起删了（v5 迁移负责清老存档残留）。
function L.RemoveQuest(db, questID)
    questID = tonumber(questID)
    if not db or not questID then return false, "questID 无效" end
    local rec = db.quests and db.quests[questID]
    if not rec then return false, "账本里没有这个任务" end
    rec.ignored = true
    rec.kwLogAdded = nil
    rec.kwLogKey = nil
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
--- 【"这一轮答不出"不许抹掉上一轮的结论】（2026-11-02 用户现场："亚基入侵：有的角色完成了，
--- 再次登录后又变成问号了"）：重新登录后客户端有一段时间答不出这条（pin 图标先消失，
--- 中间态约十来分钟，有的号要等下次整点更新），状态接口与虚拟行探测器两路都如实回
--- `"unknown"`（Api.lua:826-845）。这句"unknown"是**非空字符串**，本文件
--- EnsureRegistered 末尾那句 `if e.state then L.SetView(...)` 拦不住它，于是
--- 矩阵开着时每 10 秒一趟的快照（Sync.RefreshRegistered → Sync.lua:445 重问状态）
--- 第一趟就把这个号已经拿到的 `done` 连根抹掉 —— 抹的是 SavedVariables = 永久覆盖，
--- 界面从此顶着一个"?"，直到客户端缓过来重新答出徽章文案才自己回来。
--- Api.lua:817 那句现成的注释早就写着"接口这轮没答 ≠ 状态变了"，只是落在内存缓存上、
--- 没落到判据 —— 这一处就是把它落到判据上：判不出 = 不成立（fail-closed），
--- 但"不成立"不许顺手删掉玩家已经拿到的结论，这一格没有新事实，只有"没问到"。
--- 只在**同一条实例**上保留旧结论（`L.SameInstanceKey`，任一边没编号就不成立）：
--- 换 CD / 板上换了另一条实例 ⇒ 编号变了，这一格本来就该空出来重新问，`unknown`
--- 作为**新实例**的初始值照旧写进去，不许让上一条实例的答案赖在这一格上。
--- 其它状态（done/open/progress/skip/ended）一字不变：玩家点格子清除覆盖走 open、
--- 交割当场写 done（Sync.lua:1267）、池路写视图（Sync.lua:908）都不受这里影响。
--- 收口只在这一处：五个写点（本文件 EnsureRegistered + Sync.lua:272/908/948/1267）
--- 共用这一条判据，不在调用点各写一遍。
function L.SetView(db, charKey, questID, state, t, curKey)
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    db.chars[charKey].views = db.chars[charKey].views or {}
    if curKey == nil then
        local rec = db.quests and db.quests[questID]
        curKey = L.InstanceKey(db, questID, rec, t)
    end
    if state == "unknown" then
        local old = db.chars[charKey].views[questID]
        if type(old) == "table" and old.s and old.s ~= "unknown"
            and L.SameInstanceKey(old.k, curKey) then
            return
        end
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
--- 【同一条实例之内，正数不被新的 0 冲掉】（2026-11-03 口径，计划
--- docs/superpowers/plans/2026-11-03-gql-per-char-gold-attribution.md §二第 1 条）：
--- 行级那一份早就有这道保护（`NoteGoldThisInstance:729` 只在 `money > 0` 时才写 ⇒
--- 切狩猎模式读到 0 冲不掉已读到的正数，双模式不掉行靠它），号级这一处以前**没有**
--- ⇒ 同一个号在同一条实例上先普通模式读到 1089、之后切狩猎模式同步一次，读数被无条件
--- 写成 0，格子从「1089」翻成「这号不给金币」、周汇总跟着掉（用户 11-03 特别提醒：
--- 同一任务不同模式（是否狩猎）奖励不同，不许为此反复读取）。
--- 保护只在**同一个 charKey 且新编号与已有 `reads[questID].k` 相同**时生效：
--- · 新的正数照旧覆盖（0→正数、正数→正数都放行 —— 客户端改口就往真答案走）；
--- · 编号不同照旧整笔重写（旧实例的数不给新实例背书，换实例后这一笔本来也匹配不上）；
--- · 已有那笔没烙编号（写下时身份未定）也不保护：它本来就永远匹配不上，留着只会挡路。
--- 判据是纯账本一次比较，一次接口调用都不多（写点仍只吃同一条 `pool[i].money`）。
function L.SetRewardRead(db, charKey, questID, money, t, curKey)
    if type(money) ~= "number" then return false end
    db.chars[charKey] = db.chars[charKey] or { lastSync = 0, views = {} }
    local row = db.chars[charKey]
    row.reads = row.reads or {}
    if curKey == nil then
        local rec = db.quests and db.quests[questID]
        curKey = L.InstanceKey(db, questID, rec, t)
    end
    local old = row.reads[questID]
    if money == 0 and type(old) == "table"
        and L.SameInstanceKey(old.k, curKey)
        and type(old.g) == "number" and old.g > 0 then
        return false
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

--- 这条实例上是否真的"按号分岔"：至少两个不同 charKey，一个正数、一个 0。
--- 纯账本一次遍历，零接口调用。**见 0 就判分岔是不行的**：Api.lua:187-189 会在
--- "数据在本地却读不出金额"时造出推断 0，Matrix.lua:573-575 也承认 0 可能是做完前的
--- 旧答案；写侧"正数不被 0 冲掉"的真身在 Ledger.lua:1400-1403（Config.lua:32-35 只是注释），
--- 且只在"先正数后 0"生效。一个推断 0 就把所有没读过的号打回 ?，正是本轮要消灭的症状。
--- 一个 charKey 只有一条 reads[questID]，所以 sawZero + sawPositive 天然来自两个不同号。
function L.RewardDiffers(db, questID, now, curKey)
    local rec = db and db.quests and db.quests[questID]
    if not rec then return false end
    if curKey == nil and now ~= nil then
        curKey = L.InstanceKey(db, questID, rec, tonumber(now))
    end
    if not curKey then return false end
    local sawZero, sawPositive = false, false
    for _, row in pairs(db.chars or {}) do
        local r = row and row.reads and row.reads[questID]
        if r and r.k ~= nil and L.SameInstanceKey(r.k, curKey) then
            local g = tonumber(r.g)
            if g == 0 then sawZero = true
            elseif type(g) == "number" and g > 0 then sawPositive = true end
        end
    end
    return sawZero and sawPositive
end

--- 这一格该报多少钱：正数 / 0 / nil（nil = 界面报"金额未知"）。
--- 11-04 定稿：默认按行，只有这条实例上判出分岔才按号 —— 11-03 那一版把三处消费侧
--- 全改成只认本号读数，是从"整行一个数"走到"一律按号"的另一个极端。
--- 借的那一份必须是**同一条实例**的行级金额（rec.goldKey 与传入编号对得上）：
--- 上一条实例留下的钱不给这一条背书，跟 CharRewardMoney 同一道门。
--- curKey 优先用调用方显式传的；没传才按传入的 now 算，不许偷偷用 LedgerNow(db)。
function L.CellRewardMoney(db, charKey, questID, now, curKey)
    local own = L.CharRewardMoney(db, charKey, questID, now, curKey)
    if own ~= nil then return own end
    local rec = db and db.quests and db.quests[questID]
    if not rec then return nil end
    local key = curKey
    if key == nil and now ~= nil then
        key = L.InstanceKey(db, questID, rec, tonumber(now))
    end
    if not key then return nil end
    if L.RewardDiffers(db, questID, now, key) then return nil end
    if not L.SameInstanceKey(rec.goldKey, key) then return nil end
    local g = tonumber(rec.goldMoney)
    if type(g) == "number" and g > 0 then return g end
    return nil
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

--- 落一条注册周常。entries = { { key, questID, name, mapID, mapSrc, money, state, onBoard, seen }, ... }
--- 与 AddQuest 的区别刻意为之：
---   · 记 regKey（族身份/常驻周常身份，见上面），不记 kwLogAdded（那是关键词从日志
---     收进来的那道门；manualAdded 一线已随字段下线消失，见 AddQuest）；
---   · 到期口径按**注册前缀**分岔（L.IsPinRegKey，用户 2026-11-02 定稿 + T6b）：`kw:`/内置那一
---     族只按 CD 尾巴，板面实例的轮换倒计时不算它们的寿命；`pin:` 那一路照普通池行用它自己
---     问出的结束时刻到期（写不写由 L.SetExpiry 那一句定，本函数只负责把手里的 remaining 递过去）；
---   · lastSeenAt **只有 entry 带着本轮真事实才续**（见下面那句判据）：注册键本身不是事实，
---     每轮回问一次名字/状态不配给行续命（用户 2026-11-02 D6「上板的任务，没有任何一条可以
---     豁免说不下板」）；Prune 的三周时清照旧碰不到非 pin: 的记录，那是账本体积那道门，
---     与界面行寿命是两个问题（前缀口径见 L.IsPinRegKey）。
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
            -- 一条任务只留最新那一把注册键（用户 2026-11-02 定稿的另一半）：同一块板的
            -- pin 名周内会换（改名/换阶段），新旧两把都挂在 rec.regKey 上迟早变成"一串键"，
            -- 而其中任何一把都能把行钉在板上。旧键是 pin: 且不是同一把 ⇒ 走既有退订
            -- L.UnregisterByKey 整把退掉（同一旧名下别的那几条一并退：那块板这轮已经没报它们了）
            -- 再挂新键。反过来 pin: 也不许把玩家点名那一把（kw: / 内置）顶掉 —— 点名身份是
            -- 玩家给的，金币门自己收的行不配撤它（撤了等于连驻板豁免一起没，右键白点那一族）。
            local oldKey = rec.regKey
            if oldKey ~= e.key then
                local keepNamedKey = oldKey ~= nil and not L.IsPinRegKey(oldKey)
                    and L.IsPinRegKey(e.key)
                if not keepNamedKey then
                    if L.IsPinRegKey(oldKey) then L.UnregisterByKey(db, oldKey) end
                    rec.regKey = e.key
                end
            end
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
            -- 续命只认调用方递来的**本轮真事实**（2026-11-02 D6/D7）：这一趟每轮快照都会拿
            -- 账本里的注册键名单喂一遍（Sync.RefreshRegistered），把它当成"见过"就等于让每一
            -- 把带 regKey 的行自己给自己续命 ⇒ 到期门②档那句"这一 CD 一次都没真见过 ⇒ 到期"
            -- 永远不能对它们开（豁免与白续互为因果，谁也不敢先动，所以两句一起动）。
            --   e.onBoard —— 调用方本轮亲眼在图上/板上看到它（语义见函数头，未变）；
            --   e.seen    —— 本轮客户端答过一个关于它的事实，但不是在板上（事件 pin 在场就用它）。
            if e.onBoard or e.seen then rec.lastSeenAt = now end
            -- `pin:` 那一路问到的钱同时按**这一条实例**记一笔（L.NoteGoldThisInstance，
            -- 与池循环用的是同一份原语，零新增接口调用）：金币门在收行那一刻问出来的金额
            -- 原本只落在 rec.lastMoney 这一格记录级字段（永不作废、也不是准入门读的那份），
            -- 而矩阵认的是 obs 的当前签名 + goldMoney/goldKey 那份按实例粘滞。不补这一笔，
            -- "和其他普通金币任务一样"（用户 2026-11-02）就只能靠注册键白拿 ① 的绕门槛待遇；
            -- 补了之后它走的是②那道门，和普通池行同一把判据。编号换了（周重置/换实例）
            -- goldKey 自己匹配不上 ⇒ 这份钱作废 ⇒ 行退出，正是"到期就要自动消失"。
            if money and L.IsPinRegKey(e.key) then
                L.NoteGoldThisInstance(db, questID, money, now)
            end
            -- "在不在板上"只由调用方说：池里有 / 跟踪地图清单里扫到才传 true。
            -- 没有板面证据的锚点不成行（用户 2026-10-23 定的模型：行=子任务，各回各的图、
            -- 各带自己的倒计时；锚点只当"这族存在 + 这个号本周做过没有"的证据载体）。
            if e.onBoard then rec.onBoard = true end
            -- 到期口径归 SetExpiry 一句定：`kw:`/内置那两把键是常驻周常，只认 CD 尾巴；`pin:`
            -- 那一路不早退（T6b，用户 2026-11-02「和其他普通金币任务一样。到期就要自动消失」）。
            -- 这里以前直接把拿着 ID 问出来的倒计时写成到期 —— 那条倒计时是**板上实例**的
            -- 轮换寿命（几小时），于是 加固符文石 这种注册周常当天下午就从矩阵消失。
            -- 末位那条日志凭据同理要传过去：这条既是注册周常又是日志收的（94446 那种）时，
            -- 写板面倒计时会把编号从 CD 尾巴挪到板面档，刚烙的凭据自己作废（SetExpiry 注释）。
            -- 生产三个调用方（每轮注册重问 / 关键词补扫 / 事件 pin 收路）都不带 remaining 进来，
            -- 所以这一路对 pin 行现在是"递了个空"；pin 行的板面倒计时走池循环那一句。
            L.SetExpiry(rec, now, e.remaining, L.KeywordLogAdmits(db, questID, rec, now))
            if e.state then L.SetView(db, charKey, questID, e.state, now) end
            touched = touched + 1
        end
    end
    return touched
end

--- 账本里所有带 regKey 的记录（内置预设 + 关键词补扫注册的 + 金币门从事件 pin 收的），
--- { questID, key } 列表。Sync 每轮快照按这份重问状态：补扫只在那一轮建过账，不照这份
--- 回问的话"虚空侵袭"这行的红勾会永远停在补扫当天。ignored 的跳过（玩家永久屏蔽掉的这行，
--- 接口也不用再问一遍 —— 屏蔽只有玩家在设置页关键词段里点回来才会撤）。
--- **这份名单是"有哪些注册行"的账，不是"该续谁的 lastSeenAt"**：Sync 那趟重问自己按前缀
--- 把 pin: 挑出去（散场的 event pin 不许靠这趟白续命，见 Sync.RefreshRegistered），
--- 补扫/取证那两处照旧要全量。
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
-- 排行（族里多条取最强的一条）：做过 > 在做 > 手记不做 > 可做 > 已结束 > 问不出。
-- "不做"排在"可做"之上：玩家说过这一号这一族不做了，就别再拿客户端那句"还在池里"
-- 把它盖回去（那一盖就等于右键白点）；它排在"在做/做过"之下，真凭据照样压得住。
-- 数值顺移一位是为了空出 skip 这一档，别把 done 的 4 改成别的意思。
local STATE_RANK = { done = 5, progress = 4, skip = 3, open = 2, ended = 1, unknown = 0 }

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
--- 第五档 "skip"（2026-10-30，右键"这一号本轮不做"）：它只在**没有真凭据**时接管这一格，
--- 一旦这一条实例有实测流水或快照答 done，就照旧报 done —— 意向压不住凭据。
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
        -- 手记"不做"（右键，2026-10-30）：玩家说这一号这轮不做它，界面给一枚 ×。
        -- 它是**意向**不是凭据 —— 这一条实例真有凭据（实测流水、或快照答 done）时照旧
        -- 报 done：标了不做又去做了，那一笔不许被一枚 × 藏起来（周统计也跟着少算）。
        -- 只有一份凭据都没有时，skip 才接管这一格。
        if m.state == "skip" then
            local tRows = db.turnins and db.turnins[charKey] and db.turnins[charKey][questID]
            local tLast = tRows and tRows[#tRows]
            if tLast and L.SameInstanceKey(tLast.k, curKey)
                and (byCycle or L.GetObs(db, questID)) then
                return "done", nil
            end
            local ch = db.chars and db.chars[charKey]
            local v = ch and ch.views and ch.views[questID]
            if v and v.s == "done" and L.SameInstanceKey(v.k, curKey) then
                return "done", nil
            end
            return "skip", "manual"
        end
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
--- 有完成记录但一条**这一号自己**读到的金币金额都没有时 gold 返回 -1，UI 显示 "?"，绝不显示 0。
--- （2026-11-03：那句"读到过金币"从行级 rec.goldMoney 换成按号 reads —— 金额列是**这一列的号**
--- 挣了多少，不是这一条实例上任意一个号挣了多少。）
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
    -- cycleStart = 0 / nil 是"不加过滤"（上方 docstring 的既定口径），
    -- 不能拿 0 当 uptime 域去做同域比较，否则纪元秒流水会静默算成"完成 0"。
    local filtered = cycleStart and cycleStart > 0
    for questID, rows in pairs(db.turnins[charKey] or {}) do
        -- 金币默认按行（11-04 定稿 §二 6）：这一号没读过就借这一条实例的行级金额，
        -- 但只有这条实例上真判出分岔（两个不同号一正一 0）才退回只认这一号自己的读数。
        -- 11-03 那版一律按号，把"绝大部分正常任务"也打成了 `?`；这一版把代价收回分岔那一档。
        -- 问的是唯一口径 L.CellRewardMoney，编号用这笔流水自己的 `r.k`（交任务挂在
        -- 哪条实例就取那一条），不是"当前编号"。
        -- 正数才计入并置 goldKnown；nil（判不出/分岔没读过）与 0（读过、答的是
        -- 不给金币）都不计金额 —— 完成条数 count 照旧不受影响。
        for i = 1, #rows do
            local r = rows[i]
            -- 逐笔同域比较：锚点是开机秒、这笔是纪元秒（或反过来）时判不出
            -- "是不是本周"，按 fail-closed 不计入；not filtered 直接放行 0 / nil。
            if r.t and (not filtered or
                    ((r.t > 1e9) == (cycleStart > 1e9)
                     and r.t >= cycleStart)) then
                count = count + 1
                counted[questID] = true
                if r.k ~= nil then
                    local g = L.CellRewardMoney(db, charKey, questID, nil, r.k)
                    if type(g) == "number" and g > 0 then
                        gold = gold + g
                        goldKnown = true
                    end
                end
            end
        end
    end
    for questID, m in pairs(db.manual[charKey] or {}) do
        if m.state == "done" and not counted[questID]
            and (not filtered or
                ((m.t or 0) > 1e9) == (cycleStart > 1e9)
                and (m.t or 0) >= cycleStart) then
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
        -- 注册周常也不按时清 —— 但豁免按**注册前缀**给（L.IsPinRegKey，2026-11-02 定稿）：
        -- kw: 那一路是玩家敲关键词点名的、内置周常是代码里声明的整周常驻族，它们本来就不进
        -- 世界任务池，而 lastSeenAt 现在只由**本轮真事实**续（板上见到 / 日志挂着 / 族成员在板上 /
        -- 事件 pin 在场），每轮注册重问那一趟一个字都不续（2026-11-02 D6/D7，见 Sync.RefreshRegistered）
        -- —— 所以这一路长期没续上是正常状态，不是"这行该删"的信号：删了还得把标题/地图接口重问
        -- 一遍才刷得回来，而玩家那句"我要盯着这一条"并没有撤。三周不开面板就把用户点名要的任务
        -- 删掉不合理（这一道门只管账本体积，界面行的寿命由 L.InstanceExpired 判）。
        -- **pin: 不给豁免**：金币门从事件 pin 收的那一行按用户 2026-11-02 那句定稿
        -- （"和其他普通金币任务一样。到期就要自动消失"）走普通池行的待遇 —— 到期门②档先把行
        -- 从矩阵吃掉，三周没人再见到（散场后没有任何写点续它的 lastSeenAt）就把整条清出账本。
        local seen = tonumber(rec.lastSeenAt)
        local comparable = seen ~= nil and ((seen > 1e9) == nowEpoch)
        if not rec.ignored and (not rec.regKey or L.IsPinRegKey(rec.regKey))
            and comparable and nowN - seen > 3 * WEEK then
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

--- 矩阵行"上一帧列没列"的对账表（questID → 布尔）。MatrixRows 每轮写；只被翻面对账
--- 日志读（见 MatrixRows 里那段），不进存档 —— /reload 清零，从头对起。
local admitSeen = {}

--- 这条任务**这一条实例**报多少金币。金额优先来自当前签名的 observation，
--- 其次来自这一条实例的金币粘滞（`rec.goldMoney` + `rec.goldKey` 一对，写点是 NoteGoldThisInstance）。
--- 【它是行金额，不是某一号的到手】（2026-11-03 计划 §二第 3 条）：这一份的语义是
--- "这一条实例里**任意一个号**读到过的正数（谁最后同步算谁的）"，继续负责它本来的两件事——
--- 行不掉（双模式）、金币门槛；某一号的到手一律改问 reads（L.CharRewardMoney），
--- 界面不许再拿这份冒充"这一号挣了这么多"（用户 11-03 那句「麦德 … 到手 1089🪙」）。
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
    -- 交任务实测那一笔**不**把未判定这条转正（OnTurnIn 不量、也不存到手金币）——
    -- 奖励是不是金币只来自奖励读取，交任务那 15 秒钱窗捡到的钱不算数
    -- （用户定稿："不能依赖交任务的金额"）。
    if obs and obs.gold == true then return true end
    if obs and obs.gold == false then return false end
    -- 只剩"上一条实例读到过金币、这一条还没读到"这一种：L.QuestMoney 已经按编号拒了那份
    -- 粘滞，所以这里不会翻回"是"；也不许报"否" —— 客户端这一条根本没说它不给钱，
    -- 折成 false 等于替它答，行会当场消失（UI 该显示 ?）。
    return nil
end

--- 界面要分清话是谁说的：这笔金币是当前这一读到，还是同一条实例的另一档读到的。
--- 现在这一档不给金币却还按金币占行时返回金额，否则返回 nil。
--- 与 L.QuestMoney 同一份**行级**口径（"这一条实例里任意一个号读到过的钱"）：
--- 它只答"这行为什么还在列"，不答"某一号挣了多少" —— 按号那一问一律走 reads
--- （L.CharRewardMoney，2026-11-03 计划 §二第 3 条）。
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
    -- 行因此少一份"本轮已交"的**凭据**（只影响撞了关键词的那些行，见 L.QuestHasCredential）
    -- —— 掉不掉行归准入门那两类依据判（玩家点名 / 奖励金币过门槛），不是这里。
    if not curKey then return false end
    for _, row in pairs(db.chars or {}) do
        local view = row.views and row.views[questID]
        if view and view.s == "done" and L.SameInstanceKey(view.k, curKey) then return true end
    end
    return false
end

--- "有号把这条接进过日志"这一份**属于这一条实例的凭据**在不在（梦魇任务 94446 那一单，
--- 计划 docs/tasks/2026-10-29-keyword-log-weekly-resident.md §三第 2 条）。全仓唯一读点：
--- 除本函数以外任何地方都不许拿 rec 上那份编号去比 —— 判据散成两处迟早分岔，而这一族
--- 分岔的症状是"整行对全账号消失"（照 L.SameInstanceKey / L.QuestMoney 那份金币粘滞的先例，
--- tests 里 the_log_credential_has_one_read_home 按文件扫源码钉这一条）。
--- 签名与 L.DoneByAnyCharThisRound 同形状：curKey 末位可选，矩阵那条路必须传
--- MatrixRows 预扫好的 keyOf[questID]（§十一 每行每帧只算一次编号）；调用方手上没有
--- 本轮编号时传 nil，这里按 now 自己算一次**纯算术**（零新增接口调用，见 L.InstanceEnd
--- 那一段的禁令），一次都不许多问时钟。
--- fail-closed 方向与 DoneByAnyCharThisRound 一致：算不出编号（身份未定）⇒ 返回 false，
--- 不放行；`L.SameInstanceKey` 的 nil==nil 也不许放行（§五）。
--- 这一份只**放宽**准入（R1-R9 一律 `or` 在原条件后面），不给任何一格凭空打勾。
--- 【第一道为什么先问关键词】Sync 收尾那道撤点手上只有一个事实——"这条在本次日志清单的
--- keep 里没有"，可它背后是两种完全不同的原因：①任务离开了日志（玩家接完就交掉/放弃，
--- 用户 10-29 要的正是这一种**驻整周**）；②玩家把那条关键词删了（这行再没有绕过金币门槛
--- 的理由）。数据层分不开这两种，所以区分住在判据里：名字不再命中玩家自己的关键词 ⇒
--- 这份编号一律不作数。照 L.QuestKnown 那句现成的口径"玩家删了关键词这道门自动关闭
--- （不引入新字段、不留新状态）"，一个字节都不写进存档；同一周里把关键词加回来，行当场
--- 回来——"这一条实例被某个号的日志证明存在过"这件事不会因为删一次配置变成没发生过
--- （用例 a_removed_keyword_takes_the_log_credential_down_with_it 钉这两半）。
--- 代价照实：Config.KeywordMatch 是玩家配置的子串匹配，名字还没问出来的记录（rec.name 为
--- nil）判不出命中 ⇒ 不放行，方向是少报不是多报。
function L.KeywordLogAdmits(db, questID, rec, now, curKey)
    if not rec then return false end
    if not (GQL.Config and GQL.Config.KeywordMatch and GQL.Config.KeywordMatch(rec.name)) then
        return false
    end
    if curKey == nil then
        curKey = L.InstanceKey(db, questID, rec, now ~= nil and tonumber(now) or LedgerNow(db))
    end
    if not curKey then return false end
    return L.SameInstanceKey(rec.kwLogKey, curKey)
end

--- 这条记录有没有"客户端今天还答得出它"的凭据（命中任一即算）。第 32 轮准入门的唯一真源：
--- 名字撞关键词只是玩家说"这个名字我要看"，不构成"这条任务还真存在"——12.1.5 实测
--- 82157 特别任务：巨兽崛起 / 86174 特别任务：风暴在即 两条上上世代的垃圾行就是靠 kwHit
--- 一张名字通行证挂在矩阵里，还顶着当时 SetCycleExpiry 给的"下次周重置"倒计时
--- （那一组排程字段已在 T5 整组退役；计划 §2.1）。
--- 凭据集合全部是账本里已有的字段，零新增接口调用：进过世界任务池（poolSeen）/
--- 关键词+任务日志收进账本（kwLogAdded 当前号还挂着 / L.KeywordLogAdmits 这一条实例被
--- 某个号的日志证明存在过 —— 梦魇任务 94446 那一单，换一个没接它的号登录整行不该没）/
--- 注册过周常档（regKey）/ 读到过奖励金币
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
    -- regKey 只有"整周常驻"那一路算凭据（L.IsResidentRegKey）：`pin:` 是金币门自己收的，
    -- 它对"这条任务还真存在吗"没有比别的普通行更硬的说法 —— 那一趟扫描本身就是证据，
    -- 证据住在 poolSeen/mapID/lastMoney 这些现成字段里，不需要 regKey 再替它背一份。
    if rec.poolSeen or rec.kwLogAdded or L.IsResidentRegKey(rec.regKey)
        or L.KeywordLogAdmits(db, questID, rec, now, curKey) then return true end
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

--- 取证面的一份事实：**任一角色这一条实例交过这条**（on）+ 那几笔对应的金币（gain）。
--- 【第 39 轮：它不再是一道门】用户 2026-10-29 定稿"什么时候也没说做过就是准入门槛。
--- 1、匹配关键词的任务 2、金币奖励满足的任务" ⇒ "本轮交过"从 L.RowAdmitted 里整支删除，
--- 这里只留给 `/gql quest` 解释"那一格的绿勾是从哪一笔来的"。曾经用它占行的理由
--- （12.1.5 反馈④"松露大乱斗只有在狩猎模式下看才是金币任务，所以当一个角色完成一个
--- 符合条件的金币任务时，至少要加入矩阵"，那条的 962 金接口侧读不到）今天走的是正路：
--- 切一次模式就读到金币 ⇒ rec.goldMoney/goldKey 按实例粘滞 ⇒ 由金币门放行，不需要后门。
--- 比编号那半条口径不动（§四 R7）：**只认这笔流水写下的编号**。
--- 为什么"按轮次"那句在这里其实一直没生效：轮次起点判不出时兜底成周初，于是
--- `t >= 起点` 对本 CD 任何一笔都成立；另一半 `cycle == db.cycleAnchor` 在锚点定不出来
--- （=0）时一进门就放行 —— 94612 那十个假勾的两个入口都在这一段里。
--- 现在这两道门合成一道比编号：编号跟着结束时刻走，换了一条实例旧流水立刻匹配不上；
--- `cycle` 那一格**留着只给 /gql quest 取证**（计划 §六 第 2 条），判据一字不读它。
--- 周常照旧不受影响：常驻周常问不出板面倒计时，它的编号就是周 CD 尾巴那一桶
--- —— 比编号 == 比 CD，同一件事，不需要另设豁免。
--- gain 的优先级三条一条都不能反（它今天只是报告里的"到手 X"，摆错一位就是谎报）：
---   这一条实例奖励读取到金币（rec.goldMoney 且 rec.goldKey == 该笔交任务的实例编号）
---     → 那个正数（本来就该在列，不能被别号没读到顶掉）；
---   否则这一条实例没读到金币 → nil（判不了——读数失败/还没读到时就是 nil，
---     当成 0 会把松露大乱斗那种读数失败说成"白干"）；
---   读到金币的判定只来自奖励读取（DiffPool / NoteGoldThisInstance），交任务到手数已退役。
--- 【这一份是行级口径，不是某一号的到手】（2026-11-03 计划 §二第 6 条）：这一趟按 questID
--- 聚合、号在 `for _, byQuest` 里当场就被丢掉，所以它天生答不出"这一号挣了多少"。
--- 界面上一号的到手一律改问 reads（L.LastCompletion / L.WeekStats），这一份只喂
--- `/gql quest` 与周汇总取证，读它的人要记住：那句话是"**这一条实例读到过的金币**"。
--- on 的语义与第 25 轮那份逐字相同，只是不再提前 break：金额要扫全表才聚得出。
--- 性能（§十一）：`now` 在循环外定一次，每条任务的编号只算一次（同一条任务的流水散在
--- 14 个号里，逐笔重问就是把纯算术乘上角色数）；调用方不传 now 才走 LedgerNow 那一次本地时钟。
--- keyOf：调用方已经把整张「任务 → 编号」的表预扫好了就传进来，这一趟一次都不重算；
--- 别的调用方（取证面）不传，就在这里自建同一张表（false = 算过、算不出）。
--- 矩阵那一趟**不再**传 keyOf 进来预扫：第 39 起 on/gain 两份表在 MatrixRows 都没有读者，
--- 每帧少翻一遍 db.turnins（零新增接口调用，纯减少遍历）。
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
                            -- 金币只认奖励读取那份（rec.goldMoney，按实例焊死），交任务到手数
                            -- 已退役：这一条实例读过金币（rec.goldKey == r.k）才记正数，否则 unknown。
                            local rec = (db.quests or {})[questID]
                            local g = (rec and rec.goldKey == r.k
                                and type(rec.goldMoney) == "number")
                                and rec.goldMoney or nil
                            if type(g) == "number" and g > 0 then
                                if not pos[questID] then pos[questID] = g end
                            elseif g == nil then
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

-- 导出给 /gql quest 用：取证面要解释"那一格绿勾是从哪一笔来的"，
-- 自己数一遍 db.turnins 的报告会在判据改口径那天开始说瞎话。
-- 【第 39 轮：这两份只服务取证面，不再决定任何一行在不在列】占行的是准入门那两类依据
-- （L.RowAdmitted：玩家点名 / 奖励金币≥门槛），"本轮交过"从来不是准入门槛（用户 10-29 定稿）。
-- CD 那份（TurnedInThisCycle）同样只留给报告解释"本 CD 交过但这条实例匹配不上"。
-- TurnedInThisRound 只是 TurnInCred 的 .on 薄壳：返回形状（{[questID]=true}）一字不许变，
-- Dump 与用例都在读它；金额那份另开 L.TurnInGainsThisRound，同一份表同一趟遍历。
-- 矩阵那一趟**不再**预扫这两份表（第 39 轮起没有读者，每帧少扫一遍 db.turnins）。
-- now 透进去（取证面手边就有）：不传的话 TurnInCred 自己问一次本地时钟兜底。
L.TurnedInThisRound = function(db, now) return TurnInCred(db, now).on end
L.TurnInGainsThisRound = function(db, now) return TurnInCred(db, now).gain end
L.TurnedInThisCycle = TurnedInThisCycle

--- 【第 39 轮整块删除：L.TurnInAdmits】这里曾经是"本轮交过"这道准入门的唯一真源
--- （第 35 轮立、10-29 上午重写），判据只问 `goldConfirmed`——"这一条实例的奖励金币读到过没有"。
--- 它漏了金额够不够门槛，于是等于给"做过"发一张免门槛通行证（91594 狩猎：坚韧猎人
--- 34 金照样占一行），用户 10-29 定稿把它整体作废：准入门只剩玩家点名与金币≥门槛两类。
--- 函数本体不留死码；两代口径（第 25 轮"任一角色完成过就至少加入矩阵"、
--- 第 35 轮"接和交两处客户端都答 0 才否决"）同时作废，以 L.RowAdmitted 那份为准。

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
---    2026-10-29 起"日志收的"这一支还认 L.KeywordLogAdmits（这一条实例被某个号的日志
---    证明存在过的那份编号）—— 本周的凭据不许当遗产删；
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
                -- 这一条实例的日志凭据（L.KeywordLogAdmits）与 kwLogAdded 同罪同判：
                -- ①不放宽 ⇒ 本周刚烙的凭据会被当成遗产整条删掉；②不放宽 ⇒ 给它写 roundEnd
                -- 会把身份从周 CD 尾巴挪到板面档，刚烙的那份凭据当场对不上（梦魇任务 94446）。
                local logAdmits = L.KeywordLogAdmits(db, questID, legacyRec, wall)
                if exp < wall then
                    if not legacyRec.regKey and not legacyRec.kwLogAdded
                        and not logAdmits and not legacyRec.ignored then
                        db.quests[questID] = nil
                        purged = purged + 1
                    end
                elseif not legacyRec.roundEnd and not legacyRec.regKey
                    and not legacyRec.kwLogAdded and not logAdmits then
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

--- v10 一次性补烙（计划 docs/tasks/2026-10-29-keyword-log-weekly-resident.md §三"老存档
--- 不自愈 ⇒ v10 迁移"）：代码修好了，**已经写进存档的那一份不会自愈**（v2/v6/v7/v9
--- 同一族教训）。真档 14:57 落盘那份里 94446 梦魇任务此刻就挂着 `kwLogAdded` 而没有编号：
--- 更新完第一次登录如果用的正是"没接这条的号"，Sync 收尾那道撤点当场把布尔位撤走，而这条
--- 记录没有第二份凭据可留 ⇒ 整行对全账号消失 —— 用户会读成"代码改完了矩阵一点变化没有"，
--- 正是这一族反馈的根。所以这一档给当下挂着布尔位的记录烙上**当前**这条实例的编号
--- （走 L.NoteKeywordLogThisInstance 那唯一写法点：它自己算编号，问不出板面倒计时就落
--- 周 CD 尾巴档，下周重置一到尾巴整段挪一档、凭据自己作废，不需要任何清理代码）。
--- 三条边界：
--- ① 只认 `kwLogAdded` 在场且 `kwLogKey` 还没有的（幂等：重跑 0 条，已烙过的一字不动）；
--- ② 没有布尔位的记录一条不碰 —— 方向是**少报不多报**，凭空发凭据等于给玩家从没要过的
---    行发一张上板通行证（v6/v9 同一句口径）；
--- ③ 只烙编号不给勾：勾仍只来自 views/turnins/manual 那三份带编号的凭据，
---    这里既不写 roundEnd 也不动角色级快照（无背书 done 那一族归 v9 管）。
--- 时钟照 v6/v7 的邻居写法：一趟迁移只问一次（调用方给了就用，没给才问 Api），
--- 不是每条记录问一遍；每条记录按自己 `roundEnd` 的时钟域挑 now（v6 同款），
--- 算不出编号（尾巴定不出来 / 跨域证明不了活着）就 fail-closed 一笔不写，宁缺不编。
--- **锚点过期要先补问一次**（照 v6 `BackfillInstanceKeysOnce` 开头那道守卫，理由同源）：
--- 这一档的编号来自 `cycleAnchor + 604800` 那档尾巴，而 `Config.Init` 只在锚点为 0/nil 时
--- 补问周重置、旧 CD 的值它不动 ⇒ "周四刚过、代码刚更新"那一趟拿的是**上周**的尾巴，
--- 烙出来的编号比当前编号低一整周，同一趟里锚点随后前进 ⇒ 凭据对不上自己、行照旧消失，
--- 用户读成"改完那一整周都没变化"。代价：只有锚点确实过期时多问一次周重置，没过期 0 次。
--- 返回值 = 烙上的笔数（Config.Migrate 那句通用日志用它）。
function L.StampKeywordLogKeysOnce(db, wallNow)
    if not db or type(db.quests) ~= "table" then return 0 end
    local wall, up = wallNow, nil
    if GQL.Api and GQL.Api.Uptime then up = GQL.Api.Uptime() end
    if wall == nil and GQL.Api and GQL.Api.WallClock then
        local okCall, v = pcall(GQL.Api.WallClock)
        wall = okCall and v or nil
    end
    wall, up = tonumber(wall), tonumber(up)

    local anchor = tonumber(db.cycleAnchor)
    if anchor and anchor > 0 then
        local ref = (anchor > 1e9) and wall or up
        if ref and (ref > 1e9) == (anchor > 1e9) and anchor + 604800 <= ref
            and GQL.Config and GQL.Config.RefreshCycle then
            GQL.Config.RefreshCycle(wall, up)
        end
    end

    local stamped = 0
    for questID, rec in pairs(db.quests) do
        if type(rec) == "table" and rec.kwLogAdded and not rec.kwLogKey then
            local endAt = tonumber(rec.roundEnd)
            local now = endAt and ((endAt > 1e9) and wall or up) or (wall or up)
            if L.NoteKeywordLogThisInstance(db, questID, rec, now) then
                stamped = stamped + 1
            end
        end
    end
    return stamped
end

--- 一次性清掉老存档里的**准入粘滞** `rec.admittedKey`（schema v11，2026-10-29 狩猎：坚韧猎人
--- 91594 那一单，计划 docs/tasks/2026-10-29-two-admission-doors.md §三 W3）。
--- 为什么必须清：判据里"准入粘滞"和准入门是**或**的关系，而 L.MatrixRows 每帧重烙一次。
--- 第 39 轮把"本轮交过"那一支从准入门里删掉之后，代码不再写这个字段、也不再读它，
--- 但存档里已经烙进去的那一份不自愈 ⇒ 91594 还会顶着上周那次放行留板到自己的倒计时走完
--- —— 正是第 34/36 轮那一族"改完读成没变化"的形状（v2/v6/v7/v9/v10 同一课）。
--- 闸门只有档位：Migrate 逐档跑（from+1..SCHEMA_VERSION），这一档只在 db.schema < 11 时跑到，
--- 所以这里不设自定义标记位（v4/v5/v8/v9 邻居同一写法 —— 加一个"清过没有"的字段
--- 等于给下一次修的档再埋一个坑，见 ResetStickyGoldOnce 那句"标记分档"的教训）。
--- 天然幂等：清完第二次再跑找不到 admittedKey，返回 0。
--- 【只碰这一个字段】金币粘滞 `goldMoney`/`goldKey`（双模式不掉行靠它）、日志凭据 `kwLogKey`、
--- 角色快照、流水一律不动；不清 roundEnd，不改 ignored/regKey。
--- 返回值 = 清掉的笔数（Config.Migrate 那句通用日志用它）。
function L.ClearAdmittedKeysOnce(db)
    if not db or type(db.quests) ~= "table" then return 0 end
    local n = 0
    for _, rec in pairs(db.quests) do
        if type(rec) == "table" and rec.admittedKey ~= nil then
            rec.admittedKey = nil
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

--- 准入门的唯一真源（判据的来历与逐条说明见 MatrixRows 里那一大段注释）。
--- 第 34 轮从 MatrixRows 里提出来，不是为了好看：`/gql quest` 以前自己拼一句写死的猜话
--- （"没命中关键词、本轮没人交过，金币也没到 X"），12.1.5 实测它和紧下面那句
--- "在列靠：本轮有角色交过"直接打架，真砍掉这一行的到期门一个字没提；第 32 轮把
--- "凭据"那半条加进门判时报告漏了一整轮，也是同一个价。
--- money / kwHit / floorMoney 一律由调用方算好传进来：MatrixRows 每帧重画都要用
--- 同一批数，在这里重算等于给热路径加一遍账本遍历（手动金币那份表与问 Config 的门槛尤其贵，
--- 上一版为此专门留了"门槛一次读"那行）。诊断报告传 nil 门槛 = 每条现问一次，无所谓。
--- 【第 39 轮：准入门收成两类依据（用户 2026-10-29 定稿）】
--- "什么时候也没说做过就是准入门槛。1、匹配关键词的任务 2、金币奖励满足的任务"。
--- 以前中间那一支"本轮有角色交过"（判据 L.TurnInAdmits）整支删除，`turnedIn` 参数一并
--- 从签名里去掉。它为什么必须是死：TurnInAdmits 只问"这一条实例的奖励金币**读到过**没有"
--- （L.GoldVerdict 的三态：正数→true / 0→false / 读不出→nil），金额够不够门槛不在它口径里
--- ⇒ 它等于给"做过"发了一张免门槛通行证。12.1.5 真档 91594 狩猎：坚韧猎人就是这一形状：
--- 奖励 34 金（observation.money=341600）、门槛 500 金（5000000）、名字不撞任何关键词，
--- 仅凭"爱情之力 2 小时前交过它"（编号 91594:1992572）一直挂在矩阵上，而它自己的 probe
--- 段写着"不到门槛又不命中关键词就不列" —— 判据和取证话术打架，玩家照报告理解规则必被误导。
--- 现在只剩三行、两类依据：①玩家点名（关键词从日志收的 / 注册周常 / 名字撞关键词且有
--- 客户端凭据）②奖励金币读到过**且够门槛**。
--- "本轮交过"在取证面还有用（解释那一格绿勾从哪来），所以 L.TurnedInThisRound /
--- L.TurnInGainsThisRound 留着，但它们不再决定任何一行在不在列。
--- now / curKey 末两位：②的金币和③的凭据都改问编号（§四 R10/R6），而编号每行只许算一次
--- （§十一）⇒ 由 MatrixRows 的预扫传进来，这里只往下透。
--- ①第一支"日志收的"认两份：当前号日志里挂着（kwLogAdded）+ 这一条实例被某个号的日志
--- 证明存在过（L.KeywordLogAdmits）；后者撑的是用户 10-29 那句"只要有一个号接了…
--- 不管是不是金币任务，都要上矩阵"，所以这一支**继续绕金币门槛**。
function L.RowAdmitted(db, questID, rec, money, kwHit, floorMoney, now, curKey)
    if not rec then return false end
    -- ① 玩家点名要看的：日志收进来的 / 注册周常。金币门从事件 pin 自己收的那一路（`pin:`）
    --   不算点名（L.IsResidentRegKey，用户 2026-11-02「和其他普通金币任务一样」）——
    --   它一旦算，金币读数后来掉了也照样绕门槛占着行，那就不是普通金币任务了。
    if rec.kwLogAdded or L.IsResidentRegKey(rec.regKey)
        or L.KeywordLogAdmits(db, questID, rec, now, curKey) then return true end
    -- ② 奖励金币读到过且够门槛（L.QuestMoney 自带按实例的双模式粘滞，掉行不由这里管）
    if money and money >= (floorMoney or L.MatrixMoneyFloor()) then return true end
    -- ①的另一种形状：名字命中玩家关键词 + 客户端答得出至少一条凭据（第 32 轮口径不动）
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
    -- 【第 39 轮删掉这一趟预扫】以前每帧在这里 TurnInCred(db, keyNow, keyOf) 把"本轮交过"
    -- 两份表（on 占行 / gain 给"两处 0 才否决"那道门）扫出来喂准入门。准入门收成两类依据
    -- 之后 on/gain 在 MatrixRows 都没有读者了（取证面另开 L.TurnedInThisRound /
    -- L.TurnInGainsThisRound，只在 /gql quest 那一趟扫）⇒ 每帧少翻一遍 db.turnins，
    -- 零新增接口调用。keyOf 那份预扫照旧留着（金币、凭据、快照都还认这把编号）。
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
        -- 显示条件（准入门，两类依据：玩家点名 / 奖励金币够门槛；判据真源 L.RowAdmitted）：
        -- ① 玩家点名要看的 —— 关键词从日志收的（kwLogAdded / 这一条实例被某个号的日志证明
        --    存在过 L.KeywordLogAdmits）、注册周常（regKey）、名字命中玩家关键词**且**这条记录
        --    答得出至少一条客户端凭据（kwHit and L.QuestHasCredential：池/日志/注册/读到过钱/
        --    客户端答的图/这一条实例上任一角色被答成已完成）。
        --    第 32 轮把以前"kwHit 单独放行"降成最后这一条：12.1.5 实测两条上上世代的垃圾行
        --    （82157 巨兽崛起 / 86174 风暴在即）零凭据、纯靠名字撞关键词上板，还顶着
        --    "下次周重置"的假倒计时（计划 §2.1）。同一批里 96718 奈格塔尔虚空决战（英雄）
        --    **不是**垃圾行：14 个号本轮都被客户端答成已完成，只是它交完就离开日志、
        --    把日志那道凭据撤掉了 —— 所以"本轮任一角色被客户端答成已交"也在凭据集合里
        --    （用户 2026-10-27："这个不应该消失，这是我手动添加的关键词，正巧匹配到了我要的任务"）。
        -- ② 奖励金币读到过**且**≥门槛（门槛=设置面板那条，默认 500g；L.QuestMoney 自带
        --    按实例的双模式粘滞，所以"一档读到金币、切另一档读到 0"不会因为当场读到非金币掉行）。
        -- 【第 39 轮：以前这里还有第三类"本轮有角色交过"，整支删除】
        -- 用户 2026-10-29 定稿："什么时候也没说做过就是准入门槛。1、匹配关键词的任务
        -- 2、金币奖励满足的任务"。那一支（判据 L.TurnInAdmits）只问"奖励金币读到过没有"、
        -- 不问够不够门槛 ⇒ 做过等于一张免门槛通行证：真档 91594 狩猎：坚韧猎人奖励 34 金、
        -- 门槛 500 金、不撞任何关键词，仅凭"爱情之力两小时前交过它"一直占着一行，
        -- 而它自己的报告写着"不到门槛又不命中关键词就不列"。代价照实（计划 §四）：
        -- 34 金这种做掉了的板面任务不再为它留行，那一格的绿勾照旧在（勾来自带编号的
        -- views/turnins 凭据，本轮不动）；想留行的正路是加关键词或调门槛，都是配置项。
        -- 代价照实：真存在但客户端一条都答不出的特别任务，不接就不上板（§2.5，本轮不假装解决）。
        -- 永久否决只有一道：玩家在「关键词 · 屏蔽管理」点"忽略"过的（rec.ignored）永远不列 ——
        -- 池子和日志每轮都会重新建记录，只删不管用，必须认这条意图
        -- （12.1.5 实测反馈："删不掉，右键点击后还会刷出来"）。它**永久**有效，换 CD、
        -- 换轮都不作废，只有玩家在设置页的屏蔽管理里点回来才撤（见 L.RemoveQuest）。
        -- 这两类之外以前还有 `rec.manualAdded`（点过一次添加就绕门槛，还连带
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
        local admittedNow = L.RowAdmitted(db, questID, rec, money, kwHit,
            floorMoney, keyNow, curKey)
        -- 【第 39 轮删掉"准入粘滞"】以前这里每帧把 admittedNow 烙进 rec.admittedKey，
        -- 下面再用 `admittedNow or rec.admittedKey == curKey` 当"或"放行 —— 粘滞会把
        -- ①那一支的免门槛放行冻进存档（91594 靠它留板到 8 时 54 分到期），只删判据不删
        -- 粘滞就是玩家读到的"改完没变化"。现在在列只认当场这一帧算出的 admittedNow。
        -- 双模式不掉行**不靠**这份粘滞：L.QuestMoney 里 `rec.goldKey == curKey` 那份按实例
        -- 金币粘滞负责（一档读到 962 金、另一档读到 0，门槛那道照样拿到 962 ⇒ 由②放行）。
        -- expired 提到准入门外面算：下面的对账日志（行出现/消失）要把两道门的结论
        -- 一起摊出来，闪现只活几秒、截图截不到在场那一帧，只有翻面当场记的这一条
        -- 能分清是金币门、关键词凭据还是到期门在抖（用户 10-31 的"出现2次消失2次"）。
        -- 纯账本字段，零新增接口调用；对每条记录都算一次的开销是几个字段读。
        local expired = L.InstanceExpired(db, rec, now)
        local listed = false
        -- 到期即移除：账本里的记录仍在（快照/prune 的事），只是不再显示。判据是这条
        -- 实例自己的结束时刻（L.InstanceExpired，计划 §七）：一个已经过去的到期时刻是
        -- **上一条实例**留下的墓碑，不作数（89267"第二个玩家登录了几分钟，矩阵出现了"
        -- 就是它把整行吃掉的）；这一条自己到点照旧死行（96718"时间结束后都要移除"）。
        -- 它与准入门都只问 `L.InstanceEnd` 那一个真源，remaining 只报还没过去的那半，
        -- 不许谁自拼第二份到期账 —— 那会出现"行还在、悬停写剩余 已到期"的谎报。
        if rec and not rec.ignored and L.OccupiesRow(db, questID) and admittedNow then
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
                    -- 终态有两档：done（做了）和 skip（右键记的"这一号本轮不做"，
                    -- 2026-10-30）。用户 2026-10-31："全部标记做完或不做，任务名称要置灰"
                    -- —— 一排 ✓ 和 × 混着的行同样扫得出"这行不用再看了"，只有
                    -- 问不出（?）和还没处理的空格才挡着置灰。
                    if st ~= "done" and st ~= "skip" then allDone = false end
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
                -- 日志那份凭据走预扫好的 curKey（§十一 每行每帧只算一次编号），
                -- 这一行上面 14 个格子与准入门用的都是同一把键。
                row.watched = (kwHit or rec.kwLogAdded or fam
                    or L.KeywordLogAdmits(db, questID, rec, keyNow, curKey)
                    or row.groupMapID == WEEKLY_GROUP_MAP)
                    and true or nil
                rows[#rows + 1] = row
                listed = true
            end
        end
        -- 行出现/消失的对账日志（用户 10-31："该任务本轮不是金币，却出现2次消失2次"）。
        -- 同一条任务相邻两次重算的"列不列"翻面时，把两道门的判据原样摊一条进 /gql log
        -- —— 闪现只活几秒、截图永远截不到它在场的那一帧，只有翻面当场记下的这一条能
        -- 分清是金币门（钱）、关键词凭据、注册还是到期门在抖。只在翻面那一刻写一条，
        -- 稳态零开销；全部是现成账本字段，零新增接口调用。admitSeen 挂内存不进存档。
        local was = admitSeen[questID]
        if was ~= nil and was ~= listed and GQL.Config and GQL.Config.Log then
            GQL.Config.Log(("矩阵行%s quest=%s 名=%s 钱=%s 关键词命中=%s 注册=%s 日志凭据=%s 到期=%s")
                :format(listed and "出现" or "消失", tostring(questID), tostring(name),
                    money and tostring(money) or "问不出",
                    tostring(not not kwHit), tostring(not not (rec and rec.regKey)),
                    tostring(not not (rec and (rec.kwLogAdded
                        or L.KeywordLogAdmits(db, questID, rec, keyNow, curKey)))),
                    tostring(expired)))
        end
        admitSeen[questID] = listed
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
    -- 组内任务按结束时间正序排（剩余越少 = 越早结束，越靠上）：最先过期的顶在最前，
    -- 玩家一眼看到该先去清哪条。remaining = 结束时刻 − now，升序即结束时刻升序。
    -- 问不出倒计时（remaining=nil，接口没给数）的活行沉到本组末尾，不抢有倒计时的行。
    -- **稳定排序**：g.rows 进这道之前是"收益高者在前、同额按 questID"（见上面 ids 那趟），
    -- 相等或都是 nil 时按这份原序兜底 —— table.sort 不稳定，不兜底每帧重画可能换位
    -- （与顶层 ids 排序同样的坑），也会把没有结束时间的普通行从收益序打散。
    for i = 1, #out do
        local g = out[i]
        local idx = {}
        for j = 1, #g.rows do idx[g.rows[j]] = j end
        table.sort(g.rows, function(a, b)
            local ra, rb = a.remaining, b.remaining
            if ra == nil and rb == nil then return idx[a] < idx[b] end
            if ra == nil then return false end
            if rb == nil then return true end
            if ra == rb then return idx[a] < idx[b] end
            return ra < rb
        end)
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
