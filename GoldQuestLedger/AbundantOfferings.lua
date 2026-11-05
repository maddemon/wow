-- 丰饶供品（Abundant Offerings，questID 89507）单文件包办 —— 镜像 AqirInvasion.lua，12.x 版本闸。
--
-- 与亚基入侵同属"按 M 看到的事件 pin 才认得到在哪办"的事件型周常：它不上世界任务板，
-- 池/日志/板上都问不到本体（89507 是真实任务，但客户端不把它当世界任务答），分组跟着事件
-- pin 走（事件在哪张图就归哪张图，轮换时跟着换）。历史上丰饶还记了一批同名子任务 id
-- （板上"丰饶：草药洞穴"那种），那套"一族一行、子任务只当证据"的机制在 Sync.ScanBoards /
-- Ledger 的族逻辑里，是通用的，不在这文件。
--
-- 本文件只持有丰饶"这一族"专属的两件事：
--   ① 版本闸：12.x 才有丰饶供品，13.0 起客户端没有这个任务 ⇒ 不挂，换 CD 自然到期下板；
--   ② 续命：每轮快照回问客户端 89507 的真实状态（open/progress/done/unknown），答到才 seen
--      续，答不到就 fail-closed 不续，下一轮再试。与亚基入侵靠 pin 报在场同源，无"常驻"特判。
--
-- 注册（族本体 89507 落账）与分组（事件 pin 选哪张图、轮换时跟着换）走通用的
-- WEEKLY_PRESETS + RefreshEventPins：那条预设住在 Api.WEEKLY_PRESETS（共享的"家族登记表"，
-- 和加固符文石并列，不挪到本文件以避免破坏依赖它的 140+ 用例）；事件归属的轮换修复在
-- Sync.RefreshEventPins 里，对所有事件 pin 通用（按 pin 自身的剩余时间判"一轮"，而非每周锚点）。

GQL.AbundantOfferings = {}
local M = GQL.AbundantOfferings

local QUEST_ID = 89507
local REG_KEY = "abundant-offerings"
local PIN_WORD = "丰饶"

-- 版本闸：12.x 才有丰饶供品；13.0 起客户端没有这个任务 ⇒ 模块不挂、丰饶不再续命、
-- 换 CD 后下板（与"13.0 没有丰饶"的口径一致）。主版本号随 GetBuildInfo 取，取不到一律不挂。
-- 取不到（离线 / 接口缺失）宁可不挂，也不许把"问不出版本"当"有丰饶"硬挂。
local function Active()
    local okCall, v = pcall(GetBuildInfo or function() end)
    if not (okCall and type(v) == "string" and v ~= "") then return false end
    local major = tonumber(tostring(v):match("^(%d+)"))
    return major == 12
end

--- 每轮快照扩展点（GQL.SnapshotExtras）：这轮客户端回问到 89507 的真实状态 ⇒ seen 续命；
--- 问不到（接口这轮没答 / 任务不存在）⇒ 不续，维持现状（fail-closed），下一轮再试。
--- 走 EnsureRegistered 的 seen 分支，与亚基入侵 / DiffPool 同源，无"常驻"特判。
function M.Refresh(db, now)
    if not Active() then return end
    local Ledger = GQL.Ledger
    if not (Ledger and Ledger.EnsureRegistered) then return end
    local state = GQL.Api and GQL.Api.QuestState and GQL.Api.QuestState(QUEST_ID)
    -- 只有客户端回得出明确状态才算"本轮回问到"，unknown / nil 不续（问不出 = 不成立）。
    if not state or state == "unknown" then return end
    Ledger.EnsureRegistered(db, {
        { key = REG_KEY, questID = QUEST_ID, seen = true },
    }, GQL.Sync and GQL.Sync.CurrentCharKey and GQL.Sync.CurrentCharKey(), now)
end

GQL.SnapshotExtras = GQL.SnapshotExtras or {}
GQL.SnapshotExtras[#GQL.SnapshotExtras + 1] = M.Refresh
