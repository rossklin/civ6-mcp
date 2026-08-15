local id = Game.GetLocalPlayer()
local te = Players[id]:GetTechs()
local cu = Players[id]:GetCulture()
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
print("CURRENT|" .. techName .. "|" .. techTurns .. "|" .. civicName .. "|" .. civicTurns)
-- Build boost lookup
local boostsByTech = {}
local boostsByCivic = {}
for b in GameInfo.Boosts() do
    if b.TechnologyType then boostsByTech[b.TechnologyType] = b end
    if b.CivicType then boostsByCivic[b.CivicType] = b end
end
-- Build tech prereqs lookup
local techPrereqs = {}
pcall(function()
    for row in GameInfo.TechnologyPrereqs() do
        if not techPrereqs[row.Technology] then techPrereqs[row.Technology] = {} end
        table.insert(techPrereqs[row.Technology], row.PrereqTech)
    end
end)
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
        local unlocks = {}
        for u in GameInfo.Units() do if u.PrereqTech == tech.TechnologyType then table.insert(unlocks, Locale.Lookup(u.Name)) end end
        for bld in GameInfo.Buildings() do if bld.PrereqTech == tech.TechnologyType then table.insert(unlocks, Locale.Lookup(bld.Name)) end end
        for d in GameInfo.Districts() do if d.PrereqTech == tech.TechnologyType then table.insert(unlocks, Locale.Lookup(d.Name)) end end
        for imp in GameInfo.Improvements() do if imp.PrereqTech == tech.TechnologyType then table.insert(unlocks, Locale.Lookup(imp.Name)) end end
        for r in GameInfo.Resources() do
            if r.PrereqTech == tech.TechnologyType then table.insert(unlocks, "Reveals " .. Locale.Lookup(r.Name)) end
        end
        pcall(function()
            for proj in GameInfo.Projects() do
                if proj.PrereqTech == tech.TechnologyType then table.insert(unlocks, "Project: " .. Locale.Lookup(proj.Name)) end
            end
        end)
        local unlockStr = table.concat(unlocks, ", "):gsub("|", "/")
        local boostTag = boosted and "BOOSTED" or "UNBOOSTED"
        local prereqStr = ""
        if techPrereqs[tech.TechnologyType] then
            prereqStr = table.concat(techPrereqs[tech.TechnologyType], ",")
        end
        print("TECH|" .. Locale.Lookup(tech.Name) .. "|" .. tech.TechnologyType .. "|" .. cost .. "|" .. pct .. "|" .. turns .. "|" .. boostTag .. "|" .. boostDesc .. "|" .. unlockStr .. "|" .. prereqStr .. "|" .. (tech.EraType or ""))
    end
end
local completedTechs = 0
for tech in GameInfo.Technologies() do
    if te:HasTech(tech.Index) then completedTechs = completedTechs + 1 end
end
local completedCivics = 0
for civic in GameInfo.Civics() do
    if cu:HasCivic(civic.Index) then completedCivics = completedCivics + 1 end
end
print("COMPLETED|" .. completedTechs .. "|" .. completedCivics)
local curEra = Game.GetEras():GetCurrentEra()
local prereqs = {}
for row in GameInfo.CivicPrereqs() do
    if not prereqs[row.Civic] then prereqs[row.Civic] = {} end
    table.insert(prereqs[row.Civic], row.PrereqCivic)
end
local eraLookup = {}
for e in GameInfo.Eras() do eraLookup[e.EraType] = e.Index end
for civic in GameInfo.Civics() do
    if not cu:HasCivic(civic.Index) then
        local civicEra = eraLookup[civic.EraType] or 99
        if civicEra <= curEra + 2 then
            local canProgress = true
            if prereqs[civic.CivicType] then
                for _, pType in ipairs(prereqs[civic.CivicType]) do
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
                local boostTag2 = boosted2 and "BOOSTED" or "UNBOOSTED"
                local civicPrereqStr = ""
                if prereqs[civic.CivicType] then
                    civicPrereqStr = table.concat(prereqs[civic.CivicType], ",")
                end
                print("CIVIC|" .. Locale.Lookup(civic.Name) .. "|" .. civic.CivicType .. "|" .. cost .. "|" .. pct2 .. "|" .. turns2 .. "|" .. boostTag2 .. "|" .. boostDesc2 .. "|" .. civicPrereqStr .. "|" .. (civic.EraType or ""))
            end
        end
    end
end
-- Locked civics: within curEra + 2 only (skip far-future clutter)
for civic in GameInfo.Civics() do
    local civicEra = eraLookup[civic.EraType] or 99
    if not cu:HasCivic(civic.Index) and civicEra <= curEra + 2 then
        local missing = {}
        if prereqs[civic.CivicType] then
            for _, pType in ipairs(prereqs[civic.CivicType]) do
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
            local boostTag = cu:HasBoostBeenTriggered(civic.Index) and "BOOSTED" or "UNBOOSTED"
            print("LOCKED_CIVIC|" .. Locale.Lookup(civic.Name):gsub("|", "/") .. "|" .. civic.CivicType .. "|" .. table.concat(missing, ",") .. "|" .. (civic.EraType or "") .. "|" .. boostTag .. "|" .. boostDesc)
        end
    end
end
-- Locked techs: within curEra + 2 only (skip far-future clutter)
for tech in GameInfo.Technologies() do
    local techEra = eraLookup[tech.EraType] or 99
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
            local boostTag = te:HasBoostBeenTriggered(tech.Index) and "BOOSTED" or "UNBOOSTED"
            print("LOCKED_TECH|" .. Locale.Lookup(tech.Name):gsub("|", "/") .. "|" .. tech.TechnologyType .. "|" .. table.concat(missing, ",") .. "|" .. (tech.EraType or "") .. "|" .. boostTag .. "|" .. boostDesc)
        end
    end
end
print("{SENTINEL}")