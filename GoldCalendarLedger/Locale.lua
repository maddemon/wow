-- 本地化：这个文件只有数据 + 查表 + "这次到底用了哪档、是谁定的"这一份判定记录。
-- 除了两个本仓库已证存在的全局（GetLocale / GetRealmName，判语言用，全部包 type 检查 + pcall），
-- 不碰别的客户端接口、不建帧、不订事件、不挂每帧脚本。
--
-- 存档口径（## SavedVariables 安全）：玩家选的语言存在 GCLDB.settings.language，
-- 只有 /gcl lang 写它；自动判定的结果**绝不落盘**（没选过 = 每次登录重新判，
-- 落盘等于把某一次登录看到的服当成永久决定）。
-- 键的纪律（用例 test_locale_* 钉死）：界面里每一个查表的字面键，
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
-- 默认档：读不出任何语言信号、也没人选过语言时用它
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
        -- ‹ ›（U+2039/203A）在 12.1.5 中文字体里没有字形，按钮画出来是空的 ⇒ 用 ASCII 尖括号。
        MO_PREV         = "<",
        MO_NEXT         = ">",

        -- 星期表头（下标 = Ledger.WeekdayOfDateKey 的 wday，1=周日）
        WD_1            = "日",
        WD_2            = "一",
        WD_3            = "二",
        WD_4            = "三",
        WD_5            = "四",
        WD_6            = "五",
        WD_7            = "六",

        -- 范围档位（顶栏三个读数与范围按钮共用同一批词）。
        -- 周档显示"本周"但口径仍是周 CD（国服周四起）：改的只是字，范围算法一个字不动。
        RANGE_DAY       = "本日",
        RANGE_WEEK      = "本周",
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

        -- 小地图按钮的悬停提示（标题复用 CAL_TITLE；这里只补玩家要的那句说法）
        MINIMAP_CLICK   = "点击打开日历",

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
        MO_PREV         = "<",
        MO_NEXT         = ">",

        WD_1            = "Sun",
        WD_2            = "Mon",
        WD_3            = "Tue",
        WD_4            = "Wed",
        WD_5            = "Thu",
        WD_6            = "Fri",
        WD_7            = "Sat",

        RANGE_DAY       = "Day",
        RANGE_WEEK      = "This Week",
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

        MINIMAP_CLICK   = "Click to open the calendar",

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

--- T4 起的设置入口：存档里的 settings.language 原样递进来（OnInitialize 调）。
--- 这一档只回答"已经选定了就用它"。nil / "auto" 在这里保持默认档、**不跑自动判定** ——
--- 判语言要问 GetRealmName，而它在 ADDON_LOADED 那一刻还答不出，
--- 判定归 PLAYER_LOGIN 的 Locale.ResolveForLogin（时序由用例 test_locale_auto_detect_wired_at_login 钉住）。
function Locale.ApplySetting(code)
    if code == nil or code == "auto" then
        return Locale.Set(DEFAULT)
    end
    return Locale.Set(code)
end

---------------------------------------------------------------------------
-- 自动判定的判据（只在 PLAYER_LOGIN 之后问，那时服名才答得出）
---------------------------------------------------------------------------

--- 服名里有没有 CJK 字节。判国服不能只看 GetLocale()（它是文本语言，国服能回 enUS），
--- 这条字节判据是上一轮给 GoldLedger 打的补丁（GoldLedger/Locale.lua:220-225 同一条路）。
function Locale.RealmHasCJK(realm)
    if type(realm) ~= "string" then return false end
    return realm:match("[\228-\244]") ~= nil
end

--- 问两个信号，回 "判出的档 or nil" + "凭什么判的"（后者只进 /gcl probe 与 /gcl lang）。
--- 全程 pcall + type 检查：接口缺席、报错、回非字符串都只算"这个信号没答"，绝不抛到登录链上。
local function DetectSignal()
    local textLocale, realm
    local fn = _G.GetLocale
    if type(fn) == "function" then
        local okCall, v = pcall(fn)
        if okCall and type(v) == "string" then textLocale = v end
    end
    local rf = _G.GetRealmName
    if type(rf) == "function" then
        local okCall, v = pcall(rf)
        if okCall and type(v) == "string" and v ~= "" then realm = v end
    end
    if textLocale == "zhCN" or textLocale == "zhTW" then
        return "zhCN", ("客户端文本语言就是 %s"):format(textLocale), textLocale, realm
    end
    if Locale.RealmHasCJK(realm) then
        return "zhCN", ("文本语言 %s 不算数，服名 %s 里有中文字节")
            :format(tostring(textLocale), tostring(realm)), textLocale, realm
    end
    if realm then
        return "enUS", ("服名 %s 里没有中文字节"):format(realm), textLocale, realm
    end
    return nil, "两个信号都没答：GetLocale=" .. tostring(textLocale)
        .. " / GetRealmName=" .. tostring(realm), textLocale, realm
end

--- @return string|nil  判得出给 "zhCN"/"enUS"；两个信号都问不出返回 nil（= 保持默认档）
function Locale.Detect()
    return (DetectSignal())
end

---------------------------------------------------------------------------
-- 这次到底用了哪档、是谁定的（只给 /gcl probe 与 /gcl lang 念，窗口上一个字都不显示）
--   source = "init"     ADDON_LOADED 之后、还没登录：吃默认档
--            "auto"     自动判定定的
--            "chosen"   玩家用 /gcl lang 选的（存在 settings.language 里）
--            "default"  自动判定没判出来，吃默认档（reason 写清为什么）
--            "invalid"  存档里写的代码不是中英两档（手改存档 / 旧版本留下的）
---------------------------------------------------------------------------

local decision = { source = "init", code = DEFAULT, reason = "还没到登录，先吃的默认档" }

function Locale.GetDecision()
    return decision
end

local function Record(source, code, reason)
    decision.source, decision.code, decision.reason = source, code, reason
    return decision
end

local function StoredSetting()
    local db = _G.GCLDB
    if type(db) ~= "table" or type(db.settings) ~= "table" then return nil end
    return db.settings.language
end

--- 写/清玩家的选择。回 false = 存档还没建好（ADDON_LOADED 之前），这次只活在本场会话里。
local function Store(code)
    local db = _G.GCLDB
    if type(db) ~= "table" or type(db.settings) ~= "table" then return false end
    db.settings.language = code
    return true
end

--- 登录时的正主：玩家选过就听玩家的，没选过（nil / "auto"）才跑自动判定。
--- 判出来的代码**绝不写回存档**：写回去等于把这一次登录看到的服当成玩家的决定，
--- 以后换了客户端语言/换了号就再也判不回来了。
function Locale.ResolveForLogin()
    local code = StoredSetting()
    if code == nil or code == "auto" then
        local det, why = DetectSignal()
        if det then
            Locale.Set(det)
            return Record("auto", det, why)
        end
        Locale.Set(DEFAULT)
        return Record("default", DEFAULT, why)
    end
    if type(code) ~= "string" or not Locale.CODES[code] then
        Locale.Set(DEFAULT)
        return Record("invalid", DEFAULT, "存档 settings.language 写的是 " .. tostring(code))
    end
    Locale.Set(code)
    return Record("chosen", code, "存档 settings.language=" .. tostring(code))
end

--- 判定结论的一行人话（/gcl lang 与 /gcl probe 共用；证据措辞只出现在这两处，不上窗口）。
function Locale.Describe()
    if decision.source == "auto" then
        return ("自动判定（%s）"):format(decision.reason)
    elseif decision.source == "chosen" then
        return "你选的（/gcl lang），记在存档里"
    elseif decision.source == "invalid" then
        return ("没判成：%s，退回默认档"):format(decision.reason)
    elseif decision.source == "default" then
        return ("自动判定没判出来：%s，用的默认档"):format(decision.reason)
    end
    return decision.reason
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
-- 生命周期：ADDON_LOADED 只把存档里已选的语言应用上；判定要问服名，那一问留到 PLAYER_LOGIN。
---------------------------------------------------------------------------

function Locale:OnInitialize()
    local code = StoredSetting()
    Locale.ApplySetting(code)
    -- 记一笔"还没判定"而不是直接写死默认档：probe 在登录前跑也要说得出依据。
    Record("init", Locale.GetLocale(), "还没到 PLAYER_LOGIN，先按存档摆：" .. tostring(code))
end

function Locale:OnLogin()
    return Locale.ResolveForLogin()
end

---------------------------------------------------------------------------
-- /gcl lang：看当前用的是哪档、这次是谁定的，或者改掉它。
-- 认错的代码当场回绝、把能写的值念出来：玩家打的字没生效又不吭声，比报错更难查。
---------------------------------------------------------------------------

local LANG_ARGS = { zhcn = "zhCN", enus = "enUS", auto = "auto" }

--- 换档后把已经建好的窗子重画一遍（没建过时 Calendar.Refresh 自己 no-op，不用在这儿判）。
local function Repaint()
    local cal = GCL and GCL.Calendar
    if type(cal) == "table" and type(cal.Refresh) == "function" then
        pcall(cal.Refresh)
    end
end

local function OnLangCommand(rest)
    local arg = (type(rest) == "string" and rest:match("^%s*(%S*)")) or ""
    if arg == "" then
        print(("GCL: 界面语言 %s —— %s"):format(decision.code, Locale.Describe()))
        print("GCL: 改成中文 /gcl lang zhCN，改成英文 /gcl lang enUS，交回自动判定 /gcl lang auto")
        return true
    end
    local want = LANG_ARGS[arg:lower()]
    if not want then
        print(("GCL: 语言 %s 不认识，能写的只有 zhCN、enUS、auto（留空是看当前）"):format(arg))
        return false
    end
    local stored
    if want == "auto" then
        -- 清掉选择而不是写 "auto"：档里没这个键才是不折不扣的"没选过"，
        -- 判出来的档一律不落盘（理由见 ResolveForLogin 上方）。
        stored = Store(nil)
        Locale.ResolveForLogin()
    else
        stored = Store(want)
        Locale.Set(want)
        Record("chosen", want, "存档 settings.language=" .. want)
    end
    Repaint()
    print(("GCL: 界面语言现在是 %s —— %s%s"):format(decision.code, Locale.Describe(),
        stored and "" or "（存档还没建好，这一改只在本场会话里有效）"))
    return true
end

---------------------------------------------------------------------------
-- /gcl probe 里属于本地化那两行：凭什么定成这一档，是"我们怎么知道的"，只进取证。
---------------------------------------------------------------------------

local function SignalText(fn)
    if type(fn) ~= "function" then return "没有这个接口" end
    local okCall, v = pcall(fn)
    if not okCall then return "接口抛错" end
    if v == nil or v == "" then return "答不出" end
    return tostring(v)
end

local function PrintProbeSection()
    print(("GCL probe: 语言 界面用=%s 来源=%s 依据=%s")
          :format(decision.code, decision.source, tostring(decision.reason)))
    print(("GCL probe: 语言信号 GetLocale=%s GetRealmName=%s 判出=%s 存档 settings.language=%s")
          :format(SignalText(_G.GetLocale), SignalText(_G.GetRealmName),
              tostring(Locale.Detect()), tostring(StoredSetting())))
end

if GCL.RegisterCommand then
    GCL.RegisterCommand("lang", OnLangCommand, "看/改界面语言：/gcl lang [zhCN|enUS|auto]")
    if type(GCL.ChainProbe) == "function" then
        GCL.ChainProbe("lang", PrintProbeSection)
    end
end

return Locale
