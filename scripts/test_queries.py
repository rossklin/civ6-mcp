"""Test all lua_queries builders + parsers against a live game.

Usage: uv run python scripts/test_queries.py

Requires Civ 6 to be running with EnableTuner=1 and a game in progress.
Map coordinates depend on your current game — the script uses your first city's
position automatically.
"""

import asyncio

from civ_mcp import lua_queries as lq
from civ_mcp.connection import GameConnection


async def main():
    conn = GameConnection()
    await conn.connect()

    # Overview
    lines = await conn.execute_read(lq.build_overview_query())
    ov = lq.parse_overview_response(lines)
    print(f"OVERVIEW: {ov}")

    # Units
    lines = await conn.execute_write(lq.build_units_query())
    units = lq.parse_units_response(lines)
    print(f"UNITS ({len(units)}):")
    for u in units:
        print(f"  {u}")

    # Cities
    lines = await conn.execute_read(lq.build_cities_query())
    cities = lq.parse_cities_response(lines)
    print(f"CITIES ({len(cities)}):")
    for c in cities:
        print(f"  {c}")

    # Diplomacy
    lines = await conn.execute_read(lq.build_diplomacy_query())
    civs = lq.parse_diplomacy_response(lines)
    print(f"DIPLOMACY ({len(civs)} civs):")
    for c in civs:
        print(f"  {c}")

    # Tech/Civics
    lines = await conn.execute_read(lq.build_tech_civics_query())
    tc = lq.parse_tech_civics_response(lines)
    print(f"TECH/CIVICS: {tc}")

    await conn.disconnect()


asyncio.run(main())
