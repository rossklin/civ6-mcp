"""Ad-hoc FireTuner driver for the diplomacy-under-handoff investigation.

Talks to the game directly so the MCP server does not have to hold the
single-client tuner port. See docs/human-vs-agent.md, "Diplomacy and trade
under handoff".

Usage:
    uv run python scripts/diplo_probe.py states
    uv run python scripts/diplo_probe.py exec InGame "print(Game.GetLocalPlayer())"
    uv run python scripts/diplo_probe.py shim          # patch DealManager.SendWorkingDeal
    uv run python scripts/diplo_probe.py unshim        # restore it
    uv run python scripts/diplo_probe.py watch 180     # stream all game output
"""

from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, "src")
from civ_mcp.tuner_client import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    connect,
    execute_lua,
    handshake,
    recv_message_timeout,
)

# Every UI context is its own Lua state with its own engine wrapper tables, so
# the shim has to be installed in DiplomacyDealView itself — patching the
# tuner's InGame DealManager would never have been seen by the game's own UI.
SHIM_STATE = "DiplomacyDealView"

SHIM_LUA = """
if __MCP_orig_SWD == nil then
  __MCP_orig_SWD = DealManager.SendWorkingDeal;
  DealManager.SendWorkingDeal = function(a, b, c)
    print("MCPDEAL|action=" .. tostring(a) .. "|from=" .. tostring(b) .. "|to=" .. tostring(c));
    return __MCP_orig_SWD(a, b, c);
  end
end
print("MCPSHIM|installed=" .. tostring(DealManager.SendWorkingDeal ~= __MCP_orig_SWD)
  .. "|dealmgr=" .. tostring(DealManager))
"""

UNSHIM_LUA = """
if __MCP_orig_SWD ~= nil then
  DealManager.SendWorkingDeal = __MCP_orig_SWD;
  __MCP_orig_SWD = nil;
end
print("MCPSHIM|removed")
"""


async def open_session():
    reader, writer = await connect(DEFAULT_HOST, DEFAULT_PORT)
    _app, raw = await handshake(reader, writer)
    # LSQ returns alternating [index_number, state_name] pairs.
    index: dict[str, int] = {}
    i = 0
    while i + 1 < len(raw):
        try:
            idx = int(raw[i])
        except ValueError:
            i += 1
            continue
        index.setdefault(raw[i + 1], idx)
        i += 2
    return reader, writer, index


def resolve(index: dict[str, int], name: str) -> int:
    if name in index:
        return index[name]
    raise SystemExit(f"No Lua state named {name!r}; have {sorted(index)}")


async def stream(reader, seconds: float) -> None:
    """Print every message the game sends until the deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        msg = await recv_message_timeout(reader, timeout=1.0)
        if msg is None:
            continue
        text = msg.payload.strip()
        if text:
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]

    reader, writer, index = await open_session()

    if cmd == "states":
        for name, i in sorted(index.items(), key=lambda kv: kv[1]):
            print(f"[{i}] {name}")

    elif cmd in ("exec", "execf"):
        state, arg = sys.argv[2], sys.argv[3]
        code = open(arg, encoding="utf-8").read() if cmd == "execf" else arg
        result = await execute_lua(reader, writer, resolve(index, state), code)
        print(result if result else "(no direct response)")
        await stream(reader, 1.5)

    elif cmd in ("shim", "unshim"):
        lua = SHIM_LUA if cmd == "shim" else UNSHIM_LUA
        await execute_lua(reader, writer, resolve(index, SHIM_STATE), lua)
        await stream(reader, 2.0)

    elif cmd == "watch":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
        print(f"Streaming game output for {seconds:.0f}s...", flush=True)
        await stream(reader, seconds)
        print("done.", flush=True)

    else:
        raise SystemExit(__doc__)

    writer.close()
    await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
