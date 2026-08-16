local w, h = Map.GetGridSize()
for y = 0, h - 1 do
    local rowStared = false
    for x = 0, w - 1 do
        local plot = Map.GetPlot(x, y)
        if plot then
            local plotIdx = plot:GetIndex()
            local revealed = vis:IsRevealed(plotIdx)
            if revealed then
                local isFirst = not rowStared
                rowStared = true

                -- Look at the NW (dir enum 5) tile to determine if this row is left or right shifted
                local adj = Map.GetAdjacentPlot(x, y, 5)
                local adjX = adj and adj:GetX() or -1
                local parity
                if (x == adjX) then
                    parity = "right"
                else
                    parity = "left"
                end

                local terrain = GameInfo.Terrains[plot:GetTerrainType()].TerrainType
                local hills = plot:IsHills() and "1" or "0"
                local featureIdx = plot:GetFeatureType()
                local feature = "none"
                if featureIdx >= 0 then feature = GameInfo.Features[featureIdx].FeatureType end

                -- add two spaces before tile info unless this is the first tile in a left shifted row
                local prefix = ""
                if (isFirst) then
                    prefix = y .. "|"
                    if (parity ~= "left") then
                        prefix = prefix .. "  "
                    end
                end

                -- TODO map terrain and feature to ascii symbols

            end
        end
    end
end

-- TODO print axis and legend

print("{SENTINEL}")