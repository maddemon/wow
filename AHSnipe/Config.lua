-- 配置数据层：默认值、物品增删改查、SavedVariables 读写。
-- 数据结构（AHSnipeDB）：
--   interval   = 0         轮间歇（秒）；0=连续轮询
--   discount   = 0.65      门槛折扣系数（主簇最低单价 × 该系数，自动门槛的唯一来源）
--   diagAH     = false     AH 事件诊断开关（/ahsnipe diag 切换）
--   listPos    = nil       可购买列表窗位置（拖动后持久化 {point,x,y}）
--   items      = { [itemID] = { name, enabled } }

AHSnipe.Config = {}
local Config = AHSnipe.Config

-- 供其他模块访问 SavedVariables 根表（含 logs）
function AHSnipe.DB()
    return AHSnipeDB
end

-- 写诊断日志（保留最近 200 条），购买链路排查用。
-- 时间戳用墙上时钟（月-日 时:分:秒），人类可读且能直接判断是不是最新日志；
-- 需要亚秒级间隔时由具体日志行自带"距发送X.Xs"等相对值（GetTime 是客户端运行时长，
-- 绝对值对人类无意义，故不直接输出）。
function Config.Log(msg)
    if type(AHSnipeDB) ~= "table" then return end
    AHSnipeDB.logs = AHSnipeDB.logs or {}
    local logs = AHSnipeDB.logs
    logs[#logs + 1] = ("[%s] %s"):format(date("%m-%d %H:%M:%S"), msg)
    if #logs > 200 then
        table.remove(logs, 1)
    end
end

local function DefaultDB()
    return {
        interval = 0,       -- 轮间歇（秒）；0=连续轮询（节流闸门天然限速）
        discount = 0.65,
        diagAH = false,     -- AH 事件诊断：记录所有拍卖行相关事件（购买链路排查，/ahsnipe diag 切换）
        paused = false,     -- 用户手动暂停扫描（持久化，跨会话保持）
        items = {},
        logs = {},
    }
end

-- 初始化：加载 SavedVariables 并合并缺失的默认字段。
function Config.Init()
    if type(AHSnipeDB) ~= "table" then
        AHSnipeDB = DefaultDB()
    end
    local db = AHSnipeDB
    if type(db.interval) ~= "number" then db.interval = 0 end
    -- 迁移：连续轮询新策略下默认 0（旧默认 30/8 秒统一迁移）
    if db.interval == 30 or db.interval == 8 then db.interval = 0 end
    if type(db.discount) ~= "number" or db.discount <= 0 or db.discount >= 1 then
        db.discount = 0.65
    end
    if type(db.items) ~= "table" then db.items = {} end
    if type(db.logs) ~= "table" then db.logs = {} end
    if type(db.paused) ~= "boolean" then db.paused = false end
    if type(db.diagAH) ~= "boolean" then db.diagAH = false end
    -- 旧弹窗时代字段整体废弃（可购买列表替代，规格 2026-10-16）；含价格快照，配置面板价格列已下架
    db.bindKey = nil
    db.flashAlert = nil
    db.repeatPing = nil
    db.lastPrices = nil
    if type(db.listPos) == "table" and type(db.listPos.point) ~= "string" then
        db.listPos = nil
    end
    -- 迁移：旧版本曾把"物品+ID"兜底名存入 DB，清空以触发重新异步加载
    -- 旧版本星级用 itemQuality 误存（reagentQuality 字段为 nil），统一清空后由 UI 重新取
    -- 手动门槛已废弃（一律用默认折扣系数自动计算），清空旧值避免混淆
    for _, it in pairs(db.items) do
        if it.name and it.name:match("^物品%d+$") then
            it.name = nil
            it.quality = nil
        end
        it.reagentQuality = nil
        it.starTexture = nil
        it.threshold = nil
    end
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

-- AH 事件诊断开关（持久化）：开启后记录所有拍卖行相关事件，
-- 用于排查"购买请求发出后服务器毫无回应"类问题（/ahsnipe diag 切换）
function Config.IsDiagAH()
    return AHSnipeDB.diagAH
end

function Config.SetDiagAH(enabled)
    AHSnipeDB.diagAH = enabled and true or false
end

-- 可购买列表窗位置（拖动结束由 SnipeList 写入；空则创建时用默认锚点）
function Config.GetListPos()
    local p = AHSnipeDB.listPos
    if type(p) == "table" and type(p.point) == "string" then return p end
    return nil
end

function Config.SetListPos(point, x, y)
    AHSnipeDB.listPos = { point = point, x = x, y = y }
end

-- 轮间歇秒数；0=连续轮询（节流闸门天然限速，负数非法）
function Config.SetInterval(seconds)
    if type(seconds) == "number" and seconds >= 0 then
        AHSnipeDB.interval = seconds
    end
end

function Config.SetDiscount(discount)
    if type(discount) == "number" and discount > 0 and discount < 1 then
        AHSnipeDB.discount = discount
    end
end

-- 按物品ID升序返回已配置物品的数组：{ itemID, name, quality, enabled }
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
    AHSnipeDB.items[itemID] = { name = name, quality = nil, reagentQuality = nil, starTexture = nil, threshold = nil, enabled = true }
    return true
end

-- 更新物品显示信息（名称/品质/reagent星级/星级贴图），由 UI 层在数据加载后调用。
-- reagentQuality：制造材料星级 1-3（C_TradeSkillUI.GetItemReagentQualityByItemInfo），非制造材料为 nil。
-- starTexture：系统物品链接里自带的星级贴图名（与聊天/AH 显示同款图标），有则优先于图集探测。
function Config.UpdateItemInfo(itemID, name, quality, reagentQuality, starTexture)
    local it = AHSnipeDB.items[itemID]
    if not it then return end
    if name then it.name = name end
    if quality then it.quality = quality end
    if reagentQuality ~= nil then it.reagentQuality = reagentQuality end
    if starTexture then it.starTexture = starTexture end
end

local QUALITY_COLOR = {  -- 品质颜色（对应 ItemQuality 0-6）
    [0] = "|cff9d9d9d", [1] = "|cffffffff", [2] = "|cff1eff00",
    [3] = "|cff0070dd", [4] = "|cffa335ee", [5] = "|cffff8000", [6] = "|ffe6cc80",
}

-- 品质颜色转义（供任意物品显示使用，不限于已配置物品）
function Config.GetQualityColor(quality)
    return QUALITY_COLOR[quality] or "|cffffffff"
end

-- 推断材料星级：存储值缺失时，按同一名称下 itemID 升序推断 1-2-3 阶。
-- 正式服专业材料每个星级都是独立 itemID，且通常 itemID 按星级递增；
-- 该推断仅用于 UI 兜底显示，不会写入 DB。
function Config.GetEffectiveReagentQuality(itemID)
    local it = AHSnipeDB.items[itemID]
    if not it then return nil end
    -- API 已取到则直接用
    if it.reagentQuality ~= nil then return it.reagentQuality end
    -- 没有名称还无法分组
    if not it.name then return nil end

    local group = {}
    for id, other in pairs(AHSnipeDB.items) do
        if other.name == it.name then
            group[#group + 1] = id
        end
    end
    if #group <= 1 then return nil end

    table.sort(group)
    for tier, id in ipairs(group) do
        if id == itemID then
            -- 星级通常最多 3 阶，超过时按 3 阶截断
            return tier <= 3 and tier or 3
        end
    end
    return nil
end

-- 材料星级标识，按优先级：
-- 0) 系统物品链接自带的星级贴图名（聊天/AH/背包里显示的五角星图标就是它，保真度最高）
-- 1) 官方 API（Professions.GetIconForQuality，存在与否/签名未知，pcall 防炸）
-- 2) 星级图集运行时探测：历史上出现过多种命名（Icon/ChatIcon × -Inv/-Small/无后缀，
--    来源：WA #4116 用 Professions-Icon-Quality-Tier3-Small，战网论坛链接样本含
--    Professions-ChatIcon-Quality-Tier*），逐个探测，命中缓存并写日志便于核对版本差异
-- 3) 全部失效时回退彩色 ♦ 字符
local STAR_ATLAS_TPL = {
    "Professions-Icon-Quality-Tier%d-Inv",
    "Professions-Icon-Quality-Tier%d-Small",
    "Professions-Icon-Quality-Tier%d",
    "Professions-ChatIcon-Quality-Tier%d-Small",
    "Professions-ChatIcon-Quality-Tier%d",
}
local starAtlasHit = {}   -- [tier] = 命中的图集名（会话内缓存，避免重复探测/日志刷屏）

local function GetStarMarkup(quality, texture)
    if not quality or quality < 1 then return "" end
    -- 0) 链接内贴图：与系统渲染完全同款
    if texture then return ("|A:%s:14:14|a"):format(texture) end
    -- 1) 官方 API
    if Professions and Professions.GetIconForQuality then
        local ok, icon = pcall(Professions.GetIconForQuality, quality, true)
        if ok and icon then
            return ("|A:%s:14:14|a"):format(icon)
        end
    end
    -- 2) 图集探测（会话缓存）
    if not starAtlasHit[quality] then
        for _, tpl in ipairs(STAR_ATLAS_TPL) do
            local atlas = tpl:format(quality)
            local okProbe, atlasInfo = pcall(function()
                return C_Texture and C_Texture.GetAtlasInfo and C_Texture.GetAtlasInfo(atlas)
            end)
            if okProbe and atlasInfo then
                local okMk, markup = pcall(CreateAtlasMarkup, atlas, 14, 14)
                if okMk and markup then
                    starAtlasHit[quality] = atlas
                    Config.Log(("星级图集命中：tier%d=%s"):format(quality, atlas))
                    break
                end
            end
        end
    end
    if starAtlasHit[quality] then
        return ("|A:%s:14:14|a"):format(starAtlasHit[quality])
    end
    -- 3) 回退字符：系统语义是"单个图标、颜色区分星级"，故只画 1 个 ♦（银/金对应 1/2 阶）
    local colors = { "|cffb0b0b0", "|cffffd200", "|cffff8c00" }
    local color = colors[quality] or "|cffffd200"
    return color .. "♦|r"
end

-- 构造物品显示名：品质颜色 + 名称 + 星级标识（优先系统图标，不可用时回退 ♦，见 GetStarMarkup）。
-- 星级必须用 C_TradeSkillUI.GetItemReagentQualityByItemInfo（itemQuality 是物品品质，不是材料星级）。
-- 正式服专业材料每个星级是独立 itemID，扫描天然按星级分离，此标识用于人工区分。
function Config.GetItemDisplayName(itemID)
    local it = AHSnipeDB.items[itemID]
    if not it then return "物品" .. itemID end
    local name = it.name or ("物品" .. itemID)
    if it.quality then
        name = (QUALITY_COLOR[it.quality] or "|cffffffff") .. name .. "|r"
    end
    local star = GetStarMarkup(Config.GetEffectiveReagentQuality(itemID),
        it.reagentQuality ~= nil and it.starTexture or nil)
    if star ~= "" then
        name = name .. " " .. star
    end
    return name
end

function Config.RemoveItem(itemID)
    AHSnipeDB.items[itemID] = nil
end

-- 设置启用状态
function Config.SetEnabled(itemID, enabled)
    local it = AHSnipeDB.items[itemID]
    if it then
        it.enabled = enabled and true or false
    end
end
