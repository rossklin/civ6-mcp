-- Government & policy status, emitted as natural text.
--
-- This file is a TEMPLATE loaded by build_policies_query() in governance.py.
-- __MCP_SENTINEL_TAG__ is replaced with the response sentinel
-- (see _helpers.SENTINEL). Used both for the Policies section of
-- get_full_game_state and as the follow-up report of change_government.
--
-- Sections: current government, policy unlock cost, slots with their current
-- cards, every unlocked government (slot layout + bonuses), and every
-- unlocked policy card grouped by card type.
--
-- Card availability mirrors the game's own GovernmentScreen (Expansion2
-- IsPolicyAvailable): unlocked, not banned, not obsolete. Cards are listed
-- even when the CURRENT government has no slot for them, so the agent can
-- weigh a government change. Dark Age cards (Policies_XP1.RequiresDarkAge)
-- are listed in their own group only while the player is in a Dark Age, and
-- only the cards of that Dark Age (era window covers the current game era).
local me = Game.GetLocalPlayer()
local pCulture = Players[me]:GetCulture()

local out = {}
local function emit(s) table.insert(out, s) end
local function clean(s)
    -- [ICON_x] tags only duplicate the word that follows them
    -- ("[ICON_Production] Production"), so drop them with their trailing space.
    return (tostring(s or ""):gsub("%[NEWLINE%]", " "):gsub("\n", " "):gsub("%[ICON_[%w_]+%]%s*", ""))
end
local function lookup(key)
    if key == nil then return "" end
    return clean(Locale.Lookup(key))
end

-- GetSlotType() index -> label. 3 and 4 are both wildcard slots.
local slotLabels = {[0]="Economic", [1]="Military", [2]="Diplomatic", [3]="Wildcard", [4]="Wildcard"}
-- GameInfo GovernmentSlotType -> label. SLOT_GREAT_PERSON is the type of the
-- regular wildcard CARDS; SLOT_WILDCARD is the type of wildcard SLOTS and of
-- Dark Age cards.
local typeLabels = {
    SLOT_MILITARY="Military", SLOT_ECONOMIC="Economic", SLOT_DIPLOMATIC="Diplomatic",
    SLOT_GREAT_PERSON="Wildcard", SLOT_WILDCARD="Wildcard",
}
local groupOrder = {"Military", "Economic", "Diplomatic", "Wildcard"}

-- Current government
local govIdx = pCulture:GetCurrentGovernment()
local govEntry = (govIdx and govIdx >= 0) and GameInfo.Governments[govIdx] or nil
if govEntry then
    emit("Government: " .. lookup(govEntry.Name) .. " (" .. govEntry.GovernmentType .. ")")
else
    emit("Government: None")
end

-- Policy unlock cost: 0 = free to change this turn (civic just completed or
-- already unlocked); >0 = locked, set_policies will charge this much.
local unlockCost = 0
pcall(function() unlockCost = pCulture:GetCostToUnlockPolicies() or 0 end)
if unlockCost <= 0 then
    emit("Policies: free to change this turn.")
else
    local byFaith = (GlobalParameters.GOVERNMENT_UNLOCK_WITH_FAITH ~= 0)
    local currency = byFaith and "faith" or "gold"
    local balance = 0
    if byFaith then
        pcall(function() balance = Players[me]:GetReligion():GetFaithBalance() or 0 end)
    else
        pcall(function() balance = Players[me]:GetTreasury():GetGoldBalance() or 0 end)
    end
    if balance >= unlockCost then
        emit("Policies LOCKED - set_policies will cost " .. unlockCost .. " " .. currency .. " to unlock.")
    else
        emit("Policies LOCKED - set_policies costs " .. unlockCost .. " " .. currency .. " (cannot afford yet).")
    end
end

-- Slots of the current government
local numSlots = pCulture:GetNumPolicySlots()
local slotOfPolicy = {}   -- policy index -> slot index it is currently in
local haveSlotLabel = {}  -- label -> true when the current government has such a slot
local emptySlots = 0
emit("")
emit(numSlots .. " policy slots:")
for s = 0, numSlots - 1 do
    local label = slotLabels[pCulture:GetSlotType(s)] or ("Type" .. tostring(pCulture:GetSlotType(s)))
    haveSlotLabel[label] = true
    local policyIdx = pCulture:GetSlotPolicy(s)
    local pe = (policyIdx and policyIdx >= 0) and GameInfo.Policies[policyIdx] or nil
    if pe then
        slotOfPolicy[policyIdx] = s
        emit("  Slot " .. s .. " (" .. label .. "): " .. lookup(pe.Name) .. " (" .. pe.PolicyType .. ")")
    else
        emptySlots = emptySlots + 1
        emit("  Slot " .. s .. " (" .. label .. "): EMPTY")
    end
end
local function finish()
    print(table.concat(out, "\n"))
    print("__MCP_SENTINEL_TAG__")
end
if emptySlots > 0 then
    emit("!! " .. emptySlots .. " slot(s) EMPTY - no card is in effect until every slot is filled: call set_policies with an assignment for ALL " .. numSlots .. " slots.")
end

-- Slots-only mode (the flag below is filled in by build_policies_query):
-- the report appended to a change_government result stops here. The agent
-- already has the card list from the game state, and a command result has
-- to stay short.
if __MCP_SLOTS_ONLY__ then
    finish()
    return
end

-- Unlocked governments
local function slotLayout(govType)
    local counts = {}
    for row in GameInfo.Government_SlotCounts() do
        if row.GovernmentType == govType then
            local label = typeLabels[row.GovernmentSlotType] or row.GovernmentSlotType
            counts[label] = (counts[label] or 0) + row.NumSlots
        end
    end
    local parts = {}
    for _, label in ipairs(groupOrder) do
        if counts[label] then table.insert(parts, counts[label] .. " " .. label) end
    end
    return table.concat(parts, ", ")
end
emit("")
emit("Unlocked governments (switch with change_government; returning to a government you have used before causes anarchy):")
for row in GameInfo.Governments() do
    if pCulture:IsGovernmentUnlocked(row.Index) then
        local tag = (row.Index == govIdx) and " [CURRENT]" or ""
        local favor = ""
        if GameInfo.Governments_XP2 then
            for x in GameInfo.Governments_XP2() do
                if x.GovernmentType == row.GovernmentType then
                    favor = " | +" .. tostring(x.Favor) .. " favor/turn"
                    break
                end
            end
        end
        emit("  " .. lookup(row.Name) .. " (" .. row.GovernmentType .. ")" .. tag .. " - slots: " .. slotLayout(row.GovernmentType) .. favor)
        local inherent = lookup(row.InherentBonusDesc)
        local legacy = lookup(row.AccumulatedBonusDesc)
        if inherent ~= "" then emit("    Bonus: " .. inherent) end
        if legacy ~= "" and legacy ~= inherent then emit("    Legacy bonus: " .. legacy) end
    end
end

-- Unlocked policy cards, grouped by card type
local groups = {Military={}, Economic={}, Diplomatic={}, Wildcard={}}
local darkCards = {}
local hasXP1 = (GameInfo.Policies_XP1 ~= nil)
local curEra = 0
pcall(function() curEra = Game.GetEras():GetCurrentEra() end)
local inDark = false
pcall(function() inDark = Game.GetEras():HasDarkAge(me) end)
for policy in GameInfo.Policies() do
    local idx = policy.Index
    local banned = false
    pcall(function() banned = pCulture:IsPolicyBanned(idx) end)
    if pCulture:IsPolicyUnlocked(idx) and not pCulture:IsPolicyObsolete(idx) and not banned then
        local xp1 = hasXP1 and GameInfo.Policies_XP1[policy.PolicyType] or nil
        local line = lookup(policy.Name) .. " (" .. policy.PolicyType .. ")"
        if slotOfPolicy[idx] ~= nil then line = line .. " [in slot " .. slotOfPolicy[idx] .. "]" end
        if xp1 and xp1.RequiresDarkAge then
            -- Listed only during a Dark Age, and only the cards of this
            -- Dark Age (era window covers the current game era). The era
            -- window is checked from the data rather than relying on
            -- CanPolicyBeSlotted alone, so the cards still show when the
            -- current government has no Wildcard slot for them.
            if inDark then
                local minEra = GameInfo.Eras[xp1.MinimumGameEra]
                local maxEra = GameInfo.Eras[xp1.MaximumGameEra]
                local inWindow = (minEra == nil or minEra.Index <= curEra)
                    and (maxEra == nil or maxEra.Index >= curEra)
                local usable = false
                pcall(function() usable = pCulture:CanPolicyBeSlotted(idx) end)
                if usable or inWindow then
                    table.insert(darkCards, line .. ": " .. lookup(policy.Description))
                end
            end
        else
            local label = typeLabels[policy.GovernmentSlotType] or "Wildcard"
            table.insert(groups[label], line .. ": " .. lookup(policy.Description))
        end
    end
end
emit("")
emit("Unlocked policy cards (a card goes in a slot of its own type or in a Wildcard slot):")
for _, label in ipairs(groupOrder) do
    local cards = groups[label]
    if #cards > 0 then
        local note = ""
        if label == "Wildcard" then
            if not haveSlotLabel["Wildcard"] then note = " - current government has no Wildcard slot" end
        elseif not haveSlotLabel[label] then
            if haveSlotLabel["Wildcard"] then
                note = " - current government has no " .. label .. " slot, only a Wildcard slot can take these"
            else
                note = " - current government has no slot for these"
            end
        end
        emit("  " .. label .. note .. ":")
        for _, c in ipairs(cards) do emit("    " .. c) end
    end
end
if #darkCards > 0 then
    local note = ""
    if not haveSlotLabel["Wildcard"] then note = " - current government has no Wildcard slot" end
    emit("  Dark Age (available during this Dark Age, Wildcard slot only)" .. note .. ":")
    for _, c in ipairs(darkCards) do emit("    " .. c) end
end

emit("")
emit("Use set_policies to fill ALL slots at once, keyed by slot index, e.g. {\"action\": \"set_policies\", \"params\": {\"assignments\": {\"0\": \"POLICY_AGOGE\", \"1\": \"POLICY_URBAN_PLANNING\"}}}")

finish()
