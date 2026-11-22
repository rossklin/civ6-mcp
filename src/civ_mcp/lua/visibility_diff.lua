---@diagnostic disable: lowercase-global
-- Post-move visibility diff (GameCore context).
--
-- This file is a TEMPLATE loaded by build_visibility_diff_query() in units.py.
--   __MCP_SENTINEL_TAG__  the response sentinel (see _helpers.SENTINEL)
--
-- Compares the current visibility against the snapshot taken just before
-- the move by visibility_snapshot.lua and emits:
--   VIS_NEW|x,y;x,y;...   plots revealed for the first time by this move
--   VIS_UNITS|x,y;...     plots that were already explored but only now came
--                         into view AND hold a foreign unit
--   VIS_NONE              no usable snapshot (e.g. Lua state recycled)
-- Either list may be empty ("VIS_NEW|"). Keying on the plot's visibility
-- CHANGE means a foreign unit is reported once, when its plot comes into
-- view, and not again by later moves while it stays in view.
local snap = __civmcp_vis
local me = Game.GetLocalPlayer()
if not snap or snap.player ~= me or snap.turn ~= Game.GetCurrentGameTurn() then
    print("VIS_NONE")
    print("__MCP_SENTINEL_TAG__")
    return
end
local vis = PlayersVisibility[me]

-- Plots holding a foreign unit, by plot index.
local foreign = {}
for pid = 0, 63 do
    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
        local units = Players[pid]:GetUnits()
        if units then
            for _, u in units:Members() do
                local p = Map.GetPlot(u:GetX(), u:GetY())
                if p then foreign[p:GetIndex()] = true end
            end
        end
    end
end

local newTiles, unitTiles = {}, {}
for idx = 0, Map.GetPlotCount() - 1 do
    local p = Map.GetPlotByIndex(idx)
    local x, y = p:GetX(), p:GetY()
    if vis:IsRevealed(x, y) then
        if not snap.rev[idx] then
            newTiles[#newTiles + 1] = x .. "," .. y
        elseif foreign[idx] and not snap.seen[idx] and vis:IsVisible(x, y) then
            unitTiles[#unitTiles + 1] = x .. "," .. y
        end
    end
end
-- The snapshot is single-use: a later diff must not compare against it.
__civmcp_vis = nil
print("VIS_NEW|" .. table.concat(newTiles, ";"))
print("VIS_UNITS|" .. table.concat(unitTiles, ";"))
print("__MCP_SENTINEL_TAG__")
