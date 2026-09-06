-- Generic unit action executor (InGame context).
--
-- This file is a TEMPLATE loaded by build_unit_action() in units.py.
-- Tags substituted before the Lua is sent to the game:
--   __UNIT_ID__            -> engine unit id (UnitManager.GetUnit key)
--   __ACTION_ID__          -> quoted DB action id ("UNITCOMMAND_GIFT" or
--                             "UNITOPERATION_FORTIFY" — the same ids shown in
--                             the unit's unit_action list in game state)
--   __HAS_PLOT__ / __P_X__ / __P_Y__      -> optional target tile
--   __HAS_TARGET_UNIT__ / __TARGET_UNIT_ID__ -> optional partner unit
--   __HAS_IMPROVEMENT__ / __IMPROVEMENT__    -> improvement type to build
--   __HAS_PROMOTION__ / __PROMOTION_TYPE__   -> promotion to apply
--   __HAS_WMD__ / __WMD_TYPE__               -> WMD type to strike with
--   __MCP_SENTINEL_TAG__  -> the response sentinel
--
-- Mirrors the game UI's generic execution path (UnitPanel.OnUnitActionClicked
-- -> UnitManager.RequestCommand / RequestOperation): resolve the id in
-- GameInfo, build the parameter table, validate with the same CanStart call
-- the UI uses, then send the request. The interface-mode layer the UI uses
-- for target selection is bypassed — target plots arrive as PARAM_X/PARAM_Y
-- exactly the way the UI's mode handlers (WorldInput.lua) forward them.
--
-- Parameter-key notes (all repo- or UI-verified):
--   * spy operations take PARAM_X0/PARAM_Y0, not PARAM_X/PARAM_Y (live-
--     verified in the old espionage builders; the game UI's choosers use X/Y
--     but X0/Y0 is what worked through the tuner).
--   * commands take UnitCommandTypes.PARAM_X/Y (airlift); operations take
--     UnitOperationTypes.PARAM_X/Y.
--   * requests always pass a table — for no-param actions the {{}} shape
--     (a table holding an empty table) that the legacy builders verified.
--     Checks pass nil instead when no real parameter was supplied, matching
--     the UI's bare strict check.
--   * FORM_CORPS / FORM_ARMY / ENTER_FORMATION take the partner unit's
--     PARAM_UNIT_PLAYER + PARAM_UNIT_ID (WorldInput.FormCorps shape).
--
-- Actions owned by dedicated commands are refused here (same deny-list as
-- the UACTION enumeration in units.lua) so the generic command can never
-- bypass move_unit/attack_unit/found_city and the chooser flows.

local me = Game.GetLocalPlayer()
local unit = UnitManager.GetUnit(me, __UNIT_ID__)
if unit == nil then
    print("ERR:UNIT_NOT_FOUND")
    print("__MCP_SENTINEL_TAG__")
    return
end

local actionId = __ACTION_ID__

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
if DENY_ACTIONS[actionId] then
    print("ERR:ACTION_DENIED|" .. actionId .. " is owned by a dedicated command (move_unit, attack_unit, found_city, religion/trade/spy flows). Use the documented command instead.")
    print("__MCP_SENTINEL_TAG__")
    return
end

-- Great Prophets don't use the generic activation — their path is the
-- religion-founding flow (unit op + belief PlayerOperations), all handled
-- by the found_religion command. Refuse with a pointer instead of letting
-- the engine produce a confusing failure.
local uEntry = GameInfo.Units[unit:GetType()]
if actionId == "UNITCOMMAND_ACTIVATE_GREAT_PERSON"
    and uEntry ~= nil and uEntry.UnitType == "UNIT_GREAT_PROPHET" then
    print("ERR:ACTION_DENIED|Great Prophets found religions instead of activating — use the found_religion command (the prophet must be on a completed Holy Site).")
    print("__MCP_SENTINEL_TAG__")
    return
end

local row = GameInfo.UnitCommands[actionId]
local isCommand = row ~= nil
if row == nil then row = GameInfo.UnitOperations[actionId] end
if row == nil then
    print("ERR:ACTION_NOT_FOUND|" .. actionId .. " is not a UnitCommand or UnitOperation. Use an action id from this unit's unit_action list in get_full_game_state.")
    print("__MCP_SENTINEL_TAG__")
    return
end
local h = row.Hash
local isSpyOp = (not isCommand) and (string.find(actionId, "^UNITOPERATION_SPY_") ~= nil)

-- Clean an engine failure string for the pipe-delimited reply: localize LOC
-- keys (nil-guarded — Locale.Lookup(nil) raises), strip [ICON_]/[COLOR_]
-- tags and the delimiters our line format uses.
local function cleanReason(s)
    if s == nil then return "" end
    local t = tostring(s)
    pcall(function() t = tostring(Locale.Lookup(s)) end)
    t = t:gsub("%b[]", "")
         :gsub("[,;|~]", " ")
         :gsub("%s+", " ")
         :gsub("^%s+", "")
         :gsub("%s+$", "")
    if #t > 90 then t = t:sub(1, 90) .. "..." end
    return t
end

local function collectReasons(results, out)
    if results == nil then return end
    local arr = results[UnitOperationResults.FAILURE_REASONS]
    if type(arr) == "table" then
        for _, r in ipairs(arr) do
            local c = cleanReason(r)
            if c ~= "" then table.insert(out, c) end
        end
    end
    -- Command results may nest reason/description lists in sub-tables
    for _, v in pairs(results) do
        if type(v) == "table" and v ~= arr then
            for _, s in pairs(v) do
                if type(s) == "string" and s ~= "" then
                    local c = cleanReason(s)
                    if c ~= "" and #out < 3 then table.insert(out, c) end
                end
            end
        end
    end
end

-- Parameter table (always non-nil; {{}} when empty — see header)
local params = {{}}
local nReal = 0

if __HAS_PLOT__ then
    if isSpyOp then
        params[UnitOperationTypes.PARAM_X0] = __P_X__
        params[UnitOperationTypes.PARAM_Y0] = __P_Y__
    elseif isCommand then
        params[UnitCommandTypes.PARAM_X] = __P_X__
        params[UnitCommandTypes.PARAM_Y] = __P_Y__
    else
        params[UnitOperationTypes.PARAM_X] = __P_X__
        params[UnitOperationTypes.PARAM_Y] = __P_Y__
    end
    nReal = nReal + 2
end

if __HAS_TARGET_UNIT__ then
    local target = nil
    for _, tu in Players[me]:GetUnits():Members() do
        if tu:GetID() == __TARGET_UNIT_ID__ and tu:GetX() ~= -9999 then
            target = tu
            break
        end
    end
    if target == nil then
        print("ERR:TARGET_UNIT_NOT_FOUND|No owned unit with id __TARGET_UNIT_ID__")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    params[UnitCommandTypes.PARAM_UNIT_PLAYER] = target:GetOwner()
    params[UnitCommandTypes.PARAM_UNIT_ID] = target:GetID()
    nReal = nReal + 2
end

if __HAS_IMPROVEMENT__ then
    -- Buildable only on the unit's own tile. The owning build_unit_action()
    -- guarantees improvement is never combined with plot coords or a target
    -- unit, so overwriting PARAM_X/PARAM_Y here cannot clobber another
    -- branch's values.
    local imp = GameInfo.Improvements[__IMPROVEMENT__]
    if imp == nil then
        print("ERR:IMPROVEMENT_NOT_FOUND|" .. __IMPROVEMENT__ .. " is not in the Improvements database")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    -- Improvements are built on the builder's own tile
    params[UnitOperationTypes.PARAM_X] = unit:GetX()
    params[UnitOperationTypes.PARAM_Y] = unit:GetY()
    params[UnitOperationTypes.PARAM_IMPROVEMENT_TYPE] = imp.Hash
    nReal = nReal + 3
end

if __HAS_PROMOTION__ then
    local promo = GameInfo.UnitPromotions[__PROMOTION_TYPE__]
    if promo == nil then
        print("ERR:PROMOTION_NOT_FOUND|" .. __PROMOTION_TYPE__ .. " is not in the UnitPromotions database")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    params[UnitCommandTypes.PARAM_PROMOTION_TYPE] = promo.Index
    nReal = nReal + 1
end

if __HAS_WMD__ then
    local wmdRow = GameInfo.WMDs[__WMD_TYPE__]
    if wmdRow == nil then
        print("ERR:WMD_NOT_FOUND|" .. __WMD_TYPE__ .. " is not in the WMDs database")
        print("__MCP_SENTINEL_TAG__")
        return
    end
    params[UnitOperationTypes.PARAM_WMD_TYPE] = wmdRow.Index
    nReal = nReal + 1
end

-- PROMOTE: validate the promotion is in the unit's takeable list (the engine
-- accepts RequestCommand for promotions the unit cannot take and silently
-- no-ops — same authoritative-membership gate the legacy promote builder and
-- the UI's promotion popup use).
if isCommand and h == UnitCommandTypes.PROMOTE then
    local allowed = false
    pcall(function()
        local bCan, tRes = UnitManager.CanStartCommand(unit, h, true, true)
        if bCan and tRes then
            local list = tRes[UnitCommandResults.PROMOTIONS]
            if list then
                for _, pidx in pairs(list) do
                    if pidx == params[UnitCommandTypes.PARAM_PROMOTION_TYPE] then
                        allowed = true
                        break
                    end
                end
            end
        end
    end)
    if not allowed then
        print("ERR:CANNOT_PROMOTE|" .. __PROMOTION_TYPE__ .. " is not available to this unit now (insufficient XP, missing prereq, wrong class, or no moves). Available promotions are shown in the Units section of get_full_game_state.")
        print("__MCP_SENTINEL_TAG__")
        return
    end
end

-- Strict validation with the same call shapes the UI uses
local canStart, results
if isCommand then
    if nReal > 0 then
        canStart, results = UnitManager.CanStartCommand(unit, h, params, true)
    else
        canStart, results = UnitManager.CanStartCommand(unit, h, false, true)
    end
else
    if nReal > 0 then
        canStart, results = UnitManager.CanStartOperation(unit, h, nil, params, true)
    else
        canStart, results = UnitManager.CanStartOperation(unit, h, nil, false, OperationResultsTypes.NO_TARGETS)
    end
end
if not canStart then
    local reasons = {}
    pcall(function() collectReasons(results, reasons) end)
    local rstr = ""
    if #reasons > 0 then rstr = "|" .. table.concat(reasons, "; ") end
    print("ERR:CANNOT_START|" .. actionId .. " cannot be started now" .. rstr)
    print("__MCP_SENTINEL_TAG__")
    return
end

-- Execute (async in the engine — the reply reports the request; the next
-- state read is the authority on the effect)
if isCommand then
    UnitManager.RequestCommand(unit, h, params)
else
    UnitManager.RequestOperation(unit, h, params)
end
print("OK:ACTION|" .. actionId .. " requested for unit __UNIT_ID__ (verify in next state read)")
print("__MCP_SENTINEL_TAG__")
