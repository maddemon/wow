-- 默认配置包：按**客户端版本**生效的数据文件。
--
-- 为什么单独一份文件：用户 2026-11-01 定稿（原话）"我希望有个默认配置文件 来配置
-- 地图和关键词，这个配置依赖于游戏版本，比如现在是12.1.5，那我们的地图配置应该是
-- ^12.1.5 这样以后有12.1.7 12.2.5等，这些地图是兼容的，等到13.0 这个配置就自动
-- 不起作用了"。所以插件以后出大版本更新时，改的多半是这张表，不是代码。
--
-- 口径（改之前先读完这四条，都是往回踩过的坑）：
--   ① version 以 "^" 开头 = **caret 语义（不是字符串前缀）**：`^12.1.5` 读作
--      "12.1.5 及以上、且仍是 12.x"：12.1.5 / 12.1.5.58352 / 12.1.7 / 12.2.5 全成立，
--      13.0.0 不成立 ⇒ 资料片一换这张包自动失效，不需要谁去删代码，也不会把
--      上一资料片的地图带进新版本。
--      为什么不是前缀匹配：12.1.7 不是以 "12.1.5" 开头的字符串 —— 按前缀写，
--      用户原话里"以后有 12.1.7、12.2.5，这些地图是兼容的"这句根本不成立。
--   ② 一个包只套一次：db.config.defaults[key] 记着"这个包已经套过"
--      （key = version .. "#" .. rev）。玩家之后删掉的地图/关键词不会又冒回来
--      —— 只有我们**改了包的内容**（把 rev 加 1）才会再套一遍，那是新内容，
--      代价是玩家可能得再删一次默认项，可以接受。
--   ③ 包里的地图写**名字**不写 mapID：mapID 是猜出来的数字，写错一个就是一个
--      静静不生效的配置；名字走 Config.AddMapByName（全等优先、大小写不敏感，
--      命中不唯一就**不收**并记日志 —— 宁可不收也不悄悄多收，T14），
--      接口答不出就跳过并记日志，绝不编一张图。子图归属仍然由客户端答
--      （Config.AddMap 加图时向下记子图，见那里的注释），这里不用管。
--   ④ 套包失败（登录那一刻地图树还没就绪、Api.WQMapList 问不出来）时**不记套过**，
--      下一次进世界由 Sync.OnWorldEnter 重试一次。已经加过的地图/关键词走
--      Config.AddMap / Config.AddKeyword 的去重，重试不会重复添加。
--
-- 加新包时：version 用"这一套数据第一次可用的版本"，rev 从 1 开始；
-- 只在**确实要动的版本**上加一条，别每个小版本都复制一份（包越多越难判断谁在生效）。
GQL.Defaults = {}
local D = GQL.Defaults

D.PACKS = {
    {
        version = "^12.1.5",
        rev = 2,
        -- 12.1.5 的几张主区（照用户 2026-11-01 面板原样抄：哈籁恩达尔 / 永歌森林 /
        -- 盘卷蛇岛 / 祖阿曼 / 虚影风暴 —— 是"盘卷蛇岛"，不是"盘蛇岛"，玩家已经纠正过
        -- 一次）。屠戮者高地这类**子图**不写：它们是虚影风暴的子图，由 Config.AddMap
        -- 自动带叶子树（写进来只会在面板里多出一行同义图）。
        -- 名字按客户端答的 locale 匹配（中文客户端写中文名），匹配不上会在日志里点名。
        maps = { "哈籁恩达尔", "永歌森林", "盘卷蛇岛", "祖阿曼", "虚影风暴" },
        -- 用户自用的那一串（2026-11-01 截图逐条抄的原文）。前几条这几族不在世界任务
        -- 池里（周常/事件型），靠关键词 + 扫描地图才收得进来；后面几条是按任务名筛
        -- 特定主线/事件。故意保留有重叠的（"亚基" ⊂ "亚基入侵"）：命中的是同一个任务，
        -- 关键词只是取名的门，重复不会让同一行走两遍。
        keywords = { "特别任务", "符文石", "斯托玛兰", "追踪萨拉塔斯", "亚基",
                     "虚空决战", "丰饶供品", "世界首领", "梦魇任务", "回响复燃",
                     "虚空侵袭", "亚基入侵" },
    },
}

--- 客户端的游戏版本串（"12.1.5"）。取不到回 nil —— 绝不拿 Interface 号猜一个版本。
--- GetBuildInfo 的四个返回值里只有第一个是版本串，其余是构建号/日期/界面号。
function D.BuildVersion()
    -- 判"能不能调"用 pcall，不用 type(x)=="function"：宿主/翻译层给的函数不一定是
    -- Lua function（离线替身里就是个 userdata），拿类型判会把好好的接口判成"没有"。
    local fn = GetBuildInfo
    if fn == nil then return nil end
    local okCall, v = pcall(fn)
    if okCall and type(v) == "string" and v ~= "" then return v end
    return nil
end

--- 版本串拆成数字段：'12.1.5' → {12,1,5}；'12.1.5.58352' → {12,1,5,58352}。
--- 逐段比数字而不是比字符串，才能让 "12.10" 排在 "12.9" 后面。
local function VersionParts(v)
    local out = {}
    for seg in tostring(v):gmatch("%d+") do out[#out + 1] = tonumber(seg) end
    return out
end

--- 比大小：a>b 回 1、a<b 回 -1、相等回 0。缺的段按 0 补（12.1 == 12.1.0）。
local function CompareVersion(a, b)
    local A, B = VersionParts(a), VersionParts(b)
    local n = math.max(#A, #B)
    for i = 1, n do
        local x, y = A[i] or 0, B[i] or 0
        if x ~= y then return x > y and 1 or -1 end
    end
    return 0
end
D.Compare = CompareVersion

--- 版本匹配：pattern 以 "^" 开头走 caret 语义（见文件头①）：build ≥ 基点且主版本相同；
--- 不以 "^" 开头要求数值相等（"12.1.5" 与 "12.1.5.0" 视为同一个版本）。
--- 版本串或 pattern 取不到一律 false：宁可这条包不生效，也不许"匹配不上却当成匹配"。
function D.Matches(build, pattern)
    if type(build) ~= "string" or build == "" then return false end
    if type(pattern) ~= "string" or pattern == "" then return false end
    if pattern:sub(1, 1) == "^" then
        local base = pattern:sub(2)
        local B = VersionParts(base)
        if #B == 0 then return false end
        if CompareVersion(build, base) < 0 then return false end
        -- 主版本必须相同：12.x 的包到 13.0 自动失效（用户口径里那句"等到13.0 这个配置
        -- 就自动不起作用了"就是这一行）
        return (VersionParts(build)[1] or 0) == B[1]
    end
    return CompareVersion(build, pattern) == 0
end

--- 当前版本该套哪个包（第一个匹配的，按表顺序）。返回 pack, key。
function D.Pick(build)
    build = build or D.BuildVersion()
    for i = 1, #D.PACKS do
        local pack = D.PACKS[i]
        if D.Matches(build, pack.version) then
            return pack, pack.version .. "#" .. tostring(pack.rev or 1), build
        end
    end
    return nil, nil, build
end

-- "没收进来的图名"的落日志口径：按 Config.AddMapByName 的 reason 分开写，
-- 措辞给玩家读，不塞取证细节（T14）。
local NO_ADD_WHY = {
    ambiguous = "命中几张图，不能替你选",
    notfound = "没匹配到",
    unavailable = "地图清单没就绪",
    addfail = "没能写入跟踪",
}

--- 套一个包：关键词先（零接口依赖），再按名字加地图。
--- 返回 true = 这一包处理完了（该记"已套过"）；false = 地图表还问不出来，下次重试。
--- 日志三分（T14）：新加 / 已在跟踪（幂等，不算改动，旧版把它混进"没匹配到"那句
--- 就是措辞说谎）/ 没收进来（歧义、没匹配到……按 reason 分开写）。
local function ApplyPack(db, pack)
    local nKw, dupKw = 0, 0
    local kws = pack.keywords or {}
    for i = 1, #kws do
        local okAdd = GQL.Config and GQL.Config.AddKeyword
            and select(1, GQL.Config.AddKeyword(kws[i]))
        if okAdd then nKw = nKw + 1 else dupKw = dupKw + 1 end
    end
    local names = pack.maps or {}
    local list = (GQL.Api and GQL.Api.WQMapList and GQL.Api.WQMapList()) or nil
    if not list then
        -- 地图树还没就绪（登录早期很常见）：关键词已经落了，地图下趟再来
        GQL.Config.Log(("默认配置包 %s：地图清单还问不出来，地图部分下次进世界重试（已加关键词 %d）")
            :format(tostring(pack.version), nKw))
        return false
    end
    local nMap, nAlready = 0, 0
    local noAdd = {}
    for i = 1, #names do
        local nm = names[i]
        local added, already, reason = 0, 0, "addfail"
        if GQL.Config and GQL.Config.AddMapByName then
            local _, _, a, al, why = GQL.Config.AddMapByName(nm)
            added, already, reason = tonumber(a) or 0, tonumber(al) or 0, why
        end
        nMap = nMap + added
        nAlready = nAlready + already
        if added == 0 and already == 0 then
            noAdd[#noAdd + 1] = tostring(nm)
                .. "（" .. (NO_ADD_WHY[reason] or "没匹配到") .. "）"
        end
    end
    GQL.Config.Log(("默认配置包 %s rev=%s：新增地图 %d 张、关键词 %d 个"
        .. "（已在跟踪：关键词 %d 个、地图 %d 张；不算改动）%s")
        :format(tostring(pack.version), tostring(pack.rev or 1), nMap, nKw, dupKw, nAlready,
            #noAdd > 0 and ("；没收进来的图名：" .. table.concat(noAdd, "、")) or ""))
    return true
end

--- 把当前版本适用的包套进账本。幂等：已套过的包直接跳过（口径②）。
--- 返回本次真正套上的包 key（没有则 nil），给 /gql version 与日志用。
function D.Apply(db)
    db = db or (GQL.Config and GQL.Config.DB and GQL.Config.DB())
    if not db or not db.config then return nil end
    local cfg = db.config
    cfg.defaults = cfg.defaults or {}        -- { [包 key] = true } 已经套过的包
    local pack, key, build = D.Pick()
    if not pack then return nil end           -- 版本问不出 / 没有任何包匹配（如 13.0）
    if cfg.defaults[key] then return nil end
    if not ApplyPack(db, pack) then return nil end
    cfg.defaults[key] = true
    GQL.Config.Log(("默认配置包生效 %s（客户端 %s）"):format(tostring(key), tostring(build)))
    return key
end

--- 探针/`/gql version` 用：当前版本、匹配到的包、已经套过的包，一句话说完。
function D.Describe(db)
    db = db or (GQL.Config and GQL.Config.DB and GQL.Config.DB())
    local build = D.BuildVersion() or "问不出"
    local pack, key = D.Pick()
    local applied = db and db.config and db.config.defaults or {}
    local done = {}
    for k in pairs(applied) do done[#done + 1] = tostring(k) end
    table.sort(done)
    return ("客户端 %s · 适用配置包 %s · 已套过 %s"):format(
        tostring(build), tostring(key or "无（这个版本没有对应配置）"),
        #done > 0 and table.concat(done, "、") or "无")
end
