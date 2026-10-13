-- 价格簇分析模块：纯逻辑，不依赖任何 WoW API，可在游戏外测试。
-- 输入 listing 形如 { unitPrice = 铜, quantity = 数量 }。

local SPLIT_RATIO = 1.5   -- 相邻单价比值达到该值即切分为不同价格簇
local BAIT_MAX_QUANTITY = 10       -- 诱饵判定：挂单数量上限
local BAIT_MAX_PRICE_RATIO = 0.65  -- 诱饵判定：单价不超过主簇最低价的该倍数

AHSnipePriceCluster = {}
local PC = AHSnipePriceCluster

-- 构建价格簇：输入的 listing 数组被复制并按单价升序排序后切分。
-- 返回簇数组（单价升序），每簇 { minPrice, maxPrice, totalQuantity }。
function PC.BuildClusters(listings)
    local sorted = {}
    for i = 1, #listings do
        sorted[i] = listings[i]
    end
    table.sort(sorted, function(a, b) return a.unitPrice < b.unitPrice end)

    local clusters = {}
    local current = nil
    for i = 1, #sorted do
        local l = sorted[i]
        if current and l.unitPrice / current.maxPrice < SPLIT_RATIO then
            -- 与当前簇最高价仍属同一簇
            current.maxPrice = l.unitPrice
            current.totalQuantity = current.totalQuantity + l.quantity
        else
            current = { minPrice = l.unitPrice, maxPrice = l.unitPrice, totalQuantity = l.quantity }
            clusters[#clusters + 1] = current
        end
    end
    return clusters
end

-- 选择主簇：总挂单数量最多的簇；并列时取最低单价更高的簇。
function PC.GetMainCluster(clusters)
    local main = nil
    for i = 1, #clusters do
        local c = clusters[i]
        if not main
            or c.totalQuantity > main.totalQuantity
            or (c.totalQuantity == main.totalQuantity and c.minPrice > main.minPrice) then
            main = c
        end
    end
    return main
end

-- 计算默认门槛：主簇最低单价 × 折扣系数。空挂单返回 nil。
function PC.GetThreshold(listings, discount)
    local main = PC.GetMainCluster(PC.BuildClusters(listings))
    if not main then
        return nil
    end
    return main.minPrice * discount
end

-- 诱饵标注判定：挂单数量少且单价显著低于主簇最低价。
function PC.IsBaitLike(listing, mainMinPrice)
    return listing.quantity <= BAIT_MAX_QUANTITY
        and listing.unitPrice <= mainMinPrice * BAIT_MAX_PRICE_RATIO
end

-- 供游戏外（lupa）与游戏内（AHSnipe 全局表）两种加载方式使用。
if AHSnipe then
    AHSnipe.PriceCluster = AHSnipePriceCluster
end
