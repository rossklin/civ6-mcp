local me = Game.GetLocalPlayer()
local unit = UnitManager.GetUnit(me, __MCP_UNIT_ID_TAG__)
if unit == nil then print('ERR:UNIT_NOT_FOUND') end

local targetPlot = Map.GetPlot(__MCP_TARGET_X_TAG__, __MCP_TARGET_Y_TAG__)
if not targetPlot then prin "ERR:INVALID_TARGET Target (__MCP_TARGET_X_TAG__,__MCP_TARGET_Y_TAG__) is out of bounds" end
local path = UnitManager.GetMoveToPath(unit, targetPlot:GetIndex())
if not path or #path == 0 then
    print("ERR:PATH_NOT_GENERATED engine failed to generate path to (__MCP_TARGET_X_TAG__,__MCP_TARGET_Y_TAG__)")
    return
end
-- Validate path reaches destination (GetMoveToPath returns garbage for unreachable targets)
local lastPlot = Map.GetPlotByIndex(path[#path])
if lastPlot:GetX() ~= __MCP_TARGET_X_TAG__ or lastPlot:GetY() ~= __MCP_TARGET_Y_TAG__ then
    print("ERR:PATH_NOT_FOUND Target (__MCP_TARGET_X_TAG__,__MCP_TARGET_Y_TAG__) is unreachable")
    return
end

local waypoints = {}
for i, pIdx in ipairs(path) do
    local plot = Map.GetPlotByIndex(pIdx)
    waypoints[#waypoints + 1] = "(" .. plot:GetX() .. "," .. plot:GetY() .. ")"
end
print("WAYPOINTS: " .. table.concat(waypoints, ";"))