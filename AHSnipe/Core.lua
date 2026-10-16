-- 插件入口：全局表、SavedVariables 初始化、AH 开/关事件驱动扫描开关、斜杠命令。

local ADDON_NAME, AHSnipe = ...
_G.AHSnipe = AHSnipe

local frame = CreateFrame("Frame", "AHSnipeFrame")
AHSnipe.frame = frame

------------------------------------------------------------------------
-- AH 事件诊断帧：diagAH 开启时记录所有拍卖行相关事件（含未被我们处理的事件），
-- 用于判断"购买请求发出后服务器到底回没回、回了什么"。搜索类事件不记录（噪音太大）。
------------------------------------------------------------------------
local DIAG_EVENTS = {
    "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED",
    "AUCTION_HOUSE_THROTTLED_SYSTEM_READY",
    "COMMODITY_PRICE_UPDATED", "COMMODITY_PRICE_UNAVAILABLE",
    "COMMODITY_PURCHASE_SUCCEEDED", "COMMODITY_PURCHASE_FAILED", "COMMODITY_PURCHASED",
    "ITEM_PURCHASED",
    "AUCTION_HOUSE_ERROR",
}

local function DiagFormat(v)
    if type(v) == "table" then
        if v.itemID then return "item:" .. tostring(v.itemID) end
        return "table"
    end
    return tostring(v)
end

local function InitDiagFrame()
    local f = CreateFrame("Frame")
    -- 12.x 移除了部分旧事件（如 AUCTION_HOUSE_ERROR，2026-10-15 日志），
    -- 逐事件 pcall，避免一个未知事件中断其余诊断事件注册
    for _, ev in ipairs(DIAG_EVENTS) do
        pcall(f.RegisterEvent, f, ev)
    end
    f:SetScript("OnEvent", function(self, event, ...)
        if not AHSnipe.Config.IsDiagAH() then return end
        local parts = {}
        for i = 1, select("#", ...) do parts[#parts + 1] = DiagFormat((select(i, ...))) end
        AHSnipe.Config.Log(("AH事件 %s %s"):format(event, table.concat(parts, " ")))
    end)
    AHSnipe.diagFrame = f
end

-- 安全步骤包装：事件链上任何一步报错都不中断其余步骤，并写入诊断日志。
-- 游戏版本变动导致 API 失效时，未捕获的错误会让后续步骤静默不执行
-- （2026-10-15 实例：AUCTION_HOUSE_SHOW 里一步报错 → 配置面板不出现，表象"看不到界面"）。
local function SafeStep(desc, fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then
        AHSnipe.Config.Log(("步骤失败 %s：%s"):format(desc, tostring(err)))
    end
    return ok
end

local function OnEvent(self, event, ...)
    if event == "ADDON_LOADED" then
        local name = ...
        if name ~= ADDON_NAME then return end

        -- 接口版本检测：仅正式服启用
        if WOW_PROJECT_MAINLINE ~= WOW_PROJECT_ID then
            print("|cffFF4040AHSnipe: 仅支持正式服，已禁用。|r")
            self:UnregisterAllEvents()
            return
        end

        AHSnipe.Config.Init()
        if AHSnipe.SnipeWindow then SafeStep("SnipeWindow.Init", AHSnipe.SnipeWindow.Init) end
        if AHSnipe.ConfigPanel then SafeStep("ConfigPanel.Init", AHSnipe.ConfigPanel.Init) end
        if AHSnipe.BuyQueue then SafeStep("BuyQueue.Init", AHSnipe.BuyQueue.Init) end
        if AHSnipe.Scanner then SafeStep("Scanner.Init", AHSnipe.Scanner.Init) end
        if AHSnipe.LogWindow then SafeStep("LogWindow.Init", AHSnipe.LogWindow.Init) end
        SafeStep("InitDiagFrame", InitDiagFrame)

        print("|cff33FF99AHSnipe|r 已加载，输入 /ahsnipe 打开配置面板。")
    elseif event == "AUCTION_HOUSE_SHOW" then
        if AHSnipe.Scanner then SafeStep("Scanner.Start", AHSnipe.Scanner.Start) end
        -- 打开拍卖行时自动呼出配置面板，便于随时查看价格与修改门槛
        if AHSnipe.ConfigPanel then SafeStep("ConfigPanel.Open", AHSnipe.ConfigPanel.Open) end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        if AHSnipe.Scanner then SafeStep("Scanner.Stop", AHSnipe.Scanner.Stop) end
        if AHSnipe.ConfigPanel then SafeStep("ConfigPanel.Close", AHSnipe.ConfigPanel.Close) end
    elseif event == "PLAYER_LOGOUT" then
        -- SavedVariables 由游戏自动保存 AHSnipeDB
    end
end

frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:SetScript("OnEvent", OnEvent)

-- 斜杠命令：/ahsnipe 打开面板，/ahsnipe scan 手动扫描并重置间隔计时，/ahsnipe log 输出诊断日志，
-- /ahsnipe diag 切换 AH 事件诊断（记录所有拍卖行事件，排查"购买请求无响应"类问题），
-- /ahsnipe bind <按键> 一键购买，/ahsnipe unbind 解除该按键绑定
SLASH_AHSNIPE1 = "/ahsnipe"
SlashCmdList["AHSNIPE"] = function(msg)
    local raw = (msg or ""):trim()
    msg = raw:lower()
    if msg == "scan" then
        if AHSnipe.Scanner and AHSnipe.Scanner.ScanNow() then
            print("|cff33FF99AHSnipe|r: 已立即扫描，自动计时从现在重新开始。")
        else
            print("|cffFF4040AHSnipe|r: 扫描未启用（需先打开拍卖行，或已暂停）。")
        end
    elseif msg == "log" then
        -- 诊断日志窗口（可复制粘贴，不再刷屏聊天框）：最新在底部
        if AHSnipe.LogWindow then
            AHSnipe.LogWindow.Show()
            local n = AHSnipe.DB().logs and #AHSnipe.DB().logs or 0
            print(("|cff33FF99AHSnipe|r: 日志窗口已打开（共 %d 条）——点文本框 Ctrl+A 全选、Ctrl+C 复制。"):format(n))
        end
    elseif msg == "diag" then
        local on = not AHSnipe.Config.IsDiagAH()
        AHSnipe.Config.SetDiagAH(on)
        if on then
            print("|cff33FF99AHSnipe|r: AH 事件诊断已开启（所有拍卖行事件将写入 /ahsnipe log）。")
        else
            print("|cffFF4040AHSnipe|r: AH 事件诊断已关闭。")
        end
    elseif msg == "bind" or msg:sub(1, 5) == "bind " then
        if not AHSnipe.SnipeWindow then
            print("|cffFF4040AHSnipe|r: 模块未加载，无法绑定按键。")
            return
        end
        local Win = AHSnipe.SnipeWindow
        local key = raw:match("^bind%s+(.+)")
        if not key or key == "" then
            local cur = AHSnipe.Config.GetBindKey()
            print(("|cff33FF99AHSnipe|r: 用法 /ahsnipe bind <按键>，如 /ahsnipe bind f（也可用组合键 Shift-1、Ctrl-2）。当前绑定：%s")
                :format(cur or "无"))
            print(("|cff33FF99AHSnipe|r: 也可直接做宏 /click %s 并绑键（宏走战斗锁定允许的安全按钮通道）。"):format(
                Win.KeyButtonName()))
            return
        end
        -- 按键令牌统一大写，与 WoW 绑定表写法一致
        local ok, err = Win.BindKey(key:upper())
        if ok then
            print(("|cff33FF99AHSnipe|r: 已绑定 %s 为狙击弹窗的一键购买（等同点击【全部购买】；战斗中绑定动作需在脱战时设置）。"):format(key:upper()))
        else
            print(("|cffFF4040AHSnipe|r: 绑定失败：%s"):format(tostring(err)))
        end
    elseif msg == "unbind" then
        if AHSnipe.SnipeWindow and AHSnipe.SnipeWindow.UnbindKey() then
            print("|cff33FF99AHSnipe|r: 已解除一键购买按键绑定。")
        else
            print("|cffFF4040AHSnipe|r: 没有可解除的按键绑定。")
        end
    else
        if AHSnipe.ConfigPanel and AHSnipe.ConfigPanel.Toggle then
            AHSnipe.ConfigPanel.Toggle()
        end
    end
end
