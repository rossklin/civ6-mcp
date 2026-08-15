---@diagnostic disable: lowercase-global
-- Allow lowercase global to avoid confusion with template parameters (e.g. __TARGET_X__).
-- Tribal village (goody hut) reward listener installer + pre-move snapshot.
--
-- This file is a TEMPLATE loaded by build_goody_snapshot_query() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--   __TARGET_X__          -> destination tile X
--   __TARGET_Y__          -> destination tile Y
--
-- Civ 6 grants a reward when a unit enters a tribal village
-- (IMPROVEMENT_GOODY_HUT, RemoveOnEntry). The reward subtype is chosen in C++
-- and exposed to Lua only through the transient GameEvents.UnitTriggerGoodyHut
-- event - there is no player API to query the last reward. So we install a
-- persistent listener (GameCore context, same pattern as the turn-handoff
-- handler in handoff.py) that logs each reward with a monotonic sequence
-- number. move_unit snapshots the sequence before issuing the move and diffs
-- afterward via build_read_goody_log.lua.
--
-- The listener is idempotent: re-running after a save load (Lua state
-- recycled, global nil) re-installs automatically, so calling this every move
-- also recovers from a recycled state.
--
-- Emits GOODY_SEQ|<seq>|<expect> where seq is the last sequence assigned
-- (0 if none yet) and expect is 1 when the destination tile currently holds a
-- goody hut (so the caller knows to poll if no reward is captured immediately).

__civmcp_goody = __civmcp_goody or {}
local g = __civmcp_goody
g.log = g.log or {}
g.seq = g.seq or 0
if not g.fn then
  g.fn = function(playerID, unitID, goodyHutType)
    local s = __civmcp_goody
    s.seq = s.seq + 1
    local sub = nil
    pcall(function() sub = GameInfo.GoodyHutSubTypes[goodyHutType] end)
    local name = (sub and sub.SubTypeGoodyHut) or ("IDX_" .. tostring(goodyHutType))
    local cat = (sub and sub.GoodyHut) or ""
    local desc = ""
    if sub and sub.Description and sub.Description ~= "" then
      pcall(function()
        local t = Locale.Lookup(sub.Description)
        if t then
          t = t:gsub("%b[]", "")        -- strip [COLOR_*]/[ICON_*]/[ENDCOLOR] tags
          t = t:gsub("{.-}", "")         -- strip {1_Num}-style placeholders
          t = t:gsub("|", "/")
          t = t:gsub("^%s+", ""):gsub("%s+$", "")
          -- Keep only if something alphanumeric survived (a bare "+" from
          -- stripped tokens is useless; fall back to the subtype name instead).
          if t:match("[%w]") then desc = t end
        end
      end)
    end
    s.log[#s.log + 1] = s.seq .. "|" .. tostring(Game.GetCurrentGameTurn()) .. "|" .. tostring(playerID) .. "|" .. tostring(unitID) .. "|" .. name .. "|" .. cat .. "|" .. desc
    while #s.log > 50 do table.remove(s.log, 1) end
  end
  pcall(function() GameEvents.UnitTriggerGoodyHut.Add(g.fn) end)
end
local expect = 0
local plot = Map.GetPlot(__TARGET_X__, __TARGET_Y__)
if plot then
  local ii = plot:GetImprovementType()
  if ii and ii >= 0 then
    local iInfo = GameInfo.Improvements[ii]
    if iInfo and iInfo.Goody then expect = 1 end
  end
end
print("GOODY_SEQ|" .. tostring(g.seq) .. "|" .. expect)
print("__MCP_SENTINEL_TAG__")
