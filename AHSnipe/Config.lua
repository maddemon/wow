-- 配置数据层：默认值、物品增删改查、SavedVariables 读写。
-- 数据结构（AHSnipeDB）：
--   interval   = 30        扫描间隔（秒）
--   discount   = 0.65      默认门槛折扣系数（主簇最低单价 × 该系数）
--   items      = { [itemID] = { name, threshold(铜|nil=自动), enabled } }

AHSnipe.Config = {}
local Config = AHSnipe.Config

local function DefaultDB()
    return {
        interval = 30,
        discount = 0.65,
        items = {},
    }
end

-- 初始化：加载 SavedVariables 并合并缺失的默认字段。
function Config.Init()
    if type(AHSnipeDB) ~= "table" then
        AHSnipeDB = DefaultDB()
    end
    local db = AHSnipeDB
    if type(db.interval) ~= "number" then db.interval = 30 end
    if type(db.discount) ~= "number" or db.discount <= 0 or db.discount >= 1 then
        db.discount = 0.65
    end
    if type(db.items) ~= "table" then db.items = {} end
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

-- 按物品ID升序返回已配置物品的数组：{ itemID, name, threshold, enabled }
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
            threshold = it.threshold,
            enabled = it.enabled,
        }
    end
    return result
end

function Config.GetItem(itemID)
    return AHSnipeDB.items[itemID]
end

-- 添加物品（已存在则忽略，返回是否新增）。
function Config.AddItem(itemID, name)
    if type(itemID) ~= "number" then return false end
    if AHSnipeDB.items[itemID] then return false end
    AHSnipeDB.items[itemID] = { name = name or ("物品" .. itemID), threshold = nil, enabled = true }
    return true
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
