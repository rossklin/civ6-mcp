-- Units snapshot (InGame context).
--
-- Lists every local-player unit with upgrade, builder-improvement, formation,
-- attack-target, and available-action info. Output is pipe-delimited for
-- parse_units_response (NOT narrated prose) because the structured UnitInfo
-- is consumed by the turn-snapshot functions in game_state.py. Per-unit
-- fields go on the UNITS| line; multi-value relations (FORMATION|, UACTION|)
-- are separate lines parsed in later passes.
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
-- Available unit actions (second pass, separate UACTION| lines parsed by
-- parse_units_response). Mirrors UnitPanel.GetUnitActionsTable exactly:
-- the loose check ("could the unit ever do this") decides whether the
-- action is LISTED at all, the strict check decides enabled vs disabled,
-- and the strict check's failure reasons — the same strings the UI shows
-- on a greyed-out button's tooltip — are carried alongside. A disabled
-- entry tells the agent the action exists and what prerequisite is
-- missing (e.g. FORM_CORPS "requires Nationalism", ACTIVATE_GREAT_PERSON
-- "must be on a completed Industrial Zone"), matching what a human reads
-- off the greyed buttons.
--
-- Actions owned by dedicated commands are excluded:
--   MOVE_TO / RANGE_ATTACK / AIR_ATTACK  (move_unit / attack_unit; the
--     targets column above already carries combat predictions)
--   FOUND_CITY (found_city — verification + advisor fallback)
--   FOUND_RELIGION / EVANGELIZE_BELIEF (religion-chooser flows; prophets
--     found via the found_religion command)
--   MAKE_TRADE_ROUTE (make_trade_route)
--   SKIP_TURN (skip_unit uses GameCore FinishMoves)
--   NAME_UNIT (text input, not exposed)
-- ACTIVATE_GREAT_PERSON is additionally skipped for Great Prophets —
-- their founding path is the found_religion command, and the generic
-- activation would only produce a confusing engine error.
--
-- Exceptions to the disabled model (listed only when actually startable,
-- to avoid noise): PROMOTE (the available_promotions column carries both
-- the choices and the gating), OFFENSIVESPY missions (a spy away from a
-- valid city would otherwise list all eight missions), and WMD_STRIKE
-- (the per-type fan-out is the availability signal).
--
-- Line format: UACTION|unit_id|action_id|category|needs|disabled|detail|reasons
--   needs: none | plot | unit | improvement | promotion | wmd — derived
--     the way the UI dispatches (DB InterfaceMode column + the
--     special-cased fans below); plot => x,y params, unit =>
--     target_unit_id, improvement => improvement, promotion =>
--     promotion_type, wmd => wmd_type + x,y
--   disabled: 0 = startable now, 1 = listed but not startable (reasons
--     hold the engine's failure strings, semicolon-joined)
--   detail: partner unit ids (ENTER_FORMATION, FORM_CORPS/FORM_ARMY),
--     WMD types (WMD_STRIKE)
--
-- Commands are listed regardless of movement; operations only when moves
-- remain (UnitPanel gates operations the same way).
local DENY_ACTIONS = {
    UNITOPERATION_MOVE_TO = true,
    UNITOPERATION_RANGE_ATTACK = true,
    UNITOPERATION_AIR_ATTACK = true,
    UNITOPERATION_FOUND_CITY = true,
    UNITOPERATION_FOUND_RELIGION = true,
    UNITOPERATION_EVANGELIZE_BELIEF = true,
    UNITOPERATION_MAKE_TRADE_ROUTE = true,
    UNITOPERATION_SKIP_TURN = true,
    UNITCOMMAND_NAME_UNIT = true,
}
local function emitUAction(uid, row, actionId, needs, detail, disabled, reasons)
    local line = "UACTION|" .. uid .. "|" .. actionId
        .. "|" .. (row.CategoryInUI or "SPECIFIC") .. "|" .. needs
        .. "|" .. (disabled and "1" or "0")
        .. "|" .. (detail or "")
    if reasons and reasons ~= "" then line = line .. "|" .. reasons end
    print(line)
end
-- Localize and scrub an engine failure string for the pipe-delimited
-- reply (Locale.Lookup is nil-guarded — it raises on nil, and LOC keys
-- may be absent for mod-added rows).
local function cleanReason(s)
    if s == nil then return "" end
    local t = tostring(s)
    pcall(function() t = tostring(Locale.Lookup(s)) end)
    t = t:gsub("%b[]", "")
         :gsub("[,;|~]", " ")
         :gsub("%s+", " ")
         :gsub("^%s+", "")
         :gsub("%s+$", "")
    if #t > 80 then t = t:sub(1, 80) .. "..." end
    return t
end
-- Extract up to two cleaned failure reasons from a strict-check results
-- table. Handles both shapes the engine produces: the FAILURE_REASONS
-- array (operations) and nested string lists (commands).
local function strictReasons(tRes)
    if tRes == nil then return "" end
    local out = {}
    local arr = tRes[UnitOperationResults.FAILURE_REASONS]
    if type(arr) == "table" then
        for _, r in ipairs(arr) do
            local c = cleanReason(r)
            if c ~= "" then table.insert(out, c) end
        end
    end
    for _, v in pairs(tRes) do
        if type(v) == "table" and v ~= arr then
            for _, s in pairs(v) do
                if type(s) == "string" and s ~= "" then
                    local c = cleanReason(s)
                    if c ~= "" then table.insert(out, c) end
                end
            end
        end
    end
    while #out > 2 do table.remove(out) end
    if #out == 0 then return "" end
    return table.concat(out, "; ")
end
local function uactionNeeds(row, h, isCommand)
    if h == UnitOperationTypes.BUILD_IMPROVEMENT then return "improvement"
    elseif isCommand and h == UnitCommandTypes.PROMOTE then return "promotion"
    elseif isCommand and (h == UnitCommandTypes.ENTER_FORMATION
        or h == UnitCommandTypes.FORM_CORPS
        or h == UnitCommandTypes.FORM_ARMY) then return "unit"
    elseif h == UnitOperationTypes.WMD_STRIKE then return "wmd"
    elseif row.InterfaceMode ~= nil and row.InterfaceMode ~= "" then return "plot"
    else return "none" end
end
for _, u2 in Players[id]:GetUnits():Members() do
    local x2 = u2:GetX()
    if x2 ~= -9999 then
        local uid2 = u2:GetID()
        local uEntry = GameInfo.Units[u2:GetType()]
        local isProphet = uEntry ~= nil and uEntry.UnitType == "UNIT_GREAT_PROPHET"
        -- Commands (listed regardless of movement)
        for row in GameInfo.UnitCommands() do
            if row.VisibleInUI and not DENY_ACTIONS[row.CommandType]
                and not (isProphet and row.Hash == UnitCommandTypes.ACTIVATE_GREAT_PERSON) then
                local h = row.Hash
                if h == UnitCommandTypes.ENTER_FORMATION then
                    -- One action with the valid partner ids in detail (the
                    -- UI shows one button per partner from the same results)
                    pcall(function()
                        local bCan, tRes = UnitManager.CanStartCommand(u2, h, nil, true)
                        local partners = (bCan and tRes)
                            and tRes[UnitCommandResults.UNITS] or nil
                        if bCan and partners ~= nil and #partners > 0 then
                            local ids = {}
                            for _, p in ipairs(partners) do
                                table.insert(ids, tostring(p.id))
                            end
                            emitUAction(uid2, row, row.CommandType, "unit",
                                "partners:" .. table.concat(ids, ","), false, nil)
                        elseif UnitManager.CanStartCommand(u2, h, true) then
                            local _, tRes2 = UnitManager.CanStartCommand(u2, h, false, true)
                            emitUAction(uid2, row, row.CommandType, "unit",
                                nil, true, strictReasons(tRes2))
                        end
                    end)
                elseif h == UnitCommandTypes.FORM_CORPS
                    or h == UnitCommandTypes.FORM_ARMY then
                    -- Corps/army (and fleet/armada — the same two commands
                    -- on naval units; the UI only swaps the tooltip text by
                    -- domain). Partner enumeration goes through
                    -- GetCommandTargets, the same call the UI's form-corps
                    -- targeting overlay uses (WorldInput.lua); the
                    -- CanStartCommand-results UNITS list is only filled for
                    -- ENTER_FORMATION.
                    pcall(function()
                        local bNow, tRes = UnitManager.CanStartCommand(u2, h, false, true)
                        if bNow then
                            local ids = {}
                            local t = UnitManager.GetCommandTargets(u2, h)
                            if t ~= nil and t[UnitCommandResults.UNITS] ~= nil then
                                for _, cid in ipairs(t[UnitCommandResults.UNITS]) do
                                    table.insert(ids, tostring(cid.id))
                                end
                            end
                            local detail = nil
                            if #ids > 0 then
                                detail = "partners:" .. table.concat(ids, ",")
                            end
                            emitUAction(uid2, row, row.CommandType, "unit",
                                detail, false, nil)
                        elseif UnitManager.CanStartCommand(u2, h, true) then
                            emitUAction(uid2, row, row.CommandType, "unit",
                                nil, true, strictReasons(tRes))
                        end
                    end)
                elseif h == UnitCommandTypes.PROMOTE then
                    -- Listed only when a promotion is actually takeable; the
                    -- available_promotions column carries the choices
                    pcall(function()
                        local bCan, tRes = UnitManager.CanStartCommand(u2, h, true, true)
                        if bCan and tRes then
                            local promos = tRes[UnitCommandResults.PROMOTIONS]
                            if promos ~= nil and #promos > 0 then
                                emitUAction(uid2, row, row.CommandType,
                                    "promotion", nil, false, nil)
                            end
                        end
                    end)
                else
                    -- Generic two-phase check, exactly like the UI: loose
                    -- ("could the unit ever") decides listing, strict
                    -- ("right now") decides enabled vs disabled+reasons
                    pcall(function()
                        if UnitManager.CanStartCommand(u2, h, true) then
                            local bNow, tRes = UnitManager.CanStartCommand(u2, h, false, true)
                            if bNow then
                                emitUAction(uid2, row, row.CommandType,
                                    uactionNeeds(row, h, true), nil, false, nil)
                            else
                                emitUAction(uid2, row, row.CommandType,
                                    uactionNeeds(row, h, true), nil, true,
                                    strictReasons(tRes))
                            end
                        end
                    end)
                end
            end
        end
        -- Operations (only while movement remains, mirroring UnitPanel)
        if u2:GetMovesRemaining() > 0 then
            for row in GameInfo.UnitOperations() do
                if row.VisibleInUI and not DENY_ACTIONS[row.OperationType] then
                    local h = row.Hash
                    if h == UnitOperationTypes.BUILD_IMPROVEMENT then
                        -- Loose check with the unit's own plot, like the UI;
                        -- the IMPROVEMENTS result list says the unit could
                        -- build here at all (the valid_improvements column
                        -- lists them by name), then a strict check with the
                        -- engine's own best pick gates it (e.g. no moves
                        -- left) and supplies failure reasons.
                        pcall(function()
                            local bp = {}
                            bp[UnitOperationTypes.PARAM_X] = x2
                            bp[UnitOperationTypes.PARAM_Y] = u2:GetY()
                            local bCan, tRes = UnitManager.CanStartOperation(u2, h, nil, bp, true)
                            local imps = (bCan and tRes ~= nil)
                                and tRes[UnitOperationResults.IMPROVEMENTS] or nil
                            if bCan and imps ~= nil and #imps > 0 then
                                local best = tRes[UnitOperationResults.BEST_IMPROVEMENT]
                                if best == nil or best == -1 then best = imps[1] end
                                local sp = {}
                                sp[UnitOperationTypes.PARAM_X] = x2
                                sp[UnitOperationTypes.PARAM_Y] = u2:GetY()
                                sp[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = best
                                local bNow, tRes2 = UnitManager.CanStartOperation(u2, h, nil, sp, true)
                                emitUAction(uid2, row, row.OperationType,
                                    "improvement", nil, not bNow, strictReasons(tRes2))
                            end
                        end)
                    elseif row.CategoryInUI == "OFFENSIVESPY" then
                        -- Spy missions: strict-only gate (the UI's espionage
                        -- branch) so a spy away from a valid city doesn't
                        -- list every mission
                        pcall(function()
                            if UnitManager.CanStartOperation(u2, h, nil, false, false) then
                                emitUAction(uid2, row, row.OperationType,
                                    "plot", nil, false, nil)
                            end
                        end)
                    elseif h == UnitOperationTypes.WMD_STRIKE then
                        -- One action; detail lists the startable WMD types
                        -- (the UI fans out one button per type)
                        pcall(function()
                            if UnitManager.CanStartOperation(u2, h, nil, true) then
                                local okTypes = {}
                                for entry in GameInfo.WMDs() do
                                    local wp = {}
                                    wp[UnitOperationTypes.PARAM_WMD_TYPE] = entry.Index
                                    -- Same call shape as UnitPanel's WMD fan-out
                                    if UnitManager.CanStartOperation(u2, h, nil, wp, true) then
                                        table.insert(okTypes, entry.WMDType or entry.Type or "?")
                                    end
                                end
                                if #okTypes > 0 then
                                    emitUAction(uid2, row, row.OperationType, "wmd",
                                        "types:" .. table.concat(okTypes, ","), false, nil)
                                end
                            end
                        end)
                    else
                        -- Generic two-phase check like the UI. Strict pass
                        -- hints NO_TARGETS — we don't consume target lists
                        -- here (the executor re-checks with real params).
                        pcall(function()
                            if UnitManager.CanStartOperation(u2, h, nil, true) then
                                local bNow, tRes = UnitManager.CanStartOperation(u2, h, nil, false,
                                        OperationResultsTypes.NO_TARGETS)
                                if bNow then
                                    emitUAction(uid2, row, row.OperationType,
                                        uactionNeeds(row, h, false), nil, false, nil)
                                else
                                    emitUAction(uid2, row, row.OperationType,
                                        uactionNeeds(row, h, false), nil, true,
                                        strictReasons(tRes))
                                end
                            end
                        end)
                    end
                end
            end
        end
    end
end
print("__MCP_SENTINEL_TAG__")
