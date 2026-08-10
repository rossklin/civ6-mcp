"""Tests for the server-side message mailbox (message_mailbox.py)."""

import pytest

from civ_mcp.message_mailbox import Message, MessageMailbox


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


class TestMessage:
    def test_defaults(self):
        m = Message()
        assert m.message_id == ""
        assert m.from_player == -1
        assert m.to_player == -1
        assert m.text == ""
        assert m.turn == 0
        assert m.direction == ""


# ---------------------------------------------------------------------------
# MessageMailbox — posting
# ---------------------------------------------------------------------------


class TestPost:
    def test_post_assigns_id(self):
        mb = MessageMailbox()
        mid = mb.post(Message(from_player=0, to_player=1, text="hi", direction="out"))
        assert mid == mb.all_messages()[0].message_id
        assert len(mid) > 0

    def test_post_preserves_existing_id(self):
        mb = MessageMailbox()
        m = Message(message_id="fixed123", from_player=0, to_player=1, text="hi")
        mid = mb.post(m)
        assert mid == "fixed123"

    def test_post_appends_in_order(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=0, to_player=1, text="first"))
        mb.post(Message(from_player=0, to_player=1, text="second"))
        msgs = mb.all_messages()
        assert len(msgs) == 2
        assert msgs[0].text == "first"
        assert msgs[1].text == "second"

    def test_inbound_sets_last_sender(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=2, to_player=0, text="hello", direction="in"))
        assert mb.last_inbound_sender(0) == 2

    def test_outbound_does_not_set_last_sender(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=0, to_player=2, text="hi", direction="out"))
        assert mb.last_inbound_sender(2) is None


# ---------------------------------------------------------------------------
# MessageMailbox — history cap
# ---------------------------------------------------------------------------


class TestHistoryCap:
    def test_cap_drops_oldest(self):
        mb = MessageMailbox(history_cap=3)
        for i in range(5):
            mb.post(Message(from_player=0, to_player=1, text=f"m{i}"))
        msgs = mb.all_messages()
        assert len(msgs) == 3
        assert msgs[0].text == "m2"
        assert msgs[-1].text == "m4"

    def test_cap_drops_by_id_index(self):
        mb = MessageMailbox(history_cap=2)
        mb.post(Message(message_id="a", from_player=0, to_player=1, text="a"))
        mb.post(Message(message_id="b", from_player=0, to_player=1, text="b"))
        mb.post(Message(message_id="c", from_player=0, to_player=1, text="c"))
        ids = {m.message_id for m in mb.all_messages()}
        assert ids == {"b", "c"}


# ---------------------------------------------------------------------------
# MessageMailbox — queries
# ---------------------------------------------------------------------------


class TestForPlayer:
    def test_incoming_and_outgoing(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=0, to_player=1, text="out", direction="out"))
        mb.post(Message(from_player=2, to_player=0, text="in", direction="in"))
        msgs = mb.for_player(0)
        assert len(msgs) == 2
        assert msgs[0].text == "out"
        assert msgs[1].text == "in"

    def test_excludes_other_players(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=1, to_player=2, text="not me"))
        assert mb.for_player(0) == []

    def test_limit_caps_newest(self):
        mb = MessageMailbox()
        for i in range(10):
            mb.post(Message(from_player=0, to_player=1, text=f"m{i}"))
        msgs = mb.for_player(0, limit=3)
        assert len(msgs) == 3
        assert msgs[-1].text == "m9"
        assert msgs[0].text == "m7"


class TestLastInboundSender:
    def test_most_recent_wins(self):
        mb = MessageMailbox()
        mb.post(Message(from_player=1, to_player=0, text="a", direction="in"))
        mb.post(Message(from_player=2, to_player=0, text="b", direction="in"))
        assert mb.last_inbound_sender(0) == 2

    def test_set_explicitly(self):
        mb = MessageMailbox()
        mb.set_last_inbound_sender(0, 3)
        assert mb.last_inbound_sender(0) == 3

    def test_unknown_player_returns_none(self):
        mb = MessageMailbox()
        assert mb.last_inbound_sender(99) is None
