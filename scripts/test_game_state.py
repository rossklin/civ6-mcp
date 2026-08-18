"""Test GameState layer against a live game.

Usage: uv run python scripts/test_game_state.py

Requires Civ 6 to be running with EnableTuner=1 and a game in progress.
Map coordinates depend on your current game — adjust as needed.
"""

import asyncio
import sys

# Windows defaults stdout to the system ANSI codepage (cp1252), which crashes
# on non-Latin-1 characters (civ/city names). Force UTF-8 regardless of shell
# or .env configuration.
sys.stdout.reconfigure(encoding="utf-8")

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState

async def main():
    conn = GameConnection()
    await conn.connect()
    gs = GameState(conn)

    # The managed ids only matter for formatting of the diplomacy section
    print(await gs.get_full_game_state({0,1}))

    await conn.disconnect()


asyncio.run(main())
