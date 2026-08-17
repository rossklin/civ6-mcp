"""Cities domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import (
    _ITEM_PARAM_MAP,
    _ITEM_TABLE_MAP,
    SENTINEL,
    _bail,
    _bail_lua,
    _lua_get_city,
)

def build_city_attack(city_id: int, target_x: int, target_y: int) -> str:
    """InGame context: fire city ranged attack at a target tile."""
    return f"""
{_lua_get_city(city_id)}
local cx, cy = pCity:GetX(), pCity:GetY()
local dist = Map.GetPlotDistance(cx, cy, {target_x}, {target_y})
local enemy = nil
local pu = Map.GetUnitsAt({target_x}, {target_y})
if pu then for other in pu:Units() do if other:GetOwner() ~= me then enemy = other end end end
if not enemy then {_bail("ERR:NO_ENEMY|No hostile unit at target tile")} end
local eInfo = GameInfo.Units[enemy:GetType()]
local eName = eInfo and eInfo.UnitType or "UNKNOWN"
local eHP = enemy:GetMaxDamage() - enemy:GetDamage()
local params = {{}}
params[CityCommandTypes.PARAM_X] = {target_x}
params[CityCommandTypes.PARAM_Y] = {target_y}
-- Pre-checks for specific error messages
local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
local hasWalls = false
for _, d in pCity:GetDistricts():Members() do
    if d:GetType() == ccIdx then
        local wHP = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER)
        if wHP and wHP > 0 then hasWalls = true end
        break
    end
end
if not hasWalls then
    {_bail("ERR:NO_WALLS|City has no walls — build Ancient Walls first")}
end
if dist > 2 then
    {_bail_lua('"ERR:OUT_OF_RANGE|Target is " .. dist .. " tiles away (city attack range is 2)"')}
end
-- Check if target is in the valid target list (covers LOS + already-fired)
local validTargets = CityManager.GetCommandTargets(pCity, CityCommandTypes.RANGE_ATTACK)
local targetPlotIdx = {target_y} * Map.GetGridSize() + {target_x}
local inTargets = false
if validTargets then
    for _, tbl in pairs(validTargets) do
        if type(tbl) == "table" then
            for _, idx in ipairs(tbl) do
                if idx == targetPlotIdx then inTargets = true; break end
            end
        end
        if inTargets then break end
    end
end
if not inTargets then
    -- Distinguish already-fired from LOS: if NO targets at all, city already fired
    local totalTargets = 0
    if validTargets then
        for _, tbl in pairs(validTargets) do
            if type(tbl) == "table" then totalTargets = totalTargets + #tbl end
        end
    end
    if totalTargets == 0 then
        {_bail("ERR:ALREADY_FIRED|City already attacked this turn")}
    else
        {_bail_lua('"ERR:NO_LOS|Line of sight to (" .. {target_x} .. "," .. {target_y} .. ") is blocked from (" .. cx .. "," .. cy .. ")"')}
    end
end
local canAttack = CityManager.CanStartCommand(pCity, CityCommandTypes.RANGE_ATTACK, true, params, false)
if not canAttack then
    {_bail("ERR:CANNOT_ATTACK|City cannot attack this target (unknown reason)")}
end
CityManager.RequestCommand(pCity, CityCommandTypes.RANGE_ATTACK, params)
print("OK:CITY_RANGE_ATTACK|" .. Locale.Lookup(pCity:GetName()) .. " -> " .. eName .. "@{target_x},{target_y}|pre_hp:" .. eHP .. "/" .. enemy:GetMaxDamage())
print("{SENTINEL}")
"""


def build_resolve_city_capture(action: str) -> str:
    """InGame context: resolve a 'Keep or Free City' / 'Raze City' blocker.

    action: 'keep', 'reject', 'raze', 'liberate_founder', 'liberate_previous'
    Tries GetNextRebelledCity first (loyalty flip), then GetNextCapturedCity (conquest).
    """
    directive_map = {
        "keep": "CityDestroyDirectives.KEEP",
        "reject": "CityDestroyDirectives.REJECT",
        "raze": "CityDestroyDirectives.RAZE",
        "liberate_founder": "CityDestroyDirectives.LIBERATE_FOUNDER",
        "liberate_previous": "CityDestroyDirectives.LIBERATE_PREVIOUS_OWNER",
    }
    directive = directive_map.get(action)
    if not directive:
        valid = ", ".join(directive_map.keys())
        return _bail(f"ERR:INVALID_ACTION|Valid actions: {valid}")

    return f"""
local me = Game.GetLocalPlayer()
local player = Players[me]
local city = player:GetCities():GetNextRebelledCity()
local source = "rebelled"
if city == nil then
    city = player:GetCities():GetNextCapturedCity()
    source = "captured"
end
if city == nil then {_bail("ERR:NO_PENDING_CITY|No rebelled or captured city pending decision")} end
local name = Locale.Lookup(city:GetName())
local pop = city:GetPopulation()
local cid = city:GetID()
local params = {{}}
params[UnitOperationTypes.PARAM_FLAGS] = {directive}
local canDo = CityManager.CanStartCommand(city, CityCommandTypes.DESTROY, params)
if not canDo then {_bail_lua(f'"ERR:CANNOT_{action.upper()}|Cannot {action} " .. name .. " (CanStartCommand returned false)"')} end
CityManager.RequestCommand(city, CityCommandTypes.DESTROY, params)
print("OK:{action.upper()}|" .. name .. " (pop " .. pop .. ", id:" .. cid .. ", " .. source .. ")")
print("{SENTINEL}")
"""

def build_produce_item(
    city_id: int,
    item_type: str,
    item_name: str,
    target_x: int | None = None,
    target_y: int | None = None,
) -> str:
    """Set production for a city via CityManager.RequestOperation (InGame context).

    item_type is UNIT/BUILDING/DISTRICT, item_name is e.g. UNIT_WARRIOR.
    Uses .Hash for item refs and VALUE_REPLACE_AT position 0 to replace current production.
    For districts, pass target_x/target_y to specify placement tile.
    """
    itype = item_type.upper()
    table_name = _ITEM_TABLE_MAP.get(itype, "Units")
    param_key = _ITEM_PARAM_MAP.get(itype, "PARAM_UNIT_TYPE")
    # Districts require placement coordinates
    if itype == "DISTRICT" and (target_x is None or target_y is None):
        return (
            f'print("ERR:MISSING_COORDS|{item_name} is a district and requires '
            f"target_x/target_y for placement. Use get_district_advisor(city_id, "
            f"'{item_name}') to find the best tile.\")\n"
            f'print("{SENTINEL}")'
        )
    # Extra params for district placement
    xy_params = ""
    xy_check_params = ""
    if target_x is not None and target_y is not None:
        xy_params = f"tParams[CityOperationTypes.PARAM_X] = {target_x}\ntParams[CityOperationTypes.PARAM_Y] = {target_y}"
        xy_check_params = f"tCheck[CityOperationTypes.PARAM_X] = {target_x}\ntCheck[CityOperationTypes.PARAM_Y] = {target_y}"
    return f"""
{_lua_get_city(city_id)}
local item = GameInfo.{table_name}["{item_name}"]
if item == nil then {_bail(f"ERR:ITEM_NOT_FOUND|{item_name}")} end
local bq = pCity:GetBuildQueue()
if not bq:CanProduce(item.Hash, true) then
    -- Diagnose why production is blocked
    local reason = ""
    pcall(function()
        if item.PrereqDistrict then
            local hasDistrict = false
            for _, d in pCity:GetDistricts():Members() do
                local dInfo = GameInfo.Districts[d:GetType()]
                if dInfo and dInfo.DistrictType == item.PrereqDistrict then hasDistrict = true; break end
            end
            if not hasDistrict then reason = " (requires " .. item.PrereqDistrict .. " district)" end
        end
        if reason == "" and item.PrereqBuildingType then
            reason = " (requires " .. item.PrereqBuildingType .. ")"
        end
        if reason == "" then
            -- Check if building is already built in this city
            local buildings = pCity:GetBuildings()
            if buildings and buildings:HasBuilding(item.Index) then
                reason = " (already built)"
            end
        end
    end)
    {
        _bail_lua(
            f'"ERR:CANNOT_PRODUCE|{item_name} cannot be produced in this city" .. reason'
        )
    }
end
{
        ""
        if itype != "BUILDING" or (target_x is not None and target_y is not None)
        else f'''if item.IsWonder then
    {_bail(f"ERR:MISSING_COORDS|{item_name} is a wonder and requires target_x/target_y for placement. Use get_wonder_advisor(city_id, '{item_name}') to find valid tiles.")}
end'''
    }
-- Trader cap check: game silently rejects when count >= route capacity
if "{item_name}" == "UNIT_TRADER" then
    local pTrade = Players[me]:GetTrade()
    local traderCount = 0
    for _, u in Players[me]:GetUnits():Members() do
        if GameInfo.Units[u:GetType()].UnitType == "UNIT_TRADER" then traderCount = traderCount + 1 end
    end
    local routeCap = pTrade:GetOutgoingRouteCapacity()
    if traderCount >= routeCap then
        print("ERR:TRADER_CAP|Cannot build Trader: you have " .. traderCount .. " Traders but only " .. routeCap .. " trade route capacity. Build Markets or Lighthouses to increase capacity.")
        print("{SENTINEL}")
        return
    end
end
local tCheck = {{}}
tCheck[CityOperationTypes.{param_key}] = item.Hash
{xy_check_params}
local canStart = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, tCheck, true)
local tParams = {{}}
tParams[CityOperationTypes.{param_key}] = item.Hash
{xy_params}
-- Always EXCLUSIVE: set_city_production's contract is "replace the current
-- build", not "queue alongside existing items". EXCLUSIVE clears the queue
-- and writes one item, avoiding silent no-ops that hit REPLACE_AT when the
-- queue is in a degenerate state.
tParams[CityOperationTypes.PARAM_INSERT_MODE] = CityOperationTypes.VALUE_EXCLUSIVE
CityManager.RequestOperation(pCity, CityOperationTypes.BUILD, tParams)
if canStart then
    local turnsLeft = bq:GetTurnsLeft(item.Hash)
    print("OK:PRODUCING|{item_name}|" .. turnsLeft .. " turns")
else
    -- Check for pillaged districts to give actionable error
    local pillaged = {{}}
    for _, d in pCity:GetDistricts():Members() do
        if d:IsPillaged() then
            local dInfo = GameInfo.Districts[d:GetType()]
            if dInfo then table.insert(pillaged, dInfo.DistrictType) end
        end
    end
    if #pillaged > 0 then
        print("MAYBE:PRODUCING|{
        item_name
    }|canStart=false|PILLAGED:" .. table.concat(pillaged, ","))
    else
        print("MAYBE:PRODUCING|{item_name}|canStart=false")
    end
end
print("{SENTINEL}")
"""


def build_verify_production(city_id: int, item_name: str) -> str:
    """GameCore readback: verify production was set after RequestOperation.

    Uses CurrentlyBuilding() (GameCore) instead of GetCurrentProductionTypeHash()
    which is InGame-only and returns nil in GameCore context.
    """
    return f"""
local me = Game.GetLocalPlayer()
local pCity = Players[me]:GetCities():FindID({city_id} % 65536)
if pCity == nil then print("NOT_FOUND"); print("{SENTINEL}"); return end
local bq = pCity:GetBuildQueue()
local cur = bq:CurrentlyBuilding()
if cur == "{item_name}" then
    print("CONFIRMED|" .. bq:GetTurnsLeft() .. " turns")
else
    print("NOT_SET|current=" .. tostring(cur) .. "|expected={item_name}")
end
print("{SENTINEL}")
"""


def build_purchase_item(
    city_id: int, item_type: str, item_name: str, yield_type: str = "YIELD_GOLD"
) -> str:
    """Purchase a unit or building with gold/faith via CityManager.RequestCommand (InGame context)."""
    itype = item_type.upper()
    table_name = _ITEM_TABLE_MAP.get(itype)
    param_key = _ITEM_PARAM_MAP.get(itype)
    if table_name is None or param_key is None:
        return _bail(
            f"ERR:INVALID_TYPE|Can only purchase UNIT or BUILDING, got {item_type}"
        )
    return f"""
{_lua_get_city(city_id)}
local item = GameInfo.{table_name}["{item_name}"]
if item == nil then {_bail(f"ERR:ITEM_NOT_FOUND|{item_name}")} end
local yieldRow = GameInfo.Yields["{yield_type}"]
if yieldRow == nil then {_bail(f"ERR:YIELD_NOT_FOUND|{yield_type}")} end
local tParams = {{}}
tParams[CityCommandTypes.{param_key}] = item.Hash
tParams[CityCommandTypes.PARAM_YIELD_TYPE] = yieldRow.Index
if "{itype}" == "UNIT" then
    tParams[CityCommandTypes.PARAM_MILITARY_FORMATION_TYPE] = MilitaryFormationTypes.STANDARD_MILITARY_FORMATION
    local cx, cy = pCity:GetX(), pCity:GetY()
    local targetClass = item.FormationClass
    local existing = Map.GetUnitsAt(cx, cy)
    if existing and existing:GetCount() > 0 then
        for u in existing:Units() do
            if u:GetOwner() == me then
                local uDef = GameInfo.Units[u:GetType()]
                if uDef and uDef.FormationClass == targetClass then
                    local uid = u:GetID() + u:GetOwner() * 65536
                    {_bail_lua(f'"ERR:STACKING_CONFLICT|Cannot purchase {item_name} — " .. uDef.UnitType .. " (unit_id=" .. uid .. ") is on the city tile. Move it with unit_action(unit_id=" .. uid .. ", action=\'move\', target_x, target_y) first, then retry the purchase."')}
                end
            end
        end
    end
end
local cost = pCity:GetGold():GetPurchaseCost(yieldRow.Index, item.Hash, MilitaryFormationTypes.STANDARD_MILITARY_FORMATION)
local isFaith = ("{yield_type}" == "YIELD_FAITH")
local balance
if isFaith then
    balance = Players[me]:GetReligion():GetFaithBalance()
else
    balance = Players[me]:GetTreasury():GetGoldBalance()
end
local suffix = isFaith and "f" or "g"
local canBuy, results = CityManager.CanStartCommand(pCity, CityCommandTypes.PURCHASE, false, tParams, true)
if not canBuy then
    local reasons = {{}}
    if results then
        for _,v in pairs(results) do
            if type(v) == "table" then
                for _,msg in pairs(v) do if type(msg) == "string" then table.insert(reasons, msg) end end
            elseif type(v) == "string" then table.insert(reasons, v)
            end
        end
    end
    if cost > balance then
        table.insert(reasons, 1, "costs " .. math.floor(cost) .. suffix .. " but you only have " .. math.floor(balance) .. suffix)
    end
    local reason = #reasons > 0 and table.concat(reasons, "; ") or "unknown"
    {_bail_lua('"ERR:CANNOT_PURCHASE|" .. reason')}
end
CityManager.RequestCommand(pCity, CityCommandTypes.PURCHASE, tParams)
print("OK:PURCHASED|{item_name}|cost=" .. math.floor(cost) .. suffix .. " (had " .. math.floor(balance) .. suffix .. ")")
print("{SENTINEL}")
"""


def build_city_yield_focus_query(city_id: int) -> str:
    """Get current yield focus settings for a city (InGame context)."""
    return f"""
{_lua_get_city(city_id)}
local citz = pCity:GetCitizens()
local yields = {{"YIELD_FOOD", "YIELD_PRODUCTION", "YIELD_GOLD", "YIELD_SCIENCE", "YIELD_CULTURE", "YIELD_FAITH"}}
for _, yName in ipairs(yields) do
    local yRow = GameInfo.Yields[yName]
    if yRow then
        local favored = citz:IsFavoredYield(yRow.Index)
        local disfavored = citz:IsDisfavoredYield(yRow.Index)
        local status = "neutral"
        if favored then status = "favored" elseif disfavored then status = "disfavored" end
        print("FOCUS|" .. yName .. "|" .. status)
    end
end
print("{SENTINEL}")
"""


def build_set_yield_focus(city_id: int, yield_type: str) -> str:
    """Set or clear a yield focus for a city (InGame context).

    Uses CityManager.RequestCommand with CityCommandTypes.SET_FOCUS.
    yield_type="DEFAULT" clears all focus. Otherwise sets the given yield as favored.
    PARAM_FLAGS: 1 = toggle favored, 0 = toggle disfavored.
    """
    if yield_type.upper() == "DEFAULT":
        # Clear all focus by toggling off any currently favored/disfavored yields
        return f"""
{_lua_get_city(city_id)}
local citz = pCity:GetCitizens()
local cleared = false
for yRow in GameInfo.Yields() do
    if citz:IsFavoredYield(yRow.Index) then
        local tp = {{}}
        tp[CityCommandTypes.PARAM_YIELD_TYPE] = yRow.Index
        tp[CityCommandTypes.PARAM_FLAGS] = 1
        CityManager.RequestCommand(pCity, CityCommandTypes.SET_FOCUS, tp)
        cleared = true
    end
    if citz:IsDisfavoredYield(yRow.Index) then
        local tp = {{}}
        tp[CityCommandTypes.PARAM_YIELD_TYPE] = yRow.Index
        tp[CityCommandTypes.PARAM_FLAGS] = 0
        CityManager.RequestCommand(pCity, CityCommandTypes.SET_FOCUS, tp)
        cleared = true
    end
end
if cleared then print("OK:FOCUS_CLEARED|All yield focus cleared")
else print("OK:FOCUS_CLEARED|No focus was set") end
print("{SENTINEL}")
"""
    yield_name = yield_type.upper()
    if not yield_name.startswith("YIELD_"):
        yield_name = f"YIELD_{yield_name}"
    return f"""
{_lua_get_city(city_id)}
local yRow = GameInfo.Yields["{yield_name}"]
if yRow == nil then {_bail(f"ERR:YIELD_NOT_FOUND|{yield_name}")} end
local citz = pCity:GetCitizens()
-- Clear existing favored focus first
for yr in GameInfo.Yields() do
    if citz:IsFavoredYield(yr.Index) then
        local tp = {{}}
        tp[CityCommandTypes.PARAM_YIELD_TYPE] = yr.Index
        tp[CityCommandTypes.PARAM_FLAGS] = 1
        CityManager.RequestCommand(pCity, CityCommandTypes.SET_FOCUS, tp)
    end
end
-- Set new focus
local tParams = {{}}
tParams[CityCommandTypes.PARAM_YIELD_TYPE] = yRow.Index
tParams[CityCommandTypes.PARAM_FLAGS] = 1
CityManager.RequestCommand(pCity, CityCommandTypes.SET_FOCUS, tParams)
print("OK:FOCUS_SET|{yield_name}|favored")
print("{SENTINEL}")
"""
