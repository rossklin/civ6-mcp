-- Deal shim: wraps SendWorkingDeal, IsAutoPropose and UpdateDealStatus in
-- the DiplomacyDealView state for managed player IDs.
--
-- This file is a TEMPLATE loaded by build_deal_shim_install_lua() in
-- handoff.py.  Two tags are substituted before the Lua is sent to the game:
--   __MCP_MANAGED_IDS_TAG__  -> {[p]=true,[q]=true,...} for the managed IDs
--   __MCP_SENTINEL_TAG__     -> the response sentinel (see _helpers.SENTINEL)
--
-- Idempotent re-arm: each wrapped function's original is captured once
-- (when __MCP_orig_* is nil) and called through a LOCAL upvalue, so repeated
-- installs never stack wrappers and can never self-recurse.

-- Per-player lookup table for the managed check (idempotent overwrite).
__MCP_managed_ids = __MCP_MANAGED_IDS_TAG__
__MCP_managed_deal = false

-- Install counter: persists across re-arms within one Lua state, resets on
-- save load. Lets us read re-arm frequency off the log and confirm re-arms
-- no longer stack (each re-arm prints a new count and never crashes).
__MCP_shim_install_count = (__MCP_shim_install_count or 0) + 1

-- IsAutoPropose override ---------------------------------------------------
-- Always return false. With auto-propose off the human always sees a Propose
-- button and must click it explicitly, giving us a clean PROPOSED to
-- intercept. The guard keeps __MCP_orig_IAP pointing at the real function so
-- uninstall/health-check restore the right thing.
if __MCP_orig_IAP == nil then
    __MCP_orig_IAP = IsAutoPropose
end
IsAutoPropose = function()
    return false
end

-- UpdateDealStatus wrapper -------------------------------------------------
-- After the game's UpdateProposalButtons runs and hides Accept (because
-- ms_OtherPlayerIsHuman is false), force the buttons back for managed
-- targets. Find the other player by scanning for the open diplomacy session.
if __MCP_orig_UDS == nil then
    __MCP_orig_UDS = UpdateDealStatus
end
local origUDS = __MCP_orig_UDS
UpdateDealStatus = function()
    origUDS()

    print("MCP_TRACE|UDS|proposal_id=" .. tostring(__MCP_deal_proposal_id or "nil"))

    local me = Game.GetLocalPlayer()
    local other = -1
    for i = 0, 62 do
        if i ~= me then
            local sid = DiplomacyManager.FindOpenSessionID(me, i)
            if sid and sid >= 0 then
                other = i; break
            end
        end
    end

    if other >= 0 and __MCP_managed_ids[other] then
        Controls.AcceptDeal:SetHide(false)
        Controls.AcceptDeal:LocalizeAndSetText("LOC_DIPLOMACY_DEAL_ACCEPT_DEAL")
        Controls.RefuseDeal:SetHide(false)
        Controls.RefuseDeal:LocalizeAndSetText("LOC_DIPLOMACY_DEAL_REFUSE_DEAL")
        Controls.EqualizeDeal:SetHide(true)

        local cfg = PlayerConfigurations[other]
        local name = cfg and Locale.Lookup(cfg:GetLeaderName()) or ("P" .. tostring(other))
        Controls.LeaderDialog:SetText(name .. ' proposes the following deal:')
    end
end

-- SendWorkingDeal wrapper --------------------------------------------------
-- Same guard + local-upvalue pattern: the guard keeps the global pointing at
-- the real function; the local upvalue (origSWD) makes self-recursion
-- impossible even if the guard is removed again.
if __MCP_orig_SWD == nil then
    __MCP_orig_SWD = DealManager.SendWorkingDeal
end
local origSWD = __MCP_orig_SWD
DealManager.SendWorkingDeal = function(action, fromP, toP)
    -- Diagnostic: log every call to trace what fires.
    print("MCP_TRACE|SWD|action=" .. tostring(action)
        .. "|fromP=" .. tostring(fromP)
        .. "|toP=" .. tostring(toP)
        .. "|proposal_id=" .. tostring(__MCP_deal_proposal_id or "nil"))

    -- Only intercept for managed targets.
    if __MCP_managed_ids[toP] or __MCP_managed_ids[fromP] then
        -- INSPECT (7): suppress; we don't want the AI evaluating the deal.
        if action == 7 then
            print("MCPDEAL|INSPECT|suppressed|from=" .. tostring(fromP) .. "|to=" .. tostring(toP))
            return
        end

        -- PROPOSED (4): if __MCP_deal_proposal_id is set, this is the human
        -- accepting a mailbox deal (print HUMAN_ACCEPTED); otherwise it's a
        -- new proposal from the human (serialise and mailbox it).
        if action == 4 then
            if __MCP_deal_proposal_id ~= nil then
                print("MCPDEAL|HUMAN_ACCEPTED|" .. tostring(__MCP_deal_proposal_id)
                    .. "|from=" .. tostring(fromP)
                    .. "|to=" .. tostring(toP))
                __MCP_deal_proposal_id = nil
                return
            end

            print("MCPDEAL|PROPOSED|from=" .. tostring(fromP) .. "|to=" .. tostring(toP))

            local pDeal = DealManager.GetWorkingDeal(0, fromP, toP)
            if pDeal then
                for item in pDeal:Items() do
                    local iType = item:GetType()
                    local typeName = "UNKNOWN"
                    if iType == DealItemTypes.GOLD then
                        typeName = "GOLD"
                    elseif iType == DealItemTypes.RESOURCES then
                        typeName = "RESOURCE"
                    elseif iType == DealItemTypes.AGREEMENTS then
                        typeName = "AGREEMENT"
                    elseif iType == DealItemTypes.FAVOR then
                        typeName = "FAVOR"
                    elseif iType == DealItemTypes.CITIES then
                        typeName = "CITY"
                    elseif iType == DealItemTypes.GREATWORK then
                        typeName = "GREAT_WORK"
                    end

                    print("MCPDEAL_ITEM|" .. typeName
                        .. "|from=" .. tostring(item:GetFromPlayerID())
                        .. "|amount=" .. tostring(item:GetAmount())
                        .. "|duration=" .. tostring(item:GetDuration())
                        .. "|value=" .. tostring(item:GetValueType() or -1)
                        .. "|sub=" .. tostring(item:GetSubType() or -1))
                end
            end

            print("MCPDEAL_END")
            return
        end
        -- ACCEPTED (1) / REJECTED (2): allow through (forced-deal execution).
    end

    return origSWD(action, fromP, toP)
end

print("DEALSHIM|installed|install_count=" .. tostring(__MCP_shim_install_count))
print("__MCP_SENTINEL_TAG__")
