local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local pTech = Players[me]:GetTechs()
local w, h = Map.GetGridSize()

local function resVisible(resEntry)
    if not resEntry.PrereqTech then return true end
    local t = GameInfo.Technologies[resEntry.PrereqTech]
    return t and pTech:HasTech(t.Index)
end

-- strip a literal prefix token (e.g. "TERRAIN_") from a type string
local function strip(s, prefix)
    return (s:gsub(prefix, ""))
end

-- Build enemy unit lookup table (avoid O(tiles*players*units))
-- Values are joined with ", " to match narrate_map's unit formatting.
local enemyLookup = {}
for i = 0, 63 do
    if i ~= me and Players[i] and Players[i]:IsAlive() then
        local units = Players[i]:GetUnits()
        if units then
            for _, u in units:Members() do
                local key = u:GetX() .. "," .. u:GetY()
                local entry = GameInfo.Units[u:GetType()]
                local ut = entry and entry.UnitType or "UNKNOWN"
                local label = ""
                if i == 63 then label = "Barbarian"
                else
                    local oCfg = PlayerConfigurations[i]
                    if oCfg then
                        label = Locale.Lookup(oCfg:GetCivilizationShortDescription())
                    end
                end
                local hp = u:GetMaxDamage() - u:GetDamage()
                local maxHp = u:GetMaxDamage()
                local val = label .. " " .. strip(ut, "UNIT_") .. "(" .. hp .. "/" .. maxHp .. "hp)"
                if enemyLookup[key] then
                    enemyLookup[key] = enemyLookup[key] .. ", " .. val
                else
                    enemyLookup[key] = val
                end
            end
        end
    end
end

-- Own units lookup (always known regardless of visibility)
local myLookup = {}
local myPlayerUnits = Players[me]:GetUnits()
if myPlayerUnits then
    for _, u in myPlayerUnits:Members() do
        local key = u:GetX() .. "," .. u:GetY()
        local entry = GameInfo.Units[u:GetType()]
        local ut = entry and entry.UnitType or "UNKNOWN"
        ut = strip(ut, "UNIT_")
        if myLookup[key] then
            myLookup[key] = myLookup[key] .. ", " .. ut
        else
            myLookup[key] = ut
        end
    end
end

-- Scan every tile, format each revealed one as a narrate_map line.
local lines = {}
for y = h - 1, 0, -1 do
    for x = 0, w - 1 do
        local plot = Map.GetPlot(x, y)
        if plot then
            local plotIdx = plot:GetIndex()
            if vis:IsRevealed(plotIdx) then
                local visible = vis:IsVisible(plotIdx)
                local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
                local isHills = plot:IsHills()
                local isRiver = plot:IsRiver()
                local isCoastal = plot:IsCoastalLand()
                local owner = plot:GetOwner()

                local ownerName = ""
                if owner >= 0 then
                    local cfg = PlayerConfigurations[owner]
                    local p = Players[owner]
                    if cfg and p and p:IsAlive() then
                        ownerName = Locale.Lookup(cfg:GetCivilizationShortDescription())
                        if not p:IsMajor() then ownerName = ownerName .. ":CS" end
                    elseif owner == 63 then
                        ownerName = "Barbarian"
                    end
                end

                local featureIdx = plot:GetFeatureType()
                local feature = "none"
                if featureIdx >= 0 then feature = GameInfo.Features[featureIdx].FeatureType end

                local resIdx = plot:GetResourceType()
                local resEntry = nil
                if resIdx >= 0 then resEntry = GameInfo.Resources[resIdx] end

                local impIdx = plot:GetImprovementType()
                local imp = "none"
                if impIdx >= 0 then imp = GameInfo.Improvements[impIdx].ImprovementType end

                local distIdx = plot:GetDistrictType()
                local distName = "none"
                if distIdx >= 0 then
                    local dInfo = GameInfo.Districts[distIdx]
                    if dInfo then distName = dInfo.DistrictType end
                end

                local routeType = -1
                pcall(function() routeType = plot:GetRouteType() end)

                local moveCost = 1
                if terrain ~= "TERRAIN_OCEAN" and terrain ~= "TERRAIN_COAST" then
                    if isHills then moveCost = 2 end
                    if featureIdx >= 0 then
                        local fInfo = GameInfo.Features[featureIdx]
                        if fInfo and fInfo.MovementChange then
                            moveCost = moveCost + math.max(0, fInfo.MovementChange)
                        end
                    end
                    if routeType >= 0 then moveCost = 1 end
                end

                -- Build the space-joined descriptor parts (order matches _format_tile)
                local parts = {}
                table.insert(parts, strip(terrain, "TERRAIN_"))
                if isHills then table.insert(parts, "Hills") end
                if featureIdx >= 0 then table.insert(parts, strip(feature, "FEATURE_")) end
                if resEntry and resVisible(resEntry) then
                    local resLabel = strip(resEntry.ResourceType, "RESOURCE_")
                    local cls = resEntry.ResourceClassType or ""
                    if cls == "RESOURCECLASS_STRATEGIC" then resLabel = resLabel .. "*"
                    elseif cls == "RESOURCECLASS_LUXURY" then resLabel = resLabel .. "+"
                    end
                    table.insert(parts, "[" .. resLabel .. "]")
                end
                if isRiver then table.insert(parts, "River") end
                if isCoastal then table.insert(parts, "Coast") end
                if visible and plot:IsFreshWater() and not isRiver then
                    table.insert(parts, "FreshWater")
                end
                if impIdx >= 0 then
                    local impLabel = strip(imp, "IMPROVEMENT_")
                    if plot:IsImprovementPillaged() then impLabel = impLabel .. " PILLAGED" end
                    table.insert(parts, "(" .. impLabel .. ")")
                end
                if routeType >= 0 then
                    local routeName = "Railroad" -- routeType == 4
                    if routeType ~= 4 then routeName = "Road" end
                    table.insert(parts, "(" .. routeName .. ")")
                end
                if distIdx >= 0 and distName ~= "none" then
                    table.insert(parts, "[" .. strip(distName, "DISTRICT_") .. "]")
                end
                if visible then
                    local f = plot:GetYield(0)
                    local p = plot:GetYield(1)
                    local g = plot:GetYield(2)
                    local s = plot:GetYield(3)
                    local c = plot:GetYield(4)
                    local fa = plot:GetYield(5)
                    local ys = "F:" .. f .. " P:" .. p
                    if g > 0 then ys = ys .. " G:" .. g end
                    if s > 0 then ys = ys .. " S:" .. s end
                    if c > 0 then ys = ys .. " C:" .. c end
                    if fa > 0 then ys = ys .. " Fa:" .. fa end
                    table.insert(parts, "{" .. ys .. "}")
                end

                -- Owner annotation
                local ownerStr = ""
                if owner >= 0 then
                    if ownerName ~= "" then
                        local label = (ownerName:gsub(":CS", " [City-State]"))
                        ownerStr = " (owned by " .. label .. ")"
                    else
                        ownerStr = " (owned by player " .. owner .. ")"
                    end
                end

                -- Visibility / movement / unit annotations
                local visTag = ""
                if not visible then visTag = " [fog]" end

                local mvStr = ""
                if moveCost > 1 then mvStr = " [mv:" .. moveCost .. "]" end

                local key = x .. "," .. y
                local ownStr = ""
                if myLookup[key] then ownStr = " [my: " .. myLookup[key] .. "]" end

                local unitStr = ""
                if visible and enemyLookup[key] then
                    unitStr = " **[" .. enemyLookup[key] .. "]**"
                end

                -- Neighbour tiles
                local dirNames = {"NE","E","SE","SW","W","NW"}
                local neighbours = {}
                for i = 0,5 do
                    local adj = Map.GetAdjacentPlot(x, y, i)
                    if adj then
                        local strbuf = dirNames[i+1] .. " (" .. adj:GetX() .. "," .. adj:GetY() .. ")"
                        if (adj:IsRiverCrossingToPlot(plot)) then strbuf = strbuf .. " RC" end
                        neighbours[#neighbours+1] = strbuf
                    end
                end

                lines[#lines + 1] = "  (" .. x .. "," .. y .. "): " .. table.concat(parts, " ") .. ownerStr .. visTag .. mvStr .. ownStr .. unitStr .. ", neighbours: " .. table.concat(neighbours, ", ")
            end
        end
    end
end

if #lines == 0 then
    print("No tiles.")
else
    print(#lines .. " tiles:")
    print(table.concat(lines, "\n"))
end
