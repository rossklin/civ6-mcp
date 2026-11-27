-- List the local player's units on a tile (GameCore context).
--
-- This file is a TEMPLATE loaded by build_capture_outcome_query() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__ -> the response sentinel (see _helpers.SENTINEL)
--   __TARGET_X__         -> tile X
--   __TARGET_Y__         -> tile Y
--
-- A civilian capture lands no damage, so it is verified by ownership: the
-- captured unit joins the local player and shows up here together with the
-- capturing unit. Units come straight from the tile (Map.GetUnitsAt takes
-- the plot object in GameCore, live-verified; nil for an empty tile).
--
-- Emits one OURS|TYPE line per local-player unit on the tile.

local me = Game.GetLocalPlayer()
local plot = Map.GetPlot(__TARGET_X__, __TARGET_Y__)
local tileUnits = plot and Map.GetUnitsAt(plot) or nil
if tileUnits then
    for u in tileUnits:Units() do
        if u:GetOwner() == me then
            local entry = GameInfo.Units[u:GetType()]
            print("OURS|" .. (entry and entry.UnitType or "UNKNOWN"))
        end
    end
end
print("__MCP_SENTINEL_TAG__")
