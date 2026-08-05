"""Tests for the human-vs-agent turn-boundary handoff.

Covers configuration parsing, the generated GameCore Lua, response parsing,
the read-perspective rewrite, and the wait_for_turn state machine.
"""

import asyncio
import types

import pytest

from civ_mcp import handoff
from civ_mcp.connection import apply_perspective
from civ_mcp.handoff import HandoffConfig, TurnOwnership
from civ_mcp.lua import build_units_query
from civ_mcp.seats import PendingTurnReport, view_as

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestHandoffConfig:
    def test_disabled_by_default(self):
        cfg = HandoffConfig.from_env({})
        assert cfg.enabled is False
        assert cfg.agent_ids == ()

    def test_single_agent(self):
        cfg = HandoffConfig.from_env({"CIV_MCP_AGENT_PLAYERS": "1"})
        assert cfg.enabled is True
        assert cfg.human_id == 0
        assert cfg.agent_ids == (1,)
        assert cfg.managed_ids == (0, 1)

    def test_multiple_agents_with_spaces(self):
        cfg = HandoffConfig.from_env({"CIV_MCP_AGENT_PLAYERS": "1, 2 ,3"})
        assert cfg.agent_ids == (1, 2, 3)
        assert cfg.managed_ids == (0, 1, 2, 3)

    def test_custom_human_slot(self):
        cfg = HandoffConfig.from_env(
            {"CIV_MCP_AGENT_PLAYERS": "0,2", "CIV_MCP_HUMAN_PLAYER": "1"}
        )
        assert cfg.human_id == 1
        assert cfg.managed_ids == (1, 0, 2)

    def test_blank_value_disables(self):
        cfg = HandoffConfig.from_env({"CIV_MCP_AGENT_PLAYERS": "   "})
        assert cfg.enabled is False

    def test_human_cannot_also_be_an_agent(self):
        with pytest.raises(ValueError, match="also listed as an agent"):
            HandoffConfig(enabled=True, human_id=1, agent_ids=(1, 2))

    def test_duplicate_agent_ids_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            HandoffConfig(enabled=True, agent_ids=(1, 1))

    def test_out_of_range_player_rejected(self):
        with pytest.raises(ValueError, match="out of range"):
            HandoffConfig(enabled=True, agent_ids=(62,))


# ---------------------------------------------------------------------------
# Generated Lua
# ---------------------------------------------------------------------------


class TestInstallLua:
    def test_registers_every_managed_player(self):
        cfg = HandoffConfig(enabled=True, human_id=0, agent_ids=(1, 3))
        lua = handoff.build_install_lua(cfg)
        for pid in (0, 1, 3):
            assert f"h.players[{pid}] = true" in lua
        assert "h.players[2] = true" not in lua

    def test_hooks_player_turn_started_once(self):
        lua = handoff.build_install_lua(HandoffConfig(enabled=True, agent_ids=(1,)))
        assert lua.count("GameEvents.PlayerTurnStarted.Add(h.fn)") == 1
        # Guarded so repeated installs update the roster instead of stacking
        # duplicate listeners.
        assert "if not h.fn then" in lua

    def test_never_uses_remove_all(self):
        """RemoveAll would strip the game's own PlayerTurnStarted listeners."""
        cfg = HandoffConfig(enabled=True, agent_ids=(1,))
        for lua in (
            handoff.build_install_lua(cfg),
            handoff.build_install_lua(cfg, force=True),
            handoff.build_uninstall_lua(),
        ):
            assert "RemoveAll" not in lua

    def test_force_removes_before_adding(self):
        cfg = HandoffConfig(enabled=True, agent_ids=(1,))
        assert "Remove(h.fn)" not in handoff.build_install_lua(cfg)
        assert "Remove(h.fn)" in handoff.build_install_lua(cfg, force=True)

    def test_switch_is_pcall_wrapped(self):
        """A raw error in a GameEvents handler would break turn processing."""
        lua = handoff.build_install_lua(HandoffConfig(enabled=True, agent_ids=(1,)))
        assert "pcall(function()" in lua
        assert "PlayerManager.SetLocalPlayerAndObserver(pid)" in lua

    def test_log_ring_buffer_is_bounded(self):
        lua = handoff.build_install_lua(HandoffConfig(enabled=True, agent_ids=(1,)))
        assert "while #s.log > 64 do table.remove(s.log, 1) end" in lua

    def test_all_snippets_terminate_with_the_sentinel(self):
        cfg = HandoffConfig(enabled=True, agent_ids=(1,))
        for lua in (
            handoff.build_install_lua(cfg),
            handoff.build_uninstall_lua(),
            handoff.build_status_lua(),
            handoff.build_log_lua(),
            handoff.build_roster_lua((0, 1)),
            handoff.build_diplomacy_ui_fix_lua(),
        ):
            assert "---END---" in lua


class TestDiplomacyUIFixLua:
    """The screen repair described in docs/human-vs-agent.md."""

    def test_delivers_the_event_the_engine_swallows(self):
        lua = handoff.build_diplomacy_ui_fix_lua()
        # Both events, because their order differs between a normal turn start
        # and a handoff one, and we cannot know which arrives last.
        assert "Events.LocalPlayerChanged.Add(__civmcp_diplo_fix)" in lua
        assert "Events.PlayerTurnActivated.Add(__civmcp_diplo_fix)" in lua
        assert "pcall(OnLocalPlayerTurnBegin)" in lua

    def test_only_repairs_when_the_turn_is_really_active(self):
        """Setting the flag while off the clock would enable actions wrongly."""
        lua = handoff.build_diplomacy_ui_fix_lua()
        assert "p:IsTurnActive() and not g_bIsLocalPlayerTurn" in lua

    def test_registers_listeners_once(self):
        lua = handoff.build_diplomacy_ui_fix_lua()
        assert "if __civmcp_diplo_fix == nil then" in lua

    def test_bails_out_in_a_context_without_the_flag(self):
        lua = handoff.build_diplomacy_ui_fix_lua()
        assert "if g_bIsLocalPlayerTurn == nil then" in lua
        assert "DIPLOFIX|absent" in lua

    def test_targets_only_the_context_that_reads_the_flag(self):
        assert handoff.DIPLOMACY_UI_STATES == ("DiplomacyActionView",)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseStatus:
    def test_full_response(self):
        own = handoff.parse_status(["TURN|42", "LOCAL|1", "HOOK|true", "MANAGED|0,1,2"])
        assert own.turn == 42
        assert own.local_player == 1
        assert own.handler_installed is True
        assert own.managed == (0, 1, 2)

    def test_lua_float_formatting(self):
        """Lua prints integers as floats in some contexts (42.0)."""
        own = handoff.parse_status(["TURN|42.0", "LOCAL|1.0", "HOOK|false"])
        assert own.turn == 42
        assert own.local_player == 1
        assert own.handler_installed is False

    def test_missing_fields_are_none(self):
        own = handoff.parse_status([])
        assert own.turn is None
        assert own.local_player is None
        assert own.handler_installed is False
        assert own.managed == ()

    def test_ignores_unrelated_output(self):
        own = handoff.parse_status(["some debug print", "LOCAL|3"])
        assert own.local_player == 3


class TestParseRoster:
    def test_parses_civ_and_leader(self):
        roster = handoff.parse_roster(
            ["CIV|0|Vietnam|Ba Trieu|true", "CIV|1|Mongolia|Genghis Khan|false"]
        )
        assert roster[0] == ("Vietnam", "Ba Trieu", True)
        assert roster[1] == ("Mongolia", "Genghis Khan", False)

    def test_skips_malformed_rows(self):
        assert handoff.parse_roster(["CIV|0|Vietnam", "junk"]) == {}


# ---------------------------------------------------------------------------
# Read perspective
# ---------------------------------------------------------------------------


class TestApplyPerspective:
    def test_none_is_a_no_op(self):
        code = "local id = Game.GetLocalPlayer()"
        assert apply_perspective(code, None) == code

    def test_rewrites_every_occurrence(self):
        code = "local a = Game.GetLocalPlayer() local b = Game.GetLocalPlayer()"
        assert apply_perspective(code, 2) == "local a = 2 local b = 2"

    def test_rewrites_a_real_builder_query(self):
        """The builders spell the caller's player exactly one way, so a single
        textual rewrite covers all ~100 call sites."""
        query = build_units_query()
        assert "Game.GetLocalPlayer()" in query
        rewritten = apply_perspective(query, 3)
        assert "Game.GetLocalPlayer()" not in rewritten
        assert "local id = 3" in rewritten

    def test_leaves_other_game_calls_alone(self):
        code = "Game.GetCurrentGameTurn() Game.GetLocalPlayer()"
        assert apply_perspective(code, 1) == "Game.GetCurrentGameTurn() 1"

    def test_view_as_scopes_the_rewrite(self):
        from civ_mcp.seats import get_view_player

        assert get_view_player() is None
        with view_as(2):
            assert get_view_player() == 2
        assert get_view_player() is None


# ---------------------------------------------------------------------------
# describe_ownership
# ---------------------------------------------------------------------------


class TestDescribeOwnership:
    cfg = HandoffConfig(enabled=True, human_id=0, agent_ids=(1, 2))

    def _own(self, local):
        return TurnOwnership(turn=7, local_player=local, handler_installed=True)

    def test_your_turn(self):
        text = handoff.describe_ownership(self._own(1), self.cfg, 1)
        assert "you (P1) are on the clock" in text

    def test_human_turn(self):
        text = handoff.describe_ownership(self._own(0), self.cfg, 1)
        assert "human (P0)" in text

    def test_other_agent_turn(self):
        text = handoff.describe_ownership(self._own(2), self.cfg, 1)
        assert "agent P2" in text

    def test_builtin_ai_turn(self):
        text = handoff.describe_ownership(self._own(5), self.cfg, 1)
        assert "built-in AI P5" in text

    def test_unknown_owner(self):
        text = handoff.describe_ownership(self._own(None), self.cfg, 1)
        assert "unknown" in text


# ---------------------------------------------------------------------------
# wait_for_turn
# ---------------------------------------------------------------------------


def _fake_seat(player_id: int, pending=None):
    return types.SimpleNamespace(player_id=player_id, pending_report=pending)


def _fake_gs():
    return types.SimpleNamespace(
        conn=object(), _high_water_turn=0, _advisor_calls_this_turn=99
    )


class TestWaitForTurn:
    """wait_for_turn is the agent's "am I on the clock yet?" loop."""

    cfg = HandoffConfig(enabled=True, human_id=0, agent_ids=(1, 2))

    @staticmethod
    def _run(gs, seat, **kwargs):
        return asyncio.run(
            handoff.wait_for_turn(gs, seat, TestWaitForTurn.cfg, **kwargs)
        )

    @staticmethod
    def _owner(local, turn=9, hook=True):
        async def fake_ownership(conn):
            return TurnOwnership(turn=turn, local_player=local, handler_installed=hook)

        return fake_ownership

    def test_returns_immediately_when_on_the_clock(self, monkeypatch):
        monkeypatch.setattr(handoff, "get_ownership", self._owner(1))
        result = self._run(_fake_gs(), _fake_seat(1), timeout_seconds=1)
        assert "Your turn" in result
        assert "turn 9" in result

    def test_new_turn_resets_the_advisor_budget(self, monkeypatch):
        """execute_end_turn cannot reset it for a seat: the turn counter does
        not move when a seat ends its turn mid-round."""
        monkeypatch.setattr(handoff, "get_ownership", self._owner(1))
        gs = _fake_gs()
        self._run(gs, _fake_seat(1), timeout_seconds=1)
        assert gs._advisor_calls_this_turn == 0
        assert gs._high_water_turn == 9

    def test_times_out_with_status_not_an_error(self, monkeypatch):
        monkeypatch.setattr(handoff, "get_ownership", self._owner(0))
        result = self._run(
            _fake_gs(), _fake_seat(1), timeout_seconds=0, poll_interval=0
        )
        assert "Not your turn yet" in result
        assert "human (P0)" in result

    def test_reports_a_missing_hook_instead_of_waiting_forever(self, monkeypatch):
        monkeypatch.setattr(handoff, "get_ownership", self._owner(0, hook=False))
        result = self._run(_fake_gs(), _fake_seat(1), timeout_seconds=30)
        assert "not installed" in result
        assert "reinstall_handoff" in result

    def test_delivers_the_deferred_turn_report(self, monkeypatch):
        captured = {}

        async def fake_report(gs, snap, turn_before, turn_after, threats):
            captured.update(
                snap=snap,
                turn_before=turn_before,
                turn_after=turn_after,
                threats=threats,
            )
            return "TURN REPORT"

        monkeypatch.setattr(handoff, "get_ownership", self._owner(1, turn=10))
        monkeypatch.setattr("civ_mcp.end_turn.build_post_turn_report", fake_report)

        seat = _fake_seat(
            1, PendingTurnReport(snapshot="snap", turn_before=9, threats_before=["t"])
        )
        result = self._run(_fake_gs(), seat, timeout_seconds=1)
        assert result == "TURN REPORT"
        assert captured == {
            "snap": "snap",
            "turn_before": 9,
            "turn_after": 10,
            "threats": ["t"],
        }
        # Consumed — the next turn must not re-diff against a stale baseline.
        assert seat.pending_report is None

    def test_survives_a_failing_report(self, monkeypatch):
        async def boom(*a, **k):
            raise RuntimeError("no connection")

        monkeypatch.setattr(handoff, "get_ownership", self._owner(1, turn=10))
        monkeypatch.setattr("civ_mcp.end_turn.build_post_turn_report", boom)
        seat = _fake_seat(1, PendingTurnReport(snapshot=None, turn_before=9))
        result = self._run(_fake_gs(), seat, timeout_seconds=1)
        assert "Your turn" in result
        assert "Turn report failed" in result

    def test_polls_until_the_slot_arrives(self, monkeypatch):
        owners = [0, 2, 2, 1]

        async def fake_ownership(conn):
            local = owners.pop(0) if owners else 1
            return TurnOwnership(turn=9, local_player=local, handler_installed=True)

        monkeypatch.setattr(handoff, "get_ownership", fake_ownership)
        result = self._run(
            _fake_gs(), _fake_seat(1), timeout_seconds=30, poll_interval=0
        )
        assert "Your turn" in result
        assert owners == []
