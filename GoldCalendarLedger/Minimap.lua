-- 小地图按钮（T4）：挂在 Minimap 环上的一颗入口，能按住拖着换位置。
-- 这个文件不记账、不算数、不画界面，只做两件事：把日历窗叫出来（Calendar.Toggle），
-- 把拖出来的角度存进 GCLDB.settings.minimapAngle。
-- 它也不订任何事件（建按钮由 Core 的 OnLogin 钩子叫一次，只此一次），
-- 空闲状态一个每帧脚本都不挂：OnUpdate 只在按住拖动期间存在，松手立刻摘掉。

local GCL = _G.GCL
local Minimap = {}

if GCL and GCL.RegisterModule then
    GCL.RegisterModule("Minimap", Minimap)
else
    GCL = GCL or {}
    GCL.Minimap = Minimap
end

local DEFAULT_ANGLE = 200   -- 度（约右下方）：环上别的插件多在正上/正下，先错开
local BTN_SIZE      = 32
local HIGHLIGHT     = "Interface\\Buttons\\UI-Common-MouseHilight"

-- 宿主只在建按钮那一刻抓一次：之后放环/拖动只读这个上值，不再直连全局 Minimap。
-- 判降级看的就是 host，用的时候换成全局等于绕过那道判断（按钮建好后照样能踩空）。
local minimapHost, button

---------------------------------------------------------------------------
-- 素材路径与角度
---------------------------------------------------------------------------

--- 图标路径一律由 toc 名（= 文件夹名）拼出来：目录改名时图标跟着走，
--- 写死 "GoldCalendarLedger" 会在改名后画成一片空白（计划 §5 #9 的那一处）。
function Minimap.IconPath()
    local n = GCL and GCL.addonName
    if type(n) ~= "string" or n == "" then return nil end
    return ("Interface\\AddOns\\%s\\%s"):format(n, n)
end

function Minimap.Angle()
    local db = _G.GCLDB
    if type(db) == "table" and type(db.settings) == "table" then
        local a = tonumber(db.settings.minimapAngle)
        if a then return a end
    end
    return DEFAULT_ANGLE
end

local function StoreAngle(deg)
    local db = _G.GCLDB
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
    local okCall, v = pcall(fn, tostring(GCL and GCL.addonName), "IconTexture")
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
    local loc = GCL and GCL.Locale
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
    local cal = GCL and GCL.Calendar
    if type(cal) == "table" and type(cal.Toggle) == "function" then
        local okCall, err = pcall(cal.Toggle)
        if not okCall then print("GCL: 日历窗没开起来：" .. tostring(err)) end
        return okCall
    end
    print("GCL: 日历模块不在（toc 少了一行？）：窗口命令是 /gcl open")
    return false
end

local function OnEnter(self)
    local tip = Tip()
    if not tip then return false end
    -- 三行全是玩家要的：名字、点了会怎样、哪个版本。
    -- "为什么是这个名字/素材哪来的"这类话只进 /gcl probe，一句都不上提示。
    local okCall, err = pcall(function()
        tip:SetOwner(self, "ANCHOR_LEFT")
        tip:ClearLines()
        tip:AddLine(T("CAL_TITLE"))
        tip:AddLine(T("MINIMAP_CLICK"), 0.8, 0.8, 0.8)
        if GCL.VERSION then
            tip:AddLine("v" .. tostring(GCL.VERSION), 0.5, 0.5, 0.5)
        end
        tip:Show()
    end)
    if not okCall then print("GCL: 悬停提示没弹出来：" .. tostring(err)) end
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
    local btn = CreateFrame("Button", "GCLMapButton", minimapHost)
    btn:SetSize(BTN_SIZE, BTN_SIZE)
    -- 抬几层：环上插件一堆，压在小地图下面等于没画。
    local okL, level = pcall(minimapHost.GetFrameLevel, minimapHost)
    btn:SetFrameLevel(((okL and tonumber(level)) or 1) + 5)
    local path = Minimap.IconPath()
    if path then
        btn:SetNormalTexture(path)
    else
        print("GCL: 小地图按钮没有图标：toc 名（GCL.addonName）读不出，取证看 /gcl probe")
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

--- @return Frame|nil  取不到宿主或建到一半抛错都回 nil：入口还有 /gcl open，不许连累登录
function Minimap.EnsureButton()
    if button then return button end
    local host = _G.Minimap
    -- 只判真假，不判 type：帧对象由引擎造，Lua 看到的类型不保证是 table。
    if not host then
        print("GCL: 小地图按钮没建：这台客户端上找不到 Minimap 这一帧。窗口照旧用 /gcl open 打开")
        return nil
    end
    minimapHost = host
    local okCall, res = pcall(BuildButton)
    if not okCall then
        minimapHost = nil
        print("GCL: 小地图按钮没建起来：" .. tostring(res) .. "。窗口照旧用 /gcl open 打开")
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
-- /gcl probe 里属于这一任务的那几行（证据措辞只进取证，一句不上窗口）
---------------------------------------------------------------------------

local function PrintProbeSection()
    local path = Minimap.IconPath()
    local declared, whyNoMeta = ReadIconMeta()
    print(("GCL probe: 图标 按钮用的=%s toc声明的=%s%s")
          :format(tostring(path), tostring(declared),
              declared == nil and ("（读不出：" .. tostring(whyNoMeta) .. "）")
                  or ((declared == path) and "（两处一致）" or "（两处不一致：改了一处！）")))
    -- .tga 的字节在客户端里读不出：本仓库没有证过的"文件在不在"接口，
    -- 这条不许猜，只把离线那一关（用例 test_bundled_icon_tga_*）说的口径复述一遍。
    print("GCL probe: 图标素材 随包 tga 在不在 Lua 读不出：判据是插件列表与小地图有没有画上方格图")

    local host = _G.Minimap
    if not host then
        print("GCL probe: 小地图宿主 Minimap=没有这个全局 ⇒ 按钮降级，入口只剩 /gcl open")
        return
    end
    local okW, w = pcall(host.GetWidth, host)
    local okH, h = pcall(host.GetHeight, host)
    print(("GCL probe: 小地图宿主 Minimap=有 尺寸=%sx%s 角度=%s 存档里存过=%s")
          :format(okW and tostring(w) or "问不出", okH and tostring(h) or "问不出",
              tostring(Minimap.Angle()),
              tostring(type(_G.GCLDB) == "table" and type(_G.GCLDB.settings) == "table"
                  and _G.GCLDB.settings.minimapAngle or nil)))
    if not button then
        print("GCL probe: 按钮=还没建（OnLogin 没跑到，或那次宿主缺席）")
        return
    end
    local okShown, shown = pcall(button.IsShown, button)
    print(("GCL probe: 按钮=GCLMapButton 显隐=%s 尺寸=%sx%s 空闲每帧脚本=%s")
          :format(tostring(okShown and shown or false),
              tostring(button:GetWidth()), tostring(button:GetHeight()),
              tostring(button:GetScript("OnUpdate") ~= nil and "挂着（不该有）" or "无")))
end

if type(GCL.ChainProbe) == "function" then
    GCL.ChainProbe("map", PrintProbeSection)
end

return Minimap
