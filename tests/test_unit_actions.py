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

    def test_own_tile_ops_carry_plot_params(self):
        """Own-tile operations pass the unit's plot in the strict check /
        request — the paramless check passed a builder on a featureless
        city center (live-verified T85)."""
        for lua in (
            lq.build_units_query(),
            lq.build_unit_action(1, "UNITOPERATION_REMOVE_FEATURE"),
        ):
            assert "OWN_TILE_OPS" in lua
            for op in (
                "UNITOPERATION_REMOVE_FEATURE",
                "UNITOPERATION_REMOVE_IMPROVEMENT",
                "UNITOPERATION_REPAIR",
                "UNITOPERATION_BUILD_ROUTE",
                "UNITOPERATION_SPREAD_RELIGION",
            ):
                assert f"{op} = true" in lua

    def test_reason_extraction_is_kind_split(self):
        """Failure reasons come from the FAILURE_REASONS array for both
        kinds; the nested sub-table scan is commands-only — operation
        results nest effect DESCRIPTIONS there (live-verified T85:
        BUILD_IMPROVEMENT surfaced 'Provides 0.5 Housing')."""
        state_lua = lq.build_units_query()
        assert "strictReasons(tRes, true)" in state_lua  # commands
        assert "strictReasons(tRes, false)" in state_lua  # operations
        assert "strictReasons(tRes2, true)" in state_lua
        assert "strictReasons(tRes2, false)" in state_lua
        exec_lua = lq.build_unit_action(1, "UNITCOMMAND_GIFT")
        assert "collectReasons(results, reasons, isCommand)" in exec_lua

    def test_build_improvement_requires_improvement_param(self):
        """Bare BUILD_IMPROVEMENT (no improvement=) is rejected loudly —
        the engine would silently no-op a paramless request."""
        lua = lq.build_unit_action(1, "UNITOPERATION_BUILD_IMPROVEMENT")
        assert "ERR:MISSING_PARAM" in lua
        # ...and the properly-parametrized call doesn't trip the guard
        ok_lua = lq.build_unit_action(
            1, "UNITOPERATION_BUILD_IMPROVEMENT", improvement="IMPROVEMENT_MINE"
        )
        assert 'GameInfo.Improvements["IMPROVEMENT_MINE"]' in ok_lua


class TestBuildAttackUnit:
    """attack_unit.lua ports the target-validity rules from the units.lua
    listing (checked against the game's own UI source). Tag checks pin the
    substitution; content checks pin the corrected rules — the old inline
    builder required war for EVERY attack (wrongly blocking theological
    combat, which needs none) and picked the enemy by Combat>0 instead of by
    occupancy class. No Lua runtime here — live behavior is verified
    in-game."""

    def test_substitution(self):
        lua = lq.build_attack_unit(7, 12, 4)
        assert "UnitManager.GetUnit(me, 7)" in lua
        assert "local tx, ty = 12, 4" in lua
        for tag in (
            "__UNIT_ID__",
            "__TARGET_X__",
            "__TARGET_Y__",
            "__MCP_SENTINEL_TAG__",
            "__LUA_OCCUPANCY_CLASS__",
        ):
            assert tag not in lua

    def test_hostility_rule_allows_theological_combat_without_war(self):
        lua = lq.build_attack_unit(7, 12, 4)
        # Hostility = barbarian/free-city owner, OR at war, OR religious
        # attacker vs religious target (theological combat needs no war)
        assert "otherOwner >= 62" in lua
        assert "or (aIsReligious and oClass == \"RELIGIOUS\")" in lua
        # The old unconditional war gate is gone
        assert "enemyOwner ~= 63" not in lua

    def test_tile_classification_by_occupancy_class(self):
        lua = lq.build_attack_unit(7, 12, 4)
        assert 'occupancyClass(entry) == "RELIGIOUS"' in lua
        assert '"FORMATION_CLASS_CIVILIAN"' in lua
        # Military units cannot attack religious units (and vice versa)
        assert "ERR:RELIGIOUS_TARGET" in lua
        assert "ERR:WRONG_ATTACKER_TYPE" in lua
        # Unescorted civilians are captured by moving onto the tile
        assert "isCapture" in lua
        assert "ERR:CAPTURE_NOT_POSSIBLE" in lua

    def test_operation_routing_matches_ui(self):
        lua = lq.build_attack_unit(7, 12, 4)
        assert 'entry.Domain == "DOMAIN_AIR"' in lua  # AIR_ATTACK routing
        assert "UnitOperationTypes.AIR_ATTACK" in lua
        assert "UnitOperationTypes.RANGE_ATTACK" in lua
        # MOVE_TO attack-move for melee/capture/theological (UI parity)
        assert "MOVE_IGNORE_UNEXPLORED_DESTINATION" in lua
        # Engine authority in every branch
        assert lua.count("CanStartOperation") >= 3

    def test_ranged_attacks_apply_at_any_distance(self):
        """Ranged units ranged-attack even when adjacent (the UI's
        RequestMoveOperation tries RANGE_ATTACK first with no distance
        branch) — both the executor and the units.lua listing gate on
        capability, not distance."""
        exec_lua = lq.build_attack_unit(7, 12, 4)
        assert "not isCapture and not isTheological and rs > 0 then" in exec_lua
        state_lua = lq.build_units_query()
        assert "if rs > 0 and simName ~= nil then" in state_lua
        for lua in (exec_lua, state_lua):
            assert "rs > 0 and d > 1" not in lua
            assert "rs > 0 and dist > 1" not in lua
        # The engine sim's forced combat type follows the same rule
        assert "if rs > 0 then eCombatType = CombatTypes.RANGED end" in state_lua
        # Only MELEE kills capture an escorted civilian — a ranged
        # attacker's interaction is always RANGED, which never captures
        assert "cs > 0 and rs == 0 and d == 1" in state_lua

    def test_single_request_no_restrike(self):
        """Whether a strike landed is settled by the GameCore poll, never by
        re-requesting the operation — a two-attacks-per-turn promotion would
        double-strike. Exactly one RequestOperation per attack path."""
        lua = lq.build_attack_unit(7, 12, 4)
        assert "ERR:STOPPED_SHORT" in lua
        assert lua.count("UnitManager.RequestOperation") == 3

    def test_defensible_district_is_combat_target(self):
        """A hostile defensible district (GameInfo HitPoints > 0 - city
        center, encampment) IS the combat target: garrisoned units are not
        separately targetable and take no damage while its defenses stand.
        pre_hp/enemy stats report the layer that takes damage (walls first,
        else district HP). City capture surfaces as enemy:CAPTURED_CITY in
        the outcome query, and capture verification is a separate
        ownership-only query."""
        lua = lq.build_attack_unit(7, 12, 4)
        assert "(dInfo.HitPoints or 0) > 0" in lua
        assert "DISTRICT_OUTER" in lua
        assert "DISTRICT_GARRISON" in lua
        assert "if distName ~= nil and not aIsReligious then" in lua
        outcome = lq.build_attack_outcome_query(7, 12, 4)
        assert "CAPTURED_CITY" in outcome
        assert "DISTRICT_OUTER" in outcome
        assert "OURS|" not in outcome  # ownership check lives elsewhere
        cap = lq.build_capture_outcome_query(12, 4)
        assert 'print("OURS|"' in cap

    def test_outcome_tags_for_post_combat_poll(self):
        lua = lq.build_attack_unit(7, 12, 4)
        # game_state.attack_unit polls GameCore for these outcome prefixes
        for tag in (
            "MELEE_ATTACK",
            "RANGE_ATTACK",
            "AIR_ATTACK",
            "THEOLOGICAL_ATTACK",
        ):
            assert f"OK:{tag}|" in lua
        # CAPTURE is a move, not damage — no poll, but reported
        assert "OK:CAPTURE|" in lua
        # pre_hp:/your HP: fields feed _extract_pre_hp/_extract_attacker_pre_hp
        assert "pre_hp:" in lua
        assert "your HP:" in lua


class _StubConnection:
    def __init__(self, lines, read_lines=None):
        self._lines = lines
        # The GameCore poll (execute_read) often reports different state
        # than the InGame write; default to the same lines for simplicity.
        self._read_lines = read_lines if read_lines is not None else lines
        self.lua_sent = None

    async def execute_write(self, lua):
        self.lua_sent = lua
        return self._lines

    async def execute_read(self, lua):
        return self._read_lines


class TestGameStateUnitAction:
    def test_ok_passthrough(self):
        conn = _StubConnection(["OK:ACTION|UNITCOMMAND_GIFT done"])
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.unit_action(7, "UNITCOMMAND_GIFT"))
        assert result == "ACTION|UNITCOMMAND_GIFT done"
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


class TestGameStateAttackUnit:
    """The GameCore poll settles the outcomes the Lua layer cannot prove:
    damage (any attack changes at least one combatant's HP or the district
    defense HP), city capture (CAPTURED_CITY), and civilian capture by
    ownership change (OURS| from the dedicated capture query)."""

    def test_capture_verified_by_ownership(self):
        conn = _StubConnection(
            ["OK:CAPTURE|unit:UNIT_SETTLER at (12,4)", "---END---"],
            read_lines=[
                "OURS|UNIT_WARRIOR",
                "OURS|UNIT_SETTLER",
                "---END---",
            ],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert result.startswith("CAPTURE|unit:UNIT_SETTLER")
        assert "captured — the unit is yours" in result

    def test_capture_failure_reported_by_ownership(self):
        # No OURS|UNIT_SETTLER at the tile — the order silently no-opped
        conn = _StubConnection(
            ["OK:CAPTURE|unit:UNIT_SETTLER at (12,4)", "---END---"],
            read_lines=["---END---"],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert "capture did NOT complete" in result
        assert "UNIT_SETTLER" in result

    def test_no_damage_reports_failure(self):
        # Both HPs unchanged in GameCore -> the strike did not land; the
        # failure string is kept separate from any combat stats
        conn = _StubConnection(
            [
                "OK:MELEE_ATTACK|target:UNIT_WARRIOR at (12,4)|pre_hp:100/100|your HP:100|CS:30",
                "---END---",
            ],
            read_lines=[
                "OUTCOME|att_hp:100|att_max:100|enemy:UNIT_WARRIOR|enemy_hp:100|enemy_max:100",
                "---END---",
            ],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert "Combat could not resolve" in result

    def test_city_capture_reported(self):
        conn = _StubConnection(
            [
                "OK:MELEE_ATTACK|target:DISTRICT_CITY_CENTER at (12,4)|pre_hp:200/200|your HP:100|CS:30",
                "---END---",
            ],
            read_lines=[
                "OUTCOME|att_hp:100|att_max:100|enemy:CAPTURED_CITY",
                "---END---",
            ],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert "city CAPTURED" in result

    def test_district_damage_resolves(self):
        # Walls take the damage; the formatter covers district stats out of
        # the box because they ride the enemy* variables
        conn = _StubConnection(
            [
                "OK:RANGE_ATTACK|target:DISTRICT_CITY_CENTER at (12,4)|pre_hp:100/100|your HP:100|range:2 dist:2",
                "---END---",
            ],
            read_lines=[
                "OUTCOME|att_hp:100|att_max:100|enemy:DISTRICT_CITY_CENTER|enemy_hp:82|enemy_max:100",
                "---END---",
            ],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert "enemy HP: 100 -> 82/100" in result
        assert "your HP: 100 -> 100" in result

    def test_melee_damage_resolves_normally(self):
        conn = _StubConnection(
            [
                "OK:MELEE_ATTACK|target:UNIT_WARRIOR at (12,4)|pre_hp:100/100|your HP:100|CS:30",
                "---END---",
            ],
            read_lines=[
                "OUTCOME|att_hp:86|att_max:100|enemy:UNIT_WARRIOR|enemy_hp:62|enemy_max:100",
                "---END---",
            ],
        )
        gs = GameState.__new__(GameState)
        gs.conn = conn
        result = asyncio.run(gs.attack_unit(7, 12, 4))
        assert "enemy HP: 100 -> 62/100" in result
        assert "your HP: 100 -> 86" in result
        assert "could not resolve" not in result
