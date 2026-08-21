-- Diplo shim: wraps DiplomacyManager.RequestSession and AddResponse in the
-- DiplomacyActionView state (the 3D leader screen) for managed player IDs.
--
-- This file is a TEMPLATE loaded by build_diplo_shim_install_lua() in
-- handoff.py. Two tags are substituted before the Lua is sent to the game:
--   __MCP_MANAGED_IDS_TAG__  -> {[p]=true,[q]=true,...} for the managed IDs
--   __MCP_SENTINEL_TAG__     -> the response sentinel (see _helpers.SENTINEL)
--
-- Sister of deal_shim.lua (DiplomacyDealView state). Same idempotent re-arm
-- pattern: each wrapped function's original is captured once (when
-- __MCP_orig_* is nil) and called through a LOCAL upvalue, so repeated
-- installs never stack wrappers and can never self-recurse.
--
-- Why RequestSession: the human's Declare Friendship / Send Delegation /
-- Establish Embassy buttons (overview mode) all funnel through
-- DiplomacyManager.RequestSession(local, target, "DECLARE_FRIEND" |
-- "DIPLOMATIC_DELEGATION" | "RESIDENT_EMBASSY") (DiplomacyActionView.lua
-- ~line 470). Wrapping the engine call catches the proposal BEFORE any
-- session exists — once a session opens, the target's built-in AI answers
-- inside the engine with no Lua-visible moment to intercept.
--
-- AddResponse wrapper: when Python presents a mailbox proposal to the human
-- (synthetic AI-initiated session opened from the gamecore state), it sets
-- __MCP_diplo_proposal_id here first. The human's Accept/Decline click calls
-- AddResponse(sid, localPlayer, "POSITIVE"/"NEGATIVE"/...); we report the
-- answer as MCPDIPLO_RESPONDED and call through — the engine applies the
-- effect from that single response (Initiator=AI sessions complete on the
-- human's reply alone).
--
-- Context isolation note: each Lua context has its own DiplomacyManager
-- binding, so this wrapper only sees calls made FROM the
-- DiplomacyActionView state (the human's clicks). Our own engine calls
-- (session opens at click time, drain-time execution) run in the gamecore
-- state and hit the pristine binding there — no bypass flag needed.
--
-- Fallback: if DiplomacyManager is read-only in this state (like Network is
-- in ChatPanel), the install wraps the UI globals
-- OnSelectInitialDiplomacyStatement / OnSelectConversationDiplomacyStatement
-- instead. The status line reports which hook is active.

-- Per-player lookup table for the managed check (idempotent overwrite).
__MCP_managed_ids = __MCP_MANAGED_IDS_TAG__

-- Install counter: persists across re-arms within one Lua state, resets on
-- save load (same diagnostics as the deal shim).
__MCP_shim_install_count = (__MCP_shim_install_count or 0) + 1

-- The three response-able proposal session strings. All other statement
-- types (wars, MAKE_DEAL, denounce...) pass straight through.
local MCP_PROPOSAL_TYPES = {
    DECLARE_FRIEND = true,
    DIPLOMATIC_DELEGATION = true,
    RESIDENT_EMBASSY = true,
}

-- Close the leader screen after a suppressed proposal: the click has been
-- "handled" (filed to the mailbox), and the screen would otherwise sit in
-- overview mode waiting for a statement result that never arrives. Prefer
-- the view's own Close(); fall back to the raw hide events (same idiom as
-- build_diplomacy_respond's EXIT path) in case Close is not global here.
local function mcp_close_leader_screen()
    if not pcall(Close) then
        pcall(function() LuaEvents.DiplomacyActionView_ShowIngameUI() end)
        pcall(function() Events.HideLeaderScreen() end)
    end
end

local MCP_HOOK = "none"

-- Primary hook: wrap the engine table members --------------------------------
local okEngine = pcall(function()
    if __MCP_orig_RS == nil then
        __MCP_orig_RS = DiplomacyManager.RequestSession
    end
    local origRS = __MCP_orig_RS
    DiplomacyManager.RequestSession = function(fromP, toP, typeStr)
        if __MCP_managed_ids[toP] and typeStr ~= nil
            and MCP_PROPOSAL_TYPES[typeStr] then
            print("MCPDIPLO|PROPOSED"
                .. "|from=" .. tostring(fromP)
                .. "|to=" .. tostring(toP)
                .. "|action=" .. tostring(typeStr))
            mcp_close_leader_screen()
            return
        end
        return origRS(fromP, toP, typeStr)
    end

    if __MCP_orig_AR == nil then
        __MCP_orig_AR = DiplomacyManager.AddResponse
    end
    local origAR = __MCP_orig_AR
    DiplomacyManager.AddResponse = function(sid, fromP, resp)
        if __MCP_diplo_proposal_id ~= nil then
            -- Only report when the session actually involves a managed
            -- player, so answering an unrelated genuine-AI session while
            -- a mailbox proposal is presented does not consume the flag.
            local involvesManaged = false
            pcall(function()
                local info = DiplomacyManager.GetSessionInfo(sid)
                if info and (__MCP_managed_ids[info.FromPlayer]
                    or __MCP_managed_ids[info.ToPlayer]) then
                    involvesManaged = true
                end
            end)
            if involvesManaged then
                print("MCPDIPLO_RESPONDED|"
                    .. tostring(__MCP_diplo_proposal_id)
                    .. "|" .. tostring(resp))
                __MCP_diplo_proposal_id = nil
            end
        end
        return origAR(sid, fromP, resp)
    end
end)

if okEngine then
    MCP_HOOK = "engine"
else
    -- Fallback hook: wrap the UI globals -------------------------------------
    -- CHOICE_ key -> session string (DiplomacySelections.Key ->
    -- DiplomacyStatementTypes.Type, e.g. CHOICE_DECLARE_FRIENDSHIP ->
    -- "DECLARE_FRIEND").
    local MCP_CHOICE_TO_ACTION = {
        CHOICE_DECLARE_FRIENDSHIP = "DECLARE_FRIEND",
        CHOICE_DIPLOMATIC_DELEGATION = "DIPLOMATIC_DELEGATION",
        CHOICE_RESIDENT_EMBASSY = "RESIDENT_EMBASSY",
    }
    -- CHOICE_ key -> response string (OnSelectConversationDiplomacyStatement
    -- lines ~530-562).
    local MCP_CHOICE_TO_RESPONSE = {
        CHOICE_POSITIVE = "POSITIVE",
        CHOICE_NEGATIVE = "NEGATIVE",
    }

    if __MCP_orig_OIDS == nil then
        __MCP_orig_OIDS = OnSelectInitialDiplomacyStatement
    end
    local origOIDS = __MCP_orig_OIDS
    OnSelectInitialDiplomacyStatement = function(key)
        -- ms_SelectedPlayerID is a file-scope global in this state (set in
        -- SetupPlayers). If it is not visible here the guard fails open:
        -- the call passes through as vanilla.
        local target = ms_SelectedPlayerID
        local action = MCP_CHOICE_TO_ACTION[key]
        if target ~= nil and __MCP_managed_ids[target] and action ~= nil then
            print("MCPDIPLO|PROPOSED"
                .. "|from=" .. tostring(Game.GetLocalPlayer())
                .. "|to=" .. tostring(target)
                .. "|action=" .. action)
            mcp_close_leader_screen()
            return
        end
        return origOIDS(key)
    end

    if __MCP_orig_OCDR == nil then
        __MCP_orig_OCDR = OnSelectConversationDiplomacyStatement
    end
    local origOCDR = __MCP_orig_OCDR
    OnSelectConversationDiplomacyStatement = function(key)
        if __MCP_diplo_proposal_id ~= nil then
            local resp = MCP_CHOICE_TO_RESPONSE[key]
            if resp ~= nil then
                print("MCPDIPLO_RESPONDED|"
                    .. tostring(__MCP_diplo_proposal_id)
                    .. "|" .. resp)
                __MCP_diplo_proposal_id = nil
            end
        end
        return origOCDR(key)
    end
    MCP_HOOK = "uiglobal"
end

print("DIPLOSHIM|installed|hook=" .. MCP_HOOK
    .. "|install_count=" .. tostring(__MCP_shim_install_count))
print("__MCP_SENTINEL_TAG__")
