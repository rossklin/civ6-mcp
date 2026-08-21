"""Tests for unsolicited shim-print dispatch in GameConnection.

A shim print (MCPDEAL/MCPDIPLO/…) can arrive on the tuner socket while a
command's read loop is active — between the command send and its sentinel.
Regression test for the bug where such a line was silently swallowed into the
command's result lines and never dispatched to deal callbacks (observed live:
the human's first "Declare friendship" click closed the leader screen — the
Lua wrapper ran — but the mailbox never heard the proposal; the second click,
falling between poll commands, worked).
"""

import asyncio
import types

from civ_mcp import tuner_client
from civ_mcp.connection import GameConnection


def _msg(text: str) -> tuner_client.Message:
    """Wrap plain text in the wire output format."""
    return tuner_client.Message(tag=1, payload=f"O\x00InGame: {text}")


def _make_conn() -> GameConnection:
    conn = GameConnection()
    conn._reader = object()
    conn._writer = types.SimpleNamespace(is_closing=lambda: False)
    return conn


def _patch_transport(monkeypatch, incoming: list[tuner_client.Message]):
    """Stub the tuner transport: no stale/trailing messages, and
    recv_message_timeout replays ``incoming`` in order."""
    sent: list[str] = []

    async def fake_send(writer, tag, payload):
        sent.append(payload)

    async def fake_drain(reader, timeout=0.5):
        return []

    async def fake_recv(reader, timeout=2.0):
        if incoming:
            return incoming.pop(0)
        return None

    monkeypatch.setattr(tuner_client, "send_message", fake_send)
    monkeypatch.setattr(tuner_client, "drain_messages", fake_drain)
    monkeypatch.setattr(tuner_client, "recv_message_timeout", fake_recv)
    return sent


class TestUnsolicitedDispatchMidCommand:
    def test_shim_print_between_send_and_sentinel_is_dispatched(self, monkeypatch):
        """An unsolicited MCPDIPLO line interleaving with a command's own
        output must fire the deal callback AND still appear in the result."""
        conn = _make_conn()
        events = []
        conn.add_deal_callback(lambda etype, data: events.append((etype, data)))

        incoming = [
            _msg("MCPDIPLO|PROPOSED|from=0|to=1|action=DECLARE_FRIEND"),
            _msg("OK|command output"),
            _msg("---END---"),
        ]
        _patch_transport(monkeypatch, incoming)

        async def run():
            async with conn._lock:
                return await conn._locked_execute(1, "print('x')", timeout=2.0)

        lines = asyncio.run(run())

        assert lines == [
            "MCPDIPLO|PROPOSED|from=0|to=1|action=DECLARE_FRIEND",
            "OK|command output",
        ]
        assert events == [
            (
                "diplo_proposed",
                {"type": "diplo_proposed", "from": 0, "to": 1,
                 "action": "DECLARE_FRIEND"},
            )
        ]

    def test_mcpdeal_line_mid_command_is_dispatched(self, monkeypatch):
        """Same interleaving for a deal-shim click line."""
        conn = _make_conn()
        events = []
        conn.add_deal_callback(lambda etype, data: events.append((etype, data)))

        incoming = [
            _msg("MCPDEAL_CLICK|abc123|pid=0"),
            _msg("---END---"),
        ]
        _patch_transport(monkeypatch, incoming)

        async def run():
            async with conn._lock:
                return await conn._locked_execute(1, "print('x')", timeout=2.0)

        asyncio.run(run())

        assert events == [
            ("click", {"type": "click", "proposal_id": "abc123", "pid": 0})
        ]

    def test_plain_output_mid_command_does_not_dispatch(self, monkeypatch):
        """Ordinary command output is not routed to callbacks."""
        conn = _make_conn()
        events = []
        conn.add_deal_callback(lambda etype, data: events.append((etype, data)))

        incoming = [
            _msg("OK|command output"),
            _msg("---END---"),
        ]
        _patch_transport(monkeypatch, incoming)

        async def run():
            async with conn._lock:
                return await conn._locked_execute(1, "print('x')", timeout=2.0)

        lines = asyncio.run(run())

        assert lines == ["OK|command output"]
        assert events == []

    def test_callback_exception_does_not_break_command(self, monkeypatch):
        """A failing callback must not abort the command's collection."""
        conn = _make_conn()

        def boom(etype, data):
            raise RuntimeError("callback bug")

        conn.add_deal_callback(boom)

        incoming = [
            _msg("MCPDIPLO|PROPOSED|from=0|to=1|action=DECLARE_FRIEND"),
            _msg("OK|command output"),
            _msg("---END---"),
        ]
        _patch_transport(monkeypatch, incoming)

        async def run():
            async with conn._lock:
                return await conn._locked_execute(1, "print('x')", timeout=2.0)

        lines = asyncio.run(run())

        assert lines == [
            "MCPDIPLO|PROPOSED|from=0|to=1|action=DECLARE_FRIEND",
            "OK|command output",
        ]
