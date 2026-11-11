-- 小地图按钮（T4）：挂在 Minimap 环上的一颗入口，能按住拖着换位置。
-- 这个文件不记账、不写档、不画界面：把日历窗叫出来（Calendar.Toggle），
-- 把拖出来的角度存进 GDLDB.settings.minimapAngle，悬停时只读一遍今天的/本月的
-- 收支（形状照 GoldLedger 的同款悬浮，2026-11-10 用户点名要的）。
-- 它也不订任何事件（建按钮由 Core 的 OnLogin 钩子叫一次，只此一次），
-- 空闲状态一个每帧脚本都不挂：OnUpdate 只在按住拖动期间存在，松手立刻摘掉。

local GDL = _G.GDL
local Minimap = {}

if GDL and GDL.RegisterModule then
    GDL.RegisterModule("Minimap", Minimap)
else
    GDL = GDL or {}
    GDL.Minimap = Minimap
end

local DEFAULT_ANGLE = 200   -- 度（约右下方）：环上别的插件多在正上/正下，先错开
local BTN_SIZE      = 26    -- 用户 2026-11-11：32 在环上还是偏大，缩到 26（图标铺满按钮）
local HIGHLIGHT     = "Interface\\Buttons\\UI-Common-MouseHilight"

-- 宿主只在建按钮那一刻抓一次：之后放环/拖动只读这个上值，不再直连全局 Minimap。
-- 判降级看的就是 host，用的时候换成全局等于绕过那道判断（按钮建好后照样能踩空）。
local minimapHost, button

---------------------------------------------------------------------------
-- 素材路径与角度
---------------------------------------------------------------------------

--- 图标路径一律由 toc 名（= 文件夹名）拼出来：目录改名时图标跟着走，
--- 写死 "GoldDailyLedger" 会在改名后画成一片空白（计划 §5 #9 的那一处）。
function Minimap.IconPath()
    local n = GDL and GDL.addonName
    if type(n) ~= "string" or n == "" then return nil end
    return ("Interface\\AddOns\\%s\\%s"):format(n, n)
end

function Minimap.Angle()
    local db = _G.GDLDB
    if type(db) == "table" and type(db.settings) == "table" then
        local a = tonumber(db.settings.minimapAngle)
        if a then return a end
    end
    return DEFAULT_ANGLE
end

local function StoreAngle(deg)
    local db = _G.GDLDB
    if type(db) ~= "table" or type(db.settings) ~= "table" then return false end
    db.settings.minimapAngle = deg
    return true
end

--- 读 toc 声明的那颗图标（## IconTexture）：C_AddOns.GetAddOnMetadata 是本仓库
--- 用过的接口（GoldLedger/Core.lua:20 取版本号就是它），缺席时老实说读不出。
local function ReadIconMeta()
    local bag = _G.C_AddOns or _G.GetAddOnMetadata
    local fn = (type(bag) == "table" and bag.GetAddOnMetadata) or bag
    if type(fn) ~= "function" then return nil, "没有 C_AddOns.GetAddOnMetadata" end
    local okCall, v = pcall(fn, tostring(GDL and GDL.addonName), "IconTexture")
    if not okCall then return nil, "接口抛错" end
    if type(v) ~= "string" or v == "" then return nil, "答不出（toc 里没这行？）" end
    return v, nil
end

---------------------------------------------------------------------------
-- 环上定位与悬停提示
---------------------------------------------------------------------------

--- 骑在地图边缘：圆心取宿主中心，半径 = 半宽 + 半个图标 − 4。
--- 宿主尺寸问不到就按 160 估（暴雪小地图的常规边长），位置偏一点不碍事。
local function Place(btn)
    local host = minimapHost
    if not host then return false end
    local okW, w = pcall(host.GetWidth, host)
    local ring = (okW and tonumber(w) and tonumber(w) > 0 and tonumber(w)) or 160
    local okB, bw = pcall(btn.GetWidth, btn)
    local half = ((okB and tonumber(bw) and tonumber(bw) > 0 and tonumber(bw)) or BTN_SIZE) / 2
    local rad = math.rad(Minimap.Angle())
    btn:ClearAllPoints()
    pcall(btn.SetPoint, btn, "CENTER", host, "CENTER",
        math.cos(rad) * (ring / 2 + half - 4), math.sin(rad) * (ring / 2 + half - 4))
    return true
end

local function T(key)
    local loc = GDL and GDL.Locale
    if type(loc) == "table" and type(loc.Get) == "function" then
        local okCall, r = pcall(loc.Get, key)
        if okCall and type(r) == "string" then return r end
    end
    return tostring(key)
end

local function Tip()
    local tip = _G.GameTooltip
    if type(tip) ~= "table" then return nil end
    if type(tip.SetOwner) ~= "function" or type(tip.AddLine) ~= "function"
        or type(tip.ClearLines) ~= "function" or type(tip.Show) ~= "function" then return nil end
    return tip
end

--- 点击只做一件事：开/关日历窗。Calendar 缺席（toc 被改坏）时指一条路，不抛错。
local function ToggleCalendar()
    local cal = GDL and GDL.Calendar
    if type(cal) == "table" and type(cal.Toggle) == "function" then
        local okCall, err = pcall(cal.Toggle)
        if not okCall then print("GDL: 日历窗没开起来：" .. tostring(err)) end
        return okCall
    end
    print("GDL: 日历模块不在（toc 少了一行？）：窗口命令是 /gdl open")
    return false
end

--- 只读账本（GetRangeTotals / TodayKey / DaysInMonth 都是 Ledger 的公开函数）。
--- 形状照 Calendar.lua 的 LCall：先验函数在，再 pcall，缺哪层都回 nil 绝不抛。
local function LedgerCall(name, ...)
    local l = GDL and GDL.Ledger
    if type(l) ~= "table" or type(l[name]) ~= "function" then return nil end
    local okCall, r = pcall(l[name], ...)
    if not okCall then return nil end
    return r
end

--- 金额统一走日历的 FormatMoneyIcon（只显示金币，大额用「万 / 亿」压缩）：
--- 用户 2026-11-11 定「只有流水显示银/铜，包括所有悬停只写金币」。
--- 两处各写一份、改一处漏一处就是"列表一个样式、提示另一个样式"（兄弟插件 2026-11-10 踩过）。
local function MoneyText(copper)
    local cal = GDL and GDL.Calendar
    if type(cal) == "table" and type(cal.FormatMoneyIcon) == "function" then
        local okCall, s = pcall(cal.FormatMoneyIcon, copper)
        if okCall and type(s) == "string" then return s end
    end
    return tostring(copper or 0)
end

--- 前一天（纯 ISO 键算，不碰客户端时间戳）：跨月回退到上月最后一天。
local function DayBeforeKey(key)
    local y, m, d = key:match("^(%d%d%d%d)%-(%d%d)%-(%d%d)$")
    y, m, d = tonumber(y), tonumber(m), tonumber(d)
    if not (y and m and d) then return nil end
    d = d - 1
    if d >= 1 then return ("%04d-%02d-%02d"):format(y, m, d) end
    m = m - 1
    if m < 1 then m, y = 12, y - 1 end
    local dim = LedgerCall("DaysInMonth", y, m)
    if not dim then return nil end
    return ("%04d-%02d-%02d"):format(y, m, dim)
end

--- 本周起点：跟日历的「本周」同一个周 CD 口径（周四重置）。
--- 时钟/CD 读不出就整段不写；这里不自己造一个假的周起点。
local function WeekRange(todayKey)
    local cal = GDL and GDL.Calendar
    if type(cal) ~= "table" or type(cal.WeekCDRange) ~= "function" then return nil end
    local okCall, s, e = pcall(cal.WeekCDRange)
    if okCall and type(s) == "string" and type(e) == "string" then return s, e end
    return nil
end

--- 悬停净收入段（用户 2026-11-11 定）：只统计净收入，展示今日 / 昨日 / 本周；
--- 金额只写金币（大额走万/亿）。GameTooltip 缺 AddDoubleLine 时退 AddLine。
local function AddStats(tip, todayKey)
    if type(tip.AddDoubleLine) ~= "function" and type(tip.AddLine) ~= "function" then
        return
    end
    local put = function(left, right)
        if type(tip.AddDoubleLine) == "function" then
            tip:AddDoubleLine(left, right, 1, 1, 1, 1, 1, 1)
        else
            tip:AddLine(tostring(left) .. " " .. tostring(right), 1, 1, 1)
        end
    end
    local weekStart = WeekRange(todayKey)
    local periods = {
        { label = T("MINIMAP_TODAY"), s = todayKey, e = todayKey },
        { label = T("MINIMAP_YESTERDAY"), s = DayBeforeKey(todayKey) },
        { label = T("MINIMAP_WEEK"), s = weekStart, e = todayKey },
    }
    for _, p in ipairs(periods) do
        local s = p.s
        local e = p.e or p.s
        if type(s) == "string" and type(e) == "string" then
            local t = LedgerCall("GetRangeTotals", s, e)
            if type(t) == "table" then
                local net = tonumber(t.net)
                if net == nil then
                    net = (tonumber(t.income) or 0) - (tonumber(t.expense) or 0)
                end
                put(p.label, MoneyText(net))
            end
        end
    end
end

local function OnEnter(self)
    local tip = Tip()
    if not tip then return false end
    -- 三行全是玩家要的：名字、点了会怎样、哪个版本。
    -- "为什么是这个名字/素材哪来的"这类话只进 /gdl probe，一句都不上提示。
    local okCall, err = pcall(function()
        tip:SetOwner(self, "ANCHOR_LEFT")
        tip:ClearLines()
        tip:AddLine(T("CAL_TITLE"))
        -- 今日 / 昨日 / 本周净收入（账本缺席时整段不出现）
        local todayKey = LedgerCall("TodayKey")
        if type(todayKey) == "string" then
            AddStats(tip, todayKey)
        end
        tip:AddLine(T("MINIMAP_CLICK"), 0.8, 0.8, 0.8)
        if GDL.VERSION then
            tip:AddLine("v" .. tostring(GDL.VERSION), 0.5, 0.5, 0.5)
        end
        tip:Show()
    end)
    if not okCall then print("GDL: 悬停提示没弹出来：" .. tostring(err)) end
    return okCall
end

local function OnLeave()
    local tip = _G.GameTooltip
    if type(tip) == "table" and type(tip.Hide) == "function" then
        pcall(tip.Hide, tip)
    end
    return true
end

---------------------------------------------------------------------------
-- 拖动沿环换位：OnUpdate 只在按住期间存在
---------------------------------------------------------------------------

local function DragUpdate(self)
    local host = minimapHost
    if not host then return false end
    local okC, cx, cy = pcall(host.GetCenter, host)
    if not okC or type(cx) ~= "number" or type(cy) ~= "number" then return false end
    local okS, s = pcall(host.GetEffectiveScale, host)
    local scale = (okS and tonumber(s) and tonumber(s) > 0) and tonumber(s) or 1
    local pointer = _G.GetCursorPosition
    if type(pointer) ~= "function" then return false end
    local okP, px, py = pcall(pointer)
    if not okP or type(px) ~= "number" or type(py) ~= "number" then return false end
    -- atan2 在真客户端（Lua 5.1）有、离线 lupa 那边并进了两参 math.atan：取先有的那个，
    -- 两边算出来的是同一个角。光标给的是物理像素 ⇒ 除以宿主缩放，纵向不取反
    -- （小地图坐标系与屏幕同向，y 大 = 靠上）。
    local atan2 = math.atan2 or math.atan
    local okA, deg = pcall(function()
        return math.deg(atan2((py / scale) - cy, (px / scale) - cx))
    end)
    if not okA or type(deg) ~= "number" then return false end
    StoreAngle(deg)
    return Place(self)
end

local function OnDragStart(self)
    self:SetScript("OnUpdate", DragUpdate)
    return true
end

local function OnDragStop(self)
    -- 松手立刻摘掉：留着就是全程轮询，用户明确禁止（用例 test_minimap_drag 钉死这一句）。
    self:SetScript("OnUpdate", nil)
    return true
end

---------------------------------------------------------------------------
-- 建按钮（只在 PLAYER_LOGIN 叫一次）
---------------------------------------------------------------------------

local function BuildButton()
    local btn = CreateFrame("Button", "GDLMapButton", minimapHost)
    btn:SetSize(BTN_SIZE, BTN_SIZE)
    -- 抬几层：环上插件一堆，压在小地图下面等于没画。
    local okL, level = pcall(minimapHost.GetFrameLevel, minimapHost)
    btn:SetFrameLevel(((okL and tonumber(level)) or 1) + 5)
    local path = Minimap.IconPath()
    if path then
        btn:SetNormalTexture(path)
    else
        print("GDL: 小地图按钮没有图标：toc 名（GDL.addonName）读不出，取证看 /gdl probe")
    end
    btn:SetHighlightTexture(HIGHLIGHT)
    btn:SetScript("OnClick", ToggleCalendar)
    btn:SetScript("OnEnter", OnEnter)
    btn:SetScript("OnLeave", OnLeave)
    btn:RegisterForDrag("LeftButton")
    btn:SetScript("OnDragStart", OnDragStart)
    btn:SetScript("OnDragStop", OnDragStop)
    Place(btn)
    btn:Show()
    return btn
end

--- @return Frame|nil  取不到宿主或建到一半抛错都回 nil：入口还有 /gdl open，不许连累登录
function Minimap.EnsureButton()
    if button then return button end
    local host = _G.Minimap
    -- 只判真假，不判 type：帧对象由引擎造，Lua 看到的类型不保证是 table。
    if not host then
        print("GDL: 小地图按钮没建：这台客户端上找不到 Minimap 这一帧。窗口照旧用 /gdl open 打开")
        return nil
    end
    minimapHost = host
    local okCall, res = pcall(BuildButton)
    if not okCall then
        minimapHost = nil
        print("GDL: 小地图按钮没建起来：" .. tostring(res) .. "。窗口照旧用 /gdl open 打开")
        return nil
    end
    button = res
    return button
end

function Minimap.GetButton()
    return button
end

function Minimap:OnLogin()
    return Minimap.EnsureButton()
end

---------------------------------------------------------------------------
-- /gdl probe 里属于这一任务的那几行（证据措辞只进取证，一句不上窗口）
---------------------------------------------------------------------------

local function PrintProbeSection()
    local path = Minimap.IconPath()
    local declared, whyNoMeta = ReadIconMeta()
    print(("GDL probe: 图标 按钮用的=%s toc声明的=%s%s")
          :format(tostring(path), tostring(declared),
              declared == nil and ("（读不出：" .. tostring(whyNoMeta) .. "）")
                  or ((declared == path) and "（两处一致）" or "（两处不一致：改了一处！）")))
    -- .tga 的字节在客户端里读不出：本仓库没有证过的"文件在不在"接口，
    -- 这条不许猜，只把离线那一关（用例 test_bundled_icon_tga_*）说的口径复述一遍。
    print("GDL probe: 图标素材 随包 tga 在不在 Lua 读不出：判据是插件列表与小地图有没有画上方格图")

    local host = _G.Minimap
    if not host then
        print("GDL probe: 小地图宿主 Minimap=没有这个全局 ⇒ 按钮降级，入口只剩 /gdl open")
        return
    end
    local okW, w = pcall(host.GetWidth, host)
    local okH, h = pcall(host.GetHeight, host)
    print(("GDL probe: 小地图宿主 Minimap=有 尺寸=%sx%s 角度=%s 存档里存过=%s")
          :format(okW and tostring(w) or "问不出", okH and tostring(h) or "问不出",
              tostring(Minimap.Angle()),
              tostring(type(_G.GDLDB) == "table" and type(_G.GDLDB.settings) == "table"
                  and _G.GDLDB.settings.minimapAngle or nil)))
    if not button then
        print("GDL probe: 按钮=还没建（OnLogin 没跑到，或那次宿主缺席）")
        return
    end
    local okShown, shown = pcall(button.IsShown, button)
    print(("GDL probe: 按钮=GDLMapButton 显隐=%s 尺寸=%sx%s 空闲每帧脚本=%s")
          :format(tostring(okShown and shown or false),
              tostring(button:GetWidth()), tostring(button:GetHeight()),
              tostring(button:GetScript("OnUpdate") ~= nil and "挂着（不该有）" or "无")))
end

if type(GDL.ChainProbe) == "function" then
    GDL.ChainProbe("map", PrintProbeSection)
end

return Minimap
