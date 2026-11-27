-- Read true post-combat state after an attack (GameCore context).
--
-- This file is a TEMPLATE loaded by build_attack_outcome_query() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__ -> the response sentinel (see _helpers.SENTINEL)
--   __ATTACKER_UNIT_ID__ -> engine unit id of the attacker (FindID key)
--   __TARGET_X__         -> target tile X
--   __TARGET_Y__         -> target tile Y
--   __TARGET_CLASS__     -> what the attack engaged: MILITARY (defender or
--                           district), RELIGIOUS (theological combat) or
--                           SUPPORT (a lone support unit)
--   LUA_OCCUPANCY_CLASS tag -> shared occupancyClass helper (_helpers.py).
--                           Not spelled with its double underscores here:
--                           the multi-line helper would be substituted
--                           into this comment and break the script.
--
-- InGame state does not reflect post-combat HP within the same turn, but
-- GameCore (authoritative sim state) does. Reads the attacker's real HP and
-- the enemy at the target tile. A defensible district (city center,
-- encampment, ... - GameInfo HitPoints > 0) outranks units on its tile:
-- combat hits the district while its defenses stand, so the district's
-- defense layer (walls first, else district HP) becomes the enemy stats -
-- matching the pre_hp attack_unit.lua reports for district targets. A
-- district whose city now belongs to the local player was captured.
-- Otherwise the enemy is the unit of the occupancy class the attack engaged
-- (religious unit for theological combat, a lone support unit, else the
-- military defender).
--
-- Emits OUTCOME|att_hp:N|att_max:N|enemy:TYPE|enemy_hp:N|enemy_max:N,
-- ...|enemy:CAPTURED_CITY after a city capture, or ...|enemy:KILLED when no
-- enemy remains - or ERR:DISTRICT_LOOKUP|reason when the district defense
-- read fails (those APIs are unverified in GameCore; every other district
-- read in this repo runs in InGame context).

local me = Game.GetLocalPlayer()
local tx, ty = __TARGET_X__, __TARGET_Y__
local targetClass = "__TARGET_CLASS__"
local attacker = Players[me]:GetUnits():FindID(__ATTACKER_UNIT_ID__)
local attHP, attMax = -1, -1
if attacker then
    attMax = attacker:GetMaxDamage()
    attHP = attMax - attacker:GetDamage()
end
local enemyFound = false
local enemyName = "KILLED"
local enemyHP, enemyMax = 0, 0
local plot = Map.GetPlot(tx, ty)
-- Defensible district first: combat targets it, not the garrisoned units
-- (only a MILITARY-class attack engages a district). CityManager.GetDistrictAt
-- resolves the district straight from the tile. A failed district read is
-- reported as ERR:DISTRICT_LOOKUP - it must not fall through to the unit
-- lookup, which would misreport a garrisoned unit as the target.
if plot and targetClass == "MILITARY" then
    local ok, err = pcall(function()
        local dIdx = plot:GetDistrictType()
        if dIdx and dIdx >= 0 then
            local dInfo = GameInfo.Districts[dIdx]
            if dInfo and (dInfo.HitPoints or 0) > 0 then
                local dOwner = plot:GetOwner()
                if dOwner == me then
                    enemyName = "CAPTURED_CITY"
                    enemyFound = true
                elseif dOwner ~= nil and dOwner >= 0 then
                    local d = CityManager.GetDistrictAt(tx, ty)
                    if d == nil then
                        error("CityManager.GetDistrictAt returned nil for a " .. tostring(dInfo.DistrictType) .. " tile")
                    end
                    enemyName = dInfo.DistrictType
                    local wMax = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                    local wHP = wMax - (d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0)
                    local gMax = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                    local gHP = gMax - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                    if wMax > 0 then
                        enemyHP, enemyMax = wHP, wMax
                    else
                        enemyHP, enemyMax = gHP, gMax
                    end
                    enemyFound = true
                end
            end
        end
    end)
    if not ok then
        print("ERR:DISTRICT_LOOKUP|" .. tostring(err))
        print("__MCP_SENTINEL_TAG__")
        return
    end
end
-- Otherwise the unit this attack engaged, picked by effective occupancy
-- class exactly like attack_unit.lua: a theological attack engages the
-- religious unit, an attack tagged target_class:SUPPORT the lone support
-- unit, any other attack the military defender (neither religious, civilian
-- nor support - FORMATION_CLASS_SUPPORT units such as siege towers stack
-- with the defender like civilians do). A civilian, support or religious
-- unit stacked with the defender is never the combat target, so it must not
-- be read as the enemy - when the defender dies the tile reports KILLED
-- even if its civilian is still there.
-- (Map.GetUnitsAt takes the plot object in GameCore, as the game's own
-- Maps/Utility/PlotFiltering.lua calls it.)
__LUA_OCCUPANCY_CLASS__
local function targetCategory(info)
    local class = occupancyClass(info)
    if class == "RELIGIOUS" then return "RELIGIOUS" end
    if class == "FORMATION_CLASS_CIVILIAN" then return "CIVILIAN" end
    if class == "FORMATION_CLASS_SUPPORT" then return "SUPPORT" end
    return "MILITARY"
end
if not enemyFound and plot then
    local tileUnits = Map.GetUnitsAt(plot)
    if tileUnits then
        for u in tileUnits:Units() do
            if u:GetOwner() ~= me then
                local entry = GameInfo.Units[u:GetType()]
                if targetCategory(entry) == targetClass then
                    enemyName = entry and entry.UnitType or "UNKNOWN"
                    enemyMax = u:GetMaxDamage()
                    enemyHP = enemyMax - u:GetDamage()
                    enemyFound = true
                    break
                end
            end
        end
    end
end
local out = "OUTCOME|att_hp:" .. attHP .. "|att_max:" .. attMax .. "|enemy:"
if enemyName == "CAPTURED_CITY" then
    out = out .. "CAPTURED_CITY"
elseif enemyFound then
    out = out .. enemyName .. "|enemy_hp:" .. enemyHP .. "|enemy_max:" .. enemyMax
else
    out = out .. "KILLED"
end
print(out)
print("__MCP_SENTINEL_TAG__")
