# GoldQuestLedger 阶段一离线单测：纯数据逻辑 + API 适配降级 + 同步时序
# 用法：python tests/run_gql_tests.py            —— 运行单测
#       python tests/run_gql_tests.py --syntax    —— 额外对插件全部 Lua 做 loadfile 语法检查
# 静态守卫：test_lua_upvalue_budget 每次都跑，数每个顶层函数的 upvalue（WoW 上限 60）。
import os
import re
import sys

from lupa import LuaRuntime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "GoldQuestLedger")
FAILED = []


def fail(name, msg):
    FAILED.append(name)
    # Windows 控制台默认 cp936，矩阵文案里的 ✓/●/⊘ 会直接把 print 炸掉，
    # 失败信息看不见了比失败本身更糟 —— 这里按控制台编码做无损降级替换。
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(msg).encode(enc, "replace").decode(enc, "replace")
    print(f"FAIL {name}: {safe}")


def ok(name):
    print(f"PASS {name}")


# WoW 环境替身：只提供本阶段真正用到的入口。
# C_Timer.After 不自动执行，测试用 GQL_FireTimers() 手动推进（配合 GQL_NOW），
# 否则每个用例都要真等 2 秒/15 秒。delay 未到期的定时器必须留在队列里。
GQL_MOCK_ENV = r"""
UIParent = {}
-- 暴雪的 ESC 关闭名单：真客户端里由 FrameXML 预置，帧必须用全局名字挂进来。
-- 替身不给这张表，Config.RegisterEscapeClose 会一律走降级分支，
-- "ESC 到底挂没挂上"这条就永远测不出来。
UISpecialFrames = {}
GQL_LOG = {}
GQL_NEWTIMERS = {}
GQL_NOW = 0
C_Timer = { After = function(delay, fn) table.insert(GQL_LOG, { delay = delay, fn = fn }) end }
-- NewTimer：可取消的定时器（到期唤醒用它）。Cancel 后 GQL_FireTimers 不再触发它。
C_Timer.NewTimer = function(delay, fn)
    local t = { delay = delay, fn = fn, cancelled = false }
    t.Cancel = function(self) self.cancelled = true end
    table.insert(GQL_NEWTIMERS, t)
    return t
end
function GQL_FireTimers()
    local pending, fired = GQL_LOG, 0
    GQL_LOG = {}
    for i = 1, #pending do
        if pending[i].delay <= GQL_NOW then
            fired = fired + 1
            pending[i].fn()
        else
            table.insert(GQL_LOG, pending[i])
        end
    end
    -- 可取消定时器：到期的触发（已取消的不触发），未到期的留到下一趟。
    -- 先换队列表再跑回调：客户端的游戏时间不会在一次回调里跳，所以回调里新排的
    -- 定时器只能等下一趟 GQL_FireTimers；反过来（跑完再整体赋值）会把它们吞掉，
    -- "账本没就绪就重排一次重试"这类自我续期的路径就永远测不到。
    local snapshot, keep = GQL_NEWTIMERS, {}
    GQL_NEWTIMERS = keep
    for i = 1, #snapshot do
        local t = snapshot[i]
        if t.cancelled then
            -- 已取消：直接丢弃
        elseif t.delay <= GQL_NOW then
            fired = fired + 1
            t.fired = true
            t.fn()
        else
            keep[#keep + 1] = t
        end
    end
    return fired
end
function GetTime() return GQL_NOW or 0 end
-- 拖动滚动条要读光标位置：屏幕坐标原点在左下、单位是物理像素，
-- 用例靠改 GQL_CURSOR 模拟"手往下/往右拖"。
GQL_CURSOR = { 0, 0 }
function GetCursorPosition() return GQL_CURSOR[1], GQL_CURSOR[2] end
-- 悬停扫描会先问"焦点到底在谁身上"（IsMouseOver 只比几何、不看遮挡）：焦点不在
-- 矩阵子树里 —— 含 nil 和 UIParent —— 就不许亮行、不许弹提示。用例要模拟
-- "鼠标真停在热区上"就把 GQL_FOCUS 设成那个帧；要模拟遮挡就设成别的窗口。
GQL_FOCUS = nil
function GetMouseFocus() return GQL_FOCUS end
SlashCmdList = {}
SLASH_GQL1 = nil
function print(...) return ... end
"""


# 游戏里 RegisterEvent 遇到未知事件名是直接抛错的（12.1.5 实测
# "Attempt to register unknown event"），替身必须照做，否则"未知事件名把登录同步整条炸掉"
# 这种真事故在离线单测里永远复现不了。名单里没有的即视为该客户端不认识。
GQL_KNOWN_EVENTS = r"""
GQL_KNOWN_EVENTS = {
    ADDON_LOADED = true, PLAYER_LOGIN = true, PLAYER_ENTERING_WORLD = true,
    QUEST_LOG_UPDATE = true, QUEST_COMPLETE = true, QUEST_TURNED_IN = true,
}
"""


def load_lua(*names, mock=False):
    """按 toc 顺序加载 Lua 文件。mock=True 时先注入 WoW 替身（含 CreateFrame）。"""
    lua = LuaRuntime(unpack_returned_tuples=True)
    if mock:
        lua.execute(GQL_MOCK_ENV)
        lua.execute(GQL_KNOWN_EVENTS)
        lua.execute(r"""
        function CreateFrame(frameType, name, parent, template)
            local f = { __events = {}, __scripts = {}, __shown = false, __type = frameType,
                        __parent = parent, __children = {},
                     SetScript = function(self, k, fn) self.__scripts[k] = fn end,
                     GetScript = function(self, k) return self.__scripts[k] end,
                     IsShown = function(self) return self.__shown end,
                     Show = function(self) self.__shown = true end,
                     -- 客户端只在"从可见变不可见"时触发一次 OnHide，没挂脚本时什么都不做。
                     -- 替身必须照做：矩阵把"停后台轮询"挂在 OnHide 上（ESC 是暴雪直接 Hide
                     -- 我们的帧，不走 Toggle），Hide 不触发的话这条在离线永远测不到。
                     Hide = function(self)
                         local was = self.__shown
                         self.__shown = false
                         if was and self.__scripts and self.__scripts["OnHide"] then
                             self.__scripts["OnHide"](self)
                         end
                     end }
            -- 游戏里带名字的 CreateFrame 会把自己注册成全局，矩阵/按钮的用例要靠这个找到帧；
            -- 子帧也挂进父帧 __children，帧树文本才走得完。
            if name then _G[name] = f end
            -- UIParent 在替身里是个裸表，没有 __children 字段，必须先建再挂
            if type(parent) == "table" then
                parent.__children = parent.__children or {}
                parent.__children[#parent.__children + 1] = f
            end
            f.SetSize = function(self, w, h) self.__w, self.__h = w, h end
            f.SetWidth = function(self, w) self.__w = w end
            f.SetHeight = function(self, h) self.__h = h end
            f.GetSize = function(self) return self.__w or 0, self.__h or 0 end
            -- ScrollFrame 接口（矩阵滚动区用暴雪原声组件）
            f.SetScrollChild = function(self, child) self.__scrollchild = child end
            f.GetScrollChild = function(self) return self.__scrollchild end
            f.SetHorizontalScroll = function(self, v) self.__hscroll = v end
            f.GetHorizontalScroll = function(self) return self.__hscroll or 0 end
            f.SetVerticalScroll = function(self, v) self.__vscroll = v end
            f.GetVerticalScroll = function(self) return self.__vscroll or 0 end
            f.GetHorizontalScrollRange = function(self) return 0 end
            f.GetVerticalScrollRange = function(self) return 0 end
            f.SetBackdropColor = function(self, r, g, b, a)
                self.__bdcolor = { r, g, b, a } end
            f.GetWidth = function(self) return self.__w or 0 end
            f.GetHeight = function(self) return self.__h or 0 end
            f.GetFrameStrata = function(self) return self.__strata or "MEDIUM" end
            f.GetFrameLevel = function(self) return self.__framelevel or 1 end
            f.SetFrameLevel = function(self, l) self.__framelevel = l end
            f.GetCenter = function(self) return self.__cx or 0, self.__cy or 0 end
            f.GetEffectiveScale = function(self) return self.__scale or 1 end
            f.SetNormalTexture = function(self, t) self.__normal = t end
            -- 纹理对象自己也有 SetTexture/GetTexture（小地图按钮的金币图标是子纹理，
            -- 探针要把"实际设上去的是哪张图"报回给用户，替身缺这两个方法就验不到）
            f.SetTexture = function(self, t) self.__texture = t end
            f.GetTexture = function(self) return self.__texture end
            -- 真客户端 GetNormalTexture 返回的是**纹理对象**，不是当初传进去的路径字符串。
            -- 替身要是也回字符串，"把返回值直接塞进 %s" 这种写法离线永远测不出来
            -- （12.1.5 实测：ButtonInfo 因此把整份 probe 报告炸成一行 bad argument #3）。
            f.GetNormalTexture = function(self)
                if self.__normal == nil then return nil end
                return { __path = self.__normal,
                         GetTexture = function(tex) return tex.__path end }
            end
            f.SetHighlightTexture = function(self, t) self.__highlight = t end
            -- 这里**不许**出现真客户端没有的方法名。上一版补过
            -- SetTooltipDescriptionFunc / GetTooltipDescription，理由写的是
            -- "不存的话用例读不到"，结果生产代码照着替身写，12.1.5 里按钮根本没这个方法：
            -- Matrix.lua 的"刷新"按钮调用它 → Ensure 断在半路 → 文字池没建 → Render 再炸 →
            -- 矩阵打开一篇漆黑且再也打不开（Ensure 开头就 return 那张半成品皮）。
            -- 按钮悬停说明的实测写法是 OnEnter/OnLeave + GameTooltip（见小地图按钮）。
            f.RegisterEvent = function(self, e)
                if GQL_KNOWN_EVENTS[e] ~= true then
                    error(('Attempt to register unknown event "%s"'):format(tostring(e)))
                end
                self.__events[e] = true
            end
            f.UnregisterEvent = function(self, e) self.__events[e] = nil end
            f.IsEventRegistered = function(self, e) return self.__events[e] == true end
            f.SetPoint = function(self, a, b, c, d, e)
                self.__points = self.__points or {}
                table.insert(self.__points, { a, b, c, d, e })
            end
            f.ClearAllPoints = function(self) self.__points = nil end
            f.SetText = function(self, t) self.__text = tostring(t) end
            f.GetText = function(self) return self.__text or "" end
            f.SetJustifyH = function(self, h) self.__justifyH = h end
            f.CreateFontString = function(self, _point, _layer, font)
                local c = CreateFrame("FontString", nil, self)
                -- 记下建串时用的字体：矩阵的文字池是复用条目，只看文本看不出
                -- 字体串了水（12.1.5 实测"任务名变成组头的大号字"就是这么来的）。
                -- 真客户端 CreateFontString 的第 3 参就是这个字体名。
                c.__font = font
                return c
            end
            f.SetBackdrop = function(self, b) self.__backdrop = b end
            -- 遮挡判定用 GetBackdrop 区分"真窗口"和"铺满整屏的布局容器"
            f.GetBackdrop = function(self) return self.__backdrop end
            f.SetMovable = function(self, v) self.__movable = v end
            f.SetClampedToScreen = function(self, v) self.__clamped = v end
            f.EnableMouse = function(self, v) self.__mouse = v end
            f.IsMouseOver = function(self) return self.__mouseover == true end
            -- RegisterForClicks 把注册到的点击串按真客户端的语义（整组替换，不是追加）
            -- 记在帧上。第 31 轮下线矩阵右键屏蔽入口，守卫要读"这一行到底还注册没注册
            -- RightButtonUp"——替身不记的话生产代码那句 pcall 本来就把它吞了（以前帧上
            -- 根本没这个方法），"矩阵不再提供任何屏蔽入口"这条离线就永远验不到。
            f.RegisterForClicks = function(self, ...)
                self.__clicks = {}
                for i = 1, select("#", ...) do
                    self.__clicks[i] = tostring(select(i, ...))
                end
            end
            -- 可见 = 自己和所有祖先都 Show 过：矩阵的遮挡判定按这个筛候选窗口，
            -- 替身缺 IsVisible 的话那条判定在离线永远走不到。
            f.IsVisible = function(self)
                local n = self
                while n do
                    if n.__shown ~= true then return false end
                    n = n.__parent
                end
                return true
            end
            f.GetName = function(self) return self.__name or name end
            -- HookScript 是"追加"不是替换：Track.Init 每次过图被 Core 调一次，
            -- 不守卫就会累积，每次开图多扫一遍全树。按次数记下来才测得出来。
            f.HookScript = function(self, k, fn)
                self.__hooks = self.__hooks or {}
                self.__hooks[k] = (self.__hooks[k] or 0) + 1
                self.__scripts[k] = fn
            end
            -- 子帧的两种给法都替上，两个计数用来验扫描优先走不建表的那条
            f.GetChildren = function(self)
                self.__getChildrenCalls = (self.__getChildrenCalls or 0) + 1
                -- 替身跑在 lupa 的 Lua 版本上（5.1 才有全局 unpack），两种都要认
                local u = table.unpack or unpack
                return u(self.__children or {})
            end
            f.EnumerateChildren = function(self)
                self.__enumCalls = (self.__enumCalls or 0) + 1
                -- 现客户端形态：回迭代器函数（不是"带 Next 的对象"——上一版替身
                -- 造的是后者，客户端压根不回这个形状，于是绿的测试对应的是一条死路）
                local kids, i = self.__children or {}, 0
                return function() i = i + 1 return kids[i] end
            end
            f.CreateTexture = function(self)
                local t = CreateFrame("Texture", nil, self)
                return t
            end
            f.RegisterForDrag = function(self, b) self.__drag = b end
            f.StartMoving = function(self) self.__moving = true end
            f.StopMovingOrSizing = function(self) self.__moving = false end
            f.SetFrameStrata = function(self, s) self.__strata = s end
            f.SetParent = function(self, p) self.__parent = p end
            f.GetParent = function(self) return self.__parent end
            f.SetAlpha = function(self, a) self.__alpha = a end
            -- 遮挡判定要跳过半透明以下的常驻辅助帧；没设过 alpha 的帧在客户端是 1。
            f.GetAlpha = function(self) return self.__alpha or 1 end
            f.SetColorTexture = function(self, r, g, b, a)
                self.__color = { r, g, b, a } end
            f.SetAllPoints = function(self, o) self.__allpoints = o end
            -- 滚动条（Slider）与输入框（EditBox）：设置面板用得到
            f.SetMinMaxValues = function(self, a, b) self.__min, self.__max = a, b end
            f.GetMinMaxValues = function(self) return self.__min or 0, self.__max or 0 end
            f.SetValue = function(self, v) self.__value = v end
            f.GetValue = function(self) return self.__value or 0 end
            f.SetValueStep = function(self, s) self.__step = s end
            f.SetAutoFocus = function(self, v) self.__autofocus = v end
            f.SetTextColor = function(self, r, g, b, a) self.__textcolor = { r, g, b, a } end
            f.Enable = function(self) self.__enabled = true end
            f.Disable = function(self) self.__enabled = false end
            -- CheckButton 的勾选态（设置面板"登录时同步"用）。真客户端 SetChecked 收
            -- true/false（历史形态 1/nil 也认），GetChecked 现代客户端回布尔。
            f.SetChecked = function(self, v) self.__checked = not not v end
            f.GetChecked = function(self) return self.__checked end
            -- Button:Click() 是真客户端方法（第十四轮崩溃栈里 163UI 的小地图按钮
            -- 就是这么调 Minimap 按钮的 Click 的）。不替上的话"回车=点确定"这类
            -- 接线在离线永远走不到，等于没测。
            f.Click = function(self, button)
                local s = self.__scripts and self.__scripts["OnClick"]
                if s then s(self, button or "LeftButton") end
            end
            return f
        end
        -- UIParent 在真客户端里就是一张帧：矩阵判遮挡要 UIParent:GetChildren() 枚举
        -- 顶层窗口。裸表没有这些方法，那条判定在离线会整段跳过，等于没测。
        do
            local up = CreateFrame("Frame")
            for k, v in pairs(up) do if UIParent[k] == nil then UIParent[k] = v end end
            UIParent.__shown = true
            UIParent.__w, UIParent.__h = 1440, 720
        end
        -- 地图宿主：矩阵按钮挂它上面。第三条用例把它置 nil 验证降级。
        WorldMapFrame = CreateFrame("Frame", "WorldMapFrame", UIParent)
        -- 小地图宿主：阶段二按钮改挂 Minimap（环上图标），替身要能取到且带尺寸。
        Minimap = CreateFrame("Frame", "Minimap", UIParent)
        Minimap:SetSize(160, 160)
        """)
    # 命名空间：游戏里由 Core.lua 的 vararg 提供，测试里预置空表
    lua.execute(r"""
        GQL = GQL or {}
        GQL.commands = {}
        function GQL.RegisterCommand(name, fn, help) GQL.commands[name] = { fn = fn, help = help or "" } end
    """)
    for name in names:
        with open(os.path.join(PLUGIN_DIR, name + ".lua"), encoding="utf-8") as f:
            lua.execute(f.read())
    return lua, lua.globals().GQL


def _lua_top_locals(text):
    """顶层 local / 顶层 function 名字集合 —— 它们就是每个闭包的 upvalue 候选。"""
    ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    top = set()
    for ln in text.split("\n"):
        if ln.startswith("local "):
            head = ln[6:].split("=")[0]
            m = re.match(r"function\s+([\w.:]+)", head)
            if m:
                top.add(m.group(1).split(".")[-1])
            else:
                top.update(ident.findall(head))
        m = re.match(r"function\s+([\w.:]+)", ln)
        if m:
            top.add(m.group(1).split(".")[-1])
    return top


def _lua_strip(raw):
    s = re.sub(r"--.*$", "", raw)
    s = re.sub(r'"(?:[^"\\]|\\.)*"', " ", s)
    return re.sub(r"'(?:[^'\\]|\\.)*'", " ", s)


def _lua_body_end(lines, idx):
    d = 0
    for j in range(idx, len(lines)):
        s = _lua_strip(lines[j])
        d += len(re.findall(r"\b(?:function|then|do|repeat)\b", s))
        # elseif 自带 then 但不带独立 end，先扣掉再数
        d -= len(re.findall(r"\belseif\b", s))
        d -= len(re.findall(r"\bend\b", s))
        if d <= 0 and j >= idx:
            return j
    return len(lines) - 1


def test_repaint_coalesces_within_a_burst():
    """同一批更新里连着来几次 Refresh，只重排一次版面：
    一次快照会同时触发补读、到期唤醒、交任务结算三处重绘，
    每次都全量排版（帧池化省的是建帧，不是排版）。
    首次重绘必须同步生效——用户点刷新后要立刻看到新数据。"""
    lua, gql, _ = sync_env([(170, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()                       # 打开：立刻画一次
    renders = int(lua.execute("return GQL.Matrix.renderCount or 0"))
    if renders != 1:
        return fail("repaint_coalesces_within_a_burst",
                    f"打开面板应立刻画一次，实际 {renders}")
    for _ in range(4):
        gql.Matrix.Refresh()                  # 同一批更新里连着要重绘
    after = int(lua.execute("return GQL.Matrix.renderCount or 0"))
    if after != renders:
        return fail("repaint_coalesces_within_a_burst",
                    f"合并没生效：立刻又画了 {after - renders} 次")
    g.GQL_NOW = 2
    lua.globals().GQL_FireTimers()             # 合并窗口到点：补画一次
    done = int(lua.execute("return GQL.Matrix.renderCount or 0"))
    if done != renders + 1:
        return fail("repaint_coalesces_within_a_burst",
                    f"合并窗口到点只该补画一次，实际画到 {done}")
    ok("repaint_coalesces_within_a_burst")


def test_lua_upvalue_budget():
    """WoW 的 Lua 5.1 每个闭包最多 60 个 upvalue，超了整份文件加载失败
    （12.1.5 实测：Matrix.lua "function has more than 60 upvalues" → /gql matrix
     和小地图图标一起消失）。lupa 用的是 LuaJIT，没有这个上限，语法检查抓不到，
    只能静态数：顶层函数的 upvalue 只可能是"体内引用到的顶层 local"，数这个即可
    （内层闭包还会再加上外层 local，但体内引用只会更少，不改变超限结论）。
    加顶层 local 之前先跑这条；快贴上限时把新状态收进表里（见 Matrix 的 Geom/Head）。"""
    limit = 60
    for name in sorted(os.listdir(PLUGIN_DIR)):
        if not name.endswith(".lua"):
            continue
        with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        top = _lua_top_locals("\n".join(lines))
        worst, worst_line = 0, 0
        for i, raw in enumerate(lines):
            if raw[:1].isspace() or not re.search(r"\bfunction\b", raw):
                continue          # 只看顶格定义（含 ClampOffsets = function 这种补写）
            body = "\n".join(_lua_strip(l) for l in lines[i + 1:_lua_body_end(lines, i) + 1])
            n = sum(1 for key in top
                    if re.search(r"(?<![\w.:])%s\b" % re.escape(key), body))
            if n > worst:
                worst, worst_line = n, i + 1
        if worst > limit:
            fail("upvalues:%s" % name,
                 "最热的顶层函数 L%d 约 %d 个 upvalue，超过 WoW 上限 %d" % (worst_line, worst, limit))
        else:
            ok("upvalues:%s" % name)


def test_no_old_addon_name_left_anywhere():
    """静态守卫（改名轮）：旧插件名与旧全局前缀是撞了别人家同名项目的字样
    （出处引用是含 Tercioo 的署名行，白名单整行放行），改名之后插件代码、
    toc 与本 runner 里都不该再有残留。
    命中判定用的旧名串是拆开的字面量拼接：整体串一是会被换名机械替换吞掉，
    二是会让本守卫扫到自己时命中模式串本身，两头都不能要。"""
    old_tokens = ("WQ" + "T", "WorldQuest" + "Tracker")
    targets = [os.path.join(PLUGIN_DIR, fn)
               for fn in sorted(os.listdir(PLUGIN_DIR))
               if fn.endswith(".lua") or fn.endswith(".toc")]
    targets.append(os.path.abspath(__file__))
    hits = []
    for path in targets:
        rel = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh.read().splitlines(), start=1):
                if "Tercioo" in line:
                    continue          # 白名单：外部署名行一个字母不许动
                if any(tok in line for tok in old_tokens):
                    hits.append("%s:%d %s" % (rel, i, line.strip()))
    if hits:
        shown = "\n".join(hits[:20])
        more = "" if len(hits) <= 20 else "\n… 共 %d 处" % len(hits)
        return fail("no_old_addon_name_left_anywhere",
                    "旧名残留（文件:行号 原文）：\n" + shown + more)
    ok("no_old_addon_name_left_anywhere")


def test_gql_slash_command_registered():
    """斜杠命令两处注册耦合（`SLASH_` + 大写键名的全局变量，与 SlashCmdList
    的键）改错任何一处，聊天框里命令直接消失且不报错 —— 这是行为门，
    静态守卫替不了它；断言里旧命令的全局名同样是拆开拼的，防换名吞掉断言。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    if str(g.SLASH_GQL1 or "") != "/gql":
        return fail("gql_slash_command_registered",
                    "SLASH_GQL1 应为 /gql，实际 %r" % (g.SLASH_GQL1,))
    t = str(lua.execute('return type(SlashCmdList["GQL"])'))
    if t != "function":
        return fail("gql_slash_command_registered",
                    "SlashCmdList['GQL'] 应为 function，实际 type=%r" % (t,))
    if g["SLASH_W" + "QT1"] is not None:
        return fail("gql_slash_command_registered",
                    "旧命令全局名（" + "SLASH_W" + "QT1）没清干净，命令名留了残骸")
    ok("gql_slash_command_registered")


def syntax_check():
    """WoW 专有全局在 loadfile 阶段不参与解析，所以这一步只抓语法错误。"""
    for name in sorted(os.listdir(PLUGIN_DIR)):
        if not name.endswith(".lua"):
            continue
        lua = LuaRuntime()
        # lupa 单值返回不套 tuple，成功时 loadfile 只回一个 chunk；失败时才回 (nil, err)。
        result = lua.globals().loadfile(os.path.join(PLUGIN_DIR, name).replace("\\", "/"))
        if isinstance(result, tuple):
            chunk, err = result
        else:
            chunk, err = result, None
        if chunk is None:
            fail("syntax:%s" % name, err)
        else:
            ok("syntax:%s" % name)


def test_config_init_builds_schema():
    """空库 Init 后建齐四张表并写入 schema 与 cycleAnchor。"""
    lua, gql = load_lua("Core", "Config", "Api", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    # 锚点只认周口径：12.1.5 实测 GetQuestResetTime 答的是每日重置（周五给出明早 7 点），
    # 拿它减一周得到的"本周起点"每天往前挪 24 小时，周统计会静默算错。
    epoch = 1700000000
    g.time = lambda: epoch
    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: 432000})
    g.GetQuestResetTime = lambda: 46509
    gql.Config.Init()
    db = g.GQLDB
    for key in ("quests", "chars", "turnins", "manual"):
        if db[key] is None:
            return fail("config_init_builds_schema", f"{key} 表未建立")
    # Task 9 让 schema "写了还真读"：Init 末尾跑一次性升级，新库建完就该停在最新档。
    # 断言跟着 SCHEMA_VERSION 走（不是放松，是把"停在上一档"这种半升级也钉死）。
    if int(db["schema"]) != int(gql.Config.SCHEMA_VERSION):
        return fail("config_init_builds_schema",
                    f"schema={db['schema']}, 期望最新档 {gql.Config.SCHEMA_VERSION}")
    if int(db["cycleAnchor"]) != epoch + 432000 - 604800:
        return fail("config_init_builds_schema",
                    f"锚点应由周重置倒计时反推，实际 {db['cycleAnchor']}")
    ok("config_init_builds_schema")


def test_config_init_idempotent():
    """重复 Init 不得清空已有账本。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    g.GetQuestResetTime = lambda: 1200000
    gql.Config.Init()
    g.GQLDB["quests"][42] = lua.table_from({"name": "测试任务"})
    gql.Config.Init()
    if g.GQLDB["quests"][42] is None:
        return fail("config_init_idempotent", "重复 Init 丢失已有任务记录")
    ok("config_init_idempotent")


def test_cycle_anchor_rolls_over():
    """周重置倒计时前移（进入新 CD）→ 锚点更新；未到期 → 不回退。"""
    lua, gql = load_lua("Core", "Config", "Api", mock=True)
    g = lua.globals()
    epoch = 1700000000
    g.GQLDB = lua.table_from({"cycleAnchor": 100})
    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: 300000})
    if gql.Config.RefreshCycle(epoch, 400000) is not True:
        return fail("cycle_anchor_rolls_over", "新 CD 应返回 true")
    if int(g.GQLDB["cycleAnchor"]) != epoch + 300000 - 604800:
        return fail("cycle_anchor_rolls_over",
                    f"锚点应是 墙上时钟+周倒计时-一周，实际 {g.GQLDB['cycleAnchor']}")
    # 同一个 CD 内再问一次：倒计时不会凭空变大，锚点必须原地不动
    if gql.Config.RefreshCycle(epoch, 400001) is not False:
        return fail("cycle_anchor_rolls_over", "同一 CD 内不该重复前进")
    if int(g.GQLDB["cycleAnchor"]) != epoch + 300000 - 604800:
        return fail("cycle_anchor_rolls_over", "未到期不应回退锚点")
    # 跨过重置点：墙上时钟前进一周 → 锚点跟着前进一周
    if gql.Config.RefreshCycle(epoch + 604800, 400000) is not True:
        return fail("cycle_anchor_rolls_over", "跨过重置点应前进到新 CD")
    if int(g.GQLDB["cycleAnchor"]) != epoch + 604800 + 300000 - 604800:
        return fail("cycle_anchor_rolls_over",
                    f"新 CD 锚点应前进一周，实际 {g.GQLDB['cycleAnchor']}")
    ok("cycle_anchor_rolls_over")


def test_cycle_anchor_reads_reset_as_countdown():
    """游戏内实测（12.1.5）：GetQuestResetTime=46509、GetTime=569772、time=1792577090。
    下次重置不可能落在过去，所以它是"距下次重置还剩几秒"的倒计时，不是绝对时间戳。
    倒计时当绝对值减一周会算出负锚点，周统计整段失真。"""
    lua, gql = load_lua("Core", "Config", "Api", mock=True)
    C = gql.Config
    epoch = 1792577090
    week = 604800
    got = C.CycleAnchorOf(46509, epoch, 569772)
    if got is None or int(got) != epoch + 46509 - week:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    f"有墙上时钟时锚点应是 纪元+倒计时-一周，实际 {got}")
    # 开机不足一周时倒计时无法在会话域定位（锚点会落在开机之前）→ 只能判未知，不编数
    if C.CycleAnchorOf(46509, None, 100) is not None:
        return fail("cycle_anchor_reads_reset_as_countdown", "会话域定不出锚点应返回 nil")
    got = C.CycleAnchorOf(46509, None, 700000)
    if got is None or int(got) != 700000 + 46509 - week:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    f"开机够久时可在会话域定位，实际 {got}")
    # 绝对纪元值（>1e9）照旧直接减一周，两种形态都要能吃下
    if int(C.CycleAnchorOf(epoch + 400000, None, None)) != epoch + 400000 - week:
        return fail("cycle_anchor_reads_reset_as_countdown", "纪元绝对值应直接减一周")
    # —— RefreshCycle 只认周口径 ——
    # 12.1.5 用户在矩阵上看到的"距下次周重置 12 时 6 分"= 45607 秒，正是每日档
    # （周五看到明早 7 点）。拿它当本周尾巴，玩家按这个数安排刷本周周常就全排错了，
    # 而且锚点每天往前挪 24 小时、周统计静默算错。所以每日档单独在场时锚点必须一动不动。
    g = lua.globals()
    g.GQLDB = lua.table_from({"cycleAnchor": 0})
    g.GetQuestResetTime = lambda: 46509
    g.GQL_NOW = 569772
    if C.RefreshCycle(epoch, 569772) is not False:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    "只有每日档时不许前进锚点：那是明天的重置，不是本周的")
    if int(g.GQLDB["cycleAnchor"]) != 0:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    f"每日档不该写锚点，实际 {g.GQLDB['cycleAnchor']}")
    logs = g.GQLDB["logs"]
    if "周重置接口不可用" not in str(logs[len(logs)]):
        return fail("cycle_anchor_reads_reset_as_countdown",
                    "降级要留一句日志，否则玩家只看到周统计一直是 ?")
    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: 46509})
    if C.RefreshCycle(epoch, 569772) is not True:
        return fail("cycle_anchor_reads_reset_as_countdown", "周倒计时形态应写进锚点")
    if int(g.GQLDB["cycleAnchor"]) != epoch + 46509 - week:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    f"锚点应落在纪元域，实际 {g.GQLDB['cycleAnchor']}")
    # Config.Init 那条路没人传时钟：RefreshCycle 必须自己在运行时问回来，
    # 否则锚点停在 0，周统计会把历次 CD 的记录全算成本周
    lua2, gql2 = load_lua("Core", "Config", "Api", mock=True)
    g2 = lua2.globals()
    g2.GQLDB = lua2.table_from({"cycleAnchor": 0})
    g2.GetQuestResetTime = lambda: 46509
    g2.GetServerTime = lambda: epoch
    g2.C_DateAndTime = lua2.table_from({"GetSecondsUntilWeeklyReset": lambda: 46509})
    g2.GQL_NOW = 569772
    if gql2.Config.RefreshCycle() is not True:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    "不传时钟时 RefreshCycle 应自己取时钟并前进锚点")
    if int(g2.GQLDB["cycleAnchor"]) != epoch + 46509 - week:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    f"自取时钟的锚点应一致，实际 {g2.GQLDB['cycleAnchor']}")
    # 周接口给出一个超过一周的数 = 不可能是周倒计时（答的是别的域）→ 照旧拒绝，
    # 否则锚点会落在未来，本周的记录全被判成"上周的"
    g2.C_DateAndTime = lua2.table_from({"GetSecondsUntilWeeklyReset": lambda: 700000})
    g2.GQLDB["cycleAnchor"] = 0
    if gql2.Config.RefreshCycle() is not False:
        return fail("cycle_anchor_reads_reset_as_countdown",
                    "超过一周的数不能当周倒计时用")
    ok("cycle_anchor_reads_reset_as_countdown")


def test_cycle_anchor_survives_missing_api():
    """GetQuestResetTime 缺失时沿用旧锚点，不报错。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({"cycleAnchor": 555})
    if gql.Config.RefreshCycle() is not False:
        return fail("cycle_anchor_survives_missing_api", "接口缺失应返回 false")
    if int(g.GQLDB["cycleAnchor"]) != 555:
        return fail("cycle_anchor_survives_missing_api", "锚点不应被改")
    ok("cycle_anchor_survives_missing_api")


def test_cycle_anchor_clock_domain_bridge():
    """GetQuestResetTime 若给的是会话内秒数，必须换算到纪元，否则跨一次开机就没法比。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    week = 604800
    epoch = 1700000000
    # 纪元域（>1e9）直接用，不做换算
    if int(gql.Config.CycleAnchorOf(epoch + 900000, None, None)) != epoch + 900000 - week:
        return fail("cycle_anchor_clock_domain_bridge", "纪元域应直接减一周")
    # 会话域：resetTime 与 uptimeNow 同域，用墙上时钟搭桥
    got = int(gql.Config.CycleAnchorOf(900000, epoch, 100000))
    if got != epoch + (900000 - 100000) - week:
        return fail("cycle_anchor_clock_domain_bridge", f"会话域应换算，实际 {got}")
    # 没有可信墙上时钟时退回会话域：宁可只在本次开机内可比，也不要算出错的数
    if int(gql.Config.CycleAnchorOf(900000, None, None)) != 900000 - week:
        return fail("cycle_anchor_clock_domain_bridge", "无墙上时钟应沿用旧口径")
    # RefreshCycle 这条生产路径只会拿到周倒计时（≤ 一周），锚点必须搭在墙上时钟的纪元域上：
    # 写进账本的数如果停在开机秒域，下次开机 GetTime 归零，本周窗口就散了。
    lua2, gql2 = load_lua("Core", "Config", "Api", mock=True)
    g2 = lua2.globals()
    g2.GQLDB = lua2.table_from({"cycleAnchor": 0})
    g2.GetQuestResetTime = lambda: 900000       # 每日档：只许当对照，不许进锚点
    g2.C_DateAndTime = lua2.table_from({"GetSecondsUntilWeeklyReset": lambda: 500000})
    if gql2.Config.RefreshCycle(epoch, 100000) is not True:
        return fail("cycle_anchor_clock_domain_bridge", "带墙上时钟时锚点应前进")
    if int(g2.GQLDB["cycleAnchor"]) != epoch + 500000 - week:
        return fail("cycle_anchor_clock_domain_bridge",
                    f"锚点应在纪元域、由周倒计时反推，实际 {g2.GQLDB['cycleAnchor']}")
    ok("cycle_anchor_clock_domain_bridge")


def test_format_gold():
    _, gql = load_lua("Config")
    # 1 金 = 100 银 = 10000 铜
    if str(gql.Config.FormatGold(12345)) != "1g23s45c":
        return fail("format_gold", f"12345 铜应显示 1g23s45c，实际 {gql.Config.FormatGold(12345)}")
    if str(gql.Config.FormatGold(123450)) != "12g34s50c":
        return fail("format_gold", f"123450 铜应显示 12g34s50c，实际 {gql.Config.FormatGold(123450)}")
    if str(gql.Config.FormatGold(None)) != "?":
        return fail("format_gold", "nil 必须显示 ?（未知），不能显示 0")
    if str(gql.Config.FormatGold(0)) != "0g0s0c":
        return fail("format_gold", "0 应显示 0g0s0c（真的没赚钱）")
    # WeekStats 用 -1 表示"本周统计不可信"，任何调用点都要显示 ?。
    # 判负必须收在 FormatGold 里：调用方写 `gold < 0 and nil or gold` 会被 Lua 的
    # `X and nil or Y` 恒等于 Y 坑掉，-1 会原样格式化成 "-1g99s99c"（游戏内实测过）。
    if str(gql.Config.FormatGold(-1)) != "?":
        return fail("format_gold", f"-1（未知哨兵）应显示 ?，实际 {gql.Config.FormatGold(-1)}")
    ok("format_gold")


def test_log_ring_buffer():
    """日志超过 200 条丢最旧。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    for i in range(1, 206):
        gql.Config.Log("行%d" % i)
    logs = g.GQLDB["logs"]
    if len(logs) != 200:
        return fail("log_ring_buffer", f"日志条数={len(logs)}, 期望 200")
    text = str(gql.Config.GetLogs())
    if "行205" not in text or "行1 " in text:
        return fail("log_ring_buffer", "应保留最新 200 条")
    ok("log_ring_buffer")


def ledger_db(lua):
    """构造可直接喂给 Ledger 的账本。"""
    return lua.table_from({
        "schema": 1, "cycleAnchor": 0,
        "quests": lua.table_from({}), "chars": lua.table_from({}),
        "turnins": lua.table_from({}), "manual": lua.table_from({}),
        "logs": lua.table_from({}),
    })


def pool(lua, rows):
    """rows: [(questID, money, items)]；mapID 固定 2007，name 自动生成 '任务<id>'。"""
    arr = lua.table_from({})
    for i, (qid, money, items) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        if items is not None:
            entry["items"] = lua.table_from(items)
        arr[i] = lua.table_from(entry)
    return arr


def pool_map(lua, rows):
    """rows: [(questID, mapID, money)]；money 为 None 表示奖励接口取不到（未判定）。"""
    arr = lua.table_from({})
    for i, (qid, map_id, money) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": map_id, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        arr[i] = lua.table_from(entry)
    return arr


def test_sig_stable_and_gold():
    """签名与物品顺序无关；有金币判金币任务，0 与 nil 都不判。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    a = str(L.MakeSig(123456, lua.table_from([7, 3, 9])))
    b = str(L.MakeSig(123456, lua.table_from([9, 7, 3])))
    if a != b:
        return fail("sig_stable_and_gold", f"物品顺序不应改变签名：{a} vs {b}")
    if not L.IsGoldSig(str(L.MakeSig(123456, lua.table_from({})))):
        return fail("sig_stable_and_gold", "含金币应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(0, lua.table_from([555])))):
        return fail("sig_stable_and_gold", "无金币只掉物品不应判金币任务")
    if L.IsGoldSig(str(L.MakeSig(None, lua.table_from({})))):
        return fail("sig_stable_and_gold", "奖励未知不应判金币任务")
    if str(L.MakeSig(1, lua.table_from({}))) == str(L.MakeSig(2, lua.table_from({}))):
        return fail("sig_stable_and_gold", "金币不同应产生不同签名")
    ok("sig_stable_and_gold")


def test_diff_added_and_gone():
    """首次抓池全部 added；下次池中消失的任务置 gone 并清 currentSig。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    r1 = L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 0, [111])]), 1000)
    if len(r1["added"]) != 2 or len(r1["gone"]) != 0:
        return fail("diff_added_and_gone", f"首轮 added={len(r1['added'])} gone={len(r1['gone'])}，期望 2/0")
    sig1 = str(db["quests"][1]["currentSig"])
    if not L.IsGoldSig(sig1):
        return fail("diff_added_and_gone", "任务 1 应判为金币任务")
    if db["quests"][1]["observations"][sig1]["gold"] is not True:
        return fail("diff_added_and_gone", "observation.gold 应为 true")
    r2 = L.DiffPool(db, pool(lua, [(1, 500000, None)]), 2000)
    if len(r2["gone"]) != 1 or int(r2["gone"][1]) != 2:
        return fail("diff_added_and_gone", f"任务 2 应消失，实际 gone 条数={len(r2['gone'])} 首项={r2['gone'][1]}")
    if db["quests"][2]["currentSig"] is not None:
        return fail("diff_added_and_gone", "消失任务应清掉 currentSig")
    if L.GetObs(db, 2) is not None:
        return fail("diff_added_and_gone", "不在池中不应有当前 observation")
    if L.GetObs(db, 1) is None:
        return fail("diff_added_and_gone", "在池任务应有 observation")
    if L.GetQuestName(db, 1) != "任务1":
        return fail("diff_added_and_gone", f"任务名应可读，实际 {L.GetQuestName(db, 1)}")
    ok("diff_added_and_gone")


def test_diff_renewed_new_round():
    """12 小时任务消失后又出现 = 新一轮：firstSeen 重置为本轮起点，goneSince 清空。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r3 = L.DiffPool(db, one, 3000)
    if len(r3["renewed"]) != 1 or int(r3["renewed"][1]) != 7:
        return fail("diff_renewed_new_round", f"应报 renewed 任务 7，实际 {len(r3['renewed'])}")
    obs = L.GetObs(db, 7)
    if obs is None:
        return fail("diff_renewed_new_round", "重新出现后应能取到 observation")
    if int(obs["firstSeen"]) != 3000:
        return fail("diff_renewed_new_round", f"新一轮 firstSeen 应为 3000，实际 {obs['firstSeen']}")
    if obs["goneSince"] is not None:
        return fail("diff_renewed_new_round", "重新出现后 goneSince 应清空")
    ok("diff_renewed_new_round")


def test_diff_sig_changed_modes():
    """同一任务奖励变化（狩猎/普通模式差异）：两条 observation 并存，金币标记各算各的。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(9, 400000, None)]), 1000)
    r = L.DiffPool(db, pool(lua, [(9, 0, [777])]), 2000)
    if len(r["sigChanged"]) != 1 or int(r["sigChanged"][1]) != 9:
        return fail("diff_sig_changed_modes", f"应只报 sigChanged，实际 gone={len(r['gone'])} added={len(r['added'])}")
    if int(L.CountObs(db, 9)) != 2:
        return fail("diff_sig_changed_modes", f"应有 2 条 observation，实际 {L.CountObs(db, 9)}")
    if L.IsGoldSig(str(db["quests"][9]["currentSig"])):
        return fail("diff_sig_changed_modes", "当前签名（无金币）不应判金币任务")
    ok("diff_sig_changed_modes")


def test_reward_change_within_a_round_keeps_the_turned_in_mark():
    """12.1.5 反馈（2026-10-25，用户原话）："切换狩猎模式的时候，有些任务本来不是奖励金币，
    现在奖励了。有些任务本来奖励金币现在不奖励了。但是现在切换模式，会导致本来做过这个任务的，
    现在标记成了没做过，这就不对了。这个任务并没有结束 并不是新一轮的刷新，完成状态不应该改变。"
    根因不在换模式，在**本轮起点存在哪**：firstSeen 是 observation 的字段，而 observation
    是按奖励签名分的 ⇒ 奖励一变就新建一份、起点跳到当下 ⇒ L.State 的两条判据同时失效
    （"亲眼记到的交任务"与"本角色快照"都要求时间戳 ≥ obs.firstSeen）⇒ 已交被改回未交。
    换模式本来就翻签名（上一条用例 diff_sig_changed_modes 钉的就是那件事），
    我上一轮写的"客户端确认无金币 ⇒ 金额 0"又多翻一次 ⇒ 起点必须与奖励解耦：
    只有真换轮才重置（消失过又回来 / 它自己的到期时刻已过），换奖励不许。
    走 Sync 真实事件入口（fire QUEST_COMPLETE + FinishWindow），不直调 Ledger。"""
    lua, gql, money = sync_env([(9, 30000000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 9)
    money["v"] = 31000000
    g.GQL_NOW = 5000
    gql.Sync.FinishWindow()
    if state1(gql.Ledger.State, g.GQLDB, key, 9) != "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "前置：刚交掉的这条当轮就该显示已交")

    def set_pool(rows):
        g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    one = lambda extra: lua.table_from([lua.table_from(
        {"questID": 9, "mapID": 2007, "name": "任务9"})] + list(extra))
    # 换狩猎模式：同一条不再给金币（签名翻面），任务既没结束也没换轮
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.HaveQuestRewardData = lambda qid: True
    set_pool(one([]))
    g.GQL_NOW = 6000
    gql.Sync.SnapshotNow()
    fs = int(gql.Ledger.GetObs(g.GQLDB, 9)["firstSeen"])
    if fs != 1000:
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    f"换奖励不是换轮，本轮起点要留住 1000，实际 {fs}")
    if state1(gql.Ledger.State, g.GQLDB, key, 9) != "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "换模式后做过的那格不许变回未交（用户报的就是这一格）")
    # 反向也要成立：真消失过再回来 = 新一轮，起点必须重置，
    # 否则 12 小时区"上一轮的已交冒充这一轮"（屠戮者高地秘密狩猎）会复发
    set_pool(lua.table_from([lua.table_from(
        {"questID": 99, "mapID": 2007, "name": "任务99"})]))
    g.GQL_NOW = 7000
    gql.Sync.SnapshotNow()
    set_pool(one([]))
    g.GQL_NOW = 8000
    gql.Sync.SnapshotNow()
    if int(gql.Ledger.GetObs(g.GQLDB, 9)["firstSeen"]) != 8000:
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "消失后重现=新一轮，起点仍要跳到 8000")
    if state1(gql.Ledger.State, g.GQLDB, key, 9) == "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "新一轮里上一轮的交任务不许再算已交")
    ok("reward_change_within_a_round_keeps_the_turned_in_mark")


def test_high_freq_learning():
    """消失时长 < 1.5 天的学到高频；消失 4 天的周常不学。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    day = 86400
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)      # 12 小时后消失
    L.DiffPool(db, one, 1000 + 86400)                # 又出现
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_learning", "消失 12 小时应学到高频标记")
    db2 = ledger_db(lua)
    L.DiffPool(db2, one, 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, one, 1000 + 5 * day)             # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_learning", "消失 4 天的周常不应标为高频")
    ok("high_freq_learning")


def test_diff_renewed_with_new_reward():
    """消失后换奖励回来（12 小时区每轮奖励可能不同）：算新一轮，不算 added/sigChanged。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r = L.DiffPool(db, pool(lua, [(7, 0, [888])]), 3000)
    if len(r["renewed"]) != 1 or int(r["renewed"][1]) != 7:
        return fail("diff_renewed_with_new_reward",
                    f"换奖励重现应报 renewed，实际 renewed={len(r['renewed'])} added={len(r['added'])} sigChanged={len(r['sigChanged'])}")
    if len(r["added"]) != 0 or len(r["sigChanged"]) != 0:
        return fail("diff_renewed_with_new_reward", "重现不应同时报成 added 或 sigChanged")
    if int(L.CountObs(db, 7)) != 2:
        return fail("diff_renewed_with_new_reward", f"两份奖励应各留一条 observation，实际 {L.CountObs(db, 7)}")
    if L.IsGoldSig(str(db["quests"][7]["currentSig"])):
        return fail("diff_renewed_with_new_reward", "当前这轮无金币，不应判金币任务")
    ok("diff_renewed_with_new_reward")


def test_high_freq_after_reward_rotation():
    """高频标记只看任务消失多久，不看奖励签名：12 小时区每轮奖励不同，绑签名就永远学不到。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    day = 86400
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db, pool(lua, []), 1000 + 43200)          # 消失 12 小时
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 1000 + 86400)  # 换奖励回来
    if not L.IsHighFreq(db, 7):
        return fail("high_freq_after_reward_rotation", "换奖励的 12 小时重现也应学到高频")
    db2 = ledger_db(lua)
    L.DiffPool(db2, pool(lua, [(7, 300000, None)]), 1000)
    L.DiffPool(db2, pool(lua, []), 1000 + day)
    L.DiffPool(db2, pool(lua, [(7, 0, [888])]), 1000 + 5 * day)  # 消失 4 天
    if L.IsHighFreq(db2, 7):
        return fail("high_freq_after_reward_rotation", "消失 4 天的周常不应标为高频")
    ok("high_freq_after_reward_rotation")


def test_diff_renewed_by_expired_round():
    """任务从没被"看见消失"过也要能开新一轮：判据是它自己上一轮的到期时刻已过。
    12.x 答得出数据的图源只有按图问的那条（partial=true），消失判定整段被关掉，
    于是 12 小时区的上一轮已交会一路冒充到当下（12.1.5 实测：屠戮者高地"秘密狩猎"
    全角色 ✓，新刷出来的那一轮再也记不上）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    day = 86400
    one = pool(lua, [(7, 300000, None)])

    def fresh():
        db = ledger_db(lua)
        L.DiffPool(db, one, 1000)
        db["quests"][7]["expiresAt"] = 1000 + 43200
        db["quests"][7]["expiresSrc"] = "接口倒计时"
        return db

    db = fresh()
    L.SetView(db, "B-永恒之井", 7, "done", 2000)
    L.OnTurnIn(db, "A-永恒之井", 7, 2000, 300000, str(db["quests"][7]["currentSig"]))
    if state1(L.State, db, "B-永恒之井", 7) != "done":
        return fail("diff_renewed_by_expired_round", "到期前本轮已交应算已交")
    # 没到期：还挂着，不该开新一轮
    r = L.DiffPool(db, one, 1000 + 43200 - day)
    if len(r["renewed"]) != 0:
        return fail("diff_renewed_by_expired_round", "没到期不该判新一轮")
    # 到期时刻已过（并留够分钟取整的余量）：池里这份就是新一轮
    r = L.DiffPool(db, one, 1000 + 43200 + 600)
    if len(r["renewed"]) != 1 or int(r["renewed"][1]) != 7:
        return fail("diff_renewed_by_expired_round",
                    f"上一轮到期后应报 renewed，实际 {len(r['renewed'])}")
    obs = L.GetObs(db, 7)
    if int(obs["firstSeen"]) != 1000 + 43200 + 600:
        return fail("diff_renewed_by_expired_round",
                    f"新一轮 firstSeen 应为本次快照时刻，实际 {obs['firstSeen']}")
    if state1(L.State, db, "B-永恒之井", 7) != "open":
        return fail("diff_renewed_by_expired_round",
                    f"旧轮的已交快照不该跨轮，实际 {L.State(db, 'B-永恒之井', 7)}")
    if state1(L.State, db, "A-永恒之井", 7) != "open":
        return fail("diff_renewed_by_expired_round",
                    f"旧轮的实测交任务不该跨轮，实际 {L.State(db, 'A-永恒之井', 7)}")
    # 幂等：倒计时这一轮没补上（expiresAt 仍停在过去）时不许每轮快照都重开一次，
    # 否则本轮刚交的会被自己踢出本轮
    r2 = L.DiffPool(db, one, 1000 + 43200 + 1200)
    if len(r2["renewed"]) != 0:
        return fail("diff_renewed_by_expired_round", "到期时刻没更新时不该再判一轮")
    if state1(L.State, db, "A-永恒之井", 7) != "open":
        return fail("diff_renewed_by_expired_round", "状态不该被重复判轮影响")
    ok("diff_renewed_by_expired_round")


def test_expired_round_with_a_new_reward_still_rolls():
    """继承上一轮起点只限"还在同一轮里换奖励"。到期换轮 + 同时换奖励（切狩猎模式
    撞上 12 小时区刷新）必须重新起算：12.x 按图问池时消失判定整段关闭，唯一的换轮
    凭据是接口倒计时，过期后旧 CD 那一笔已交绝不能冒充新一轮（否则 ✓ 永远翻不掉）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 300000, None)]), 1000)
    db["quests"][7]["expiresAt"] = 1000 + 43200
    db["quests"][7]["expiresSrc"] = "接口倒计时"
    L.OnTurnIn(db, "A-永恒之井", 7, 2000, 300000, str(db["quests"][7]["currentSig"]))
    if state1(L.State, db, "A-永恒之井", 7) != "done":
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "前置：到期前本轮已交应算已交")
    r = L.DiffPool(db, pool(lua, [(7, 0, [888])]), 1000 + 43200 + 600)
    obs = L.GetObs(db, 7)
    if int(obs["firstSeen"]) != 1000 + 43200 + 600:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"到期后换奖励是新一轮，起点该重新起算，实际 {obs['firstSeen']}")
    if len(r["sigChanged"]) != 1 or int(r["sigChanged"][1]) != 7:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "奖励变了要报 sigChanged")
    if len(r["renewed"]) != 1 or int(r["renewed"][1]) != 7:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "同一次快照里也换了轮，汇总要一起报 renewed，"
                    "只报 sigChanged 玩家看不懂 ✓ 为什么没了")
    if state1(L.State, db, "A-永恒之井", 7) != "open":
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"旧 CD 那一笔不许跨轮冒充，实际 {L.State(db, 'A-永恒之井', 7)}")
    ok("expired_round_with_a_new_reward_still_rolls")


def test_a_lost_check_is_never_bought_back_with_a_false_one():
    """老存档没有 roundAt，只能拿现存的起点补。补法只许认"当前这一轮那份记录的起点"，
    不许退回各份里最早的那一次 —— 用户 2026-10-25 实测（屠戮者高地 12 小时区）：
    "意外遭遇现在不是金币任务，但是都标记全部做完了，实际上上一轮做完了这一轮都没做过。
    上一轮的结束时间至少是这一轮结束时间的 12 个小时之前。"
    最早那份属于上一轮，按它补就把上一轮的已交补成了本轮的全员 ✓。
    少一个勾能忍，假勾是谎报（切模式弄丢的那几格本周不回来，下个 CD 自动干净）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    gold_sig = str(L.MakeSig(300000))
    free_sig = str(L.MakeSig(0, lua.table_from([888])))

    def legacy(anchor):
        db = ledger_db(lua)
        db["cycleAnchor"] = anchor
        db["quests"][7] = lua.table_from({
            "name": "任务7", "mapID": 2007, "currentSig": free_sig,
            "observations": lua.table_from({
                gold_sig: lua.table_from({"money": 300000, "gold": True,
                                          "firstSeen": 1000, "lastSeen": 5000}),
                free_sig: lua.table_from({"money": 0, "gold": False,
                                          "firstSeen": 6000, "lastSeen": 6000}),
            })})
        L.SetView(db, "A-永恒之井", 7, "done", 2000)
        return db

    db = legacy(500)
    if int(L.RoundAt(db, db["quests"][7])) != 6000:
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    f"起点该认当前这一轮那份的 6000，实际 {L.RoundAt(db, db['quests'][7])}")
    if state1(L.State, db, "A-永恒之井", 7) == "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "上一轮（更早那份记录）的已交不许冒充本轮，全员 ✓ 就是谎报")
    # 起点也不许早于本 CD：锚点在 7000，那份 6000 是上个 CD 看见的
    db = legacy(7000)
    if int(L.RoundAt(db, db["quests"][7])) != 7000:
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "补的起点不许早于本 CD 起点")
    if state1(L.State, db, "A-永恒之井", 7) == "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "上个 CD 的快照不该算本周")
    ok("a_lost_check_is_never_bought_back_with_a_false_one")


def test_a_new_round_never_carries_the_old_gold_number():
    """12.1.5 实测反馈（2026-10-25，用户原话）："屠戮者高地又刷新了新任务 神秘身影，
    之前是金币任务，现在不是了，但是还是出现在矩阵里"。
    病不在门槛，在**读闪延续没有轮次边界**：这一轮整条金额链一个字没答，
    DiffPool 按"预加载时差"把上一轮那份记录里的金币数搬进新 observation ⇒ 门槛照旧答"够"
    ⇒ 行一路赖着。换轮之后那个数属于上一轮那条实例，本轮没资格沿用（本轮真读不到
    就是读不到，宁可不列也不拿上一轮的数冒充）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)      # 上一轮：给金币（600 金，过门槛）
    L.DiffPool(db, pool(lua, []), 2000)                       # 整块板没看见它 = 结束
    L.DiffPool(db, pool(lua, [(7, None, None)]), 3000)        # 新刷的这条：金额问不出
    m = L.QuestMoney(db, 7)
    if m is not None:
        return fail("a_new_round_never_carries_the_old_gold_number",
                    f"新一轮不许沿用上一轮的金额，实际 {m}")
    if len(L.MatrixRows(db)) != 0:
        return fail("a_new_round_never_carries_the_old_gold_number",
                    "金额问不出的新实例不该占行（上一轮的 600 金不许冒充本轮）")
    ok("a_new_round_never_carries_the_old_gold_number")


def test_an_expired_round_never_carries_it_into_a_new_reward():
    """同一条危害的另一半：任务人还在池里、靠自己的到期时刻换的轮（12 小时区到点重刷）。
    这时 currentSig 还指着上一轮那份签名，新建的 observation 走的是"上一份有金额就延续"
    那条路 —— 没有轮次边界就把上一轮的数搬进新一轮。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)       # 本轮到期 44200
    if len(L.MatrixRows(db)) != 1:
        return fail("an_expired_round_never_carries_it_into_a_new_reward",
                    "前置：上一轮 600 金该占一行")
    L.DiffPool(db, pool(lua, [(7, None, None)]), 44400)   # 到期已过，换轮、奖励签名也是新的
    m = L.QuestMoney(db, 7)
    if m is not None:
        return fail("an_expired_round_never_carries_it_into_a_new_reward",
                    f"换了轮、金额问不出就是问不出，不许报 {m}")
    if len(L.MatrixRows(db)) != 0:
        return fail("an_expired_round_never_carries_it_into_a_new_reward",
                    "上一轮的数延续进新一轮就会继续占行")
    ok("an_expired_round_never_carries_it_into_a_new_reward")


def test_a_reused_record_loses_the_previous_rounds_number_at_the_roll():
    """同一条奖励签名会跨轮复用那份 observation（12 小时区"这副奖励隔一轮又出现"很常见）。
    换轮时那份记录里存的数字属于上一轮 ⇒ 必须清空重新读，否则 DiffPool 按"本轮读到过金币"
    拿它当本轮客户端的答案（上一条用例只管新建签名那一半，签名复用这一半它管不到）。
    换轮走的是到期时刻那条：用户 12.1.5 实测「神秘身影」在屠戮者高地 12 小时区，
    漏采那一次（partial）压根不会写 goneAt，能锚定新实例的只有这条任务自己的倒计时。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    sig_a = L.MakeSig(None, None)
    L.DiffPool(db, pool(lua, [(7, None, None)]), 1000)    # 签名 A：这一读金额问不出
    L.SetExpiry(db["quests"][7], 1000, 43200)             # 本轮到期 44200
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1100)  # 签名 B：奖励数据到了，带数
    L.DiffPool(db, pool(lua, [(7, None, None)]), 1200)    # 读闪回 A：本轮内延续，该有数
    if L.QuestMoney(db, 7) is None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：同一轮里金额读闪要延续，行不许闪")
    if db["quests"][7]["observations"][sig_a]["money"] is None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：A 那份记录里得真的存着本轮延续来的数，否则测不到复用")
    L.DiffPool(db, pool(lua, [(7, None, None)]), 44400)   # 到期已过 → 换轮，A 那份记录被复用
    if db["quests"][7]["observations"][sig_a]["money"] is not None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "换轮后复用记录里的旧数必须当场作废")
    m = L.QuestMoney(db, 7)
    if m is not None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    f"新一轮不许拿上一轮那份记录里的数占行，实际 {m}")
    if len(L.MatrixRows(db)) != 0:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "旧数没作废就会继续占行")
    ok("a_reused_record_loses_the_previous_rounds_number_at_the_roll")


def test_a_reused_sig_reads_the_gold_number_back_after_the_roll():
    """上一条的对偶：换轮把复用记录里的旧数清了，可**这一读客户端真答出了金币**
    （12 小时区里同一副奖励隔一轮重现，签名一模一样）⇒ 那份数必须当场写回去。
    少这一笔等于"清了旧的、新的又记不上"，金币任务换一轮就白掉一次行，
    一次性作废也就永远等不到"真给金币的自己长回来"（12.1.5 真存档回放 89347 实测掉过）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    sig = L.MakeSig(6000000, None)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)             # 本轮到期 44200
    if int(L.QuestMoney(db, 7)) != 6000000:
        return fail("a_reused_sig_reads_the_gold_number_back_after_the_roll", "前置：第一轮该读到金币")
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 44400)  # 到期已过 → 换轮，同一副签名重现
    if L.QuestMoney(db, 7) is None or int(L.QuestMoney(db, 7)) != 6000000:
        return fail("a_reused_sig_reads_the_gold_number_back_after_the_roll",
                    "这一读客户端答了 600 金，账里却是 %s" % L.QuestMoney(db, 7))
    if db["quests"][7]["observations"][sig]["gold"] is not True:
        return fail("a_reused_sig_reads_the_gold_number_back_after_the_roll",
                    "读回来的金币那份记录要标成金币任务，dump 的清单靠它")
    if len(L.MatrixRows(db)) != 1:
        return fail("a_reused_sig_reads_the_gold_number_back_after_the_roll",
                    "新一轮真读到金币的那条该占行")
    ok("a_reused_sig_reads_the_gold_number_back_after_the_roll")


def test_a_turned_in_row_drops_when_the_instance_rolls():
    """12.1.5 实测（2026-10-25，用户原话）："神秘身影 之前是金币任务，现在不是了，
    但是还是出现在矩阵里"，而 dump 的第二道门写着"本周有角色交过"。
    交过那道门本来是为松露大乱斗那种接口读不出金额的金币任务开的（不看金额，硬凭据），
    但它按 CD 算 ⇒ 12 小时区换了一条实例、上一轮的已交还替这一轮占行 ——
    而这一轮那一行的格子全是空的（✓ 早就按轮次归零了）。
    ⇒ 占行的"交过"必须跟 ✓ 同一个口径：本轮交过才算（用户 2026-10-25 定的收口）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)        # 本轮到期 44200
    L.OnTurnIn(db, "A-永恒之井", 7, 1500, 6000000, "6000000")
    # 同一条实例换成不给金币那一档：金额掉到门槛以下，靠"本轮交过"还得占行
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 2000)
    L.SetExpiry(db["quests"][7], 2000, 42200)       # 问出来还是 44200 ⇒ 同一条实例
    rows = L.MatrixRows(db, 2500)
    if len(rows) != 1:
        return fail("a_turned_in_row_drops_when_the_instance_rolls",
                    "本轮交过的那一行就算不给金币也该占行（松露大乱斗那道门）")
    # 换实例：到期时刻整段挪走 ⇒ 上一轮的已交不再替这一轮占行
    L.SetExpiry(db["quests"][7], 44400, 43200)       # 新一条，到期 87600
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 44500)
    if len(L.MatrixRows(db, 44500)) != 0:
        return fail("a_turned_in_row_drops_when_the_instance_rolls",
                    "换了实例，上一轮的已交不该再占行（这一轮谁都没做过）")
    ok("a_turned_in_row_drops_when_the_instance_rolls")


def test_a_weekly_turned_in_earlier_this_cd_still_occupies_its_row():
    """收口只许收 12 小时区那一侧：注册周常的"本轮"就是整个 CD（它不会中途换实例），
    周初交过、周末才被重新建出来的记录也得照旧占行 —— 松露大乱斗那种金额永远读不出的
    金币任务，唯一的凭据就是"有人交过"（12.1.5 反馈④）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 100
    db["quests"][98000] = lua.table_from({
        "name": "松露大乱斗", "regKey": "w:松露大乱斗", "mapID": 2405,
        "observations": lua.table_from({}), "lastSeenAt": 9000})
    db["turnins"]["A-永恒之井"] = lua.table_from({})
    db["turnins"]["A-永恒之井"][98000] = lua.table_from({})
    db["turnins"]["A-永恒之井"][98000][1] = lua.table_from(
        {"t": 200, "g": 9620000, "cycle": 100})
    if int(L.RoundAt(db, db["quests"][98000])) != 100:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "前置：没有轮次起点的老记录要按本 CD 起点补，实际 %s"
                    % L.RoundAt(db, db["quests"][98000]))
    if len(L.MatrixRows(db, 9000)) != 1:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "周初交过的注册周常不许被轮次收口弄掉（它的本轮=整个 CD）")
    ok("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row")


def test_the_poisoned_sticky_gold_is_cleared_once_at_sync():
    """代码修好了，**已经写进存档的那一份不会自愈**：粘滞只在换轮时作废，
    而用户不会为了等一次换轮再挂 12 小时假金币（12.1.5 复盘：改完代码"刷新矩阵没变化"，
    就是因为 15:02 那次同步写坏的 goldMoney 一直答到 03:00 换轮才消）。
    ⇒ 按标记清一次，之后正常同步重新读、真读到金币那一档自己长回来。

    形状照 89267 真存档抄（第二版用例，第一版只造了 goldMoney 没造那份被搬坏的
    observation ⇒ 用例绿着、游戏里 15:50 那次同步又把粘滞造回来了，用户第二次报"没有变化"）：
    上一轮真读到的 1075金39银、本轮被旧代码搬进 "-1" 那一份、客户端本轮明说 0。"""
    lua, gql, _ = sync_env([(7, None)])
    g = lua.globals()
    L = gql.Ledger
    db = g.GQLDB
    # 用户实机存档就是这一档：上一版迁移已经跑过（粘滞清过、标记写过 1），
    # 但 15:50 那次同步又把它造回来了 ⇒ 闸门只认"非空即跳过"的话这一版永远轮不到清来源
    db["stickyGoldReset"] = 1
    lua.execute("""GQLDB.quests[7] = {
        name = '任务7', mapID = 2007, roundAt = 4900, currentSig = '0',
        goldMoney = 10753956, goldRound = 4900,
        observations = {
            ['10753956'] = { money = 10753956, gold = true, firstSeen = 1000, lastSeen = 2000 },
            ['-1'] = { money = 10753956, gold = true, firstSeen = 4900, lastSeen = 4900 },
            ['0'] = { money = 0, gold = false, firstSeen = 4900, lastSeen = 4900 } } }""")
    def money():
        m = L.QuestMoney(db, 7)
        return -1 if m is None else int(m)
    def listed():
        return any(int(r.questID) == 7 for k in L.MatrixRows(db, 5000).keys()
                   for i in L.MatrixRows(db, 5000)[k]["rows"].keys())
    if money() != 10753956:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "前置：污染没造出来，实际 %d" % money())
    # 15:50 那一次的形状：金额链一个字没答（HaveQuestRewardData=false）⇒ 签名落到 '-1'。
    # 旧版迁移只清了粘滞，DiffPool 结尾照着 obs['-1'].money 那个搬来的数又写回 goldMoney
    g.HaveQuestRewardData = lambda qid: False
    g.GQL_NOW = 5000
    # Task 9：清旧金币凭据这一棒从"每次快照都判"改成了"一次性升级"。
    # 触发点换成 Config.Migrate（档位退到 v2 前让它重跑），清理结果的断言一条不放松。
    db["schema"] = 1
    lua.execute("GQL.Config.Migrate(GQL.Config.DB())")
    gql.Sync.SnapshotNow()
    if money() == 10753956:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "被污染的粘滞该在一次性升级时清掉，实际还是 %d" % money())
    if int(db.quests[7].observations["10753956"].money) != 10753956:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "客户端真答过的正数签名不许当成污染清掉")
    # 关键的一半：下一次同步不许让它自己长回来（旧版迁移只清粘滞，就是死在这一步）
    g.HaveQuestRewardData = lambda qid: True      # 奖励数据在本地却问不出金币 = 明说不给 ⇒ 写 0
    g.GQL_NOW = 5010
    gql.Sync.SnapshotNow()
    if money() != 0:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "本轮客户端只答过 0（不给金币），迁移之后不许再报 %d" % money())
    if listed():
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "被污染的金额没清干净，这一行还占着")
    # 只清一次：之后本轮真读到金币的，得照常算钱、照常占行
    g.GetQuestLogRewardMoney = lambda qid: 6000000
    g.GQL_NOW = 5020
    gql.Sync.SnapshotNow()
    if money() != 6000000:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "迁移只许跑一次，之后本轮真读到的金币得留着，实际 %d" % money())
    # 搬 0 进来不许标成"给金币"（/gql dump 的金币任务清单读 obs.gold）
    if db.quests[7].observations["-1"].gold is not None:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "延续过来的 0 不许标 gold=true")
    if int(db["stickyGoldReset"]) != 2:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "标记要写到 2（1 这一档只清了粘滞，见 Ledger.ResetStickyGoldOnce）")
    ok("the_poisoned_sticky_gold_is_cleared_once_at_sync")


def test_a_carried_zero_never_marks_the_quest_as_gold():
    """新建一份 observation 时金额是从上一份延续过来的（读闪保护），延续过来的可能是 **0**
    = 客户端明说"这一档不给金币"。那份记录一旦标成 gold，/gql dump 的金币任务清单
    （Ledger.GoldQuestIDs 直接读 obs.gold）就凭空多报一条 —— 多报比少报严重。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 0, None)]), 1000)        # 客户端答了"不给金币"
    L.DiffPool(db, pool(lua, [(7, None, [888])]), 2000)    # 同一轮换了一副道具 ⇒ 新签名
    # 拿 obs.gold 判（GoldQuestIDs 返回的是 Lua 表，空的在 Python 里也当真值）
    if db.quests[7].observations["-1|888"].gold:
        return fail("a_carried_zero_never_marks_the_quest_as_gold",
                    "延续过来的 0 被算成金币任务了")
    if int(L.QuestMoney(db, 7)) != 0:
        return fail("a_carried_zero_never_marks_the_quest_as_gold",
                    "读闪延续本身还在，金额该照旧是 0，实际 %s" % L.QuestMoney(db, 7))
    ok("a_carried_zero_never_marks_the_quest_as_gold")


def test_dual_mode_flip_keeps_the_row_and_the_check():
    """用户 2026-10-25 定的口径（原话）："双模式的任务，不是就不进，但是当用户切换模式
    再打开矩阵时，这个时候读取的应该就是金币任务 就应该进了。而之前…之前是金币，
    那么也不应该移除（结束时间没变）。"
    ⇒ 同一条实例（到期时刻没变、也没被板面漏掉）里任意一档读到过金币就一直占行；
    完成状态更不许因为换奖励而改动。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 30000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)       # 这一轮到期 = 44200
    L.SetView(db, "A-永恒之井", 7, "done", 2000)
    if len(L.MatrixRows(db)) != 1:
        return fail("dual_mode_flip_keeps_the_row_and_the_check", "前置：金币那一档该占一行")

    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 5000)
    L.SetExpiry(db["quests"][7], 5000, 39200)       # 问出来还是 44200 ⇒ 同一条实例
    rows = L.MatrixRows(db)
    if len(rows) != 1 or int(rows[1]["rows"][1]["questID"]) != 7:
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    "切到不给金币那一档不许把行弄掉（结束时间没变就是同一条）")
    if int(L.QuestMoney(db, 7)) != 30000000:
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    f"该报本轮另一档读到的金币，实际 {L.QuestMoney(db, 7)}")
    if state1(L.State, db, "A-永恒之井", 7) != "done":
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    "换奖励不许动完成状态，实际 " + state1(L.State, db, "A-永恒之井", 7))
    ok("dual_mode_flip_keeps_the_row_and_the_check")


def test_a_new_instance_rejudges_gold_and_clears_the_check():
    """到期时刻变了 = 板面上换成了另一条任务：金币判定重新读、上一轮的 ✓ 也不再算。
    这一条才是 12.1.5"意外遭遇已经不是金币任务、还挂着上个 CD 的 1064 金"该走的退出路
    （不是"读到非金币就当场删行"——那会把双模式那条误删，见上一条用例）。
    倒计时同一条实例只会有分钟取整的几十秒抖动，4500 秒的差只能是换了一条。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 30000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)       # 到期 44200
    L.SetView(db, "A-永恒之井", 7, "done", 2000)
    if state1(L.State, db, "A-永恒之井", 7) != "done":
        return fail("a_new_instance_rejudges_gold_and_clears_the_check", "前置：本轮已交")

    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 5000)
    L.SetExpiry(db["quests"][7], 5000, 43700)       # 到期 48700 ⇒ 新的一条
    if state1(L.State, db, "A-永恒之井", 7) == "done":
        return fail("a_new_instance_rejudges_gold_and_clears_the_check",
                    "换了实例，上一轮的已交不该再算，实际 " + state1(L.State, db, "A-永恒之井", 7))
    if len(L.MatrixRows(db)) != 0:
        return fail("a_new_instance_rejudges_gold_and_clears_the_check",
                    "新实例这一读不给金币，不该占行（上个 CD 的金币不许延续过来）")
    ok("a_new_instance_rejudges_gold_and_clears_the_check")


def test_diff_no_renewal_without_real_countdown():
    """只有"拿着这条任务的 questID 问出来的倒计时"才配当轮次边界：
    排程值是我们编的 24 小时、周/每日重置是 CD 的尾巴，都不是这一轮的寿命。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    one = pool(lua, [(7, 300000, None)])
    for src in ("排程值", "下次周重置", "下次每日重置", None):
        db = ledger_db(lua)
        L.DiffPool(db, one, 1000)
        db["quests"][7]["expiresAt"] = 5000
        db["quests"][7]["expiresSrc"] = src
        r = L.DiffPool(db, one, 999999)
        if len(r["renewed"]) != 0:
            return fail("diff_no_renewal_without_real_countdown",
                        f"来源={src} 不是这条任务自己的倒计时，不该判新一轮")
    ok("diff_no_renewal_without_real_countdown")


def test_turnin_state_and_week_stats():
    """交任务入日志；本轮内显示已交；周统计只算本 CD。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 5000
    L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 300000, None)]), 6000)
    sig = str(db["quests"][1]["currentSig"])
    L.OnTurnIn(db, "A-永恒之井", 1, 6100, 500000, sig)
    if state1(L.State, db, "A-永恒之井", 1) != "done":
        return fail("turnin_state_and_week_stats", "本轮内已交应显示 done")
    if state1(L.State, db, "A-永恒之井", 2) != "open":
        return fail("turnin_state_and_week_stats", "池内未做应显示 open")
    count, gold = L.WeekStats(db, "A-永恒之井", 5000)
    if int(count) != 1 or int(gold) != 500000:
        return fail("turnin_state_and_week_stats", f"本周应 1 条/500000，实际 {count}/{gold}")
    count0, _ = L.WeekStats(db, "A-永恒之井", 7000)
    if int(count0) != 0:
        return fail("turnin_state_and_week_stats", "上个 CD 的记录不应计入本周")
    ok("turnin_state_and_week_stats")


def test_turnin_not_counted_across_rounds():
    """任务消失又出现 = 新一轮，上一轮的已交不再计入本轮。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.OnTurnIn(db, "A-永恒之井", 7, 1100, 300000, str(db["quests"][7]["currentSig"]))
    if state1(L.State, db, "A-永恒之井", 7) != "done":
        return fail("turnin_not_counted_across_rounds", "本轮做完应 done")
    L.DiffPool(db, pool(lua, []), 2000)
    if state1(L.State, db, "A-永恒之井", 7) != "ended":
        return fail("turnin_not_counted_across_rounds", "消失后应显示本轮已结束")
    L.DiffPool(db, one, 3000)
    st = state1(L.State, db, "A-永恒之井", 7)
    if st != "open":
        return fail("turnin_not_counted_across_rounds", f"新一轮应回到待做，实际 {st}")
    count, _ = L.WeekStats(db, "A-永恒之井", 0)
    if int(count) != 1:
        return fail("turnin_not_counted_across_rounds", "金币历史仍留在库里（统计口径不变，本轮状态另算）")
    ok("turnin_not_counted_across_rounds")


def test_manual_beats_auto_and_survives_sync():
    """手动补录优先于自动快照；后续 SetView 不得覆盖它；撤销能还原。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(3, 200000, None)]), 1000)
    L.SetView(db, "B-永恒之井", 3, "open", 1000)
    # 补录的时刻落在**本轮之内**（这一条实例 1000 起）：轮次门见 test_a_hand_mark
    # _expires_when_the_instance_rolls，本轮之外的那一笔本来就不该再打勾。
    old = L.SetManual(db, "B-永恒之井", 3, "done", 1100, 150000)
    if state1(L.State, db, "B-永恒之井", 3) != "done":
        return fail("manual_beats_auto_and_survives_sync", "手动已交应覆盖自动 open")
    L.SetView(db, "B-永恒之井", 3, "open", 2000)
    if state1(L.State, db, "B-永恒之井", 3) != "done":
        return fail("manual_beats_auto_and_survives_sync", "登录同步不应覆盖手动记录")
    count, gold = L.WeekStats(db, "B-永恒之井", 0)
    if int(count) != 1 or int(gold) != 150000:
        return fail("manual_beats_auto_and_survives_sync", f"手动补录应计入统计，实际 {count}/{gold}")
    L.UndoApply(db, {"kind": "manual", "charKey": "B-永恒之井", "questID": 3, "old": old})
    if state1(L.State, db, "B-永恒之井", 3) != "open":
        return fail("manual_beats_auto_and_survives_sync", "撤销应回到自动判定的 open")
    ok("manual_beats_auto_and_survives_sync")


def test_manual_not_double_counted_with_turnin():
    """同一任务既有自动交任务记录又有手动已交：完成数只算一次。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(8, 250000, None)]), 1000)
    L.OnTurnIn(db, "E-永恒之井", 8, 1100, 250000, str(db["quests"][8]["currentSig"]))
    L.SetManual(db, "E-永恒之井", 8, "done", 1200, 250000)
    count, gold = L.WeekStats(db, "E-永恒之井", 0)
    if int(count) != 1 or int(gold) != 250000:
        return fail("manual_not_double_counted_with_turnin", f"不应重复计数，实际 {count}/{gold}")
    ok("manual_not_double_counted_with_turnin")


def test_gold_unmeasured_counts_not_sum():
    """钱差测不到（g=nil）：完成数照计，金币总额报未知（-1），不报 0。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(4, 100000, None)]), 1000)
    L.OnTurnIn(db, "C-永恒之井", 4, 1100, None, str(db["quests"][4]["currentSig"]))
    count, gold = L.WeekStats(db, "C-永恒之井", 0)
    if int(count) != 1:
        return fail("gold_unmeasured_counts_not_sum", f"count 应为 1，实际 {count}")
    if int(gold) != -1:
        return fail("gold_unmeasured_counts_not_sum", f"无实测值 gold 应为 -1，实际 {gold}")
    ok("gold_unmeasured_counts_not_sum")


def test_mark_gold_by_observation():
    """实测到账金币把未判定任务转正；手动锁定后拒绝自动改写。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(5, None, None)]), 1000)   # 奖励查不到 → 未判定
    sig = str(db["quests"][5]["currentSig"])
    if len(L.GoldQuestIDs(db)) != 0:
        return fail("mark_gold_by_observation", "未判定任务不应进金币列表")
    L.OnTurnIn(db, "F-永恒之井", 5, 1100, 300000, sig)
    if len(L.GoldQuestIDs(db)) != 1:
        return fail("mark_gold_by_observation", "实测到金币后应自动转正为金币任务")
    L.SetGoldLock(db, 5, True)
    db["quests"][5]["observations"][sig]["gold"] = False
    if L.MarkGoldByObservation(db, 5, sig) is not False:
        return fail("mark_gold_by_observation", "goldLock 时应拒绝自动改写")
    ok("mark_gold_by_observation")


def test_prune_bounds_size():
    """turnins 每任务上限 8 条；三个 CD 未出现的任务整条清出，金币历史保留。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(6, 100000, None)]), 1000)
    sig = str(db["quests"][6]["currentSig"])
    for i in range(1, 12):
        L.OnTurnIn(db, "D-永恒之井", 6, 1000 + i, 100000, sig)
    rows = db["turnins"]["D-永恒之井"][6]
    if len(rows) != 8:
        return fail("prune_bounds_size", f"turnins 应截到 8 条，实际 {len(rows)}")
    if int(rows[len(rows)]["t"]) != 1011:
        return fail("prune_bounds_size", "应丢最旧留最新")
    L.DiffPool(db, pool(lua, []), 1000)
    L.Prune(db, 1000 + 25 * 86400)
    if db["quests"][6] is not None:
        return fail("prune_bounds_size", "连续 3 个 CD 未出现应清出账本")
    if db["turnins"]["D-永恒之井"][6] is None:
        return fail("prune_bounds_size", "清理任务定义不应删掉金币历史")
    ok("prune_bounds_size")


def test_state_uses_own_snapshot_across_characters():
    """池是按角色各自的：别的角色那边任务消失，不该改写本角色自己的快照结论。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(1, 500000, None)]), 1000)
    L.SetView(db, "B-永恒之井", 1, "progress", 1500)
    L.DiffPool(db, pool(lua, []), 2000)              # 当前角色的池里再也没有这个任务
    if state1(L.State, db, "B-永恒之井", 1) != "progress":
        return fail("state_uses_own_snapshot_across_characters",
                    f"B 自己说过进行中，不该被别人的池判成已结束，实际 {L.State(db, 'B-永恒之井', 1)}")
    if state1(L.State, db, "A-永恒之井", 1) != "ended":
        return fail("state_uses_own_snapshot_across_characters", "没有快照的角色仍应显示本轮已结束")
    ok("state_uses_own_snapshot_across_characters")


def test_stale_snapshot_not_trusted_in_new_round():
    """新一轮开始后，上一轮遗留的"已交"快照不得继续冒充本轮。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    L.SetView(db, "B-永恒之井", 7, "done", 1500)
    L.DiffPool(db, pool(lua, []), 2000)
    L.DiffPool(db, one, 3000)                        # 同一签名重现 = 新一轮，firstSeen=3000
    if state1(L.State, db, "B-永恒之井", 7) != "open":
        return fail("stale_snapshot_not_trusted_in_new_round",
                    f"旧快照不应跨轮，实际 {L.State(db, 'B-永恒之井', 7)}")
    ok("stale_snapshot_not_trusted_in_new_round")


def test_api_degrades_when_missing():
    """接口全缺失：GetPool 返回 nil + 原因，不抛错。"""
    _, gql = load_lua("Config", "Ledger", "Api")
    rows, err, meta = gql.Api.GetPool()
    if rows is not None:
        return fail("api_degrades_when_missing", "无 API 时不应返回池数据")
    if err is None:
        return fail("api_degrades_when_missing", "必须报告降级原因")
    ok("api_degrades_when_missing")


def test_api_empty_pool_is_data_not_failure():
    """候选接口都调用成功、池真的空了：要返回空表当数据，不能报"不可用"。
    否则全清完世界任务后账本会永久冻结，"全部消失"这个转移永远测不出来。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})
    rows, err, meta = gql.Api.GetPool()
    if rows is None:
        return fail("api_empty_pool_is_data_not_failure", f"空池应是数据，实际 err={err}")
    if len(rows) != 0:
        return fail("api_empty_pool_is_data_not_failure", f"应为 0 条，实际 {len(rows)}")
    ok("api_empty_pool_is_data_not_failure")


def test_api_state_unknown_when_missing():
    """完成与接取接口都缺失：QuestState 返回 unknown，绝不返回 open。"""
    _, gql = load_lua("Config", "Ledger", "Api")
    state, err = gql.Api.QuestState(123)
    if str(state) != "unknown":
        return fail("api_state_unknown_when_missing", f"应返回 unknown，实际 {state}")
    if err is None:
        return fail("api_state_unknown_when_missing", "应带降级原因")
    ok("api_state_unknown_when_missing")


def test_getpool_uses_task_quest_map_source():
    """12.x 全量接口全灭时走按图源：C_TaskQuest.GetQuestsForPlayerByMapID 带 mapID 取数，
    普通任务（QuestUtils 判否）必须被筛掉，meta 要标 partial=true ——
    用户实测地图上有彩色世界任务环，说明这条源一定存在，只是入口不叫 C_WorldQuests。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None          # 老全量源在 12.1.5 已移除
    g.GetWorldQuests = None
    g.C_Map = lua.table_from({
        "GetBestMapForUnit": lambda unit: 2007,
        "GetMapInfo": lambda mid: lua.table_from({"parentMapID": 947}),
    })
    g.QuestUtils_IsQuestWorldQuest = lambda qid: qid >= 100
    rows = lua.table_from([
        lua.table_from({"questID": 101, "mapID": 2007, "name": "虚空突袭"}),
        lua.table_from({"questID": 3, "mapID": 2007, "name": "普通任务不许进账本"}),
    ])
    g.C_TaskQuest = lua.table_from({"GetQuestsForPlayerByMapID": lambda mid: rows})
    g.C_QuestLog = None
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_uses_task_quest_map_source", f"按图源应可用，err={err}")
    if len(pool) != 1 or int(pool[1]["questID"]) != 101:
        return fail("getpool_uses_task_quest_map_source",
                    f"普通任务没被筛掉或世界任务丢了：{len(pool)} 条")
    if meta["partial"] is not True:
        return fail("getpool_uses_task_quest_map_source", f"按图源必须标 partial，meta={meta}")
    if "GetQuestsForPlayerByMapID" not in str(meta["source"]):
        return fail("getpool_uses_task_quest_map_source", f"meta 应记来源，meta={meta}")
    ok("getpool_uses_task_quest_map_source")


def test_getpool_refuses_unclassifiable_map_rows():
    """世界任务判别接口缺失/报错时必须放弃按图源（返回 nil）：
    把普通任务当世界任务灌进账本，比没有数据糟糕得多。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    g.QuestUtils_IsQuestWorldQuest = None
    rows = lua.table_from([lua.table_from({"questID": 101, "mapID": 2007})])
    g.C_TaskQuest = lua.table_from({"GetQuestsForPlayerByMapID": lambda mid: rows})
    g.C_QuestLog = None
    pool, err, meta = gql.Api.GetPool()
    if pool is not None:
        return fail("getpool_refuses_unclassifiable_map_rows",
                    f"判别接口缺失应放弃该源，实际收到 {len(pool)} 条")
    if "无法判别" not in str(gql.Api.LastDegrade()):
        return fail("getpool_refuses_unclassifiable_map_rows",
                    f"降级原因应写明无法判别：{gql.Api.LastDegrade()}")
    ok("getpool_refuses_unclassifiable_map_rows")


def test_getpool_fills_title_and_map_for_numeric_rows():
    """12.1.5 实测 GetQuestsOnMap 只回一串纯 questID：名字与地图要拿 questID 单独补查
    （C_QuestLog.GetTitleForQuestID / 地图候选链），否则矩阵全是
    "92120. 92120" 根本看不出是哪个任务。补查不到就留 nil，不许编名字。
    地图候选只剩 GetQuestInfo 与 GetQuestZoneID：GetQuestLocation 实测只要坐标
    （(x, y) 两个返回值，第三个从不存在），拿它当地图就是拿坐标当图号（12.1.5 复盘）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({92120, 91460}),
        "GetQuestZoneID": lambda qid: 2373 if int(qid) == 92120 else None,
        # 探针实测只回 x, y：这里给它回个像图号的第三个值，看生产链还会不会去取
        "GetQuestLocation": lambda qid: (None, None, 1859) if int(qid) == 91460 else None,
    })
    g.C_QuestLog = lua.table_from({
        "GetTitleForQuestID": lambda qid: "突袭！永歌森林" if int(qid) == 92120 else None,
    })
    g.GetQuestLogRewardMoney = lambda qid: 154800 if int(qid) == 92120 else 0
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_fills_title_and_map_for_numeric_rows", f"数字行应可用，err={err}")
    by_id = {}
    for i in range(1, len(pool) + 1):
        by_id[int(pool[i]["questID"])] = pool[i]
    row = by_id.get(92120)
    if row is None:
        return fail("getpool_fills_title_and_map_for_numeric_rows", "应有任务 92120")
    if str(row["name"]) != "突袭！永歌森林" or int(row["mapID"]) != 2373:
        return fail("getpool_fills_title_and_map_for_numeric_rows",
                    f"名字/地图应补查到，实际 {row['name']}/{row['mapID']}")
    if int(row["money"]) != 154800:
        return fail("getpool_fills_title_and_map_for_numeric_rows",
                    f"金额应由 GetQuestLogRewardMoney 补到，实际 {row['money']}")
    row = by_id.get(91460)
    if row["name"] is not None:
        return fail("getpool_fills_title_and_map_for_numeric_rows",
                    "名字补查不到就该留 nil，不许编")
    # 地图问不出来时拿"这次问的是哪张图"兜底：逐图问出来的行不会跑到别的图上去，
    # 留 nil 会让这些任务整堆掉进矩阵底部的"未知地图"分组（用户实测的噪音源之一）
    if int(row["mapID"]) != 2007:
        return fail("getpool_fills_title_and_map_for_numeric_rows",
                    f"补查不到地图该用询问的 2007 兜底，实际 {row['mapID']}")
    ok("getpool_fills_title_and_map_for_numeric_rows")


def test_getpool_enumerates_continent_maps():
    """世界任务按大陆一片分布：脚下是区域 2007，同大陆其它区域（2050）的任务不能漏
    （用户实测虚影风暴缺了同大陆其它金币任务就是只问了脚下那张图）。必须递归收集
    所属大陆的全部子地图，逐图问一遍，按 questID 去重。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None

    def get_map_info(mid):
        mid = int(mid)
        if mid == 2007:
            return lua.table_from({"parentMapID": 947})
        if mid == 947:
            return lua.table_from({"parentMapID": None})  # 大陆顶层，没有父图
        if mid == 2050:
            return lua.table_from({"parentMapID": 947})
        return None

    def get_children(mid):
        if int(mid) == 947:
            return lua.table_from([lua.table_from({"mapID": 2007}),
                                   lua.table_from({"mapID": 2050})])
        return lua.table_from({})

    seen = []

    def quests_on_map(mid):
        seen.append(int(mid))
        if int(mid) == 2050:
            return lua.table_from([lua.table_from({"questID": 555, "mapID": 2050})])
        return lua.table_from({})

    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda u: 2007,
                              "GetMapInfo": get_map_info,
                              "GetMapChildrenInfo": get_children})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": quests_on_map})
    g.C_QuestLog = None
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_enumerates_continent_maps", f"应可用，err={err}")
    by_id = {}
    for i in range(1, len(pool) + 1):
        by_id[int(pool[i]["questID"])] = pool[i]
    if 555 not in by_id:
        return fail("getpool_enumerates_continent_maps",
                    f"同大陆其它区域任务 555 应被捞到，seen={seen}")
    if 2050 not in seen:
        return fail("getpool_enumerates_continent_maps",
                    f"应查过区域 2050 的图，seen={seen}")
    ok("getpool_enumerates_continent_maps")


def test_quest_reward_money_sources():
    """奖励金额接口：探针实测 GetQuestLogRewardMoney(questID) 直接返回 copper（非金币任务为 0）。
    C_QuestLog.GetQuestRewardMoney 在 12.1.5 不存在，之前误判的入口已弃用。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 250000
    if int(gql.Api.QuestRewardMoney(11)) != 250000:
        return fail("quest_reward_money_sources", "GetQuestLogRewardMoney 应直接返回金币数(copper)")
    g.GetQuestLogRewardMoney = lambda qid: 0
    if gql.Api.QuestRewardMoney(11) is not None:
        return fail("quest_reward_money_sources", "非金币任务(返回0)应判为 nil，进未判定组")
    ok("quest_reward_money_sources")


def test_quest_time_left():
    """世界任务剩余时间（秒）：探针枚举 C_TaskQuest 发现 GetQuestTimeLeftSeconds /
    GetQuestTimeLeftMinutes，"GetQuestTimeLeft" 反而是 MISSING 的。Minutes 兜底需 ×60。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 43200})
    if int(gql.Api.QuestTimeLeft(11)) != 43200:
        return fail("quest_time_left", f"应返回剩余秒数，实际 {gql.Api.QuestTimeLeft(11)}")
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftMinutes": lambda qid: 720})
    if int(gql.Api.QuestTimeLeft(11)) != 43200:
        return fail("quest_time_left", "Minutes 源应换算成秒(720*60=43200)")
    ok("quest_time_left")


def test_snapshot_partial_pool_does_not_mark_gone():
    """部分池（只看到当前地图）绝不能当全量 diff：先在全量池记到任务 21，
    再切到只看得见任务 22 的按图源 —— 21 没被"没看见"误判成消失/结束，22 照常入账。
    用户换地图就会触发这种"看到的片变了"的场景，判错会清掉整本账。"""
    lua, gql, _ = sync_env([(21, 400000)])
    g = lua.globals()
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_partial_pool_does_not_mark_gone", f"第一次快照应成功：{res}")

    g.C_WorldQuests = None
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    rows = lua.table_from([lua.table_from({"questID": 22, "mapID": 2007, "name": "任务22",
                                           "money": 500})])
    g.C_TaskQuest = lua.table_from({"GetQuestsForPlayerByMapID": lambda mid: rows})
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_partial_pool_does_not_mark_gone", f"部分池快照应成功：{res}")
    if res["partial"] is not True:
        return fail("snapshot_partial_pool_does_not_mark_gone", f"应标部分池：{res}")
    db = g.GQLDB
    key = str(gql.Sync.CurrentCharKey())
    if db["quests"][21] is None or db["quests"][21]["currentSig"] is None:
        return fail("snapshot_partial_pool_does_not_mark_gone",
                    f"任务 21 被误标消失：{db['quests'][21]}")
    if str(db["chars"][key]["views"][21]["s"]) == "ended":
        return fail("snapshot_partial_pool_does_not_mark_gone", "任务 21 视图被误标结束")
    if db["quests"][22] is None:
        return fail("snapshot_partial_pool_does_not_mark_gone", "任务 22 应照常入账")
    ok("snapshot_partial_pool_does_not_mark_gone")


def test_partial_pool_renews_round_at_expiry():
    """走完整同步链路验 12 小时区换轮：12.x 只有按图源答得出数据（partial=true），
    插件永远看不见"任务从池里消失"，屠戮者高地"秘密狩猎"上一轮的已交就这么一路冒充到
    新一轮（12.1.5 实测反馈：全角色 ✓，新刷出来的那个再也记不上）。
    到期时刻已过而池里还有它 = 新一轮：本角色实测到的交任务、别的角色留下的旧快照，
    都不许再压着这一格。"""
    lua, gql, _ = sync_env([(71, 300000)])
    g = lua.globals()
    g.C_WorldQuests = None
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_TaskQuest = lua.table_from({
        "GetQuestsForPlayerByMapID": lambda mid: lua.table_from([
            lua.table_from({"questID": 71, "mapID": 2007, "name": "秘密狩猎",
                            "money": 300000})]),
        "GetQuestTimeLeftSeconds": lambda qid: 43200,
    })
    g.GQL_NOW = 1000
    res = gql.Sync.SnapshotNow()
    if res is None or res["partial"] is not True:
        return fail("partial_pool_renews_round_at_expiry", f"应走部分池：{res}")
    db = g.GQLDB
    if int(db["quests"][71]["expiresAt"]) != 44200:
        return fail("partial_pool_renews_round_at_expiry",
                    f"到期时刻应为 44200，实际 {db['quests'][71]['expiresAt']}")

    key = str(gql.Sync.CurrentCharKey())
    sig = str(db["quests"][71]["currentSig"])
    for who in (key, "B-永恒之井"):
        gql.Ledger.OnTurnIn(db, who, 71, 2000, 300000, sig)
        gql.Ledger.SetView(db, who, 71, "done", 2000)
    if state1(gql.Ledger.State, db, key, 71) != "done":
        return fail("partial_pool_renews_round_at_expiry", "本轮内交过任务应显示已交")

    g.GQL_NOW = 44800                       # 上一轮 44200 到期，池里这份是刷出来的新一轮
    res = gql.Sync.SnapshotNow()
    if len(res["renewed"]) != 1 or int(res["renewed"][1]) != 71:
        return fail("partial_pool_renews_round_at_expiry",
                    f"到期后池里还有它应报重现，实际 renewed={len(res['renewed'])}")
    for who in (key, "B-永恒之井"):
        if state1(gql.Ledger.State, db, who, 71) != "open":
            return fail("partial_pool_renews_round_at_expiry",
                        f"新一轮该重开，{who} 实际 {state1(gql.Ledger.State, db, who, 71)}")
    # 金币流水按 CD 记，不许因为换了轮就凭空少一笔
    if int(gql.Ledger.WeekStats(db, key, 0)[0]) != 1:
        return fail("partial_pool_renews_round_at_expiry",
                    f"上一轮那笔交任务仍要计入本周，实际 {gql.Ledger.WeekStats(db, key, 0)[0]}")
    ok("partial_pool_renews_round_at_expiry")


def test_api_normalizes_known_shape():
    """模拟 WorldQuestInfo 形状：字段名差异要归一，奖励接口缺失要留 nil。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    # 用 list 而不是 set 字面量：lua.table_from(set) 的迭代顺序按 Python 对象哈希走，
    # 会随机交换两行，rows[1]/rows[2] 的断言就变成不稳定测试。
    g.C_WorldQuests = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from([
            lua.table_from({"questID": 11, "mapID": 2007, "name": "补给车队"}),
            lua.table_from({"questID": 12, "zoneID": 2008, "title": "虚空突袭"}),
        ])
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid == 11,
        "IsQuestCompleted": lambda qid: False,
    })
    rows, err, meta = gql.Api.GetPool()
    if rows is None:
        return fail("api_normalizes_known_shape", f"应取到池数据，err={err}")
    if len(rows) != 2:
        return fail("api_normalizes_known_shape", f"应有 2 条，实际 {len(rows)}")
    if int(rows[1]["questID"]) != 11 or int(rows[1]["mapID"]) != 2007:
        return fail("api_normalizes_known_shape", f"条目 1 归一错误：{rows[1]['questID']}/{rows[1]['mapID']}")
    if str(rows[1]["mapSrc"] or "") != "池行自带":
        return fail("api_normalizes_known_shape",
                    f"行自己报的地图要标成池行自带，实际 {rows[1]['mapSrc']}")
    if int(rows[2]["questID"]) != 12 or int(rows[2]["mapID"]) != 2008:
        return fail("api_normalizes_known_shape", f"zoneID 应可当 mapID，实际 {rows[2]['mapID']}")
    if str(rows[2]["name"]) != "虚空突袭":
        return fail("api_normalizes_known_shape", "title 应可当 name")
    if rows[1]["money"] is not None:
        return fail("api_normalizes_known_shape", "奖励接口缺失时 money 必须是 nil（未判定），不能当 0")
    started, _ = gql.Api.QuestState(11)
    if str(started) != "progress":
        return fail("api_normalizes_known_shape", f"IsQuestStarted 为真应得 progress，实际 {started}")
    done, _ = gql.Api.QuestState(999)
    if str(done) != "open":
        return fail("api_normalizes_known_shape", f"两个接口都为假应得 open，实际 {done}")
    ok("api_normalizes_known_shape")


def test_probe_report_and_command_registered():
    """探测报告列出每个候选的可用性；Probe/LogWindow 应注册 /gql 子命令。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", "LogWindow", mock=True)
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})
    report = str(gql.Probe.Run())
    if "C_WorldQuests.GetWorldQuests" not in report:
        return fail("probe_report_and_command_registered", "报告应含候选接口名")
    if "AVAILABLE" not in report or "MISSING" not in report:
        return fail("probe_report_and_command_registered", "报告应区分可用/缺失")
    if "LastDegrade" not in report:
        return fail("probe_report_and_command_registered", "报告应带降级原因")
    for cmd in ("probe", "log"):
        if gql.commands[cmd] is None:
            return fail("probe_report_and_command_registered", f"/gql {cmd} 未注册")
    ok("probe_report_and_command_registered")


def test_probe_map_candidates_use_map_id_and_skip_without_one():
    """上一轮报告里 C_QuestLog.GetQuestsOnMap 只留下 "bad argument #1 (Usage:
    GetQuestsOnMap(uiMapID))" 这条废证据：它要 mapID 才问得出形状。
    这次必须拿真 mapID 调用，并把 C_Map 的成员列出来（枚举地图的入口就藏在里面）；
    拿不到 mapID 时明写跳过，不许冒充成"缺失"或"报错"。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    rows = lua.table_from({})
    rows[1] = lua.table_from({ "questID": 88, "title": "虚空突袭" })
    g.C_QuestLog = lua.table_from({
        "GetQuestsOnMap": lambda mid: rows,
        "GetBountiesForMapID": lambda mid: lua.table_from({}),
    })
    report = str(gql.Probe.Run())
    if "GetQuestsOnMap(mapID=2007)" not in report:
        return fail("probe_map_candidates_use_map_id_and_skip_without_one",
                    f"应带真 mapID 调用:\n{report}")
    if "C_Map 成员数" not in report:
        return fail("probe_map_candidates_use_map_id_and_skip_without_one",
                    f"接口发现段应枚举 C_Map:\n{report}")
    if "GetBountiesForMapID" not in report:
        return fail("probe_map_candidates_use_map_id_and_skip_without_one",
                    "C_QuestLog 里的 bounty 入口也要问一遍")

    g.C_Map = None
    report = str(gql.Probe.Run())
    if "无 mapID" not in report:
        return fail("probe_map_candidates_use_map_id_and_skip_without_one",
                    f"没有 mapID 时应记跳过:\n{report}")
    ok("probe_map_candidates_use_map_id_and_skip_without_one")


def test_probe_reports_map_button_host():
    """按钮"看不见"和"没建"是两件事，报告必须分得开：宿主在不在、显隐、尺寸、
    按钮建没建四样都要有。阶段二按钮挂 Minimap，这里验 Minimap 的几何与降级。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    lua.execute("Minimap.__shown = true\nMinimap:SetSize(1400, 900)")
    report = str(gql.Probe.Run())
    if "---- 地图按钮宿主" not in report:
        return fail("probe_reports_map_button_host", f"报告缺少地图按钮宿主段:\n{report}")
    if "Minimap=可用" not in report or "尺寸=1400x900" not in report:
        return fail("probe_reports_map_button_host", f"宿主几何没写全:\n{report}")
    if "按钮=未建" not in report:
        return fail("probe_reports_map_button_host", f"按钮没建要如实报未建:\n{report}")
    g.Minimap = None
    report = str(gql.Probe.Run())
    if "Minimap=缺失" not in report:
        return fail("probe_reports_map_button_host", f"宿主缺失要写明:\n{report}")
    ok("probe_reports_map_button_host")


def test_probe_distinguishes_unbuilt_from_invisible_button():
    """"没建"和"建了但看不见"修法完全不同：前者是入口漏接，后者是层级或锚点问题。
    报告里这一行必须跟着 Matrix 的真实状态变，不能恒定写未建。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", "Matrix", mock=True)
    if "按钮=未建" not in str(gql.Probe.Run()):
        return fail("probe_distinguishes_unbuilt_from_invisible_button",
                    "没建按钮时应报未建")
    gql.Matrix.EnsureMapButton()
    if "按钮=已建" not in str(gql.Probe.Run()):
        return fail("probe_distinguishes_unbuilt_from_invisible_button",
                    f"建好后应报已建:\n{gql.Probe.Run()}")
    ok("probe_distinguishes_unbuilt_from_invisible_button")


def test_probe_area_poi_lists_event_pins():
    """丰饶"现在在哪个区"、亚基入侵在哪个区不是任务，是地图上的事件 pin
    （用户 2026-10-24：按 M 打开地图就能看到，人不用跑过去）。这一段必须按图枚举
    GetEventsForMap、把命中族词的 pin 摊开，并在末尾单独报"哪张图报了事件"——
    那份名单才是下一版决定"行跟着事件走还是退回周常组"的唯一依据。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2444: True, 2437: True})
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2437})
    g.C_AreaPoiInfo = lua.table_from({
        "GetEventsForMap": lambda mid: (lua.table_from({77})
                                        if mid == 2444 else lua.table_from({})),
        "GetAreaPOIForMap": lambda mid: lua.table_from({77, 88}),
        "GetAreaPOIInfo": lambda mid, pid: lua.table_from(
            {"name": "丰饶：草药洞穴", "isCurrentEvent": True, "areaPoiID": pid}),
        "GetAreaPOISecondsLeft": lambda pid: 3120,
        "IsAreaPOITimed": lambda pid: True,
    })
    report = str(gql.Probe.Run())
    if "枚举范围=跟踪清单 共 2 张图" not in report:
        return fail("probe_area_poi_lists_event_pins", f"没按跟踪清单枚举:\n{report}")
    if "[2444" not in report or "事件=1" not in report:
        return fail("probe_area_poi_lists_event_pins", f"每张图的事件数没报:\n{report}")
    if "丰饶：草药洞穴" not in report or "命中「丰饶」" not in report:
        return fail("probe_area_poi_lists_event_pins", f"命中族词的 pin 没摊开:\n{report}")
    if "剩余=3120" not in report:
        return fail("probe_area_poi_lists_event_pins", f"pin 的倒计时没报:\n{report}")
    if "2444(?) poi#77" not in report:
        return fail("probe_area_poi_lists_event_pins",
                    f"末尾那份\"哪张图报了事件\"不成立:\n{report}")
    ok("probe_area_poi_lists_event_pins")


def test_probe_area_poi_missing_does_not_swallow_later_sections():
    """12.x 到底有没有 C_AreaPoiInfo 一点没实测过。没有的时候这段要照实打 MISSING，
    并且不许把后面的按钮段/池样本段一起吞掉（12.1.5 实测过"一处 format 报错，
    整份报告没了"，取证报告比实现更容易踩这个坑）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2444: True})
    report = str(gql.Probe.Run())
    if "MISSING   C_AreaPoiInfo.GetEventsForMap" not in report:
        return fail("probe_area_poi_missing_does_not_swallow_later_sections",
                    f"入口缺失要照实报:\n{report}")
    if "一条都没有" not in report:
        return fail("probe_area_poi_missing_does_not_swallow_later_sections",
                    f"没有事件源要给出结论:\n{report}")
    if "---- 地图按钮宿主" not in report:
        return fail("probe_area_poi_missing_does_not_swallow_later_sections",
                    "事件段把后面的段吞了")
    ok("probe_area_poi_missing_does_not_swallow_later_sections")


def test_probe_asks_map_membership_for_resident_weekly():
    """12.1.5 实测 GetQuestGiverMapPosition / GetQuestLogZoneText 都不存在，
    "问发布者"这条线死了。追踪萨拉塔斯那种永不上板的周常还剩两条天生带 mapID 的
    入口（C_QuestLog.IsOnMap、C_TaskQuest.DoesMapShowTaskQuestObjectives），
    逐张问一遍就够，人不用跑。报告必须把"每张都说在"这种不按图区分的回答
    自己判成废证据 —— 否则下一版会拿它当真凭据去归组。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2444: True, 2437: True})
    g.GQLDB["quests"][98172] = lua.table_from({"name": "追踪萨拉塔斯", "regKey": "kw:追踪萨拉塔斯"})
    g.C_QuestLog = lua.table_from({
        "IsOnMap": lambda q, mid: mid == 2444,
        "HasActiveThreats": lambda: True,
        "GetActiveThreatMaps": lambda: lua.table_from({2444, 2437}),
        "GetNextWaypointForMap": lambda q, mid: f"{q}@{mid}",
    })
    g.C_TaskQuest = lua.table_from({"DoesMapShowTaskQuestObjectives": lambda q, mid: True,
                                    "GetThreatQuests": lambda: lua.table_from({98172})})
    report = str(gql.Probe.Run())
    if '---- 按图问"这条在不在这张图"' not in report:
        return fail("probe_asks_map_membership_for_resident_weekly", f"缺按图问的那段:\n{report}")
    if "「追踪萨拉塔斯」C_QuestLog.IsOnMap -> 说在的图 2444(?)" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"只有 2444 答在，就该只列 2444:\n{report}")
    if "每张都说在 = 不按图区分，这条凭据作废" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"全图同答要自己判成废证据:\n{report}")
    if "C_QuestLog.GetActiveThreatMaps -> table[#2]" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"威胁清单没报:\n{report}")
    if "GetNextWaypointForMap(98172, 2444(?)) -> 98172@2444" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"下一跳要按 (questID, uiMapID) 两参问，只传图号必抛错:\n{report}")

    # 威胁任务问不出（入口缺失/回空）时不许拿图号硬凑：照实 SKIP，别造一条假坐标。
    g.C_TaskQuest = lua.table_from({"DoesMapShowTaskQuestObjectives": lambda q, mid: True})
    report = str(gql.Probe.Run())
    if "GetNextWaypointForMap(2444(?)) -> SKIP" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"配不上任务号要写 SKIP:\n{report}")

    g.C_QuestLog = None
    g.C_TaskQuest = None
    report = str(gql.Probe.Run())
    if "「追踪萨拉塔斯」C_QuestLog.IsOnMap MISSING" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"入口缺失要照实报 MISSING:\n{report}")
    if "C_QuestLog.HasActiveThreats -> MISSING" not in report:
        return fail("probe_asks_map_membership_for_resident_weekly",
                    f"威胁入口缺失要照实报:\n{report}")
    ok("probe_asks_map_membership_for_resident_weekly")


def poi_env(lua, pins, parents=None, names=None, world_quest=None):
    """事件 pin 的替身，形状照 12.1.5 实测：GetEventsForMap(mapID) 回 poiID 名单，
    GetAreaPOIInfo(uiMapID, poiID) 回 name/isCurrentEvent/isPrimaryMapForPOI。
    跟踪清单直接用 extraMaps（默认清单在这台机器要按区名匹配，测试里没必要）。"""
    g = lua.globals()
    maps = sorted({int(k) for k in pins})
    g.GQLDB["config"]["extraMaps"] = lua.table_from({m: True for m in maps})
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: lua.table_from({
        "mapID": int(mid),
        "name": (names or {}).get(int(mid)),
        "parentMapID": (parents or {}).get(int(mid), 0)})})

    def events(mid):
        return lua.table_from([p["id"] for p in pins.get(int(mid), [])])

    def info(mid, pid):
        for p in pins.get(int(mid), []):
            if p["id"] == int(pid):
                row = {"areaPoiID": p["id"], "name": p["name"], "isCurrentEvent": True}
                if p.get("primary"):
                    row["isPrimaryMapForPOI"] = True
                # pin 上挂不挂任务号 = 12.x 没实测过的一格（/gql probe 的"任务号="结案）。
                # 这是给**已有**接口换一种返回形状，不是凭空造接口：造方法会让替身答得出
                # 客户端根本没有的东西（12.1.5 矩阵砖掉就是这么来的）。
                if p.get("questID"):
                    row["questID"] = p["questID"]
                return lua.table_from(row)
        return None

    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": events, "GetAreaPOIInfo": info})
    g.QuestUtils_IsQuestWorldQuest = world_quest or (lambda qid: True)


def group_keys(lua, now=None):
    """矩阵分组：'mapID=1,2;mapID=3' 一串（验的是 groupMapID，不是账本里的 mapID）。"""
    return one(lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), ...) local s = '' "
        "for i, g in ipairs(out) do "
        "  s = s .. (i > 1 and ';' or '') .. tostring(g.mapID) .. '=' "
        "  for j, r in ipairs(g.rows) do "
        "    s = s .. (j > 1 and ',' or '') .. tostring(r.questID) end end return s", now))


def hand_rec(lua, extra):
    """手工造的账本记录必须自带 observations：Ledger 的收拢/清理直接索引它，
    少一笔会让用例抛错（夹具自己造出来的假崩，不是产品缺陷）。"""
    rec = lua.table_from({"observations": lua.table_from({})})
    for k, v in extra.items():
        rec[k] = v
    return rec


def test_event_pin_groups_the_row_where_the_event_is():
    """用户 2026-10-24 定的口径（原话）："如果知道去哪办 那就没必要放在祖阿曼了吧？
    事件在哪 就在哪个地图分组"。
    丰饶供品（内置表 89507，族词"丰饶"）：客户端答的图是 2437 祖阿曼，可按 M 看到的那块板
    挂在 2413 哈籁恩达尔 —— 12.1.5 实测同名两条 pin（8527@2413 + 8676@2537 奎尔萨拉斯=父图），
    两条 isPrimaryMapForPOI 都是 false，只能靠"排掉别的候选图的父图"选得出真正办事的那张子图。
    归属只写派生字段 evMapID：**账本里的 2437 不许被改写**（那是客户端答过的旧凭据，
    pin 断了还要靠它回到原位，清了整行就掉进"未知地图"）。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua,
            {2413: [{"id": 8527, "name": "丰饶：草药洞窟"}],
             2537: [{"id": 8676, "name": "丰饶：草药洞窟"}],
             2437: []},
            parents={2413: 2537},
            names={2413: "哈籁恩达尔", 2537: "奎尔萨拉斯", 2437: "祖阿曼"})
    g.GQLDB["quests"][89507] = hand_rec(lua, {
        "name": "丰饶供品", "regKey": "abundant-offerings",
        "mapID": 2437, "mapSrc": "跟踪地图清单"})
    res = gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    rec = g.GQLDB["quests"][89507]
    if int(rec["evMapID"]) != 2413:
        return fail("event_pin_groups_the_row_where_the_event_is",
                    f"同名 pin 挂在子图+父图，排掉父图才选得出 2413:\n{rec['evMapID']}")
    if str(rec["evName"]) != "丰饶：草药洞窟" or int(rec["evAt"]) != 5000:
        return fail("event_pin_groups_the_row_where_the_event_is",
                    f"pin 名字与问到的时刻要一起存:\n{rec['evName']}/{rec['evAt']}")
    if int(rec["mapID"]) != 2437:
        return fail("event_pin_groups_the_row_where_the_event_is",
                    "事件归属是派生字段，不许改写客户端答过的那个图号")
    if int(res[0]) != 1:
        return fail("event_pin_groups_the_row_where_the_event_is", "写入条数要能回报（日志用它）")
    groups = group_keys(lua)
    if groups != "2413=89507":
        return fail("event_pin_groups_the_row_where_the_event_is",
                    f"分组头要跟着事件走（2413），实际 {groups}")
    ok("event_pin_groups_the_row_where_the_event_is")


def test_event_pin_ambiguous_or_gone_never_invents_a_group():
    """三种"选不出"都不许写归属，行留在客户端原来那个分组：
    ① 同名 pin 挂在两张**互不为父子**的图上（primary 都没标）= 客户端没告诉我们哪张是办的地方，
       猜一张就是拿假图当分组头，玩家照它跑错区；
    ② pin 散了（这块板这一轮没报）= 上一轮的归属必须清掉，挂着旧图等于谎报一块不存在的板子；
    ③ C_AreaPoiInfo 整个缺失/报错 ≠ "这张图没有事件" —— 不清、也不写，照实回报"没答"，
       否则下次装上插件之前矩阵会整批跳组。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua,
            {2413: [{"id": 8527, "name": "丰饶：草药洞窟"}],
             2437: [{"id": 8900, "name": "丰饶：草药洞窟"}]},
            parents={}, names={2413: "哈籁恩达尔", 2437: "祖阿曼"})
    g.GQLDB["quests"][89507] = hand_rec(lua, {
        "name": "丰饶供品", "regKey": "abundant-offerings",
        "mapID": 2437, "mapSrc": "跟踪地图清单"})
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    if g.GQLDB["quests"][89507]["evMapID"] is not None:
        return fail("event_pin_ambiguous_or_gone_never_invents_a_group",
                    "两张互不为父图的候选图 = 选不出，不许猜一张当分组依据")
    if group_keys(lua) != "2437=89507":
        return fail("event_pin_ambiguous_or_gone_never_invents_a_group",
                    f"选不出时行留在客户端答过的图:\n{group_keys(lua)}")

    # ② 这块板这一轮没报了
    g.C_AreaPoiInfo = lua.table_from({
        "GetEventsForMap": lambda mid: lua.table_from({}),
        "GetAreaPOIInfo": lambda mid, pid: None})
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    g.GQLDB["quests"][89507]["evMapID"] = 2413      # 造一份上一轮留下的归属
    gql.Sync.RefreshEventPins(g.GQLDB, 7000)
    if g.GQLDB["quests"][89507]["evMapID"] is not None:
        return fail("event_pin_ambiguous_or_gone_never_invents_a_group",
                    "pin 散了还挂着旧图 = 谎报一块不存在的板子，必须清")

    # ③ 接口整个没答
    g.C_AreaPoiInfo = None
    g.GQLDB["quests"][89507]["evMapID"] = 2413
    res = gql.Sync.RefreshEventPins(g.GQLDB, 8000)
    if bool(res[2]):
        return fail("event_pin_ambiguous_or_gone_never_invents_a_group",
                    "接口缺失要回报\"没答\"，调用方才写得出降级日志")
    if g.GQLDB["quests"][89507]["evMapID"] is None:
        return fail("event_pin_ambiguous_or_gone_never_invents_a_group",
                    "客户端没答 ≠ 这块板没了：不清上一轮的归属")
    ok("event_pin_ambiguous_or_gone_never_invents_a_group")


def test_probe_reports_the_production_pin_attribution():
    """取证段必须报"生产据此把行归到了哪张图"，不能只报"客户端答了什么形状"：
    词配不上、选不出图、pin 散了这三种掉法在界面上长得一模一样（行都留在旧分组），
    只有这一段能分开。判据读 Sync.PinWordFor + Ledger.RowGroupMapID —— 与矩阵同源，
    这里再写一遍就等着 probe 说 A、界面画 B（最难查的那种分歧）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2413: True, 2437: True})
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: lua.table_from(
        {"mapID": int(mid), "parentMapID": 0,
         "name": {2413: "哈籁恩达尔", 2437: "祖阿曼"}.get(int(mid))})})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: False
    g.GQLDB["quests"][89507] = hand_rec(lua, {
        "name": "丰饶供品", "regKey": "abundant-offerings",
        "mapID": 2437, "mapSrc": "跟踪地图清单",
        "evMapID": 2413, "evName": "丰饶：草药洞窟", "evAt": 5000})
    g.GQLDB["quests"][98172] = hand_rec(lua, {
        "name": "追踪萨拉塔斯", "regKey": "kw:追踪萨拉塔斯",
        "mapID": 2393, "mapSrc": "玩家指定"})
    report = str(gql.Probe.Run())
    if "---- 事件 pin 的分组归属" not in report:
        return fail("probe_reports_the_production_pin_attribution",
                    f"缺生产归属那一段:\n{report}")
    if "#89507 丰饶供品 词=丰饶 归属=2413(哈籁恩达尔)" not in report:
        return fail("probe_reports_the_production_pin_attribution",
                    f"pin 归到 2413 就报 2413，别再报账本那个 2437:\n{report}")
    if "周常（答不出地图）" not in report:
        return fail("probe_reports_the_production_pin_attribution",
                    f"问不出图的常驻周常要报成周常组:\n{report}")
    ok("probe_reports_the_production_pin_attribution")


def test_weekly_group_holds_the_rows_the_client_cannot_place():
    """用户 2026-10-24 两条口径合起来：
    "如果实现没有地图，可以**新增一个周常组**，把它放进去"、选择"**只降周常**"。
    追踪萨拉塔斯 98172：发布者/日志区/悬赏/按图清单/IsOnMap/DoesMapShow/威胁清单七条凭据
    在这台 12.1.5 上全灭，只有玩家口述的"银月城"。口述不能当分组依据（那是替客户端回答），
    但也不许跟"换轮换出板的世界任务"混在同一个"未知地图"里 —— 那批等一等地图就回来，
    这批永远问不出，混在一起玩家分不清该等还是该认。
    同时划两条边界：① 真世界任务（客户端答 isWorldQuest=true）哪怕只有口述图也照旧按图分组
    （第十三轮"加固符文石/特别任务整批消失"就是拿宽判据砍合法行）；
    ② 客户端答过图的常驻周常（板面/清单来源）留在客户端那张图，不进周常组。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua, {}, names={2393: "银月城", 2413: "哈籁恩达尔"},
            world_quest=lambda qid: qid == 777)
    g.GQLDB["quests"][98172] = hand_rec(lua, {
        "name": "追踪萨拉塔斯", "regKey": "kw:追踪萨拉塔斯",
        "mapID": 2393, "mapSrc": "玩家指定"})
    g.GQLDB["quests"][777] = hand_rec(lua, {
        "name": "城市日常", "kwLogAdded": True, "mapID": 2393, "mapSrc": "玩家指定"})
    g.GQLDB["quests"][888] = hand_rec(lua, {
        "name": "特别任务：加固符文石", "regKey": "kw:特别任务",
        "mapID": 2413, "mapSrc": "跟踪地图清单"})
    weekly = int(g.GQL.Ledger.WeeklyGroupMapID())
    groups = group_keys(lua)
    # 周常组置顶（用户 2026-10-24 第二十二轮："如果有周常，就周常分组放在第一，其他分组靠后"），
    # 其余组仍按行顺序（MatrixRows 按金额降序、同金额按 questID 升序摊行，这三条都没金额）
    want = f"{weekly}=98172;2393=777;2413=888"
    if groups != want:
        return fail("weekly_group_holds_the_rows_the_client_cannot_place",
                    f"周常组/世界任务/客户端答过图的周常三条边界：期望 {want}，实际 {groups}")
    # 组头只写"周常"（同轮："分组名 周常 就行 不要答不出 字样"）：
    # "答不出地图"是我们的取证口径，不是玩家要读的东西，完整解释在行悬停里。
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if not any("周常" in t for t in frame_texts(g.GQLMatrixFrame)):
        return fail("weekly_group_holds_the_rows_the_client_cannot_place",
                    f"周常组的组头就是「周常」两个字:\n{joined}")
    if "答不出" in joined or "没报过地图" in joined:
        return fail("weekly_group_holds_the_rows_the_client_cannot_place",
                    f"组头不许带取证口径的字样（用户：'分组名 周常 就行 不要答不出 字样'）:\n{joined}")
    if "未知地图" in joined:
        return fail("weekly_group_holds_the_rows_the_client_cannot_place",
                    f"这批有名字有周常身份，不该再挂'未知地图':\n{joined}")
    ok("weekly_group_holds_the_rows_the_client_cannot_place")


def test_an_unregistered_weekly_lands_in_the_weekly_group():
    """用户 2026-10-24 第二十二轮指着截图说："追踪萨拉塔斯 也应该归入"周常组。
    上一版周常组只收"注册过的"（regKey / kwLogAdded），而这条既没注册也不是日志收的进行中
    任务，两个标记都没有 ⇒ 它顶着口述的"银月城"留在地图分组里，正是截图那条。
    判据换成只认客户端那一句"这不是世界任务"：答 true 归板、答不出（nil）不动它的分组。
    夹具的准入凭据（第二十七轮）：以前靠 `manualAdded` 把三条拉进矩阵，那个字段本轮下线
    ⇒ 改给真凭据（上过板 + 本轮读到过门槛金币），验的还是同一件事，只是不再拿
    "玩家点了一下"当上板的理由。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    # 三条各占一种客户端答法：false=明确不是世界任务、true=是、nil=接口答不出。
    # 分开 questID 才验得出三种：是不是世界任务是会话内静态属性，Api 里有缓存，
    # 同一个 ID 中途改替身答法不会重新问（那是缓存，不是 bug）。
    poi_env(lua, {}, names={2393: "银月城"}, world_quest=lambda qid: (
        False if int(qid) == 98172 else None if int(qid) == 98174 else True))
    for qid in (98172, 98173, 98174):
        g.GQLDB["quests"][qid] = hand_rec(lua, {
            "name": ("追踪萨拉塔斯" if qid == 98172 else "同名待遇#" + str(qid)),
            "poolSeen": True, "goldMoney": 30000000, "goldRound": 0,
            "mapID": 2393, "mapSrc": "玩家指定"})
    weekly = int(g.GQL.Ledger.WeeklyGroupMapID())
    want = f"{weekly}=98172;2393=98173,98174"
    if group_keys(lua) != want:
        return fail("an_unregistered_weekly_lands_in_the_weekly_group",
                    f"只有客户端明确答「不是世界任务」的那条进周常组（答是/答不出都留在原位）："
                    f"期望 {want}，实际 {group_keys(lua)}")
    ok("an_unregistered_weekly_lands_in_the_weekly_group")


def test_a_pin_that_carries_a_quest_id_becomes_a_row():
    """用户 2026-10-24 第二十二轮："另外就是亚基入侵了，关键字我已经加了，矩阵里还看不到"。
    那种事件既不在世界任务板上、也不在任务日志里 ⇒ 关键词补扫的两条路（板面清单 + 日志）
    都碰不到它。唯一可能的自动凭据 = pin 自己带的任务号（12.x 未实测，probe 的"任务号="结案）。
    客户端给了就按 kw:<关键词> 注册成行、并按这块板所在的图分组；没给就一行都不造 ——
    拿事件名去猜一个 questID 会把别的任务的完成状态冒充成它的。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua, {2395: [{"id": 8813, "name": "亚基入侵", "questID": 99001,
                          "primary": True}],
                  2437: [{"id": 8813, "name": "亚基入侵", "questID": 99001}]},
            names={2395: "永歌森林", 2437: "祖阿曼"}, world_quest=lambda qid: False)
    gql.Config.AddKeyword("亚基入侵")
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    rec = g.GQLDB["quests"][99001]
    if rec is None:
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    "pin 带了任务号就该注册成行，否则玩家加了关键词还是看不到")
    if str(rec["regKey"]) != "kw:亚基入侵":
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    f"注册要挂 kw:关键词（删关键词一起退），实际 {rec['regKey']}")
    if int(rec["evMapID"]) != 2395:
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    f"归属图仍走 primary 判据（2395），实际 {rec['evMapID']}")
    if group_keys(lua) != "2395=99001":
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    f"行要出现在事件那块板的分组里，实际 {group_keys(lua)}")
    logs = str(gql.Config.GetLogs())
    if "地图事件里注册了 1 条周常" not in logs:
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    f"注册了几条、哪几条要写进日志:\n{logs}")

    # 客户端没给任务号：一行都不许造
    lua.execute("GQLDB.quests[99001] = nil")
    g.C_AreaPoiInfo = lua.table_from({
        "GetEventsForMap": lambda mid: lua.table_from({1: 8813}),
        "GetAreaPOIInfo": lambda mid, pid: lua.table_from({
            "areaPoiID": int(pid), "name": "亚基入侵", "isCurrentEvent": True,
            "isPrimaryMapForPOI": int(mid) == 2395}),
    })
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    if g.GQLDB["quests"][99001] is not None:
        return fail("a_pin_that_carries_a_quest_id_becomes_a_row",
                    "pin 没带任务号时不许凭空造行")
    ok("a_pin_that_carries_a_quest_id_becomes_a_row")


def test_keyword_hunt_names_the_pin_only_hits():
    """加了关键词却"0 张图命中"有两种完全不同的原因（用户 2026-10-24 第二十二轮：
    "亚基入侵了，关键字我已经加了，矩阵里还看不到"）：① 图没加进跟踪清单；
    ② 它压根不是任务，只是地图事件 pin。上一版两种都回同一句"先确认关键词写的是任务名的一部分"，
    玩家照着改关键词改三轮也没用。现在回报里分开说，并带上"客户端给没给任务号"。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2395} end")
    poi_env(lua, {2395: [{"id": 8813, "name": "亚基入侵", "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({}),
        "GetQuestInfoByQuestID": lambda qid: None,
    })
    gql.Config.AddKeyword("亚基入侵")
    done, msg = gql.Sync.HuntKeywordMaps()
    if not done:
        return fail("keyword_hunt_names_the_pin_only_hits", "补扫应跑完")
    if "亚基入侵" not in str(msg) or "任务号" not in str(msg):
        return fail("keyword_hunt_names_the_pin_only_hits",
                    f"命中 0 条时要说出「这名字只在地图事件 pin 里、客户端没给任务号」:\n{msg}")
    ok("keyword_hunt_names_the_pin_only_hits")


def test_keyword_hunt_logs_the_pin_only_sentence():
    """补扫命中 0 的那句取证说明（哪条名字只在事件 pin 里、客户端没给任务号）过去只 print 到聊天，
    聊天一滚过去就读不到了 —— db.logs 才是 /gql log 读的。修法是把最终那句原样再 Log 一条。
    诚实标注：这条钉的是"最终句进了 db.logs 且是最后一条、与回报的聊天句一字不差"，
    聊天 print 本身离线抓不到（不验它）。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2395} end")
    poi_env(lua, {2395: [{"id": 8813, "name": "亚基入侵", "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({}),
        "GetQuestInfoByQuestID": lambda qid: None,
    })
    gql.Config.AddKeyword("亚基入侵")
    done, msg = gql.Sync.HuntKeywordMaps()
    if not done:
        return fail("keyword_hunt_logs_the_pin_only_sentence", "补扫应跑完")
    last = lua.execute("local L = (GQL.Config.DB().logs) or {} return L[#L]")
    if not last:
        return fail("keyword_hunt_logs_the_pin_only_sentence", "补扫后 db.logs 是空的")
    if "只在地图事件 pin 里" not in str(last) or "取证看 /gql probe 的事件段" not in str(last):
        return fail("keyword_hunt_logs_the_pin_only_sentence",
                    f"命中 0 时的取证句要进日志末条（否则聊天滚掉就读不到）:\n{last}")
    if str(msg) not in str(last):
        return fail("keyword_hunt_logs_the_pin_only_sentence",
                    f"日志末条必须与回报聊天句一字不差（别各说各话）:\n聊天: {msg}\n日志: {last}")
    ok("keyword_hunt_logs_the_pin_only_sentence")


def test_probe_lists_weekly_reward_activities():
    """亚基入侵这种公共事件**不发任务**（12.1.5 外部口径：轮次制地图公共事件，奖励是声望+货币+包；
    本机 poiInfo 15 个字段里也没有 questID，`C_AreaPoiInfo` 全族更没有取任务号的入口）
    ⇒ 我们那套"问任务做没做"的凭据对它全废。只剩每周奖励面板那份活动清单
    （`C_WeeklyRewards.GetActivities`，TWW 形状每条带 questID 与进度）没实测过：
    probe 要摊出来；这层缺失时要照实说"只剩声望/货币增量、归因不干净所以不接"，
    并且不许吞掉后面的段（取证报告踩过的老坑：一处报错，整份没了）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
    g.C_WeeklyRewards = lua.table_from({
        "GetActivities": lambda: lua.table_from({
            1: lua.table_from({"questID": 4321, "progress": 2, "numProgress": 4}),
        }),
        "HasAvailableRewards": lambda: True,
        "AreRewardsForCurrentRewardPeriod": lambda: True,
    })
    report = str(gql.Probe.Run())
    if "---- 每周奖励面板的活动清单" not in report:
        return fail("probe_lists_weekly_reward_activities", f"缺这一段:\n{report}")
    if "questID=4321" not in report or "progress=2" not in report:
        return fail("probe_lists_weekly_reward_activities",
                    f"活动清单要逐条摊开（参与记录就藏在这几条里）:\n{report}")
    g.C_WeeklyRewards = None
    report = str(gql.Probe.Run())
    if "MISSING   C_WeeklyRewards.GetActivities" not in report:
        return fail("probe_lists_weekly_reward_activities",
                    f"这层缺失要照实报 MISSING:\n{report}")
    if "只剩声望/货币增量" not in report:
        return fail("probe_lists_weekly_reward_activities",
                    f"缺失时要给结论：增量那条归因不干净，不接:\n{report}")
    if "---- 地图按钮宿主" not in report:
        return fail("probe_lists_weekly_reward_activities", "这一段把后面的段吞了")
    ok("probe_lists_weekly_reward_activities")


def test_api_current_map_id_returns_number_or_nil():
    """CurrentMapID 只是探测用的样本，猜名候选逐个 pcall：C_Map 缺失、接口报错、
    返回的不是数字，三种都必须 nil —— 编一个地图号会让下一条探测拿到假数据。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    if gql.Api.CurrentMapID() is not None:
        return fail("api_current_map_id_returns_number_or_nil", "C_Map 整个缺失应返回 nil")

    def boom(unit):
        raise RuntimeError("bad argument #1")
    g.C_Map = lua.table_from({ "GetBestMapForUnit": boom })
    if gql.Api.CurrentMapID() is not None:
        return fail("api_current_map_id_returns_number_or_nil", "接口报错应返回 nil")

    g.C_Map = lua.table_from({
        "GetBestMapForUnit": lambda unit: None,
        "GetDisplayedMapID": lambda: lua.table_from({ "mapID": 2008 }),
    })
    if int(gql.Api.CurrentMapID()) != 2008:
        return fail("api_current_map_id_returns_number_or_nil",
                    "带参调用拿不到时应退到零参候选，返回表时取 mapID 字段")
    ok("api_current_map_id_returns_number_or_nil")


def frame_texts(node, out=None):
    """把替身帧树里所有非空文本收成 list（CreateFrame 替身把子帧记进 __children）。"""
    out = [] if out is None else out
    if node is None:
        return out
    text = node.__text
    if text is not None and str(text) != "":
        out.append(str(text))
    kids = node.__children
    if kids is None:
        return out
    i = 1
    while kids[i] is not None:
        frame_texts(kids[i], out)
        i += 1
    return out


def font_strings(node, out=None):
    """替身帧树里所有 FontString，回 [(文本, 建串时的字体, 这轮是否可见)]。
    矩阵的文字池按条目复用，光看文本分不清某行用的是哪条串、什么字号。"""
    out = [] if out is None else out
    if node is None:
        return out
    font = node.__font
    if font is not None:
        out.append((str(node.__text or ""), str(font), bool(node.__shown)))
    kids = node.__children
    if kids is None:
        return out
    i = 1
    while kids[i] is not None:
        font_strings(kids[i], out)
        i += 1
    return out


def fire(gql, event, *args):
    """经 Sync 自己的帧驱动事件，避免绕开真实分发路径。"""
    frame = gql.Sync.frame
    handler = frame.GetScript(frame, "OnEvent")
    return handler(frame, event, *args)


def sync_env(quests, started=None, completed=None, money=0, reset=700000,
             weekly=None, char=("小号一", "永恒之井"), visible=None, extra=()):
    """搭好 Core+Config+Ledger+Api+Sync 的可控环境。

    quests: [(questID, money)]；money 为 None 表示奖励接口取不到。
    reset: GetQuestResetTime 的返回。12.1.5 实测它答的是**每日**重置（wiki 也这么写），
           所以它只配当对照，锚点/周统计一律不许用它。
    weekly: C_DateAndTime.GetSecondsUntilWeeklyReset 的返回（周口径唯一来源）。
            None = 本机问不出周重置 → RefreshCycle 必须降级沿用旧锚点、
            NextCycleEnd 只能退回每日档并如实标 kind="daily"。
    """
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", *extra, mock=True)
    g = lua.globals()
    g.GQL_NOW = 0
    g.GQLDB = lua.table_from({})

    rows = lua.table_from({})
    for i, (qid, gold) in enumerate(quests, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if gold is not None:
            entry["money"] = gold
        rows[i] = lua.table_from(entry)
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    started = started or set()
    completed = completed or set()
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: qid in started,
        "IsQuestCompleted": lambda qid: qid in completed,
    })
    g.GetQuestResetTime = lambda: reset
    if weekly is not None:
        g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: weekly})
    g.UnitName = lambda *a: char[0]
    g.GetRealmName = lambda: char[1]
    holder = lua.table_from({})
    g.GQL_MONEY = holder
    holder["v"] = money
    g.GetMoney = lambda: holder["v"]
    if visible:
        g.FrameIsVisible = lambda name: name in visible
    gql.Config.Init()
    return lua, gql, holder


def test_snapshot_records_added_and_views():
    """登录快照：新任务入表，本角色三态写进 views，只有真含金币的判金币任务。"""
    lua, gql, _ = sync_env([(21, 400000), (22, 0)], started={22})
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_records_added_and_views", f"快照应成功，实际 {res}")
    if len(res["added"]) != 2:
        return fail("snapshot_records_added_and_views", f"应有 2 个新任务，实际 {len(res['added'])}")
    key = str(gql.Sync.CurrentCharKey())
    if key != "小号一-永恒之井":
        return fail("snapshot_records_added_and_views", f"角色 key 错误：{key}")
    views = lua.globals().GQLDB["chars"][key]["views"]
    if str(views[21]["s"]) != "open":
        return fail("snapshot_records_added_and_views", f"任务 21 应待做，实际 {views[21]['s']}")
    if str(views[22]["s"]) != "progress":
        return fail("snapshot_records_added_and_views", f"任务 22 应已接未交，实际 {views[22]['s']}")
    if len(gql.Ledger.GoldQuestIDs(lua.globals().GQLDB)) != 1:
        return fail("snapshot_records_added_and_views", "只有任务 21 应判金币任务")
    ok("snapshot_records_added_and_views")


def test_snapshot_degrades_to_unknown():
    """完成状态接口缺失：views 写 unknown 而非 open，degrade 带原因。"""
    lua, gql, _ = sync_env([(31, None)])
    g = lua.globals()
    g.C_QuestLog = None      # 状态接口整体不可用
    res = gql.Sync.SnapshotNow()
    if res is None:
        return fail("snapshot_degrades_to_unknown", "缺状态接口不应整体失败")
    key = str(gql.Sync.CurrentCharKey())
    st = str(g.GQLDB["chars"][key]["views"][31]["s"])
    if st != "unknown":
        return fail("snapshot_degrades_to_unknown", f"应写 unknown，实际 {st}")
    ok("snapshot_degrades_to_unknown")


def test_pool_unavailable_keeps_ledger():
    """池接口不可用：不动账本，不把全部任务判成消失。"""
    lua, gql, _ = sync_env([(41, 500000)])
    g = lua.globals()
    gql.Sync.SnapshotNow()
    before = int(g.GQLDB["quests"][41]["lastSeenAt"])
    g.C_WorldQuests = None           # 接口整个消失
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is None:
        return fail("pool_unavailable_keeps_ledger", "应返回降级原因")
    if int(g.GQLDB["quests"][41]["lastSeenAt"]) != before:
        return fail("pool_unavailable_keeps_ledger", "池不可用时账本不应变化")
    if g.GQLDB["quests"][41]["currentSig"] is None:
        return fail("pool_unavailable_keeps_ledger", "不应把读不到当成任务消失")
    ok("pool_unavailable_keeps_ledger")


def test_empty_pool_needs_second_read():
    """空池是数据，但登录那一瞬接口未必就绪。一次空读不许判全部消失：
    要 30 秒后第二次仍为空才入库，否则本轮已交会被一次误读毁掉。"""
    lua, gql, _ = sync_env([(61, 500000)])
    g = lua.globals()
    gql.Sync.SnapshotNow()
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({})})

    res = gql.Sync.SnapshotNow()
    if res is None or res["pendingEmpty"] is None:
        return fail("empty_pool_needs_second_read", f"首次空读应只等确认，实际 {res}")
    if g.GQLDB["quests"][61]["currentSig"] is None:
        return fail("empty_pool_needs_second_read", "首次空读不应把任务判成消失")

    g.GQL_NOW = 31
    res = gql.Sync.SnapshotNow()
    if res is None or res["pendingEmpty"] is not None or int(res["poolCount"]) != 0:
        return fail("empty_pool_needs_second_read", f"二次空读应入库，实际 {res}")
    if g.GQLDB["quests"][61]["currentSig"] is not None:
        return fail("empty_pool_needs_second_read", "二次确认后应记为消失")
    ok("empty_pool_needs_second_read")


def test_turnin_money_window_capture_and_abort():
    """交任务：窗口内钱差入账；期间开过邮箱则作废记 nil（未知），不记可疑数字。"""
    lua, gql, money = sync_env([(51, 500000)], visible={"MailFrame"})
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    g = lua.globals()
    money["v"] = 1000000

    fire(gql, "QUEST_COMPLETE", 51)
    money["v"] = 1600000
    g.FrameIsVisible = lambda name: False
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    rows = g.GQLDB["turnins"][key][51]
    if rows is None or len(rows) != 1:
        return fail("turnin_money_window_capture_and_abort", f"应写 1 条记录，实际 {rows}")
    if int(rows[1]["g"]) != 600000:
        return fail("turnin_money_window_capture_and_abort", f"增量应 600000，实际 {rows[1]['g']}")
    if state1(gql.Ledger.State, g.GQLDB, key, 51) != "done":
        return fail("turnin_money_window_capture_and_abort", "交完应显示已交")

    fire(gql, "QUEST_COMPLETE", 51)
    money["v"] = 2200000
    g.FrameIsVisible = lambda name: name == "MailFrame"
    g.GQL_NOW = 40
    gql.Sync.FinishWindow()
    rows = g.GQLDB["turnins"][key][51]
    if len(rows) != 2:
        return fail("turnin_money_window_capture_and_abort", "第二次也应入账（新一轮）")
    if rows[2]["g"] is not None:
        return fail("turnin_money_window_capture_and_abort", "窗口内开过邮箱应放弃测量记 nil")
    ok("turnin_money_window_capture_and_abort")


def test_api_frame_visible_candidates():
    """帧可见性必须走 Api：Sync 直接调全局函数，12.x 形态既测不到也报不进 probe，
    钱差窗口的"开过邮箱就作废"会静默失效。两条候选都要试，都没有时降级为 false。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    g = lua.globals()
    if gql.Api.FrameVisible is None:
        return fail("api_frame_visible_candidates", "Api.FrameVisible 尚未实现")
    if bool(gql.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "两条候选都缺失应返回 false")
    # lupa 的 Python 迭代给的是键不是值，必须按 1-based 下标取值
    table = gql.Api.CandidatePaths()
    paths = [str(table[i]) for i in range(1, len(table) + 1)]
    if "FrameIsVisible" not in paths:
        return fail("api_frame_visible_candidates", "probe 候选应含 FrameIsVisible")

    g.FrameIsVisible = lambda name: name == "MailFrame"
    if not bool(gql.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "函数候选可用时应返回可见")
    g.FrameIsVisible = None

    frame = lua.table_from({"IsShown": lambda self: True})
    g.MailFrame = frame
    if not bool(gql.Api.FrameVisible("MailFrame")):
        return fail("api_frame_visible_candidates", "应回退到 _G[名字]:IsShown()")
    if bool(gql.Api.FrameVisible("AuctionFrame")):
        return fail("api_frame_visible_candidates", "不存在的帧应返回 false")
    ok("api_frame_visible_candidates")


def test_money_window_polluted_by_frame_object():
    """窗口里邮箱帧存在且 IsShown：这笔入账作废记 nil，不记可疑数字。"""
    lua, gql, money = sync_env([(55, 500000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 55)
    money["v"] = 1600000
    g.MailFrame = lua.table_from({"IsShown": lambda self: True})
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    rows = g.GQLDB["turnins"][key][55]
    if rows is None or len(rows) != 1:
        return fail("money_window_polluted_by_frame_object", f"应写 1 条记录，实际 {rows}")
    if rows[1]["g"] is not None:
        return fail("money_window_polluted_by_frame_object", f"邮箱帧开着应作废记 nil，实际 {rows[1]['g']}")
    ok("money_window_polluted_by_frame_object")


def test_second_turnin_event_does_not_reset_window():
    """QUEST_COMPLETE 与 QUEST_TURNED_IN 都会触发：窗口起点必须取第一个事件，
    否则钱已在两个事件之间到账，起点被覆盖成到账后的数，这笔金币会记成 0。"""
    lua, gql, money = sync_env([(57, 500000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 57)
    money["v"] = 1300000        # 任务直接给的钱在两个事件之间到账
    fire(gql, "QUEST_TURNED_IN", 57)
    money["v"] = 1600000        # 随后开宝箱
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    rows = g.GQLDB["turnins"][key][57]
    if rows is None or len(rows) != 1:
        return fail("second_turnin_event_does_not_reset_window", f"应只写 1 条记录，实际 {rows}")
    if int(rows[1]["g"]) != 600000:
        return fail("second_turnin_event_does_not_reset_window",
                    f"窗口应覆盖两个事件的进账，合计 600000，实际 {rows[1]['g']}")
    ok("second_turnin_event_does_not_reset_window")


def test_back_to_back_turnins_both_recorded():
    """连着交两个任务：前一笔立刻结算，两个任务都要有记录。
    只保留第一个窗口的写法会把后一个任务整笔丢掉，账本上就变成"没做"。"""
    lua, gql, money = sync_env([(58, 500000), (59, 400000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 58)
    money["v"] = 1300000
    fire(gql, "QUEST_COMPLETE", 59)
    money["v"] = 1700000
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    rowsA = g.GQLDB["turnins"][key][58]
    rowsB = g.GQLDB["turnins"][key][59]
    if rowsA is None or len(rowsA) != 1 or int(rowsA[1]["g"]) != 300000:
        return fail("back_to_back_turnins_both_recorded", f"任务 58 应立刻结算 300000，实际 {rowsA}")
    if rowsB is None or len(rowsB) != 1 or int(rowsB[1]["g"]) != 400000:
        return fail("back_to_back_turnins_both_recorded", f"任务 59 应结算 400000，实际 {rowsB}")
    ok("back_to_back_turnins_both_recorded")


def test_money_window_samples_during_window():
    """污染要在整段窗口里盯，不能只在结算那一刻看：
    交完任务开邮箱捡钱再关掉，15 秒后结算那一刻帧已经隐了，只在结尾采样会把这笔外来入账记到任务头上。"""
    lua, gql, money = sync_env([(61, 500000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 61)
    g.MailFrame = lua.table_from({"IsShown": lambda self: True})
    money["v"] = 1400000
    g.GQL_NOW = 5
    lua.globals().GQL_FireTimers()      # 窗口内的那次采样
    g.MailFrame = None                  # 捡完钱关掉
    money["v"] = 1500000
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    rows = g.GQLDB["turnins"][key][61]
    if rows is None or len(rows) != 1:
        return fail("money_window_samples_during_window", f"应写 1 条记录，实际 {rows}")
    if rows[1]["g"] is not None:
        return fail("money_window_samples_during_window",
                    f"窗口中途开过邮箱就应整笔作废记 nil，实际 {rows[1]['g']}")
    ok("money_window_samples_during_window")


def test_debounce_single_snapshot():
    """世界任务更新事件连发只抓一次池（10 秒去抖：做任务时 QUEST_LOG_UPDATE
    几乎连成一片，2 秒一发就是开着矩阵的周期性卡顿）。"""
    lua, gql, _ = sync_env([(61, 100000)])
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    # 倒计时能读到：否则快照会另排一个 5 秒奖励补读定时器，把"去抖只抓一次"
    # 这条测成两次（补读是另一条路径，见 preload_retry 用例）
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 3600})
    # 登录同步默认就抓，不需要先打开面板
    gql.Sync.OnWorldEnter()
    for _ in range(5):
        fire(gql, "WORLD_QUEST_LOG_UPDATE")
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    if len(calls) != 1:
        return fail("debounce_single_snapshot", f"去抖后应只抓一次池，实际 {len(calls)}")
    ok("debounce_single_snapshot")


def test_sync_survives_unknown_event_name():
    """12.1.5 用户日志：Sync.OnWorldEnter 出错 ... Attempt to register unknown event
    "WORLD_QUEST_LOG_UPDATE"。未知事件名会直接抛错，而它是 OnWorldEnter 第一行，
    于是登录同步整条没跑。候选名必须逐个容错注册，快照去抖照常排上。"""
    lua, gql, _ = sync_env([(61, 100000)])
    g = lua.globals()
    # 登录同步默认就抓，不需要先打开面板
    gql.Sync.OnWorldEnter()      # 不该把错抛给调用方
    events = lua.execute("return GQLSyncFrame.__events")
    if events["QUEST_LOG_UPDATE"] is not True:
        return fail("sync_survives_unknown_event_name",
                    "WORLD_QUEST_LOG_UPDATE 不认识时应退到 QUEST_LOG_UPDATE")
    for evt in ("QUEST_COMPLETE", "QUEST_TURNED_IN"):
        if events[evt] is not True:
            return fail("sync_survives_unknown_event_name", f"{evt} 未注册")
    if events["WORLD_QUEST_LOG_UPDATE"] is not None:
        return fail("sync_survives_unknown_event_name", "不认识的事件名不该出现在注册表里")
    g.GQL_NOW = 12
    g.GQL_FireTimers()
    if lua.execute("return next(GQLDB.chars) == nil"):
        return fail("sync_survives_unknown_event_name",
                    f"登录同步没跑:\n{gql.Config.GetLogs()}")
    if "池变化事件全部不可用" in str(gql.Config.GetLogs()):
        return fail("sync_survives_unknown_event_name", "退成功时不该报全部不可用")
    ok("sync_survives_unknown_event_name")


def test_sync_logs_when_no_pool_event_registered():
    """两个候选事件名都不认识时，只能靠登录与 /gql sync 更新，这件事必须写进日志：
    否则用户只会看到"矩阵不刷新"，查不出是没有事件。"""
    lua, gql, _ = sync_env([(61, 100000)])
    lua.execute('GQL_KNOWN_EVENTS["WORLD_QUEST_LOG_UPDATE"] = nil\n'
                'GQL_KNOWN_EVENTS["QUEST_LOG_UPDATE"] = nil')
    # 登录同步默认就抓，不需要先打开面板
    gql.Sync.OnWorldEnter()
    if "池变化事件全部不可用" not in str(gql.Config.GetLogs()):
        return fail("sync_logs_when_no_pool_event_registered",
                    f"应记降级日志:\n{gql.Config.GetLogs()}")
    g = lua.globals()
    g.GQL_NOW = 12
    g.GQL_FireTimers()      # 没事件也必须照常抓一次池
    if lua.execute("return next(GQLDB.chars) == nil"):
        return fail("sync_logs_when_no_pool_event_registered", "登录首次快照被事件缺失带累")
    ok("sync_logs_when_no_pool_event_registered")


def test_dump_sections():
    """dump 应含角色汇总、任务行、列头角色名、降级与未知计数。"""
    lua, gql, _ = sync_env([(71, 250000), (72, None)], extra=("Dump",))
    g = lua.globals()
    # 登录同步默认就抓，不需要先打开面板
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    g.GQL_FireTimers()          # 走真实去抖路径，而不是直调 SnapshotNow
    text = str(gql.Dump.Run())
    if "本周汇总" not in text:
        return fail("dump_sections", "缺少角色汇总段")
    if "任务71" not in text:
        return fail("dump_sections", "缺少金币任务行")
    if "小号一-永恒之井" not in text:
        return fail("dump_sections", "缺少列头角色名")
    if "未判定=1" not in text:
        return fail("dump_sections", "奖励接口取不到的任务 72 应计入未判定")
    if "降级" not in text:
        return fail("dump_sections", "缺少降级计数段")
    if gql.commands["dump"] is None:
        return fail("dump_sections", "/gql dump 未注册")
    ok("dump_sections")


def test_dump_shows_stale_char():
    """两周没上线的角色应标注快照过期，而不是显示成全部没做。"""
    lua, gql, _ = sync_env([(71, 250000)], extra=("Dump",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    g.GQL_NOW = 20 * 86400
    g.UnitName = lambda *a: "挂机号"
    g.GetRealmName = lambda: "永恒之井"
    gql.Sync.SnapshotNow()
    g.GQL_NOW = 20 * 86400 + 14 * 86400
    text = str(gql.Dump.Run())
    if "过期" not in text:
        return fail("dump_shows_stale_char", "久未同步的角色应显示过期标记")
    ok("dump_shows_stale_char")


def test_dump_uses_the_ledger_for_columns_and_time():
    """导出必须读 Ledger.MatrixColumns 与 Config.FormatTimeLeft，不再自己抄一份判据：
    "快照过期"的阈值和剩余时间的读法在两处各写一遍，改一处就出现矩阵与导出对不上。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "GoldQuestLedger", "Dump.lua"), encoding="utf-8").read()
    stale = []
    if re.search(r"STALE_SECONDS\s*=\s*7\s*\*\s*86400", src):
        stale.append("自己写了一份 STALE_SECONDS")
    if re.search(r"local function SpanLeft", src):
        stale.append("自己写了一份秒数→文字")
    if re.search(r"\(now\s*-\s*lastSync\)\s*>\s*STALE_SECONDS", src):
        stale.append("自己算快照过期")
    if "GQL.Ledger.MatrixColumns" not in src:
        stale.append("列头没走 Ledger.MatrixColumns")
    if stale:
        return fail("dump_uses_the_ledger_for_columns_and_time", "；".join(stale))
    ok("dump_uses_the_ledger_for_columns_and_time")


def test_dump_sections_unchanged_after_dedupe():
    """合并判据后导出的内容口径不许变：这几节本来就是判"账本记的是不是真实情况"的。"""
    lua, gql, _ = sync_env([(71, 250000), (72, None)], extra=("Dump",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    text = str(gql.Dump.Run())
    # 「完成 ?」不是笔误：这台机器问不出周重置 → cycleAnchor 停在 0，没有锚点就没有周统计，
    # 合并判据后这一格必须由 MatrixColumns 照样报 ?（报 0 才是谎报）
    for need in ("本周汇总", "金币任务", "小号一-永恒之井", "完成 ?", "未判定"):
        if need not in text:
            return fail("dump_sections_unchanged_after_dedupe",
                        f"导出缺少「{need}」:\n{text}")
    # 反方向也要钉住：锚点问得出的机器上这一格是**数字**，不是 ?。
    # 只断言无锚点报 ?，把 MatrixColumns 的 -1 判据写反（永远 -1）没人发现得出。
    lua2, gql2, _ = sync_env([(71, 250000)], weekly=432000, reset=1700060480,
                             extra=("Dump",))
    g2 = lua2.globals()
    g2.time = lambda: 1700000000
    lua2.execute("GQL.Api.WallClock = function() return 1700000000 end")
    gql2.Sync.SnapshotNow()
    text2 = str(gql2.Dump.Run())
    if "完成 0" not in text2:
        return fail("dump_sections_unchanged_after_dedupe",
                    "锚点问得出、本周确实没交过 → 该报数字 0，不该是 ?:\n" + text2)
    ok("dump_sections_unchanged_after_dedupe")


def test_dump_quest_explains_why_a_row_stays():
    """/gql quest 要一眼说清三件事：这行靠什么留在矩阵、为什么没有倒计时、会不会自己消失。
    关键词任务离开池子后，账本里的到期时刻被同步清空 → 没有任何机制会移除它，
    这正是用户实测"任务感觉结束了但还在，鼠标放上去也没倒计时"的机制。"""
    lua, gql, _ = sync_env([(71, 250000)], extra=("Dump",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    gql.Config.AddKeyword("任务71")
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from([
        lua.table_from({"questID": 72, "mapID": 2007, "name": "任务72", "money": 300000})])})
    gql.Sync.SnapshotNow()
    text = str(gql.Dump.Quest("任务71"))
    for want in ("在列", "不在池里", "命中关键词「任务71」",
                 "账本里没有到期时刻", "没有任何机制会把它移走"):
        if want not in text:
            return fail("dump_quest_explains_why_a_row_stays",
                        f"缺「{want}」：\n{text}")
    if gql.commands["quest"] is None:
        return fail("dump_quest_explains_why_a_row_stays", "/gql quest 未注册")
    ok("dump_quest_explains_why_a_row_stays")


def test_dump_names_which_chars_read_the_gold():
    """/gql quest 里那句金币归属要点名，不能只说"本轮另一档读到的"：12.1.5 实测 97128
    是同一个 questID 在两个号上奖励不同（一个 1090 金、一个 150 碎片），"另一档"会被玩家
    读成"我切了狩猎模式"，而真问题是这笔钱归谁 —— 报告是取证用的，归谁就写谁。"""
    lua, gql, _ = sync_env([(71, 10900000)], extra=("Dump",))
    g = lua.globals()
    gql.Sync.SnapshotNow()          # 小号一 读到 1090 金：按号读数 + 本轮金币粘滞都落账
    key = str(gql.Sync.CurrentCharKey())
    # 再喂一次"这一档客户端明说 0 金币"。0 只能从账本这头进：Api 有意把池行的 0
    # 当"数据还没到"去重问接口（那是松露大乱斗那条已定的口径），走不到这个形状。
    gql.Ledger.DiffPool(g.GQLDB, pool(lua, [(71, 0, None)]),
                        lua.execute("return GQLDB.chars[%r].lastSync" % key))
    text = str(gql.Dump.Quest("任务71"))
    if "本轮读到金币的号：小号一-永恒之井" not in text:
        return fail("dump_names_which_chars_read_the_gold", f"该点名金币是谁读到的:\n{text}")
    ok("dump_names_which_chars_read_the_gold")


def test_dump_quest_reports_real_expiry():
    """接口读到倒计时的任务：结论必须是"到点自动消失"，不能报成赖着不走；
    而且要把"这个数是谁答的"一起报出来 —— 上一版只有"接口读到过"一句话，
    周常被无参接口答成"还剩 2 小时"时无从看出是谁答的（追踪萨拉塔斯）。"""
    lua, gql, _ = sync_env([(71, 250000)], extra=("Dump", "Probe"))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    gql.Sync.SnapshotNow()
    text = str(gql.Dump.Quest("任务71"))
    if "还剩 2时0分" not in text or "来源=接口倒计时" not in text:
        return fail("dump_quest_reports_real_expiry", f"应报接口倒计时并带上来源：\n{text}")
    if "没有任何机制会把它移走" in text:
        return fail("dump_quest_reports_real_expiry", f"有到期时刻不该报永不消失：\n{text}")
    # 取证段：用户回传这一份就要能看出"日期是谁答错的"
    if "---- 倒计时是谁答的" not in text or "GetQuestLogTimeLeft" not in text:
        return fail("dump_quest_reports_real_expiry", f"应带倒计时取证段：\n{text}")
    if "---- 本 CD 结束时刻" not in text:
        return fail("dump_quest_reports_real_expiry", f"应报本 CD 什么时候结束：\n{text}")
    ok("dump_quest_reports_real_expiry")


def test_weekstats_unknown_when_clock_domains_split():
    """锚点是纪元秒、交任务时间戳是本次开机秒：直接比大小会静默算成"完成 0"。
    域不匹配时周汇总必须是未知，不能报一个看起来正常的数。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    epoch_anchor = 1700000000 - 604800
    L.OnTurnIn(db, "B-永恒之井", 3, 120, 500000, None)
    db.clockDomain = "uptime"
    count, _ = L.WeekStats(db, "B-永恒之井", epoch_anchor)
    if int(count) != -1:
        return fail("weekstats_unknown_when_clock_domains_split",
                    f"跨域应记未知（-1），实际 count={count}")
    db.clockDomain = "epoch"
    L.OnTurnIn(db, "B-永恒之井", 4, epoch_anchor + 10, 500000, None)
    count, gold = L.WeekStats(db, "B-永恒之井", epoch_anchor)
    if int(count) != 1 or int(gold) != 500000:
        return fail("weekstats_unknown_when_clock_domains_split",
                    f"同域后应照旧只数本 CD 内那条，实际 {count}/{gold}")
    ok("weekstats_unknown_when_clock_domains_split")


def test_dump_marks_week_total_unknown_across_domains():
    """无可信墙上时钟、锚点却是纪元秒：本周汇总只能显示 ?，报 0 是谎报。"""
    lua, gql, money = sync_env([(75, 500000)], extra=("Dump",))
    g = lua.globals()
    # 锚点是纪元秒、账本时间戳是开机秒（没有可信墙上时钟）→ 两个域不可比。
    # 直接写锚点：12.1.5 的 RefreshCycle 只认周重置接口，本机问不出时沿用旧值，
    # 所以"旧账本里有纪元锚点、这次开机问不到墙上时钟"是真实存在的组合。
    lua.execute("GQLDB.cycleAnchor = 1700000000")
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 75)
    money["v"] = 1500000
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    text = str(gql.Dump.Run())
    if "完成 ?" not in text:
        return fail("dump_marks_week_total_unknown_across_domains",
                    f"跨域应显示未知，实际:\n{text}")
    if "金币 ?" not in text:
        return fail("dump_marks_week_total_unknown_across_domains",
                    f"跨域时金币栏也要显示未知，实际:\n{text}")
    ok("dump_marks_week_total_unknown_across_domains")


def test_api_pool_shape_mismatch_is_not_empty():
    """接口返回了 N 条却一条 questID 都认不出，是形状不符不是空池。
    当成空池会走"全部消失"转移，把整张账本判成没做。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuestDB = lua.table_from({"GetWorldQuests": lambda: lua.table_from(
        {lua.table_from({"foo": 1}), lua.table_from({"bar": 2})})})
    pool, err, meta = gql.Api.GetPool()
    if pool is not None:
        return fail("api_pool_shape_mismatch_is_not_empty", f"形状不符不得返回池数据，实际 {pool}")
    if err is None or "形状" not in str(err):
        return fail("api_pool_shape_mismatch_is_not_empty", f"降级原因要说清形状不符，实际 {err}")

    # 老接口返回的是 questID 数组（纯数字），这同样是真数据，不能当成认不出
    g.C_WorldQuestDB = lua.table_from({"GetWorldQuests": lambda: lua.table_from({61, 62})})
    pool, err, meta = gql.Api.GetPool()
    if pool is None or len(pool) != 2:
        return fail("api_pool_shape_mismatch_is_not_empty", f"questID 数组应能吃下，实际 {pool} {err}")
    if int(pool[1]["questID"]) != 61:
        return fail("api_pool_shape_mismatch_is_not_empty", f"questID 应 61，实际 {pool[1]['questID']}")
    ok("api_pool_shape_mismatch_is_not_empty")


def test_api_pool_extra_candidates():
    """12.1.5 实测 C_WorldQuests.* 整个不存在，探测必须把已知的其它入口一起问一遍，
    否则用户每次 reload 只能多拿一行 MISSING。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    hits = []
    for i in range(1, len(gql.Api.CandidatePaths()) + 1):
        hits.append(str(gql.Api.CandidatePaths()[i]))
    joined = ",".join(hits)
    for name in ("GetWorldQuests", "C_QuestLog.IsQuestFlaggedCompleted"):
        if name not in joined:
            return fail("api_pool_extra_candidates", f"候选里应有 {name}，实际 {joined}")
    ok("api_pool_extra_candidates")


def test_probe_discovers_unknown_api_names():
    """12.1.5 实测两个池候选全 MISSING —— 猜名字没有意义。probe 必须能反过来问游戏：
    已知命名表里到底有哪些成员，把真名打回来才能接下一跳。
    _G 扫描也一并做，但要如实报出 _G 里到底有多少成员：WoW 的 API 未必挂在 _G 上，
    扫不到时这条计数就是"别再指望 _G"的证据。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.C_WorldQuestDB = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from({}),
        "GetWorldQuestInfo": lambda qid: None,
    })
    g.GetAvailableWorldQuests = lambda: None
    text = str(gql.Probe.Run())
    if "C_WorldQuestDB" not in text:
        return fail("probe_discovers_unknown_api_names", f"报告应枚举已知命名表:\n{text}")
    if "GetWorldQuestInfo" not in text:
        return fail("probe_discovers_unknown_api_names", f"应列出该表的成员名:\n{text}")
    if "GetAvailableWorldQuests" not in text:
        return fail("probe_discovers_unknown_api_names", f"应扫 _G 里带 WorldQuest 的名字:\n{text}")
    if "_G成员数" not in text:
        return fail("probe_discovers_unknown_api_names", f"应如实报 _G 扫描规模:\n{text}")
    ok("probe_discovers_unknown_api_names")


def test_probe_reports_reset_countdown_reading():
    """GetQuestResetTime=46509 而 GetTime=569772：报告要直接把两种读法并排写出来，
    不然下一个版本又会把它当绝对时间戳用。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.GetQuestResetTime = lambda: 46509
    g.GQL_NOW = 569772
    g.GetServerTime = lambda: 1792577090
    text = str(gql.Probe.Run())
    if "倒计时" not in text:
        return fail("probe_reports_reset_countdown_reading", f"报告应给出重置时间的读法判定:\n{text}")
    ok("probe_reports_reset_countdown_reading")


def test_api_frame_visible_accepts_frame_userdata():
    """真实客户端里帧对象不是 Lua table，是引擎对象（离线用 Python 对象模拟）。
    Api.FrameVisible 的回退路径若用 type(frame) == "table" 判，12.1.5 上
    FrameIsVisible 已实测 MISSING，回退又认不出帧 → "开过邮箱就作废"整条静默失效。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()

    class FakeFrame:
        def IsShown(self):
            return True

    g.MailFrame = FakeFrame()
    if gql.Api.FrameVisible("MailFrame") is not True:
        return fail("api_frame_visible_accepts_frame_userdata",
                    f"帧对象应能走 IsShown 回退，实际 {gql.Api.FrameVisible('MailFrame')}")
    if "帧可见性接口不可用" in str(gql.Api.LastDegrade()):
        return fail("api_frame_visible_accepts_frame_userdata",
                    f"回退成功不应记降级，实际 {gql.Api.LastDegrade()}")
    ok("api_frame_visible_accepts_frame_userdata")


def test_probe_reports_map_button_entries():
    """地图按钮要用 WorldMapFrame 那套入口，12.x 形态同样没实测过。
    现在就只判存在性、不真调用（调用有副作用），把可用名单带回来再写阶段二。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", mock=True)
    g = lua.globals()
    g.WorldMapFrame = lua.table_from({"RegisterButton": lambda *a: None})
    text = str(gql.Probe.Run())
    if "---- 阶段二入口" not in text:
        return fail("probe_reports_map_button_entries", f"报告应有阶段二入口段:\n{text}")
    for want in ("WorldMapFrame", "RegisterButton", "C_Map.GetMapInfo"):
        if want not in text:
            return fail("probe_reports_map_button_entries", f"应报出 {want} 的有无:\n{text}")
    ok("probe_reports_map_button_entries")


def test_dump_refuses_week_total_without_anchor():
    """锚点取不到时停在 0，而 Lua 里 0 是真值：WeekStats 的 r.t >= 0 恒真，
    历次 CD 的交任务会被全算进"本周"。没有锚点就只能显示 ?，不能给一个数。"""
    lua, gql, money = sync_env([(81, 500000)], extra=("Dump",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 81)
    money["v"] = 1500000
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    g.GQLDB["cycleAnchor"] = 0            # 锚点定不出来（GetQuestResetTime 缺失时就是这样）
    text = str(gql.Dump.Run())
    if "完成 ?" not in text:
        return fail("dump_refuses_week_total_without_anchor",
                    f"无锚点不得报数，实际:\n{text}")
    ok("dump_refuses_week_total_without_anchor")


def test_matrix_rows_group_by_map_and_filter_by_gold_floor():
    """矩阵只收"金额到了门槛"的任务：<1 金的杂鱼、金额没读到的、明确不给金币的一律不出现；
    达标任务按地图分组、组内按收益从高到低。
    门槛本身是配置项（第十五轮反馈①"门槛就要提到 500g 或者可以配置"，默认 500 金），
    这条用例只验分组与排序，取的都是远超/远低于任何门槛的值。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["chars"] = lua.table_from({"A-永恒之井": lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})})
    L.DiffPool(db, pool_map(lua, [(11, 206, 25000000), (12, 206, 10000000),
                                  (13, 205, None), (14, 205, 0),
                                  (15, 207, 500000)]), 1000)
    groups = L.MatrixRows(db)
    if len(groups) != 1:
        return fail("matrix_rows_group_by_map_and_filter_by_gold_floor",
                    f"应只有地图 206 一组，实际 {len(groups)} 组")
    if int(groups[1]["mapID"]) != 206 or len(groups[1]["rows"]) != 2:
        return fail("matrix_rows_group_by_map_and_filter_by_gold_floor",
                    f"应按地图 206 收 2 行，实际 {groups[1]['mapID']}/{len(groups[1]['rows'])}")
    if int(groups[1]["rows"][1]["questID"]) != 11 or int(groups[1]["rows"][2]["questID"]) != 12:
        return fail("matrix_rows_group_by_map_and_filter_by_gold_floor",
                    "同图内应按收益从高到低")
    if str(groups[1]["rows"][1]["state"]["A-永恒之井"]) != "open":
        return fail("matrix_rows_group_by_map_and_filter_by_gold_floor",
                    f"池内未做应 open，实际 {groups[1]['rows'][1]['state']}")
    shown = set()
    for gi in range(1, len(groups) + 1):
        for ri in range(1, len(groups[gi]["rows"]) + 1):
            shown.add(int(groups[gi]["rows"][ri]["questID"]))
    for hidden in (13, 14, 15):
        if hidden in shown:
            return fail("matrix_rows_group_by_map_and_filter_by_gold_floor",
                        f"任务 {hidden}（未判定/非金币/<100g）不应进矩阵")
    if int(L.QuestMoney(db, 11)) != 25000000:
        return fail("matrix_rows_group_by_map_and_filter_by_gold_floor", "money 应取当前签名")
    ok("matrix_rows_group_by_map_and_filter_by_gold_floor")


def test_format_gold_icon():
    """矩阵界面金额用金银铜图标（|T 转义），不再用 g/s/c 字母；
    文字版 FormatGold 仍留给 dump/日志。转义必须是 路径:高:宽 三段式 ——
    只写高不写宽客户端不认，会把整串转义原样印出来（用户截图实测过）。"""
    _, gql = load_lua("Config")
    gold = gql.Config.FormatGoldIcon
    g_icon = r"|TInterface\MoneyFrame\UI-GoldIcon:12:12|t"
    s_icon = r"|TInterface\MoneyFrame\UI-SilverIcon:12:12|t"
    c_icon = r"|TInterface\MoneyFrame\UI-CopperIcon:12:12|t"
    if str(gold(12345)) != f"1{g_icon}23{s_icon}":
        return fail("format_gold_icon", f"12345 铜=1金23银，应只显示金银两位，实际 {gold(12345)}")
    if str(gold(25000000)) != f"2500{g_icon}0{s_icon}":
        return fail("format_gold_icon", f"25000000 铜应 2500金图标，实际 {gold(25000000)}")
    if str(gold(None)) != "?":
        return fail("format_gold_icon", "nil 必须显示 ?（未知）")
    if str(gold(-1)) != "?":
        return fail("format_gold_icon", "-1（未知哨兵）应显示 ?")
    if str(gold(0)) != f"0{c_icon}":
        return fail("format_gold_icon", "0 应显示 0 铜图标")
    ok("format_gold_icon")


def test_api_midnight_map_by_name():
    """至暗之夜四个区域（永歌森林/祖阿曼/哈籁恩达尔/虚影风暴，暴雪官网确认）
    放行，旧资料片图（苏拉玛、瓦尔莎拉）滤掉；地图名取不到时 nil=不滤（宁多勿漏）。
    按本地化地图名判定而非 mapID：mapID 跨小版本会变，名字稳定，
    且矩阵分组头本来就用 MapName，子区域图（阿塔阿曼）也能一并兜住。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    names = {2395: "永歌森林", 2393: "虚影风暴", 2400: "祖阿曼",
             2401: "哈籁恩达尔", 680: "苏拉玛", 641: "瓦尔莎拉"}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": names[int(mid)]})
        if int(mid) in names else None,
    })
    for mid, want in ((2395, True), (2393, True), (2400, True), (2401, True),
                      (680, False), (641, False)):
        if gql.Api.IsMidnightMap(mid) is not want:
            return fail("api_midnight_map_by_name",
                        f"地图 {mid}({names[mid]}) 应判 {want}，实际 {gql.Api.IsMidnightMap(mid)}")
    if gql.Api.IsMidnightMap(9999) is not None:
        return fail("api_midnight_map_by_name", "地图名取不到必须 nil（不滤）")
    if gql.Api.IsMidnightMap(None) is not None:
        return fail("api_midnight_map_by_name", "无 mapID 必须 nil")
    ok("api_midnight_map_by_name")


def test_map_name_cached_per_session():
    """Api.MapName 一张图一次登录只问一次接口：矩阵分组头、本资料片判定、池行补图
    每轮都拿同一批 mapID 反复问，之前每次都落到 C_Map（审计实测一次快照 100~400 次）。
    地图名一个会话内不变，问出来一次就该记住 —— 这张表是纯砍重复，不许多问。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    asked = []

    def map_info(mid):
        asked.append(int(mid))
        return lua.table_from({"name": "永歌森林"})

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    vals = [str(gql.Api.MapName(2395)) for _ in range(5)]
    if asked != [2395]:
        return fail("map_name_cached_per_session",
                    f"同一张图连问 5 次只该落到接口 1 次，实际问过 {asked}")
    if vals != ["永歌森林"] * 5:
        return fail("map_name_cached_per_session", f"缓存后的 5 次返回值应相同，实际 {vals}")
    ok("map_name_cached_per_session")


def test_map_parent_cached_per_session():
    """钉住平方项被砍掉：Sync.PickPinMap 候选图两两问父图判孤儿是 O(#图²)，
    10 张图各问 10 次 = 100 次询问。落到底层接口的次数必须是「每张图一次」= 10。
    关键在没父图的那 3 张（大洲图）也要落表 —— 它们每次都答 nil，nil 不进表就是
    下一轮照旧重问 #kept 次，光缓存"问到的父图号"根本砍不掉这条。
    同时哨兵不许漏进调用方：那 3 张每次拿到的都必须是 nil（全仓唯一调用方
    Sync.lua:603 只把返回值和数字比大小）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    ids = list(range(2400, 2410))
    no_parent = {2400, 2403, 2407}
    asked = []

    def map_info(mid):
        mid = int(mid)
        asked.append(mid)
        row = {"name": "图%d" % mid}
        if mid not in no_parent:
            row["parentMapID"] = 948
        return lua.table_from(row)

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    for _ in range(10):
        for mid in ids:
            got = gql.Api.MapParentID(mid)
            if mid in no_parent:
                if got is not None:
                    return fail("map_parent_cached_per_session",
                                f"{mid} 没有父图，对外必须回 nil（false 哨兵不许漏出去），实际 {got}")
            elif got is None or int(got) != 948:
                return fail("map_parent_cached_per_session",
                            f"{mid} 的父图应每次都回 948，实际 {got}")
    if len(asked) != len(ids):
        return fail("map_parent_cached_per_session",
                    f"10 张图各问 10 次只该落接口 10 次，实际 {len(asked)} 次——平方项没砍掉")
    ok("map_parent_cached_per_session")


def test_map_name_nil_is_not_frozen():
    """问不出一律不落表（MapName 与 MapParentID 同一条口径）：接口报错、回 nil、
    回的不是表，三种都只算"这台客户端这一轮没说"。登录早期地图数据可能还没就绪，
    把 nil 冻进缓存等于这张图整场没名字；父图那侧更糟——缓存里的 nil 会被
    PickPinMap 当成"确实没有父图"，整张图整场被判成孤儿。
    换成答得出的替身必须立刻问得出（新替身真被调用过一次 = 上一轮没落表）。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()

    def boom(mid):
        raise RuntimeError("地图数据没就绪")

    g.C_Map = lua.table_from({"GetMapInfo": boom})
    if gql.Api.MapName(2395) is not None:
        return fail("map_name_nil_is_not_frozen", "接口报错时 MapName 必须回 nil")
    if gql.Api.MapParentID(2395) is not None:
        return fail("map_name_nil_is_not_frozen", "接口报错时 MapParentID 必须回 nil")

    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: None if int(mid) != 2395
        else lua.table_from({"name": "永歌森林", "parentMapID": 2413}),
    })
    if str(gql.Api.MapName(2395)) != "永歌森林":
        return fail("map_name_nil_is_not_frozen", "报错不该冻进表，换替身后必须立刻问得出名字")
    if int(gql.Api.MapParentID(2395)) != 2413:
        return fail("map_name_nil_is_not_frozen", "报错不该冻进表，换替身后必须立刻问得出父图")
    # 回 nil 与回非表同样不落表：换回能答的替身还得问得出
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: "不是一张表"})
    if gql.Api.MapParentID(2400) is not None:
        return fail("map_name_nil_is_not_frozen", "回的不是表时 MapParentID 必须回 nil")
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "虚影风暴", "parentMapID": 2412}),
    })
    if int(gql.Api.MapParentID(2400)) != 2412:
        return fail("map_name_nil_is_not_frozen", "回非表也不许落表")
    ok("map_name_nil_is_not_frozen")


def test_getpool_enumerates_all_maps_without_player_location():
    """人在哪不该影响看到哪些任务（用户实测 + Tercioo WQT 都不依赖位置：
    它硬编码全部 zone mapID 逐图问）。GetBestMapForUnit 缺失（拿不到位置）时，
    只要能从世界根图（GetFallbackWorldMapID）枚举出地图树，两个大陆的任务都收得齐。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None

    def children(mid):
        mid = int(mid)
        if mid == 946:
            return lua.table_from({lua.table_from({"mapID": 2000, "mapType": 2}),
                                   lua.table_from({"mapID": 3000, "mapType": 2})})
        if mid == 2000:
            return lua.table_from({lua.table_from({"mapID": 2001, "mapType": 3})})
        if mid == 3000:
            return lua.table_from({lua.table_from({"mapID": 3001, "mapType": 3})})
        return lua.table_from({})

    g.C_Map = lua.table_from({
        "GetFallbackWorldMapID": lambda: 946,
        "GetMapChildrenInfo": children,
    })

    def quests_on_map(mid):
        mid = int(mid)
        if mid == 2001:
            return lua.table_from({91001})
        if mid == 3001:
            return lua.table_from({92001})
        return lua.table_from({})

    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": quests_on_map})
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_enumerates_all_maps_without_player_location", f"池应可用，err={err}")
    ids = sorted(int(pool[i]["questID"]) for i in range(1, len(pool) + 1))
    if ids != [91001, 92001]:
        return fail("getpool_enumerates_all_maps_without_player_location",
                    f"两个大陆的任务都应收进来，实际 {ids}")
    if meta["partial"] is not True:
        return fail("getpool_enumerates_all_maps_without_player_location",
                    f"按图源必须标 partial，meta={meta}")
    ok("getpool_enumerates_all_maps_without_player_location")


def test_getpool_requests_reward_preload_when_money_missing():
    """学 Tercioo WQT：奖励数据不在本地时 GetQuestLogRewardMoney 读出来是 0，
    看着像非金币任务其实是数据没到。读空必须 C_TaskQuest.RequestPreloadRewardData
    要一次，下轮快照才能读到真金额 —— 不请求的话金额永远空着。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.C_Map = lua.table_from({"GetBestMapForUnit": lambda unit: 2007})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    preloaded = []

    def preload(qid):
        preloaded.append(int(qid))
        return None

    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({92120}),
        "RequestPreloadRewardData": preload,
    })
    g.HaveQuestRewardData = lambda qid: False
    g.GetQuestLogRewardMoney = lambda qid: 0
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_requests_reward_preload_when_money_missing", f"池应可用，err={err}")
    if preloaded != [92120]:
        return fail("getpool_requests_reward_preload_when_money_missing",
                    f"金额读空应请求预加载，实际 preloaded={preloaded}")
    ok("getpool_requests_reward_preload_when_money_missing")


def test_getpool_zero_money_row_falls_through_to_reward_chain():
    """池行自己写着 money=0 时必须继续问金额链：0 是"奖励数据还没到"的默认返回值，
    不是"这条不给金币"。上一版 `if v then return v end` 把 0 当成读到了，后面接口一次没问，
    12.1.5 反馈③"蛇岛的松露大乱斗是金币任务，矩阵里却没有"就卡死在 100g 门槛下。
    同时验金额链不止一条：第一条（GetQuestLogRewardMoney）答 0 时后面的候选还要有机会答。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = lua.table_from({
        "GetWorldQuests": lambda: lua.table_from([
            lua.table_from({"questID": 95484, "mapID": 2395,
                            "name": "松露大乱斗", "money": 0}),
        ])
    })
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.C_QuestRewards = lua.table_from({"GetCachedQuestMoney": lambda qid: 154800})
    pool, err, _ = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_zero_money_row_falls_through_to_reward_chain", f"池应可用，err={err}")
    row = pool[1]
    if int(row["money"]) != 154800:
        return fail("getpool_zero_money_row_falls_through_to_reward_chain",
                    f"行里的 0 不算答案，应继续问到金额链给出 154800，实际 {row['money']}")

    # 全链都答不出（非金币任务 / 数据没到）→ 留 nil，不许写成 0 参与门槛比较
    g.C_QuestRewards = None
    pool2, _, _ = gql.Api.GetPool()
    if pool2[1]["money"] is not None:
        return fail("getpool_zero_money_row_falls_through_to_reward_chain",
                    f"金额问不出应留 nil，实际 {pool2[1]['money']}")
    ok("getpool_zero_money_row_falls_through_to_reward_chain")


def test_matrix_columns_stale_and_manual_only_quest_kept():
    """列头：超 7 天没登录的角色要标过期；锚点定不出来时完成数记 ?（沿用 Dump 口径）。
    行：只剩手动记录、已从池里消失的任务必须留在矩阵里，否则补录的记录会凭空蒸发。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 5000
    db["chars"] = lua.table_from({
        "A-永恒之井": lua.table_from({"lastSync": 860400, "views": lua.table_from({})}),
        "C-旧名字": lua.table_from({"lastSync": 864000 - 8 * 86400, "views": lua.table_from({})}),
        "B-永恒之井": lua.table_from({"lastSync": 0, "views": lua.table_from({})}),
    })
    L.DiffPool(db, pool_map(lua, [(21, 206, 30000000)]), 6000)
    sig = str(db["quests"][21]["currentSig"])
    L.OnTurnIn(db, "A-永恒之井", 21, 6100, 30000000, sig)
    db["quests"][22] = lua.table_from({"name": "已经刷没了", "observations": lua.table_from({})})
    L.SetManual(db, "B-永恒之井", 22, "done", 6100, 20000000)

    cols = L.MatrixColumns(db, 864000)
    by_key = {}
    for i in range(1, len(cols) + 1):
        by_key[str(cols[i]["key"])] = cols[i]
    if by_key["C-旧名字"]["stale"] is not True:
        return fail("matrix_columns_stale_and_manual_only_quest_kept", "8 天没登录应标过期")
    if by_key["A-永恒之井"]["stale"] is not None:
        return fail("matrix_columns_stale_and_manual_only_quest_kept", "刚同步的不该被误标过期")
    if by_key["B-永恒之井"]["stale"] is not None:
        # lastSync=0 是"这个列从没同步过"，不是"快照过期"——两种状态在 UI 上必须分开
        return fail("matrix_columns_stale_and_manual_only_quest_kept", "没同步过的列不该标过期")
    if int(by_key["A-永恒之井"]["count"]) != 1 or int(by_key["A-永恒之井"]["gold"]) != 30000000:
        return fail("matrix_columns_stale_and_manual_only_quest_kept",
                    f"本周应 1/30000000，实际 {by_key['A-永恒之井']['count']}/{by_key['A-永恒之井']['gold']}")
    if int(by_key["B-永恒之井"]["count"]) != 1:
        return fail("matrix_columns_stale_and_manual_only_quest_kept", "手动补录要计入本周完成数")
    db["cycleAnchor"] = 0
    cols = L.MatrixColumns(db, 864000)
    if int(cols[1]["count"]) != -1:
        return fail("matrix_columns_stale_and_manual_only_quest_kept",
                    f"无锚点不得报周数，实际 {cols[1]['count']}")

    groups = L.MatrixRows(db)
    ids = {}
    for gi in range(1, len(groups) + 1):
        rows = groups[gi]["rows"]
        for ri in range(1, len(rows) + 1):
            ids[int(rows[ri]["questID"])] = rows[ri]
    if 22 not in ids:
        return fail("matrix_columns_stale_and_manual_only_quest_kept",
                    f"只剩手动记录的任务应留在矩阵，实际行={sorted(ids)}")
    if str(ids[22]["state"]["B-永恒之井"]) != "done":
        return fail("matrix_columns_stale_and_manual_only_quest_kept",
                    f"手动状态要如实显示，实际 {ids[22]['state']}")
    if str(ids[21]["state"]["A-永恒之井"]) != "done":
        return fail("matrix_columns_stale_and_manual_only_quest_kept",
                    f"实测交任务应显示 done，实际 {ids[21]['state']}")
    ok("matrix_columns_stale_and_manual_only_quest_kept")


def test_matrix_renders_rows_and_week_totals():
    """矩阵要有任务行、单元格状态（完成=打勾图标）。底部本周合计/金币列已移除
    （用户要求矩阵只反映"谁做了哪个任务"，金币与计数不再进矩阵）。"""
    lua, gql, money = sync_env([(91, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 100000            # 锚点是 700000-604800=95200，交任务必须落在本周内
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 91)
    money["v"] = 500000               # 窗口里反而少钱 → 整笔作废，g 记 nil
    g.GQL_NOW = 100020
    gql.Sync.FinishWindow()
    n = gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if int(n) != 1:
        return fail("matrix_renders_rows_and_week_totals", f"应渲染 1 行，实际 {n}\n{joined}")
    if "任务91" not in joined:
        return fail("matrix_renders_rows_and_week_totals", f"缺任务行:\n{joined}")
    if "ReadyCheck-Ready" not in joined:
        return fail("matrix_renders_rows_and_week_totals",
                    f"单元格应显示完成图标(ReadyCheck-Ready):\n{joined}")
    ok("matrix_renders_rows_and_week_totals")


def test_matrix_text_pool_never_borrows_another_font():
    """文字池不许跨字体复用条目（用户 12.1.5 实测：新加一条任务之后，某行任务名突然
    变成地图组头那种大号字，整块看着像错了行；点"刷新"也没用，换号登录才恢复）。
    根因：FontString 建好之后 SetText 换不掉字体，而组头（大号）和任务名（小一号）
    原来共用一条池子 —— 行序一变，第 N 条就会被另一种行领走。
    用例把一条任务挪到另一张图，让新出现的组头正好落在上一轮任务名那条串上。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    gql.Matrix.Render()
    lua.execute("GQLDB.quests[102].mapID = 2260")     # 换图 → 多出一个地图组头
    gql.Matrix.Render()
    shown = [(t, f) for t, f, v in font_strings(g.GQLMatrixContentL) if v]
    heads = [t for t, f in shown if "|cffffd100" in t]
    tasks = [t for t, f in shown if "任务10" in t]
    if len(heads) != 2 or len(tasks) != 2:
        return fail("matrix_text_pool_never_borrows_another_font",
                    f"前置没搭起来：应有 2 个组头 + 2 行任务，实际 {heads} / {tasks}")
    bad = []
    for text, font in shown:
        want = "GameFontNormalLarge" if "|cffffd100" in text else (
            "GameFontHighlight" if "任务10" in text else None)
        if want and font != want:
            bad.append("%r 字体=%s，应为 %s" % (text[:24], font, want))
    if bad:
        return fail("matrix_text_pool_never_borrows_another_font",
                    "复用条目串了字体（刷新修不好，只有重建帧才恢复）:\n" + "\n".join(bad))
    ok("matrix_text_pool_never_borrows_another_font")


def test_matrix_empty_ledger_shows_placeholder():
    """账本还没数据（池接口没通 / 首次登录）时矩阵不能画成一片空白：
    用户要能一眼分清"没数据"和"渲染坏了"。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Matrix", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    n = gql.Matrix.Render()
    if int(n) != 0:
        return fail("matrix_empty_ledger_shows_placeholder", f"空账本应 0 行，实际 {n}")
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "还没有" not in joined:
        return fail("matrix_empty_ledger_shows_placeholder", f"空账本要有占位文案:\n{joined}")
    ok("matrix_empty_ledger_shows_placeholder")


def test_map_button_opens_matrix():
    """小地图要有按钮，点它开矩阵、再点关。按钮必须挂 Minimap 才永远可见。"""
    lua, gql, _ = sync_env([(93, 250000)], extra=("Matrix",))
    btn = gql.Matrix.EnsureMapButton()
    if btn is None:
        return fail("map_button_opens_matrix", "Minimap 存在却没建按钮")
    if not lua.execute("return GQLMapButton.__parent == Minimap"):
        return fail("map_button_opens_matrix", "按钮父层不是 Minimap")
    if bool(lua.execute("return GQLMatrixFrame ~= nil and GQLMatrixFrame.__shown")):
        return fail("map_button_opens_matrix", "建按钮不该顺手把矩阵打开")
    click = btn.__scripts["OnClick"]
    click()
    if not bool(lua.execute("return GQLMatrixFrame.__shown")):
        return fail("map_button_opens_matrix", "点按钮没打开矩阵")
    click()
    if bool(lua.execute("return GQLMatrixFrame.__shown")):
        return fail("map_button_opens_matrix", "再点应关闭矩阵")
    if gql.commands["matrix"] is None:
        return fail("map_button_opens_matrix", "/gql matrix 未注册")
    ok("map_button_opens_matrix")


def test_map_button_degrades_without_minimap():
    """12.x 若 Minimap 缺失（理论上不会），按钮只能不建并记降级，绝不报错。
    降级必须留痕，否则用户只会看到"怎么没有按钮"而查不出原因。"""
    lua, gql, _ = sync_env([(94, 250000)], extra=("Matrix",))
    lua.globals().Minimap = None
    if gql.Matrix.EnsureMapButton() is not None:
        return fail("map_button_degrades_without_minimap", "宿主缺失应返回 nil")
    if "地图按钮降级" not in str(gql.Config.GetLogs()):
        return fail("map_button_degrades_without_minimap",
                    f"降级未记日志:\n{gql.Config.GetLogs()}")
    ok("map_button_degrades_without_minimap")


def fire_pew(lua):
    """走 Core 自己的 PLAYER_ENTERING_WORLD 分发，而不是直调各模块入口：
    用户实测"打开地图看不见按钮"的根因就是没人调用 EnsureMapButton，
    只有从登录事件这条真路径打，才能验出"入口漏接"。"""
    core = lua.execute("return GQLFrame")
    handler = core.__scripts["OnEvent"]
    handler(core, "PLAYER_LOGIN")
    handler(core, "PLAYER_ENTERING_WORLD")


def test_login_creates_minimap_button():
    """登录后必须自动建出小地图按钮，用户不用敲命令。"""
    lua, gql, _ = sync_env([(95, 250000)], extra=("Matrix",))
    if lua.execute("return GQLMapButton ~= nil"):
        return fail("login_creates_minimap_button", "加载阶段就建按钮会把 CreateFrame 提前")
    fire_pew(lua)
    if not lua.execute("return GQLMapButton ~= nil"):
        return fail("login_creates_minimap_button", f"登录后没建按钮:\n{gql.Config.GetLogs()}")
    if not lua.execute("return GQLMapButton.__parent == Minimap"):
        return fail("login_creates_minimap_button", "按钮父层不是 Minimap")
    ok("login_creates_minimap_button")


def test_login_retries_button_when_minimap_appears_later():
    """12.x 可能延迟创建 Minimap：第一次没宿主只能记降级，
    下次进入世界要能补建，而不是永久没按钮。"""
    lua, gql, _ = sync_env([(95, 250000)], extra=("Matrix",))
    lua.globals().Minimap = None
    fire_pew(lua)                       # 不该抛错
    if lua.execute("return GQLMapButton ~= nil"):
        return fail("login_retries_button_when_minimap_appears_later",
                    "宿主缺失时不该凭空造出按钮")
    lua.execute("Minimap = CreateFrame('Frame', 'Minimap', UIParent); Minimap:SetSize(160, 160)")
    fire_pew(lua)
    if not lua.execute("return GQLMapButton ~= nil and GQLMapButton.__parent == Minimap"):
        return fail("login_retries_button_when_minimap_appears_later",
                    f"宿主出现后应补建按钮:\n{gql.Config.GetLogs()}")
    ok("login_retries_button_when_minimap_appears_later")


def test_minimap_button_inherits_host_strata():
    """按钮不许自己设 FrameStrata：子帧默认跟随 Minimap 父层（小地图按钮标准做法）。
    写成独立一层反而可能被其它环上图标或地图盖住，继承是唯一不猜的写法。"""
    lua, gql, _ = sync_env([(95, 250000)], extra=("Matrix",))
    gql.Matrix.EnsureMapButton()
    if lua.execute("return GQLMapButton.__strata ~= nil"):
        return fail("minimap_button_inherits_host_strata",
                    "按钮覆盖了 FrameStrata，应跟随 Minimap 父层")
    if "地图按钮已建" not in str(gql.Config.GetLogs()):
        return fail("minimap_button_inherits_host_strata",
                    f"建成要留痕，否则看不见按钮时无从排查:\n{gql.Config.GetLogs()}")
    ok("minimap_button_inherits_host_strata")


def test_minimap_button_raised_above_minimap():
    """小地图按钮必须抬到 Minimap 之上，否则被其它环上图标盖住。"""
    lua, gql, _ = sync_env([(95, 250000)], extra=("Matrix",))
    lua.execute("Minimap.__framelevel = 500")
    gql.Matrix.EnsureMapButton()
    if not lua.execute("return GQLMapButton.__framelevel > 500"):
        lvl = lua.execute("return GQLMapButton.__framelevel")
        return fail("minimap_button_raised_above_minimap",
                    f"按钮层级 {lvl} 未高于 Minimap 500")
    if not lua.execute("return GQLMapButton.__parent == Minimap"):
        return fail("minimap_button_raised_above_minimap", "按钮父层必须仍是 Minimap")
    ok("minimap_button_raised_above_minimap")


def test_minimap_button_has_coin_icon():
    """按钮 = 以前的金币素材铺满（用户："图标最好换回以前金币的图标"，
    试过暴雪小地图追踪环当边框，12.1.5 实测画出来是"方块图标 + 左上角一个小圆环"，
    用户回图说"不对哦"并明确"如果不好做成圆形，方块的也行，就这么大"）。
    尺寸 20：比截图里那颗略小一点，用户嫌 32 太大、盖住地图内容。"""
    lua, gql, _ = sync_env([(95, 250000)], extra=("Matrix",))
    gql.Matrix.EnsureMapButton()
    normal = one(lua.execute("return GQLMapButton.__normal"))
    if "INV_Misc_Coin_01" not in normal:
        return fail("minimap_button_has_coin_icon", f"图标要换回以前的金币素材，实际 {normal}")
    if "TrackingBorder" in normal:
        return fail("minimap_button_has_coin_icon",
                    "追踪环当边框实测画成方块加左上角小圆，别再放回去")
    if not lua.execute("return GQLMapButton.__highlight == 'Interface\\\\Buttons\\\\UI-Common-MouseHilight'"):
        hl = one(lua.execute("return GQLMapButton.__highlight"))
        return fail("minimap_button_has_coin_icon", f"高亮要圆形外圈，实际 {hl}")
    w, h = lua.execute("return GQLMapButton:GetWidth(), GQLMapButton:GetHeight()")
    if int(w) > 22 or int(h) > 22:
        return fail("minimap_button_has_coin_icon", f"按钮还是太大：{w}x{h}")
    info = str(gql.Matrix.ButtonInfo())
    if "纹理=" not in info or "已建" not in info:
        return fail("minimap_button_has_coin_icon",
                    f"probe 要报得出按钮纹理，实际 {info}")
    ok("minimap_button_has_coin_icon")


def test_map_name_goes_through_api_and_matrix_falls_back_to_id():
    """地图名只能从 Api 拿（pcall + 取不到返回 nil），矩阵不许直接摸 C_Map；
    接口缺失时标题回退"地图 <id>"，不能留一个空白分组让人不知道看的是哪张图。"""
    def map_info(mid):
        # 真接口对不存在的 mapID 返回 nil；替身要是恒定返回名字，就验不出"编造地图名"这条
        return lua.table_from({"name": "虚影风暴"}) if int(mid) == 2007 else None

    lua, gql, money = sync_env([(96, 25000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    if str(gql.Api.MapName(2007)) != "虚影风暴":
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    f"应取到地图名，实际 {gql.Api.MapName(2007)}")
    if gql.Api.MapName(9999) is not None:
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    "查不到的 mapID 必须返回 nil，返回字符串会把不存在的地图名编出来")
    # 接口中途没了：从没问过的图回 nil（不报错），问过的图仍用记住的名字 ——
    # 会话级缓存的口径（地图名一场里不会变，见 map_name_cached_per_session）。
    # 不许把这条改成"期望 nil"：那等于要求缓存每轮重问一遍。
    g.C_Map = None
    if gql.Api.MapName(2444) is not None:
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    "C_Map 整个缺失时没问过的 mapID 应返回 nil 而不是报错")
    if str(gql.Api.MapName(2007)) != "虚影风暴":
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    "本场问出过名字的图，接口断了也不该退回空白（缓存没记住就是白问）")

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    gql.Sync.SnapshotNow()
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "虚影风暴" not in joined:
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    f"分组标题应显示地图名:\n{joined}")
    # 接口整场问不出才走"地图 <id>"回退：另开一场（新 LuaRuntime = 新会话，
    # 缓存里没有这张图），C_Map 从头就不在。
    lua2, gql2, _ = sync_env([(96, 25000000)], extra=("Matrix",))
    g2 = lua2.globals()
    g2.C_Map = None
    gql2.Sync.SnapshotNow()
    gql2.Matrix.Render()
    joined2 = "\n".join(frame_texts(g2.GQLMatrixFrame))
    if "地图 2007" not in joined2:
        return fail("map_name_goes_through_api_and_matrix_falls_back_to_id",
                    f"接口整场缺失应回退地图号:\n{joined2}")
    ok("map_name_goes_through_api_and_matrix_falls_back_to_id")


def test_matrix_skips_old_expansion_maps():
    """非本资料片的旧图（苏拉玛们）整组不渲染（用户实测要求）；
    地图名判不出来（接口挂）时不滤，宁多勿漏。判定走本地化地图名。"""
    lua, gql, _ = sync_env([(97, 30000000)], extra=("Matrix",))
    g = lua.globals()
    rows = lua.table_from({
        lua.table_from({"questID": 97, "mapID": 2007, "name": "任务97",
                        "money": 30000000}),
        lua.table_from({"questID": 98, "mapID": 2099, "name": "任务98",
                        "money": 30000000}),
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    names = {2007: "虚影风暴", 2099: "苏拉玛"}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": names[int(mid)]})
        if int(mid) in names else None,
    })
    gql.Sync.SnapshotNow()
    n = gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if int(n) != 1:
        return fail("matrix_skips_old_expansion_maps",
                    f"只应渲染本资料片 1 行，实际 {n}\n{joined}")
    if "任务98" in joined:
        return fail("matrix_skips_old_expansion_maps",
                    f"旧资料片地图 2099 的任务不该出现:\n{joined}")
    if "任务97" not in joined:
        return fail("matrix_skips_old_expansion_maps",
                    f"本资料片任务 97 应照常显示:\n{joined}")
    # 地图名全取不到（全新环境，不带判定缓存）→ 不滤，旧图任务也显示（宁多勿漏）
    lua2, gql2, _ = sync_env([(97, 30000000)], extra=("Matrix",))
    g2 = lua2.globals()
    rows2 = lua2.table_from({
        lua2.table_from({"questID": 97, "mapID": 2007, "name": "任务97",
                         "money": 30000000}),
        lua2.table_from({"questID": 98, "mapID": 2099, "name": "任务98",
                         "money": 30000000}),
    })
    g2.C_WorldQuests = lua2.table_from({"GetWorldQuests": lambda: rows2})
    g2.C_Map = lua2.table_from({"GetMapInfo": lambda mid: None})
    gql2.Sync.SnapshotNow()
    n = gql2.Matrix.Render()
    if int(n) != 2:
        return fail("matrix_skips_old_expansion_maps",
                    f"判不出地图名时不该过滤，实际 {n} 行")
    ok("matrix_skips_old_expansion_maps")


def test_matrix_toggle_gates_background_sync():
    """登录抓一次（默认开），之后只有矩阵开着才继续抓池：关上面板后事件驱动的
    快照整个跳过（用户实测平时抓池会卡）。Toggle 打开=开轮询；关闭=停轮询。"""
    lua, gql, _ = sync_env([(81, 30000000)], extra=("Matrix",))
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    if lua.execute("return next(GQLDB.chars) == nil"):
        return fail("matrix_toggle_gates_background_sync", "登录那一次快照没跑（默认开）")
    if len(calls) != 1:
        return fail("matrix_toggle_gates_background_sync",
                    f"登录同步只该抓一次，实际 {len(calls)}")
    gql.Matrix.Toggle()
    if gql.Sync.Polling() is not True:
        return fail("matrix_toggle_gates_background_sync", "矩阵开着时应轮询")
    gql.Matrix.Toggle()
    if gql.Sync.Polling() is not False:
        return fail("matrix_toggle_gates_background_sync", "关上面板应停轮询")
    fired = len(calls)
    fire(gql, "QUEST_LOG_UPDATE")
    g.GQL_NOW = 60
    lua.globals().GQL_FireTimers()
    if len(calls) != fired:
        return fail("matrix_toggle_gates_background_sync",
                    f"关上面板后不该再抓池，多抓 {len(calls) - fired} 次")
    ok("matrix_toggle_gates_background_sync")


def test_login_snapshot_runs_without_polling():
    """用户 2026-10-25 定的口径：登录抓一次池（默认开），之后矩阵没打开就完全静默。
    抓池只经过去抖路径，仍是一次读取，不加第二个定时器。"""
    lua, gql, _ = sync_env([(81, 30000000)])
    g = lua.globals()
    calls = []
    real = g.C_WorldQuests.GetWorldQuests

    def counting():
        calls.append(1)
        return real()

    g.C_WorldQuests = lua.table_from({"GetWorldQuests": counting})
    gql.Sync.OnWorldEnter()          # 不调 SetPolling：面板没开
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    if lua.execute("return next(GQLDB.chars) == nil"):
        return fail("login_snapshot_runs_without_polling",
                    f"登录那次快照没跑:\n{gql.Config.GetLogs()}")
    if len(calls) != 1:
        return fail("login_snapshot_runs_without_polling",
                    f"登录同步应只抓一次池，实际 {len(calls)} 次")
    ok("login_snapshot_runs_without_polling")


def test_login_snapshot_switch_off():
    """设置里关掉开关 = 登录不抓池（用户实测后台抓池会卡，必须能自己关）。
    手动 /gql sync 与打开矩阵那条路不受开关影响。"""
    lua, gql, _ = sync_env([(81, 30000000)])
    g = lua.globals()
    if gql.Config.SetSyncOnLogin(False) is not True:
        return fail("login_snapshot_switch_off", "开关写不进去")
    if gql.Config.SyncOnLogin() is not False:
        return fail("login_snapshot_switch_off", "开关读不出来")
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    if not lua.execute("return next(GQLDB.chars) == nil"):
        return fail("login_snapshot_switch_off", "开关关了登录仍抓池")
    if lua.execute("return GQL.Sync.SnapshotNow() ~= nil") is not True:
        return fail("login_snapshot_switch_off", "关开关不许牵连手动快照")
    ok("login_snapshot_switch_off")


def test_matrix_scrolls_instead_of_scaling():
    """十几个角色时不缩字体，改滚动：内容区是 ScrollFrame 的 scrollchild，
    滚轮改的是组件的滚动值（SetVerticalScroll/SetHorizontalScroll），
    字体串一直停在内容坐标上不动（滚动由组件负责）。"""
    lua, gql, _ = sync_env([(82, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    if lua.execute("return GQLMatrixScroll:GetScript('OnMouseWheel') == nil"):
        return fail("matrix_scrolls_instead_of_scaling", "滚动区应挂滚轮事件")
    if lua.execute("return GQLMatrixScroll:GetScrollChild() ~= GQLMatrixContent"):
        return fail("matrix_scrolls_instead_of_scaling", "内容区应挂成 scrollchild")
    # 帧宽 = 自然宽度优先（紧凑）：1 列时 120+72+24=216，取下限 260
    if int(lua.execute("return GQLMatrixFrame.__w")) != 260:
        return fail("matrix_scrolls_instead_of_scaling",
                    f"帧宽应取紧凑下限 260（1 列），实际 {lua.execute('return GQLMatrixFrame.__w')}")
    # 滚轮往下滚（delta=-1）：推进纵向滚动值，和网页一个方向。
    # （这个客户端往下滚给的是负 delta，用户实测"加 delta 的手感是反的"）
    # 先造出纵向溢出：内容比可视区高才滚得动（没溢出时 clamp 归零是对的）
    for i in range(30):
        g.GQLDB["quests"][200 + i] = lua.table_from({
            "name": "凑行任务%d" % i, "mapID": 2007,
            "observations": lua.table_from({
                "30000000": lua.table_from({"money": 30000000, "gold": True,
                                            "firstSeen": 1000, "lastSeen": 1000})}),
            "currentSig": "30000000"})
    before = int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))
    lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "f(GQLMatrixScroll, -1)")
    after = int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))
    if after <= before:
        return fail("matrix_scrolls_instead_of_scaling",
                    f"往下滚轮应推进纵向滚动值，{before} -> {after}")
    # 内容坐标不随滚动改变（组件负责平移，字体串不用重排）：任务名在冻结左列
    pos = lua.execute(
        "for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__text and string.find(c.__text, '任务82', 1, true) then "
        "    local p = c.__points[1] return tostring(p[4])..'/'..tostring(p[5]) end end "
        "return ''")
    if "/" not in str(pos):
        return fail("matrix_scrolls_instead_of_scaling", "任务行应仍在内容区上")
    # 往上滚回原位
    lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "f(GQLMatrixScroll, 1)")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != before:
        return fail("matrix_scrolls_instead_of_scaling", "反向滚轮应回到原位")
    ok("matrix_scrolls_instead_of_scaling")


def test_next_refresh_at_is_earliest_expiry():
    """下次刷新时刻 = 账本里最早的 expiresAt；一条都没有 → nil（该抓了）。
    世界任务到点才换，最早的没到池子就不可能变 —— 据此不用一直轮询。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    if L.NextRefreshAt(db) is not None:
        return fail("next_refresh_at_is_earliest_expiry", "没有到期信息必须 nil")
    db["quests"] = lua.table_from({
        11: lua.table_from({"expiresAt": 9000}),
        12: lua.table_from({"expiresAt": 5000}),
        13: lua.table_from({"expiresAt": 0}),        # 0 = 取不到，不参与
        14: lua.table_from({}),                       # 没有该字段
    })
    if int(L.NextRefreshAt(db)) != 5000:
        return fail("next_refresh_at_is_earliest_expiry",
                    f"应取最早的 5000，实际 {L.NextRefreshAt(db)}")
    ok("next_refresh_at_is_earliest_expiry")


def test_snapshot_records_expires_at():
    """快照时把每个任务的剩余时间换算成到期时刻入账本，供"下次何时刷新"判断。"""
    lua, gql, _ = sync_env([(84, 30000000)])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    if int(g.GQLDB["quests"][84]["expiresAt"]) != 8200:
        return fail("snapshot_records_expires_at",
                    f"expiresAt 应为 now+剩余秒=8200，实际 {g.GQLDB['quests'][84]['expiresAt']}")
    ok("snapshot_records_expires_at")


def test_matrix_open_skips_snapshot_until_expiry():
    """打开矩阵不等于抓池：任务到期时刻都没到 = 世界任务还没换，直接渲染；
    过了最早的到期时刻才真抓一次。这正是"平时不轮询、该更新时才更新"。"""
    lua, gql, _ = sync_env([(85, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    if int(g.GQLDB["chars"][key]["lastSync"]) != 1000:
        return fail("matrix_open_skips_snapshot_until_expiry", "首次快照应已入账")
    g.GQL_NOW = 5000                       # 最早的到期是 8200，还没到
    gql.Matrix.Toggle()
    if int(g.GQLDB["chars"][key]["lastSync"]) != 1000:
        return fail("matrix_open_skips_snapshot_until_expiry",
                    "没到期不该抓池（轮询 = 抓任务池，几百张图的活）")
    gql.Matrix.Toggle()                    # 关上面板
    g.GQL_NOW = 9000                       # 过了到期时刻
    gql.Matrix.Toggle()
    if int(g.GQLDB["chars"][key]["lastSync"]) != 9000:
        return fail("matrix_open_skips_snapshot_until_expiry",
                    f"到期了应抓一次，lastSync 仍 {g.GQLDB['chars'][key]['lastSync']}")
    ok("matrix_open_skips_snapshot_until_expiry")


def test_refresh_wakes_at_earliest_expiry():
    """矩阵开着时按最早到期时刻排一个唤醒定时器：到点自动抓一次并重画。
    定时器必须是单实例：每次快照都会重排，不取消旧的就会越攒越多，
    几十个同时炸 = "开着矩阵就定时卡一下"（用户实测）。"""
    lua, gql, _ = sync_env([(86, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()                    # 打开=抓一次（没到期信息，必须抓）
    if int(lua.execute("return #GQL_NEWTIMERS")) != 1:
        return fail("refresh_wakes_at_earliest_expiry",
                    f"应只排 1 个唤醒定时器，实际 {lua.execute('return #GQL_NEWTIMERS')}")
    if int(lua.execute("return GQL_NEWTIMERS[1].delay")) != 7200:
        return fail("refresh_wakes_at_earliest_expiry",
                    f"应按最早到期排 7200 秒，实际 {lua.execute('return GQL_NEWTIMERS[1].delay')}")
    # 反复快照（模拟做任务时 QUEST_LOG_UPDATE 频繁触发）：定时器不许堆积
    for _ in range(5):
        gql.Sync.SnapshotNow()
    pending = lua.execute(
        "local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
        "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) != 1:
        return fail("refresh_wakes_at_earliest_expiry",
                    f"重排必须取消旧的，实际待触发 {pending} 个")
    key = str(gql.Sync.CurrentCharKey())
    g.GQL_NOW = 9000                       # 到点，定时器触发
    fired = lua.globals().GQL_FireTimers()
    if int(fired) < 1:
        return fail("refresh_wakes_at_earliest_expiry", "到期定时器应触发")
    if int(g.GQLDB["chars"][key]["lastSync"]) != 9000:
        return fail("refresh_wakes_at_earliest_expiry", "到点应自动抓一次快照")
    ok("refresh_wakes_at_earliest_expiry")


def test_closing_matrix_cancels_pending_refresh():
    """关上面板不光停轮询，还要把挂起的唤醒定时器取消掉 —— 否则到点它还会
    跑一趟全量抓池（回调里虽然有 polling 判断，但取消才是真的静默）。"""
    lua, gql, _ = sync_env([(87, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()
    gql.Matrix.Toggle()                    # 关闭
    pending = lua.execute(
        "local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
        "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) != 0:
        return fail("closing_matrix_cancels_pending_refresh",
                    f"关闭后不该有待触发定时器，实际 {pending} 个")
    ok("closing_matrix_cancels_pending_refresh")


def test_matrix_columns_show_char_name_only():
    """列头只显示角色名不显示服务器（用户实测要求）；账本 key 仍带服务器防撞。"""
    lua, gql, _ = sync_env([(89, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Sync.SetPolling(True)
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "小号一" not in joined:
        return fail("matrix_columns_show_char_name_only", f"列头应显示角色名:\n{joined}")
    if "永恒之井" in joined:
        return fail("matrix_columns_show_char_name_only",
                    f"列头不该出现服务器名:\n{joined}")
    ok("matrix_columns_show_char_name_only")


def test_matrix_cell_click_toggles_manual_state():
    """点状态格手动切换完成状态：点一下记 done（接口查不到任务时靠自己记），
    再点一下取消、回到自动判定。手动状态在 Ledger.State 里优先级最高。"""
    lua, gql, _ = sync_env([(90, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()

    def click_first_cell():
        return bool(lua.execute(
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  local s = c.__scripts and c.__scripts['OnClick'] "
            "  if s then s() return true end "
            "end return false"))

    key = str(gql.Sync.CurrentCharKey())
    if not click_first_cell():
        return fail("matrix_cell_click_toggles_manual_state", "应有点击热区")
    if str(g.GQLDB["manual"][key][90]["state"]) != "done":
        return fail("matrix_cell_click_toggles_manual_state",
                    f"点一下应记 done，实际 {g.GQLDB['manual'][key][90]}")
    if state1(gql.Ledger.State, g.GQLDB, key, 90) != "done":
        return fail("matrix_cell_click_toggles_manual_state", "手动状态应优先生效")
    if not click_first_cell():
        return fail("matrix_cell_click_toggles_manual_state", "再点应还能点到热区")
    if g.GQLDB["manual"][key][90] is not None:
        return fail("matrix_cell_click_toggles_manual_state", "再点一下应取消手动覆盖")
    ok("matrix_cell_click_toggles_manual_state")


def click_cell(lua, char_key, qid):
    """按"列上的号名 + questID"点中那一格：走的是热区自己的 OnClick，也就是
    玩家真点下去的那条路（CellClick），不从中间调函数。"""
    return bool(lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s and c.__questID == %d and c.__charKey == %r then s() return true end "
        "end return false" % (qid, char_key)))


def check_glyph(frame, tinted=None):
    """取那一格的勾用了哪张贴图串（ReadyCheck-Ready 手点的染了琥珀顶点色）。"""
    out = []
    for t in frame_texts(frame):
        if "ReadyCheck-Ready" in t:
            inner = t.split("|T", 1)[1].split("|t", 1)[0].split(":")
            if tinted is True and len(inner) == 14:
                out.append(t)
            elif tinted is False and len(inner) == 3:
                out.append(t)
            elif tinted is None:
                out.append(t)
    return out


def test_hand_marked_cell_uses_an_amber_check():
    """玩家手点的勾要换成琥珀色（用户 2026-10-25："手动补录这个，换 √ 的颜色，
    一目了然哪个是手动的"）。还是同一张 ReadyCheck-Ready，只加 |T 的顶点色：
    颜色一对一对上"这一格能不能当凭据"（绿=客户端/流水认的，琥珀=我自己记的）。
    |T 参数顺序照暴雪 UI escape sequences 原文：贴图:高:宽:偏移x:偏移y:
    贴图宽:贴图高:左:右:上:下:红:绿:蓝，色值 0-255 —— 用例按位置断言，
    冒号数错了这条就红（离线看不出渲染，只能钉字段位）。"""
    lua, gql, _ = sync_env([(92, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    # 接口答"没做过"，玩家还点这一格 = 真手点的（不许拿接口替玩家背书）
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, key, 92):
        return fail("hand_marked_cell_uses_an_amber_check", "点不到那一格")
    gql.Matrix.Render()
    tinted = check_glyph(g.GQLMatrixFrame, tinted=True)
    if len(tinted) != 1:
        return fail("hand_marked_cell_uses_an_amber_check",
                    "手点的格该有一枚染色勾:\n" + "\n".join(frame_texts(g.GQLMatrixFrame)))
    parts = tinted[0].split("|T", 1)[1].split("|t", 1)[0].split(":")
    if parts[11:14] != ["255", "176", "0"]:
        return fail("hand_marked_cell_uses_an_amber_check",
                    f"琥珀顶点色要落在 |T 的第 11~13 个参数上，实际 {parts}")
    # 反方向：客户端认的那一格（实测交任务）不许跟着变色，否则颜色就不映射"凭据"了
    lua.execute("GQLDB.quests[92].roundAt = 0 "
                "GQL.Ledger.SetManual(GQLDB, %r, 92, 'open', 1, nil) "
                "GQL.Ledger.OnTurnIn(GQLDB, %r, 92, 1, 30000000, nil)" % (key, key))
    gql.Matrix.Render()
    plain = check_glyph(g.GQLMatrixFrame, tinted=False)
    if len(plain) != 1 or check_glyph(g.GQLMatrixFrame, tinted=True):
        return fail("hand_marked_cell_uses_an_amber_check",
                    "客户端答的勾要保持原色（不染色）:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    ok("hand_marked_cell_uses_an_amber_check")


def test_click_on_this_char_verifies_with_the_interface():
    """点**当前这个号**的格子时先单独问一次接口（用户 2026-10-25 问的就是这条：
    手动补录能不能用 API 查出该角色已经完成）。接口说已完成 → 记成"接口核验"：
    勾保持客户端认的那枚绿色，不再标成玩家手点的。
    代价照实用例钉住：一次点击只问这一回（不轮询、不进同步循环）。"""
    lua, gql, _ = sync_env([(93, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL_API_STATE_CALLS = 0 "
                "GQL.Api.QuestState = function(q) "
                "  GQL_API_STATE_CALLS = GQL_API_STATE_CALLS + 1 return 'done', nil end")
    if not click_cell(lua, key, 93):
        return fail("click_on_this_char_verifies_with_the_interface", "点不到那一格")
    if int(g.GQL_API_STATE_CALLS) != 1:
        return fail("click_on_this_char_verifies_with_the_interface",
                    "点一次只该问一次接口，实际问了 %s 次" % g.GQL_API_STATE_CALLS)
    if str(g.GQLDB["manual"][key][93]["v"]) != "接口核验":
        return fail("click_on_this_char_verifies_with_the_interface",
                    "接口认的那一格要标成核验，实际 %s" % g.GQLDB["manual"][key][93])
    gql.Matrix.Render()
    if check_glyph(g.GQLMatrixFrame, tinted=True):
        return fail("click_on_this_char_verifies_with_the_interface",
                    "核验过的格不许染成手点的颜色:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    if state1(gql.Ledger.State, g.GQLDB, key, 93) != "done":
        return fail("click_on_this_char_verifies_with_the_interface", "核验后这一格该是勾")
    ok("click_on_this_char_verifies_with_the_interface")


def test_click_on_another_chars_cell_never_borrows_this_chars_answer():
    """别人的号问不出来：客户端只答当前角色，跨号数据压根不在本机内存里。
    所以点别的号的格子**不许**去问接口 —— 那等于拿我这个号的答案冒充那一号（谎报），
    也白加一次接口调用。那一号只能照旧记手动补录（琥珀勾）。"""
    lua, gql, _ = sync_env([(94, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQLDB.chars['别人-永恒之井'] = { lastSync = 1, views = {} }")
    gql.Matrix.Toggle()
    lua.execute("GQL_API_STATE_CALLS = 0 "
                "GQL.Api.QuestState = function(q) "
                "  GQL_API_STATE_CALLS = GQL_API_STATE_CALLS + 1 return 'done', nil end")
    if not click_cell(lua, "别人-永恒之井", 94):
        return fail("click_on_another_chars_cell_never_borrows_this_chars_answer",
                    "点不到那一号那一格（列没建起来？）")
    if int(g.GQL_API_STATE_CALLS) != 0:
        return fail("click_on_another_chars_cell_never_borrows_this_chars_answer",
                    "点别的号的格子不许问接口（它只答当前角色），实际问了 %s 次"
                    % g.GQL_API_STATE_CALLS)
    if g.GQLDB["manual"]["别人-永恒之井"][94]["v"] is not None:
        return fail("click_on_another_chars_cell_never_borrows_this_chars_answer",
                    "这一号没人答过，只能记手动补录")
    gql.Matrix.Render()
    if len(check_glyph(g.GQLMatrixFrame, tinted=True)) != 1:
        return fail("click_on_another_chars_cell_never_borrows_this_chars_answer",
                    "手动那一格该是琥珀勾:\n" + "\n".join(frame_texts(g.GQLMatrixFrame)))
    ok("click_on_another_chars_cell_never_borrows_this_chars_answer")


def test_completion_line_names_the_evidence_behind_the_check():
    """悬停最下面那行要分清凭据（用户 2026-10-25：接口答已完成的那一格"就不标记手动
    补录，而是等于单独查询一次、核验完成了"）：核验过的写（接口核验），
    玩家手点的写（手动补录）—— 两个词两种可信度，别混成一句。"""
    lua, gql, g, calls = tip_env(131, 1792577090, cell=True)
    key = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Ledger.SetManual(GQLDB, %r, 131, 'done', 1792577090, nil, '接口核验')" % key)
    gql.Matrix.Render()
    hover_update(lua)
    joined = "\n".join(calls)
    if "（接口核验）" not in joined:
        return fail("completion_line_names_the_evidence_behind_the_check",
                    f"核验过的那格要写明凭据:\n{joined}")
    if "手动补录" in joined:
        return fail("completion_line_names_the_evidence_behind_the_check",
                    f"接口认的不能还说成玩家手点的:\n{joined}")
    ok("completion_line_names_the_evidence_behind_the_check")


def test_quest_id_is_gray_like_the_bottom_line():
    """ID 上灰、和完成那一行同一个色（用户 2026-10-25："改成灰色的，
    和最下面那一行一个颜色，字也小一点"）。标题行是 AddDoubleLine：
    左=任务名（白，标题字体），右=questID（0.6,0.6,0.6，模板里右列本来就比标题小）。
    字号是客户端模板的事（暴雪转义表里没有"字号"这一项，只有字体对象能改），
    所以用例钉得住颜色参数，钉不住字号 —— 那条要在游戏里看一眼。"""
    lua, gql, g, calls = tip_env(132, 1792577090, gain=3000000, turnin=1792577090, cell=True)
    colors = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: colors.append((str(text), tuple(a))),
        "AddDoubleLine": lambda self, l, r, *a: colors.append(("%s|%s" % (l, r), tuple(a))),
        "Show": lambda *a: None,
        "Hide": lambda *a: None,
    })
    hover_update(lua)
    title = [c for c in colors if c[0].endswith("|132")]
    if not title:
        return fail("quest_id_is_gray_like_the_bottom_line",
                    f"标题行该是『名字|ID』两列:\n{[c[0] for c in colors]}")
    rgb = title[0][1]
    if rgb[:6] != (1, 1, 1, 0.6, 0.6, 0.6):
        return fail("quest_id_is_gray_like_the_bottom_line",
                    f"名字要白、ID 要 0.6 灰（AddDoubleLine 的六色参数顺序）: {rgb}")
    done = [c for c in colors if "交于" in c[0]]
    if not done or done[0][1][:3] != (0.6, 0.6, 0.6):
        return fail("quest_id_is_gray_like_the_bottom_line",
                    f"ID 要和完成那行同灰，实际 {done and done[0][1]}")
    ok("quest_id_is_gray_like_the_bottom_line")


def test_midnight_map_ids_match_by_name_and_remember():
    """本资料片地图按本地化名字匹配出 ID 并记进账本：下次登录先用记住的 ID
    （逐个验名字），地图改版对不上才重新全图扫描。一张都没匹配到 → nil
    （调用方退回全量枚举，绝不返回空池）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    names = {2395: "永歌森林", 2393: "虚影风暴", 2400: "祖阿曼",
             2401: "盘卷蛇岛", 680: "苏拉玛", 641: "瓦尔莎拉"}
    tree = {946: [2000, 3000], 2000: [2393, 680], 3000: [2395, 2400, 2401, 641]}
    g.C_Map = lua.table_from({
        "GetFallbackWorldMapID": lambda: 946,
        "GetMapInfo": lambda mid: lua.table_from({"name": names.get(int(mid))})
        if int(mid) in names else None,
        "GetMapChildrenInfo": lambda mid: lua.table_from(tree.get(int(mid), [])),
    })
    ids = gql.Api.MidnightMapIDs()
    got = sorted(int(ids[i]) for i in range(1, len(ids) + 1))
    if got != [2393, 2395, 2400, 2401]:
        return fail("midnight_map_ids_match_by_name_and_remember",
                    f"应按名字匹配出 4 张本资料片图，实际 {got}")
    # 匹配结果记进账本
    saved = lua.execute(
        "local out = {} for i, v in ipairs(GQLDB.midnightMapIds) "
        "do out[i] = v end table.sort(out) return table.concat(out, ',')")
    if str(saved) != "2393,2395,2400,2401":
        return fail("midnight_map_ids_match_by_name_and_remember",
                    f"匹配结果应记进账本，实际 {saved}")
    # 记住的 ID 全部验得上 → 直接用，不再全图扫描（C_Map 整个拔掉也行）
    g.C_Map = None
    again = lua.execute(
        "local out = {} for i, v in ipairs(GQL.Api.MidnightMapIDs()) "
        "do out[i] = v end table.sort(out) return table.concat(out, ',')")
    if str(again) != "2393,2395,2400,2401":
        return fail("midnight_map_ids_match_by_name_and_remember",
                    f"记住的 ID 验得上应直接复用，实际 {again}")
    # 地图改版（模拟新会话：判定缓存是会话级的，重开游戏才清）：账本里记住的
    # ID 名字对不上 → 重新扫描也匹配不到 → nil（退回全量枚举）
    lua3, gql3 = load_lua("Core", "Config", "Ledger", "Api", "Sync", mock=True)
    g3 = lua3.globals()
    g3.GQLDB = lua3.table_from({"midnightMapIds": lua3.table_from({2099})})
    gql3.Config.Init()
    g3.C_Map = lua3.table_from({
        "GetFallbackWorldMapID": lambda: 946,
        "GetMapInfo": lambda mid: lua3.table_from({"name": "旧地名"})
        if int(mid) == 2099 else None,
        "GetMapChildrenInfo": lambda mid: lua3.table_from({}),
    })
    if gql3.Api.MidnightMapIDs() is not None:
        return fail("midnight_map_ids_match_by_name_and_remember",
                    "名字全对不上时必须 nil，不能把旧 ID 当新资料片地图用")
    ok("midnight_map_ids_match_by_name_and_remember")


def test_getpool_queries_only_midnight_maps():
    """池快照只问本资料片地图（用户实测：其他地图以后不查，省掉几百张图的
    全图扫描 —— 那是卡顿源）。旧资料片图的任务进不了池。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    names = {2395: "永歌森林", 2393: "虚影风暴", 680: "苏拉玛"}
    tree = {946: [2000, 3000], 2000: [2393, 680], 3000: [2395]}
    asked = []

    def quests_on_map(mid):
        asked.append(int(mid))
        if int(mid) == 2395:
            return lua.table_from({lua.table_from(
                {"questID": 92160, "mapID": 2395, "name": "幼崽大骚乱",
                 "money": 10826761})})
        return lua.table_from({})

    g.C_Map = lua.table_from({
        "GetFallbackWorldMapID": lambda: 946,
        "GetMapInfo": lambda mid: lua.table_from({"name": names.get(int(mid))})
        if int(mid) in names else None,
        "GetMapChildrenInfo": lambda mid: lua.table_from(tree.get(int(mid), [])),
    })
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": quests_on_map})
    g.GQLDB = lua.table_from({})
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_queries_only_midnight_maps", f"池应可用，err={err}")
    if len(pool) != 1:
        return fail("getpool_queries_only_midnight_maps", f"应只收到本资料片任务，实际 {len(pool)} 条")
    if 680 in asked:
        return fail("getpool_queries_only_midnight_maps",
                    f"旧资料片图不该被查询，实际问过 {sorted(set(asked))}")
    ok("getpool_queries_only_midnight_maps")


def test_matrix_cell_click_records_quest_gold():
    """手动点 done 时把任务金币一起记上：玩家确实做了任务，周统计不能显示
    "完成 1 金币 0"（那表示白干）。金额没读到才留空。"""
    lua, gql, _ = sync_env([(93, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 100000            # 锚点是 700000-604800=95200，手动时间戳要落在本周内
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s then s() break end end")
    gval = lua.execute(
        "local m = GQLDB.manual['%s'][93] "
        "if not m then return nil end return m.g" % key)
    if gval is None or int(gval) != 30000000:
        return fail("matrix_cell_click_records_quest_gold",
                    f"手动 done 应记上任务金币，实际 {gval}")
    cnt = lua.execute(
        "return (GQL.Ledger.WeekStats(GQLDB, '%s', GQLDB.cycleAnchor))" % key)
    if int(cnt) < 1:
        return fail("matrix_cell_click_records_quest_gold", "手动 done 应计入本周完成数")
    ok("matrix_cell_click_records_quest_gold")


def test_matrix_task_info_in_fixed_subcolumns():
    """任务名在冻结的左列（横向滚动时钉死不动），角色列在右窗格跟着滚；
    不再有金额/剩余固定小列（用户要求去掉，改用任务名颜色表示剩余时间）。
    表头有"任务"；任务名在冻结左列、角色名在固定表头条（headR，不在滚动内容里）；
    左窗格横向永远 0。"""
    lua, gql, _ = sync_env([(94, 30000000)], extra=("Matrix",))
    g = lua.globals()
    # 多来几个角色列制造横向溢出
    for i in range(8):
        g.GQLDB["chars"]["打杂%d-永恒之井" % i] = lua.table_from(
            {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "任务" not in joined:
        return fail("matrix_task_info_in_fixed_subcolumns", f"表头缺 任务:\n{joined}")
    # 任务名在冻结左列（GQLMatrixContentL），不随横向滚动移动
    in_left = lua.execute(
        "local found = false for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__text and string.find(c.__text, '任务94', 1, true) then found = true end end "
        "return found")
    if not in_left:
        return fail("matrix_task_info_in_fixed_subcolumns", "任务名应在冻结左列")
    # 角色名在固定的表头条右格（headR），不再是滚动内容区：纵滚才滚不走它
    in_head = lua.execute(
        "local found = false for i, c in ipairs(GQLMatrixHeadR.__children) do "
        "  if c.__text and string.find(c.__text, '小号一', 1, true) then found = true end end "
        "return found")
    if not in_head:
        return fail("matrix_task_info_in_fixed_subcolumns", "角色名应在固定表头条")
    in_body = lua.execute(
        "local found = false for i, c in ipairs(GQLMatrixContent.__children) do "
        "  if c.__text and string.find(c.__text, '小号一', 1, true) then found = true end end "
        "return found")
    if in_body:
        return fail("matrix_task_info_in_fixed_subcolumns",
                    "角色名不该再画进滚动内容区（会跟着纵向滚走）")
    # 冻结验证：左窗格横向滚动永远 0（Shift+滚轮打在左窗格上也不动）
    lua.execute("IsShiftKeyDown = function() return true end "
                "local f = GQLMatrixScrollL:GetScript('OnMouseWheel') "
                "f(GQLMatrixScrollL, 3)")
    if int(lua.execute("return GQLMatrixScrollL:GetHorizontalScroll()")) != 0:
        return fail("matrix_task_info_in_fixed_subcolumns", "左窗格横向应钉死为 0")
    ok("matrix_task_info_in_fixed_subcolumns")


def test_matrix_class_color_applied():
    """角色名必须按职业上色：之前误用 RAID_CLASS_COLORS[token].colorHex
    （该字段不存在），一直退白。颜色应从 .colorStr（"ffRRGGBB"）取后 6 位。"""
    lua, gql, _ = sync_env([(94, 30000000)], extra=("Matrix",))
    g = lua.globals()
    # 替身里塞一套职业色表 + UnitClass，复现"快照记住职业 → 列头按职业上色"
    g.RAID_CLASS_COLORS = lua.table_from({
        "MAGE": lua.table_from({"colorStr": "ffa330c9", "r": 0.64, "g": 0.19, "b": 0.79}),
    })
    g.UnitClass = lambda unit: ("法师", "MAGE", 8)
    gql.Matrix.Toggle()                       # Toggle 内会快照，记下 class=MAGE
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    # 列头角色名应带上职业色 a330c9（不再是一直退白的 ffffff）
    if "a330c9" not in joined:
        return fail("matrix_class_color_applied", f"角色名未按职业上色:\n{joined}")
    if "ffffff" in joined and "a330c9" not in joined:
        return fail("matrix_class_color_applied", "职业色被白色覆盖")
    ok("matrix_class_color_applied")


def test_matrix_frame_width_capped_with_scroll():
    """帧宽封顶屏幕 75%：角色再多也不把弹窗拉得比屏幕宽，超出横向滚动
    （滚轮/Shift+滚轮）。"""
    lua, gql, _ = sync_env([(95, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.UIParent.GetWidth = lambda self: 1000      # 模拟 1000px 宽的屏幕
    db = g.GQLDB
    for i in range(12):                          # 再加 12 个角色列
        db["chars"]["打杂%d-永恒之井" % i] = lua.table_from(
            {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    w = int(lua.execute("return GQLMatrixFrame.__w"))
    if w != 750:
        return fail("matrix_frame_width_capped_with_scroll",
                    f"帧宽应封顶 1000*0.75=750，实际 {w}")
    ok("matrix_frame_width_capped_with_scroll")


def test_matrix_group_header_is_name_only():
    """地图名行只要名字：不要下三角、不要"合计多少金"（用户实测要求），
    用金色字体和任务行区分。"""
    lua, gql, _ = sync_env([(96, 30000000)], extra=("Matrix",))
    g = lua.globals()
    names = {2007: "虚影风暴"}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": names.get(int(mid))})
        if int(mid) in names else None,
    })
    gql.Sync.SnapshotNow()
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "虚影风暴" not in joined:
        return fail("matrix_group_header_is_name_only", f"组头应显示地图名:\n{joined}")
    if "▼" in joined or "合计" in joined:
        return fail("matrix_group_header_is_name_only",
                    f"组头不该有下三角/合计:\n{joined}")
    ok("matrix_group_header_is_name_only")


def test_preload_retry_fills_money_without_reopening():
    """第一次打开金额没读全（预加载数据没到）时，开着矩阵每 5 秒自动补读一次，
    不用手动关闭再打开（用户实测反馈）。补读次数封顶，不会无限转。"""
    lua, gql, _ = sync_env([(97, None)], extra=("Matrix",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 0      # 数据一直没到
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()                           # 打开=抓一次，金额读空
    if int(lua.execute("return #GQL_NEWTIMERS")) < 1:
        return fail("preload_retry_fills_money_without_reopening",
                    "金额读空应排补读定时器")
    # 推进时间让补读一次次触发，次数必须封顶（5 秒 × 最多 4 次）
    for step in range(6):
        g.GQL_NOW = 1000 + 5 * (step + 1)
        lua.globals().GQL_FireTimers()
    scheduled = lua.execute(
        "local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
        "if not t.cancelled then n = n + 1 end end return n")
    if int(scheduled) > 1:
        return fail("preload_retry_fills_money_without_reopening",
                    f"补读应串行不堆积，实际待触发 {scheduled} 个")
    fired = lua.execute(
        "local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
        "if t.fired then n = n + 1 end end return n")
    # GQL_FireTimers 会把触发的清出队列，fired 计数只反映本轮；改为数日志
    logs = str(gql.Config.GetLogs())
    if logs.count("快照完成") > 5:
        return fail("preload_retry_fills_money_without_reopening",
                    f"补读次数应封顶 4 次，实际快照 {logs.count('快照完成')} 次")
    ok("preload_retry_fills_money_without_reopening")


def test_event_snapshot_repaints_matrix():
    """事件去抖那条自动快照抓完池必须喊面板重画。矩阵平时靠这条更新（做任务时
    QUEST_LOG_UPDATE 连成一片），少了这一句就是"账本里金额都进来了、屏幕上还是上一份"：
    12.1.5 实测 89347 那条 967 金进账本 12 分钟，矩阵里始终没有，玩家只能去手动添加
    （用户："后面刷新了就会变成金币任务，好像不会自动加入矩阵"）。重画只抄内存里的账本。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    gql.Matrix.Toggle()                       # 打开 = 抓一次 + 渲染 + 开轮询
    if int(lua.execute("return GQL.Matrix.rowCount or -1")) < 1:
        return fail("event_snapshot_repaints_matrix", "打开面板应先画出一行")
    lua.execute("GQL.Matrix.rowCount = -1")   # 抹掉重画标记，下面只看有没有人重画
    fire(gql, "QUEST_LOG_UPDATE")
    g.GQL_NOW = 20
    g.GQL_FireTimers()                        # 走真实去抖路径，不直调 SnapshotNow
    if int(lua.execute("return GQL.Matrix.rowCount")) < 0:
        return fail("event_snapshot_repaints_matrix",
                    f"自动快照后没重画，账本更新了玩家看不见:\n{gql.Config.GetLogs()}")
    ok("event_snapshot_repaints_matrix")


def test_turnin_settlement_repaints_matrix():
    """交任务那笔钱结算完也要重画：本周交过是矩阵六道准入门里的硬凭据之一
    （Ledger.MatrixRows 拿它绕过金币门槛），不重画的话玩家刚做完一条任务，
    那一格还停在旧的一份画面上。同样零接口调用。"""
    lua, gql, _ = sync_env([(121, 30000000)], money=1000000, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    lua.execute("GQL.Matrix.rowCount = -1")
    fire(gql, "QUEST_COMPLETE", 121)
    g.GQL_MONEY["v"] = 12000000
    g.GQL_NOW = 20
    g.GQL_FireTimers()                        # 15 秒窗口到点 → 真 FinishWindow
    text = str(gql.Config.GetLogs())
    if "交任务 quest=121" not in text:
        return fail("turnin_settlement_repaints_matrix", f"窗口没结算:\n{text}")
    if int(lua.execute("return GQL.Matrix.rowCount")) < 0:
        return fail("turnin_settlement_repaints_matrix", "记完这笔没重画矩阵")
    ok("turnin_settlement_repaints_matrix")


def test_matrix_uses_real_scrollframe():
    """滚动区是暴雪原声 ScrollFrame（拍卖行插件同款）：内容挂 scrollchild，
    组件自己裁剪滚动，字体串绝不会画到帧外头（手动裁剪治标不治本，
    用户实测"还是会溢出"）。帧宽封顶 75%，横向滚看其余角色列。"""
    lua, gql, _ = sync_env([(98, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.UIParent.GetWidth = lambda self: 1000      # 帧宽封顶 750
    db = g.GQLDB
    for i in range(12):                          # 13 列，自然宽度远超帧宽
        db["chars"]["打杂%d-永恒之井" % i] = lua.table_from(
            {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    # 内容区挂进 ScrollFrame、内容宽度=自然宽度：组件负责裁剪，
    # 字体串不会再画到帧外头（手动裁剪治标不治本，用户实测"还是会溢出"）
    if lua.execute("return GQLMatrixScroll:GetScrollChild() ~= GQLMatrixContent"):
        return fail("matrix_clips_content_outside_visible_area",
                    "内容区应挂成 scrollchild")
    if int(lua.execute("return GQLMatrixContent.__w")) != 72 * 13 + 8:
        return fail("matrix_clips_content_outside_visible_area",
                    "内容宽应是全角色列展开的自然宽（紧凑列宽 72）")
    if int(lua.execute("return GQLMatrixFrame.__w")) != 750:
        return fail("matrix_clips_content_outside_visible_area", "帧宽应封顶 750")
    # 横向滚轮（Shift+往下滚 = 负 delta）推进组件滚动值
    before = int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()"))
    lua.execute("IsShiftKeyDown = function() return true end "
                "local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "f(GQLMatrixScroll, -3)")
    after = int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()"))
    if after <= before:
        return fail("matrix_clips_content_outside_visible_area",
                    f"横向滚动应推进组件滚动值，{before} -> {after}")
    ok("matrix_clips_content_outside_visible_area")


def test_matrix_shows_scrollbars_when_content_overflows():
    """内容超出可见区时要出真滚动条（可拖）：用户实测"没有滚动条，状态溢出"。
    内容不超时整条藏起来。"""
    lua, gql, _ = sync_env([(99, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()          # 1 个角色，内容不超 → 滚动条藏起来
    if lua.execute("return GQLMatrixScroll ~= nil and true"):
        pass
    # 竖条只在内容超高时显示：直接检查 UpdateScrollbars 的行为太内部，
    # 改为验证滚动条对象已建成（CreateFrame 替身把具名帧挂 _G，这里用子帧数）
    bars = lua.execute("return #GQLMatrixFrame.__children")
    if int(bars) < 3:
        return fail("matrix_shows_scrollbars_when_content_overflows",
                    f"帧下应建有滚动条等子对象，实际 {bars}")
    ok("matrix_shows_scrollbars_when_content_overflows")


def test_money_read_flicker_does_not_drop_rows():
    """预加载时差会把同一任务的金额读闪：这次真金额、下次 0。sig 跟着翻转，
    currentSig 指到"金额为空"那份 observation 时矩阵行会忽隐忽现
    （用户实测"来回点几次列表就变、任务缺失"）。延续金额后行必须稳。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 100000
    gql.Sync.SnapshotNow()
    # 下一轮：101 的金额读成 0（数据没到），102 不变
    rows = lua.table_from({
        lua.table_from({"questID": 101, "mapID": 2007, "name": "任务101"}),
        lua.table_from({"questID": 102, "mapID": 2007, "name": "任务102",
                        "money": 20000000}),
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    g.GQL_NOW = 100100
    gql.Sync.SnapshotNow()
    n = int(gql.Matrix.Render())
    if n != 2:
        return fail("money_read_flicker_does_not_drop_rows",
                    f"金额读闪不该丢行，实际 {n} 行")
    m = lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 101)")
    if m is None or int(m) != 30000000:
        return fail("money_read_flicker_does_not_drop_rows",
                    f"101 的金额应延续旧值，实际 {m}")
    ok("money_read_flicker_does_not_drop_rows")


def test_confirmed_no_gold_does_not_keep_the_old_number():
    """12.1.5 反馈（2026-10-25）："意外遭遇 在矩阵里被激活（之前应该做过这个任务，当时是金币
    任务），但目前这个任务不是金币任务。依然在矩阵里"，附两张图：游戏自己的地图气泡只有
    52 宝匣钥匙碎片 + 战团声望，我们的悬停却还写着 1064 金。
    根因不在门槛，在金额：这一轮整条金额链一个字没答 ⇒ DiffPool 按"预加载读闪"处理，
    把上个 CD 读到的金币延续过来 ⇒ 门槛照旧为它答"够"，行就一路赖着不走。
    客户端其实分得开这两件事：HaveQuestRewardData 答 true = 奖励数据就在本地，
    金额链还是问不出 = 它明说"这一条不给金币" → 写 0（真数），旧数不许再往下传。
    **与上一条用例成对**：那条是这入口没答（本机没这个入口/答 false）→ 照旧延续、行不许闪。
    行掉出去的凭据是"换了一条实例"（结束时间变了，用户同日定的口径），不是"当场读到非金币"
    —— 后者会把双模式那条误删（见 test_dual_mode_flip_keeps_the_row_and_the_check）。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    secs = {"v": 43200}
    g.C_TaskQuest = lua.table_from(
        {"GetQuestTimeLeftSeconds": lambda qid: secs["v"]})
    g.GQL_NOW = 100000
    gql.Sync.SnapshotNow()
    # 换轮：101 这块板不再给金币（行不带金额、金额链也问不出），但数据就在本地
    rows = lua.table_from({
        lua.table_from({"questID": 101, "mapID": 2007, "name": "任务101"}),
        lua.table_from({"questID": 102, "mapID": 2007, "name": "任务102",
                        "money": 20000000}),
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.HaveQuestRewardData = lambda qid: True
    g.GQL_NOW = 100100
    secs["v"] = 36000                       # 到期时刻挪了 7200 秒 = 板面上换了一条
    gql.Sync.SnapshotNow()
    m = lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 101)")
    if m is None or int(m) != 0:
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    f"客户端确认无金币时要写 0，不许延续上个 CD 的数，实际 {m}")
    n = int(gql.Matrix.Render())
    if n != 1:
        joined = "\n".join(frame_texts(g.GQLMatrixFrame))
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    f"换了实例又不给金币的那条不该再占行，实际 {n} 行:\n{joined}")
    if int(lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 102)")) != 20000000:
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    "同一轮里真带金币的那条不许被牵连（0 只写给客户端答了'没金币'的那条）")
    ok("confirmed_no_gold_does_not_keep_the_old_number")


def test_countdown_falls_back_to_ledger_expiry():
    """倒计时列已改为"按剩余时间给任务名上色"（>1天蓝 / >1小时绿 / <1小时红）。
    实时倒计时读数偶发失败（接口 nil）时，仍按账本记的到期时刻算剩余、上色，
    不然有的任务永远白着（用户实测"有的任务倒计时看不到了"）。"""
    lua, gql, _ = sync_env([(103, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 100000
    gql.Sync.SnapshotNow()                       # expiresAt = 100000 + 7200 = 107200
    # 实时读数失败（接口 nil）
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: None})
    # 让矩阵用固定 now=100000 算剩余（账本 expiresAt 兜底）
    lua.execute("GQL.Api.WallClock = function() return 100000 end")
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    # 剩余 7200s 落在 (3600, 86400) → 绿色 37e83a，即使实时读数失败也要上色
    if "37e83a" not in joined:
        return fail("countdown_falls_back_to_ledger_expiry",
                    f"实时读数失败时应按账本到期时刻上色（绿）:\n{joined}")
    ok("countdown_falls_back_to_ledger_expiry")


def test_wheel_works_over_cell_buttons():
    """滚轮必须满格都能用：状态格按钮盖在内容区上，滚轮事件打在按钮上
    就哑了（用户实测"大部分情况滚轮无效，只有滚到缝隙才触发"）。
    每个热区都要把滚轮转给同一个处理器。"""
    lua, gql, _ = sync_env([(104, 30000000)], extra=("Matrix",))
    g = lua.globals()
    # 先造横向溢出（多来几个角色列），不然没内容可滚、clamp 归零
    for i in range(12):
        g.GQLDB["chars"]["打杂%d-永恒之井" % i] = lua.table_from(
            {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    # 直接打第一个热区的滚轮脚本（模拟鼠标停在格子上滚）
    lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  local s = c.__scripts and c.__scripts['OnMouseWheel'] "
        "  if s then s(c, 1) break end end")
    if int(lua.execute(
            "local n = 0 for i, c in ipairs(GQLMatrixContent.__children) do "
            "  if c.__scripts and c.__scripts['OnMouseWheel'] then n = n + 1 end "
            "end return n")) < 2:
        return fail("wheel_works_over_cell_buttons",
                    "每个热区都应挂滚轮透传（Local 顺序坑会挂成 nil）")
    # 停在格子上横向滚（Shift+往右滚 = 负 delta）：组件的横向滚动值必须推进
    before = int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()"))
    lua.execute("IsShiftKeyDown = function() return true end "
                "for i, c in ipairs(GQLMatrixContent.__children) do "
                "  local s = c.__scripts and c.__scripts['OnMouseWheel'] "
                "  if s then s(c, -3) break end end")
    after = int(lua.execute("return GQLMatrixScroll:GetHorizontalScroll()"))
    if after <= before:
        return fail("wheel_works_over_cell_buttons",
                    f"格子上的横向滚轮应推进滚动，{before} -> {after}")
    ok("wheel_works_over_cell_buttons")


def test_row_hover_highlight_covers_whole_row():
    """整行都要有 hover 高亮：左窗格有盖满任务列宽的整行悬停热区，悬停扫描
    （OnUpdate + IsMouseOver）找到哪行被压着就亮哪行——左窗格高亮 + 右窗格高亮
    拼成整行。原来只有状态格有热区、且 OnEnter 触发顺序不可靠导致高亮时有时无
    （用户实测"看不出来"、"依然没有 current line highlight"）。"""
    lua, gql, _ = sync_env([(105, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    # 左窗格里要有盖满任务列宽（>=300）的整行悬停热区
    has_row_btn = lua.execute(
        "local n = 0 for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__scripts and c.__scripts['OnMouseWheel'] and (c.__w or 0) >= 100 then "
        "    n = n + 1 end end return n")
    if int(has_row_btn) < 1:
        return fail("row_hover_highlight_covers_whole_row",
                    "应有盖满任务列宽的整行悬停热区")
    # 模拟鼠标停在某一行：把该行热区的 IsMouseOver 置真，并让客户端报出焦点
    # 就在这个热区上（悬停准入要看焦点），再驱动 OnUpdate 扫描
    lua.execute(
        "for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__scripts and c.__scripts['OnMouseWheel'] then "
        "    c.__mouseover = true GQL_FOCUS = c break end end")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    # 高亮应出现在两个窗格里，各自盖满所在窗格的整行宽（合起来就是整行）
    res = lua.execute(
        "local hlL, hlR = false, false "
        "for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__type == 'Frame' and c.__shown and (c.__w or 0) >= 100 then hlL = true end end "
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  if c.__type == 'Frame' and c.__shown and (c.__w or 0) >= 60 then hlR = true end end "
        "return hlL, hlR")
    hlL, hlR = res
    if not (hlL and hlR):
        return fail("row_hover_highlight_covers_whole_row",
                    "整行高亮应在两个窗格都出现（左+右拼成整行）")
    ok("row_hover_highlight_covers_whole_row")


# ---- 矩阵渲染/悬停裁到可见区（性能计划 ②）共用的助手与常量 --------------------
# 版式常量照 Matrix.lua 顶部那份（行高 20、组头 26、任务列 120、角色列 72、
# 纵向内边距 56、可见窗口上下各让 3 行缓冲）；这几处一改，下面的期望值要跟着改。
MX_ROW_H, MX_GROUP_H, MX_NAME_W, MX_COL_W = 20, 26, 120, 72
MX_PAD_V, MX_OVERSCAN_ROWS = 56, 3


def big_matrix_env(rows=120, cols=6, screen_w=None):
    """矩阵开着 + rows 行 × cols 列的环境。
    替身默认屏幕 1440×720 → 帧高封顶 612 → 右窗格可视高 536 ≈ 26 行，
    所以行要给到 120 才真裁得动（行少的时候全量本来就放得下）。
    screen_w 把屏幕宽压小，用来造"视口取不到数"那条兜底。"""
    lua, gql, _ = sync_env([(1000 + i, 30000000) for i in range(rows)], extra=("Matrix",))
    g = lua.globals()
    if screen_w:
        g.UIParent.GetWidth = lambda self: screen_w
    gql.Sync.SnapshotNow()
    for i in range(cols - 1):                 # 当前角色占第 1 列，其余补上
        g.GQLDB["chars"]["打杂%d-永恒之井" % i] = lua.table_from(
            {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    return lua, gql, g


def matrix_visible_h(lua):
    """右窗格可视高。替身里 ScrollFrame 只 SetPoint、从不 SetSize，所以 GetHeight 回 0，
    Render 走的是"按帧高反推"那条兜底 —— 这里照抄同一条口径。"""
    return int(lua.execute("return GQLMatrixFrame.__h")) - MX_PAD_V - MX_ROW_H


def matrix_visible_w(lua):
    return int(lua.execute("return GQLMatrixFrame.__w")) - MX_NAME_W - 28


def matrix_hotzones(lua):
    """本轮**真正摆出来**的热区数（左窗格整行热区 / 右窗格状态格）。
    替身里"摆出来" = Show 过 + 挂着滚轮透传脚本：Render 尾部会把没用上的热区 Hide 掉，
    所以 Show 着的个数就是本轮摆出去的个数（usedRowBtns / usedBtns 是函数内 local，读不到）。"""
    res = lua.execute(
        "local function counted(parent, w) local n = 0 "
        "for i, c in ipairs(parent.__children) do "
        "  if c.__type == 'Button' and c.__shown and (c.__w or 0) == w "
        "     and c.__scripts and c.__scripts['OnMouseWheel'] then n = n + 1 end end "
        "return n end "
        "return counted(GQLMatrixContentL, %d), counted(GQLMatrixContent, %d)"
        % (MX_NAME_W, MX_COL_W))
    return int(res[0]), int(res[1])


def matrix_row_ys(lua):
    """本轮摆出来的整行热区各在哪一行（热区自己记着 __rowY = 行顶坐标，
    悬停扫描靠它定位高亮 —— 也是"这一行到底有没有对象"的直接证据）。"""
    s = str(lua.execute(
        "local out = {} for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__type == 'Button' and c.__shown and c.__scripts "
        "     and c.__scripts['OnMouseWheel'] then out[#out + 1] = c.__rowY end end "
        "return table.concat(out, ',')"))
    return [float(v) for v in s.split(",") if v != ""]


def test_matrix_render_clips_to_visible_rows():
    """120 行 × 6 列、可视高只放得下约 26 行：Render 只摆可见窗口（± 3 行缓冲）里的对象。
    裁的是**对象**，不是口径：rowCount 照旧数全部行、内容高照旧全量、滚动范围照旧按全量算
    （滚动条长度与当前角色整列底色全靠它）。"""
    rows, cols = 120, 6
    lua, gql, g = big_matrix_env(rows, cols)
    n = int(lua.execute("return GQL.Matrix.rowCount"))
    if n != rows:
        return fail("matrix_render_clips_to_visible_rows",
                    f"裁剪不许改 rowCount 口径（应仍数全部 {rows} 行），实际 {n}")
    row_btns, cell_btns = matrix_hotzones(lua)
    vis_h = matrix_visible_h(lua)
    min_rows = vis_h // MX_ROW_H                      # 可视区本身就放这么多行，一个都不许少
    max_rows = (vis_h + 2 * MX_OVERSCAN_ROWS * MX_ROW_H) // MX_ROW_H + 1
    if not (min_rows <= row_btns <= max_rows):
        return fail("matrix_render_clips_to_visible_rows",
                    f"整行热区该只摆可见窗口里的：{row_btns}"
                    f"（窗口上界 {max_rows}，全量 {rows}）")
    if cell_btns >= rows * cols or cell_btns < row_btns:
        return fail("matrix_render_clips_to_visible_rows",
                    f"状态格热区数该远小于全量 {rows * cols}，实际 {cell_btns}"
                    f"（本轮行热区 {row_btns}）")
    # 格子文字跟热区同一条判据：窗口外那几格连 FontString 都不摆（省下的是 SetText/SetPoint）
    shown_text = sum(1 for (_t, _f, vis) in font_strings(g.GQLMatrixContent) if vis)
    if shown_text >= rows * cols:
        return fail("matrix_render_clips_to_visible_rows",
                    f"窗口外的格子文字不该继续摆：{shown_text}（全量 {rows * cols}）")
    groups = int(lua.execute("return #GQL.Ledger.MatrixRows(GQLDB, nil)"))
    want_h = groups * MX_GROUP_H + rows * MX_ROW_H
    if int(lua.execute("return GQLMatrixContent.__h")) != want_h:
        return fail("matrix_render_clips_to_visible_rows",
                    f"右窗格内容高必须仍是全量高 {want_h}（滚动条长度、整列底色靠它）")
    if int(lua.execute("return GQLMatrixContentL.__h")) != want_h:
        return fail("matrix_render_clips_to_visible_rows", "左窗格内容高同样不许跟着裁")
    # 滚动范围按全量内容高算：滚到底该停在 contentH - visH，不是停在"画出来的最后一行"
    lua.execute("GQLMatrixScroll:GetScript('OnMouseWheel')(GQLMatrixScroll, -200)")
    off = int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))
    if off != want_h - vis_h:
        return fail("matrix_render_clips_to_visible_rows",
                    f"纵向该能滚到全量底 {want_h - vis_h}，实际 {off}")
    ok("matrix_render_clips_to_visible_rows")


def test_matrix_scroll_keeps_first_cell_after_content_shrinks():
    """内容从 120 行掉到 30 行、offY 还停在旧的大值：本轮必须先用**本轮算出的内容高**
    把 offY 夹掉，再按夹完的 offY 决定摆哪些行。按旧的偏大 offY 摆完才在末尾夹小
    → 夹完后的窗口里一个对象都没有，正是用户实测过的"滚动错位/首格被吞"。"""
    lua, gql, _ = big_matrix_env()
    vis_h = matrix_visible_h(lua)
    # 前置：滚到底，让 offY 停在旧内容的下沿
    lua.execute("GQLMatrixScroll:GetScript('OnMouseWheel')(GQLMatrixScroll, -200)")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) <= 0:
        return fail("matrix_scroll_keeps_first_cell_after_content_shrinks",
                    "前置条件没搭起来：120 行该能滚出正的偏移")
    # 账本掉到 30 行（旧 contentH 远大于新内容高，正是咬人的那一幕）
    lua.execute("local i = 0 for k in pairs(GQLDB.quests) do "
                "  i = i + 1 if i > 30 then GQLDB.quests[k] = nil end end")
    gql.Matrix.Render()
    content_h = int(lua.execute("return GQLMatrixContent.__h"))
    off = int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))
    want_off = max(0, content_h - vis_h)
    if off != want_off:
        return fail("matrix_scroll_keeps_first_cell_after_content_shrinks",
                    f"offY 该夹到 max(0, contentH - visH) = {want_off}，实际 {off}")
    ys = matrix_row_ys(lua)
    if not ys:
        return fail("matrix_scroll_keeps_first_cell_after_content_shrinks",
                    f"内容缩小后窗口内一个对象都没有（offY={off}，内容高={content_h}）= 整屏空白")
    if not any(y <= off < y + MX_ROW_H for y in ys):
        return fail("matrix_scroll_keeps_first_cell_after_content_shrinks",
                    f"窗口上沿那一行没有对象（首格被吞）：offY={off}，摆出的行 y={ys}")
    bottom = min(off + vis_h, content_h) - 1
    if not any(y <= bottom < y + MX_ROW_H for y in ys):
        return fail("matrix_scroll_keeps_first_cell_after_content_shrinks",
                    f"窗口下沿那一行没有对象（下沿留空洞）：y={bottom}，摆出的行 y={ys}")
    ok("matrix_scroll_keeps_first_cell_after_content_shrinks")


def test_matrix_hover_sweep_is_bounded_by_drawn_rows():
    """HoverScan 一帧扫的 IsMouseOver 数 = 本轮**摆出来**的热区数（它只遍历
    1..usedRowBtns / 1..usedBtns），所以开销跟着 Render 的可见区裁剪走，
    不是这段循环自己轻。120 行 × 6 列时不许再回到全量 120 + 720 那个量级。"""
    rows, cols = 120, 6
    lua, gql, _ = big_matrix_env(rows, cols)
    lua.execute(
        "GQL_MS_COUNT = 0 "
        "for _, parent in ipairs({ GQLMatrixContentL, GQLMatrixContent }) do "
        "  for i, c in ipairs(parent.__children) do "
        "    if c.__type == 'Button' then "
        "      c.IsMouseOver = function(self) "
        "        GQL_MS_COUNT = GQL_MS_COUNT + 1 return self.__mouseover == true end "
        "    end end end")
    # 鼠标压在**最后一个**摆出来的格子上：两个池子整条跑完才命中 = 一帧的最坏扫描量
    lua.execute(
        "local lastBtn "
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  if c.__type == 'Button' and c.__shown then lastBtn = c end end "
        "if not lastBtn then error('可见窗口内一个格子热区都没有') end "
        "lastBtn.__mouseover = true GQL_FOCUS = lastBtn")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    swept = int(lua.execute("return GQL_MS_COUNT"))
    if swept <= 0:
        return fail("matrix_hover_sweep_is_bounded_by_drawn_rows", "一帧都没扫，测试没跑起来")
    # 上限按几何算（可见行+缓冲）×（可见列+缓冲）× 两个池子，不读内部计数
    vis_rows = (matrix_visible_h(lua) + 2 * MX_OVERSCAN_ROWS * MX_ROW_H) // MX_ROW_H + 1
    vis_cols = (max(0, matrix_visible_w(lua)) + 2 * MX_OVERSCAN_ROWS * MX_ROW_H) // MX_COL_W + 1
    bound = vis_rows * vis_cols * 2
    if swept > bound:
        return fail("matrix_hover_sweep_is_bounded_by_drawn_rows",
                    f"一帧扫了 {swept} 次 IsMouseOver，超过可见窗口预算 {bound}"
                    f"（{vis_rows} 行 × {vis_cols} 列 × 2）")
    if swept >= rows + rows * cols:
        return fail("matrix_hover_sweep_is_bounded_by_drawn_rows",
                    f"还在按全量行×列扫（旧口径 {rows + rows * cols}），实际 {swept}")
    # 省下来的只是窗口外那些对象：摆在可见行上的悬停照旧点得亮整行
    if not bool(lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown == true")):
        return fail("matrix_hover_sweep_is_bounded_by_drawn_rows",
                    "裁到可见区后，压在可见格上的悬停仍该点亮整行高亮")
    ok("matrix_hover_sweep_is_bounded_by_drawn_rows")


def test_matrix_clip_disabled_when_viewport_unknown():
    """视口取不到数（这里把屏幕宽压成 1px → 右窗格可视宽 ≤0）→ 整轮**不裁**，
    对象数按今天的全量路径。钉住兜底：宁可多摆对象，也绝不许"视口不知道就把行藏了"
    —— 空版面是最难查的一种坏法。"""
    rows, cols = 120, 6
    lua, gql, _ = big_matrix_env(rows, cols, screen_w=1)
    if matrix_visible_w(lua) > 0:
        return fail("matrix_clip_disabled_when_viewport_unknown",
                    "前置条件没搭起来：帧宽该被压到取不出可视宽")
    n = int(lua.execute("return GQL.Matrix.rowCount"))
    row_btns, cell_btns = matrix_hotzones(lua)
    if n != rows:
        return fail("matrix_clip_disabled_when_viewport_unknown",
                    f"rowCount 该是全部 {rows} 行，实际 {n}")
    if row_btns != rows or cell_btns != rows * cols:
        return fail("matrix_clip_disabled_when_viewport_unknown",
                    f"视口取不到时该全量摆出来：整行热区 {row_btns}/{rows}、"
                    f"状态格 {cell_btns}/{rows * cols}")
    ok("matrix_clip_disabled_when_viewport_unknown")


def test_hover_shows_money_tooltip():
    """鼠标停在任务行（或状态格）上应弹出 GameTooltip：任务名 + 金币数
    （用户要求"任务hover的时候显示具体金额"）。替身里塞一个 GameTooltip 桩
    记下 AddLine 的内容。"""
    lua, gql, _ = sync_env([(107, 30000000)], extra=("Matrix",))
    g = lua.globals()
    # 必须先设时间接口再 Toggle（Toggle 内会快照、写出 expiresAt）：
    # 接口给 7200s 剩余，WallClock 固定 0 → expiresAt=7200，倒计时剩 2时0分
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    lua.execute("GQL.Api.WallClock = function() return 0 end")
    # 桩一个地图名（用户要求：hover 提示要带地图名）
    lua.execute("GQL.Api.MapName = function(id) return '虚影风暴' end")
    gql.Matrix.Toggle()
    calls = []
    tip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        # 方法调用首个参数是 self，文本在第二个
        "AddLine": lambda self, text, *a: calls.append(str(text)),
        # 标题行改成双列（左=任务名，右=灰字 questID），替身把两列拼成一行
        "AddDoubleLine": lambda self, l, r, *a: calls.append("%s %s" % (l, r)),
        "Show": lambda *a: calls.append("SHOW"),
        "Hide": lambda *a: None,
    })
    g.GameTooltip = tip
    # 模拟鼠标停在这一行（焦点也要报在这个热区上，否则悬停准入门会拦掉）
    lua.execute(
        "for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__scripts and c.__scripts['OnMouseWheel'] then "
        "    c.__mouseover = true GQL_FOCUS = c break end end")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    joined = "\n".join(calls)
    if "SHOW" not in calls:
        return fail("hover_shows_money_tooltip", f"提示应 Show 出来:\n{joined}")
    if "任务107" not in joined:
        return fail("hover_shows_money_tooltip", f"提示应含任务名:\n{joined}")
    if "3000" not in joined:
        return fail("hover_shows_money_tooltip", f"提示应含金币数:\n{joined}")
    if "虚影风暴" not in joined:
        return fail("hover_shows_money_tooltip", f"提示应含地图名:\n{joined}")
    if "剩余" not in joined or "2时" not in joined:
        return fail("hover_shows_money_tooltip", f"提示应含倒计时:\n{joined}")
    ok("hover_shows_money_tooltip")


def tip_env(qid, wall, gain=None, turnin=None, cell=False, cols=None, hover=None):
    """矩阵开着 + 停在第 qid 那一行/那一格上的环境。可选：
      gain/turnin  给本号记一笔完成流水（时刻 turnin、到手 gain）
      cols         额外角色列 {角色key: 本轮读到的金额}，金额 None = 这一号本轮没读到过
      hover        停在**哪个号**的格子上（只在 cell=True 时用；默认当前角色）

    与 hover_env 的分工：那个停在整行热区（左窗格，压根没有角色归属），
    这里要的是"鼠标压在某个号的格子上"那条路径 —— 完成时间、按号读到的金币
    只有这一条路拿得到。
    wall = GQL.Api.WallClock 的返回（"nil" = 本机没有墙上时钟，时间戳落在开机秒域）。

    cols 的金额走 Ledger.SetRewardRead（生产写点就是 Sync 那一处，这里直接落账是为了
    绕开"一次同步只有一个号"的限制）：先抓一次快照把这条任务的轮次起点定下来，
    读数再按那个起点落账 —— 顺序反了的话 Matrix.Toggle 里那次快照会把轮次往前推，
    这份读数当场被判成"上一轮的"，用例就变成在测轮次门（那是另一条用例）。
    """
    lua, gql, _ = sync_env([(qid, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda q: 7200})
    lua.execute("GQL.Api.WallClock = function() return %s end" % wall)
    lua.execute("GQL.Api.MapName = function(id) return '虚影风暴' end")
    gql.Sync.SnapshotNow()
    calls = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: calls.append(str(text)),
        # 标题行改成双列（左=任务名，右=灰字 questID），替身把两列拼成一行
        "AddDoubleLine": lambda self, l, r, *a: calls.append("%s %s" % (l, r)),
        "Show": lambda *a: calls.append("SHOW"),
        "Hide": lambda *a: None,
    })
    for key, money in (cols or {}).items():
        # 时刻取这条任务自己的轮次起点：用例不必猜同步用的是哪个时钟域，
        # 又保证这份读数落在"本轮"里（换轮那条用例自己把时刻往前挪）
        lua.execute(
            "local db = GQL.Config.DB()"
            " local at = GQL.Ledger.RoundAt(db, db.quests[%d])"
            " GQL.Ledger.SetView(db, %r, %d, 'open', at)"
            " GQL.Ledger.SetRewardRead(db, %r, %d, %s, at)"
            % (qid, key, qid, key, qid, ("nil" if money is None else money)))
    gql.Matrix.Toggle()
    if gain is not None:
        lua.execute(
            "GQL.Ledger.OnTurnIn(GQL.Config.DB(), GQL.Sync.CurrentCharKey(), %d, %d, %d, nil)"
            % (qid, turnin, gain))
    hover_key = hover or str(gql.Sync.CurrentCharKey())
    lua.execute(
        "for i, c in ipairs(%s.__children) do "
        "  if c.__questID == %d%s then c.__mouseover = true GQL_FOCUS = c break end end"
        % ("GQLMatrixContent" if cell else "GQLMatrixContentL",
           qid,
           (" and c.__charKey == %r" % hover_key) if cell else ""))
    return lua, gql, g, calls


def one(v):
    """取 Lua 侧的第一个返回值：L.State / RegisteredState / BestRegisteredState 现在多回
    一个"这一格是谁答的"（手动补录的格要换颜色），lupa 把多返回值交成 Python 元组，
    只关心状态的用例照旧读第一个。"""
    return str(v[0] if isinstance(v, tuple) else v)


def state1(fn, *a):
    """L.State / RegisteredState / BestRegisteredState 现在多回一个"这一格是谁答的"
    （手动补录的格要换颜色）。lupa 把 Lua 多返回值变成 Python 元组，
    只关心状态的用例统一走这里取第一个。"""
    v = fn(*a)
    return str(v[0] if isinstance(v, tuple) else v)


def tip_title(calls, qid):
    """取标题行：任务名 + 空格 + ID（用户 2026-10-25："任务 id 直接显示在任务名称后面，
    不要标签，直接写 id"）。单独一行"任务ID xxx"要另占一行、把提示撑长，出现即算不合格。"""
    for c in calls:
        if c.startswith("任务ID"):
            return None
    for c in calls:
        if c.endswith(" %d" % qid):
            return c
    return None


def test_hover_prints_the_quest_id():
    """悬停要报 questID（用户 2026-10-25："每个单元格悬停的时候显示任务id"）。
    玩家不可能知道 ID，可同名两条 ID 在矩阵里只留一条（L.MatrixRows 的同名去重让
    "读到过金额"那份赢），没有 ID 就分不出"这一档不给金币"说的是哪条任务，
    也拿不到 /gql quest <ID> 的入参。位置：跟在任务名后面，不另起一行。"""
    lua, gql, g, calls = tip_env(111, 1792577090)
    hover_update(lua)
    joined = "\n".join(calls)
    if tip_title(calls, 111) is None:
        return fail("hover_prints_the_quest_id",
                    f"标题行该是『任务名 ID』且没有单独的『任务ID』行:\n{joined}")
    if calls[0] != tip_title(calls, 111):
        return fail("hover_prints_the_quest_id",
                    f"ID 要并在第一行（任务名）上，实际首行 {calls[0]!r}:\n{joined}")
    ok("hover_prints_the_quest_id")


def test_cell_hover_prints_that_chars_completion_time():
    """停在某个号的格子上：报**那一列的号**上一次交这条是哪一刻、到手多少（用户要求
    "完成时间（如果有数据）"）。时刻必须真是日期时刻，不是"2分前"那种相对值 ——
    取证要的是"我周四几点做的"。"""
    lua, gql, g, calls = tip_env(112, 1792577090, gain=3000000, turnin=1792577090,
                                 cell=True)
    hover_update(lua)
    joined = "\n".join(calls)
    line = [c for c in calls if "交于" in c]
    if not line:
        return fail("cell_hover_prints_that_chars_completion_time",
                    f"格子悬停应有完成时间:\n{joined}")
    # 主语必须是那一列的号（默认夹具叫"小号一"）：格子属于列，不属于正在玩的号，
    # 写成"本号"在别的列上就是谎报
    if not re.match(r"^小号一 交于 \d{2}-\d{2} \d{2}:\d{2}", line[0]):
        return fail("cell_hover_prints_that_chars_completion_time",
                    "完成时间要报成『某号 交于 月-日 时:分』：" + line[0])
    if "到手" not in line[0]:
        return fail("cell_hover_prints_that_chars_completion_time",
                    f"完成时间要带到手金额：{line[0]}")
    if tip_title(calls, 112) is None:
        return fail("cell_hover_prints_that_chars_completion_time",
                    f"格子悬停的标题行也要带 ID:\n{joined}")
    ok("cell_hover_prints_that_chars_completion_time")


def test_row_hover_does_not_borrow_a_cells_completion():
    """整行热区（任务名那一侧）没有角色归属：不许报任何"某号 交于 …"。
    把某个号的完成时间挂在整行上 = 谎报，而这一行正是玩家看名字的地方。"""
    lua, gql, g, calls = tip_env(113, 1792577090, gain=3000000, turnin=1792577090,
                                 cell=False)
    hover_update(lua)
    joined = "\n".join(calls)
    if "交于" in joined:
        return fail("row_hover_does_not_borrow_a_cells_completion",
                    f"整行悬停不该报某个号的完成时间:\n{joined}")
    if tip_title(calls, 113) is None:
        return fail("row_hover_does_not_borrow_a_cells_completion",
                    f"整行悬停该把 ID 写在标题上:\n{joined}")
    ok("row_hover_does_not_borrow_a_cells_completion")


def test_completion_stamp_stays_quiet_without_wall_clock():
    """只有开机秒（时钟域降级，GetTime 跨重启归零）时报不出完成时刻：那一行整条不写。
    拿 3000 秒去格式化会印成 1970-01-01，那是凭空造一个日期。标题那行不受影响。"""
    lua, gql, g, calls = tip_env(114, "nil", gain=3000000, turnin=3000, cell=True)
    hover_update(lua)
    joined = "\n".join(calls)
    if "交于" in joined:
        return fail("completion_stamp_stays_quiet_without_wall_clock",
                    f"开机秒域不许编日期:\n{joined}")
    if tip_title(calls, 114) is None:
        return fail("completion_stamp_stays_quiet_without_wall_clock",
                    f"标题的 ID 与有没有墙上时钟无关:\n{joined}")
    ok("completion_stamp_stays_quiet_without_wall_clock")


def test_completion_line_hidden_once_the_instance_rolled():
    """板面换轮 = 这条任务刷成了新的一条，上一轮的交付不算本轮（用户 2026-10-25 的
    89377「秘密狩猎」：今天上午交过，可它刷新了、这一号还没做，悬停不该还印
    "小白龙 交于 10-25 09:32 到手 …"）。判据用格子自己的显示口径 L.State，
    于是界面不会出现"格子是空、下面说交过"这种自相矛盾。
    轮次起点往前推一格就代表换轮（生产里由 L.SetExpiry / RollRound 写 roundAt）。"""
    lua, gql, g, calls = tip_env(130, 1792577090, gain=3000000, turnin=1792577090,
                                 cell=True)
    hover_update(lua)
    if not [c for c in calls if "交于" in c]:
        return fail("completion_line_hidden_once_the_instance_rolled",
                    "用例前提：本轮内交过该报完成时间:\n" + "\n".join(calls))
    # 换轮：这条任务自己的起点推到那次交付之后
    lua.execute("GQLDB.quests[130].roundAt = 1792577090 + 1")
    # 重绘一次：停在同一行的提示有缓存（每帧重建会掉帧），不重画就不会重新弹
    gql.Matrix.Render()
    calls.clear()
    hover_update(lua)
    joined = "\n".join(calls)
    if "交于" in joined:
        return fail("completion_line_hidden_once_the_instance_rolled",
                    f"换轮后上一轮的交付不许再报:\n{joined}")
    if "SHOW" not in calls:
        return fail("completion_line_hidden_once_the_instance_rolled",
                    f"只是不报完成时间，提示本身照常:\n{joined}")
    ok("completion_line_hidden_once_the_instance_rolled")


def test_format_stamp_only_for_wall_clock():
    """Config.FormatStamp：纪元秒 → "月-日 时:分"；开机秒/nil/非数 → nil
    （不是 "?"，调用方按 nil 决定这一行写不写）。"""
    lua, gql = load_lua("Core", "Config", mock=True)
    s = one(lua.execute("return GQL.Config.FormatStamp(1792577090)"))
    if not re.match(r"^\d{2}-\d{2} \d{2}:\d{2}$", s):
        return fail("format_stamp_only_for_wall_clock", f"纪元秒应报成日期，实际 {s!r}")
    for bad in ("3000", "0", "-1", "nil"):
        if lua.execute("return GQL.Config.FormatStamp(%s)" % bad) is not None:
            return fail("format_stamp_only_for_wall_clock",
                        f"开机秒/缺值应回 nil，实际 FormatStamp(%s) 有值" % bad)
    ok("format_stamp_only_for_wall_clock")


def test_last_completion_takes_the_newer_record():
    """L.LastCompletion：实测流水与手动补录都在时取时刻新的那份 —— 补录更新要盖过流水，
    流水更新要盖回补录（只认数组头 = 永远报最老那条）。矩阵悬停与以后任何读完成时间的
    地方都问这一处，别在界面里再翻一遍 db.turnins：流水的存法一改报告就说瞎话。"""
    lua, gql, _ = sync_env([(115, 30000000)])
    key = str(gql.Sync.CurrentCharKey())
    db = lua.globals().GQLDB

    def got():
        # 三个返回值在 lupa 里会被尾部 nil 挤形（单值 nil 返成 None），在 Lua 侧拼成一串再取
        return one(lua.execute(
            "local t, g, s = GQL.Ledger.LastCompletion(GQL.Config.DB(), %r, 115)\n"
            "return (t and tostring(t) or '-') .. '|' .. (g and tostring(g) or '-')"
            " .. '|' .. (s or '-')" % key))

    if got() != "-|-|-":
        return fail("last_completion_takes_the_newer_record",
                    f"没记录应回 nil，实际 {got()}")
    gql.Ledger.OnTurnIn(db, key, 115, 1792577090, 3000000, None)
    if got() != "1792577090|3000000|实测":
        return fail("last_completion_takes_the_newer_record",
                    f"应报实测流水，实际 {got()}")
    gql.Ledger.SetManual(db, key, 115, "done", 1792577990, 100)
    if got() != "1792577990|100|手动补录":
        return fail("last_completion_takes_the_newer_record",
                    f"补录更新时应盖过流水，实际 {got()}")
    gql.Ledger.SetManual(db, key, 115, "done", 1792576000, 100)
    if got() != "1792577090|3000000|实测":
        return fail("last_completion_takes_the_newer_record",
                    f"补录更旧时该让回流水，实际 {got()}")
    ok("last_completion_takes_the_newer_record")


def test_cell_hover_shows_that_alts_own_gold_not_the_last_syncers():
    """同一个 questID 的奖励是各号各自的（12.1.5 实测 97128：Mad 给 1090 金、
    爱情之力给 150 宝匣钥匙碎片）。压在**金币号**的格子上要报这个号自己读到的钱，
    不许再对所有号印同一句"这一档不给金币，上面是本轮另一档读到的" ——
    给金币的号本来就给金币，那句话对它就是凭空否认。"""
    lua, gql, g, calls = tip_env(116, 1792577090, cell=True, hover="金币号-永恒之井",
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    gold = [c for c in calls if "不给金币" in c]
    if gold:
        return fail("cell_hover_shows_that_alts_own_gold_not_the_last_syncers",
                    f"这一号本轮读到的就是金币，不该有否认金币的行:\n{joined}")
    # 也不许反过来加一句"这一号没读到过奖励"：它明明读到了 1090 金
    if "没读到过" in joined:
        return fail("cell_hover_shows_that_alts_own_gold_not_the_last_syncers",
                    f"读数属于这一号，不该说它没读到过:\n{joined}")
    if "1090" not in joined:
        return fail("cell_hover_shows_that_alts_own_gold_not_the_last_syncers",
                    f"金币号该报自己读到的 1090 金:\n{joined}")
    ok("cell_hover_shows_that_alts_own_gold_not_the_last_syncers")


def test_item_cell_says_that_alt_and_names_who_read_the_gold():
    """压在**物品号**的格子上：说清"是这个号本轮读到的奖励不给金币"，
    并把钱归到真读到它的号名下 —— 只说"这一轮不给金币"是把一个号的答案
    当成整行结论（用户实测：给金币的角色都做完了，提示还说这一档不给金币）。"""
    lua, gql, g, calls = tip_env(117, 1792577090, cell=True, hover="物品号-永恒之井",
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    mine = [c for c in calls if "物品号 本轮读到的奖励不给金币" in c]
    if not mine:
        return fail("item_cell_says_that_alt_and_names_who_read_the_gold",
                    f"该按号说这一格不给金币:\n{joined}")
    if "金币号" not in joined or "本轮读到了金币" not in joined:
        return fail("item_cell_says_that_alt_and_names_who_read_the_gold",
                    f"要报出这笔金币是谁读的（带金额），否则玩家不懂这行为什么还在列:\n{joined}")
    ok("item_cell_says_that_alt_and_names_who_read_the_gold")


def test_item_cell_stays_neutral_not_an_alert():
    """不给金币那一格用中性灰，不许上红/黄：这一号本轮没得刷既不是损失、也不要求玩家
    做任何事（用户口径：没花钱的自我放弃不许和真损失共用红字）。"""
    lua, gql, g, calls = tip_env(118, 1792577090, cell=True, hover="物品号-永恒之井",
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    colors = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: colors.append((str(text), tuple(a))),
        "AddDoubleLine": lambda self, l, r, *a: colors.append(("%s %s" % (l, r), tuple(a))),
        "Show": lambda *a: None,
        "Hide": lambda *a: None,
    })
    hover_update(lua)
    line = [c for c in colors if "不给金币" in c[0] and "物品号" in c[0]]
    if not line:
        return fail("item_cell_stays_neutral_not_an_alert",
                    f"用例没抓到那一行: {[c[0] for c in colors]}")
    rgb = line[0][1]
    if len(rgb) < 3 or max(rgb[:3]) - min(rgb[:3]) > 0.2:
        return fail("item_cell_stays_neutral_not_an_alert",
                    f"『这一号不给金币』必须是中性色（r≈g≈b），实际 {rgb}")
    ok("item_cell_stays_neutral_not_an_alert")


def test_unsynced_cell_gets_no_attribution_line():
    """这一号本轮没读到过奖励（没同步过）：悬停只印行上的金额，**不加一句归属解释**
    （用户 2026-10-25："上面是 xx 读到的，本轮还没读到，这个就没必要显示了"）。
    但也不许反过来说它"不给金币" —— 客户端压根没替这一号答过，那句是替它下结论。"""
    lua, gql, g, calls = tip_env(119, 1792577090, cell=True, hover="没同步号-永恒之井",
                                 cols={"金币号-永恒之井": 10900000, "没同步号-永恒之井": None})
    hover_update(lua)
    joined = "\n".join(calls)
    for gone in ("没读到过", "上面是", "本轮读到的"):
        if gone in joined:
            return fail("unsynced_cell_gets_no_attribution_line",
                        f"归属解释那一行已经按用户要求删掉，不该出现「{gone}」:\n{joined}")
    if "不给金币" in joined:
        return fail("unsynced_cell_gets_no_attribution_line",
                    f"没读到过 ≠ 客户端答了不给金币:\n{joined}")
    if "SHOW" not in calls:
        return fail("unsynced_cell_gets_no_attribution_line",
                    f"删的是废话，提示本身照常:\n{joined}")
    ok("unsynced_cell_gets_no_attribution_line")


def test_row_hover_stays_free_of_alt_names():
    """整行热区没有角色归属：既不该报某个号的完成时间，也不该报某个号读到的金币
    —— 上一版为了"钱是谁读的"在行上指点号名，用户 2026-10-25 嫌这套话没用，
    整条删掉。这一用例把两条路一起钉住：删完只剩任务本身的信息。"""
    lua, gql, g, calls = tip_env(120, 1792577090, cols={
        "金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    if "交于" in joined:
        return fail("row_hover_stays_free_of_alt_names",
                    f"整行悬停不该报某个号的完成时间:\n{joined}")
    for gone in ("上面是", "本轮读到的", "没读到过", "金币号", "物品号"):
        if gone in joined:
            return fail("row_hover_stays_free_of_alt_names",
                        f"行悬停不该点名某个号（「{gone}」）:\n{joined}")
    if tip_title(calls, 120) is None:
        return fail("row_hover_stays_free_of_alt_names",
                    f"删废话不许把 ID 一起删掉:\n{joined}")
    ok("row_hover_stays_free_of_alt_names")


def test_manual_done_records_that_alts_own_gain():
    """点格子补录"这一号做完了"：到手金额取**这一号自己本轮读到的**那份。
    12.1.5 实测 97128 在两个号上分别给 1090 金和 150 碎片，而原来记的是行金额
    （最后一次同步那个号的数字）—— 白干的号被记上一笔金，那一列的周汇总从此虚高，
    用户截图里"爱情之力 手动补录 到手 1070 金"就是这么来的。
    这一号答了 0 就记 0（白干也是真答案）；这一号没答过而别人答过 → 留空显示 ?，绝不编。"""
    lua, gql, g, calls = tip_env(125, 1792577090, cols={
        "金币号-永恒之井": 10900000, "物品号-永恒之井": 0, "没同步号-永恒之井": None})

    def gain_of(key):
        return one(lua.execute(
            "local m = GQLDB.manual[%r] and GQLDB.manual[%r][125]"
            " return tostring(m and m.g)" % (key, key)))

    for who in ("金币号-永恒之井", "物品号-永恒之井", "没同步号-永恒之井"):
        lua.execute(
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  if c.__questID == 125 and c.__charKey == %r and c.__scripts "
            "     and c.__scripts['OnClick'] then c.__scripts['OnClick'](c) return end end" % who)
    if gain_of("金币号-永恒之井") != "10900000":
        return fail("manual_done_records_that_alts_own_gain",
                    f"金币号该记自己读到的 1090 金，实际 {gain_of('金币号-永恒之井')}")
    if gain_of("物品号-永恒之井") != "0":
        return fail("manual_done_records_that_alts_own_gain",
                    f"客户端答了不给金币就记 0，实际 {gain_of('物品号-永恒之井')}")
    if gain_of("没同步号-永恒之井") != "nil":
        return fail("manual_done_records_that_alts_own_gain",
                    "这一号没答过就不该拿别人的金额补上去（应为 nil → 界面显示 ?），实际 "
                    + gain_of("没同步号-永恒之井"))
    ok("manual_done_records_that_alts_own_gain")


def test_sync_records_the_reward_answer_per_character():
    """按号的奖励读数只有一个写点：Sync 的池循环（跟 views 同一趟）。
    没有这一笔，账本里就只剩"最后一次同步的那个号看到的那一副"，
    矩阵对所有号只能报同一个答案。金额读不出（nil）不落账 —— 那是"没答"，
    写进去就变成"这一号不给金币"。"""
    lua, gql, _ = sync_env([(121, 10900000), (122, None)])
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    g = lua.globals()
    got = one(lua.execute("local r = GQLDB.chars[%r].reads\n"
                          "return tostring(r and r[121] and r[121].g)" % key))
    if got != "10900000":
        return fail("sync_records_the_reward_answer_per_character",
                    f"池里读到 1090 金该按号落账，实际 reads[121].g={got}")
    if lua.execute("local r = GQLDB.chars[%r].reads\n"
                   "return r and r[122] ~= nil" % key):
        return fail("sync_records_the_reward_answer_per_character",
                    "金额没读到的那条不该落账：nil ≠ 这一号不给金币")
    ok("sync_records_the_reward_answer_per_character")


def test_reward_read_stops_counting_when_the_round_rolls():
    """上一轮那一份奖励不算本轮：换轮（L.RoundAt 往前推）后 CharRewardMoney 必须答 nil，
    否则本轮刚换成物品的那一档会被上一轮的金币数冒充"这一号本轮有金币可刷"。
    与 L.State 的视图门同一口径（比的是这条实例自己的起点，不是 CD）。"""
    lua, gql, _ = sync_env([(123, 10900000)])
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    db = lua.globals().GQLDB
    if gql.Ledger.CharRewardMoney(db, key, 123) != 10900000:
        return fail("reward_read_stops_counting_when_the_round_rolls",
                    "本轮读到的金币该报出来")
    gql.Ledger.SetRewardRead(db, key, 123, 10900000, 500)
    lua.execute("GQLDB.quests[123].roundAt = 900")
    if gql.Ledger.CharRewardMoney(db, key, 123) is not None:
        return fail("reward_read_stops_counting_when_the_round_rolls",
                    "时刻早于本轮起点的那一份不许冒充本轮")
    ok("reward_read_stops_counting_when_the_round_rolls")


def test_gold_readers_lists_only_this_rounds_gold_answers():
    """"本轮读到金币的号"清单：0（客户端答了不给金币）、上一轮的读数、压根没读数的号
    都不许进 —— 这份名单直接进悬停和 /gql quest 的报告，多一个名字就是谎报。
    顺序必须稳定（pairs 无序，不排序的话同一个号一次悬停一个样）。"""
    lua, gql, _ = sync_env([(124, 10900000)])
    gql.Sync.SnapshotNow()
    db = lua.globals().GQLDB
    key = str(gql.Sync.CurrentCharKey())
    at = gql.Ledger.RoundAt(db, db.quests[124])
    gql.Ledger.SetRewardRead(db, "乙读零-永恒之井", 124, 0, at)
    gql.Ledger.SetRewardRead(db, "丙上一轮-永恒之井", 124, 9000000, at - 1)
    gql.Ledger.SetRewardRead(db, "甲读金币-永恒之井", 124, 7000000, at)
    gql.Ledger.SetView(db, "丁没读-永恒之井", 124, "open", at)
    # 名单在 Lua 侧拼成一串再取：lupa 直接迭代数组会连 key 一起带出来
    def readers():
        return one(lua.execute(
            "local r = GQL.Ledger.GoldReaders(GQL.Config.DB(), 124)\n"
            "return r and table.concat(r, '|') or '-'"))
    want = "|".join(sorted([key, "甲读金币-永恒之井"]))
    if readers() != want:
        return fail("gold_readers_lists_only_this_rounds_gold_answers",
                    f"名单应只有本轮读到金币的两个号、并按 key 排好序，实际 {readers()!r}")
    ok("gold_readers_lists_only_this_rounds_gold_answers")


def test_matrix_char_name_max4_no_ellipsis():
    """列头角色名最多取前 4 个字，超出直接截掉、不加省略号（用户要求）；
    当前角色的"◀"标记删掉（部分字体渲染成方块，用户实测不要显示）。"""
    lua, gql, _ = sync_env([(94, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.GQLDB["chars"]["回到最橙灯光之力-永恒之井"] = lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "回到最橙" not in joined:
        return fail("matrix_char_name_max4_no_ellipsis",
                    f"列头应显示名字前 4 个字:\n{joined}")
    if "灯光" in joined or "回到最橙…" in joined:
        return fail("matrix_char_name_max4_no_ellipsis",
                    f"超过 4 字应直接截掉（不加省略号）:\n{joined}")
    if "◀" in joined:
        return fail("matrix_char_name_max4_no_ellipsis",
                    f"当前角色的 ◀ 标记应删掉（字体渲染成方块）:\n{joined}")
    ok("matrix_char_name_max4_no_ellipsis")


def test_matrix_header_frozen_while_body_scrolls():
    """表头条（角色名列头）钉在滚动区之外：往下滚正文，列头一个像素都不该动
    （用户实测"表头跟着滚，扫不清哪列是谁"）。列头挂在 headR 上、
    锚点相对 headR，滚动只改 ScrollFrame 的滚动值。"""
    lua, gql, _ = sync_env([(107, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    for i in range(30):
        g.GQLDB["quests"][200 + i] = lua.table_from({
            "name": "凑行任务%d" % i, "mapID": 2007,
            "observations": lua.table_from({
                "30000000": lua.table_from({"money": 30000000, "gold": True,
                                            "firstSeen": 1000, "lastSeen": 1000})}),
            "currentSig": "30000000"})
    gql.Matrix.Render()

    def head_state():
        return lua.execute(
            "for i, c in ipairs(GQLMatrixHeadR.__children) do "
            "  if c.__text and string.find(c.__text, '小号一', 1, true) then "
            "    local p = c.__points[1] "
            "    return tostring(p[2] == GQLMatrixHeadR)..'/'..tostring(p[4])"
            "      ..'/'..tostring(p[5]) end end return ''")

    before = str(head_state())
    if "/" not in before:
        return fail("matrix_header_frozen_while_body_scrolls", "没找到列头字体串")
    if not before.startswith("true/"):
        return fail("matrix_header_frozen_while_body_scrolls",
                    f"列头应锚在固定表头条上，实际 {before}")
    lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "for i = 1, 5 do f(GQLMatrixScroll, -1) end")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) <= 0:
        return fail("matrix_header_frozen_while_body_scrolls", "正文应已往下滚")
    if str(head_state()) != before:
        return fail("matrix_header_frozen_while_body_scrolls",
                    f"纵向滚后列头动了：{before} -> {head_state()}")
    ok("matrix_header_frozen_while_body_scrolls")


def test_matrix_thumb_drag_follows_cursor():
    """滚动条必须拖得动、而且跟手：往下拖 = 往下滚。
    GetCursorPosition 的 y 从屏幕底往上算、单位是物理像素，所以纵向要取反、
    两轴都要除以屏幕缩放；漏掉任何一样，滑块都反着跑（用户实测"拖不动，只能滚轮"）。"""
    lua, gql, _ = sync_env([(108, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    for i in range(30):
        g.GQLDB["quests"][200 + i] = lua.table_from({
            "name": "凑行任务%d" % i, "mapID": 2007,
            "observations": lua.table_from({
                "30000000": lua.table_from({"money": 30000000, "gold": True,
                                            "firstSeen": 1000, "lastSeen": 1000})}),
            "currentSig": "30000000"})
    gql.Matrix.Render()
    # 替身里锚定尺寸算不出来，手动给出轨道/滑块尺寸与缩放
    lua.execute("GQLMatrixBarV.__h, GQLMatrixThumbV.__h = 200, 40 "
                "GQLMatrixBarV.__scale = 1")

    def to_top():
        lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                    "for i = 1, 5 do f(GQLMatrixScroll, 1) end")

    def drag(px, scale):
        to_top()
        lua.execute("GQLMatrixBarV.__scale = %s "
                    "GQL_CURSOR[1], GQL_CURSOR[2] = 500, 500 "
                    "local d = GQLMatrixThumbV:GetScript('OnMouseDown') "
                    "d(GQLMatrixThumbV, 'LeftButton')" % scale)
        lua.execute("GQL_CURSOR[1], GQL_CURSOR[2] = 500, %d "
                    "local u = GQLMatrixThumbV:GetScript('OnUpdate') "
                    "u(GQLMatrixThumbV)" % px)
        return int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()"))

    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != 0:
        return fail("matrix_thumb_drag_follows_cursor", "起点应回到顶部")
    at_scale1 = drag(460, 1)      # 手往下移 40px
    if at_scale1 <= 0:
        return fail("matrix_thumb_drag_follows_cursor",
                    f"往下拖滑块应往下滚，实际滚动值 {at_scale1}")
    at_scale2 = drag(460, 2)      # 缩放 2 倍：同样的手位移只该滚一半
    if not (0 < at_scale2 < at_scale1):
        return fail("matrix_thumb_drag_follows_cursor",
                    f"光标位移要除屏幕缩放，2 倍缩放进 {at_scale2} vs 1 倍 {at_scale1}")
    # 往上拖回到上面（正向位移应减小偏移，clamp 到 0 不许翻负）
    to_top()
    lua.execute("GQLMatrixBarV.__scale = 1 GQL_CURSOR[1], GQL_CURSOR[2] = 500, 500 "
                "local d = GQLMatrixThumbV:GetScript('OnMouseDown') "
                "d(GQLMatrixThumbV, 'LeftButton')")
    lua.execute("GQL_CURSOR[1], GQL_CURSOR[2] = 500, 560 "
                "local u = GQLMatrixThumbV:GetScript('OnUpdate') u(GQLMatrixThumbV)")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != 0:
        return fail("matrix_thumb_drag_follows_cursor", "到顶后继续往上拖应夹在 0")
    ok("matrix_thumb_drag_follows_cursor")


def test_matrix_max_height_follows_screen():
    """最大高度自适应：内容少 = 按内容收高且滚不动；内容多 = 封顶屏幕高 85%，
    超出才纵向滚（用户实测：以前写死 480，屏幕明明还空着）。"""
    lua, gql, _ = sync_env([(109, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.UIParent.GetHeight = lambda self: 800        # 模拟 800px 高的屏幕
    gql.Matrix.Toggle()
    # 1 个任务：正文 2 行（组头 + 任务行），自然高低于下限 → 取 200，不该滚得动
    if int(lua.execute("return GQLMatrixFrame.__h")) != 200:
        return fail("matrix_max_height_follows_screen",
                    f"内容少时帧高应按内容收（下限 200），实际 "
                    f"{lua.execute('return GQLMatrixFrame.__h')}")
    lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "for i = 1, 3 do f(GQLMatrixScroll, -1) end")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) != 0:
        return fail("matrix_max_height_follows_screen", "内容放得下就不该出滚动")
    # 塞满正文：自然高 56+20+32*20=716 > 800*0.85=680 → 封顶 680 并开始滚
    for i in range(30):
        g.GQLDB["quests"][200 + i] = lua.table_from({
            "name": "凑行任务%d" % i, "mapID": 2007,
            "observations": lua.table_from({
                "30000000": lua.table_from({"money": 30000000, "gold": True,
                                            "firstSeen": 1000, "lastSeen": 1000})}),
            "currentSig": "30000000"})
    gql.Matrix.Render()
    h = int(lua.execute("return GQLMatrixFrame.__h"))
    if h != 680:
        return fail("matrix_max_height_follows_screen",
                    f"帧高应封顶屏幕高 85%=680，实际 {h}")
    lua.execute("local f = GQLMatrixScroll:GetScript('OnMouseWheel') "
                "f(GQLMatrixScroll, -1)")
    if int(lua.execute("return GQLMatrixScroll:GetVerticalScroll()")) <= 0:
        return fail("matrix_max_height_follows_screen", "封顶后应能纵向滚动")
    ok("matrix_max_height_follows_screen")


def test_scrollbars_are_visible_not_white_line():
    """滚动条要看得见：底色纹理 WHITE8X8 本身是白的，不显式上色的话
    轨道滑块一片白，用户实测"底部只有一条白线"。轨道深灰、滑块亮灰。"""
    lua, gql, _ = sync_env([(106, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    # 找滚动条帧（有 backdrop 颜色的子帧），验证上过色
    colored = lua.execute(
        "local n = 0 for i, c in ipairs(GQLMatrixFrame.__children) do "
        "  if c.__bdcolor then n = n + 1 end end return n")
    if int(colored) < 2:
        return fail("scrollbars_are_visible_not_white_line",
                    f"轨道/滑块都应显式上色，实际 {colored} 个")
    ok("scrollbars_are_visible_not_white_line")


def test_matrix_title_drops_readonly_label():
    """标题去掉"（只读）"（用户要求）：矩阵现在能点格子记状态，那三个字早就不成立。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "只读" in joined:
        return fail("matrix_title_drops_readonly_label", f"标题不该再有 只读:\n{joined}")
    if "任务×角色矩阵" not in joined:
        return fail("matrix_title_drops_readonly_label", f"标题应保留矩阵名:\n{joined}")
    ok("matrix_title_drops_readonly_label")


def test_matrix_refresh_button_sits_by_settings_and_resyncs():
    """用户 2026-10-24 反馈②："刷新矩阵的按钮放在设置旁边"。上一版"刷新"只长在设置
    面板里（补扫按钮旁边），玩家盯着矩阵怀疑数据旧了，得先进设置再点 —— 现场没有兜底。
    判据三条：标题栏有"刷新"、它锚在"设置"左边、点一次真的走 SnapshotNow 重画
    （接线用例走真实入口，不直接调 Mx.Render 自证）。"""
    lua, gql, _ = sync_env([(101, 30000000)], extra=("Matrix",))
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    why = lua.execute("""
        local function find(f, text)
            local kids = f.__children or {}
            for i = 1, #kids do
                if kids[i].__text == text then return kids[i] end
                local got = find(kids[i], text)
                if got then return got end
            end
        end
        local r = find(GQLMatrixFrame, "刷新")
        if not r then return "标题栏没有刷新按钮" end
        if not r.__scripts or not r.__scripts["OnClick"] then return "刷新按钮没挂 OnClick" end
        local c = find(GQLMatrixFrame, "设置")
        if not c then return "标题栏没有设置按钮" end
        local p = (r.__points or {})[1]
        if not p or p[1] ~= "RIGHT" or p[2] ~= c or p[3] ~= "LEFT" then
            return "刷新没锚在设置的左边"
        end
        return nil
    """)
    if why:
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs", str(why))
    # 清掉账本再点：记录回来了才说明这一趟真抓了池（不是只重画了一遍旧数据）
    lua.execute("GQL.Config.DB().quests = {}")
    # 轮询关着点刷新：刷新必须自己把它打开，否则 SnapshotNow 末尾的"最早到期唤醒"
    # 定时器会被 SetPolling(false) 直接跳过，刷新完矩阵就再也不自己更新了
    gql.Sync.SetPolling(False)
    if not click_frame_by_text(lua, "GQLMatrixFrame", "刷新"):
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs", "点不动刷新按钮")
    if int(lua.execute("return GQL.Sync.Polling() and 1 or 0")) != 1:
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs",
                    "点刷新没打开轮询：刷新之后到期唤醒定时器排不上")
    if lua.execute("return GQL.Config.DB().quests[101] ~= nil") is not True:
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs",
                    "点刷新没真抓池（账本里那条 101 没回来）")
    # 悬停说明必须是实测存在的写法：OnEnter/OnLeave + GameTooltip。
    # 12.1.5 实测按钮**没有** SetTooltipDescriptionFunc，调它 Ensure 直接断在半路。
    if lua.execute("""
        local function find(f, text)
            local kids = f.__children or {}
            for i = 1, #kids do
                if kids[i].__text == text then return kids[i] end
                local got = find(kids[i], text)
                if got then return got end
            end
        end
        local r = find(GQLMatrixFrame, "刷新")
        return r ~= nil and r.__scripts["OnEnter"] ~= nil
            and r.__scripts["OnLeave"] ~= nil
    """) is not True:
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs",
                    "刷新按钮的悬停说明没走 OnEnter/OnLeave（12.1.5 没有 SetTooltipDescriptionFunc）")
    # 12.1.5 事故的判据：Ensure 中途抛错时 frame 已经建好、文字池还没建，
    # 之后每次 Toggle 都在开头 return 这张半成品皮 → 玩家看到"打开一篇漆黑、再也打不开"。
    # 所以这里要求矩阵里真的画出了行文本，光"按钮找得到"验不到这一层。
    drawn = lua.execute("""
        local n = 0
        local function walk(f)
            local kids = f.__children or {}
            for i = 1, #kids do
                if (kids[i].__text or "") ~= "" then n = n + 1 end
                walk(kids[i])
            end
        end
        walk(GQLMatrixFrame)
        return n
    """)
    if drawn is None or int(drawn) < 4:
        return fail("matrix_refresh_button_sits_by_settings_and_resyncs",
                    "矩阵只画出了皮、没有行文本（Ensure 断在半路的症状）")
    ok("matrix_refresh_button_sits_by_settings_and_resyncs")


def test_windows_are_opaque():
    """矩阵和设置面板都要不透明（用户实测"不需要半透明，没有意义"）：
    对话框纹理是半透明的，底下的团队框架会透上来，还会让人以为鼠标穿透了。"""
    lua, gql, _ = sync_env([(121, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    gql.Matrix.Toggle()
    gql.Options.Open()
    for name in ("GQLMatrixFrame", "GQLOptionsFrame"):
        bg = one(lua.execute("return %s.__backdrop and %s.__backdrop.bgFile" % (name, name)))
        alpha = lua.execute("return %s.__bdcolor and %s.__bdcolor[4]" % (name, name))
        if "WHITE8X8" not in bg:
            return fail("windows_are_opaque", f"{name} 背板应是纯色纹理，实际 {bg}")
        if alpha is None or abs(float(alpha) - 1.0) > 1e-6:
            return fail("windows_are_opaque", f"{name} 背板 alpha 应是 1，实际 {alpha}")
    ok("windows_are_opaque")


def test_hover_ignores_foreign_mouse_focus():
    """别的窗口压在矩阵上面时，矩阵不许亮行：IsMouseOver 只比几何、不看遮挡，
    鼠标在设置窗口里却把矩阵那行点亮、tooltip 弹出（用户实测反馈）。
    现在先问 GetMouseFocus，焦点不在矩阵子树里就整条跳过。"""
    lua, gql, _ = sync_env([(122, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()

    def hover_row():
        lua.execute("for i, c in ipairs(GQLMatrixContentL.__children) do "
                    "  if c.__scripts and c.__scripts['OnMouseWheel'] and (c.__w or 0) >= 100 "
                    "    then c.__mouseover = true GQL_FOCUS = c return end end")

    def lit():
        # 直接看那条整行高亮帧（Render 里的当前角色列底也是子帧，别混进来）
        return bool(lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown"))

    hover_row()
    # 焦点就是被压着的那个热区本身（在矩阵子树里）→ 应该亮
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if not lit():
        return fail("hover_ignores_foreign_mouse_focus", "焦点在矩阵内时整行高亮应出现")
    # 焦点换成矩阵之外的窗口（设置面板压在上面）→ 不许再亮
    lua.execute("GQL_FOCUS = GQLOptionsFrame or CreateFrame('Frame', nil, UIParent)")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if lit():
        return fail("hover_ignores_foreign_mouse_focus", "焦点在别的窗口时矩阵不该亮行")
    ok("hover_ignores_foreign_mouse_focus")


def suppress_logs(lua):
    """抑制日志的条数（口径：焦点不在矩阵上但几何压到了热区时留一条线索）。"""
    return int(lua.execute("local c = 0 local db = GQL.Config.DB() "
                           "for _, m in ipairs((db and db.logs) or {}) do "
                           "  if string.find(m, 'hover 抑制', 1, true) then c = c + 1 end end "
                           "return c"))


def test_hover_needs_focus_confirmed_on_matrix():
    """焦点问不出来（nil）或报的是 UIParent（鼠标落在不吃鼠标的地方，比如盖在矩阵
    上面的透明窗口）时，旧口径会退回纯几何判断，金额提示仍从压着的窗口底下弹出来
    （用户实测"影响不好"）。现在两种都必须整条不亮，而且焦点回到矩阵上时 hover 要
    能恢复 —— 不许为了遮挡把悬停整个哑掉。"""
    lua, gql, g, calls = hover_env(121)
    lit = lambda: bool(lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown"))
    hover_update(lua)
    if "SHOW" not in calls or not lit():
        return fail("hover_needs_focus_confirmed_on_matrix", "起点：焦点在热区上应亮行并弹提示")

    for expr, label in (("nil", "无焦点"), ("UIParent", "UIParent")):
        lua.execute("GQL_FOCUS = " + expr)
        del calls[:]
        hover_update(lua)        # 我们自己弹过的那条要收掉，允许一次 HIDE
        del calls[:]
        if lit():
            return fail("hover_needs_focus_confirmed_on_matrix",
                        "%s：焦点不在矩阵上时不许亮行" % label)
        if "SHOW" in calls:
            return fail("hover_needs_focus_confirmed_on_matrix",
                        "%s：焦点不在矩阵上时不许弹提示" % label)

    # 同一个焦点持续压着：日志只留第一条，不许每帧刷
    lua.execute("GQL_FOCUS = nil")
    hover_update(lua)
    n = suppress_logs(lua)
    if n < 1:
        return fail("hover_needs_focus_confirmed_on_matrix",
                    "抑制时要在日志里留一条线索，否则游戏内复现查不出是谁压的")
    for _ in range(5):
        hover_update(lua)
    if suppress_logs(lua) != n:
        return fail("hover_needs_focus_confirmed_on_matrix",
                    "同一个焦点不该每帧写一条抑制日志（%d -> %d）" % (n, suppress_logs(lua)))

    # 焦点回到矩阵热区 → 提示必须恢复
    del calls[:]
    lua.execute("GQL_FOCUS = GQL_HOVERBTN")
    hover_update(lua)
    if "SHOW" not in calls or not lit():
        return fail("hover_needs_focus_confirmed_on_matrix",
                    f"焦点回到矩阵后 hover 应恢复:\n{chr(10).join(calls)}")
    ok("hover_needs_focus_confirmed_on_matrix")


def test_hide_money_tooltip_only_ours():
    """收提示只收我们自己弹的那条：压在矩阵上的窗口可能正在用同一个 GameTooltip
    （比如显示一个背包物品），无条件 Hide 会把人家的提示一起吞掉（用户实测"影响不好"
    的另一半）。"""
    lua, gql, g, calls = hover_env(124)
    hover_update(lua)
    if "SHOW" not in calls:
        return fail("hide_money_tooltip_only_ours", "起点：应弹过我们自己的提示")
    del calls[:]
    # 鼠标移到盖着的窗口上（焦点出去、几何还压着行）→ 收回自己那条
    lua.execute("GQL_FOCUS = nil")
    hover_update(lua)
    if "HIDE" not in calls:
        return fail("hide_money_tooltip_only_ours", "焦点离开后要把自己弹的提示收掉")
    del calls[:]
    # 之后提示已经被别人接手：不许再动 GameTooltip
    for _ in range(3):
        hover_update(lua)
    if calls:
        return fail("hide_money_tooltip_only_ours",
                    f"没在弹自己提示时不许碰 GameTooltip:\n{chr(10).join(calls)}")
    ok("hide_money_tooltip_only_ours")


def test_hover_suppressed_when_window_covers_matrix():
    """画在矩阵上面的窗口如果不吃鼠标，焦点门根本拦不住：12.1.5 实测暴雪地图盖住
    矩阵后，鼠标停在地图空白处，GetMouseFocus 穿透到压在下面的矩阵热区上，金额提示
    照样弹。所以遮挡要自己按绘制序算：候选窗口层序在我们之上、且盖住这个热区 → 不亮。
    但通用扫描会撞上各种常驻全屏帧（客户端自己的容器、别的插件的遮罩），一律拦的结果
    是 hover 整个没了（同一次实测）—— 所以只有名单内的窗口真拦，名单外只记日志。"""
    lua, gql, g, calls = hover_env(126)
    lit = lambda: bool(lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown"))

    def logged(sub):
        return int(lua.execute("local n = 0 for _, m in ipairs((GQL.Config.DB().logs) or {}) do "
                               "if string.find(m, '%s', 1, true) then n = n + 1 end end return n" % sub))

    def cover(expr):
        """换掉盖着的窗口并把 0.5 秒遮挡缓存过掉，然后跑一帧。"""
        lua.execute("WorldMapFrame:Hide() if CoverMock then CoverMock:Hide() end "
                    "if TimerMock then TimerMock:Hide() end")
        g.GQL_NOW = (g.GQL_NOW or 0) + 1
        lua.execute(expr)
        del calls[:]
        hover_update(lua)

    # 暴雪地图：盖满全屏、层序在矩阵之上（DIALOG > 默认 MEDIUM）、不吃鼠标、名单内 → 拦
    lua.execute("WorldMapFrame:SetFrameStrata('DIALOG') WorldMapFrame:SetSize(1440, 720) "
                "WorldMapFrame.__cx, WorldMapFrame.__cy = 0, 0 WorldMapFrame:Show()")
    del calls[:]
    hover_update(lua)
    if "SHOW" in calls or lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "被名单内的地图盖住时不许亮行、不许弹提示:\n" + "\n".join(calls))
    if logged("热区被 WorldMapFrame 盖住") < 1:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "遮挡抑制要在日志里留下盖住者的名字，否则游戏内查不出是谁")

    # 名单外的别的插件全屏容器：只记一条，hover 必须照常。
    # 这一条就是 12.1.5 那次"提示彻底没了"的回归守卫。
    lua.execute("CoverMock = CreateFrame('Frame', 'SomeOtherAddonPanel', UIParent) "
                "CoverMock:SetFrameStrata('DIALOG') CoverMock:SetSize(1440, 720) "
                "CoverMock.__cx, CoverMock.__cy = 0, 0")
    cover("CoverMock:Show()")
    if not lit() or "SHOW" not in calls:
        return fail("hover_suppressed_when_window_covers_matrix",
                    f"名单外的覆盖帧不许拦 hover:\n{chr(10).join(calls)}")
    if logged("名单外") < 1:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "名单外的命中也要留日志，否则游戏内不知道该把谁加进名单")
    # 同一个名单外帧持续压着：线索只留一条。12.1.5 实测 TimerTracker 每帧都命中，
    # 按帧写就把整份日志挤成同一句话，真正有用的行全被冲掉。
    n = logged("名单外")
    for _ in range(20):
        g.GQL_NOW = (g.GQL_NOW or 0) + 1
        hover_update(lua)
    if logged("名单外") != n:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "同一个名单外帧不该每次都写抑制日志（%d -> %d）" % (n, logged("名单外")))

    # 名单外帧排在名单内**前面**时不许把它挡在后面：12.1.5 实测常驻全屏的 TimerTracker
    # 是 UIParent 靠前的子帧，"找到第一个命中就返回"让地图永远轮不上判定 ——
    # 用户截图里地图明明压着矩阵，日志却只有 TimerTracker 那一条。
    lua.execute("TimerMock = CreateFrame('Frame', 'TimerTracker', UIParent) "
                "TimerMock:SetFrameStrata('DIALOG') TimerMock:SetSize(1440, 720) "
                "table.insert(UIParent.__children, 1, TimerMock)")
    n = logged("热区被 WorldMapFrame 盖住")
    cover("TimerMock:Show() WorldMapFrame:Show()")
    if lit() or logged("热区被 WorldMapFrame 盖住") <= n:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "名单外帧排在前面时，名单内的地图仍要判成遮挡:\n" + "\n".join(calls))

    # 地图收起来 → hover 恢复（此时 TimerTracker 还压着，只算名单外线索）
    cover("TimerMock:Show()")
    if "SHOW" not in calls or not lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "盖住的窗口收掉后 hover 应恢复:\n" + "\n".join(calls))

    # 名单内的窗口**不在 UIParent 子帧列表里**时照样要拦：判定走全局名那条路，
    # 不依赖列表口径（客户端把地图挪去别处挂也不该影响功能）。
    # （把地图从顶层列表里摘掉，模拟"不是直接子帧"）
    lua.execute("for i = #UIParent.__children, 1, -1 do "
                "  if UIParent.__children[i] == WorldMapFrame then "
                "    table.remove(UIParent.__children, i) end end")
    cover("WorldMapFrame:Show()")
    if lit() or "SHOW" in calls:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "名单内窗口不是 UIParent 直接子帧时也要拦下 hover:\n" + "\n".join(calls))
    cover("WorldMapFrame:Hide()")
    if not lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "地图收掉后 hover 应恢复")

    # 画在我们**下面**（BACKGROUND）：不算遮挡 —— 误杀 hover 比漏判更糟。
    # 停在同一行不会重建提示（那是省掉每帧重画的优化），所以只看高亮还在不在。
    cover("WorldMapFrame:SetFrameStrata('BACKGROUND') WorldMapFrame:Show()")
    if not lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "画在矩阵下面的名单内窗口不该算遮挡（亮行=%s）" % lit())

    # 12.1.5 probe 实测到的真配置：地图与矩阵同为 MEDIUM、层号同为 1、地图自己没背板，
    # 开图后它却画在我们上面（同层同层号谁在上面由客户端重排顺序定，API 问不出来）。
    # 名单内的窗口同层也要拦 —— 前几轮"整份日志里一条 WorldMapFrame 都没有"就卡在这。
    cover("WorldMapFrame:SetFrameStrata('MEDIUM') WorldMapFrame:SetFrameLevel(1) "
          "WorldMapFrame:Show()")
    if lit() or "SHOW" in calls:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "名单内窗口同层同层号时也要拦（实测地图正是这个配置）:\n"
                    + "\n".join(calls))

    cover("WorldMapFrame:Hide()")
    if not lit():
        return fail("hover_suppressed_when_window_covers_matrix", "地图收掉后 hover 应恢复")

    # 同层的**名单外**全屏容器：照样不许误杀 —— 暴雪在 MEDIUM 层挂了一堆布局容器，
    # 里面就有层号更高、还带背板的，认它们等于把 hover 永久哑掉（第三次实测的教训）。
    cover("CoverMock:SetFrameStrata('MEDIUM') CoverMock:SetFrameLevel(20) "
          "CoverMock:SetBackdrop({ bgFile = 'Interface/Buttons/WHITE8X8' }) CoverMock:Show()")
    if not lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "同层的名单外容器不该拦 hover（亮行=%s）" % lit())

    # 名单内但几乎透明（alpha < 0.5）：画不出东西，不算遮挡
    cover("WorldMapFrame:SetAlpha(0.2) WorldMapFrame:Show()")
    if not lit():
        return fail("hover_suppressed_when_window_covers_matrix",
                    "半透明（alpha 0.2）的名单内窗口不该拦 hover")
    ok("hover_suppressed_when_window_covers_matrix")



def test_hover_debug_reports_why_a_window_is_not_a_candidate():
    """/gql probe 的"悬停遮挡"段是这一串 bug 唯一的取证入口：光看抑制日志分不清
    "不是顶层子帧 / 尺寸取不到 / 层序认不出" 哪一种。字段必须齐、缺了要照报，
    取不到也不能炸（探针一报错整份报告就没了，12.1.5 已经吃过一次）。"""
    lua, gql, g, calls = hover_env(127)
    hover_update(lua)
    lua.execute("WorldMapFrame:SetFrameStrata('DIALOG') WorldMapFrame:SetSize(1440, 720) WorldMapFrame:Show()")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    okR, lines = lua.execute("local ok, r = pcall(GQL.Matrix.HoverDebug) return ok, r")
    if not okR:
        return fail("hover_debug_reports_why_a_window_is_not_a_candidate",
                    "HoverDebug 不许报错: %s" % lines)
    text = "\n".join(str(lines[i]) for i in range(1, int(lines.__len__()) + 1))
    for key in ("热区", "矩阵", "WorldMapFrame", "层=", "尺寸=", "盖住="):
        if key not in text:
            return fail("hover_debug_reports_why_a_window_is_not_a_candidate",
                        f"报告缺字段 {key}:\n{text}")
    if "盖住=是" not in text:
        return fail("hover_debug_reports_why_a_window_is_not_a_candidate",
                    "地图盖着热区时要报 盖住=是:\n" + text)
    # 没 hover 过时也要能跑（矩阵刚开、鼠标还没压到行）
    lua.execute("GQLMatrixHiR:Hide() GQLMatrixHiL:Hide()")
    okR2, _ = lua.execute("return pcall(GQL.Matrix.HoverDebug)")
    if not okR2:
        return fail("hover_debug_reports_why_a_window_is_not_a_candidate",
                    "没 hover 过时 HoverDebug 也要能跑")
    ok("hover_debug_reports_why_a_window_is_not_a_candidate")


def test_keyword_task_survives_map_filter():
    """关键词任务必须压过"本资料片地图"过滤：用户在设置里跟踪了自己的地图、
    关键词也真命中，行却还是被整组滤掉（用户实测"加了关键词没有对应任务"）。
    这里任务挂在旧资料片图（苏拉玛）、金额只有 50g，仍应出现在矩阵里。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    rows = lua.table_from({lua.table_from(
        {"questID": 123, "mapID": 619, "name": "符文石的低语", "money": 50000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    names = {619: "苏拉玛"}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": names.get(int(mid))})
        if int(mid) in names else None})
    gql.Config.AddKeyword("符文石")
    gql.Sync.SnapshotNow()
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "符文石的低语" not in joined:
        return fail("keyword_task_survives_map_filter",
                    f"关键词任务应无视地图过滤显示:\n{joined}")
    ok("keyword_task_survives_map_filter")


def test_keyword_hunt_registers_weekly_rows():
    """补扫跟踪地图要做两件事：① 行自己报的地图补写进跟踪清单；② 命中的任务注册成周常行。
    ②是用户新提的口径："我想添加其他类似的周常，比如虚空侵袭、亚基入侵，在哪里配置" ——
    这类任务天生不在世界任务池里，上一版补扫问的就是池（PoolForMapID），
    扫完只能"命中 0 条"，只 AddMap 等于什么都没加。现在问的是不带世界任务过滤的
    C_TaskQuest.GetQuestsOnMap 那份行数据，命中即落一条 regKey="kw:关键词" 的记录，
    之后每轮快照按账本里的 regKey 重问状态。玩家始终只输入任务名的一部分，不用知道 ID。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    # 只跟踪默认那张 2007；它挂回来的行自己报 mapID=619（区域任务列在父图上、
    # 行里的地图是子图，客户端真实形状）
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: (lua.table_from({
            lua.table_from({"questID": 124, "mapID": 619}),
        }) if int(mid) == 2007 else lua.table_from({})),
        "GetQuestInfoByQuestID": lambda qid: ("符文石的回响", 619) if int(qid) == 124 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "符文荒野"}) if int(mid) == 619 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 124,
    })
    gql.Config.AddKeyword("符文石")
    done, _ = gql.Sync.HuntKeywordMaps()
    if not done:
        return fail("keyword_hunt_registers_weekly_rows", "补扫应启动")
    if not lua.execute("return GQL.Config.DB().config.extraMaps[619] == true"):
        return fail("keyword_hunt_registers_weekly_rows", "命中的地图 619 应自动加进跟踪清单")
    if one(lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey or ''")) != "kw:符文石":
        return fail("keyword_hunt_registers_weekly_rows",
                    "命中的任务要注册成周常行（regKey=kw:关键词），否则矩阵永远没有它")
    if one(lua.execute("return GQLDB.quests[124].name or ''")) != "符文石的回响":
        return fail("keyword_hunt_registers_weekly_rows", "名字该由接口答出来，设置里不填名字")
    logs = str(gql.Config.GetLogs())
    if "补扫命中" not in logs or "注册 1 条周常" not in logs:
        return fail("keyword_hunt_registers_weekly_rows", f"日志要有命中与注册数:\n{logs}")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "符文石的回响" not in joined:
        return fail("keyword_hunt_registers_weekly_rows", f"补扫完矩阵就该有这一行:\n{joined}")
    # 之后每轮快照按账本重问：状态翻了行要跟着翻（不重问的话红勾会永远停在补扫那一刻）。
    # 这里用 questID 串判存在，不用帧文本：重绘只 Hide 掉多余的 FontString、不清 __text，
    # 旧行名会留在帧树里骗人（matrix_listed 就是为此存在的）
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQL.Ledger.RegisteredState(GQLDB, GQL.Sync.CurrentCharKey(), 124)")) != "open":
        return fail("keyword_hunt_registers_weekly_rows",
                    "注册行每轮快照都要重问状态，接口翻成 open 账本就得跟着翻")
    # 翻成"没人做过"也照样占一行：kw: 前缀的是玩家自己点名要的（虚空侵袭那种），
    # 全员空勾就是"这周还没人做"这个答案 —— 只有内置预设才享受"没人碰过就不列"
    if "124," not in matrix_listed(lua):
        return fail("keyword_hunt_registers_weekly_rows",
                    f"玩家点名注册的行整周常驻：{matrix_listed(lua)}")
    # 删关键词 = 退掉由它注册的行；内置预设那一行不许被牵连（key 不带 kw: 前缀）
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "  { { key = 'abundant-offerings', questID = 89507, name = '丰饶供品' } }, "
                "  GQL.Sync.CurrentCharKey(), 1700000000)")
    gql.Config.RemoveKeyword("符文石")
    if int(gql.Sync.UnregisterKeyword("符文石")) != 1:
        return fail("keyword_hunt_registers_weekly_rows", "删关键词要退掉它注册的行")
    if lua.execute("return GQLDB.quests[124].regKey ~= nil"):
        return fail("keyword_hunt_registers_weekly_rows", "退掉的行 regKey 要清空（记录留着，别重问一遍名字）")
    if lua.execute("return GQLDB.quests[89507].regKey == nil"):
        return fail("keyword_hunt_registers_weekly_rows", "内置预设那行不该被删关键词拆掉")
    ok("keyword_hunt_registers_weekly_rows")


def test_keyword_name_alone_does_not_admit_a_row():
    """名字撞关键词**单独**不再是矩阵准入门（12.1.5 第 32 轮实测：82157 特别任务：巨兽崛起 /
    86174 特别任务：风暴在即 两条上上世代的垃圾行，
    零客户端凭据（无池/无日志/无注册/lastMoney=nil/无图），全靠 kwHit 一张名字通行证
    赖在界面上，还挂着 `SetCycleExpiry` 给的"下次周重置"倒计时）。
    新口径：kwHit 只降级为"绕过金币门槛"的标记，关键词行还得答得出**至少一条**客户端
    凭据（poolSeen / kwLogAdded / regKey / lastMoney 是数 / 客户端自己答的 mapID /
    本轮任一角色被客户端答成已交 —— 那一条见下一条用例，96718 靠它留住）。
    对照组钉住"不许顺手把真行也砍了"：98172 追踪萨拉塔斯 68.32 金（低于 500 门槛），
    留它的理由是"客户端答过钱"（lastMoney 是数），不是"钱到了门槛"；93244 靠池凭据。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    gql.Config.AddKeyword("特别任务")
    gql.Config.AddKeyword("追踪萨拉塔斯")
    # 只有名字撞关键词、凭据全无 —— 垃圾行的原始形状（连地图都没有；
    # observations 是记录天生要在的字段，手搓记录必须给上，见 8388 那条同口径注释）
    lua.execute("GQLDB.quests[82157] = { name = '特别任务：巨兽崛起', currentSig = -1, "
                "  observations = {} }")
    if "82157," in matrix_listed(lua):
        return fail("keyword_name_alone_does_not_admit_a_row",
                    "只有名字命中关键词、零客户端凭据的记录不许再上矩阵（§2.1 的三条垃圾行）")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 82157)"):
        return fail("keyword_name_alone_does_not_admit_a_row",
                    "零凭据记录在 QuestHasCredential 里必须答 false")
    # 凭据一：读到过金币（哪怕 68.32 金 < 500 门槛）—— 客户端答过"这条值多少钱"
    lua.execute("GQLDB.quests[98172] = { name = '追踪萨拉塔斯', lastMoney = 683200 }")
    if "98172," not in matrix_listed(lua):
        return fail("keyword_name_alone_does_not_admit_a_row",
                    "lastMoney 是数（客户端答过钱）的关键词行必须留住：" + matrix_listed(lua))
    # 凭据二：真在世界任务池里出现过
    lua.execute("GQLDB.quests[93244] = { name = '特别任务：神盾密探', poolSeen = true }")
    if "93244," not in matrix_listed(lua):
        return fail("keyword_name_alone_does_not_admit_a_row",
                    "poolSeen 的关键词行必须留住：" + matrix_listed(lua))
    if not lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 93244)"):
        return fail("keyword_name_alone_does_not_admit_a_row",
                    "poolSeen 要算客户端凭据")


def test_player_dictated_map_is_not_an_admission_credential():
    """玩家口述的地图（mapManual / mapSrc=玩家指定）不算凭据：那是"在哪办"的备注，
    不是"这条任务今天还真存在"的证据（§2.2 实测：93013/94795/96307 一整批旧特别任务
    就是 mapManual + 名字撞关键词，已过期还挂在屏蔽管理里）。
    对照：同一张图、地图是客户端自己答的（无 mapManual）= 凭据，必须上板。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    gql.Config.AddKeyword("特别任务")
    lua.execute("GQLDB.quests[93013] = { name = '特别任务：击退圣光', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true }")
    if "93013," in matrix_listed(lua):
        return fail("player_dictated_map_is_not_an_admission_credential",
                    "mapID+mapManual（玩家口述）+名字命中，不该上矩阵（§2.2 的过期垃圾形状）")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 93013)"):
        return fail("player_dictated_map_is_not_an_admission_credential",
                    "QuestHasCredential 不许把玩家口述的地图算成凭据")
    # 对照组：客户端自己答的图才是凭据
    lua.execute("GQLDB.quests[94391] = { name = '特别任务：圣光对决', mapID = 2405 }")
    if "94391," not in matrix_listed(lua):
        return fail("player_dictated_map_is_not_an_admission_credential",
                    "客户端答得出地图的关键词行必须留住：" + matrix_listed(lua))


def test_a_quest_the_alts_finished_this_round_is_a_credential():
    """本轮任一角色被客户端答成"已完成"= 一条上板凭据（用户 2026-10-27 的反弹：
    "奈格塔尔虚空决战（英雄）这个不应该消失，这是我手动添加的关键词，正巧匹配到了我要的任务"）。
    12.1.5 真存档（96718）的形状：没有池、没有金币、没有客户端答的图、`kwLogAdded` 已被撤 ——
    它唯一的痕迹是 14 个号本轮的 done 快照（时刻全在本轮起点 1792824882 之后）。
    **交完 → 离开任务日志 → 下一轮日志那道凭据被"还挂在日志里才续期"的规则撤掉**，
    等于"做掉了"反而把上板凭据做没了，这是上一版准入门漏掉的一格。
    三条对照组钉住这条凭据不许变成万能通行证：
      * done 快照落在本轮起点之前（这条实例换新的了）→ 不算凭据；
      * 只有玩家手点的补录算 done（db.manual）→ 不算凭据，与"口述地图不算凭据"同一口径；
      * 全员 progress（82157 那种还挂在日志里没交的）→ 不算凭据。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    gql.Config.AddKeyword("虚空决战")
    lua.execute("GQLDB.quests[96718] = { name = '奈格塔尔虚空决战（英雄）', "
                "  currentSig = '-1', roundAt = 1000, observations = {} } "
                "GQLDB.chars = { "
                "  ['A-永恒之井'] = { views = { [96718] = { s = 'done', t = 1500 } } }, "
                "  ['B-永恒之井'] = { views = { [96718] = { s = 'progress', t = 1500 } } } }")
    if not lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 96718)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "本轮被客户端答成已交是凭据，QuestHasCredential 不许答 false")
    if "96718," not in matrix_listed(lua):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "14 个号本轮都交掉了的行不许消失：" + matrix_listed(lua))
    # 对照组一：轮次起点推到快照之后 = 这条实例换了新的，上一轮的勾不再说话
    lua.execute("GQLDB.quests[96718].roundAt = 2000")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 96718)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "done 快照早于本轮起点时不算凭据（换轮的垃圾行活不过一轮）")
    if "96718," in matrix_listed(lua):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "上一轮的 done 不许把行留下来")
    # 对照组二：玩家手点的补录（db.manual）不算客户端的答复
    lua.execute("GQLDB.quests[82157] = { name = '特别任务：巨兽崛起', roundAt = 1000, "
                "  observations = {} } "
                "GQLDB.manual = { ['A-永恒之井'] = { [82157] = { state = 'done', t = 1500 } } }")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 82157)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "手点补录不是客户端的答复，不许当凭据")
    # 对照组三：还挂在日志里没交（全源 progress，真存档里 82157/86174 的形状）
    lua.execute("GQLDB.manual = {} "
                "GQLDB.quests[86174] = { name = '特别任务：风暴在即', roundAt = 1000, "
                "  observations = {} } "
                "GQLDB.chars['A-永恒之井'].views[86174] = { s = 'progress', t = 1500 }")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 86174)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "只有 progress 快照不算凭据")
    if "86174," in matrix_listed(lua) or "82157," in matrix_listed(lua):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "零凭据的两条垃圾行仍然不许上板：" + matrix_listed(lua))


def test_keyword_hunt_scans_tracked_maps_only():
    """补扫只问"跟踪清单"里的图（默认=本资料片 ∪ 用户额外关注），不再遍历整棵地图树。
    用户口径："应该不需要全图扫描吧，我觉得只需要扫描用户添加的地图就好了，包括默认地图" ——
    全树一次要问上几百张图，而关键词任务所在的图本来就该由玩家加进跟踪清单
    （站在那张图上点"＋添加当前所在地图"，或按名称添加）。"""
    lua, gql, _ = sync_env([], weekly=432000)
    g = lua.globals()
    g.time = lambda: 1700000000
    asked = []
    # 地图树里多一张 999：没跟踪，但上面挂着名字对得上关键词的任务
    lua.execute("GQL.Api.WQMapList = function() return {2007, 619, 999} end")
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")

    def rows_on(mid):
        mid = int(mid)
        asked.append(mid)
        if mid == 999:
            return lua.table_from({lua.table_from({"questID": 125, "mapID": 999})})
        if mid == 2007:
            return lua.table_from({lua.table_from({"questID": 124, "mapID": 2007})})
        return lua.table_from({})

    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": rows_on,
        "GetQuestInfoByQuestID": lambda qid: ("符文石的回响" if int(qid) == 124 else "符文圣物"),
    })
    gql.Config.AddKeyword("符文石")
    gql.Config.AddMap(619)                    # 用户自己加的图也要扫到
    done, msg = gql.Sync.HuntKeywordMaps()
    if not done:
        return fail("keyword_hunt_scans_tracked_maps_only", "补扫应启动: " + str(msg))
    if sorted(set(asked)) != [619, 2007]:
        return fail("keyword_hunt_scans_tracked_maps_only",
                    "只该问跟踪清单（默认 2007 + 用户加的 619），实际问了 %s" % sorted(set(asked)))
    if not lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey == 'kw:符文石'"):
        return fail("keyword_hunt_scans_tracked_maps_only", "跟踪图上的命中要注册成周常行")
    if lua.execute("return GQLDB.quests[125] ~= nil"):
        return fail("keyword_hunt_scans_tracked_maps_only",
                    "没跟踪的图上的任务不该入账（否则又回到全图扫描的口径）")
    if "2 张图扫完" not in str(msg):
        return fail("keyword_hunt_scans_tracked_maps_only",
                    "点一次就该报出扫了几张图，不该只回「开始扫描」: " + str(msg))
    # 命中 0 时必须把"图没跟踪"这条原因说出来，否则玩家只会觉得关键词没生效
    gql.Config.RemoveKeyword("符文石")
    gql.Config.AddKeyword("圣物")
    done2, msg2 = gql.Sync.HuntKeywordMaps()
    if not done2 or "没有命中" not in str(msg2) or "跟踪" not in str(msg2):
        return fail("keyword_hunt_scans_tracked_maps_only",
                    "命中 0 要说明可能是图没跟踪: " + str(msg2))
    ok("keyword_hunt_scans_tracked_maps_only")


def hidden_texts_matching(node, needle):
    """替身帧树里"文本正是 needle、但自己没 Show"的 FontString 个数。
    frame_texts 只读 __text，看不见 Hide —— "复用了池子却忘了 Show"这类 bug
    恰好只在可见性上，非用这个查不可。"""
    n = 0
    stack = [node]
    while stack:
        f = stack.pop()
        if f is None:
            continue
        t = f.__text
        if t is not None and needle in str(t) and not bool(f.__shown):
            n += 1
        kids = f.__children
        if kids is not None:
            i = 1
            while kids[i] is not None:
                stack.append(kids[i])
                i += 1
    return n


def click_frame_by_text(lua, root_name, text):
    """在替身帧树里递归找 __text == text 且挂了 OnClick 的对象，点它（设置面板/新窗口的按钮
    埋在 scroll→content 底下，只翻一层的找不到；同 select_panel_keyword 那批辅助函数的道理）。"""
    return bool(lua.execute("""
    local function walk(f)
        local kids = f.__children or {}
        for i = 1, #kids do
            local c = kids[i]
            if c.__text == '%s' and c.__scripts and c.__scripts['OnClick'] then
                c.__scripts['OnClick'](c) return true
            end
            if walk(c) then return true end
        end
        return false
    end
    return walk(%s)
    """ % (text, root_name)))


def test_matrix_repaint_shows_reused_text_pool():
    """文本池"缩回去又长回来"时，上一轮被 Hide 掉的 FontString 复用时必须 Show 回来。
    12.1.5 实测：从"已忽略"里恢复一行之后，最底下那组任务的文字整片不见了，
    可鼠标停上去 hover 还在 —— PutL/Put 只 SetText+SetPoint，每轮末尾
    "Hide 掉多余条目"藏掉的正是下一轮要复用的那几条。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000), (103, 10000000)],
                           extra=("Matrix",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if hidden_texts_matching(g.GQLMatrixContentL, "任务103") != 0:
        return fail("matrix_repaint_shows_reused_text_pool", "第一次画出来就该是可见的")
    # 合并窗口（Matrix 的 COALESCE_SECONDS=0.25）之内 Refresh 只排队不画，而替身时钟
    # 默认一动不动 —— 不推进 GQL_NOW 的话这两次 Refresh 什么都没重排，下面读到的
    # 还是 Toggle 那一版的版面，用例就成了空跑。每次改完数据推进 1 秒，逼它同步画，
    # 并用 renderCount 当场证明"版面真的重排过"。
    def repaint(note):
        before = int(lua.execute("return GQL.Matrix.renderCount or 0"))
        g.GQL_NOW = float(g.GQL_NOW) + 1
        gql.Matrix.Refresh()
        if int(lua.execute("return GQL.Matrix.renderCount or 0")) == before:
            return fail("matrix_repaint_shows_reused_text_pool",
                        "Refresh 没重排版面（%s），后面断言读的是旧版面" % note)
        return None
    lua.execute("GQL.Ledger.RemoveQuest(GQL.Config.DB(), 103)")
    if repaint("少一行") is not None: return
    lua.execute("GQL.Config.DB().quests[103].ignored = nil")
    if repaint("长回来") is not None: return
    if "103," not in matrix_listed(lua):
        return fail("matrix_repaint_shows_reused_text_pool", "恢复后这行确实回到列表里了")
    n = hidden_texts_matching(g.GQLMatrixContentL, "任务103")
    if n != 0:
        return fail("matrix_repaint_shows_reused_text_pool",
                    "复用的文本条目没 Show：热区在、字没了（用户实测的恢复 bug）")
    ok("matrix_repaint_shows_reused_text_pool")


def test_family_anchor_lists_all_week_and_the_board_child_does_not():
    """用户 2026-10-24 定的模型（原话）："我鼠标悬停的丰饶 和 我真正要完成的 丰饶日常
    不是一个任务。你反查完成的丰饶为准"，板上那条"不显示它（它不是周常）"。
    落点：**一族一行**，行由内置表声明的那条 ID 顶着，而且整周常驻（它是每周 lockout，
    不跟着板子换轮出现/消失 —— SavedInstances 同一族也只有一条 type=single 的勾）。
    板上轮换出来的同名子任务只当完成证据，不占行。
    第十一轮的边界照旧成立（用户："永歌森林少了 符文石和特别任务，这两个都是周常任务，
    不应该到期了"）：玩家关键词注册的普通周常**不是轮换族**，整周常驻、不要板面证据。
    第十二轮那条"锚点没有板面证据就不许占行"的口径按本轮用户裁决**作废**（它换来的正是
    "丰饶这一行整周看不见"）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    lua.execute("GQLDB = {}")
    gql.Config.Init()

    def listed_ids():
        return one(lua.execute("local out = GQL.Ledger.MatrixRows(GQLDB, nil) local s = '' "
                               "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
                               "  s = s .. r.questID .. ',' end end return s"))

    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "{ { key = 'kw:虚空侵袭', questID = 89600, name = '虚空侵袭：先驱' } }, "
                "  '某人-永恒之井', 10)")
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "{ { key = 'kw:丰饶', questID = 89700, name = '丰饶：剥皮洞穴' } }, "
                "  '某人-永恒之井', 10)")
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "{ { key = 'abundant-offerings', questID = 89507, name = '丰饶供品' } }, "
                "  '某人-永恒之井', 10)")
    ids = listed_ids()
    if "89600," not in ids:
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "玩家关键词点名的普通周常不是轮换族：没有板面证据也整周常驻: " + ids)
    if "89507," not in ids:
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    f"族本体这轮不在图上也该占行（用户：矩阵里丰饶整周看不见）: {ids}")
    if "89700," in ids:
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    f"板上那条同名子任务不是周常，不许占行（「有 2 个丰饶」）: {ids}")
    lua.execute("GQL.Config.DB().quests[89700].onBoard = true")
    if "89700," in listed_ids():
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "在图上也只是完成证据，不是第二条行")
    # 第二十七轮把"点过一次添加"那条破例整个删了（`manualAdded` 字段下线）：一族一行
    # 没有例外。以前这条断言反过来（玩家右键点名要看的子任务才破例占第二行），
    # 那个豁免兜的正是"玩家标记这条要强制上板"——用户本轮明说那不是他的意图。
    lua.execute("GQL.Config.DB().quests[89700].manualAdded = true")
    if "89700," in listed_ids():
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "非锚点的族子任务带任何旧标记都不许占第二行（一族一行没有例外）")
    lua.execute("GQL.Config.DB().quests[89700].manualAdded = nil "
                "GQL.Config.DB().quests[89507].ignored = true")
    ids = listed_ids()
    if "89507," in ids:
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "玩家移除过的族本体不许因为「整周常驻」又刷回来: " + ids)
    if "89700," not in ids:
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    f"族本体被移除后，行让给同族另一条（整族不许凭空消失）: {ids}")
    # 族本体占行 ≠ 失去证据：账本里的记录照留，勾仍由全族来判（见家族用例）
    if lua.execute("return GQLDB.quests[89507] == nil"):
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "不列行只砍显示，账本里的锚点记录要留着当家族状态证据")
    # 连名字都问不出的族本体是一行"任务#7777"的裸 ID，玩家读不出任何信息 → 不占行
    declare_family(lua, "ghost", "本机问不出名字的那族", (7777,))
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "{ { key = 'ghost', questID = 7777 } }, '某人-永恒之井', 10)")
    if "7777," in listed_ids():
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "名字问不出时不占行（裸 ID 行不是可行动的信息）")
    lua.execute("GQL.Config.DB().quests[7777].name = '本机问不出名字的那族·第一'")
    if "7777," not in listed_ids():
        return fail("family_anchor_lists_all_week_and_the_board_child_does_not",
                    "问出名字就占行：整周常驻的是族本体")
    ok("family_anchor_lists_all_week_and_the_board_child_does_not")


def test_panel_reused_rows_are_shown():
    """新窗口右栏的行池同一个坑（旧弹框 add_dialog_reused_rows 的教训平移过来）：
    上一轮末尾 Hide 掉的条目，这轮复用时不 Show 就是一条既看不见也点不回来的
    "已忽略"记录 —— 屏蔽变成单向门（12.1.5 实测）。"""
    lua, gql, _ = sync_env([(190, 30000000), (191, 20000000), (192, 10000000)],
                           extra=("Matrix", "Options"))
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_reused_rows_are_shown", "左栏点不动\"任务\"这一行关键词")
    rows = panel_right_rows(lua)
    if [r["name"] for r in rows if r["shown"]] != ["任务190", "任务191", "任务192"]:
        return fail("panel_reused_rows_are_shown",
                    f"第一次打开就该有三条可见行: {rows}")
    # 命中集合先变短（池与账本都去掉两行）→ 池尾被 Hide
    lua.execute("GQLDB.quests[191] = nil GQLDB.quests[192] = nil")
    set_test_pool(lua, [(190, 30000000)])
    gql.Options.RefreshKeywordPanel()
    # 再变长 → 复用到池尾那两条
    set_test_pool(lua, [(190, 30000000), (191, 20000000), (192, 10000000)])
    gql.Options.RefreshKeywordPanel()
    stale = [r for r in panel_right_rows(lua) if r["name"] and not r["shown"]]
    if stale:
        return fail("panel_reused_rows_are_shown",
                    f"复用的行没 Show：看不见也点不回来（屏蔽成了单向门）: {stale}")
    ok("panel_reused_rows_are_shown")


def test_panel_unknown_money_row_shows_its_id():
    """右栏那一列整列换成 questID（用户 2026-10-27："最好加上questId，不用加金额"）：
    以前金额问不出显示"?"、读到显示"3000g"，现在两条都写自己的编号 —— 读不到金额这件事
    不再由这一列承担解释。
    `nil ≠ 0 金币` 那条实测教训一个字没作废（12.1.5 把 89347 那条 967 金的任务标成
    "非金币"，玩家据此以为插件判它不该上板），但它的**归属**改写成矩阵的金额列与悬停
    （Matrix.lua 那份本轮没动，由 matrix 那族用例继续钉）。这里只留还属于本窗口的
    那半句反向断言：界面上不许冒出"非金币"这种把"问不出"说成"没有"的字。"""
    lua, gql, _ = sync_env([(193, None), (194, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_unknown_money_row_shows_its_id", "左栏点不动关键词行")
    joined = "\n".join(frame_texts(g.GQLKeywordPanel))
    if "非金币" in joined:
        return fail("panel_unknown_money_row_shows_its_id",
                    f"问不出金额却断言非金币:\n{joined}")
    byq = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if byq.get(193, {}).get("id") != "193":
        return fail("panel_unknown_money_row_shows_its_id",
                    f"金额问不出那条，这一列写自己的编号: {byq}")
    if byq.get(194, {}).get("id") != "194":
        return fail("panel_unknown_money_row_shows_its_id",
                    f"读到金额那条也一样只写编号（金额整列撤下）: {byq}")
    ok("panel_unknown_money_row_shows_its_id")


def test_panel_lists_no_expired_or_uncredentialed_rows():
    """屏蔽管理右栏的口径 = 「矩阵可能列出的行」∪「你屏蔽过的行」：同名只留一条，
    已过期的、零客户端凭据的旧 ID 不进列表（用户 2026-10-27："好几个特别任务都是重复的，
    不光怨灵之怒"）。凭据与到期两道跟矩阵同一条口径；金币门槛**不算在这里** ——
    这一栏管的是屏蔽/找回，不是"哪条值得刷"，拿门槛筛会把玩家明确点名要屏蔽的行藏掉。
    形状照他 12.1.5 的真存档（计划 §2.2）：
      95918 特别任务：怨灵之怒 池凭据 + 未过期            ← 矩阵里那行，右栏也该只有它
      96307 特别任务：怨灵之怒 无凭据(玩家指定地图)+已过期  ← 上一轮留下的壳
      93013 / 94391 特别任务：击退圣光 两条都过期、都只有玩家口述的地图 ← 整批消失
    凭据那道判定读 T1 的唯一真源 Ledger.QuestHasCredential，这里不另拼第二套判据；
    到期那道与矩阵同一句（now >= rec.expiresAt）。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    g = lua.globals()
    now = 1760000000
    g.time = lambda: now              # 墙上时钟固定住，到期门才有确定的"现在"
    gql.Config.AddKeyword("特别任务")
    lua.execute("GQLDB.quests[95918] = { name = '特别任务：怨灵之怒', poolSeen = true, "
                "  mapID = 2405, expiresAt = %d }" % (now + 3600))
    lua.execute("GQLDB.quests[96307] = { name = '特别任务：怨灵之怒', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d }" % (now - 3600))
    lua.execute("GQLDB.quests[93013] = { name = '特别任务：击退圣光', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d }" % (now - 7200))
    lua.execute("GQLDB.quests[94391] = { name = '特别任务：击退圣光', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d }" % (now - 9000))
    rows = panel_match_rows(lua, "特别任务")
    if [r["id"] for r in rows] != ["95918"]:
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    f"同名两条只留带凭据的那条活的，过期旧 ID 不许再来一遍: {rows}")
    if any("击退圣光" in r["name"] for r in rows):
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    f"两条都已过期且零凭据的名字整批不进列表: {rows}")
    # 到期门对"带凭据的行"同样要砍：不砍的话同名重复只解决一半，
    # 矩阵那条到期判据（now >= rec.expiresAt）与右栏必须同一条口径。
    lua.execute("GQLDB.quests[93244] = { name = '特别任务：神盾密探', poolSeen = true, "
                "  lastMoney = 5000000, expiresAt = %d }" % (now - 60))
    if "93244" in [r["id"] for r in panel_match_rows(lua, "特别任务")]:
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    "到期门对带凭据的关键词行同样生效（与矩阵同一句口径）")
    ok("panel_lists_no_expired_or_uncredentialed_rows")


def test_panel_keeps_blocked_rows_without_evidence():
    """已屏蔽的行**无条件**留在右栏（计划 §三 T2.1 第一条）：这个窗口是唯一的取消屏蔽
    入口，屏蔽过的行要是也被"零凭据/已过期"过滤掉，玩家点错一次就再也找不回来 ——
    屏蔽变成单向门，正是第 30/31 轮实测过的那类事故（L.Prune 也留着 ignored 的行）。
    91193 的形状照他存档里那条自己屏蔽的行：无凭据 + 已过期 + ignored=true。
    对照组 99001：同样零凭据、同样已过期，只是没屏蔽过 → 必须**不**在列表里
    （没有这条对照，留口就把过滤本身测没了）。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    g = lua.globals()
    now = 1760000000
    g.time = lambda: now
    gql.Config.AddKeyword("特别任务")
    lua.execute("GQLDB.quests[91193] = { name = '特别任务解锁：顶点1 - 解锁', "
                "  ignored = true, expiresAt = %d }" % (now - 3600))
    lua.execute("GQLDB.quests[99001] = { name = '特别任务解锁：顶点2 - 解锁', "
                "  expiresAt = %d }" % (now - 3600))
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 91193)"):
        return fail("panel_keeps_blocked_rows_without_evidence",
                    "前提：这条确实零凭据（留着它靠的是 ignored，不是凭据）")
    rows = panel_match_rows(lua, "特别任务")
    ids = [r["id"] for r in rows]
    if "91193" not in ids:
        return fail("panel_keeps_blocked_rows_without_evidence",
                    f"屏蔽过的行哪怕过期/零凭据也必须列出来（那是找回入口）: {rows}")
    if "99001" in ids:
        return fail("panel_keeps_blocked_rows_without_evidence",
                    f"没屏蔽过的零凭据过期行仍要被过滤掉: {rows}")
    blocked = [r for r in rows if r["id"] == "91193"][0]
    if blocked["ignored"] != "true":
        return fail("panel_keeps_blocked_rows_without_evidence",
                    f"行上得带着 ignored，按钮才会写\"取消忽略\": {rows}")
    # 渲染层同步核对：这一行在右栏真看得见，按钮文字是"取消忽略"
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "特别任务"):
        return fail("panel_keeps_blocked_rows_without_evidence", "左栏点不动关键词行")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(91193, {}).get("btn") != "取消忽略":
        return fail("panel_keeps_blocked_rows_without_evidence",
                    f"屏蔽过的那行按钮该写\"取消忽略\": {vis}")
    ok("panel_keeps_blocked_rows_without_evidence")


def test_panel_shows_quest_id_not_money():
    """右栏那一列的实测形状（用户 2026-10-27："任务列表还是没加 questId 还是显示的
    ？或金币数量"）：每条可见行的最右列写 questID，整栏不许再冒出 "?" 或 "NNNg"；
    提示行（还没选关键词那一行）这一列空着 —— 它没有任务，也就没有编号。
    顺带钉"不留没人读的字段"：KeywordMatches 返回行里的 `money` 连同回填一起删了。"""
    lua, gql, _ = sync_env([(193, None), (194, 30000000)], extra=("Matrix", "Options"))
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()
    gql.Options.OpenKeywordPanel()
    hint = [r for r in panel_right_rows(lua) if r["shown"]]
    if not hint or hint[0]["name"] != "点左边一个关键词":
        return fail("panel_shows_quest_id_not_money", f"没选关键词时该只有提示行: {hint}")
    if hint[0]["id"] != "":
        return fail("panel_shows_quest_id_not_money",
                    f"提示行没有任务，编号那一列必须空着: {hint}")
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_shows_quest_id_not_money", "左栏点不动关键词行")
    rows = [r for r in panel_right_rows(lua) if r["shown"]]
    if [r["id"] for r in rows] != ["193", "194"]:
        return fail("panel_shows_quest_id_not_money", f"每行显示自己的 questID: {rows}")
    for r in rows:
        if r["id"] == "?" or r["id"].endswith("g"):
            return fail("panel_shows_quest_id_not_money",
                        f"这一列不许再是金额/问号（用户：不用加金额）: {rows}")
    money = [r["money"] for r in panel_match_rows(lua, "任务")]
    if money and any(m != "nil" for m in money):
        return fail("panel_shows_quest_id_not_money",
                    f"金额字段唯一的读者没了，整个删掉不留死字段: {money}")
    ok("panel_shows_quest_id_not_money")


def test_panel_lists_only_the_selected_keyword_matches():
    """右栏只列"选中的那个关键词"命中的任务：池里不含那个词的对照任务不许出现；
    换点另一个关键词，右栏整栏换掉（Hide 掉的行不许还看得见）——
    钉住两栏窗口的核心交互，顺带继续钉住"复用行池不 Show 就既看不见也点不着"
    那个老坑的另一半（切回多行时第 2、3 条必须回到可见）。"""
    lua, gql, _ = sync_env([(150, 30000000), (151, 20000000)],
                           extra=("Matrix", "Options"))
    gql.Config.AddKeyword("任务150")
    gql.Config.AddKeyword("任务151")
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务150"):
        return fail("panel_lists_only_the_selected_keyword_matches", "点不动\"任务150\"行")
    vis = [r["name"] for r in panel_right_rows(lua) if r["shown"]]
    if vis != ["任务150"]:
        return fail("panel_lists_only_the_selected_keyword_matches",
                    f"选中\"任务150\"后右栏只该有它: {vis}")
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_lists_only_the_selected_keyword_matches", "点不动\"任务\"行")
    vis = [r["name"] for r in panel_right_rows(lua) if r["shown"]]
    if vis != ["任务150", "任务151"]:
        return fail("panel_lists_only_the_selected_keyword_matches",
                    f"换关键词后右栏整栏换掉，复用的行必须 Show: {vis}")
    ok("panel_lists_only_the_selected_keyword_matches")


def test_panel_ignore_button_marks_permanently():
    """右栏点"忽略"= 走 Track.RemoveQuest 落永久屏蔽（与矩阵右键同一条路）：
    ignored=true、kwLogAdded 跟着清、矩阵该行当场消失、按钮文字当场变"取消忽略"
    （点完必须重刷面板，不许等玩家关窗重开才看到翻转）。"""
    lua, gql, _ = sync_env([(150, 30000000)], extra=("Matrix", "Track", "Options"))
    # 152 只挂在任务日志里（不在池里）——这才是 kwLogAdded 那种来源。
    # 池子不许空着：空池那轮快照直接早退（"30 秒后二次确认再判消失"），日志根本不收，
    # 所以放一条名字不撞关键词的对照任务（任务150 不含"任务152"）。
    g = lua.globals()
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 152, title = '任务152', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("任务152")
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if not lua.execute("return GQLDB.quests[152].kwLogAdded == true"):
        return fail("panel_ignore_button_marks_permanently",
                    "前提：这条是命中关键词、由日志收进来的（kwLogAdded）")
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务152"):
        return fail("panel_ignore_button_marks_permanently", "点不动关键词行")
    printed = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    if not click_panel_ignore_button(lua, 152):
        return fail("panel_ignore_button_marks_permanently",
                    "右栏那一行没有可点的\"忽略\"按钮（走真实 OnClick）")
    if not lua.execute("return GQLDB.quests[152].ignored == true"):
        return fail("panel_ignore_button_marks_permanently", "点\"忽略\"要当场落成永久屏蔽")
    if lua.execute("return GQLDB.quests[152].kwLogAdded ~= nil"):
        return fail("panel_ignore_button_marks_permanently",
                    "屏蔽后 kwLogAdded 要跟着清（与右键那一路同口径）")
    if "152," in matrix_listed(lua):
        return fail("panel_ignore_button_marks_permanently",
                    "屏蔽后矩阵该行要当场消失: " + matrix_listed(lua))
    if not any("已永久屏蔽" in p for p in printed):
        return fail("panel_ignore_button_marks_permanently",
                    f"聊天要报\"已永久屏蔽\"：{printed}")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(152, {}).get("btn") != "取消忽略":
        return fail("panel_ignore_button_marks_permanently",
                    f"按钮文字要当场变\"取消忽略\": {vis}")
    ok("panel_ignore_button_marks_permanently")


def test_panel_unignore_button_clears_the_mark():
    """右栏点"取消忽略"= 走 Track.AddQuest 撤销永久屏蔽：ignored 清掉、
    聊天报"已取消忽略"（不是"已添加"——玩家没做过"加"这个动作）；
    行回不回矩阵由五道自动判据说，这里关键词仍命中、金币也过门槛，必须回得来。"""
    lua, gql, _ = sync_env([(153, 30000000)], extra=("Matrix", "Track", "Options"))
    g = lua.globals()
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()
    lua.execute("GQL.Ledger.RemoveQuest(GQL.Config.DB(), 153)")
    gql.Matrix.Toggle()
    if "153," in matrix_listed(lua):
        return fail("panel_unignore_button_clears_the_mark", "前提：这一条已被屏蔽、不在矩阵")
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_unignore_button_clears_the_mark", "点不动关键词行")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(153, {}).get("btn") != "取消忽略":
        return fail("panel_unignore_button_clears_the_mark",
                    f"屏蔽过的那行按钮该是\"取消忽略\": {vis}")
    printed = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    if not click_panel_ignore_button(lua, 153):
        return fail("panel_unignore_button_clears_the_mark", "点不动\"取消忽略\"按钮")
    if one(lua.execute("return tostring(GQLDB.quests[153].ignored)")) != "nil":
        return fail("panel_unignore_button_clears_the_mark", "点\"取消忽略\"要撤销屏蔽")
    if not any("已取消忽略" in p for p in printed):
        return fail("panel_unignore_button_clears_the_mark",
                    f"别报成\"已添加\"（玩家没加过）：{printed}")
    if not any("在不在板上" in p or "关键词" in p for p in printed):
        return fail("panel_unignore_button_clears_the_mark",
                    f"撤销这句要把\"行会不会回来由谁判\"说清：{printed}")
    if "153," not in matrix_listed(lua):
        return fail("panel_unignore_button_clears_the_mark",
                    "关键词仍命中、金币过门槛，撤掉屏蔽后这行该回来: " + matrix_listed(lua))
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(153, {}).get("btn") != "忽略":
        return fail("panel_unignore_button_clears_the_mark",
                    f"撤销后按钮文字当场翻回\"忽略\": {vis}")
    ok("panel_unignore_button_clears_the_mark")


def test_panel_can_add_and_remove_keyword():
    """关键词还能继续加删（用户补充口径："设置里移除掉"是入口挪走，不是功能废了）。
    栏底输入框 + "添加"走 Config.AddKeyword；"补扫跟踪地图"走 Sync.HuntKeywordMaps；
    行尾"删"= Config.RemoveKeyword + Sync.UnregisterKeyword，由它注册的周常行跟着退列
    （regKey 清掉、记录留着）——口径与原先设置面板那一路逐句相同（入口搬家，判据没动）。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix", "Options"))
    g = lua.globals()
    g.time = lambda: 1700000000
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: (lua.table_from({
            lua.table_from({"questID": 124, "mapID": 619}),
        }) if int(mid) == 2007 else lua.table_from({})),
        "GetQuestInfoByQuestID": lambda qid: ("符文石的回响", 619) if int(qid) == 124 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "符文荒野"}) if int(mid) == 619 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Options.OpenKeywordPanel()
    if not lua.execute("GQLKeywordPanelKwEdit:SetText('符文石') return true"):
        return fail("panel_can_add_and_remove_keyword", "栏底没有关键词输入框")
    if not click_frame_by_text(lua, "GQLKeywordPanel", "添加"):
        return fail("panel_can_add_and_remove_keyword", "栏底没有\"添加\"按钮或没挂 OnClick")
    if not lua.execute("return GQL.Config.GetKeywords()[1] == '符文石'"):
        return fail("panel_can_add_and_remove_keyword", "点添加应写进配置（走真实 OnClick）")
    if not click_frame_by_text(lua, "GQLKeywordPanel", "补扫跟踪地图"):
        return fail("panel_can_add_and_remove_keyword", "栏底没有补扫按钮")
    if not lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey == 'kw:符文石'"):
        return fail("panel_can_add_and_remove_keyword", "补扫应把命中注册成周常行")
    if not lua.execute("""
        local found = false
        local function walk(f)
            local kids = f.__children or {}
            for i = 1, #kids do
                local c = kids[i]
                if c.__kw == '符文石' and c.__shown and c.__label
                    and c.__label.__text == '符文石  1' then
                    found = true return
                end
                walk(c)
            end
        end
        walk(GQLKeywordPanel)
        return found
    """):
        return fail("panel_can_add_and_remove_keyword",
                    "左栏该出现可见行\"符文石  1\"（补扫把命中数带上来了）")
    if not lua.execute("""
        local function walk(f)
            local kids = f.__children or {}
            for i = 1, #kids do
                local c = kids[i]
                if c.__kw == '符文石' and c.__shown and c.__btn
                    and c.__btn.__text == '删'
                    and c.__btn.__scripts and c.__btn.__scripts['OnClick'] then
                    c.__btn.__scripts['OnClick'](c.__btn) return true
                end
                if walk(c) then return true end
            end
            return false
        end
        return walk(GQLKeywordPanel)
    """):
        return fail("panel_can_add_and_remove_keyword", "行尾没有\"删\"按钮")
    if lua.execute("for _, k in ipairs(GQL.Config.GetKeywords()) do "
                   "if k == '符文石' then return true end end return false"):
        return fail("panel_can_add_and_remove_keyword", "点删该把关键词从配置里去掉")
    if lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey ~= nil"):
        return fail("panel_can_add_and_remove_keyword",
                    "删关键词要退掉由它注册的周常行（Sync.UnregisterKeyword 那一路）")
    if lua.execute("return GQLDB.quests[124] == nil"):
        return fail("panel_can_add_and_remove_keyword", "退注册只清 regKey，账本记录要留着")
    ok("panel_can_add_and_remove_keyword")


def test_panel_columns_and_footer_stay_inside_the_window():
    """两栏窗口的底部锚点全部要落在窗口**里面**（用户 2026-10-27 截图实测）。
    上一版四处写错：左栏滚动区的第二条锚点相对点写成 TOPLEFT（视口高度算出来是负的，
    客户端裁出来只剩几十像素，第 2 行关键词被腰斩），"补扫跟踪地图" y=-12、
    关键词输入框 y=-52 —— WoW 从**底边**锚定时 y 向上为正，那两处的负数把控件整排
    甩到窗口外面（=这一轮玩家根本没法加关键词，比"补扫溢出"更严重）。
    替身帧不根据锚点算高度，"掉到窗口外"只能读 __points 逐条断言 relativePoint 与 y 的符号。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    gql.Options.OpenKeywordPanel()
    why = lua.execute("""
        local f = GQLKeywordPanel
        if not f then return "窗口没建出来" end
        local function childByText(text)
            local kids = f.__children or {}
            for i = 1, #kids do
                if kids[i].__text == text then return kids[i] end
            end
        end
        local function pt(x, i) return x and x.__points and x.__points[i] end
        local function say(tag, p)
            if not p then return tag .. ": 没有锚点" end
            return string.format("%s: point=%s relativeTo=%s relativePoint=%s x=%s y=%s",
                tag, tostring(p[1]), (p[2] == f) and "窗口本体" or "别的对象",
                tostring(p[3]), tostring(p[4]), tostring(p[5]))
        end
        local function bottom(tag, p, wantRP)
            if not p then return say(tag, p) end
            if p[2] ~= f then return say(tag, p) .. " ← 该相对窗口本体锚定" end
            if p[3] ~= wantRP then
                return say(tag, p) .. " ← 底边该锚在窗口的 " .. wantRP .. " 上"
            end
            local y = tonumber(p[5])
            if not y or y <= 0 then
                return say(tag, p) .. " ← 从底边锚定 y 必须为正（向上量），负数=控件在窗口外面"
            end
        end
        local why = bottom("左栏滚动区底边", pt(GQLKeywordPanelScrollL, 2), "BOTTOMLEFT")
        if why then return why end
        why = bottom("右栏滚动区底边", pt(GQLKeywordPanelScrollR, 2), "BOTTOMRIGHT")
        if why then return why end
        local kw = GQLKeywordPanelKwEdit
        if not kw then return "栏底没有关键词输入框（GQLKeywordPanelKwEdit）" end
        local hunt = childByText("补扫跟踪地图")
        if not hunt then return "栏底找不到\\"补扫跟踪地图\\"按钮" end
        why = bottom("关键词输入框", pt(kw, 1), "BOTTOMLEFT")
        if why then return why end
        why = bottom("补扫按钮", pt(hunt, 1), "BOTTOMLEFT")
        if why then return why end
        local ky, hy = tonumber(kw.__points[1][5]), tonumber(hunt.__points[1][5])
        if ky <= hy then
            return string.format("输入框 y=%s 必须比补扫按钮 y=%s 高，两排固定控件不许叠在一起",
                                 tostring(ky), tostring(hy))
        end
    """)
    if why:
        return fail("panel_columns_and_footer_stay_inside_the_window", str(why))
    ok("panel_columns_and_footer_stay_inside_the_window")


def test_panel_footer_controls_are_not_under_a_viewport():
    """「添加」点不动、回车有效（用户 2026-10-27 第二条）：右栏视口 scrollR 的底边
    当初锚到 y=12，把页脚那条也占了，压住「添加」右边 38px（含"添加"两字的中心 x≈286）；
    scrollR 是 EnableMouse(true) 的窗口子帧，子树层级比浅一层的按钮高，点在文字上就是被视口吃掉。
    修法是把 scrollR 底边让到 FOOT_H(74)，与左栏 scrollL（Options.lua:527 早就是这个）同一条页脚带。

    替身帧不根据锚点算几何，所以这条只能**静态还原矩形**：读代码 SetSize 记进 `__w/__h` 的尺寸、
    读 SetPoint 记进 `__points` 的锚点，按窗口坐标系（f 的 BOTTOMLEFT=(0,0)）把五只控件的矩形算出来，
    断言三只页脚控件（添加/输入框/补扫）与两只视口矩形两两**不相交**。

    诚实标注：这条**不是**真命中测试 —— 真客户端 `Button:Click()` 绕过命中检测，离线没有任何办法
    证明鼠标真的落到了按钮上。它钉的是"别把页脚再还给视口"：一旦哪个视口的矩形重新盖住页脚控件，
    这条立刻红。真正的验收门是玩家在游戏里点一次"添加"（计划 T6 第 3 条）。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    gql.Options.OpenKeywordPanel()
    why = lua.execute("""
        local f = GQLKeywordPanel
        if not f then return "窗口没建出来" end
        local W, H = tonumber(f.__w), tonumber(f.__h)
        if not W or not H then return "窗口没记到 SetSize 的宽高，无法还原矩形" end

        local function childByText(text)
            local kids = f.__children or {}
            for i = 1, #kids do
                if kids[i].__text == text then return kids[i] end
            end
        end
        -- 相对点名 → 窗口坐标系里的那一点（f 的 BOTTOMLEFT = (0,0)，y 向上为正）
        local function refPoint(rp)
            if rp == "TOPLEFT" then return 0, H end
            if rp == "TOPRIGHT" then return W, H end
            if rp == "BOTTOMLEFT" then return 0, 0 end
            if rp == "BOTTOMRIGHT" then return W, 0 end
        end
        -- 一条锚点 p={point, relativeTo, relativePoint, x, y}：p[2]==f 时把自身锚点换算进窗口坐标
        local function anchorXY(p)
            local rx, ry = refPoint(p[3])
            if not rx then return nil end
            return rx + tonumber(p[4]), ry + tonumber(p[5])
        end
        -- 视口：TOPLEFT + BOTTOM* 两条对角锚点唯一确定矩形
        local function viewportRect(x)
            local p1 = x and x.__points and x.__points[1]
            local p2 = x and x.__points and x.__points[2]
            if not p1 or not p2 then return nil end
            if p1[2] ~= f or p2[2] ~= f then return nil end
            local tx, ty = anchorXY(p1)
            local bx, by = anchorXY(p2)
            if not tx or not bx then return nil end
            return { x1 = tx, y1 = by, x2 = bx, y2 = ty }
        end
        -- 底边左对齐 + 显式尺寸：BOTTOMLEFT 锚点 + SetSize → 矩形
        local function sizeRect(x)
            local p1 = x and x.__points and x.__points[1]
            if not p1 or p1[2] ~= f then return nil end
            local w, h = tonumber(x.__w), tonumber(x.__h)
            local ox, oy = anchorXY(p1)
            if not ox or not w or not h then return nil end
            return { x1 = ox, y1 = oy, x2 = ox + w, y2 = oy + h }
        end
        -- 「添加」相对 kwEdit.RIGHT 锚定：LEFT 边贴 kwEdit 右边界 + 偏移，竖直中心对齐 kwEdit
        local function addBtnRect(kwFrame, kwRect, btn)
            local p1 = btn and btn.__points and btn.__points[1]
            if not p1 or p1[2] ~= kwFrame then return nil end
            local w, h = tonumber(btn.__w), tonumber(btn.__h)
            if not w or not h then return nil end
            local cx = kwRect.x2 + tonumber(p1[4])
            local cy = (kwRect.y1 + kwRect.y2) / 2 + tonumber(p1[5])
            return { x1 = cx, y1 = cy - h / 2, x2 = cx + w, y2 = cy + h / 2 }
        end
        local function fmt(r)
            if not r then return "(缺失)" end
            return string.format("x %.0f..%.0f y %.0f..%.0f", r.x1, r.x2, r.y1, r.y2)
        end
        -- 严格不等：只共边（相等）不算相交
        local function overlaps(a, b)
            return a.x1 < b.x2 and b.x1 < a.x2 and a.y1 < b.y2 and b.y1 < a.y2
        end

        local scrollL = viewportRect(GQLKeywordPanelScrollL)
        local scrollR = viewportRect(GQLKeywordPanelScrollR)
        if not scrollL then return "左栏视口矩形还原不出来（锚点形状变了？）" end
        if not scrollR then return "右栏视口矩形还原不出来（锚点形状变了？）" end
        local kw = GQLKeywordPanelKwEdit
        if not kw then return "栏底没有关键词输入框（GQLKeywordPanelKwEdit）" end
        local kwRect = sizeRect(kw)
        if not kwRect then return "输入框矩形还原不出来（BOTTOMLEFT 锚点或 SetSize 缺记录）" end
        local addBtn = childByText("添加")
        if not addBtn then return "栏底找不到\\"添加\\"按钮" end
        local addRect = addBtnRect(kw, kwRect, addBtn)
        if not addRect then return "添加按钮矩形还原不出来（应 LEFT 贴 kwEdit.RIGHT）" end
        local hunt = childByText("补扫跟踪地图")
        if not hunt then return "栏底找不到\\"补扫跟踪地图\\"按钮" end
        local huntRect = sizeRect(hunt)
        if not huntRect then return "补扫按钮矩形还原不出来" end

        local checks = {
            { "添加按钮", addRect, "左栏视口", scrollL },
            { "添加按钮", addRect, "右栏视口", scrollR },
            { "输入框",   kwRect,  "左栏视口", scrollL },
            { "输入框",   kwRect,  "右栏视口", scrollR },
            { "补扫按钮", huntRect, "左栏视口", scrollL },
            { "补扫按钮", huntRect, "右栏视口", scrollR },
        }
        for i = 1, #checks do
            local c = checks[i]
            if overlaps(c[2], c[4]) then
                return string.format("%s 被 %s 压住：%s ∩ %s（页脚带不许属于任何视口）",
                    c[1], c[3], fmt(c[2]), fmt(c[4]))
            end
        end
    """)
    if why:
        return fail("panel_footer_controls_are_not_under_a_viewport", str(why))
    ok("panel_footer_controls_are_not_under_a_viewport")


def test_panel_columns_scroll_with_wheel_when_content_overflows():
    """内容比视口高时两栏的滚轮要真的滚得动（用户口径："多了要能滚动"）。
    诚实标注：这条**不是**本轮 bug 的门 —— 替身帧不根据锚点算高度，
    上一版那个"负高度视口"在这里读不出来（真正的 RED 门是上一条几何用例）。
    它钉的是修几何时别把 clamp 写坏：滚轮值 = 减 delta 再按 (内容高 - 视口高) 夹住，
    所以用例直接把视口人为压矮，逼出溢出。"""
    lua, gql, _ = sync_env([(301, 30000000), (302, 30000000), (303, 30000000)],
                           extra=("Matrix", "Options"))
    gql.Sync.SnapshotNow()
    gql.Config.AddKeyword("任务")            # 命中上面 3 条 = 右栏溢出
    for _, k in enumerate(("甲", "乙", "丙", "丁", "戊"), start=2):
        gql.Config.AddKeyword("关键词" + k)   # 凑够 6 行 = 左栏溢出
    gql.Options.OpenKeywordPanel()
    lua.execute("""
        GQLKeywordPanelScrollL:SetSize(250, 40)
        GQLKeywordPanelScrollR:SetSize(350, 40)
    """)
    gql.Options.RefreshKeywordPanel()

    def wheel(frame_name):
        return lua.execute("""
            local s = %s
            local w = s:GetScript('OnMouseWheel')
            if not w then return nil end
            w(s, -1)                      -- 真客户端往下滚给的是负 delta（见 Options 的 OnWheel 头注）
            return s:GetVerticalScroll()
        """ % frame_name)

    left = wheel("GQLKeywordPanelScrollL")
    if left is None:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    "左栏滚动区没挂 OnMouseWheel（滚轮在这栏上根本没接线）")
    if float(left) <= 0:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"左栏 6 行关键词、视口压到 40 像素，往下滚轮该滚动，实际 vscroll={left}")
    if not select_panel_keyword(lua, "任务"):
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    "点不动关键词行（右栏没内容可滚）")
    right = wheel("GQLKeywordPanelScrollR")
    if right is None:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    "右栏滚动区没挂 OnMouseWheel")
    if float(right) <= 0:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"右栏 3 条命中、视口压到 40 像素，往下滚轮该滚动，实际 vscroll={right}")
    ok("panel_columns_scroll_with_wheel_when_content_overflows")


def test_panel_shows_keyword_hit_count():
    """左栏每行关键词后面报命中数（设置面板那条用例的口径原样搬进新窗口）：
    0 就代表这个名字既不在跟踪地图的板上、也不在账本里，
    用户实测加了两个关键词看不到任务、分不清是关键词写错还是图没跟踪。"""
    lua, gql, _ = sync_env([(125, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    gql.Sync.SnapshotNow()                  # 任务125 进账本
    gql.Config.AddKeyword("任务125")
    gql.Config.AddKeyword("追踪萨拉塔斯")
    gql.Options.OpenKeywordPanel()
    left = panel_left_row_texts(lua)
    if "任务125  1" not in left:
        return fail("panel_shows_keyword_hit_count",
                    f"命中的关键词行尾应报 1，实际左栏: {left}")
    if "追踪萨拉塔斯  0" not in left:
        return fail("panel_shows_keyword_hit_count",
                    f"没命中的关键词行尾应报 0，实际左栏: {left}")
    if "补扫跟踪地图" not in "\n".join(frame_texts(g.GQLKeywordPanel)):
        return fail("panel_shows_keyword_hit_count",
                    "窗口应有补扫入口（只问跟踪清单）")
    if not select_panel_keyword(lua, "追踪萨拉塔斯"):
        return fail("panel_shows_keyword_hit_count", "点不动命中 0 的关键词行")
    vis = [r["name"] for r in panel_right_rows(lua) if r["shown"]]
    if vis != ["（没有命中：这个名字不在跟踪地图的板上，也不在账本里）"]:
        return fail("panel_shows_keyword_hit_count",
                    f"命中 0 时右栏就一行灰字，别的什么都不写: {vis}")
    ok("panel_shows_keyword_hit_count")


def test_options_gold_threshold_control_writes_config():
    """金币门槛这一段 UI 从设置面板整体撤下（用户 2026-10-27 口径四："三也去掉 暂时不能配置"）。
    反向钉住：面板上不许再出现门槛控件与那两句大段说明——数据与判据一个都不改：
    db.config.minGold 仍是唯一真源，Config.SetMinGold 的校验与矩阵的门槛过滤照旧生效
    （想改门槛只能改存档或等这轮 UI 加回来，代价写在计划 §六）。"""
    lua, gql, _ = sync_env([(140, 30000000), (141, 3000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    gql.Options.Open()
    joined = "\n".join(frame_texts(g.GQLOptionsFrame))
    for banned in ("金币门槛", "三、", "现在 = "):
        if banned in joined:
            return fail("options_gold_threshold_control_writes_config",
                        f"门槛那段（含\"{banned}\"）已从设置面板撤下，还在就说明控件没删干净:\n"
                        + joined)
    if "141," in matrix_listed(lua):
        return fail("options_gold_threshold_control_writes_config",
                    "默认 500 金门槛照常说了算（UI 撤下不等于门槛作废）")
    ok_set, val = gql.Config.SetMinGold("100")
    if not ok_set or int(val) != 100:
        return fail("options_gold_threshold_control_writes_config",
                    f"SetMinGold 是这张配置表唯一合法写点，应生效并回报新值: {ok_set} {val}")
    if "141," not in matrix_listed(lua):
        return fail("options_gold_threshold_control_writes_config",
                    "门槛改到 100 金后那条 300 金的任务要立刻进矩阵（判据走数据层）: "
                    + matrix_listed(lua))
    ok_bad, _ = gql.Config.SetMinGold("abc")
    if ok_bad or int(gql.Config.GetMinGold()) != 100:
        return fail("options_gold_threshold_control_writes_config",
                    "非数字要被 SetMinGold 挡掉、维持原值")
    ok("options_gold_threshold_control_writes_config")


def test_options_panel_has_no_login_sync_switch():
    """"登录时抓一次世界任务池"那颗勾从设置面板整块撤下（用户 2026-10-27 口径二：
    "登录抓取那个去掉，默认就抓取 不用设置"）。撤的是控件，不是数据：
    db.config.syncOnLogin / Config.SyncOnLogin / Config.SetSyncOnLogin /
    Sync.OnWorldEnter 读档那一句全留着（登录默认照抓，行为不变；
    数据层那两档语义由 test_login_snapshot_switch_off 继续钉）。
    反向断言 + 正面断言一起写：只撤那一段，别把整个面板一起弄没。"""
    lua, gql, _ = sync_env([(160, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    gql.Options.Open()
    joined = "\n".join(frame_texts(g.GQLOptionsFrame))
    if "登录" in joined:
        return fail("options_panel_has_no_login_sync_switch",
                    "设置面板不该再有\"登录时抓一次世界任务池\"那一段:\n" + joined)
    for want in ("跟踪地图", "按名称添加"):
        if want not in joined:
            return fail("options_panel_has_no_login_sync_switch",
                        f"面板正文只剩\"跟踪地图\"一段，\"{want}\"还得在:\n" + joined)
    ok("options_panel_has_no_login_sync_switch")


def track_env():
    """带 Track 的可控环境 + 一枚世界任务 pin（挂在 WorldMapFrame 子树里）。"""
    lua, gql, _ = sync_env([], extra=("Track",))
    g = lua.globals()
    lua.execute(
        "local p = CreateFrame('Button', 'GQLPinA', WorldMapFrame) "
        "p.questID = 90 return true")
    return lua, gql, g


def test_track_hook_registers_once():
    """Core 在每次 PLAYER_ENTERING_WORLD 都调 Track.Init（过图/飞行都会重发这个事件）。
    HookScript 是追加、CreateFrame 是新建：不守卫的话过图 N 次就有 N 套处理器，
    之后每次地图刷新要 N 遍全树递归 —— 越玩越卡。这里验 hook 只挂一次。"""
    lua, gql, g = track_env()
    lua.execute("WorldMapFrame:Show()")
    for _ in range(3):
        gql.Track.Init()
    if int(g.WorldMapFrame.__hooks["OnShow"]) != 1:
        return fail("track_hook_registers_once",
                    f"OnShow hook 应只挂一次，实际 {int(g.WorldMapFrame.__hooks['OnShow'])}")
    logs = str(gql.Config.GetLogs())
    if logs.count("地图 hook 已挂") != 1:
        return fail("track_hook_registers_once",
                    f"hook 挂载日志应只有一条（重复挂会有多条）:\n{logs}")
    ok("track_hook_registers_once")


def test_track_scan_skips_hidden_map():
    """pin 池只在地图显示时才有激活对象，而 WORLD_MAP_UPDATE 图关着也发
    （换区域、小地图刷新都触发）。不挡这一条，跑图路上每进一片新区域就空跑一遍
    几千帧的全树 —— 这就是"没开面板也一卡一卡"。"""
    lua, gql, g = track_env()
    lua.execute("WorldMapFrame:Hide()")
    gql.Track.Init()
    if g.GQLPinA.__hooks is not None:
        return fail("track_scan_skips_hidden_map", "地图没开时不该扫描挂 hook")
    lua.execute("WorldMapFrame:Show()")
    gql.Track.Init()
    if g.GQLPinA.__hooks is None:
        return fail("track_scan_skips_hidden_map", "地图开着应挂上右键 hook")
    ok("track_scan_skips_hidden_map")


def test_track_walk_prefers_child_iterator():
    """帧树遍历要走 EnumerateChildren（逐个给子帧）；走 GetChildren 会给每个节点
    建一张临时表。同时验没有该接口时仍能退回原路径（老客户端不许扫不到 pin）。"""
    lua, gql, g = track_env()
    lua.execute("WorldMapFrame:Show()")
    gql.Track.Init()
    if g.WorldMapFrame.__getChildrenCalls is not None:
        return fail("track_walk_prefers_child_iterator",
                    "有 EnumerateChildren 时不该再建 GetChildren 临时表")
    if int(g.WorldMapFrame.__enumCalls or 0) < 1:
        return fail("track_walk_prefers_child_iterator", "应走 EnumerateChildren 遍历")
    # 退回路径：拿掉 EnumerateChildren，新 pin 仍要钩上
    lua.execute("WorldMapFrame.EnumerateChildren = nil "
                "local p = CreateFrame('Button', 'GQLPinB', WorldMapFrame) "
                "p.questID = 91")
    gql.Track.Init()
    if g.GQLPinB.__hooks is None:
        return fail("track_walk_prefers_child_iterator",
                    "缺 EnumerateChildren 应退回 GetChildren 继续扫到 pin")
    ok("track_walk_prefers_child_iterator")


def test_child_frames_accepts_both_api_shapes():
    """子帧遍历同时收两种真实形态：EnumerateChildren 回迭代器函数（现客户端）
    与 GetChildren 回一张表（现客户端）/ 一个个返回值（老客户端）。
    上一版 Track 只认"带 .Next 的对象"，那条快路径在真客户端上永远走不到，
    兜底又只认多返回值形状 —— 于是整棵帧树扫不到 pin，Shift+点添加只剩 pinPools 活着。"""
    lua, gql, g = track_env()
    got = lua.execute("""
        local f = CreateFrame("Frame", "WQKShapeA", UIParent)
        for i = 1, 3 do CreateFrame("Button", "WQKShapeA" .. i, f) end
        -- 形态①：EnumerateChildren 回迭代器函数
        f.EnumerateChildren = function(self)
            local kids, i = self.__children or {}, 0
            return function() i = i + 1 return kids[i] end
        end
        local n = 0
        for _, c in ipairs(GQL.Api.ChildFrames(f) or {}) do n = n + 1 end
        -- 形态②：GetChildren 回一张表
        f.EnumerateChildren = nil
        f.GetChildren = function(self) return self.__children or {} end
        local m = 0
        for _, c in ipairs(GQL.Api.ChildFrames(f) or {}) do m = m + 1 end
        return n * 100 + m
    """)
    if int(got) != 303:
        return fail("child_frames_accepts_both_api_shapes",
                    f"两种形态都该拿到 3 个子帧，实际 n,m = {int(got) // 100},{int(got) % 100}")
    # 形态③：EnumerateChildren 回"带 Next 的对象"（本资料片没实测过这形态，
    # 但老替身造的就是它）—— 收着当退路，否则把替身换成迭代器后这条代码就没人验了
    got3 = lua.execute("""
        local f = CreateFrame("Frame", "WQKShapeB", UIParent)
        for i = 1, 3 do CreateFrame("Button", "WQKShapeB" .. i, f) end
        f.EnumerateChildren = function(self)
            local kids, i = self.__children or {}, 0
            return { Next = function() i = i + 1 return i, kids[i] end }
        end
        local p = 0
        for _, c in ipairs(GQL.Api.ChildFrames(f) or {}) do p = p + 1 end
        return p
    """)
    if int(got3) != 3:
        return fail("child_frames_accepts_both_api_shapes",
                    f"带 Next 的对象形态也该拿到 3 个子帧，实际 {int(got3)}")
    ok("child_frames_accepts_both_api_shapes")


def test_only_api_reads_data_globals():
    """数据接口只由 Api 读、存档只由 Config.DB() 取：绕开这两条就绕开了
    pcall 容错、候选名单与判空（Api.lua 直读 GQLDB、Config.lua 直调 GetQuestResetTime
    就是这两条纪律各漏的一处）。
    匹配前先剥掉注释与字符串字面量：Probe.lua 的报告行逐字印 GetQuestResetTime
    （那是回灌给用户的客户端原始答案，不是直调），按文件名放行 Probe 等于把
    纪律留个"下次也能过"的口子，只能把"提及"与"读"在形状上分干净。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "GoldQuestLedger")

    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    lua_comments_and_strings = re.compile(
        r"--\[(=*)\[[\s\S]*?\]\1\]"        # 长注释 --[==[ … ]==]
        r"|--[^\n]*"                        # 行注释
        r"|\[(=*)\[[\s\S]*?\]\2\]"          # 长字符串 [[ … ]] / [==[ … ]==]
        r"|\"(?:[^\"\\\n]|\\[\s\S])*\""     # 双引号串（含 \" 转义）
        r"|'(?:[^'\\\n]|\\[\s\S])*'"        # 单引号串
    )
    bad = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua"):
            continue
        src = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for i, line in enumerate(lua_comments_and_strings.sub(blank, src).splitlines(), start=1):
            if fn != "Config.lua" and re.search(r"\bGQLDB\b", line):
                bad.append(f"{fn}:{i} 直读 GQLDB（改用 GQL.Config.DB()）")
            if fn != "Api.lua" and re.search(r"\bGetQuestResetTime\b", line):
                bad.append(f"{fn}:{i} 直调数据接口（改用 GQL.Api.ResetTime()）")
    if bad:
        return fail("only_api_reads_data_globals", "；".join(bad))
    ok("only_api_reads_data_globals")


def hover_env(qid, extra=("Matrix",)):
    """矩阵开着、停在某一行上的环境：返回 tooltip 调用记录与那一行的热区。
    extra 默认只带 Matrix；新窗口的遮挡用例还要 Options（窗口本身就是被测对象）。"""
    lua, gql, _ = sync_env([(qid, 30000000)], extra=extra)
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid2: 7200})
    lua.execute("GQL.Api.WallClock = function() return 0 end")
    lua.execute("GQL.Api.MapName = function(id) return '虚影风暴' end")
    calls = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: calls.append(str(text)),
        # 标题行改成双列（左=任务名，右=灰字 questID），替身把两列拼成一行
        "AddDoubleLine": lambda self, l, r, *a: calls.append("%s %s" % (l, r)),
        "Show": lambda *a: calls.append("SHOW"),
        "Hide": lambda *a: calls.append("HIDE"),
    })
    gql.Matrix.Toggle()
    # 停在第一条整行热区上，并让客户端把焦点报在它身上（GQL_HOVERBTN 留着给
    # 后面的用例把焦点切回矩阵）
    lua.execute(
        "for i, c in ipairs(GQLMatrixContentL.__children) do "
        "  if c.__scripts and c.__scripts['OnMouseWheel'] then "
        "    c.__mouseover = true GQL_FOCUS = c GQL_HOVERBTN = c break end end")
    return lua, gql, g, calls


def hover_update(lua):
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")


def test_hover_same_row_does_not_rebuild_tooltip():
    """OnUpdate 每帧都跑：鼠标停在同一行时原来每帧重摆高亮 + 重建 GameTooltip，
    而提示里金币是 |T…|t 图标串，每帧重设文本等于每帧重建纹理 ——
    鼠标压在列表上就持续掉帧。修后同一行只在第一帧建一次。"""
    lua, gql, g, calls = hover_env(108)
    hover_update(lua)
    first = len(calls)
    if "SHOW" not in calls:
        return fail("hover_same_row_does_not_rebuild_tooltip",
                    f"停上行应弹提示:\n{chr(10).join(calls)}")
    for _ in range(5):
        hover_update(lua)
    if len(calls) != first:
        return fail("hover_same_row_does_not_rebuild_tooltip",
                    f"同一行不该再重建提示（{first} -> {len(calls)}）")
    ok("hover_same_row_does_not_rebuild_tooltip")


def test_hover_rebuilds_after_rerender():
    """重绘会把按钮池重新分配给别的行：hover 缓存必须作废，否则"同一行别重画"
    会让提示一直停在错的任务上（数据变了金币数也不更新）。"""
    lua, gql, g, calls = hover_env(109)
    hover_update(lua)
    first = len(calls)
    gql.Matrix.Render()
    hover_update(lua)
    if len(calls) <= first:
        return fail("hover_rebuilds_after_rerender",
                    "重绘后应重建一次提示（缓存没作废）")
    if "任务109" not in "\n".join(calls[first:]):
        return fail("hover_rebuilds_after_rerender", "重建的提示应是当前那一行的任务")
    ok("hover_rebuilds_after_rerender")


def test_hover_off_rows_leaves_tooltip_alone():
    """鼠标没压着任何行：每帧的 Hide 调用也要省掉（原来每帧两个 Hide +
    pcall(GameTooltip.Hide)，纯空转）。"""
    lua, gql, g, calls = hover_env(110)
    lua.execute("for i, c in ipairs(GQLMatrixContentL.__children) do "
                "  c.__mouseover = false end")
    hover_update(lua)
    first = len(calls)
    for _ in range(5):
        hover_update(lua)
    if len(calls) != first:
        return fail("hover_off_rows_leaves_tooltip_alone",
                    f"没压着行时不该每帧动提示（{first} -> {len(calls)}）")
    ok("hover_off_rows_leaves_tooltip_alone")


def test_escape_closes_matrix_and_options():
    """矩阵/设置面板要能用 ESC 关（用户要求）。口径来自 warcraft.wiki.gg
    "Make frames closable with the Escape key"：UISpecialFrames 存的是**全局帧名字符串**，
    没名字的帧挂不进去，所以两个窗口建帧时都写死了名字。挂名单还得幂等 ——
    反复开关不许往名单里塞重复项。"""
    lua, gql, _ = sync_env([(130, 30000000)], extra=("Matrix", "Options"))

    def listed(name):
        return bool(lua.execute(
            "for _, n in ipairs(UISpecialFrames or {}) do "
            "if n == '" + name + "' then return true end end return false"))

    gql.Matrix.Toggle()
    gql.Options.Open()
    for want in ("GQLMatrixFrame", "GQLOptionsFrame"):
        if not listed(want):
            return fail("escape_closes_matrix_and_options",
                        f"{want} 应挂进 UISpecialFrames")
    gql.Matrix.Toggle()
    gql.Matrix.Toggle()
    dup = lua.execute("local c = 0 for _, v in ipairs(UISpecialFrames) do "
                      "if v == 'GQLMatrixFrame' then c = c + 1 end end return c")
    if int(dup) != 1:
        return fail("escape_closes_matrix_and_options",
                    f"重复开关不该挂两份，实际 {int(dup)}")
    ok("escape_closes_matrix_and_options")


def test_hide_stops_pool_polling():
    """ESC 是暴雪直接 Hide 我们的帧，根本不经过 Toggle —— 停轮询必须挂在 OnHide。
    写在 Toggle 里的话，按 ESC 关掉矩阵后后台还在每次事件抓一遍池（用户实测的卡顿源）。"""
    lua, gql, _ = sync_env([(131, 30000000)], extra=("Matrix",))
    gql.Matrix.Toggle()
    if not gql.Sync.Polling():
        return fail("hide_stops_pool_polling", "矩阵打开应启动轮询")
    lua.execute("GQLMatrixFrame:Hide()")     # 模拟 ESC：只 Hide，不走 Toggle
    if gql.Sync.Polling():
        return fail("hide_stops_pool_polling", "ESC 关掉后后台还在抓池（停轮询没挂 OnHide）")
    ok("hide_stops_pool_polling")


def test_panel_button_text_follows_ignored_mark():
    """右栏按钮文字按 rec.ignored 二选一：屏蔽过 → "取消忽略"，其余 → "忽略"。
    旧弹框那套"矩阵已有/已加"标签体系随 Mx.listed 一起下线（2026-10-27 用户澄清：
    点这一行的意图是撤销忽略，不是标记要上板），新窗口里也不许再造一个等价标签。
    前置：132 金币过门槛自动在列；133 没被屏蔽；134 被屏蔽过。点 134 的"取消忽略"
    撤销后按钮当场翻回"忽略"，整块面板不许冒出"矩阵已有"或"已加"。"""
    name = "panel_button_text_follows_ignored_mark"
    lua, gql, _ = sync_env([(132, 30000000), (133, 1000), (134, 1000)],
                           extra=("Matrix", "Track", "Options"))
    g = lua.globals()
    gql.Config.AddKeyword("任务")
    gql.Sync.SnapshotNow()                   # 三条都进了账本
    lua.execute("GQL.Ledger.RemoveQuest(GQL.Config.DB(), 134)")
    gql.Matrix.Toggle()
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务"):
        return fail(name, "左栏点不动关键词行")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(134, {}).get("btn") != "取消忽略":
        return fail(name, f"被永久屏蔽的那条按钮该是\"取消忽略\"（点一下撤销）: {vis}")
    if vis.get(132, {}).get("btn") != "忽略":
        return fail(name, f"没屏蔽过的行按钮该是\"忽略\"（金币在不在矩阵都不标签）: {vis}")
    if vis.get(133, {}).get("btn") != "忽略":
        return fail(name, f"非金币也没被屏蔽的行同样只给\"忽略\": {vis}")
    joined = "\n".join(frame_texts(g.GQLKeywordPanel))
    if "矩阵已有" in joined or "已加" in joined or "已忽略" in joined:
        return fail(name, f"旧标签体系已随弹框下线，不许在新窗口里再造等价标签:\n{joined}")
    if not click_panel_ignore_button(lua, 134):
        return fail(name, "点不动\"取消忽略\"按钮")
    if one(lua.execute("return tostring(GQLDB.quests[134].ignored)")) != "nil":
        return fail(name, "点\"取消忽略\"要撤销屏蔽")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(134, {}).get("btn") != "忽略":
        return fail(name, f"撤销之后按钮要当场翻回\"忽略\": {vis}")
    ok(name)


def test_hover_stays_off_while_the_panel_is_open():
    """（旧弹框用例 hover_ignores_rows_while_add_dialog_open 的口径原样搬家，只是
    被测对象从"矩阵的子帧弹框"换成 UIParent 下的新窗口。）
    新窗口不再是主帧子帧：FocusOnMatrix 向上走撞不到 frame，天然判外部，
    那句"先撞到 dlgAdd 才算外部"的特判随之整块下线——本用例钉住行为不退步：
    鼠标几何仍压在矩阵行上、焦点落在新窗口里 → 高亮整条跳过；关掉后恢复。"""
    lua, gql, g, calls = hover_env(135, extra=("Matrix", "Options"))
    hover_update(lua)
    if "SHOW" not in calls:
        return fail("hover_stays_off_while_the_panel_is_open", "先在矩阵上亮一行作为前提")
    gql.Options.OpenKeywordPanel()
    lua.execute("GQL_FOCUS = GQLKeywordPanel")   # 鼠标落在新窗口上，行热区几何仍压着
    del calls[:]
    hover_update(lua)
    hover_update(lua)
    # 只许出现那一次"收掉上一帧残留提示"的 HIDE：焦点判外部之后 tooltip 撤一次就安静了。
    # 再有任何 SHOW / 文本行就是新窗口没挡住 hover（旧弹框用例里 OpenAddDialog 自己先收了一次，
    # 所以那边 calls 全空；窗口挪到 Options 后不再顺手收矩阵的提示，口径改成"不许再点亮"）。
    if any(c != "HIDE" for c in calls):
        return fail("hover_stays_off_while_the_panel_is_open",
                    f"新窗口开着时不该再点亮/弹提示：{chr(10).join(calls)}")
    if lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown"):
        return fail("hover_stays_off_while_the_panel_is_open", "整行高亮还亮着")
    lua.execute("GQLKeywordPanel:Hide() GQL_FOCUS = GQLMatrixFrame")
    del calls[:]
    hover_update(lua)
    if "SHOW" not in calls:
        return fail("hover_stays_off_while_the_panel_is_open", "关掉新窗口后 hover 应恢复")
    ok("hover_stays_off_while_the_panel_is_open")


def test_keyword_quest_pulled_from_quest_log():
    """关键词以前只在世界任务池里找，而城市日常/战役任务根本不在池里 —— 用户实测
    "追踪萨拉塔斯"就在任务栏第一个，设置里那条关键词的账本命中却是 0。
    现在已接任务日志是第二条来源：命中的建 kwLogAdded 记录、进矩阵、命中数变 1。
    它天生不在池里，所以消失判定必须放过它；名字没命中的（未接的"符文石"类）不许凭空入账。"""
    lua, gql, _ = sync_env([(136, 30000000)], extra=("Matrix", "Options"))
    g = lua.globals()
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9001, title = '追踪萨拉塔斯', "
                "        isHeader = false } end "
                "  if i == 2 then return { questID = 9002, title = '银月城日常', "
                "        isHeader = false } end "
                "  return nil end")
    gql.Config.AddKeyword("萨拉塔斯")
    res = gql.Sync.SnapshotNow()
    if int(res["logAdded"]) != 1:
        return fail("keyword_quest_pulled_from_quest_log",
                    f"日志应收 1 条，实际 {res['logAdded']}")
    if lua.execute("return GQLDB.quests[9002] ~= nil"):
        return fail("keyword_quest_pulled_from_quest_log", "没命中关键词的任务不该入账")
    state = lua.execute("local db = GQL.Config.DB() "
                        "return db.chars['小号一-永恒之井'].views[9001].s")
    if str(state) != "progress":
        return fail("keyword_quest_pulled_from_quest_log",
                    f"还接在日志里就绝不该被池子的消失判成 ended，实际 {state}")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "追踪萨拉塔斯" not in joined:
        return fail("keyword_quest_pulled_from_quest_log",
                    f"日志收的任务应出现在矩阵里:\n{joined}")
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "萨拉塔斯"):
        return fail("keyword_quest_pulled_from_quest_log", "新窗口左栏点不动关键词行")
    if "萨拉塔斯  1" not in panel_left_row_texts(lua):
        return fail("keyword_quest_pulled_from_quest_log",
                    "关键词面板左栏的命中数应跟着变成 1（旧设置面板那条读法搬家）")

    # 交掉/放弃后自然离开日志：标记撤掉，交回池子那套规则管（不许永久赖着）
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9002, title = '银月城日常', "
                "        isHeader = false } end return nil end")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[9001].kwLogAdded == true"):
        return fail("keyword_quest_pulled_from_quest_log",
                    "任务离开日志后 kwLogAdded 该撤掉，否则它永久绕过消失判定")
    ok("keyword_quest_pulled_from_quest_log")


def test_keyword_removal_drops_log_added_row():
    """删掉关键词后，上一轮由日志收进来的那行必须把 kwLogAdded 撤掉 ——
    任务还接在日志里，但已经没有关键词理由绕过 100g 门槛，
    不撤标记它会在矩阵里赖到 24 小时到期。"""
    lua, gql, _ = sync_env([(137, 5000)], extra=("Matrix",))
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9003, title = '符文石的低语', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("符文石")
    gql.Sync.SnapshotNow()
    if not lua.execute("return GQLDB.quests[9003].kwLogAdded == true"):
        return fail("keyword_removal_drops_log_added_row", "先确认这轮由日志收进来了")
    gql.Config.RemoveKeyword("符文石")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[9003].kwLogAdded == true"):
        return fail("keyword_removal_drops_log_added_row",
                    "关键词删掉后标记该撤，矩阵里不该继续留这一行")
    ok("keyword_removal_drops_log_added_row")


def test_probe_reports_button_texture_path_and_survives_bad_sections():
    """/gql probe 在 12.1.5 一行报告都没有，只回 "Matrix.lua:1476: bad argument #3 to
    'format' (string expected, got table)"：GetNormalTexture 返回的是**纹理对象**，
    旧代码把它直接塞进 %s，一处取数炸掉就把后面所有段的证据一起吞了
    （等于这一轮白测）。现在纹理要再问一次 :GetTexture() 拿路径；
    哪怕某一段整个报错，报告也必须继续往下走 —— 宁可写"出错"也不能没有。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Probe", "Matrix", mock=True)
    gql.Matrix.EnsureMapButton()
    info = str(gql.Matrix.ButtonInfo())
    if "INV_Misc_Coin_01" not in info:
        return fail("probe_reports_button_texture_path_and_survives_bad_sections",
                    f"按钮信息该报出纹理路径，实际 {info}")
    if "table:" in info:
        return fail("probe_reports_button_texture_path_and_survives_bad_sections",
                    f"纹理要取路径不是纹理对象，实际 {info}")
    report = str(gql.Probe.Run())
    if "INV_Misc_Coin_01" not in report:
        return fail("probe_reports_button_texture_path_and_survives_bad_sections",
                    f"报告里应看得到按钮纹理，实际:\n{report}")

    lua.execute("GQL.Matrix.ButtonInfo = function() error('模拟某段取数炸掉') end")
    report = str(gql.Probe.Run())
    if "ButtonInfo 出错" not in report:
        return fail("probe_reports_button_texture_path_and_survives_bad_sections",
                    f"取不到的段要如实记出错，不能吞掉:\n{report}")
    if "---- 其它" not in report:
        return fail("probe_reports_button_texture_path_and_survives_bad_sections",
                    "一段报错不许把后面所有段的证据一起吞掉")
    ok("probe_reports_button_texture_path_and_survives_bad_sections")


def test_pool_for_map_id_stamps_queried_map():
    """按图问出来的行必须记住"问的是哪张图"：12.1.5 的 GetQuestsOnMap 只给纯 questID，
    名字和地图单独补查也问不出时，旧代码留 nil → 关键词补扫捞到的任务整堆掉进
    矩阵底部"未知地图"分组（用户实测的那批噪音）。我们是逐张图问的，
    问的那张图就是它所在的图，可以直接兜底；补查到的更具体区域图仍优先。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({93001, 93002}),
        # 只有 93001 补查得到地图（更具体的区域图），93002 问不出 → 用询问的父图兜底
        "GetQuestZoneID": lambda qid: 2373 if int(qid) == 93001 else None,
        # 12.1.5 实测真 Usage 是 (questID, uiMapID) 两参、单参必抛错、只回 x,y。
        # 这里让它回一个看着像图号的第三个值：链里还留着它的话 93002 会拿到 1859 而不是兜底
        "GetQuestLocation": lambda qid: (None, None, 1859) if int(qid) == 93002 else None,
    })
    g.C_QuestLog = None
    pool = gql.Api.PoolForMapID(2050)
    if pool is None:
        return fail("pool_for_map_id_stamps_queried_map", "按图补扫应拿到数据")
    by_id = {}
    for i in range(1, len(pool) + 1):
        by_id[int(pool[i]["questID"])] = pool[i]
    if int(by_id[93001]["mapID"]) != 2373:
        return fail("pool_for_map_id_stamps_queried_map",
                    "补查到的区域图比询问用的父图更准，不许被兜底盖掉")
    if int(by_id[93002]["mapID"]) != 2050:
        return fail("pool_for_map_id_stamps_queried_map",
                    f"问不出的应用询问的 2050 兜底，实际 {by_id[93002]['mapID']}")
    # 三级兜底各自留名，否则下一份报告还是分不出"客户端跨图列任务"和"我们套错了图"
    if str(by_id[93001]["mapSrc"] or "") != "补问 C_TaskQuest.GetQuestZoneID":
        return fail("pool_for_map_id_stamps_queried_map",
                    f"补问来的地图要带上是哪个接口，实际 {by_id[93001]['mapSrc']}")
    if str(by_id[93002]["mapSrc"] or "") != "询问用的图兜底":
        return fail("pool_for_map_id_stamps_queried_map",
                    f"兜底套上去的要单独标出来，实际 {by_id[93002]['mapSrc']}")
    # 问的是哪张图也要跟着行走：行自带 2373、扫的是 2050，这个不一致是
    # "任务被列进别的图清单"的唯一线索，探针地图取证段靠它挑样本
    if int(by_id[93001]["askedMapID"]) != 2050:
        return fail("pool_for_map_id_stamps_queried_map", "池行要带上这次问的是哪张图")
    if by_id[93002]["name"] is not None:
        return fail("pool_for_map_id_stamps_queried_map", "名字问不出仍须留 nil，不许编")
    ok("pool_for_map_id_stamps_queried_map")


def test_log_source_skips_quest_already_mapped_by_name():
    """同一个世界任务会有两份 questID（池里带地图的那份 + 日志里的同名那份），
    日志源照单全收的话，用户看到的是上面各地图分组里有勾、底部又重复一整行 "?"
    （12.1.5 实测："特别任务：击退/神盾/怨灵"全是这样多出来的）。
    账本里已有同名带地图的记录时日志那份不再收；只在日志里的真关键词任务照常收。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    lua.execute("C_WorldQuests.GetWorldQuests = function() "
                "  local r = {} r[1] = { questID = 172, mapID = 2007, "
                "    name = '特别任务：神盾', money = 30000000 } return r end")
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9101, title = '特别任务：神盾', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("特别任务")
    res = gql.Sync.SnapshotNow()
    if int(res["logAdded"]) != 0:
        return fail("log_source_skips_quest_already_mapped_by_name",
                    f"同名且池里已有地图的任务不该再收一遍，实际收 {res['logAdded']}")
    if lua.execute("return GQLDB.quests[9101] ~= nil"):
        return fail("log_source_skips_quest_already_mapped_by_name",
                    "日志里同名的第二份 questID 不该进账本（矩阵底部那行 ? 就是它）")

    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9101, title = '特别任务：神盾', "
                "        isHeader = false } end "
                "  if i == 2 then return { questID = 9102, title = '追踪萨拉塔斯', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("萨拉塔斯")
    res = gql.Sync.SnapshotNow()
    if int(res["logAdded"]) != 1:
        return fail("log_source_skips_quest_already_mapped_by_name",
                    f"只在日志里的关键词任务仍要收进来，实际 {res['logAdded']}")
    if not lua.execute("return GQLDB.quests[9102].kwLogAdded == true"):
        return fail("log_source_skips_quest_already_mapped_by_name",
                    "日志收进来的任务要标 kwLogAdded（消失判定放过它）")
    ok("log_source_skips_quest_already_mapped_by_name")


def test_matrix_rows_drop_unmapped_same_name_duplicate():
    """上一版已经收进账本的同名重复行（没有地图信息）在矩阵里也不能出现两份：
    有地图的那条留下，没地图的那条是同一个任务的第二份 questID。
    老的手动标记也不再是豁免（2026-10-27 口径）：`manualAdded` 整个下线之后，
    同名去重只看证据强弱，玩家点没点过"添加"跟这一行该不该留没有关系 ——
    他点那一下只是撤销"别再显示"的意图，不是给自己发一张上板通行证。"""
    lua, gql, _ = sync_env([(173, 30000000)], extra=("Matrix",))
    gql.Sync.SnapshotNow()
    lua.execute("GQLDB.quests[174] = { name = '任务173', kwLogAdded = true }")

    def listed():
        return one(lua.execute(
            "local out = GQL.Ledger.MatrixRows(GQLDB, nil) local s = '' "
            "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
            "  s = s .. r.questID .. ',' end end return s"))

    ids = listed()
    if "173," not in ids:
        return fail("matrix_rows_drop_unmapped_same_name_duplicate",
                    f"带地图的同名行该保留，实际 {ids}")
    if "174," in ids:
        return fail("matrix_rows_drop_unmapped_same_name_duplicate",
                    f"底部重复的同名行（无地图）不该出现两次，实际 {ids}")
    # 反向断言：老的手动标记不许再破例（这一行是本轮下线的豁免权本身）
    lua.execute("GQLDB.quests[174].manualAdded = true")
    if "174," in listed():
        return fail("matrix_rows_drop_unmapped_same_name_duplicate",
                    "手动标记不再是同名去重的豁免，同名只留证据最强的那条")
    ok("matrix_rows_drop_unmapped_same_name_duplicate")


def test_snapshot_heals_missing_map():
    """矩阵把 mapID=nil 的记录全丢进底部"未知地图"分组。DiffPool 只给"这轮还在池里"
    的任务写地图，所以已接走/轮换出池的旧记录会永远停在 nil —— 12.1.5 实测底部那批
    "特别任务"（击退圣光/怨灵之怒/神盾密探）全是这个原因，而它们其实是问得出地图的
    世界任务。同步时逐条补问一次（Api 内部按会话缓存，问不出的留 nil 不许编）。"""
    lua, gql, _ = sync_env([(180, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        # GetQuestZoneID 是链里唯一答得出这条的入口；GetQuestLocation 留着当诱饵
        # （12.1.5 实测它单参调用必抛错、只回 x,y，第 3 个返回值从不存在 → 已从链里删）
        "GetQuestZoneID": lambda qid: 2395 if int(qid) == 181 else None,
        "GetQuestLocation": lambda qid: (None, None, 2395) if int(qid) == 181 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    lua.execute("GQLDB.quests[181] = { name = '特别任务：风暴在即' }")
    gql.Config.AddKeyword("特别任务")
    gql.Sync.SnapshotNow()
    if int(lua.execute("return GQLDB.quests[181].mapID")) != 2395:
        return fail("snapshot_heals_missing_map", "缺地图的旧记录该在同步时补问到地图")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "特别任务：风暴在即" not in joined:
        return fail("snapshot_heals_missing_map", f"补到地图后这一行仍要显示:\n{joined}")
    if "未知地图" in joined:
        return fail("snapshot_heals_missing_map", "补到地图的行不该再待在底部'未知地图'分组里")
    # 问不出地图的必须留 nil：编一个地图号会让整行归到错误的分组下
    lua.execute("GQLDB.quests[182] = { name = '特别任务：巨兽崛起' }")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[182].mapID ~= nil"):
        return fail("snapshot_heals_missing_map", "问不出地图不许编")
    ok("snapshot_heals_missing_map")


def test_questmapid_resolves_accepted_quest_via_log_info():
    """世界任务口径的接口对"已接的日常/战役任务"全问不出地图 —— 12.1.5 实测
    追踪萨拉塔斯 地图=nil，落进矩阵底部"未知地图"，可它明明在永歌森林>银月城。
    用户要的是"压根不该存在没有所属地图的任务"，那就得补日志口径的入口：
    C_QuestLog.GetQuestInfo（表里带 mapID）与 C_TaskQuest.GetQuestZoneID。
    两条都问不出时仍留 nil。上一版还挂着三条，实测全在撒谎（见 Api.lua 的
    QUEST_MAP_SOURCES 注释），这里给它们回"像地图号的值"确认链里真的不再取。"""
    lua, gql, _ = sync_env([])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        # 撒谎的三条：单参调用只回 x,y（这里回第三个值当诱饵）／ret2 回的不是地图号
        "GetQuestLocation": lambda qid: (None, None, 2444),
        "GetQuestInfoByQuestID": lambda qid: (None, 2696),
        "GetQuestZoneID": lambda qid: None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestCompleted": lambda qid: False,
        # 实测回的是"玩家脚下这张图"（2444=屠戮者高地），也是诱饵
        "GetMapForQuestPOIs": lambda qid: 2444,
        "GetQuestInfo": lambda qid: lua.table_from({"mapID": 2395}) if int(qid) == 98172 else None,
    })
    # QuestMapID 现在回 (地图号, 是哪条接口答的)，Python 侧拿到的是元组
    def mapid(qid):
        return int(lua.execute("return (GQL.Api.QuestMapID(%d)) or 0" % qid))
    if mapid(98172) != 2395:
        return fail("questmapid_resolves_accepted_quest_via_log_info",
                    "GetQuestInfo 表里的 mapID 该接上")
    # 只有 GetQuestZoneID 答得出的那条（换个 questID：QuestMapID 按会话缓存，同 id 问第二次没意义）
    g.C_QuestLog.GetQuestInfo = lambda qid: None
    g.C_TaskQuest.GetQuestZoneID = lambda qid: 2395
    if mapid(98173) != 2395:
        return fail("questmapid_resolves_accepted_quest_via_log_info",
                    "GetQuestZoneID 兜底也该接上")
    g.C_TaskQuest.GetQuestZoneID = lambda qid: None
    if gql.Api.QuestMapID(98174) is not None:
        return fail("questmapid_resolves_accepted_quest_via_log_info",
                    "全问不出不许编一个地图号（撒谎的三条也不许拿坐标/脚下图顶）")
    ok("questmapid_resolves_accepted_quest_via_log_info")


def test_accepted_keyword_quest_groups_under_its_map():
    """补问接口得真的把这行从底部"未知地图"搬进 银月城 分组才算解决问题（走完整登录同步
    + 矩阵渲染，而不是只验 Api 单个函数）：这里故意让地图只有日志口径的接口问得出。"""
    lua, gql, _ = sync_env([(193, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        "GetQuestLocation": lambda qid: None,
        "GetQuestInfoByQuestID": lambda qid: None,
        "GetQuestZoneID": lambda qid: 2395,
        "GetQuestsOnMap": lambda mid: None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestCompleted": lambda qid: False,
        "GetInfo": lambda i: lua.table_from({
            "questID": 98172, "title": "追踪萨拉塔斯", "isHeader": False,
        }) if int(i) == 1 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    gql.Config.AddKeyword("萨拉塔斯")
    gql.Sync.SnapshotNow()
    if int(lua.execute("return GQLDB.quests[98172].mapID or 0")) != 2395:
        return fail("accepted_keyword_quest_groups_under_its_map",
                    "日志收进来的那条建记录时就要带上所属地图")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "追踪萨拉塔斯" not in joined:
        return fail("accepted_keyword_quest_groups_under_its_map", f"这行仍要显示:\n{joined}")
    if "未知地图" in joined:
        return fail("accepted_keyword_quest_groups_under_its_map", "补到地图后不该还挂在'未知地图'里")
    if "银月城" not in joined:
        return fail("accepted_keyword_quest_groups_under_its_map",
                    f"要归进它真正的地图分组:\n{joined}")
    ok("accepted_keyword_quest_groups_under_its_map")


def test_snapshot_falls_back_to_reverse_map_index():
    """"压根不应该存在没有所属地图的任务"（12.1.5 用户原话）。逐条问地图的接口全是世界任务
    口径，已接任务一条都问不出（追踪萨拉塔斯 明明挂在银月城图上）。兜底不能靠猜接口：
    拿"池子已经在扫的那批地图"反过来问 C_QuestLog.GetQuestsOnMap(mapID) —— 这个入口
    在用户回传的报告里就是 AVAILABLE，行里自带 questID 与 mapID，只认行报的地图。"""
    lua, gql, _ = sync_env([(196, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        "GetQuestLocation": lambda qid: None,
        "GetQuestZoneID": lambda qid: None,
        "GetQuestInfoByQuestID": lambda qid: None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestCompleted": lambda qid: False,
        # 逐条问地图的这条路彻底问不出，只有反向索引能救
        "GetMapForQuestPOIs": lambda qid: None,
        "GetInfo": lambda i: lua.table_from({
            "questID": 98180, "title": "追踪萨拉塔斯", "isHeader": False,
        }) if int(i) == 1 else None,
        "GetQuestsOnMap": lambda mid: lua.table_from(
            # 行必须自带 mapID：反向索引不再拿"这次问的是哪张图"兜底
            {1: lua.table_from({"questID": 98180, "mapID": 2395})}) if int(mid) == 2395 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    gql.Config.AddMap(2395)
    gql.Config.AddKeyword("萨拉塔斯")
    gql.Sync.SnapshotNow()
    if int(lua.execute("return GQLDB.quests[98180].mapID or 0")) != 2395:
        return fail("snapshot_falls_back_to_reverse_map_index",
                    "逐条问不出地图时要用反向索引归位")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "未知地图" in joined:
        return fail("snapshot_falls_back_to_reverse_map_index",
                    f"兜底成功后底部不该再有'未知地图'分组:\n{joined}")
    if "银月城" not in joined or "追踪萨拉塔斯" not in joined:
        return fail("snapshot_falls_back_to_reverse_map_index",
                    f"要归进它真正所在的地图分组:\n{joined}")
    # 反向索引一张图都没问到 = 没证据，不许把 nil 判成"确认过没有地图"
    lua.execute("GQLDB.quests[98181] = { name = '追踪萨拉塔斯：下篇', kwLogAdded = true }")
    g.C_QuestLog.GetQuestsOnMap = lambda mid: None
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[98181].mapID ~= nil"):
        return fail("snapshot_falls_back_to_reverse_map_index", "问不出地图不许编")
    # 行自己不报地图时也不许兜底：上一版把"这次问的是哪张图"当答案，结果 12.1.5 实测
    # 一整批任务全被归进同一张图（用户截图里那批"盘卷蛇岛"）
    g.C_QuestLog.GetQuestsOnMap = lambda mid: lua.table_from(
        {1: lua.table_from({"questID": 98182})})
    lua.execute("GQLDB.quests[98182] = { name = '无名氏任务', kwLogAdded = true }")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[98182].mapID ~= nil"):
        return fail("snapshot_falls_back_to_reverse_map_index",
                    "反向索引只认行自带的 mapID，问的是哪张图不算证据")
    ok("snapshot_falls_back_to_reverse_map_index")


def test_probe_proves_every_map_candidate_with_map_name():
    """上一轮只报"地图=nil"，用户没法判是哪条接口该接。取证段要拿账本里真缺地图的
    questID，把生产候选名单（Api.MapSources = QuestMapID 问的同一份）逐个问一遍，
    数字返回值一律带上"这个号是哪张图"；MISSING 的也要打出来，证据才覆盖整套候选。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_TaskQuest = lua.table_from({
        "GetQuestLocation": lambda qid: None,
        "GetQuestInfoByQuestID": lambda qid: None,
        "GetQuestZoneID": lambda qid: 1859,
    })
    g.C_QuestLog = lua.table_from({
        "GetMapForQuestPOIs": lambda qid: 2395,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "银月城" if int(mid) == 2395 else "永歌森林"}) if int(mid) in (2395, 1859) else None,
    })
    gql.Config.Init()
    lua.execute("GQLDB.quests[98172] = { name = '追踪萨拉塔斯', kwLogAdded = true }")
    report = str(gql.Probe.Run())
    if "---- 地图取证" not in report:
        return fail("probe_proves_every_map_candidate_with_map_name", f"缺取证段:\n{report}")
    if "AVAILABLE C_QuestLog.GetMapForQuestPOIs(questID=98172) -> 2395(number) 地图名=银月城" not in report:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    f"候选返回值要带地图名:\n{report}")
    if "MISSING   C_QuestLog.GetQuestInfo" not in report:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    "候选名单里不存在的入口也要如实报，不能只挑问得出的")
    if "来源=日志" not in report:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    f"缺地图那条要写清来源:\n{report}")
    # 一条都不缺地图时也要继续取证：地图填满 ≠ 问对了（上一轮就是在这里断的线索，
    # 那句"账本里没有缺地图的记录"把"谁把任务答成盘卷蛇岛"彻底埋了）
    lua.execute("GQLDB.quests[98172].mapID = 2395")
    clean = str(gql.Probe.Run())
    if "账本里没有带地图的记录" in clean:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    "有地图的记录也要拿去问候选接口")
    if "questID=98172 名=追踪萨拉塔斯" not in clean:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    f"干净账本同样要出样本:\n{clean}")
    if "AVAILABLE C_QuestLog.GetMapForQuestPOIs(questID=98172) -> 2395(number) 地图名=银月城" not in clean:
        return fail("probe_proves_every_map_candidate_with_map_name",
                    "候选接口带着这条真 questID 重新问一遍，才能证实这一轮地图对不对")
    ok("probe_proves_every_map_candidate_with_map_name")


def test_matrix_lists_one_row_per_quest_name():
    """两份 questID 都补到地图时，上一版"只丢无地图那条"的去重直接失效 —— 同一个任务
    在两个分组里各出现一行（12.1.5 实测："好几个特别任务都跑到了盘卷蛇岛里…而且其他
    地图已经有了"）。现在按证据强弱只留一条：池里出现过的那份赢。"""
    lua, gql, _ = sync_env([(173, 30000000)], extra=("Matrix",))
    gql.Sync.SnapshotNow()
    # 同名第二份 questID，还被补问到了一个完全不同的地图上（用户截图里那一堆）
    lua.execute("GQLDB.quests[174] = { name = '任务173', mapID = 2373, kwLogAdded = true }")
    ids = matrix_listed(lua)
    if "174," in ids:
        return fail("matrix_lists_one_row_per_quest_name",
                    f"同名两份 questID 只该列一行，实际 {ids}")
    if "173," not in ids:
        return fail("matrix_lists_one_row_per_quest_name",
                    f"该留池里真出现过的那条，实际 {ids}")
    if int(lua.execute("return GQL.Ledger.MatrixRows(GQL.Config.DB(), nil)[1].rows[1].mapID")) != 2007:
        return fail("matrix_lists_one_row_per_quest_name",
                    "留下的那行必须还挂在池子给它的地图上，不是补问出来的那张")
    # 反向断言（2026-10-27）：老的手动标记不再是不参与竞争的豁免权 ——
    # 点"添加"只是撤销忽略，同名两份 questID 仍然只留证据最强的那条
    lua.execute("GQLDB.quests[174].manualAdded = true")
    if "174," in matrix_listed(lua):
        return fail("matrix_lists_one_row_per_quest_name",
                    "手动标记不该再破同名去重的例（豁免权随字段一起下线）")
    ok("matrix_lists_one_row_per_quest_name")


def test_guessed_expiry_never_shows_countdown_or_color():
    """日志/手动收进来的行没有"这条任务自己的倒计时"。两种兜底得分开说：
    ① 问得出下次周重置 → 那是服务端真数，可以显示，但话必须说成"距下次周重置"，
       不能说成"剩余"（用户会读成这任务本身只剩这么久）；周常活整个 CD，
       拿 24 小时顶着的后果就是这行隔天自己没了（12.1.5 实测：追踪萨拉塔斯是周常，
       判出来的日期却"马上就要结束"）。
    ② 周接口没了、只剩每日档 → 只能退回每日时刻，而且话必须改成"距下次每日重置"。
       12.1.5 用户实测：周五矩阵写着"距下次周重置 12 时 6 分"，那是明早 7 点的每日重置
       —— 玩家照它安排本周周常就全排错了。
    ③ 连下次重置都问不出 → 退回编的 24 小时排程值，标 guessed，
       名字本色、悬停不报剩余（上一版就是把假数涂成绿字报出来的）。"""
    lua, gql, _ = sync_env([(173, 30000000)], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9001, title = '追踪萨拉塔斯', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("萨拉塔斯")
    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQLDB.quests[9001].expiresSrc or ''")) != "下次周重置":
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    "问得出下次周重置时不该再编 24 小时")
    if lua.execute("return GQLDB.quests[9001].expiresGuessed == true"):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    "CD 结束时刻是服务端真数，不该标 guessed")

    gql.Matrix.Toggle()
    # 钉在"本 CD 还差 2 小时结束"上：悬停要报"距下次周重置"，不许说成"剩余"
    exp = int(lua.execute("return GQLDB.quests[9001].expiresAt"))
    lua.execute("GQL.Api.WallClock = function() return %d end" % (exp - 7200))
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "追踪萨拉塔斯" not in joined:
        return fail("guessed_expiry_never_shows_countdown_or_color", f"这行仍要显示:\n{joined}")

    calls = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None, "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: calls.append(str(text)),
        # 标题行改成双列（左=任务名，右=灰字 questID），替身把两列拼成一行
        "AddDoubleLine": lambda self, l, r, *a: calls.append("%s %s" % (l, r)),
        "Show": lambda *a: None, "Hide": lambda *a: None,
    })
    # 停在"这一条"上（不是池里那条）：热区按 questID 找，停错行等于什么都没验
    lua.execute("for i, c in ipairs(GQLMatrixContentL.__children) do "
                "  if c.__questID == 9001 and c.__scripts and c.__scripts['OnMouseWheel'] then "
                "    c.__mouseover = true GQL_FOCUS = c break end end")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if "追踪萨拉塔斯" not in "\n".join(calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"悬停提示本身要弹出来，否则这条断言是空的:\n{calls}")
    if not any("距下次周重置" in t for t in calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"CD 来源要说成「距下次周重置」:\n{calls}")
    if any(t.startswith("剩余") for t in calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"CD 结束时刻不许谎称成任务自己的剩余时间:\n{calls}")

    # ②：周接口没了、只剩每日档 → 来源与话都得跟着改成"每日"，不许再冒认本周的尾巴
    del calls[:]
    lua.execute("GQLDB.quests[9001] = { name = '追踪萨拉塔斯', kwLogAdded = true, "
                "  observations = {} }")
    lua.execute("GQL.Api.WeeklyResetCountdown = function() return nil end")
    lua.execute("GQL.Api.ResetTime = function() return 45607 end")
    gql.Sync.SnapshotNow()
    src = one(lua.execute("return GQLDB.quests[9001].expiresSrc or ''"))
    if src != "下次每日重置":
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"只有每日档时来源要写成「下次每日重置」，实际 {src}")
    if lua.execute("return GQLDB.quests[9001].expiresGuessed == true"):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    "每日重置也是服务端给的数，不该标 guessed")
    exp2 = int(lua.execute("return GQLDB.quests[9001].expiresAt"))
    lua.execute("GQL.Api.WallClock = function() return %d end" % (exp2 - 3600))
    gql.Matrix.Render()
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if not any("距下次每日重置" in t for t in calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"每日档要照实说「距下次每日重置」:\n{calls}")
    if any("距下次周重置" in t for t in calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"每日倒计时不许冒认周重置（12 时 6 分那次就是这么来的）:\n{calls}")

    # ③：下次重置彻底问不出 → 排程值照旧标 guessed、本色、不报倒计时
    del calls[:]
    lua.execute("GQLDB.quests[9001] = { name = '追踪萨拉塔斯', kwLogAdded = true, "
                "  observations = {} }")
    lua.execute("GQL.Api.ResetTime = function() return nil end GetQuestResetTime = nil")
    lua.execute("GQL.Api.WallClock = function() return %d end" % (exp2 + 10))
    gql.Sync.SnapshotNow()
    if not lua.execute("return GQLDB.quests[9001].expiresGuessed == true"):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    "编出来的到期时刻必须标 guessed，否则显示层分不清真假")
    if one(lua.execute("return GQLDB.quests[9001].expiresSrc or ''")) != "排程值":
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    "排程值要写明来源，dump 才知道这行是顶着的")
    del calls[:]
    gql.Matrix.Render()
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if any(("剩余" in t) or ("重置" in t) for t in calls):
        return fail("guessed_expiry_never_shows_countdown_or_color",
                    f"假到期不许报任何倒计时:\n{calls}")
    ok("guessed_expiry_never_shows_countdown_or_color")


def test_time_left_sources_require_quest_id():
    """倒计时名单里只准登记"收 questID 的入口"。上一版名单第三条是全局
    GetQuestLogTimeLeft —— 它压根没有 questID 参数，答的是"任务日志里当前选中那条
    限时任务还剩多久"，我们却把它写成某条周常的 expiresAt 并清了 expiresGuessed，
    于是矩阵给一条还有 5 天的周常涂上红/绿（12.1.5 实测：追踪萨拉塔斯"马上就要结束了"）。
    现在它对任何任务都问不出剩余时间。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.GetQuestLogTimeLeft = lambda: 900          # 无参、值还不小：正是那条冒名顶替的数
    g.C_TaskQuest = lua.table_from({})
    if gql.Api.QuestTimeLeft(11) is not None:
        return fail("time_left_sources_require_quest_id",
                    "没有 questID 参数的入口不许冒充某条任务的剩余时间")
    srcs = gql.Api.TimeLeftSources()
    if len(srcs) == 0:
        return fail("time_left_sources_require_quest_id", "取证名单应能取到（probe 用的同一份）")
    for i in range(1, len(srcs) + 1):
        s = srcs[i]
        if str(s["arg"]) != "questID":
            return fail("time_left_sources_require_quest_id",
                        f"名单里混进了不吃 questID 的入口：{s}")
    ok("time_left_sources_require_quest_id")


def test_next_cycle_end_follows_reset_form():
    """下次重置的时刻必须跟 cycleAnchor 用同一份形态判读（Config.ResetFormOf），
    并且要带出口径：weekly=周接口答的，daily=只有每日倒计时可用。
    倒计时形态搭在哪个时钟上都成立，纪元形态遇到开机域的 now 就不可比 —— 不可比时
    返回 nil 让调用方退回排程值，绝不能拿两个域的数相减编出一个"还剩几天"。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    lua.execute("GQL.Api.ResetTime = function() return 46509 end "
                "GQL.Api.Uptime = function() return 569772 end "
                "GQL.Api.WeeklyResetCountdown = function() return 432000, "
                "  'C_DateAndTime.GetSecondsUntilWeeklyReset' end")
    got, kind = lua.execute("return GQL.Config.NextCycleEnd(1700000000, 569772)")
    if got is None or int(got) != 1700000000 + 432000 or str(kind) != "weekly":
        return fail("next_cycle_end_follows_reset_form",
                    f"周接口在场时应优先用它算 now+周剩余秒，实际 {got} 口径 {kind}")
    # 周接口没了：只能退回每日档，kind 必须如实写 daily —— 显示层靠它决定说「周」还是「每日」
    lua.execute("GQL.Api.WeeklyResetCountdown = function() return nil end")
    got, kind = lua.execute("return GQL.Config.NextCycleEnd(1700000000, 569772)")
    if got is None or int(got) != 1700000000 + 46509 or str(kind) != "daily":
        return fail("next_cycle_end_follows_reset_form",
                    f"退回每日档要标 daily，实际 {got} 口径 {kind}")
    lua.execute("GQL.Api.ResetTime = function() return 1700060000 end "
                "GQL.Api.WeeklyResetCountdown = function() return 1700060000 end")
    # 域对不上：纪元重置值 vs 开机秒 now → 两个返回值都得拿（lupa 会把 (nil, kind) 返成元组，
    # 只判 `is not None` 会把"时刻没了但口径还在"错当成有结果）
    got, kind = lua.execute("return GQL.Config.NextCycleEnd(5000, 5000)")
    if got is not None:
        return fail("next_cycle_end_follows_reset_form",
                    f"纪元的重置时刻与开机域的 now 不可比，必须返回 nil，实际 {got} 口径 {kind}")
    got, kind = lua.execute("return GQL.Config.NextCycleEnd(1700000000, 5000)")
    if got is None or int(got) != 1700060000:
        return fail("next_cycle_end_follows_reset_form",
                    f"纪元形态配纪元 now 应直接取重置值，实际 {got}")
    ok("next_cycle_end_follows_reset_form")


def test_map_source_labels_which_answer_landed():
    """账本里那个地图号必须说得出是谁答的。12.1.5 第九轮回传写着"地图=2512 来源=池、
    mapSrc 全空"，于是"客户端把任务跨图列进 2512"和"我们拿问的那张图兜底套上去"
    两份嫌疑永远分不开，盘卷蛇岛的归组错案就这么悬着。
    现在 DiffPool 必须把池行带来的来源原样写进账本（上一版无条件清成 nil）。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    db = ledger_db(lua)
    pool = lua.table_from({})
    pool[1] = lua.table_from({"questID": 501, "mapID": 2393, "name": "行自带",
                              "mapSrc": "池行自带", "askedMapID": 2512})
    pool[2] = lua.table_from({"questID": 502, "mapID": 2373, "name": "补问来",
                              "mapSrc": "补问 C_TaskQuest.GetQuestLocation", "askedMapID": 2512})
    pool[3] = lua.table_from({"questID": 503, "mapID": 2512, "name": "兜底来",
                              "mapSrc": "询问用的图兜底", "askedMapID": 2512})
    gql.Ledger.DiffPool(db, pool, 1000, False)
    cases = {501: "池行自带", 502: "补问 ", 503: "询问用的图兜底"}
    for qid, want in cases.items():
        rec = db["quests"][qid]
        if rec is None:
            return fail("map_source_labels_which_answer_landed", f"{qid} 应入账")
        got = str(rec["mapSrc"] or "")
        if not got.startswith(want):
            return fail("map_source_labels_which_answer_landed",
                        f"{qid} 的地图来源应保留 {want}，实际 {got}")
    ok("map_source_labels_which_answer_landed")


def test_quest_evidence_sections_present():
    """/gql quest 的取证段：地图、倒计时、本 CD 结束时刻、周期判据候选四段都得在，
    用户回传一份文本就要能定"日期是谁答错的""地图是谁归错的"。"""
    lua, gql, _ = sync_env([(88, 30000000)], extra=("Probe",))
    lines = gql.Probe.QuestEvidence(88)
    text = "\n".join(str(lines[i]) for i in range(1, len(lines) + 1))
    for want in ("---- 本 CD 结束时刻", "---- 倒计时是谁答的",
                 "---- 地图是谁答的", "---- 周期判据候选"):
        if want not in text:
            return fail("quest_evidence_sections_present", f"缺段 {want}:\n{text}")
    if "GetQuestsOnMap" not in text:
        return fail("quest_evidence_sections_present",
                    f"应逐张跟踪图反查它出现在谁的清单里:\n{text}")
    ok("quest_evidence_sections_present")


def test_quest_evidence_reports_who_answered_the_money():
    """/gql quest 的"金额是谁答的"段。矩阵 100g 门槛只看一个数，可用户反馈③"松露大乱斗
    在池里却没进矩阵"有三种完全不同的成因（这台客户端没金额接口 / 奖励数据还没到本地 /
    这条真不给金币），只报 `上次金额=?` 一种都定不了。所以候选接口逐条 AVAILABLE/MISSING，
    再用 HaveQuestRewardData 把"0"分岔开。**MISSING 照打，不许拿 0 冒充"读过了、真没金币"**。"""
    lua, gql, _ = sync_env([(88, None)], extra=("Probe",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.HaveQuestRewardData = lambda qid: False
    g.C_TaskQuest = lua.table_from({"RequestPreloadRewardData": lambda qid: None})
    gql.Sync.SnapshotNow()
    lines = gql.Probe.QuestEvidence(88)
    text = "\n".join(str(lines[i]) for i in range(1, len(lines) + 1))
    if "---- 金额是谁答的" not in text:
        return fail("quest_evidence_reports_who_answered_the_money", f"缺金额取证段:\n{text}")
    sec = text.split("---- 金额是谁答的")[1].split("---- ")[0]
    for want in ("GetQuestLogRewardMoney", "C_QuestRewards.GetCachedQuestMoney",
                 "C_WorldQuestRewards.GetWorldQuestRewards"):
        if want not in sec:
            return fail("quest_evidence_reports_who_answered_the_money",
                        f"金额候选名单要和生产链同一份（缺 {want}）:\n{sec}")
    if "MISSING" not in sec:
        return fail("quest_evidence_reports_who_answered_the_money",
                    f"本机还没实测的候选要照打 MISSING:\n{sec}")
    if "HaveQuestRewardData=false" not in sec:
        return fail("quest_evidence_reports_who_answered_the_money",
                    f"要报出 0 是「数据还没到」还是「真不给金币」:\n{sec}")
    if "Api.QuestRewardMoney(88)=nil" not in sec:
        return fail("quest_evidence_reports_who_answered_the_money",
                    f"全链答不出时总入口写 nil，不许写 0:\n{sec}")
    if "金额一次都没读到过" not in sec:
        return fail("quest_evidence_reports_who_answered_the_money",
                    f"账本从没读到金额要点明（这条从来没靠金币进矩阵）:\n{sec}")
    ok("quest_evidence_reports_who_answered_the_money")


def test_questmapid_ignores_coordinates():
    """地图号必须按每条候选声明的位置取，取不到就留 nil —— 编一个地图号会把整行归到
    错误的分组下。小数、0、负数都不是地图（坐标就是这种形状）。12.1.5 实测后
    GetQuestLocation 已从链里摘掉（真 Usage 要两参、单参必抛错、只回 x,y），这里仍然
    让它回一个"第 3 个返回值像图号"的值当诱饵：链里还留着它的话 700 会拿到 2395。"""
    lua, gql, _ = sync_env([])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        "GetQuestZoneID": lambda qid: {
            700: 45.5,          # 只有坐标：小数不是地图号
            701: 2395,          # 声明位置上的地图号
            702: None,
            703: 0,             # 0 也不是
        }.get(int(qid)),
        "GetQuestLocation": lambda qid: (45.5, 62.25, 2395) if int(qid) == 700 else None,
    })
    if lua.execute("return (GQL.Api.QuestMapID(700)) ~= nil"):
        return fail("questmapid_ignores_coordinates", "横坐标/已摘掉的接口都不许给出地图号")
    if int(lua.execute("return (GQL.Api.QuestMapID(701)) or 0")) != 2395:
        return fail("questmapid_ignores_coordinates", "声明位置上的地图号要取到")
    if lua.execute("return (GQL.Api.QuestMapID(703)) ~= nil"):
        return fail("questmapid_ignores_coordinates", "0 不是地图号")
    if lua.execute("return (GQL.Api.QuestMapID(702)) ~= nil"):
        return fail("questmapid_ignores_coordinates", "问不出不许编一个地图号")
    # 第二个返回值顺带报出"是哪条接口答的"，归错组时才查得出该摘掉哪一条
    if one(lua.execute("return (select(2, GQL.Api.QuestMapID(701)))")) != "C_TaskQuest.GetQuestZoneID":
        return fail("questmapid_ignores_coordinates", "要连带报出给出地图号的接口名")
    ok("questmapid_ignores_coordinates")


def test_heal_skips_record_whose_name_already_has_map():
    """同名已经有一条带地图的记录 = 上面那个分组里已经有这个任务了，这一条是它的第二份
    questID。给它补地图纯属帮倒忙：补回来的分组是错的，还会让矩阵以为两条都有地图各留一行
    （12.1.5 实测"好几个特别任务都跑到了盘卷蛇岛里，而且其他地图已经有了"）。"""
    lua, gql, _ = sync_env([(180, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        "GetQuestZoneID": lambda qid: 2373 if int(qid) in (181, 182) else None,
    })
    lua.execute("GQLDB.quests[181] = { name = '任务180' }")           # 同名重复那份
    lua.execute("GQLDB.quests[182] = { name = '独一份任务' }")         # 账本里独一份
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[181].mapID ~= nil"):
        return fail("heal_skips_record_whose_name_already_has_map",
                    "同名已有带地图记录的那条不许再补地图，留 nil 去跟它比证据")
    if int(lua.execute("return GQLDB.quests[182].mapID or 0")) != 2373:
        return fail("heal_skips_record_whose_name_already_has_map",
                    "独一份名字的缺地图记录照旧要补（压根不该存在没有所属地图的任务）")
    ok("heal_skips_record_whose_name_already_has_map")


def test_probe_reports_which_interface_gave_each_healed_map():
    """自动补问会把缺地图的记录填满，下一份报告永远只剩"没有缺地图的记录"——
    归错组也查不出是谁答的（那批"盘卷蛇岛"就是这么丢的）。取证段要连"已经补到过地图"
    的记录一起报，带上地图名、给出答案的接口名，以及代码取的是第几个返回值。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    gql.Config.Init()
    lua.execute("GQLDB.quests[98172] = { name = '追踪萨拉塔斯', kwLogAdded = true, "
                "  mapID = 2395, mapSrc = 'C_QuestLog.GetMapForQuestPOIs' }")
    report = str(gql.Probe.Run())
    if "账本地图=2395(银月城)" not in report:
        return fail("probe_reports_which_interface_gave_each_healed_map",
                    f"补问出来的地图要连地图名一起报:\n{report}")
    if "补问自 C_QuestLog.GetMapForQuestPOIs" not in report:
        return fail("probe_reports_which_interface_gave_each_healed_map",
                    "要报出是哪条接口给的地图号，否则下一轮无从判断该摘掉哪一条")
    if "＜代码取第 1 个返回值＞" not in report:
        return fail("probe_reports_which_interface_gave_each_healed_map",
                    f"要写明代码取的位置，报告里的 4 个返回值才对得上:\n{report}")
    ok("probe_reports_which_interface_gave_each_healed_map")


def test_probe_samples_map_evidence_even_when_nothing_missing():
    """上一轮这段只挑"地图=nil 或带补问标记"的记录，回传就一句"账本里没有缺地图的记录"：
    自动补问早就把地图填满，池给的地图又不带标记，于是"谁把任务答成盘卷蛇岛"永远查不出来
    （等于这一轮白测）。现在不缺地图也要拿真任务把候选名单问一遍，
    并且优先挑"账本地图 ≠ 扫到它的那张图"的记录 —— 那正是归错组的症状。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"

    def map_info(mid):
        mid = int(mid)
        if mid == 2512:
            return lua.table_from({"name": "盘卷蛇岛", "parentMapID": 2395})
        if mid == 2395:
            return lua.table_from({"name": "永歌森林"})
        return None

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    # 池与任务日志各给一条，验这两段的地图号也带名字（上一轮通篇是裸号码）
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from([
        lua.table_from({"questID": 98172, "mapID": 2395,
                        "name": "追踪萨拉塔斯", "money": 300000})])})
    g.C_QuestLog = lua.table_from({"GetInfo": lambda i: lua.table_from(
        {"questID": 98172, "title": "追踪萨拉塔斯", "isHeader": False}) if int(i) == 1 else None})
    gql.Config.Init()
    gql.Config.AddKeyword("追踪萨拉塔斯")
    # 全都带地图、都没有 mapSrc：正是上一轮那句"账本里没有缺地图的记录"的场景
    lua.execute("GQLDB.quests[98172] = { name = '追踪萨拉塔斯', mapID = 2512, mapAsked = 2395 }")
    for qid in (98173, 98174, 98175, 98176):
        lua.execute("GQLDB.quests[%d] = { name = '丰饶%d', mapID = 2395 }" % (qid, qid - 98172))
    report = str(gql.Probe.Run())
    if "没有缺地图的记录" in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    "地图填满不等于问对了，不缺也要取证")
    if "账本地图=2512(盘卷蛇岛)" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"要报出地图名，光有号码判不出归组对不对:\n{report}")
    if "扫到它的图=2395(永歌森林)" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"「账本地图≠扫描图」这条不一致要一起报:\n{report}")
    if "父图=2395(永歌森林)" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    "父图链才是判「归组该信哪个号」的依据")
    if "＜代码只取表字段 mapID/uiMapID/zoneID＞" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"没缺地图时也要把候选接口逐个问一遍:\n{report}")
    # 已摘掉的三条也要继续问：只报生产那份的话，看不出"摘掉之后这条任务其实没人答得出"
    if "已停用" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"摘掉的候选要带原因一起报:\n{report}")
    # 只细查 3 条时，名额要给"最可能归错组"的那条，不是给账本里最靠前的几条
    if "questID=98172 名=" not in report or "questID=98176 名=" in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"样本排序应把不一致的那条排进前 3:\n{report}")
    if "在池=true 地图=2512(盘卷蛇岛)" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"任务日志命中行也要带地图名:\n{report}")
    if "mapID=2395(永歌森林)" not in report:
        return fail("probe_samples_map_evidence_even_when_nothing_missing",
                    f"池快照样本的地图号也要带名字:\n{report}")
    ok("probe_samples_map_evidence_even_when_nothing_missing")


def test_probe_shows_row_map_against_queried_map():
    """池行自带的 mapID 可以不同于我们扫的那张图（12.1.5 实测：问 2395 回来的行写 2393）。
    光有号码判不出这是"子区域的真任务"还是"别的图上的指示器 pin"（后者正是
    "任务凭空出现在盘卷蛇岛"的症状），所以查询图/行内图/行内父图要和
    childDepth、isMapIndicatorQuest 一起摊开，用户回传即可定论。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"

    def map_info(mid):
        mid = int(mid)
        if mid == 2395:
            return lua.table_from({"name": "永歌森林"})
        if mid == 2393:
            return lua.table_from({"name": "银月城", "parentMapID": 2395})
        return None

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": lambda mid: lua.table_from([
        lua.table_from({"questID": 95245, "mapID": 2393, "childDepth": 1,
                        "isMapIndicatorQuest": False, "inProgress": False})])})
    gql.Config.Init()
    lua.execute("GQLDB.quests[95245] = { name = '狩猎：被遗弃的营地', mapID = 2393 }")
    report = str(gql.Probe.Run())
    if "行内父图=2395(永歌森林)" not in report:
        return fail("probe_shows_row_map_against_queried_map",
                    f"行内地图与查询地图不同时要把父图链报出来:\n{report}")
    if "行#1 questID=95245 行内地图=2393(银月城) childDepth=1 只是指示器=false 进行中=false" not in report:
        return fail("probe_shows_row_map_against_queried_map",
                    f"逐行的 childDepth/指示器标记才是判据:\n{report}")
    ok("probe_shows_row_map_against_queried_map")


def test_pool_map_disagreement_is_kept_for_probe():
    """池行自带的 mapID 可以和我们扫的那张图不同（12.1.5 实测：问 2395 回来的行写 2393）。
    这个不一致必须留在账本里给探针当样本，否则下一份报告又是"没有缺地图的记录"。
    两张图本来就一致时不许留痕，免得 SavedVariables 白胖。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    arr = lua.table_from({})
    arr[1] = lua.table_from({"questID": 501, "mapID": 2512, "askedMapID": 2395,
                             "name": "任务501", "money": 300000})
    L.DiffPool(db, arr, 1000)
    if int(db["quests"][501]["mapAsked"]) != 2395:
        return fail("pool_map_disagreement_is_kept_for_probe",
                    "扫描用的地图号要记下来，探针才挑得出这条不一致的记录")
    arr2 = lua.table_from({})
    arr2[1] = lua.table_from({"questID": 501, "mapID": 2512, "askedMapID": 2512,
                              "name": "任务501", "money": 300000})
    L.DiffPool(db, arr2, 2000)
    if db["quests"][501]["mapAsked"] is not None:
        return fail("pool_map_disagreement_is_kept_for_probe",
                    "两张图一致时不该留 mapAsked，更要紧的是不一致的痕不能被一致那轮盖掉")
    ok("pool_map_disagreement_is_kept_for_probe")


def test_probe_reports_worldquest_and_money_per_keyword_hit():
    """底部那批"特别任务"里，哪些是"已经接了的世界任务"（该留）、哪些是杂项（该按金币
    门槛滤掉），只有"是不是世界任务 + 奖励金币"这两列分得开；上一轮只报了状态/地图，
    去留判不了。每条命中都带上这两列，取不到就写 nil，不许猜。"""
    lua, gql = load_lua("Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: lua.table_from({
            "questID": 9101, "title": "特别任务：神盾密探", "isHeader": False,
        }) if int(i) == 1 else None,
    })
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.GetQuestLogRewardMoney = lambda qid: 154800
    gql.Config.Init()
    gql.Config.AddKeyword("特别任务")
    report = str(gql.Probe.Run())
    if "世界任务=true" not in report or "奖励金币=154800" not in report:
        return fail("probe_reports_worldquest_and_money_per_keyword_hit",
                    f"命中行要带世界任务判别与奖励金币:\n{report}")
    g.QuestUtils_IsQuestWorldQuest = None
    g.GetQuestLogRewardMoney = lambda qid: 0
    # 换个 questID 再问：世界任务判别是会话内静态缓存（上一条的 true 该留着），
    # 用同一条验不出"接口缺失时如实报 nil"
    g.C_QuestLog.GetInfo = lambda i: lua.table_from({
        "questID": 9102, "title": "特别任务：神盾密探", "isHeader": False,
    }) if int(i) == 1 else None
    report = str(gql.Probe.Run())
    if "世界任务=nil 奖励金币=nil" not in report:
        return fail("probe_reports_worldquest_and_money_per_keyword_hit",
                    f"接口缺失/金额为 0 要如实报 nil:\n{report}")
    ok("probe_reports_worldquest_and_money_per_keyword_hit")


def declare_family(lua, key, match, ids=()):
    """把"会轮换搬区的一族"声明进内置表（生产里 `Api.WEEKLY_PRESETS` 是唯一声明处）。
    用例不许拿 regKey 当族身份：第十一轮实测就是栽在这儿 —— 把"带 regKey"都算家族，
    玩家关键词点名的普通周常（永歌森林的 符文石 / 特别任务）也被拖进"只认板面证据"，
    整批行凭空消失（用户："这两个都是周常任务，不应该到期了"）。"""
    lua.execute("table.insert(GQL.Api.WEEKLY_PRESETS, { key = '%s', ids = { %s }, match = '%s' })"
                % (key, ", ".join(str(i) for i in ids), match))


def matrix_listed(lua, now=None):
    """当前矩阵真列出的 questID 串（逗号分隔）。不用帧文本判存在与否：
    重绘只是 Hide 掉多余的 FontString、不清 __text，旧行名会留在树里骗人。
    now 传数字才能验"按当前时刻"的判据（注册行取在板上那个 ID 当行首）；
    默认 nil = 沿用老口径，不按到期剔行。"""
    return one(lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), ...) local s = '' "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  s = s .. r.questID .. ',' end end return s", now))


def matrix_row_hotzone_state(lua, qid):
    """左窗格整行热区按钮（RowHoverButton 建在 GQLMatrixContentL 下、带 __questID 的
    那颗 Button）的现状，回 (有OnClick, 注册过的点击串, EnableMouse, 有OnMouseWheel)。
    第 31 轮起矩阵不再提供任何屏蔽入口，这颗按钮只剩"悬停高亮 + 金币提示 + 滚轮透传"
    三件事，所以要一次读全：既钉"OnClick / RightButtonUp 都没了"，
    也钉"删 OnClick 没把 hover 和滚轮一起带走"（计划 A 节"必须留下的"三条）。
    找不到那一行回 None —— 行都不在，谈什么入口拆没拆。"""
    res = lua.execute("""
    for i, c in ipairs(GQLMatrixContentL.__children or {}) do
        if c.__questID == %d and c.__type == "Button" then
            local clicks = ''
            for j = 1, #(c.__clicks or {}) do
                clicks = clicks .. c.__clicks[j] .. ';'
            end
            return tostring(c.__scripts and c.__scripts['OnClick'] ~= nil),
                   clicks,
                   tostring(c.__mouse == true),
                   tostring(c.__scripts and c.__scripts['OnMouseWheel'] ~= nil)
        end
    end
    return nil
    """ % qid)
    if res is None or (isinstance(res, tuple) and res[0] is None):
        return None
    return res if isinstance(res, tuple) else (res,)


def click_matrix_row_right(lua, qid):
    """在左窗格那一行上模拟一次右键：走替身帧自己的 :Click("RightButton")。
    第 31 轮起热区按钮不再挂 OnClick，Click 找不到脚本就是"什么都没发生"——
    这正是那条用例要钉的行为，所以这里不许改成"先确认有 OnClick 再点"。"""
    return bool(lua.execute(
        "for i, c in ipairs(GQLMatrixContentL.__children or {}) do "
        "  if c.__questID == %d and c.__type == 'Button' then "
        "    c:Click('RightButton') return true end end "
        "return false" % qid))


def set_test_pool(lua, items):
    """换掉替身世界任务池：items = [(questID, money)]，money=None 表示金额问不出。
    新窗口"命中集合随池子变化"的用例要改池，只能重挂 C_WorldQuests.GetWorldQuests。"""
    g = lua.globals()
    rows = lua.table_from({})
    for i, (qid, gold) in enumerate(items, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if gold is not None:
            entry["money"] = gold
        rows[i] = lua.table_from(entry)
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})


def select_panel_keyword(lua, kw):
    """新窗口左栏点一行关键词 = 选中（走行按钮真实注册的 OnClick，不直调渲染函数）。
    行本体带 __kw 和 __label（行尾"删"子按钮带 __text 但没有 __kw），
    只认当前可见的行——池尾被 Hide 的旧行还留着 __kw，别点进死条目。
    行埋在 scroll→content 底下，和 click_frame_by_text 一样必须递归 walk（只翻一层找不到）。"""
    return bool(lua.execute("""
    local function walk(f)
        local kids = f.__children or {}
        for i = 1, #kids do
            local c = kids[i]
            if c.__kw == '%s' and c.__shown and c.__label
                and c.__scripts and c.__scripts['OnClick'] then
                c.__scripts['OnClick'](c)
                return true
            end
            if walk(c) then return true end
        end
        return false
    end
    return walk(GQLKeywordPanel)
    """ % kw))


def click_panel_ignore_button(lua, qid):
    """新窗口右栏点"忽略/取消忽略"（走按钮真实 OnClick）。行是 Frame，按钮挂在它下面；
    行可见才点——被 Hide 的池尾旧行还挂着 __questID，点错行等于测了个空。"""
    return bool(lua.execute("""
    local found = false
    local function walk(f)
        local kids = f.__children or {}
        for i = 1, #kids do
            local c = kids[i]
            if found then return end
            if c.__questID == %d and c.__shown and c.__btn
                and c.__btn.__scripts and c.__btn.__scripts['OnClick'] then
                c.__btn.__scripts['OnClick'](c.__btn)
                found = true
                return
            end
            walk(c)
        end
    end
    walk(GQLKeywordPanel)
    return found
    """ % qid))


def panel_right_rows(lua):
    """新窗口右栏行：[{'qid','name','id','btn','shown'}]，从 GQLKeywordPanelContentR
    的直接子帧收（行都建在 content 下面）。"没有命中/点左边"这类提示行 qid=None，一起收。
    shown 直接读 __shown：frame_texts 看不见 Hide，"复用行没 Show"这个坑恰好只在可见性上。
    id 读的是行上那列 **questID**（`r.__id`）：这一列第 32 轮前叫 `__gold`、写的是金额，
    用户口径"最好加上questId，不用加金额"之后整列换成编号，字段名跟着改口 ——
    一个叫 __gold 的字段装 ID 是谎，读它的用例也就跟着读 __id。"""
    res = lua.execute("""
    local out = {}
    local content = GQLKeywordPanelContentR
    if not content then return out end
    for i, c in ipairs(content.__children or {}) do
        if c.__label then
            out[#out + 1] = {
                qid = c.__questID,
                name = tostring(c.__label.__text or ""),
                id = tostring((c.__id and c.__id.__text) or ""),
                btn = tostring((c.__btn and c.__btn.__text) or ""),
                shown = c.__shown == true,
            }
        end
    end
    return out
    """)
    rows = []
    if res is None:
        return rows
    i = 1
    while res[i] is not None:
        r = res[i]
        rows.append({
            "qid": None if r["qid"] is None else int(r["qid"]),
            "name": str(r["name"]), "id": str(r["id"]),
            "btn": str(r["btn"]), "shown": bool(r["shown"]),
        })
        i += 1
    return rows


def panel_match_rows(lua, kw):
    """`Options.KeywordMatches(kw)` 这一轮真会列出的行：[{'id','name','ignored','money'}]
    （money 是**反向断言用的**：第 32 轮右栏不显示金额，这个字段连同回填一起删了，
    读到 "nil" 才算对）。读集合本身而不是帧文本：屏蔽/取消屏蔽之外，"这一行到底该不该
    出现在屏蔽管理里"就是这一个函数说了算，绕帧池会被 Hide 掉的复用旧行骗（同 matrix_listed）。"""
    res = lua.execute("""
    local out = {}
    for i, m in ipairs(GQL.Options.KeywordMatches(...) or {}) do
        out[#out + 1] = {
            id = tostring(m.questID), name = tostring(m.name),
            ignored = tostring(m.ignored), money = tostring(m.money),
        }
    end
    return out
    """, kw)
    rows = []
    if res is None:
        return rows
    i = 1
    while res[i] is not None:
        r = res[i]
        rows.append({"id": str(r["id"]), "name": str(r["name"]),
                     "ignored": str(r["ignored"]), "money": str(r["money"])})
        i += 1
    return rows


def panel_left_row_texts(lua):
    """新窗口左栏当前可见的行文字（"关键词  命中数"，存在行本体的 __label 里）。
    池尾被 Hide 的旧行不算可见——读的是玩家这一轮真看得见的版面。"""
    res = lua.execute("""
    local out = {}
    local content = GQLKeywordPanelContentL
    if not content then return out end
    for i, c in ipairs(content.__children or {}) do
        if c.__kw and c.__shown and c.__label then
            out[#out + 1] = tostring(c.__label.__text or "")
        end
    end
    return out
    """)
    texts = []
    if res is None:
        return texts
    i = 1
    while res[i] is not None:
        texts.append(str(res[i]))
        i += 1
    return texts


def test_matrix_right_click_blocks_nothing():
    """矩阵里没有任何屏蔽入口（用户 2026-10-27："我刚才不小心右键忽略掉了一行任务"
    → 点名"移除右键屏蔽功能"）。左窗格整行热区那颗按钮以前挂 OnClick +
    RegisterForClicks("LeftButtonUp","RightButtonUp")，右键当场永久屏蔽；本轮整块下线，
    而且不留"注册了右键但什么都不做"的半吊子：
    ① 行为门：热区按钮上没有 OnClick，也从没注册过 RightButtonUp；
    ② 数据门：在那一行上模拟一次右键（没有 OnClick 就是"什么都没发生"），
       ignored 仍是 nil、该行仍在矩阵里、聊天没有"已永久屏蔽"；
    ③ 确认框下线的旧口径继续钉：GQLRemoveQuestDialog 这个全局帧不许存在
       （旧读法 frame_texts(g.GQLRemoveQuestDialog) 的反面：g.GQLRemoveQuestDialog == nil）；
    ④ 删 OnClick 不许顺手把 hover/滚轮一起带走：EnableMouse 与 OnMouseWheel 必须还在
       （计划 A 节"必须留下的"三条；第三条 __questID 就是上面按它找到这一行的前提）。"""
    lua, gql, _ = sync_env([(190, 30000000), (191, 20000000)],
                           extra=("Matrix", "Track"))
    g = lua.globals()
    g.GQL_NOW = 5000
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if "190," not in matrix_listed(lua):
        return fail("matrix_right_click_blocks_nothing", "前提：这一行本来在矩阵里")
    if g.GQLRemoveQuestDialog is not None:
        return fail("matrix_right_click_blocks_nothing", "矩阵里不该有确认框全局")

    st = matrix_row_hotzone_state(lua, 190)
    if st is None:
        return fail("matrix_right_click_blocks_nothing",
                    "前提：矩阵里找不到这一行的整行热区按钮（__questID 丢了？）")
    if st[0] != "false":
        return fail("matrix_right_click_blocks_nothing",
                    "整行热区按钮不许再挂 OnClick —— 右键屏蔽入口本轮整体下线")
    if "RightButtonUp" in st[1]:
        return fail("matrix_right_click_blocks_nothing",
                    "整行热区按钮不许再注册 RightButtonUp（不留半个右键把手）: " + st[1])
    if st[2] != "true" or st[3] != "true":
        return fail("matrix_right_click_blocks_nothing",
                    "删 OnClick 别把鼠标热区/滚轮透传一起删了（整行高亮靠 IsMouseOver，"
                    "滚轮要能在行上滚）: EnableMouse=%s OnMouseWheel=%s" % (st[2], st[3]))

    printed = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    if not click_matrix_row_right(lua, 190):
        return fail("matrix_right_click_blocks_nothing", "找不到这一行的热区按钮，右键没落点")

    if lua.execute("return GQLDB.quests[190].ignored ~= nil"):
        return fail("matrix_right_click_blocks_nothing",
                    "右键再也屏蔽不了任何行：ignored 必须还是 nil，实际 "
                    + one(lua.execute("return tostring(GQLDB.quests[190].ignored)")))
    if "190," not in matrix_listed(lua):
        return fail("matrix_right_click_blocks_nothing",
                    "右键过的行要留在矩阵里（什么都不该发生）: " + matrix_listed(lua))
    if any("已永久屏蔽" in p for p in printed):
        return fail("matrix_right_click_blocks_nothing",
                    f"右键不该报\"已永久屏蔽\"：{printed}")
    if g.GQLRemoveQuestDialog is not None:
        return fail("matrix_right_click_blocks_nothing",
                    "确认框已下线，右键不该建出 GQLRemoveQuestDialog 这个全局帧")
    ok("matrix_right_click_blocks_nothing")


def test_panel_block_survives_resync_and_is_reversible():
    """12.1.5 实测反馈："我现在删不掉，右键点击后还会刷出来。"
    RemoveQuest 以前真删记录，可池子/任务日志每轮同步都会把同一条重新建出来 —— 删了等于没删。
    改成持久意图 rec.ignored（**永久**：换 CD、换轮都不作废，见 an_ignore_outlives_every_cycle_and_round；
    用户 2026-10-27 的原话"我这个意图其实是想永久屏蔽的"，上一版做成"只管本 CD"是误读）：
    矩阵不列、日志源不再收、按时长清理也不动它，
    但记录本身留着（金币账不能因为藏了一行就凭空少一笔），恢复走"添加任务"打开的
    关键词面板里那一行"取消忽略"。
    屏蔽那一步第 31 轮换过入口：不再右键矩阵行，改点「关键词 · 屏蔽管理」右栏的"忽略"
    （Config.AddKeyword → OpenKeywordPanel → 选关键词 → 点按钮，全程真实 OnClick，
    不直调 Track.RemoveQuest）；四条数据口径一条没动。"""
    lua, gql, _ = sync_env([(190, 30000000), (191, 20000000), (193, 1000)],
                           extra=("Matrix", "Track", "Options"))
    g = lua.globals()
    g.GQL_NOW = 5000
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if "190," not in matrix_listed(lua):
        return fail("panel_block_survives_resync_and_is_reversible", "前提：这一行本来在矩阵里")

    # 屏蔽这一步走面板：先要有命中它的关键词（右栏只列命中已有关键词的任务）。
    # 关键词写成"任务190"而不是"任务"：下面那条 193 的判据要求它**不**命中关键词，
    # 宽关键词会让 193 靠关键词门槛直接进矩阵，把那一半口径测没了。
    gql.Config.AddKeyword("任务190")
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "任务190"):
        return fail("panel_block_survives_resync_and_is_reversible", "面板左栏点不动关键词行")
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(190, {}).get("btn") != "忽略":
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"没屏蔽过的行右栏按钮该是\"忽略\": {vis}")
    if not click_panel_ignore_button(lua, 190):
        return fail("panel_block_survives_resync_and_is_reversible",
                    "右栏那一行点不动\"忽略\"（走真实 OnClick）")

    ids = matrix_listed(lua)
    if "190," in ids or "191," not in ids:
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"点\"忽略\"当场屏蔽：该行要消失、别的行不许跟着没:\n{ids}")
    if lua.execute("return GQLDB.quests[190] == nil"):
        return fail("panel_block_survives_resync_and_is_reversible", "只藏行，账本记录（金币历史）不许删")
    if one(lua.execute("return tostring(GQLDB.quests[190].ignored)")) != "true":
        return fail("panel_block_survives_resync_and_is_reversible",
                    "屏蔽要落成永久意图标记（true，不是时间戳 —— 留个数字会被读成会过期），实际 "
                    + one(lua.execute("return tostring(GQLDB.quests[190].ignored)")))
    if lua.execute("return GQLDB.quests[190].kwLogAdded ~= nil"):
        return fail("panel_block_survives_resync_and_is_reversible",
                    "屏蔽后\"日志收的\"不再是事实，留着恢复时会被误认成本来就在列")

    # 下一次同步：池子还会把 190 端回来，这正是用户看见"刷出来"的地方
    gql.Sync.SnapshotNow()
    ids = matrix_listed(lua)
    if "190," in ids:
        return fail("panel_block_survives_resync_and_is_reversible", f"同步后移除过的行不该再刷出来:\n{ids}")

    # 移除成了单向门也不行：能在关键词面板里点回来（新窗口只列命中关键词的任务，
    # 前置要先有命中它的关键词——用户 2026-10-27 的屏蔽都来自关键词误命中，正好命中得到）。
    gql.Options.RefreshKeywordPanel()
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(190, {}).get("btn") != "取消忽略":
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"忽略掉的任务要列在右栏、按钮标\"取消忽略\": {vis}")

    # 本轮口径的另一半：点一条**从来没屏蔽过**的任务 = 只记账，不换来上板凭据
    # （193 在池里、金币没到门槛、不命中关键词 → 本来就不列）。
    # 上一版这里会盖一个 manualAdded 把它硬塞进矩阵，用户 2026-10-27 说清那不是他的意图。
    # （旧弹框"点一行"的入口随弹框下线，这条规则改走 Track.AddQuest 本身继续钉，
    #  与 test_a_click_add_does_not_force_the_row_onto_the_matrix 同一条数据判据。）
    early = []
    g.print = lambda *a: early.append(" ".join(str(x) for x in a))
    gql.Track.AddQuest(193)
    if "193," in matrix_listed(lua):
        return fail("panel_block_survives_resync_and_is_reversible",
                    "点一下\"添加\"不许再把没凭据的行塞进矩阵: " + matrix_listed(lua))
    if lua.execute("return GQLDB.quests[193].manualAdded ~= nil"):
        return fail("panel_block_survives_resync_and_is_reversible",
                    "manualAdded 字段已下线，AddQuest 不该再写它")
    if not any("不强制上板" in p for p in early):
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"没被屏蔽过的行点完要说清不强制上板：{early}")

    # 更狠的一种：这任务轮换出池了，池子问不到它 —— 只按池建列表就会把
    # 屏蔽过的那条一起漏掉，玩家删错就只能干看着。账本里的命中不受池子轮换影响。
    set_test_pool(lua, [(191, 20000000)])
    gql.Sync.SnapshotNow()
    gql.Options.RefreshKeywordPanel()
    vis = {r["qid"]: r for r in panel_right_rows(lua) if r["shown"]}
    if vis.get(190, {}).get("btn") != "取消忽略":
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"不在池里也要能找回忽略掉的任务（账本那半来源）: {vis}")

    printed = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    if not click_panel_ignore_button(lua, 190):
        return fail("panel_block_survives_resync_and_is_reversible", "点不动\"取消忽略\"按钮")
    if not lua.execute("return GQLDB.quests[190].ignored == nil"):
        return fail("panel_block_survives_resync_and_is_reversible", "点一下该撤销忽略")
    if "190," not in matrix_listed(lua):
        return fail("panel_block_survives_resync_and_is_reversible", "撤销忽略后这一行要回到矩阵")
    if not any("已取消忽略" in p for p in printed):
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"取消忽略别报成'已添加'（玩家没加过）：{printed}")
    if not any("在不在板上" in p or "关键词" in p for p in printed):
        return fail("panel_block_survives_resync_and_is_reversible",
                    f"撤销屏蔽这句要把\"这行会不会回来由谁判\"说清：{printed}")
    ok("panel_block_survives_resync_and_is_reversible")


def test_removed_keyword_log_quest_does_not_come_back():
    """任务日志那条来源每轮都会重新收关键词命中的任务。玩家在屏蔽管理里点"忽略"移除过它，
    再收就会把 kwLogAdded 写回去、当场抹掉"别再显示"的意图（12.1.5 实测"删不掉"的另一半）。
    按时长清理也不许把忽略过的记录整条丢掉 —— 丢了下次同步照样刷回来。
    屏蔽那一步第 31 轮换过入口：矩阵右键已下线，改走「关键词 · 屏蔽管理」右栏的"忽略"
    （关键词"萨拉塔斯"本用例已经加过，直接开面板选它点按钮，全程真实 OnClick）；
    数据层断言（logAdded、kwLogAdded、清理、刷回）一条没动。"""
    lua, gql, _ = sync_env([(192, 30000000)], extra=("Matrix", "Track", "Options"))
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9011, title = '追踪萨拉塔斯', "
                "        isHeader = false } end "
                "  return nil end")
    g = lua.globals()
    gql.Config.AddKeyword("萨拉塔斯")
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if "9011," not in matrix_listed(lua):
        return fail("removed_keyword_log_quest_does_not_come_back", "前提：日志收的任务在矩阵里")
    gql.Options.OpenKeywordPanel()
    if not select_panel_keyword(lua, "萨拉塔斯"):
        return fail("removed_keyword_log_quest_does_not_come_back", "面板左栏点不动关键词行")
    if not click_panel_ignore_button(lua, 9011):
        return fail("removed_keyword_log_quest_does_not_come_back",
                    "右栏那一行点不动\"忽略\"（走真实 OnClick）")
    if not lua.execute("return GQLDB.quests[9011].ignored == true"):
        return fail("removed_keyword_log_quest_does_not_come_back",
                    "前提：面板点\"忽略\"要当场落成永久屏蔽")
    res = gql.Sync.SnapshotNow()
    if int(res["logAdded"]) != 0:
        return fail("removed_keyword_log_quest_does_not_come_back",
                    f"移除过的不该再被日志收回来，实际收 {res['logAdded']}")
    if lua.execute("return GQLDB.quests[9011].kwLogAdded ~= nil"):
        return fail("removed_keyword_log_quest_does_not_come_back",
                    "移除过的任务不许被日志源悄悄写回'日志收的'标记（等于当场抹掉玩家的意图）")
    if "9011," in matrix_listed(lua):
        return fail("removed_keyword_log_quest_does_not_come_back", "移除过的行同步后仍不该出现")
    lua.execute("GQLDB.quests[9011].lastSeenAt = 0")
    gql.Ledger.Prune(gql.Config.DB(), 30 * 86400)
    if not lua.execute("return GQLDB.quests[9011] ~= nil and GQLDB.quests[9011].ignored ~= nil"):
        return fail("removed_keyword_log_quest_does_not_come_back",
                    "陈旧清理别把'别再显示'的意图连同记录一起丢掉")
    g.GQL_NOW = 600000
    gql.Sync.SnapshotNow()
    if "9011," in matrix_listed(lua):
        return fail("removed_keyword_log_quest_does_not_come_back", "清理过之后也别刷回来")
    ok("removed_keyword_log_quest_does_not_come_back")


def test_matrix_has_no_block_entry():
    """静态守卫（第 31 轮）：屏蔽一条任务在 UI 上只剩「关键词 · 屏蔽管理」这一个入口。
    本轮的验收物是"入口少了一个"，光钉行为用例拦不住下一个人 —— 他在 Matrix 里再挂
    一颗右键按钮，行为用例跑得照样绿，所以这里按文件扫源码：
    · `Track.RemoveQuest` 的**调用**只许出现在 Options.lua（面板右栏那颗按钮）。
      `function Track.RemoveQuest` 是 Track.lua 自己的定义行、不算入口；
      `Ledger.RemoveQuest` 与 Track 内部那次转调也不算 —— 数据层本轮一个字没动。
    · Matrix.lua 里连 `RemoveQuest` 这个字样都不许出现（注释也一起管）：
      矩阵既不该调它，也不该再顶着"右键能屏蔽"这种已经成假的说法写注释。
    判"调用"前先剥掉注释与字符串字面量（照 test_only_api_reads_data_globals 的办法），
    否则 Track.lua 那句"记的是**永久**屏蔽（Ledger.RemoveQuest）"会被读成一次调用。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "GoldQuestLedger")

    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    lua_comments_and_strings = re.compile(
        r"--\[(=*)\[[\s\S]*?\]\1\]"        # 长注释 --[==[ … ]==]
        r"|--[^\n]*"                        # 行注释
        r"|\[(=*)\[[\s\S]*?\]\2\]"          # 长字符串 [[ … ]] / [==[ … ]==]
        r"|\"(?:[^\"\\\n]|\\[\s\S])*\""     # 双引号串（含 \" 转义）
        r"|'(?:[^'\\\n]|\\[\s\S])*'"        # 单引号串
    )
    bad = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua"):
            continue
        with open(os.path.join(src_dir, fn), encoding="utf-8") as fh:
            raw = fh.read()
        code = lua_comments_and_strings.sub(blank, raw)
        for i, line in enumerate(code.splitlines(), start=1):
            if not re.search(r"\bTrack\.RemoveQuest\b", line):
                continue
            if re.search(r"\bfunction\s+Track\.RemoveQuest\b", line):
                continue                    # 定义行，不是屏蔽入口
            if fn != "Options.lua":
                bad.append("%s:%d 多了一个屏蔽入口（只许面板按钮调 Track.RemoveQuest）: %s"
                           % (fn, i, line.strip()))
        if fn == "Matrix.lua":
            for i, line in enumerate(raw.splitlines(), start=1):
                if "RemoveQuest" in line:
                    bad.append("Matrix.lua:%d 矩阵不该再碰屏蔽: %s" % (i, line.strip()))
    if bad:
        return fail("matrix_has_no_block_entry", "；".join(bad))
    ok("matrix_has_no_block_entry")


def test_api_static_quest_info_cached_within_session():
    """任务名、所在地图在一个会话里不会变：问出来就该记住。之前每次快照都按候选
    名单再逐个问一遍（每个任务 6~7 次接口调用 × 池里几十个任务），矩阵开着做任务时
    的周期性卡顿有一半是这份白烧。取不到的（奖励数据还没到）不许缓存。"""
    lua, gql, _ = sync_env([])
    g = lua.globals()
    titleCalls, mapCalls = [], []
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestCompleted": lambda qid: False,
        "GetTitleForQuestID": lambda qid: (titleCalls.append(int(qid)) or "任务%d" % int(qid)),
        # 链里唯一表字段型的地图入口。只答 500，别的 id 返回 nil
        # 才验得出"取不到不许缓存"
        "GetQuestInfo": lambda qid: (lua.table_from({"mapID": 2007})
                                     if (mapCalls.append(int(qid)) or int(qid) == 500) else None),
    })
    g.C_TaskQuest = lua.table_from({
        # 已摘掉的那条留着当诱饵：链里还取 ret2 的话这里也会命中，缓存计数就对不上
        "GetQuestInfoByQuestID": lambda qid: ("任务", 2696) if int(qid) == 500 else None,
        "GetQuestZoneID": lambda qid: None,
    })
    for _ in range(3):
        if str(gql.Api.QuestTitle(500)) != "任务500":
            return fail("api_static_quest_info_cached_within_session", "任务名应取到")
        if int(lua.execute("return (GQL.Api.QuestMapID(500)) or 0")) != 2007:
            return fail("api_static_quest_info_cached_within_session", "mapID 应取到")
    if len(titleCalls) != 1 or len(mapCalls) != 1:
        return fail("api_static_quest_info_cached_within_session",
                    f"静态属性问一次就该记住，实际 名={len(titleCalls)} 图={len(mapCalls)}")
    # 取不到不缓存：换个 id 先确认没问到，再让它开始有答案，必须重新去问
    g.C_QuestLog.GetTitleForQuestID = lambda qid: None
    if gql.Api.QuestTitle(501) is not None:
        return fail("api_static_quest_info_cached_within_session", "问不到应返回 nil")
    callsNow = len(titleCalls)
    g.C_QuestLog.GetTitleForQuestID = lambda qid: (titleCalls.append(int(qid)) or "任务%d" % int(qid))
    if str(gql.Api.QuestTitle(501)) != "任务501":
        return fail("api_static_quest_info_cached_within_session",
                    "取到答案前不许缓存，否则会一直显示不出来")
    if len(titleCalls) != callsNow + 1:
        return fail("api_static_quest_info_cached_within_session", "空结果的 questID 应重新去问")
    ok("api_static_quest_info_cached_within_session")


def test_matrix_scroll_reuses_cell_click_handler():
    """滚一格就整表重绘：每格重新 new 一个 OnClick 闭包，几百行 × 角色列数 =
    连滚几轮就制造几百上千个待回收闭包。目标（角色列）没变时该留着原处理器。"""
    lua, gql, _ = sync_env([(130, 30000000), (131, 20000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    if not lua.execute(
            "GQL_TEST_CLICK = nil "
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  if c.__scripts and c.__scripts['OnClick'] then "
            "    GQL_TEST_CLICK = c.__scripts['OnClick'] break end end "
            "return GQL_TEST_CLICK ~= nil"):
        return fail("matrix_scroll_reuses_cell_click_handler", "应存在格子点击处理器")
    lua.execute("for i, c in ipairs(GQLMatrixContent.__children) do "
                "  local s = c.__scripts and c.__scripts['OnMouseWheel'] "
                "  if s then s(c, -1) break end end")     # 滚一轮 = 整表重绘
    if not lua.execute(
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  if c.__scripts and c.__scripts['OnClick'] then "
            "    return c.__scripts['OnClick'] == GQL_TEST_CLICK end end return false"):
        return fail("matrix_scroll_reuses_cell_click_handler",
                    "列没变时重绘不该换掉点击处理器（每轮都在新建闭包）")
    # 角色列真的换了（多一个角色）时处理器要跟着改：点击仍要落到正确的角色上
    g.GQLDB["chars"]["小号二-永恒之井"] = lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})
    gql.Matrix.Render()
    if not lua.execute(
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  local s = c.__scripts and c.__scripts['OnClick'] "
            "  if s then s() return true end end return false"):
        return fail("matrix_scroll_reuses_cell_click_handler", "重绘后应还能点到热区")
    if not lua.execute(
            "for k, rec in pairs(GQLDB.manual) do if rec[130] then return true end end "
            "return false"):
        return fail("matrix_scroll_reuses_cell_click_handler",
                    "点击应把这一格的手动状态写进某个角色（闭包读错了列）")
    ok("matrix_scroll_reuses_cell_click_handler")


def test_matrix_rows_money_and_order_unchanged_by_cache():
    """排序键改成"先一次算好金额"之后，口径必须和原来一致：同收益按 questID 稳定排，
    只有手动补录金额的任务也不能被金币门槛误滤掉。
    金额从 200g 改成 600g：第十五轮反馈①"出来一堆狩猎的 >100g 已做任务，门槛要提到
    500g 或可配置"之后默认门槛是 500g，200g 那条现在**应该**被滤掉（新用例专门验这条）。"""
    lua, gql, _ = sync_env([(140, 30000000), (141, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    rows1 = lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQLDB, nil) local s = '' "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  s = s .. r.questID .. ',' end end return s")
    rows2 = lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQLDB, nil) local s = '' "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  s = s .. r.questID .. ',' end end return s")
    if str(rows1) != str(rows2):
        return fail("matrix_rows_money_and_order_unchanged_by_cache",
                    f"同收益必须稳定排序：{rows1} vs {rows2}")
    if str(rows1) != "140,141,":
        return fail("matrix_rows_money_and_order_unchanged_by_cache",
                    f"同额应按 questID 升序，实际 {rows1}")
    key = str(gql.Sync.CurrentCharKey())
    g.GQLDB["manual"][key] = lua.table_from(
        {999: lua.table_from({"state": "done", "t": 10, "g": 6000000})})
    g.GQLDB["quests"][999] = lua.table_from({"name": "手工任务", "mapID": 2007})
    rows3 = lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQLDB, nil) local seen = false "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  if r.questID == 999 then seen = true end end end return seen")
    if rows3 is not True:
        return fail("matrix_rows_money_and_order_unchanged_by_cache",
                    "只有手动补录金额（600g）的任务应过 500g 门槛")
    ok("matrix_rows_money_and_order_unchanged_by_cache")


def test_matrix_gold_threshold_is_configurable():
    """用户 2026-10-24 第十五轮反馈①（原话）："越改越坏了，出来了一堆其他的任务
    （狩猎的 >100g 的已做任务，这样的话，门槛就要提到 500g 或者可以配置）"。
    100g 这个数写死在代码里 = 替 14 个小号决定什么值得刷，用户只能看着一屏不想要的行。
    所以门槛改成配置项：默认提到 500g，`/reload` 之后还在（存进 SavedVariables）。
    （门槛那段 UI 2026-10-27 从设置面板撤下，见 test_options_gold_threshold_control_writes_config
     —— 这里钉的是数据层与判据：Config.SetMinGold 仍是唯一合法写点。）
    另一件必须钉住：**矩阵、设置面板、/gql quest 三处读同一个函数**。上一版各自抄数字，
    门槛一改，"这条为什么没进矩阵"那份报告就指着旧门槛说瞎话。"""
    lua, gql, _ = sync_env([(140, 30000000), (141, 3000000), (142, 5000000)],
                           extra=("Matrix", "Dump", "Options"))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    # 默认档：3000g / 500g 过门（500 整是"≥"，不是"＞"），300g 不许再进来
    if int(gql.Config.GetMinGold()) != 500:
        return fail("matrix_gold_threshold_is_configurable",
                    "默认门槛应提到 500 金，实际 " + str(gql.Config.GetMinGold()))
    if int(lua.execute("return GQL.Config.MinMatrixMoney()")) != 5000000:
        return fail("matrix_gold_threshold_is_configurable",
                    "1 金 = 10000 铜，界面输入的是金、门槛比的是铜")
    if matrix_listed(lua) != "140,142,":
        return fail("matrix_gold_threshold_is_configurable",
                    "300g 那条正是用户抱怨的形状，不该在列；500g 整要按 ≥ 收下: "
                    + matrix_listed(lua))
    # 改档：100g → 300g 那条回来（顺序仍按收益高者在前）
    ok_set, val = gql.Config.SetMinGold("100")
    if not ok_set or int(val) != 100:
        return fail("matrix_gold_threshold_is_configurable",
                    f"SetMinGold 应生效并回报新值，实际 {ok_set} {val}")
    if matrix_listed(lua) != "140,142,141,":
        return fail("matrix_gold_threshold_is_configurable",
                    "门槛降到 100g 后那条 300g 的任务要立刻回到矩阵（仍按收益高者在前）: "
                    + matrix_listed(lua))
    # 存盘：门槛是 SavedVariables 里的配置，不是会话内的临时值
    if int(lua.execute("return GQLDB.config.minGold or -1")) != 100:
        return fail("matrix_gold_threshold_is_configurable",
                    "门槛要落进 db.config（/reload 之后还在）")
    # 非法输入不改值：把"门槛"设成 0 或负数等于关掉门槛，那正是用户抱怨的一屏
    before = one(lua.execute("return tostring(GQLDB.config.minGold)"))
    for bad in ("abc", ""):
        okBad, msg = gql.Config.SetMinGold(bad)
        if okBad:
            return fail("matrix_gold_threshold_is_configurable",
                        f"「{bad}」不该被当成门槛接受")
    if one(lua.execute("return tostring(GQLDB.config.minGold)")) != before:
        return fail("matrix_gold_threshold_is_configurable",
                    "非法输入要把原值留着，不能顺手清成 nil")
    # 报告跟着门槛走：同一份账本、同一条任务，改档前后台词里的数字必须不同。
    # 上一版 Dump/Probe 各抄一份 100g，门槛一改它们就指着旧门槛解释"为什么没进矩阵"。
    okRaise, _ = gql.Config.SetMinGold("2000")
    if not okRaise:
        return fail("matrix_gold_threshold_is_configurable", "2000 金应当被接受")
    if matrix_listed(lua) != "140,":
        return fail("matrix_gold_threshold_is_configurable",
                    "门槛提到 2000 金后 300g/500g 两条都要退出矩阵: " + matrix_listed(lua))
    txt = str(gql.Dump.Quest("任务141"))
    if "金币也没到 2000" not in txt:
        return fail("matrix_gold_threshold_is_configurable",
                    "/gql quest 的门槛文案要读当前配置（现在是 2000 金），不许抄写死的数:\n" + txt)
    ok("matrix_gold_threshold_is_configurable")


def test_a_turned_in_row_this_round_admits_row_without_money():
    """用户 2026-10-24 第十五轮反馈④（原话）："松露大乱斗只有在狩猎模式下看才是金币任务，
    所以当一个角色完成一个符合条件的金币任务时，至少要加入矩阵"，附游戏自带气泡截图 962 金。
    接口侧读不到它：`GetQuestLogRewardMoney` 回 0、`HaveQuestRewardData=false`，
    另两个候选入口 12.1.5 实测 MISSING —— 金额这道闸门**永远**判不出这条，
    于是"做过的角色连行都看不见"。
    改判据：有角色亲手交过它 = 玩家真拿金币换过，比任何接口答的金额都硬。
    2026-10-25 起这道门按轮次收口（12 小时区换了一条实例，上一轮的已交不再占行），
    周常那条的本轮=整个 CD，行为不变（另两条用例分别钉这两侧）。
    两件事要分开钉：① 只认这一笔**存在**，不认金额（窗口被邮箱/拍卖污染时 g 记 nil，
    可"交过"照样成立）；② 上一轮交的不算（换周了它可能已经不轮换出来）。
    走真实事件入口 fire(QUEST_COMPLETE) → FinishWindow，不直调 Ledger.OnTurnIn。"""
    lua, gql, money = sync_env([(140, 30000000)], extra=("Matrix", "Dump"))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    # 松露大乱斗的形状：账本里有记录（补扫/日志收进来的）、接口一分钱都答不出、不在池里
    # poolSeen 得写上：收进账本的那条路（Ledger 补扫）一定会盖这个章，
    # 交任务记账现在只认有身份的记录（本轮 Task 2），漏写就等于测了一个生产里不存在的形状。
    lua.execute("GQLDB.quests[95484] = { name = '松露大乱斗', mapID = 2437, "
                "observations = {}, poolSeen = true }")
    if "95484," in matrix_listed(lua):
        return fail("turned_in_this_cycle_admits_row_without_money",
                    "金额读不出、又没人交过的时候它不该在列（先钉住后半句验的是交过）")
    # 交任务时钱没变化（奖励是物品/材料）→ gain=0，仍然要入账
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 95484)
    g.GQL_NOW = 20
    gql.Sync.FinishWindow()
    listed = matrix_listed(lua)
    if "95484," not in listed:
        return fail("turned_in_this_cycle_admits_row_without_money",
                    "任一角色本轮交过就要进矩阵，哪怕金额一笔都没读到: " + listed
                    + " 入账=" + one(lua.execute(
                        "local t = GQLDB.turnins[GQL.Sync.CurrentCharKey()][95484] "
                        "return t and tostring(t[1].g) or 'nil'")))
    # 换轮（cycleAnchor 变了）→ 那一笔不再是本周的证据，行也跟着退场
    lua.execute("GQLDB.cycleAnchor = (GQLDB.cycleAnchor or 0) + 86400 * 7")
    if "95484," in matrix_listed(lua):
        return fail("turned_in_this_cycle_admits_row_without_money",
                    "上周交的不能冒充本周（12 小时区'上一轮的已交冒充这一轮'同形）")
    # 换个角色交也算：矩阵是 14 个小号共用的一张表
    lua.execute("GQLDB.cycleAnchor = 0 "
                "GQLDB.turnins['小号二-永恒之井'] = { [95484] = { { t = 10, g = nil } } }")
    if "95484," not in matrix_listed(lua):
        return fail("turned_in_this_cycle_admits_row_without_money",
                    "别的角色本周交过的同样算（g=nil 只认这笔存在）")
    # 取证同源：/gql quest 要报出"在列靠：本轮有角色交过"，不能仍说"金币也没到 X"
    txt = str(gql.Dump.Quest("松露大乱斗"))
    if "本轮有角色交过" not in txt:
        return fail("turned_in_this_cycle_admits_row_without_money",
                    f"诊断报告要与矩阵同一道门，否则用户照报告去改门槛:\n{txt}")
    ok("turned_in_this_cycle_admits_row_without_money")


def test_legacy_map_number_is_kept_and_reasked():
    """用户 2026-10-24 第十五轮反馈②③（原话）："太阳的支持 应该是属于永歌森林的，
    怎么改到别的地方了？""具体地图的特别任务没有了，没有地图的几个特别任务反而还在"，
    附截图：底部一大组"未知地图"。
    上一版把"有 mapID 但 mapSrc 是空"当成已停用谎报接口的残留一起清成 nil。回看证据：
    那三条接口写的号**都带自己的标签**，"没标签"根本推不出"是谎报接口写的"，它只是旧版
    来不及记标签的客户端答复。清掉的代价就是这一屏未知地图。
    现在的口径：**留着图号 + 打标 `旧记录未标来源` + 每轮照旧重问**
    （HealMissingMaps 原来只看 mapID==nil，这类号会永远躲过复核，那是另一半错）。
    问得出就覆盖成新答复并标来源，问不出原样保留 —— 既不塌进未知地图，也不冒充本轮证据
    （这个标签不在 PropagateFamilyMaps 的可信白名单里，见上一条用例）。"""
    lua, gql, _ = sync_env([(180, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        # 170 号：客户端这轮改口了（旧图 2395 → 2373）；171 号：这轮问不出
        "GetQuestZoneID": lambda qid: 2373 if int(qid) == 170 else None,
    })
    # 两条都是"旧版写的、没有来源标签"的号（SavedVariables 里的真实形状）。
    # 它们不在本轮池里（currentSig 会被清掉、金额跟着归零），所以靠关键词占行 ——
    # 这样"归组"那条断言验的是地图，不是门槛。
    lua.execute("GQLDB.quests[170] = { name = '独一份任务', mapID = 2395, "
                "  lastSeenAt = 1699996400, "
                "  currentSig = '30000000', lastMoney = 30000000, "
                "  observations = { ['30000000'] = { gold = true, money = 30000000, "
                "    firstSeen = 1699996400, lastSeen = 1699996400 } } }")
    lua.execute("GQLDB.quests[171] = { name = '另一个任务', mapID = 2395, "
                "  lastSeenAt = 1699996400, "
                "  currentSig = '30000000', lastMoney = 30000000, "
                "  observations = { ['30000000'] = { gold = true, money = 30000000, "
                "    firstSeen = 1699996400, lastSeen = 1699996400 } } }")
    gql.Config.AddKeyword("独一份任务")
    gql.Config.AddKeyword("另一个任务")
    gql.Sync.SnapshotNow()

    def mapof(qid):
        return one(lua.execute("local r = GQLDB.quests[%d] "
                               "return tostring(r.mapID) .. '/' .. tostring(r.mapSrc)" % qid))

    if mapof(170) != "2373/补问 C_TaskQuest.GetQuestZoneID":
        return fail("legacy_map_number_is_kept_and_reasked",
                    "旧图号要留着当待复核的答案，本轮问得出就覆盖并标上新来源: " + mapof(170))
    if mapof(171) != "2395/旧记录未标来源":
        return fail("legacy_map_number_is_kept_and_reasked",
                    "问不出的那条要留在原图上、只打存疑标签（清成 nil 就是用户骂的未知地图）: "
                    + mapof(171))
    # 标签是"来源存疑"不是"没地图"：矩阵仍按那张图分组，不许掉进底部未知地图组
    grp = one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), nil) local s = '' "
                          "for i, g in ipairs(out) do s = s .. tostring(g.mapID) .. ',' end return s"))
    if "2395," not in grp:
        return fail("legacy_map_number_is_kept_and_reasked",
                    f"留着旧图号的那条要照常归组，不该多出未知地图组: {grp}")
    if "-1," in grp:
        return fail("legacy_map_number_is_kept_and_reasked",
                    f"底部不许出现未知地图组（第十五轮反馈②③那一屏）: {grp}")
    # 重问每轮都做，不是打标时问一次就完：下一轮客户端改口要能跟上
    lua.execute("GQLDB.quests[171].mapID = 2395 GQLDB.quests[171].mapSrc = nil")
    g.C_TaskQuest.GetQuestZoneID = lambda qid: 2373 if int(qid) in (170, 171) else None
    gql.Sync.SnapshotNow()
    if mapof(171) != "2373/补问 C_TaskQuest.GetQuestZoneID":
        return fail("legacy_map_number_is_kept_and_reasked",
                    "打标的下一轮也要重问，否则这张旧图永远没人推翻: " + mapof(171))
    ok("legacy_map_number_is_kept_and_reasked")


def test_family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it():
    """用户 2026-10-24 定稿（原话）："我鼠标悬停的丰饶 和 我真正要完成的 丰饶日常 不是一个
    任务。你反查完成的丰饶为准"，板上那条"不显示它（它不是周常）"。
    SavedInstances 同一族也只有一条：Modules/Progress.lua:1622-1632 type=single、
    questID=89507、reset="weekly"，板上那些"丰饶：草药洞穴"它根本不跟。
    落点：**占行的是族本体**（名字、地图、勾都由它报，勾问全族），板面子任务只贡献两件事
    —— "这族本周还活着"与"本周它挂在哪张图"；它那条 7200 秒的轮换倒计时绝不许写成
    族本体的到期（那正是上一轮 加固符文石 整行下午消失的病因）。
    旧口径①"行=子任务、到期就结束、新的出来在别的图"按本轮裁决作废。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    rows = lua.table_from({1: lua.table_from({"questID": 196, "mapID": 2393,
                                              "name": "辉耀丰饶", "money": 30000000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2393} end")
    g.C_TaskQuest = lua.table_from({
        # 族本体自己不在任何板上（它是每周 lockout，不是板面条目）
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507 else None,
        "GetQuestsOnMap": lambda mid: lua.table_from(
            {1: lua.table_from({"questID": 196, "mapID": 2393})}) if int(mid) == 2393 else None,
        "GetQuestTimeLeftSeconds": lambda qid: 7200 if int(qid) == 196 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395
        else (lua.table_from({"name": "巨龙岛"}) if int(mid) == 2393 else None),
    })
    # 只留 IsQuestFlaggedCompleted：DONE_SOURCES 先问 IsQuestCompleted，
    # 替身要还给 False 就永远轮不到真那条（名单顺序由 Api 定，这里配合它）
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
    })
    gql.Sync.SnapshotNow()
    # 第二轮才搬地图：第一轮扫描时账本里还没有 196 这条（池那趟在它后面才落账）
    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQLDB.quests[89507].regKey or ''")) != "abundant-offerings":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "内置表这条要带 regKey 落账（周常本体），名字由接口答出来")

    listed = one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                             "local s = '' for i, grp in ipairs(out) do "
                             "  for j, r in ipairs(grp.rows) do "
                             "    s = s .. r.questID .. '/' .. tostring(r.family) "
                             "    .. '/' .. tostring(r.familyCount) .. ',' end end return s"))
    if listed != "89507/abundant-offerings/2,":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"占行的只能是族本体那一行，且它要认出族里两条 ID：{listed}")

    # 族本体的地图只能来自**这一轮**同族成员的可信证据（池行自带/跟踪地图清单/清单反查/
    # GetQuestZoneID），标记里带上是哪条成员给的才查得出归组是谁答的。
    # 这条标记故意不算"可信"：换轮后板上成员跳到别的图，锚点要跟着搬，
    # 算可信就永远停在上周那张（12.1.5 反馈"丰饶出现在了屠戮者高地"）。
    if one(lua.execute('local rec = GQLDB.quests[89507] '
                       'return tostring(rec.mapID) .. \'/\' .. tostring(rec.mapSrc)')) \
            != "2393/同族成员 196":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "族本体那行的地图要跟着这轮板面上问到的同族子任务搬："
                    + one(lua.execute("local rec = GQLDB.quests[89507] "
                                      "return tostring(rec.mapID) .. '/' .. tostring(rec.mapSrc)")))
    if one(lua.execute("return GQLDB.quests[89507].expiresSrc or ''")) != "下次周重置":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "注册周常的到期只认 CD 尾巴，不许是板上那条的轮换倒计时："
                    + one(lua.execute("return tostring(GQLDB.quests[89507].expiresSrc)")))

    key = str(gql.Sync.CurrentCharKey())
    if one(lua.execute("return GQLDB.chars[...].views[196].s", key)) == "done":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "196 这条自己没做过，视图不许被家族改写（勾来自问全族那一层）")
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "丰饶供品" not in joined:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"矩阵要显示族本体那行:\n{joined}")
    if "辉耀丰饶" in joined:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"板上那条子任务不占行（用户：它不是周常）:\n{joined}")
    if "巨龙岛" not in joined:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"要归进这一轮板面证据那张图:\n{joined}")
    if "银月城" in joined:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"GetMapForQuestPOIs 猜出来的城不该还占一个分组:\n{joined}")

    calls = []
    g.GameTooltip = lua.table_from({
        "SetOwner": lambda *a: None,
        "ClearLines": lambda *a: None,
        "AddLine": lambda self, text, *a: calls.append(str(text)),
        # 标题行改成双列（左=任务名，右=灰字 questID），替身把两列拼成一行
        "AddDoubleLine": lambda self, l, r, *a: calls.append("%s %s" % (l, r)),
        "Show": lambda *a: calls.append("SHOW"),
        "Hide": lambda *a: None,
    })
    lua.execute("for i, c in ipairs(GQLMatrixContentL.__children) do "
                "  if c.__questID == 89507 then c.__mouseover = true GQL_FOCUS = c break end end")
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    tip = "\n".join(calls)
    if "本周同族 2 条" not in tip:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"hover 要说清这一格问的是全族:\n{tip}")
    if "距下次周重置" not in tip:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"族本体报的是本周 CD 的尾巴，话要说清来源:\n{tip}")
    if "2时0分" in tip:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"板上那条的 7200 秒不许冒充这一行的剩余时间:\n{tip}")

    # 族里翻成"本周谁都没做过" → 那一格跟着变回空勾（勾只由家族状态决定，不写死）
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, ...)",
                       key, lua.table_from({196, 89507}))) == "done":
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "接口翻成没做过，这一格要跟着退回未做")
    # 到期口径：板上那条 7200 秒走完，这一行不许跟着走（整周常驻）；CD 尾巴过了才退出。
    # 锚点记录要留在账本里当家族证据，重上板时不必重问名字/地图。
    if "89507," not in matrix_listed(lua, 1700007300):
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "板上子任务到期不该带走族本体这行:\n" + matrix_listed(lua, 1700007300))
    if "89507," in matrix_listed(lua, 1700432100):
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "本周 CD 的尾巴过了这行该自己走:\n" + matrix_listed(lua, 1700432100))
    if lua.execute("return GQLDB.quests[89507] == nil"):
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "砍的是显示，不是家族证据")
    ok("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it")


def test_anchor_in_the_pool_lists_only_one_row():
    """池里同时冒出与族本体同名的一条（196 也叫丰饶供品）时，仍然只占一行 ——
    留下的那行是**族本体**（89507）：用户 2026-10-24 定口径"你反查完成的丰饶为准"、
    板上那条"不显示它（它不是周常）"。勾不会因为这行由族本体顶着就丢（它问全族）。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    rows = lua.table_from({})
    rows[1] = lua.table_from({"questID": 196, "mapID": 2395, "name": "丰饶供品", "money": 30000000})
    rows[2] = lua.table_from({"questID": 89507, "mapID": 2395, "name": "丰饶供品", "money": 30000000})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
    })
    gql.Sync.SnapshotNow()
    listed = one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                             "local s = '' for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
                             "  s = s .. r.questID .. '/' .. tostring(r.familyCount) .. ',' end end return s"))
    if listed != "89507/2,":
        return fail("anchor_in_the_pool_lists_only_one_row",
                    f"同名的两行只许占一行，留下的那行是族本体（89507）：{listed}")
    gql.Matrix.Toggle()
    texts = frame_texts(g.GQLMatrixFrame)
    twice = sum(1 for t in texts if "丰饶供品" in t)
    if twice != 1:
        return fail("anchor_in_the_pool_lists_only_one_row",
                    f"矩阵上这行名字只该出现一次，实际 {twice} 次:\n{texts}")
    key = str(gql.Sync.CurrentCharKey())
    if one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                       "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
                       "  return tostring(r.state[...]) end end return 'none'", key)) != "done":
        return fail("anchor_in_the_pool_lists_only_one_row",
                    "这一格的红勾要问全族：族本体被接口答成做过就是做过")
    ok("anchor_in_the_pool_lists_only_one_row")


def test_registered_anchor_refreshes_when_pool_unavailable():
    """锚点/族成员的状态与板面证据只靠"拿着 ID 问接口 + 问跟踪地图清单"，跟世界任务池
    没关系。12.x 池读不到时整趟快照会提前 return，上一版把板面扫描放在那之后 →
    前置失败连带把这一行砍没（矩阵整周空着）。现在池问不出也先把板面证据问完。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_WorldQuests = lua.table_from({})     # 池接口全问不出 = GetPool 回 nil
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2395} end")
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507 else None,
        "GetQuestsOnMap": lambda mid: lua.table_from({
            1: lua.table_from({"questID": 89507, "mapID": 2395})}) if int(mid) == 2395 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
    })
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is None:
        return fail("registered_anchor_refreshes_when_pool_unavailable",
                    f"池问不出时快照应报降级而不是成功: {res}")
    if lua.execute("return GQLDB.quests[89507] == nil"):
        return fail("registered_anchor_refreshes_when_pool_unavailable",
                    "池读不到也要刷家族锚点（它不依赖池）")
    if "89507," not in matrix_listed(lua, 1700000000):
        return fail("registered_anchor_refreshes_when_pool_unavailable",
                    f"这一轮在图上问到了，池哑了也该有这一行: {matrix_listed(lua, 1700000000)}")
    ok("registered_anchor_refreshes_when_pool_unavailable")


def test_family_state_does_not_leak_across_cycles():
    """跨 CD 的"做过"绝不能冒充本周（用户口径：矩阵的红勾就是"这周还差谁"）。
    一族多个 ID 时，任何一个 ID 本周做过就算做过；玩家手点的优先于接口。
    成行的只有族本体那一条（7001），板上那条（7002）只当证据 —— 用户 2026-10-24 定的：
    "我鼠标悬停的丰饶 和 我真正要完成的丰饶日常不是一个任务。你反查完成的丰饶为准。"
    红勾来自全族（用户②"只要完成过一次都认为完成了"）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    lua.execute("GQLDB = {}")
    gql.Config.Init()
    declare_family(lua, "rotating", "轮换周常")
    lua.execute("""
        local db = GQL.Config.DB()
        db.cycleAnchor = 1000
        db.chars["旧号-永恒之井"] = { lastSync = 900, views = {} }
        GQL.Ledger.EnsureRegistered(db, {
            { key = "rotating", questID = 7001, name = "轮换周常" },
            { key = "rotating", questID = 7002, name = "轮换周常：板上的那条", onBoard = true },
        }, "旧号-永恒之井", 900)
        -- 上一 CD 的状态：视图比锚点旧，本周必须算"不知道"
        db.chars["旧号-永恒之井"].views[7001] = { s = "done", t = 999 }
        db.chars["旧号-永恒之井"].lastSync = 999
        -- 本周登录的号：7001 接口说 open，7002 别的接口说 done（族里任一做过即做过）
        db.chars["新号-永恒之井"] = { lastSync = 1001,
            views = { [7001] = { s = "open", t = 1001 }, [7002] = { s = "done", t = 1001 } } }
        db.turnins["新号-永恒之井"] = { [7002] = { { t = 1002 } } }
    """)
    if one(lua.execute("return GQL.Ledger.RegisteredState(GQLDB, ...)",
                       "旧号-永恒之井", 7001)) != "unknown":
        return fail("family_state_does_not_leak_across_cycles",
                    "锚点之前的旧视图不许当本周已做")
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, ...)",
                       "旧号-永恒之井", lua.table_from({7001, 7002}))) != "unknown":
        return fail("family_state_does_not_leak_across_cycles",
                    "整族都旧了就该是 unknown，别拼出一个假红勾")
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, ...)",
                       "新号-永恒之井", lua.table_from({7001, 7002}))) != "done":
        return fail("family_state_does_not_leak_across_cycles",
                    "族里任一 ID 本周做过 = 这一行做过")
    # 玩家手点的 open 优先于接口：他要记"这周我没做"，不能被上一个 CD 的视图盖掉
    lua.execute("GQLDB.manual['旧号-永恒之井'] = { [7001] = { state = 'open', t = 1002 } }")
    if one(lua.execute("return GQL.Ledger.RegisteredState(GQLDB, ...)",
                       "旧号-永恒之井", 7001)) != "open":
        return fail("family_state_does_not_leak_across_cycles",
                    "本周内的手动记录要压过旧视图")
    rows = lua.execute("local out = GQL.Ledger.MatrixRows(GQLDB, 1002) local s = '' "
                       "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
                       "  s = s .. r.questID .. '/' .. tostring(r.familyCount) "
                       "  .. '/' .. r.state['新号-永恒之井'] .. ',' end end return s")
    if str(rows) != "7001/2/done,":
        return fail("family_state_does_not_leak_across_cycles",
                    f"只列族本体那条 ID，勾来自全族：{rows}")
    if "7002" in str(rows):
        return fail("family_state_does_not_leak_across_cycles",
                    f"板上的子任务只当证据，不许再占一行：{rows}")
    # 三周没打开面板也不许被 Prune 清掉（清掉 = 这行的名字/地图都要重新问一遍）
    lua.execute("GQL.Ledger.Prune(GQLDB, 900 + 4 * 7 * 86400)")
    if lua.execute("return GQLDB.quests[7001] == nil"):
        return fail("family_state_does_not_leak_across_cycles",
                    "家族锚点不做三周时清")
    ok("family_state_does_not_leak_across_cycles")


def test_family_member_hidden_only_when_player_removed_it():
    """这一族能不能成行只由板面证据决定：本机不认的那条预设 ID 从来不会被池或跟踪清单
    报出来，等于自动不列（上一版那道"问不出名字就不列"的显示门不用再造）。
    玩家右键移除过的那条要永久让位 —— 但它是家族证据，族里别的那条照旧打勾。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    lua.execute("GQLDB = {}")
    gql.Config.Init()

    def listed_ids():
        return one(lua.execute("local out = GQL.Ledger.MatrixRows(GQLDB, nil) local s = '' "
                               "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
                               "  s = s .. r.questID .. ',' end end return s"))

    declare_family(lua, "ghost", "本机问不出名字的那族")
    declare_family(lua, "feast", "轮换的下半")
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), "
                "{ { key = 'ghost', questID = 89507, state = 'done' } }, "
                "'某人-永恒之井', 10)")
    if listed_ids() != "":
        return fail("family_member_hidden_only_when_player_removed_it",
                    "这轮没在图上问到的锚点不该占一行")
    lua.execute("GQL.Config.DB().quests[89507].name = '丰饶供品' "
                "GQL.Config.DB().quests[89507].mapID = 2395 "
                "GQL.Config.DB().quests[89507].onBoard = true")
    if listed_ids() != "89507,":
        return fail("family_member_hidden_only_when_player_removed_it",
                    f"接口答出名字与地图、这轮也在图上就该列出来: {listed_ids()}")
    lua.execute("GQL.Config.DB().quests[89507].ignored = true")
    if listed_ids() != "":
        return fail("family_member_hidden_only_when_player_removed_it",
                    "玩家右键移除过的族成员不许被下一轮证据刷回来")
    lua.execute("local db = GQL.Config.DB() db.quests[89507].ignored = nil "
                "db.quests[89508] = { observations = {}, regKey = 'feast', name = '轮换的下半', "
                "  ignored = true, onBoard = true } "
                "db.quests[89509] = { observations = {}, regKey = 'feast', name = '轮换的下半', "
                "  onBoard = true } "
                "db.chars['某人-永恒之井'].views[89509] = { s = 'done', t = 10 }")
    if "89509," not in listed_ids() or "89508," in listed_ids():
        return fail("family_member_hidden_only_when_player_removed_it",
                    f"族里被忽略的那条要单独让位，别把整行判死: {listed_ids()}")
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, '某人-永恒之井', "
                       "{ 89508, 89509 })")) != "done":
        return fail("family_member_hidden_only_when_player_removed_it",
                    "被忽略的那条仍是家族证据：它本周做过，整行照样打勾")
    ok("family_member_hidden_only_when_player_removed_it")


def test_map_rows_prefer_taskquest_source():
    """12.1.5 实测 C_QuestLog.GetQuestsOnMap(2444) 回 table[#0]（空表），行数据全在
    C_TaskQuest.GetQuestsOnMap 里。上一版反向索引只问前者，等于一直拿空接口补地图，
    "未知地图"那批就是这么堆出来的。问得出就算问到；问到空表也算这张图问完了。
    用户 2026-10-23 的口径："日志里面有任务时，接口查不到地图归属的，从任务列表里找"
    → 补地图那条线（MapQuestIndex）两个入口合起来问；
      但**板面证据**那条线（MapQuestRows 默认）只认任务清单：丰饶供品这种母任务常年
      住在日志里，让它算板上证据就等于把锚点重新拽回矩阵（"有 2 个丰饶"回来）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_QuestLog = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({}),
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    rows = lua.table_from({1: lua.table_from({"questID": 98180, "mapID": 2395, "inProgress": True})})
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": lambda mid: rows if int(mid) == 2395 else None})
    got, src = lua.execute("local i, s = GQL.Api.MapQuestIndex({ 2395, 2444 }) "
                           "return (i and i[98180]), (s or '')")
    if int(got or 0) != 2395:
        return fail("map_rows_prefer_taskquest_source",
                    "问答得的那条接口要当数据来源，空接口不能顶掉它")
    if str(src) != "C_TaskQuest.GetQuestsOnMap + C_QuestLog.GetQuestsOnMap":
        return fail("map_rows_prefer_taskquest_source",
                    f"补地图那条线两个入口都问了，来源要把它们都写出来: {src}")
    # 任务清单答空的图，日志清单里的那条要能补到地图（用户那句"从任务列表里找"）
    g.C_QuestLog = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from(
            {1: lua.table_from({"questID": 89507, "mapID": 2395})}) if int(mid) == 2395 else None,
    })
    idx = one(lua.execute("local i = GQL.Api.MapQuestIndex({ 2395 }) "
                          "return tostring(i and i[89507])"))
    if idx != "2395":
        return fail("map_rows_prefer_taskquest_source",
                    f"清单问不到、日志里有的那条要能补上地图: {idx}")
    # 同一条线不许把日志行当板面证据：默认（不带 includeLog）只认任务清单那批
    only = one(lua.execute("local r, s = GQL.Api.MapQuestRows(2395) "
                           "local out = {} for i = 1, (r and #r or 0) do out[#out+1] = r[i].questID end "
                           "return table.concat(out, ',') .. '/' .. tostring(s)"))
    if only != "98180/C_TaskQuest.GetQuestsOnMap":
        return fail("map_rows_prefer_taskquest_source",
                    f"板面证据那条线只许认任务清单，日志里的母任务不能算在图上: {only}")
    # 行自己不报 mapID = 没证据：拿"问的是哪张图"当答案会把一批任务塞进同一张图
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({1: lua.table_from({"questID": 98181})}),
    })
    if lua.execute("local i = GQL.Api.MapQuestIndex({ 2395 }) return i and i[98181] ~= nil"):
        return fail("map_rows_prefer_taskquest_source", "行不报地图就不许兜底")
    # 两条接口全问不出 = nil（没证据），不是"确认过这些图上没有任务"
    g.C_TaskQuest = lua.table_from({})
    g.C_QuestLog = lua.table_from({})
    if lua.execute("return GQL.Api.MapQuestIndex({ 2395 }) ~= nil"):
        return fail("map_rows_prefer_taskquest_source",
                    "一张都没问到要回 nil，别把问不出说成没有")
    ok("map_rows_prefer_taskquest_source")


def test_keyword_rows_stay_separate_and_outlive_the_board():
    """玩家关键词注册的普通周常：一条一行、各归自己那张图，而且整周常驻。
    12.1.5 的两个坑：
    ① 上一版拿 GetMapForQuestPOIs 猜出来的银月城把整族钉在一座城上，换图之后行还挂着
       旧名字旧地图（用户："换地图了，换地区了，任务应该也换了"）→ 地图取行自己报的那张。
    ② 板子换轮后这条从清单里问不到了 —— 旧逻辑拿板面倒计时当它的寿命，行提前几小时
       凭空消失（用户："加固符文石这个任务没了，可能和倒计时有关系"）
       → 注册行只按 CD 尾巴算到期，下板不退行。"""
    lua, gql, _ = sync_env([], weekly=432000, reset=1700060480, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
    board = {619: [124], 620: [125]}

    def set_rows(mid):
        """区域任务列在父图（2007）下问得到、行里自己报子图 —— 与按子图问回同一批行，
        这里就用"问哪张图都回整块板、行各报各的 mapID"这个真实形状。"""
        ids = [q for k in sorted(board) for q in board[k]]
        return lua.table_from({i: lua.table_from({"questID": q,
                                                  "mapID": 619 if q == 124 else 620})
                               for i, q in enumerate(ids, 1)})

    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: set_rows(mid),
        "GetQuestInfoByQuestID": lambda qid: (
            ("虚空侵袭：东部", 619) if int(qid) == 124
            else ("虚空侵袭：西部", 620) if int(qid) == 125 else None),
        "GetQuestTimeLeftSeconds": lambda qid: 7200 if int(qid) == 125 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "符文荒野" if int(mid) == 619 else "沙葬荒野"})
        if int(mid) in (619, 620) else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Config.AddKeyword("虚空侵袭")
    gql.Sync.HuntKeywordMaps()

    listed = matrix_listed(lua, 1700000000)
    if "124," not in listed or "125," not in listed:
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    f"关键词收来的两条各占一行、各归自己那张图: {listed}")
    if one(lua.execute("local db = GQL.Config.DB() "
                       "return tostring(db.quests[124].mapID) .. '/' .. "
                       "tostring(db.quests[125].mapID)")) != "619/620":
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "地图要取行自己报的那张，不是「这次问的是哪张图」")
    # 关键词行不是内置族：名字撞了也不许合行（合行 = 一条做了另一条也打勾，用户没要求过）
    if one(lua.execute('local of = GQL.Ledger.Families(GQLDB) '
                       'return tostring(of[124]) .. \'/\' .. tostring(of[125])')) != "nil/nil":
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "玩家关键词注册的普通周常不进家族（第十一轮实测：进了家族整批行凭空消失）")

    # 换轮：东部那条从清单里问不到了 → 行照留，地图留着上次问到的那张
    board[619] = []
    gql.Sync.SnapshotNow()
    listed = matrix_listed(lua, 1700000000)
    if "124," not in listed:
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    f"下板只是这一轮没扫到，常驻周常不许因此消失: {listed}")
    if "125," not in listed:
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    f"还在板上的那条更不许被连带砍掉: {listed}")
    if one(lua.execute("return tostring(GQL.Config.DB().quests[124].mapID)")) != "619":
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "下板不该把上次问到的地图抹成未知")
    if one(lua.execute("return tostring(GQL.Config.DB().quests[125].expiresSrc)")) != "下次周重置":
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "板面给的 2 小时轮换倒计时不是这条周常的寿命："
                    + one(lua.execute("return tostring(GQL.Config.DB().quests[125].expiresAt) "
                                      "  .. '/' .. tostring(GQL.Config.DB().quests[125].expiresSrc)")))
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    for want in ("虚空侵袭：东部", "虚空侵袭：西部", "符文荒野", "沙葬荒野"):
        if want not in joined:
            return fail("keyword_rows_stay_separate_and_outlive_the_board",
                        f"缺「{want}」:\n{joined}")
    ok("keyword_rows_stay_separate_and_outlive_the_board")


def test_pool_countdown_does_not_kill_a_registered_weekly():
    """12.1.5 实测：加固符文石从矩阵里没了（用户："加固符文石这个任务没了，可能和倒计时
    有关系，倒计时不准或者也是子任务导致的"）。根因不在"子任务"，在这个字段被两个写入点
    按板面倒计时抢写过：注册行有时也住在世界任务池/任务日志里，那里问出来的剩余时间是
    **板上这一轮那个实例**的寿命（几小时），旧逻辑拿它直接当到期 → 当天下午这行凭空消失，
    可它是整周的周常（第三个写入点 RefreshRegistered 干脆不再问倒计时了）。
    现在的口径集中在 Ledger.SetExpiry：注册过（regKey）或日志收的（kwLogAdded）只按 CD 尾巴；
    没注册的池任务照旧用自己那条板面倒计时 —— 那才是真"到期即结束"，不许一起改掉。
    顺带钉住日志那条路的维持标记：上一版"同名不收"没按 questID 排除自己，第二轮起这条
    记录被自己的名字判成"另一份同名"，标记一撤就被消失判定砍掉 —— 同样表现为"任务没了"。"""
    lua, gql, _ = sync_env([(89600, 3000000), (89601, 3000000)],
                           weekly=432000, reset=1700060480, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    # 板面倒计时：两条都只活 3 小时（12.1.5 加固符文石那种板上实例的轮换寿命）
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 10800})
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    # 89600 = 补扫收进来的注册行（等价于玩家输入关键词"符文石"点补扫后的账本状态）
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), { { "
                "key = 'kw:符文石', questID = 89600, name = '加固符文石', onBoard = true } }, "
                "GQL.Sync.CurrentCharKey(), 1700000000 )")
    gql.Sync.SnapshotNow()

    def fields(qid):
        return one(lua.execute("local r = GQL.Config.DB().quests[%s] "
                               "return tostring(r.expiresSrc) .. '/' .. tostring(r.expiresAt)" % qid))

    if fields(89600) != "下次周重置/1700432000":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "注册行的寿命是本周 CD 尾巴，不是池给的 3 小时: " + fields(89600))
    if fields(89601) != "接口倒计时/1700010800":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "没注册的池任务仍要按自己的板面倒计时到期（这条口径不许被一起改掉）: "
                    + fields(89601))
    if "89600," not in matrix_listed(lua, 1700000000):
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "刚注册就该在矩阵里: " + matrix_listed(lua, 1700000000))
    # 3 小时后：板上那个实例早走完了，这一行还得在（用户抱怨的就是它下午没了）
    if "89600," not in matrix_listed(lua, 1700100000):
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "板面倒计时走完不许把整周的注册周常带走: " + matrix_listed(lua, 1700100000))
    if "89601," in matrix_listed(lua, 1700100000):
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "普通池任务到期就该退出: " + matrix_listed(lua, 1700100000))
    # 本周 CD 尾巴过了才该消失（下一轮快照会按新锚点重算到期）
    if "89600," in matrix_listed(lua, 1700432100):
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "过了本周 CD 尾巴还占行 = 上周的勾会冒充本周")

    # 第二个写法点：任务还接在**日志**里（kwLogAdded 那条路径，池这一轮收不到它）。
    # 上一轮池给它写过 3 小时倒计时，旧代码在这里"留着已有的接口倒计时"→ 加固符文石
    # 当天下午整行没了（用户："倒计时不准或者也是子任务导致的"，实际跟子任务无关）。
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: lua.table_from(
            {"questID": 89602, "title": "特别任务：加固符文石", "isHeader": False})
        if int(i) == 1 else None,
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Config.AddKeyword("符文石")
    lua.execute("GQL.Config.DB().quests[89602] = { name = '特别任务：加固符文石', "
                "  mapID = 2007, kwLogAdded = true, expiresAt = 1700010800, "
                "  expiresSrc = '接口倒计时', lastSeenAt = 1699990000 }")
    gql.Sync.SnapshotNow()
    # 先钉住标记还在：第二轮它自己（带地图、同名）曾被"同名不收"判成"另一份同名"，
    # keep 收不到 → kwLogAdded 被撤 → 下面那条消失判定把它当成没人维护的记录
    if one(lua.execute("return tostring(GQL.Config.DB().quests[89602].kwLogAdded)")) != "true":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "还接在日志里的这条被自己的名字取消维持了（加固符文石就是这么没的）")
    if fields(89602) != "下次周重置/1700432000":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "还接在日志里的关键词任务，每轮要把板面倒计时改回 CD 尾巴: " + fields(89602))
    if "89602," not in matrix_listed(lua, 1700100000):
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "这一条也不许 3 小时后消失: " + matrix_listed(lua, 1700100000))
    ok("pool_countdown_does_not_kill_a_registered_weekly")


def test_family_ids_come_from_the_objective_list():
    """用户 2026-10-24 问"有办法通过任务列表添加这个丰饶吗"，实测给的方向是：
    "从丰饶供品的任务追踪里面可以看到子任务的内容""从子任务反查不到"
    （GetParentQuestForChild(89507)=nil、C_QuestLog.GetQuestObjectives(89507) 回 table[#1]）。
    → 认亲第二条来源落在母任务自己的目标行上：行里带的 questID 直接并进同一族。
    这条线**不看名字**（下周换的子任务名字不带"丰饶"也认得到），也不要玩家输 ID。
    认来的那条按一族一行只当「本周这族做过没有」的证据 + 这周在哪个图，行仍是族本体。
    反面用例同样重要：角色没接母任务（问不出）/ 目标行只有文本没有 ID 时，
    一条都不许注册 —— 编一个 ID 出来比认不到更糟。"""
    def env(objectives):
        """objectives = 拿 (lua运行时, questID) 回目标行表；None = 这台机器上问不出这条接口。"""
        lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
        g = lua.globals()
        g.time = lambda: 1700000000
        # 跟踪图按父图问，行自己报子图（与 12.1.5 的 GetQuestsOnMap 形状一致）
        lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
        g.C_TaskQuest = lua.table_from({
            "GetQuestsOnMap": lambda mid: lua.table_from(
                {1: lua.table_from({"questID": 122870, "mapID": 2393})}),
            # 子任务名字刻意不带"丰饶"：名字认亲在这条用例里必须失效，
            # 行才真是任务追踪认出来的
            "GetQuestInfoByQuestID": lambda qid: (
                ("丰饶供品", 2395) if int(qid) == 89507
                else ("月光祭品", 2393) if int(qid) == 122870 else None),
            "GetQuestTimeLeftSeconds": lambda qid: 7200 if int(qid) == 122870 else None,
        })
        g.C_Map = lua.table_from({
            "GetMapInfo": lambda mid: lua.table_from(
                {"name": "银月城" if int(mid) == 2395 else "巨龙岛"}),
        })
        qlog = {"IsQuestStarted": lambda qid: False,
                "IsQuestFlaggedCompleted": lambda qid: False}
        if objectives is not None:
            # 这里必须用 env 自己的 lua 实例造表：把外层局部留给调用处会拿到未赋值的空壳
            qlog["GetQuestObjectives"] = lambda qid: objectives(lua, qid)
        g.C_QuestLog = lua.table_from(qlog)
        gql.Sync.SnapshotNow()
        return lua, gql, g

    # ① 目标行带子任务 ID → 同族落账、在板上就成行，全程没有玩家输入
    lua, gql, g = env(lambda L, qid: L.table_from(
        {1: L.table_from({"questID": 122870, "text": "完成一次辉耀丰饶"})}) if int(qid) == 89507 else None)
    if one(lua.execute("return GQLDB.quests[122870] and GQLDB.quests[122870].regKey or ''")) \
            != "abundant-offerings":
        return fail("family_ids_come_from_the_objective_list",
                    "母任务目标行里的 ID 要带同一个族 key 落账："
                    + one(lua.execute("return tostring(GQLDB.quests[122870] and "
                                      "GQLDB.quests[122870].regKey)")))
    if one(lua.execute("return GQLDB.quests[122870].name or ''")) != "月光祭品":
        return fail("family_ids_come_from_the_objective_list",
                    "名字由接口答，不许拿目标行文本当任务名")
    listed = matrix_listed(lua, 1700000000)
    if "89507," not in listed:
        return fail("family_ids_come_from_the_objective_list",
                    f"族本体这条要占行（一族一行）: {listed}")
    if "122870," in listed:
        return fail("family_ids_come_from_the_objective_list",
                    f"认亲收进来的子任务只当证据，不占第二行: {listed}")
    fam = one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                          "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
                          "  if r.questID == 89507 then "
                          "    return tostring(r.family) .. '/' .. tostring(r.familyCount) "
                          "  end end end return 'none'"))
    if fam != "abundant-offerings/2":
        return fail("family_ids_come_from_the_objective_list",
                    f"这一行要认出自己是丰饶家族、族里两条 ID（锚点 + 任务追踪那条）: {fam}")
    # 板上那条的价值是把**本周地图**告诉族本体：89507 自己问不出地图（母任务不在板上），
    # 没有子任务这一轮它只能落在"未知地图"分组里。
    if one(lua.execute("local db = GQL.Config.DB() "
                       "return tostring(db.quests[89507].mapID) .. '/' .. "
                       "tostring(db.quests[89507].mapSrc)")) != "2393/跟踪地图清单":
        return fail("family_ids_come_from_the_objective_list",
                    "子任务那张图要传给族本体："
                    + one(lua.execute("local db = GQL.Config.DB() "
                                      "return tostring(db.quests[89507].mapID) .. '/' .. "
                                      "tostring(db.quests[89507].mapSrc)")))
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "丰饶供品" not in joined or "巨龙岛" not in joined:
        return fail("family_ids_come_from_the_objective_list",
                    f"族本体那行要用子任务传来的这张图归组:\n{joined}")
    if "月光祭品" in joined:
        return fail("family_ids_come_from_the_objective_list",
                    f"同一族不许出现第二行（用户 bug「有 2 个丰饶」）:\n{joined}")

    # ② 角色没接母任务：接口问不出 → 一条都不许多注册（不许拿文本猜 ID）
    lua2, _gql2, _g2 = env(None)
    if not lua2.execute("return GQLDB.quests[89507] ~= nil"):
        return fail("family_ids_come_from_the_objective_list",
                    "内置锚点本身还是要落账（它是家族完成证据的载体）")
    if lua2.execute("return GQLDB.quests[122870] ~= nil"):
        return fail("family_ids_come_from_the_objective_list",
                    "任务追踪问不出就等于没有这条，不许凭空注册")

    # ③ 目标行只有文本没有 ID：认亲失败要安静，不许把文本编成 ID
    lua3, _gql3, _g3 = env(lambda L, qid: L.table_from(
        {1: L.table_from({"text": "完成一次辉耀丰饶"})}) if int(qid) == 89507 else None)
    if lua3.execute("return (function() "
                    "  local declared = {} "
                    "  local presets = GQL.Api.WEEKLY_PRESETS or {} "
                    "  for i = 1, #presets do "
                    "    local ids = presets[i].ids or {} "
                    "    for j = 1, #ids do declared[tonumber(ids[j])] = true end end "
                    "  for id, rec in pairs(GQLDB.quests) do "
                    "    if rec.regKey and not declared[id] then return true end end "
                    "  return false end)()"):
        # 内置表自己声明的 ID 每轮都会带 regKey 落账（那是常驻行，不是认亲认出来的），
        # 所以要排除掉它们才算"文本编成 ID"
        return fail("family_ids_come_from_the_objective_list",
                    "只有文本、没有 ID 的目标行不该注册任何成员")
    # 目标行把母任务自己回填成 questID：那不叫子任务，别把它当第二条成员数
    lua4, _gql4, _g4 = env(lambda L, qid: L.table_from(
        {1: L.table_from({"questID": 89507, "text": "丰饶供品"})}) if int(qid) == 89507 else None)
    if str(lua4.execute("local of, mem = GQL.Ledger.Families(GQLDB) "
                        "local m = mem['abundant-offerings'] "
                        "return tostring(m and #m or 0)")) != "1":
        return fail("family_ids_come_from_the_objective_list",
                    "目标行回填母任务自己的 ID 时不许算成同族第二条")
    ok("family_ids_come_from_the_objective_list")


def test_last_weeks_child_does_not_stand_in_for_this_week():
    """用户 2026-10-24 回传（截图：游戏自带的任务介绍写"丰饶收获 / 东部王国 / 祖阿曼 /
    剩余时间 6 小时 51 分钟"）："图是系统自带的任务介绍，属于祖阿曼地图，而我们的任务叫
    辉煌丰饶 属于银月城，不知道怎么回事"，同一份 probe 里"按关键词…在候选里命中 0 条"。
    两个根因：
    ① 上周那条"辉耀丰饶@银月城"是账本留下的旧记录（没板面证据、也没有本轮到期时刻）。
       按现在的一族一行：名字命中族词它就不是行，只当证据 —— 界面那一行是族本体，
       挂哪张图由这一轮的证据决定，不会被上周那条的名字与地图钉住。
    ② 本周的图（祖阿曼 2437）不在跟踪清单里 → 补扫永远扫不到。上一版拿
       C_QuestLog.GetMapForQuestPOIs 当"轮换指针"每周重问一遍来刷新锚点地图，
       本轮实测把这条钉死：它答的是**玩家脚下那张图**（站在屠戮者高地时 89507 也答 2444），
       12.1.5 反馈⑥"丰饶出现在了屠戮者高地（当前我的角色就在这儿，这个不对的）"就是它。
       已从生产链摘掉（见 Api.QUEST_MAP_SOURCES / RetiredMapSources），所以这里它答得出 2437
       也**不许**再搬锚点的地图 —— 锚点只认两类证据：自己那条问得出的可信地图，
       和本轮板面上真出现过的同族成员（Ledger.PropagateFamilyMaps）。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
    # 上周留下的旧记录：真从池里收进来过（有金币观察、有 lastSeenAt，所以不会被 Prune 清掉），
    # 这一轮板上已经没有它 —— 它该被"板面证据"这道门砍掉，而不是被 100g 门槛或清理顺带砍掉
    lua.execute("GQLDB.quests = GQLDB.quests or {} "
                "GQLDB.quests[122870] = { name='辉耀丰饶', mapID=2393, mapSrc='上周的池行', "
                "  lastSeenAt=1699996400, currentSig='1500000', lastMoney=1500000, "
                "  observations={ ['1500000']={ gold=true, money=1500000, "
                "    firstSeen=1699996400, lastSeen=1699996400 } } }")
    # 锚点账本里也还挂着上周那张图
    lua.execute("GQLDB.quests[89507] = { name='丰饶供品', mapID=2393, regKey='abundant-offerings', "
                "  observations={} }")

    def board(mid):
        # 只有祖阿曼（2437）的板清单里有本周那条；默认跟踪图答空表（问得出、就是没有）
        if int(mid) == 2437:
            return lua.table_from({1: lua.table_from({"questID": 137001, "mapID": 2437})})
        return lua.table_from({})

    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": board,
        # 12.1.5 实测：C_TaskQuest 这族对丰饶供品是空的（答 nil），名字只能走 C_QuestLog
        "GetQuestInfoByQuestID": lambda qid: (
            None if int(qid) == 89507
            else ("辉耀丰饶", 2393) if int(qid) == 122870
            else ("丰饶收获", 2437) if int(qid) == 137001 else None),
        "GetQuestTimeLeftSeconds": lambda qid: 24660 if int(qid) == 137001 else None,
    })
    g.C_QuestLog = lua.table_from({
        "GetTitleForQuestID": lambda qid: (
            "丰饶供品" if int(qid) == 89507 else "辉耀丰饶" if int(qid) == 122870
            else "丰饶收获" if int(qid) == 137001 else None),
        # 轮换指针：本周指向祖阿曼（与游戏自带的气泡同一张图）
        "GetMapForQuestPOIs": lambda qid: 2437 if int(qid) == 89507 else 2393,
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
        # 12.1.5 实测形状：目标行只有 {type,numRequired,finished,text,numFulfilled}，没有 questID
        "GetQuestObjectives": lambda qid: lua.table_from({1: lua.table_from(
            {"type": "monster", "numRequired": 8, "finished": False, "text": "",
             "numFulfilled": 0})}),
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "银月城" if int(mid) in (2393, 2395) else "祖阿曼" if int(mid) == 2437
             else "盘卷蛇岛"}),
    })
    gql.Config.AddKeyword("丰饶")
    gql.Sync.SnapshotNow()

    listed = matrix_listed(lua, 1700000000)
    # 先钉住"这条确实还在账本里、也过得了金币与关键词门槛"：
    # 否则下面的"不列出"可能是 Prune/门槛顺带砍的，验不到一族一行那道门
    if not lua.execute("return GQLDB.quests[122870] ~= nil"):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "上周那条记录被清掉了，这条用例验不到一族一行那道门")
    if one(lua.execute("return tostring(GQL.Config.KeywordMatch('辉耀丰饶'))")) != "true":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "它本该命中关键词，排除只能靠家族占行规则")
    if "122870," in listed:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "名字命中族词的子任务不占行（一族一行）: " + listed)
    if "89507," not in listed:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "占行的是族本体，它整周常驻: " + listed)
    # 锚点的地图：本轮没有任何可信证据（它自己问不出、板上那条这轮没在跟踪图上出现），
    # 撒谎的轮换指针又答得出 2437 —— 这时**只许保持原样并标明来源存疑**，跟着它走就等于
    # 把玩家站哪张图写成这族本周在哪张图（12.1.5 反馈⑥的原样）。
    # 它原来挂的 2393 是**没有来源标签**的旧图号。第十五轮把这一步的口径改回来了：
    # 上一版把这类一起清成 nil，结果反馈②③"太阳的支持应该是属于永歌森林的，怎么改到
    # 别的地方了""具体地图的特别任务没有了，没有地图的几个特别任务反而还在" —— 整批行
    # 塌进底部"未知地图"。回看证据：三条谎报接口**都带自己的标签**，"没标签"推不出
    # "是谎报接口写的"，它只是旧版没来得及记标签的客户端答复。
    # 所以现在：留着图号 + 打标 `旧记录未标来源` + 每轮照旧重问（HealMissingMaps 认这个标签）。
    anchorMap = one(lua.execute("return tostring(GQLDB.quests[89507].mapID)"))
    if anchorMap != "2393":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "没来源标签的旧图号要留着等本轮证据（不许清成 nil 塌进未知地图，"
                    "更不许跟已停用的轮换指针走）：实际 " + anchorMap)
    if one(lua.execute("return tostring(GQLDB.quests[89507].mapSrc)")) != "旧记录未标来源":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "留着图号必须同时打上存疑标签，否则下一轮它会冒充成本轮客户端答复")
    # 同一份账本里**带来源**的上周记录（122870 的"上周的池行"）不许被这条规则误伤：
    # 它归零靠的是板面证据那道门，不是清洗
    if one(lua.execute("return tostring(GQLDB.quests[122870].mapID)")) != "2393":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "带来源标签的地图号不归这条清洗管（它该被板面证据那道门处理）：实际 "
                    + one(lua.execute("return tostring(GQLDB.quests[122870].mapID)")))

    ok1, msg = gql.Sync.HuntKeywordMaps()
    if not ok1:
        return fail("last_weeks_child_does_not_stand_in_for_this_week", "补扫该跑起来: " + str(msg))
    if "没有命中" not in str(msg) or "添加当前所在地图" not in str(msg):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "轮换指针摘掉后本周那张图不在跟踪清单里，补扫要如实说没命中并给出"
                    "「站到那张图上添加地图再补扫」这条路: " + str(msg))
    if lua.execute("return GQLDB.quests[137001] ~= nil"):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "「丰饶收获」是板上的轮换子任务，不是周常本体，不占行也不建注册行")
    listed = matrix_listed(lua, 1700000000)
    if "122870," in listed or "137001," in listed:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "补扫前后都不许多出行: " + listed)
    if "89507," not in listed:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "族本体这条要一直在: " + listed)
    fam = one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                          "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
                          "  if r.questID == 89507 then "
                          "    return tostring(grp.mapID) .. '/' .. tostring(r.name) "
                          "      .. '/' .. tostring(r.familyCount) end end end return 'none'"))
    # 没来源标签 → 留着并打标 → 行照旧挂在那张图上。第十五轮反馈②③要的就是这个：
    # 把它清成 nil 的代价是整批任务掉进"未知地图"，用户看到的比错图更糟
    if fam != "2393/丰饶供品/2":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "没有新证据时旧图号要留住，名字仍是族本体、族里算上上周那条: " + fam)
    if one(lua.execute("return tostring(GQLDB.quests[89507].expiresSrc)")) != "下次周重置":
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "族本体的寿命是本周 CD 尾巴，不是板上那个实例的 6 小时: "
                    + one(lua.execute("return tostring(GQLDB.quests[89507].expiresAt) .. '/' "
                                      ".. tostring(GQLDB.quests[89507].expiresSrc)")))
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "丰饶供品" not in joined or "银月城" not in joined:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    f"界面要出现族本体这一行，它还挂在旧图号那张图上（没被清成未知地图）:\n{joined}")
    if "未知地图" in joined:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "有旧图号的任务不许堆进底部未知地图组（第十五轮反馈②③的形状）:\n" + joined)
    for gone in ("辉耀丰饶", "祖阿曼", "丰饶收获"):
        if gone in joined:
            return fail("last_weeks_child_does_not_stand_in_for_this_week",
                        f"上周的名字/指针猜出来的图/板上子任务都不许出现在界面上（「{gone}」）:\n{joined}")

    # 反过来：本周那张图真的进了跟踪清单（玩家站到祖阿曼点过"＋添加当前所在地图"），
    # 板面证据一到位，锚点就跟着搬 —— 这才是"换区"唯一的合法通道
    gql.Config.AddMap(2437)
    lua.execute("GQLDB.quests[137001] = { name = '丰饶收获', mapID = 2437, "
                "  mapSrc = '池行自带', onBoard = true, observations = {} }")
    gql.Sync.SnapshotNow()
    fam = one(lua.execute("local db = GQL.Config.DB() "
                          "return tostring(db.quests[89507].mapID) .. '/' "
                          ".. tostring(db.quests[89507].mapSrc)"))
    if not (fam.startswith("2437/") and fam != "2437/nil"):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "本轮板面上的同族成员要把族本体带到本周那张图，并留下来源标记（只认成员"
                    "自己报的图/清单反查，指针猜的不算）: " + fam)
    if "GetMapForQuestPOIs" in fam:
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "锚点的图不许又回到已停用的轮换指针: " + fam)
    ok("last_weeks_child_does_not_stand_in_for_this_week")


def test_retired_map_sources_are_cleared_from_the_ledger():
    """摘掉一条撒谎的地图接口，它写进账本的地图号必须一起清掉：HealMissingMaps 只认
    "没地图或来源存疑"，不清就永远不会重问，用户下次开矩阵看到的还是"丰饶在屠戮者高地"
    （12.1.5 反馈⑥，那张图是 GetMapForQuestPOIs 按玩家脚下答的）。
    走真实入口 SnapshotNow，不直调 Ledger.ClearRetiredMaps —— 接线没人调正是往轮的 bug。
    可信来源（池行自带/清单反查）一条都不许碰：那些是客户端说"这任务就在这张图上"。
    另一类**只许标注、不许清**：只有图号、没有来源标签的记录。那是更早版本写进
    SavedVariables 的形状（当时还没记 mapSrc）。上一版把它一起清成 nil，第十五轮反馈②③
    当场炸了："太阳的支持应该是属于永歌森林的，怎么改到别的地方了""具体地图的特别任务
    没有了，没有地图的几个特别任务反而还在" —— 整批行塌进底部"未知地图"。
    回看证据：那三条谎报接口写的号**都带自己的标签**，"没标签"根本推不出"是谎报接口写的"，
    它只是旧版来不及记标签的客户端答复。现在留着图号 + 打标 `旧记录未标来源`，
    HealMissingMaps 每轮照旧重问，问得出就覆盖、问不出也不比原来更差。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    def seed(qid, mapID, mapSrc):
        # 带 lastSeenAt + observation 才活得过 Prune：否则"清掉了"其实是"清记录顺手删了"，
        # 这条用例就验不到 ClearRetiredMaps 那道门
        lua.execute("GQLDB.quests[%d] = { name = '测试%d', mapID = %d, %s"
                    "  lastSeenAt = 1699996400, currentSig = '1500000', lastMoney = 1500000, "
                    "  observations = { ['1500000'] = { gold = true, money = 1500000, "
                    "    firstSeen = 1699996400, lastSeen = 1699996400 } } }"
                    % (qid, qid % 1000, mapID,
                       "mapSrc = nil, " if mapSrc is None else "mapSrc = '%s', " % mapSrc))

    seed(777001, 2444, "补问 C_QuestLog.GetMapForQuestPOIs")
    # 旧版本没写"补问 "前缀时也按接口名子串认，否则老 SavedVariables 清不干净
    seed(777002, 2696, "C_TaskQuest.GetQuestInfoByQuestID")
    seed(777003, 2393, "池行自带")
    seed(777004, 2395, None)
    # 空字符串来源同样算没标
    seed(777005, 2395, "")
    gql.Sync.SnapshotNow()

    def mapof(qid):
        return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                               "return tostring(r and r.mapID) .. '/' .. tostring(r and r.mapSrc)" % qid))

    if not lua.execute("return GQL.Config.DB().quests[777003] ~= nil"):
        return fail("retired_map_sources_are_cleared_from_the_ledger",
                    "测试记录都被 Prune 清掉了，这条用例验不到清理那道门")
    # 问不出的就留 nil，矩阵落"未知地图"，不许拿脚下那张顶
    if not mapof(777001).startswith("nil") or not mapof(777002).startswith("nil"):
        return fail("retired_map_sources_are_cleared_from_the_ledger",
                    "已停用接口写进来的地图号要清成 nil: " + mapof(777001) + " " + mapof(777002))
    if mapof(777003) != "2393/池行自带":
        return fail("retired_map_sources_are_cleared_from_the_ledger",
                    "可信来源的那条不许被顺手清掉: " + mapof(777003))
    for qid in (777004, 777005):
        if mapof(qid) != "2395/旧记录未标来源":
            return fail("retired_map_sources_are_cleared_from_the_ledger",
                        f"只有图号、没有来源标签的旧记录要留着并打存疑标签（{qid}）: {mapof(qid)}")
    ok("retired_map_sources_are_cleared_from_the_ledger")


def test_a_rotated_off_board_quest_keeps_its_map():
    """换轮换出板的世界任务不许塌进"未知地图"（12.1.5 反馈："任务所属地图乱了……
    为什么他们没有了所属地图"，12 行整批堆在底部）。
    形状是用户回传的 /gql quest 幽魂角斗士(96625)：一天前还在池里、池行自己不报地图、
    逐条问的两条入口在本机一条 MISSING 一条答 nil，那个图号正是上一版还挂在链里的
    GetMapForQuestPOIs 补出来的。第十五轮"按来源标签回溯清图"把这种行也清成 nil，
    可它一旦离板，板面三条来源（池 / 跟踪清单 / 清单反查）只认**当下还在板上**的任务，
    那份图号就永远补不回来 —— 于是"清掉谎报"直接等于"整批行无家可归"。
    现在：真在板上出现过（poolSeen）的行只降级标签、不清图，HealMissingMaps 每轮照旧重问。
    压根没上过板的（丰饶 89507 那种注册周常）照旧清成 nil —— 那条图是接口按玩家脚下猜的，
    留着就是第十五轮反馈⑥"丰饶出现在了屠戮者高地"。
    走真实入口 SnapshotNow，不直调 Ledger.ClearRetiredMaps。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000

    def seed(qid, pool_seen):
        lua.execute("GQLDB.quests[%d] = { name = '测试%d', mapID = 2512,"
                    "  mapSrc = '补问 C_QuestLog.GetMapForQuestPOIs', poolSeen = %s,"
                    "  lastSeenAt = 1699996400, currentSig = '10909760', lastMoney = 10909760,"
                    "  observations = { ['10909760'] = { gold = true, money = 10909760,"
                    "    firstSeen = 1699996400, lastSeen = 1699996400 } } }"
                    % (qid, qid % 1000, "true" if pool_seen else "nil"))

    seed(777101, True)     # 上过板的世界任务：图号要留住
    seed(777102, False)    # 从没上过板：照旧清成 nil
    # 关键词这条绕开金币门槛：本机金额接口答不出（L.QuestMoney 拿不到数），
    # 不加这一条就验不到"分组"那道门，只会验到"根本没列进行"
    gql.Config.AddKeyword("测试")
    gql.Sync.SnapshotNow()

    def mapof(qid):
        return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                               "return tostring(r and r.mapID) .. '/' .. tostring(r and r.mapSrc)" % qid))

    if not lua.execute("return GQL.Config.DB().quests[777101] ~= nil"):
        return fail("a_rotated_off_board_quest_keeps_its_map", "测试记录被 Prune 清掉了，验不到清理那道门")
    if "存疑" not in mapof(777101) or not mapof(777101).startswith("2512/"):
        return fail("a_rotated_off_board_quest_keeps_its_map",
                    "上过板的那条要留图号 + 打存疑标签（每轮照旧重问）: " + mapof(777101))
    if not mapof(777102).startswith("nil"):
        return fail("a_rotated_off_board_quest_keeps_its_map",
                    "没上过板的行仍要清成 nil，否则丰饶那种脚下图又留下来: " + mapof(777102))
    grp = one(lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  if r.questID == 777101 then return tostring(g.mapID) end end end return 'none'"))
    if grp != "2512":
        return fail("a_rotated_off_board_quest_keeps_its_map",
                    "矩阵要把这条归回它原来那张图，而不是底部 -1 未知地图组: " + grp)
    ok("a_rotated_off_board_quest_keeps_its_map")


def test_hidden_log_quests_are_not_collected():
    """isHidden=true 的日志行是客户端内部行，不许被关键词收进矩阵。
    12.1.5 实测 82157「特别任务：巨兽崛起」：isHidden=true、isOnMap=false、
    玩家本周根本没有它，却因为命中关键词「特别任务」被从日志收进行，
    每轮还按周重置续命（dump："倒计时：还剩 4天20时（来源=下次周重置）"）、
    全角色状态 "?"。用户："蓝色2个特别任务不应该出现 本周没有"。
    上一版只滤分组头 isHeader。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_QuestLog.GetInfo = lambda i: (
        lua.table_from({"questID": 82157, "title": "特别任务：巨兽崛起", "isHeader": False,
                        "isHidden": True, "questLogIndex": 40}) if int(i) == 1 else
        lua.table_from({"questID": 82158, "title": "特别任务：神盾密探", "isHeader": False,
                        "isHidden": False, "questLogIndex": 41}) if int(i) == 2 else None)
    gql.Config.AddKeyword("特别任务")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[82157] ~= nil"):
        return fail("hidden_log_quests_are_not_collected",
                    "isHidden 的内部行不该被关键词从日志收进账本")
    if not lua.execute("return GQLDB.quests[82158] ~= nil"):
        return fail("hidden_log_quests_are_not_collected",
                    "普通那条日志任务照收：别把过滤写成一律不收")
    ok("hidden_log_quests_are_not_collected")


def test_log_zone_map_gives_an_accepted_quest_its_map():
    """已接任务的归属图走日志口径 C_QuestLog.GetQuestLogZoneMapID(日志索引)。
    特别任务/战役任务不上世界任务板，板面三条来源对它全答不出，逐条问的两条
    在本机一条 MISSING 一条 nil（12.1.5 实测 82157：扫了 12 张跟踪图也没有它）——
    只剩"问日志自己"这一条路。
    这个入口**在 12.x 存不存在还没实测**：它不答时必须什么都不写，
    绝不拿玩家脚下那张图顶（GetMapForQuestPOIs 就是栽在这儿，第十五轮反馈⑥）。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_QuestLog.GetInfo = lambda i: (
        lua.table_from({"questID": 82158, "title": "特别任务：神盾密探", "isHeader": False,
                        "questLogIndex": 41}) if int(i) == 1 else None)
    g.C_QuestLog.GetLogIndexForQuestID = lambda qid: 41 if int(qid) == 82158 else None
    g.C_QuestLog.GetQuestLogZoneMapID = lambda idx: 2248 if int(idx) == 41 else None
    gql.Config.AddKeyword("特别任务")
    gql.Sync.SnapshotNow()

    def mapof(qid):
        return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                               "return tostring(r and r.mapID) .. '/' .. tostring(r and r.mapSrc)" % qid))

    if mapof(82158) != "2248/日志分区":
        return fail("log_zone_map_gives_an_accepted_quest_its_map",
                    "日志答得出的分区图要落进账本并留下来源名: " + mapof(82158))
    # 客户端不答这条（本机现在就是未知）：什么都不写，不许编一张
    g.C_QuestLog.GetQuestLogZoneMapID = None
    lua.execute("GQLDB.quests[82158].mapID, GQLDB.quests[82158].mapSrc = nil, nil")
    gql.Sync.SnapshotNow()
    if not mapof(82158).startswith("nil"):
        return fail("log_zone_map_gives_an_accepted_quest_its_map",
                    "问不出地图就留 nil（落未知地图），不许拿别的图号占位: " + mapof(82158))
    ok("log_zone_map_gives_an_accepted_quest_its_map")


def test_bounty_board_gives_special_assignments_their_map():
    """MapQuestIndex 并上悬赏清单：C_QuestLog.GetBountiesForMapID(图) 的行本身就写着
    "这条悬赏挂在这张图上"（12.1.5 实测行带 questID，见 Probe.BountyIDsForMap 那份取证实现）。
    特别任务不上世界任务板，板面反查对它永远是空 → 一整批"特别任务：X"堆在底部未知地图
    （用户："几个灰色的特别任务属于各自的地图"）。
    来源标签必须分开报：`悬赏清单反查` 与 `地图任务清单反查` 是两条不同凭据，
    混成一个就没法复盘"这张图到底是谁说的"。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from(
            {1: lua.table_from({"questID": 501, "mapID": 2248})}) if int(mid) == 2248
            else lua.table_from({}),
    })
    g.C_QuestLog.GetBountiesForMapID = lambda mid: lua.table_from(
        {1: lua.table_from({"questID": 82158})}) if int(mid) == 2444 else lua.table_from({})
    got = one(lua.execute(
        "local i, _, s = GQL.Api.MapQuestIndex({ 2248, 2444 }) "
        "return tostring(i[501]) .. '/' .. tostring(s and s[501]) .. '/' "
        ".. tostring(i[82158]) .. '/' .. tostring(s and s[82158])"))
    if got != "2248/地图任务清单反查/2444/悬赏清单反查":
        return fail("bounty_board_gives_special_assignments_their_map",
                    "板面行按行自带地图、悬赏行按询问图，两条来源分开标: " + got)
    # 每张图回**同一批**悬赏 = 这份清单根本不按图区分（第十二轮实测就是这个形状）：
    # 整批作废，一条都不许归图 —— 归错组比没图更糟（"好几个特别任务都跑到了盘卷蛇岛"）
    g.C_QuestLog.GetBountiesForMapID = lambda mid: lua.table_from(
        {1: lua.table_from({"questID": 82158}), 2: lua.table_from({"questID": 82999})})
    got3 = one(lua.execute(
        "local i, _, s = GQL.Api.MapQuestIndex({ 2248, 2444 }) "
        "return tostring(i[82158]) .. '/' .. tostring(i[82999]) .. '/' "
        ".. tostring(s and s[82158]) .. '/' .. tostring(s and s[501])"))
    if got3 != "nil/nil/nil/地图任务清单反查":
        return fail("bounty_board_gives_special_assignments_their_map",
                    "不按图区分的悬赏答案要整批否掉，板面那份凭据不受影响: " + got3)
    # 悬赏入口缺失（本机到底有没有还没实测）：整张索引照常可用，不许顺手把板面那份判成问不出
    g.C_QuestLog.GetBountiesForMapID = None
    got2 = one(lua.execute(
        "local i, _, s = GQL.Api.MapQuestIndex({ 2248 }) "
        "return tostring(i[501]) .. '/' .. tostring(i[82158])"))
    if got2 != "2248/nil":
        return fail("bounty_board_gives_special_assignments_their_map",
                    "没有悬赏入口时只少这一类凭据，板面那份照旧: " + got2)
    ok("bounty_board_gives_special_assignments_their_map")


def test_registered_weekly_map_source_is_labelled_like_the_others():
    """注册周常自己问出来的图，来源标签要和别的补问路径同形状（`补问 <接口>`）。
    Ledger.PropagateFamilyMaps 的可信白名单认的就是这个带前缀的名字，上一版
    RefreshRegistered 把裸接口名直接落进 rec.mapSrc，于是 加固符文石(90573) 那条
    既不被族搬运当可信凭据、也不在清理名单里 —— 用户看到的仍是"它没有所属地图"。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    lua.execute("table.insert(GQL.Api.WEEKLY_PRESETS, { key = 'fortify-label-test', ids = { 90573 } })")
    g.C_TaskQuest = lua.table_from({"GetQuestZoneID": lambda qid: 2248})
    gql.Sync.SnapshotNow()
    got = one(lua.execute("local r = GQLDB.quests[90573] "
                          "return tostring(r and r.mapID) .. '/' .. tostring(r and r.mapSrc)"))
    if got != "2248/补问 C_TaskQuest.GetQuestZoneID":
        return fail("registered_weekly_map_source_is_labelled_like_the_others",
                    "来源标签要和 HealMissingMaps/DiffPool 同形状，族白名单才认: " + got)
    ok("registered_weekly_map_source_is_labelled_like_the_others")


def test_probe_prints_every_field_of_a_row():
    """取证段摊**全**字段：上一版 FlatDescribe 截到 16 个键，而 12.1.5 那条日志行是
    table[#25] —— 我们要找的地图凭据（uiMapChartValue / areaName / zone 一类）
    很可能就在被截掉的 9 个里。截断等于把下一轮唯一的线索自己扔了。
    顺带把新接的日志分区口径与悬赏反查口径也报出来，取证与实现必须同一批入口。"""
    lua, gql, _ = sync_env([(88, 30000000)], extra=("Probe",))
    g = lua.globals()
    row = {"questID": 88, "title": "追踪萨拉塔斯", "isHeader": False}
    for i in range(1, 21):
        row["f%02d" % i] = i
    g.C_QuestLog.GetInfo = lambda i: lua.table_from(row) if int(i) == 1 else None
    g.C_QuestLog.GetLogIndexForQuestID = lambda qid: 1
    text = "\n".join(str(gql.Probe.QuestEvidence(88)[i]) for i in range(1, 200))
    missing = ["f%02d=20(number)" % 20] + ["f%02d=" % i for i in range(1, 21)]
    for want in missing:
        if want not in text:
            return fail("probe_prints_every_field_of_a_row",
                        f"字段「{want}」没被打出来（截断就没法找地图凭据）:\n{text[:2000]}")
    if "table[#23]" not in text:
        return fail("probe_prints_every_field_of_a_row",
                    "总数要报真实字段数（3 个基础 + 20 个 = 23）:\n" + text[:2000])
    if "C_QuestLog.GetQuestLogZoneMapID" not in text:
        return fail("probe_prints_every_field_of_a_row",
                    "日志分区口径要单独一行报，答不答得出决定已接任务有没有救:\n" + text[:2000])
    if "GetBountiesForMapID" not in text:
        return fail("probe_prints_every_field_of_a_row",
                    "按图反查段要连悬赏清单一起问，否则报告与实现两套:\n" + text[:2000])
    # 这条凭据到底能不能用，取决于它按不按图区分 —— 报告要自己把这个形状算给玩家看
    # （生产侧 MapQuestIndex 那道跨图自检认的是同一个形状，两处不许两套口径）
    lua.execute("GQL.Config.TrackedMapIDs = function() return { 2248, 2444 } end")
    g.C_QuestLog.GetBountiesForMapID = lambda mid: lua.table_from({1: lua.table_from({"questID": 88})})
    text2 = "\n".join(str(gql.Probe.QuestEvidence(88)[i]) for i in range(1, 200))
    if "不按图区分" not in text2:
        return fail("probe_prints_every_field_of_a_row",
                    "逐张图回同一批悬赏时要直说这条凭据本轮作废:\n" + text2[-1200:])
    ok("probe_prints_every_field_of_a_row")


def test_the_player_can_fix_maps_the_client_cannot_answer():
    """玩家口述的地图要能落进账本（第十七轮原话："我知道每个任务属于哪个地图"）。
    这批任务本机就是问不出地图：换轮换出板的世界任务，板面三条来源只认当下还在板上的；
    压根不上板的特别任务，逐条问的两条 MISSING 或答 nil。上一版清图又把 SavedVariables 里
    唯一的旧答案清了 —— **接口本周没有出路**，不给这条路，用户就只能看着一屏"未知地图"。
    表里只准写任务名/图名（questID 与地图号是我们内部的东西，不许问玩家要），
    真凭据（池行自带）一律压不过；改了表下一轮重配。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    names = {2512: "盘卷蛇岛", 2248: "永歌森林"}
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: lua.table_from(
        {"name": names.get(int(mid))})})
    lua.execute("GQL.Config.TrackedMapIDs = function() return {2512, 2248} end")
    lua.execute("GQL.Api.MAP_MEMORY = { { name = '幽魂角斗士', map = '盘卷蛇岛' },"
                "  { name = '太阳的支持', map = '永歌森林' } }")
    lua.execute("GQLDB.quests[96625] = { name = '幽魂角斗士', mapID = nil, mapSrc = nil,"
                "  poolSeen = true, lastSeenAt = 1699996400, currentSig = '10909760',"
                "  lastMoney = 10909760, observations = { ['10909760'] = { gold = true,"
                "    money = 10909760, firstSeen = 1699996400, lastSeen = 1699996400 } } }")
    # 这条有真凭据（池行自带），只是图不一样：玩家的话压不过客户端本轮直答
    lua.execute("GQLDB.quests[96626] = { name = '太阳的支持', mapID = 2393, mapSrc = '池行自带',"
                "  poolSeen = true, lastSeenAt = 1699996400, currentSig = '10909760',"
                "  lastMoney = 10909760, observations = { ['10909760'] = { gold = true,"
                "    money = 10909760, firstSeen = 1699996400, lastSeen = 1699996400 } } }")
    gql.Config.AddKeyword("角斗士")    # 绕开金币门槛（本机金额接口答不出），两条都要能进矩阵对比
    gql.Config.AddKeyword("支持")
    gql.Sync.SnapshotNow()

    def mapof(qid):
        return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                               "return tostring(r and r.mapID) .. '/' .. tostring(r and r.mapSrc)" % qid))

    if mapof(96625) != "2512/玩家指定":
        return fail("the_player_can_fix_maps_the_client_cannot_answer",
                    "问不出地图的那条要按玩家口述落图并留来源: " + mapof(96625))
    if mapof(96626) != "2393/池行自带":
        return fail("the_player_can_fix_maps_the_client_cannot_answer",
                    "客户端本轮直答的凭据不许被玩家的话压过: " + mapof(96626))
    grp = one(lua.execute(
        "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
        "for i, g in ipairs(out) do for j, r in ipairs(g.rows) do "
        "  if r.questID == 96625 then return tostring(g.mapID) end end end return 'none'"))
    if grp != "2512":
        return fail("the_player_can_fix_maps_the_client_cannot_answer",
                    "补上地图的行要离开底部未知地图组: " + grp)
    # 改表 = 下一轮重配（这张表是"玩家指定"这一类唯一的编辑入口，不叫玩家动 SavedVariables）
    lua.execute("GQL.Api.MAP_MEMORY = { { name = '幽魂角斗士', map = '永歌森林' } }")
    gql.Sync.SnapshotNow()
    if mapof(96625) != "2248/玩家指定":
        return fail("the_player_can_fix_maps_the_client_cannot_answer",
                    "表里换了图，下一轮要跟着重配: " + mapof(96625))
    ok("the_player_can_fix_maps_the_client_cannot_answer")


def test_map_memory_says_what_it_could_not_apply():
    """配不上要**点名说清缺哪一环**，不能静默：这张表靠用户回传的名字填，
    名字被矩阵截断、或那张图没在跟踪清单里，都是"我改了没生效"，没有日志就是死无对证。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: lua.table_from(
        {"name": "盘卷蛇岛" if int(mid) == 2512 else None})})
    lua.execute("GQL.Config.TrackedMapIDs = function() return {2512} end")
    lua.execute("GQL.Api.MAP_MEMORY = { { name = '漫天烈火', map = '虚影风暴' },"
                "  { name = '根本没有这个任务', map = '盘卷蛇岛' } }")
    lua.execute("GQLDB.quests[96700] = { name = '漫天烈火', mapID = nil,"
                "  lastSeenAt = 1699996400, currentSig = '10909760', lastMoney = 10909760,"
                "  observations = { ['10909760'] = { gold = true, money = 10909760,"
                "    firstSeen = 1699996400, lastSeen = 1699996400 } } }")
    gql.Sync.SnapshotNow()
    logs = one(lua.execute("local d = GQL.Config.DB() local s = ''"
                           " for _, m in ipairs((d and d.logs) or {}) do s = s .. m .. '|' end return s"))
    if "不在跟踪清单" not in logs:
        return fail("map_memory_says_what_it_could_not_apply",
                    "图名不在跟踪清单要点名（并给出\"添加当前所在地图\"这条路）:\n" + logs)
    if "账本里没有叫" not in logs:
        return fail("map_memory_says_what_it_could_not_apply",
                    "账本里没这个名字也要点名（矩阵截断名字时靠这句核对）:\n" + logs)
    if one(lua.execute("return tostring(GQLDB.quests[96700].mapID)")) != "nil":
        return fail("map_memory_says_what_it_could_not_apply", "配不上就不许写图号")
    ok("map_memory_says_what_it_could_not_apply")


def test_an_ignore_outlives_every_cycle_and_round():
    """忽略 = **永久屏蔽**（用户 2026-10-27 原话："我最开始 忽略 任务的意图 就是想把
    关键词匹配到了我不想要的任务，比如丰饶关键词，匹配到了辉煌丰饶，但是辉煌丰饶不是世界
    任务，我要屏蔽掉，那么我这个意图其实是想永久屏蔽的"）。
    上一版（§25，2026-10-24 那句"如果任务下个 CD 是金币任务，虽然忽略了也应该自动回来"）
    把它做成"只管本 CD"—— 那是理解错了玩家的意图：他要的是"这条一辈子别上板"，
    不是"这周先藏一下、下周插件自己决定放不放回来"。本轮改回永久：换 CD、换轮都不作废，
    只有玩家自己在"添加任务"里点回来才撤。
    四段都要验，走真实入口 SnapshotNow（不直调清理函数）：
    ① 同一个 CD 内照旧压着（第十二轮"右键之后还会刷出来"要的就是这个，原断言留着）；
    ② 锚点退回上周 = 跨过周重置点换了 CD，屏蔽还得压着 —— 哪怕它 30000g 远超门槛、
       又稳稳挂在池里（上一版正是这一步自动解除的）；
    ③ 再把这一条实例换轮（接口倒计时整段挪走 = 板上换成另一条），一样不许自己回来；
    ④ 取消忽略走真实入口 AddQuest：只撤否决。撤完不当场上板（③ 换轮把本轮金币凭据
       作废了，五道门一道不成立），等下一次快照在这一轮重新读到门槛金币才自己回来。
    日志里不许再出现"换了 CD：解除"那句 —— 那句话本身就是上一版误读的产物。"""
    name = "an_ignore_outlives_every_cycle_and_round"
    lua, gql, _ = sync_env([(180, 30000000)], weekly=300000)
    g = lua.globals()
    clock = {"v": 1700000000}
    g.time = lambda: clock["v"]
    secs = {"v": 43200}
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: secs["v"]})
    anchor = 1700000000 + 300000 - 604800        # RefreshCycle 本轮会算出的锚点
    gql.Sync.SnapshotNow()
    if not bool(gql.Ledger.RemoveQuest(gql.Config.DB(), 180)[0]):
        return fail(name, "RemoveQuest 该成功")

    def ignored():
        return one(lua.execute("return tostring(GQLDB.quests[180] and GQLDB.quests[180].ignored)"))

    def logs():
        return one(lua.execute("local s = '' for _, m in ipairs((GQL.Config.DB().logs) or {}) do"
                               " s = s .. m .. '|' end return s"))

    # 永久意图存的是 true，不是时间戳：留个数字会被读成"到期时刻"，可它根本没有到期
    if ignored() != "true":
        return fail(name, f"移除要落成永久屏蔽标记 true，实际 {ignored()}")
    lua.execute("GQLDB.cycleAnchor = %d" % anchor)
    gql.Sync.SnapshotNow()
    if ignored() == "nil":
        return fail(name, "同一个 CD 内不许自己解除屏蔽")
    # 金额 30000g 远超门槛、又在池里：这一轮它**不列**才是"屏蔽生效"（第十二轮那条反馈）
    if "180," in matrix_listed(lua):
        return fail(name, "本 CD 内被屏蔽的行不许列: " + matrix_listed(lua))

    # ② 换 CD：锚点退回上周，本轮快照跨过周重置点（上一版就是在这里把屏蔽解除的）
    lua.execute("GQLDB.cycleAnchor = %d" % (anchor - 604800))
    gql.Sync.SnapshotNow()
    if ignored() == "nil":
        return fail(name, "换了 CD 就把永久屏蔽解除了 —— 玩家点的是「辉煌丰饶一辈子"
                          "别上板」，不是「这周先藏一下」:\n" + logs())
    if "180," in matrix_listed(lua):
        return fail(name, "换 CD 后这条 30000g、又在池里的行不许自己回来: "
                          + matrix_listed(lua))
    if "换了 CD：解除" in logs():
        return fail(name, "日志里不许再有\"换了 CD：解除\"那句（上一版的误读）:\n" + logs())

    # ③ 换轮：接口倒计时整段挪走 = 板上换成下一条实例。RollRound 作废的是"本轮凭据"
    #    （金币粘滞），不许顺手把玩家那句永久屏蔽一起清掉
    clock["v"] = 1700003600
    before = one(lua.execute("return tostring(GQLDB.quests[180].roundAt)"))
    secs["v"] = 36000                     # 结束时刻差 3600 秒 > 900 秒容差 = 换了一条实例
    gql.Sync.SnapshotNow()
    after = one(lua.execute("return tostring(GQLDB.quests[180].roundAt)"))
    if after == before:
        return fail(name, f"前置：这一条实例该换成下一条了（roundAt 没动，{before}）")
    if ignored() == "nil":
        return fail(name, "换轮清了屏蔽 —— 金币粘滞作废是一回事，玩家那句\"别再显示\"是另一回事")
    if "180," in matrix_listed(lua):
        return fail(name, "换轮后被屏蔽的行不许回来: " + matrix_listed(lua))

    # ④ 取消屏蔽走真实入口：AddQuest 只撤否决。撤完这一行**不会**当场回来 ——
    #    ③ 那次换轮已经把本轮金币凭据（goldRound）作废，五道自动门此刻一道都不成立。
    #    这正是第二十七轮要的语义：点"添加"是撤销"别再显示"，不是"标记这条上板"
    #    （上一版在这里写 manualAdded，行立刻凭玩家那一戳回来了）。
    lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), 180, 1700004000, nil)")
    if ignored() != "nil":
        return fail(name, f"点\"添加任务\"要撤销永久屏蔽，实际 {ignored()}")
    if "180," in matrix_listed(lua):
        return fail(name, "取消忽略只撤否决，不许把没有本轮凭据的行当场放上板: "
                          + matrix_listed(lua))
    # 下一次快照在这一轮重新读到 30000g（自动凭据长回来）→ 行按金币门自己回到矩阵
    gql.Sync.SnapshotNow()
    if "180," not in matrix_listed(lua):
        return fail(name, "重新读到本轮门槛金币后该行要自动回到矩阵（取消忽略=撤否决，"
                          "上板由判据重算）: " + matrix_listed(lua))
    ok(name)


def test_map_memory_fills_every_row_sharing_a_name():
    """账本里**同名几条**是常态（一族多 questID、轮换出的新旧两份都留在账本里）。
    口述表按名字配，只配"第一条"= 看运气：`pairs()` 顺序随机，第十八轮实测就是这个形状 ——
    日志一句没抱怨（说明配上了一条），矩阵里那一行照旧躺在"未知地图"（补到的是没显示的那条）。
    所以：**同名有几条弱记录就补几条**，有真凭据的那条照旧压不过。"""
    lua, gql, _ = sync_env([(180, 30000000)])
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_Map = lua.table_from({"GetMapInfo": lambda mid: lua.table_from(
        {"name": "盘卷蛇岛" if int(mid) == 2512 else None})})
    lua.execute("GQL.Config.TrackedMapIDs = function() return {2512} end")
    lua.execute("GQL.Api.MAP_MEMORY = { { name = '特别任务：怨灵之怒', map = '盘卷蛇岛' } }")
    # 两条同名记录：一条图号 nil，一条带着 `旧记录未标来源` 的旧图号 —— 两条都算"没落实"
    lua.execute("GQLDB.quests[96800] = { name = '特别任务：怨灵之怒', mapID = nil, mapSrc = nil,"
                "  lastSeenAt = 1699996400 }")
    lua.execute("GQLDB.quests[96801] = { name = '特别任务：怨灵之怒', mapID = 1234,"
                "  mapSrc = '旧记录未标来源', lastSeenAt = 1699996400 }")
    # 这条有真凭据：玩家的话不许压过客户端本轮直答（同名也不许）
    lua.execute("GQLDB.quests[96802] = { name = '特别任务：怨灵之怒', mapID = 2393,"
                "  mapSrc = '池行自带', lastSeenAt = 1699996400 }")
    gql.Sync.SnapshotNow()
    got = [one(lua.execute("local r = GQL.Config.DB().quests[%d]"
                           " return tostring(r.mapID) .. '/' .. tostring(r.mapSrc)" % qid))
           for qid in (96800, 96801, 96802)]
    if got[0] != "2512/玩家指定" or got[1] != "2512/玩家指定":
        return fail("map_memory_fills_every_row_sharing_a_name",
                    "同名弱记录要一条条都补上（只补一条 = 看 pairs 顺序运气）: " + "  ".join(got))
    if got[2] != "2393/池行自带":
        return fail("map_memory_fills_every_row_sharing_a_name",
                    "有真凭据的那条不许被玩家的话压过: " + got[2])
    ok("map_memory_fills_every_row_sharing_a_name")


def test_shipped_map_memory_names_a_real_zone():
    """`Api.MAP_MEMORY` 是**手抄的用户口述**，没有任何接口会校验图名。
    写错一个字（"哈籁恩达尔"→"哈籁恩答尔"）的下场是：ApplyMapMemory 配不上 → 日志点名 →
    行照旧躺在"未知地图"，而用户以为补过了。图名必须在 `MIDNIGHT_ZONE_NAMES` 里：
    渲染层把"确定不是本资料片"的整组滤掉（`Api.IsMidnightMap` 返回 false），
    指着清单外的图 = 这条要么配不上、要么配上了也永远不显示。
    同一条任务在表里只许出现一次：`玩家指定` 那类按表重配，重复条目是**后一条覆盖前一条**，
    想给"银月城"写个"退到永歌森林"的兜底是行不通的 —— 只会永远落在最后那条。"""
    with open(os.path.join(PLUGIN_DIR, "Api.lua"), encoding="utf-8") as fh:
        src = fh.read()
    zone_block = re.search(r"local MIDNIGHT_ZONE_NAMES = \{(.*?)\n\}", src, re.S)
    mem_block = re.search(r"Api\.MAP_MEMORY = \{(.*?)\n\}", src, re.S)
    if not zone_block or not mem_block:
        return fail("shipped_map_memory_names_a_real_zone",
                    "两张表都要能静态读到（改了写法就同步改这条用例）")
    zones = set(re.findall(r"\[\"([^\"]+)\"\]\s*=\s*true", zone_block.group(1)))
    entries = re.findall(r"\{([^{}]*)\}", mem_block.group(1))
    if not entries:
        return fail("shipped_map_memory_names_a_real_zone", "表读空了 = 正则和写法脱钩了")
    seen = set()
    for raw in entries:
        body = raw.split("--")[0]                      # 行尾注释不参与判重
        who = re.search(r"(?:name|questID)\s*=\s*\"?([^\",]+)\"?", body)
        where = re.search(r"map\s*=\s*\"([^\"]+)\"", body)
        if not who or not where:
            return fail("shipped_map_memory_names_a_real_zone",
                        "每条都要有 name 或 questID，外加 map：" + raw.strip())
        if where.group(1) not in zones:
            return fail("shipped_map_memory_names_a_real_zone",
                        "「%s」的图「%s」不在 MIDNIGHT_ZONE_NAMES 里" % (who.group(1), where.group(1)))
        if who.group(1) in seen:
            return fail("shipped_map_memory_names_a_real_zone",
                        "「%s」在表里出现两次，后一条会静默盖掉前一条" % who.group(1))
        seen.add(who.group(1))
    ok("shipped_map_memory_names_a_real_zone")


def test_pool_rotation_auto_rescans_tracked_maps():
    """板子一换就得重扫跟踪地图：轮换任务换了 questID，族里只存着老 ID 的话，
    新 ID 的名字/地图/倒计时永远进不了这一行，玩家还得自己回设置点"补扫"。
    SavedInstances 的办法是让维护者每赛季手抄新 ID（Progress.lua 那张预设表），
    我们没有这条路 —— 玩家不可能知道 questID。
    走真实入口 SnapshotNow，不直调 HuntKeywordMaps：接线没人调正是上一轮的 bug。"""
    lua, gql, _ = sync_env([], weekly=432000, reset=1700060480, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    pool, board = [124], [124]
    lua.execute("GQL.Api.MidnightMapIDs = function() return {2007} end")
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({
        i: lua.table_from({"questID": q, "mapID": 2007, "name": "任务%d" % q,
                           "money": 1000000})
        for i, q in enumerate(pool, 1)})})
    g.C_TaskQuest = lua.table_from({
        "GetQuestsOnMap": lambda mid: lua.table_from({
            i: lua.table_from({"questID": q, "mapID": 619})
            for i, q in enumerate(board, 1)}),
        "GetQuestInfoByQuestID": lambda qid: (
            ("虚空侵袭：东部", 619) if int(qid) == 124
            else ("虚空侵袭：西部", 619) if int(qid) == 125 else None),
        "GetQuestTimeLeftSeconds": lambda qid: 7200,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    gql.Config.AddKeyword("虚空侵袭")

    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey or ''")) != "kw:虚空侵袭":
        return fail("pool_rotation_auto_rescans_tracked_maps",
                    "登录第一轮快照就该自动补扫，不该等玩家去点按钮")
    hunts = str(gql.Config.GetLogs()).count("池已轮换")
    if hunts != 1:
        return fail("pool_rotation_auto_rescans_tracked_maps",
                    f"同一块板子只扫一次（自动补扫 {hunts} 次）")

    # 换轮：老 ID 下板、新 ID 上板 —— 谁都不许再点一下才认得出新任务
    pool[:] = [125]
    board[:] = [125]
    gql.Sync.SnapshotNow()
    if one(lua.execute("return GQLDB.quests[125] and GQLDB.quests[125].regKey or ''")) != "kw:虚空侵袭":
        return fail("pool_rotation_auto_rescans_tracked_maps",
                    f"换轮后新 ID 要自动进族：{matrix_listed(lua)}")
    if str(gql.Config.GetLogs()).count("池已轮换") != 2:
        return fail("pool_rotation_auto_rescans_tracked_maps", "换轮要再扫一次")

    # 板子没变就不许再扫：补扫是每张跟踪地图一次接口调用，重复扫等于白烧
    gql.Sync.SnapshotNow()
    if str(gql.Config.GetLogs()).count("池已轮换") != 2:
        return fail("pool_rotation_auto_rescans_tracked_maps",
                    "池子没轮换不许重复补扫")
    ok("pool_rotation_auto_rescans_tracked_maps")





def test_weekly_countdown_must_fit_the_anchor():
    """周口径自检：CD 锚点是周四 07:00（12.1.5 实测 cycleAnchor=1792623599），
    同一个"周"接口周五晚上却报出 3 时2分 —— 那是每日档冒充周档，照它写
    "距下次周重置"就是谎报（上一版 12 时 6 分那次同类错）。对不上格子的整条按
    不知道处理：宁可不上色、不报剩余，也不给玩家一个假 CD 尾巴。"""
    lua, gql, _ = sync_env([], weekly=432000, reset=1700060480)
    g = lua.globals()
    g.time = lambda: 1700000000
    gql.Sync.SnapshotNow()
    if lua.execute("return GQL.Config.NextCycleEnd(1700000000) == nil"):
        return fail("weekly_countdown_must_fit_the_anchor",
                    "锚点与倒计时自洽时该给出下次结束时刻")
    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: 11170})
    if not lua.execute("return GQL.Config.NextCycleEnd(1700000000) == nil"):
        return fail("weekly_countdown_must_fit_the_anchor",
                    "报出的下次结束不在锚点 + 整周的格子上 = 不是周口径，要判不知道")
    # 判不知道之后不许退回每日档冒充周：只能写排程值并标 guessed（不上色、不报剩余）
    lua.execute("GQL_TMP = {} GQL.Ledger.SetCycleExpiry(GQL_TMP, 1700000000)")
    if one(lua.execute("return GQL_TMP.expiresSrc or ''")) != "排程值":
        return fail("weekly_countdown_must_fit_the_anchor",
                    "周口径不可信时到期只能标排程值，实际 "
                    + one(lua.execute("return GQL_TMP.expiresSrc or ''")))
    if not lua.execute("return GQL_TMP.expiresGuessed"):
        return fail("weekly_countdown_must_fit_the_anchor", "排程值必须标 guessed，界面才不上色")
    ok("weekly_countdown_must_fit_the_anchor")


def test_dump_shows_the_weekly_family_section():
    """用户回传的 /gql dump 里根本没有丰饶供品这一行 —— 金币任务那节按金额筛，
    它金额问不出就被整条滤掉，最该取证的那条永远取不到证。
    现在家族单独一节，成员逐条摊开"占不占行"（一族一行：行由族本体占，板上那条同名
    子任务只当证据），每角色那一格问全族，外加周口径的原始证据一行。
    另验一件事：全文只进日志窗，chat 只报一句指路（12.1.5 实测"dump 太长了，
    chat 框放不下"，用户只能一张张贴图回传）。"""
    lua, gql, _ = sync_env([], weekly=432000, reset=1700060480, extra=("Dump",))
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: lua.table_from({
        1: lua.table_from({"questID": 196, "mapID": 2393,
                           "name": "辉耀丰饶", "money": 30000000})})})
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507 else None,
        "GetQuestTimeLeftSeconds": lambda qid: 7170,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "银月城"}) if int(mid) == 2395 else lua.table_from({"name": "巨龙岛"})
        if int(mid) == 2393 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
        # 加固符文石这一族本轮才进内置表（12.1.5 反馈⑦）：90573 是 probe 实测问得出名字的
        # 那条，占行的就是它；90574-90576 问不出名字（客户端不认识的 ID），
        # 按"裸 ID 不占行"只当家族证据
        "GetTitleForQuestID": lambda qid: "加固符文石：魔导师" if int(qid) == 90573 else None,
    })
    lua.execute("GQL.Api.WallClock = function() return 1700000000 end")
    gql.Config.AddKeyword("丰饶")
    gql.Sync.SnapshotNow()
    printed = []
    g.print = lambda *a: printed.append(" ".join(str(x) for x in a))
    text = str(gql.Dump.Run())
    head = text.split("== 周常家族 ==")
    if len(head) != 2:
        return fail("dump_shows_the_weekly_family_section", f"导出要有周常家族一节:\n{text}")
    sec = head[1]
    if "族=abundant-offerings" not in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"族 key 要打出来（内置表的 key，不是 kw: 前缀）:\n{sec}")
    # 锚点自己的地图这条机器上问不出（GetQuestInfoByQuestID 的第 2 返回值实测不是地图号，
    # 已摘），图是这一轮板面上同族那条 196 搬过来的 —— 见 Ledger.PropagateFamilyMaps
    if "丰饶供品#89507[2393]" not in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"占行的族本体要带着名字与这一轮同族成员给的地图打出来:\n{sec}")
    if "89507" in sec and "89507[2393]只当证据" in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"族本体是那一行，不该被标成不占行:\n{sec}")
    if "加固符文石：魔导师#90573" not in sec or "加固符文石：魔导师#90573[无图]只当证据" in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"内置表新收的加固符文石这一族要出证，且占行的是问得出名字的 90573:\n{sec}")
    if "?#90574[无图]只当证据" not in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"问不出名字的成员只当家族证据，不许占一行裸 ID:\n{sec}")
    if "辉耀丰饶#196[2393]只当证据，不占行" not in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"板上那条子任务要带着自己那张图、并标明只当证据:\n{sec}")
    fam_line = next((l for l in sec.split("\n") if "族=abundant-offerings" in l), "")
    if "已交" not in fam_line:
        return fail("dump_shows_the_weekly_family_section",
                    f"每角色那一格问的是全族，锚点做过就得看得见:\n{fam_line}")
    if "周口径：倒计时=432000" not in sec:
        return fail("dump_shows_the_weekly_family_section",
                    f"周接口原值与来源要并排打出来:\n{sec}")
    if "轮换=" in text:
        return fail("dump_shows_the_weekly_family_section",
                    "轮换那条线已经按用户口径④删掉，导出里不许还留着")
    chat = "\n".join(printed)
    if "导出共" not in chat or "== 金币任务 ==" in chat:
        return fail("dump_shows_the_weekly_family_section",
                    f"chat 只报一句指路，全文进日志窗（实测抱怨：dump 太长放不下）:\n{chat}")
    ok("dump_shows_the_weekly_family_section")


def test_cycle_expiry_never_downgrades_a_verified_value():
    """日志收进来的周常（Sync 那条路径）到期口径仍走 SetCycleExpiry：
    能问出下次周重置就用它。两个不许：① 接口这一轮偶发问不出时，不许把已经验过的
    未来真数覆盖成 24 小时排程值（那是把真 CD 说成假 CD）；② 已经过期的旧值要退回
    排程值并标 guessed，界面才不上色、不报剩余。"""
    lua, gql, _ = sync_env([], weekly=432000, reset=1700060480)
    g = lua.globals()
    g.time = lambda: 1700000000
    lua.execute("GQL.Api.WallClock = function() return 1700000000 end")
    gql.Sync.SnapshotNow()
    lua.execute("GQL.Api.WeeklyResetCountdown = function() return nil end "
                "GQL.Api.ResetTime = function() return nil end")
    g.GetQuestResetTime = lambda: None
    lua.execute("T = { expiresAt = 1700432000, expiresSrc = '下次周重置' } "
                "GQL.Ledger.SetCycleExpiry(T, 1700000000)")
    if one(lua.execute("return T.expiresSrc")) != "下次周重置":
        return fail("cycle_expiry_never_downgrades_a_verified_value",
                    "接口偶发问不出时，未来的真数不许被排程值覆盖，实际口径 "
                    + one(lua.execute("return T.expiresSrc")))
    lua.execute("U = { expiresAt = 1699000000, expiresSrc = '下次周重置' } "
                "GQL.Ledger.SetCycleExpiry(U, 1700000000)")
    if one(lua.execute("return U.expiresSrc")) != "排程值":
        return fail("cycle_expiry_never_downgrades_a_verified_value",
                    "已经过期的旧值要退回排程值")
    if not lua.execute("return U.expiresGuessed"):
        return fail("cycle_expiry_never_downgrades_a_verified_value",
                    "排程值必须标 guessed，界面才不上色")
    ok("cycle_expiry_never_downgrades_a_verified_value")


def test_probe_reports_weekly_source_and_timeleft_candidates():
    """两处取证自己在撒谎：① 周重置那行永远报"来源=无可用周入口"——
    `local a, b = (X and X()) or nil` 的括号把调用截断成单值，第二个返回值当场丢掉，
    12.1.5 的实测报告里"周重置倒计时=467060 来源=无可用周入口"就是这么自相矛盾的；
    ② 家族段要逐个问剩余计时入口（用户口径④：界面只报子任务自己的倒计时，
    那个数是谁答的、答没答出来必须在这里看得见）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": lambda: 432000})
    g.C_QuestLog = lua.table_from({
        "IsQuestFlaggedCompleted": lambda qid: False,
        "GetTitleForQuestID": lambda qid: "丰饶供品" if int(qid) == 89507 else None,
        "GetTimeAllowed": lambda qid: 7200 if int(qid) == 89507 else None,
    })
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: None,
        "GetQuestTimeLeftSeconds": lambda qid: None,
    })
    gql.Config.Init()
    report = str(gql.Probe.Run())
    if "周重置倒计时=432000 来源=C_DateAndTime.GetSecondsUntilWeeklyReset" not in report:
        return fail("probe_reports_weekly_source_and_timeleft_candidates",
                    f"周口径的数与来源必须同时报得出:\n{report}")
    if "＜剩余计时候选＞" not in report:
        return fail("probe_reports_weekly_source_and_timeleft_candidates",
                    f"家族段要逐个问计时入口:\n{report}")
    for want in ("C_TaskQuest.GetQuestTimeLeftSeconds", "C_QuestLog.GetTimeAllowed"):
        if want not in report:
            return fail("probe_reports_weekly_source_and_timeleft_candidates",
                        f"候选名单缺 {want}:\n{report}")
    if "＜轮换计时候选＞" in report:
        return fail("probe_reports_weekly_source_and_timeleft_candidates",
                    "轮换口径已经删掉，取证段不许还挂着那个名字催用户去找第二条线")
    ok("probe_reports_weekly_source_and_timeleft_candidates")


def test_probe_reports_registered_preset_evidence():
    """probe 要把内置表这几条 ID 的本机证据摊开：名字/地图/完成状态各是谁答的、
    在不在按图枚举出来的候选清单里、在不在已完成清单里 —— 还要看用户的关键词
    能不能在候选里白捡到任务（"自动找 ID"这条线的判据在
    test_probe_lists_board_names_and_the_bounty_surface 那边，本用例只验注册取证段）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_TaskQuest = lua.table_from({
        # 预设那条 + 另一条名字同样命中关键词的轮换 ID（"自动找 ID"的证据就来自后者）
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507
            else (("丰饶供品：冬季版", 2395) if int(qid) == 91000 else None),
        # 按图枚举：候选里除了预设那条，再放一条名字命中关键词的轮换 ID
        "GetQuestsOnMap": lambda mid: lua.table_from({
            1: lua.table_from({"questID": 89507, "mapID": 2395}),
            2: lua.table_from({"questID": 91000, "mapID": 2395}),
        }) if int(mid) == 2395 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 89507,
        "GetAllCompletedQuestIDs": lambda: lua.table_from({1: 89507, 2: 90999}),
    })
    gql.Config.Init()
    gql.Config.AddMap(2395)
    gql.Config.AddKeyword("丰饶")
    report = str(gql.Probe.Run())
    if "---- 注册周常取证" not in report:
        return fail("probe_reports_registered_preset_evidence", f"缺注册周常取证段:\n{report}")
    if "预设 abundant-offerings questID=89507" not in report:
        return fail("probe_reports_registered_preset_evidence",
                    f"内置表每条 ID 都要单独报证据:\n{report}")
    if "在候选清单=是" not in report:
        return fail("probe_reports_registered_preset_evidence",
                    f"要报预设 ID 在不在按图枚举出来的候选里:\n{report}")
    if "在已完成清单=是" not in report:
        return fail("probe_reports_registered_preset_evidence",
                    f"要报它在不在这角色的已完成清单里:\n{report}")
    if "Api.CompletedQuestIDs() -> 共 2 条" not in report:
        return fail("probe_reports_registered_preset_evidence",
                    f"已完成清单的形状要如实报:\n{report}")
    if "91000" not in report:
        return fail("probe_reports_registered_preset_evidence",
                    f"候选里名字命中关键词的那条要打出来（这才谈得上自动找 ID）:\n{report}")
    # 已完成清单问不出时不许写成"0 条"：把接口故障说成"这周什么都没做"最坑
    g.C_QuestLog = lua.table_from({"IsQuestFlaggedCompleted": lambda qid: False})
    empty = str(gql.Probe.Run())
    if "问不出" not in empty:
        return fail("probe_reports_registered_preset_evidence",
                    f"清单接口缺失要写明问不出:\n{empty}")
    ok("probe_reports_registered_preset_evidence")


def test_probe_lists_board_names_and_the_bounty_surface():
    """用户 2026-10-24 回传：`图 2437(祖阿曼) 板清单=9 并上日志=9 命中关键词=0`，矩阵里祖阿曼空着。
    光看这两个数分不出"那 9 条到底叫什么"和"名字根本问不出来"，所以每张图的清单名字要逐条摊开
    （命中关键词的标 *）。再补第三块清单：特别任务这种很可能只住在
    C_QuestLog.GetBountiesForMapID 里 —— 它**只进取证、没接进生产**，等这份报告答得出来再接线。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_TaskQuest = lua.table_from({
        # 锚点本周在祖阿曼这件事，12.1.5 实测只有 GetQuestZoneID 答得出（91805/91808/91811
        # 它答 2437，与所在那张板一致）。GetQuestInfoByQuestID 留在这里只贡献名字：
        # 它第 2 返回值实测是 2696/1859 这种"地图名=nil"的号，已从地图链摘掉，当诱饵用
        "GetQuestZoneID": lambda qid: 2437 if int(qid) == 89507 else None,
        "GetQuestInfoByQuestID": lambda qid: (
            ("丰饶供品", 2696) if int(qid) == 89507
            else ("丰饶：剥皮洞穴", 2696) if int(qid) == 91001
            else ("祖阿曼的杂活", 2696) if int(qid) == 91002
            else ("永歌森林的杂活", 2696) if int(qid) == 91003 else None),
        "GetQuestsOnMap": lambda mid: lua.table_from(
            {1: lua.table_from({"questID": 91002, "mapID": 2437})}) if int(mid) == 2437
            else lua.table_from({1: lua.table_from({"questID": 91003, "mapID": 2395})})
            if int(mid) == 2395 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "祖阿曼" if int(mid) == 2437 else "永歌森林"}) if int(mid) in (2437, 2395)
            else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestFlaggedCompleted": lambda qid: False,
        "GetAllCompletedQuestIDs": lambda: lua.table_from({}),
        # 本周那条只出现在悬赏清单里（板清单 9 条没有它 = 用户回传的那个 命中 0）
        "GetBountiesForMapID": lambda mid: lua.table_from(
            {1: lua.table_from({"questID": 91001, "numObjectives": 1})}) if int(mid) == 2437
            else lua.table_from({}),
    })
    gql.Config.Init()
    gql.Config.AddMap(2395)
    gql.Config.AddKeyword("丰饶")
    lua.execute("GQLDB.quests = GQLDB.quests or {} "
                "GQLDB.quests[89507] = { name='丰饶供品', mapID=2393, regKey='abundant-offerings', "
                "  observations={} }")
    report = str(gql.Probe.Run())
    if "---- 补扫图源" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface", f"缺补扫图源段:\n{report}")
    if "图 2437(祖阿曼)[锚]" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    f"锚点报的图要标出来，才看得出它是靠哪条线进清单的:\n{report}")
    if "91002=祖阿曼的杂活" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    "没命中的行也要把名字摊开（否则 板清单=9 命中=0 分不出是哪种情况）:\n" + report)
    if "91001=丰饶：剥皮洞穴*" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    "命中关键词的行要标 *，名字来自接口现问:\n" + report)
    if "悬赏=1" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    f"悬赏清单要单独报条数:\n{report}")
    if "丰饶：剥皮洞穴(#91001|丰饶)" not in report:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    f"命中要报出名字+ID+命中的关键词:\n{report}")
    # 接口缺失时写"问不出"，不许写成 0：把"这台客户端没答"说成"这张图没有特别任务"最坑
    del g.C_QuestLog.GetBountiesForMapID
    noapi = str(gql.Probe.Run())
    if "悬赏=问不出" not in noapi:
        return fail("probe_lists_board_names_and_the_bounty_surface",
                    f"悬赏接口缺失要写明问不出:\n{noapi}")
    ok("probe_lists_board_names_and_the_bounty_surface")


def test_probe_shows_row_week_evidence_and_raw_log_fields():
    """12.1.5 实测反馈④"蛇岛出现了 2 个不是本周的特别任务，本周的反而没出现，本周是怨灵"
    和⑤"追踪萨拉塔斯并不是蛇岛的任务……它应该是银月城的，或者永歌森林的"。
    两段新取证，各钉一件事：
    · 矩阵行的本周凭据 —— 这轮在不在板上 + 在不在池 + 状态由哪条判据接口答的 + 到期时刻哪来的。
      上一轮只打了状态，回传还是分不出"上周那条为什么还在"；池问不出时必须写"判不了"，
      空表读成"全都不在池里"会把每条都冤枉成上周残留。
    · 日志原始行字段 —— C_QuestLog.GetInfo 每行的**未归一化**字段。生产链两条入口对
      追踪萨拉塔斯全答不出地图，下一份凭据只能从这批字段里找；没找到之前它落"未知地图"，
      不许拿"玩家当时站在蛇岛"当它的归属（那正是已停用的 GetMapForQuestPOIs 干的事）。"""
    def build(drop=()):
        lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
        g = lua.globals()
        g.time = lambda: 1700000000
        g.UnitName = lambda *a: "小白龙"
        g.GetRealmName = lambda: "奥达曼"
        if "pool" not in drop:
            # 池里只有本周那条怨灵：另一条就是用户说的"不是本周的特别任务"
            g.C_WorldQuests = lua.table_from({
                "GetWorldQuests": lambda: lua.table_from(
                    {1: lua.table_from({"questID": 91805, "mapID": 2512})}),
            })
        g.C_TaskQuest = lua.table_from({
            "GetQuestsOnMap": lambda mid: lua.table_from({
                1: lua.table_from({"questID": 91805, "mapID": 2512}),
                2: lua.table_from({"questID": 91808, "mapID": 2512}),
            }) if int(mid) == 2512 else lua.table_from({}),
            "GetQuestZoneID": lambda qid: None,
            "GetQuestInfoByQuestID": lambda qid: None,
            "GetQuestTimeLeftSeconds": lambda qid: None,
        })
        if "judge" not in drop:
            g.QuestUtils_IsQuestWorldQuest = lambda qid: True
        log_rows = {
            "GetTitleForQuestID": lambda qid: (
                "特别任务：怨灵" if int(qid) == 91805
                else "特别任务：暗影伏击" if int(qid) == 91808
                else "追踪萨拉塔斯" if int(qid) == 98172 else None),
            # 怨灵这周做过：只有 IsQuestFlaggedCompleted 答得出（另两条 12.1.5 实测不存在）
            "GetAllCompletedQuestIDs": lambda: lua.table_from({}),
        }
        if "state" not in drop:
            log_rows["IsQuestFlaggedCompleted"] = lambda qid: int(qid) == 91805
        if "log" not in drop:
            # 原始行带 mapID/zoneID/uiMapID：这段就是把它们原样摊开，别只留 title/questID
            log_rows["GetInfo"] = lambda i: (
                lua.table_from({"questID": 98172, "title": "追踪萨拉塔斯", "isHeader": False,
                                "mapID": 2395, "zoneID": 2394, "uiMapID": 2395, "level": 80})
                if int(i) == 1 else
                lua.table_from({"questID": 91808, "title": "特别任务：暗影伏击",
                                "isHeader": False, "questType": 1}) if int(i) == 2 else None)
        g.C_QuestLog = lua.table_from(log_rows)
        g.C_Map = lua.table_from({
            "GetMapInfo": lambda mid: lua.table_from(
                {"name": "盘卷蛇岛"}) if int(mid) == 2512 else None,
        })
        gql.Config.Init()
        gql.Config.AddMap(2512)
        lua.execute("GQLDB.quests = GQLDB.quests or {} "
                    "GQLDB.quests[91805] = { name='特别任务：怨灵', mapID=2512, onBoard=true, "
                    "  expiresAt=1700024660, expiresSrc='接口倒计时', lastSeenAt=1700000000, observations={} } "
                    "GQLDB.quests[91808] = { name='特别任务：暗影伏击', mapID=2512, "
                    "  lastSeenAt=1700000000, observations={} } "
                    "GQLDB.chars = { ['小白龙-奥达曼'] = { views = { "
                    "  [91805] = { s='done' }, [91808] = { s='open' } } } }")
        return str(gql.Probe.Run())

    report = build()
    if "---- 矩阵行的本周凭据" not in report:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"缺矩阵行本周凭据段:\n{report}")
    sec = report.split("---- 矩阵行的本周凭据")[1].split("---- ")[0]
    if "#91805 特别任务：怨灵" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"怨灵那条要摊开（用户说本周就是它）:\n{sec}")
    if "这轮在板上=true 在池=true 状态=done" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    "本周那条的三列凭据要一起报出来，才和上周残留分得开:\n" + sec)
    if "C_QuestLog.IsQuestFlaggedCompleted 回 true（done 档）" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"状态要报出**谁答的**（判据名单必须与生产同一份）:\n{sec}")
    if "6时51分（接口倒计时）" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"到期要带剩余多长 + 来源:\n{sec}")
    if "#91808 特别任务：暗影伏击" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"占行的每条都要摊（用户看到的正是这两条）:\n{sec}")
    line1808 = [ln for ln in sec.split("\n") if "#91808" in ln]
    if not line1808 or "这轮在板上=false 在池=false" not in line1808[0]:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    "上周残留那条要能看出「这轮既不在板上也不在池里」:\n" + sec)
    if "到期=无（接口从没答过它的剩余）" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"没有到期时刻的条要写无，不许留空或写 0:\n{sec}")

    # 日志原始行字段：追踪萨拉塔斯那张蛇岛是历史残留，下一份地图凭据只能从这批字段里找
    if "---- 日志原始行字段" not in report:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"缺日志原始行字段段:\n{report}")
    raw = report.split("---- 日志原始行字段")[1].split("\n\n")[0]
    if "questID=98172 名=追踪萨拉塔斯" not in raw:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"日志第一条要报出 ID 与名字:\n{raw}")
    if "mapID=2395" not in raw or "uiMapID=2395" not in raw or "zoneID=2394" not in raw:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    "要摊**未归一化**的原样字段（AcceptedQuests 只挑两个字段等于把地图线索丢了）:\n"
                    + raw)
    if "questID=行里没有" in raw:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"行里有 questID 却报成没有：归一化写坏了:\n{raw}")
    # 日志入口缺失 → 写问不出，不许留一段空白让人以为"日志里没任务"
    noraw = build(drop=("log",)).split("---- 日志原始行字段")[1].split("---- ")[0]
    if "C_QuestLog.GetInfo 问不出" not in noraw:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"日志入口缺失要写明问不出:\n{noraw}")
    # 池问不出 → 在池一列写判不了，不许塌成 false（那等于说每条都是上周残留）
    nopool = build(drop=("pool", "judge"))
    nsec = nopool.split("---- 矩阵行的本周凭据")[1].split("---- ")[0]
    if "在池=判不了" not in nsec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"池问不出时在池一列要写判不了:\n{nsec}")
    if "在池=false" in nsec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"池没答不许写成客户端答了 false:\n{nsec}")
    # 判据接口全缺失 → 「谁答的」写判据缺失，不许留空或写成某条接口答了
    nostate = build(drop=("state",)).split("---- 矩阵行的本周凭据")[1].split("---- ")[0]
    if "谁答的=问不出（判据接口全缺失或全报错）" not in nostate:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"判据接口全缺失要写清是接口没有，不是任务没做:\n{nostate}")
    ok("probe_shows_row_week_evidence_and_raw_log_fields")


def test_probe_reports_why_a_board_quest_is_not_in_the_pool():
    """12.1.5 实测反馈③"蛇岛有个松露大乱斗的金币任务，我的角色做了，但是点刷新，矩阵没有看到这个任务"。
    它挂在那张图的板清单上（补扫图源那段能翻到它的 ID），却进不了矩阵，中间只有三道闸：
    ① Api.RowsToPool 的世界任务判别（QuestUtils_IsQuestWorldQuest 回 false 当场丢，池根本收不到）；
    ② 金额（进了池但没到门槛、名字又不命中关键词，矩阵不占行）；
    ③ 账本（有记录=池收下了、卡在②；没记录=卡在①）。
    这段就逐条把三闸的答复摊开。两件事必须钉住：
    · 接口缺失要写"判不了"，不许写成 false —— 那等于替客户端说"它说这条不是世界任务"；
    · 池问不出时不许把整段说成"池里没有"，否则玩家会照着假结论去改设置。
    两个降级场景各自 build 一套模块：世界任务判别有会话内缓存（wqVerdict），
    在同一实例里删掉接口再问一次，读到的还是上一轮的缓存 —— 那结论是假的。"""
    def build(drop=()):
        lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
        g = lua.globals()
        g.UnitName = lambda *a: "小白龙"
        g.GetRealmName = lambda: "奥达曼"
        # 池里只有 91002（非按图源，不走世界任务判别）；板上四条另有一条在池外
        if "pool" not in drop:
            g.C_WorldQuests = lua.table_from({
                "GetWorldQuests": lambda: lua.table_from(
                    {1: lua.table_from({"questID": 91002, "mapID": 2437})}),
            })
        g.C_TaskQuest = lua.table_from({
            "GetQuestsOnMap": lambda mid: lua.table_from({
                1: lua.table_from({"questID": 91002, "mapID": 2437}),
                2: lua.table_from({"questID": 95484, "mapID": 2437}),   # 松露大乱斗：该收却没收到
                3: lua.table_from({"questID": 91003, "mapID": 2437}),   # 客户端说它不是世界任务
                4: lua.table_from({"questID": 91005, "mapID": 2437}),   # 进了账本但金币不到门槛
            }) if int(mid) == 2437 else lua.table_from({}),
            "GetQuestZoneID": lambda qid: None,
            "GetQuestInfoByQuestID": lambda qid: None,
            "GetQuestTimeLeftSeconds": lambda qid: None,
        })
        # 世界任务判别：95484 与 91005 是真世界任务，91003 客户端直接说不是
        if "judge" not in drop:
            g.QuestUtils_IsQuestWorldQuest = lambda qid: int(qid) != 91003
        g.GetQuestLogRewardMoney = lambda qid: (
            # 962金 = 用户第十五轮回传的游戏自带气泡截图（松露大乱斗），量级照它取；
            # 91005 那条 150g 在新默认门槛 500g 之下，正是要报"不到门槛"的形状
            9620000 if int(qid) == 95484 else 1500000 if int(qid) == 91005 else 0)
        g.C_QuestLog = lua.table_from({
            "GetTitleForQuestID": lambda qid: (
                "松露大乱斗" if int(qid) == 95484 else "祖阿曼的杂活" if int(qid) == 91003
                else "健步如飞" if int(qid) == 91005 else None),
            "IsQuestFlaggedCompleted": lambda qid: False,
            "GetAllCompletedQuestIDs": lambda: lua.table_from({}),
        })
        g.C_Map = lua.table_from({
            "GetMapInfo": lambda mid: lua.table_from(
                {"name": "祖阿曼"}) if int(mid) == 2437 else None,
        })
        gql.Config.Init()
        gql.Config.AddMap(2437)
        lua.execute("GQLDB.quests = GQLDB.quests or {} "
                    "GQLDB.quests[91005] = { name='健步如飞', mapID=2437, observations={} }")
        return str(gql.Probe.Run())

    report = build()
    if "---- 板清单有、池里没有" not in report:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"缺板清单/池差集取证段:\n{report}")
    sec = report.split("---- 板清单有、池里没有")[1].split("---- ")[0]
    if "#95484 松露大乱斗" not in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"板清单里、池没有的那条要摊开（用户的原案）:\n{sec}")
    if "世界任务=true 金币=9620000 账本=无" not in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    "这条要能看出「判别过了、金币到了、可账本没收到」：\n" + sec)
    if "#91003" not in sec or "世界任务=false" not in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"客户端判它不是世界任务的要报 false（这就是第①闸丢的）:\n{sec}")
    if "91002" in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"池里已经答到的条不许混进差集:\n{sec}")
    if "金币=1500000（不到门槛）" not in sec or "账本=有" not in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    "进了账本、金币不到门槛的那条要报出「账本=有 + 不到门槛」（卡在第②闸）:\n" + sec)
    if "板清单共 4 条，池里没答到 3 条" not in sec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"要有总数与差集条数，才知道这段覆盖了多少:\n{sec}")
    # 判别接口缺失 → 写"判不了"，不许塌成 false（那等于替客户端说话）
    nsec = build(drop=("judge",)).split("---- 板清单有、池里没有")[1].split("---- ")[0]
    if "世界任务=判不了" not in nsec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"判别接口缺失要写判不了:\n{nsec}")
    if "世界任务=false" in nsec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"接口没答不许写成客户端答了 false:\n{nsec}")
    # 池问不出 → 整段降级成"没被确认"，不许说成"池里没有"。
    # 这一份要同时摘掉判别：按图那几条源只要有一行判不了就整体放弃（RowsToPool 的
    # unclassified 口径），池于是真问不出 —— 只删全局池源是问不出不了的，它会退回按图枚举。
    psec = build(drop=("judge", "pool")).split("---- 板清单有、池里没有")[1].split("---- ")[0]
    if "池问不出（GetPool 不可用）" not in psec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"池问不出要写明判不了，不许当成池里没有:\n{psec}")
    if "池里没答到 4 条；上面" in psec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"池问不出时差集条数也要带上判不了标记:\n{psec}")
    if "板清单共 4 条，池里没答到 4 条（池问不出，判不了）" not in psec:
        return fail("probe_reports_why_a_board_quest_is_not_in_the_pool",
                    f"池问不出时四条都要进差集，并标上判不了:\n{psec}")
    ok("probe_reports_why_a_board_quest_is_not_in_the_pool")


def test_probe_reverse_checks_the_completed_list():
    """第十一轮把按图清单这条路堵死了（祖阿曼 板=9、悬赏=5，摊开名字没有丰饶）。
    剩下唯一可能自己冒出子任务 ID 的面是已完成清单（12.1.5 实测 10418 条答得出）。
    用户问"丰饶任务能查询到吗？查询到的任务有所属地图、结束时间吗"——这段就是让他的
    客户端自己回答：命中族词的每一条 ID，把名字/地图（谁答的）/剩余/做过没有摊开。
    做过这一列单独不作数（IsQuestFlaggedCompleted 一次性任务也永远回做过），
    要与剩余时间并排读：只有答得出剩余的才是本周 lockout。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_TaskQuest = lua.table_from({
        # 地图只能由 GetQuestZoneID 答：GetQuestInfoByQuestID 的第 2 返回值在这里是诱饵
        # （12.1.5 实测它对 91805 回 2696、对 42859 回 1859，那两个号的地图名问出来是 nil，
        # 不是地图号），生产链已经把它摘掉了，断言看它有没有从这份报告里溜回来。
        "GetQuestInfoByQuestID": lambda qid: (
            ("丰饶：剥皮洞穴", 2696) if int(qid) == 91001
            else ("丰饶供品", 2696) if int(qid) == 89507
            else ("暖房聚会", 2696) if int(qid) == 95440 else None),
        "GetQuestZoneID": lambda qid: 2437 if int(qid) == 91001 else None,
        "GetQuestsOnMap": lambda mid: lua.table_from({}),
        "GetQuestTimeLeftSeconds": lambda qid: 24660 if int(qid) == 91001 else None,
    })
    g.C_QuestLog = lua.table_from({
        "GetTitleForQuestID": lambda qid: (
            "丰饶：剥皮洞穴" if int(qid) == 91001 else "丰饶供品" if int(qid) == 89507
            else "暖房聚会" if int(qid) == 95440 else None),
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 91001,
        "GetAllCompletedQuestIDs": lambda: lua.table_from({1: 91001, 2: 95440}),
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": "祖阿曼" if int(mid) == 2437 else "永歌森林"}),
    })
    gql.Config.Init()
    report = str(gql.Probe.Run())
    if "---- 族词反查已完成清单" not in report:
        return fail("probe_reverse_checks_the_completed_list", f"缺反查段:\n{report}")
    sec = report.split("---- 族词反查已完成清单")[1]
    if "#91001 丰饶：剥皮洞穴 地图=2437(C_TaskQuest.GetQuestZoneID)" not in report:
        return fail("probe_reverse_checks_the_completed_list",
                    "命中族词的已完成 ID 要带地图与**谁答的**:\n" + report)
    if "地图=2696" in sec:
        return fail("probe_reverse_checks_the_completed_list",
                    "已停用的 GetQuestInfoByQuestID 第 2 返回值不许再当这张图的地图号:\n" + sec)
    if "剩余=6.8时" not in report:
        return fail("probe_reverse_checks_the_completed_list",
                    f"倒计时要按小时报出来（问不出就写问不出）:\n{report}")
    if "做过=done" not in report:
        return fail("probe_reverse_checks_the_completed_list",
                    f"做过没有要报（这决定它能不能当家族证据）:\n{report}")
    if "本周=" in sec:
        return fail("probe_reverse_checks_the_completed_list",
                    "这一列读不出本周（一次性任务永远回做过），列名不许写成本周:\n" + report)
    if "暖房聚会" in sec:
        return fail("probe_reverse_checks_the_completed_list",
                    "没命中族词的条目不许混进反查结果")
    # 清单接口缺失时写"问不出"，不许当成"这族一条都没做过"
    del g.C_QuestLog.GetAllCompletedQuestIDs
    noapi = str(gql.Probe.Run())
    if "已完成清单问不出" not in noapi:
        return fail("probe_reverse_checks_the_completed_list",
                    f"清单接口缺失要写明问不出:\n{noapi}")
    ok("probe_reverse_checks_the_completed_list")


def test_probe_checks_the_runestone_ids_from_savedinstances():
    """用户让我查"SaveInstance 怎么处理加固符文石"。它确实跟这条：
    `Modules/Progress.lua:1673-1687` 手抄了四条 ID（90573/90574/90575/90576 = 四个阵营符石，
    type=any、reset=weekly），国服名在 `Locale.lua:609`。
    12.1.5 实测之后这四条**已经接进内置族表**（用户反馈⑦"所有角色都做完了，矩阵里没有"）：
    占行的是问得出名字的 90573，另外三条只当"全族取最强状态"的证据。
    这段取证于是换成钉住接线口径本身——① 四条 ID 逐个摊开、名字由客户端答不许写死；
    ② 族表里这四条一条都不许少（少一条就等于换个骑士团的号永久打不上勾）；
    ③ 这一族**不许有 match**：板上有条不相干的"特别任务：加固符文石"，
       加了族词会被扫进族里、整行反而消失。
    剩下唯一没定的（任一即算 vs 一周各做几条）只有玩家在游戏里能答，段标题要问出来。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", "Probe", mock=True)
    g = lua.globals()
    g.UnitName = lambda *a: "小白龙"
    g.GetRealmName = lambda: "奥达曼"
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: (
            ("加固符文石：魔导师", 2393) if int(qid) == 90573 else None),
        "GetQuestsOnMap": lambda mid: lua.table_from({}),
        "GetQuestTimeLeftSeconds": lambda qid: None,
    })
    g.C_QuestLog = lua.table_from({
        "GetTitleForQuestID": lambda qid: (
            "加固符文石：血骑士" if int(qid) == 90574 else None),
        "IsQuestStarted": lambda qid: False,
        "IsQuestFlaggedCompleted": lambda qid: int(qid) == 90575,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "巨龙岛"}) if int(mid) == 2393 else None,
    })
    gql.Config.Init()
    report = str(gql.Probe.Run())
    sec = report.split("---- 加固符文石")[1] if "---- 加固符文石" in report else ""
    if not sec:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    f"缺加固符文石取证段:\n{report}")
    for want in ("#90573", "#90574", "#90575", "#90576"):
        if want not in sec:
            return fail("probe_checks_the_runestone_ids_from_savedinstances",
                        f"四条 ID 要逐条摊开（缺 {want}）:\n{sec}")
    if "名=加固符文石：魔导师" not in sec:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    f"C_TaskQuest 答得出的名字要打出来:\n{sec}")
    if "名=加固符文石：血骑士" not in sec:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    f"C_QuestLog 答得出的名字也要打出来:\n{sec}")
    if "剩余=问不出" not in sec:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    f"板面倒计时问不出要写问不出，不许留空或写 0:\n{sec}")
    if "做过=done" not in sec:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    f"做过的判据要报出来（这条决定它能不能当家族证据）:\n{sec}")
    if "已接进内置族表" not in sec.split("\n")[0]:
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    "段标题要写明这四条已经接进族表、90573 占行，玩家才会去答"
                    "「任一即算」那一问")
    # 接线口径钉在用例里：族表收全四条，锚点是 ids[1]=90573，而且这一族不许带 match
    got = one(lua.execute("local p for _, x in ipairs(GQL.Api.WEEKLY_PRESETS) do "
                          "  if x.key == 'fortify-the-runestones' then p = x end end "
                          "if not p then return 'no preset' end "
                          "local ids = table.concat(p.ids or {}, ',') "
                          "return ids .. '|' .. tostring(p.match)"))
    if got != "90573,90574,90575,90576|nil":
        return fail("probe_checks_the_runestone_ids_from_savedinstances",
                    "族表要收全四条、90573 排第一当锚点、且不带 match"
                    f"（有 match 会把『特别任务：加固符文石』扫进这一族）: {got}")
    ok("probe_checks_the_runestone_ids_from_savedinstances")


def test_turnin_of_unknown_quest_never_creates_a_row():
    """交一条账本里没有、接口也说不是世界任务的普通任务（职业/战役任务），
    不许建记录、也不许写下"本周交过"的凭据。宁可不记，也不给矩阵造假行。"""
    lua, gql, _ = sync_env([(130, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQL.Api.IsWorldQuest = function() return false end")
    gql.Sync.BeginCapture(7777)          # 7777 从没上过板、不在账本
    g.GQL_NOW = 30
    lua.globals().GQL_FireTimers()
    if lua.execute("return GQL.Config.DB().quests[7777] ~= nil") is True:
        return fail("turnin_of_unknown_quest_never_creates_a_row",
                    "交普通任务凭空建了任务记录")
    if lua.execute("""
        local db = GQL.Config.DB()
        local key = GQL.Sync.CurrentCharKey()
        return db.turnins[key] ~= nil and db.turnins[key][7777] ~= nil
    """):
        return fail("turnin_of_unknown_quest_never_creates_a_row",
                    "交普通任务写下了交任务凭据")
    ok("turnin_of_unknown_quest_never_creates_a_row")


def test_turnin_of_pooled_quest_still_settles():
    """放宽判据的代价必须有人盯着：上过板的世界任务照常记账（这条不许红）。"""
    lua, gql, _ = sync_env([(131, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQL.Api.IsWorldQuest = function() return nil end")   # 接口答不出
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()        # 先把 131 收进池（拿到 poolSeen）
    gql.Sync.BeginCapture(131)
    g.GetMoney = lambda: 5100000          # 计划里写的 100500 与断言 5000000 对不上
    g.GQL_NOW = 40
    lua.globals().GQL_FireTimers()
    if lua.execute("""
        local db = GQL.Config.DB()
        local key = GQL.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][131]
        return rows ~= nil and rows[#rows].g == 5000000
    """) is not True:
        return fail("turnin_of_pooled_quest_still_settles",
                    f"在板任务交完没记到金币:\n{gql.Config.GetLogs()}")
    ok("turnin_of_pooled_quest_still_settles")


def test_money_window_seq_rejects_stale_timer():
    """换任务时旧钱窗的定时器不许结算新窗口：每个窗口只认自己那一份 seq。
    串台结算会把新窗口的钱按已经走掉的一小截算少。"""
    lua, gql, _ = sync_env([(140, 30000000), (141, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()          # 两条任务都进账本（拿到 poolSeen）
    gql.Sync.BeginCapture(140)
    first = int(lua.execute("return GQL.Sync.__captureSeq()"))
    g.GQL_NOW = 13
    g.GetMoney = lambda: 100500
    gql.Sync.BeginCapture(141)              # 换任务：旧窗口应先结算、旧定时器应作废
    second = int(lua.execute("return GQL.Sync.__captureSeq()"))
    if second <= first:
        return fail("money_window_seq_rejects_stale_timer", "窗口没有自己的 seq，迟到定时器无法作废")
    if not lua.execute("return GQL.Sync.__capturing()"):
        return fail("money_window_seq_rejects_stale_timer", "换任务后新窗口不该是空的")
    g.GQL_NOW = 60                          # 旧定时器的刻度到了：它不该再动新窗口
    lua.globals().GQL_FireTimers()
    if lua.execute("""
        local db = GQL.Config.DB()
        local key = GQL.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][140]
        return rows ~= nil and #rows == 1
    """) is not True:
        return fail("money_window_seq_rejects_stale_timer",
                    f"换任务时上一笔没结算成恰好一条:\n{gql.Config.GetLogs()}")
    ok("money_window_seq_rejects_stale_timer")


def test_money_window_kept_when_ledger_missing():
    """账本没就绪时不许丢掉窗口：那笔钱留着，等账本回来补记上。
    丢了就等于"做完了也不记"，玩家看到的是空白格。"""
    lua, gql, _ = sync_env([(142, 30000000)], money=100000, extra=("Matrix",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    gql.Sync.BeginCapture(142)
    # 桩掉 DB 模拟"结算那一刻账本没就绪"。日志环本身存在账本里，DB 为 nil 时
    # Config.Log 必然写不进去（本轮不动 Config 的日志落盘），所以这里把日志接口
    # 换成内存收集器：判据不变 —— 推迟结算必须留一行日志。
    lua.execute("""
        GQL_REAL_DB = GQL.Config.DB
        GQL.Config.DB = function() return nil end
        GQL_DEFER_LOGS = {}
        GQL.Config.Log = function(msg) table.insert(GQL_DEFER_LOGS, tostring(msg)) end
        GQL.Config.GetLogs = function() return table.concat(GQL_DEFER_LOGS, "\\n") end
    """)
    g.GQL_NOW = 40
    lua.globals().GQL_FireTimers()
    if not lua.execute("return GQL.Sync.__capturing()"):
        return fail("money_window_kept_when_ledger_missing",
                    "账本没就绪时窗口被清掉了（那笔钱丢了）")
    if "结算推迟" not in str(gql.Config.GetLogs()):
        return fail("money_window_kept_when_ledger_missing", "推迟结算必须留日志")
    # 账本回来之后那一笔必须真落账：只"保留窗口"而不再排一次，等于永远不结算。
    lua.execute("GQL.Config.DB = GQL_REAL_DB")
    g.GetMoney = lambda: 100500
    lua.globals().GQL_FireTimers()
    if lua.execute("""
        local db = GQL.Config.DB()
        local key = GQL.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][142]
        return rows ~= nil and #rows == 1 and rows[#rows].g == 500
    """) is not True:
        return fail("money_window_kept_when_ledger_missing",
                    "账本就绪后那笔钱没补记上:\n" + str(gql.Config.GetLogs()))
    if lua.execute("return GQL.Sync.__capturing()"):
        return fail("money_window_kept_when_ledger_missing", "补记完成窗口还挂着")
    ok("money_window_kept_when_ledger_missing")


def test_snapshot_survives_a_quest_api_error():
    """某一条任务的状态接口抛错，不许炸掉整趟快照：别的任务照常入账，
    刷新定时器照常排，出错的事留一行日志。"""
    lua, gql, _ = sync_env([(150, 30000000), (151, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute("""
        local real = GQL.Api.QuestState
        GQL.Api.QuestState = function(qid)
            if qid == 150 then error("模拟单条状态接口抛错") end
            return real(qid)
        end
    """)
    gql.Sync.SetPolling(True)
    res = lua.execute("local ok = pcall(GQL.Sync.SnapshotNow) return ok")
    if res is not True:
        return fail("snapshot_survives_a_quest_api_error",
                    "单条接口抛错把整趟快照炸掉了（后面还有收日志/补地图/消失判定）")
    if lua.execute("return GQL.Config.DB().quests[151] ~= nil") is not True:
        return fail("snapshot_survives_a_quest_api_error", "一条报错把别的任务也带没了")
    if lua.execute("local key = GQL.Sync.CurrentCharKey() "
                   "return GQL.Config.DB().chars[key].views[151] ~= nil") is not True:
        return fail("snapshot_survives_a_quest_api_error", "出错之后没写完别的角色视图")
    pending = lua.execute("local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
                          "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) < 1:
        return fail("snapshot_survives_a_quest_api_error",
                    "快照出错后没排刷新定时器：矩阵开着也不会再自己更新（会话死锁）")
    if "状态问不出" not in str(gql.Config.GetLogs()):
        return fail("snapshot_survives_a_quest_api_error", "单条接口抛错必须留日志")
    ok("snapshot_survives_a_quest_api_error")


def test_map_memory_rejects_non_numeric_quest_id():
    """口述地图表里 questID 写错成非数字时，不许静默跳过：
    { tonumber(x) } 在 Lua 里永远是真值，得先 tonumber 再判，否则退回名字那条路走不到。"""
    lua, gql, _ = sync_env([(152, 30000000)], extra=("Matrix",))
    g = lua.globals()
    lua.execute("""
        GQL.Api.MAP_MEMORY = {
            { questID = "96625x", name = "幽魂角斗士", map = "盘卷蛇岛" },
            { name = "任务152", map = "虚影风暴" },
        }
        GQL.Config.DB().config.extraMaps = { [2009] = true }
    """)
    lua.execute("GQL.Api.MapName = function(id) if id == 2009 then return '虚影风暴' end end")
    lua.execute("local ok = pcall(GQL.Sync.ApplyMapMemory, GQL.Config.DB()) return ok")
    logs = str(gql.Config.GetLogs())
    if "口述地图表的 questID 不是数字" not in logs:
        return fail("map_memory_rejects_non_numeric_quest_id",
                    f"非数字 questID 该点名报出来:\n{logs}")
    ok("map_memory_rejects_non_numeric_quest_id")


def test_missing_countdown_also_schedules_retry():
    """倒计时读不出（接口答 nil，不是抛错）也必须排补读。
    Task 4 的逐条容错差点把这条写丢：补读不排，"有的任务永远没有剩余时间"
    就又回来了（用户实测）。金额那条有自己的补读门，这里只盯倒计时这一半。"""
    lua, gql, _ = sync_env([(143, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: None})
    gql.Matrix.Toggle()          # 打开 = 抓一次 + 开轮询（补读只在面板开着时排）
    before = str(gql.Config.GetLogs()).count("快照完成")
    if before < 1:
        return fail("missing_countdown_also_schedules_retry",
                    f"打开面板没抓到池:\n{gql.Config.GetLogs()}")
    # 只推进 5 秒：到期唤醒那条排的是"最早到期时刻"，倒计时读不出时会排得很远，
    # 所以这一趟真再抓一次池只可能是补读干的。
    g.GQL_NOW = 5005
    lua.globals().GQL_FireTimers()
    if str(gql.Config.GetLogs()).count("快照完成") <= before:
        return fail("missing_countdown_also_schedules_retry",
                    f"倒计时读不出没触发补读:\n{gql.Config.GetLogs()}")
    ok("missing_countdown_also_schedules_retry")


def test_prune_never_mixes_clock_domains():
    """纪元秒与本次开机秒不可比：跨域时整条按时间清理必须跳过。
    混用的差值是几十亿，一次快照就能把整张账本清光（ThisCycle/WeekStats 都防了这手）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 100000, None)]), 1000)
    db["clockDomain"] = "epoch"
    db["quests"][7]["lastSeenAt"] = 1_700_000_000   # 上个存档写的纪元秒
    L.Prune(db, 500)                                # now 是本次开机秒
    if db["quests"][7] is None:
        return fail("prune_never_mixes_clock_domains", "跨时钟域把任务整条清掉了")
    db["clockDomain"] = "uptime"
    db["quests"][7]["lastSeenAt"] = 1000             # 记录回到本次开机秒
    L.Prune(db, 1_700_000_500)                      # 反过来：now 纪元、记录开机秒
    if db["quests"][7] is None:
        return fail("prune_never_mixes_clock_domains", "反向跨域同样不该清")
    db["clockDomain"] = "epoch"
    db["quests"][7]["lastSeenAt"] = 1_600_000_000
    L.Prune(db, 1_600_000_000 + 25 * 86400)         # 同域且真超三个 CD：要清
    if db["quests"][7] is not None:
        return fail("prune_never_mixes_clock_domains", "同域超期应清出，体积清理不能失效")
    ok("prune_never_mixes_clock_domains")


def test_refresh_wake_is_capped():
    """到期/排程时刻一旦被写得很远（这里用周常排程值，本会给出 ~34 天的 due），
    唤醒定时器也只排到封顶值：排到几周之后等于矩阵开着再也不更新，玩家只能手点刷新。
    封顶后每 6 小时多问一次池，只在矩阵开着时发生，比"永远不刷新"便宜。
    （矩阵开着时账本里有注册周常，NextRefreshAt 取最早 expiresAt = 它的排程尾巴，
    所以排程值远 = 唤醒排得远；封顶只压 delay，不新增定时器。）"""
    lua, gql, _ = sync_env([(153, 30000000)], reset=3_000_000, extra=("Matrix",))
    gql.Sync.SetPolling(True)
    lua.execute("GQL.Sync.SnapshotNow()")           # 排一次唤醒：due 由周常排程值给到很远
    delays = lua.execute("local s = '' for _, t in ipairs(GQL_NEWTIMERS) do "
                         "if not t.cancelled then s = s .. tostring(t.delay) .. ',' end end return s")
    if str(delays).strip(",") == "":
        return fail("refresh_wake_is_capped", "没排任何唤醒定时器")
    for part in str(delays).strip(",").split(","):
        if float(part) > 6 * 3600 + 60:
            return fail("refresh_wake_is_capped",
                        f"唤醒排到 {part} 秒后，没封到 6 小时")
    ok("refresh_wake_is_capped")


def _lua_factory_body(lines):
    """找到 Ledger.NewQuestRecord 自己的函数体（首尾行号），守卫只对它放行。
    工厂被删掉时返回 None —— 那种情况用例直接报红，不许"没得查所以通过"。"""
    for i, raw in enumerate(lines):
        if raw.startswith("function L.NewQuestRecord"):
            for j in range(i + 1, len(lines)):
                if lines[j].rstrip() == "end":
                    return (i, j)
            return (i, len(lines) - 1)
    return None


# 一条任务记录的"形状"长什么样：观测表是唯一天生要在的字段，所以这三条样式
# 出现在工厂以外 = 又开了一份字段清单（本轮金币赖行/状态串轮那批 bug 的共同根因）。
QUEST_RECORD_SHAPES = (
    (r"db\.quests\[[^\]]+\]\s*=\s*\{", "直接往账本里塞一张字面量表当任务记录"),
    (r"\brec\s*=\s*\{", "给任务记录变量赋一张新的字面量表"),
    (r"\bobservations\s*=\s*\{", "自己写了一份 observations 初值（工厂的活儿）"),
)


def test_quest_records_only_come_from_the_factory():
    """任务记录的形状只在 Ledger.NewQuestRecord 一处定义，字段清单只在
    Ledger.QuestRecordFields 一处声明。此前一条记录 28 个字段散在 6 个文件里、
    构造点三处各写一份 `observations = {}`，其余字段全靠"谁用到谁加"——
    同一件事存在多处、读法有两套，正是本轮金币赖行与状态串轮这批 bug 的共同根因。
    存储结构不动（本轮不合并字段），先把清单收成唯一出处并让静态守卫盯住它：
      · 工厂以外任何"新建一条任务记录"的写法一律报红；
      · rec.<字段> 只要不在清单里就报红 —— 要么补进清单（并在 Ledger 里说清归谁写），
        要么那个 rec 根本不是任务记录（角色行 db.chars、补录行 db.manual 都别叫 rec，
        改局部变量名，别把字段硬塞进清单）。
    清单从 Lua 侧读，守卫盯的就是运行时真正那一份；改 Lua 不用改这条用例。
    注释行不参与检查：注释里提字段名/贴一段老代码是说明，不是写入。"""
    lua, _ = load_lua("Config", "Ledger")
    have = lua.execute("return GQL.Ledger.QuestRecordFields ~= nil "
                       "and GQL.Ledger.NewQuestRecord ~= nil")
    if not have:
        return fail("quest_records_only_come_from_the_factory",
                    "Ledger 里没有 QuestRecordFields/NewQuestRecord —— 记录形状又没出处了")
    declared = set(str(k) for k in lua.execute(
        "local t = {} for k in pairs(GQL.Ledger.QuestRecordFields()) do t[k] = true end "
        "return t").keys())
    if "observations" not in declared:
        return fail("quest_records_only_come_from_the_factory",
                    "清单里没有 observations —— 工厂建的形状和清单对不上")

    built, unknown = [], []
    for name in sorted(os.listdir(PLUGIN_DIR)):
        if not name.endswith(".lua"):
            continue
        with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        factory = _lua_factory_body(lines)
        for i, raw in enumerate(lines):
            if factory and factory[0] <= i <= factory[1]:
                continue            # 唯一允许的构造点
            code = raw.split("--", 1)[0]
            for pattern, why in QUEST_RECORD_SHAPES:
                if re.search(pattern, code):
                    built.append(f"{name}:{i + 1}（{why}）")
            for m in re.finditer(r"\brec\.([A-Za-z_][A-Za-z0-9_]*)", code):
                if m.group(1) not in declared:
                    unknown.append(f"{name}:{i + 1} rec.{m.group(1)}")
    if built:
        return fail("quest_records_only_come_from_the_factory",
                    "工厂以外还在造任务记录，改用 GQL.Ledger.NewQuestRecord(): " + ", ".join(built))
    if unknown:
        return fail("quest_records_only_come_from_the_factory",
                    "字段清单缺这些（或那个 rec 不是任务记录，改局部变量名）: "
                    + ", ".join(sorted(set(unknown))))
    ok("quest_records_only_come_from_the_factory")


def test_state_and_registered_state_share_precedence():
    """两份状态判定的优先级必须完全一致，只有新鲜度门不同（本轮 L.RoundAt vs 本 CD 锚点）。
    这里用"手动补录压过交任务凭据"这一条同形断言钉两处：上一版只有 State 那条路径
    按本轮判，注册行那份读 db.manual 的字段清单也各写一份。
    Matrix 的"清除补录"走 SetManual(state="open")，当场就被当成清除，所以存着 open 的
    这一份只能直接写进 db.manual（老存档形状；断的是读取优先级，不是写入口）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(8, 100000, None)]), 1000)
    db["manual"]["E-永恒之井"] = lua.table_from({
        8: lua.table_from({"state": "open", "t": 1200})})
    L.OnTurnIn(db, "E-永恒之井", 8, 1150, 100000, str(db["quests"][8]["currentSig"]))
    if state1(L.State, db, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "世界任务行：手动补录应压过交任务凭据")
    if state1(L.RegisteredState, db, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "注册行：同一份手动凭据却读成别的——两份判定已经漂移")
    # 没有时刻的交任务流水（老存档形状：L.WeekStats 自己就写着 `if r.t and ...`）：
    # 两份都不许把它当"实测交任务"。世界任务行在 Task 4 已经拒绝，注册行那份靠
    # ThisCycle 挡 —— 可锚点定不出来时 cycleAnchor 就是 0，ThisCycle 一进门就放行，
    # 一个时间都没有的流水照样被读成 done。同一份账本两处报两个状态 = 两份各写的代价。
    bare = ledger_db(lua)
    L.DiffPool(bare, pool(lua, [(8, 100000, None)]), 1000)
    bare["turnins"]["E-永恒之井"] = lua.table_from({
        8: lua.table_from([lua.table_from({"g": 100000})])})
    if state1(L.State, bare, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "世界任务行：没有时刻的流水不该算本轮已交")
    if state1(L.RegisteredState, bare, "E-永恒之井", 8) != "unknown":
        return fail("state_and_registered_state_share_precedence",
                    "注册行把没有时刻的流水读成 %s：说不出它属于哪一周就报 unknown，"
                    "两道新鲜度门不许一边松一边紧"
                    % state1(L.RegisteredState, bare, "E-永恒之井", 8))
    # 连 db.manual 都没建起来的账本 = "没有这条来源"，不是当场抛错的理由：
    # 上一版只有注册行判了这一层，世界任务行直接拿 db.manual[charKey] 索引 nil。
    noManual = ledger_db(lua)
    L.DiffPool(noManual, pool(lua, [(8, 100000, None)]), 1000)
    L.OnTurnIn(noManual, "E-永恒之井", 8, 1150, 100000,
               str(noManual["quests"][8]["currentSig"]))
    noManual["manual"] = None
    if state1(L.State, noManual, "E-永恒之井", 8) != "done":
        return fail("state_and_registered_state_share_precedence",
                    "缺 manual 表时世界任务行应照旧认实测交任务")
    if state1(L.RegisteredState, noManual, "E-永恒之井", 8) != "done":
        return fail("state_and_registered_state_share_precedence",
                    "缺 manual 表时注册行应给出同一个答案")
    ok("state_and_registered_state_share_precedence")


def test_gold_verdict_is_the_single_gold_entry():
    """"这条给不给金币"只有一个口径：本轮读到过（含双模式另一档的粘滞，见 L.QuestMoney）= 是；
    客户端明确答过非金币 = 否；压根问不出 = nil（UI 显示 ?），绝不当成"客户端说它不给钱"。
    Dump 的"金币任务"节与矩阵行准入过去各判各的（前者只看 obs.gold，后者看粘滞金额），
    同一份账本两处报两个数。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(9, 100000, None)]), 1000)
    if L.GoldVerdict(db, 9) is not True:
        return fail("gold_verdict_is_the_single_gold_entry", "本轮读到金币应判是")
    # 双模式：当前这一档读到非金币，但本轮另一档读到过 → 仍然是金币任务
    db["quests"][9]["goldMoney"] = 100000
    db["quests"][9]["goldRound"] = L.RoundAt(db, db["quests"][9])
    obs_sig = str(db["quests"][9]["currentSig"])
    db["quests"][9]["observations"][obs_sig]["money"] = 0
    db["quests"][9]["observations"][obs_sig]["gold"] = False
    if L.GoldVerdict(db, 9) is not True:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "本轮另一档读到过金币，切档不许判成非金币")
    if len(L.GoldQuestIDs(db)) != 1:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "导出的金币任务节没走同一个口径")
    # 客户端明说这一档不给金币（读到的是 0，且本轮另一档也没读到过）：判否
    notGold = ledger_db(lua)
    L.DiffPool(notGold, pool(lua, [(10, 0, [888])]), 1000)
    if L.GoldVerdict(notGold, 10) is not False:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "读到 0 = 客户端答过不给金币，应判否")
    # 完全没有来源的记录：既不许说"是"，也不许替客户端答"不给钱"——报不出就报 nil
    silent = ledger_db(lua)
    silent["quests"][11] = L.NewQuestRecord()
    if L.GoldVerdict(silent, 11) is not None:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "压根没读到的那条应报 nil（UI 显示 ?），不许折成是/否")
    if len(L.GoldQuestIDs(silent)) != 0:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "问不出的任务不许混进导出的金币节")
    # 上一轮读到过金币、本轮还没读到：粘滞不许翻回"是"（L.QuestMoney 按轮次拒了它），
    # 也不许折成"否"——客户端本轮根本没答，UI 该显示 ?。
    stale = ledger_db(lua)
    stale["quests"][12] = L.NewQuestRecord()
    stale["quests"][12]["goldMoney"] = 100000
    stale["quests"][12]["goldRound"] = 1          # 对不上任何一轮的起点 = 上个 CD 的数
    if L.GoldVerdict(stale, 12) is not None:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "过期粘滞该报 nil（本轮没读到），不许折成客户端说过不给钱")
    ok("gold_verdict_is_the_single_gold_entry")


def test_map_source_labels_are_constants():
    """mapSrc 取值只有一个出处（Config.MAP_SRC）：这些串决定分组、族搬运、清理、
    周常归组，散成裸字符串打错一个字就静默失效，现有测试一条也发现不了。"""
    lua, gql = load_lua("Config", "Ledger", "Api")
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "GoldQuestLedger")
    table = lua.execute("return GQL.Config.MAP_SRC")
    labels = set()
    for v in table.values():
        s = str(v)
        if not s.endswith(" "):          # healPrefix/familyPrefix 是前缀，不进精确匹配
            labels.add(s)
    offenders = []
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".lua") or fn == "Config.lua":
            continue
        src = open(os.path.join(src_dir, fn), encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), start=1):
            code = line.split("--", 1)[0]
            for m in re.finditer(r'mapSrc\s*=\s*"([^"]*)"', code):
                # 一律不许写成串：哪怕串和 Config.MAP_SRC 抄得一模一样。
                # 抄对了只是"现在还对得上"，两处出处迟早分岔 —— 这正是本用例要防的事
                known = "（串抄对了，但仍该走常量）" if m.group(1) in labels else ""
                offenders.append(f"{fn}:{i} mapSrc 裸串{known}")
            for m in re.finditer(r'"([^"]*池行自带[^"]*|[^"]*玩家指定[^"]*|'
                                 r'[^"]*旧记录未标来源[^"]*|[^"]*存疑：[^"]*|'
                                 # Dump 手抄过的两个变体（"没"写成了"未"、"池行/未标来源"）：
                                 # 不进黑名单就等于给下一次抄写留着门
                                 r'[^"]*旧记录没标来源[^"]*|[^"]*池行/未标来源[^"]*)"', code):
                offenders.append(f"{fn}:{i} 来源串写死在判断里")
    offenders = [o for o in offenders if "MAP_SRC" not in o]
    if offenders:
        return fail("map_source_labels_are_constants",
                    "这些标签该走 Config.MAP_SRC: " + ", ".join(sorted(set(offenders))))
    ok("map_source_labels_are_constants")


def test_map_source_blacklist_is_built_from_the_same_table():
    """判定表里的"这不算客户端答复"必须和 Config.MAP_SRC 是同一批串：
    手抄一份就会和写入方分岔（本轮把 label 改成从常量取之后，最怕的就是串改了、
    黑名单还在比旧字面量 —— 玩家口述的图会被当成客户端答复，归组当场错位）。
    四个非客户端来源逐个验，再加一条"池行自带"反向确认白名单没被一起砍掉。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    M = lua.execute("return GQL.Config.MAP_SRC")
    for key in ("manual", "legacy", "suspect", "asked"):
        rec = lua.table_from({"mapID": 2400, "mapSrc": str(M[key])})
        if L.ClientMapOf(rec) is not None:
            return fail("map_source_blacklist_is_built_from_the_same_table",
                        f"{key}（{M[key]}）不该算客户端答复")
    rec = lua.table_from({"mapID": 2400, "mapSrc": str(M["pool"])})
    if int(L.ClientMapOf(rec)) != 2400:
        return fail("map_source_blacklist_is_built_from_the_same_table",
                    "池行自带的图不许一起被砍（客户端直答的那条才是凭据）")
    ok("map_source_blacklist_is_built_from_the_same_table")


def test_migrate_runs_once_per_schema():
    """schema 升级只在版本落后时跑，跑一次就把档位推上去：
    每轮快照都判一遍 = 每次同步多一趟全账本扫描，而且坏数据修复的语义变成"看运气"。"""
    lua, gql = load_lua("Config", "Ledger")
    C = gql.Config
    g = lua.globals()
    g.GQLDB = lua.table_from({})         # 非 mock 环境没有 SavedVariables，这里自建
    db = g.GQLDB
    db["quests"] = lua.table_from({})
    db["quests"][3] = lua.table_from({
        "observations": lua.table_from({"-1|": lua.table_from({"money": 100000, "gold": True})}),
        "goldMoney": 100000, "goldRound": 500,
    })
    db["schema"] = 1
    db["stickyGoldReset"] = 1            # 上一版的档位：这一版必须重新清
    ran = lua.execute("return GQL.Config.Migrate(GQL.Config.DB())")
    if ran is not True:
        return fail("migrate_runs_once_per_schema", "Migrate 没跑或返回不对")
    if int(db["schema"]) != int(C.SCHEMA_VERSION):
        return fail("migrate_runs_once_per_schema",
                    f"跑完要推到 SCHEMA_VERSION，实际 {db['schema']}")
    if db["quests"][3]["goldMoney"] is not None:
        return fail("migrate_runs_once_per_schema", "坏金币凭据没被清（迁移没接到这一棒）")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail("migrate_runs_once_per_schema", "同一档不该再跑第二趟")
    ok("migrate_runs_once_per_schema")


def test_migrate_failure_keeps_ledger_usable():
    """升级函数抛错时：档位停在上一档（下次登录重试同一档）、原始表不重建、留一条日志。
    直接重建等于把用户三个 CD 的金币记录抹掉，比坏数据严重得多。"""
    lua, gql = load_lua("Config", "Ledger")
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    db = g.GQLDB
    db["schema"] = 1
    lua.execute("GQL.Config.MIGRATIONS[2] = function() error('模拟升级函数抛错') end")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail("migrate_failure_keeps_ledger_usable", "抛错要报失败")
    if int(db["schema"]) != 1:
        return fail("migrate_failure_keeps_ledger_usable", "失败后档位不该前进")
    if "升级失败" not in str(gql.Config.GetLogs()):
        return fail("migrate_failure_keeps_ledger_usable", "失败必须留日志")
    ok("migrate_failure_keeps_ledger_usable")


def test_migrate_drops_turnins_with_no_evidence_behind_them():
    """Task 2 之前，交一条普通任务（职业/战役任务）也会写进 turnins —— 那些凭据
    是矩阵六道准入门之一，老存档里已经躺了一批。一次性清掉"账本里查不到可信身份"
    的凭据：清掉的代价是那一行少报一次（下一次上板就自己长回来），
    留着的代价是非金币任务永远赖在矩阵里骗玩家。"""
    lua, gql = load_lua("Config", "Ledger")
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    lua.execute("""
        local db = GQL.Config.DB()
        db.quests = db.quests or {}
        db.quests[5001] = { poolSeen = true, observations = {} }   -- 有身份：这条要留
        k = "角色|阵营|服"
        db.turnins = { [k] = {
            [5001] = { { t = 10, g = 30000 } },
            [7777] = { { t = 10, g = 0 } },                        -- 账本里根本没这条
        } }
        db.schema = 2
    """)
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail("migrate_drops_turnins_with_no_evidence_behind_them", "v3 没跑")
    if lua.execute("return GQL.Config.DB().turnins[k][7777] ~= nil"):
        return fail("migrate_drops_turnins_with_no_evidence_behind_them",
                    "没身份支撑的交任务凭据没被清")
    if lua.execute("return GQL.Config.DB().turnins[k][5001] == nil"):
        return fail("migrate_drops_turnins_with_no_evidence_behind_them",
                    "有池证据的凭据被误伤（判据不能只看 questID 在不在）")
    ok("migrate_drops_turnins_with_no_evidence_behind_them")


def test_minimap_drag_survives_scale_call():
    """拖动那一帧读小地图缩放必须带 self 且取不到就退回 1：
    上一版 `pcall(Minimap.GetEffectiveScale, Minimap) and Minimap.GetEffectiveScale()`
    后半是点调用没带 self，pcall 成功时它当场抛错，且这句不在任何保护里。
    拖动/放环还不许直连全局 Minimap——宿主由 EnsureMapButton 用 Api.Resolve 拿到后
    放上值：全局被暴雪改名/挪走（12.x 有先例）时，降级判查看的是 host、读的时候
    换成全局等于绕过那道判断，按钮已建好的情况下拖动帧会当场崩。"""
    lua, gql, _ = sync_env([(160, 30000000)], extra=("Matrix",))
    btn = gql.Matrix.EnsureMapButton()
    if btn is None:
        return fail("minimap_drag_survives_scale_call", "替身里按钮没建起来")
    # ① 全局 Minimap 还在时拖一帧：点调用没带 self 的写法在这里就抛
    ok1 = lua.execute("""
        local b = GQLMapButton
        return pcall(function()
            b.__scripts['OnDragStart'](b)
            local u = b.__scripts['OnUpdate']
            if u then u(b, 0.1) end
        end)
    """)
    # ② 全局随后没了（宿主引用留在上值里）：拖动帧和 OnDragStop 都不许抛
    lua.execute("Minimap = nil")
    ok2 = lua.execute("""
        local b = GQLMapButton
        return pcall(function()
            b.__scripts['OnDragStart'](b)
            local u = b.__scripts['OnUpdate']
            if u then u(b, 0.1) end
            local s = b.__scripts['OnDragStop']
            if s then s(b) end
        end)
    """)
    logs = str(gql.Config.GetLogs())
    if ok1 is not True:
        return fail("minimap_drag_survives_scale_call",
                    f"全局还在时拖动帧就抛错（点调用没带 self）:\n{logs}")
    if ok2 is not True:
        return fail("minimap_drag_survives_scale_call",
                    f"全局 Minimap 没了以后拖动帧抛错（直连全局绕过 host 上值）:\n{logs}")
    if lua.execute("return GQL.Config.DB().minimapAngle == nil"):
        return fail("minimap_drag_survives_scale_call", "拖动帧应把角度写进存档")
    ok("minimap_drag_survives_scale_call")


def test_options_wheel_matches_matrix_direction():
    """两个面板的滚轮方向必须一致：这个客户端往下滚给的是负 delta，矩阵是
    offY = offY - delta*40（Matrix.lua 的 OnMouseWheel，注释写明"加 delta 的手感是反的"）。
    设置面板滚动条 0=顶、值越大越靠下（OnValueChanged 直接喂 SetVerticalScroll），
    所以往下滚（delta<0）时滚动条值要变大 = 面板同样要"减 delta"。
    上一版注释写"与矩阵一致"、代码却是 value + delta*STEP，两个面板方向相反。"""
    lua, gql, _ = sync_env([(161, 30000000)], extra=("Matrix", "Options"))
    gql.Options.Open()
    moved = lua.execute("""
        local bar = GQLOptScrollBar
        if not bar then return nil end
        bar.__shown = true            -- 内容真超屏时 Layout 才 Show；用例直接模拟"已显示"
        bar:SetMinMaxValues(0, 1000)
        bar:SetValue(500)
        local wheel = GQLOptScroll.__scripts['OnMouseWheel']
        if not wheel then return nil end
        wheel(GQLOptScroll, -1)       -- 往下滚（这个客户端给负 delta）→ 值该变大
        local down = bar.__value
        bar:SetValue(500)
        wheel(GQLOptScroll, 1)        -- 往上滚 → 值该变小
        return down, bar.__value
    """)
    if moved is None:
        return fail("options_wheel_matches_matrix_direction",
                    "替身里找不到设置面板的滚动条或滚轮脚本")
    down, up = moved
    if float(down) <= 500 or float(down) > 1000:
        return fail("options_wheel_matches_matrix_direction",
                    f"往下滚应让滚动条值变大（与矩阵减 delta 同向），实际 {down}")
    if float(up) >= 500 or float(up) < 0:
        return fail("options_wheel_matches_matrix_direction",
                    f"往上滚应让滚动条值变小，实际 {up}")
    ok("options_wheel_matches_matrix_direction")


def test_panel_hunt_button_survives_missing_sync():
    """（旧 options_hunt_button 用例的口径原样搬家，入口从设置面板挪到关键词面板左栏底部）
    同步模块没加载时点"补扫跟踪地图"不许抛错：上一版直接
    `pcall(GQL.Sync.HuntKeywordMaps)` —— 被 pcall 的参数 `GQL.Sync.HuntKeywordMaps`
    是在保护**外**求值的，GQL.Sync 为 nil 时"attempt to index a nil value"
    当场炸出 OnClick，pcall 根本没护到。要先判空（本文件别处同款写法）再调用。"""
    lua, gql, _ = sync_env([(162, 30000000)], extra=("Matrix", "Options"))
    gql.Options.OpenKeywordPanel()
    found = lua.execute("""
        for _, c in ipairs(GQLKeywordPanel.__children) do
            if c.__text == "补扫跟踪地图" then GQL_HUNTBTN = c return true end
        end
        return false
    """)
    if found is not True:
        return fail("panel_hunt_button_survives_missing_sync",
                    "新窗口栏底没找到补扫按钮（它应直接挂在窗口下，不埋在滚动区里）")
    lua.execute("GQL.Sync = nil")
    okCall = lua.execute("""
        return pcall(function()
            local b = GQL_HUNTBTN
            b.__scripts['OnClick'](b)
        end)
    """)
    if okCall is not True:
        return fail("panel_hunt_button_survives_missing_sync",
                    "Sync 没加载时点补扫抛错（参数在 pcall 外求值）")
    ok("panel_hunt_button_survives_missing_sync")


def manual_add_env(*ids):
    """手动添加那两条用例共用的环境：一个可控倒计时 + 一个能读出门槛金币的池
    （1000 金 = 10 * 10000 * 100，稳稳过 500 金那道门槛）。

    走真实入口（sync_env 起 Core+Config+Ledger+Api+Sync，两次 SnapshotNow 推账本），
    不手搓 rec 字段 —— 手写的形状测的是"我以为账本长这样"，不是玩家游戏里那份。
    """
    rows = [(qid, 10 * 10000 * 100) for qid in ids]
    lua, gql, _ = sync_env(rows)
    g = lua.globals()
    secs = {"v": 43200}
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: secs["v"]})

    def read(qid, field):
        return one(lua.execute(
            "local rec = GQL.Config.DB().quests[%d] return tostring(rec and rec.%s)"
            % (qid, field)))

    def listed():
        return one(lua.execute(
            "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), GQL_NOW) local s = ',' "
            "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
            "  s = s .. ',' .. r.questID .. ',' end end return s"))

    return lua, gql, g, secs, read, listed


def roll_pool(lua, g, rows):
    """把池换成 rows: [(questID, money)]（money=None = 这一行不带金额）。
    金额链一律答"数据在本地、这条没有金币"⇒ Api 把 0 当真实数写进账本。
    时刻与倒计时由调用方自己推：换不换轮正是两条用例的分岔口。"""
    arr = lua.table_from({})
    for i, (qid, money) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": 2007, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        arr[i] = lua.table_from(entry)
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: arr})
    # 这一读客户端明说"这副奖励没有金币"：金额链一个字没答，但奖励数据就在本地
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.HaveQuestRewardData = lambda qid: True


def test_a_click_add_does_not_force_the_row_onto_the_matrix():
    """点"添加任务"不再把一行强制送上矩阵（用户 2026-10-27 的原话：手动加回来对应的是
    "忽略错了 再加回来，其意图是 取消 忽略 而不是标记 加了"）。
    上一版（§26）把这个点记成 `manualAdded`，还让它当矩阵的六道准入门之一 —— 绕过金币
    门槛、破"一族一行"、免同名去重、冒充交任务记账的可信身份，这些例外全是一个玩家根本没有
    的意图换来的。本轮字段整体下线：一条任务在不在矩阵，只由客户端凭据（在不在板上 /
    本轮金币 / 命中关键词 / 注册周常 / 本轮有人交过）和玩家配置说了算，
    `AddQuest` 的玩家侧含义只剩"撤掉那句别再显示"。
    三条都在池里、都不命中关键词：141 点一次添加、142 什么都不做（对照组）、
    143 本轮读到门槛金币（凭据对照组）。
    断言 141 与 142 都**不列**、`manualAdded` 根本没被写进去；143 照旧在列
    （本轮金币这道门不许被牵连）。
    再让 141 在下一轮读到门槛金币：它靠金币门回来 —— 这才证明"能不能上板"说的是客户端，
    不是那一次点击。"""
    name = "a_click_add_does_not_force_the_row_onto_the_matrix"
    lua, gql, g, secs, read, listed = manual_add_env(141, 142, 143)
    g.GQL_NOW = 100000
    # 三条都在池里，只有 143 本轮读到门槛金币（1000 金 > 500 金门槛）
    roll_pool(lua, g, [(141, 0), (142, 0), (143, 10 * 10000 * 100)])
    gql.Sync.SnapshotNow()
    lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), 141, GQL_NOW, nil)")  # 玩家点了一次添加

    if read(141, "manualAdded") != "nil":
        return fail(name, f"点添加不许再写 manualAdded（字段本轮下线），"
                          f"实际 {read(141, 'manualAdded')}")
    got = listed()
    if ",141," in got:
        return fail(name, f"点一次添加不该把非金币的行强制送上矩阵：{got}")
    if ",142," in got:
        return fail(name, f"没点过也没金币的对照组更不该在列：{got}")
    if ",143," not in got:
        return fail(name, f"本轮读到门槛金币的那条照旧该在列：{got}")

    # 下一轮快照给 141 读到门槛金币：它靠金币门回来（倒计时不动 ⇒ 还是同一条实例）
    roll_pool(lua, g, [(141, 10 * 10000 * 100), (142, 0), (143, 10 * 10000 * 100)])
    g.GQL_NOW = 100100
    gql.Sync.SnapshotNow()
    got = listed()
    if ",141," not in got:
        return fail(name, f"141 这一轮读到门槛金币，该靠金币门重新上板：{got}")
    if ",142," in got:
        return fail(name, f"没金币也没凭据的对照组不许跟着回来：{got}")
    ok(name)


def test_migrate_clears_manual_added_from_old_saves():
    """老存档里那份"点一次添加永久有效"的 manualAdded 在登录时一次性作废（schema v4）。
    上一版这个标记跨轮、跨 CD 都不作废；判据现在收窄成"本轮点过才算"（RollRound），
    可那只对**下一次换轮**生效 —— 已经写进 SavedVariables 的那一份不会自己长回来，
    不清的话玩家要等下一次 12 小时刷新才看得见修法生效（上一版 ResetStickyGoldOnce
    就是栽在这上面，"代码改完、矩阵一点变化没有"，玩家二次报修）。
    三条凭据各异的记录走真实入口建（快照 + AddQuest + EnsureRegistered），再手改
    db.schema = 3 冒充上一版写盘的存档，形状照 12.1.5 真存档那几条 manualAdded
    （88679 意外遭遇 = 本轮明说不给金币 / 95429 健步如飞 = 本轮读到过金币 /
    98172 追踪萨拉塔斯 = 命中玩家自己配的关键词），外加一条注册周常（门 5，
    真存档里 89377/89347 那种常驻行）当"不许误伤"的靶子。
    迁移只清标记、不替门槛判活：清完该在列的照旧在列（B 靠本轮金币粘滞、C 靠关键词、
    D 靠 regKey），只有全靠那次点名撑着的 A 不在列 —— 这条差别才是玩家真会看见的。
    **第二十七轮的口径变化**（本用例因此改过前置，读代码时别看岔）：manualAdded 这个
    字段整体下线，AddQuest 不再写它、MatrixRows 不再认它当准入门。所以
    ① 存档里那份标记现在只能手写（模拟老版本写盘的形状，v4 管的正是这份残留）；
    ② "A 靠它撑着才在列"这个**迁移前**的状态在新代码里已经复现不出来了 —— A 在迁移前
       就不在列，v4 剩下的作用是把这个僵尸字段从存档里抹干净（Dump/Probe 取证才不会
       再报"玩家手动添加"），档位阶梯 v3→v4→v5 也必须连续，故本档不删。"""
    name = "migrate_clears_manual_added_from_old_saves"
    ALL = (151, 152, 153, 154)
    lua, gql, g, secs, read, listed = manual_add_env(*ALL)
    g.GQL_NOW = 100000
    # 本轮金额凭据照真存档那几条造：A=88679（客户端明说不给金币）、
    # B=95429（本轮读到过 1000 金）、C=98172 与 D=注册周常都不给金币
    roll_pool(lua, g, [(151, 0), (152, 10 * 10000 * 100), (153, 0), (154, 0)])
    gql.Sync.SnapshotNow()
    # 关键词现造这一条，别依赖默认值：清完标记后 C=153 就靠它占行（门 3）
    lua.execute("GQL.Config.AddKeyword('任务153')")
    if one(lua.execute("return tostring(GQL.Config.DB().config.keywords[1])")) != "任务153":
        return fail(name, "前置：关键词没写进 db.config.keywords")
    # D = 注册周常（门 5，丰饶供品那种常驻行）：key 不带 kw: 前缀，才不会被
    # "删关键词一起退行"那条逻辑牵连（它不是靠关键词进来的）
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), { { "
                "key = 'migrate-weekly-D', questID = 154, name = '任务154', onBoard = true } }, "
                "GQL.Sync.CurrentCharKey(), GQL_NOW)")
    if read(154, "regKey") != "migrate-weekly-D":
        return fail(name, f"前置：D 该是注册周常（门 5），实际 regKey={read(154, 'regKey')}")
    for qid in ALL:
        lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), %d, GQL_NOW, nil)" % qid)
    # 手写这份标记：AddQuest 从第二十七轮起不再写 manualAdded（字段整体下线，
    # 点添加只撤销忽略），而 v4 要清的是**老版本**已经写进 SavedVariables 的那一份
    for qid in ALL:
        lua.execute("GQL.Config.DB().quests[%d].manualAdded = true" % qid)

    # 四条实例都没换（结束时刻不动），只把 B 的奖励翻到"这一副不给金币"那档（双模式切档）：
    # 于是本轮 B 手上只剩 goldMoney/goldRound 那份粘滞（门 1），A、C、D 从来就没有金额
    roll_pool(lua, g, [(151, 0), (152, 0), (153, 0), (154, 0)])
    g.GQL_NOW = 100100
    secs["v"] = 43100                # 100100+43100 = 143200 = 上一轮的 roundEnd ⇒ 还是同一条实例
    gql.Sync.SnapshotNow()

    for qid in ALL:
        if read(qid, "manualAdded") != "true":
            return fail(name, f"前置：{qid} 该带着老存档那份手动添加的标记")
    for qid in (151, 153, 154):
        if read(qid, "goldMoney") != "nil":
            return fail(name, f"前置：{qid} 本轮不该有金额凭据（A 才是"
                              "老规则下全靠手动添加撑着的那一条）")
    if read(152, "goldRound") != read(152, "roundAt"):
        return fail(name, "前置：B 的金币粘滞该属于本轮（goldRound == roundAt）")
    if one(lua.execute("return tostring(GQL.Ledger.QuestMoney(GQL.Config.DB(), 152))")) \
            != "10000000":
        return fail(name, "前置：B 本轮读到过的金币该还在（门 1 的证据）")
    if one(lua.execute("return tostring(GQL.Ledger.QuestMoney(GQL.Config.DB(), 151))")) != "0":
        return fail(name, "前置：A 这一读该是客户端明说的 0（不给金币）")
    pre = listed()
    # 新代码已经不认 manualAdded 这道门，A 在迁移前就不在列（见文档字符串口径变化②）
    for qid in (152, 153, 154):
        if f",{qid}," not in pre:
            return fail(name, f"前置：金币（B）、关键词（C）、注册周常（D）三条该在列，"
                              f"实际列了 {pre}")
    if ",151," in pre:
        return fail(name, f"前置：手动标记已不是准入门，A 不许因为在列就把迁移变成看不见的空操作，"
                          f"实际列了 {pre}")

    lua.execute("GQL.Config.DB().schema = 3")     # 冒充上一版写盘的老存档
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "v4 没接上这一棒：Migrate 对 schema=3 的存档没跑新档")
    if int(lua.execute("return GQL.Config.DB().schema")) < 4:
        return fail(name, "跑完档位该前进到 4（v4 没跑或被别的档跳过了）")
    if int(lua.execute("return GQL.Config.DB().schema")) != int(gql.Config.SCHEMA_VERSION):
        return fail(name, "跑完该停在最新档，实际 "
                          f"{lua.execute('return GQL.Config.DB().schema')}")
    for qid in ALL:
        if read(qid, "manualAdded") != "nil":
            return fail(name, f"老存档那份永久标记没作废（{qid}.manualAdded 还在，"
                              "取证层会一直报\"玩家手动添加\"）")
    # 返回值（清掉几条）要能被日志看见：档位照样前进、迁移函数却是个空壳的写法，
    # 只看 schema 是查不出来的（Migrate 把取不到的档位当 0 条处理，不报错）
    if "数据升级 v4 完成（处理 4 条）" not in str(gql.Config.GetLogs()):
        return fail(name, f"v4 该报出清掉的条数，实际日志：{gql.Config.GetLogs()}")
    if one(lua.execute("return tostring(GQL.Config.DB().quests[152].goldMoney)")) != "10000000":
        return fail(name, "迁移不该顺手清本轮金币凭据（v4 只管手动标记）")
    if read(154, "regKey") != "migrate-weekly-D":
        return fail(name, f"注册周常的 regKey 被牵连了（实际 {read(154, 'regKey')}）—— "
                          "v4 只清手动添加那一份，门 5 是常驻行的凭据不是玩家的点名")
    post = listed()
    if ",152," not in post or ",153," not in post or ",154," not in post:
        return fail(name, f"本轮金币（B）、关键词（C）、注册周常（D）三行不许"
                          f"因为清标记而掉，实际列了 {post}")
    if ",151," in post:
        return fail(name, f"没有客户端凭据的 A 不该回到矩阵（真存档 88679「意外遭遇」那一行），"
                          f"实际列了 {post}")

    # 档位闸门：v4/v5 只跑一趟 —— 迁移之后新写进去的标记不许被下次登录吃掉。
    # 用"标记非空即跳过"当闸门的写法在这里就露馅（ResetStickyGoldOnce 踩过同一处）。
    # 新代码的 AddQuest 已经不写 manualAdded，所以这一趟只能手写，验的是档位本身。
    lua.execute("GQL.Config.DB().quests[151].manualAdded = true")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "已是最新档不该再跑一趟")
    if read(151, "manualAdded") != "true":
        return fail(name, "迁移又跑了一趟，把升级之后写下的标记清了 —— 档位闸门没守住")
    ok(name)


def test_the_upgrade_clears_every_old_ignore_and_add_mark():
    """升级 v5：老存档里**所有**历史忽略与添加标记一次性作废（用户 2026-10-27 原话：
    "所以你先把所有已经做了忽略 / 添加 的数据 清理掉，然后实现上面真正的意图"）。
    为什么这次连老忽略也一起清（v4 只管手动添加）：忽略的语义本轮从"只管本 CD"改成
    **永久屏蔽**，上一版为了"看一眼就随手点掉"的那次移除，会在新语义里永久压着一行，
    而玩家从没打算永久屏蔽它 —— 那是在拿旧意图当真凭据。
    代价照实（迁移注释与文档都写了）：他真想永久屏蔽的那几条（关键词"丰饶"误命中
    "辉煌丰饶"那种）也一并回到矩阵，得重新右键点一次。
    四条记录走真实入口建（快照 + AddQuest + EnsureRegistered + 现造关键词，别依赖默认值），
    再把 manualAdded / ignored 手工写成老存档的形状（ignored = **时间戳**，上一版就这么存的），
    db.schema = 4 冒充上一版写盘的存档。
    清完之后没有"手点"这一道门了，能不能上板只剩客户端凭据说话：
    B 靠本轮读到的门槛金币回来、C 靠关键词、D 靠 regKey 照旧在列；
    A（本来只靠那次添加撑着）掉出矩阵 —— 这条差别才是玩家在游戏里看得见的。"""
    name = "the_upgrade_clears_every_old_ignore_and_add_mark"
    ALL = (161, 162, 163, 164)
    lua, gql, g, secs, read, listed = manual_add_env(*ALL)
    g.GQL_NOW = 100000
    # A/C/D 这一读客户端明说不给金币，只有 B 本轮读到 1000 金（过 500 金门槛）
    roll_pool(lua, g, [(161, 0), (162, 10 * 10000 * 100), (163, 0), (164, 0)])
    gql.Sync.SnapshotNow()
    # 关键词现造，别依赖默认值：清完标记后 C=163 就靠它占行
    lua.execute("GQL.Config.AddKeyword('任务163')")
    if one(lua.execute("return tostring(GQL.Config.DB().config.keywords[1])")) != "任务163":
        return fail(name, "前置：关键词没写进 db.config.keywords")
    # D = 注册周常（丰饶供品那种常驻行）：key 不带 kw: 前缀，才不被"删关键词一起退行"牵连
    lua.execute("GQL.Ledger.EnsureRegistered(GQL.Config.DB(), { { "
                "key = 'v5-weekly-D', questID = 164, name = '任务164', onBoard = true } }, "
                "GQL.Sync.CurrentCharKey(), GQL_NOW)")
    if read(164, "regKey") != "v5-weekly-D":
        return fail(name, f"前置：D 该是注册周常，实际 regKey={read(164, 'regKey')}")
    for qid in ALL:
        lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), %d, GQL_NOW, nil)" % qid)
    # 结束时刻不动（还是同一条实例），只把 B 的奖励翻到非金币档：
    # 于是 B 手上剩的是本轮那份金币粘滞，A/C/D 从来就没有金额
    roll_pool(lua, g, [(161, 0), (162, 0), (163, 0), (164, 0)])
    g.GQL_NOW = 100100
    secs["v"] = 43100                    # 100100+43100 = 143200 = 上一轮的 roundEnd ⇒ 没换轮
    gql.Sync.SnapshotNow()

    # A、B 各点一次"移除"（真实入口），再把两份标记写成老存档的形状：
    # AddQuest 从本轮起不再写 manualAdded（字段下线），所以这份残留也只能手写
    for qid in (161, 162):
        if one(lua.execute("return tostring((GQL.Ledger.RemoveQuest(GQL.Config.DB(), %d)))"
                           % qid)) != "true":
            return fail(name, f"前置：{qid} 该能走真实入口移除")
        lua.execute("GQL.Config.DB().quests[%d].ignored = 1690000000"
                    " GQL.Config.DB().quests[%d].manualAdded = true" % (qid, qid))
    for qid in (163, 164):
        lua.execute("GQL.Config.DB().quests[%d].manualAdded = true" % qid)
    if read(162, "goldRound") != read(162, "roundAt"):
        return fail(name, "前置：B 的金币粘滞该属于本轮（goldRound == roundAt）")
    pre = listed()
    for qid in (161, 162):
        if f",{qid}," in pre:
            return fail(name, f"前置：{qid} 被老忽略压着，升级前不该在列，实际列了 {pre}")
    for qid in (163, 164):
        if f",{qid}," not in pre:
            return fail(name, f"前置：{qid} 靠关键词/regKey 本来就在列，实际列了 {pre}")

    lua.execute("GQL.Config.DB().schema = 4")     # 冒充上一版写盘的老存档
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "v5 没接上这一棒：Migrate 对 schema=4 的存档没跑新档")
    logs = str(gql.Config.GetLogs())
    # 返回值（清掉几条）要能在日志里看见：档位照样前进、迁移函数却是个空壳的写法，
    # 只看 schema 查不出来（Migrate 把取不到的档位当 0 条、照样把 schema 推上去）
    if "数据升级 v5 完成（处理 4 条）" not in logs:
        return fail(name, f"v5 该报出清掉的条数，实际日志：{logs}")
    if int(lua.execute("return GQL.Config.DB().schema")) != int(gql.Config.SCHEMA_VERSION):
        return fail(name, "跑完该停在最新档，实际 "
                          f"{lua.execute('return GQL.Config.DB().schema')}")
    for qid in ALL:
        if read(qid, "ignored") != "nil":
            return fail(name, f"{qid}.ignored 还在（{read(qid, 'ignored')}）—— "
                              "老存档那份\"只管本 CD\"的移除在新语义里会变成永久屏蔽")
        if read(qid, "manualAdded") != "nil":
            return fail(name, f"{qid}.manualAdded 没清掉 —— v4 之后这字段整体下线")
    if one(lua.execute("return tostring(GQL.Config.DB().quests[162].goldMoney)")) != "10000000":
        return fail(name, "v5 不许顺手清本轮金币凭据（只清两份玩家标记）")
    if read(164, "regKey") != "v5-weekly-D":
        return fail(name, f"注册周常的 regKey 被牵连了（实际 {read(164, 'regKey')}）")
    post = listed()
    for qid, why in ((162, "本轮金币凭据"), (163, "关键词"), (164, "regKey")):
        if f",{qid}," not in post:
            return fail(name, f"{qid} 靠{why}该回到矩阵（取消忽略后由自动判据重算），"
                              f"实际列了 {post}")
    if ",161," in post:
        return fail(name, f"只靠那次添加撑着的 A 该掉出矩阵（门里再没有\"手动\"这一道），"
                          f"实际列了 {post}")

    # 档位闸门：v5 只跑一趟。玩家重新点出来的永久屏蔽不许被下一次登录清掉 ——
    # 用"标记非空即跳过"当闸门的写法在这里露馅（ResetStickyGoldOnce 踩过同一处）
    lua.execute("GQL.Ledger.RemoveQuest(GQL.Config.DB(), 164)")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "已是最新档不该再跑一趟 v5")
    if read(164, "ignored") == "nil":
        return fail(name, "v5 又跑了一趟，把玩家升级后重新点的永久屏蔽清了 —— 档位闸门没守住")
    ok(name)


def roll_the_instance(lua, L, db, qid, money, at, then):
    """让一条在池的世界任务真换上**下一条实例**：先拿接口倒计时定好这一轮的结束时刻，
    再推一次快照（到期时刻已过 + 池里还挂着它 = Ledger.RoundEnded 成立）。
    时刻形状照 12.1.5 真存档 89347：白天点的勾撑到下一轮还在。"""
    L.DiffPool(db, pool(lua, [(qid, money, None)]), at)
    L.SetExpiry(db["quests"][qid], at, 43200)      # 到期 = at+43200，来源=接口倒计时
    L.DiffPool(db, pool(lua, [(qid, money, None)]), then)
    return db


def bind_db(lua, db):
    """把纯账本用例手搓的 db 接成 GQL.Config.DB() 的返回值：
    AddQuest / SetCycleExpiry 这些"账本 + 接口"混合入口都从那儿读库，
    不接这一句它们会去摸替身里根本不存在的 GQLDB。"""
    lua.globals().GQLDB = db
    lua.execute("GQL.Config.DB = function() return GQLDB end")
    return db


def round_at(lua, db, qid):
    return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                           "return tostring(r and r.roundAt)" % qid))


def roll_the_instance(lua, L, db, qid, money, at, then):
    """让一条在池的世界任务真换上**下一条实例**：先拿接口倒计时定好这一轮的结束时刻，
    再推一次快照（到期时刻已过 + 池里还挂着它 = Ledger.RoundEnded 成立）。
    时刻形状照 12.1.5 真存档 89347：白天点的勾撑到下一轮还在。"""
    L.DiffPool(db, pool(lua, [(qid, money, None)]), at)
    L.SetExpiry(db["quests"][qid], at, 43200)      # 到期 = at+43200，来源=接口倒计时
    L.DiffPool(db, pool(lua, [(qid, money, None)]), then)
    return db


def bind_db(lua, db):
    """把纯账本用例手搓的 db 接成 GQL.Config.DB() 的返回值：
    AddQuest / SetCycleExpiry 这些"账本 + 接口"混合入口都从那儿读库，
    不接这一句它们会去摸替身里根本不存在的 GQLDB。"""
    lua.globals().GQLDB = db
    lua.execute("GQL.Config.DB = function() return GQLDB end")
    return db


def round_at(lua, db, qid):
    return one(lua.execute("local r = GQL.Config.DB().quests[%d] "
                           "return tostring(r and r.roundAt)" % qid))


def test_a_hand_mark_expires_when_the_instance_rolls():
    """补录也分轮次（用户 2026-10-27："手动标记也是要分轮次的，这个勾不补不代表
    永远完成了，世界任务都是不断轮换的"）。12.1.5 真存档 89347「战胜未知：怒火攻心的
    漂流者」（屠戮者高地 12 小时区）：换轮已经抓到（roundAt=10-26 14:01、到期 10-27 01:40），
    可 10-25 04:22 点的那笔补录还带着 done —— 本轮一格没勾，界面照样打勾。
    根因：L.State 第一道（补录）是四份凭据里唯一没有轮次门的，流水、快照、按号读数都有。
    钱照旧按 CD 记（那一笔真进过钱包），本轮没交过就是没交过。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 12, 300000, 1000, 20000)
    key = "A-永恒之井"
    L.SetManual(db, key, 12, "done", 1100, 150000)
    if state1(L.State, db, key, 12) != "done":
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "前置：本轮之内点的补录该算数（别把修法做成补录永远不认）")
    if int(L.WeekStats(db, key, 0)[0]) != 1:
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    f"前置：本轮之内的补录该计入本周，实际 {L.WeekStats(db, key, 0)[0]}")

    roll_the_instance(lua, L, db, 12, 300000, 1000, 44800)   # 到期已过 + 还在池里 = 换轮
    if round_at(lua, db, 12) != "44800":
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    f"前置：这一条实例该换轮了，实际 roundAt={round_at(lua, db, 12)}")
    if state1(L.State, db, key, 12) == "done":
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "换了实例，上一轮点的补录还压着这一格 = 玩家 10-27 看到的假勾")
    if int(L.WeekStats(db, key, 0)[0]) != 0:
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "过期补录不许再冒充本周完成数（格子不勾、汇总却 +1 是两处两个口径），"
                    f"实际 {L.WeekStats(db, key, 0)[0]}")
    ok("a_hand_mark_expires_when_the_instance_rolls")


def test_a_hand_mark_survives_within_the_same_instance():
    """与上一条成对：结束时刻没变（同一条实例里切奖励档）时补录必须还在。
    钉的是"别把轮次门放宽成每次快照都清"——那等于把 test_manual_beats_auto_and_survives_sync
    要的"本轮内补录优先于自动判定"当场改回去。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 13, 300000, 1000, 20000)   # 20000 < 到期 44200 ⇒ 还是同一条
    key = "A-永恒之井"
    L.SetManual(db, key, 13, "done", 1100, 150000)
    if round_at(lua, db, 13) != "1000":
        return fail("a_hand_mark_survives_within_the_same_instance",
                    f"前置：没到期不该换轮，实际 roundAt={round_at(lua, db, 13)}")
    if state1(L.State, db, key, 13) != "done":
        return fail("a_hand_mark_survives_within_the_same_instance",
                    "同一条实例里切了奖励档就把补录判死 = 本轮的勾凭空没了")
    ok("a_hand_mark_survives_within_the_same_instance")


def test_a_stale_hand_mark_without_any_client_answer_reports_unknown():
    """补录过期、而客户端这一轮一个字没答（不在池、没流水、没快照）：报 ? 不报"没做"。
    报"没做"和报"永远已做"是同一个错的两侧 —— 玩家点过的那一笔过期了不等于他没说，
    如实就是问不出（UI 显示 ?），照着 ? 再点一次就补上。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    L.AddQuest(db, 14, 1000)                                  # 玩家点名加的一条（不上板）
    L.SetManual(db, "A-永恒之井", 14, "done", 1100, 0)
    L.SetExpiry(db["quests"][14], 1000, 43200)
    if state1(L.State, db, "A-永恒之井", 14) != "done":
        return fail("a_stale_hand_mark_without_any_client_answer_reports_unknown",
                    "前置：本轮之内的补录该打勾")
    # 下一轮（roundAt 推到 44800）+ 此刻不在池里（currentSig 清空）= 客户端答不出
    lua.execute("local r = GQL.Config.DB().quests[14] "
                "r.roundAt, r.currentSig = 44800, nil")
    st = state1(L.State, db, "A-永恒之井", 14)
    if st != "unknown":
        return fail("a_stale_hand_mark_without_any_client_answer_reports_unknown",
                    f"过期补录 + 客户端没答该是 ?（unknown），实际 {st}")
    ok("a_stale_hand_mark_without_any_client_answer_reports_unknown")


def test_a_stale_hand_mark_does_not_outrank_this_rounds_evidence():
    """本轮真交过之后，上一轮那笔过期补录不许再插话：状态回到自动判定的 done，
    而且这一格不再算"玩家手点的"（颜色该从琥珀翻回绿，客户端认了这笔）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 15, 300000, 1000, 44800)
    key = "A-永恒之井"
    L.SetManual(db, key, 15, "done", 1100, 150000)
    if round_at(lua, db, 15) != "44800":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    f"前置：该换轮了，实际 roundAt={round_at(lua, db, 15)}")
    sig = one(lua.execute("return tostring(GQL.Config.DB().quests[15].currentSig)"))
    L.OnTurnIn(db, key, 15, 45000, 300000, sig)
    if state1(L.State, db, key, 15) != "done":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    "本轮实测交过该打勾（过期补录不许把它压成 ? 或 open）")
    src = one(lua.execute("local _, s = GQL.Ledger.State(GQL.Config.DB(), %r, 15) "
                         "return tostring(s)" % key))
    if src != "nil":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    f"这一格本轮有流水背书，不该还标成手点的，实际 src={src}")
    ok("a_stale_hand_mark_does_not_outrank_this_rounds_evidence")


def test_cell_click_can_cancel_a_mark_that_expired_with_the_round():
    """换轮以后那一格不再是"done"，玩家仍要能取消自己点过的那一笔：
    取消这一支问的是**有没有点过**（L.HasManualMark），拿当前状态当撤销凭据的话，
    再点一次等于重新补录 —— 那个琥珀勾就永远赖在格子里撤不掉了。"""
    lua, gql, _ = sync_env([(91, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    if not click_cell(lua, key, 91):
        return fail("cell_click_can_cancel_a_mark_that_expired_with_the_round",
                    "应有那一格的热区")
    if g.GQLDB["manual"][key][91] is None:
        return fail("cell_click_can_cancel_a_mark_that_expired_with_the_round",
                    "前置：点一下该记下补录")
    roll_the_instance(lua, gql.Ledger, g.GQLDB, 91, 30000000,
                        int(g.GQL_NOW), int(g.GQL_NOW) + 50000)
    if state1(gql.Ledger.State, g.GQLDB, key, 91) == "done":
        return fail("cell_click_can_cancel_a_mark_that_expired_with_the_round",
                    "前置：换轮后这一格不该还打着勾")
    if not click_cell(lua, key, 91):
        return fail("cell_click_can_cancel_a_mark_that_expired_with_the_round",
                    "过期那一格该还能点（取消自己点过的补录）")
    if g.GQLDB["manual"][key][91] is not None:
        return fail("cell_click_can_cancel_a_mark_that_expired_with_the_round",
                    "再点一下应清掉那笔补录，而不是把它重新算成本轮的")
    ok("cell_click_can_cancel_a_mark_that_expired_with_the_round")


if __name__ == "__main__":
    tests = [
        test_config_init_builds_schema,
        test_config_init_idempotent,
        test_cycle_anchor_rolls_over,
        test_cycle_anchor_reads_reset_as_countdown,
        test_cycle_anchor_survives_missing_api,
        test_cycle_anchor_clock_domain_bridge,
        test_format_gold,
        test_log_ring_buffer,
        test_sig_stable_and_gold,
        test_diff_added_and_gone,
        test_diff_renewed_new_round,
        test_diff_sig_changed_modes,
        test_reward_change_within_a_round_keeps_the_turned_in_mark,
        test_high_freq_learning,
        test_diff_renewed_with_new_reward,
        test_high_freq_after_reward_rotation,
        test_diff_renewed_by_expired_round,
        test_expired_round_with_a_new_reward_still_rolls,
        test_a_lost_check_is_never_bought_back_with_a_false_one,
        test_a_new_round_never_carries_the_old_gold_number,
        test_an_expired_round_never_carries_it_into_a_new_reward,
        test_a_reused_record_loses_the_previous_rounds_number_at_the_roll,
        test_a_reused_sig_reads_the_gold_number_back_after_the_roll,
        test_a_turned_in_row_drops_when_the_instance_rolls,
        test_a_weekly_turned_in_earlier_this_cd_still_occupies_its_row,
        test_the_poisoned_sticky_gold_is_cleared_once_at_sync,
        test_a_carried_zero_never_marks_the_quest_as_gold,
        test_dual_mode_flip_keeps_the_row_and_the_check,
        test_a_new_instance_rejudges_gold_and_clears_the_check,
        test_diff_no_renewal_without_real_countdown,
        test_turnin_state_and_week_stats,
        test_turnin_not_counted_across_rounds,
        test_manual_beats_auto_and_survives_sync,
        test_manual_not_double_counted_with_turnin,
        test_gold_unmeasured_counts_not_sum,
        test_mark_gold_by_observation,
        test_prune_bounds_size,
        test_state_uses_own_snapshot_across_characters,
        test_stale_snapshot_not_trusted_in_new_round,
        test_api_degrades_when_missing,
        test_api_empty_pool_is_data_not_failure,
        test_api_state_unknown_when_missing,
        test_api_normalizes_known_shape,
        test_getpool_uses_task_quest_map_source,
        test_getpool_refuses_unclassifiable_map_rows,
        test_getpool_fills_title_and_map_for_numeric_rows,
        test_getpool_enumerates_continent_maps,
        test_quest_reward_money_sources,
        test_quest_time_left,
        test_snapshot_partial_pool_does_not_mark_gone,
        test_partial_pool_renews_round_at_expiry,
        test_api_pool_shape_mismatch_is_not_empty,
        test_api_pool_extra_candidates,
        test_api_frame_visible_accepts_frame_userdata,
        test_probe_report_and_command_registered,
        test_probe_reports_map_button_host,
        test_probe_distinguishes_unbuilt_from_invisible_button,
        test_probe_map_candidates_use_map_id_and_skip_without_one,
        test_api_current_map_id_returns_number_or_nil,
        test_probe_discovers_unknown_api_names,
        test_probe_reports_reset_countdown_reading,
        test_probe_reports_map_button_entries,
        test_snapshot_records_added_and_views,
        test_snapshot_degrades_to_unknown,
        test_pool_unavailable_keeps_ledger,
        test_empty_pool_needs_second_read,
        test_turnin_money_window_capture_and_abort,
        test_api_frame_visible_candidates,
        test_money_window_polluted_by_frame_object,
        test_second_turnin_event_does_not_reset_window,
        test_back_to_back_turnins_both_recorded,
        test_money_window_samples_during_window,
        test_debounce_single_snapshot,
        test_sync_survives_unknown_event_name,
        test_sync_logs_when_no_pool_event_registered,
        test_dump_sections,
        test_dump_shows_stale_char,
        test_dump_quest_explains_why_a_row_stays,
        test_dump_quest_reports_real_expiry,
        test_weekstats_unknown_when_clock_domains_split,
        test_dump_marks_week_total_unknown_across_domains,
        test_dump_refuses_week_total_without_anchor,
        test_matrix_rows_group_by_map_and_filter_by_gold_floor,
        test_format_gold_icon,
        test_api_midnight_map_by_name,
        test_getpool_enumerates_all_maps_without_player_location,
        test_getpool_requests_reward_preload_when_money_missing,
        test_getpool_zero_money_row_falls_through_to_reward_chain,
        test_matrix_columns_stale_and_manual_only_quest_kept,
        test_matrix_renders_rows_and_week_totals,
        test_matrix_text_pool_never_borrows_another_font,
        test_matrix_empty_ledger_shows_placeholder,
        test_map_button_opens_matrix,
        test_map_button_degrades_without_minimap,
        test_login_creates_minimap_button,
        test_login_retries_button_when_minimap_appears_later,
        test_minimap_button_inherits_host_strata,
        test_minimap_button_raised_above_minimap,
        test_minimap_button_has_coin_icon,
        test_map_name_goes_through_api_and_matrix_falls_back_to_id,
        test_matrix_skips_old_expansion_maps,
        test_matrix_toggle_gates_background_sync,
        test_login_snapshot_runs_without_polling,
        test_login_snapshot_switch_off,
        test_matrix_columns_show_char_name_only,
        test_matrix_frame_width_capped_with_scroll,
        test_matrix_group_header_is_name_only,
        test_matrix_uses_real_scrollframe,
        test_money_read_flicker_does_not_drop_rows,
        test_confirmed_no_gold_does_not_keep_the_old_number,
        test_countdown_falls_back_to_ledger_expiry,
        test_wheel_works_over_cell_buttons,
        test_row_hover_highlight_covers_whole_row,
        test_scrollbars_are_visible_not_white_line,
        test_matrix_shows_scrollbars_when_content_overflows,
        test_matrix_header_frozen_while_body_scrolls,
        test_matrix_thumb_drag_follows_cursor,
        test_matrix_max_height_follows_screen,
        test_preload_retry_fills_money_without_reopening,
        test_event_snapshot_repaints_matrix,
        test_turnin_settlement_repaints_matrix,
        test_matrix_cell_click_toggles_manual_state,
        test_hand_marked_cell_uses_an_amber_check,
        test_click_on_this_char_verifies_with_the_interface,
        test_click_on_another_chars_cell_never_borrows_this_chars_answer,
        test_completion_line_names_the_evidence_behind_the_check,
        test_quest_id_is_gray_like_the_bottom_line,
        test_midnight_map_ids_match_by_name_and_remember,
        test_getpool_queries_only_midnight_maps,
        test_matrix_cell_click_records_quest_gold,
        test_matrix_task_info_in_fixed_subcolumns,
        test_matrix_class_color_applied,
        test_hover_shows_money_tooltip,
        test_hover_prints_the_quest_id,
        test_cell_hover_prints_that_chars_completion_time,
        test_row_hover_does_not_borrow_a_cells_completion,
        test_completion_stamp_stays_quiet_without_wall_clock,
        test_completion_line_hidden_once_the_instance_rolled,
        test_format_stamp_only_for_wall_clock,
        test_last_completion_takes_the_newer_record,
        test_cell_hover_shows_that_alts_own_gold_not_the_last_syncers,
        test_item_cell_says_that_alt_and_names_who_read_the_gold,
        test_item_cell_stays_neutral_not_an_alert,
        test_unsynced_cell_gets_no_attribution_line,
        test_row_hover_stays_free_of_alt_names,
        test_manual_done_records_that_alts_own_gain,
        test_sync_records_the_reward_answer_per_character,
        test_reward_read_stops_counting_when_the_round_rolls,
        test_gold_readers_lists_only_this_rounds_gold_answers,
        test_matrix_char_name_max4_no_ellipsis,
        test_matrix_scrolls_instead_of_scaling,
        test_next_refresh_at_is_earliest_expiry,
        test_snapshot_records_expires_at,
        test_matrix_open_skips_snapshot_until_expiry,
        test_refresh_wakes_at_earliest_expiry,
        test_closing_matrix_cancels_pending_refresh,
        test_matrix_title_drops_readonly_label,
        test_matrix_refresh_button_sits_by_settings_and_resyncs,
        test_windows_are_opaque,
        test_hover_ignores_foreign_mouse_focus,
        test_keyword_task_survives_map_filter,
        test_keyword_hunt_registers_weekly_rows,
        test_keyword_name_alone_does_not_admit_a_row,
        test_player_dictated_map_is_not_an_admission_credential,
        test_a_quest_the_alts_finished_this_round_is_a_credential,
        test_keyword_hunt_scans_tracked_maps_only,
        test_family_anchor_lists_all_week_and_the_board_child_does_not,
        test_matrix_repaint_shows_reused_text_pool,
        test_panel_reused_rows_are_shown,
        test_panel_unknown_money_row_shows_its_id,
        test_panel_lists_no_expired_or_uncredentialed_rows,
        test_panel_keeps_blocked_rows_without_evidence,
        test_panel_shows_quest_id_not_money,
        test_panel_lists_only_the_selected_keyword_matches,
        test_panel_ignore_button_marks_permanently,
        test_panel_unignore_button_clears_the_mark,
        test_panel_can_add_and_remove_keyword,
        test_panel_columns_and_footer_stay_inside_the_window,
        test_panel_footer_controls_are_not_under_a_viewport,
        test_panel_columns_scroll_with_wheel_when_content_overflows,
        test_panel_shows_keyword_hit_count,
        test_options_gold_threshold_control_writes_config,
        test_options_panel_has_no_login_sync_switch,
        test_track_hook_registers_once,
        test_track_scan_skips_hidden_map,
        test_track_walk_prefers_child_iterator,
        test_hover_same_row_does_not_rebuild_tooltip,
        test_hover_rebuilds_after_rerender,
        test_hover_off_rows_leaves_tooltip_alone,
        test_hover_needs_focus_confirmed_on_matrix,
        test_hide_money_tooltip_only_ours,
        test_hover_suppressed_when_window_covers_matrix,
        test_hover_debug_reports_why_a_window_is_not_a_candidate,
        test_escape_closes_matrix_and_options,
        test_hide_stops_pool_polling,
        test_panel_button_text_follows_ignored_mark,
        test_hover_stays_off_while_the_panel_is_open,
        test_keyword_quest_pulled_from_quest_log,
        test_keyword_removal_drops_log_added_row,
        test_probe_reports_button_texture_path_and_survives_bad_sections,
        test_pool_for_map_id_stamps_queried_map,
        test_log_source_skips_quest_already_mapped_by_name,
        test_matrix_rows_drop_unmapped_same_name_duplicate,
        test_snapshot_heals_missing_map,
        test_probe_reports_worldquest_and_money_per_keyword_hit,
        test_questmapid_resolves_accepted_quest_via_log_info,
        test_accepted_keyword_quest_groups_under_its_map,
        test_snapshot_falls_back_to_reverse_map_index,
        test_probe_proves_every_map_candidate_with_map_name,
        test_matrix_lists_one_row_per_quest_name,
        test_guessed_expiry_never_shows_countdown_or_color,
        test_questmapid_ignores_coordinates,
        test_heal_skips_record_whose_name_already_has_map,
        test_probe_reports_which_interface_gave_each_healed_map,
        test_probe_samples_map_evidence_even_when_nothing_missing,
        test_pool_map_disagreement_is_kept_for_probe,
        test_probe_shows_row_map_against_queried_map,
        test_matrix_right_click_blocks_nothing,
        test_panel_block_survives_resync_and_is_reversible,
        test_removed_keyword_log_quest_does_not_come_back,
        test_api_static_quest_info_cached_within_session,
        test_time_left_sources_require_quest_id,
        test_next_cycle_end_follows_reset_form,
        test_map_source_labels_which_answer_landed,
        test_quest_evidence_sections_present,
        test_quest_evidence_reports_who_answered_the_money,
        test_matrix_scroll_reuses_cell_click_handler,
        test_matrix_rows_money_and_order_unchanged_by_cache,
        test_matrix_gold_threshold_is_configurable,
        test_a_turned_in_row_this_round_admits_row_without_money,
        test_legacy_map_number_is_kept_and_reasked,
        test_family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it,
        test_anchor_in_the_pool_lists_only_one_row,
        test_registered_anchor_refreshes_when_pool_unavailable,
        test_family_state_does_not_leak_across_cycles,
        test_family_member_hidden_only_when_player_removed_it,
        test_keyword_rows_stay_separate_and_outlive_the_board,
        test_pool_countdown_does_not_kill_a_registered_weekly,
        test_family_ids_come_from_the_objective_list,
        test_last_weeks_child_does_not_stand_in_for_this_week,
        test_retired_map_sources_are_cleared_from_the_ledger,
        test_a_rotated_off_board_quest_keeps_its_map,
        test_hidden_log_quests_are_not_collected,
        test_log_zone_map_gives_an_accepted_quest_its_map,
        test_bounty_board_gives_special_assignments_their_map,
        test_registered_weekly_map_source_is_labelled_like_the_others,
        test_probe_prints_every_field_of_a_row,
        test_the_player_can_fix_maps_the_client_cannot_answer,
        test_map_memory_says_what_it_could_not_apply,
        test_map_memory_fills_every_row_sharing_a_name,
        test_an_ignore_outlives_every_cycle_and_round,
        test_shipped_map_memory_names_a_real_zone,
        test_pool_rotation_auto_rescans_tracked_maps,
        test_weekly_countdown_must_fit_the_anchor,
        test_dump_shows_the_weekly_family_section,
        test_cycle_expiry_never_downgrades_a_verified_value,
        test_probe_reports_weekly_source_and_timeleft_candidates,
        test_map_rows_prefer_taskquest_source,
        test_probe_reports_registered_preset_evidence,
        test_probe_lists_board_names_and_the_bounty_surface,
        test_probe_shows_row_week_evidence_and_raw_log_fields,
        test_probe_reports_why_a_board_quest_is_not_in_the_pool,
        test_probe_reverse_checks_the_completed_list,
        test_probe_area_poi_lists_event_pins,
        test_probe_area_poi_missing_does_not_swallow_later_sections,
        test_probe_asks_map_membership_for_resident_weekly,
        test_event_pin_groups_the_row_where_the_event_is,
        test_event_pin_ambiguous_or_gone_never_invents_a_group,
        test_weekly_group_holds_the_rows_the_client_cannot_place,
        test_an_unregistered_weekly_lands_in_the_weekly_group,
        test_a_pin_that_carries_a_quest_id_becomes_a_row,
        test_keyword_hunt_names_the_pin_only_hits,
        test_keyword_hunt_logs_the_pin_only_sentence,
        test_probe_lists_weekly_reward_activities,
        test_probe_checks_the_runestone_ids_from_savedinstances,
        test_turnin_of_unknown_quest_never_creates_a_row,
        test_turnin_of_pooled_quest_still_settles,
        test_money_window_seq_rejects_stale_timer,
        test_money_window_kept_when_ledger_missing,
        test_missing_countdown_also_schedules_retry,
        test_snapshot_survives_a_quest_api_error,
        test_map_memory_rejects_non_numeric_quest_id,
        test_prune_never_mixes_clock_domains,
        test_refresh_wake_is_capped,
        test_quest_records_only_come_from_the_factory,
        test_state_and_registered_state_share_precedence,
        test_gold_verdict_is_the_single_gold_entry,
        test_map_source_labels_are_constants,
        test_map_source_blacklist_is_built_from_the_same_table,
        test_migrate_runs_once_per_schema,
        test_migrate_failure_keeps_ledger_usable,
        test_migrate_drops_turnins_with_no_evidence_behind_them,
        test_minimap_drag_survives_scale_call,
        test_options_wheel_matches_matrix_direction,
        test_panel_hunt_button_survives_missing_sync,
        test_repaint_coalesces_within_a_burst,
        test_dump_uses_the_ledger_for_columns_and_time,
        test_dump_names_which_chars_read_the_gold,
        test_dump_sections_unchanged_after_dedupe,
        test_child_frames_accepts_both_api_shapes,
        test_only_api_reads_data_globals,
        test_a_click_add_does_not_force_the_row_onto_the_matrix,
        test_migrate_clears_manual_added_from_old_saves,
        test_the_upgrade_clears_every_old_ignore_and_add_mark,
        test_a_hand_mark_expires_when_the_instance_rolls,
        test_a_hand_mark_survives_within_the_same_instance,
        test_a_stale_hand_mark_without_any_client_answer_reports_unknown,
        test_a_stale_hand_mark_does_not_outrank_this_rounds_evidence,
        test_cell_click_can_cancel_a_mark_that_expired_with_the_round,
        test_no_old_addon_name_left_anywhere,
        test_gql_slash_command_registered,
        test_matrix_has_no_block_entry,
        test_lua_upvalue_budget,
        test_map_name_cached_per_session,
        test_map_parent_cached_per_session,
        test_map_name_nil_is_not_frozen,
        test_matrix_render_clips_to_visible_rows,
        test_matrix_scroll_keeps_first_cell_after_content_shrinks,
        test_matrix_hover_sweep_is_bounded_by_drawn_rows,
        test_matrix_clip_disabled_when_viewport_unknown,
    ]
    # -k 子串过滤：只跑用例名含该子串的几条（逗号分隔取并集）。逐任务 RED/GREEN 时
    # 不必每次全量跑（全量两分多钟），但**收尾必须回到全量** —— 过滤跑出来的"全绿"
    # 只证明这一族，别拿它当整个套件的结论。
    if "-k" in sys.argv:
        i = sys.argv.index("-k")
        if i + 1 >= len(sys.argv):
            print("用法：python tests/run_gql_tests.py -k 用例名子串[,另一个子串]")
            sys.exit(1)
        needle = sys.argv[i + 1]
        keys = [k for k in needle.split(",") if k]
        tests = [t for t in tests if any(k in t.__name__ for k in keys)]
        if not tests:
            print(f"没有用例名匹配 {needle}")
            sys.exit(1)
        print(f"（-k {needle}：只跑 {len(tests)} 条）")
    for t in tests:
        try:
            t()
        except Exception as exc:          # 用例抛错也要跑完剩下的：一条崩掉会让全部结论消失
            fail(t.__name__, f"用例抛错: {exc}")
    if "--syntax" in sys.argv:
        syntax_check()
    static = ("syntax:", "upvalues:")   # 静态检查逐文件报，不占单测名额
    print(f"\n{len(tests) - len([f for f in FAILED if not f.startswith(static)])}/{len(tests)} 单测通过")
    sys.exit(1 if FAILED else 0)
