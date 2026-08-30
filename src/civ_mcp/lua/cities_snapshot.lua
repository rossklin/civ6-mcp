-- Cities snapshot (InGame context).
--
-- Minimal per-city state for the turn-snapshot diff (CitySnapshot model in
-- models.py). Output is pipe-delimited for parse_city_snapshot_response
-- (NOT narrated prose) — the stripped-down sibling of cities.lua, which
-- carries the full narrated report (production options, defense, buildings,
-- distance matrix) for the agent.
--
-- Line format (one per city):
--   CITY|<id>|<name>|<population>|<producing>|<food_surplus>|<turns_to_grow>|<loyalty>|<loyalty_per_turn>
--
-- <producing> is the UNIT_/BUILDING_/DISTRICT_/PROJECT_ type name, or
-- "nothing" when the build queue is empty or holds a ghost entry.

local me = Game.GetLocalPlayer()
local hashName = {}
for u in GameInfo.Units() do hashName[u.Hash] = u.UnitType end
for b in GameInfo.Buildings() do hashName[b.Hash] = b.BuildingType end
for d in GameInfo.Districts() do hashName[d.Hash] = d.DistrictType end
for p in GameInfo.Projects() do hashName[p.Hash] = p.ProjectType end

for i, c in Players[me]:GetCities():Members() do
    -- Pipe would break the delimited format; cities.lua escapes the same way.
    local nm = Locale.Lookup(c:GetName()):gsub("|", "/")
    local bq = c:GetBuildQueue()
    local producing = "nothing"
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
    end
    local g = c:GetGrowth()
    -- Loyalty defaults for the pre-Loyalty game phase / ruleset
    local loy, loyPT = 100, 0
    local cult = c:GetCulturalIdentity()
    if cult then
        loy = cult:GetLoyalty()
        loyPT = cult:GetLoyaltyPerTurn()
    end
    print("CITY|" .. c:GetID() .. "|" .. nm .. "|" .. c:GetPopulation() .. "|" .. producing .. "|" .. string.format("%.1f", g:GetFoodSurplus()) .. "|" .. g:GetTurnsUntilGrowth() .. "|" .. string.format("%.1f", loy) .. "|" .. string.format("%.1f", loyPT))
end
print("__MCP_SENTINEL_TAG__")
