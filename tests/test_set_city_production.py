"""Tests for set_city_production silent-failure detection.

The game's CityManager.RequestOperation is fire-and-forget; even when
CanStartOperation returned true, it can silently no-op if the queue is
in a degenerate state. The OK-path verification catches this.
"""

import asyncio
import types

from civ_mcp.game_state import GameState


class _StubConnection:
    """Stubs execute_write and execute_read with queued responses."""

    def __init__(self, write_lines, read_lines):
        self._write = list(write_lines)
        self._read = list(read_lines)

    async def execute_write(self, lua):
        return self._write.pop(0)

    async def execute_read(self, lua):
        return self._read.pop(0)


def _gs(write_lines, read_lines) -> GameState:
    gs = GameState.__new__(GameState)
    gs.conn = _StubConnection(write_lines, read_lines)
    return gs


class TestSetCityProductionVerification:
    def test_ok_path_verified(self):
        """CanStartOperation=true, verify confirms → return original OK."""
        gs = _gs(
            write_lines=[["OK:PRODUCING|BUILDING_MONUMENT|6 turns"]],
            read_lines=[["CONFIRMED"]],
        )
        result = asyncio.run(
            gs.set_city_production(65536, "BUILDING", "BUILDING_MONUMENT")
        )
        assert result == "PRODUCING|BUILDING_MONUMENT|6 turns"

    def test_ok_path_silent_failure(self):
        """Lua returns OK but verify reads NOT_SET → SILENT_FAILURE error."""
        gs = _gs(
            write_lines=[["OK:PRODUCING|UNIT_TRADER|1 turns"]],
            read_lines=[["NOT_SET|current=nil|expected=UNIT_TRADER"]],
        )
        result = asyncio.run(gs.set_city_production(262145, "UNIT", "UNIT_TRADER"))
        assert "SILENT_FAILURE" in result
        assert "UNIT_TRADER" in result
        assert "purchase_item" in result

    def test_cannot_produce_bypasses_verify(self):
        """Hard error from CanProduce check never reaches the verify path."""
        gs = _gs(
            write_lines=[
                [
                    "ERR:CANNOT_PRODUCE|BUILDING_UNIVERSITY cannot be produced "
                    "(requires DISTRICT_CAMPUS district)"
                ]
            ],
            read_lines=[],  # never called
        )
        result = asyncio.run(
            gs.set_city_production(65536, "BUILDING", "BUILDING_UNIVERSITY")
        )
        assert "CANNOT_PRODUCE" in result

    def test_verify_failure_falls_through(self):
        """If verify itself throws, return the original OK optimistically."""

        class _ThrowingConn:
            async def execute_write(self, lua):
                return ["OK:PRODUCING|UNIT_WARRIOR|2 turns"]

            async def execute_read(self, lua):
                raise RuntimeError("connection dropped")

        gs = GameState.__new__(GameState)
        gs.conn = _ThrowingConn()
        result = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_WARRIOR"))
        assert result == "PRODUCING|UNIT_WARRIOR|2 turns"
