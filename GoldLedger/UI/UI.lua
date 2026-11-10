--[[
    GoldLedger: UI.lua
    Core UI module: helpers, GoldFormatter, minimap button, public API
    Sub-modules: UI_MainFrame, UI_Popups, UI_Settings
]]

local ADDON_NAME, ns = ...
local GoldLedger = ns.GoldLedger
local L = ns.L

local UI = {}
GoldLedger:RegisterModule("UI", UI)

-------------------------------------------------------------------------------
-- Theme helpers (must be before any UI creation functions)
-------------------------------------------------------------------------------

local function T(key)
    local Themes = GoldLedger:GetModule("Themes")
    return Themes:GetColor(key)
end

local function TC(key)
    local Themes = GoldLedger:GetModule("Themes")
    return Themes:C(key)
end

local function GetSourceColors()
    local Themes = GoldLedger:GetModule("Themes")
    return Themes:GetSourceColors()
end

local function GetBackdrop()
    local Themes = GoldLedger:GetModule("Themes")
    return {
        bgFile = Themes:GetBgTexture(),
        edgeFile = Themes:GetEdgeTexture(),
        tile = true, tileSize = 16, edgeSize = 16,
        insets = { left = 4, right = 4, top = 4, bottom = 4 },
    }
end

local function GetSmallBackdrop()
    local Themes = GoldLedger:GetModule("Themes")
    return {
        bgFile = Themes:GetBgTexture(),
        edgeFile = Themes:GetEdgeTexture(),
        tile = true, tileSize = 8, edgeSize = 8,
        insets = { left = 2, right = 2, top = 2, bottom = 2 },
    }
end

local function SnapToMain(frame)
    local main = GoldLedgerMainFrame
    if not main then return end
    frame:ClearAllPoints()
    frame:SetPoint("TOPLEFT", main, "TOPRIGHT", 8, 0)
end

-------------------------------------------------------------------------------
-- UI creation helpers
-------------------------------------------------------------------------------

local function MakeSeparator(parent, yOffset)
    local sep = parent:CreateTexture(nil, "ARTWORK")
    sep:SetHeight(1)
    sep:SetPoint("TOPLEFT", parent, "TOPLEFT", 12, yOffset)
    sep:SetPoint("TOPRIGHT", parent, "TOPRIGHT", -12, yOffset)
    sep:SetColorTexture(TC("SEPARATOR"))
    return sep
end

-------------------------------------------------------------------------------
-- Strategy Pattern: Gold Formatting
-------------------------------------------------------------------------------
local GoldFormatter = {}

function GoldFormatter.Full(copper)
    copper = math.abs(copper or 0)
    local gold = math.floor(copper / 10000)
    local silver = math.floor((copper % 10000) / 100)
    local cop = copper % 100

    local parts = {}
    if gold > 0 then
        local goldStr = tostring(gold):reverse():gsub("(%d%d%d)", "%1,"):reverse():gsub("^,", "")
        table.insert(parts, goldStr .. L["GOLD_ABBR"])
    end
    if silver > 0 then
        table.insert(parts, silver .. L["SILVER_ABBR"])
    end
    if cop > 0 or #parts == 0 then
        table.insert(parts, cop .. L["COPPER_ABBR"])
    end
    return table.concat(parts, " ")
end

function GoldFormatter.Short(copper)
    copper = math.abs(copper or 0)
    local gold = copper / 10000
    if gold >= 1 then
        return ("%.1f%s"):format(gold, L["GOLD_ABBR"])
    else
        return ("%.0f%s"):format(copper / 100, L["SILVER_ABBR"])
    end
end

-- Compact format for fixed-width stat cards (Today/Yesterday/Session/Month).
-- Full() can be 18+ chars for a rich player and overlaps the row label; this
-- caps width with k/M/B suffixes so value and label never merge.
function GoldFormatter.Abbrev(copper)
    copper = math.abs(copper or 0)
    local gold = copper / 10000
    if gold >= 1e9 then
        return ("%.1fB"):format(gold / 1e9) .. L["GOLD_ABBR"]
    elseif gold >= 1e6 then
        return ("%.1fM"):format(gold / 1e6) .. L["GOLD_ABBR"]
    elseif gold >= 1e3 then
        return ("%.1fk"):format(gold / 1e3) .. L["GOLD_ABBR"]
    elseif gold >= 1 then
        return ("%d"):format(math.floor(gold)) .. L["GOLD_ABBR"]
    elseif copper >= 100 then
        return ("%d"):format(math.floor(copper / 100)) .. L["SILVER_ABBR"]
    else
        return "0" .. L["GOLD_ABBR"]
    end
end

function GoldFormatter.Colored(copper, entryType)
    local text = GoldFormatter.Full(copper)
    local Themes = GoldLedger:GetModule("Themes")
    local c
    if entryType == "income" then
        c = Themes:GetColor("INCOME")
        return ("|cff%02x%02x%02x+%s|r"):format(c[1]*255, c[2]*255, c[3]*255, text)
    else
        c = Themes:GetColor("EXPENSE")
        return ("|cff%02x%02x%02x-%s|r"):format(c[1]*255, c[2]*255, c[3]*255, text)
    end
end

UI.GoldFormatter = GoldFormatter

-------------------------------------------------------------------------------
-- BringToFront: raise frame AND all descendants above other popups
-- (WoW Raise() doesn't affect children — they keep their original frame levels)
-------------------------------------------------------------------------------
-- Global counter for popup z-order. Each BringToFront bumps by a big step (20)
-- so that native template children (with internal level offsets) stay above
-- the previous popup's children.
local _popupLevelCounter = 100

local function RaiseAll(frame)
    if not frame then return end
    -- Bump counter and assign a fresh high level so native children (which
    -- have absolute level offsets) cannot bleed under the previous popup.
    _popupLevelCounter = _popupLevelCounter + 20
    if _popupLevelCounter > 9000 then _popupLevelCounter = 100 end  -- wrap

    if frame.SetFrameLevel then
        frame:SetFrameLevel(_popupLevelCounter)
    end
    -- Raise direct children so they inherit above-siblings position too
    if frame.GetChildren then
        for _, child in ipairs({ frame:GetChildren() }) do
            if child and child.SetFrameLevel then
                child:SetFrameLevel(_popupLevelCounter + 1)
            end
            if child and child.GetChildren then
                for _, gc in ipairs({ child:GetChildren() }) do
                    if gc and gc.SetFrameLevel then
                        gc:SetFrameLevel(_popupLevelCounter + 2)
                    end
                end
            end
        end
    end
end

UI.BringToFront = RaiseAll

-------------------------------------------------------------------------------
-- UI.Colors — public token namespace.
-- Reads current theme colors dynamically so theme switching works transparently.
-- Maps friendly token names to internal Themes keys.
-------------------------------------------------------------------------------
local colorKeyMap = {
    Income      = "INCOME",
    Expense     = "EXPENSE",
    Gold        = "GOLD_TEXT",
    Label       = "LABEL",
    TextWhite   = "TEXT_WHITE",
    TextDim     = "TEXT_DIM",
    TextHint    = "TEXT_HINT",
    BalancePos  = "BALANCE_POS",
    BalanceNeg  = "BALANCE_NEG",
    FrameBg     = "FRAME_BG",
    Border      = "BORDER",
    TitleBg     = "TITLE_BG",
    CardBg      = "CARD_BG",
    CardBorder  = "CARD_BORDER",
    CardTitle   = "CARD_TITLE",
    ButtonBg    = "BTN_BG",
    ButtonHover = "BTN_BG_HOVER",
    ButtonBorder = "BTN_BORDER",
    ButtonActive = "BTN_ACTIVE",
    AccentBtn   = "ACCENT_BTN_BG",
    RowAlt      = "ROW_ALT",
    ScrollBg    = "SCROLL_BG",
}

UI.Colors = setmetatable({}, {
    __index = function(_, key)
        local themeKey = colorKeyMap[key] or key
        local Themes = GoldLedger:GetModule("Themes")
        if Themes and Themes.GetColor then
            return Themes:GetColor(themeKey)
        end
        return { 1, 1, 1, 1 }
    end
})

--- Creates a card container with title (top-left label).
--- @param parent Frame
--- @param opts { title, width, height }
--- @return Frame card with .titleText FontString
function UI:CreateCard(parent, opts)
    opts = opts or {}
    -- Native Blizzard sunken inset panel (used inside BasicFrameTemplate frames)
    local card = CreateFrame("Frame", nil, parent, "InsetFrameTemplate")
    card:SetSize(opts.width or 200, opts.height or 100)

    local cardTitle = card:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    cardTitle:SetPoint("TOPLEFT", card, "TOPLEFT", 8, -6)
    cardTitle:SetText(opts.title or "")
    cardTitle:SetTextColor(1, 0.82, 0)  -- gold (native heading color)

    card.titleText = cardTitle
    return card
end

--- Creates a label/value row inside a card (label left, value right).
--- @param parent Frame card frame
--- @param opts { label, yOffset }
--- @return table row { label, value, SetValue(text, r, g, b) }
function UI:CreateCardRow(parent, opts)
    opts = opts or {}
    local row = {}

    row.label = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.label:SetPoint("TOPLEFT", parent, "TOPLEFT", 10, opts.yOffset or 0)
    row.label:SetText(opts.label or "")
    row.label:SetTextColor(TC("LABEL"))

    row.value = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    row.value:SetPoint("TOPRIGHT", parent, "TOPRIGHT", -8, opts.yOffset or 0)
    row.value:SetJustifyH("RIGHT")

    function row:SetValue(text, r, g, b)
        self.value:SetText(text)
        if r then self.value:SetTextColor(r, g, b) end
    end

    return row
end

-------------------------------------------------------------------------------
-- UI API primitives: CreatePopup, CreateButton, CreateEditBox
-- Feature modules should use these instead of CreateFrame/SetBackdrop/etc.
-------------------------------------------------------------------------------

--- Creates a standardized popup window (title bar, close button, drag, backdrop, ESC-close).
--- @param opts table { name, title, width, height, strata, movable, specialFrame, onClose, parent }
--- @return Frame popup frame with .titleBar, .title, .closeBtn fields
function UI:CreatePopup(opts)
    opts = opts or {}
    local name        = opts.name
    local title       = opts.title or ""
    local width       = opts.width or 400
    local height      = opts.height or 300
    local strata      = opts.strata or "FULLSCREEN_DIALOG"
    local movable     = opts.movable ~= false
    local specialFrame = opts.specialFrame ~= false
    local parent      = opts.parent or UIParent

    -- Native WoW frame with Blizzard's built-in title bar + close button + inset
    local f = CreateFrame("Frame", name, parent, "BasicFrameTemplateWithInset")
    f:SetSize(width, height)
    f:SetPoint("CENTER", parent, "CENTER", 0, 0)
    if movable then
        f:SetMovable(true)
        f:EnableMouse(true)
        f:RegisterForDrag("LeftButton")
        f:SetScript("OnDragStart", f.StartMoving)
        f:SetScript("OnDragStop", f.StopMovingOrSizing)
    end
    f:SetFrameStrata(strata)
    f:SetClampedToScreen(true)

    -- Native template provides TitleText (FontString), TitleBg (Texture), CloseButton (Button)
    if f.TitleText then f.TitleText:SetText(title) end

    if f.CloseButton then
        f.CloseButton:SetScript("OnClick", function()
            if opts.onClose then opts.onClose(f) end
            f:Hide()
        end)
    end

    -- Create a proper Frame to serve as titleBar (TitleBg is a Texture — cannot parent buttons to it).
    -- This frame sits over the native title strip so modules can anchor header buttons here.
    local titleBar = CreateFrame("Frame", nil, f)
    titleBar:SetHeight(22)
    titleBar:SetPoint("TOPLEFT", f, "TOPLEFT", 4, -4)
    titleBar:SetPoint("TOPRIGHT", f, "TOPRIGHT", -4, -4)

    -- ESC-закрытие. Попапы пересоздаются (смена языка) — имя добавляем один раз
    if specialFrame and name and UISpecialFrames then
        local registered = false
        for _, n in ipairs(UISpecialFrames) do
            if n == name then registered = true break end
        end
        if not registered then table.insert(UISpecialFrames, name) end
    end

    f:HookScript("OnShow", function(self)
        UI.BringToFront(self)
        local Anim = ns.UI_Animations
        if Anim and Anim.FadeIn then Anim.FadeIn(self, 0.2) end
    end)

    -- SDK contract fields (preserved for modules using f.titleBar / f.title / f.closeBtn)
    f.titleBar = titleBar                 -- proper Frame, safe as parent for buttons
    f.title    = f.TitleText              -- native template's title FontString
    f.closeBtn = f.CloseButton             -- native template's CloseButton

    f:Hide()
    return f
end

--- Creates a standardized button (backdrop, hover, click).
--- @param parent Frame
--- @param opts table { label, width, height, onClick, onEnter, onLeave, tooltip }
--- @return Button
function UI:CreateButton(parent, opts)
    opts = opts or {}
    -- Native Blizzard button with gold border, hover, press states built-in
    local btn = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    -- Opt out of UIPanelButton's fit-to-text auto-sizing so our SetSize is respected
    btn.fitTextCanWidthDecrease = false
    btn.fitTextWidthPadding = 0
    btn:SetSize(opts.width or 80, opts.height or 22)
    btn:SetText(opts.label or "")
    btn.text = btn:GetFontString()

    if opts.onClick then
        btn:SetScript("OnClick", opts.onClick)
    end

    if opts.tooltip or opts.onEnter or opts.onLeave then
        btn:SetScript("OnEnter", function(self)
            if opts.tooltip then
                GameTooltip:SetOwner(self, "ANCHOR_TOP")
                GameTooltip:AddLine(opts.tooltip, 1, 1, 1)
                GameTooltip:Show()
            end
            if opts.onEnter then opts.onEnter(self) end
        end)
        btn:SetScript("OnLeave", function(self)
            if opts.tooltip then GameTooltip:Hide() end
            if opts.onLeave then opts.onLeave(self) end
        end)
    end

    --- Public SDK method: toggle active highlight (replaces btn:SetBackdropColor hacks).
    --- Implementation can use native LockHighlight or custom backdrop — module doesn't care.
    function btn:SetActive(active)
        if active then
            if self.LockHighlight then self:LockHighlight() end
            if self.text and self.text.SetTextColor then self.text:SetTextColor(1, 0.82, 0) end
        else
            if self.UnlockHighlight then self:UnlockHighlight() end
            if self.text and self.text.SetTextColor then self.text:SetTextColor(1, 1, 1) end
        end
    end

    return btn
end

--- Creates a standardized label (FontString).
--- @param parent Frame
--- @param opts table { text, color ("LABEL"|"INCOME"|"EXPENSE"|rgb), font, anchor={point, relTo, relPoint, x, y}, width }
--- @return FontString
function UI:CreateLabel(parent, opts)
    opts = opts or {}
    local lbl = parent:CreateFontString(nil, "OVERLAY", opts.font or "GameFontHighlightSmall")
    if opts.text then lbl:SetText(opts.text) end
    if opts.width then lbl:SetWidth(opts.width) end
    if opts.justify then lbl:SetJustifyH(opts.justify) end
    -- Color: can be string key (looked up via TC) or {r,g,b}
    if opts.color then
        if type(opts.color) == "string" then
            lbl:SetTextColor(TC(opts.color))
        elseif type(opts.color) == "table" then
            lbl:SetTextColor(opts.color[1], opts.color[2], opts.color[3], opts.color[4] or 1)
        end
    end
    -- Anchor shortcut
    if opts.anchor then
        local a = opts.anchor
        lbl:SetPoint(a.point or "TOPLEFT", a.relTo or parent, a.relPoint or a.point or "TOPLEFT", a.x or 0, a.y or 0)
    end
    return lbl
end

--- Creates a list row (Frame anchored to parent top, optionally with alt-bg color).
--- @param parent Frame scrollChild or similar
--- @param opts table { index, height, altBg ("ROW_ALT"|rgb) — applied if index % 2 == 0 }
--- @return Frame row frame
function UI:CreateListRow(parent, opts)
    opts = opts or {}
    local index = opts.index or 1
    local height = opts.height or 20
    local row = CreateFrame("Frame", nil, parent)
    row:SetHeight(height)
    row:SetPoint("TOPLEFT", parent, "TOPLEFT", 0, -((index - 1) * height))
    row:SetPoint("TOPRIGHT", parent, "TOPRIGHT", 0, -((index - 1) * height))

    if opts.altBg and index % 2 == 0 then
        local bg = row:CreateTexture(nil, "BACKGROUND")
        bg:SetAllPoints()
        if type(opts.altBg) == "string" then
            bg:SetColorTexture(TC(opts.altBg))
        else
            bg:SetColorTexture(opts.altBg[1], opts.altBg[2], opts.altBg[3], opts.altBg[4] or 1)
        end
    end
    return row
end

--- Creates a scroll frame with a scroll child. Returns frame with .child field.
--- @param parent Frame
--- @param opts table { width, height, childWidth, bgColor }
--- @return ScrollFrame scrollFrame with .child (scroll content frame)
function UI:CreateScrollFrame(parent, opts)
    opts = opts or {}
    local scrollFrame = CreateFrame("ScrollFrame", nil, parent, "UIPanelScrollFrameTemplate")
    if opts.width and opts.height then
        scrollFrame:SetSize(opts.width, opts.height)
    end

    if opts.bgColor then
        local bg = parent:CreateTexture(nil, "BACKGROUND", nil, -1)
        bg:SetPoint("TOPLEFT", scrollFrame, "TOPLEFT", -2, 2)
        bg:SetPoint("BOTTOMRIGHT", scrollFrame, "BOTTOMRIGHT", 20, -2)
        bg:SetColorTexture(TC(opts.bgColor))
        scrollFrame.bg = bg
    end

    local child = CreateFrame("Frame", nil, scrollFrame)
    child:SetWidth(opts.childWidth or (opts.width and opts.width - 20) or 400)
    child:SetHeight(1)
    scrollFrame:SetScrollChild(child)

    scrollFrame.child = child
    return scrollFrame
end

--- Creates a progress bar with background + fill + optional label.
--- @param parent Frame
--- @param opts { width, height, bgColor, fillColor }
--- @return Frame bar { fill, SetProgress(0..1), GetProgress() }
function UI:CreateProgressBar(parent, opts)
    opts = opts or {}
    -- Native WoW StatusBar widget (built-in smooth progress with texture)
    local bar = CreateFrame("StatusBar", nil, parent, "TextStatusBar")
    bar:SetSize(opts.width or 200, opts.height or 20)
    bar:SetStatusBarTexture("Interface\\TargetingFrame\\UI-StatusBar")

    if opts.fillColor then
        bar:SetStatusBarColor(opts.fillColor[1], opts.fillColor[2], opts.fillColor[3], opts.fillColor[4] or 1)
    else
        bar:SetStatusBarColor(TC("GOAL_FILL"))
    end

    bar:SetMinMaxValues(0, 1)
    bar:SetValue(0)

    -- Dark bg behind the status bar
    local bg = bar:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints()
    if opts.bgColor then
        bg:SetColorTexture(opts.bgColor[1], opts.bgColor[2], opts.bgColor[3], opts.bgColor[4] or 1)
    else
        bg:SetColorTexture(0.05, 0.05, 0.08, 0.8)
    end
    bar.bg = bg

    -- SDK contract: keep .fill reference for legacy callers
    bar.fill = bar:GetStatusBarTexture()

    function bar:SetProgress(value)
        if value < 0 then value = 0 end
        if value > 1 then value = 1 end
        self:SetValue(value)
        self._progress = value
    end
    function bar:GetProgress() return self._progress or 0 end

    return bar
end

--- Creates a simple bar chart with income/expense pairs.
--- @param parent Frame
--- @param opts { width, height, barWidth, gap }
--- @return Frame chart with :SetData({ {label, income, expense}, ... })
function UI:CreateChart(parent, opts)
    opts = opts or {}
    local chart = CreateFrame("Frame", nil, parent)
    chart:SetSize(opts.width or 400, opts.height or 100)

    chart._bars = {}
    chart._barWidth = opts.barWidth or 14
    chart._gap = opts.gap or 4

    function chart:SetData(data)
        self._data = data or {}
        -- Hide old bars
        for _, pair in ipairs(self._bars) do
            if pair.income then pair.income:Hide() end
            if pair.expense then pair.expense:Hide() end
            if pair.label then pair.label:Hide() end
        end
        -- Find max for scaling
        local maxVal = 1
        for _, d in ipairs(self._data) do
            if (d.income or 0) > maxVal then maxVal = d.income end
            if (d.expense or 0) > maxVal then maxVal = d.expense end
        end
        self._maxVal = maxVal

        local h = self:GetHeight()
        for i, d in ipairs(self._data) do
            local pair = self._bars[i] or {}
            if not pair.income then
                pair.income = self:CreateTexture(nil, "ARTWORK")
                pair.expense = self:CreateTexture(nil, "ARTWORK")
                pair.label = self:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
                self._bars[i] = pair
            end
            local x = (i - 1) * (self._barWidth * 2 + self._gap)
            local incomeH = math.max(1, ((d.income or 0) / maxVal) * h)
            local expenseH = math.max(1, ((d.expense or 0) / maxVal) * h)

            pair.income:SetSize(self._barWidth, incomeH)
            pair.income:SetPoint("BOTTOMLEFT", self, "BOTTOMLEFT", x, 0)
            local ic = TC and { TC("INCOME") } or { 0, 1, 0, 1 }
            pair.income:SetColorTexture(ic[1], ic[2], ic[3], 0.9)
            pair.income:Show()

            pair.expense:SetSize(self._barWidth, expenseH)
            pair.expense:SetPoint("BOTTOMLEFT", self, "BOTTOMLEFT", x + self._barWidth, 0)
            local ec = TC and { TC("EXPENSE") } or { 1, 0, 0, 1 }
            pair.expense:SetColorTexture(ec[1], ec[2], ec[3], 0.9)
            pair.expense:Show()

            pair.label:SetText(d.label or "")
            pair.label:SetPoint("TOP", pair.income, "BOTTOM", self._barWidth / 2, -2)
            pair.label:Show()
        end
    end

    return chart
end

--- Creates a horizontal filter bar — row of colored toggle buttons (only one active).
--- @param parent Frame
--- @param opts { options = { {key,label,color}, ... }, onSelect(key), initial, x, y }
--- @return table bar { buttons, activeKey, SetActive(key) }
function UI:CreateFilterBar(parent, opts)
    opts = opts or {}
    local options = opts.options or {}
    local bar = { buttons = {}, activeKey = opts.initial or (options[1] and options[1].key) }

    local function apply()
        for _, btn in ipairs(bar.buttons) do
            if btn.key == bar.activeKey then
                if btn.LockHighlight then btn:LockHighlight() end
                if btn.text and btn.text.SetTextColor then
                    btn.text:SetTextColor(btn.color[1], btn.color[2], btn.color[3])
                end
            else
                if btn.UnlockHighlight then btn:UnlockHighlight() end
                if btn.text and btn.text.SetTextColor then
                    btn.text:SetTextColor(0.6, 0.6, 0.6)  -- inactive: dim
                end
            end
        end
    end

    local xOff = opts.x or 0
    for _, info in ipairs(options) do
        -- compute width from text length (UTF-8 aware)
        local lbl = info.label or info.key or ""
        local charLen = strlenutf8 and strlenutf8(lbl) or #lbl
        local width = math.max(40, charLen * 9 + 14)

        local btn = self:CreateButton(parent, {
            label = lbl, width = width, height = 20,
            onClick = function()
                bar.activeKey = info.key
                apply()
                if opts.onSelect then opts.onSelect(info.key) end
            end,
        })
        btn:SetPoint("TOPLEFT", parent, "TOPLEFT", xOff, opts.y or 0)

        btn.key = info.key
        btn.color = info.color or { 1, 1, 1 }

        bar.buttons[#bar.buttons + 1] = btn
        xOff = xOff + btn:GetWidth() + 3
    end

    function bar:SetActive(key)
        self.activeKey = key
        apply()
    end

    apply()
    return bar
end

--- Shows a tooltip anchored to owner with given lines.
--- @param owner Frame
--- @param lines table { { text, r, g, b } or { left, right, lr, lg, lb, rr, rg, rb } }
--- @param anchor string default "ANCHOR_TOP"
function UI:ShowTooltip(owner, lines, anchor)
    if not GameTooltip or not owner then return end
    GameTooltip:SetOwner(owner, anchor or "ANCHOR_TOP")
    for _, line in ipairs(lines or {}) do
        if line.left and line.right then
            GameTooltip:AddDoubleLine(line.left, line.right,
                line.lr or 1, line.lg or 1, line.lb or 1,
                line.rr or 1, line.rg or 1, line.rb or 1)
        else
            GameTooltip:AddLine(line.text or "", line.r or 1, line.g or 1, line.b or 1)
        end
    end
    GameTooltip:Show()
end

--- Hides the game tooltip.
function UI:HideTooltip()
    if GameTooltip and GameTooltip.Hide then GameTooltip:Hide() end
end

--- Creates a tab group — horizontal row of tab buttons with active-highlight.
--- @param parent Frame
--- @param opts { tabs = { {key,label}, ... }, onSelect(key), initial }
--- @return table group { tabs, activeKey, SetActive(key) }
function UI:CreateTabGroup(parent, opts)
    opts = opts or {}
    local tabs = opts.tabs or {}
    local group = { tabs = {}, activeKey = opts.initial or (tabs[1] and tabs[1].key) }

    local function apply()
        for _, t in ipairs(group.tabs) do
            -- UIPanelButtonTemplate: use native LockHighlight for active tab, UnlockHighlight for inactive
            if t.key == group.activeKey then
                if t.LockHighlight then t:LockHighlight() end
                if t.text and t.text.SetTextColor then t.text:SetTextColor(1, 0.82, 0) end  -- gold for active
            else
                if t.UnlockHighlight then t:UnlockHighlight() end
                if t.text and t.text.SetTextColor then t.text:SetTextColor(1, 1, 1) end     -- white for inactive
            end
        end
    end

    local x = 0
    for _, info in ipairs(tabs) do
        -- strlenutf8 counts characters, not bytes (Cyrillic = 2 bytes per char)
        local charLen = strlenutf8 and strlenutf8(info.label or "") or #(info.label or "")
        local width = math.max(60, charLen * 10 + 16)
        local tab = self:CreateButton(parent, {
            label = info.label, width = width, height = 18,
            onClick = function()
                group.activeKey = info.key
                apply()
                if opts.onSelect then opts.onSelect(info.key) end
            end,
        })
        tab:SetPoint("TOPLEFT", parent, "TOPLEFT", x, 0)
        tab.key = info.key
        group.tabs[#group.tabs + 1] = tab
        x = x + width + 4
    end

    function group:SetActive(key)
        self.activeKey = key
        apply()
    end

    apply()
    return group
end

--- Creates a standardized checkbox (checked/unchecked with label).
--- @param parent Frame
--- @param opts { label, onChange(checked), initial }
--- @return CheckButton with .labelText, SetChecked, GetChecked, and wrapped OnClick
function UI:CreateCheckbox(parent, opts)
    opts = opts or {}
    local cb = CreateFrame("CheckButton", nil, parent, "UICheckButtonTemplate")
    cb:SetSize(24, 24)

    local labelText = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    labelText:SetPoint("LEFT", cb, "RIGHT", 4, 0)
    labelText:SetText(opts.label or "")
    labelText:SetTextColor(TC("LABEL"))
    cb.labelText = labelText

    if opts.initial ~= nil then cb:SetChecked(opts.initial) end

    cb:SetScript("OnClick", function(self)
        if opts.onChange then opts.onChange(self:GetChecked()) end
    end)

    return cb
end

--- Creates a standardized edit box (text input).
--- @param parent Frame
--- @param opts table { width, height, maxLetters, autoFocus, onChange, onEnterPressed, onEscapePressed }
--- @return EditBox
function UI:CreateEditBox(parent, opts)
    opts = opts or {}
    local eb = CreateFrame("EditBox", nil, parent, "InputBoxTemplate")
    eb:SetSize(opts.width or 160, opts.height or 20)
    eb:SetAutoFocus(opts.autoFocus or false)
    eb:SetMaxLetters(opts.maxLetters or 64)
    eb:SetFontObject(ChatFontNormal)

    if opts.onChange then
        eb:SetScript("OnTextChanged", function(self) opts.onChange(self, self:GetText() or "") end)
    end
    if opts.onEnterPressed then
        eb:SetScript("OnEnterPressed", opts.onEnterPressed)
    end
    eb:SetScript("OnEscapePressed", opts.onEscapePressed or function(self) self:ClearFocus() end)

    return eb
end

-------------------------------------------------------------------------------
-- Export helpers to namespace for sub-modules
-------------------------------------------------------------------------------
-------------------------------------------------------------------------------
-- Reusable UI Components (public API for feature modules)
-------------------------------------------------------------------------------

--- Creates a simple dropdown (no UIDropDownMenu — avoids taint)
--- @param parent Frame
--- @param width number
--- @param options table[] { {value, label}, ... }
--- @param onSelect function(value)
--- @param initialValue any
--- @return Button dropdown frame
function UI:CreateDropdown(parent, width, options, onSelect, initialValue)
    local dd = CreateFrame("Button", nil, parent, "BackdropTemplate")
    dd:SetSize(width, 22)
    dd:SetBackdrop(GetSmallBackdrop())
    dd:SetBackdropColor(TC("BTN_BG"))
    dd:SetBackdropBorderColor(TC("BTN_BORDER"))

    local label = dd:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    label:SetPoint("LEFT", dd, "LEFT", 8, 0)
    label:SetTextColor(TC("TEXT_WHITE"))

    local arrow = dd:CreateTexture(nil, "OVERLAY")
    arrow:SetSize(10, 10)
    arrow:SetPoint("RIGHT", dd, "RIGHT", -6, 0)
    arrow:SetTexture("Interface\\ChatFrame\\ChatFrameExpandArrow")

    dd.label = label
    dd.selectedValue = initialValue

    -- Popup is a child of the dropdown's parent frame — so when the parent
    -- hides (e.g., Auction popup closed), this popup auto-hides via parent chain.
    -- Use TOOLTIP strata so it renders above ALL other UI (dropdown is meant to overlay content).
    local popup = CreateFrame("Frame", nil, parent, "BackdropTemplate")
    popup:SetSize(width, #options * 20 + 8)
    popup:SetFrameStrata("TOOLTIP")
    popup:EnableMouse(true)
    popup:SetBackdrop(GetSmallBackdrop())
    popup:SetBackdropColor(0, 0, 0, 1)        -- fully opaque black
    popup:SetBackdropBorderColor(TC("BORDER"))
    popup:Hide()

    -- Extra safety: when the dropdown button itself hides, hide the popup too
    dd:HookScript("OnHide", function() popup:Hide() end)

    local itemBtns = {}
    for i, opt in ipairs(options) do
        local btn = CreateFrame("Button", nil, popup)
        btn:SetSize(width - 8, 18)
        btn:SetPoint("TOPLEFT", popup, "TOPLEFT", 4, -4 - (i - 1) * 20)
        btn:RegisterForClicks("LeftButtonUp", "AnyUp")
        btn:EnableMouse(true)

        local bg = btn:CreateTexture(nil, "BACKGROUND")
        bg:SetAllPoints()
        bg:SetColorTexture(0, 0, 0, 0)

        local t = btn:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
        t:SetPoint("LEFT", btn, "LEFT", 4, 0)
        t:SetText(opt.label)
        t:SetTextColor(TC("TEXT_WHITE"))

        btn:SetScript("OnEnter", function() bg:SetColorTexture(TC("BTN_BG_HOVER")) end)
        btn:SetScript("OnLeave", function() bg:SetColorTexture(0, 0, 0, 0) end)
        btn:SetScript("OnClick", function()
            dd.selectedValue = opt.value
            label:SetText(opt.label)
            popup:Hide()
            if onSelect then onSelect(opt.value) end
        end)
        itemBtns[i] = { btn = btn, opt = opt }
    end

    dd:SetScript("OnClick", function()
        if popup:IsShown() then
            popup:Hide()
        else
            popup:ClearAllPoints()
            popup:SetPoint("TOPLEFT", dd, "BOTTOMLEFT", 0, -2)
            popup:Show()
        end
    end)

    function dd:SetValue(value)
        for _, entry in ipairs(itemBtns) do
            if entry.opt.value == value then
                label:SetText(entry.opt.label)
                dd.selectedValue = value
                return
            end
        end
    end

    for _, opt in ipairs(options) do
        if opt.value == initialValue then
            label:SetText(opt.label)
            break
        end
    end

    return dd
end

--- Creates a pagination bar (Prev / Page X/Y / Next)
--- @param parent Frame
--- @param callbacks table { onPrev=fn, onNext=fn }
--- @return table { prevBtn, nextBtn, pageLabel, showingLabel, SetState(page, totalPages, total, perPage) }
function UI:CreatePaginationBar(parent, callbacks)
    local bar = {}

    bar.prevBtn = self:CreateButton(parent, {
        label = "< Prev", width = 64, height = 22,
        onClick = callbacks.onPrev or function() end,
    })
    bar.prevBtn:SetPoint("BOTTOMLEFT", parent, "BOTTOMLEFT", 10, 10)

    bar.nextBtn = self:CreateButton(parent, {
        label = "Next >", width = 64, height = 22,
        onClick = callbacks.onNext or function() end,
    })
    bar.nextBtn:SetPoint("BOTTOMRIGHT", parent, "BOTTOMRIGHT", -10, 10)

    bar.pageLabel = self:CreateLabel(parent, { color = "LABEL" })
    bar.pageLabel:SetPoint("BOTTOM", parent, "BOTTOM", 0, 15)

    function bar:SetState(page, totalPages, total, perPage, count)
        if totalPages > 0 then
            self.pageLabel:SetText(("Page %d/%d"):format(page, totalPages))
        else
            self.pageLabel:SetText("")
        end
        self.prevBtn:SetAlpha(page > 1 and 1 or 0.4)
        self.nextBtn:SetAlpha(page < totalPages and 1 or 0.4)
    end

    return bar
end

--- Creates a status bar (Records / Income / Expense / Net)
--- @param parent Frame
--- @return table { SetData(records, income, expense, net) }
function UI:CreateStatusBar(parent)
    local bar = {}

    bar.records = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    bar.records:SetPoint("BOTTOMLEFT", parent, "BOTTOMLEFT", 14, 36)
    bar.records:SetTextColor(TC("LABEL"))

    bar.income = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    bar.income:SetPoint("LEFT", bar.records, "RIGHT", 16, 0)

    bar.expense = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    bar.expense:SetPoint("LEFT", bar.income, "RIGHT", 16, 0)

    bar.net = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
    bar.net:SetPoint("LEFT", bar.expense, "RIGHT", 16, 0)

    function bar:SetData(records, income, expense, netVal)
        self.records:SetText(("Records: %d"):format(records or 0))
        self.income:SetText(("Income: %s"):format(GoldFormatter.Short(income or 0)))
        self.income:SetTextColor(TC("INCOME"))
        self.expense:SetText(("Expense: %s"):format(GoldFormatter.Short(expense or 0)))
        self.expense:SetTextColor(TC("EXPENSE"))
        self.net:SetText(("Net: %s"):format(GoldFormatter.Short(netVal or 0)))
        if (netVal or 0) >= 0 then
            local c = T("BALANCE_POS")
            self.net:SetTextColor(c[1], c[2], c[3])
        else
            local c = T("BALANCE_NEG")
            self.net:SetTextColor(c[1], c[2], c[3])
        end
    end

    return bar
end

-------------------------------------------------------------------------------
-- Export helpers to namespace for sub-modules
-------------------------------------------------------------------------------
ns.UI_Helpers = {
    T = T,
    TC = TC,
    GetSourceColors = GetSourceColors,
    GetBackdrop = GetBackdrop,
    GetSmallBackdrop = GetSmallBackdrop,
    SnapToMain = SnapToMain,
    MakeSeparator = MakeSeparator,
    GoldFormatter = GoldFormatter,
}

-------------------------------------------------------------------------------
-- Public API (delegates to sub-modules)
-------------------------------------------------------------------------------

function UI:ToggleMainFrame()
    local MF = ns.UI_MainFrame
    if not MF then return end  -- defensive: MainFrame not loaded
    local mf = MF.GetFrame()
    if not mf then
        MF.Create()
        mf = MF.GetFrame()
    end
    if mf:IsShown() then
        mf:Hide()
    else
        mf:Show()
        MF.UpdateSummaries()
    end
end


function UI:ToggleSettingsFrame()
    local S = ns.UI_Settings
    S.Create()
    S.Refresh()
    local sf = S.GetFrame()
    if sf:IsShown() then
        sf:Hide()
    else
        SnapToMain(sf)
        sf:Show()
    end
end

function UI:RefreshGoal()
    local MF = ns.UI_MainFrame
    if MF then MF.UpdateGoal() end
end

-------------------------------------------------------------------------------
-- Lifecycle
-------------------------------------------------------------------------------

function UI:OnEnable()
    -- Minimap is now a feature module (ns.Minimap), creates its own button on OnEnable.
    -- Respect legacy showMinimap=false setting via module facade
    if GoldLedgerDB and GoldLedgerDB.settings and GoldLedgerDB.settings.showMinimap == false then
        if ns.Minimap and ns.Minimap.SetVisible then ns.Minimap.SetVisible(false) end
    end

    local Tracker = GoldLedger:GetModule("Tracker")
    if Tracker then
        Tracker:OnGoldChanged(function()
            local MF = ns.UI_MainFrame
            if MF then MF.UpdateSummaries() end
        end)
    end
end
