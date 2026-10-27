-- 插件入口：命名空间、/gql 子命令注册表、事件分发。
-- 命名空间在测试里已由 mock 预置，游戏里由 WoW 传入的 vararg 提供。

local ADDON_NAME, NS = ...
GQL = GQL or NS or {}
_G.GQL = GQL
GQL.addonName = ADDON_NAME

if not GQL.commands then GQL.commands = {} end

function GQL.RegisterCommand(name, fn, help)
    GQL.commands[name] = { fn = fn, help = help or "" }
end

local function PrintHelp()
    print("GQL 命令：" .. table.concat((function()
        local out = {}
        for name in pairs(GQL.commands) do out[#out + 1] = name end
        table.sort(out)
        return out
    end)(), " | "))
end

local function OnSlash(text)
    local sub = (text or ""):match("^(%S*)")
    if sub == "" then return PrintHelp() end
    local cmd = GQL.commands[sub]
    if not cmd then return PrintHelp() end
    local okCall, err = pcall(cmd.fn, (text or ""):match("^%S+%s*(.*)$"))
    if not okCall then
        GQL.Config.Log(("/gql %s 出错: %s"):format(sub, tostring(err)))
        print("GQL: /gql " .. sub .. " 出错，详情 /gql log")
    end
end

SLASH_GQL1 = "/gql"
SlashCmdList["GQL"] = OnSlash

GQL.RegisterCommand("config", function()
    if GQL.Options and GQL.Options.Open then GQL.Options.Open()
    else print("GQL: 配置面板未就绪") end
end, "打开设置（地图/关键词配置）")

GQL.frame = CreateFrame("Frame", "GQLFrame", UIParent)
GQL.frame:RegisterEvent("ADDON_LOADED")
GQL.frame:RegisterEvent("PLAYER_LOGIN")
GQL.frame:SetScript("OnEvent", function(self, event, arg1)
    if event == "ADDON_LOADED" then
        if arg1 == ADDON_NAME then
            local okCall, err = pcall(GQL.Config.Init)
            if not okCall then print("GQL 初始化失败: " .. tostring(err)) end
        end
    elseif event == "PLAYER_LOGIN" then
        GQL.frame:RegisterEvent("PLAYER_ENTERING_WORLD")
    elseif event == "PLAYER_ENTERING_WORLD" then
        -- 世界任务池登录后数秒才就绪，交给 Sync 去抖
        if GQL.Sync and GQL.Sync.OnWorldEnter then
            local okCall, err = pcall(GQL.Sync.OnWorldEnter)
            if not okCall then GQL.Config.Log("Sync.OnWorldEnter 出错: " .. tostring(err)) end
        end
        -- 地图按钮：用户实测"打开地图没看到按钮"，根因就是这一步没人调用。
        -- 每次进入世界都补一次（EnsureMapButton 自己幂等），因为 12.x 里
        -- WorldMapFrame 是不是登录时就已建出来还没有实测依据。
        if GQL.Matrix and GQL.Matrix.EnsureMapButton then
            local okCall, err = pcall(GQL.Matrix.EnsureMapButton)
            if not okCall then GQL.Config.Log("地图按钮创建出错: " .. tostring(err)) end
        end
        -- 点 pin 记进账本 / 撤销屏蔽：挂世界地图 pin 的 Shift+左键 hook（不依赖命令行）。
        -- 这里每次进世界都调是故意的（WorldMapFrame 可能登录时还没建出来），
        -- 幂等由 Track.Init 自己保证：hook 全会话只挂一次。
        if GQL.Track and GQL.Track.Init then
            local okCall, err = pcall(GQL.Track.Init)
            if not okCall then GQL.Config.Log("Track.Init 出错: " .. tostring(err)) end
        end
    end
end)
