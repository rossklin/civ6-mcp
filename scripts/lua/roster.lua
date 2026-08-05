-- Run in GameCore_Tuner. Lists the major civs and who holds the human slot.
print("ROSTER turn=" .. tostring(Game.GetCurrentGameTurn())
  .. " local=" .. tostring(Game.GetLocalPlayer()))
for i = 0, 7 do
  local p = Players[i]
  if p ~= nil and p:IsMajor() then
    local cfg = PlayerConfigurations[i]
    print(string.format("  P%d %s / %s  human=%s turnActive=%s",
      i,
      tostring(cfg:GetCivilizationTypeName()),
      tostring(cfg:GetLeaderTypeName()),
      tostring(p:IsHuman()),
      tostring(p:IsTurnActive())))
  end
end
