"""Tests for the deal-shim Lua builders added to handoff.py."""

import pytest

from civ_mcp import handoff
from civ_mcp.deal_mailbox import PendingProposal, SerializedDealItem


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _simple_proposal(**kw):
    """Build a minimal PendingProposal for injection/execution tests."""
    return PendingProposal(
        proposal_id=kw.get("proposal_id", "test-001"),
        from_player=kw.get("from_player", 1),
        to_player=kw.get("to_player", 0),
        items_from_proposer=kw.get("items_from_proposer", [
            SerializedDealItem(
                item_type="GOLD", from_player_id=1, amount=200, duration=0
            ),
        ]),
        items_from_target=kw.get("items_from_target", [
            SerializedDealItem(
                item_type="GOLD", from_player_id=0, amount=10, duration=30
            ),
        ]),
        turn_proposed=kw.get("turn_proposed", 5),
    )


# ---------------------------------------------------------------------------
# build_deal_shim_install_lua
# ---------------------------------------------------------------------------


class TestDealShimInstallLua:
    def test_sentinel_present(self):
        lua = handoff.build_deal_shim_install_lua((1, 2))
        assert "---END---" in lua

    def test_wraps_send_working_deal(self):
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "__MCP_orig_SWD = DealManager.SendWorkingDeal" in lua
        assert "DealManager.SendWorkingDeal = function" in lua

    def test_overrides_is_auto_propose(self):
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "__MCP_orig_IAP = IsAutoPropose" in lua
        assert "IsAutoPropose = function" in lua

    def test_is_auto_propose_always_returns_false(self):
        """IsAutoPropose always returns false — no auto-propose for any civ."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "return false end" in lua

    def test_inspect_suppressed_for_managed(self):
        """INSPECT (action=7) is suppressed for managed targets."""
        lua = handoff.build_deal_shim_install_lua((1, 2))
        assert "action == 7" in lua
        assert "MCPDEAL|INSPECT|suppressed" in lua

    def test_proposed_serialized_for_managed(self):
        """PROPOSED (action=4) is serialised to MCPDEAL lines."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "action == 4" in lua
        assert "MCPDEAL|PROPOSED|from=" in lua
        assert "MCPDEAL_ITEM|" in lua
        assert "MCPDEAL_END" in lua

    def test_managed_ids_lookup_table(self):
        """Managed player ids are in a lookup table for O(1) checking."""
        lua = handoff.build_deal_shim_install_lua((1, 3))
        assert "[1]=true" in lua
        assert "[3]=true" in lua
        assert "__MCP_managed_ids = {" in lua

    def test_all_managed_ids_checked(self):
        """Both fromP and toP are checked against managed ids."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "__MCP_managed_ids[toP] or __MCP_managed_ids[fromP]" in lua

    def test_non_managed_forwarded(self):
        """Deals with non-managed civs are forwarded to the original function."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "return __MCP_orig_SWD(action, fromP, toP)" in lua

    def test_always_overwrites(self):
        """The shim always overwrites — game Lua state persists across
        server restarts, so a new server must update the wrappers."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "__MCP_orig_SWD = DealManager.SendWorkingDeal" in lua
        assert "__MCP_orig_IAP = IsAutoPropose" in lua
        assert "__MCP_orig_UDS = UpdateDealStatus" in lua

    def test_deal_items_enumerated(self):
        """Serialisation uses pDeal:Items() iteration."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert "for item in pDeal:Items() do" in lua

    def test_all_item_types_serialized(self):
        """All six deal item types are recognised in the serialisation."""
        lua = handoff.build_deal_shim_install_lua((1,))
        assert 'typeName = "GOLD"' in lua
        assert 'typeName = "RESOURCE"' in lua
        assert 'typeName = "AGREEMENT"' in lua
        assert 'typeName = "FAVOR"' in lua
        assert 'typeName = "CITY"' in lua
        assert 'typeName = "GREAT_WORK"' in lua


# ---------------------------------------------------------------------------
# build_deal_shim_uninstall_lua
# ---------------------------------------------------------------------------


class TestDealShimUninstallLua:
    def test_restores_originals(self):
        lua = handoff.build_deal_shim_uninstall_lua()
        assert "DealManager.SendWorkingDeal = __MCP_orig_SWD" in lua
        assert "IsAutoPropose = __MCP_orig_IAP" in lua
        assert "__MCP_orig_SWD = nil" in lua
        assert "__MCP_orig_IAP = nil" in lua

    def test_cleans_up_globals(self):
        lua = handoff.build_deal_shim_uninstall_lua()
        assert "__MCP_managed_ids = nil" in lua
        assert "__MCP_managed_deal = nil" in lua

    def test_sentinel_present(self):
        lua = handoff.build_deal_shim_uninstall_lua()
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_deal_shim_health_check_lua
# ---------------------------------------------------------------------------


class TestDealShimHealthCheckLua:
    def test_checks_wrapper_and_override(self):
        lua = handoff.build_deal_shim_health_check_lua()
        assert "DEALSHIM_HEALTH|" in lua
        assert "__MCP_orig_SWD ~= nil" in lua
        assert "DealManager.SendWorkingDeal ~= __MCP_orig_SWD" in lua
        assert "__MCP_orig_IAP ~= nil" in lua

    def test_sentinel_present(self):
        lua = handoff.build_deal_shim_health_check_lua()
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_inject_deal_lua
# ---------------------------------------------------------------------------


class TestInjectDealLua:
    def test_clears_outgoing_first(self):
        proposal = _simple_proposal()
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "ClearWorkingDeal(0, me, other)" in lua

    def test_adds_proposer_items(self):
        proposal = _simple_proposal()
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        # Proposer (P1) offers 200g — uses variable 'fromP'
        assert "AddItemOfType(DealItemTypes.GOLD, fromP)" in lua
        assert "SetAmount(200)" in lua
        assert "SetDuration(0)" in lua

    def test_adds_target_items(self):
        proposal = _simple_proposal()
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        # Target (P0) gives 10gpt — uses variable 'toP'
        assert "AddItemOfType(DealItemTypes.GOLD, toP)" in lua
        assert "SetAmount(10)" in lua
        assert "SetDuration(30)" in lua

    def test_includes_item_count_in_output(self):
        proposal = _simple_proposal()
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert 'print("INJECTED|"' in lua
        assert "GetItemCount()" in lua

    def test_sentinel_present(self):
        proposal = _simple_proposal()
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "---END---" in lua

    def test_empty_items_is_valid(self):
        """A proposal with no items should still produce valid Lua."""
        proposal = PendingProposal(
            proposal_id="empty", from_player=1, to_player=0
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "ClearWorkingDeal(0, me, other)" in lua
        assert "---END---" in lua

    def test_resource_item_injection(self):
        """Resource items pass value_type to the deal."""
        proposal = PendingProposal(
            proposal_id="r1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="RESOURCE", from_player_id=1, amount=1,
                    duration=30, value_type=23
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "AddItemOfType(DealItemTypes.RESOURCES, fromP)" in lua
        assert "SetValueType(23)" in lua

    def test_agreement_item_injection(self):
        """Agreement items pass sub_type to the deal."""
        proposal = PendingProposal(
            proposal_id="a1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="AGREEMENT", from_player_id=1, amount=0,
                    duration=0, sub_type=8
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "AddItemOfType(DealItemTypes.AGREEMENTS, fromP)" in lua
        assert "SetSubType(8)" in lua

    def test_favor_item_injection(self):
        proposal = PendingProposal(
            proposal_id="f1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="FAVOR", from_player_id=1, amount=30,
                    duration=0
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "AddItemOfType(DealItemTypes.FAVOR, fromP)" in lua
        assert "SetAmount(30)" in lua


# ---------------------------------------------------------------------------
# build_present_deal_lua
# ---------------------------------------------------------------------------


class TestPresentDealLua:
    def test_opens_make_deal_session(self):
        lua = handoff.build_present_deal_lua(0, 2)
        assert 'RequestSession(0, 2, "MAKE_DEAL")' in lua

    def test_reports_session_id(self):
        lua = handoff.build_present_deal_lua(0, 2)
        assert 'print("SESSION_OPENED|"' in lua

    def test_sentinel_present(self):
        lua = handoff.build_present_deal_lua(0, 2)
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_force_deal_buttons_lua
# ---------------------------------------------------------------------------


class TestForceDealButtonsLua:
    def test_sets_managed_flag(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "__MCP_managed_deal = true" in lua

    def test_shows_accept_button(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "Controls.AcceptDeal:SetHide(false)" in lua

    def test_shows_refuse_button(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "Controls.RefuseDeal:SetHide(false)" in lua

    def test_hides_equalize_button(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "Controls.EqualizeDeal:SetHide(true)" in lua

    def test_sets_leader_dialog_with_proposer_name(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "proposes the following deal:" in lua
        assert "PlayerConfigurations[1]" in lua

    def test_stores_proposal_id(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert '__MCP_deal_proposal_id = "abc123"' in lua

    def test_confirmation_output(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert 'print("DEAL_BUTTONS_READY")' in lua

    def test_sentinel_present(self):
        lua = handoff.build_force_deal_buttons_lua(1, "abc123")
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_clear_deal_flag_lua
# ---------------------------------------------------------------------------


class TestClearDealFlagLua:
    def test_clears_flag_and_proposal_id(self):
        lua = handoff.build_clear_deal_flag_lua()
        assert "__MCP_managed_deal = false" in lua
        assert "__MCP_deal_proposal_id = nil" in lua

    def test_sentinel_present(self):
        lua = handoff.build_clear_deal_flag_lua()
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_notification_handler_lua
# ---------------------------------------------------------------------------


class TestNotificationHandlerLua:
    def test_registers_on_notification_activated(self):
        lua = handoff.build_notification_handler_lua((1, 2))
        assert "Events.NotificationActivated.Add(__MCP_note_handler)" in lua

    def test_checks_by_user(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert "if not byUser then return end" in lua

    def test_checks_local_player(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert "if pid ~= Game.GetLocalPlayer()" in lua

    def test_extracts_proposal_id(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert 'msg:match("MCPDEAL:(.*)")' in lua

    def test_dismisses_notification(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert "NotificationManager.Dismiss(pid, nid)" in lua

    def test_prints_mcpdeal_click(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert 'print("MCPDEAL_CLICK|"' in lua

    def test_idempotent(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert "if __MCP_note_handler == nil then" in lua
        assert 'print("NOTE_HANDLER|installed")' in lua
        assert 'print("NOTE_HANDLER|present")' in lua

    def test_sentinel_present(self):
        lua = handoff.build_notification_handler_lua((1,))
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_send_deal_notification_lua
# ---------------------------------------------------------------------------


class TestSendDealNotificationLua:
    def test_sends_notification_to_correct_player(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Test summary"
        )
        assert "local pid = 0" in lua
        assert "NotificationManager.SendNotification(pid, nType," in lua

    def test_embeds_proposal_id(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Test summary"
        )
        assert "'MCPDEAL:abc123'" in lua

    def test_includes_summary(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Maya offers: 200g"
        )
        assert "Maya offers: 200g" in lua

    def test_escapes_single_quotes_in_summary(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Leader's deal"
        )
        assert "Leader\\'s deal" in lua

    def test_uses_user_defined_notification_type(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Test"
        )
        assert "NOTIFICATION_USER_DEFINED_1" in lua

    def test_confirmation_output(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Test"
        )
        assert 'print("NOTIFY_SENT|abc123")' in lua

    def test_sentinel_present(self):
        lua = handoff.build_send_deal_notification_lua(
            0, 1, "abc123", "Test"
        )
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# build_execute_deal_lua
# ---------------------------------------------------------------------------


class TestExecuteDealLua:
    def test_clears_outgoing_first(self):
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        assert "ClearWorkingDeal(0, me, other)" in lua

    def test_adds_all_items_from_both_sides(self):
        """Items from proposer and target are both added to the deal."""
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        # Should include items from both proposer (200g lump) and target (10gpt)
        assert lua.count("AddItemOfType") == 2

    def test_sends_working_deal_accepted(self):
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        assert "SendWorkingDeal(1, me, other)" in lua

    def test_includes_session_teardown(self):
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        # _lua_close_diplo_session() should be included
        assert "FindOpenSessionID" in lua
        assert "CloseSession" in lua
        assert "DiplomacyActionView_ShowIngameUI" in lua

    def test_confirmation_output(self):
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        assert 'print("DEAL_EXECUTED|"' in lua

    def test_sentinel_present(self):
        proposal = _simple_proposal()
        lua = handoff.build_execute_deal_lua(proposal, 0)
        assert "---END---" in lua


# ---------------------------------------------------------------------------
# _lua_add_deal_item (via build_inject_deal_lua / build_execute_deal_lua)
# ---------------------------------------------------------------------------


class TestLuaAddDealItem:
    def test_gold_lump_no_duration(self):
        proposal = PendingProposal(
            proposal_id="g1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="GOLD", from_player_id=1, amount=500,
                    duration=0
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "SetAmount(500)" in lua
        assert "SetDuration(0)" in lua

    def test_gold_per_turn(self):
        proposal = PendingProposal(
            proposal_id="g2",
            from_player=1, to_player=0,
            items_from_target=[
                SerializedDealItem(
                    item_type="GOLD", from_player_id=0, amount=5,
                    duration=30
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "SetDuration(30)" in lua

    def test_city_item(self):
        proposal = PendingProposal(
            proposal_id="c1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="CITY", from_player_id=1, amount=0,
                    duration=0, value_type=42
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "AddItemOfType(DealItemTypes.CITIES, fromP)" in lua
        assert "SetValueType(42)" in lua

    def test_great_work_item(self):
        proposal = PendingProposal(
            proposal_id="gw1",
            from_player=1, to_player=0,
            items_from_proposer=[
                SerializedDealItem(
                    item_type="GREAT_WORK", from_player_id=1, amount=0,
                    duration=0, value_type=7
                ),
            ],
        )
        lua = handoff.build_inject_deal_lua(0, 1, proposal)
        assert "AddItemOfType(DealItemTypes.GREATWORK, fromP)" in lua
        assert "SetValueType(7)" in lua
