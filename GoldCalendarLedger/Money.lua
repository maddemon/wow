-- 记账层（T1）：全插件唯一一处把 PLAYER_MONEY 变成"一笔带来源的流水"的地方。
-- 做法逐条照计划第 1 节（GoldLedger 在用户 12.1.5 真机上跑通的口径），代码自己写：
-- 那个包没 LICENSE，路线 B 只借知识、不借代码，所以这里的命名、结构、判据顺序都是本仓库的。
-- 每一条"看着多余"的守卫背后都有一个实测过的理由，删之前先读注释。

local GCL = _G.GCL
local Money = {}

if GCL and GCL.RegisterModule then
    GCL.RegisterModule("Money", Money)
else
    GCL = GCL or {}
    GCL.Money = Money
end

---------------------------------------------------------------------------
-- 常量（改动要连同用例一起改）
---------------------------------------------------------------------------

local TRANSFER_TTL_SECONDS = 30  -- 搬钱队列的寿命
local TRANSFER_QUEUE_LIMIT = 10  -- 队列上限
local MAIL_SNAPSHOT_LIMIT  = 10  -- 邮件快照上限
local RECENT_PROBE_KEEP    = 5   -- /gcl probe 报最近几笔

-- 搬钱方向的写法（内部口径，不进存档）
local MONEY_IN, MONEY_OUT = "in", "out"

---------------------------------------------------------------------------
-- 状态（全在内存；落盘是 T2 账本的事）
---------------------------------------------------------------------------

local baseline          -- 上一次读到的金币总额（铜），nil = 还没取到基准
local trackingOn   = false
local context      = "unknown"   -- 界面上下文，由各 *_SHOW / *_CLOSED 事件写
local repairPending = false      -- RepairAllItems() 刚被调过
local mailSnaps   = {}           -- FIFO：TakeInboxMoney 当场抓的 {sender, subject, money}
local transfers   = {}           -- 排队等 PLAYER_MONEY 的搬钱请求 {amount, dir, from, at}
local session     = { income = 0, expense = 0 }
local recent      = {}           -- 最近捕获的几笔，只给 /gcl probe 取证用
local counters    = { droppedWithoutLedger = 0, hookFailures = 0 }
local installed   = false

---------------------------------------------------------------------------
-- 来源清单（8 类，计划 §6 定稿；transfer 不是一类来源，是"这笔不算收支"的标记）
---------------------------------------------------------------------------

local SOURCE_UNKNOWN = "unknown"

-- 界面开着 ⇒ 钱大概率来自这里。QUEST_TURNED_IN 没有对应的 *_CLOSED，
-- 所以它的上下文只能"一笔就清"（见 RecordChange），否则会一直误挂到下次开窗。
local CONTEXT_SET = {
    MERCHANT_SHOW      = "vendor",
    AUCTION_HOUSE_SHOW = "ah",
    MAIL_SHOW          = "mail",
    TRADE_SHOW         = "trade",
    LOOT_OPENED        = "loot",
    QUEST_TURNED_IN    = "quest",
}

local CONTEXT_CLEAR = {
    MERCHANT_CLOSED      = true,
    AUCTION_HOUSE_CLOSED = true,
    MAIL_CLOSED          = true,
    TRADE_CLOSED         = true,
    LOOT_CLOSED          = true,
}

---------------------------------------------------------------------------
-- 小工具：所有客户端接口一律"先问有没有、再 pcall"
---------------------------------------------------------------------------

--- 12.x 的秘密值（secret value）：这种数字不许插件拿去做比较，
--- 而且 issecretvalue 本身在部分客户端上压根不存在 ⇒ 判存在 + pcall，
--- 取不到就当"不是秘密值"。秘密值绝不能当 0 用（当 0 会凭空造出一笔收支）。
local function IsSecretAmount(v)
    local fn = _G.issecretvalue
    if type(fn) ~= "function" then return false end
    local okCall, res = pcall(fn, v)
    return (okCall and res == true) == true
end

local function Uptime()
    local fn = _G.GetTime
    if type(fn) ~= "function" then return nil end
    local okCall, t = pcall(fn)
    if not okCall or type(t) ~= "number" then return nil end
    return t
end

--- 金币总额：nil 或非数一律当"这一读问不出来"，不做 or 0 的兜底
--- （0 会让下一笔 delta 变成一整笔假收入）。
local function ReadTotal()
    local fn = _G.GetMoney
    if type(fn) ~= "function" then return nil end
    local okCall, v = pcall(fn)
    if not okCall or type(v) ~= "number" then return nil end
    return v
end

---------------------------------------------------------------------------
-- 搬钱队列：号间搬钱既不是收入也不是支出（计划 §1 #4）
---------------------------------------------------------------------------

--- 被拒绝的请求（钱不够、公会银行权限、超上限）永远等不到 PLAYER_MONEY。
--- 没有 TTL 的话，它会在很久之后精准吃掉一笔同额的真实收支 —— 这条注释就是那个坑。
local function QueueTransfer(amount, dir, from)
    if type(amount) ~= "number" or amount <= 0 then return end
    if type(dir) ~= "string" or type(from) ~= "string" then return end
    if IsSecretAmount(amount) then return end   -- 秘密值日后没法精确比，宁可不排队
    transfers[#transfers + 1] = { amount = amount, dir = dir, from = from, at = Uptime() }
    while #transfers > TRANSFER_QUEUE_LIMIT do
        table.remove(transfers, 1)              -- 超上限丢最老的
    end
end

--- 找一条"金额精确相等且方向相同"的请求并摘掉；顺手清掉过期条目。
--- @return string|nil 命中则返回搬钱来源（warband / guildbank）
local function TakeMatchingTransfer(absAmount, dir)
    local now = Uptime()
    for i = #transfers, 1, -1 do
        local t = transfers[i]
        if now and t.at and (now - t.at) > TRANSFER_TTL_SECONDS then
            table.remove(transfers, i)
        end
    end
    for i = 1, #transfers do
        local t = transfers[i]
        if t.amount == absAmount and t.dir == dir then
            table.remove(transfers, i)
            return t.from
        end
    end
    return nil
end

---------------------------------------------------------------------------
-- 归属判据
---------------------------------------------------------------------------

-- ★★★ 来源归属优先级，全插件只有这一处，要改顺序只改这里 ★★★
--   1 transfer   搬钱队列同额同向命中 —— 压过任何开着的界面窗口（在银行窗口里
--                卖个灰、在 AH 里取钱都不该改判）
--   2 repair     修理标志 + 卖店支出（修理费混在卖店消费里，只能靠钩子分开）
--   3 ah         邮件收入 + 快照金额精确相等 **且** 发件人正是拍卖行常量
--   4 context    界面上下文（vendor / ah / mail / trade / loot / quest）
--   5 unknown    都没命中 —— 界面外的变动（到期自动到账之类），计划 §6 的固有边界，
--                不做猜测式补全
-- 命中 1 时绝不消耗修理标志、绝不弹邮件快照，否则后面那笔真实消费会被判成 repair。
local function Attribute(absAmount, dir)
    local from = TakeMatchingTransfer(absAmount, dir)
    if from then
        return from, true, nil
    end

    if context == "vendor" and dir == MONEY_OUT and repairPending then
        repairPending = false
        return "repair", false, nil
    end

    if context == "mail" and dir == MONEY_IN then
        -- 拍卖行卖出的钱是邮件送来的（计划 §1 #7）。只对得上"刚点收取"那一封，
        -- 对不上就老实留在 mail —— 宁可不归因，也不假归因。
        local snap = table.remove(mailSnaps, 1)
        if snap then
            local meta = { sender = snap.sender, subject = snap.subject }
            local seller = _G["AUCTION_HOUSE_MAIL_SELLER"]   -- 国服存不存在没验过（§6）
            if seller ~= nil and snap.money == absAmount and snap.sender == seller then
                return "ah", false, meta
            end
            return "mail", false, meta
        end
        return "mail", false, nil
    end

    return context, false, nil
end

---------------------------------------------------------------------------
-- 写出：一笔变动只产生一次调用
---------------------------------------------------------------------------

local function PushRecent(signedAmount, source, isTransfer)
    recent[#recent + 1] = { amount = signedAmount, source = source, transfer = isTransfer and true or false }
    while #recent > RECENT_PROBE_KEEP do
        table.remove(recent, 1)
    end
end

--- 只有账本模块（T2）没注册这一种降级：不报错、不假装成功，
--- 第一条 print 一次、之后只默默计数（用例 test_money_without_ledger_logs_once）。
local function Emit(signedAmount, source, meta)
    PushRecent(signedAmount, source, meta.transfer)
    local ledger = GCL and GCL.Ledger
    if type(ledger) ~= "table" or type(ledger.AddEntry) ~= "function" then
        counters.droppedWithoutLedger = counters.droppedWithoutLedger + 1
        if counters.droppedWithoutLedger == 1 then
            print(("GCL: 账本模块（Ledger）还没注册，这笔只进了会话统计：%s %s 铜")
                  :format(tostring(source), tostring(signedAmount)))
        end
        return false
    end
    local okCall, err = pcall(ledger.AddEntry, ledger, signedAmount, source, meta)
    if not okCall then
        print("GCL: 写账本出错: " .. tostring(err))
        return false
    end
    return true
end

local function RecordChange()
    if not trackingOn then return end   -- 基准还没取：登录瞬间的钱不能当收入（§1 #2）

    local total = ReadTotal()
    if type(total) ~= "number" then return end
    if type(baseline) ~= "number" then
        baseline = total                -- 基准问不出来时，这一笔只当基准，不产生流水
        return
    end

    if IsSecretAmount(total) then
        -- 秘密值一律不许进减法（拿它当基准，下一笔 `总额 - 基准` 会一直抛错，
        -- 基准再也推不动 ⇒ 整条记账从此瘫痪）。这里把基准作废：这一笔不记，
        -- 下一次问得出正常数时重新立基准。宁缺这一笔，也不要假的一笔。
        baseline = nil
        return
    end

    local delta = total - baseline
    if delta == 0 then
        baseline = total                -- 空转的 PLAYER_MONEY：只刷基准，不发流水
        return
    end

    local absAmount = delta > 0 and delta or -delta
    local dir = delta > 0 and MONEY_IN or MONEY_OUT
    local source, isTransfer, mailMeta = Attribute(absAmount, dir)

    if not isTransfer then
        if dir == MONEY_IN then
            session.income = session.income + absAmount
        else
            session.expense = session.expense + absAmount
        end
    end

    if context == "quest" then
        context = SOURCE_UNKNOWN        -- 任务上下文一笔就清（§1 #3）
    end

    local meta = { transfer = isTransfer and true or false }
    if mailMeta then
        meta.sender = mailMeta.sender    -- 只带真从 GetInboxHeaderInfo 读到的字段；
        meta.subject = mailMeta.subject  -- 物品名/数量等没读到的东西一律不编
    end

    Emit(delta, source, meta)
    baseline = total                     -- 连续两笔各自独立：不并、不串（§1 #1）
end

---------------------------------------------------------------------------
-- 钩子与事件
---------------------------------------------------------------------------

--- 后置钩子统一入口：hooksecurefunc 挂不上时降级计数，绝不把整趟初始化带崩。
local function InstallHook(target, name, handler)
    if type(_G.hooksecurefunc) ~= "function" then
        counters.hookFailures = counters.hookFailures + 1
        return
    end
    local okCall
    if target then
        okCall = pcall(_G.hooksecurefunc, target, name, handler)
    else
        okCall = pcall(_G.hooksecurefunc, name, handler)
    end
    if not okCall then
        counters.hookFailures = counters.hookFailures + 1
    end
end

local function InstallHooks()
    -- 修理费与卖店消费分不开 ⇒ 只留一个标志，下一笔 vendor 支出改判 repair
    InstallHook(nil, "RepairAllItems", function()
        repairPending = true
    end)

    -- 收邮件钱的当场就把这一封的 sender/subject/money 抓下来：钱取走就读不到了。
    -- 连续点"全部收取"会连着调用多次 ⇒ 必须 FIFO 排队，不能只留一条。
    InstallHook(nil, "TakeInboxMoney", function(index)
        if type(index) ~= "number" then return end
        local fn = _G.GetInboxHeaderInfo
        if type(fn) ~= "function" then return end
        local okCall, _a, _b, sender, subject, money = pcall(fn, index)
        if not okCall then return end
        if type(money) ~= "number" or money <= 0 then return end
        if IsSecretAmount(money) then return end
        mailSnaps[#mailSnaps + 1] = { sender = sender, subject = subject, money = money }
        while #mailSnaps > MAIL_SNAPSHOT_LIMIT do
            table.remove(mailSnaps, 1)
        end
    end)

    -- 公会银行：取出 = 角色这边进钱，存入 = 角色这边出钱，两向都是搬钱
    InstallHook(nil, "WithdrawGuildBankMoney", function(amount)
        QueueTransfer(amount, MONEY_IN, "guildbank")
    end)
    InstallHook(nil, "DepositGuildBankMoney", function(amount)
        QueueTransfer(amount, MONEY_OUT, "guildbank")
    end)

    -- 战团银行（12.x 的 C_Bank 一族）；表或方法不存在就整块跳过
    local bank = _G.C_Bank
    if type(bank) == "table" then
        if type(bank.WithdrawMoney) == "function" then
            InstallHook(bank, "WithdrawMoney", function(_, amount)
                QueueTransfer(amount, MONEY_IN, "warband")
            end)
        end
        if type(bank.DepositMoney) == "function" then
            InstallHook(bank, "DepositMoney", function(_, amount)
                QueueTransfer(amount, MONEY_OUT, "warband")
            end)
        end
    end
end

local function InstallEvents()
    GCL.RegisterEvent("PLAYER_MONEY", RecordChange)
    for event in pairs(CONTEXT_SET) do
        GCL.RegisterEvent(event, function()
            context = CONTEXT_SET[event]
        end)
    end
    for event in pairs(CONTEXT_CLEAR) do
        GCL.RegisterEvent(event, function()
            context = SOURCE_UNKNOWN
        end)
    end
end

function Money:OnInitialize()
    if installed then return end      -- 二次 ADDON_LOADED 不许把钩子挂两遍
    installed = true
    InstallEvents()
    InstallHooks()
end

--- 基准只在 PLAYER_LOGIN 之后取（ADDON_LOADED 时金币可能还没同步完）：
--- 登录那一刻钱包里的钱绝不能变成一笔收入（§1 #2）。
function Money:OnLogin()
    trackingOn = true
    baseline = ReadTotal()
end

---------------------------------------------------------------------------
-- 对外读数
---------------------------------------------------------------------------

--- 本次会话统计（只在内存，不落盘 §1 #10）；搬钱不计入收支。
--- @return table { income, expense, net } 单位铜
function Money.GetSessionStats()
    return { income = session.income, expense = session.expense, net = session.income - session.expense }
end

--- 最近捕获的几笔（给 /gcl probe，也给 T3 的界面顶栏复用；口径与 probe 完全一致，
--- 免得两处各算一遍算出两个答案）
function Money.GetRecentMovements()
    return recent
end

--- 账本缺席时丢掉了几笔（只报第一条 print，其余靠这个数看出来）
function Money.GetDroppedCount()
    return counters.droppedWithoutLedger
end

---------------------------------------------------------------------------
-- /gcl probe：只读取证，不改任何状态。
-- 目的是让用户在他自己的 12.1.5 客户端上跑出"这些接口到底有没有"，
-- 所以每一条读取都包 pcall，取不到就印"没有" —— 首要要求是绝不报错。
---------------------------------------------------------------------------

local function DescribeCallable(fn)
    if type(fn) ~= "function" then return "没有" end
    return "有"
end

local function DescribeMember(tbl, key)
    if type(tbl) ~= "table" then return "没有（表本身不存在）" end
    return DescribeCallable(tbl[key])
end

local function PrintProbe()
    print("GCL probe: 记账层依赖的接口取证（只读，不改状态）")
    local seller = _G["AUCTION_HOUSE_MAIL_SELLER"]
    if seller == nil then
        print("AUCTION_HOUSE_MAIL_SELLER: 没有 —— 拍卖行邮件一律老实记 mail")
    else
        print(("AUCTION_HOUSE_MAIL_SELLER: %s"):format(tostring(seller)))
    end
    print(("C_Bank.WithdrawMoney: %s"):format(DescribeMember(_G.C_Bank, "WithdrawMoney")))
    print(("C_Bank.DepositMoney: %s"):format(DescribeMember(_G.C_Bank, "DepositMoney")))
    print(("WithdrawGuildBankMoney: %s"):format(DescribeCallable(_G.WithdrawGuildBankMoney)))
    print(("DepositGuildBankMoney: %s"):format(DescribeCallable(_G.DepositGuildBankMoney)))
    print(("issecretvalue: %s"):format(DescribeCallable(_G.issecretvalue)))
    print(("GetInboxHeaderInfo: %s"):format(DescribeCallable(_G.GetInboxHeaderInfo)))
    print(("RepairAllItems: %s"):format(DescribeCallable(_G.RepairAllItems)))
    print(("TakeInboxMoney: %s"):format(DescribeCallable(_G.TakeInboxMoney)))
    print(("金币总额可读: %s"):format(ReadTotal() == nil and "读不到" or tostring(ReadTotal())))
    print(("当前上下文: %s / 搬钱队列 %d 条 / 邮件快照 %d 条 / 钩子失败 %d 次")
          :format(tostring(context), #transfers, #mailSnaps, counters.hookFailures))

    -- 计划 §6 要的那一次取证：拍卖行到账邮件的发件人字符串到底是什么，
    -- 只能等他真收一笔才知道 —— 这里把抓到的原样打出来，不加工、不翻译。
    if #mailSnaps > 0 then
        print("待认领的邮件快照（发件人原样）:")
        for i = 1, #mailSnaps do
            local m = mailSnaps[i]
            print(("  [%d] sender=%s subject=%s money=%s")
                  :format(i, tostring(m.sender), tostring(m.subject), tostring(m.money)))
        end
    end

    local n = #recent
    if n == 0 then
        print("最近 5 笔变动: 还没有")
        return
    end
    print(("最近 %d 笔变动（金额带符号，铜）:"):format(n))
    for i = 1, n do
        local r = recent[i]
        print(("  %s %+d"):format(r.transfer and "transfer" or r.source, r.amount))
    end
end

if GCL.RegisterCommand then
    GCL.RegisterCommand("probe", PrintProbe, "取证：本客户端上记账层依赖的接口在不在")
end

return Money
