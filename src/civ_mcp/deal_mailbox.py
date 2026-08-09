"""Server-side deal mailbox — intercepts trades with managed civs.

Under the human-vs-agent handoff, deals with a managed civ are auto-resolved
by the built-in AI because the whole AI response happens inside a single C++
call (``DealManager.SendWorkingDeal``).  There is no Lua-visible moment between
"proposed" and "AI decided", so the deal must simply never be sent.

The mailbox holds proposals that are waiting for the target's response.  Both
directions (agent→managed, human→managed) write to the same mailbox; agents
read pending proposals through ``get_pending_trades``, and the human is alerted
via in-game notifications on their turn.

No mod is required — every hook point is a writable global in an addressable
Lua state.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SerializedDealItem:
    """One item in a deal proposal, as read from the engine's working deal."""

    item_type: str  # "GOLD", "RESOURCE", "AGREEMENT", "FAVOR", "CITY", "GREAT_WORK"
    from_player_id: int  # who provides this item
    amount: int
    duration: int  # 0 = lump sum, >0 = per-turn
    value_type: int = -1  # resource index, great work type, etc.
    # Agreement subtype. Two encodings coexist:
    #  - agent-constructed items carry the DealAgreementTypes enum NAME
    #    (e.g. "OPEN_BORDERS", "PEACE", "ALLIANCE"); rendered in Lua as
    #    DealAgreementTypes.<name>.
    #  - human-constructed items (read off the engine via item:GetSubType())
    #    carry the enum's integer value; rendered bare.
    sub_type: int | str = -1
    name: str = ""  # resource type string for agent-constructed RESOURCE items
    alliance_type: str = ""  # e.g. "MILITARY" for agent-constructed ALLIANCE items

    @property
    def is_gold_lump(self) -> bool:
        return self.item_type == "GOLD" and self.duration == 0

    @property
    def is_gold_per_turn(self) -> bool:
        return self.item_type == "GOLD" and self.duration > 0


@dataclass
class PendingProposal:
    """A deal waiting for the target's response."""

    proposal_id: str = ""
    from_player: int = -1
    to_player: int = -1
    items_from_proposer: list[SerializedDealItem] = field(default_factory=list)
    items_from_target: list[SerializedDealItem] = field(default_factory=list)
    turn_proposed: int = 0
    # Where the proposal came from — determines the delivery path.
    proposed_by: str = ""  # "agent" or "human"

    @property
    def key(self) -> tuple[int, int]:
        return (self.from_player, self.to_player)


class DealMailbox:
    """Pending proposals keyed by ``(from_player, to_player)``.

    Lives on the shared :class:`AppContext` alongside :class:`SeatRegistry` —
    not on a per-seat ``GameState``, because proposals span seats (an agent
    proposes to the human, the human answers on their own turn).
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingProposal] = {}  # proposal_id -> proposal

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------

    def propose(self, proposal: PendingProposal) -> str:
        """File a proposal. Returns the proposal id."""
        if not proposal.proposal_id:
            proposal.proposal_id = uuid.uuid4().hex[:12]
        self._pending[proposal.proposal_id] = proposal
        log.info(
            "Mailbox: P%d→P%d proposal %s (%d+%d items)",
            proposal.from_player,
            proposal.to_player,
            proposal.proposal_id,
            len(proposal.items_from_proposer),
            len(proposal.items_from_target),
        )
        return proposal.proposal_id

    def get(self, proposal_id: str) -> PendingProposal | None:
        return self._pending.get(proposal_id)

    def accept(self, proposal_id: str) -> PendingProposal | None:
        """Mark a proposal accepted and return it for execution."""
        p = self._pending.pop(proposal_id, None)
        if p:
            log.info(
                "Mailbox: P%d accepted proposal %s from P%d",
                p.to_player,
                proposal_id,
                p.from_player,
            )
        return p

    def reject(self, proposal_id: str) -> PendingProposal | None:
        """Mark a proposal rejected."""
        p = self._pending.pop(proposal_id, None)
        if p:
            log.info(
                "Mailbox: P%d rejected proposal %s from P%d",
                p.to_player,
                proposal_id,
                p.from_player,
            )
        return p

    def expire(self, proposal_id: str) -> PendingProposal | None:
        """Remove a stale proposal (e.g. target civ eliminated)."""
        return self._pending.pop(proposal_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_pending_for(self, player_id: int) -> list[PendingProposal]:
        """All proposals where *player_id* is the target."""
        return [p for p in self._pending.values() if p.to_player == player_id]

    def get_sent_by(self, player_id: int) -> list[PendingProposal]:
        """All proposals *player_id* has sent that are still waiting."""
        return [p for p in self._pending.values() if p.from_player == player_id]

    def get_between(
        self, from_player: int, to_player: int
    ) -> list[PendingProposal]:
        """Proposals from one specific player to another."""
        return [
            p
            for p in self._pending.values()
            if p.from_player == from_player and p.to_player == to_player
        ]

    def has_pending(self, from_player: int, to_player: int) -> bool:
        return any(
            p.from_player == from_player and p.to_player == to_player
            for p in self._pending.values()
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def all_pending(self) -> list[PendingProposal]:
        return list(self._pending.values())
