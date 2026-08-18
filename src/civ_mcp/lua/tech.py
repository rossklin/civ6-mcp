"""Tech domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import SENTINEL, _bail, load_lua_template

def _build_set_ingame(
    name: str,
    gi_table: str,
    type_field: str,
    param: str,
    operation: str,
    blocking: str,
    ok_prefix: str,
) -> str:
    """Shared builder for set_research / set_civic via InGame UI."""
    err_label = "TECH" if "Tech" in gi_table else "CIVIC"
    has_method = "HasTech" if "Tech" in gi_table else "HasCivic"
    player_method = "GetTechs" if "Tech" in gi_table else "GetCulture"
    return f"""
local id = Game.GetLocalPlayer()
local idx = nil
for row in GameInfo.{gi_table}() do
    if row.{type_field} == "{name}" then idx = row.Index; break end
end
if idx == nil then {_bail(f"ERR:{err_label}_NOT_FOUND|{name}")} end
if Players[id]:{player_method}():{has_method}(idx) then
    {_bail(f"ERR:ALREADY_COMPLETED|{name} is already researched")}
end
local params = {{}}
params[PlayerOperations.{param}] = idx
UI.RequestPlayerOperation(id, PlayerOperations.{operation}, params)
local list = NotificationManager.GetList(id)
if list then
    for _, nid in ipairs(list) do
        local e = NotificationManager.Find(id, nid)
        if e and not e:IsDismissed() then
            local bt = e:GetEndTurnBlocking()
            if bt and bt == EndTurnBlockingTypes.{blocking} then
                pcall(function() NotificationManager.SendActivated(id, nid) end)
                pcall(function() NotificationManager.Dismiss(id, nid) end)
            end
        end
    end
end
print("{ok_prefix}|{name}")
print("{SENTINEL}")
"""


def build_set_research(tech_name: str) -> str:
    return _build_set_ingame(
        tech_name,
        "Technologies",
        "TechnologyType",
        "PARAM_TECH_TYPE",
        "RESEARCH",
        "ENDTURN_BLOCKING_RESEARCH",
        "OK:RESEARCHING",
    )


def build_set_civic(civic_name: str) -> str:
    return _build_set_ingame(
        civic_name,
        "Civics",
        "CivicType",
        "PARAM_CIVIC_TYPE",
        "PROGRESS_CIVIC",
        "ENDTURN_BLOCKING_CIVIC",
        "OK:PROGRESSING",
    )


def _build_set_gamecore(
    name: str,
    gi_table: str,
    type_field: str,
    player_method: str,
    setter: str,
    getter: str | None,
    ok_prefix: str,
) -> str:
    """Shared builder for set_research / set_civic via GameCore fallback."""
    err_label = "TECH" if "Tech" in gi_table else "CIVIC"
    verify = ""
    if getter:
        verify = f"""
local now = Players[id]:{player_method}():{getter}()
if now == idx then
    print("{ok_prefix}|{name}")
else
    {_bail(f"ERR:RESEARCH_FAILED|GameCore also failed to set {name}")}
end"""
    else:
        verify = f'\nprint("{ok_prefix}|{name}")'
    has_method = "HasTech" if "Tech" in gi_table else "HasCivic"
    completed_method = "GetTechs" if "Tech" in gi_table else "GetCulture"
    return f"""
local id = Game.GetLocalPlayer()
local idx = nil
for row in GameInfo.{gi_table}() do
    if row.{type_field} == "{name}" then idx = row.Index; break end
end
if idx == nil then {_bail(f"ERR:{err_label}_NOT_FOUND|{name}")} end
if Players[id]:{completed_method}():{has_method}(idx) then
    {_bail(f"ERR:ALREADY_COMPLETED|{name} is already researched")}
end
Players[id]:{player_method}():{setter}(idx){verify}
print("{SENTINEL}")
"""


def build_set_research_gamecore(tech_name: str) -> str:
    """Set tech via GameCore — fallback when InGame RequestPlayerOperation silently fails."""
    return _build_set_gamecore(
        tech_name,
        "Technologies",
        "TechnologyType",
        "GetTechs",
        "SetResearchingTech",
        "GetResearchingTech",
        "OK:RESEARCHING_GAMECORE",
    )


def build_set_civic_gamecore(civic_name: str) -> str:
    """Set civic via GameCore — fallback when InGame RequestPlayerOperation silently fails."""
    return _build_set_gamecore(
        civic_name,
        "Civics",
        "CivicType",
        "GetCulture",
        "SetProgressingCivic",
        None,
        "OK:PROGRESSING_GC",
    )
