"""Internal helpers — reduce boilerplate in Lua query/action builders."""

from __future__ import annotations

SENTINEL = "---END---"


def lua_quote(s: str) -> str:
    """Produce a safe Lua double-quoted string literal.

    Escapes backslashes, double quotes and newlines so arbitrary text (chat
    messages) can be embedded in generated Lua without breaking the string.
    """
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
    s = s.replace("\n", "\\n")
    return '"' + s + '"'

# Item type → GameInfo table name (shared by produce + purchase builders)
_ITEM_TABLE_MAP: dict[str, str] = {
    "UNIT": "Units",
    "BUILDING": "Buildings",
    "DISTRICT": "Districts",
    "PROJECT": "Projects",
}

# Item type → CityOperationTypes param key (shared by produce + purchase builders)
_ITEM_PARAM_MAP: dict[str, str] = {
    "UNIT": "PARAM_UNIT_TYPE",
    "BUILDING": "PARAM_BUILDING_TYPE",
    "DISTRICT": "PARAM_DISTRICT_TYPE",
    "PROJECT": "PARAM_PROJECT_TYPE",
}


def _bail(msg: str) -> str:
    """Python-side helper that expands to the Lua bail pattern.

    Usage in f-strings: ``if cond then {_bail("ERR:REASON")} end``
    Generates: ``print("ERR:REASON"); print("---END---"); return``
    """
    return f'print("{msg}"); print("{SENTINEL}"); return'


def _bail_lua(lua_expr: str) -> str:
    """Like _bail but the argument is a raw Lua expression (for string concatenation).

    Usage in f-strings: ``if cond then {_bail_lua('"ERR:REASON|" .. luaVar')} end``
    Generates: ``print("ERR:REASON|" .. luaVar); print("---END---"); return``
    """
    return f'print({lua_expr}); print("{SENTINEL}"); return'


def _lua_close_diplo_session() -> str:
    """Lua snippet: close any open diplomacy session with ``target``, restore UI.

    Expects ``me`` and ``target`` to be defined in scope.
    """
    return (
        "for r = 1, 5 do "
        "sid = DiplomacyManager.FindOpenSessionID(me, target) "
        "if not sid or sid < 0 then break end "
        'DiplomacyManager.AddResponse(sid, me, "NEGATIVE") '
        "sid = DiplomacyManager.FindOpenSessionID(me, target) "
        "if not sid or sid < 0 then break end "
        "DiplomacyManager.CloseSession(sid) "
        "end "
        "LuaEvents.DiplomacyActionView_ShowIngameUI() "
        "pcall(function() Events.HideLeaderScreen() end)"
    )


def _lua_get_unit(unit_index: int) -> str:
    """Lua snippet: look up a unit in InGame context or bail."""
    return (
        f"local me = Game.GetLocalPlayer() "
        f"local unit = UnitManager.GetUnit(me, {unit_index}) "
        f"if unit == nil then {_bail('ERR:UNIT_NOT_FOUND')} end"
    )


def _lua_get_unit_gamecore(unit_index: int) -> str:
    """Lua snippet: look up a unit in GameCore context or bail."""
    return (
        f"local me = Game.GetLocalPlayer() "
        f"local unit = Players[me]:GetUnits():FindID({unit_index}) "
        f"if unit == nil then {_bail('ERR:UNIT_NOT_FOUND')} end"
    )


def _lua_get_city(city_id: int) -> str:
    """Lua snippet: look up a city in InGame context or bail."""
    return (
        f"local me = Game.GetLocalPlayer() "
        f"local pCity = CityManager.GetCity(me, {city_id} % 65536) "
        f"if pCity == nil then {_bail('ERR:CITY_NOT_FOUND')} end"
    )


# ---------------------------------------------------------------------------
# Parser helpers — reduce noise in pipe-delimited response parsers
# ---------------------------------------------------------------------------


def _int(s: str) -> int:
    """Parse a string that may be a float representation to int.

    Lua prints integers as floats (e.g. ``3.0``).  This avoids the
    ``int(float(x))`` pattern repeated across every parser.
    """
    return int(float(s))


# ---------------------------------------------------------------------------
# Shared Lua snippet constants — compose into builders via string concat.
# These are plain strings (NOT f-strings), so Lua braces are unescaped.
# Interpolate into f-string builders with {_LUA_RES_VISIBLE} etc.
# ---------------------------------------------------------------------------

# Resource visibility check — expects ``pTech`` in scope.
_LUA_RES_VISIBLE = """\
local function resVisible(resEntry)
    if not resEntry.PrereqTech then return true end
    local t = GameInfo.Technologies[resEntry.PrereqTech]
    return t and pTech:HasTech(t.Index)
end"""

# Victory-enabled check — prints VENABLED| lines for each enabled victory type.
# Yield label table + formatters for trade route yield display.
# fmtY: format array of {YieldIndex, Amount} objects (from GetOutgoingRoutes).
# fmtFlat/sumFlat: format/sum flat 6-element arrays (from Calculate* APIs).
# Both share the yN label table.
_LUA_YIELD_LABELS = 'local yN = {"F","P","G","S","C","A"}'

_LUA_FMT_Y = """\
local function fmtY(tbl)
    if not tbl then return "" end
    local s = ""
    for _, e in ipairs(tbl) do
        if e.Amount and e.Amount > 0 then
            local idx = e.YieldIndex + 1
            if idx >= 1 and idx <= 6 then
                local amt = e.Amount
                if amt == math.floor(amt) then amt = math.floor(amt) end
                s = s .. yN[idx] .. amt
            end
        end
    end
    return s
end"""

_LUA_FMT_FLAT = """\
local function sumFlat(...)
    local s = {0,0,0,0,0,0}
    for _, t in ipairs({...}) do
        if t then for j = 1, 6 do s[j] = s[j] + (t[j] or 0) end end
    end
    return s
end
local function fmtFlat(arr)
    if not arr then return "" end
    local s = ""
    for j = 1, 6 do
        local v = arr[j]
        if v and v > 0 then
            if v == math.floor(v) then v = math.floor(v) end
            s = s .. yN[j] .. v
        end
    end
    return s
end"""

_LUA_VICTORY_ENABLED = """\
local _vtypes = {"VICTORY_TECHNOLOGY","VICTORY_CULTURE","VICTORY_RELIGIOUS","VICTORY_DIPLOMATIC","VICTORY_CONQUEST"}
for _, vt in ipairs(_vtypes) do
    local row = GameInfo.Victories[vt]
    if row then
        local ok, en = pcall(function() return Game.IsVictoryEnabled(row.Index) end)
        if ok and en then print("VENABLED|" .. vt) end
    end
end"""

# Combat estimate helpers — shared by build_combat_estimate_query and
# build_units_query. Defines two LOCAL functions the caller invokes:
#   buildPromoBonuses() -> table  (scan GameInfo.UnitPromotionModifiers ONCE)
#   computeEstimate(attacker, enemy, tx, ty, ux, uy, attCS, attRS, defCS,
#                   promoBonuses, myId)
#       -> effAttCS, effDefCS, isRanged, mods  (mods is a list table)
# Callers format the result (ESTIMATE| line or per-target token) and do the
# damage formula in Python. Expects GameInfo/Map/Players in scope (InGame ctx).
_LUA_COMBAT_ESTIMATE = """\
local function buildPromoBonuses()
    local t = {}
    pcall(function()
        for pm in GameInfo.UnitPromotionModifiers() do
            local mod = GameInfo.Modifiers[pm.ModifierId]
            if mod and mod.ModifierType == "MODIFIER_UNIT_ADJUST_COMBAT_STRENGTH" then
                for arg in GameInfo.ModifierArguments() do
                    if arg.ModifierId == pm.ModifierId and arg.Name == "Amount" then
                        local val = tonumber(arg.Value) or 0
                        if val ~= 0 then
                            if not t[pm.UnitPromotionType] then t[pm.UnitPromotionType] = {} end
                            table.insert(t[pm.UnitPromotionType], { amount = val, name = pm.ModifierId })
                        end
                    end
                end
            end
        end
    end)
    return t
end
local function promoBonusFor(u, promoBonuses)
    local total, parts = 0, {}
    local exp = u:GetExperience()
    for promoType, infos in pairs(promoBonuses) do
        local promoRow = GameInfo.UnitPromotions[promoType]
        if promoRow then
            local ok, has = pcall(function() return exp:HasPromotion(promoRow.Index) end)
            if ok and has then
                for _, info in ipairs(infos) do
                    total = total + info.amount
                    local short = info.name:gsub("MODIFIER_", "")
                    table.insert(parts, short .. " " .. (info.amount > 0 and "+" or "") .. info.amount)
                end
            end
        end
    end
    return total, parts
end
local function computeEstimate(attacker, enemy, tx, ty, ux, uy, attCS, attRS, defCS, promoBonuses, myId)
    local dist = Map.GetPlotDistance(ux, uy, tx, ty)
    local isRanged = attRS > 0 and dist > 1
    local effAttCS = isRanged and attRS or attCS
    local mods, defModTotal, attModTotal = {}, 0, 0
    -- Attacker promotion bonuses
    local apB, apM = promoBonusFor(attacker, promoBonuses)
    if apB ~= 0 then
        attModTotal = attModTotal + apB
        for _, m in ipairs(apM) do table.insert(mods, "att " .. m) end
    end
    -- Defender promotion bonuses
    local dpB, dpM = promoBonusFor(enemy, promoBonuses)
    if dpB ~= 0 then
        defModTotal = defModTotal + dpB
        for _, m in ipairs(dpM) do table.insert(mods, "def " .. m) end
    end
    -- Defender fortified
    local ok1, ft = pcall(function() return enemy:GetFortifyTurns() end)
    if ok1 and ft and ft > 0 then
        local bonus = math.min(ft * 3, 6)
        table.insert(mods, "fortified +" .. bonus)
        defModTotal = defModTotal + bonus
    end
    local tgtPlot = Map.GetPlot(tx, ty)
    -- Defender on hills
    if tgtPlot and tgtPlot:IsHills() then
        table.insert(mods, "hills +3")
        defModTotal = defModTotal + 3
    end
    -- Forest/jungle defense
    if tgtPlot then
        local feat = tgtPlot:GetFeatureType()
        if feat >= 0 then
            local fInfo = GameInfo.Features[feat]
            if fInfo and (fInfo.FeatureType == "FEATURE_FOREST" or fInfo.FeatureType == "FEATURE_JUNGLE") then
                table.insert(mods, fInfo.FeatureType:gsub("FEATURE_",""):lower() .. " +3")
                defModTotal = defModTotal + 3
            end
        end
    end
    -- River crossing penalty (melee only)
    if not isRanged and tgtPlot then
        local attPlot = Map.GetPlot(ux, uy)
        if attPlot and tgtPlot:IsRiverCrossingToPlot(attPlot) then
            table.insert(mods, "river -2")
            attModTotal = attModTotal - 2
        end
    end
    -- Flanking: our units adjacent to defender (excluding attacker)
    if not isRanged then
        local flankBonus = 0
        for dy = -1, 1 do for dx = -1, 1 do
            if dx ~= 0 or dy ~= 0 then
                local fx, fy = tx + dx, ty + dy
                if not (fx == ux and fy == uy) then
                    local adjUnits = Map.GetUnitsAt(fx, fy)
                    if adjUnits then
                        for adjU in adjUnits:Units() do
                            if adjU:GetOwner() == myId then
                                local adjInfo = GameInfo.Units[adjU:GetType()]
                                if adjInfo and (adjInfo.Combat or 0) > 0 then flankBonus = flankBonus + 2 end
                            end
                        end
                    end
                end
            end
        end end
        if flankBonus > 0 then
            table.insert(mods, "flank +" .. flankBonus)
            attModTotal = attModTotal + flankBonus
        end
    end
    -- Support: defender's adjacent friendlies
    if not isRanged then
        local enemyOwner = enemy:GetOwner()
        local supportBonus = 0
        for dy = -1, 1 do for dx = -1, 1 do
            if dx ~= 0 or dy ~= 0 then
                local sx, sy = tx + dx, ty + dy
                local adjUnits = Map.GetUnitsAt(sx, sy)
                if adjUnits then
                    for adjU in adjUnits:Units() do
                        if adjU:GetOwner() == enemyOwner and adjU ~= enemy then
                            local adjInfo = GameInfo.Units[adjU:GetType()]
                            if adjInfo and (adjInfo.Combat or 0) > 0 then supportBonus = supportBonus + 2 end
                        end
                    end
                end
            end
        end end
        if supportBonus > 0 then
            table.insert(mods, "support +" .. supportBonus)
            defModTotal = defModTotal + supportBonus
        end
    end
    return effAttCS + attModTotal, defCS + defModTotal, isRanged, mods
end"""
