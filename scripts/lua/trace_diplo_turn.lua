-- Install in the DiplomacyActionView Lua state.
-- Records every event that can move g_bIsLocalPlayerTurn, which is the flag
-- that disables every action button (DiplomacyActionView.lua:736/740).
-- Our handlers run after the game's own, so the logged g= is the post-value.
if __MCP_trace == nil then
  __MCP_trace = {}

  local function note(tag)
    if #__MCP_trace > 200 then table.remove(__MCP_trace, 1) end
    __MCP_trace[#__MCP_trace + 1] = string.format(
      "%s turn=%s local=%s observer=%s g=%s",
      tag,
      tostring(Game.GetCurrentGameTurn()),
      tostring(Game.GetLocalPlayer()),
      tostring(Game.GetLocalObserver()),
      tostring(g_bIsLocalPlayerTurn))
  end

  Events.LocalPlayerTurnBegin.Add(function() note("TURN_BEGIN") end)
  Events.LocalPlayerTurnEnd.Add(function() note("TURN_END") end)
  Events.LocalPlayerChanged.Add(function(a, b)
    note("LOCAL_CHANGED(" .. tostring(a) .. "," .. tostring(b) .. ")")
  end)
  Events.PlayerTurnActivated.Add(function(pid, first)
    note("ACTIVATED(p" .. tostring(pid) .. "," .. tostring(first) .. ")")
  end)
  note("INSTALLED")
end

print("MCPTRACE installed, entries=" .. tostring(#__MCP_trace))
