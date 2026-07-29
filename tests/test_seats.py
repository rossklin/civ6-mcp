"""Tests for seat assignment and turn-ownership gating.

The registry decides which player each connected agent drives; the gate decides
whether that agent may act right now.  Between them they are what stops two
agents (or an agent and the human) issuing orders on the same turn.
"""

import asyncio
import types

from civ_mcp import handoff, server
from civ_mcp import seats as seats_mod
from civ_mcp.handoff import HandoffConfig, TurnOwnership
from civ_mcp.seats import Seat, SeatRegistry


def _stub_seat(player_id: int, label: str = "") -> Seat:
    """A Seat with inert per-player machinery — the registry never touches it."""
    return Seat(
        player_id=player_id,
        game=types.SimpleNamespace(conn=object()),
        logger=types.SimpleNamespace(),
        spatial=types.SimpleNamespace(),
        map_capture=types.SimpleNamespace(),
        label=label,
    )


def _registry(agent_ids=(1, 2), human_id=0) -> SeatRegistry:
    return SeatRegistry(
        default=_stub_seat(human_id, "default"),
        agent_ids=agent_ids,
        human_id=human_id,
        factory=_stub_seat,
    )


# ---------------------------------------------------------------------------
# SeatRegistry
# ---------------------------------------------------------------------------


class TestSeatRegistry:
    def test_classic_mode_is_disabled_and_always_default(self):
        reg = SeatRegistry(default=_stub_seat(0))
        assert reg.enabled is False
        assert reg.seats == []
        assert reg.resolve(12345) is reg.default

    def test_requires_a_factory_for_agent_seats(self):
        try:
            SeatRegistry(default=_stub_seat(0), agent_ids=(1,))
        except ValueError as e:
            assert "factory" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_creates_one_seat_per_agent(self):
        reg = _registry((1, 2, 3))
        assert [s.player_id for s in reg.seats] == [1, 2, 3]
        assert all(not s.claimed for s in reg.seats)

    def test_seats_have_independent_state(self):
        """Per-player caches must not be shared or one agent's snapshot diff
        would be computed against another's baseline."""
        reg = _registry((1, 2))
        assert reg.get(1).game is not reg.get(2).game
        assert reg.get(1) is not reg.default

    def test_claim_binds_a_session(self):
        reg = _registry()
        seat, msg = reg.claim(1, session_key=100, client_name="claude 1.0")
        assert seat is reg.get(1)
        assert seat.claimed
        assert "P1" in msg
        assert reg.for_session(100) is seat
        assert reg.resolve(100) is seat

    def test_unclaimed_session_resolves_to_default(self):
        reg = _registry()
        reg.claim(1, session_key=100)
        assert reg.resolve(999) is reg.default

    def test_claiming_a_non_agent_player_is_refused(self):
        reg = _registry((1, 2))
        seat, msg = reg.claim(0, session_key=100)
        assert seat is None
        assert "not an agent seat" in msg
        assert "1, 2" in msg

    def test_reclaiming_the_same_seat_is_idempotent(self):
        reg = _registry()
        reg.claim(1, session_key=100)
        seat, _ = reg.claim(1, session_key=100)
        assert seat is reg.get(1)
        assert reg.for_session(100) is seat

    def test_switching_seats_releases_the_previous_one(self):
        reg = _registry()
        reg.claim(1, session_key=100)
        reg.claim(2, session_key=100)
        assert reg.get(1).claimed is False
        assert reg.get(2).claimed is True
        assert reg.for_session(100) is reg.get(2)

    def test_two_sessions_hold_different_seats(self):
        reg = _registry()
        reg.claim(1, session_key=100)
        reg.claim(2, session_key=200)
        assert reg.resolve(100).player_id == 1
        assert reg.resolve(200).player_id == 2

    def test_reconnecting_client_can_take_its_seat_back(self):
        """A dropped session leaves the seat bound; the agent must be able to
        reclaim it rather than being locked out of its own civ."""
        reg = _registry()
        reg.claim(1, session_key=100)
        seat, _ = reg.claim(1, session_key=101)
        assert seat.session_key == 101
        assert reg.resolve(100) is reg.default

    def test_release(self):
        reg = _registry()
        reg.claim(1, session_key=100)
        released = reg.release(100)
        assert released is reg.get(1)
        assert not released.claimed
        assert reg.resolve(100) is reg.default

    def test_release_without_a_seat(self):
        assert _registry().release(100) is None

    def test_describe(self):
        reg = _registry()
        assert "unclaimed" in reg.get(1).describe()
        reg.claim(1, session_key=100, client_name="claude")
        assert "claimed (claude)" in reg.get(1).describe()


# ---------------------------------------------------------------------------
# Read perspective plumbing
# ---------------------------------------------------------------------------


class TestViewPlayer:
    def test_defaults_to_none(self):
        assert seats_mod.get_view_player() is None

    def test_set_is_scoped_to_the_task(self):
        """Each MCP request runs in its own task, so one session's perspective
        must not bleed into another's."""

        async def scenario():
            async def agent(pid, seen):
                seats_mod.set_view_player(pid)
                await asyncio.sleep(0)
                seen[pid] = seats_mod.get_view_player()

            seen: dict[int, int | None] = {}
            await asyncio.gather(agent(1, seen), agent(2, seen))
            return seen, seats_mod.get_view_player()

        seen, outer = asyncio.run(scenario())
        assert seen == {1: 1, 2: 2}
        assert outer is None


# ---------------------------------------------------------------------------
# Turn gating
# ---------------------------------------------------------------------------


def _ctx(registry: SeatRegistry, cfg: HandoffConfig, claims: int | None = None):
    """Minimal stand-in for an MCP Context.

    ``claims`` claims that player id for this context's session, using the same
    session key the server derives from the real Context.
    """
    app = types.SimpleNamespace(
        seats=registry,
        handoff_config=cfg,
        game=registry.default.game,
        keeper=None,
        camera=None,
        watchdog=None,
    )
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=app),
        session=types.SimpleNamespace(),
    )
    if claims is not None:
        seat, msg = registry.claim(claims, seats_mod.session_key(ctx))
        assert seat is not None, msg
    return ctx


def _gate(ctx, tool, params=None):
    return asyncio.run(server._check_turn_gate(ctx, tool, params or {}))


class TestTurnGate:
    cfg = HandoffConfig(enabled=True, human_id=0, agent_ids=(1, 2))

    def _armed(self, monkeypatch, local_player):
        async def fake(conn):
            return TurnOwnership(
                turn=12, local_player=local_player, handler_installed=True
            )

        monkeypatch.setattr(handoff, "get_ownership", fake)

    def test_classic_mode_never_gates(self):
        reg = SeatRegistry(default=_stub_seat(0))
        ctx = _ctx(reg, HandoffConfig())
        assert _gate(ctx, "end_turn") is None
        assert _gate(ctx, "unit_action") is None

    def test_unseated_session_must_claim_first(self):
        reg = _registry()
        ctx = _ctx(reg, self.cfg)
        msg = _gate(ctx, "get_units")
        assert msg is not None
        assert "claim_seat" in msg
        assert "P1" in msg and "P2" in msg

    def test_seat_management_tools_stay_open_when_unseated(self):
        reg = _registry()
        ctx = _ctx(reg, self.cfg)
        for tool in ("get_seats", "claim_seat", "get_turn_status", "wait_for_turn"):
            assert _gate(ctx, tool) is None

    def test_reads_allowed_off_the_clock(self, monkeypatch):
        """The design accepts shared visibility so agents can plan while
        waiting; blocking reads would make waiting useless."""
        self._armed(monkeypatch, local_player=0)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        for tool in ("get_units", "get_cities", "get_map_area", "get_diplomacy"):
            assert _gate(ctx, tool) is None

    def test_writes_refused_off_the_clock(self, monkeypatch):
        self._armed(monkeypatch, local_player=0)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        msg = _gate(ctx, "unit_action")
        assert msg is not None
        assert "Not your turn" in msg
        assert "human (P0)" in msg

    def test_writes_refused_during_another_agents_turn(self, monkeypatch):
        self._armed(monkeypatch, local_player=2)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        msg = _gate(ctx, "end_turn")
        assert msg is not None
        assert "agent P2" in msg

    def test_writes_allowed_on_the_clock(self, monkeypatch):
        self._armed(monkeypatch, local_player=1)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        for tool in ("unit_action", "end_turn", "set_city_production", "set_research"):
            assert _gate(ctx, tool) is None

    def test_game_reloading_tools_always_refused(self, monkeypatch):
        """These would throw away the human's session and every other agent's
        progress, so they are refused even on the agent's own turn."""
        self._armed(monkeypatch, local_player=1)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        for tool in ("load_game_save", "load_save", "restart_and_load", "kill_game"):
            msg = _gate(ctx, tool)
            assert msg is not None
            assert "disabled in a shared" in msg

    def test_gamecore_run_lua_is_a_read(self, monkeypatch):
        self._armed(monkeypatch, local_player=0)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        assert _gate(ctx, "run_lua", {"context": "gamecore"}) is None
        assert _gate(ctx, "run_lua", {"context": "ingame"}) is not None

    def test_gate_allows_when_the_game_is_unreachable(self, monkeypatch):
        """A connection failure must surface as a connection error from the real
        call, not as a misleading 'not your turn'."""

        async def unreachable(conn):
            return TurnOwnership(turn=None, local_player=None)

        monkeypatch.setattr(handoff, "try_ownership", unreachable)
        reg = _registry()
        ctx = _ctx(reg, self.cfg, claims=1)
        assert _gate(ctx, "unit_action") is None

    def test_every_state_changing_tool_is_covered(self):
        """The gated set is derived from readOnlyHint, so a new write tool is
        gated by default rather than by remembering to list it."""
        for tool in ("unit_action", "city_action", "end_turn", "propose_trade"):
            assert tool in server._WRITE_TOOLS
        for tool in ("get_units", "get_cities", "get_game_overview"):
            assert tool not in server._WRITE_TOOLS
        # Seat/turn tools are exempt rather than gated.
        assert server._WRITE_TOOLS.isdisjoint(server._GATE_EXEMPT)


# ---------------------------------------------------------------------------
# Shared lifespan
# ---------------------------------------------------------------------------


class TestSharedLifespan:
    """The MCP SDK enters the server lifespan once per client session. Over
    HTTP that means once per connected agent, so the context — connection,
    seat registry, web dashboard — must be built once and shared."""

    def _patch(self, monkeypatch, opened, closed):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_open():
            opened.append(1)
            try:
                yield object()
            finally:
                closed.append(1)

        monkeypatch.setattr(server, "_open_app_context", fake_open)
        monkeypatch.setattr(server, "_shared_ctx", None)
        monkeypatch.setattr(server, "_shared_stack", None)
        monkeypatch.setattr(server, "_shared_refs", 0)
        monkeypatch.setattr(server, "_shared_lock", None)

    def test_concurrent_sessions_share_one_context(self, monkeypatch):
        opened, closed = [], []
        self._patch(monkeypatch, opened, closed)

        async def scenario():
            async with server.lifespan(None) as a:
                async with server.lifespan(None) as b:
                    assert a is b
                    assert len(opened) == 1
                    assert closed == []
                # first session gone, second still open — context must survive
                assert closed == []
            return True

        assert asyncio.run(scenario())
        assert len(opened) == 1
        assert len(closed) == 1

    def test_context_is_rebuilt_after_everyone_disconnects(self, monkeypatch):
        opened, closed = [], []
        self._patch(monkeypatch, opened, closed)

        async def scenario():
            async with server.lifespan(None):
                pass
            async with server.lifespan(None):
                pass

        asyncio.run(scenario())
        assert len(opened) == 2
        assert len(closed) == 2


# ---------------------------------------------------------------------------
# Web dashboard startup
# ---------------------------------------------------------------------------


class TestWebApiIsNonFatal:
    """uvicorn calls sys.exit(1) when it cannot bind. SystemExit is a
    BaseException, so an uncaught one in this background task takes the whole
    MCP server down with it — which is what happened when a second civ-mcp
    process clashed on port 8000."""

    def test_bind_failure_does_not_propagate(self):
        class Exiting:
            async def serve(self):
                raise SystemExit(1)

        asyncio.run(server._serve_web_api(Exiting(), 8000))

    def test_other_errors_do_not_propagate(self):
        class Broken:
            async def serve(self):
                raise OSError("boom")

        asyncio.run(server._serve_web_api(Broken(), 8000))

    def test_cancellation_still_propagates(self):
        class Cancelled:
            async def serve(self):
                raise asyncio.CancelledError

        async def scenario():
            try:
                await server._serve_web_api(Cancelled(), 8000)
            except asyncio.CancelledError:
                return True
            return False

        assert asyncio.run(scenario())
