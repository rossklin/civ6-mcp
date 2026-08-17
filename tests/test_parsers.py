"""Unit tests for Lua response parsers.

Each parser takes list[str] (pipe-delimited lines from Lua print()) and returns
typed dataclasses. These tests verify the parsing logic with realistic fixtures.
"""

import pytest

from civ_mcp.lua.overview import parse_gameover_response, parse_overview_response
from civ_mcp.lua.units import (
    parse_attack_outcome,
    parse_combat_estimate,
    parse_threat_scan_response,
    parse_units_response,
)
from civ_mcp.lua.notifications import parse_end_turn_blocking


# ---------------------------------------------------------------------------
# parse_gameover_response
# ---------------------------------------------------------------------------


class TestParseGameover:
    def test_game_active(self):
        assert parse_gameover_response(["GAME_ACTIVE"]) is None

    def test_victory(self):
        lines = ["GAME_OVER|VICTORY|Gandhi|SCIENCE|alive|Gandhi"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.is_game_over is True
        assert result.is_defeat is False
        assert result.winner_name == "Gandhi"
        assert result.victory_type == "SCIENCE"
        assert result.player_alive is True
        assert result.winner_leader == "Gandhi"

    def test_defeat(self):
        lines = ["GAME_OVER|DEFEAT|Gilgamesh|DOMINATION|dead|Gilgamesh"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.is_defeat is True
        assert result.victory_type == "DOMINATION"
        assert result.player_alive is False

    def test_empty_lines(self):
        assert parse_gameover_response([]) is None

    def test_minimal_fields(self):
        """Only 2 fields — optional fields should get defaults."""
        lines = ["GAME_OVER|VICTORY"]
        result = parse_gameover_response(lines)
        assert result is not None
        assert result.winner_name == "Unknown"
        assert result.victory_type == "Unknown"


# ---------------------------------------------------------------------------
# parse_overview_response
# ---------------------------------------------------------------------------


class TestParseOverview:
    # Minimal 19-field main line: turn|pid|civ|leader|gold|gpt|sci|cul|faith|
    #   research|civic|cities|units|score|favor|fpt|pop|gold_income|maintenance
    MAIN_LINE = (
        "42|0|CIVILIZATION_INDIA|Gandhi|500.0|10.5|25.0|18.0|12.0|"
        "TECH_POTTERY|CIVIC_CODE_OF_LAWS|3|5|120|10|2|15|35.0|24.5"
    )

    def test_basic_fields(self):
        result = parse_overview_response([self.MAIN_LINE])
        assert result.turn == 42
        assert result.player_id == 0
        assert result.civ_name == "CIVILIZATION_INDIA"
        assert result.leader_name == "Gandhi"
        assert result.gold == 500.0
        assert result.gold_per_turn == 10.5
        assert result.science_yield == 25.0
        assert result.culture_yield == 18.0
        assert result.faith == 12.0
        assert result.current_research == "TECH_POTTERY"
        assert result.current_civic == "CIVIC_CODE_OF_LAWS"
        assert result.num_cities == 3
        assert result.num_units == 5
        assert result.score == 120

    def test_rankings(self):
        lines = [
            self.MAIN_LINE,
            "RANK|0|India|120",
            "RANK|1|Sumeria|95",
        ]
        result = parse_overview_response(lines)
        assert result.rankings is not None
        assert len(result.rankings) == 2
        assert result.rankings[0].civ_name == "India"
        assert result.rankings[1].score == 95

    def test_era_info(self):
        lines = [self.MAIN_LINE, "ERA|Classical|15|12|24"]
        result = parse_overview_response(lines)
        assert result.era_name == "Classical"
        assert result.era_score == 15
        assert result.era_dark_threshold == 12
        assert result.era_golden_threshold == 24

    def test_exploration(self):
        lines = [self.MAIN_LINE, "EXPLORE|200|1000"]
        result = parse_overview_response(lines)
        assert result.explored_land == 200
        assert result.total_land == 1000

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty overview response"):
            parse_overview_response([])

    def test_too_few_fields_raises(self):
        with pytest.raises(ValueError, match="expected >=14"):
            parse_overview_response(["1|2|3"])


# ---------------------------------------------------------------------------
# parse_units_response
# ---------------------------------------------------------------------------


class TestParseUnits:
    # Fields: uid|index|name|type|x,y|moves/max|hp/max|cs|rs|charges|targets|promo|upgrade|upgrade_target|upgrade_cost|valid_imps|religion
    WARRIOR = "0|0|Warrior|UNIT_WARRIOR|10,24|2.0/2.0|100/100|20|0|0||0|0|||"
    BUILDER = "1|1|Builder|UNIT_BUILDER|12,22|2.0/2.0|100/100|0|0|3||0|0|||IMPROVEMENT_FARM;IMPROVEMENT_MINE|"

    def test_basic_warrior(self):
        units = parse_units_response([self.WARRIOR])
        assert len(units) == 1
        u = units[0]
        assert u.unit_id == 0
        assert u.name == "Warrior"
        assert u.unit_type == "UNIT_WARRIOR"
        assert u.x == 10
        assert u.y == 24
        assert u.moves_remaining == 2.0
        assert u.health == 100
        assert u.combat_strength == 20
        assert u.ranged_strength == 0
        assert u.build_charges == 0

    def test_builder_with_improvements(self):
        units = parse_units_response([self.BUILDER])
        u = units[0]
        assert u.build_charges == 3
        assert "IMPROVEMENT_FARM" in u.valid_improvements
        assert "IMPROVEMENT_MINE" in u.valid_improvements

    def test_multiple_units(self):
        units = parse_units_response([self.WARRIOR, self.BUILDER])
        assert len(units) == 2

    def test_short_line_skipped(self):
        units = parse_units_response(["too|few|fields"])
        assert len(units) == 0

    def test_targets_legacy_bare(self):
        # Legacy bare "x,y" target form (no estimate) still parses.
        line = "2|2|Archer|UNIT_ARCHER|5,5|2.0/2.0|100/100|25|25|0|14,6;15,7|0|0|||"
        units = parse_units_response([line])
        t = units[0].targets
        assert len(t) == 2
        assert t[0].x == 14 and t[0].y == 6
        assert t[1].x == 15 and t[1].y == 7
        assert t[0].est_damage_to_defender == 0

    def test_targets_with_estimate(self):
        # New format: eName@x,y~hp:N~att:N~def:N~r:0/1~m:mod,mod
        tgt = "UNIT_WARRIOR@14,6~hp:100~att:27~def:20~r:0~m:att Battlecry +7,hills +3"
        line = f"2|2|Archer|UNIT_ARCHER|5,5|2.0/2.0|100/100|25|25|0|{tgt}|0|0|||"
        units = parse_units_response([line])
        t = units[0].targets
        assert len(t) == 1
        assert t[0].unit_type == "UNIT_WARRIOR"
        assert t[0].x == 14 and t[0].y == 6
        assert t[0].hp == 100
        assert t[0].is_ranged is False
        # att 27 vs def 20 -> dmg_to_def > 24; melee -> counter-damage > 0
        assert t[0].est_damage_to_defender > 24
        assert t[0].est_damage_to_attacker > 0
        assert "att Battlecry +7" in t[0].modifiers
        assert "hills +3" in t[0].modifiers

    def test_target_ranged_kill(self):
        # Ranged attacker, enough damage to kill: is_kill True, no counter-damage.
        tgt = "UNIT_WARRIOR@14,6~hp:20~att:30~def:15~r:1~m:"
        line = f"2|2|Archer|UNIT_ARCHER|5,5|2.0/2.0|100/100|25|25|0|{tgt}|0|0|||"
        units = parse_units_response([line])
        t = units[0].targets[0]
        assert t.is_ranged is True
        assert t.est_damage_to_attacker == 0
        assert t.est_damage_to_defender >= 20
        assert t.is_kill is True


# ---------------------------------------------------------------------------
# parse_combat_estimate
# ---------------------------------------------------------------------------


class TestParseCombat:
    def test_melee_combat(self):
        # ESTIMATE|att_type|def_type|eff_att_cs|eff_def_cs|is_ranged|modifiers|my_hp|enemy_hp
        line = "ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0|Flanking +2;Fortified -4|100|100"
        result = parse_combat_estimate([line], att_cs=20, def_cs=20)
        assert result is not None
        assert result.attacker_type == "UNIT_WARRIOR"
        assert result.defender_type == "UNIT_WARRIOR"
        assert result.attacker_cs == 20
        assert result.defender_cs == 20
        assert result.is_ranged is False
        assert "Flanking +2" in result.modifiers
        assert "Fortified -4" in result.modifiers
        # Equal CS: damage should be base (24) for both sides
        assert result.est_damage_to_defender == 24
        assert result.est_damage_to_attacker == 24

    def test_ranged_no_counter(self):
        line = "ESTIMATE|UNIT_ARCHER|UNIT_WARRIOR|25|20|1||100|100"
        result = parse_combat_estimate([line], att_cs=25, def_cs=20)
        assert result is not None
        assert result.is_ranged is True
        assert result.est_damage_to_attacker == 0  # ranged = no counter
        assert result.est_damage_to_defender > 24  # attacker stronger

    def test_no_estimate_line(self):
        assert parse_combat_estimate(["some other line"], att_cs=20, def_cs=20) is None


# ---------------------------------------------------------------------------
# parse_attack_outcome
# ---------------------------------------------------------------------------


class TestParseAttackOutcome:
    def test_enemy_killed(self):
        lines = ["OUTCOME|att_hp:88|att_max:100|enemy:KILLED", "---END---"]
        outcome = parse_attack_outcome(lines)
        assert outcome is not None
        assert outcome.attacker_hp == 88
        assert outcome.attacker_max == 100
        assert outcome.enemy_present is False

    def test_enemy_survives(self):
        lines = ["OUTCOME|att_hp:80|att_max:100|enemy:UNIT_WARRIOR|enemy_hp:31|enemy_max:100"]
        outcome = parse_attack_outcome(lines)
        assert outcome is not None
        assert outcome.enemy_present is True
        assert outcome.enemy_type == "UNIT_WARRIOR"
        assert outcome.enemy_hp == 31
        assert outcome.enemy_max == 100
        assert outcome.attacker_hp == 80

    def test_city_flag(self):
        # CITY| line may arrive before or after OUTCOME|; either order sets is_city.
        lines = ["OUTCOME|att_hp:88|att_max:100|enemy:KILLED", "CITY|1"]
        outcome = parse_attack_outcome(lines)
        assert outcome is not None
        assert outcome.is_city is True

    def test_city_flag_before_outcome(self):
        lines = ["CITY|1", "OUTCOME|att_hp:88|att_max:100|enemy:KILLED"]
        outcome = parse_attack_outcome(lines)
        assert outcome is not None
        assert outcome.is_city is True

    def test_no_outcome_line(self):
        assert parse_attack_outcome(["some other line"]) is None


# ---------------------------------------------------------------------------
# parse_threat_scan_response
# ---------------------------------------------------------------------------


class TestParseThreatScan:
    def test_standard_threat(self):
        line = "THREAT|63|Barbarian|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3|cs:0|uid:42"
        threats = parse_threat_scan_response([line])
        assert len(threats) == 1
        t = threats[0]
        assert t.owner_id == 63
        assert t.owner_name == "Barbarian"
        assert t.unit_type == "UNIT_WARRIOR"
        assert t.x == 15
        assert t.y == 30
        assert t.hp == 100
        assert t.combat_strength == 20
        assert t.distance == 3
        assert t.unit_id == 42

    def test_city_state_threat(self):
        line = (
            "THREAT|10|Zanzibar|UNIT_ARCHER|8,12|80/100|CS:25|RS:25|dist:2|cs:1|uid:5"
        )
        threats = parse_threat_scan_response([line])
        assert threats[0].is_city_state is True

    def test_non_threat_lines_skipped(self):
        threats = parse_threat_scan_response(["SOME_OTHER_LINE", "ALSO_NOT_THREAT"])
        assert len(threats) == 0

    def test_legacy_format(self):
        """Older format without owner_id/owner_name."""
        line = "THREAT|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3"
        threats = parse_threat_scan_response([line])
        assert len(threats) == 1
        assert threats[0].unit_type == "UNIT_WARRIOR"
        assert threats[0].x == 15

# ---------------------------------------------------------------------------
# parse_end_turn_blocking
# ---------------------------------------------------------------------------


class TestParseEndTurnBlocking:
    def test_none(self):
        assert parse_end_turn_blocking(["NONE"]) == []

    def test_single_blocker(self):
        blockers = parse_end_turn_blocking(
            ["BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24"]
        )
        assert len(blockers) == 1
        assert blockers[0] == ("UNIT_NEEDS_ORDERS", "Warrior at 10,24")

    def test_multiple_blockers(self):
        lines = [
            "BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24",
            "BLOCKING|CHOOSE_PRODUCTION|Delhi needs production",
        ]
        blockers = parse_end_turn_blocking(lines)
        assert len(blockers) == 2

    def test_empty_lines(self):
        assert parse_end_turn_blocking([]) == []
