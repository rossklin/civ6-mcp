-- Describe a given list of tiles, one text line each (InGame context).
--
-- This file is a TEMPLATE loaded by build_describe_tiles_query() in map.py,
-- which replaces three tokens in the code below: the tile-describer token
-- (with tile_describe.lua, defining describeTile(plot)), the tiles token
-- (with the {x, y} pairs to describe) and the sentinel tag (with the
-- response sentinel, _helpers.SENTINEL).
--
-- Used to report what a unit move revealed. The lines are produced by the
-- same describeTile() as the full map dump, so they read identically.
-- Each line is prefixed "TILE|x,y|" so the caller can tell which requested
-- tile it belongs to; tiles that are not revealed produce no line.
__LUA_TILE_DESCRIBE__

local tiles = { __MCP_TILES__ }
for _, t in ipairs(tiles) do
    local plot = Map.GetPlot(t[1], t[2])
    if plot then
        local line = describeTile(plot)
        if line then print("TILE|" .. t[1] .. "," .. t[2] .. "|" .. line) end
    end
end
print("__MCP_SENTINEL_TAG__")
