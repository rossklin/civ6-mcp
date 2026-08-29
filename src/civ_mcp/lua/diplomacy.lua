-- Diplomacy overview of every major civilization, emitted as natural text
-- (the same format narrate_diplomacy used to produce from the pipe-delimited
-- roundtrip). __MCP_MANAGED_IDS_TAG__ is substituted Python-side with a Lua
-- table of agent-managed player ids ({} or {[1]=true,[3]=true}): managed
-- civs are effectively "us", so their relationship state, modifiers and
-- agendas are hidden.
local managed = __MCP_MANAGED_IDS_TAG__
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local states = {"ALLIED","DECLARED_FRIEND","FRIENDLY","NEUTRAL","UNFRIENDLY","DENOUNCED","WAR"}
local aNames = {"RESEARCH","CULTURAL","ECONOMIC","MILITARY","RELIGIOUS"}
local checkActions = {"DIPLOACTION_DIPLOMATIC_DELEGATION","DIPLOACTION_DECLARE_FRIENDSHIP","DIPLOACTION_DENOUNCE","DIPLOACTION_RESIDENT_EMBASSY","DIPLOACTION_OPEN_BORDERS","DIPLOACTION_MAKE_ALLIANCE"}

local out = {}
local function emit(s) table.insert(out, s) end

-- Clean localized text: strip icon/newline/color markup tokens and collapse
-- whitespace (from _LUA_CLEAN_TEXT in lua/diplomacy.py).
local function _cleanText(s)
    if s == nil then return "" end
    s = tostring(s)
    s = s:gsub("%[ICON_[%w_]+%]", "")
    s = s:gsub("%[NEWLINE%]", " ")
    s = s:gsub("%[COLOR[%w_]*%]", "")
    s = s:gsub("%[/COLOR%]", "")
    s = s:gsub("|", "/")
    s = s:gsub("%s+", " ")
    return s
end
-- Nil-safe localized lookup: GameInfo rows may have nil Name/Description and
-- Locale.Lookup(nil) raises.
local function _loc(rawKey)
    if rawKey == nil then return "" end
    return _cleanText(Locale.Lookup(rawKey))
end

-- Python str.title() equivalent for action labels: capitalize each word,
-- lowercase the rest ("DECLARE_WAR" -> "Declare War").
local function titleCase(s)
    return (s:gsub("_", " "):gsub("%a+", function(w)
        return w:sub(1, 1):upper() .. w:sub(2):lower()
    end))
end

-- traitType -> list of {kind, name, desc} records for the unique units/
-- buildings/districts/improvements granted by that trait (from
-- _LUA_BUILD_TRAIT_UNIQUES in lua/diplomacy.py).
local traitUniques = {}
local function addUnique(t, kind, name, desc)
    if t == nil then return end
    if traitUniques[t] == nil then traitUniques[t] = {} end
    table.insert(traitUniques[t], {kind = kind, name = name, desc = desc})
end
for u in GameInfo.Units() do addUnique(u.TraitType, "UNIT", _loc(u.Name), _loc(u.Description)) end
for b in GameInfo.Buildings() do addUnique(b.TraitType, "BUILDING", _loc(b.Name), _loc(b.Description)) end
for d in GameInfo.Districts() do addUnique(d.TraitType, "DISTRICT", _loc(d.Name), _loc(d.Description)) end
for imp in GameInfo.Improvements() do addUnique(imp.TraitType, "IMPROVEMENT", _loc(imp.Name), _loc(imp.Description)) end

local function civDisplayName(pid)
    local cfg = PlayerConfigurations[pid]
    if cfg then return Locale.Lookup(cfg:GetCivilizationShortDescription()):gsub("|", "/") end
    return "player " .. pid
end

-- Our military strength (the "vs our N" reference for every rival).
local myMil = 0
pcall(function() myMil = Players[me]:GetStats():GetMilitaryStrength() or 0 end)

-- Third-party relationships between the other met civs, collected up front
-- so each civ's block can list them inline: relOthers[pid] = list of
-- descriptor strings. Only pairs where we have met BOTH civs are included
-- (the game hides relationship intel on civs we haven't met). What started
-- as the PACT|a|b|DEFENSIVE scan now also covers alliances, declared
-- friendships, open borders, denouncements and wars.
local relOthers = {}
local function addPairRel(i, j, descFn)
    -- descFn(selfPid, otherPid) returns the fragment for self's block, so
    -- directional relationships can phrase each side correctly.
    if relOthers[i] == nil then relOthers[i] = {} end
    if relOthers[j] == nil then relOthers[j] = {} end
    table.insert(relOthers[i], descFn(i, j))
    table.insert(relOthers[j], descFn(j, i))
end
local function partnerName(pid)
    return civDisplayName(pid) .. " (player " .. pid .. ")"
end
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() and pDiplo:HasMet(i) then
        for j = i + 1, 62 do
            if j ~= me and Players[j] and Players[j]:IsAlive() and Players[j]:IsMajor() and pDiplo:HasMet(j) then
                local iDiplo = Players[i]:GetDiplomacy()
                local jDiplo = Players[j]:GetDiplomacy()
                -- Diplomatic state index each way (offset into the states
                -- table: 0=ALLIED, 1=DECLARED_FRIEND, 5=DENOUNCED; -1 when
                -- they have not met each other, in which case nothing below
                -- matches).
                local si, sj = -1, -1
                pcall(function() si = Players[i]:GetDiplomaticAI():GetDiplomaticStateIndex(j) end)
                pcall(function() sj = Players[j]:GetDiplomaticAI():GetDiplomaticStateIndex(i) end)

                local okW, atWar = pcall(function() return iDiplo:IsAtWarWith(j) end)
                if okW and atWar then
                    addPairRel(i, j, function(_, other)
                        return "at war with " .. partnerName(other)
                    end)
                end

                -- Alliance is mutual; the type index is only meaningful
                -- while the ALLIED state is set.
                if si == 0 or sj == 0 then
                    local aType, aLevel = -1, 1
                    pcall(function() aType = iDiplo:GetAllianceType(j) or -1 end)
                    pcall(function() aLevel = iDiplo:GetAllianceLevel(j) or 1 end)
                    local typeStr = ((aType >= 0 and aNames[aType + 1]) or "unknown"):lower()
                    local lvlStr = ""
                    if aLevel > 0 then lvlStr = " Lv" .. aLevel end
                    addPairRel(i, j, function(_, other)
                        return typeStr .. " alliance" .. lvlStr .. " with " .. partnerName(other)
                    end)
                end

                if si == 1 or sj == 1 then
                    addPairRel(i, j, function(_, other)
                        return "declared friends with " .. partnerName(other)
                    end)
                end

                -- Open borders is directional: HasOpenBordersFrom(j) on
                -- iDiplo means j granted passage, so i's units may enter j's
                -- lands (usually traded both ways in one deal).
                local obi, obj = false, false
                pcall(function() obi = iDiplo:HasOpenBordersFrom(j) end)
                pcall(function() obj = jDiplo:HasOpenBordersFrom(i) end)
                if obi or obj then
                    addPairRel(i, j, function(self, other)
                        if obi and obj then return "open borders with " .. partnerName(other) end
                        if (self == i) == obi then return "gets open borders from " .. partnerName(other) end
                        return "gives open borders to " .. partnerName(other)
                    end)
                end

                -- Denunciation is directional too: GetDenounceTurn(k) is the
                -- turn this player denounced the other (-1 when none); the
                -- DENOUNCED state only says somebody did.
                if si == 5 or sj == 5 then
                    local iDT, jDT = -1, -1
                    pcall(function() iDT = iDiplo:GetDenounceTurn(j) or -1 end)
                    pcall(function() jDT = jDiplo:GetDenounceTurn(i) or -1 end)
                    addPairRel(i, j, function(self, other)
                        local selfDT = (self == i) and iDT or jDT
                        local otherDT = (self == i) and jDT or iDT
                        if selfDT >= 0 and otherDT >= 0 then return "mutually denounced with " .. partnerName(other) end
                        if selfDT >= 0 then return "denounced " .. partnerName(other) end
                        if otherDT >= 0 then return "denounced by " .. partnerName(other) end
                        return "denunciation with " .. partnerName(other)
                    end)
                end

                local okP, hp = pcall(function() return iDiplo:HasDefensivePact(j) end)
                if okP and hp then
                    addPairRel(i, j, function(_, other)
                        return "defensive pact with " .. partnerName(other)
                    end)
                end
            end
        end
    end
end

-- Render each met civ as a block of lines; unmet civs get a one-liner.
-- blocks is keyed by player id and flushed in ascending order at the end.
local blocks = {}
local nCivs = 0
for i = 0, 62 do
    if i ~= me and Players[i] and Players[i]:IsAlive() and Players[i]:IsMajor() then
        nCivs = nCivs + 1
        if not pDiplo:HasMet(i) then
            blocks[i] = {"  Unmet Civilization (Unknown Leader) — not met"}
        else
            local cfg = PlayerConfigurations[i]
            local civName = Locale.Lookup(cfg:GetCivilizationShortDescription()):gsub("|", "/")
            local leaderName = Locale.Lookup(cfg:GetLeaderName()):gsub("|", "/")
            local bl = {}
            local function line(s) table.insert(bl, s) end

            local ai = Players[i]:GetDiplomaticAI()
            local stateIdx = ai:GetDiplomaticStateIndex(me)
            local stateName = states[stateIdx + 1] or tostring(stateIdx)
            local grievances = pDiplo:GetGrievancesAgainst(i)
            local vis = pDiplo:GetVisibilityOn(i)
            local hasDel = pDiplo:HasDelegationAt(i)
            local hasEmb = pDiplo:HasEmbassyAt(i)
            local theyDel = Players[i]:GetDiplomacy():HasDelegationAt(me)
            local theyEmb = Players[i]:GetDiplomacy():HasEmbassyAt(me)

            -- Relationship modifiers; the score sum feeds the header line.
            local mods = {}
            local relScore = 0
            local okMods, modList = pcall(function() return ai:GetDiplomaticModifiers(me) end)
            if okMods and modList then
                for _, mod in ipairs(modList) do
                    table.insert(mods, mod)
                    relScore = relScore + mod.Score
                end
            end

            -- Header with state and score
            local warStr = ""
            if pDiplo:IsAtWarWith(i) then warStr = " **AT WAR**" end
            local allianceStr = ""
            if stateIdx == 0 then
                local ok3, aType = pcall(function() return pDiplo:GetAllianceType(i) end)
                if ok3 and aType and aType >= 0 then
                    local aLevel = 1
                    pcall(function() aLevel = pDiplo:GetAllianceLevel(i) or 1 end)
                    local lvlStr = ""
                    if aLevel > 0 then lvlStr = " Lv" .. aLevel end
                    allianceStr = " (" .. (aNames[aType + 1] or tostring(aType)) .. " alliance" .. lvlStr .. ")"
                end
            end
            local statusStr = ""
            if not managed[i] then
                statusStr = " — " .. stateName .. " (" .. (relScore >= 0 and "+" or "") .. relScore .. ")"
            end
            line("  " .. civName .. " (" .. leaderName .. ") " .. statusStr .. warStr .. allianceStr .. " [player " .. i .. "]")

            -- City details
            local nCivCities = 0
            local cityParts = {}
            for _, ec in Players[i]:GetCities():Members() do
                nCivCities = nCivCities + 1
                local ecx, ecy = ec:GetX(), ec:GetY()
                if pVis:IsRevealed(ecx, ecy) then
                    local ecName = Locale.Lookup(ec:GetName()):gsub("|", "/")
                    local ecPop = ec:GetPopulation()
                    local ecLoy, ecLoyPT = 100, 0
                    local ecCult = ec:GetCulturalIdentity()
                    if ecCult then ecLoy = ecCult:GetLoyalty(); ecLoyPT = ecCult:GetLoyaltyPerTurn() end
                    local ecWalls = 0
                    pcall(function()
                        for _, d in ec:GetDistricts():Members() do
                            local di = GameInfo.Districts[d:GetType()]
                            if di and di.DistrictType == "DISTRICT_CITY_CENTER" then
                                ecWalls = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                                break
                            end
                        end
                    end)
                    local wallsStr = ""
                    if ecWalls > 0 then wallsStr = " [walls]" end
                    local loyWarn = ""
                    if ecLoyPT < -0.5 or ecLoy < 50 then
                        loyWarn = string.format(" !! loy %.0f (%+.1f/t)", ecLoy, ecLoyPT)
                    end
                    table.insert(cityParts, ecName .. " pop " .. ecPop .. " (" .. ecx .. "," .. ecy .. ")" .. wallsStr .. loyWarn)
                end
            end
            if nCivCities > 0 then
                if #cityParts > 0 then
                    local hidden = nCivCities - #cityParts
                    local fogStr = ""
                    if hidden > 0 then fogStr = " + " .. hidden .. " in fog" end
                    line("    Cities (" .. nCivCities .. "): " .. table.concat(cityParts, "; ") .. fogStr)
                else
                    line("    Cities: " .. nCivCities .. " (all in fog)")
                end
            end

            -- Military strength comparison
            local okMil, milRaw = pcall(function() return Players[i]:GetStats():GetMilitaryStrength() end)
            local mil = (okMil and milRaw) or 0
            if mil > 0 then
                if myMil > 0 then
                    local ratio = mil / myMil
                    local ratioStr = ""
                    if ratio >= 1.2 or ratio <= 0.8 then ratioStr = string.format(" (%.1fx)", ratio) end
                    local threatFlag = ""
                    if ratio >= 1.5 and (stateName == "UNFRIENDLY" or stateName == "DENOUNCED" or stateName == "WAR") then
                        threatFlag = " !! MILITARY THREAT"
                    elseif ratio >= 2.0 then
                        threatFlag = " !! MUCH STRONGER"
                    end
                    line("    Military: " .. mil .. " vs our " .. myMil .. ratioStr .. threatFlag)
                else
                    line("    Military: " .. mil)
                end
            end

            -- Access: delegations/embassies
            local access = {}
            if hasDel then table.insert(access, "we have delegation") end
            if theyDel then table.insert(access, "they have delegation") end
            if hasEmb then table.insert(access, "we have embassy") end
            if theyEmb then table.insert(access, "they have embassy") end
            if grievances > 0 then table.insert(access, "grievances: " .. grievances) end
            if #access > 0 then line("    Access: " .. table.concat(access, ", ")) end

            -- Relationship modifiers (hidden for managed civs)
            if not managed[i] then
                for _, mod in ipairs(mods) do
                    local txt = tostring(mod.Text):gsub("|", "/")
                    line("    " .. (mod.Score >= 0 and "+" or "") .. mod.Score .. " " .. txt)
                end
            end

            -- Relationships with the other civilizations (alliances,
            -- declared friendships, open borders, denouncements, wars,
            -- defensive pacts — gathered by the pair scan above)
            if relOthers[i] then
                line("    With other civs: " .. table.concat(relOthers[i], "; "))
            end

            -- Agendas (hidden for managed civs; visibility-gated: historical
            -- always, random only at SECRET+)
            if not managed[i] then
                local okAg, agendas = pcall(function() return Players[i]:GetAgendaTypes() end)
                if okAg and agendas then
                    local histSet = {}
                    local leaderType = cfg:GetLeaderTypeName()
                    for ha in GameInfo.HistoricalAgendas() do
                        if ha.LeaderType == leaderType then
                            local aDef = GameInfo.Agendas[ha.AgendaType]
                            if aDef then histSet[aDef.Index] = true end
                        end
                    end
                    for _, agIdx in ipairs(agendas) do
                        local aDef = GameInfo.Agendas[agIdx]
                        if aDef then
                            if histSet[agIdx] then
                                line("    Agenda: " .. Locale.Lookup(aDef.Name) .. " — " .. Locale.Lookup(aDef.Description))
                            elseif vis >= 3 then
                                line("    Agenda: [Hidden] " .. Locale.Lookup(aDef.Name) .. " — " .. Locale.Lookup(aDef.Description))
                            else
                                line("    Agenda: [Hidden] — Requires Secret diplomatic visibility (spy or alliance)")
                            end
                        end
                    end
                end
            end

            -- Unique abilities (civ + leader traits) and unique units/
            -- buildings/districts/improvements. Only the named ability traits
            -- carry a Description; marker traits (e.g. infrastructure tags)
            -- are filtered out by requiring a non-empty description.
            local civType = cfg:GetCivilizationTypeName()
            local leaderType = cfg:GetLeaderTypeName()
            local traitSeen = {}
            for ct in GameInfo.CivilizationTraits() do
                if ct.CivilizationType == civType and traitSeen[ct.TraitType] == nil then
                    traitSeen[ct.TraitType] = true
                    local tDef = GameInfo.Traits[ct.TraitType]
                    if tDef and tDef.Description and tDef.Description ~= "" then
                        local tName = _loc(tDef.Name)
                        if tName == "" then tName = ct.TraitType end
                        line("    Ability [Civ]: " .. tName .. " — " .. _loc(tDef.Description))
                    end
                end
            end
            for lt in GameInfo.LeaderTraits() do
                if lt.LeaderType == leaderType and traitSeen[lt.TraitType] == nil then
                    traitSeen[lt.TraitType] = true
                    local tDef = GameInfo.Traits[lt.TraitType]
                    if tDef and tDef.Description and tDef.Description ~= "" then
                        local tName = _loc(tDef.Name)
                        if tName == "" then tName = lt.TraitType end
                        line("    Ability [Leader]: " .. tName .. " — " .. _loc(tDef.Description))
                    end
                end
            end
            for t in pairs(traitSeen) do
                local list = traitUniques[t]
                if list then
                    for _, u in ipairs(list) do
                        local descStr = ""
                        if u.desc ~= "" then descStr = " — " .. u.desc end
                        line("    Unique [" .. titleCase(u.kind) .. "]: " .. u.name .. descStr)
                    end
                end
            end

            -- Available actions
            local avail = {}
            for _, aName in ipairs(checkActions) do
                local ok2, valid = pcall(function() return pDiplo:IsDiplomaticActionValid(aName, i, false) end)
                if ok2 and valid then
                    local label = aName:gsub("DIPLOACTION_", "")
                    if label == "OPEN_BORDERS" then label = "Open Borders (via propose_trade)" end
                    table.insert(avail, titleCase(label))
                end
            end
            if not pDiplo:IsAtWarWith(i) then
                local canWar = false
                pcall(function() canWar = pDiplo:CanDeclareWarOn(i) end)
                if canWar then table.insert(avail, titleCase("DECLARE_WAR")) end
            end
            if #avail > 0 then line("    Can: " .. table.concat(avail, ", ")) end

            blocks[i] = bl
        end
    end
end

if nCivs == 0 then
    emit("No known civilizations.")
else
    emit(nCivs .. " civilizations:")
    for i = 0, 62 do
        if blocks[i] then
            for _, l in ipairs(blocks[i]) do emit(l) end
        end
    end
end
print(table.concat(out, "\n"))
