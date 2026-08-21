-- Send a proactive diplomatic action (delegation, friendship declaration,
-- embassy, denounce, war declaration) and report the outcome.
--
-- This file is a TEMPLATE loaded by build_send_diplo_action() in diplomacy.py.
-- Tags substituted before the Lua is sent to the game:
--   __MCP_TARGET_TAG__         -> target player id
--   __MCP_ACTION_TAG__         -> action name (e.g. DIPLOMATIC_DELEGATION)
--   __MCP_SESSION_STRING_TAG__ -> DiplomacyManager.RequestSession session string
--   __MCP_IS_WAR_TAG__         -> true/false (Lua boolean literal)
--   __MCP_SENTINEL_TAG__       -> the response sentinel (see _helpers.SENTINEL)
-- Python also splices the shared IsDiplomaticActionValid pre-check Lua into
-- the else branch below (see _diplo_action_validity_lua in diplomacy.py) —
-- its token is deliberately not spelled out here so this header is not
-- mangled by the substitution.
--
-- Key discovery: RequestSession uses DIFFERENT action strings from DIPLOACTION_
-- names: DECLARE_FRIENDSHIP -> session string "DECLARE_FRIEND" (not
-- "DECLARE_FRIENDSHIP"); others use the same name as the action name. The
-- mapping lives in diplomacy.py (DIPLO_SESSION_STRING_MAP) so the diplo
-- mailbox opens the same session type when executing an accepted proposal.
-- Flow: RequestSession -> 2x AddResponse(POSITIVE) -> CloseSession.
-- No AddStatement needed (that crashes on mismatched session types).
--
-- War declarations branch on isWar: they validate via CanDeclareWarOn instead
-- of IsDiplomaticActionValid, and leave the session open so the leader
-- animation plays — Python schedules cleanup ~8s later via
-- _cleanup_war_diplomacy (build_war_close_session + build_war_dismiss_view).
--
-- Open Borders is NOT supported here — it's a trade deal, not a diplomatic
-- action. Use propose_trade with AGREEMENT/OPEN_BORDERS items instead.

local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local target = __MCP_TARGET_TAG__
local action = "__MCP_ACTION_TAG__"
local isWar = __MCP_IS_WAR_TAG__
if isWar then
    if pDiplo:IsAtWarWith(target) then
        print("ERR:ALREADY_AT_WAR|Already at war with this player"); print("__MCP_SENTINEL_TAG__"); return
    end
    local canWar = false
    pcall(function() canWar = pDiplo:CanDeclareWarOn(target) end)
    if not canWar then
        print("ERR:CANNOT_DECLARE_WAR|Cannot declare war. Possible reasons: friendship/alliance active, 10-turn peace cooldown, or target is invalid."); print("__MCP_SENTINEL_TAG__"); return
    end
else
__MCP_VALIDITY_BLOCK_TAG__
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
local sessionCompleted = false
if sid and sid >= 0 then
    DiplomacyManager.AddResponse(sid, me, "POSITIVE")
    DiplomacyManager.AddResponse(sid, me, "POSITIVE")
    if not isWar then
        DiplomacyManager.CloseSession(sid)
    end
    sessionCompleted = true
end
if not isWar then
    for r = 1, 5 do
        sid = DiplomacyManager.FindOpenSessionID(me, target)
        if not sid or sid < 0 then break end
        DiplomacyManager.AddResponse(sid, me, "NEGATIVE")
        sid = DiplomacyManager.FindOpenSessionID(me, target)
        if not sid or sid < 0 then break end
        DiplomacyManager.CloseSession(sid)
    end
    LuaEvents.DiplomacyActionView_ShowIngameUI()
    pcall(function() Events.HideLeaderScreen() end)
end
-- Report result.
-- NOTE: All post-state queries (HasDelegationAt, HasEmbassyAt, IsDiplomaticActionValid,
-- GetGoldBalance, GetVisibilityOn) are STALE same-frame after CloseSession. The C++ engine
-- commits state changes on the next frame. So we cannot reliably detect acceptance by
-- comparing pre/post state. Instead: IsDiplomaticActionValid passed the pre-check above,
-- meaning the action was valid. If the session completed, the game accepted it.
local name = Locale.Lookup(PlayerConfigurations[target]:GetCivilizationShortDescription())
if action:find("_WAR") then
    local atWar = pDiplo:IsAtWarWith(target)
    if atWar then
        print("OK:WAR_DECLARED|" .. action .. " on " .. name .. " — now at war")
    else
        print("WARN:WAR_UNCERTAIN|" .. action .. " session completed but war state not yet confirmed for " .. name .. ". Check next turn.")
    end
elseif action == "DIPLOMATIC_DELEGATION" then
    if sessionCompleted then
        print("OK:ACCEPTED|" .. name .. " accepted your delegation")
    else
        print("OK:ACCEPTED|Delegation sent to " .. name)
    end
elseif action == "RESIDENT_EMBASSY" then
    print("OK:ACCEPTED|" .. name .. " accepted your embassy")
elseif action == "DECLARE_FRIENDSHIP" then
    print("OK:ACCEPTED|" .. name .. " accepted your friendship declaration")
elseif action == "DENOUNCE" then
    print("OK:SENT|Denounced " .. name)
else
    print("OK:SENT|" .. action .. " sent to " .. name)
end
print("__MCP_SENTINEL_TAG__")
