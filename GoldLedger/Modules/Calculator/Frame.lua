--[[
    GoldLedger / Calculator: Frame.lua
    Built-in calculator popup.
    Uses ONLY UI public API — no direct WoW API calls.
]]

local ADDON_NAME, ns = ...
ns.Calculator = ns.Calculator or {}

local Frame = {}
ns.Calculator.Frame = Frame

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------
local function UI() return ns.GoldLedger and ns.GoldLedger:GetModule("UI") end
local function L()  return ns.Calculator.L end
local function H()  return ns.UI_Helpers end

-------------------------------------------------------------------------------
-- Calculator state
-------------------------------------------------------------------------------
local calcFrame

function Frame:Create()
    local ui = UI()
    local helpers = H()
    local l = L()
    if not ui then return end

    -- Standardized popup: title bar, close button, drag, backdrop
    local f = ui:CreatePopup({
        name   = "GoldLedgerCalcFrame",
        title  = l["CALC_TITLE"],
        width  = 220,
        height = 290,
    })

    -- Display (edit box for expression)
    local display = ui:CreateEditBox(f, {
        width = 196,
        height = 28,
        maxLetters = 64,
        autoFocus = false,
    })
    display:ClearAllPoints()
    display:SetPoint("TOPLEFT", f, "TOPLEFT", 12, -36)
    display:SetPoint("TOPRIGHT", f, "TOPRIGHT", -12, -36)
    display:SetJustifyH("RIGHT")
    display:SetTextInsets(6, 6, 0, 0)
    display:SetFontObject(GameFontHighlight)
    display:SetText("0")
    display:SetScript("OnChar", function(self, char)
        if not char:match("[%d%.%+%-%*/]") then
            local text = self:GetText() or ""
            self:SetText(text:gsub("[^%d%.%+%-%*/]+", ""))
            self:SetCursorPosition(#self:GetText())
        end
    end)
    f.display = display

    local BASE_WIDTH = 220
    local MARGIN = 12
    local BTN_GAP = 4
    local BTN_H = 32
    local gridTop = -70
    local measure = display:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    measure:Hide()

    local allBtns = {}
    local clearBtn

    local function RelayoutButtons()
        local innerW = f:GetWidth() - MARGIN * 2
        local btnW = (innerW - 3 * BTN_GAP) / 4
        for idx, btn in ipairs(allBtns) do
            local col = ((idx - 1) % 4)
            local row = math.floor((idx - 1) / 4)
            btn:SetSize(btnW, BTN_H)
            btn:ClearAllPoints()
            btn:SetPoint("TOPLEFT", f, "TOPLEFT", MARGIN + col * (btnW + BTN_GAP), gridTop - row * (BTN_H + BTN_GAP))
        end
        if clearBtn then
            clearBtn:SetSize(innerW, 28)
            clearBtn:ClearAllPoints()
            clearBtn:SetPoint("TOPLEFT", f, "TOPLEFT", MARGIN, gridTop - 4 * (BTN_H + BTN_GAP) - 4)
        end
    end

    local function ResizeToFit()
        measure:SetText(display:GetText() or "")
        local textWidth = measure:GetStringWidth() + 36
        local needed = math.max(BASE_WIDTH, textWidth + MARGIN * 2)
        if math.abs(needed - f:GetWidth()) > 2 then
            f:SetWidth(needed)
            RelayoutButtons()
        end
    end
    display:SetScript("OnTextChanged", ResizeToFit)

    -- Button grid
    local buttons = {
        "7", "8", "9", "/",
        "4", "5", "6", "*",
        "1", "2", "3", "-",
        "0", ".", "=", "+",
    }

    for idx, label in ipairs(buttons) do
        local col = ((idx - 1) % 4)
        local row = math.floor((idx - 1) / 4)
        local btnW = (f:GetWidth() - MARGIN * 2 - 3 * BTN_GAP) / 4

        local btn = ui:CreateButton(f, {
            label  = label,
            width  = btnW,
            height = BTN_H,
            onClick = function()
                local cur = display:GetText() or ""
                if label == "=" then
                    local expr = cur:gsub("[^%d%.%+%-%*/%%%(%)]+", "")
                    local fn, err = loadstring("return " .. expr)
                    if fn then
                        local ok, result = pcall(fn)
                        display:SetText(ok and result and tostring(result) or "Error")
                    else
                        display:SetText("Error")
                    end
                else
                    if cur == "0" or cur == "Error" then
                        if label == "." then
                            display:SetText("0.")
                        elseif label == "/" or label == "*" or label == "-" or label == "+" then
                            display:SetText("0" .. label)
                        else
                            display:SetText(label)
                        end
                    else
                        display:SetText(cur .. label)
                    end
                end
                display:SetCursorPosition(#display:GetText())
            end,
        })
        btn:SetPoint("TOPLEFT", f, "TOPLEFT", MARGIN + col * (btnW + BTN_GAP), gridTop - row * (BTN_H + BTN_GAP))
        btn.text:SetFontObject(GameFontHighlight)
        table.insert(allBtns, btn)
    end

    -- Clear button
    clearBtn = ui:CreateButton(f, {
        label  = l["CALC_CLEAR"],
        width  = f:GetWidth() - MARGIN * 2,
        height = 28,
        onClick = function() display:SetText("0") end,
    })
    clearBtn:SetPoint("TOPLEFT", f, "TOPLEFT", MARGIN, gridTop - 4 * (BTN_H + BTN_GAP) - 4)
    clearBtn.text:SetFontObject(GameFontHighlight)

    calcFrame = f
    return f
end

-------------------------------------------------------------------------------
-- Public
-------------------------------------------------------------------------------
function Frame:Toggle()
    if not calcFrame then
        self:Create()
    end
    if calcFrame:IsShown() then
        calcFrame:Hide()
    else
        local helpers = H()
        if helpers and helpers.SnapToMain then
            helpers.SnapToMain(calcFrame)
        end
        calcFrame:Show()
    end
end

function Frame:GetFrame()
    return calcFrame
end

function Frame:SetFrame(f)
    calcFrame = f
end
