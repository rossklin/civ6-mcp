"""Tests for the UACTION enumeration (available unit actions in game state).

units.lua emits ``UACTION|unit_id|action_id|category|needs[|detail]`` lines
beside the per-unit UNITS| lines; parse_units_response attaches them to the
matching UnitInfo and narrate_units renders them as a compact unit_action
hint list. The unit_action.lua executor template is covered by tag-
substitution and content checks (no Lua runtime here — live behavior is
verified in-game).
"""

import asyncio

import pytest

from civ_mcp import lua as lq
from civ_mcp.game_state import GameState
from civ_mcp.narrate import narrate_units

# Realistic shaped output of build_units_query: one UNITS| line per unit
# (16 columns) plus FORMATION|/UACTION| lines emitted in later passes.
# UACTION|unit_id|action_id|category|needs|disabled|detail[|reasons]
_SAMPLE_LINES = [
    # id|name|type|x,y|moves/max|hp/max|cs|rs|charges|targets|promo|canUp|upName|upCost|imps|religion
    "7|Warrior|UNIT_WARRIOR|10,20|2/2|100/100|20|0|0||||0|||",
    "12|Settler|UNIT_SETTLER|11,21|2/2|100/100|0|0|0||||0|||",
    "15|Trader|UNIT_TRADER|9,19|2/2|100/100|0|0|0||||0|||",
    "21|Great Engineer|UNIT_GREAT_ENGINEER|5,5|2/2|100/100|0|0|1||||0|||",
    "UACTION|7|UNITOPERATION_FORTIFY|INPLACE|none|0|",
    "UACTION|7|UNITOPERATION_ALERT|INPLACE|none|0|",
    "UACTION|7|UNITCOMMAND_CONDEMN_HERETIC|SPECIFIC|none|0|",
    "UACTION|7|UNITCOMMAND_ENTER_FORMATION|SPECIFIC|unit|0|partners:12",
    "UACTION|7|UNITOPERATION_COASTAL_RAID|ATTACK|plot|0|",
    "UACTION|7|UNITOPERATION_WMD_STRIKE|ATTACK|wmd|0|types:WMD_NUCLEAR_DEVICE",
    "UACTION|7|UNITCOMMAND_FORM_CORPS|SPECIFIC|unit|1||Requires the Nationalism civic",
    "UACTION|7|UNITCOMMAND_UPGRADE|SPECIFIC|none|1||Requires 320 Gold",
    "UACTION|12|UNITCOMMAND_GIFT|INPLACE|none|0|",
    "UACTION|21|UNITCOMMAND_ACTIVATE_GREAT_PERSON|SPECIFIC|none|1||Must be on a completed Industrial Zone district",
    "FORMATION|7|12|SETTLER",
]


class TestParseUnitActions:
    def test_actions_attach_to_correct_unit(self):
        units = lq.parse_units_response(_SAMPLE_LINES)
        by_id = {u.unit_id: u for u in units}
        assert len(by_id[7].available_actions) == 8
        assert len(by_id[12].available_actions) == 1

    def test_action_fields(self):
        units = lq.parse_units_response(_SAMPLE_LINES)
        fortify = next(
            a
            for a in units[0].available_actions
            if a.action_id == "UNITOPERATION_FORTIFY"
        )
        assert fortify.category == "INPLACE"
        assert fortify.needs == "none"
        assert fortify.detail == ""
        assert fortify.disabled is False
        assert fortify.reasons == []
        partners = next(
            a
            for a in units[0].available_actions
            if a.action_id == "UNITCOMMAND_ENTER_FORMATION"
        )
        assert partners.needs == "unit"
        assert partners.detail == "partners:12"
        wmd = next(
            a
            for a in units[0].available_actions
            if a.action_id == "UNITOPERATION_WMD_STRIKE"
        )
        assert wmd.needs == "wmd"
        assert wmd.detail == "types:WMD_NUCLEAR_DEVICE"

    def test_disabled_actions_carry_reasons(self):
        units = lq.parse_units_response(_SAMPLE_LINES)
        by_id = {u.unit_id: u for u in units}
        corps = next(
            a
            for a in by_id[7].available_actions
            if a.action_id == "UNITCOMMAND_FORM_CORPS"
        )
        assert corps.disabled is True
        assert corps.reasons == ["Requires the Nationalism civic"]
        gp = next(
            a
            for a in by_id[21].available_actions
            if a.action_id == "UNITCOMMAND_ACTIVATE_GREAT_PERSON"
        )
        assert gp.disabled is True
        assert gp.reasons == ["Must be on a completed Industrial Zone district"]

    def test_unit_without_actions_gets_empty_list(self):
        units = lq.parse_units_response(
            [
                "3|Scout|UNIT_SCOUT|1,1|3/3|100/100|10|0|0||||0|||",
            ]
        )
        assert units[0].available_actions == []

    def test_malformed_lines_skipped(self):
        units = lq.parse_units_response(
            [
                "7|Warrior|UNIT_WARRIOR|10,20|2/2|100/100|20|0|0||||0|||",
                "UACTION|x|UNITOPERATION_FORTIFY|INPLACE|none|0|",  # bad unit id
                "UACTION|7|UNITOPERATION_FORTIFY|INPLACE",  # too few fields
                "UACTION|7|UNITOPERATION_SLEEP|INPLACE|none|0|",
            ]
        )
        assert len(units[0].available_actions) == 1
        assert units[0].available_actions[0].action_id == "UNITOPERATION_SLEEP"

    def test_formation_parsing_unaffected(self):
        units = lq.parse_units_response(_SAMPLE_LINES)
        by_id = {u.unit_id: u for u in units}
        assert by_id[7].formation_linked_to == 12


class TestNarrateUnitActions:
    def test_renders_compact_action_line(self):
        units = lq.parse_units_response(_SAMPLE_LINES)
        text = narrate_units(units)
        warrior_block = text.split("[id:7]")[1].split("[id:12]")[0]
        # Prefixes stripped, needs hints appended, detail kept
        assert "FORTIFY" in warrior_block
        assert "CONDEMN_HERETIC" in warrior_block
        assert "COASTAL_RAID(x,y)" in warrior_block
        assert "ENTER_FORMATION(target_unit_id)[partners:12]" in warrior_block
        assert "WMD_STRIKE(wmd_type,x,y)[types:WMD_NUCLEAR_DEVICE]" in warrior_block
        # Disabled actions land on the "not yet possible" line with the
        # engine's reason; enabled ones never mix in
        assert "not yet possible: FORM_CORPS — Requires the Nationalism civic" in warrior_block
        assert "UPGRADE — Requires 320 Gold" in warrior_block
        unit_action_line = next(
            ln for ln in warrior_block.splitlines() if ">> unit_action:" in ln
        )
        assert "FORM_CORPS" not in unit_action_line
        # Unit without actions shows no hint line (trader id 15 has none)
        trader_block = text.split("[id:15]")[1]
        assert "unit_action:" not in trader_block


class TestLuaTemplate:
    def test_template_contains_enumeration(self):
        """units.lua ships the UACTION pass and the deny-list of dedicated
        commands (parse would silently drop the whole feature otherwise)."""
        lua = lq.build_units_query()
        assert 'print("UACTION|"' in lua or "UACTION|" in lua
        for denied in (
            "UNITOPERATION_MOVE_TO",
            "UNITOPERATION_RANGE_ATTACK",
            "UNITOPERATION_AIR_ATTACK",
            "UNITOPERATION_FOUND_CITY",
            "UNITOPERATION_FOUND_RELIGION",
            "UNITOPERATION_EVANGELIZE_BELIEF",
            "UNITOPERATION_MAKE_TRADE_ROUTE",
            "UNITOPERATION_SKIP_TURN",
            "UNITCOMMAND_NAME_UNIT",
        ):
            assert f"{denied} = true" in lua
        # Tag substitution happened
        assert "__MCP_SENTINEL_TAG__" not in lua
        assert "__LUA_OCCUPANCY_CLASS__" not in lua


class TestBuildUnitAction:
    def test_minimal_substitution(self):
        lua = lq.build_unit_action(7, "UNITCOMMAND_CONDEMN_HERETIC")
        assert 'local actionId = "UNITCOMMAND_CONDEMN_HERETIC"' in lua
        assert "UnitManager.GetUnit(me, 7)" in lua
        # All optional flags off
        assert "if false then" in lua
        # No unsubstituted tags remain
        for tag in (
            "__UNIT_ID__",
            "__ACTION_ID__",
            "__HAS_PLOT__",
            "__P_X__",
            "__P_Y__",
            "__HAS_TARGET_UNIT__",
            "__TARGET_UNIT_ID__",
            "__HAS_IMPROVEMENT__",
            "__IMPROVEMENT__",
            "__HAS_PROMOTION__",
            "__PROMOTION_TYPE__",
            "__HAS_WMD__",
            "__WMD_TYPE__",
            "__MCP_SENTINEL_TAG__",
        ):
            assert tag not in lua

    def test_plot_params_substituted(self):
        lua = lq.build_unit_action(
            3, "UNITOPERATION_COASTAL_RAID", target_x=12, target_y=4
        )
        assert "if true then" in lua
        assert "PARAM_X0" in lua  # spy-key routing lives in the template

    def test_all_params_substituted(self):
        lua = lq.build_unit_action(
            9,
            "UNITOPERATION_WMD_STRIKE",
            target_x=1,
            target_y=2,
            wmd_type="WMD_NUCLEAR_DEVICE",
        )
        assert '"WMD_NUCLEAR_DEVICE"' in lua

    def test_mismatched_coords_rejected(self):
        with pytest.raises(ValueError):
            lq.build_unit_action(7, "UNITOPERATION_REBASE", target_x=5)

    def test_wmd_requires_plot(self):
        with pytest.raises(ValueError):
            lq.build_unit_action(7, "UNITOPERATION_WMD_STRIKE", wmd_type="WMD_NUCLEAR_DEVICE")

    def test_target_dimensions_mutually_exclusive(self):
        # improvement + plot: the Lua improvement branch would overwrite
        # PARAM_X/PARAM_Y with the unit's own tile — must be rejected loudly
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7,
                "UNITOPERATION_BUILD_IMPROVEMENT",
                target_x=5,
                target_y=6,
                improvement="IMPROVEMENT_FARM",
            )
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7, "UNITCOMMAND_FORM_CORPS", target_unit_id=9, improvement="IMPROVEMENT_FORT"
            )
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7, "UNITCOMMAND_FORM_CORPS", target_unit_id=9, target_x=1, target_y=2
            )

    def test_promotion_excludes_other_params(self):
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7,
                "UNITCOMMAND_PROMOTE",
                promotion_type="PROMOTION_CITY_ASSAULT",
                target_x=1,
                target_y=2,
            )
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7,
                "UNITCOMMAND_PROMOTE",
                promotion_type="PROMOTION_CITY_ASSAULT",
                target_unit_id=9,
            )

    def test_wmd_plot_combination_is_valid(self):
        lua = lq.build_unit_action(
            7,
            "UNITOPERATION_WMD_STRIKE",
            target_x=10,
            target_y=20,
            wmd_type="WMD_NUCLEAR_DEVICE",
        )
        assert "if true then" in lua
        with pytest.raises(ValueError):
            lq.build_unit_action(
                7,
                "UNITOPERATION_WMD_STRIKE",
                target_x=10,
                target_y=20,
                wmd_type="WMD_NUCLEAR_DEVICE",
                target_unit_id=9,
            )

    def test_deny_list_and_execution_paths_present(self):
        lua = lq.build_unit_action(1, "UNITCOMMAND_GIFT")
        # Same deny-list as the state enumeration
        for denied in (
            "UNITOPERATION_MOVE_TO",
            "UNITOPERATION_RANGE_ATTACK",
            "UNITOPERATION_FOUND_CITY",
            "UNITCOMMAND_NAME_UNIT",
        ):
            assert f"{denied} = true" in lua
        # Generic execution paths and repo-verified quirks
        assert "UnitManager.RequestCommand" in lua
        assert "UnitManager.RequestOperation" in lua
        assert "local params = {{}}" in lua
        assert "PARAM_X0" in lua
        assert "UnitCommandResults.PROMOTIONS" in lua

    def test_corps_partner_enumeration_present(self):
        """units.lua enumerates FORM_CORPS/FORM_ARMY partners through
        GetCommandTargets — the same call the UI's targeting overlay uses
        (WorldInput.lua) — since CanStartCommand-results UNITS is only
        filled for ENTER_FORMATION."""
        lua = lq.build_units_query()
        assert "UnitManager.GetCommandTargets" in lua
        assert "UnitCommandTypes.FORM_CORPS" in lua
        assert "UnitCommandTypes.FORM_ARMY" in lua


class _StubConnection:
    def __init__(self, lines):
        self._lines = lines
        self.lua_sent = None

    async def execute_write(self, lua):
        self.lua_sent = lua
        return self._lines

    async def execute_read(self, lua):
        return self._lines


class TestGameStateUnitAction:
    def test_ok_passthrough(self):
        conn = _StubConnection(["OK:ACTION|UNITCOMMAND_GIFT requested for unit 7 (verify in next state read)"])
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.unit_action(7, "UNITCOMMAND_GIFT"))
        assert result.startswith("ACTION|UNITCOMMAND_GIFT requested")
        assert "UNITCOMMAND_GIFT" in conn.lua_sent

    def test_err_passthrough(self):
        conn = _StubConnection(["ERR:CANNOT_START|UNITCOMMAND_GIFT cannot be started now|Not in foreign territory"])
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.unit_action(7, "UNITCOMMAND_GIFT"))
        assert result.startswith("Error: CANNOT_START")
        assert "Not in foreign territory" in result

    def test_lua_contains_params(self):
        conn = _StubConnection(["OK:ACTION|x"])
        gs = GameState.__new__(GameState)
        gs.conn = conn
        asyncio.run(
            gs.unit_action(
                5,
                "UNITCOMMAND_FORM_CORPS",
                target_unit_id=11,
            )
        )
        assert "UnitManager.GetUnit(me, 5)" in conn.lua_sent
        assert "tu:GetID() == 11" in conn.lua_sent
