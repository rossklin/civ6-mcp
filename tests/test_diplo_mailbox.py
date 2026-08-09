"""Tests for the server-side diplomacy mailbox (diplo_mailbox.py) and its
wiring in :mod:`civ_mcp.server`.

Covers:
  - ``PendingDiploProposal`` defaults and the ``key`` property.
  - ``DiploMailbox`` lifecycle: unlike ``DealMailbox``, ``accept``/``reject``
    *keep* the proposal so the proposer can drain it next turn.
  - ``DiploMailbox`` queries: ``get_pending_for``, ``get_sent_by``,
    ``get_drainable_by``, ``has_pending``.
  - ``execute_commands`` routing: response-able actions to managed civs go to
    the mailbox; one-way actions and non-managed targets fall through to the
    engine.
  - ``respond_to_diplo_action`` accept/reject marking, string coercion, and
    fall-through when no mailbox proposal matches.
  - ``_drain_diplo_proposals``: accepted → executes via the engine path and is
    removed; rejected → reported and removed; pending → left alone.
  - ``_mailbox_propose_diplo`` filing + the None-mailbox guard.
"""

import asyncio
import json
import types

from civ_mcp import server, seats as seats_mod
from civ_mcp.diplo_mailbox import DiploMailbox, PendingDiploProposal
from civ_mcp.handoff import HandoffConfig
from civ_mcp.lua.diplomacy import RESPONSEABLE_DIPLO_ACTIONS
from civ_mcp.seats import Seat, SeatRegistry


# ---------------------------------------------------------------------------
# PendingDiploProposal
# ---------------------------------------------------------------------------


class TestPendingDiploProposal:
    def test_key_is_from_to_tuple(self):
        p = PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"
        )
        assert p.key == (1, 0)

    def test_defaults(self):
        p = PendingDiploProposal()
        assert p.status == "pending"
        assert p.from_player == -1
        assert p.to_player == -1
        assert p.action_name == ""
        assert p.proposed_by == ""
        assert p.proposal_id == ""

    def test_has_no_session_string_field(self):
        """session_string was removed — the action_name is remapped to a
        session string at execution time via DIPLO_SESSION_STRING_MAP, so
        storing it on the proposal was dead weight."""
        p = PendingDiploProposal(from_player=1, to_player=0)
        assert not hasattr(p, "session_string")


# ---------------------------------------------------------------------------
# DiploMailbox — lifecycle (accept/reject KEEP, unlike DealMailbox)
# ---------------------------------------------------------------------------


class TestDiploMailboxLifecycle:
    def test_propose_assigns_id_if_empty(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        assert len(pid) == 12  # uuid hex[:12]
        assert mb.get(pid).proposal_id == pid

    def test_propose_preserves_existing_id(self):
        mb = DiploMailbox()
        p = PendingDiploProposal(
            proposal_id="my-id", from_player=1, to_player=0
        )
        assert mb.propose(p) == "my-id"

    def test_get_returns_none_for_unknown(self):
        assert DiploMailbox().get("nope") is None

    def test_accept_marks_accepted_and_keeps(self):
        """Key difference from DealMailbox: accept keeps the proposal so the
        proposer can drain (execute) it on its next turn."""
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        result = mb.accept(pid)
        assert result is not None
        assert result.status == "accepted"
        # Still present — not removed.
        assert mb.get(pid) is result
        assert mb.pending_count == 1

    def test_reject_marks_rejected_and_keeps(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        result = mb.reject(pid)
        assert result is not None
        assert result.status == "rejected"
        assert mb.get(pid) is result
        assert mb.pending_count == 1

    def test_accept_unknown_returns_none(self):
        assert DiploMailbox().accept("nope") is None

    def test_reject_unknown_returns_none(self):
        assert DiploMailbox().reject("nope") is None

    def test_remove_drops_proposal(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        assert mb.remove(pid) is not None
        assert mb.get(pid) is None
        assert mb.pending_count == 0

    def test_remove_unknown_returns_none(self):
        assert DiploMailbox().remove("nope") is None

    def test_expire_drops_proposal(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        assert mb.expire(pid) is not None
        assert mb.pending_count == 0


# ---------------------------------------------------------------------------
# DiploMailbox — queries
# ---------------------------------------------------------------------------


class TestDiploMailboxQueries:
    def test_get_pending_for_filters_by_target_and_status(self):
        mb = DiploMailbox()
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))  # pending
        mb.propose(PendingDiploProposal(from_player=2, to_player=0))  # pending
        mb.propose(PendingDiploProposal(from_player=1, to_player=2))  # other target
        pending = mb.get_pending_for(0)
        assert len(pending) == 2
        assert all(p.to_player == 0 and p.status == "pending" for p in pending)

    def test_get_pending_for_excludes_answered(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        mb.accept(pid)
        # No longer pending once accepted.
        assert mb.get_pending_for(0) == []

    def test_get_sent_by_filters_by_sender_any_status(self):
        mb = DiploMailbox()
        a = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        mb.propose(PendingDiploProposal(from_player=1, to_player=2))
        mb.propose(PendingDiploProposal(from_player=2, to_player=0))
        mb.accept(a)  # still "sent by" 1
        sent = mb.get_sent_by(1)
        assert len(sent) == 2
        assert all(p.from_player == 1 for p in sent)

    def test_get_drainable_by_filters_by_sender_and_status(self):
        mb = DiploMailbox()
        acc = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        rej = mb.propose(PendingDiploProposal(from_player=1, to_player=2))
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))  # pending
        mb.propose(PendingDiploProposal(from_player=2, to_player=0))  # other sender
        mb.accept(acc)
        mb.reject(rej)
        drainable = mb.get_drainable_by(1)
        assert len(drainable) == 2
        assert all(p.status in ("accepted", "rejected") for p in drainable)

    def test_get_drainable_by_excludes_pending(self):
        mb = DiploMailbox()
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))  # pending
        assert mb.get_drainable_by(1) == []

    def test_has_pending_true(self):
        mb = DiploMailbox()
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        assert mb.has_pending(1, 0) is True

    def test_has_pending_false_after_answer(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        mb.accept(pid)
        assert mb.has_pending(1, 0) is False

    def test_has_pending_false_for_wrong_pair(self):
        mb = DiploMailbox()
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        assert mb.has_pending(1, 2) is False
        assert mb.has_pending(2, 0) is False

    def test_pending_count(self):
        mb = DiploMailbox()
        assert mb.pending_count == 0
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        mb.propose(PendingDiploProposal(from_player=2, to_player=0))
        assert mb.pending_count == 2

    def test_all_pending_returns_copy(self):
        mb = DiploMailbox()
        mb.propose(PendingDiploProposal(from_player=1, to_player=0))
        snapshot = mb.all_pending()
        assert len(snapshot) == 1
        # Mutating the returned list must not touch the mailbox.
        snapshot.clear()
        assert mb.pending_count == 1


# ---------------------------------------------------------------------------
# Shared fixtures for server routing tests
# ---------------------------------------------------------------------------


async def _passthrough_logged(ctx, name, params, fn, **kw):
    """Bypass turn-gating/logging so the inner _run is exercised directly."""
    return await fn()


def _make_ctx(agent_id=1, human_id=0, agent_ids=(1,)):
    """A minimal MCP Context whose app carries a real HandoffConfig and a real
    DiploMailbox.  The agent ``agent_id`` claims its seat so ``_get_seat``
    resolves to it.  ``managed_ids`` is therefore (human_id, *agent_ids)."""
    def factory(pid):
        return Seat(
            player_id=pid,
            game=types.SimpleNamespace(conn=object()),
            logger=types.SimpleNamespace(),
            spatial=types.SimpleNamespace(),
            map_capture=types.SimpleNamespace(),
        )

    cfg = HandoffConfig(enabled=True, human_id=human_id, agent_ids=agent_ids)
    reg = SeatRegistry(
        default=factory(human_id), agent_ids=agent_ids,
        human_id=human_id, factory=factory,
    )
    app = types.SimpleNamespace(
        seats=reg,
        handoff_config=cfg,
        mailbox=object(),
        diplo_mailbox=DiploMailbox(),
    )
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=app),
        session=types.SimpleNamespace(),
    )
    seat, msg = reg.claim(agent_id, seats_mod.session_key(ctx))
    assert seat is not None, msg
    return ctx, app


def _patch_engine(monkeypatch):
    """Patch _logged (turn-gating) and the engine executor. Returns the list
    of forwarded command batches the fake executor captured."""
    executed = []

    async def fake_exec(gs, js):
        executed.append(js)
        return "engine-ok"

    monkeypatch.setattr(server, "_logged", _passthrough_logged)
    monkeypatch.setattr(server, "_execute_commands", fake_exec)
    return executed


# ---------------------------------------------------------------------------
# execute_commands routing — send_diplomatic_action
# ---------------------------------------------------------------------------


class TestSendDiplomaticActionRouting:
    def test_friendship_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, app = _make_ctx()  # managed_ids = (0, 1); agent is P1
        executed = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "DECLARE_FRIENDSHIP"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert executed == []  # never reached the engine
        proposals = app.diplo_mailbox.all_pending()
        assert len(proposals) == 1
        p = proposals[0]
        assert p.from_player == 1       # the agent (proposer)
        assert p.to_player == 0         # the managed human
        assert p.action_name == "DECLARE_FRIENDSHIP"
        assert p.status == "pending"
        assert "awaiting response" in result

    def test_delegation_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "DIPLOMATIC_DELEGATION"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        p = app.diplo_mailbox.all_pending()[0]
        assert p.action_name == "DIPLOMATIC_DELEGATION"
        assert p.from_player == 1 and p.to_player == 0

    def test_embassy_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "RESIDENT_EMBASSY"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        p = app.diplo_mailbox.all_pending()[0]
        assert p.action_name == "RESIDENT_EMBASSY"

    def test_responseable_to_unmanaged_falls_through(self, monkeypatch):
        """A friendship proposal to a built-in AI civ (P2, unmanaged) goes
        straight to the engine — the AI responding is the correct behaviour."""
        ctx, app = _make_ctx()
        executed = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 2, "action_name": "DECLARE_FRIENDSHIP"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "send_diplomatic_action"
        assert forwarded[0]["params"]["other_player_id"] == 2

    def test_oneway_action_to_managed_falls_through(self, monkeypatch):
        """Denounce is one-way (no target response) — always the engine path,
        even toward a managed civ."""
        ctx, app = _make_ctx()
        executed = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "DENOUNCE"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(executed) == 1
        assert json.loads(executed[0])[0]["params"]["action_name"] == "DENOUNCE"

    def test_war_declaration_to_managed_falls_through(self, monkeypatch):
        ctx, app = _make_ctx()
        executed = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0,
                            "action_name": "DECLARE_SURPRISE_WAR"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(executed) == 1

    def test_mixed_batch_mailbox_and_engine(self, monkeypatch):
        """A mailbox-routed diplo action and a unit move in one batch: the
        diplo goes to the mailbox, the move goes to the executor."""
        ctx, app = _make_ctx()
        executed = _patch_engine(monkeypatch)
        cmds = [
            {"action": "send_diplomatic_action",
             "params": {"other_player_id": 0, "action_name": "DECLARE_FRIENDSHIP"}},
            {"action": "fortify_unit", "params": {"unit_index": 3}},
        ]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 1
        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "fortify_unit"
        assert "awaiting response" in result
        assert "engine-ok" in result


# ---------------------------------------------------------------------------
# execute_commands routing — respond_to_diplo_action
# ---------------------------------------------------------------------------


class TestRespondToDiploAction:
    def _file_incoming(self, app, from_player, to_player, action="DECLARE_FRIENDSHIP"):
        """Pre-file a proposal directed at the agent (the seat's player)."""
        return app.diplo_mailbox.propose(PendingDiploProposal(
            from_player=from_player, to_player=to_player, action_name=action,
        ))

    def test_accept_marks_accepted_and_keeps(self, monkeypatch):
        ctx, app = _make_ctx()  # agent is P1
        _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]
        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        p = app.diplo_mailbox.get(pid)
        assert p.status == "accepted"
        assert app.diplo_mailbox.pending_count == 1  # kept for the proposer's drain
        assert "takes effect on P0's next turn" in result

    def test_reject_marks_rejected(self, monkeypatch):
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": False}}]
        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        p = app.diplo_mailbox.get(pid)
        assert p.status == "rejected"
        assert "Rejected" in result

    def test_accept_string_coercion(self, monkeypatch):
        """accept may arrive as a string from the MCP tool boundary."""
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": "true"}}]
        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.get(pid).status == "accepted"

    def test_no_match_falls_through_to_engine(self, monkeypatch):
        """No pending mailbox proposal → fall through to engine
        respond_to_diplomacy (e.g. a real AI-opened session)."""
        ctx, _ = _make_ctx()
        executed = _patch_engine(monkeypatch)
        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "respond_to_diplo_action"

    def test_only_matches_proposal_from_target(self, monkeypatch):
        """respond_to_diplo_action(other_player_id=X) must match a proposal
        FROM X — not a proposal from some other player also pending."""
        ctx, app = _make_ctx()  # agent is P1
        executed = _patch_engine(monkeypatch)
        # Pending proposal from P2 to the agent, but we respond to P0.
        self._file_incoming(app, from_player=2, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]
        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        # No match for P0 → fall through to engine; P2's proposal untouched.
        assert len(executed) == 1
        assert app.diplo_mailbox.all_pending()[0].from_player == 2
        assert app.diplo_mailbox.all_pending()[0].status == "pending"


# ---------------------------------------------------------------------------
# _drain_diplo_proposals
# ---------------------------------------------------------------------------


class _FakeGameState:
    """Records send_diplomatic_action calls; returns a canned result string."""

    def __init__(self, result="OK:ACCEPTED"):
        self.calls = []
        self._result = result

    async def send_diplomatic_action(self, other_player_id, action):
        self.calls.append((other_player_id, action))
        return self._result


class TestDrainDiploProposals:
    def test_accepted_executes_and_removes(self):
        mb = DiploMailbox()
        p = PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP",
        )
        pid = mb.propose(p)
        mb.accept(pid)
        gs = _FakeGameState()

        lines = asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == [(0, "DECLARE_FRIENDSHIP")]
        assert mb.get(pid) is None  # drained → removed
        assert len(lines) == 1
        assert "took effect" in lines[0]

    def test_directionality_preserved(self):
        """The proposer (from_player) executes; the action targets to_player.
        For a delegation this is what places the PROPOSER's delegation at the
        target — not the reverse."""
        mb = DiploMailbox()
        p = PendingDiploProposal(
            from_player=1, to_player=0, action_name="DIPLOMATIC_DELEGATION",
        )
        pid = mb.propose(p)
        mb.accept(pid)
        gs = _FakeGameState()

        asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == [(0, "DIPLOMATIC_DELEGATION")]

    def test_rejected_reports_and_removes_without_executing(self):
        mb = DiploMailbox()
        p = PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP",
        )
        pid = mb.propose(p)
        mb.reject(pid)
        gs = _FakeGameState()

        lines = asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == []  # never executed
        assert mb.get(pid) is None
        assert len(lines) == 1
        assert "rejected" in lines[0]

    def test_pending_not_drained(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP",
        ))  # still pending — target hasn't answered
        gs = _FakeGameState()

        lines = asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == []
        assert lines == []
        assert mb.get(pid) is not None  # left in place

    def test_empty_when_nothing_drainable(self):
        mb = DiploMailbox()
        gs = _FakeGameState()
        assert asyncio.run(server._drain_diplo_proposals(gs, mb, 1)) == []

    def test_only_drains_this_proposers_proposals(self):
        mb = DiploMailbox()
        # P1's accepted proposal and P2's accepted proposal.
        a1 = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        a2 = mb.propose(PendingDiploProposal(
            from_player=2, to_player=0, action_name="RESIDENT_EMBASSY"))
        mb.accept(a1)
        mb.accept(a2)
        gs = _FakeGameState()

        asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == [(0, "DECLARE_FRIENDSHIP")]
        assert mb.get(a1) is None      # P1's drained
        assert mb.get(a2) is not None  # P2's left for P2's turn

    def test_accepted_and_rejected_drained_together(self):
        mb = DiploMailbox()
        acc = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        rej = mb.propose(PendingDiploProposal(
            from_player=1, to_player=2, action_name="RESIDENT_EMBASSY"))
        mb.accept(acc)
        mb.reject(rej)
        gs = _FakeGameState()

        lines = asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == [(0, "DECLARE_FRIENDSHIP")]  # only the accepted one
        assert mb.pending_count == 0
        assert len(lines) == 2

    def test_execution_failure_surfaces_instead_of_crashing(self):
        mb = DiploMailbox()
        p = PendingDiploProposal(
            from_player=1, to_player=0, action_name="DIPLOMATIC_DELEGATION",
        )
        pid = mb.propose(p)
        mb.accept(pid)

        class _BoomGS:
            async def send_diplomatic_action(self, other, action):
                raise RuntimeError("tuner gone")

        lines = asyncio.run(
            server._drain_diplo_proposals(_BoomGS(), mb, proposer_pid=1)
        )
        assert mb.get(pid) is None  # still removed (no retry)
        assert len(lines) == 1
        assert "execution failed" in lines[0]


# ---------------------------------------------------------------------------
# _mailbox_propose_diplo
# ---------------------------------------------------------------------------


class TestMailboxProposeDiplo:
    def test_files_proposal_and_returns_message(self):
        app = types.SimpleNamespace(diplo_mailbox=DiploMailbox())
        seat = types.SimpleNamespace(player_id=1)

        result = asyncio.run(
            server._mailbox_propose_diplo(app, seat, target=0,
                                          action_name="DECLARE_FRIENDSHIP")
        )

        proposals = app.diplo_mailbox.all_pending()
        assert len(proposals) == 1
        p = proposals[0]
        assert p.from_player == 1
        assert p.to_player == 0
        assert p.action_name == "DECLARE_FRIENDSHIP"
        assert p.proposed_by == "agent"
        assert "awaiting response" in result

    def test_none_mailbox_returns_error(self):
        app = types.SimpleNamespace(diplo_mailbox=None)
        seat = types.SimpleNamespace(player_id=1)
        result = asyncio.run(
            server._mailbox_propose_diplo(app, seat, target=0,
                                          action_name="DECLARE_FRIENDSHIP")
        )
        assert "not available" in result


# ---------------------------------------------------------------------------
# Constants sanity — locks the response-able set
# ---------------------------------------------------------------------------


class TestResponseableActions:
    def test_responseable_set_is_exactly_the_three(self):
        assert RESPONSEABLE_DIPLO_ACTIONS == frozenset(
            {"DECLARE_FRIENDSHIP", "DIPLOMATIC_DELEGATION", "RESIDENT_EMBASSY"}
        )

    def test_oneway_actions_excluded(self):
        for a in ("DENOUNCE", "DECLARE_SURPRISE_WAR", "DECLARE_FORMAL_WAR"):
            assert a not in RESPONSEABLE_DIPLO_ACTIONS
