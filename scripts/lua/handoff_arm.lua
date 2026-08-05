-- Run in GameCore_Tuner. Mirrors src/civ_mcp/handoff.py's install script,
-- managing P1 only, for the diplomacy investigation. Idempotent; never
-- calls RemoveAll() (that would strip the game's own listeners).
local h = __civmcp_handoff
if h == nil then
  h = {}
  __civmcp_handoff = h
end
-- The human MUST be in the managed set too, otherwise the slot never returns
-- to them and the AI plays their turns. handoff.py includes the human id in
-- cfg.managed_ids for exactly this reason.
h.players = { [0] = true, [1] = true }
h.enabled = true
h.log = h.log or {}

if h.fn == nil then
  h.fn = function(pid)
    local s = __civmcp_handoff
    if not s.enabled then return end
    if not s.players[pid] then return end
    local before = Game.GetLocalPlayer()
    local ok, err = pcall(function()
      PlayerManager.SetLocalPlayerAndObserver(pid)
    end)
    s.log[#s.log + 1] = table.concat({
      tostring(Game.GetCurrentGameTurn()), tostring(pid),
      tostring(before), tostring(Game.GetLocalPlayer()),
      ok and "ok" or tostring(err) }, "|")
    while #s.log > 40 do table.remove(s.log, 1) end
  end
  GameEvents.PlayerTurnStarted.Add(h.fn)
  print("HANDOFF|installed managed=1")
else
  print("HANDOFF|updated managed=1")
end
