-- List the units on a tile with their current HP (InGame context).
--
-- This file is a TEMPLATE loaded by build_attack_followup_query() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__ -> the response sentinel (see _helpers.SENTINEL)
--   __TARGET_X__         -> tile X
--   __TARGET_Y__         -> tile Y
--
-- Used by city_attack's post-combat report - a city ranged attack can only
-- ever target units, so units are all it needs to read back. Units come
-- straight from the tile (Map.GetUnitsAt returns nil for an empty tile).
--
-- Emits one UNIT|TYPE|hp/max|owner:N line per unit on the tile, or EMPTY.

local found = false
local tileUnits = Map.GetUnitsAt(__TARGET_X__, __TARGET_Y__)
if tileUnits then
    for u in tileUnits:Units() do
        local hp = u:GetMaxDamage() - u:GetDamage()
        local entry = GameInfo.Units[u:GetType()]
        local name = entry and entry.UnitType or "UNKNOWN"
        print("UNIT|" .. name .. "|" .. hp .. "/" .. u:GetMaxDamage() .. "|owner:" .. u:GetOwner())
        found = true
    end
end
if not found then print("EMPTY") end
print("__MCP_SENTINEL_TAG__")
