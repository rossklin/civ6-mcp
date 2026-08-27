"""Tests for peace/alliance mailbox routing and the deal-item serialisation fixes.

Covers:
  - ``build_check_proposal_eligibility`` / ``_eligibility_guard_lua`` (the
    shared guard that ``build_propose_peace`` and ``build_form_alliance`` now
    embed, so the mailbox routing check and the engine path cannot drift).
  - ``_parse_trade_params`` producing MAKE_PEACE / ALLIANCE agreement items.
  - ``_lua_add_deal_item`` handling both subtype encodings (agent enum name
    vs. human int), alliance value-type resolution, and resource index
    resolution by name.
  - ``_check_proposal_eligibility`` parsing the Lua guard's output.
  - ``execute_commands`` routing propose_peace / form_alliance to the deal
    mailbox for managed targets and to the engine for unmanaged targets.
"""

import asyncio
import json
import types

from civ_mcp import server, seats as seats_mod
from civ_mcp.deal_mailbox import SerializedDealItem
from civ_mcp.handoff import HandoffConfig, _lua_add_deal_item
from civ_mcp.lua.diplomacy import (
    _eligibility_guard_lua,
    build_check_proposal_eligibility,
    build_form_alliance,
    build_propose_peace,
)
from civ_mcp.seats import Seat, SeatRegistry


# ---------------------------------------------------------------------------
# Eligibility guard + check builder
# ---------------------------------------------------------------------------


class TestEligibilityGuard:
    def test_peace_guard_checks_war_and_canmakepeace(self):
        lua = _eligibility_guard_lua(3, "MAKE_PEACE")
        assert "IsAtWarWith(target)" in lua
        assert "CanMakePeaceWith(target)" in lua
        assert "ERR:NOT_AT_WAR" in lua
        assert "ERR:CANNOT_MAKE_PEACE" in lua

    def test_alliance_guard_checks_friends_and_civic(self):
        lua = _eligibility_guard_lua(3, "ALLIANCE")
        assert "HasMet(target)" in lua
        assert "IsAtWarWith(target)" in lua
        assert "GetDiplomaticStateIndex(me)" in lua
        assert "ERR:ALREADY_ALLIED" in lua
        assert "ERR:NOT_FRIENDS" in lua
        assert "CIVIC_DIPLOMATIC_SERVICE" in lua
        assert "ERR:NO_CIVIC" in lua

    def test_peace_guard_does_not_mention_alliance_conditions(self):
        lua = _eligibility_guard_lua(3, "MAKE_PEACE")
        assert "CIVIC_DIPLOMATIC_SERVICE" not in lua
        assert "GetDiplomaticStateIndex" not in lua

    def test_alliance_guard_does_not_mention_peace_conditions(self):
        lua = _eligibility_guard_lua(3, "ALLIANCE")
        assert "CanMakePeaceWith" not in lua


class TestCheckProposalEligibilityBuilder:
    def test_peace_ok_on_success(self):
        lua = build_check_proposal_eligibility(3, "MAKE_PEACE")
        # Declares the locals the guard expects, then prints OK and sentinel.
        assert "local me = Game.GetLocalPlayer()" in lua
        assert "local target = 3" in lua
        assert "local pDiplo = Players[me]:GetDiplomacy()" in lua
        assert 'print("OK|MAKE_PEACE")' in lua
        assert "---END---" in lua

    def test_alliance_ok_on_success(self):
        lua = build_check_proposal_eligibility(5, "alliance")
        # kind is normalised to upper case.
        assert 'print("OK|ALLIANCE")' in lua
        assert "local target = 5" in lua

    def test_does_not_open_session_or_send_deal(self):
        """The check is pure — it must not mutate any working deal or session."""
        lua = build_check_proposal_eligibility(3, "MAKE_PEACE")
        assert "RequestSession" not in lua
        assert "SendWorkingDeal" not in lua
        assert "ClearWorkingDeal" not in lua
        assert "AddItemOfType" not in lua

    def test_kind_normalised(self):
        lua = build_check_proposal_eligibility(3, "make_peace")
        assert 'print("OK|MAKE_PEACE")' in lua


class TestBuildersReuseGuard:
    """build_propose_peace / build_form_alliance embed the shared guard rather
    than re-declaring the conditions inline — so the mailbox check and the
    engine path stay in lockstep."""

    def test_propose_peace_uses_peace_guard(self):
        lua = build_propose_peace(3)
        # The guard's bail strings appear exactly once (no duplicate inline copy).
        assert lua.count("ERR:NOT_AT_WAR") == 1
        assert lua.count("ERR:CANNOT_MAKE_PEACE") == 1
        assert "CanMakePeaceWith(target)" in lua
        # And it still sends the deal via MAKE_PEACE.
        assert 'RequestSession(me, target, "MAKE_PEACE")' in lua

    def test_form_alliance_uses_alliance_guard(self):
        lua = build_form_alliance(3, "MILITARY")
        assert lua.count("ERR:NOT_FRIENDS") == 1
        assert lua.count("ERR:NO_CIVIC") == 1
        assert lua.count("CIVIC_DIPLOMATIC_SERVICE") == 1
        # And it still adds the ALLIANCE agreement item with a value type.
        assert "SetSubType(DealAgreementTypes.ALLIANCE)" in lua
        assert "SetValueType(type_idx)" in lua
        assert 'GameInfo.Alliances["ALLIANCE_MILITARY"]' in lua


# ---------------------------------------------------------------------------
# _parse_trade_params
# ---------------------------------------------------------------------------


class TestParseTradeParamsPeaceAlliance:
    def test_offer_peace_produces_peace_agreement(self):
        offer, request = server._parse_trade_params(
            {"other_player_id": 0, "offer_peace": True}
        )
        assert offer == [{"type": "AGREEMENT", "subtype": "MAKE_PEACE"}]
        assert request == [{"type": "AGREEMENT", "subtype": "MAKE_PEACE"}]

    def test_offer_peace_false_omitted(self):
        offer, _ = server._parse_trade_params({"offer_peace": False})
        assert offer == []

    def test_offer_alliance_produces_alliance_item_with_type(self):
        offer, request = server._parse_trade_params(
            {"other_player_id": 0, "offer_alliance": "military"}
        )
        assert offer == [
            {"type": "AGREEMENT", "subtype": "ALLIANCE", "alliance_type": "MILITARY"}
        ]
        assert request == [
            {"type": "AGREEMENT", "subtype": "ALLIANCE", "alliance_type": "MILITARY"}
        ]

    def test_offer_alliance_uppercases_type(self):
        offer, _ = server._parse_trade_params({"offer_alliance": "Research"})
        assert offer[0]["alliance_type"] == "RESEARCH"

    def test_peace_and_alliance_are_offer_only(self):
        """Both are mutual agreements and should be present on both sides of the deal."""
        offer, request = server._parse_trade_params(
            {"offer_peace": True, "offer_alliance": "cultural"}
        )
        assert all(it["type"] == "AGREEMENT" for it in offer)
        assert len(offer) == 2
        assert all(it["type"] == "AGREEMENT" for it in request)
        assert len(request) == 2

    def test_open_borders_still_supported(self):
        offer, request = server._parse_trade_params(
            {"offer_open_borders": True, "request_open_borders": True}
        )
        assert offer == [{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}]
        assert request == [{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}]

    def test_joint_war_target_in_value_type_key(self):
        """Regression: the target MUST land under "value_type" — the mailbox
        filing and both Lua item builders read that key.  An earlier version
        emitted "value", the target was silently dropped to -1 at filing,
        and the presentation injected a joint war against nobody (engine
        dropped the invalid item → empty deal table, live-observed)."""
        offer, request = server._parse_trade_params(
            {"other_player_id": 0, "joint_war_target": 2}
        )
        expected = {
            "type": "AGREEMENT",
            "subtype": "JOINT_WAR",
            "value_type": 2,
            "duration": 30,
        }
        assert offer == [expected]
        assert request == [expected]


# ---------------------------------------------------------------------------
# _lua_add_deal_item
# ---------------------------------------------------------------------------


def _item(**kw):
    """Build a SerializedDealItem with sensible defaults."""
    return SerializedDealItem(
        item_type=kw.pop("item_type", "AGREEMENT"),
        from_player_id=kw.pop("from_player_id", 1),
        amount=kw.pop("amount", 0),
        duration=kw.pop("duration", 0),
        **kw,
    )


class TestLuaAddDealItemAgreement:
    def test_agent_string_subtype_gets_enum_prefix(self):
        """Agent-constructed items carry the enum NAME — must render as
        DealAgreementTypes.<name>, not a bare global lookup (which is nil)."""
        lua = _lua_add_deal_item("me", _item(subtype="OPEN_BORDERS"))
        assert "SetSubType(DealAgreementTypes.OPEN_BORDERS)" in lua
        assert "SetSubType(OPEN_BORDERS)" not in lua  # no bare form

    def test_agent_peace_uses_enum_name(self):
        lua = _lua_add_deal_item("me", _item(subtype="MAKE_PEACE"))
        assert "SetSubType(DealAgreementTypes.MAKE_PEACE)" in lua

    def test_human_int_subtype_rendered_bare(self):
        """Human-constructed items carry the enum's integer value; the int
        works directly since enum members are ints."""
        lua = _lua_add_deal_item("me", _item(subtype=547027585))
        assert "SetSubType(547027585)" in lua
        assert "DealAgreementTypes.547027585" not in lua

    def test_joint_war_agent_item_sets_target(self):
        """Regression: the presentation path must SetValueType(target) +
        WarType parameter + duration for JOINT_WAR items — without the
        ValueType the item is a war against nobody, which the engine's deal
        validation silently drops (live-observed: empty deal table)."""
        lua = _lua_add_deal_item(
            "me", _item(subtype="JOINT_WAR", value_type=2, duration=30)
        )
        assert "SetSubType(DealAgreementTypes.JOINT_WAR)" in lua
        assert "ai:SetValueType(2)" in lua
        assert 'SetParameterValue("WarType"' in lua
        assert 'GameInfo.Wars["JOINT_WAR"]' in lua
        assert "ai:SetDuration(30)" in lua

    def test_joint_war_human_int_item_sets_target(self):
        """Human-constructed joint war items (int subtype from the engine)
        get the same treatment."""
        lua = _lua_add_deal_item(
            "me", _item(subtype=-768271062, value_type=2, duration=30)
        )
        assert "SetSubType(-768271062)" in lua
        assert "ai:SetValueType(2)" in lua
        assert 'SetParameterValue("WarType"' in lua

    def test_joint_war_defaults_duration(self):
        lua = _lua_add_deal_item(
            "me", _item(subtype="JOINT_WAR", value_type=2, duration=0)
        )
        assert "ai:SetDuration(30)" in lua

    def test_alliance_agent_resolves_value_by_name(self):
        lua = _lua_add_deal_item(
            "me", _item(subtype="ALLIANCE", alliance_type="military")
        )
        assert "SetSubType(DealAgreementTypes.ALLIANCE)" in lua
        assert 'GameInfo.Alliances["ALLIANCE_MILITARY"]' in lua
        assert "ai:SetValueType(r.Index)" in lua
        # The value set is guarded by an alliance check.
        assert "DealAgreementTypes.ALLIANCE == DealAgreementTypes.ALLIANCE" in lua

    def test_alliance_human_uses_int_value_type(self):
        """Human alliance items carry the int alliance-type index from the
        engine; no GameInfo lookup, but still guarded by the alliance check."""
        lua = _lua_add_deal_item(
            "me", _item(subtype=547027585, value_type=2)
        )
        assert "SetSubType(547027585)" in lua
        assert "ai:SetValueType(2)" in lua
        assert "547027585 == DealAgreementTypes.ALLIANCE" in lua
        assert "GameInfo.Alliances" not in lua

    def test_non_alliance_agreement_has_no_value_type(self):
        lua = _lua_add_deal_item("me", _item(subtype="OPEN_BORDERS"))
        assert "SetValueType" not in lua

    def test_peace_has_no_value_type(self):
        lua = _lua_add_deal_item("me", _item(subtype="MAKE_PEACE"))
        assert "SetValueType" not in lua


class TestLuaAddDealItemResource:
    def test_agent_resource_resolved_by_name(self):
        """Agent-constructed RESOURCE items carry only the type name; the index
        must be resolved via GameInfo.Resources (the bug was SetValueType(-1))."""
        lua = _lua_add_deal_item(
            "me",
            _item(item_type="RESOURCE", name="RESOURCE_SILK", amount=1, duration=30),
        )
        assert 'GameInfo.Resources["RESOURCE_SILK"]' in lua
        assert "SetValueType(-1)" not in lua
        assert "SetAmount(1)" in lua
        assert "SetDuration(30)" in lua

    def test_human_resource_uses_int_value_type(self):
        """Human-constructed RESOURCE items carry the int index from the engine."""
        lua = _lua_add_deal_item(
            "me",
            _item(item_type="RESOURCE", value_type=23, amount=2, duration=30),
        )
        assert "SetValueType(23)" in lua
        assert "GameInfo.Resources" not in lua

    def test_resource_without_name_or_value_falls_back_to_minus_one(self):
        lua = _lua_add_deal_item(
            "me", _item(item_type="RESOURCE", amount=1, duration=30)
        )
        assert "SetValueType(-1)" in lua


class TestLuaAddDealItemOtherTypes:
    def test_gold_unchanged(self):
        lua = _lua_add_deal_item(
            "me", _item(item_type="GOLD", amount=100, duration=0)
        )
        assert "AddItemOfType(DealItemTypes.GOLD, me)" in lua
        assert "SetAmount(100)" in lua
        assert "SetDuration(0)" in lua

    def test_city_uses_value_type(self):
        lua = _lua_add_deal_item(
            "me", _item(item_type="CITY", value_type=7)
        )
        assert "AddItemOfType(DealItemTypes.CITIES, me)" in lua
        assert "SetValueType(7)" in lua


# ---------------------------------------------------------------------------
# _check_proposal_eligibility
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, lines):
        self._lines = lines

    async def execute_write(self, lua, perspective=False):
        return self._lines


class TestCheckProposalEligibility:
    def test_ok(self):
        gs = types.SimpleNamespace(
            conn=_FakeConn(["OK|MAKE_PEACE", "---END---"])
        )
        ok, reason = asyncio.run(
            server._check_proposal_eligibility(gs, 3, "MAKE_PEACE")
        )
        assert ok is True
        assert reason == ""

    def test_err_strips_prefix(self):
        gs = types.SimpleNamespace(
            conn=_FakeConn(["ERR:NOT_AT_WAR|Not at war with player 3", "---END---"])
        )
        ok, reason = asyncio.run(
            server._check_proposal_eligibility(gs, 3, "MAKE_PEACE")
        )
        assert ok is False
        assert reason == "NOT_AT_WAR|Not at war with player 3"

    def test_no_result_treated_as_failure(self):
        gs = types.SimpleNamespace(conn=_FakeConn(["---END---"]))
        ok, reason = asyncio.run(
            server._check_proposal_eligibility(gs, 3, "MAKE_PEACE")
        )
        assert ok is False
        assert "no result" in reason

    def test_connection_error_treated_as_failure(self):
        class _BoomConn:
            async def execute_write(self, lua, perspective=False):
                raise ConnectionError("tuner gone")

        gs = types.SimpleNamespace(conn=_BoomConn())
        ok, reason = asyncio.run(
            server._check_proposal_eligibility(gs, 3, "MAKE_PEACE")
        )
        assert ok is False
        assert "eligibility check failed" in reason


# ---------------------------------------------------------------------------
# execute_commands routing
# ---------------------------------------------------------------------------


async def _passthrough_logged(ctx, name, params, fn, **kw):
    """Bypass turn-gating/logging so the inner _run is exercised directly."""
    return await fn()


async def _elig_ok(gs, target, kind):
    return True, ""


def _elig_bad_factory(reason):
    async def _bad(gs, target, kind):
        return False, reason

    return _bad


def _make_ctx(agent_id=1, human_id=0, agent_ids=(1,)):
    """A minimal MCP Context whose app carries a real HandoffConfig + mailbox.

    The agent ``agent_id`` claims its seat so ``_get_seat`` returns it.
    """
    def factory(pid):
        return Seat(
            player_id=pid,
            game=types.SimpleNamespace(conn=object()),
            logger=types.SimpleNamespace(),
            spatial=types.SimpleNamespace(),
            map_capture=types.SimpleNamespace(),
        )

    cfg = HandoffConfig(enabled=True, human_id=human_id, agent_ids=agent_ids)
    reg = SeatRegistry(default=factory(human_id), agent_ids=agent_ids,
                       human_id=human_id, factory=factory)
    app = types.SimpleNamespace(
        seats=reg, handoff_config=cfg, mailbox=object()
    )
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=app),
        session=types.SimpleNamespace(),
    )
    seat, msg = reg.claim(agent_id, seats_mod.session_key(ctx))
    assert seat is not None, msg
    return ctx, app


def _patch(monkeypatch, elig=_elig_ok):
    """Patch execute_commands' collaborators. Returns (mailed, executed)
    lists that the fakes append to."""
    mailed = []
    executed = []

    async def fake_mailbox(app, seat, target, params):
        mailed.append((target, dict(params)))
        return f"mailed-{target}"

    async def fake_exec(gs, js):
        executed.append(js)
        return "engine-ok"

    monkeypatch.setattr(server, "_logged", _passthrough_logged)
    monkeypatch.setattr(server, "_check_proposal_eligibility", elig)
    monkeypatch.setattr(server, "_mailbox_propose_trade", fake_mailbox)
    monkeypatch.setattr(server, "_execute_commands", fake_exec)
    return mailed, executed


class TestExecuteCommandsPeaceAllianceRouting:
    def test_propose_peace_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "propose_peace", "params": {"other_player_id": 0}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == [(0, {"other_player_id": 0, "offer_peace": True})]
        assert executed == []
        assert "propose_peace:" in result
        assert "mailed-0" in result

    def test_propose_peace_to_unmanaged_falls_through_to_engine(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        # Player 2 is not in managed_ids (0, 1).
        cmds = [{"action": "propose_peace", "params": {"other_player_id": 2}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert len(executed) == 1
        # The command is forwarded unchanged to the engine.
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "propose_peace"
        assert forwarded[0]["params"]["other_player_id"] == 2

    def test_propose_peace_ineligible_does_not_mail(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch, elig=_elig_bad_factory("NOT_AT_WAR|x"))
        cmds = [{"action": "propose_peace", "params": {"other_player_id": 0}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert executed == []
        assert "NOT_AT_WAR|x" in result

    def test_form_alliance_to_managed_routes_to_mailbox(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "form_alliance",
                 "params": {"other_player_id": 0, "alliance_type": "military"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == [(0, {"other_player_id": 0, "offer_alliance": "military"})]
        assert executed == []
        assert "form_alliance:" in result

    def test_form_alliance_missing_type_errors(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "form_alliance", "params": {"other_player_id": 0}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert executed == []
        assert "alliance_type is required" in result

    def test_form_alliance_ineligible_does_not_mail(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(
            monkeypatch, elig=_elig_bad_factory("NOT_FRIENDS|Must be declared friends first")
        )
        cmds = [{"action": "form_alliance",
                 "params": {"other_player_id": 0, "alliance_type": "RESEARCH"}}]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert executed == []
        assert "NOT_FRIENDS" in result

    def test_form_alliance_to_unmanaged_falls_through(self, monkeypatch):
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "form_alliance",
                 "params": {"other_player_id": 2, "alliance_type": "CULTURAL"}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "form_alliance"
        assert forwarded[0]["params"]["alliance_type"] == "CULTURAL"

    def test_propose_trade_managed_still_routes_to_mailbox(self, monkeypatch):
        """Regression: the existing propose_trade managed branch is unchanged."""
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "propose_trade",
                 "params": {"other_player_id": 0, "offer_gold": 50}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed and mailed[0][0] == 0
        assert mailed[0][1].get("offer_gold") == 50
        assert executed == []

    def test_propose_trade_unmanaged_converts_params_for_engine(self, monkeypatch):
        """Regression: propose_trade to an unmanaged civ is converted from flat
        params into offer_items/request_items before hitting the executor."""
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [{"action": "propose_trade",
                 "params": {"other_player_id": 2, "offer_gold": 50}}]

        asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        params = forwarded[0]["params"]
        assert params["other_player_id"] == 2
        assert any(it["type"] == "GOLD" and it["amount"] == 50 for it in params["offer_items"])

    def test_mixed_batch_mailbox_and_engine(self, monkeypatch):
        """A peace proposal to a managed civ and a unit move in one batch:
        the peace goes to the mailbox, the move goes to the executor."""
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [
            {"action": "propose_peace", "params": {"other_player_id": 0}},
            {"action": "fortify_unit", "params": {"unit_index": 3}},
        ]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == [(0, {"other_player_id": 0, "offer_peace": True})]
        assert len(executed) == 1
        forwarded = json.loads(executed[0])
        assert forwarded[0]["action"] == "fortify_unit"
        assert "propose_peace:" in result
        assert "engine-ok" in result

    def test_propose_trade_does_not_accept_peace_or_alliance_items(self, monkeypatch):
        """Regression: propose_trade is not allowed to carry peace/alliance
        items, which are now handled by propose_peace/form_alliance."""
        ctx, _ = _make_ctx()
        mailed, executed = _patch(monkeypatch)
        cmds = [
            {"action": "propose_trade",
             "params": {"other_player_id": 0, "offer_peace": True}},
            {"action": "propose_trade",
             "params": {"other_player_id": 0, "offer_alliance": "military"}},
        ]

        result = asyncio.run(server.execute_commands(ctx, json.dumps(cmds)))

        assert mailed == []
        assert executed == []
        assert "peace/alliance items are not allowed in propose_trade" in result