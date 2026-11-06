-- 阶段一只读导出：矩阵 UI 属阶段二，本阶段用文本确认账本记录的是真实情况。

GQL.Dump = {}
local Dump = GQL.Dump

local STATUS_TEXT = {
    done = "已交", progress = "在办", open = "待做",
    ended = "本轮结束", unknown = "?",
}

--- 距今多久（"3天前"）。缺时间返回 "?"；未来时刻压成"0分前"，反常一眼看得出来。
local function Ago(t, now)
    if type(t) ~= "number" or type(now) ~= "number" then return "?" end
    local s = now - t
    if s < 0 then s = 0 end
    if s >= 86400 then return ("%d天前"):format(math.floor(s / 86400)) end
    if s >= 3600 then return ("%d小时前"):format(math.floor(s / 3600)) end
    return ("%d分前"):format(math.floor(s / 60))
end

--- 到期时刻怎么读法：将来=还有多久，过去=已到期（Ago 会把负数夹成"0分前"，
--- 拿它报将来的时刻等于报"0分前"，那是谎报）。
--- 秒数→文字一律走 Config.FormatTimeLeft：这份读法原来在本文件又抄了一遍（SpanLeft），
--- 同一分钟的剩余时间导出和别处报两个数，是最难查的分歧。
local function WhenEnd(t, now)
    if type(t) ~= "number" or type(now) ~= "number" then return "问不出" end
    if t > now then return "还有 " .. GQL.Config.FormatTimeLeft(t - now) end
    return "已到期（" .. Ago(t, now) .. "）"
end

local function MapNameOrId(mapID)
    if type(mapID) ~= "number" or mapID <= 0 then return "无图" end
    local name = GQL.Api and GQL.Api.MapName and GQL.Api.MapName(mapID)
    return tostring(name or ("地图" .. mapID))
end

function Dump.Run()
    local db = GQL.Config.DB()
    if not db then return "GQL: 账本还没就绪（登录后再试，详情 /gql log）" end
    local L = GQL.Ledger
    -- now 必须跟入库时间戳同域，否则"过期 7 天"这类判断跨开机就失效
    local now = (GQL.Api and GQL.Api.WallClock()) or math.floor((GQL.Api and GQL.Api.Uptime()) or 0)
    local domain = db.clockDomain or "uptime"
    local lines = { ("GQL 账本导出 cycleAnchor=%s now=%d 时钟域=%s%s")
        :format(tostring(db.cycleAnchor), now, domain,
            domain == "epoch" and "" or "（仅本次开机内可比，周统计不可靠）") }

    local chars = L.Characters(db)
    local anchor = db.cycleAnchor or 0

    lines[#lines + 1] = "== 本周汇总 =="
    -- 列头数据只有一份，在 GQL.Ledger.MatrixColumns 里（矩阵画的就是它）：
    -- "快照过期"阈值、本周完成数/金币在这儿各算一遍的话，改一处就出现矩阵与导出对不上。
    -- 锚点定不出来（0）或时钟域分裂时它给 -1，这里显示 ? 而不是 0 ——
    -- Lua 里 0 是真值，拿 WeekStats 的 r.t >= 0 硬筛会把历次 CD 全算进"本周"。
    local cols = GQL.Ledger.MatrixColumns(db, now)
    if #cols == 0 then lines[#lines + 1] = "（还没有任何角色同步过）" end
    for i = 1, #cols do
        local c = cols[i]
        -- 金币同样把 -1 直接交给 FormatGold（它对负数就报 ?）：在这儿夹成 0
        -- 等于把"问不出"说成"这个号一分钱没挣"
        lines[#lines + 1] = ("%s  完成 %s  金币 %s  同步于 %s%s"):format(
            c.key, c.count < 0 and "?" or tostring(c.count),
            GQL.Config.FormatGold(c.gold),
            tostring(c.lastSync), c.stale and "（快照过期）" or "")
    end

    lines[#lines + 1] = "== 金币任务 =="
    local quests = L.GoldQuestIDs(db, now)
    if #quests == 0 then
        lines[#lines + 1] = "（无金币任务：奖励接口可能不可用，见下方未判定计数）"
    end
    for i = 1, #quests do
        local questID = quests[i]
        local cells = {}
        for c = 1, #chars do
            local st = L.State(db, chars[c], questID, nil, now)
            cells[#cells + 1] = STATUS_TEXT[st] or tostring(st)
        end
        lines[#lines + 1] = ("%s%s [%s]"):format(
            L.GetQuestName(db, questID) or tostring(questID),
            L.IsHighFreq(db, questID) and "（高频）" or "",
            table.concat(cells, " | "))
    end

    -- 周常家族单独一节：金币任务那节按"金额≥门槛"筛，丰饶供品这种金额问不出的
    -- 根本不会出现在里面 —— 12.1.5 用户回传的导出里没有这一行，等于最该取证的那条
    -- 永远取不到证。族、成员（谁在图上谁没在）、每角色家族状态都在这里摊开。
    lines[#lines + 1] = "== 周常家族 =="
    local weekly, weeklySrc = nil, nil
    if GQL.Api and GQL.Api.WeeklyResetCountdown then
        weekly, weeklySrc = GQL.Api.WeeklyResetCountdown()
    end
    local cycleEnd, cycleKind = GQL.Config.NextCycleEnd(now)
    -- 周口径的原始证据单独一行：12.1.5 实测锚点=1792623599（周四 07:00，对的），
    -- 同一个"周"接口周五晚上却报出 3 小时 —— 那是每日档冒充周档。光看界面上的
    -- "距下次周重置 X"分不出是谁答错的，必须把倒计时原值、来源、自检结果并排打出来
    lines[#lines + 1] = ("周口径：倒计时=%s 来源=%s 判为=%s 下次结束=%s 锚点=%s")
        :format(tostring(weekly), tostring(weeklySrc or "无"),
            tostring(cycleKind or "不采信(与锚点对不上)"),
            tostring(cycleEnd or "nil"), tostring(anchor))
    local _, famMembers = L.Families(db)
    local famOrder = {}
    for key in pairs(famMembers) do famOrder[#famOrder + 1] = key end
    table.sort(famOrder)
    if #famOrder == 0 then
        lines[#lines + 1] = "（账本里没有周常家族：设置里加关键词后点补扫，或等池换轮自动扫）"
    end
    for i = 1, #famOrder do
        local key = famOrder[i]
        local ids = famMembers[key]
        table.sort(ids)
        local named, members = nil, {}
        for j = 1, #ids do
            local rec = db.quests[ids[j]] or {}
            if not named and type(rec.name) == "string" and rec.name ~= "" then
                named = rec.name
            end
            -- "占不占行"与矩阵同一份判据（一族一行，行由族本体占），别在这儿再写一遍：
            -- 板上那条同名子任务不占行，但它仍是这一族本周做过没做的那份证据，
            -- 所以导出里必须看得见（用户 2026-10-24："不显示它（它不是周常）"、
            -- "你反查完成的丰饶为准"）
            members[#members + 1] = ("%s#%s[%s]%s"):format(
                tostring(rec.name or "?"), tostring(ids[j]),
                tostring(rec.mapID or "无图"),
                L.OccupiesRow(db, ids[j]) and "" or "只当证据，不占行")
        end
        local cells = {}
        for c = 1, #chars do
            local st = L.BestRegisteredState(db, chars[c], ids, now)
            cells[#cells + 1] = STATUS_TEXT[st] or tostring(st)
        end
        lines[#lines + 1] = ("%s 族=%s 成员=%s [%s]"):format(
            named or ("任务#" .. tostring(ids[1])), key,
            table.concat(members, " "), table.concat(cells, " | "))
    end

    local undetermined, unknownCells = 0, 0
    for questID in pairs(db.quests) do
        local obs = L.GetObs(db, questID)
        if obs and not obs.gold and obs.money == nil then undetermined = undetermined + 1 end
    end
    for _, charKey in ipairs(chars) do
        for questID in pairs(db.quests) do
            if L.State(db, charKey, questID, nil, now) == "unknown" then
                unknownCells = unknownCells + 1
            end
        end
    end
    lines[#lines + 1] = ("== 降级与未知 == 未判定=%d 未知格=%d 最近降级=%s")
        :format(undetermined, unknownCells, tostring(GQL.Api.LastDegrade()))

    local text = table.concat(lines, "\n")
    GQL.Config.Log("dump 完成")
    if GQL.LogWindow and GQL.LogWindow.Show then GQL.LogWindow.Show(text) end
    -- 全文只进日志窗（窗里 Ctrl+A 全选、Ctrl+C 复制回传）。12.1.5 实测这份导出上百行，
    -- print 进 chat 会被聊天框截断，用户只能一张张贴图 —— chat 只报一句指路 +
    -- 最该先看的两个降级计数。
    print(("GQL: 导出共 %d 行，全文在日志窗（Ctrl+A 全选、Ctrl+C 复制）"
        .. " 未判定=%d 未知格=%d"):format(#lines, undetermined, unknownCells))
    return text
end

GQL.RegisterCommand("dump", Dump.Run, "文本导出账本（阶段一验收用）")

--- /gql quest <名片段|questID>：单行任务"为什么还在列、为什么没倒计时、什么时候自己走"。
--- 在不在池、在不在日志一律现问客户端：账本里的 poolSeen / currentSig 只是历史，
--- 拿它们判"现在还在图上"会把已经结束的轮次当成还开着（12.1.5 实测的怀疑点）。
function Dump.Quest(arg)
    local db = GQL.Config.DB()
    if not (db and db.quests) then print("GQL: 账本未就绪") return end
    arg = (type(arg) == "string" and arg:match("^%s*(.-)%s*$")) or ""
    if arg == "" then
        print("用法：/gql quest <任务名片段或 questID>")
        return
    end
    local L = GQL.Ledger
    local now = (GQL.Api and GQL.Api.WallClock())
        or math.floor((GQL.Api and GQL.Api.Uptime()) or 0)
    local key = tonumber(arg)
    local fmt = GQL.Config.FormatGoldIcon or GQL.Config.FormatGold
    -- 没标签时报的那串字也必须从 Config.MAP_SRC 走（经 Ledger 的对外函数，Task 8 把 probe
    -- 那处改成这个写法）：过去这里手抄过"旧记录没标来源"，账本里的串一改，
    -- 导出的归属/倒计时就会指着一条并不存在的来源解释
    local legacySrc = (L.LegacyMapSource and L.LegacyMapSource()) or "?"

    local pool, poolOK = {}, false
    local okPool, poolRaw = pcall(function() return GQL.Api.GetPool() end)
    if okPool and type(poolRaw) == "table" then
        poolOK = true
        for i = 1, #poolRaw do pool[poolRaw[i].questID] = poolRaw[i] end
    end
    local logged, logOK = {}, false
    local okLog, logRaw = pcall(function() return GQL.Api.AcceptedQuests() end)
    if okLog and type(logRaw) == "table" then
        logOK = true
        for i = 1, #logRaw do logged[logRaw[i].questID] = logRaw[i].name end
    end

    -- "在不在列"只认 MatrixRows 算出来的那份：与界面同源，不会出现诊断说在列、界面却没有。
    -- 注意 MatrixRows 内部还按名字去过重，落选的那份 questID 这里就显示"不在列"，是对的。
    local listed = {}
    local groups = L.MatrixRows(db, now) or {}
    for gi = 1, #groups do
        local rows = groups[gi].rows or {}
        for ri = 1, #rows do listed[rows[ri].questID] = rows[ri] end
    end
    -- 【第 39 轮：这两份只作取证，不再复判任何一道门】"本轮交过"从准入门里整支删了，
    -- 留这两份表是为了说清"那一格的绿勾是从哪一笔来的"（用户 10-29：什么时候也没说做过
    -- 就是准入门槛）。自己数 db.turnins 的报告会在这条判定改口径那天开始说瞎话，
    -- 所以还是问 Ledger 那一次遍历，报告不翻账本。
    local turnedIn = L.TurnedInThisRound and L.TurnedInThisRound(db, now) or {}
    -- 交割那几笔对应的金额（读到金币才是数，读不到留 nil）：只印"到手 X 仅供参考"
    local turnInGains = L.TurnInGainsThisRound and L.TurnInGainsThisRound(db, now) or {}
    -- CD 口径只用来解释"为什么这条不占行了"，不参与判行（判行一律问 Ledger 的轮次口径）
    local cdTurnedIn = L.TurnedInThisCycle and L.TurnedInThisCycle(db) or {}

    -- 手动补录的金币一次收成一张表：准入门里"金币够不够"那一道，矩阵用的是
    -- L.QuestMoney 或这份手动数（Ledger.MatrixRows 的预扫），报告少一份就会误判凶手
    local manualMoney = L.ManualMoneyByQuest and L.ManualMoneyByQuest(db) or {}

    --- 这一行不在矩阵上，到底是哪道门砍的。以前这里写死一句猜话（"没命中关键词、本轮没人交过，
    --- 金币也没到 X"），12.1.5 实测它和紧下面那句"在列靠：本轮有角色交过"直接打架，
    --- 而真正的凶手（到期门）一个字没提 —— 玩家照这句去改门槛/加关键词当然没用。
    --- 现在猜话没了：判据一律问 Ledger，报告只复述门的名字。第 39 轮起"本轮交过"不再是
    --- 一道门（准入门只剩玩家点名 / 奖励金币≥门槛两类），所以这里也不许再为它单独分岔。
    --- 分岔顺序照矩阵那一处（屏蔽 → 占行 → 准入 → 到期 → 同名去重），不许自己排：
    --- 到期门在矩阵里排在准入门**之后**，反过来说就是"这条既没过准入、又带着过期值"的行
    --- 会被报错凶手。判据一律问 Ledger（L.RowAdmitted / L.EffectiveExpiry / L.OccupiesRow），
    --- 门槛的数字问 L.MatrixMoneyFloor：Dump 再拼一套的话，准入门一改这里就说瞎话
    --- （第 32 轮漏掉"客户端凭据"那半条就是这个价）。
    local function offListReason(questID, rec, name)
        if rec.ignored then
            return "屏蔽门：玩家在「关键词 · 屏蔽管理」里点过「忽略」（右栏点回来才取消）"
        end
        if not L.OccupiesRow(db, questID) then
            return "一族只占一行：占行的是同族另一条 ID，这一条只当完成证据"
        end
        local money = L.QuestMoney(db, questID, now) or manualMoney[questID]
        local kwHit = (GQL.Config and GQL.Config.KeywordMatch
            and GQL.Config.KeywordMatch(name)) or false
        if not L.RowAdmitted(db, questID, rec, money, kwHit, nil, now) then
            -- 【第 39 轮改口】以前这里有一整岔专门解释"玩家确实交过、为什么还是没进矩阵"
            -- （凶手写成"这一轮没读到过它的奖励金币"），因为"本轮交过"那时是一道准入门。
            -- 现在门只有两类依据，交没交过根本不参与判定 ⇒ 那一岔没有读者了，整岔删除；
            -- 留一句白话把规则说全，别让玩家以为"做过就该在列"（用户 10-29 定稿）。
            return "准入门没过：既不是玩家点名要看的（名字没命中关键词、也不是关键词从日志"
                .. "收进来的或注册周常），奖励金币也没到 " .. fmt(L.MatrixMoneyFloor())
                .. "；做过/交过不作准入门槛"
                .. ((kwHit and not L.QuestHasCredential(db, questID, now))
                    -- 第 32 轮那半条必须单独说：撞了关键词的人最容易以为"我加了关键词就该看见它"
                    and "，而且名字虽然撞了关键词、客户端却一条凭据都答不出" or "")
        end
        if L.InstanceExpired(db, rec, now) then
            return "到期门：这一条实例自己的结束时刻已过，到期即删"
        end
        for qid, r in pairs(listed) do
            if qid ~= questID and r.name == name then
                return ("同名去重落选：矩阵按名字只留证据更强的那条（同名的 %s 在列）")
                    :format(tostring(qid))
            end
        end
        return "这几道门都没拦住它，原因问不出（把这份报告整段回传）"
    end

    local ids = {}
    for questID in pairs(db.quests) do ids[#ids + 1] = questID end
    table.sort(ids)
    local lines, found = {}, 0
    for i = 1, #ids do
        local questID = ids[i]
        local name = L.QuestName(db, questID)
        if questID == key or name:find(arg, 1, true) then
            found = found + 1
            local rec = db.quests[questID]
            local row = listed[questID]
            -- 池问不出时保持 nil（"判不了"），别写成 and/or 表达式：
            -- `poolOK and pool[q] ~= nil or nil` 在"不在池里"时会塌成 nil，判不了和不在池混为一谈
            local inPool
            if poolOK then inPool = pool[questID] ~= nil end
            local mid = rec.mapID
            local mapText = "未知"
            if mid and mid > 0 then
                local mn = GQL.Api.MapName and GQL.Api.MapName(mid)
                mapText = (type(mn) == "string" and mn or "名字问不出") .. "(" .. mid .. ")"
            end
            lines[#lines + 1] = ("【%s】questID=%s  地图=%s"):format(
                name, tostring(questID), mapText)
            -- 不在列的原因必须分岔回话：上一版这里是一句写死的猜话，12.1.5 实测它和紧下面
            -- 那句"在列靠：本轮有角色交过"自相矛盾，而真砍掉这一行的到期门一个字没提。
            lines[#lines + 1] = ("  矩阵：%s"):format(row and "在列"
                or ("不在列（" .. offListReason(questID, rec, name) .. "）"))
            -- 与渲染层对账（用户 2026-10-31 的"暗影汇聚之地"：/gql quest 报在列、
            -- 矩阵里却整组没有）：MatrixRows 说"在列"≠ 界面一定画得出来 —— 矩阵渲染
            -- 还有一道"关注地图过滤"（Matrix.Render 的 VisibleRows）：分组图不在
            -- 关注集合（玩家自己加的图 ∪ 自动记住的子图）、且这一行不是玩家点名要的
            -- （row.watched）时整组不画。判据照 VisibleRows 同一份
            -- （IsTrackedMap = false 才滤），报告不再只说账本那一半。
            if row and GQL.Api and GQL.Api.IsTrackedMap
                and GQL.Api.IsTrackedMap(row.groupMapID or row.mapID) == false
                and not row.watched then
                lines[#lines + 1] = "  界面过滤：分组图不在你添加的关注地图里（含其子图），渲染层整组不画"
                    .. "（这一行不是关键词/日志/家族行，没有豁免——这是本报告与矩阵唯一的分歧处）"
            end
            lines[#lines + 1] = ("  现在：%s  当前角色日志：%s"):format(
                poolOK and (inPool and "在池里"
                    or ("不在池里（刚问到的 " .. #poolRaw .. " 条里没有它）")) or "池问不出，判不了",
                logOK and (logged[questID] and "已接" or "没接") or "日志问不出")
            local why = {}
            -- 准入门逐条报（判据全在 L.RowAdmitted 那一处，两类依据：玩家点名 / 奖励金币≥门槛）。
            -- 以前这里还报"玩家手动添加"（读 rec.manualAdded）：那个字段 2026-10-27
            -- 整体下线 —— 点"添加"只是撤销屏蔽，不是一条上板凭据，报出来等于谎报
            if rec.kwLogAdded then why[#why + 1] = "关键词从日志收的" end
            -- 布尔位是"当前同步这个号日志里还挂着没"，它被撤掉不等于这条实例没被日志证明
            -- 存在过：编号那一份还在（L.KeywordLogAdmits 唯一读点），报告必须照真判据说清
            -- 是哪一份把行顶住的 —— 拿"关键词从日志收的"糊上去就是在谎报（梦魇任务 94446）。
            -- 编号这里现算一次（取证面每行一次，不在矩阵每帧那条路上），既当判据也印出来。
            local logKey = L.InstanceKey(db, questID, rec, now)
            if not rec.kwLogAdded and L.KeywordLogAdmits(db, questID, rec, now, logKey) then
                why[#why + 1] = "本周有号接进任务日志（当前这个号已经没了，这一条实例 "
                    .. tostring(logKey) .. "）"
            end
            local hit = GQL.Config.KeywordHit and GQL.Config.KeywordHit(name)
            if hit then why[#why + 1] = "命中关键词「" .. hit .. "」" end
            if type(rec.regKey) == "string" then
                -- 措辞按注册前缀分岔（L.IsPinRegKey，用户 2026-11-02 定稿 + T6b）：旧那句对
                -- **任何** regKey 都写"常驻，到期只按 CD 尾巴算"，而金币门从事件 pin 收的那一路
                -- 既非常驻、也不只看 CD 尾巴 —— 报告照那句报，玩家就会以为一条散场的事件行
                -- 本来就该挂在板上到周重置，把"到期不消失"当成设计意图。界面措辞不动，
                -- 取证细节只写在这两个取证入口（本轮不许动 Matrix 的 tooltip）。
                if L.IsPinRegKey and L.IsPinRegKey(rec.regKey) then
                    why[#why + 1] = "金币门从地图事件 pin 收的注册行（非常驻，玩家没点过名）："
                        .. (type(rec.roundEnd) == "number"
                            and "这一条已经烙上自己那条实例的板面结束时刻，到点即到期"
                            or "这一条压根没上过板、问不出自己的结束时刻，按「本周没被看见过 ⇒ "
                               .. "到期」清（最坏到下次周重置）")
                else
                    why[#why + 1] = "注册周常（常驻，到期只按 CD 尾巴算）"
                end
            end
            -- 判"这笔金币是谁读的"只认 Ledger 那一处口径（GoldFromOtherMode），
            -- 报告自己比一遍粘滞标记（老那句 `rec.goldRound == 本轮起点`）的话，
            -- 粘滞规则一改这里就会说瞎话（12.1.5 复盘）
            local other = L.GoldFromOtherMode and L.GoldFromOtherMode(db, questID, now)
            if other and other >= L.MatrixMoneyFloor() then
                -- 报得出名字就报名字：12.1.5 实测 97128 是同 questID 在两个号上奖励不同
                -- （一个 1090 金、一个 150 碎片），"本轮另一档读到的"会被玩家读成"我切了
                -- 狩猎模式"，而真问题是这笔钱归谁 —— 归谁就写谁。
                local who = L.GoldReaders and L.GoldReaders(db, questID, now)
                why[#why + 1] = "金币 " .. fmt(other) .. "（"
                    .. (who and #who > 0
                        and ("本轮读到金币的号：" .. table.concat(who, "、"))
                        or "本轮另一档读到的") .. "，这一档读到不给金币）"
            elseif row and type(row.money) == "number" and row.money >= L.MatrixMoneyFloor() then
                why[#why + 1] = "金币 " .. fmt(row.money)
            end
            -- 【第 39 轮】以前这里还会往"在列靠"里添一句"本轮有角色交过"，因为交过曾是一道
            -- 准入门。判据整支删了 ⇒ 在列靠只报两类依据（玩家点名 / 奖励金币≥门槛）。
            -- 交割那笔仍然要说清（玩家会问"这一格的绿勾是从哪来的"），所以挪到下面单独一行，
            -- 只报事实、不替判据编因果。
            lines[#lines + 1] = "  在列靠：" .. (#why > 0 and table.concat(why, " + ") or "（无）")
            if turnedIn[questID] then
                local gain = turnInGains[questID]
                lines[#lines + 1] = "  交割：本轮有角色交过（只解释那一格的绿勾从哪来，"
                    .. "不作在列依据"
                    .. (type(gain) == "number" and gain > 0
                        and ("；这一条实例读到的奖励金币 " .. fmt(gain) .. " 仅供参考") or "")
                    .. "）"
            end
            -- 本轮没交、本 CD 交过 = 12 小时区换了一条实例，那份凭据立刻匹配不上。
            -- 这一句必须单独说：不说的话，玩家看到的是"我明明做过它，怎么勾没了"。
            -- 【第 39 轮改口】老那句写的是"不占行"，那会儿交过确实占行；现在行归准入门两类
            -- 依据管，这里只影响那一格的勾，别再谎报成"本来能占行"。
            if not turnedIn[questID] and cdTurnedIn[questID] then
                lines[#lines + 1] = "  本 CD 早前那几轮交过：换了实例就不算本轮，那一格的勾也不认它"
            end
            local exp = tonumber(rec.roundEnd)
            -- 到期时刻的"本轮生效"口径与矩阵/悬停/右栏/唤醒定时器同一个真源：这里再自拼一遍
            -- 判定就会出现报告与界面两个答案（第 32 轮右栏的教训）。
            -- roundEnd 只回答"板面自己答没答过这条实例、答的是几点结束"（唯一写点 L.SetExpiry，
            -- 老 expiresAt/expiresSrc 那一组含 24 小时排程值，T5 起整组退役，取证面也不再问来源）。
            local eff = L.EffectiveExpiry(db, rec, now)
            -- 来源这一格问的是"eff 是①档板面值还是②档周 CD 尾巴"：只跟身份真源
            -- L.InstanceEnd 比一次，不自拼判据。账本里压着上一条 CD 的旧结束时刻时
            -- （96718 真档，判据 StalePreviousCycleEnd），身份真源已经换成尾巴、exp 还是
            -- 那张旧纸条 —— 此时照①档写"来源=板面倒计时"就是取证面撒谎。
            local endAt = eff ~= nil and L.InstanceEnd(db, rec, now) or nil
            local fromTail = endAt ~= nil and (type(exp) ~= "number" or endAt ~= exp)
            if eff == nil then
                if type(exp) ~= "number" then
                    lines[#lines + 1] = "  倒计时：账本里没有到期时刻（接口从没读到过它的剩余时间，也没有周 CD 锚点尾巴可借）"
                else
                    -- 账本里有数，但它已经过去（或跨时钟域证明不了还活着）⇒ 是上一条实例的墓碑：
                    -- 做完它的那个号身上 `Api.QuestTimeLeft=nil`、`HaveQuestRewardData=false`，
                    -- `SetExpiry` 问不出就一个字不写（89267 报告里"倒计时：还剩 ?"配着
                    -- "结束时间=已到期（20小时前）"正是这个形状）。污染源要在取证面看得见。
                    lines[#lines + 1] = ("  倒计时：本轮还没读到（账本里那条到期时刻是上一条实例"
                        .. "留下的，%s）"):format(Ago(exp, now))
                end
            elseif fromTail then
                -- ②档：这条自己没有本 CD 的板面倒计时，按周 CD 尾巴（cycleAnchor + 604800）算结束。
                local why = (type(exp) == "number")
                    and "账本里压着的是上一条 CD 的旧结束时刻，不作数"
                    or "它自己没有板面倒计时"
                lines[#lines + 1] = ("  倒计时：还剩 %s（来源=周 CD 尾巴，%s）"):format(
                    GQL.Config.FormatTimeLeft(eff - now), why)
            else
                -- ①档：这条任务自己的板面剩余时间（现在只剩这一种真数来源，编的数已整组退役）。
                lines[#lines + 1] = ("  倒计时：还剩 %s（来源=板面倒计时）"):format(
                    GQL.Config.FormatTimeLeft(eff - now))
            end
            -- 一族只占一行，占行的是族本体（内置表 ids 那条）。板上那条同名子任务
            -- 不占行、只当完成证据 —— 用户 2026-10-24 的口径："不显示它（它不是周常）"。
            -- 这一轮在不在图上另报：它决定"这族本周还活着"，不决定占不占行。
            if rec.regKey then
                local preset = L.PresetFor(db, questID)
                lines[#lines + 1] = ("  家族：%s  本体=%s 占行=%s 这一轮在图上=%s%s"):format(
                    tostring(L.FamilyKey(db, questID) or "无（不是内置族，只是注册周常）"),
                    tostring(preset and (L.FamilyAnchorID(db, preset) or "?") or "无内置族"),
                    L.OccupiesRow(db, questID) and "是" or "否",
                    not not rec.onBoard and "是" or "否",
                    L.OccupiesRow(db, questID) and ""
                        or "（族本体才占行；这一条只当「这一族本周做过没有」的证据）")
            end
            lines[#lines + 1] = ("  地图来源：%s  本 CD 锚点=%s")
                :format(tostring(rec.mapSrc or (rec.mapID and legacySrc or "nil")),
                    tostring(db.cycleAnchor))
            -- 分组头按三层判据（地图事件 pin > 客户端答过的图 > 周常组），与上面那个
            -- "地图来源"可以不是同一张图 —— 用户 2026-10-24："事件在哪 就在哪个地图分组"。
            -- 判据只认 Ledger.RowGroupMapID 一处：导出说祖阿曼、界面在哈籁恩达尔这种分歧最难查。
            local groupMap = L.RowGroupMapID(db, questID)
            local groupLabel
            if groupMap == nil then
                groupLabel = "未知地图"
            elseif groupMap == L.WeeklyGroupMapID() then
                groupLabel = "周常（答不出地图）"
            else
                groupLabel = (GQL.Api and GQL.Api.MapName and GQL.Api.MapName(groupMap))
                    or ("地图 " .. tostring(groupMap))
            end
            lines[#lines + 1] = ("  分组头：%s  归属=%s  口述/账本图=%s")
                :format(groupLabel,
                    (type(rec.evMapID) == "number")
                        and ("地图事件「" .. tostring(rec.evName or rec.evMapID) .. "」")
                        or tostring(rec.mapSrc or legacySrc),
                    MapNameOrId(rec.mapID))
            -- 这一列是 lastSeenAt，池与日志两条路都会写它（SyncKeywordQuestsFromLog 每轮
            -- 给还接在日志里的行续期）。上一版标成"最后在池"，于是 82157 那种压根不在板上
            -- 的任务报出"最后在池=2分前"，把取证方向整个带偏 —— 名字照实写
            -- 结束时间这一格要说清"这个数属于哪一条实例"：本轮不作数的那条（eff 为 nil）
            -- 照实标成上一条实例留下的 —— 取证面看不见污染源，下一份报告就还得靠猜
            -- （89267 那份里"结束时间=已到期（20小时前）"配的是一条还活着的行）。
            local endText
            if type(exp) == "number" and eff == nil then
                endText = "本轮还没读到倒计时（账本里那条 " .. Ago(exp, now)
                    .. " 是上一条实例留下的）"
            elseif type(eff) == "number" then
                -- EffectiveExpiry 只回还没过去的数（板面①/周 CD 尾巴②都从这一行走）
                endText = WhenEnd(eff, now)
            else
                endText = "问不出"
            end
            -- 【第 36 轮 T4b：这一行不再报"本轮起点"】起点那套机器整块删了（计划 §五），
            -- 判据问的是"这笔凭据写下的编号 == 这条任务当前的编号"⇒ 取证要看得见的是**两把键**：
            -- 这一条现在算不算得出身份、以及金币那笔粘滞记在哪一条实例上。
            -- 上一版打一个时刻，谁也没法据此回答"这条证据为什么匹配不上"（94612 那份报告
            -- 里"本轮起点=59分前"配的正是十条压根不属于这一条实例的快照）。
            -- 算不出编号就如实写"身份未定"——那正是界面对这一行报 ? 的原因（fail-closed 那面）。
            lines[#lines + 1] = ("  痕迹：最后见到(池或日志)=%s  这一条的编号=%s  结束时间=%s  "
                .. "当前轮次=%s  金币记在=%s  上次金额=%s  永久屏蔽=%s"):format(
                Ago(rec.lastSeenAt, now),
                L.InstanceKey(db, questID, rec, now) or "身份未定（问不出结束时刻）",
                endText,
                rec.currentSig and "有" or "无",
                rec.goldKey or "（没记）",
                type(rec.lastMoney) == "number" and fmt(rec.lastMoney) or "?",
                rec.ignored and "是" or "否")
            -- "这条到底是不是世界任务"决定它还有没有救：会上板的世界任务（特别任务、
            -- 幽魂角斗士那种）换轮时板面证据自己会把图写回来，口述图只是下板那几天的占位；
            -- 永不上板的城市周常（追踪萨拉塔斯）永远等不到真凭据，只能单独成组。
            -- 两个都得报：IsWorldQuest 是当场问客户端的，poolSeen 只是"这角色同步时见过"的历史。
            -- 必须分开判 nil：`X and A or B` 在 X=false 时会落到 B，"不是世界任务"
            -- 和"判不了"就混成一句话了。
            local wq = GQL.Api.IsWorldQuest and GQL.Api.IsWorldQuest(questID)
            lines[#lines + 1] = ("  是世界任务=%s  上过板=%s"):format(
                wq == nil and "判不了" or (wq and "是" or "否"),
                rec.poolSeen and "是" or "否")
            -- 角色级凭据取证（2026-10-29 回响复燃 92713 那单）：一格的绿/琥珀只可能来自
            -- 三份按角色存的凭据（快照 views / 交割流水 turnins / 补录 manual），上一版
            -- dump 只摊行级痕迹，"这格的绿勾是谁写的"没有据可查。这里逐号摊开三份凭据
            -- 各自记下的编号，并跟当前编号对答案 —— 对上了又答 done 的那份就是出处。
            local curKey = L.InstanceKey(db, questID, rec, now)
            local function KeyHint(k)
                if k == nil then return "" end
                return L.SameInstanceKey(k, curKey) and " ✔对上当前" or "（对不上）"
            end
            local credLines, anyCred = {}, false
            for _, cr in ipairs(L.CredentialsOf and L.CredentialsOf(db, questID) or {}) do
                anyCred = true
                local parts = {}
                if cr.view then
                    parts[#parts + 1] = ("快照=%s k=%s%s"):format(
                        tostring(cr.view.s), tostring(cr.view.k), KeyHint(cr.view.k))
                end
                if cr.lastTurnin then
                    -- 流水那笔的金额字段在账本里叫 `g`（Ledger.OnTurnIn 的写点），
                    -- 上一版这里读 `.money` 于是永远打印 "?"，"到手多少"这一列取证白摊
                    parts[#parts + 1] = ("流水%d笔 末笔t=%s k=%s%s 金额=%s"):format(
                        cr.turninCount, Ago(cr.lastTurnin.t, now), tostring(cr.lastTurnin.k),
                        KeyHint(cr.lastTurnin.k),
                        type(cr.lastTurnin.g) == "number" and fmt(cr.lastTurnin.g) or "?")
                end
                if cr.mark then
                    parts[#parts + 1] = ("补录=%s 核验=%s k=%s%s"):format(
                        tostring(cr.mark.state), cr.mark.v and "是" or "否",
                        tostring(cr.mark.k), KeyHint(cr.mark.k))
                end
                credLines[#credLines + 1] = ("    %s：  %s"):format(cr.charKey,
                    table.concat(parts, "  "))
            end
            if anyCred then
                lines[#lines + 1] = "  凭据（按角色）："
                for c = 1, #credLines do lines[#lines + 1] = credLines[c] end
                lines[#lines + 1] = ("    （当前编号=%s；k 后标 ✔ 的那份对上了它，"
                    .. "状态是 done 的那份就是这一格绿勾的出处）"):format(
                    curKey and tostring(curKey) or "身份未定")
            end
            local verdict
            if not row then
                -- "已经没有了"必须带上是哪道门砍的：不带的话玩家只能拿上一句猜话去改门槛/加关键词
                -- （本轮实测的那条就是这么被他读出"报告1 当前角色刚才已经做完了这个任务"的）
                verdict = "这行界面已经没有了（" .. offListReason(questID, rec, name) .. "）"
            elseif type(eff) == "number" and (eff - now) > 0 then
                verdict = ("有到期时刻，最迟 %s 后自动消失"):format(
                    GQL.Config.FormatTimeLeft(eff - now))
            elseif eff == nil and type(exp) == "number" then
                -- 本轮没有生效的到期时刻 ≠ 账本里没有到期时刻：话说反了就是另一句谎报
                -- （这一行靠"等接口读到新的剩余时间"活着，不是靠"没有任何机制会把它移走"）
                verdict = "本轮没有生效的到期时刻（账本里那条属于上一条实例）→ "
                    .. "等接口读到新的剩余时间，它自己会回到将来"
            elseif inPool == false then
                verdict = "不在池里、账本里又没有到期时刻 → "
                    .. "没有任何机制会把它移走，只能在「关键词 · 屏蔽管理」点「忽略」屏蔽"
            elseif inPool then
                verdict = "还在池里，等接口读到倒计时"
            else
                verdict = "池问不出，判不了"
            end
            lines[#lines + 1] = "  结论：" .. verdict
            -- 取证段：地图/倒计时/CD 各自是谁答的，全摊开。用户回传这一份文本就能定
            -- "日期是谁答错的""地图是谁归错的"，不用再靠猜改代码（12.1.5 复盘）。
            if GQL.Probe and GQL.Probe.QuestEvidence then
                local okEv, ev = pcall(GQL.Probe.QuestEvidence, questID)
                if okEv and type(ev) == "table" then
                    for e = 1, #ev do lines[#lines + 1] = ev[e] end
                else
                    lines[#lines + 1] = "  取证段出错: " .. tostring(ev)
                end
            end
        end
    end
    if found == 0 then
        print("GQL: 账本里没有名字含「" .. GQL.Config.EscapeUI(arg) .. "」的任务（清出账本的老任务也查不到）")
        return
    end
    local text = table.concat(lines, "\n")
    -- 这条只打印、不写日志：/gql dump 全文进日志窗是因为 chat 会截断（chat 只留一句指路），
    -- 而这里 chat 打的就是全文，两条都写只会让 /gql log 塞满重复行 —— 日志留给真错误。
    if GQL.LogWindow and GQL.LogWindow.Show then GQL.LogWindow.Show(text) end
    print(text)
    return text
end

GQL.RegisterCommand("quest", Dump.Quest, "查单行任务为什么还在列/为什么没倒计时")
