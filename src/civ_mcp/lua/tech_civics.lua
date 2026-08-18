-- Tech & civics status, emitted as natural text (same format that
-- narrate_tech_civics used to produce from the pipe-delimited roundtrip).
local id = Game.GetLocalPlayer()
local te = Players[id]:GetTechs()
local cu = Players[id]:GetCulture()

-- Output buffer: each entry is one rendered line. Joined with "\n" at the end.
local out = {}
local function emit(s) table.insert(out, s) end

-- Current research / civic
local techIdx = te:GetResearchingTech()
local civicIdx = cu:GetProgressingCivic()
local techName = "None"
local techTurns = -1
if techIdx >= 0 then
    techName = Locale.Lookup(GameInfo.Technologies[techIdx].Name)
    techTurns = te:GetTurnsToResearch(techIdx)
end
local civicName = "None"
local civicTurns = -1
if civicIdx >= 0 then
    civicName = Locale.Lookup(GameInfo.Civics[civicIdx].Name)
    civicTurns = cu:GetTurnsLeft()
end

-- Completed counts (header shows these)
local completedTechs = 0
for tech in GameInfo.Technologies() do
    if te:HasTech(tech.Index) then completedTechs = completedTechs + 1 end
end
local completedCivics = 0
for civic in GameInfo.Civics() do
    if cu:HasCivic(civic.Index) then completedCivics = completedCivics + 1 end
end
local completed = ""
if completedTechs > 0 or completedCivics > 0 then
    completed = " | Completed: " .. completedTechs .. " techs, " .. completedCivics .. " civics"
end
if techName ~= "None" then
    emit("Researching: " .. techName .. " (" .. techTurns .. " turns)" .. completed)
else
    emit("No technology being researched!" .. completed)
end
if civicName ~= "None" then
    emit("Civic: " .. civicName .. " (" .. civicTurns .. " turns)")
else
    emit("No civic being progressed!")
end

-- Boost lookup
local boostsByTech = {}
local boostsByCivic = {}
for b in GameInfo.Boosts() do
    if b.TechnologyType then boostsByTech[b.TechnologyType] = b end
    if b.CivicType then boostsByCivic[b.CivicType] = b end
end

-- Tech/civic "abilities" and full unlock lists, matching what the tech/civic
-- tree UI shows as icons. Three sources, all read straight from GameInfo:
--   1. The row's Description field — the authored ability prose rendered as the
--      ICON_TECHUNLOCK_13 icon (e.g. "Allows Builders to embark.",
--      "+1 Movement for all naval units.", "Allows clearing of Marsh, and
--      harvesting of Bananas."). Covers the vast majority of ability text and
--      supersedes hand-rolling clear/harvest from Features/Resource_Harvests.
--   2. Unlockable items — units, buildings, districts, improvements, projects,
--      resources (PrereqTech/PrereqCivic), plus governments and policies
--      (PrereqCivic, civic-only) and diplomatic actions (InitiatorPrereqTech/
--      InitiatorPrereqCivic, e.g. Joint War, Alliance, Open Borders).
--   3. Envoy awards — CivicModifiers with MODIFIER_PLAYER_GRANT_INFLUENCE_TOKEN,
--      the one modifier type the civic tree renders as its own numbered icon.
-- Description text carries [ICON_*] / [NEWLINE] tags; sanitizeText strips them.
local function sanitizeText(s)
    if not s then return "" end
    s = Locale.Lookup(s)
    s = s:gsub("%[ICON_[^%]]*%]", "")
    s = s:gsub("%[NEWLINE%]", " ")
    s = s:gsub("%s+", " ")
    s = s:gsub("^%s+", ""):gsub("%s+$", "")
    s = s:gsub("|", "/")
    return s
end
local function descStrFor(row)
    if not row.Description then return "" end
    local d = sanitizeText(row.Description)
    if d == "" then return "" end
    return " [Abilities: " .. d .. "]"
end
-- Envoy awards per civic (civicType -> count).
local envoysByCivic = {}
for cm in GameInfo.CivicModifiers() do
    local m = GameInfo.Modifiers[cm.ModifierId]
    if m and m.ModifierType == "MODIFIER_PLAYER_GRANT_INFLUENCE_TOKEN" then
        for ma in GameInfo.ModifierArguments() do
            if ma.ModifierId == cm.ModifierId then
                local v = tonumber(ma.Value)
                if v then envoysByCivic[cm.CivicType] = (envoysByCivic[cm.CivicType] or 0) + v end
            end
        end
    end
end
-- Build the " -> item, item, ..." unlock string for a tech or civic.
-- prereqField/initiatorField select PrereqTech/PrereqCivic vs the diplomatic-
-- action InitiatorPrereqTech/InitiatorPrereqCivic columns; isCivic also pulls
-- governments + policies (civic-locked) and envoy awards.
local function buildUnlocks(prereqField, initiatorField, typeStr, isCivic)
    local unlocks = {}
    for u in GameInfo.Units() do if u[prereqField] == typeStr then table.insert(unlocks, Locale.Lookup(u.Name)) end end
    for bld in GameInfo.Buildings() do if bld[prereqField] == typeStr then table.insert(unlocks, Locale.Lookup(bld.Name)) end end
    for d in GameInfo.Districts() do if d[prereqField] == typeStr then table.insert(unlocks, Locale.Lookup(d.Name)) end end
    for imp in GameInfo.Improvements() do if imp[prereqField] == typeStr then table.insert(unlocks, Locale.Lookup(imp.Name)) end end
    for r in GameInfo.Resources() do if r[prereqField] == typeStr then table.insert(unlocks, "Reveals " .. Locale.Lookup(r.Name)) end end
    pcall(function()
        for proj in GameInfo.Projects() do
            if proj[prereqField] == typeStr then table.insert(unlocks, "Project: " .. Locale.Lookup(proj.Name)) end
        end
    end)
    if isCivic then
        for g in GameInfo.Governments() do if g.PrereqCivic == typeStr then table.insert(unlocks, "Gov: " .. Locale.Lookup(g.Name)) end end
        for p in GameInfo.Policies() do if p.PrereqCivic == typeStr then table.insert(unlocks, "Policy: " .. Locale.Lookup(p.Name)) end end
    end
    for da in GameInfo.DiplomaticActions() do
        if da[initiatorField] == typeStr and da.Name ~= nil then table.insert(unlocks, Locale.Lookup(da.Name)) end
    end
    if isCivic and envoysByCivic[typeStr] then
        local n = envoysByCivic[typeStr]
        table.insert(unlocks, "Awards " .. n .. " Envoy" .. (n > 1 and "s" or ""))
    end
    if #unlocks == 0 then return "" end
    return " -> " .. table.concat(unlocks, ", "):gsub("|", "/")
end

-- Tech prereqs lookup (all prereqs of each tech — type strings)
local techPrereqs = {}
pcall(function()
    for row in GameInfo.TechnologyPrereqs() do
        if not techPrereqs[row.Technology] then techPrereqs[row.Technology] = {} end
        table.insert(techPrereqs[row.Technology], row.PrereqTech)
    end
end)

-- Civic prereqs lookup (used both for available "needs:" and locked detection)
local civicPrereqs = {}
for row in GameInfo.CivicPrereqs() do
    if not civicPrereqs[row.Civic] then civicPrereqs[row.Civic] = {} end
    table.insert(civicPrereqs[row.Civic], row.PrereqCivic)
end

-- Era index from GameInfo (matches GetCurrentEra()) — used for the
-- "within curEra + 2" gating so far-future items are skipped.
local eraIndex = {}
for e in GameInfo.Eras() do eraIndex[e.EraType] = e.Index end
local curEra = Game.GetEras():GetCurrentEra()

-- Display ordering for locked groupings (matches narrate's era_order).
local eraOrder = {
    "ERA_ANCIENT", "ERA_CLASSICAL", "ERA_MEDIEVAL", "ERA_RENAISSANCE",
    "ERA_INDUSTRIAL", "ERA_MODERN", "ERA_ATOMIC", "ERA_INFORMATION", "ERA_FUTURE",
}
local eraRank = {}
for i, e in ipairs(eraOrder) do eraRank[e] = i end
local function eraLabel(eraType)
    if not eraType then return "UNKNOWN" end
    return (eraType:gsub("ERA_", ""))
end

-- Available techs (researchable, not yet completed)
local techOptions = {}
for tech in GameInfo.Technologies() do
    if te:CanResearch(tech.Index) and not te:HasTech(tech.Index) then
        local cost = te:GetResearchCost(tech.Index)
        local progress = te:GetResearchProgress(tech.Index)
        local turns = te:GetTurnsToResearch(tech.Index)
        local pct = cost > 0 and math.floor(progress * 100 / cost) or 0
        local boosted = te:HasBoostBeenTriggered(tech.Index)
        local boostDesc = ""
        local b = boostsByTech[tech.TechnologyType]
        if b and b.TriggerDescription then
            boostDesc = Locale.Lookup(b.TriggerDescription):gsub("|", "/")
        end
        local eraStr = tech.EraType and (" [" .. eraLabel(tech.EraType) .. "]") or ""
        local boostStr = boosted and " BOOSTED" or ""
        local boostDescStr = (boostDesc ~= "" and (" [Boost: " .. boostDesc .. "]")) or ""
        local unlocksStr = buildUnlocks("PrereqTech", "InitiatorPrereqTech", tech.TechnologyType, false)
        local abilityStr = descStrFor(tech)
        local prereqStr = ""
        if techPrereqs[tech.TechnologyType] then
            prereqStr = " (needs: " .. table.concat(techPrereqs[tech.TechnologyType], ",") .. ")"
        end
        local flag = (turns <= 2) and " !! GRAB THIS" or ""
        local line = "  " .. Locale.Lookup(tech.Name) .. " (" .. tech.TechnologyType .. ")" .. eraStr .. " - " .. pct .. "%, " .. turns .. " turns" .. boostStr .. boostDescStr .. unlocksStr .. abilityStr .. prereqStr .. flag
        table.insert(techOptions, {turns = turns, line = line})
    end
end
if #techOptions > 0 then
    table.sort(techOptions, function(a, b) return a.turns < b.turns end)
    emit("")
    emit("Available techs:")
    for _, t in ipairs(techOptions) do emit(t.line) end
end

-- Available civics (within curEra + 2, prereqs satisfied)
local civicOptions = {}
for civic in GameInfo.Civics() do
    if not cu:HasCivic(civic.Index) then
        local civicEra = eraIndex[civic.EraType] or 99
        if civicEra <= curEra + 2 then
            local canProgress = true
            if civicPrereqs[civic.CivicType] then
                for _, pType in ipairs(civicPrereqs[civic.CivicType]) do
                    local pEntry = GameInfo.Civics[pType]
                    if pEntry and not cu:HasCivic(pEntry.Index) then canProgress = false; break end
                end
            end
            if canProgress then
                local cost = cu:GetCultureCost(civic.Index)
                local currentProg = 0
                pcall(function() currentProg = cu:GetCulturalProgress(civic.Index) end)
                local pct2 = cost > 0 and math.floor(currentProg * 100 / cost) or 0
                local turns2 = cu:GetTurnsToProgressCivic(civic.Index)
                local boosted2 = cu:HasBoostBeenTriggered(civic.Index)
                local boostDesc2 = ""
                local b2 = boostsByCivic[civic.CivicType]
                if b2 and b2.TriggerDescription then
                    boostDesc2 = Locale.Lookup(b2.TriggerDescription):gsub("|", "/")
                end
                local eraStr = civic.EraType and (" [" .. eraLabel(civic.EraType) .. "]") or ""
                local boostStr = boosted2 and " BOOSTED" or ""
                local boostDescStr = (boostDesc2 ~= "" and (" [Boost: " .. boostDesc2 .. "]")) or ""
                local unlocksStr = buildUnlocks("PrereqCivic", "InitiatorPrereqCivic", civic.CivicType, true)
                local abilityStr = descStrFor(civic)
                local prereqStr = ""
                if civicPrereqs[civic.CivicType] then
                    prereqStr = " (needs: " .. table.concat(civicPrereqs[civic.CivicType], ",") .. ")"
                end
                local flag = (turns2 <= 2) and " !! GRAB THIS" or ""
                local line = "  " .. Locale.Lookup(civic.Name) .. " (" .. civic.CivicType .. ")" .. eraStr .. " - " .. pct2 .. "%, " .. turns2 .. " turns" .. boostStr .. boostDescStr .. unlocksStr .. abilityStr .. prereqStr .. flag
                table.insert(civicOptions, {turns = turns2, line = line})
            end
        end
    end
end
if #civicOptions > 0 then
    table.sort(civicOptions, function(a, b) return a.turns < b.turns end)
    emit("")
    emit("Available civics:")
    for _, c in ipairs(civicOptions) do emit(c.line) end
end

-- Locked techs (within curEra + 2 only - skip far-future clutter)
local lockedTechsByEra = {}
local hasLockedTechs = false
for tech in GameInfo.Technologies() do
    local techEra = eraIndex[tech.EraType] or 99
    if not te:HasTech(tech.Index) and not te:CanResearch(tech.Index) and techEra <= curEra + 2 then
        local missing = {}
        if techPrereqs[tech.TechnologyType] then
            for _, pType in ipairs(techPrereqs[tech.TechnologyType]) do
                local pEntry = GameInfo.Technologies[pType]
                if pEntry and not te:HasTech(pEntry.Index) then
                    table.insert(missing, (Locale.Lookup(pEntry.Name):gsub("|", "/")))
                end
            end
        end
        if #missing > 0 then
            local boostDesc = ""
            local b = boostsByTech[tech.TechnologyType]
            if b and b.TriggerDescription then boostDesc = Locale.Lookup(b.TriggerDescription):gsub("|", "/") end
            local boostTag = te:HasBoostBeenTriggered(tech.Index) and " BOOSTED" or ""
            local boostDescStr = (boostDesc ~= "" and (" [Boost: " .. boostDesc .. "]")) or ""
            local abilityStr = descStrFor(tech)
            local line = "  " .. Locale.Lookup(tech.Name):gsub("|", "/") .. " (" .. tech.TechnologyType .. ") - needs: " .. table.concat(missing, ", ") .. boostTag .. boostDescStr .. abilityStr
            local eraKey = tech.EraType or "UNKNOWN"
            if not lockedTechsByEra[eraKey] then lockedTechsByEra[eraKey] = {} end
            table.insert(lockedTechsByEra[eraKey], line)
            hasLockedTechs = true
        end
    end
end
if hasLockedTechs then
    emit("")
    emit("Locked techs (prerequisites missing):")
    local eras = {}
    for era in pairs(lockedTechsByEra) do table.insert(eras, era) end
    table.sort(eras, function(a, b) return (eraRank[a] or 99) < (eraRank[b] or 99) end)
    for _, era in ipairs(eras) do
        emit("  -- " .. eraLabel(era) .. " --")
        for _, line in ipairs(lockedTechsByEra[era]) do emit(line) end
    end
end

-- Locked civics (within curEra + 2 only)
local lockedCivicsByEra = {}
local hasLockedCivics = false
for civic in GameInfo.Civics() do
    local civicEra = eraIndex[civic.EraType] or 99
    if not cu:HasCivic(civic.Index) and civicEra <= curEra + 2 then
        local missing = {}
        if civicPrereqs[civic.CivicType] then
            for _, pType in ipairs(civicPrereqs[civic.CivicType]) do
                local pEntry = GameInfo.Civics[pType]
                if pEntry and not cu:HasCivic(pEntry.Index) then
                    table.insert(missing, (Locale.Lookup(pEntry.Name):gsub("|", "/")))
                end
            end
        end
        if #missing > 0 then
            local boostDesc = ""
            local b = boostsByCivic[civic.CivicType]
            if b and b.TriggerDescription then boostDesc = Locale.Lookup(b.TriggerDescription):gsub("|", "/") end
            local boostTag = cu:HasBoostBeenTriggered(civic.Index) and " BOOSTED" or ""
            local boostDescStr = (boostDesc ~= "" and (" [Boost: " .. boostDesc .. "]")) or ""
            local abilityStr = descStrFor(civic)
            local line = "  " .. Locale.Lookup(civic.Name):gsub("|", "/") .. " (" .. civic.CivicType .. ") - needs: " .. table.concat(missing, ", ") .. boostTag .. boostDescStr .. abilityStr
            local eraKey = civic.EraType or "UNKNOWN"
            if not lockedCivicsByEra[eraKey] then lockedCivicsByEra[eraKey] = {} end
            table.insert(lockedCivicsByEra[eraKey], line)
            hasLockedCivics = true
        end
    end
end
if hasLockedCivics then
    emit("")
    emit("Locked civics (prerequisites missing):")
    local eras = {}
    for era in pairs(lockedCivicsByEra) do table.insert(eras, era) end
    table.sort(eras, function(a, b) return (eraRank[a] or 99) < (eraRank[b] or 99) end)
    for _, era in ipairs(eras) do
        emit("  -- " .. eraLabel(era) .. " --")
        for _, line in ipairs(lockedCivicsByEra[era]) do emit(line) end
    end
end

print(table.concat(out, "\n"))
