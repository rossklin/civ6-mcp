"""Tests for the MCPDEAL line parser (connection._parse_mcpdeal_line)."""

import pytest

from civ_mcp.connection import _parse_mcpdeal_line


# The parser expects the *payload* (raw message from the wire), which includes
# the O\x00<context>: prefix.  _parse_output strips that prefix; the parser
# calls _parse_output internally.


def _p(text: str) -> str:
    """Wrap plain text in the wire format that _parse_output expects."""
    return f"O\x00InGame: {text}"


# ---------------------------------------------------------------------------
# Valid events
# ---------------------------------------------------------------------------


class TestParseProposedDeal:
    def test_proposed_header_starts_accumulation(self):
        """PROPOSED header doesn't emit an event yet — it starts buffering."""
        result = _parse_mcpdeal_line(
            _p("MCPDEAL|action=4|from=0|to=1")
        )
        assert result is None  # waiting for items

    def test_item_lines_accumulate(self):
        """Item lines are buffered, not emitted individually."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        result = _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=1|amount=200|duration=0|value=-1|sub=-1")
        )
        assert result is None  # still accumulating

    def test_end_emits_complete_proposal(self):
        """MCPDEAL_END emits the full proposal with all accumulated items."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=1|amount=200|duration=0|value=-1|sub=-1")
        )
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=0|amount=15|duration=30|value=-1|sub=-1")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        assert result is not None
        assert result["type"] == "proposed"
        assert result["action"] == "PROPOSED"
        assert result["from"] == 0
        assert result["to"] == 1
        assert len(result["items"]) == 2

    def test_items_have_correct_types(self):
        """Numeric fields are parsed as int, string fields as str."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=1|amount=200|duration=0|value=-1|sub=-1")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        item = result["items"][0]
        assert item["item_type"] == "GOLD"
        assert item["from"] == 1
        assert isinstance(item["from"], int)
        assert item["amount"] == 200
        assert isinstance(item["amount"], int)
        assert item["duration"] == 0
        assert item["value"] == -1
        assert item["sub"] == -1

    def test_single_item_deal(self):
        """One-item deal (e.g. a pure gift)."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=2"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=2|amount=1|duration=0|value=-1|sub=-1")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        assert len(result["items"]) == 1
        assert result["items"][0]["amount"] == 1

    def test_multiple_deals_reset_state(self):
        """After a deal is emitted, the next deal starts fresh."""
        # First deal
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=1|amount=100|duration=0|value=-1|sub=-1")
        )
        r1 = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        assert len(r1["items"]) == 1

        # Second deal — should not include first deal's items
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=2"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=2|amount=50|duration=0|value=-1|sub=-1")
        )
        r2 = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        assert len(r2["items"]) == 1
        assert r2["items"][0]["amount"] == 50
        assert r2["from"] == 0
        assert r2["to"] == 2

    def test_resource_item_fields(self):
        """Resource items pass through value_type correctly."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|RESOURCE|from=1|amount=1|duration=30|value=23|sub=-1")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        item = result["items"][0]
        assert item["item_type"] == "RESOURCE"
        assert item["value"] == 23

    def test_agreement_item_fields(self):
        """Agreement items pass through subtype correctly."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|AGREEMENT|from=1|amount=0|duration=0|value=-1|sub=5")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        item = result["items"][0]
        assert item["item_type"] == "AGREEMENT"
        assert item["sub"] == 5

class TestParseClick:
    def test_click_with_pid(self):
        result = _parse_mcpdeal_line(
            _p("MCPDEAL_CLICK|abc123|pid=0")
        )
        assert result is not None
        assert result["type"] == "click"
        assert result["proposal_id"] == "abc123"
        assert result["pid"] == 0

    def test_click_without_pid(self):
        result = _parse_mcpdeal_line(
            _p("MCPDEAL_CLICK|abc123")
        )
        assert result is not None
        assert result["type"] == "click"
        assert result["proposal_id"] == "abc123"
        assert "pid" not in result


class TestParseInspectSuppressed:
    def test_inspect_is_suppressed_event(self):
        result = _parse_mcpdeal_line(
            _p("MCPDEAL|action=7|from=0|to=1")
        )
        assert result is not None
        assert result["type"] == "inspect_suppressed"
        assert result["from"] == 0
        assert result["to"] == 1

    def test_explicit_suppressed_text(self):
        result = _parse_mcpdeal_line(
            _p("MCPDEAL|INSPECT|suppressed|from=0|to=1")
        )
        assert result is not None
        assert result["type"] == "inspect_suppressed"


class TestParseHealth:
    def test_health_ok(self):
        result = _parse_mcpdeal_line(
            _p("DEALSHIM_HEALTH|true")
        )
        assert result == {"type": "health", "ok": True}

    def test_health_bad(self):
        result = _parse_mcpdeal_line(
            _p("DEALSHIM_HEALTH|false")
        )
        assert result == {"type": "health", "ok": False}


class TestParseNotifySent:
    def test_notify_sent(self):
        result = _parse_mcpdeal_line(
            _p("NOTIFY_SENT|abc123")
        )
        assert result == {"type": "notify_sent", "proposal_id": "abc123"}


class TestParseChatSend:
    """MCPCHAT|SEND lines from the chat shim (human typed a message)."""

    @staticmethod
    def _hex(s: str) -> str:
        return s.encode("utf-8").hex()

    def test_basic_message(self):
        text = "hello world"
        result = _parse_mcpdeal_line(
            _p(
                "MCPCHAT|SEND|from=0|to=2|ttype=2|hex=" + self._hex(text)
            )
        )
        assert result is not None
        assert result["type"] == "chat_send"
        assert result["from"] == 0
        assert result["to"] == 2
        assert result["ttype"] == 2
        assert result["text"] == text

    def test_message_without_target_fields(self):
        """The shim emits only from= and hex= (it intercepts before the
        target is computed). to/ttype must be absent, not defaulted."""
        text = "hi there"
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|SEND|from=0|hex=" + self._hex(text))
        )
        assert result is not None
        assert result["type"] == "chat_send"
        assert result["from"] == 0
        assert result["text"] == text
        assert "to" not in result
        assert "ttype" not in result

    def test_text_with_pipes_and_newlines(self):
        """Hex encoding survives pipe/newline/quote characters."""
        text = "line1\nline2|with|pipes \"quotes\""
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|SEND|from=1|to=3|ttype=2|hex=" + self._hex(text))
        )
        assert result["text"] == text

    def test_empty_text(self):
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|SEND|from=0|to=1|ttype=0|hex=")
        )
        assert result["type"] == "chat_send"
        assert result["text"] == ""

    def test_non_utf8_falls_back_to_replace(self):
        """Invalid UTF-8 hex decodes to replacement chars, not an exception."""
        # 0xff is invalid as a standalone UTF-8 leading byte.
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|SEND|from=0|to=1|ttype=0|hex=ff")
        )
        assert result["type"] == "chat_send"
        assert "�" in result["text"]

    def test_non_send_verb_returns_none(self):
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|OTHER|from=0")
        )
        assert result is None

    def test_missing_numeric_fields_do_not_crash(self):
        result = _parse_mcpdeal_line(
            _p("MCPCHAT|SEND|from=abc|to=1|ttype=0|hex=68")
        )
        assert result is not None
        assert result["type"] == "chat_send"
        assert "from" not in result  # bad int dropped
        assert result["to"] == 1
        assert result["text"] == "h"


# ---------------------------------------------------------------------------
# Non-MCPDEAL lines
# ---------------------------------------------------------------------------


class TestParseNonMcpdeal:
    def test_regular_output_returns_none(self):
        """Non-MCPDEAL print() output is ignored."""
        result = _parse_mcpdeal_line(
            _p("Some random game output")
        )
        assert result is None

    def test_sentinel_returns_none(self):
        result = _parse_mcpdeal_line(
            _p("---END---")
        )
        assert result is None

    def test_empty_string_returns_none(self):
        result = _parse_mcpdeal_line("")
        assert result is None

    def test_non_output_payload_returns_none(self):
        """Messages that don't start with 'O' are not output lines."""
        result = _parse_mcpdeal_line("ERR:something went wrong")
        assert result is None

    def test_similar_but_not_mcpdeal(self):
        """Lines that look similar but aren't MCPDEAL are ignored."""
        result = _parse_mcpdeal_line(
            _p("DEAL|1|Rome|Trajan")
        )
        assert result is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    def test_end_without_header_emits_empty(self):
        """If MCPDEAL_END arrives without a header, emit empty items."""
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        assert result is not None
        assert result["type"] == "proposed"
        assert result["items"] == []

    def test_second_header_resets_items(self):
        """A second MCPDEAL|PROPOSED before END resets the buffer."""
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=1"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=1|amount=100|duration=0|value=-1|sub=-1")
        )
        # Second header — resets
        _parse_mcpdeal_line(_p("MCPDEAL|action=4|from=0|to=2"))
        _parse_mcpdeal_line(
            _p("MCPDEAL_ITEM|GOLD|from=2|amount=50|duration=0|value=-1|sub=-1")
        )
        result = _parse_mcpdeal_line(_p("MCPDEAL_END"))
        # Should only have the second deal's items
        assert len(result["items"]) == 1
        assert result["items"][0]["amount"] == 50
        assert result["from"] == 0
        assert result["to"] == 2

    def test_unknown_action_is_still_an_event(self):
        result = _parse_mcpdeal_line(
            _p("MCPDEAL|action=99|from=0|to=1")
        )
        assert result is not None
        assert result["type"] == "unknown"
        assert result["action"] == "99"
