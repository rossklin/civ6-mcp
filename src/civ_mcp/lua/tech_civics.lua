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
-- Per-civic award counts, read from CivicModifiers + ModifierArguments.
-- Envoy awards (MODIFIER_PLAYER_GRANT_INFLUENCE_TOKEN, "Amount") are the civic
-- tree's numbered icon; governor titles (MODIFIER_PLAYER_ADJUST_GOVERNOR_POINTS,
-- "Delta") have no tree icon but are granted by ~13 civics (State Workforce,
-- Early Empire, ... Future Civic), one each.
local modTypeById = {}
for m in GameInfo.Modifiers() do modTypeById[m.ModifierId] = m.ModifierType end
local argByNameByModId = {}
for ma in GameInfo.ModifierArguments() do
    if not argByNameByModId[ma.ModifierId] then argByNameByModId[ma.ModifierId] = {} end
    argByNameByModId[ma.ModifierId][ma.Name] = ma.Value
end
local function awardsByCivic(modifierType, argName)
    local map = {}
    for cm in GameInfo.CivicModifiers() do
        if modTypeById[cm.ModifierId] == modifierType then
            local args = argByNameByModId[cm.ModifierId]
            if args then
                local v = tonumber(args[argName])
                if v then map[cm.CivicType] = (map[cm.CivicType] or 0) + v end
            end
        end
    end
    return map
end
local envoysByCivic = awardsByCivic("MODIFIER_PLAYER_GRANT_INFLUENCE_TOKEN", "Amount")
local govTitlesByCivic = awardsByCivic("MODIFIER_PLAYER_ADJUST_GOVERNOR_POINTS", "Delta")
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
    if isCivic and govTitlesByCivic[typeStr] then
        local n = govTitlesByCivic[typeStr]
        table.insert(unlocks, "Awards " .. n .. " Governor Title" .. (n > 1 and "s" or ""))
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

-- Display ordering for era groupings (completed / locked sections).
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
-- Emit a by-era table (eraType -> list of lines) sorted chronologically,
-- each era under a "-- ERA --" header. No-op when empty.
local function emitByEra(header, byEra)
    local eras = {}
    for era in pairs(byEra) do table.insert(eras, era) end
    if #eras == 0 then return end
    table.sort(eras, function(a, b) return (eraRank[a] or 99) < (eraRank[b] or 99) end)
    emit("")
    emit(header)
    for _, era in ipairs(eras) do
        emit("  -- " .. eraLabel(era) .. " --")
        for _, line in ipairs(byEra[era]) do emit(line) end
    end
end

-- Completed techs (grouped by era, full detail)
local completedTechsByEra = {}
for tech in GameInfo.Technologies() do
    if te:HasTech(tech.Index) then
        local eraStr = tech.EraType and (" [" .. eraLabel(tech.EraType) .. "]") or ""
        local unlocksStr = buildUnlocks("PrereqTech", "InitiatorPrereqTech", tech.TechnologyType, false)
        local abilityStr = descStrFor(tech)
        local line = "  " .. Locale.Lookup(tech.Name):gsub("|", "/") .. " (" .. tech.TechnologyType .. ")" .. eraStr .. unlocksStr .. abilityStr
        local eraKey = tech.EraType or "UNKNOWN"
        if not completedTechsByEra[eraKey] then completedTechsByEra[eraKey] = {} end
        table.insert(completedTechsByEra[eraKey], line)
    end
end
emitByEra("Completed techs:", completedTechsByEra)

-- Completed civics (grouped by era, full detail)
local completedCivicsByEra = {}
for civic in GameInfo.Civics() do
    if cu:HasCivic(civic.Index) then
        local eraStr = civic.EraType and (" [" .. eraLabel(civic.EraType) .. "]") or ""
        local unlocksStr = buildUnlocks("PrereqCivic", "InitiatorPrereqCivic", civic.CivicType, true)
        local abilityStr = descStrFor(civic)
        local line = "  " .. Locale.Lookup(civic.Name):gsub("|", "/") .. " (" .. civic.CivicType .. ")" .. eraStr .. unlocksStr .. abilityStr
        local eraKey = civic.EraType or "UNKNOWN"
        if not completedCivicsByEra[eraKey] then completedCivicsByEra[eraKey] = {} end
        table.insert(completedCivicsByEra[eraKey], line)
    end
end
emitByEra("Completed civics:", completedCivicsByEra)

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

-- Available civics (prereqs satisfied)
local civicOptions = {}
for civic in GameInfo.Civics() do
    if not cu:HasCivic(civic.Index) then
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
if #civicOptions > 0 then
    table.sort(civicOptions, function(a, b) return a.turns < b.turns end)
    emit("")
    emit("Available civics:")
    for _, c in ipairs(civicOptions) do emit(c.line) end
end

-- Locked techs (all eras, prerequisites missing)
local lockedTechsByEra = {}
for tech in GameInfo.Technologies() do
    if not te:HasTech(tech.Index) and not te:CanResearch(tech.Index) then
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
            local unlocksStr = buildUnlocks("PrereqTech", "InitiatorPrereqTech", tech.TechnologyType, false)
            local abilityStr = descStrFor(tech)
            local line = "  " .. Locale.Lookup(tech.Name):gsub("|", "/") .. " (" .. tech.TechnologyType .. ") - needs: " .. table.concat(missing, ", ") .. boostTag .. boostDescStr .. unlocksStr .. abilityStr
            local eraKey = tech.EraType or "UNKNOWN"
            if not lockedTechsByEra[eraKey] then lockedTechsByEra[eraKey] = {} end
            table.insert(lockedTechsByEra[eraKey], line)
        end
    end
end
emitByEra("Locked techs (prerequisites missing):", lockedTechsByEra)

-- Locked civics (all eras, prerequisites missing)
local lockedCivicsByEra = {}
for civic in GameInfo.Civics() do
    if not cu:HasCivic(civic.Index) then
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
            local unlocksStr = buildUnlocks("PrereqCivic", "InitiatorPrereqCivic", civic.CivicType, true)
            local abilityStr = descStrFor(civic)
            local line = "  " .. Locale.Lookup(civic.Name):gsub("|", "/") .. " (" .. civic.CivicType .. ") - needs: " .. table.concat(missing, ", ") .. boostTag .. boostDescStr .. unlocksStr .. abilityStr
            local eraKey = civic.EraType or "UNKNOWN"
            if not lockedCivicsByEra[eraKey] then lockedCivicsByEra[eraKey] = {} end
            table.insert(lockedCivicsByEra[eraKey], line)
        end
    end
end
emitByEra("Locked civics (prerequisites missing):", lockedCivicsByEra)

print(table.concat(out, "\n"))
