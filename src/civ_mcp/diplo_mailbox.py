"""Server-side diplomacy mailbox — intercepts diplomatic actions to managed civs.

Sister of :mod:`civ_mcp.deal_mailbox`.  A diplomatic action (friendship,
delegation, embassy) opened against a managed civ auto-resolves via the built-in
AI: during a managed civ's turn only that civ is human-type, so every other
player — including other managed civs — is treated as default AI by the engine
and answers a freshly-opened session within seconds.  There is no Lua-visible
moment between "session opened" and "AI decided", so the session must never be
opened at propose time.

Instead the proposal is filed here in Python.  The target records an
accept/reject decision on its own turn — and on accept, the agreed action is
executed *immediately, target-local*: the accepting player is the local player
at that moment, which is exactly what the engine requires (completing responses
must come from the target in the DiplomacyActionView context; see
``server._execute_diplo_agreement`` and DIPLO_EXECUTION_PLAN.md for the
live-verified recipe).  ``accepted`` is therefore only ever a transient status
— one that persists only when execution failed or the process died mid-accept;
a proposal whose recipe ran and verified is ``executed``.

The human flows use the same mailbox:

* **Human -> agent**: the human's button click on the native leader screen is
  intercepted *before* any session exists by the diplo shim
  (:mod:`civ_mcp.lua` diplo_shim.lua in the DiplomacyActionView state) and
  filed here with ``proposed_by="human"``.  The agent target answers via the
  mailbox; an accepted proposal is executed right then on the agent's turn
  (the agent is local), and the human hears the outcome via chat at their next
  slot; a rejected one is reported the same way.
* **Agent -> human**: the agent files the proposal here; the human gets a
  clickable notification and answers on the native leader screen (a primed
  synthetic AI-initiated session — recipe steps 1–6 run at notification
  click).  The human's single ``POSITIVE`` response completes the action
  in-engine, so the proposal is marked ``executed`` (not ``accepted``) — the
  proposer's drain then only reports, never re-executes.

Only the three response-able actions are routed here; one-way actions (denounce,
war declarations) go straight to the engine.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class PendingDiploProposal:
    """A diplomatic action waiting for the target's response.

    Unlike a trade deal there are no items — just an action type.  Direction
    matters: the action is always initiated by ``from_player`` (the
    proposer), so the synthetic session is opened from→to (delegation and
    embassy belong to the proposer), while the completing response comes
    from ``to_player`` while it is the local player.
    """

    proposal_id: str = ""
    from_player: int = -1  # the proposer; owns the action's direction
    to_player: int = -1  # the target; records accept/reject and completes
    action_name: str = ""  # DECLARE_FRIENDSHIP | DIPLOMATIC_DELEGATION | RESIDENT_EMBASSY
    turn_proposed: int = 0
    proposed_by: str = ""  # "agent" or "human"
    # pending | accepted | rejected | executed.  Execution happens at accept
    # time on the target's turn, so "accepted" is normally transient: it
    # persists only when the target-local execution failed (or the process
    # died mid-accept).  "executed" = the effect was verified as applied
    # in-engine (recipe verification flip, or the human's own click on the
    # native leader screen); the proposer's drain reports it without
    # re-executing.
    status: str = "pending"

    @property
    def key(self) -> tuple[int, int]:
        return (self.from_player, self.to_player)


class DiploMailbox:
    """Pending diplomatic proposals keyed by ``proposal_id``.

    Lives on the shared :class:`AppContext` alongside :class:`DealMailbox` — not
    on a per-seat ``GameState``, because proposals span seats (an agent proposes
    to the human, the human answers on their own turn).
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingDiploProposal] = {}

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------

    def propose(self, proposal: PendingDiploProposal) -> str:
        """File a proposal. Returns the proposal id."""
        if not proposal.proposal_id:
            proposal.proposal_id = uuid.uuid4().hex[:12]
        self._pending[proposal.proposal_id] = proposal
        log.info(
            "DiploMailbox: P%d->P%d %s proposal %s",
            proposal.from_player,
            proposal.to_player,
            proposal.action_name,
            proposal.proposal_id,
        )
        return proposal.proposal_id

    def get(self, proposal_id: str) -> PendingDiploProposal | None:
        return self._pending.get(proposal_id)

    def accept(self, proposal_id: str) -> PendingDiploProposal | None:
        """Mark a proposal accepted (transient — the caller executes the
        agreed action target-local immediately after, flipping the status to
        ``executed`` on success or removing the proposal on failure)."""
        p = self._pending.get(proposal_id)
        if p:
            p.status = "accepted"
            log.info(
                "DiploMailbox: P%d accepted %s proposal %s from P%d",
                p.to_player,
                p.action_name,
                proposal_id,
                p.from_player,
            )
        return p

    def reject(self, proposal_id: str) -> PendingDiploProposal | None:
        """Mark a proposal rejected (kept for the proposer's drain/report)."""
        p = self._pending.get(proposal_id)
        if p:
            p.status = "rejected"
            log.info(
                "DiploMailbox: P%d rejected %s proposal %s from P%d",
                p.to_player,
                p.action_name,
                proposal_id,
                p.from_player,
            )
        return p

    def mark_executed(self, proposal_id: str) -> PendingDiploProposal | None:
        """Mark a proposal executed in-engine (kept for the proposer's drain).

        Used both when the accept-time recipe's verification flip confirms
        the effect, and when the human answers an agent->human proposal on
        the native leader screen (the single POSITIVE response applies the
        effect right then).  The proposer's drain must report "took effect"
        but never re-execute.
        """
        p = self._pending.get(proposal_id)
        if p:
            p.status = "executed"
            log.info(
                "DiploMailbox: P%d accepted %s proposal %s from P%d "
                "(executed in-engine via native UI)",
                p.to_player,
                p.action_name,
                proposal_id,
                p.from_player,
            )
        return p

    def remove(self, proposal_id: str) -> PendingDiploProposal | None:
        """Drop a proposal after the proposer has drained it."""
        return self._pending.pop(proposal_id, None)

    def expire(self, proposal_id: str) -> PendingDiploProposal | None:
        """Remove a stale proposal (e.g. a player was eliminated)."""
        return self._pending.pop(proposal_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_pending_for(self, player_id: int) -> list[PendingDiploProposal]:
        """Proposals where *player_id* is the target and still awaiting a reply."""
        return [
            p
            for p in self._pending.values()
            if p.to_player == player_id and p.status == "pending"
        ]

    def get_sent_by(self, player_id: int) -> list[PendingDiploProposal]:
        """All proposals *player_id* has sent (any status) — for surfacing."""
        return [p for p in self._pending.values() if p.from_player == player_id]

    def get_drainable_by(self, player_id: int) -> list[PendingDiploProposal]:
        """Proposals *player_id* filed that are answered and ready to drain
        (report-only) on the proposer's turn: execution already happened at
        accept time on the target's turn, so the drain reports ``executed``
        as took-effect, ``rejected`` as declined, and a lingering
        ``accepted`` as incomplete (failed execution, never retried)."""
        return [
            p
            for p in self._pending.values()
            if p.from_player == player_id
            and p.status in ("accepted", "rejected", "executed")
        ]

    def has_pending(self, from_player: int, to_player: int) -> bool:
        return any(
            p.from_player == from_player
            and p.to_player == to_player
            and p.status == "pending"
            for p in self._pending.values()
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def all_pending(self) -> list[PendingDiploProposal]:
        return list(self._pending.values())
