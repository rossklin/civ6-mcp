"""Tests for the server-side deal mailbox (deal_mailbox.py)."""

import pytest

from civ_mcp.deal_mailbox import DealMailbox, PendingProposal, SerializedDealItem


# ---------------------------------------------------------------------------
# SerializedDealItem
# ---------------------------------------------------------------------------


class TestSerializedDealItem:
    def test_gold_lump_detection(self):
        item = SerializedDealItem(
            item_type="GOLD", from_player_id=1, amount=200, duration=0
        )
        assert item.is_gold_lump is True
        assert item.is_gold_per_turn is False

    def test_gold_per_turn_detection(self):
        item = SerializedDealItem(
            item_type="GOLD", from_player_id=0, amount=10, duration=30
        )
        assert item.is_gold_lump is False
        assert item.is_gold_per_turn is True

    def test_non_gold_is_neither(self):
        item = SerializedDealItem(
            item_type="RESOURCE", from_player_id=1, amount=1, duration=30,
            value_type=5
        )
        assert item.is_gold_lump is False
        assert item.is_gold_per_turn is False

    def test_defaults(self):
        item = SerializedDealItem(
            item_type="FAVOR", from_player_id=2, amount=50, duration=0
        )
        assert item.value_type == -1
        assert item.subtype == -1


# ---------------------------------------------------------------------------
# PendingProposal
# ---------------------------------------------------------------------------


class TestPendingProposal:
    def test_key_is_from_to_tuple(self):
        p = PendingProposal(
            proposal_id="abc", from_player=1, to_player=0, turn_proposed=5
        )
        assert p.key == (1, 0)

    def test_items_default_to_empty(self):
        p = PendingProposal(
            proposal_id="abc", from_player=1, to_player=0
        )
        assert p.items_from_proposer == []
        assert p.items_from_target == []


# ---------------------------------------------------------------------------
# DealMailbox — basic CRUD
# ---------------------------------------------------------------------------


class TestDealMailboxBasic:
    def test_propose_assigns_id_if_empty(self):
        mailbox = DealMailbox()
        p = PendingProposal(from_player=1, to_player=0)
        pid = mailbox.propose(p)
        assert len(pid) == 12  # uuid hex
        assert p.proposal_id == pid

    def test_propose_preserves_existing_id(self):
        mailbox = DealMailbox()
        p = PendingProposal(
            proposal_id="my-id", from_player=1, to_player=0
        )
        pid = mailbox.propose(p)
        assert pid == "my-id"

    def test_get_returns_none_for_unknown(self):
        mailbox = DealMailbox()
        assert mailbox.get("nonexistent") is None

    def test_get_returns_proposal(self):
        mailbox = DealMailbox()
        p = PendingProposal(from_player=1, to_player=0)
        pid = mailbox.propose(p)
        assert mailbox.get(pid) is p

    def test_accept_removes_and_returns(self):
        mailbox = DealMailbox()
        p = PendingProposal(from_player=1, to_player=0)
        pid = mailbox.propose(p)
        result = mailbox.accept(pid)
        assert result is p
        assert mailbox.get(pid) is None
        assert mailbox.pending_count == 0

    def test_accept_unknown_returns_none(self):
        mailbox = DealMailbox()
        assert mailbox.accept("nope") is None

    def test_reject_removes_and_returns(self):
        mailbox = DealMailbox()
        p = PendingProposal(from_player=1, to_player=0)
        pid = mailbox.propose(p)
        result = mailbox.reject(pid)
        assert result is p
        assert mailbox.get(pid) is None

    def test_expire_removes(self):
        mailbox = DealMailbox()
        p = PendingProposal(from_player=1, to_player=0)
        pid = mailbox.propose(p)
        mailbox.expire(pid)
        assert mailbox.pending_count == 0


# ---------------------------------------------------------------------------
# DealMailbox — queries
# ---------------------------------------------------------------------------


class TestDealMailboxQueries:
    def test_get_pending_for_filters_by_target(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        mailbox.propose(PendingProposal(from_player=2, to_player=0))
        mailbox.propose(PendingProposal(from_player=1, to_player=2))
        # Only proposals targeting player 0
        pending = mailbox.get_pending_for(0)
        assert len(pending) == 2
        assert all(p.to_player == 0 for p in pending)

    def test_get_pending_for_empty_when_none(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=2))
        assert mailbox.get_pending_for(0) == []

    def test_get_sent_by_filters_by_sender(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        mailbox.propose(PendingProposal(from_player=1, to_player=2))
        mailbox.propose(PendingProposal(from_player=2, to_player=0))
        sent = mailbox.get_sent_by(1)
        assert len(sent) == 2
        assert all(p.from_player == 1 for p in sent)

    def test_get_between_exact_match(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        mailbox.propose(PendingProposal(from_player=2, to_player=0))
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        between = mailbox.get_between(1, 0)
        assert len(between) == 2

    def test_get_between_empty_when_no_match(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        assert mailbox.get_between(2, 0) == []

    def test_has_pending_true(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        assert mailbox.has_pending(1, 0) is True

    def test_has_pending_false(self):
        mailbox = DealMailbox()
        assert mailbox.has_pending(1, 0) is False

    def test_pending_count(self):
        mailbox = DealMailbox()
        assert mailbox.pending_count == 0
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        mailbox.propose(PendingProposal(from_player=2, to_player=0))
        assert mailbox.pending_count == 2
        mailbox.accept(list(mailbox.all_pending())[0].proposal_id)
        assert mailbox.pending_count == 1

    def test_all_pending_returns_copy(self):
        mailbox = DealMailbox()
        mailbox.propose(PendingProposal(from_player=1, to_player=0))
        all_p = mailbox.all_pending()
        assert len(all_p) == 1


# ---------------------------------------------------------------------------
# DealMailbox — concurrency and edge cases
# ---------------------------------------------------------------------------


class TestDealMailboxEdgeCases:
    def test_multiple_proposals_same_pair(self):
        """Two proposals between the same players are independently tracked."""
        mailbox = DealMailbox()
        p1 = PendingProposal(from_player=1, to_player=0)
        p2 = PendingProposal(from_player=1, to_player=0)
        id1 = mailbox.propose(p1)
        id2 = mailbox.propose(p2)
        assert id1 != id2
        assert mailbox.pending_count == 2

    def test_accept_only_removes_target(self):
        """Accepting one proposal leaves others untouched."""
        mailbox = DealMailbox()
        p1 = PendingProposal(from_player=1, to_player=0)
        p2 = PendingProposal(from_player=2, to_player=0)
        id1 = mailbox.propose(p1)
        id2 = mailbox.propose(p2)
        mailbox.accept(id1)
        assert mailbox.pending_count == 1
        assert mailbox.get(id2) is p2

    def test_no_duplicate_id_collision(self):
        """Auto-generated ids should be unique."""
        mailbox = DealMailbox()
        ids = set()
        for _ in range(100):
            p = PendingProposal(from_player=1, to_player=0)
            pid = mailbox.propose(p)
            ids.add(pid)
            mailbox.accept(pid)  # remove so count stays low
        assert len(ids) == 100

    def test_proposed_by_field_preserved(self):
        mailbox = DealMailbox()
        p = PendingProposal(
            from_player=1, to_player=0, proposed_by="agent"
        )
        pid = mailbox.propose(p)
        assert mailbox.get(pid).proposed_by == "agent"

    def test_items_preserved_through_roundtrip(self):
        mailbox = DealMailbox()
        items_from = [
            SerializedDealItem(
                item_type="GOLD", from_player_id=1, amount=200, duration=0
            ),
            SerializedDealItem(
                item_type="RESOURCE", from_player_id=1, amount=1, duration=30,
                value_type=17
            ),
        ]
        items_to = [
            SerializedDealItem(
                item_type="GOLD", from_player_id=0, amount=15, duration=30
            ),
        ]
        p = PendingProposal(
            from_player=1, to_player=0,
            items_from_proposer=items_from,
            items_from_target=items_to,
        )
        pid = mailbox.propose(p)
        retrieved = mailbox.get(pid)
        assert len(retrieved.items_from_proposer) == 2
        assert retrieved.items_from_proposer[0].amount == 200
        assert retrieved.items_from_proposer[0].item_type == "GOLD"
        assert retrieved.items_from_proposer[1].value_type == 17
        assert len(retrieved.items_from_target) == 1
        assert retrieved.items_from_target[0].amount == 15
        assert retrieved.items_from_target[0].duration == 30
