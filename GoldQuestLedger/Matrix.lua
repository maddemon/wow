-- 任务×角色矩阵（spec 8.1）+ 打开它的地图按钮。
-- 这个文件只做两件事：把 MatrixView 决定要画的那份版面画成帧、
-- 把 12.x 宿主取不到这件事变成"降级 + 日志"，而不是加载期报错。
-- "这次画哪些组、每组画哪几行、组头写什么"不在这里判 —— 那是显示层的决定，
-- 住在 MatrixView.lua；这里只按它给的结果摆帧、算几何、管滚动。
-- 状态用记号区分：做了打绿勾、玩家手点的打琥珀勾、手记"不做"的灰蓝 ×，
-- 没做留空，本轮结束灰色横线。
--
-- 布局（2026-10 重构，全部来自用户实测反馈）：
--   左窗格（冻结）= 任务名，横向不滚，只跟着上下滚；
--   右窗格 = 角色列，两个方向都能滚。
--   两个窗格各是一个 ScrollFrame（暴雪原声组件，自己裁剪自己滚），
--   纵向偏移共用 offY，横向偏移 offX 只属于右窗格 —— 任务列横滚时钉死不动。
--   表头条（"任务" + 角色名）在两个窗格之上、滚动区之外：纵滚钉住不动，
--   列头横向仍按 offX 平移和列对齐（用户要求"最顶部的表头不要滚动"）。
--   不再显示金额/倒计时列，也不再有角色周统计：剩余时间用任务名的颜色表示
--   （>1天 蓝，>1小时 绿，<1小时 红）；全员完成的任务置灰，到期移除。
--   格线只留横线（用户 2026-10-24："最好没有竖条，合并单元格的效果"）：角色列之间、
--   表头、任务列与角色列的分界全部不画；地图分组行比任务行高（GROUP_H > ROW_H）。
--   0.6 T6 新增第二种排布（设置里的"角色竖排"开关，Config.CharsInRows，默认关）：
--   开关开着时 Mx.Render 分派一句给 Mx.RenderCharsInRows —— 左窗格=角色名（冻结）、
--   右窗格=任务状态格（横滚）、表头两行（上一行分组、下一行任务名）。
--   横排路径除那一句分派外一字未改（计划 docs/tasks/2026-11-08-0.6-matrix-layout.md T6）。

GQL.Matrix = {}
local Mx = GQL.Matrix

-- 紧凑布局（用户实测"紧凑一些，尽量不会有滚动条"）：列宽收窄，名字太长靠
-- 省略号截断（FitLeftText）。行高用户要求"增加一些"，16 → 20。
-- 任务列再收窄一半（用户实测"任务列太宽"）：240 → 120
-- GROUP_H = 地图分组行高（用户 2026-10-24："分组行高稍微高一些，任务行高不变"）。
local NAME_W, COL_W, ROW_H, GROUP_H = 120, 72, 20, 26
local FONT_GROUP = "GameFontNormalLarge"     -- 地图分组行（金色大字）
local FONT_ROW = "GameFontHighlight"         -- 任务名（带描边，深色背板上更跳）
-- 尺寸上限与内边距全收在 Geom 一个表里。maxW/maxH 由 Ensure 按屏幕折算：
-- 宽上限 75%（角色再多也不许比屏幕宽，超出走横滚，用户实测要求）、
-- 高上限 85%（用户要求"最大高度也自适应"：正文放得下就按内容收高、
-- 完全不出滚动条，放不下才在 85% 处停住再开始纵滚）。
-- padV = 标题行 + 上下内边距（不含表头条），取 56 是为了和原来
-- "estRows*ROW_H + 56" 的紧凑尺寸对齐 —— 只是表头条从内容里挪了出来。
-- 注意：表头条自己按几行算**不在这里**，在 Mx.Render 的 `Geom.padV + ROW_H + …`
-- 那句（那里加的一行 = 横排"表头=1 行"的假设，只属于横排）。0.6 T6 竖排是 2 行，
-- 那份 2*ROW_H 假设长在 Mx.RenderCharsInRows 自己的帧高/可视高公式里，不动这里；
-- Geom 里也不开 headRows 这类新字段（upvalue 预算见下面 Head 那段说明）。
-- overscanRows = 可见窗口上下左右各多摆几行（滚动时窗口边缘不许先露白边再补画）。
-- 它放进 Geom 而不是开顶层 local：顶层 local 会变成 Mx.Render 的 upvalue，
-- 那个函数已经贴着 Lua 5.1 的 60 个上限（见 Head 那段说明）。
local Geom = {
    ratioW = 0.75, ratioH = 0.85, padV = 56,
    maxW = 1440, maxH = 720, overscanRows = 3,
}

local frame, scrollL, contentL, scroll, content
-- 固定表头条（用户要求"最顶部的表头不要滚动"）：贴在两个窗格之上、滚动区之外，
-- 纵向滚钉死不动；角色列头挂在 Head.right 上，横向仍按 offX 平移保证和列对齐。
-- 整条状态收进 Head 这一个表，不再开顶层 local —— WoW 的 Lua 5.1 每个闭包最多
-- 60 个 upvalue，超了整份文件加载失败、/gql matrix 和小地图图标一起消失
-- （12.1.5 实测："function has more than 60 upvalues"；lupa 用的 LuaJIT 没这个
-- 上限，所以离线单测永远发现不了，改这文件前先看 tests 里的 upvalue 检查）。
-- btns/btnUsed（0.6 T6）：竖排表头两行的可悬停热区池，只属于 RenderCharsInRows；
-- tipBtn：表头热区"这条提示已经弹过"的缓存（悬停是每帧热路径，见 HoverScan）；
-- title（0.6 T6）：Ensure 里那颗标题串的引用 —— 竖排进场换锚、横排回摆竖排锚都要
-- 拿同一个锚点参照物，不许竖排里重新找（重新找就得改 Ensure，横排建帧代码不许动）。
-- anchorV（0.6 T6）："窗格顶边现在让的是 2 行表头"的记号。竖排进场置真、横排
-- 回摆时清掉：窗格顶边只能由"下一次真要画的那一排"来定，竖排画完就还原会把
-- 自己那第二行表头盖在正文上（表头条的底边本来就是跟着 scroll 顶边锚出来的）。
-- bDiv/hDiv/divUsed（0.6 T8）：竖排组界竖线的两截池子与本轮计数 —— 表头那截挂
-- Head.right、正文那截挂 content，一根线一次摆好两截，所以两池同序号、共用一个计数。
local Head = { pool = {}, btns = {}, btnUsed = 0, bDiv = {}, hDiv = {}, divUsed = 0 }
-- 两个窗格的文字池：字体名 -> { items = {...}, used = n }（见下面 Pool 的说明，
-- 一种字体一条池子，绝不跨字体复用条目）
local texts, textsL, button
-- 行分隔线池（网页表格风）：每行底部一条淡线，组头下一条更亮，扫起来行更清楚
local rowLines, rowLinesL, usedLines = {}, {}, 0
-- 滚动偏移：offY 两个窗格共用（纵向），offX 只属于右窗格（横向）。
-- 滚轮/Shift+滚轮/拖动滚动条都改它们再重绘。
local offX, offY = 0, 0
-- 右窗格内容的自然宽高、两个窗格各自的可视尺寸，由 Render 每轮写进来
local contentW, contentH = 0, 0
local frameW, frameH = 820, 480
local visW, visH = 0, 0
-- 前向声明：下面的热区/滑块/内容区都要挂滚轮处理器、共用夹取函数，
-- 而它们在更后面才定义。Lua 的 local 必须先声明才能被上面的闭包捕获，
-- 直接引用会变成全局 nil（用户实测"滚轮大部分情况无效"就是这个坑：
-- 按钮上的脚本挂了个 nil，只有 ScrollFrame 自己那个好的）。
local ClampOffsets, OnMouseWheel

-- 重绘合并窗口（秒）。0.25 足够把"一次快照带来的多处 Refresh"并成一次排版：
-- 贵的是 MatrixRows 全量重算（每行×每列问状态、金额、名字再排序），不是建帧
-- （帧已经池化，见本文件的取用池）。这是**一次性**定时器，不轮询、面板关了就不存在。
-- 三个状态只被 Mx.Refresh/Mx.RefreshNow 引用，绝不进 Mx.Render —— 后者贴着
-- Lua 5.1 的 60 upvalue 上限（见文件顶部 Head 那段说明），多一个就整文件加载失败。
local COALESCE_SECONDS = 0.25
local paintTimer = nil
local lastPaint = -COALESCE_SECONDS   -- 首次必画

-- SavedInstances 风：状态格用记号区分，一眼扫出还差哪些。
-- 勾走贴图（细的字形看不清，见下面一段），文字记号走 |cff 转义；离线测试只认这些
-- 上屏内容，不读注释。做了打勾（用户实测要求：不用"没做/已做"文字），
-- 没做留空 —— 格子清爽才好扫。
local function Hex(s, c) return "|cff" .. c .. s .. "|r" end

-- 完成勾走**贴图**，不走文字字形（用户 2026-10-30："目前的 √ 有点太细，看不清晰"）：
-- 形状用游戏自带的 ReadyCheck-Ready —— 用户原话是"以前用的游戏自带的"，它比 10 号字
-- 的 √ 粗得多。上一版退回字形是 |T 内联色那套赌输了（顶点色字段位摆错、离线测不出，
-- 琥珀勾按贴图原色上屏），换成真的纹理对象以后 SetVertexColor 一定生效。
-- 两档仍是**同一个形状、两种颜色**（用户 2026-10-29："这两个对勾的样式不统一，
-- 最好统一一下"）：绿勾直接用贴图原色；手点补录先 SetDesaturated 抽成灰度再乘琥珀
-- —— 只有这条路能既保持形状一致、又保证颜色上屏。颜色一对一映射"这一格能不能当凭据"。
local CHECK_TEX = "Interface\\RaidFrame\\ReadyCheck-Ready"
local WAIT_TEX = "Interface\\RaidFrame\\ReadyCheck-Waiting"   -- "在做"，不是做完了
local MARK_SIZE = 16                                          -- 勾的边长（行高 20 放得下）
local MARK_DIM = 0.45                                         -- 全员完成那一行：勾压到这个透明度
local MARK_FONT = "GameFontNormalLarge"                       -- 文字记号大一号（? / — / ×）

-- 文字记号只剩三种：问不出（红 ?）、本轮结束（灰 —）、这一号不做（灰蓝 ×）。
-- 勾与"在做"图标走贴图（见 CellMark）：同一格每轮只会出现其中一种记号。
-- 空串留给"没做"（用户实测要求：不用"没做/已做"文字，格子清爽才好扫）。
local STATE_TEXT = {
    open = "",
    ended = Hex("—", "777777"),
    unknown = Hex("?", "ff8080"),
    -- 右键记下的"这一号本轮不做"（用户 2026-10-30：某号这个任务不给金币、也不打算做）。
    -- 灰蓝而不是红：不做是玩家的选择，不是报警。
    skip = Hex("×", "8fa2c0"),
}
local STATE_FALLBACK = STATE_TEXT.unknown

-- 剩余时间 → 任务名颜色（用户实测要求：不用倒计时文字，用颜色表示）
--   >1 天 蓝；>1 小时且 <24 小时 绿；<1 小时 红；读不到时间 浅奶白
-- 颜色都挑偏高饱和的鲜亮值：深色背板上"看清楚"比"柔和"重要
local COLOR_FRESH = "33a1ff"      -- 蓝：还有一整天
local COLOR_SOON = "37e83a"       -- 绿：一天以内
local COLOR_URGENT = "ff4d4d"     -- 红：一小时以内（鲜红才抓眼）
local COLOR_GRAY = "9a9a9a"       -- 灰：全员完成（置灰禁用态）
local COLOR_PLAIN = "f2ead2"      -- 浅奶白：读不到剩余时间（纯白在亮背板会糊）
local function TimeColor(remaining)
    if type(remaining) ~= "number" then return COLOR_PLAIN end
    if remaining > 86400 then return COLOR_FRESH end
    if remaining > 3600 then return COLOR_SOON end
    if remaining > 0 then return COLOR_URGENT end
    return COLOR_PLAIN
end

--- 角色名按职业上色（用户实测要求）：UnitClass 第二返回值是职业 token
-- （MAGE/PALADIN/...），快照时已存进账本；颜色表是暴雪全局 RAID_CLASS_COLORS。
-- 注意该表没有 .colorHex 字段（只有 .r/.g/.b 和 .colorStr），之前误用 .colorHex
-- 一直返回 nil → 全白，这才是"角色名无法着色"的真凶。取不到就退白色，绝不编色。
local function ClassColor(classToken)
    local cc = RAID_CLASS_COLORS
    if cc and classToken and cc[classToken] then
        local c = cc[classToken]
        if c.colorStr then
            return c.colorStr:sub(3)          -- "ffRRGGBB" 去 alpha
        end
        if c.r and c.g and c.b then
            return ("%02x%02x%02x"):format(
                c.r * 255 + 0.5, c.g * 255 + 0.5, c.b * 255 + 0.5)
        end
    end
    return "ffffff"
end

--- 角色列头只显示角色名，不带服务器（用户实测要求："角色不要显示服务器名称"）。
--- 账本里的 key 仍是"名字-服务器"（保证跨服不撞），这里只切展示。
local function ShortCharKey(charKey)
    local s = tostring(charKey)
    local name = s:match("^[^-]+")
    return (name and name ~= "") and name or s
end

--- 角色名最多取前 n 个字（默认 4），超出直接截掉、不加省略号（用户要求：
--- "角色名称最多取4个字，更多了不加..."）。按 UTF-8 首字节宽度从前往后数字，
--- 数满 n 个字就从字边界截断——不依赖 utf8 库（替身环境没有），两端行为一致。
local function ShortName(name, n)
    n = n or 4
    if type(name) ~= "string" or name == "" then return tostring(name) end
    local bytes, chars, pos = #name, 0, 1
    while pos <= bytes do
        local b = name:byte(pos)
        local width = 1
        if b >= 0xF0 then width = 4
        elseif b >= 0xE0 then width = 3
        elseif b >= 0xC0 then width = 2 end
        chars = chars + 1
        if chars > n then
            return name:sub(1, pos - 1)   -- 第 n+1 个字之前截断，不加省略号
        end
        pos = pos + width
    end
    return name
end

--- 一行文字的显示宽度（px）。转义序列不算字符宽：
--- |T路径:高:宽|t 纹理图标 ≈14px，|cAARRGGBB|r 颜色零宽。
--- 不剥掉它们就会把一长串转义码当字符量，左列裁得莫名其妙。
local function DisplayWidth(s)
    local w, icons, i, n = 0, 0, 1, #s
    while i <= n do
        local b = s:byte(i)
        if b == 124 then                       -- '|'：可能是转义序列的开头
            local rest = s:sub(i)
            local tex = rest:match("^|T[^|]-|t")
            local col = rest:match("^|c%x%x%x%x%x%x%x%x|r")
            if tex then
                icons = icons + 1
                i = i + #tex
            elseif col then
                i = i + #col                   -- 颜色转义不占宽
            else
                w = w + 6
                i = i + 1
            end
        else
            local clen = (b >= 0xF0) and 4 or (b >= 0xE0) and 3 or (b >= 0xC0) and 2 or 1
            w = w + (clen == 1 and 6 or 12)    -- ASCII ≈6px，中文 ≈12px（10 号字）
            i = i + clen
        end
    end
    return w + icons * 14
end

--- 左列文字裁到指定宽度以内。任务名经常超过列宽，超宽会盖住右侧角色列 ——
--- 裁掉并补省略号，列永远对齐。裁在转义序列边界上，绝不把 |T...|t 切成两半。
local function FitLeftText(s, maxW)
    if DisplayWidth(s) <= maxW - 8 then return s end
    local w, i, n = 0, 1, #s
    while i <= n do
        local b = s:byte(i)
        local piece
        if b == 124 then
            local rest = s:sub(i)
            piece = rest:match("^|T[^|]-|t") or rest:match("^|c%x%x%x%x%x%x%x%x|r")
        end
        if piece then
            w = w + (piece:sub(2, 2) == "T") and 14 or 0
            i = i + #piece
        else
            local clen = (b >= 0xF0) and 4 or (b >= 0xE0) and 3 or (b >= 0xC0) and 2 or 1
            w = w + (clen == 1 and 6 or 12)
            i = i + clen
        end
        if w > maxW - 8 then
            return s:sub(1, i - 1) .. "…"
        end
    end
    return s
end

--- 点格子手动标记。左键 = 切换"完成"，右键 = 切换"这一号本轮不做"
--- （用户 2026-10-30：某个号这条任务不给金币、玩家也不打算做，需要一个和
--- "没做""做完了"都分得开的记号）。
--- 接口查不到某个角色的这个任务时（unknown/ended），玩家自己知道做没做 ——
--- 点一下记 done，再点一下取消、回到自动判定。
--- 手动状态在 Ledger.State 里优先级最高（只在**本轮之内**，见那处），重画后立刻生效。
--- "取消"这一支问的是**有没有点过**（L.HasManualMark），不是当前状态：换轮以后
--- 状态不再是 done，拿状态当凭据的话玩家再也撤不掉自己点过的那一笔。
--- 记 done 时把任务已知的金币一起写上：玩家确实做了这个任务，周统计里
--- "完成 1 金币 0" 是错的（那表示白干）；金额没读到才留空，显示 ?。
local function CellClick(charKey, questID, button)
    local db = GQL.Config.DB()
    if not db or not GQL.Ledger then return end
    -- 这一格有没有被手点过（手动补录记录存在）：决定点下去是"取消"还是"记一笔"。
    -- 自动判定出的完成不在这里参与（取消只清手动记录，见 SetManual 的 open 分支），
    -- 跟设计 §6-C「换轮作废」口径一致：只能取消自己手点的那笔。没做的任务若仍显绿，
    -- 那是 L.State 的轮次边界把上一轮完成误判成本轮，得修 L.State，不是在这里覆盖。
    -- 这一次点击共用一个时钟读数（下面每一处判据都要问"这条实例现在算不算得出编号"）：
    -- Ledger 那几处在没 now 时会自己兜一次本地时钟，一次点击问五遍纯属浪费。
    local now = Mx.ClickClock()
    -- 这一格当前状态只问一次（右键那支的"已完成就别记不做"、左键那支的"点过没有"共用）
    local cur = GQL.Ledger.State and GQL.Ledger.State(db, charKey, questID, nil, now) or nil
    -- 右键：记"这一号本轮不做" / 撤销。已完成的格子不许被压掉 —— done 是客户端或流水
    -- 认的凭据，拿一个"不做"盖上去等于让周统计少算一笔，也让玩家看不出自己做过。
    if button == "RightButton" then
        if cur == "done" then
            GQL.Config.Log(("不做：这一格已完成，不改 quest=%s 角色=%s")
                :format(tostring(questID), tostring(charKey)))
            return
        end
        if cur == "skip" then
            GQL.Ledger.SetManual(db, charKey, questID, "open", now, nil)
            GQL.Config.Log(("取消不做 quest=%s 角色=%s")
                :format(tostring(questID), tostring(charKey)))
        else
            GQL.Ledger.SetManual(db, charKey, questID, "skip", now, nil)
            GQL.Config.Log(("记不做 quest=%s 角色=%s")
                :format(tostring(questID), tostring(charKey)))
        end
        Mx.Render()
        return
    end
    -- 手记"不做"的那一格不算"点过完成"：左键压下去要直接记完成（覆盖掉那条 skip），
    -- 而不是先撤掉 skip 再让玩家点第二下。
    -- marked 只认"这一格被手点过"（手动补录记录存在）——见本函数开头注释 §6-C。
    -- 不能把 State=="done" 也算进来：自 2026-11-13 起，离池但本轮有实测流水的世界任务
    -- State 也返回 done（真实完成，持久勾），那不是玩家手点的，点一下不该被当成"取消"。
    -- cur~=skip 必须留：skip 格左键要"直接记完成、覆盖掉那条 skip"（下面 else 分支），
    -- 而不是当成"点过"去走取消——否则记不成 done，测试步骤④与用户预期都坏掉。
    local marked = (cur ~= "skip")
        and GQL.Ledger.HasManualMark
        and GQL.Ledger.HasManualMark(db, charKey, questID)
    if marked then
        GQL.Ledger.SetManual(db, charKey, questID, "open", now, nil)
        GQL.Config.Log(("手动取消 quest=%s 角色=%s"):format(tostring(questID), tostring(charKey)))
    else
        -- 到手金额按**这一号自己读到的**记（12.1.5 实测 97128：Mad 给 1090 金、
        -- 另一个号给 150 碎片）。行金额 QuestMoney 是"最后一次同步的那个号"看到的那一副，
        -- 拿它写进这一号的补录，等于给一个白干的号记上一笔 1090 金 ——
        -- 那一列的周汇总从此虚高，玩家看到的正是"到手 1070 金"那种对不上的数。
        -- 这一号本轮答了 0（客户端明说这副奖励不给金币）就照记 0：白干也是真答案。
        local own = GQL.Ledger.CharRewardMoney
            and GQL.Ledger.CharRewardMoney(db, charKey, questID, now)
        local money = own
        if money == nil then
            -- 这一号没答过：账本里**有别人**答过就留空（显示 ?），那笔钱不是它的；
            -- 一个都没答过（老存档、或压根不上板的注册周常）才退回行金额，
            -- 否则单号用户点一下补录就全变成"完成 1 金币 ?"（那是另一条已修的问题）。
            -- 写成 `A and nil or B` 是 Lua 的经典坑：`and nil` 恒假，B 永远会漏进来
            -- （离线用例抓出来的），必须显式分岔。
            local rd = GQL.Ledger.GoldReaders and GQL.Ledger.GoldReaders(db, questID, now)
            if not rd or #rd == 0 then money = GQL.Ledger.QuestMoney(db, questID, now) end
        end
        -- 手动补录：客户端接口答 done 且这一笔完成落在"这一条任务本轮"里才升绿色（接口核验），
        -- 否则记成玩家自己手点的琥珀勾。绿=客户端/流水认的、琥珀=我自己记的。
        -- 光"客户端答 done"不够：固定 ID 周常 / 历史完成 flag 残留会让上一轮的 done
        -- 把本轮手点的一笔顶成绿色（用户 2026-10-27），所以升绿色必须过 L.CompletedThisRound 本轮门。
        -- 别的号的格子问不了接口（客户端只答当前角色），只能记成琥珀勾。
        local verified
        if GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Api and GQL.Api.QuestState
            and charKey == GQL.Sync.CurrentCharKey() then
            local okCall, ans = pcall(GQL.Api.QuestState, questID)
            local thisRound = GQL.Ledger.CompletedThisRound
                and GQL.Ledger.CompletedThisRound(db, charKey, questID, now)
            -- 客户端答 done 还不算：那一笔得落在"这一条任务本轮"里才认作接口核验（绿色）。
            -- 固定 ID 周常 / 历史完成 flag 残留会让上一轮的 done 把本轮手动点的一笔顶成绿色，
            -- 琥珀色手动标记就出不来（用户 2026-10-27）。只有真有本轮凭据时才升绿色。
            if okCall and ans == "done" and thisRound then
                verified = "接口核验"
            end
            GQL.Config.Log(("核验 quest=%s 角色=%s 接口答=%s 本轮完成=%s"):format(
                tostring(questID), tostring(charKey),
                okCall and tostring(ans) or ("抛错:" .. tostring(ans)),
                tostring(thisRound)))
        end
        GQL.Ledger.SetManual(db, charKey, questID, "done", now, money, verified)
        GQL.Config.Log(("%s quest=%s 角色=%s 金币=%s")
            :format(verified and "接口核验完成" or "手动记完成",
                tostring(questID), tostring(charKey),
                GQL.Config.FormatGold(money)))
    end
    Mx.Render()
end

--- 状态格点击热区池（右窗格）：和文本池一样复用，只改挂点和点击目标。
--- 挂在右窗格内容区上，跟着内容一起平移（ScrollFrame 负责裁剪）。
--- __rowY 记着行坐标，悬停扫描靠它定位高亮。
--- 左键/右键都注册：左键切换完成、右键切换"不做"（见 CellClick）。左窗格那排整行
--- 热区不在此列 —— 矩阵里除了这两档手动标记不再有任何"屏蔽/忽略"入口。
local cellBtns, usedBtns = {}, 0
local function CellButton(x, y, charKey, questID)
    usedBtns = usedBtns + 1
    local b = cellBtns[usedBtns]
    if not b then
        b = CreateFrame("Button", nil, content)
        b:EnableMouse(true)
        pcall(b.RegisterForClicks, b, "LeftButtonUp", "RightButtonUp")
        cellBtns[usedBtns] = b
    end
    b:ClearAllPoints()
    b:SetPoint("TOPLEFT", content, "TOPLEFT", x, -y)
    b:SetSize(COL_W, ROW_H)
    b.__rowY = y
    b.__questID = questID               -- 悬停提示要用：这格是哪个任务的
    -- 点击闭包只在目标变了的时候重建：纯滚动时池子里每格对应的行列不变，
    -- 原来每滚一格要新建几百个闭包（几百行 × 角色列数），GC 一直被踩 —— 连滚就卡。
    if b.__charKey ~= charKey then
        b.__charKey = charKey
        b:SetScript("OnClick", function(self, button)
            CellClick(self.__charKey, self.__questID, button)
        end)
    end
    -- 滚轮透传：按钮盖在内容区上，滚轮事件打在按钮上就哑了
    -- （用户实测"大部分情况滚轮无效"——只有滚到没按钮的缝隙才触发）
    b:SetScript("OnMouseWheel", OnMouseWheel)
    b:Show()
    return b
end

--- 格子里的贴图记号：完成勾 / "在做"图标。文字字形（√）在 10 号字下太细
--- （用户 2026-10-30："有点太细，看不清晰"），换成游戏自带的勾之后粗细跟客户端一致。
--- 贴图挂死在热区上（热区本身就是池，一格里只有一枚记号），每轮只改图/颜色/可见性。
--- 手点补录那条：先抽成灰度再乘琥珀 —— 直接给绿勾染琥珀是乘算，绿 × 琥珀只会更绿。
--- dim = 这一行已经全员完成（名字置灰）：勾跟着压暗（用户 2026-10-30），
--- 一整排鲜亮的勾压在灰名字上反而抢视线 —— 做完了的行不该是最扎眼的那一排。
local function CellMark(btn, st, manual, dim)
    local tex = btn.__mark
    if not tex then
        tex = btn:CreateTexture(nil, "ARTWORK")
        tex:SetSize(MARK_SIZE, MARK_SIZE)
        tex:SetPoint("CENTER", btn, "CENTER", 0, 0)
        btn.__mark = tex
    end
    tex:SetAlpha(dim and MARK_DIM or 1)
    if st == "done" then
        tex:SetTexture(CHECK_TEX)
        if manual then
            tex:SetDesaturated(true)
            tex:SetVertexColor(1, 0.76, 0.10)
        else
            tex:SetDesaturated(false)
            tex:SetVertexColor(1, 1, 1)
        end
        tex:Show()
    elseif st == "progress" then
        tex:SetTexture(WAIT_TEX)
        tex:SetDesaturated(false)
        tex:SetVertexColor(1, 1, 1)
        tex:Show()
    else
        -- ? / — / × / 空 都走文字池，贴图这一枚必须收起来：复用旧条目时
        -- 只改文字不藏贴图，上一行那枚勾会留在空着的格子上
        tex:Hide()
    end
end

--- 整行悬停热区池（左窗格）：一行一条，盖满任务列宽度。横排传 row（那一行是哪个任务），
--- 竖排左窗格传 nil（那一行是角色，不属于任何任务）。
--- 横排那一格注册**右键**：整条任务本轮所有角色都不做（见 Mx.RowBatchClick）。
--- 左键不注册 —— 这一格没有角色归属，格子那两档手动标记搬过来就是谎报。
--- __row 存渲染时那一份行：点击与悬停就地读它，零重算（池子里复用，每轮排版都重写）。
--- 批量"本轮不做"（2026-11-10）：入口是任务名那一格的右键，实现全挂在 Mx 模块表上
--- （见下面 FormatRemaining 之后的那一节）。这里**不新增文件级 local** —— 本文件的
--- Mx.Render 正贴着 60 upvalue / 主块 200 local 两道天花板，多一个就整文件加载失败。
local rowBtns, usedRowBtns = {}, 0
local function RowHoverButton(y, row)
    usedRowBtns = usedRowBtns + 1
    local b = rowBtns[usedRowBtns]
    if not b then
        b = CreateFrame("Button", nil, contentL)
        b:EnableMouse(true)
        b:SetScript("OnMouseWheel", OnMouseWheel)
        pcall(b.RegisterForClicks, b, "RightButtonUp")
        b:SetScript("OnClick", function(self, button)
            if button == "RightButton" then Mx.RowBatchClick(self.__row) end
        end)
        rowBtns[usedRowBtns] = b
    end
    b:ClearAllPoints()
    b:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, -y)
    b:SetSize(NAME_W, ROW_H)
    b.__rowY = y
    b.__questID = row and row.questID or nil   -- 悬停提示与金额那一路认行靠它（老口径不动）
    b.__row = row                              -- 批量那一枪就地读这份
    b:Show()
    return b
end

--- 悬停高亮（左右各一条，同 y 拼起来就是整行）：鼠标在哪一行一目了然
-- （用户实测"看不出来"、"依然没有 current line highlight"）。
--- 不依赖 OnEnter/OnLeave：那两个脚本的触发顺序在客户端里不可靠
-- （Leave 可能比下一个 Enter 还晚，高亮会被误藏），改用 frame 的 OnUpdate
--- 每帧扫一遍 IsMouseOver，扫到谁就亮谁 —— 事件顺序再乱也不怕。
local hiL, hiR, hiRow
local function EnsureHiFrames()
    if hiL and hiR then return end
    hiL = CreateFrame("Frame", "GQLMatrixHiL", contentL)
    hiR = CreateFrame("Frame", "GQLMatrixHiR", content)
    for _, h in ipairs({ hiL, hiR }) do
        pcall(function()
            local tex = h:CreateTexture(nil, "BACKGROUND")
            -- 暖色 22%：8% 白在深色底上几乎看不见（用户实测"没有 hover 效果"）
            tex:SetColorTexture(1, 0.85, 0.55, 0.22)
            tex:SetAllPoints(h)
        end)
    end
end

-- 前置声明：ShowMoneyTooltip 里要调用 FormatRemaining/HexToRGB，但这两个函数定义在它后面。
-- 不前置声明的话，Lua 会把它们当成全局（nil），运行到"剩余时间"那行就报错，
-- pcall 吞掉后 GameTooltip 永远不 Show（悬停提示一直不弹）。
local FormatRemaining, HexToRGB

--- 悬停提示：鼠标停在任务行/状态格上时显示这个任务的金币（用户要求）。
--- 全程守卫 + pcall：GameTooltip 只在真客户端有，替身环境没有也不许炸。
--- 守卫不是装饰（T12）：下面那个 pcall 是**静默**的（返回值就地丢弃，不写日志），
--- 少守卫的裸点一缺模块就整段炸成"悬停没反应"，/gql log 里还一个字都没有。
--- 同函数只此一套口径：所有跨模块调用点全部 `模块 and 模块.函数 and 模块.函数(...)`，
--- 问不出的那一路按"宁缺不编"少画一行（同 585 完成行缺数不写的先例），不许冒裸调用。
--- moneyTipOn 记下"这条提示是我们弹的"：压在矩阵上的窗口自己也可能在用
--- GameTooltip（比如它正显示一个背包物品），无条件 Hide 会连人家的提示一起吞掉。
local moneyTipOn

local function ShowMoneyTooltip(anchor, questID, charKey)
    if not (GameTooltip and anchor and questID) then return end
    pcall(function()
        local db = (GQL.Config and GQL.Config.DB and GQL.Config.DB()) or {}
        local q = db.quests and db.quests[questID]
        -- 这一次悬停共用一个时钟读数：金额、到期、格子状态三处都要问"这一条实例现在
        -- 算不算得出编号"，Ledger 那边没 now 会各自兜一次本地时钟（悬停是热路径，别多问）。
        local now = GQL.Api and GQL.Api.WallClock and GQL.Api.WallClock()
        local money = (GQL.Ledger and GQL.Ledger.QuestMoney)
            and GQL.Ledger.QuestMoney(db, questID, now) or nil
        -- 金额格式只在这里问一次：下面金币那行和"某号 交于 … 到手 X"要用同一个口径，
        -- 两处各写一份的话界面会出现同一条钱两个样子
        local fmt = GQL.Config and (GQL.Config.FormatGoldIcon or GQL.Config.FormatGold)
        GameTooltip:SetOwner(anchor, "ANCHOR_RIGHT")
        GameTooltip:ClearLines()
        -- ID 直接跟在任务名后面、右侧那列上灰色（用户 2026-10-25："任务 id 直接显示在
        -- 任务名称后面，不要标签"；"改成灰色的，和最下面那一行一个颜色"）。
        -- 用 AddDoubleLine 而不是把 ID 拼进名字里：暴雪的转义表里没有"字号"这一项
        -- （只有字体对象能改字号），而同一条线的**左右两列各自是一根 FontString**，
        -- 右边那列在提示模板里本来就比标题小 —— 名字保持白，ID 灰，一眼分得开。
        -- ID 这一笔不能省：玩家不可能知道 ID，同名两条 ID 在矩阵里会被去重，
        -- 没有它就拿不到 /gql quest <ID> 的入参。
        local qName = q and q.name
        GameTooltip:AddDoubleLine(qName or ("世界任务#" .. tostring(questID)),
            tostring(questID), 1, 1, 1, 0.6, 0.6, 0.6)
        -- 地图名（用户要求）：这任务是哪张图的，悬停一眼看清
        local mid = q and q.mapID
        local mapName = (mid and mid > 0 and GQL.Api and GQL.Api.MapName)
            and GQL.Api.MapName(mid) or nil
        if mapName then
            -- 一律「区域 <名>」。原来分组与账本图不一致时要加一句"客户端记的图"，
            -- 那是我们按哪一层证据归组的取证口径（用户 2026-10-27："客户端记的图…
            -- 这类说明我们怎么知道的字不像正常软件该展示的"）；分组头本来就写着另一张图，
            -- 分歧的完整解释在 /gql quest 与导出里。
            GameTooltip:AddLine("区域 " .. mapName, 0.7, 0.7, 0.7)
        end
        -- 完成要求（用户 2026-11-11）：只在「任务名单元格」hover 时显示（整行热区，
        -- charKey 为 nil），角色单元格（charKey 有值）不显示 —— 那格的提示讲的是这一号
        -- 的状态/到手，要求属于整条任务。世界任务**没接也问得出**（C_TaskQuest 一路，
        -- 地图上 pin 的 tooltip 就是这份本地说），已接的退任务日志口径。
        -- 只列要求文本不列进度：进度是"当前角色此刻"的活数，缓存必腐，格子上的
        -- 完成口径本来就在说这件事。问不出（没上板又没接）整段不画 —— 宁缺不编，
        -- 同上面地图名的口径。文本在 Api 侧有会话缓存：提示每帧重建，不缓存就是每帧白问。
        if not charKey then
            local objLines = (GQL.Api and GQL.Api.QuestObjectiveLines)
                and GQL.Api.QuestObjectiveLines(questID) or nil
            if objLines then
                for oi = 1, #objLines do
                    GameTooltip:AddLine("- " .. objLines[oi], 0.9, 0.9, 0.9)
                end
            end
            -- 任务耗时（用户 2026-11-13 新功能）：只算在线在册时长，放弃不计入。
            -- 只在任务名单元格 hover（整行热区，charKey 为 nil）显示，讲整条任务，不分角色。
            if GQL.Duration then
                local avg, n = GQL.Duration.Average(db, questID)
                if type(avg) == "number" and n > 0 then
                    local span = GQL.Duration.FormatSpan(avg)
                    if span then
                        GameTooltip:AddLine(("平均耗时 %s（%d次）"):format(span, n),
                            0.8, 0.9, 0.6)
                    end
                end
                local last = GQL.Duration.LastDurAll(db, questID)
                local lastSpan = type(last) == "number" and GQL.Duration.FormatSpan(last)
                if lastSpan then
                    GameTooltip:AddLine("最近一次 " .. lastSpan, 0.8, 0.9, 0.6)
                end
            end
        end
        -- "这一轮在哪办 <名>（地图事件：<pin>）"与周常组那句"这台客户端问不出它的地图"
        -- 整句从界面删了（同上一条口径）：行已经分到具体地图上，玩家不需要知道这份归属
        -- 是问谁要来的、也不许在这露出 pin 的倒计时（跨图问整轮回 nil，留旧数就是谎报）。
        -- 取证落点本来就有：Probe 的事件段与 Dump 都打「地图事件「X」」，
        -- 周常那批的组头「周常」两个字就是说清这一组是什么的那一句。
        -- 倒计时（用户要求）：任务还剩多久结束。真源只问 Ledger.EffectiveExpiry（不是账本里
        -- 自拼的第二份到期账），它给的数只有两个来源 —— ①板面答得出自己的倒计时
        -- （rec.roundEnd）；②答不出、按周 CD 尾巴算的结束时间（cycleAnchor + 604800）。
        -- 老"排程值"（为了让这行别立刻过期而编的 24 小时）随 T5 整组退役：编的数报出来
        -- 就是谎报（12.1.5 实测反馈："剩余 22时58分"配着绿名，可那任务根本没倒计时），
        -- 从此这条路上没有猜的数。
        -- 过去值也不从这条路漏出去：账本里比现在还早的一条属于上一条实例，
        -- 这一行现在活着（矩阵已经放行），拿它算倒计时会印出"剩余 已到期"——
        -- 本轮要消灭的那句谎报就在这（用户 2026-10-27 的 89267）。nil 时整行不显示，
        -- FormatRemaining(nil) 本来就回 nil，下面那道 `if remStr` 会跳过。
        -- now 在函数开头取的那一份，这里直接用（同一次悬停不多问一次时钟）。
        local exp = (GQL.Ledger and GQL.Ledger.EffectiveExpiry)
            and GQL.Ledger.EffectiveExpiry(db, q, now) or nil
        local remStr = FormatRemaining((exp and type(exp) == "number"
            and type(now) == "number") and (exp - now) or nil)
        if remStr then
            local r, g, b = HexToRGB(TimeColor((exp and type(exp) == "number"
                and type(now) == "number") and (exp - now) or nil))
            -- 话跟着**数**说：第 36 轮 T4a 起这个数是这条实例自己的结束时刻（Ledger.InstanceEnd），
            -- 它只有两个来源 —— ①板面答得出自己的倒计时（rec.roundEnd）= "剩余"；
            -- ②答不出、按周 CD 尾巴算的结束时间 = "距下次周重置"。
            -- 不再拿 rec.expiresSrc 定标签：那个字段记的是"当时兜底问了哪一档重置时刻"，
            -- 跟这里算的数可能根本不同档（12.1.5 实测那句"周五显示距下次周重置 12 时 6 分"，
            -- 数是明早 7 点的每日重置、话是周的 —— 玩家照它安排本周周常就全排错了）。
            -- 尾巴档一旦过期就从 EffectiveExpiry 回 nil，这一行整行不显示，不会再冒认本周的尾巴。
            local label = (q and tonumber(q.roundEnd)) and "剩余 " or "距下次周重置 "
            GameTooltip:AddLine(label .. remStr, r, g, b)
        end
        -- 同族（同一件周事的几条子任务，做过任意一条即算完成）的条数不再上界面
        -- （用户 2026-10-27："不要写 同名任务本周x条，做过任意一条都算，用户不需要知道这些"）。
        -- 判据一个字没动：那一格的完成口径照旧问全族（Ledger.Families / BestRegisteredState），
        -- 上面那行"距下次周重置"就是族本体的寿命；条数在 Probe 的同族段（「同族共 N 条」）。
        -- 金币按号说的那一档只留给真分岔的任务（用户 2026-10-25 的 97128「巢穴：尼姆瑞莎·
        -- 唤波者」：同一个任务号，Mad 读到 1090 金、爱情之力读到 150 宝匣钥匙碎片）。
        -- 【2026-11-04 定稿 §二 6】默认按行：绝大多数任务各号读到的是同一副奖励，
        -- 这一号还没同步过时照旧报行级那份（11-03 一律按号把正常任务也打成了 `?`）。
        -- 只有这条实例上至少两个不同号、一个正数一个 0 时 L.CellRewardMoney 判出分岔，
        -- 没自己读过的号才走下面「金额未知」那支 —— 这时候借谁的钱都是猜。
        -- 停在**整行热区**（charKey 为 nil，没有角色归属）照旧印行金额 —— 那一格不属于谁。
        -- 行金额本身没退役：行不掉、金币门槛（QuestMoney / GoldFromOtherMode）都还靠它。
        -- 显式 if 分岔而不是 `A and own or money`：Lua 里 `and nil or` 恒假（见 :297 那条警告）。
        local own
        if charKey and GQL.Ledger and GQL.Ledger.CellRewardMoney then
            local rec = db.quests and db.quests[questID]
            local curKey = GQL.Ledger.InstanceKey(db, questID, rec, now)
            own = GQL.Ledger.CellRewardMoney(db, charKey, questID, now, curKey)
        end
        local shown
        if charKey then
            shown = own
        else
            shown = money
        end
        if shown ~= nil and shown > 0 then
            -- FormatGoldIcon 是矩阵界面用的金银铜图标格式，比纯文本好看；
            -- 格式函数问不出（缺模块）就少这一行，宁缺不编，也不许冒 fmt 调用
            if fmt then GameTooltip:AddLine(fmt(shown), 1, 0.82, 0) end
            -- 这一号本轮没读到过奖励时**只印上面那个数，不再加一句解释**（用户
            -- 2026-10-25："上面是 xx 读到的，本轮还没读到，这个就没必要显示了"）：
            -- 这种格子本来就是还没同步的号，报名字反而把提示撑长、也提不出任何行动。
            -- 走到这一支的格子只有两种来源：格子上 = 这一号自己的读数，或没分岔时
            -- 借来的行金额（话不用再说归属），整行热区 = 行金额（没有角色归属，
            -- 点谁的名都是谎报）。
        elseif shown == 0 then
            -- 0 是客户端答的"这一副奖励不给金币"（奖励数据就在本地却问不出金额），
            -- 跟"没读到"是两件事，界面必须分开说（用户 2026-10-25：意外遭遇已经不是
            -- 金币任务却还挂在矩阵里，悬停还显示上个 CD 的 1064 金）。
            -- 停在格子上时只说这一号自己的结果，不替整行下结论。"这行为什么还在列"
            -- （别的号本轮读到了金币）不上界面（同上一条口径）：那份归属搬进了
            -- /gql quest 的「金币读者」行。
            -- 颜色用中性灰不是红：这一号本轮没得刷不是损失、也不需要玩家做什么，
            -- 给金币的号又已经交完了 —— 对着物品号催刷等于凭空报警。
            if own == 0 then
                -- 这个 0 有可能是做完之前的旧答案：关键词收进来的任务没做完时读不到钱
                -- （用户 2026-10-29 的特别任务），交完任务就从日志里没了，0 再没机会改口。
                -- 【2026-11-04 的口径】底下"交于…到手 X"那个 X 现在问 L.CellRewardMoney
                -- （默认按行、分岔才按号；这一号自己有读数时先认自己那份），
                -- 所以这号真读到金币时
                -- 走的是上面 `shown > 0` 那一支，压根到不了这里 —— 两处说的是同一份读数，
                -- 0 就照印"这轮做它不给金币"，不再有"上面不给金币、下面到手 2068🪙"
                -- 那种自己打自己（老代码在这借的是**另一个号**的 2068，正是 11-03 截图
                -- 「麦德 … 到手 1089🪙」的同一件事；代价照实：关键词类那笔旧 0 从此
                -- 显成"不给金币"，那是数据源本身的局限，归 /gql quest 的「金币读者」取证）。
                -- 下面这道守卫因此只剩一种真能命中的形状：这一号自己补录过正数金额
                -- （manual.g 按号，LastCompletion 取的是新那份）而读数答过 0 —— 那两句
                -- 才是真打架，这句整行不印。守卫留着不删：缺模块/缺函数时按"没这笔数"
                -- 处理（同本函数 T12 的口径），这句照印，不吞整段提示。
                local done = (GQL.Ledger and GQL.Ledger.State)
                    and GQL.Ledger.State(db, charKey, questID, nil, now) == "done"
                local gain
                if GQL.Ledger and GQL.Ledger.LastCompletion then
                    local _, g = GQL.Ledger.LastCompletion(db, charKey, questID)
                    gain = g
                end
                if not (done and type(gain) == "number" and gain > 0) then
                    GameTooltip:AddLine(ShortCharKey(charKey) .. " 这轮做它不给金币",
                        0.8, 0.8, 0.8)
                end
            else
                GameTooltip:AddLine("这一轮不给金币", 0.8, 0.8, 0.8)
            end
        else
            GameTooltip:AddLine("金额未知", 0.8, 0.8, 0.8)
        end
        -- 这一格被手记成"不做"时点明一句（界面上只是一枚 ×，玩家得知道它是什么意思）。
        -- 只报这一号的、只报这一条实例：判据照旧问 L.State（编号对不上就不算）。
        local cellState = (charKey and GQL.Ledger and GQL.Ledger.State)
            and GQL.Ledger.State(db, charKey, questID, nil, now) or nil
        if cellState == "skip" then
            GameTooltip:AddLine(ShortCharKey(charKey) .. " 本轮不做", 0.62, 0.70, 0.86)
        end
        -- 最底下只留一行：这一列的号本轮交了没有、什么时候交、到手多少。
        -- 停在具体格子上时才报：整行热区没有角色归属，拿某个号的完成时间挂在整行上
        -- 就是谎报。主语取列上的号名，不写"本号"（格子属于列，不属于正在玩的这个号）。
        -- **只报这一条实例的**（用户 2026-10-25 的 89377「秘密狩猎」：板面换轮了、
        -- 这一号还没做，悬停却印着今天上午那次交付）：判据直接问格子的显示口径
        -- L.State，它比的就是这笔凭据写下的编号和这条任务当前的编号（§四 R1）。
        -- 不另问一遍时间，免得界面出现"格子是勾、下面说没交过"这种自己跟自己对不上。
        -- 没数据（没交过 / 只有开机秒域的时间戳）就整行不写，宁缺不编。
        if charKey and GQL.Ledger and GQL.Ledger.LastCompletion
            and GQL.Config and GQL.Config.FormatStamp
            and GQL.Ledger.State(db, charKey, questID, nil, now) == "done" then
            local t, gain = GQL.Ledger.LastCompletion(db, charKey, questID)
            local stamp = t and GQL.Config.FormatStamp(t)
            if stamp then
                local line = ShortCharKey(charKey) .. " 交于 " .. stamp
                -- 完成时间后面那个来源括号（实测/接口核验/手动补录）不再上界面
                -- （同上一条口径：玩家这一眼要的是"什么时候交的、到手多少"，不是凭据种类）。
                -- 源头一个字没动：LastCompletion 的第三个返回值、db.manual[号][任务].v
                -- 的存法与 Ledger.lua 那三个标签串照旧，/gql quest 与导出仍报得出是哪一种。
                -- 「到手 X」只在**正数**时拼：X 自 2026-11-03 起是按号的
                -- （Ledger.LastCompletion 实测分支问的是这一号自己的 reads）——
                -- 0 是客户端答的"这一号做它不给金币"，那句由上面那一支说，印成「到手 0🪙」
                -- 等于把一句话拆成两句还多一个假钱数；nil 是这一号在这条实例上没读过，
                -- 更不能拿别人的钱来填（11-03 截图那句「麦德 … 到手 1089🪙」就是这么来的）。
                if fmt and type(gain) == "number" and gain > 0 then
                    line = line .. " 到手 " .. fmt(gain)
                end
                GameTooltip:AddLine(line, 0.6, 0.6, 0.6)
            end
        end
        -- 两档手动标记怎么点，写在格子的提示最底下（整行热区没有角色归属，不写）：
        -- 右键那一路没有别的入口能发现，不写明就等于只有作者知道。
        if charKey then
            GameTooltip:AddLine("左键 切换完成 · 右键 切换不做", 0.5, 0.5, 0.5)
        elseif anchor and anchor.__row then
            -- 任务名那一格（横排整行热区 / 竖排表头那格）：右键那一枪是整条任务的批量不做。
            -- 就地读渲染时那份 row（Mx.BatchCounts 是纯表查、零接口）—— 为这一句话
            -- 重算一遍版面是不划算的（零新增接口调用那条纪律同一坑位）。
            -- 无事可做的行没有这一句；格子那两句照旧不搬到这一格。
            local mode = Mx.BatchCounts(anchor.__row).mode
            if mode == "set" then
                GameTooltip:AddLine("右键 本任务本轮全号不做", 0.5, 0.5, 0.5)
            elseif mode == "clear" then
                GameTooltip:AddLine("右键 撤销：本任务本轮全号不做", 0.5, 0.5, 0.5)
            end
        end
        GameTooltip:Show()
        moneyTipOn = true
    end)
end

--- 秒数 → 易读倒计时（"X天Y时Z分"）。缺/非数返回 nil，≤0 显示"已到期"。
FormatRemaining = function(sec)
    if type(sec) ~= "number" then return nil end
    if sec <= 0 then return "已到期" end
    local d = math.floor(sec / 86400)
    local h = math.floor((sec % 86400) / 3600)
    local m = math.floor((sec % 3600) / 60)
    if d > 0 then return ("%d天%d时%d分"):format(d, h, m) end
    if h > 0 then return ("%d时%d分"):format(h, m) end
    return ("%d分"):format(m)
end

-- ===================== 批量"本轮不做"（任务名那一格右键）=====================
-- 用户 2026-11-10：整条任务本轮所有角色都不做，可现在只能一个一个格子点。
-- 这一档只是**手势**：写的还是格子右键那同一笔记录（Ledger.SetManual 的 state="skip"，
-- 自带实例编号 k，换轮在读侧自动作废）。不新增存档字段、不新增判据。
-- 全挂 Mx：本文件的主块 local 数量与 Mx.Render 的 upvalue 都顶着上限，加文件级
-- local 会让整个插件加载失败（见文件顶部那两段说明与 test_lua_upvalue_budget）。

--- 这一行的批量方向与影响面：**只认格子上显示的那份状态**（row.state，家族行已由
--- Ledger.BestRegisteredState 折成最强一条）。done 不动；已是 skip 的不重写（一刷时间戳
--- 就把玩家单独点的那笔取证改成了"我批量点的"）；其余（空 / ? / 在做 / 已到期）算可写。
--- 有可写的 ⇒ 这一枪是"记"；没有可写的但还有 × ⇒ 这一枪是"撤"；两样都没有 ⇒ 无事可做。
--- 纯查表，零接口调用（判据表见 test_row_batch_counts_follow_the_displayed_row_state）。
function Mx.BatchCounts(row)
    local counts = { total = 0, done = 0, skip = 0, writable = 0 }
    local state = row and row.state
    if state then
        for _key, shown in pairs(state) do
            counts.total = counts.total + 1
            if shown == "done" then counts.done = counts.done + 1
            elseif shown == "skip" then counts.skip = counts.skip + 1
            else counts.writable = counts.writable + 1 end
        end
    end
    counts.mode = (counts.writable > 0 and "set")
        or (counts.skip > 0 and "clear") or nil
    return counts
end

--- 按任务号在**当前版面**里找那一行：走 MatrixView.Groups（和渲染同一份，含两道显示过滤），
--- 于是批量看到的格子状态与玩家眼前看到的逐字相同。
--- **只有点确认那一步用它**（框停在屏上期间玩家可能 /reload、可能换轮、这行可能消失，
--- 必须重新解析）；点击与悬停都不许用 —— 那两条路要零重算，见 Mx.RowBatchClick 与提示那一句。
function Mx.BatchRowOf(db, now, questID)
    local groups = (GQL.MatrixView and GQL.MatrixView.Groups)
        and GQL.MatrixView.Groups(db, now) or nil
    if not groups then return nil end
    for gi = 1, #groups do
        local rows = groups[gi].rows or {}
        for ri = 1, #rows do
            if rows[ri].questID == questID then return rows[ri] end
        end
    end
    return nil
end

--- 确认框那几句。界面只说玩家这一眼要用的：哪条任务、本轮到几点、动了几个格。
--- · 地图名那一句不做：行名本来就是按名字去重的（Ledger.MatrixRows 那张分数表），
---   而 GroupLabel/MapName 要为这一句问一次客户端 —— 一句话换一次接口，不划算。
--- · 到期只在**墙上钟域**才写：开机秒印成日期就是谎报（EffectiveExpiry 在开机秒域
---   回的是 43200 那种小数字）。问不到整句不写，宁缺不编。
--- · `now` 由调用方传进来：一次点击只取一次读数，文案不许再问一遍时钟。
--- · 措辞过 test_ui_text_no_forensic_words：那十个取证词一个都不许进来。
function Mx.BatchText(row, counts, now)
    local name = row.name or ("世界任务#" .. tostring(row.questID))
    local lines = {}
    if counts.mode == "clear" then
        lines[#lines + 1] = name .. "  撤销“这一条本轮所有角色都不做”？"
    else
        lines[#lines + 1] = name .. "  这一条本轮所有角色都不做？"
    end
    local db = GQL.Config.DB()
    local rec = db and db.quests and db.quests[row.questID]
    local exp = (GQL.Ledger and GQL.Ledger.EffectiveExpiry)
        and GQL.Ledger.EffectiveExpiry(db, rec, now) or nil
    if type(exp) == "number" and exp > 1e9 and GQL.Config.FormatStamp then
        local stamp = GQL.Config.FormatStamp(exp)
        if stamp then lines[#lines + 1] = "本轮到 " .. stamp end
    end
    if counts.mode == "clear" then
        lines[#lines + 1] = ("清掉这一行上 %d 枚 ×（含你以前单独点的），%d 格已完成保持 ✓ 不动")
            :format(counts.skip, counts.done)
    else
        lines[#lines + 1] = ("%d 个号：%d 格记不做，%d 格已完成保持 ✓，%d 格已是 × 不动")
            :format(counts.total, counts.writable, counts.done, counts.skip)
    end
    return table.concat(lines, "\n")
end

--- 一次点击只问一次时钟：墙上钟优先，问不到退回开机秒。
--- 原来这段内联在 CellClick 里，现在格子那一路和整行那一路共用同一个 now 口径。
function Mx.ClickClock()
    local now = GQL.Api and GQL.Api.WallClock() or nil
    if not now and GQL.Api and GQL.Api.Uptime then
        now = math.floor(GQL.Api.Uptime())
    end
    return now
end

--- 确认之后那一批写。判据与方向都在 data 里带过来，但**状态一律重新解析**：
--- 框停在屏上这段时间里玩家可能 /reload、可能换轮、这行可能消失。
--- 三道门任意一道拦住就一个字都不写（fail-closed），并在日志里说清为什么。
function Mx.ApplyBatch(data)
    local db = GQL.Config.DB()
    if not db or not data or not data.questID then return 0 end
    local now = Mx.ClickClock()
    local row = Mx.BatchRowOf(db, now, data.questID)
    if not row then
        GQL.Config.Log(("批量不做：这行已不在矩阵，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    local rec = db.quests and db.quests[data.questID]
    local key = (GQL.Ledger and GQL.Ledger.InstanceKey)
        and GQL.Ledger.InstanceKey(db, data.questID, rec, now) or nil
    if not key then
        GQL.Config.Log(("批量不做：这一条身份未定，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    if key ~= data.k then
        -- 框开着的时候板面换成了另一条实例：这一枪许的是上一条，不许盖到新的上面
        GQL.Config.Log(("批量不做：本轮已换，未改动 quest=%s")
            :format(tostring(data.questID)))
        return 0
    end
    local counts = Mx.BatchCounts(row)
    local written = 0
    for charKey, shown in pairs(row.state or {}) do
        if data.mode == "set" and shown ~= "done" and shown ~= "skip" then
            GQL.Ledger.SetManual(db, charKey, data.questID, "skip", now, nil)
            written = written + 1
        elseif data.mode == "clear" and shown == "skip" then
            GQL.Ledger.SetManual(db, charKey, data.questID, "open", now, nil)
            written = written + 1
        end
    end
    GQL.Config.Log(("批量%s quest=%s 改了 %d 格（共 %d 号，%d 格已完成未动，%s 编号）")
        :format(data.mode == "clear" and "撤销" or "不做", tostring(data.questID),
            written, counts.total, counts.done, tostring(key)))
    Mx.Render()
    return written
end

--- 弹窗条目只建一次（本插件第一处确认框）。两个方向复用同一条目、只换文案与 data。
--- text 固定 "%s"：StaticPopupDialogs[*].text 是**格式串**，真客户端会 format(text, text1, text2)，
--- 任务名里万一有个 % 直接被当格式串解释 = 报错或错字，所以内容一律走 text1。
function Mx.EnsureBatchDialog()
    if type(StaticPopupDialogs) ~= "table" then return false end
    if StaticPopupDialogs["GQL_ROW_BATCH_SKIP"] then return true end
    StaticPopupDialogs["GQL_ROW_BATCH_SKIP"] = {
        text = "%s",
        button1 = "确认",
        button2 = "取消",
        timeout = 0,
        persistent = true,
        hideOnEscape = true,
        OnButton1 = function(_dialog, data) Mx.ApplyBatch(data) end,
        OnButton2 = function(_dialog, data)
            GQL.Config.Log(("批量不做：取消 quest=%s")
                :format(tostring(data and data.questID)))
        end,
    }
    return true
end

--- 弹确认框。两件事要挡：叠框（连点两次不许排两个框、也不许换掉第一个框的文案）、层级。
--- 层级口径照本文件 ActivateMatrix 那两段：DIALOG 只是"打开这一下"的临时抬高，
--- 后开任何窗口都由 YieldToWindow 让回 MEDIUM —— 所以弹之前让回 MEDIUM 就是既有那一招，
--- 让回去之后**不需要还原**（MEDIUM 本来就是矩阵的常驻层）。
--- 静态弹窗在不在矩阵之上**没实测过**，所以把这一笔层号原样写进日志：玩家报"看不见框"时
--- 先看这一行，别凭猜改。
function Mx.ShowBatchConfirm(text, data)
    if not Mx.EnsureBatchDialog() or type(StaticPopup_Show) ~= "function" then
        GQL.Config.Log("批量不做：这台客户端没有确认弹窗，未改动")
        return false
    end
    if type(StaticPopup_Visible) == "function"
        and StaticPopup_Visible("GQL_ROW_BATCH_SKIP") then
        GQL.Config.Log("批量不做：确认框还开着，这次点击忽略")
        return false
    end
    local before = "?"
    if frame and frame.GetFrameStrata then
        local okS, s = pcall(frame.GetFrameStrata, frame)
        if okS then before = tostring(s) end
        if before == "DIALOG" then pcall(frame.SetFrameStrata, frame, "MEDIUM") end
    end
    local shown = pcall(StaticPopup_Show, "GQL_ROW_BATCH_SKIP", text, nil, data)
    GQL.Config.Log(("批量%s：弹确认 quest=%s 矩阵层=%s→%s%s")
        :format(data.mode == "clear" and "撤销" or "不做", tostring(data.questID),
            before, before == "DIALOG" and "MEDIUM" or before,
            shown and "" or "（弹窗没起来，未改动）"))
    return shown and true or false
end

--- 任务名那一格的右键（横排整行热区 / 竖排表头那一格，两排同一个入口）。
--- **只接右键**：这一格没有角色归属，格子那两档手动标记（完成/不做）搬过来就是谎报。
--- 收的是渲染时摆上去的那一份 row：判据、方向、文案全部就地读它，
--- 于是一枪从按下到弹框**零版面重算**、只问一次时钟（Global Constraints 那一条）。
--- 换轮/重载的安全门留在确认那一步（Mx.ApplyBatch 自己重新解析）。
function Mx.RowBatchClick(row)
    local db = GQL.Config.DB()
    if not db or not row or not row.questID then return end
    local counts = Mx.BatchCounts(row)
    if not counts.mode then
        GQL.Config.Log(("批量不做：这一行没有可改的格子 quest=%s（%d 号都在完成）")
            :format(tostring(row.questID), counts.total))
        return
    end
    local now = Mx.ClickClock()
    local rec = db.quests and db.quests[row.questID]
    local key = (GQL.Ledger and GQL.Ledger.InstanceKey)
        and GQL.Ledger.InstanceKey(db, row.questID, rec, now) or nil
    if not key then
        -- 比格子右键**有意更严**：格子那一档写下去只是暂时画不出来（读侧比不上键），
        -- 批量一枪十几格，等于往存档塞十几笔永远画不出来的记录。不许为「两处一致」改回去。
        GQL.Config.Log(("批量不做：这一条身份未定，未改动 quest=%s")
            :format(tostring(row.questID)))
        return
    end
    Mx.ShowBatchConfirm(Mx.BatchText(row, counts, now),
        { questID = row.questID, k = key, mode = counts.mode })
end

--- 6 位 hex（"rrggbb"）→ r,g,b（0~1），给 GameTooltip:AddLine 上色用。
HexToRGB = function(hex)
    if type(hex) ~= "string" or #hex < 6 then return 1, 1, 1 end
    return tonumber("0x" .. hex:sub(1, 2)) / 255,
        tonumber("0x" .. hex:sub(3, 4)) / 255,
        tonumber("0x" .. hex:sub(5, 6)) / 255
end

local function HideMoneyTooltip()
    -- 表头提示缓存跟着"提示没了"走（0.6 T6）：不管这条提示是不是我们弹的，
    -- 只要走到这里就说明下一轮悬停必须能重新弹，缓存不许把那条"不弹"留住。
    Head.tipBtn = nil
    -- 只收我们自己弹的那条（见 moneyTipOn）
    if not moneyTipOn then return end
    moneyTipOn = nil
    if GameTooltip and GameTooltip.Hide then pcall(GameTooltip.Hide, GameTooltip) end
end

local function ShowRowHighlight(y, btn)
    EnsureHiFrames()
    Head.tipBtn = nil   -- 亮行提示走的是行/格口径，表头那一份缓存作废（0.6 T6）
    if not (hiL and hiR) then return end
    hiRow = y
    hiL.__btn, hiL.__y = btn, y
    hiL:ClearAllPoints()
    hiL:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, -y)
    hiL:SetSize(NAME_W, ROW_H)
    hiR:ClearAllPoints()
    hiR:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
    hiR:SetSize(math.max(contentW, 1), ROW_H)
    hiL:Show()
    hiR:Show()
    -- 悬停时顺带把金额提示带上（行/格热区都存了 __questID；__charKey 只有状态格有）
    if btn and btn.__questID then
        ShowMoneyTooltip(btn, btn.__questID, btn.__charKey)
    else
        HideMoneyTooltip()
    end
end

local function HideHiFrame()
    -- 已经藏着就整条跳过：原来每帧都跑一遍两个 Hide + pcall(GameTooltip.Hide)，
    -- 鼠标压根没碰列表时也白烧每帧
    if not hiL or not hiL:IsShown() then
        -- 例外（0.6 T6）：竖排表头热区**只出提示不亮行**，高亮帧藏着不代表提示是干净的。
        -- 光标从表头挪到空白处时若被这道早退吞掉，金额提示会一直挂在表头上不走。
        -- HideMoneyTooltip 自己有 moneyTipOn/缓存两道守卫，没提示时是零成本空转。
        HideMoneyTooltip()
        return
    end
    if hiR then hiR:Hide() end
    hiL:Hide()
    hiL.__btn, hiL.__y = nil, nil
    HideMoneyTooltip()
end

--- 行分隔线：网页表格那种逐行淡线。rowTopY 是该行顶部坐标（PutL 用的那个 y），
--- 线画在这行底部（下一行顶上）。左窗格盖满任务列宽，右窗格盖满角色列宽。
--- alpha 默认 0.10（淡），组头下用更亮的值把"分组"这条界划清楚。
--- h = 这一行多高（任务行 ROW_H，地图分组行 GROUP_H），线才落在正确的行底。
--- 厚度按**设备像素**算，不写死 1 个 UI 单位（用户 2026-10-30："为什么胆大包天和巢穴
--- 两个任务之间没有分割线"）：线其实每行都画了，但 1 个 UI 单位在 UI 缩放 ≠ 1 时
--- 不足一个屏幕像素，客户端会把那一根整个丢掉 —— 那张截图里缺的两条（胆大包天下、
--- 浴血尖塔下）像素级看是零亮度，左右邻居却都在，所以是"整根没画"而不是"变淡"。
--- 取 1/scale = 正好一个设备像素：每个 y 上都一样清楚，滚动时也不会忽隐忽现。
--- 两个值（厚度 / 行底）算在这里而不是叫调用方传：调用方只管"这一行多高"。
local function RowBorder(rowTopY, alpha, h)
    alpha = alpha or 0.10
    usedLines = usedLines + 1
    local okS, s = pcall(content.GetEffectiveScale, content)
    local k = (okS and tonumber(s)) or 1
    if k <= 0 then k = 1 end
    local lineH = 1 / k
    -- 线的下沿压在行底：底边对齐下一行顶上，整条落在本行之内（不吃下一行的字）
    local off = -(rowTopY + (h or ROW_H)) + lineH
    local r = rowLines[usedLines]
    if not r then
        r = content:CreateTexture(nil, "BACKGROUND")
        rowLines[usedLines] = r
    end
    r:SetColorTexture(1, 1, 1, alpha)
    r:ClearAllPoints()
    r:SetPoint("TOPLEFT", content, "TOPLEFT", 0, off)
    r:SetSize(math.max(contentW, 1), lineH)
    r:Show()
    local l = rowLinesL[usedLines]
    if not l then
        l = contentL:CreateTexture(nil, "BACKGROUND")
        rowLinesL[usedLines] = l
    end
    l:SetColorTexture(1, 1, 1, alpha)
    l:ClearAllPoints()
    l:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, off)
    l:SetSize(NAME_W, lineH)
    l:Show()
end

--- 悬停准入门：焦点到底落在谁身上。IsMouseOver 只比几何、不看遮挡，别的窗口压在
--- 矩阵上面时，鼠标在那个窗口里却把矩阵这行点亮、金额提示照弹（用户实测）。
--- 客户端报出来的焦点才算数：
---   · 在矩阵子树里 → true，可以亮
---   · 别的窗口（关键词面板、设置面板都挂在 UIParent 上）、UIParent、nil → false，整条跳过
---   · 接口取不到（老客户端/离线替身没这个函数）→ nil，退回原来的纯几何判断
--- 返回 second 值 = 焦点帧本身，给抑制日志取名用。
local function FocusOnMatrix()
    if type(GetMouseFocus) ~= "function" then return nil end
    local okFocus, focus = pcall(GetMouseFocus)
    if not okFocus then return nil end
    if not focus or focus == UIParent then return false, focus end
    if focus == GameTooltip then
        -- 我们自己弹的金额提示就挂在热区右边，压着光标是常态。提示在 TOOLTIP 层、
        -- 不是矩阵子树，不认这一条就是自己锁死：弹→焦点变成提示→收→再弹……
        return moneyTipOn == true, focus
    end
    local node, depth, inside = focus, 0, false
    while node and depth < 12 do
        -- 这里原来有一句"先撞到'添加任务'弹框就判外部"的特判：那个弹框建在矩阵主帧
        -- 下面，只问"在不在子树里"会把鼠标算成矩阵内部，玩家停在弹框上背后那行照样
        -- 被点亮。搬进 UIParent 上的关键词面板之后，它不再是子帧，向上走自然撞不到
        -- frame，判外部这条天生就成立 —— 特判跟着弹框一起下线
        -- （用例 test_hover_stays_off_while_the_panel_is_open 钉着这个口径）。
        if node == frame then inside = true break end
        node = node.GetParent and node:GetParent() or nil
        depth = depth + 1
    end
    return inside, focus
end

--- 几何上被光标压着的热区：先整行热区（左窗格），再状态格（右窗格），
--- 最后表头热区（0.6 T6 竖排才有；它与正文两池几何上不相交，顺序不影响判定）。
--- 只看几何，不看遮挡 —— 亮不亮由 FocusOnMatrix 决定。
local function HoverTarget()
    for i = 1, usedRowBtns do
        local b = rowBtns[i]
        if b and b:IsShown() and b.IsMouseOver and b:IsMouseOver() then
            return b.__rowY, b
        end
    end
    for i = 1, usedBtns do
        local b = cellBtns[i]
        if b and b:IsShown() and b.IsMouseOver and b:IsMouseOver() then
            return b.__rowY, b
        end
    end
    -- 表头热区池（计划点 6 的三处之一）：只有竖排轮次会摆，遍历跟着 Head.btnUsed 走
    for i = 1, Head.btnUsed do
        local b = Head.btns[i]
        if b and b:IsShown() and b.IsMouseOver and b:IsMouseOver() then
            return b.__rowY, b
        end
    end
end

--- 匿名帧没有名字，退而报所在层级 —— 用户截图回来能认出是哪个窗口压的。
local function FocusName(f)
    if not f then return "无焦点" end
    local ok, n = pcall(f.GetName, f)
    if ok and n then return tostring(n) end
    local ok2, s = pcall(f.GetFrameStrata, f)
    if ok2 and s then return "未命名帧/" .. tostring(s) end
    return "未命名帧"
end

-- 抑制日志按"这次是被谁拦的"去重：同一个原因持续压着时只记第一条，不每帧刷。
local hoverGateLog

local function LogSuppress(msg)
    if not (GQL.Config and GQL.Config.Log) then return end
    if msg == hoverGateLog then return end
    hoverGateLog = msg
    GQL.Config.Log("hover 抑制：" .. msg)
end

--- 绘制层序表。只收有把握的值，认不出的层一律当"不在我们上面" ——
--- 宁可漏判也不许误杀 hover。每层留 4 格给现代客户端的 *_LOW/_MEDIUM/_HIGH 变体。
--- 跨层直接比这张表；同层还要再比层号，且要求对方有背板（见 HasBackdrop）。
local StrataRank = {}
do
    local order = { "WORLD", "BACKGROUND", "LOW", "MEDIUM", "HIGH", "DIALOG", "STRAP",
        "OVERLAY", "FULLSCREEN", "FULLSCREEN_DIALOG", "TOOLTIP", "ARTWORK",
        "ERROR", "LOADING" }
    for i = 1, #order do
        local base = i * 4
        StrataRank[order[i]] = base
        StrataRank[order[i] .. "_LOW"] = base + 1
        StrataRank[order[i] .. "_MEDIUM"] = base + 2
        StrataRank[order[i] .. "_HIGH"] = base + 3
    end
end

--- 该帧所在层 + 层内层号；认不出的层回 nil = 不参与遮挡判定。
local function StrataPos(f)
    local ok, s = pcall(f.GetFrameStrata, f)
    local rank = (ok and s and StrataRank[tostring(s)]) or nil
    if not rank then return nil end
    local ok2, lv = pcall(f.GetFrameLevel, f)
    return rank, (ok2 and tonumber(lv)) or 0
end

--- 同层同层号时谁在上面看不出来（客户端按创建顺序画），所以同层只认"层号更高、
--- 并且真有背板"的窗口 —— 暴雪铺满整屏的布局容器（UIParent*AllButtons 一类）
--- 没有背板，不会被误当成遮挡者。
local function HasBackdrop(f)
    local ok, bd = pcall(f.GetBackdrop, f)
    return ok and bd ~= nil
end

--- 顶层帧列表。现代客户端 GetChildren 回一张表，老接口/离线替身是一个个返回值，
--- 两种都收下：带 GetChildren 的是帧，不带的当成表摊开。
local function TopLevelFrames()
    if not (UIParent and UIParent.GetChildren) then return nil end
    local call = { pcall(UIParent.GetChildren, UIParent) }
    if not call[1] then return nil end
    local list = {}
    for i = 2, #call do
        local v = call[i]
        if type(v) == "table" then
            if v.GetChildren then
                list[#list + 1] = v
            else
                for _, f in pairs(v) do
                    if type(f) == "table" then list[#list + 1] = f end
                end
            end
        end
    end
    return list
end

--- 帧的几何取到的是它自己缩放下的坐标，跨帧比之前要换算到 UIParent 坐标系
--- （客户端可以整体缩放，插件窗口也能各自设 scale）。
local function UiScale(f)
    local ok, s = pcall(f.GetEffectiveScale, f)
    s = (ok and tonumber(s)) or 1
    local okU, u = pcall(UIParent.GetEffectiveScale, UIParent)
    u = (okU and tonumber(u)) or 1
    if s <= 0 or u <= 0 then return 1 end
    return s / u
end

--- 允许真正拦 hover 的窗口名。通用扫描会撞上各种常驻的全屏帧（12.1.5 实测每次 hover
--- 都命中 `TimerTracker`，还有一样铺满屏的 `GlobalFXDialogModelScene`），一律拦的结果
--- 是 hover 整个没了 —— 所以名单之外的命中只写日志不改行为。
--- 名单里只放"盖住矩阵是常态、且空白处不吃鼠标（焦点门拦不住）"的窗口。
--- 自家那几个窗口（设置面板/日志窗/关键词面板）**不放进来**：它们都 EnableMouse(true)，
--- 焦点门本来就拦得住；而且 12.x 起 addon 用 CreateFrame 传的名字**不再注册成全局**
--- （实测 `_G.GQLOptionsFrame`/`_G.GQLLogFrame` 取不到），按名字判是条死路。
-- 2026-11-01 加两名：暴雪选项面板（SettingsPanel，我们的设置页正挂在里面）与 ESC 菜单
-- （GameMenuFrame）—— 它们盖住矩阵时同样不该点亮行。矩阵打开时临时在 DIALOG
-- （与这两张同层），让位后回到 MEDIUM（与地图同层）：两档都由下面 sameRankCounts 判。
-- 取不到全局就跳过（pcall + nil 判定在下游），老客户端没有 SettingsPanel 也不报错。
local CoverBlock = { WorldMapFrame = true, SettingsPanel = true, GameMenuFrame = true }

--- 这张帧是否画在我们之上、并且把 (cx,cy) 这点盖住。所有取值全程 pcall：
--- 认不出层序、取不到尺寸/中心、不可见、几乎全透明 —— 任一不成立都判"没盖住"，
--- 宁可漏判也不许误杀 hover。
--- sameRankCounts = 同层也算压在上面。名单内的窗口用 true：12.1.5 实测
--- `WorldMapFrame` 与矩阵同为 MEDIUM、层号同为 1，而开图后地图画在我们上面
--- （同层同层号谁在上面由客户端的重排顺序决定，API 问不出来）。要求"层号更高"
--- 就等于永远判不到它 —— 这才是前几轮"日志里没有一条 WorldMapFrame"的真因。
local function CoversPoint(f, cx, cy, myRank, myLevel, sameRankCounts)
    if not f or f == frame or f == UIParent or f == GameTooltip then return false end
    local rank, level = StrataPos(f)
    if not rank or rank < myRank then return false end
    if rank == myRank and not sameRankCounts
        and not (level > myLevel and HasBackdrop(f)) then return false end
    local okV, vis = pcall(f.IsVisible, f)
    if not (okV and vis) then return false end
    local okA, a = pcall(f.GetAlpha, f)
    if okA and type(a) == "number" and a < 0.5 then return false end
    local okS, w, h = pcall(f.GetSize, f)
    local okP, fx, fy = pcall(f.GetCenter, f)
    w, h, fx, fy = tonumber(w), tonumber(h), tonumber(fx), tonumber(fy)
    if not (w and h and fx and fy) or w <= 0 or h <= 0 then return false end
    local kf = UiScale(f)
    return math.abs(fx * kf - cx) * 2 <= w * kf
        and math.abs(fy * kf - cy) * 2 <= h * kf
end

--- 有没有别的窗口画在矩阵上面、正好把这一点盖住。
--- 为什么还要多这一趟：GetMouseFocus 只认"吃鼠标"的帧，暴雪地图的空白处/边框
--- 不吃鼠标，焦点会穿透到压在下面的矩阵热区上 —— 焦点门放行、金额提示照样从
--- 地图底下弹出来（12.1.5 实测截图）。遮挡只能自己按绘制序算。
--- 回两个值：block = 名单内、该拦的覆盖帧；shadow = 名单外的第一个（只用来出线索）。
--- 两趟分开走：
---   1) 名单内的按**全局名**直接取帧判定（暴雪自家帧确实有全局），同层也算压在上面；
---   2) 名单外的线索才扫 `UIParent` 顶层子帧，且必须扫完再挑：`TimerTracker` 这类常驻
---      全屏帧排在地图前面，找到第一个命中就返回会让名单判定形同虚设（同一次实测）。
local function CoveringFrame(b)
    local myRank, myLevel = StrataPos(frame)
    if not myRank then return nil end
    local okC, cx, cy = pcall(b.GetCenter, b)
    if not okC or type(cx) ~= "number" or type(cy) ~= "number" then return nil end
    local kb = UiScale(b)
    cx, cy = cx * kb, cy * kb
    for name in pairs(CoverBlock) do
        local okG, gf = pcall(function() return _G[name] end)
        if okG and gf and CoversPoint(gf, cx, cy, myRank, myLevel, true) then return gf end
    end
    local kids = TopLevelFrames()
    if not kids then return nil end
    local shadow
    for i = 1, #kids do
        local f = kids[i]
        -- 只认有名字的：无名全屏辅助帧（光效、遮罩、暴雪那些布局容器）太多，
        -- 拿它们出线索会把日志刷满，而真凶基本都是有名字的窗口。
        local okN, n = pcall(f.GetName, f)
        if okN and n and not CoverBlock[tostring(n)]
            and CoversPoint(f, cx, cy, myRank, myLevel) then
            shadow = shadow or f
        end
    end
    return nil, shadow
end

-- 遮挡扫描比整棵顶层帧树贵：同一个热区 0.5 秒内只判一次；换行立刻重判。
local coverBtn, coverAt, coverBlockHit, coverShadow
local function CoverCheck(b)
    local now = (GetTime and tonumber(GetTime())) or 0
    if b == coverBtn and now - (coverAt or -99) < 0.5 then return coverBlockHit, coverShadow end
    coverBtn, coverAt = b, now
    coverBlockHit, coverShadow = CoveringFrame(b)
    return coverBlockHit, coverShadow
end

-- 最近一次几何压到的热区：/gql probe 的遮挡段拿它的坐标去比各个窗口的矩形。
local hoverLast

--- 探针用：把一张帧在遮挡判定里能看到的每个字段都摊开（含"盖没盖住 (cx,cy)"）。
--- 12.1.5 实测：地图明明压着矩阵，日志里却只有 TimerTracker 那条名单外线索，
--- 说明 WorldMapFrame 压根没进候选。是"不是 UIParent 直接子帧""尺寸取不到"还是
--- "层序认不出"，只有这份报告分得出来，别再靠猜改代码。
local function CoverFactLine(f, cx, cy)
    local okN, n = pcall(f.GetName, f)
    local okT, s = pcall(f.GetFrameStrata, f)
    local rank, level = StrataPos(f)
    local okS, w, h = pcall(f.GetSize, f)
    local okC, fx, fy = pcall(f.GetCenter, f)
    local okV, vis = pcall(f.IsVisible, f)
    local okA, a = pcall(f.GetAlpha, f)
    w, h, fx, fy = tonumber(w), tonumber(h), tonumber(fx), tonumber(fy)
    local hit = "?"
    if cx and w and h and fx and fy and w > 0 and h > 0 then
        local kf = UiScale(f)
        hit = (math.abs(fx * kf - cx) * 2 <= w * kf
            and math.abs(fy * kf - cy) * 2 <= h * kf) and "是" or "否"
    end
    return ("%s 层=%s 名次=%s 层号=%s 尺寸=%sx%s 中心=%s,%s alpha=%s 可见=%s 盖住=%s"):format(
        (okN and n and tostring(n) or "未命名"), tostring(s or "?"),
        rank and tostring(rank) or "认不出", tostring(level or "?"),
        tostring(w or "?"), tostring(h or "?"),
        tostring(fx or "?"), tostring(fy or "?"),
        tostring(okA and a or "?"), tostring(okV and vis or "?"), hit)
end

function Mx.HoverDebug()
    local lines = {}
    if not frame then return { "矩阵还没建（先 /gql matrix）" } end
    local myRank, myLevel = StrataPos(frame)
    local cx, cy
    if hoverLast then
        local okC, x, y = pcall(hoverLast.GetCenter, hoverLast)
        if okC then
            local k = UiScale(hoverLast)
            cx, cy = (tonumber(x) or 0) * k, (tonumber(y) or 0) * k
        end
        lines[#lines + 1] = ("热区 %s"):format(CoverFactLine(hoverLast, cx, cy))
    else
        lines[#lines + 1] = "热区=还没 hover 过（开着矩阵把鼠标压在任务行上再跑本命令）"
    end
    lines[#lines + 1] = ("矩阵 %s"):format(CoverFactLine(frame, cx, cy))
    local kids = TopLevelFrames()
    if not kids then
        lines[#lines + 1] = "UIParent:GetChildren 取不到 —— 遮挡判定整段跑不了"
        return lines
    end
    lines[#lines + 1] = ("顶层子帧共 %d 张，名单内的逐张摊开（拦=这帧会不会真的拦 hover）：")
        :format(#kids)
    for name in pairs(CoverBlock) do
        local okG, gf = pcall(function() return _G[name] end)
        if not (okG and gf) then
            lines[#lines + 1] = ("名单内 %s=全局取不到"):format(name)
        else
            local isTop = false
            for i = 1, #kids do if kids[i] == gf then isTop = true break end end
            lines[#lines + 1] = ("名单内 %s 顶层=%s 拦=%s | %s"):format(
                name, isTop and "是" or "否",
                (cx and CoversPoint(gf, cx, cy, myRank, myLevel, true)) and "是" or "否",
                CoverFactLine(gf, cx, cy))
        end
    end
    local shown, skipped = 0, 0
    local sameRank
    for i = 1, #kids do
        local f = kids[i]
        local rank = StrataPos(f)
        local okV, vis = pcall(f.IsVisible, f)
        local okN, n = pcall(f.GetName, f)
        if f ~= frame and rank and myRank and okV and vis and okN and n then
            if rank > myRank then
                if shown < 12 then
                    shown = shown + 1
                    lines[#lines + 1] = ("我们之上 %s"):format(CoverFactLine(f, cx, cy))
                else
                    skipped = skipped + 1
                end
            elseif rank == myRank and cx and CoversPoint(f, cx, cy, myRank, myLevel) then
                sameRank = ((sameRank or 0) + 1)
                if sameRank <= 6 then
                    lines[#lines + 1] = ("同层且盖住该点 %s"):format(tostring(n))
                end
            end
        end
    end
    if skipped > 0 then lines[#lines + 1] = ("其余 %d 张同条件帧省略"):format(skipped) end
    return lines
end

--- 每帧扫一遍：哪个热区被鼠标压着就亮哪行。挂在主帧的 OnUpdate 上，
--- 面板开着才跑（隐藏的帧不收 OnUpdate）。
--- GDL（GoldDailyLedger）主窗 / 流水窗压在矩阵上面时整条不亮行：与自家设置窗同口径
--- （Options.PanelUnderMouse），但走模块引用取帧 —— 12.x 的 CreateFrame 名字不进 _G，
--- 照 GCL/Core.lua 的 RaiseFront 那一套取帧方式，不摸 _G 里的帧名。光标在 GCL 任一可见
--- 窗口里就判外部；取不到（GCL 没装 / 还没建窗）就当"没压着"，绝不许误杀 hover。
local function GCLWindowUnderMouse()
    local gcl = _G.GCL
    if type(gcl) ~= "table" then return false end
    for _, mod in ipairs({ "Calendar", "Flows" }) do
        local m = gcl[mod]
        if type(m) == "table" and type(m.GetFrame) == "function" then
            local ok, f = pcall(m.GetFrame)
            if ok and type(f) == "table" and f.IsShown and f:IsShown()
               and f.IsMouseOver and f:IsMouseOver() then
                return true
            end
        end
    end
    return false
end

--- 扫描量不是这段循环自己"轻"，它只遍历 1..usedRowBtns / 1..usedBtns，
--- 这两个计数由 Render 本轮真正摆出来的热区决定 —— 所以它跟着 Render 的可见区裁剪走。
--- 200 行×20 列全量排版时实测 4200 次 IsMouseOver/帧（≈25 万次/秒），那时一点都不轻；
--- 裁到可见行×可见列之后才回到常数级。改 Render 的裁剪口径等于改这里的开销，两边要一起看。
local function HoverScan()
    if not frame or not frame:IsShown() then return end
    -- 自家的设置/关键词窗口压在身上时整条不亮（用户 2026-11-01 截图实测：两个窗口确实
    -- 画在矩阵前面，鼠标停在窗口里，矩阵那行和金额提示照样跟着走）。
    -- 为什么焦点门拦不住：那两个窗口的正文全是可点控件，GetMouseFocus 落到子控件上时
    -- 焦点链照样走不到矩阵，但**行热区的几何**仍被光标压着，而遮挡扫描只认名单内的帧
    -- （WorldMapFrame 那一条）。所以这里直接问窗口几何 —— 自家窗口打开时与矩阵同在
    -- MEDIUM，不能用层序判，只能按矩形判，判据只有 Options.PanelUnderMouse 一处。
    if GQL.Options and GQL.Options.PanelUnderMouse
        and GQL.Options.PanelUnderMouse() == true then
        HideHiFrame()
        return
    end
    -- 自家 GCL 窗口（日历/流水）压在矩阵上面时整条不亮：与 Options.PanelUnderMouse 同口径，
    -- 走模块引用取帧（12.x 的 CreateFrame 名字不进 _G），不摸 _G 里的帧名。GCL 没装 / 没开窗
    -- 就当没压着，绝不许误杀 hover（用户 2026-11-10 截图：GCL 盖住 GQL 时矩阵还在亮行）。
    if GCLWindowUnderMouse() then
        HideHiFrame()
        return
    end
    local found, foundBtn = HoverTarget()
    local inside, focus = FocusOnMatrix()
    if inside == false then
        if found then LogSuppress(("焦点在 %s 上，不在矩阵里"):format(FocusName(focus))) end
        HideHiFrame()
        return
    end
    if not found then
        hoverGateLog = nil
        HideHiFrame()
        return
    end
    hoverLast = foundBtn
    local cover, shadow = CoverCheck(foundBtn)
    if cover then
        local okN, cn = pcall(cover.GetName, cover)
        LogSuppress(("热区被 %s 盖住"):format(cn and tostring(cn) or FocusName(cover)))
        HideHiFrame()
        return
    end
    if shadow then
        -- 名单外：不改行为，只留一条线索。线索记在**那张帧**上、每个名字整场只写一次：
        -- TimerTracker 这种常驻全屏帧每次 hover 都命中，而下面那行 hoverGateLog 一清，
        -- 按消息去重就等于每帧一条，同一个名字能把整份日志挤满（12.1.5 实测）。
        if not shadow.__gqlShadowSeen then
            shadow.__gqlShadowSeen = true
            local okN, cn = pcall(shadow.GetName, shadow)
            LogSuppress(("热区被 %s 盖住（名单外，只记录）")
                :format(cn and tostring(cn) or FocusName(shadow)))
        end
    end
    hoverGateLog = nil
    -- 表头热区分岔（0.6 T6，计划点 6 的第三处）：竖排表头那格**没有"行"可亮**
    -- （高亮帧挂在正文内容区上，表头不在其中），只出行级金额提示。
    -- charKey 传 nil = 行级金额、没有角色归属，绝不会替某个号下结论（口径同整行热区）。
    -- 缓存 Head.tipBtn：提示里金币那行是 |T…|t 图标串，每帧重建等于每帧重问一遍纹理
    -- —— 同一格连悬停只重弹一次（口径同下面横排那条"同一行整帧跳过"）。
    -- 顺序讲究：先把正文那行的旧高亮（连同旧提示）收一次，再弹这一格的；
    -- 高亮已藏时不许每帧走 HideHiFrame —— 它的兜底分支会把刚弹的提示收掉，
    -- 变成弹→收→弹的每帧抖动。
    if foundBtn.__head then
        if hiL and hiL:IsShown() then
            HideHiFrame()
        end
        if Head.tipBtn ~= foundBtn then
            Head.tipBtn = foundBtn
            ShowMoneyTooltip(foundBtn, foundBtn.__questID, nil)
        end
        return
    end
    -- 还是同一行、同一个热区就整帧跳过：原来每帧都重摆高亮 + 重建 tooltip，而提示里
    -- 金币那行是 |T…|t 图标串，每帧 SetText 等于每帧重建纹理 —— 鼠标压在列表上就一直掉帧。
    if hiL and hiL:IsShown() and hiL.__btn == foundBtn and hiL.__y == found then return end
    ShowRowHighlight(found, foundBtn)
end

--- 当前角色的整列背景（职业色淡条）：一眼找到自己那列
-- （用户要求"整个角色列背景亮一点"，不是只高亮表头格子）。
-- 高度在 Render 末尾按内容高拉满整列；这里先定横向位置和颜色。
local curHi
local function ShowCurCharHeader(cx, classToken)
    if not curHi then
        curHi = CreateFrame("Frame", nil, content)
        pcall(function()
            local tex = curHi:CreateTexture(nil, "BACKGROUND")
            tex:SetAllPoints(curHi)
            curHi.__tex = tex
        end)
    end
    if not curHi then return end
    local cc = RAID_CLASS_COLORS and classToken and RAID_CLASS_COLORS[classToken]
    local r, g, b = 1, 0.84, 0.4
    if cc then r, g, b = cc.r, cc.g, cc.b end
    if curHi.__tex then
        -- 整列铺满，透明度压到 0.18，淡但整列一眼可辨（"亮一点"不是"糊一片"）
        pcall(curHi.__tex.SetColorTexture, curHi.__tex, r, g, b, 0.18)
    end
    curHi.__cx = cx
    curHi:ClearAllPoints()
    curHi:SetPoint("TOPLEFT", content, "TOPLEFT", cx, 0)
    curHi:SetSize(COL_W, ROW_H)
    curHi:Show()
end

--- 真滚动条（竖 + 横）。之前只有滚轮：不知道能滚、也看不到滚到哪了，
--- 角色一多状态直接溢出到帧外（用户实测反馈）。拖动滑块改偏移再重绘。
--- 竖条管两个窗格共用的纵向偏移；横条只管右窗格的横向偏移。
local barV, thumbV, barH, thumbH
local function BuildScrollbars()
    -- 竖条：贴帧右缘。轨道深灰、滑块亮灰 —— 底色纹理 WHITE8X8 本身是白的，
    -- 不显式上色的话轨道滑块一片白，看着就"只有一条白线"（用户实测）
    barV = CreateFrame("Frame", "GQLMatrixBarV", frame, "BackdropTemplate")
    barV:SetWidth(10)
    barV:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -4, -34)
    barV:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -4, 14)
    pcall(barV.SetBackdrop, barV, { bgFile = "Interface/Buttons/WHITE8X8" })
    pcall(barV.SetBackdropColor, barV, 0.08, 0.08, 0.10, 0.85)
    thumbV = CreateFrame("Button", "GQLMatrixThumbV", barV, "BackdropTemplate")
    thumbV:EnableMouse(true)
    pcall(thumbV.SetBackdrop, thumbV, { bgFile = "Interface/Buttons/WHITE8X8" })
    pcall(thumbV.SetBackdropColor, thumbV, 0.55, 0.55, 0.60, 0.95)
    -- 横条：贴帧底缘（只跨右窗格，任务列冻结不参与横滚）
    barH = CreateFrame("Frame", "GQLMatrixBarH", frame, "BackdropTemplate")
    barH:SetHeight(10)
    barH:SetPoint("BOTTOMLEFT", frame, "BOTTOMLEFT", NAME_W + 4, 4)
    barH:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -16, 4)
    pcall(barH.SetBackdrop, barH, { bgFile = "Interface/Buttons/WHITE8X8" })
    pcall(barH.SetBackdropColor, barH, 0.08, 0.08, 0.10, 0.85)
    thumbH = CreateFrame("Button", "GQLMatrixThumbH", barH, "BackdropTemplate")
    thumbH:EnableMouse(true)
    pcall(thumbH.SetBackdrop, thumbH, { bgFile = "Interface/Buttons/WHITE8X8" })
    pcall(thumbH.SetBackdropColor, thumbH, 0.55, 0.55, 0.60, 0.95)
end

--- 给滑块挂拖动：按下记住起点，OnUpdate 里按光标位移换算成内容偏移。
--- 光标位移要除屏幕缩放（GetCursorPosition 给的是物理像素），纵向还要取反
--- （它的 y 从屏幕底往上算）：之前两样都没处理，一拖滑块就往反方向跑，
--- 用户实测成"拖不动，只能滚轮"。
local function HookThumb(thumb, bar, axis)
    thumb:SetScript("OnMouseDown", function(self, button)
        if button ~= "LeftButton" then return end
        local cx, cy = GetCursorPosition()
        local start = { cx, cy }
        local scale = (bar.GetEffectiveScale and bar:GetEffectiveScale()) or 1
        if type(scale) ~= "number" or scale <= 0 then scale = 1 end
        local base = (axis == "v") and offY or offX
        self:SetScript("OnUpdate", function(self2)
            local nx, ny = GetCursorPosition()
            local delta = ((axis == "v") and (start[2] - ny) or (nx - start[1])) / scale
            local trackLen = (axis == "v") and (bar:GetHeight() or 0) or (bar:GetWidth() or 0)
            local thumbLen = (axis == "v") and (self2:GetHeight() or 0) or (self2:GetWidth() or 0)
            local span = math.max(1, trackLen - thumbLen)
            local contentLen = (axis == "v")
                and math.max(0, contentH - visH) or math.max(0, contentW - visW)
            local v = base + (delta / span) * contentLen
            if axis == "v" then offY = v else offX = v end
            ClampOffsets()
            Mx.Render()
        end)
    end)
    thumb:SetScript("OnMouseUp", function(self)
        self:SetScript("OnUpdate", nil)
    end)
end

--- 每轮渲染后刷新滚动条：滑块长度 ∝ 可见区/内容，位置 ∕ 偏移。
--- 内容不超可见区时整条藏起来，不占地方。
local function UpdateScrollbars()
    if not barV then return end
    local maxY = math.max(0, contentH - visH)
    local maxX = math.max(0, contentW - visW)
    local trackH = barV:GetHeight() or 0
    local trackW = barH:GetWidth() or 0
    if maxY <= 0 or trackH <= 0 then
        barV:Hide()
        thumbV:Hide()
    else
        barV:Show()
        thumbV:Show()
        local th = math.max(24, trackH * (visH / contentH))
        thumbV:SetSize(10, th)
        thumbV:ClearAllPoints()
        thumbV:SetPoint("TOP", barV, "TOP", 0, -(offY / maxY) * (trackH - th))
    end
    -- <1 而非 <=0：亚像素误差（四舍五入）会让本该刚好放下的内容算出极小正溢出，
    -- 把横向滚动条永远点亮还吃掉一行位置（用户实测）。差不到 1px 视为"不需要滚"。
    if maxX < 1 or trackW <= 0 then
        barH:Hide()
        thumbH:Hide()
    else
        barH:Show()
        thumbH:Show()
        local tw = math.max(24, trackW * (visW / contentW))
        thumbH:SetSize(tw, 10)
        thumbH:ClearAllPoints()
        thumbH:SetPoint("LEFT", barH, "LEFT", (offX / maxX) * (trackW - tw), 0)
    end
end

-- 前向声明已移到模块状态区（上面的闭包要先能看见它）

--- 文字池按字体分桶：一种字体一条池子，条目创建时就把字体定死，复用只改文字和位置。
--- 为什么不共用一条池（12.1.5 实测 bug）：FontString 建好以后没有可靠的换字体接口
--- （SetFont 要重传字体文件名 + 字号，暴雪那套系统字体名问不出来），所以原来只 SetText
--- 的复用等于"沿用上一轮那条的字体"。组头用大号字、任务名用小一号的字，共用一条池子时
--- 只要行序一变（新加一条任务、某组多了一行），任务名就会领到组头那条大字 ——
--- 表现就是"加了奈格塔尔虚空决战之后字体大小不一致、看着像错行"，
--- 而且刷新永远修不好（池子还是那批条目），只有重建帧（换号登录 /reload）才恢复。
local Pool = {}
function Pool.Get(pools, parent, font)
    local p = pools[font]
    if not p then
        p = { items = {}, used = 0 }
        pools[font] = p
    end
    p.used = p.used + 1
    local t = p.items[p.used]
    if not t then
        t = parent:CreateFontString(nil, "OVERLAY", font)
        p.items[p.used] = t
    end
    return t
end

--- Begin 把各桶的游标归零、End 收起这轮没用上的尾巴。
--- Show 由各 Put 补，不能省：下一轮行数变多复用到隐藏条目时，只 SetText 不 Show
--- 就是一行字消失而热区还在
--- （12.1.5 实测：从"已忽略"恢复一行后，最底下那组任务全看不见了）。
function Pool.Begin(pools)
    for _, p in pairs(pools) do p.used = 0 end
end

function Pool.End(pools)
    for _, p in pairs(pools) do
        for i = p.used + 1, #p.items do p.items[i]:Hide() end
    end
end

--- 左窗格文本池：任务名/组头挂这里，横滚时不随内容动（冻结列）。
local function PutL(x, y, text, font)
    local t = Pool.Get(textsL, contentL, font or "GameFontNormalSmall")
    t:SetText(text)
    t:ClearAllPoints()
    t:SetPoint("TOPLEFT", contentL, "TOPLEFT", x, -y)
    t:Show()
    return t
end

--- 居中版 Put：单元格内容居中（用户要求）。px/py 是单元格中心在内容区的坐标。
local function PutCenter(px, py, text, font)
    local t = Pool.Get(texts, content, font or "GameFontNormalSmall")
    t:SetText(text)
    t:ClearAllPoints()
    t:SetPoint("CENTER", content, "TOPLEFT", px, -py)
    t:Show()
    return t
end

--- 表头文本池：挂在固定的 Head.right 上（在滚动区之外，纵向滚不到它）。
--- px 是列中心已经减掉 offX 的可视坐标，所以横向滚时列头跟着列走。
--- Head.right 没有裁剪能力，滑出右窗格的那几列必须手动藏，否则会画到任务列/帧外。
--- 助手都收进 Head 表（不是顶层 local）：顶层 local 会变成 Render 的 upvalue，
--- 而 WoW 的 Lua 5.1 每个闭包上限 60 个，超了整块编译失败、矩阵直接不存在。
--- rowY（0.6 T6）：表头行的纵向中心偏移。缺省 = -ROW_H/2，逐字等于本函数搬家前的
--- 那句 SetPoint —— 横排调用一个参数都不多传，表头=1 行照常；
--- 竖排表头=2 行，分组行用缺省、任务名行传 -ROW_H - ROW_H/2（计划 T6 表格）。
function Head.Put(px, text, font, rowY)
    if not Head.right then return end
    local t = Pool.Get(Head.pool, Head.right, font or "GameFontNormalSmall")
    t:SetText(text)
    t:ClearAllPoints()
    t:SetPoint("CENTER", Head.right, "TOPLEFT", px, rowY or -ROW_H / 2)
    if px + COL_W / 2 < 0 or px - COL_W / 2 > visW then t:Hide() else t:Show() end
    return t
end

--- 组界竖线（0.6 T8，用户 2026-11-09："竖排的时候分组之间没有明确的分割线，
--- 想知道这个任务属于哪个地图一眼看不出"）：一个边界一次摆两截，上下接成一根 ——
---   正文那截挂 content（滚动区自己裁，x 是内容坐标，**不许**减 offX）；
---   表头那截挂 Head.right（在滚动区之外，x 已经减掉 offX，滑出右窗格必须手动藏，
---     口径同 Head.Put —— Head.right 没有裁剪能力，不藏就画到帧外）。
--- 两截都**上下两头钉**（TOPLEFT + BOTTOMLEFT，同一 x），高度由两头算出来：正文那截
--- 自动跟着 contentH 长、表头那截自动正好两行表头，写死高度就得每轮重算。
--- 厚度 = 1/scale 设备像素（口径同 RowBorder：1 个 UI 单位在 UI 缩放 ≠ 1 时不足一个
--- 屏幕像素，客户端会整根丢掉，玩家看到的正是"这几个分组之间没有线"）。两截都挂在
--- 同一个主帧下面，缩放不可能不同，所以只问 content 那一次。
--- alpha 0.22 = 横排组头那条下划线的档（分组界一档，逐行淡线 0.10 又一档）。
function Head.Div(contentX, headX)
    if not Head.right then return end
    local okS, s = pcall(content.GetEffectiveScale, content)
    local k = (okS and tonumber(s)) or 1
    if k <= 0 then k = 1 end
    local w = 1 / k
    Head.divUsed = Head.divUsed + 1
    local n = Head.divUsed
    local body = Head.bDiv[n]
    if not body then
        body = content:CreateTexture(nil, "BACKGROUND")
        Head.bDiv[n] = body
    end
    body:SetColorTexture(1, 1, 1, 0.22)
    body:ClearAllPoints()
    body:SetPoint("TOPLEFT", content, "TOPLEFT", contentX, 0)
    body:SetPoint("BOTTOMLEFT", content, "BOTTOMLEFT", contentX, 0)
    body:SetWidth(w)
    body:Show()
    local head = Head.hDiv[n]
    if not head then
        head = Head.right:CreateTexture(nil, "BACKGROUND")
        Head.hDiv[n] = head
    end
    head:SetColorTexture(1, 1, 1, 0.22)
    head:ClearAllPoints()
    head:SetPoint("TOPLEFT", Head.right, "TOPLEFT", headX, 0)
    head:SetPoint("BOTTOMLEFT", Head.right, "BOTTOMLEFT", headX, 0)
    head:SetWidth(w)
    if headX < -w or headX > visW then head:Hide() else head:Show() end
end

--- 当前角色列头底色：和正文整列高亮同色，表头那格才看得出哪列是"我"。
--- 每轮按 class 重算颜色，位置随 offX 摆；不是当前角色就藏。
--- w/h（0.6 T6）：缺省 = COL_W × ROW_H，逐字等于横排现状；竖排把**同一块**底色帧
--- 挪到左窗格表头那格（计划点 4：三条路都要换轴，但不许新建第二块底色帧）。
function Head.ShowCur(px, classToken, show, w, h)
    if not Head.right then return end
    if not Head.cur and show then
        Head.cur = CreateFrame("Frame", "GQLMatrixHeadCur", Head.right)
        pcall(function()
            local tex = Head.cur:CreateTexture(nil, "BACKGROUND")
            tex:SetAllPoints(Head.cur)
            Head.cur.__tex = tex
        end)
    end
    if not Head.cur then return end
    if not show then
        Head.cur:Hide()
        return
    end
    local cc = RAID_CLASS_COLORS and classToken and RAID_CLASS_COLORS[classToken]
    local r, g, b = 1, 0.84, 0.4
    if cc then r, g, b = cc.r, cc.g, cc.b end
    pcall(Head.cur.__tex.SetColorTexture, Head.cur.__tex, r, g, b, 0.18)
    Head.cur:ClearAllPoints()
    Head.cur:SetPoint("TOPLEFT", Head.right, "TOPLEFT", px, 0)
    Head.cur:SetSize(w or COL_W, h or ROW_H)
    if px + (w or COL_W) < 0 or px > visW then Head.cur:Hide() else Head.cur:Show() end
end

--- 竖排表头热区（0.6 T6，计划点 6 的"表头新增可悬停热区"）：盖在表头第二行
--- （任务名行）的每一列上。可悬停，另外**只接右键**：整条任务本轮所有角色都不做
--- （Mx.RowBatchClick，和横排任务名那一格同一个入口，2026-11-11 补满竖排缺口）。
--- 原来那句"这一格没有角色归属，CellClick 的两档手动标记语义在这里不成立"照旧成立 ——
--- 所以左键仍不注册；批量这一档是按任务问所有号，本来就不需要归属。
--- 要同时进三处：
---   ① HoverTarget 的池遍历（本池排在状态格之后；它与正文热区几何上不相交，顺序无关）
---   ② 末尾"多余热区收起来"的 Hide 循环（见 RenderCharsInRows 与 Mx.Render 分派处）
---   ③ ShowRowHighlight/金额提示的分岔（见 HoverScan：表头那格没有"行"可亮，只出提示）
--- Head.right 没有裁剪能力：滑出可视区的列必须手动藏（口径同 Head.Put），
--- 否则一只哑热区压在左窗格"角色"二字上，还会把滚轮之外的事件吞掉。
function Head.Button(x, row)
    Head.btnUsed = Head.btnUsed + 1
    local b = Head.btns[Head.btnUsed]
    if not b then
        b = CreateFrame("Button", nil, Head.right)
        b:EnableMouse(true)
        -- 滚轮透传必须挂：热区盖在表头条上，不挂就正好复现用户实测过的
        -- "滚轮大部分情况无效"（按钮吃掉事件、OnMouseWheel 为 nil）。
        -- 注册点击不会顶掉它：两者挂的是不同的脚本槽。
        b:SetScript("OnMouseWheel", OnMouseWheel)
        -- 右键 = 整条任务本轮所有角色批量"不做"/撤销（与横排左栏任务名行同一支
        -- Mx.RowBatchClick）。只注册右键：表头这格没有角色归属，格子那两档手动标记
        -- 搬过来就是谎报。__row 存渲染时那一份行，点击就地读它，零重算（池子复用，
        -- 每轮排版都重写）。
        pcall(b.RegisterForClicks, b, "RightButtonUp")
        b:SetScript("OnClick", function(self, button)
            if button == "RightButton" then Mx.RowBatchClick(self.__row) end
        end)
        Head.btns[Head.btnUsed] = b
    end
    b:ClearAllPoints()
    b:SetPoint("TOPLEFT", Head.right, "TOPLEFT", x, -ROW_H)
    b:SetSize(COL_W, ROW_H)
    b.__questID = row and row.questID or nil  -- 悬停金额提示用（行级口径，无角色归属）
    b.__row = row                             -- 批量与提示那一句就地读这份（零重算）
    b.__rowY = 0                    -- 表头没有正文行；HoverScan 的表头分岔不看它
    b.__head = true                 -- HoverScan 分岔的判据（池里复用，恒真不用清）
    b.__type = "Button"             -- 测试热区池遍历按它认表头那一格
    if x + COL_W < 0 or x > visW then b:Hide() else b:Show() end
    return b
end

--- 把偏移夹在内容范围内：内容比可视区小就不给滚（偏移归零），
--- 滚过头了回弹到边界。可视尺寸取右窗格的实测值。
ClampOffsets = function()
    local vw, vh = 0, 0
    if scroll then
        if scroll.GetWidth then vw = scroll:GetWidth() or 0 end
        if scroll.GetHeight then vh = scroll:GetHeight() or 0 end
    end
    -- 0 在 Lua 里是真值，取不到尺寸时按 Render 估的可见区兜底
    if type(vw) ~= "number" or vw <= 0 then vw = visW end
    if type(vh) ~= "number" or vh <= 0 then vh = visH end
    local maxX = math.max(0, contentW - vw)
    local maxY = math.max(0, contentH - vh)
    if offX > maxX then offX = maxX end
    if offY > maxY then offY = maxY end
    if offX < 0 then offX = 0 end
    if offY < 0 then offY = 0 end
end

OnMouseWheel = function(self, delta)
    local horiz = IsShiftKeyDown and IsShiftKeyDown()
    -- 这个客户端往下滚轮给的是负 delta，所以是"减"：往下滚 = 偏移变大 =
    -- 看到后面的行，和网页一个方向（用户实测：加 delta 的手感是反的）。
    if horiz then
        offX = offX - delta * 40
    else
        offY = offY - delta * 40
    end
    -- 不在这里夹偏移：Render 末尾会用新算出的内容尺寸夹。提前夹会拿到
    -- 上一次的旧尺寸，内容刚变多时第一格滚动会被吞掉（用户实测"滚不动"）
    HideHiFrame()          -- 滚动了高亮就收起，免得停在错误的位置
    Mx.Render()
end

--- 打开/点击矩阵：本窗常驻 DIALOG（Ensure 里已设），这里只 Raise 到同层最上。
--- toplevel 已能在点击时自动置顶，但 OnShow 不一定触发 toplevel 的自动 Raise，
--- 所以显示时显式 Raise 一次，保证"打开即在最前"。后开暴雪面板仍由 YieldToWindow
--- 把矩阵 Lower 到同层最底让位（不再退回 MEDIUM，保持 2026-11-11 的 DIALOG 统一协议）。
local function ActivateMatrix()
    if not frame then return end
    pcall(frame.SetFrameStrata, frame, "DIALOG")
    pcall(frame.Raise, frame)
end

--- 后开的窗口上前：矩阵降到同层最底（保持 DIALOG，不再退回 MEDIUM —— 退 MEDIUM 会和
--- GDL 的 DIALOG 协议打架、层序随机），新窗口 Raise 到它自己那层最上。
--- 只在矩阵真的开着时才动层序，关着的时候不碰暴雪窗口。
local function YieldToWindow(panel)
    if not (frame and frame:IsShown()) then return end
    pcall(frame.Lower, frame)
    if panel and panel.Raise then pcall(panel.Raise, panel) end
end

function Mx.Ensure()
    if frame then return frame end
    -- 帧宽/帧高上限按屏幕算：角色再多也不许比屏幕宽，任务再多也不许比屏幕 85% 高，
    -- 超出部分走滚动（用户实测要求：最大高度也要自适应，不是写死 480）
    local screenW, screenH = 1440, 720
    if UIParent and UIParent.GetWidth then
        local okW, w = pcall(UIParent.GetWidth, UIParent)
        if okW and tonumber(w) and tonumber(w) > 0 then screenW = tonumber(w) end
    end
    if UIParent and UIParent.GetHeight then
        local okH, h = pcall(UIParent.GetHeight, UIParent)
        if okH and tonumber(h) and tonumber(h) > 0 then screenH = tonumber(h) end
    end
    Geom.maxW = math.floor(screenW * Geom.ratioW)
    Geom.maxH = math.floor(screenH * Geom.ratioH)
    frame = CreateFrame("Frame", "GQLMatrixFrame", UIParent, "BackdropTemplate")
    frame:SetSize(820, 480)
    frame:SetPoint("CENTER")
    -- 层序（2026-11-11 统一协议）：本窗与 GDL 流水/日历、GQL 设置/日志窗全部用
    -- DIALOG + SetToplevel。不再默认 MEDIUM、OnShow 临时抬 DIALOG —— 那种"默认 strata
    -- 和对方不一致、靠临时切换"的写法，会和 GDL 的 RaiseFront 互相改写（GDL 把本窗拉回
    -- MEDIUM、本窗 OnShow 又抬 DIALOG），谁在上完全看最后谁的动作生效，随机（用户实测
    -- "大概率在下 / 再点又可能在上面"）。现在常驻 DIALOG：WoW 的 toplevel 保证"谁被点 /
    -- 谁最后开谁在最前"，跨插件层序确定。
    -- 全程 pcall：老客户端没有 ShowUIPanel / 名单帧还没建出来时退回默认层序，
    -- 不许因为抬层失败把整个矩阵建不出来。
    frame:SetFrameStrata("DIALOG")
    -- 原生点击置顶：与 GCL 日历/流水、GL 主窗同为 DIALOG + SetToplevel，
    -- 谁被点/谁最后开谁在最前（老农整合包用的就是这一对）。点一下由 toplevel 自己
    -- 把本窗抬到同 strata 最上，不必再靠手写 Raise 长串逻辑。
    pcall(frame.SetToplevel, frame, true)
    frame:SetScript("OnShow", ActivateMatrix)
    -- 点矩阵任意一处（含拖动）就把它抬回最前：GoldDailyLedger 开窗时会把矩阵
    -- 让回 MEDIUM 并按层号压在自己上面（GCL/Core.lua 的 RaiseFront），矩阵自己不点一下
    -- 就一直压在人家底下。用户 2026-11-10 截图：两窗重叠、分不清谁在上面。
    -- 仍不常驻置顶：只在这一下抬，暴雪后开窗口照样由 YieldToWindow 让回去。
    frame:SetScript("OnMouseDown", ActivateMatrix)
    if type(hooksecurefunc) == "function" and type(_G["ShowUIPanel"]) == "function" then
        pcall(hooksecurefunc, "ShowUIPanel", YieldToWindow)
    end
    for name in pairs(CoverBlock) do
        local gf = _G[name]
        if gf and gf.HookScript then
            pcall(gf.HookScript, gf, "OnShow", YieldToWindow)
        end
    end
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
    frame:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    frame:SetBackdrop({
        -- 不透明深灰：对话框纹理是半透明的，底下的团队框架/BOSS 条会透上来。
        -- 用户实测"不需要半透明，没有意义"，而且透明窗口盖在矩阵上时，
        -- 鼠标在窗口里却把矩阵那行点亮（hover 只看几何、不看遮挡）。
        bgFile = "Interface/Buttons/WHITE8X8",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 16,
        insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })
    pcall(frame.SetBackdropColor, frame, 0.07, 0.07, 0.09, 1)

    local title = frame:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    title:SetPoint("TOPLEFT", frame, "TOPLEFT", 12, -10)
    -- 竖排换锚要用同一个锚点参照物（见 RenderCharsInRows 的进场/出场复位）：
    -- 引用收进 Head，不开顶层 local（upvalue 预算），也不改下面横排建帧的锚参数。
    Head.title = title
    -- 只写插件名（用户 2026-11-09："只显示名称就行了"「任务角色矩阵这些字不用显示了」）：
    -- 标题从 x=12 起往右铺，窄窗口（1~2 列 ⇒ 窗宽落到下限 260）里铺长了就盖到"设置"按钮上。
    -- 版本号仍有设置窗标题、小地图按钮悬停、/gql version 三处可看。
    title:SetText("GoldQuestLedger")

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -6, -6)
    close:SetScript("OnClick", function() frame:Hide() end)
    -- 暴雪的 × 模板自带层号 510（FrameXML Blizzard_SharedXML/Mainline/SharedUIPanelTemplates.xml:134
    -- UIPanelCloseButtonNoScripts 的 frameLevel="510"，UIPanelCloseButton 继承）—— 用户 2026-11-09
    -- 截图：设置窗盖住了矩阵正文、这颗 × 还浮在上面（同层比层号，510 赢了那张窗）。这一句把 × 收回
    -- "和自己窗口一起"，另两处同模板同病同一句（Options.lua 设置窗 ×、LogWindow.lua 日志窗 ×）。
    -- 510 算绝对层号还是相对父帧客户端没答过，这里不写死；也不加 strata 动作（层那条路截图证明是对的）。
    close:SetFrameLevel(frame:GetFrameLevel() + 1)

    -- ESC 关闭（用户实测要求）：帧名 GQLMatrixFrame 是全局名，挂进暴雪名单即可。
    -- 设置窗口挂在 UIParent 上、不是主帧的子帧，所以它自己也要挂一份（见 Options.Build）。
    GQL.Config.RegisterEscapeClose("GQLMatrixFrame")

    -- 停轮询挂在 OnHide 而不是 Toggle 里：ESC 是暴雪直接 Hide 我们的帧，
    -- 根本不经过 Toggle —— 写在 Toggle 里的话，按 ESC 关掉后后台还在抓池
    -- （"平时不抓池"这条是用户实测卡顿后的硬要求）。
    frame:SetScript("OnHide", function()
        if GQL.Sync and GQL.Sync.SetPolling then GQL.Sync.SetPolling(false) end
        -- 面板都关了，挂起的补画定时器就没意义了：撤掉它，"一次性、面板关了
        -- 就不存在"这条才成立（否则关掉后 0.25 秒还会凭空重排一次版面）。
        if paintTimer then
            pcall(function() paintTimer:Cancel() end)
            paintTimer = nil
        end
        HideHiFrame()
    end)

    -- 矩阵上原来那颗"添加任务"2026-11-01 整段下线（用户口径："关键词和地图管理放在
    -- 一个页面里，这样矩阵里的添加任务按钮就不需要了"）—— 它的唯一去处就是设置页的
    -- 关键词段，现在跟着设置一起进。
    --
    -- "设置"：打开我们自己的那一页（跟踪地图 + 关键词 · 屏蔽管理），**不关矩阵**。
    -- 用户 2026-11-01："矩阵里点击设置后，矩阵就被关闭了，这是必须的吗" —— 不是。
    -- 上一版点设置直接开暴雪选项，而暴雪开面板会 CloseSpecialWindows，把挂在 ESC
    -- 名单里的矩阵一并关掉；走 Options.OpenWindow 这两件事都不发生。
    local cfgBtn = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
    cfgBtn:SetSize(72, 22)
    cfgBtn:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -32, -8)
    cfgBtn:SetText("设置")
    cfgBtn:SetScript("OnClick", function()
        -- 判空写在调用之前：Options 没加载（Core 报错中断）时，这颗按钮不该变成一次 Lua 报错
        local open = GQL.Options and (GQL.Options.OpenWindow or GQL.Options.Open)
        if not open then
            if GQL.Config and GQL.Config.Log then
                GQL.Config.Log("Matrix: GQL.Options.OpenWindow 不可用")
            end
            print("GQL: 设置模块没加载，设置打不开（/gql log 看原因）")
            return
        end
        local okCall, err = pcall(open)
        if not okCall then
            if GQL.Config and GQL.Config.Log then
                GQL.Config.Log("Matrix: 设置窗口出错 " .. tostring(err))
            end
            print("GQL: 设置打不开（/gql log 看原因）")
        end
    end)

    -- 左窗格（冻结）：只上下滚，任务名/组头挂它上面。
    -- 顶边往下让出一行（ROW_H）给固定表头条——用户要求"表头不要跟着滚"。
    scrollL = CreateFrame("ScrollFrame", "GQLMatrixScrollL", frame)
    scrollL:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -10 - ROW_H)
    scrollL:SetPoint("BOTTOMRIGHT", frame, "BOTTOMLEFT", NAME_W, 16)
    scrollL:EnableMouse(true)
    scrollL:SetScript("OnMouseWheel", OnMouseWheel)
    contentL = CreateFrame("Frame", "GQLMatrixContentL", scrollL)
    contentL:SetSize(NAME_W, 1)
    contentL:EnableMouse(true)
    contentL:SetScript("OnMouseWheel", OnMouseWheel)
    scrollL:SetScrollChild(contentL)

    -- 右窗格：角色列，两个方向都能滚。真 ScrollFrame + 内容子帧，
    -- 组件自己裁剪自己滚，字体串/热区/高亮全挂内容区上，
    -- 超出可视区绝不会画到帧外（用户实测"还是会溢出"——手动裁剪治标不治本）
    scroll = CreateFrame("ScrollFrame", "GQLMatrixScroll", frame)
    scroll:SetPoint("TOPLEFT", title, "BOTTOMLEFT", NAME_W, -10 - ROW_H)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -16, 16)
    scroll:EnableMouse(true)
    scroll:SetScript("OnMouseWheel", OnMouseWheel)
    content = CreateFrame("Frame", "GQLMatrixContent", scroll)
    content:SetSize(1, 1)
    content:EnableMouse(true)
    content:SetScript("OnMouseWheel", OnMouseWheel)
    scroll:SetScrollChild(content)

    -- 固定表头条：夹在标题行和两个窗格之间的那一行高（顶边 = 窗格让出来的位置，
    -- 底边 = 窗格顶边）。它不是 scrollchild，所以纵向滚它一动不动；
    -- 右边的角色列头挂在 Head.right 上，横向按 offX 平移，跟正文列对齐。
    Head.bar = CreateFrame("Frame", "GQLMatrixHead", frame)
    Head.bar:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -10)
    Head.bar:SetPoint("BOTTOMRIGHT", scroll, "TOPRIGHT", 0, 0)
    -- 鼠标停在表头上也能滚：这条是窗格之外唯一没被滚动区盖住的窄带
    Head.bar:EnableMouse(true)
    Head.bar:SetScript("OnMouseWheel", OnMouseWheel)
    Head.right = CreateFrame("Frame", "GQLMatrixHeadR", Head.bar)
    Head.right:SetPoint("TOPLEFT", Head.bar, "TOPLEFT", NAME_W, 0)
    Head.right:SetPoint("BOTTOMRIGHT", Head.bar, "BOTTOMRIGHT", 0, 0)
    Head.label = Head.bar:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    Head.label:SetJustifyH("LEFT")
    pcall(function()
        local hl = Head.bar:CreateTexture(nil, "BACKGROUND")
        hl:SetColorTexture(1, 1, 1, 0.18)
        hl:SetPoint("BOTTOMLEFT", Head.bar, "BOTTOMLEFT", 0, 0)
        hl:SetPoint("BOTTOMRIGHT", Head.bar, "BOTTOMRIGHT", 0, 0)
        hl:SetHeight(1)
    end)

    -- 横排不画竖线（用户 2026-10-24："最好没有竖条，合并单元格的效果"）：
    -- 角色列之间、表头、任务列与角色列的分界全部去掉，只留逐行淡横线。
    -- （0.6 T8 起竖排在**分组界**画一根，见 Head.Div —— 那是"这一列属于哪张图"的
    -- 落点，与横排这条不冲突；横排路径一行没动。）

    textsL, texts = {}, {}
    BuildScrollbars()
    HookThumb(thumbV, barV, "v")
    HookThumb(thumbH, barH, "h")
    -- 悬停扫描：不依赖 OnEnter/OnLeave（触发顺序不可靠），每帧问 IsMouseOver
    frame:SetScript("OnUpdate", HoverScan)

    frame:Hide()
    return frame
end

--- 只读渲染，返回渲染的任务行数。
--- 表头固定在滚动区之外的一行里：左格"任务"，右格角色名（按职业上色，
--- 当前角色那格加背景淡条），横滚跟着列走、纵滚不动；正文按地图分组，
--- 组头金色；任务行只有名字，颜色表示剩余时间（蓝>1天 / 绿>1小时 / 红<1小时），
--- 全员完成的置灰。
function Mx.Render()
    -- 重绘计数挂在模块表（不是 db）：内存里数一次，零接口调用，
    -- /gql 与单测都靠它看清"到底重排了几次版面"（合并生效与否的直接证据）。
    GQL.Matrix.renderCount = (GQL.Matrix.renderCount or 0) + 1
    local f = Mx.Ensure()
    usedBtns, usedRowBtns, usedLines = 0, 0, 0
    -- 三处文字池本轮从头复用（池子按字体分桶，见 Pool 的说明）
    Pool.Begin(textsL)
    Pool.Begin(texts)
    Pool.Begin(Head.pool)
    local db = GQL.Config.DB() or {}
    local now = (GQL.Api and GQL.Api.WallClock())
        or math.floor((GQL.Api and GQL.Api.Uptime()) or 0)
    -- 画版面要问的全在显示层：准入门与排序仍是 Ledger 给的（groups 与拆层前同一份顺序），
    -- 这里只拿"这组画不画、画哪几行"。判据边界写在 MatrixView.lua 文件头。
    local view = GQL.MatrixView
    local groups = view.Groups(db, now)
    -- 当前号一轮只问这一次：既给列序优先用（Ledger 自己不碰接口），也给下面的
    -- 底色/表头高亮用；横排竖排共用它（11-09 定的口径）。
    local okCur, curKey = pcall(function()
        return GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey()
    end)
    local cols = GQL.Ledger.MatrixColumns(db, now, curKey)
    -- 0.6 T6 分派（计划 T6"分派只加一行"）：竖排开关开着就把整轮交给
    -- Mx.RenderCharsInRows，下面横排代码一行都不再执行。groups/cols 传的就是
    -- 上面这两份现成结果 —— 竖排不许再问一遍 view（每组跟踪清单那轮客户端答复
    -- 已经在 groups 里），也不许再算一次列（矩阵列与 Ledger 同源同序）。
    -- 序里含"当前号优先"，真源仍在 Ledger。
    if GQL.Config and GQL.Config.CharsInRows and GQL.Config.CharsInRows() == true then
        return Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)
    end
    -- 分派的另一半（计划"分派只加一行"的两处必要偏差之一）：开关关着时先收掉
    -- **上一次竖排**留在表头里的悬停热区。它们只属于竖排；横排正文代码本轮一字未改
    -- （计划纪律），清理只能长在分派处。
    -- 横排常态下池是空的，这个循环空转，版面逐字不变（用例
    -- chars_in_rows_off_keeps_horizontal_identical 钉"关着=现状"）。
    Head.btnUsed = 0
    for i = 1, #Head.btns do Head.btns[i]:Hide() end
    -- 组界竖线同理：两截都只属于竖排，横排的分组界是组头下面那条横线（现状不动）。
    Head.divUsed = 0
    for i = 1, #Head.bDiv do Head.bDiv[i]:Hide() end
    for i = 1, #Head.hDiv do Head.hDiv[i]:Hide() end
    -- 同样只属于竖排的残留：窗格顶边。竖排那轮把顶边让到 2 行表头下面
    -- （Head.bar 的底边跟着 scroll 的顶边锚，所以让顶边就是加高表头条），
    -- 它自己**不许**在末尾还原 —— 还原了就是"表头条只有一行高、第二行字溢到正文上"。
    -- 记号在 Head.anchorV：横排常态一个字段都不摸，只有真从竖排回来才重摆一次锚。
    if Head.anchorV then
        scrollL:ClearAllPoints()
        scrollL:SetPoint("TOPLEFT", Head.title or frame, "BOTTOMLEFT", 0, -10 - ROW_H)
        scrollL:SetPoint("BOTTOMRIGHT", frame, "BOTTOMLEFT", NAME_W, 16)
        scroll:ClearAllPoints()
        scroll:SetPoint("TOPLEFT", Head.title or frame, "BOTTOMLEFT", NAME_W, -10 - ROW_H)
        scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -16, 16)
        Head.anchorV = nil
    end
    local rowCount = 0
    -- 帧宽：自然宽度优先（紧凑，不留大块空白），下限 260，上限屏幕 75%
    -- （超了走横向滚动，不把弹窗拉得比屏幕还宽）。
    -- 余量取 40 = 右窗格占用（NAME_W+28）之外再留 12px 空隙：原来只留 24，
    -- 实际右窗格占 NAME_W+28，导致每列全展开时正好差 4px → 假的横向溢出。
    frameW = math.min(math.max(260, NAME_W + COL_W * #cols + 40), Geom.maxW)
    -- 帧高按内容算（紧凑，尽量不出纵向滚动条），上限是屏幕高的 85%：
    -- 超过上限的内容照旧滚，不把弹窗拉成通天柱
    -- 正文高度（表头条移出滚动区，不再算进内容高）。上限按屏幕高 85% 折算成
    -- 可放的正文高：放得下就按内容收高（不出滚动条），放不下才滚。
    -- 某个地图组真正要画的行 = groups[gi].rows（关注地图过滤 + 玩家点名豁免、
    -- 以及 0.6 T5 的"隐藏全部完成"都只在 MatrixView 那一处判（Groups 内部那一趟），
    -- 返回 nil 表示整组不画；因隐藏开关被掏空的组还带 allHiddenDone 标记，
    -- 末尾空态那一句靠它认凶手）。
    -- 写在 MatrixView 而不是这里的函数体内：0.6 的两个显示开关都落在这一层，留在 Render 里就等于往准入门加判据。
    -- 正文高按像素累加，不是"行数 × ROW_H"：地图分组行比任务行高
    -- 估算与绘制共用 view.Groups 那一份结果（以前两处各调一次 VisibleRows，
    -- 等于每组向客户端问两遍跟踪清单，还会让"帧高按 A 算、版面按 B 画"有可乘之机）。
    local estBodyH = 0
    for gi = 1, #groups do
        local vr = groups[gi].rows
        if vr then estBodyH = estBodyH + GROUP_H + #vr * ROW_H end
    end
    if #groups == 0 then estBodyH = estBodyH + ROW_H end
    if #cols == 0 then estBodyH = estBodyH + ROW_H end
    local availBody = Geom.maxH - Geom.padV - ROW_H
    if availBody < 120 then availBody = 120 end
    frameH = Geom.padV + ROW_H + math.min(estBodyH, availBody)
    if frameH < 200 then frameH = 200 end
    pcall(f.SetSize, f, frameW, frameH)
    -- 右窗格内容宽 = 角色列全展开 + 8px 右留白（测试与渲染都依赖这个自然宽）。
    -- 关键：这 8px 只是"内容里的留白"，判断要不要横向滚动时会被下面的 visW 吸收掉，
    -- 不会变成假的横向溢出。
    contentW = COL_W * #cols + 8
    -- 右窗格实际宽度是确定值：frameW - (12 左内边距 + NAME_W) - 16 右内边距 = frameW-NAME_W-28。
    -- 不要用 scroll:GetWidth()：SetSize 后布局未必同步刷新，取到的是上一轮旧宽，会算出假的
    -- 横向溢出（这也是"明明不需要滚动却有滚动条"的根因之一）。直接按 frameW 算才稳。
    local sh = (scroll and scroll.GetHeight) and scroll:GetHeight() or 0
    -- rawVisW 是"没被抬到 1 的"真实可视宽：下面的裁剪窗口只信它。
    -- visW 那行 <1 就顶到 1 是为了不让滚动条算出除零，可把 1px 的窗口当成可信视口，
    -- 裁出来的就是"满屏一个对象都没有"这种最难查的空版面。
    local rawVisW = frameW - NAME_W - 28
    visW = rawVisW < 1 and 1 or rawVisW
    -- 可视高同理按帧高反推（表头条占一行）：取到实测高就用实测高，取不到用这个兜底
    visH = (type(sh) == "number" and sh > 0) and sh or (frameH - Geom.padV - ROW_H)
    -- 画列头之前夹 offX（contentW/visW 本轮已经算好）：offX 越界时整排列头
    -- 都会被判定成"滑出可视区"而藏掉。
    local headMaxX = math.max(0, contentW - visW)
    if offX > headMaxX then offX = headMaxX end
    if offX < 0 then offX = 0 end
    -- offY 同样要在进绘制循环之前夹，但用的必须是本轮算出的 estBodyH，不是上一轮的
    -- contentH —— 老坑的根因是"拿旧内容高提前夹"，不是"提前夹"这件事本身。
    -- 不夹就咬人：下面的裁剪窗口按 offY 决定摆哪些行，按旧的、偏大的 offY 摆完才在
    -- 末尾被夹小 → 窗口下沿那几行本轮压根没有对象，下一轮才补上，
    -- 表现就是"滚动错位/首格被吞"（用户实测过的老坑）。
    -- 末尾那次 ClampOffsets 原样留着：y 照常走完全部行，contentH == estBodyH，它退化成幂等。
    local bodyMaxY = math.max(0, estBodyH - visH)
    if offY > bodyMaxY then offY = bodyMaxY end
    if offY < 0 then offY = 0 end
    -- 可见窗口（上下左右各让出 overscanRows 行的缓冲：滚动时边缘不许先露白边再补画）。
    -- 视口取不到数（不是数字或 ≤0，布局未就绪的帧、离线替身都算）→ 整轮不裁，
    -- 走全量路径：宁可多摆对象，也绝不"视口不知道就把行藏了"。
    local clip, winTop, winBot, winLeft, winRight
    if type(visH) == "number" and visH > 0 and type(rawVisW) == "number" and rawVisW > 0 then
        clip = true
        local ov = Geom.overscanRows * ROW_H
        winTop, winBot = offY - ov, offY + visH + ov
        winLeft, winRight = offX - ov, offX + visW + ov
    end

    -- 表头画在固定条上：左格"任务"钉死，右格角色名按 offX 平移（列头随横滚走、
    -- 纵滚不动）。正文那行不再是表头，y 从 0 开始。
    if Head.label then
        Head.label:SetText("任务")
        Head.label:ClearAllPoints()
        Head.label:SetPoint("TOPLEFT", Head.bar, "TOPLEFT", 0, 0)
        Head.label:Show()
    end
    local curInHead = false
    for i = 1, #cols do
        local c = cols[i]
        local isCur = okCur and curKey ~= nil and tostring(c.key) == tostring(curKey)
        local cx = (i - 1) * COL_W               -- 正文里的列原点（内容坐标）
        if isCur then ShowCurCharHeader(cx, c.class) end
        -- 列头：角色名最多取前 4 个字，超出直接截掉、不加省略号（用户要求）。
        -- "◀" 标记删掉（部分字体渲染成方块，用户实测）——当前角色已有整列背景高亮。
        -- 过期角色：名字压到 2 字 + "过期"，保证 72px 窄列里放得下。
        local nm = ShortName(ShortCharKey(c.key))
        local label = c.stale and (ShortName(ShortCharKey(c.key), 2) .. "过期") or nm
        Head.Put(cx - offX + COL_W / 2, Hex(label, ClassColor(c.class)), "GameFontNormalSmall")
        if isCur then
            curInHead = true
            Head.ShowCur(cx - offX, c.class, true)
        end
    end
    if not curInHead then Head.ShowCur(0, nil, false) end
    Pool.End(Head.pool)
    local y = 0

    for gi = 1, #groups do
        local g = groups[gi]
        -- 关注地图过滤 + 关键词/日志/家族豁免都在 MatrixView 那一处（和上面估行数是同一份结果）
        local rows = g.rows
        if rows and #rows > 0 then
            -- 整组（组头 + 这一组所有行）和竖窗不相交 → 组头不摆（连带省掉那一次地图名）。
            -- y 与 rowCount 都照常推进：内容高、滚动条长度、当前角色整列底色一律按全量算。
            if not clip or (y + GROUP_H + #rows * ROW_H >= winTop and y <= winBot) then
                -- 组头那句字由显示层给（措辞口径与"算哪一组"的判断分别写在
                -- MatrixView.GroupLabel / Ledger.GroupKind；"未知地图""周常"这两类
                -- 天生没有地图名，不进 MapName 那一问）。
                -- 地图名行只要名字：不要下三角、不要"合计多少金"（用户实测要求），
                -- 用金色字体把组头和任务行区分开
                local label = view.GroupLabel(g.mapID)
                -- 分组行：金色大字 + 比任务行高一截（用户 2026-10-24："分组行高稍微高一些"）
                PutL(0, y + (GROUP_H - ROW_H) / 2,
                    Hex(FitLeftText(label, NAME_W - 8), "ffd100"), FONT_GROUP)
                RowBorder(y, 0.22, GROUP_H)   -- 组头下划线，比行线亮，分组一眼分清
            end
            y = y + GROUP_H
            for ri = 1, #rows do
                local row = rows[ri]
                -- rowCount 数的是"本轮画出的行数"这个口径（/gql 和用例都读它），
                -- 不是"本轮摆了几个对象"：裁剪只砍对象，它照旧走全量。
                -- 0.6 T5：开了"隐藏全部完成"后它变小是**对的**——它就是画出来的行数；
                -- "上板多少条"（准入门给了几条）一律看 /gql quest，两个口径不许混为一谈。
                rowCount = rowCount + 1
                if not clip or (y + ROW_H >= winTop and y <= winBot) then
                    -- 整行悬停热区（左窗格，盖满任务列宽）；带 row，悬停提示与批量右键都读它
                    RowHoverButton(y, row)
                    -- 任务名：颜色 = 剩余时间；全员完成置灰。用 Highlight 字体（带描边阴影）
                    -- 让浅色名字在深色背板上更跳、更"显眼"（用户实测"任务不明显"）
                    local name = row.name or ("世界任务#" .. tostring(row.questID))
                    local color = row.allDone and COLOR_GRAY or TimeColor(row.remaining)
                    -- 带"高频"标签时名字再让出一段，别叠在一起
                    PutL(0, y,
                        Hex(FitLeftText(name, row.highFreq and NAME_W - 56 or NAME_W - 8),
                            color),
                        FONT_ROW)
                    if row.highFreq then
                        PutL(NAME_W - 48, y, Hex("高频", "ff9d5c"))
                    end
                    for ci = 1, #cols do
                        local cx = (ci - 1) * COL_W
                        -- 列的可见判据和列头那侧（Head.ShowCur 按 visW 藏滑出去的列）
                        -- 同一条：滑出横窗的列既不摆文字也不摆热区，
                        -- 于是悬停扫描遍历的热区数跟着降到"可见行 × 可见列"。
                        if not clip or (cx + COL_W >= winLeft and cx <= winRight) then
                            -- 单元格内容居中（用户要求）；勾/空/横线都压在格子正中
                            local ckey = cols[ci].key
                            local st = row.state[ckey]
                            -- 勾与"在做"走贴图（CellMark），其余是文字记号（? / — / ×）。
                            -- 两者互斥：贴图那两档不摆文字，免得同一格出现两种记号。
                            if st ~= "done" and st ~= "progress" then
                                local glyph = STATE_TEXT[st] or STATE_FALLBACK
                                if glyph ~= "" then
                                    PutCenter(cx + COL_W / 2, y + ROW_H / 2, glyph, MARK_FONT)
                                end
                            end
                            -- 格子可点：左键手动切换完成、右键切换"这一号本轮不做"
                            local btn = CellButton(cx, y, ckey, row.questID)
                            -- 全员完成（row.allDone，名字已置灰）时勾一起压暗
                            CellMark(btn, st, st == "done" and row.src and row.src[ckey] == "manual",
                                row.allDone)
                        end
                    end
                    RowBorder(y)          -- 行底淡线，逐行分隔（网页表格风）
                end
                y = y + ROW_H
            end
        end
    end

    -- 0.6 T5 空态补句（计划十.2 已定，2026-11-09）：整轮一条都没画、且凶手是
    -- "隐藏全部完成"（view 给的 allHiddenDone 标记）时给一句（都完成了）。
    -- 为什么必须是新增判据而不是改上面那句占位文案：今天整组被显示层滤空时
    -- #groups 照样 > 0，那句"没有够格的任务"根本不触发，画出来是一片空白
    -- （v2 说"会写那句加关键词"是错的，计划附录 2 已翻案）。
    -- 凶手只认标记、不在这里重算 allDone/配置：关注地图过滤造成的整组空白
    -- **保持现状**（一片空白，不触发这句），两种空白的原因与出路不同，不许混。
    -- 帧高不涨：这句话不往 estBodyH 加任何东西（空轮 estBodyH=0，帧高仍是 200 下限）。
    if rowCount == 0 then
        for gi = 1, #groups do
            if groups[gi].allHiddenDone then
                PutL(0, y, "（都完成了）")
                y = y + ROW_H
                break
            end
        end
    end
    if #groups == 0 then
        -- 空态只指一条当场做得到的动作。旧句里那三层一起撤下：
        -- "默认只列本资料片地图的"是准入口径（取证），写死的"≥100 金币"是错的
        -- （门槛是配置项、默认 500，`Ledger.MatrixMoneyFloor`），
        -- "调低门槛"这半句本来就不该写 —— 门槛控件 2026-10-27 第 32 轮整段从设置面板撤下，
        -- UI 里改不到，只能改存档 db.config.minGold（见 Options.lua 文件头）。
        PutL(0, y, "（没有够格的任务：想多列就加关键词）")
        y = y + ROW_H
    end
    if #cols == 0 then
        -- 同样只留玩家要做的动作（登录一次）；"任何角色同步过"那半句是取证说法。
        PutL(0, y, "（还没有角色数据：任意角色登录一次就会出现）")
        y = y + ROW_H
    end

    Pool.End(textsL)
    Pool.End(texts)
    -- 内容尺寸灌进两个 ScrollFrame（高度画完才知道），再应用当前偏移：
    -- 组件按内容/可视区之比裁剪滚动，字体串绝不会画到帧外
    contentH = math.max(y, 1)
    contentL:SetSize(NAME_W, contentH)
    content:SetSize(contentW, contentH)
    -- 当前角色整列背景拉满整列高（表头那格只是它的最上面一段）
    if curHi then
        curHi:ClearAllPoints()
        curHi:SetPoint("TOPLEFT", content, "TOPLEFT", curHi.__cx or 0, 0)
        curHi:SetSize(COL_W, contentH)
        curHi:Show()
    end
    ClampOffsets()
    pcall(scrollL.SetHorizontalScroll, scrollL, 0)   -- 左窗格永不横滚
    pcall(scrollL.SetVerticalScroll, scrollL, offY)
    pcall(scroll.SetHorizontalScroll, scroll, offX)
    pcall(scroll.SetVerticalScroll, scroll, offY)
    -- 多余点击热区收起来，别堆在上面挡视线
    for i = usedBtns + 1, #cellBtns do cellBtns[i]:Hide() end
    for i = usedRowBtns + 1, #rowBtns do rowBtns[i]:Hide() end
    -- 多余分隔线收起来
    for i = usedLines + 1, #rowLines do rowLines[i]:Hide() end
    for i = usedLines + 1, #rowLinesL do rowLinesL[i]:Hide() end
    -- 高亮跟着重绘走：挂在各自内容区上，坐标就是原始行坐标；
    -- 滑出可视区时 ScrollFrame 会自己裁掉，不用手动藏
    -- 缓存的热区先作废：按钮池本轮被重新分配给了别的行，不作废的话
    -- "行没变就别重画"会把 tooltip 一直停在错的任务上
    if hiL then hiL.__btn = nil end
    if hiRow and hiL and hiL:IsShown() then
        hiL:ClearAllPoints()
        hiL:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, -hiRow)
        hiL:SetSize(NAME_W, ROW_H)
        hiR:ClearAllPoints()
        hiR:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -hiRow)
        hiR:SetSize(math.max(contentW, 1), ROW_H)
    end
    UpdateScrollbars()
    Mx.rowCount = rowCount
    return rowCount
end

--- 0.6 T6：角色竖排（任务横排）。开关 = Config.CharsInRows（T4 字段），唯一调用点是
--- Mx.Render 里那一句分派；groups/cols 传的就是 Render 已经拿到的那两份现成结果 ——
--- 本函数不许再问一遍 view（每组向客户端的那轮跟踪清单答复已经在 groups 里），
--- 不许再算一次列，更不许重排（列序 = 组序 × 组内行序，排序真源始终在 Ledger）。
--- 序里含"当前号优先"，真源仍在 Ledger。
--- 版面（计划 docs/tasks/2026-11-08-0.6-matrix-layout.md T6 的表格）：
---   左窗格（冻结、只跟纵滚）= 角色名：ShortName 4 字、职业色；过期角色写成
---     2 字 + "过期"，与横排列头同一条规则（计划点 7：竖排时过期标记落在左栏）。
---   右窗格（横滚 + 纵滚）= 任务状态格：格子的记号口径、点击语义、全员完成压暗
---     全部与横排同一批助手（CellButton/CellMark/STATE_TEXT），换轴不改口径。
---   表头 = 两行：上一行分组（同图相邻列合并单元格：只写一次图名、金色大字，组界
---     一根竖线把"这一列属于哪张图"划清楚，见 Head.Div），下一行任务名（ShortName 4 字，
---     超 4 字直接截掉**不加省略号**，与角色列头同一规则；颜色 = 剩余时间
---     蓝>1天/绿>1小时/红<1小时/奶白=问不出，全员完成压灰 —— 换轴不改口径）。
--- 取舍（计划点 8，后面别当 bug 补回来）："高频"标签在 72px 表头格里和 4 字名字
---   放不下两样，竖排**不画**；这条信息改由表头悬停给（Head.Button 那池热区）。
--- 列宽 = 72 直接复用 COL_W（计划十.1 已定），不新增宽度状态、不给 CellButton 加宽度入参。
--- 当前角色标记三条路全部换轴、但不新建第二块底色帧（计划点 4）：
---   表头那格 → Head.cur 挪到左窗格表头（-NAME_W 相对 Head.right，尺寸 NAME_W × 两行表头）；
---   正文底色 → curHi 从"整列"变"整行"（同一块帧，只改挂点与尺寸）；
---   末尾拉满整列 → 竖排不需要（行高就是 ROW_H，循环里一次摆好）。
--- 空态三句的落位（计划点 9）：全部画在**左窗格末行之后** —— "没有够格的任务"与
---   "（都完成了）"说的是任务轴，竖排里任务轴在表头/右窗格，而这两句出现时右窗格
---   本来 0 列没处落，左窗格末尾就是与横排"正文之后补一行"同一语义的位置；
---   "还没有角色数据"说的是行轴，左窗格就是行轴本身。文案与横排逐字相同。
--- 几何：表头 = 2 行，两个窗格顶边让 2*ROW_H（进场换锚、出场复位横排锚 ——
---   横排的渲染代码本轮一字不许改，"记得摆回去"只能长在竖排的出口；开关关着时
---   下一轮横排 Render 读到的锚钉与 Ensure 建帧时逐字相同，帧高公式里那条
---   "表头=1 行"的假设因此继续只对横排成立，见 Geom.padV 那段注释）。
function Mx.RenderCharsInRows(db, now, groups, cols, okCur, curKey)
    local f = Mx.Ensure()
    local title = Head.title or frame
    Head.btnUsed = 0
    Head.divUsed = 0
    -- 进场：竖排锚（让 2 行表头）。Ensure 里 scrollL/scroll 各两条锚（TOPLEFT + BOTTOMRIGHT），
    -- ClearAllPoints 会一起抹掉，所以两条都要按 Ensure 的原参数重摆，只改 TOPLEFT 那条的 y。
    scrollL:ClearAllPoints()
    scrollL:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -10 - 2 * ROW_H)
    scrollL:SetPoint("BOTTOMRIGHT", frame, "BOTTOMLEFT", NAME_W, 16)
    scroll:ClearAllPoints()
    scroll:SetPoint("TOPLEFT", title, "BOTTOMLEFT", NAME_W, -10 - 2 * ROW_H)
    scroll:SetPoint("BOTTOMRIGHT", frame, "BOTTOMRIGHT", -16, 16)
    -- 记号交给横排分派处去复位（见 Mx.Render 的 Head.anchorV 那一段）：竖排轮次末尾
    -- 不许还原，还原了就是"表头条按一行高锚、第二行字溢到正文上"。
    Head.anchorV = true

    -- 摊平任务列：列序 = 组序 × 组内行序（view 已按 Ledger 的组序/行序给好）。
    -- gFirst[gi]/gN[gi] 记每组的起始列号与列数，供表头分组行合并单元格用；
    -- rows=nil 的组没有列（整组不画的判据在 MatrixView，这里只承接）。
    local qcols, gFirst, gN = {}, {}, {}
    for gi = 1, #groups do
        local rows = groups[gi].rows
        if rows and #rows > 0 then
            gFirst[gi] = #qcols + 1
            gN[gi] = #rows
            for ri = 1, #rows do
                qcols[#qcols + 1] = rows[ri]
            end
        end
    end
    local nQ = #qcols
    local rowCount = nQ      -- Mx.rowCount 的口径**不因换轴而变**：本轮画出的任务条数。
                             -- 两种排布它必须相等（计划点⑤，用例直接钉这条）。

    local curI
    if okCur and curKey ~= nil then
        for i = 1, #cols do
            if tostring(cols[i].key) == tostring(curKey) then
                curI = i
                break
            end
        end
    end

    -- 帧宽：自然宽优先，下限 260，上限屏幕 75%（超出走横滚）—— 与横排同一条公式，
    -- 只是"列数"换成任务列数（列宽同为 COL_W=72，计划十.1 已定复用，不新开状态）。
    frameW = math.min(math.max(260, NAME_W + COL_W * nQ + 40), Geom.maxW)
    -- 帧高：正文 = 角色行 × ROW_H；表头 = 2 行（横排那条 1 行假设属于横排路径）。
    -- 空态占位各加一行（形状与横排的 estBodyH 完全对着：组空一句、列空一句）。
    local estBodyH = #cols * ROW_H
    if #groups == 0 then estBodyH = estBodyH + ROW_H end
    if #cols == 0 then estBodyH = estBodyH + ROW_H end
    local availBody = Geom.maxH - Geom.padV - 2 * ROW_H
    if availBody < 120 then availBody = 120 end
    frameH = Geom.padV + 2 * ROW_H + math.min(estBodyH, availBody)
    if frameH < 200 then frameH = 200 end
    pcall(f.SetSize, f, frameW, frameH)
    -- 右窗格内容宽 = 任务列全展开 + 8px 右留白（与横排同一条自然宽口径，
    -- 那 8px 会被下面的 visW 吸收，不造成假的横向溢出）。
    contentW = COL_W * nQ + 8
    local sh = (scroll and scroll.GetHeight) and scroll:GetHeight() or 0
    local rawVisW = frameW - NAME_W - 28
    visW = rawVisW < 1 and 1 or rawVisW
    -- 可视高：横排兜底公式加的是 1 行表头，竖排加 2 行（计划点 2 点名这处换算）。
    visH = (type(sh) == "number" and sh > 0) and sh or (frameH - Geom.padV - 2 * ROW_H)
    -- 绘制前先夹偏移（横排同一条"用本轮新尺寸夹"的纪律，防"滚动错位/首格被吞"）。
    local headMaxX = math.max(0, contentW - visW)
    if offX > headMaxX then offX = headMaxX end
    if offX < 0 then offX = 0 end
    local bodyMaxY = math.max(0, estBodyH - visH)
    if offY > bodyMaxY then offY = bodyMaxY end
    if offY < 0 then offY = 0 end
    local clip, winTop, winBot, winLeft, winRight
    if type(visH) == "number" and visH > 0 and type(rawVisW) == "number" and rawVisW > 0 then
        clip = true
        local ov = Geom.overscanRows * ROW_H
        winTop, winBot = offY - ov, offY + visH + ov
        winLeft, winRight = offX - ov, offX + visW + ov
    end

    -- 表头左格：横排钉"任务"，竖排钉"角色"（同一颗 Head.label，只换文案与语义，
    -- 两行表头下贴顶放，正对左窗格的角色名列）。
    if Head.label then
        Head.label:SetText("角色")
        Head.label:ClearAllPoints()
        Head.label:SetPoint("TOPLEFT", Head.bar, "TOPLEFT", 0, 0)
        Head.label:Show()
    end

    -- 表头第一行：分组名。同图相邻列**合并成一个单元**：一组只写一次图名
    -- （组天然连续 —— view 的组序就是列序的分段），金色大字。
    -- Api.MapName 只在"这一组至少有一列落在可见窗口"时才问
    -- （横排是"组真要画才问"，竖排按横窗裁同样的门；缓冲列（overscan）不该多问）。
    -- 组界各画一根竖线（0.6 T8，用户 2026-11-09："分组之间没有明确的分割线，
    -- 想知道这个任务属于哪个地图一眼看不出"；替掉 2026-10-24 那句"不画竖线"——
    -- 合并单元格的效果他要，只是光靠合并数不出归属）。第一个组不画：它的左沿就是
    -- 窗格边，再画一根是双线。竖线不受上面那道横窗门管，两截各有各的裁法（见 Head.Div）。
    for gi = 1, #groups do
        if gFirst[gi] then
            local x0 = (gFirst[gi] - 1) * COL_W
            local wGroup = gN[gi] * COL_W
            if x0 > 0 then Head.Div(x0, x0 - offX) end
            if not clip or (x0 + wGroup >= offX and x0 <= offX + visW) then
                local label = GQL.MatrixView.GroupLabel(groups[gi].mapID)
                Head.Put(x0 + wGroup / 2 - offX,
                    Hex(FitLeftText(label, math.max(COL_W - 8, wGroup - 8)), "ffd100"),
                    FONT_GROUP)
            end
        end
    end

    -- 表头第二行：任务名。ShortName(name, 4) —— 超 4 字直接截掉、不加省略号（计划点 3，
    -- 与角色列头同一条规则；横排正文那条 FitLeftText 的省略号在这里**不许**用）。
    -- 颜色 = TimeColor(row.remaining)，全员完成压 COLOR_GRAY（换轴不改口径）。
    -- 取舍（计划点 8）：这一格不画"高频"标签 —— 72px 放不下一枚标签加 4 个字；
    -- 这条信息改由这一列的表头悬停给出（Head.Button 那池热区，行级金额口径）。
    for ci = 1, nQ do
        local row = qcols[ci]
        local cx = (ci - 1) * COL_W
        local nm = ShortName(row.name or ("世界任务#" .. tostring(row.questID)), 4)
        local color = row.allDone and COLOR_GRAY or TimeColor(row.remaining)
        Head.Put(cx - offX + COL_W / 2, Hex(nm, color),
            "GameFontNormalSmall", -ROW_H - ROW_H / 2)
        Head.Button(cx - offX, row)
    end

    -- 表头那格的当前角色底色（三条路之一，计划点 4）：Head.cur 复用同一块帧，
    -- 挪到左窗格表头 —— 相对 Head.right 往回偏 NAME_W，尺寸盖满左表头格（两行高）。
    -- 不是当前角色/当前号不在列：走横排末尾同款 ShowCur(false) 把它藏起来。
    if curI then
        Head.ShowCur(-NAME_W, cols[curI].class, true, NAME_W, 2 * ROW_H)
    else
        Head.ShowCur(0, nil, false)
    end

    local y = 0
    local curPainted = false
    for i = 1, #cols do
        local c = cols[i]
        -- rowCount 走的是"任务条数"口径（nQ），这里只数**摆出来的对象**；
        -- 行裁剪与横排同一条判据（± overscan 缓冲）。
        if not clip or (y + ROW_H >= winTop and y <= winBot) then
            -- 左窗格：角色名整条写出，不再砍 4 个字（用户 2026-11-11；120px 放得下
            -- 10 个中文字，而角色名上限 7 个字 ⇒ 连省略号这道护栏都不用加）。
            -- 「过期」那档同样全名打头。横排列头（Head.Put 那一处）照旧砍 4 字 ——
            -- 那是 72px 的窄列，两处不是同一个几何，别顺手对齐。
            local nm = ShortCharKey(c.key)
            local label = c.stale and (nm .. "过期") or nm
            PutL(0, y, Hex(label, ClassColor(c.class)), "GameFontNormalSmall")
            -- 整行悬停热区：不属于任何一条任务（questID 传 nil）——
            -- 悬停只亮行、不出金额提示（计划点 6 的分岔判据就在 ShowRowHighlight
            -- 那句 `btn.__questID` 上，今天的行为，不用新开关）。
            RowHoverButton(y, nil)
            for ci = 1, nQ do
                local cx = (ci - 1) * COL_W
                if not clip or (cx + COL_W >= winLeft and cx <= winRight) then
                    local row = qcols[ci]
                    local ckey = c.key
                    local st = row.state[ckey]
                    -- 记号口径逐字照横排：勾/在做走贴图，? / — / × 走文字，空格不摆
                    if st ~= "done" and st ~= "progress" then
                        local glyph = STATE_TEXT[st] or STATE_FALLBACK
                        if glyph ~= "" then
                            PutCenter(cx + COL_W / 2, y + ROW_H / 2, glyph, MARK_FONT)
                        end
                    end
                    -- 点击目标 (角色, 任务) 随坐标换轴一起搬过去：CellButton 的
                    -- charKey/questID 是两个入参，摆哪个方向都是同一笔记账。
                    local btn = CellButton(cx, y, ckey, row.questID)
                    CellMark(btn, st, st == "done" and row.src and row.src[ckey] == "manual",
                        row.allDone)
                end
            end
            -- 行线：RowBorder 的宽度判据是 contentW（计划点 5）—— 本函数在它之前
            -- 已经把 contentW 换成了"任务列全宽"，左右两段照样拼成一条横线、不画半屏。
            RowBorder(y)
            if i == curI then
                -- 当前角色底色（三条路之二、之三，计划点 4）：整列 → 整行。
                -- **复用 curHi 那一块帧**（ShowCurCharHeader 建的，不新建第二块），
                -- 只改挂点与尺寸；"末尾拉满整列高"在竖排没有对应物（行高就是 ROW_H），
                -- 所以在这里一次摆好、末尾不再拉。
                ShowCurCharHeader(0, c.class)
                curHi:ClearAllPoints()
                curHi:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -y)
                curHi:SetSize(math.max(contentW, 1), ROW_H)
                curHi:Show()
                curPainted = true
            end
        end
        y = y + ROW_H
    end
    -- 当前号不在列 / 它那一行被裁出去：底色帧不许留着上一轮的位置亮着（不是新建，
    -- 是把**同一块** curHi 藏起来；下一轮回横排时末尾那段会照常重摆它）。
    if curHi and not curPainted then curHi:Hide() end

    -- 空态三句（计划点 9，落位说明在函数头注释）：画在左窗格、角色行之后，
    -- 文案与横排逐字相同（取证措辞纪律照旧，界面只给动作不给原因）。
    if rowCount == 0 then
        for gi = 1, #groups do
            if groups[gi].allHiddenDone then
                PutL(0, y, "（都完成了）")
                y = y + ROW_H
                break
            end
        end
    end
    if #groups == 0 then
        PutL(0, y, "（没有够格的任务：想多列就加关键词）")
        y = y + ROW_H
    end
    if #cols == 0 then
        PutL(0, y, "（还没有角色数据：任意角色登录一次就会出现）")
        y = y + ROW_H
    end

    Pool.End(textsL)
    Pool.End(texts)
    Pool.End(Head.pool)
    contentH = math.max(y, 1)
    contentL:SetSize(NAME_W, contentH)
    content:SetSize(contentW, contentH)
    ClampOffsets()
    pcall(scrollL.SetHorizontalScroll, scrollL, 0)   -- 左窗格永不横滚（与横排同一条）
    pcall(scrollL.SetVerticalScroll, scrollL, offY)
    pcall(scroll.SetHorizontalScroll, scroll, offX)
    pcall(scroll.SetVerticalScroll, scroll, offY)
    -- 多余热区收起来（计划点 6 的第二处）：三个池一起清 —— 状态格、整行、表头。
    for i = usedBtns + 1, #cellBtns do cellBtns[i]:Hide() end
    for i = usedRowBtns + 1, #rowBtns do rowBtns[i]:Hide() end
    for i = Head.btnUsed + 1, #Head.btns do Head.btns[i]:Hide() end
    -- 组界线两截一起收：组数变少时留下的那根是**假的分组界**
    -- （玩家会照着它去数哪几列属于同一张图），比少画一根严重。
    for i = Head.divUsed + 1, #Head.bDiv do Head.bDiv[i]:Hide() end
    for i = Head.divUsed + 1, #Head.hDiv do Head.hDiv[i]:Hide() end
    for i = usedLines + 1, #rowLines do rowLines[i]:Hide() end
    for i = usedLines + 1, #rowLinesL do rowLinesL[i]:Hide() end
    if hiL then hiL.__btn = nil end
    if hiRow and hiL and hiL:IsShown() then
        -- 高亮跟着重绘走：hiRow 是角色行的 y，横竖两排它是同一个"行坐标"，
        -- 只是 hiR 的宽度口径 = contentW 已经指向任务列全宽（左+右仍拼成整行）。
        hiL:ClearAllPoints()
        hiL:SetPoint("TOPLEFT", contentL, "TOPLEFT", 0, -hiRow)
        hiL:SetSize(NAME_W, ROW_H)
        hiR:ClearAllPoints()
        hiR:SetPoint("TOPLEFT", content, "TOPLEFT", 0, -hiRow)
        hiR:SetSize(math.max(contentW, 1), ROW_H)
    end
    UpdateScrollbars()
    -- 锚**不在这里还原**：表头条的高度是跟着 scroll 顶边锚出来的，竖排这一轮结束时
    -- 版面就是"2 行表头"的样子，锚必须留在 2 行的位置；下一次横排 Render 会在分派处
    -- 按 Ensure 建帧的原参重摆回去（Head.anchorV 那段）。
    Mx.rowCount = rowCount
    return rowCount
end

function Mx.IsOpen()
    return frame ~= nil and frame:IsShown() == true
end

--- 主帧本身。设置/关键词窗口打开时要先让矩阵退回 MEDIUM、再按层号把窗口抬上去
--- （见 Options.RaiseAboveMatrix）—— 这是那两处唯一需要的窗口引用，
--- 别让它们去摸 _G.GQLMatrixFrame：12.x 用 CreateFrame 传的名字不再注册成全局。
function Mx.Frame()
    return frame
end

--- "添加任务"窗口 2026-10-27 搬进 Options 的关键词段；2026-11-01 起关键词与地图管理
--- 合成一页，矩阵上那颗按钮整个下线（用户口径："这样矩阵里的添加任务按钮就不需要了"）。
--- 旧弹框 Mx.OpenAddDialog / Mx.RefreshAddList 与 Mx.listed、"矩阵已有/已加/已忽略"
--- 那套标签更早就下线了。用户口径的三条实测事实（地图 pin 装了画环插件就钩不到、
--- 金额 nil ≠ 0 金币只显 "?"、点一行只是撤销屏蔽不承诺重新上板）随注释带进
--- Options.lua 的页面段，这里不再重复一份。

GQL.RegisterCommand("matrix", function()
    Mx.Toggle()
end, "打开/关闭 任务×角色 矩阵")

--- 立刻重排版面（同步）：这是所有重绘的出口，只在这里记时间戳。
--- 计数在 Mx.Render 里做（那才是真正的排版出口，被 Track/Options 直接调时也要数），
--- 这里只负责"补画前先撤掉挂起的定时器、把 lastPaint 推到当下"。
function Mx.RefreshNow()
    if paintTimer then
        pcall(function() paintTimer:Cancel() end)
        paintTimer = nil
    end
    lastPaint = (GQL.Api and GQL.Api.Uptime()) or 0
    Mx.Render()
end

function Mx.Refresh()
    if not (frame and frame:IsShown()) then return end
    local now = (GQL.Api and GQL.Api.Uptime()) or 0
    -- 距离上次排版已超过合并窗口 → 立刻同步画（用户点刷新后要马上看到新数据）
    if now - lastPaint >= COALESCE_SECONDS then
        Mx.RefreshNow()
        return
    end
    if paintTimer then return end          -- 这一批已经排了补画
    -- 窗口内的重复请求只在结尾补画一次；定时器一次性，不重复、不轮询
    paintTimer = C_Timer.NewTimer(COALESCE_SECONDS, function()
        paintTimer = nil
        if frame and frame:IsShown() then Mx.RefreshNow() end
    end)
end

function Mx.Toggle()
    local f = Mx.Ensure()
    if f:IsShown() then
        -- 停轮询由 OnHide 统一负责（ESC 关闭也走那里），这里不再重复一遍
        f:Hide()
    else
        offX, offY = 0, 0
        -- 【11-04 定稿】先 Show 再抓池：Mx.Refresh() 有 frame:IsShown() 门，
        -- 打开那一趟里 HuntKeywordMaps / LogBypass 触发的重画以前全被丢掉，
        -- 玩家实测"刚打开矩阵没更新、要等一会儿才刷新"的另一半就是这个。
        -- OnShow 只做幂等的抬层，换顺序不触发额外副作用；同步首绘仍在最后，
        -- lastPaint 不会被提前推进，零接口、不新增定时器。
        f:Show()
        -- 再显式抬一次层：不依赖客户端是否派发 OnShow（离线替身就不派发），
        -- "先开其它窗口再开矩阵"这条口径必须每次打开都成立。
        ActivateMatrix()
        -- 先开轮询：这样接下来若真抓了快照，"最早到期时刻"的唤醒定时器才排得上
        if GQL.Sync and GQL.Sync.SetPolling then GQL.Sync.SetPolling(true) end
        -- 打开即抓一次池（用户 2026-11-01："打开矩阵的时候最好能自动刷新，而不是手动刷新"）。
        -- 标题栏那颗"刷新"按钮 11-04 下线，这里就是唯一的手工刷新入口。
        -- 代价照实：每开一次面板多一次池读取（12.x 是按图问的那条，几十张图）。
        -- 奖励金额是另一条异步链路、没有到达事件，所以"点开就是现在的板面"不等于
        -- "点开就看到全部金额" —— 这段窗口抓没抓到池，凭据在 /gql quest 和日志里。
        if GQL.Sync and GQL.Sync.SnapshotNow then
            local okSnap, err = pcall(GQL.Sync.SnapshotNow)
            if not okSnap then GQL.Config.Log("开面板抓池出错: " .. tostring(err)) end
        end
        -- 首绘保持同步（走 RefreshNow 而不是 Refresh：开面板要立刻看到新数据），
        -- 同时把 lastPaint 推到当下 —— 否则紧随其后的那批 Refresh 会以为"很久没画过"，
        -- 又同步重排一次，合并就白做了。
        Mx.RefreshNow()
    end
end

--- 小地图图标按钮：很多插件的做法，挂在 Minimap 上，永远可见、不依赖世界地图内部帧。
--- 默认贴在地图环上某个角度，可拖动换位置，角度记进 SavedVariables（GQLDB.minimapAngle）。
local MINIMAP_DEFAULT_ANGLE = 200   -- 默认角度（度），约在右下方
local BTN_SIZE = 20                 -- 环上按钮边长（金币图标铺满；用户嫌 32 太大）

--- 小地图宿主：EnsureMapButton 用 Api.Resolve("Minimap") 拿到后存这儿。
--- 放环/拖动帧只读这个上值，不许再直连全局 Minimap：宿主被暴雪改名/挪走时
--- （12.x 有过先例），EnsureMapButton 判降级看的是 host，读的时候换成全局
--- 等于绕过那道判断——按钮一旦建好，拖动照样能踩到不存在的全局当场抛错。
local minimapHost

local function MinimapAngle()
    local db = GQL.Config.DB()
    local a = db and tonumber(db.minimapAngle)
    return a or MINIMAP_DEFAULT_ANGLE
end

--- 把图标放到小地图环上：圆心取 Minimap 中心，半径 = 半宽 + 图标半边，
--- 这样图标正好骑在地图边缘，不会盖住地图内容也不会被盖住。
local function PlaceOnMinimapRing(btn)
    local host = minimapHost
    if not host then return end   -- 没宿主没处可放：EnsureMapButton 早已降级，这里只是兜底
    -- 全程 pcall + "报不出就退回 160"：宿主是登录早期抓的引用，方法问不到就用默认环宽
    local okW, w = pcall(host.GetWidth, host)
    local radius = ((okW and tonumber(w) and tonumber(w) > 0 and tonumber(w)) or 160) / 2
        + (btn:GetWidth() or 32) / 2 - 4
    local angle = math.rad(MinimapAngle())
    btn:ClearAllPoints()
    pcall(btn.SetPoint, btn, "CENTER", host, "CENTER", math.cos(angle) * radius, math.sin(angle) * radius)
end

--- 小地图图标：自带素材（任务清单+绿勾）+ 悬停提示，点击开/关矩阵。宿主取不到（Minimap 缺失，
--- 12.x 理论上不会）就记降级：矩阵仍有 /gql matrix。日志必须留痕，否则用户只会看到
--- "怎么没有按钮"而查不出原因。
function Mx.EnsureMapButton()
    if button then return button end
    local host = GQL.Api and GQL.Api.Resolve("Minimap")
    -- 只判真假不判 type：帧对象由引擎造，Lua 看到的类型不保证是 table
    -- （Api.FrameVisible 在 12.1.5 就因为这条回退失效过，同一个坑不踩第二次）
    if not host then
        GQL.Config.Log("地图按钮降级：Minimap 不可用（矩阵改用 /gql matrix）")
        return nil
    end
    button = CreateFrame("Button", "GQLMapButton", host)
    minimapHost = host      -- 放环/拖动帧读的就是这个上值，不再碰全局 Minimap（见上方注释）
    button:SetSize(BTN_SIZE, BTN_SIZE)
    -- 子帧跟随 Minimap 的 strata 即可；小地图按钮标准做法是抬一点 frame level 防被其它环上图标盖住。
    button:SetFrameLevel((pcall(host.GetFrameLevel, host) and host:GetFrameLevel() or 1) + 5)
    -- 图标用插件自带的 GoldQuestLedger.tga（任务清单+绿勾）。暴雪素材两颗都被否过：
    -- 金币和记账插件 GoldLedger 撞脸，任务日志书本 BtWQuests 那族在用（用户 2026-11-10）。
    -- 追踪环当边框 12.1.5 实测画成"方块图标+左上角一个小圆环"，素材排版没法离线验；
    -- 用户明确"如果不好做成圆形，方块的也行，就这么大" —— 就按方块做，尺寸照截图。
    button:SetNormalTexture("Interface\\AddOns\\GoldQuestLedger\\GoldQuestLedger")
    -- 高亮用暴雪的圆形外圈（方形素材当高亮会在按下时又变回方块）
    button:SetHighlightTexture("Interface\\Buttons\\UI-Common-MouseHilight")
    button:SetScript("OnClick", function() Mx.Toggle() end)
    button:SetScript("OnEnter", function(self)
        if GameTooltip then
            GameTooltip:SetOwner(self, "ANCHOR_LEFT")
            GameTooltip:SetText("世界任务金币矩阵")
            GameTooltip:AddLine("点击打开 任务×角色 矩阵", 0.8, 0.8, 0.8, true)
            -- 版本号：共享出去以后对方第一句话往往是"我这是哪个版本"，
            -- 悬停就能念出来（真源仍是 Core 的 GQL.VERSION）
            if GQL.VERSION then
                GameTooltip:AddLine("v" .. tostring(GQL.VERSION), 0.5, 0.5, 0.5)
            end
            GameTooltip:Show()
        end
    end)
    button:SetScript("OnLeave", function()
        if GameTooltip then GameTooltip:Hide() end
    end)
    -- 拖动换位置：沿小地图环移动，角度记进账本。
    button:RegisterForDrag("LeftButton")
    button:SetScript("OnDragStart", function(self)
        self:SetScript("OnUpdate", function()
            -- 宿主一律用上值 host，不直连全局 Minimap：EnsureMapButton 判降级用的
            -- 就是 Api.Resolve("Minimap")，读的时候换成全局等于绕过那道判断。
            local okC, cx, cy = pcall(host.GetCenter, host)
            if not okC then return end
            -- 上一版是 pcall(...GetEffectiveScale, ...) and Minimap.GetEffectiveScale()：
            -- 后半点调用没带 self，pcall 成功时它照样当场抛错，而且这句不在任何保护里。
            local okS, s = pcall(host.GetEffectiveScale, host)
            local scale = (okS and tonumber(s) and tonumber(s) > 0) and tonumber(s) or 1
            local okP, px, py = pcall(GetCursorPosition)
            if not okP then return end
            -- atan2 在真客户端（Lua 5.1）有；离线 lupa 用的是新版 Lua 已删掉它，
            -- 两参 math.atan(y,x) 是同名同义形态，取先有的那个，两边都算得出角度。
            local atan2 = math.atan2 or math.atan
            local angle = atan2((py / scale) - cy, (px / scale) - cx)
            local db = GQL.Config.DB()
            if db then db.minimapAngle = math.deg(angle) end
            PlaceOnMinimapRing(self)
        end)
    end)
    button:SetScript("OnDragStop", function(self)
        self:SetScript("OnUpdate", nil)
        GQL.Config.Log(("小地图按钮角度=%s"):format(tostring(MinimapAngle())))
    end)
    PlaceOnMinimapRing(button)
    button:Show()
    GQL.Config.Log("地图按钮已建（Minimap 子帧）")
    return button
end

--- 探针用：按钮"没建"还是"建了但看不见"修法完全不同，报告必须跟真实状态走。
--- 全程 pcall：替身环境的帧没有 GetName/GetFrameLevel 这些方法，真客户端都有。
function Mx.ButtonInfo()
    if not button then return "未建" end
    local parent = button:GetParent()
    local pname = "?"
    if parent then
        local ok, n = pcall(parent.GetName, parent)
        pname = tostring((ok and n) and n or "?")
    end
    local okL, lvl = pcall(button.GetFrameLevel, button)
    -- GetNormalTexture 返回的是**纹理对象**不是路径字符串，直接塞进 %s 会把整份
    -- probe 报告炸掉（12.1.5 实测：bad argument #3 to 'format' (string expected, got table)），
    -- 路径要再问一次 :GetTexture()
    local texPath = "?"
    local okT, tex = pcall(button.GetNormalTexture, button)
    if okT and tex then
        local okP, p = pcall(tex.GetTexture, tex)
        texPath = tostring((okP and p) and p or "纹理对象（路径取不到）")
    end
    return ("已建 父=%s 层级=%s 纹理=%s"):format(
        pname, tostring(okL and lvl or "?"), texPath)
end
