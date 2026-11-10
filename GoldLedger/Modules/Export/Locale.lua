local ADDON_NAME, ns = ...
ns.Export = ns.Export or {}

local enUS = {
    EXPORT_BUTTON = "Export",
    EXPORT_TITLE  = "Export Data (CSV)",
    EXPORT_HINT   = "Ctrl+A, then Ctrl+C to copy",
}

local zhCN = {
    EXPORT_BUTTON = "导出",
    EXPORT_TITLE  = "导出数据（CSV）",
    EXPORT_HINT   = "Ctrl+A 全选，再 Ctrl+C 复制",
}

local L = {}
local function getLocale()
    if ns.L and ns.L.GetLocale then return ns.L:GetLocale() end
    return GetLocale and GetLocale() == "zhCN" and "zhCN" or "enUS"
end

setmetatable(L, { __index = function(_, key)
    if getLocale() == "zhCN" then return zhCN[key] or enUS[key] or key end
    return enUS[key] or key
end })

ns.Export.L = L
