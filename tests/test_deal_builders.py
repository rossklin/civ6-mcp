"""Regression tests for the trade/alliance deal builders.

The working deal persists in the engine across calls. If a builder skips
``ClearWorkingDeal``, leftover items from a previous call stack underneath the
new ones and get traded for real — observed live on 2026-08-01, where a
``test_trade`` preview followed by ``propose_trade`` moved twice the intended
gold. These tests pin the clearing behaviour into the generated Lua.
Note that test_trade has been removed and I'm not sure if the same applies to a 
previous rejected proposal.
"""

from civ_mcp.lua.diplomacy import (
    build_form_alliance,
    build_propose_trade,
)

GOLD_50 = [{"type": "GOLD", "amount": 50, "duration": 0}]
OPEN_BORDERS = [{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}]


class TestProposeTradeClearsWorkingDeal:
    def test_clear_is_unconditional(self):
        """A HasPendingDeal guard around the clear is the bug — it lets a
        leftover deal survive and stack under the new items."""
        lua = build_propose_trade(0, GOLD_50, OPEN_BORDERS)
        assert "ClearWorkingDeal(DealDirection.OUTGOING, me, target)" in lua
        assert "HasPendingDeal" not in lua

    def test_clear_precedes_item_additions(self):
        lua = build_propose_trade(0, GOLD_50, [])
        assert lua.index("ClearWorkingDeal") < lua.index("AddItemOfType")

    def test_items_added_once_each(self):
        lua = build_propose_trade(0, GOLD_50, OPEN_BORDERS)
        assert lua.count("AddItemOfType(DealItemTypes.GOLD, me)") == 1
        assert lua.count("AddItemOfType(DealItemTypes.AGREEMENTS, target)") == 1


class TestFormAllianceClearsWorkingDeal:
    def test_clear_is_unconditional(self):
        lua = build_form_alliance(0, "MILITARY")
        assert "ClearWorkingDeal(DealDirection.OUTGOING, me, target)" in lua
        assert "HasPendingDeal" not in lua
