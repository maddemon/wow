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
-- 本层唯一的外部读取是 Api.IsTrackedMap —— 原来就长在 Render 里，一次都没多（拆层前后
-- 每组各问一次 → 每组各问一次；估算与绘制共用同一份答案，是把原有的重复砍掉，不是加量）。
-- 它答的是客户端的跟踪清单，不是本层的记忆，所以不破坏纯函数性。

GQL.MatrixView = {}
local V = GQL.MatrixView

--- 这一组是不是"确定不在跟踪集合里"。IsTrackedMap 是三值，只认 false：
--- nil = 判不出（C_Map 报错 / 父链问不出）按不滤处理，宁多勿漏 —— 接口坏一轮不许
--- 把玩家关注的整组任务凭空吞掉（T13）。`== false` 就是这个意思，别改成 not-truthy。
local function filteredOut(mapID)
    return GQL.Api and GQL.Api.IsTrackedMap
        and GQL.Api.IsTrackedMap(mapID) == false
end

--- 某个地图组真正要画的行。不在关注地图集合（玩家自己加的图 ∪ 自动记住的子图）里的
--- 整组不列，但玩家点名要的（命中关键词、日志收的、认成家族成员 = row.watched）必须列出来 ——
--- 否则用户在设置里跟踪了自己的地图、关键词也确实命中，行却还是被这道过滤吞掉
--- （12.1.5 实测反馈"加了关键词没有对应任务"）。
--- 返回 nil = 整组不画。**不许返回空表**：渲染侧 `if rows and #rows > 0` 与估算侧
--- `if vr then estBodyH = estBodyH + GROUP_H + ...` 对 nil 和空表的处理不一样，
--- 空表会让一个压根不画的组白涨 26px 帧高（阶段二一开"隐藏全部完成"就踩得到）。
function V.VisibleRows(g)
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

--- 这一行本轮画不画。取证面（Dump 的"界面过滤"那一句）只问这一句，不许再抄一份
--- IsTrackedMap 判据 —— 两处各判一次正是"/gql quest 报在列、矩阵里整组没有"这种分歧的温床。
--- 分组键与 MatrixRows 排组时同一个拼法：groupMapID > mapID > -1「未知地图」。
function V.RowHidden(row)
    if not row then return false end
    if not filteredOut(row.groupMapID or row.mapID or -1) then return false end
    return not row.watched
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
function V.Groups(db, now)
    local groups = V.AdmitOnly(db, now)
    local out = {}
    for gi = 1, #groups do
        local g = groups[gi]
        out[gi] = { mapID = g.mapID, rows = V.VisibleRows(g) }
    end
    return out
end
