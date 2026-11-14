-- MawBuffGuard
--
-- 暴雪 12.1.x 的 ShouldShowMawBuffs() 在任务追踪器排版链里裸读玩家光环：
--
--   LayoutContents() -> ShouldShowMawBuffs() -> C_UnitAuras.GetAuraDataByIndex("player", 1, "MAW")
--
-- 一旦执行上下文被插件污染（12.x 的 secret aura 机制），这次读取会抛
--   GetAuraDataByIndex(): Auras cannot be accessed when secret while tainted by '...'
-- 而 LayoutContents() 一带没有 pcall，抛错直接中断整轮排版：任务进度不再刷新，
-- 必须 /reload 才恢复。见 https://github.com/Stanzilla/WoWUIBugs/issues/882
--
-- 这里把这个函数换成本地安全版：秘文期间不读光环，其余情况读取失败也当作"没有"。
-- 返回值只决定一个 Shadowlands 的 UI 块要不要显示，吞掉错误没有副作用。

local function SafeShouldShowMawBuffs()
	if IsInJailersTower and IsInJailersTower() then
		return true
	end

	local ok, show = pcall(function()
		-- 秘文期间任何光环读取都会被拒，直接当作"没有"
		if C_Secrets and C_Secrets.ShouldAurasBeSecret and C_Secrets.ShouldAurasBeSecret() then
			return false
		end

		local auraData = C_UnitAuras.GetAuraDataByIndex("player", 1, "MAW")
		return (auraData and auraData.icon) and true or false
	end)

	if not ok then
		return false
	end
	return show == true
end

local function Apply()
	ShouldShowMawBuffs = SafeShouldShowMawBuffs
end

Apply()

-- 暴雪的 Blizzard_MawBuffs 如果后加载，会把同名全局覆盖回去，这里抢回来
local frame = CreateFrame("Frame")
frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("PLAYER_ENTERING_WORLD")
frame:SetScript("OnEvent", function(_, event, addonName)
	if event == "ADDON_LOADED" then
		if addonName == "Blizzard_MawBuffs" or addonName == "Blizzard_ObjectiveTracker" then
			Apply()
		end
	else
		-- 换地图/登录后也再确认一次，防止懒加载把它覆盖掉
		Apply()
	end
end)

SLASH_MAWBUFFGUARD1 = "/mawbuff"
SlashCmdList["MAWBUFFGUARD"] = function()
	local replaced = ShouldShowMawBuffs == SafeShouldShowMawBuffs
	print(("|cff33ff99MawBuffGuard|r: %s"):format(replaced
		and "已接管 ShouldShowMawBuffs，追踪器不会再因 #882 卡住。"
		or "未接管（被其他插件覆盖），追踪器仍可能卡住。"))

	if C_Secrets and C_Secrets.ShouldAurasBeSecret then
		local ok, secret = pcall(C_Secrets.ShouldAurasBeSecret)
		print(("|cff33ff99MawBuffGuard|r: 当前 ShouldAurasBeSecret = %s"):format(
			ok and tostring(secret) or "读取失败"))
	end
end
