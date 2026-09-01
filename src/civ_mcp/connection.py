"""Persistent, reconnectable FireTuner connection to Civilization VI.

Wraps tuner_client.py into a stateful connection manager with:
- Lua state index discovery (GameCore_Tuner, InGame)
- asyncio lock for serializing commands
- Sentinel-based multi-line response collection
- Output prefix parsing (O\x00<context>: <value>)
- Reconnection on connection loss
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from civ_mcp import tuner_client
from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)

# Every Lua builder spells the caller's own player exactly this way.
LOCAL_PLAYER_EXPR = "Game.GetLocalPlayer()"

# Lua commands whose engine round-trip exceeds this log at INFO instead of
# DEBUG. The engine figure excludes the fixed per-command drain overhead
# (~0.3s: stale + trailing drains in _locked_execute).
_SLOW_ENGINE_SECONDS = 1.0

# CIV_MCP_PROFILE — the same switch that turns on per-tool pyinstrument
# profiling in server.py — promotes every command timing line to INFO for
# the duration of a profiling session.
_PROMOTE_LUA_TIMING = bool(os.environ.get("CIV_MCP_PROFILE"))


class LuaError(Exception):
    """Raised when Lua code execution returns an error."""


def apply_perspective(lua_code: str, view_player: int | None) -> str:
    """Rewrite ``Game.GetLocalPlayer()`` to a literal player id.

    Under the human-vs-agent handoff the local-player slot moves between civs
    at every turn boundary, so an agent reading during someone else's turn
    would otherwise get *that* civ's state.  Binding the expression to the
    caller's own seat keeps every query answering for the right empire.

    A no-op when ``view_player`` is None, and a no-op in effect while the seat
    holds the local-player slot (the substituted id is the local player).
    """
    if view_player is None:
        return lua_code
    return lua_code.replace(LOCAL_PLAYER_EXPR, str(view_player))


class GameConnection:
    """Persistent FireTuner TCP connection to Civ 6."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4318):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.lua_states: dict[int, str] = {}  # index -> name
        self.gamecore_index: int | None = None
        self.ingame_index: int | None = None
        # Game-scoped, not player-scoped: several seats can build a turn report
        # inside the same game turn, and the MCP autosave is written once.
        self.last_autosave_turn: int | None = None
        # Deal mailbox — callbacks invoked when MCPDEAL| lines arrive over the
        # tuner socket (unsolicited print() output from the deal shim).
        self._deal_callbacks: list = []
        self._deal_monitor_task: asyncio.Task | None = None

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Connect to Civ 6 and discover Lua state indexes."""
        log.info("Connecting to Civ 6 at %s:%d", self.host, self.port)
        try:
            self._reader, self._writer = await tuner_client.connect(
                self.host, self.port
            )
        except (asyncio.TimeoutError, OSError) as e:
            raise ConnectionError(
                f"Cannot connect to Civ 6 at {self.host}:{self.port}. "
                "Is the game running with EnableTuner=1?"
            ) from e
        app_identity, raw_states = await tuner_client.handshake(
            self._reader, self._writer
        )
        log.info("Connected: %s", app_identity)

        # Parse state list: alternating [index_number, state_name] pairs
        self.lua_states = {}
        self.gamecore_index = None
        self.ingame_index = None
        i = 0
        while i + 1 < len(raw_states):
            try:
                idx = int(raw_states[i])
                name = raw_states[i + 1]
                self.lua_states[idx] = name
                if name == "GameCore_Tuner" and self.gamecore_index is None:
                    self.gamecore_index = idx
                if name == "InGame" and self.ingame_index is None:
                    self.ingame_index = idx
                i += 2
            except ValueError:
                i += 1

        log.info(
            "Discovered %d Lua states (GameCore=%s, InGame=%s)",
            len(self.lua_states),
            self.gamecore_index,
            self.ingame_index,
        )

    async def disconnect(self) -> None:
        if self._writer and not self._writer.is_closing():
            self._writer.close()
            await self._writer.wait_closed()
        self._writer = None
        self._reader = None

    # ------------------------------------------------------------------
    # Deal mailbox — unsolicited MCPDEAL message routing
    # ------------------------------------------------------------------

    def add_deal_callback(self, callback) -> None:
        """Register a callback for parsed MCPDEAL/MCPDIPLO events.

        ``callback(event_type: str, data: dict)`` is called for each parsed
        event.  ``event_type`` is one of ``"proposed"``, ``"click"``,
        ``"health"``, ``"item"``, ``"chat_send"``, ``"diplo_proposed"``,
        ``"diplo_click"``, ``"diplo_responded"``, ``"diplo_health"``.
        """
        self._deal_callbacks.append(callback)

    def remove_deal_callback(self, callback) -> None:
        try:
            self._deal_callbacks.remove(callback)
        except ValueError:
            pass

    async def start_deal_monitor(self, poll_interval: float = 0.5) -> None:
        """Start a background task that polls for unsolicited MCPDEAL lines.

        The monitor briefly acquires the connection lock to drain messages,
        so it yields to command execution.  Messages are parsed and routed
        to registered callbacks.
        """
        if self._deal_monitor_task is not None:
            return
        self._deal_monitor_task = asyncio.create_task(
            self._deal_monitor_loop(poll_interval), name="deal-monitor"
        )

    async def stop_deal_monitor(self) -> None:
        if self._deal_monitor_task is None:
            return
        self._deal_monitor_task.cancel()
        try:
            await self._deal_monitor_task
        except asyncio.CancelledError:
            pass
        self._deal_monitor_task = None

    async def poll_deal_messages(self) -> list[dict]:
        """Drain and parse any pending MCPDEAL messages.  Returns parsed events.

        Safe to call from any task — acquires the connection lock briefly.
        """
        if not self._reader or not self.is_connected:
            return []
        events: list[dict] = []
        try:
            async with self._lock:
                messages = await tuner_client.drain_messages(
                    self._reader, timeout=0.1
                )
        except Exception:
            return events
        for msg in messages:
            event = _parse_mcpdeal_line(msg.payload)
            if event:
                events.append(event)
        return events

    async def _deal_monitor_loop(self, poll_interval: float) -> None:
        """Background loop: poll for deal messages, route to callbacks."""
        while True:
            try:
                events = await self.poll_deal_messages()
                for event in events:
                    for cb in self._deal_callbacks:
                        try:
                            cb(event["type"], event)
                        except Exception:
                            log.debug(
                                "Deal callback failed", exc_info=True
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Deal monitor poll failed", exc_info=True)
            await asyncio.sleep(poll_interval)

    async def ensure_connected(self) -> None:
        """Connect (or reconnect) if not connected. Raises ConnectionError on failure."""
        if self.is_connected:
            return
        log.info("Connecting to Civ 6...")
        await self.connect()

    async def reconnect(self) -> None:
        """Force a fresh connection and re-discover Lua states."""
        await self.disconnect()
        await self.connect()

    async def _ensure_game_states(self) -> None:
        """Ensure we have GameCore and InGame state indexes.

        If connected but missing game states (e.g. connected at main menu),
        reconnect to re-discover states now that a game may be loaded.
        """
        await self.ensure_connected()
        if self.gamecore_index is None or self.ingame_index is None:
            log.info("Game states not found, reconnecting to re-discover...")
            await self.reconnect()
        if self.gamecore_index is None or self.ingame_index is None:
            raise ConnectionError(
                "GameCore_Tuner/InGame states not found. "
                "Make sure a game is in progress (not at the main menu)."
            )

    async def execute_read(
        self, lua_code: str, timeout: float = 5.0, perspective: bool = True
    ) -> list[str]:
        """Execute Lua in GameCore context (read state). Returns parsed output lines.

        Pass ``perspective=False`` for code that must see the *actual* local
        player rather than the calling seat's — turn-ownership probes and the
        handoff installer.
        """
        await self._ensure_game_states()
        return await self._execute_and_collect(
            self.gamecore_index, lua_code, timeout, perspective
        )

    async def execute_write(
        self, lua_code: str, timeout: float = 5.0, perspective: bool = True
    ) -> list[str]:
        """Execute Lua in InGame context (issue commands). Returns parsed output lines."""
        await self._ensure_game_states()
        return await self._execute_and_collect(
            self.ingame_index, lua_code, timeout, perspective
        )

    async def execute_in_state(
        self,
        state_index: int,
        lua_code: str,
        timeout: float = 5.0,
        perspective: bool = True,
    ) -> list[str]:
        """Execute Lua in an arbitrary state index. Returns parsed output lines."""
        return await self._execute_and_collect(
            state_index, lua_code, timeout, perspective
        )

    async def state_index_for(self, name: str) -> int | None:
        """Resolve a Lua state index by exact name, or None if absent.

        Every UI context is its own Lua state with its own copies of the engine
        wrapper tables (``DealManager`` in ``DiplomacyDealView`` is a different
        table from the one in ``InGame``), so reaching a context's globals means
        addressing its state directly.  Contexts are registered lazily, so a
        miss triggers one re-handshake before giving up.
        """
        await self.ensure_connected()
        for index, state in self.lua_states.items():
            if state == name:
                return index
        await self.reconnect()
        for index, state in self.lua_states.items():
            if state == name:
                return index
        return None

    async def execute_in_named_state(
        self, name: str, lua_code: str, timeout: float = 5.0
    ) -> list[str]:
        """Execute Lua in a named UI context's state. Empty list if absent.

        Never applies the seat perspective rewrite: these scripts act on the
        real local player's UI, not on a seat's view of the world.
        """
        index = await self.state_index_for(name)
        if index is None:
            log.warning("Lua state %r not found", name)
            return []
        return await self._execute_and_collect(index, lua_code, timeout, False)

    async def _execute_and_collect(
        self,
        state_index: int,
        lua_code: str,
        timeout: float,
        perspective: bool = True,
    ) -> list[str]:
        """Send Lua code and collect output lines until sentinel or timeout.

        Auto-reconnects once on dead socket (e.g. after game crash/reload).
        """
        if perspective:
            from civ_mcp.seats import get_view_player

            lua_code = apply_perspective(lua_code, get_view_player())
        await self.ensure_connected()
        async with self._lock:
            try:
                return await self._locked_execute(state_index, lua_code, timeout)
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                # Dead socket — reconnect once and retry (still holding lock)
                log.info("Connection lost, reconnecting...")
                await self.reconnect()
                return await self._locked_execute(state_index, lua_code, timeout)

    def _dispatch_unsolicited(self, messages) -> None:
        """Parse shim prints (MCPDEAL/MCPDIPLO/…) and fire deal callbacks.

        Used for messages drained before/after a command AND for unsolicited
        prints that interleave with a command's own output — a shim print
        landing between the command send and its sentinel must still become
        an event, or it is silently swallowed into the command result (the
        human's click happened, the screen closed, but the mailbox never
        heard about it).
        """
        for msg in messages:
            event = _parse_mcpdeal_line(msg.payload)
            if event:
                for cb in self._deal_callbacks:
                    try:
                        cb(event["type"], event)
                    except Exception:
                        log.debug("Deal callback failed", exc_info=True)

    async def _locked_execute(
        self, state_index: int, lua_code: str, timeout: float
    ) -> list[str]:
        """Inner execute — must be called while holding self._lock."""
        assert self._reader is not None
        assert self._writer is not None

        # Timing, reported via _log_lua_timing: `engine` is the send→(sentinel
        # or give-up) round-trip, `total` additionally covers the fixed drain
        # overhead and collection work. `timed_out` marks every break that
        # isn't the sentinel (deadline expiry or silent socket).
        start = time.perf_counter()
        sent_at = 0.0
        engine = 0.0
        timed_out = False
        error = False
        lines: list[str] = []
        try:
            # Drain any stale messages — route through deal callbacks so
            # MCPDEAL lines aren't lost between monitor poll cycles.
            stale = await tuner_client.drain_messages(self._reader, timeout=0.1)
            self._dispatch_unsolicited(stale)

            sent_at = time.perf_counter()
            await tuner_client.send_message(
                self._writer, tuner_client.TAG_COMMAND, f"CMD:{state_index}:{lua_code}"
            )

            deadline = asyncio.get_running_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break

                msg = await tuner_client.recv_message_timeout(
                    self._reader, timeout=min(remaining, 2.0)
                )
                if msg is None:
                    timed_out = True
                    break

                if msg.payload.startswith("ERR:"):
                    raise LuaError(msg.payload)

                # An unsolicited shim print can interleave with this command's
                # output — dispatch it as an event before treating it as a
                # result line (see _dispatch_unsolicited).
                self._dispatch_unsolicited([msg])

                text = _parse_output(msg.payload)
                if text is not None:
                    if text.strip() == SENTINEL:
                        break
                    lines.append(text)
                # Ignore non-output messages (e.g. tag=3 empty ack)

            engine = time.perf_counter() - sent_at

            # Drain any trailing unsolicited output — also route through deal
            # callbacks in case the Lua code produced MCPDEAL lines.
            trailing = await tuner_client.drain_messages(self._reader, timeout=0.2)
            self._dispatch_unsolicited(trailing)

            return lines
        except Exception:
            error = True
            raise
        finally:
            _log_lua_timing(
                state_index,
                lua_code,
                engine=engine,
                total=time.perf_counter() - start,
                lines=lines,
                timed_out=timed_out,
                error=error,
            )


def _parse_output(payload: str) -> str | None:
    """Extract value from a print() output message.

    Format: O\\x00<context_name>: <value>
    Returns the value part, or None if not an output message.
    """
    if not payload.startswith("O"):
        return None

    # Find the ': ' separator after the context name
    sep = payload.find(": ", 2)
    if sep >= 0:
        return payload[sep + 2 :]

    # Fallback: strip the O and null byte prefix
    return payload.lstrip("O").lstrip("\x00").strip()


def _log_lua_timing(
    state_index: int,
    lua_code: str,
    engine: float,
    total: float,
    lines: list[str],
    timed_out: bool,
    error: bool,
) -> None:
    """Log per-command latency so slow tools are attributable.

    Every FireTuner command funnels through _locked_execute, so this is the
    one place that can say whether a slow tool was slow in the game engine
    or on the Python side: ``engine`` is the send→sentinel round-trip,
    ``total − engine`` is fixed drain overhead plus collection work here.
    Python-side CPU hotspots beyond that show up in pyinstrument reports
    (see CIV_MCP_PROFILE in server.py).
    """
    label = " ".join(lua_code.split())[:80]
    result = f"{len(lines)} lines/{sum(len(l) for l in lines) // 1024}KB"
    status = "TIMEOUT" if timed_out else ("ERROR" if error else "ok")
    if _PROMOTE_LUA_TIMING or timed_out or error or engine >= _SLOW_ENGINE_SECONDS:
        log.info(
            "lua[%d] status=%s engine=%.0fms total=%.0fms out=%s | %s",
            state_index,
            status,
            engine * 1000,
            total * 1000,
            result,
            label,
        )
    else:
        log.debug(
            "lua[%d] status=%s engine=%.0fms total=%.0fms out=%s | %s",
            state_index,
            status,
            engine * 1000,
            total * 1000,
            result,
            label,
        )


# ---------------------------------------------------------------------------
# MCPDEAL line parser
# ---------------------------------------------------------------------------

# Tracks partial deal serialisations across multiple print() lines.
# Keyed by something unique to the connection; in practice only one deal
# is serialised at a time (the human can only have one deal screen open).
_pending_deal_items: list[dict] = []
_pending_deal_from: int = -1
_pending_deal_to: int = -1


def _parse_mcpdeal_line(payload: str) -> dict | None:
    """Parse one unsolicited output line into a deal event dict, or None.

    Multi-line deal serialisations (``MCPDEAL_ITEM`` / ``MCPDEAL_END``) are
    accumulated in module-level state and emitted as a single ``"proposed"``
    event when ``MCPDEAL_END`` arrives.

    Returns::

        {"type": "proposed", "action": "PROPOSED", "from": int, "to": int,
         "items": [{"type": "GOLD", "from": int, "amount": 200, ...}, ...]}
        {"type": "click", "proposal_id": str, "pid": int}
        {"type": "health", "ok": bool}
        {"type": "chat_send", "from": int, "to": int, "ttype": int, "text": str}
        {"type": "diplo_proposed", "from": int, "to": int, "action": str}
        {"type": "diplo_click", "proposal_id": str, "pid": int}
        {"type": "diplo_responded", "proposal_id": str, "response": str}
        {"type": "diplo_health", "ok": bool}
    """
    global _pending_deal_items, _pending_deal_from, _pending_deal_to

    text = _parse_output(payload)
    if text is None:
        return None

    # Notification click — forwarded by the click handler
    if text.startswith("MCPDEAL_CLICK|"):
        parts = text[len("MCPDEAL_CLICK|"):].split("|")
        data: dict = {"type": "click", "proposal_id": parts[0] if parts else ""}
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "pid":
                    data["pid"] = int(v)
        return data

    # Diplo shim: human proposed a response-able diplo action (friendship /
    # delegation / embassy) to a managed civ from the native leader screen.
    # Format: MCPDIPLO|PROPOSED|from=<pid>|to=<pid>|action=<session string>
    if text.startswith("MCPDIPLO|"):
        parts = text[len("MCPDIPLO|"):].split("|")
        if parts and parts[0] == "PROPOSED":
            data: dict = {"type": "diplo_proposed"}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in ("from", "to"):
                        try:
                            data[k] = int(v)
                        except ValueError:
                            data[k] = -1
                    elif k == "action":
                        data["action"] = v
            log.info(
                "DIPLO TRACE: human P%s proposed %s to P%s",
                data.get("from", -1),
                data.get("action", "?"),
                data.get("to", -1),
            )
            return data
        return None

    # Diplo notification click — forwarded by the click handler.
    # Format: MCPDIPLO_CLICK|<proposal_id>|pid=<pid>
    if text.startswith("MCPDIPLO_CLICK|"):
        parts = text[len("MCPDIPLO_CLICK|"):].split("|")
        data: dict = {
            "type": "diplo_click",
            "proposal_id": parts[0] if parts else "",
        }
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                if k == "pid":
                    data["pid"] = int(v)
        return data

    # Diplo shim: human answered a presented mailbox proposal on the native
    # leader screen. Format: MCPDIPLO_RESPONDED|<proposal_id>|<response>
    # (POSITIVE / NEGATIVE / REJECTED_PERMANENT / RESPONSE_IGNORE).
    if text.startswith("MCPDIPLO_RESPONDED|"):
        parts = text[len("MCPDIPLO_RESPONDED|"):].split("|")
        if len(parts) >= 2:
            return {
                "type": "diplo_responded",
                "proposal_id": parts[0],
                "response": parts[1],
            }
        return None

    # Diplo shim health check
    if text.startswith("DIPLOSHIM_HEALTH|"):
        status = text[len("DIPLOSHIM_HEALTH|"):].strip()
        return {"type": "diplo_health", "ok": status == "true"}

    # Deal shim health check
    if text.startswith("DEALSHIM_HEALTH|"):
        status = text[len("DEALSHIM_HEALTH|"):].strip()
        return {"type": "health", "ok": status == "true"}

    # Diagnostic trace lines
    if text.startswith("MCP_TRACE|"):
        return {"type": "trace", "msg": text[len("MCP_TRACE|"):]}

    # Notification sent acknowledgment
    if text.startswith("NOTIFY_SENT|"):
        return {"type": "notify_sent", "proposal_id": text[len("NOTIFY_SENT|"):].strip()}

    # Chat shim: human typed a message in the native chat panel.
    # Format: MCPCHAT|SEND|from=<pid>|to=<pid>|ttype=<enum>|hex=<hex text>
    # The text is hex-encoded by the shim so pipes/newlines/quotes cannot
    # break the pipe-delimited line. The hex field is always last and contains
    # only [0-9a-f], so split("|") is safe.
    if text.startswith("MCPCHAT|"):
        rest = text[len("MCPCHAT|"):]
        parts = rest.split("|")
        verb = parts[0] if parts else ""
        if verb == "SEND":
            data: dict = {"type": "chat_send"}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k in ("from", "to", "ttype"):
                        try:
                            data[k] = int(v)
                        except ValueError:
                            pass
                    elif k == "hex":
                        data["hex"] = v
            if "hex" in data:
                try:
                    data["text"] = bytes.fromhex(data["hex"]).decode(
                        "utf-8", errors="replace"
                    )
                except ValueError:
                    data["text"] = ""
            return data
        return None

    # Deal proposal header
    if text.startswith("MCPDEAL|"):
        rest = text[len("MCPDEAL|"):]
        parts = rest.split("|")
        action_code = -1
        from_pid = -1
        to_pid = -1
        for part in parts:
            if (part == "PROPOSED"):
                action_code = 4
            elif (part == "INSPECT"):
                action_code = 7
            elif "=" in part:
                k, v = part.split("=", 1)
                if k == "action":
                    try:
                        action_code = int(v)
                    except ValueError:
                        action_code = -1
                elif k == "from":
                    try:
                        from_pid = int(v)
                    except ValueError:
                        pass
                elif k == "to":
                    try:
                        to_pid = int(v)
                    except ValueError:
                        pass
        # INSPECT (7): suppressed for managed targets.
        if action_code == 7 or "suppressed" in rest:
            return {"type": "inspect_suppressed", "from": from_pid, "to": to_pid}
        # PROPOSED (4): start accumulating items.
        if action_code == 4:
            _pending_deal_items = []
            _pending_deal_from = from_pid
            _pending_deal_to = to_pid
            log.info("DEAL TRACE: got %s", text)
            log.info("DEAL TRACE: start parsing proposal from P%d to P%d", from_pid, to_pid)
            return None  # wait for items

        return {"type": "unknown", "action": str(action_code)}

    # Deal item
    if text.startswith("MCPDEAL_ITEM|"):
        parts = text[len("MCPDEAL_ITEM|"):].split("|")
        item: dict = {}
        item_type = parts[0] if parts else "UNKNOWN"
        item["item_type"] = item_type
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    item[k] = int(v)
                except ValueError:
                    item[k] = v
        _pending_deal_items.append(item)
        return None  # still accumulating

    # End of deal serialisation — emit the complete proposal
    if text.startswith("MCPDEAL_END"):
        items = _pending_deal_items
        from_pid = _pending_deal_from
        to_pid = _pending_deal_to
        _pending_deal_items = []
        _pending_deal_from = -1
        _pending_deal_to = -1
        log.info(
            "DEAL TRACE: finished parsing proposal from P%d to P%d (%d items)",
            from_pid,
            to_pid,
            len(items),
        )
        return {
            "type": "proposed",
            "action": "PROPOSED",
            "from": from_pid,
            "to": to_pid,
            "items": items,
        }

    return None
