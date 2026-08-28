"""Tests for the turn timer — the soft pace limit on each seat's turn.

The timer starts when get_full_game_state first completes while the seat is
on the clock, the budget is 100 + <turn number> seconds, and the only
consequence of exceeding it is a yellow-card warning appended to
execute_commands output — the turn is never actually terminated.
"""

import asyncio
import types

from civ_mcp import handoff, server
from civ_mcp.handoff import TurnOwnership
from civ_mcp.seats import Seat


def _stub_seat(player_id: int) -> Seat:
    """A Seat with inert per-player machinery — the timer never touches it."""
    return Seat(
        player_id=player_id,
        game=types.SimpleNamespace(conn=object()),
        logger=types.SimpleNamespace(),
        spatial=types.SimpleNamespace(),
        map_capture=types.SimpleNamespace(),
    )


def _own(turn: int = 12, local_player: int = 1) -> TurnOwnership:
    return TurnOwnership(
        turn=turn, local_player=local_player, handler_installed=True
    )


def _fake_clock(monkeypatch, now: float = 0.0) -> list[float]:
    """Freeze time.monotonic() at a mutable reading the test advances.

    A single stable value rather than a tick sequence: asyncio's Windows
    proactor teardown also reads time.monotonic(), and a finite iterator
    would be exhausted by those stray calls.
    """
    reading = [now]
    monkeypatch.setattr(server.time, "monotonic", lambda: reading[0])
    return reading


class TestStartTurnTimer:
    def test_starts_on_first_orientation_of_a_turn(self, monkeypatch):
        seat = _stub_seat(1)
        _fake_clock(monkeypatch, 100.0)
        server._start_turn_timer(seat, _own(turn=12, local_player=1))
        assert seat.timer_turn == 12
        assert seat.timer_start == 100.0

    def test_orientation_while_off_the_clock_is_ignored(self):
        # Scouting with get_full_game_state during another player's turn
        # must not eat this seat's budget.
        seat = _stub_seat(1)
        server._start_turn_timer(seat, _own(turn=12, local_player=0))
        assert seat.timer_start is None
        assert seat.timer_turn is None

    def test_repeated_orientations_same_turn_do_not_restart(self, monkeypatch):
        seat = _stub_seat(1)
        now = _fake_clock(monkeypatch, 100.0)
        server._start_turn_timer(seat, _own(turn=12, local_player=1))
        now[0] = 500.0
        server._start_turn_timer(seat, _own(turn=12, local_player=1))
        assert seat.timer_start == 100.0

    def test_new_turn_restarts_the_timer(self, monkeypatch):
        seat = _stub_seat(1)
        now = _fake_clock(monkeypatch, 100.0)
        server._start_turn_timer(seat, _own(turn=12, local_player=1))
        now[0] = 900.0
        server._start_turn_timer(seat, _own(turn=13, local_player=1))
        assert seat.timer_turn == 13
        assert seat.timer_start == 900.0

    def test_unknown_turn_is_ignored(self):
        seat = _stub_seat(1)
        server._start_turn_timer(seat, _own(turn=None, local_player=1))
        assert seat.timer_start is None


class TestTurnTimerWarning:
    def _armed(self, monkeypatch, turn: int, local_player: int) -> None:
        async def fake(conn):
            return _own(turn=turn, local_player=local_player)

        monkeypatch.setattr(handoff, "get_ownership", fake)

    def test_over_budget_on_own_turn_draws_yellow_card(self, monkeypatch):
        seat = _stub_seat(1)
        seat.timer_turn = 12  # budget 112s
        seat.timer_start = 0.0
        self._armed(monkeypatch, turn=12, local_player=1)
        _fake_clock(monkeypatch, 120.0)  # 8s over
        warning = asyncio.run(server._turn_timer_warning(seat, object()))
        assert warning is not None
        assert "YELLOW CARD" in warning
        assert "8s" in warning
        assert "terminated" in warning

    def test_exactly_on_budget_is_not_over(self, monkeypatch):
        seat = _stub_seat(1)
        seat.timer_turn = 12
        seat.timer_start = 0.0
        self._armed(monkeypatch, turn=12, local_player=1)
        _fake_clock(monkeypatch, 112.0)  # exactly the budget
        assert asyncio.run(server._turn_timer_warning(seat, object())) is None

    def test_under_budget_has_no_warning(self, monkeypatch):
        seat = _stub_seat(1)
        seat.timer_turn = 12
        seat.timer_start = 0.0
        self._armed(monkeypatch, turn=12, local_player=1)
        _fake_clock(monkeypatch, 50.0)
        assert asyncio.run(server._turn_timer_warning(seat, object())) is None

    def test_stale_timer_from_an_earlier_turn_never_fires(self, monkeypatch):
        # The agent oriented on turn 11 but never re-oriented; on turn 12
        # the old timer must not draw a card.
        seat = _stub_seat(1)
        seat.timer_turn = 11
        seat.timer_start = 0.0
        self._armed(monkeypatch, turn=12, local_player=1)
        _fake_clock(monkeypatch, 10000.0)
        assert asyncio.run(server._turn_timer_warning(seat, object())) is None

    def test_off_the_clock_never_fires(self, monkeypatch):
        seat = _stub_seat(1)
        seat.timer_turn = 12
        seat.timer_start = 0.0
        self._armed(monkeypatch, turn=12, local_player=0)
        _fake_clock(monkeypatch, 10000.0)
        assert asyncio.run(server._turn_timer_warning(seat, object())) is None

    def test_unstarted_timer_has_no_warning(self):
        seat = _stub_seat(1)
        assert asyncio.run(server._turn_timer_warning(seat, object())) is None
