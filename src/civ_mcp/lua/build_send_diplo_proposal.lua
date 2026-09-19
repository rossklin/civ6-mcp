-- Open a response-able diplomatic proposal (friendship / delegation /
-- embassy) toward a NON-MANAGED (built-in AI) player.
--
-- This chunk does ONLY what the native UI's button does: a bare
-- RequestSession (DiplomacyActionView.lua OnSelectInitialDiplomacyStatement
-- ~470 — the UI sends no SendAction and no AddResponse). The engine's AI
-- computes its own answer on later frames and applies the effect from that
-- response; the local player's only native input afterwards is the goodbye
-- CloseSession. Any proposer-side AddResponse is ignored (the completing
-- response belongs to the TARGET), and a same-frame CloseSession kills the
-- session before the engine ever processes it — both mistakes of the old
-- flow this template replaced (live-verified failure; see
-- DIPLO_EXECUTION_PLAN.md §1).
--
-- Python (GameState._await_diplo_ai_answer) polls the validity-flip oracle
-- for the AI's answer, then tears the session + the popped leader screen
-- down. Response-able proposals to MANAGED civs never reach this template
-- — server routing files them in the diplo mailbox and the target-local
-- recipe executes them at accept time.
--
-- This file is a TEMPLATE loaded by build_send_diplo_action() in diplomacy.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_TARGET_TAG__         -> target player id
--   __MCP_ACTION_TAG__         -> action name (e.g. DECLARE_FRIENDSHIP)
--   __MCP_SESSION_STRING_TAG__ -> RequestSession string (DECLARE_FRIENDSHIP
--                                 -> "DECLARE_FRIEND"; others: the action name)
--   __MCP_SENTINEL_TAG__       -> the response sentinel (see _helpers.SENTINEL)
-- Python also splices the shared IsDiplomaticActionValid pre-check Lua into
-- the validity slot below (see _diplo_action_validity_lua in diplomacy.py) —
-- its token is deliberately not spelled out here so this header is not
-- mangled by the substitution. The chunk prints the session id, the local
-- player id (for the follow-up builders' from/to arguments) and the target
-- name for Python to parse.

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
-- The native button equivalent: request the session and stop. No
-- AddResponse, no CloseSession in this frame (see the header).
DiplomacyManager.RequestSession(me, target, "__MCP_SESSION_STRING_TAG__")
local sid = DiplomacyManager.FindOpenSessionID(me, target)
if sid and sid >= 0 then
    local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
    print("OK:SESSION_OPENED|" .. sid .. "|" .. me .. "|" .. name:gsub("|","/"))
else
    print("ERR:NO_SESSION|RequestSession opened no session")
end
print("__MCP_SENTINEL_TAG__")
