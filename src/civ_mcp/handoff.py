"""Turn-boundary local-player handoff — human + N external agents in one game.

Civ 6 single-player allows exactly one human player at a time, and
``PlayerManager.SetLocalPlayerAndObserver(N)`` *swaps* the human designation
rather than granting it.  Switching at a turn boundary — when the civ being
left behind is already turn-complete — is safe; switching mid-turn hands that
civ's remaining turn to the built-in AI.

So the whole mechanism is a single persistent ``GameEvents.PlayerTurnStarted``
handler registered in the **GameCore** Lua state over the existing FireTuner
connection.  When a managed player's turn starts, the local-player (human)
designation moves to it and the engine halts waiting for input.  The human
plays their turn in the UI; each agent plays its turn over MCP.

No mod is required — the event tables are reachable from the tuner states.

Handlers live in the tuner Lua state and are destroyed on game load, so
``install`` is idempotent and must be re-run after every save load.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from civ_mcp.connection import GameConnection
from civ_mcp.lua._helpers import SENTINEL

log = logging.getLogger(__name__)

# Bounded diagnostic ring buffer kept in the Lua state.
_LOG_LIMIT = 64


@dataclass(frozen=True)
class HandoffConfig:
    """Which player is the human and which players external agents drive."""

    enabled: bool = False
    human_id: int = 0
    agent_ids: tuple[int, ...] = ()

    @property
    def managed_ids(self) -> tuple[int, ...]:
        """Every player the handoff handler switches the human slot to."""
        return (self.human_id,) + self.agent_ids

    def __post_init__(self) -> None:
        if self.human_id in self.agent_ids:
            raise ValueError(
                f"human_id {self.human_id} also listed as an agent player: "
                f"{self.agent_ids}"
            )
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError(f"duplicate agent player ids: {self.agent_ids}")
        for pid in self.managed_ids:
            if not 0 <= pid <= 61:
                raise ValueError(f"player id out of range for a major civ: {pid}")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HandoffConfig:
        """Build config from ``CIV_MCP_AGENT_PLAYERS`` / ``CIV_MCP_HUMAN_PLAYER``.

        ``CIV_MCP_AGENT_PLAYERS`` is a comma-separated list of player ids the
        MCP agents control (e.g. ``"1,2"``).  Setting it enables handoff mode.
        ``CIV_MCP_HUMAN_PLAYER`` defaults to 0.
        """
        e = os.environ if env is None else env
        raw = (e.get("CIV_MCP_AGENT_PLAYERS") or "").strip()
        if not raw:
            return cls()
        agent_ids = tuple(int(p) for p in raw.replace(" ", "").split(",") if p)
        human_id = int((e.get("CIV_MCP_HUMAN_PLAYER") or "0").strip() or 0)
        return cls(enabled=bool(agent_ids), human_id=human_id, agent_ids=agent_ids)


@dataclass
class TurnOwnership:
    """Snapshot of who currently holds the human (local player) slot."""

    turn: int | None
    local_player: int | None
    handler_installed: bool = False
    managed: tuple[int, ...] = field(default_factory=tuple)


def build_install_lua(cfg: HandoffConfig, force: bool = False) -> str:
    """GameCore Lua that (re)registers the turn-boundary handoff handler.

    Idempotent: re-running updates the managed player set without stacking
    duplicate listeners.  ``force`` removes an existing listener first — use
    it after a save load, when the Lua state may have been recycled.

    Never uses ``RemoveAll()`` — that would strip the game's own listeners.
    """
    players = " ".join(f"h.players[{pid}] = true" for pid in cfg.managed_ids)
    force_lua = (
        "if h.fn then "
        "pcall(function() GameEvents.PlayerTurnStarted.Remove(h.fn) end) "
        "h.fn = nil "
        "end "
        if force
        else ""
    )
    return (
        "__civmcp_handoff = __civmcp_handoff or {} "
        "local h = __civmcp_handoff "
        "h.log = h.log or {} "
        "h.enabled = true "
        "h.players = {} "
        f"{players} "
        f"{force_lua}"
        "if not h.fn then "
        "  h.fn = function(pid) "
        "    local s = __civmcp_handoff "
        "    if not s.enabled then return end "
        "    if not s.players[pid] then return end "
        "    local before = Game.GetLocalPlayer() "
        "    local ok, err = pcall(function() "
        "      PlayerManager.SetLocalPlayerAndObserver(pid) "
        "    end) "
        "    s.log[#s.log + 1] = table.concat({ "
        "      tostring(Game.GetCurrentGameTurn()), tostring(pid), "
        "      tostring(before), tostring(Game.GetLocalPlayer()), "
        '      ok and "ok" or tostring(err) }, "|") '
        f"    while #s.log > {_LOG_LIMIT} do table.remove(s.log, 1) end "
        "  end "
        "  GameEvents.PlayerTurnStarted.Add(h.fn) "
        '  print("HANDOFF|installed") '
        "else "
        '  print("HANDOFF|updated") '
        "end "
        f'print("{SENTINEL}")'
    )


def build_uninstall_lua() -> str:
    """GameCore Lua that removes the handoff handler and disables the flag."""
    return (
        "local h = __civmcp_handoff "
        "if not h then "
        '  print("HANDOFF|absent") '
        f'  print("{SENTINEL}") '
        "  return "
        "end "
        "h.enabled = false "
        "if h.fn then "
        "  pcall(function() GameEvents.PlayerTurnStarted.Remove(h.fn) end) "
        "  h.fn = nil "
        "end "
        'print("HANDOFF|removed") '
        f'print("{SENTINEL}")'
    )


def build_status_lua() -> str:
    """GameCore Lua reporting turn, local player, and handler presence."""
    return (
        'print("TURN|" .. tostring(Game.GetCurrentGameTurn())) '
        'print("LOCAL|" .. tostring(Game.GetLocalPlayer())) '
        "local h = __civmcp_handoff "
        'print("HOOK|" .. tostring(h ~= nil and h.fn ~= nil and h.enabled == true)) '
        "if h and h.players then "
        "  local ids = {} "
        "  for pid, on in pairs(h.players) do "
        "    if on then ids[#ids + 1] = tostring(pid) end "
        "  end "
        '  print("MANAGED|" .. table.concat(ids, ",")) '
        "end "
        f'print("{SENTINEL}")'
    )


def build_log_lua() -> str:
    """GameCore Lua dumping the handoff diagnostic ring buffer."""
    return (
        "local h = __civmcp_handoff "
        "if h and h.log then "
        '  for _, e in ipairs(h.log) do print("EVT|" .. e) end '
        "end "
        f'print("{SENTINEL}")'
    )


def build_roster_lua(player_ids: tuple[int, ...]) -> str:
    """GameCore Lua naming each managed player's civ and leader."""
    ids = ",".join(str(p) for p in player_ids)
    return (
        f"for _, pid in ipairs({{{ids}}}) do "
        "  local cfg = PlayerConfigurations[pid] "
        "  if cfg then "
        '    local civ = "?" '
        '    local leader = "?" '
        "    pcall(function() civ = Locale.Lookup(cfg:GetCivilizationShortDescription()) end) "
        "    pcall(function() leader = Locale.Lookup(cfg:GetLeaderName()) end) "
        "    local alive = false "
        "    pcall(function() alive = Players[pid]:IsAlive() end) "
        '    print("CIV|" .. pid .. "|" .. tostring(civ) .. "|" '
        '      .. tostring(leader) .. "|" .. tostring(alive)) '
        "  end "
        "end "
        f'print("{SENTINEL}")'
    )


def parse_roster(lines: list[str]) -> dict[int, tuple[str, str, bool]]:
    """Parse :func:`build_roster_lua` into ``{pid: (civ, leader, alive)}``."""
    roster: dict[int, tuple[str, str, bool]] = {}
    for line in lines:
        if not line.startswith("CIV|"):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        pid = _parse_int(parts[1])
        if pid is None:
            continue
        roster[pid] = (parts[2], parts[3], parts[4].strip() == "true")
    return roster


async def get_roster(
    conn: GameConnection, player_ids: tuple[int, ...]
) -> dict[int, tuple[str, str, bool]]:
    """Civ and leader names for the given players.  Empty dict on failure."""
    if not player_ids:
        return {}
    try:
        lines = await conn.execute_read(build_roster_lua(player_ids), perspective=False)
    except Exception:
        log.debug("Roster query failed", exc_info=True)
        return {}
    return parse_roster(lines)


def _parse_int(raw: str) -> int | None:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_status(lines: list[str]) -> TurnOwnership:
    """Parse the output of :func:`build_status_lua`."""
    turn: int | None = None
    local: int | None = None
    hook = False
    managed: tuple[int, ...] = ()
    for line in lines:
        if line.startswith("TURN|"):
            turn = _parse_int(line[5:])
        elif line.startswith("LOCAL|"):
            local = _parse_int(line[6:])
        elif line.startswith("HOOK|"):
            hook = line[5:].strip() == "true"
        elif line.startswith("MANAGED|"):
            ids = [_parse_int(p) for p in line[8:].split(",") if p.strip()]
            managed = tuple(sorted(i for i in ids if i is not None))
    return TurnOwnership(
        turn=turn, local_player=local, handler_installed=hook, managed=managed
    )


async def install(conn: GameConnection, cfg: HandoffConfig, force: bool = False) -> str:
    """Register the handoff handler in the GameCore state.

    Safe to call repeatedly.  Returns a short status string.
    """
    lines = await conn.execute_read(
        build_install_lua(cfg, force=force), perspective=False
    )
    state = next((l[8:] for l in lines if l.startswith("HANDOFF|")), "unknown")
    log.info(
        "Handoff %s: human=P%d agents=%s",
        state,
        cfg.human_id,
        ",".join(f"P{p}" for p in cfg.agent_ids),
    )
    return state


async def uninstall(conn: GameConnection) -> str:
    """Remove the handoff handler, leaving the game's own listeners intact."""
    lines = await conn.execute_read(build_uninstall_lua(), perspective=False)
    return next((l[8:] for l in lines if l.startswith("HANDOFF|")), "unknown")


async def get_ownership(conn: GameConnection) -> TurnOwnership:
    """Read who currently holds the human slot, and whether the hook is live."""
    lines = await conn.execute_read(build_status_lua(), perspective=False)
    return parse_status(lines)


async def try_ownership(conn: GameConnection) -> TurnOwnership:
    """Like :func:`get_ownership` but reports an unreachable game as unknown.

    Agents can connect before the human has loaded a save, so the seat and
    turn-status tools must answer rather than raise.
    """
    try:
        return await get_ownership(conn)
    except Exception:
        log.debug("Ownership probe failed", exc_info=True)
        return TurnOwnership(turn=None, local_player=None)


def build_hand_back_lua(human_id: int, agent_ids: tuple[int, ...]) -> str:
    """GameCore Lua returning the human slot to the human if an agent holds it.

    Switching away from a civ whose turn is still active hands that turn to the
    built-in AI — normally the thing to avoid, but exactly right here: nobody is
    driving the agent civ any more, so the AI should finish for it.
    """
    ids = ",".join(str(p) for p in agent_ids)
    return (
        "local cur = Game.GetLocalPlayer() "
        f"local agents = {{{ids}}} "
        "local held = false "
        "for _, pid in ipairs(agents) do if pid == cur then held = true end end "
        "if held then "
        f"  local ok = pcall(function() PlayerManager.SetLocalPlayerAndObserver({human_id}) end) "
        '  print("HANDBACK|" .. tostring(ok) .. "|" .. tostring(Game.GetLocalPlayer())) '
        "else "
        '  print("HANDBACK|skip|" .. tostring(cur)) '
        "end "
        f'print("{SENTINEL}")'
    )


async def hand_back(conn: GameConnection, cfg: HandoffConfig) -> str:
    """Disarm the hook and return the human slot to the human.

    Called when the last agent disconnects: without this the game would sit
    forever on an agent civ's turn with nobody to play it.
    """
    if not cfg.enabled:
        return "disabled"
    await uninstall(conn)
    lines = await conn.execute_read(
        build_hand_back_lua(cfg.human_id, cfg.agent_ids), perspective=False
    )
    result = next((l for l in lines if l.startswith("HANDBACK|")), "HANDBACK|unknown")
    log.info("Handoff torn down: %s", result)
    return result


async def read_log(conn: GameConnection) -> list[str]:
    """Return the handoff diagnostic ring buffer, oldest first."""
    lines = await conn.execute_read(build_log_lua(), perspective=False)
    return [l[4:] for l in lines if l.startswith("EVT|")]


class HandoffKeeper:
    """Background task that keeps the handoff hook armed.

    The handler lives in the tuner Lua state and is destroyed whenever a save
    is loaded or the game restarts, which would silently drop every agent out
    of the game.  Rather than chase every load path, this polls for the hook
    and re-arms it whenever it goes missing.
    """

    def __init__(
        self,
        conn: GameConnection,
        cfg: HandoffConfig,
        poll_interval: float = 10.0,
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._interval = poll_interval
        self._task = None
        self.last_ownership: TurnOwnership | None = None
        self.installs = 0

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        self._task = asyncio.create_task(self._run(), name="handoff-keeper")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def ensure_installed(self, force: bool = False) -> TurnOwnership:
        """Arm the hook if it is missing.  Returns fresh ownership state."""
        own = await get_ownership(self._conn)
        if force or not own.handler_installed:
            await install(self._conn, self._cfg, force=force)
            self.installs += 1
            own = await get_ownership(self._conn)
        self.last_ownership = own
        return own

    async def _run(self) -> None:
        previous: int | None = None
        while True:
            try:
                own = await self.ensure_installed()
                if own.local_player != previous:
                    log.info(
                        "Local player now P%s (turn %s)",
                        own.local_player,
                        own.turn,
                    )
                    previous = own.local_player
            except Exception:
                # No connection yet, or the game is mid-load. Try again later.
                log.debug("Handoff keeper poll failed", exc_info=True)
            await asyncio.sleep(self._interval)


def describe_ownership(own: TurnOwnership, cfg: HandoffConfig, seat_id: int) -> str:
    """One-line summary of who is on the clock, from ``seat_id``'s point of view."""
    if own.local_player is None:
        return "Turn owner unknown — could not read the local player."
    if own.local_player == seat_id:
        return f"T{own.turn}: you (P{seat_id}) are on the clock."
    if own.local_player == cfg.human_id:
        return f"T{own.turn}: the human (P{cfg.human_id}) is playing."
    if own.local_player in cfg.agent_ids:
        return f"T{own.turn}: agent P{own.local_player} is playing."
    return f"T{own.turn}: built-in AI P{own.local_player} is processing."


async def wait_for_turn(
    gs,
    seat,
    cfg: HandoffConfig,
    timeout_seconds: float = 90.0,
    poll_interval: float = 3.0,
) -> str:
    """Block until ``seat`` holds the local-player slot, then report the round.

    Returns the deferred post-turn report (snapshot diff, threats, empire
    warnings) built from the baseline stashed when the seat ended its turn, so
    the agent starts its turn knowing what changed while it was off the clock.

    On timeout it returns the current ownership instead of raising — the agent
    simply calls again.  Blocking for the whole round in one tool call would
    outlive most MCP client request timeouts.
    """
    from civ_mcp.end_turn import build_post_turn_report

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0.0)
    own = await try_ownership(gs.conn)
    while own.local_player != seat.player_id:
        if own.local_player is not None and not own.handler_installed:
            return (
                "The turn-handoff hook is not installed, so the local-player "
                "slot will never move to your civ. "
                f"{describe_ownership(own, cfg, seat.player_id)}\n"
                "Ask the operator to call reinstall_handoff() — the hook is "
                "wiped whenever a save is loaded."
            )
        if loop.time() >= deadline:
            if own.local_player is None:
                return (
                    "Cannot reach the game — the human may not have a save "
                    "loaded yet. Call wait_for_turn() again to keep waiting."
                )
            return (
                f"Not your turn yet. {describe_ownership(own, cfg, seat.player_id)}\n"
                "Read tools answer for your empire while you wait — or call "
                "wait_for_turn() again to keep waiting."
            )
        await asyncio.sleep(poll_interval)
        own = await try_ownership(gs.conn)

    # On the clock. A new game turn resets the per-turn advisor budget, which
    # execute_end_turn cannot do for a seat (the turn counter does not move
    # when a seat ends its turn mid-round).
    if own.turn is not None and own.turn > gs._high_water_turn:
        gs._advisor_calls_this_turn = 0
        gs._high_water_turn = own.turn

    pending = seat.pending_report
    if pending is None:
        return (
            f"Your turn — P{seat.player_id}, turn {own.turn}. "
            "No previous turn to diff against; call get_game_overview to orient."
        )
    seat.pending_report = None
    try:
        return await build_post_turn_report(
            gs,
            pending.snapshot,
            pending.turn_before,
            own.turn,
            pending.threats_before,
        )
    except Exception:
        log.warning("Deferred turn report failed", exc_info=True)
        return (
            f"Your turn — P{seat.player_id}, turn {own.turn}. "
            "Turn report failed to build; query state directly."
        )
