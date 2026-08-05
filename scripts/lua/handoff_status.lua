-- Run in GameCore_Tuner. Reports hook state, slot ownership and the ring buffer.
local h = __civmcp_handoff
print("HOOK|" .. tostring(h ~= nil and h.fn ~= nil and h.enabled == true)
  .. " turn=" .. tostring(Game.GetCurrentGameTurn())
  .. " local=" .. tostring(Game.GetLocalPlayer()))
for i = 0, 3 do
  local p = Players[i]
  if p ~= nil then
    print(string.format("  P%d human=%s turnActive=%s", i,
      tostring(p:IsHuman()), tostring(p:IsTurnActive())))
  end
end
if h ~= nil and h.log ~= nil then
  for i, line in ipairs(h.log) do print("LOG[" .. i .. "] " .. line) end
end
