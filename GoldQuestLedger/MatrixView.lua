-- 显示层：矩阵"这一次画什么"的唯一出处（0.6 阶段一从 Matrix.Render 里搬出来，判据一字未改）。
--
-- 职责边界，一条不许越：
--   * 输入 = 存档 + 时刻，输出 = 要画的组与行。自己不写存档、不记状态、不排顺序。
--   * 准入门（金币门槛 / 记录 / 到期 / 屏蔽）与**全部排序**（收益序、周常置顶、组内按结束
--     时间的稳定序）都留在 Ledger.MatrixRows 那一处；这里只承接它给出的顺序，绝不重排第二次
--     —— view 再排一次就是第二个真源，每帧重画可能换位。
--   * 为什么要有这一层：0.6 的"隐藏全部完成""角色竖排"都是显示偏好。往准入门加一句
--     等于把玩家界面喜好焊进记账判据（用户 2026-11-08 原话："数据是数据，显示是显示，
--     不能因为隐藏已完成的任务，再把数据搞错了"）。
--
-- 纯函数约定（matrix_view_is_a_pure_function）：同一份 db + 同一个 now 连调两次，输出逐字相同。
-- 本层向客户端问的两件事，都是拆层前就长在 Render 里的，一次都没多：
--   · Api.IsTrackedMap —— 每组每轮一次（估算与绘制共用 Groups 同一份答案，
--     是把 Render 里原有的"两处各问一遍"砍掉，不是加量）。它答的是客户端的跟踪清单，
--     不是本层的记忆，所以不破坏纯函数性。
--   · Api.MapName —— 只在组头真要画这一次才问（GroupLabel 由渲染侧在裁剪之后调，
--     画不到的组连图名都不问，判据同拆层前）。
-- Config.HideAllDone 只是读内存里的配置表，不是接口调用，也不算本层的记忆。
--
-- 0.6 阶段二：两个显示开关落在这一层（本轮做的是"隐藏全部完成"，竖排在 T6）。
-- 判据只读账本算好的 row.allDone，这里不重算状态、不遍历 chars（数据侧一个字不改）；
-- 取证面继续只问 AdmitOnly + 只读谓词 DoneHidden —— 开关改变的是"画不画"，
-- 永远不是"在不在列"（用户 2026-11-08 原话的落点："数据是数据，显示是显示"）。

GQL.MatrixView = {}
local V = GQL.MatrixView

--- 这一组是不是"确定不在跟踪集合里"。IsTrackedMap 是三值，只认 false：
--- nil = 判不出（C_Map 报错 / 父链问不出）按不滤处理，宁多勿漏 —— 接口坏一轮不许
--- 把玩家关注的整组任务凭空吞掉（T13）。`== false` 就是这个意思，别改成 not-truthy。
local function filteredOut(mapID)
    return GQL.Api and GQL.Api.IsTrackedMap
        and GQL.Api.IsTrackedMap(mapID) == false
end

--- 「隐藏全部完成」开关开着吗（0.6 T4 字段，设置页勾选框在 T7）。
--- 带保护的调用形状照 filteredOut 里的 Api 写法：Config 模块不在加载链里
--- （离线单测只拼部分模块）就当关 —— 方向与默认一致，缺什么都不许悄悄改版面。
local function hideAllDone()
    return (GQL.Config and GQL.Config.HideAllDone and GQL.Config.HideAllDone()) == true
end

--- 某个地图组在**关注地图过滤**之后剩下的行（这一句是阶段一从 Render 搬来的原有判据，
--- 一字未改）：不在关注地图集合（玩家自己加的图 ∪ 自动记住的子图）里的整组不列，
--- 但玩家点名要的（命中关键词、日志收的、认成家族成员 = row.watched）必须列出来 ——
--- 否则用户在设置里跟踪了自己的地图、关键词也确实命中，行却还是被这道过滤吞掉
--- （12.1.5 实测反馈"加了关键词没有对应任务"）。
--- 返回 nil = 整组因地图原因不画。
local function mapKeptRows(g)
    if not filteredOut(g.mapID) then return g.rows end
    local keep
    for ri = 1, #g.rows do
        if g.rows[ri].watched then
            keep = keep or {}
            keep[#keep + 1] = g.rows[ri]
        end
    end
    return keep
end

--- 一趟同时给出"要画的行"与"这组是不是被全员完成隐藏掏空的"。
--- 合成一趟而不是 Groups 再问一次 mapKeptRows：那会让每组向客户端多问一遍跟踪清单，
--- 接口调用预算必须与拆层前逐字一致（零新增是用户 10-27 定的硬约束）。
--- 隐藏全部完成（0.6 T5）叠在关注地图过滤**之后**再滤一道：判据只用账本算好的
--- row.allDone（Ledger.MatrixRows 遍历全部角色、done 与 skip 都算终态，Ledger.lua:3078-3098），
--- 这里不重算状态、不遍历 chars。两个过滤器是叠加关系：一条行既在被滤图里又全员完成，
--- 照样不画。开关关着时这一趟逐字等于阶段一那次搬家（原样返回 mapKeptRows 的结果，
--- 一个字节都不多做）。
--- allHiddenDone 只在"地图过滤留下了行、而这些行**全部**被隐藏开关滤掉"时为真 ——
--- 因地图原因本来就整组不画的照旧是 nil 无标记（那种空白是现状，本轮不动，
--- 渲染侧据此决定空态要不要补那句"（都完成了）"，见 Matrix.Render 的分岔注释）。
--- 渲染侧不许再自己数：这一层的输出就是"要画的那份"。契约（用例 hide_all_done_empties_group_
--- without_frame_growth 钉着）：**整组不画一律返回 nil，不许返回空表** —— 渲染侧
--- `if rows and #rows > 0` 与估算侧 `if vr then estBodyH = estBodyH + GROUP_H + ...`
--- 对 nil 和空表的处理不一样，空表会让一个压根不画的组白涨 26px 帧高。
local function visibleEntry(g)
    local kept = mapKeptRows(g)
    if not kept then return nil, false end
    if not hideAllDone() then return kept, false end
    local keep
    for ri = 1, #kept do
        if not kept[ri].allDone then
            keep = keep or {}
            keep[#keep + 1] = kept[ri]
        end
    end
    if keep then return keep, false end
    return nil, #kept > 0
end

--- 这一行本轮画不画。取证面（Dump 的"界面过滤"那一句）只问这一句，不许再抄一份
--- IsTrackedMap 判据 —— 两处各判一次正是"/gql quest 报在列、矩阵里整组没有"这种分歧的温床。
--- 只管**关注地图过滤**这一件事："隐藏全部完成"是另一个原因，走下面的 DoneHidden，
--- 两句在报告里各说各的（用户要的取证措辞：藏的原因分得清，才谈得上"数据是数据"）。
--- 分组键与 MatrixRows 排组时同一个拼法：groupMapID > mapID > -1「未知地图」。
function V.RowHidden(row)
    if not row then return false end
    if not filteredOut(row.groupMapID or row.mapID or -1) then return false end
    return not row.watched
end

--- 只读取证谓词（0.6 T5）：开关开着且这一行 row.allDone 才为真。nil 行恒 false。
--- 它**不参与**画不画的判定（那是 visibleEntry 的事），存在的意义只有一个：
--- 让 Dump/导出能报"这几条是被显示偏好藏了，不是没记账、也不是没过准入门"。
--- 判据只用账本算好的 allDone，这里不重算；AdmitOnly 永远不跟这一句走。
function V.DoneHidden(row)
    if not row or not row.allDone then return false end
    return hideAllDone()
end

--- 组头那句字，**界面**措辞：-2 那批只写"周常"，不写"答不出地图"（那是取证口径，
--- 归 /gql quest；用户 2026-10-24 第二十二轮 + 10-27 定的）。-1 = 从没拿到过地图的任务。
--- 判断本身在 Ledger.GroupKind 一处（四处界面的措辞各不相同，是有意的，不许合并成一句话）。
--- 只在真要画这一组时才调：里面那一次 Api.MapName 是客户端读取，画不到就不问。
function V.GroupLabel(mapID)
    local kind = (GQL.Ledger and GQL.Ledger.GroupKind and GQL.Ledger.GroupKind(mapID)) or "map"
    if kind == "unknown" then return "未知地图" end
    if kind == "weekly" then return "周常" end
    local name = GQL.Api and GQL.Api.MapName and GQL.Api.MapName(mapID)
    return name or ("地图 " .. tostring(mapID))
end

--- 诊断面（Dump / /gql quest / probe）问这一句：只要准入门的答案，**不跟任何显示偏好走**。
--- 它是 MatrixRows 的一层薄壳，存在的意义只有一个：把"这条在不在列"和"这条画不画"
--- 写成两个不同的问题。以后加显示开关，取证面必须继续只调这里。
function V.AdmitOnly(db, now)
    return (GQL.Ledger and GQL.Ledger.MatrixRows(db, now)) or {}
end

--- 横排要画的东西：与 AdmitOnly 的分组一一对应（条数不变 —— 空态那句"没有够格的任务"
--- 判的还是准入门给了几组，与被显示层滤空无关，这一点拆层前后逐字一致），
--- 被整组滤掉的组 rows = nil。顺序照搬数据侧。
--- 组头标签不在这里取：它要问客户端，只有真画到的组才该问，所以由渲染侧调 GroupLabel。
--- allHiddenDone 标记（0.6 T5 新增字段，不影响条数对应）：这一组不是没进准入门、
--- 也不是被关注地图滤掉，而是"全员完成 + 开关开着"被掏空的 —— 渲染侧只在整轮
--- 一条都没画时拿它决定要不要补那句"（都完成了）"（判据出处见 Matrix.Render）。
--- 取证面不许读这个标记：它们的"在不在列"永远只问 AdmitOnly；"界面因这颗开关少画几条"
--- 问 DoneHidden 与 RowHidden 两句（Dump.Run 的计数就是这么拼的，判据仍然只在这一层）。
function V.Groups(db, now)
    local groups = V.AdmitOnly(db, now)
    local out = {}
    for gi = 1, #groups do
        local g = groups[gi]
        local rows, allHiddenDone = visibleEntry(g)
        local entry = { mapID = g.mapID, rows = rows }
        if allHiddenDone then entry.allHiddenDone = true end
        out[gi] = entry
    end
    return out
end
