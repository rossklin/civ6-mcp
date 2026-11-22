---@diagnostic disable: lowercase-global
-- Pre-move visibility snapshot (GameCore context).
--
-- NOT a standalone query: build_pre_move_snapshot_query() in units.py
-- prepends this to the tribal-village snapshot so both ride one round trip
-- (that query prints the sentinel).
--
-- Records which plots the local player has revealed and which are currently
-- visible, in the persistent global __civmcp_vis (same persistence pattern
-- as __civmcp_goody). After the move, visibility_diff.lua compares the
-- current state against it. Diffing before/after inside the game means no
-- sight range or radius has to be assumed, whatever the unit can see.
--
-- Wrapped in pcall so a failure here can never block the move.
pcall(function()
    local me = Game.GetLocalPlayer()
    local vis = PlayersVisibility[me]
    local rev, seen = {}, {}
    for idx = 0, Map.GetPlotCount() - 1 do
        local p = Map.GetPlotByIndex(idx)
        local x, y = p:GetX(), p:GetY()
        if vis:IsRevealed(x, y) then
            rev[idx] = true
            if vis:IsVisible(x, y) then seen[idx] = true end
        end
    end
    __civmcp_vis = { player = me, turn = Game.GetCurrentGameTurn(), rev = rev, seen = seen }
end)
