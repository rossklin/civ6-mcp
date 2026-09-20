-- Attack the hostile unit on a tile (InGame context).
--
-- This file is a TEMPLATE loaded by build_attack_unit() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__    -> the response sentinel (see _helpers.SENTINEL)
--   __LUA_OCCUPANCY_CLASS__ -> shared occupancyClass helper (_helpers.py)
--   __UNIT_ID__             -> engine unit id (UnitManager.GetUnit key)
--   __TARGET_X__            -> target tile X
--   __TARGET_Y__            -> target tile Y
--
-- Attack validity follows the rules units.lua (the game-state attack
-- listing, checked against the game's own UI source) presents targets by.
-- The previous inline builder got several of these wrong - it required war
-- for EVERY attack (wrongly blocking theological combat, which needs none)
-- and picked "the enemy" by Combat>0 instead of by occupancy class:
--
--   * Hostility: a foreign unit is hostile iff its owner is a barbarian or
--     free city (owner >= 62), OR the local player is at war with its
--     owner, OR this is a religious attacker and the target is a religious
--     unit. THEOLOGICAL COMBAT NEEDS NO WAR.
--   * Tile classification by effective occupancy class (shared
--     occupancyClass helper): a tile can hold one military defender plus
--     one civilian (and religious units); combat always targets the
--     military defender. A non-religious attacker engages the defender, or
--     captures an unescorted civilian adjacent to it (a move onto the
--     tile - no damage, requires Combat > 0). A religious attacker engages
--     ONLY the religious unit: military units cannot attack religious
--     units (that interaction is UNITCOMMAND_CONDEMN_HERETIC or
--     theological combat), and religious units cannot attack
--     military/civilian units. A hostile DEFENSIBLE DISTRICT (city center,
--     encampment, ... - GameInfo HitPoints > 0) outranks the units on its
--     tile: combat targets the district, its garrisoned units are not
--     separately targetable and take no damage while its defenses stand.
--   * Operation routing mirrors the game UI (Civ6Common.RequestMoveOperation
--     and units.lua's listing gate): DOMAIN_AIR -> AIR_ATTACK; a ranged
--     unit (RangedCombat > 0) -> RANGE_ATTACK at ANY distance in range -
--     adjacent included (the UI's move path tries RANGE_ATTACK first with
--     no distance branch); everything else (melee, civilian capture, and
--     theological combat - there is no dedicated theological operation, the
--     engine resolves a MOVE_TO attack-move between religious units as
--     theological combat, exactly like melee: the winner advances onto the
--     tile) -> MOVE_TO with ATTACK + MOVE_IGNORE_UNEXPLORED_DESTINATION.
--     CanStartOperation with the target params is the engine's authority
--     in every branch (ranged LOS included; GetOperationTargets returns
--     empty for some valid targets, e.g. naval, so it is not used).
--
-- A melee attack-move toward a DISTANT target ("charging") is a legal
-- engine order (the unit closes and strikes when adjacent); it reports
-- ERR:STOPPED_SHORT when it cannot reach adjacency this turn. Whether a
-- strike actually landed is not provable in Lua state, so the caller's
-- GameCore poll gives the concrete outcome: damage on either combatant,
-- district defense HP, or the ownership change of a capture.
--
-- Emits OK:RANGE_ATTACK / OK:AIR_ATTACK / OK:MELEE_ATTACK /
-- OK:THEOLOGICAL_ATTACK / OK:CAPTURE (the pre_hp:/your HP: fields feed
-- game_state.attack_unit's post-combat GameCore poll; for a district
-- target pre_hp is the defense layer that takes damage - walls first,
-- else the district HP) or ERR:* with a specific reason.

local me = Game.GetLocalPlayer()
local unit = UnitManager.GetUnit(me, __UNIT_ID__)
if unit == nil then
    print("ERR:UNIT_NOT_FOUND")
    print("__MCP_SENTINEL_TAG__")
    return
end

local tx, ty = __TARGET_X__, __TARGET_Y__
local ux, uy = unit:GetX(), unit:GetY()
local dist = Map.GetPlotDistance(ux, uy, tx, ty)
if dist < 1 then
    print("ERR:SELF_TARGET|Target tile is the attacking unit's own tile.")
    print("__MCP_SENTINEL_TAG__")
    return
end

local entry = GameInfo.Units[unit:GetType()]
local cs = entry and entry.Combat or 0
local rs = entry and entry.RangedCombat or 0
local isAir = entry ~= nil and entry.Domain == "DOMAIN_AIR"

-- Attacker gates, same as the units.lua listing: military (Combat or
-- RangedCombat > 0) or religious, and operations only while movement
-- remains (the UI's UnitPanel gates operations the same way).
__LUA_OCCUPANCY_CLASS__
local aIsReligious = occupancyClass(entry) == "RELIGIOUS"
if cs <= 0 and rs <= 0 and not aIsReligious then
    print("ERR:NOT_COMBAT_UNIT|Unit cannot attack: no Combat/RangedCombat strength and not a religious unit.")
    print("__MCP_SENTINEL_TAG__")
    return
end
if unit:GetMovesRemaining() <= 0 then
    print("ERR:NO_MOVES|Unit has no movement points remaining. Attacks require movement (the game UI lists attack operations only while moves remain). Wait until next turn.")
    print("__MCP_SENTINEL_TAG__")
    return
end

-- Classify the tile's foreign units (units.lua's rules): one military
-- defender + one civilian + one religious unit may share a tile.
-- Theological combat needs no war (and no barbarian); military
-- interactions keep the war/barbarian/free-city gate.
local defName, defHP, defMax = nil, 0, 0
local civName, civHP, civMax = nil, 0, 0
local relName, relHP, relMax = nil, 0, 0
local relUnit = nil
local sawForeignName, sawForeignOwner = nil, -1
local sawRelName = nil
local tgtUnits = Map.GetUnitsAt(tx, ty)
if tgtUnits then
    for other in tgtUnits:Units() do
        local otherOwner = other:GetOwner()
        if otherOwner ~= me then
            local oInfo = GameInfo.Units[other:GetType()]
            local oName = oInfo and oInfo.UnitType or "UNKNOWN"
            if sawForeignName == nil then
                sawForeignName = oName
                sawForeignOwner = otherOwner
            end
            local oClass = occupancyClass(oInfo)
            if oClass == "RELIGIOUS" and sawRelName == nil then
                sawRelName = oName
            end
            local isHostile = otherOwner >= 62
                or Players[me]:GetDiplomacy():IsAtWarWith(otherOwner)
                or (aIsReligious and oClass == "RELIGIOUS")
            if isHostile then
                local oMax = other:GetMaxDamage()
                local oHP = oMax - other:GetDamage()
                if oClass == "RELIGIOUS" then
                    if relName == nil then
                        relName, relHP, relMax, relUnit = oName, oHP, oMax, other
                    end
                elseif oClass == "FORMATION_CLASS_CIVILIAN" then
                    if civName == nil then
                        civName, civHP, civMax = oName, oHP, oMax
                    end
                elseif defName == nil then
                    defName, defHP, defMax = oName, oHP, oMax
                end
            end
        end
    end
end

-- Defensible district on the tile (city center, encampment, ... - the
-- GameInfo HitPoints column is > 0 exactly for those)? Combat targets the
-- DISTRICT: units garrisoned in it are not separately targetable, and while
-- its defenses stand they take no damage. Hostility follows the same rule
-- as units (barbarian/free-city owner or at war). District defense reads
-- run in InGame context here (the same APIs build_attack_followup_query
-- uses); they are pcall-guarded because they are unverified in GameCore.
local distName, distHP, distMax = nil, 0, 0
local sawDistrictOwner = -1
do
    local plot = Map.GetPlot(tx, ty)
    if plot then
        pcall(function()
            local dIdx = plot:GetDistrictType()
            if dIdx and dIdx >= 0 then
                local dInfo = GameInfo.Districts[dIdx]
                if dInfo and (dInfo.HitPoints or 0) > 0 then
                    local dOwner = plot:GetOwner()
                    if dOwner ~= nil and dOwner ~= me and dOwner >= 0 then
                        sawDistrictOwner = dOwner
                        if dOwner >= 62
                            or Players[me]:GetDiplomacy():IsAtWarWith(dOwner) then
                            for _, c in Players[dOwner]:GetCities():Members() do
                                for _, d in c:GetDistricts():Members() do
                                    if d:GetX() == tx and d:GetY() == ty then
                                        distName = dInfo.DistrictType
                                        -- Walls (outer defense) absorb damage
                                        -- first; report the layer that takes
                                        -- it so the caller's pre/post HP
                                        -- comparison stays meaningful.
                                        local wMax = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                        local wHP = wMax - (d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0)
                                        local gMax = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                                        local gHP = gMax - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                                        if wMax > 0 then
                                            distHP, distMax = wHP, wMax
                                        else
                                            distHP, distMax = gHP, gMax
                                        end
                                        break
                                    end
                                end
                                if distName ~= nil then break end
                            end
                        end
                    end
                end
            end
        end)
    end
end

local function ownerDisplayName(pid)
    local name = "player " .. pid
    pcall(function()
        local cfg = PlayerConfigurations[pid]
        if cfg then name = Locale.Lookup(cfg:GetCivilizationDescription()) end
    end)
    return name
end

-- What does THIS attacker engage on the tile? (units.lua's selection; a
-- hostile defensible district outranks everything - it IS the combat
-- target. A religious attacker ignores the district: theological combat
-- engages the religious unit on the tile, never the district.)
local simName, simHP, simMax = nil, 0, 0
local isCapture, isTheological = false, false
if distName ~= nil and not aIsReligious then
    simName, simHP, simMax = distName, distHP, distMax
elseif defName ~= nil and not aIsReligious then
    simName, simHP, simMax = defName, defHP, defMax
elseif relName ~= nil and aIsReligious then
    simName, simHP, simMax = relName, relHP, relMax
    isTheological = true
elseif civName ~= nil and cs > 0 and dist == 1 and not isAir then
    simName, simHP, simMax = civName, civHP, civMax
    isCapture = true
end

if simName == nil then
    -- units.lua would list no target for this attacker either; explain why.
    if aIsReligious then
        print("ERR:WRONG_ATTACKER_TYPE|Religious units fight only other religious units (theological combat, which needs no war); there is none at (" .. tx .. "," .. ty .. ").")
    elseif sawForeignName == nil and sawDistrictOwner < 0 then
        print("ERR:NO_ENEMY|No hostile unit at (" .. tx .. "," .. ty .. ").")
    elseif sawForeignName == nil and sawDistrictOwner >= 0 then
        -- A defensible district is there but its owner is not hostile
        -- (a hostile one would have been selected as the target above).
        print("ERR:NOT_AT_WAR|Cannot attack the district at (" .. tx .. "," .. ty .. ") - you are at peace with " .. ownerDisplayName(sawDistrictOwner) .. ". Declare war first or target a different tile.")
    elseif sawRelName ~= nil and defName == nil and civName == nil then
        print("ERR:RELIGIOUS_TARGET|" .. sawRelName .. " is a religious unit - military units cannot attack it. Use an apostle/inquisitor for theological combat, or unit_action UNITCOMMAND_CONDEMN_HERETIC.")
    elseif civName ~= nil then
        if cs <= 0 or isAir then
            print("ERR:CAPTURE_NOT_POSSIBLE|" .. civName .. " at (" .. tx .. "," .. ty .. ") is an unescorted civilian; capturing it requires a melee-capable unit (Combat > 0, not air) moving onto the tile.")
        elseif dist > 1 then
            print("ERR:OUT_OF_RANGE|" .. civName .. " at (" .. tx .. "," .. ty .. ") is an unescorted civilian - move adjacent (distance 1) to capture it; current distance " .. dist .. ".")
        else
            print("ERR:ATTACK_BLOCKED|Cannot move onto (" .. tx .. "," .. ty .. ") to capture " .. civName .. ".")
        end
    else
        print("ERR:NOT_AT_WAR|Cannot attack " .. sawForeignName .. " - you are at peace with " .. ownerDisplayName(sawForeignOwner) .. ". Declare war first or target a different unit.")
    end
    print("__MCP_SENTINEL_TAG__")
    return
end

local myHP = unit:GetMaxDamage() - unit:GetDamage()
local params = {}
params[UnitOperationTypes.PARAM_X] = tx
params[UnitOperationTypes.PARAM_Y] = ty

if isAir then
    -- Air units attack through AIR_ATTACK exactly like the UI
    -- (Civ6Common.RequestMoveOperation); DEPLOY is its reposition-only
    -- fallback and is deliberately not issued here. Combat resolves
    -- asynchronously - the caller polls GameCore for the outcome.
    local rng = entry and entry.Range or 1
    if dist > rng then
        print("ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but air range is " .. rng .. ". Rebase closer first.")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
    if not UnitManager.CanStartOperation(unit, UnitOperationTypes.AIR_ATTACK, nil, params) then
        print("ERR:ATTACK_BLOCKED|Cannot air-attack " .. simName .. " at (" .. tx .. "," .. ty .. ") (dist " .. dist .. "). The unit may have already attacked this turn.")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.AIR_ATTACK, params)
    print("OK:AIR_ATTACK|target:" .. simName .. " at (" .. tx .. "," .. ty .. ")|pre_hp:" .. simHP .. "/" .. simMax .. "|your HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
    print("__MCP_SENTINEL_TAG__")
    return
end

if not isCapture and not isTheological and rs > 0 then
    -- Ranged fire at ANY distance in range - adjacent included. The UI's
    -- move path (Civ6Common.RequestMoveOperation) tries RANGE_ATTACK first
    -- with no distance branch; units.lua gates the listing the same way.
    local rng = entry and entry.Range or 1
    if dist > rng then
        print("ERR:OUT_OF_RANGE|Target at distance " .. dist .. " but range is " .. rng .. ". Move closer first.")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    if not UnitManager.CanStartOperation(unit, UnitOperationTypes.RANGE_ATTACK, nil, params) then
        print("ERR:NO_LOS|Cannot ranged-attack target at (" .. tx .. "," .. ty .. ") from (" .. ux .. "," .. uy .. "). LOS blocked or unit already attacked this turn.")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    UnitManager.RequestOperation(unit, UnitOperationTypes.RANGE_ATTACK, params)
    print("OK:RANGE_ATTACK|target:" .. simName .. " at (" .. tx .. "," .. ty .. ")|pre_hp:" .. simHP .. "/" .. simMax .. "|your HP:" .. myHP .. "|range:" .. rng .. " dist:" .. dist)
    print("__MCP_SENTINEL_TAG__")
    return
end

-- MOVE_TO attack-move: melee, civilian capture, theological combat (all
-- identical engine orders - a theological charge closes and strikes on
-- arrival like a melee charge, and the winner advances onto the tile).
params[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
    + UnitOperationMoveModifiers.MOVE_IGNORE_UNEXPLORED_DESTINATION
if not UnitManager.CanStartOperation(unit, UnitOperationTypes.MOVE_TO, nil, params) then
    if isTheological then
        -- Name the one theological blocker the generic message can't: units
        -- of the same religion never fight each other.
        local sameRel = false
        pcall(function()
            local myRel = unit:GetReligionType()
            if myRel ~= nil and myRel >= 0 and myRel == relUnit:GetReligionType() then
                sameRel = true
            end
        end)
        if sameRel then
            print("ERR:SAME_RELIGION|Cannot attack " .. simName .. " - it follows your own religion (theological combat only happens between different religions).")
            print("__MCP_SENTINEL_TAG__")
            return
        end
    end
    print("ERR:ATTACK_BLOCKED|Cannot attack " .. simName .. " at (" .. tx .. "," .. ty .. ") (map dist=" .. dist .. "). Unit not adjacent, cannot reach the tile, or blocked by popup/diplomacy.")
    print("__MCP_SENTINEL_TAG__")
    return
end
UnitManager.RequestOperation(unit, UnitOperationTypes.MOVE_TO, params)
-- Movement resolves synchronously; combat (if struck) resolves async
-- (post-combat HP comes from the caller's GameCore poll). Capture moves the
-- unit onto the tile (dist 0); melee and theological both stay adjacent (1)
-- and advance onto the tile when they defeat the target (0); anything > 1
-- means the unit could not close this turn.
local newX, newY = unit:GetX(), unit:GetY()
local newDist = Map.GetPlotDistance(newX, newY, tx, ty)
if newDist > 1 then
    print("ERR:STOPPED_SHORT|Unit moved to (" .. newX .. "," .. newY .. ") but could not reach target at (" .. tx .. "," .. ty .. ") - " .. newDist .. " tiles away. Movement exhausted by terrain. Try again next turn from closer position.")
    print("__MCP_SENTINEL_TAG__")
    return
end
-- Whether the strike actually landed is not provable here (attacking
-- zeroes movement and attacking needs movement, so a charge that ends
-- adjacent reads the same struck or halted; the engine can also silently
-- no-op the whole order). The caller's GameCore poll gives the concrete
-- outcome: damage on either combatant, district defense HP, or the
-- ownership change of a capture.
if isCapture then
    print("OK:CAPTURE|unit:" .. simName .. " at (" .. tx .. "," .. ty .. ")")
elseif isTheological then
    print("OK:THEOLOGICAL_ATTACK|target:" .. simName .. " at (" .. tx .. "," .. ty .. ")|pre_hp:" .. simHP .. "/" .. simMax .. "|your HP:" .. myHP)
else
    -- A civilian stacked with the defender is captured if the melee attack
    -- kills its escort (matches the units.lua ~captures: target listing).
    local capNote = ""
    if civName ~= nil then capNote = "|captures:" .. civName .. " if the defender dies" end
    print("OK:MELEE_ATTACK|target:" .. simName .. " at (" .. tx .. "," .. ty .. ")|pre_hp:" .. simHP .. "/" .. simMax .. "|your HP:" .. myHP .. "|CS:" .. cs .. capNote)
end
print("__MCP_SENTINEL_TAG__")
