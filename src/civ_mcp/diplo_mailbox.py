"""Server-side diplomacy mailbox — intercepts diplomatic actions to managed civs.

Sister of :mod:`civ_mcp.deal_mailbox`.  A diplomatic action (friendship,
delegation, embassy) opened against a managed civ auto-resolves via the built-in
AI: during a managed civ's turn only that civ is human-type, so every other
player — including other managed civs — is treated as default AI by the engine
and answers a freshly-opened session within seconds.  There is no Lua-visible
moment between "session opened" and "AI decided", so the session must never be
opened at propose time.

Instead the proposal is filed here in Python.  The target records an
accept/reject decision on its own turn (a pure Python signal — no engine
session).  The proposer then executes the action on its *next* turn, when it is
local again, via the existing :func:`build_send_diplo_action` which forces a
``POSITIVE`` response and closes the session same-frame — before the target's
AI can decide.  Because the proposer opens the session, the action's direction
(delegation/embassy belong to the proposer) is correct.

The human flows use the same mailbox with different execution paths:

* **Human -> agent**: the human's button click on the native leader screen is
  intercepted *before* any session exists by the diplo shim
  (:mod:`civ_mcp.lua` diplo_shim.lua in the DiplomacyActionView state) and
  filed here with ``proposed_by="human"``.  The agent target answers via the
  mailbox; an accepted proposal is executed at the start of the human's *next*
  slot (the human is the proposer, so direction is correct), a rejected one is
  reported to the human via chat.
* **Agent -> human**: the agent files the proposal here; the human gets a
  clickable notification and answers on the native leader screen (a synthetic
  AI-initiated session).  The human's single ``POSITIVE`` response completes
  the action in-engine, so the proposal is marked ``executed`` (not
  ``accepted``) — the proposer's drain then only reports, never re-executes.

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
    matters: the action is always initiated by ``from_player`` (the proposer),
    so execution must run as the proposer.
    """

    proposal_id: str = ""
    from_player: int = -1  # the proposer; also the executor
    to_player: int = -1  # the target; records accept/reject
    action_name: str = ""  # DECLARE_FRIENDSHIP | DIPLOMATIC_DELEGATION | RESIDENT_EMBASSY
    turn_proposed: int = 0
    proposed_by: str = ""  # "agent" or "human"
    # pending | accepted | rejected | executed.  "executed" = the effect was
    # already applied in-engine (agent->human proposal answered by the human
    # on the native leader screen); the proposer's drain reports it without
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
        """Mark a proposal accepted (kept for the proposer's execution drain)."""
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

        Used when the human answers an agent->human proposal on the native
        leader screen: the single POSITIVE response applies the effect right
        then, so the proposer's drain must report "took effect" but never
        re-execute.
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
        """Proposals *player_id* filed that are answered and ready to drain on
        the proposer's turn: execute on accept (unless already executed
        in-engine), report on reject/executed."""
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
