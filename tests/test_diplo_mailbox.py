"""Tests for the server-side diplomacy mailbox (diplo_mailbox.py) and its
wiring in :mod:`civ_mcp.server`.

Covers:
  - ``PendingDiploProposal`` defaults and the ``key`` property.
  - ``DiploMailbox`` lifecycle: unlike ``DealMailbox``, ``accept``/``reject``
    *keep* the proposal so the proposer can drain it next turn;
    ``mark_executed`` marks agent→human proposals the engine already applied.
  - ``DiploMailbox`` queries: ``get_pending_for``, ``get_sent_by``,
    ``get_drainable_by`` (includes ``executed``), ``has_pending``.
  - ``execute_commands`` routing: response-able actions to managed civs (and
    the human) go to the mailbox; agent→human proposals are validity-gated;
    one-way actions and non-managed targets fall through to the engine.
  - ``respond_to_diplo_action`` accept/reject marking, string coercion, and
    fall-through when no mailbox proposal matches.
  - ``_drain_diplo_proposals``: accepted → executes via the engine path and is
    removed; executed → reported without an engine call; rejected → reported
    and removed; pending → left alone.
  - ``_mailbox_propose_diplo`` filing + the None-mailbox guard.
  - Human-side handlers: ``_handle_human_diplo_proposed`` (shim interception
    → mailbox + chat echo), ``_handle_diplo_notification_click`` (flag armed
    before the synthetic session opens), ``_handle_diplo_response``, and
    ``_drain_human_diplo_proposals`` (human slot-start execution).
  - Diplo shim builders in :mod:`civ_mcp.handoff`.
"""

import asyncio
import json
import types

from civ_mcp import handoff, server, seats as seats_mod
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


def _patch_engine(monkeypatch, validity=(True, "")):
    """Patch _logged (turn-gating), the engine executor, and the agent→human
    validity pre-check. Returns the list of forwarded command batches the
    fake executor captured.  ``validity`` seeds the pre-check result for
    tests that exercise the gating itself."""
    executed = []

    async def fake_exec(gs, js):
        executed.append(js)
        return "engine-ok"

    async def fake_validity(gs, target, action_name):
        return validity

    monkeypatch.setattr(server, "_logged", _passthrough_logged)
    monkeypatch.setattr(server, "_execute_commands", fake_exec)
    monkeypatch.setattr(server, "_check_diplo_action_validity", fake_validity)
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


# ---------------------------------------------------------------------------
# mark_executed (agent→human proposals answered on the native leader screen)
# ---------------------------------------------------------------------------


class TestMarkExecuted:
    def test_marks_executed_and_keeps(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        result = mb.mark_executed(pid)
        assert result is not None
        assert result.status == "executed"
        assert mb.get(pid) is result  # kept for the proposer's drain report

    def test_unknown_returns_none(self):
        assert DiploMailbox().mark_executed("nope") is None

    def test_executed_is_drainable(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.mark_executed(pid)
        assert [p.proposal_id for p in mb.get_drainable_by(1)] == [pid]

    def test_executed_not_pending_for_target(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.mark_executed(pid)
        assert mb.get_pending_for(0) == []


class TestDrainDiploProposalsExecuted:
    def test_executed_reports_without_engine_call(self):
        """Agent→human proposal the human accepted on the native leader
        screen: the engine already applied the effect, so the drain only
        reports — never re-executes."""
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.mark_executed(pid)
        gs = _FakeGameState()

        lines = asyncio.run(server._drain_diplo_proposals(gs, mb, proposer_pid=1))

        assert gs.calls == []  # never re-executed
        assert mb.get(pid) is None
        assert len(lines) == 1
        assert "took effect" in lines[0]
        assert "diplomacy screen" in lines[0]


# ---------------------------------------------------------------------------
# execute_commands routing — agent→human validity gate
# ---------------------------------------------------------------------------


class TestAgentToHumanValidityGate:
    def test_invalid_proposal_not_filed(self, monkeypatch):
        """Agent→human proposals are pre-checked so the human is never asked
        to answer a doomed proposal (already friends, obsolete delegation)."""
        ctx, app = _make_ctx()  # agent P1, human P0
        _patch_engine(monkeypatch, validity=(False, "INVALID|Already friends"))
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0,
                            "action_name": "DECLARE_FRIENDSHIP"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert "not valid" in result
        assert "Already friends" in result

    def test_valid_proposal_filed(self, monkeypatch):
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch, validity=(True, ""))
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0,
                            "action_name": "DECLARE_FRIENDSHIP"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 1
        assert "awaiting response" in result


# ---------------------------------------------------------------------------
# _handle_human_diplo_proposed (human→agent interception callback)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Records execute_in_named_state / execute_write calls."""

    def __init__(self):
        self.named: list[tuple[str, str]] = []
        self.writes: list[str] = []

    async def execute_in_named_state(self, state, lua):
        self.named.append((state, lua))
        return []

    async def execute_write(self, lua, perspective=True):
        self.writes.append(lua)
        return []


def _make_cfg(human_id=0, agent_ids=(1,)):
    return HandoffConfig(enabled=True, human_id=human_id, agent_ids=agent_ids)


class TestHandleHumanDiploProposed:
    def _run(self, conn, mb, data, cfg):
        # The handler is sync but schedules an async chat echo, so it must
        # run inside a loop for the ensure_future to resolve.
        async def _go():
            server._handle_human_diplo_proposed(conn, mb, data, cfg)
            await asyncio.sleep(0)
        asyncio.run(_go())

    def test_files_proposal_with_mapped_action_name(self):
        """The shim reports the session string (DECLARE_FRIEND); the mailbox
        stores the action name (DECLARE_FRIENDSHIP)."""
        conn, mb = _FakeConn(), DiploMailbox()
        data = {"from": 0, "to": 1, "action": "DECLARE_FRIEND"}

        self._run(conn, mb, data, _make_cfg())

        p = mb.all_pending()[0]
        assert p.from_player == 0 and p.to_player == 1
        assert p.action_name == "DECLARE_FRIENDSHIP"
        assert p.proposed_by == "human"
        assert p.status == "pending"

    def test_identity_action_strings_pass_through(self):
        conn, mb = _FakeConn(), DiploMailbox()
        data = {"from": 0, "to": 1, "action": "DIPLOMATIC_DELEGATION"}

        self._run(conn, mb, data, _make_cfg())

        assert mb.all_pending()[0].action_name == "DIPLOMATIC_DELEGATION"

    def test_echoes_confirmation_to_human_chat(self):
        conn, mb = _FakeConn(), DiploMailbox()
        data = {"from": 0, "to": 1, "action": "DECLARE_FRIEND"}

        self._run(conn, mb, data, _make_cfg())

        assert len(conn.named) == 1
        state, lua = conn.named[0]
        assert state == handoff.CHAT_SHIM_STATE
        assert "friendship" in lua

    def test_non_managed_target_ignored(self):
        conn, mb = _FakeConn(), DiploMailbox()
        data = {"from": 0, "to": 5, "action": "DECLARE_FRIEND"}

        self._run(conn, mb, data, _make_cfg())

        assert mb.pending_count == 0
        assert conn.named == []


# ---------------------------------------------------------------------------
# _handle_diplo_response / _handle_diplo_notification_click
# ---------------------------------------------------------------------------


class TestHandleDiploResponse:
    def _file(self, mb):
        return mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))

    def test_positive_marks_executed(self):
        mb = DiploMailbox()
        pid = self._file(mb)
        server._handle_diplo_response(mb, {"proposal_id": pid,
                                           "response": "POSITIVE"})
        assert mb.get(pid).status == "executed"

    def test_negative_marks_rejected(self):
        mb = DiploMailbox()
        pid = self._file(mb)
        server._handle_diplo_response(mb, {"proposal_id": pid,
                                           "response": "NEGATIVE"})
        assert mb.get(pid).status == "rejected"

    def test_ignore_counts_as_rejected(self):
        mb = DiploMailbox()
        pid = self._file(mb)
        server._handle_diplo_response(mb, {"proposal_id": pid,
                                           "response": "RESPONSE_IGNORE"})
        assert mb.get(pid).status == "rejected"

    def test_unknown_id_no_crash(self):
        server._handle_diplo_response(DiploMailbox(),
                                      {"proposal_id": "nope",
                                       "response": "POSITIVE"})


class TestHandleDiploNotificationClick:
    def test_arms_flag_then_opens_session(self):
        conn, mb = _FakeConn(), DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))

        asyncio.run(server._handle_diplo_notification_click(
            conn, mb, {"proposal_id": pid}, _make_cfg(), None))

        # 1. Flag armed in the DiplomacyActionView state...
        assert conn.named[0][0] == handoff.DIPLO_SHIM_STATE
        assert pid in conn.named[0][1]
        # 2. ...BEFORE the synthetic session opens in the gamecore state.
        assert len(conn.writes) == 1
        assert 'DiplomacyManager.RequestSession(1, 0, "DECLARE_FRIEND")' \
            in conn.writes[0]

    def test_unknown_proposal_makes_no_calls(self):
        conn, mb = _FakeConn(), DiploMailbox()

        asyncio.run(server._handle_diplo_notification_click(
            conn, mb, {"proposal_id": "nope"}, _make_cfg(), None))

        assert conn.named == [] and conn.writes == []


# ---------------------------------------------------------------------------
# _drain_human_diplo_proposals (executed at the human's slot start)
# ---------------------------------------------------------------------------


class TestDrainHumanDiploProposals:
    def _file_human_proposal(self, mb, status, action="DIPLOMATIC_DELEGATION"):
        pid = mb.propose(PendingDiploProposal(
            from_player=0, to_player=1, action_name=action))
        if status == "accepted":
            mb.accept(pid)
        elif status == "rejected":
            mb.reject(pid)
        return pid

    def test_accepted_executes_as_human_and_notifies(self):
        mb = DiploMailbox()
        pid = self._file_human_proposal(mb, "accepted")
        conn, gs = _FakeConn(), _FakeGameState()

        asyncio.run(server._drain_human_diplo_proposals(
            gs, conn, mb, _make_cfg()))

        assert gs.calls == [(1, "DIPLOMATIC_DELEGATION")]
        assert mb.get(pid) is None
        chat = conn.named[0]
        assert chat[0] == handoff.CHAT_SHIM_STATE
        assert "took effect" in chat[1]

    def test_rejected_reports_decline_without_executing(self):
        mb = DiploMailbox()
        pid = self._file_human_proposal(mb, "rejected")
        conn, gs = _FakeConn(), _FakeGameState()

        asyncio.run(server._drain_human_diplo_proposals(
            gs, conn, mb, _make_cfg()))

        assert gs.calls == []
        assert mb.get(pid) is None
        assert "declined" in conn.named[0][1]

    def test_engine_error_surfaces_in_chat(self):
        mb = DiploMailbox()
        self._file_human_proposal(mb, "accepted")
        conn = _FakeConn()
        gs = _FakeGameState(result="ERR:INVALID|Not enough gold")

        asyncio.run(server._drain_human_diplo_proposals(
            gs, conn, mb, _make_cfg()))

        assert "could not be completed" in conn.named[0][1]

    def test_agent_proposals_not_drained_here(self):
        """Proposals FROM agents drain on the agent's own turn
        (_drain_diplo_proposals), not in the human-slot drain."""
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.accept(pid)
        conn, gs = _FakeConn(), _FakeGameState()

        asyncio.run(server._drain_human_diplo_proposals(
            gs, conn, mb, _make_cfg()))

        assert gs.calls == []
        assert mb.get(pid) is not None


# ---------------------------------------------------------------------------
# Diplo shim builders (handoff.py)
# ---------------------------------------------------------------------------


class TestDiploShimBuilders:
    def test_install_substitutes_tags(self):
        lua = handoff.build_diplo_shim_install_lua((1, 2))
        assert "__MCP_MANAGED_IDS_TAG__" not in lua
        assert "__MCP_SENTINEL_TAG__" not in lua
        assert "{[1]=true,[2]=true}" in lua
        assert "---END---" in lua

    def test_install_wraps_request_and_response(self):
        lua = handoff.build_diplo_shim_install_lua((1,))
        assert "__MCP_orig_RS" in lua
        assert "__MCP_orig_AR" in lua
        # The three proposal session strings are intercepted; others pass.
        assert "DECLARE_FRIEND" in lua
        assert "MCPDIPLO|PROPOSED" in lua
        assert "MCPDIPLO_RESPONDED" in lua

    def test_uninstall_restores_both_hook_paths(self):
        lua = handoff.build_diplo_shim_uninstall_lua()
        assert "DiplomacyManager.RequestSession = __MCP_orig_RS" in lua
        assert "DiplomacyManager.AddResponse = __MCP_orig_AR" in lua
        assert "OnSelectInitialDiplomacyStatement = __MCP_orig_OIDS" in lua
        assert "OnSelectConversationDiplomacyStatement = __MCP_orig_OCDR" in lua

    def test_health_check_covers_both_hooks(self):
        lua = handoff.build_diplo_shim_health_check_lua()
        assert "__MCP_orig_RS ~= nil" in lua
        assert "__MCP_orig_OIDS ~= nil" in lua

    def test_open_session_uses_session_string(self):
        lua = handoff.build_open_diplo_session_lua(1, 0, "DECLARE_FRIEND")
        assert 'DiplomacyManager.RequestSession(1, 0, "DECLARE_FRIEND")' in lua

    def test_set_flag_carries_proposal_id(self):
        lua = handoff.build_set_diplo_proposal_lua(1, "abc123")
        assert '__MCP_diplo_proposal_id = "abc123"' in lua

    def test_notification_uses_diplo_marker(self):
        lua = handoff.build_send_diplo_notification_lua(
            0, 1, "abc123", "Cleopatra proposes friendship.")
        assert "'MCPDIPLO:abc123'" in lua
        assert "Cleopatra proposes friendship." in lua
