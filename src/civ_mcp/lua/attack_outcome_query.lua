-- Read true post-combat state after an attack (GameCore context).
--
-- This file is a TEMPLATE loaded by build_attack_outcome_query() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__ -> the response sentinel (see _helpers.SENTINEL)
--   __ATTACKER_UNIT_ID__ -> engine unit id of the attacker (FindID key)
--   __TARGET_X__         -> target tile X
--   __TARGET_Y__         -> target tile Y
--
-- InGame state does not reflect post-combat HP within the same turn, but
-- GameCore (authoritative sim state) does. Reads the attacker's real HP and
-- the enemy at the target tile. A defensible district (city center,
-- encampment, ... - GameInfo HitPoints > 0) outranks units on its tile:
-- combat hits the district while its defenses stand, so the district's
-- defense layer (walls first, else district HP) becomes the enemy stats -
-- matching the pre_hp attack_unit.lua reports for district targets. A
-- district whose city now belongs to the local player was captured.
--
-- Emits OUTCOME|att_hp:N|att_max:N|enemy:TYPE|enemy_hp:N|enemy_max:N,
-- ...|enemy:CAPTURED_CITY after a city capture, or ...|enemy:KILLED when no
-- enemy remains.
--
-- District defense reads (GetDistricts/GetMaxDamage) are pcall-guarded:
-- every other district read in this repo runs in InGame context, and the old
-- claim that they are unavailable in GameCore is unverified. If they do fail
-- here, district attacks degrade to unresolved (no stats) rather than
-- misreporting.

local me = Game.GetLocalPlayer()
local tx, ty = __TARGET_X__, __TARGET_Y__
local attacker = Players[me]:GetUnits():FindID(__ATTACKER_UNIT_ID__)
local attHP, attMax = -1, -1
if attacker then
    attMax = attacker:GetMaxDamage()
    attHP = attMax - attacker:GetDamage()
end
local enemyFound = false
local enemyName = "KILLED"
local enemyHP, enemyMax = 0, 0
-- Defensible district first: combat targets it, not the garrisoned units.
pcall(function()
    local plot = Map.GetPlot(tx, ty)
    if plot then
        local dIdx = plot:GetDistrictType()
        if dIdx and dIdx >= 0 then
            local dInfo = GameInfo.Districts[dIdx]
            if dInfo and (dInfo.HitPoints or 0) > 0 then
                local dOwner = plot:GetOwner()
                if dOwner == me then
                    enemyName = "CAPTURED_CITY"
                    enemyFound = true
                elseif dOwner ~= nil and dOwner >= 0 then
                    for _, c in Players[dOwner]:GetCities():Members() do
                        for _, d in c:GetDistricts():Members() do
                            if d:GetX() == tx and d:GetY() == ty then
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
                                break
                            end
                        end
                        if enemyFound then break end
                    end
                end
            end
        end
    end
end)
if not enemyFound then
    for i = 0, 63 do
        if Players[i] and Players[i]:IsAlive() and i ~= me then
            for _, u in Players[i]:GetUnits():Members() do
                if u:GetX() == tx and u:GetY() == ty then
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
