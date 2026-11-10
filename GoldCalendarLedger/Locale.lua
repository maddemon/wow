-- 本地化（原属 T4，随 T3 界面先落地，免得界面写两遍）：这个文件只有数据 + 查表。
-- 除了两个本仓库已证存在的全局（GetLocale / GetRealmName，判语言用，全部包 type 检查 + pcall），
-- 不碰任何客户端接口、不建帧、不订事件、不挂每帧脚本。
--
-- 存档口径（## SavedVariables 安全）：用户选的语言由 T4 持久化在 GCLDB.settings.language，
-- 本模块只认 Locale.ApplySetting(code) 这一个入口，绝不自己新建全局存档、也不直接读别的插件的档。
-- 键的纪律（用例 test_locale_* 钉死）：界面里每一个 Locale.Get("…") 的字面键，
-- zhCN 与 enUS 两张表都必须有，而且 %d/%s 占位符个数一致 —— 漏一种语言是这类改动最常见的破法。

local GCL = _G.GCL
local Locale = {}

if GCL and GCL.RegisterModule then
    GCL.RegisterModule("Locale", Locale)
else
    GCL = GCL or {}
    GCL.Locale = Locale
end

---------------------------------------------------------------------------
-- 默认档：读不出任何语言信号时用它（T4 才接自动判定，见 Locale.Detect）
---------------------------------------------------------------------------

local DEFAULT = "zhCN"

---------------------------------------------------------------------------
-- 文本表：只有 zhCN / enUS 两档（计划 §3 定稿"只留中英"）。
-- 格式串一律 %d / %s 占位、locale-agnostic；日期键保持 ISO，不进文本表。
---------------------------------------------------------------------------

Locale.L = {
    zhCN = {
        -- 窗口
        CAL_TITLE       = "金币日历账本",
        CAL_MONTH       = "%d年%d月",
        MO_PREV         = "‹",
        MO_NEXT         = "›",

        -- 星期表头（下标 = Ledger.WeekdayOfDateKey 的 wday，1=周日）
        WD_1            = "日",
        WD_2            = "一",
        WD_3            = "二",
        WD_4            = "三",
        WD_5            = "四",
        WD_6            = "五",
        WD_7            = "六",

        -- 范围预设（顶栏三个读数与下半区四颗按钮共用同一批词）
        RANGE_DAY       = "本日",
        RANGE_WEEK      = "本周CD",
        RANGE_MONTH     = "本月",
        RANGE_ALL       = "全部",

        -- 列头 / 行
        COL_CHAR        = "号名",
        COL_INCOME      = "收入",
        COL_EXPENSE     = "支出",
        COL_NET         = "净",
        COL_TIME        = "时间",
        COL_SOURCE      = "分类",
        COL_AMOUNT      = "金额",
        COL_TOTAL       = "合计",
        COL_SUBTOTALS   = "分类小计",

        -- 按钮（管理类列表每行只有一颗，就是 BTN_FLOW）
        BTN_FLOW        = "流水",
        BTN_BACK        = "返回",
        BTN_PREV_PAGE   = "上一页",
        BTN_NEXT_PAGE   = "下一页",

        -- 一格天 / 悬停提示：日期 + 收 / 支 / 净 + 几个号有变动（不含来源明细，计划 §3）
        TIP_DATE        = "%s",
        TIP_INCOME      = "收入 %s",
        TIP_EXPENSE     = "支出 %s",
        TIP_NET         = "净 %s",
        TIP_CHARS       = "%d 个号有变动",
        CELL_SUB        = "%s·%s",

        -- 金额缩写（格子里的小字用；图标形式与 g/s/c 形式见 Calendar 的格式化函数）
        UNIT_GOLD       = "金",
        UNIT_SILVER     = "银",
        UNIT_COPPER     = "铜",

        -- 流水页
        FLOW_TIME       = "%s %02d:%02d",
        PAGE_INFO       = "第 %d / %d 页",

        -- 空态与溢出
        EMPTY_RANGE     = "这个范围没有变动",
        EMPTY_FLOW      = "这个号在这段没有流水",
        MORE_CHARS      = "另有 %d 个号未列出",
        MORE_SOURCES    = "另有 %d 类未列出",
        NO_CLOCK        = "读不出今天：这台机器没有可用的墙上时钟",

        -- 来源名（Money 实际会写进条目的 10 个名字，用例逐个钉）
        SRC_VENDOR      = "卖店",
        SRC_REPAIR      = "修理",
        SRC_AH          = "拍卖行",
        SRC_MAIL        = "邮件",
        SRC_QUEST       = "任务",
        SRC_LOOT        = "拾取",
        SRC_TRADE       = "交易",
        SRC_UNKNOWN     = "未知",
        SRC_WARBAND     = "号间搬钱",
        SRC_GUILDBANK   = "公会银行",
    },
    enUS = {
        CAL_TITLE       = "Gold Calendar Ledger",
        CAL_MONTH       = "%d-%d",
        MO_PREV         = "‹",
        MO_NEXT         = "›",

        WD_1            = "Sun",
        WD_2            = "Mon",
        WD_3            = "Tue",
        WD_4            = "Wed",
        WD_5            = "Thu",
        WD_6            = "Fri",
        WD_7            = "Sat",

        RANGE_DAY       = "Day",
        RANGE_WEEK      = "Week CD",
        RANGE_MONTH     = "Month",
        RANGE_ALL       = "All",

        COL_CHAR        = "Character",
        COL_INCOME      = "Income",
        COL_EXPENSE     = "Expense",
        COL_NET         = "Net",
        COL_TIME        = "Time",
        COL_SOURCE      = "Source",
        COL_AMOUNT      = "Amount",
        COL_TOTAL       = "Total",
        COL_SUBTOTALS   = "By source",

        BTN_FLOW        = "Flows",
        BTN_BACK        = "Back",
        BTN_PREV_PAGE   = "Prev",
        BTN_NEXT_PAGE   = "Next",

        TIP_DATE        = "%s",
        TIP_INCOME      = "Income %s",
        TIP_EXPENSE     = "Expense %s",
        TIP_NET         = "Net %s",
        TIP_CHARS       = "%d characters moved",
        CELL_SUB        = "%s/%s",

        UNIT_GOLD       = "g",
        UNIT_SILVER     = "s",
        UNIT_COPPER     = "c",

        FLOW_TIME       = "%s %02d:%02d",
        PAGE_INFO       = "Page %d / %d",

        EMPTY_RANGE     = "No movement in this range",
        EMPTY_FLOW      = "No entries for this character here",
        MORE_CHARS      = "%d more characters not listed",
        MORE_SOURCES    = "%d more sources not listed",
        NO_CLOCK        = "Cannot tell today: no usable wall clock",

        SRC_VENDOR      = "Vendor",
        SRC_REPAIR      = "Repair",
        SRC_AH          = "Auction House",
        SRC_MAIL        = "Mail",
        SRC_QUEST       = "Quest",
        SRC_LOOT        = "Loot",
        SRC_TRADE       = "Trade",
        SRC_UNKNOWN     = "Unknown",
        SRC_WARBAND     = "Alt transfer",
        SRC_GUILDBANK   = "Guild bank",
    },
}

Locale.DEFAULT_LOCALE = DEFAULT
Locale.CODES = { zhCN = true, enUS = true }

---------------------------------------------------------------------------
-- 查表
---------------------------------------------------------------------------

local active = DEFAULT

--- 取文本：当前语言 → 静默回落 zhCN → 再回落键本身（界面永远不会因为缺串报错）。
function Locale.Get(key)
    if type(key) ~= "string" then return tostring(key) end
    local table_ = Locale.L[active]
    local s = type(table_) == "table" and table_[key] or nil
    if type(s) == "string" then return s end
    local d = Locale.L[DEFAULT]
    s = type(d) == "table" and d[key] or nil
    if type(s) == "string" then return s end
    return key
end

function Locale.GetLocale()
    return active
end

--- 切语言：只认 Locale.CODES 里的档，别的（含 nil / 拼错）一律回默认档，不报错。
function Locale.Set(code)
    if type(code) == "string" and Locale.CODES[code] then
        active = code
    else
        active = DEFAULT
    end
    return active
end

--- T4 的设置入口：存档里的 settings.language 直接原样递进来。
--- nil 与 "auto" 都表示"没选过 / 跟随自动判定"，此时保持默认档（T4 在这里接 Detect）。
function Locale.ApplySetting(code)
    if code == nil or code == "auto" then
        -- T4：这一行改成 Locale.Set(Locale.Detect())，并把 GCLDB.settings.language 落盘。
        return Locale.Set(DEFAULT)
    end
    return Locale.Set(code)
end

---------------------------------------------------------------------------
-- 自动判定的判据（数据侧；T4 才在 Core 的 PLAYER_LOGIN 链上接线调用）
---------------------------------------------------------------------------

--- 服名里有没有 CJK 字节。判国服不能只看 GetLocale()（它是文本语言，国服能回 enUS），
--- 这条字节判据是上一轮给 GoldLedger 打的补丁（GoldLedger/Locale.lua:220-225 同一条路）。
function Locale.RealmHasCJK(realm)
    if type(realm) ~= "string" then return false end
    return realm:match("[\228-\244]") ~= nil
end

--- @return string|nil  判得出给 "zhCN"/"enUS"；两个信号都问不出返回 nil（= 保持默认档）
function Locale.Detect()
    local fn = _G.GetLocale
    if type(fn) == "function" then
        local okCall, v = pcall(fn)
        if okCall and type(v) == "string" and (v == "zhCN" or v == "zhTW") then
            return "zhCN"
        end
    end
    local rf = _G.GetRealmName
    if type(rf) == "function" then
        local okCall, realm = pcall(rf)
        if okCall and Locale.RealmHasCJK(realm) then return "zhCN" end
        if okCall and type(realm) == "string" and realm ~= "" then return "enUS" end
    end
    return nil
end

---------------------------------------------------------------------------
-- 来源名：Money 写进条目的 src 字符串 → 界面词（问不到就原样回它，绝不空着）
---------------------------------------------------------------------------

function Locale.SrcName(src)
    if type(src) ~= "string" or src == "" then return Locale.Get("SRC_UNKNOWN") end
    local key = "SRC_" .. src:upper()
    local r = Locale.Get(key)
    if r == key then return src end
    return r
end

---------------------------------------------------------------------------
-- 生命周期：只把存档里已选的语言应用上（T1/T2 建好的 settings 表，读不到就默认档）。
-- 自动判定与写档都在 T4，这里不碰。
---------------------------------------------------------------------------

function Locale:OnInitialize()
    local db = _G.GCLDB
    local code = type(db) == "table" and type(db.settings) == "table"
        and db.settings.language or nil
    Locale.ApplySetting(code)
end

return Locale
