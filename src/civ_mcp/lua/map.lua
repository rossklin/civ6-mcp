-- Map dump of explored tiles, emitted as natural text.
--
-- This file is a TEMPLATE loaded by build_map_query() in map.py, which
-- replaces the token on the first code line below with the shared tile
-- describer (tile_describe.lua, defining describeTile(plot)) and the
-- sentinel tag at the end with the response sentinel (_helpers.SENTINEL).
__LUA_TILE_DESCRIBE__

local w, h = Map.GetGridSize()

-- Scan every tile north to south; describeTile returns nil for unrevealed ones.
local lines = {}
for y = h - 1, 0, -1 do
    for x = 0, w - 1 do
        local plot = Map.GetPlot(x, y)
        if plot then
            local line = describeTile(plot)
            if line then lines[#lines + 1] = line end
        end
    end
end

if #lines == 0 then
    print("No tiles.")
else
    print(#lines .. " tiles:")
    print(table.concat(lines, "\n"))
end
print("__MCP_SENTINEL_TAG__")
