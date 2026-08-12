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
from pathlib import Path

from civ_mcp.connection import GameConnection
from civ_mcp.lua._helpers import SENTINEL, lua_quote

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


#: UI contexts whose ``g_bIsLocalPlayerTurn`` needs repairing after a handoff.
#: Only this one: the flag appears in ``DiplomacyActionView.lua`` and in the
#: Expansion2 script that replaces it, and a replacement runs inside the same
#: context.  The ``_AllianceTab`` / ``_WorldCongressTab`` siblings are separate
#: Lua states but never read the flag.
DIPLOMACY_UI_STATES = ("DiplomacyActionView",)


def build_diplomacy_ui_fix_lua() -> str:
    """Lua that repairs the diplomacy screen's turn flag after a handoff.

    ``SetLocalPlayerAndObserver`` raises ``Events.LocalPlayerChanged``
    immediately before the new local player's turn activates, and the engine
    then **suppresses** ``Events.LocalPlayerTurnBegin`` for that activation
    (measured live 2026-08-05, turns 47-50).  ``DiplomacyActionView`` only ever
    sets ``g_bIsLocalPlayerTurn = true`` from that event, so after the human's
    own End Turn cleared it the flag stays false for their whole next turn and
    every action button is disabled at DiplomacyActionView.lua:736/740.

    The repair delivers the swallowed event by calling the screen's own
    ``OnLocalPlayerTurnBegin`` once the local player's turn really is active.
    Registered on both candidate events because their order differs between a
    normal turn start and a handoff one; the flag check makes it idempotent.
    """
    return (
        "if g_bIsLocalPlayerTurn == nil then "
        '  print("DIPLOFIX|absent") '
        f'  print("{SENTINEL}") '
        "  return "
        "end "
        "if __civmcp_diplo_fix == nil then "
        "  __civmcp_diplo_fix = function() "
        "    local pid = Game.GetLocalPlayer() "
        "    if pid == nil or pid < 0 then return end "
        "    local p = Players[pid] "
        "    if p ~= nil and p:IsTurnActive() and not g_bIsLocalPlayerTurn then "
        "      pcall(OnLocalPlayerTurnBegin) "
        "    end "
        "  end "
        "  Events.LocalPlayerChanged.Add(__civmcp_diplo_fix) "
        "  Events.PlayerTurnActivated.Add(__civmcp_diplo_fix) "
        "  __civmcp_diplo_fix() "
        '  print("DIPLOFIX|installed") '
        "else "
        "  __civmcp_diplo_fix() "
        '  print("DIPLOFIX|present") '
        "end "
        f'print("{SENTINEL}")'
    )


async def install_diplomacy_ui_fix(conn: GameConnection) -> str:
    """Install the turn-flag repair in each diplomacy UI context.

    Contexts are rebuilt on every save load, so this is re-applied by
    :class:`HandoffKeeper` alongside the hook itself.  A context that does not
    define ``g_bIsLocalPlayerTurn`` is simply skipped.
    """
    results = []
    for state in DIPLOMACY_UI_STATES:
        lines = await conn.execute_in_named_state(
            state, build_diplomacy_ui_fix_lua()
        )
        status = next((l[9:] for l in lines if l.startswith("DIPLOFIX|")), None)
        if status is not None:
            results.append(f"{state}={status}")
    if results:
        log.info("Diplomacy UI fix: %s", ", ".join(results))
    return ", ".join(results) if results else "no diplomacy contexts"


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
    # The hook and the UI contexts die together on a save load, so the screen
    # repair is armed on the same path rather than on a schedule of its own.
    await install_diplomacy_ui_fix(conn)
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
        self._post_install_hooks: list = []

    def add_post_install_hook(self, hook) -> None:
        """Register an async callable to run after each keeper re-arm cycle.

        Used to re-arm the deal shim and notification handler, which live in
        UI contexts that are rebuilt on save load.
        """
        self._post_install_hooks.append(hook)

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
                    # Once armed the repair is self-sustaining, but a context
                    # rebuilt between polls would have lost it. A handoff just
                    # happened, so this is the cheap moment to re-arm.
                    await install_diplomacy_ui_fix(self._conn)
                    for hook in self._post_install_hooks:
                        try:
                            await hook()
                        except Exception:
                            log.debug(
                                "Post-install hook failed", exc_info=True
                            )
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
    """Block until ``seat`` holds the local-player slot, then return a status.

    On timeout it returns the current ownership instead of raising — the agent
    simply calls again.  Blocking for the whole round in one tool call would
    outlive most MCP client request timeouts.
    """
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

    # The deferred post-turn report is delivered by get_full_game_state (see
    # server.get_full_game_state), not here. Just tell the agent to orient.
    if seat.pending_report is not None:
        return (
            f"Your turn — P{seat.player_id}, turn {own.turn}. "
            "Call get_full_game_state to orient: it includes the turn report "
            "for the round that just finished (what changed, threats, warnings)."
        )
    return (
        f"Your turn — P{seat.player_id}, turn {own.turn}. "
        "No previous turn to diff against; call get_full_game_state to orient."
    )


# ---------------------------------------------------------------------------
# Deal mailbox — DiplomacyDealView shim
# ---------------------------------------------------------------------------
# Installed in the DiplomacyDealView Lua state alongside the diplomacy UI fix.
# Wraps DealManager.SendWorkingDeal to intercept proposals to managed civs and
# overrides IsAutoPropose for managed targets so the screen stops firing INSPECT.
#
# The shim lives in a UI context, so it dies on save load and is re-armed by
# HandoffKeeper on the same path as the diplomacy UI fix.

DEAL_SHIM_STATE = "DiplomacyDealView"


# Template Lua for the deal shim, kept in a separate file for readability.
# Two tags are substituted per-call by build_deal_shim_install_lua():
#   __MCP_MANAGED_IDS_TAG__  -> {[p]=true,...} for the managed IDs
#   __MCP_SENTINEL_TAG__     -> the response sentinel (_helpers.SENTINEL)
_DEAL_SHIM_PATH = Path(__file__).resolve().parent.parent / "lua" / "deal_shim.lua"
_DEAL_SHIM_TEMPLATE: str | None = None


def _load_deal_shim_template() -> str:
    """Read and cache the deal-shim Lua template from disk."""
    global _DEAL_SHIM_TEMPLATE
    if _DEAL_SHIM_TEMPLATE is None:
        _DEAL_SHIM_TEMPLATE = _DEAL_SHIM_PATH.read_text(encoding="utf-8")
    return _DEAL_SHIM_TEMPLATE


def build_deal_shim_install_lua(managed_ids: tuple[int, ...]) -> str:
    """Lua that wraps SendWorkingDeal, IsAutoPropose and UpdateDealStatus in
    the DiplomacyDealView state.

    Idempotent re-arm: the original of each wrapped function is captured once
    (when ``__MCP_orig_*`` is nil) and restored before every subsequent
    re-wrap, so repeated installs never stack wrappers.  Each wrapper calls
    its original through a *local upvalue* (``origSWD``/``origUDS``) rather
    than the ``__MCP_orig_*`` global — this is what makes re-install safe: a
    second install cannot make a wrapper call itself.  An earlier version
    read the global from inside the wrapper with the idempotency guards
    removed; a second install pointed that global at wrapper1, so wrapper1
    called itself -> infinite recursion -> Lua stack overflow -> hard crash
    on deal-screen open.

    - ``SendWorkingDeal`` wrapper: for a managed target, suppress ``INSPECT``
      (action 7), and serialise ``PROPOSED`` (action 4) to ``MCPDEAL|...``
      lines (or emit ``HUMAN_ACCEPTED`` when ``__MCP_deal_proposal_id`` is
      set); other actions forward to the original.
    - ``IsAutoPropose``: always returns false.
    - ``UpdateDealStatus`` wrapper: force Accept/Refuse visible for managed
      targets after the game hides them.
    """
    managed_entries = ",".join(f"[{p}]=true" for p in managed_ids)
    managed_table = "{" + managed_entries + "}"

    lua = _load_deal_shim_template()
    lua = lua.replace("__MCP_MANAGED_IDS_TAG__", managed_table)
    lua = lua.replace("__MCP_SENTINEL_TAG__", SENTINEL)
    return lua


def build_deal_shim_uninstall_lua() -> str:
    """Restore the original SendWorkingDeal and IsAutoPropose."""
    return (
        "if __MCP_orig_SWD ~= nil then "
        "  DealManager.SendWorkingDeal = __MCP_orig_SWD "
        "  __MCP_orig_SWD = nil "
        "end "
        "if __MCP_orig_IAP ~= nil then "
        "  IsAutoPropose = __MCP_orig_IAP "
        "  __MCP_orig_IAP = nil "
        "end "
        "if __MCP_orig_UDS ~= nil then "
        "  UpdateDealStatus = __MCP_orig_UDS "
        "  __MCP_orig_UDS = nil "
        "end "
        "__MCP_managed_ids = nil "
        "__MCP_managed_deal = nil "
        'print("DEALSHIM|removed") '
        f'print("{SENTINEL}")'
    )


def build_deal_shim_health_check_lua() -> str:
    """Verify the wrapper is in place (returns non-zero if missing)."""
    return (
        'print("DEALSHIM_HEALTH|" .. tostring(__MCP_orig_SWD ~= nil '
        "  and DealManager.SendWorkingDeal ~= __MCP_orig_SWD "
        "  and __MCP_orig_IAP ~= nil)) "
        f'print("{SENTINEL}")'
    )


async def install_deal_shim(
    conn: GameConnection, managed_ids: tuple[int, ...]
) -> str:
    """Install the deal interception shim in the DiplomacyDealView state."""
    index = await conn.state_index_for(DEAL_SHIM_STATE)
    if index is None:
        log.warning("Deal shim: %s state not found", DEAL_SHIM_STATE)
        return "absent"
    lines = await conn.execute_in_named_state(
        DEAL_SHIM_STATE, build_deal_shim_install_lua(managed_ids)
    )
    status = next(
        (l[9:] for l in lines if l.startswith("DEALSHIM|")), "unknown"
    )
    log.info("Deal shim: %s", status)
    return status


# ---------------------------------------------------------------------------
# Chat shim — wraps Network.SendChat in the ChatPanel state
# ---------------------------------------------------------------------------

CHAT_SHIM_STATE = "ChatPanel"
WORLDTRACKER_STATE = "WorldTracker"

# Template Lua for the chat shim, kept in a separate file for readability.
# One tag is substituted per-call by build_chat_shim_install_lua():
#   __MCP_SENTINEL_TAG__  -> the response sentinel (_helpers.SENTINEL)
_CHAT_SHIM_PATH = Path(__file__).resolve().parent.parent / "lua" / "chat_shim.lua"
_CHAT_SHIM_TEMPLATE: str | None = None


def _load_chat_shim_template() -> str:
    """Read and cache the chat-shim Lua template from disk."""
    global _CHAT_SHIM_TEMPLATE
    if _CHAT_SHIM_TEMPLATE is None:
        _CHAT_SHIM_TEMPLATE = _CHAT_SHIM_PATH.read_text(encoding="utf-8")
    return _CHAT_SHIM_TEMPLATE


def build_chat_shim_install_lua() -> str:
    """Lua that wraps ParseInputChatString in the ChatPanel state.

    Idempotent re-arm (same pattern as the deal shim,
    build_deal_shim_install_lua): the original is captured once in
    ``__MCP_orig_ParseInputChatString`` and called through a local upvalue, so
    repeated installs never stack wrappers.  The wrapper hex-encodes the
    human's parsed text and prints an ``MCPCHAT|SEND|...`` line drained by the
    deal monitor, then echoes the human's own message into the chat log
    (``Network.SendChat`` does not echo locally in single-player).  It delegates
    to the original so ``SendChat`` continues normally (clear box, sent sound).

    Why ParseInputChatString and not Network.SendChat: ``Network`` is a
    read-only table in the ChatPanel state (assignment raises "Attempt to
    modify read-only table"), but ``ParseInputChatString`` is a plain global
    (declared in ChatLogic.lua, include()'d into the ChatPanel state) and thus
    writable — the same kind of hook the deal shim uses for IsAutoPropose /
    UpdateDealStatus.  It is called once per message inside SendChat
    (ChatPanel.lua:301), on Enter-key commit.
    """
    lua = _load_chat_shim_template()
    lua = lua.replace("__MCP_SENTINEL_TAG__", SENTINEL)
    return lua


def build_chat_shim_uninstall_lua() -> str:
    """Restore the original ParseInputChatString in the ChatPanel state."""
    return (
        "if __MCP_orig_ParseInputChatString ~= nil then "
        "  ParseInputChatString = __MCP_orig_ParseInputChatString "
        "  __MCP_orig_ParseInputChatString = nil "
        "end "
        'print("CHATSHIM|removed") '
        f'print("{SENTINEL}")'
    )


async def install_chat_shim(conn: GameConnection) -> str:
    """Install the chat shim in the ChatPanel state. Returns status string."""
    index = await conn.state_index_for(CHAT_SHIM_STATE)
    if index is None:
        log.warning("Chat shim: %s state not found", CHAT_SHIM_STATE)
        return "absent"
    lines = await conn.execute_in_named_state(
        CHAT_SHIM_STATE, build_chat_shim_install_lua()
    )
    status = next(
        (l[9:] for l in lines if l.startswith("CHATSHIM|")), "unknown"
    )
    log.info("Chat shim: %s", status)
    return status


def build_unhide_chat_lua() -> str:
    """Lua (WorldTracker state) that force-shows the chat panel in single-player.

    Reverses the WorldTracker.LateInitialize gate (which hides the chat panel and
    its toggle checkbox when not in network multiplayer) by calling the global
    ``UpdateChatPanel(false)`` and unhiding ``ChatCheck``.  Idempotent: safe to
    call repeatedly.  ``UpdateChatPanel`` sets the internal ``m_hideChat`` flag,
    the container hide, and the checkbox check, so the WorldTracker's own
    resize/show-hide logic stays consistent.
    """
    return (
        "UpdateChatPanel(false) "
        "Controls.ChatCheck:SetHide(false) "
        'print("CHATUNHID") '
        f'print("{SENTINEL}")'
    )


def build_send_chat_message_lua(
    from_pid: int, to_pid: int, text: str
) -> str:
    """Lua (ChatPanel state) that renders one message into the human's chat log.

    Calls the global ``OnChat`` so the message uses the native render path:
    sender name from ``PlayerConfigurations[from_pid]``, whisper color, and a
    diplomacy-ribbon portrait flash via ``LuaEvents.ChatPanel_OnChatReceived``.
    ``OnChat`` and ``ChatTargetTypes`` are globals in the ChatPanel state.
    """
    safe_text = lua_quote(text)
    return (
        f"local fromP = {from_pid} "
        f"local toP = {to_pid} "
        f"local text = {safe_text} "
        "OnChat(fromP, toP, text, ChatTargetTypes.CHATTARGET_PLAYER, true) "
        'print("CHATSENT") '
        f'print("{SENTINEL}")'
    )


# ---------------------------------------------------------------------------
# Deal injection and native-screen presentation
# ---------------------------------------------------------------------------


def build_inject_deal_lua(
    human_pid: int, agent_pid: int, proposal
) -> str:
    """Lua (InGame) that injects a mailbox proposal into the OUTGOING working deal.

    The native trade screen always reads from OUTGOING, so this is how an
    incoming proposal from an agent becomes visible to the human.
    ``proposal`` is a :class:`~civ_mcp.deal_mailbox.PendingProposal`.
    """
    # Build item-adding snippets
    items_lua: list[str] = []
    for item in proposal.items_from_proposer:
        items_lua.append(_lua_add_deal_item("fromP", item))
    for item in proposal.items_from_target:
        items_lua.append(_lua_add_deal_item("toP", item))

    return (
        f"local me = {human_pid} "
        f"local other = {agent_pid} "
        # Use the agent as the item source for their offers,
        # the human as the source for what the agent requests.
        f"local fromP = {agent_pid} "
        f"local toP = {human_pid} "
        "DealManager.ClearWorkingDeal(0, me, other) "
        "local deal = DealManager.GetWorkingDeal(0, me, other) "
        "if deal then "
        + " ".join(items_lua)
        + ' print("INJECTED|" .. tostring(deal:GetItemCount()) .. " items") '
        "else "
        ' print("INJECT_FAILED|no deal object") '
        "end "
        f'print("{SENTINEL}")'
    )


def build_present_deal_lua(human_pid: int, agent_pid: int) -> str:
    """Lua (InGame) that opens a MAKE_DEAL session for an injected deal."""
    return (
        f"DiplomacyManager.RequestSession({human_pid}, {agent_pid}, "
        '"MAKE_DEAL") '
        "local sid = DiplomacyManager.FindOpenSessionID("
        f"{human_pid}, {agent_pid}) "
        'print("SESSION_OPENED|" .. tostring(sid)) '
        f'print("{SENTINEL}")'
    )


def build_force_deal_buttons_lua(
    agent_pid: int, proposal_id: str
) -> str:
    """Lua (DiplomacyDealView) that forces Accept/Refuse buttons and sets
    the leader dialog to show the proposal is from a managed civ."""
    return (
        # Set the managed-deal flag so IsAutoPropose returns false.
        f"__MCP_managed_deal = true "
        # Show Accept/Refuse, hide AI evaluation.
        "Controls.AcceptDeal:SetHide(false) "
        "Controls.AcceptDeal:LocalizeAndSetText("
        '"LOC_DIPLOMACY_DEAL_ACCEPT_DEAL") '
        "Controls.RefuseDeal:SetHide(false) "
        "Controls.RefuseDeal:LocalizeAndSetText("
        '"LOC_DIPLOMACY_DEAL_REFUSE_DEAL") '
        "Controls.EqualizeDeal:SetHide(true) "
        # Leader dialog: name the proposer.
        f"local cfg = PlayerConfigurations[{agent_pid}] "
        "local name = cfg and "
        "  Locale.Lookup(cfg:GetLeaderName()) or 'Unknown' "
        "Controls.LeaderDialog:SetText("
        "  name .. ' proposes the following deal:') "
        # Store the proposal id so the SendWorkingDeal wrapper can include it.
        f'__MCP_deal_proposal_id = "{proposal_id}" '
        'print("DEAL_BUTTONS_READY") '
        f'print("{SENTINEL}")'
    )


def build_clear_deal_flag_lua() -> str:
    """Lua (DiplomacyDealView) that clears the managed-deal flag."""
    return (
        "__MCP_managed_deal = false "
        "__MCP_deal_proposal_id = nil "
        'print("DEAL_FLAG_CLEARED") '
        f'print("{SENTINEL}")'
    )


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------


def build_notification_handler_lua(managed_ids: tuple[int, ...]) -> str:
    """Lua (InGame) that registers a click handler for deal notifications.

    On ``Events.NotificationActivated``, checks if the notification is one of
    ours (message starts with ``MCPDEAL:``), extracts the proposal id, and
    prints ``MCPDEAL_CLICK|<proposal_id>|<player_id>``.
    """
    managed = ",".join(str(p) for p in managed_ids)
    return (
        "if __MCP_note_handler == nil then "
        "  __MCP_note_handler = function(pid, nid, byUser) "
        "    if not byUser then return end "
        "    if pid ~= Game.GetLocalPlayer() then return end "
        "    local n = NotificationManager.Find(pid, nid) "
        "    if not n then return end "
        "    local msg = n:GetMessage() "
        '    if not msg or not msg:find("MCPDEAL:") then return end '
        '    local proposalId = msg:match("MCPDEAL:(.*)") '
        "    NotificationManager.Dismiss(pid, nid) "
        '    print("MCPDEAL_CLICK|" .. tostring(proposalId) '
        '      .. "|pid=" .. tostring(pid)) '
        "  end "
        "  Events.NotificationActivated.Add(__MCP_note_handler) "
        '  print("NOTE_HANDLER|installed") '
        "else "
        '  print("NOTE_HANDLER|present") '
        "end "
        f'print("{SENTINEL}")'
    )


def build_send_deal_notification_lua(
    human_pid: int, agent_pid: int, proposal_id: str, summary: str
) -> str:
    """Lua (InGame) that sends a clickable deal notification to the human.

    The proposal id is embedded in the message string (hidden from the player)
    and extracted by the click handler.  The summary is what the player sees.
    """
    # Escape single quotes in summary for Lua string.
    safe_summary = summary.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"local pid = {human_pid} "
        "local nType = DB.MakeHash('NOTIFICATION_USER_DEFINED_1') "
        "NotificationManager.SendNotification(pid, nType, "
        f"'MCPDEAL:{proposal_id}', "
        f"'{safe_summary}', -1, -1) "
        f'print("NOTIFY_SENT|{proposal_id}") '
        f'print("{SENTINEL}")'
    )


# ---------------------------------------------------------------------------
# Execution — forced-deal primitive
# ---------------------------------------------------------------------------


def build_execute_deal_lua(
    proposal, accepter_pid: int
) -> str:
    """Lua (InGame) that executes a mailbox proposal via the forced-deal
    primitive (``SendWorkingDeal(ACCEPTED, …)`` with no ``PROPOSED``).

    The accepter is on the clock by definition, so the local-player slot is
    already correct.  Follows with the ``_lua_close_diplo_session()`` teardown
    because a forced deal leaves an open diplomacy session behind.
    """
    from civ_mcp.lua._helpers import _lua_close_diplo_session

    proposer_pid = proposal.from_player
    # Build item-adding snippets
    items_lua: list[str] = []
    for item in proposal.items_from_proposer:
        items_lua.append(_lua_add_deal_item(str(item.from_player_id), item))
    for item in proposal.items_from_target:
        items_lua.append(_lua_add_deal_item(str(item.from_player_id), item))

    return (
        f"local me = {accepter_pid} "
        f"local other = {proposer_pid} "
        "DealManager.ClearWorkingDeal(0, me, other) "
        "local deal = DealManager.GetWorkingDeal(0, me, other) "
        "if not deal then "
        ' print("EXEC_FAILED|no deal object") '
        f' print("{SENTINEL}") '
        " return "
        "end "
        + " ".join(items_lua)
        + " "
        "DealManager.SendWorkingDeal(1, me, other) "
        f"{_lua_close_diplo_session()} "
        'print("DEAL_EXECUTED|" .. tostring(deal:GetItemCount()) .. " items") '
        f'print("{SENTINEL}")'
    )


# ---------------------------------------------------------------------------
# Item serialisation helpers
# ---------------------------------------------------------------------------

# Map item type names to DealItemTypes enum members for Lua.
_DEAL_ITEM_TYPE_LUA: dict[str, str] = {
    "GOLD": "DealItemTypes.GOLD",
    "RESOURCE": "DealItemTypes.RESOURCES",
    "AGREEMENT": "DealItemTypes.AGREEMENTS",
    "FAVOR": "DealItemTypes.FAVOR",
    "CITY": "DealItemTypes.CITIES",
    "GREAT_WORK": "DealItemTypes.GREATWORK",
}


def _lua_add_deal_item(from_var: str, item) -> str:
    """One Lua snippet adding a :class:`SerializedDealItem` to ``deal``.

    ``from_var`` is the Lua variable/expression for the player id that
    *provides* the item (e.g. ``"me"`` or ``"other"``).

    Agreement ``subtype`` has two encodings (see :class:`SerializedDealItem`):
    agent-constructed items carry the enum *name* (rendered as
    ``DealAgreementTypes.<name>``); human-constructed items carry the enum's
    integer value (rendered bare, since the enum members are ints).  Alliance
    items additionally need ``SetValueType`` with the ``GameInfo.Alliances``
    index — agent items carry the type *name* and resolve it in Lua; human
    items carry the int ``value_type`` straight from the engine.
    """
    t = item.item_type.upper()
    dt = _DEAL_ITEM_TYPE_LUA.get(t, "DealItemTypes.GOLD")
    amount = item.amount
    duration = item.duration

    if t == "GOLD":
        return (
            f"do local gi = deal:AddItemOfType({dt}, {from_var}) "
            f"if gi then gi:SetAmount({amount}) "
            f"gi:SetDuration({duration}) end end "
        )
    elif t == "RESOURCE":
        # Agent-constructed items carry the resource type name (resolve via
        # GameInfo here); human-constructed items carry the int value_type
        # from the engine.
        if item.value_type is not None and item.value_type >= 0:
            vt = f"{item.value_type}"
        elif item.name:
            vt = f'(GameInfo.Resources["{item.name}"] and GameInfo.Resources["{item.name}"].Index or -1)'
        else:
            vt = "-1"
        return (
            f"do local ri = deal:AddItemOfType({dt}, {from_var}) "
            f"if ri then ri:SetValueType({vt}) "
            f"ri:SetAmount({amount}) "
            f"ri:SetDuration({duration}) end end "
        )
    elif t == "FAVOR":
        return (
            f"do local fi = deal:AddItemOfType({dt}, {from_var}) "
            f"if fi then fi:SetAmount({amount}) end end "
        )
    elif t == "AGREEMENT":
        sub = item.subtype
        if isinstance(sub, str) and sub:
            sub_expr = f"DealAgreementTypes.{sub}"
        else:
            sub_expr = str(sub)  # int — enum members are ints, so this works
        parts = [f"ai:SetSubType({sub_expr})"]
        # Alliance items additionally need SetValueType with the
        # GameInfo.Alliances index.  Detect in Lua so it works for both
        # encodings (agent name → DealAgreementTypes.ALLIANCE; human int →
        # the enum's integer value).
        at = getattr(item, "alliance_type", "") or ""
        has_vt = item.value_type is not None and item.value_type >= 0
        if at:
            # Agent item: resolve the index by alliance-type name.
            vt_lua = (
                f'pcall(function() local r=GameInfo.Alliances["ALLIANCE_{at.upper()}"] '
                f"if r then ai:SetValueType(r.Index) end end)"
            )
        elif has_vt:
            # Human item: int alliance-type index straight from the engine.
            vt_lua = f"ai:SetValueType({item.value_type})"
        else:
            vt_lua = ""
        if vt_lua:
            parts.append(
                f"if {sub_expr} == DealAgreementTypes.ALLIANCE then {vt_lua} end"
            )
        return (
            f"do local ai = deal:AddItemOfType({dt}, {from_var}) "
            f"if ai then {' '.join(parts)} end end "
        )
    elif t == "CITY":
        return (
            f"do local ci = deal:AddItemOfType({dt}, {from_var}) "
            f"if ci then ci:SetValueType({item.value_type}) end end "
        )
    elif t == "GREAT_WORK":
        return (
            f"do local gw = deal:AddItemOfType({dt}, {from_var}) "
            f"if gw then gw:SetValueType({item.value_type}) end end "
        )
    else:
        return f"-- unsupported deal item type: {t} "
