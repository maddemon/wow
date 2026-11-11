-- 流水弹窗（T8 → 第 3 轮）：点日历右栏某一号开出来的一扇独立小窗。
-- 范围锚是**日历点选的那天**（用户 2026-11-10 定："根据日历点击的日期来"）：
-- 本日 / 昨日 / 本周CD / 本月 / 全部五档由 Calendar.PopupRange 统一算，弹窗不自己猜日期。
-- 筛选四样全走自绘下拉（角色 / 类型 / 日期 / 收支方向），不引 UIDropDownMenu
-- （本仓库没有它在 12.x 的已验证调用点；形状照 GoldLedger/UI/UI.lua:769-859）。
-- 真分页每页 20 行，分类汇总挪到右侧竖列，不占流水列表的高度。
-- 遮挡照 GoldQuestLedger/Options.lua:889-899 的 RaiseAboveMatrix：两窗都是 UIParent 平级
-- MEDIUM，打开时按"主窗层号 + 10"抬自己，再把 × 钉回本窗主帧 +1。
-- 空闲不挂任何每帧脚本：滚轮偏移自己记（AHSnipe/SnipeList.lua:42 那条实测口径）。

local GDL = _G.GDL
local Flows = {}

if GDL and GDL.RegisterModule then
    GDL.RegisterModule("Flows", Flows)
else
    GDL = GDL or {}
    GDL.Flows = Flows
end

local GEOM = {
    PAD         = 12,
    TITLE_H     = 22,
    FILTER_H    = 26,   -- 四个筛选下拉那一行
    HEAD_H      = 20,   -- 时间 / 分类 / 金额 列头
    ROW_H       = 20,
    LIST_ROWS   = 12,   -- 滚动视口可见行（一页 20 行，页内还能滚）
    LIST_W      = 464,  -- 左半：流水列表（用户 2026-11-11：弹窗改大一点）
    PAGE_H      = 26,   -- 上一页 / 页码 / 下一页
    SUB_W       = 190,  -- 右半：分类汇总
    SUB_GAP     = 12,
    SUB_ROW_H   = 18,
    SUB_MAX     = 10,   -- 来源枚举就 10 类（Money 写进条名的全集）
    SCROLL_W    = 22,   -- 模板自带滚动条占的宽
    CLOSE_INSET = 6,
    BTN_H       = 22,
    NAV_W       = 72,
    -- 四格宽用户 2026-11-11 终稿点数：角色 110、类型 90、日期 150、收支 90。
    -- 按钮文字另有 ClipLabel 按宽度裁断兜底，不会折行。
    DD_W        = { 110, 90, 150, 90 },
    DD_GAP      = 6,
    COLS = { time = 2, src = 120, subInc = 112, subExp = 186 },
}
GEOM.LIST_H = GEOM.LIST_ROWS * GEOM.ROW_H
GEOM.SUB_X = GEOM.PAD + GEOM.LIST_W + GEOM.SCROLL_W + GEOM.SUB_GAP
-- 分类汇总底部那一行「总计」：贴着列表底，上面再压一根横线，和分类行分开。
GEOM.SUB_TOTAL_Y = GEOM.LIST_H - GEOM.SUB_ROW_H
GEOM.SUB_TOTAL_DIV_GAP = 8
-- 窗高恒定：行数变化只体现在内容高度上，不该让窗子尺寸跳动（AHSnipe/SnipeList.lua:274）。
GEOM.WIN_W = GEOM.PAD + GEOM.LIST_W + GEOM.SCROLL_W + GEOM.SUB_GAP + GEOM.SUB_W + GEOM.PAD
GEOM.WIN_H = GEOM.PAD + GEOM.TITLE_H + GEOM.FILTER_H + GEOM.HEAD_H + GEOM.LIST_H
    + GEOM.PAGE_H + GEOM.PAD
Flows.GEOM = GEOM

local PER_PAGE = 20
Flows.PER_PAGE = PER_PAGE

local RANGE_PRESETS = { "day", "yesterday", "week", "month", "all" }
local SOURCES = { "vendor", "repair", "ah", "mail", "quest", "loot", "trade",
                  "unknown", "warband", "guildbank" }

local C_POS  = { 0.24, 0.78, 0.32 }
local C_NEG  = { 0.86, 0.28, 0.26 }
local C_ZERO = { 0.62, 0.62, 0.62 }
local C_TEXT = { 1, 0.92, 0.60 }
-- 下拉选项 / 分类名的常态色：白色。灰色只留给"净为 0"这类数值，
-- 不再拿它染角色名和类型名（用户 2026-11-10：不要搞成灰色）。
local C_ITEM = { 1, 1, 1 }

local FONT_TITLE = "GameFontHighlight"
local FONT_BODY  = "GameFontHighlightSmall"
local FONT_SMALL = "GameFontNormalSmall"

local function T(key)
    local loc = GDL and GDL.Locale
    if type(loc) == "table" and type(loc.Get) == "function" then
        local okCall, r = pcall(loc.Get, key)
        if okCall and type(r) == "string" then return r end
    end
    return tostring(key)
end

local function Fmt(key, ...)
    local s = T(key)
    if select("#", ...) == 0 then return s end
    local okCall, r = pcall(string.format, s, ...)
    if okCall and type(r) == "string" then return r end
    return s
end

local function SrcName(src)
    local loc = GDL and GDL.Locale
    if type(loc) == "table" and type(loc.SrcName) == "function" then
        local okCall, r = pcall(loc.SrcName, src)
        if okCall and type(r) == "string" and r ~= "" then return r end
    end
    return tostring(src)
end

--- 金额两个口径（用户 2026-11-11 定）：流水行的金额列宽度够，走全量 金/银/铜；
--- 右侧分类小计与底部总计的三列挤在 SUB_W 里，全量摆不开（用户截图：类型名
--- 被顶没了），回整数金。都借日历的现成函数，不各写一份。
local function MoneyFull(copper)
    local cal = GDL and GDL.Calendar
    if type(cal) == "table" and type(cal.FormatMoneyFull) == "function" then
        return cal.FormatMoneyFull(copper)
    end
    return "?"
end

local function Money(copper)
    local cal = GDL and GDL.Calendar
    if type(cal) == "table" and type(cal.FormatMoneyIcon) == "function" then
        return cal.FormatMoneyIcon(copper)
    end
    return "?"
end

local function NetColor(net)
    if type(net) ~= "number" then return C_ZERO end
    if net > 0 then return C_POS end
    if net < 0 then return C_NEG end
    return C_ZERO
end

--- 字节预算裁断：截在半字符上客户端画乱码方块 ⇒ 退到整字符（Calendar.lua 同一条口径）。
local function Clip(s, maxBytes)
    s = tostring(s or "")
    if #s <= maxBytes then return s end
    local cut = maxBytes
    while cut > 0 and s:byte(cut + 1) and s:byte(cut + 1) >= 0x80 and s:byte(cut + 1) < 0xC0 do
        cut = cut - 1
    end
    return s:sub(1, cut)
end

--- 下拉按钮只写选中的值，且按字节预算裁断：定宽的 FontString 碰上长文本会**折行**
--- （用户截图里「角色 · 风行者·麦德」折成两行），前缀去掉后仍需一道长度兜底。
--- 角色名带 |cffRRGGBB...|r 转义，转义不占显示宽度、更不能截在中间 ⇒
--- 剥出来只裁名字，再把颜色套回去。
local function ClipLabel(s, maxBytes)
    s = tostring(s or "")
    local color, name = s:match("^(|cff%x%x%x%x%x%x)(.*)|r$")
    if color then return color .. Clip(name, maxBytes) .. "|r" end
    return Clip(s, maxBytes)
end

local function LedgerOK()
    local l = GDL and GDL.Ledger
    return type(l) == "table" and l or nil
end

local function LCall(name, ...)
    local l = LedgerOK()
    if not l or type(l[name]) ~= "function" then return nil end
    local okCall, r = pcall(l[name], ...)
    if not okCall then return nil end
    return r
end

local function SetText(fs, text)
    if not fs then return end
    pcall(fs.SetText, fs, tostring(text))
end

local function SetColor(fs, rgb)
    if not fs then return end
    pcall(fs.SetTextColor, fs, rgb[1], rgb[2], rgb[3], 1)
end

--- 角色名一律走账本的短名 + 职业色；读不到就退回纯文本短名，绝不印 "名字-服务器"。
local function CharLabel(charKey, maxBytes)
    local lab = LCall("CharLabel", charKey, maxBytes)
    if type(lab) == "string" and lab ~= "" then return lab end
    local short = LCall("ShortName", charKey)
    return Clip(tostring(short or charKey or ""), maxBytes or 40)
end

local function CharKeys()
    local keys = LCall("GetCharKeys")
    return type(keys) == "table" and keys or {}
end

--- 五档日期范围只问 Calendar：它握着周 CD 接口与 KeyOf/DaysInMonth，弹窗不另算一遍。
local function CalRange(preset, anchorKey)
    local cal = GDL and GDL.Calendar
    if type(cal) ~= "table" or type(cal.PopupRange) ~= "function" then return nil, nil end
    local okCall, s, e = pcall(cal.PopupRange, preset, anchorKey)
    if okCall and type(s) == "string" and type(e) == "string" then return s, e end
    return nil, nil
end

local state = {
    charKey = nil,        -- nil = 全部角色
    source = nil,         -- nil = 全部类型
    preset = "day",       -- day / yesterday / week / month / all
    direction = "all",    -- all / in / out
    anchorKey = nil,      -- 日历点选的那天，所有档位都锚它
    startKey = nil, endKey = nil, rangeText = "",
    page = 1, info = nil, scrollY = 0,
}
Flows.GetState = function() return state end

local frame, ui, built
Flows.GetUI = function() return ui end

local function ContentH()
    local n = type(state.info) == "table" and type(state.info.rows) == "table" and #state.info.rows or 0
    return math.max(GEOM.LIST_H, n * GEOM.ROW_H)
end

--- 滚轮：偏移自己记、上限自己算。GetVerticalScroll / GetVerticalRange 在 12.x 的返回形态
--- 没实证过（AHSnipe/SnipeList.lua:42），所以不读回来，只写出去。
local function Wheel(delta)
    if not ui or not ui.scroll then return end
    local top = math.max(0, ContentH() - GEOM.LIST_H)
    local y = (tonumber(state.scrollY) or 0) - (tonumber(delta) or 0) * GEOM.ROW_H
    state.scrollY = math.max(0, math.min(top, y))
    pcall(ui.scroll.SetVerticalScroll, ui.scroll, state.scrollY)
end

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

local function RowTip(row)
    local tip = _G.GameTooltip
    if type(tip) ~= "table" or type(row.subject) ~= "string" or row.subject == "" then return end
    if type(tip.SetOwner) ~= "function" or type(tip.AddLine) ~= "function" then return end
    pcall(function()
        tip:SetOwner(row.host, "ANCHOR_RIGHT")
        tip:ClearLines()
        tip:AddLine(SrcName(row.src))
        tip:AddLine(Clip(row.subject, 60), 0.8, 0.8, 0.8)
        tip:Show()
    end)
end

---------------------------------------------------------------------------
-- 自绘下拉（GoldLedger/UI/UI.lua:769-859 的形状，去掉它用的 HookScript / SetAllPoints：
-- 本仓库的离线替身与已证清单里没有这两个方法）。popup 挂 TOOLTIP strata，
-- 保证选项浮在弹窗自己的列表与汇总上面；父窗 Hide 时由父帧链一起带走。
---------------------------------------------------------------------------

local DD_BACKDROP = {
    bgFile = "Interface/Buttons/WHITE8X8",
    edgeFile = "Interface/Tooltips/UI-Tooltip-Border",
    edgeSize = 8, insets = { left = 2, right = 2, top = 2, bottom = 2 },
}

local function MakeDropdown(parent, width, onSelect, font)
    local dd = CreateFrame("Button", nil, parent, "BackdropTemplate")
    dd:SetSize(width, GEOM.BTN_H)
    dd:SetBackdrop(DD_BACKDROP)
    pcall(dd.SetBackdropColor, dd, 0.10, 0.10, 0.13, 1)
    pcall(dd.SetBackdropBorderColor, dd, 0.40, 0.40, 0.45, 1)

    dd.label = dd:CreateFontString(nil, "OVERLAY", font or FONT_BODY)
    dd.label:SetPoint("LEFT", dd, "LEFT", 6, 0)
    -- 给字串定宽：不定宽时 WoW 会让它按内容长出去，日期档选中值就溢到隔壁
    -- "收支"下拉脸上（用户 2026-11-10 截图）。日期档另用小一号字体。
    dd.label:SetWidth(width - 12)
    pcall(dd.label.SetJustifyH, dd.label, "LEFT")

    local popup = CreateFrame("Frame", nil, parent, "BackdropTemplate")
    popup:SetFrameStrata("TOOLTIP")
    popup:SetWidth(width)
    popup:EnableMouse(true)
    popup:SetBackdrop(DD_BACKDROP)
    pcall(popup.SetBackdropColor, popup, 0, 0, 0, 1)
    popup:SetPoint("TOPLEFT", dd, "BOTTOMLEFT", 0, -2)
    popup:Hide()

    dd.popup = popup
    dd.items = {}
    dd.options = {}
    dd.value = nil
    dd.currentLabel = ""
    -- 按钮文字的字节预算：中文一个字 3 字节约 12px（≈4px/字节），ASCII 更省，
    -- 用 4 是偏保守的估算；宁可少摆一两个字，也不让定宽 FontString 折行。
    dd.clipMax = math.floor((width - 12) / 4)

    dd:SetScript("OnClick", function()
        if popup:IsShown() then
            popup:Hide()
        else
            Flows.ClosePopups()
            popup:Show()
        end
    end)

    -- 选项按钮池化：选项个数/标签会随角色集、语言、日期锚变化，所以每次重设而不是建死。
    dd.SetOptions = function(opts, value)
        dd.options = opts or {}
        dd.value = value
        local n = #dd.options
        popup:SetHeight(n * GEOM.ROW_H + 8)
        local shown = math.max(n, #dd.items)
        for i = 1, shown do
            local item = dd.items[i]
            if not item then
                local b = CreateFrame("Button", nil, popup)
                b:SetSize(width - 8, GEOM.ROW_H)
                b:SetPoint("TOPLEFT", popup, "TOPLEFT", 4, -4 - (i - 1) * GEOM.ROW_H)
                b:EnableMouse(true)
                pcall(b.RegisterForClicks, b, "LeftButtonUp")
                local fs = b:CreateFontString(nil, "OVERLAY", FONT_BODY)
                fs:SetPoint("LEFT", b, "LEFT", 4, 0)
                item = { btn = b, fs = fs }
                dd.items[i] = item
                local idx = i
                b:SetScript("OnClick", function()
                    popup:Hide()
                    local opt = dd.options[idx]
                    if opt and onSelect then onSelect(opt.value) end
                end)
            end
            local opt = dd.options[i]
            if not opt then
                item.btn:Hide()
            else
                item.btn:Show()
                SetText(item.fs, opt.label)
                if opt.value == value then
                    SetColor(item.fs, C_TEXT)
                else
                    SetColor(item.fs, opt.rgb or C_ITEM)
                end
            end
        end
        dd.currentLabel = ""
        for i = 1, n do
            if dd.options[i].value == value then
                dd.currentLabel = dd.options[i].label
                break
            end
        end
    end
    return dd
end

function Flows.ClosePopups()
    if not ui or not ui.dropdowns then return end
    for _, dd in pairs(ui.dropdowns) do
        if dd and dd.popup then dd.popup:Hide() end
    end
end

local function EnsureRow(i)
    if ui.rows[i] then return ui.rows[i] end
    local host = CreateFrame("Frame", nil, ui.content)
    host:SetSize(GEOM.LIST_W, GEOM.ROW_H)
    host:SetPoint("TOPLEFT", ui.content, "TOPLEFT", 0, -((i - 1) * GEOM.ROW_H))
    host:EnableMouse(true)
    host:EnableMouseWheel(true)
    host:SetScript("OnMouseWheel", function(_, delta) Wheel(delta) end)
    local row = { host = host, subject = nil, src = nil }
    row.time = host:CreateFontString(nil, "OVERLAY", FONT_SMALL)
    row.time:SetPoint("LEFT", host, "LEFT", GEOM.COLS.time, 0)
    -- 字串叫 srcText：row.src 留给来源字符串（悬停提示用），重画时别把字串对象盖掉。
    row.srcText = host:CreateFontString(nil, "OVERLAY", FONT_BODY)
    row.srcText:SetPoint("LEFT", host, "LEFT", GEOM.COLS.src, 0)
    row.amt = host:CreateFontString(nil, "OVERLAY", FONT_BODY)
    row.amt:SetPoint("RIGHT", host, "RIGHT", -2, 0)
    host:SetScript("OnEnter", function() RowTip(row) end)
    host:SetScript("OnLeave", function()
        local tip = _G.GameTooltip
        if type(tip) == "table" and type(tip.Hide) == "function" then pcall(tip.Hide, tip) end
    end)
    ui.rows[i] = row
    return row
end

---------------------------------------------------------------------------
-- 抬层：GoldQuestLedger/Options.lua:889-899 的 RaiseAboveMatrix 同款。
-- 两扇窗都是 UIParent 平级 MEDIUM，跨 strata 比层号无效 ⇒ 先让主窗退回 MEDIUM，
-- 再把整棵弹窗子树抬到"主窗层号 + 10"。不隐藏/禁用主窗、不加 SetToplevel。
---------------------------------------------------------------------------

local function RaiseAboveCalendar(f)
    -- Core.RaiseFront（2026-11-11 统一协议）：GDL 流水/日历与 GQL 矩阵/设置/日志窗都是
    -- DIALOG + SetToplevel，谁被点/谁最后开谁在最前，所以这里只让 Core 把本窗 Raise 到
    -- DIALOG 最上、并钉回 ×。不再把对端窗拉回 MEDIUM 按层号压自己（那是层序随机的根因）。
    if type(GDL) == "table" and type(GDL.RaiseFront) == "function" then
        pcall(GDL.RaiseFront, f, ui and ui.close)
        return
    end
    -- Core 缺席的降级形状：只按日历主窗层号 +10 抬自己，宁可层级不完美也不许弹窗开不起来。
    -- 两窗都是 DIALOG，同 strata 比层号就够。
    local cal = GDL and GDL.Calendar
    if type(cal) ~= "table" or type(cal.GetFrame) ~= "function" then return end
    local okGet, mf = pcall(cal.GetFrame)
    if not okGet or type(mf) ~= "table" then return end
    local okLvl, lvl = pcall(mf.GetFrameLevel, mf)
    lvl = (okLvl and tonumber(lvl)) or 1
    pcall(f.SetFrameLevel, f, lvl + 10)
    local okL2, l2 = pcall(f.GetFrameLevel, f)
    if okL2 and tonumber(l2) and ui and ui.close then
        pcall(ui.close.SetFrameLevel, ui.close, tonumber(l2) + 1)
    end
end

local function Build()
    if built then return true end
    ui = { rows = {}, subRows = {}, dropdowns = {} }

    frame = CreateFrame("Frame", "GDLFlowFrame", UIParent, "BackdropTemplate")
    frame:SetSize(GEOM.WIN_W, GEOM.WIN_H)
    frame:SetPoint("CENTER")
    -- 与日历主窗、GQL 矩阵同为 DIALOG + SetToplevel：三窗间谁被点/谁最后开谁在最前
    -- （原生窗口那种点一下就上来的能力；老农整合包用的就是这一对）。
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
    -- 点/拖动/显示流水弹窗时也抬一次：GQL 或日历主窗后来开窗不该把弹窗压下去。
    local function BringToFront()
        RaiseAboveCalendar(frame)
    end
    frame:SetScript("OnMouseDown", BringToFront)
    frame:SetScript("OnShow", BringToFront)

    -- ESC 关闭名单存的是全局名字符串（Calendar.lua 同一条口径）。
    local list = _G.UISpecialFrames
    if type(list) == "table" then
        local already = false
        for i = 1, #list do
            if list[i] == "GDLFlowFrame" then already = true end
        end
        if not already then table.insert(list, "GDLFlowFrame") end
    end

    ui.title = frame:CreateFontString(nil, "OVERLAY", FONT_TITLE)
    ui.title:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -GEOM.PAD)

    ui.close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    ui.close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", -GEOM.CLOSE_INSET, -GEOM.CLOSE_INSET)
    ui.close:SetScript("OnClick", function() Flows.Close() end)
    local okL, lvl = pcall(frame.GetFrameLevel, frame)
    if okL and tonumber(lvl) then
        pcall(ui.close.SetFrameLevel, ui.close, tonumber(lvl) + 1)
    end

    -- 四个筛选下拉：一行排开，按钮上写「筛子名 · 当前值」。
    local filterY = GEOM.PAD + GEOM.TITLE_H
    local specs = {
        { key = "char",  onSelect = function(v) Flows.SetChar(v) end },
        { key = "type",  onSelect = function(v) Flows.SetSource(v) end },
        { key = "range", onSelect = function(v) Flows.SetRange(v) end, font = FONT_SMALL },
        { key = "dir",   onSelect = function(v) Flows.SetDirection(v) end },
    }
    local x = GEOM.PAD
    for i, spec in ipairs(specs) do
        local dd = MakeDropdown(frame, GEOM.DD_W[i], spec.onSelect, spec.font)
        dd:SetPoint("TOPLEFT", frame, "TOPLEFT", x, -filterY)
        ui.dropdowns[spec.key] = dd
        x = x + GEOM.DD_W[i] + GEOM.DD_GAP
    end

    local headY = filterY + GEOM.FILTER_H
    ui.head = {
        time = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
        src = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
        amt = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
    }
    ui.head.time:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + GEOM.COLS.time, -headY)
    ui.head.src:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + GEOM.COLS.src, -headY)
    ui.head.amt:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.PAD + GEOM.LIST_W - 2, -headY)

    local listY = headY + GEOM.HEAD_H
    ui.scroll = CreateFrame("ScrollFrame", "GDLFlowScroll", frame, "UIPanelScrollFrameTemplate")
    ui.scroll:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -listY)
    ui.scroll:SetSize(GEOM.LIST_W, GEOM.LIST_H)
    ui.scroll:EnableMouseWheel(true)
    ui.scroll:SetScript("OnMouseWheel", function(_, delta) Wheel(delta) end)
    ui.content = CreateFrame("Frame", "GDLFlowContent", ui.scroll)
    ui.content:SetWidth(GEOM.LIST_W)
    ui.content:SetHeight(GEOM.LIST_H)
    ui.scroll:SetScrollChild(ui.content)

    ui.empty = ui.content:CreateFontString(nil, "OVERLAY", FONT_SMALL)
    ui.empty:SetPoint("TOPLEFT", ui.content, "TOPLEFT", GEOM.COLS.time, -4)

    -- 真分页：上一页 / 页码 / 下一页，列表下面单独一行，不占汇总的地。
    local pageY = listY + GEOM.LIST_H
    ui.page = {}
    ui.page.prev = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
    ui.page.prev:SetSize(GEOM.NAV_W, GEOM.BTN_H)
    ui.page.prev:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD, -pageY)
    ui.page.prev:SetScript("OnClick", function() Flows.ShiftPage(-1) end)
    ui.page.next = CreateFrame("Button", nil, frame, "UIPanelButtonTemplate")
    ui.page.next:SetSize(GEOM.NAV_W, GEOM.BTN_H)
    ui.page.next:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.PAD + GEOM.NAV_W + GEOM.DD_GAP, -pageY)
    ui.page.next:SetScript("OnClick", function() Flows.ShiftPage(1) end)
    ui.page.label = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL)
    ui.page.label:SetPoint("TOPLEFT", frame, "TOPLEFT",
        GEOM.PAD + 2 * GEOM.NAV_W + 2 * GEOM.DD_GAP, -(pageY + 4))

    -- 分类汇总：右侧竖列（用户 2026-11-11 三次定稿：做到顶，与左边的筛选行 +
    -- 列表整个平级）——列头顶到筛选行，行从流水列头那行起，一直铺到列表底；
    -- 总计仍钉在列表底（横线 + 行），不跟着分类行数挪。
    ui.subHead = {
        label = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
        inc = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
        exp = frame:CreateFontString(nil, "OVERLAY", FONT_SMALL),
    }
    ui.subHead.label:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.SUB_X + 4, -filterY)
    ui.subHead.inc:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.SUB_X + GEOM.COLS.subInc, -filterY)
    ui.subHead.exp:SetPoint("TOPRIGHT", frame, "TOPLEFT", GEOM.SUB_X + GEOM.COLS.subExp, -filterY)

    for i = 1, GEOM.SUB_MAX do
        local b = CreateFrame("Button", nil, frame)
        b:SetSize(GEOM.SUB_W, GEOM.SUB_ROW_H)
        b:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.SUB_X, -(headY + (i - 1) * GEOM.SUB_ROW_H))
        b:EnableMouse(true)
        pcall(b.RegisterForClicks, b, "LeftButtonUp")
        local row = { btn = b, src = nil }
        row.label = b:CreateFontString(nil, "OVERLAY", FONT_BODY)
        row.label:SetPoint("LEFT", b, "LEFT", 4, 0)
        -- 三列都锚在行按钮的垂直中心：原来 inc/exp 锚 "TOPLEFT" 且 y=0，
        -- 数字贴着行顶、和左边的类型名错开一行（用户截图"金币和类型不在一行"）。
        -- 右锚的负偏移换算成与列头相同的 x（SUB_X + COLS.subInc / subExp）。
        row.inc = b:CreateFontString(nil, "OVERLAY", FONT_BODY)
        row.inc:SetPoint("RIGHT", b, "RIGHT", -(GEOM.SUB_W - GEOM.COLS.subInc), 0)
        row.exp = b:CreateFontString(nil, "OVERLAY", FONT_BODY)
        row.exp:SetPoint("RIGHT", b, "RIGHT", -(GEOM.SUB_W - GEOM.COLS.subExp), 0)
        b.__gclSub = row
        b:SetScript("OnClick", function(self) Flows.ToggleSource(self.__gclSub.src) end)
        ui.subRows[i] = row
    end

    -- 列表与分类汇总之间的竖分割线：照日历主窗 ui.panelDiv 那根 1 像素线
    -- （GoldQuestLedger/Matrix.lua:1610-1640 同类），两头钉、不用细长 Frame。
    ui.subDiv = frame:CreateTexture(nil, "ARTWORK")
    pcall(ui.subDiv.SetColorTexture, ui.subDiv, 1, 1, 1, 0.15)
    ui.subDiv:SetPoint("TOPLEFT", frame, "TOPLEFT",
        GEOM.SUB_X - math.floor(GEOM.SUB_GAP / 2), -filterY)
    ui.subDiv:SetWidth(1)
    ui.subDiv:SetHeight(GEOM.FILTER_H + GEOM.HEAD_H + GEOM.LIST_H)

    -- 底部「总计」：横线上方留白，行本身与分类行同一套三列锚点。
    ui.subTotalDiv = frame:CreateTexture(nil, "ARTWORK")
    pcall(ui.subTotalDiv.SetColorTexture, ui.subTotalDiv, 1, 1, 1, 0.15)
    ui.subTotalDiv:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.SUB_X + 4,
        -(listY + GEOM.SUB_TOTAL_Y - GEOM.SUB_TOTAL_DIV_GAP))
    ui.subTotalDiv:SetWidth(GEOM.SUB_W - 8)
    ui.subTotalDiv:SetHeight(1)

    local tb = CreateFrame("Button", nil, frame)
    tb:SetSize(GEOM.SUB_W, GEOM.SUB_ROW_H)
    tb:SetPoint("TOPLEFT", frame, "TOPLEFT", GEOM.SUB_X, -(listY + GEOM.SUB_TOTAL_Y))
    tb:EnableMouse(false)
    ui.subTotal = { btn = tb }
    ui.subTotal.label = tb:CreateFontString(nil, "OVERLAY", FONT_BODY)
    ui.subTotal.label:SetPoint("LEFT", tb, "LEFT", 4, 0)
    ui.subTotal.inc = tb:CreateFontString(nil, "OVERLAY", FONT_BODY)
    ui.subTotal.inc:SetPoint("RIGHT", tb, "RIGHT", -(GEOM.SUB_W - GEOM.COLS.subInc), 0)
    ui.subTotal.exp = tb:CreateFontString(nil, "OVERLAY", FONT_BODY)
    ui.subTotal.exp:SetPoint("RIGHT", tb, "RIGHT", -(GEOM.SUB_W - GEOM.COLS.subExp), 0)
    SetText(ui.subTotal.label, T("COL_TOTAL"))

    built = true
    return true
end

---------------------------------------------------------------------------
-- 渲染：下拉选项、静态字、分页、汇总都在 Refresh 里重摆（换语言/换角色集都跟着变）
---------------------------------------------------------------------------

local function RefreshDropdowns()
    local charOpts = { { value = "", label = T("ALL_CHARS") } }
    for _, key in ipairs(CharKeys()) do
        charOpts[#charOpts + 1] = { value = key, label = CharLabel(key, 21),
                                    rgb = LCall("CharColor", key) }
    end
    ui.dropdowns.char.SetOptions(charOpts, state.charKey or "")

    local typeOpts = { { value = "", label = T("ALL_TYPES") } }
    for _, src in ipairs(SOURCES) do
        typeOpts[#typeOpts + 1] = { value = src, label = SrcName(src) }
    end
    ui.dropdowns.type.SetOptions(typeOpts, state.source or "")

    local rangeOpts = {}
    for _, preset in ipairs(RANGE_PRESETS) do
        local s, e = CalRange(preset, state.anchorKey)
        if s then rangeOpts[#rangeOpts + 1] = { value = preset, label = Flows.RangeText(preset, s, e) } end
    end
    ui.dropdowns.range.SetOptions(rangeOpts, state.preset)

    local dirOpts = {
        { value = "all", label = T("DIR_ALL") },
        { value = "in",  label = T("DIR_INCOME") },
        { value = "out", label = T("DIR_EXPENSE") },
    }
    ui.dropdowns.dir.SetOptions(dirOpts, state.direction)

    -- 四个按钮一律只写选中的值，不写「角色 · / 类型 · / 收支 ·」这类筛子名前缀：
    -- 前缀 + 长号名会把定宽字串挤到第二行（用户 2026-11-10 截图）。筛子靠选项列表自解释。
    -- 每个按钮另按自己的宽度裁一道，颜色转义留给 ClipLabel 原样套回。
    local labels = {
        char  = ui.dropdowns.char.currentLabel,
        type  = ui.dropdowns.type.currentLabel,
        range = (ui.dropdowns.range.currentLabel ~= "" and ui.dropdowns.range.currentLabel)
                    or state.rangeText,
        dir   = ui.dropdowns.dir.currentLabel,
    }
    for key, text in pairs(labels) do
        local dd = ui.dropdowns[key]
        SetText(dd.label, ClipLabel(text or "", dd.clipMax))
    end
end

local function Render()
    if not built then return end
    local info = type(state.info) == "table" and state.info or { rows = {}, bySource = {}, total = 0 }
    local rows = type(info.rows) == "table" and info.rows or {}

    local who = state.charKey and CharLabel(state.charKey, 40) or T("ALL_CHARS")
    SetText(ui.title, Fmt("FLOW_TITLE", who, state.rangeText))
    SetText(ui.head.time, T("COL_TIME"))
    SetText(ui.head.src, T("COL_SOURCE"))
    SetText(ui.head.amt, T("COL_AMOUNT"))

    local shown = math.max(#rows, #ui.rows)
    for i = 1, shown do
        local row = EnsureRow(i)
        local e = rows[i]
        if not e then
            row.host:Hide()
            row.subject, row.src = nil, nil
        else
            row.host:Show()
            local key = (type(e.dateKey) == "string" and e.dateKey) or LCall("DateKey", e.t)
            local hh, mm = 0, 0
            local d = _G.date
            if type(d) == "function" and type(e.t) == "number" then
                local okCall, t = pcall(d, "*t", e.t)
                if okCall and type(t) == "table" then
                    hh, mm = tonumber(t.hour) or 0, tonumber(t.min) or 0
                end
            end
            SetText(row.time, Fmt("FLOW_TIME", tostring(key), hh, mm))
            -- 卖店连发在读出层并成一行：标出并了几条，免得玩家以为漏记。
            local mergedCount = tonumber(e.mergedCount) or 0
            if mergedCount > 1 then
                SetText(row.srcText, Fmt("FLOW_MERGED", SrcName(e.src), mergedCount))
            else
                SetText(row.srcText, SrcName(e.src))
            end
            SetText(row.amt, MoneyFull(tonumber(e.amt)))
            SetColor(row.amt, NetColor(tonumber(e.amt)))
            row.src = e.src
            row.subject = (type(e.subject) == "string" and e.subject ~= "") and e.subject or nil
        end
    end

    ui.content:SetHeight(ContentH())
    local top = math.max(0, ContentH() - GEOM.LIST_H)
    state.scrollY = math.max(0, math.min(top, tonumber(state.scrollY) or 0))
    pcall(ui.scroll.SetVerticalScroll, ui.scroll, state.scrollY)

    -- 分页：页码按真实 totalPages 报，按钮到边就 Disable（不再有"另有 N 笔未列出"）。
    local totalPages = tonumber(info.totalPages) or 0
    local page = tonumber(info.page) or state.page
    SetText(ui.page.prev, T("BTN_PREV_PAGE"))
    SetText(ui.page.next, T("BTN_NEXT_PAGE"))
    SetText(ui.page.label, Fmt("PAGE_INFO", page, math.max(totalPages, 1)))
    if page > 1 then ui.page.prev:Enable() else ui.page.prev:Disable() end
    if page < totalPages then ui.page.next:Enable() else ui.page.next:Disable() end

    RefreshDropdowns()

    local sources = SortedSources(info.bySource)
    if #sources > 0 then
        ui.subHead.label:Show() ; ui.subHead.inc:Show() ; ui.subHead.exp:Show()
        SetText(ui.subHead.label, T("COL_SUBTOTALS"))
        SetText(ui.subHead.inc, T("COL_INCOME"))
        SetText(ui.subHead.exp, T("COL_EXPENSE"))
    else
        ui.subHead.label:Hide() ; ui.subHead.inc:Hide() ; ui.subHead.exp:Hide()
    end
    -- 底部「总计」按完整的 char+range 汇总算，不跟着类型/收支筛选收窄（与分类小计同源）。
    local totalInc, totalExp = 0, 0
    for _, s in ipairs(sources) do
        totalInc = totalInc + (tonumber(s.income) or 0)
        totalExp = totalExp + (tonumber(s.expense) or 0)
    end
    if #sources > 0 then
        ui.subTotal.btn:Show()
        ui.subTotalDiv:Show()
        SetText(ui.subTotal.label, T("COL_TOTAL"))
        SetText(ui.subTotal.inc, Money(totalInc))
        SetText(ui.subTotal.exp, Money(totalExp))
        SetColor(ui.subTotal.inc, C_POS)
        SetColor(ui.subTotal.exp, C_NEG)
        SetColor(ui.subTotal.label, C_ITEM)
    else
        ui.subTotal.btn:Hide()
        ui.subTotalDiv:Hide()
    end
    for i, row in ipairs(ui.subRows) do
        local s = sources[i]
        if not s then
            row.btn:Hide()
            row.src = nil
        else
            row.btn:Show()
            row.src = s.src
            SetText(row.label, SrcName(s.src))
            SetText(row.inc, Money(s.income))
            SetText(row.exp, Money(s.expense))
            SetColor(row.inc, C_POS)
            SetColor(row.exp, C_NEG)
            SetColor(row.label, (state.source == s.src) and C_TEXT or C_ITEM)
        end
    end

    if #rows == 0 then
        ui.empty:Show()
        SetText(ui.empty, state.charKey and T("EMPTY_FLOW") or T("EMPTY_RANGE"))
    else
        ui.empty:Hide()
    end
end

local function Load()
    local charKey = state.charKey
    if charKey == "" then charKey = nil end
    local source = state.source
    if source == "" then source = nil end
    local res = LCall("GetCharEntries", charKey, state.startKey, state.endKey,
        state.page, PER_PAGE, source, state.direction)
    if type(res) ~= "table" then
        res = { rows = {}, total = 0, totalPages = 0, page = state.page, bySource = {} }
    end
    -- 筛选后页数可能变少：把页码夹回末页，再取一次（只一次，不递归）。
    local totalPages = tonumber(res.totalPages) or 0
    if totalPages > 0 and state.page > totalPages then
        state.page = totalPages
        res = LCall("GetCharEntries", charKey, state.startKey, state.endKey,
            state.page, PER_PAGE, source, state.direction) or res
    end
    state.info = res
    state.scrollY = 0
    Render()
end

local function ApplyRange()
    local s, e = CalRange(state.preset, state.anchorKey)
    if s and e then
        state.startKey, state.endKey = s, e
        state.rangeText = Flows.RangeText(state.preset, s, e)
    end
end

--- @param charKey string|nil  nil = 全部角色
--- @param anchorKey string|nil 日历点选的那天；不传就用 startKey（/gdl flow 兜底）
--- @param preset string|nil 初始日期档：day / yesterday / week / month / all
function Flows.Open(charKey, startKey, endKey, rangeText, anchorKey, preset)
    if type(startKey) ~= "string" or type(endKey) ~= "string" or startKey > endKey then
        return false
    end
    state.charKey = (type(charKey) == "string" and charKey ~= "") and charKey or nil
    state.source = nil
    state.direction = "all"
    state.preset = (preset == "week" or preset == "month") and preset or "day"
    state.page = 1
    state.anchorKey = (type(anchorKey) == "string" and anchorKey ~= "") and anchorKey or startKey
    state.startKey, state.endKey = startKey, endKey
    state.rangeText = tostring(rangeText or startKey)
    if not Build() then return false end
    ApplyRange()
    Flows.ClosePopups()
    RaiseAboveCalendar(frame)
    Load()
    frame:Show()
    return true
end

--- 下拉选角色：空串 = 全部角色。每次换筛选都回第 1 页。
function Flows.SetChar(value)
    state.charKey = (type(value) == "string" and value ~= "") and value or nil
    state.page = 1
    Load()
    return true
end

--- 下拉选类型：空串 = 全部类型（点当前项不清筛选，那是汇总行的行为）。
function Flows.SetSource(src)
    local v = (type(src) == "string" and src ~= "") and src or nil
    state.source = v
    state.page = 1
    Load()
    return true
end

--- 点分类汇总那一行：点同一行清掉筛选，点别的行换类；下拉选中值跟着重画。
function Flows.ToggleSource(src)
    if type(src) ~= "string" then return false end
    if state.source == src then state.source = nil else state.source = src end
    state.page = 1
    Load()
    return true
end

function Flows.SetRange(preset)
    local okPreset = false
    for _, p in ipairs(RANGE_PRESETS) do if p == preset then okPreset = true end end
    if not okPreset then return false end
    local s, e = CalRange(preset, state.anchorKey)
    if not s then return false end
    state.preset, state.startKey, state.endKey = preset, s, e
    state.rangeText = Flows.RangeText(preset, s, e)
    state.page = 1
    Load()
    return true
end

function Flows.SetDirection(v)
    if v ~= "all" and v ~= "in" and v ~= "out" then return false end
    state.direction = v
    state.page = 1
    Load()
    return true
end

function Flows.ShiftPage(delta)
    local totalPages = tonumber(state.info and state.info.totalPages) or 0
    if totalPages <= 0 then return false end
    local want = math.max(1, math.min(totalPages, state.page + delta))
    if want == state.page then return false end
    state.page = want
    Load()
    return true
end

function Flows.Close()
    Flows.ClosePopups()
    if frame then frame:Hide() end
    return true
end

--- 换语言后由 Locale.Repaint 调用：四个下拉的筛子名/选项、页码按钮、列头、标题
--- 都是静态串，不重画就会半中半英（主窗那边同一条纪律）。窗没开时 no-op。
Flows.Refresh = function()
    if built and frame and frame:IsShown() then Render() end
end

--- 范围说法：弹窗标题与日期下拉选项共用。档位词后面都带确切日期，
--- 免得"本日"到底指今天还是日历点选的那天说不清（用户 2026-11-10 定）。
function Flows.RangeText(preset, startKey, endKey)
    if preset == "all" then return T("RANGE_ALL") end
    if preset == "week" and type(startKey) == "string" and type(endKey) == "string" then
        return Fmt("FLOW_RANGE_WEEK", T("RANGE_WEEK"), startKey:sub(6), endKey:sub(6))
    end
    if preset == "month" and type(startKey) == "string" then
        return Fmt("FLOW_RANGE_MONTH", T("RANGE_MONTH"), startKey:sub(1, 7))
    end
    if preset == "yesterday" then
        return Fmt("FLOW_RANGE_DAY", T("RANGE_YESTERDAY"), tostring(startKey))
    end
    return Fmt("FLOW_RANGE_DAY", T("RANGE_DAY"), tostring(startKey))
end

if GDL and GDL.RegisterCommand then
    GDL.RegisterCommand("flow", function(rest)
        local today = LCall("TodayKey")
        if not today then
            print("GDL: " .. T("NO_CLOCK"))
            return false
        end
        local charKey = (type(rest) == "string" and rest ~= "") and rest or nil
        return Flows.Open(charKey, today, today, Flows.RangeText("day", today, today), today, "day")
    end, "打开流水弹窗（可带 号名-服名；不带就是全部角色的本日）")
end

return Flows
