local function SpacePad(num, len)
    local str = tostring(num)
    local pad = len - string.len(str)
    if (pad > 0) then
        return string.rep(" ", pad) .. str
    else
        return str
    end
end

local function GetMapASCII_Terrain()
    local me = Game.GetLocalPlayer()
    local vis = PlayersVisibility[me]
    local output = ""
    local w, h = Map.GetGridSize()

    -- Single-char glyphs for terrain (first char) and feature (second char).
    local TERRAIN_CHARS = {
        GRASS="g", GRASS_HILLS="G", GRASS_MOUNTAIN="^",
        PLAINS="p", PLAINS_HILLS="P", PLAINS_MOUNTAIN="M",
        DESERT="d", DESERT_HILLS="D",
        TUNDRA="t", TUNDRA_HILLS="T",
        SNOW="s", SNOW_HILLS="S",
        COAST="c", OCEAN="o",
        CITY="C",
        UNDISCOVERED="?", UNKNOWN="U",
    }

    local FEATURE_CHARS = {
        none=" ",
        ICE="i",
        FOREST="f",
        GEOTHERMAL_FISSURE="g",
        VOLCANIC_SOIL="v",
        VOLCANO="V",
        FLOODPLAINS_GRASSLAND="=",
        FLOODPLAINS_PLAINS="+",
        MARSH="m",
        JUNGLE="j",
        KILIMANJARO="K",
        UNDISCOVERED="?", UNKNOWN="U",
    }

    for y = 0, h - 1 do
        for x = 0, w - 1 do
            local plot = Map.GetPlot(x, y)
            if plot then
                local isFirst = x == 0

                -- Look at the NW (dir enum 5) tile to determine if this row is left or right shifted
                local adj = Map.GetAdjacentPlot(x, y, 5)
                local adjX = adj and adj:GetX() or -1
                local parity
                if (x == adjX) then
                    parity = "right"
                else
                    parity = "left"
                end

                -- add two spaces before tile info unless this is the first tile in a left shifted row
                local prefix = ""
                if (isFirst) then
                    prefix = SpacePad(y, 2) .. " |"
                    if (parity ~= "left") then
                        prefix = prefix .. "  "
                    end
                else
                    prefix = "  "
                end

                local plotIdx = plot:GetIndex()
                local revealed = vis:IsRevealed(plotIdx)
                local tileString = ""
                if revealed then
                    
                    -- map terrain and feature to ascii symbols
                    local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
                    local featureIdx = plot:GetFeatureType()
                    local feature = "UNKNOWN"
                    if featureIdx >= 0 then feature = GameInfo.Features[featureIdx].FeatureType end

                    local tChar = TERRAIN_CHARS[terrain] or "U"
                    local fChar = FEATURE_CHARS[feature] or "U"

                    -- Special case: check for city
                    local distIdx = plot:GetDistrictType()
                    if distIdx >= 0 then
                        local dInfo = GameInfo.Districts[distIdx]
                        if dInfo and dInfo.DistrictType == "CITY_CENTER" then
                            tChar = "C"
                            fChar = " "
                        end
                    end

                    tileString = prefix .. tChar .. fChar
                else
                    tileString = prefix .. "??"
                end
                output = output .. tileString
            else
                return "ERROR: Map.GetPlot(" .. x .. ", " .. y .. ") returned nil"
            end
        end
        output = output .. "\n"
    end

    -- X-axis labels
    output = output .. "  X"
    for x = 0, w - 1 do
        output = output .. SpacePad(x, 4)
    end

    -- Legend (generated from the char tables so it stays in sync)
    local function CharLabel(ch)
        if ch == " " then return "(space)" end
        return ch
    end
    local function BuildLegend(title, table)
        local line = "  " .. title .. ":"
        for key, ch in pairs(table) do
            line = line .. " " .. CharLabel(ch) .. "=" .. key
        end
        return line .. "\n"
    end
    output = output .. "\nLegend (each tile is terrain char + feature char; unrevealed tiles shown as '??'):\n"
    output = output .. BuildLegend("Terrain", TERRAIN_CHARS)
    output = output .. BuildLegend("Feature", FEATURE_CHARS)
    return output
end

print(GetMapASCII_Terrain())