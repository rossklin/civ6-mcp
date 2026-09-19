-- Send a proactive ONE-WAY diplomatic statement (denounce) and clean up.
--
-- One-way statements need no response from the target: the proposer's own
-- AddResponse(POSITIVE) calls advance the statement playback, the session is
-- closed in the same chunk, and any stragglers are swept with a short
-- NEGATIVE/CloseSession loop. No AddStatement (that crashes on mismatched
-- session types).
--
-- No view dismissal here, deliberately: the statement events pop
-- DiplomacyActionView on later frames, so a same-frame hide runs before the
-- view processes them (leaving the screen up or in a stale state), and raw
-- hide events skip the view's teardown entirely (frozen-UI risk).
-- game_state.send_diplomatic_action schedules the delayed DAV-context
-- Close() via _cleanup_diplo_screen instead.
--
-- Wars have their own template (build_send_war_declaration.lua — different
-- validation, session left open for the leader animation); response-able
-- proposals to built-in AIs have theirs (build_send_diplo_proposal.lua).
--
-- This file is a TEMPLATE loaded by build_send_diplo_action() in diplomacy.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_TARGET_TAG__         -> target player id
--   __MCP_ACTION_TAG__         -> action name (e.g. DENOUNCE)
--   __MCP_SESSION_STRING_TAG__ -> DiplomacyManager.RequestSession session string
--   __MCP_SENTINEL_TAG__       -> the response sentinel (see _helpers.SENTINEL)
-- Python also splices the shared IsDiplomaticActionValid pre-check Lua into
-- the validity slot below (see _diplo_action_validity_lua in diplomacy.py) —
-- its token is deliberately not spelled out here so this header is not
-- mangled by the substitution.
--
-- Key discovery: RequestSession uses DIFFERENT action strings from DIPLOACTION_
-- names for some actions (the mapping lives in diplomacy.py,
-- DIPLO_SESSION_STRING_MAP).

local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local target = __MCP_TARGET_TAG__
local action = "__MCP_ACTION_TAG__"
__MCP_VALIDITY_BLOCK_TAG__
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
    DiplomacyManager.CloseSession(sid)
end
for r = 1, 5 do
    sid = DiplomacyManager.FindOpenSessionID(me, target)
    if not sid or sid < 0 then break end
    DiplomacyManager.AddResponse(sid, me, "NEGATIVE")
    sid = DiplomacyManager.FindOpenSessionID(me, target)
    if not sid or sid < 0 then break end
    DiplomacyManager.CloseSession(sid)
end
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
if action == "DENOUNCE" then
    print("OK:SENT|Denounced " .. name)
else
    print("OK:SENT|" .. action .. " sent to " .. name)
end
print("__MCP_SENTINEL_TAG__")
