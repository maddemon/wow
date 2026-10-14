-- 配置数据层：默认值、物品增删改查、SavedVariables 读写。
-- 数据结构（AHSnipeDB）：
--   interval   = 8         扫描间隔（秒）
--   discount   = 0.65      默认门槛折扣系数（主簇最低单价 × 该系数）
--   items      = { [itemID] = { name, threshold(铜|nil=自动), enabled } }

AHSnipe.Config = {}
local Config = AHSnipe.Config

-- 供其他模块访问 SavedVariables 根表（含 logs/lastPrices）
function AHSnipe.DB()
    return AHSnipeDB
end

-- 写诊断日志（保留最近 200 条），购买链路排查用
function Config.Log(msg)
    if type(AHSnipeDB) ~= "table" then return end
    AHSnipeDB.logs = AHSnipeDB.logs or {}
    local logs = AHSnipeDB.logs
    logs[#logs + 1] = ("%.1f %s"):format(GetTime(), msg)
    if #logs > 200 then
        table.remove(logs, 1)
    end
end

local function DefaultDB()
    return {
        interval = 8,
        discount = 0.65,
        paused = false,     -- 用户手动暂停扫描（持久化，跨会话保持）
        items = {},
        logs = {},
        lastPrices = {},   -- [itemID] = { minUnit, threshold, time } 上次扫描价格快照
    }
end

-- 初始化：加载 SavedVariables 并合并缺失的默认字段。
function Config.Init()
    if type(AHSnipeDB) ~= "table" then
        AHSnipeDB = DefaultDB()
    end
    local db = AHSnipeDB
    if type(db.interval) ~= "number" then db.interval = 8 end
    -- 迁移：旧默认 30 秒在新策略（贴闸门扫描）下偏慢，统一降到 8 秒
    if db.interval == 30 then db.interval = 8 end
    if type(db.discount) ~= "number" or db.discount <= 0 or db.discount >= 1 then
        db.discount = 0.65
    end
    if type(db.items) ~= "table" then db.items = {} end
    if type(db.logs) ~= "table" then db.logs = {} end
    if type(db.lastPrices) ~= "table" then db.lastPrices = {} end
    if type(db.paused) ~= "boolean" then db.paused = false end
    -- 迁移：旧版本曾把"物品+ID"兜底名存入 DB，清空以触发重新异步加载
    -- 旧版本星级用 itemQuality 误存（reagentQuality 字段为 nil），统一清空后由 UI 重新取
    for _, it in pairs(db.items) do
        if it.name and it.name:match("^物品%d+$") then
            it.name = nil
            it.quality = nil
        end
        it.reagentQuality = nil
    end
end

-- 记录/读取上次扫描的价格快照（用于配置面板显示价格列）
function Config.SetLastPrice(itemID, minUnit, threshold)
    AHSnipeDB.lastPrices[itemID] = {
        minUnit = minUnit,
        threshold = threshold,
        time = time(),
    }
end

function Config.GetLastPrice(itemID)
    return AHSnipeDB.lastPrices[itemID]
end

function Config.GetDB()
    return AHSnipeDB
end

function Config.GetInterval()
    return AHSnipeDB.interval
end

function Config.GetDiscount()
    return AHSnipeDB.discount
end

-- 扫描暂停状态（持久化；暂停期间 AH 打开也不扫描）
function Config.IsPaused()
    return AHSnipeDB.paused
end

function Config.SetPaused(paused)
    AHSnipeDB.paused = paused and true or false
end

function Config.SetInterval(seconds)
    if type(seconds) == "number" and seconds >= 5 then
        AHSnipeDB.interval = seconds
    end
end

function Config.SetDiscount(discount)
    if type(discount) == "number" and discount > 0 and discount < 1 then
        AHSnipeDB.discount = discount
    end
end

-- 按物品ID升序返回已配置物品的数组：{ itemID, name, quality, threshold, enabled }
function Config.GetItems()
    local list = {}
    for itemID in pairs(AHSnipeDB.items) do
        list[#list + 1] = itemID
    end
    table.sort(list)
    local result = {}
    for _, itemID in ipairs(list) do
        local it = AHSnipeDB.items[itemID]
        result[#result + 1] = {
            itemID = itemID,
            name = it.name,
            quality = it.quality,
            threshold = it.threshold,
            enabled = it.enabled,
        }
    end
    return result
end

function Config.GetItem(itemID)
    return AHSnipeDB.items[itemID]
end

-- 添加物品（已存在则忽略，返回是否新增）。name 可为 nil，由 UI 层异步加载后经 UpdateItemInfo 补全。
function Config.AddItem(itemID, name)
    if type(itemID) ~= "number" then return false end
    if AHSnipeDB.items[itemID] then return false end
    AHSnipeDB.items[itemID] = { name = name, quality = nil, reagentQuality = nil, threshold = nil, enabled = true }
    return true
end

-- 更新物品显示信息（名称/品质/reagent星级），由 UI 层在数据加载后调用。
-- reagentQuality：制造材料星级 1-3（C_TradeSkillUI.GetItemReagentQualityByItemInfo），非制造材料为 nil。
function Config.UpdateItemInfo(itemID, name, quality, reagentQuality)
    local it = AHSnipeDB.items[itemID]
    if not it then return end
    if name then it.name = name end
    if quality then it.quality = quality end
    if reagentQuality ~= nil then it.reagentQuality = reagentQuality end
end

local QUALITY_COLOR = {  -- 品质颜色（对应 ItemQuality 0-6）
    [0] = "|cff9d9d9d", [1] = "|cffffffff", [2] = "|cff1eff00",
    [3] = "|cff0070dd", [4] = "|cffa335ee", [5] = "|cffff8000", [6] = "|ffe6cc80",
}

-- 品质颜色转义（供任意物品显示使用，不限于已配置物品）
function Config.GetQualityColor(quality)
    return QUALITY_COLOR[quality] or "|cffffffff"
end

-- 构造物品显示名：品质颜色 + 名称 + 星级标识（reagentQuality 1/2/3 对应 ★1/★2/★3）。
-- 星级必须用 C_TradeSkillUI.GetItemReagentQualityByItemInfo（itemQuality 是物品品质，不是材料星级）。
-- 正式服专业材料每个星级是独立 itemID，扫描天然按星级分离，此标识用于人工区分。
function Config.GetItemDisplayName(itemID)
    local it = AHSnipeDB.items[itemID]
    if not it then return "物品" .. itemID end
    local name = it.name or ("物品" .. itemID)
    if it.quality then
        name = (QUALITY_COLOR[it.quality] or "|cffffffff") .. name .. "|r"
    end
    if it.reagentQuality then
        name = name .. " |cffffd200★" .. it.reagentQuality .. "|r"
    end
    return name
end

function Config.RemoveItem(itemID)
    AHSnipeDB.items[itemID] = nil
end

-- 设置单价门槛（单位铜；nil 表示恢复自动计算）。
function Config.SetThreshold(itemID, copper)
    local it = AHSnipeDB.items[itemID]
    if not it then return end
    if copper == nil then
        it.threshold = nil
    elseif type(copper) == "number" and copper > 0 then
        it.threshold = copper
    end
end

function Config.SetEnabled(itemID, enabled)
    local it = AHSnipeDB.items[itemID]
    if it then
        it.enabled = enabled and true or false
    end
end
