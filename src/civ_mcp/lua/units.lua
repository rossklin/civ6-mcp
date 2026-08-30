-- Units snapshot (InGame context).
--
-- Lists every local-player unit with upgrade, builder-improvement, formation,
-- and attack-target info. Output is pipe-delimited for parse_units_response
-- (NOT narrated prose) because the structured UnitInfo is consumed by the
-- turn-snapshot functions in game_state.py.
--
-- Attack-target estimates use the engine's own combat estimator
-- (CombatManager.SimulateAttackInto — the same call the UI combat preview
-- uses), which is authoritative for damage, modifiers, and combat type.

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
        -- Scan for attackable enemies if unit has moves
        local targets = ""
        if u:GetMovesRemaining() > 0 and (cs > 0 or rs > 0) then
            local rng = (rs > 0) and (entry and entry.Range or 1) or 1
            local tgtList = {}
            for dy = -rng, rng do
                for dx = -rng, rng do
                    local tx, ty = x + dx, y + dy
                    local d = Map.GetPlotDistance(x, y, tx, ty)
                    if d >= 1 and d <= rng then
                        local plotUnits = Map.GetUnitsAt(tx, ty)
                        if plotUnits then
                            for other in plotUnits:Units() do
                                local otherOwner = other:GetOwner()
                                if otherOwner ~= id and (otherOwner >= 62 or Players[id]:GetDiplomacy():IsAtWarWith(otherOwner)) then
                                    -- LOS check for ranged units (d>1): verify the
                                    -- game engine agrees we can actually fire there.
                                    -- Melee (d==1) doesn't need LOS.
                                    local losOK = true
                                    if rs > 0 and d > 1 then
                                        local lp = {}
                                        lp[UnitOperationTypes.PARAM_X] = tx
                                        lp[UnitOperationTypes.PARAM_Y] = ty
                                        losOK = UnitManager.CanStartOperation(u, UnitOperationTypes.RANGE_ATTACK, nil, lp)
                                    end
                                    if losOK then
                                        local eInfo = GameInfo.Units[other:GetType()]
                                        local eName = eInfo and eInfo.UnitType or "UNKNOWN"
                                        local eHP = other:GetMaxDamage() - other:GetDamage()
                                        -- Engine combat estimate: same call the UI
                                        -- combat preview uses. Returns nil if the
                                        -- engine can't evaluate (busy/invalid); we
                                        -- then emit a target with zeroed estimate.
                                        local eCombatType = nil
                                        if rs > 0 and d > 1 then eCombatType = CombatTypes.RANGED end
                                        local eDD, eDA, eR, eMods = 0, 0, false, nil
                                        pcall(function()
                                            local sim = CombatManager.SimulateAttackInto(u:GetComponentID(), eCombatType, tx, ty)
                                            if sim then
                                                local att = sim[CombatResultParameters.ATTACKER]
                                                local def = sim[CombatResultParameters.DEFENDER]
                                                if def then eDD = def[CombatResultParameters.DAMAGE_TO] or 0 end
                                                if att then eDA = att[CombatResultParameters.DAMAGE_TO] or 0 end
                                                local ct = sim[CombatResultParameters.COMBAT_TYPE]
                                                eR = (ct == CombatTypes.RANGED or ct == CombatTypes.BOMBARD)
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
                                                for _, c in ipairs({att, def}) do
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
                                        local modStr = ""
                                        if eMods and #eMods > 0 then modStr = "~m:" .. table.concat(eMods, ",") end
                                        table.insert(tgtList, eName .. "@" .. tx .. "," .. ty .. "~hp:" .. eHP .. "~dd:" .. eDD .. "~da:" .. eDA .. "~r:" .. (eR and "1" or "0") .. modStr)
                                    end
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
