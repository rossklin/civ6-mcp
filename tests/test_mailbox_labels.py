"""Tests for _format_mailbox_item label rendering.

The deal shim resolves AGREEMENT/RESOURCE items into agent-readable labels at
serialization time (enum names + GameInfo only exist in the game's Lua
states); the formatter must prefer them, with sane fallbacks for
agent-constructed items (which carry `name` instead) and legacy items.
"""

from civ_mcp.deal_mailbox import SerializedDealItem
from civ_mcp.server import _format_mailbox_item


class TestFormatMailboxItemLabels:
    def test_agreement_with_label(self):
        item = SerializedDealItem(
            item_type="AGREEMENT",
            from_player_id=0,
            amount=0,
            duration=30,
            subtype=547027585,
            label="Peace Treaty",
        )
        assert _format_mailbox_item(item, indent="  ") == "  - Peace Treaty\n"

    def test_agreement_label_with_duration(self):
        item = SerializedDealItem(
            item_type="AGREEMENT",
            from_player_id=0,
            amount=0,
            duration=30,
            subtype=-12345,
            label="Joint War vs Rome (30 turns)",
        )
        assert (
            _format_mailbox_item(item, indent="") == "- Joint War vs Rome (30 turns)\n"
        )

    def test_agreement_without_label_falls_back(self):
        """Legacy/agent-constructed items without a label keep the raw
        subtype rendering (execution-side items pass through here)."""
        item = SerializedDealItem(
            item_type="AGREEMENT",
            from_player_id=0,
            amount=0,
            duration=0,
            subtype="OPEN_BORDERS",
        )
        assert (
            _format_mailbox_item(item, indent="")
            == "- Agreement (sub=OPEN_BORDERS)\n"
        )

    def test_resource_prefers_label_then_name(self):
        shim_labeled = SerializedDealItem(
            item_type="RESOURCE",
            from_player_id=0,
            amount=2,
            duration=30,
            value_type=23,
            label="Cotton",
        )
        assert _format_mailbox_item(shim_labeled, indent="") == "- Cotton x2\n"

        agent_named = SerializedDealItem(
            item_type="RESOURCE",
            from_player_id=0,
            amount=1,
            duration=30,
            name="COTTON",
        )
        assert _format_mailbox_item(agent_named, indent="") == "- COTTON x1\n"

        legacy = SerializedDealItem(
            item_type="RESOURCE",
            from_player_id=0,
            amount=1,
            duration=30,
            value_type=23,
        )
        assert _format_mailbox_item(legacy, indent="") == "- Resource (id=23) x1\n"

    def test_gold_unchanged(self):
        item = SerializedDealItem(
            item_type="GOLD", from_player_id=0, amount=5, duration=30
        )
        assert (
            _format_mailbox_item(item, indent="")
            == "- 5 Gold per turn (30 turns)\n"
        )
