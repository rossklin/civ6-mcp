local function GetMapASCII()
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
        local rowStared = false
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
                    -- TODO space pad y to 2 chars
                    prefix = y .. " |"
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
                    local hills = plot:IsHills() and "1" or "0"
                    local featureIdx = plot:GetFeatureType()
                    local feature = "none"
                    if featureIdx >= 0 then feature = GameInfo.Features[featureIdx].FeatureType end

                    local tChar = TERRAIN_CHARS[terrain] or "U"
                    local fChar = FEATURE_CHARS[feature] or "U"
                    tileString = prefix .. tChar .. fChar
                else
                    tileString = prefix .. "??"
                end
                output = output .. tileString
            else
                return "ERROR: Map.GetPlot(" .. x .. ", " .. y .. ") returned nil"
            end
        end
    end

    -- TODO print axis and legend

    output = output .. "{SENTINEL}"
    return output
end

print(GetMapASCII())