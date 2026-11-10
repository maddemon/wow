"""GoldLedger 本地化检查：zhCN/enUS 解析、格式串占位符、语言按钮档位。"""
import pathlib
import lupa

ROOT = pathlib.Path(__file__).resolve().parent.parent / "GoldLedger"
lua = lupa.LuaRuntime()

lua.globals()["GetLocale"] = lua.eval("function() return 'zhCN' end")
lua.globals()["GetRealmName"] = lua.eval("function() return '时光之穴' end")
ns = lua.table()
chunk = lua.eval("load")(ROOT.joinpath("Locale.lua").read_text(encoding="utf-8"), "Locale.lua")
chunk("GoldLedger", ns)
L = ns["L"]

assert L["HEADER_INCOME"] == "收入", L["HEADER_INCOME"]
assert L["HEADER_EXPENSE"] == "支出", L["HEADER_EXPENSE"]
assert L["SRC_AH"] == "拍卖行", L["SRC_AH"]
assert L["UI_PAGE"] == "第 %d/%d 页", L["UI_PAGE"]

L["SetLocale"](L, "enUS")
assert L["HEADER_INCOME"] == "Income", L["HEADER_INCOME"]

L["SetLocale"](L, "ruRU")
assert L["GetLocale"](L) == "enUS", L["GetLocale"](L)
assert L["HEADER_INCOME"] == "Income", L["HEADER_INCOME"]

L["SetLocale"](L)
assert L["GetLocale"](L) == "zhCN", L["GetLocale"](L)

assert L["DATE_FORMAT"] == "%Y-%m-%d", "日期键必须保持 ISO，CleanupOldEntries 按字符串比大小"

fmt = lua.eval("string.format")
assert fmt(L["UI_PAGE"], 2, 7) == "第 2/7 页"
assert fmt(L["UI_RECORDS"], 12) == "记录数：12"
assert fmt(L["UI_NET"], "1.5金") == "净额：1.5金"
assert fmt(L["GOAL_REMAINING"], 3) == "按当前速度约需 3 天"

# 自动档兜底：文本语言挂着 enUS、但服务器是国服时，仍要认中文
lua.globals()["GetLocale"] = lua.eval("function() return 'enUS' end")
ns2 = lua.table()
lua.eval("load")(ROOT.joinpath("Locale.lua").read_text(encoding="utf-8"), "Locale.lua")("GoldLedger", ns2)
L2 = ns2["L"]
assert L2["GetLocale"](L2) == "zhCN", "国服 realm 名 + enUS 文本语言应判成中文"
assert L2["HEADER_INCOME"] == "收入", L2["HEADER_INCOME"]
L2["SetLocale"](L2)
assert L2["GetLocale"](L2) == "zhCN"

# 既不是中文文本语言、realm 名也是英文时，才掉回英文
lua.globals()["GetRealmName"] = lua.eval("function() return 'Stormrage' end")
ns3 = lua.table()
lua.eval("load")(ROOT.joinpath("Locale.lua").read_text(encoding="utf-8"), "Locale.lua")("GoldLedger", ns3)
L3 = ns3["L"]
assert L3["GetLocale"](L3) == "enUS", "英文客户端 + 英文服应判成英文"

assert L["SETTINGS_LANG_AUTO"] == "自动", L["SETTINGS_LANG_AUTO"]
L["SetLocale"](L, "enUS")
assert L["SETTINGS_LANG_AUTO"] == "Auto", L["SETTINGS_LANG_AUTO"]

settings = ROOT.joinpath("UI/Settings.lua").read_text(encoding="utf-8")
assert '"auto", "enUS", "zhCN"' in settings, "语言档位只剩 自动/英文/中文"
assert "ruRU" not in settings and "Русский" not in settings

for path in sorted(ROOT.rglob("*.lua")):
    src = path.read_text(encoding="utf-8")
    if path.name == "Locale.lua" and path.parent == ROOT:
        continue
    bad = [w for w in ("ruRU", "Русский") if w in src]
    assert not bad, f"{path.relative_to(ROOT)} 仍残留俄文档位: {bad}"

print("GoldLedger 本地化检查通过")
