-- Tribal village (goody hut) reward log reader (post-move diff).
--
-- This file is a TEMPLATE loaded by build_read_goody_log() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--   __SINCE_SEQ__         -> only emit log entries with sequence > this value
--
-- Emits one GOODY|seq|turn|player|unit|subtype|category|desc line per new
-- reward, or GOODY_NONE when there are no new entries. The listener that
-- populates __civmcp_goody is installed by build_goody_snapshot_query.lua.

local g = __civmcp_goody
if not g or not g.log then print("GOODY_NONE"); print("__MCP_SENTINEL_TAG__"); return end
local n = 0
for _, entry in ipairs(g.log) do
  local seq = tonumber(entry:match("^%d+"))
  if seq and seq > __SINCE_SEQ__ then
    print("GOODY|" .. entry)
    n = n + 1
  end
end
if n == 0 then print("GOODY_NONE") end
print("__MCP_SENTINEL_TAG__")
