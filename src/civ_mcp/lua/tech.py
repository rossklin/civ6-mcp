"""Tech domain — Lua builders and parsers."""

from __future__ import annotations
from pathlib import Path

from civ_mcp.lua._helpers import SENTINEL, _bail
from civ_mcp.lua.models import (
    CivicOption,
    LockedCivic,
    LockedTech,
    TechCivicStatus,
    TechOption,
)

_BUILD_TECHS_CIVICS_QUERY_TEMPLATE: str | None = None

def _load_build_techs_civics_query_template() -> str:
    """Read and cache the build_tech_civics_query Lua template from disk."""
    global _BUILD_TECHS_CIVICS_QUERY_TEMPLATE
    if _BUILD_TECHS_CIVICS_QUERY_TEMPLATE is None:
        _BUILD_TECHS_CIVICS_QUERY_TEMPLATE = Path(__file__).resolve().parent / "build_tech_civics_query.lua".read_text(encoding="utf-8")
    return _BUILD_TECHS_CIVICS_QUERY_TEMPLATE

def build_tech_civics_query() -> str:
    return _load_build_techs_civics_query_template().replace("{SENTINEL}", SENTINEL)

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


def parse_tech_civics_response(lines: list[str]) -> TechCivicStatus:
    current_research = "None"
    current_research_turns = -1
    current_civic = "None"
    current_civic_turns = -1
    available_techs: list[TechOption] = []
    available_civics: list[CivicOption] = []
    completed_tech_count = 0
    completed_civic_count = 0

    locked_civics: list[LockedCivic] = []
    locked_techs: list[LockedTech] = []

    for line in lines:
        if line.startswith("COMPLETED|"):
            parts = line.split("|")
            completed_tech_count = int(parts[1]) if len(parts) > 1 else 0
            completed_civic_count = int(parts[2]) if len(parts) > 2 else 0
        elif line.startswith("CURRENT|"):
            parts = line.split("|")
            current_research = parts[1]
            current_research_turns = int(parts[2])
            current_civic = parts[3]
            current_civic_turns = int(parts[4])
        elif line.startswith("TECH|"):
            parts = line.split("|")
            if len(parts) >= 9:
                available_techs.append(
                    TechOption(
                        name=parts[1],
                        tech_type=parts[2],
                        cost=int(parts[3]),
                        progress_pct=int(parts[4]),
                        turns=int(parts[5]),
                        boosted=parts[6] == "BOOSTED",
                        boost_desc=parts[7],
                        unlocks=parts[8],
                        prereqs=parts[9] if len(parts) > 9 else "",
                        era=parts[10] if len(parts) > 10 else "",
                    )
                )
            elif len(parts) >= 3:
                available_techs.append(
                    TechOption(
                        name=parts[1],
                        tech_type=parts[2],
                        cost=0,
                        progress_pct=0,
                        turns=0,
                        boosted=False,
                        boost_desc="",
                        unlocks="",
                    )
                )
        elif line.startswith("CIVIC|"):
            parts = line.split("|")
            if len(parts) >= 8:
                available_civics.append(
                    CivicOption(
                        name=parts[1],
                        civic_type=parts[2],
                        cost=int(parts[3]),
                        progress_pct=int(parts[4]),
                        turns=int(parts[5]),
                        boosted=parts[6] == "BOOSTED",
                        boost_desc=parts[7],
                        prereqs=parts[8] if len(parts) > 8 else "",
                        era=parts[9] if len(parts) > 9 else "",
                    )
                )
            elif len(parts) >= 3:
                available_civics.append(
                    CivicOption(
                        name=parts[1],
                        civic_type=parts[2],
                        cost=0,
                        progress_pct=0,
                        turns=0,
                        boosted=False,
                        boost_desc="",
                    )
                )
        elif line.startswith("LOCKED_CIVIC|"):
            parts = line.split("|")
            if len(parts) >= 4:
                locked_civics.append(
                    LockedCivic(
                        name=parts[1],
                        civic_type=parts[2],
                        missing_prereqs=parts[3].split(","),
                        era=parts[4] if len(parts) > 4 else "",
                        boosted=parts[5] == "BOOSTED" if len(parts) > 5 else False,
                        boost_desc=parts[6] if len(parts) > 6 else "",
                    )
                )
        elif line.startswith("LOCKED_TECH|"):
            parts = line.split("|")
            if len(parts) >= 4:
                locked_techs.append(
                    LockedTech(
                        name=parts[1],
                        tech_type=parts[2],
                        missing_prereqs=parts[3].split(","),
                        era=parts[4] if len(parts) > 4 else "",
                        boosted=parts[5] == "BOOSTED" if len(parts) > 5 else False,
                        boost_desc=parts[6] if len(parts) > 6 else "",
                    )
                )

    return TechCivicStatus(
        current_research=current_research,
        current_research_turns=current_research_turns,
        current_civic=current_civic,
        current_civic_turns=current_civic_turns,
        available_techs=available_techs,
        available_civics=available_civics,
        completed_tech_count=completed_tech_count,
        completed_civic_count=completed_civic_count,
        locked_civics=locked_civics or None,
        locked_techs=locked_techs or None,
    )
