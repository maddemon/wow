-- 插件入口：全局表、SavedVariables 初始化、AH 开/关事件驱动扫描开关、斜杠命令。

local ADDON_NAME, AHSnipe = ...
_G.AHSnipe = AHSnipe

local frame = CreateFrame("Frame", "AHSnipeFrame")
AHSnipe.frame = frame

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
        if AHSnipe.SnipeWindow then AHSnipe.SnipeWindow.Init() end
        if AHSnipe.ConfigPanel then AHSnipe.ConfigPanel.Init() end
        if AHSnipe.BuyQueue then AHSnipe.BuyQueue.Init() end
        if AHSnipe.Scanner then AHSnipe.Scanner.Init() end

        print("|cff33FF99AHSnipe|r 已加载，输入 /ahsnipe 打开配置面板。")
    elseif event == "AUCTION_HOUSE_SHOW" then
        if AHSnipe.Scanner then AHSnipe.Scanner.Start() end
        -- 打开拍卖行时自动呼出配置面板，便于随时查看价格与修改门槛
        if AHSnipe.ConfigPanel then AHSnipe.ConfigPanel.Open() end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        if AHSnipe.Scanner then AHSnipe.Scanner.Stop() end
        if AHSnipe.ConfigPanel then AHSnipe.ConfigPanel.Close() end
    elseif event == "PLAYER_LOGOUT" then
        -- SavedVariables 由游戏自动保存 AHSnipeDB
    end
end

frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:SetScript("OnEvent", OnEvent)

-- 斜杠命令：/ahsnipe 打开面板，/ahsnipe scan 手动扫描一轮，/ahsnipe log 输出诊断日志
SLASH_AHSNIPE1 = "/ahsnipe"
SlashCmdList["AHSNIPE"] = function(msg)
    msg = (msg or ""):trim():lower()
    if msg == "scan" then
        if AHSnipe.Scanner and AHSnipe.Scanner.IsEnabled() then
            AHSnipe.Scanner.ScanRound()
            print("|cff33FF99AHSnipe|r: 手动扫描已触发。")
        else
            print("|cffFF4040AHSnipe|r: 扫描未启用（需先打开拍卖行）。")
        end
    elseif msg == "log" then
        -- 输出诊断日志（购买链路排查用），可全选聊天框内容反馈
        local logs = AHSnipe.DB().logs
        if not logs or #logs == 0 then
            print("|cff33FF99AHSnipe|r: 暂无日志。")
        else
            print("|cff33FF99AHSnipe|r 诊断日志（共 " .. #logs .. " 条，可全选复制反馈）：")
            for _, line in ipairs(logs) do
                print(line)
            end
        end
    else
        if AHSnipe.ConfigPanel and AHSnipe.ConfigPanel.Toggle then
            AHSnipe.ConfigPanel.Toggle()
        end
    end
end
