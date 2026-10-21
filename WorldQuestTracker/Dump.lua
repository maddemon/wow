-- 阶段一只读导出：矩阵 UI 属阶段二，本阶段用文本确认账本记录的是真实情况。

WQT.Dump = {}
local Dump = WQT.Dump

local STATUS_TEXT = {
    done = "已交", progress = "在办", open = "待做",
    ended = "本轮结束", unknown = "?",
}
local STALE_SECONDS = 7 * 86400

function Dump.Run()
    local db = WQT.Config.DB()
    local L = WQT.Ledger
    -- now 必须跟入库时间戳同域，否则"过期 7 天"这类判断跨开机就失效
    local now = (WQT.Api and WQT.Api.WallClock()) or math.floor((WQT.Api and WQT.Api.Uptime()) or 0)
    local domain = db.clockDomain or "uptime"
    local lines = { ("WQT 账本导出 cycleAnchor=%s now=%d 时钟域=%s%s")
        :format(tostring(db.cycleAnchor), now, domain,
            domain == "epoch" and "" or "（仅本次开机内可比，周统计不可靠）") }

    local chars = L.Characters(db)

    lines[#lines + 1] = "== 本周汇总 =="
    if #chars == 0 then lines[#lines + 1] = "（还没有任何角色同步过）" end
    for i = 1, #chars do
        local charKey = chars[i]
        local count, gold = L.WeekStats(db, charKey, db.cycleAnchor or 0)
        local lastSync = db.chars[charKey].lastSync or 0
        local stale = (now - lastSync) > STALE_SECONDS and "（快照过期）" or ""
        -- -1 = 本周统计不可信（时钟域分裂），显示 ? 而不是 0
        lines[#lines + 1] = ("%s  完成 %s  金币 %s  同步于 %d%s"):format(
            charKey, count < 0 and "?" or tostring(count),
            WQT.Config.FormatGold(gold < 0 and nil or gold), lastSync, stale)
    end

    lines[#lines + 1] = "== 金币任务 =="
    local quests = L.GoldQuestIDs(db)
    if #quests == 0 then
        lines[#lines + 1] = "（无金币任务：奖励接口可能不可用，见下方未判定计数）"
    end
    for i = 1, #quests do
        local questID = quests[i]
        local cells = {}
        for c = 1, #chars do
            local st = L.State(db, chars[c], questID)
            cells[#cells + 1] = STATUS_TEXT[st] or tostring(st)
        end
        lines[#lines + 1] = ("%s%s [%s]"):format(
            L.GetQuestName(db, questID) or tostring(questID),
            L.IsHighFreq(db, questID) and "（高频）" or "",
            table.concat(cells, " | "))
    end

    local undetermined, unknownCells = 0, 0
    for questID in pairs(db.quests) do
        local obs = L.GetObs(db, questID)
        if obs and not obs.gold and obs.money == nil then undetermined = undetermined + 1 end
    end
    for _, charKey in ipairs(chars) do
        for questID in pairs(db.quests) do
            if L.State(db, charKey, questID) == "unknown" then unknownCells = unknownCells + 1 end
        end
    end
    lines[#lines + 1] = ("== 降级与未知 == 未判定=%d 未知格=%d 最近降级=%s")
        :format(undetermined, unknownCells, tostring(WQT.Api.LastDegrade()))

    local text = table.concat(lines, "\n")
    WQT.Config.Log("dump 完成")
    if WQT.LogWindow and WQT.LogWindow.Show then WQT.LogWindow.Show(text) end
    print(text)
    return text
end

WQT.RegisterCommand("dump", Dump.Run, "文本导出账本（阶段一验收用）")
