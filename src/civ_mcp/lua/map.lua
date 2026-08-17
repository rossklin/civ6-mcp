local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local pTech = Players[me]:GetTechs()
local w, h = Map.GetGridSize()
local function resVisible(resEntry)
    if not resEntry.PrereqTech then return true end
    local t = GameInfo.Technologies[resEntry.PrereqTech]
    return t and pTech:HasTech(t.Index)
end

-- Pass 1: build enemy unit lookup table (avoid O(tiles*players*units))
local enemyLookup = {{}}
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
                local val = label .. " " .. ut:gsub("UNIT_", "") .. "(" .. hp .. "/" .. maxHp .. "hp)"
                if enemyLookup[key] then
                    enemyLookup[key] = enemyLookup[key] .. ";" .. val
                else
                    enemyLookup[key] = val
                end
            end
        end
    end
end

-- Pass 2: scan every tile, emit revealed ones
local seenRows = {}
local rowFirstTile = {{}}  -- first revealed tile per row (for parity check)
for y = 0, h - 1 do
    for x = 0, w - 1 do
        local plot = Map.GetPlot(x, y)
        if plot then
            local plotIdx = plot:GetIndex()
            local revealed = vis:IsRevealed(plotIdx)
            if revealed then
                if not seenRows[y] then
                    seenRows[y] = true
                    rowFirstTile[y] = {{x = x, y = y}}
                end
                local visible = vis:IsVisible(plotIdx)
                local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
                local hills = plot:IsHills() and "1" or "0"
                local river = plot:IsRiver() and "1" or "0"
                local coastal = plot:IsCoastalLand() and "1" or "0"
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
                local resource = "none"
                local resIdx = plot:GetResourceType()
                if resIdx >= 0 then
                    local resEntry = GameInfo.Resources[resIdx]
                    if resVisible(resEntry) then
                        resource = resEntry.ResourceType .. ":" .. (resEntry.ResourceClassType or "")
                    end
                end
                local imp = "none"
                local impIdx = plot:GetImprovementType()
                if impIdx >= 0 then
                    imp = GameInfo.Improvements[impIdx].ImprovementType
                    if plot:IsImprovementPillaged() then imp = imp .. ":PILLAGED" end
                end
                local freshWater = "0"
                local yields = "0,0,0,0,0,0"
                local unitStr = "none"
                local visTag = "revealed"
                if visible then
                    visTag = "visible"
                    freshWater = plot:IsFreshWater() and "1" or "0"
                    yields = plot:GetYield(0) .. "," .. plot:GetYield(1) .. "," .. plot:GetYield(2) .. "," .. plot:GetYield(3) .. "," .. plot:GetYield(4) .. "," .. plot:GetYield(5)
                    local key = x .. "," .. y
                    if enemyLookup[key] then
                        unitStr = enemyLookup[key]
                    end
                end
                -- Own units: always known regardless of visibility
                local myUnitStr = "none"
                local myParts = {{}}
                local myPlayerUnits = Players[me]:GetUnits()
                if myPlayerUnits then
                    for _, u in myPlayerUnits:Members() do
                        if u:GetX() == x and u:GetY() == y then
                            local entry = GameInfo.Units[u:GetType()]
                            local ut = entry and entry.UnitType or "UNKNOWN"
                            table.insert(myParts, (ut:gsub("UNIT_", "")))
                        end
                    end
                end
                if #myParts > 0 then myUnitStr = table.concat(myParts, ";") end
                local distName = "none"
                local distIdx = plot:GetDistrictType()
                if distIdx >= 0 then
                    local dInfo = GameInfo.Districts[distIdx]
                    if dInfo then distName = dInfo.DistrictType end
                end
                local routeType = -1
                pcall(function() routeType = plot:GetRouteType() end)
                local moveCost = 1
                if terrain ~= "TERRAIN_OCEAN" and terrain ~= "TERRAIN_COAST" then
                    if plot:IsHills() then moveCost = 2 end
                    if featureIdx >= 0 then
                        local fInfo = GameInfo.Features[featureIdx]
                        if fInfo and fInfo.MovementChange then
                            moveCost = moveCost + math.max(0, fInfo.MovementChange)
                        end
                    end
                    if routeType >= 0 then moveCost = 1 end
                end
                print(x .. "," .. y .. "|" .. terrain .. "|" .. feature .. "|" .. resource .. "|" .. hills .. "|" .. river .. "|" .. coastal .. "|" .. imp .. "|" .. owner .. "|" .. unitStr .. "|" .. visTag .. "|" .. freshWater .. "|" .. yields .. "|" .. distName .. "|" .. ownerName .. "|" .. myUnitStr .. "|" .. routeType .. "|" .. moveCost)
            end
        end
    end
end
-- Pass 3: determine row shift by calling GetAdjacentPlot on a tile in each row
-- dir 5 = NW. Use modulo to handle map wrapping at edges (cylindrical maps)
for y, tile in pairs(rowFirstTile) do
    for dx = 0, 10 do
        local tx = tile.x + dx
        local adj = Map.GetAdjacentPlot(tx, y, 5)
        if adj then
            local ax = adj:GetX()
            -- Normalize delta accounting for map wrap: (ax - tx + w) % w
            local d = (ax - tx + w) % w
            if d == 0 then
                print("ROWINFO|" .. y .. "|right")
            elseif d == w - 1 then
                print("ROWINFO|" .. y .. "|left")
            end
            break
        end
    end
end
