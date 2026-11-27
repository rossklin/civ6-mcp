-- Check whether the local player has a city on a tile (GameCore context).
--
-- This file is a TEMPLATE loaded by build_verify_city_at() in map.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__ -> the response sentinel (see _helpers.SENTINEL)
--   __X__                -> tile X
--   __Y__                -> tile Y
--
-- The city comes straight from the tile via CityManager.GetCityAt(x, y) -
-- the getter the game's own gameplay scripts use (e.g.
-- BlackDeathScenario.lua). Only the local player's city counts.
--
-- City IDs can be 0 (e.g. the capital), so the parser tells "found" from
-- "not found" by line prefix: OK:CITY_EXISTS|id or OK:NO_CITY.

local me = Game.GetLocalPlayer()
local cityId = -1
local city = CityManager.GetCityAt(__X__, __Y__)
if city ~= nil and city:GetOwner() == me then
    cityId = city:GetID()
end
if cityId >= 0 then
    print("OK:CITY_EXISTS|" .. cityId)
else
    print("OK:NO_CITY")
end
print("__MCP_SENTINEL_TAG__")
