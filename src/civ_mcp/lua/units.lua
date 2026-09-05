-- Units snapshot (InGame context).
--
-- Lists every local-player unit with upgrade, builder-improvement, formation,
-- and attack-target info. Output is pipe-delimited for parse_units_response
-- (NOT narrated prose) because the structured UnitInfo is consumed by the
-- turn-snapshot functions in game_state.py.
--
-- Attack targets are classified by effective occupancy class (the shared
-- occupancyClass helper, injected from _helpers.py's _LUA_OCCUPANCY_CLASS
-- snippet) and gated by the engine's own CanStartOperation with the same
-- operation the UI's move chain would use (RANGE_ATTACK for ranged fire at
-- distance; the MOVE_TO attack-move otherwise - there is no dedicated
-- theological operation, the engine resolves religious-vs-religious as
-- theological combat, needs no war, and only apostles/inquisitors may
-- initiate it). Classification: military
-- targets carry an engine damage estimate (CombatManager.SimulateAttackInto -
-- the same call the UI combat preview uses, authoritative for damage,
-- modifiers, and combat type); an unescorted civilian adjacent to a
-- melee-capable attacker is a CAPTURE target (move onto the tile - civilians
-- are never valid ranged targets); religious targets appear only for
-- religious attackers (theological combat - the simulator reports
-- CombatTypes.RELIGIOUS; military units cannot attack religious units).

__LUA_OCCUPANCY_CLASS__
local id = Game.GetLocalPlayer()
local tileUnits = {}
for i, u in Players[id]:GetUnits():Members() do
    local x, y = u:GetX(), u:GetY()
    if x ~= -9999 then
        local uid = u:GetID()
        local entry = GameInfo.Units[u:GetType()]
        local ut = entry and entry.UnitType or "UNKNOWN"
        local nm = Locale.Lookup(u:GetName())
        local cs = entry and entry.Combat or 0
        local rs = entry and entry.RangedCombat or 0
        local charges = u:GetBuildCharges() or 0
        local gp = u:GetGreatPerson()
        if gp then
            local ok_gp, gp_charges = pcall(function() return gp:GetActionCharges() end)
            if ok_gp and gp_charges and gp_charges > 0 then charges = gp_charges end
            if charges == 0 then
                -- Cultural GPs (Writers/Artists/Musicians) return 0 from
                -- GetActionCharges(). Fall back to the individual definition.
                pcall(function()
                    local indIdx = gp:GetIndividual()
                    for ind in GameInfo.GreatPersonIndividuals() do
                        if ind.Index == indIdx then
                            charges = ind.ActionCharges or 0
                            break
                        end
                    end
                end)
            end
        end
        if charges == 0 then
            local ok_sp, sp = pcall(function() return u:GetSpreadCharges() end)
            if ok_sp and sp and sp > 0 then charges = sp end
        end
        local relName = ""
        local ok_r, rIdx = pcall(function() return u:GetReligionType() end)
        if ok_r and rIdx and rIdx >= 0 then
            for row in GameInfo.Religions() do
                if row.Index == rIdx then relName = row.ReligionType; break end
            end
        end
        -- Attack-target scan (classified by occupancy class, see header).
        local targets = ""
        local aIsReligious = occupancyClass(entry) == "RELIGIOUS"
        if u:GetMovesRemaining() > 0 and (cs > 0 or rs > 0 or aIsReligious) then
            local rng = (rs > 0) and (entry and entry.Range or 1) or 1
            local tgtList = {}
            for dy = -rng, rng do
                for dx = -rng, rng do
                    local tx, ty = x + dx, y + dy
                    local d = Map.GetPlotDistance(x, y, tx, ty)
                    if d >= 1 and d <= rng then
                        local plotUnits = Map.GetUnitsAt(tx, ty)
                        if plotUnits then
                            -- Classify the tile's hostile units. Occupancy
                            -- rules allow one military defender plus one
                            -- civilian (and religious units) per tile; combat
                            -- always targets the military defender.
                            local def, defHP = nil, 0
                            local civ, civHP = nil, 0
                            local rel, relHP = nil, 0
                            for other in plotUnits:Units() do
                                local otherOwner = other:GetOwner()
                                if otherOwner ~= id then
                                    local oInfo = GameInfo.Units[other:GetType()]
                                    local oClass = occupancyClass(oInfo)
                                    -- Theological combat needs no war (and no
                                    -- barbarian); military interactions keep the
                                    -- war/barbarian gate.
                                    local isHostile = otherOwner >= 62
                                        or Players[id]:GetDiplomacy():IsAtWarWith(otherOwner)
                                        or (aIsReligious and oClass == "RELIGIOUS")
                                    if isHostile then
                                        local oName = oInfo and oInfo.UnitType or "UNKNOWN"
                                        local oHP = other:GetMaxDamage() - other:GetDamage()
                                        if oClass == "RELIGIOUS" then
                                            if rel == nil then rel, relHP = oName, oHP end
                                        elseif oClass == "FORMATION_CLASS_CIVILIAN" then
                                            if civ == nil then civ, civHP = oName, oHP end
                                        elseif def == nil then
                                            def, defHP = oName, oHP
                                        end
                                    end
                                end
                            end
                            -- What does THIS attacker interact with on the tile?
                            local simName, simHP = nil, 0
                            if def ~= nil and not aIsReligious then
                                simName, simHP = def, defHP
                            elseif rel ~= nil and aIsReligious then
                                simName, simHP = rel, relHP
                            end
                            -- Engine validity gate - the same operation the
                            -- UI's own move chain (Civ6Common.RequestMoveOperation)
                            -- would use: RANGE_ATTACK for ranged fire at
                            -- distance, otherwise the MOVE_TO attack-move
                            -- (there is no dedicated theological operation -
                            -- the engine resolves religious-vs-religious as
                            -- theological combat). CanStartOperation is the
                            -- engine's authority on attacker capability (only
                            -- apostles and inquisitors may initiate
                            -- theological combat) and on ranged LOS.
                            local engOK = true
                            if rs > 0 and d > 1 then
                                local lp = {}
                                lp[UnitOperationTypes.PARAM_X] = tx
                                lp[UnitOperationTypes.PARAM_Y] = ty
                                engOK = UnitManager.CanStartOperation(u, UnitOperationTypes.RANGE_ATTACK, nil, lp)
                            elseif simName ~= nil or civ ~= nil then
                                local ap = {}
                                ap[UnitOperationTypes.PARAM_X] = tx
                                ap[UnitOperationTypes.PARAM_Y] = ty
                                ap[UnitOperationTypes.PARAM_MODIFIERS] = UnitOperationMoveModifiers.ATTACK
                                    + UnitOperationMoveModifiers.MOVE_IGNORE_UNEXPLORED_DESTINATION
                                engOK = UnitManager.CanStartOperation(u, UnitOperationTypes.MOVE_TO, nil, ap)
                            end
                            if engOK then
                                if simName ~= nil then
                                    -- Engine combat estimate: same call the UI
                                    -- combat preview uses. eCombatType nil lets
                                    -- the engine pick the combat type - melee
                                    -- and theological sims come back with their
                                    -- COMBAT_TYPE filled in (CombatTypes.MELEE
                                    -- / RELIGIOUS / ...). Returns nil if the
                                    -- engine can't evaluate (busy/invalid); we
                                    -- then emit a target with zeroed estimate.
                                    local eCombatType = nil
                                    if rs > 0 and d > 1 then eCombatType = CombatTypes.RANGED end
                                    local eDD, eDA, eR, eTheo, eMods = 0, 0, false, false, nil
                                    pcall(function()
                                        local sim = CombatManager.SimulateAttackInto(u:GetComponentID(), eCombatType, tx, ty)
                                        if sim then
                                            local simAtt = sim[CombatResultParameters.ATTACKER]
                                            local simDef = sim[CombatResultParameters.DEFENDER]
                                            if simDef then eDD = simDef[CombatResultParameters.DAMAGE_TO] or 0 end
                                            if simAtt then eDA = simAtt[CombatResultParameters.DAMAGE_TO] or 0 end
                                            local ct = sim[CombatResultParameters.COMBAT_TYPE]
                                            eR = (ct == CombatTypes.RANGED or ct == CombatTypes.BOMBARD)
                                            eTheo = (ct == CombatTypes.RELIGIOUS)
                                            -- Collect the human-readable modifier
                                            -- descriptions the engine produces for
                                            -- each combatant (terrain, flanking,
                                            -- promotion, defenses, ...).
                                            local ptKeys = {
                                                "PREVIEW_TEXT_TERRAIN", "PREVIEW_TEXT_ASSIST",
                                                "PREVIEW_TEXT_PROMOTION", "PREVIEW_TEXT_DEFENSES",
                                                "PREVIEW_TEXT_HEALTH", "PREVIEW_TEXT_OPPONENT",
                                                "PREVIEW_TEXT_MODIFIER", "PREVIEW_TEXT_RESOURCES",
                                                "PREVIEW_TEXT_INTERCEPTOR", "PREVIEW_TEXT_ANTI_AIR",
                                            }
                                            local mods = {}
                                            for _, c in ipairs({simAtt, simDef}) do
                                                if c then
                                                    for _, pk in ipairs(ptKeys) do
                                                        local arr = c[CombatResultParameters[pk]]
                                                        if arr then
                                                            for _, s in ipairs(arr) do
                                                                local txt = tostring(s)
                                                                pcall(function() txt = tostring(Locale.Lookup(s)) end)
                                                                -- strip control tags ([COLOR_..]/[ENDCOLOR]/
                                                                -- [ICON_..]/[NEWLINE]) and the delimiters
                                                                -- used by the token format
                                                                txt = txt:gsub("%b[]", "")
                                                                          :gsub("[,;~|]", " ")
                                                                          :gsub("%s+", " ")
                                                                          :gsub("^%s", "")
                                                                          :gsub("%s$", "")
                                                                if txt ~= "" then table.insert(mods, txt) end
                                                            end
                                                        end
                                                    end
                                                end
                                            end
                                            if #mods > 0 then eMods = mods end
                                        end
                                    end)
                                    -- A religious attacker's target must resolve
                                    -- as THEOLOGICAL combat in the engine's own
                                    -- simulation; anything else (e.g. a
                                    -- non-combat religious unit that slipped the
                                    -- capability check above, or a busy engine)
                                    -- is not emitted.
                                    if not aIsReligious or eTheo then
                                        local kindStr = eTheo and "~kind:theological" or ""
                                        -- A civilian stacked with the defender is
                                        -- captured when a MELEE attack kills the
                                        -- escort (ranged kills do not capture).
                                        local capStr = ""
                                        if civ ~= nil and cs > 0 and d == 1 then
                                            capStr = "~captures:" .. civ
                                        end
                                        local modStr = ""
                                        if eMods and #eMods > 0 then modStr = "~m:" .. table.concat(eMods, ",") end
                                        table.insert(tgtList, simName .. "@" .. tx .. "," .. ty .. "~hp:" .. simHP .. "~dd:" .. eDD .. "~da:" .. eDA .. "~r:" .. (eR and "1" or "0") .. kindStr .. capStr .. modStr)
                                    end
                                elseif civ ~= nil and cs > 0 and d == 1 then
                                    -- Unescorted civilian adjacent to a melee-capable
                                    -- attacker: move onto the tile to capture it.
                                    -- No damage simulation - civilians aren't damage
                                    -- targets, and ranged units cannot target them.
                                    table.insert(tgtList, civ .. "@" .. tx .. "," .. ty .. "~hp:" .. civHP .. "~kind:capture")
                                end
                            end
                        end
                    end
                end
            end
            if #tgtList > 0 then targets = table.concat(tgtList, ";") end
        end
        -- Available promotions (InGame). Cheap gate first (xp >= next-level
        -- threshold), then ask the engine for the authoritative list via the
        -- same CanStartCommand(PROMOTE) call the UI uses. Returns promotion
        -- indices; we map to "TYPE~Name~Description" joined by ";".
        local promo = ""
        do
            local ok_exp, exp = pcall(function() return u:GetExperience() end)
            if ok_exp and exp then
                local ok_pc, pc = pcall(function() return entry and entry.PromotionClass or "" end)
                if ok_pc and pc and pc ~= "" then
                    local ok_xp, xp = pcall(function() return exp:GetExperiencePoints() end)
                    local ok_ne, need = pcall(function() return exp:GetExperienceForNextLevel() end)
                    if ok_xp and ok_ne and xp >= need then
                        pcall(function()
                            local bCan, tRes = UnitManager.CanStartCommand(u, UnitCommandTypes.PROMOTE, true, true)
                            if bCan and tRes then
                                local idxs = tRes[UnitCommandResults.PROMOTIONS]
                                if idxs then
                                    local parts = {}
                                    for _, pidx in pairs(idxs) do
                                        local pinfo = GameInfo.UnitPromotions[pidx]
                                        if pinfo then
                                            local pn = Locale.Lookup(pinfo.Name):gsub("[|;~]", " "):gsub("\\n", " ")
                                            local pd = Locale.Lookup(pinfo.Description):gsub("[|;~]", " "):gsub("\\n", " ")
                                            table.insert(parts, pinfo.UnitPromotionType .. "~" .. pn .. "~" .. pd)
                                        end
                                    end
                                    if #parts > 0 then promo = table.concat(parts, ";") end
                                end
                            end
                        end)
                    end
                end
            end
        end
        -- Upgrade info (InGame only: CanStartCommand)
        local canUp, upName, upCost = "0", "", "0"
        local ok1, _ = pcall(function()
            if UnitManager.CanStartCommand(u, UnitCommandTypes.UPGRADE, nil, true) then
                canUp = "1"
                local c2 = u:GetUpgradeCost()
                if c2 then upCost = tostring(c2) end
                if entry and entry.UpgradeUnitCollection then
                    for _, row in ipairs(entry.UpgradeUnitCollection) do
                        if row.UpgradeUnit then upName = row.UpgradeUnit end
                        break
                    end
                end
            end
        end)
        -- Builder improvement advisor (InGame only: CanStartOperation)
        local validImps = ""
        if ut == "UNIT_BUILDER" and u:GetMovesRemaining() > 0 then
            local plot = Map.GetPlot(x, y)
            if plot and plot:GetOwner() == id then
                local impList = {}
                for imp in GameInfo.Improvements() do
                    if imp.Buildable and not imp.TraitType then
                        local bParams = {}
                        bParams[UnitOperationTypes.PARAM_X] = x
                        bParams[UnitOperationTypes.PARAM_Y] = y
                        bParams[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
                        local ok2, _ = pcall(function()
                            if UnitManager.CanStartOperation(u, UnitOperationTypes.BUILD_IMPROVEMENT, nil, bParams) then
                                table.insert(impList, imp.ImprovementType)
                            end
                        end)
                    end
                end
                if #impList > 0 then validImps = table.concat(impList, ";") end
            end
        end
        -- Military Engineer advisor (BUILD_ROUTE + fort/airstrip)
        if ut == "UNIT_MILITARY_ENGINEER" and u:GetMovesRemaining() > 0 then
            local meList = {}
            pcall(function()
                local opRow = GameInfo.UnitOperations["UNITOPERATION_BUILD_ROUTE"]
                if opRow then
                    local rp = {}
                    rp[UnitOperationTypes.PARAM_X] = x
                    rp[UnitOperationTypes.PARAM_Y] = y
                    if UnitManager.CanStartOperation(u, opRow.Hash, nil, rp) then
                        table.insert(meList, "BUILD_ROUTE")
                    end
                end
            end)
            local plot = Map.GetPlot(x, y)
            if plot and plot:GetOwner() == id then
                for imp in GameInfo.Improvements() do
                    if imp.Buildable and not imp.TraitType then
                        pcall(function()
                            local bp = {}
                            bp[UnitOperationTypes.PARAM_X] = x
                            bp[UnitOperationTypes.PARAM_Y] = y
                            bp[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
                            if UnitManager.CanStartOperation(u, UnitOperationTypes.BUILD_IMPROVEMENT, nil, bp) then
                                table.insert(meList, imp.ImprovementType)
                            end
                        end)
                    end
                end
            end
            if #meList > 0 then validImps = table.concat(meList, ";") end
        end
        print(uid .. "|" .. nm .. "|" .. ut .. "|" .. x .. "," .. y .. "|" .. u:GetMovesRemaining() .. "/" .. u:GetMaxMoves() .. "|" .. (u:GetMaxDamage() - u:GetDamage()) .. "/" .. u:GetMaxDamage() .. "|" .. cs .. "|" .. rs .. "|" .. charges .. "|" .. targets .. "|" .. promo .. "|" .. canUp .. "|" .. upName .. "|" .. upCost .. "|" .. validImps .. "|" .. relName)
        -- Track tile occupancy + formation state (checked via game API, not heuristic)
        local key = x .. "," .. y
        if not tileUnits[key] then tileUnits[key] = {} end
        local inFormation = false
        pcall(function()
            inFormation = UnitManager.CanStartCommand(u, UnitCommandTypes.EXIT_FORMATION, id, true)
        end)
        table.insert(tileUnits[key], {id = uid, utype = ut, in_fm = inFormation})
    end
end
-- Detect formations: pair units on same tile where at least one is linked
for key, group in pairs(tileUnits) do
    if #group >= 2 then
        -- Pair each formation unit with every other formation unit on the same tile
        -- (when linked, both units return CanStartCommand(EXIT_FORMATION)=true)
        for _, a in ipairs(group) do
            if a.in_fm then
                for _, b in ipairs(group) do
                    if a.id ~= b.id and b.in_fm then
                        print("FORMATION|" .. a.id .. "|" .. b.id .. "|" .. b.utype:gsub("UNIT_", ""))
                    end
                end
            end
        end
    end
end
print("__MCP_SENTINEL_TAG__")
