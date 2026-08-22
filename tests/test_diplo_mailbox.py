"""Tests for the server-side diplomacy mailbox (diplo_mailbox.py) and its
wiring in :mod:`civ_mcp.server`.

Covers:
  - ``PendingDiploProposal`` defaults and the ``key`` property.
  - ``DiploMailbox`` lifecycle: unlike ``DealMailbox``, ``accept``/``reject``
    *keep* the proposal so the proposer can drain it next turn;
    ``mark_executed`` marks proposals whose effect registered in-engine.
  - ``DiploMailbox`` queries: ``get_pending_for``, ``get_sent_by``,
    ``get_drainable_by`` (includes ``executed``), ``has_pending``.
  - ``execute_commands`` routing: response-able actions to managed civs (and
    the human) go to the mailbox; agent→human proposals are validity-gated;
    one-way actions and non-managed targets fall through to the engine.
  - ``respond_to_diplo_action``: accept executes the agreement at accept time
    (target-local recipe) — success marks ``executed``, failure removes the
    proposal and surfaces the error; reject marks rejected; fall-through when
    no mailbox proposal matches.
  - ``_execute_diplo_agreement``: recipe step ordering (InGame open+prime,
    DAV nudge, adoption poll, DAV complete, verify, teardown), and the
    failure paths (invalid, no adoption, no verification flip, delegation
    direction).
  - ``_drain_diplo_proposals`` / ``_drain_human_diplo_proposals``:
    report-only (execution happens at accept time on the target's turn).
  - ``_mailbox_propose_diplo`` filing + the None-mailbox guard.
  - Human-side handlers: ``_handle_human_diplo_proposed`` (shim interception
    → mailbox + chat echo), ``_handle_diplo_notification_click`` (recipe
    steps 1–6, flag armed only after adoption), ``_handle_diplo_response``.
  - Diplo shim builders in :mod:`civ_mcp.handoff` and the recipe step
    builders + response-able refusal in :mod:`civ_mcp.lua.diplomacy`.
"""

import asyncio
import json
import types

from civ_mcp import handoff, server, seats as seats_mod
from civ_mcp.diplo_mailbox import DiploMailbox, PendingDiploProposal
from civ_mcp.handoff import HandoffConfig
from civ_mcp.lua import diplomacy as diplo_lua
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


def _patch_engine(monkeypatch, validity=(True, ""), diplo_result=(True, "effect verified as applied")):
    """Patch _logged (turn-gating), the engine executor, the agent→human
    validity pre-check, and the accept-time recipe executor.  Returns a
    record namespace: ``engine`` holds forwarded command batches, ``diplo``
    holds ``(from_player, to_player, action_name)`` tuples for recipe
    executions.  ``validity``/``diplo_result`` seed the respective fakes."""
    rec = types.SimpleNamespace(engine=[], diplo=[])

    async def fake_exec(gs, js):
        rec.engine.append(js)
        return "engine-ok"

    async def fake_validity(gs, target, action_name):
        return validity

    async def fake_diplo(gs, conn, from_player, to_player, action_name):
        rec.diplo.append((from_player, to_player, action_name))
        return diplo_result

    monkeypatch.setattr(server, "_logged", _passthrough_logged)
    monkeypatch.setattr(server, "_execute_commands", fake_exec)
    monkeypatch.setattr(server, "_check_diplo_action_validity", fake_validity)
    monkeypatch.setattr(server, "_execute_diplo_agreement", fake_diplo)
    return rec


# ---------------------------------------------------------------------------
# execute_commands routing — send_diplomatic_action
# ---------------------------------------------------------------------------


class TestSendDiplomaticActionRouting:
    def test_friendship_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, app = _make_ctx()  # managed_ids = (0, 1); agent is P1
        rec = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "DECLARE_FRIENDSHIP"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert rec.engine == []  # never reached the engine
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
        straight to the engine — the AI responding is the correct behaviour.
        (The Lua builder itself then refuses it with a clear ERR: the
        proposer-side flow silently fails — see the builder tests.)"""
        ctx, app = _make_ctx()
        rec = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 2, "action_name": "DECLARE_FRIENDSHIP"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(rec.engine) == 1
        forwarded = json.loads(rec.engine[0])
        assert forwarded[0]["action"] == "send_diplomatic_action"
        assert forwarded[0]["params"]["other_player_id"] == 2

    def test_oneway_action_to_managed_falls_through(self, monkeypatch):
        """Denounce is one-way (no target response) — always the engine path,
        even toward a managed civ."""
        ctx, app = _make_ctx()
        rec = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0, "action_name": "DENOUNCE"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(rec.engine) == 1
        assert json.loads(rec.engine[0])[0]["params"]["action_name"] == "DENOUNCE"

    def test_war_declaration_to_managed_falls_through(self, monkeypatch):
        ctx, app = _make_ctx()
        rec = _patch_engine(monkeypatch)
        cmds = [{"action": "send_diplomatic_action",
                 "params": {"other_player_id": 0,
                            "action_name": "DECLARE_SURPRISE_WAR"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 0
        assert len(rec.engine) == 1

    def test_mixed_batch_mailbox_and_engine(self, monkeypatch):
        """A mailbox-routed diplo action and a unit move in one batch: the
        diplo goes to the mailbox, the move goes to the executor."""
        ctx, app = _make_ctx()
        rec = _patch_engine(monkeypatch)
        cmds = [
            {"action": "send_diplomatic_action",
             "params": {"other_player_id": 0, "action_name": "DECLARE_FRIENDSHIP"}},
            {"action": "fortify_unit", "params": {"unit_index": 3}},
        ]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.pending_count == 1
        assert len(rec.engine) == 1
        forwarded = json.loads(rec.engine[0])
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

    def test_accept_executes_at_accept_time(self, monkeypatch):
        """Accept runs the target-local recipe immediately (this seat IS the
        target and is on its turn): success flips the status to executed —
        kept for the proposer's report drain — with (from=proposer, to=seat)."""
        ctx, app = _make_ctx()  # agent is P1
        rec = _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]
        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert rec.diplo == [(0, 1, "DECLARE_FRIENDSHIP")]
        p = app.diplo_mailbox.get(pid)
        assert p.status == "executed"
        assert app.diplo_mailbox.pending_count == 1  # kept for the proposer's drain
        assert "took effect" in result
        assert rec.engine == []  # nothing fell through to the executor

    def test_accept_execution_failure_drops_proposal(self, monkeypatch):
        """A failed recipe reports the honest error and removes the proposal
        (no retry loops) — the proposer simply never hears 'took effect'."""
        ctx, app = _make_ctx()
        rec = _patch_engine(
            monkeypatch,
            diplo_result=(False, "effect did not register (validity still true)"),
        )
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]
        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.get(pid) is None  # dropped
        assert "did not take effect" in result
        assert "validity still true" in result

    def test_reject_marks_rejected(self, monkeypatch):
        ctx, app = _make_ctx()
        rec = _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": False}}]
        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        p = app.diplo_mailbox.get(pid)
        assert p.status == "rejected"
        assert "Rejected" in result
        assert rec.diplo == []  # no engine execution on reject

    def test_accept_string_coercion(self, monkeypatch):
        """accept may arrive as a string from the MCP tool boundary."""
        ctx, app = _make_ctx()
        _patch_engine(monkeypatch)
        pid = self._file_incoming(app, from_player=0, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": "true"}}]
        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert app.diplo_mailbox.get(pid).status == "executed"

    def test_no_match_falls_through_to_engine(self, monkeypatch):
        """No pending mailbox proposal → fall through to engine
        respond_to_diplomacy (e.g. a real AI-opened session)."""
        ctx, _ = _make_ctx()
        rec = _patch_engine(monkeypatch)
        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert len(rec.engine) == 1
        forwarded = json.loads(rec.engine[0])
        assert forwarded[0]["action"] == "respond_to_diplo_action"

    def test_only_matches_proposal_from_target(self, monkeypatch):
        """respond_to_diplo_action(other_player_id=X) must match a proposal
        FROM X — not a proposal from some other player also pending."""
        ctx, app = _make_ctx()  # agent is P1
        rec = _patch_engine(monkeypatch)
        # Pending proposal from P2 to the agent, but we respond to P0.
        self._file_incoming(app, from_player=2, to_player=1)

        cmds = [{"action": "respond_to_diplo_action",
                 "params": {"other_player_id": 0, "accept": True}}]
        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        # No match for P0 → fall through to engine; P2's proposal untouched.
        assert len(rec.engine) == 1
        assert app.diplo_mailbox.all_pending()[0].from_player == 2
        assert app.diplo_mailbox.all_pending()[0].status == "pending"


# ---------------------------------------------------------------------------
# _execute_diplo_agreement — the target-local recipe
# ---------------------------------------------------------------------------


class _RecipeConn:
    """Scripted conn for the recipe: routes execute_write (InGame steps)
    vs execute_in_named_state (DiplomacyActionView / chat steps) and pops
    scripted line-lists per call in order. Records every call's Lua."""

    def __init__(self, writes=(), named=()):
        self.write_scripts = [list(w) for w in writes]
        self.named_scripts = [list(w) for w in named]
        self.write_calls: list[str] = []
        self.named_calls: list[tuple[str, str]] = []

    async def execute_write(self, lua, perspective=True, timeout=5.0):
        self.write_calls.append(lua)
        return self.write_scripts.pop(0) if self.write_scripts else []

    async def execute_in_named_state(self, state, lua, timeout=5.0):
        self.named_calls.append((state, lua))
        return self.named_scripts.pop(0) if self.named_scripts else []


def _zero_diplo_timings(monkeypatch):
    """Collapse the recipe's inter-step delays so tests run instantly."""
    monkeypatch.setattr(server, "_DIPLO_ADOPT_POLL_INTERVAL", 0.0)
    monkeypatch.setattr(server, "_DIPLO_ADOPT_TIMEOUT", 0.0)
    monkeypatch.setattr(server, "_DIPLO_EFFECT_SETTLE", 0.0)
    monkeypatch.setattr(server, "_DIPLO_TEARDOWN_SETTLE", 0.0)


def _happy_scripts(action="DECLARE_FRIENDSHIP", sid=7):
    """Line scripts for a full happy-path recipe run, (writes, named).

    writes: validity, open, prime, effect check, teardown close, dismiss.
    named: flag pre-clear, nudge, adoption poll, completing response."""
    effect = ["VALID|false", f"STATE|1|1"]
    if action == "DIPLOMATIC_DELEGATION":
        effect = ["VALID|false", "HAS_DELEGATION|true", "STATE|3|3"]
    writes = (
        [f"OK|{action}"],
        [f"OK|OPENED|{sid}"],
        [f"OK|PRIMED|true|sid={sid}"],
        effect,
        [f"OK|CLOSED|{sid}"],
        ["DIPLO_VIEW_DISMISSED"],
    )
    named = (
        ["DIPLO_FLAG_CLEARED"],
        [f"OK|RESPONSE_SENT|{sid}"],
        ["ADOPTED|true"],
        [f"OK|RESPONSE_SENT|{sid}"],
    )
    return writes, named


class TestExecuteDiploAgreement:
    def _run(self, conn):
        return asyncio.run(server._execute_diplo_agreement(
            None, conn, from_player=0, to_player=1,
            action_name="DECLARE_FRIENDSHIP",
        ))

    def test_happy_path_ordering_and_verdict(self, monkeypatch):
        """InGame open+prime → DAV nudge → adoption poll → DAV complete →
        verify → teardown; ok comes from the verification flip only."""
        _zero_diplo_timings(monkeypatch)
        writes, named = _happy_scripts()
        conn = _RecipeConn(writes=writes, named=named)

        ok, msg = self._run(conn)

        assert ok is True
        assert "verified as applied" in msg
        # InGame steps in order: validity, open, prime, effect, close, dismiss.
        assert "IsDiplomaticActionValid" in conn.write_calls[0]
        assert "local me = 0" in conn.write_calls[0]  # acting = proposer
        assert 'RequestSession(from, to, "DECLARE_FRIEND")' in conn.write_calls[1]
        assert "DiplomacyActionTypes.DECLARE_FRIEND" in conn.write_calls[2]
        assert "VALID|" in conn.write_calls[3]  # effect check reads validity
        assert "CloseSession" in conn.write_calls[4]
        assert "HideLeaderScreen" in conn.write_calls[5]
        # DAV steps in order: flag clear, nudge, adoption poll, complete.
        states = [s for s, _ in conn.named_calls]
        assert states == [handoff.DIPLO_SHIM_STATE] * 4
        assert "__MCP_diplo_proposal_id = nil" in conn.named_calls[0][1]
        assert 'AddResponse(7, 1, "POSITIVE")' in conn.named_calls[1][1]
        assert "ms_ActiveSessionID == 7" in conn.named_calls[2][1]
        assert 'AddResponse(7, 1, "POSITIVE")' in conn.named_calls[3][1]

    def test_delegation_requires_has_delegation_at(self, monkeypatch):
        """Delegations verify validity flip AND HasDelegationAt(from→to) —
        the direction the action creates."""
        _zero_diplo_timings(monkeypatch)
        writes, named = _happy_scripts(action="DIPLOMATIC_DELEGATION")
        conn = _RecipeConn(writes=writes, named=named)

        ok, _ = asyncio.run(server._execute_diplo_agreement(
            None, conn, 0, 1, "DIPLOMATIC_DELEGATION"))

        assert ok is True
        assert "DiplomacyActionTypes.SET_DELEGATION" in conn.write_calls[2]

    def test_delegation_without_direction_flag_fails(self, monkeypatch):
        """Validity flipped but HasDelegationAt is false → not applied."""
        _zero_diplo_timings(monkeypatch)
        writes, named = _happy_scripts(action="DIPLOMATIC_DELEGATION")
        writes = list(writes)
        writes[3] = ["VALID|false", "HAS_DELEGATION|false", "STATE|3|3"]
        conn = _RecipeConn(writes=writes, named=named)

        ok, msg = asyncio.run(server._execute_diplo_agreement(
            None, conn, 0, 1, "DIPLOMATIC_DELEGATION"))

        assert ok is False
        assert "did not register" in msg
        assert "HasDelegationAt False" in msg

    def test_invalid_action_short_circuits_before_opening(self, monkeypatch):
        _zero_diplo_timings(monkeypatch)
        conn = _RecipeConn(
            writes=[["ERR:INVALID|Already friends with this player"]])

        ok, msg = self._run(conn)

        assert ok is False
        assert "not valid" in msg
        assert "Already friends" in msg
        assert len(conn.write_calls) == 1  # nothing opened, no teardown
        assert conn.named_calls == []

    def test_adoption_timeout_fails_with_teardown(self, monkeypatch):
        """The view never adopts: honest failure + bare teardown of the
        half-open session."""
        _zero_diplo_timings(monkeypatch)
        conn = _RecipeConn(
            writes=[["OK|DECLARE_FRIENDSHIP"], ["OK|OPENED|7"],
                    ["OK|PRIMED|true|sid=7"], ["OK|CLOSED|7"],
                    ["DIPLO_VIEW_DISMISSED"]],
            named=[["DIPLO_FLAG_CLEARED"], ["OK|RESPONSE_SENT|7"],
                   ["ADOPTED|false"]],
        )

        ok, msg = self._run(conn)

        assert ok is False
        assert "did not adopt" in msg
        # Teardown ran: close + dismiss after the 3 setup writes.
        assert len(conn.write_calls) == 5
        assert "CloseSession" in conn.write_calls[3]

    def test_no_verification_flip_fails(self, monkeypatch):
        """The completing response was sent but validity stayed true — the
        effect did not register, so ok is False despite all OK prints."""
        _zero_diplo_timings(monkeypatch)
        writes, named = _happy_scripts()
        writes = list(writes)
        writes[3] = ["VALID|true", "STATE|2|2"]
        conn = _RecipeConn(writes=writes, named=named)

        ok, msg = self._run(conn)

        assert ok is False
        assert "did not register" in msg
        assert "STATE|2|2" in msg  # diagnostics surfaced

    def test_completing_response_error_fails(self, monkeypatch):
        _zero_diplo_timings(monkeypatch)
        writes, named = _happy_scripts()
        named = list(named)
        named[3] = ["ERR:RESPONSE_FAILED|boom"]
        conn = _RecipeConn(writes=writes, named=named)

        ok, msg = self._run(conn)

        assert ok is False
        assert "completing response failed" in msg
        assert "boom" in msg

    def test_non_responseable_action_refused(self):
        conn = _RecipeConn()
        ok, msg = asyncio.run(server._execute_diplo_agreement(
            None, conn, 0, 1, "DENOUNCE"))
        assert ok is False
        assert "not a response-able action" in msg
        assert conn.write_calls == [] and conn.named_calls == []


# ---------------------------------------------------------------------------
# _drain_diplo_proposals (report-only — execution happens at accept time)
# ---------------------------------------------------------------------------


class TestDrainDiploProposals:
    def test_executed_reports_took_effect_without_engine(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.mark_executed(pid)

        lines = asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert mb.get(pid) is None
        assert len(lines) == 1
        assert "took effect" in lines[0]

    def test_lingering_accepted_reported_incomplete_and_removed(self):
        """'accepted' persisting means the accept-time execution failed or
        the server died mid-accept: honest line, no retry."""
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DIPLOMATIC_DELEGATION"))
        mb.accept(pid)

        lines = asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert mb.get(pid) is None
        assert len(lines) == 1
        assert "did not complete" in lines[0]

    def test_rejected_reports_and_removes(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.reject(pid)

        lines = asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert mb.get(pid) is None
        assert len(lines) == 1
        assert "rejected" in lines[0]

    def test_pending_not_drained(self):
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP",
        ))  # still pending — target hasn't answered

        lines = asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert lines == []
        assert mb.get(pid) is not None  # left in place

    def test_empty_when_nothing_drainable(self):
        mb = DiploMailbox()
        assert asyncio.run(server._drain_diplo_proposals(mb, 1)) == []

    def test_only_drains_this_proposers_proposals(self):
        mb = DiploMailbox()
        a1 = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        a2 = mb.propose(PendingDiploProposal(
            from_player=2, to_player=0, action_name="RESIDENT_EMBASSY"))
        mb.mark_executed(a1)
        mb.mark_executed(a2)

        asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert mb.get(a1) is None      # P1's drained
        assert mb.get(a2) is not None  # P2's left for P2's turn

    def test_executed_and_rejected_drained_together(self):
        mb = DiploMailbox()
        exe = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        rej = mb.propose(PendingDiploProposal(
            from_player=1, to_player=2, action_name="RESIDENT_EMBASSY"))
        mb.mark_executed(exe)
        mb.reject(rej)

        lines = asyncio.run(server._drain_diplo_proposals(mb, proposer_pid=1))

        assert mb.pending_count == 0
        assert len(lines) == 2
        assert "took effect" in lines[0]
        assert "rejected" in lines[1]


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
    def _file(self, mb, action="DECLARE_FRIENDSHIP"):
        return mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name=action))

    def _run(self, conn, mb, pid, cfg=None):
        asyncio.run(server._handle_diplo_notification_click(
            conn, mb, {"proposal_id": pid}, cfg or _make_cfg(), None))

    def test_runs_recipe_then_arms_flag_after_adoption(self, monkeypatch):
        """Validate → open+prime → nudge → poll adoption → assert not
        applied → arm. The flag is armed only AFTER adoption: the shim
        consumes it on the first reported AddResponse involving a managed
        player, so arming earlier would misreport our own nudge."""
        _zero_diplo_timings(monkeypatch)
        mb = DiploMailbox()
        pid = self._file(mb)
        conn = _RecipeConn(
            writes=[["OK|DECLARE_FRIENDSHIP"], ["OK|OPENED|7"],
                    ["OK|PRIMED|true|sid=7"], ["VALID|true", "STATE|2|2"]],
            named=[["DIPLO_FLAG_CLEARED"], ["OK|RESPONSE_SENT|7"],
                   ["ADOPTED|true"], ["DIPLO_FLAG_SET"]],
        )

        self._run(conn, mb, pid)

        # InGame: validity, open, prime, not-yet-applied check.
        assert "local me = 1" in conn.write_calls[0]  # acting = agent P1
        assert 'RequestSession(from, to, "DECLARE_FRIEND")' in conn.write_calls[1]
        assert "DiplomacyActionTypes.DECLARE_FRIEND" in conn.write_calls[2]
        assert "VALID|" in conn.write_calls[3]
        # DAV: flag clear, nudge (flag NOT armed — no pid in the lua),
        # adoption poll, then the arm carrying the proposal id.
        assert "__MCP_diplo_proposal_id = nil" in conn.named_calls[0][1]
        nudge = conn.named_calls[1][1]
        assert 'AddResponse(7, 0, "POSITIVE")' in nudge  # to = human P0
        assert pid not in nudge
        assert "ms_ActiveSessionID == 7" in conn.named_calls[2][1]
        arm = conn.named_calls[3]
        assert arm[0] == handoff.DIPLO_SHIM_STATE
        assert f'__MCP_diplo_proposal_id = "{pid}"' in arm[1]
        # The arm is the LAST call — nothing executes past it (the human's
        # click completes the proposal natively).
        assert len(conn.named_calls) == 4 and len(conn.write_calls) == 4
        assert mb.get(pid).status == "pending"  # answered by the human later

    def test_invalid_proposal_withdrawn_with_chat_note(self, monkeypatch):
        _zero_diplo_timings(monkeypatch)
        mb = DiploMailbox()
        pid = self._file(mb)
        conn = _RecipeConn(writes=[["ERR:INVALID|Already friends"]])

        self._run(conn, mb, pid)

        assert mb.get(pid) is None  # withdrawn — no doomed-dialogue loop
        chat = conn.named_calls[0]
        assert chat[0] == handoff.CHAT_SHIM_STATE
        assert "no longer valid" in chat[1]

    def test_nudge_auto_applied_marks_executed(self, monkeypatch):
        """Open-risk guard: if the nudge ever applies the effect before the
        human decides, record the honest state instead of arming."""
        _zero_diplo_timings(monkeypatch)
        mb = DiploMailbox()
        pid = self._file(mb)
        conn = _RecipeConn(
            writes=[["OK|DECLARE_FRIENDSHIP"], ["OK|OPENED|7"],
                    ["OK|PRIMED|true|sid=7"], ["VALID|false", "STATE|1|1"],
                    ["OK|CLOSED|7"], ["DIPLO_VIEW_DISMISSED"]],
            named=[["DIPLO_FLAG_CLEARED"], ["OK|RESPONSE_SENT|7"],
                   ["ADOPTED|true"]],
        )

        self._run(conn, mb, pid)

        assert mb.get(pid).status == "executed"
        # No arm happened (nothing left for the human to answer).
        assert all('__MCP_diplo_proposal_id = "' not in lua
                   for _, lua in conn.named_calls)

    def test_adoption_timeout_leaves_proposal_pending(self, monkeypatch):
        """Transient presentation failure: tear the half-open session down,
        keep the proposal — the notification re-sends at the next slot."""
        _zero_diplo_timings(monkeypatch)
        mb = DiploMailbox()
        pid = self._file(mb)
        conn = _RecipeConn(
            writes=[["OK|DECLARE_FRIENDSHIP"], ["OK|OPENED|7"],
                    ["OK|PRIMED|true|sid=7"], ["OK|CLOSED|7"],
                    ["DIPLO_VIEW_DISMISSED"]],
            named=[["DIPLO_FLAG_CLEARED"], ["OK|RESPONSE_SENT|7"],
                   ["ADOPTED|false"]],
        )

        self._run(conn, mb, pid)

        assert mb.get(pid).status == "pending"
        assert "CloseSession" in conn.write_calls[3]  # teardown ran
        assert len(conn.write_calls) == 5  # close + dismiss, no arm

    def test_unknown_proposal_makes_no_calls(self):
        conn, mb = _RecipeConn(), DiploMailbox()

        self._run(conn, mb, "nope")

        assert conn.named_calls == [] and conn.write_calls == []


# ---------------------------------------------------------------------------
# _drain_human_diplo_proposals (report-only chat at the human's slot start)
# ---------------------------------------------------------------------------


class TestDrainHumanDiploProposals:
    def _file_human_proposal(self, mb, status, action="DIPLOMATIC_DELEGATION"):
        pid = mb.propose(PendingDiploProposal(
            from_player=0, to_player=1, action_name=action))
        if status == "executed":
            mb.mark_executed(pid)
        elif status == "accepted":
            mb.accept(pid)
        elif status == "rejected":
            mb.reject(pid)
        return pid

    def test_executed_chats_took_effect_without_engine(self):
        mb = DiploMailbox()
        pid = self._file_human_proposal(mb, "executed")
        conn = _RecipeConn()

        asyncio.run(server._drain_human_diplo_proposals(conn, mb, _make_cfg()))

        assert mb.get(pid) is None
        assert len(conn.write_calls) == 0  # report-only: no engine calls
        chat = conn.named_calls[0]
        assert chat[0] == handoff.CHAT_SHIM_STATE
        assert "took effect" in chat[1]

    def test_rejected_chats_decline(self):
        mb = DiploMailbox()
        pid = self._file_human_proposal(mb, "rejected")
        conn = _RecipeConn()

        asyncio.run(server._drain_human_diplo_proposals(conn, mb, _make_cfg()))

        assert mb.get(pid) is None
        assert len(conn.write_calls) == 0
        assert "declined" in conn.named_calls[0][1]

    def test_lingering_accepted_chats_incomplete(self):
        mb = DiploMailbox()
        self._file_human_proposal(mb, "accepted")
        conn = _RecipeConn()

        asyncio.run(server._drain_human_diplo_proposals(conn, mb, _make_cfg()))

        assert len(conn.write_calls) == 0
        assert "could not be completed" in conn.named_calls[0][1]

    def test_agent_proposals_not_drained_here(self):
        """Proposals FROM agents drain on the agent's own turn
        (_drain_diplo_proposals), not in the human-slot drain."""
        mb = DiploMailbox()
        pid = mb.propose(PendingDiploProposal(
            from_player=1, to_player=0, action_name="DECLARE_FRIENDSHIP"))
        mb.mark_executed(pid)
        conn = _RecipeConn()

        asyncio.run(server._drain_human_diplo_proposals(conn, mb, _make_cfg()))

        assert conn.named_calls == []
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


# ---------------------------------------------------------------------------
# Recipe step builders + response-able refusal (lua/diplomacy.py)
# ---------------------------------------------------------------------------


class TestDiploRecipeBuilders:
    def test_enum_map_covers_exactly_the_responseable_set(self):
        assert set(diplo_lua.DIPLO_ACTION_TO_ENUM) == set(RESPONSEABLE_DIPLO_ACTIONS)
        assert diplo_lua.DIPLO_ACTION_TO_ENUM == {
            "DECLARE_FRIENDSHIP": "DECLARE_FRIEND",
            "DIPLOMATIC_DELEGATION": "SET_DELEGATION",
            "RESIDENT_EMBASSY": "SET_EMBASSY",
        }

    def test_open_step_stale_close_then_request_with_session_string(self):
        lua = diplo_lua.build_diplo_open_step(0, 1, "DECLARE_FRIEND")
        # Stale close keyed on the responder-side pair (to, from)...
        assert "FindOpenSessionID(to, from)" in lua
        assert "DiplomacyManager.CloseSession(stale)" in lua
        # ...then the open with the remapped session string, sid printed.
        assert 'RequestSession(from, to, "DECLARE_FRIEND")' in lua
        assert 'print("OK|OPENED|" .. sid)' in lua
        assert "---END---" in lua

    def test_prime_step_uses_enum_key_and_empty_params(self):
        lua = diplo_lua.build_diplo_prime_step(0, 1, "SET_DELEGATION")
        assert "DiplomacyActionTypes.SET_DELEGATION, {})" in lua
        assert 'print("OK|PRIMED|"' in lua

    def test_response_step_threads_sid_and_target(self):
        lua = diplo_lua.build_diplo_response_step(7, 1)
        assert 'DiplomacyManager.AddResponse(7, 1, "POSITIVE")' in lua
        assert 'print("OK|RESPONSE_SENT|7")' in lua

    def test_adoption_check_compares_ms_active_session_id(self):
        lua = diplo_lua.build_diplo_adoption_check(7)
        assert "ms_ActiveSessionID == 7" in lua
        assert 'print("ADOPTED|" .. tostring' in lua
        # ms_Mode is traced but never gated on (may stay nil after adoption).
        assert "ms_Mode" in lua

    def test_effect_check_direction_and_delegation_flag(self):
        lua = diplo_lua.build_diplo_effect_check(0, 1, "DIPLOMATIC_DELEGATION")
        assert 'Players[from]:GetDiplomacy():IsDiplomaticActionValid(' in lua
        assert "Players[from]:GetDiplomacy():HasDelegationAt(to)" in lua
        assert 'print("HAS_DELEGATION|" .. tostring(hasDel))' in lua
        assert 'print("VALID|" .. tostring(valid))' in lua
        assert 'print("STATE|"' in lua

    def test_effect_check_gates_delegation_line_on_action(self):
        """The HasDelegationAt read is emitted for every action but gated in
        Lua on DIPLOMATIC_DELEGATION, so friendship runs validity + state
        only."""
        lua = diplo_lua.build_diplo_effect_check(0, 1, "DECLARE_FRIENDSHIP")
        assert 'if action == "DIPLOMATIC_DELEGATION" then' in lua

    def test_close_step_is_bare_close_without_hide_events(self):
        lua = diplo_lua.build_diplo_close_step(0, 1)
        assert "DiplomacyManager.FindOpenSessionID(1, 0)" in lua
        assert "DiplomacyManager.CloseSession(sid)" in lua
        # Bare close only — hide events in the same instant leave a stale
        # ms_ActiveSessionID that swallows the next session's statement.
        assert "HideLeaderScreen" not in lua
        assert "ShowIngameUI" not in lua

    def test_validity_check_defaults_to_local_player(self):
        lua = diplo_lua.build_check_diplo_action_validity(1, "DECLARE_FRIENDSHIP")
        assert "local me = Game.GetLocalPlayer()" in lua

    def test_validity_check_accepts_acting_player(self):
        lua = diplo_lua.build_check_diplo_action_validity(
            1, "DECLARE_FRIENDSHIP", acting_player_id=0)
        assert "local me = 0" in lua
        assert "Players[me]:GetDiplomacy()" in lua

    def test_send_diplo_action_refuses_responseable_actions(self):
        for action in RESPONSEABLE_DIPLO_ACTIONS:
            lua = diplo_lua.build_send_diplo_action(2, action)
            assert 'print("ERR:NOT_SUPPORTED|' in lua
            assert "---END---" in lua
            # The broken proposer-side flow never loads (the refusal message
            # mentions AddResponse, but no engine call is ever emitted).
            assert "DiplomacyManager.AddResponse" not in lua
            assert "RequestSession(me, target" not in lua

    def test_send_diplo_action_keeps_one_way_actions(self):
        lua = diplo_lua.build_send_diplo_action(2, "DENOUNCE")
        assert "ERR:NOT_SUPPORTED" not in lua
        assert 'RequestSession(me, target, "DENOUNCE")' in lua
