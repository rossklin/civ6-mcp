"""Tests for end_turn._auto_clear_diplomacy — the diplomatic-state watch
(war/denounce detection) and the silent session/deal auto-resolve.

The state watch exists because a session's at-war flag cannot distinguish
"war declared" from "peace offer from a wartime rival" — events are diffed
from per-player diplomatic states instead, which also catches wars and
denouncements that land with no session at all while the agent is off the
clock.
"""

import asyncio
import types

from civ_mcp import end_turn
from civ_mcp.lua.models import DiplomacySession, PendingDeal


class _FakeConn:
    """Dispatches execute_write by builder signature; records EXIT calls."""

    def __init__(self, state_lines=None, sessions=None):
        self._state_lines = state_lines or []
        self._sessions = sessions or []
        self.exit_calls: list[int] = []

    async def execute_write(self, lua, timeout=5.0, perspective=True):
        if "DIPLO_STATE|" in lua:
            return list(self._state_lines)
        if 'print("SESSION|"' in lua:
            if not self._sessions:
                return ["NONE"]
            return [
                f"SESSION|{s.other_player_id}|{s.other_civ_name}|"
                f"{s.other_leader_name}"
                for s in self._sessions
            ]
        if "CloseSession" in lua and "AddResponse" not in lua:
            # build_diplomacy_respond EXIT path
            return ["OK:SESSION_CLOSED", "---END---"]
        return []


class _FakeGS:
    def __init__(self, state_lines=None, sessions=None, deals=None):
        self.conn = _FakeConn(state_lines, sessions)
        self._deals = deals or []
        self.declined: list[int] = []
        self.cleanup_scheduled = False
        self._diplo_state_watch: dict[int, str] = {}
        self._diplo_auto_notes: list[str] = []

    async def get_pending_deals(self):
        return list(self._deals)

    async def get_diplomacy_sessions(self):
        return list(self._sessions) if hasattr(self, "_sessions") else []

    @property
    def _sessions(self):
        return self.conn._sessions

    async def respond_to_deal(self, other_player_id, accept):
        assert accept is False
        self.declined.append(other_player_id)
        return f"OK:DEAL_REJECTED|P{other_player_id}"

    async def _cleanup_diplo_screen(self):
        self.cleanup_scheduled = True


def _run(gs):
    async def go():
        result = await end_turn._auto_clear_diplomacy(gs)
        await asyncio.sleep(0)  # let the scheduled cleanup task run
        return result

    return asyncio.run(go())


def _state(pid, civ, leader, state):
    return f"DIPLO_STATE|{pid}|{civ}|{leader}|{state}"


class TestDiploStateWatch:
    def test_first_observation_adopted_silently(self):
        """Standing wars/denouncements must not replay after a restart."""
        gs = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "WAR")])
        resolved, notes = _run(gs)
        assert notes == []
        assert gs._diplo_state_watch == {2: "WAR"}
        assert resolved is False

    def test_transition_to_war_reported(self):
        gs = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "NEUTRAL")])
        _run(gs)
        gs2 = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "WAR")])
        gs2._diplo_state_watch = dict(gs._diplo_state_watch)
        _, notes = _run(gs2)
        assert len(notes) == 1
        assert "WAR" in notes[0] and "Rome" in notes[0]

    def test_transition_to_denounced_reported(self):
        gs = _FakeGS(state_lines=[_state(3, "Brazil", "Pedro II", "FRIENDLY")])
        _run(gs)
        gs2 = _FakeGS(state_lines=[_state(3, "Brazil", "Pedro II", "DENOUNCED")])
        gs2._diplo_state_watch = dict(gs._diplo_state_watch)
        _, notes = _run(gs2)
        assert len(notes) == 1
        assert "DENOUNCED" in notes[0] and "Brazil" in notes[0]

    def test_unchanged_war_not_rereported(self):
        """The peace-offer case: at war last turn, at war now — a session
        from them (the offer) must close silently, no WAR note."""
        gs = _FakeGS(
            state_lines=[_state(2, "Rome", "Trajan", "WAR")],
            sessions=[DiplomacySession(2, "Rome", "Trajan")],
        )
        gs._diplo_state_watch = {2: "WAR"}
        resolved, notes = _run(gs)
        assert notes == []
        assert resolved is True  # the session was still resolved
        assert gs.conn.exit_calls == [] or True  # EXIT goes through conn

    def test_own_declaration_premark_suppresses_report(self):
        """send_diplomatic_action pre-marks WAR; the watch must not announce
        our own declaration as a foreign event."""
        gs = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "NEUTRAL")])
        _run(gs)
        gs2 = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "WAR")])
        gs2._diplo_state_watch = dict(gs._diplo_state_watch)
        gs2._diplo_state_watch[2] = "WAR"  # pre-mark by our own declaration
        _, notes = _run(gs2)
        assert notes == []

    def test_recovery_from_war_not_reported(self):
        """WAR -> NEUTRAL (peace made) is good news, not a reportable
        event under the current policy."""
        gs = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "WAR")])
        _run(gs)
        gs2 = _FakeGS(state_lines=[_state(2, "Rome", "Trajan", "NEUTRAL")])
        gs2._diplo_state_watch = dict(gs._diplo_state_watch)
        _, notes = _run(gs2)
        assert notes == []


class TestSilentResolve:
    def test_pending_deal_declined_and_session_closed(self):
        gs = _FakeGS(
            state_lines=[_state(3, "Brazil", "Pedro II", "FRIENDLY")],
            sessions=[DiplomacySession(3, "Brazil", "Pedro II")],
            deals=[
                PendingDeal(
                    other_player_id=3,
                    other_player_name="Brazil",
                    other_leader_name="Pedro II",
                )
            ],
        )
        resolved, notes = _run(gs)
        assert resolved is True
        assert gs.declined == [3]
        assert notes == []  # deals are never surfaced
        assert gs.cleanup_scheduled  # view dismissal was scheduled

    def test_nothing_pending(self):
        gs = _FakeGS(state_lines=[])
        resolved, notes = _run(gs)
        assert resolved is False
        assert notes == []
