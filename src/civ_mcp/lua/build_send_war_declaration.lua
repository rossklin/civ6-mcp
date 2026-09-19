-- Declare war on a player and leave the session open for the animation.
--
-- Wars validate via CanDeclareWarOn (not IsDiplomaticActionValid) and the
-- session is deliberately NOT closed in this chunk: the declaration leader
-- animation plays first. Python schedules the close + the DAV-context
-- Close() dismiss ~8s later (game_state._cleanup_war_diplomacy:
-- build_war_close_session, then handoff.build_dismiss_leader_screen_lua).
--
-- One-way statements (denounce) have their own template
-- (build_send_diplo_statement.lua — same-chunk close + sweep); response-able
-- proposals to built-in AIs have theirs (build_send_diplo_proposal.lua).
--
-- This file is a TEMPLATE loaded by build_send_diplo_action() in diplomacy.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_TARGET_TAG__         -> target player id
--   __MCP_ACTION_TAG__         -> war action name (e.g. DECLARE_FORMAL_WAR)
--   __MCP_SESSION_STRING_TAG__ -> RequestSession string (wars: the action name)
--   __MCP_SENTINEL_TAG__       -> the response sentinel (see _helpers.SENTINEL)

local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local target = __MCP_TARGET_TAG__
local action = "__MCP_ACTION_TAG__"
if pDiplo:IsAtWarWith(target) then
    print("ERR:ALREADY_AT_WAR|Already at war with this player"); print("__MCP_SENTINEL_TAG__"); return
end
local canWar = false
pcall(function() canWar = pDiplo:CanDeclareWarOn(target) end)
if not canWar then
    print("ERR:CANNOT_DECLARE_WAR|Cannot declare war. Possible reasons: friendship/alliance active, 10-turn peace cooldown, or target is invalid."); print("__MCP_SENTINEL_TAG__"); return
end
-- Clean stale session for THIS target only (not all session IDs).
-- Mass-closing sessions via IsSessionIDOpen loop corrupts AI diplomacy state.
local staleSid = DiplomacyManager.FindOpenSessionID(me, target)
if staleSid and staleSid >= 0 then
    DiplomacyManager.CloseSession(staleSid)
end
-- Open session with the correct action string
DiplomacyManager.RequestSession(me, target, "__MCP_SESSION_STRING_TAG__")
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if sid and sid >= 0 then
    DiplomacyManager.AddResponse(sid, me, "POSITIVE")
    DiplomacyManager.AddResponse(sid, me, "POSITIVE")
    -- No CloseSession: the session stays open so the leader animation
    -- plays; Python closes it later (see the header).
end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
print("OK:WAR_DECLARED|" .. action .. " on " .. name .. " — now at war")
print("__MCP_SENTINEL_TAG__")
