--[[
    GoldLedger: Core.lua
    Patterns: Module, Observer (Event Bus), Singleton

    Ядро аддона:
    - Module system для регистрации и доступа к модулям
    - Event bus с dispatch-таблицей (Observer pattern)
    - Slash-команды /gl и /goldledger
    - Инициализация при ADDON_LOADED
]]

local ADDON_NAME, ns = ...
local L = ns.L

-------------------------------------------------------------------------------
-- Singleton: глобальный namespace аддона
-------------------------------------------------------------------------------
local GoldLedger = {}
GoldLedger.name = ADDON_NAME
GoldLedger.version = C_AddOns.GetAddOnMetadata(ADDON_NAME, "Version") or "1.0.0"

-- Export to global and namespace
_G.GoldLedger = GoldLedger
ns.GoldLedger = GoldLedger

-------------------------------------------------------------------------------
-- Module Pattern: регистрация и доступ к модулям
-------------------------------------------------------------------------------
local modules = {}

--- Регистрирует модуль в системе
--- @param name string Имя модуля
--- @param module table Таблица модуля
function GoldLedger:RegisterModule(name, module)
    if modules[name] then
        error(("GoldLedger: Module '%s' already registered"):format(name))
    end
    modules[name] = module
    module.name = name
    return module
end

--- Возвращает зарегистрированный модуль
--- @param name string Имя модуля
--- @return table|nil
function GoldLedger:GetModule(name)
    return modules[name]
end

--- Вызывает метод на всех модулях (если метод существует)
--- @param method string Имя метода
--- @param ... any Аргументы
function GoldLedger:CallModules(method, ...)
    for _, mod in pairs(modules) do
        if type(mod[method]) == "function" then
            mod[method](mod, ...)
        end
    end
end

-------------------------------------------------------------------------------
-- Observer Pattern: Event Bus с dispatch-таблицей
-------------------------------------------------------------------------------
local eventFrame = CreateFrame("Frame")
local eventHandlers = {} -- { [event] = { callback1, callback2, ... } }

--- Регистрирует обработчик WoW-ивента
--- @param event string WoW event name
--- @param callback function Обработчик
function GoldLedger:RegisterEvent(event, callback)
    if not eventHandlers[event] then
        eventHandlers[event] = {}
        eventFrame:RegisterEvent(event)
    end
    table.insert(eventHandlers[event], callback)
end

--- Снимает все обработчики ивента
--- @param event string WoW event name
function GoldLedger:UnregisterEvent(event)
    eventHandlers[event] = nil
    eventFrame:UnregisterEvent(event)
end

-- Центральный dispatch: один OnEvent для всех ивентов
eventFrame:SetScript("OnEvent", function(_, event, ...)
    local handlers = eventHandlers[event]
    if handlers then
        for _, callback in ipairs(handlers) do
            callback(event, ...)
        end
    end
end)

-------------------------------------------------------------------------------
-- Internal Event Bus: GoldLedger.Events:On/Off/Emit for cross-module messages
-------------------------------------------------------------------------------
local busSubscribers = {}  -- { [event_name] = { fn1, fn2, ... } }

GoldLedger.Events = {}

function GoldLedger.Events:On(name, handler)
    busSubscribers[name] = busSubscribers[name] or {}
    table.insert(busSubscribers[name], handler)
    return handler
end

function GoldLedger.Events:Off(name, handler)
    local list = busSubscribers[name]
    if not list then return end
    for i, fn in ipairs(list) do
        if fn == handler then table.remove(list, i) return end
    end
end

--- Public wrapper: register a handler for a WoW-native event (PLAYER_MONEY etc.)
--- Equivalent to GoldLedger:RegisterEvent but on the Events namespace for discoverability.
function GoldLedger.Events:RegisterWoWEvent(event, handler)
    return GoldLedger:RegisterEvent(event, handler)
end

function GoldLedger.Events:Emit(name, data)
    local list = busSubscribers[name]
    if not list then return end
    for _, fn in ipairs(list) do
        local ok, err = pcall(fn, data)
        if not ok then
            GoldLedger:Debug("Events", "subscriber error on", name, "→", tostring(err))
        end
    end
end

-------------------------------------------------------------------------------
-- CreateLocale: helper to build a localized table for feature modules
-- Usage: local L = GoldLedger:CreateLocale({ enUS = {...}, zhCN = {...} })
-- Reads current language from core Locale module (ns.L:GetLocale()).
-------------------------------------------------------------------------------
function GoldLedger:CreateLocale(tables)
    local L = {}
    local enUS = tables.enUS or {}
    local zhCN = tables.zhCN or enUS
    local function getLocale()
        local coreL = ns.L
        if coreL and coreL.GetLocale then return coreL:GetLocale() end
        return GetLocale and GetLocale() == "zhCN" and "zhCN" or "enUS"
    end
    setmetatable(L, { __index = function(_, key)
        if getLocale() == "zhCN" then
            return zhCN[key] or enUS[key] or key
        end
        return enUS[key] or key
    end })
    return L
end

-------------------------------------------------------------------------------
-- Initialization: ADDON_LOADED
-------------------------------------------------------------------------------
GoldLedger:RegisterEvent("ADDON_LOADED", function(event, loadedAddon)
    if loadedAddon ~= ADDON_NAME then return end

    -- Инициализация SavedVariables с defaults
    GoldLedger:CallModules("OnInitialize")
    -- Feature modules init (loaded after core via .toc order)
    GoldLedger:CallFeatures("OnInitialize")

    -- Приветственное сообщение (отложено чтобы избежать taint)
    C_Timer.After(0, function()
        print(L["ADDON_LOADED"])
    end)

    -- Больше не нужен этот ивент
    GoldLedger:UnregisterEvent("ADDON_LOADED")
end)

-- PLAYER_LOGIN: модули могут подключиться к игровым данным
GoldLedger:RegisterEvent("PLAYER_LOGIN", function()
    -- Auto-language needs GetRealmName(), which is nil until PLAYER_LOGIN.
    local saved = GoldLedgerDB and GoldLedgerDB.settings and GoldLedgerDB.settings.language
    if saved == nil or saved == "auto" then
        L:SetLocale(nil)
    end
    GoldLedger:CallModules("OnEnable")
    GoldLedger:CallFeatures("OnEnable")
    GoldLedger:UnregisterEvent("PLAYER_LOGIN")
end)

-------------------------------------------------------------------------------
-- Feature Module System (for addon features like Auction, History, etc.)
-------------------------------------------------------------------------------
local features = {}
local featureSlashCommands = {}
local headerButtons = {}

--- Регистрирует feature-модуль (для фич, не инфраструктуры)
--- @param name string Уникальное имя фичи (e.g. "auction")
--- @param module table Таблица модуля с OnInitialize/OnEnable callbacks
function GoldLedger:RegisterFeature(name, module)
    features[name] = module
    module.name = name
    return module
end

--- Возвращает feature-модуль по имени
function GoldLedger:GetFeature(name)
    return features[name]
end

--- Вызывает метод на всех feature-модулях
function GoldLedger:CallFeatures(method, ...)
    for _, mod in pairs(features) do
        if type(mod[method]) == "function" then
            mod[method](mod, ...)
        end
    end
end

--- Регистрирует slash-подкоманду /gl <subcommand> для feature-модуля
function GoldLedger:RegisterSlashCommand(subcommand, callback)
    featureSlashCommands[subcommand:lower()] = callback
end

--- Регистрирует кнопку в header bar главного окна
--- @param name string ID кнопки
--- @param label string|function Текст кнопки или функция возвращающая текст (для динамической локализации)
--- @param callback function OnClick handler
function GoldLedger:RegisterHeaderButton(name, label, callback)
    headerButtons[#headerButtons + 1] = { name = name, label = label, callback = callback }
end

--- Снимает регистрацию header-кнопки по имени.
--- @return boolean true если кнопка найдена и удалена
function GoldLedger:UnregisterHeaderButton(name)
    for i, btn in ipairs(headerButtons) do
        if btn.name == name then
            table.remove(headerButtons, i)
            return true
        end
    end
    return false
end

--- Снимает регистрацию slash-подкоманды.
function GoldLedger:UnregisterSlashCommand(subcommand)
    if subcommand then
        featureSlashCommands[subcommand:lower()] = nil
    end
end

--- Снимает регистрацию feature-модуля + чистит его header buttons и slash commands.
--- Используется при runtime-выключении фичи (без перезагрузки UI).
function GoldLedger:UnregisterFeature(name)
    if not features[name] then return false end
    features[name] = nil
    -- Header buttons часто регистрируются с тем же ключом что и feature
    self:UnregisterHeaderButton(name)
    return true
end

--- Возвращает зарегистрированные header buttons (для UI/MainFrame)
function GoldLedger:GetHeaderButtons()
    return headerButtons
end

-------------------------------------------------------------------------------
-- Debug mode flag (declared BEFORE slash handler so `not debugMode` resolves
-- to this local, not a stray global. Setter/printers live further down.)
-------------------------------------------------------------------------------
local debugMode = false

-------------------------------------------------------------------------------
-- Slash Commands
-------------------------------------------------------------------------------
SLASH_GOLDLEDGER1 = "/gl"
SLASH_GOLDLEDGER2 = "/goldledger"

--- Ищет feature slash callback по cmd. Поддерживает два варианта:
---   1. Точное совпадение  ("calc" → featureSlashCommands["calc"])
---   2. Префикс с пробелом  ("goal 1000" → featureSlashCommands["goal"], arg = "1000")
--- Возвращает (callback, arg_string_or_nil) либо nil.
local function findFeatureSlash(cmd)
    if featureSlashCommands[cmd] then
        return featureSlashCommands[cmd], nil
    end
    -- Search registered keys for "key " prefix; longest match wins.
    local matchedKey, matchedFn
    for key, fn in pairs(featureSlashCommands) do
        if cmd:sub(1, #key + 1) == (key .. " ") then
            if not matchedKey or #key > #matchedKey then
                matchedKey, matchedFn = key, fn
            end
        end
    end
    if matchedFn then
        return matchedFn, strtrim(cmd:sub(#matchedKey + 1))
    end
    return nil, nil
end

SlashCmdList["GOLDLEDGER"] = function(msg)
    local cmd = strtrim(msg):lower()

    if cmd == "reset" then
        local Data = GoldLedger:GetModule("Data")
        if Data then
            Data:ResetCharacterData()
            print("|cff00ff00GoldLedger:|r " .. L["RESET_CONFIRM"])
        end
    elseif cmd == "debug" then
        GoldLedger:SetDebug(not debugMode)
    elseif cmd == "dump" then
        local Data = GoldLedger:GetModule("Data")
        if Data then
            GoldLedger:DumpTable("Daily", Data:GetDailySummary())
            GoldLedger:DumpTable("Monthly", Data:GetMonthlySummary())
            GoldLedger:DumpTable("Settings", Data:GetSettings())
            print("|cff888888[GL:Dump]|r Entries: " .. #Data:GetRecentEntries(999))
        end
    elseif cmd == "settings" or cmd == "config" then
        local UI = GoldLedger:GetModule("UI")
        if UI then UI:ToggleSettingsFrame() end
    elseif cmd == "help" then
        print("|cff00ff00GoldLedger:|r " .. L["SLASH_HELP"])
    else
        local fn, arg = findFeatureSlash(cmd)
        if fn then
            -- Built-in slash commands take precedence over feature dispatch
            -- so "reset"/"help" etc. cannot be hijacked by a feature.
            fn(arg)
        else
            -- Default: toggle main window
            local UI = GoldLedger:GetModule("UI")
            if UI then
                UI:ToggleMainFrame()
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Debug System (debugMode local is declared above the slash handler)
-------------------------------------------------------------------------------

--- Включает/выключает debug-режим
function GoldLedger:SetDebug(enabled)
    debugMode = enabled
    print("|cff00ff00GoldLedger:|r debug " .. (enabled and "|cff00ff00ON|r" or "|cffff4444OFF|r"))
end

--- Выводит debug-сообщение в чат (только если debug включён)
--- @param module string Имя модуля
--- @param ... any Аргументы для конкатенации
function GoldLedger:Debug(module, ...)
    if not debugMode then return end

    local args = {...}
    local parts = {}
    for i = 1, #args do
        parts[i] = tostring(args[i])
    end

    print(("|cff888888[GL:%s]|r %s"):format(module, table.concat(parts, " ")))
end

--- Дамп таблицы в чат (debug-режим)
--- @param label string Название
--- @param tbl table Таблица для вывода
function GoldLedger:DumpTable(label, tbl)
    if not debugMode then return end

    print("|cff888888[GL:Dump]|r " .. label .. ":")
    if type(tbl) ~= "table" then
        print("  " .. tostring(tbl))
        return
    end
    for k, v in pairs(tbl) do
        if type(v) == "table" then
            print(("  %s = {%d items}"):format(tostring(k), #v > 0 and #v or 0))
        else
            print(("  %s = %s"):format(tostring(k), tostring(v)))
        end
    end
end

-------------------------------------------------------------------------------
-- LANGUAGE_CHANGED: tear down all module/feature frames so the next Toggle
-- recreates them with fresh localized strings. Generic — iterates the ns
-- namespace and looks for the convention <ns.X.Frame>.SetFrame; new feature
-- modules get this for free without touching core. The main window is the
-- exception: if it was open, it is recreated in place and shown again.
-------------------------------------------------------------------------------
GoldLedger.Events:On("LANGUAGE_CHANGED", function()
    local function teardown(target)
        if not target or type(target.SetFrame) ~= "function" then return end
        local f = type(target.GetFrame) == "function" and target.GetFrame() or nil
        if f and type(f.Hide) == "function" then f:Hide() end
        target.SetFrame(nil)
    end
    -- Главное окно, если было открыто, пересоздаём на том же месте и
    -- показываем снова — смена языка не должна его закрывать.
    local MF = ns.UI_MainFrame
    local mf = MF and type(MF.GetFrame) == "function" and MF.GetFrame() or nil
    local reopenAt
    if mf and type(mf.IsShown) == "function" and mf:IsShown() then
        reopenAt = { left = mf:GetLeft(), top = mf:GetTop() }
    end

    for _, t in pairs(ns) do
        if type(t) == "table" then teardown(t.Frame) end
    end
    teardown(MF)

    if reopenAt and type(MF.Create) == "function" then
        MF.Create()
        local newMf = MF.GetFrame()
        if newMf then
            if reopenAt.left and reopenAt.top then
                newMf:ClearAllPoints()
                newMf:SetPoint("TOPLEFT", UIParent, "BOTTOMLEFT", reopenAt.left, reopenAt.top)
            end
            newMf:Show()
            if type(MF.UpdateSummaries) == "function" then MF.UpdateSummaries() end
        end
    end
end)

-------------------------------------------------------------------------------
-- Utility: передаём L в namespace для удобства
-------------------------------------------------------------------------------
GoldLedger.L = L
