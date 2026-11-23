-- Governor status, emitted as natural text.
--
-- This file is a TEMPLATE loaded by build_governors_query() in governance.py.
--   __MCP_INCLUDE_UNAPPOINTED__  true|false: also list governors that have
--       not been appointed yet (the get_available_governors tool); the
--       Governors section of get_full_game_state leaves them out.
--   __MCP_SENTINEL_TAG__         the response sentinel (see _helpers.SENTINEL).
--
-- Every promotion of a governor is listed so the agent can plan ahead, each
-- tagged OWNED / AVAILABLE / LOCKED. Prerequisites come from
-- GovernorPromotionPrereqs and are OR-based: a promotion is unlocked by
-- owning any ONE of its prerequisites (same rule build_promote_governor
-- enforces).
local includeUnappointed = __MCP_INCLUDE_UNAPPOINTED__
local me = Game.GetLocalPlayer()
local pGovs = Players[me]:GetGovernors()

local out = {}
local function emit(s) table.insert(out, s) end
local function lookup(key)
    if key == nil then return "" end
    -- [ICON_x] tags only duplicate the word that follows them, so drop them
    -- with their trailing space.
    return (tostring(Locale.Lookup(key)):gsub("%[NEWLINE%]", " "):gsub("\n", " "):gsub("%[ICON_[%w_]+%]%s*", ""))
end

-- promotion type -> list of prerequisite promotion types (any one suffices)
local prereqsOf = {}
for row in GameInfo.GovernorPromotionPrereqs() do
    local t = row.GovernorPromotionType
    if prereqsOf[t] == nil then prereqsOf[t] = {} end
    table.insert(prereqsOf[t], row.PrereqGovernorPromotion)
end

-- All promotions of a governor type, in tree order (level, then column).
local function promotionsOf(governorType)
    local promos = {}
    for set in GameInfo.GovernorPromotionSets() do
        if set.GovernorType == governorType then
            local row = GameInfo.GovernorPromotions[set.GovernorPromotion]
            if row then table.insert(promos, row) end
        end
    end
    table.sort(promos, function(a, b)
        local la, lb = a.Level or 0, b.Level or 0
        if la ~= lb then return la < lb end
        return (a.Column or 0) < (b.Column or 0)
    end)
    return promos
end

-- "A or B" list of a promotion's prerequisite names, "" when it has none.
local function prereqNames(promoType)
    local names = {}
    for _, reqType in ipairs(prereqsOf[promoType] or {}) do
        local reqRow = GameInfo.GovernorPromotions[reqType]
        table.insert(names, reqRow and lookup(reqRow.Name) or reqType)
    end
    return table.concat(names, " or ")
end

-- g is the appointed governor object, or nil for a governor not yet appointed.
local function emitPromotions(governorType, g)
    for _, row in ipairs(promotionsOf(governorType)) do
        local promoType = row.GovernorPromotionType
        local needs = prereqNames(promoType)
        local tag
        if g ~= nil and g:HasPromotion(row.Index) then
            tag = "OWNED"
        elseif (row.Level or 0) == 0 then
            -- The level-0 promotion is the governor's base ability: it comes
            -- with the appointment and is never bought with a title.
            tag = "BASE ABILITY (comes with appointment)"
        elseif g == nil then
            tag = (needs ~= "") and ("needs " .. needs) or "first tier"
        else
            local unlocked = (needs == "")
            for _, reqType in ipairs(prereqsOf[promoType] or {}) do
                local reqRow = GameInfo.GovernorPromotions[reqType]
                if reqRow and g:HasPromotion(reqRow.Index) then unlocked = true end
            end
            if unlocked then
                tag = "AVAILABLE"
            else
                tag = "LOCKED - needs " .. needs
            end
        end
        emit("      [" .. tag .. "] " .. lookup(row.Name) .. " (" .. promoType .. "): " .. lookup(row.Description))
    end
end

local total = pGovs:GetGovernorPoints()
local spent = pGovs:GetGovernorPointsSpent()
emit("Governor Points: " .. (total - spent) .. " available, " .. spent .. " spent")
if pGovs:CanAppoint() then emit("** Can appoint a new governor! **") end

-- Eligibility follows the game's GovernorPanel: CanEverAppointGovernor
-- excludes governors tied to another civ (e.g. the Ottoman-only Ibrahim).
local appointed, unappointed = {}, {}
for row in GameInfo.Governors() do
    if row.TransitionStrength and row.TransitionStrength > 0 then
        if pGovs:HasGovernor(row.Hash) then
            table.insert(appointed, row)
        elseif pGovs:CanEverAppointGovernor(row.Hash) then
            table.insert(unappointed, row)
        end
    end
end

if #appointed > 0 then
    emit("")
    emit("Appointed (" .. #appointed .. "):")
    for _, row in ipairs(appointed) do
        local g = pGovs:GetGovernor(row.Hash)
        local where = "Unassigned"
        local city = g:GetAssignedCity()
        if city then
            where = lookup(city:GetName())
            if g:IsEstablished() then
                where = where .. " (established)"
            else
                where = where .. " (" .. g:GetTurnsToEstablish() .. " turns to establish)"
            end
        end
        emit("  " .. lookup(row.Name) .. " (" .. row.GovernorType .. ") - " .. where)
        emit("    Promotions (AVAILABLE ones cost one governor point each):")
        emitPromotions(row.GovernorType, g)
    end
end

if includeUnappointed then
    emit("")
    if #unappointed > 0 then
        emit("Available to appoint (" .. #unappointed .. "):")
        for _, row in ipairs(unappointed) do
            emit("  " .. lookup(row.Name) .. " - " .. lookup(row.Title) .. " (" .. row.GovernorType .. ")")
            local desc = lookup(row.Description)
            if desc ~= "" then emit("    " .. desc) end
            emit("    Promotions:")
            emitPromotions(row.GovernorType, nil)
        end
    else
        emit("All governors are appointed.")
    end
elseif #unappointed > 0 then
    emit("")
    emit(#unappointed .. " governor(s) not yet appointed - see get_available_governors.")
end

emit("")
emit("Use appoint_governor(governor_type), assign_governor(governor_type, city_id), promote_governor(governor_type, promotion_type).")

print(table.concat(out, "\n"))
print("__MCP_SENTINEL_TAG__")
