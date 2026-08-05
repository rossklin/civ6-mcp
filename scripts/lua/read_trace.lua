-- Run in DiplomacyActionView. Dumps the turn-event trace and current gating state.
print("NOW g_bIsLocalPlayerTurn=" .. tostring(g_bIsLocalPlayerTurn)
  .. " ms_LocalPlayerID=" .. tostring(ms_LocalPlayerID)
  .. " ms_SelectedPlayerID=" .. tostring(ms_SelectedPlayerID)
  .. " local=" .. tostring(Game.GetLocalPlayer())
  .. " hidden=" .. tostring(ContextPtr:IsHidden()))
if __MCP_trace ~= nil then
  for i, line in ipairs(__MCP_trace) do print("T[" .. i .. "] " .. line) end
else
  print("TRACE MISSING - context was rebuilt")
end
