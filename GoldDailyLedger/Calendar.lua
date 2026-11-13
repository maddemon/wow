-- 日历界面（T7）：左边月历、右边角色净值列表，流水另开一扇弹窗（版式照计划 §7.2）。
-- 下半区（四档按钮 + 号列表 + 分类小计 + 内嵌流水页）整块拆掉：两页共用槽位就是截图里
-- 那片叠字的根。格子只留两行（日号 / 净），收·支只在悬停里给；点右栏整行开 GDL.Flows。
--
-- 三条纪律，都有用例钉着（tests/run_gcl_tests.py 的 test_calendar_*）：
--   1. 只在"开窗"和"点击"时算：这个文件里没有每帧脚本、没有定时器、不订任何事件，
--      读数全部走 Ledger 的查表接口（计划 §3 末条）。
--   2. 界面上只放玩家这一眼要用的东西；凡是"我们怎么知道的"这类证据一律进 /gdl probe ui。
--   3. 组件只用本仓库已在用户 12.1.5 客户端上跑通的写法：
--        窗口   = GoldQuestLedger/Matrix.lua:1548-1585（CreateFrame + BackdropTemplate + 拖动 + 不透明深灰）
--        关闭   = GoldQuestLedger/Matrix.lua:1597-1605（UIPanelCloseButton 的 × 自带 frameLevel=510，必须钉回主帧+1）
--        按钮   = GoldQuestLedger/Matrix.lua:1633-1636（UIPanelButtonTemplate + SetText + OnClick）
--        热区   = GoldQuestLedger/Matrix.lua:358-385（Button + RegisterForClicks，池化复用）
--        提示   = 自绘 TOOLTIP strata 不透明小框（第 5 轮）；OnEnter/OnLeave + pcall 的形状照 GQL Matrix.lua:481-700
--        滚动   = AHSnipe/SnipeList.lua:354-360 + GoldQuestLedger/Matrix.lua:1546（行按钮也挂 OnMouseWheel）
--        ESC    = GoldQuestLedger/Config.lua:272-283（UISpecialFrames 挂的是帧的**全局名**）
--        金额   = GoldQuestLedger/Config.lua:60-86（|T 转义必须三段式 路径:高:宽|t，两段会原样印成文本）
--      一行都不拷，只照同一形状调用。

local GDL = _G.GDL
local Calendar = {}

if GDL and GDL.RegisterModule then
    GDL.RegisterModule("Calendar", Calendar)
else
    GDL = GDL or {}
    GDL.Calendar = Calendar
end

---------------------------------------------------------------------------
-- 几何常量：双栏一扇窗（计划 §7.2 的尺寸表）。格子/字号仍以真机 /gdl probe ui
-- 量回来的宽度为准；除此之外这是唯一一处版面尺寸，界面各处只从这里取值。
-- 版式段落之外不许再出现裸像素常量（test_calendar_static_guards 扫全文）。
---------------------------------------------------------------------------

local GEOM = {
    PAD           = 12,  -- 窗内四周留白
    TITLE_H       = 22,  -- 标题行
    HELP_BTN      = 18,  -- 标题旁那颗 ?（反馈入口）
    HELP_GAP      = 6,   -- ? 距标题右沿
    NAV_H         = 26,  -- < 月份 > 导航行（读数已移到日历下方，用户 2026-11-10）
    WD_H          = 18,  -- 星期表头
    CELL_W        = 88,  -- 左栏 7 列
    CELL_H        = 44,  -- 一格两行字：日号 / 净（用户 2026-11-10：格子再高一点）
    GRID_ROWS     = 6,   -- 6 行 × 7 列 = 42 格（月历最长形态）

    -- 右栏加宽：× 锚在窗右上角，原先「本月」那一格右沿会压到它（用户截图）。
    -- 加宽的是右栏（PANEL_X 不变），所以顶部三格原地不动、× 往右让开。
    PANEL_W       = 264,
    PANEL_GAP     = 12,  -- 左右栏之间的间隔
    PANEL_PRESET_H = 30, -- 右栏顶部页签那一行（页签高 + 到列头的留白）
    PANEL_HEAD_H  = 20,  -- 号名 / 净 列头
    ROW_H         = 20,  -- 右栏一行
    SCROLL_W      = 22,  -- UIPanelScrollFrameTemplate 自带滚动条占的宽

    -- 三档改成页签（用户 2026-11-13：三颗普通按钮看不出当前是哪一档）。
    -- 左沿与下面的号列表对齐（x=0），右沿停在滚动条那一列之前；
    -- 三格排完的右沿仍要离右上角那颗 × 有余量（× 按 32 宽算，左沿在窗内 878）。
    PRESET_W      = 76,  -- 一个页签
    PRESET_GAP    = 2,   -- 页签之间露出窗底当分隔
    TAB_H         = 26,  -- 选中页签的高
    TAB_LIP       = 4,   -- 未选中的矮这么多；三格底沿共用一条基线
    TAB_RULE_H    = 1,   -- 页签基线
    TAB_MARK_H    = 2,   -- 选中页签底沿那条金线（压住基线，看着与下面的列表连成一片）
    BTN_H         = 22,  -- 通用按钮高
    MO_BTN_W      = 22,  -- < >
    MO_BTN_GAP    = 2,
    MO_LABEL_W    = 96,  -- 「2026年11月」这一段留给多宽
    MO_LABEL_GAP  = 6,
    -- 日历下方那条读数带：横线 + 六个等宽方块（本周 / 上周 / 本月 / 全部 / 战团 / 总计）。
    -- 方块贴着排、靠 1px 竖线分隔。读数带贯穿整个窗底（用户 2026-11-11）。
    SUMMARY_H       = 52, -- 读数带总高（含横线上下的留白）
    SUMMARY_DIV_GAP = 6,  -- 横线距网格底
    BLOCK_PAD       = 10, -- 文字上沿距横线：不紧贴分割线
    BLOCK_TEXT_X    = 8,  -- 文字距方块左边
    CLOSE_INSET   = 6,   -- × 距窗角
    TIP_W         = 176, -- 自绘悬停提示框宽
    TIP_ROW_H     = 16,  -- 提示每行同高
    TIP_PAD       = 6,   -- 提示框内上下留白

    -- 反馈网址弹窗（标题旁 ? 的落点）：一扇独立小窗，装一个全选的 EditBox。
    FEEDBACK_W      = 460,
    FEEDBACK_H      = 158,
    FEEDBACK_GAP    = 8,   -- 提示行与输入框之间的距离
    FEEDBACK_EDIT_H = 24,  -- 输入框宿主高
    FEEDBACK_BTN_W  = 110, -- 复制 / 关闭两颗按钮宽

    -- 数字一律右对齐，用 RIGHT/TOPRIGHT 锚，不给宽度约束：字体串没定宽时
    -- SetJustifyH 在客户端不起作用，右锚才是本仓库量得准的做法。
    COLS = { name = 4, net = 4 },
}
GEOM.GRID_W       = GEOM.CELL_W * 7
GEOM.PANEL_X      = GEOM.PAD + GEOM.GRID_W + GEOM.PANEL_GAP
GEOM.CONTENT_W    = GEOM.PANEL_W - GEOM.SCROLL_W
GEOM.PRESET_STRIP_W = 3 * GEOM.PRESET_W + 2 * GEOM.PRESET_GAP
GEOM.WIN_W        = GEOM.PAD + GEOM.GRID_W + GEOM.PANEL_GAP + GEOM.PANEL_W + GEOM.PAD
GEOM.SUMMARY_TOP  = GEOM.PAD + GEOM.TITLE_H + GEOM.NAV_H + GEOM.WD_H
    + GEOM.GRID_ROWS * GEOM.CELL_H
GEOM.WIN_H        = GEOM.SUMMARY_TOP + GEOM.SUMMARY_H + GEOM.PAD
-- 高度预算（Matrix.lua:1535-1537 那条真机要求）：不许比屏幕 85% 还高（720 档 ⇒ 612）。
GEOM.MAX_WIN_H    = math.floor(720 * 0.85)
GEOM.SUMMARY_W    = GEOM.WIN_W - 2 * GEOM.PAD
-- 六个方块等分整条读数带；最后一块吸收除不尽的 4px，右沿才能贴住窗内边。
GEOM.BAND_W       = math.floor(GEOM.SUMMARY_W / 6)
GEOM.BAND_LAST_W  = GEOM.SUMMARY_W - 5 * GEOM.BAND_W
GEOM.BAND_GAP     = 0                        -- 贴着排，靠竖线分格
GEOM.TOTALS_LEFT  = GEOM.PAD
-- 右栏到日历网格底部为止：窗底整条让给读数带，角色列表滚动区相应缩短（用户 2026-11-11）。
GEOM.PANEL_H      = GEOM.SUMMARY_TOP - GEOM.PAD
GEOM.PANEL_LIST_H = GEOM.PANEL_H - GEOM.PANEL_PRESET_H - GEOM.PANEL_HEAD_H
GEOM.PANEL_ROWS   = math.floor(GEOM.PANEL_LIST_H / GEOM.ROW_H)
Calendar.GEOM = GEOM

local COLS = GEOM.COLS

-- 日历下方六个读数方块（用户 2026-11-11：原四档 + 战团 + 总计）。
-- 「上周」是上一整个周 CD，不与「本周至今」共用范围算法的结果；
-- 后两块不是范围档，走金币快照读数，不进 TOTALS_PRESETS。
local TOTALS_PRESETS = { "week", "lastweek", "month", "all" }
local SUMMARY_BLOCKS = { "week", "lastweek", "month", "all", "warband", "assets" }
-- 右栏顶部三个页签，顺序就是从左到右；「全部」只是下方读数，不进这里。
local PANEL_PRESETS = { "day", "week", "month" }
local NO_VALUE = "—" -- 读不到就说读不到，和真正的 0 分开

--- 只有这四个方块是时间范围，能点开流水；战团 / 总计是资产快照，点了也没有"范围"可筛。
local function IsRangeBlock(preset)
    for _, p in ipairs(TOTALS_PRESETS) do if p == preset then return true end end
    return false
end

---------------------------------------------------------------------------
-- 颜色（净：正绿负红；计划 §3 草图）
---------------------------------------------------------------------------

local C_POS = { 0.24, 0.78, 0.32 }
local C_NEG = { 0.86, 0.28, 0.26 }
local C_ZERO = { 0.62, 0.62, 0.62 }
-- 资产读数（战团 / 总计）不是收支，用中性亮色，别被绿/红误导。
local C_ASSET = { 0.95, 0.93, 0.86 }
-- 角色名一律走 Ledger.CharLabel 的职业色；当前号只靠"置顶"区分，不再盖一层琥珀。
local C_DIM = { 0.62, 0.62, 0.62 }

-- 右栏三档页签：选中 = 暖底 + 金字 + 底沿金线（与"选中那天"同一族暖色，金币读数也是这一族），
-- 未选中 = 冷灰底 + 灰字。鼠标停在未选中的那格上只亮底与字，绝不冒充选中态。
local C_TAB_ON     = { 1.00, 0.82, 0.00 }
local C_TAB_OFF    = C_DIM
local C_TAB_HOVER  = { 0.88, 0.88, 0.92 }
local TAB_BG_ON    = { 0.17, 0.15, 0.09 }
local TAB_BG_OFF   = { 0.10, 0.10, 0.13 }
local TAB_BG_HOVER = { 0.14, 0.14, 0.18 }
-- 无 edgeFile 的纯底色背板（与月历格子 CELL_BG_NORMAL 同一形状，只把内缩改成 0：页签要铺满）。
local TAB_BACKDROP = { bgFile = "Interface/Buttons/WHITE8X8", edgeSize = 16,
                       insets = { left = 0, right = 0, top = 0, bottom = 0 } }

local FONT_TITLE = "GameFontHighlight"       -- Matrix.lua:1587
local FONT_LABEL = "GameFontNormal"          -- Options.lua:343
local FONT_BODY  = "GameFontHighlightSmall"  -- Matrix.lua:1695
local FONT_SMALL = "GameFontNormalSmall"     -- LogWindow.lua:23
-- 格子里的日号 / 净用与悬停提示正文同一量级的小字（用户 2026-11-10：
-- "日历里面的数字可以小一点，和鼠标提示里的数字一样大小"）。右栏行与顶栏仍用 FONT_BODY。
local FONT_CELL  = "GameFontNormalSmall"
-- 悬停提示四行全用这一个字体对象：GameTooltip 的第一行走 header、后续走正文，
-- 收入/支出天生一大一小；自己的框就没有这个分叉。
local FONT_TIP   = "GameFontNormalSmall"

---------------------------------------------------------------------------
-- 文本与数值格式
---------------------------------------------------------------------------

--- 取文本：Locale 没加载（toc 顺序被改 / Core 报错中断）时原样回键，界面不许因为缺串炸。
local function T(key)
    local loc = GDL and GDL.Locale
    if type(loc) == "table" and type(loc.Get) == "function" then
        local okCall, r = pcall(loc.Get, key)
        if okCall and type(r) == "string" then return r end
    end
    return tostring(key)
end

--- 取文本并代入参数：占位符与语言表不匹配时（改串的人数错格）只回原文，绝不抛错。
local function Fmt(key, ...)
    local s = T(key)
    if select("#", ...) == 0 then return s end
    local okCall, r = pcall(string.format, s, ...)
    if okCall and type(r) == "string" then return r end
    return s
end

-- |T 转义必须写成 路径:高:宽|t 三段：只写高不写宽客户端不认，会把整串当普通文本原样印出来
-- （用户在截图里撞过，见 GoldQuestLedger/Config.lua:66-73）。
local ICON_GOLD   = "|TInterface\\MoneyFrame\\UI-GoldIcon:12:12|t"
local ICON_SILVER = "|TInterface\\MoneyFrame\\UI-SilverIcon:12:12|t"
local ICON_COPPER = "|TInterface\\MoneyFrame\\UI-CopperIcon:12:12|t"

--- 只显示金币（紧凑位与所有悬停共用，用户 2026-11-11 定）：整数金，银/铜尾数直接丢掉，
--- 不足 1 金显示 0 金。大额走「万 / 亿」压缩（52.8万🪙 / 1.05亿🪙），
--- 万/亿以内的小数最多两位、末尾 0 不写；超过 100 万/亿取整，免得方块摆不下。
--- 非数字回 "?"：与 0（真没有钱）严格区分；-0 不写负号。
local function TrimDecimal(whole, frac, digits)
    if digits <= 0 then return tostring(whole) end
    local s = tostring(frac)
    while #s < digits do s = "0" .. s end
    s = s:sub(1, digits):gsub("0+$", "")
    if s == "" then return tostring(whole) end
    return whole .. "." .. s
end

local function CompactGold(gold)
    if gold >= 100000000 then
        local whole, rem = math.floor(gold / 100000000), gold % 100000000
        local digits = whole >= 100 and 0 or (whole >= 10 and 1 or 2)
        local frac = digits > 0 and math.floor(rem / (10 ^ (8 - digits))) or 0
        return TrimDecimal(whole, frac, digits) .. "亿"
    elseif gold >= 10000 then
        local whole, rem = math.floor(gold / 10000), gold % 10000
        local digits = whole >= 100 and 0 or (whole >= 10 and 1 or 2)
        local frac = digits > 0 and math.floor(rem / (10 ^ (4 - digits))) or 0
        return TrimDecimal(whole, frac, digits) .. "万"
    end
    return tostring(gold)
end

function Calendar.FormatMoneyIcon(copper)
    if type(copper) ~= "number" then return "?" end
    local gold = math.floor(math.abs(copper) / 10000)
    local sign = (copper < 0 and gold > 0) and "-" or ""
    return sign .. CompactGold(gold) .. ICON_GOLD
end

--- 全量 金/银/铜（**只给流水明细行**，用户 2026-11-11 定：只有流水显示银/铜，
--- 悬停一律只写金币）：高位为 0 跳过该段（2113金 33银 44铜 / 50银 / -6银 78铜），
--- 全 0 显示 0金，符号挂整串最前。
function Calendar.FormatMoneyFull(copper)
    if type(copper) ~= "number" then return "?" end
    local a = math.floor(math.abs(copper))
    local g, s, c = math.floor(a / 10000), math.floor(a / 100) % 100, a % 100
    local parts = {}
    if g > 0 or (s == 0 and c == 0) then parts[#parts + 1] = g .. ICON_GOLD end
    if s > 0 then parts[#parts + 1] = s .. ICON_SILVER end
    if c > 0 then parts[#parts + 1] = c .. ICON_COPPER end
    local sign = (copper < 0 and a > 0) and "-" or ""
    return sign .. table.concat(parts, " ")
end

--- 文字截断：subject / 号名都可能比列位长，宁可省略尾巴也不许盖到下一列去。
--- maxBytes 是**字节**预算（号名/服名是 UTF-8），截在半字符上客户端会显示乱码方块 ⇒ 退到整字符。
local function Clip(s, maxBytes)
    if type(s) ~= "string" then return "" end
    if #s <= maxBytes then return s end
    local cut = maxBytes
    while cut > 0 do
        local b = s:byte(cut + 1)
        if not b or b < 0x80 or b >= 0xC0 then break end   -- 0x80..0xBF 是后续字节，退回去
        cut = cut - 1
    end
    return s:sub(1, cut) .. "…"
end

---------------------------------------------------------------------------
-- 日期：只用 Ledger 已证的读数（DateKey / MonthKey / DaysInMonth / WeekdayOfDateKey /
-- TodayKey / WallClock），**不换算时间戳、不碰客户端的 date/time**：
-- 键是定宽 ISO，字典序就是时间序，所以纯按"这个月有几天"往前后推就够。
---------------------------------------------------------------------------

local function LedgerOK(name)
    local l = GDL and GDL.Ledger
    return type(l) == "table" and type(l[name]) == "function"
end

local function LCall(name, ...)
    if not LedgerOK(name) then return nil end
    local l = GDL.Ledger
    local okCall, r = pcall(l[name], ...)
    if not okCall then return nil end
    return r
end

--- 角色名统一走账本：去服名 + 职业色。账本缺席（用例会卸掉 Ledger）时退回
--- 纯文本短名，绝不把整串 "名字-服务器" 摆到界面上。
--- 必须放在 LCall **之后**：Lua 的 local 只被它后面的函数捕获，放前面会去摸一个
--- 不存在的全局 LCall（这一轮真踩过，界面整屏空白、错误只进 state.errors）。
local function CharLabel(charKey, maxBytes)
    local lab = LCall("CharLabel", charKey, maxBytes)
    if type(lab) == "string" and lab ~= "" then return lab end
    local short = LCall("ShortName", charKey)
    return Clip(tostring(short or charKey or ""), maxBytes or 24)
end

--- 把可能越界的 (年,月,日) 归一成 ISO 键：月末日之后就是下月 1 日，1 日之前就是上月末日。
--- 步数上限是护栏：DaysInMonth 答不出来（nil）时立刻收手，绝不在这死循环。
function Calendar.KeyOf(year, month, day)
    year, month, day = tonumber(year), tonumber(month), tonumber(day)
    if not year or not month or not day then return nil end
    for _ = 1, 5000 do
        if month < 1 then month = month + 12 ; year = year - 1
        elseif month > 12 then month = month - 12 ; year = year + 1
        elseif day < 1 then
            local pm, py = month - 1, year
            if pm < 1 then pm = 12 ; py = py - 1 end
            local pd = LCall("DaysInMonth", py, pm)
            if not pd then return nil end
            day = day + pd ; month = pm ; year = py
        else
            local dim = LCall("DaysInMonth", year, month)
            if not dim then return nil end
            if day <= dim then return ("%04d-%02d-%02d"):format(year, month, day) end
            day = day - dim ; month = month + 1
            if month > 12 then month = 1 ; year = year + 1 end
        end
    end
    return nil
end

--- ISO 键 → 年, 月, 日（纯字符串拆，不问任何接口）。
local function PartsOf(key)
    if type(key) ~= "string" then return nil end
    local y, m, d = key:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
    if not y then return nil end
    return tonumber(y), tonumber(m), tonumber(d)
end

---------------------------------------------------------------------------
-- 状态（只在开窗与点击时改；没有任何东西在这里"自己动"）
-- 位置纪律：这一段必须在**任何**用到 state 的函数之前 —— Lua 的 local 只被它
-- 之后的函数捕获，写晚了那些函数会去摸一个不存在的全局（首次调用即抛错）。
---------------------------------------------------------------------------

local ALL_START, ALL_END = "0000-01-01", "9999-12-31"   -- 「全部」：定宽 ISO 键的两端哨兵

local state = {
    year = nil, month = nil,          -- 月历显示的那个月
    preset = "day",                   -- 右栏三档：day / week / month（顶栏的 all 只当读数）
    startKey = nil, endKey = nil,     -- 右栏当前范围（含端点），锚在 selKey
    selKey = nil,                     -- 选中的那一天（开窗默认今天）
    panelCount = 0, scrollY = 0,      -- 右栏列表条数 / 自己记的滚动偏移
    weekCD = { source = "none", seconds = nil, reason = nil },
    tip = { lines = nil, error = nil, shown = false },
    errors = {},
}
Calendar.GetState = function() return state end

local frame, ui, built
-- 反馈网址弹窗（标题旁 ? 的落点）：第一次点才建，和主窗同一套层序协议。
local feedbackFrame
Calendar.GetUI = function() return ui end
--- 给流水弹窗抬层用：12.x 里 addon 帧名不再注册进 _G（Matrix.lua:1150-1152），
--- 所以跨窗口取帧只能走模块引用，不能查 _G.GDLCalFrame。
Calendar.GetFrame = function() return frame end

local function NoteError(what, err)
    state.errors[#state.errors + 1] = tostring(what) .. ": " .. tostring(err)
    while #state.errors > 20 do table.remove(state.errors, 1) end
end

---------------------------------------------------------------------------
-- 周 CD 起点（计划 §3：周 CD 只当读数和范围预设，不做第二个网格）
---------------------------------------------------------------------------

local WEEK_SECONDS = 604800
-- 接口存在性证据：GoldQuestLedger/Api.lua:1408-1423 用的就是它，而且 12.1.5 真机回传
-- 里 AVAILABLE C_DateAndTime.GetSecondsUntilWeeklyReset -> 511085（docs/log.txt:19）。
local WEEK_RESET_API = "C_DateAndTime.GetSecondsUntilWeeklyReset"

--- 周口径倒计时：调用形状照 Api.lua:1413-1423（判存在 → pcall → 只接受 (0, 一周] 的数）。
--- @return number|nil 倒计时秒数, string 说明（"接口答了什么"，只进 probe，不上界面）
function Calendar.WeekResetCountdown()
    local tbl = _G.C_DateAndTime
    if type(tbl) ~= "table" or type(tbl.GetSecondsUntilWeeklyReset) ~= "function" then
        return nil, WEEK_RESET_API .. " 不存在"
    end
    local okCall, v = pcall(tbl.GetSecondsUntilWeeklyReset)
    local n = okCall and tonumber(v) or nil
    if n and n > 0 and n <= WEEK_SECONDS then return n, WEEK_RESET_API end
    return nil, WEEK_RESET_API .. " 答的不是周口径（回 " .. tostring(v) .. "）"
end

--- 本周 CD 的起止键（含端点）。国服周重置在周四；接口问不出就退回**日历周周一**，
--- 并把原因只记在这里（界面不加字，取证在 /gdl probe ui）。
--- @return string|nil startKey, string|nil endKey
function Calendar.WeekCDRange()
    local now = LCall("WallClock")
    local today = LCall("TodayKey")
    if not now or not today then
        state.weekCD = { source = "none", seconds = nil, reason = "墙上时钟读不出（TodayKey 为空）" }
        return nil, nil
    end
    local cd, why = Calendar.WeekResetCountdown()
    if cd then
        local startKey = LCall("DateKey", now + cd - WEEK_SECONDS)
        if startKey then
            state.weekCD = { source = why, seconds = cd, reason = nil }
            return startKey, today
        end
        why = why .. " 但 DateKey 读不出锚点"
    end
    -- 退化：按日历周（周一为一周首日）。wday 用 Ledger 的算法，不自己再算一遍星期。
    local wday = LCall("WeekdayOfDateKey", today)
    local y, m, d = PartsOf(today)
    if not wday or not y then
        state.weekCD = { source = "none", seconds = nil, reason = why .. "；日历周也推不出" }
        return nil, nil
    end
    local back = (wday + 5) % 7     -- 周一=0 天：周日(wday=1) 回退 6 天
    state.weekCD = { source = "monday-fallback", seconds = nil, reason = why }
    return Calendar.KeyOf(y, m, d - back), today
end

---------------------------------------------------------------------------
-- 范围：顶栏读数（锚今天 / 显示月，与右栏选了哪档无关）与右栏三档（锚选中那天）分开算
---------------------------------------------------------------------------

--- 顶栏三个读数用：本周 CD 是"本周至今"（周四起到今天），本月跟着显示月走，全部是累计。
--- @return string|nil startKey, string|nil endKey  含端点的 ISO 键
function Calendar.GetRange(preset)
    if preset == "all" then return ALL_START, ALL_END end
    if preset == "month" then
        if not state.year or not state.month then return nil, nil end
        local dim = LCall("DaysInMonth", state.year, state.month)
        if not dim then return nil, nil end
        return Calendar.KeyOf(state.year, state.month, 1),
               Calendar.KeyOf(state.year, state.month, dim)
    end
    if preset == "week" then
        return Calendar.WeekCDRange()
    end
    if preset == "lastweek" then
        -- 上一整个周 CD：拿本周 CD 的起点往回平移 7 天，止点就是本周起点的前一天。
        -- 本周 CD 读不出（没墙上钟）就跟着读不出，绝不拿显示月去凑一个假的「上周」。
        local s = Calendar.WeekCDRange()
        if not s then return nil, nil end
        local y, m, d = PartsOf(s)
        if not y then return nil, nil end
        return Calendar.KeyOf(y, m, d - 7), Calendar.KeyOf(y, m, d - 1)
    end
    return nil, nil
end

--- 选中那天**所在的那整周** CD（周四起，含起止端点，不截到今天）。
--- 接口给的是"距下次重置还有多久" ⇒ 先推当前 CD 的周四锚点，再按整周平移到包住 key
--- 的那一周；ISO 键定宽，字典序就是时间序，所以只用 KeyOf 加 7 天与字符串比较。
--- 接口问不出 / 锚点推不动 ⇒ 退回以 key 为锚的日历周（周一），原因只进 state.weekCD 供 probe。
function Calendar.WeekCDRangeOf(key)
    local y, m, d = PartsOf(key)
    if not y then return nil, nil end
    local wday = LCall("WeekdayOfDateKey", key)
    local cd, why = Calendar.WeekResetCountdown()
    local now = LCall("WallClock")
    local today = LCall("TodayKey")
    local anchor = (cd and now and today) and LCall("DateKey", now + cd - WEEK_SECONDS) or nil
    if anchor and wday then
        local start, guard = anchor, 0
        while start > key and guard < 520 do
            local ay, am, ad = PartsOf(start)
            if not ay then break end
            start = Calendar.KeyOf(ay, am, ad - 7)
            if not start then break end
            guard = guard + 1
        end
        guard = 0
        while start and guard < 520 do
            local ay, am, ad = PartsOf(start)
            if not ay then break end
            local nxt = Calendar.KeyOf(ay, am, ad + 7)
            if not nxt or nxt > key then break end
            start = nxt
            guard = guard + 1
        end
        local sy, sm, sd
        if start then sy, sm, sd = PartsOf(start) end
        if sy and start <= key then
            state.weekCD = { source = why, seconds = cd, reason = nil }
            return start, Calendar.KeyOf(sy, sm, sd + 6)
        end
    end
    if wday then
        local back = (wday + 5) % 7     -- 周一=0 天：周日(wday=1) 回退 6 天
        state.weekCD = { source = "monday-fallback", seconds = nil, reason = why }
        return Calendar.KeyOf(y, m, d - back), Calendar.KeyOf(y, m, d - back + 6)
    end
    state.weekCD = { source = "none", seconds = nil, reason = why }
    return nil, nil
end

--- 右栏三档：本日 = 选中那天；本周 = 那天的周 CD；本月 = 那天所在月。没有「全部」档。
function Calendar.GetPanelRange(preset)
    local key = state.selKey
    if type(key) ~= "string" then return nil, nil end
    if preset == "day" then return key, key end
    if preset == "week" then return Calendar.WeekCDRangeOf(key) end
    if preset == "month" then
        local y, m = PartsOf(key)
        local dim = LCall("DaysInMonth", y, m)
        if not dim then return nil, nil end
        return Calendar.KeyOf(y, m, 1), Calendar.KeyOf(y, m, dim)
    end
    return nil, nil
end

--- 流水弹窗的六档日期范围，锚**一律是日历点选的那天**（用户 2026-11-10 定：
--- "根据日历点击的日期来，这样不容易出错"）。点历史日开弹窗不会跳回今天。
--- 本日 / 昨日给单日键，本周是那天的整周 CD，上周是再往前一整个周 CD，
--- 本月是那天所在月，全部是哨兵两端。下方读数方块点进来时锚换成今天 / 显示月首日。
function Calendar.PopupRange(preset, anchorKey)
    if type(anchorKey) ~= "string" then return nil, nil end
    if preset == "all" then return ALL_START, ALL_END end
    local y, m, d = PartsOf(anchorKey)
    if not y then return nil, nil end
    if preset == "day" then return anchorKey, anchorKey end
    if preset == "yesterday" then
        local k = Calendar.KeyOf(y, m, d - 1)
        if k then return k, k end
        return nil, nil
    end
    if preset == "week" then return Calendar.WeekCDRangeOf(anchorKey) end
    if preset == "lastweek" then
        -- 与 GetRange('lastweek') 同一算法，只是锚换成传进来的那天：本周 CD 起点
        -- 往回 7 天到前一天。CD 推不出就跟着读不出，不拿显示月凑一个假的「上周」。
        local s = Calendar.WeekCDRangeOf(anchorKey)
        if not s then return nil, nil end
        local sy, sm, sd = PartsOf(s)
        if not sy then return nil, nil end
        return Calendar.KeyOf(sy, sm, sd - 7), Calendar.KeyOf(sy, sm, sd - 1)
    end
    if preset == "month" then
        local dim = LCall("DaysInMonth", y, m)
        if not dim then return nil, nil end
        return Calendar.KeyOf(y, m, 1), Calendar.KeyOf(y, m, dim)
    end
    return nil, nil
end

--- 切右栏档位：只换范围，不碰选中日；滚动偏移回顶（新列表不该停在旧列表的半腰）。
function Calendar.SelectPreset(preset)
    if preset ~= "day" and preset ~= "week" and preset ~= "month" then return false end
    local s, e = Calendar.GetPanelRange(preset)
    if not s then return false end
    state.preset, state.startKey, state.endKey = preset, s, e
    state.scrollY = 0
    Calendar.Refresh()
    return true
end

---------------------------------------------------------------------------
-- 建窗（第一次打开时才建：加载期一个帧都不造）
---------------------------------------------------------------------------

local function AddFont(role, parent, font)
    local fs = parent:CreateFontString(nil, "OVERLAY", font)
    pcall(fs.SetJustifyH, fs, "LEFT")
    ui.fonts[#ui.fonts + 1] = { role = role, fs = fs, font = font }
    return fs
end

local function AddButton(parent, template)
    local b = CreateFrame("Button", nil, parent, template)
    pcall(b.RegisterForClicks, b, "LeftButtonUp")
    return b
end

local function AddCellHost(parent)
    -- 格子本身就是可点可悬的 Button（热区池那套，Matrix.lua:358-385）+ BackdropTemplate
    -- 给的底色（无 edgeFile），今天那一格才补上边框纹理。
    return CreateFrame("Button", nil, parent, "BackdropTemplate")
end

---------------------------------------------------------------------------
-- 摆字三件套（Build 和绘制都要用，放在建窗之前）
---------------------------------------------------------------------------

local function SetText(fs, text)
    if not fs then return end
    pcall(fs.SetText, fs, tostring(text))
end

local function SetColor(fs, rgb)
    if not fs then return end
    pcall(fs.SetTextColor, fs, rgb[1], rgb[2], rgb[3], 1)
end

local function NetColor(net)
    if type(net) ~= "number" then return C_ZERO end
    if net > 0 then return C_POS end
    if net < 0 then return C_NEG end
    return C_ZERO
end

--- 只跟语言走的那些串（标题、星期表头、按钮、列头）集中在这里摆。
--- Build 尾一次 + 每次 Refresh 一次：运行中换语言（Locale.Set）不用再重开窗口也能立刻换字，
--- 用例 test_locale_selection_and_fallbacks 钉着这条。
--- 数值/日期那种每次重画都要重算的串不在这里，仍归各自的 Render。
local function ApplyStaticTexts()
    SetText(ui.title, T("CAL_TITLE"))
    SetText(ui.help, T("FEEDBACK_BTN"))
    SetText(ui.btnPrevMo, T("MO_PREV"))
    SetText(ui.btnNextMo, T("MO_NEXT"))
    for c = 1, 7 do
        SetText(ui.weekdays[c], T("WD_" .. c))
    end
    -- 下方读数方块：字面键逐个摆，静态守卫才认得出（动态拼键那套会漏）。
    local totalLabels = {
        week = T("RANGE_WEEK"), lastweek = T("RANGE_LASTWEEK"),
        month = T("RANGE_MONTH"), all = T("RANGE_ALL"),
        warband = T("SUMMARY_WARBAND"), assets = T("SUMMARY_ASSETS"),
    }
    for _, preset in ipairs(SUMMARY_BLOCKS) do
        local slot = ui.totals[preset]
        if slot then SetText(slot.label, totalLabels[preset]) end
    end
    -- 右栏只有三档：本日 / 本周 / 本月（字挂在页签自己的 label 上，不是按钮自带文本）
    for _, preset in ipairs(PANEL_PRESETS) do
        SetText(ui.presets[preset].label, T("RANGE_" .. preset:upper()))
    end
    SetText(ui.charHead.name, T("COL_CHAR"))
    SetText(ui.charHead.net, T("COL_NET"))
end

--- 右栏滚动：偏移自己记、上限自己算。GetVerticalScroll / GetVerticalRange 在 12.x 的
--- 返回形态没实证过（AHSnipe/SnipeList.lua:42 明说不许拿它们做算术），所以只写不读。
local function ContentH()
    return math.max(GEOM.PANEL_LIST_H, (tonumber(state.panelCount) or 0) * GEOM.ROW_H)
end

local function Wheel(delta)
    if not ui or not ui.scroll then return end
    local top = math.max(0, ContentH() - GEOM.PANEL_LIST_H)
    local y = (tonumber(state.scrollY) or 0) - (tonumber(delta) or 0) * GEOM.ROW_H
    state.scrollY = math.max(0, math.min(top, y))
    pcall(ui.scroll.SetVerticalScroll, ui.scroll, state.scrollY)
end

--- 行池：整行就是那一颗按钮（管理类列表每行一颗，用户定的硬规矩），按需造、不预造一屏空行。
--- 每行宿主也要挂 OnMouseWheel：滚轮落在按钮上不吃滚动事件（Matrix.lua:1546 实测）。
local function EnsurePanelRow(i)
    if ui.charRows[i] then return ui.charRows[i] end
    local host = CreateFrame("Button", nil, ui.content)
    host:SetSize(GEOM.CONTENT_W, GEOM.ROW_H)
    host:SetPoint("TOPLEFT", ui.content, "TOPLEFT", 0, -((i - 1) * GEOM.ROW_H))
    host:EnableMouse(true)
    host:EnableMouseWheel(true)
    pcall(host.RegisterForClicks, host, "LeftButtonUp")
    host:SetScript("OnMouseWheel", function(_, delta) Wheel(delta) end)
    local row = { host = host, charKey = nil, income = 0, expense = 0, current = false }
    row.btn = host                       -- 整行按钮（取证件沿用的名字）
    row.name = host:CreateFontString(nil, "OVERLAY", FONT_BODY)
    row.name:SetPoint("LEFT", host, "LEFT", COLS.name, 0)
    row.net = host:CreateFontString(nil, "OVERLAY", FONT_BODY)
    row.net:SetPoint("RIGHT", host, "RIGHT", -COLS.net, 0)
    host.__gclRow = row
    host:SetScript("OnClick", function(self) Calendar.OpenCharFlow(self.__gclRow.charKey) end)
    host:SetScript("OnEnter", function(self) Calendar.ShowRowTip(self.__gclRow) end)
    host:SetScript("OnLeave", function() Calendar.HideCellTip() end)
    ui.charRows[i] = row
    return row
end

---------------------------------------------------------------------------
-- 页签重画（放在 Build 之前：Build 里接的悬停脚本要捕获到这两个 local）
---------------------------------------------------------------------------

--- 三格页签只跟 state.preset 与鼠标走。选中那格：更高、暖底、金字、底沿金线压住基线；
--- 其余：矮 TAB_LIP、冷灰底、灰字、无金线。底沿钉死在同一条基线上，所以只改高矮，
--- 不重设锚点（矮的那格顶沿往下缩，正是页签该有的样子）。
--- 没有定时器、没有事件：只在 Refresh（开窗 / 点击 / 换语言）和进出悬停时重画这三格。
local function RenderPresets()
    for _, preset in ipairs(PANEL_PRESETS) do
        local tab = ui.presets[preset]
        local on = (state.preset == preset)
        tab.active = on
        tab:SetHeight(on and GEOM.TAB_H or (GEOM.TAB_H - GEOM.TAB_LIP))
        local bg = TAB_BG_OFF
        local fg = C_TAB_OFF
        if on then
            bg, fg = TAB_BG_ON, C_TAB_ON
        elseif tab.hover then
            bg, fg = TAB_BG_HOVER, C_TAB_HOVER
        end
        pcall(tab.SetBackdropColor, tab, bg[1], bg[2], bg[3], 1)
        SetColor(tab.label, fg)
        if on then tab.mark:Show() else tab.mark:Hide() end
    end
end

--- 鼠标进出一格页签：只亮这一格的底与字，不动选中档、不重画列表。
local function HoverPreset(preset, on)
    local tab = ui and ui.presets[preset]
    if not tab then return end
    tab.hover = on and true or false
    RenderPresets()
end

local function Build()
    if built then return true end
    ui = { fonts = {}, cells = {}, charRows = {}, totals = {}, presets = {} }

    frame = CreateFrame("Frame", "GDLCalFrame", UIParent, "BackdropTemplate")
    frame:SetSize(GEOM.WIN_W, GEOM.WIN_H)
    frame:SetPoint("CENTER")
    -- 层序：与 GQL 矩阵同为 DIALOG + SetToplevel，这样"谁被点 / 谁最后开"谁在最前
    -- （原生窗口那种点一下就上来的能力，老农整合包用的就是这一对；GQL/GL 同样挂）。
    -- 不走手写 Raise 的长串逻辑也能互相压：同 strata 里 toplevel 自己管点击置顶。
    frame:SetFrameStrata("DIALOG")
    pcall(frame.SetToplevel, frame, true)
    frame:SetClampedToScreen(true)
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
    frame:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
    frame:SetBackdrop({
        bgFile = "Interface/Buttons/WHITE8X8",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 16, insets = { left = 4, right = 4, top = 4, bottom = 4 },
    })
    pcall(frame.SetBackdropColor, frame, 0.07, 0.07, 0.09, 1)

    -- 点/拖动/显示这扇窗时，把它抬到同时开着的 GQL 等平级窗口之上（Core.RaiseFront）。
    -- 统一协议（2026-11-11）：本窗与 GQL 各窗都是 DIALOG + SetToplevel，谁被点/谁最后开谁
    -- 在最前，所以 RaiseFront 只把本窗 Raise 到 DIALOG 最上，不再改写对端窗的 strata/层号。
    local function BringToFront()
        if type(GDL) == "table" and type(GDL.RaiseFront) == "function" then
            pcall(GDL.RaiseFront, frame, ui and ui.close)
        end
    end
    frame:SetScript("OnMouseDown", BringToFront)
    frame:SetScript("OnShow", BringToFront)

    -- ESC 关闭：挂暴雪名单。名单里存的是**全局名字符串**，客户端拿它去 _G 里找帧，
    -- 所以帧必须有全局名（GDLCalFrame），这一条有用例钉着（Matrix.lua:1607-1609 同口径）。
    local list = _G.UISpecialFrames
    if type(list) == "table" then
        local already = false
        for i = 1, #list do
            if list[i] == "GDLCalFrame" then already = true end
        end
        if not already then table.insert(list, "GDLCalFrame") end
    end

    -- 标题行
    ui.title = AddFont("标题", frame, FONT_TITLE)
    ui.title:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -GEOM.PAD)

    -- 标题旁的 ?：点开一扇可复制反馈网址的小窗（用户 2026-11-11）。
    -- 文字归 ApplyStaticTexts（换语言要重画），这里只建控件与接线。
    ui.help = AddButton(frame, "UIPanelButtonTemplate")
    ui.help:SetSize(GEOM.HELP_BTN, GEOM.HELP_BTN)
    ui.help:SetPoint("LEFT", ui.title, "RIGHT", GEOM.HELP_GAP, 0)
    ui.help:SetScript("OnClick", function() Calendar.ShowFeedback() end)

    ui.close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    ui.close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -GEOM.CLOSE_INSET, -GEOM.CLOSE_INSET)
    ui.close:SetScript("OnClick", function() Calendar.Close() end)
    -- × 的层号必须钉回主帧 +1：暴雪模板自带 frameLevel=510（Matrix.lua:1600-1605 记着这条实测），
    -- 不钉的话设置窗/别的窗盖上来时，那颗 × 还浮在人家上面。
    -- GetFrameLevel 读不出（别的客户端/别的时机）就不动它，宁可维持模板默认也不瞎写一层。
    local okL, lvl = pcall(frame.GetFrameLevel, frame)
    if okL and tonumber(lvl) then
        pcall(ui.close.SetFrameLevel, ui.close, tonumber(lvl) + 1)
    end

    -- 导航 + 顶栏三个读数（草图同一行）
    ui.btnPrevMo = AddButton(frame, "UIPanelButtonTemplate")
    ui.btnPrevMo:SetSize(GEOM.MO_BTN_W, GEOM.BTN_H)
    ui.btnPrevMo:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -(GEOM.PAD + GEOM.TITLE_H))
    ui.btnPrevMo:SetScript("OnClick", function() Calendar.ShiftMonth(-1) end)

    ui.btnNextMo = AddButton(frame, "UIPanelButtonTemplate")
    ui.btnNextMo:SetSize(GEOM.MO_BTN_W, GEOM.BTN_H)
    ui.btnNextMo:SetPoint("TOPLEFT", ui.btnPrevMo, "TOPRIGHT", GEOM.MO_BTN_GAP, 0)
    ui.btnNextMo:SetScript("OnClick", function() Calendar.ShiftMonth(1) end)

    ui.monthLabel = AddFont("月标题", frame, FONT_LABEL)
    ui.monthLabel:SetPoint("LEFT", ui.btnNextMo, "RIGHT", GEOM.MO_LABEL_GAP, 0)

    -- 顶栏读数已下线：导航行只留 < 月份 >，四个读数方块搬到日历下面（见下方读数带）。

    -- 星期表头 + 网格底板
    ui.weekdays = {}
    local wdY = GEOM.PAD + GEOM.TITLE_H + GEOM.NAV_H
    for c = 1, 7 do
        local fs = AddFont("星期" .. c, frame, FONT_SMALL)
        fs:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + (c - 1) * GEOM.CELL_W + 4, -wdY)
        ui.weekdays[c] = fs
    end

    ui.gridTop = wdY + GEOM.WD_H
    ui.grid = CreateFrame("Frame", nil, frame)
    ui.grid:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -ui.gridTop)
    ui.grid:SetSize(GEOM.CELL_W * 7, GEOM.CELL_H * GEOM.GRID_ROWS)

    for i = 1, GEOM.GRID_ROWS * 7 do
        local row = math.floor((i - 1) / 7) + 1
        local col = ((i - 1) % 7) + 1
        local host = AddCellHost(ui.grid)
        host:SetSize(GEOM.CELL_W - 2, GEOM.CELL_H - 2)
        host:SetPoint("TOPLEFT", ui.grid, "TOPLEFT", (col - 1) * GEOM.CELL_W + 1, -(row - 1) * GEOM.CELL_H + 1)
        local cell = { host = host, row = row, col = col, dateKey = nil, inMonth = false, used = false }
        cell.num = AddFont("格" .. i .. "日号", host, FONT_CELL)
        cell.num:SetPoint("TOPLEFT", host, "TOPLEFT", 4, -5)
        cell.net = AddFont("格" .. i .. "净", host, FONT_CELL)
        cell.net:SetPoint("BOTTOM", host, "BOTTOM", 0, 5)

        -- 点击 = 选中这一天（右栏换成这天的号）；悬停 = 日期 + 收 / 支 + 几个号有变动。
        host.__gclCell = cell
        host:SetScript("OnClick", function(self) Calendar.SelectDay(self.__gclCell.dateKey) end)
        host:SetScript("OnEnter", function(self) Calendar.ShowCellTip(self.__gclCell) end)
        host:SetScript("OnLeave", function() Calendar.HideCellTip() end)
        ui.cells[i] = cell
    end

    -- 日历下方读数带：横线隔开网格，六个等宽方块贯穿窗底（本周 / 上周 / 本月 / 全部 / 战团 / 总计）。
    -- 形状照 GoldQuestLedger/Options.lua:695-699 那根 1px 线 + Matrix.lua:1610-1640 的竖分格：
    -- CreateTexture + SetColorTexture，两头钉，不用细长 Frame。
    -- 文字上沿比横线低 BLOCK_PAD，不再贴着分割线（用户 2026-11-10）。
    ui.summaryDiv = frame:CreateTexture(nil, "ARTWORK")
    pcall(ui.summaryDiv.SetColorTexture, ui.summaryDiv, 1, 1, 1, 0.15)
    ui.summaryDiv:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD,
        -(GEOM.SUMMARY_TOP + GEOM.SUMMARY_DIV_GAP))
    ui.summaryDiv:SetWidth(GEOM.SUMMARY_W)
    ui.summaryDiv:SetHeight(1)
    ui.summaryVDivs = {}
    for i = 1, #SUMMARY_BLOCKS - 1 do
        local v = frame:CreateTexture(nil, "ARTWORK")
        pcall(v.SetColorTexture, v, 1, 1, 1, 0.15)
        v:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + i * GEOM.BAND_W,
            -(GEOM.SUMMARY_TOP + GEOM.SUMMARY_DIV_GAP + GEOM.BLOCK_PAD))
        v:SetWidth(1)
        v:SetHeight(GEOM.SUMMARY_H - GEOM.SUMMARY_DIV_GAP - GEOM.BLOCK_PAD - 6)
        ui.summaryVDivs[i] = v
    end
    for i, preset in ipairs(SUMMARY_BLOCKS) do
        local left = GEOM.TOTALS_LEFT + (i - 1) * (GEOM.BAND_W + GEOM.BAND_GAP)
        local width = (i == #SUMMARY_BLOCKS) and GEOM.BAND_LAST_W or GEOM.BAND_W
        local host = CreateFrame("Button", nil, frame)
        host:SetSize(width, GEOM.SUMMARY_H - GEOM.SUMMARY_DIV_GAP)
        host:SetPoint("TOPLEFT", frame, "TOPLEFT", left,
            -(GEOM.SUMMARY_TOP + GEOM.SUMMARY_DIV_GAP))
        host:EnableMouse(true)
        local label = AddFont("读数" .. preset .. "标签", host, FONT_SMALL)
        label:SetPoint("TOPLEFT", host, "TOPLEFT", GEOM.BLOCK_TEXT_X, -GEOM.BLOCK_PAD)
        local value = AddFont("读数" .. preset .. "数值", host, FONT_BODY)
        value:SetPoint("TOPLEFT", host, "TOPLEFT", GEOM.BLOCK_TEXT_X,
            -(GEOM.BLOCK_PAD + 16))
        local slot = { host = host, label = label, value = value, preset = preset,
                       left = left, right = left + width }
        host.__gclTotals = slot
        -- 四个范围方块整块可点，点了开这一段时间的流水（和右栏整行一个路子）。
        if IsRangeBlock(preset) then
            pcall(host.RegisterForClicks, host, "LeftButtonUp")
            host:SetScript("OnClick", function(self)
                Calendar.OpenRangeFlow(self.__gclTotals.preset)
            end)
        end
        -- 战团不挂悬停（用户 2026-11-11：悬停内容还是战团，没有额外信息）。
        if preset ~= "warband" then
            host:SetScript("OnEnter", function(self) Calendar.ShowTotalsTip(self.__gclTotals) end)
            host:SetScript("OnLeave", function() Calendar.HideCellTip() end)
        end
        ui.totals[preset] = slot
    end

    -- 左右栏之间的竖分隔（Matrix.lua:1695-1702 那种 1 像素线）；到网格底为止，不穿读数带。
    ui.panelDiv = frame:CreateTexture(nil, "ARTWORK")
    pcall(ui.panelDiv.SetColorTexture, ui.panelDiv, 1, 1, 1, 0.15)
    ui.panelDiv:SetPoint("TOPLEFT", frame, "TOPLEFT",
        GEOM.PANEL_X - math.floor(GEOM.PANEL_GAP / 2), -GEOM.PAD)
    ui.panelDiv:SetWidth(1)
    ui.panelDiv:SetHeight(GEOM.PANEL_H)

    -- 右栏：顶部三个页签 + 列头 + 可滚动的「号名 / 净」列表
    ui.panel = CreateFrame("Frame", nil, frame)
    ui.panel:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PANEL_X, -GEOM.PAD)
    ui.panel:SetSize(GEOM.PANEL_W, GEOM.PANEL_H)
    ui.panel:Show()

    -- 页签基线：先建（层号在三格之下），选中那格底沿的金线才压得住它。
    ui.presetRule = ui.panel:CreateTexture(nil, "ARTWORK")
    pcall(ui.presetRule.SetColorTexture, ui.presetRule, 1, 1, 1, 0.15)
    ui.presetRule:SetPoint("TOPLEFT", ui.panel, "TOPLEFT", 0, -GEOM.TAB_H)
    ui.presetRule:SetWidth(GEOM.PRESET_STRIP_W)
    ui.presetRule:SetHeight(GEOM.TAB_RULE_H)

    for i, preset in ipairs(PANEL_PRESETS) do
        local b = CreateFrame("Button", nil, ui.panel, "BackdropTemplate")
        b:SetSize(GEOM.PRESET_W, GEOM.TAB_H)
        b:SetPoint("BOTTOMLEFT", ui.panel, "TOPLEFT",
            (i - 1) * (GEOM.PRESET_W + GEOM.PRESET_GAP), -GEOM.TAB_H)
        b:EnableMouse(true)
        pcall(b.RegisterForClicks, b, "LeftButtonUp")
        pcall(b.SetBackdrop, b, TAB_BACKDROP)
        b.__gclPreset = preset
        b.active, b.hover = false, false
        b.label = AddFont("页签" .. preset, b, FONT_LABEL)
        b.label:SetPoint("CENTER", b, "CENTER", 0, 0)
        b.mark = b:CreateTexture(nil, "OVERLAY")
        pcall(b.mark.SetColorTexture, b.mark, C_TAB_ON[1], C_TAB_ON[2], C_TAB_ON[3], 1)
        b.mark:SetPoint("BOTTOMLEFT", b, "BOTTOMLEFT", 0, -GEOM.TAB_RULE_H)
        b.mark:SetWidth(GEOM.PRESET_W)
        b.mark:SetHeight(GEOM.TAB_MARK_H)
        b.mark:Hide()
        b:SetScript("OnClick", function(self) Calendar.SelectPreset(self.__gclPreset) end)
        b:SetScript("OnEnter", function(self) HoverPreset(self.__gclPreset, true) end)
        b:SetScript("OnLeave", function(self) HoverPreset(self.__gclPreset, false) end)
        ui.presets[preset] = b
    end
    ui.charHead = { name = AddFont("右栏列头号名", ui.panel, FONT_SMALL),
                    net = AddFont("右栏列头净", ui.panel, FONT_SMALL) }
    ui.charHead.name:SetPoint("TOPLEFT", ui.panel, "TOPLEFT", COLS.name, -GEOM.PANEL_PRESET_H)
    ui.charHead.net:SetPoint("TOPRIGHT", ui.panel, "TOPLEFT",
        GEOM.CONTENT_W - COLS.net, -GEOM.PANEL_PRESET_H)

    ui.scroll = CreateFrame("ScrollFrame", "GDLCalPanelScroll", ui.panel,
        "UIPanelScrollFrameTemplate")
    ui.scroll:SetPoint("TOPLEFT", ui.panel, "TOPLEFT", 0,
        -(GEOM.PANEL_PRESET_H + GEOM.PANEL_HEAD_H))
    ui.scroll:SetSize(GEOM.CONTENT_W, GEOM.PANEL_LIST_H)
    ui.scroll:EnableMouseWheel(true)
    ui.scroll:SetScript("OnMouseWheel", function(_, delta) Wheel(delta) end)
    ui.scroll:Show()

    ui.content = CreateFrame("Frame", "GDLCalPanelContent", ui.scroll)
    ui.content:SetWidth(GEOM.CONTENT_W)
    ui.content:SetHeight(GEOM.PANEL_LIST_H)
    ui.scroll:SetScrollChild(ui.content)
    ui.content:Show()

    ui.empty = ui.content:CreateFontString(nil, "OVERLAY", FONT_LABEL)
    ui.empty:SetPoint("TOPLEFT", ui.content, "TOPLEFT", COLS.name, -4)

    -- 暗尺：/gdl probe ui 量提示框那几行字有多宽用（LogWindow.lua:96-101 同一手法：
    -- alpha=0 而不是 Hide —— 隐藏的帧客户端不一定还给排版，量回来是 0）。
    ui.meas = frame:CreateFontString(nil, "OVERLAY", FONT_BODY)
    ui.meas:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -GEOM.PAD)
    ui.meas:SetWidth(GEOM.CELL_W * 7)
    pcall(ui.meas.SetAlpha, ui.meas, 0)

    -- 自绘悬停提示框：GameTooltip 那块背板半透明，且第一行走 header 字体、后续走正文，
    -- 收入/支出天然一大一小 —— SetAlpha / SetBackdropColor 都压不住（12.x 的 tooltip
    -- 背板不是普通 backdrop）。自己建一扇 TOOLTIP strata 的不透明小框，四行同一个字体，
    -- 两条毛病一次断根。组件全是本仓库已证过的 CreateFrame + BackdropTemplate + AddFont。
    ui.tip = CreateFrame("Frame", "GDLCalTipFrame", UIParent, "BackdropTemplate")
    ui.tip:SetFrameStrata("TOOLTIP")
    ui.tip:SetWidth(GEOM.TIP_W)
    ui.tip:SetBackdrop({
        bgFile = "Interface/Buttons/WHITE8X8",
        edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
        edgeSize = 12, insets = { left = 3, right = 3, top = 3, bottom = 3 },
    })
    pcall(ui.tip.SetBackdropColor, ui.tip, 0.07, 0.07, 0.09, 1)
    pcall(ui.tip.SetBackdropBorderColor, ui.tip, 0.40, 0.40, 0.45, 1)
    ui.tip:EnableMouse(false)
    ui.tipLines = {}
    for i = 1, 4 do
        local fs = AddFont("提示行" .. i, ui.tip, FONT_TIP)
        fs:SetWidth(GEOM.TIP_W - 2 * GEOM.TIP_PAD - 4)
        fs:SetPoint("TOPLEFT", ui.tip, "TOPLEFT", GEOM.TIP_PAD + 2,
            -(GEOM.TIP_PAD + (i - 1) * GEOM.TIP_ROW_H))
        fs:Hide()
        ui.tipLines[i] = fs
    end
    ui.tip:Hide()

    ApplyStaticTexts()
    built = true
    return true
end

---------------------------------------------------------------------------
-- 绘制：只做查表 + 摆字，不算别的
---------------------------------------------------------------------------

--- 当前号键：与 Ledger 的拼法一致（UnitName .. "-" .. GetRealmName，Ledger.lua:186-191），
--- 两个读不出来就是 nil（不猜、不用占位假号）。
local function CurrentCharKey()
    local name, realm
    local fn = _G.UnitName
    if type(fn) == "function" then
        local okCall, v = pcall(fn, "player")
        if okCall and type(v) == "string" and v ~= "" then name = v end
    end
    local rf = _G.GetRealmName
    if type(rf) == "function" then
        local okCall, v = pcall(rf)
        if okCall and type(v) == "string" and v ~= "" then realm = v end
    end
    if not name or not realm then return nil end
    return name .. "-" .. realm
end

--- 下方六个读数方块：前四个是范围收支，显示词写「本周」，口径仍是周四起的周 CD 至今；
--- 「上周」是上一整个周 CD；本月跟着显示月走；全部是记账以来累计。全走 GetRangeTotals
--- 的查表结果，与右栏选了哪档无关。每个方块把收入/支出/范围留在 slot 上，悬停时直接读，不重算。
--- 「全部」**不等于银行里的钱**（计划 §3：转账不计收支、界面外变动记 unknown），
--- 两个数对不上是正常的，这里绝不互相"修正"。
--- 后两块不是收支：战团读 C_Bank 快照；总计 = 已记录角色快照之和 + 战团。
--- 读不到一律写「—」，不拿 0 冒充（与范围块读不到时钟写 "?" 同一口径）。
local function RenderTotals()
    for _, preset in ipairs(TOTALS_PRESETS) do
        local slot = ui.totals[preset]
        if slot then
            local s, e = Calendar.GetRange(preset)
            local t = s and LCall("GetRangeTotals", s, e) or nil
            local net = (type(t) == "table") and t.net or nil
            SetText(slot.value, Calendar.FormatMoneyIcon(net))
            SetColor(slot.value, NetColor(net))
            slot.startKey, slot.endKey = s, e
            slot.income = (type(t) == "table") and t.income or nil
            slot.expense = (type(t) == "table") and t.expense or nil
            -- 悬停的日期行：「全部」不用哨兵 0000-01-01~9999-12-31（没有信息量，用户
            -- 2026-11-10 定稿：要么从真实记录的日期算起到当前时间，要么就别写日期）。
            -- 起点取账本最早记录日、终点今天；一条记录都没有时两个都留 nil，
            -- ShowTotalsTip 就整行不写日期。查表照旧用哨兵（结果一致），只换显示端。
            if preset == "all" then
                slot.tipStart = LCall("FirstDateKey")
                slot.tipEnd = LCall("TodayKey")
            else
                slot.tipStart, slot.tipEnd = s, e
            end
        end
    end

    local snapshot = LCall("GetGoldAssets")
    if type(snapshot) ~= "table" then snapshot = {} end
    local warband = tonumber(snapshot.warband)

    local warbandSlot = ui.totals.warband
    if warbandSlot then
        if warband ~= nil then
            SetText(warbandSlot.value, Calendar.FormatMoneyIcon(warband))
            SetColor(warbandSlot.value, C_ASSET)
        else
            SetText(warbandSlot.value, NO_VALUE)
            SetColor(warbandSlot.value, C_ZERO)
        end
    end

    local assetsSlot = ui.totals.assets
    if assetsSlot then
        local charGold = tonumber(snapshot.charGold) or 0
        local charCount = tonumber(snapshot.charCount) or 0
        local known = charCount > 0 or warband ~= nil
        if known then
            SetText(assetsSlot.value, Calendar.FormatMoneyIcon(charGold + (warband or 0)))
            SetColor(assetsSlot.value, C_ASSET)
        else
            SetText(assetsSlot.value, NO_VALUE)
            SetColor(assetsSlot.value, C_ZERO)
        end
        assetsSlot.charGold, assetsSlot.charCount = charGold, charCount
        assetsSlot.warband = warband
    end
end

local CELL_BG_NORMAL   = { bgFile = "Interface/Buttons/WHITE8X8", edgeSize = 16,
                           insets = { left = 1, right = 1, top = 1, bottom = 1 } }
local CELL_BG_TODAY    = { bgFile = "Interface/Buttons/WHITE8X8",
                           edgeFile = "Interface/Tooltips/UI-Tooltip-Border", edgeSize = 16,
                           insets = { left = 1, right = 1, top = 1, bottom = 1 } }

--- 月历：固定 42 格（6 行 × 7 列）。每格都算出自己的 ISO 键，
--- 月外的格子只画底不摆字，超出本月行数的整格 Hide。
local function RenderGrid()
    if not state.year or not state.month then return end
    local dim = LCall("DaysInMonth", state.year, state.month)
    if not dim then return end
    local firstKey = Calendar.KeyOf(state.year, state.month, 1)
    local firstWday = firstKey and LCall("WeekdayOfDateKey", firstKey)
    if not firstWday then return end
    local offset = firstWday - 1                       -- 1=周日 → 首日前的空格数
    local rowsNeeded = math.ceil((offset + dim) / 7)
    if rowsNeeded > GEOM.GRID_ROWS then rowsNeeded = GEOM.GRID_ROWS end
    local todayKey = LCall("TodayKey")
    local monthPrefix = ("%04d-%02d"):format(state.year, state.month)

    ui.gridInfo = { firstKey = firstKey, firstWday = firstWday, dim = dim,
                    rowsNeeded = rowsNeeded, todayKey = todayKey }

    for i, cell in ipairs(ui.cells) do
        local key = Calendar.KeyOf(state.year, state.month, i - offset)
        cell.dateKey = key
        cell.inMonth = (type(key) == "string" and key:sub(1, 7) == monthPrefix)
        cell.used = (math.floor((i - 1) / 7) + 1) <= rowsNeeded
        cell.today = (key == todayKey)
        cell.selected = (key == state.selKey)

        if not cell.used then
            cell.host:Hide()
        else
            cell.host:Show()
            -- 只有今天那一格带边框纹理；选中的那天用底色标出来（两件事不混用）。
            pcall(cell.host.SetBackdrop, cell.host, cell.today and CELL_BG_TODAY or CELL_BG_NORMAL)
            if cell.today then
                pcall(cell.host.SetBackdropColor, cell.host, 0.16, 0.15, 0.10, 1)
            elseif cell.selected then
                pcall(cell.host.SetBackdropColor, cell.host, 0.17, 0.15, 0.09, 1)
            elseif cell.inMonth then
                pcall(cell.host.SetBackdropColor, cell.host, 0.10, 0.10, 0.13, 1)
            else
                pcall(cell.host.SetBackdropColor, cell.host, 0.05, 0.05, 0.07, 1)
            end

            local y, m, d = PartsOf(key)
            SetText(cell.num, cell.inMonth and tostring(d) or "")
            if not cell.inMonth then
                -- 月外格：只画底，不摆数字（草图"空格只画底"）
                SetText(cell.net, "")
                SetColor(cell.net, C_ZERO)
            else
                local day = LCall("GetDay", key)
                local inc = type(day) == "table" and day.income or 0
                local exp = type(day) == "table" and day.expense or 0
                local net = type(day) == "table" and day.net or 0
                if inc == 0 and exp == 0 then
                    -- 零变动：只留日号（收·支留给悬停，净不写 0）
                    SetText(cell.net, "")
                    SetColor(cell.net, C_ZERO)
                else
                    SetText(cell.net, Calendar.FormatMoneyIcon(net))
                    SetColor(cell.net, NetColor(net))
                end
                cell.income, cell.expense = inc, exp
            end
        end
    end
end

--- 右栏：这段范围里**有变动**的每个号，一行「号名 / 净」；0 变动的号不列（玩家 2026-11-10 定）。
--- 溢出不裁列表：内容高度按行数长，滚动区给出口（小号多是他明确提出来的形状）。
local function RenderPanelRows()
    local rows = LCall("GetRangeChars", state.startKey, state.endKey) or {}
    local curKey = CurrentCharKey()

    -- 当前号置顶（Ledger 只在并列时用当前号破平，这里无条件挪到第一行）+ 视觉标记。
    -- 注意是"取出再插到最前"：把第 1 行复制一份插到 i 位再删头部是**轮转**，
    -- 那样当前号仍在原位、只是别的号错位了（写错这处时用例 rows[1] 照样查得出号名，很难看出来）。
    if curKey then
        for i = 2, #rows do
            if rows[i].name == curKey then
                table.insert(rows, 1, table.remove(rows, i))
                break
            end
        end
    end

    state.panelCount = #rows
    local shown = math.max(#rows, #ui.charRows)
    for i = 1, shown do
        local row = EnsurePanelRow(i)
        local data = rows[i]
        if not data then
            row.host:Hide()
            row.charKey = nil
            row.income, row.expense = 0, 0
            SetText(row.name, "")
            SetText(row.net, "")
        else
            row.host:Show()
            row.charKey = data.name
            row.income, row.expense = tonumber(data.income) or 0, tonumber(data.expense) or 0
            SetText(row.name, CharLabel(data.name, 24))
            SetText(row.net, Calendar.FormatMoneyIcon(data.net))
            SetColor(row.net, NetColor(data.net))
            -- 当前号只靠置顶区分；名字一律职业色，不再盖一层琥珀。
            row.current = (curKey ~= nil and data.name == curKey)
        end
    end

    ui.content:SetHeight(ContentH())
    local top = math.max(0, ContentH() - GEOM.PANEL_LIST_H)
    state.scrollY = math.max(0, math.min(top, tonumber(state.scrollY) or 0))
    pcall(ui.scroll.SetVerticalScroll, ui.scroll, state.scrollY)

    if #rows == 0 then
        ui.empty:Show()
        -- 范围键本身没算出来（墙上钟读不出）⇒ 绝不写成"这个范围没有变动"：那是对着
        -- 一个根本没确定的日子报"没事发生"（读不到就说为什么读不到）。
        SetText(ui.empty, state.startKey and T("EMPTY_RANGE") or T("NO_CLOCK"))
    else
        ui.empty:Hide()
    end
end

function Calendar.Refresh()
    if not built then return end
    local okCall, err = pcall(function()
        ApplyStaticTexts()
        RenderPresets()
        RenderTotals()
        RenderGrid()
        RenderPanelRows()
    end)
    if not okCall then NoteError("Refresh", err) end
    pcall(SetText, ui.monthLabel, Fmt("CAL_MONTH", state.year or 0, state.month or 0))
end

---------------------------------------------------------------------------
-- 交互（只有点击会改状态）
---------------------------------------------------------------------------

--- 换月：< / >。顶栏「本月」读数跟着显示月走；右栏范围锚在选中那天，不跟着翻月跑。
function Calendar.ShiftMonth(delta)
    if not state.year or not state.month then return false end
    local y, m = state.year, state.month + delta
    if m < 1 then m = 12 ; y = y - 1 elseif m > 12 then m = 1 ; y = y + 1 end
    state.year, state.month = y, m
    Calendar.Refresh()
    return true
end

--- 点日号：选中它、档位落「本日」、右栏列这天的号，并把月历翻到它所在月。
--- 点已经选中的那天不动档位（T3 钉过：开窗本就选中今天，重复点不该把「本月」踢回「本日」）。
function Calendar.SelectDay(dateKey)
    if type(dateKey) ~= "string" then return false end
    local y, m = PartsOf(dateKey)
    if not y then return false end
    if state.selKey ~= dateKey then
        state.selKey = dateKey
        state.preset = "day"
        state.startKey, state.endKey = dateKey, dateKey
        state.scrollY = 0
    end
    state.year, state.month = y, m
    Calendar.Refresh()
    return true
end

--- 点右栏整行：开该号在**当前范围**的流水弹窗（范围说法与 /gdl flow 共用一段拼法）。
function Calendar.OpenCharFlow(charKey)
    if type(charKey) ~= "string" or charKey == "" then return false end
    local flows = GDL and GDL.Flows
    if type(flows) ~= "table" or type(flows.Open) ~= "function" then return false end
    local s, e = state.startKey, state.endKey
    if type(s) ~= "string" or type(e) ~= "string" then return false end
    local text
    if type(flows.RangeText) == "function" then
        local okRT, r = pcall(flows.RangeText, state.preset, s, e)
        if okRT then text = r end
    end
    if type(text) ~= "string" then
        text = Fmt("FLOW_RANGE_DAY", T("RANGE_DAY"), s)
    end
    -- 第 5/6 参把"日历点选的那天"与右栏当前档交给弹窗：它自己的日期下拉按这个锚算，
    -- 点历史日开弹窗不会跳回今天（用户 2026-11-10 定）。
    -- 提示框是 TOOLTIP strata 的 UIParent 子帧，别让它浮到刚开的弹窗上面。
    Calendar.HideCellTip()
    local okCall, opened = pcall(flows.Open, charKey, s, e, text, state.selKey, state.preset)
    return okCall and opened ~= false
end

--- 点下方四个范围方块（本周 / 上周 / 本月 / 全部）：和点右栏角色一样开流水弹窗，
--- 只是筛的维度换成时间 —— 范围就用这一块自己那个数算出来的起止，不重算，
--- 免得点开的流水和方块上的数对不上；角色 / 类型 / 收支 / 页码由 Flows.Open 一律回默认。
function Calendar.OpenRangeFlow(preset)
    if not IsRangeBlock(preset) then return false end
    local flows = GDL and GDL.Flows
    if type(flows) ~= "table" or type(flows.Open) ~= "function" then return false end
    local slot = ui and ui.totals[preset]
    local s, e = slot and slot.startKey, slot and slot.endKey
    -- 读不出时钟的方块写的是 "?"，没有范围可筛，点了就不开（不拿显示月凑一段）。
    if type(s) ~= "string" or type(e) ~= "string" then return false end
    -- 日期下拉其余档位的锚：本周 / 上周 / 全部都是相对今天算的，锚今天；本月跟着日历
    -- 显示月走（可能不是今天所在月），锚该月首日，否则下拉里的「本月」会和点开的那段对不上。
    local anchor = s
    if preset ~= "month" then anchor = LCall("TodayKey") or state.selKey end
    local okRT, text = pcall(flows.RangeText, preset, s, e)
    if not okRT then text = nil end
    Calendar.HideCellTip()
    local okCall, opened = pcall(flows.Open, nil, s, e, text, anchor, preset)
    return okCall and opened ~= false
end

---------------------------------------------------------------------------
-- 悬停提示：格子给 日期 + 收入 / 支出 + 几个号有变动；右栏给 收入 / 支出。
-- 净不进提示（格子和行上已经写着净），分类明细不进提示（那属于点进去的弹窗）。
---------------------------------------------------------------------------

local C_TIP_WHITE = { 1, 1, 1 }

--- 提示只走这一个出口：四行在 Build 里就是同一个字体对象、同一行高，
--- 背板 alpha=1。不再碰 GameTooltip（它的半透明背板与 header/正文双字号改不动）。
local function ShowTip(host, lines, colors)
    if not ui or not ui.tip or type(host) ~= "table" then return false end
    for i, fs in ipairs(ui.tipLines) do
        if lines[i] then
            SetText(fs, lines[i])
            SetColor(fs, colors[i] or C_TIP_WHITE)
            fs:Show()
        else
            SetText(fs, "")
            fs:Hide()
        end
    end
    ui.tip:SetHeight(2 * GEOM.TIP_PAD + #lines * GEOM.TIP_ROW_H)
    ui.tip:ClearAllPoints()
    ui.tip:SetPoint("TOPLEFT", host, "TOPRIGHT", 6, 0)
    ui.tip:Show()
    return true
end

local function HideTip()
    if ui and ui.tip then ui.tip:Hide() end
end

function Calendar.ShowCellTip(cell)
    -- 先把上一次我们弹的收掉：客户端一般按 OnLeave→OnEnter 的顺序走，但**月外格**这条
    -- 是"什么都不设"就 return，不收的话上一格的提示会挂在人家脸上（用例走的就是这条序列）。
    Calendar.HideCellTip()
    state.tip.shown = false
    if type(cell) ~= "table" or not cell.inMonth then return false end   -- 月外格什么都不设
    local key = cell.dateKey
    if type(key) ~= "string" then return false end
    if not ui or not ui.tip then
        state.tip.error = "提示框还没建（窗口没打开）"
        return false
    end
    local day = LCall("GetDay", key)
    if type(day) ~= "table" then return false end
    local chars = type(day.charOrder) == "table" and #day.charOrder or 0
    local lines = {
        Fmt("TIP_DATE", key),
        Fmt("TIP_INCOME", Calendar.FormatMoneyIcon(day.income)),
        Fmt("TIP_EXPENSE", Calendar.FormatMoneyIcon(day.expense)),
        Fmt("TIP_CHARS", chars),
    }
    local okCall, err = pcall(ShowTip, cell.host, lines,
        { C_TIP_WHITE, C_POS, C_NEG, C_DIM })
    if not okCall then
        state.tip.error = "弹提示出错: " .. tostring(err)
        return false
    end
    state.tip.error = nil
    state.tip.lines = lines
    state.tip.shown = true
    return true
end

--- 下方读数方块：方块上只摆净，悬停给「档位 + 确切范围 / 收入 / 支出」三行。
--- 战团方块不挂悬停（用户 2026-11-11：悬停了也还是战团，没有额外信息）。
--- 与格子、右栏行共用同一扇自绘提示框，四行同字体、背板不透明。
function Calendar.ShowTotalsTip(slot)
    Calendar.HideCellTip()
    state.tip.shown = false
    if type(slot) ~= "table" or type(slot.preset) ~= "string" then return false end
    if not ui or not ui.tip then
        state.tip.error = "提示框还没建（窗口没打开）"
        return false
    end
    -- 档位词（本周/上周/本月/全部）单独一行，确切范围单独一行：两行都短，
    -- 不再像旧版 "%s %s~%s" 把一长串挤进同一行而换行（用户 2026-11-10 截图反馈）。
    -- 范围行只在拿得到真实起止时才画：「全部」没有记录就不写日期（用户定稿二选一）。
    -- 战团没有悬停（用户 2026-11-11）：不摆信息就不弹，避免又出现重复的「战团」一行。
    if slot.preset == "warband" then return false end
    local lines, colors
    if slot.preset == "assets" then
        local charCount = tonumber(slot.charCount) or 0
        local charText = charCount > 0 and Calendar.FormatMoneyIcon(slot.charGold) or NO_VALUE
        local warbandText = type(slot.warband) == "number"
            and Calendar.FormatMoneyIcon(slot.warband) or NO_VALUE
        lines = {
            T("SUMMARY_ASSETS"),
            Fmt("TIP_ASSETS_CHARS", charText),
            Fmt("TIP_ASSETS_WARBAND", warbandText),
            T("TIP_ASSETS_NOTE"),
        }
        colors = { C_TIP_WHITE, C_ASSET, C_ASSET, C_DIM }
    else
        lines = { T("RANGE_" .. slot.preset:upper()) }
        colors = { C_TIP_WHITE }
        if type(slot.tipStart) == "string" and type(slot.tipEnd) == "string" then
            lines[#lines + 1] = Fmt("TIP_TOTALS_RANGE", slot.tipStart, slot.tipEnd)
            colors[#colors + 1] = C_TIP_WHITE
        end
        lines[#lines + 1] = Fmt("TIP_INCOME", Calendar.FormatMoneyIcon(slot.income))
        colors[#colors + 1] = C_POS
        lines[#lines + 1] = Fmt("TIP_EXPENSE", Calendar.FormatMoneyIcon(slot.expense))
        colors[#colors + 1] = C_NEG
    end
    local okCall, err = pcall(ShowTip, slot.host, lines, colors)
    if not okCall then
        state.tip.error = "弹提示出错: " .. tostring(err)
        return false
    end
    state.tip.error = nil
    state.tip.lines = lines
    state.tip.shown = true
    return true
end

--- 右栏一行：净已经写在行上，悬停给 收入 / 支出 / 金币 三行（玩家 2026-11-11 加金币）。
--- 其他号的"金币"只能是它上次登录时的快照；没记录就写「—」，不拿 0 冒充。
function Calendar.ShowRowTip(row)
    Calendar.HideCellTip()
    state.tip.shown = false
    if type(row) ~= "table" or type(row.charKey) ~= "string" then return false end
    if not ui or not ui.tip then
        state.tip.error = "提示框还没建（窗口没打开）"
        return false
    end
    local gold = LCall("GetCharGold", row.charKey)
    local goldText = type(gold) == "number" and Calendar.FormatMoneyIcon(gold) or NO_VALUE
    local lines = {
        Fmt("TIP_INCOME", Calendar.FormatMoneyIcon(row.income)),
        Fmt("TIP_EXPENSE", Calendar.FormatMoneyIcon(row.expense)),
        Fmt("TIP_GOLD", goldText),
    }
    local okCall, err = pcall(ShowTip, row.host, lines, { C_POS, C_NEG, C_ASSET })
    if not okCall then
        state.tip.error = "弹提示出错: " .. tostring(err)
        return false
    end
    state.tip.error = nil
    state.tip.lines = lines
    state.tip.shown = true
    return true
end

function Calendar.HideCellTip()
    -- 只收我们自己这扇框（Matrix.lua:693-701 同一条理由：别人的提示不许连锅端）
    if not state.tip.shown then return false end
    state.tip.shown = false
    HideTip()
    return true
end

----------------------------------------------------------------------------
-- 反馈网址弹窗（标题旁 ? 的落点）
-- 游戏没给插件的剪贴板接口，能复制的唯一正路是一个聚焦并全选的 EditBox：
-- 弹窗一打开就全选，玩家按 Ctrl+C 即得；按钮只是重新全选，不假装写进了剪贴板。
----------------------------------------------------------------------------

local FEEDBACK_URL = "https://bbs.nga.cn/read.php?tid=47896763"

local function FocusAll(edit)
    if not edit then return end
    pcall(function()
        edit:SetFocus()
        edit:HighlightText()
    end)
end

function Calendar.ShowFeedback()
    if not feedbackFrame then
        local d = CreateFrame("Frame", "GDLFeedbackFrame", UIParent, "BackdropTemplate")
        d:SetSize(GEOM.FEEDBACK_W, GEOM.FEEDBACK_H)
        d:SetPoint("CENTER")
        -- 与主窗同一协议：DIALOG + SetToplevel，谁被点/谁最后开谁在最前。
        d:SetFrameStrata("DIALOG")
        pcall(d.SetToplevel, d, true)
        d:SetClampedToScreen(true)
        d:SetMovable(true)
        d:EnableMouse(true)
        d:RegisterForDrag("LeftButton")
        d:SetScript("OnDragStart", function(self) self:StartMoving() end)
        d:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
        d:SetBackdrop({
            bgFile = "Interface/Buttons/WHITE8X8",
            edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
            edgeSize = 16, insets = { left = 4, right = 4, top = 4, bottom = 4 },
        })
        pcall(d.SetBackdropColor, d, 0.07, 0.07, 0.09, 1)

        local title = d:CreateFontString(nil, "OVERLAY", FONT_TITLE)
        title:SetPoint("TOPLEFT", d, "TOPLEFT", GEOM.PAD, -GEOM.PAD)
        d.__title = title

        local close = AddButton(d, "UIPanelButtonTemplate")
        close:SetSize(GEOM.FEEDBACK_BTN_W, GEOM.BTN_H)
        close:SetPoint("BOTTOMRIGHT", d, "BOTTOMRIGHT", -GEOM.PAD, GEOM.PAD)
        close:SetScript("OnClick", function() Calendar.CloseFeedback() end)
        d.__close = close

        local hint = d:CreateFontString(nil, "OVERLAY", FONT_SMALL)
        hint:SetPoint("TOPLEFT", d, "TOPLEFT", GEOM.PAD, -(GEOM.PAD + GEOM.TITLE_H))
        d.__hint = hint

        -- 输入框宿主：无模板的 EditBox 自己没有边框，用一扇 BackdropTemplate
        -- 小框托住（本仓库证过的写法，不引入 InputBoxTemplate）。
        local host = CreateFrame("Frame", nil, d, "BackdropTemplate")
        host:SetPoint("TOPLEFT", d, "TOPLEFT", GEOM.PAD,
            -(GEOM.PAD + GEOM.TITLE_H + GEOM.FEEDBACK_GAP + 14))
        host:SetSize(GEOM.FEEDBACK_W - 2 * GEOM.PAD, GEOM.FEEDBACK_EDIT_H)
        host:SetBackdrop({
            bgFile = "Interface/Buttons/WHITE8X8",
            edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
            edgeSize = 12, insets = { left = 3, right = 3, top = 3, bottom = 3 },
        })
        pcall(host.SetBackdropColor, host, 0.02, 0.02, 0.03, 1)
        pcall(host.SetBackdropBorderColor, host, 0.40, 0.40, 0.45, 1)

        local edit = CreateFrame("EditBox", nil, host)
        edit:SetPoint("TOPLEFT", host, "TOPLEFT", 6, -4)
        edit:SetPoint("BOTTOMRIGHT", host, "BOTTOMRIGHT", -6, 4)
        edit:SetAutoFocus(false)
        edit:SetText(FEEDBACK_URL)
        local fontObj = _G.GameFontHighlightSmall or _G.GameFontNormal
        if fontObj then pcall(edit.SetFontObject, edit, fontObj) end
        edit:SetScript("OnEscapePressed", function(self)
            self:ClearFocus()
            Calendar.CloseFeedback()
        end)
        edit:SetScript("OnEnterPressed", function(self) FocusAll(self) end)
        d.__url = edit

        local copy = AddButton(d, "UIPanelButtonTemplate")
        copy:SetSize(GEOM.FEEDBACK_BTN_W, GEOM.BTN_H)
        copy:SetPoint("BOTTOMLEFT", d, "BOTTOMLEFT", GEOM.PAD, GEOM.PAD)
        copy:SetScript("OnClick", function() FocusAll(edit) end)
        d.__copy = copy

        local tip = d:CreateFontString(nil, "OVERLAY", FONT_SMALL)
        tip:SetPoint("BOTTOM", d, "BOTTOM", 0, GEOM.PAD + GEOM.BTN_H + GEOM.FEEDBACK_GAP)
        d.__tip = tip

        feedbackFrame = d
        -- ESC 关闭：与主窗同一张暴雪名单（帧必须有全局名）。
        local list = _G.UISpecialFrames
        if type(list) == "table" then
            local already = false
            for i = 1, #list do
                if list[i] == "GDLFeedbackFrame" then already = true end
            end
            if not already then table.insert(list, "GDLFeedbackFrame") end
        end
    end

    -- 文字每次显示都重摆：运行中换语言不用重开窗。
    SetText(feedbackFrame.__title, T("FEEDBACK_TITLE"))
    SetText(feedbackFrame.__hint, T("FEEDBACK_HINT"))
    SetText(feedbackFrame.__copy, T("FEEDBACK_COPY"))
    SetText(feedbackFrame.__close, T("FEEDBACK_CLOSE"))
    SetText(feedbackFrame.__tip, T("FEEDBACK_TIP"))

    feedbackFrame:Show()
    if type(GDL) == "table" and type(GDL.RaiseFront) == "function" then
        pcall(GDL.RaiseFront, feedbackFrame, feedbackFrame.__close)
    end
    FocusAll(feedbackFrame.__url)
    return true
end

function Calendar.CloseFeedback()
    if feedbackFrame then feedbackFrame:Hide() end
    return true
end

---------------------------------------------------------------------------
-- 开关窗（刷新只在这里发生）
---------------------------------------------------------------------------

function Calendar.Open()
    local okCall, err = pcall(Build)
    if not okCall then
        NoteError("Build", err)
        print("GDL: 日历窗口建不起来：" .. tostring(err))
        return false
    end
    -- 每次开窗回到「本日 + 今天」（不记忆上次档位，计划 §7.6）
    local today = LCall("TodayKey")
    local y, m = PartsOf(today)
    if y then state.year, state.month = y, m end
    state.selKey = today
    state.preset = "day"
    state.startKey, state.endKey = today, today
    state.panelCount, state.scrollY = 0, 0
    frame:Show()
    -- 离线替身不派发 OnShow，生产客户端会；两条路都走到同一句抬层，别只靠脚本。
    if type(GDL) == "table" and type(GDL.RaiseFront) == "function" then
        pcall(GDL.RaiseFront, frame, ui and ui.close)
    end
    Calendar.Refresh()
    return true
end

function Calendar.Close()
    if frame then frame:Hide() end
    -- 提示框是 UIParent 的子帧，不随主窗 Hide 一起消失 ⇒ 关窗必须自己收。
    state.tip.shown = false
    HideTip()
    -- 反馈弹窗同理（UIParent 子帧）：关主窗时一起收，别留一扇孤儿窗。
    Calendar.CloseFeedback()
    return true
end

function Calendar.Toggle()
    if frame and frame:IsShown() then return Calendar.Close() end
    return Calendar.Open()
end

---------------------------------------------------------------------------
-- /gdl probe ui：只读取证，不改任何状态；窗开没开都不许报错。
-- 这些都是"我们怎么知道的"，一条都不进界面（用户的硬规矩）。
---------------------------------------------------------------------------

local function ProbeDescribe(fn, label)
    local okCall, v = pcall(fn)
    if not okCall then return label .. "=问不出(" .. tostring(v) .. ")" end
    if v == nil then return label .. "=nil" end
    return label .. "=" .. tostring(v)
end

local function Measure(fs)
    if not fs or type(fs.GetStringWidth) ~= "function" then return nil end
    local okCall, w = pcall(fs.GetStringWidth, fs)
    if not okCall then return nil end
    return tonumber(w)
end

--- 一条字符串用暗尺量宽（提示框四行同字体，按同一把尺估）。
local function MeasureText(meas, text)
    if not meas or type(meas.SetText) ~= "function"
        or type(meas.GetStringWidth) ~= "function" then return nil end
    pcall(meas.SetText, meas, tostring(text))
    local w = Measure(meas)
    pcall(meas.SetText, meas, "")
    return w
end

local function PrintUIProbe(rest)
    print("GDL probe ui: 界面几何取证（只读，不改状态）")
    print(("GDL probe ui: 常量 WIN_W=%d WIN_H=%d CELL_W=%d CELL_H=%d GRID_ROWS=%d "
        .. "PANEL_W=%d PANEL_X=%d PANEL_LIST_H=%d PANEL_ROWS=%d ROW_H=%d PAD=%d NAV_H=%d")
        :format(GEOM.WIN_W, GEOM.WIN_H, GEOM.CELL_W, GEOM.CELL_H, GEOM.GRID_ROWS,
            GEOM.PANEL_W, GEOM.PANEL_X, GEOM.PANEL_LIST_H, GEOM.PANEL_ROWS,
            GEOM.ROW_H, GEOM.PAD, GEOM.NAV_H))

    local cd, why = Calendar.WeekResetCountdown()
    print(("GDL probe ui: 周CD接口 %s（答 %s）本次采用来源=%s")
          :format(WEEK_RESET_API, tostring(cd), tostring(state.weekCD and state.weekCD.source)))
    print(("GDL probe ui: 周CD退化原因（没退化则为 nil）=%s 界面不许显示这一条")
          :format(tostring(state.weekCD and state.weekCD.reason)))

    if not built then
        print("GDL probe ui: 窗口还没建 —— 先 /gdl open 再跑一次才有几何")
        return
    end

    print(("GDL probe ui: 帧 %s %s %s %s %s ESC名单=%s")
          :format(tostring(_G.GDLCalFrame == frame and "GDLCalFrame(全局名可解析)" or "GDLCalFrame?"),
            ProbeDescribe(function() return frame:GetWidth() end, "宽"),
            ProbeDescribe(function() return frame:GetHeight() end, "高"),
            ProbeDescribe(function() return frame:GetFrameStrata() end, "strata"),
            ProbeDescribe(function() return frame:GetFrameLevel() end, "层号"),
            ProbeDescribe(function()
                local list = _G.UISpecialFrames
                if type(list) ~= "table" then return nil end
                for i = 1, #list do
                    if list[i] == "GDLCalFrame" then return "已挂" end
                end
                return "不在名单里"
            end, "UISpecialFrames")))
    -- 矩形：本仓库证过的只有 GetCenter + GetWidth/GetHeight（Matrix.lua:956/1251），
    -- GetRect 指不出调用点 ⇒ 用证过的这三样拼中心+宽高，不写没证过的方法名。
    print(ProbeDescribe(function()
        local cx, cy = frame:GetCenter()
        return ("中心=%s,%s 宽=%s 高=%s"):format(tostring(cx), tostring(cy),
            tostring(frame:GetWidth()), tostring(frame:GetHeight()))
    end, "GDL probe ui: 帧位置尺寸"))

    print(("GDL probe ui: 显示 %s 今天=%s 首格=%s 首日星期(1=周日)=%s 月长=%s 用出行数=%s")
          :format(tostring(state.year), tostring(LCall("TodayKey")),
            tostring(ui.gridInfo and ui.gridInfo.firstKey),
            tostring(ui.gridInfo and ui.gridInfo.firstWday),
            tostring(ui.gridInfo and ui.gridInfo.dim),
            tostring(ui.gridInfo and ui.gridInfo.rowsNeeded)))
    print(("GDL probe ui: 范围 选中=%s 档=%s %s..%s 右栏行=%d 滚动=%s")
          :format(tostring(state.selKey), tostring(state.preset),
            tostring(state.startKey), tostring(state.endKey),
            tonumber(state.panelCount) or 0, tostring(state.scrollY)))
    print(("GDL probe ui: 右栏 滚动区高=%s 内容高=%s 可见行=%d")
          :format(tostring(ui.scroll and ui.scroll:GetHeight()),
            tostring(ui.content and ui.content:GetHeight()), GEOM.PANEL_ROWS))
    -- 三格页签当前哪一格是选中态：高矮 / 底沿金线 / 字，界面只看得出来、说不清楚时查这里。
    for _, preset in ipairs(PANEL_PRESETS) do
        local tab = ui.presets[preset]
        print(("GDL probe ui: 页签 %s 选中=%s 高=%s 底沿金线=%s 字=%s")
              :format(preset, tostring(tab.active), tostring(tab:GetHeight()),
                tostring(tab.mark:IsShown()), tostring(tab.label:GetText())))
    end

    -- 四个范围方块能不能点、点开的是哪一段：界面只看得出来、说不清楚时查这里。
    for _, preset in ipairs(TOTALS_PRESETS) do
        local slot = ui.totals[preset]
        print(("GDL probe ui: 读数块 %s 可点=%s 范围=%s..%s")
              :format(preset, tostring(slot.host:GetScript("OnClick") ~= nil),
                tostring(slot.startKey), tostring(slot.endKey)))
    end

    -- 每格矩形（相对格主机的算式，客户端不给矩形就只能这样对）+ 两行字的实测宽度
    for i, cell in ipairs(ui.cells) do
        if cell.used then
            local l = (cell.col - 1) * GEOM.CELL_W + 1
            local t = -((cell.row - 1) * GEOM.CELL_H + 1)
            print(("GDL probe ui: 格%d r%d c%d 键=%s 月内=%s 今天=%s 选中=%s x=[%d..%d] y=[%d..%d] 宽=%s 净=%s")
                  :format(i, cell.row, cell.col, tostring(cell.dateKey),
                      tostring(cell.inMonth), tostring(cell.today), tostring(cell.selected),
                      l, l + GEOM.CELL_W - 2, t, t - (GEOM.CELL_H - 2),
                      tostring(Measure(cell.num)), tostring(Measure(cell.net))))
        end
    end

    -- 每种字体一条：文本原样 + 客户端实测宽度 + 建串时传的字体对象名（第 3 参就是它）
    for _, f in ipairs(ui.fonts) do
        local w = Measure(f.fs)
        local okT, txt = pcall(f.fs.GetText, f.fs)
        print(("GDL probe ui: 字体 角色=%s 字体名=%s 文本=%s 量宽=%s")
              :format(f.role, tostring(f.font), okT and tostring(txt) or "?", tostring(w)))
    end

    -- 自绘提示框那几行：四行同一个字体对象，暗尺按 FONT_TIP 估宽。
    if type(state.tip.lines) == "table" and #state.tip.lines > 0 then
        for i, line in ipairs(state.tip.lines) do
            print(("GDL probe ui: 提示行%d 文本=%s 量宽=%s（暗尺按 %s 估）")
                  :format(i, line, tostring(MeasureText(ui.meas, line)), FONT_TIP))
        end
    else
        print("GDL probe ui: 提示行：本次会话还没悬停过月内的格子，量不到")
    end
    print(ProbeDescribe(function()
        if not ui.tip then return nil end
        return ("%sx%s"):format(tostring(ui.tip:GetWidth()), tostring(ui.tip:GetHeight()))
    end, "GDL probe ui: 自绘提示框尺寸"))
    print(ProbeDescribe(function()
        return ui.close:GetFrameLevel()
    end, "GDL probe ui: × 的层号（必须=主帧层号+1，模板自带 510 会浮到别人上面）"))

    local errs = state.errors
    if #errs > 0 then
        print(("GDL probe ui: 最近 %d 次绘制/建窗报错：")
              :format(#errs))
        for i = 1, #errs do print("  " .. tostring(errs[i])) end
    else
        print("GDL probe ui: 绘制没报错")
    end
end

---------------------------------------------------------------------------
-- 命令
---------------------------------------------------------------------------

if GDL.RegisterCommand then
    GDL.RegisterCommand("open", function() return Calendar.Toggle() end, "打开/关闭日历窗口")

    -- 「probe ui」：Money 已占用 probe 这个子命令，这里按参数分流，不动记账层的取证段。
    -- 参数是 ui 才走界面取证，其余（含空参数）原样转给上一层的 handler。
    local cmd = GDL.commands and GDL.commands["probe"]
    if type(cmd) == "table" and type(cmd.fn) == "function" and not cmd.__uiChained then
        local prev = cmd.fn
        cmd.fn = function(rest)
            if type(rest) == "string" and rest:match("^ui") then return PrintUIProbe(rest) end
            return prev(rest)
        end
        cmd.__uiChained = true
    else
        GDL.RegisterCommand("probeui", PrintUIProbe, "取证：界面几何（等价 /gdl probe ui）")
    end
end

return Calendar
