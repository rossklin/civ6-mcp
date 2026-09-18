-- Set policy cards: fill EVERY government slot in one atomic change.
--
-- This file is a TEMPLATE loaded by build_set_policies() in governance.py,
-- which injects the {slot, PolicyType} assignment pairs and the response
-- sentinel. A government is only valid with all slots filled, so the
-- assignment must cover every slot; all slots are cleared and refilled in
-- a single RequestPolicyChanges batch. Partial assignments are refused.
--
-- Pre-checks run BEFORE UNLOCK_POLICIES because CanSlotPolicy is reliable
-- there (it goes stale same-frame after the unlock in the same Lua string).
-- The engine's CanSlotPolicy first checks !IsPolicyActive (see the game's
-- own GovernmentScreen.lua), so it rejects ANY policy that is currently
-- slotted, whatever the target slot. Since this batch clears every slot, a
-- move of an already-active card is legal, so a slot's assignment is valid
-- when CanSlotPolicy accepts it OR the policy is currently active in any
-- slot.

local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()
local numSlots = pCulture:GetNumPolicySlots()
if numSlots <= 0 then
    print("ERR:NO_GOVERNMENT|No government selected")
    print("__MCP_SENTINEL_TAG__")
    return
end
local slotNames = {[0]="Economic", [1]="Military", [2]="Diplomatic", [3]="Wildcard", [4]="Wildcard"}
local slotTypeMap = {SLOT_ECONOMIC=0, SLOT_MILITARY=1, SLOT_DIPLOMATIC=2, SLOT_WILDCARD=3, SLOT_GREAT_PERSON=4}

local assignments = {
    __MCP_ASSIGNMENTS__
}

-- Structural check: every government slot must be assigned. Duplicate
-- indices are impossible (the builder injects unique dict keys).
local assigned = {}
for _, a in ipairs(assignments) do
    local k = a[1]
    if k < 0 or k >= numSlots then
        print("ERR:SLOT_OUT_OF_RANGE|slot " .. k .. " - this government has " .. numSlots .. " slots (0-" .. (numSlots - 1) .. ")")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    assigned[k] = true
end
for k = 0, numSlots - 1 do
    if not assigned[k] then
        print("ERR:MISSING_SLOT|slot " .. k .. " (" .. (slotNames[pCulture:GetSlotType(k)] or "unknown") .. ") - assignments must fill ALL " .. numSlots .. " slots")
        print("__MCP_SENTINEL_TAG__")
        return
    end
end

-- Per-slot pre-checks (before UNLOCK_POLICIES - see header).
for _, a in ipairs(assignments) do
    local slot = a[1]
    local policyType = a[2]
    local pe = GameInfo.Policies[policyType]
    if pe == nil then
        print("ERR:POLICY_NOT_FOUND|" .. policyType)
        print("__MCP_SENTINEL_TAG__")
        return
    end
    -- Static type gate: catches e.g. a military card in the economic slot
    -- before the engine silently no-ops the change.
    local sType = pCulture:GetSlotType(slot)
    local pSlot = slotTypeMap[pe.GovernmentSlotType] or -1
    if sType < 3 and pSlot ~= sType and pe.GovernmentSlotType ~= "SLOT_WILDCARD" then
        print("ERR:SLOT_MISMATCH|" .. policyType .. " (" .. pe.GovernmentSlotType .. ") cannot go in slot " .. slot .. " (" .. slotNames[sType] .. ")")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    -- Engine gate, rescued for cards currently active in ANY slot: this
    -- batch clears every slot, so an active card is a legal assignment (it
    -- moves to its new slot at commit time).
    if not pCulture:CanSlotPolicy(pe.Index, slot) then
        local active = false
        for k = 0, numSlots - 1 do
            if pCulture:GetSlotPolicy(k) == pe.Index then active = true; break end
        end
        if not active then
            print("ERR:CANNOT_SLOT|" .. policyType .. " (" .. (pe.GovernmentSlotType or "unknown") .. ") rejected for slot " .. slot .. " (" .. (slotNames[sType] or "unknown") .. ") - not slottable and not currently active in any slot")
            print("__MCP_SENTINEL_TAG__")
            return
        end
    end
end

UI.RequestPlayerOperation(me, PlayerOperations.UNLOCK_POLICIES, {})
local clearList = {}
for k = 0, numSlots - 1 do table.insert(clearList, k) end
local addList = {}
for _, a in ipairs(assignments) do
    addList[a[1]] = GameInfo.Policies[a[2]].Hash
end
pCulture:RequestPolicyChanges(clearList, addList)
print("OK:POLICIES_SET|Policies updated. Use get_policies to verify.")
print("__MCP_SENTINEL_TAG__")
