-- 日历界面（T3）：一扇窗、三级钻取（月历 → 这天的号 → 某号这段的流水），版式照计划 §3 的草图。
--
-- 三条纪律，都有用例钉着（tests/run_gcl_tests.py 的 test_calendar_*）：
--   1. 只在"开窗"和"点击"时算：这个文件里没有每帧脚本、没有定时器、不订任何事件，
--      读数全部走 Ledger 的查表接口（计划 §3 末条）。
--   2. 界面上只放玩家这一眼要用的东西；凡是"我们怎么知道的"这类证据一律进 /gcl probe ui。
--   3. 组件只用本仓库已在用户 12.1.5 客户端上跑通的写法：
--        窗口   = GoldQuestLedger/Matrix.lua:1548-1585（CreateFrame + BackdropTemplate + 拖动 + 不透明深灰）
--        关闭   = GoldQuestLedger/Matrix.lua:1597-1605（UIPanelCloseButton 的 × 自带 frameLevel=510，必须钉回主帧+1）
--        按钮   = GoldQuestLedger/Matrix.lua:1633-1636（UIPanelButtonTemplate + SetText + OnClick）
--        热区   = GoldQuestLedger/Matrix.lua:358-385（Button + RegisterForClicks，池化复用）
--        提示   = GoldQuestLedger/Matrix.lua:481-700 + 2451-2465（OnEnter/OnLeave + GameTooltip，守卫 + pcall）
--        ESC    = GoldQuestLedger/Config.lua:272-283（UISpecialFrames 挂的是帧的**全局名**）
--        金额   = GoldQuestLedger/Config.lua:60-86（|T 转义必须三段式 路径:高:宽|t，两段会原样印成文本）
--      一行都不拷，只照同一形状调用。

local GCL = _G.GCL
local Calendar = {}

if GCL and GCL.RegisterModule then
    GCL.RegisterModule("Calendar", Calendar)
else
    GCL = GCL or {}
    GCL.Calendar = Calendar
end

---------------------------------------------------------------------------
-- 几何常量：**全部是临时的**。格子/字号要等用户在真机跑 /gcl probe ui，
-- 用客户端实际量回来的文字宽度定，不照像素猜（计划 §3"格子尺寸先用探针量文字实际宽度再定"）。
-- 除此之外这是唯一一处版面尺寸，界面各处只从这里取值。
---------------------------------------------------------------------------

local GEOM = {
    PAD          = 12,   -- 窗内四周留白
    TITLE_H      = 22,   -- 标题行
    NAV_H        = 26,   -- ‹ 月份 › + 顶栏三个读数（草图这一行是一行的）
    WD_H         = 18,   -- 星期表头
    CELL_W       = 88,   -- = (WIN_W - 2*PAD) / 7
    CELL_H       = 42,   -- 一格三行字：日号 / 净 / 小字 收·支
    GRID_ROWS    = 6,    -- 6 行 × 7 列 = 42 格（月历最长形态）
    DIV_H        = 12,   -- 月历与下半区之间的分隔带
    PRESET_H     = 26,   -- 四颗预设按钮（+ 流水页的翻页/返回，同一行右侧）
    LOWER_HEAD_H = 20,   -- 下半区列表表头
    ROW_H        = 20,   -- 下半区一行
    CHARS_ROWS   = 6,    -- 号列表可见行（超出的号数进"另有 N 个号未列出"，合计行仍是全量）
    SUB_NOTE     = 1,    -- 溢出提示占一行
    TOTAL_ROW    = 1,    -- 合计占一行
    FLOW_ROWS    = 5,    -- 流水页可见行
    SUB_HEAD     = 1,    -- 分类小计的表头占一行
    SUB_ROWS     = 3,    -- 分类小计可见行数（8 类 + 2 个搬钱来源，最多 10 类，超了进提示）

    -- 控件尺寸与列位也在这一块里（用例 test_calendar_static_guards 会扫全文：
    -- 版式段落之外不许再出现裸像素常量，否则改一处版式要满文件找数）。
    BTN_H        = 22,   -- 通用按钮高
    MO_BTN_W     = 22,   -- ‹ ›
    MO_BTN_GAP   = 2,
    MO_LABEL_W   = 96,   -- 「2026年11月」这一段留给多宽
    MO_LABEL_GAP = 6,
    BAND_W       = 140,  -- 顶栏一个读数（标签+数值）的带宽
    BAND_GAP     = 6,
    PSET_W       = 72,   -- 四颗范围预设按钮
    PSET_GAP     = 4,
    NAV_W        = 64,   -- 上一页 / 下一页 / 返回
    NAV_GAP      = 4,
    ROW_BTN_H    = 18,   -- 号列表行内那唯一一颗「流水」
    CLOSE_INSET  = 6,    -- × 距窗角

    -- 下半区列位的右边界（数字一律右对齐，用 TOPRIGHT 锚，不给宽度约束：
    -- 字体串没定宽时 SetJustifyH 在客户端不起作用，右锚才是本仓库量得准的做法）。
    COLS = {
        name = 4, inc = 262, exp = 382, net = 482, btn = 494, btnW = 82,
        time = 4, src = 214, amt = 404, note = 416,
    },
}
GEOM.LOWER_ROWS = 1 + GEOM.CHARS_ROWS + GEOM.SUB_NOTE + GEOM.TOTAL_ROW   -- 号列表页 = 表头 + 号行 + 提示 + 合计
GEOM.FLOW_SLOTS = 1 + GEOM.FLOW_ROWS + GEOM.SUB_HEAD + GEOM.SUB_ROWS     -- 流水页 = 表头 + 行 + 小计头 + 小计行
GEOM.WIN_W = GEOM.PAD * 2 + GEOM.CELL_W * 7
GEOM.WIN_H = GEOM.PAD + GEOM.TITLE_H + GEOM.NAV_H + GEOM.WD_H
    + GEOM.GRID_ROWS * GEOM.CELL_H + GEOM.DIV_H + GEOM.PRESET_H
    + GEOM.LOWER_HEAD_H + math.max(GEOM.LOWER_ROWS, GEOM.FLOW_SLOTS) * GEOM.ROW_H + GEOM.PAD
-- 高度预算（Matrix.lua:1535-1537 那条用户实测要求的同款口径）：**不许比屏幕 85% 还高**。
-- 屏幕按 720 高这一档算 ⇒ 上限 612；用例 test_calendar_geometry_budget 钉这个数，
-- 改上面任何常量都会把它顶爆，那时要么减可见行、要么进滚动区（本轮不做滚动区）。
GEOM.MAX_WIN_H = math.floor(720 * 0.85)
-- 顶栏三个读数的起点：三颗控件（‹ › + 月标题）之后才是读数。
GEOM.TOTALS_LEFT = GEOM.PAD + GEOM.MO_BTN_W + GEOM.MO_BTN_GAP + GEOM.MO_BTN_W
    + GEOM.MO_LABEL_GAP + GEOM.MO_LABEL_W
Calendar.GEOM = GEOM

local COLS = GEOM.COLS

local FLOW_ROWS   = GEOM.FLOW_ROWS
Calendar.PER_PAGE = FLOW_ROWS        -- 一页 = 一屏可见行，翻页按钮因此天然对齐

---------------------------------------------------------------------------
-- 颜色（净：正绿负红；计划 §3 草图）
---------------------------------------------------------------------------

local C_POS = { 0.24, 0.78, 0.32 }
local C_NEG = { 0.86, 0.28, 0.26 }
local C_ZERO = { 0.62, 0.62, 0.62 }
local C_TEXT = { 1, 0.92, 0.60 }        -- 当前号那行的标记色（琥珀，和矩阵的强调色一路）
local C_DIM = { 0.62, 0.62, 0.62 }

local FONT_TITLE = "GameFontHighlight"       -- Matrix.lua:1587
local FONT_LABEL = "GameFontNormal"          -- Options.lua:343
local FONT_BODY  = "GameFontHighlightSmall"  -- Matrix.lua:1695
local FONT_SMALL = "GameFontNormalSmall"     -- LogWindow.lua:23

---------------------------------------------------------------------------
-- 文本与数值格式
---------------------------------------------------------------------------

--- 取文本：Locale 没加载（toc 顺序被改 / Core 报错中断）时原样回键，界面不许因为缺串炸。
local function T(key)
    local loc = GCL and GCL.Locale
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

local function SrcName(src)
    local loc = GCL and GCL.Locale
    if type(loc) == "table" and type(loc.SrcName) == "function" then
        local okCall, r = pcall(loc.SrcName, src)
        if okCall and type(r) == "string" and r ~= "" then return r end
    end
    return tostring(src)
end

-- |T 转义必须写成 路径:高:宽|t 三段：只写高不写宽客户端不认，会把整串当普通文本原样印出来
-- （用户在截图里撞过，见 GoldQuestLedger/Config.lua:66-73）。
local ICON_GOLD   = "|TInterface\\MoneyFrame\\UI-GoldIcon:12:12|t"
local ICON_SILVER = "|TInterface\\MoneyFrame\\UI-SilverIcon:12:12|t"
local ICON_COPPER = "|TInterface\\MoneyFrame\\UI-CopperIcon:12:12|t"

--- 图标形式（顶栏三个读数、流水金额用它）。非数字回 "?"：与 0（真没有钱）严格区分。
function Calendar.FormatMoneyIcon(copper)
    if type(copper) ~= "number" then return "?" end
    local sign = copper < 0 and "-" or ""
    local v = math.floor(copper < 0 and -copper or copper)
    local g = math.floor(v / 10000)
    local s = math.floor((v % 10000) / 100)
    local c = v % 100
    if g > 0 then return sign .. ("%d%s%d%s"):format(g, ICON_GOLD, s, ICON_SILVER) end
    if s > 0 then return sign .. ("%d%s%d%s"):format(s, ICON_SILVER, c, ICON_COPPER) end
    return sign .. ("%d%s"):format(c, ICON_COPPER)
end

--- 纯文本形式（probe / 日志用，同 GoldQuestLedger/Config.lua:60-64 的口径）。
function Calendar.FormatMoneyPlain(copper)
    if type(copper) ~= "number" then return "?" end
    local sign = copper < 0 and "-" or ""
    local v = math.floor(copper < 0 and -copper or copper)
    return sign .. ("%dg%ds%dc"):format(math.floor(v / 10000),
        math.floor((v % 10000) / 100), v % 100)
end

--- 缩写形式（格子里"收·支"那行小字用它：图标形式两个并排就超出临时格宽）。
function Calendar.FormatMoneyCompact(copper)
    if type(copper) ~= "number" then return "?" end
    local sign = copper < 0 and "-" or ""
    local v = math.floor(copper < 0 and -copper or copper)
    if v >= 10000 then return ("%s%.1f%s"):format(sign, v / 10000, T("UNIT_GOLD")) end
    if v >= 100 then return ("%s%d%s"):format(sign, math.floor(v / 100), T("UNIT_SILVER")) end
    return ("%s%d%s"):format(sign, v, T("UNIT_COPPER"))
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
    local l = GCL and GCL.Ledger
    return type(l) == "table" and type(l[name]) == "function"
end

local function LCall(name, ...)
    if not LedgerOK(name) then return nil end
    local l = GCL.Ledger
    local okCall, r = pcall(l[name], ...)
    if not okCall then return nil end
    return r
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
    preset = "day",                   -- day / week / month / all / sel
    startKey = nil, endKey = nil,     -- 当前范围（含端点）
    selKey = nil,                     -- 点选的某一天
    page = "chars",                   -- chars / flow
    flowChar = nil, flowPage = 1,
    weekCD = { source = "none", seconds = nil, reason = nil },
    tip = { lines = nil, error = nil, shown = false },
    errors = {},
}
Calendar.GetState = function() return state end

local frame, ui, built
Calendar.GetUI = function() return ui end

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
--- 并把原因只记在这里（界面不加字，取证在 /gcl probe ui）。
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
-- 范围预设
---------------------------------------------------------------------------

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
    if preset == "sel" then
        local k = state.selKey or LCall("TodayKey")
        if not k then return nil, nil end
        return k, k
    end
    -- 默认档「本日」
    local today = LCall("TodayKey")
    if not today then return nil, nil end
    return today, today
end

--- 切预设：范围对上，下半区回到号列表页（流水页只由「流水」按钮进、「返回」出）。
function Calendar.SelectPreset(preset)
    local s, e = Calendar.GetRange(preset)
    if not s then return false end
    state.preset, state.startKey, state.endKey = preset, s, e
    if preset == "day" then state.selKey = s end
    state.page = "chars"
    state.flowChar, state.flowPage = nil, 1
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
    SetText(ui.btnPrevMo, T("MO_PREV"))
    SetText(ui.btnNextMo, T("MO_NEXT"))
    for c = 1, 7 do
        SetText(ui.weekdays[c], T("WD_" .. c))
    end
    for _, preset in ipairs({ "week", "month", "all" }) do
        local slot = ui.totals[preset]
        if slot then SetText(slot.label, T("RANGE_" .. preset:upper())) end
    end
    for _, preset in ipairs({ "day", "week", "month", "all" }) do
        SetText(ui.presets[preset], T("RANGE_" .. preset:upper()))
    end
    SetText(ui.nav.prev, T("BTN_PREV_PAGE"))
    SetText(ui.nav.next, T("BTN_NEXT_PAGE"))
    SetText(ui.nav.back, T("BTN_BACK"))
    SetText(ui.charHead.name, T("COL_CHAR"))
    SetText(ui.charHead.inc, T("COL_INCOME"))
    SetText(ui.charHead.exp, T("COL_EXPENSE"))
    SetText(ui.charHead.net, T("COL_NET"))
    SetText(ui.flowHead.time, T("COL_TIME"))
    SetText(ui.flowHead.src, T("COL_SOURCE"))
    SetText(ui.flowHead.amt, T("COL_AMOUNT"))
    SetText(ui.subHead, T("COL_SUBTOTALS"))
    SetText(ui.totalRow.name, T("COL_TOTAL"))
    for i = 1, GEOM.CHARS_ROWS do
        SetText(ui.charRows[i].btn, T("BTN_FLOW"))
    end
end

local function Build()
    if built then return true end
    ui = { fonts = {}, cells = {}, charRows = {}, flowRows = {}, subRows = {}, totals = {} }

    frame = CreateFrame("Frame", "GCLCalFrame", UIParent, "BackdropTemplate")
    frame:SetSize(GEOM.WIN_W, GEOM.WIN_H)
    frame:SetPoint("CENTER")
    frame:SetFrameStrata("MEDIUM")
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

    -- ESC 关闭：挂暴雪名单。名单里存的是**全局名字符串**，客户端拿它去 _G 里找帧，
    -- 所以帧必须有全局名（GCLCalFrame），这一条有用例钉着（Matrix.lua:1607-1609 同口径）。
    local list = _G.UISpecialFrames
    if type(list) == "table" then
        local already = false
        for i = 1, #list do
            if list[i] == "GCLCalFrame" then already = true end
        end
        if not already then table.insert(list, "GCLCalFrame") end
    end

    -- 标题行
    ui.title = AddFont("标题", frame, FONT_TITLE)
    ui.title:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -GEOM.PAD)

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

    for i, preset in ipairs({ "week", "month", "all" }) do
        local left = GEOM.TOTALS_LEFT + (i - 1) * (GEOM.BAND_W + GEOM.BAND_GAP)
        local label = AddFont("读数" .. preset .. "标签", frame, FONT_SMALL)
        label:SetPoint("TOPLEFT", frame, "TOPLEFT", left, -(GEOM.PAD + GEOM.TITLE_H + 4))
        local value = AddFont("读数" .. preset .. "数值", frame, FONT_BODY)
        value:SetPoint("TOPRIGHT", frame, "TOPLEFT", left + GEOM.BAND_W, -(GEOM.PAD + GEOM.TITLE_H + 4))
        ui.totals[preset] = { label = label, value = value, left = left, right = left + GEOM.BAND_W }
    end

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
        cell.num = AddFont("格" .. i .. "日号", host, FONT_BODY)
        cell.num:SetPoint("TOPLEFT", host, "TOPLEFT", 4, -3)
        cell.net = AddFont("格" .. i .. "净", host, FONT_BODY)
        cell.net:SetPoint("TOP", host, "TOP", 0, -17)
        cell.sub = AddFont("格" .. i .. "收支", host, FONT_SMALL)
        cell.sub:SetPoint("BOTTOM", host, "BOTTOM", 0, 3)

        -- 点击 = 选中这一天（下半区换成这天的号列表）；悬停 = 日期 + 收/支/净 + 几个号有变动。
        host.__gclCell = cell
        host:SetScript("OnClick", function(self) Calendar.SelectDay(self.__gclCell.dateKey) end)
        host:SetScript("OnEnter", function(self) Calendar.ShowCellTip(self.__gclCell) end)
        host:SetScript("OnLeave", function() Calendar.HideCellTip() end)
        ui.cells[i] = cell
    end

    -- 分隔线（Matrix.lua:1695-1702 那种 1 像素横线）
    local divY = ui.gridTop + GEOM.CELL_H * GEOM.GRID_ROWS + math.floor(GEOM.DIV_H / 2)
    ui.div = frame:CreateTexture(nil, "ARTWORK")
    pcall(ui.div.SetColorTexture, ui.div, 1, 1, 1, 0.15)
    ui.div:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -divY)
    ui.div:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -GEOM.PAD, -divY)
    ui.div:SetHeight(1)

    -- 预设行：四颗范围按钮 + 流水页的三颗（同右-left 排，翻页/返回只在流水页显示）
    local presetY = ui.gridTop + GEOM.CELL_H * GEOM.GRID_ROWS + GEOM.DIV_H
    ui.presets = {}
    local x = GEOM.PAD
    for _, preset in ipairs({ "day", "week", "month", "all" }) do
        local b = AddButton(frame, "UIPanelButtonTemplate")
        b:SetSize(GEOM.PSET_W, GEOM.BTN_H)
        b:SetPoint("TOPLEFT", frame, "TOPLEFT", x, -presetY)
        b.__gclPreset = preset
        b:SetScript("OnClick", function(self) Calendar.SelectPreset(self.__gclPreset) end)
        ui.presets[preset] = b
        x = x + GEOM.PSET_W + GEOM.PSET_GAP
    end

    ui.pageInfo = AddFont("页码", frame, FONT_SMALL)
    ui.pageInfo:SetPoint("TOPLEFT", frame, "TOPLEFT", x + 6, -(presetY + GEOM.BTN_H))

    -- 流水页导航（右上角）：上一页 / 下一页 / 返回
    ui.nav = {}
    local navX = GEOM.WIN_W - GEOM.PAD
    for _, spec in ipairs({ "back", "next", "prev" }) do
        local b = AddButton(frame, "UIPanelButtonTemplate")
        b:SetSize(GEOM.NAV_W, GEOM.BTN_H)
        b:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -(GEOM.WIN_W - navX), -presetY)
        if spec == "back" then
            b:SetScript("OnClick", function() Calendar.Back() end)
        elseif spec == "next" then
            b:SetScript("OnClick", function() Calendar.ShiftPage(1) end)
        else
            b:SetScript("OnClick", function() Calendar.ShiftPage(-1) end)
        end
        ui.nav[spec] = b
        navX = navX - GEOM.NAV_W - GEOM.NAV_GAP
    end

    -- 下半区
    local lowerY = presetY + GEOM.PRESET_H
    ui.lowerTop = lowerY
    ui.listHead = CreateFrame("Frame", nil, frame)
    ui.listHead:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -lowerY)
    ui.listHead:SetSize(GEOM.CELL_W * 7, GEOM.LOWER_HEAD_H + GEOM.ROW_H * math.max(GEOM.LOWER_ROWS, GEOM.FLOW_SLOTS))

    ui.charHead = { name = AddFont("列头号名", ui.listHead, FONT_SMALL),
                    inc = AddFont("列头收入", ui.listHead, FONT_SMALL),
                    exp = AddFont("列头支出", ui.listHead, FONT_SMALL),
                    net = AddFont("列头净", ui.listHead, FONT_SMALL) }
    ui.charHead.name:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.name, -2)
    ui.charHead.inc:SetPoint("TOPRIGHT", ui.listHead, "TOPLEFT", COLS.inc, -2)
    ui.charHead.exp:SetPoint("TOPRIGHT", ui.listHead, "TOPLEFT", COLS.exp, -2)
    ui.charHead.net:SetPoint("TOPRIGHT", ui.listHead, "TOPLEFT", COLS.net, -2)

    ui.flowHead = { time = AddFont("列头时间", ui.listHead, FONT_SMALL),
                    src = AddFont("列头分类", ui.listHead, FONT_SMALL),
                    amt = AddFont("列头金额", ui.listHead, FONT_SMALL) }
    ui.flowHead.time:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.time, -2)
    ui.flowHead.src:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.src, -2)
    ui.flowHead.amt:SetPoint("TOPRIGHT", ui.listHead, "TOPLEFT", COLS.amt, -2)

    -- 号列表：每行一颗「流水」，不多不少（管理类列表每行一颗按钮是用户定的硬规矩）
    for i = 1, GEOM.CHARS_ROWS do
        local host = CreateFrame("Frame", nil, ui.listHead)
        host:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", 0, -(GEOM.LOWER_HEAD_H + (i - 1) * GEOM.ROW_H))
        host:SetSize(GEOM.CELL_W * 7, GEOM.ROW_H)
        local row = { host = host, charKey = nil }
        row.name = AddFont("号行" .. i .. "名", host, FONT_BODY)
        row.name:SetPoint("LEFT", host, "LEFT", COLS.name, 0)
        row.inc = AddFont("号行" .. i .. "收入", host, FONT_BODY)
        row.inc:SetPoint("RIGHT", host, "TOPLEFT", COLS.inc, 0)
        row.exp = AddFont("号行" .. i .. "支出", host, FONT_BODY)
        row.exp:SetPoint("RIGHT", host, "TOPLEFT", COLS.exp, 0)
        row.net = AddFont("号行" .. i .. "净", host, FONT_BODY)
        row.net:SetPoint("RIGHT", host, "TOPLEFT", COLS.net, 0)
        row.btn = AddButton(host, "UIPanelButtonTemplate")
        row.btn:SetSize(COLS.btnW, GEOM.ROW_BTN_H)
        row.btn:SetPoint("TOPLEFT", host, "TOPLEFT", COLS.btn, 1)
        row.btn.__gclRow = row
        row.btn:SetScript("OnClick", function(self) Calendar.OpenFlow(self.__gclRow.charKey) end)
        ui.charRows[i] = row
    end

    ui.moreChars = AddFont("溢出号", ui.listHead, FONT_SMALL)
    ui.moreChars:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.name,
        -(GEOM.LOWER_HEAD_H + GEOM.CHARS_ROWS * GEOM.ROW_H))

    ui.totalRow = { name = AddFont("合计名", ui.listHead, FONT_BODY),
                    inc = AddFont("合计收入", ui.listHead, FONT_BODY),
                    exp = AddFont("合计支出", ui.listHead, FONT_BODY),
                    net = AddFont("合计净", ui.listHead, FONT_BODY) }
    local totalY = lowerY + GEOM.LOWER_HEAD_H + (GEOM.CHARS_ROWS + GEOM.SUB_NOTE) * GEOM.ROW_H
    ui.totalRow.name:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + COLS.name, -totalY)
    ui.totalRow.inc:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.PAD + COLS.inc, -totalY)
    ui.totalRow.exp:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.PAD + COLS.exp, -totalY)
    ui.totalRow.net:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.PAD + COLS.net, -totalY)

    -- 流水页：时间 | 分类 | 金额（+ 条目真带到的邮件标题）
    for i = 1, GEOM.FLOW_ROWS do
        local host = CreateFrame("Frame", nil, ui.listHead)
        host:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", 0,
            -(GEOM.LOWER_HEAD_H + (i - 1) * GEOM.ROW_H))
        host:SetSize(GEOM.CELL_W * 7, GEOM.ROW_H)
        local row = { host = host }
        row.time = AddFont("流水" .. i .. "时间", host, FONT_SMALL)
        row.time:SetPoint("LEFT", host, "LEFT", COLS.time, 0)
        row.src = AddFont("流水" .. i .. "分类", host, FONT_BODY)
        row.src:SetPoint("LEFT", host, "LEFT", COLS.src, 0)
        row.amt = AddFont("流水" .. i .. "金额", host, FONT_BODY)
        row.amt:SetPoint("RIGHT", host, "TOPLEFT", COLS.amt, 0)
        row.note = AddFont("流水" .. i .. "备注", host, FONT_SMALL)
        row.note:SetPoint("LEFT", host, "LEFT", COLS.note, 0)
        ui.flowRows[i] = row
    end

    ui.subHead = AddFont("小计头", ui.listHead, FONT_SMALL)
    ui.subHead:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.name,
        -(GEOM.LOWER_HEAD_H + GEOM.FLOW_ROWS * GEOM.ROW_H))

    for i = 1, GEOM.SUB_ROWS do
        local host = CreateFrame("Frame", nil, ui.listHead)
        host:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", 0,
            -(GEOM.LOWER_HEAD_H + (GEOM.FLOW_ROWS + GEOM.SUB_HEAD + i - 1) * GEOM.ROW_H))
        host:SetSize(GEOM.CELL_W * 7, GEOM.ROW_H)
        local row = { host = host }
        row.label = AddFont("小计" .. i .. "名", host, FONT_BODY)
        row.label:SetPoint("LEFT", host, "LEFT", COLS.name, 0)
        row.value = AddFont("小计" .. i .. "值", host, FONT_BODY)
        row.value:SetPoint("RIGHT", host, "TOPLEFT", COLS.net, 0)
        ui.subRows[i] = row
    end

    ui.moreSources = AddFont("溢出类", ui.listHead, FONT_SMALL)
    ui.moreSources:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.inc,
        -(GEOM.LOWER_HEAD_H + (GEOM.FLOW_ROWS + GEOM.SUB_HEAD + GEOM.SUB_ROWS) * GEOM.ROW_H))

    ui.empty = AddFont("空态", ui.listHead, FONT_LABEL)
    ui.empty:SetPoint("TOPLEFT", ui.listHead, "TOPLEFT", COLS.name,
        -(GEOM.LOWER_HEAD_H + GEOM.ROW_H))

    -- 暗尺：/gcl probe ui 量 GameTooltip 那几行字有多宽用（LogWindow.lua:96-101 同一手法：
    -- alpha=0 而不是 Hide —— 隐藏的帧客户端不一定还给排版，量回来是 0）。
    ui.meas = frame:CreateFontString(nil, "OVERLAY", FONT_BODY)
    ui.meas:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -GEOM.PAD)
    ui.meas:SetWidth(GEOM.CELL_W * 7)
    pcall(ui.meas.SetAlpha, ui.meas, 0)

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

--- 顶栏三个读数：本周CD / 本月 / 全部，全是 GetRangeTotals 的查表结果。
--- 「全部」是记账以来的流水累计，**不等于银行里的钱**（计划 §3：转账不计收支、
--- 界面外变动记 unknown），两个数对不上是正常的，这里绝不互相"修正"。
local function RenderTotals()
    for _, preset in ipairs({ "week", "month", "all" }) do
        local slot = ui.totals[preset]
        if slot then
            local s, e = Calendar.GetRange(preset)
            if not s then
                SetText(slot.value, "?")
                SetColor(slot.value, C_ZERO)
            else
                local t = LCall("GetRangeTotals", s, e)
                local net = type(t) == "table" and t.net or nil
                SetText(slot.value, Calendar.FormatMoneyIcon(net))
                SetColor(slot.value, NetColor(net))
            end
            slot.startKey, slot.endKey = s, e
        end
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

        if not cell.used then
            cell.host:Hide()
        else
            cell.host:Show()
            -- 底色/描边：今天那一格才有边框纹理（"描边"就是它的 edgeFile）
            pcall(cell.host.SetBackdrop, cell.host, cell.today and CELL_BG_TODAY or CELL_BG_NORMAL)
            if cell.today then
                pcall(cell.host.SetBackdropColor, cell.host, 0.16, 0.15, 0.10, 1)
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
                SetText(cell.sub, "")
            else
                local day = LCall("GetDay", key)
                local inc = type(day) == "table" and day.income or 0
                local exp = type(day) == "table" and day.expense or 0
                local net = type(day) == "table" and day.net or 0
                if inc == 0 and exp == 0 then
                    -- 零变动：净与小字都空着，只留日号（没发生的事不占视线）
                    SetText(cell.net, "")
                    SetText(cell.sub, "")
                    SetColor(cell.net, C_ZERO)
                else
                    SetText(cell.net, Calendar.FormatMoneyIcon(net))
                    SetColor(cell.net, NetColor(net))
                    SetText(cell.sub, Fmt("CELL_SUB",
                        Calendar.FormatMoneyCompact(inc), Calendar.FormatMoneyCompact(exp)))
                    SetColor(cell.sub, C_DIM)
                end
                cell.income, cell.expense = inc, exp
            end
        end
    end
end

--- 下半区：号列表页（默认）或流水页。
local function RenderCharList()
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

    for i, row in ipairs(ui.charRows) do
        local data = rows[i]
        if not data then
            row.host:Hide()
            row.charKey = nil
        else
            row.host:Show()
            row.charKey = data.name
            SetText(row.name, Clip(data.name, 34))
            SetText(row.inc, Calendar.FormatMoneyIcon(data.income))
            SetText(row.exp, Calendar.FormatMoneyIcon(data.expense))
            SetText(row.net, Calendar.FormatMoneyIcon(data.net))
            SetColor(row.net, NetColor(data.net))
            local isCur = (curKey ~= nil and data.name == curKey)
            SetColor(row.name, isCur and C_TEXT or C_ZERO)
            row.current = isCur
        end
    end

    local overflow = #rows - GEOM.CHARS_ROWS
    if overflow > 0 then
        ui.moreChars:Show()
        SetText(ui.moreChars, Fmt("MORE_CHARS", overflow))
    else
        ui.moreChars:Hide()
    end

    local t = LCall("GetRangeTotals", state.startKey, state.endKey)
    SetText(ui.totalRow.inc, Calendar.FormatMoneyIcon(type(t) == "table" and t.income or 0))
    SetText(ui.totalRow.exp, Calendar.FormatMoneyIcon(type(t) == "table" and t.expense or 0))
    SetText(ui.totalRow.net, Calendar.FormatMoneyIcon(type(t) == "table" and t.net or 0))
    SetColor(ui.totalRow.net, NetColor(type(t) == "table" and t.net or 0))

    if #rows == 0 then
        ui.empty:Show()
        -- 范围键本身没算出来（墙上钟读不出）⇒ 绝不写成"这个范围没有变动"：那是对着
        -- 一个根本没确定的日子报"没事发生"（读不到就说为什么读不到）。
        SetText(ui.empty, state.startKey and T("EMPTY_RANGE") or T("NO_CLOCK"))
    else
        ui.empty:Hide()
    end
end

--- 分类小计：Ledger 已经算好整段范围的 bySource，这里只排序 + 截可见行。
local function SortedSources(bySource)
    local out = {}
    if type(bySource) ~= "table" then return out end
    for src, bucket in pairs(bySource) do
        local inc = tonumber(bucket and bucket.income) or 0
        local exp = tonumber(bucket and bucket.expense) or 0
        out[#out + 1] = { src = tostring(src), income = inc, expense = exp, net = inc - exp }
    end
    table.sort(out, function(a, b)
        if a.net ~= b.net then return a.net > b.net end
        return a.src < b.src
    end)
    return out
end

local function RenderFlow()
    local info = state.flowInfo
    local rows = (type(info) == "table" and type(info.rows) == "table") and info.rows or {}

    for i, row in ipairs(ui.flowRows) do
        local e = rows[i]
        if not e then
            row.host:Hide()
        else
            row.host:Show()
            local key = (type(e.dateKey) == "string" and e.dateKey) or LCall("DateKey", e.t)
            local hh, mm = "?", "?"
            local d = _G.date
            if type(d) == "function" and type(e.t) == "number" then
                local okCall, t = pcall(d, "*t", e.t)
                if okCall and type(t) == "table" then
                    hh, mm = tostring(tonumber(t.hour) or "?"), tostring(tonumber(t.min) or "?")
                end
            end
            SetText(row.time, Fmt("FLOW_TIME", tostring(key), tonumber(hh) or 0, tonumber(mm) or 0))
            SetText(row.src, SrcName(e.src))
            SetText(row.amt, Calendar.FormatMoneyIcon(tonumber(e.amt)))
            SetColor(row.amt, NetColor(tonumber(e.amt)))
            -- 邮件标题：只带条目里真读到的（T1 口径 —— 编不出来的东西一律不显示）
            if type(e.subject) == "string" and e.subject ~= "" then
                row.note:Show()
                SetText(row.note, Clip(e.subject, 48))
            else
                row.note:Hide()
                SetText(row.note, "")
            end
        end
    end

    local sources = SortedSources(type(info) == "table" and info.bySource)
    if #sources > 0 then
        ui.subHead:Show()
    else
        ui.subHead:Hide()
    end
    for i, row in ipairs(ui.subRows) do
        local s = sources[i]
        if not s then
            row.host:Hide()
        else
            row.host:Show()
            SetText(row.label, SrcName(s.src))
            SetText(row.value, Fmt("CELL_SUB", Calendar.FormatMoneyIcon(s.income),
                Calendar.FormatMoneyIcon(s.expense)))
            SetColor(row.value, NetColor(s.net))
        end
    end
    local subOverflow = #sources - GEOM.SUB_ROWS
    if subOverflow > 0 then
        ui.moreSources:Show()
        SetText(ui.moreSources, Fmt("MORE_SOURCES", subOverflow))
    else
        ui.moreSources:Hide()
    end

    local totalPages = tonumber(info and info.totalPages) or 0
    if totalPages == 0 then
        ui.empty:Show()
        SetText(ui.empty, T("EMPTY_FLOW"))
    else
        ui.empty:Hide()
    end
    ui.pageInfo:Show()
    SetText(ui.pageInfo, Fmt("PAGE_INFO", state.flowPage, math.max(totalPages, 1)))
    -- 按钮开关用 Enable/Disable（本仓库的实测写法，GoldQuestLedger/Options.lua:281）：
    -- SetEnabled 在这一仓库的已证清单里指得出调用点，Enable/Disable 指得出，就用指得出的那个。
    if state.flowPage > 1 then ui.nav.prev:Enable() else ui.nav.prev:Disable() end
    if state.flowPage < totalPages then ui.nav.next:Enable() else ui.nav.next:Disable() end
end

local function RenderLowerHead(page)
    local chars = (page == "chars")
    for _, fs in ipairs({ ui.charHead.name, ui.charHead.inc, ui.charHead.exp, ui.charHead.net }) do
        if chars then fs:Show() else fs:Hide() end
    end
    for _, fs in ipairs({ ui.flowHead.time, ui.flowHead.src, ui.flowHead.amt }) do
        if chars then fs:Hide() else fs:Show() end
    end
    for _, b in ipairs({ ui.nav.prev, ui.nav.next, ui.nav.back }) do
        if chars then b:Hide() else b:Show() end
    end
    if chars then
        ui.pageInfo:Hide()
        ui.totalRow.name:Show()
        ui.totalRow.inc:Show()
        ui.totalRow.exp:Show()
        ui.totalRow.net:Show()
    else
        ui.totalRow.name:Hide()
        ui.totalRow.inc:Hide()
        ui.totalRow.exp:Hide()
        ui.totalRow.net:Hide()
    end
end

function Calendar.Refresh()
    if not built then return end
    local okCall, err = pcall(function()
        ApplyStaticTexts()
        RenderTotals()
        RenderGrid()
        RenderLowerHead(state.page)
        if state.page == "flow" then RenderFlow() else RenderCharList() end
    end)
    if not okCall then NoteError("Refresh", err) end
    pcall(SetText, ui.monthLabel, Fmt("CAL_MONTH", state.year or 0, state.month or 0))
end

---------------------------------------------------------------------------
-- 交互（只有点击会改状态）
---------------------------------------------------------------------------

--- 换月：‹ / ›。预设「本月」跟着显示月走，其余预设不动（范围与显示月本就两件事）。
function Calendar.ShiftMonth(delta)
    if not state.year or not state.month then return end
    local y, m = state.year, state.month + delta
    if m < 1 then m = 12 ; y = y - 1 elseif m > 12 then m = 1 ; y = y + 1 end
    state.year, state.month = y, m
    if state.preset == "month" then
        return Calendar.SelectPreset("month")
    end
    Calendar.Refresh()
end

--- 点日号：下半区列**这天**有钱变动的号（范围 = 单日），并显示所在月。
function Calendar.SelectDay(dateKey)
    if type(dateKey) ~= "string" then return false end
    local y, m = PartsOf(dateKey)
    if not y then return false end
    if state.selKey ~= dateKey then
        state.selKey = dateKey
        state.preset = "sel"
        state.startKey, state.endKey = dateKey, dateKey
        state.page = "chars"
        state.flowChar, state.flowPage = nil, 1
    end
    state.year, state.month = y, m
    Calendar.Refresh()
    return true
end

--- 点「流水」：换成该号这段范围的分页流水（新在前）。
function Calendar.OpenFlow(charKey)
    if type(charKey) ~= "string" or charKey == "" then return false end
    state.page = "flow"
    state.flowChar = charKey
    state.flowPage = 1
    Calendar.LoadFlowPage()
    return true
end

function Calendar.LoadFlowPage()
    if state.page ~= "flow" or not state.flowChar then return nil end
    local res = LCall("GetCharEntries", state.flowChar, state.startKey, state.endKey,
        state.flowPage, Calendar.PER_PAGE)
    if type(res) ~= "table" then
        res = { rows = {}, total = 0, totalPages = 0, page = 1, bySource = {} }
    end
    state.flowInfo = res
    -- 页码夹回 [1, totalPages]（totalPages 为 0 时停在第 1 页，空页照样要能返回）。
    local totalPages = tonumber(res.totalPages) or 0
    if totalPages > 0 and state.flowPage > totalPages then
        state.flowPage = totalPages
        return Calendar.LoadFlowPage()
    end
    Calendar.Refresh()
    return res
end

--- 翻页：到边就夹住（按钮不许把页码推出范围，也不许在空流水上打转）。
function Calendar.ShiftPage(delta)
    if state.page ~= "flow" then return false end
    local totalPages = tonumber(state.flowInfo and state.flowInfo.totalPages) or 0
    local want = state.flowPage + delta
    if want < 1 then want = 1 end
    if totalPages > 0 and want > totalPages then want = totalPages end
    if want == state.flowPage then return false end
    state.flowPage = want
    Calendar.LoadFlowPage()
    return true
end

function Calendar.Back()
    if state.page ~= "flow" then return false end
    state.page = "chars"
    state.flowChar, state.flowPage, state.flowInfo = nil, 1, nil
    Calendar.Refresh()
    return true
end

---------------------------------------------------------------------------
-- 悬停提示：日期 + 收入 / 支出 / 净 + 几个号有变动。
-- 不给分类明细（那要再养一份按天按来源的冗余，而且两句话说不清 —— 它属于点进去那页）。
---------------------------------------------------------------------------

local function TipGuard()
    local tip = _G.GameTooltip
    if type(tip) ~= "table" then return nil end
    if type(tip.SetOwner) ~= "function" or type(tip.AddLine) ~= "function"
        or type(tip.Show) ~= "function" then return nil end
    return tip
end

function Calendar.ShowCellTip(cell)
    -- 先把上一次我们弹的收掉：客户端一般按 OnLeave→OnEnter 的顺序走，但**月外格**这条
    -- 是"什么都不设"就 return，不收的话上一格的提示会挂在人家脸上（用例走的就是这条序列）。
    Calendar.HideCellTip()
    state.tip.shown = false
    if type(cell) ~= "table" or not cell.inMonth then return false end   -- 月外格什么都不设
    local key = cell.dateKey
    if type(key) ~= "string" then return false end
    local tip = TipGuard()
    if not tip then
        state.tip.error = "GameTooltip 不可用（没有这个全局或方法不齐）"
        return false
    end
    local day = LCall("GetDay", key)
    if type(day) ~= "table" then return false end
    local chars = type(day.charOrder) == "table" and #day.charOrder or 0
    local lines = {
        Fmt("TIP_DATE", key),
        Fmt("TIP_INCOME", Calendar.FormatMoneyIcon(day.income)),
        Fmt("TIP_EXPENSE", Calendar.FormatMoneyIcon(day.expense)),
        Fmt("TIP_NET", Calendar.FormatMoneyIcon(day.net)),
        Fmt("TIP_CHARS", chars),
    }
    -- GameTooltip 是 Frame，没有 SetText 这个方法（GQL 全程只用 SetOwner/ClearLines/AddLine/Show，
    -- Matrix.lua:494-548）；第一行也只能 AddLine。
    local okCall, err = pcall(function()
        tip:SetOwner(cell.host, "ANCHOR_RIGHT")
        tip:ClearLines()
        tip:AddLine(lines[1])
        tip:AddLine(lines[2], C_POS[1], C_POS[2], C_POS[3])
        tip:AddLine(lines[3], C_NEG[1], C_NEG[2], C_NEG[3])
        local net = NetColor(day.net)
        tip:AddLine(lines[4], net[1], net[2], net[3])
        tip:AddLine(lines[5], C_DIM[1], C_DIM[2], C_DIM[3])
        tip:Show()
    end)
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
    -- 只收我们自己弹的那条（Matrix.lua:693-701 同一条理由：别人的提示不许连锅端）
    if not state.tip.shown then return false end
    state.tip.shown = false
    local tip = TipGuard()
    if tip and type(tip.Hide) == "function" then pcall(tip.Hide, tip) end
    return true
end

---------------------------------------------------------------------------
-- 开关窗（刷新只在这里发生）
---------------------------------------------------------------------------

function Calendar.Open()
    local okCall, err = pcall(Build)
    if not okCall then
        NoteError("Build", err)
        print("GCL: 日历窗口建不起来：" .. tostring(err))
        return false
    end
    local today = LCall("TodayKey")
    local y, m = PartsOf(today)
    if y then state.year, state.month = y, m end
    state.page = "chars"
    state.flowChar, state.flowPage, state.flowInfo = nil, 1, nil
    state.selKey = today
    state.preset, state.startKey, state.endKey = "day", today, today
    frame:Show()
    Calendar.Refresh()
    return true
end

function Calendar.Close()
    if frame then frame:Hide() end
    Calendar.HideCellTip()
    return true
end

function Calendar.Toggle()
    if frame and frame:IsShown() then return Calendar.Close() end
    return Calendar.Open()
end

---------------------------------------------------------------------------
-- /gcl probe ui：只读取证，不改任何状态；窗开没开都不许报错。
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

--- 一条字符串用暗尺量宽（客户端没给我们量 GameTooltip 排版的能力，只能同字体估）。
local function MeasureText(meas, text)
    if not meas or type(meas.SetText) ~= "function"
        or type(meas.GetStringWidth) ~= "function" then return nil end
    pcall(meas.SetText, meas, tostring(text))
    local w = Measure(meas)
    pcall(meas.SetText, meas, "")
    return w
end

local function PrintUIProbe(rest)
    print("GCL probe ui: 界面几何取证（只读，不改状态）")
    print(("GCL probe ui: 常量 WIN_W=%d WIN_H=%d CELL_W=%d CELL_H=%d GRID_ROWS=%d ROW_H=%d "
        .. "CHARS_ROWS=%d FLOW_ROWS=%d SUB_ROWS=%d PER_PAGE=%d PAD=%d NAV_H=%d PRESET_H=%d")
        :format(GEOM.WIN_W, GEOM.WIN_H, GEOM.CELL_W, GEOM.CELL_H, GEOM.GRID_ROWS, GEOM.ROW_H,
            GEOM.CHARS_ROWS, GEOM.FLOW_ROWS, GEOM.SUB_ROWS, Calendar.PER_PAGE,
            GEOM.PAD, GEOM.NAV_H, GEOM.PRESET_H))

    local cd, why = Calendar.WeekResetCountdown()
    print(("GCL probe ui: 周CD接口 %s（答 %s）本次采用来源=%s")
          :format(WEEK_RESET_API, tostring(cd), tostring(state.weekCD and state.weekCD.source)))
    print(("GCL probe ui: 周CD退化原因（没退化则为 nil）=%s 界面不许显示这一条")
          :format(tostring(state.weekCD and state.weekCD.reason)))

    if not built then
        print("GCL probe ui: 窗口还没建 —— 先 /gcl open 再跑一次才有几何")
        return
    end

    print(("GCL probe ui: 帧 %s %s %s %s %s ESC名单=%s")
          :format(tostring(_G.GCLCalFrame == frame and "GCLCalFrame(全局名可解析)" or "GCLCalFrame?"),
            ProbeDescribe(function() return frame:GetWidth() end, "宽"),
            ProbeDescribe(function() return frame:GetHeight() end, "高"),
            ProbeDescribe(function() return frame:GetFrameStrata() end, "strata"),
            ProbeDescribe(function() return frame:GetFrameLevel() end, "层号"),
            ProbeDescribe(function()
                local list = _G.UISpecialFrames
                if type(list) ~= "table" then return nil end
                for i = 1, #list do
                    if list[i] == "GCLCalFrame" then return "已挂" end
                end
                return "不在名单里"
            end, "UISpecialFrames")))
    -- 矩形：本仓库证过的只有 GetCenter + GetWidth/GetHeight（Matrix.lua:956/1251），
    -- GetRect 指不出调用点 ⇒ 用证过的这三样拼中心+宽高，不写没证过的方法名。
    print(ProbeDescribe(function()
        local cx, cy = frame:GetCenter()
        return ("中心=%s,%s 宽=%s 高=%s"):format(tostring(cx), tostring(cy),
            tostring(frame:GetWidth()), tostring(frame:GetHeight()))
    end, "GCL probe ui: 帧位置尺寸"))

    print(("GCL probe ui: 显示 %s 今天=%s 首格=%s 首日星期(1=周日)=%s 月长=%s 用出行数=%s")
          :format(tostring(state.year), tostring(LCall("TodayKey")),
            tostring(ui.gridInfo and ui.gridInfo.firstKey),
            tostring(ui.gridInfo and ui.gridInfo.firstWday),
            tostring(ui.gridInfo and ui.gridInfo.dim),
            tostring(ui.gridInfo and ui.gridInfo.rowsNeeded)))
    print(("GCL probe ui: 范围 %s=%s..%s 页=%s 号=%s 页码=%s")
          :format(tostring(state.preset), tostring(state.startKey), tostring(state.endKey),
            tostring(state.page), tostring(state.flowChar), tostring(state.flowPage)))

    -- 每格矩形（相对格主机的算式，客户端不给矩形就只能这样对）+ 三行字的实测宽度
    for i, cell in ipairs(ui.cells) do
        if cell.used then
            local l = (cell.col - 1) * GEOM.CELL_W + 1
            local t = -((cell.row - 1) * GEOM.CELL_H + 1)
            print(("GCL probe ui: 格%d r%d c%d 键=%s 月内=%s 今天=%s x=[%d..%d] y=[%d..%d] 宽=%s 净=%s 小=%s")
                  :format(i, cell.row, cell.col, tostring(cell.dateKey),
                      tostring(cell.inMonth), tostring(cell.today),
                      l, l + GEOM.CELL_W - 2, t, t - (GEOM.CELL_H - 2),
                      tostring(Measure(cell.num)), tostring(Measure(cell.net)),
                      tostring(Measure(cell.sub))))
        end
    end

    -- 每种字体一条：文本原样 + 客户端实测宽度 + 建串时传的字体对象名（第 3 参就是它）
    for _, f in ipairs(ui.fonts) do
        local w = Measure(f.fs)
        local okT, txt = pcall(f.fs.GetText, f.fs)
        print(("GCL probe ui: 字体 角色=%s 字体名=%s 文本=%s 量宽=%s")
              :format(f.role, tostring(f.font), okT and tostring(txt) or "?", tostring(w)))
    end

    -- 悬停提示那几行的宽度（暗尺估：GameTooltip 自己的字体问不出来）
    if type(state.tip.lines) == "table" and #state.tip.lines > 0 then
        for i, line in ipairs(state.tip.lines) do
            print(("GCL probe ui: 提示行%d 文本=%s 量宽=%s（暗尺按 %s 估，非 GameTooltip 自身字体）")
                  :format(i, line, tostring(MeasureText(ui.meas, line)), FONT_BODY))
        end
    else
        print("GCL probe ui: 提示行：本次会话还没悬停过月内的格子，量不到")
    end
    print(ProbeDescribe(function()
        local tip = _G.GameTooltip
        if type(tip) ~= "table" or type(tip.GetWidth) ~= "function" then return nil end
        return ("%sx%s"):format(tostring(tip:GetWidth()), tostring(tip:GetHeight()))
    end, "GCL probe ui: GameTooltip 自身尺寸"))
    print(ProbeDescribe(function()
        return ui.close:GetFrameLevel()
    end, "GCL probe ui: × 的层号（必须=主帧层号+1，模板自带 510 会浮到别人上面）"))

    local errs = state.errors
    if #errs > 0 then
        print(("GCL probe ui: 最近 %d 次绘制/建窗报错：")
              :format(#errs))
        for i = 1, #errs do print("  " .. tostring(errs[i])) end
    else
        print("GCL probe ui: 绘制没报错")
    end
end

---------------------------------------------------------------------------
-- 命令
---------------------------------------------------------------------------

if GCL.RegisterCommand then
    GCL.RegisterCommand("open", function() return Calendar.Toggle() end, "打开/关闭日历窗口")

    -- 「probe ui」：Money 已占用 probe 这个子命令，这里按参数分流，不动记账层的取证段。
    -- 参数是 ui 才走界面取证，其余（含空参数）原样转给上一层的 handler。
    local cmd = GCL.commands and GCL.commands["probe"]
    if type(cmd) == "table" and type(cmd.fn) == "function" and not cmd.__uiChained then
        local prev = cmd.fn
        cmd.fn = function(rest)
            if type(rest) == "string" and rest:match("^ui") then return PrintUIProbe(rest) end
            return prev(rest)
        end
        cmd.__uiChained = true
    else
        GCL.RegisterCommand("probeui", PrintUIProbe, "取证：界面几何（等价 /gcl probe ui）")
    end
end

return Calendar
