-- Government screen repair: refreshes GovernmentScreen's cached local-player
-- state after a turn-boundary handoff.
--
-- This file is a TEMPLATE loaded by build_government_screen_fix_lua() in
-- handoff.py. One tag is substituted before the Lua is sent to the game:
--   __MCP_SENTINEL_TAG__  -> the response sentinel (see _helpers.SENTINEL)
--
-- Background (see docs/human-vs-agent.md, "The diplomacy screen dies"):
-- The handoff calls PlayerManager.SetLocalPlayerAndObserver(pid), which raises
-- Events.LocalPlayerChanged immediately before the new local player's turn
-- activates. When that happens the engine SUPPRESSES
-- Events.LocalPlayerTurnBegin for that activation. GovernmentScreen.lua only
-- updates its cached local player (m_ePlayer) and its m_isLocalPlayerTurn flag
-- from OnLocalPlayerTurnBegin / OnLocalPlayerTurnEnd, so after a handoff both
-- go stale: the screen shows the PREVIOUS civ's government, and the policy
-- slots are non-interactable because IsAbleToChangeGovernment /
-- IsAbleToChangePolicies gate on m_isLocalPlayerTurn. set_policies still works
-- over MCP because it bypasses the screen entirely; only the UI is wrong.
--
-- The fix delivers the swallowed event by calling the screen's own
-- OnLocalPlayerTurnBegin once the local player's turn really is active.
--
-- Unlike DiplomacyActionView's g_bIsLocalPlayerTurn (a global), GovernmentScreen's
-- m_isLocalPlayerTurn and m_ePlayer are file-LOCAL upvalues, so they cannot be
-- read from injected tuner code to guard the repair. We instead track the last
-- local player we repaired in our own global and re-fire whenever it changes,
-- resetting that tracker whenever the turn goes inactive so the same civ's next
-- activation always repairs (m_isLocalPlayerTurn is cleared by
-- OnLocalPlayerTurnEnd between turns).

if OnLocalPlayerTurnBegin == nil then
    -- Not the GovernmentScreen context, or the handler is not defined here.
    print("GOVFIX|absent")
    print("__MCP_SENTINEL_TAG__")
    return
end

if __civmcp_gov_fix == nil then
    __civmcp_gov_last = -1
    __civmcp_gov_fix = function()
        local pid = Game.GetLocalPlayer()
        if pid == nil or pid < 0 then
            return
        end
        local p = Players[pid]
        if p == nil then
            return
        end
        -- Off the clock: clear the tracker so the next activation always
        -- repairs. The genuine OnLocalPlayerTurnEnd has just cleared the flag,
        -- and we cannot read that flag ourselves to know.
        if not p:IsTurnActive() then
            __civmcp_gov_last = -1
            return
        end
        -- On the clock for a player we have not repaired yet this activation:
        -- deliver the swallowed OnLocalPlayerTurnBegin. Calling the screen's
        -- own handler (rather than poking the upvalues, which are invisible to
        -- us) keeps everything else that handler is responsible for correct.
        if pid ~= __civmcp_gov_last then
            __civmcp_gov_last = pid
            pcall(OnLocalPlayerTurnBegin)
        end
    end
    -- Both events are load-bearing and not symmetric (measured for the diplomacy
    -- screen): LocalPlayerChanged fires BEFORE activation, so IsTurnActive() is
    -- false there and we only reset the tracker; PlayerTurnActivated is what
    -- actually lands the repair. Registering both is necessary because their
    -- order is not guaranteed, and the guard makes the redundant one free.
    Events.LocalPlayerChanged.Add(__civmcp_gov_fix)
    Events.PlayerTurnActivated.Add(__civmcp_gov_fix)
    __civmcp_gov_fix()
    print("GOVFIX|installed")
else
    __civmcp_gov_fix()
    print("GOVFIX|present")
end

print("__MCP_SENTINEL_TAG__")
