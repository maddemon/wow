-- 玩家侧的"加/撤"入口，全程不碰命令行。两条路：
--   1) 世界地图上 Shift+左键世界任务 pin → 记进账本 / 撤销屏蔽（主路径，最贴近"点任务加进去"；
--      右键与 Ctrl+左键都被原生地图占用，12.1.5 用户实测）。
--   2) 设置页关键词段的右栏点一行（兜底：走池，不依赖地图 hook，
--      也不许手输 ID —— 装了画环类插件时 pin 根本钩不到，那条实测就是这么坏的）。
-- 这一路的含义只有"撤销那句别再显示"（用户 2026-10-27：其意图是 取消 忽略 而不是标记 加了）：
-- 点完不换来任何上板凭据，那一行该不该列由客户端读到的证据 + 玩家配的关键词判（见 Ledger.MatrixRows）。
-- 地图 hook 走防御式：所有内部结构名都 pcall 试，取不到就降级记日志，绝不加载期报错。

GQL.Track = {}
local Track = GQL.Track

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
    if GQL.Api and GQL.Api.Callable(pin.GetQuestID) then
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

local Callable = function(fn) return GQL.Api and GQL.Api.Callable(fn) == true end

--- 子帧遍历一律走 Api.ChildFrames：迭代器函数那条快路径才有形状可走（本文件上一版
--- 认的是"带 .Next 的对象"，真客户端不这么回，于是这趟遍历实际一直在跑退路径；
--- 而退路径又只认多返回值形状，现代客户端回一张表 → 整棵帧树扫不到 pin，
--- Shift+点添加只剩 pinPools 那条快路径活着）。
--- 深度上限 10 层防环；世界地图那棵树几千帧，一次登录只扫一两趟，别加轮询。
local function Walk(frame, depth)
    if not frame or depth > 10 then return end
    HookPin(frame)
    local kids = GQL.Api and GQL.Api.ChildFrames(frame)
    if not kids then return end
    for i = 1, #kids do Walk(kids[i], depth + 1) end
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

--- 扫当前世界地图的所有 pin，给世界任务 pin 挂 Shift+左键处理。
--- 现代客户端 pin 在 WorldMapFrame.pinPools 里按模板分池，activeObjects 是当前激活的 pin 集。
--- 内部字段名跨版本可能变，全程 pcall；取不到就不挂（降级由 Init 记日志）。
--- 两路并进：
---   1) 快速路径：原生 pin 池（多数客户端世界任务 pin 在这里）；
---   2) 兜底路径：递归遍历世界地图整棵帧树，钩住任何带 questID 的帧。
---      彩色环不管是哪个插件画的，只要它那个 pin 帧上有 questID 字段，
---      就逃不过这趟遍历——与具体插件解耦，因此"不知道是哪个插件"也无所谓。
---      hookedPins 按帧引用缓存（弱键，不替客户端的池子续命），已钩过的帧不再重复处理。
local function ScanPins()
    local wmf = GQL.Api and GQL.Api.Resolve("WorldMapFrame")
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
    if GQL.Api and GQL.Api.WallClock then now = GQL.Api.WallClock() end
    if not now and GQL.Api and GQL.Api.Uptime then now = GQL.Api.Uptime() end
    return now or 0
end

--- 对外入口：撤销某个 questID 的"别再显示这一行"意图（顺带把这条记进账本）。
--- 被地图 pin 与设置页关键词段的右栏共用（2026-11-01 起关键词并进设置页，
--- 见 Options.OpenWindow）。玩家点这一行的全部含义就是**取消屏蔽**
--- （用户 2026-10-27 的原话："手动加回来 对应的就是忽略错了 再加回来，其意图是 取消 忽略
--- 而不是标记 加了"）—— 上一版这里还会给他盖一个 manualAdded 当矩阵准入门，等于把
--- 一次撤忽略当成一张上板通行证，那个字段本轮整体下线。
--- 两种情况分开报，因为玩家 next 要做的动作不一样：
---   · 之前屏蔽过 → 报"已取消忽略"，并交代这一行会不会真回来由自动判据算；
---   · 本来没屏蔽 → 说清"手动添加不再强制上板"，别让他以为点完就该看见它。
function Track.AddQuest(questID)
    local db = GQL.Config and GQL.Config.DB()
    if not db then print("GQL: 账本未就绪") return end
    local qid = tonumber(questID)
    local rec0 = qid and db.quests and db.quests[qid]
    local wasIgnored = not not (rec0 and rec0.ignored)
    local now = NowSeconds()
    local ok, msg = GQL.Ledger.AddQuest(db, questID, now)
    if ok then
        local rec = db.quests[qid]
        local name = (rec and rec.name) or ("#" .. tostring(questID))
        print(wasIgnored
            and ("GQL: 已取消忽略 %s（会不会重新上板，按两类依据判：玩家点名（关键词/注册周常）"
                .. " 或 奖励金币够门槛；做过不作数）"):format(name)
            or ("GQL: 已记进账本 %s（手动添加不强制上板；不在矩阵 = 插件判它这轮不该列）"):format(name))
        GQL.Config.Log(("取消忽略 quest=%s %s"):format(tostring(questID), msg or ""))
        if GQL.Matrix and GQL.Matrix.IsOpen and GQL.Matrix.IsOpen() then
            GQL.Matrix.Render()
        end
    else
        print("GQL: 添加失败：" .. tostring(msg))
    end
end

--- 屏蔽一条任务（点错了 / 关键词误命中了不想要的任务）。
--- 被「关键词 · 屏蔽管理」右栏的"忽略"按钮调用；同样不需要手输 ID。
--- 记的是**永久**屏蔽（Ledger.RemoveQuest）：下次同步池子/日志还会把同一条建回来，
--- 真删记录等于删不掉（12.1.5 实测反馈）；换 CD、换轮都不作废，只有玩家在"关键词 ·
--- 屏蔽管理"窗口里点"取消忽略"才撤 —— 所以提示必须把"永久"和"怎么恢复"两句都说清。
--- 主语用任务名：questID 是内部的东西，玩家读不到也不该读，账本没名字才退回编号。
function Track.RemoveQuest(questID)
    local db = GQL.Config and GQL.Config.DB()
    if not db then print("GQL: 账本未就绪") return end
    local ok, msg = GQL.Ledger.RemoveQuest(db, questID)
    if ok then
        local rec = db.quests and db.quests[tonumber(questID)]
        local name = (rec and rec.name) or ("#" .. tostring(questID))
        print(("GQL: 已永久屏蔽 %s（换 CD、换轮都不会自己回来；要恢复就去设置页的关键词段点回来）")
            :format(name))
        GQL.Config.Log(("永久屏蔽任务 quest=%s"):format(tostring(questID)))
        if GQL.Matrix and GQL.Matrix.IsOpen and GQL.Matrix.IsOpen() then
            GQL.Matrix.Render()
        end
    else
        print("GQL: 移除失败：" .. tostring(msg))
    end
end

--- 挂地图 hook。地图打开/内容变化时重扫 pin；WORLD_MAP_UPDATE 没触发也不影响
--- （OnShow 兜底）。每次进入世界由 Core 调一次，但 hook 整个会话只挂一次：
--- PLAYER_ENTERING_WORLD 每次过图都重发，而 HookScript 是追加、CreateFrame 是新建 ——
--- 不守卫的话过图 N 次就有 N 套处理器，每次地图刷新要 N 遍全树递归，越玩越卡（卡顿主源）。
--- WorldMapFrame 还没建出来时如实降级，下次进世界再试。
local hookFrame         -- WORLD_MAP_UPDATE 监听帧：全会话只建一个
local showHooked        -- OnShow 只追加一次
local warnedDegrade     -- "WorldMapFrame 不可用"这句提示每会话只报一次
function Track.Init()
    local wmf = GQL.Api and GQL.Api.Resolve("WorldMapFrame")
    if not wmf then
        -- 每次进世界 Core 都会调一遍 Track.Init；WorldMapFrame 迟迟建不出来时
        -- 这句会跟着每张地图刷一次，按会话只留一条。
        if not warnedDegrade then
            warnedDegrade = true
            GQL.Config.Log("手动添加-地图 hook 降级：WorldMapFrame 不可用（改用设置页关键词段的忽略/取消忽略）")
        end
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
        if not okReg then GQL.Config.Log("注册事件失败 WORLD_MAP_UPDATE: " .. tostring(err)) end
        GQL.Config.Log("手动添加-地图 hook 已挂（Shift+左键世界任务 pin 添加；左键仍打开任务）")
    end
    pcall(ScanPins)   -- 地图可能早已开着
end

function Track.Status()
    return ("地图hook=已挂 已挂pin=" .. tostring(hookedCount))
end
