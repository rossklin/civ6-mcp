-- Move a unit to a tile (InGame context).
--
-- This file is a TEMPLATE loaded by build_move_unit() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--   __UNIT_ID__           -> engine unit id (UnitManager.GetUnit key)
--   __TARGET_X__          -> destination tile X
--   __TARGET_Y__          -> destination tile Y
--
-- Ports the game UI's own move pipeline (Civ6Common.lua MoveUnitToPlot ->
-- RequestMoveOperation - the code behind a human click/drag move):
--
--   * The move family ALWAYS carries PARAM_MODIFIERS = ATTACK +
--     MOVE_IGNORE_UNEXPLORED_DESTINATION. ATTACK makes an enemy-occupied
--     destination an attack-move (civilian capture); IGNORE_UNEXPLORED lets
--     the order target fogged tiles. The UI never issues MOVE_TO with a bare
--     {X, Y} table, and a bare-params CanStartOperation(MOVE_TO) immediately
--     before an equally bare RequestOperation left the engine rejecting the
--     op while still charging movement (the silent-fail signature from
--     devlog game_004; bare-params checks with remote tiles also corrupt
--     engine state - see build_builder_tasks_query).
--   * Trading places with another owned unit on the destination tile is the
--     dedicated SWAP_UNITS operation, attempted exactly like the UI when a
--     same-class unit occupies the destination. CanStartOperation(SWAP_UNITS)
--     is the engine's authority on the other unit's remaining movement.
--   * Stacking conflicts: the engine exposes no "can these units share a
--     tile" query (the UI never checks - it renders flags and lets the
--     operation system decide). Rule used here: effective occupancy class
--     is "RELIGIOUS" for religious units, otherwise the unit's
--     FormationClass; an occupant blocks the mover iff effective classes
--     match (one unit per occupancy class per tile). Religious units
--     (detected via the GameInfo ReligiousStrength column - the same test
--     the game's UI uses in UnitFlagManager.lua) are
--     FORMATION_CLASS_CIVILIAN in the data but treated as their own class:
--     they co-locate with civilians and military but conflict with each
--     other. Not yet verified live (no religious units in the test game) -
--     verify with the first missionary/apostle and record the outcome in
--     the diary notes.
--   * No MOVE_TO pre-check on the common path: the UI fire-and-forgets the
--     final MOVE_TO too; move_unit's GameCore position readback is the
--     verification. A rejected MOVE_TO was verified live to charge NO
--     movement - the failure surfaces as BLOCKED in the readback rather
--     than a clean error, which is why the stacking guard above exists.
--
-- Air units: the UI routes them through AIR_ATTACK/DEPLOY (Civ6Common) -
-- not handled here, same as before this template existed.
--
-- Emits OK:SWAPPING|<x>,<y>|from:<fx>,<fy>, OK:CAPTURE_MOVE|... or
-- OK:MOVING_TO|... on success; ERR:* lines otherwise.

local me = Game.GetLocalPlayer()
local unit = UnitManager.GetUnit(me, __UNIT_ID__)
if unit == nil then
    print("ERR:UNIT_NOT_FOUND")
    print("__MCP_SENTINEL_TAG__")
    return
end
if unit:GetMovesRemaining() <= 0 then
    print("ERR:NO_MOVES|Unit has no movement points remaining this turn. Use skip or wait until next turn.")
    print("__MCP_SENTINEL_TAG__")
    return
end

local targetX, targetY = __TARGET_X__, __TARGET_Y__
local fromX, fromY = unit:GetX(), unit:GetY()
if fromX == targetX and fromY == targetY then
    print("ERR:ALREADY_THERE|Unit is already at (" .. targetX .. "," .. targetY .. ").")
    print("__MCP_SENTINEL_TAG__")
    return
end

local params = {}
params[UnitOperationTypes.PARAM_X] = targetX
params[UnitOperationTypes.PARAM_Y] = targetY
params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
    + UnitOperationMoveModifiers.MOVE_IGNORE_UNEXPLORED_DESTINATION

-- Occupancy scan. An occupant blocks the mover iff effective occupancy
-- classes match (see header note). The class test lives in the shared
-- occupancyClass helper (injected from _helpers.py's _LUA_OCCUPANCY_CLASS
-- snippet, also used by units.lua). Any non-local unit at the
-- destination makes the order an attack-move (tag only - the ATTACK
-- modifier above is what actually arms it).
__LUA_OCCUPANCY_CLASS__
local moverClass = occupancyClass(GameInfo.Units[unit:GetType()])
local blockedBy = nil
local hasHostile = false
local tgtUnits = Map.GetUnitsAt(targetX, targetY)
if tgtUnits then
    for other in tgtUnits:Units() do
        if other:GetOwner() == me then
            local info = GameInfo.Units[other:GetType()]
            if moverClass ~= nil and occupancyClass(info) == moverClass then
                blockedBy = info.UnitType
            end
        else
            hasHostile = true
        end
    end
end

if blockedBy ~= nil then
    -- Same-class occupant holds the tile: the only way in is the engine's
    -- swap operation (which also requires the occupant to have movement).
    if UnitManager.CanStartOperation(unit, UnitOperationTypes.SWAP_UNITS, nil, params) then
        UnitManager.RequestOperation(unit, UnitOperationTypes.SWAP_UNITS, params)
        print("OK:SWAPPING|" .. targetX .. "," .. targetY .. "|from:" .. fromX .. "," .. fromY)
        print("__MCP_SENTINEL_TAG__")
        return
    end
    print("ERR:STACKING_CONFLICT|Cannot join or swap with friendly " .. blockedBy
        .. " at (" .. targetX .. "," .. targetY
        .. "). Co-location is not allowed and swapping is not possible (the other unit may be out of movement).")
    print("__MCP_SENTINEL_TAG__")
    return
end

if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO, nil, params) then
    print("ERR:CANNOT_MOVE|Unit cannot move (invalid state or unreachable destination)")
    print("__MCP_SENTINEL_TAG__")
    return
end
UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
local tag = hasHostile and "OK:CAPTURE_MOVE|" or "OK:MOVING_TO|"
print(tag .. targetX .. "," .. targetY .. "|from:" .. fromX .. "," .. fromY)
print("__MCP_SENTINEL_TAG__")
