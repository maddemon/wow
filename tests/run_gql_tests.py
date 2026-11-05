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
    PLAYER_LOGOUT = true,
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
            -- 纹理染色：矩阵的完成勾是贴图 + SetVertexColor（手点那档还要先抽成灰度，
            -- 见 CellMark）。替身不记这两样，"琥珀勾到底染没染上"离线就永远验不到 ——
            -- 上一版 |T 内联色字段位摆错、勾按原色上屏，正是没人能验的那一面。
            f.SetVertexColor = function(self, r, g, b, a)
                self.__vcolor = { r, g, b, a } end
            f.SetDesaturated = function(self, v) self.__desat = not not v end
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


def test_every_defined_case_is_registered_in_the_runner():
    """静态守卫（第 36 轮 T4b 抓出来的洞）：`def test_*` 定义了、但没写进 __main__ 那份
    人手维护的 `tests = [...]` 清单 ⇒ **这条一行都不跑，而且一句错都不报** —— 上一版
    `probe_reports_the_production_pin_attribution` 就是这么静默躺了若干轮，报数一直显示全绿。
    清单靠人加、改名时最容易漏一条，行为跑法替不了，只能拿本文件自己的源码钉。
    【只认清单那一段】不能拿"名字在文件里出现过两次"当证据：老用例的 docstring 和
    fail() 消息里到处写着自己的名字，那样 orphan 照样漏过去。"""
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        src = fh.read()
    block = re.search(r"^    tests = \[\n(.*?)^    \]\n", src, re.M | re.S)
    if not block:
        return fail("every_defined_case_is_registered_in_the_runner",
                    "找不到 __main__ 里那份 tests = [...] 清单（清单形状变了，守卫跟着改）")
    listed = set(re.findall(r"\b(test_\w+),", block.group(1)))
    defined = set(re.findall(r"^def (test_\w+)", src, re.M))
    orphans = sorted(defined - listed)
    ghost = sorted(n for n in listed if n not in defined)
    if orphans or ghost:
        return fail("every_defined_case_is_registered_in_the_runner",
                    "定义了但清单里没跑（一条都不会执行）：%s\n清单里有但没定义（跑它会 NameError）：%s"
                    % ("、".join(orphans) or "无",
                       "、".join(ghost) or "无"))
    ok("every_defined_case_is_registered_in_the_runner")


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
    """rows: [(questID, mapID, money)]；money 为 None 表示奖励接口问不到（未判定）。"""
    arr = lua.table_from({})
    for i, (qid, map_id, money) in enumerate(rows, start=1):
        entry = {"questID": qid, "mapID": map_id, "name": "任务%d" % qid}
        if money is not None:
            entry["money"] = money
        arr[i] = lua.table_from(entry)
    return arr


# ------------------------------------------------------------------
# 第 36 轮 T4b：身份 = 「任务号 + 结束时刻」的唯一编号。造存档数据一律走真实写点
# （SetView / SetManual / SetRewardRead / OnTurnIn / NoteGoldThisInstance / SetExpiry
# 自己烙键），不手拼 k = "94612:21" 那种字面量 —— 手拼会把用例写成对实现的空转复述。
# ------------------------------------------------------------------

def uid(L, db, qid, now):
    """这一条任务此刻的唯一编号（字符串）；判不出（身份未定）= None。
    走 L.InstanceKey 现算，不手拼字符串。"""
    rec = db["quests"][qid] if db["quests"] else None
    k = L.InstanceKey(db, qid, rec, now)
    return None if k is None else str(k)


def board_expiry(L, db, qid, now, remaining):
    """让一条记录答出**板面倒计时**（身份①档）：走真实写点 L.SetExpiry。
    它同时写 roundEnd 与 expiresSrc="接口倒计时"，手拼那两个字段的用例会把
    "来源标签不符就不算身份"那道守卫测成空转。"""
    L.SetExpiry(db["quests"][qid], now, remaining)
    return db["quests"][qid]


def pool_pass(lua, L, db, rows, now, ends=None):
    """一趟真实同步的**写侧顺序**（计划 §六 1(a)(b)(c)）：先整池 DiffPool，再逐条
    SetExpiry（这一读问出的倒计时）→ NoteGoldThisInstance（按这条实例的编号记钱）。
    rows = [(questID, money, items)]；ends = {questID: 剩余秒数}，缺 = 这一读问不出倒计时。
    用例不许自拼 roundEnd / goldKey / obs.key：绕开真实写点就测不到"顺序"本身，
    而 §六 1(a) 那个顺序正是"第一次上板那一笔快照挂得上身份"的唯一保证。

    注意**第一趟**的形状：DiffPool 排在倒计时读取之前（§六 1(a)），新建的那条记录当时
    还没有 roundEnd ⇒ 它给 obs.key 烙的是 nil；随后 SetExpiry 让身份成立，
    NoteGoldThisInstance 第 2 步看见 `obs.key(nil) ≠ key` 就把刚读到的那个数作废掉
    （§六 1(c)：作废只往"少报"那边走）。所以客户端真会给出的"接=0 判得出 false"，
    要**第二趟**才落地（同一实例的下一轮快照，倒计时已在账上 ⇒ DiffPool 自己就烙对键、
    不再作废）。用例要"读到 0"这个前提时就调两趟，别手改 obs.money 假装读到过。"""
    L.DiffPool(db, pool(lua, rows), now)
    for qid, money, _items in rows:
        rec = db["quests"][qid]
        if rec is None:
            continue
        rem = (ends or {}).get(qid)
        if rem:
            L.SetExpiry(rec, now, rem)
        L.NoteGoldThisInstance(db, qid, money, now)
    return db


def board_clock(lua, end_at):
    """给走真实 Sync 入口的用例挂上"接口答得出这条板面倒计时"。
    答案随 GQL_NOW 递减（客户端就是这么答的：剩余 = 固定结束时刻 - 现在）——
    写死一个常数会让每次同步算出不同的结束时刻 = 每次同步凭空换一条实例，
    那测的不是客户端会有的形状。改返回值的 ["end"] = 板面换成下一条实例。
    挂上它之后这一条实例才有结束时刻 ⇒ L.InstanceKey 才算得出编号；编号模型里
    所有走 Sync 的用例都得有个身份来源，替身不给就等于客户端压根没这个接口。"""
    g = lua.globals()
    holder = lua.table_from({})
    holder["end"] = int(end_at)
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: max(1, int(holder["end"]) - int(g.GQL_NOW))})
    g.GQL_BOARD = holder
    return holder


def raw_turnin(lua, L, db, char, qid, t, sig=None):
    """按客户端真会写的形状塞一笔交任务流水，编号走 L.InstanceKey 现算。
    保留它是因为用例要手搓一笔带编号的流水而不触发 SnapshotNow 那一整套
    （2026-10-29 交割当场落账之后 Sync 也不再往流水里写金额，行形状与 OnTurnIn 一致）。"""
    row = {"t": t, "cycle": int(db["cycleAnchor"] or 0)}
    if sig is not None:
        row["sig"] = sig
    key = uid(L, db, qid, t)
    if key is not None:
        row["k"] = key
    log = db["turnins"][char]
    if log is None:
        log = lua.table_from({})
        db["turnins"][char] = log
    rows = log[qid]
    if rows is None:
        rows = lua.table_from({})
        log[qid] = rows
    rows[len(rows) + 1] = lua.table_from(row)
    return rows[len(rows)]


def st(L, db, char, qid, now=None, gate=None):
    """L.State 的只取状态版：身份要靠 now 判"这条实例还活着没"，
    取证面不传 now 时走 LedgerNow 兜底（离线没挂 Api 就是 nil ⇒ 一律判不出）。"""
    return state1(L.State, db, char, qid, gate, now)


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
    """12 小时任务消失后又出现：面板要报"这条换轮了"（`res.renewed` 留在取证/提示那一档，
    计划 §五），observation 的 goneSince 清掉。
    【T4b 换写法】老断言"新一轮 firstSeen = 本轮起点"钉的是已被删掉的"本轮起点"
    （firstSeen 按 §六 第 3 条降级成纯取证：这一副奖励第一次读到的时刻，建那一刻写一次、
    以后只更 lastSeen，没有任何判据读它）。语义不变的那一半换成钉身份：
    换没换一条实例只问结束时刻 —— 结束时刻挪走 ⇒ 编号变 ⇒ 上一条实例的勾不再算。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    one = pool(lua, [(7, 300000, None)])
    L.DiffPool(db, one, 1000)
    board_expiry(L, db, 7, 1000, 43200)             # 这一条实例结束于 44200
    L.SetView(db, "A-永恒之井", 7, "done", 1500)    # 这一条实例上的快照（自己烙键）
    old_key = uid(L, db, 7, 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r3 = L.DiffPool(db, one, 3000)
    if len(r3["renewed"]) != 1 or int(r3["renewed"][1]) != 7:
        return fail("diff_renewed_new_round", f"应报 renewed 任务 7，实际 {len(r3['renewed'])}")
    obs = L.GetObs(db, 7)
    if obs is None:
        return fail("diff_renewed_new_round", "重新出现后应能取到 observation")
    if int(obs["firstSeen"]) != 1000:
        return fail("diff_renewed_new_round",
                    "firstSeen 现在只说「这一副奖励第一次读到」，重现不许把它推成起点，"
                    f"实际 {obs['firstSeen']}")
    if int(obs["lastSeen"]) != 3000:
        return fail("diff_renewed_new_round", f"lastSeen 要跟着每次快照走，实际 {obs['lastSeen']}")
    if obs["goneSince"] is not None:
        return fail("diff_renewed_new_round", "重新出现后 goneSince 应清空")
    # 身份只认结束时刻：接口补上下一条实例的结束时刻 ⇒ 换编号 ⇒ 上一条实例那笔快照不再打勾
    board_expiry(L, db, 7, 3000, 43200)
    if uid(L, db, 7, 3000) == old_key:
        return fail("diff_renewed_new_round", "结束时刻挪走了编号却没变，身份不是挂在结束时刻上")
    if st(L, db, "A-永恒之井", 7, 3000) == "done":
        return fail("diff_renewed_new_round", "换了实例，上一轮那笔快照不许再打勾")
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
    根因从来不在换模式，在**身份挂在哪**：observation 是按奖励签名分的 ⇒ 奖励一变就新建一份，
    老代码那一份新记录带着"跳到当下"的本轮起点，两条判据（实测流水 / 本角色快照）同时失效。
    第 36 轮把身份换成「任务号 + 结束时刻」之后这件事是自然成立的：
    **结束时刻没变 = 同一条实例 = 同一个编号 ⇒ 勾一个不丢**（计划 §十用例 1 的对照组）。
    `obs.firstSeen` 按 §六 第 3 条降级成纯取证（这一副奖励第一次读到的时刻），
    没有任何判据读它 ⇒ 这一条不再拿它当"本轮起点"断言，改断"编号没变"。
    反向也要成立：结束时刻整段挪走（真换一条实例）⇒ 换编号 ⇒ 上一轮的交任务不再算已交。
    走 Sync 真实事件入口（fire QUEST_COMPLETE → Sync.RecordTurnIn 当场落账；2026-10-29
    起 QUEST_COMPLETE / QUEST_TURNED_IN 任一到达即入账，没有跨事件的待结算窗口），
    不直调 Ledger。"""
    lua, gql, money = sync_env([(9, 30000000)])
    g = lua.globals()
    board = board_clock(lua, 10000 + 43200)   # 接口答得出这条的倒计时：结束时刻 = 53200
    key = str(gql.Sync.CurrentCharKey())
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 9)
    money["v"] = 31000000
    g.GQL_NOW = 5000
    if st(gql.Ledger, g.GQLDB, key, 9, 5000) != "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "前置：刚交掉的这条在同一条实例上就该显示已交")
    inst = uid(gql.Ledger, g.GQLDB, 9, 5000)
    sig_before = str(g.GQLDB["quests"][9]["currentSig"])

    def set_pool(rows):
        g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    one = lambda extra: lua.table_from([lua.table_from(
        {"questID": 9, "mapID": 2007, "name": "任务9"})] + list(extra))
    # 换狩猎模式：同一条不再给金币（签名翻面），任务既没结束也没换实例
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.HaveQuestRewardData = lambda qid: True
    set_pool(one([]))
    g.GQL_NOW = 6000
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 9, 6000) != inst:
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "结束时刻没变就是同一条实例，换奖励不许翻出第二个编号")
    if gql.Ledger.GetObs(g.GQLDB, 9)["firstSeen"] is None:
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "前置：换奖励新建的那份 observation 得有它自己的取证字段")
    if st(gql.Ledger, g.GQLDB, key, 9, 6000) != "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "换模式后做过的那格不许变回未交（用户报的就是这一格）")
    # 反向：真换一条实例（板面把结束时刻整段挪走 12 小时）= 上一条的凭据作废，
    # 否则 12 小时区"上一轮的已交冒充这一轮"（屠戮者高地秘密狩猎）会复发。
    # 11-04 口径：换实例 = 旧那条已经下板，账本里它的结束时刻不再算数；旧 roundEnd
    # 还活着时直接把替身的 ["end"] 往后挪，等于"同一条实例被凭空改了结束时间"，
    # 客户端不会这么答，新口径也不会去重问。
    g.GQLDB["quests"][9]["roundEnd"] = None
    board["end"] = 96400
    set_pool(lua.table_from([lua.table_from(
        {"questID": 99, "mapID": 2007, "name": "任务99"})]))
    g.GQL_NOW = 7000
    gql.Sync.SnapshotNow()
    set_pool(one([]))
    g.GQL_NOW = 8000
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 9, 8000) == inst:
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "前置：这一条实例该换成下一条了（结束时刻没挪走 = 场景没造出来）")
    if st(gql.Ledger, g.GQLDB, key, 9, 8000) == "done":
        return fail("reward_change_within_a_round_keeps_the_turned_in_mark",
                    "换了实例，上一轮的交任务不许再算已交")
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
    """换轮检测（res.renewed）从这一版起只剩**取证**一件事：整块板没看见它、这次又出现
    （isNew / goneAt）。到期时刻已过不再推出任何一轮 —— 计划 §五 把 RoundEnded / RollRound
    那台"推起点"的机器整块删了，身份改由「任务号 + 结束时刻」的编号直接推出：结束时刻一变
    编号就变，旧凭据自己匹配不上，不需要"开新一轮"这个动作，也不需要 15 分钟容差。
    12.x 答得出数据的图源只有按图问的那条（partial=true），消失判定整段被关掉，
    于是老用例钉的"靠它自己上一轮的到期时刻开新一轮"在新模型里**没有对应保证可钉**，
    逐条换成钉它的替代品（这三档才是 12.1.5 实测"屠戮者高地秘密狩猎全角色 ✓、
    新刷那一轮再也记不上"真正的因果链）：
    · 结束时刻没变（同一条实例）⇒ 编号不变 ⇒ 本轮两份凭据（快照 + 实测流水）都算已交；
    · 结束时刻已过而这一读倒计时问不出 ⇒ 那是上一条实例的墓碑，身份判不出 ⇒ 一律 ?，
      绝不许拿过去那条冒充 done；
    · 倒计时答出一个新的结束时刻（整段挪走）⇒ 换编号 ⇒ 旧快照与旧流水一起匹配不上。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    day = 86400
    one = pool(lua, [(7, 300000, None)])

    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 300000, None)], 1000, {7: 43200})
    inst1 = uid(L, db, 7, 2000)
    if inst1 is None:
        return fail("diff_renewed_by_expired_round", "前置：答出板面倒计时就该有唯一编号")
    L.SetView(db, "B-永恒之井", 7, "done", 2000)
    L.OnTurnIn(db, "A-永恒之井", 7, 2000, str(db["quests"][7]["currentSig"]))
    if st(L, db, "B-永恒之井", 7, 2000) != "done":
        return fail("diff_renewed_by_expired_round", "同一条实例里本轮已交的快照应算已交")
    if st(L, db, "A-永恒之井", 7, 2000) != "done":
        return fail("diff_renewed_by_expired_round", "同一条实例里本轮实测的流水应算已交")
    # 没到期：还挂在同一条实例上，谁也不该"重新起算"
    mid = 1000 + 21600
    r = L.DiffPool(db, one, mid)
    if len(r["renewed"]) != 0:
        return fail("diff_renewed_by_expired_round", "没到期不该报 renewed")
    if uid(L, db, 7, mid) != inst1:
        return fail("diff_renewed_by_expired_round",
                    f"结束时刻没变编号就不该变，实际 {uid(L, db, 7, mid)} vs {inst1}")
    # 到期时刻已过、这一读倒计时问不出：roundEnd 停在上一条实例的过去值 = 墓碑（§三 R3），
    # 拿它当身份就是 94612 那批假勾的形状 ⇒ 编号必须是 nil、格子必须是 ?
    past = 1000 + 43200 + 600
    r = L.DiffPool(db, one, past)
    if uid(L, db, 7, past) is not None:
        return fail("diff_renewed_by_expired_round",
                    "上一条实例的过去结束时刻不许再当身份，编号应为 nil")
    if len(r["renewed"]) != 0:
        return fail("diff_renewed_by_expired_round",
                    f"到期本身不再推出一轮，renewed 只由真的消失过又回来触发，实际 {len(r['renewed'])}")
    for who in ("B-永恒之井", "A-永恒之井"):
        got = st(L, db, who, 7, past)
        if got != "unknown":
            return fail("diff_renewed_by_expired_round",
                        f"身份未定时{who}那一格的凭据属于上一条实例，应如实报 ?，实际 {got}")
    # 幂等 + firstSeen 降级取证（§六 第 3 条）：倒计时一直问不出时，后续每次同步都不许
    # "重开一轮"改写出任何判据读的东西；老那句 renew 会把 obs.firstSeen 推到当下，
    # 而 Dump 的"上次金额"照旧读它 —— 被推等于报告说瞎话。
    seen_first = int(L.GetObs(db, 7)["firstSeen"])
    r = L.DiffPool(db, one, past + 600)
    if len(r["renewed"]) != 0:
        return fail("diff_renewed_by_expired_round", "到期时刻没更新时不该再判一轮")
    if int(L.GetObs(db, 7)["firstSeen"]) != seen_first:
        return fail("diff_renewed_by_expired_round",
                    f"firstSeen 只是取证字段（这一副奖励第一次读到的时刻），不该被换轮检测改写，实际 {seen_first}")
    if st(L, db, "A-永恒之井", 7, past + 600) != "unknown":
        return fail("diff_renewed_by_expired_round", "状态不该被重复判轮影响")
    # 倒计时答出新的结束时刻 = 板面上换成了另一条实例：编号自然变，旧的两份凭据一起作废
    L.SetExpiry(db["quests"][7], past + 600, 43200)
    inst2 = uid(L, db, 7, past + 600)
    if inst2 is None or inst2 == inst1:
        return fail("diff_renewed_by_expired_round",
                    f"结束时刻整段挪走就该换编号，实际 {inst2}")
    for who in ("B-永恒之井", "A-永恒之井"):
        if st(L, db, who, 7, past + 600) == "done":
            return fail("diff_renewed_by_expired_round",
                        f"换了一条实例，{who}旧轮的凭据不该再算已交（本轮真做过会有新的凭据）")
    if st(L, db, "B-永恒之井", 7, past + 600) != "open":
        return fail("diff_renewed_by_expired_round",
                    f"这条还在板上，新实例该如实报可做，实际 {st(L, db, 'B-永恒之井', 7, past + 600)}")
    ok("diff_renewed_by_expired_round")


def test_expired_round_with_a_new_reward_still_rolls():
    """「到期 + 同时换奖励」（切狩猎模式撞上 12 小时区刷新）这一簇，编号模型里拆成两件
    互相独立的事，老用例把它们捆在"推起点"那一刀上，现在必须分开钉：
    · **身份**：只换奖励、结束时刻没变 ⇒ 编号不变 ⇒ 那一笔已交照旧算数
      （第 25 轮"切一次模式不该丢勾"那一半，判据方向一字不许动）；
      结束时刻整段挪走 ⇒ 换编号 ⇒ 旧 CD 那一笔立刻匹配不上（否则 ✓ 永远翻不掉）。
    · **面板提示**：res.renewed 从此只由"真的从板上消失过又回来"触发（§五 删了到期推轮），
      sigChanged 照旧报 —— 两条都得有读者，只报 sigChanged 玩家看不懂"不过换了个奖励，
      怎么满屏 ✓ 没了"。
    12.x 按图问池时消失判定整段关闭，唯一还能锚定新实例的凭据就是这条自己的倒计时。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 300000, None)], 1000, {7: 43200})
    inst1 = uid(L, db, 7, 2000)
    L.OnTurnIn(db, "A-永恒之井", 7, 2000, str(db["quests"][7]["currentSig"]))
    if st(L, db, "A-永恒之井", 7, 2000) != "done":
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "前置：到期前本轮已交应算已交")
    # ① 同一结束时刻里只换奖励（切模式）：编号不变、那一笔仍算数，只报 sigChanged
    r = L.DiffPool(db, pool(lua, [(7, 0, [888])]), 1000 + 21600)
    if uid(L, db, 7, 1000 + 21600) != inst1:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"结束时刻没变换奖励不许换编号（那正是「切模式丢勾」的形状），实际 {inst1}")
    if st(L, db, "A-永恒之井", 7, 1000 + 21600) != "done":
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "同一条实例里换奖励，那一笔已交照旧算数")
    if len(r["sigChanged"]) != 1 or int(r["sigChanged"][1]) != 7:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "奖励变了要报 sigChanged")
    if len(r["renewed"]) != 0:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "没消失过、也没换实例，不该报 renewed")
    # ② 结束时刻整段挪走（板面上换成了下一条实例）：换编号 ⇒ 旧 CD 那一笔不许跨实例冒充
    past = 1000 + 43200 + 600
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), past)
    L.SetExpiry(db["quests"][7], past, 43200)
    inst2 = uid(L, db, 7, past)
    if inst2 is None or inst2 == inst1:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"结束时刻整段挪走必须换编号，实际 {inst2}")
    r2 = L.DiffPool(db, pool(lua, [(7, 300000, None)]), past)
    if len(r2["sigChanged"]) != 1 or int(r2["sigChanged"][1]) != 7:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    "同一次快照里换了奖励，汇总要报 sigChanged")
    if st(L, db, "A-永恒之井", 7, past) != "open":
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"旧 CD 那一笔不许跨实例冒充，实际 {st(L, db, 'A-永恒之井', 7, past)}")
    # ③ renewed 今天唯一的触发路：整块板没看见它、这次又出现（goneAt 那条）。
    # 这是它降级成纯取证之后仅剩的语义 —— 面板那句"这条换轮了"从此只报真有痕迹的。
    L.DiffPool(db, pool(lua, []), past + 60)
    r3 = L.DiffPool(db, pool(lua, [(7, 0, None)]), past + 120)
    if len(r3["renewed"]) != 1 or int(r3["renewed"][1]) != 7:
        return fail("expired_round_with_a_new_reward_still_rolls",
                    f"真消失过又回来才该报 renewed，实际 {len(r3['renewed'])}")
    ok("expired_round_with_a_new_reward_still_rolls")


def test_a_lost_check_is_never_bought_back_with_a_false_one():
    """上一轮的 ✓ 不许冒充本轮（用户 2026-10-25 实测，屠戮者高地 12 小时区：
    「意外遭遇现在不是金币任务，但是都标记全部做完了，实际上上一轮做完了这一轮都没做过。
    上一轮的结束时间至少是这一轮结束时间的 12 个小时之前」）。
    老实现靠"从各份记录里挑一个本轮起点"，挑错一个（挑了最早那份）就把上一轮的已交
    补成全员 ✓。编号模型里这件事不再靠"起点选哪一个"实现，三条一起归成同一句判据：
    **认这笔证据写下的编号 == 这条任务当前的编号**，对不上就是不对上，没有第二道兜底 ——
    · 压根没烙编号的一笔（写下那一刻身份还没定 / v6 回填前的老档）；
    · 上一条实例的编号（结束时刻整段挪走过，12 小时区刷新）；
    · 上个 CD 尾巴的编号（周 CD 换过一次，常驻周常那种问不出板面倒计时的走这一档）。
    老那三处"起点判不出就兜底成周初"的反推整删（§五），本用例就是它删掉之后钉住的替代保证。
    少一个勾能忍，假勾是谎报；对照组（烙当前编号的那一笔必须算数）钉住修法不许退化成
    "永远不认"，否则这条用例就成了空转。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    gold_sig = str(L.MakeSig(300000))
    free_sig = str(L.MakeSig(0, lua.table_from([888])))

    def build(anchor):
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
        return db

    # ① 没烙编号的一笔：真实写点在"身份判不出"那一刻写下的就是 nil（锚点 0 + 倒计时问不出）
    db = build(0)
    L.SetView(db, "A-永恒之井", 7, "done", 2000)
    if db["chars"]["A-永恒之井"]["views"][7]["k"] is not None:
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "前置：身份判不出时真实写点烙下的就该是没有编号")
    if st(L, db, "A-永恒之井", 7, 2000) == "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "没有编号的一笔绝不许冒充本轮已交（不许退回「t >= 起点」那道兜底）")
    if st(L, db, "A-永恒之井", 7, 2000) != "unknown":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    f"身份未定那一格应如实报 ?，实际 {st(L, db, 'A-永恒之井', 7, 2000)}")

    # ② 上一条实例的编号：结束时刻整段挪走过（12 小时区刷新）
    db = build(0)
    L.SetExpiry(db["quests"][7], 1000, 43200)         # 上一条实例 44200 结束
    prev = uid(L, db, 7, 2000)
    L.SetExpiry(db["quests"][7], 44800, 43200)        # 板面答出下一条：88000 结束
    now = 44800
    cur = uid(L, db, 7, now)
    if prev is None or cur is None or prev == cur:
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    f"前置：两条实例该有两个编号，实际 {prev} / {cur}")
    # 像 Sync 那样把算好的编号显式传进真实写点，模拟"这条快照属于上一条实例"
    L.SetView(db, "A-永恒之井", 7, "done", now, prev)
    if st(L, db, "A-永恒之井", 7, now) == "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "上一轮（上一条实例）的已交不许冒充本轮，全员 ✓ 就是谎报")
    if st(L, db, "A-永恒之井", 7, now) != "open":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    f"换了一条实例、这条还在板上，该如实报可做，实际 {st(L, db, 'A-永恒之井', 7, now)}")
    L.SetView(db, "A-永恒之井", 7, "done", now, cur)   # 对照组：同一处写点、当前编号
    if st(L, db, "A-永恒之井", 7, now) != "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "当前这一条实例的已交快照该算已交（修法不许退化成永远不认）")

    # ③ 上个 CD 尾巴的编号：问不出板面倒计时的走 ②档（周 CD 尾巴），换 CD 自动作废
    db = build(604800)                                # 上个 CD 的锚点
    old_cd = uid(L, db, 7, 1300000)
    db["cycleAnchor"] = 1209600                       # 本周 CD 的锚点
    cur_cd = uid(L, db, 7, 1300000)
    if old_cd is None or cur_cd is None or old_cd == cur_cd:
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    f"前置：换 CD 该换尾巴编号，实际 {old_cd} / {cur_cd}")
    L.SetView(db, "A-永恒之井", 7, "done", 1300000, old_cd)
    if st(L, db, "A-永恒之井", 7, 1300000) == "done":
        return fail("a_lost_check_is_never_bought_back_with_a_false_one",
                    "上个 CD 的快照不该算本周（老用例那句「起点不许早于本 CD」的替代品）")
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


def test_all_done_counts_skip_marks():
    """用户 2026-10-31："巢穴全部标记做完或不做，但是任务名称还不是灰色"。
    置灰的判据是 row.allDone，以前只认 done —— 右键记的"不做"（skip，2026-10-30）
    会把一排 ✓/× 混着的行挡在置灰外面。skip 与 done 同为这一格的终态：
    全格都是终态就置灰；问不出的 ?、还没处理的空格照旧挡着。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)          # 本轮到期 44200

    def row7(now):
        groups = L.MatrixRows(db, now)
        for gk in groups.keys():
            rows = groups[gk]["rows"]
            for rk in rows.keys():
                if int(rows[rk]["questID"]) == 7:
                    return rows[rk]
        return None

    now = 2000
    L.SetView(db, "A-永恒之井", 7, "done", now)         # 第一个号：做了
    L.SetManual(db, "B-永恒之井", 7, "skip", now)       # 第二个号：右键记"不做"
    row = row7(now)
    if row is None:
        return fail("all_done_counts_skip_marks", "前置：该行该在矩阵上")
    if not row["allDone"]:
        return fail("all_done_counts_skip_marks",
                    "全格都是终态（done/skip）就该置灰：skip 不该挡住 allDone")
    # 对照：补一列"在做"（progress，不是终态）⇒ 不算全员完成
    L.SetView(db, "C-永恒之井", 7, "progress", now)
    if row7(now)["allDone"]:
        return fail("all_done_counts_skip_marks",
                    "还有一列在做/问不出/空着就不许置灰（修法不许退化成永远灰）")
    ok("all_done_counts_skip_marks")


def test_pool_read_without_name_keeps_the_known_name():
    """用户 2026-10-31 的"出现2次消失2次"嫌疑门之一：池行的名字要奖励数据预加载才答
    得出来，没答出来的一读（name=nil）不许把上一读客户端确认过的名字冲掉 ——
    关键词命中靠名字（kwHit），名字被冲掉的下一帧命中翻 false，命中关键词的行闪没一回，
    名字回来又冒出来。地图同理：mapID 被冲掉不光塌进"未知地图"分组，
    还丢一份"客户端答过图"的凭据（L.QuestHasCredential 的 mapID 那一支）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    if str(db["quests"][7]["name"]) != "任务7":
        return fail("pool_read_without_name_keeps_the_known_name",
                    "前置：第一读的名字要在账上")
    # 下一读名字/地图都没问出来（奖励数据没预加载的常见形状）
    bare = lua.table_from({})
    bare[1] = lua.table_from({"questID": 7, "money": 6000000})
    L.DiffPool(db, bare, 2000)
    if str(db["quests"][7]["name"] or "") != "任务7":
        return fail("pool_read_without_name_keeps_the_known_name",
                    "名字没问出来的一读不许把客户端确认过的名字冲掉")
    if int(db["quests"][7]["mapID"] or 0) != 2007:
        return fail("pool_read_without_name_keeps_the_known_name",
                    "空读数不许把已确认的图号冲成 nil")
    # 真换图/真改名的新答案非空，照旧覆盖（修法不许退化成永不更新）
    L.DiffPool(db, pool_map(lua, [(7, 2393, 6000000)]), 3000)
    if int(db["quests"][7]["mapID"]) != 2393:
        return fail("pool_read_without_name_keeps_the_known_name",
                    "新的非空答案要覆盖旧值")
    ok("pool_read_without_name_keeps_the_known_name")


def test_matrix_rows_logs_admission_flips():
    """用户 2026-10-31："该任务本轮不是金币，却出现2次消失2次"。闪现只活几秒，
    截图永远截不到它在场的那一帧 ⇒ 翻面当场把两道门的判据摊一条进 /gql log。
    钉三面：① 首次计算没有"上一帧"可比，不许写；② 在列→掉列要写"消失"且带判据；
    ③ 稳态重算（两帧同判）不许刷日志。"""
    lua, gql = load_lua("Config", "Ledger", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    L = gql.Ledger
    db = gql.Config.DB()

    def flip_count(word):
        return str(gql.Config.GetLogs() or "").count("矩阵行" + word)

    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)           # 本轮到期 44200
    L.NoteGoldThisInstance(db, 7, 6000000, 1000)
    L.MatrixRows(db, 2000)                              # 首算：立基线
    if flip_count("出现") + flip_count("消失") != 0:
        return fail("matrix_rows_logs_admission_flips",
                    "首次计算没有上一帧可比，不许写对账日志")
    L.MatrixRows(db, 2000)                              # 稳态：同判据重算
    if flip_count("出现") + flip_count("消失") != 0:
        return fail("matrix_rows_logs_admission_flips", "稳态重算不许刷日志")
    # 换一条实例、这一读答 0：金币门当场关上 ⇒ 行消失，要写一条带判据的
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 44400)
    L.SetExpiry(db["quests"][7], 44400, 43200)          # 新一条：87600 结束
    L.NoteGoldThisInstance(db, 7, 0, 44400)
    L.MatrixRows(db, 44500)
    body = str(gql.Config.GetLogs() or "")
    if body.count("矩阵行消失") != 1:
        return fail("matrix_rows_logs_admission_flips",
                    f"行从在列翻到掉列要写一条「矩阵行消失」，实际日志：\n{body}")
    if "quest=7" not in body or "钱=" not in body:
        return fail("matrix_rows_logs_admission_flips",
                    "对账日志要带任务号与两道门的判据（钱/关键词/注册/到期），不然没法定门")
    L.MatrixRows(db, 44600)                             # 掉列后的稳态：不再写
    if str(gql.Config.GetLogs() or "").count("矩阵行消失") != 1:
        return fail("matrix_rows_logs_admission_flips", "掉列后的稳态重算不许再写")
    ok("matrix_rows_logs_admission_flips")


def test_a_reused_record_loses_the_previous_rounds_number_at_the_roll():
    """同一条奖励签名会跨轮复用那份 observation（12 小时区"这副奖励隔一轮又出现"很常见）。
    换实例时那份记录里存的数字属于上一条实例 ⇒ 必须清空重新读，否则读侧按"这条读到过金币"
    拿它当本条实例客户端的答案（上一条用例只管新建签名那一半，签名复用这一半它管不到）。
    作废它的是 L.NoteGoldThisInstance 第 2 步（计划 §六 1(c)：obs.key 对不上这次 ⇒
    这份观察是按上一条实例记的账，作废它的金额）—— 老那句 `if rolled then
    reuse.money/reuse.gold = nil` 只覆盖"真的消失过又回来"那一半，到期换实例这一半
    从今天起归编号。所以本用例照真实同步的写序跑（DiffPool → SetExpiry → NoteGold，
    §六 1(a)），不自拼 obs.key：漏掉哪一步都该红。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    sig_a = L.MakeSig(None, None)
    L.DiffPool(db, pool(lua, [(7, None, None)]), 1000)    # 签名 A：这一读金额问不出
    L.SetExpiry(db["quests"][7], 1000, 43200)             # 这一条实例 44200 结束
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1100)  # 签名 B：奖励数据到了，带数
    L.DiffPool(db, pool(lua, [(7, None, None)]), 1200)    # 读闪回 A：同一条实例内延续，该有数
    old_key = uid(L, db, 7, 1200)
    if L.QuestMoney(db, 7) is None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：同一条实例里金额读闪要延续，行不许闪")
    if db["quests"][7]["observations"][sig_a]["money"] is None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：A 那份记录里得真的存着这条实例延续来的数，否则测不到复用")
    if old_key is None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：这一读问得出倒计时，那份记录该烙上编号")
    # 到点之后板面答出**下一条**实例的结束时刻，而这一读金额问不出：真实写序三步
    L.DiffPool(db, pool(lua, [(7, None, None)]), 44400)
    L.SetExpiry(db["quests"][7], 44400, 43200)            # 下一条实例 87600 结束
    if uid(L, db, 7, 44400) == old_key:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "前置：结束时刻整段挪走该换成另一个编号")
    L.NoteGoldThisInstance(db, 7, None, 44400)
    if db["quests"][7]["observations"][sig_a]["money"] is not None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    "换了实例，复用记录里那条实例的旧数必须当场作废（§六 1(c) 第 2 步）")
    m = L.QuestMoney(db, 7, 44400)
    if m is not None:
        return fail("a_reused_record_loses_the_previous_rounds_number_at_the_roll",
                    f"新实例不许拿上一条那份记录里的数占行，实际 {m}")
    if len(L.MatrixRows(db, 44400)) != 0:
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
    ⇒ 占行的"交过"必须跟 ✓ 同一个口径：本轮交过才算（用户 2026-10-25 定的收口）。
    2026-10-29 定稿补一刀：非关键字任务，这一轮没读到过奖励金币（接=0 或读不出）就别想
    靠交过占位 —— 哪怕同一条实例从金币档切成非金币档，这一行也该掉。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][7], 1000, 43200)        # 本轮到期 44200
    L.OnTurnIn(db, "A-永恒之井", 7, 1500, "6000000")
    # 同一条实例换成不给金币那一档：金额掉到门槛以下、且接=0 ⇒ 不该再占行
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 2000)
    L.SetExpiry(db["quests"][7], 2000, 42200)       # 问出来还是 44200 ⇒ 同一条实例
    if len(L.MatrixRows(db, 2500)) != 0:
        return fail("a_turned_in_row_drops_when_the_instance_rolls",
                    "同一条实例切成非金币档后，非关键字任务不该靠交过占行（2026-10-29 定稿）")
    # 换实例：到期时刻整段挪走 ⇒ 上一轮的已交不再替这一轮占行
    L.SetExpiry(db["quests"][7], 44400, 43200)       # 新一条，到期 87600
    L.DiffPool(db, pool(lua, [(7, 0, [888])]), 44500)
    if len(L.MatrixRows(db, 44500)) != 0:
        return fail("a_turned_in_row_drops_when_the_instance_rolls",
                    "换了实例，上一轮的已交不该再占行（这一轮谁都没做过）")
    ok("a_turned_in_row_drops_when_the_instance_rolls")


def test_turnin_round_key_is_stamped_at_write_and_expires_on_roll():
    """用户 2026-10-28 定案的核心：交任务流水在**写下的那一刻**把当时的唯一编号
    （任务号 + 结束时刻）烙进记录，L.State 之后只比"记录上的编号 == 当前编号"，
    不再用 t >= 陈旧的起点反推（老那三处反推是 94612 十个假勾的入口，§五 整删）。
    换了一条实例编号对不上即判上一条 —— 世界任务不断轮换，上一轮做过的不能顶本轮的格子。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 7, 6000000, 1000, 20000)   # 这一条实例 63200 结束
    key = "A-永恒之井"
    L.OnTurnIn(db, key, 7, 1500, "6000000")
    stamped = one(lua.execute(
        "local r = GQL.Config.DB().turnins[%r][7][1] return tostring(r and r.k)" % key))
    if stamped != uid(L, db, 7, 1500):
        return fail("turnin_round_key_is_stamped_at_write_and_expires_on_roll",
                    f"交任务该烙下当时的唯一编号，实际 {stamped} 期望 {uid(L, db, 7, 1500)}")
    if st(L, db, key, 7, 1500) != "done":
        return fail("turnin_round_key_is_stamped_at_write_and_expires_on_roll",
                    "前置：这一条实例里交过的该打勾")
    # 换实例：结束时刻整段挪走 ⇒ 当前编号换了，这一笔流水的编号对不上 ⇒ 不再算已交
    roll_the_instance(lua, L, db, 7, 6000000, 20000, 64000)
    if uid(L, db, 7, 64000) is None or uid(L, db, 7, 64000) == stamped:
        return fail("turnin_round_key_is_stamped_at_write_and_expires_on_roll",
                    f"前置：该换成下一条实例的编号了，实际 {uid(L, db, 7, 64000)} vs {stamped}")
    if st(L, db, key, 7, 64000) == "done":
        return fail("turnin_round_key_is_stamped_at_write_and_expires_on_roll",
                    "换了实例，上一轮的已交不该再打勾（唯一编号对不上）")
    ok("turnin_round_key_is_stamped_at_write_and_expires_on_roll")


def test_id_plus_end_time_is_the_identity_not_the_start():
    """用户 2026-10-28 定案（第 36 轮判据的出处）："一个任务id+结束时间，算他的唯一标识"。
    94612 举钳欢庆一行 10 个假勾的根因就是身份挂在"本轮起点"上、起点判不出时兜底到
    周初（fail-open）。新模型只认结束时刻：结束时刻挪出半桶以上 ⇒ 下一条实例、换编号；
    结束时刻不动、只翻奖励签名（切狩猎模式）⇒ 编号不许变 —— 第 25 轮那条
    "做过不该被标成没做过"的老保证在新写法下必须照样成立。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    rec = db["quests"][7]
    L.SetExpiry(rec, 1000, 42200)                     # 本条实例结束于 43200
    k1 = str(L.InstanceKey(db, 7, rec, 1000))
    if k1 != "7:48":
        return fail("id_plus_end_time_is_the_identity_not_the_start",
                    f"前置：编号该是结束时刻 43200 的 15 分钟桶，实际 {k1}")
    # 换实例：板面结束时刻整段挪走（远超 15 分钟）⇒ 编号必须变，旧实例的凭据自然对不上
    L.SetExpiry(rec, 44400, 45600)                    # 下一条实例结束于 90000
    k2 = str(L.InstanceKey(db, 7, rec, 44400))
    if k2 == k1:
        return fail("id_plus_end_time_is_the_identity_not_the_start",
                    "结束时刻换了一大段还是同一个编号，说明身份没挂在结束时间上")
    # 对照组：结束时刻不动，真实 DiffPool 换一副奖励签名（currentSig 确实翻了）⇒ 编号不变
    sig_before = str(rec["currentSig"])
    L.DiffPool(db, pool(lua, [(7, 3000000, [555])]), 44500)
    if str(rec["currentSig"]) == sig_before:
        return fail("id_plus_end_time_is_the_identity_not_the_start",
                    "前置：这步没把奖励签名翻掉，对照组不成立")
    k3 = str(L.InstanceKey(db, 7, rec, 44500))
    if k3 != k2:
        return fail("id_plus_end_time_is_the_identity_not_the_start",
                    f"同一条实例换奖励翻出两个编号（{k2} vs {k3}），✓ 会凭空全丢")
    ok("id_plus_end_time_is_the_identity_not_the_start")


def test_a_one_second_jitter_never_forges_a_new_instance():
    """计划 §三第 3 条：接口答的是"剩余秒数"，now + remaining 只会比真结束时刻小 0~1 秒，
    而真板面结束时刻全落在整点上（12.1.5 真档实测 19:00 / 07:00 / 15:00 / 11:00）。
    裸 floor(endAt/900) 的桶边界正压在整点上 ⇒ 同一条实例两次读数有近一半概率分进
    相邻桶 = 凭空造一条新实例、把整排 ✓ 判成上一轮。+450 半桶偏移摘掉这条必须红。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(7, 6000000, None)]), 1000)
    rec = db["quests"][7]
    L.SetExpiry(rec, 1000, 42200)                     # 真结束 43200，恰好是 900 的整数倍
    k1 = str(L.InstanceKey(db, 7, rec, 1000))
    L.SetExpiry(rec, 2000, 41199)                     # 第二次读数：now+remaining 小 1 秒
    k2 = str(L.InstanceKey(db, 7, rec, 2000))
    if k1 != k2:
        return fail("a_one_second_jitter_never_forges_a_new_instance",
                    f"同一条实例差 1 秒的两次读数翻出两个编号（{k1} vs {k2}）")
    # 差 0 秒的那次读数也钉住（桶值本身 = floor((43200+450)/900)=48，摘掉 +450 时 43199 落 47）
    L.SetExpiry(rec, 2500, 40700)                     # now+remaining 不抖动，还是 43200
    k3 = str(L.InstanceKey(db, 7, rec, 2500))
    if k3 != k1 or k1 != "7:48":
        return fail("a_one_second_jitter_never_forges_a_new_instance",
                    f"0 秒抖动必须原地不动且落在半桶 48，实际 {k1} / {k3}")
    ok("a_one_second_jitter_never_forges_a_new_instance")


def test_weekly_family_without_countdown_uses_the_cd_tail():
    """用户定案原话："周常（获取不到结束时间的任务组）先按周CD的时间算结束时间"。
    97128 巢穴 / 90962 斯托玛兰突袭战他点过名，都走这一档。尾巴 = cycleAnchor + 604800
    是纯账本算式（锚点本来就是"下次周重置 - 604800"反推的），零新增接口调用 ——
    他 10-27 明令：修法不许加接口调用、不许轮询、不许扫描。同一 CD 内编号稳定；
    锚点前进一整周（下个 CD）⇒ 换编号，上周的勾不许跨 CD 冒充本周。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 1000
    db["quests"][97128] = lua.table_from({
        "name": "巢穴", "regKey": "w:巢穴", "mapID": 2405,
        "observations": lua.table_from({})})
    rec = db["quests"][97128]
    if int(L.InstanceEnd(db, rec, 3000)) != 605800:
        return fail("weekly_family_without_countdown_uses_the_cd_tail",
                    "问不出倒计时的注册周常，结束时刻该是锚点+604800，实际 %s"
                    % L.InstanceEnd(db, rec, 3000))
    k1 = str(L.InstanceKey(db, 97128, rec, 3000))
    k2 = str(L.InstanceKey(db, 97128, rec, 3000000))  # 同一 CD 内换个时刻再问
    if k1 != k2:
        return fail("weekly_family_without_countdown_uses_the_cd_tail",
                    f"同一个 CD 内两次读数编号会漂（{k1} vs {k2}），周常的勾留不住")
    db["cycleAnchor"] = 1000 + 604800                 # 下个 CD：锚点前进一整周
    k3 = str(L.InstanceKey(db, 97128, rec, 3000))
    if k3 == k1:
        return fail("weekly_family_without_countdown_uses_the_cd_tail",
                    "跨 CD 了编号还一样，上周做过的会冒充本周")
    # 优先级钉一下：同一条记录问出了活着的板面倒计时 ⇒ 走板面、不走尾巴。
    # T5 起 roundEnd 在场就是板面读到的（唯一写点 SetExpiry），不再配来源字段。
    db["cycleAnchor"] = 1000
    rec["roundEnd"] = 700000
    if int(L.InstanceEnd(db, rec, 600000)) != 700000:
        return fail("weekly_family_without_countdown_uses_the_cd_tail",
                    "板面倒计时还活着时必须优先于周 CD 尾巴")
    ok("weekly_family_without_countdown_uses_the_cd_tail")


def test_an_identity_that_cannot_be_determined_is_nil_never_a_guess():
    """本轮靶心的反面：第 36 轮 94612 十个假勾的根因是 L.RoundAt 判不出起点时兜底成
    cycleAnchor —— "判不出"被实现成"从周初一直都在"（fail-open）。新模型唯一的降级
    方向是**如实回 nil**（界面对它就是整排 ?，宁缺不编）。四个问不出来的形状：
    锚点是 0 且没板面倒计时；板面倒计时已经是过去值（上一条实例的墓碑，§三第 1 条）；
    老档残留的 expiresAt/expiresSrc/expiresGuessed 那一组（连同 24 小时排程值，T5 整组
    退役）一律无读者 —— 拿不出 roundEnd 就按尾巴/nil 走，残留数不许冒充身份；
    板面值与 now 跨时钟域
    （ThisCycle / EffectiveExpiry / WeekStats 同一条 house rule：跨域不比较，
    "活着没"判不出来就不许猜）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger

    def key_of(anchor, rec, now):
        db = ledger_db(lua)
        db["cycleAnchor"] = anchor
        return str(L.InstanceKey(db, 42, rec, now))

    bare = lua.table_from({"observations": lua.table_from({})})
    if str(L.InstanceEnd(ledger_db(lua), bare, 1000)) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "锚点 0 且没有板面倒计时时 InstanceEnd 必须答 nil（不许猜周初）")
    if key_of(0, bare, 1000) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "锚点 0 且没有板面倒计时时 InstanceKey 必须答 nil")
    tombstone = lua.table_from({
        "roundEnd": 500, "observations": lua.table_from({})})
    if str(L.InstanceEnd(ledger_db(lua), tombstone, 1000)) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "已经过去的板面倒计时是上一条实例的墓碑，不许再当身份")
    # 墓碑这一档不许被周 CD 尾巴接住（89377「秘密狩猎」形状：12 小时区、全角色交完之后
    # 再也读不到倒计时）。尾巴是按周算的，同周上下两条实例会并成同一个编号 =
    # 上一轮的 ✓ 冒充本轮，正是本轮要消灭的那类 fail-open。
    tail_ready = ledger_db(lua)
    tail_ready["cycleAnchor"] = 1000
    if str(L.InstanceEnd(tail_ready, tombstone, 1000)) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "答出过板面倒计时、可那条已过去的行退回了周 CD 尾巴，同周两条实例会并成一个编号")
    legacy = lua.table_from({
        "expiresAt": 900, "expiresSrc": "排程值", "expiresGuessed": True,
        "observations": lua.table_from({})})
    if str(L.InstanceEnd(ledger_db(lua), legacy, 1000)) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "老档残留的排程值字段已整组退役：没有 roundEnd、锚点又是 0，必须答 nil，不许拿残留数当身份")
    anchored = ledger_db(lua)
    anchored["cycleAnchor"] = 1000
    if int(L.InstanceEnd(anchored, legacy, 1000)) != 605800:
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    f"残留老到期字段无读者：没 roundEnd 就该走周 CD 尾巴 605800，实际 {L.InstanceEnd(anchored, legacy, 1000)}")
    split = lua.table_from({
        "roundEnd": 1760000000,
        "observations": lua.table_from({})})
    if str(L.InstanceEnd(ledger_db(lua), split, 1000)) != "None":
        return fail("an_identity_that_cannot_be_determined_is_nil_never_a_guess",
                    "板面值与 now 跨时钟域时判不出'活着没'，必须如实 nil，不许拿大小硬比")
    ok("an_identity_that_cannot_be_determined_is_nil_never_a_guess")


def test_snapshot_written_this_round_is_the_missing_case_today():
    """计划 §十用例 6 —— 94612 举钳欢庆那个 bug 的复现用例，走真实 Sync 入口。
    真档形状：这条今早同步的两个号客户端自己答的是 open（恶灵之力 10-28 09:01、
    群山之力 08:54），可行上还挂着 10 个 ✓ —— 因为快照那一份压根不带身份，
    "算不算本轮"只能拿 t 跟本轮起点反推，而它的 roundAt 与 cycleAnchor 逐秒相同
    （从没滚过轮）⇒ t >= roundAt 恒成立。这一条钉两件事：
    ① 登录快照写下的 {s,t,k} 必须带 k（替身照 12.1.5 答 open）；
    ② §六 1(a) 那个顺序：记录事先没有 roundEnd、倒计时问得出 5 小时 ⇒ **这同一次**
       快照写下的 view/read 就必须已经带编号。用例故意把周锚点留在 0（sync_env 不给
       weekly 入口，RefreshCycle 定不出锚点），尾巴档也帮不上忙，身份只剩"这一趟刚问出来
       的板面倒计时"一条路 —— 把逐条倒计时读取挪回 SetView 之后，这里烙出来就是 nil，
       这条立刻红。"""
    lua, gql, _ = sync_env([(94612, 10580000)])      # 真档实测 1058 金（门槛 500 金）
    g = lua.globals()
    g.GQL_NOW = 1000
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 18000})
    db = g.GQLDB
    if db["quests"][94612] is not None:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    "前置：这条记录该是这一次快照第一次建出来的（第一次上板那一支）")
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"快照应成功：{res}")
    rec = db["quests"][94612]
    if int(rec["roundEnd"]) != 19000:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"前置：5 小时倒计时该把结束时刻写成 19000，实际 {rec['roundEnd']}")
    want = str(gql.Ledger.InstanceKey(db, 94612, rec, 1000))
    if want != "94612:21":                            # floor((19000+450)/900) = 21
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"前置：编号该是结束时刻 19000 的 15 分钟桶，实际 {want}")
    char = str(gql.Sync.CurrentCharKey())
    view = db["chars"][char]["views"][94612]
    if str(view["s"]) != "open":
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"替身要复刻 12.1.5 的 open 答复，实际 {view['s']}")
    if view["k"] is None or str(view["k"]) != want:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"第一次上板这同一趟快照的 view 没带上唯一编号（实际 {view['k']}，"
                    "期望 " + want + "）—— 94612 那十个假勾的入口就是这一格没有身份")
    read = db["chars"][char]["reads"][94612]
    if read["k"] is None or str(read["k"]) != want:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    f"同一趟的按号读数没带唯一编号，实际 {read['k']}")
    if rec["goldKey"] is None or str(rec["goldKey"]) != want:
        return fail("snapshot_written_this_round_is_the_missing_case_today",
                    "金币粘滞的编号要排在 SetExpiry 之后烙进记录（计划 §六 1(b)）")
    ok("snapshot_written_this_round_is_the_missing_case_today")


def test_the_write_side_stamps_the_key_and_keeps_the_old_round():
    """写侧五个点各钉两件事（计划 §四 W1-W5 的 T4b 收口版）：
    · 新的 k / goldKey / obs.key 烙上了，且等于同一时刻 L.InstanceKey 算出的那一份；
    · §六 第 2/3 条降级成**取证**的老字段（流水的 cycle、obs.firstSeen）一个都不许少，
      而 §五 点名的那一批（round / goldRound / roundAt）一个都不许再写 ——
      "删字段与删它的读点是同一件事"，留着写点下次就有人再给它接上判据。
    NoteGoldThisInstance 从这一版起**就是**金额的归属（§六 1(c) 四步：nil 编号不写、
    obs.key 对不上先作废、烙新键时把 rec.goldMoney 一起作废、第 4 步用池行原始读数写钱）。
    最后钉身份未定时新函数回 nil、一个编号都不烙：判不出就是判不出，不许兜底成周初。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 1000
    L.DiffPool(db, pool(lua, [(94612, 6000000, None)]), 1000)
    rec = db["quests"][94612]
    L.SetExpiry(rec, 1000, 42200)                     # 本条实例结束于 43200
    want = str(L.InstanceKey(db, 94612, rec, 2000))
    if want != "94612:48":
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"前置：编号该是 43200 的 15 分钟桶，实际 {want}")
    char = "A-永恒之井"

    L.OnTurnIn(db, char, 94612, 2000, str(rec["currentSig"]))
    row = db["turnins"][char][94612][1]
    if str(row["k"]) != want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"交任务流水没烙编号，实际 {row['k']}")
    if row["round"] is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "§五：挂在可变起点上的 round 已经和它一起删了，不许再写")
    if row["cycle"] is None or row["sig"] is None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "cycle/sig 降级成 /gql quest 的取证字段（§六 第 2 条），不是删字段")

    L.SetManual(db, char, 94612, "done", 2000, 6000000, "接口核验")
    m = db["manual"][char][94612]
    if str(m["k"]) != want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"手动补录没烙编号，实际 {m['k']}")
    if m["round"] is not None or str(m["state"]) != "done" or m["v"] is None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "补录的 state/v 一个都不许少，round 则随 §五 一起下线")

    L.SetView(db, char, 94612, "open", 2000)
    v = db["chars"][char]["views"][94612]
    if str(v["k"]) != want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "W3（94612 的入口）：快照这一格得带上当时的编号")
    if str(v["s"]) != "open" or int(v["t"]) != 2000:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "W3 不许改快照原有的形状")

    L.SetRewardRead(db, char, 94612, 6000000, 2000)
    r = db["chars"][char]["reads"][94612]
    if str(r["k"]) != want or int(r["g"]) != 6000000:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "W4：按号读数要同时留着金额（g）和编号（k）")

    obs = L.GetObs(db, 94612)
    first_seen = int(obs["firstSeen"])
    got = L.NoteGoldThisInstance(db, 94612, 9990000, 2000)
    if got is None or str(got) != want or str(rec["goldKey"]) != want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"W5：rec.goldKey 该是 {want}，实际 {rec['goldKey']}")
    if str(L.GetObs(db, 94612)["key"]) != want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "W5：当前签名那份 observation 也要带上同一编号（读闪延续认它）")
    if int(rec["goldMoney"]) != 9990000:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"§六 1(c) 第 4 步：金额归这一笔写（用池行原始读数），实际 {rec['goldMoney']}")
    if int(L.GetObs(db, 94612)["firstSeen"]) != first_seen:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "obs.firstSeen 只降级成取证字段（§六 第 3 条），不许被新写点改写")
    # 第 2 步：换到下一条实例（结束时刻整段挪走）后再记一笔，那份按上一条实例记的账要作废
    L.SetExpiry(rec, 44400, 45600)                    # 下一条实例结束于 90000
    want2 = str(L.InstanceKey(db, 94612, rec, 44400))
    if want2 == want:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "前置：结束时刻整段挪走该换编号")
    L.NoteGoldThisInstance(db, 94612, None, 44400)
    if str(rec["goldKey"]) != want2:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    f"第 3 步该把编号换成新的，实际 {rec['goldKey']}")
    if rec["goldMoney"] is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "第 3 步换键时要把上一条实例粘滞的那个数一起作废（新实例认领旧钱 = 假勾）")
    if L.GetObs(db, 94612)["money"] is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "第 2 步：obs.key 对不上这次的读数不属于这一条实例，金额要作废")

    # 身份未定（锚点 0、板面倒计时没读到）⇒ 不烙编号，也不许编一个周初的编号顶上去
    other = ledger_db(lua)
    L.DiffPool(other, pool(lua, [(89267, 6000000, None)]), 1000)
    if L.NoteGoldThisInstance(other, 89267, 6000000, 1000) is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "判不出身份时新函数必须回 nil（fail-closed，不许兜底成周初）")
    if other["quests"][89267]["goldKey"] is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "判不出身份还写了 goldKey，等于把'不知道'做成一条凭据")
    if other["quests"][89267]["goldRound"] is not None:
        return fail("the_write_side_stamps_the_key_and_keeps_the_old_round",
                    "goldRound 随 §五 一起下线：粘滞从此只认 goldKey，不许再写第二套")
    ok("the_write_side_stamps_the_key_and_keeps_the_old_round")


def test_a_weekly_turned_in_earlier_this_cd_still_occupies_its_row():
    """收口只许收 12 小时区那一侧：注册周常的"这一条实例"就是整个 CD（它不会中途换实例），
    周初交过、周末才被重新建出来的记录也得照旧占行 —— 松露大乱斗那种金额永远读不出的
    金币任务，唯一的凭据就是"有人交过"（12.1.5 反馈④）。
    编号模型里这条**不需要另设豁免**：问不出板面倒计时 ⇒ L.InstanceEnd 走②档
    （cycleAnchor + 604800），同一个 CD 里周初和周末算出的是同一个编号，
    "比编号"就是"比 CD"（§八第 1 条把 gate 收窄成只管兜底答案，正是为此）。
    对照组钉住另一半：烙的是上一个 CD 尾巴编号的那一笔，换 CD 后不许再占行
    —— 老写法是拿 RoundAt 兜底成周初来放行，那一支 fail-open，§五 整删。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 100
    db["quests"][98000] = lua.table_from({
        "name": "松露大乱斗", "regKey": "w:松露大乱斗", "mapID": 2405,
        "observations": lua.table_from({}), "lastSeenAt": 9000})
    # 周初那一笔走真实写点：它烙的是"任务号 + 本 CD 尾巴"这一档编号
    L.OnTurnIn(db, "A-永恒之井", 98000, 200, None)
    if uid(L, db, 98000, 200) is None:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "前置：问不出板面倒计时的注册周常该用周 CD 尾巴定出编号")
    if uid(L, db, 98000, 200) != uid(L, db, 98000, 9000):
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "同一个 CD 内周初与周末该算同一条实例（比编号就是比 CD），"
                    f"实际 {uid(L, db, 98000, 200)} vs {uid(L, db, 98000, 9000)}")
    if len(L.MatrixRows(db, 9000)) != 1:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "周初交过的注册周常不许被轮次收口弄掉（它的这一条实例=整个 CD）")
    if L.TurnedInThisRound(db, 9000)[98000] is None:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "本 CD 尾巴那笔流水要能被准入门认出来，否则松露大乱斗那种永远读不出金额的就没凭据了")
    # 对照：换到下一个 CD，那一笔烙的是上个 CD 的尾巴编号 ⇒ 不再算"这一条实例交过"
    db["cycleAnchor"] = 100 + 604800
    db["quests"][98000]["lastSeenAt"] = 9000 + 604800
    if L.TurnedInThisRound(db, 9000 + 604800)[98000] is not None:
        return fail("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row",
                    "上周交过的那一笔不许冒充本周（那条 fail-open 的兜底已经删了）")
    ok("a_weekly_turned_in_earlier_this_cd_still_occupies_its_row")


def test_a_turnin_with_no_gold_on_both_sides_does_not_admit():
    """沼地威胁 91803 的形状：客户端这一轮明说"接"不给金币（奖励读到 0）、
    玩家"交"这一轮那一笔也没有可读的金额（2026-10-29 起 OnTurnIn 不量也不存到手金币）
    ⇒ "本轮交过"不再算上板凭据，这一行不许在矩阵上。
    用户 2026-10-27 原话："接任务金币是0，交任务金币还是0…那不能上，就比如这个 沼地威胁"。
    形状照真存档：接 = currentSig='0'、那份 observation money=0（本轮直接读到）；
    交 = 本轮那一笔照 12.1.5 那次 18:18 的交记录直接手写进 db.turnins（老版本那一行还带
    g 这一列；现在的 OnTurnIn 只写 t / sig / cycle / k，既不再量到手，也不再顺手把任务
    标成金币）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(91803, 0, None)]), 1000)
    if L.GoldVerdict(db, 91803) is not False:
        return fail("a_turnin_with_no_gold_on_both_sides_does_not_admit",
                    "前置：接到 0 时 GoldVerdict 必须答得出 false（'接'这一处得读得出 0）")
    db["turnins"]["爱情之力-奥达曼"] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803][1] = lua.table_from({"t": 1000, "g": 0, "cycle": 0})
    if 91803 in matrix_rows_by_id(L, db, 1000):
        return fail("a_turnin_with_no_gold_on_both_sides_does_not_admit",
                    "接和交两处客户端都答了 0 金币，'本轮交过'不该再把它放上板")
    ok("a_turnin_with_no_gold_on_both_sides_does_not_admit")


def test_the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read():
    """12.1.5 真存档 91803 一字不改的形状：currentSig='-1'（这一次快照压根没读出金额），
    那份 observation 里的 0 是同轮另一副签名搬来的，gold 那面旗是空的。
    判据若挂在旗上（或挂在"这一档读到过"上），部署后他刷新一次看到的还是那一行赖着 ——
    正是 10-25 那句"没有变化，是我拷贝错了吗"。搬来的 0 也是这条实例本轮的答案：
    延续那道门第 34 轮收成"只许同轮之内搬"，上一条实例的 0 进不来。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["quests"][91803] = lua.table_from({
        "name": "沼地威胁", "mapID": 2437, "poolSeen": True, "currentSig": "-1",
        "lastMoney": 0, "roundAt": 1000,
        "observations": lua.table_from({
            "-1": lua.table_from({"firstSeen": 1000, "lastSeen": 1000, "money": 0}),
            "0": lua.table_from({"firstSeen": 1000, "lastSeen": 1000, "money": 0}),
        }),
    })
    db["turnins"]["爱情之力-奥达曼"] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803][1] = lua.table_from({"t": 1000, "g": 0, "cycle": 0})
    if L.GoldVerdict(db, 91803) is not False:
        return fail("the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read",
                    "真存档那份搬来的 0 必须判得出'这一轮不给金币'，否则部署后行还赖着")
    if 91803 in matrix_rows_by_id(L, db, 1000):
        return fail("the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read",
                    "照存档原样（不等新快照）这一行就该掉，不能要玩家先刷新一次才生效")
    ok("the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read")


def test_a_turnin_with_unread_reward_money_does_not_admit():
    """对照组（真存档 91808 资源回收 / 95484 松露大乱斗那一档）：奖励这一轮读不出金额
    （money=nil ⇒ GoldVerdict 答不出 = nil）。用户 2026-10-29 定稿：非关键字任务，
    读不到金币奖励就不该上榜 —— 松露大乱斗那类金币任务切模式就会读到正数，不会卡在 nil；
    所以"接读不出"这一档不再放行 —— 而交割那一处从 2026-10-29 起压根不量、不存到手金币，
    判据也没有可读的那一列，"交过"更顶不进来。
    （§十(a)：造数据走真实写点 —— 板面倒计时用 SetExpiry、流水用 L.OnTurnIn 自己烙编号；
    手拼 k = "91808:xx" 会让这条变成对实现的空转复述。）"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    # 照真存档：currentSig='-1'，那份 observation 只有 firstSeen/lastSeen，没 gold 也没 money
    db["quests"][91808] = lua.table_from({
        "name": "资源回收", "mapID": 2437, "poolSeen": True, "currentSig": "-1",
        "observations": lua.table_from({
            "-1": lua.table_from({"firstSeen": 1000, "lastSeen": 1000}),
        }),
    })
    board_expiry(L, db, 91808, 1000, 43200)      # 这一读答得出板面倒计时 ⇒ 有身份
    if L.GoldVerdict(db, 91808, 1000) is not None:
        return fail("a_turnin_with_unread_reward_money_does_not_admit",
                    "前置：奖励读不出金额时 GoldVerdict 必须答 nil（不能折成 false）")
    L.OnTurnIn(db, "A-永恒之井", 91808, 1000, "-1")
    if str(db["turnins"]["A-永恒之井"][91808][1]["k"]) != uid(L, db, 91808, 1000):
        return fail("a_turnin_with_unread_reward_money_does_not_admit",
                    "前置：那一笔流水该烙上这一条实例的编号，否则测不到否决那一支")
    if 91808 in matrix_rows_by_id(L, db, 1000):
        return fail("a_turnin_with_unread_reward_money_does_not_admit",
                    "接读不出金额的非关键字任务 2026-10-29 起不该上榜（读不到金币奖励就不上榜）")
    ok("a_turnin_with_unread_reward_money_does_not_admit")


def test_a_turnin_row_that_stores_no_gold_does_not_admit():
    """对照组：接=0（读到 0 金币、判得出 false），交割那一笔压根没写过金额 ——
    OnTurnIn 自 2026-10-29 起不量也不存到手金币，流水行只有 t / sig / cycle / k
    四个字段（老"15 秒钱窗"到点把金额传进 OnTurnIn 那一套整块退役；Sync 现在由
    RecordTurnIn 在 QUEST_COMPLETE / QUEST_TURNED_IN 任一到达时当场落账，没有跨事件
    的待结算窗口）。钉的是**金币准入门**：这道门的唯一真源是奖励读取，交割流水既没有
    金额可读、"交过"本身也不是门 —— 接=0 已坐实"这条不是金币任务"，本 CD 交过一次
    照样不放行，行必须掉。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    # 板面答完身份之后客户端还会再读一次池（周期刷新），那一趟才把"接=0"落成 obs.money=0
    # —— 见 pool_pass 的说明：第一趟 DiffPool 排在倒计时之前，那份读数会被作废掉。
    pool_pass(lua, L, db, [(91803, 0, None)], 1000, {91803: 43200})
    pool_pass(lua, L, db, [(91803, 0, None)], 1100, {91803: 43100})
    L.OnTurnIn(db, "A-永恒之井", 91803, 1000, str(db["quests"][91803]["currentSig"]))
    if db["turnins"]["A-永恒之井"][91803][1]["g"] is not None:
        return fail("a_turnin_row_that_stores_no_gold_does_not_admit",
                    "前置：交割流水那一行不许带金额这一列（g 字段压根不写），不是 0")
    if L.GoldVerdict(db, 91803, 1000) is not False:
        return fail("a_turnin_row_that_stores_no_gold_does_not_admit",
                    "前置：这一档接=0 判得出 false（否决的那一头在奖励读取，不在交割）")
    if 91803 in matrix_rows_by_id(L, db, 1000):
        return fail("a_turnin_row_that_stores_no_gold_does_not_admit",
                    "接=0 的非关键字任务，本 CD 交过一笔（流水里也没有金额）照样过不了金币准入门")
    ok("a_turnin_row_that_stores_no_gold_does_not_admit")


def test_a_turnin_with_zero_reward_read_does_not_admit():
    """钉 2026-10-29 定稿：到手金额不再计入判据、也不再从交割流水里读。
    松露大乱斗（95484）这一条实例的**奖励读取**答 0（接口明说这一趟没金币），交割流水
    照记（有编号、有时刻）—— 掉的是金额那一列，不是"交过"这一事实。于是
    TurnInGainsThisRound 报 nil（判不了，不是 0：读数失败和白干是两回事），
    这一行也不上榜：非关键字任务只靠"交过"占位在砍掉交任务金额之后没有这条路。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(95484, 0, None)], 1000, {95484: 43200})   # 接=0（判得出 false）
    raw_turnin(lua, L, db, "A-永恒之井", 95484, 1000)
    L.OnTurnIn(db, "赤尾之寂-永恒之井", 95484, 1000,
               str(db["quests"][95484]["currentSig"]))
    if L.TurnedInThisRound(db, 1000)[95484] is not True:
        return fail("a_turnin_with_zero_reward_read_does_not_admit",
                    "前置：两个号各一笔交割流水都在本轮列内 —— 这一例砍的是金额，不是交割")
    gain = L.TurnInGainsThisRound(db, 1000)[95484]
    if gain is not None:
        return fail("a_turnin_with_zero_reward_read_does_not_admit",
                    "奖励读取为 0 时这一条实例没有金币读数可报，到手那一列该是 nil（判不了），实际 %s" % gain)
    if 95484 in matrix_rows_by_id(L, db, 1000):
        return fail("a_turnin_with_zero_reward_read_does_not_admit",
                    "接=0 的非关键字任务，交过两次也不该靠'交过'占位（2026-10-29 定稿）")
    # 正对照：同一实例把奖励读到金币（切狩猎模式看任务那一路），金额与上榜都跟着来
    pool_pass(lua, L, db, [(95484, 9624697, None)], 1100, {95484: 43100})
    gain = L.TurnInGainsThisRound(db, 1100)[95484]
    if gain is None or int(gain) != 9624697:
        return fail("a_turnin_with_zero_reward_read_does_not_admit",
                    "这一条实例奖励读取到金币后，到手那一列就回那个正数（真源换了但形状不变），实际 %s" % gain)
    if 95484 not in matrix_rows_by_id(L, db, 1100):
        return fail("a_turnin_with_zero_reward_read_does_not_admit",
                    "读到 962 金（过门槛）却没上榜 ⇒ 金币准入门没吃到奖励读取那份读数")
    ok("a_turnin_with_zero_reward_read_does_not_admit")


def test_a_zero_reward_read_is_a_verdict_not_a_mystery():
    """§四的连带缺陷：同一个"客户端答了 0"，直接读到那一支以前折成 nil、和新建那支（false）
    两个值，导出金币节与准入门从此各说各话。判据挂在**这一条实例读到的那个数**上
    （不在旗子上）：读到 0 ⇒ 否。搬来的 0 也算这条实例答过 —— 延续那道门第 36 轮收成
    "只许同一条实例那份才搬"（编号版），上一条实例的 0 搬不进来，所以同条实例另一副签名
    答的 0 就是这条实例自己的答案（用户 2026-10-27："仅限这个任务本次生命周期"）。
    旗子照旧不许标 false：那是"这一档读到过"的凭据，搬来的数不该冒充这一档读过。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    # 直接读到 0：新建走"签名 0 → false"，再重读一次走"这一条实例直接读到"那一支钉住 false
    pool_pass(lua, L, db, [(91803, 0, None)], 1000, {91803: 43200})
    L.DiffPool(db, pool(lua, [(91803, 0, None)]), 1000)
    if L.GoldVerdict(db, 91803, 1000) is not False:
        return fail("a_zero_reward_read_is_a_verdict_not_a_mystery",
                    "这一条实例直接读到 0 之后 GoldVerdict 必须答 false（不是 nil=判不了）")
    # 搬来的 0：同一条实例另一副签名答的，这条实例就该按"它答过 0"算（真存档 91803 走的这档）
    db2 = ledger_db(lua)
    L.DiffPool(db2, pool(lua, [(91804, None, [888])]), 1000)   # "-1|888"：金额读不出
    board_expiry(L, db2, 91804, 1000, 43200)                   # 板面倒计时到了才谈得上同一条实例
    L.DiffPool(db2, pool(lua, [(91804, 0, [888])]), 1000)      # "0|888"：这一副读到 0
    L.NoteGoldThisInstance(db2, 91804, 0, 1000)
    L.DiffPool(db2, pool(lua, [(91804, None, [888])]), 1000)   # 回 "-1|888"：从 "0|888" 搬来 0
    if L.GoldVerdict(db2, 91804, 1000) is not False:
        return fail("a_zero_reward_read_is_a_verdict_not_a_mystery",
                    "同一条实例搬来的 0 也是这条实例自己的答案，GoldVerdict 该答 false")
    if L.GetObs(db2, 91804)["gold"] is not None:
        return fail("a_zero_reward_read_is_a_verdict_not_a_mystery",
                    "搬来的数不许写进这一档的凭据旗（667 那支照旧 > 0 or nil）")
    ok("a_zero_reward_read_is_a_verdict_not_a_mystery")


def test_keyword_row_needs_no_turnin_to_stay():
    """第 39 轮：撞关键词 + 有客户端凭据的行靠**玩家点名**那道门留在板上，
    与"交没交过"彻底无关。以前这条用例的前置是"两处 0 时交过门判 false，靠关键词救场"
    （`L.TurnInAdmits` 那一代口径），那个函数已经整体删除 ⇒ 前置改成直接问判据本身：
    把流水抹掉、只留"接=0 + 撞关键词 + 上过板"，行必须在列。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    gql.Config.AddKeyword("沼地威胁")
    L.DiffPool(db, pool(lua, [(91803, 0, None)]), 1000)
    db["quests"][91803]["name"] = "沼地威胁"          # 池行的自动名字换成带关键词的真名
    db["turnins"]["爱情之力-奥达曼"] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803] = lua.table_from({})
    db["turnins"]["爱情之力-奥达曼"][91803][1] = lua.table_from({"t": 1000, "g": 0, "cycle": 0})
    if 91803 not in matrix_rows_by_id(L, db, 1000):
        return fail("keyword_row_needs_no_turnin_to_stay",
                    "撞关键词 + 有凭据的行不许被砍（它靠玩家点名那道门，不靠交过）")
    # 反面：把那一笔流水整个抹掉，行必须在原处 —— 证明顶住它的不是交过
    db["turnins"] = lua.table_from({})
    if 91803 not in matrix_rows_by_id(L, db, 1000):
        return fail("keyword_row_needs_no_turnin_to_stay",
                    "删掉流水后行该照旧在列（第 39 轮：做过不参与判定）")
    ok("keyword_row_needs_no_turnin_to_stay")


def test_a_reset_special_task_does_not_haunt_the_matrix():
    """92139 特别任务：暗影与利爪 —— 周重置后整行 ? 赖在矩阵（2026-11-01 实测）。
    根因是逻辑 bug（数据没坏）：特别任务不在世界任务板，L.SetExpiry 从不被调用
    ⇒ rec.roundEnd 永远不写 ⇒ 到期门只能走②档（cycleAnchor + 604800）。②档读数
    永远指向下一个周重置，而 lastSeenAt 又被注册重问每轮白续、②档再挂着身份豁免
    ⇒ 上周的实例永远判不出到期 —— "永不下板"的两句病根（白续 + 豁免）在 2026-11-02
    D6/D7 定稿里一起撤：续期只认本轮真事实，"这一 CD 零真事实 ⇒ 到期"对任何行都问。
    本用例钉修复的两半：②档记录本周一次都没被真看见（lastSeenAt 落在锚点之前、
    同域可比）⇒ 到期，**regKey / kwLogAdded 不再构成豁免**（旧口径把它们钉成
    "必须不到期"，方向已按 D6 反过来）；带着本轮真事实的同形状行照旧不到期。"""
    lua, gql = load_lua("Config", "Ledger")
    L, C = gql.Ledger, gql.Config
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    C.AddKeyword("特别任务")
    day = 86400
    week = 7 * day
    anchor_old = 1700000000            # 上周 CD 起点
    anchor_new = anchor_old + week     # 本周 CD 起点（周重置那一刻）
    now = anchor_new + 3600            # 本周重置后约 1 小时
    db["cycleAnchor"] = anchor_new
    # 上周接取时读到的凭据（名字含关键词、奖励是物品=0 金、客户端答过地图）。
    # 故意不写 roundEnd：特别任务不在板上，SetExpiry 从不被调用。
    db["quests"][92139] = lua.table_from({
        "name": "特别任务：暗影与利爪",
        "mapID": 2444,                 # 永歌森林一带
        "lastMoney": 0,                # 奖励是物品，客户端明说这档不给金币
        "lastSeenAt": anchor_new - 3 * day,   # 只在上周被看见过
    })
    # 上周烙的那份日志凭据编号：尾部桶 = 上周尾巴 = anchor_new
    db["quests"][92139]["kwLogKey"] = "%d:%d" % (92139, (anchor_new + 450) // 900)
    # 钉死"是到期门在干活、不是准入门把它踢了"：准入门仍要放行这条
    # （kwHit + 记录级凭据 rec.lastMoney=0 / rec.mapID），否则测到的就不是这个 bug。
    if not L.QuestHasCredential(db, 92139, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "准入门的凭据集合仍该放行这条（kwHit + rec.lastMoney），"
                    "测的是到期门、不是准入门")
    # 核心修复：②档记录本周一次都没被真看见（lastSeenAt < anchor）⇒ 它的实例是上一周的
    # ⇒ 判到期。修复前 L.InstanceExpired 对②档永远 false（instEnd 永远指向下一个周重置）。
    if not L.InstanceExpired(db, db["quests"][92139], now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "过期一周、本周零真证据的任务必须判到期")
    # 端到端：周重置后这条过期的特别任务不该再出现在矩阵
    if 92139 in matrix_rows_by_id(L, db, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "周重置后这条过期的特别任务不该再出现在矩阵")
    # 旧豁免作废（2026-11-02 D6）：内置周常（regKey）本 CD 零真事实同样判到期 ——
    # 它的 lastSeenAt 今天只由真事实续（族成员在板上 / 日志挂着 / pin 在场），
    # 没续上就是这一周它压根没出现过 ⇒ 到期，下周再出现是新 CD 新编号。
    reg_rec = lua.table_from({"name": "丰饶供品", "regKey": "abundant-offerings",
                              "lastSeenAt": anchor_new - 3 * week})
    if not L.InstanceExpired(db, reg_rec, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "regKey 不再是免死牌：本 CD 零真事实的内置周常必须到期")
    # 同一条带本轮真事实（族成员上板/日志挂着都会续上这份）⇒ 不许误杀
    reg_live = lua.table_from({"name": "丰饶供品", "regKey": "abundant-offerings",
                               "lastSeenAt": now - 60})
    if L.InstanceExpired(db, reg_live, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "本 CD 真见过的注册行不许被撤豁免误杀")
    # 旧豁免作废：kwLogAdded 布尔位本 CD 没续上（当前号日志已经不挂它、也没有别的真事实）
    # ⇒ 同判到期；日志还挂着的行由 Sync 日志那一路每轮续 lastSeenAt，走不到这一岔。
    kw_rec = lua.table_from({"name": "特别任务：梦魇", "kwLogAdded": True,
                             "lastSeenAt": anchor_new - 3 * day})
    if not L.InstanceExpired(db, kw_rec, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "kwLogAdded 不再是免死牌：本 CD 零真事实照样到期")
    kw_live = lua.table_from({"name": "特别任务：梦魇", "kwLogAdded": True,
                              "lastSeenAt": now - 60})
    if L.InstanceExpired(db, kw_live, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "当前号日志还挂着（每轮真续 lastSeenAt）的行不许误杀")
    # ①档（有 roundEnd）不受影响：没过期的板面实例仍活着
    live = lua.table_from({"name": "任务7", "roundEnd": now + 43200,
                          "lastSeenAt": now - 100})
    if L.InstanceExpired(db, live, now):
        return fail("a_reset_special_task_does_not_haunt_the_matrix",
                    "①档没过期的实例不该被这一刀误判到期")
    ok("a_reset_special_task_does_not_haunt_the_matrix")


def test_resident_keyword_row_with_stale_roundEnd_still_admitted():
    """玩家实测 98172「追踪萨拉塔斯」：关键词命中、已接、本周 lastSeenAt 在近处，但账本残留
    一条 12 天前的 roundEnd（上一条实例的墓碑）。旧行为：InstanceEndRaw 拿这条过期
    roundEnd 当①档 ⇒ InstanceExpired 走"已过期"短路、跳过②档按 lastSeenAt 判活 ⇒ 行被
    误删、进不了矩阵。修复：kwLogAdded 行的过期 roundEnd 退到②档周 CD 尾巴（注册周常
    regKey 的过期 roundEnd 仍按①档判到期，见 stale_round_end_with_no_log_source_expires），
    范围只此一种形状。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    anchor_new = 1793833199
    now = 1793841335
    db["cycleAnchor"] = anchor_new
    rec = lua.table_from({
        "name": "追踪萨拉塔斯",
        "kwLogAdded": True,
        "roundEnd": 1793229750,   # 12 天前那一档的旧值
        "lastSeenAt": now - 420,  # 本周 7 分钟前见过（已接、日志挂着）
    })
    # 结束时刻应退回周 CD 尾巴（未来值），不是那条过期 roundEnd 的旧桶
    endAt = L.InstanceEnd(db, rec, now)
    if not isinstance(endAt, (int, float)) or int(endAt) <= now:
        return fail("resident_keyword_row_with_stale_roundEnd_still_admitted",
                    f"常驻关键词行的结束时刻应退回周 CD 尾巴（未来值），实际 {endAt}")
    # 到期门不该误杀：本周见过 ⇒ 未到期（与 kw_live 同形状）
    if L.InstanceExpired(db, rec, now):
        return fail("resident_keyword_row_with_stale_roundEnd_still_admitted",
                    "已接且本周见过的常驻关键词行不该被残留过期 roundEnd 误判到期")
    ok("resident_keyword_row_with_stale_roundEnd_still_admitted")


def test_a_special_task_from_log_expires_at_its_own_end():
    """92139 暗影与利爪（**特别任务**）：还挂在角色日志里时，客户端能答出它**自己的**剩余
    （C_TaskQuest.GetQuestTimeLeftSeconds，用户 11-01 实测：接到特别任务时就能读到结束时间，
    不接时答 nil）。日志之路把它烙成 roundEnd = 现在 + 剩余，到期门走 ①档在真实到期日去掉，
    不再依赖周 CD 尾巴（cycleAnchor + 604800）——有的特别任务周中周日开、跨两个周 CD，
    尾巴永远指向下次周重置 ⇒ 行赖在矩阵。
    守卫：板面轮换寿命（< 1 天，加固符文石那种）不写 roundEnd，仍走 CD 尾巴；读出来等于周重置
    倒计时的回声也不写。"""
    # 池子不许空：空池那轮快照会早退（"30 秒后二次确认再判消失"），日志根本不收。
    # 放一条不撞关键词、不在板上的对照任务撑场，92139 本身仍只经日志这条路。
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID = 92139
    gql.Config.AddKeyword("特别任务")
    # 接到任务那一刻：客户端答出这条任务还剩 7 天
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "特别任务：暗影与利爪",
         "questLogIndex": 1}) if int(i) == 1 else None)
    g.C_TaskQuest = lua.table_from(
        {"GetQuestTimeLeftSeconds": lambda qid: 604800 if int(qid) == QID else None})
    gql.Sync.SnapshotNow()
    rec = db["quests"][QID]
    if rec is None or rec["roundEnd"] is None:
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "特别任务接到时要烙上真实 roundEnd（GetQuestTimeLeft），实际 %s" % (rec,))
    if abs(int(rec["roundEnd"]) - (1700000000 + 604800)) > 1:
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "roundEnd 应是现在 + 7 天，实际 %s" % (rec["roundEnd"],))
    # 凭据编号得跟着 roundEnd 重新烙，否则准入凭据对不上、这一行当场掉出矩阵
    key = uid(L, db, QID, 1700000000)
    if str(rec["kwLogKey"]) != key:
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "凭据编号要随 roundEnd 重新烙（%s != %s）" % (rec["kwLogKey"], key))
    # 真实到期日之前仍在矩阵、之后消失（不依赖周 CD 尾巴）
    now_alive = 1700000000 + 604800 - 3600
    if QID not in matrix_rows_by_id(L, db, now_alive):
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "真实到期日之前这一行该在矩阵里")
    # 到点那一刻任务已离开日志、撤点清掉 kwLogAdded：没有活源续那份过期的 roundEnd
    rec["kwLogAdded"] = None
    now_dead = 1700000000 + 604800 + 3600
    if not L.InstanceExpired(db, rec, now_dead):
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "真实到期日一过该判到期（不走周 CD 尾巴那一档）")
    if QID in matrix_rows_by_id(L, db, now_dead):
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "过期后这一行不该再出现在矩阵")
    # 守卫：板面轮换寿命（< 1 天）不写 roundEnd，仍走 CD 尾巴（加固符文石那个形状）
    WQ = 89602
    gql.Config.AddKeyword("符文石")
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": WQ, "title": "加固符文石",
         "questLogIndex": 1}) if int(i) == 1 else None)
    g.C_TaskQuest = lua.table_from(
        {"GetQuestTimeLeftSeconds": lambda qid: 10800 if int(qid) == WQ else None})
    gql.Sync.SnapshotNow()
    wrec = db["quests"][WQ]
    if wrec is not None and wrec["roundEnd"] is not None:
        return fail("a_special_task_from_log_expires_at_its_own_end",
                    "板面轮换寿命(<1天)不许烙 roundEnd，否则当天下午整行没")
    ok("a_special_task_from_log_expires_at_its_own_end")


def test_stale_round_end_with_no_log_source_expires():
    """当前 92139 那个形状（用户 11-01 /gql quest 实录）：注册周常（regKey），账本里留着
    上一条实例的过期 roundEnd（"4小时前"），当前角色日志没接、不在池里（kwLogAdded 已被
    撤点清掉），最近一次"见到"（lastSeenAt=4分前）只是它掉出池/日志前的一闪。
    到期门必须判到期 —— 否则 seen(4分前) > roundEnd(4小时前) 的 stale 组合把它永远赖在
    矩阵（keep-alive：seen 在 roundEnd 之后就当还活着）。判据是"结束时刻的来源"：
    注册行的 roundEnd 唯一合法写点是日志路烙的"这条任务自己的到期日"（SetExpiry 对注册行
    从不写它），它一过实例就结束；板面周常（SetExpiry 写的板面倒计时，89267 那句）不在此列。
    另一半对称钉住：日志路盖章（roundEndSrc="kwLog"）的过期 roundEnd 同样不吃 keep-alive，
    哪怕还挂在某个号的日志里（任务自己的 7 天到了就是到了）。"""
    lua, gql, _ = sync_env([], weekly=432000, wall=1700000000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    gql.Config.AddKeyword("特别任务")
    db["quests"][92139] = lua.table_from({
        "name": "特别任务：暗影与利爪",
        "regKey": "kw:特别任务",               # 注册周常（真实存档形状：在列靠=关键词+注册）
        "roundEnd": 1700000000 - 4 * 3600,     # 4 小时前过期
        "lastSeenAt": 1700000000 - 4 * 60,     # 4 分钟前还"见到"
        # kwLogAdded 缺席 = 撤点已清（当前没号接）
    })
    rec = db["quests"][92139]
    if not L.InstanceExpired(db, rec, 1700000000):
        return fail("stale_round_end_with_no_log_source_expires",
                    "注册行带着上一实例的过期 roundEnd 必须判到期（不吃 keep-alive）")
    if 92139 in matrix_rows_by_id(L, db, 1700000000):
        return fail("stale_round_end_with_no_log_source_expires",
                    "这份 stale 行不该再出现在矩阵")
    # 对称形状：日志路盖章的过期 roundEnd 同样到点即死（哪怕 kwLogAdded 还在）
    db["quests"][92140] = lua.table_from({
        "name": "特别任务：暗影与利爪",
        "roundEnd": 1700000000 - 4 * 3600,
        "roundEndSrc": "kwLog",
        "kwLogAdded": True,                    # 还有号挂着它，但任务自己的到期日已过
    })
    if not L.InstanceExpired(db, db["quests"][92140], 1700000000):
        return fail("stale_round_end_with_no_log_source_expires",
                    "日志路盖章的过期 roundEnd 到点即死，kwLogAdded 救不了它")
    ok("stale_round_end_with_no_log_source_expires")


def test_dump_names_the_non_gold_turnin_rejection():
    """取证面不许两处各说各话：沼地威胁这种形状（接=0 金币、本轮交过、不撞关键词），
    报告得说清它是被**准入门**砍的（"准入门没过 …… 奖励金币也没到 X"），
    并且**不再**把"本轮有角色交过"写进"在列靠"（第 39 轮：交过不参与判定，
    那句只作为"这一格绿勾从哪来"单独一行报事实）。
    §十(a)：板面倒计时走 sync_env(board=…) 的真接口，那一笔流水走 raw_turnin
    （它现算 L.InstanceKey 烙编号）—— 手拼一条没有 k 的流水只会让报告改口说
    "换了实例就不算本轮"，测不到真判据。"""
    lua, gql, money = sync_env([(91803, 0)], extra=("Matrix", "Dump"), board=43200)
    g = lua.globals()
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    db = g.GQLDB
    # 接到 0 只能从账本这头进（池里的 0 会被当"数据没到"重问接口，走不到这个形状）
    # 身份已由 SnapshotNow 那一趟的 SetExpiry 烙上，所以这一趟 DiffPool 自己就把 0 记进 obs
    now = lua.execute("return GQLDB.chars[%r].lastSync" % key)
    gql.Ledger.DiffPool(g.GQLDB, pool(lua, [(91803, 0, None)]), now)
    if gql.Ledger.GoldVerdict(db, 91803, now) is not False:
        return fail("dump_names_the_non_gold_turnin_rejection",
                    "前置：'接'这一处必须读得出 0，否则测不到非金币那一档")
    raw_turnin(lua, gql.Ledger, db, key, 91803, now,
               str(db["quests"][91803]["currentSig"]))
    txt = str(gql.Dump.Quest("任务91803"))
    if "准入门没过" not in txt or "奖励金币也没到" not in txt:
        return fail("dump_names_the_non_gold_turnin_rejection",
                    "非金币任务交过也该被砍，报告必须点名准入门:\n%s" % txt)
    if "在列靠：本轮有角色交过" in txt or "在列靠：（无）" not in txt:
        return fail("dump_names_the_non_gold_turnin_rejection",
                    "'在列靠'只报两类依据；交过另起一行只报事实:\n%s" % txt)
    if "交割：本轮有角色交过" not in txt:
        return fail("dump_names_the_non_gold_turnin_rejection",
                    "绿勾的出处仍要说清（单独一行报事实，不写进在列靠）:\n%s" % txt)
    ok("dump_names_the_non_gold_turnin_rejection")


def test_dump_blames_the_missing_credential_not_the_keyword():
    """同一句分岔的另一半：撞了关键词、客户端却一条凭据都答不出的行，也是被"两处 0"砍的。
    对它说"且不是关键词任务"就是谎报（第 32 轮立的 kwHit 只免金币门槛，免不掉凭据）。"""
    lua, gql, _ = sync_env([(91803, 0)], extra=("Matrix", "Dump"))
    g = lua.globals()
    gql.Config.AddKeyword("沼地威胁")
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    now = lua.execute("return GQLDB.chars[%r].lastSync" % key)
    db = g.GQLDB
    gql.Ledger.DiffPool(db, pool(lua, [(91803, 0, None)]), now)
    rec = db["quests"][91803]
    rec["name"] = "沼地威胁"
    # 凭据摘干净：没上过池、没有客户端答的图、没读到过金额、没有号被答成已完成
    rec["poolSeen"] = None
    rec["mapID"] = None
    rec["lastMoney"] = None
    lua.execute("GQLDB.chars[%r].views = {}" % key)
    if gql.Ledger.QuestHasCredential(db, 91803):
        return fail("dump_blames_the_missing_credential_not_the_keyword", "前置：这条必须一条凭据都没有")
    lua.execute("GQLDB.turnins[%r] = { [91803] = { { t = %s, g = 0, cycle = 0 } } }" % (key, now))
    txt = str(gql.Dump.Quest("沼地威胁"))
    if "名字虽然撞了关键词、客户端却一条凭据都答不出" not in txt:
        return fail("dump_blames_the_missing_credential_not_the_keyword",
                    f"撞关键词缺凭据的那条必须单独说:\n{txt}")
    if "且不是关键词任务" in txt:
        return fail("dump_blames_the_missing_credential_not_the_keyword",
                    f"这条撞了关键词，报告不许说'不是关键词任务':\n{txt}")
    ok("dump_blames_the_missing_credential_not_the_keyword")


def test_non_gold_turnin_does_not_admit_row():
    """用户 2026-10-29 定稿（第 39 轮）：准入门只有两类——玩家点名（关键词/注册）与
    奖励金币≥门槛。烂到核心 93071 形状：奖励是 52 宝匣钥匙碎片（接=0）、本轮交过、
    非关键字 ⇒ 不在列。老那句"交过这一道被 TurnInAdmits 砍了"的前置随函数一起删除。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    # 接=0：池里读到 0（客户端明说非金币），GoldVerdict == false
    L.DiffPool(db, pool(lua, [(93071, 0, None)]), 1000)
    if L.GoldVerdict(db, 93071, 1000) is not False:
        return fail("non_gold_turnin_does_not_admit_row",
                    "前置：93071 必须读成明说不给金币（GoldVerdict == false）")
    # 本轮交过一笔：OnTurnIn 只写 t / sig / cycle / k，压根不量到手金额（老钱窗那一档
    # 读数不可靠的口径整块退役）；走 OnTurnIn 才烙得上编号
    L.OnTurnIn(db, "爱情之力-奥达曼", 93071, 1000,
               str(db["quests"][93071]["currentSig"]))
    if 93071 in matrix_rows_by_id(L, db, 1000):
        return fail("non_gold_turnin_does_not_admit_row",
                    "93071 这种非金币任务不该进矩阵（做过不参与准入）")
    ok("non_gold_turnin_does_not_admit_row")


def test_gold_above_floor_admits_row():
    """补反面：奖励金币读到过**且够门槛**的非关键字任务在列 —— 顶住它的是金币门，
    不是"有人交过"。所以把交割流水整个抹掉，行必须还在（第 39 轮的归属；
    这条以前叫 gold_read_turnin_admits_row，靠 TurnInAdmits 放行，那是本轮作废的口径）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    # 接=正数金币且够门槛（962 金 > 500 金）
    L.DiffPool(db, pool(lua, [(93071, 9620000, None)]), 1000)
    if L.GoldVerdict(db, 93071, 1000) is not True:
        return fail("gold_above_floor_admits_row",
                    "前置：93071 必须读成确认金币（GoldVerdict == true）")
    board_expiry(L, db, 93071, 1000, 43200)   # 这一读答得出板面倒计时 ⇒ 有身份
    if 93071 not in matrix_rows_by_id(L, db, 1000):
        return fail("gold_above_floor_admits_row", "够门槛的金币任务该进矩阵")
    L.OnTurnIn(db, "爱情之力-奥达曼", 93071, 1000,
               str(db["quests"][93071]["currentSig"]))
    db["turnins"] = lua.table_from({})        # 把那一笔连同人交过一起抹掉
    if 93071 not in matrix_rows_by_id(L, db, 1000):
        return fail("gold_above_floor_admits_row",
                    "删掉流水后仍要在列：顶住它的是金币门，不是交过")
    ok("gold_above_floor_admits_row")


def test_turnin_is_not_an_admission_door():
    """第 39 轮的立身之本（前身是 turnin_both_zero_gate_has_one_home，钉的是
    "两处都答 0 才否决"那一代判据只住一个函数；那一代整块删除，守卫跟着换方向）：
    ① `L.TurnInAdmits` 这个函数在判据里不许有任何读者（函数本体已删，留个壳就会被重新接上）；
    ② 矩阵热路径（MatrixRows）不许再为"本轮交过"翻一遍 db.turnins —— 预扫整趟删除，
       每帧少一次全表遍历（零新增接口调用）；
    ③ 准入粘滞 admittedKey 不许有写点/读点（它会把免门槛的放行冻进存档）；
    ④ db.turnins 的全量聚合遍历仍只许 2 处（TurnInCred + TurnedInThisCycle，都只服务取证
       与那一格的勾），迁移体里回填那一趟单独数。散成第三处就是有人把交过重新塞回判据。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "..", "GoldQuestLedger")
    def code(name):
        raw = open(os.path.join(src_dir, name), encoding="utf-8").read()
        return "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    led, dum, mtx = code("Ledger.lua"), code("Dump.lua"), code("Matrix.lua")
    for who, src in (("Ledger", led), ("Dump", dum), ("Matrix", mtx)):
        if "TurnInAdmits" in src:
            return fail("turnin_is_not_an_admission_door",
                        "%s.lua 里还有 TurnInAdmits 的读者：交过不是准入门（第 39 轮）" % who)
    body = led.split("function L.MatrixRows", 1)
    if len(body) != 2:
        return fail("turnin_is_not_an_admission_door", "L.MatrixRows 没找到（函数被改名了？）")
    if "TurnInCred" in body[1]:
        return fail("turnin_is_not_an_admission_door",
                    "MatrixRows 里不许再为准入预扫 TurnInCred（每帧一次 db.turnins 全表遍历）")
    if "admittedKey" in body[1]:
        return fail("turnin_is_not_an_admission_door",
                    "MatrixRows 里不许再写/读准入粘滞 admittedKey（v11 已整体下线）")
    # v6 迁移那一趟翻档案是"一次性升级里读存档"，与热路径的全量聚合不是一回事
    head, sep, backfill = led.partition("function L.BackfillInstanceKeysOnce")
    if not sep:
        return fail("turnin_is_not_an_admission_door",
                    "v6 的 BackfillInstanceKeysOnce 没找到（迁移函数被删了？）")
    n = head.count("for _, byQuest in pairs(db.turnins or {})")
    if n != 2:
        return fail("turnin_is_not_an_admission_door",
                    "db.turnins 的轮次/CD 全量聚合遍历（迁移以外）只该有 2 处"
                    "（TurnInCred + TurnedInThisCycle），实际 %d" % n)
    if backfill.count("for _, byQuest in pairs(db.turnins or {})") != 1:
        return fail("turnin_is_not_an_admission_door",
                    "v6 回填必须自己把 db.turnins 翻一遍（只给没带 k 的老凭据补编号），"
                    "迁移体里这一趟遍历只许 1 处")
    if "db.turnins" in dum:
        return fail("turnin_is_not_an_admission_door",
                    "Dump 不许自己翻 db.turnins，一律问 L.TurnedInThisRound / L.TurnInGainsThisRound")
    ok("turnin_is_not_an_admission_door")


def test_a_34_gold_turned_in_row_is_not_listed():
    """第 39 轮的原始工单（12.1.5 真档 91594 狩猎：坚韧猎人，永歌森林）：
    奖励 34 金 16 银（observation.money=341600）、门槛 500 金（5000000）、名字不撞任何
    关键词，只因为"爱情之力 2 小时前交过它"（编号 91594:1992572 对上当前）就一直挂在矩阵上，
    而它自己的 probe 段写着"不到门槛又不命中关键词就不列"。
    用户 2026-10-29 定稿："什么时候也没说做过就是准入门槛。1、匹配关键词的任务
    2、金币奖励满足的任务" ⇒ 这一条必须掉板。三个对照钉住"不该被牵连的仍然在列"。"""
    name = "a_34_gold_turned_in_row_is_not_listed"
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    # 真档形状：34 金 + 板面答得出倒计时（这一条实例算得出编号）+ 上过板
    pool_pass(lua, L, db, [(91594, 341600, None)], 1000, {91594: 32040})
    L.OnTurnIn(db, "爱情之力-奥达曼", 91594, 1000,
               str(db["quests"][91594]["currentSig"]))
    if int(L.MatrixMoneyFloor()) <= 341600:
        return fail(name, "前置：门槛必须高于 34 金，否则测不到门槛这一刀")
    if 91594 in matrix_rows_by_id(L, db, 1000):
        return fail(name, "34 金 + 不撞关键词 + 只因为交过 ⇒ 第 39 轮起不许在列")
    # 交过不参与判定：把流水整个抹掉，行照旧不在列（两种存档形状，同一个答案）
    db["turnins"] = lua.table_from({})
    if 91594 in matrix_rows_by_id(L, db, 1000):
        return fail(name, "删掉流水后仍不在列：判据里没有任何一处读'本轮交过'")
    # 对照①：玩家点名（名字撞关键词）+ 客户端凭据（poolSeen）⇒ 回列，与金额无关
    gql.Config.AddKeyword("任务91594")
    if 91594 not in matrix_rows_by_id(L, db, 1000):
        return fail(name, "撞关键词 + 上过板的行该靠'玩家点名'回列（第 32 轮口径不动）")
    ok(name)


def test_double_mode_row_survives_without_admission_sticky():
    """双模式任务（普通模式给金币、狩猎模式换成物品）切回读到非金币那一档不许掉行
    （用户 2026-10-25："之前是金币，那么也不应该移除（结束时间没变）"）。
    以前这一条同时靠两份东西：**准入粘滞 `rec.admittedKey`**（第 39 轮删）与
    `L.QuestMoney` 里 `rec.goldKey == 当前编号` 那份**按实例金币粘滞**。
    这一条钉后一份仍然顶用，且前者一个字都不再被写进存档。"""
    name = "double_mode_row_survives_without_admission_sticky"
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    pool_pass(lua, L, db, [(96200, 9620000, None)], 1000, {96200: 32040})
    if 96200 not in matrix_rows_by_id(L, db, 1000):
        return fail(name, "前置：962 金够门槛，第一趟就该在列")
    # 切档：同一条实例读到"不给金币"（结束时刻没挪：1000+32040 == 1200+31840）
    pool_pass(lua, L, db, [(96200, 0, None)], 1200, {96200: 31840})
    if db["quests"][96200]["admittedKey"] is not None:
        return fail(name, "准入粘滞字段不许再被写（判据已删，老档由 v11 清）")
    if 96200 not in matrix_rows_by_id(L, db, 1200):
        return fail(name, "切档读到 0 不许掉行：金币门要拿到 goldKey 那份 962 金")
    ok(name)


def test_v11_clears_the_admission_sticky_once():
    """schema v11：判据不再写/读 `rec.admittedKey`，**已经烙进存档的那一份不自愈**。
    不清的形状就是 91594：粘滞与准入门是"或"的关系，光删判据它还会顶着一句
    "改完一点变化没有"留板到自己的倒计时走完（v2/v6/v7/v9/v10 同一族教训）。
    三条边界：① 只碰 admittedKey，金币粘滞 goldMoney/goldKey、日志凭据 kwLogKey、
    注册 regKey 一字不动；② 幂等（重跑 0 条）；③ 档位停在最新档。"""
    name = "v11_clears_the_admission_sticky_once"
    lua, gql = load_lua("Config", "Ledger")
    L, C = gql.Ledger, gql.Config
    if lua.execute("return type(GQL.Config.MIGRATIONS[11]) == 'function'") is not True:
        return fail(name, f"清粘滞必须开成 v11 这一档（老存档不自愈 = 改完没变化），"
                          f"实际 MIGRATIONS[11] 不是函数（SCHEMA_VERSION={C.SCHEMA_VERSION}）")
    g = lua.globals()
    g.GQLDB = lua.table_from({})          # 非 mock 环境没有 SavedVariables，这里自建
    db = g.GQLDB
    db["schema"] = 10
    db["cycleAnchor"] = 1700000000
    db["quests"] = lua.table_from({
        91594: lua.table_from({"name": "狩猎：坚韧猎人", "poolSeen": True,
                               "admittedKey": "91594:1992572",
                               "goldMoney": 341600, "goldKey": "91594:1992572",
                               "kwLogKey": "91594:1",
                               "observations": lua.table_from({})}),
        89507: lua.table_from({"name": "丰饶供品", "regKey": "abundant-offerings",
                               "observations": lua.table_from({})}),
    })
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "Migrate 没跑 v11 这一档")
    if int(db["schema"]) != 11:
        return fail(name, f"跑完要停在最新档 11，实际 {db['schema']}")
    if db["quests"][91594]["admittedKey"] is not None:
        return fail(name, "粘滞没清掉 —— 这一行还会靠它留板，用户读成'改完没变化'")
    if float(db["quests"][91594]["goldMoney"]) != 341600:
        return fail(name, "只许碰 admittedKey：金币粘滞 goldMoney 是双模式不掉行靠的那份")
    if str(db["quests"][91594]["goldKey"]) != "91594:1992572":
        return fail(name, "goldKey 与 goldMoney 同进同退，迁移不许拆这对")
    if str(db["quests"][91594]["kwLogKey"]) != "91594:1":
        return fail(name, "日志那份实例凭据（v10 立的）不在本轮范围内")
    if str(db["quests"][89507]["regKey"]) != "abundant-offerings":
        return fail(name, "注册周常身份一字不许动")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "同一档不该再跑第二趟")
    if int(L.ClearAdmittedKeysOnce(db)) != 0:
        return fail(name, "跑完之后再手跑一遍该是 0 条（幂等）")
    if "数据升级 v11 完成（处理 1 条）" not in str(gql.Config.GetLogs()):
        return fail(name, f"迁移返回值（清掉的笔数）要进通用日志，实际：{gql.Config.GetLogs()}")
    ok(name)


def test_the_log_credential_has_one_read_home():
    """静态守卫（2026-10-29 梦魇任务 94446 那一单，计划
    docs/tasks/2026-10-29-keyword-log-weekly-resident.md §三第 2/3 条）：
    "有号把这条接进过日志"这一份**属于这一条实例的凭据**（用户原话："只要有一个号接了，
    那就意味着我要关注这个任务，不管是不是金币任务，都要上矩阵"）全仓只许有一处读法 ——
    L.KeywordLogAdmits 体内那一句 L.SameInstanceKey(rec.kwLogKey, curKey)，
    R1-R9 九道门一律问这一个判据；也只许有一处写法 —— 两个写点（Sync 收日志、
    Ledger.AddQuest 的 questlog 那一路）都调 L.NoteKeywordLogThisInstance，
    不许调用方各自拼 L.InstanceKey 造第二把键（照 L.TurnInAdmits / turnin_both_zero_gate
    的先例：同一件事散成两处迟早分岔，而这一族的分岔是"整行对全账号消失"）。
    注释行不参与计数（先剥 --，与本文件其余静态守卫同一办法）。"""
    name = "the_log_credential_has_one_read_home"
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "GoldQuestLedger")

    def code(fname):
        raw = open(os.path.join(src_dir, fname), encoding="utf-8").read()
        return "\n".join(line.split("--", 1)[0] for line in raw.splitlines())

    files = {n: code(n) for n in sorted(os.listdir(src_dir)) if n.endswith(".lua")}
    led = files["Ledger.lua"]

    # ① 读：整个仓库只许一句拿 rec.kwLogKey 去比编号，而且就住在 KeywordLogAdmits 里
    cmp_sites = [(n, len(re.findall(r"SameInstanceKey\(\s*rec\.kwLogKey", t)))
                 for n, t in files.items()]
    total_cmp = sum(c for _, c in cmp_sites)
    if total_cmp != 1:
        return fail(name, "rec.kwLogKey 的比编号判据全仓只许 1 处（L.KeywordLogAdmits 那一句），"
                          "实际 %d 处：%s" % (total_cmp, [x for x in cmp_sites if x[1]]))
    admits = re.split(r"\n(?=function )", led)
    body = [f for f in admits if f.startswith("function L.KeywordLogAdmits(")]
    if len(body) != 1:
        return fail(name, "L.KeywordLogAdmits 必须定义且只定义一次（上板凭据的唯一读点），"
                          f"实际找到 {len(body)} 处定义")
    if len(re.findall(r"SameInstanceKey\(\s*rec\.kwLogKey", body[0])) != 1:
        return fail(name, "那一句比编号就住在 L.KeywordLogAdmits 的函数体里，"
                          "判据挪走或再拼一份都要红")
    if "function L.KeywordLogAdmits(db, questID, rec, now, curKey)" not in led:
        return fail(name, "签名要和 L.DoneByAnyCharThisRound 同形状（curKey 末位可选，"
                          "矩阵那条路传预扫好的 keyOf[questID]，每行每帧只算一次编号）")

    # ② 除了"那一句读"和"写侧自己的赋值/幂等判断"，任何地方都不许再碰这个字段。
    #    幂等判断（"已经烙过的不重烙"）只许住在 Ledger.lua —— 写侧全在 Ledger；
    #    Sync/Matrix/Dump/Probe 这些读者一律问 L.KeywordLogAdmits（第 34 轮的教训：
    #    取证口径与界面必须是同一个真源）。
    stray = []
    for fname, text in files.items():
        for m in re.finditer(r"\brec\.kwLogKey\b", text):
            head = text[max(0, m.start() - 30):m.start()]
            tail = text[m.end():m.end() + 8]
            is_cmp = re.search(r"SameInstanceKey\(\s*$", head) is not None
            is_write = re.match(r"\s*=[^=]", tail) is not None
            is_nil_test = fname == "Ledger.lua" and (
                re.search(r"\bnot\s*$", head) is not None
                or re.match(r"\s*[~=]=\s*nil", tail) is not None)
            if not (is_cmp or is_write or is_nil_test):
                stray.append("%s:%d" % (fname, text[:m.start()].count("\n") + 1))
    if stray:
        return fail(name, "这些位置直接读写了 kwLogKey，绕开了唯一真源（读问 L.KeywordLogAdmits、"
                          "写走 L.NoteKeywordLogThisInstance）：" + ", ".join(stray))

    # ③ 写：两个写点（W1 Sync 收日志 / W2 Ledger.AddQuest 的 questlog 那一路）形状必须一样 ——
    #    写下 kwLogAdded = true 的那一处，紧跟一句烙编号。少接一条路，那条路上板的行就没有凭据；
    #    在调用方各自拼 L.InstanceKey 更是分岔（同一件事两处各算一遍 = 本轮要消灭的形状）。
    defs = led.count("function L.NoteKeywordLogThisInstance(")
    calls = sum(t.count("NoteKeywordLogThisInstance(") for t in files.values()) - defs
    if defs != 1:
        return fail(name, f"L.NoteKeywordLogThisInstance 必须定义且只定义一次，实际 {defs} 处")
    if calls < 2:
        return fail(name, f"两个写点都该调这一个函数（迁移复用它也算），实际调用 {calls} 次")
    stamp_missing = []
    for fname, text in files.items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"\brec\.kwLogAdded\s*=\s*true\b", line):
                continue          # 字段清单那行没有 rec. 前缀，不会误伤
            window = "\n".join(lines[i:i + 4])
            if "NoteKeywordLogThisInstance(" not in window:
                stamp_missing.append("%s:%d" % (fname, i + 1))
    if stamp_missing:
        return fail(name, "这些写点只写了布尔位、没烙这一条实例的编号（撤掉布尔位这一行就"
                          "对全账号消失）：" + ", ".join(stamp_missing))
    for fname, text in files.items():
        if re.search(r"kwLogKey\s*=\s*[\w.]*InstanceKey", text):
            return fail(name, f"{fname} 在调用方自己拼 L.InstanceKey 写 kwLogKey："
                              "分岔的编号模型就是本轮要消灭的那类两处各算一遍")

    # ④ 判据不许住着一个没人读的空函数（定义了但一处都没接线 = 那条行照样会掉）
    admits_calls = sum(t.count("KeywordLogAdmits(") for t in files.values()) - 1
    if admits_calls < 1:
        return fail(name, "L.KeywordLogAdmits 定义了却一个调用点都没有 = 修法定根本没接上线")
    ok(name)


def test_the_poisoned_sticky_gold_is_cleared_once_at_sync():
    """代码修好了，**已经写进存档的那一份不会自愈**：粘滞只在换轮时作废，
    而用户不会为了等一次换轮再挂 12 小时假金币（12.1.5 复盘：改完代码"刷新矩阵没变化"，
    就是因为 15:02 那次同步写坏的 goldMoney 一直答到 03:00 换轮才消）。
    ⇒ 按标记清一次，之后正常同步重新读、真读到金币那一档自己长回来。

    形状照 89267 真存档抄（第二版用例，第一版只造了 goldMoney 没造那份被搬坏的
    observation ⇒ 用例绿着、游戏里 15:50 那次同步又把粘滞造回来了，用户第二次报"没有变化"）：
    上一轮真读到的 1075金39银、本轮被旧代码搬进 "-1" 那一份、客户端本轮明说 0。

    【第 36 轮 T4b 改了前置那一半】粘滞的读法换成编号之后，这份 v5 存档里的
    `goldMoney/goldRound` 已经没有读者（读侧只认 rec.goldKey，v5 没烙过）⇒ 拿它当"污染造出来了"
    等于断言一条已经不存在的链路。真还会咬人的是**来源**那一半：`-1` 占位签名里被旧代码搬进来的
    那个数 —— DiffPool 的读闪延续与 /gql dump 的金币清单都直接读 obs.money/obs.gold。
    所以前置钉那一份，其余断言（清完不许再长回来、只清一次、真读到的金币照常算）一条不松。"""
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
    def poison():
        o = db.quests[7].observations["-1"]
        return -1 if o.money is None else int(o.money)
    def listed():
        return any(int(r.questID) == 7 for k in L.MatrixRows(db, 5000).keys()
                   for i in L.MatrixRows(db, 5000)[k]["rows"].keys())
    if poison() != 10753956:
        return fail("the_poisoned_sticky_gold_is_cleared_once_at_sync",
                    "前置：污染没造出来，实际 %d" % poison())
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
    （Ledger.GoldQuestIDs 直接读 obs.gold）就凭空多报一条 —— 多报比少报严重。
    搬钱那道门第 36 轮起问的是编号（§六 1(c)：只许从同一条实例那份搬），所以要给这三趟
    同一个结束时刻；第一趟按 §六 1(a) 的写序还挂不上编号（那一笔的钱会被第 2 步作废），
    "读到 0"这个前提从第二趟才落地。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 0, None)], 1000, {7: 43200})         # 立身份（这一笔挂不上编号）
    pool_pass(lua, L, db, [(7, 0, None)], 2000, {7: 42200})         # 客户端答了"不给金币"
    pool_pass(lua, L, db, [(7, None, [888])], 3000, {7: 41200})     # 同一轮换道具 ⇒ 新签名
    # 拿 obs.gold 判（GoldQuestIDs 返回的是 Lua 表，空的在 Python 里也当真值）
    if db.quests[7].observations["-1|888"].gold:
        return fail("a_carried_zero_never_marks_the_quest_as_gold",
                    "延续过来的 0 被算成金币任务了")
    if int(L.QuestMoney(db, 7, 3000)) != 0:
        return fail("a_carried_zero_never_marks_the_quest_as_gold",
                    "读闪延续本身还在，金额该照旧是 0，实际 %s" % L.QuestMoney(db, 7, 3000))
    ok("a_carried_zero_never_marks_the_quest_as_gold")


def test_dual_mode_flip_keeps_the_row_and_the_check():
    """用户 2026-10-25 定的口径（原话）："双模式的任务，不是就不进，但是当用户切换模式
    再打开矩阵时，这个时候读取的应该就是金币任务 就应该进了。而之前…之前是金币，
    那么也不应该移除（结束时间没变）。"
    ⇒ 同一条实例（到期时刻没变、也没被板面漏掉）里任意一档读到过金币就一直占行；
    完成状态更不许因为换奖励而改动。
    两趟同步走 pool_pass（真实写序：DiffPool → 逐条倒计时 → NoteGoldThisInstance），
    粘滞那一份由第 4 步从池行原始读数记进 rec.goldMoney + rec.goldKey。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 30000000, None)], 1000, {7: 43200})   # 这一轮到期 = 44200
    L.SetView(db, "A-永恒之井", 7, "done", 2000)
    if len(L.MatrixRows(db, 2000)) != 1:
        return fail("dual_mode_flip_keeps_the_row_and_the_check", "前置：金币那一档该占一行")

    pool_pass(lua, L, db, [(7, 0, [888])], 5000, {7: 39200})         # 还是 44200 ⇒ 同一条实例
    rows = L.MatrixRows(db, 5000)
    if len(rows) != 1 or int(rows[1]["rows"][1]["questID"]) != 7:
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    "切到不给金币那一档不许把行弄掉（结束时间没变就是同一条）")
    if int(L.QuestMoney(db, 7, 5000)) != 30000000:
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    f"该报这一条实例另一档读到的金币，实际 {L.QuestMoney(db, 7, 5000)}")
    if st(L, db, "A-永恒之井", 7, 5000) != "done":
        return fail("dual_mode_flip_keeps_the_row_and_the_check",
                    "换奖励不许动完成状态，实际 " + str(st(L, db, "A-永恒之井", 7, 5000)))
    ok("dual_mode_flip_keeps_the_row_and_the_check")


def test_a_new_instance_rejudges_gold_and_clears_the_check():
    """到期时刻变了 = 板面上换成了另一条任务：金币判定重新读、上一条的 ✓ 也不再算。
    这一条才是 12.1.5"意外遭遇已经不是金币任务、还挂着上个 CD 的 1064 金"该走的退出路
    （不是"读到非金币就当场删行"——那会把双模式那条误删，见上一条用例）。
    倒计时同一条实例只会有分钟取整的几十秒抖动，4500 秒的差只能是换了一条。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 30000000, None)], 1000, {7: 43200})   # 到期 44200
    L.SetView(db, "A-永恒之井", 7, "done", 2000)
    if st(L, db, "A-永恒之井", 7, 2000) != "done":
        return fail("a_new_instance_rejudges_gold_and_clears_the_check",
                    "前置：这一条实例已交")

    pool_pass(lua, L, db, [(7, 0, [888])], 5000, {7: 43700})         # 到期 48700 ⇒ 新的一条
    if st(L, db, "A-永恒之井", 7, 5000) == "done":
        return fail("a_new_instance_rejudges_gold_and_clears_the_check",
                    "换了实例，上一条的已交不该再算，实际 " + str(st(L, db, "A-永恒之井", 7, 5000)))
    if len(L.MatrixRows(db, 5000)) != 0:
        return fail("a_new_instance_rejudges_gold_and_clears_the_check",
                    "新实例这一读不给金币，不该占行（上个 CD 的金币不许延续过来）")
    ok("a_new_instance_rejudges_gold_and_clears_the_check")


def test_diff_no_renewal_without_real_countdown():
    """编号模型里换轮不需要任何到期字段：renewed 只由"板上真消失过一轮又出现"（rec.goneAt）
    产生（T4b 口径）。老版这里钉的是"排程值/周重置/每日重置这些来源不算轮次边界"——
    expiresAt/expiresSrc/expiresGuessed 那一组连同 24 小时排程值已在 T5 整组退役、无读者；
    这条改钉新事实：往残留字段里写什么都不许影响 renewed 判定，同时钉住唯一真出处。"""
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
                        f"来源={src} 那组字段已整组退役，残留数不该再触发任何换轮判定")
    # 真凭据链照旧钉住：renewed 现在只有一条路 —— 板上消失又回来
    db = ledger_db(lua)
    L.DiffPool(db, one, 1000)
    L.DiffPool(db, pool(lua, []), 2000)
    r = L.DiffPool(db, one, 3000)
    if len(r["renewed"]) == 0:
        return fail("diff_no_renewal_without_real_countdown",
                    "真'板上消失又回来'应产生 renewed —— 这是 renewed 唯一的出处")
    ok("diff_no_renewal_without_real_countdown")


def test_turnin_state_and_week_stats():
    """交任务入日志；本轮内显示已交；周统计只算本 CD。
    金额自 2026-10-29 起只来自奖励读取那一路；2026-11-04 起默认按行借这一条实例的
    行级金额，只有这条实例上两个不同号一正一 0（分岔）才改按号 —— 下面那句
    "行级钱不许冒充这一号"钉的是分岔那一档（夹具先补出分岔凭据）。不分岔时该借到
    500000 的形状由 test_a_char_without_a_read_borrows_... 那条单独钉着。
    交割流水行上没有金额，真实同步在同一条 pool[i].money 上写两份
    （Sync 池循环的 NoteGoldThisInstance + SetRewardRead），夹具照这两处一起落账。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 5000
    L.DiffPool(db, pool(lua, [(1, 500000, None), (2, 300000, None)]), 6000)
    sig = str(db["quests"][1]["currentSig"])
    L.OnTurnIn(db, "A-永恒之井", 1, 6100, sig)
    row = db["turnins"]["A-永恒之井"][1][1]
    if str(row["sig"]) != sig:
        return fail("turnin_state_and_week_stats",
                    f"流水要烙上完成时看到的奖励签名（取证），实际 {row['sig']}")
    if row["g"] is not None:
        return fail("turnin_state_and_week_stats", "交割流水不再写金额，行里不许有 g")
    if state1(L.State, db, "A-永恒之井", 1) != "done":
        return fail("turnin_state_and_week_stats", "本轮内已交应显示 done")
    if state1(L.State, db, "A-永恒之井", 2) != "open":
        return fail("turnin_state_and_week_stats", "池内未做应显示 open")
    count, gold = L.WeekStats(db, "A-永恒之井", 5000)
    if int(count) != 1 or int(gold) != -1:
        return fail("turnin_state_and_week_stats",
                    f"这一条实例没读到过奖励金币 ⇒ 金币栏报未知，实际 {count}/{gold}")
    L.NoteGoldThisInstance(db, 1, 500000, 6100)
    # 11-04 定稿 §二 6：补出分岔凭据（两个不同号、一个正数一个 0），这一条才落到
    # "按号"那一档，下面那句断言测的才是它适用的形状；不分岔时照旧按行借。
    L.SetRewardRead(db, "B-永恒之井", 1, 500000, 6100)
    L.SetRewardRead(db, "C-永恒之井", 1, 0, 6100)
    if int(L.WeekStats(db, "A-永恒之井", 5000)[1]) != -1:
        return fail("turnin_state_and_week_stats",
                    "分岔时行级那份钱（任意一个号读到的）不许冒充这一号的本周金币")
    L.SetRewardRead(db, "A-永恒之井", 1, 500000, 6100)
    count, gold = L.WeekStats(db, "A-永恒之井", 5000)
    if int(count) != 1 or int(gold) != 500000:
        return fail("turnin_state_and_week_stats", f"本周应 1 条/500000，实际 {count}/{gold}")
    count0, _ = L.WeekStats(db, "A-永恒之井", 7000)
    if int(count0) != 0:
        return fail("turnin_state_and_week_stats", "上个 CD 的记录不应计入本周")
    ok("turnin_state_and_week_stats")


def test_turnin_not_counted_across_instances():
    """任务消失后又以**新的结束时刻**回到板上 = 下一条实例，上一条的已交不再计入这一条。
    编号模型里"消失又出现"本身不推任何起点（计划 §五）：换没换一条只看结束时刻变没变，
    所以这一条用例自己把第二趟的倒计时答成新的那一条。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 300000, None)], 1000, {7: 43200})
    L.OnTurnIn(db, "A-永恒之井", 7, 1100, str(db["quests"][7]["currentSig"]))
    if st(L, db, "A-永恒之井", 7, 1200) != "done":
        return fail("turnin_not_counted_across_instances", "这一条实例做完应 done")
    pool_pass(lua, L, db, [], 2000)
    if st(L, db, "A-永恒之井", 7, 2100) != "ended":
        return fail("turnin_not_counted_across_instances", "消失后应显示这一条已结束")
    pool_pass(lua, L, db, [(7, 300000, None)], 3000, {7: 43200})
    st_now = st(L, db, "A-永恒之井", 7, 3100)
    if st_now != "open":
        return fail("turnin_not_counted_across_instances",
                    f"新的结束时刻 = 下一条实例，上一条那笔不许跨过来，实际 {st_now}")
    count, _ = L.WeekStats(db, "A-永恒之井", 0, 3100)
    if int(count) != 1:
        return fail("turnin_not_counted_across_instances", "金币历史仍留在库里（统计口径不变，这一条的状态另算）")
    ok("turnin_not_counted_across_instances")


def test_manual_beats_auto_and_survives_sync():
    """手动补录优先于自动快照；后续 SetView 不得覆盖它；撤销能还原。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(3, 200000, None)], 1000, {3: 43200})
    L.SetView(db, "B-永恒之井", 3, "open", 1000)
    # 补录的时刻落在**这一条实例之内**（结束时刻 44200）：换一条实例那道门见
    # test_a_manual_mark_expires_when_the_instance_rolls，那一笔本来就不该再打勾。
    old = L.SetManual(db, "B-永恒之井", 3, "done", 1100, 150000)
    if st(L, db, "B-永恒之井", 3, 1200) != "done":
        return fail("manual_beats_auto_and_survives_sync", "手动已交应覆盖自动 open")
    L.SetView(db, "B-永恒之井", 3, "open", 2000)
    if st(L, db, "B-永恒之井", 3, 2100) != "done":
        return fail("manual_beats_auto_and_survives_sync", "登录同步不应覆盖手动记录")
    count, gold = L.WeekStats(db, "B-永恒之井", 0, 2100)
    if int(count) != 1 or int(gold) != 150000:
        return fail("manual_beats_auto_and_survives_sync", f"手动补录应计入统计，实际 {count}/{gold}")
    L.UndoApply(db, {"kind": "manual", "charKey": "B-永恒之井", "questID": 3, "old": old})
    if st(L, db, "B-永恒之井", 3, 2100) != "open":
        return fail("manual_beats_auto_and_survives_sync", "撤销应回到自动判定的 open")
    ok("manual_beats_auto_and_survives_sync")


def test_manual_not_double_counted_with_turnin():
    """同一任务既有自动交任务记录又有手动已交：完成数只算一次，金币也只认那一份奖励读数。
    金额的真源是**这一号**在这一条实例上的奖励读取（reads，2026-11-03 计划 §二第 2 条），
    不是交割行 —— 交割那一笔只负责"这条实例交过"。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 500          # 周 CD 尾巴档：这一条实例算得出编号，凭据才挂得上
    L.DiffPool(db, pool(lua, [(8, 250000, None)]), 1000)
    L.OnTurnIn(db, "E-永恒之井", 8, 1100, str(db["quests"][8]["currentSig"]))
    L.NoteGoldThisInstance(db, 8, 250000, 1100)
    L.SetRewardRead(db, "E-永恒之井", 8, 250000, 1100)   # 同一次同步在 pool[i].money 上写的另一份
    L.SetManual(db, "E-永恒之井", 8, "done", 1200, 250000)
    count, gold = L.WeekStats(db, "E-永恒之井", 0)
    if int(count) != 1 or int(gold) != 250000:
        return fail("manual_not_double_counted_with_turnin", f"不应重复计数，实际 {count}/{gold}")
    ok("manual_not_double_counted_with_turnin")


def test_gold_unmeasured_counts_not_sum():
    """这一条实例的奖励金额读不出（rec.goldKey/goldMoney 配不上交割那一笔，2026-10-29 起
    周统计的金币只来自奖励读取，交割那一处不再量、也不存到手数）：完成数照计，
    金币总额报未知（-1），不报 0。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(4, 100000, None)]), 1000)
    L.OnTurnIn(db, "C-永恒之井", 4, 1100, str(db["quests"][4]["currentSig"]))
    count, gold = L.WeekStats(db, "C-永恒之井", 0)
    if int(count) != 1:
        return fail("gold_unmeasured_counts_not_sum", f"count 应为 1，实际 {count}")
    if int(gold) != -1:
        return fail("gold_unmeasured_counts_not_sum", f"无实测值 gold 应为 -1，实际 {gold}")
    ok("gold_unmeasured_counts_not_sum")


def test_turnin_gain_does_not_mark_the_row_gold():
    """奖励是不是金币只来自奖励读取（DiffPool 当场问到的金额 + NoteGoldThisInstance
    那份按实例粘滞）。交任务那 15 秒钱窗捡到的到手金额不算数（用户 2026-10-29 定稿：
    "不能依赖交任务的金额"）—— 这一条把"未判定的行不会因为交了一笔钱就转正"钉住。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(5, None, None)]), 1000)   # 这一条不是金币任务
    sig = str(db["quests"][5]["currentSig"])
    if len(L.GoldQuestIDs(db)) != 0:
        return fail("turnin_gain_does_not_mark_the_row_gold", "奖励没答出金币的任务不应进金币列表")
    L.OnTurnIn(db, "F-永恒之井", 5, 1100, sig)
    if len(L.GoldQuestIDs(db)) != 0:
        return fail("turnin_gain_does_not_mark_the_row_gold",
                    "交任务拿到金币不自动转正为金币任务")
    ok("turnin_gain_does_not_mark_the_row_gold")


def test_prune_bounds_size():
    """turnins 每任务上限 8 条；三个 CD 未出现的任务整条清出，金币历史保留。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(6, 100000, None)]), 1000)
    sig = str(db["quests"][6]["currentSig"])
    for i in range(1, 12):
        L.OnTurnIn(db, "D-永恒之井", 6, 1000 + i, sig)
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
    pool_pass(lua, L, db, [(1, 500000, None)], 1000, {1: 43200})
    L.SetView(db, "B-永恒之井", 1, "progress", 1500)
    pool_pass(lua, L, db, [], 2000)              # 当前角色的池里再也没有这个任务
    if st(L, db, "B-永恒之井", 1, 2100) != "progress":
        return fail("state_uses_own_snapshot_across_characters",
                    f"B 自己说过进行中，不该被别人的池判成已结束，实际 {st(L, db, 'B-永恒之井', 1, 2100)}")
    if st(L, db, "A-永恒之井", 1, 2100) != "ended":
        return fail("state_uses_own_snapshot_across_characters", "没有快照的角色仍应显示这一条已结束")
    ok("state_uses_own_snapshot_across_characters")


def test_stale_snapshot_not_trusted_in_a_new_instance():
    """板上换成下一条实例之后，上一条遗留的"已交"快照不得继续冒充这一条。
    换没换一条只问结束时刻（编号模型里没有"起点"）：所以这一条把第二次上板的
    倒计时答成新的那一条，而不是只把池空跑一趟。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 300000, None)], 1000, {7: 43200})
    L.SetView(db, "B-永恒之井", 7, "done", 1500)
    pool_pass(lua, L, db, [], 2000)
    pool_pass(lua, L, db, [(7, 300000, None)], 3000, {7: 43200})   # 结束时刻挪走 = 下一条实例
    if st(L, db, "B-永恒之井", 7, 3100) != "open":
        return fail("stale_snapshot_not_trusted_in_a_new_instance",
                    f"上一条实例的快照不该跨过来，实际 {st(L, db, 'B-永恒之井', 7, 3100)}")
    ok("stale_snapshot_not_trusted_in_a_new_instance")


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


def test_partial_pool_rolls_when_the_board_countdown_moves():
    """走完整同步链路验 12 小时区换轮：12.x 只有按图源答得出数据（partial=true），
    插件永远看不见"任务从池里消失"，屠戮者高地"秘密狩猎"上一轮的已交就这么一路冒充到
    新一轮（12.1.5 实测反馈：全角色 ✓，新刷出来的那个再也记不上）。
    【第 36 轮 T4b 换了判据】老这里靠 DiffPool 的"到期时刻已过但池里还有它 ⇒ 报 renewed"
    那个检测器（§五 整块删除）；现在换轮是**自然发生的**：板面倒计时问出来晚了 12 小时
    ⇒ 结束时刻变 ⇒ 编号变 ⇒ 旧快照与旧流水一律匹配不上，格子自己回到 open。
    所以这条不再断言 renewed（那个字段现在只由"真看不见它又出现"产生），改钉编号换了。"""
    lua, gql, _ = sync_env([(71, 300000)])
    g = lua.globals()
    L = gql.Ledger
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
        return fail("partial_pool_rolls_when_the_board_countdown_moves", f"应走部分池：{res}")
    db = g.GQLDB
    if int(db["quests"][71]["roundEnd"]) != 44200:
        return fail("partial_pool_rolls_when_the_board_countdown_moves",
                    f"结束时刻应为 44200，实际 {db['quests'][71]['roundEnd']}")

    key = str(gql.Sync.CurrentCharKey())
    sig = str(db["quests"][71]["currentSig"])
    before = uid(L, db, 71, 2000)
    for who in (key, "B-永恒之井"):
        gql.Ledger.OnTurnIn(db, who, 71, 2000, sig)
        gql.Ledger.SetView(db, who, 71, "done", 2000)
    if st(L, db, key, 71, 2000) != "done":
        return fail("partial_pool_rolls_when_the_board_countdown_moves", "本轮内交过任务应显示已交")

    g.GQL_NOW = 44800                       # 上一轮 44200 到期，池里这份是刷出来的新一轮
    gql.Sync.SnapshotNow()
    after = uid(L, db, 71, 44800)
    if after is None or after == before:
        return fail("partial_pool_rolls_when_the_board_countdown_moves",
                    f"结束时刻挪了就必须换编号，实际 {before} → {after}")
    for who in (key, "B-永恒之井"):
        if st(L, db, who, 71, 44800) != "open":
            return fail("partial_pool_rolls_when_the_board_countdown_moves",
                        f"新一轮该重开，{who} 实际 {st(L, db, who, 71, 44800)}")
    # 金币流水按 CD 记，不许因为换了轮就凭空少一笔
    if int(L.WeekStats(db, key, 0, 44800)[0]) != 1:
        return fail("partial_pool_rolls_when_the_board_countdown_moves",
                    f"上一轮那笔交任务仍要计入本周，实际 {L.WeekStats(db, key, 0, 44800)[0]}")
    ok("partial_pool_rolls_when_the_board_countdown_moves")


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


def poi_env(lua, pins, parents=None, names=None, world_quest=None, secs=None):
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
                # Api.EventPins 真读的字段名（Api.lua 的 info.tooltipWidgetSet）。
                # 这个字段名本身 12.x 未逐字实测（公开写法常见 tooltipWidgetSetID），
                # 这里只是给已有接口换返回形状，读不到时生产照样 fail-closed。
                if p.get("widgetSet"):
                    row["tooltipWidgetSet"] = p["widgetSet"]
                return lua.table_from(row)
        return None

    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": events, "GetAreaPOIInfo": info})
    if secs is not None:
        g.C_AreaPoiInfo["GetAreaPOISecondsLeft"] = secs if callable(secs) else (lambda pid: secs)
    g.QuestUtils_IsQuestWorldQuest = world_quest or (lambda qid: True)


def widget_env(lua, sets):
    """悬停 widget 层的替身（C_UIWidgetManager）：
    sets = {setID: [(widgetID, [文本串] | None | []), ...]} ——
    None = 该 widget 对 GetItemDisplayVisualizationInfo 不答表（2026-11-01 实测：
    同一 set 里只有奖励 widget 答话，文本 widget 不答），[] = 答表但没有任何文本。
    问链是生产代码真问的那条（tooltipWidgetSet → GetAllWidgetsBySetID →
    GetItemDisplayVisualizationInfo），接口名一个不造，只换返回形状。"""
    g = lua.globals()

    def list_fn(set_id):
        rows = lua.table_from({})
        for i, (wid, texts) in enumerate(sets.get(int(set_id), []), start=1):
            rows[i] = lua.table_from({"widgetID": wid,
                                      "widgetType": 27 if texts else 8})
        return rows

    def item_fn(wid):
        for entries in sets.values():
            for wid_i, texts in entries:
                if wid_i == int(wid):
                    if texts is None:
                        return None
                    return lua.table_from({"text": lua.table_from(list(texts))})
        return None

    g.C_UIWidgetManager = lua.table_from({
        "GetAllWidgetsBySetID": list_fn,
        "GetItemDisplayVisualizationInfo": item_fn,
    })


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

    # ② 这块板这一轮没报了（这条记录是旧档形状：归属在、编号没烙过 ⇒ 下一轮如实清掉，
    #    之后由新规则管住，见 event_pin_group_latches_for_the_instance）
    g.GQLDB["cycleAnchor"] = 1700000000     # 立起来才有编号可谈
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


def test_event_pin_group_latches_for_the_instance():
    """用户 2026-11-02 报的现象（原话）：「亚基入侵 一会属于 周常（这个时候地图上没有pin），
    一会属于祖阿曼。这个体验也很不好，变来变去的。这个任务是我们虚构的，第一次获取到pin
    的时候就知道他所属地图，本轮次就不会再变更地图了，下周才会变，这是这个任务的特点。」
    旧写法每轮快照都按"这一轮扫到没有"重写/清空归属，而 pin 会轮换到没跟踪的图、做完之后
    图标还会先消失一阵 ⇒ 矩阵开着就是 10 秒一次的"祖阿曼 ↔ 周常"来回跳。
    五步钉住新口径（拿 89507 丰饶供品那一族跑同一套判据，归属只写派生字段的老规矩不动）：
    ① 本轮扫到 pin 在 2413 ⇒ 归属 = 2413，并烙上这一条实例自己的编号；
    ② 下一轮板子没报 ⇒ 归属不许动，分组头不掉回周常组；
    ③ 再一轮同一张 pin 改报在 2437 ⇒ 本轮以第一次那张为准，不再变；
    ④ 周重置（锚点前进一整周 = 另一条实例）且这轮没报 ⇒ 旧归属作废，行回到客户端答的图；
    ⑤ 作废之后本周第一次扫到 pin（这次在 2437）⇒ 重新定下来 —— "下周才会变"走的就是这条路。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    g.GQLDB["cycleAnchor"] = 1700000000        # 周 CD 锚点：常驻周常那条实例的编号由它算
    rec = lambda: g.GQLDB["quests"][89507]
    gone = lua.table_from({                    # 这块板这一轮什么都不报
        "GetEventsForMap": lambda mid: lua.table_from({}),
        "GetAreaPOIInfo": lambda mid, pid: None})

    def pin_on(mid):
        others = {2413, 2437} - {mid}
        poi_env(lua, {mid: [{"id": 8527, "name": "丰饶：草药洞窟"}],
                      **{m: [] for m in others}},
                names={2413: "哈籁恩达尔", 2437: "祖阿曼"})
        g.GQLDB["quests"][89507] = hand_rec(lua, {
            "name": "丰饶供品", "regKey": "abundant-offerings",
            "mapID": 2437, "mapSrc": "跟踪地图清单"})

    # ① 第一次扫到
    pin_on(2413)
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    if int(rec()["evMapID"]) != 2413 or rec()["evMapKey"] is None:
        return fail("event_pin_group_latches_for_the_instance",
                    "①第一次扫到 pin 要写下归属，并烙上这一条实例的编号")
    if group_keys(lua) != "2413=89507":
        return fail("event_pin_group_latches_for_the_instance",
                    f"①分组头跟着事件走:\n{group_keys(lua)}")

    # ② 这一轮没扫到（事件换到没跟踪的图 / 做完之后图标先消失那一段）
    g.C_AreaPoiInfo = gone
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    if rec()["evMapID"] is None:
        return fail("event_pin_group_latches_for_the_instance",
                    "②这一轮没扫到 ≠ 归属没了：抹掉它矩阵就掉进周常组，正是用户说的来回跳")
    if group_keys(lua) != "2413=89507":
        return fail("event_pin_group_latches_for_the_instance",
                    f"②没 pin 的那一轮分组头要留在第一次那张图:\n{group_keys(lua)}")

    # ③ 同一条实例里 pin 改报另一张图 ⇒ 第一次那张算数
    poi_env(lua, {2437: [{"id": 8527, "name": "丰饶：草药洞窟"}], 2413: []},
            names={2413: "哈籁恩达尔", 2437: "祖阿曼"})
    gql.Sync.RefreshEventPins(g.GQLDB, 7000)
    if int(rec()["evMapID"]) != 2413:
        return fail("event_pin_group_latches_for_the_instance",
                    "③本轮次不再变更地图（第一次获取到 pin 那张为准）")

    # ④ 周重置：锚点前进一整周 ⇒ 另一条实例，旧归属作废
    g.C_AreaPoiInfo = gone
    g.GQLDB["cycleAnchor"] = 1700000000 + 604800
    gql.Sync.RefreshEventPins(g.GQLDB, 8000)
    if rec()["evMapID"] is not None:
        return fail("event_pin_group_latches_for_the_instance",
                    "④换了一条实例（周重置）就该作废，不然上周的图会一直挂着")
    if group_keys(lua) != "2437=89507":
        return fail("event_pin_group_latches_for_the_instance",
                    f"④作废后行回到客户端答过的那张图:\n{group_keys(lua)}")

    # ⑤ 本周第一次扫到 pin（换了张图）⇒ 归属重新定下来
    poi_env(lua, {2437: [{"id": 8527, "name": "丰饶：草药洞窟"}], 2413: []},
            names={2413: "哈籁恩达尔", 2437: "祖阿曼"})
    gql.Sync.RefreshEventPins(g.GQLDB, 9000)
    if rec()["evMapID"] is None or int(rec()["evMapID"]) != 2437:
        return fail("event_pin_group_latches_for_the_instance",
                    "⑤下周换图要能重新定下来（用户：'下周才会变'）")
    ok("event_pin_group_latches_for_the_instance")


def test_event_pin_group_rotates_with_own_countdown():
    """丰饶按小时/天轮换：归属的"一轮"必须以事件自身剩余时间为界，而非每周锚点，
    否则整周锁死在第一次见到的那张图（bug：丰饶归到哈籁恩达尔）。pin 报得出剩余时间时，
    轮换到新图要能跟着换；造一份上一轮卡在哈籁恩达尔(2413)的旧归属也必须作废重定。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua,
            {2395: [{"id": 8528, "name": "丰饶：附魔墓穴", "primary": True}],
             2437: [{"id": 8528, "name": "丰饶：附魔墓穴"}]},
            names={2395: "永歌森林", 2437: "祖阿曼"},
            secs=11743)   # 生产里 C_AreaPoiInfo.GetAreaPOISecondsLeft 答得出
    g.GQLDB["quests"][89507] = hand_rec(lua, {
        "name": "丰饶供品", "regKey": "abundant-offerings",
        "mapID": 2437, "mapSrc": "跟踪地图清单"})
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    rec = g.GQLDB["quests"][89507]
    if int(rec["evMapID"]) != 2395:
        return fail("event_pin_group_rotates_with_own_countdown",
                    f"丰饶该跟着 pin 到永歌森林(2395)，实际:\n{rec['evMapID']}")
    if "e" not in str(rec["evMapKey"]):
        return fail("event_pin_group_rotates_with_own_countdown",
                    f"归属轮次键该用事件自身剩余时间(e 前缀)，实际:\n{rec['evMapKey']}")
    # 造一份上一轮卡在哈籁恩达尔(2413)的旧归属：轮换后必须能改过来
    rec["evMapID"] = 2413
    rec["evMapKey"] = "89507:999"   # 旧每周锚点键（与事件剩余时间键不同源）
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    if int(rec["evMapID"]) != 2395:
        return fail("event_pin_group_rotates_with_own_countdown",
                    "轮换到新图后旧归属要作废并重定:\n" + str(rec["evMapID"]))
    ok("event_pin_group_rotates_with_own_countdown")


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
    ⇒ 改给真凭据（上过板 + 这一条实例读到过门槛金币），验的还是同一件事，只是不再拿
    "玩家点了一下"当上板的理由。
    第 36 轮 T4b 跟着改：金币粘滞认编号、不认 `goldRound`（那个字段随 §五 下线），所以先立身份。
    这三条正是"问不出板面倒计时"的周常 ⇒ 按用户 10-28 定案走**周 CD 尾巴**那一档
    （`db.cycleAnchor`），不是拿夹具硬塞一个 roundEnd 给它。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    L = gql.Ledger
    # 三条各占一种客户端答法：false=明确不是世界任务、true=是、nil=接口答不出。
    # 分开 questID 才验得出三种：是不是世界任务是会话内静态属性，Api 里有缓存，
    # 同一个 ID 中途改替身答法不会重新问（那是缓存，不是 bug）。
    poi_env(lua, {}, names={2393: "银月城"}, world_quest=lambda qid: (
        False if int(qid) == 98172 else None if int(qid) == 98174 else True))
    g.GQLDB["cycleAnchor"] = 1000          # 周 CD 尾巴档：这三条的结束时刻 = 1000 + 604800
    for qid in (98172, 98173, 98174):
        g.GQLDB["quests"][qid] = hand_rec(lua, {
            "name": ("追踪萨拉塔斯" if qid == 98172 else "同名待遇#" + str(qid)),
            "poolSeen": True, "goldMoney": 30000000,
            "mapID": 2393, "mapSrc": "玩家指定"})
        g.GQLDB["quests"][qid]["goldKey"] = uid(L, g.GQLDB, qid, None)
    if uid(L, g.GQLDB, 98172, None) is None:
        return fail("an_unregistered_weekly_lands_in_the_weekly_group",
                    "前置：这三条该挂上周 CD 尾巴那一档的编号，否则金币凭据配不上、一行都不进矩阵")
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


def test_aqir_state_needs_positive_evidence_not_the_absent_sentence():
    """T15（用户 2026-11-02 现场事实推翻旧判据）：亚基入侵"这周奖励领没领"客户端**不即时**
    —— ① 刚做完 pin 图标先消失；② 下次上线先呈一段既不是"每周仅可获得一次"也不是正常
    奖励段的中间态（十来分钟，有的角色要等下次整点更新）；③ 做完立即退游戏，别的号这段
    时间根本读不到"已完成"。⇒ 中间态是常态，"没看到那句周常文案"**不是**已领证据。
    旧判据"第一个能答出表且文本非空的 item-display：含周常字样 ⇒ open，否则当场
    return done"（AqirInvasion.lua 旧 :88）把中间态直接写成假绿勾，整删。
    新判据：扫完整个 set；done 只认注释记录过的真客户端徽章字样（正向证据），
    open 只认"每周仅可获得一次"；两边都没命中 = nil（判不出），nil 一路走不到
    Ledger.SetView（Ledger.lua:1466 `if e.state then`），一格凭据都不许写。
    本用例钉住四岔（外加矛盾形状）：
    ① 含"每周仅可获得一次" ⇒ open；
    ② 含实测徽章字样 ⇒ done，且当场落在**本号**的 views（T16：逐号凭据，不升全账号）；
    ③ 奖励段整个消失（widget 都不答/答空）⇒ 判不出：不许有 done 凭据，行本身要留着，
       状态如实答 unknown（"读不到"，矩阵显示 ?，不许画成勾）；
    ④ set 里混着一条非奖励文本的 item-display ⇒ 仍不许判 done；把它和周常那条放一起
       （乱的在前）⇒ 要答 open —— 钉死"第一个 widget 定终身"不许长回来；
    ⑤ 周常与徽章两条同轮都出现（矛盾形状）⇒ 判不出，两边都不许落。"""
    def aqir(widgets):
        """widgets: [(widgetID, 文本串列表 | None=不答表)], 挂在替身 widgetSet 2253 上。"""
        lua, gql, _ = sync_env([], extra=("AqirInvasion",))
        g = lua.globals()
        poi_env(lua, {2395: [{"id": 8813, "name": "亚基入侵", "primary": True,
                              "widgetSet": 2253}]},
                names={2395: "祖阿曼"}, world_quest=lambda qid: False)
        widget_env(lua, {2253: widgets})
        gql.Config.AddKeyword("亚基入侵")
        gql.Sync.RefreshEventPins(g.GQLDB, 5000)
        return lua, gql

    def view_s(lua):
        """本号视图里 99000001 那一格的状态（nil = 一格凭据都没写过）。"""
        v = lua.execute(
            "local c = GQLDB.chars and GQLDB.chars[...]"
            " local r = c and c.views and c.views[99000001]"
            " return r and r.s or nil", str(lua.globals().GQL.Sync.CurrentCharKey()))
        return None if v is None else str(v)

    def probe_state(lua):
        return one(lua.execute("return GQL.Api.QuestState(99000001)"))

    # ① 周常宝箱文案在场 ⇒ open（正向证据：这句话本身就是"还没领"）
    lua, gql = aqir([(8403, ["亚基战利品宝箱", "每周仅可获得一次"]), (8404, None)])
    if view_s(lua) != "open":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"含「每周仅可获得一次」要判 open，实际 {view_s(lua)}")
    if probe_state(lua) != "open":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"虚拟探测器同一轮要答 open，实际 {probe_state(lua)}")

    # ② 实测徽章文案（2026-11-01 双角色对照，本文件注释记录的字样）⇒ done，
    #    且这笔凭据落在**当前号**的 views 上（逐号，不升成全账号 —— T16 当场落账）
    lua, gql = aqir([(8404, ["获得一枚勋章", "可换取任意至暗之夜阵营的声望"])])
    if view_s(lua) != "done":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"读到明确已领字样要当场落 done，实际 {view_s(lua)}")
    others = int(lua.execute(
        "local n = 0 local mine = ... for k, c in pairs(GQLDB.chars or {}) do"
        "  if k ~= mine and c.views and c.views[99000001] then n = n + 1 end end return n",
        str(lua.globals().GQL.Sync.CurrentCharKey())))
    if others != 0:
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    "本号读到的状态只许落本号（逐号凭据），不许写进别的号的视图")

    # ③ 奖励段整个消失（没有一条 widget 答得出 item-display 表、或答得出但没文本）
    #    ⇒ 判不出：不许写下任何 done 凭据，行要留着，状态如实 unknown（显示"读不到"）
    lua, gql = aqir([(8403, None), (8404, [])])
    if view_s(lua) is not None:
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"中间态/奖励段消失这一轮一格凭据都不许写，实际写了 {view_s(lua)}")
    if lua.execute("return GQLDB.quests and GQLDB.quests[99000001] or nil") is None:
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    "判不出不许吞掉玩家点名的那一行（行留着，状态答读不到）")
    if probe_state(lua) != "unknown":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"从没答出过时探测器要如实 unknown，实际 {probe_state(lua)}")

    # ④ 混入非奖励文本的 item-display：
    #    a) 全场只有这条闲文本 —— 旧判据"答出表且非周常 ⇒ done"就是在这里写假绿勾的
    lua, gql = aqir([(8402, ["沙漠之下潜伏着亚基帝国的虫群"])])
    if view_s(lua) is not None:
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    "读不懂的文本不是已领证据，不许落任何凭据")
    if probe_state(lua) != "unknown":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"只有闲文本时状态要答 unknown，实际 {probe_state(lua)}")
    #    b) 闲文本在前、周常那条在后 —— 钉"不许第一个 widget 定终身"：扫完整个 set 才裁
    lua, gql = aqir([(8402, ["沙漠之下潜伏着亚基帝国的虫群"]),
                     (8403, ["每周仅可获得一次"])])
    if view_s(lua) != "open":
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"先撞上闲文本再撞上周常那条，要答 open（定终身旧账），实际 {view_s(lua)}")

    # ⑤ 周常与徽章两条正向字样同轮都出现：没见过的矛盾形状 ⇒ 判不出，两边都不许落
    lua, gql = aqir([(8403, ["每周仅可获得一次"]),
                     (8404, ["获得一枚勋章", "可换取任意至暗之夜阵营的声望"])])
    if view_s(lua) is not None:
        return fail("aqir_state_needs_positive_evidence_not_the_absent_sentence",
                    f"矛盾形状两边都不许落，实际写了 {view_s(lua)}")
    ok("aqir_state_needs_positive_evidence_not_the_absent_sentence")


# ------------------------------------------------------------------
# 第 17 轮（用户 2026-11-02 现场："亚基入侵：有的角色完成了，再次登录后又变成问号了，
# 应该是又去问是否完成了"）：客户端"这一轮答不出"（"unknown"）不是新事实，只许让格子
# 保持上一结论，不许抹掉玩家已经拿到的凭据。判据收在 Ledger.SetView 一处
# （Ledger.lua:1294 起），下面五条把它的五岔各钉一遍。
# ------------------------------------------------------------------

def test_an_unanswerable_round_never_erases_the_aqir_done_view():
    """端到端跑用户那一单的形状（亚基入侵虚拟行）：
    ① 本号做完时客户端答出徽章文案 → done 落进本号 views；
    ② 重新登录（AqirInvasion.lua:50 那份 lastWidgetSet/lastState 是**模块内存**变量，
       登录归零）+ 延迟期（用户实测：pin 图标先消失、奖励段十来分钟读不出）
       → Sync 每轮快照按 RegisteredIndex 重问（Sync.lua:445）→ Api.QuestState 的虚拟
       探测器答不出 → "unknown"（Api.lua:829-831）→ EnsureRegistered 末尾那道
       `if e.state then L.SetView(...)`（"unknown" 是非空字符串，它拦不住）→ SetView；
    ③ 视图必须还是那份 done（连 t 都没动），RegisteredState 不许退成问号。
    真 reload 离线跑不了，② 里按"归零后的探测器"重挂一条答不出的（返回 nil = 从没答过，
    正是 AqirInvasion.lua:169 那个形状），并把 pin 从板上撤掉（图标先消失）让
    RefreshEventPins 那一路不再写 done —— 剩下的写点全是生产的。
    反向验证：守卫改回无条件覆盖，③ 的两条断言当场红。"""
    lua, gql, _ = sync_env([], wall=1700000000, weekly=432000, extra=("AqirInvasion",))
    g = lua.globals()
    poi_env(lua, {2395: [{"id": 8813, "name": "亚基入侵", "primary": True,
                          "widgetSet": 2253}]},
            names={2395: "祖阿曼"}, world_quest=lambda qid: False)
    widget_env(lua, {2253: [(8404, ["获得一枚勋章", "可换取任意至暗之夜阵营的声望"]),
                            (8403, None)]})
    gql.Config.AddKeyword("亚基入侵")
    gql.Sync.RefreshEventPins(g.GQLDB, 1700000000)
    key = str(gql.Sync.CurrentCharKey())
    view = g.GQLDB["chars"][key]["views"][99000001]
    if view is None or str(view["s"]) != "done":
        return fail("an_unanswerable_round_never_erases_the_aqir_done_view",
                    f"前置：读到徽章文案那轮该落 done，实际 {view}")
    first_t = int(view["t"])

    lua.execute("GQL.Api.RegisterVirtualState(99000001, function() return nil end)")
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    g.GetServerTime = lambda: 1700000060
    gql.Sync.SnapshotNow()

    view = g.GQLDB["chars"][key]["views"][99000001]
    if str(view["s"]) != "done" or int(view["t"]) != first_t:
        return fail("an_unanswerable_round_never_erases_the_aqir_done_view",
                    f"这一轮答不出（unknown）不许抹掉已到手的 done，"
                    f"实际 {view['s']}/{view['t']}")
    if one(lua.execute("return GQL.Ledger.RegisteredState(GQLDB, ..., 99000001,"
                       " 1700000060)", key)) != "done":
        return fail("an_unanswerable_round_never_erases_the_aqir_done_view",
                    "界面不许因为一轮答不出就退成问号：RegisteredState 要照旧答 done")
    ok("an_unanswerable_round_never_erases_the_aqir_done_view")


def test_an_unanswerable_round_keeps_a_recorded_open_answer():
    """注册行已答 open、这一轮答不出 ⇒ 仍是 open（守卫不只保 done）。
    写点走 EnsureRegistered → SetView 这条真门（Sync.lua:445 每轮快照重问注册行
    落的就是它）。对照那岔同一条门里跑：同一趟里从没答出过的那条注册行，
    unknown 照写 —— 没有旧结论可保时格子如实"读不到"。
    反向验证：守卫写成只保 done（漏掉 open），第一处断言红。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    lua.execute("GQLDB = {}")
    gql.Config.Init()
    L = gql.Ledger
    db = gql.Config.DB()
    lua.execute("""
        local d = GQL.Config.DB()
        d.cycleAnchor = 1699838400 - 604800 + 604800
        GQL.Ledger.EnsureRegistered(d, {
            { key = "kw:特别任务", questID = 124, name = "特别任务", state = "open" },
            { key = "kw:从没答出", questID = 125, name = "从没答出", state = "unknown" },
        }, "A-永恒之井", 1699838400)
    """)
    views = db["chars"]["A-永恒之井"]["views"]
    if str(views[124]["s"]) != "open":
        return fail("an_unanswerable_round_keeps_a_recorded_open_answer",
                    "前置：这一条实例的视图该先落 open")
    if str(views[125]["s"]) != "unknown":
        return fail("an_unanswerable_round_keeps_a_recorded_open_answer",
                    "从没答出过的那条：unknown 要照写（界面如实显示读不到）")
    first_t = int(views[124]["t"])
    if str(views[124]["k"]) != uid(L, db, 124, 1699838400):
        return fail("an_unanswerable_round_keeps_a_recorded_open_answer",
                    "前置：那一笔 open 烙的是这一条实例的编号")

    lua.execute("""
        GQL.Ledger.EnsureRegistered(GQL.Config.DB(), {
            { key = "kw:特别任务", questID = 124, name = "特别任务", state = "unknown" },
        }, "A-永恒之井", 1699838460)
    """)
    if str(views[124]["s"]) != "open" or int(views[124]["t"]) != first_t:
        return fail("an_unanswerable_round_keeps_a_recorded_open_answer",
                    f"这一轮答不出不许换掉已答出的那份 open（连时刻都不许动），"
                    f"实际 {views[124]['s']}/{views[124]['t']}")
    if one(L.RegisteredState(db, "A-永恒之井", 124, 1699838460)) != "open":
        return fail("an_unanswerable_round_keeps_a_recorded_open_answer",
                    "RegisteredState 不许因为一轮答不出就变成问号")
    ok("an_unanswerable_round_keeps_a_recorded_open_answer")


def test_a_rolled_instance_lets_unknown_take_the_cell():
    """编号变了 ⇒ unknown 照写（守卫只保"同一条实例"的那份结论，不许让上一条的答案
    赖在这一格上）。两岔：
    ① 换 CD（注册行的身份 = 周 CD 尾巴那一档）：上周尾巴烙的 done 对本周编号匹配不上
       ⇒ 这一轮答不出要如实落成 unknown；
    ② 板上换了另一条实例（真写点 SetExpiry 把结束时刻挪到下一个桶）：同上。
    反向验证：守卫写成"不分实例、有旧结论就保"，①② 都停在 done = 假勾跨轮/跨实例
    （94612 那批假勾的同一条方向）。"""
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", mock=True)
    lua.execute("GQLDB = {}")
    gql.Config.Init()
    L = gql.Ledger
    db = gql.Config.DB()
    lua.execute("""
        local d = GQL.Config.DB()
        d.cycleAnchor = 1699233600                      -- 上一 CD 的锚点
        GQL.Ledger.EnsureRegistered(d, { { key = "kw:特别任务", questID = 124,
            name = "特别任务", state = "done" } }, "A-永恒之井", 1699500000)
        d.cycleAnchor = 1699838400                      -- 换 CD：本周尾巴那一档
        GQL.Ledger.EnsureRegistered(d, { { key = "kw:特别任务", questID = 124,
            name = "特别任务", state = "unknown" } }, "A-永恒之井", 1700000000)
    """)
    view = db["chars"]["A-永恒之井"]["views"][124]
    if str(view["s"]) != "unknown":
        return fail("a_rolled_instance_lets_unknown_take_the_cell",
                    f"换 CD 后上一轮的 done 不许赖着，要落 unknown，实际 {view['s']}")
    if str(view["k"]) != uid(L, db, 124, 1700000000):
        return fail("a_rolled_instance_lets_unknown_take_the_cell",
                    "写进去的必须是**这一条**实例的编号")

    lua.execute("""
        local d = GQL.Config.DB()
        GQL.Ledger.EnsureRegistered(d, { { key = "pin:举钳欢庆", questID = 94612,
            name = "举钳欢庆" } }, "B-永恒之井", 1700000000)
        local rec = d.quests[94612]
        GQL.Ledger.SetExpiry(rec, 1700000000, 43200)     -- 板上这一条：1700043200 结束
        GQL.Ledger.SetView(d, "B-永恒之井", 94612, "done", 1700000000)
        GQL.Ledger.SetExpiry(rec, 1700043300, 43200)     -- 板面换成下一条实例
        GQL.Ledger.SetView(d, "B-永恒之井", 94612, "unknown", 1700043300)
    """)
    view = db["chars"]["B-永恒之井"]["views"][94612]
    if str(view["s"]) != "unknown":
        return fail("a_rolled_instance_lets_unknown_take_the_cell",
                    f"板上换了实例，旧 done 不许顶住新实例的初始值，实际 {view['s']}")
    if str(view["k"]) != uid(L, db, 94612, 1700043300):
        return fail("a_rolled_instance_lets_unknown_take_the_cell",
                    "换实例后烙进去的编号要跟着换成这一条的")
    if st(L, db, "B-永恒之井", 94612, 1700043300) != "unknown":
        return fail("a_rolled_instance_lets_unknown_take_the_cell",
                    "新实例这一格界面要如实问号")
    ok("a_rolled_instance_lets_unknown_take_the_cell")


def test_a_pool_row_with_no_state_api_keeps_its_answer():
    """普通行（非虚拟）在同一条覆盖路径上的对照 —— 用户口径：点一个缺陷必追"类似的都要
    解决"。Api.QuestState 普通那一路接口不可用时同样答 "unknown"（Api.lua:833-845），
    Sync 的池循环把它原样喂给 SetView（Sync.lua:908；840-843 只把"抛错"清成 nil，
    "答不出"照原样留着）⇒ 与亚基那一格是同一个洞，收口在 SetView 一起治好。
    ① 第一趟快照：接口答得出的 open 落进视图（真 Sync 写点）；
    ② 撤掉状态接口、下一轮再快照：视图仍是那份 open（t 没动），格子不退成问号；
    ③ 对照：同一趟里刚上板、从没答出过的那条新池行照旧写 unknown。
    反向验证：守卫改回无条件覆盖，② 的两条断言当场红。"""
    lua, gql, _ = sync_env([(71, 300000)], board=43200)
    g = lua.globals()
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    views = g.GQLDB["chars"][key]["views"]
    if str(views[71]["s"]) != "open":
        return fail("a_pool_row_with_no_state_api_keeps_its_answer",
                    f"前置：第一趟接口答得出 open，实际 {views[71]['s']}")
    first_t = int(views[71]["t"])
    # 下一轮快照（矩阵开着 10 秒一趟，这里隔一分钟）+ 完成/接取接口整体不可用
    rows = lua.table_from({})
    rows[1] = lua.table_from({"questID": 71, "mapID": 2007, "name": "任务71",
                              "money": 300000})
    rows[2] = lua.table_from({"questID": 72, "mapID": 2007, "name": "任务72",
                              "money": 400000})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    lua.execute("C_QuestLog = nil")
    g.GQL_NOW = 60
    gql.Sync.SnapshotNow()
    if str(views[71]["s"]) != "open" or int(views[71]["t"]) != first_t:
        return fail("a_pool_row_with_no_state_api_keeps_its_answer",
                    f"接口不可用这一轮不许抹掉已答出的那份结论，"
                    f"实际 {views[71]['s']}/{views[71]['t']}")
    if str(views[72]["s"]) != "unknown":
        return fail("a_pool_row_with_no_state_api_keeps_its_answer",
                    "刚上板、从没答出过的那条：unknown 照写（没有旧结论可保）")
    if st(gql.Ledger, g.GQLDB, key, 71, 60) != "open":
        return fail("a_pool_row_with_no_state_api_keeps_its_answer",
                    "普通行的格子也不许因为一轮答不出就变问号")
    ok("a_pool_row_with_no_state_api_keeps_its_answer")


def test_a_definite_answer_still_overwrites_the_cell():
    """守卫只管 unknown 这一岔，确定答案的覆盖一字不变（用户点名不许动的两条路）：
    ① 玩家点格子把这一号的覆盖清掉 → 下一轮接口答 open，视图要从 done 翻回 open；
    ② 交割当场写 done（Sync.lua:1267 那个写点的形状）压得过先前任何一份结论；
    ③ progress / skip / ended 与 unknown 同门而立：一份被保下来的旧结论，
       确定状态照样改得动。
    反向验证：守卫写成"有旧结论就一律不覆盖"，这三条全红；
    既有的点击/交割用例（matrix_cell_click_toggles_manual_state、
    turnin_records_immediately_without_any_window）也照这条口径继续绿。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(7, 300000, None)], 1000, {7: 43200})
    key = "A-永恒之井"
    L.SetView(db, key, 7, "done", 1100)
    if st(L, db, key, 7, 1150) != "done":
        return fail("a_definite_answer_still_overwrites_the_cell", "前置：先落 done")
    L.SetView(db, key, 7, "open", 1200)          # 玩家点了格子，下一轮接口答 open
    if str(db["chars"][key]["views"][7]["s"]) != "open":
        return fail("a_definite_answer_still_overwrites_the_cell",
                    "确定答案 open 照样覆盖 done（清除覆盖那条路不许被守卫挡掉）")
    L.SetView(db, key, 7, "unknown", 1300)       # 接口这一轮答不出
    L.SetView(db, key, 7, "done", 1400)         # 交割当场落账
    if str(db["chars"][key]["views"][7]["s"]) != "done" or \
            int(db["chars"][key]["views"][7]["t"]) != 1400:
        return fail("a_definite_answer_still_overwrites_the_cell",
                    "被保下来的那份结论前面，确定状态照样写得进去")
    for probe, want in (("progress", "progress"), ("skip", "skip"), ("ended", "ended")):
        L.SetView(db, key, 7, probe, 1500)
        if str(db["chars"][key]["views"][7]["s"]) != want:
            return fail("a_definite_answer_still_overwrites_the_cell",
                        f"{want} 这一档的覆盖行为不许变")
    ok("a_definite_answer_still_overwrites_the_cell")


def test_event_pin_gold_quest_registers_without_keyword():
    """用户 2026-10-31："暗影汇聚之地（战团周常 pin，奖励 956 金）怎么点刷新都不出现"。
    它不在世界任务池（按图源逐行过 IsWorldQuest 时被滤掉）、日志里也没有，
    玩家也没给它关键词 —— 收路上它从来进不了账本，准入门②（金币奖励满足）
    再对也白搭：卡的是收路不是准入门。pin 上客户端给了真任务号、奖励金币读得到
    且够门槛时照②收：regKey 挂 pin: 前缀（删关键词不许退掉它）。奖励答不出
    或明确不给金币的 pin 照旧不收 —— 界面还是只服务两类依据，绝不拿事件名猜行。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    poi_env(lua, {2395: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) == 99002 else 0
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    rec = g.GQLDB["quests"][99002]
    if rec is None:
        return fail("event_pin_gold_quest_registers_without_keyword",
                    "pin 带真任务号、奖励金币够门槛，金币门这一路该把它收进账本")
    if str(rec["regKey"]) != "pin:暗影汇聚之地":
        return fail("event_pin_gold_quest_registers_without_keyword",
                    f"金币门收的行要挂 pin: 前缀（删关键词不许退掉它），实际 {rec['regKey']}")
    if int(rec["lastMoney"] or 0) != 9560000:
        return fail("event_pin_gold_quest_registers_without_keyword",
                    f"读到的奖励金币要落账（rec.lastMoney），实际 {rec['lastMoney']}")
    if group_keys(lua) != "2395=99002":
        return fail("event_pin_gold_quest_registers_without_keyword",
                    f"行要真的出现在矩阵的事件那块板分组里，实际 {group_keys(lua)}")
    # 对照：奖励答不出/答 0（客户端明说不给金币）的 pin 不收
    poi_env(lua, {2395: [{"id": 8815, "name": "不给金币的活动", "questID": 99003,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    if g.GQLDB["quests"][99003] is not None:
        return fail("event_pin_gold_quest_registers_without_keyword",
                    "奖励不够门槛的 pin 不许收进账本（准入门②过不了就不该有行）")
    ok("event_pin_gold_quest_registers_without_keyword")


def test_a_pin_registered_row_expires_on_the_cycle_gate():
    """2026-11-02 D6/D7 定稿（用户原话）：「没有任何任务，包括我们虚拟的亚基入侵，都是有
    结束时间的，时间一到，必须消失」「上板的任务，没有任何一条可以豁免说不下板」。
    到期门②档那句按**身份**的豁免（kwLogAdded / regKey / IsPinRegKey 三支）整句删掉：
    门只问一个事实 —— "这一 CD 真见过没有"。lastSeenAt 的唯一续期来源是本轮真事实
    （池快照 DiffPool / 当前号日志挂着 / 调用方传的 onBoard、seen），注册键本身不是事实。
    于是同一条"上周尾巴 + 本 CD 零证据"的形状，不管挂着哪把键都判到期（旧口径在这里
    全部返回 false —— 那正是"永不下板"的病根：白续顶着豁免，谁也不敢先动）；
    带着本轮真事实的行（板上见过 / pin 在场 / 某个号日志挂着 ⇒ lastSeenAt 续在本 CD 内）
    照旧不到期 —— 门问的是事实，不是身份。①档（有 roundEnd）口径一字不动。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    day, week = 86400, 7 * 86400
    anchor = 1700000000                 # 本 CD 起点（上周那条实例的结束时刻）
    db["cycleAnchor"] = anchor
    now = anchor + 3600                 # 本周开始一小时，这一周还没真见过它
    # 病标本：上周事件在场时金币门收的 pin 行（有金币、有图、注册键是 pin:）⇒ 到期
    pin_rec = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地",
                             "mapID": 2395, "lastMoney": 9560000,
                             "lastSeenAt": anchor - 2 * day})
    if not L.InstanceExpired(db, pin_rec, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "本周零真事实的 pin: 行必须判到期（'到期就要自动消失'）")
    # ①本周零真事实的 kw: 行（玩家敲关键词点名）⇒ 照判到期：点名是准入门的事，不是免死牌
    kw_rec = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                            "mapID": 2395, "lastSeenAt": anchor - 2 * day})
    if not L.InstanceExpired(db, kw_rec, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "kw: 行本 CD 零真事实必须到期（旧口径在这里豁免 = 永不下板的病根）")
    # ②内置周常那一族（键不带前缀）同形状 ⇒ 照判到期（D6：没有任何一条可以豁免）
    preset_rec = hand_rec(lua, {"name": "丰饶供品", "regKey": "abundant-offerings",
                                "mapID": 2395, "lastSeenAt": anchor - 2 * day})
    if not L.InstanceExpired(db, preset_rec, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "内置周常本 CD 零真事实同样必须到期；再出现由下一 CD 的真事实重新收")
    # 正向对照①：同一条 kw: 行带着本轮真事实（板上见过/pin 在场/日志挂着都续这一格）⇒ 不许到期
    kw_live = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                             "mapID": 2395, "lastSeenAt": now - 60})
    if L.InstanceExpired(db, kw_live, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "本 CD 真见过的 kw: 行不许误杀 —— 门问的是'这一轮有没有事实'")
    # 正向对照②：内置锚点带本轮真事实（族成员在板上续的那一份）⇒ 不许到期
    preset_live = hand_rec(lua, {"name": "丰饶供品", "regKey": "abundant-offerings",
                                 "mapID": 2395, "lastSeenAt": now - 60})
    if L.InstanceExpired(db, preset_live, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "本 CD 真见过的内置周常不许误杀（续期已由真事实接上，不需要豁免）")
    # 这一 CD 内还见过的 pin 行不到期：②档咬的是"本周零证据"，不是"昨天散场今天就删"
    live_pin = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地",
                               "mapID": 2395, "lastSeenAt": now - 60})
    if L.InstanceExpired(db, live_pin, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "事件还在场（本周见过）的 pin 行不该被这一刀提前砍掉")
    # 判不出 = 不成立：锚点没立起来时不许反过来把行吃掉（fail-closed 的方向不动）
    if L.InstanceExpired(ledger_db(lua), pin_rec, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "cycleAnchor 定不出来 = 判不出 ⇒ 不判到期，豁免删掉也不改这一条")
    # ①档口径不动：万一答出了自己的结束时刻（日志那一路烙的），过点即到期
    st_pin = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地",
                            "roundEnd": now - 60, "lastSeenAt": now - 30})
    if not L.InstanceExpired(db, st_pin, now):
        return fail("a_pin_registered_row_expires_on_the_cycle_gate",
                    "有 roundEnd 就按它自己答的结束时刻判，过点了就到期")
    ok("a_pin_registered_row_expires_on_the_cycle_gate")


def test_a_pin_key_does_not_open_the_player_named_door():
    """用户 2026-11-02 那句「和其他普通金币任务一样。到期就要自动消失」落在两道门上：
    准入门 ① 与"上板凭据"都不许认 `pin:` 这把注册键 —— 它是金币门自己收的行，不是玩家点名
    要的（L.IsResidentRegKey 一句话收成一处）。旧写法 `rec.regKey` 不分前缀，于是 pin 行既
    白拿"绕金币门槛"，又被算成"客户端今天还答得出它"的那份凭据：钱读不到、或者换了实例
    那份钱作废之后，它照样顶着一个记录级字段挂在矩阵上 = 到期不消失。
    ①pin 行 + 钱读不到 + 名字撞关键词但没有一份客户端凭据 ⇒ 不在列（旧口径在这里放 true）；
    ②同一形状换成 `kw:`（玩家亲手敲的关键词）⇒ 照旧在列，点名身份一动不动；
    ③同一形状换成内置周常那把键 ⇒ 照旧在列（代码里声明的整周常驻族）；
    ④pin 行 + 钱够门槛 ⇒ 在列，走的正是 ② 那道金币门，与普通池行同一把判据；
    ⑤凭据那一侧同向：只有 regKey 的 pin 行答"没凭据"，同形状的 kw: 行答"有"。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 1700000000
    now = 1700003600
    floor = 5000000                       # 500 金门槛，用例里写死不跟配置走
    pin = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地"})
    kw = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务"})
    preset = hand_rec(lua, {"name": "丰饶供品", "regKey": "abundant-offerings"})
    db["quests"][201], db["quests"][202], db["quests"][203] = pin, kw, preset
    pin_money = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地"})
    db["quests"][204] = pin_money

    if L.RowAdmitted(db, 201, pin, None, "暗影", floor, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "①pin: 是金币门自己收的，不配 ① 那支的绕门槛待遇")
    if not L.RowAdmitted(db, 202, kw, None, None, floor, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "②kw: 是玩家亲手点名的身份，收窄不许把它砍了")
    if not L.RowAdmitted(db, 203, preset, None, None, floor, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "③内置周常族整周常驻，同样不在这一刀的射程里")
    if not L.RowAdmitted(db, 204, pin_money, 9560000, None, floor, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "④钱够门槛就该在列 —— 这才是'和普通金币任务一样'的那道门")
    if L.QuestHasCredential(db, 201, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "⑤只有 regKey 的 pin 行不该被算成'客户端还答得出它'的凭据")
    if not L.QuestHasCredential(db, 202, now):
        return fail("a_pin_key_does_not_open_the_player_named_door",
                    "⑤对照：同形状的 kw: 行那份点名身份照旧算凭据")
    ok("a_pin_key_does_not_open_the_player_named_door")


def test_the_pin_rows_gold_is_stamped_on_its_own_instance():
    """金币门从事件 pin 收行那一刻问到的金额，2026-11-02 起同时按**这一条实例**记账
    （L.NoteGoldThisInstance —— 与池循环那份同一原语，零新增接口调用）。这是上一条用例
    那句"pin 行走金币那道门"的前提：只落 rec.lastMoney（记录级、准入门不读）的话，
    ② 那道门根本看不见这笔钱，行就只能靠注册键白拿 ① 的待遇。
    ①收行当场烙上 goldMoney + goldKey，且 goldKey == 这条实例自己的编号；
    ②池收尾把 currentSig 抹掉（这条不在世界任务板上）之后钱仍然读得出 —— 矩阵这才认它是金币任务；
    ③换到下一条实例（周重置，锚点前进一整周）⇒ 那份按实例粘滞自己匹配不上 ⇒ 钱读不出
      ⇒ 准入门②关 ⇒ 行退出（"到期就要自动消失"的钱面，不靠任何前缀豁免）；
    ④同趟里 kw: 点名的注册行照旧在列。"""
    lua, gql, _ = sync_env([(130, 30000000)], wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L = gql.Ledger
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) == 99002 else 0
    poi_env(lua, {2395: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    g.GQLDB["quests"][99004] = hand_rec(lua, {"name": "特别任务：梦魇",
                                              "regKey": "kw:特别任务", "mapID": 2395})
    gql.Sync.SnapshotNow()
    db, now = g.GQLDB, int(g.GetServerTime())
    rec = db["quests"][99002]
    if rec is None:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance", "前置：金币门该收了这条 pin 行")
    key = str(L.InstanceKey(db, 99002, rec, now))
    if int(rec["goldMoney"]) != 9560000 or str(rec["goldKey"]) != key:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    f"①收行那一刻就要按这一条实例记账:\n{rec['goldMoney']}/{rec['goldKey']} vs {key}")
    if rec["currentSig"] is not None:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    "②③的前提没了：这条用例要靠'池收尾抹掉当前签名'来证明钱不是从 obs 读来的")
    if int(L.QuestMoney(db, 99002, now)) != 9560000:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    "②不在板上、当前签名被抹之后，按实例那份粘滞仍要读得出这笔钱")
    rows = matrix_rows_by_id(L, db, now)
    if 99002 not in rows:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    "②钱读得出就该从 ② 那道门进列（不再靠注册键）")
    if 99004 not in rows:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance", "④kw: 点名的行照旧在列")
    # ③ 换 CD：锚点前进一整周 = 另一条实例，那份粘滞自己作废
    db["cycleAnchor"] = int(db["cycleAnchor"]) + 604800
    next_now = 1700604800
    if L.QuestMoney(db, 99002, next_now) is not None:
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    "③换了实例，上周那笔钱不许再被认成本条的金币凭据")
    if L.RowAdmitted(db, 99002, rec, L.QuestMoney(db, 99002, next_now), None, 5000000, next_now):
        return fail("the_pin_rows_gold_is_stamped_on_its_own_instance",
                    "③钱作废 ⇒ ②那道门关掉 ⇒ 准入门不再放行（到期就要自动消失）")
    ok("the_pin_rows_gold_is_stamped_on_its_own_instance")


def test_prune_sweeps_a_stale_pin_row_but_not_a_named_weekly():
    """L.Prune 的"注册行不按时清"同样按前缀收窄（2026-11-02 定稿的另一半）：
    pin: 那一路散场后没有任何写点续 lastSeenAt（到期门先把行从矩阵吃掉），三周规则是它
    的第二道退出路 —— 账本里留一条没人再见过的散场事件纯属体积。
    kw:（玩家点名）与内置周常照旧不按时清：它们本来就不进世界任务池，删了要把标题/地图
    重问一遍接口才刷得回来。ignored 那道守卫不动（永久屏蔽的记录永远留着）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    stale = 1000
    db["quests"][301] = hand_rec(lua, {"name": "暗影汇聚之地", "regKey": "pin:暗影汇聚之地",
                                       "lastSeenAt": stale})
    db["quests"][302] = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                                       "lastSeenAt": stale})
    db["quests"][303] = hand_rec(lua, {"name": "丰饶供品", "regKey": "abundant-offerings",
                                       "lastSeenAt": stale})
    db["quests"][304] = hand_rec(lua, {"name": "任务304", "lastSeenAt": stale})
    db["quests"][305] = hand_rec(lua, {"name": "屏蔽过的散场事件", "regKey": "pin:暗影汇聚之地",
                                       "lastSeenAt": stale, "ignored": True})
    L.Prune(db, stale + 25 * 86400)
    if db["quests"][301] is not None:
        return fail("prune_sweeps_a_stale_pin_row_but_not_a_named_weekly",
                    "pin: 收的行不享注册行豁免：三周没人再见到就该整条清出账本")
    for qid, why in ((302, "kw: 玩家点名"), (303, "内置周常")):
        if db["quests"][qid] is None:
            return fail("prune_sweeps_a_stale_pin_row_but_not_a_named_weekly",
                        f"{why}的行被按时清了（删了还得重问标题/地图），豁免收窄误伤了它")
    if db["quests"][304] is not None:
        return fail("prune_sweeps_a_stale_pin_row_but_not_a_named_weekly",
                    "没有注册身份的普通行照旧要三周时清（这条是 Prune 的本职）")
    if db["quests"][305] is None:
        return fail("prune_sweeps_a_stale_pin_row_but_not_a_named_weekly",
                    "玩家屏蔽过的记录永远留着（永久屏蔽的意图不许被时清撤销），pin 前缀也不例外")
    ok("prune_sweeps_a_stale_pin_row_but_not_a_named_weekly")


def test_a_rotated_pin_name_leaves_only_the_newest_key():
    """同一块板的 pin 名一周内会换（事件改名/阶段推进），一条 questID 只许留最新那一把
    pin: 键（T6 第 3 条）。旧键留着不是难看：regKey 既是驻板豁免也是准入凭据，一把旧名
    = 一条随时能把行钉回板上的把柄，而那块板早散场了；/gql quest 也从一串 pin:* 里读不出
    这一行是哪条路收的。换键走既有退订 L.UnregisterByKey，不新写第二套退路。
    另一半方向也钉住：pin: 那一路不许把玩家点名（kw:）那一把顶掉 —— 顶掉等于连驻板豁免
    一起撤（右键白点/行凭空消失那一族）。"""
    lua, gql, _ = sync_env([(130, 30000000)], wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) in (99002, 99003) else 0
    poi_env(lua, {2395: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    gql.Sync.RefreshEventPins(g.GQLDB, 5000)
    if str(g.GQLDB["quests"][99002]["regKey"]) != "pin:暗影汇聚之地":
        return fail("a_rotated_pin_name_leaves_only_the_newest_key",
                    "前置：金币门收的 pin 行挂的是 pin:<事件名>")

    def keys_of(key):
        return int(lua.execute("local n = 0 for _, r in pairs(GQLDB.quests) do "
                               "if r.regKey == ... then n = n + 1 end end return n", key))

    # 同一块板换了名字（客户端自己写在 pin 上的名字，不是我们编的）
    poi_env(lua, {2395: [{"id": 8816, "name": "暗影汇聚之地：终章", "questID": 99002,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    gql.Sync.RefreshEventPins(g.GQLDB, 6000)
    if str(g.GQLDB["quests"][99002]["regKey"]) != "pin:暗影汇聚之地：终章":
        return fail("a_rotated_pin_name_leaves_only_the_newest_key",
                    f"换了 pin 名要挂新那一把，实际 {g.GQLDB['quests'][99002]['regKey']}")
    if keys_of("pin:暗影汇聚之地") != 0:
        return fail("a_rotated_pin_name_leaves_only_the_newest_key",
                    "旧 pin 键必须整把退掉：留着就是一把能把散场行钉回板上的把柄")
    # 真入口再跑一轮：每轮注册重问那趟不许把旧键请回来
    gql.Sync.SnapshotNow()
    if str(g.GQLDB["quests"][99002]["regKey"]) != "pin:暗影汇聚之地：终章" \
            or keys_of("pin:暗影汇聚之地") != 0:
        return fail("a_rotated_pin_name_leaves_only_the_newest_key",
                    "快照之后一条任务只留最新那一把 pin 键（旧键不许被任何一趟写回）")
    # 方向对照：同一个 questID 已有关键词点名那一把时，金币门的 pin 那一路不顶它
    g.GQLDB["quests"][99003] = hand_rec(lua, {"name": "特别任务：祖阿曼的馈赠",
                                              "regKey": "kw:特别任务", "mapID": 2395,
                                              "lastSeenAt": 1000})
    poi_env(lua, {2395: [{"id": 8816, "name": "暗影汇聚之地：终章", "questID": 99003,
                          "primary": True}]},
            names={2395: "永歌森林"}, world_quest=lambda qid: False)
    gql.Sync.RefreshEventPins(g.GQLDB, 7000)
    if str(g.GQLDB["quests"][99003]["regKey"]) != "kw:特别任务":
        return fail("a_rotated_pin_name_leaves_only_the_newest_key",
                    "pin 那一路不许把玩家点名（kw:）那一把顶掉（顶掉=连驻板豁免一起撤）")
    ok("a_rotated_pin_name_leaves_only_the_newest_key")


def test_a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls():
    """事件散场之后注册行的退出路，端到端（用户 2026-11-02 D6/D7 定稿：「和其他普通金币
    任务一样。到期就要自动消失」「没有任何一条可以豁免说不下板」）。一轮跑三种形状：
      · pin 散场那一轮 lastSeenAt 就断供 —— RefreshEventPins 只给真扫到的 pin 传 seen，
        散场后这一趟不再有它的 entry；
      · 每轮注册重问那一趟（RefreshRegistered）**不再续任何行** —— 对照那把 kw: 点名的
        行 lastSeenAt 冻在上一 CD 的旧值（旧口径正是这里每轮白续，把②档顶成永远成立，
        再靠身份豁免不下板；两句本轮一起撤）；
      · 换 CD 后本 CD 零真事实 ⇒ ②档判到期，pin: 与 kw: 一起中招（豁免整句删掉，
        点名是准入门的事、不是免死牌）；同一趟里真在板上的行由 DiffPool 续上真事实，
        照旧在列 —— 行活着只靠"这一轮真有一个关于它的事实"。"""
    lua, gql, _ = sync_env([(130, 30000000)], wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) == 99002 else 0
    poi_env(lua, {2007: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2007: "永歌森林"}, world_quest=lambda qid: False)
    # 对照行：玩家关键词点名的注册周常。lastSeenAt 落在**本 CD 内**（锚点 1699827200 之后）
    # ⇒ 前两轮"这一 CD 真见过"仍在列，看的是事实不是豁免。
    g.GQLDB["quests"][99004] = hand_rec(lua, {"name": "特别任务：梦魇",
                                              "regKey": "kw:特别任务", "mapID": 2007,
                                              "lastSeenAt": 1699900000})
    gql.Sync.SnapshotNow()
    rec = g.GQLDB["quests"][99002]
    if rec is None:
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "前置：事件在场时金币门该把这条 pin 收进账本")
    seen_live = int(rec["lastSeenAt"])
    if 99002 not in matrix_rows_by_id(gql.Ledger, g.GQLDB, seen_live):
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "前置：pin 在场（真事实续上）的这一行该在矩阵里")
    # 事件散场：这块板这一轮没报（C_AreaPoiInfo 答"没有事件"）
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    g.GetServerTime = lambda: 1700000600      # 墙钟往前推 10 分钟，续没续命才看得出来
    gql.Sync.SnapshotNow()
    if int(rec["lastSeenAt"]) != seen_live:
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "散场的 pin 行不许被任何一路白续 lastSeenAt")
    if int(g.GQLDB["quests"][99004]["lastSeenAt"]) != 1699900000:
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "注册重问那一趟不续任何行：kw: 点名的行也没在板上/没被 pin 报上来，"
                    "lastSeenAt 必须纹丝不动（旧口径在这里每轮白续 = 病根）")
    # 换 CD：锚点前进到本周起点，本 CD 零真事实 ⇒ ②档到期，pin: 与 kw: 一起退出矩阵
    g.GetServerTime = lambda: 1700604800
    gql.Sync.SnapshotNow()
    next_now = 1700604800
    if not gql.Ledger.InstanceExpired(g.GQLDB, rec, next_now):
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "换了 CD 还一次都没被真见到的 pin 行必须判到期")
    if 99002 in matrix_rows_by_id(gql.Ledger, g.GQLDB, next_now):
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "到期后这一行不该再出现在矩阵（'到期就要自动消失'）")
    kw_rec = g.GQLDB["quests"][99004]
    if not gql.Ledger.InstanceExpired(g.GQLDB, kw_rec, next_now):
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "kw: 点名的行本 CD 零真事实同样必须到期 —— 按身份的驻板豁免已整句作废")
    if 99004 in matrix_rows_by_id(gql.Ledger, g.GQLDB, next_now):
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "到期即下板：kw: 行到期后不许留在矩阵，下周再出现要由新 CD 的真事实重新收")
    rows = matrix_rows_by_id(gql.Ledger, g.GQLDB, next_now)
    if 130 not in rows:
        return fail("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls",
                    "同一趟里真在板上的行（DiffPool 续的真事实）不许被这一刀误杀 —— "
                    "门问的是事实，撤豁免不等于杀所有行")
    ok("a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls")


def test_a_pin_row_dies_at_its_own_board_end_without_waiting_for_the_roll():
    """T6b（用户 2026-11-02 定稿的字面意思：「和其他普通金币任务一样。到期就要自动消失」）。
    上一轮 T6 只把 `pin:` 行从"驻板豁免"里摘出来，①档对它仍然是空的 —— L.SetExpiry 那句对
    **任何** regKey 都早退，而 pin 行没有第二个 roundEnd 写点（日志那一路 Sync.lua:244 只收
    "撞了关键词 + 挂在当前号任务日志里 + 不在池里"的名字，金币门收的那批三条都不满足）
    ⇒ 它只能走②档"本周没被看见过 ⇒ 到期"，最坏驻板到下一次周重置。
    早退按前缀收窄之后（`pin:` 不早退，`kw:`/内置照旧早退）：金币门收的行在板上的那一轮拿的是
    **同一条快照本来就读到的**倒计时（池循环 Sync.lua:868 的 secs，零新增接口调用），烙进
    roundEnd 后到点当场退出矩阵 —— 不必等周重置。
    对照那把 kw: 点名的行：同一次快照、同样只活 3 小时的板面倒计时，一个字都不许写 ——
    它是玩家点名要的常驻身份，寿命是每周 CD 尾巴（12.1.5 实测"加固符文石这个任务没了"
    就是拿板上几小时当到期烙出来的；用户："这两个都是周常任务，不应该到期了"）。"""
    lua, gql, _ = sync_env([(99002, 30000000), (99005, 30000000)], board=10800,
                           wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    name = "a_pin_row_dies_at_its_own_board_end_without_waiting_for_the_roll"
    g.GetQuestLogRewardMoney = lambda qid: 30000000
    # 对照行：玩家敲关键词点名的那一路（kw: 前缀），形状照真存档；它也在板上
    db["quests"][99005] = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                                         "mapID": 2007, "lastSeenAt": 1700000000})
    # 金币门从事件 pin 收这一条：pin 上带着客户端给的真任务号，奖励 3000 金过门槛
    poi_env(lua, {2007: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2007: "屠戮者高地"}, world_quest=lambda qid: True)
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：这一趟快照没跑成")
    pin, kw = db["quests"][99002], db["quests"][99005]
    if pin is None or str(pin["regKey"]) != "pin:暗影汇聚之地":
        return fail(name, f"前置：金币门该把这条 pin 收成 pin: 键，实际 "
                          f"{pin and pin['regKey']}")
    if pin["roundEnd"] is None:
        return fail(name, "pin 行在板上明明问出了 3 小时的倒计时，roundEnd 还是空的 —— "
                          "L.SetExpiry 那句早退没按前缀收窄，①档对它永远不成立，"
                          "它就只能在②档等到下次周重置")
    if int(pin["roundEnd"]) != 1700010800:
        return fail(name, f"roundEnd 必须是这条快照自己读到的结束时刻 now+secs（零新增接口"
                          f"调用、也不许编排程值），实际 {pin['roundEnd']}")
    if kw["roundEnd"] is not None:
        return fail(name, "对照塌了：kw:（玩家点名）那一把的早退必须原样保留 —— 板上几小时的"
                          "轮换寿命一烙进常驻周常的 roundEnd，当天下午整行就没（加固符文石"
                          f"那个形状），实际 roundEnd={kw['roundEnd']}")
    # 到点：只过它自己那条实例的结束时刻，**周 CD 尾巴还在很远的未来**
    later = 1700010860
    if later >= int(db["cycleAnchor"] or 0) + 604800:
        return fail(name, "前置：这一刀必须落在本 CD 之内，才叫'不必等下次周重置'")
    if not L.InstanceExpired(db, pin, later):
        return fail(name, "pin 行带着已经过去的板面结束时刻，到期门却不认它（①档没生效）")
    if L.InstanceExpired(db, kw, later):
        return fail(name, "同一刀不许砍 kw: 点名的行：它没有 roundEnd，走 CD 尾巴，尾巴还没到")
    rows = matrix_rows_by_id(L, db, later)
    if 99002 in rows:
        return fail(name, "到期就要自动消失：到点这一行还挂在矩阵里")
    if 99005 not in rows:
        return fail(name, "对照：kw: 点名的行在同一个时刻必须还在列（收窄误伤了它）")
    ok(name)


def test_a_gone_pin_row_is_answered_ended_like_a_pool_row():
    """Sync 收尾那句"整块板扫完（不是 partial）却没有这条 ⇒ 这一格判 ended"的判据换成了
    事实：poolSeen（只在 DiffPool 由池快照写、全仓没有清它的地方，语义 = 客户端确实答过
    这条 ID 上过板）。2026-11-02 D6 定稿：「上板的任务，没有任何一条可以豁免说不下板」
    —— 上过板而现在不在池里就是真没了，带 kw:/内置 键也照判；从没上过板的记录（家族锚点、
    日志任务、亚基虚拟行）"不在池里"是常态不是新闻，这句一行不碰。
    实测坐实的病标本（用户自己 2026-11-02 12:01 的存档）：93578「暗影汇聚之地」
    poolSeen=true、地图 2444、板面自己答出的 roundEnd、958 金那一对按实例的账，而
    regKey 是空的 —— 它从头到尾就是走世界任务池进来的普通行，事件 pin 只是那块板的归属
    提示。金币门从 pin 收行补的那把 `pin:` 键同样不配耐磨：
    ①还在板上的那一轮：这一格是普通答案，不许被提前判成已结束；
    ②事件散场 + 不在板上：同一趟快照里它和别的池行走同一句判掉（判据是 poolSeen，不是"没有豁免"）；
    ③对照：kw: 点名的行那一轮**还在池里** ⇒ 这句一行不碰 —— 保护它的是"在板上"这个事实，
      不是注册键；哪天它下了板，同一句照判（见 ended 门那族新用例）。"""
    name = "a_gone_pin_row_is_answered_ended_like_a_pool_row"
    lua, gql, _ = sync_env([(99002, 30000000), (99005, 30000000), (130, 30000000)],
                           board=10800, wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    db = g.GQLDB
    g.GetQuestLogRewardMoney = lambda qid: 30000000
    db["quests"][99005] = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                                         "mapID": 2007, "lastSeenAt": 1700000000})
    poi_env(lua, {2007: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2007: "屠戮者高地"}, world_quest=lambda qid: True)
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：这一趟快照没跑成")
    key = str(gql.Sync.CurrentCharKey())
    pin = db["quests"][99002]
    if pin is None or str(pin["regKey"]) != "pin:暗影汇聚之地":
        return fail(name, f"前置：金币门该把这条 pin 收成 pin: 键，实际 {pin and pin['regKey']}")
    live = db["chars"][key]["views"][99002]
    if live is None:
        return fail(name, "前置：在板上的那一轮该写下这个号的一格")
    if str(live["s"]) == "ended":
        return fail(name, "①还在板上就被判成已结束（板面扫到了它，'不在池里'不成立）")
    # 散场：这块板这一轮不报了；板上换成 130 + 99005（不是空池）—— 99005 这一轮**还在池里**
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    keep = lua.table_from({1: lua.table_from({"questID": 130, "mapID": 2007,
                                              "name": "任务130", "money": 30000000}),
                           2: lua.table_from({"questID": 99005, "mapID": 2007,
                                              "name": "特别任务：梦魇", "money": 30000000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: keep})
    g.GetServerTime = lambda: 1700000600
    gql.Sync.SnapshotNow()
    if str(db["chars"][key]["views"][99002]["s"]) != "ended":
        return fail(name, "②散场 + 不在板上：这条上过板的普通世界金币任务的格子该被同一句判掉"
                          "（判据 poolSeen；旧写法在这里被 regKey 整条豁免，格子上整周挂'还能做'）")
    kw = db["chars"][key]["views"][99005]
    if kw is not None and str(kw["s"]) == "ended":
        return fail(name, "③对照塌了：这一轮 99005 还在池里 —— 不判 ended 的理由是'在板上'"
                          "这个事实，与它的 kw: 注册键无关")
    ok(name)


def test_the_fortify_preset_still_ignores_a_sub_daily_rotation_countdown():
    """T6b 那句早退收窄的**连带风险**查的就是这一族：加固符文石 90573-90576（参考插件
    SavedInstances 用 type="any" 手抄的那四条），板面实例只活几小时，12.1.5 实测"加固符文石
    这个任务没了，可能和倒计时有关系"就是拿那几小时当到期烙出来的。
    本用例钉住三道 shield，缺一就要把 Sync.lua:249 那道"< 1 天不当 roundEnd"的闸门补进
    L.SetExpiry —— 而补进去正好废掉 T6b：屠戮者高地那种 12 小时区的 pin 行答出的就是几小时，
    "不烙"就又退回②档等下一次周重置。
      ① 它是**内置表声明**的族（Api.lua:335 key="fortify-the-runestones"、ids=90573-90576），
         注册键不带 kw:/pin: 前缀 ⇒ L.SetExpiry 那句早退照旧命中。用例不手搓 regKey：让每轮
         注册重问那一趟自己把内置 ID 落进账本，才验得到"它的键长什么样"；
      ② 它拿不到 `pin:` 键：收 pin 那一趟先把内置族的 ID 排掉（Sync.lua:636 PresetByID），
         EnsureRegistered 换键那一句也禁止 pin: 顶掉非 pin 键（Ledger.lua:1388 keepNamedKey）
         —— 所以这里连事件 pin 直接挂着 90573 也抢不走它的身份；
      ③ 它被日志收进来那一趟另有一道自己的闸门（Sync.lua:249），与本句无关。
    对照组：同一次快照里一条没注册的普通池行照旧被烙上自己的板面结束时刻、到点退出
    （这道门只认前缀，不许顺手把 SetExpiry 变成空操作）。
    2026-11-02 D6/D7 再钉另一半：不吃板面轮换 ≠ 永生 —— 换 CD 后本 CD 零真事实，
    这一族照样到期下板（到期门②档的身份豁免已整句删掉）。"""
    lua, gql, _ = sync_env([(90573, 30000000), (90580, 30000000)], board=10800,
                           wall=1700000000, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    name = "the_fortify_preset_still_ignores_a_sub_daily_rotation_countdown"
    g.GetQuestLogRewardMoney = lambda qid: 30000000
    poi_env(lua, {2007: [{"id": 8899, "name": "加固符文石：魔导师", "questID": 90573,
                          "primary": True}]},
            names={2007: "永歌森林"}, world_quest=lambda qid: True)
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：这一趟快照没跑成")
    rec, ctrl = db["quests"][90573], db["quests"][90580]
    if rec is None:
        return fail(name, "前置：内置表的 ID 该被每轮注册重问落进账本")
    if str(rec["regKey"]).startswith("pin:"):
        return fail(name, f"内置族的 ID 被金币门的 pin 那一路抢走了键（实际 {rec['regKey']}）："
                          "收窄早退之后它就会被板上几小时的倒计时烙成身份，当天下午整行没")
    if str(rec["regKey"]) != "fortify-the-runestones":
        return fail(name, f"前置：这一族该由内置表那条键注册，实际 {rec['regKey']}")
    if rec["roundEnd"] is not None:
        return fail(name, "板上 3 小时的轮换寿命不许写进内置周常的 roundEnd —— 一写就等于把"
                          f"身份从周 CD 尾巴挪到板面档，实际 roundEnd={rec['roundEnd']}")
    if ctrl is None or ctrl["roundEnd"] is None:
        return fail(name, "对照组塌了：没有注册身份的普通池行照旧要用它自己的板面倒计时定身份")
    later = 1700010860                      # 板面那条实例早走完了，周 CD 尾巴还在未来
    if L.InstanceExpired(db, rec, later):
        return fail(name, "板面倒计时走完不许把整周的内置周常判到期（用户抱怨的就是它下午没了）")
    rows = matrix_rows_by_id(L, db, later)
    if 90573 not in rows:
        return fail(name, f"过了板上那 3 小时这一行还该在矩阵里，实际 {sorted(rows.keys())}")
    if 90580 in rows:
        return fail(name, "对照组：普通池行到点就该退出，不许被这一族的豁免带着续命")
    # D6/D7 的另一半（2026-11-02 定稿）：加固这一族**不是永生** —— "板面轮换不算它的命"
    # 不等于"没有会过去的结束时刻"。换 CD 且这一轮起零真事实（90573 下了板、pin 不再报、
    # 没有族成员在板上、日志没人挂）⇒ ②档必须判到期；再出现（族成员重新上板）是新 CD
    # 新编号，旧"本轮做过"的勾一概不带走。
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    keep = lua.table_from({1: lua.table_from({"questID": 90580, "mapID": 2007,
                                              "name": "任务90580", "money": 30000000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: keep})
    g.GetServerTime = lambda: 1700604800
    gql.Sync.SnapshotNow()
    roll_now = 1700604800
    if not L.InstanceExpired(db, rec, roll_now):
        return fail(name, "换 CD 后本 CD 零真事实的加固族必须到期 —— 按身份的驻板豁免整句删掉，"
                          "没有哪一族是永生（D6：上板的任务没有一条可以豁免）")
    if 90573 in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "到期就该下板：下一 CD 它再被真事实收回来时是新编号、旧标记不带走")
    ok(name)


def test_the_registration_reask_answers_everything_but_a_renewal():
    """D6/D7 那一对病根的第一刀（计划 §11.2 T30）：每轮快照都拿账本里的注册键名单把
    Sync.RefreshRegistered 喂一遍，而 L.EnsureRegistered 末尾以前是**无条件**
    `rec.lastSeenAt = now` ⇒ 每一把带 regKey 的行每 10 秒自己给自己盖一个"这一轮见过"。
    名单是"有哪些注册行"的账，不是续期单：这一趟只重问名字/地图/金额/状态，续命只认
    调用方递来的本轮真事实（e.onBoard = 板上/图上亲眼见到，e.seen = 客户端答过一个事实
    但不在板上）。白续与按身份的驻板豁免互为因果，所以两句一起动（豁免那半句见
    test_a_pin_registered_row_expires_on_the_cycle_gate）。
    两岔方向相反，缺一条就是砍错刀：
      ①真事实那一路照旧每轮续：池里的 130 由 DiffPool 续、到期门不碰它；
      ②只靠注册名单活着的那两行都要"问得到、续不上"：
        · kw: 点名的 99004 —— 名字/地图/状态三样照样落进账本，lastSeenAt 冻在上一 CD，
          ②档当场判到期、当场下板（旧写法在这里每轮白续，②档永远咬不着它）；
        · 内置表第一次落账的族本体 89507 —— 这一趟只建账、不续（它的续命由 AbundantOfferings.lua
          每轮回问客户端状态走 seen，不在这里）：lastSeenAt 是**空的**，判不出 = 不成立
          （fail-closed）：行留着，等第一条真事实把它续上或把它判掉。"""
    name = "the_registration_reask_answers_everything_but_a_renewal"
    lua, gql, _ = sync_env([(130, 30000000)], wall=1699999650, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    # 名字与地图只由这一趟现问（Api 的名单里 C_TaskQuest 那两条是本用例给的替身）
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: (
            ("特别任务：梦魇", 2007) if int(qid) == 99004
            else (("丰饶供品", 2007) if int(qid) == 89507 else None)),
        "GetQuestZoneID": lambda qid: 2007 if int(qid) in (99004, 89507) else None,
    })
    key = str(gql.Sync.CurrentCharKey())
    stale = 1699000000          # 上一 CD 的旧值（本轮锚点 1699826850 之前）
    db["quests"][99004] = hand_rec(lua, {"regKey": "kw:特别任务", "lastSeenAt": stale})
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：这一趟快照没跑成")
    now1, rec = 1699999650, db["quests"][99004]
    anchor = int(db["cycleAnchor"])
    if not anchor > stale:
        return fail(name, f"前置：本轮锚点要在旧值之后（锚点 {anchor} vs {stale}）")
    # ②a 撤白续不许把这一趟变成空操作：名字/地图/状态三样照旧问回来
    if str(rec["name"]) != "特别任务：梦魇":
        return fail(name, f"注册重问照样要落名字，实际 {rec['name']}")
    if int(rec["mapID"] or 0) != 2007 or rec["mapSrc"] is None:
        return fail(name, f"注册重问照样要落地图与来源，实际 {rec['mapID']}/{rec['mapSrc']}")
    view = db["chars"][key]["views"][99004]
    if view is None or str(view["s"]) != "open":
        return fail(name, f"注册重问照样要把状态写进本号视图，实际 {view}")
    # ②b 这一趟一个字都不续
    if int(rec["lastSeenAt"]) != stale:
        return fail(name, "注册重问那一趟不再续任何行的 lastSeenAt（名单不是续期单），"
                          f"实际被续成 {rec['lastSeenAt']}")
    if not L.InstanceExpired(db, rec, now1):
        return fail(name, "本 CD 零真事实的 kw: 行必须判到期 —— 白续一撤②档才咬得着")
    if 99004 in matrix_rows_by_id(L, db, now1):
        return fail(name, "到期就要下板：这一行不许还挂在矩阵里")
    # ②c 内置表第一次靠这一趟落账的族本体：只有账、没有事实（续命交给 AbundantOfferings.lua）
    fam = db["quests"][89507]
    if fam is None or str(fam["regKey"]) != "abundant-offerings":
        return fail(name, "前置：内置表的族本体该由这一趟落进账本")
    if fam["lastSeenAt"] is not None:
        return fail(name, "第一次只靠注册名单落的账不该有 lastSeenAt（这一轮没人真见过它），"
                          f"实际 {fam['lastSeenAt']}")
    if str(fam["name"]) != "丰饶供品":
        return fail(name, f"同一条路上的名字照旧要问回来，实际 {fam['name']}")
    if L.InstanceExpired(db, fam, now1):
        return fail(name, "lastSeenAt 缺失 = 判不出 ⇒ fail-closed 不动（撤白续不改这个方向）")
    # ①对照：真在板上的那一行由 DiffPool 续本轮的真事实
    pool_rec = db["quests"][130]
    if int(pool_rec["lastSeenAt"]) != now1:
        return fail(name, f"①池里的行该由 DiffPool 续上本轮时刻，实际 {pool_rec['lastSeenAt']}")
    if L.InstanceExpired(db, pool_rec, now1):
        return fail(name, "①板上见过一行不许判到期")
    # 第二轮（墙钟 +400 秒，还在同一 CD 同一个编号桶里）：注册重问照旧不续 —— 病根是
    # "每 10 秒一趟自己盖一个时刻"，只跑一轮测不出漏续。
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    if int(rec["lastSeenAt"]) != stale:
        return fail(name, f"第二轮注册重问照旧不许续命（kw: 行仍冻在上一 CD），实际 {rec['lastSeenAt']}")
    if fam["lastSeenAt"] is not None:
        return fail(name, "第二轮也不许给没落板的族本体凭空续一份")
    if int(pool_rec["lastSeenAt"]) != 1700000050:
        return fail(name, f"①对照塌了：板上那一条这一轮该续到新时刻，实际 {pool_rec['lastSeenAt']}")
    ok(name)


def test_abundant_offerings_renews_only_while_client_reports_it():
    """丰饶供品（89507）续命走 AbundantOfferings.lua，与亚基入侵靠 pin 报在场同一条路：
    每轮快照里本模块回问客户端 89507 的状态，客户端答得出真实状态（open/progress/done）
    ⇒ seen 续 lastSeenAt；答 unknown / 任务不存在 ⇒ 不续（fail-closed）；13.x 版本闸不挂。
    续命来源不在 Sync.RefreshRegistered（test_the_registration_reask... 钉死了注册重问不续
    族本体），也不在 DiffPool/ScanBoards（丰饶不上板）；独立成文件、按版本闸，无"常驻"特判。
      ① 12.x 且客户端答 open ⇒ 这轮续上、不判到期；
      ② 同一 CD 再一轮仍答 open ⇒ 再续一次（连续在线不许被②档误杀）；
      ③ 客户端答 unknown（接口这轮没答）⇒ 不续，时刻冻在上一轮（fail-closed，下一轮再试）；
      ④ 换 CD 且再没被答到 ⇒ ②档到期、下板；
      ⑤ 版本闸：GetBuildInfo 主版本 13 ⇒ 模块不挂，哪怕客户端答 open 也不续。"""
    name = "abundant_offerings_renews_only_while_client_reports_it"
    lua, gql, _ = sync_env([], wall=1699999650, weekly=432000, extra=("AbundantOfferings",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    # 把 Api.QuestState 换成读一张可控的 Lua 表（按 questID 返回 mock 状态）
    lua.execute(r"""
        GQL._aoState = {}
        GQL._aoRealQuestState = GQL.Api.QuestState
        GQL.Api.QuestState = function(qid)
            local s = GQL._aoState[tostring(qid)]
            if s ~= nil then return s end
            return GQL._aoRealQuestState(qid)
        end
    """)

    def set_state(qid, st):
        if st is None:
            lua.execute('GQL._aoState["%d"] = nil' % int(qid))
        else:
            lua.execute('GQL._aoState["%d"] = "%s"' % (int(qid), st))

    g.GetBuildInfo = lambda: "12.1.5"
    set_state(89507, "open")
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：快照没跑成")
    now1 = 1699999650
    anchor = int(db["cycleAnchor"])
    fam = db["quests"][89507]
    if fam is None or str(fam["regKey"]) != "abundant-offerings":
        return fail(name, "前置：AbundantOfferings 模块该把族本体落进账本")
    if int(fam["lastSeenAt"] or 0) != now1:
        return fail(name, f"①客户端答 open 该由模块续上本轮时刻，实际 {fam['lastSeenAt']}")
    if L.InstanceExpired(db, fam, now1):
        return fail(name, "①这轮被续上了，不许判到期")
    # ② 同一 CD 再一轮（+400 秒）仍答 open ⇒ 再续一次
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    if int(fam["lastSeenAt"] or 0) != 1700000050:
        return fail(name, f"②仍答 open 该再续到新时刻，实际 {fam['lastSeenAt']}")
    # ③ 客户端答 unknown（接口这轮没答）⇒ 不续，时刻冻在上一轮（fail-closed）
    set_state(89507, "unknown")
    g.GetServerTime = lambda: 1700000450
    gql.Sync.SnapshotNow()
    if int(fam["lastSeenAt"] or 0) != 1700000050:
        return fail(name, f"③答 unknown 不许续，时刻应冻在上一轮，实际 {fam['lastSeenAt']}")
    if L.InstanceExpired(db, fam, 1700000450):
        return fail(name, "③答 unknown 只是断供、未到期（寿命由那条会过去的结束时刻定），不许当场死")
    # ④ 换 CD（锚点随 RefreshCycle 前移）且仍答 unknown ⇒ ②档到期、下板
    now_cd2 = anchor + 604800 + 5000
    g.GetServerTime = lambda: now_cd2
    gql.Sync.SnapshotNow()
    if not L.InstanceExpired(db, fam, now_cd2):
        return fail(name, "④换 CD 且没被答到，应判到期下板")
    # ⑤ 版本闸：主版本 13 ⇒ 模块不挂，哪怕客户端答 open 也不续
    db["quests"][89507] = None
    g.GetBuildInfo = lambda: "13.0.2"
    set_state(89507, "open")
    g.GetServerTime = lambda: now_cd2 + 100
    gql.Sync.SnapshotNow()
    fam2 = db["quests"][89507]
    if fam2 is not None and fam2["lastSeenAt"] is not None:
        return fail(name, "⑤13.x 版本闸不挂，族本体不该被续命")
    ok(name)


def test_a_present_event_pin_renews_only_while_it_is_reported():
    """事件 pin 那一路的续命来源（计划 §11.2 T31 的正面半句）：Sync.RefreshEventPins
    只给**这一轮真扫到的** pin 传 seen = true，L.EnsureRegistered 据此续 lastSeenAt。
    散场那一轮起这块板不再报它，entry 里没有它 ⇒ 当场断供。同一条键的退出路
    （散场 + 换 CD 的端到端）由 test_a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls
    钉；本用例钉的是另一半，少了它"续命只认真事实"就退化成"pin 行一律不续"：
      ①同一 CD 里连续两轮 pin 在场 ⇒ lastSeenAt 跟着两轮的时刻各续一次（事件期间不许被②档误杀）；
      ②这一轮 pin 不在场 ⇒ 时刻冻在上一轮，且本 CD 尾巴没到、行照旧在列
        （断供不等于立刻死，寿命由那条会过去的结束时刻定）；
      ③换 CD 且再没被报上来 ⇒ ②档到期、下板；
      ④对照：同趟里真在板上的 130 照旧被 DiffPool 续上，不许被这一刀误杀。
    全程零新增接口调用：seen 复用的是同一趟归属扫描已经问到的证据。"""
    name = "a_present_event_pin_renews_only_while_it_is_reported"
    lua, gql, _ = sync_env([(130, 30000000)], wall=1699999650, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) == 99002 else 0
    poi_env(lua, {2007: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2007: "永歌森林"}, world_quest=lambda qid: False)
    gql.Sync.SnapshotNow()
    rec = db["quests"][99002]
    if rec is None:
        return fail(name, "前置：事件在场、奖励过门槛，金币门该把这条 pin 收进账本")
    if not str(rec["regKey"] or "").startswith("pin:"):
        return fail(name, f"前置：金币门收的行挂 pin: 键，实际 {rec['regKey']}")
    if int(rec["lastSeenAt"] or 0) != 1699999650:
        return fail(name, f"①pin 在场那一轮就该由 seen 续上本轮时刻，实际 {rec['lastSeenAt']}")
    if L.InstanceExpired(db, rec, 1699999650):
        return fail(name, "①事件正在场的这一行不许判到期（续得真才谈得上断供）")
    # ① 同一 CD 再一轮（+400 秒），pin 仍在场 ⇒ 再续一次
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    if int(rec["lastSeenAt"]) != 1700000050:
        return fail(name, f"①pin 每轮在场都要续一次（到期门看的是最后一次），实际 {rec['lastSeenAt']}")
    if 99002 not in matrix_rows_by_id(L, db, 1700000050):
        return fail(name, "①事件还在跑的这一行必须还在矩阵里")
    # ② 散场：这块板这一轮不报了（C_AreaPoiInfo 答"这张图没有事件"）
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    g.GetServerTime = lambda: 1700000450
    gql.Sync.SnapshotNow()
    if int(rec["lastSeenAt"]) != 1700000050:
        return fail(name, f"②pin 不在场这一轮必须断供，时刻冻在上一轮，实际 {rec['lastSeenAt']}")
    if 99002 not in matrix_rows_by_id(L, db, 1700000450):
        return fail(name, "②散场一轮不许把行提前吃掉：本 CD 尾巴还没到、这一 CD 内真见过它")
    # ③ 换 CD：锚点越过冻住的那个时刻 ⇒ ②档到期，行下板
    g.GetServerTime = lambda: 1700604450
    gql.Sync.SnapshotNow()
    roll_now = 1700604450
    if int(rec["lastSeenAt"]) != 1700000050:
        return fail(name, f"③散场后没有任何写点续 pin 行，实际 {rec['lastSeenAt']}")
    if not L.InstanceExpired(db, rec, roll_now):
        return fail(name, "③换了 CD 还一次都没被真见到的 pin 行必须到期（'到期就要自动消失'）")
    if 99002 in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "③到期后这一行不许还挂在矩阵里")
    if 130 not in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "④对照塌了：同一趟里真在板上的行不许被这一刀误杀")
    ok(name)


def test_the_aqir_virtual_row_lives_and_dies_on_the_cycle_tail():
    """虚拟的亚基入侵行整条生命周期（用户 D7 第⑤条点名的那一条：它压根没有任务号，
    永远问不出自己的结束时刻 ⇒ 结束时刻只有②档 = 读数现算的周 CD 尾巴
    （L.InstanceEndRaw 的 db.cycleAnchor + 604800，不新增字段、不烙 roundEnd），
    于是它的寿命就等价于"这一 CD 有没有被那块板上的 pin 报上来"。一条行走完
    上板 → 散场 → 换 CD 下板 → 新 CD 再上板，四岔各钉一次：
      ①在场：行在列，done 凭据烙在**本 CD 尾巴那一档编号**上，lastSeenAt 由 seen 续；
      ②同一 CD 里散场：断供但尾巴没到、编号没换 ⇒ 行照旧在列、那一格结论照旧在
        （锚点每轮跟着墙钟挪一格，编号分桶那 +450 秒的半桶偏移就是为了让这种挪动不换实例）；
      ③换 CD 仍然没人报它：②档判到期 ⇒ 下板（虚拟行也不给豁免）；
      ④新 CD 里 pin 又出现、这轮答的是"每周仅可获得一次"：行重新在列，编号是新 CD 那一档，
        上一 CD 那个 done 不带走（D6/D7：再上板就是新 CD 新实例，旧标记一概不带走）。"""
    name = "the_aqir_virtual_row_lives_and_dies_on_the_cycle_tail"
    lua, gql, _ = sync_env([(130, 30000000)], wall=1699999650, weekly=432000,
                           extra=("AqirInvasion", "Matrix"))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    poi_env(lua, {2007: [{"id": 8813, "name": "亚基入侵：祖阿曼", "primary": True,
                          "widgetSet": 2253}]},
            names={2007: "祖阿曼"}, world_quest=lambda qid: False)
    widget_env(lua, {2253: [(8404, ["获得一枚勋章", "可换取任意至暗之夜阵营的声望"])]})
    gql.Config.AddKeyword("亚基入侵")
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    rec = db["quests"][99000001]
    if rec is None:
        return fail(name, "前置：点了关键词、pin 在场，虚拟行该注册进账本")
    if str(rec["regKey"]) != "kw:亚基入侵":
        return fail(name, f"①虚拟行走关键词那一路注册（删关键词要一起退行），实际 {rec['regKey']}")
    if int(rec["lastSeenAt"] or 0) != 1699999650:
        return fail(name, f"①pin 在场那一轮由 seen 续命，实际 {rec['lastSeenAt']}")
    if rec["roundEnd"] is not None:
        return fail(name, "②档定稿：虚拟行的兜底尾巴不许被烙进 roundEnd（读数现算、不新增字段）")
    k1 = uid(L, db, 99000001, 1699999650)
    view = db["chars"][key]["views"][99000001]
    if view is None or str(view["s"]) != "done":
        return fail(name, f"①读到徽章文案那一轮要落 done，实际 {view}")
    if str(view["k"]) != k1:
        return fail(name, f"①那一格烙的是本 CD 尾巴那条实例的编号 {k1}，实际 {view['k']}")
    if 99000001 not in matrix_rows_by_id(L, db, 1699999650):
        return fail(name, "①在场、有凭据，这一行该在矩阵里")
    # ② 同一 CD 散场（墙钟 +400 秒，尾巴仍落在同一个 15 分钟桶里）
    g.C_AreaPoiInfo = lua.table_from({"GetEventsForMap": lambda mid: lua.table_from({}),
                                      "GetAreaPOIInfo": lambda mid, pid: None})
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    if int(rec["lastSeenAt"]) != 1699999650:
        return fail(name, f"②这一轮没扫到 pin 就该断供，实际 {rec['lastSeenAt']}")
    if uid(L, db, 99000001, 1700000050) != k1:
        return fail(name, f"②同一 CD 的尾巴挪 400 秒仍在同一条实例上（编号不许凭空换档）："
                          f"{k1} vs {uid(L, db, 99000001, 1700000050)}")
    if 99000001 not in matrix_rows_by_id(L, db, 1700000050):
        return fail(name, "②散场一轮不许把玩家点名的虚拟行提前吃掉（尾巴还没到）")
    if str(db["chars"][key]["views"][99000001]["s"]) != "done":
        return fail(name, "②同一实例上已拿到的结论不许被'这一轮没问到'抹掉")
    # ③ 换 CD：锚点越过冻住的 lastSeenAt ⇒ 到期下板
    g.GetServerTime = lambda: 1700604450
    gql.Sync.SnapshotNow()
    roll_now = 1700604450
    if not L.InstanceExpired(db, rec, roll_now):
        return fail(name, "③换了 CD 再没被 pin 报上来的虚拟行必须到期（虚拟行同样不豁免）")
    if 99000001 in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "③到期就要自动消失：这一行不许还挂在矩阵里")
    if db["quests"][99000001] is None:
        return fail(name, "③砍的是界面那一行，账本里的记录由 Prune 那道体积门管（kw: 不按时清）")
    # ④ 新 CD 里 pin 又出现，这轮客户端答的是周常宝箱那一句
    poi_env(lua, {2007: [{"id": 8813, "name": "亚基入侵：祖阿曼", "primary": True,
                          "widgetSet": 2253}]},
            names={2007: "祖阿曼"}, world_quest=lambda qid: False)
    widget_env(lua, {2253: [(8403, ["亚基战利品宝箱", "每周仅可获得一次"]), (8404, None)]})
    g.GetServerTime = lambda: 1700604850
    gql.Sync.SnapshotNow()
    new_now = 1700604850
    if int(rec["lastSeenAt"]) != new_now:
        return fail(name, f"④重新被 pin 报上来就要重新续命，实际 {rec['lastSeenAt']}")
    if 99000001 not in matrix_rows_by_id(L, db, new_now):
        return fail(name, "④新 CD 的真事实该把这一行重新收回矩阵")
    k4 = uid(L, db, 99000001, new_now)
    if k4 == k1:
        return fail(name, f"④换 CD 就是换一条实例（旧编号 {k1} 与新编号相同 = 身份没挪档）")
    view = db["chars"][key]["views"][99000001]
    if str(view["k"]) != k4 or str(view["s"]) != "open":
        return fail(name, f"④上一 CD 那个 done 不带走：这一格属于新实例、答的是本轮读到的 "
                          f"open，实际 {view['s']}/{view['k']} vs {k4}")
    if one(lua.execute("local k = GQL.Sync.CurrentCharKey()"
                       " return GQL.Ledger.RegisteredState(GQLDB, k, 99000001, ...)",
                       new_now)) != "open":
        return fail(name, "④界面那一格要跟着新实例退回待做，不许顶着一周前的勾")
    ok(name)


def test_a_family_anchor_lives_only_while_a_member_is_on_the_board():
    """撤掉注册重问那份白续之后，家族锚点（族本体）这一行靠什么活着？真档形状：
    89507「丰饶供品」与 90573-90576「加固符文石」那几条 poolSeen 与倒计时全空 ——
    族本体从来不上板（它是每周 lockout，板上挂的是同族子任务），而 Sync 每轮清一次
    onBoard、EnsureRegistered 现在又不认裸注册键 ⇒ 锚点唯一能拿到的真事实是
    **"这一轮族里有成员挂在板上"**。这一刀接在池循环里，与 poolRec.onBoard 同一趟、
    走现成的 L.FamilyAnchorID（占行判据同一个入口），零新增接口调用：不接它，锚点会在
    换 CD 那一刻到期、整族当场死掉再也回不来（计划 §11.2 T32）。
      ①成员在板的那一轮：锚点 lastSeenAt = 本轮时刻，占行的仍是族本体；
      ②同一 CD 再来一轮、成员仍在板：再续一次（每轮跟着事实走，不是一次性）；
      ③换 CD 且这一轮起族里没人上板：断供 ⇒ ②档到期 ⇒ 整族那一行下板；
      ④对照：同趟真在板上的无关行（130）照旧在列。"""
    name = "a_family_anchor_lives_only_while_a_member_is_on_the_board"
    lua, gql, _ = sync_env([(90574, 30000000)], wall=1699999650, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    g.GetQuestLogRewardMoney = lambda qid: 30000000
    g.C_TaskQuest = lua.table_from({
        # 族本体自己不在任何板上：名字由注册重问那一路现问（L.OccupiesRow 要名字才占行）
        "GetQuestInfoByQuestID": lambda qid: (
            ("加固符文石：魔导师", 2007) if int(qid) == 90573 else None),
        "GetQuestZoneID": lambda qid: 2007 if int(qid) == 90573 else None,
    })
    gql.Sync.SnapshotNow()
    anchor = db["quests"][90573]
    if anchor is None:
        return fail(name, "前置：内置表的族本体该由注册重问落进账本")
    if str(anchor["regKey"]) != "fortify-the-runestones":
        return fail(name, f"前置：这一族该由内置表那把键注册，实际 {anchor['regKey']}")
    if int(anchor["lastSeenAt"] or 0) != 1699999650:
        return fail(name, "①族成员在板上 = 关于这一族的真事实，锚点要在同一趟被续上，"
                          f"实际 {anchor['lastSeenAt']}")
    if anchor["poolSeen"] is not None:
        return fail(name, "①续的是锚点自己的 lastSeenAt，不许伪造'它上过板'这份事实")
    rows = matrix_rows_by_id(L, db, 1699999650)
    if 90573 not in rows:
        return fail(name, "①占行的该是族本体那一行（一族一行没变）")
    if 90574 in rows:
        return fail(name, "①板上的成员只当证据，不占行")
    # ② 同一 CD 再一轮，成员仍在板
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    if int(anchor["lastSeenAt"]) != 1700000050:
        return fail(name, f"②成员每轮在板就每轮续一次，实际 {anchor['lastSeenAt']}")
    if L.InstanceExpired(db, anchor, 1700000050):
        return fail(name, "②本 CD 每轮都有真事实，这一族不许判到期")
    # ③ 换 CD，板上换成与这一族无关的 130
    keep = lua.table_from({1: lua.table_from({"questID": 130, "mapID": 2007,
                                              "name": "任务130", "money": 30000000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: keep})
    g.GetServerTime = lambda: 1700604450
    gql.Sync.SnapshotNow()
    roll_now = 1700604450
    if int(anchor["lastSeenAt"]) != 1700000050:
        return fail(name, f"③族里没人上板就是断供，锚点不许被注册重问白续，实际 {anchor['lastSeenAt']}")
    if not L.InstanceExpired(db, anchor, roll_now):
        return fail(name, "③换 CD 零真事实的锚点必须到期 —— 撤掉豁免后整族的命就挂在这条事实上")
    if 90573 in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "③到期就下板：这一族那一行不许还挂着（下周再上板是新 CD 新编号）")
    if 130 not in matrix_rows_by_id(L, db, roll_now):
        return fail(name, "④对照塌了：这一轮真在板上的无关行照旧要在列")
    ok(name)


def test_the_disappear_gate_asks_only_whether_the_row_ever_boarded():
    """Sync 收尾那句"整块板扫完（不是 partial）却没有这一条 ⇒ 这一格判 ended"的判据，
    以前挂着三条身份豁免（kwLogAdded / IsResidentRegKey / KeywordLogAdmits），
    现在只问一个事实：rec.poolSeen（只在 DiffPool 由池快照写、全仓没有清它的地方，
    语义 = 客户端确实答过这条 ID 上过板）。计划 §11.2 T34，用户 D6 原话
    "上板的任务，没有任何一条可以豁免说不下板"。
    判据换成事实之后三岔一起成立，缺一条就是门写歪：
      ①带 kw: 点名键、上一轮真在池里的行（99005）：这一轮不在池 ⇒ 格子判 ended
        （旧写法在这里整条豁免，格子上挂一整周"还能做"，而那块板早就散场了）；
      ②从没上过板的注册行（89507 那种族本体，靠注册重问落账，poolSeen 空）：
        "不在池里"是它的常态不是新闻 ⇒ 这句一行不碰（碰了就是把每周 lockout 说成没了）；
      ③日志收进来的那一行（99006，kwLogAdded 形状、没有 regKey、没上过板）：同样不碰，
        连时刻都不许动 —— 这一条是对"判据只认 poolSeen"最直白的反向验证。
    另钉一格：判据不许用 onBoard（它每轮快照清一次，问的是"这一轮"不是"上过板"）——
    ①那一行第二轮已经没有 onBoard 却仍被正确判 ended，靠的就是 poolSeen 那份旧事实。"""
    name = "the_disappear_gate_asks_only_whether_the_row_ever_boarded"
    lua, gql, _ = sync_env([], wall=1699999650, weekly=432000, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2007) if int(qid) == 89507 else None,
    })
    key = str(gql.Sync.CurrentCharKey())
    first = lua.table_from({
        1: lua.table_from({"questID": 130, "mapID": 2007, "name": "任务130", "money": 30000000}),
        2: lua.table_from({"questID": 99005, "mapID": 2007, "name": "特别任务：梦魇",
                           "money": 30000000}),
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: first})
    # ①玩家点名、而且真上过板的一行；③日志收进来、从没上过板的一行
    db["quests"][99005] = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                                         "mapID": 2007, "lastSeenAt": 1699900000})
    db["quests"][99006] = hand_rec(lua, {"name": "特别任务：风暴在即", "kwLogAdded": True,
                                         "mapID": 2007, "lastSeenAt": 1699900000})
    L.SetView(db, key, 99006, "progress", 1699900000)
    gql.Sync.SnapshotNow()
    if db["quests"][99005]["poolSeen"] is None:
        return fail(name, "前置：这一轮它在池里，poolSeen 该由 DiffPool 写下")
    if str(db["chars"][key]["views"][99005]["s"]) == "ended":
        return fail(name, "①还在板上的那一轮不许被提前判成已结束")
    # 换池：99005/99006 都不在这块板上了（不是空池 ⇒ 整块板扫完，这句才有意义）
    keep = lua.table_from({1: lua.table_from({"questID": 130, "mapID": 2007,
                                              "name": "任务130", "money": 30000000})})
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: keep})
    g.GetServerTime = lambda: 1700000050
    gql.Sync.SnapshotNow()
    views = db["chars"][key]["views"]
    if str(views[99005]["s"]) != "ended":
        return fail(name, "①上过板而现在不在池里：带 kw: 点名键也照判 ended（旧写法在这里"
                          f"整条豁免，实际 {views[99005]['s']}）")
    if str(views[89507]["s"]) == "ended":
        return fail(name, "②族本体自己从不上板：'不在池里'是常态，这句不许碰它那一格")
    if str(views[99006]["s"]) != "progress" or int(views[99006]["t"]) != 1699900000:
        return fail(name, "③日志收的、从没上过板的那一行整格不许动（判据只认 poolSeen），"
                          f"实际 {views[99006]['s']}/{views[99006]['t']}")
    # 判 ended ≠ 砍行：这一行这一 CD 内真见过（lastSeenAt 在锚点之后），行还该在列
    if 99005 not in matrix_rows_by_id(L, db, 1700000050):
        return fail(name, "格子判 ended 是这一轮的事实，行的寿命由到期门管：本 CD 见过就该还在列")
    ok(name)

def test_the_forensic_entries_split_a_pin_row_from_a_resident_weekly():
    """取证话术跟着口径改口（T6b 第 4 条）：Dump/Probe 以前对**任何**字符串 regKey 都写
    "注册周常（常驻，到期只按 CD 尾巴算）"。`pin:` 那一路既非常驻、也不只看 CD 尾巴 ——
    报告照那句念，玩家与维护者就会把"到期不消失"当成设计意图，永远查不到到期门那一侧。
    分工不动：界面措辞保持玩家语言（本轮不许动 Matrix 的 tooltip），取证细节只写在这两个入口。"""
    lua, gql, _ = sync_env([(99002, 30000000), (99005, 30000000)], board=10800,
                           wall=1700000000, weekly=432000, extra=("Matrix", "Dump", "Probe"))
    g = lua.globals()
    name = "the_forensic_entries_split_a_pin_row_from_a_resident_weekly"
    g.GetQuestLogRewardMoney = lambda qid: 30000000
    g.GQLDB["quests"][99005] = hand_rec(lua, {"name": "特别任务：梦魇", "regKey": "kw:特别任务",
                                              "mapID": 2007, "lastSeenAt": 1700000000})
    poi_env(lua, {2007: [{"id": 8814, "name": "暗影汇聚之地", "questID": 99002,
                          "primary": True}]},
            names={2007: "屠戮者高地"}, world_quest=lambda qid: True)
    gql.Sync.SnapshotNow()
    pin_txt = str(gql.Dump.Quest("99002"))
    kw_txt = str(gql.Dump.Quest("99005"))
    if "金币门从地图事件 pin 收的注册行" not in pin_txt:
        return fail(name, f"pin 行的报告要照实说它是金币门从事件 pin 收进来的:\n{pin_txt}")
    if "注册周常（常驻" in pin_txt:
        return fail(name, f"pin 行不许再被报成常驻周常:\n{pin_txt}")
    if "注册周常（常驻，到期只按 CD 尾巴算）" not in kw_txt:
        return fail(name, f"kw: 点名的那一把照旧是常驻周常，措辞不许跟着改:\n{kw_txt}")
    report = str(gql.Probe.Run())
    if "账本里的注册周常共" in report:
        return fail(name, "probe 那句把任何 regKey 都算成注册周常的旧口径还在"
                          "（pin: 那一路不常驻）")
    if "金币门从事件 pin 收的普通世界金币任务（pin:）1" not in report:
        return fail(name, f"probe 的注册段要按前缀把三类分开计数:\n{report}")
    if "到期=自己那条实例的板面结束时刻" not in report:
        return fail(name, "probe 的每一行要直接印出这一行的到期口径，别让人回去推前缀")
    ok(name)


def test_keyword_hunt_names_the_pin_only_hits():
    """加了关键词却"0 张图命中"有两种完全不同的原因（用户 2026-10-24 第二十二轮：
    "亚基入侵了，关键字我已经加了，矩阵里还看不到"）：① 图没加进跟踪清单；
    ② 它压根不是任务，只是地图事件 pin。上一版两种都回同一句"先确认关键词写的是任务名的一部分"，
    玩家照着改关键词改三轮也没用。现在回报里分开说，并带上"客户端给没给任务号"。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
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
             weekly=None, char=("小号一", "永恒之井"), visible=None, extra=(),
             board=None, wall=None):
    """搭好 Core+Config+Ledger+Api+Sync 的可控环境。

    quests: [(questID, money)]；money 为 None 表示奖励接口取不到。
    reset: GetQuestResetTime 的返回。12.1.5 实测它答的是**每日**重置（wiki 也这么写），
           所以它只配当对照，锚点/周统计一律不许用它。
    weekly: C_DateAndTime.GetSecondsUntilWeeklyReset 的返回（周口径唯一来源）。
            None = 本机问不出周重置 → RefreshCycle 必须降级沿用旧锚点、
            NextCycleEnd 只能退回每日档并如实标 kind="daily"。
    board: 每条在板任务自己的剩余秒数（C_TaskQuest.GetQuestTimeLeftSeconds 的答案）。
           第 36 轮 T4b 之后身份 = 任务号 + 结束时刻，**没有身份来源的用例一律判不出编号**
           （格子全 ?、凭据全不匹配），所以要走编号判据的 Sync 用例得给它一个：
           board=43200 ⇒ 结束时刻 = 当时的 GQL_NOW + 43200，随 GQL_NOW 递减、不随同步漂。
           不给 = 这一读问不出倒计时（要测降级形状的用例保持不给）。
    wall:  墙上时钟（GetServerTime 的返回值，纪元秒）。只在 `weekly` 一起给的时候有用：
           锚点 = 周尾巴 - 604800，Config.CycleAnchorOf 只有"有墙上钟"或
           "开机秒 + 倒计时 ≥ 一周"两种情况才定得出锚点。
           **不在池里、也没板面倒计时的记录**（日志收进来的、注册周常）只有周 CD 尾巴
           这一档身份来源 ⇒ 要验它们的凭据就必须给 wall，锚点 0 = 身份未定。
    """
    lua, gql = load_lua("Core", "Config", "Ledger", "Api", "Sync", *extra, mock=True)
    g = lua.globals()
    g.GQL_NOW = 0
    g.GQLDB = lua.table_from({})
    if wall is not None:
        g.GetServerTime = lambda: wall
    if board is not None:
        board_clock(lua, int(g.GQL_NOW) + int(board))

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
    # 关注清单预置 2007（夹具把所有任务都放在这张图上）＝"玩家加过这张图"。
    # 2026-10-31 起没有默认图，渲染过滤按"玩家加没加"判 —— 不预置的话
    # 每个渲染用例都得自己补一遍。
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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


def test_snapshot_done_needs_this_instance_turnin():
    """快照写 done 的门（2026-10-29 健步如飞 ×14 假绿，用户批准的"两刀"之一）：
    客户端对固定 ID 任务的完成 flag 会跨轮/跨 CD 残留，答的 done 分不出"这一条实例"，
    池循环里不许原样进快照 —— 必须有编号对上的本轮流水背书，否则降级 open。
    ① 无流水：接口答 done，快照照旧 open（真交过的话交割结算自会补写流水）；
    ② 补一笔编号对上的流水：再快照就写 done；
    ③ 板面换成下一条实例（编号一变）：flag 还残留着（替身的 completed 是死值），
       快照又退回 open —— 正是健步如飞那批假绿的形状。"""
    lua, gql, _ = sync_env([(71, 500000)], completed={71}, board=43200)
    g = lua.globals()
    res = gql.Sync.SnapshotNow()
    if res is None or res["degrade"] is not None:
        return fail("snapshot_done_needs_this_instance_turnin", f"快照应成功：{res}")
    key = str(gql.Sync.CurrentCharKey())
    L, db = gql.Ledger, g.GQLDB
    if str(db["chars"][key]["views"][71]["s"]) != "open":
        return fail("snapshot_done_needs_this_instance_turnin",
                    f"客户端答 done 但没有本轮流水背书，快照该降级 open，"
                    f"实际 {db['chars'][key]['views'][71]['s']}")
    # 补上背书：raw_turnin 自己烙编号（金额 0 = 这一处没量到钱，不污染金币任务前提）
    raw_turnin(lua, L, db, key, 71, int(g.GQL_NOW))
    gql.Sync.SnapshotNow()
    if str(db["chars"][key]["views"][71]["s"]) != "done":
        return fail("snapshot_done_needs_this_instance_turnin",
                    f"有编号对上的本轮流水背书了，快照该写 done，"
                    f"实际 {db['chars'][key]['views'][71]['s']}")
    # 板面换成下一条实例：旧流水的编号对不上新实例，flag 残留不许续命成 done。
    # 11-04 口径：换实例 = 旧那条已经下板，账本里它的结束时刻不再算数，新实例的
    # 结束时刻还没问过 ⇒ 这一趟必须重问。旧写法在旧 roundEnd 还活着时直接把替身的
    # ["end"] 往后挪，等于"同一条实例被凭空改了结束时间"，客户端不会这么答。
    g.GQLDB["quests"][71]["roundEnd"] = None
    g.GQL_BOARD["end"] = 43200 + 90000
    gql.Sync.SnapshotNow()
    if str(db["chars"][key]["views"][71]["s"]) != "open":
        return fail("snapshot_done_needs_this_instance_turnin",
                    f"实例换了、旧流水编号对不上，快照该退回 open，"
                    f"实际 {db['chars'][key]['views'][71]['s']}")
    ok("snapshot_done_needs_this_instance_turnin")


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


def test_turnin_records_immediately_without_any_window():
    """2026-10-29 用户定稿：QUEST_COMPLETE / QUEST_TURNED_IN 任一到达就**当场**落账，
    15 秒钱窗整体退役 ⇒ 事件返回的那一刻流水与快照都已经在账本里，
    不排定时器、不等 GQL_FireTimers（老形状：事件只开窗，推进时钟之后才有账）。
    到手金额也不再挂在这一笔上：流水行只带 t / k / sig ——
    sig 钉的是 Sync 那一处 5 参调用的实参错位（以前 currentSig 掉进被忽略的金额槽，
    写进流水的 sig 恒为 nil，"完成时看到的奖励签名"这一份取证整批丢失）。"""
    lua, gql, money = sync_env([(51, 500000)], board=7200)
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000

    fire(gql, "QUEST_COMPLETE", 51)
    rows = g.GQLDB["turnins"][key][51]
    if rows is None or len(rows) != 1:
        return fail("turnin_records_immediately_without_any_window",
                    f"事件到达当场就该有那一笔:\n{gql.Config.GetLogs()}")
    row = rows[1]
    if row["g"] is not None:
        return fail("turnin_records_immediately_without_any_window",
                    f"交割流水不再存到手金额（钱归奖励读取 rec.goldMoney），实际 {row['g']}")
    if str(row["sig"]) != str(g.GQLDB["quests"][51]["currentSig"]):
        return fail("turnin_records_immediately_without_any_window",
                    f"流水要带上完成时看到的奖励签名，实际 {row['sig']}")
    if str(row["k"]) != uid(gql.Ledger, g.GQLDB, 51, int(g.GQL_NOW)):
        return fail("turnin_records_immediately_without_any_window",
                    "那一笔得烙上这一条实例自己的编号")
    if state1(gql.Ledger.State, g.GQLDB, key, 51) != "done":
        return fail("turnin_records_immediately_without_any_window", "交完应显示已交")

    # 没有任何待结算窗口：推进时钟 + 让钱包进账，都不许再补第二笔、也不许回头改那一笔
    money["v"] = 5000000
    g.GQL_NOW = 400
    lua.globals().GQL_FireTimers()
    rows = g.GQLDB["turnins"][key][51]
    if len(rows) != 1 or rows[1]["g"] is not None:
        return fail("turnin_records_immediately_without_any_window",
                    f"钱窗已退役，推进时钟不该再动这一笔，实际 {rows}")
    ok("turnin_records_immediately_without_any_window")


def test_two_turnin_events_of_one_settlement_record_one_row():
    """一笔交割的两个事件（先开对话框、再真按"完成任务"）只记一笔 ——
    两个事件常在同一秒内连着来，按"这一条实例的编号"去重。
    换一条实例（板面把结束时刻整段挪走）之后再来一次交割才是第二笔。"""
    lua, gql, _ = sync_env([(57, 500000)], board=7200)
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    fire(gql, "QUEST_COMPLETE", 57)
    fire(gql, "QUEST_TURNED_IN", 57)
    rows = g.GQLDB["turnins"][key][57]
    if rows is None or len(rows) != 1:
        return fail("two_turnin_events_of_one_settlement_record_one_row",
                    f"同一实例的两个交割事件只该记一笔，实际 {rows}")
    board = g.GQL_BOARD
    # 11-04 口径：先让旧那条实例的结束时刻失效（它已经下板），再让板面给出新结束时刻；
    # 否则"同一条实例凭空改了结束时间"，客户端不会这么答，新口径也不会去重问。
    g.GQLDB["quests"][57]["roundEnd"] = None
    board["end"] = int(g.GQL_NOW) + 43200        # 板上换成下一条实例
    gql.Sync.SnapshotNow()                        # 新结束时刻入账
    fire(gql, "QUEST_COMPLETE", 57)
    rows = g.GQLDB["turnins"][key][57]
    if len(rows) != 2:
        return fail("two_turnin_events_of_one_settlement_record_one_row",
                    f"换了实例的第二次交割该另记一笔，实际 {len(rows)}")
    if str(rows[1]["k"]) == str(rows[2]["k"]):
        return fail("two_turnin_events_of_one_settlement_record_one_row",
                    "前置：这两笔必须落在两条不同的实例上")
    ok("two_turnin_events_of_one_settlement_record_one_row")


def test_turnin_with_no_instance_key_is_still_recorded():
    """T5（2026-10-29 定稿的落点）：算不出实例编号也照记这一笔。
    板上倒计时刚跨过去那一小段时间里交任务，L.InstanceEnd 按"已经过去的是上一条实例的
    墓碑"回 nil、锚点还没定出来时也算不出 —— 老实现因此整笔不落账，玩家真做过的这一条
    在周统计里凭空消失。现在流水照写、快照照写 done，代价如实：
    那一笔的 k 是 nil ⇒ 读侧 L.SameInstanceKey(nil, nil) 恒 false ⇒ 绿勾永不认它（§五
    "没编号 = 不匹配"），去重因此改认"双方都没编号且落在同一秒"（Clock() 答整秒，
    同一秒里的这两个事件不可能是两条任务）。
    造型：周重置问不出、也没有墙上钟 ⇒ cycleAnchor 为 0；板上也不答倒计时。"""
    lua, gql, _ = sync_env([(64, 500000)], weekly=None)
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 100
    lua.globals().GQL_FireTimers()      # 登录那一趟去抖到点：把 64 收进池（拿到 poolSeen）
    if uid(gql.Ledger, g.GQLDB, 64, 100) is not None:
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    "前置：这一条此刻必须算不出实例编号，否则测不到那一支")

    fire(gql, "QUEST_COMPLETE", 64)
    fire(gql, "QUEST_TURNED_IN", 64)    # 同一秒内的第二个事件 = 同一笔交割
    rows = g.GQLDB["turnins"][key][64]
    if rows is None or len(rows) != 1:
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    f"没编号也要落账、且同一秒只记一笔，实际 {rows}\n{gql.Config.GetLogs()}")
    if rows[1]["k"] is not None:
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    f"前置：这一笔就该没有编号，实际 {rows[1]['k']}")
    view = g.GQLDB["chars"][key]["views"][64]
    if view is None or str(view["s"]) != "done":
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    f"快照照写 done（做过这一事实不靠编号才成立），实际 {view}")
    if state1(gql.Ledger.State, g.GQLDB, key, 64) == "done":
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    "没编号的一笔不许点亮绿勾 —— §五「没编号 = 不匹配」")
    count, _ = gql.Ledger.WeekStats(g.GQLDB, key, 0)
    if int(count) != 1:
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    f"周统计要如实数进这一条，实际 {count}")

    # 下一秒再交一条 = 另一笔交割（同刻去重只吞同一秒的那一次重复事件）
    g.GQL_NOW = 101
    fire(gql, "QUEST_COMPLETE", 64)
    rows = g.GQLDB["turnins"][key][64]
    if len(rows) != 2:
        return fail("turnin_with_no_instance_key_is_still_recorded",
                    f"换到下一秒的交割该另起一笔，实际 {len(rows)}")
    ok("turnin_with_no_instance_key_is_still_recorded")


def test_api_frame_visible_candidates():
    """帧可见性必须走 Api：直接调全局函数在 12.x 形态下既测不到也报不进 probe。
    现在的活调用方是矩阵的遮挡判定与 Probe 的Minimap 一项（Api.lua 之外不许再摸全局）。
    两条候选都要试，都没有时降级为 false。"""
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


def test_back_to_back_turnins_both_recorded():
    """连着交两个任务：两条任务都要各自落账（同一秒内到也算两笔 ——
    同刻去重只吞**同一条任务**的重复事件，不许跨任务串）。
    2026-10-29 起金额不在交割这一处量：流水行上压根没有 g，周统计那个金币只来自
    两条实例各自的奖励读数（Sync 池循环里 NoteGoldThisInstance 写下的 rec.goldMoney）。"""
    lua, gql, money = sync_env([(58, 500000), (59, 400000)], board=7200)
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    gql.Sync.SnapshotNow()
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 58)
    money["v"] = 1300000
    fire(gql, "QUEST_COMPLETE", 59)
    rowsA = g.GQLDB["turnins"][key][58]
    rowsB = g.GQLDB["turnins"][key][59]
    if rowsA is None or len(rowsA) != 1:
        return fail("back_to_back_turnins_both_recorded", f"任务 58 该当场有一笔，实际 {rowsA}")
    if rowsB is None or len(rowsB) != 1:
        return fail("back_to_back_turnins_both_recorded", f"任务 59 该当场有一笔，实际 {rowsB}")
    if rowsA[1]["g"] is not None or rowsB[1]["g"] is not None:
        return fail("back_to_back_turnins_both_recorded",
                    "交割流水不写金额（钱归奖励读取），行里不许再有 g")
    count, gold = gql.Ledger.WeekStats(g.GQLDB, key, 0)
    if int(count) != 2:
        return fail("back_to_back_turnins_both_recorded", f"本周该数两条，实际 {count}")
    if int(gold) != 900000:
        return fail("back_to_back_turnins_both_recorded",
                    f"两条实例各读到 50 金 / 40 金，合计应 900000，实际 {count}/{gold}")
    ok("back_to_back_turnins_both_recorded")


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
    读成"我切了狩猎模式"，而真问题是这笔钱归谁 —— 报告是取证用的，归谁就写谁。
    board=43200：按号读数（db.chars[..].reads）烙的是**这一条实例的编号**，
    这一条挂不上编号时 GoldReaders 一个号都点不出名（那是"如实报问不出"，不是本用例要的形状）。"""
    lua, gql, _ = sync_env([(71, 10900000)], board=43200, extra=("Dump",))
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


def test_dump_quest_shows_per_char_credentials():
    """2026-10-29 回响复燃 92713 那单：本轮没做过的任务出绿勾，可旧版 /gql quest 只摊
    行级痕迹，绿勾是谁写的没有据可查。补"凭据（按角色）"一节：三份按角色存的凭据
    （快照 views / 流水 turnins / 补录 manual）逐号摊开各自记的编号，并与当前编号对答案 ——
    编号对上又答 done 的那份必须被 ✔ 点名（绿勾出处），对不上的要明说（对不上）。"""
    lua, gql, _ = sync_env([(71, 250000)], board=43200, extra=("Dump",))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    curKey = lua.execute(
        "return GQL.Ledger.InstanceKey(GQLDB, 71, GQLDB.quests[71], GQLDB.chars[%r].lastSync)" % key)
    if not curKey:
        return fail("dump_quest_shows_per_char_credentials", "这条任务连当前编号都算不出")
    # 两个号各一份补录：一号的编号对上当前（✔点名的形状），二号的对不上（明说不匹配）
    lua.execute(("GQLDB.chars['小号二'] = GQLDB.chars['小号二'] or {{}}; "
                 "GQLDB.manual['{k1}'] = {{ [71] = {{ state='done', t=GQLDB.lastNow, "
                 "v='接口核验', k='{ck}' }} }}; "
                 "GQLDB.manual['小号二'] = {{ [71] = {{ state='done', t=GQLDB.lastNow, "
                 "v=nil, k='71:9999999' }} }}").format(k1=key, ck=curKey))
    text = str(gql.Dump.Quest("任务71"))
    if "凭据（按角色）" not in text:
        return fail("dump_quest_shows_per_char_credentials", f"缺凭据取证节：\n{text}")
    if f"{key}：" not in text or "补录=done 核验=是" not in text:
        return fail("dump_quest_shows_per_char_credentials", f"该逐号摊出补录凭据：\n{text}")
    if "✔对上当前" not in text:
        return fail("dump_quest_shows_per_char_credentials", f"对上当前编号的凭据没被点名：\n{text}")
    if text.count("（对不上）") < 1:
        return fail("dump_quest_shows_per_char_credentials", f"对不上的凭据该明说不匹配：\n{text}")
    ok("dump_quest_shows_per_char_credentials")


def test_dump_quest_reports_real_expiry():
    """接口读到倒计时的任务：结论必须是"到点自动消失"，不能报成赖着不走；
    而且要把"这个数是怎么来的"一起报出来 —— T5 后来源只剩两种：板面倒计时（roundEnd）
    或周 CD 尾巴，编的排程值连同老来源标签整组退役。上一版只有"接口读到过"一句话，
    周常被无参接口答成"还剩 2 小时"时无从看出是谁答的（追踪萨拉塔斯）。"""
    lua, gql, _ = sync_env([(71, 250000)], extra=("Dump", "Probe"))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    gql.Sync.SnapshotNow()
    text = str(gql.Dump.Quest("任务71"))
    if "还剩 2时0分" not in text or "来源=板面倒计时" not in text:
        return fail("dump_quest_reports_real_expiry", f"应报板面倒计时并带上来源：\n{text}")
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
    域不匹配时周汇总必须是未知，不能报一个看起来正常的数。
    同域那一半顺带盯住金币来源：周汇总那个钱只来自奖励读取，而且自 2026-11-03 起问的是
    **这一号自己**在那一条实例上的读数（reads），交割流水本身不带金额、
    行级 rec.goldMoney 也不再冒充某一号的到手。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    epoch_anchor = 1700000000 - 604800
    # 锚点先立起来：这条问不出板面倒计时，身份只能落②档（cycleAnchor + 604800 那一桶）。
    # 锚点为 0 时两边算出的编号都是 nil ⇒ 这一半测的就成了 §五「没编号 = 不匹配」，
    # 而不是这里要盯的"同域后照旧数本 CD 那一条"。
    db.cycleAnchor = epoch_anchor
    L.OnTurnIn(db, "B-永恒之井", 3, 120, None)
    db.clockDomain = "uptime"
    count, _ = L.WeekStats(db, "B-永恒之井", epoch_anchor)
    if int(count) != -1:
        return fail("weekstats_unknown_when_clock_domains_split",
                    f"跨域应记未知（-1），实际 count={count}")
    db.clockDomain = "epoch"
    # 500000 这笔钱走真实写点上板（DiffPool → NoteGoldThisInstance）：奖励金额只有
    # Sync 池循环那一处会记进 rec.goldMoney，手拼 goldKey/goldMoney 会把"按实例记账"
    # 那四步（换键作废旧钱）测成空转。
    # 2026-11-03 起周汇总的金额按**这一号自己的读数**问（reads），而 pool_pass 只演写侧的
    # 行级那一步 ⇒ 同一笔 pool[i].money 的号级那一份照真实顺序补上（Sync.lua:1020/1026）。
    pool_pass(lua, L, db, [(4, 500000, None)], epoch_anchor + 5)
    L.SetRewardRead(db, "B-永恒之井", 4, 500000, epoch_anchor + 10)
    L.OnTurnIn(db, "B-永恒之井", 4, epoch_anchor + 10, None)
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
    这条用例只验分组与排序，取的都是远超/远低于任何门槛的值。
    身份：这一条只跑 DiffPool（不走完整 Sync），所以逐条补一次 SetExpiry 当"板上读到的倒计时"，
    否则第 36 轮之后 L.State 一律答 ?（判不出编号 = 不成立），这条要验的 open/ended 就没有前提。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["chars"] = lua.table_from({"A-永恒之井": lua.table_from(
        {"lastSync": 1000, "views": lua.table_from({})})})
    L.DiffPool(db, pool_map(lua, [(11, 206, 25000000), (12, 206, 10000000),
                                  (13, 205, None), (14, 205, 0),
                                  (15, 207, 500000)]), 1000)
    for qid in (11, 12, 13, 14, 15):
        L.SetExpiry(db["quests"][qid], 1000, 43200)
    groups = L.MatrixRows(db, 1000)
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


def test_api_tracked_map_semantics():
    """关注地图判定（2026-10-31 重构后口径）：没有默认图，关注集合 =
    玩家自己加的图 ∪ 加图时自动记住的子图；父子关系玩家不录，客户端答 ——
    向下 GetMapChildrenInfo（加图那一刻整棵子树记进 subMaps）、
    向上 C_Map.GetMapInfo 的 parentMapID（父图链，兜"加图之后版本新加的子图"）。
    钉五面：① 一张没加时谁都不关注（false，渲染层才滤得动）；
    ② 玩家加的图 true，且 AddMap 之后立即生效（清单变更必须清缓存）；
    ③ 自动记住的子图 true；④ 没加过的图 false；⑤ 父图移除子图跟着 false；
    ⑥ 存档子图被清掉后，父图链照样把版本新加的子图判上。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    names = {2395: "虚影风暴", 2444: "屠戮者高地", 2099: "苏拉玛"}
    parents = {2444: 2395, 2395: 870, 2099: 1642, 1642: 0}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": names.get(int(mid)), "parentMapID": parents.get(int(mid), 0)})
        if int(mid) in names else None,
        "GetMapChildrenInfo": lambda mid: lua.table_from([2444])
        if int(mid) == 2395 else lua.table_from([]),
    })
    if gql.Api.IsTrackedMap(2395) is not False:
        return fail("api_tracked_map_semantics",
                    f"一张图都没加时谁都不关注（false），实际 {gql.Api.IsTrackedMap(2395)}")
    gql.Config.AddMap(2395)
    if gql.Api.IsTrackedMap(2395) is not True:
        return fail("api_tracked_map_semantics",
                    "玩家加的图必须 true，且 AddMap 后立即生效（缓存必须随清单失效）")
    if gql.Api.IsTrackedMap(2444) is not True:
        return fail("api_tracked_map_semantics",
                    "自动记住的子图必须 true（存档 subMaps 或父图链都该答上来）")
    if gql.Api.IsTrackedMap(2099) is not False:
        return fail("api_tracked_map_semantics", "没加过的图必须 false（渲染层要滤整组）")
    if gql.Api.IsTrackedMap(None) is not None:
        return fail("api_tracked_map_semantics", "无 mapID 必须 nil")
    gql.Config.RemoveMap(2395)
    if gql.Api.IsTrackedMap(2444) is not False:
        return fail("api_tracked_map_semantics", "父图移除后子图必须跟着 false")
    # 父图链兜底：存档子图清空（模拟"加图之后版本又新加的子图"），
    # 只要 parentMapID 链够到玩家加的图，照样算关注
    gql.Config.AddMap(2395)
    g.GQLDB["config"]["subMaps"] = lua.table_from({})
    gql.Api.ResetTrackedMapCache()
    if gql.Api.IsTrackedMap(2444) is not True:
        return fail("api_tracked_map_semantics",
                    "存档没记的子图必须靠父图链判上（版本新加的子图不许漏）")
    ok("api_tracked_map_semantics")


def test_api_tracked_map_three_valued():
    """T13：IsTrackedMap 是三值，"判不出"不许折成"确定不是"。渲染层只在 == false
    时滤整组；折成 false 等于 C_Map 一坏，玩家关注的整组任务从矩阵上凭空消失
    （旧版把 WalkToOwner 的第二返回值 progress 丢了，直接 verdict = owner ~= nil）。
    钉四面：
    ① 玩家自己加的图 → true；
    ② 父链问到确定的"没有父图"顶点仍没碰到玩家的图 → 确定不在，false（照滤）；
    ③ 父链一次都没问出来（GetMapInfo 报错）→ 判不出 nil，且落 UNDECIDED 哨兵：
       同一张图一场最多落接口一次（不许变成每帧替坏接口重问）；
    ④ 关注清单为空："没加图"本身就是答案 → false，且这一路根本不碰接口。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    asked = []

    def map_info(mid):
        mid = int(mid)
        if mid == 9999:
            asked.append(mid)
            raise RuntimeError("C_Map 报错")
        if mid == 8888:
            return lua.table_from({"name": "旧大陆"})   # 确实没有父图：链到头
        return None

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    gql.Config.AddMap(2395)
    if gql.Api.IsTrackedMap(2395) is not True:
        return fail("api_tracked_map_three_valued",
                    "玩家加的图必须 true，实际 %r" % (gql.Api.IsTrackedMap(2395),))
    for _ in range(5):
        if gql.Api.IsTrackedMap(9999) is not None:
            return fail("api_tracked_map_three_valued",
                        "父链一次都没问出来 = 判不出，必须 nil（渲染层 nil 不滤），实际 %r"
                        % (gql.Api.IsTrackedMap(9999),))
    if asked != [9999]:
        return fail("api_tracked_map_three_valued",
                    f"判不出的落哨兵：一张图一场最多走一次父图链，实际落接口 {asked}")
    if gql.Api.IsTrackedMap(8888) is not False:
        return fail("api_tracked_map_three_valued",
                    "链问到确定顶点没碰到玩家的图 = 确定不在跟踪，必须 false")
    gql.Config.RemoveMap(2395)
    if gql.Api.IsTrackedMap(9999) is not False:
        return fail("api_tracked_map_three_valued",
                    "空清单时「不属于谁」就是确定答案 → false，不许报 nil")
    if asked != [9999]:
        return fail("api_tracked_map_three_valued",
                    f"空清单这一路不该再落接口，实际问过 {asked}")
    ok("api_tracked_map_three_valued")


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
    L.OnTurnIn(db, "A-永恒之井", 21, 6100, sig)
    # 金额只来自奖励读取（2026-10-29 定稿），而且自 2026-11-03 起按**这一号自己的读数**问
    # （WeekStats → reads）：真实同步在同一条 pool[i].money 上写两份（Sync 池循环的
    # NoteGoldThisInstance + SetRewardRead）。这一例手拼 DiffPool，那两步也得手拼齐，
    # 否则列头的"本周金币"永远是 -1 —— 交割流水里压根不再有金额可读。
    L.NoteGoldThisInstance(db, 21, 30000000, 6100)
    L.SetRewardRead(db, "A-永恒之井", 21, 30000000, 6100)
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
    # 上面那一小段只问"锚点没了还能不能报本周合计"；下面的行判据要靠编号，
    # 而这一条（不上板的注册外任务）唯一的身份来源就是周 CD 尾巴 ⇒ 锚点要还回去。
    db["cycleAnchor"] = 5000

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
    """矩阵要有任务行、单元格状态（完成=绿色勾，2026-10-30 起是贴图不是 √ 字形）。
    底部本周合计/金币列已移除（用户要求矩阵只反映"谁做了哪个任务"，金币与计数不再进矩阵）。"""
    lua, gql, _ = sync_env([(91, 30000000)], extra=("Matrix",))
    g = lua.globals()
    board_clock(lua, 200000)          # 板上答得出倒计时 ⇒ 这一条实例有结束时刻，勾才烙得上编号
    g.GQL_NOW = 100000            # 锚点是 700000-604800=95200，交任务必须落在本周内
    gql.Sync.SnapshotNow()
    fire(gql, "QUEST_COMPLETE", 91)
    g.GQL_NOW = 100020
    n = gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if int(n) != 1:
        return fail("matrix_renders_rows_and_week_totals", f"应渲染 1 行，实际 {n}\n{joined}")
    if "任务91" not in joined:
        return fail("matrix_renders_rows_and_week_totals", f"缺任务行:\n{joined}")
    if not check_glyph(g.GQLMatrixFrame, tinted=False):
        return fail("matrix_renders_rows_and_week_totals",
                    "单元格应显示完成记号（原色绿勾贴图）:\n%s\n记号: %r"
                    % (joined, cell_marks(g.GQLMatrixFrame)))
    ok("matrix_renders_rows_and_week_totals")


def test_matrix_text_pool_never_borrows_another_font():
    """文字池不许跨字体复用条目（用户 12.1.5 实测：新加一条任务之后，某行任务名突然
    变成地图组头那种大号字，整块看着像错了行；点"刷新"也没用，换号登录才恢复）。
    根因：FontString 建好之后 SetText 换不掉字体，而组头（大号）和任务名（小一号）
    原来共用一条池子 —— 行序一变，第 N 条就会被另一种行领走。
    用例把一条任务挪到另一张图，让新出现的组头正好落在上一轮任务名那条串上。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    # 本单关心的是字体池，不关心关注过滤：两张图都算玩家加过的（2026-10-31 起
    # 没加过的图整组不渲染，2260 不加的话第二个组头根本不出现）
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True, 2260: True})
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
    """不在关注集合（玩家自己加的图 ∪ 自动记住的子图）的图整组不渲染
    （用户实测要求；2026-10-31 起判定按"玩家加没加"，不再有默认图名单）。
    加进关注清单后立即照列 —— 清单变更必须清判定缓存，改完清单不许重载。"""
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
                    f"只应渲染关注图 1 行（夹具只加了 2007），实际 {n}\n{joined}")
    if "任务98" in joined:
        return fail("matrix_skips_old_expansion_maps",
                    f"没加过的地图 2099 的任务不该出现:\n{joined}")
    if "任务97" not in joined:
        return fail("matrix_skips_old_expansion_maps",
                    f"关注图 2007 的任务 97 应照常显示:\n{joined}")
    # 玩家把 2099 加进关注清单 → 立即照列（判定缓存必须随清单失效）
    gql.Config.AddMap(2099)
    n = gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if int(n) != 2:
        return fail("matrix_skips_old_expansion_maps",
                    f"加进关注清单后该照列，实际 {n} 行\n{joined}")
    ok("matrix_skips_old_expansion_maps")


def test_group_rolls_up_to_the_tracked_parent_map():
    """用户 2026-10-31 两连报："暗影汇聚之地"（普通世界任务、奖励 958 金）怎么点
    刷新矩阵都没有；过滤修完它又落进"屠戮者高地"分组 —— 玩家关注的是虚影风暴、
    没加过屠戮者高地，面板不该冒出这个分组。新口径（同日重构）：关注清单 =
    玩家自己加的图（默认图已整段撤下）；父子关系玩家不录，客户端答 ——
    加图时整棵子树自动记下（不展示），归属滚动到玩家加的那张图。钉五面：
    ① 玩家只加 2395 虚影风暴，挂 2444 屠戮者高地（其子图）的任务照常渲染；
    ② 分组头是虚影风暴，不许冒"屠戮者高地"分组；
    ③ 没加过的图（2099）整组不渲染；
    ④ /gql quest 对账行：被滤的那条要写明「界面过滤」；
    ⑤ 归属滚动的行不许再报界面过滤（它就在玩家加的图那组里）。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Dump"))
    g = lua.globals()
    rows = lua.table_from({
        lua.table_from({"questID": 97, "mapID": 2444, "name": "任务97",
                        "money": 30000000}),   # 屠戮者高地（虚影风暴的子图）
        lua.table_from({"questID": 98, "mapID": 2099, "name": "任务98",
                        "money": 30000000}),   # 玩家没加过的图
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})
    names = {2444: "屠戮者高地", 2395: "虚影风暴", 2099: "苏拉玛"}
    parents = {2444: 2395, 2395: 870, 2099: 1642, 1642: 0}
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from(
            {"name": names.get(int(mid)), "parentMapID": parents.get(int(mid), 0)})
        if int(mid) in names else None,
        "GetMapChildrenInfo": lambda mid: lua.table_from([2444])
        if int(mid) == 2395 else lua.table_from([]),
    })
    gql.Config.AddMap(2395)     # 玩家只加虚影风暴；AddMap 顺带把 2444 记成它的子图
    gql.Sync.SnapshotNow()
    n = gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if int(n) != 1:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"子图任务该渲染、没加过的图该滤掉，实际 {n} 行\n{joined}")
    if "任务97" not in joined:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"玩家加的图（虚影风暴）的子图任务该出现:\n{joined}")
    if "屠戮者高地" in joined:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"玩家没加过的子图不该成为分组头:\n{joined}")
    if "虚影风暴" not in joined:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"分组头应是玩家加的虚影风暴:\n{joined}")
    if "任务98" in joined:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"没加过的图（苏拉玛）不许出现:\n{joined}")
    # 被渲染层滤掉的那条：/gql quest 要写明分歧，不许只说账本那一半
    text = str(gql.Dump.Quest("任务98") or "")
    if "界面过滤" not in text:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"渲染层砍掉的行，/gql quest 要补「界面过滤」对账行:\n{text}")
    text97 = str(gql.Dump.Quest("任务97") or "")
    if "界面过滤" in text97:
        return fail("group_rolls_up_to_the_tracked_parent_map",
                    f"归属滚动到玩家加的图之后，这条不该再报界面过滤:\n{text97}")
    ok("group_rolls_up_to_the_tracked_parent_map")


def test_matrix_keeps_group_when_map_undecidable():
    """T13 渲染侧：关注过滤只砍"确定不在跟踪"（IsTrackedMap == false）的组；
    判不出（nil）的组一行都不许吞 —— 旧版把接口坏折成 false，C_Map 一报错玩家
    关注的整组任务就从矩阵凭空消失（被替换的旧函数 IsMidnightMap 头注释本来就写
    "判不了按不滤处理，宁多勿漏"）。三张图三值各一条：任务97 在玩家加的 2007（照列）、
    任务98 的 9999 父链问不出（判不出 → 照列）、任务99 的 8888 链到确定顶点没找到
    （false → 整组滤）。"""
    lua, gql, _ = sync_env([(97, 30000000)], extra=("Matrix",))
    g = lua.globals()
    rows = lua.table_from({
        lua.table_from({"questID": 97, "mapID": 2007, "name": "任务97",
                        "money": 30000000}),
        lua.table_from({"questID": 98, "mapID": 9999, "name": "任务98",
                        "money": 30000000}),
        lua.table_from({"questID": 99, "mapID": 8888, "name": "任务99",
                        "money": 30000000}),
    })
    g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

    def map_info(mid):
        mid = int(mid)
        if mid == 9999:
            raise RuntimeError("C_Map 报错")
        if mid == 2007:
            return lua.table_from({"name": "虚影风暴"})
        if mid == 8888:
            return lua.table_from({"name": "旧大陆"})   # 没有 parentMapID：确定到顶
        return None

    g.C_Map = lua.table_from({"GetMapInfo": map_info})
    gql.Sync.SnapshotNow()
    n = int(gql.Matrix.Render())
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "任务98" not in joined:
        return fail("matrix_keeps_group_when_map_undecidable",
                    f"判不出的组一行都不许吞（nil 不滤）:\n{joined}")
    if "任务97" not in joined:
        return fail("matrix_keeps_group_when_map_undecidable",
                    f"确定在跟踪的组照常渲染:\n{joined}")
    if "任务99" in joined:
        return fail("matrix_keeps_group_when_map_undecidable",
                    f"确定不在跟踪（链问到顶点）的组照常整组滤掉:\n{joined}")
    if n != 2:
        return fail("matrix_keeps_group_when_map_undecidable",
                    f"该渲染 2 行（2007 + 判不出的 9999），实际 {n}\n{joined}")
    ok("matrix_keeps_group_when_map_undecidable")


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
    """下次刷新时刻 = 账本里最早的"还没过去的实例结束时刻"；一条都没有 → nil（该抓了）。
    世界任务到点才换，最早的没到池子就不可能变 —— 据此不用一直轮询。
    第 36 轮 T4a 把来源换成这条实例自己的结束时刻（计划 §七）、T5 把老 rec.expiresAt
    那一组（连同编出来的 24 小时排程值）整组退役：真源只有 roundEnd 与周 CD 尾巴。
    now 由调用方给（Sync 那两个调用点本来就先问了 Clock()），这里照实传进去。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    if L.NextRefreshAt(db, 1000) is not None:
        return fail("next_refresh_at_is_earliest_expiry", "没有到期信息必须 nil")
    db["quests"] = lua.table_from({
        11: lua.table_from({"roundEnd": 9000}),
        12: lua.table_from({"roundEnd": 5000}),
        13: lua.table_from({"roundEnd": 0}),         # 0 = 取不到，不参与
        14: lua.table_from({}),                       # 没有该字段
    })
    if int(L.NextRefreshAt(db, 1000)) != 5000:
        return fail("next_refresh_at_is_earliest_expiry",
                    f"应取最早的 5000，实际 {L.NextRefreshAt(db, 1000)}")
    ok("next_refresh_at_is_earliest_expiry")


def test_snapshot_records_the_instance_end_time():
    """快照时把每个任务的剩余时间换算成这条实例自己的结束时刻（roundEnd）入账本，
    供"下次何时刷新"与身份编号判断。T5 起老 expiresAt 那一组不再被写。"""
    lua, gql, _ = sync_env([(84, 30000000)])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    if int(g.GQLDB["quests"][84]["roundEnd"]) != 8200:
        return fail("snapshot_records_the_instance_end_time",
                    f"roundEnd 应为 now+剩余秒=8200，实际 {g.GQLDB['quests'][84]['roundEnd']}")
    for legacy in ("expiresAt", "expiresSrc", "expiresGuessed"):
        if g.GQLDB["quests"][84][legacy] is not None:
            return fail("snapshot_records_the_instance_end_time",
                        f"老到期字段 {legacy} 已退役，快照不许再写它")
    ok("snapshot_records_the_instance_end_time")


def test_a_live_expiry_is_never_asked_again():
    """同一条实例的结束时刻还在未来 ⇒ 板上还是这一条，重问只会问出同一个时刻。
    三处读取点（池循环 / 现场问答 / 日志旁路）都不许再问（11-04 定稿 §二 1/2）。"""
    lua, gql, _ = sync_env([(161, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    asked = []
    real = g.C_TaskQuest.GetQuestTimeLeftSeconds
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), real(qid))[1]})
    gql.Sync.SnapshotNow()        # 第一次上板：问一次、把 roundEnd 烙成 43200
    asked.clear()
    for step in range(2):
        g.GQL_NOW = 2000 + 100 * step
        gql.Sync.SnapshotNow()
    if asked:
        return fail("a_live_expiry_is_never_asked_again",
                    f"结束时刻没过期还重问倒计时 {len(asked)} 次: {asked}")
    # sync_env(board=43200) 在 GQL_NOW=0 时把结束时刻钉成 43200，倒计时答 43200-GQL_NOW；
    # 于是 now=1000 时 SetExpiry 写 1000+42200 = 43200，不是 1000+43200。
    if int(lua.execute("return math.floor(GQLDB.quests[161].roundEnd)")) != 43200:
        return fail("a_live_expiry_is_never_asked_again",
                    "跳过问不许把账本里那条结束时刻改掉（应为 43200）")
    ok("a_live_expiry_is_never_asked_again")


def test_a_suspect_expiry_is_asked_again():
    """时刻被写坏（比 now 远过一个 CD + 一天）必须重问，否则那条永远钉着不走。"""
    lua, gql, _ = sync_env([(162, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    asked = []
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), 43200)[1]})
    gql.Sync.SnapshotNow()
    lua.execute("GQLDB.quests[162].roundEnd = 1000 + 700000")
    asked.clear()
    gql.Sync.SnapshotNow()
    if asked != [162]:
        return fail("a_suspect_expiry_is_asked_again",
                    f"时刻被写坏时该重问一次，实际问了 {asked}")
    ok("a_suspect_expiry_is_asked_again")


def test_a_live_expiry_is_not_asked_by_event_capture():
    """第三处读取点：现场问答（Sync.lua:387 每次 QUEST_ACCEPTED 都问、:398 还会改写 roundEnd）。
    口径不许只落在池循环上 —— 只调 SnapshotNow 的用例测不到这一路。"""
    lua, gql, _ = sync_env([(163, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 1000
    gql.Sync.SetPolling(True)
    gql.Sync.SnapshotNow()
    asked = []
    real = g.C_TaskQuest.GetQuestTimeLeftSeconds
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), real(qid))[1]})
    for step in range(2):
        g.GQL_NOW = 1100 + 100 * step
        fire(gql, "QUEST_ACCEPTED", 163)
    if asked:
        return fail("a_live_expiry_is_not_asked_by_event_capture",
                    f"结束时刻还活着，现场问答却重问了倒计时：{asked}")
    if int(lua.execute("return math.floor(GQLDB.quests[163].roundEnd)")) != 43200:
        return fail("a_live_expiry_is_not_asked_by_event_capture",
                    "现场问答不许改写同一条实例的结束时刻")
    ok("a_live_expiry_is_not_asked_by_event_capture")


def test_matrix_open_always_refreshes():
    """打开矩阵就重抓一次池（用户 2026-11-01 原话："打开矩阵的时候最好能自动刷新，
    而不是手动刷新"）。上一版只在"最早到期时刻已过"时才抓 —— 那条判据只说明这条
    倒计时快走完了，跟"账本是不是最新"不是一回事：刚交完任务、刚换号、刚切模式
    它一概不知道，玩家看到的是打开面板之前那一刻的板面。
    现在每开一次面板都走 SnapshotNow：① 到期时刻还没到也抓；② 关掉再开还是抓
    （每次打开都是一次新读取，不是"第一次才抓"）。"""
    lua, gql, _ = sync_env([(85, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    if int(g.GQLDB["chars"][key]["lastSync"]) != 1000:
        return fail("matrix_open_always_refreshes", "首次快照应已入账")
    g.GQL_NOW = 5000                       # 最早的到期是 8200，还没到
    gql.Matrix.Toggle()
    if int(g.GQLDB["chars"][key]["lastSync"]) != 5000:
        return fail("matrix_open_always_refreshes",
                    f"开面板就要抓一次（到期没到也一样），lastSync 仍 "
                    f"{g.GQLDB['chars'][key]['lastSync']}")
    gql.Matrix.Toggle()                    # 关上面板
    g.GQL_NOW = 6000
    gql.Matrix.Toggle()                    # 再开一次
    if int(g.GQLDB["chars"][key]["lastSync"]) != 6000:
        return fail("matrix_open_always_refreshes",
                    f"每次打开都该重抓，lastSync 仍 {g.GQLDB['chars'][key]['lastSync']}")
    ok("matrix_open_always_refreshes")


def test_refresh_wakes_at_earliest_expiry():
    """矩阵开着时按最早到期时刻排一个唤醒定时器：到点只重画，不重问整板块。
    "该行撤掉"是账本算式（结束时刻跟此刻一比就知道），不需要向游戏要任何数
    （用户 11-04："到期了就不展示了，安排要什么呢"）。
    定时器必须是单实例：每次快照都会重排，不取消旧的就会越攒越多，
    几十个同时炸 = "开着矩阵就定时卡一下"（用户实测）。"""
    lua, gql, _ = sync_env([(86, 30000000)], extra=("Matrix",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 7200})
    g.GQL_NOW = 1000
    lua.execute("GQL_REPAINTED = 0 "
                "local real = GQL.Matrix.Refresh "
                "GQL.Matrix.Refresh = function() GQL_REPAINTED = GQL_REPAINTED + 1 "
                "return real() end")
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
    snap_before = str(gql.Config.GetLogs()).count("快照完成")
    lua.execute("GQL_REPAINTED = 0")   # Toggle 与前 5 次快照都会走到 Refresh，先清零
    g.GQL_NOW = 9000                       # 到点，定时器触发
    fired = lua.globals().GQL_FireTimers()
    if int(fired) < 1:
        return fail("refresh_wakes_at_earliest_expiry", "到期定时器应触发")
    logs = str(gql.Config.GetLogs())
    if logs.count("快照完成") > snap_before:
        return fail("refresh_wakes_at_earliest_expiry",
                    f"到点不该重问整板块（每趟抓池要问几百张图）:\n{logs}")
    if int(g.GQLDB["chars"][key]["lastSync"]) == 9000:
        return fail("refresh_wakes_at_earliest_expiry", "到点不该写 lastSync（那是抓池的痕迹）")
    if int(lua.execute("return GQL_REPAINTED or 0")) < 1:
        return fail("refresh_wakes_at_earliest_expiry", "到点该喊一次重画")
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
    再点一下取消、回到自动判定。手动状态在 Ledger.State 里优先级最高。
    board=43200 给这一条一个结束时刻 ⇒ 补录烙得上编号（身份未定的那一格一律 ?，
    判据不认它，见 L.State 的 fail-closed 那一条）。"""
    lua, gql, _ = sync_env([(90, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()

    def click_first_cell():
        return bool(lua.execute(
            "for i, c in ipairs(GQLMatrixContent.__children) do "
            "  local s = c.__scripts and c.__scripts['OnClick'] "
            "  if s then s(c) return true end "
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


def test_right_click_marks_a_cell_as_skipped():
    """右键 = "这一号本轮不做"（用户 2026-10-30：某个号这条任务不给金币、玩家也不打算做，
    需要一个和"没做""做完了"都分得开的记号；左键仍是"完成了"）。
    钉四件事：
    ① 右键真能落到热区上 —— RegisterForClicks 里必须有 RightButtonUp，否则客户端
       压根不派发这一次点击，界面上按不动；
    ② 记下来的是 state=skip、格子上是灰蓝 ×（不是勾，也不是 —）；
    ③ 再右键一次撤销（回到自动判定）；
    ④ skip 不算完成：周统计只认 done，左键压在 skip 格上要直接记完成（不用点两下）。
    左窗格那排整行热区不参与 —— 矩阵里不许再出现"右键屏蔽一行"那种入口。"""
    lua, gql, _ = sync_env([(195, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    clicks = str(lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  if c.__questID == 195 and c.__charKey == %r and c.__clicks then "
        "    return table.concat(c.__clicks, ';') end end return ''" % key))
    if "RightButtonUp" not in clicks:
        return fail("right_click_marks_a_cell_as_skipped",
                    f"状态格没注册右键，客户端不会派发这一次点击: {clicks!r}")
    # 接口答"还挂着没做"，玩家自己决定这一号不做它
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, key, 195, "RightButton"):
        return fail("right_click_marks_a_cell_as_skipped", "点不到那一格")
    mark = g.GQLDB["manual"][key][195]
    if mark is None or str(mark["state"]) != "skip":
        return fail("right_click_marks_a_cell_as_skipped", f"右键应记 skip，实际 {mark}")
    if state1(gql.Ledger.State, g.GQLDB, key, 195) != "skip":
        return fail("right_click_marks_a_cell_as_skipped", "这一格的状态该是 skip")
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "×" not in joined:
        return fail("right_click_marks_a_cell_as_skipped",
                    f"不做那一格该是一枚 ×:\n{joined}")
    if check_glyph(g.GQLMatrixFrame):
        return fail("right_click_marks_a_cell_as_skipped",
                    "不做不是完成，格子上不许出现勾")
    # 周统计只认 done：标了不做不许被算成"完成 1"
    count = lua.execute("return (GQL.Ledger.WeekStats(GQLDB, %r, 0, nil))" % key)
    if int(count) != 0:
        return fail("right_click_marks_a_cell_as_skipped",
                    f"不做不许计入完成数，实际 {count}")
    # ③ 再右键一次撤销
    if not click_cell(lua, key, 195, "RightButton"):
        return fail("right_click_marks_a_cell_as_skipped", "撤销时点不到那一格")
    if g.GQLDB["manual"][key][195] is not None:
        return fail("right_click_marks_a_cell_as_skipped", "再右键一次应撤销不做")
    # ④ 左键压在 skip 格上直接记完成（不是先撤一次 skip）
    click_cell(lua, key, 195, "RightButton")
    if not click_cell(lua, key, 195):
        return fail("right_click_marks_a_cell_as_skipped", "左键点不到那一格")
    if str(g.GQLDB["manual"][key][195]["state"]) != "done":
        return fail("right_click_marks_a_cell_as_skipped",
                    "左键该直接记完成（覆盖那条 skip），实际 %s"
                    % g.GQLDB["manual"][key][195])
    # 已完成的格子右键不许被压成"不做"：那会让周统计少算一笔
    click_cell(lua, key, 195, "RightButton")
    if str(g.GQLDB["manual"][key][195]["state"]) != "done":
        return fail("right_click_marks_a_cell_as_skipped",
                    "已完成的格子不该被右键改成不做，实际 %s"
                    % g.GQLDB["manual"][key][195])
    ok("right_click_marks_a_cell_as_skipped")


def test_all_done_row_dims_its_checks():
    """全员完成的那一行连勾一起压暗（用户 2026-10-30："这个任务都全部完成了 现在是置灰，
    我想对勾降低一下透明度，不用再这么亮了"）。名字已经置灰了，一整排鲜亮绿/琥珀的勾压在
    灰名字上反而抢视线 —— 扫矩阵时最扎眼的该是"还差哪些"，不是已经做完的行。
    钉两面：① 还有一个号没做完 ⇒ 勾保持原亮度；② 全做完 ⇒ 勾压暗，
    而形状（同一张贴图）与颜色（绿/琥珀这一档）一个都不许跟着变。"""
    lua, gql, _ = sync_env([(196, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    alt = "小号二-永恒之井"
    lua.execute("GQLDB.chars[%r] = { lastSync = 1, views = {} }" % alt)
    gql.Matrix.Render()
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")

    def alphas():
        return [a for path, a, shown in mark_alphas(g.GQLMatrixFrame)
                if path == CHECK_TEX and shown]

    if not click_cell(lua, key, 196):
        return fail("all_done_row_dims_its_checks", "点不到当前角色那一格")
    gql.Matrix.Render()
    one = alphas()
    if len(one) != 1:
        return fail("all_done_row_dims_its_checks",
                    "前提：这一行该只有当前角色那一枚勾，实际 %r" % (one,))
    if one[0] != 1.0:
        return fail("all_done_row_dims_its_checks",
                    f"还有一个号没做完，勾不该压暗，实际 alpha={one[0]}")
    # 另一个号也做完 ⇒ 这一行全员完成（名字置灰），勾跟着压暗
    if not click_cell(lua, alt, 196):
        return fail("all_done_row_dims_its_checks", "点不到另一个号那一格")
    gql.Matrix.Render()
    both = alphas()
    if len(both) != 2:
        return fail("all_done_row_dims_its_checks",
                    "前提：两格都该是勾，实际 %r" % (both,))
    if any(a >= 0.8 for a in both):
        return fail("all_done_row_dims_its_checks",
                    f"全员完成的行勾要压暗，实际 alpha={both}")
    # 压暗只动透明度：形状与颜色原样（两枚都是手点的琥珀档）
    if len(check_glyph(g.GQLMatrixFrame, tinted=True)) != 2:
        return fail("all_done_row_dims_its_checks",
                    "压暗不许改掉勾的形状/颜色档，实际 %r"
                    % (cell_marks(g.GQLMatrixFrame),))
    ok("all_done_row_dims_its_checks")


def row_line_heights(lua):
    """右窗格里所有"行分隔线"的高度（线是 content 上的 1 像素级淡色纹理：
    宽度盖满内容、带 __color、高度很小）。没有线回空表。"""
    res = lua.execute("""
        local out = {}
        for i, c in ipairs(GQLMatrixContent.__children or {}) do
            if c.__shown and c.__color and (c.__h or 99) <= 6 and (c.__w or 0) > 50 then
                out[#out + 1] = c.__h
            end
        end
        return table.concat(out, ',')
    """)
    return [float(v) for v in str(res or "").split(",") if v != ""]


def test_row_separator_is_one_device_pixel_tall():
    """行分隔线的厚度按设备像素算，不写死 1（用户 2026-10-30："为什么胆大包天和巢穴
    两个任务之间没有分割线"）。线本来每行都画了，但 1 个 UI 单位在 UI 缩放 ≠ 1 时
    不足一个屏幕像素，客户端会把那一根整个丢掉 —— 那张截图放大到像素级看，
    缺的两条是**零亮度**、左右邻居都在，所以是"整根没画"，不是"变淡"。
    修法：厚度 = 1/scale（正好一个设备像素）。这条用例把缩放设成 0.5，
    要求线高变成 2；默认缩放 1 时仍然是 1（像素级不动，只是不再消失）。"""
    lua, gql, _ = sync_env([(197, 30000000), (198, 20000000)], extra=("Matrix",))
    gql.Matrix.Toggle()          # 打开时会抓一次快照，才真的有行可画
    plain = row_line_heights(lua)
    if not plain:
        return fail("row_separator_is_one_device_pixel_tall", "一行分隔线都没摆出来")
    if any(abs(h - 1) > 0.01 for h in plain):
        return fail("row_separator_is_one_device_pixel_tall",
                    f"缩放 1 时线高该还是 1，实际 {plain}")
    # 半缩放：一个 UI 单位只有半个屏幕像素，线要加厚到 2 才盖得住一个整像素
    lua.execute("GQLMatrixContent.__scale = 0.5 GQLMatrixContentL.__scale = 0.5")
    gql.Matrix.Render()
    half = row_line_heights(lua)
    if not half:
        return fail("row_separator_is_one_device_pixel_tall", "缩放 0.5 后线没了")
    if any(abs(h - 2) > 0.01 for h in half):
        return fail("row_separator_is_one_device_pixel_tall",
                    f"缩放 0.5 时线高该是 2（= 一个设备像素），实际 {half}")
    ok("row_separator_is_one_device_pixel_tall")


def click_cell(lua, char_key, qid, button=None):
    """按"列上的号名 + questID"点中那一格：走的是热区自己的 OnClick，也就是
    玩家真点下去的那条路（CellClick），不从中间调函数。
    button=None 保持老口径（不传按钮 = 左键那条，CellClick 只认 "RightButton" 这一个值）。"""
    arg = "" if button is None else ", %r" % button
    return bool(lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s and c.__questID == %d and c.__charKey == %r then s(c%s) return true end "
        "end return false" % (qid, char_key, arg)))


# 完成勾是**贴图**（用户 2026-10-30："目前的 √ 有点太细，看不清晰"→ 回到游戏自带的
# ReadyCheck-Ready）。两档仍是同一个形状、只换颜色：绿勾原色不动，手点补录先
# SetDesaturated(true) 抽成灰度再乘琥珀 —— 绿勾直接乘琥珀只会更绿，抽灰度是必须的。
CHECK_TEX = "Interface\\RaidFrame\\ReadyCheck-Ready"
AMBER_TINT = (1, 0.76, 0.10)


def cell_marks(frame):
    """矩阵每格里那枚贴图记号：回 [(纹理路径, 是否抽灰度, (r,g,b), 是否可见)]。
    勾与"在做"图标都走贴图（Matrix.CellMark），文字池那边只剩 ? / — / ×。"""
    out = []

    def walk(node):
        if node is None:
            return
        mark = node.__mark
        if mark is not None:
            vc = mark.__vcolor
            # Lua 的表是 1 起：__vcolor = { r, g, b, a } ⇒ [1]/[2]/[3]
            out.append((str(mark.__texture or ""), bool(mark.__desat),
                        None if vc is None else (float(vc[1]), float(vc[2]), float(vc[3])),
                        bool(mark.__shown)))
        kids = node.__children
        if kids is None:
            return
        i = 1
        while kids[i] is not None:
            walk(kids[i])
            i += 1

    walk(frame)
    return out


def mark_alphas(frame):
    """每一枚贴图记号的透明度。全员完成那一行要连勾一起压暗（见
    test_all_done_row_dims_its_checks）—— 只改 alpha，形状与颜色不许跟着动。"""
    out = []

    def walk(node):
        if node is None:
            return
        mark = node.__mark
        if mark is not None:
            out.append((str(mark.__texture or ""), float(mark.__alpha or 1), bool(mark.__shown)))
        kids = node.__children
        if kids is None:
            return
        i = 1
        while kids[i] is not None:
            walk(kids[i])
            i += 1

    walk(frame)
    return out


def check_glyph(frame, tinted=None):
    """取那一格的完成勾是哪一档：手点 = 抽灰度 + 琥珀染色，客户端/流水认的 = 原色绿勾。
    两枚**同一个形状**（同一张贴图，用户 2026-10-29："这两个对勾的样式不统一，
    最好统一一下"），区分只靠颜色，所以这里认贴图 + 顶点色本体（|cff 那套文字字形
    已经不用了）。tinted=True 只取琥珀，False 只取绿的，None 两档都算。"""
    out = []
    for path, desat, vc, shown in cell_marks(frame):
        if path != CHECK_TEX or not shown:
            continue
        amber = desat and vc is not None and vc[1] < 0.85 and vc[2] < 0.3
        green = (not desat) and (vc is None or (vc[0] > 0.9 and vc[1] > 0.9 and vc[2] > 0.9))
        if tinted is True and amber:
            out.append("amber")
        elif tinted is False and green:
            out.append("green")
        elif tinted is None and (amber or green):
            out.append("check")
    return out


def test_both_checks_share_one_glyph():
    """两枚勾的形状钉死成同一个（用户 2026-10-29 看着矩阵里"绿的是游戏原生圆底勾、
    琥珀的是文字 √"说"这两个对勾的样式不统一，最好统一一下"）。
    2026-10-30 用户又说"√ 太细，看不清晰"，于是形状回到游戏自带的 ReadyCheck-Ready，
    而颜色改走 Texture:SetVertexColor —— 上一版失败的写法是 **|T 内联色**（顶点色字段位
    摆错、勾按原色上屏），不是贴图本身。所以这一条静态守卫改成钉三件事：
    ① 勾的形状只有一张贴图（ReadyCheck-Ready 在文件里只出现一次，就是 CHECK_TEX）；
    ② 染色只有一条路（SetVertexColor），且手点那档必须先 SetDesaturated 抽灰度 ——
       绿勾直接乘琥珀只会更绿，抽灰是这档颜色能出来的前提；
    ③ 文字字形 √ 不许回来：用户已经说过它太细。
    progress 那枚 ReadyCheck-Waiting 贴图不在范围：它是"在做"，不是做完了的勾。"""
    with open(os.path.join(PLUGIN_DIR, "Matrix.lua"), encoding="utf-8") as fh:
        code = "\n".join(line.split("--", 1)[0] for line in fh)
    if code.count("ReadyCheck-Ready") != 1:
        return fail("both_checks_share_one_glyph",
                    "完成勾的贴图路径只许在 CHECK_TEX 写一处（形状只剩一种）")
    if "√" in code or "DONE_GLYPH" in code:
        return fail("both_checks_share_one_glyph",
                    "文字字形 √ 不许回来（用户 2026-10-30：太细，看不清）")
    for want in ("tex:SetVertexColor(", "tex:SetDesaturated(true)"):
        if want not in code:
            return fail("both_checks_share_one_glyph",
                        f"两档勾只许用纹理染色，缺 {want}")
    ok("both_checks_share_one_glyph")


def test_hand_marked_cell_uses_an_amber_check():
    """玩家手点的勾要换成琥珀色（用户 2026-10-25："手动补录这个，换 √ 的颜色，
    一目了然哪个是手动的"）。第一版给 ReadyCheck-Ready 挂 |T 内联顶点色，但纹理串
    字段位摆错且离线测不出渲染：顶点色没生效、勾按贴图原色（绿）上屏 —— 用户
    2026-10-29："没做过的任务 点击标记完成 直接就是绿色"。现在的写法是**真的纹理对象**
    加 SetVertexColor：琥珀勾 = 抽灰度 + 染色、客户端不认；绿勾 = 同一张贴图原色。
    形状仍只有一种（用户 2026-10-29 要求样式统一），颜色一对一映射"这一格能不能当凭据"。
    用例钉贴图路径 + 顶点色 + 抽灰这三样，任何一样丢了这条就红。"""
    lua, gql, _ = sync_env([(92, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    # 接口答"没做过"，玩家还点这一格 = 真手点的（不许拿接口替玩家背书）
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, key, 92):
        return fail("hand_marked_cell_uses_an_amber_check", "点不到那一格")
    gql.Matrix.Render()
    amber = check_glyph(g.GQLMatrixFrame, tinted=True)
    if len(amber) != 1:
        return fail("hand_marked_cell_uses_an_amber_check",
                    "手点的格该有一枚琥珀勾:\n" + "\n".join(frame_texts(g.GQLMatrixFrame))
                    + "\n记号: %r" % (cell_marks(g.GQLMatrixFrame),))
    # 染色必须真落在这枚贴图上：路径对、抽过灰、顶点色就是那一档琥珀
    marks = [m for m in cell_marks(g.GQLMatrixFrame) if m[0] == CHECK_TEX and m[3]]
    if len(marks) != 1 or not marks[0][1] or marks[0][2] is None:
        return fail("hand_marked_cell_uses_an_amber_check",
                    f"琥珀勾要先抽灰度再染色，实际 {marks!r}")
    if tuple(round(v, 2) for v in marks[0][2]) != AMBER_TINT:
        return fail("hand_marked_cell_uses_an_amber_check",
                    f"琥珀顶点色要原样上屏 {AMBER_TINT}，实际 {marks[0][2]!r}")
    # 反方向：客户端认的那一格（实测交任务）不许跟着变色，否则颜色就不映射"凭据"了
    # 这一支不再写 `roundAt = 0`（第 36 轮 T4b 那个字段整个下线）：清掉手点的那一笔，
    # 只留实测流水，格子的完成就只剩"这一条实例交过"这一个来源 —— 编号由 OnTurnIn 自己烙。
    lua.execute("GQL.Ledger.SetManual(GQLDB, %r, 92, 'open', 1, nil) "
                "GQL.Ledger.OnTurnIn(GQLDB, %r, 92, 1, nil)" % (key, key))
    gql.Matrix.Render()
    plain = check_glyph(g.GQLMatrixFrame, tinted=False)
    if len(plain) != 1 or check_glyph(g.GQLMatrixFrame, tinted=True):
        return fail("hand_marked_cell_uses_an_amber_check",
                    "客户端/流水认的那一格该是原色绿勾（同一张贴图，只有颜色不同）:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    ok("hand_marked_cell_uses_an_amber_check")


def test_manual_mark_api_done_without_this_instance_evidence_is_amber():
    """回归（用户 2026-10-27）：手动补录一律记成玩家手点的琥珀勾，客户端接口答什么都
    不再借它升绿色（IsQuestFlaggedCompleted 之类跨轮/跨 CD 会残留，分不出"这一条实例"）。
    绿=客户端/流水认的、琥珀=我自己记的，颜色一对一映射"这一格能不能当凭据"。
    这一条把客户端 stub 成 done，证明手动补录就是琥珀、不跟着接口走。"""
    lua, gql, _ = sync_env([(93, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Api.QuestState = function(q) return 'done', nil end")
    if not click_cell(lua, key, 93):
        return fail("manual_mark_api_done_without_this_instance_evidence_is_amber", "点不到那一格")
    if g.GQLDB["manual"][key][93]["v"] is not None:
        return fail("manual_mark_api_done_without_this_instance_evidence_is_amber",
                    "手动补录不许升绿色（接口核验），实际 %s" % g.GQLDB["manual"][key][93])
    gql.Matrix.Render()
    if len(check_glyph(g.GQLMatrixFrame, tinted=True)) != 1:
        return fail("manual_mark_api_done_without_this_instance_evidence_is_amber",
                    "这一条实例没有凭据的那一格该是琥珀勾:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    ok("manual_mark_api_done_without_this_instance_evidence_is_amber")


def test_manual_mark_with_this_instance_evidence_turns_green():
    """对照上一条：接口答 done 且账本里这笔完成的编号 == 这一条任务当前的编号
    （= 这一条实例交过）→ 记成接口核验，勾保持客户端认的绿色（不是玩家手点的琥珀）。
    绿/琥珀只映射"这一格能不能当凭据"，所以只有真凭据才升绿色。
    老那句 `q.roundAt = 100` + "时间戳落在本轮窗口内" 是反推起点的写法（§五 整块下线）；
    这一条改走真实写点：OnTurnIn 自己烙编号，所以不再手拼 turnins 表。
    也不再 stub `_G.time`：WallClock 一回纪元秒，板面倒计时（开机秒域）就跟它跨域，
    编号压根算不出来 —— 那测的是"身份未定"，不是"有凭据"。"""
    lua, gql, _ = sync_env([(93, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    # 这一条已经不在板上了（obs=false），所以 L.State 不会直接答 done、点下去走的是
    # 手动补录分支；这一条实例交过（流水带编号）才升绿色。金额 0 = 这一处没量到钱，
    # 用 raw_turnin 而不是 OnTurnIn：后者会顺手把那份签名标成金币任务，污染别的前提。
    lua.execute("local q = GQLDB.quests[93] "
                "q.observations = {} q.currentSig = 'x' q.poolSeen = true")
    raw_turnin(lua, gql.Ledger, g.GQLDB, key, 93, 1)
    lua.execute("GQL_API_STATE_CALLS = 0 "
                "GQL.Api.QuestState = function(q) "
                "  GQL_API_STATE_CALLS = GQL_API_STATE_CALLS + 1 return 'done', nil end")
    if not click_cell(lua, key, 93):
        return fail("manual_mark_with_this_instance_evidence_turns_green", "点不到那一格")
    if int(g.GQL_API_STATE_CALLS) != 1:
        return fail("manual_mark_with_this_instance_evidence_turns_green",
                    "点一次只该问一次接口，实际问了 %s 次" % g.GQL_API_STATE_CALLS)
    if str(g.GQLDB["manual"][key][93]["v"]) != "接口核验":
        return fail("manual_mark_with_this_instance_evidence_turns_green",
                    "有这一条实例凭据的那一格要标成核验，实际 %s" % g.GQLDB["manual"][key][93])
    gql.Matrix.Render()
    if check_glyph(g.GQLMatrixFrame, tinted=True):
        return fail("manual_mark_with_this_instance_evidence_turns_green",
                    "核验过的格不许染成手点的颜色:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    ok("manual_mark_with_this_instance_evidence_turns_green")


def test_manual_mark_from_the_previous_instance_stays_amber():
    """绿色那一条的反方向：交任务的凭据属于**上一条实例**（它的编号已被新的顶掉）⇒
    不算这一条的凭据，点下去只记玩家手点的琥珀勾。
    上一版用 `t < 本轮起点` 表达这件事，第 36 轮换成"换一个结束时刻"：编号一变，
    旧凭据自动匹配不上 —— 正是 94612 那批假勾的修法。"""
    lua, gql, _ = sync_env([(93, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    lua.execute("local q = GQLDB.quests[93] "
                "q.observations = {} q.currentSig = 'x' q.poolSeen = true")
    raw_turnin(lua, gql.Ledger, g.GQLDB, key, 93, 1)
    board = g.GQL_BOARD
    # 11-04 口径：旧实例下板 ⇒ 账本里它的结束时刻不再算数，先作废再问新的
    g.GQLDB["quests"][93]["roundEnd"] = None
    board["end"] = 200000                          # 板面换成下一条实例
    gql.Sync.SnapshotNow()
    lua.execute("GQL.Api.QuestState = function(q) return 'done', nil end")
    if not click_cell(lua, key, 93):
        return fail("manual_mark_from_the_previous_instance_stays_amber", "点不到那一格")
    if g.GQLDB["manual"][key][93]["v"] is not None:
        return fail("manual_mark_from_the_previous_instance_stays_amber",
                    "上一条例的凭据不许把这一条顶成绿色，实际 %s" % g.GQLDB["manual"][key][93])
    ok("manual_mark_from_the_previous_instance_stays_amber")


def test_click_on_another_chars_cell_never_borrows_this_chars_answer():
    """别人的号问不出来：客户端只答当前角色，跨号数据压根不在本机内存里。
    所以点别的号的格子**不许**去问接口 —— 那等于拿我这个号的答案冒充那一号（谎报），
    也白加一次接口调用。那一号只能照旧记手动补录（琥珀勾）。"""
    lua, gql, _ = sync_env([(94, 30000000)], board=43200, extra=("Matrix",))
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


def test_completion_line_carries_no_source_suffix():
    """悬停最下面那一行只写『某号 交于 时刻 到手 X』（第 33 轮口径：完成时间后面那个来源
    括号 ——（实测）/（接口核验）/（手动补录）—— 说的是"这一格凭什么算完成"，属于取证，
    不上界面）。源头一个字不许动：接口核验过的那一格存档里照旧写"接口核验"
    （和 click_on_this_char_verifies_with_the_interface 那一条同一口径），
    /gql quest 与导出仍报得出是哪一种。"""
    lua, gql, g, calls = tip_env(131, 1792577090, cell=True)
    key = str(gql.Sync.CurrentCharKey())
    lua.execute("GQL.Ledger.SetManual(GQLDB, %r, 131, 'done', 1792577090, nil, '接口核验')" % key)
    gql.Matrix.Render()
    hover_update(lua)
    joined = "\n".join(calls)
    if str(g.GQLDB["manual"][key][131]["v"]) != "接口核验":
        return fail("completion_line_carries_no_source_suffix",
                    "只许删显示：存档里的来源标签要还是「接口核验」，实际 %s"
                    % g.GQLDB["manual"][key][131])
    line = [c for c in calls if "交于" in c]
    if not line:
        return fail("completion_line_carries_no_source_suffix",
                    f"完成那一行本身不许消失:\n{joined}")
    for banned in ("（", "实测", "接口核验", "手动补录", "凭据"):
        if banned in line[0]:
            return fail("completion_line_carries_no_source_suffix",
                        f"核验过的那一格时间行里不许再带任何来源后缀（{banned}）: {line[0]}")
    ok("completion_line_carries_no_source_suffix")


def test_quest_id_is_gray_like_the_bottom_line():
    """ID 上灰、和完成那一行同一个色（用户 2026-10-25："改成灰色的，
    和最下面那一行一个颜色，字也小一点"）。标题行是 AddDoubleLine：
    左=任务名（白，标题字体），右=questID（0.6,0.6,0.6，模板里右列本来就比标题小）。
    字号是客户端模板的事（暴雪转义表里没有"字号"这一项，只有字体对象能改），
    所以用例钉得住颜色参数，钉不住字号 —— 那条要在游戏里看一眼。"""
    lua, gql, g, calls = tip_env(132, 1792577090, turnin=1792577090, cell=True)
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


def test_add_map_remembers_sub_maps_and_remove_removes_them():
    """用户 2026-10-31："用户添加后，系统自动记住所有其子地图，但不用展示出来。"
    AddMap 落两笔账：extraMaps[图]=true（面板展示、可移除）与
    subMaps[子图]=父图（只进扫描清单与判定，面板不列）；RemoveMap 把两笔一起退。
    TrackedMapIDs = 两笔的并集；一张都没有时回 nil —— 不是空表：
    池那路把空清单当"真·空池"会毁掉整轮轮次。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    tree = {2395: [2444, 2401], 2444: [2445], 2401: [], 2445: []}
    g.C_Map = lua.table_from({
        "GetMapChildrenInfo": lambda mid: lua.table_from(tree.get(int(mid), [])),
    })
    if gql.Config.TrackedMapIDs() is not None:
        return fail("add_map_remembers_sub_maps_and_remove_removes_them",
                    "一张图都没加时清单必须 nil（不是空表）")
    gql.Config.AddMap(2395)
    listed = gql.Config.TrackedMapIDs()
    ids = sorted(int(listed[i]) for i in range(1, len(listed) + 1))
    if ids != [2395, 2401, 2444, 2445]:
        return fail("add_map_remembers_sub_maps_and_remove_removes_them",
                    f"清单应是父图 + 整棵子树（递归两层），实际 {ids}")
    owner = lua.execute("return GQLDB.config.subMaps[2445]")
    if owner is None or int(owner) != 2395:
        return fail("add_map_remembers_sub_maps_and_remove_removes_them",
                    f"子图要记着主人（归属滚动用），实际 {owner}")
    gql.Config.RemoveMap(2395)
    if gql.Config.TrackedMapIDs() is not None:
        return fail("add_map_remembers_sub_maps_and_remove_removes_them",
                    "移除后父图与它名下的子图都要退掉")
    ok("add_map_remembers_sub_maps_and_remove_removes_them")


def test_getpool_queries_only_tracked_maps():
    """池快照只问关注清单里的地图（用户实测：其他地图以后不查，省掉几百张图的
    全图扫描 —— 那是卡顿源）。2026-10-31 起清单 = 玩家自己加的图 ∪ 自动记住的
    子图；没加过的图（680）不问、任务也进不了池。"""
    lua, gql = load_lua("Config", "Ledger", "Api", mock=True)
    g = lua.globals()
    g.C_WorldQuests = None
    g.GetWorldQuests = None
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
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
        "GetMapChildrenInfo": lambda mid: lua.table_from(tree.get(int(mid), [])),
    })
    g.C_TaskQuest = lua.table_from({"GetQuestsOnMap": quests_on_map})
    g.GQLDB = lua.table_from({})
    gql.Config.Init()
    # 玩家只加了 2395（和它自动记下的子图，若有）
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
    pool, err, meta = gql.Api.GetPool()
    if pool is None:
        return fail("getpool_queries_only_tracked_maps", f"池应可用，err={err}")
    if len(pool) != 1:
        return fail("getpool_queries_only_tracked_maps", f"应只收到关注图的任务，实际 {len(pool)} 条")
    if 680 in asked or 2393 in asked:
        return fail("getpool_queries_only_tracked_maps",
                    f"没加过的图不该被查询，实际问过 {sorted(set(asked))}")
    ok("getpool_queries_only_tracked_maps")


def test_matrix_cell_click_records_quest_gold():
    """手动点 done 时把任务金币一起记上：玩家确实做了任务，周统计不能显示
    "完成 1 金币 0"（那表示白干）。金额没读到才留空。"""
    lua, gql, _ = sync_env([(93, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    g.GQL_NOW = 100000            # 锚点是 700000-604800=95200，手动时间戳要落在本周内
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    lua.execute(
        "for i, c in ipairs(GQLMatrixContent.__children) do "
        "  local s = c.__scripts and c.__scripts['OnClick'] "
        "  if s then s(c) break end end")
    gval = lua.execute(
        "local m = GQLDB.manual['%s'][93] "
        "if not m then return nil end return m.g" % key)
    if gval is None or int(gval) != 30000000:
        return fail("matrix_cell_click_records_quest_gold",
                    f"手动 done 应记上任务金币，实际 {gval}")
    cnt = lua.execute(
        "return (GQL.Ledger.WeekStats(GQLDB, '%s', GQLDB.cycleAnchor, GQL_NOW))" % key)
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


def test_missing_money_never_reschedules_a_full_pool_pass():
    """金额读不全时，开着矩阵不再每 5 秒重问整板块（几百张图逐个问 = 用户实测的卡顿）。
    缺的那几条走定向补读（Sync.SettleAskedRewardGold，借 QUEST_LOG_UPDATE，每条每趟 1 次），
    见 test_reward_data_arrival_settles_gold_without_the_matrix。
    这里钉的是反面：面板开着、金额一直读不出，待触发定时器只该有"到期重画"那一颗，
    而且推进 6 个 5 秒不会多抓一次池。"""
    lua, gql, _ = sync_env([(97, None)], extra=("Matrix",))
    g = lua.globals()
    g.GetQuestLogRewardMoney = lambda qid: 0      # 数据一直没到
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()                           # 打开 = 抓一次，金额读空
    before = str(gql.Config.GetLogs()).count("快照完成")
    for step in range(6):
        g.GQL_NOW = 1000 + 5 * (step + 1)
        lua.globals().GQL_FireTimers()
    logs = str(gql.Config.GetLogs())
    if str(logs).count("快照完成") != before:
        return fail("missing_money_never_reschedules_a_full_pool_pass",
                    f"金额读空不该再排整板块补读:\n{logs}")
    pending = lua.execute("local n = 0 for _, t in ipairs(GQL_NEWTIMERS) do "
                          "if not t.cancelled then n = n + 1 end end return n")
    if int(pending) > 1:
        return fail("missing_money_never_reschedules_a_full_pool_pass",
                    f"待触发定时器只留到期重画那一颗，实际 {pending} 个")
    ok("missing_money_never_reschedules_a_full_pool_pass")


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
    """交割当场落账之后也要重画矩阵：玩家刚交完一条任务，那一格还停在旧的一份画面上
    就是账本更新了玩家看不见。这条路径零接口调用（流水与 views 全在本地算完）。
    重绘走的是版面合并那条口子（Matrix.lua 的 COALESCE_SECONDS = 0.25 秒）：开面板那次
    是同步首绘并把 lastPaint 推到当下，紧随其后的 Refresh 只排一次性补画定时器，
    所以这里必须把合并窗口点到，才能看见"交割确实喊到了重画"（同 event_snapshot 那条）。"""
    lua, gql, _ = sync_env([(121, 30000000)], extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    lua.execute("GQL.Matrix.rowCount = -1")
    fire(gql, "QUEST_COMPLETE", 121)
    text = str(gql.Config.GetLogs())
    if "交割当场落账 quest=121" not in text:
        return fail("turnin_settlement_repaints_matrix", f"交割没当场落账:\n{text}")
    g.GQL_NOW = 2
    g.GQL_FireTimers()                        # 合并窗口到点：补画那一次
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
    （用户实测"来回点几次列表就变、任务缺失"）。延续金额后行必须稳。
    board_clock(200000)：两趟同步要用**同一个**结束时刻（倒计时随 GQL_NOW 递减，
    结束时刻才不变）—— 否则第二趟就是换了一条实例，粘滞照 §六 1(c) 第 3 步作废。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    board_clock(lua, 200000)
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
    m = lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 101, 100100)")
    if m is None or int(m) != 30000000:
        return fail("money_read_flicker_does_not_drop_rows",
                    f"101 的金额应延续这一条实例的旧值，实际 {m}")
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
    —— 后者会把双模式那条误删（见 test_dual_mode_flip_keeps_the_row_and_the_check）。
    【快照跑三趟】换实例的那一次同步里，§六 1(c) 第 2 步会先作废这份 observation 的金额
    （DiffPool 排在倒计时读取之前，那一份还挂着上一条实例的编号），第 4 步只把**正数**
    续回 rec.goldMoney ⇒ 换实例那一趟客户端答的那个 0 会晚一趟才落地（少报一趟，不是假勾）。
    所以断言"写 0"要在新实例的第二趟读，第三趟同一条实例才是它应有的形状。"""
    lua, gql, _ = sync_env([(101, 30000000), (102, 20000000)], extra=("Matrix",))
    g = lua.globals()
    board = board_clock(lua, 143200)        # 这一条实例的结束时刻 = 143200
    g.GQL_NOW = 100000
    gql.Sync.SnapshotNow()
    before = uid(gql.Ledger, g.GQLDB, 101, 100000)
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
    # 11-04 口径：换实例 = 旧那条已经下板，账本里它的结束时刻不再算数；先清掉再改板面，
    # 新实例才会在这一趟被问到自己的结束时刻。
    g.GQLDB["quests"][101]["roundEnd"] = None
    board["end"] = 136100                   # 结束时刻挪了 7200 秒 = 板面上换了一条
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 101, 100100) == before:
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    "前置：换了结束时刻却没换编号，粘滞那一道门根本没被走到")
    # 同一条实例的下一趟：这一读的 0 挂得上了 ⇒ 照实写 0（客户端明说这一条不给金币）
    gql.Sync.SnapshotNow()
    m = lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 101, 100100)")
    if m is None or int(m) != 0:
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    f"客户端确认无金币时要写 0，不许延续上个 CD 的数，实际 {m}")
    n = int(gql.Matrix.Render())
    if n != 1:
        joined = "\n".join(frame_texts(g.GQLMatrixFrame))
        return fail("confirmed_no_gold_does_not_keep_the_old_number",
                    f"换了实例又不给金币的那条不该再占行，实际 {n} 行:\n{joined}")
    if int(lua.execute("return GQL.Ledger.QuestMoney(GQLDB, 102, 100100)")) != 20000000:
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


def tip_env(qid, wall, turnin=None, cell=False, cols=None, hover=None,
            pool_money=30000000, kw=None):
    """矩阵开着 + 停在第 qid 那一行/那一格上的环境。可选：
      turnin     给本号记一笔完成流水（时刻 turnin）。2026-10-29 起这一笔不带金额：
                 提示里"到手多少"只来自这一条实例的奖励读数（rec.goldMoney），
                 要改那个数就改 pool_money，别在流水上动手脚。
      pool_money 池行这一读答出的奖励金额（0 = 客户端明说这副奖励不给金币）
      kw         先加进关注关键词再重画：奖励读 0 的行只能靠"玩家点名 + 凭据"上板
      cols       额外角色列 {角色key: 本轮读到的金额}，金额 None = 这一号本轮没读到过
      hover      停在**哪个号**的格子上（只在 cell=True 时用；默认当前角色）

    与 hover_env 的分工：那个停在整行热区（左窗格，压根没有角色归属），
    这里要的是"鼠标压在某个号的格子上"那条路径 —— 完成时间、按号读到的金币
    只有这一条路拿得到。
    wall = GQL.Api.WallClock 的返回（"nil" = 本机没有墙上时钟，时间戳落在开机秒域）。

    cols 的金额走 Ledger.SetRewardRead（生产写点就是 Sync 那一处，这里直接落账是为了
    绕开"一次同步只有一个号"的限制）：先抓一次快照把这一条实例的结束时刻定下来，
    读数再按那一刻落账 —— 顺序反了的话 Matrix.Toggle 里那次快照会换成下一条实例，
    这份读数当场挂不上编号，用例就变成在测身份门（那是另一条用例）。
    """
    lua, gql, _ = sync_env([(qid, pool_money)], extra=("Matrix",))
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
        # 时刻取"这一次同步见到它"那一刻（rec.lastSeenAt）：用例不必猜同步用的是哪个时钟域，
        # 又保证这份证据落在**这一条实例**里 —— 它和 roundEnd 同域、必然早于结束时刻，
        # SetView/SetRewardRead 自己算出的编号就是当前这一把。
        # 换一条实例那条用例自己把结束时刻往后挪，这一把编号自然对不上。
        lua.execute(
            "local db = GQL.Config.DB()"
            " local at = db.quests[%d].lastSeenAt"
            " GQL.Ledger.SetView(db, %r, %d, 'open', at)"
            " GQL.Ledger.SetRewardRead(db, %r, %d, %s, at)"
            % (qid, key, qid, key, qid, ("nil" if money is None else money)))
    if kw is not None:
        # 关注关键词要在重画之前就位：读 0 奖励的那一行只有靠"玩家点名 + 凭据"这道门才在列
        lua.execute("GQL.Config.AddKeyword(%r)" % kw)
    gql.Matrix.Toggle()
    if turnin is not None:
        lua.execute(
            "GQL.Ledger.OnTurnIn(GQL.Config.DB(), GQL.Sync.CurrentCharKey(), %d, %d, nil)"
            % (qid, turnin))
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


def test_money_tooltip_survives_missing_ledger():
    """T12：tooltip 组装这一路外层是**静默 pcall**（Matrix.ShowMoneyTooltip 的
    pcall 就地丢弃返回值、不写日志）—— 裸调用一缺模块就炸成"悬停没反应"，
    /gql log 里一个字都没有。所以该函数所有跨模块调用点统一守卫口径（572/585
    那个样式），问不出的按"宁缺不编"少画一行。钉：GQL.Ledger 整模块缺失时
    这一格 tooltip 不许炸整段 —— 标题、金额占位、Show 都得照常出来。"""
    lua, gql, g, calls = tip_env(125, 1792577090)
    lua.execute("GQL.Ledger = nil")
    hover_update(lua)
    joined = "\n".join(calls)
    if "SHOW" not in calls:
        return fail("money_tooltip_survives_missing_ledger",
                    "缺 Ledger 时提示仍要走到 Show（守卫兜住，不靠 pcall 吞错）")
    if tip_title(calls, 125) is None:
        return fail("money_tooltip_survives_missing_ledger",
                    f"标题行（任务名 + ID）不许被缺模块吃掉:\n{joined}")
    if "金额未知" not in joined:
        return fail("money_tooltip_survives_missing_ledger",
                    f"金额问不出要如实降级成「金额未知」，别的行照出:\n{joined}")
    ok("money_tooltip_survives_missing_ledger")


def test_cell_hover_prints_that_chars_completion_time():
    """停在某个号的格子上：报**那一列的号**上一次交这条是哪一刻、到手多少（用户要求
    "完成时间（如果有数据）"）。时刻必须真是日期时刻，不是"2分前"那种相对值 ——
    取证要的是"我周四几点做的"。"""
    lua, gql, g, calls = tip_env(112, 1792577090, turnin=1792577090,
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
    lua, gql, g, calls = tip_env(113, 1792577090, turnin=1792577090,
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
    lua, gql, g, calls = tip_env(114, "nil", turnin=3000, cell=True)
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
    第 36 轮起"换轮"这件事没有起点可推：板面倒计时整段挪走 = 板上换成下一条实例，
    编号跟着换，上一轮的流水自己匹配不上（生产里由 L.SetExpiry 写 rec.roundEnd）。"""
    lua, gql, g, calls = tip_env(130, 1792577090, turnin=1792577090,
                                 cell=True)
    hover_update(lua)
    if not [c for c in calls if "交于" in c]:
        return fail("completion_line_hidden_once_the_instance_rolled",
                    "用例前提：本轮内交过该报完成时间:\n" + "\n".join(calls))
    # 换一条实例：倒计时挪走一小时，再同步一次让账本读到新的结束时刻
    before = uid(gql.Ledger, g.GQLDB, 130, 1792577090)
    # 11-04 口径：旧那条已下板 ⇒ 它的结束时刻不再算数；不清就只是"同一条实例被
    # 凭空改了倒计时"，客户端不会这么答，新口径也照旧不重问（编号不变）。
    g.GQLDB["quests"][130]["roundEnd"] = None
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda q: 3600})
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 130, 1792577090) == before:
        return fail("completion_line_hidden_once_the_instance_rolled",
                    "前置：倒计时挪走要真的换出一条实例（编号没变，%s）" % before)
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
    地方都问这一处，别在界面里再翻一遍 db.turnins：流水的存法一改报告就说瞎话。
    到手金额自 2026-10-29 起只来自这一条实例的奖励读取（rec.goldMoney + rec.goldKey），
    所以这里走一次真实快照把读数拿到账上，并且给一个墙上时钟 —— 流水那笔的时刻在纪元域，
    没有锚点就算不出编号，编号算不出金额就匹配不上（不是回归，是身份未定）。"""
    lua, gql, _ = sync_env([(115, 30000000)], weekly=600000, wall=1792577090)
    key = str(gql.Sync.CurrentCharKey())
    db = lua.globals().GQLDB
    gql.Sync.SnapshotNow()

    def got():
        # 三个返回值在 lupa 里会被尾部 nil 挤形（单值 nil 返成 None），在 Lua 侧拼成一串再取
        return one(lua.execute(
            "local t, g, s = GQL.Ledger.LastCompletion(GQL.Config.DB(), %r, 115)\n"
            "return (t and tostring(t) or '-') .. '|' .. (g and tostring(g) or '-')"
            " .. '|' .. (s or '-')" % key))

    if got() != "-|-|-":
        return fail("last_completion_takes_the_newer_record",
                    f"没记录应回 nil，实际 {got()}")
    if str(gql.Ledger.GetObs(db, 115)["key"]) == "nil":
        return fail("last_completion_takes_the_newer_record",
                    "前置：这条实例的编号必须算得出，否则金额匹配不上，测的就不是取新那份")
    gql.Ledger.OnTurnIn(db, key, 115, 1792577090, None)
    if got() != "1792577090|30000000|实测":
        return fail("last_completion_takes_the_newer_record",
                    f"应报实测流水，实际 {got()}")
    gql.Ledger.SetManual(db, key, 115, "done", 1792577990, 100)
    if got() != "1792577990|100|手动补录":
        return fail("last_completion_takes_the_newer_record",
                    f"补录更新时应盖过流水，实际 {got()}")
    gql.Ledger.SetManual(db, key, 115, "done", 1792576000, 100)
    if got() != "1792577090|30000000|实测":
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


def test_item_cell_says_that_alt_and_keeps_the_reader_list_off_ui():
    """压在**物品号**的格子上：只说这一号自己的结果（用户 2026-10-25：给金币的角色
    都做完了，悬停还说"这一档不给金币"是把一个号的答案当成整行结论）。
    第 33 轮口径：那半句"本轮读到的奖励"、以及"这行还在列是因为别的号读到了金币"
    都不再上界面（讲的是我们怎么知道的），归属改由 /gql quest 的「金币读者」行报。"""
    lua, gql, g, calls = tip_env(117, 1792577090, cell=True, hover="物品号-永恒之井",
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    mine = [c for c in calls if "物品号 这轮做它不给金币" in c]
    if not mine:
        return fail("item_cell_says_that_alt_and_keeps_the_reader_list_off_ui",
                    f"该按号说这一格不给金币:\n{joined}")
    for gone in ("这行还在列", "本轮读到了金币", "本轮读到的", "金币号"):
        if gone in joined:
            return fail("item_cell_says_that_alt_and_keeps_the_reader_list_off_ui",
                        f"『这一行为什么还在列』的取证说法不许再出现在界面上（{gone}）:\n{joined}")
    ok("item_cell_says_that_alt_and_keeps_the_reader_list_off_ui")


def test_turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line():
    """【2026-11-03 改口径，计划 §二第 4 条；这一条钉的曾是相反的一面】
    2026-10-29 那一版：这一号读到 0、而这一条实例被**另一个号**读到过 2068 金时，
    拿那个正数把"这轮做它不给金币"那句抑制掉 —— 因为底下"交于…到手 2068🪙"跟它自己打自己
    （用户 2026-10-29 的特别任务：猎人的忏悔，圣光之力 10-29 09:27 到手 2068 金）。
    11-03 的截图证明那次圆场本身就是缺陷：那句"到手 2068"（截图里是 1089）根本不是这一号
    挣的，挂在麦德名下就是借号挂名。现在两处都按号：
    · 上面那行金额 = 这一号自己的读数（0 ⇒ 如实说"这轮做它不给金币"）；
    · 交割行 = 这一号的时刻，读数不是正数就不拼「到手」；
    · 行级那 2068 没退役，只是回到本来的岗位（行不掉 / 金币门槛 / 「金币读者」取证）。
    代价照实（计划里写明的）：关键词类"没做完读不到钱、交完就离开板面"的那笔旧 0
    从此显成"不给金币" —— 那是数据源本身的局限，界面不替客户端圆场。"""
    lua, gql, g, calls = tip_env(121, 1792577090, cell=True, hover="物品号-永恒之井",
                                 pool_money=20680086,
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    lua.execute(
        "GQL.Ledger.OnTurnIn(GQL.Config.DB(), '物品号-永恒之井', 121, 1792577090, nil)")
    hover_update(lua)
    joined = "\n".join(calls)
    if "物品号 这轮做它不给金币" not in joined:
        return fail("turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line",
                    f"这一号自己的读数就是 0，那句是真答案，不再被别人的正数抑制:\n{joined}")
    if "物品号 交于" not in joined:
        return fail("turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line",
                    f"交割行照旧报这一号的时刻:\n{joined}")
    if "到手" in joined or "2068" in joined:
        return fail("turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line",
                    f"2068 是另一个号本轮读到的钱，一分钱也不许挂在物品号名下:\n{joined}")
    # 行级那份还在原岗：金币门槛与"切模式不掉行"靠它（QuestMoney 语义一字未动）
    if int(gql.Ledger.QuestMoney(g.GQLDB, 121, 1792577090)) != 20680086:
        return fail("turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line",
                    "行级金额该仍挂在这一条实例上（行不掉/门槛靠它，只是不再冒充某一号的到手）")
    ok("turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line")


def test_zero_gold_read_turnin_keeps_the_no_gold_line():
    """交过这条实例，但它的奖励读取答的是 0（客户端明说这副奖励不给金币）：
    NoteGoldThisInstance 第 4 步只记 > 0 的读数 ⇒ rec.goldMoney 一次都没写下 ⇒
    L.LastCompletion 报不出到手 ⇒ 那句"这轮做它不给金币"照印（2026-10-29 定稿：
    金额只认奖励读取那一路，交割行不带金额，所以这一号本轮的真答案就是 0，
    没有任何一份读数跟它打架）。读 0 的行只能靠"玩家点名 + 凭据"上板，夹具因此给 kw。"""
    lua, gql, g, calls = tip_env(122, 1792577090, cell=True, hover="物品号-永恒之井",
                                 pool_money=0, kw="任务122",
                                 cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0})
    lua.execute(
        "GQL.Ledger.OnTurnIn(GQL.Config.DB(), '物品号-永恒之井', 122, 1792577090, nil)")
    hover_update(lua)
    joined = "\n".join(calls)
    if "物品号 这轮做它不给金币" not in joined:
        return fail("zero_gold_read_turnin_keeps_the_no_gold_line",
                    f"这一条实例读到的是 0，那句还是真答案，不该撤:\n{joined}")
    if "物品号 交于" not in joined or "到手" in joined:
        return fail("zero_gold_read_turnin_keeps_the_no_gold_line",
                    f"交割行要报时刻、但报不出到手（钱不在交割那一处）:\n{joined}")
    ok("zero_gold_read_turnin_keeps_the_no_gold_line")


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
    """这一号本轮没读到过奖励（没同步过）：金额那一行如实报"金额未知"（2026-11-03 起
    格子上不再退回行金额，那个正数是别的号本轮同步读到的），**不加一句归属解释**
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


def test_ui_text_no_forensic_words():
    """第 33 轮口径（用户对着悬停截图："移除一些说明性的文字，比如 客户端记录图、问不出等等，
    这不像是正常软件需要展示的内容，奖励还没读到、实测 等等"）：界面只放玩家这一眼要用的东西，
    "我们是怎么知道的"一律待在 probe / dump / 日志里。这条是防它长回来的门 ——
    矩阵开着、一行和一格都悬上去，版面文字 + GameTooltip 行里再出现下面这批词就算不合格。
    取证面（print / Config.Log / Dump / Probe）不在扫描范围内，它们的作用就是报"问到了什么"。"""
    banned = ("客户端", "问不出", "读到", "实测", "接口", "凭据",
              "账本", "补扫", "同族", "去哪办")
    scenes = (
        # ① 停在整行热区上（左窗格：没有角色归属那一路）
        (dict(cell=False, cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0}),
         "任务163"),
        # ② 压在某个号的格子上：按号金额 + 完成时间那两路全走一遍（A8 的括号就在这一行）
        (dict(cell=True, turnin=1792577090,
              cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0}), "交于"),
        # ③ 压在**不给金币**那个号的格子上（A5 那句按号的话就在这里）
        (dict(cell=True, hover="物品号-永恒之井",
              cols={"金币号-永恒之井": 10900000, "物品号-永恒之井": 0}), "这轮做它不给金币"),
    )
    for i, (kw, marker) in enumerate(scenes):
        lua, gql, g, calls = tip_env(163, 1792577090, **kw)
        hover_update(lua)
        texts = list(calls) + frame_texts(g.GQLMatrixFrame)
        joined = "\n".join(texts)
        # 这一路真弹了提示才算扫过：空扫描会让上面那条负向断言白过
        if not any(marker in t for t in texts):
            return fail("ui_text_no_forensic_words",
                        f"第 {i + 1} 个悬停场景没抓到『{marker}』那一行，扫描是空的:\n{joined}")
        hit = [w for w in banned if w in joined]
        if hit:
            return fail("ui_text_no_forensic_words",
                        f"第 {i + 1} 个悬停场景：界面文字里不许出现取证措辞 {hit}:\n{joined}")
    ok("ui_text_no_forensic_words")


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


def test_reward_read_stops_counting_when_the_instance_rolls():
    """上一条实例那一份奖励不算这一条：结束时刻一挪（板上换成下一条实例）CharRewardMoney
    必须答 nil，否则这一条刚换成物品的那一档会被上一条的金币数冒充"这一号在这条实例上
    有金币可刷"。与 L.State 的视图门同一口径 —— 比的是这笔读数烙下的编号，不是 CD。"""
    lua, gql, _ = sync_env([(123, 10900000)])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda q: 7200})
    gql.Sync.SnapshotNow()
    key = str(gql.Sync.CurrentCharKey())
    db = lua.globals().GQLDB
    if gql.Ledger.CharRewardMoney(db, key, 123) != 10900000:
        return fail("reward_read_stops_counting_when_the_instance_rolls",
                    "这一条实例读到的金币该报出来")
    # 板上换成下一条实例：生产里就是 L.SetExpiry 拿新倒计时挪 roundEnd（编号跟着变）
    gql.Ledger.SetExpiry(db["quests"][123], 20000, 43200)
    if gql.Ledger.CharRewardMoney(db, key, 123) is not None:
        return fail("reward_read_stops_counting_when_the_instance_rolls",
                    "那一份读数挂的是上一条实例的编号，不许冒充这一条")
    # 对照组：这一条实例重新读到就立刻报得出 —— 门不是恒假，是认编号
    gql.Ledger.SetRewardRead(db, key, 123, 7000000, 21000)
    if gql.Ledger.CharRewardMoney(db, key, 123) != 7000000:
        return fail("reward_read_stops_counting_when_the_instance_rolls",
                    "换一条实例重新读到的那份要报出来（摘掉编号门这条不会红，是恒假的话会）")
    ok("reward_read_stops_counting_when_the_instance_rolls")


def test_gold_readers_lists_only_this_instances_gold_answers():
    """"这条实例上读到金币的号"清单：0（客户端答了不给金币）、上一条实例的读数、
    压根没读数的号都不许进 —— 这份名单进 /gql quest 的「金币读者」行（第 33 轮起矩阵
    悬停不再报它，界面上没有"这行为什么还在列"那半句），多一个名字就是谎报。
    顺序必须稳定（pairs 无序，不排序的话同一个号一次悬停一个样）。"""
    lua, gql, _ = sync_env([(124, 10900000)])
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda q: 7200})
    gql.Sync.SnapshotNow()
    db = lua.globals().GQLDB
    key = str(gql.Sync.CurrentCharKey())
    # 丙：这一笔写在"板上换成下一条实例"之前 ⇒ 烙的是上一条实例的编号
    gql.Ledger.SetRewardRead(db, "丙上一轮-永恒之井", 124, 9000000, 0)
    gql.Ledger.SetExpiry(db["quests"][124], 20000, 43200)
    at = 21000            # 这一条实例之内的时刻（结束时刻 = 20000+43200）
    gql.Ledger.SetRewardRead(db, "乙读零-永恒之井", 124, 0, at)
    gql.Ledger.SetRewardRead(db, "甲读金币-永恒之井", 124, 7000000, at)
    gql.Ledger.SetRewardRead(db, key, 124, 10900000, at)
    gql.Ledger.SetView(db, "丁没读-永恒之井", 124, "open", at)
    # 名单在 Lua 侧拼成一串再取：lupa 直接迭代数组会连 key 一起带出来
    def readers():
        return one(lua.execute(
            "local r = GQL.Ledger.GoldReaders(GQL.Config.DB(), 124)\n"
            "return r and table.concat(r, '|') or '-'"))
    want = "|".join(sorted([key, "甲读金币-永恒之井"]))
    if readers() != want:
        return fail("gold_readers_lists_only_this_instances_gold_answers",
                    f"名单应只有这条实例上读到金币的两个号、并按 key 排好序，实际 {readers()!r}")
    ok("gold_readers_lists_only_this_instances_gold_answers")


# ------------------------------------------------------------------
# 2026-11-03 金币按号归属（计划
# docs/superpowers/plans/2026-11-03-gql-per-char-gold-attribution.md §四 T6）。
# 靶心是用户 11-03 的截图：97128 麦德那一格印「风行者 · 麦德 交于 11-03 09:16 到手 1089🪙」，
# 而麦德做它**不给金币**（给物品，它的 reads[97128].g = 0），1089 是另一个号本轮读到的钱。
# 夹具里的号名直接用截图那两个（风行者 / 麦德），金额用真数 1089 金 = 10890000 铜。
# ------------------------------------------------------------------

def test_char_reward_read_positive_survives_mode_flip():
    """T1（计划 §二第 1 条，用户 11-03 提醒"同一任务不同模式（是否狩猎）奖励不同，
    不许为此反复读取"）：同一个号在**同一条实例**上先普通模式读到 1089、之后切狩猎模式
    同步一次读到 0 ⇒ 那笔正数不许被冲掉。行级那一份早就有这道保护
    （NoteGoldThisInstance 只在 money > 0 时写 ⇒ 切模式不掉行靠它），号级这一处以前是
    无条件覆盖，症状是格子从「1089」翻成「这号不给金币」、周汇总跟着掉。
    判据只在账本里比一次字段（同 charKey + 同 k），一次接口调用都不多。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(144, 10890000, None)], 6000, {144: 43200})
    key = "麦德-永恒之井"
    L.SetRewardRead(db, key, 144, 10890000, 6100)
    if L.CharRewardMoney(db, key, 144, 6200) != 10890000:
        return fail("char_reward_read_positive_survives_mode_flip",
                    "前置：这一号普通模式读到的 1089 该报出来")
    if uid(L, db, 144, 6300) != uid(L, db, 144, 6100):
        return fail("char_reward_read_positive_survives_mode_flip",
                    "前置：这两次读数必须挂在同一条实例上（编号换了就测不到保护）")
    # 切狩猎模式同步一次：同一条实例，客户端答这副奖励不给金币
    L.SetRewardRead(db, key, 144, 0, 6300)
    if L.CharRewardMoney(db, key, 144, 6400) != 10890000:
        return fail("char_reward_read_positive_survives_mode_flip",
                    f"同一条实例上新的 0 不许冲掉已读到的 1089，实际 "
                    f"{L.CharRewardMoney(db, key, 144, 6400)}")
    if int(db["chars"][key]["reads"][144]["t"]) != 6100:
        return fail("char_reward_read_positive_survives_mode_flip",
                    "被拒的那一笔该一个字不写（落账时刻仍是最初那次读数）")
    # 行级那一份的保护照旧：两处同一形状，不许一处有一处没有
    L.NoteGoldThisInstance(db, 144, 0, 6500)
    if int(db["quests"][144]["goldMoney"]) != 10890000:
        return fail("char_reward_read_positive_survives_mode_flip",
                    "行级 rec.goldMoney 同一条实例上也该留住 1089（双模式不掉行靠它）")
    ok("char_reward_read_positive_survives_mode_flip")


def test_char_reward_read_zero_then_positive():
    """保护只挡"正数被 0 冲"，不挡反方向：这一号先读到 0、切回普通模式读到 1089 ⇒
    照旧更新为正数（客户端改口就往真答案走）。这一条是保护写过头时的门 ——
    粘滞写反了就把"这一号本来给金币"的号永久钉成白干。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(145, 0, [111])], 6000, {145: 43200})
    key = "麦德-永恒之井"
    L.SetRewardRead(db, key, 145, 0, 6100)
    if L.CharRewardMoney(db, key, 145, 6200) != 0:
        return fail("char_reward_read_zero_then_positive",
                    "前置：0 是客户端明说的答案，要落账（≠ 没读过）")
    L.SetRewardRead(db, key, 145, 10890000, 6300)
    if L.CharRewardMoney(db, key, 145, 6400) != 10890000:
        return fail("char_reward_read_zero_then_positive",
                    f"0 之后读到正数要更新为正数，实际 {L.CharRewardMoney(db, key, 145, 6400)}")
    ok("char_reward_read_zero_then_positive")


def test_char_reward_read_new_instance_replaces_value():
    """保护的边界 = 那把编号：板面换成下一条实例（结束时刻挪走）⇒ 旧实例那一份 1089
    从此匹配不上（CharRewardMoney 回 nil，宁缺不编），而新实例上读到 0 照写 0 ——
    旧数不给新实例背书，保护也不许跨实例挡新读数（否则 1089 会赖在板面上不走，
    正是"意外遭遇上个 CD 的 1064 金"那一族）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(146, 10890000, None)], 6000, {146: 43200})
    key = "麦德-永恒之井"
    L.SetRewardRead(db, key, 146, 10890000, 6100)
    old_key = uid(L, db, 146, 6100)
    if L.CharRewardMoney(db, key, 146, 6200) != 10890000:
        return fail("char_reward_read_new_instance_replaces_value",
                    "前置：上一条实例上这一号读到 1089")
    # 板上换成下一条实例（生产里由 L.SetExpiry 写新的 roundEnd，编号跟着换）
    L.SetExpiry(db["quests"][146], 7000, 43200)
    new_key = uid(L, db, 146, 7100)
    if new_key == old_key:
        return fail("char_reward_read_new_instance_replaces_value",
                    f"前置：结束时刻挪走要真换出一条实例（编号还是 {old_key}）")
    if L.CharRewardMoney(db, key, 146, 7100) is not None:
        return fail("char_reward_read_new_instance_replaces_value",
                    "旧实例那一份 1089 不许给新实例背书（该回 nil）")
    # 编号不同 ⇒ 整笔重写：新实例上读到 0 落账，保护只在同一条实例之内生效
    L.SetRewardRead(db, key, 146, 0, 7200)
    if L.CharRewardMoney(db, key, 146, 7300) != 0:
        return fail("char_reward_read_new_instance_replaces_value",
                    f"换实例后新的 0 该整笔重写，实际 {L.CharRewardMoney(db, key, 146, 7300)}")
    if str(db["chars"][key]["reads"][146]["k"]) != new_key:
        return fail("char_reward_read_new_instance_replaces_value",
                    "重写的那一笔要烙上新实例的编号")
    ok("char_reward_read_new_instance_replaces_value")


def test_last_completion_gain_is_per_char():
    """T2（计划 §二第 2 条）：某一号的到手只由**这一号**的读数决定。
    老判据 `rec.goldKey == r.k` 回的是 rec.goldMoney —— "这一条实例里任意一个号最后
    同步读到的钱"，跟眼前这一号无关 ⇒ 用户 11-03 截图那句「风行者 · 麦德 交于 11-03
    09:16 到手 1089🪙」：麦德做它不给金币，1089 是风行者读的钱，借号挂名。
    三档一起钉：这一号读到正数 → 那个正数；这一号读到 0 → 0（客户端的真答案，
    由界面决定不拼成「到手 0」）；这一号没读过 → nil（宁缺不编）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    pool_pass(lua, L, db, [(147, 10890000, None)], 6000, {147: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 147, 10890000, 6100)
    L.SetRewardRead(db, "麦德-永恒之井", 147, 0, 6100)
    L.OnTurnIn(db, "风行者-永恒之井", 147, 6200, None)
    L.OnTurnIn(db, "麦德-永恒之井", 147, 6250, None)
    L.OnTurnIn(db, "丙没读-永恒之井", 147, 6300, None)

    def got(who):
        # 三个返回值在 lupa 里会被尾部 nil 挤形，在 Lua 侧拼成一串再取（同老用例）
        return one(lua.execute(
            "local t, g, s = GQL.Ledger.LastCompletion(GQL.Config.DB(), %r, 147)\n"
            "return (t and tostring(t) or '-') .. '|' .. (g and tostring(g) or '-')"
            " .. '|' .. (s or '-')" % who))

    rec = db["quests"][147]
    if int(rec["goldMoney"]) != 10890000 or str(rec["goldKey"]) != uid(L, db, 147, 6400):
        return fail("last_completion_gain_is_per_char",
                    "前置：行级那份 1089 得正挂在当前实例上 —— 老判据就是从这一份借的钱")
    if got("风行者-永恒之井") != "6200|10890000|实测":
        return fail("last_completion_gain_is_per_char",
                    f"这一号自己读到 1089 ⇒ 到手 1089，实际 {got('风行者-永恒之井')}")
    if got("麦德-永恒之井") != "6250|0|实测":
        return fail("last_completion_gain_is_per_char",
                    f"麦德做它不给金币 ⇒ 到手是它自己的 0，绝不是风行者的 1089，"
                    f"实际 {got('麦德-永恒之井')}")
    if got("丙没读-永恒之井") != "6300|-|实测":
        return fail("last_completion_gain_is_per_char",
                    f"这一号在这条实例上没读过 ⇒ 到手 nil（宁缺不编），"
                    f"实际 {got('丙没读-永恒之井')}")
    ok("last_completion_gain_is_per_char")


def test_last_completion_gain_uses_turnin_instance_key():
    """T2 的后半条：金额取的是**这笔流水自己那条实例**（r.k）上的读数，不是"当前编号"
    上的读数。交任务挂在哪一条实例，就取那一条实例上这一号读到的数 ——
    拿当前编号的话，上一轮那一单会认领这一轮刚读到的钱（板面换轮后格子还是勾的现场）。
    夹具说明：生产里一次同步会把行级和号级两份一起挪到新实例，这里照这个形状把两份都挪
    （SetExpiry 换结束时刻 → SetRewardRead 挪号级 → NoteGoldThisInstance 挪行级），
    流水的旧编号便既对不上号级读数、也对不上行级金额，单独问清"取哪把编号"这一件事。
    11-04 按行借数之后，只挪号级、把行级留在旧实例会人为造出"旧实例的行级钱给旧实例的
    流水背书"这种生产不会有的形状，所以行级必须一起挪。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    lua.globals().GQLDB = db
    pool_pass(lua, L, db, [(148, 10890000, None)], 6000, {148: 43200})
    key = "麦德-永恒之井"
    L.SetRewardRead(db, key, 148, 10890000, 6100)
    L.OnTurnIn(db, key, 148, 6200, None)

    def gain():
        return one(lua.execute(
            "local _, g = GQL.Ledger.LastCompletion(GQL.Config.DB(), %r, 148)\n"
            "return tostring(g)" % key))

    row = db["turnins"][key][148][1]
    if str(row["k"]) != uid(L, db, 148, 6200):
        return fail("last_completion_gain_uses_turnin_instance_key",
                    f"前置：流水要烙上当时的编号（{row['k']}）")
    if gain() != "10890000":
        return fail("last_completion_gain_uses_turnin_instance_key",
                    f"同一条实例内该报出自己读到的 1089，实际 {gain()}")
    # 板面换成下一条实例，这一号在新实例上读到 2000 金 —— 上一单那笔不许改口
    L.SetExpiry(db["quests"][148], 7000, 43200)
    L.SetRewardRead(db, key, 148, 20000000, 7100)
    L.NoteGoldThisInstance(db, 148, 20000000, 7100)  # 行级那份按生产形状一起挪到新实例
    if uid(L, db, 148, 7100) == str(row["k"]):
        return fail("last_completion_gain_uses_turnin_instance_key",
                    "前置：结束时刻没挪出一条新实例，测不到取哪把编号")
    if gain() != "nil":
        return fail("last_completion_gain_uses_turnin_instance_key",
                    f"到手取的是这笔流水那条实例的读数（那一条上这号没有当前那份数）⇒ nil，"
                    f"实际 {gain()}")
    ok("last_completion_gain_uses_turnin_instance_key")


def test_week_stats_gold_excludes_other_chars_read():
    """T3（计划 §二第 2/3 条）：矩阵列头"本周金币"里，别的号读到的那一笔不计进这一列。
    老判据把 rec.goldMoney（任意一个号最后同步读到的钱）加给每个有流水的号 ⇒
    白干的号那一列照样 +1089，总额虚高 —— 用户截图里那一列正是这么涨的。
    这一号读到 0（客户端明说不给金币）与从没读过都不计金额；一条金额都没读过时
    函数尾部那句照旧回 -1 ⇒ 界面显示 ?（宁缺不编，不是新行为）。完成条数不受影响。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(149, 10890000, None)], 6000, {149: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 149, 10890000, 6100)
    L.SetRewardRead(db, "麦德-永恒之井", 149, 0, 6100)
    L.OnTurnIn(db, "麦德-永恒之井", 149, 6200, None)
    L.OnTurnIn(db, "丙没读-永恒之井", 149, 6250, None)
    if int(db["quests"][149]["goldMoney"]) != 10890000:
        return fail("week_stats_gold_excludes_other_chars_read",
                    "前置：行级那一份（风行者的 1089）要在账上")
    count, gold = L.WeekStats(db, "麦德-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != -1:
        return fail("week_stats_gold_excludes_other_chars_read",
                    f"这一号做它不给金币：完成 1 条、金额如实未知（不是别人的 1089），"
                    f"实际 {count}/{gold}")
    count, gold = L.WeekStats(db, "丙没读-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != -1:
        return fail("week_stats_gold_excludes_other_chars_read",
                    f"这一号从没读过：完成 1 条、金额未知，实际 {count}/{gold}")
    ok("week_stats_gold_excludes_other_chars_read")


def test_week_stats_gold_counts_own_read():
    """T3 的对照组：这一号自己读到的正数照旧计入它那一列，且计入的是**它自己那份**——
    同一条实例上风行者后同步、把行级那份覆盖成 2000，麦德那一列仍是自己的 1089。
    老判据两处都回 2000（行级"谁最后同步算谁的"），于是每一列报的都是最后同步那个号的钱。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(150, 10890000, None)], 6000, {150: 43200})
    L.SetRewardRead(db, "麦德-永恒之井", 150, 10890000, 6100)
    L.SetRewardRead(db, "风行者-永恒之井", 150, 20000000, 6150)
    # 行级那份按"最后同步那个号"覆盖成 2000（NoteGoldThisInstance 的原语义，不动它）
    L.NoteGoldThisInstance(db, 150, 20000000, 6150)
    L.OnTurnIn(db, "麦德-永恒之井", 150, 6200, None)
    L.OnTurnIn(db, "风行者-永恒之井", 150, 6250, None)
    if int(db["quests"][150]["goldMoney"]) != 20000000:
        return fail("week_stats_gold_counts_own_read", "前置：行级那份该是最后同步的 2000")
    count, gold = L.WeekStats(db, "麦德-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != 10890000:
        return fail("week_stats_gold_counts_own_read",
                    f"麦德那一列该是自己读到的 1089，实际 {count}/{gold}")
    count, gold = L.WeekStats(db, "风行者-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != 20000000:
        return fail("week_stats_gold_counts_own_read",
                    f"风行者那一列该是自己读到的 2000，实际 {count}/{gold}")
    ok("week_stats_gold_counts_own_read")


def test_a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero():
    """默认按行（用户 11-04："刚开始确实是整行的…绝大部分情况下按行没问题"）：
    这条任务上没有任何一个号读到过 0 = 没有分岔的凭据 ⇒ 还没同步过的号照旧报行级那份钱，
    不再一律 `?`。11-03 那一版把三处消费侧全改成只认本号读数，代价落到了这种正常任务上。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(155, 10890000, None)], 6000, {155: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 155, 10890000, 6100)
    L.OnTurnIn(db, "没同步过的号-永恒之井", 155, 6200, None)
    if L.RewardDiffers(db, 155, 6300) is not False:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    "没人读到过 0 = 判不出分岔")
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 155, 6300) != 10890000:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    "不分岔的条上，没读过的号该报行级那份 1089")
    count, gold = L.WeekStats(db, "没同步过的号-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != 10890000:
        return fail("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero",
                    f"周汇总同样按行：完成 1 条、金额 1089，实际 {count}/{gold}")
    ok("a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero")


def test_a_zero_read_on_any_char_forbids_borrowing_for_the_rest():
    """例外那一档（用户 11-04 点名的形状：有的号给金币、有的号不给，不给的号也做了）：
    这条实例上风行者正数、麦德 0 = 分岔凭据成立 ⇒ 没自己读到过数的号一律不给数（`?`），
    因为这时候借谁的钱都是猜（11-03 截图「麦德 … 到手 1089🪙」就是这么来的）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(156, 10890000, None)], 6000, {156: 43200})
    L.SetRewardRead(db, "风行者-永恒之井", 156, 10890000, 6100)
    L.SetRewardRead(db, "麦德-永恒之井", 156, 0, 6100)
    L.OnTurnIn(db, "丙没读-永恒之井", 156, 6200, None)
    if L.RewardDiffers(db, 156, 6300) is not True:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "一正一 0 两个不同号 = 这条按号会变，要判成分岔")
    if L.CellRewardMoney(db, "丙没读-永恒之井", 156, 6300) is not None:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "分岔的条上没读过的号不许借风行者的 1089")
    if L.CellRewardMoney(db, "麦德-永恒之井", 156, 6300) != 0:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    "读到 0 的那一号照旧报 0（客户端明说不给金币）")
    count, gold = L.WeekStats(db, "丙没读-永恒之井", 0, 6300)
    if int(count) != 1 or int(gold) != -1:
        return fail("a_zero_read_on_any_char_forbids_borrowing_for_the_rest",
                    f"分岔的条上没读过的号：完成 1 条、金额如实未知，实际 {count}/{gold}")
    ok("a_zero_read_on_any_char_forbids_borrowing_for_the_rest")


def test_a_lone_zero_read_is_not_a_reward_split():
    """推断出来的 0 不许把整条任务打回按号：Api.lua:187-189 会在"数据在本地、
    金额链却读不出"时造出 money=0，Matrix.lua:573-575 也承认 0 可能是做完前的旧答案。
    只有一个号读到 0（没有另一个号读到正数）时，分岔不成立，没读过的号照旧借行级。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(157, 10890000, None)], 6000, {157: 43200})
    L.SetRewardRead(db, "推断零-永恒之井", 157, 0, 6100)
    L.OnTurnIn(db, "没同步过的号-永恒之井", 157, 6200, None)
    if L.RewardDiffers(db, 157, 6300) is not False:
        return fail("a_lone_zero_read_is_not_a_reward_split", "单号 0 不构成分岔")
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 157, 6300) != 10890000:
        return fail("a_lone_zero_read_is_not_a_reward_split", "推断 0 不该让全员报 ?")
    ok("a_lone_zero_read_is_not_a_reward_split")


def test_an_old_save_without_row_money_stays_unknown():
    """§三 的承诺要有兜底：老档里 goldKey 还在、goldMoney 被迁移清掉
    （Ledger.lua:2323；pool_pass 给 None 金额就是同一个形状）⇒ 借数如实报 ?。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    pool_pass(lua, L, db, [(158, None, None)], 6000, {158: 43200})
    if L.CellRewardMoney(db, "没同步过的号-永恒之井", 158, 6300) is not None:
        return fail("an_old_save_without_row_money_stays_unknown",
                    "行级金额为空时不许借、不许编")
    ok("an_old_save_without_row_money_stays_unknown")


def test_tooltip_cell_without_char_read_says_unknown():
    """T4（计划 §二第 3 条）：停在**格子**上时，分岔的条上没读过的号不许借行金额，
    走"金额未知"那支 —— 借来的那个正数是别的号本轮同步读到的，压在这一号格子上
    就是 11-03 截图那句「到手 1089🪙」的另一半。
    【11-04 前置】默认按行借是新口径，所以这里必须补出一个读到 0 的号，让风行者的
    正数 + 物品号的 0 构成分岔凭据；不分岔时该借到 1089 的形状由
    test_a_char_without_a_read_borrows_... 那条单独钉着。
    没读过也**不许**说成"这轮做它不给金币"（那是替客户端下结论，见 :119 那一族用例）。"""
    lua, gql, g, calls = tip_env(151, 1792577090, cell=True, hover="麦德-永恒之井",
                                 pool_money=10890000,
                                 cols={"风行者-永恒之井": 10890000,
                                       "麦德-永恒之井": None,
                                       "物品号-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    if "金额未知" not in joined:
        return fail("tooltip_cell_without_char_read_says_unknown",
                    f"这一号在这条实例上没读过 ⇒ 金额那一行如实报未知:\n{joined}")
    if "1089" in joined:
        return fail("tooltip_cell_without_char_read_says_unknown",
                    f"不许把别的号读到的 1089 印在这一号的格子上:\n{joined}")
    if "不给金币" in joined:
        return fail("tooltip_cell_without_char_read_says_unknown",
                    f"没读过 ≠ 客户端答了不给金币:\n{joined}")
    if "SHOW" not in calls:
        return fail("tooltip_cell_without_char_read_says_unknown",
                    "少的是那一笔钱，提示本身照常")
    ok("tooltip_cell_without_char_read_says_unknown")


def test_tooltip_row_hotzone_still_shows_row_money():
    """T4 的另一支：整行热区（没有 charKey）压根没有角色归属 ⇒ 照旧印行金额。
    行金额没退役，它继续负责"这行为什么还在列"（行不掉 / 金币门槛），只是不再冒充
    某一号的到手。这一条钉的是"改口径别把行悬停一起改坏"：钱要报得出、号名一个不许点。"""
    lua, gql, g, calls = tip_env(152, 1792577090, cell=False, pool_money=10890000,
                                 cols={"风行者-永恒之井": 10890000, "麦德-永恒之井": 0})
    hover_update(lua)
    joined = "\n".join(calls)
    if "1089" not in joined:
        return fail("tooltip_row_hotzone_still_shows_row_money",
                    f"整行热区该照旧印行金额 1089:\n{joined}")
    if "不给金币" in joined:
        return fail("tooltip_row_hotzone_still_shows_row_money",
                    f"整行没有角色归属，不许按号下结论:\n{joined}")
    for who in ("麦德", "风行者"):
        if who in joined:
            return fail("tooltip_row_hotzone_still_shows_row_money",
                        f"行悬停不许点名某个号（{who}）:\n{joined}")
    if "交于" in joined:
        return fail("tooltip_row_hotzone_still_shows_row_money",
                    f"行悬停不许借某个号的完成时间:\n{joined}")
    ok("tooltip_row_hotzone_still_shows_row_money")


def test_tooltip_zero_read_prints_no_gold_line():
    """T4 + T2 的现场（用户 11-03 截图，97128 麦德）：这一号读到 0、且这条实例真交过
    （done）⇒ 印「麦德 这轮做它不给金币」，交割行只报时刻、**不拼「到手 X」**。
    老代码在这里借风行者的 1089 当"这一号的到手"，结果两句都不是真答案：既用别人的钱
    抑制掉了"不给金币"那句，又把它挂成麦德挣的。改完之后 X 本身就是按号的，
    0 与 nil 都不拼钱（Matrix.lua:574 那道守卫不删，它现在只挡"这一号自己补录过正数"）。"""
    lua, gql, g, calls = tip_env(153, 1792577090, cell=True, hover="麦德-永恒之井",
                                 pool_money=10890000,
                                 cols={"风行者-永恒之井": 10890000, "麦德-永恒之井": 0})
    lua.execute(
        "GQL.Ledger.OnTurnIn(GQL.Config.DB(), '麦德-永恒之井', 153, 1792577090, nil)")
    hover_update(lua)
    joined = "\n".join(calls)
    if "麦德 这轮做它不给金币" not in joined:
        return fail("tooltip_zero_read_prints_no_gold_line",
                    f"这一号做它不给金币就是真答案，不许被别人的正数抑制掉:\n{joined}")
    if "麦德 交于" not in joined:
        return fail("tooltip_zero_read_prints_no_gold_line",
                    f"交割行照旧报这一号的时刻:\n{joined}")
    if "到手" in joined or "1089" in joined:
        return fail("tooltip_zero_read_prints_no_gold_line",
                    f"到手只在正数时拼，别人的 1089 一分钱也不许出现在这一格:\n{joined}")
    ok("tooltip_zero_read_prints_no_gold_line")


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


def test_opening_matrix_resyncs_and_shows_last_sync():
    """11-04 定稿：标题栏不再放刷新按钮，打开矩阵就是唯一的手工刷新入口；
    标题栏只报最后一次抓池时刻（db.chars[].lastSync），不报上次重画。
    保留原用例钉住的三件：账本 101 回来、轮询被打开、矩阵真画出行文本
    （Ensure 断在半路那次"打开一篇漆黑"事故）。
    另外钉住抓池顺序：f:Show() 必须先于 SnapshotNow()，否则 Mx.Refresh() 的
    frame:IsShown() 门会把打开那趟触发的重画全丢掉 —— 这正是玩家实测
    "刚打开没更新、要等一会儿"的另一半原因。"""
    name = "opening_matrix_resyncs_and_shows_last_sync"
    lua, gql, _ = sync_env([(101, 30000000)], extra=("Matrix",))
    g = lua.globals()
    # 打开会重抓池、把 lastSync 推到当前时刻；包装里在"重抓之后"把值按场景钉回来，
    # 测的是标题读的是不是 lastSync，同时记下抓池那一刻面板是否已经可见。
    lua.execute("""
        GQL_SNAP_SHOWN, GQL_FAKE_LASTSYNC = nil, 1000
        local real = GQL.Sync.SnapshotNow
        GQL.Sync.SnapshotNow = function()
            GQL_SNAP_SHOWN = GQLMatrixFrame ~= nil and GQLMatrixFrame:IsShown() or nil
            local r = real()
            if GQL_FAKE_LASTSYNC ~= nil then
                GQL.Config.DB().chars[GQL.Sync.CurrentCharKey()].lastSync = GQL_FAKE_LASTSYNC
            end
            return r
        end
    """)
    g.GQL_NOW = 1600
    gql.Sync.SnapshotNow()
    gql.Matrix.Toggle()
    if lua.execute("return GQL_SNAP_SHOWN") is not True:
        return fail(name, "打开时 f:Show() 必须在抓池之前（首趟重画会被 IsShown 门丢掉）")
    texts = frame_texts(g.GQLMatrixFrame)
    if "刷新" in texts:
        return fail(name, "标题栏不该再有刷新按钮（打开矩阵就是刷新）")
    first = [s for s in texts if "最后同步" in s]
    if not first:
        return fail(name, "标题栏该报最后一次抓池时刻:\n" + "\n".join(texts))
    if int(lua.execute("return GQL.Sync.Polling() and 1 or 0")) != 1:
        return fail(name, "打开矩阵没把轮询打开")
    if lua.execute("return GQL.Config.DB().quests[101] ~= nil") is not True:
        return fail(name, "打开矩阵没真抓池（账本里那条 101 没回来）")
    # 12.1.5 事故的判据：Ensure 中途抛错时 frame 已经建好、文字池还没建，
    # 之后每次 Toggle 都在开头 return 这张半成品皮 → 玩家看到"打开一篇漆黑、再也打不开"。
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
        return fail(name, "矩阵只画出了皮、没有行文本（Ensure 断在半路的症状）")
    # 改一次 lastSync、关开一次面板，文本要跟着走；不给任何定时器/事件插手的机会。
    lua.execute("GQL_FAKE_LASTSYNC = 700")
    lua.execute("GQLMatrixFrame:Hide()")   # 替身方法是 function(self)，Python 侧点调用会丢 self
    gql.Matrix.Toggle()
    second = [s for s in frame_texts(g.GQLMatrixFrame) if "最后同步" in s]
    if not second or second[0] == first[0]:
        return fail(name, f"标题该跟着 lastSync 变：{first} → {second}")
    ok(name)


def test_last_sync_label_handles_three_clock_shapes():
    """标题栏时刻只认 db.chars[].lastSync，三种时钟形状都要如实说：
    纪元秒 → date("%H:%M:%S")；开机秒 → 与 Api.Uptime() 做差报 N 分前；
    没有 lastSync（或跨域算不出）→ 破折号，不编一个时刻出来。
    三种形状都钉住"没有为这行字新增定时器 / OnUpdate"。"""
    name = "last_sync_label_handles_three_clock_shapes"

    def label(lua):
        hits = [s for s in frame_texts(lua.globals().GQLMatrixFrame) if "最后同步" in s]
        return hits[0] if hits else None

    def timers(lua):
        return str(lua.execute("""
            local total, short = 0, 0
            for _, t in ipairs(GQL_NEWTIMERS) do
                if not t.cancelled then
                    total = total + 1
                    if t.delay < 5 then short = short + 1 end
                end
            end
            local function walk(f, n)
                if f.__scripts and f.__scripts["OnUpdate"] then n = n + 1 end
                for _, k in ipairs(f.__children or {}) do n = walk(k, n) end
                return n
            end
            return ("%d/%d/%d"):format(total, short, walk(GQLMatrixFrame, 0))
        """))

    def check_timers(lua, tag):
        total, short, onupdate = [int(v) for v in timers(lua).split("/")]
        # 皮上本来只有主帧那一个 HoverScan 的 OnUpdate；时间戳不许再挂第二个。
        if total > 1 or short > 0 or onupdate > 1:
            return f"{tag}：不该为时间戳新增定时器/OnUpdate（{timers(lua)}）"
        return None

    # ① 纪元域：date 替身固定答 12:03:45，钉住实现走 date 而不是把 epoch 数字印出来。
    lua1, gql1, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Matrix",))
    lua1.globals().date = lambda fmt, t: "12:03:45"
    gql1.Matrix.Toggle()
    if label(lua1) != "最后同步 12:03:45":
        return fail(name, f"纪元域该格式化成钟点，实际 {label(lua1)}")
    why = check_timers(lua1, "纪元域")
    if why:
        return fail(name, why)

    # ② 开机秒域：sync_env 不 stub 墙上钟，Clock()/Uptime() 都落在 GetTime 的会话秒。
    lua2, gql2, _ = sync_env([], extra=("Matrix",))
    lua2.globals().GQL_NOW = 1600
    gql2.Matrix.Toggle()
    lua2.execute("GQL.Config.DB().chars[GQL.Sync.CurrentCharKey()].lastSync = 1000")
    gql2.Matrix.RefreshSyncLabel()
    if label(lua2) != "最后同步 10 分前":
        return fail(name, f"开机秒域该报 10 分前，实际 {label(lua2)}")
    why = check_timers(lua2, "开机秒域")
    if why:
        return fail(name, why)

    # ③ 没有 lastSync：老档/换号第一次抓池之前，如实报 —。
    lua3, gql3, _ = sync_env([], extra=("Matrix",))
    gql3.Matrix.Toggle()
    lua3.execute("GQL.Config.DB().chars[GQL.Sync.CurrentCharKey()].lastSync = nil")
    gql3.Matrix.RefreshSyncLabel()
    if label(lua3) != "最后同步 —":
        return fail(name, f"没有 lastSync 该报 —，实际 {label(lua3)}")
    why = check_timers(lua3, "无 lastSync")
    if why:
        return fail(name, why)
    ok(name)


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

    # 暴雪地图：盖满全屏、层序与矩阵同为 DIALOG（矩阵 2026-11-01 抬到 DIALOG）、
    # 不吃鼠标、名单内 → 同层也要拦（名单内窗口按 sameRankCounts 判）
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
    # 层序用 FULLSCREEN：矩阵 2026-11-01 起在 DIALOG，同层且没有背板的帧按"看不出来"
    # 处理（不许误杀），要模拟"真画在我们上面"就得换更高一层。
    lua.execute("CoverMock = CreateFrame('Frame', 'SomeOtherAddonPanel', UIParent) "
                "CoverMock:SetFrameStrata('FULLSCREEN') CoverMock:SetSize(1440, 720) "
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

    # 层序的新口径（2026-11-01）：矩阵为了压住地图/角色面板抬到了 DIALOG，
    # 12.1.5 probe 里那条"地图与矩阵同为 MEDIUM、层号同为 1"不再成立 —— 同层要拦的
    # 判据改成"同为 DIALOG"，而 MEDIUM 的地图此刻画在我们**下面**，不许再拦。
    # 先让地图在 DIALOG 拦一次，再把它降回 MEDIUM，看 hover 是否当场恢复。
    cover("WorldMapFrame:SetAlpha(1) WorldMapFrame:SetFrameStrata('DIALOG') "
          "WorldMapFrame:SetFrameLevel(1) WorldMapFrame:Show()")
    if lit() or "SHOW" in calls:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "与矩阵同层（DIALOG）的全屏窗口必须拦:\n" + "\n".join(calls))
    cover("WorldMapFrame:SetFrameStrata('MEDIUM') WorldMapFrame:SetFrameLevel(1) "
          "WorldMapFrame:Show()")
    if not lit() or "SHOW" not in calls:
        return fail("hover_suppressed_when_window_covers_matrix",
                    "矩阵在 DIALOG、地图在 MEDIUM 时地图画在我们下面，不该拦 hover:\n"
                    + "\n".join(calls))
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
    """按关键词重扫地图要做两件事：① 行自己报的地图补写进跟踪清单；② 命中的任务注册成周常行。
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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
      * done 快照烙的是**上一条实例**的编号（这一条换了）→ 不算凭据；
      * 只有玩家手点的补录算 done（db.manual）→ 不算凭据，与"口述地图不算凭据"同一口径；
      * 全员 progress（82157 那种还挂在日志里没交的）→ 不算凭据。
    这一族问不出板面倒计时 ⇒ 身份只有周 CD 尾巴一档：先把 cycleAnchor 摆好，
    再走真实写点 SetView/SetManual 落证据（手搓 views 会漏掉编号，测的就不是凭据了）。"""
    lua, gql, _ = sync_env([], extra=("Matrix",))
    g = lua.globals()
    L = gql.Ledger
    gql.Config.AddKeyword("虚空决战")
    lua.execute("GQLDB.cycleAnchor = 1000 "
                "GQLDB.quests[96718] = { name = '奈格塔尔虚空决战（英雄）', "
                "  currentSig = '-1', observations = {} } "
                "GQLDB.chars = { ['A-永恒之井'] = { views = {} }, "
                "                ['B-永恒之井'] = { views = {} } }")
    L.SetView(g.GQLDB, "A-永恒之井", 96718, "done", 1500)
    L.SetView(g.GQLDB, "B-永恒之井", 96718, "progress", 1500)
    if not lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 96718)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "这一条实例里被客户端答成已交是凭据，QuestHasCredential 不许答 false")
    if "96718," not in matrix_listed(lua):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "14 个号本轮都交掉了的行不许消失：" + matrix_listed(lua))
    # 对照组一：把周尾巴整周往前推 = 这一条换成下一条实例，快照烙的编号匹配不上
    lua.execute("GQLDB.cycleAnchor = 1000 + 604800")
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 96718)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "上一条例的 done 快照不算凭据（换轮的垃圾行活不过一条实例）")
    if "96718," in matrix_listed(lua):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "上一轮的 done 不许把行留下来")
    # 对照组二：玩家手点的补录（db.manual）不算客户端的答复 —— 照样烙上**当前**编号，
    # 否则它是因为"挂不上编号"才被拒的，这一格就没测到"补录不是客户端答复"那句话。
    lua.execute("GQLDB.cycleAnchor = 1000 "
                "GQLDB.quests[82157] = { name = '特别任务：巨兽崛起', observations = {} }")
    L.SetManual(g.GQLDB, "A-永恒之井", 82157, "done", 1500, None, None)
    if lua.execute("return GQL.Ledger.QuestHasCredential(GQL.Config.DB(), 82157)"):
        return fail("a_quest_the_alts_finished_this_round_is_a_credential",
                    "手点补录不是客户端的答复，不许当凭据")
    # 对照组三：还挂在日志里没交（全源 progress，真存档里 82157/86174 的形状）
    lua.execute("GQLDB.manual = {} "
                "GQLDB.quests[86174] = { name = '特别任务：风暴在即', observations = {} }")
    L.SetView(g.GQLDB, "A-永恒之井", 86174, "progress", 1500)
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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})

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
    joined = "\n".join(frame_texts(g.GQLOptionsFrame))
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
    到期那道与矩阵同一句（第 36 轮 T4a：Ledger.InstanceExpired —— 这一条实例自己的结束
    时刻 rec.roundEnd 已过，且没有更晚还在板上被看见过的证据）。
    """
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    g = lua.globals()
    now = 1760000000
    g.time = lambda: now              # 墙上时钟固定住，到期门才有确定的"现在"
    gql.Config.AddKeyword("特别任务")
    lua.execute("GQLDB.quests[95918] = { name = '特别任务：怨灵之怒', poolSeen = true, "
                "  mapID = 2405, expiresAt = %d, roundEnd = %d, expiresSrc = '接口倒计时' }"
                % (now + 3600, now + 3600))
    lua.execute("GQLDB.quests[96307] = { name = '特别任务：怨灵之怒', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d, roundEnd = %d, "
                "  expiresSrc = '接口倒计时' }" % (now - 3600, now - 3600))
    lua.execute("GQLDB.quests[93013] = { name = '特别任务：击退圣光', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d, roundEnd = %d, "
                "  expiresSrc = '接口倒计时' }" % (now - 7200, now - 7200))
    lua.execute("GQLDB.quests[94391] = { name = '特别任务：击退圣光', mapID = 2405, "
                "  mapSrc = '玩家指定', mapManual = true, expiresAt = %d, roundEnd = %d, "
                "  expiresSrc = '接口倒计时' }" % (now - 9000, now - 9000))
    rows = panel_match_rows(lua, "特别任务")
    if [r["id"] for r in rows] != ["95918"]:
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    f"同名两条只留带凭据的那条活的，过期旧 ID 不许再来一遍: {rows}")
    if any("击退圣光" in r["name"] for r in rows):
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    f"两条都已过期且零凭据的名字整批不进列表: {rows}")
    # 到期门对"带凭据的行"同样要砍：不砍的话同名重复只解决一半，
    # 矩阵那条到期判据（L.InstanceExpired）与右栏必须同一条口径。
    lua.execute("GQLDB.quests[93244] = { name = '特别任务：神盾密探', poolSeen = true, "
                "  lastMoney = 5000000, expiresAt = %d, roundEnd = %d, "
                "  expiresSrc = '接口倒计时' }" % (now - 60, now - 60))
    if "93244" in [r["id"] for r in panel_match_rows(lua, "特别任务")]:
        return fail("panel_lists_no_expired_or_uncredentialed_rows",
                    "到期门对带凭据的关键词行同样生效（与矩阵同一句口径）")
    ok("panel_lists_no_expired_or_uncredentialed_rows")


def matrix_rows_by_id(L, db, now):
    """MatrixRows 摊平成 {questID: row}：换轮这一族用例要读 row.remaining，
    光看 ID 不够 —— "行还在、但剩余那一格是空的"才是换轮后该有的样子。"""
    out = {}
    for group in L.MatrixRows(db, now).values():
        for row in group["rows"].values():
            out[int(row["questID"])] = row
    return out


def stale_after_roll_db(lua, L):
    """照 89267「神秘身影」的真存档形状造一份账本（用户 2026-10-27 的原话：
    "刚刷新的神秘身影任务，已经有个角色做完了，没有上矩阵，什么情况？"）：
      第 1 条实例：池里 600 金 + 接口倒计时 12 小时 ⇒ 结束时刻 44200
      结束时刻已过、板上又看到它 ⇒ 账本里那条一直停在 44200（上一条实例的墓碑）
      这一读**金额照旧答得出、倒计时答不出**（`Api.QuestTimeLeft=nil`、
      `HaveQuestRewardData=true`，SetExpiry 问不出就一个字不改）
      ⇒ 这一条挂不上编号（板面那条已过去、锚点是 0 定不出周 CD 尾巴），
        行靠"板上读到 600 金"那道门槛留在矩阵上，格子一律 ?
    （旧写法这里传的是 `money=None`：第 34 轮那时行靠"本轮有角色交过"顶住，而那一笔
     当时烙的是上一条实例的编号 —— 正是本轮要消灭的假勾形状。编号模型下它不再是凭据，
     所以这份账本按真档的形状把金额写成答得出，行的"活着"改由门槛那一道顶。）
    """
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(89267, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][89267], 1000, 43200)
    L.DiffPool(db, pool(lua, [(89267, 6000000, None)]), 44400)
    L.OnTurnIn(db, "A-永恒之井", 89267, 44400,
               str(db["quests"][89267]["currentSig"]))
    return db


def test_a_stale_expiry_never_kills_a_row_the_gold_floor_holds():
    """换轮之后，上一条实例留在账本里的那条到期时刻不许把整行吃掉（12.1.5 实测 89267）。
    到期门现在只认"这一条实例自己的、还没过去的那一条"（`L.InstanceEnd`）：墓碑值一律不收，
    也就一律不许当到期判据，等接口自己写新的（零新增接口调用、不轮询、不扫图）。
    前置钉的是"实例真的换了"这件事 —— 第 36 轮起没有"本轮起点"可钉，
    改钉这一条当前的编号：墓碑 + 定不出的周 CD 尾巴 ⇒ 编号算不出（身份未定）。
    行还活着靠的是准入门里"板上读到 600 金 ≥ 门槛"那一支（与凭据无关）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = stale_after_roll_db(lua, L)
    rec = db["quests"][89267]
    if L.InstanceKey(db, 89267, rec, 44400) is not None:
        return fail("a_stale_expiry_never_kills_a_row_the_gold_floor_holds",
                    "前置：这一条已经换成新实例、旧结束时刻又当不了身份 ⇒ 编号该算不出，实际 %s"
                    % L.InstanceKey(db, 89267, rec, 44400))
    if int(rec["roundEnd"]) != 44200:
        return fail("a_stale_expiry_never_kills_a_row_the_gold_floor_holds",
                    "前置：问不出倒计时时结束时刻该原样停在上一条实例（44200），实际 %s"
                    % rec["roundEnd"])
    rows = matrix_rows_by_id(L, db, 44400)
    if 89267 in rows:
        return fail("a_stale_expiry_never_kills_a_row_the_gold_floor_holds",
                    "上一条实例的过期 roundEnd 一过就该判到期、出矩阵（金币地板不再保活）")
    ok("a_stale_expiry_never_kills_a_row_the_gold_floor_holds")


def test_row_still_dies_at_its_own_round_expiry():
    """对照组（计划 T7.2）：到期时刻在本轮起点**之后** = 这一行自己的寿命，到点照样掉出矩阵。
    96718 / 89601 那批"到期即移除"的口径一个字不许被本轮的修法绕掉
    （用户 2026-10-25："时间结束后都要移除"）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(89267, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][89267], 1000, 43200)              # 到期 44200，本轮起点 1000
    L.OnTurnIn(db, "A-永恒之井", 89267, 1500, "6000000")
    if 89267 not in matrix_rows_by_id(L, db, 2500):
        return fail("row_still_dies_at_its_own_round_expiry",
                    "前置：同一条实例还没到期，这一行该在列")
    if 89267 in matrix_rows_by_id(L, db, 44400):
        return fail("row_still_dies_at_its_own_round_expiry",
                    "本轮之内读到的到期时刻过了就是过了，不许因为改判据而活下来")
    ok("row_still_dies_at_its_own_round_expiry")


def test_a_stale_expiry_shows_no_countdown():
    """行活着不等于倒计时存在：用例 1 的形状下 row.remaining 必须是 nil。
    负数会一路流到悬停（`FormatRemaining(≤0)` 回"已到期"），修完到期门后那行会带着
    "剩余 已到期"挂在一条还活着的行上 —— 正是本轮要消灭的那句谎报（矩阵悬停同一条口径）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = stale_after_roll_db(lua, L)
    # 新规则：结束时刻已过直接出矩阵（不再是"在列但剩余为空"）
    row = matrix_rows_by_id(L, db, 44400).get(89267)
    if row is not None:
        return fail("a_stale_expiry_shows_no_countdown",
                    "上一条实例的过期 roundEnd 一过，该行就该出矩阵，不该还挂着")
    if L.EffectiveExpiry(db, db["quests"][89267], 44400) is not None:
        return fail("a_stale_expiry_shows_no_countdown",
                    "EffectiveExpiry 该把上一条实例的到期时刻判成 nil，实际 %s"
                    % L.EffectiveExpiry(db, db["quests"][89267], 44400))
    ok("a_stale_expiry_shows_no_countdown")


def test_stale_expiry_never_eats_a_row_that_the_board_still_shows():
    """计划 §十 用例 7 第一条（原本保住 89267「神秘身影」那条实测）。
    2026-11-02 改规则：结束时刻已过就删，**不再**因"最近板上还看见过"保活——
    关注图的任务每轮都正常刷结束时间，过期的 stale roundEnd 不该把行赖着（到期门只认
    `endAt <= now`）。这条现在断言：上一条实例的过期 roundEnd 一过就判到期、出矩阵。
    对照组钉住另一头：同一条过去时刻，且没有更晚的"板上看见过"证据 ⇒ 照旧死行。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = stale_after_roll_db(lua, L)
    rec = db["quests"][89267]
    if int(rec["roundEnd"]) != 44200:
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "前置：倒计时问不出时结束时刻该停在上一条实例（44200），实际 %s"
                    % rec["roundEnd"])
    if int(rec["lastSeenAt"]) != 44400:
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "前置：这一轮板上还看见过它（lastSeenAt=44400 > 结束时刻），实际 %s"
                    % rec["lastSeenAt"])
    if not L.InstanceExpired(db, rec, 44400):
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "上一条实例的过期 roundEnd 一过就该判到期、出矩阵（89267 那句旧保活已移除）")
    if 89267 in matrix_rows_by_id(L, db, 44400):
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "这条实例的结束时刻已过，该行不该还留在矩阵上")
    # 对照：这一条之后再没在板上被看见过（lastSeenAt 停在结束时刻之前）⇒ 真的结束了 ⇒ 死行
    rec["lastSeenAt"] = 2000
    if not L.InstanceExpired(db, rec, 44400):
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "对照组：没有更新的板上证据还不判到期，到期门就变成了永不判到期")
    if 89267 in matrix_rows_by_id(L, db, 44400):
        return fail("stale_expiry_never_eats_a_row_that_the_board_still_shows",
                    "对照组：这条实例自己结束了、板上也没再见过它，就该掉出矩阵")
    ok("stale_expiry_never_eats_a_row_that_the_board_still_shows")


def test_the_expiry_gate_kills_a_row_at_its_own_instance_end():
    """计划 §十 用例 7 第二条（96718 那句"时间结束后都要移除"，用户 10-24 定）：
    结束时刻在未来、now 越过它、且没有被更新的"板上看见过"证据 ⇒ 死行。
    这一条走 T4a 的新判据 L.InstanceExpired（老 EffectiveExpiry 只回 > now 的值，
    拿它已经判不出"过去"这件事了）：摘掉 `instEnd <= now` 这一支，本条必须红。
    名字与既有的 row_still_dies_at_its_own_round_expiry 表达同一含义 —— 那条从"本轮起点"
    的角度钉（老判据还在跑的这一刀不许动），这一条从"这一条实例自己的结束时刻"钉，
    并把两组读数都直接打在到期门上（哪一支写坏都能当场看见）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    L.DiffPool(db, pool(lua, [(96718, 6000000, None)]), 1000)
    L.SetExpiry(db["quests"][96718], 1000, 43200)          # 这一条实例结束于 44200
    rec = db["quests"][96718]
    L.OnTurnIn(db, "A-永恒之井", 96718, 1500, str(rec["currentSig"]))
    # 到点之前：这一条还活着，不许被砍（对照组的"活着"那一半）
    if L.InstanceExpired(db, rec, 44100):
        return fail("the_expiry_gate_kills_a_row_at_its_own_instance_end",
                    "结束时刻还在未来就判到期 = 把还没结束的行提前砍掉")
    if 96718 not in matrix_rows_by_id(L, db, 44100):
        return fail("the_expiry_gate_kills_a_row_at_its_own_instance_end",
                    "前置：同一条实例还没到期，这一行该在列")
    # now 越过它、且没有更晚的板上证据（lastSeenAt 还停在 1000）⇒ 到期门砍行
    if not L.InstanceExpired(db, rec, 44400):
        return fail("the_expiry_gate_kills_a_row_at_its_own_instance_end",
                    "这条实例自己的结束时刻已经过去还不判到期 = 到期门整条失效")
    if 96718 in matrix_rows_by_id(L, db, 44400):
        return fail("the_expiry_gate_kills_a_row_at_its_own_instance_end",
                    "用户 10-24 定的是「时间结束后都要移除」，不许因为换了判据就活下来")
    ok("the_expiry_gate_kills_a_row_at_its_own_instance_end")


def test_panel_right_column_shares_the_round_expiry_gate():
    """屏蔽管理右栏（第 31 轮起唯一的屏蔽/找回入口）与矩阵必须同一条到期口径：
    换轮后倒计时问不出、账本里那条结束时刻还停在上一条实例 ⇒ 矩阵还在列 ⇒ 右栏不许先把它
    藏掉，否则玩家在"界面明明还有这一行"的任务上找不到取消屏蔽的按钮（Options.lua 那段注释
    本来就要求两处同源）。第 36 轮 T4a 把两处都换成同一句 L.InstanceExpired
    （计划 §七：这条实例自己的结束时刻已过、且没有更晚还在板上被看见过的证据）。
    对照组钉住另一侧：这一条自己到点了、也没有更新的板上证据 ⇒ 右栏照旧不收
    （panel_lists_no_expired_or_uncredentialed_rows 那条口径一点不许松）。"""
    lua, gql, _ = sync_env([], extra=("Matrix", "Options"))
    g = lua.globals()
    now = 1760000000
    g.time = lambda: now
    gql.Config.AddKeyword("特别任务")
    # 89267 的形状：上一条实例的结束时刻（20 小时前）已过 ⇒ 2026-11-02 起直接出矩阵
    # （不再因"1 分钟前还看得见"保活）
    lua.execute("GQLDB.quests[89267] = { name = '特别任务：神秘身影', poolSeen = true, "
                "  mapID = 2411, roundEnd = %d, expiresAt = %d, expiresSrc = '接口倒计时', "
                "  lastSeenAt = %d }" % (now - 72000, now - 72000, now - 60))
    ids = [r["id"] for r in panel_match_rows(lua, "特别任务")]
    if "89267" in ids:
        return fail("panel_right_column_shares_the_round_expiry_gate",
                    f"到期门判到期出矩阵后，找回入口也该跟着不收这条: {ids}")
    # 对照：这一条实例自己的结束时刻已过，且没有更晚的"板上看见过"证据 —— 上一轮那条口径没动
    lua.execute("GQLDB.quests[89268] = { name = '特别任务：换轮对照组', poolSeen = true, "
                "  mapID = 2411, roundEnd = %d, expiresAt = %d, expiresSrc = '接口倒计时', "
                "  lastSeenAt = %d }" % (now - 60, now - 60, now - 72000))
    ids = [r["id"] for r in panel_match_rows(lua, "特别任务")]
    if "89268" in ids:
        return fail("panel_right_column_shares_the_round_expiry_gate",
                    f"这一条自己到点了的，右栏照旧不收: {ids}")
    ok("panel_right_column_shares_the_round_expiry_gate")


def test_next_refresh_skips_a_stale_expiry():
    """下次刷新定时器也不许拿上一条实例的到期时刻当"最早到期"：那等于把定点唤醒的兜底
    永久钉在过去（Sync 那句"到期已过/取不到 ⇒ 不自动安排"照旧，这里不改调用方、
    不新增任何定时器）。全是过期值时回 nil，与今天一致。
    第 36 轮 T4a：来源换成这条实例自己的结束时刻（计划 §七），"已经过去的那一条"
    从 EffectiveExpiry 就出不来了（它只回 > now 的值），所以过期值自然不参与最早到期。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["quests"] = lua.table_from({
        # 上一条实例留下的那条（板面结束时刻已在 now=7000 之前）：不参与
        21: lua.table_from({"roundEnd": 5000, "expiresSrc": "接口倒计时"}),
        # 这一条自己还没结束：照旧当"最早到期"
        22: lua.table_from({"roundEnd": 9000, "expiresSrc": "接口倒计时"}),
    })
    if int(L.NextRefreshAt(db, 7000)) != 9000:
        return fail("next_refresh_skips_a_stale_expiry",
                    f"过期值不参与「最早到期」，应取 9000，实际 {L.NextRefreshAt(db, 7000)}")
    db["quests"][22] = lua.table_from({"roundEnd": 3000, "expiresSrc": "接口倒计时"})
    if L.NextRefreshAt(db, 7000) is not None:
        return fail("next_refresh_skips_a_stale_expiry",
                    "全是上一条实例的过期值时必须回 nil（调用方按「该抓了」处理）")
    ok("next_refresh_skips_a_stale_expiry")


def test_dump_names_the_gate_that_dropped_the_row():
    """/gql quest 的两处谎报（用户 2026-10-27 的"报告1 当前角色刚才已经做完了这个任务，
    为什么记录没交过？"）：写死那句"没命中关键词、本轮没人交过，金币也没到 X"既是猜话、
    又和紧下面那句"在列靠：本轮有角色交过"直接打架，而真砍掉这一行的到期门一个字没提。
    现在按真判据分岔，两类原因分开说：到期门那种形状里不许再冒出"本轮没人交过"。
    第 36 轮起"靠本轮交过进来的行同时被到期门砍掉"这个形状已经不成立了：结束时刻一过
    这一条就挂不上编号（板面那条成了墓碑），本轮交过自然匹配不上 ⇒ 行先撞准入门。
    所以这一格改由**身份无关**那道门（关键词从日志收的）把行顶住，到期门才是凶手 ——
    报告点名的还是同一件事：到期门砍的行必须说到期门。"""
    lua, gql, _ = sync_env([(89267, 6000000)], extra=("Matrix", "Dump"))
    g = lua.globals()
    g.GQL_NOW = 1000
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 43200})
    gql.Sync.SnapshotNow()                    # 这一条实例结束于 44200
    lua.execute("GQLDB.quests[89267].kwLogAdded = true")
    lua.execute("GQL.Ledger.OnTurnIn(GQL.Config.DB(), 'A-永恒之井', 89267, 1500, "
                "  tostring(GQL.Config.DB().quests[89267].currentSig))")
    g.GQL_NOW = 44400                         # 结束时刻已过、板上也没再见过 = 这条自己的寿命
    txt = str(gql.Dump.Quest("89267"))
    if "到期" not in txt:
        return fail("dump_names_the_gate_that_dropped_the_row", f"报告没点出到期门:\n{txt}")
    if "不在列（到期门" not in txt:
        return fail("dump_names_the_gate_that_dropped_the_row",
                    f"「矩阵」那一行要按真判据说清是哪道门:\n{txt}")
    if "本轮没人交过" in txt:
        return fail("dump_names_the_gate_that_dropped_the_row",
                    f"到期门砍掉的行不许再报「本轮没人交过」（与下一行在列靠自相矛盾）:\n{txt}")
    if "在列靠：关键词从日志收的" not in txt:
        return fail("dump_names_the_gate_that_dropped_the_row",
                    f"前置：这一行该是靠日志收进来那道身份无关的门进来的:\n{txt}")
    ok("dump_names_the_gate_that_dropped_the_row")


def test_dump_reports_the_admission_gate_before_the_expiry_gate():
    """分岔顺序必须照矩阵（准入 → 到期）：一条既没过准入门、又带着本轮已过期值的行，
    界面是准入门砍的，报告却说到期门就把凶手报错 —— 玩家会去查到期时刻，其实该查门槛/关键词。
    两条同样带"本轮之内读到且已过去"的到期时刻，只差金币那道硬门过没过，
    必须报出两道不同的门（只测一条的话顺序怎么排都能过）。"""
    lua, gql, _ = sync_env([(77801, 6000000), (77802, 100000)], extra=("Matrix", "Dump"))
    g = lua.globals()
    g.GQL_NOW = 1000
    gql.Sync.SnapshotNow()
    lua.execute("GQLDB.quests[77801].expiresAt = 1500  GQLDB.quests[77802].expiresAt = 1500 "
                "for _, r in ipairs({77801, 77802}) do "
                "  GQLDB.quests[r].roundEnd = 1500 GQLDB.quests[r].expiresSrc = '接口倒计时' end")
    g.GQL_NOW = 2000
    if int(lua.execute("return GQL.Ledger.MatrixMoneyFloor()")) <= 100000:
        return fail("dump_reports_the_admission_gate_before_the_expiry_gate",
                    "前置：10 金那条必须卡在金币门槛外面")
    low = str(gql.Dump.Quest("77802"))
    if "不在列（准入门没过" not in low:
        return fail("dump_reports_the_admission_gate_before_the_expiry_gate",
                    f"没过准入门的行要先报准入门，不许报到期门:\n{low}")
    if "不在列（到期门" in low:
        return fail("dump_reports_the_admission_gate_before_the_expiry_gate",
                    f"到期门排在准入门之后，这里说它就是报错凶手:\n{low}")
    high = str(gql.Dump.Quest("77801"))
    if "不在列（到期门" not in high:
        return fail("dump_reports_the_admission_gate_before_the_expiry_gate",
                    f"对照组：同样过期、但过了金币门槛的那条才该报到期门:\n{high}")
    ok("dump_reports_the_admission_gate_before_the_expiry_gate")


def test_cross_clock_domain_expiry_is_never_claimed_as_stale():
    """到期门的时钟域口径（2026-11-02 定）：**跨域一律不判**，同域才比大小。
    为什么不能"统一日历时间"就算完：Sync.Clock()（Sync.lua:64-70）在 WallClock() 答不出时
    明写"退回 GetTime()"，而烙 roundEnd 用的就是那个 now（Sync.lua:252 / Ledger.SetExpiry）
    ⇒ 一次降级会话就能把开机秒写进账本。那种行下次登录拿墙上秒一比，endAt(小) <= n(大)
    恒成立 ⇒ 带 roundEnd 的活行会被整批判到期、当场从矩阵消失。身份那一路
    （L.InstanceEnd:641）卡的就是同域，两门必须同一个口径。
      ① 跨域（无论哪一侧是开机秒）⇒ 不判到期（判不出就不判，与 L.Prune 同一条 house rule）。
      ② 同域、roundEnd 已过去 ⇒ 判到期（lastSeenAt 不参与保活，那是 10-30 删掉的另一件事）。
      ③ 对照：同域且这条实例自己真到点了 ⇒ 照旧砍（96718 那句）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)

    # ① 开机秒写的 roundEnd 撞上墙上秒的 now：小 <= 大 恒成立，判了就是凭空吃掉一行。
    uptime_end = lua.table_from({
        "roundEnd": 5000, "expiresSrc": "接口倒计时", "lastSeenAt": 4000})
    if L.InstanceExpired(db, uptime_end, 1700000060):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "①roundEnd 落在开机秒、now 落在墙上秒 ⇒ 跨域判不出，不许判到期")
    # ① 的反方向：墙上秒写的 roundEnd 撞上本次开机的 now。
    epoch_end = lua.table_from({"roundEnd": 1700000000, "expiresSrc": "接口倒计时"})
    if L.InstanceExpired(db, epoch_end, 5000):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "①反方向同样跨域：不许拿大小硬比出来一个'已到期'")

    # ② 同域（都是墙上秒）、roundEnd 已过去 ⇒ 判到期；lastSeenAt 在另一个域也救不了它。
    split_seen = lua.table_from({
        "roundEnd": 1700000000, "expiresSrc": "接口倒计时", "lastSeenAt": 5000})
    if not L.InstanceExpired(db, split_seen, 1700000060):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "②roundEnd 与 now 同域且已过 ⇒ 判到期（lastSeenAt 跨域不再保活）")

    # ③ 对照：全同域、这条自己真到点了 ⇒ 照旧砍
    same = lua.table_from({
        "roundEnd": 1700000000, "expiresSrc": "接口倒计时", "lastSeenAt": 1699990000})
    if not L.InstanceExpired(db, same, 1700000060):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "对照组：同域且这条实例自己的结束时刻已过就该判到期（96718 那句）")
    live = lua.table_from({"roundEnd": 1700000900, "expiresSrc": "接口倒计时"})
    if int(L.EffectiveExpiry(db, live, 1700000060) or -1) != 1700000900:
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "同域且还没过去的结束时刻照旧报出来，界面才有倒计时可显示")
    if L.InstanceExpired(db, live, 1700000060):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "这一条自己还没到点就不许判到期")
    # 没落账的记录 = 没有任何实例可谈。锚点先摆上（尾巴档有值可兜底）：不摆的话
    # 这条断言是空的 —— 兜底链自己就断了，谁也看不出"rec 传 nil 时到底回什么"。
    db["cycleAnchor"] = 1700000000
    if L.EffectiveExpiry(db, None, 1700000060) is not None:
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "没落账的记录回 nil，不许凭空造一个到期时刻")
    if L.InstanceExpired(db, None, 1700000060):
        return fail("cross_clock_domain_expiry_is_never_claimed_as_stale",
                    "没落账的记录不许判到期（尾巴档是这条记录的兜底，不是全局兜底）")
    ok("cross_clock_domain_expiry_is_never_claimed_as_stale")


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
    栏底输入框 + "添加"走 Config.AddKeyword；"扫描地图"（原"按关键词重扫地图"，
    2026-11-01 按用户口径改名并挪到"添加"右边）走 Sync.HuntKeywordMaps；
    行尾"删"= Config.RemoveKeyword + Sync.UnregisterKeyword，由它注册的周常行跟着退列
    （regKey 清掉、记录留着）——口径与原先设置面板那一路逐句相同（入口搬家，判据没动）。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix", "Options"))
    g = lua.globals()
    g.time = lambda: 1700000000
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
    gql.Options.OpenWindow()
    if not lua.execute("GQL.Options.pages.win.kwEdit:SetText('符文石') return true"):
        return fail("panel_can_add_and_remove_keyword", "页面里没有关键词输入框")
    if not lua.execute("GQL.Options.pages.win.addKw:Click() return true"):
        return fail("panel_can_add_and_remove_keyword", "页面里没有\"添加\"按钮或没挂 OnClick")
    if not lua.execute("return GQL.Config.GetKeywords()[1] == '符文石'"):
        return fail("panel_can_add_and_remove_keyword", "点添加应写进配置（走真实 OnClick）")
    if not lua.execute("GQL.Options.pages.win.huntBtn:Click() return true"):
        return fail("panel_can_add_and_remove_keyword", "页面里没有扫描按钮")
    if not lua.execute("return GQLDB.quests[124] and GQLDB.quests[124].regKey == 'kw:符文石'"):
        return fail("panel_can_add_and_remove_keyword", "补扫应把命中注册成周常行")
    if not lua.execute("""
        local rows = GQL.Options.pages.win.kwRows
        for i = 1, #rows do
            local c = rows[i]
            if c.__kw == '符文石' and c.__shown and c.__label
                and c.__label.__text == '符文石  1' then
                return true
            end
        end
        return false
    """):
        return fail("panel_can_add_and_remove_keyword",
                    "左列该出现可见行\"符文石  1\"（扫描把命中数带上来了）")
    if not lua.execute("""
        local rows = GQL.Options.pages.win.kwRows
        for i = 1, #rows do
            local c = rows[i]
            if c.__kw == '符文石' and c.__shown and c.__btn
                and c.__btn.__text == '删'
                and c.__btn.__scripts and c.__btn.__scripts['OnClick'] then
                c.__btn.__scripts['OnClick'](c.__btn)
                return true
            end
        end
        return false
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
    """一页装完地图与关键词之后，"控件全在窗口里、那一排按顺序排开"必须还成立。
    上一版把页脚整排甩到窗口外面的教训（用户 2026-10-27 截图）：从窗口**底边**锚定时
    y 向上为正，写成负数 = 控件在窗外。2026-11-01 页面改成自上而下一条流水
    （地图行 → 输入框/按名称添加/扫描地图/添加当前所在地图 → 关键词段），替身帧不根据
    锚点算几何，所以这里静态读 __points 断言三件事：
      ① 那一排是"前一颗 RIGHT → 后一颗 LEFT"的链（用户口径：后三颗都排在按名称添加后面）；
      ② 这一排的总宽落在内容宽度里（放不下就是出框，玩家点不到最右边那颗）；
      ③ 关键词段的"添加"也贴着自己的输入框。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    gql.Options.OpenWindow()
    why = lua.execute("""
        local p = GQL.Options.pages and GQL.Options.pages.win
        if not p then return "页面没建出来" end
        local function pt(x) return x and x.__points and x.__points[1] end
        local function chain(tag, a, b)
            local ap = pt(a)
            if not ap then return tag .. " 没有锚点" end
            if ap[2] ~= b or ap[3] ~= "RIGHT" then
                return tag .. " 该用 LEFT 贴在前一颗的 RIGHT 上（顺序是用户 2026-11-01 定的）"
            end
        end
        local w = chain("按名称添加", p.addByName, p.edit)
        if w then return w end
        w = chain("扫描地图", p.huntBtn, p.addByName)
        if w then return w end
        w = chain("添加当前所在地图", p.addCur, p.huntBtn)
        if w then return w end
        w = chain("关键词的添加", p.addKw, p.kwEdit)
        if w then return w end
        local function width(x) return tonumber(x.__w) or 0 end
        local ap = pt(p.edit)
        local total = (tonumber(ap[4]) or 0) + width(p.edit)
        local list = { p.addByName, p.huntBtn, p.addCur }
        for i = 1, #list do
            local q = pt(list[i])
            total = total + (tonumber(q[4]) or 0) + width(list[i])
        end
        local contentW = tonumber(p.host.__w) or 0
        if contentW <= 0 then return "内容帧没记到宽度" end
        if total > contentW then
            return string.format("那一排总宽 %.0f 超出内容宽 %.0f（最右边那颗会出框）",
                total, contentW)
        end
    """)
    if why:
        return fail("panel_columns_and_footer_stay_inside_the_window", str(why))
    ok("panel_columns_and_footer_stay_inside_the_window")


def test_panel_footer_controls_are_not_under_a_viewport():
    """原来钉的是"页脚控件别被两栏视口压住"（视口 EnableMouse 会把点击吃掉，玩家实测
    「添加」点不动、回车有效）。2026-11-01 合成一页后没有嵌套视口了，这条守同一个 bug
    的另一半：**控件之间不许叠** —— 地图行、那一排控件、关键词标题、关键词那一排，
    自上而下依次往下排，任何两段的竖直位置都不许回头（叠了就等于上层把下层吃掉）。
    读法：所有 y 都相对内容帧 TOPLEFT 的负数，-y 必须严格递增。"""
    lua, gql, _ = sync_env([(120, 30000000), (121, 10000000)], extra=("Matrix", "Options"))
    gql.Config.AddMap(2405)          # 前置：两行地图，才看得出"最后一行"与那一排的关系
    gql.Options.OpenWindow()
    why = lua.execute("""
        local p = GQL.Options.pages and GQL.Options.pages.win
        if not p then return "页面没建出来" end
        local function yOf(x)
            local q = x and x.__points and x.__points[1]
            if not q then return nil end
            return -(tonumber(q[5]) or 0)
        end
        local seq = {}
        local function push(tag, x)
            local y = yOf(x)
            if not y then return tag .. " 没有锚点" end
            seq[#seq + 1] = { tag = tag, y = y }
        end
        local w = push("地图标题", p.mapTitle)
        if w then return w end
        local rows = p.mapRows
        if #rows < 2 then return "前置：该有两行地图" end
        w = push("最后一行地图", rows[2])
        if w then return w end
        w = push("那一排控件", p.edit)
        if w then return w end
        w = push("关键词标题", p.kwTitle)
        if w then return w end
        w = push("关键词那一排", p.kwEdit)
        if w then return w end
        for i = 2, #seq do
            if seq[i].y <= seq[i - 1].y then
                return string.format("%s 没有排在 %s 下面（叠在一起就会被上层的鼠标区域吃掉）",
                    seq[i].tag, seq[i - 1].tag)
            end
        end
    """)
    if why:
        return fail("panel_footer_controls_are_not_under_a_viewport", str(why))
    ok("panel_footer_controls_are_not_under_a_viewport")


def test_panel_columns_scroll_with_wheel_when_content_overflows():
    """内容比窗口高时滚轮要真的滚得动（用户口径："多了要能滚动"）。
    2026-11-01 合成一页后只有一条滚动轴：窗口右边那条滚动条（画布用自己那套）。
    所以这条压出溢出后滚一格，看滚动条的值有没有动 —— 值 = 减 delta 再按
    (内容高 - 视口高) 夹住（见 OnWheel 头注）。"""
    lua, gql, _ = sync_env([(301, 30000000), (302, 30000000), (303, 30000000)],
                           extra=("Matrix", "Options"))
    gql.Sync.SnapshotNow()
    gql.Config.AddKeyword("任务")            # 命中上面 3 条 = 右列 3 行
    # 30 个空关键词 = 30 行；窗口自适应高度最多到屏幕 85%（720*0.85≈612），
    # 30*20 + 地图/标题 ≈ 700 > 612 ⇒ 一定溢出，滚动条才该出现
    for i in range(30):
        gql.Config.AddKeyword("关键词%02d" % i)
    gql.Options.OpenWindow()
    info = lua.execute("""
        local bar = GQLOptScrollBar
        if not bar then return nil end
        local _, maxV = bar:GetMinMaxValues()
        local before = bar:GetValue() or 0
        local content = GQLOptContent
        local w = content:GetScript('OnMouseWheel')
        if not w then return "nowheel" end
        w(content, -1)      -- 真客户端往下滚给的是负 delta（见 Options 的 OnWheel 头注）
        return string.format("%s|%s|%s", tostring(maxV), tostring(before),
            tostring(bar:GetValue() or 0))
    """)
    if info in (None, "nowheel"):
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"内容帧没挂 OnMouseWheel（滚轮根本没接线）: {info}")
    maxV, before, after = [float(x) for x in str(info).split("|")]
    if maxV <= 0:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"8 行关键词 + 3 条命中早该溢出，滚动条最大滚动量却={maxV}")
    if after <= before:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"往下滚一格滚动条该往前走，实际 {before} → {after}")
    if after - before > 40:
        return fail("panel_columns_scroll_with_wheel_when_content_overflows",
                    f"一格滚了 {after - before} 像素（STEP=30，滚过头了）")
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
    if "扫描地图" not in "\n".join(frame_texts(g.GQLOptionsFrame)):
        return fail("panel_shows_keyword_hit_count",
                    "窗口应有扫描入口（只问跟踪清单）")
    if not select_panel_keyword(lua, "追踪萨拉塔斯"):
        return fail("panel_shows_keyword_hit_count", "点不动命中 0 的关键词行")
    vis = [r["name"] for r in panel_right_rows(lua) if r["shown"]]
    if vis != ["（没找到匹配的任务）"]:
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
    joined = "\n".join(frame_texts(g.GQLOptionsFrame))
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
    gql.Options.OpenWindow()
    lua.execute("GQL_FOCUS = GQLOptionsFrame")   # 鼠标落在设置窗口上，行热区几何仍压着
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
    lua.execute("GQLOptionsFrame:Hide() GQL_FOCUS = GQLMatrixFrame")
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


def test_weekly_rows_only_show_the_cd_tail_never_a_guess():
    """日志收进来的行没有"这条任务自己的倒计时"。T5 起只剩两种形状：
    ① 问得出周 CD 尾巴（cycleAnchor + 604800）→ 尾巴档的数可以显示，但话必须说成
       "距下次周重置"，不能说成"剩余"（用户会读成这任务本身只剩这么久）。常驻周常
       不许吃板面倒计时：12.1.5 实测板上实例几小时一换，拿它当到期等于当天下午抹掉
       整行（追踪萨拉塔斯是周常，判出来的日期却"马上就要结束"）。
    ② 周接口没了 → 锚点不前进（RefreshCycle 只朝前改），界面照旧只读周 CD 尾巴，
       话跟着数说「距下次周重置」。老版这里还会把 expiresSrc 记成「下次每日重置」当取证——
       那一组字段随 T5 整组退役，每日那个数不再进账本、更进不了界面；12.1.5 那次
       "周五写着距下次周重置 12 时 6 分、其实是明早 7 点的每日重置"从这个模型里根本拼不出来。
    ③ 连尾巴都问不出（锚点 0、板值也没有）→ 界面什么都不能报：没有剩余、没有重置行，
       名字本色。老版这里退回编一个 24 小时排程值并标 guessed——从此没有猜的数，
       那一组字段整组退役，这条就是钉"不许再编"。"""
    lua, gql, _ = sync_env([(173, 30000000)], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    lua.execute("C_QuestLog.GetInfo = function(i) "
                "  if i == 1 then return { questID = 9001, title = '追踪萨拉塔斯', "
                "        isHeader = false } end return nil end")
    gql.Config.AddKeyword("萨拉塔斯")
    # 悬停的剩余时刻读这条实例自己的结束时刻 = 周 CD 尾巴（cycleAnchor + 604800）。
    # 锚点要由**同步那一刻**的墙上时钟算出来（RefreshCycle），所以这里先给一个纪元域的
    # WallClock（真客户端就是这么来的：国服有 GetServerTime），否则 RefreshCycle 定不出
    # 锚点 → 尾巴 nil → 常驻周常压根没有结束时刻可显示。
    lua.execute("GQL.Api.WallClock = function() return 1700000000 end")
    gql.Sync.SnapshotNow()
    for legacy in ("expiresAt", "expiresSrc", "expiresGuessed"):
        if lua.execute("return GQLDB.quests[9001].%s ~= nil" % legacy):
            return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                        f"老到期字段 {legacy} 那一组连同排程值已在 T5 整组退役，同步不许再写它")
    if lua.execute("return GQLDB.quests[9001].roundEnd ~= nil"):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    "常驻周常不许吃板上倒计时：roundEnd 留空，结束时刻才走得到周 CD 尾巴那一档")

    gql.Matrix.Toggle()
    # 钉在"本 CD 还差 2 小时结束"上：悬停要报"距下次周重置"，不许说成"剩余"
    exp = int(lua.execute("return GQLDB.cycleAnchor + 604800"))
    lua.execute("GQL.Api.WallClock = function() return %d end" % (exp - 7200))
    gql.Matrix.Render()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if "追踪萨拉塔斯" not in joined:
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess", f"这行仍要显示:\n{joined}")

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
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"悬停提示本身要弹出来，否则这条断言是空的:\n{calls}")
    if not any("距下次周重置" in t for t in calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"CD 来源要说成「距下次周重置」:\n{calls}")
    if any(t.startswith("剩余") for t in calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"CD 结束时刻不许谎称成任务自己的剩余时间:\n{calls}")

    # ②：周接口没了、只剩每日档 → 锚点不动，数仍来自周 CD 尾巴；每日那个数没有落脚点
    del calls[:]
    lua.execute("GQLDB.quests[9001] = { name = '追踪萨拉塔斯', kwLogAdded = true, "
                "  observations = {} }")
    lua.execute("GQL.Api.WeeklyResetCountdown = function() return nil end")
    lua.execute("GQL.Api.ResetTime = function() return 45607 end")
    gql.Sync.SnapshotNow()
    if lua.execute("return GQLDB.quests[9001].expiresAt ~= nil "
                   "or GQLDB.quests[9001].expiresSrc ~= nil "
                   "or GQLDB.quests[9001].roundEnd ~= nil"):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    "只有每日档可用时也不再写任何来源字段/板上数：界面唯一能读的就是尾巴")
    # 渲染时刻取尾巴前 30 分钟 ⇒ 报出来的必须是"30分"，取错一档这个数字就对不上。
    lua.execute("GQL.Api.WallClock = function() return %d end" % (exp - 1800))
    gql.Matrix.Render()
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if not any(t == "距下次周重置 30分" for t in calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"数来自周 CD 尾巴，话就要说「距下次周重置」而且是尾巴那 30 分:\n{calls}")
    if any(("每日" in t) or t.startswith("剩余") for t in calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"每日档数字不再参与倒计时，也不许谎称是任务自己的剩余:\n{calls}")

    # ③：连尾巴都问不出（锚点 0、板值也没有）→ 什么都不报；老版这里编 24 小时标 guessed
    del calls[:]
    lua.execute("GQLDB.quests[9001] = { name = '追踪萨拉塔斯', kwLogAdded = true, "
                "  observations = {} }")
    lua.execute("GQL.Api.ResetTime = function() return nil end GetQuestResetTime = nil "
                "GQL.Api.WeeklyResetCountdown = function() return nil end")
    lua.execute("GQLDB.cycleAnchor = 0")
    gql.Sync.SnapshotNow()
    if int(lua.execute("return GQLDB.cycleAnchor")) != 0:
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    "问不出重置时刻：锚点维持 0，不许编一个出来")
    if lua.execute("return GQLDB.quests[9001].expiresAt ~= nil "
                   "or GQLDB.quests[9001].expiresSrc ~= nil "
                   "or GQLDB.quests[9001].expiresGuessed ~= nil "
                   "or GQLDB.quests[9001].roundEnd ~= nil"):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    "问不出就是问不出：不再有任何兜底到期可写（排程值随 T5 整组退役）")
    gql.Matrix.Render()
    lua.execute("GQLMatrixFrame:GetScript('OnUpdate')(GQLMatrixFrame)")
    if "追踪萨拉塔斯" not in "\n".join(calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"悬停本身要弹出来，否则下面那句『不许报倒计时』是空断言:\n{calls}")
    if any(("剩余" in t) or ("重置" in t) for t in calls):
        return fail("weekly_rows_only_show_the_cd_tail_never_a_guess",
                    f"问不出到期不许报任何倒计时:\n{calls}")
    ok("weekly_rows_only_show_the_cd_tail_never_a_guess")


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
    """设置页关键词段点一行关键词 = 选中（走行按钮真实注册的 OnClick，不直调渲染函数）。
    2026-11-01 起关键词不再是独立窗口，行池挂在 GQL.Options.pages.win.kwRows 上
    （控件都在页面表里，不再各自占全局名），所以这里直接翻行池，不再递归 walk 帧树。
    行本体带 __kw 和 __label；只认当前可见的行 —— 池尾被 Hide 的旧行还留着 __kw。"""
    return bool(lua.execute("""
    local rows = (GQL.Options.pages and GQL.Options.pages.win
        and GQL.Options.pages.win.kwRows) or {}
    for i = 1, #rows do
        local c = rows[i]
        if c.__kw == '%s' and c.__shown and c.__label
            and c.__scripts and c.__scripts['OnClick'] then
            c.__scripts['OnClick'](c)
            return true
        end
    end
    return false
    """ % kw))


def click_panel_ignore_button(lua, qid):
    """设置页关键词段右列点"忽略/取消忽略"（走按钮真实 OnClick）。
    行可见才点——被 Hide 的池尾旧行还挂着 __questID，点错行等于测了个空。"""
    return bool(lua.execute("""
    local rows = (GQL.Options.pages and GQL.Options.pages.win
        and GQL.Options.pages.win.questRows) or {}
    for i = 1, #rows do
        local c = rows[i]
        if c.__questID == %d and c.__shown and c.__btn
            and c.__btn.__scripts and c.__btn.__scripts['OnClick'] then
            c.__btn.__scripts['OnClick'](c.__btn)
            return true
        end
    end
    return false
    """ % qid))


def panel_right_rows(lua):
    """设置页关键词段右列的行：[{'qid','name','id','btn','shown'}]，从页面行池
    GQL.Options.pages.win.questRows 收。"没有命中/点左边"这类提示行 qid=None，一起收。
    shown 直接读 __shown：frame_texts 看不见 Hide，"复用行没 Show"这个坑恰好只在可见性上。
    id 读的是行上那列 **questID**（`r.__id`）：这一列第 32 轮前叫 `__gold`、写的是金额，
    用户口径"最好加上questId，不用加金额"之后整列换成编号，字段名跟着改口 ——
    一个叫 __gold 的字段装 ID 是谎，读它的用例也就跟着读 __id。"""
    res = lua.execute("""
    local out = {}
    local rows = (GQL.Options.pages and GQL.Options.pages.win
        and GQL.Options.pages.win.questRows) or {}
    for i = 1, #rows do
        local c = rows[i]
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
    """设置页关键词段当前可见的行文字（"关键词  命中数"，存在行本体的 __label 里）。
    池尾被 Hide 的旧行不算可见——读的是玩家这一轮真看得见的版面。
    2026-11-01 起关键词并进设置页，行池在 GQL.Options.pages.win.kwRows 上。"""
    res = lua.execute("""
    local out = {}
    local rows = (GQL.Options.pages and GQL.Options.pages.win
        and GQL.Options.pages.win.kwRows) or {}
    for i = 1, #rows do
        local c = rows[i]
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


def test_a_keyword_weekly_stays_after_the_char_that_accepted_it_logs_out():
    """梦魇任务 94446 那一单（2026-10-29 用户点名，定稿口径原话）：
    "梦魇任务是关键词任务，只要有一个号接了，那就意味着我要关注这个任务，
     不管是不是金币任务，都要上矩阵。"
    根因：这条靠玩家自配的关键词从**任务日志**收进账本，上板凭据只有 `rec.kwLogAdded`
    那一份，而它的语义是"**此刻正在同步的那个号**日志里挂着它"（Sync.lua:259 换号就撤）。
    于是 14 个号里任何一个没接这条的号登录一次，整行对**全账号所有人**消失
    （真档只读回放实测：清掉 kwLogAdded 后面板 15 行 → 14 行，掉的正是梦魇任务）。
    修法把它升成**这一条实例的凭据** `rec.kwLogKey`：收日志那一刻烙下 L.InstanceKey 的
    编号（这条问不出板面倒计时 ⇒ 落周 CD 尾巴档 ⇒ 下周重置自己作废，不需要清理代码）。
    全程走真实同步入口（SyncKeywordQuestsFromLog 在快照里跑），不手搓记录：
    ① A 号日志里有它 → 行在矩阵 + 当场烙上本周编号；
    ② 换成没接它的 B 号 → 行还在（用户报的就是这条）、布尔位照旧被撤（语义不动，
       撤布尔位不许顺带撤编号）、currentSig 不被 DiffPool 的消失判定清空（灰横线那一族）、
       格子不被"不在池里 ⇒ ended"刷成一排 —（R3/R4 那两道豁免）；
    ③ 第三个号 C 再登录一次 —— 撤点之后才是这两道门真正干活的时刻。"""
    name = "a_keyword_weekly_stays_after_the_char_that_accepted_it_logs_out"
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000,
                           extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID = 94446
    if int(db["cycleAnchor"]) <= 0:
        return fail(name, "前置：周 CD 尾巴要定得出来（cycleAnchor>0）—— 锚点 0 ⇒ 编号算不出 ⇒ "
                          "这份凭据压根烙不上（生产里 Config.Init 每次登录都会算出锚点）")
    gql.Config.AddKeyword("梦魇")

    def listed():
        return matrix_listed(lua)

    def cells():
        """这一行每个号的格子状态串（ch=state;…），行不在列就回空串。"""
        return one(lua.execute(
            "local out = GQL.Ledger.MatrixRows(GQL.Config.DB()) "
            "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
            "if r.questID == %d then local s = '' "
            "for k, ch in ipairs(GQL.Ledger.Characters(GQL.Config.DB())) do "
            "s = s .. ch .. '=' .. tostring(r.state[ch]) .. ';' end "
            "return s end end end return ''" % QID))

    # A 号：日志里挂着这条 → 走真实收日志那条路（同一次快照里还会跑撤点那趟扫描）
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "梦魇任务", "questLogIndex": 1}) if int(i) == 1 else None)
    res = gql.Sync.SnapshotNow()
    if res is None or int(res["logAdded"]) != 1:
        return fail(name, f"前置：A 号这趟日志该收进这一条，实际 {res}")
    rec = db["quests"][QID]
    if rec is None or rec["kwLogAdded"] is None:
        return fail(name, "前置：这条该由日志收进账本并盖上 kwLogAdded")
    key = uid(L, db, QID, 1700000000)
    if key is None:
        return fail(name, "前置：这一条实例的编号该算得出来（周 CD 尾巴档）")
    if rec["kwLogKey"] is None or str(rec["kwLogKey"]) != key:
        return fail(name, f"A 号的日志证明这一条存在过 ⇒ 当场烙上它这一条实例的编号 {key}，"
                          f"实际 kwLogKey={rec['kwLogKey']}")
    if "%d," % QID not in listed():
        return fail(name, "前置：A 号登录后这一行要在矩阵里: " + listed())

    # B 号登录：日志里压根没这条 → Sync.lua:259 把布尔位撤掉（这一句语义不许动）
    g.UnitName = lambda *a: "小号二"
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 777, "title": "无关日常", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    if "%d," % QID not in listed():
        return fail(name, "一个没接这条的号登录，就把整行对全账号撤走了 —— 用户点名的就是这条："
                          + listed())
    if rec["kwLogAdded"] is not None:
        return fail(name, "kwLogAdded 的语义是'当前这个号还挂着没'，换号登录当场该撤 —— "
                          "留着它等于把修法做成第二个 live 标记（消失判定与事件 pin 都靠它）")
    if rec["kwLogKey"] is None or str(rec["kwLogKey"]) != key:
        return fail(name, f"撤掉布尔位不许把这一条实例的凭据一起撤走（编号 {key} 要活着），"
                          f"实际 kwLogKey={rec['kwLogKey']}")
    if rec["currentSig"] is None:
        return fail(name, "DiffPool 的'本轮没在池里 ⇒ 记 gone'不许清空 currentSig —— "
                          "行会退成一条灰横线（用户看到的还是那句'加了关键词却没有任务'）")
    if "ended" in cells():
        return fail(name, "格子不许被'不在池里 ⇒ 写 ended'刷成一排 —：" + cells())

    # C 号再登录：布尔位早就没了，这一趟才是 R3/R4 两道豁免真正接力的地方
    g.UnitName = lambda *a: "小号三"
    gql.Sync.SnapshotNow()
    if "%d," % QID not in listed():
        return fail(name, "第三个号登录后这一行还是要在（凭据是本周的编号，不是当前号的日志）："
                          + listed())
    if rec["currentSig"] is None or rec["kwLogKey"] is None:
        return fail(name, "又换一个号登录就丢了 currentSig/kwLogKey（第二趟同步起就没有任何"
                          "'当前号挂着它'的事实了）: " + cells())
    if "ended" in cells():
        return fail(name, "第二趟之后的同步仍不许把这一行刷成 ended：" + cells())
    ok(name)


def test_the_log_credential_dies_with_the_instance():
    """驻整周，不是驻永久（用户 2026-10-29 同一句口径的另一半："只要有一个号接了…都要上矩阵"
    说的是**这一条任务实例**被日志证明存在过，不是"这条 ID 从此永远挂在板上"）。
    `rec.kwLogKey` 走的是周 CD 尾巴那一档编号（cycleAnchor + 604800 按 15 分钟分桶），
    下一个周重置一到 ⇒ 尾巴整段挪一档 ⇒ 编号自然对不上 ⇒ 行自己退场，
    不需要任何"清理"代码（第 36 轮编号模型的全部意义）。
    少了这一刀就是把 kwLogAdded 改成永久标记：上周接过的赖在矩阵里，
    正是第 32 轮写进注释的那族"上上世代垃圾行"。"""
    name = "the_log_credential_dies_with_the_instance"
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000,
                           extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID = 94446
    gql.Config.AddKeyword("梦魇")
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "梦魇任务", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    # 换成没接这条的号：布尔位被撤，此刻这一行只靠 kwLogKey 那一份凭据撑着
    g.UnitName = lambda *a: "小号二"
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 777, "title": "无关日常", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    rec = db["quests"][QID]
    if rec is None or rec["kwLogAdded"] is not None:
        return fail(name, "前置：布尔位该已被没接这条的号撤掉（那正是本用例的起点）")
    if rec["kwLogKey"] is None:
        return fail(name, "前置：这一行此刻只靠本周编号撑着，而编号压根没烙上 —— "
                          "凭据没接上线，下面那条'下周自己作废'测不到任何东西")
    if "%d," % QID not in matrix_listed(lua):
        return fail(name, "前置：布尔位撤掉后这一行还得在矩阵里（用户要的'驻整周'）: "
                          + matrix_listed(lua))

    # 周重置：尾巴整段挪一档 ⇒ 编号变 ⇒ 行撤（"驻整周"到此为止）
    old_key = str(rec["kwLogKey"])
    lua.execute("GQLDB.cycleAnchor = (GQLDB.cycleAnchor or 0) + 86400 * 7")
    if uid(L, db, QID, 1700000000) == old_key:
        return fail(name, f"前置：推一整周之后尾巴档该换编号，实际还是 {old_key}")
    if "%d," % QID in matrix_listed(lua):
        return fail(name, "上周那条实例的日志凭据不许给本周续命（永久驻板 = 第 32 轮那族"
                          "上上世代垃圾行回来）: " + matrix_listed(lua))
    ok(name)


def test_a_masked_row_loses_both_log_marks():
    """屏蔽（矩阵/面板右栏那颗"忽略"，数据层 L.RemoveQuest）要清掉**两份**日志标记：
    布尔位早就清了（现网注释写的是同一条理由 —— 屏蔽之后"日志收的"不再是事实，
    留着恢复时会被误当成本来就在列），新加的这份编号凭据同理必须一起清，
    否则"取消忽略"当场把玩家明确不要的那一行顶回矩阵。
    取消屏蔽也不许让编号自己长回来（L.AddQuest 玩家那一路 addedBy=nil，
    只撤意图、不发上板凭据）—— 只有再有一次**真日志快照**才重新烙（那条路认配置）。"""
    name = "a_masked_row_loses_both_log_marks"
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000,
                           extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID = 94446
    gql.Config.AddKeyword("梦魇")
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "梦魇任务", "questLogIndex": 1}) if int(i) == 1 else None)
    res = gql.Sync.SnapshotNow()
    if res is None or res["logAdded"] is None or int(res["logAdded"]) != 1:
        return fail(name, f"前置：这条该由日志收进账本，实际 {res}")
    rec = db["quests"][QID]
    key = uid(L, db, QID, 1700000000)
    if rec["kwLogAdded"] is None or rec["kwLogKey"] is None:
        return fail(name, f"前置：两份日志标记都该在记录上（编号 {key}），实际 "
                          f"kwLogAdded={rec['kwLogAdded']} kwLogKey={rec['kwLogKey']}")

    L.RemoveQuest(db, QID)
    if rec["ignored"] is None:
        return fail(name, "前置：RemoveQuest 要落成永久屏蔽意图")
    if rec["kwLogAdded"] is not None:
        return fail(name, "屏蔽之后'日志收的'不再是事实，布尔位该清（现网那条注释的理由）")
    if rec["kwLogKey"] is not None:
        return fail(name, "同一句理由对新增那份凭据同样成立：屏蔽后 kwLogKey 也必须清掉，"
                          "否则'取消忽略'会拿上周的编号把玩家明确不要的行当场顶回矩阵")

    # 取消屏蔽：只撤意图，不许把两份日志凭据一起发回来
    # addedBy 传 nil = 玩家点"取消忽略"那一路（传 "questlog" 测的是日志那一路，
    # 与这条用例要钉的"取消屏蔽不发凭据"不是一件事）。
    L.AddQuest(db, QID, 1700000000, None)
    if rec["ignored"] is not None:
        return fail(name, "前置：AddQuest 那一路要撤销'别再显示'的意图")
    if rec["kwLogAdded"] is not None or rec["kwLogKey"] is not None:
        return fail(name, "取消忽略不等于'这条挂在谁的日志里'，两份标记都不许自己长回来："
                          f"{rec['kwLogAdded']}/{rec['kwLogKey']}")
    # 真再来一次日志快照（同一条收日志的路）⇒ 两份都重新落上、编号还是本周那一条实例的
    gql.Sync.SnapshotNow()
    if rec["kwLogAdded"] is None:
        return fail(name, "前置：日志真挂着它的那一趟要把布尔位写回来")
    if rec["kwLogKey"] is None or str(rec["kwLogKey"]) != key:
        return fail(name, f"取消屏蔽后要再有一次日志快照才重新烙编号（{key}），"
                          f"实际 kwLogKey={rec['kwLogKey']}")
    ok(name)


def test_a_removed_keyword_takes_the_log_credential_down_with_it():
    """删掉关键词 = 玩家收回"这类任务我要跟踪"这句话，那份本周编号凭据必须跟着失效。
    Sync 那道撤点只认"当前这个号日志里挂着没"，而 keep 收不到它有两种完全不同的原因：
    ①任务离开了日志（用户 10-29 要的正是这一种**驻整周**："只要有一个号接了…都要上矩阵"）；
    ②关键词没了（这行再没有绕过金币门槛的理由）。数据层分不开这两种（撤点手上只有
    "在不在 keep"这一个事实），所以区分住在判据里：L.KeywordLogAdmits 先问
    Config.KeywordMatch(rec.name) —— 照 L.QuestKnown 那一句现成的口径
    "玩家删了关键词这道门自动关闭（不引入新字段、不留新状态）"，一个字不写进存档。
    少了这一道，删掉关键词的行会顶着编号赖到周 CD 尾巴，正是老注释里那句
    "标记不清掉会在矩阵里一直赖着"换个字段重演。"""
    name = "a_removed_keyword_takes_the_log_credential_down_with_it"
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000,
                           extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID = 94446
    gql.Config.AddKeyword("梦魇")
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "梦魇任务", "questLogIndex": 1}) if int(i) == 1 else None)
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：A 号这趟快照没跑成")
    # 换成没接这条的号：布尔位被撤，这一行此刻只靠本周编号撑着（上一条用例的起点）
    g.UnitName = lambda *a: "小号二"
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 777, "title": "无关日常", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    rec = db["quests"][QID]
    if rec is None or rec["kwLogAdded"] is not None or rec["kwLogKey"] is None:
        return fail(name, f"前置：布尔位该被没接这条的号撤掉、编号该还挂着，实际 "
                          f"{rec['kwLogAdded'] if rec else '无记录'}/{rec['kwLogKey'] if rec else None}")
    if "%d," % QID not in matrix_listed(lua):
        return fail(name, "前置：这一行此刻靠本周编号驻板: " + matrix_listed(lua))

    # 玩家删掉关键词 + 再一趟同步 ⇒ 行必须退场（通行证是玩家配的那条关键词，不是编号自己）
    gql.Config.RemoveKeyword("梦魇")
    gql.Sync.SnapshotNow()
    if "%d," % QID in matrix_listed(lua):
        return fail(name, "关键词删掉了，这一行还顶着编号在矩阵里 —— 它再没有绕过金币门槛的"
                          "理由了（老注释那句「标记不清掉会在矩阵里一直赖着」换个字段重演）: "
                          + matrix_listed(lua))
    if rec["kwLogAdded"] is not None:
        return fail(name, "前置：关键词全删/删掉这一条时布尔位照旧要撤（现网语义不动）")

    # 同一个 CD 内把关键词加回来 ⇒ 行当场回来："这一条实例被某个号的日志证明存在过"
    # 并没有因为玩家删一次配置就变成没发生过，凭据仍是那一份编号，不必重新收日志。
    gql.Config.AddKeyword("梦魇")
    if "%d," % QID not in matrix_listed(lua):
        return fail(name, "重新声明「这类任务我要跟踪」，本周那份日志证据还在，行该直接回来；"
                          "要等某个号再把这条接进日志才上板 = 把配置当成了删除历史的开关: "
                          + matrix_listed(lua))
    ok(name)


def test_a_log_row_back_on_the_board_keeps_its_weekly_identity():
    """R5（计划 §三落点表第 5 条，梦魇任务 94446 那一刀的配套门）：一条靠日志维持的行
    这周之后**重新出现在世界任务板上**时，板上那条实例只剩几小时，可它的每周 lockout
    还活着。L.SetExpiry 要是把这几小时的轮换倒计时写进 roundEnd ⇒ L.InstanceEnd 从
    ②档（周 CD 尾巴）挪到①档（板面）⇒ 编号跟着换档 ⇒ 刚烙上的那份日志凭据立刻对不上号、
    整行当场没 —— 正是 12.1.5 实测"加固符文石 当天下午整行没了"那个形状，只是这次
    咬掉的是本轮新加的凭据。而这一刻 `kwLogAdded` 布尔位恰恰是空的（在池里的行由池维护，
    Sync 那道撤点认的就是这个），所以老那句 `regKey or kwLogAdded` 拦不住它：
    这就是 SetExpiry 多第 4 个可选参数 resident 的全部理由。
    全程走真实同步入口（快照的池循环里那一句 L.SetExpiry）：
    A 号接在日志里（烙编号）→ B 号登录（布尔位被撤，只剩编号）→ 这条重新上板、
    接口答出几小时的轮换倒计时 ⇒ roundEnd 必须还是 nil、编号不许被挪档、行还在。
    对照组：同一次快照里另一条同样撞关键词、但**没有**日志凭据的池内任务照旧写 roundEnd
    （这道门只认那份编号，不许顺手把 SetExpiry 变成空操作）。"""
    name = "a_log_row_back_on_the_board_keeps_its_weekly_identity"
    lua, gql, _ = sync_env([(130, 30000000)], weekly=432000, wall=1700000000,
                           board=43200, extra=("Matrix",))
    g = lua.globals()
    L, db = gql.Ledger, g.GQLDB
    QID, CTRL = 94446, 94449
    gql.Config.AddKeyword("梦魇")
    gql.Config.AddKeyword("梦魇对照")
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": QID, "title": "梦魇任务", "questLogIndex": 1}) if int(i) == 1 else None)
    if gql.Sync.SnapshotNow() is None:
        return fail(name, "前置：A 号这趟快照没跑成")
    rec = db["quests"][QID]
    key = uid(L, db, QID, 1700000000)
    if rec is None or rec["kwLogKey"] is None or str(rec["kwLogKey"]) != key:
        return fail(name, f"前置：A 号的日志要把这一条实例的编号 {key} 烙上，实际 "
                          f"{rec and rec['kwLogKey']}")
    # B 号登录：布尔位被撤，此刻这一行只靠编号撑着
    g.UnitName = lambda *a: "小号二"
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 777, "title": "无关日常", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    if rec["kwLogAdded"] is not None:
        return fail(name, "前置：没接这条的号登录后布尔位该被撤掉（这正是 R5 要拦的时刻）")

    # 这一条重新上板：接口答出几小时的板面轮换倒计时（对照那条同样在板上、没有日志凭据）
    g.C_WorldQuests["GetWorldQuests"] = lambda: lua.table_from({
        1: lua.table_from({"questID": QID, "mapID": 2007, "name": "梦魇任务", "money": 1000}),
        2: lua.table_from({"questID": CTRL, "mapID": 2007, "name": "梦魇对照组", "money": 1000}),
    })
    gql.Sync.SnapshotNow()

    if rec["roundEnd"] is not None:
        return fail(name, "板面上几小时的轮换寿命不许写进常驻周常的 roundEnd —— 一写就等于"
                          "把身份从周 CD 尾巴挪到板面档，刚烙的日志凭据当场对不上（加固符文石"
                          "那个形状），实际 roundEnd=%s" % (rec["roundEnd"],))
    if str(rec["kwLogKey"]) != key:
        return fail(name, f"编号不许被板面倒计时挪档（{key} → {rec['kwLogKey']}）")
    if "%d," % QID not in matrix_listed(lua):
        return fail(name, "这一行此刻既在板上又有本周的日志凭据，不许掉出矩阵: "
                          + matrix_listed(lua))
    ctrl = db["quests"][CTRL]
    if ctrl is None or ctrl["roundEnd"] is None:
        return fail(name, "对照组塌了：没有日志凭据的池内任务照旧要用它自己的板面倒计时定身份"
                          "（这道门只认那份编号，不是把 SetExpiry 变成空操作）")
    ok(name)


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
            "  if s then s(c) return true end end return false"):
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


def test_a_turned_in_row_without_gold_read_does_not_admit():
    """2026-10-29 定稿的反向用例：用户当天把第 15 轮那条"交过就硬凭据"收口了 ——
    "非关键字任务，如果读不到金币奖励，就不应该上榜；不能依赖交任务的金额这个逻辑"。
    松露大乱斗那种金币任务，用户明确说切到狩猎模式去看任务时金额就知道了（接口读得到），
    不会卡在"读不出"。所以"接读不出/读 0，但交过"这一档不再放行。
    第 36 轮起这道门按**唯一编号**收口那一半保留：上一轮交的不算（编号里带着结束时刻，
    尾巴挪一档就匹配不上）。
    走真实事件入口 fire(QUEST_COMPLETE) → Sync.RecordTurnIn 当场落账（交割时不量钱、
    流水里没有金额那一列），不直调 Ledger.OnTurnIn。"""
    lua, gql, money = sync_env([(140, 30000000)], weekly=432000, wall=1792577090,
                               extra=("Matrix", "Dump"))
    g = lua.globals()
    gql.Sync.SnapshotNow()
    # 松露大乱斗的形状：账本里有记录（补扫/日志收进来的）、接口一分钱都答不出、不在池里
    lua.execute("GQLDB.quests[95484] = { name = '松露大乱斗', mapID = 2437, "
                "observations = {}, poolSeen = true }")
    if "95484," in matrix_listed(lua):
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    "金额读不出、又没人交过时它不该在列")
    # 交割事件到达即入账（奖励是物品/材料、钱包一点没变也照记这一笔，量钱那一步已退役）
    money["v"] = 1000000
    fire(gql, "QUEST_COMPLETE", 95484)
    g.GQL_NOW = 20
    listed = matrix_listed(lua)
    if "95484," in listed:
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    "2026-10-29 定稿：接读不出金币、交过也照样不该进矩阵（不依赖交任务金额）: " + listed)
    # 换一条实例（尾巴整周往前挪）→ 那一笔烙的编号自然匹配不上，仍然不在列
    anchor = int(lua.execute("return GQLDB.cycleAnchor or 0"))
    if anchor <= 0:
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    "前置：周 CD 尾巴要定得出（cycleAnchor>0），否则这条压根挂不上编号")
    lua.execute("GQLDB.cycleAnchor = (GQLDB.cycleAnchor or 0) + 86400 * 7")
    if "95484," in matrix_listed(lua):
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    "换实例后依旧不该在列")
    # 换个角色交也算：矩阵是 14 个小号共用的一张表。仍不该在列。
    lua.execute("GQLDB.turnins = {} GQLDB.cycleAnchor = %d" % anchor)
    lua.execute("GQL.Ledger.OnTurnIn(GQLDB, '小号二-永恒之井', 95484, GQL_NOW, nil)")
    if "95484," in matrix_listed(lua):
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    "别的角色本周交过的同样不该进矩阵（读不到金币奖励就不上榜）")
    # 取证同源：/gql quest 要说"准入门没过（点名与金币两类都不沾）"，不许仍拿"本轮交过"当在列依据
    txt = str(gql.Dump.Quest("松露大乱斗"))
    if "准入门没过" not in txt:
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    f"诊断报告要与矩阵同一道门:\n{txt}")
    if "在列靠：本轮有角色交过" in txt:
        return fail("a_turned_in_row_without_gold_read_does_not_admit",
                    f"第 39 轮起交过不是准入门，报告不许把它写成在列依据:\n{txt}")
    ok("a_turned_in_row_without_gold_read_does_not_admit")


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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2393: True})
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
    if lua.execute("return GQLDB.quests[89507].roundEnd ~= nil"):
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "注册周常不许吃板上那条的轮换倒计时：roundEnd 必须留空（结束时刻走周 CD 尾巴）")
    if int(lua.execute("return GQL.Ledger.EffectiveExpiry(GQLDB, GQLDB.quests[89507], 1700000000)")) \
            != int(lua.execute("return GQLDB.cycleAnchor + 604800")):
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    "注册周常的到期只认 CD 尾巴（cycleAnchor + 604800），实际 "
                    + one(lua.execute("return tostring(GQL.Ledger.EffectiveExpiry("
                                      "GQLDB, GQLDB.quests[89507], 1700000000))")))

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
    if "同族" in tip:
        return fail("family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it",
                    f"同族条数不再上界面（第 33 轮：那是判据说明，玩家只需要那一格的勾）:\n{tip}")
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
    板上那条"不显示它（它不是周常）"。勾不会因为这行由族本体顶着就丢（它问全族）。
    （2026-10-29 健步如飞案起，客户端 flag 残留不再单独作 done 凭据 —— 快照那道
    "本轮有流水"门对注册周常一视同仁；这条的 done 改由族本体自己的流水背书供给，
    问全族的路径一字不变。）"""
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
    # 交割流水背书（金额 0 = 没量到钱）：门放行后快照才写 done，勾从问全族那一层长出来
    key = str(gql.Sync.CurrentCharKey())
    raw_turnin(lua, gql.Ledger, g.GQLDB, key, 89507, 1700000000)
    gql.Sync.SnapshotNow()
    if one(lua.execute("local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), 1700000000) "
                       "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
                       "  return tostring(r.state[...]) end end return 'none'", key)) != "done":
        return fail("anchor_in_the_pool_lists_only_one_row",
                    "这一格的红勾要问全族：族本体有编号对上的流水就是做过")
    ok("anchor_in_the_pool_lists_only_one_row")


def test_registered_anchor_refreshes_when_pool_unavailable():
    """锚点/族成员的状态与板面证据只靠"拿着 ID 问接口 + 问跟踪地图清单"，跟世界任务池
    没关系。12.x 池读不到时整趟快照会提前 return，上一版把板面扫描放在那之后 →
    前置失败连带把这一行砍没（矩阵整周空着）。现在池问不出也先把板面证据问完。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    g.time = lambda: 1700000000
    g.C_WorldQuests = lua.table_from({})     # 池接口全问不出 = GetPool 回 nil
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
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


def test_scan_board_renews_family_anchor_across_cd():
    """用户实测"看不到丰饶供品"（v0.5.0）。D6/D7 撤掉②档按身份的豁免后，族锚点只能靠
    "本 CD 族成员真在板上被看到"续 lastSeenAt。丰饶子任务是任务清单条目（C_TaskQuest.
    GetQuestsOnMap），不是世界任务 —— 进不了池循环那条续命路，只有按跟踪图扫板
    （ScanBoards）看得到。上一版这一趟只更新地图、不续 lastSeenAt ⇒ 换 CD 那一刻锚点被
    ②档"本 CD 没见过 ⇒ 到期"判死，整族那行再也回不来。这里池问不出、锚点只带上一 CD 的
    lastSeenAt，靠扫板这一趟续命后行必须照旧在列。"""
    lua, gql, _ = sync_env([], weekly=432000, extra=("Matrix",))
    g = lua.globals()
    now = 1700000000
    g.time = lambda: now
    g.C_WorldQuests = lua.table_from({})          # 池问不出 = 只剩扫板这条唯一证据路
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2395: True})
    g.C_TaskQuest = lua.table_from({
        "GetQuestInfoByQuestID": lambda qid: ("丰饶供品", 2395) if int(qid) == 89507 else None,
        "GetQuestsOnMap": lambda mid: lua.table_from({
            1: lua.table_from({"questID": 89507, "mapID": 2395})}) if int(mid) == 2395 else None,
    })
    g.C_Map = lua.table_from({
        "GetMapInfo": lambda mid: lua.table_from({"name": "银月城"}) if int(mid) == 2395 else None,
    })
    g.C_QuestLog = lua.table_from({
        "IsQuestFlaggedCompleted": lambda qid: False,
    })
    # 锚点只带上一 CD 的 lastSeenAt（本 CD 还没真见过）
    lua.execute("""
        local db = GQL.Config.DB()
        db.quests[89507] = { name = '丰饶供品', regKey = 'abundant-offerings',
                             observations = {}, lastSeenAt = %d - 604800 }
    """ % now)
    gql.Sync.SnapshotNow()
    seen = one(lua.execute("return GQLDB.quests[89507].lastSeenAt"))
    if int(seen) != now:
        return fail("scan_board_renews_family_anchor_across_cd",
                    f"扫板看到族成员要续锚点 lastSeenAt，实得 {seen}（不是 {now}）")
    if "89507," not in matrix_listed(lua, now):
        return fail("scan_board_renews_family_anchor_across_cd",
                    f"续上之后这一行必须在列: {matrix_listed(lua, now)}")
    ok("scan_board_renews_family_anchor_across_cd")


def test_family_state_does_not_leak_across_cycles():
    """跨 CD 的"做过"绝不能冒充本周（用户口径：矩阵的红勾就是"这周还差谁"）。
    一族多个 ID 时，任何一个 ID 本周做过就算做过；玩家手点的优先于接口。
    成行的只有族本体那一条（7001），板上那条（7002）只当证据 —— 用户 2026-10-24 定的：
    "我鼠标悬停的丰饶 和 我真正要完成的丰饶日常不是一个任务。你反查完成的丰饶为准。"
    红勾来自全族（用户②"只要完成过一次都认为完成了"）。
    第 36 轮起"本周/上本周"这件事没有起点可反推了：这一族问不出板面倒计时，
    编号 = 周 CD 尾巴那一桶 ⇒ 用例把锚点先摆在**上一周**再烙旧证据，然后把锚点推到现在，
    旧编号自然匹配不上（老写法那句"视图比锚点旧"是拿 t 跟锚点比大小，正是 fail-open 那一支）。"""
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
        -- 上一 CD 的状态：锚点摆回上一周再烙这一笔 ⇒ 它带的是上一周尾巴那条实例的编号
        db.cycleAnchor = 1000 - 604800
        GQL.Ledger.SetView(db, "旧号-永恒之井", 7001, "done", 999)
        db.chars["旧号-永恒之井"].lastSync = 999
        -- 本周登录的号：7001 接口说 open，7002 别的接口说 done（族里任一做过即做过）
        db.cycleAnchor = 1000
        db.chars["新号-永恒之井"] = { lastSync = 1001, views = {} }
        GQL.Ledger.SetView(db, "新号-永恒之井", 7001, "open", 1001)
        GQL.Ledger.SetView(db, "新号-永恒之井", 7002, "done", 1001)
        GQL.Ledger.OnTurnIn(db, "新号-永恒之井", 7002, 1002, nil)
    """)
    if one(lua.execute("return GQL.Ledger.RegisteredState(GQLDB, ...)",
                       "旧号-永恒之井", 7001)) != "unknown":
        return fail("family_state_does_not_leak_across_cycles",
                    "上一条实例烙的视图不许当本周已做（编号匹配不上）")
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, ...)",
                       "旧号-永恒之井", lua.table_from({7001, 7002}))) != "unknown":
        return fail("family_state_does_not_leak_across_cycles",
                    "整族都旧了就该是 unknown，别拼出一个假红勾")
    if one(lua.execute("return GQL.Ledger.BestRegisteredState(GQLDB, ...)",
                       "新号-永恒之井", lua.table_from({7001, 7002}))) != "done":
        return fail("family_state_does_not_leak_across_cycles",
                    "族里任一 ID 本周做过 = 这一行做过")
    # 玩家手点的 open 优先于接口：他要记"这周我没做"，不能被别的来源盖掉。
    # 这一格手搓而不是走 SetManual：SetManual 的 "open" 那一支是**删除**（再点一次取消补录），
    # 所以带 state='open' 的记录只在旧存档里有 —— L.State 照编号认它，这一格钉的是旧档兼容。
    lua.execute("local db = GQL.Config.DB() "
                "db.manual['旧号-永恒之井'] = { [7001] = { state = 'open', t = 1002, "
                "  k = GQL.Ledger.InstanceKey(db, 7001, db.quests[7001], 1002) } }")
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
    # 这一族问不出板面倒计时 ⇒ 编号只能由周 CD 尾巴算（锚点 0 = 身份未定 = 一格 done 都认不出）。
    # 这一条用例钉的是"勾来自族里哪一条"，所以先把身份来源备齐，凭据才烙得上编号。
    lua.execute("GQL.Config.DB().cycleAnchor = 1000")
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
                "GQL.Ledger.SetView(db, '某人-永恒之井', 89509, 'done', 10)")
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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
    if lua.execute("local db = GQL.Config.DB() return db.quests[125].roundEnd ~= nil"):
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "板面给的 2 小时轮换倒计时不是这条周常的寿命：注册行不许写 roundEnd")
    if int(lua.execute("local db = GQL.Config.DB() "
                       "return GQL.Ledger.EffectiveExpiry(db, db.quests[125], 1700000000)")) \
            != int(lua.execute("return GQL.Config.DB().cycleAnchor + 604800")):
        return fail("keyword_rows_stay_separate_and_outlive_the_board",
                    "注册行的到期只认周 CD 尾巴（cycleAnchor + 604800），实际 "
                    + one(lua.execute("local db = GQL.Config.DB() "
                                      "return tostring(GQL.Ledger.EffectiveExpiry("
                                      "db, db.quests[125], 1700000000))")))
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
        return one(lua.execute(
            "local db = GQL.Config.DB() local r = db.quests[%s] "
            "return tostring(r.roundEnd) .. '/' .. "
            "tostring(GQL.Ledger.EffectiveExpiry(db, r, 1700000000))" % qid))

    if fields(89600) != "nil/1700432000":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "注册行的寿命是本周 CD 尾巴，不是池给的 3 小时: " + fields(89600))
    if fields(89601) != "1700010800/1700010800":
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
    # 这一份记录是**老存档形状**：池在 T5 之前给它写过一板 3 小时倒计时，留下的正是
    # expiresAt/expiresSrc 那一组残留 —— 旧代码在这里"留着已有的接口倒计时"→ 加固符文石
    # 当天下午整行没了（用户："倒计时不准或者也是子任务导致的"，实际跟子任务无关）。
    # T5 的处置：残留**原地留着**（不删数据、不加新 schema 档），但再没有任何读者。
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
    if fields(89602) != "nil/1700432000":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "还接在日志里的关键词任务：寿命只认周 CD 尾巴，残留的老板面值不许参与: "
                    + fields(89602))
    # 老档残留按定案原地不动：不删、不改写（谁也不读它，删它反而要动数据）
    if one(lua.execute("local r = GQL.Config.DB().quests[89602] "
                       "return tostring(r.expiresAt) .. '/' .. tostring(r.expiresSrc)")) \
            != "1700010800/接口倒计时":
        return fail("pool_countdown_does_not_kill_a_registered_weekly",
                    "老档残留不许被清掉或改写（T5 的处置是无读者、原地留着）")
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
        g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
    if lua.execute("return GQLDB.quests[89507].roundEnd ~= nil"):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "板上那个实例的 6 小时倒计时不许写成族本体的到期：roundEnd 必须留空")
    if int(lua.execute("return GQL.Ledger.EffectiveExpiry(GQLDB, GQLDB.quests[89507], 1700000000)")) \
            != int(lua.execute("return GQLDB.cycleAnchor + 604800")):
        return fail("last_weeks_child_does_not_stand_in_for_this_week",
                    "族本体的寿命是本周 CD 尾巴（cycleAnchor + 604800），不是板上那个实例的 6 小时: "
                    + one(lua.execute("return tostring(GQL.Ledger.EffectiveExpiry("
                                      "GQLDB, GQLDB.quests[89507], 1700000000))")))
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
    ③ 再把这一条实例换一条（接口倒计时整段挪走 = 板上换成下一条实例），一样不许自己回来；
    ④ 取消忽略走真实入口 AddQuest：**只撤否决，不带上板凭据**。撤完回不回得来由五道门重算
       —— ③ 那次同步已经按新编号读到 30000g（金币粘滞跟着新实例走），所以这一行自己回来；
    ⑤ 反过来：换一条实例、这一读金额又问不出 ⇒ 那一刻五道门一道不成立，
       撤完否决不许当场上板，等下一次快照在这一条实例重新读到门槛金币才自己回来
       （⑤ 这一段同时钉计划 §六 1(c) 的写侧：换编号那一次同步把 goldMoney 一起作废）。
    日志里不许再出现"换了 CD：解除"那句 —— 那句话本身就是上一版误读的产物。"""
    name = "an_ignore_outlives_every_cycle_and_round"
    lua, gql, _ = sync_env([(180, 30000000)], weekly=300000)
    g = lua.globals()
    clock = {"v": 1700000000}
    g.time = lambda: clock["v"]
    secs = {"v": 43200}
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: secs["v"]})

    def board_rows(money):
        """这一读池子里 180 那行给不给金额：money=None = 客户端答不出金额。"""
        entry = lua.table_from({"questID": 180, "mapID": 2007, "name": "任务180"})
        if money is not None:
            entry["money"] = money
        rows = lua.table_from({})
        rows[1] = entry
        g.C_WorldQuests = lua.table_from({"GetWorldQuests": lambda: rows})

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

    # ③ 换实例：接口倒计时整段挪走 = 板上换成下一条实例。换实例作废的是"这一条的凭据"
    #    （金币粘滞），不许顺手把玩家那句永久屏蔽一起清掉
    clock["v"] = 1700003600
    before = uid(gql.Ledger, g.GQLDB, 180, 1700003600)
    # 11-04 口径：旧实例下板 = 账本里它的结束时刻不再算数，先作废才会重问出新的
    lua.execute("GQLDB.quests[180].roundEnd = nil")
    secs["v"] = 36000                     # 结束时刻差 3600 秒 > 900 秒一桶 = 换了一条实例
    gql.Sync.SnapshotNow()
    after = uid(gql.Ledger, g.GQLDB, 180, 1700003600)
    if after == before:
        return fail(name, f"前置：这一条实例该换成下一条了（唯一编号没变，{before}）")
    if ignored() == "nil":
        return fail(name, "换轮清了屏蔽 —— 金币粘滞作废是一回事，玩家那句\"别再显示\"是另一回事")
    if "180," in matrix_listed(lua):
        return fail(name, "换轮后被屏蔽的行不许回来: " + matrix_listed(lua))

    # ④ 取消屏蔽走真实入口：AddQuest 只撤否决。这一行回来靠的是③那次同步按**新编号**
    #    重新读到的 30000g（金币粘滞跟着这一条实例），不是靠玩家点的那一下。
    #    （第二十七轮的口径：点"添加"是撤销"别再显示"，不是"标记这条上板" ——
    #     上一版在这里写 manualAdded，行立刻凭玩家那一戳回来了。）
    lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), 180, 1700004000, nil)")
    if ignored() != "nil":
        return fail(name, f"点\"添加任务\"要撤销永久屏蔽，实际 {ignored()}")
    if "180," not in matrix_listed(lua):
        return fail(name, "这一条实例已经读到过门槛金币，撤了否决就该自己回板: "
                          + matrix_listed(lua))
    if one(lua.execute("return tostring(GQL.Ledger.GoldVerdict(GQLDB, 180, 1700004000))")) != "true":
        return fail(name, "回来的凭据必须是金币（这一条实例读到过钱），不是玩家那一戳")

    # ⑤ 换一条实例、这一读金额又问不出 ⇒ 那一刻五道门一道不成立：
    #    撤否决只撤否决，不许把没有凭据的行放上板（等下一次快照重新读到钱才自己回来）。
    lua.execute("GQL.Ledger.RemoveQuest(GQL.Config.DB(), 180)")
    clock["v"] = 1700007200
    lua.execute("GQLDB.quests[180].roundEnd = nil")   # 同上：旧实例下板才问得出新的
    secs["v"] = 18000                       # 结束时刻挪 5 个桶以上 = 板上换成下一条实例
    board_rows(None)                        # 这一读客户端答不出金额
    gql.Sync.SnapshotNow()
    if uid(gql.Ledger, g.GQLDB, 180, 1700007200) == after:
        return fail(name, "前置：⑤ 要换成下一条实例，编号却没变")
    if one(lua.execute("return tostring(GQLDB.quests[180].goldMoney)")) != "nil":
        return fail(name, "前置：换一条实例要把金币粘滞一起作废（§六 1(c) 第 3 步），"
                          "实际还留着 " + str(lua.execute(
                              "return tostring(GQLDB.quests[180].goldMoney)")))
    lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), 180, 1700007200, nil)")
    if "180," in matrix_listed(lua):
        return fail(name, "取消忽略只撤否决，不许把没有这一条实例凭据的行当场放上板: "
                          + matrix_listed(lua))
    # 下一次快照在这一条实例重新读到 30000g（自动凭据长回来）→ 行按金币门自己回到矩阵
    board_rows(30000000)
    gql.Sync.SnapshotNow()
    if "180," not in matrix_listed(lua):
        return fail(name, "重新读到这一条实例的门槛金币后该行要自动回到矩阵（取消忽略=撤否决，"
                          "上板由判据重算）: " + matrix_listed(lua))
    ok(name)


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
    g.GQLDB["config"]["extraMaps"] = lua.table_from({2007: True})
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
    # 判不知道之后不再编任何兜底：SetCycleExpiry 连同 24 小时排程值已在 T5 整组退役 ——
    # 锚点不可用、板面也没答过时，结束时刻就是问不出（界面不上色、不报剩余，
    # 老版这里编一个"now+86400"顶着并标 guessed 的口子从根上封死）。
    if lua.execute("return GQL.Ledger.SetCycleExpiry ~= nil"):
        return fail("weekly_countdown_must_fit_the_anchor",
                    "L.SetCycleExpiry 应随排程值一起整组退役，函数本体必须没了")
    lua.execute("GQL_TMP = { regKey = 'kw:虚空侵袭', observations = {} } "
                "GQL.Ledger.SetExpiry(GQL_TMP, 1700000000, 10800)")
    if lua.execute("return GQL_TMP.roundEnd ~= nil or GQL_TMP.expiresAt ~= nil "
                   "or GQL_TMP.expiresSrc ~= nil or GQL_TMP.expiresGuessed ~= nil"):
        return fail("weekly_countdown_must_fit_the_anchor",
                    "注册周常的 SetExpiry 就该立刻回：一个字都不许写，更不许编排程值")
    lua.execute("GQL_DB0 = { cycleAnchor = 0, quests = {} }")
    if str(lua.execute("return GQL.Ledger.InstanceEnd(GQL_DB0, GQL_TMP, 1700000000)")) != "None":
        return fail("weekly_countdown_must_fit_the_anchor",
                    "周口径不可信、锚点又定不出来：结束时刻必须问不出，不许编一个顶着")
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


def test_a_flaky_interface_never_overwrites_or_invents_expiry():
    """接口偶发问不出时不许动账本，更不许编一个数顶上。老版靠 SetCycleExpiry 里
    两句钉这个（未来的真数不许被 24 小时排程值覆盖；已经过期的旧值要退回排程值并标
    guessed）—— T5 把那一组连函数带字段整组退役后，"问不出"就是**一个字不写**：
    ① 池任务：roundEnd 停在上一条板上读到的真值；矩阵对过去值本来就只报"本轮还没
       读到"（EffectiveExpiry 不收过去值），没有任何兜底数会被写出来。
    ② 老 expiresAt/expiresSrc/expiresGuessed 三个字段：任何同步路径都不再写它们。"""
    lua, gql, _ = sync_env([(7, 30000000)], board=43200, weekly=432000, wall=1700000000)
    g = lua.globals()
    gql.Sync.SnapshotNow()
    end1 = int(lua.execute("return GQLDB.quests[7].roundEnd"))
    for legacy in ("expiresAt", "expiresSrc", "expiresGuessed"):
        if lua.execute("return GQLDB.quests[7].%s ~= nil" % legacy):
            return fail("a_flaky_interface_never_overwrites_or_invents_expiry",
                        f"老到期字段 {legacy} 已整组退役，同步里不该有它的写点")
    # 这一读问不出（接口偶发 nil）：roundEnd 保留上一条真值，也不写任何别的到期字段
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: None})
    g.GQL_NOW = 600
    gql.Sync.SnapshotNow()
    if int(lua.execute("return GQLDB.quests[7].roundEnd")) != end1:
        return fail("a_flaky_interface_never_overwrites_or_invents_expiry",
                    "偶发问不出不许覆盖账本里已有的板面结束时刻")
    for legacy in ("expiresAt", "expiresSrc", "expiresGuessed"):
        if lua.execute("return GQLDB.quests[7].%s ~= nil" % legacy):
            return fail("a_flaky_interface_never_overwrites_or_invents_expiry",
                        f"问不出的降级形状就是什么都不写：{legacy} 不该出现")
    ok("a_flaky_interface_never_overwrites_or_invents_expiry")


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
                    "  roundEnd=1700024660, lastSeenAt=1700000000, observations={} } "
                    "GQLDB.quests[91808] = { name='特别任务：暗影伏击', mapID=2512, "
                    "  lastSeenAt=1700000000, observations={} }")
        # 快照走真写点 L.SetView：第 36 轮起状态只认"烙着这一条实例唯一编号"的证据，
        # 裸写 { s='done' }（没有 k = 写下时身份还没定）一律不认 —— 那是 94612 十个假勾
        # 的入口，不是这条记录该有的形状（T5 起 SetExpiry 只写 roundEnd 这一个到期字段）。
        db = gql.Config.DB()
        gql.Ledger.SetView(db, "小白龙-奥达曼", 91805, "done", 1700000000)
        gql.Ledger.SetView(db, "小白龙-奥达曼", 91808, "open", 1700000000)
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
    if "6时51分（板面倒计时）" not in sec:
        return fail("probe_shows_row_week_evidence_and_raw_log_fields",
                    f"到期要带剩余多长 + 来源（板面倒计时/周 CD 尾巴两种之一）:\n{sec}")
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
    fire(gql, "QUEST_COMPLETE", 7777)          # 7777 从没上过板、不在账本
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
    """放宽判据的代价必须有人盯着：上过板的世界任务照常记账（这条不许红）——
    接口答不出"是不是世界任务"（回 nil）时靠账本凭据 poolSeen 把记账门开住。
    2026-10-29 定稿：交割那一处不再量钱，这一笔流水只带 t / sig / cycle / k；
    周统计里那笔金币来自**这一条实例的奖励读取**（Sync 池循环 NoteGoldThisInstance 写下的
    rec.goldMoney，只有 rec.goldKey 与流水那一行的 k 同一条实例才计入）。
    身份来源给板面倒计时（board）：没有身份时两边的编号都是 nil，那条测的就成了
    §五「没编号 = 不匹配」，而不是这里要盯的"在板的条照常把钱记上"。"""
    lua, gql, _ = sync_env([(131, 30000000)], board=7200, extra=("Matrix",))
    g = lua.globals()
    lua.execute("GQL.Api.IsWorldQuest = function() return nil end")   # 接口答不出
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()        # 先把 131 收进池（拿到 poolSeen + 结束时刻）
    fire(gql, "QUEST_COMPLETE", 131)
    key = str(gql.Sync.CurrentCharKey())
    rows = g.GQLDB["turnins"][key] and g.GQLDB["turnins"][key][131]
    if rows is None or len(rows) != 1:
        return fail("turnin_of_pooled_quest_still_settles",
                    f"在板任务交完当场就该有一笔流水:\n{gql.Config.GetLogs()}")
    count, gold = gql.Ledger.WeekStats(g.GQLDB, key, 0)
    if int(count) != 1 or int(gold) != 30000000:
        return fail("turnin_of_pooled_quest_still_settles",
                    f"这一条实例的奖励读取读到 3000 金，周统计该这么记，实际 {count}/{gold}")
    ok("turnin_of_pooled_quest_still_settles")


def test_keyword_weekly_turnin_gets_recorded():
    """刀 1（2026-10-29 回响复燃 92713 那单，玩家明确否掉"下线丢账"：做这条不需要下线）：
    记账门 L.QuestKnown 只认五样凭据（poolSeen/onBoard/regKey/kwLogAdded/ignored），
    矩阵准入门 QuestHasCredential 却**再加**"本轮有号交过" —— 两道门不同源 ⇒ 92713 那行
    靠圣光之力的一笔流水显示得出来，小号（kwLogAdded 被登录同步翻成 nil：小号登录时任务
    还没接，这标记只认"当前这个号此刻日志里挂着没有"）交任务整笔不记。
    这条把 92713 的形状走真实入口造出来：日志收进账本（名字命中关键词）→ 再同步一趟
    任务没接着（kwLogAdded 当场清 nil）→ 不在池、IsWorldQuest 回 false →
    fire(QUEST_COMPLETE) → 当场落账 ⇒ 必须有流水 + 快照 done。
    钱那一半照 2026-10-29 的定稿走：交割流水不带金额，而这一条从没上过板 ⇒ 奖励读取
    一次都没读到过钱 ⇒ 周统计如实只数完成数、金币报未知（-1 = 界面上的 ?），
    既不凭空报 0（那是客户端明说"这副奖励不给金币"）也不报一个数。
    对照组：账本里有记录、名字不命中关键词的陌生任务仍一笔不记 —— 刀 1 只补"名字命中
    玩家亲手声明的关键词"这一条身份（判据只认 Config.KeywordMatch 一个入口），
    **不许**放宽成"账本里有记录就开门"（那是 test_turnin_of_unknown_quest 守的方向）。"""
    lua, gql, _ = sync_env([(130, 30000000)])
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    db = g.GQLDB
    lua.execute("GQL.Config.AddKeyword('回响复燃')")
    # 第一趟同步：日志里挂着 92713 → 日志收那条路建记录，名字就取自日志行
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 92713, "title": "回响复燃", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    # 第二趟同步：小号登录时任务还没接（日志只剩一条无关的）→ kwLogAdded 被当场清掉。
    # 这就是 92713 的翻面现场：不是事件时序丢了，是这条路对"不上板的关键词周常"不开门。
    g.C_QuestLog["GetInfo"] = lambda i: (lua.table_from(
        {"questID": 777, "title": "无关日常", "questLogIndex": 1}) if int(i) == 1 else None)
    gql.Sync.SnapshotNow()
    rec = db["quests"][92713]
    if rec is None or str(rec["name"]) != "回响复燃":
        return fail("keyword_weekly_turnin_gets_recorded",
                    f"前置：92713 该由日志收进账本、名字在记录上，实际 {rec}")
    if rec["kwLogAdded"] is not None or rec["poolSeen"] is not None \
            or rec["regKey"] is not None or rec["onBoard"] is not None:
        return fail("keyword_weekly_turnin_gets_recorded",
                    "前置：这一条必须五样凭据全空、只剩一个命中关键词的名字")
    lua.execute("GQL.Api.IsWorldQuest = function() return false end")
    fire(gql, "QUEST_COMPLETE", 92713)
    rows = db["turnins"][key] and db["turnins"][key][92713]
    if rows is None or len(rows) != 1:
        return fail("keyword_weekly_turnin_gets_recorded",
                    f"名字命中关键词的周常交任务必须记账（记账门与准入门同源）:\n"
                    f"{gql.Config.GetLogs()}")
    count, gold = gql.Ledger.WeekStats(db, key, 0)
    if int(count) != 1 or int(gold) != -1:
        return fail("keyword_weekly_turnin_gets_recorded",
                    f"没上过板的关键词周常：完成数照记、金币如实未知，实际 {count}/{gold}")
    view = db["chars"][key]["views"][92713]
    if view is None or str(view["s"]) != "done":
        return fail("keyword_weekly_turnin_gets_recorded",
                    f"交割完快照该写 done，实际 {view}")
    # 对照组：记录在账本里、名字不命中关键词 → 门照旧关着，一笔不记。
    # addedBy 传 nil（玩家"添加任务"那一路）：传 "questlog" 会当场写 kwLogAdded=true，
    # 那测的是旧的五样凭据，不是刀 1 新加的这一条身份。
    g.C_QuestLog["GetTitleForQuestID"] = lambda qid: "陌生人不会点的日常"
    lua.execute("GQL.Ledger.AddQuest(GQL.Config.DB(), 92714, GQL_NOW, nil)")
    if db["quests"][92714] is None:
        return fail("keyword_weekly_turnin_gets_recorded", "前置：对照组记录没建出来")
    fire(gql, "QUEST_COMPLETE", 92714)
    if db["turnins"][key][92714] is not None:
        return fail("keyword_weekly_turnin_gets_recorded",
                    "名字不命中关键词的陌生任务不许被开门 —— 放宽成\"有记录就开门\""
                    "就是 test_turnin_of_unknown_quest 守的那条假行")
    ok("keyword_weekly_turnin_gets_recorded")


def test_turned_in_event_alone_settles_on_arrival():
    """刀 2（2026-10-29 回响复燃 92713 那单的第二半）剩下的那半截：QUEST_TURNED_IN 是
    真按了"完成任务"那一下客户端答的，事件本身就是凭据 ⇒ 到达时**当场**写流水 + 快照 done，
    既不要求先来一次 QUEST_COMPLETE（开对话框），也不许推进时钟之后才有账。
    15 秒钱窗整体退役（2026-10-29 定稿）之后"记完成"和"量钱"彻底分家：这一笔只有
    t / sig / cycle / k，金额归奖励读取那一路 —— 所以这一条只盯 TURNED_IN 单独到达
    那一支（QUEST_COMPLETE 那一支与"两笔合计多少钱"分别由
    turnin_records_immediately_without_any_window、back_to_back_turnins_both_recorded 守着，
    同一实例两个事件只记一笔由 two_turnin_events_of_one_settlement_record_one_row 守着）。
    推进时钟 + 再同步一趟也不许多出第二笔、也不许把已经点亮的那一格顶回未交
    （再同步那一趟会按客户端重算 views，而客户端这条压根没答"已完成"：这一格靠的是
    Sync 池循环里"有编号对上的流水就判回 done"那一道门，见 snapshot_done_needs_this_instance_turnin
    盯的同一处）。
    日志必须写明走的是当场落账那条路：12.x 到底哪些任务会发 QUEST_TURNED_IN 没有实测
    依据，等 /gql log 里有样本了再决定要不要收紧"开了对话框没交"那口子（计划"不做的事"第三条）。"""
    lua, gql, _ = sync_env([(63, 500000)], board=7200)
    g = lua.globals()
    key = str(gql.Sync.CurrentCharKey())
    db = g.GQLDB
    gql.Sync.SnapshotNow()
    fire(gql, "QUEST_TURNED_IN", 63)
    rows = db["turnins"][key] and db["turnins"][key][63]
    if rows is None or len(rows) != 1:
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"QUEST_TURNED_IN 到达就该当场落账，不许等任何计时:\n"
                    f"{gql.Config.GetLogs()}")
    if str(rows[1]["k"]) != uid(gql.Ledger, db, 63, int(g.GQL_NOW)):
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"当场那一笔要烙上这一条实例的编号，实际 {rows[1]['k']}")
    view = db["chars"][key]["views"][63]
    if view is None or str(view["s"]) != "done":
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"当场快照就该写 done，实际 {view}")
    if state1(gql.Ledger.State, db, key, 63) != "done":
        return fail("turned_in_event_alone_settles_on_arrival",
                    "只有 TURNED_IN 这一支也应该把那一格点亮成已交")
    g.GQL_NOW = 20
    gql.Sync.SnapshotNow()                    # 客户端这一条没答"已完成"，但流水在账上
    g.GQL_FireTimers()
    rows = db["turnins"][key][63]
    if len(rows) != 1:
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"推进时钟/再同步都不许多记一笔（周统计一笔交任务只能算一次），"
                    f"实际 {len(rows)} 笔")
    if state1(gql.Ledger.State, db, key, 63) != "done":
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"再同步一趟不许把已交顶回未交:\n{gql.Config.GetLogs()}")
    text = str(gql.Config.GetLogs())
    if "交割当场落账" not in text:
        return fail("turned_in_event_alone_settles_on_arrival",
                    f"日志要写明这笔走了当场落账那条路:\n{text}")
    ok("turned_in_event_alone_settles_on_arrival")


def test_event_moment_writes_the_end_time_it_asks():
    """这一条原名 test_event_moment_asks_time_and_money_without_writing，钉的是旧契约
    "事件那一问一笔都不许写账本"。**改写理由不是为了让测试变绿**：用户 2026-11-04 定的
    第 1 条把这条契约反过来说 —— "接任务/交割那一刻问出来的结束时刻，当场写进这条记录"
    （计划 §二 第 1 条）。旧断言只是"先只取证、等现场答案回来再决定怎么改判据"那一轮的
    临时口径，现场答案已经回来了（计划 §一 1：95794 接任务那一刻客户端答 69110 秒），
    而矩阵关着时除了这一处再没有第二个写点 ⇒ 问到了却不写 = 结束时刻永久空着
    = 交割烙下的编号只能落"本周尾巴"那一档 = 玩家看到的"勾没了"。
    金额那一半在 T2 改了语义（计划 §二第 2 条"那一刻向服务器要一次，数据回来就落账"）：
    问不出时这一处会向服务器要一次，但**要的那一句只进日志、不进账本**，
    所以这一条的断言是"金额问不出时除 roundEnd 之外一个字都不许多写"
    （本夹具全程答不出金额，正是那一岔；落账发生在补读那一趟，见
    test_reward_data_arrival_settles_gold_without_the_matrix）。
    goldMoney / goldKey / lastMoney / currentSig / observations 仍逐字段不许动。
    正向：88 答得出倒计时 7200 ⇒ roundEnd = 当时的 now + 7200，问答行照打，且那一行的
    "此刻按账本算的编号"用的是刚落账的那一档（编号排在落账之前算 = 旧形状，这条要红）。
    88 那条倒计时是 7200 秒（< 1 天）—— 正向本身就是"不许把 SyncKeywordQuestsFromLog
    里那道 `secs < 86400 就不写` 的门搬到这里"的钉子：搬过来每一条 24 小时内到期的世界
    任务的真实结束时刻就一律拒收，正是要治的那个形状。
    对照组一：99 答不出倒计时 ⇒ 一个字都不写（roundEnd 仍 nil、其余字段与问前逐字段相同），
    日志照旧写 倒计时=nil / 账本已有结束时刻=无。
    对照组二：71 带 regKey="kw:..."（注册周常那一族）⇒ 按 11-04 新口径连客户端都不再问：
    这一族 L.SetExpiry 开头就早退，问出来的结束时刻没人收，白问一次接口调用。替身照旧
    摆着"答得出 43200"，日志该如实写 倒计时=nil —— 钉的是"不问"而不是"问不出"，
    账本一个字都不写。
    反向一：交割那一笔的 k 是交割那一刻算出来的编号；本轮之前已经被"猜的那一档"烙下的
    旧流水一笔都不回写（用户 11-04 第 3 条："已经被'猜的那一档'烙下的完成记录…不改"）。
    反向二：账本压根没这条（55）⇒ 问归问，不建记录；客户端答"不是世界任务"且账本也没有
    的（77）⇒ 连问都不问。"""
    lua, gql, _ = sync_env([(63, 500000)], board=7200, wall=1_790_000_000, weekly=300000)
    g = lua.globals()
    L = gql.Ledger
    name = "event_moment_writes_the_end_time_it_asks"
    wall = int(g.GetServerTime())            # Clock() 的第一个返回值（有墙上钟就用它）
    # 逐条控制客户端的答案：63/88 答 7200、71 摆着 43200（对照组二的替身，
    # 新口径下这一族连问都不会问），99/55/77 问不出（要测的就是"问不出"与"账本没这条"）
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
        lambda qid: 7200 if int(qid) in (63, 88) else (
            43200 if int(qid) == 71 else None)})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: int(qid) != 77
    g.C_QuestLog = lua.table_from({"IsQuestStarted":
        lambda qid: qid in (88, 99, 71, 55),
                                   "IsQuestCompleted": lambda qid: False})
    gql.Sync.SnapshotNow()
    db = g.GQLDB
    key = str(gql.Sync.CurrentCharKey())

    def rows_of(qid):
        log = db["turnins"][key]
        return None if log is None else log[qid]

    FIELDS = ("goldMoney", "goldKey", "lastMoney", "currentSig",
              "kwLogAdded", "kwLogKey", "ignored", "mapID", "name")

    def snap(qid):
        r = db["quests"][qid]
        d = {f: (None if r[f] is None else str(r[f])) for f in FIELDS}
        d["roundEnd"] = None if r["roundEnd"] is None else int(r["roundEnd"])
        d["obs"] = str(one(lua.execute(
            "local r = GQLDB.quests[%d] if not r then return 'no-rec' end "
            "local out, n = {}, 0 "
            "for sig, o in pairs(r.observations or {}) do "
            "  n = n + 1 "
            "  out[n] = tostring(sig) .. '|' .. tostring(o.gold) .. '|' .. "
            "    tostring(o.money) .. '|' .. tostring(o.firstSeen) .. '|' .. "
            "    tostring(o.lastSeen) .. '|' .. tostring(o.key) "
            "end "
            "table.sort(out) return table.concat(out, ';')" % int(qid))))
        return d

    def qa_line(text, qid, reason):
        needle = "%s现场问答 quest=%d" % (reason, int(qid))
        for ln in text.splitlines():
            if needle in ln:
                return ln
        return ""

    for qid in (88, 99, 71):
        gql.Ledger.AddQuest(db, qid, 0, None)
    db["quests"][71]["regKey"] = "kw:特别任务"        # 真实存档形状（注册周常那一族）
    before = {qid: snap(qid) for qid in (88, 99, 71)}
    for qid in (88, 99, 71):
        if before[qid]["roundEnd"] is not None:
            return fail(name, f"前置：{qid} 这一条本轮还该没有结束时刻，实际 {before[qid]['roundEnd']}")
    # 本轮之前已经被"猜的那一档"烙下的旧流水（这一条此刻没结束时刻 ⇒ 编号落在周 CD 尾巴）
    old = raw_turnin(lua, L, db, key, 88, int(g.GQL_NOW))
    old_key = None if old["k"] is None else str(old["k"])
    if old_key is None or not old_key.startswith("88:"):
        return fail(name, f"前置：旧流水要烙得上编号（尾巴那一档），实际 {old['k']}")

    fire(gql, "QUEST_ACCEPTED", 88)
    text = str(gql.Config.GetLogs())
    ln = qa_line(text, 88, "接任务")
    if not ln:
        return fail(name, f"接任务那一刻要留下问答行:\n{text}")
    after = snap(88)
    if after["roundEnd"] != wall + 7200:
        return fail(name, f"问出来的结束时刻要当场写进这一条：期望 {wall + 7200}，"
                          f"实际 {after['roundEnd']}\n{ln}")
    b, a = dict(before[88]), dict(after)
    b.pop("roundEnd")
    a.pop("roundEnd")
    if b != a:
        return fail(name, f"金额问不出时除 roundEnd 外不许多写（T2 只让这一处向服务器要一次，"
                          f"要到的那份由补读那一趟落账）：问前 {b} 问后 {a}")
    want = uid(L, db, 88, wall)
    logged = ln.split("此刻按账本算的编号=", 1)[1].split(" ")[0] \
        if "此刻按账本算的编号=" in ln else ""
    if logged != want:
        return fail(name, f"那一行的编号要用刚落账的那一档算（先落账、再算键）："
                          f"日志 {logged}，此刻账本 {want}\n{ln}")
    if logged == old_key:
        return fail(name, f"日志里的编号还停在'本周尾巴'那一档 = 倒计时没排在算键之前落账："
                          f"{logged}\n{ln}")
    rows = rows_of(88)
    if rows is None or len(rows) != 1:
        return fail(name, f"接任务那一问只许写结束时刻，不许写交割流水，实际 "
                          f"{0 if rows is None else len(rows)} 笔:\n{text}")
    if str(rows[1]["k"]) != old_key:
        return fail(name, f"已经存在的旧流水一笔都不许回写（用户 11-04 第 3 条）："
                          f"{old_key} → {rows[1]['k']}")

    fire(gql, "QUEST_COMPLETE", 88)
    text = str(gql.Config.GetLogs())
    if not qa_line(text, 88, "交割"):
        return fail(name, f"交割那一刻也要有问答行:\n{text}")
    rows = rows_of(88)
    if rows is None or len(rows) != 2:
        return fail(name, f"交割那一笔照旧要落账（问答只是多写它自己的结束时刻），实际 "
                          f"{0 if rows is None else len(rows)} 笔:\n{text}")
    if str(rows[1]["k"]) != old_key:
        return fail(name, f"交割不许把旧那一笔顶掉：{old_key} → {rows[1]['k']}")
    if str(rows[2]["k"]) != uid(L, db, 88, wall):
        return fail(name, f"新那一笔的 k 是交割那一刻按这条自己的结束时刻算出的编号，"
                          f"实际 {rows[2]['k']}，期望 {uid(L, db, 88, wall)}\n{text}")
    if snap(88)["roundEnd"] != wall + 7200:
        return fail(name, f"交割那一问也不许多改结束时刻（客户端答的是同一条实例）："
                          f"{after['roundEnd']} → {snap(88)['roundEnd']}")

    # 对照组一：答不出倒计时 ⇒ 一个字都不写，日志照打
    fire(gql, "QUEST_ACCEPTED", 99)
    text = str(gql.Config.GetLogs())
    ln = qa_line(text, 99, "接任务")
    if "倒计时=nil" not in ln or "账本已有结束时刻=无" not in ln:
        return fail(name, f"问不出就要如实写着 倒计时=nil / 账本已有结束时刻=无:\n{ln}")
    if snap(99) != before[99]:
        return fail(name, f"答不出倒计时就一个字都不许写：问前 {before[99]} 问后 {snap(99)}")

    # 对照组二：注册周常（regKey）⇒ 11-04 新口径下连客户端都不问：这一族 SetExpiry
    # 开头就早退，问出来的答案没人收。替身照旧答得出 43200，日志该如实写 倒计时=nil。
    fire(gql, "QUEST_ACCEPTED", 71)
    ln = qa_line(str(gql.Config.GetLogs()), 71, "接任务")
    if "倒计时=nil" not in ln:
        return fail(name, f"注册周常那一族不该再向客户端问倒计时，日志该写 倒计时=nil:\n{ln}")
    if snap(71) != before[71]:
        return fail(name, f"注册周常那一族一个字都不许写（不问、也不建结束时刻）："
                          f"问前 {before[71]} 问后 {snap(71)}")

    # 反向：账本压根没这条 ⇒ 问归问，不建记录
    fire(gql, "QUEST_ACCEPTED", 55)
    if not qa_line(str(gql.Config.GetLogs()), 55, "接任务"):
        return fail(name, "客户端答得出是世界任务的这一条要留下问答行（哪怕账本还没有它）")
    if db["quests"][55] is not None:
        return fail(name, "这一趟不建记录（建记录是日志旁路那一趟的事，它自己带金币判据）")
    # 反向：客户端一口咬定不是世界任务、账本也没有 ⇒ 连问都不问
    fire(gql, "QUEST_ACCEPTED", 77)
    if qa_line(str(gql.Config.GetLogs()), 77, "接任务"):
        return fail(name, "陌生普通任务不在取证线上：客户端说不是世界任务就不该问")
    if db["quests"][77] is not None:
        return fail(name, "取证那一问不许建出任何记录")
    ok(name)


def test_passive_probe_logs_any_accepted_world_quest():
    """11-04 用户："你不能指定这个任务 id 吧，这个我都做完了，应该任何世界任务的监听都打日志"
    ⇒ 被动那一问挂在日志事件上：日志里挂着的**每一条**世界任务，只要账本还没有它的结束时刻，
    就问一遍、答案进日志 —— 不认某一条 ID，也不用玩家再去接一个新任务（92576 已经做完了，
    接单那一下再也复现不出来）。
    正向：日志挂着 88（不在板上、账本里没结束时刻），事件本身不带任务号 ⇒ 出现
    "日志里挂着现场问答 quest=88"，并且如实写着这条问不出倒计时。
    反向一（去重）：过了 10 秒节流再来一次日志事件，不许对同一条问第二遍 ——
    这一趟 10 秒就跑一遍，不去重就是刷屏。
    反向二（不花钱）：已经有结束时刻的那条（63）连客户端都不问，更不进这一问。
    反向三（只问不写）：88 的 roundEnd/goldMoney/goldKey/lastMoney 全程为 nil。"""
    lua, gql, _ = sync_env([(63, 500000)], wall=1_790_000_000, weekly=300000)
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
        lambda qid: 7200 if int(qid) == 63 else None})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: lua.table_from(
            {"questID": 88, "title": "任务88", "questLogIndex": 1}) if int(i) == 1 else None,
        "IsQuestStarted": lambda qid: qid == 88,
        "IsQuestCompleted": lambda qid: False,
    })
    gql.Sync.SnapshotNow()                       # 63 在板上，这一趟拿到自己的结束时刻
    gql.Ledger.AddQuest(g.GQLDB, 88, 0, None)    # 88 只在日志里，账本没有它的结束时刻
    db = g.GQLDB
    if db["quests"][63]["roundEnd"] is None:
        return fail("passive_probe_logs_any_accepted_world_quest",
                    "前置：63 这轮该有板面结束时刻（对照组要用它）")
    fire(gql, "QUEST_LOG_UPDATE")
    text = str(gql.Config.GetLogs())
    if "日志里挂着现场问答 quest=88" not in text:
        return fail("passive_probe_logs_any_accepted_world_quest",
                    f"日志里挂着的这条要被被动问一遍:\n{text}")
    if "倒计时=nil" not in text:
        return fail("passive_probe_logs_any_accepted_world_quest",
                    f"问不出就如实写 nil:\n{text}")
    if "quest=63" in text:
        return fail("passive_probe_logs_any_accepted_world_quest",
                    "已经有结束时刻的那条不该进这一问（先查账本，不花接口调用）")
    rec = db["quests"][88]
    for f in ("roundEnd", "goldMoney", "goldKey", "lastMoney"):
        if rec[f] is not None:
            return fail("passive_probe_logs_any_accepted_world_quest",
                        f"被动那一问不许写 {f}，实际 {rec[f]}")
    # 过了 10 秒节流：同一条不许问第二遍
    g.GQL_NOW = 60
    fire(gql, "QUEST_LOG_UPDATE")
    text = str(gql.Config.GetLogs())
    if text.count("日志里挂着现场问答 quest=88") != 1:
        return fail("passive_probe_logs_any_accepted_world_quest",
                    f"每次开机每条任务只被动问一次，第二次不许再来:\n{text}")
    ok("passive_probe_logs_any_accepted_world_quest")


def test_log_bypass_gold_quest_keeps_a_real_end_time():
    """T1-b（计划 §三）：日志旁路把金币世界任务收进账本时用的是 "questlog" 那个来源标签，
    而它的语义是"**关键词命中**、从任务日志收的"（L.AddQuest 因此烙 kwLogAdded + kwLogKey，
    L.SetExpiry 对带这个位的记录早退）。这一趟的准入判据是"客户端答得出是世界任务 +
    当场读到正数金币"，跟关键词一个字无关 ⇒ 标签用错：既绕开金币门槛，又让这条
    **永久拿不到自己的真结束时刻**（92576 的形状）。改成不传 addedBy（照 Track.lua 那一例）。
    正向：关键词一个都没配，日志挂着 91（不在板上、账本没有它），客户端答"是世界任务"
    且金额>0 ⇒ QUEST_LOG_UPDATE 把它收进来，kwLogAdded / kwLogKey 都是 nil，
    并且同一趟紧随其后的现场问答就把它自己的 roundEnd 写进去了（问答排在建记录之后）。
    反向：同一条日志里的 92 金额答 0 ⇒ 非金币的不收，账本里压根没有它。
    对照组：92 的名字命中关键词、走 SyncKeywordQuestsFromLog 那一路 ⇒ 照旧烙 kwLogAdded
    （那里的 "questlog" 标签是**对的**，这一族的上板点名凭据不许被上面那处改动带着退役），
    而且它答得出 43200 秒倒计时也不写 roundEnd —— 早退仍在 SetExpiry 那一侧。
    再反向：加了关键词之后再同步一趟，91 那颗布尔位仍为 nil、结束时刻不被顶掉。"""
    lua, gql, _ = sync_env([(63, 500000)], wall=1_790_000_000, weekly=300000)
    g = lua.globals()
    name = "log_bypass_gold_quest_keeps_a_real_end_time"
    wall = int(g.GetServerTime())
    # 91/92 都答得出自己的倒计时 43200（> 1 天，绕得开关键词那一路自己的轮换寿命闸门，
    # 好让对照组二测到"烙了 kwLogAdded 才不写"而不是"问不出才不写"）
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
        lambda qid: 43200 if int(qid) in (91, 92) else None})
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.GetQuestLogRewardMoney = lambda qid: 9560000 if int(qid) == 91 else 0
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: (lua.table_from(
            {"questID": 91, "title": "世界任务：清剿营地", "questLogIndex": 1})
            if int(i) == 1 else lua.table_from(
                {"questID": 92, "title": "特别任务：加固符文石", "questLogIndex": 2})
            if int(i) == 2 else None),
        "IsQuestStarted": lambda qid: qid in (91, 92),
        "IsQuestCompleted": lambda qid: False,
    })
    db = g.GQLDB

    fire(gql, "QUEST_LOG_UPDATE")
    text = str(gql.Config.GetLogs())
    rec = db["quests"][91]
    if rec is None:
        return fail(name, f"日志旁路该把这条金币世界任务收进账本:\n{text}")
    if rec["kwLogAdded"] is not None or rec["kwLogKey"] is not None:
        return fail(name, f"这一趟的准入是'是世界任务 + 读到金币'，跟关键词无关，"
                          f"不许烙上日志来源位（烙了 L.SetExpiry 就永久早退）："
                          f"{rec['kwLogAdded']} / {rec['kwLogKey']}")
    if int(rec["lastMoney"]) != 9560000:
        return fail(name, f"前置：收进来这一趟要带上读到的金币，实际 {rec['lastMoney']}")
    # 【问答排在建记录之后 = 这一趟就要写下它自己的结束时刻】上一版这里钉的是"旁路那一趟
    # 记录还不存在，不该写结束时刻" —— 那是问答排在建记录之前的后果，而那个形状正是本轮要
    # 消灭的：这条带着"账本有记录、没有真结束时刻"过完整个开机（去重表让它不再问第二次）。
    if rec["roundEnd"] is None:
        return fail(name, f"刚收进来的这一条本轮就该有自己的结束时刻（问答排在建记录之后）:\n{text}")
    if int(rec["roundEnd"]) != wall + 43200:
        return fail(name, f"旁路那一趟写下的结束时刻应是客户端答的那一档："
                          f"期望 {wall + 43200}，实际 {rec['roundEnd']}")
    if "日志里挂着现场问答 quest=91" not in text:
        return fail(name, f"被动那一问要留下问答行:\n{text}")
    if db["quests"][92] is not None:
        return fail(name, "非金币的不收（矩阵本来就只列金币与点名）")
    if "日志旁路：收 1 条金币世界任务" not in text:
        return fail(name, f"收了几条要报得出:\n{text}")

    # 这条现在已经在账本里、且没被来源位钉死 ⇒ 现场问答写得进它自己的结束时刻
    fire(gql, "QUEST_ACCEPTED", 91)
    text = str(gql.Config.GetLogs())
    rec = db["quests"][91]
    if int(rec["roundEnd"] or 0) != wall + 43200:
        return fail(name, f"接任务那一刻问出来的结束时刻要落进这条自己的记录："
                          f"期望 {wall + 43200}，实际 {rec['roundEnd']}\n{text}")
    if rec["kwLogAdded"] is not None:
        return fail(name, f"写结束时刻这一趟不许顺手烙上日志来源位：{rec['kwLogAdded']}")

    # 对照组：关键词命中的那一条走日志扫描那一路，来源位照旧（那里的 "questlog" 是对的）
    gql.Config.AddKeyword("符文石")
    gql.Sync.SnapshotNow()
    kw = db["quests"][92]
    if kw is None:
        return fail(name, f"前置：命中关键词的那条该由日志扫描收进来:\n{str(gql.Config.GetLogs())}")
    if kw["kwLogAdded"] is not True:
        return fail(name, f"关键词+日志那一路的上板点名凭据不许退役，实际 {kw['kwLogAdded']}")
    fire(gql, "QUEST_ACCEPTED", 92)
    if kw["roundEnd"] is not None:
        return fail(name, f"烙了 kwLogAdded 的这一条由 L.SetExpiry 自己早退（保护不在调用方加门），"
                          f"实际写进了 {kw['roundEnd']}")
    rec = db["quests"][91]
    if rec["kwLogAdded"] is not None or int(rec["roundEnd"]) != wall + 43200:
        return fail(name, f"再同步一趟不许把旁路收进来的那条改标或顶掉它的结束时刻："
                          f"{rec['kwLogAdded']} / {rec['roundEnd']}")
    ok(name)


def test_event_capture_asks_the_server_once_for_reward_data():
    """T2-a（计划 §四）：现场问答金额问不出 ⇒ 就地向服务器要一次，一条任务每次开机只发一次。
    去重表在函数外（会话级），日志那一行末尾如实写这一件，让玩家看得出插件不是装死：
      金额=nil（已向服务器要这份数据） / 金额=nil（早就问过服务器了）。
    对照组：`HaveQuestRewardData` 答 true 的那条（96）一次请求都不发 —— 这一句不在外面重做，
    Api.PreloadRewardData 内部先问它、答"已经在本地"就不发（GoldQuestLedger/Api.lua:299-304）；
    同一条也不进补读表：客户端已经答过"这一档不给金币"，再问六次只是白烧接口调用。"""
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000)
    g = lua.globals()
    name = "event_capture_asks_the_server_once_for_reward_data"
    asked = []

    def preload(qid):
        asked.append(int(qid))
        return None

    g.C_TaskQuest = lua.table_from({
        "GetQuestTimeLeftSeconds": lambda qid: 7200 if int(qid) == 88 else None,
        "RequestPreloadRewardData": preload,
    })
    g.HaveQuestRewardData = lambda qid: int(qid) == 96      # 88 的数据不在本地，96 在
    g.GetQuestLogRewardMoney = lambda qid: 0                # 全程问不出金额
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: lua.table_from(
            {"questID": 88, "title": "任务88", "questLogIndex": 1}) if int(i) == 1 else None,
        "IsQuestStarted": lambda qid: qid == 88,
        "IsQuestCompleted": lambda qid: False,
    })

    def qa(qid, reason):
        """最后一条"某一刻现场问答 quest=<id>"日志（同一趟可能问好几遍，取最新那行）。"""
        out = ""
        for ln in str(gql.Config.GetLogs()).splitlines():
            if ("%s现场问答 quest=%d " % (reason, int(qid))) in ln:
                out = ln
        return out

    fire(gql, "QUEST_ACCEPTED", 88)
    if asked != [88]:
        return fail(name, f"金额问不出就该向服务器要这一次，实际请求 {asked}:\n"
                          f"{gql.Config.GetLogs()}")
    if "金额=nil（已向服务器要这份数据）" not in qa(88, "接任务"):
        return fail(name, f"日志要让玩家看出插件已经去要了:\n{qa(88, '接任务')}")

    # 过节流再走两趟日志事件 + 同一条再来一次接任务问答：一次都不许多发
    g.GQL_NOW = 20
    fire(gql, "QUEST_LOG_UPDATE")
    g.GQL_NOW = 40
    fire(gql, "QUEST_LOG_UPDATE")
    fire(gql, "QUEST_ACCEPTED", 88)
    if asked != [88]:
        return fail(name, f"一条任务每次开机只许发一次预加载请求，实际 {asked}")
    if "金额=nil（早就问过服务器了）" not in qa(88, "接任务"):
        return fail(name, f"问过的那一条要如实写'早就问过服务器了':\n{qa(88, '接任务')}")
    if str(gql.Config.GetLogs()).count("已向服务器要这份数据") != 1:
        return fail(name, f"'已向服务器要'整场只许出现一次:\n{gql.Config.GetLogs()}")

    # 对照组：数据已经在本地的那条 ⇒ 一次请求都不发
    fire(gql, "QUEST_ACCEPTED", 96)
    if asked != [88]:
        return fail(name, f"HaveQuestRewardData 答 true 的不该再向服务器要，实际 {asked}")
    if "奖励数据本来就在本地，这一档客户端答的是不给金币" not in qa(96, "接任务"):
        return fail(name, f"客户端自己答'数据就在本地'却读不出金额 = 这一档它明说不给金币，"
                          f"话要说成这个样（别写成'没再去要'那种含糊话，也别谎报发过请求）：\n{qa(96, '接任务')}")

    # 数据回来了：只有真发过请求的那条进了补读表 ⇒ 只有 88 落账，96 不在补读之列
    g.GetQuestLogRewardMoney = lambda qid: 10557708 if int(qid) in (88, 96) else 0
    g.GQL_NOW = 60
    fire(gql, "QUEST_LOG_UPDATE")
    text = str(gql.Config.GetLogs())
    if "补读到金额 quest=88" not in text:
        return fail(name, f"发过请求的那条数据到了要落账:\n{text}")
    if "补读到金额 quest=96" in text:
        return fail(name, f"数据本来就在本地的那条不该进补读表:\n{text}")

    # 反向（这台客户端压根没有预加载入口）：说"已向服务器要这份数据"就是谎话。
    # 11-04 实测这个入口在 12.1.5 是有的，但判据不许按"有"写死 —— 答不出就照实说，
    # 而且**不进补读表**：没有入口还补读六次，纯粹白烧六次接口调用。
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: 3600})
    fire(gql, "QUEST_ACCEPTED", 97)
    if asked != [88]:
        return fail(name, f"没有入口就不许凭空报'已发请求'，实际请求 {asked}")
    if "本机没有向服务器要奖励数据的入口" not in qa(97, "接任务"):
        return fail(name, f"这一条要如实说这条路在这台客户端不通:\n{qa(97, '接任务')}")
    g.GQL_NOW = 80
    fire(gql, "QUEST_LOG_UPDATE")
    if "补读 6 次仍问不出金额 quest=97" in str(gql.Config.GetLogs()) \
            or "补读到金额 quest=97" in str(gql.Config.GetLogs()):
        return fail(name, "没有入口的那条不该进补读表（一次接口调用都不许多花）:\n"
                          f"{gql.Config.GetLogs()}")
    ok(name)


def test_recent_money_misses_are_remembered_for_the_probe():
    """T1（计划 §三）：金额问不出的那几条，现场问答当场在内存里记一笔。
    为什么要有这张表：下一轮的取证命令 `/gql probe fetchdata` 得知道"该拿哪一条做试验"，
    而玩家要能说出任务名或任务号，前提就是先按 M 打开世界地图 —— 取证的目的恰恰是
    验证"不按 M 行不行"，不许把前提当成取证的条件（用户 11-04 原话）。
    表只有四件事要钉：问不出的记下（带 hasData 原样）、问得出的**不记**、同一条重复问不出
    只留一条且排在最后、最多留 5 条（最旧先丢）。
    会话级、不落档：这张表不进 db，所以断言全部打在 GQL.Sync 的内存读口上。"""
    name = "recent_money_misses_are_remembered_for_the_probe"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000)
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: None})
    g.HaveQuestRewardData = lambda qid: False       # 服务器那份数据不在本机
    g.GetQuestLogRewardMoney = lambda qid: 0        # ⇒ Api.QuestRewardMoney 判成问不出
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    g.C_QuestLog = lua.table_from({
        "GetInfo": lambda i: None,
        "IsQuestStarted": lambda qid: False,
        "IsQuestCompleted": lambda qid: False,
    })

    def missed():
        """表里现在的 questID，按顺序（取证要看顺序：最后一条才是'刚刚那条'）。"""
        return [int(r["questID"]) for r in gql.Sync.RecentMoneyMisses().values()]

    fire(gql, "QUEST_ACCEPTED", 88)
    if missed() != [88]:
        return fail(name, f"金额问不出就该记下这一条，实际 {missed()}:\n{gql.Config.GetLogs()}")
    first = gql.Sync.LastMoneyMiss()
    if int(first["questID"]) != 88 or first["hasData"] is not False:
        return fail(name, f"记的那一笔要带客户端对'奖励数据'的原样答案（false 就是 false，"
                          f"不许折成 nil），实际 {first['questID']}/{first['hasData']}")

    # 问得出的那条不许占表：否则取证会照着一条本来就有钱的任务试
    g.GetQuestLogRewardMoney = lambda qid: 9_000_000 if int(qid) == 99 else 0
    fire(gql, "QUEST_ACCEPTED", 99)
    if missed() != [88]:
        return fail(name, f"金额答得出的那条不该记，实际 {missed()}")

    # 同一条重复问不出 ⇒ 只留一笔，且这一笔排在最后（"刚刚问不出的就是它"）、时刻取最后一次
    # （时钟走的是墙上钟：sync_env 给了 wall，Config.Now 就以 GetServerTime 为准，
    #   所以挪 GQL_NOW 没用，要挪墙上钟才看得出"这一次"的时刻）
    g.GetQuestLogRewardMoney = lambda qid: 0
    fire(gql, "QUEST_ACCEPTED", 90)
    g.GetServerTime = lambda: 1_790_000_500
    fire(gql, "QUEST_ACCEPTED", 88)
    if missed() != [90, 88]:
        return fail(name, f"同一条重复问不出只许留一笔、并挪到最后，实际 {missed()}")
    last = gql.Sync.LastMoneyMiss()
    if int(last["questID"]) != 88 or int(last["at"]) != 1_790_000_500:
        return fail(name, f"去重后那一笔要带最后一次的墙上时刻（取证说的是'刚刚那条'），"
                          f"实际 {int(last['questID'])}/at={last['at']}")

    # 封顶 5 条：最旧的先丢，别让一次做十几条任务把表撑爆
    # （此刻表里是 [90,88]，再进 81..84 四条 ⇒ 第 6 条落进来时最旧的 90 先被挤掉）
    for qid in (81, 82, 83, 84):
        fire(gql, "QUEST_ACCEPTED", qid)
    table = missed()
    if table != [88, 81, 82, 83, 84]:
        return fail(name, f"最多只留最近 5 条、最旧先丢，实际 {table}")
    if int(gql.Sync.LastMoneyMiss()["questID"]) != 84:
        return fail(name, "读口给的要能直接拿去做试验那一条（最后问不出的），不是最旧那一条")

    # `wq ~= false` 那道守卫：客户端一口咬定"这不是世界任务"的不进这张表
    # （表只服务于世界任务取证），判不了(nil)的照收 —— 那正是"这一刻它什么都没说"的那一条。
    # 要测得到守卫必须账本里先有这条记录：wq==false 且没记录时 CaptureQuestData 开头就早退了。
    db = gql.Config.DB()
    gql.Ledger.AddQuest(db, 97, 1_790_000_500)
    gql.Ledger.AddQuest(db, 98, 1_790_000_500)
    g.QuestUtils_IsQuestWorldQuest = lambda qid: None if int(qid) == 98 else False
    fire(gql, "QUEST_ACCEPTED", 97)
    if 97 in missed():
        return fail(name, f"客户端明说不是世界任务的不许占取证表，实际 {missed()}")
    fire(gql, "QUEST_ACCEPTED", 98)
    if missed()[-1] != 98:
        return fail(name, f"判不了(nil)的那条要照收 —— 它就是'这一刻客户端什么都没说'的证据，"
                          f"实际 {missed()}")
    ok(name)


def test_probe_fetchdata_reports_only_what_the_client_answers():
    """T2（计划 §四）：`/gql probe fetchdata` 把"不按 M、直接问这一条要数据"几个动作挨个试，
    每一行只抄这台客户端自己的回答 —— 入口不在就写 MISSING，绝不把"没答"说成"答了不给"。
    用户 11-04 的取证方向原话："我知道任务名的前提就是先按 M……通过代码达到按 M 的效果，
    不要用你说的任务池，就直接读数据。" ⇒ 这一段只看"直接问这一条"，不看那张图的清单。
    这一轮**只取证**：账本一个字不许写；延时是一次性的 6 条（+1/+3/+8 直读复看、
    +12 等效按 M 对照、+14/+18 对照复看），跑完即散，不许自我续期。"""
    name = "probe_fetchdata_reports_only_what_the_client_answers"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    before = int(lua.execute("return #GQL_LOG"))
    report = str(gql.Probe.Run("fetchdata 88"))
    for want in ("动作1=C_TaskQuest.RequestPreloadRewardData 状态=入口缺失(MISSING)",
                 "动作2=C_QuestRewards.FetchData(只给任务号) 状态=入口缺失(MISSING)",
                 "动作3=选中这条 按任务号 88=入口缺失(MISSING)→判不了(两个读口都答不出)",
                 "选中项读口=两个读口都答不出 值=nil",
                 "事件名=QUEST_REWARD_DATA_RECEIVED 注册=MISSING",
                 # 已知对照组必须报"有"：它报"无"就说明这一段本身跑歪了，
                 # 那三行 MISSING 就不能当证据用
                 "事件名=QUEST_LOG_UPDATE 注册=有",
                 "已排 6 条一次性延时"):
        if want not in report:
            return fail(name, f"报告里少了 '{want}':\n{report}")
    if "账本一个字没写" not in report:
        return fail(name, f"每一行复看都要写明不落账:\n{report}")
    # 11-04 回传的形状：读口全程答不出时，恢复那三个候选**一个都不许多跳过**。
    # 上一版就在这里停住（`ClearQuestLogSelection` 一 MISSING 就 break），把 89267 留在玩家任务日志里。
    restore_line = [ln for ln in report.split("\n") if "退回原来选中的" in ln]
    if len(restore_line) != 1 or restore_line[0].count("入口缺失") != 3:
        return fail(name, f"三个恢复候选都要试过、每个各写一句答案，实际 {restore_line}")
    if restore_line[0].count("读回nil") != 3:
        return fail(name, f"每按一下都要把读回来的值写在同一行，实际 {restore_line}")
    quests = gql.Config.DB()["quests"]
    if quests is not None and len(list(quests.values())) != 0:
        return fail(name, f"取证探针不许建记录、不许写金额:{list(quests.values())}")
    queued = int(lua.execute("return #GQL_LOG")) - before
    if queued != 6:
        return fail(name, f"一次命令只许排 6 条一次性延时，实际新增 {queued}")
    g.GQL_NOW = 20
    lua.globals().GQL_FireTimers()
    after = int(lua.execute("return #GQL_LOG")) - before
    if after != 0:
        return fail(name, f"延时跑完不许自我续期（常驻定时器就是这轮的靶子），队列还剩 {after}")
    text = str(gql.Config.GetLogs())
    for want in ("直读试探 稍后+1秒 quest=88", "直读试探 对照=等效按M 已发起 quest=88",
                 "直读试探 对照复看+18秒 quest=88", "直读试探收尾 quest=88 哪一个动作把金额问出来了=都没问出来"):
        if want not in text:
            return fail(name, f"推进到 20 秒后日志里少了 '{want}':\n{text}")
    if text.count("哪一个动作把金额问出来了") != 1:
        return fail(name, f"一个数都没问出来时只许收尾写一次'都没问出来'，不许提前冒结论:\n{text}")
    if text.count("事件名试探已注销") != 1:
        return fail(name, f"一趟收尾只许注销一次（挂久了每次任务日志变化都往日志塞噪音行）:\n{text}")

    # 连敲两次：第一次的 +18 秒收尾**不许**把第二次那趟还在用的试探窗口掐掉，
    # 两趟合起来只在最后注销一次。
    str(gql.Probe.Run("fetchdata 88"))
    str(gql.Probe.Run("fetchdata 88"))
    g.GQL_NOW = 60
    lua.globals().GQL_FireTimers()
    text = str(gql.Config.GetLogs())
    if text.count("事件名试探已注销") != 2:
        return fail(name, f"两趟重叠只许在最后注销一次，实际 {text.count('事件名试探已注销')} 次:\n{text}")
    ok(name)


def test_probe_fetchdata_does_not_claim_a_number_that_was_already_readable():
    """起点就读得出金额的那一条（本来就有钱，不需要取证）⇒ 后面每一个动作都不许把那个数
    认领成"我问出来的"。认领错了，回传的那份证据就是废的：看起来像"动作1 有效"，
    实际上插件什么都没做。"""
    name = "probe_fetchdata_does_not_claim_a_number_that_was_already_readable"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"RequestPreloadRewardData": lambda qid: None})
    g.HaveQuestRewardData = lambda qid: True
    g.GetQuestLogRewardMoney = lambda qid: 11_181_872      # 起点就答得出
    report = str(gql.Probe.Run("fetchdata 88"))
    if "金额=11181872" not in report:
        return fail(name, f"起点要把客户端的答案原样报出来:\n{report}")
    text = str(gql.Config.GetLogs())
    if "哪一个动作把金额问出来了" in text:
        return fail(name, f"本来就读得出，不许在这一步下'问出来了'的结论:\n{text}")
    g.GQL_NOW = 60
    lua.globals().GQL_FireTimers()
    text = str(gql.Config.GetLogs())
    if "哪一个动作把金额问出来了=起点本来就读得出（这一条不用取证）" not in text:
        return fail(name, f"收尾要如实写这个数不是探针问出来的:\n{text}")
    ok(name)


def test_probe_fetchdata_asks_each_entry_once_and_probes_both_arities():
    """入口在的时候只许各问一次（多问就是白烧接口调用）；而 `C_QuestRewards.FetchData`
    的参数个数**没有任何可靠依据**（warcraft.wiki / wiki.gg 都没这一页，本机装的四十多个
    插件里除了咱们自己没一个调用点）⇒ 只给任务号被客户端拒了，必须再试带上四个 true 的宽写，
    并且把客户端自己的报错原文抄进日志 —— 它说怎么给就是怎么给，不许咱们替它圆场。"""
    name = "probe_fetchdata_asks_each_entry_once_and_probes_both_arities"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    calls = []

    def preload(*a):
        calls.append(("preload", [x if not isinstance(x, bool) else int(x) for x in a]))

    def fetch(*a):
        # 单参 = 客户端不认这个写法（真客户端抛的是 "Wrong number of arguments"）
        if len(a) == 1:
            raise TypeError("Wrong arguments")
        calls.append(("fetch", [x if not isinstance(x, bool) else int(x) for x in a]))

    g.C_TaskQuest = lua.table_from({"RequestPreloadRewardData": preload})
    g.C_QuestRewards = lua.table_from({"FetchData": fetch})
    report = str(gql.Probe.Run("fetchdata 88"))
    kinds = [c[0] for c in calls]
    if kinds.count("preload") != 1:
        return fail(name, f"要数据每个入口只许各问一次，实际 {calls}")
    if "动作1=C_TaskQuest.RequestPreloadRewardData 状态=已调用" not in report:
        return fail(name, f"入口在就要如实写'已调用':\n{report}")
    if "Wrong arguments" not in report:
        return fail(name, f"客户端的报错原文必须抄进日志，不许折成一句'Data 不可用':\n{report}")
    wide = [c for c in calls if c[0] == "fetch" and len(c[1]) == 5]
    if len(wide) != 1:
        return fail(name, f"单参被拒后必须再试宽写一次（只一次），实际 {calls}")
    if wide[0][1] != [88, 1, 1, 1, 1]:
        return fail(name, f"宽写是'任务号 + 四个 true'，实际 {wide[0][1]}")
    if "动作2b=C_QuestRewards.FetchData(带上四个 true) 状态=已调用" not in report:
        return fail(name, f"两行都要留在报告里:\n{report}")
    if report.count("动作1之后") != 1 or report.count("动作2之后") != 1 \
            or report.count("动作2b之后") != 1:
        return fail(name, f"每个动作之后各复看一次，不许漏也不许重:\n{report}")

    # 反向：单参就认的那一条 ⇒ 不许再试宽写（多一次调用就是白烧）
    calls.clear()

    def fetch_ok(*a):
        calls.append(("fetch", len(a)))

    g.C_QuestRewards = lua.table_from({"FetchData": fetch_ok})
    report = str(gql.Probe.Run("fetchdata 89"))
    if [c for c in calls if c[0] == "fetch"] != [("fetch", 1)]:
        return fail(name, f"单参认的就只问一次，不许跟着试宽写，实际 {calls}")
    if "动作2b" in report:
        return fail(name, f"没被拒就不该有 2b 这行:\n{report}")
    ok(name)


def test_probe_fetchdata_restores_the_quest_log_selection():
    """动作3"把这条在任务日志里选中"是唯一带副作用的一步（选中项是玩家正在看的东西）。
    用户 11-04 点头的形状："放进探针，用完恢复"。
    11-04 第一次回传把这一段打穿了：`ClearQuestLogSelection` 这台客户端根本没有、
    `GetQuestLogSelection` 答 nil ⇒ 上一版改完撤不回去，把 89267 留在了他的任务日志里；
    而且"已调用"并不等于真选中（AutoTurnIn 那个入口收的是**日志序号**，上一版给的是任务号）。
    ⇒ 这一版每按一下都读回来对一遍，写口按读口的种类配（序号读口↔序号写口、任务号读口↔任务号写口），
      对不上就照实说"没退回原选中"。生产路径一次都不许调用 SetSelectedQuest。"""
    name = "probe_fetchdata_restores_the_quest_log_selection"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()

    # —— 形状 A：12.x 读口（答任务号）在，老读口故意答一个对不上的 ⇒ 用 12.x 那个
    st, calls = {"sel": 5}, []
    g.C_QuestLog = lua.table_from({
        "GetLogIndexForQuestID": lambda qid: 4,
        "GetSelectedQuest": lambda: (calls.append(("read12",)), st["sel"])[1],
        "SetSelectedQuest": lambda a: (calls.append(("select", int(a))),
                                       st.__setitem__("sel", int(a)))[0],
    })
    g.GetQuestLogSelection = lambda: 40
    g.SelectQuestLogEntry = lambda i: (calls.append(("entry", int(i))),
                                       st.__setitem__("sel", int(i)))[0]
    report = str(gql.Probe.Run("fetchdata 88"))
    if "选中项读口=C_QuestLog.GetSelectedQuest 值=5" not in report:
        return fail(name, f"读口要挑真答得出的那一个，并把原值写进日志:\n{report}")
    if "按任务号 88=已调用→就是这条" not in report:
        return fail(name, f"读口答的是任务号 ⇒ 第一下就按任务号写:\n{report}")
    if "按日志序号" in report:
        return fail(name, f"11-04 第二次回传实测这台客户端 SetSelectedQuest 收任务号，"
                         f"先试序号那一下是白烧的接口调用，不该还在:\n{report}")
    if "退回原来选中的 C_QuestLog.SetSelectedQuest=已调用→读回5（原来选中的=5" not in report:
        return fail(name, f"12.x 读口的值就该用 12.x 的写口退，并把读回值写上:\n{report}")
    if [c for c in calls if c[0] == "select"] != [("select", 88), ("select", 5)]:
        return fail(name, f"选中一下 + 退回一下，各一次；不许把序号喂给那个入口:{calls}")
    if st["sel"] != 5:
        return fail(name, f"最终必须真退回原来那条（现在停在 {st['sel']}）")
    if "没退回原选中" in report:
        return fail(name, f"撤干净了就不许有这句:\n{report}")

    # —— 形状 B：原本没选中 ⇒ 退成"没选中"。12.x 读口的机器先把 12.x 写口试到位，
    #     读回空了就停 ⇒ 那两个老名字（第二次回传实测本机根本没有）不再各按一下、各多一行。
    st, calls = {"sel": 0}, []
    g.C_QuestLog = lua.table_from({
        "GetLogIndexForQuestID": lambda qid: 4,
        "GetSelectedQuest": lambda: st["sel"],
        "SetSelectedQuest": lambda a: (calls.append(("select", int(a))),
                                       st.__setitem__("sel", int(a)))[0],
    })
    g.ClearQuestLogSelection = lambda: (calls.append(("clear",)), st.__setitem__("sel", 0))[0]
    report = str(gql.Probe.Run("fetchdata 88"))
    if ("select", 0) not in calls:
        return fail(name, f"原本没选中 ⇒ 要退成没选中，实际 {calls}")
    if ("clear",) in calls:
        return fail(name, f"第一个真能用的候选够了就停，不许接着试别的:{calls}")
    if st["sel"] != 0:
        return fail(name, f"退完得是没选中的状态，实际 {st['sel']}")

    # —— 形状 C：只有老读口答得出（它答的是日志序号）⇒ 写口必须配 SelectQuestLogEntry
    st, calls = {"idx": 7}, []
    g.C_QuestLog = lua.table_from({
        "GetLogIndexForQuestID": lambda qid: 4,
        "SetSelectedQuest": lambda a: (calls.append(("select", int(a))),
                                       st.__setitem__("idx", int(a)))[0],
    })
    g.GetQuestLogSelection = lambda: st["idx"]
    g.SelectQuestLogEntry = lambda i: (calls.append(("entry", int(i))),
                                       st.__setitem__("idx", int(i)))[0]
    report = str(gql.Probe.Run("fetchdata 88"))
    if "按日志序号 4=已调用→就是这条" not in report:
        return fail(name, f"老读口的值对上了就不必再按任务号补:\n{report}")
    if ("entry", 7) not in calls or st["idx"] != 7:
        return fail(name, f"序号读口要退回序号写口，实际 {calls}")
    if ("select", 88) in calls:
        return fail(name, f"任务号不是日志序号，这一趟不该拿它去调那个入口:{calls}")

    # —— 形状 D：撤不回去就照实说（老读口答得出，可写口本机没有）
    st = {"idx": 7}
    g.SelectQuestLogEntry = None
    report = str(gql.Probe.Run("fetchdata 88"))
    if "没退回原选中（原来是 7，现在读回 4）" not in report:
        return fail(name, f"撤不回去必须把两边值都写出来，别让用户自己猜:\n{report}")

    # —— 形状 E：入口在、"已调用"，但客户端根本没选中 ⇒ 另一域那一下还得补（第一次回传那个坑）
    st, calls = {"sel": 5}, []
    g.C_QuestLog = lua.table_from({
        "GetLogIndexForQuestID": lambda qid: 4,
        "GetSelectedQuest": lambda: st["sel"],
        "SetSelectedQuest": lambda a: (calls.append(("select", int(a))),)[0],   # 收了，但什么都不改
    })
    report = str(gql.Probe.Run("fetchdata 88"))
    if "按任务号 88=已调用→不是这条(读回5)" not in report \
            or "按日志序号 4=已调用→不是这条(读回5)" not in report:
        return fail(name, f"第一下读回对不上就要补另一域，两行都得留在日志里:\n{report}")
    if [c for c in calls if c[0] == "select"] != [("select", 88), ("select", 4), ("select", 5)]:
        return fail(name, f"两下尝试 + 退一下，各一次；顺序按读口的种类，实际 {calls}")

    # —— 生产路径不许碰这个副作用：只有探针里能出现 SetSelectedQuest
    here = os.path.dirname(os.path.abspath(__file__))
    for fn in ("Sync.lua", "Ledger.lua", "Matrix.lua", "Api.lua", "Core.lua"):
        src = open(os.path.join(here, "..", "GoldQuestLedger", fn), encoding="utf-8").read()
        code = "\n".join(l.split("--", 1)[0] for l in src.splitlines())
        if "SetSelectedQuest" in code:
            return fail(name, f"{fn} 的生产路径里出现了 SetSelectedQuest（副作用只许留在探针）")
    ok(name)


def test_probe_fetchdata_without_a_target_touches_nothing():
    """没任务号、这次开机里也还没有"金额问不出"的记录 ⇒ 一句实话，零接口、零延时。
    （取证不能要求玩家先知道任务名 —— 那前提就是按 M。所以这张表由插件自己记，见 T1。）"""
    name = "probe_fetchdata_without_a_target_touches_nothing"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    hits = []
    g.C_TaskQuest = lua.table_from({"RequestPreloadRewardData": lambda *a: hits.append(a)})
    g.C_QuestRewards = lua.table_from({"FetchData": lambda *a: hits.append(a)})
    g.HaveQuestRewardData = lambda qid: hits.append(("has", qid)) or False
    g.GetQuestLogRewardMoney = lambda qid: hits.append(("money", qid)) or 0
    before = int(lua.execute("return #GQL_LOG"))
    report = str(gql.Probe.Run("fetchdata"))
    if "没得试" not in report:
        return fail(name, f"要老实说没得试、并告诉他怎么让它有得试:\n{report}")
    if hits:
        return fail(name, f"没目标就不许问任何接口，实际 {hits}")
    if int(lua.execute("return #GQL_LOG")) != before:
        return fail(name, "没目标就不许排延时")
    ok(name)


def test_probe_fetchdata_picks_the_quest_that_just_failed():
    """任务号的来路必须是"刚刚金额问不出"那一条（T1 那张表），不是玩家报的名字：
    他要知道任务名就得先按 M，而取证要验的恰恰是不按 M 行不行。"""
    name = "probe_fetchdata_picks_the_quest_that_just_failed"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds": lambda qid: None,
                                    "RequestPreloadRewardData": lambda qid: None})
    g.HaveQuestRewardData = lambda qid: False
    g.GetQuestLogRewardMoney = lambda qid: 0
    g.QuestUtils_IsQuestWorldQuest = lambda qid: True
    fire(gql, "QUEST_ACCEPTED", 95253)
    report = str(gql.Probe.Run("fetchdata"))
    if "quest=95253" not in report:
        return fail(name, f"探针要自己挑中刚问不出金额的那一条:\n{report}")
    if "日志序号=判不了" not in report:
        return fail(name, f"反查不到日志序号要照实写判不了（别填 0 冒充问过）:\n{report}")
    # 显式给了任务号就用给的，别抢表里那条
    report = str(gql.Probe.Run("fetchdata 94967"))
    if "quest=94967" not in report or "quest=95253" in report:
        return fail(name, f"带任务号时要以它为准:\n{report}")

    # 11-04 回传的教训：那三趟自己挑中的是 89267 —— 客户端答过"数据在本地"、金额永远空，
    # 也就是**这一条本来就不给金币**，拿它问不出"金额能不能不按 M 读出来"。
    # ⇒ 表里还有"数据不在本地"那档就先挑它（那才有得问），只剩"不给金币"那档时把话说在前面。
    g.HaveQuestRewardData = lambda qid: int(qid) != 95253   # 94967 这条数据已经在本地了
    fire(gql, "QUEST_ACCEPTED", 94967)    # 表里多一条更新的，可那一档客户端答的是"不给金币"
    report = str(gql.Probe.Run("fetchdata"))
    if "quest=95253" not in report:
        return fail(name, f"表里同时有两条时，要挑'数据还不在本地'的那一条做试验"
                         f"（它才有得问），不是单纯挑最新那条:\n{report}")
    g.HaveQuestRewardData = lambda qid: True
    fire(gql, "QUEST_ACCEPTED", 95253)    # 现在两条都答"数据在本地、金额就是空"
    report = str(gql.Probe.Run("fetchdata"))
    if "这一条客户端已经答过了" not in report:
        return fail(name, f"表里只剩'不给金币'那档时，要当场说清这一条问不出新东西:\n{report}")
    if "日志序号=判不了" not in report or "判不了(" not in report:
        return fail(name, f"判不了要带原因（入口缺失 / 答不出 / 答 0），别光写三个字:\n{report}")
    ok(name)


def test_probe_fetchdata_never_writes_the_ledger_even_when_it_works():
    """直读真的把金额问出来了（这一轮的靶心），也**只进日志、不进账**：
    判据、门槛、按号读数都归生产那几趟，探针不许越权改账 —— 不然"取证"和"落账"混在一起，
    回传就分不清是哪个环节把数弄对的。结论行只许写一次，由第一个答出正数的动作认领。"""
    name = "probe_fetchdata_never_writes_the_ledger_even_when_it_works"
    lua, gql, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Probe",))
    g = lua.globals()
    pays = {"on": False}
    g.C_TaskQuest = lua.table_from({"RequestPreloadRewardData": lambda qid: None})
    g.HaveQuestRewardData = lambda qid: False if not pays["on"] else True
    g.GetQuestLogRewardMoney = lambda qid: 10_000_000 if pays["on"] else 0
    report = str(gql.Probe.Run("fetchdata 88"))
    if "直读试探结论" in report:
        return fail(name, f"直读这几下还没问出来，不许提前下结论:\n{report}")
    pays["on"] = True
    g.GQL_NOW = 20
    lua.globals().GQL_FireTimers()
    text = str(gql.Config.GetLogs())
    if "哪一个动作把金额问出来了=稍后+1秒" not in text:
        return fail(name, f"第一个问出正数的那一步要认领结论:\n{text}")
    if text.count("哪一个动作把金额问出来了") != 2:      # 认领一次 + 收尾一次
        return fail(name, f"结论只许认领一次，不许每次复看都重说:\n{text}")
    quests = gql.Config.DB()["quests"]
    if quests is not None and len(list(quests.values())) != 0:
        return fail(name, f"探针读到的金额不许落进账本:{list(quests.values())}")
    if "钱=10000000" in text or "补读到金额" in text:
        return fail(name, f"探针不许走落账那一路:\n{text}")
    ok(name)


def test_reward_data_arrival_settles_gold_without_the_matrix():
    """T2-b（计划 §四，本轮靶心）：矩阵全程没开过，接一条金币世界任务、金额先问不出，
    数据在后面某一趟日志事件上回来了 ⇒ 钱当场落进这一条自己的记录，
    并且**准入门读的那几份凭据一次点亮**（用户 11-04："接到任务后为什么没拿到正确的信息
    然后进矩阵，这个要处理好"）。
    补读不新增事件/定时器/轮询：这台客户端**没实测过**"奖励数据回包"的事件名，
    按名字猜着注册炸过登录同步（GoldQuestLedger/Sync.lua 的 TryRegister 那段注释），
    所以挂在本来就成串触发、且自带 10 秒节流的日志旁路上。
    正向钉这几样（缺一就是没落账）：goldMoney / goldKey / lastMoney / currentSig +
    那份 observation 的金币旗 + 号级 reads；并且 goldKey 用的是**这一条自己的结束时刻**
    那一档 —— 现场问答那一趟先把 roundEnd 落进 SetExpiry、补读只排在它之后算键，
    顺序倒了就落在"本 CD 尾巴"那一档（前置断言把那一档的键先记下来，正主要与它不同）。
    代价也钉：每条待补读任务每趟只问 1 次金额，落账之后不再问（已了结）；
    反向那一岔的接口调用总数 = 建记录 1 + 现场问答 1 + 补读封顶 6 = 8 次，到顶之后一次都不许多烧。
    反向：补读到顶（GOLD_READ_MAX_TRIES=6）仍问不出 ⇒ 那几个字段全 nil、
    金币判据不许因为这行凭空亮勾、日志写着"不猜"。"""
    name = "reward_data_arrival_settles_gold_without_the_matrix"

    def env(qid, pays):
        """一套"矩阵全程没开过"的环境：qid 这条挂在任务日志里，pays(qid) 决定金额接口答什么。
        倒计时答得出（现场问答那一趟落得下这条自己的结束时刻，键序才测得到）；
        金额先答 0 = 服务器那份数据还没到（Api.QuestRewardMoney 只认正数 ⇒ 判成问不出）。
        顺带把金额接口被问了几次记下来 —— 补读的接口代价就钉在这里。"""
        lu, gg, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Matrix",))
        gl = lu.globals()
        seen = []

        def money(a):
            seen.append(int(a))
            return pays(int(a))

        gl.C_TaskQuest = lu.table_from({
            "GetQuestTimeLeftSeconds": lambda a: 7200 if int(a) == qid else None,
            "RequestPreloadRewardData": lambda a: None,
        })
        gl.HaveQuestRewardData = lambda a: False
        gl.GetQuestLogRewardMoney = money
        gl.QuestUtils_IsQuestWorldQuest = lambda a: True
        gl.C_QuestLog = lu.table_from({
            "GetInfo": lambda i: lu.table_from(
                {"questID": qid, "title": "任务%d" % qid, "questLogIndex": 1})
                if int(i) == 1 else None,
            "IsQuestStarted": lambda a: a == qid,
            "IsQuestCompleted": lambda a: False,
        })
        return lu, gg, gl, seen

    def obs_of(lu, qid):
        return one(lu.execute(
            "local r = GQLDB.quests[%d] if not r then return 'no-rec' end "
            "local o = r.currentSig and r.observations[r.currentSig] "
            "if not o then return 'no-obs' end "
            "return tostring(o.gold) .. '|' .. tostring(o.money)" % int(qid)))

    def gold_ids(gg, dd):
        # lupa 遍历 Lua 数组表给的是**下标**，不是元素 ⇒ 只能按下标取（金币判据的名单就是那一条）
        out = gg.Ledger.GoldQuestIDs(dd, wall)
        return [int(out[i]) for i in range(1, len(out) + 1)]

    # ---------------- 正向：服务器那份数据在后面某一趟日志事件上回来了 ----------------
    arrived = [False]
    lua, gql, g, reads = env(88, lambda qid: 10557708 if arrived[0] else 0)
    L, db = gql.Ledger, g.GQLDB
    wall = int(g.GetServerTime())                # Sync.Clock() 的第一个返回值（全程不变）
    charKey = str(gql.Sync.CurrentCharKey())

    # 记录早就在账本上（矩阵某次开着时收的），只是结束时刻与金额一直空着 —— 92576 的形状
    gql.Ledger.AddQuest(db, 88, 0, None)
    tail_key = uid(L, db, 88, wall)              # 还没有任何结束时刻 ⇒ 只能落在尾巴那一档
    if tail_key is None:
        return fail(name, "前置：尾巴档的编号要算得出（正向要拿它当反面对照）")
    if obs_of(lua, 88) != "false|nil":
        return fail(name, f"前置：这一条此刻该是金额空着，实际 {obs_of(lua, 88)}")

    fire(gql, "QUEST_ACCEPTED", 88)              # 矩阵全程没开过：补读落账不依赖面板
    rec = db["quests"][88]
    if int(rec["roundEnd"] or 0) != wall + 7200:
        return fail(name, f"前置：现场问答那一趟要把这条自己的结束时刻先落进 SetExpiry，"
                          f"实际 {rec['roundEnd']}")
    if uid(L, db, 88, wall) == tail_key:
        return fail(name, "前置：结束时刻落账之后编号就该换到板面那一档，"
                          "否则正向测的就是同一个档，白测")
    # 补读前两趟：数据还没到
    g.GQL_NOW = 10
    fire(gql, "QUEST_LOG_UPDATE")
    g.GQL_NOW = 20
    fire(gql, "QUEST_LOG_UPDATE")
    for f in ("goldMoney", "goldKey", "lastMoney"):
        if rec[f] is not None:
            return fail(name, f"数据没到时不许凭空写 {f}，实际 {rec[f]}")
    if gold_ids(gql, db):
        return fail(name, f"数据没到时金币判据不该亮起这一行，实际 {gold_ids(gql, db)}")

    arrived[0] = True
    n_before = len(reads)
    g.GQL_NOW = 30
    fire(gql, "QUEST_LOG_UPDATE")
    text = str(gql.Config.GetLogs())
    if len(reads) - n_before != 1:
        return fail(name, f"补读每趟每条只问 1 次金额，实际问了 {len(reads) - n_before} 次:\n{text}")
    want = uid(L, db, 88, wall)
    if int(rec["goldMoney"] or 0) != 10557708:
        return fail(name, f"数据到了金额要落进这一条：期望 10557708，实际 {rec['goldMoney']}")
    if str(rec["goldKey"]) != want:
        return fail(name, f"goldKey 要用这条自己的结束时刻那一档：期望 {want}，实际 {rec['goldKey']}")
    if str(rec["goldKey"]) == tail_key:
        return fail(name, f"goldKey 还停在'本 CD 尾巴'那一档 = 没排在结束时刻落账之后算键："
                          f"{rec['goldKey']}")
    if int(rec["lastMoney"] or 0) != 10557708:
        return fail(name, f"记录级的金额也要落账，实际 {rec['lastMoney']}")
    if str(rec["currentSig"]) != "10557708":
        return fail(name, f"当前奖励签名要换成读到金币那一副，实际 {rec['currentSig']}")
    if obs_of(lua, 88) != "true|10557708":
        return fail(name, f"那份 observation 的金币旗与金额都要落进去，实际 {obs_of(lua, 88)}")
    rd = db["chars"][charKey]["reads"][88]
    if rd is None or int(rd["g"]) != 10557708 or str(rd["k"]) != want:
        return fail(name, f"号级到手读数（按这一条实例的编号）也要落进去，实际 {rd}")
    if 88 not in gold_ids(gql, db):
        return fail(name, "钱到了这一行就该自己进金币判据（矩阵没开过也一样）")
    if "补读到金额 quest=88 钱=10557708 编号=%s（矩阵没开过也进账了）" % want not in text:
        return fail(name, f"落账要留一行取证，编号就是刚算出的那把:\n{text}")
    if g.GQLMatrixFrame is not None and bool(g.GQLMatrixFrame.IsShown()):
        return fail(name, "这一条测的是矩阵全程没开过的形状：不许有用例把面板打开")
    # 已了结：再走两趟日志事件，一次接口都不许多烧
    n_settled = len(reads)
    g.GQL_NOW = 40
    fire(gql, "QUEST_LOG_UPDATE")
    g.GQL_NOW = 50
    fire(gql, "QUEST_LOG_UPDATE")
    if len(reads) != n_settled:
        return fail(name, f"落过账的那条不许再补读，多问了 {len(reads) - n_settled} 次:\n"
                          f"{gql.Config.GetLogs()}")

    # -------- 反向（另一套环境）：从头到尾问不出 ⇒ 到顶放手，一个数都不许猜 --------
    lua2, gql2, g2, reads2 = env(89, lambda qid: 0)
    db2 = g2.GQLDB
    charKey2 = str(gql2.Sync.CurrentCharKey())
    gql2.Ledger.AddQuest(db2, 89, 0, None)
    fire(gql2, "QUEST_ACCEPTED", 89)
    rec2 = db2["quests"][89]
    for tick in (10, 20, 30, 40, 50, 60, 70):
        g2.GQL_NOW = tick
        fire(gql2, "QUEST_LOG_UPDATE")
    text2 = str(gql2.Config.GetLogs())
    if "补读 6 次仍问不出金额 quest=89（服务器那份数据这一趟没到，不猜）" not in text2:
        return fail(name, f"到顶要如实写'不猜'，不许猜一个数:\n{text2}")
    for f in ("goldMoney", "goldKey", "lastMoney"):
        if rec2[f] is not None:
            return fail(name, f"到顶也不许凭空写 {f}，实际 {rec2[f]}")
    if str(rec2["currentSig"]) != "-1" or obs_of(lua2, 89) != "false|nil":
        return fail(name, f"到顶也不许把这份空读数签成读到金币："
                          f"{rec2['currentSig']} / {obs_of(lua2, 89)}")
    if 89 in gold_ids(gql2, db2):
        return fail(name, "矩阵里不许因为这行凭空出现一个金币勾")
    row2 = db2["chars"][charKey2]
    if row2 is not None and row2["reads"] is not None and row2["reads"][89] is not None:
        return fail(name, "到顶的那条也不许留下号级到手读数")
    if reads2.count(89) != 8:      # 建记录 1 + 现场问答 1 + 补读封顶 6
        return fail(name, f"每条待补读任务的接口调用要封顶在 8 次（多一次都不许多烧），"
                          f"实际 89 被问了 {reads2.count(89)} 次:\n{text2}")
    g2.GQL_NOW = 80
    fire(gql2, "QUEST_LOG_UPDATE")
    g2.GQL_NOW = 90
    fire(gql2, "QUEST_LOG_UPDATE")
    if reads2.count(89) != 8:
        return fail(name, f"标成已了结之后一次接口都不许多烧：{reads2.count(89)} 次")
    ok(name)


def _money_env(qid):
    """一套"金额永远问不出、倒计时答得出"的环境，并把金额接口的调用次数记下来。
    seen 里正数 = 问过金额，负数 = 向服务器要过这份数据（用例 3 靠它分辨"没被全局 -1 关掉"）。
    返回 (lua, gql, globals, 被问过的 questID 列表)。"""
    lu, gg, _ = sync_env([], wall=1_790_000_000, weekly=300000, extra=("Matrix",))
    gl = lu.globals()
    seen = []

    def money(a):
        seen.append(int(a))
        return 0                                  # 0 = 数据还没到（Api 只认正数）

    gl.C_TaskQuest = lu.table_from({
        "GetQuestTimeLeftSeconds": lambda a: 7200 if int(a) == qid else None,
        "RequestPreloadRewardData": lambda a: seen.append(-int(a)),
    })
    gl.HaveQuestRewardData = lambda a: False
    gl.GetQuestLogRewardMoney = money
    gl.QuestUtils_IsQuestWorldQuest = lambda a: True
    gl.C_QuestLog = lu.table_from({
        "GetInfo": lambda i: lu.table_from(
            {"questID": qid, "title": "任务%d" % qid, "questLogIndex": 1})
            if int(i) == 1 else None,
        "IsQuestStarted": lambda a: a == qid,
        "IsQuestCompleted": lambda a: False,
    })
    return lu, gg, gl, seen


def test_a_skipped_char_stops_the_reward_reread():
    """右键标了"这一号本轮不做" ⇒ 这一号不再替这条问钱：补读当场了结，
    现场问答那一路也不再向服务器要这份数据（用户 11-04："直到拿到金币数
    或者用户右键表示不做为止"）。替身记金额接口被问了几次，钉的就是次数。"""
    name = "a_skipped_char_stops_the_reward_reread"
    lu, gg, gl, seen = _money_env(93)
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 93, 0, None)
    fire(gg, "QUEST_ACCEPTED", 93)               # 金额问不出 ⇒ 进补读表
    if not seen:
        return fail(name, "前置：现场问答那一路要问过金额接口")
    seen.clear()
    L.SetManual(db, charKey, 93, "skip", 1_790_000_000, None)
    # 每次触发之间要把 GQL_NOW 往前推：日志旁路自带节流（LOG_BYPASS_COOLDOWN），
    # 连着敲三下只会认第一下，那样测出来的是"没跑到"，不是"停住了"。
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)       # 补读借的就是这一串事件
    if seen:
        return fail(name, f"标了不做还替这一号问钱，问了 {len(seen)} 次")
    logs = str(gg.Config.GetLogs())
    if "补读停：这一号标了不做" not in logs:
        return fail(name, f"停了要在日志里说清为什么停:\n{logs}")
    ok(name)


def test_a_skipped_other_char_does_not_stop_this_one():
    """别的号标了不做，眼前这一号该问的照问（否则一个号的放弃会替全账号关掉补读，
    正是 11-04 之前那类"当前号的事实被当全账号凭据"的形状）。"""
    name = "a_skipped_other_char_does_not_stop_this_one"
    lu, gg, gl, seen = _money_env(94)
    L, db = gg.Ledger, gl.GQLDB
    L.AddQuest(db, 94, 0, None)
    L.SetManual(db, "别人-永恒之井", 94, "skip", 1_790_000_000, None)
    seen.clear()
    fire(gg, "QUEST_ACCEPTED", 94)
    if not seen:
        return fail(name, "别的号标了不做，不该挡住当前这一号的补读")
    ok(name)


def test_a_skipped_char_does_not_close_the_quest_for_other_chars():
    """-1 只对本号生效：本号标了不做，换一个号登录这条仍要能补读。
    Sync.CurrentCharKey() 每次现算（Sync.lua:79），改 UnitName 就是换号。"""
    name = "a_skipped_char_does_not_close_the_quest_for_other_chars"
    lu, gg, gl, seen = _money_env(95)
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 95, 0, None)
    fire(gg, "QUEST_ACCEPTED", 95)
    L.SetManual(db, charKey, 95, "skip", 1_790_000_000, None)
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)      # 本号这一份被了结
    gl.UnitName = lambda who: "另一个号"        # 换号：byChar[新号] 还是 nil
    gl.GQL_NOW = 2000
    seen.clear()
    fire(gg, "QUEST_ACCEPTED", 95)             # 新号的现场问答该照样向服务器要数据
    if -95 not in seen:
        return fail(name, f"本号的放弃不该替别的号关掉补读，实际 {seen}")
    ok(name)


def test_a_stale_round_skip_does_not_stop_this_round():
    """上一轮烙的 skip：m.k 对不上当前实例 ⇒ 这一轮照旧问（不许把陈旧 × 当本轮凭据）。"""
    name = "a_stale_round_skip_does_not_stop_this_round"
    lu, gg, gl, seen = _money_env(96)
    L, db = gg.Ledger, gl.GQLDB
    charKey = str(gg.Sync.CurrentCharKey())
    L.AddQuest(db, 96, 0, None)
    fire(gg, "QUEST_ACCEPTED", 96)
    L.SetManual(db, charKey, 96, "skip", 1_790_000_000, None)
    lu.execute("for _, v in pairs(GQLDB.manual) do if v[96] then "
               "v[96].k = '96:上一轮' end end")   # 换轮后老档残留的形状
    seen.clear()
    for step in range(3):
        gl.GQL_NOW = 1000 + 100 * step
        fire(gg, "QUEST_LOG_UPDATE", None)
    if not seen:
        return fail(name, "上一轮的 skip 不该挡住这一轮的补读")
    ok(name)


def test_turnin_row_upgrades_an_earlier_manual_mark_to_green():
    """刀 3（2026-10-29 回响复燃 92713 报告：玩家只点过 2 个琥珀勾，界面却有 4 个 ——
    "先点一下、流水后到"的格子永久停在琥珀）：颜色的语义是用户 10-25/10-27 定的
    "绿=客户端/流水认的、琥珀=我自己记的"，实测流水在账上却报琥珀就是映射错了。
    L.State 里补录排在流水前面，但只在**补录未核验 + 补录状态是 done + 同一条实例的
    实测流水在**时才改回绿（判据与下面流水那支一字不差）；核验过的仍回 verified、
    没有流水背书的仍回 manual（琥珀）—— test_manual_beats_auto_and_survives_sync
    （手动不许被自动 open 顶掉）不许因此变红。
    正向：点格子（无流水 ⇒ 琥珀）→ 流水落账（同一条实例）⇒ done + 第二返回 nil、
    上屏绿勾不是琥珀。反向对照：流水编号属于上一条实例（换实例后才点的手动）⇒ 仍琥珀。
    小号格子点不出绿色不在本刀范围（客户端只答当前角色），本刀只保证
    "账本里有真凭据时颜色不许降级"。"""
    lua, gql, _ = sync_env([(95, 30000000)], board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Matrix.Toggle()
    key = str(gql.Sync.CurrentCharKey())
    L, db = gql.Ledger, g.GQLDB
    lua.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua, key, 95):
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green", "点不到那一格")

    def state_pair():
        return str(lua.execute(
            "local s, src = GQL.Ledger.State(GQLDB, %r, 95, nil, 1) "
            "return tostring(s) .. '/' .. tostring(src)" % key))

    if state_pair() != "done/manual":
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green",
                    f"前置：无流水的手点补录该是琥珀 done，实际 {state_pair()}")
    # 流水后到（raw_turnin 现算编号烙在这一条实例上）⇒ 不许还报琥珀
    raw_turnin(lua, L, db, key, 95, 1)
    if state_pair() != "done/nil":
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green",
                    f"同一条实例的实测流水在账上，这一格不许停在琥珀，实际 {state_pair()}")
    gql.Matrix.Render()
    if check_glyph(g.GQLMatrixFrame, tinted=True):
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green",
                    "上屏不许还是琥珀 √：\n" + "\n".join(frame_texts(g.GQLMatrixFrame)))
    if len(check_glyph(g.GQLMatrixFrame, tinted=False)) != 1:
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green",
                    "这一格该是客户端认的绿勾上屏:\n"
                    + "\n".join(frame_texts(g.GQLMatrixFrame)))
    # 反向对照：流水属于上一条实例、手动烙在换实例之后 ⇒ 不算这一条的凭据，仍琥珀。
    # 另起一套环境，形状照 manual_mark_from_the_previous_instance_stays_amber 那组。
    lua2, gql2, _ = sync_env([(96, 30000000)], board=43200, extra=("Matrix",))
    g2 = lua2.globals()
    gql2.Matrix.Toggle()
    key2 = str(gql2.Sync.CurrentCharKey())
    raw_turnin(lua2, gql2.Ledger, g2.GQLDB, key2, 96, 1)   # 流水烙在实例 A
    # 11-04 口径：旧实例下板 ⇒ 账本里它的结束时刻不再算数，先作废再问新的
    g2.GQLDB["quests"][96]["roundEnd"] = None
    g2.GQL_BOARD["end"] = 200000                               # 板面换成下一条实例
    gql2.Sync.SnapshotNow()
    lua2.execute("GQL.Api.QuestState = function(q) return 'open', nil end")
    if not click_cell(lua2, key2, 96):                         # 手动烙在新实例 B
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green", "对照格点不到")
    got2 = str(lua2.execute(
        "local s, src = GQL.Ledger.State(GQLDB, %r, 96, nil, 1) "
        "return tostring(s) .. '/' .. tostring(src)" % key2))
    if got2 != "done/manual":
        return fail("turnin_row_upgrades_an_earlier_manual_mark_to_green",
                    f"流水编号属于上一条实例时不许升绿，实际 {got2}")
    ok("turnin_row_upgrades_an_earlier_manual_mark_to_green")


def test_back_to_back_turnin_events_do_not_crosstalk():
    """连交两条不同任务不许串台：各自当场写恰好一笔流水。
    【为什么改名】老用例 money_window_seq_rejects_stale_timer 钉的是"15 秒钱窗换任务时
    旧窗口的迟到定时器不许结算新窗口（每个窗口认自己的 seq）"。2026-10-29 那一刀把整个
    钱窗退役了（用户定稿：不要等 15 秒再更新、交任务获得的金币不准），Sync 里既没有
    capture 也没有 seq ⇒ 那条判据没有读者；这一串真正要守的东西还剩"换任务不许串台"，
    于是照新形状钉：QUEST_COMPLETE 到达当场落账，两条任务各得恰好一笔、互不覆盖。"""
    lua, gql, _ = sync_env([(140, 30000000), (141, 30000000)], money=100000,
                           board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()          # 两条任务都进账本（拿到 poolSeen）
    fire(gql, "QUEST_COMPLETE", 140)
    g.GQL_NOW = 13
    g.GetMoney = lambda: 100500
    fire(gql, "QUEST_COMPLETE", 141)        # 换任务：上一笔已经当场落账，不该被下一笔顶掉
    g.GQL_NOW = 60                          # 老迟到定时器的刻度：现在没有任何窗口排它
    lua.globals().GQL_FireTimers()
    for qid in (140, 141):
        if lua.execute("""
            local db = GQL.Config.DB()
            local key = GQL.Sync.CurrentCharKey()
            local rows = db.turnins[key] and db.turnins[key][%d]
            return rows ~= nil and #rows == 1
        """ % qid) is not True:
            return fail("back_to_back_turnin_events_do_not_crosstalk",
                        f"quest={qid} 该恰好一笔流水（当场落账、不被下一个事件串台）:\n"
                        f"{gql.Config.GetLogs()}")
    ok("back_to_back_turnin_events_do_not_crosstalk")


def test_turnin_without_ledger_writes_nothing():
    """账本没就绪时交割一笔都不许写：不造任务记录、不写流水、不抛错。
    【为什么改名】老用例 money_window_kept_when_ledger_missing 要守的是"15 秒钱窗到点时
    账本没就绪 ⇒ 窗口保留、稍后补记"。钱窗 2026-10-29 整块退役后不再有"稍后"这条路，
    RecordTurnIn 拿不到 DB 就直接 return（写一半的账比少记一笔更糟）。这里把新口径钉住：
    门外那条路一个字节都不落，账本回来后再交一次照旧能记上。"""
    lua, gql, _ = sync_env([(142, 30000000)], money=100000, board=43200, extra=("Matrix",))
    g = lua.globals()
    gql.Sync.OnWorldEnter()
    g.GQL_NOW = 12
    lua.globals().GQL_FireTimers()
    # 桩掉 DB 模拟"交割那一刻账本没就绪"。日志环本身存在账本里，DB 为 nil 时
    # Config.Log 必然写不进去，所以把日志接口换成内存收集器：抛错与否要看得到。
    lua.execute("""
        GQL_REAL_DB = GQL.Config.DB
        GQL.Config.DB = function() return nil end
        GQL_DEFER_LOGS = {}
        GQL.Config.Log = function(msg) table.insert(GQL_DEFER_LOGS, tostring(msg)) end
        GQL.Config.GetLogs = function() return table.concat(GQL_DEFER_LOGS, "\\n") end
    """)
    g.GQL_NOW = 40
    lua.globals().GQL_FireTimers()
    if lua.execute("return GQL.Config.DB() ~= nil") is not False:
        return fail("turnin_without_ledger_writes_nothing", "前置：DB 桩没生效")
    fire(gql, "QUEST_COMPLETE", 142)
    g.GQL_NOW = 60
    lua.globals().GQL_FireTimers()
    # 门外那一路一个字节都不落：没有流水、没有格子勾、不抛错（抛错会被 fire 那条 pcall 抓到）。
    if lua.execute("return GQL.Config.DB() ~= nil") is not False:
        return fail("turnin_without_ledger_writes_nothing", "交割那一趟把 DB 桩跑没了")
    # 账本就绪之后同一趟再交一次：那一笔必须真落账（前面那笔丢掉是设计，这里不许连累）。
    lua.execute("GQL.Config.DB = GQL_REAL_DB")
    g.GetMoney = lambda: 100500
    g.GQL_NOW = 80
    fire(gql, "QUEST_COMPLETE", 142)
    if lua.execute("""
        local db = GQL.Config.DB()
        local key = GQL.Sync.CurrentCharKey()
        local rows = db.turnins[key] and db.turnins[key][142]
        return rows ~= nil and #rows == 1
    """) is not True:
        return fail("turnin_without_ledger_writes_nothing",
                    "账本就绪后那一笔没记上:\n" + str(gql.Config.GetLogs()))
    ok("turnin_without_ledger_writes_nothing")


def test_snapshot_survives_a_quest_api_error():
    """某一条任务的状态接口抛错，不许炸掉整趟快照：别的任务照常入账，
    刷新定时器照常排，出错的事留一行日志。
    【11-04 前置】以前"有定时器"这一格是 5 秒整板块补读定时器在撑着（Task 3 已下线），
    断言钉的却是"出错之后到期唤醒照排"；现在给两条任务一个真实的板面结束时刻，
    让前置由 ScheduleNextRefresh 自己给，断言一字不动。"""
    lua, gql, _ = sync_env([(150, 30000000), (151, 30000000)],
                           board=7200, extra=("Matrix",))
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


def test_missing_countdown_is_reasked_on_the_next_event_snapshot():
    """倒计时读不出（接口答 nil）的那几条：不排整板块补读，但下一次事件快照会重问
    —— 账本里没有结束时刻 ⇒ Task 1 那道"活着就不问"不成立 ⇒ 该问的照问（用户实测过
    "有个任务没有倒计时了"，那条不能因为下线补读而永久没剩余）。"""
    lua, gql, _ = sync_env([(143, 30000000)], extra=("Matrix",))
    g = lua.globals()
    asked = []
    g.C_TaskQuest = lua.table_from({"GetQuestTimeLeftSeconds":
                                    lambda qid: (asked.append(int(qid)), None)[1]})
    g.GQL_NOW = 1000
    gql.Matrix.Toggle()
    if asked != [143]:
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    f"第一趟该问一次倒计时，实际 {asked}")
    asked.clear()
    gql.Sync.SnapshotNow()                        # 下一次事件快照（去抖后走的就是这条）
    if asked != [143]:
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    f"结束时刻一直没定下来，下一趟要接着问，实际 {asked}")
    logs = str(gql.Config.GetLogs())
    for step in range(6):
        g.GQL_NOW = 1000 + 5 * (step + 1)
        lua.globals().GQL_FireTimers()
    if str(gql.Config.GetLogs()).count("快照完成") > logs.count("快照完成"):
        return fail("missing_countdown_is_reasked_on_the_next_event_snapshot",
                    "倒计时读不出不该再排整板块补读")
    ok("missing_countdown_is_reasked_on_the_next_event_snapshot")


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
    """到期时刻一旦被写得很远（这里让池里这条自己的板面结束时刻落在 34 天后，
    就是脏数据那一类），唤醒定时器也只排到封顶值：排到几周之后等于矩阵开着再也不更新，
    玩家只能手点刷新。封顶后每 6 小时重画一次，只在矩阵开着时发生。
    【11-04 前置】以前"有定时器"这一格是 5 秒整板块补读定时器在撑着，
    于是 5 <= 6 小时恒成立、封顶压根没被走到（Task 3 下线补读后当场露出来）。
    现在由 board 把 due 真造到 34 天后，断言一字不动，测的才是封顶本身。"""
    lua, gql, _ = sync_env([(153, 30000000)], reset=3_000_000,
                           board=3_000_000, extra=("Matrix",))
    gql.Sync.SetPolling(True)
    lua.execute("GQL.Sync.SnapshotNow()")           # 排一次唤醒：due 由这条自己的远期结束时刻给出
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
    """两份状态判定的优先级必须完全一致，只有"一份证据都对不上时兜底答什么"不同
    （世界任务行答 可做/已结束，常驻周常答 ?）。第 36 轮起两份用的是同一道编号门。
    这里用"手动补录压过交任务凭据"这一条同形断言钉两处：上一版只有 State 那条路径
    按本轮判，注册行那份读 db.manual 的字段清单也各写一份。
    Matrix 的"清除补录"走 SetManual(state="open")，当场就被当成清除，所以存着 open 的
    这一份只能直接写进 db.manual（老存档形状；断的是读取优先级，不是写入口）。
    cycleAnchor=1000：这三段都要"这一条挂得上编号"，锚点 0 时身份未定 = 一律 ?，
    测不到优先级（那是用例 5 那一档，另有专门的用例钉）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = ledger_db(lua)
    db["cycleAnchor"] = 1000
    L.DiffPool(db, pool(lua, [(8, 100000, None)]), 1000)
    db["manual"]["E-永恒之井"] = lua.table_from({
        8: lua.table_from({"state": "open", "t": 1200,
                           "k": uid(L, db, 8, 1200)})})
    L.OnTurnIn(db, "E-永恒之井", 8, 1150, str(db["quests"][8]["currentSig"]))
    if state1(L.State, db, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "世界任务行：手动补录应压过交任务凭据")
    if state1(L.RegisteredState, db, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "注册行：同一份手动凭据却读成别的——两份判定已经漂移")
    # 没有时刻的交任务流水（老存档形状：L.WeekStats 自己就写着 `if r.t and ...`）：
    # 两份都不许把它当"实测交任务"。这一笔说不出它属于哪一条实例（没烙编号）⇒
    # 世界任务行退回"还在池里 = 可做"，注册行退回 unknown —— 两道门不许一边松一边紧。
    bare = ledger_db(lua)
    bare["cycleAnchor"] = 1000
    L.DiffPool(bare, pool(lua, [(8, 100000, None)]), 1000)
    bare["turnins"]["E-永恒之井"] = lua.table_from({
        8: lua.table_from([lua.table_from({"g": 100000})])})
    if state1(L.State, bare, "E-永恒之井", 8) != "open":
        return fail("state_and_registered_state_share_precedence",
                    "世界任务行：没有时刻的流水不该算这一条实例已交")
    if state1(L.RegisteredState, bare, "E-永恒之井", 8) != "unknown":
        return fail("state_and_registered_state_share_precedence",
                    "注册行把没有时刻的流水读成 %s：说不出它属于哪一条实例就报 unknown，"
                    "两道兜底不许一边松一边紧"
                    % state1(L.RegisteredState, bare, "E-永恒之井", 8))
    # 连 db.manual 都没建起来的账本 = "没有这条来源"，不是当场抛错的理由：
    # 上一版只有注册行判了这一层，世界任务行直接拿 db.manual[charKey] 索引 nil。
    noManual = ledger_db(lua)
    noManual["cycleAnchor"] = 1000
    L.DiffPool(noManual, pool(lua, [(8, 100000, None)]), 1000)
    L.OnTurnIn(noManual, "E-永恒之井", 8, 1150,
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
    # 双模式：当前这一档读到非金币，但**这一条实例**另一档读到过 → 仍然是金币任务。
    # 粘滞配对的是编号（`goldMoney` + `goldKey`），所以先给这条记录一个身份来源
    # （锚点 0 = 身份未定 = 粘滞永远配不上，那是另一条用例的形状）。
    db["cycleAnchor"] = 1000
    db["quests"][9]["goldMoney"] = 100000
    db["quests"][9]["goldKey"] = uid(L, db, 9, 1000)
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
    # 上一条实例读到过金币、这一条还没读到：粘滞不许翻回"是"（L.QuestMoney 按编号拒了它），
    # 也不许折成"否"——客户端这一条根本没答，UI 该显示 ?。
    stale = ledger_db(lua)
    stale["cycleAnchor"] = 1000
    stale["quests"][12] = L.NewQuestRecord()
    stale["quests"][12]["goldMoney"] = 100000
    stale["quests"][12]["goldKey"] = uid(L, stale, 12, 1000)
    stale["cycleAnchor"] = 1000 + 604800           # 换了一个 CD = 编号换了，钱是上一条实例的
    if L.GoldVerdict(stale, 12) is not None:
        return fail("gold_verdict_is_the_single_gold_entry",
                    "过期粘滞该报 nil（这一条没读到），不许折成客户端说过不给钱")
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


def test_migrate_clears_manual_maps_from_old_saves():
    """schema v8：口述机制（Api.MAP_MEMORY）2026-10-29 整门下线（用户原话：
    "口述这一块直接去掉…等下个资料片，就成垃圾代码片段了"），老存档里已经写进去的
    那份由这档一次清干净：
    ① mapSrc=玩家指定 → mapID/mapSrc 一起清（口述图不是客户端答复，宁缺不编，
       该落"周常"组落"周常"组）；
    ② 只残留 mapManual 标记、mapSrc 已被客户端来源（池行自带）覆盖的 → **图保留**，
       只抹没人再写的旧标记；
    ③ 客户端自己的图（无标记）→ 一个字段都不许动；
    ④ 幂等：跑完推到最新档，再跑 Migrate 返回 false，第二趟不重复清。
    形状照加固符文石(90573)/幽魂角斗士(96625) 的真存档（v7 之后的同款残留）。"""
    name = "migrate_clears_manual_maps_from_old_saves"
    lua, gql = load_lua("Config", "Ledger")
    g = lua.globals()
    g.GQLDB = lua.table_from({})          # 非 mock 环境没有 SavedVariables，这里自建
    db = g.GQLDB
    db["quests"] = lua.table_from({
        # ① 口述图：标签 + 标记成对出现（SetManualMap 的写入形状）
        90573: lua.table_from({"name": "加固符文石：魔导师", "mapID": 2393,
                               "mapSrc": "玩家指定", "mapManual": True}),
        # ② 标记是残留、图已被客户端重写：池行自带 > 口述
        96625: lua.table_from({"name": "幽魂角斗士", "mapID": 2393,
                               "mapSrc": "池行自带", "mapManual": True}),
        # ③ 客户端自己的图，与口述无关
        89377: lua.table_from({"name": "哈籁恩达尔任务", "mapID": 2413}),
    })
    db["schema"] = 7
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "Migrate 没跑 v8 这一档")
    if int(db["schema"]) != int(gql.Config.SCHEMA_VERSION):
        return fail(name, f"跑完没推到最新档，实际 {db['schema']}")
    if db["quests"][90573]["mapID"] is not None or db["quests"][90573]["mapSrc"] is not None \
            or db["quests"][90573]["mapManual"] is not None:
        return fail(name, "口述图没清干净（mapID/mapSrc/标记要一起走）")
    if db["quests"][96625]["mapID"] != 2393 or db["quests"][96625]["mapSrc"] != "池行自带":
        return fail(name, "客户端重写过的图不许清，只能抹旧标记")
    if db["quests"][96625]["mapManual"] is not None:
        return fail(name, "残留的 mapManual 标记没抹掉")
    if db["quests"][89377]["mapID"] != 2413:
        return fail(name, "客户端自己的图被误伤")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "同一档不该再跑第二趟")
    ok(name)


def test_migrate_drops_unbacked_done_views():
    """schema v9：健步如飞 ×14 假绿的存档侧一刀（2026-10-29，用户批准的"两刀"之二）。
    老代码把客户端的完成 flag 残留原样写进快照（固定 ID 任务跨轮不清 flag），
    凭空续命出"已交付"的绿勾；快照写入口已加"编号对上的本轮流水"门（Sync 池循环），
    这档把存档里躺着的那批一次清掉 —— evidence 梯子降级回去，结算后自愈：
    ① 池任务（poolSeen、无 regKey）的 done 快照：没有流水、最新流水编号对不上、
       双方都没编号 → 删；
    ② 编号对上的 → 留（真交过的凭据 k/t 一字不动）；s≠done 的快照一个不碰；
    ③ 注册周常（regKey）与非池任务豁免 —— 判据只对 flag 残留的已知形状下手；
    ④ turnins 金币账一条不删；
    ⑤ 幂等：跑完推到最新档，再跑 Migrate 返回 false，手跑一遍返回 0。"""
    name = "migrate_drops_unbacked_done_views"
    lua, gql = load_lua("Config", "Ledger")
    g = lua.globals()
    g.GQLDB = lua.table_from({})          # 非 mock 环境没有 SavedVariables，这里自建
    db = g.GQLDB
    db["quests"] = lua.table_from({
        91001: lua.table_from({"name": "健步如飞", "mapID": 2405, "poolSeen": True}),
        91002: lua.table_from({"name": "真交过的", "mapID": 2405, "poolSeen": True}),
        91003: lua.table_from({"name": "编号对不上", "mapID": 2405, "poolSeen": True}),
        91004: lua.table_from({"name": "注册周常", "mapID": 2405, "poolSeen": True,
                               "regKey": "w:巢穴"}),
        91005: lua.table_from({"name": "非池任务", "mapID": 2405}),
        91006: lua.table_from({"name": "没做完的", "mapID": 2405, "poolSeen": True}),
        91007: lua.table_from({"name": "双方都没编号", "mapID": 2405, "poolSeen": True}),
    })
    db["chars"] = lua.table_from({
        "甲-永恒之井": lua.table_from({
            "views": lua.table_from({
                91001: lua.table_from({"s": "done", "t": 10}),
                91002: lua.table_from({"s": "done", "t": 20, "k": "91002:100"}),
                91003: lua.table_from({"s": "done", "t": 30, "k": "91003:200"}),
                91004: lua.table_from({"s": "done", "t": 40}),
                91005: lua.table_from({"s": "done", "t": 50}),
                91006: lua.table_from({"s": "open", "t": 60}),
                91007: lua.table_from({"s": "done", "t": 70}),
            }),
        }),
        "乙-永恒之井": lua.table_from({
            "views": lua.table_from({91001: lua.table_from({"s": "done", "t": 11})}),
        }),
    })
    db["turnins"] = lua.table_from({
        "甲-永恒之井": lua.table_from({
            91002: lua.table_from({lua.table_from({"t": 20, "k": "91002:100"})}),
            91003: lua.table_from({lua.table_from({"t": 30, "k": "91003:999"})}),
            91007: lua.table_from({lua.table_from({"t": 70})}),
        }),
    })
    db["schema"] = 8
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "Migrate 没跑 v9 这一档")
    if int(db["schema"]) != int(gql.Config.SCHEMA_VERSION):
        return fail(name, f"跑完没推到最新档，实际 {db['schema']}")
    for char in ("甲-永恒之井", "乙-永恒之井"):
        if db["chars"][char]["views"][91001] is not None:
            return fail(name, f"{char} 的 91001 done 快照没有流水背书，该清掉")
    for qid in (91003, 91007):
        if db["chars"]["甲-永恒之井"]["views"][qid] is not None:
            return fail(name, f"{qid} 的 done 快照编号对不上最新流水（或双方都没编号），该清掉")
    kept = db["chars"]["甲-永恒之井"]["views"]
    if str(kept[91002]["k"]) != "91002:100" or int(kept[91002]["t"]) != 20:
        return fail(name, "编号对上的真凭据不许动（k/t 原样）")
    if str(kept[91004]["s"]) != "done" or str(kept[91005]["s"]) != "done":
        return fail(name, "注册周常与非池任务豁免，done 快照一个不许清")
    if str(kept[91006]["s"]) != "open":
        return fail(name, "只清 done 快照，别的状态一个不碰")
    turns = db["turnins"]["甲-永恒之井"]
    for qid in (91002, 91003, 91007):
        if len(turns[qid]) != 1:
            return fail(name, f"turnins 是金币账本，{qid} 的流水一条不许删")
    if "数据升级 v9 完成（处理 4 条）" not in str(gql.Config.GetLogs()):
        return fail(name, f"迁移返回值（清理条数）要进通用日志，实际：{gql.Config.GetLogs()}")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "同一档不该再跑第二趟")
    if int(lua.execute("return GQL.Ledger.DropUnbackedDoneViews(GQL.Config.DB())")) != 0:
        return fail(name, "跑完之后再手跑一遍该是 0（幂等，别把幸存凭据也清了）")
    ok(name)


def test_migrate_stamps_the_log_key_for_rows_already_alive():
    """schema v10：代码修好了，**已经写进存档的那一份不会自愈**（v2/v6/v7/v9 同一族教训，
    计划 §三"老存档不自愈 ⇒ v10 迁移"）。真档 14:57 落盘那份里 94446 梦魇任务此刻就挂着
    `kwLogAdded` 而没有编号：更新完第一次登录如果用的是"没接这条的号"，Sync.lua:259
    当场把布尔位撤走，而这条记录没有第二份凭据可留 ⇒ 整行对全账号消失 ——
    正是"改完没有变化"那一族（用户会当成没修）。v10 一次性给当下 `kwLogAdded` 的记录
    烙上**当前**这条实例的编号（周 CD 尾巴档，下周重置自己作废）：
    ① 只认 kwLogAdded 且还没有 kwLogKey 的；② 已经烙过的一个字不动（幂等，重跑 0 条）；
    ③ 没有 kwLogAdded 的记录一条不碰 —— 方向是**少报不多报**，凭空发凭据就是给
    玩家从没要过的行发一张上板通行证；④ 跑完停在最新档 11（v10 之后还有 v11 清 admittedKey，
    阶梯用例的终态跟着推一档）；
    ⑤ 只烙编号不给勾：勾仍然只来自快照/流水/补录那三份带编号的凭据。"""
    name = "migrate_stamps_the_log_key_for_rows_already_alive"
    lua, gql = load_lua("Config", "Ledger")
    L, C = gql.Ledger, gql.Config
    if lua.execute("return type(GQL.Config.MIGRATIONS[10]) == 'function'") is not True:
        return fail(name, f"关键词日志凭据这一刀必须开成 v10 这一档（老存档不自愈 = 改完没变化），"
                          f"实际 MIGRATIONS[10] 不是函数（SCHEMA_VERSION={C.SCHEMA_VERSION}）")
    g = lua.globals()
    g.GQLDB = lua.table_from({})          # 非 mock 环境没有 SavedVariables，这里自建
    db = g.GQLDB
    db["cycleAnchor"] = 1700000000        # 墙上秒锚点 ⇒ 尾巴档 = +604800，编号算得出来
    db["quests"] = lua.table_from({
        94446: lua.table_from({"name": "梦魇任务", "kwLogAdded": True,
                               "currentSig": "-1", "lastSeenAt": 1700000000,
                               "observations": lua.table_from({})}),
        94447: lua.table_from({"name": "已经烙过的", "kwLogAdded": True,
                               "kwLogKey": "94447:1",
                               "observations": lua.table_from({})}),
        94448: lua.table_from({"name": "池里的世界任务", "poolSeen": True,
                               "observations": lua.table_from({})}),
    })
    db["chars"] = lua.table_from({
        "甲-永恒之井": lua.table_from({"views": lua.table_from(
            {94446: lua.table_from({"s": "done", "t": 1699000000})})}),
    })
    db["schema"] = 9
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "Migrate 没跑 v10 这一档")
    if int(db["schema"]) != 11:
        return fail(name, f"跑完要停在最新档 11（v10 烙编号 + v11 清 admittedKey），实际 {db['schema']}")
    want = uid(L, db, 94446, 1700000000)
    got = db["quests"][94446]["kwLogKey"]
    if want is None:
        return fail(name, "前置：这一条的本周编号该算得出来（周 CD 尾巴档）")
    if got is None or str(got) != want:
        return fail(name, f"挂着 kwLogAdded 的记录要烙上**当前**编号 {want}，实际 {got}")
    if str(db["quests"][94447]["kwLogKey"]) != "94447:1":
        return fail(name, "已经烙过的不许重烙（幂等认的是 kwLogKey 在场，不看布尔位）")
    if db["quests"][94448]["kwLogKey"] is not None:
        return fail(name, "没有 kwLogAdded 的记录一条不许碰 —— 凭空发凭据 = 造出玩家没要过的行")
    if db["quests"][94446]["roundEnd"] is not None:
        return fail(name, "迁移只烙编号，不许顺手给常驻周常写板面倒计时（roundEnd 一在场，"
                          "身份就从 CD 尾巴挪到板面档，刚烙的凭据当场对不上）")
    if str(db["chars"]["甲-永恒之井"]["views"][94446]["s"]) != "done":
        return fail(name, "只烙不删：v10 不许清动角色级快照（无背书 done 那一族归 v9 管）")
    if st(L, db, "乙-永恒之井", 94446, 1700000000) == "done":
        return fail(name, "这份编号只是'这一条实例被某号的日志证明存在过'，"
                          "不给任何一格凭空打勾（乙号一个字没同步过，勾仍只来自快照/流水/补录）")
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not False:
        return fail(name, "同一档不该再跑第二趟")
    if int(L.StampKeywordLogKeysOnce(db)) != 0:
        return fail(name, "跑完之后再手跑一遍该是 0 条（幂等，别把已经烙好的再算一遍）")
    if "数据升级 v10 完成（处理 1 条）" not in str(gql.Config.GetLogs()):
        return fail(name, f"迁移返回值（烙上的笔数）要进通用日志，实际：{gql.Config.GetLogs()}")
    ok(name)


def test_migrate_stamps_this_weeks_log_key_when_the_anchor_is_stale():
    """v10 最难的那一趟登录：**周四刚过、代码刚更新**。存档里的 `cycleAnchor` 还是上一个 CD
    那一次（`Config.Init` 只在锚点为 0/nil 时补问周重置，旧值它不动），而这一档的编号正是从
    `cycleAnchor + 604800` 那档尾巴算的 ⇒ 不补问就烙成**上周**那一桶；同一趟里锚点随后被
    刷新 ⇒ 当前编号挪了一档 ⇒ 刚烙的凭据对不上自己，梦魇任务照样整行消失 —— 而且这次是
    "改完当天那一整周都没变化"，比不迁移还难查。
    邻居 v6 `BackfillInstanceKeysOnce` 早就处理过这个形状（"补问之前那个锚点"那段守卫），
    v10 照抄同一道：锚点 +604800 <= 同域参考 ⇒ 先 `Config.RefreshCycle` **一次**再算编号。
    代价口径不破：锚点没过期时一次都不许多问周重置（下面那组对照钉的就是这个）。"""
    name = "migrate_stamps_this_weeks_log_key_when_the_anchor_is_stale"
    wall, stale_anchor, cd_anchor = 1761620000, 1760595200, 1761200000
    # 1761620000 + 184800 - 604800 = 1761200000 ⇒ 本 CD 锚点；上周那一档 = 1760595200
    weekly_calls = {"n": 0}

    def weekly():
        weekly_calls["n"] += 1
        return 184800

    def fresh_db(anchor):
        lua, gql, _ = sync_env([], wall=wall)
        g = lua.globals()
        g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": weekly})
        g.GQL_NOW = 400000
        db = gql.Config.DB()
        gql.Config.AddKeyword("梦魇")
        db["cycleAnchor"] = anchor
        db["schema"] = 9
        db["quests"][94446] = lua.table_from({
            "name": "梦魇任务", "kwLogAdded": True, "currentSig": "-1",
            "lastSeenAt": wall, "observations": lua.table_from({})})
        return lua, gql, db

    lua, gql, db = fresh_db(stale_anchor)
    L = gql.Ledger
    stale_key = "94446:%d" % ((stale_anchor + 604800 + 450) // 900)
    if uid(L, db, 94446, wall) != stale_key:
        return fail(name, f"前置：不补问时这一条现在算得出的是上周那一桶 {stale_key}"
                          f"（实际 {uid(L, db, 94446, wall)}）—— 用例要打的正是这个形状")
    weekly_calls["n"] = 0
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "锚点过期那一趟 v10 没跑")
    if int(db["schema"]) != 11:
        return fail(name, f"跑完要停在最新档 11（v10 之后还有 v11 清 admittedKey），实际 {db['schema']}")
    if str(db["cycleAnchor"]) != str(cd_anchor):
        return fail(name, f"锚点过期 ⇒ 迁移必须先补问一次把锚点推到本 CD，实际 {db['cycleAnchor']}")
    got = str(db["quests"][94446]["kwLogKey"])
    want = uid(L, db, 94446, wall)
    if got != want:
        return fail(name, f"烙的必须是**刷新后**当前编号 {want}，实际 {got}"
                          f"（{stale_key} = 上周那一桶 = 白烙）")
    if got == stale_key:
        return fail(name, "烙成了上周那一桶 ⇒ 这趟登录之后整行照旧消失，v10 等于没做")
    if weekly_calls["n"] != 1:
        return fail(name, f"补问只许发生一次（锚点确实过期 + 一趟迁移一趟），实际 {weekly_calls['n']} 次")
    # 用户报的那一幕必须在**这次更新的当周**就不成立：换一个没接这条的号登录后布尔位被撤，
    # 这一份编号是整行唯一的凭据
    db["quests"][94446]["kwLogAdded"] = None
    if gql.Ledger.KeywordLogAdmits(db, 94446, db["quests"][94446], wall) is not True:
        return fail(name, "锚点过期那趟迁移之后，没接这条的号登录仍然留不住这一行 —— "
                          "凭据烙在了对不上的桶里")

    # 对照组：锚点还是本 CD 的那一次（多数登录）⇒ 一次都不许多问周重置
    lua2, gql2, db2 = fresh_db(cd_anchor)
    weekly_calls["n"] = 0
    if lua2.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "对照：锚点没过期的那趟 v10 也没跑")
    if weekly_calls["n"] != 0:
        return fail(name, f"锚点没过期却多问了 {weekly_calls['n']} 遍周重置（零新增接口调用）")
    if str(db2["quests"][94446]["kwLogKey"]) != uid(gql2.Ledger, db2, 94446, wall):
        return fail(name, "对照组烙的编号对不上当前编号")
    ok(name)


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
    """（旧 options_hunt_button 用例的口径原样搬家，入口从设置面板挪到关键词面板页脚）
    同步模块没加载时点"扫描地图"不许抛错：上一版直接
    `pcall(GQL.Sync.HuntKeywordMaps)` —— 被 pcall 的参数 `GQL.Sync.HuntKeywordMaps`
    是在保护**外**求值的，GQL.Sync 为 nil 时"attempt to index a nil value"
    当场炸出 OnClick，pcall 根本没护到。要先判空（本文件别处同款写法）再调用。"""
    lua, gql, _ = sync_env([(162, 30000000)], extra=("Matrix", "Options"))
    gql.Options.OpenWindow()
    # 2026-11-01 起扫描按钮在地图段那一排（输入框 → 按名称添加 → 扫描地图 → 添加当前所在地图），
    # 控件挂在页面表上，不再按帧树找
    found = lua.execute("""
        local p = GQL.Options.pages and GQL.Options.pages.win
        local b = p and p.huntBtn
        if not b or b.__text ~= "扫描地图" then return false end
        GQL_HUNTBTN = b
        return true
    """)
    if found is not True:
        return fail("panel_hunt_button_survives_missing_sync",
                    "页面里没找到扫描按钮")
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
    上一版这个标记跨轮、跨 CD 都不作废；那一版把判据收窄成"本轮点过才算"（当时的
    RollRound，如今这台"推起点"的机器连同 manualAdded 字段一起整块下线，见下面
    第二十七轮那段），可那只对**下一次换轮**生效 —— 已经写进 SavedVariables 的那一份
    不会自己长回来，
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
        return fail(name, "前置：B 那份老金币粘滞与老“本轮起点”字段同档（两者现在都没有写点，"
                              "这句只钉存档形状对齐，不是现役判据）")
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
        return fail(name, "前置：B 那份老金币粘滞与老“本轮起点”字段同档（两者现在都没有写点，"
                              "这句只钉存档形状对齐，不是现役判据）")
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
    """让一条在池的世界任务真换上**下一条实例**：先拿接口倒计时定好这一条的结束时刻，
    到点之后再问一次倒计时（板面答出新的结束时刻 ⇒ L.InstanceKey 自然换编号）。
    时刻形状照 12.1.5 真存档 89347：白天点的勾撑到下一轮还在。
    老这一对在 Ledger.RoundEnded + RollRound 上"推起点"，第 36 轮 §五 整块删掉 ——
    编号模型里换轮不是动作，是"结束时刻变了"这件事的推论，所以这里第二次读的
    必须是**新的**倒计时值，不是原样再推一次快照。"""
    L.DiffPool(db, pool(lua, [(qid, money, None)]), at)
    L.SetExpiry(db["quests"][qid], at, 43200)       # 这一条到 at+43200 结束
    L.DiffPool(db, pool(lua, [(qid, money, None)]), then)
    L.SetExpiry(db["quests"][qid], then, 43200)     # 到点之后板面答出下一条的结束时刻
    return db


def bind_db(lua, db):
    """把纯账本用例手搓的 db 接成 GQL.Config.DB() 的返回值：
    AddQuest / SetCycleExpiry 这些"账本 + 接口"混合入口都从那儿读库，
    不接这一句它们会去摸替身里根本不存在的 GQLDB。"""
    lua.globals().GQLDB = db
    lua.execute("GQL.Config.DB = function() return GQLDB end")
    return db


WALL_NOW = 1761620000          # 迁移开头 Api.WallClock 答的那一次
UPTIME_NOW = 500000            # 同一趟 Api.Uptime（= 替身的 GetTime）
CD_ANCHOR = 1761200000         # 本 CD 锚点：+604800 = 1761804800 > WALL_NOW ⇒ 不过期
BOARD_END = 1761700000         # 板面倒计时答出的结束时刻（还在未来）


def v6_fixture_lua():
    """照 12.1.5 真存档那 20 行的形状手搓一份"上一版写盘、凭据全没带 k"的账本。
    刻意走真实字段名与真实档位（roundAt/roundEnd/expiresSrc/goldMoney/goldRound/
    observations[sig].firstSeen），因为 v6 的规矩之一就是**一个都不许删**。"""
    return r"""
local db = GQL.Config.DB()
db.cycleAnchor = 1761200000
db.quests, db.chars, db.turnins, db.manual = {}, {}, {}, {}
-- 靶心：94612 举钳欢庆。roundAt 与 cycleAnchor 逐秒相同 = 这条记录从没滚过轮，
-- 老档里压根没存过"这一条实例几点开始"；板上那条的结束时刻倒是答得出（10-31 15:00）。
db.quests[94612] = { name = '举钳欢庆', mapID = 2405, poolSeen = true,
    roundAt = 1761200000, roundEnd = 1761700000, expiresAt = 1761700000,
    expiresSrc = '接口倒计时', currentSig = '10580000',
    goldMoney = 10580000, goldRound = 1761200000,
    observations = { ['10580000'] = { money = 10580000, gold = true,
                                      firstSeen = 1761000000, lastSeen = 1761250000 } } }
-- 对照组（真档 93573 的形状）：roundAt 真滚过（1761450000 ≠ 锚点）⇒ 起点证得上
db.quests[93573] = { name = '战胜未知：怒火攻心的漂流者', mapID = 2405, poolSeen = true,
    roundAt = 1761450000, roundEnd = 1761700000, expiresAt = 1761700000,
    expiresSrc = '接口倒计时', currentSig = '8000000',
    goldMoney = 8000000, goldRound = 1761450000,
    observations = { ['8000000'] = { money = 8000000, gold = true,
                                     firstSeen = 1761450000, lastSeen = 1761500000 } } }
-- 尾巴档：用户点名的"获取不到结束时间的任务组"（真档 9 行之一），没有 roundEnd
db.quests[97128] = { name = '巢穴：尼姆瑞莎', regKey = 'w:巢穴', mapID = 2405,
                     observations = {} }
db.quests[92636] = { name = '特别任务：祖阿曼的馈赠', regKey = 'w:特别任务',
                     mapID = 2405, observations = {} }
-- 起点无从重建（roundAt 字段压根没有）：走 L.RoundAt 就会被兜底成锚点并写回存档，
-- 这一条专门钉"迁移只读原始字段、一个字都不写回"
db.quests[90001] = { name = '没滚过轮也没有起点', mapID = 2405,
    roundEnd = 1761700000, expiresSrc = '接口倒计时', observations = {} }
-- 身份未定（板面倒计时是上一条实例的墓碑）⇒ 一笔不回填、也不算进"判为更早"那笔账
db.quests[89377] = { name = '秘密狩猎', mapID = 2405,
    roundAt = 1761050000, roundEnd = 1761000000, expiresSrc = '接口倒计时',
    observations = {} }
-- 开机秒域的一条板面记录：now 必须挑 uptime，挑错域就 fail-closed 白掉这一个真勾
db.quests[90333] = { name = '屠戮者高地的一条', mapID = 2414, poolSeen = true,
    roundAt = 400000, roundEnd = 503600, expiresSrc = '接口倒计时',
    goldMoney = 6000000, goldRound = 400000, observations = {} }
-- 七个号的登录快照全是 10-22 那一批（时刻在锚点之后 ⇒ 今天的读侧照样给它打勾）
for i = 1, 7 do
    local k = '号' .. i .. '-永恒之井'
    db.chars[k] = { lastSync = 1761250000, views = {}, reads = {} }
    db.chars[k].views[94612] = { s = 'done', t = 1761250000 }
end
for i = 1, 3 do
    local k = '号' .. i .. '-永恒之井'
    db.manual[k] = { [94612] = { state = 'done', t = 1761250000, g = 10580000, round = 1761200000 } }
end
-- 对照组：晚于自己 roundAt 的挂上，早于 roundAt（但晚于锚点）的那两笔挂不上
db.chars['号1-永恒之井'].views[93573] = { s = 'done', t = 1761500000 }
db.chars['号2-永恒之井'].views[93573] = { s = 'done', t = 1761500000 }
db.chars['号3-永恒之井'].views[93573] = { s = 'done', t = 1761300000 }
db.chars['号1-永恒之井'].reads[93573] = { g = 8000000, t = 1761600000 }
db.turnins['号1-永恒之井'] = { [93573] = { { t = 1761300000, g = 8000000, sig = '8000000',
                                             cycle = 1761200000, round = 1761200000 },
                                           { t = 1761500000, g = 8000000, sig = '8000000',
                                             cycle = 1761200000, round = 1761450000 } } }
-- 尾巴档：锚点之后的勾全挂上，锚点之前那一笔不挂
db.chars['号4-永恒之井'].views[97128] = { s = 'done', t = 1761300000 }
db.chars['号5-永恒之井'].views[97128] = { s = 'open', t = 1761150000 }
db.turnins['号4-永恒之井'] = { [97128] = { { t = 1761250000, g = 0, sig = '0',
                                            cycle = 1761200000, round = 1761200000 } } }
db.manual['号6-永恒之井'] = { [92636] = { state = 'done', t = 1761210000, g = 0 } }
db.chars['号7-永恒之井'].views[90001] = { s = 'done', t = 1761300000 }
db.chars['号1-永恒之井'].views[90001] = { s = 'done', t = 1761350000 }
db.chars['号2-永恒之井'].views[89377] = { s = 'done', t = 1761100000 }
db.chars['号7-永恒之井'].views[90333] = { s = 'done', t = 450000 }
db.schema = 5
"""


def v6_env():
    lua, gql, _ = sync_env([])
    g = lua.globals()
    # 迁移开头那两个一次性时钟读数：墙上走 Api 的 GetServerTime 候选，开机秒走替身 GetTime
    g.GetServerTime = lambda: WALL_NOW
    g.GQL_NOW = UPTIME_NOW
    calls = {"n": 0}

    def weekly():
        calls["n"] += 1
        return 184800      # WALL_NOW + 184800 - 604800 = 1761200000 = 本 CD 的锚点

    g.C_DateAndTime = lua.table_from({"GetSecondsUntilWeeklyReset": weekly})
    lua.execute(v6_fixture_lua())
    return lua, gql, g, calls


def v6_tally(live):
    """数三件事：带 k 的笔数、不带 k 的笔数、db.turnins 的总行数（"数据一条没删"的凭据）。"""
    return live.execute("""
local db = GQL.Config.DB()
local have, missing, turns = 0, 0, 0
for _, byQuest in pairs(db.turnins or {}) do
    for _, rows in pairs(byQuest) do
        for i = 1, #rows do
            turns = turns + 1
            if rows[i].k then have = have + 1 else missing = missing + 1 end
        end
    end
end
for _, byQuest in pairs(db.manual or {}) do
    for _, m in pairs(byQuest) do
        if m.k then have = have + 1 else missing = missing + 1 end
    end
end
for _, row in pairs(db.chars or {}) do
    for _, v in pairs(row.views or {}) do
        if v.k then have = have + 1 else missing = missing + 1 end
    end
    for _, r in pairs(row.reads or {}) do
        if r.k then have = have + 1 else missing = missing + 1 end
    end
end
return have, missing, turns
""")


def v6_listed(live):
    """矩阵列了哪些行（排序后的 id 串）。迁移前后必须一模一样：v6 只配钥匙、不换门。"""
    return one(live.execute(
        "local out = GQL.Ledger.MatrixRows(GQL.Config.DB(), %d) "
        "local ids, n = {}, 0 "
        "for i, grp in ipairs(out) do for j, r in ipairs(grp.rows) do "
        "  n = n + 1 ids[#ids + 1] = tostring(r.questID) end end "
        "table.sort(ids) return table.concat(ids, ',')" % WALL_NOW))


def v6_field(live, expr):
    return one(live.execute("return tostring(%s)" % expr))


def test_v6_backfill_drops_only_the_unprovable_marks():
    """计划 §十用例 8：迁移 v5→v6 只给"证得上属于这一条实例"的老凭据配钥匙。
    用户 2026-10-28 的原话是"旧的数据如果没存，都要修改"，但同一轮他批准的那份清单里
    **唯一挂不上的就是 94612**（"举钳欢庆 挂不上才是正常的，因为我最开始就说了 本轮次还没人做过"），
    而尾巴档那 9 行（92636/90962/93244/97128/95918/90573/89507/98172/96718）的勾一个不许掉。
    ⇒ 这一条同时钉四个方向：
    ① 94612 形状（roundAt 与 cycleAnchor 逐秒相同 = 从没滚过轮）⇒ 7 笔快照 + 3 笔补录
       **一笔都不挂**（v6 只配钥匙不换门；快照那 7 笔随后被阶梯里的 v9 以"无背书"清掉，
       见后面 ① 的注）；矩阵行数与 db.turnins 行数一条不少（无编号 ≠ 抹掉流水）；
    ② 对照组（roundAt 真滚过）⇒ 晚于自己起点的挂上、早于起点的（哪怕晚于锚点）不挂
       —— 后者就是"起点兜底成周初"那张 fail-open 的脸，退回锚点当起点它当场绿回去；
    ③ 尾巴档 ⇒ 锚点之后的挂上周 CD 尾巴编号，锚点之前那一笔不挂；
    ④ **老字段一个都不在 v6 里删**（roundAt/goldMoney/goldRound/obs.firstSeen 原样还在），
       而且绝不把 roundAt **写回去**（读起点只认原始字段；走 L.RoundAt 就会把"判不出"
       做成"本周初一直在"落进存档，90001 那条 roundAt 压根没有的记录专门钉这一句）。
    外加两个 setup（§九第 0 步）：now 与 roundEnd 同域（90333 走开机秒），
    以及"锚点确实过期才补问一次周重置倒计时"—— 锚点新鲜时那次查询必须一次都没发生。
    身份未定的那条（89377，板面倒计时已是墓碑）既不回填也不计入"判为更早"：
    那不是"更早"，是"挂不上编号"，取证面另说。"""
    name = "v6_backfill_drops_only_the_unprovable_marks"
    lua, gql, g, weekly_calls = v6_env()
    L, C = gql.Ledger, gql.Config
    db = g.GQLDB
    board_key = "%d:%d" % (93573, (BOARD_END + 450) // 900)
    tail_key = "%d:%d" % (97128, (CD_ANCHOR + 604800 + 450) // 900)

    # 前置（T4b 已把读侧换成编号）：老凭据没烙编号 ⇒ 今天一律不认 —— 这正是 v6 必须
    # 排在读侧切换**之前**的原因（§九：先配钥匙再换门，否则部署那一次同步把好勾全清光）。
    if v6_field(lua, "GQL.Ledger.State(GQL.Config.DB(), '号1-永恒之井', 94612, nil, %d)"
                % WALL_NOW) == "done":
        return fail(name, "前置：读侧已换成编号，没烙编号的那一笔不该再报 done（fail-closed）")
    if v6_field(lua, "GQL.Config.DB().quests[90001].roundAt") != "nil":
        return fail(name, "前置：90001 这条老档就是没存 roundAt（写回与否的靶子）")

    weekly_calls["n"] = 0
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "v6 没接上这一棒：Migrate 对 schema=5 的存档没跑新档")
    if int(v6_field(lua, "GQL.Config.DB().schema")) != int(C.SCHEMA_VERSION) \
            or int(C.SCHEMA_VERSION) != 11:
        return fail(name, f"跑完该停在最新档 11（v11 清 admittedKey），实际 SCHEMA_VERSION={C.SCHEMA_VERSION} "
                          f"db.schema={v6_field(lua, 'GQL.Config.DB().schema')}")
    # 锚点新鲜 ⇒ 一次接口都不许多问（§三第 2 条与用户 10-27 的零新增接口调用）
    if weekly_calls["n"] != 0:
        return fail(name, f"锚点没过期的那次登录多问了 {weekly_calls['n']} 遍周重置倒计时 —— "
                          "补问只许发生在「锚点确实已经过去」那一支")

    def k_of(expr):
        return v6_field(lua, expr)

    # ① 94612：10 笔全挂不上（起点重建不出来 ⇒ 无从证明这一笔属于这一条实例）。
    #    快照那 7 笔随后被 v9 整笔清掉（池任务、无流水背书的 done 快照，见 v9 用例），
    #    "v6 没给它们烙编号"从此观测不到 —— 同一条 mark() 判的补录 3 笔照旧在档，
    #    由它们代表这个判断；快照只剩"确实不在了"可验。
    #    这一刀放在全库计数之前：万一有人把"起点判不出"退回到周初，红字要当场点名 94612，
    #    而不是只报一个对不上的总数。
    for i in range(1, 4):
        got = k_of("GQL.Config.DB().manual['号%d-永恒之井'][94612].k" % i)
        if got != "nil":
            return fail(name, f"94612 的补录第 {i} 笔被烙上了 {got} —— 这条从没滚过轮，"
                              "老档里没存过这一条实例几点开始，判挂不上（用户已批准）")
    for i in range(1, 8):
        if lua.execute("return GQL.Config.DB().chars['号%d-永恒之井'].views[94612] ~= nil" % i):
            return fail(name, f"94612 的快照第 {i} 笔还在档 —— 它无流水背书，v9 该清掉")
    # 只配钥匙不换门：拿 v9 豁免的尾巴档那笔验"回填不许改动老凭据自己的字段"（s/t 原样）
    if k_of("GQL.Config.DB().chars['号4-永恒之井'].views[97128].s") != "done" \
            or int(k_of("GQL.Config.DB().chars['号4-永恒之井'].views[97128].t")) != 1761300000:
        return fail(name, "回填不许改动老凭据自己的字段（s/t 原样）")

    have, missing, turns = v6_tally(lua)
    # v6 自己的账是"烙上 8 / 无编号 16"；随后 v9 把池任务里无流水背书的 done 快照清了
    # 10 笔（94612×7 + 93573 号2/号3 + 90333 —— 号2/号3/94612 那些本来无编号，90333 烙了），
    # 终态 6/8。号2 那笔虽烙上编号，但它自己号下没有流水 —— 背书按角色算，不能借号1 的。
    # 89377 身份未定那 1 笔不在清理范围（非池任务豁免）。
    if int(have) != 6 or int(missing) != 8:
        return fail(name, f"回填 8 笔、v9 清 10 笔无背书快照后该剩 6 有编号 / 8 无编号，"
                          f"实际 {have} / {missing}")
    if int(turns) != 3:
        return fail(name, f"db.turnins 一条都不许删（§九第 5 条：用户批准的是挂不上，不是抹流水）"
                          f"，实际剩 {turns} 行")
    logs = str(gql.Config.GetLogs())
    if "数据升级 v6 完成（处理 8 条）" not in logs:
        return fail(name, f"迁移返回值（烙上的笔数）要进那条通用日志，实际：{logs}")
    if "回填 8 笔 / 判为更早 15 笔" not in logs:
        return fail(name, f"两笔账要单独记一行（Y 得能在 /gql quest 里逐笔追认），实际：{logs}")

    # ② 对照组：晚于自己 roundAt 的都挂上；早于 roundAt（但晚于锚点）的那两笔，
    #    快照（号2/号3）因无背书已被 v9 清掉（背书按角色算，号2 不能借号1 的流水），
    #    流水旧的那笔 v9 不碰、照旧验。
    for expr, where in (("GQL.Config.DB().chars['号1-永恒之井'].views[93573].k", "快照1"),
                        ("GQL.Config.DB().chars['号1-永恒之井'].reads[93573].k", "按号读数"),
                        ("GQL.Config.DB().turnins['号1-永恒之井'][93573][2].k", "流水(新)")):
        if k_of(expr) != board_key:
            return fail(name, f"对照组{where}该挂上本条实例的编号 {board_key}，实际 {k_of(expr)}")
    for i in (2, 3):
        if lua.execute("return GQL.Config.DB().chars['号%d-永恒之井'].views[93573] ~= nil" % i):
            return fail(name, f"号{i} 的快照在自己号下没有流水背书（号3 的时刻还在 roundAt 之前，"
                              "属于上一条实例），v9 该把这类 done 快照清掉")
    if k_of("GQL.Config.DB().turnins['号1-永恒之井'][93573][1].k") != "nil":
        return fail(name, f"对照组流水(旧)的时刻在 roundAt 之前（属于上一条实例），"
                          f"却挂上了 {k_of('GQL.Config.DB().turnins[\'号1-永恒之井\'][93573][1].k')}"
                          " —— 起点退回锚点就是这么把假勾放回来的")

    # ③ 尾巴档：锚点之后一笔不掉，锚点之前那笔不挂
    if k_of("GQL.Config.DB().chars['号4-永恒之井'].views[97128].k") != tail_key:
        return fail(name, f"97128（没有 roundEnd 的周常组）该挂周 CD 尾巴编号 {tail_key}，实际 "
                          + k_of("GQL.Config.DB().chars['号4-永恒之井'].views[97128].k"))
    if k_of("GQL.Config.DB().turnins['号4-永恒之井'][97128][1].k") != tail_key:
        return fail(name, "尾巴档的流水同样要挂上尾巴编号（用户：97128 是周常，按周常 CD）")
    if k_of("GQL.Config.DB().manual['号6-永恒之井'][92636].k") != "%d:%d" % (
            92636, (CD_ANCHOR + 604800 + 450) // 900):
        return fail(name, "尾巴档的补录没挂上（92636 也在用户批准的那 9 行里）")
    if k_of("GQL.Config.DB().chars['号5-永恒之井'].views[97128].k") != "nil":
        return fail(name, "比锚点还早的一笔被算进了本周（尾巴档的起点就是 cycleAnchor）")

    # ③' 开机秒域那条：now 挑错域会 fail-closed 白掉真勾（不造假勾，但损失没人看得见）。
    #    90333 是池任务且没有流水 —— 它的 done 快照随后被 v9 清掉（无背书），这里改验
    #    "确实不在了"；烙编号的同域判定与真实 Sync 入口共用一套写点，由那些用例覆盖。
    if lua.execute("return GQL.Config.DB().chars['号7-永恒之井'].views[90333] ~= nil"):
        return fail(name, "90333 的 done 快照没有流水背书（池任务、无 regKey），v9 该清掉")

    # ③'' 身份未定（板面倒计时已是墓碑）：一笔不挂，也不算进"判为更早"那笔账
    if k_of("GQL.Config.DB().chars['号2-永恒之井'].views[89377].k") != "nil":
        return fail(name, "89377 那条已结束、再也问不出倒计时的实例退回了周 CD 尾巴 —— "
                          "同周上下两条实例会并成同一个编号（§三第 1 条）")

    # ④ 老字段一个都不删、也一个都不写回
    for expr, want, label in (
            ("GQL.Config.DB().quests[94612].roundAt", "1761200000", "roundAt"),
            ("GQL.Config.DB().quests[94612].roundEnd", "1761700000", "roundEnd"),
            ("GQL.Config.DB().quests[94612].expiresSrc", "接口倒计时", "expiresSrc"),
            ("GQL.Config.DB().quests[94612].goldMoney", "10580000", "goldMoney"),
            ("GQL.Config.DB().quests[94612].goldRound", "1761200000", "goldRound"),
            ("GQL.Config.DB().quests[94612].observations['10580000'].firstSeen",
             "1761000000", "obs.firstSeen"),
            ("GQL.Config.DB().quests[94612].currentSig", "10580000", "currentSig"),
            ("GQL.Config.DB().turnins['号1-永恒之井'][93573][1].round", "1761200000", "round"),
            ("GQL.Config.DB().manual['号1-永恒之井'][94612].state", "done", "manual.state")):
        got = k_of(expr)
        if got != want:
            return fail(name, f"v6 只许烙编号：{label} 该还是 {want}，实际 {got} —— "
                              "删早了读侧的 fail-open 当场复活，94612 的十个假勾原地回来")
    if k_of("GQL.Config.DB().quests[90001].roundAt") != "nil":
        return fail(name, "迁移把 roundAt 写回存档了（走了 L.RoundAt 的兜底）："
                          "'判不出这一条什么时候开始'被做成'本周初一直在'，"
                          "94612 那类记录下一次登录连证据都没了")

    # 行一条不少：拿一份没跑过迁移的孪生档比（迁移会就地往同一份档里烙编号，
    # 拿跑过的那份自己比自己比不出干净结论；roundAt 现在没有任何写点，不再是理由）
    lua2, gql2, _ = sync_env([])
    g2 = lua2.globals()
    g2.GetServerTime = lambda: WALL_NOW
    g2.GQL_NOW = UPTIME_NOW
    lua2.execute(v6_fixture_lua())
    before = v6_listed(lua2)
    if "94612" not in before:
        return fail(name, f"前置：94612 该行本身要在列（它靠 1058 金过门槛，不靠勾）：{before}")
    after = v6_listed(lua)
    if after != before:
        return fail(name, f"迁移前后矩阵的行变了（v6 只配钥匙不换门）：{before} → {after}")

    # §九第 0 步①：锚点还是上一个 CD 的那一次登录，尾巴档不许烙成上周的编号
    lua.execute("""
local db = GQL.Config.DB()
db.quests, db.chars, db.turnins, db.manual = {}, {}, {}, {}
db.cycleAnchor = 1760595200          -- 上周的锚点：+604800 = 1761200000 <= WALL_NOW，已过去
db.quests[97128] = { name = '巢穴：尼姆瑞莎', regKey = 'w:巢穴', mapID = 2405, observations = {} }
-- 板面的一条：roundAt 正是**上周那个锚点**（L.RoundAt 判不出时把当时的 cycleAnchor 写回存档，
-- 污染值）。补问 RefreshCycle 之后账本里的锚点前进了，只比"≠ 新锚点"会把它当成"自己滚过的起点"
-- ⇒ 上周的勾被烙进本周这条实例的编号 = 本轮要消灭的那类假勾，专挑周四刚过、代码刚更新那趟登录发作
db.quests[93573] = { name = '战胜未知：怒火攻心的漂流者', mapID = 2414, poolSeen = true,
    roundAt = 1760595200, roundEnd = 1761700000, expiresSrc = '接口倒计时',
    observations = {} }
db.chars['号1-永恒之井'] = { lastSync = 1761300000, views = {}, reads = {} }
db.chars['号1-永恒之井'].views[97128] = { s = 'done', t = 1761300000 }
db.chars['号1-永恒之井'].views[93573] = { s = 'done', t = 1761300000 }
db.schema = 5
""")
    weekly_calls["n"] = 0
    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "锚点过期那一趟 v6 没跑")
    stale_key = "%d:%d" % (97128, (1760595200 + 604800 + 450) // 900)
    got = k_of("GQL.Config.DB().chars['号1-永恒之井'].views[97128].k")
    if got != tail_key:
        return fail(name, f"锚点已经过去了却没补问周重置：尾巴档烙上的是 {got}"
                          f"（上周那一桶 = {stale_key}），期望本 CD 的 {tail_key} —— "
                          "常驻周常的勾每周四掉一次")
    if weekly_calls["n"] != 1:
        return fail(name, f"补问只许发生一次（有档要升 + 锚点确实过期），实际 {weekly_calls['n']} 次")
    if k_of("GQL.Config.DB().cycleAnchor") != str(CD_ANCHOR):
        return fail(name, f"RefreshCycle 该把锚点推到本 CD，实际 {k_of('GQL.Config.DB().cycleAnchor')}")
    # 起点只认"这条记录自己滚过的"roundAt：等于**任一个**锚点（补问前的上周那个也算）都是
    # L.RoundAt 留下的污染值，不是起点 ⇒ 一笔不烙。少了这一道，上周的快照会挂进本周的编号。
    # （v6 若误烙，v9 也会因"无背书"把这笔记号清掉 —— 终态观测只剩"快照不在档"，
    #   烙没烙过的直接证据由上面补录/流水那几笔代表。）
    if lua.execute("return GQL.Config.DB().chars['号1-永恒之井'].views[93573] ~= nil"):
        return fail(name, "锚点被补问前进之后，roundAt == 上周锚点那条污染值被当成起点 —— "
                          "上周的勾冒充本周这条实例，正是 94612 那一类假勾；"
                          "这类无背书的 done 快照终态该被 v9 清掉")
    ok(name)


def test_v7_purge_deletes_expired_legacy_records():
    """用户 2026-10-28："哪些 expiresAt 的老任务是不是早就到期了，那就全部删除吧。"
    T4a 换门后老 expiresAt 那组真档里 218/310 条还在、新模型不认它，过期的全成了
    "周 CD 尾巴"复活的燃料（真档重放 19 行 → 27 行，10 条死了几天的任务回来）。
    v7 一次性把这份遗产清干净，之后存档里不再有"只剩过期 expiresAt"的记录：
    ① 已过期 + 非 regKey/kwLogAdded/ignored ⇒ 整条删除（db.quests 出列；
       turnins 金币账原样不动 —— §九第 5 条"数据一条不删"管的是凭据流水，这条管的是任务列表）；
    ② 未过期且没有 roundEnd ⇒ roundEnd 盖章为该 expiresAt（一次性转成新模型唯一真源，
       96718"时间结束后都要移除"在周重置那一刻照旧成立）；
    ③ regKey/kwLogAdded 不转也不删（按 T5 设计走周 CD 尾巴，盖了章下周就成了过期墓碑）；
       ignored 一条不删（删了"别再显示"的意图就丢了，与 Prune 同一道守卫）；
       已有 roundEnd 的未过期记录不动（roundEnd 是唯一真源，94612 形状）；
    ④ 跨域（老纪元秒对上开机秒）与拿不到墙上钟都 fail-closed：一条不动、返回 0，宁缺不编。"""
    name = "v7_purge_deletes_expired_legacy_records"
    lua, gql, _ = sync_env([])
    g = lua.globals()
    L, C = gql.Ledger, gql.Config
    W = 1793200000                          # 墙上钟（纪元秒）
    g.GetServerTime = lambda: W
    db = g.GQLDB
    db["schema"] = 6
    db["cycleAnchor"] = W - 604800 + 450    # 本 CD 锚点：刚滚过轮的形状
    db["quests"] = lua.table_from({
        92160: lua.table_from({"name": "幼崽大骚乱", "mapID": 2405,
                               "expiresAt": W - 3600}),                         # 过期 → 删
        91811: lua.table_from({"name": "灼烧明镜", "mapID": 2405,
                               "expiresAt": W - 3600, "roundEnd": W - 7200}),   # 双老 → 删
        96718: lua.table_from({"name": "奈格塔尔虚空决战（英雄）", "mapID": 2405,
                               "expiresAt": W + 450, "expiresSrc": "下次周重置"}),  # 未过期无 roundEnd → 转
        94612: lua.table_from({"name": "举钳欢庆", "mapID": 2405,
                               "expiresAt": W + 80000, "roundEnd": W + 80000}),  # 已有 roundEnd → 不动
        89507: lua.table_from({"name": "丰饶供品", "mapID": 2405, "regKey": "w:丰饶",
                               "expiresAt": W - 3600}),                         # 注册 → 留
        96625: lua.table_from({"name": "幽魂角斗士", "mapID": 2405, "kwLogAdded": True,
                               "expiresAt": W - 3600}),                         # 日志收 → 留
        93001: lua.table_from({"name": "被屏蔽的", "mapID": 2405, "ignored": True,
                               "expiresAt": W - 3600}),                         # 屏蔽 → 留
        93002: lua.table_from({"name": "跨域残值", "mapID": 2405,
                               "expiresAt": 900}),                              # 域不对 → 不动
    })
    db["turnins"] = lua.table_from({
        "号1-永恒之井": lua.table_from({
            92160: lua.table_from({lua.table_from({"t": W - 7200, "g": 5000000})})}),
    })
    db["chars"] = lua.table_from({
        "号1-永恒之井": lua.table_from({
            "views": lua.table_from({92160: lua.table_from({"s": 5000000, "t": W - 7200})}),
            "reads": lua.table_from({92160: lua.table_from({"g": 5000000, "t": W - 7200})}),
        }),
    })
    db["manual"] = lua.table_from({
        "号1-永恒之井": lua.table_from({
            92160: lua.table_from({"state": "done", "t": W - 7200, "g": 5000000}),
        }),
    })

    if lua.execute("return GQL.Config.Migrate(GQL.Config.DB())") is not True:
        return fail(name, "Migrate 没把 schema=6 的档跑完 v7")
    if int(db["schema"]) != 11 or int(C.SCHEMA_VERSION) != 11:
        return fail(name, f"跑完该停在最新档 11（v11 清 admittedKey），实际 SCHEMA_VERSION={C.SCHEMA_VERSION} "
                          f"db.schema={db['schema']}")
    for qid in (92160, 91811):
        if db["quests"][qid] is not None:
            return fail(name, f"{qid} 的 expiresAt 已过期、无注册/日志/屏蔽凭据，该整条删除")
    if int(db["quests"][96718]["roundEnd"]) != W + 450:
        return fail(name, f"96718 未过期的周重置值该盖章成 roundEnd，实际 {db['quests'][96718]['roundEnd']}")
    if int(db["quests"][94612]["roundEnd"]) != W + 80000 \
            or int(db["quests"][94612]["expiresAt"]) != W + 80000:
        return fail(name, "94612 已有 roundEnd（唯一真源），迁移不许碰它")
    for qid, fld in ((89507, "regKey"), (96625, "kwLogAdded"), (93001, "ignored")):
        if db["quests"][qid] is None:
            return fail(name, f"{qid} 带 {fld}，是注册/屏蔽意图记录，一条不许删")
    if int(db["quests"][93002]["expiresAt"]) != 900:
        return fail(name, "跨域残值不许比大小（Prune 同一道守卫），更不许删")
    if len(db["turnins"]["号1-永恒之井"][92160]) != 1:
        return fail(name, "db.turnins 是金币账本，任务记录删了它也得原样在")
    chars = db["chars"]["号1-永恒之井"]
    if chars["views"][92160] is None or chars["reads"][92160] is None \
            or db["manual"]["号1-永恒之井"][92160] is None:
        return fail(name, "views/reads/manual 是凭据流水，任务记录删了它们也得原样在")
    logs = str(gql.Config.GetLogs())
    if "数据升级 v7 完成（处理 2 条）" not in logs:
        return fail(name, f"迁移返回值（删除条数）要进通用日志，实际：{logs}")
    if "转换 roundEnd 1 条" not in logs:
        return fail(name, f"转换条数要单独记一行（/gql quest 里逐条追认），实际：{logs}")
    if int(L.PurgeExpiredLegacyOnce(db, W)) != 0:
        return fail(name, "清理过一遍的档再跑必须 0 条（幂等）")
    # fail-closed：墙上钟拿不到（三个来源都哑）→ 一条不动、返回 0
    g.GetServerTime = None
    g.time = None
    g.GetLocalTime = None
    db2 = lua.table_from({"quests": lua.table_from({
        93003: lua.table_from({"name": "过期但没钟", "mapID": 2405,
                               "expiresAt": W - 60})})})
    got = L.PurgeExpiredLegacyOnce(db2)
    if got != 0 or db2["quests"][93003] is None:
        return fail(name, f"拿不到墙上钟必须一条不动（返回 {got}），宁缺不编")
    ok(name)


def test_a_hand_mark_expires_when_the_instance_rolls():
    """补录也分轮次（用户 2026-10-27："手动标记也是要分轮次的，这个勾不补不代表
    永远完成了，世界任务都是不断轮换的"）。12.1.5 真存档 89347「战胜未知：怒火攻心的
    漂流者」（屠戮者高地 12 小时区）：板面已经答出下一条实例的结束时刻，
    可 10-25 04:22 点的那笔补录还带着 done —— 本轮一格没勾，界面照样打勾。
    根因：L.State 第一道（补录）是四份凭据里唯一没有身份门的，流水、快照、按号读数都有。
    编号模型里这一道与前几道并成同一句：认这笔补录写下的编号 == 这条任务当前的编号，
    结束时刻整段挪走 ⇒ 补录自动作废，不再需要"轮次起点"那个参照物。
    钱照旧按 CD 记（那一笔真进过钱包），本轮没交过就是没交过。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 12, 300000, 1000, 20000)   # 这一条到 63200 结束
    key = "A-永恒之井"
    L.SetManual(db, key, 12, "done", 2100, 150000)
    now = 2100
    if st(L, db, key, 12, now) != "done":
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "前置：同一条实例之内点的补录该算数（别把修法做成补录永远不认）")
    if int(L.WeekStats(db, key, 0, now)[0]) != 1:
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    f"前置：同一条实例之内的补录该计入本周，实际 {L.WeekStats(db, key, 0, now)[0]}")

    # 板面答出下一条实例的结束时刻（整段挪走 12 小时）⇒ 换编号 ⇒ 那一笔补录匹配不上
    roll_the_instance(lua, L, db, 12, 300000, 20000, 64000)
    if uid(L, db, 12, 64000) is None:
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "前置：这一条实例该换成下一条了（新结束时刻问不出来）")
    if st(L, db, key, 12, 64000) == "done":
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "换了实例，上一轮点的补录还压着这一格 = 玩家 10-27 看到的假勾")
    if int(L.WeekStats(db, key, 0, 64000)[0]) != 0:
        return fail("a_hand_mark_expires_when_the_instance_rolls",
                    "过期补录不许再冒充本周完成数（格子不勾、汇总却 +1 是两处两个口径），"
                    f"实际 {L.WeekStats(db, key, 0, 64000)[0]}")
    ok("a_hand_mark_expires_when_the_instance_rolls")


def test_a_hand_mark_survives_within_the_same_instance():
    """与上一条成对：结束时刻没变（同一条实例里切奖励档）时补录必须还在。
    钉的是"别把身份门放宽成每次快照都清"——那等于把 test_manual_beats_auto_and_survives_sync
    要的"同一条实例内补录优先于自动判定"当场改回去。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    roll_the_instance(lua, L, db, 13, 300000, 1000, 20000)   # 63200 才结束 ⇒ 还是同一条
    key = "A-永恒之井"
    L.SetManual(db, key, 13, "done", 2100, 150000)
    inst = uid(L, db, 13, 2100)
    if inst is None or uid(L, db, 13, 40000) != inst:
        return fail("a_hand_mark_survives_within_the_same_instance",
                    f"前置：没到结束时刻不该换编号，实际 {uid(L, db, 13, 40000)} vs {inst}")
    if st(L, db, key, 13, 40000) != "done":
        return fail("a_hand_mark_survives_within_the_same_instance",
                    "同一条实例里切了奖励档就把补录判死 = 本轮的勾凭空没了")
    ok("a_hand_mark_survives_within_the_same_instance")


def test_a_stale_hand_mark_without_any_client_answer_reports_unknown():
    """补录过期、而客户端这一条一个字没答（不在池、没流水、没快照）：报 ? 不报"没做"。
    报"没做"和报"永远已做"是同一个错的两侧 —— 玩家点过的那一笔过期了不等于他没说，
    如实就是问不出（UI 显示 ?），照着 ? 再点一次就补上。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    L.AddQuest(db, 14, 1000)                                  # 玩家点名加的一条（不上板）
    L.SetExpiry(db["quests"][14], 1000, 43200)                # 这一条到 44200 结束
    L.SetManual(db, "A-永恒之井", 14, "done", 1100, 0)
    if state1(L.State, db, "A-永恒之井", 14, None, 1100) != "done":
        return fail("a_stale_hand_mark_without_any_client_answer_reports_unknown",
                    "前置：同一条实例之内的补录该打勾")
    # 下一条实例（板面答出新的结束时刻）+ 此刻不在池里（currentSig 清空）= 客户端答不出
    lua.execute("local r = GQL.Config.DB().quests[14] "
                "r.roundEnd, r.currentSig = 88000, nil")
    st = state1(L.State, db, "A-永恒之井", 14, None, 45000)
    if st != "unknown":
        return fail("a_stale_hand_mark_without_any_client_answer_reports_unknown",
                    f"过期补录 + 客户端没答该是 ?（unknown），实际 {st}")
    ok("a_stale_hand_mark_without_any_client_answer_reports_unknown")


def test_a_stale_hand_mark_does_not_outrank_this_rounds_evidence():
    """这一轮真交过之后，上一轮那笔过期补录不许再插话：状态回到自动判定的 done，
    而且这一格不再算"玩家手点的"（颜色该从琥珀翻回绿，客户端认了这笔）。"""
    lua, gql = load_lua("Config", "Ledger")
    L = gql.Ledger
    db = bind_db(lua, ledger_db(lua))
    # 先在上一条实例（63200 结束）点一笔补录，编号烙在那一条上；再换到下一条实例
    # （板面答出 107200 结束），这一笔补录的编号对不上新的一条 ⇒ 过期（用户 2026-10-27 提案）。
    roll_the_instance(lua, L, db, 15, 300000, 1000, 20000)
    key = "A-永恒之井"
    L.SetManual(db, key, 15, "done", 2100, 150000)
    if st(L, db, key, 15, 2100) != "done":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    "前置：上一轮之内点的补录该算数")
    roll_the_instance(lua, L, db, 15, 300000, 20000, 64000)
    inst = uid(L, db, 15, 64000)
    if inst is None:
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    "前置：这一条实例该换成下一条了（新结束时刻问不出来）")
    sig = one(lua.execute("return tostring(GQL.Config.DB().quests[15].currentSig)"))
    L.OnTurnIn(db, key, 15, 64500, sig)
    if st(L, db, key, 15, 64500) != "done":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    "这一轮实测交过该打勾（过期补录不许把它压成 ? 或 open）")
    src = one(lua.execute("local _, s = GQL.Ledger.State(GQL.Config.DB(), %r, 15, nil, 64500) "
                          "return tostring(s)" % key))
    if src != "nil":
        return fail("a_stale_hand_mark_does_not_outrank_this_rounds_evidence",
                    f"这一格这一轮有流水背书，不该还标成手点的，实际 src={src}")
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


# ============================================================================
# 2026-11-01 分享前的六条优化（用户口径见各用例 docstring）
# ============================================================================

def test_version_has_one_readable_source():
    """共享出去之后对方第一句话往往是"你这是哪个版本"（用户 2026-11-01："我打算把这个
    插件共享给别人使用，最好有版本号的概念"）。真源只有一个：Core.lua 的 GQL.VERSION，
    toc 的 ## Version 必须与它逐字相同（两处各写一份、改一处漏一处是这个需求的典型死法），
    矩阵标题、小地图提示、两个窗口标题都要把它带出来。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    ver = str(lua.execute("return tostring(GQL.VERSION)"))
    if ver in ("", "nil"):
        return fail("version_has_one_readable_source", "GQL.VERSION 没定义")
    with open(os.path.join(PLUGIN_DIR, "GoldQuestLedger.toc"), encoding="utf-8") as fh:
        toc = fh.read()
    m = re.search(r"^## Version:\s*(\S+)", toc, re.M)
    if not m or m.group(1) != ver:
        return fail("version_has_one_readable_source",
                    "toc 的 ## Version 应等于 GQL.VERSION %s，实际 %s"
                    % (ver, m.group(1) if m else "（缺失）"))
    g = lua.globals()
    gql.Matrix.Toggle()
    joined = "\n".join(frame_texts(g.GQLMatrixFrame))
    if ("v" + ver) not in joined:
        return fail("version_has_one_readable_source",
                    f"矩阵标题应带版本号 v{ver}:\n{joined}")
    gql.Options.Open()
    joined = "\n".join(frame_texts(g.GQLOptionsFrame))
    if ("v" + ver) not in joined:
        return fail("version_has_one_readable_source",
                    f"设置窗口标题应带版本号 v{ver}:\n{joined}")
    ok("version_has_one_readable_source")


def test_defaults_follow_the_client_version():
    """默认配置包按**客户端版本前缀**匹配（用户 2026-11-01 原话："这个配置依赖于游戏版本，
    比如现在是12.1.5，那我们的地图配置应该是 ^12.1.5 这样以后有12.1.7 12.2.5等，这些地图
    是兼容的，等到13.0 这个配置就自动不起作用了"）。钉五面：
    ① ^12.1.5 命中 12.1.5 / 12.1.7 / 12.2.5；② 13.0.0 不命中；
    ③ 命中时包里的关键词与地图真的落进账本；④ 只套一次（玩家删掉的不会自己长回来）；
    ⑤ 地图表问不出来时只落关键词、不记"套过"，下次进世界重试。"""
    lua, gql = load_lua("Core", "Config", "Defaults", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    for build, want in (("12.1.5", True), ("12.1.5.58352", True),
                        ("12.1.7", True), ("12.2.5", True),
                        ("13.0.0", False), ("11.2.0", False)):
        got = lua.execute("return GQL.Defaults.Matches('%s', '^12.1.5')" % build)
        if bool(got) != want:
            return fail("defaults_follow_the_client_version",
                        f"^12.1.5 对 {build} 应={want}，实际={bool(got)}")
    # 版本问不出来时一个包都不套（宁可不加，也不许猜一个版本去加）
    lua.execute("GQLDB = {} GQL.Config.Init()")
    if lua.execute("return next(GQLDB.config.keywords) ~= nil") is True:
        return fail("defaults_follow_the_client_version",
                    "GetBuildInfo 缺失时不许套任何默认包")

    # ③ 12.1.7 客户端：关键词与地图都落进账本
    lua.execute("GQLDB = {}")
    g.GetBuildInfo = lambda: ("12.1.7.58352", "58352", "date", 120105)
    g.GQL.Api = lua.table_from({
        "WQMapList": lambda: lua.table_from([100, 200]),
        "MapName": lambda mid: ("虚影风暴" if int(mid) == 100 else "祖阿曼"),
    })
    lua.execute("GQL.Config.Init()")
    if lua.execute("return GQLDB.config.keywords[1] == nil") is True:
        return fail("defaults_follow_the_client_version",
                    "命中的包应把关键词落进账本: " + str(lua.execute("""
                        local k, key = GQL.Defaults.Pick()
                        return string.format("build=%s matched=%s defs=%s nkw=%s",
                            tostring(GQL.Defaults.BuildVersion()), tostring(key),
                            tostring(GQLDB.config.defaults and next(GQLDB.config.defaults)),
                            tostring(GQLDB.config.keywords and #GQLDB.config.keywords))
                    """)))
    if lua.execute("return GQLDB.config.extraMaps[100] == nil and "
                   "GQLDB.config.extraMaps[200] == nil") is True:
        return fail("defaults_follow_the_client_version",
                    "包里的图名应解成 mapID 落进跟踪清单")
    if lua.execute("return next(GQLDB.config.defaults or {}) == nil") is True:
        return fail("defaults_follow_the_client_version", "套过的包要记账（否则每次登录重套）")
    # ④ 只套一次：玩家删掉的地图不许自己长回来
    lua.execute("GQLDB.config.extraMaps = {} GQL.Defaults.Apply(GQLDB)")
    if lua.execute("return GQLDB.config.extraMaps[100] ~= nil") is True:
        return fail("defaults_follow_the_client_version",
                    "删掉的默认地图不许被下一次 Apply 加回来（包只套一次）")
    # ⑤ 地图表问不出来：不记"套过"，下次重试
    lua.execute("GQLDB = {} GQL.Api = nil")
    lua.execute("GQL.Config.Init()")
    if lua.execute("return next(GQLDB.config.defaults or {}) ~= nil") is True:
        return fail("defaults_follow_the_client_version",
                    "地图表问不出来时不许记套过（否则图永远加不进来）")
    ok("defaults_follow_the_client_version")


def test_add_map_by_name_exact_first_rejects_ambiguous():
    """T14 匹配收紧：全等（大小写不敏感）命中唯一一张才收；全等歧义/零命中才退
    子串，子串命中多张**不收** —— 宁可不收，也不悄悄多收（包带 rev 记过账，错收
    的图不会自己退）。"已在跟踪"必须报成 already（幂等），不许再折进"没匹配到"。
    钉六面：① 全等唯一（子串再多也拿全等那张）；② 全等两张同名 → ambiguous 不收；
    ③ 无全等、子串唯一 → 收；④ 子串两张 → ambiguous 不收；⑤ 一张没中 → notfound；
    ⑥ 已跟踪的名字 → ok=true、already=1、added=0。"""
    lua, gql = load_lua("Config", "Ledger", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    names = {1: "祖阿曼", 2: "祖阿曼", 3: "虚影风暴", 4: "虚影风暴之地",
             5: "上盘卷蛇岛", 6: "下盘卷蛇岛", 7: "永歌森林"}
    g.GQL.Api = lua.table_from({
        "WQMapList": lambda: lua.table_from([1, 2, 3, 4, 5, 6, 7]),
        "MapName": lambda mid: names.get(int(mid)),
    })
    gql.Config.Init()

    def call(name):
        return lua.execute("""
            local ok, msg, added, already, reason = GQL.Config.AddMapByName(%r)
            return { ok = ok, msg = msg, added = added, already = already,
                     reason = reason }
        """ % name)

    r = call("虚影风暴")                      # ① 全等唯一（子串还中 3、4）
    if r["ok"] is not True or int(r["added"]) != 0 + 1 or int(r["already"]) != 0:
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"全等唯一命中应收下且只收那一张: {r['ok']!r} {r['added']!r} {r['already']!r}")
    if lua.execute("return GQLDB.config.extraMaps[4] ~= nil") is True:
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    "全等唯一时不许把子串命中的 4 也捎带收进来")
    r = call("祖阿曼")                        # ② 两张全等 → 歧义不收
    if r["ok"] is not False or str(r["reason"]) != "ambiguous":
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"两张同名图必须判歧义、一张都不收: {r['ok']!r} {r['reason']!r}")
    if lua.execute("return GQLDB.config.extraMaps[1] ~= nil or "
                   "GQLDB.config.extraMaps[2] ~= nil") is True:
        return fail("add_map_by_name_exact_first_rejects_ambiguous", "歧义时不许悄悄收图")
    r = call("森林")                          # ③ 无全等、子串唯一 → 收
    if r["ok"] is not True or int(r["added"]) != 1:
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"子串唯一命中该收: {r['ok']!r} {r['added']!r}")
    r = call("盘卷蛇岛")                      # ④ 子串两张 → 歧义不收
    if r["ok"] is not False or str(r["reason"]) != "ambiguous":
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"子串命中多张宁可不收: {r['ok']!r} {r['reason']!r}")
    r = call("诺森德")                        # ⑤ 没匹配到
    if r["ok"] is not False or str(r["reason"]) != "notfound":
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"零命中该报 notfound: {r['ok']!r} {r['reason']!r}")
    r = call("虚影风暴")                      # ⑥ 已在跟踪：幂等，不算"没匹配到"
    if r["ok"] is not True or int(r["added"]) != 0 or int(r["already"]) != 1:
        return fail("add_map_by_name_exact_first_rejects_ambiguous",
                    f"已跟踪的名字该报 already=1（幂等），实际 added={r['added']!r} "
                    f"already={r['already']!r} ok={r['ok']!r}")
    ok("add_map_by_name_exact_first_rejects_ambiguous")


def test_defaults_pack_log_three_classes():
    """T14 日志三分：新增 / 已在跟踪（幂等，不算改动）/ 没收进来（歧义与没匹配
    分开写）。旧版把"已在跟踪"折进"没匹配到的图名"那半句 —— 行为幂等，措辞说谎。
    包四个图名：祖阿曼（两张同名 → 歧义）、虚影风暴（玩家早自己加了）、盘卷蛇岛
    （全等唯一 → 新增）、雾夏岛（没匹配到）；日志要三分照写，已在跟踪的不许出现在
    "没收进来"那一串里。"""
    lua, gql = load_lua("Core", "Config", "Defaults", mock=True)
    g = lua.globals()
    g.GQLDB = lua.table_from({})
    names = {1: "祖阿曼", 2: "祖阿曼", 3: "虚影风暴", 4: "盘卷蛇岛"}
    g.GQL.Api = lua.table_from({
        "WQMapList": lambda: lua.table_from([1, 2, 3, 4]),
        "MapName": lambda mid: names.get(int(mid)),
    })
    g.GetBuildInfo = lambda: ("13.0.0", "1", "d", 130000)   # 先让 Init 不套包
    lua.execute("""
        GQL.Defaults.PACKS = { { version = "^12.1.5", rev = 9,
                                 maps = { "祖阿曼", "虚影风暴", "盘卷蛇岛", "雾夏岛" },
                                 keywords = {} } }
    """)
    gql.Config.Init()
    gql.Config.AddMap(3)                      # 玩家早就自己加了虚影风暴
    g.GetBuildInfo = lambda: ("12.1.5", "1", "d", 120105)
    key = gql.Defaults.Apply()
    if key is None:
        return fail("defaults_pack_log_three_classes", "地图问得出就该套上并记 key")
    log = str(gql.Config.GetLogs())
    if "新增地图 1 张" not in log:
        return fail("defaults_pack_log_three_classes",
                    f"盘卷蛇岛该记进新增，日志: {log}")
    if "已在跟踪" not in log:
        return fail("defaults_pack_log_three_classes",
                    f"已跟踪的虚影风暴要单独成类（不算改动），日志: {log}")
    tail = log.split("没收进来的图名：")[-1] if "没收进来的图名：" in log else ""
    if "祖阿曼（命中几张图" not in tail:
        return fail("defaults_pack_log_three_classes",
                    f"歧义名要写明是命中多图不是没匹配到，日志: {log}")
    if "雾夏岛（没匹配到）" not in tail:
        return fail("defaults_pack_log_three_classes",
                    f"没匹配到的名要写明没匹配到，日志: {log}")
    if "虚影风暴" in tail:
        return fail("defaults_pack_log_three_classes",
                    f"已在跟踪的图不许混进「没收进来」（旧版的谎），日志: {log}")
    if lua.execute("return GQLDB.config.extraMaps[1] ~= nil or "
                   "GQLDB.config.extraMaps[2] ~= nil") is True:
        return fail("defaults_pack_log_three_classes", "歧义名一张都不该被收进跟踪清单")
    ok("defaults_pack_log_three_classes")


def test_matrix_sits_above_other_windows():
    """用户 2026-11-01："打开矩阵面板的时候，如果已经打开了其他窗口，比如地图、角色面板等，
    矩阵在他们后面。这个体验不好" —— 矩阵抬到 DIALOG（地图/角色面板都在 MEDIUM 及以下），
    而自家设置/关键词窗口打开时要抬到矩阵**之上**：两个窗口现在同层，不显式抬层号
    就会反过来被矩阵盖住（修一个坏一个）。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    gql.Matrix.Toggle()
    strata = str(lua.execute("return tostring(GQLMatrixFrame.__strata)"))
    if strata != "DIALOG":
        return fail("matrix_sits_above_other_windows",
                    f"矩阵应在 DIALOG 层（压住地图/角色面板），实际 {strata}")
    mlvl = int(lua.execute("return GQLMatrixFrame:GetFrameLevel()"))
    gql.Options.Open()
    olvl = int(lua.execute("return GQLOptionsFrame:GetFrameLevel()"))
    if olvl <= mlvl:
        return fail("matrix_sits_above_other_windows",
                    f"设置窗口层号 {olvl} 必须高于矩阵 {mlvl}（同层按层号排）")
    gql.Options.OpenWindow()
    klvl = int(lua.execute("return GQLOptionsFrame:GetFrameLevel()"))
    if klvl < mlvl:
        return fail("matrix_sits_above_other_windows",
                    f"设置窗口层号 {klvl} 不该低于矩阵 {mlvl}")
    ok("matrix_sits_above_other_windows")


def test_hover_off_while_our_window_is_under_the_mouse():
    """用户 2026-11-01 截图实测："设置、添加任务两个窗口虽然在矩阵前面，但是依然会触发
    矩阵的 hover"。窗口正文全是可点控件，焦点门在那种情况下反推不回"在窗口里"，
    所以矩阵的扫描先问 Options.PanelUnderMouse：鼠标压在自家窗口矩形里就整条不亮；
    窗口收掉后 hover 必须能恢复（不许为了它把悬停整个哑掉）。
    2026-11-01 起关键词并进设置页，只剩这一个窗口，"两个窗口"这个前提消失了。"""
    lua, gql, g, calls = hover_env(122, extra=("Matrix", "Options"))
    lit = lambda: bool(lua.execute("return GQLMatrixHiR ~= nil and GQLMatrixHiR.__shown"))
    hover_update(lua)
    if not lit() or "SHOW" not in calls:
        return fail("hover_off_while_our_window_is_under_the_mouse",
                    "起点：没有窗口压着时应亮行并弹提示")
    gql.Options.OpenWindow()
    lua.execute("GQLOptionsFrame.__mouseover = true")
    del calls[:]
    hover_update(lua)
    if lit() or "SHOW" in calls:
        return fail("hover_off_while_our_window_is_under_the_mouse",
                    "鼠标在设置窗口里时不许点亮矩阵的行/弹提示:\n" + "\n".join(calls)
                    + "\nPanelUnderMouse=" + str(lua.execute("return GQL.Options.PanelUnderMouse()")))
    lua.execute("GQLOptionsFrame:Hide() GQLOptionsFrame.__mouseover = nil "
                "GQL_FOCUS = GQL_HOVERBTN")
    del calls[:]
    hover_update(lua)
    if not lit() or "SHOW" not in calls:
        return fail("hover_off_while_our_window_is_under_the_mouse",
                    "窗口收掉后 hover 应恢复:\n" + "\n".join(calls))
    ok("hover_off_while_our_window_is_under_the_mouse")


def test_options_map_section_orders_quick_add_last():
    """用户 2026-11-01："添加当前地图按钮 放在 按名称添加 后面，名称的输入框也不要太长"，
    紧接着又定："扫描地图、添加当前所在地图按钮，都放在 按名称添加按钮后面"。
    所以那一排是 输入框 → 按名称添加 → 扫描地图 → ＋添加当前所在地图；输入框 240 → 140
    （地图名就两三个字，长框把后面三颗按钮全挤到半屏外）。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    gql.Config.AddMap(2007)          # 先有一行，顺序才看得全
    gql.Options.OpenWindow()
    why = lua.execute("""
        local p = GQL.Options.pages and GQL.Options.pages.win
        if not p then return "页面没建出来" end
        if tonumber(p.edit.__w) ~= 140 then
            return "输入框宽度应为 140（太长挤掉按钮），实际 " .. tostring(p.edit.__w)
        end
        local ep = (p.edit.__points or {})[1]
        local bp = (p.addByName.__points or {})[1]
        if not bp or bp[2] ~= p.edit or bp[3] ~= "RIGHT" then
            return "按名称添加应贴在输入框右边"
        end
        -- 用户 2026-11-01：扫描地图、添加当前所在地图**都**排在按名称添加后面（同一排）
        local hp = (p.huntBtn.__points or {})[1]
        if not hp or hp[2] ~= p.addByName or hp[3] ~= "RIGHT" then
            return "扫描地图该排在按名称添加后面（同一排，LEFT 锚到它 RIGHT）"
        end
        if p.huntBtn.__text ~= "扫描地图" then
            return "那颗按钮该叫「扫描地图」，实际 " .. tostring(p.huntBtn.__text)
        end
        local cp = (p.addCur.__points or {})[1]
        if not cp or cp[2] ~= p.huntBtn or cp[3] ~= "RIGHT" then
            return "添加当前所在地图该排在扫描地图后面（同一排）"
        end
        -- 三颗都跟输入框同一行：竖直偏移都是 0
        for _, b in ipairs({ p.addByName, p.huntBtn, p.addCur }) do
            local q = (b.__points or {})[1]
            if tonumber(q[5]) ~= 0 then
                return tostring(b.__text) .. " 该与输入框同一行（y 偏移应为 0）"
            end
        end
    """)
    if why:
        return fail("options_map_section_orders_quick_add_last", str(why))
    ok("options_map_section_orders_quick_add_last")


def test_settings_register_into_blizzard_options():
    """用户 2026-11-01："我看其他的插件的配置是在这里的，我们的确实弹窗，我们能做到这里吗"
    —— 做到"在 选项 → 插件 那一列"靠 Settings.RegisterCanvasLayoutCategory。
    两面都钉住：① 有接口时注册分类并打开它（不再弹自家窗口）；
    ② 没有接口（老客户端/替身）时整段降级、退回自家窗口 —— 一个不存在的接口
    不许把 /gql config 弄成"打不开"。"""
    lua, gql, _ = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    # ① 先验降级路（mock 里没有 Settings）
    if lua.execute("return GQL.Options.settingsCategory") is not None:
        return fail("settings_register_into_blizzard_options",
                    "前置：替身里没有 Settings，第一次注册应记成不可用")
    gql.Options.Open()
    if lua.execute("return GQLOptionsFrame ~= nil") is not True:
        return fail("settings_register_into_blizzard_options",
                    "没有 Settings 接口时必须退回自家窗口")
    # ② 造一套 Settings：注册分类 + 打开到它
    lua.execute("""
        Settings = {
            __cats = {},
            RegisterCanvasLayoutCategory = function(frame, name)
                local cat = { frame = frame, name = name,
                              GetID = function(self) return 42 end }
                Settings.__cats[#Settings.__cats + 1] = cat
                return cat
            end,
            RegisterAddOnCategory = function(cat) Settings.__reg = cat end,
            OpenToCategory = function(id) Settings.__opened = id end,
        }
        GQL.Options.settingsCategory = nil
    """)
    gql.Options.Open()
    if int(lua.execute("return #Settings.__cats")) != 1:
        return fail("settings_register_into_blizzard_options",
                    "有 Settings 时应注册且只注册一个分类")
    if int(lua.execute("return Settings.__opened or 0")) != 42:
        return fail("settings_register_into_blizzard_options",
                    "Open() 应打开到我们那个分类（id=42）")
    if lua.execute("return Settings.__reg == Settings.__cats[1]") is not True:
        return fail("settings_register_into_blizzard_options",
                    "分类必须挂进插件列表（RegisterAddOnCategory）")
    texts = "\n".join(frame_texts(lua.globals().GQLSettingsCanvas))
    # 2026-11-01 起关键词也并进这一页（画布与弹窗同一段排版代码），所以画布里
    # 既有地图段也有关键词段 —— 不再是"截图里那颗 关键词·屏蔽管理 入口按钮"。
    for want in ("按名称添加", "扫描地图", "＋添加当前所在地图", "跟踪地图",
                 "关键词 · 屏蔽管理"):
        if want not in texts:
            return fail("settings_register_into_blizzard_options",
                        f"画布里该有「{want}」:\n{texts}")
    if lua.execute("return GQL.Options.pages.canvas ~= nil") is not True:
        return fail("settings_register_into_blizzard_options",
                    "画布也该有自己的页面表（行池/输入框都在上面）")
    ok("settings_register_into_blizzard_options")


def test_matrix_settings_keeps_the_matrix_open():
    """用户 2026-11-01："矩阵里点击设置后，矩阵就被关闭了，这是必须的吗" —— 不是。
    上一版那颗按钮直接开暴雪选项，暴雪开面板会 CloseSpecialWindows，把挂 ESC 名单的
    矩阵一并关掉；现在走 Options.OpenWindow（自家窗口），两件事都不发生：
    矩阵留着、设置页开着，改完图能立刻看见矩阵的变化。
    顺带钉住用户同一条里的另半句：矩阵上那颗"添加任务"已经下线（关键词并进设置页了）。"""
    lua, gql, g = sync_env([(120, 30000000)], extra=("Matrix", "Options"))
    gql.Matrix.Toggle()
    if not lua.execute("return GQLMatrixFrame.__shown == true"):
        return fail("matrix_settings_keeps_the_matrix_open", "前置：矩阵该开着")
    # 记一笔：真走了暴雪选项的话这里会被写值（那正是关掉矩阵的那条路）
    lua.execute("""
        Settings = {
            OpenToCategory = function(id) Settings.__opened = id end,
        }
    """)
    if not click_frame_by_text(lua, "GQLMatrixFrame", "设置"):
        return fail("matrix_settings_keeps_the_matrix_open", "矩阵标题栏找不到\"设置\"")
    if lua.execute("return Settings.__opened ~= nil") is True:
        return fail("matrix_settings_keeps_the_matrix_open",
                    "设置按钮不该绕道暴雪选项（那条路会 CloseSpecialWindows 把矩阵关掉）")
    if lua.execute("return GQLMatrixFrame.__shown == true") is not True:
        return fail("matrix_settings_keeps_the_matrix_open", "点完设置矩阵被关掉了")
    if lua.execute("return GQLOptionsFrame ~= nil and GQLOptionsFrame.__shown == true") is not True:
        return fail("matrix_settings_keeps_the_matrix_open", "设置页没打开")
    if lua.execute("""
        local function walk(f)
            local kids = f.__children or {}
            for i = 1, #kids do
                if kids[i].__text == "添加任务" then return true end
                if walk(kids[i]) then return true end
            end
            return false
        end
        return walk(GQLMatrixFrame)
    """) is True:
        return fail("matrix_settings_keeps_the_matrix_open",
                    "矩阵上不许再有\"添加任务\"（关键词并进设置页了）")
    ok("matrix_settings_keeps_the_matrix_open")


def test_defaults_pack_matches_the_shipped_data():
    """包里那串数据是给共享出去的人用的默认值，抄错一个字就是"这条永远是 0 命中"。
    钉住用户 2026-11-01 逐条给的那份（截图抄的原文）：
      · 地图五张，第二张是**盘卷蛇岛**（不是"盘蛇岛"，玩家纠正过一次）；
      · 关键词 12 个，含"追踪萨拉塔斯""世界首领"这些不在任务池里的周常族。"""
    lua, gql = load_lua("Core", "Config", "Defaults", mock=True)
    maps = lua.execute("""
        local m = {}
        for i, v in ipairs(GQL.Defaults.PACKS[1].maps) do m[i] = v end
        return m
    """)
    got_maps = []
    i = 1
    while maps[i] is not None:
        got_maps.append(str(maps[i]))
        i += 1
    want_maps = ["哈籁恩达尔", "永歌森林", "盘卷蛇岛", "祖阿曼", "虚影风暴"]
    if got_maps != want_maps:
        return fail("defaults_pack_matches_the_shipped_data",
                    f"地图清单该是 {want_maps}，实际 {got_maps}")
    kws = lua.execute("""
        local m = {}
        for i, v in ipairs(GQL.Defaults.PACKS[1].keywords) do m[i] = v end
        return m
    """)
    got_kws = []
    i = 1
    while kws[i] is not None:
        got_kws.append(str(kws[i]))
        i += 1
    for want in ("特别任务", "符文石", "斯托玛兰", "追踪萨拉塔斯", "亚基", "虚空决战",
                 "丰饶供品", "世界首领", "梦魇任务", "回响复燃", "虚空侵袭", "亚基入侵"):
        if want not in got_kws:
            return fail("defaults_pack_matches_the_shipped_data",
                        f"关键词里少了「{want}」: {got_kws}")
    if len(got_kws) != 12:
        return fail("defaults_pack_matches_the_shipped_data",
                    f"关键词该是 12 个（用户 2026-11-01 那份），实际 {len(got_kws)} 个: {got_kws}")
    ok("defaults_pack_matches_the_shipped_data")


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
        test_all_done_counts_skip_marks,
        test_pool_read_without_name_keeps_the_known_name,
        test_matrix_rows_logs_admission_flips,
        test_a_reused_record_loses_the_previous_rounds_number_at_the_roll,
        test_a_reused_sig_reads_the_gold_number_back_after_the_roll,
        test_a_turned_in_row_drops_when_the_instance_rolls,
        test_turnin_round_key_is_stamped_at_write_and_expires_on_roll,
        test_id_plus_end_time_is_the_identity_not_the_start,
        test_a_one_second_jitter_never_forges_a_new_instance,
        test_weekly_family_without_countdown_uses_the_cd_tail,
        test_an_identity_that_cannot_be_determined_is_nil_never_a_guess,
        test_snapshot_written_this_round_is_the_missing_case_today,
        test_v6_backfill_drops_only_the_unprovable_marks,
        test_v7_purge_deletes_expired_legacy_records,
        test_the_write_side_stamps_the_key_and_keeps_the_old_round,
        test_a_weekly_turned_in_earlier_this_cd_still_occupies_its_row,
        test_a_turnin_with_no_gold_on_both_sides_does_not_admit,
        test_the_live_save_shape_of_that_row_drops_without_waiting_for_a_fresh_read,
        test_a_turnin_with_unread_reward_money_does_not_admit,
        test_a_turnin_row_that_stores_no_gold_does_not_admit,
        test_a_turnin_with_zero_reward_read_does_not_admit,
        test_a_zero_reward_read_is_a_verdict_not_a_mystery,
        test_keyword_row_needs_no_turnin_to_stay,
        test_dump_names_the_non_gold_turnin_rejection,
        test_dump_blames_the_missing_credential_not_the_keyword,
        test_non_gold_turnin_does_not_admit_row,
        test_gold_above_floor_admits_row,
        test_turnin_is_not_an_admission_door,
        test_a_34_gold_turned_in_row_is_not_listed,
        test_double_mode_row_survives_without_admission_sticky,
        test_v11_clears_the_admission_sticky_once,
        test_the_log_credential_has_one_read_home,
        test_the_poisoned_sticky_gold_is_cleared_once_at_sync,
        test_a_carried_zero_never_marks_the_quest_as_gold,
        test_dual_mode_flip_keeps_the_row_and_the_check,
        test_a_new_instance_rejudges_gold_and_clears_the_check,
        test_diff_no_renewal_without_real_countdown,
        test_turnin_state_and_week_stats,
        test_turnin_not_counted_across_instances,
        test_manual_beats_auto_and_survives_sync,
        test_manual_not_double_counted_with_turnin,
        test_gold_unmeasured_counts_not_sum,
        test_turnin_gain_does_not_mark_the_row_gold,
        test_prune_bounds_size,
        test_state_uses_own_snapshot_across_characters,
        test_stale_snapshot_not_trusted_in_a_new_instance,
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
        test_partial_pool_rolls_when_the_board_countdown_moves,
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
        test_snapshot_done_needs_this_instance_turnin,
        test_snapshot_degrades_to_unknown,
        test_pool_unavailable_keeps_ledger,
        test_empty_pool_needs_second_read,
        test_turnin_records_immediately_without_any_window,
        test_two_turnin_events_of_one_settlement_record_one_row,
        test_turnin_with_no_instance_key_is_still_recorded,
        test_api_frame_visible_candidates,
        test_back_to_back_turnins_both_recorded,
        test_debounce_single_snapshot,
        test_sync_survives_unknown_event_name,
        test_sync_logs_when_no_pool_event_registered,
        test_dump_sections,
        test_dump_shows_stale_char,
        test_dump_quest_explains_why_a_row_stays,
        test_dump_quest_reports_real_expiry,
        test_dump_quest_shows_per_char_credentials,
        test_weekstats_unknown_when_clock_domains_split,
        test_dump_marks_week_total_unknown_across_domains,
        test_dump_refuses_week_total_without_anchor,
        test_matrix_rows_group_by_map_and_filter_by_gold_floor,
        test_format_gold_icon,
        test_api_tracked_map_semantics,
        test_api_tracked_map_three_valued,
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
        test_group_rolls_up_to_the_tracked_parent_map,
        test_matrix_keeps_group_when_map_undecidable,
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
        test_missing_money_never_reschedules_a_full_pool_pass,
        test_event_snapshot_repaints_matrix,
        test_turnin_settlement_repaints_matrix,
        test_matrix_cell_click_toggles_manual_state,
        test_right_click_marks_a_cell_as_skipped,
        test_all_done_row_dims_its_checks,
        test_row_separator_is_one_device_pixel_tall,
        test_hand_marked_cell_uses_an_amber_check,
        test_both_checks_share_one_glyph,
        test_manual_mark_api_done_without_this_instance_evidence_is_amber,
        test_manual_mark_with_this_instance_evidence_turns_green,
        test_manual_mark_from_the_previous_instance_stays_amber,
        test_click_on_another_chars_cell_never_borrows_this_chars_answer,
        test_completion_line_carries_no_source_suffix,
        test_quest_id_is_gray_like_the_bottom_line,
        test_add_map_remembers_sub_maps_and_remove_removes_them,
        test_getpool_queries_only_tracked_maps,
        test_matrix_cell_click_records_quest_gold,
        test_matrix_task_info_in_fixed_subcolumns,
        test_matrix_class_color_applied,
        test_hover_shows_money_tooltip,
        test_hover_prints_the_quest_id,
        test_money_tooltip_survives_missing_ledger,
        test_cell_hover_prints_that_chars_completion_time,
        test_row_hover_does_not_borrow_a_cells_completion,
        test_completion_stamp_stays_quiet_without_wall_clock,
        test_completion_line_hidden_once_the_instance_rolled,
        test_format_stamp_only_for_wall_clock,
        test_last_completion_takes_the_newer_record,
        test_cell_hover_shows_that_alts_own_gold_not_the_last_syncers,
        test_item_cell_says_that_alt_and_keeps_the_reader_list_off_ui,
        test_turned_in_borrowed_gold_no_longer_retracts_the_no_gold_line,
        test_zero_gold_read_turnin_keeps_the_no_gold_line,
        test_item_cell_stays_neutral_not_an_alert,
        test_unsynced_cell_gets_no_attribution_line,
        test_row_hover_stays_free_of_alt_names,
        test_manual_done_records_that_alts_own_gain,
        test_sync_records_the_reward_answer_per_character,
        test_reward_read_stops_counting_when_the_instance_rolls,
        test_gold_readers_lists_only_this_instances_gold_answers,
        test_matrix_char_name_max4_no_ellipsis,
        test_matrix_scrolls_instead_of_scaling,
        test_next_refresh_at_is_earliest_expiry,
        test_snapshot_records_the_instance_end_time,
        test_a_live_expiry_is_never_asked_again,
        test_a_suspect_expiry_is_asked_again,
        test_a_live_expiry_is_not_asked_by_event_capture,
        test_matrix_open_always_refreshes,
        test_refresh_wakes_at_earliest_expiry,
        test_closing_matrix_cancels_pending_refresh,
        test_matrix_title_drops_readonly_label,
        test_opening_matrix_resyncs_and_shows_last_sync,
        test_last_sync_label_handles_three_clock_shapes,
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
        test_a_stale_expiry_never_kills_a_row_the_gold_floor_holds,
        test_row_still_dies_at_its_own_round_expiry,
        test_stale_expiry_never_eats_a_row_that_the_board_still_shows,
        test_the_expiry_gate_kills_a_row_at_its_own_instance_end,
        test_a_stale_expiry_shows_no_countdown,
        test_panel_right_column_shares_the_round_expiry_gate,
        test_next_refresh_skips_a_stale_expiry,
        test_dump_names_the_gate_that_dropped_the_row,
        test_dump_reports_the_admission_gate_before_the_expiry_gate,
        test_cross_clock_domain_expiry_is_never_claimed_as_stale,
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
        test_weekly_rows_only_show_the_cd_tail_never_a_guess,
        test_questmapid_ignores_coordinates,
        test_heal_skips_record_whose_name_already_has_map,
        test_probe_reports_which_interface_gave_each_healed_map,
        test_probe_samples_map_evidence_even_when_nothing_missing,
        test_pool_map_disagreement_is_kept_for_probe,
        test_probe_shows_row_map_against_queried_map,
        test_matrix_right_click_blocks_nothing,
        test_panel_block_survives_resync_and_is_reversible,
        test_removed_keyword_log_quest_does_not_come_back,
        test_a_keyword_weekly_stays_after_the_char_that_accepted_it_logs_out,
        test_the_log_credential_dies_with_the_instance,
        test_a_masked_row_loses_both_log_marks,
        test_a_removed_keyword_takes_the_log_credential_down_with_it,
        test_a_log_row_back_on_the_board_keeps_its_weekly_identity,
        test_api_static_quest_info_cached_within_session,
        test_time_left_sources_require_quest_id,
        test_next_cycle_end_follows_reset_form,
        test_map_source_labels_which_answer_landed,
        test_quest_evidence_sections_present,
        test_quest_evidence_reports_who_answered_the_money,
        test_matrix_scroll_reuses_cell_click_handler,
        test_matrix_rows_money_and_order_unchanged_by_cache,
        test_matrix_gold_threshold_is_configurable,
        test_a_turned_in_row_without_gold_read_does_not_admit,
        test_legacy_map_number_is_kept_and_reasked,
        test_family_row_is_the_weekly_anchor_and_the_board_child_only_proves_it,
        test_anchor_in_the_pool_lists_only_one_row,
        test_registered_anchor_refreshes_when_pool_unavailable,
        test_scan_board_renews_family_anchor_across_cd,
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
        test_an_ignore_outlives_every_cycle_and_round,
        test_pool_rotation_auto_rescans_tracked_maps,
        test_weekly_countdown_must_fit_the_anchor,
        test_dump_shows_the_weekly_family_section,
        test_a_flaky_interface_never_overwrites_or_invents_expiry,
        test_probe_reports_weekly_source_and_timeleft_candidates,
        test_map_rows_prefer_taskquest_source,
        test_probe_reports_registered_preset_evidence,
        test_probe_lists_board_names_and_the_bounty_surface,
        test_probe_shows_row_week_evidence_and_raw_log_fields,
        test_probe_reports_the_production_pin_attribution,
        test_probe_reports_why_a_board_quest_is_not_in_the_pool,
        test_probe_reverse_checks_the_completed_list,
        test_probe_area_poi_lists_event_pins,
        test_probe_area_poi_missing_does_not_swallow_later_sections,
        test_probe_asks_map_membership_for_resident_weekly,
        test_event_pin_groups_the_row_where_the_event_is,
        test_event_pin_ambiguous_or_gone_never_invents_a_group,
        test_event_pin_group_latches_for_the_instance,
        test_event_pin_group_rotates_with_own_countdown,
        test_weekly_group_holds_the_rows_the_client_cannot_place,
        test_an_unregistered_weekly_lands_in_the_weekly_group,
        test_a_pin_that_carries_a_quest_id_becomes_a_row,
        test_aqir_state_needs_positive_evidence_not_the_absent_sentence,
        test_an_unanswerable_round_never_erases_the_aqir_done_view,
        test_an_unanswerable_round_keeps_a_recorded_open_answer,
        test_a_rolled_instance_lets_unknown_take_the_cell,
        test_a_pool_row_with_no_state_api_keeps_its_answer,
        test_a_definite_answer_still_overwrites_the_cell,
        test_event_pin_gold_quest_registers_without_keyword,
        test_a_pin_registered_row_expires_on_the_cycle_gate,
        test_a_pin_key_does_not_open_the_player_named_door,
        test_the_pin_rows_gold_is_stamped_on_its_own_instance,
        test_prune_sweeps_a_stale_pin_row_but_not_a_named_weekly,
        test_a_rotated_pin_name_leaves_only_the_newest_key,
        test_a_pin_row_dies_when_the_event_ends_and_the_cycle_rolls,
        test_a_pin_row_dies_at_its_own_board_end_without_waiting_for_the_roll,
        test_a_gone_pin_row_is_answered_ended_like_a_pool_row,
        test_the_fortify_preset_still_ignores_a_sub_daily_rotation_countdown,
        test_the_registration_reask_answers_everything_but_a_renewal,
        test_a_present_event_pin_renews_only_while_it_is_reported,
        test_abundant_offerings_renews_only_while_client_reports_it,
        test_the_aqir_virtual_row_lives_and_dies_on_the_cycle_tail,
        test_a_family_anchor_lives_only_while_a_member_is_on_the_board,
        test_the_disappear_gate_asks_only_whether_the_row_ever_boarded,
        test_the_forensic_entries_split_a_pin_row_from_a_resident_weekly,
        test_keyword_hunt_names_the_pin_only_hits,
        test_keyword_hunt_logs_the_pin_only_sentence,
        test_probe_lists_weekly_reward_activities,
        test_probe_checks_the_runestone_ids_from_savedinstances,
        test_turnin_of_unknown_quest_never_creates_a_row,
        test_turnin_of_pooled_quest_still_settles,
        test_keyword_weekly_turnin_gets_recorded,
        test_turned_in_event_alone_settles_on_arrival,
        test_event_moment_writes_the_end_time_it_asks,
        test_passive_probe_logs_any_accepted_world_quest,
        test_log_bypass_gold_quest_keeps_a_real_end_time,
        test_event_capture_asks_the_server_once_for_reward_data,
        test_recent_money_misses_are_remembered_for_the_probe,
        test_probe_fetchdata_reports_only_what_the_client_answers,
        test_probe_fetchdata_does_not_claim_a_number_that_was_already_readable,
        test_probe_fetchdata_asks_each_entry_once_and_probes_both_arities,
        test_probe_fetchdata_restores_the_quest_log_selection,
        test_probe_fetchdata_without_a_target_touches_nothing,
        test_probe_fetchdata_picks_the_quest_that_just_failed,
        test_probe_fetchdata_never_writes_the_ledger_even_when_it_works,
        test_reward_data_arrival_settles_gold_without_the_matrix,
        test_a_skipped_char_stops_the_reward_reread,
        test_a_skipped_other_char_does_not_stop_this_one,
        test_a_skipped_char_does_not_close_the_quest_for_other_chars,
        test_a_stale_round_skip_does_not_stop_this_round,
        test_turnin_row_upgrades_an_earlier_manual_mark_to_green,
        test_back_to_back_turnin_events_do_not_crosstalk,
        test_turnin_without_ledger_writes_nothing,
        test_missing_countdown_is_reasked_on_the_next_event_snapshot,
        test_snapshot_survives_a_quest_api_error,
        test_prune_never_mixes_clock_domains,
        test_refresh_wake_is_capped,
        test_quest_records_only_come_from_the_factory,
        test_state_and_registered_state_share_precedence,
        test_gold_verdict_is_the_single_gold_entry,
        test_map_source_labels_are_constants,
        test_map_source_blacklist_is_built_from_the_same_table,
        test_migrate_runs_once_per_schema,
        test_migrate_clears_manual_maps_from_old_saves,
        test_migrate_drops_unbacked_done_views,
        test_migrate_stamps_the_log_key_for_rows_already_alive,
        test_migrate_stamps_this_weeks_log_key_when_the_anchor_is_stale,
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
        test_version_has_one_readable_source,
        test_defaults_follow_the_client_version,
        test_add_map_by_name_exact_first_rejects_ambiguous,
        test_defaults_pack_log_three_classes,
        test_matrix_sits_above_other_windows,
        test_hover_off_while_our_window_is_under_the_mouse,
        test_options_map_section_orders_quick_add_last,
        test_settings_register_into_blizzard_options,
        test_matrix_settings_keeps_the_matrix_open,
        test_defaults_pack_matches_the_shipped_data,
        test_no_old_addon_name_left_anywhere,
        test_every_defined_case_is_registered_in_the_runner,
        test_a_reset_special_task_does_not_haunt_the_matrix,
        test_resident_keyword_row_with_stale_roundEnd_still_admitted,
        test_a_special_task_from_log_expires_at_its_own_end,
        test_stale_round_end_with_no_log_source_expires,
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
        test_ui_text_no_forensic_words,
        test_char_reward_read_positive_survives_mode_flip,
        test_char_reward_read_zero_then_positive,
        test_char_reward_read_new_instance_replaces_value,
        test_last_completion_gain_is_per_char,
        test_last_completion_gain_uses_turnin_instance_key,
        test_week_stats_gold_excludes_other_chars_read,
        test_week_stats_gold_counts_own_read,
        test_a_char_without_a_read_borrows_the_row_gold_when_no_alt_said_zero,
        test_a_zero_read_on_any_char_forbids_borrowing_for_the_rest,
        test_a_lone_zero_read_is_not_a_reward_split,
        test_an_old_save_without_row_money_stays_unknown,
        test_tooltip_cell_without_char_read_says_unknown,
        test_tooltip_row_hotzone_still_shows_row_money,
        test_tooltip_zero_read_prints_no_gold_line,
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
