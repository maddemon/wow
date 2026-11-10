--[[
    GoldLedger: UI/Settings.lua
    Settings popup: theme, language, minimap toggle, reset session.
    Uses ONLY UI public API — no direct WoW frame creation.
]]

local ADDON_NAME, ns = ...
local GoldLedger = ns.GoldLedger
local L = ns.L

local function UI()      return GoldLedger and GoldLedger:GetModule("UI") end
local function Helpers() return ns.UI_Helpers end
local function TC(key)
    local h = Helpers()
    if h and h.TC then return h.TC(key) end
    return 1, 1, 1, 1
end

local settingsFrame
local settingsElements = {}
local RefreshSettings

local function CreateSettingsFrame()
    if settingsFrame then return end
    local ui = UI()
    if not ui then return end

    local f = ui:CreatePopup({
        name  = "GoldLedgerSettingsFrame",
        title = L and L["HEADER_SETTINGS"] or "Settings",
        width = 320, height = 300,
    })
    f:ClearAllPoints()
    f:SetPoint("CENTER", UIParent, "CENTER", 0, 100)

    -- Minimap label + checkbox (theme selector removed — native WoW look fixed)
    local y = -40
    local minimapLabel = ui:CreateLabel(f, { color = "LABEL" })
    minimapLabel:SetPoint("TOPLEFT", f, "TOPLEFT", 14, y)
    settingsElements.minimapLabel = minimapLabel

    local minimapCheck = ui:CreateCheckbox(f, {
        onChange = function(checked)
            if _G.GoldLedgerDB and _G.GoldLedgerDB.settings then
                _G.GoldLedgerDB.settings.showMinimap = checked
            end
            if ns.Minimap and ns.Minimap.SetVisible then
                ns.Minimap.SetVisible(checked)
            end
        end,
    })
    minimapCheck:SetPoint("LEFT", minimapLabel, "RIGHT", 6, 0)
    settingsElements.minimapCheck = minimapCheck
    f.minimapCheck = minimapCheck  -- expose for tests and external code

    y = y - 36

    -- Language label
    local langLabel = ui:CreateLabel(f, { color = "LABEL" })
    langLabel:SetPoint("TOPLEFT", f, "TOPLEFT", 14, y)
    settingsElements.langLabel = langLabel

    y = y - 20
    settingsElements.langBtns = {}
    local langIds = { "auto", "enUS", "ruRU" }
    for i = 1, 3 do
        local btn = ui:CreateButton(f, {
            width = 90, height = 22,
            onClick = function()
                if _G.GoldLedgerDB and _G.GoldLedgerDB.settings then
                    _G.GoldLedgerDB.settings.language = langIds[i]
                end
                local localeArg = langIds[i] == "auto" and nil or langIds[i]
                if L and L.SetLocale then L:SetLocale(localeArg) end
                C_Timer.After(0, function()
                    -- Notify all listeners (MainFrame + each feature) that the
                    -- locale changed; they tear themselves down so the next
                    -- Toggle re-creates with fresh strings.
                    if GoldLedger and GoldLedger.Events then
                        GoldLedger.Events:Emit("LANGUAGE_CHANGED", langIds[i])
                    end
                    RefreshSettings()
                    local h = Helpers()
                    if h and h.SnapToMain then h.SnapToMain(settingsFrame) end
                    settingsFrame:Show()
                end)
            end,
        })
        btn:SetPoint("TOPLEFT", f, "TOPLEFT", 14 + (i - 1) * 96, y)
        btn.langId = langIds[i]
        settingsElements.langBtns[i] = btn
    end

    y = y - 36

    -- Reset session button
    local resetBtn = ui:CreateButton(f, {
        width = 140, height = 24,
        onClick = function()
            local Tracker = GoldLedger:GetModule("Tracker")
            if Tracker and Tracker.ResetSession then Tracker:ResetSession() end
            -- Refresh visible totals (MainFrame listens internally)
            if GoldLedger and GoldLedger.Events then
                GoldLedger.Events:Emit("SESSION_RESET")
            end
            settingsFrame:Hide()
        end,
    })
    resetBtn:SetPoint("TOPLEFT", f, "TOPLEFT", 14, y)
    settingsElements.resetBtn = resetBtn

    settingsFrame = f
end

RefreshSettings = function()
    if not settingsFrame then CreateSettingsFrame() end
    local f = settingsFrame
    local Themes = GoldLedger:GetModule("Themes")

    if f.SetBackdropColor and f.GetBackdrop and f:GetBackdrop() then
        f:SetBackdropColor(TC("FRAME_BG"))
        f:SetBackdropBorderColor(TC("BORDER"))
    end

    if f.title and L then f.title:SetText(L["HEADER_SETTINGS"]) end

    settingsElements.minimapLabel:SetText(L["SETTINGS_MINIMAP"])
    settingsElements.minimapCheck:SetChecked(
        _G.GoldLedgerDB and _G.GoldLedgerDB.settings
            and _G.GoldLedgerDB.settings.showMinimap ~= false
    )

    settingsElements.langLabel:SetText(L["SETTINGS_LANGUAGE"])

    local currentLang = (_G.GoldLedgerDB and _G.GoldLedgerDB.settings and _G.GoldLedgerDB.settings.language) or "auto"
    local langLabels = { L["SETTINGS_LANG_AUTO"], "English", "Русский" }
    for i, btn in ipairs(settingsElements.langBtns) do
        btn.text:SetText(langLabels[i])
        if btn.langId == currentLang then
            if btn.LockHighlight then btn:LockHighlight() end
            if btn.text and btn.text.SetTextColor then btn.text:SetTextColor(1, 0.82, 0) end
        else
            if btn.UnlockHighlight then btn:UnlockHighlight() end
            if btn.text and btn.text.SetTextColor then btn.text:SetTextColor(1, 1, 1) end
        end
    end

    settingsElements.resetBtn:SetText(L["SETTINGS_RESET_SESSION"])
    if settingsElements.resetBtn.text and settingsElements.resetBtn.text.SetTextColor then
        settingsElements.resetBtn.text:SetTextColor(1, 0.3, 0.3)
    end
end

-------------------------------------------------------------------------------
-- Export
-------------------------------------------------------------------------------
ns.UI_Settings = {
    Create = CreateSettingsFrame,
    Refresh = RefreshSettings,
    GetFrame = function() return settingsFrame end,
}
