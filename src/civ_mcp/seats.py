"""Seat registry — maps each connected MCP client to the player it drives.

In handoff mode one MCP server process owns the single FireTuner connection and
serves several agents at once (plus a human playing in the game UI).  Each MCP
session claims a *seat*: the player id it is allowed to issue commands for.

Two things hang off a seat:

* **Per-player state.** ``GameState`` carries player-scoped caches (last
  snapshot, pending end-turn flag, diary bookkeeping, save-load history), so
  every seat gets its own instance sharing the one ``GameConnection``.  Same
  for the logger / spatial tracker / map capture, which produce per-agent
  telemetry.

* **Perspective.** Read queries hardcode ``Game.GetLocalPlayer()``, which under
  handoff resolves to whoever currently holds the human slot.  An agent reading
  during someone else's turn would otherwise see *their* empire.  The active
  seat is published in a ``ContextVar`` and :mod:`civ_mcp.connection` rewrites
  that expression to the seat's player id, so reads always answer for the
  caller's own civ.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from civ_mcp.game_state import GameState
    from civ_mcp.logger import GameLogger
    from civ_mcp.map_capture import MapCapture
    from civ_mcp.spatial import SpatialTracker
    from civ_mcp.telemetry import TelemetryEmitter

log = logging.getLogger(__name__)

# Player id whose perspective read queries should answer for, or None to leave
# Game.GetLocalPlayer() alone.  Set per tool call; ContextVars are per-task, so
# concurrent sessions never see each other's value.
_view_player: ContextVar[int | None] = ContextVar("civ_mcp_view_player", default=None)


def get_view_player() -> int | None:
    """Player id the current tool call reads as, or None for the local player."""
    return _view_player.get()


def set_view_player(player_id: int | None) -> None:
    """Answer read queries as ``player_id`` for the rest of this tool call.

    Each MCP request is handled in its own task with its own context copy, so
    this never leaks into another session's calls or into background services.
    """
    _view_player.set(player_id)


@contextlib.contextmanager
def view_as(player_id: int | None) -> Iterator[None]:
    """Answer read queries as ``player_id`` for the duration of the block."""
    token = _view_player.set(player_id)
    try:
        yield
    finally:
        _view_player.reset(token)


@dataclass
class PendingTurnReport:
    """Baseline captured when a seat ended its turn, for the next turn's diff.

    The turn counter does not advance when an agent ends its turn — play just
    moves to the next civ in the round.  So the post-turn report (snapshot
    diff, threats, empire warnings) is built when the seat gets the human slot
    back, using this baseline.
    """

    snapshot: object | None
    turn_before: int | None
    threats_before: list = field(default_factory=list)


@dataclass
class Seat:
    """One player, and the per-player machinery of whoever is driving it."""

    player_id: int
    game: GameState
    logger: GameLogger
    spatial: SpatialTracker
    map_capture: MapCapture
    label: str = ""
    session_key: int | None = None
    client_name: str = ""
    pending_report: PendingTurnReport | None = None
    # Turn-timer bookkeeping (see the unified tools in server.py): monotonic
    # timestamp when this seat's timer started, and the game turn it started
    # on.  The first get_full_game_state completion while the seat holds the
    # clock starts it; the budget is 100 + turn seconds.
    timer_start: float | None = None
    timer_turn: int | None = None

    @property
    def claimed(self) -> bool:
        return self.session_key is not None

    def describe(self) -> str:
        who = self.label or f"P{self.player_id}"
        if not self.claimed:
            return f"P{self.player_id} {who} — unclaimed"
        client = f" ({self.client_name})" if self.client_name else ""
        return f"P{self.player_id} {who} — claimed{client}"


class SeatRegistry:
    """Fixed set of agent seats plus the default (unseated) game state.

    In classic single-agent mode the registry is empty and every accessor
    returns the default seat, so nothing about the existing behaviour changes.
    """

    def __init__(
        self,
        *,
        default: Seat,
        agent_ids: tuple[int, ...] = (),
        human_id: int = 0,
        factory=None,
    ) -> None:
        self.default = default
        self.human_id = human_id
        self.agent_ids = agent_ids
        self._seats: dict[int, Seat] = {}
        if agent_ids:
            if factory is None:
                raise ValueError("factory is required when agent_ids is non-empty")
            for pid in agent_ids:
                self._seats[pid] = factory(pid)

    @property
    def enabled(self) -> bool:
        """True when this server is arbitrating multiple agent seats."""
        return bool(self.agent_ids)

    @property
    def seats(self) -> list[Seat]:
        return [self._seats[pid] for pid in self.agent_ids]

    def get(self, player_id: int) -> Seat | None:
        return self._seats.get(player_id)

    def for_session(self, session_key: int) -> Seat | None:
        """Seat claimed by this MCP session, if any."""
        for seat in self._seats.values():
            if seat.session_key == session_key:
                return seat
        return None

    def resolve(self, session_key: int | None) -> Seat:
        """Seat for this session, falling back to the default seat."""
        if session_key is None or not self.enabled:
            return self.default
        return self.for_session(session_key) or self.default

    def claim(
        self, player_id: int, session_key: int, client_name: str = ""
    ) -> tuple[Seat | None, str]:
        """Bind a session to a seat.  Returns ``(seat, message)``.

        Re-claiming the seat a session already holds is a no-op.  Taking over a
        seat held by a session that has gone away is not allowed. If an agent
        loses its connection, restart the server and let agents claim seats again.
        """
        seat = self._seats.get(player_id)
        if seat is None:
            available = ", ".join(str(p) for p in self.agent_ids) or "none"
            return None, (
                f"P{player_id} is not an agent seat in this game. "
                f"Agent seats: {available}."
            )
        if seat.session_key is not None and seat.session_key != session_key:
            return None, f"Seat P{player_id} is already claimed by another agent."
        
        existing = self.for_session(session_key)
        if existing is not None and existing is not seat:
            existing.session_key = None
            existing.client_name = ""
        seat.session_key = session_key
        seat.client_name = client_name
        return seat, f"Claimed {seat.describe()}"

    def release(self, session_key: int) -> Seat | None:
        seat = self.for_session(session_key)
        if seat is not None:
            seat.session_key = None
            seat.client_name = ""
        return seat


def session_key(ctx) -> int | None:
    """Stable per-connection key for an MCP request context.

    ``ctx.session`` is one ``ServerSession`` object per client connection for
    both stdio and streamable-http, so its identity distinguishes agents.
    """
    try:
        return id(ctx.session)
    except Exception:  # pragma: no cover — defensive
        return None


def client_name(ctx) -> str:
    """Client name from the MCP handshake, for logging.  Best effort."""
    try:
        info = ctx.session.client_params.clientInfo
        return f"{info.name or '?'} {info.version or ''}".strip()
    except Exception:
        return ""


def build_seat(
    player_id: int,
    conn,
    emitter: TelemetryEmitter,
    label: str = "",
) -> Seat:
    """Create a seat with its own player-scoped state over a shared connection."""
    from civ_mcp.game_state import GameState
    from civ_mcp.logger import GameLogger
    from civ_mcp.map_capture import MapCapture
    from civ_mcp.spatial import SpatialTracker

    game = GameState(conn)
    spatial = SpatialTracker(emitter)
    game.spatial = spatial
    return Seat(
        player_id=player_id,
        game=game,
        logger=GameLogger(emitter),
        spatial=spatial,
        map_capture=MapCapture(emitter),
        label=label,
    )
