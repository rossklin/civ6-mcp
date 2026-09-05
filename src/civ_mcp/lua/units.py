"""Units domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import (
    _LUA_OCCUPANCY_CLASS,
    _LUA_RES_VISIBLE,
    SENTINEL,
    _bail,
    _bail_lua,
    _lua_get_unit,
    _lua_get_unit_gamecore,
    load_lua_template,
)
from civ_mcp.lua.models import (
    AttackOutcome,
    AttackTarget,
    BuilderInfo,
    BuilderTask,
    GoodyReward,
    PromotionOption,
    ThreatInfo,
    UnitInfo,
)


def build_units_query() -> str:
    """InGame context: lists all units with upgrade and builder improvement info.

    The Lua lives in ``units.lua`` (loaded via ``load_lua_template``) and emits
    pipe-delimited lines for :func:`parse_units_response` — not narrated prose
    — because the structured :class:`UnitInfo` is consumed by the turn-snapshot
    functions in ``game_state.py``. Attack targets are classified by effective
    occupancy class (shared ``occupancyClass`` helper) and gated by the
    engine's own ``CanStartOperation`` (the same op the UI's move chain uses):
    military targets carry an engine damage estimate
    (``CombatManager.SimulateAttackInto`` — the UI combat preview call),
    unescorted civilians are listed as melee capture targets, and religious
    targets appear only for religious attackers — theological combat needs no
    war, only apostles/inquisitors may initiate it, and the target must
    resolve as ``CombatTypes.RELIGIOUS`` in the simulation.
    """
    return load_lua_template("units.lua").replace(
        "__LUA_OCCUPANCY_CLASS__", _LUA_OCCUPANCY_CLASS
    ).replace("__MCP_SENTINEL_TAG__", SENTINEL)



def build_move_unit(unit_id: int, target_x: int, target_y: int) -> str:
    """InGame context: move a unit to a tile, mirroring the UI's move pipeline.

    The Lua lives in ``build_move_unit.lua`` (loaded via ``load_lua_template``)
    and ports Civ6Common.RequestMoveOperation — the code behind a human
    click/drag move:

    * ``PARAM_MODIFIERS = ATTACK + MOVE_IGNORE_UNEXPLORED_DESTINATION`` is
      always set. The UI never issues ``MOVE_TO`` with a bare ``{X, Y}``
      table, and a bare-params ``CanStartOperation(MOVE_TO)`` immediately
      before an equally bare ``RequestOperation`` left the engine rejecting
      the op while still charging movement (devlog game_004 signature).
    * Swapping places with another owned unit is the dedicated
      ``SWAP_UNITS`` operation, attempted when the destination's occupant
      blocks the mover — the engine is the authority on the other unit's
      movement.
    * Stacking rule (no engine query exists; the UI never checks): the
      effective occupancy class is ``RELIGIOUS`` for religious units
      (``ReligiousStrength > 0`` — same test the game's UI uses), otherwise
      the ``FormationClass``; an occupant blocks the mover iff effective
      classes match. Religious units co-locate with civilians and military
      but conflict with each other (religious behavior unverified — flagged
      in the template for live confirmation).
    * ``MOVE_TO`` itself is fire-and-forget like the UI; move_unit's GameCore
      position readback verifies (a rejected request charges no movement,
      verified live).
    """
    return (
        load_lua_template("build_move_unit.lua")
        .replace("__LUA_OCCUPANCY_CLASS__", _LUA_OCCUPANCY_CLASS)
        .replace("__UNIT_ID__", str(unit_id))
        .replace("__TARGET_X__", str(target_x))
        .replace("__TARGET_Y__", str(target_y))
        .replace("__MCP_SENTINEL_TAG__", SENTINEL)
    )


# ── Tribal village (goody hut) reward capture ─────────────────────────────
#
# Civ 6 grants a reward when a unit enters a tribal village
# (IMPROVEMENT_GOODY_HUT, RemoveOnEntry). The reward subtype is chosen in C++
# and exposed to Lua only through the transient ``GameEvents.UnitTriggerGoodyHut``
# event — there is no player API to query the last reward. So we install a
# persistent listener (GameCore context, same pattern as the turn-handoff
# handler) that logs each reward with a monotonic sequence number. ``move_unit``
# snapshots the sequence before issuing the move and diffs afterward.
#
# Reward amounts below are the base values from GoodyHuts.xml / Expansion2; the
# engine scales them by game speed (and era, for gold/faith), so they are
# approximate. Unknown / mod-added subtypes fall back to the SubTypeGoodyHut
# name or the LOC float-text the listener captured.
GOODY_DESCRIPTIONS: dict[str, str] = {
    # Culture
    "GOODYHUT_ONE_RELIC": "a Relic",
    "GOODYHUT_ONE_CIVIC_BOOST": "1 free civic boost",
    "GOODYHUT_TWO_CIVIC_BOOSTS": "2 free civic boosts",
    # Gold (base amounts, scaled by game speed/era)
    "GOODYHUT_SMALL_GOLD": "Gold (~40, scaled)",
    "GOODYHUT_MEDIUM_GOLD": "Gold (~75, scaled)",
    "GOODYHUT_LARGE_GOLD": "Gold (~120, scaled)",
    # Faith (base amounts, scaled by game speed/era)
    "GOODYHUT_SMALL_FAITH": "Faith (~20, scaled)",
    "GOODYHUT_MEDIUM_FAITH": "Faith (~60, scaled)",
    "GOODYHUT_LARGE_FAITH": "Faith (~100, scaled)",
    # Military
    "GOODYHUT_GRANT_SCOUT": "a Scout unit",
    "GOODYHUT_GRANT_UPGRADE": "a free unit upgrade",
    "GOODYHUT_GRANT_EXPERIENCE": "20 XP for the unit",
    "GOODYHUT_HEAL": "a full heal for the unit",
    "GOODYHUT_RESOURCES": "+20 of your most advanced strategic resource (scaled)",
    # Science
    "GOODYHUT_ONE_TECH": "1 free technology",
    "GOODYHUT_ONE_TECH_BOOST": "1 free tech boost",
    "GOODYHUT_TWO_TECH_BOOSTS": "2 free tech boosts",
    # Survivors
    "GOODYHUT_ADD_POP": "+1 population to the nearest city",
    "GOODYHUT_GRANT_BUILDER": "a Builder unit",
    "GOODYHUT_GRANT_TRADER": "a Trader unit",
    "GOODYHUT_GRANT_SETTLER": "a Settler unit",
    # Diplomacy (Expansion 2)
    "GOODYHUT_GOVERNOR_TITLE": "a Governor title",
    "GOODYHUT_ENVOY": "an Envoy",
    "GOODYHUT_FAVOR": "Diplomatic Favor (~20, scaled)",
}


def build_goody_snapshot_query(target_x: int, target_y: int) -> str:
    """GameCore: ensure the goody-hut listener is installed, then snapshot state.

    Idempotently registers a ``GameEvents.UnitTriggerGoodyHut`` handler that
    appends each reward to the ``__civmcp_goody`` log with a monotonic sequence
    number. Re-running after a save load (Lua state recycled, global nil)
    re-installs automatically.

    Emits ``GOODY_SEQ|<seq>|<expect>`` where ``seq`` is the last sequence
    assigned (0 if none yet) and ``expect`` is 1 when the destination tile
    currently holds a goody hut (so the caller knows to poll if no reward is
    captured immediately).
    """
    return (
        load_lua_template("build_goody_snapshot_query.lua")
        .replace("__TARGET_X__", str(target_x))
        .replace("__TARGET_Y__", str(target_y))
        .replace("__MCP_SENTINEL_TAG__", SENTINEL)
    )


def build_read_goody_log(since_seq: int) -> str:
    """GameCore: emit log entries with sequence > *since_seq*.

    Each line: ``GOODY|seq|turn|player|unit|subtype|category|desc``.
    Emits ``GOODY_NONE`` when there are no new entries.
    """
    return (
        load_lua_template("build_read_goody_log.lua")
        .replace("__SINCE_SEQ__", str(since_seq))
        .replace("__MCP_SENTINEL_TAG__", SENTINEL)
    )


def parse_goody_log(lines: list[str]) -> list[GoodyReward]:
    """Parse ``GOODY|...`` lines (and ignore GOODY_NONE) into GoodyReward list."""
    rewards: list[GoodyReward] = []
    for line in lines:
        if not line.startswith("GOODY|"):
            continue
        parts = line.split("|")
        # GOODY|seq|turn|player|unit|subtype|category|desc
        if len(parts) < 7:
            continue
        try:
            rewards.append(
                GoodyReward(
                    seq=int(parts[1]),
                    turn=int(float(parts[2])),
                    player_id=int(float(parts[3])),
                    unit_id=int(float(parts[4])),
                    subtype=parts[5],
                    category=parts[6],
                    description=parts[7] if len(parts) > 7 else "",
                )
            )
        except (ValueError, IndexError):
            continue
    return rewards


def build_unit_position_query(
    unit_id: int,
    move_target_x: int | None = None,
    move_target_y: int | None = None,
) -> str:
    """GameCore: read a unit's current position.

    When *move_target_x/y* are provided, also diagnoses why a blocked move
    failed (water, mountain, foreign border) so the caller doesn't need a
    second round-trip.
    """
    diag_block = ""
    if move_target_x is not None and move_target_y is not None:
        diag_block = f"""
-- Diagnose blocked move target
pcall(function()
    local plot = Map.GetPlot({move_target_x}, {move_target_y})
    if not plot then print("DIAG|UNKNOWN|tile does not exist"); return end
    if plot:IsWater() then
        local hasShip = false
        pcall(function()
            local tech = GameInfo.Technologies["TECH_SHIPBUILDING"]
            if tech then hasShip = Players[me]:GetTechs():HasTech(tech.Index) end
        end)
        if hasShip then print("DIAG|WATER_OK|water tile (can embark)")
        else print("DIAG|WATER|water tile - land units need Shipbuilding tech to embark") end
    elseif plot:IsMountain() then
        print("DIAG|MOUNTAIN|impassable mountain")
    elseif plot:IsImpassable() then
        print("DIAG|IMPASSABLE|impassable terrain (ice or natural wonder)")
    else
        local owner = plot:GetOwner()
        if owner >= 0 and owner ~= me then
            local atWar = false
            pcall(function() atWar = Players[me]:GetDiplomacy():IsAtWarWith(owner) end)
            if atWar then
                print("DIAG|UNKNOWN|tile is enemy territory but movement still blocked - check path")
            else
                local civName = "player " .. owner
                pcall(function()
                    local cfg = PlayerConfigurations[owner]
                    civName = cfg and Locale.Lookup(cfg:GetCivilizationShortDescription()) or civName
                end)
                local isMajor = true
                pcall(function() isMajor = Players[owner]:IsMajor() end)
                if isMajor then
                    print("DIAG|BORDER|foreign territory (" .. civName .. ") - need Open Borders via propose_trade")
                else
                    print("DIAG|BORDER_CS|city-state territory (" .. civName .. ") - need suzerainty or Open Borders")
                end
            end
        else
            print("DIAG|UNKNOWN|tile appears passable - path may be blocked by intermediate tiles")
        end
    end
end)
"""
    return f"""
local me = Game.GetLocalPlayer()
local u = Players[me]:GetUnits():FindID({unit_id})
if u then print("POS|" .. u:GetX() .. "|" .. u:GetY()) else print("POS|GONE") end
{diag_block}print("{SENTINEL}")
"""


def build_attack_unit(unit_id: int, target_x: int, target_y: int) -> str:
    return f"""
{_lua_get_unit(unit_id)}
local ux, uy = unit:GetX(), unit:GetY()
local dist = Map.GetPlotDistance(ux, uy, {target_x}, {target_y})
-- Find hostile unit on target tile (prefer military over civilian)
local enemy = nil
local enemyName = "unknown"
local tgtUnits = Map.GetUnitsAt({target_x}, {target_y})
if tgtUnits then
    local fallback = nil
    local fallbackName = "unknown"
    for other in tgtUnits:Units() do
        if other:GetOwner() ~= me then
            local eInfo = GameInfo.Units[other:GetType()]
            local eName = eInfo and eInfo.UnitType or "UNKNOWN"
            local eCombat = eInfo and eInfo.Combat or 0
            if eCombat > 0 then
                enemy = other
                enemyName = eName
                break
            elseif fallback == nil then
                fallback = other
                fallbackName = eName
            end
        end
    end
    if enemy == nil and fallback then enemy = fallback; enemyName = fallbackName end
end
if enemy == nil then
    {_bail(f"ERR:NO_ENEMY|No hostile unit at ({target_x},{target_y})")}
end
-- Check diplomatic status — can only attack units you're at war with (barbarians always attackable)
local enemyOwner = enemy:GetOwner()
if enemyOwner ~= 63 then
    local pDiplo = Players[me]:GetDiplomacy()
    if not pDiplo:IsAtWarWith(enemyOwner) then
        local ownerCfg = PlayerConfigurations[enemyOwner]
        local ownerName = ownerCfg and Locale.Lookup(ownerCfg:GetCivilizationDescription()) or ("player " .. enemyOwner)
        {_bail_lua('"ERR:NOT_AT_WAR|Cannot attack " .. enemyName .. " — you are at peace with " .. ownerName .. ". Declare war first or target a different unit."')}
    end
end
local enemyHP = enemy:GetMaxDamage() - enemy:GetDamage()
local enemyMaxHP = enemy:GetMaxDamage()
local myHP = unit:GetMaxDamage() - unit:GetDamage()
local params = {{}}
params[UnitOperationTypes.PARAM_X] = {target_x}
params[UnitOperationTypes.PARAM_Y] = {target_y}
-- Determine attack type
local unitInfo = GameInfo.Units[unit:GetType()]
local isRanged = UnitManager.CanStartOperation(unit, UnitOperationTypes.RANGE_ATTACK, nil, true)
local isAir = (not isRanged) and UnitManager.CanStartOperation(unit, UnitOperationTypes.AIR_ATTACK, nil, params)
if isRanged then
    if unit:GetMovesRemaining() <= 0 then
        {_bail("ERR:NO_MOVES|Unit has no movement points for ranged attack. Ranged attacks require movement. Move and attack on separate turns, or attack before moving.")}
    end
    local rng = unitInfo and unitInfo.Range or 1
    if dist > rng then
        {_bail_lua('"ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but range is " .. rng .. ". Move closer first."')}
    end
    -- LOS check: CanStartOperation with target params is authoritative;
    -- GetOperationTargets returns empty for some valid targets (naval units, etc.)
    local losParams = {{}}
    losParams[UnitOperationTypes.PARAM_X] = {target_x}
    losParams[UnitOperationTypes.PARAM_Y] = {target_y}
    local canRanged = UnitManager.CanStartOperation(unit, UnitOperationTypes.RANGE_ATTACK, nil, losParams)
    if canRanged then
        UnitManager.RequestOperation(unit, UnitOperationTypes.RANGE_ATTACK, params)
        print("OK:RANGE_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})|pre_hp:" .. enemyHP .. "/" .. enemyMaxHP .. "|your HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
        print("{SENTINEL}"); return
    elseif dist <= 1 then
        -- Ranged failed at melee range: fall through to melee attack below
        isRanged = false
    else
        {_bail_lua(f'"ERR:NO_LOS|Cannot ranged-attack target at ({target_x},{target_y}) from (" .. ux .. "," .. uy .. "). LOS blocked or unit already attacked this turn."')}
    end
end
if isAir then
    -- Air units (jet bombers, jet fighters, bombers, fighters): use AIR_ATTACK operation.
    -- Combat resolves asynchronously in the UI so post-combat HP reads may be stale.
    local rng = unitInfo and unitInfo.Range or 1
    if dist > rng then
        {_bail_lua('"ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but air range is " .. rng .. ". Rebase closer first."')}
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.AIR_ATTACK, params)
    print("OK:AIR_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})|pre_hp:" .. enemyHP .. "/" .. enemyMaxHP .. "|bomber HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
else
    -- Melee: let CanStartOperation be the authority on adjacency/validity.
    -- Map.GetPlotDistance can misreport distance on offset hex grids, so we
    -- do not use it as a gate here — only as a diagnostic in the error message.
    local myCS = unitInfo and unitInfo.Combat or 0
    -- Movement check: melee attack requires movement points (ranged does not)
    if unit:GetMovesRemaining() <= 0 then
        {_bail("ERR:NO_MOVES|Unit has no movement points for melee attack. Melee requires movement to close distance. Wait until next turn.")}
    end
    params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
    if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO, nil, params) then
        {_bail_lua('"ERR:ATTACK_BLOCKED|Cannot attack " .. enemyName .. " at ({target_x},{target_y}) (map dist=" .. dist .. "). Unit not adjacent or blocked by popup/diplomacy."')}
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
    -- Verify unit reached adjacency (MOVE_TO resolves synchronously for movement)
    local newX, newY = unit:GetX(), unit:GetY()
    local newDist = Map.GetPlotDistance(newX, newY, {target_x}, {target_y})
    if newDist > 1 then
        print("ERR:STOPPED_SHORT|Unit moved to (" .. newX .. "," .. newY .. ") but could not reach target at ({target_x},{target_y}) — " .. newDist .. " tiles away. Movement exhausted by terrain. Try again next turn from closer position.")
        print("{SENTINEL}"); return
    end
    -- Post-combat HP is NOT readable in InGame state within the same turn
    -- (combat resolves asynchronously). Only pre-attack HP is reported here;
    -- the caller reads the true post-combat state from GameCore.
    print("OK:MELEE_ATTACK|target:" .. enemyName .. " at ({target_x},{target_y})|pre_hp:" .. enemyHP .. "/" .. enemyMaxHP .. "|your HP:" .. myHP .. "|CS:" .. myCS)
end
print("{SENTINEL}")
"""


def build_attack_followup_query(target_x: int, target_y: int) -> str:
    """InGame context: get actual HP of units at target tile after combat.

    Also checks for city defenses (walls/garrison) at the target — when
    attacking a walled city, damage goes to walls first so the garrison
    unit's HP stays unchanged even though the attack succeeded.

    Runs in InGame context because enemy city district APIs
    (GetDistricts, GetMaxDamage) are not available in GameCore.
    """
    return f"""
local found = false
for i = 0, 63 do
    if Players[i] and Players[i]:IsAlive() then
        for _, u in Players[i]:GetUnits():Members() do
            if u:GetX() == {target_x} and u:GetY() == {target_y} then
                local hp = u:GetMaxDamage() - u:GetDamage()
                local entry = GameInfo.Units[u:GetType()]
                local name = entry and entry.UnitType or "UNKNOWN"
                print("UNIT|" .. name .. "|" .. hp .. "/" .. u:GetMaxDamage() .. "|owner:" .. i)
                found = true
            end
        end
        pcall(function()
            for _, c in Players[i]:GetCities():Members() do
                if c:GetX() == {target_x} and c:GetY() == {target_y} then
                    local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
                    for _, d in c:GetDistricts():Members() do
                        if d:GetType() == ccIdx then
                            pcall(function()
                                local wMax = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                local wHP = wMax - (d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0)
                                local gMax = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                                local gHP = gMax - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                                if wMax > 0 or gMax > 0 then
                                    print("CITY_DEF|wall:" .. wHP .. "/" .. wMax .. "|garrison:" .. gHP .. "/" .. gMax)
                                end
                            end)
                            break
                        end
                    end
                end
            end
        end)
    end
end
if not found then print("EMPTY") end
print("{SENTINEL}")
"""


def build_attack_outcome_query(
    attacker_unit_id: int, target_x: int, target_y: int
) -> str:
    """GameCore context: read true post-combat HP after an attack resolves.

    InGame state does not reflect post-combat HP within the same turn, but
    GameCore (authoritative sim state) does. Reads the attacker's real HP and
    any enemy unit remaining on the target tile (our own units on the tile are
    skipped — after a melee kill the attacker advances onto the target tile).

    Emits ``OUTCOME|att_hp:N|att_max:N|enemy:TYPE|enemy_hp:N|enemy_max:N`` or
    ``OUTCOME|att_hp:N|att_max:N|enemy:KILLED`` when no enemy remains, plus
    ``CITY|1`` when the target tile is a city center (caller fetches wall HP
    from InGame via build_attack_followup_query).
    """
    return f"""
local me = Game.GetLocalPlayer()
local attacker = Players[me]:GetUnits():FindID({attacker_unit_id})
local attHP, attMax = -1, -1
if attacker then
    attMax = attacker:GetMaxDamage()
    attHP = attMax - attacker:GetDamage()
end
local enemyFound = false
local enemyName = "KILLED"
local enemyHP, enemyMax = 0, 0
for i = 0, 63 do
    if Players[i] and Players[i]:IsAlive() and i ~= me then
        for _, u in Players[i]:GetUnits():Members() do
            if u:GetX() == {target_x} and u:GetY() == {target_y} then
                local entry = GameInfo.Units[u:GetType()]
                enemyName = entry and entry.UnitType or "UNKNOWN"
                enemyMax = u:GetMaxDamage()
                enemyHP = enemyMax - u:GetDamage()
                enemyFound = true
                break
            end
        end
    end
    if enemyFound then break end
end
local out = "OUTCOME|att_hp:" .. attHP .. "|att_max:" .. attMax .. "|enemy:"
if enemyFound then
    out = out .. enemyName .. "|enemy_hp:" .. enemyHP .. "|enemy_max:" .. enemyMax
else
    out = out .. "KILLED"
end
print(out)
local plot = Map.GetPlot({target_x}, {target_y})
if plot and plot:IsCity() then print("CITY|1") end
print("{SENTINEL}")
"""


def parse_attack_outcome(lines: list[str]) -> AttackOutcome | None:
    """Parse OUTCOME| line (and optional CITY|) from build_attack_outcome_query."""
    outcome: AttackOutcome | None = None
    is_city = False
    for line in lines:
        if line.startswith("OUTCOME|"):
            # OUTCOME|att_hp:N|att_max:N|enemy:TYPE|enemy_hp:N|enemy_max:N
            # OUTCOME|att_hp:N|att_max:N|enemy:KILLED
            parts = line.split("|")
            fields: dict[str, str] = {}
            for p in parts[1:]:
                if ":" in p:
                    k, _, v = p.partition(":")
                    fields[k] = v
            att_hp = int(fields.get("att_hp", "-1") or "-1")
            att_max = int(fields.get("att_max", "-1") or "-1")
            enemy = fields.get("enemy", "KILLED")
            if enemy == "KILLED" or "enemy_hp" not in fields:
                outcome = AttackOutcome(
                    attacker_hp=att_hp,
                    attacker_max=att_max,
                    enemy_present=False,
                    is_city=is_city,
                )
            else:
                outcome = AttackOutcome(
                    attacker_hp=att_hp,
                    attacker_max=att_max,
                    enemy_present=True,
                    enemy_type=enemy,
                    enemy_hp=int(fields["enemy_hp"] or "0"),
                    enemy_max=int(fields.get("enemy_max", "0") or "0"),
                    is_city=is_city,
                )
        elif line.startswith("CITY|"):
            is_city = True
            if outcome is not None:
                outcome.is_city = True
    return outcome


def parse_blocked_diagnostic(lines: list[str]) -> str:
    """Extract human-readable block reason from diagnostic Lua output."""
    for line in lines:
        if line.startswith("DIAG|"):
            parts = line.split("|", 2)
            if len(parts) >= 3:
                return parts[2]
    return "unit did not move — impassable terrain, border, or no path"


def build_threat_scan_query() -> str:
    """GameCore: scan for foreign military units visible to the player.

    Scans all players (not just barbarians) but only reports units on tiles
    the player can currently see (PlayersVisibility:IsVisible). No arbitrary
    distance limits — fog of war is the natural filter.

    Uses GameCore context but filters by fog of war — only reports units
    on tiles the player can currently see (PlayersVisibility:IsVisible).
    Reports owner, HP, combat strength, and distance from nearest friendly position.
    """
    return """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local myPos = {}
for _, c in Players[me]:GetCities():Members() do
    table.insert(myPos, {c:GetX(), c:GetY()})
end
for _, u in Players[me]:GetUnits():Members() do
    local ux, uy = u:GetX(), u:GetY()
    if ux ~= -9999 then table.insert(myPos, {ux, uy}) end
end
local found = false
for pid = 0, 63 do
    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
        local isMajor = Players[pid]:IsMajor()
        local isBarbarian = (pid == 63)
        -- Skip city-state units unless we're at war with them
        if not isMajor and not isBarbarian and not pDiplo:IsAtWarWith(pid) then
            -- City-state, not at war — not a threat
        else
        local ownerName = "Barbarian"
        if pid ~= 63 then
            local cfg = PlayerConfigurations[pid]
            if cfg then ownerName = Locale.Lookup(cfg:GetCivilizationShortDescription()) end
        end
        for _, bu in Players[pid]:GetUnits():Members() do
            local bx, by = bu:GetX(), bu:GetY()
            if bx ~= -9999 and pVis:IsVisible(bx, by) then
                local uType = bu:GetType()
                if uType then
                    local entry = GameInfo.Units[uType]
                    local bcs = entry and entry.Combat or 0
                    if bcs > 0 or (entry and entry.RangedCombat and entry.RangedCombat > 0) then
                        local minDist = 999
                        for _, pos in ipairs(myPos) do
                            local d = Map.GetPlotDistance(pos[1], pos[2], bx, by)
                            if d < minDist then minDist = d end
                        end
                        local name = entry and entry.UnitType or "UNKNOWN"
                        local hp = bu:GetMaxDamage() - bu:GetDamage()
                        local brs = entry and entry.RangedCombat or 0
                        local isCS = Players[pid]:IsMajor() and "0" or "1"
                        print("THREAT|" .. pid .. "|" .. ownerName:gsub("|","/") .. "|" .. name .. "|" .. bx .. "," .. by .. "|" .. hp .. "/" .. bu:GetMaxDamage() .. "|CS:" .. bcs .. "|RS:" .. brs .. "|dist:" .. minDist .. "|cs:" .. isCS .. "|uid:" .. bu:GetID())
                        found = true
                    end
                end
            end
        end
        end -- close city-state skip if/else
    end -- close if pid alive
end -- close for pid
if not found then print("NO_THREATS") end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_fortify_unit(unit_id: int) -> str:
    return f"""
{_lua_get_unit(unit_id)}
if unit:GetFortifyTurns() > 0 then
    print("OK:ALREADY_FORTIFIED|Fortify turns: " .. unit:GetFortifyTurns())
    print("{SENTINEL}"); return
end
if UnitManager.CanStartOperation(unit, UnitOperationTypes.FORTIFY, nil, true) then
    UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
    print("OK:FORTIFIED")
else
    local sleepOp = GameInfo.UnitOperations["UNITOPERATION_SLEEP"]
    if sleepOp and UnitManager.CanStartOperation(unit, sleepOp.Hash, nil, true) then
        UnitManager.RequestOperation(unit, sleepOp.Hash)
        print("OK:SLEEPING")
    else
        {_bail("ERR:CANNOT_FORTIFY|Unit cannot fortify or sleep")}
    end
end
print("{SENTINEL}")
"""


def build_skip_unit(unit_id: int) -> str:
    """Skip a unit's turn (GameCore context — uses FinishMoves)."""
    return f"""
{_lua_get_unit_gamecore(unit_id)}
UnitManager.FinishMoves(unit)
print("OK:SKIPPED")
print("{SENTINEL}")
"""


def build_exit_formation(unit_id: int) -> str:
    """Exit the current formation (unlink from escort partner)."""
    return f"""
{_lua_get_unit(unit_id)}
if not UnitManager.CanStartCommand(unit, UnitCommandTypes.EXIT_FORMATION, me, true) then
    {_bail("ERR:NOT_IN_FORMATION|Unit is not in a formation")}
end
local ux, uy = unit:GetX(), unit:GetY()
UnitManager.RequestCommand(unit, UnitCommandTypes.EXIT_FORMATION, {{}})
print("OK:EXITED_FORMATION|" .. ux .. "," .. uy)
print("{SENTINEL}")
"""


def build_enter_formation(unit_id: int, target_unit_id: int) -> str:
    """Enter a formation with another unit (escort/link)."""
    return f"""
{_lua_get_unit(unit_id)}
-- Find the target unit by its per-player ID
local target = nil
for _, u in Players[me]:GetUnits():Members() do
    if u:GetID() == {target_unit_id} and u:GetX() ~= -9999 then
        target = u
        break
    end
end
if not target then
    {_bail(f"ERR:TARGET_NOT_FOUND|Unit with id {target_unit_id} not found")}
end
local tx, ty = target:GetX(), target:GetY()
local ux, uy = unit:GetX(), unit:GetY()
if ux ~= tx or uy ~= ty then
    {_bail_lua(f'"ERR:NOT_ON_SAME_TILE|Units must be on the same tile to form a formation. Unit at (" .. ux .. "," .. uy .. "), target at (" .. tx .. "," .. ty .. ")")')}
end
-- Prevent stacking same formation class (already checked in move but re-check here)
local unitInfo = GameInfo.Units[unit:GetType()]
local targetInfo = GameInfo.Units[target:GetType()]
local unitClass = unitInfo and unitInfo.FormationClass or ""
local targetClass = targetInfo and targetInfo.FormationClass or ""
if unitClass == targetClass then
    {_bail_lua('"ERR:SAME_FORMATION_CLASS|Cannot link two units of the same formation class (" .. unitClass .. ")"')}
end
if UnitManager.CanStartCommand(unit, UnitCommandTypes.ENTER_FORMATION, me, true) then
    local params = {{}}
    params[UnitCommandTypes.PARAM_UNIT_ID] = target:GetID()
    UnitManager.RequestCommand(unit, UnitCommandTypes.ENTER_FORMATION, params)
    local uName = unitInfo and unitInfo.UnitType:gsub("UNIT_", "") or "unit"
    local tName = targetInfo and targetInfo.UnitType:gsub("UNIT_", "") or "unit"
    print("OK:ENTERED_FORMATION|" .. uName .. " linked with " .. tName)
else
    local uName = unitInfo and unitInfo.UnitType:gsub("UNIT_", "") or "unit"
    print("ERR:CANNOT_ENTER_FORMATION|" .. uName .. " cannot enter formation — check units are adjacent, not already linked, and compatible")
end
print("{SENTINEL}")
"""


def build_fortify_remaining_units() -> str:
    """Fortify/heal combat units with remaining moves (InGame context).

    Tries to fortify (or heal if damaged) combat units. Non-combat units
    and units that can't fortify are left for skip_remaining_units to handle.
    """
    return """
local me = Game.GetLocalPlayer()
local fortified = 0
local healed = 0
local healHash = GameInfo.UnitOperations["UNITOPERATION_HEAL"] and GameInfo.UnitOperations["UNITOPERATION_HEAL"].Hash
for _, unit in Players[me]:GetUnits():Members() do
    local x = unit:GetX()
    if x ~= -9999 and unit:GetMovesRemaining() > 0 then
        local info = GameInfo.Units[unit:GetType()]
        local isCombat = info and info.Combat > 0
        if isCombat then
            if unit:GetDamage() > 0 and healHash then
                local ok = pcall(function()
                    if UnitManager.CanStartOperation(unit, healHash, nil, true) then
                        UnitManager.RequestOperation(unit, healHash)
                        healed = healed + 1
                    end
                end)
            else
                local ok = pcall(function()
                    if UnitManager.CanStartOperation(unit, UnitOperationTypes.FORTIFY, nil, true) then
                        UnitManager.RequestOperation(unit, UnitOperationTypes.FORTIFY)
                        fortified = fortified + 1
                    end
                end)
            end
        end
    end
end
print("OK:FORTIFIED|" .. fortified .. " fortified, " .. healed .. " healing")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_skip_remaining_units() -> str:
    """Skip all units with moves remaining (GameCore context — FinishMoves for each)."""
    return """
local me = Game.GetLocalPlayer()
local count = 0
for _, unit in Players[me]:GetUnits():Members() do
    local x = unit:GetX()
    if x ~= -9999 and unit:GetMovesRemaining() > 0 then
        UnitManager.FinishMoves(unit)
        count = count + 1
    end
end
print("OK:SKIPPED|" .. count .. " units")
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def build_automate_explore(unit_id: int) -> str:
    """Automate a unit's exploration (InGame context)."""
    return f"""
{_lua_get_unit(unit_id)}
local hash = GameInfo.UnitOperations["UNITOPERATION_AUTOMATE_EXPLORE"].Hash
if not UnitManager.CanStartOperation(unit, hash, nil, nil) then
    {_bail("ERR:CANNOT_AUTOMATE|Unit cannot auto-explore")}
end
UnitManager.RequestOperation(unit, hash, {{}})
print("OK:AUTOMATED|" .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_heal_unit(unit_id: int) -> str:
    """Fortify until healed (InGame context). Distinct from plain fortify."""
    return f"""
{_lua_get_unit(unit_id)}
local hp = unit:GetMaxDamage() - unit:GetDamage()
local maxHP = unit:GetMaxDamage()
if hp >= maxHP then {_bail_lua('"ERR:FULL_HP|Unit already at full health (" .. hp .. "/" .. maxHP .. ")"')} end
local healHash = GameInfo.UnitOperations["UNITOPERATION_HEAL"].Hash
if UnitManager.CanStartOperation(unit, healHash, nil, nil) then
    UnitManager.RequestOperation(unit, healHash, {{}})
    print("OK:HEALING|HP:" .. hp .. "/" .. maxHP)
else
    {_bail("ERR:CANNOT_HEAL|Unit cannot fortify-until-healed (maybe already fortified?)")}
end
print("{SENTINEL}")
"""


def build_alert_unit(unit_id: int) -> str:
    """Put unit on alert — sleeps but auto-wakes when enemy enters sight (InGame context)."""
    return f"""
{_lua_get_unit(unit_id)}
if UnitManager.CanStartOperation(unit, UnitOperationTypes.ALERT, nil, nil) then
    UnitManager.RequestOperation(unit, UnitOperationTypes.ALERT, {{}})
    print("OK:ALERT|" .. unit:GetX() .. "," .. unit:GetY())
else
    {_bail("ERR:CANNOT_ALERT|Unit cannot be put on alert")}
end
print("{SENTINEL}")
"""


def build_sleep_unit(unit_id: int) -> str:
    """Put unit to sleep — stays until manually woken (InGame context)."""
    return f"""
{_lua_get_unit(unit_id)}
local sleepHash = GameInfo.UnitOperations["UNITOPERATION_SLEEP"].Hash
if UnitManager.CanStartOperation(unit, sleepHash, nil, nil) then
    UnitManager.RequestOperation(unit, sleepHash, {{}})
    print("OK:SLEEPING|" .. unit:GetX() .. "," .. unit:GetY())
else
    {_bail("ERR:CANNOT_SLEEP|Unit cannot sleep")}
end
print("{SENTINEL}")
"""


def build_delete_unit(unit_id: int) -> str:
    """Delete (disband) a unit (InGame context)."""
    return f"""
{_lua_get_unit(unit_id)}
local unitInfo = GameInfo.Units[unit:GetType()]
local uName = unitInfo and unitInfo.UnitType or "UNKNOWN"
if UnitManager.CanStartCommand(unit, UnitCommandTypes.DELETE, true) then
    UnitManager.RequestCommand(unit, UnitCommandTypes.DELETE)
    print("OK:DELETED|" .. uName .. " at " .. unit:GetX() .. "," .. unit:GetY())
else
    {_bail("ERR:CANNOT_DELETE|Unit cannot be deleted")}
end
print("{SENTINEL}")
"""


def build_improve_tile(unit_id: int, improvement_name: str) -> str:
    """Build an improvement with a builder unit (InGame context).

    improvement_name is e.g. IMPROVEMENT_FARM, IMPROVEMENT_MINE, etc.
    """
    return f"""
{_lua_get_unit(unit_id)}
local imp = GameInfo.Improvements["{improvement_name}"]
if imp == nil then
    -- Feature removals (IMPROVEMENT_REMOVE_*) may not be in Improvements table.
    -- Try scanning by ImprovementType name in case indexed lookup fails.
    for row in GameInfo.Improvements() do
        if row.ImprovementType == "{improvement_name}" then imp = row; break end
    end
    if imp == nil then
        -- List all available improvements so the agent can find the correct name
        local available = {{}}
        local params0 = {{}}
        params0[UnitOperationTypes.PARAM_X] = unit:GetX()
        params0[UnitOperationTypes.PARAM_Y] = unit:GetY()
        for row in GameInfo.Improvements() do
            if row.Buildable then
                params0[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = row.Hash
                local ok2, canBuild2 = pcall(function()
                    return UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, params0)
                end)
                if ok2 and canBuild2 then table.insert(available, row.ImprovementType) end
            end
        end
        local hint = #available > 0 and ". Available here: " .. table.concat(available, ", ") or ""
        {_bail_lua(f'"ERR:IMPROVEMENT_NOT_FOUND|{improvement_name} not in game database" .. hint')}
    end
end
local plot = Map.GetPlot(unit:GetX(), unit:GetY())
if plot:GetOwner() ~= me then {_bail_lua('"ERR:NOT_YOUR_TERRITORY|Tile at " .. unit:GetX() .. "," .. unit:GetY() .. " is not in your territory"')} end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = unit:GetX()
params[UnitOperationTypes.PARAM_Y] = unit:GetY()
params[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
if plot:IsImprovementPillaged() then
    local repairHash = GameInfo.UnitOperations["UNITOPERATION_REPAIR"] and GameInfo.UnitOperations["UNITOPERATION_REPAIR"].Hash
    if repairHash then
        local rParams = {{}}
        rParams[UnitOperationTypes.PARAM_X] = unit:GetX()
        rParams[UnitOperationTypes.PARAM_Y] = unit:GetY()
        -- Include improvement type — REPAIR may need to know WHICH improvement to restore
        local impType = plot:GetImprovementType()
        if impType >= 0 then
            local impRow = GameInfo.Improvements[impType]
            if impRow then rParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = impRow.Hash end
        end
        local canRepair = UnitManager.CanStartOperation(unit, repairHash, nil, rParams)
        if canRepair then
            UnitManager.RequestOperation(unit, repairHash, rParams)
            print("OK:REPAIRING|{improvement_name}|" .. unit:GetX() .. "," .. unit:GetY())
            print("{SENTINEL}"); return
        else
            -- CanStartOperation is unreliable (stale InGame state) — attempt anyway
            pcall(function() UnitManager.RequestOperation(unit, repairHash, rParams) end)
            -- Check if it worked by re-reading pillage state next frame
            print("WARN:REPAIR_ATTEMPTED|CanStartOperation=false but RequestOperation sent. Verify next turn.")
            print("{SENTINEL}"); return
        end
    end
end
if unit:GetMovesRemaining() <= 0 then
    print("ERR:CANNOT_IMPROVE|Builder has no moves remaining this turn")
    print("{SENTINEL}"); return
end
local canBuild, opResult = UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, params, true)
if not canBuild then
    local reasons = {{}}
    if opResult and opResult.FailureReasons then
        for _, r in ipairs(opResult.FailureReasons) do
            table.insert(reasons, tostring(r))
        end
    end
    local reasonStr = #reasons > 0 and table.concat(reasons, "; ") or "unknown reason"
    -- Add diagnostic context
    local diag = {{}}
    local charges = unit:GetBuildCharges()
    if charges <= 0 then
        table.insert(diag, "builder has 0 charges (will be consumed)")
    end
    local existImp = plot:GetImprovementType()
    if existImp >= 0 then
        local eiRow = GameInfo.Improvements[existImp]
        table.insert(diag, "tile already has " .. (eiRow and eiRow.ImprovementType or "improvement"))
    end
    local fType = plot:GetFeatureType()
    if fType >= 0 then
        local fInfo = GameInfo.Features[fType]
        local fName = fInfo and fInfo.FeatureType or "UNKNOWN"
        table.insert(diag, "tile has " .. fName .. " (use remove_feature first)")
    end
    -- List what CAN be built here
    local alts = {{}}
    for altImp in GameInfo.Improvements() do
        if altImp.Buildable and not altImp.TraitType then
            local aParams = {{}}
            aParams[UnitOperationTypes.PARAM_X] = unit:GetX()
            aParams[UnitOperationTypes.PARAM_Y] = unit:GetY()
            aParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = altImp.Hash
            local ok3, canAlt = pcall(function()
                return UnitManager.CanStartOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, nil, aParams)
            end)
            if ok3 and canAlt then table.insert(alts, altImp.ImprovementType) end
        end
    end
    if #alts > 0 then
        table.insert(diag, "can build here: " .. table.concat(alts, ", "))
    else
        table.insert(diag, "no improvements can be built on this tile")
    end
    local diagStr = #diag > 0 and ". " .. table.concat(diag, ". ") or ""
    print("ERR:CANNOT_IMPROVE|" .. reasonStr .. diagStr .. ". Builder at " .. unit:GetX() .. "," .. unit:GetY())
    print("{SENTINEL}"); return
end
UnitManager.RequestOperation(unit, UnitOperationTypes.BUILD_IMPROVEMENT, params)
print("OK:IMPROVING|{improvement_name}|" .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_remove_feature(unit_id: int) -> str:
    """Remove (chop/harvest) a feature from the tile the builder is standing on.

    Uses UNITOPERATION_REMOVE_FEATURE — works on forest, jungle, marsh.
    The game auto-detects which feature is present; no feature param needed.
    """
    return f"""
{_lua_get_unit(unit_id)}
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(unit:GetX(), unit:GetY())
local fType = plot:GetFeatureType()
if fType < 0 then
    {_bail_lua('"ERR:NO_FEATURE|No feature on tile (" .. unit:GetX() .. "," .. unit:GetY() .. ") to remove"')}
end
local fInfo = GameInfo.Features[fType]
local fName = fInfo and fInfo.FeatureType or "UNKNOWN"
local opRow = GameInfo.UnitOperations["UNITOPERATION_REMOVE_FEATURE"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REMOVE_FEATURE not available")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = unit:GetX()
params[UnitOperationTypes.PARAM_Y] = unit:GetY()
local canStart = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    {_bail_lua('"ERR:CANNOT_REMOVE|Cannot remove " .. fName .. " at (" .. unit:GetX() .. "," .. unit:GetY() .. ")"')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
print("OK:REMOVING_FEATURE|" .. fName .. " at " .. unit:GetX() .. "," .. unit:GetY())
print("{SENTINEL}")
"""


def build_repair_improvement(unit_id: int) -> str:
    """Repair a pillaged improvement at the builder's current tile (InGame context).

    Auto-detects the pillaged improvement — no improvement name needed.
    """
    return f"""
{_lua_get_unit(unit_id)}
local ux, uy = unit:GetX(), unit:GetY()
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(ux, uy)
if not plot then {_bail("ERR:NO_PLOT|Invalid plot")} end
local impType = plot:GetImprovementType()
if impType < 0 then
    {_bail_lua('"ERR:NO_IMPROVEMENT|No improvement on tile (" .. ux .. "," .. uy .. ") to repair"')}
end
local okPil, isPillaged = pcall(function() return plot:IsImprovementPillaged() end)
if not okPil or not isPillaged then
    local impInfo = GameInfo.Improvements[impType]
    local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
    {_bail_lua('"ERR:NOT_PILLAGED|" .. impName .. " at (" .. ux .. "," .. uy .. ") is not pillaged"')}
end
local impInfo = GameInfo.Improvements[impType]
local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
local repairOp = GameInfo.UnitOperations["UNITOPERATION_REPAIR"]
if not repairOp then {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REPAIR not available")} end
local rParams = {{}}
rParams[UnitOperationTypes.PARAM_X] = ux
rParams[UnitOperationTypes.PARAM_Y] = uy
if impInfo then rParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = impInfo.Hash end
local canRepair = UnitManager.CanStartOperation(unit, repairOp.Hash, nil, rParams)
if canRepair then
    UnitManager.RequestOperation(unit, repairOp.Hash, rParams)
    print("OK:REPAIRING|" .. impName .. " at (" .. ux .. "," .. uy .. ")")
else
    pcall(function() UnitManager.RequestOperation(unit, repairOp.Hash, rParams) end)
    print("WARN:REPAIR_ATTEMPTED|CanStartOperation=false but RequestOperation sent for " .. impName .. " at (" .. ux .. "," .. uy .. "). Verify next turn.")
end
print("{SENTINEL}")
"""


def build_remove_improvement(unit_id: int) -> str:
    """Remove (demolish) an intact improvement from the builder's current tile.

    Uses UNITOPERATION_REMOVE_IMPROVEMENT. The game auto-detects which
    improvement is present; no improvement param needed. Costs one builder charge.
    """
    return f"""
{_lua_get_unit(unit_id)}
local ux, uy = unit:GetX(), unit:GetY()
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")}
end
local plot = Map.GetPlot(ux, uy)
if not plot then {_bail("ERR:NO_PLOT|Invalid plot")} end
local impType = plot:GetImprovementType()
if impType < 0 then
    {_bail_lua('"ERR:NO_IMPROVEMENT|No improvement on tile (" .. ux .. "," .. uy .. ") to remove"')}
end
local impInfo = GameInfo.Improvements[impType]
local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
local opRow = GameInfo.UnitOperations["UNITOPERATION_REMOVE_IMPROVEMENT"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_REMOVE_IMPROVEMENT not available in this game version")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = ux
params[UnitOperationTypes.PARAM_Y] = uy
local canStart = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    {_bail_lua('"ERR:CANNOT_REMOVE|Cannot remove " .. impName .. " at (" .. ux .. "," .. uy .. "). Builder must be on the tile with moves and charges."')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
print("OK:REMOVING_IMPROVEMENT|" .. impName .. " at (" .. ux .. "," .. uy .. ")")
print("{SENTINEL}")
"""


def build_sacrifice_builder_charges(unit_id: int) -> str:
    """Sacrifice builder charges to boost a district project (Royal Society).

    Requires the Royal Society (BUILDING_GOV_SCIENCE) to be built.
    Builder must be on the district tile where a project is actively building.
    Consumes ALL remaining charges. Once per city per turn.
    Each charge adds 2% of the project's production cost.
    """
    return f"""
{_lua_get_unit(unit_id)}
local entry = GameInfo.Units[unit:GetType()]
if not entry or entry.UnitType ~= "UNIT_BUILDER" then {_bail("ERR:NOT_A_BUILDER|Unit is not a builder")} end
local ux, uy = unit:GetX(), unit:GetY()
local charges = unit:GetBuildCharges()
if charges <= 0 then {_bail("ERR:NO_CHARGES|Builder has no charges remaining")} end
if unit:GetMovesRemaining() <= 0 then {_bail("ERR:NO_MOVES|Builder has no moves remaining this turn")} end
-- Verify Royal Society exists
local hasRS = false
local rsIdx = GameInfo.Buildings["BUILDING_GOV_SCIENCE"] and GameInfo.Buildings["BUILDING_GOV_SCIENCE"].Index
if rsIdx then
    for _, city in Players[me]:GetCities():Members() do
        if city:GetBuildings():HasBuilding(rsIdx) then hasRS = true; break end
    end
end
if not hasRS then {_bail("ERR:NO_ROYAL_SOCIETY|Royal Society (Tier 3 government building) required")} end
-- Check builder is on a district tile
local plot = Map.GetPlot(ux, uy)
local distType = plot:GetDistrictType()
if distType < 0 then
    {_bail_lua('"ERR:NOT_ON_DISTRICT|Builder at (" .. ux .. "," .. uy .. ") is not on a district tile. Move to the district with an active project."')}
end
local dInfo = GameInfo.Districts[distType]
local dName = dInfo and dInfo.DistrictType or "UNKNOWN"
-- Find the city owning this plot and check for active project
local cityOwner = nil
for _, city in Players[me]:GetCities():Members() do
    for _, d in city:GetDistricts():Members() do
        if d:GetX() == ux and d:GetY() == uy then cityOwner = city; break end
    end
    if cityOwner then break end
end
if not cityOwner then {_bail("ERR:NO_CITY|Could not find city owning this district")} end
local bq = cityOwner:GetBuildQueue()
local producing = "nothing"
local okProd, currentHash = pcall(function() return bq:GetCurrentProductionTypeHash() end)
if okProd and currentHash then
    for proj in GameInfo.Projects() do
        if proj.Hash == currentHash then producing = proj.ProjectType; break end
    end
end
if producing == "nothing" then
    {_bail_lua('"ERR:NO_PROJECT|" .. Locale.Lookup(cityOwner:GetName()) .. " is not building a project. Queue a project first."')}
end
-- Execute the command
local cmdRow = GameInfo.UnitCommands["UNITCOMMAND_PROJECT_PRODUCTION"]
if not cmdRow then {_bail("ERR:CMD_NOT_FOUND|UNITCOMMAND_PROJECT_PRODUCTION not in game database")} end
local cmdHash = cmdRow.Hash
local can, failTable = UnitManager.CanStartCommand(unit, cmdHash, nil, true)
if not can then
    local reasons = {{}}
    if failTable then
        for _, v in pairs(failTable) do
            if type(v) == "table" then
                for _, s in pairs(v) do
                    if type(s) == "string" and s ~= "" then table.insert(reasons, s) end
                end
            end
        end
    end
    local reasonStr = #reasons > 0 and table.concat(reasons, "; ") or "unknown"
    {_bail_lua('"ERR:CANNOT_SACRIFICE|" .. reasonStr .. ". Builder at (" .. ux .. "," .. uy .. ") on " .. dName .. " with " .. charges .. " charges, city building " .. producing')}
end
-- Try with coordinate params first
local tParams = {{}}
tParams[UnitCommandTypes.PARAM_X] = ux
tParams[UnitCommandTypes.PARAM_Y] = uy
UnitManager.RequestCommand(unit, cmdHash, tParams)
-- Verify charges were consumed
local newCharges = unit:GetBuildCharges()
if newCharges == charges then
    -- Fallback: try with empty params
    UnitManager.RequestCommand(unit, cmdHash, {{}})
    newCharges = unit:GetBuildCharges()
end
if newCharges == charges then
    -- Second fallback: try RequestCommandImmediate
    pcall(function() UnitManager.RequestCommandImmediate(unit, cmdHash, tParams) end)
    newCharges = unit:GetBuildCharges()
end
if newCharges < charges then
    local consumed = charges - newCharges
    print("OK:SACRIFICED|" .. consumed .. " charges consumed for " .. producing .. " in " .. Locale.Lookup(cityOwner:GetName()) .. " at (" .. ux .. "," .. uy .. ") on " .. dName)
else
    print("WARN:SACRIFICE_UNCERTAIN|Command sent but charges unchanged (" .. charges .. "). Builder at (" .. ux .. "," .. uy .. ") on " .. dName .. ", city building " .. producing .. ". Ensure builder is on the exact district tile where the project's district is located.")
end
print("{SENTINEL}")
"""


def build_build_route(unit_id: int) -> str:
    """Build a route (road/railroad) on the Military Engineer's current tile.

    Uses UNITOPERATION_BUILD_ROUTE — after Steam Power tech this builds
    railroads (route type 4).  Does NOT consume charges.  Costs 1 Iron +
    1 Coal per railroad tile from the player's stockpile.
    """
    return f"""
{_lua_get_unit(unit_id)}
if unit:GetMovesRemaining() <= 0 then
    {_bail("ERR:NO_MOVES|Military Engineer has no moves remaining this turn")}
end
local x, y = unit:GetX(), unit:GetY()
local plot = Map.GetPlot(x, y)
if not plot or plot:GetOwner() ~= me then
    {_bail_lua('"ERR:NOT_YOUR_TERRITORY|Tile (" .. x .. "," .. y .. ") is not in your territory"')}
end
local opRow = GameInfo.UnitOperations["UNITOPERATION_BUILD_ROUTE"]
if not opRow then
    {_bail("ERR:OP_NOT_FOUND|UNITOPERATION_BUILD_ROUTE not in game database")}
end
local params = {{}}
params[UnitOperationTypes.PARAM_X] = x
params[UnitOperationTypes.PARAM_Y] = y
local canStart = UnitManager.CanStartOperation(unit, opRow.Hash, nil, params, true)
if not canStart then
    local rt = plot:GetRouteType()
    local reason = "unknown reason"
    if rt == 4 then reason = "tile already has a railroad"
    elseif plot:IsCity() then reason = "cannot build on city center"
    end
    {_bail_lua('"ERR:CANNOT_BUILD_ROUTE|" .. reason .. " at (" .. x .. "," .. y .. ")"')}
end
UnitManager.RequestOperation(unit, opRow.Hash, params)
-- Read back route type (may be stale same-frame, but try)
local newRoute = plot:GetRouteType()
local routeName = "ROUTE"
if newRoute == 4 then routeName = "RAILROAD"
elseif newRoute >= 0 then routeName = "ROAD"
end
print("OK:BUILT_" .. routeName .. "|" .. x .. "," .. y)
print("{SENTINEL}")
"""


def _parse_attack_target(token: str) -> AttackTarget:
    """Parse one target token from the units query into an AttackTarget.

    Formats (most-derived first):
      new:   ``eName@tx,ty~hp:EHP~dd:N~da:N~r:0~kind:K~captures:NAME~m:mod1,mod2``
             dd/da are the engine's predicted damage to the defender/attacker
             (from CombatManager.SimulateAttackInto), carried verbatim.
             kind: attack (default) | capture (unescorted civilian — move
             onto the tile; no damage fields) | theological (religious
             combat; dd/da are theological damage). captures names a
             civilian escorted by the defender (melee kill captures it).
      old:   ``eName@tx,ty(EHP hp)``
      legacy:``tx,ty``  (bare — tests only; no estimate)
    """
    # New format: carries the engine's damage estimates after '~'
    if "~" in token:
        head, *rest = token.split("~")
        # head = "eName@tx,ty"
        e_name, _, coord = head.partition("@")
        coords = coord.split(",")
        if len(coords) != 2:
            return AttackTarget(unit_type="", x=0, y=0)
        tx, ty = int(coords[0]), int(coords[1])
        fields: dict[str, str] = {}
        mods: list[str] = []
        for part in rest:
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            if k == "m":
                mods = [m for m in v.split(",") if m]
            else:
                fields[k] = v
        hp = int(fields.get("hp", "0") or "0")
        dmg_def = int(fields.get("dd", "0") or "0")
        dmg_att = int(fields.get("da", "0") or "0")
        is_ranged = fields.get("r", "0") == "1"
        return AttackTarget(
            unit_type=e_name,
            x=tx,
            y=ty,
            hp=hp,
            est_damage_to_defender=dmg_def,
            est_damage_to_attacker=dmg_att,
            is_ranged=is_ranged,
            is_kill=dmg_def >= hp if hp > 0 else False,
            modifiers=mods,
            kind=fields.get("kind", "attack") or "attack",
            captures=fields.get("captures", "") or "",
        )
    # Old format: eName@tx,ty(EHP hp)
    if "@" in token:
        e_name, _, coord = token.partition("@")
        # coord like "23,11(53hp)"
        coord = coord.replace("(hp)", "")
        hp = 0
        if "(" in coord:
            coord, _, hp_part = coord.partition("(")
            digits = "".join(c for c in hp_part if c.isdigit())
            if digits:
                hp = int(digits)
        coords = coord.split(",")
        if len(coords) != 2:
            return AttackTarget(unit_type=e_name, x=0, y=0)
        return AttackTarget(unit_type=e_name, x=int(coords[0]), y=int(coords[1]), hp=hp)
    # Legacy bare: tx,ty
    coords = token.split(",")
    if len(coords) == 2:
        try:
            return AttackTarget(unit_type="", x=int(coords[0]), y=int(coords[1]))
        except ValueError:
            pass
    return AttackTarget(unit_type=token, x=0, y=0)


def parse_units_response(lines: list[str]) -> list[UnitInfo]:
    units = []
    # Pass 1: collect FORMATION| lines first (they appear after unit lines in output)
    formations: dict[int, tuple[int, str]] = {}
    for line in lines:
        if line.startswith("FORMATION|"):
            parts = line.split("|")
            if len(parts) >= 4:
                src_id = int(parts[1])
                tgt_id = int(parts[2])
                tgt_type = parts[3]
                formations[src_id] = (tgt_id, tgt_type)
    # Pass 2: parse unit lines with formation lookup
    # Line format: id|name|type|x,y|moves/max|hp/max|cs|rs|charges|targets|promo|canUp|upName|upCost|imps|religion
    for line in lines:
        if line.startswith("FORMATION|"):
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        x_str, y_str = parts[3].split(",")
        moves_cur, moves_max = parts[4].split("/")
        hp_cur, hp_max = parts[5].split("/")
        cs = int(parts[6]) if len(parts) > 6 else 0
        rs = int(parts[7]) if len(parts) > 7 else 0
        charges = int(parts[8]) if len(parts) > 8 else 0
        targets_raw = parts[9] if len(parts) > 9 else ""
        targets = (
            [_parse_attack_target(t) for t in targets_raw.split(";") if t]
            if targets_raw
            else []
        )
        promos_raw = parts[10] if len(parts) > 10 else ""
        available_promotions: list[PromotionOption] = []
        if promos_raw and promos_raw != "0":
            for pt in promos_raw.split(";"):
                if not pt:
                    continue
                f = pt.split("~")
                if len(f) >= 3:
                    available_promotions.append(
                        PromotionOption(
                            promotion_type=f[0],
                            name=f[1],
                            description=f[2],
                        )
                    )
        can_upgrade = parts[11] == "1" if len(parts) > 11 else False
        upgrade_target = parts[12] if len(parts) > 12 else ""
        upgrade_cost = int(parts[13]) if len(parts) > 13 and parts[13].isdigit() else 0
        valid_imps_raw = parts[14] if len(parts) > 14 else ""
        valid_imps = (
            [v for v in valid_imps_raw.split(";") if v] if valid_imps_raw else []
        )
        religion = parts[15] if len(parts) > 15 else ""
        fm = formations.get(int(parts[0]))
        units.append(
            UnitInfo(
                unit_id=int(parts[0]),
                name=parts[1],
                unit_type=parts[2],
                x=int(x_str),
                y=int(y_str),
                moves_remaining=float(moves_cur),
                max_moves=float(moves_max),
                health=int(hp_cur),
                max_health=int(hp_max),
                combat_strength=cs,
                ranged_strength=rs,
                build_charges=charges,
                targets=targets,
                available_promotions=available_promotions,
                can_upgrade=can_upgrade,
                upgrade_target=upgrade_target,
                upgrade_cost=upgrade_cost,
                valid_improvements=valid_imps,
                religion=religion,
                formation_linked_to=fm[0] if fm else None,
                formation_linked_type=fm[1] if fm else "",
            )
        )
    return units


def parse_threat_scan_response(lines: list[str]) -> list[ThreatInfo]:
    threats: list[ThreatInfo] = []
    for line in lines:
        if not line.startswith("THREAT|"):
            continue
        parts = line.split("|")
        # Format: THREAT|owner_id|owner_name|unit_type|x,y|hp/max|CS:n|RS:n|dist:n|cs:0/1|uid:N
        if len(parts) >= 9:
            x_str, y_str = parts[4].split(",")
            hp_str, max_str = parts[5].split("/")
            cs = int(parts[6].replace("CS:", "")) if parts[6].startswith("CS:") else 0
            rs = int(parts[7].replace("RS:", "")) if parts[7].startswith("RS:") else 0
            dist = (
                int(parts[8].replace("dist:", ""))
                if parts[8].startswith("dist:")
                else 0
            )
            uid = 0
            if len(parts) > 10 and parts[10].startswith("uid:"):
                uid = int(parts[10][4:])
            threats.append(
                ThreatInfo(
                    unit_type=parts[3],
                    x=int(x_str),
                    y=int(y_str),
                    hp=int(hp_str),
                    max_hp=int(max_str),
                    combat_strength=cs,
                    ranged_strength=rs,
                    distance=dist,
                    owner_id=int(parts[1]),
                    owner_name=parts[2],
                    is_city_state=len(parts) > 9
                    and parts[9].startswith("cs:")
                    and parts[9][3:] == "1",
                    unit_id=uid,
                )
            )
        elif len(parts) >= 7:
            # Legacy format fallback: THREAT|unit_type|x,y|hp/max|CS:n|RS:n|dist:n
            x_str, y_str = parts[2].split(",")
            hp_str, max_str = parts[3].split("/")
            cs = int(parts[4].replace("CS:", "")) if parts[4].startswith("CS:") else 0
            rs = int(parts[5].replace("RS:", "")) if parts[5].startswith("RS:") else 0
            dist = (
                int(parts[6].replace("dist:", ""))
                if parts[6].startswith("dist:")
                else 0
            )
            threats.append(
                ThreatInfo(
                    unit_type=parts[1],
                    x=int(x_str),
                    y=int(y_str),
                    hp=int(hp_str),
                    max_hp=int(max_str),
                    combat_strength=cs,
                    ranged_strength=rs,
                    distance=dist,
                )
            )
    return threats


def build_fog_neighbor_query(positions: list[tuple[int, int]]) -> str:
    """GameCore: for each position, report which adjacent tiles are in fog."""
    checks = "\n".join(f"check({x},{y})" for x, y in positions)
    return f"""
local me = Game.GetLocalPlayer()
local pVis = PlayersVisibility[me]
local dirNames = {{"NE","E","SE","SW","W","NW"}}
function check(cx, cy)
    local plot = Map.GetPlot(cx, cy)
    if not plot then return end
    local fog = {{}}
    for i = 0, 5 do
        local adj = Map.GetAdjacentPlot(cx, cy, i)
        if adj and not pVis:IsVisible(adj:GetX(), adj:GetY()) then
            table.insert(fog, dirNames[i+1])
        end
    end
    if #fog > 0 then
        print("FOG|" .. cx .. "," .. cy .. "|" .. table.concat(fog, ","))
    end
end
{checks}
print("{SENTINEL}")
"""


def parse_fog_neighbor_response(
    lines: list[str],
) -> dict[tuple[int, int], list[str]]:
    """Parse FOG|x,y|dir1,dir2,... lines into {(x,y): [directions]}."""
    result: dict[tuple[int, int], list[str]] = {}
    for line in lines:
        if not line.startswith("FOG|"):
            continue
        parts = line.split("|")
        x_str, y_str = parts[1].split(",")
        result[(int(x_str), int(y_str))] = parts[2].split(",")
    return result


def diff_threats(
    before: list[ThreatInfo], after: list[ThreatInfo]
) -> tuple[list[ThreatInfo], list[ThreatInfo], list[ThreatInfo]]:
    """Compare threat snapshots: (disappeared, new, moved).

    Match by (owner_id, unit_id) — the engine's ComponentID pair — when the
    id is available, otherwise by (owner_id, unit_type, x, y). Unit ids are
    only unique per player, so keying by id alone would collide across owners.
    """
    after_by_uid: dict[tuple[int, int], ThreatInfo] = {}
    after_by_key: dict[tuple, ThreatInfo] = {}
    after_matched: set[int] = set()

    for i, t in enumerate(after):
        if t.unit_id:
            after_by_uid[(t.owner_id, t.unit_id)] = t
        after_by_key[(t.owner_id, t.unit_type, t.x, t.y)] = t

    disappeared: list[ThreatInfo] = []
    moved: list[ThreatInfo] = []

    for bt in before:
        at = None
        if bt.unit_id and (bt.owner_id, bt.unit_id) in after_by_uid:
            at = after_by_uid[(bt.owner_id, bt.unit_id)]
        elif (bt.owner_id, bt.unit_type, bt.x, bt.y) in after_by_key:
            at = after_by_key[(bt.owner_id, bt.unit_type, bt.x, bt.y)]

        if at is None:
            disappeared.append(bt)
        else:
            idx = after.index(at)
            after_matched.add(idx)
            if at.x != bt.x or at.y != bt.y:
                moved.append(at)

    new_threats = [t for i, t in enumerate(after) if i not in after_matched]
    return disappeared, new_threats, moved


def build_pathing_estimate_query(unit_id: int, target_x: int, target_y: int) -> str:
    """InGame context: suggested path to destination."""
    return (load_lua_template("pathing_estimate.lua")
    .replace("__MCP_UNIT_ID_TAG__", str(unit_id))
    .replace("__MCP_TARGET_X_TAG__", str(target_x))
    .replace("__MCP_TARGET_Y_TAG__", str(target_y))
    )

# ── Post-move visibility ────────────────────────────────────────────────


def build_post_move_visibility_query(now_x: int, now_y: int, radius: int = 4) -> str:
    """GameCore: scan tiles around a position and return revealed tile data.

    Used after a unit move to compute newly-revealed tiles via Python-side diff.
    Radius 4 covers all standard sight ranges (2 for most units, 3 for scouts).
    Output: ``TILE|x,y|terrain|feature|resource:class|hills|camp|units|city``
    """
    return f"""
local cx, cy, r = {now_x}, {now_y}, {radius}
local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local pTech = Players[me]:GetTechs()
{_LUA_RES_VISIBLE}
for dy = -r, r do
    for dx = -r, r do
        local x, y = cx + dx, cy + dy
        local plot = Map.GetPlot(x, y)
        if plot and vis:IsRevealed(plot:GetX(), plot:GetY()) then
            local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
            local feature = "none"
            local fi = plot:GetFeatureType()
            if fi >= 0 then feature = GameInfo.Features[fi].FeatureType end
            local resource = "none"
            local ri = plot:GetResourceType()
            if ri >= 0 then
                local re = GameInfo.Resources[ri]
                if resVisible(re) then
                    resource = re.ResourceType .. ":" .. (re.ResourceClassType or "")
                end
            end
            local hills = plot:IsHills() and "1" or "0"
            local camp = "0"
            local ii = plot:GetImprovementType()
            if ii >= 0 then
                local iInfo = GameInfo.Improvements[ii]
                if iInfo and iInfo.ImprovementType == "IMPROVEMENT_BARBARIAN_CAMP" then
                    camp = "1"
                end
            end
            local units = "none"
            if vis:IsVisible(plot:GetX(), plot:GetY()) then
                local uParts = {{}}
                for pid = 0, 63 do
                    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
                        for _, u in Players[pid]:GetUnits():Members() do
                            if u:GetX() == x and u:GetY() == y then
                                local entry = GameInfo.Units[u:GetType()]
                                local nm = entry and entry.UnitType or "UNKNOWN"
                                local ownerLabel = "Barbarian"
                                if pid ~= 63 then
                                    local cfg = PlayerConfigurations[pid]
                                    if cfg then ownerLabel = Locale.Lookup(cfg:GetCivilizationShortDescription()) end
                                end
                                table.insert(uParts, ownerLabel .. " " .. nm:gsub("UNIT_", ""))
                            end
                        end
                    end
                end
                if #uParts > 0 then units = table.concat(uParts, ";") end
            end
            local cityName = "none"
            if plot:IsCity() then
                local cOwner = plot:GetOwner()
                if cOwner >= 0 and cOwner ~= me then
                    pcall(function()
                        for _, c in Players[cOwner]:GetCities():Members() do
                            if c:GetX() == x and c:GetY() == y then
                                cityName = Locale.Lookup(c:GetName())
                                break
                            end
                        end
                    end)
                end
            end
            print("TILE|" .. x .. "," .. y .. "|" .. terrain .. "|" .. feature .. "|" .. resource .. "|" .. hills .. "|" .. camp .. "|" .. units .. "|" .. cityName)
        end
    end
end
print("{SENTINEL}")
"""


def parse_post_move_visibility(
    lines: list[str],
) -> list[tuple[int, int, dict]]:
    """Parse TILE| lines from post-move visibility query.

    Returns (x, y, metadata) tuples where metadata contains terrain, feature,
    resource, resource_class, hills, camp, units, and city fields.
    """
    results: list[tuple[int, int, dict]] = []
    for line in lines:
        if not line.startswith("TILE|"):
            continue
        parts = line.split("|")
        if len(parts) < 9:
            continue
        xy = parts[1].split(",")
        x, y = int(xy[0]), int(xy[1])
        # Parse resource into name + class
        resource = None
        resource_class = None
        if parts[4] != "none":
            rp = parts[4].split(":", 1)
            resource = rp[0]
            if len(rp) > 1 and rp[1]:
                resource_class = rp[1].replace("RESOURCECLASS_", "").lower()
        meta = {
            "terrain": parts[2],
            "feature": None if parts[3] == "none" else parts[3],
            "resource": resource,
            "resource_class": resource_class,
            "hills": parts[5] == "1",
            "camp": parts[6] == "1",
            "units": None if parts[7] == "none" else parts[7].split(";"),
            "city": None if parts[8] == "none" else parts[8],
        }
        results.append((x, y, meta))
    return results


def build_builder_tasks_query() -> str:
    """InGame context: scans all owned tiles for improvement tasks and all idle builders.

    Outputs TASK| lines for tiles needing work and BUILDER| lines for builder units.
    Uses hardcoded resource mapping and terrain heuristics for improvement recommendations.
    Does NOT use CanStartOperation with remote tiles (corrupts engine state → crash).
    """
    return """
local me = Game.GetLocalPlayer()
local pCities = Players[me]:GetCities()

-- Gather all builders with charges
local builders = {}
for _, u in Players[me]:GetUnits():Members() do
    local entry = GameInfo.Units[u:GetType()]
    if entry and entry.UnitType == "UNIT_BUILDER" and u:GetBuildCharges() > 0 then
        local bx, by = u:GetX(), u:GetY()
        if bx ~= -9999 then
            table.insert(builders, {id=u:GetID(), x=bx, y=by, charges=u:GetBuildCharges(), moves=u:GetMovesRemaining()})
        end
    end
end

-- Hardcoded resource -> improvement mapping (avoids GameInfo.Improvement_ValidResources()
-- iterator which can crash the game engine with EXCEPTION_ACCESS_VIOLATION)
local resImpMap = {
    -- Strategic
    RESOURCE_HORSES="IMPROVEMENT_PASTURE", RESOURCE_IRON="IMPROVEMENT_MINE",
    RESOURCE_NITER="IMPROVEMENT_MINE", RESOURCE_COAL="IMPROVEMENT_MINE",
    RESOURCE_ALUMINUM="IMPROVEMENT_MINE", RESOURCE_URANIUM="IMPROVEMENT_MINE",
    RESOURCE_OIL="IMPROVEMENT_OIL_WELL",
    -- Luxury (mined/quarried)
    RESOURCE_DIAMONDS="IMPROVEMENT_MINE", RESOURCE_JADE="IMPROVEMENT_MINE",
    RESOURCE_MERCURY="IMPROVEMENT_MINE", RESOURCE_SALT="IMPROVEMENT_MINE",
    RESOURCE_SILVER="IMPROVEMENT_MINE", RESOURCE_AMBER="IMPROVEMENT_MINE",
    RESOURCE_GYPSUM="IMPROVEMENT_QUARRY", RESOURCE_MARBLE="IMPROVEMENT_QUARRY",
    -- Luxury (plantation)
    RESOURCE_CITRUS="IMPROVEMENT_PLANTATION", RESOURCE_COCOA="IMPROVEMENT_PLANTATION",
    RESOURCE_COFFEE="IMPROVEMENT_PLANTATION", RESOURCE_COTTON="IMPROVEMENT_PLANTATION",
    RESOURCE_DYES="IMPROVEMENT_PLANTATION", RESOURCE_INCENSE="IMPROVEMENT_PLANTATION",
    RESOURCE_OLIVES="IMPROVEMENT_PLANTATION", RESOURCE_SILK="IMPROVEMENT_PLANTATION",
    RESOURCE_SPICES="IMPROVEMENT_PLANTATION", RESOURCE_SUGAR="IMPROVEMENT_PLANTATION",
    RESOURCE_TEA="IMPROVEMENT_PLANTATION", RESOURCE_TOBACCO="IMPROVEMENT_PLANTATION",
    RESOURCE_WINE="IMPROVEMENT_PLANTATION",
    -- Luxury (camp)
    RESOURCE_FURS="IMPROVEMENT_CAMP", RESOURCE_IVORY="IMPROVEMENT_CAMP",
    RESOURCE_TRUFFLES="IMPROVEMENT_CAMP", RESOURCE_HONEY="IMPROVEMENT_CAMP",
    -- Bonus
    RESOURCE_BANANAS="IMPROVEMENT_PLANTATION", RESOURCE_CATTLE="IMPROVEMENT_PASTURE",
    RESOURCE_SHEEP="IMPROVEMENT_PASTURE", RESOURCE_DEER="IMPROVEMENT_CAMP",
    RESOURCE_COPPER="IMPROVEMENT_MINE", RESOURCE_STONE="IMPROVEMENT_QUARRY",
    RESOURCE_MAIZE="IMPROVEMENT_FARM", RESOURCE_RICE="IMPROVEMENT_FARM",
    RESOURCE_WHEAT="IMPROVEMENT_FARM",
    -- Water (builders can't reach, but listed for completeness)
    RESOURCE_FISH="IMPROVEMENT_FISHING_BOATS", RESOURCE_CRABS="IMPROVEMENT_FISHING_BOATS",
    RESOURCE_PEARLS="IMPROVEMENT_FISHING_BOATS", RESOURCE_TURTLES="IMPROVEMENT_FISHING_BOATS",
    RESOURCE_WHALES="IMPROVEMENT_FISHING_BOATS",
}

-- Scan city territory for tasks
local seen = {}
local normalCount = 0
local maxNormal = 20
for _, city in pCities:Members() do
    local cx, cy = city:GetX(), city:GetY()
    local cityName = Locale.Lookup(city:GetName())
    for dy = -3, 3 do for dx = -3, 3 do
        local px, py = cx + dx, cy + dy
        local key = px .. "," .. py
        if not seen[key] then
            seen[key] = true
            local plot = Map.GetPlot(px, py)
            if plot and plot:GetOwner() == me and not plot:IsWater() and not plot:IsMountain() then
                local distIdx = plot:GetDistrictType()
                local impIdx = plot:GetImprovementType()
                local resIdx = plot:GetResourceType()

                -- Skip tiles with districts
                if distIdx < 0 then
                    -- Check for pillaged improvements
                    if impIdx >= 0 then
                        local okP, pil = pcall(function() return plot:IsImprovementPillaged() end)
                        if okP and pil then
                            local impInfo = GameInfo.Improvements[impIdx]
                            local impName = impInfo and impInfo.ImprovementType or "UNKNOWN"
                            -- Find nearest builder
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            print("TASK|urgent|" .. px .. "," .. py .. "|REPAIR|" .. impName:gsub("IMPROVEMENT_", "") .. "|pillaged|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                        end
                    -- Check for unimproved resource tiles
                    elseif resIdx >= 0 and impIdx < 0 then
                        local resInfo = GameInfo.Resources[resIdx]
                        if resInfo then
                            local resClass = resInfo.ResourceClassType or ""
                            local resName = resInfo.ResourceType:gsub("RESOURCE_", "")
                            local priority = "normal"
                            if resClass == "RESOURCECLASS_STRATEGIC" then priority = "urgent"
                            elseif resClass == "RESOURCECLASS_LUXURY" then priority = "high"
                            elseif resClass == "RESOURCECLASS_BONUS" then priority = "high"
                            end
                            -- Find valid improvement via resource lookup table
                            local validImp = resImpMap[resInfo.ResourceType] or "UNKNOWN"
                            -- Check tech prerequisite
                            if validImp ~= "UNKNOWN" then
                                local impInfo = GameInfo.Improvements[validImp]
                                if impInfo and impInfo.PrereqTech then
                                    local techInfo = GameInfo.Technologies[impInfo.PrereqTech]
                                    if techInfo and not Players[me]:GetTechs():HasTech(techInfo.Index) then
                                        validImp = validImp .. "_LOCKED"
                                    end
                                end
                            end
                            -- Find nearest builder
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            local classShort = "bonus"
                            if resClass == "RESOURCECLASS_STRATEGIC" then classShort = "strategic"
                            elseif resClass == "RESOURCECLASS_LUXURY" then classShort = "luxury"
                            end
                            print("TASK|" .. priority .. "|" .. px .. "," .. py .. "|" .. validImp .. "|" .. resName .. "|" .. classShort .. "|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                        end
                    -- Check for empty tiles that could use standard improvements (capped)
                    -- Uses terrain heuristics instead of CanStartOperation (which corrupts
                    -- engine state when called with remote tile coordinates, causing
                    -- EXCEPTION_ACCESS_VIOLATION during end_turn)
                    elseif impIdx < 0 and resIdx < 0 and normalCount < maxNormal then
                        local featureIdx = plot:GetFeatureType()
                        local terrIdx = plot:GetTerrainType()
                        local terrInfo = terrIdx >= 0 and GameInfo.Terrains[terrIdx] or nil
                        local terrName = terrInfo and terrInfo.TerrainType or ""
                        local bestImp = nil
                        if plot:IsHills() then
                            bestImp = "IMPROVEMENT_MINE"
                        elseif featureIdx >= 0 then
                            local fInfo = GameInfo.Features[featureIdx]
                            local fName = fInfo and fInfo.FeatureType or ""
                            if fName == "FEATURE_FOREST" then
                                bestImp = "IMPROVEMENT_LUMBER_MILL"
                            end
                            -- Jungle/marsh need removal first, skip
                        elseif terrName == "TERRAIN_DESERT" or terrName == "TERRAIN_SNOW" or terrName == "TERRAIN_TUNDRA" then
                            -- Low-yield terrain, skip
                        else
                            bestImp = "IMPROVEMENT_FARM"
                        end
                        if bestImp then
                            local nearId, nearDist = -1, 999
                            for _, b in ipairs(builders) do
                                local d = Map.GetPlotDistance(b.x, b.y, px, py)
                                if d < nearDist then nearDist = d; nearId = b.id end
                            end
                            print("TASK|normal|" .. px .. "," .. py .. "|" .. bestImp .. "||none|" .. cityName .. "|" .. nearId .. "|" .. nearDist)
                            normalCount = normalCount + 1
                        end
                    end
                end
            end
        end
    end end
end

-- Print builder info
for _, b in ipairs(builders) do
    print("BUILDER|" .. b.id .. "|" .. b.x .. "," .. b.y .. "|" .. b.charges .. "|" .. string.format("%.1f", b.moves))
end
print("{SENTINEL}")
""".replace("{SENTINEL}", SENTINEL)


def parse_builder_tasks(
    lines: list[str],
) -> tuple[list[BuilderTask], list[BuilderInfo]]:
    """Parse TASK| and BUILDER| lines from build_builder_tasks_query."""
    tasks: list[BuilderTask] = []
    builders: list[BuilderInfo] = []

    for line in lines:
        try:
            if line.startswith("TASK|"):
                parts = line.split("|")
                if len(parts) < 9:
                    continue
                xy = parts[2].split(",")
                if len(xy) != 2:
                    continue
                imp = parts[3]
                # Skip tasks where tech prerequisite isn't met
                if imp.endswith("_LOCKED"):
                    continue
                tasks.append(
                    BuilderTask(
                        priority=parts[1],
                        x=int(xy[0]),
                        y=int(xy[1]),
                        improvement=imp,
                        resource=parts[4],
                        resource_class=parts[5],
                        city_name=parts[6],
                        nearest_builder_id=int(parts[7]),
                        distance=int(parts[8]),
                    )
                )
            elif line.startswith("BUILDER|"):
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                xy = parts[2].split(",")
                if len(xy) != 2:
                    continue
                builders.append(
                    BuilderInfo(
                        unit_id=int(parts[1]),
                        x=int(xy[0]),
                        y=int(xy[1]),
                        charges=int(parts[3]),
                        moves=float(parts[4]),
                    )
                )
        except (ValueError, IndexError):
            continue

    return tasks, builders
