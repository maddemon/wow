-- 手动添加任务：在不碰命令行的前提下，把世界任务加进账本。
-- 两条入口：
--   1) 世界地图上右键世界任务 pin → 加该任务（主路径，最贴近"点任务加进去"）。
--   2) 矩阵窗口"添加任务"按钮 → 弹框输入 ID（兜底，地图 hook 在你的客户端没生效也能用）。
-- 地图 hook 走防御式：所有内部结构名都 pcall 试，取不到就降级记日志，绝不加载期报错。

WQT.Track = {}
local Track = WQT.Track

-- 弱键：pin 帧由客户端的池子持有，我们只记"挂过了"，不该替它续命
local hookedPins = setmetatable({}, { __mode = "k" })
local hookedCount = 0

--- 从 pin 帧里解出 questID。世界任务 pin 在不同版本字段名不一：
--- 有的直接挂在 pin.questID，有的在 pin.info.questID，有的有 GetQuestID() 方法。
local function PinQuestID(pin)
    if type(pin) ~= "table" then return nil end
    if tonumber(pin.questID) then return tonumber(pin.questID) end
    if type(pin.info) == "table" and tonumber(pin.info.questID) then
        return tonumber(pin.info.questID)
    end
    if WQT.Api and WQT.Api.Callable(pin.GetQuestID) then
        local ok, v = pcall(pin.GetQuestID, pin)
        if ok and tonumber(v) then return tonumber(v) end
    end
    return nil
end

--- 这枚 pin 是不是世界任务。模板名含 WorldQuest 的算；能解出 questID 的也算
--- （其它普通地图 pin 一般没有 questID，不会误挂）。
local function IsWorldQuestPin(pin)
    local ok, name = pcall(pin.GetName, pin)
    if ok and type(name) == "string" and name:find("WorldQuest") then return true end
    return PinQuestID(pin) ~= nil
end

local function OnPinClick(self, button)
    -- 地图右键=返回上一级、Ctrl+左键=定位标记，都被原生占用，不能用；
    -- 改 Shift+左键 触发添加（用户实测这两条原生键已被占）。原生左键"打开任务详情"
    -- 仍会触发（HookScript 拦不掉默认行为），添加同时会弹任务面板，关掉即可。
    if button ~= "LeftButton" then return end
    if not (IsShiftKeyDown and IsShiftKeyDown()) then return end
    local qid = PinQuestID(self)
    if qid then Track.AddQuest(qid) end
end

local function HookPin(pin)
    if hookedPins[pin] or not IsWorldQuestPin(pin) then return end
    hookedPins[pin] = true
    hookedCount = hookedCount + 1
    pcall(pin.HookScript, pin, "OnClick", OnPinClick)
end

--- 子帧遍历优先走 EnumerateChildren：逐个给子帧、不建临时表（现代客户端才有）。
--- 原先每层都 `{ pcall(frame.GetChildren, frame) }`，世界地图那棵几千帧的树扫一遍
--- 就是几千张待回收的小表 —— 一开图就卡一下的直接来源；没有该接口的客户端退回原路径。
local Callable = function(fn) return WQT.Api and WQT.Api.Callable(fn) == true end

local function Walk(frame, depth)
    if not frame or depth > 10 then return end
    HookPin(frame)
    if Callable(frame.EnumerateChildren) then
        local okIt, it = pcall(frame.EnumerateChildren, frame)
        if okIt and it ~= nil and Callable(it.Next) then
            while true do
                local okNext, _, kid = pcall(it.Next, it)
                if not okNext or kid == nil then break end
                Walk(kid, depth + 1)
            end
            return
        end
    end
    if not Callable(frame.GetChildren) then return end
    local kids = { pcall(frame.GetChildren, frame) }
    if kids[1] == true then
        for i = 2, #kids do Walk(kids[i], depth + 1) end
    end
end

--- 图没开着就没得扫：pin 池只在地图显示时才有激活对象，而 WORLD_MAP_UPDATE
--- 在图关着的时候照样发（换区域、小地图刷新都触发）。不挡这一条，跑图路上
--- 每一片新区域都要空跑一遍全树。取不到 IsShown 时按"开着"处理，宁可多扫一次。
local function MapIsShown(wmf)
    if not Callable(wmf.IsShown) then return true end
    local okShown, v = pcall(wmf.IsShown, wmf)
    if not okShown then return true end
    return v == true
end

--- 扫当前世界地图的所有 pin，给世界任务 pin 挂右键处理。
--- 现代客户端 pin 在 WorldMapFrame.pinPools 里按模板分池，activeObjects 是当前激活的 pin 集。
--- 内部字段名跨版本可能变，全程 pcall；取不到就不挂（降级由 Init 记日志）。
--- 两路并进：
---   1) 快速路径：原生 pin 池（多数客户端世界任务 pin 在这里）；
---   2) 兜底路径：递归遍历世界地图整棵帧树，钩住任何带 questID 的帧。
---      彩色环不管是哪个插件画的，只要它那个 pin 帧上有 questID 字段，
---      就逃不过这趟遍历——与具体插件解耦，因此"不知道是哪个插件"也无所谓。
---      hookedPins 按帧引用缓存（弱键，不替客户端的池子续命），已钩过的帧不再重复处理。
local function ScanPins()
    local wmf = WQT.Api and WQT.Api.Resolve("WorldMapFrame")
    if not wmf then return end
    if not MapIsShown(wmf) then return end
    pcall(function()
        local pools = wmf.pinPools
        if type(pools) == "table" then
            for _, pool in pairs(pools) do
                if type(pool) == "table" and type(pool.activeObjects) == "table" then
                    for pin in pairs(pool.activeObjects) do HookPin(pin) end
                end
            end
        end
    end)
    pcall(Walk, wmf, 0)
end

--- 加/移除共用的时间戳：优先墙上时钟（跨开机可比），问不到退回会话秒。
local function NowSeconds()
    local now
    if WQT.Api and WQT.Api.WallClock then now = WQT.Api.WallClock() end
    if not now and WQT.Api and WQT.Api.Uptime then now = WQT.Api.Uptime() end
    return now or 0
end

--- 对外入口：把某个 questID 加进账本并反馈。被地图右键与矩阵弹框共用。
--- 点过"移除"的行再点一次 = 恢复显示（Ledger 里清 ignored），提示要说清是哪一种，
--- 否则用户看见"已添加"其实做的是"取消忽略"。
function Track.AddQuest(questID)
    local db = WQT.Config and WQT.Config.DB()
    if not db then print("WQT: 账本未就绪") return end
    local qid = tonumber(questID)
    local rec0 = qid and db.quests and db.quests[qid]
    local wasIgnored = not not (rec0 and rec0.ignored)
    local now = NowSeconds()
    local ok, msg = WQT.Ledger.AddQuest(db, questID, now)
    if ok then
        local rec = db.quests[qid]
        local name = (rec and rec.name) or ("#" .. tostring(questID))
        print(wasIgnored
            and ("WQT: 已恢复显示任务 %s"):format(name)
            or ("WQT: 已添加任务 %s"):format(name))
        WQT.Config.Log(("手动添加任务 quest=%s %s"):format(tostring(questID), msg or ""))
        if WQT.Matrix and WQT.Matrix.IsOpen and WQT.Matrix.IsOpen() then
            WQT.Matrix.Render()
        end
    else
        print("WQT: 添加失败：" .. tostring(msg))
    end
end

--- 移除错加/非金币任务。被矩阵里"右键任务名"调用；同样不需要手输 ID。
--- 记的是"忽略"而不是删记录：下次快照池子/日志还会把同一条建回来，真删等于删不掉
--- （12.1.5 实测反馈）。要恢复就在矩阵"添加任务"列表里点它。
function Track.RemoveQuest(questID)
    local db = WQT.Config and WQT.Config.DB()
    if not db then print("WQT: 账本未就绪") return end
    local ok, msg = WQT.Ledger.RemoveQuest(db, questID, NowSeconds())
    if ok then
        print(("WQT: 已忽略任务 #%s（不会再自动刷出来；要恢复点矩阵的\"添加任务\"）")
            :format(tostring(questID)))
        WQT.Config.Log(("手动忽略任务 quest=%s"):format(tostring(questID)))
        if WQT.Matrix and WQT.Matrix.IsOpen and WQT.Matrix.IsOpen() then
            WQT.Matrix.Render()
        end
    else
        print("WQT: 移除失败：" .. tostring(msg))
    end
end

--- 挂地图 hook。地图打开/内容变化时重扫 pin；WORLD_MAP_UPDATE 没触发也不影响
--- （OnShow 兜底）。每次进入世界由 Core 调一次，但 hook 整个会话只挂一次：
--- PLAYER_ENTERING_WORLD 每次过图都重发，而 HookScript 是追加、CreateFrame 是新建 ——
--- 不守卫的话过图 N 次就有 N 套处理器，每次地图刷新要 N 遍全树递归，越玩越卡（卡顿主源）。
--- WorldMapFrame 还没建出来时如实降级，下次进世界再试。
local hookFrame         -- WORLD_MAP_UPDATE 监听帧：全会话只建一个
local showHooked        -- OnShow 只追加一次
function Track.Init()
    local wmf = WQT.Api and WQT.Api.Resolve("WorldMapFrame")
    if not wmf then
        WQT.Config.Log("手动添加-地图 hook 降级：WorldMapFrame 不可用（改用矩阵'添加任务'按钮）")
        return
    end
    if not showHooked and Callable(wmf.HookScript) then
        showHooked = true
        pcall(wmf.HookScript, wmf, "OnShow", function() pcall(ScanPins) end)
    end
    if not hookFrame then
        hookFrame = CreateFrame("Frame")
        hookFrame:SetScript("OnEvent", function() pcall(ScanPins) end)
        -- 未知事件名会让 RegisterEvent 直接抛错（12.1.5 实测，Sync 也踩过）：
        -- 认不下这条事件就只靠 OnShow 那一路，不许把 hook 整条带崩
        local okReg, err = pcall(hookFrame.RegisterEvent, hookFrame, "WORLD_MAP_UPDATE")
        if not okReg then WQT.Config.Log("注册事件失败 WORLD_MAP_UPDATE: " .. tostring(err)) end
        WQT.Config.Log("手动添加-地图 hook 已挂（右键世界任务 pin 添加；左键仍打开任务）")
    end
    pcall(ScanPins)   -- 地图可能早已开着
end

function Track.Status()
    return ("地图hook=已挂 已挂pin=" .. tostring(hookedCount))
end
