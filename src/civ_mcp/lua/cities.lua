-- city production
local function CityProductionOptions(cityId)
    -- Section headings in display order, each with its column header.
    -- Headings and headers are emitted only for sections that collect items,
    -- so empty sections are dropped from the output entirely.
    local sections = {
        {key="UNITS:",              header="type | production cost | turns to build | gold cost"},
        {key="BUILDINGS:",          header="type | production cost | turns to build | gold cost"},
        {key="DISTRICTS:",          header="type | production cost | turns to build | gold cost"},
        {key="PROJECTS:",           header="type | production cost | turns to build | gold cost"},
        {key="REPAIRS (DISTRICTS)", header="type | production cost | turns to build"},
        {key="REPAIRS (BUILDINGS)", header="type | production cost | turns to build"},
    }
    local output = {}  -- key -> list of item lines
    local function registerOutput(k, v)
        if output[k] == nil then output[k] = {} end
        table.insert(output[k], v)
    end

    local me = Game.GetLocalPlayer()
    local pCity = CityManager.GetCity(me, cityId)
    if pCity == nil then return "ERROR city " .. cityId .. " does not exist" end

    local bq = pCity:GetBuildQueue()
    local goldIdx = GameInfo.Yields["YIELD_GOLD"].Index
    local cityGold = pCity:GetGold()
    local function getGoldCost(hash, isUnit)
        local ok, cost = pcall(function()
            if isUnit then
                return cityGold:GetPurchaseCost(goldIdx, hash, MilitaryFormationTypes.STANDARD_MILITARY_FORMATION)
            else
                return cityGold:GetPurchaseCost(goldIdx, hash, -1)
            end
        end)
        if ok and cost and cost > 0 then return math.floor(cost) end
        return -1
    end
    -- Check Trader cap: game silently rejects Traders when count >= route capacity
    local pTrade = Players[me]:GetTrade()
    local traderCount = 0
    for _, u in Players[me]:GetUnits():Members() do
        if GameInfo.Units[u:GetType()].UnitType == "UNIT_TRADER" then traderCount = traderCount + 1 end
    end
    local routeCap = pTrade:GetOutgoingRouteCapacity()
    local traderCapped = (traderCount >= routeCap)
    for unit in GameInfo.Units() do
        if bq:CanProduce(unit.Hash, true) then
            if unit.UnitType == "UNIT_TRADER" and traderCapped then
                -- skip: game will silently reject (traders >= route capacity)
            else
                -- CanStartOperation catches missing strategic resources that CanProduce misses
                local unitCheck = {}
                unitCheck[CityOperationTypes.PARAM_UNIT_TYPE] = unit.Hash
                local canStart = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, unitCheck, true)
                if canStart then
                    local t = bq:GetTurnsLeft(unit.Hash)
                    local gc = getGoldCost(unit.Hash, true)
                    local adjCost = unit.Cost
                    pcall(function() local c = bq:GetProductionCost(unit.Hash); if c > 0 then adjCost = math.floor(c) end end)
                    registerOutput("UNITS:", unit.UnitType .. "|" .. adjCost .. "|" .. t .. "|" .. gc)
                end
            end
        end
    end
    for bldg in GameInfo.Buildings() do
        if bq:CanProduce(bldg.Hash, true) then
            -- CanStartOperation catches pillaged-district prerequisites that CanProduce misses
            local bldgCheck = {}
            bldgCheck[CityOperationTypes.PARAM_BUILDING_TYPE] = bldg.Hash
            local canStart = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, bldgCheck, true)
            if canStart then
                local t = bq:GetTurnsLeft(bldg.Hash)
                local gc = -1
                if not bldg.IsWonder then
                    gc = getGoldCost(bldg.Hash, false)
                end
                local adjCost = bldg.Cost
                pcall(function() local c = bq:GetProductionCost(bldg.Hash); if c > 0 then adjCost = math.floor(c) end end)
                registerOutput("BUILDINGS:", bldg.BuildingType .. "|" .. adjCost .. "|" .. t .. "|" .. gc)
            end
        end
    end
    for dist in GameInfo.Districts() do
        if bq:CanProduce(dist.Hash, true) then
            local t = bq:GetTurnsLeft(dist.Hash)
            local adjCost = dist.Cost
            pcall(function() local c = bq:GetProductionCost(dist.Hash); if c > 0 then adjCost = math.floor(c) end end)
            registerOutput("DISTRICTS:", dist.DistrictType .. "|" .. adjCost .. "|" .. t .. "|-1")
        end
    end
    for proj in GameInfo.Projects() do
        if bq:CanProduce(proj.Hash, true) then
            local t = bq:GetTurnsLeft(proj.Hash)
            local adjCost = proj.Cost
            pcall(function() local c = bq:GetProductionCost(proj.Hash); if c > 0 then adjCost = math.floor(c) end end)
            registerOutput("PROJECTS:", proj.ProjectType .. "|" .. adjCost .. "|" .. t .. "|-1")
        end
    end
    -- Pillaged districts/buildings that can be repaired via production queue
    local pBuildings = pCity:GetBuildings()
    for _, d in pCity:GetDistricts():Members() do
        if d:IsPillaged() then
            local dInfo = GameInfo.Districts[d:GetType()]
            if dInfo and dInfo.DistrictType ~= "DISTRICT_CITY_CENTER" then
                local repParams = {}
                repParams[CityOperationTypes.PARAM_DISTRICT_TYPE] = dInfo.Hash
                repParams[CityOperationTypes.PARAM_X] = d:GetX()
                repParams[CityOperationTypes.PARAM_Y] = d:GetY()
                local canRepair = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, repParams, true)
                if canRepair then
                    local t = bq:GetTurnsLeft(dInfo.Hash)
                    local adjCost = dInfo.Cost
                    pcall(function() local c = bq:GetProductionCost(dInfo.Hash); if c > 0 then adjCost = math.floor(c) end end)
                    registerOutput("REPAIRS (DISTRICTS)", dInfo.DistrictType .. "|" .. adjCost .. "|" .. t)
                end
            end
        end
    end
    for bldg in GameInfo.Buildings() do
        if pBuildings:HasBuilding(bldg.Index) and pBuildings:IsPillaged(bldg.Index) then
            local repCheck = {}
            repCheck[CityOperationTypes.PARAM_BUILDING_TYPE] = bldg.Hash
            local canRepair = CityManager.CanStartOperation(pCity, CityOperationTypes.BUILD, repCheck, true)
            if canRepair then
                local t = bq:GetTurnsLeft(bldg.Hash)
                local adjCost = bldg.Cost
                pcall(function() local c = bq:GetProductionCost(bldg.Hash); if c > 0 then adjCost = math.floor(c) end end)
                registerOutput("REPAIRS (BUILDINGS)", bldg.BuildingType .. "|" .. adjCost .. "|" .. t)
            end
        end
    end

    -- Assemble: for each section in order, emit heading + column header + items,
    -- but only if the section collected at least one item.
    local parts = {}
    for _, sec in ipairs(sections) do
        local items = output[sec.key]
        if items and #items > 0 then
            table.insert(parts, sec.key)
            table.insert(parts, sec.header)
            for _, line in ipairs(items) do
                table.insert(parts, line)
            end
        end
    end
    return table.concat(parts, "\n")
end

-- Cities
local me = Game.GetLocalPlayer()
local hashName = {}
for u in GameInfo.Units() do hashName[u.Hash] = u.UnitType end
for b in GameInfo.Buildings() do hashName[b.Hash] = b.BuildingType end
for d in GameInfo.Districts() do hashName[d.Hash] = d.DistrictType end
for p in GameInfo.Projects() do hashName[p.Hash] = p.ProjectType end
local cityCoords = {}
for i, c in Players[me]:GetCities():Members() do
    local nm = Locale.Lookup(c:GetName()):gsub("|", "/")
    local bq = c:GetBuildQueue()
    local producing = "nothing"
    local turnsLeft = 0
    if bq:GetSize() > 0 then
        local h = bq:GetCurrentProductionTypeHash()
        if h == 0 then
            -- Ghost entry (Babylon eureka can obsolete queued items).
            -- Try to clear it so the city reports as idle.
            pcall(function() bq:RemoveAt(0) end)
            producing = "nothing"
        else
            producing = hashName[h] or "UNKNOWN"
        end
        turnsLeft = bq:GetTurnsLeft()
    end
    local g = c:GetGrowth()
    local amNeed = 0
    pcall(function() amNeed = g:GetAmenitiesNeeded() end)
    local amTotal = amNeed + g:GetAmenities()
    -- City defense info
    local defStr, garHP, garMax, wallHP, wallMax = 0, 0, 0, 0, 0
    local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
    for _, d in c:GetDistricts():Members() do
        if d:GetType() == ccIdx then
            local ok, _ = pcall(function()
                defStr = d:GetDefenseStrength() or 0
                garMax = d:GetMaxDamage(DefenseTypes.DISTRICT_GARRISON) or 0
                garHP = garMax - (d:GetDamage(DefenseTypes.DISTRICT_GARRISON) or 0)
                wallMax = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER) or 0
                wallHP = wallMax - (d:GetDamage(DefenseTypes.DISTRICT_OUTER) or 0)
            end)
            break
        end
    end
    local cityTargets = {}
    if wallMax > 0 then
        local cx, cy = c:GetX(), c:GetY()
        for dy = -3, 3 do for dx = -3, 3 do
            local tx, ty = cx + dx, cy + dy
            local d = Map.GetPlotDistance(cx, cy, tx, ty)
            if d >= 1 and d <= 3 then
                local pu = Map.GetUnitsAt(tx, ty)
                if pu then for other in pu:Units() do
                    if other:GetOwner() ~= me then
                        local eInfo = GameInfo.Units[other:GetType()]
                        local eName = eInfo and eInfo.UnitType or "UNKNOWN"
                        local eHP = other:GetMaxDamage() - other:GetDamage()
                        table.insert(cityTargets, eName .. "@" .. tx .. "," .. ty .. "(" .. eHP .. "hp)")
                    end
                end end
            end
        end end
    end
    local distLocs = {}
    for _, d in c:GetDistricts():Members() do
        local dInfo = GameInfo.Districts[d:GetType()]
        if dInfo and dInfo.DistrictType ~= "DISTRICT_CITY_CENTER" then
            table.insert(distLocs, dInfo.DistrictType .. "@" .. d:GetX() .. "," .. d:GetY())
        end
    end
    local allBuildings = {}
    local pBuildings = c:GetBuildings()
    for bldg in GameInfo.Buildings() do
        if pBuildings:HasBuilding(bldg.Index) then
            table.insert(allBuildings, (bldg.BuildingType:gsub("BUILDING_", "")))
        end
    end
    
    table.insert(cityCoords, {name=nm, x=c:GetX(), y=c:GetY()})
    local loy, loyMax, loyPT, loyFlip = 100, 100, 0, 0
    local cult = c:GetCulturalIdentity()
    if cult then
        loy = cult:GetLoyalty()
        loyMax = cult:GetMaxLoyalty()
        loyPT = cult:GetLoyaltyPerTurn()
        loyFlip = cult:GetTurnsToConversion()
    end
    local garrisonUnit = ""
    local garFound = false
    local garUnitsAt = Map.GetUnitsAt(c:GetX(), c:GetY())
    if garUnitsAt then
        for gu in garUnitsAt:Units() do
            if not garFound and gu:GetOwner() == me then
                local guInfo = GameInfo.Units[gu:GetType()]
                if guInfo then
                    local fc = guInfo.FormationClass
                    if fc == "FORMATION_CLASS_LAND_COMBAT" or fc == "FORMATION_CLASS_NAVAL_COMBAT" then
                        garrisonUnit = guInfo.UnitType
                        garFound = true
                    end
                end
            end
        end
    end

    local baseInfo = string.format("%s (pop %d) at (%d,%d)", nm, c:GetPopulation(), c:GetX(), c:GetY())
    local yields = string.format("Food %.1f Prod  %.1f Gold %.1f Sci %.1f Cul %.1f Faith %.1f", c:GetYield(0), c:GetYield(1), c:GetYield(2), c:GetYield(3), c:GetYield(4), c:GetYield(5))
    local growth = string.format("Housing %.1f Amenities %.1f | Growth: %.1f food/t, %d turns to grow", g:GetHousing(), amTotal, g:GetFoodSurplus(), g:GetTurnsUntilGrowth())
    local producingStr = string.format("building: %s (%d turns left)", producing, turnsLeft)
    local defense = string.format("HP:%d/%d Wall:%d/%d Def:%d Gar:%s", garHP, garMax, wallHP, wallMax, defStr, garrisonUnit)
    local loyalty = string.format("Loyalty:%.1f/%.1f, per turn:%.1f, turns to flip:%d", loy, loyMax, loyPT, loyFlip)
    
    print(string.format("\n%s - %s | %s | %s | %s | %s", baseInfo, yields, growth, producingStr, defense, loyalty))
    if #allBuildings > 0 then
        print("Buildings:" .. table.concat(allBuildings, ","))
    end
    print("Production options for city " .. nm .. " (" .. c:GetID() .. ")")
    print(CityProductionOptions(c:GetID()))
end

print("City distance matrix")
for i = 1, #cityCoords do for j = i + 1, #cityCoords do
    local d = Map.GetPlotDistance(cityCoords[i].x, cityCoords[i].y, cityCoords[j].x, cityCoords[j].y)
    print("Distance from " .. cityCoords[i].name .. " to " .. cityCoords[j].name .. " is " .. d)
end end