"""Test all lua_queries builders + parsers against a live game.

Usage: uv run python scripts/test_queries.py

Requires Civ 6 to be running with EnableTuner=1 and a game in progress.
Map coordinates depend on your current game — the script uses your first city's
position automatically.
"""

import asyncio
import sys

# Windows defaults stdout to the system ANSI codepage (cp1252), which crashes
# on non-Latin-1 characters (civ/city names). Force UTF-8 regardless of shell
# or .env configuration.
sys.stdout.reconfigure(encoding="utf-8")

from civ_mcp import lua as lq
from civ_mcp.connection import GameConnection
from civ_mcp.lua._helpers import load_lua_template


async def main():
    conn = GameConnection()
    await conn.connect()

    # Cities
    lines: list[str] = await conn.execute_write(load_lua_template("cities.lua"))
    print("## Cities\n\n" + "\n".join(lines))

    await conn.disconnect()


asyncio.run(main())
