local ADDON_NAME, ns = ...
ns.Export = ns.Export or {}

local enUS = {
    EXPORT_BUTTON = "Export",
    EXPORT_TITLE  = "Export Data (CSV)",
    EXPORT_HINT   = "Ctrl+A, then Ctrl+C to copy",
}

local ruRU = {
    EXPORT_BUTTON = "Экспорт",
    EXPORT_TITLE  = "Экспорт данных (CSV)",
    EXPORT_HINT   = "Ctrl+A, затем Ctrl+C для копирования",
}

local L = {}
local function getLocale()
    if ns.L and ns.L.GetLocale then return ns.L:GetLocale() end
    return GetLocale and GetLocale() == "ruRU" and "ruRU" or "enUS"
end

setmetatable(L, { __index = function(_, key)
    if getLocale() == "ruRU" then return ruRU[key] or enUS[key] or key end
    return enUS[key] or key
end })

ns.Export.L = L
