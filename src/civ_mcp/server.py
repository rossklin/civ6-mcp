"""MCP server for Civilization VI — lets LLM agents read game state and play.

Uses FastMCP with the lifespan pattern to maintain a persistent TCP connection
to the running game via FireTuner protocol.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

from civ_mcp import game_launcher, handoff, heartbeat, seats as seats_mod
from civ_mcp.game_over_watchdog import GameOverWatchdog
from civ_mcp import narrate as nr
from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.diary import (
    diary_path as _diary_path,
    get_current_plans as _get_current_plans,
)
from civ_mcp.game_state import GameState
from civ_mcp.handoff import HandoffConfig, HandoffKeeper
from civ_mcp.logger import GameLogger
from civ_mcp.map_capture import MapCapture
from civ_mcp.command_executor import execute_commands as _execute_commands
from civ_mcp.narrate_unified import narrate_full_state
from civ_mcp.seats import Seat, SeatRegistry
from civ_mcp.spatial import SpatialTracker
from civ_mcp.spectator import CameraController, PopupWatcher
from civ_mcp.telemetry import (
    EVENT_DIARY_ROW,
    AlertSink,
    CloudSink,
    LocalSink,
    TelemetryEmitter,
)
from civ_mcp.web_api import create_app

log = logging.getLogger(__name__)

# Read once at import: the seat layout is fixed for the life of the process,
# and tool registration depends on whether handoff mode is on.
HANDOFF_CONFIG = HandoffConfig.from_env()


@dataclass
class AppContext:
    game: GameState
    logger: GameLogger
    camera: CameraController
    popup_watcher: PopupWatcher
    spatial: SpatialTracker
    map_capture: MapCapture
    watchdog: GameOverWatchdog
    # Human-vs-agent handoff. In classic single-agent mode `seats` is an empty
    # registry whose default seat wraps the fields above, so every accessor
    # resolves exactly as before.
    seats: SeatRegistry
    handoff_config: HandoffConfig
    keeper: HandoffKeeper | None = None


async def _auto_boot(conn: GameConnection, save_name: str) -> None:
    """Launch game and load a save before MCP tools become available.

    Called during lifespan when CIV_MCP_SAVE_FILE is set (eval mode).
    Blocks until the game is loaded and ready for play.
    """
    import glob

    from civ_mcp.game_lifecycle import load_game_save

    # 0. Clear stale MCP autosaves. These are the saves that the main
    # menu's "Continue Game" button would load. If a previous run
    # crashed at T197, "Continue Game" resumes T197 instead of loading
    # the scenario save. Clearing them makes "Continue Game" harmless
    # (it would load the scenario save or nothing).
    # This does NOT break --resume-save which loads by name via Lua.
    stale = glob.glob(os.path.join(game_launcher.SINGLE_SAVE_DIR, "0_MCP_*.Civ6Save"))
    if stale:
        for f in stale:
            try:
                os.remove(f)
            except OSError:
                pass
        log.info("Auto-boot: cleared %d stale MCP autosave(s)", len(stale))

    # 1. Launch game (or reuse if already running).
    # The eval runner's ensure_game_ready() typically launches the game
    # before the MCP server starts. _launch_game_sync() detects an
    # already-running game and returns immediately, avoiding a wasteful
    # kill + relaunch cycle through the Aspyr launcher.
    # The step-5 verification below catches wrong-save scenarios as a
    # safety net (the Lua load path fails when mid-session, not from
    # main menu).
    heartbeat.write("launching")
    log.info("Auto-boot: launching game...")
    result = await asyncio.to_thread(game_launcher._launch_game_sync)
    log.info("Auto-boot: launch result: %s", result)

    # 2. Connect to FireTuner (retry — game takes time to start)
    for attempt in range(90):
        try:
            await conn.connect()
            log.info("Auto-boot: connected to FireTuner")
            heartbeat.write("connecting")
            break
        except ConnectionError:
            if attempt % 10 == 0:
                log.info("Auto-boot: waiting for FireTuner... (%ds)", attempt)
            await asyncio.sleep(1)
    else:
        log.error("Auto-boot: could not connect to FireTuner after 90s")
        heartbeat.write("error")
        return

    # 2b. Verify Lua states exist (port can open before game initialises).
    # A hung splash screen ("Loading, Please Wait...") has port open but
    # GameCore never appears. Skip this check — the main menu legitimately
    # has no GameCore on any platform; it only appears after a save is
    # loaded (step 3). The splash hang detection was causing false kills
    # when autosaves were cleaned (game stays at main menu, no GameCore).
    if conn.gamecore_index is None and False:  # disabled — see comment above
        log.warning(
            "Auto-boot: FireTuner connected but GameCore not found "
            "— game may be hung at splash screen"
        )
        for retry in range(30):
            await asyncio.sleep(2)
            try:
                await conn.reconnect()
                if conn.gamecore_index is not None:
                    log.info("Auto-boot: GameCore found after %ds", (retry + 1) * 2)
                    break
            except ConnectionError:
                pass
        else:
            log.error("Auto-boot: GameCore never appeared — killing hung game")
            heartbeat.write("error")
            await asyncio.to_thread(game_launcher._kill_game_sync)
            await asyncio.sleep(5)
            result = await asyncio.to_thread(game_launcher._launch_game_sync)
            log.info("Auto-boot: relaunched after hung splash: %s", result)
            for attempt in range(90):
                try:
                    await conn.connect()
                    if conn.gamecore_index is not None:
                        log.info("Auto-boot: GameCore found on relaunch")
                        break
                except ConnectionError:
                    pass
                await asyncio.sleep(1)
            if conn.gamecore_index is None:
                log.error("Auto-boot: relaunch also failed — giving up")
                heartbeat.write("error")
                return

    # 3. Load save (Lua on Windows/macOS, OCR menu nav on Linux)
    log.info("Auto-boot: loading save '%s'...", save_name)
    result = await load_game_save(conn, save_name)
    log.info("Auto-boot: load result: %s", result)
    heartbeat.write("loading")

    # 4. Wait for save to load, click through leader intro, then reconnect.
    # The CONTINUE GAME button on the leader screen has low-contrast
    # teal-on-teal text that OCR often misses — fall back to positional
    # click grid if OCR fails. Verify the click actually worked by
    # checking for Lua states (only available once in-game, not on leader
    # screen).
    log.info("Auto-boot: waiting 15s for save to load...")
    await asyncio.sleep(15)
    clicked = await asyncio.to_thread(
        lambda: game_launcher._click_text("CONTINUE", timeout=105, post_delay=1),
    )
    if clicked:
        log.info("Auto-boot: clicked CONTINUE GAME via OCR")
    else:
        log.warning("Auto-boot: OCR missed CONTINUE — using positional click grid")
        await asyncio.to_thread(game_launcher._click_continue_positional)

    # Verify the click worked — Lua states only appear once past the
    # leader screen into gameplay. Retry positional click if needed.
    await asyncio.sleep(3)
    game_ready = False
    for attempt in range(45):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                log.info("Auto-boot: game ready (GameCore=%s)", conn.gamecore_index)
                heartbeat.write("playing")  # turn unknown until first end_turn
                game_ready = True
                break
        except ConnectionError:
            pass
        # Retry positional click every 10s in case the first click missed
        if attempt > 0 and attempt % 10 == 0:
            heartbeat.write("loading")  # keep heartbeat fresh during retry
            if not clicked:
                log.info("Auto-boot: retrying positional click (attempt %d)", attempt)
                await asyncio.to_thread(game_launcher._click_continue_positional)
        await asyncio.sleep(1)
    if not game_ready:
        log.warning("Auto-boot: save may not have loaded — GameCore not found")
        heartbeat.write("error")
        return

    # 5. Verify correct save loaded. If the wrong save loaded (e.g.
    # main-menu "Continue Game" loaded a stale autosave instead of the
    # scenario save), reload the correct one via Lua — no OCR needed.
    try:
        verify = await conn.execute_read(
            "local t = Game.GetCurrentGameTurn(); "
            'print("VERIFY|" .. t); '
            'print("---END---")'
        )
        for line in verify:
            if line.startswith("VERIFY|"):
                turn = int(line.split("|")[1])
                if turn > 5:
                    log.error(
                        "Auto-boot: loaded T%d but expected T1 — wrong save! "
                        "Reloading '%s' via Lua",
                        turn,
                        save_name,
                    )
                    # Retry via Lua (Network.LoadGame) — bypasses OCR entirely
                    result = await load_game_save(conn, save_name)
                    log.info("Auto-boot: Lua reload result: %s", result)
                    await asyncio.sleep(15)
                    # Click CONTINUE again for the leader screen
                    await asyncio.to_thread(game_launcher._click_continue_positional)
                    await asyncio.sleep(5)
                    for retry in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)
                    # Verify again
                    try:
                        verify2 = await conn.execute_read(
                            "local t = Game.GetCurrentGameTurn(); "
                            'print("VERIFY|" .. t); '
                            'print("---END---")'
                        )
                        for line2 in verify2:
                            if line2.startswith("VERIFY|"):
                                t2 = int(line2.split("|")[1])
                                if t2 > 5:
                                    log.error(
                                        "Auto-boot: Lua reload also loaded T%d "
                                        "— falling back to kill + OCR",
                                        t2,
                                    )
                                    await game_launcher.kill_game()
                                    r = await asyncio.to_thread(
                                        game_launcher._launch_game_sync
                                    )
                                    log.info("Auto-boot: relaunch: %s", r)
                                    r = await asyncio.to_thread(
                                        game_launcher._navigate_to_save_sync,
                                        save_name,
                                        None,
                                    )
                                    log.info("Auto-boot: OCR nav: %s", r)
                                    for a in range(30):
                                        try:
                                            await conn.reconnect()
                                            if conn.gamecore_index is not None:
                                                return
                                        except ConnectionError:
                                            pass
                                        await asyncio.sleep(1)
                                    log.warning("Auto-boot: all fallbacks failed")
                                    return
                                log.info("Auto-boot: Lua reload verified at T%d", t2)
                                heartbeat.write("playing", turn=t2)
                    except Exception:
                        log.debug("Auto-boot: post-reload verify failed", exc_info=True)
                    return
                log.info("Auto-boot: verified save at T%d", turn)
                heartbeat.write("playing", turn=turn)
    except Exception:
        log.debug("Auto-boot: save verification failed", exc_info=True)


def _build_emitter(run_id: str | None = None) -> TelemetryEmitter:
    """Emitter wired to the configured sinks.

    Each agent seat gets its own emitter (and therefore its own run id and its
    own set of JSONL files), so two agents in the same game do not interleave
    diary rows or fight over the sink's game binding.
    """
    emitter = TelemetryEmitter()
    emitter.add_sink(LocalSink())
    cloud_bucket = os.environ.get("CIV_MCP_TELEMETRY_BUCKET")
    if cloud_bucket:
        emitter.add_sink(CloudSink(cloud_bucket))
    alert_webhook = os.environ.get("CIV_MCP_ALERT_WEBHOOK")
    if alert_webhook:
        emitter.add_sink(AlertSink(alert_webhook))
    emitter.start(run_id=run_id)
    return emitter


async def _serve_web_api(uvi_server: uvicorn.Server, port: int) -> None:
    """Run the web dashboard, treating a failure to start as non-fatal.

    The dashboard is a convenience, not part of playing the game. uvicorn calls
    ``sys.exit(1)`` when it cannot bind, and ``SystemExit`` is a BaseException —
    left uncaught in a background task it tears down the whole ASGI app, taking
    the MCP server with it. A port clash (another civ-mcp process, or anything
    else on 8000) must not do that.
    """
    try:
        await uvi_server.serve()
    except SystemExit:
        log.warning(
            "Web dashboard could not bind port %d — something else is using it. "
            "Continuing without the dashboard; set CIV_MCP_WEB_PORT to change it.",
            port,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("Web dashboard stopped unexpectedly", exc_info=True)


@asynccontextmanager
async def _open_app_context() -> AsyncIterator[AppContext]:
    """Build the process-wide server context: connection, seats, background services.

    Entered once per process — see :func:`lifespan`.
    """
    conn = GameConnection()

    # Telemetry emitter — routes events to local JSONL + optional cloud sink
    emitter = _build_emitter()
    heartbeat.init(emitter.run_id)
    # Bind eval identity so the orchestrator can match running games to jobs
    eval_model = os.environ.get("CIV_MCP_AGENT_MODEL", "")
    eval_metadata = os.environ.get("CIV_MCP_METADATA", "")
    eval_scenario = ""
    if eval_metadata:
        try:
            eval_scenario = json.loads(eval_metadata).get("scenario_id", "")
        except Exception:
            pass
    heartbeat.bind_eval(eval_model, eval_scenario)
    heartbeat.write("starting")

    logger = GameLogger(emitter)
    spatial = SpatialTracker(emitter)
    map_capture = MapCapture(emitter)
    gs = GameState(conn)
    gs.spatial = spatial
    log.info("Game logger session: %s", logger.session_id)

    # Human-vs-agent handoff: one server, one connection, several agent seats
    # alongside a human playing in the game UI.
    cfg = HANDOFF_CONFIG
    seat_emitters: list[TelemetryEmitter] = []

    def _seat_factory(player_id: int) -> Seat:
        seat_emitter = _build_emitter(f"{emitter.run_id}_p{player_id}")
        seat_emitters.append(seat_emitter)
        return seats_mod.build_seat(player_id, conn, seat_emitter)

    registry = SeatRegistry(
        default=Seat(
            player_id=cfg.human_id,
            game=gs,
            logger=logger,
            spatial=spatial,
            map_capture=map_capture,
            label="default",
        ),
        agent_ids=cfg.agent_ids,
        human_id=cfg.human_id,
        factory=_seat_factory if cfg.agent_ids else None,
    )
    if cfg.enabled:
        log.info(
            "Handoff mode: human=P%d, agent seats=%s",
            cfg.human_id,
            ", ".join(f"P{p}" for p in cfg.agent_ids),
        )

    # Auto-boot: launch game + load save when running as eval
    save_file = os.environ.get("CIV_MCP_SAVE_FILE")
    if save_file:
        await _auto_boot(conn, save_file)

    # Spectator-mode background services (camera tracking + popup auto-dismiss).
    # In handoff mode the human owns the camera — hopping it to whatever an
    # agent is looking at would make their game unplayable.
    camera = CameraController(conn)
    popup_watcher = PopupWatcher(conn)
    watchdog = GameOverWatchdog(gs, logger)
    if not cfg.enabled:
        camera.start()
    popup_watcher.start()
    watchdog.start()

    keeper: HandoffKeeper | None = None
    if cfg.enabled:
        keeper = HandoffKeeper(conn, cfg)
        try:
            own = await keeper.ensure_installed(force=True)
            log.info(
                "Handoff armed (hook=%s, local=P%s, turn=%s)",
                own.handler_installed,
                own.local_player,
                own.turn,
            )
        except Exception:
            log.warning(
                "Could not arm the handoff hook yet — the keeper will retry "
                "once the game is reachable",
                exc_info=True,
            )
        keeper.start()

    # Start the web dashboard API as a background task.
    web_port = int(os.environ.get("CIV_MCP_WEB_PORT", "8000"))
    web_app = create_app(gs)
    uvi_config = uvicorn.Config(
        web_app, host="0.0.0.0", port=web_port, log_level="info"
    )
    uvi_server = uvicorn.Server(uvi_config)
    api_task = asyncio.create_task(_serve_web_api(uvi_server, web_port))
    log.info("Web API starting on http://0.0.0.0:%d", web_port)

    try:
        yield AppContext(
            game=gs,
            logger=logger,
            camera=camera,
            popup_watcher=popup_watcher,
            spatial=spatial,
            map_capture=map_capture,
            watchdog=watchdog,
            seats=registry,
            handoff_config=cfg,
            keeper=keeper,
        )
    finally:
        await emitter.close()
        for seat_emitter in seat_emitters:
            await seat_emitter.close()
        if keeper is not None:
            await keeper.stop()
            # Leaving the hook armed with nobody driving the agent civs would
            # halt the game on their turn with no way to continue. Disarm and
            # give the slot back so the human's game keeps working.
            try:
                await handoff.hand_back(conn, cfg)
            except Exception:
                log.debug("Handoff teardown failed", exc_info=True)
        await watchdog.stop()
        await camera.stop()
        await popup_watcher.stop()
        uvi_server.should_exit = True
        await api_task
        await conn.disconnect()


# The MCP SDK enters the server lifespan once per *client session*, which for
# streamable-http means once per connected agent. A shared game needs exactly
# one connection, one seat registry and one web dashboard, so the context is
# built on the first session and reused, then torn down when the last one goes.
_shared_ctx: AppContext | None = None
_shared_stack: Any = None
_shared_refs: int = 0
_shared_lock: asyncio.Lock | None = None


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Hand every client session the same process-wide context."""
    global _shared_ctx, _shared_stack, _shared_refs, _shared_lock
    from contextlib import AsyncExitStack

    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    async with _shared_lock:
        if _shared_ctx is None:
            _shared_stack = AsyncExitStack()
            _shared_ctx = await _shared_stack.enter_async_context(_open_app_context())
        _shared_refs += 1
        ctx = _shared_ctx
        log.info("MCP session opened (%d active)", _shared_refs)
    try:
        yield ctx
    finally:
        async with _shared_lock:
            _shared_refs -= 1
            log.info("MCP session closed (%d active)", _shared_refs)
            if _shared_refs <= 0:
                stack, _shared_stack = _shared_stack, None
                _shared_ctx, _shared_refs = None, 0
                if stack is not None:
                    await stack.aclose()


_INSTRUCTIONS = (
    "Read game state and issue commands to a running Civ 6 game. "
    "Call get_full_game_state first to orient yourself."
)

if HANDOFF_CONFIG.enabled:
    _INSTRUCTIONS = (
        "Read game state and issue commands to a running Civ 6 game. This game "
        "is shared: a human plays one civ in the game's own UI and one or more "
        "agents play rival civs through this server, taking turns in order.\n"
        "Start by calling get_seats() and then claim_seat(player_id=N) — every "
        "other tool is refused until you hold a seat. Then call "
        "get_full_game_state() to orient yourself.\n"
        "Read tools always answer for your own civ, including while other "
        "players are taking their turns, so you can scout and plan off the "
        "clock. Write tools work only during your own turn.\n"
        "Turn flow: get_full_game_state → execute_commands → end_turn → "
        "update_diary(next_turn_plan=..., long_term_plans=...) → "
        "wait_for_turn(). wait_for_turn() blocks until your next turn starts; "
        "call it again on timeout without changing the diary."
    )

mcp = FastMCP(
    "Civilization VI",
    instructions=_INSTRUCTIONS,
    lifespan=lifespan,
)


def _app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _get_seat(ctx: Context) -> Seat:
    """Seat this MCP session drives, defaulting to the single-agent seat.

    Also publishes the seat's player id as the read perspective for the rest of
    this tool call, so queries answer for the caller's own civ even while
    another player holds the local-player slot.  Every state accessor funnels
    through here, so no tool can accidentally read as the wrong player.
    """
    app = _app(ctx)
    seat = app.seats.resolve(seats_mod.session_key(ctx))
    if app.seats.enabled and seat is not app.seats.default:
        seats_mod.set_view_player(seat.player_id)
    return seat


def _get_game(ctx: Context) -> GameState:
    return _get_seat(ctx).game


def _get_logger(ctx: Context) -> GameLogger:
    return _get_seat(ctx).logger


def _get_camera(ctx: Context) -> CameraController:
    return _app(ctx).camera


def _get_spatial(ctx: Context) -> SpatialTracker:
    return _get_seat(ctx).spatial


def _get_map_capture(ctx: Context) -> MapCapture:
    return _get_seat(ctx).map_capture


def _get_watchdog(ctx: Context) -> GameOverWatchdog:
    return _app(ctx).watchdog


# ---------------------------------------------------------------------------
# Turn-ownership gating (handoff mode only)
# ---------------------------------------------------------------------------

# Tools that stay open regardless of whose turn it is: seat management, turn
# status, and the diary (a read of local files).
_GATE_EXEMPT: frozenset[str] = frozenset(
    {
        "claim_seat",
        "release_seat",
        "get_seats",
        "get_turn_status",
        "wait_for_turn",
        "reinstall_handoff",
        "update_diary",
    }
)

# Tools an agent must never call in handoff mode: they reload or kill the game
# out from under the human and the other agents.
_HANDOFF_FORBIDDEN: frozenset[str] = frozenset(
    {
        "load_save",
        "load_game_save",
        "load_save_from_menu",
        "restart_and_load",
        "kill_game",
        "launch_game",
    }
)


def _write_tool_names() -> frozenset[str]:
    """Tools that change game state — everything not flagged read-only."""
    names = set()
    for tool in mcp._tool_manager.list_tools():
        ann = tool.annotations
        if ann is not None and ann.readOnlyHint:
            continue
        names.add(tool.name)
    return frozenset(names - _GATE_EXEMPT)


async def _check_turn_gate(
    ctx: Context, tool_name: str, params: dict[str, Any]
) -> str | None:
    """Refuse writes from a seat that is not on the clock.

    Returns an error message to hand back to the agent, or None to proceed.
    Read tools are always allowed: the human and the agents can see each
    other's state in this design, and an agent that cannot look around while
    off the clock cannot plan.
    """
    app = _app(ctx)
    if not app.seats.enabled:
        return None  # classic single-agent mode — unchanged
    seat = app.seats.resolve(seats_mod.session_key(ctx))
    if tool_name in _GATE_EXEMPT:
        return None
    if seat is app.seats.default:
        available = ", ".join(f"P{p}" for p in app.handoff_config.agent_ids)
        return (
            "You have not claimed a seat yet. This game is shared between a "
            f"human player and the agent seats {available}. "
            "Call get_seats() to see which civ is yours, then "
            "claim_seat(player_id=N) before doing anything else."
        )
    if tool_name in _HANDOFF_FORBIDDEN:
        return (
            f"{tool_name} is disabled in a shared human-vs-agent game — it "
            "would reload or kill the game for the human and every other "
            "agent. Ask the operator if the game needs recovering."
        )
    if tool_name not in _WRITE_TOOLS:
        return None
    if tool_name == "run_lua" and params.get("context") != "ingame":
        return None  # gamecore run_lua is a read

    own = await handoff.try_ownership(_get_game(ctx).conn)
    if own.local_player is None:
        # Game unreachable — let the call through so the agent sees the real
        # connection error rather than a misleading "not your turn".
        return None
    if own.local_player == seat.player_id:
        return None
    return (
        f"Not your turn — {tool_name} refused. "
        f"{handoff.describe_ownership(own, app.handoff_config, seat.player_id)}\n"
        "Read tools still answer for your empire, so keep scouting and "
        "planning. Call wait_for_turn() to block until you are on the clock."
    )


def _forbidden_in_handoff(ctx: Context, tool_name: str) -> str | None:
    """Guard for lifecycle tools that do not route through :func:`_logged`."""
    app = _app(ctx)
    if not app.seats.enabled:
        return None
    return (
        f"{tool_name} is disabled in a shared human-vs-agent game — it would "
        "kill or reload the game for the human and every other agent."
    )


def _param_summary(params: dict[str, Any]) -> str:
    """Compact one-line summary of tool params for console logging."""
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _result_summary(result: str) -> str:
    """First meaningful line of a result, truncated."""
    line = result.split("\n", 1)[0].strip()
    return line[:120] + "..." if len(line) > 120 else line


async def _logged(
    ctx: Context,
    tool_name: str,
    params: dict[str, Any],
    fn: Callable[[], Awaitable[str]],
    *,
    tiles: set[tuple[int, int]] | None = None,
) -> str:
    """Run a tool function with timing, error handling, and logging."""
    logger = _get_logger(ctx)
    turn = logger._turn or "?"
    try:
        refusal = await _check_turn_gate(ctx, tool_name, params)
    except Exception:
        log.debug("Turn-gate check failed, allowing call", exc_info=True)
        refusal = None
    if refusal is not None:
        log.info("[T%s] %s(%s) GATED", turn, tool_name, _param_summary(params))
        await logger.log_error(tool_name, refusal)
        return refusal
    start = time.monotonic()
    try:
        result = await fn()
    except (LuaError, ValueError) as e:
        result = f"Error: {e}"
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)
        return result
    except ConnectionError as e:
        result = str(e)
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)

        # Connection-loss recovery: after consecutive failures,
        # the game has likely crashed. Auto-restart from autosave.
        _logged._conn_errors = getattr(_logged, "_conn_errors", 0) + 1
        if _logged._conn_errors >= 5:
            log.error(
                "CONNECTION RECOVERY: %d consecutive connection failures "
                "— triggering restart_and_load",
                _logged._conn_errors,
            )
            _logged._conn_errors = 0
            try:
                from civ_mcp.autosave import get_autosave_for_turn, get_latest_autosave

                turn_num = logger._turn
                save = (
                    get_autosave_for_turn(int(turn_num))
                    if turn_num
                    else get_latest_autosave()
                )
                restart_result = await game_launcher.restart_and_load(save)
                log.info("CONNECTION RECOVERY: %s", restart_result)
                gs = _get_game(ctx)
                for rc_attempt in range(30):
                    try:
                        await gs.conn.reconnect()
                        if gs.conn.gamecore_index is not None:
                            log.info("CONNECTION RECOVERY: reconnected")
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
            except Exception:
                log.error("CONNECTION RECOVERY: restart failed", exc_info=True)

        return result
    # Success — reset connection error counter + refresh heartbeat
    _logged._conn_errors = 0
    heartbeat.write("playing", turn=turn or 0)
    ms = int((time.monotonic() - start) * 1000)
    log.info(
        "[T%s] %s(%s) OK %dms: %s",
        turn,
        tool_name,
        _param_summary(params),
        ms,
        _result_summary(result),
    )
    await logger.log_tool_call(tool_name, params, result, ms)
    try:
        await _get_spatial(ctx).record(tool_name, params, result, ms, tiles=tiles)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Unified tools — get_full_game_state and execute_commands
# ---------------------------------------------------------------------------
# These two tools consolidate ~70 individual query/action tools into two.
# The code for the original tools is preserved in game_state.py and the
# lua/ modules — they're just not exposed as individual MCP tools.


@mcp.tool(annotations={"readOnlyHint": True})
async def get_full_game_state(ctx: Context) -> str:
    """Get the complete game state in a single call.

    Returns all game information needed to plan your turn: overview, units,
    cities, diplomacy, research, trade routes, resources, victory progress,
    religion, governors, policies, city-states, builder tasks, great people,
    world congress, notifications, strategic map, and your diary (long-term
    plans and the next-turn plan you wrote last turn).

    This replaces all individual get_* query tools. Call this once at the
    start of your turn to orient yourself, then issue commands via
    execute_commands.
    """
    gs = _get_game(ctx)

    async def _run():
        state = await gs.get_full_game_state()
        logger = _get_logger(ctx)
        if state.overview is not None:
            logger.set_turn(state.overview.turn)
            spatial = _get_spatial(ctx)
            spatial.set_turn(state.overview.turn)
            try:
                civ, seed = await gs.get_game_identity()
                logger.bind_game(civ, seed)
                spatial.bind_game(civ, seed)
                heartbeat.bind_game(civ, seed)
                gs.spatial = spatial
            except Exception:
                pass
        text = narrate_full_state(state)

        # Append diary plans (long-term + next-turn) from the JSONL file
        try:
            civ_type, seed = await gs.get_game_identity()
            run_id = _get_logger(ctx).session_id
            path = _diary_path(civ_type, seed, run_id)
            plans = _get_current_plans(path)
            ntp = plans.get("next_turn_plan", "").strip()
            ltp = plans.get("long_term_plans", "").strip()
            if ltp or ntp:
                text += "\n\n=== DIARY ==="
                if ltp:
                    text += f"\nLong-term Plans:\n{ltp}"
                else:
                    text += "\nLong-term Plans: (none)"
                if ntp:
                    text += f"\n\nPlan for This Turn (from last turn):\n{ntp}"
                else:
                    text += "\n\nPlan for This Turn: (none)"
        except Exception:
            log.debug("Failed to append diary to full game state", exc_info=True)

        return text

    return await _logged(ctx, "get_full_game_state", {}, _run)


@mcp.tool()
async def execute_commands(ctx: Context, commands_json: str) -> str:
    """Execute a batch of game commands.

    Args:
        commands_json: A JSON array of command objects. Each object has:
            - action: The command name (matching a game action)
            - params: Dict of parameters for that command

    Example:
        [{"action": "move_unit", "params": {"unit_index": 0, "target_x": 10, "target_y": 20}},
         {"action": "set_city_production", "params": {"city_id": 3, "item_type": "UNIT", "item_name": "UNIT_SETTLER"}},
         {"action": "set_research", "params": {"tech_name": "TECH_IRON_WORKING"}}]

    Use ``unit_index`` (from the idx field in the Units section), not unit_id.
    Use ``target_x``/``target_y`` for the destination coordinates.

    Commands execute sequentially in order. Unit movement commands return
    visibility intel (newly revealed tiles and enemy units) inline — you can
    call this tool multiple times per turn to scout first, then act on new
    intel. Prefer fewer calls where possible.

    Available commands include: move_unit, attack_unit, fortify_unit,
    skip_unit, skip_remaining_units, automate_explore, heal_unit, alert_unit,
    sleep_unit, delete_unit, found_city, improve_tile, remove_feature,
    repair_improvement, build_route, spread_religion, activate_great_person,
    make_trade_route, teleport_to_city, upgrade_unit, promote_unit,
    set_city_production, purchase_item, purchase_tile, set_city_focus,
    city_attack, set_research, set_civic, send_diplomatic_action,
    respond_to_diplomacy, propose_trade, test_trade, respond_to_trade,
    propose_peace, form_alliance, set_policies, appoint_governor,
    assign_governor, promote_governor, send_envoy, change_government,
    choose_dedication, choose_pantheon, found_religion, recruit_great_person,
    patronize_great_person, reject_great_person, queue_wc_votes,
    spy_travel, spy_mission, dismiss_popup, resolve_city_capture,
    and more.
    """
    gs = _get_game(ctx)

    async def _run():
        return await _execute_commands(gs, commands_json)

    return await _logged(ctx, "execute_commands", {}, _run)


# ---------------------------------------------------------------------------
# Seats and turn ownership (human-vs-agent mode)
# ---------------------------------------------------------------------------
# These tools exist only when the server was started with
# CIV_MCP_AGENT_PLAYERS set; main() removes them otherwise.


@mcp.tool(annotations={"readOnlyHint": True})
async def get_seats(ctx: Context) -> str:
    """List the players in this shared game and which agent seats are free.

    This game has a human playing in the Civ 6 UI plus one or more agent
    seats. Call this first, then claim_seat(player_id=N) to take your civ.
    """
    app = _app(ctx)
    cfg = app.handoff_config
    registry = app.seats
    conn = app.game.conn
    roster = await handoff.get_roster(conn, cfg.managed_ids)

    def _name(pid: int) -> str:
        civ, leader, alive = roster.get(pid, ("?", "?", True))
        dead = "" if alive else " [ELIMINATED]"
        return f"{civ} ({leader}){dead}"

    mine = registry.for_session(seats_mod.session_key(ctx))
    lines = [f"Human player: P{cfg.human_id} {_name(cfg.human_id)} — plays in the UI"]
    lines.append("Agent seats:")
    for seat in registry.seats:
        marker = " <-- YOURS" if seat is mine else ""
        held = "claimed" if seat.claimed else "FREE"
        client = f" by {seat.client_name}" if seat.claimed and seat.client_name else ""
        lines.append(
            f"  P{seat.player_id} {_name(seat.player_id)} — {held}{client}{marker}"
        )
    if mine is None:
        lines.append("")
        lines.append("You hold no seat. Call claim_seat(player_id=N) to take one.")

    own = await handoff.try_ownership(conn)
    lines.append("")
    if own.local_player in roster:
        on_clock = f"P{own.local_player} {_name(own.local_player)}"
    elif own.local_player is None:
        on_clock = "unknown"
    else:
        on_clock = f"P{own.local_player} (built-in AI)"
    lines.append(f"Turn {own.turn} — on the clock: {on_clock}")
    if not own.handler_installed:
        lines.append(
            "WARNING: the turn-handoff hook is not armed. Turns will not pass "
            "to agent seats until reinstall_handoff() is called."
        )
    return "\n".join(lines)


@mcp.tool()
async def claim_seat(ctx: Context, player_id: int) -> str:
    """Take control of an agent seat for the rest of this session.

    Args:
        player_id: Player id of the civ you will play. See get_seats().

    Every other tool is refused until you claim a seat. Once claimed, read
    tools answer for your civ at all times — including while other players are
    taking their turns — and write tools work only during your own turn.
    """
    app = _app(ctx)
    key = seats_mod.session_key(ctx)
    if key is None:
        return "Could not identify this MCP session; cannot bind a seat."
    seat, message = app.seats.claim(player_id, key, seats_mod.client_name(ctx))
    if seat is None:
        return message
    roster = await handoff.get_roster(app.game.conn, (player_id,))
    if player_id in roster:
        civ, leader, _alive = roster[player_id]
        seat.label = f"{civ} ({leader})"
        who = f"P{player_id} — {civ} led by {leader}"
    else:
        who = f"P{player_id} (the game is not reachable yet, so its civ is unknown)"
    own = await handoff.try_ownership(app.game.conn)
    return (
        f"You are playing {who}.\n"
        f"{handoff.describe_ownership(own, app.handoff_config, player_id)}\n"
        "Read tools answer for your empire at all times. Write tools (moving "
        "units, production, research, diplomacy, end_turn) work only on your "
        "turn. Use wait_for_turn() to block until you are on the clock; it "
        "returns a report of everything that changed while you waited."
    )


@mcp.tool()
async def release_seat(ctx: Context) -> str:
    """Give up your seat so another client can take over this civ."""
    key = seats_mod.session_key(ctx)
    if key is None:
        return "Could not identify this MCP session."
    seat = _app(ctx).seats.release(key)
    if seat is None:
        return "You did not hold a seat."
    return f"Released P{seat.player_id}."


@mcp.tool(annotations={"readOnlyHint": True})
async def get_turn_status(ctx: Context) -> str:
    """Check whether it is your turn, without blocking.

    Use this to decide between acting now and continuing to scout/plan.
    """
    app = _app(ctx)
    seat = app.seats.resolve(seats_mod.session_key(ctx))
    own = await handoff.try_ownership(app.game.conn)
    if seat is app.seats.default:
        return (
            f"Turn {own.turn}, on the clock: P{own.local_player}. "
            "You hold no seat — call get_seats() then claim_seat(player_id=N)."
        )
    text = handoff.describe_ownership(own, app.handoff_config, seat.player_id)
    if own.local_player is None:
        return (
            f"{text} The game may not have a save loaded yet — ask the human, "
            "or call wait_for_turn() to wait for it."
        )
    if own.local_player == seat.player_id:
        return f"{text} Give your orders, then call end_turn()."
    return (
        f"{text} Write tools are refused; read tools still answer for your "
        "empire. Call wait_for_turn() to block until your turn starts."
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def wait_for_turn(ctx: Context, timeout_seconds: float = 90.0) -> str:
    """Block until it is your turn, then report what changed while you waited.

    Args:
        timeout_seconds: How long to wait before returning. Capped at 600.

    Returns the full turn report for the round that just finished — snapshot
    diff, threats, notifications and empire warnings. Call update_diary() first
    to record your plans, then call wait_for_turn() to block until your next
    turn. Call it again on timeout — no diary interaction happens here.

    If the wait times out it returns the current turn status instead; just call
    it again. Nothing is lost by timing out.
    """
    app = _app(ctx)
    seat = app.seats.resolve(seats_mod.session_key(ctx))
    if seat is app.seats.default:
        return (
            "You hold no seat — call get_seats() then claim_seat(player_id=N) "
            "before waiting for a turn."
        )
    timeout = max(1.0, min(float(timeout_seconds), 600.0))
    return await _logged(
        ctx,
        "wait_for_turn",
        {"timeout_seconds": timeout},
        lambda: handoff.wait_for_turn(seat.game, seat, app.handoff_config, timeout),
    )


@mcp.tool()
async def reinstall_handoff(ctx: Context) -> str:
    """Re-arm the turn-handoff hook after a save load or game restart.

    The hook lives in the game's scripting state and is destroyed whenever a
    save loads. A background keeper re-arms it automatically; this forces it
    immediately and reports the diagnostic log.
    """
    app = _app(ctx)
    if app.keeper is None:
        return "Handoff mode is not enabled on this server."
    own = await app.keeper.ensure_installed(force=True)
    events = await handoff.read_log(app.game.conn)
    lines = [
        f"Handoff re-armed. Turn {own.turn}, local player P{own.local_player}, "
        f"managed players: {', '.join(f'P{p}' for p in own.managed) or 'none'}."
    ]
    if events:
        lines.append("Recent handoffs (turn|activated|local before|after|result):")
        lines.extend(f"  {e}" for e in events[-10:])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query tools (read-only)
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_game_overview(ctx: Context) -> str:
    """Get a high-level summary of the current game state.

    Returns turn number, civilization, yields (gold/science/culture/faith),
    current research and civic, and counts of cities and units.
    Call this first to orient yourself.
    """
    gs = _get_game(ctx)

    async def _run():
        ov = await gs.get_game_overview()
        logger = _get_logger(ctx)
        logger.set_turn(ov.turn)
        spatial = _get_spatial(ctx)
        spatial.set_turn(ov.turn)
        try:
            civ, seed = await gs.get_game_identity()
            logger.bind_game(civ, seed)
            spatial.bind_game(civ, seed)
            heartbeat.bind_game(civ, seed)
            gs.spatial = spatial
        except Exception:
            pass
        # Seed revealed tiles for visibility diff (once per session)
        if not spatial._revealed_seeded:
            try:
                seed_lines = await gs.conn.execute_read(
                    lq.build_revealed_tiles_seed_query()
                )
                seed_tiles = lq.parse_revealed_tiles_seed(seed_lines)
                spatial.seed_revealed(seed_tiles)
                log.info(
                    "Seeded spatial tracker with %d revealed tiles", len(seed_tiles)
                )
            except Exception:
                log.debug("Failed to seed revealed tiles", exc_info=True)
        text = nr.narrate_overview(ov)
        # Check for game-over state
        gameover = await gs.check_game_over()
        if gameover is not None:
            vtype = (
                gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
            )
            if gameover.is_defeat:
                text += (
                    f"\n\n*** GAME OVER — DEFEAT ***\n"
                    f"{gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory.\n"
                    f"No further actions are possible."
                )
            else:
                text += f"\n\n*** GAME OVER — VICTORY ***\nYou won a {vtype} victory!"
            try:
                await logger.log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            except Exception:
                log.warning("Failed to log game-over in overview", exc_info=True)
        return text

    return await _logged(ctx, "get_game_overview", {}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_units(ctx: Context) -> str:
    """List all your units with position, type, movement, and health.

    Each unit shows its id and idx (needed for action commands).
    Consumed units (e.g. settlers that founded cities) are excluded.
    """
    gs = _get_game(ctx)
    unit_tiles: set[tuple[int, int]] = set()

    async def _run():
        units = await gs.get_units()
        unit_tiles.update((u.x, u.y) for u in units if u.x >= 0)
        try:
            threats = await gs.get_threat_scan()
        except Exception:
            threats = None
        trade_status = None
        try:
            trade_status = await gs.get_trade_routes()
        except Exception:
            pass
        return nr.narrate_units(units, threats, trade_status)

    return await _logged(ctx, "get_units", {}, _run, tiles=unit_tiles)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_spies(ctx: Context) -> str:
    """List all your spy units with position, rank, city, and available missions.

    Shows each spy's composite id (needed for spy_action), current location,
    rank (Recruit/Agent/Special Agent/Senior Agent), XP, and which operations
    are available at their current position.

    Note: offensive missions only become available once the spy has physically
    arrived in the target city. Use spy_action with action='travel' first.
    """
    gs = _get_game(ctx)

    async def _run():
        spies = await gs.get_spies()
        return nr.narrate_spies(spies)

    return await _logged(ctx, "get_spies", {}, _run)


# @mcp.tool()
async def spy_action(
    ctx: Context,
    unit_id: int,
    action: str,
    target_x: int,
    target_y: int,
) -> str:
    """Send a spy to a city or launch a spy mission.

    Args:
        unit_id: The spy's composite ID (from get_spies output)
        action: 'travel' to move spy to a city, or a mission type to launch a mission.
            Mission types: COUNTERSPY, GAIN_SOURCES, SIPHON_FUNDS, STEAL_TECH_BOOST,
            SABOTAGE_PRODUCTION, GREAT_WORK_HEIST, RECRUIT_PARTISANS,
            NEUTRALIZE_GOVERNOR, FABRICATE_SCANDAL
        target_x: X coordinate of the target city tile
        target_y: Y coordinate of the target city tile

    Travel notes:
        - Valid targets: your own cities and city-states only.
        - Allied civ cities are NOT valid travel targets.
        - Travel is queued end-of-turn; spy position updates after turn ends.

    Mission notes:
        - Spy must be physically IN the target city to launch any offensive mission.
        - Use 'travel' first, then end the turn, then launch the mission.
        - COUNTERSPY defends your own city (spy must be in your city).
        - get_spies shows which ops are available at the spy's current location.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    params = {
        "unit_id": unit_id,
        "action": action,
        "target_x": target_x,
        "target_y": target_y,
    }

    async def _run():
        if action.lower() == "travel":
            return await gs.spy_travel(unit_index, target_x, target_y)
        return await gs.spy_mission(unit_index, action.upper(), target_x, target_y)

    result = await _logged(ctx, "spy_action", params, _run)
    _get_camera(ctx).push(target_x, target_y, f"spy {action}")
    return result


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_cities(ctx: Context) -> str:
    """List all your cities with yields, population, production, growth, and loyalty.

    Each city shows its id (needed for production commands).
    Cities losing loyalty show warnings with flip timers.
    """
    gs = _get_game(ctx)

    async def _run():
        cities, distances = await gs.get_cities()
        return nr.narrate_cities(cities, distances)

    return await _logged(ctx, "get_cities", {}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_city_production(ctx: Context, city_id: int) -> str:
    """List what a city can produce right now.

    Args:
        city_id: City ID (from get_cities output)

    Returns available units, buildings, and districts with production costs.
    Call this when a city finishes building or to decide what to produce next.
    """
    gs = _get_game(ctx)

    async def _run():
        options = await gs.list_city_production(city_id)
        return nr.narrate_city_production(options)

    return await _logged(ctx, "get_city_production", {"city_id": city_id}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_map_area(
    ctx: Context, center_x: int, center_y: int, radius: int = 2
) -> str:
    """Get terrain info for tiles around a point.

    Args:
        center_x: X coordinate of center tile
        center_y: Y coordinate of center tile
        radius: How many tiles out from center (default 2, max 4)
    """
    radius = min(radius, 4)
    gs = _get_game(ctx)
    tile_coords: set[tuple[int, int]] = set()

    async def _run():
        tiles = await gs.get_map_area(center_x, center_y, radius)
        tile_coords.update((t.x, t.y) for t in tiles)
        return nr.narrate_map(tiles)

    result = await _logged(
        ctx,
        "get_map_area",
        {"center_x": center_x, "center_y": center_y, "radius": radius},
        _run,
        tiles=tile_coords,
    )
    _get_camera(ctx).push(center_x, center_y, f"map_area ({center_x},{center_y})")
    return result


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_settle_advisor(ctx: Context, unit_id: int) -> str:
    """List best settle locations near a settler unit.

    Args:
        unit_id: The settler's composite ID (from get_units output)

    Scores locations by yields, water, defense, and resource value.
    Returns top 5 candidates sorted by score.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    return await _logged(
        ctx,
        "get_settle_advisor",
        {"unit_id": unit_id},
        lambda: gs.get_settle_advisor(unit_index),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pathing_estimate(
    ctx: Context, unit_id: int, target_x: int, target_y: int
) -> str:
    """Estimate how many turns a unit needs to reach a destination.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        target_x: Destination X coordinate
        target_y: Destination Y coordinate

    Returns estimated turns, path length, and reachable tiles this turn.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536

    async def _run():
        est = await gs.get_pathing_estimate(unit_index, target_x, target_y)
        return nr.narrate_pathing_estimate(est)

    return await _logged(
        ctx,
        "get_pathing_estimate",
        {"unit_id": unit_id, "target_x": target_x, "target_y": target_y},
        _run,
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_global_settle_advisor(ctx: Context) -> str:
    """Find the best settle locations across the entire revealed map.

    Unlike get_settle_advisor (which searches near a specific settler),
    this scans all revealed land for the top 10 settle candidates.
    Use this when deciding WHERE to send a settler, not just where to settle.
    """
    gs = _get_game(ctx)

    async def _run():
        candidates = await gs.get_global_settle_scan()
        if not candidates:
            return "No valid settle locations found on revealed map."
        return nr.narrate_settle_candidates(candidates)

    return await _logged(ctx, "get_global_settle_advisor", {}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_builder_tasks(ctx: Context) -> str:
    """Get a prioritized task board for all your builders.

    Scans your territory for tiles needing improvements and matches them
    with idle builders. Like the builder lens in the UI — shows what to
    build where and which builder is closest.

    Priority tiers:
    - URGENT: Pillaged improvements (yield loss), unimproved strategic resources
    - HIGH: Unimproved luxury/bonus resources
    - NORMAL: Empty tiles that could benefit from farms/mines/lumber mills

    Call this before issuing builder orders each turn.
    """
    gs = _get_game(ctx)

    async def _run():
        tasks, builders = await gs.get_builder_tasks()
        return nr.narrate_builder_tasks(tasks, builders)

    return await _logged(ctx, "get_builder_tasks", {}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_empire_resources(ctx: Context) -> str:
    """Get a summary of all resources in and near your empire.

    Shows owned resources (improved/unimproved) grouped by type,
    and unclaimed resources near your cities.
    """
    gs = _get_game(ctx)

    async def _run():
        stockpiles, owned, nearby, luxuries = await gs.get_empire_resources()
        return nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries)

    return await _logged(ctx, "get_empire_resources", {}, _run)


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_strategic_map(ctx: Context) -> str:
    """Get fog-of-war boundaries and unclaimed resources across the map.

    Shows how far explored territory extends from each city (in 6 directions),
    highlighting directions that need exploration. Also lists unclaimed luxury
    and strategic resources on revealed but unowned land.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_strategic_map",
        {},
        lambda: _narrate(gs.get_strategic_map, nr.narrate_strategic_map),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_diplomacy(ctx: Context) -> str:
    """Get diplomatic status with all known civilizations.

    Shows diplomatic state (Friendly/Neutral/Unfriendly), relationship modifiers
    with scores and reasons, grievances, delegations/embassies, and available
    diplomatic actions you can take. Also shows visible enemy city details
    (name, population, loyalty, walls).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy, nr.narrate_diplomacy),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_tech_civics(ctx: Context) -> str:
    """Get technology and civic research status.

    Shows current research, current civic, turns remaining,
    and lists of available technologies and civics to choose from.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_tech_civics",
        {},
        lambda: _narrate(gs.get_tech_civics, nr.narrate_tech_civics),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_trades(ctx: Context) -> str:
    """Check for pending trade deal offers from other civilizations.

    Shows what each civ is offering and what they want in return.
    Use respond_to_trade to accept or reject.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_trades",
        {},
        lambda: _narrate(gs.get_pending_deals, nr.narrate_pending_deals),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_policies(ctx: Context) -> str:
    """Get current government, policy slots, and available policies.

    Shows current government type, each policy slot with its type and current
    policy (if any), and all unlocked policies grouped by compatible slot type.
    Wildcard slots accept any policy type.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "get_policies", {}, lambda: _narrate(gs.get_policies, nr.narrate_policies)
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_notifications(ctx: Context) -> str:
    """Get all active game notifications.

    Shows action-required items (need your decision) and informational
    notifications. Action-required items include which MCP tool to use
    to resolve them. Call this to check what needs attention without
    ending the turn.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_notifications",
        {},
        lambda: _narrate(gs.get_notifications, nr.narrate_notifications),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_diplomacy(ctx: Context) -> str:
    """Check for pending diplomacy encounters (e.g. first meeting with a civ).

    Diplomacy encounters block turn progression. Call this if end_turn
    reports the turn didn't advance. Returns any open sessions with their
    dialogue text, visible buttons, and response guidance.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy_sessions, nr.narrate_diplomacy_sessions),
    )


# ---------------------------------------------------------------------------
# Action tools (mutating)
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_governors(ctx: Context) -> str:
    """Get governor status, appointed governors, and available types.

    Shows governor points, currently appointed governors with assignments,
    and governors available to appoint. Use appoint_governor to appoint one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_governors",
        {},
        lambda: _narrate(gs.get_governors, nr.narrate_governors),
    )


# @mcp.tool()
async def appoint_governor(ctx: Context, governor_type: str) -> str:
    """Appoint a new governor.

    Args:
        governor_type: e.g. GOVERNOR_THE_EDUCATOR (Pingala), GOVERNOR_THE_DEFENDER (Victor)

    Requires available governor points. Use get_governors to see options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "appoint_governor",
        {"governor_type": governor_type},
        lambda: gs.appoint_governor(governor_type),
    )


# @mcp.tool()
async def assign_governor(ctx: Context, governor_type: str, city_id: int) -> str:
    """Assign an appointed governor to a city.

    Args:
        governor_type: The governor type (from get_governors output)
        city_id: The city ID (from get_cities output)

    Governor must already be appointed. Takes several turns to establish.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "assign_governor",
        {"governor_type": governor_type, "city_id": city_id},
        lambda: gs.assign_governor(governor_type, city_id),
    )


# @mcp.tool()
async def promote_governor(
    ctx: Context, governor_type: str, promotion_type: str
) -> str:
    """Promote a governor with a new ability.

    Args:
        governor_type: The governor type (from get_governors output)
        promotion_type: The promotion type (from get_governors output, shown under each governor)

    Requires available governor points. Use get_governors to see available promotions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_governor",
        {"governor_type": governor_type, "promotion_type": promotion_type},
        lambda: gs.promote_governor(governor_type, promotion_type),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_unit_promotions(ctx: Context, unit_id: int) -> str:
    """List available promotions for a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Shows promotions filtered by the unit's promotion class.
    Only units with enough XP will have promotions available.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_unit_promotions(unit_id)
        return nr.narrate_unit_promotions(status)

    return await _logged(ctx, "get_unit_promotions", {"unit_id": unit_id}, _run)


# @mcp.tool()
async def promote_unit(ctx: Context, unit_id: int, promotion_type: str) -> str:
    """Apply a promotion to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        promotion_type: e.g. PROMOTION_BATTLECRY, PROMOTION_TORTOISE

    Use get_unit_promotions first to see available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_unit",
        {"unit_id": unit_id, "promotion_type": promotion_type},
        lambda: gs.promote_unit(unit_id, promotion_type),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_city_states(ctx: Context) -> str:
    """List known city-states with envoy counts and types.

    Shows envoy tokens available, each city-state's type (Scientific,
    Industrial, etc.), how many envoys you've sent, and who is suzerain.
    Use send_envoy to send an envoy.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_city_states",
        {},
        lambda: _narrate(gs.get_city_states, nr.narrate_city_states),
    )


# @mcp.tool()
async def send_envoy(ctx: Context, player_id: int) -> str:
    """Send an envoy to a city-state.

    Args:
        player_id: The city-state's player ID (from get_city_states)

    Requires available envoy tokens. Use get_city_states to see options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "send_envoy", {"player_id": player_id}, lambda: gs.send_envoy(player_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pantheon_beliefs(ctx: Context) -> str:
    """Get pantheon status and available beliefs for selection.

    Shows current pantheon (if any), faith balance, and all available
    pantheon beliefs with their bonuses. Use choose_pantheon to found one.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_pantheon_status()
        return nr.narrate_pantheon_status(status)

    return await _logged(ctx, "get_pantheon_beliefs", {}, _run)


# @mcp.tool()
async def choose_pantheon(ctx: Context, belief_type: str) -> str:
    """Found a pantheon with the specified belief.

    Args:
        belief_type: e.g. BELIEF_GOD_OF_THE_FORGE, BELIEF_DIVINE_SPARK

    Use get_pantheon_beliefs first to see options. Requires enough faith
    and no existing pantheon.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_pantheon",
        {"belief_type": belief_type},
        lambda: gs.choose_pantheon(belief_type),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_beliefs(ctx: Context) -> str:
    """Get religion founding status, available religions, and available beliefs.

    Shows whether you've founded a religion, available religion types to choose,
    and beliefs grouped by class (Follower, Founder, Enhancer, Worship).
    Use found_religion to found a religion after your Great Prophet activates.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_religion_founding_status()
        return nr.narrate_religion_founding_status(status)

    return await _logged(ctx, "get_religion_beliefs", {}, _run)


# @mcp.tool()
async def found_religion(
    ctx: Context, religion_type: str, follower_belief: str, founder_belief: str
) -> str:
    """Found a religion with a chosen name, follower belief, and founder belief.

    Args:
        religion_type: e.g. RELIGION_HINDUISM, RELIGION_BUDDHISM, RELIGION_ISLAM
        follower_belief: e.g. BELIEF_WORK_ETHIC, BELIEF_CHORAL_MUSIC
        founder_belief: e.g. BELIEF_STEWARDSHIP, BELIEF_CHURCH_PROPERTY

    Requires your Great Prophet to have already activated on a Holy Site
    (via UNITOPERATION_FOUND_RELIGION). Use get_religion_beliefs
    first to see available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "found_religion",
        {
            "religion_type": religion_type,
            "follower_belief": follower_belief,
            "founder_belief": founder_belief,
        },
        lambda: gs.found_religion(religion_type, follower_belief, founder_belief),
    )


# @mcp.tool()
async def upgrade_unit(ctx: Context, unit_id: int) -> str:
    """Upgrade a unit to its next type (e.g. Slinger -> Archer).

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Requires the right technology, enough gold, and the unit must have
    moves remaining. The unit's movement is consumed by upgrading.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "upgrade_unit", {"unit_id": unit_id}, lambda: gs.upgrade_unit(unit_id)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_dedications(ctx: Context) -> str:
    """Get current era age, available dedications, and active ones.

    Shows era score thresholds, whether you're in a Golden/Dark/Normal age,
    and lists available dedication choices with their bonuses.
    Use choose_dedication to select one when required.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_dedications()
        return nr.narrate_dedications(status)

    return await _logged(ctx, "get_dedications", {}, _run)


# @mcp.tool()
async def choose_dedication(ctx: Context, dedication_index: int) -> str:
    """Choose a dedication/commemoration for the current era.

    Args:
        dedication_index: The index of the dedication (from get_dedications output)

    Use get_dedications first to see available options and their bonuses.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_dedication",
        {"dedication_index": dedication_index},
        lambda: gs.choose_dedication(dedication_index),
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_options(ctx: Context, other_player_id: int) -> str:
    """See what both sides can trade — like opening the trade screen.

    Args:
        other_player_id: The player ID (from get_diplomacy output)

    Shows gold, resources, favor, open borders status, and alliance eligibility
    for both you and the other civilization. Use before propose_trade to see
    what's available.
    """
    gs = _get_game(ctx)

    async def _run():
        opts = await gs.get_deal_options(other_player_id)
        return nr.narrate_deal_options(opts)

    return await _logged(
        ctx, "get_trade_options", {"other_player_id": other_player_id}, _run
    )


# @mcp.tool()
async def respond_to_trade(ctx: Context, other_player_id: int, accept: bool) -> str:
    """Accept or reject a pending trade deal.

    Args:
        other_player_id: The player ID of the civilization (from get_pending_trades)
        accept: True to accept the deal, False to reject it

    Use get_pending_trades first to see what's being offered.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_trade",
        {"other_player_id": other_player_id, "accept": accept},
        lambda: gs.respond_to_deal(other_player_id, accept),
    )


# @mcp.tool()
async def propose_trade(
    ctx: Context,
    other_player_id: int,
    offer_gold: int = 0,
    offer_gold_per_turn: int = 0,
    offer_resources: str = "",
    offer_favor: int = 0,
    offer_open_borders: bool = False,
    request_gold: int = 0,
    request_gold_per_turn: int = 0,
    request_resources: str = "",
    request_favor: int = 0,
    request_open_borders: bool = False,
    joint_war_target: int = 0,
    mode: str = "send",
) -> str:
    """Propose a trade deal to another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        offer_gold: Lump sum gold to give them
        offer_gold_per_turn: Gold per turn to give them (30-turn duration)
        offer_resources: Comma-separated resource types to offer, e.g. "RESOURCE_SILK,RESOURCE_TEA"
        offer_favor: Diplomatic favor to offer
        offer_open_borders: True to offer our open borders
        request_gold: Lump sum gold to request from them
        request_gold_per_turn: Gold per turn to request (30-turn duration)
        request_resources: Comma-separated resource types to request
        request_favor: Diplomatic favor to request from them
        request_open_borders: True to request their open borders
        joint_war_target: Player ID of a third civ to declare joint war against
        mode: "send" to commit the deal, "test" to preview AI's counter-offer without committing

    Examples: Gift 100 gold: offer_gold=100. Trade silk for 3 gpt: offer_resources="RESOURCE_SILK", request_gold_per_turn=3.
    Mutual open borders: offer_open_borders=True, request_open_borders=True.
    Test a deal first: mode="test" to see what the AI thinks is fair, then mode="send" to commit.
    """
    gs = _get_game(ctx)

    offer_items: list[dict] = []
    request_items: list[dict] = []
    if offer_gold > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})
    if offer_gold_per_turn > 0:
        offer_items.append(
            {"type": "GOLD", "amount": offer_gold_per_turn, "duration": 30}
        )
    for res in (r.strip() for r in offer_resources.split(",") if r.strip()):
        offer_items.append(
            {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        )
    if offer_favor > 0:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})
    if offer_open_borders:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if request_gold > 0:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})
    if request_gold_per_turn > 0:
        request_items.append(
            {"type": "GOLD", "amount": request_gold_per_turn, "duration": 30}
        )
    for res in (r.strip() for r in request_resources.split(",") if r.strip()):
        request_items.append(
            {"type": "RESOURCE", "name": res, "amount": 1, "duration": 30}
        )
    if request_favor > 0:
        request_items.append({"type": "FAVOR", "amount": request_favor})
    if request_open_borders:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if joint_war_target > 0:
        # Joint war is mutual — both sides commit
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})

    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    if mode == "test":
        return await _logged(
            ctx,
            "test_trade",
            {
                "other_player_id": other_player_id,
                "offer_items": offer_items,
                "request_items": request_items,
            },
            lambda: gs.test_trade(other_player_id, offer_items, request_items),
        )

    return await _logged(
        ctx,
        "propose_trade",
        {
            "other_player_id": other_player_id,
            "offer_items": offer_items,
            "request_items": request_items,
        },
        lambda: gs.propose_trade(other_player_id, offer_items, request_items),
    )


# @mcp.tool()
async def propose_peace(ctx: Context, other_player_id: int) -> str:
    """Propose white peace to a civilization you're at war with.

    Args:
        other_player_id: The player ID (from get_diplomacy output)

    Requires being at war and past the 10-turn war cooldown.
    The AI may accept or reject based on war score and relationship.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "propose_peace",
        {"other_player_id": other_player_id},
        lambda: gs.propose_peace(other_player_id),
    )


# @mcp.tool()
async def set_policies(ctx: Context, assignments: str) -> str:
    """Set policy cards in government slots.

    Args:
        assignments: Comma-separated slot assignments, e.g.
            "0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING"
            Slots not listed keep their current policy. Use NONE to
            explicitly clear a slot (e.g. "2=NONE"). Use get_policies to
            see available policies and slot indices.

    Wildcard slots can accept any policy type. Military slots accept
    military policies, economic slots accept economic policies, etc.
    """
    gs = _get_game(ctx)

    async def _run():
        parsed: dict[int, str] = {}
        for pair in assignments.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            idx_str, policy = pair.split("=", 1)
            parsed[int(idx_str.strip())] = policy.strip()
        if not parsed:
            return "Error: no valid assignments. Format: '0=POLICY_AGOGE,1=POLICY_URBAN_PLANNING'"
        return await gs.set_policies(parsed)

    return await _logged(ctx, "set_policies", {"assignments": assignments}, _run)


# @mcp.tool()
async def respond_to_diplomacy(
    ctx: Context, other_player_id: int, response: str
) -> str:
    """Respond to a pending diplomacy encounter.

    Args:
        other_player_id: The player ID of the other civilization (from get_pending_diplomacy)
        response: "POSITIVE" (friendly) or "NEGATIVE" (dismissive)

    First meetings typically have 2-3 rounds. The tool automatically detects
    and closes goodbye-phase sessions (where dialogue text stops changing).
    If SESSION_CONTINUES is returned, send another response for the next round.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_diplomacy",
        {"other_player_id": other_player_id, "response": response},
        lambda: gs.diplomacy_respond(other_player_id, response),
    )


# @mcp.tool()
async def send_diplomatic_action(
    ctx: Context, other_player_id: int, action: str
) -> str:
    """Send a proactive diplomatic action to another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        action: One of: DIPLOMATIC_DELEGATION, DECLARE_FRIENDSHIP, DENOUNCE,
                RESIDENT_EMBASSY, OPEN_BORDERS,
                DECLARE_SURPRISE_WAR, DECLARE_FORMAL_WAR, DECLARE_HOLY_WAR,
                DECLARE_LIBERATION_WAR, DECLARE_RECONQUEST_WAR,
                DECLARE_PROTECTORATE_WAR, DECLARE_COLONIAL_WAR,
                DECLARE_TERRITORIAL_WAR

    Delegations cost 25 gold and can be rejected if the civ dislikes you.
    Embassies require Writing tech. Use get_diplomacy to see available actions.
    Surprise war is always available if not allied/friends. Other war types
    (casus belli) require specific civics and conditions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "send_diplomatic_action",
        {"other_player_id": other_player_id, "action": action},
        lambda: gs.send_diplomatic_action(other_player_id, action),
    )


# @mcp.tool()
async def form_alliance(
    ctx: Context, other_player_id: int, alliance_type: str = "MILITARY"
) -> str:
    """Form an alliance with another civilization.

    Args:
        other_player_id: The player ID (from get_diplomacy output)
        alliance_type: One of: MILITARY, RESEARCH, CULTURAL, ECONOMIC, RELIGIOUS

    Requires declared friendship and Diplomatic Service civic.
    Use get_trade_options to check alliance eligibility first.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "form_alliance",
        {"other_player_id": other_player_id, "alliance_type": alliance_type},
        lambda: gs.form_alliance(other_player_id, alliance_type.upper()),
    )


# @mcp.tool()
async def city_action(
    ctx: Context,
    city_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
) -> str:
    """Issue a command to a city.

    Args:
        city_id: City ID (from get_cities output)
        action: Currently supported: 'attack' (city ranged attack)
        target_x: Target X coordinate (required for attack)
        target_y: Target Y coordinate (required for attack)

    For attack: city must have walls and not have fired this turn.
    Range is 2 tiles from city center.

    For captured/disloyal city decisions (city_id is ignored, uses pending city):
    - 'keep': Keep the city (works for both captured and loyalty-flipped cities)
    - 'reject': Reject/free a disloyal city (loyalty flip only)
    - 'raze': Raze a captured city (military conquest only)
    - 'liberate_founder': Liberate to original founder
    - 'liberate_previous': Liberate to previous owner
    """
    gs = _get_game(ctx)
    match action:
        case "attack":
            if target_x is None or target_y is None:
                return "Error: attack requires target_x and target_y"
            result = await _logged(
                ctx,
                "city_attack",
                {"city_id": city_id, "x": target_x, "y": target_y},
                lambda: gs.city_attack(city_id, target_x, target_y),
            )
            _get_camera(ctx).push(target_x, target_y, "city attack")
            return result
        case "keep" | "reject" | "raze" | "liberate_founder" | "liberate_previous":
            return await _logged(
                ctx,
                "resolve_city_capture",
                {"action": action},
                lambda: gs.resolve_city_capture(action),
            )
        case _:
            return f"Error: Unknown city action '{action}'. Available: attack, keep, reject, raze, liberate_founder, liberate_previous"


# @mcp.tool()
async def unit_action(
    ctx: Context,
    unit_id: int,
    action: str,
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
    improvement: Optional[str] = None,
) -> str:
    """Issue a command to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        action: One of: move, attack, fortify, skip, found_city, improve, repair, remove_improvement, remove_feature, build_route, automate, heal, alert, sleep, delete, trade_route, activate, sacrifice_charges, teleport, spread_religion
        target_x: Target X coordinate (required for move/attack/trade_route/teleport)
        target_y: Target Y coordinate (required for move/attack/trade_route/teleport)
        improvement: Improvement type for builders (required for improve), e.g.
            IMPROVEMENT_FARM, IMPROVEMENT_MINE, IMPROVEMENT_QUARRY,
            IMPROVEMENT_PLANTATION, IMPROVEMENT_CAMP, IMPROVEMENT_PASTURE,
            IMPROVEMENT_FISHING_BOATS, IMPROVEMENT_LUMBER_MILL

    For move/attack: provide target_x and target_y.
    For trade_route: provide target_x and target_y of destination city.
    For teleport: provide target_x and target_y of destination city. Traders only, must be idle (not on active route).
    For improve: provide improvement name. Builder must be on the tile.
    For repair: repairs a pillaged improvement on the builder's current tile. No improvement name needed.
    For remove_improvement: demolishes an intact improvement on the builder's current tile (e.g. to replace a farm with a mine). Costs one charge.
    For activate: activates a Great Person on their matching district.
    For sacrifice_charges: Royal Society builder sacrifice — spends ALL builder charges to boost a district project (2% of cost per charge). Builder must be on the district tile.
    For spread_religion: spreads religion at current tile. Missionaries/Apostles only.
    For build_route: builds road/railroad on current tile. Military Engineers only. No charges used; costs 1 Iron + 1 Coal per railroad tile.
    For fortify/skip/found_city/automate/heal/alert/sleep/delete: no target needed.
    heal = fortify until healed (auto-wake at full HP).
    alert = sleep but auto-wake when enemy enters sight range.
    delete = permanently disband the unit.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    params: dict[str, Any] = {"unit_id": unit_id, "action": action}
    if target_x is not None:
        params["target_x"] = target_x
    if target_y is not None:
        params["target_y"] = target_y
    if improvement:
        params["improvement"] = improvement

    async def _run():
        match action.lower():
            case "move":
                if target_x is None or target_y is None:
                    return "Error: move requires target_x and target_y"
                return await gs.move_unit(unit_index, target_x, target_y)
            case "attack":
                if target_x is None or target_y is None:
                    return "Error: attack requires target_x and target_y"
                return await gs.attack_unit(unit_index, target_x, target_y)
            case "fortify":
                return await gs.fortify_unit(unit_index)
            case "skip":
                return await gs.skip_unit(unit_index)
            case "found_city":
                return await gs.found_city(unit_index)
            case "improve":
                if not improvement:
                    return "Error: improve requires improvement name (e.g. IMPROVEMENT_FARM). To repair a pillaged improvement, use action='repair' instead."
                return await gs.improve_tile(unit_index, improvement)
            case "repair":
                return await gs.repair_improvement(unit_index)
            case "remove_improvement":
                return await gs.remove_improvement(unit_index)
            case "remove_feature":
                return await gs.remove_feature(unit_index)
            case "build_route":
                return await gs.build_route(unit_index)
            case "automate":
                return await gs.automate_explore(unit_index)
            case "heal":
                return await gs.heal_unit(unit_index)
            case "alert":
                return await gs.alert_unit(unit_index)
            case "sleep":
                return await gs.sleep_unit(unit_index)
            case "delete":
                return await gs.delete_unit(unit_index)
            case "trade_route":
                if target_x is None or target_y is None:
                    return "Error: trade_route requires target_x and target_y of destination city"
                return await gs.make_trade_route(unit_index, target_x, target_y)
            case "activate":
                return await gs.activate_great_person(unit_index)
            case "sacrifice_charges":
                return await gs.sacrifice_builder_charges(unit_index)
            case "spread_religion":
                return await gs.spread_religion(unit_index)
            case "teleport":
                if target_x is None or target_y is None:
                    return "Error: teleport requires target_x and target_y of the destination city"
                return await gs.teleport_to_city(unit_index, target_x, target_y)
            case _:
                return f"Error: Unknown action '{action}'. Valid: move, attack, fortify, skip, found_city, improve, repair, remove_improvement, remove_feature, build_route, automate, heal, alert, sleep, delete, trade_route, activate, sacrifice_charges, teleport, spread_religion"

    result = await _logged(ctx, "unit_action", params, _run)
    if (
        action.lower() in ("move", "attack", "trade_route", "teleport")
        and target_x is not None
        and target_y is not None
    ):
        _get_camera(ctx).push(target_x, target_y, f"{action}→({target_x},{target_y})")
    return result


# @mcp.tool()
async def skip_remaining_units(ctx: Context) -> str:
    """Skip all units that still have moves remaining.

    Useful after diplomacy encounters invalidate all standing orders.
    Uses GameCore FinishMoves on each unit — fast, reliable, no async issues.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "skip_remaining_units", {}, lambda: gs.skip_remaining_units()
    )


# @mcp.tool()
async def set_city_production(
    ctx: Context,
    city_id: int,
    item_type: str,
    item_name: str,
    target_x: int | None = None,
    target_y: int | None = None,
) -> str:
    """Set what a city should produce.

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT, BUILDING, DISTRICT, or PROJECT
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT, DISTRICT_CAMPUS, PROJECT_LAUNCH_EARTH_SATELLITE
        target_x: X coordinate for district/wonder placement (required for districts — use get_district_advisor to find best tile)
        target_y: Y coordinate for district/wonder placement

    Tip: call get_cities first to see your cities and their IDs.
    """
    gs = _get_game(ctx)
    params: dict = {"city_id": city_id, "item_type": item_type, "item_name": item_name}
    if target_x is not None:
        params["target_x"] = target_x
        params["target_y"] = target_y
    return await _logged(
        ctx,
        "set_city_production",
        params,
        lambda: gs.set_city_production(
            city_id, item_type, item_name, target_x, target_y
        ),
    )


# @mcp.tool()
async def purchase_item(
    ctx: Context,
    city_id: int,
    item_type: str,
    item_name: str,
    yield_type: str = "YIELD_GOLD",
) -> str:
    """Purchase a unit or building instantly with gold or faith.

    Args:
        city_id: City ID (from get_cities output)
        item_type: UNIT or BUILDING
        item_name: e.g. UNIT_WARRIOR, BUILDING_MONUMENT
        yield_type: YIELD_GOLD (default) or YIELD_FAITH

    Costs gold/faith immediately. Use get_city_production to see what's available.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "purchase_item",
        {
            "city_id": city_id,
            "item_type": item_type,
            "item_name": item_name,
            "yield_type": yield_type,
        },
        lambda: gs.purchase_item(city_id, item_type, item_name, yield_type),
    )


# @mcp.tool()
async def set_research(ctx: Context, tech_or_civic: str, category: str = "tech") -> str:
    """Choose a technology or civic to research.

    Args:
        tech_or_civic: The type name, e.g. TECH_POTTERY or CIVIC_CRAFTSMANSHIP
        category: "tech" or "civic" (default: tech)

    Tip: call get_tech_civics first to see available options.
    """
    gs = _get_game(ctx)

    async def _run():
        if category.lower() == "civic":
            return await gs.set_civic(tech_or_civic)
        return await gs.set_research(tech_or_civic)

    return await _logged(
        ctx,
        "set_research",
        {"tech_or_civic": tech_or_civic, "category": category},
        _run,
    )


@mcp.tool(annotations={"destructiveHint": True})
async def end_turn(ctx: Context) -> str:
    """End the current turn.

    Make sure you've moved all units, set production, and chosen research
    before ending the turn.

    After end_turn(), think about what to do next turn and call
    update_diary(next_turn_plan=..., long_term_plans=...) to record your
    plans. Then call wait_for_turn() to block until your next turn starts.
    """
    gs = _get_game(ctx)

    # Check turn ownership up front.
    refusal = await _check_turn_gate(ctx, "end_turn", {})
    if refusal is not None:
        return refusal

    # Model ID comes from CIV_MCP_AGENT_MODEL env var (set by eval runner)
    env_model = os.environ.get("CIV_MCP_AGENT_MODEL", "")
    if env_model:
        _get_logger(ctx).set_agent_model(env_model)

    # Capture turn info for later use (map capture, heartbeat, etc.)
    _diary_turn = 0
    _diary_civ_type = None
    _diary_seed = None
    try:
        ov = await gs.get_game_overview()
        _diary_turn = ov.turn
        # Keep logger/spatial turn in sync
        _get_logger(ctx).set_turn(ov.turn)
        _get_spatial(ctx).set_turn(ov.turn)
    except Exception:
        log.warning("Failed to capture overview before end_turn", exc_info=True)
    try:
        _diary_civ_type, _diary_seed = await gs.get_game_identity()
    except Exception:
        log.warning("Failed to get game identity before end_turn", exc_info=True)

    # Advance the turn
    _seat = _get_seat(ctx)
    _seated = _app(ctx).seats.enabled and _seat is not _app(ctx).seats.default
    result = await _logged(
        ctx, "end_turn", {}, lambda: gs.end_turn(_seat if _seated else None)
    )

    # ---------------------------------------------------------------
    # Auto-recover from AI turn hangs (transparent to agent).
    # end_turn returns "HANG:{turn}:{save}|..." when AI processing is
    # stuck after ~39s of polling with no blockers found.
    # Recovery: restart_and_load the MCP autosave, reconnect, retry
    # up to _MAX_HANG_RETRIES times with escalating waits.
    #
    # Never in handoff mode: recovery kills and reloads the game, which would
    # throw away the human's session and every other agent's turn. The operator
    # decides what to do about a hang in a shared game.
    # ---------------------------------------------------------------
    _MAX_HANG_RETRIES = 3
    _HANG_EXTRA_WAIT = [0, 15, 30]  # extra seconds before retry per attempt

    if result.startswith("HANG:") and _seated:
        log.warning("HANG in handoff mode — not auto-restarting a shared game")
        _, _hang_turn, _hang_save = result.split("|", 1)[0].split(":")
        return (
            f"The game stopped responding at turn {_hang_turn} and did not "
            "hand the turn on. Auto-recovery is disabled in a shared "
            "human-vs-agent game because it would reload the game for "
            "everyone. Tell the human — they can reload "
            f"'{_hang_save}' from the game's own menu."
        )

    if result.startswith("HANG:") and not gs._hang_retry_active:
        parts = result.split("|", 1)
        hang_info = parts[
            0
        ]  # "HANG:57:AutoSave_0057" (Linux) or "HANG:57:0_MCP_0057" (Windows)
        _, hang_turn, hang_save = hang_info.split(":")
        hang_turn_int = int(hang_turn)

        # Check save file exists before attempting recovery.
        # MCP saves (0_MCP_*) are in SINGLE_SAVE_DIR; game autosaves
        # (AutoSave_*) are in SAVE_DIR (auto/ subdir).
        save_path = os.path.join(game_launcher.SINGLE_SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            save_path = os.path.join(game_launcher.SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            log.error(
                "HANG RECOVERY: Save file %s not found, cannot auto-recover",
                save_path,
            )
            # Fall through — return the hang message to agent
        else:
            identity_before = gs._game_identity
            gs._hang_retry_active = True
            try:
                for attempt in range(1, _MAX_HANG_RETRIES + 1):
                    extra_wait = _HANG_EXTRA_WAIT[
                        min(attempt - 1, len(_HANG_EXTRA_WAIT) - 1)
                    ]
                    log.warning(
                        "HANG RECOVERY: attempt %d/%d for T%s "
                        "(extra wait: %ds, save: %s)",
                        attempt,
                        _MAX_HANG_RETRIES,
                        hang_turn,
                        extra_wait,
                        hang_save,
                    )

                    # Step 1: Kill + relaunch + OCR load
                    restart_result = await game_launcher.restart_and_load(hang_save)
                    log.info("HANG RECOVERY: restart_and_load: %s", restart_result)

                    # Step 2: Reconnect
                    conn = gs.conn
                    reconnected = False
                    for rc_attempt in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                reconnected = True
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)

                    if not reconnected:
                        log.error(
                            "HANG RECOVERY: could not reconnect (attempt %d)",
                            attempt,
                        )
                        continue  # try the whole cycle again

                    # Step 2b: Verify correct game loaded (with retries).
                    # The game may still be on the leader screen after
                    # restart — Lua states exist but game APIs aren't
                    # fully initialized. Retry the check rather than
                    # restarting the entire recovery cycle.
                    if identity_before is not None:
                        identity_ok = False
                        for id_check in range(3):
                            try:
                                actual = await gs.get_game_identity()
                                if actual == identity_before:
                                    identity_ok = True
                                    break
                                log.warning(
                                    "HANG RECOVERY: wrong identity %s vs %s "
                                    "(check %d/3)",
                                    actual,
                                    identity_before,
                                    id_check + 1,
                                )
                            except Exception:
                                log.debug(
                                    "HANG RECOVERY: identity check failed "
                                    "(check %d/3), waiting...",
                                    id_check + 1,
                                )
                            await asyncio.sleep(5)
                        if not identity_ok:
                            log.warning(
                                "HANG RECOVERY: identity check inconclusive "
                                "— proceeding anyway (attempt %d)",
                                attempt,
                            )

                    # Step 3: Reset state flags
                    gs._pending_end_turn = False
                    gs._pending_end_turn_from = None
                    gs._end_turn_blocked = False

                    # Step 4: Extra wait to give AI more processing time
                    if extra_wait > 0:
                        log.info(
                            "HANG RECOVERY: waiting %ds before retry...",
                            extra_wait,
                        )
                        await asyncio.sleep(extra_wait)

                    # Step 5: Retry end_turn
                    log.info(
                        "HANG RECOVERY: retrying end_turn for T%s...",
                        hang_turn,
                    )
                    result = await gs.end_turn()
                    log.info("HANG RECOVERY: retry result: %s", result[:200])

                    if not result.startswith("HANG:"):
                        log.info(
                            "HANG RECOVERY: T%s resolved on attempt %d",
                            hang_turn,
                            attempt,
                        )
                        break  # success — fall through to normal processing
                else:
                    # All retries exhausted
                    earlier = max(1, hang_turn_int - 3)
                    log.error(
                        "HANG RECOVERY: all %d attempts failed for T%s",
                        _MAX_HANG_RETRIES,
                        hang_turn,
                    )
                    return (
                        f"AI turn hung at T{hang_turn} after "
                        f"{_MAX_HANG_RETRIES} automatic restart attempts "
                        f"with escalating waits. The hang may be "
                        f"probabilistic — another attempt could work. "
                        f"Try restart_and_load('{hang_save.replace(hang_turn, str(earlier))}') "
                        f"to skip back a few turns."
                    )
            except Exception:
                log.error("HANG RECOVERY: failed", exc_info=True)
                return (
                    f"HANG RECOVERY FAILED at T{hang_turn}: "
                    f"restart_and_load threw an exception. "
                    f"Try restart_and_load('{hang_save}') manually."
                )
            finally:
                gs._hang_retry_active = False

    # Clear stale camera events on successful turn advance
    turn_advanced = (
        "->" in result and "Cannot end turn" not in result and "Error" not in result
    )
    if turn_advanced:
        _get_camera(ctx).clear()
        gs._end_turn_blocked = False
        # Update logger/spatial turn from result ("Turn X -> Y")
        m = re.search(r"Turn \d+ -> (\d+)", result)
        if m:
            new_turn = int(m.group(1))
            _get_logger(ctx).set_turn(new_turn)
            _get_spatial(ctx).set_turn(new_turn)
            heartbeat.write("playing", turn=new_turn)
        # Map capture — record terrain (first turn) + ownership delta
        if _diary_civ_type and _diary_seed:
            try:
                mc = _get_map_capture(ctx)
                mc.bind_game(_diary_civ_type, _diary_seed)
                capture_turn = new_turn if m else _diary_turn
                await mc.capture(gs.conn, capture_turn)
            except Exception:
                log.debug("Map capture failed", exc_info=True)
    elif "Turn paused" in result or "World Congress fires" in result:
        gs._end_turn_blocked = True
        # Safety net: if WC blocker fires repeatedly on the same turn,
        # auto-submit to break infinite loops (agent used wrong voting tool)
        if "World Congress fires" in result:
            wc_turn = getattr(gs, "_wc_blocker_turn", -1)
            wc_count = getattr(gs, "_wc_blocker_count", 0)
            current = _diary_turn or 0
            if wc_turn == current:
                gs._wc_blocker_count = wc_count + 1
                if gs._wc_blocker_count >= 3:
                    log.warning(
                        "WC blocker repeated %d times on T%d — auto-submitting",
                        gs._wc_blocker_count,
                        current,
                    )
                    try:
                        await gs.submit_congress()
                    except Exception:
                        log.debug("WC auto-submit failed", exc_info=True)
            else:
                gs._wc_blocker_turn = current
                gs._wc_blocker_count = 1

    # Log structured game-over entry.
    # Also check on HANG — the game may have ended during AI processing but
    # InGame Lua froze, so end_turn returned HANG instead of GAME OVER.
    # The GameCore fallback in check_game_over can detect this.
    if "HANG:" in result and "GAME OVER" not in result:
        try:
            hang_check = await gs.check_game_over()
            if hang_check is not None:
                gs._last_game_over = hang_check
                vtype = (
                    hang_check.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                if hang_check.is_defeat:
                    result = (
                        f"GAME OVER — DEFEAT. {hang_check.winner_leader} "
                        f"of {hang_check.winner_name} won a {vtype} victory. "
                        f"The game has ended. No further actions are possible."
                    )
                else:
                    result = (
                        f"GAME OVER — VICTORY! You won a {vtype} victory! "
                        f"The game has ended."
                    )
        except Exception:
            log.debug("HANG game-over recheck failed", exc_info=True)

    if "GAME OVER" in result:
        heartbeat.write("finished", turn=_diary_turn or 0)
        try:
            gameover = gs._last_game_over
            if gameover is None:
                gameover = await gs.check_game_over()
            if gameover is not None:
                gs._last_game_over = None
                vtype = (
                    gameover.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                await _get_logger(ctx).log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            else:
                log.error(
                    "GAME OVER detected but no GameOverStatus available "
                    "— outcome will be missing from log"
                )
        except Exception:
            log.warning("Failed to log game-over entry", exc_info=True)

    # Arm the watchdog after first successful end_turn so it starts
    # polling for game-over independently of future tool calls.
    if "GAME OVER" not in result:
        _get_watchdog(ctx).arm()

    return result


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------


@mcp.tool()
async def update_diary(
    ctx: Context,
    next_turn_plan: str = "",
    long_term_plans: str = "",
) -> str:
    """Record your plans for the next turn and your long-term strategy.

    Call this after end_turn() and before wait_for_turn() — once per turn
    cycle. The plans you write here will appear in get_full_game_state() at
    the start of your next turn.

    Args:
        next_turn_plan: Your plan for the NEXT turn — what you intend to do
            when you get the turn back. This is overwritten each turn, so
            only the most recent entry matters. Be specific: mention unit
            movements, production choices, research targets.
        long_term_plans: Your long-term strategy — victory path, expansion
            goals, tech progression, diplomatic posture. Pass the complete
            current version when something changes. Leave empty to keep the
            existing long-term plans unchanged — the previous value persists.
    """
    gs = _get_game(ctx)
    try:
        civ_type, seed = await gs.get_game_identity()
    except Exception:
        return "Could not detect current game. Is the game running?"

    # Capture current turn for the diary row
    diary_turn = 0
    try:
        ov = await gs.get_game_overview()
        diary_turn = ov.turn
    except Exception:
        log.warning("update_diary: failed to capture overview", exc_info=True)

    run_id = _get_logger(ctx).session_id
    path = _diary_path(civ_type, seed, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    ntp = next_turn_plan.strip()
    ltp = long_term_plans.strip()

    # Inherit previous long-term plans if the agent left them empty.
    # next_turn_plan is always overwritten (empty = no plan for next turn).
    if not ltp:
        previous = _get_current_plans(path)
        ltp = previous.get("long_term_plans", "")

    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "turn": diary_turn,
        "next_turn_plan": ntp,
        "long_term_plans": ltp,
        "timestamp": ts,
    }

    try:
        # Append to JSONL
        with open(path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        # Emit to telemetry sinks
        await _get_logger(ctx)._emitter.emit(EVENT_DIARY_ROW, row)
        log.info("Diary updated for turn %s", diary_turn)
    except Exception:
        log.warning("update_diary: failed to write entry", exc_info=True)
        return "Error: failed to write diary entry."

    parts = [f"Diary updated for turn {diary_turn}."]
    if next_turn_plan.strip():
        parts.append(f"Next-turn plan recorded ({len(next_turn_plan)} chars).")
    if long_term_plans.strip():
        parts.append(f"Long-term plans recorded ({len(long_term_plans)} chars).")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Trade routes
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_routes(ctx: Context) -> str:
    """Get trade route capacity, active routes, and trader status.

    Shows how many routes are active vs capacity, and lists all trader
    units with their positions and whether they're idle or on a route.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_trade_routes",
        {},
        lambda: _narrate(gs.get_trade_routes, nr.narrate_trade_routes),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_destinations(ctx: Context, unit_id: int) -> str:
    """List valid trade route destinations for a trader unit.

    Args:
        unit_id: The trader's composite ID (from get_units output)

    Shows domestic and international destinations. Use unit_action
    with action='trade_route' and target_x/target_y to start a route.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536

    async def _run():
        dests = await gs.get_trade_destinations(unit_index)
        return nr.narrate_trade_destinations(dests)

    return await _logged(ctx, "get_trade_destinations", {"unit_id": unit_id}, _run)


# ---------------------------------------------------------------------------
# District advisor
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_district_advisor(ctx: Context, city_id: int, district_type: str) -> str:
    """Show best tiles to place a district with adjacency bonuses.

    Args:
        city_id: City ID (from get_cities)
        district_type: e.g. DISTRICT_CAMPUS, DISTRICT_HOLY_SITE, DISTRICT_INDUSTRIAL_ZONE

    Returns valid placement tiles ranked by adjacency bonus.
    Use set_city_production with target_x/target_y to build the district.
    """
    gs = _get_game(ctx)

    async def _run():
        result = await gs.get_district_advisor(city_id, district_type)
        if isinstance(result, str):
            return f"Error: {result}"  # propagate specific error reason
        narrated = nr.narrate_district_advisor(result, district_type)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_district_advisor",
        {"city_id": city_id, "district_type": district_type},
        _run,
    )


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_wonder_advisor(ctx: Context, city_id: int, wonder_name: str) -> str:
    """Show best tiles to place a wonder with displacement cost analysis.

    Args:
        city_id: City ID (from get_cities output)
        wonder_name: Wonder building type, e.g. BUILDING_CHICHEN_ITZA, BUILDING_ORSZAGHAZ

    Returns valid placement tiles ranked by displacement cost (lowest = best):
    tiles with no improvements or resources are preferred over productive tiles.
    Also shows terrain, feature, river/coastal status, and any resources/improvements
    that would be removed by placing the wonder there.
    Use set_city_production with target_x/target_y to build the wonder.
    """
    gs = _get_game(ctx)

    async def _run():
        placements = await gs.get_wonder_advisor(city_id, wonder_name)
        if isinstance(placements, str):
            return f"Error: {placements}"  # propagate budget/error string
        narrated = nr.narrate_wonder_advisor(placements, wonder_name)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_wonder_advisor",
        {"city_id": city_id, "wonder_name": wonder_name},
        _run,
    )


# ---------------------------------------------------------------------------
# Tile purchase tools
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_purchasable_tiles(ctx: Context, city_id: int) -> str:
    """List tiles a city can purchase with gold.

    Args:
        city_id: City ID (from get_cities)

    Shows cost, terrain, and resources for each purchasable tile.
    Tiles with luxury/strategic resources are listed first.
    """
    gs = _get_game(ctx)

    async def _run():
        tiles = await gs.get_purchasable_tiles(city_id)
        return nr.narrate_purchasable_tiles(tiles)

    return await _logged(ctx, "get_purchasable_tiles", {"city_id": city_id}, _run)


# @mcp.tool()
async def purchase_tile(ctx: Context, city_id: int, x: int, y: int) -> str:
    """Buy a tile for a city with gold.

    Args:
        city_id: City ID
        x: Tile X coordinate
        y: Tile Y coordinate

    Use get_purchasable_tiles first to see costs and options.
    """
    gs = _get_game(ctx)
    result = await _logged(
        ctx,
        "purchase_tile",
        {"city_id": city_id, "x": x, "y": y},
        lambda: gs.purchase_tile(city_id, x, y),
    )
    _get_camera(ctx).push(x, y, f"purchase tile ({x},{y})")
    return result


# ---------------------------------------------------------------------------
# Government change
# ---------------------------------------------------------------------------


# @mcp.tool()
async def change_government(ctx: Context, government_type: str) -> str:
    """Switch to a different government type.

    Args:
        government_type: e.g. GOVERNMENT_CLASSICAL_REPUBLIC, GOVERNMENT_OLIGARCHY

    Use get_policies to see current government. First switch after
    unlocking a new tier is free (no anarchy).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "change_government",
        {"government_type": government_type},
        lambda: gs.change_government(government_type),
    )


# ---------------------------------------------------------------------------
# Great People
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_great_people(ctx: Context) -> str:
    """See available Great People and recruitment progress.

    Shows which Great People are available, their recruitment cost,
    and which civilization (if any) is recruiting them.
    """
    gs = _get_game(ctx)

    async def _run():
        gp = await gs.get_great_people()
        return nr.narrate_great_people(gp)

    return await _logged(ctx, "get_great_people", {}, _run)


# @mcp.tool()
async def get_gp_advisor(ctx: Context, unit_index: int) -> str:
    """Show best cities to activate a Great Person, ranked by suitability.

    Args:
        unit_index: The Great Person unit's index (from get_units output).

    Lists all cities with the matching district (e.g., campuses for Great Scientists),
    showing which ones the GP can activate on, distance, city yield, and great work
    slot availability for cultural GPs.
    """
    gs = _get_game(ctx)

    async def _run():
        result = await gs.get_gp_advisor(unit_index)
        if result is None:
            return "Could not get GP advisor info. Is this a Great Person unit?"
        return nr.narrate_gp_advisor(result)

    return await _logged(ctx, "get_gp_advisor", {"unit": unit_index}, _run)


# @mcp.tool()
async def recruit_great_person(ctx: Context, individual_id: int) -> str:
    """Recruit a Great Person using accumulated GP points.

    Args:
        individual_id: The individual's ID (from get_great_people output, shown after ability)

    Requires enough Great Person points for that class.
    The GP spawns in your capital. Use get_great_people to check [CAN RECRUIT] status.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "recruit_great_person",
        {"id": individual_id},
        lambda: gs.recruit_great_person(individual_id),
    )


# @mcp.tool()
async def patronize_great_person(
    ctx: Context, individual_id: int, yield_type: str = "YIELD_GOLD"
) -> str:
    """Buy a Great Person instantly with gold or faith.

    Args:
        individual_id: The individual's ID (from get_great_people output)
        yield_type: YIELD_GOLD (default) or YIELD_FAITH

    Costs shown in get_great_people output under "Patronize:".
    Requires enough gold/faith to cover the cost.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "patronize_great_person",
        {"id": individual_id, "yield": yield_type},
        lambda: gs.patronize_great_person(individual_id, yield_type),
    )


# @mcp.tool()
async def reject_great_person(ctx: Context, individual_id: int) -> str:
    """Pass on a Great Person (skip to the next one in that class).

    Args:
        individual_id: The individual's ID (from get_great_people output)

    Costs faith. The next Great Person in that class becomes available.
    Use when you don't want the current GP and want to save points for a better one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "reject_great_person",
        {"id": individual_id},
        lambda: gs.reject_great_person(individual_id),
    )


# ---------------------------------------------------------------------------
# World Congress
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_world_congress(ctx: Context) -> str:
    """Get World Congress status, active resolutions, and voting options.

    Shows whether congress is in session, resolutions to vote on (with options A/B
    and possible targets), turns until next session, and your diplomatic favor.
    When in session, use queue_wc_votes to register votes before end_turn.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_world_congress()
        return nr.narrate_world_congress(status)

    return await _logged(ctx, "get_world_congress", {}, _run)


# @mcp.tool()
async def queue_wc_votes(ctx: Context, votes: str) -> str:
    """Pre-configure World Congress votes for the upcoming session.

    Args:
        votes: JSON array of vote objects, e.g.
            '[{"hash": -513644209, "option": 1, "target": 2, "votes": 5}]'
            hash = resolution type hash (from get_world_congress)
            option = 1 for A, 2 for B
            target = player ID for PlayerType resolutions (from get_world_congress
                     target list, e.g. [target=2] Portugal), or target value for
                     non-player resolutions. The handler resolves to the correct
                     0-based index at runtime.
            votes = max votes to allocate (will use as many as favor allows)

    Call this BEFORE end_turn when get_world_congress shows 0 turns until next
    session. Registers an event handler that fires during WC processing and
    casts your votes with the specified preferences.

    If you don't call this, end_turn will pause at the World Congress session
    and return control to you for interactive voting.
    """
    gs = _get_game(ctx)
    vote_list = json.loads(votes)

    async def _run():
        return await gs.queue_wc_votes(vote_list)

    return await _logged(ctx, "queue_wc_votes", {"votes": vote_list}, _run)


# ---------------------------------------------------------------------------
# Victory progress
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_victory_progress(ctx: Context) -> str:
    """Get victory condition progress for all civilizations.

    Shows progress toward Science, Domination, Culture, Religious,
    Diplomatic, and Score victories. Includes space race VP, diplomatic VP,
    tourism vs domestic tourists, religion spread, capital ownership,
    and military strength. Call every 20-30 turns to track the race.
    """
    gs = _get_game(ctx)

    async def _run():
        vp = await gs.get_victory_progress()
        return nr.narrate_victory_progress(vp)

    return await _logged(ctx, "get_victory_progress", {}, _run)


# ---------------------------------------------------------------------------
# Religion status
# ---------------------------------------------------------------------------


# @mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_spread(ctx: Context) -> str:
    """Get per-city religion breakdown across all visible cities.

    Shows which religion is majority in each city, follower counts,
    and which religions are closest to religious victory.
    """
    gs = _get_game(ctx)

    async def _run():
        rs = await gs.get_religion_status()
        return nr.narrate_religion_status(rs)

    return await _logged(ctx, "get_religion_spread", {}, _run)


# ---------------------------------------------------------------------------
# City yield focus
# ---------------------------------------------------------------------------


# @mcp.tool()
async def set_city_focus(ctx: Context, city_id: int, focus: str) -> str:
    """Set a city's citizen yield priority.

    Args:
        city_id: City ID
        focus: One of: food, production, gold, science, culture, faith, default
               'default' clears all focus settings.

    Cities automatically assign citizens to tiles. This biases the AI
    toward the chosen yield type when assigning new citizens.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_city_focus",
        {"city_id": city_id, "focus": focus},
        lambda: gs.set_city_focus(city_id, focus),
    )


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def dismiss_popup(ctx: Context) -> str:
    """Dismiss any blocking popup in the game UI.

    Call this if you suspect a popup (e.g. historic moment, boost notification)
    is blocking interaction.
    """
    gs = _get_game(ctx)
    return await _logged(ctx, "dismiss_popup", {}, gs.dismiss_popup)


@mcp.tool(annotations={"destructiveHint": True})
async def run_lua(ctx: Context, code: str, context: str = "gamecore") -> str:
    """Run arbitrary Lua code in the game. Advanced escape hatch — prefer built-in tools.

    Args:
        code: Lua code to execute. Use print() for output, end with print("---END---").
        context: "gamecore" (default) for read-only state queries.
                 "ingame" for commands and UI-dependent queries.

    Context differences:
      gamecore: Players[], GameInfo.*, Map.*, Game.* — safe read-only access.
                CANNOT use: UI.*, UnitManager.*, CityManager.*, notifications.
      ingame:   All APIs including UI.*, UnitManager.*, CityManager.*.
                Use for: moving units, setting research, diplomacy actions.

    Always use print() for output (not return).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "run_lua", {"context": context}, lambda: gs.execute_lua(code, context)
    )


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def list_saves(ctx: Context) -> str:
    """List available save files (normal, autosave).

    Returns indexed list of saves. Use load_save(save_index=N) to load one.
    Call this before load_save to see what's available.
    """
    gs = _get_game(ctx)
    return await _logged(ctx, "list_saves", {}, gs.list_saves)


@mcp.tool(annotations={"destructiveHint": True})
async def load_save(ctx: Context, save_index: int) -> str:
    """Load a save file by index from the most recent list_saves() result.

    Args:
        save_index: Index number from list_saves output (1-based)

    The game will reload entirely. Wait ~10 seconds after calling this,
    then use get_game_overview to verify the loaded state.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "load_save", {"save_index": save_index}, lambda: gs.load_save(save_index)
    )


@mcp.tool(annotations={"destructiveHint": True})
async def load_game_save(ctx: Context, save_name: str) -> str:
    """Load a save file by name. No need to call list_saves first.

    Args:
        save_name: Save name without extension (e.g. "0_MCP_0079",
                   "0A_GROUND_CONTROL", "AutoSave_0221", "quicksave").

    Tries Lua-based loading first (fast, ~5s). If the save isn't found
    via Lua (common for autosaves/quicksaves), falls back to OCR menu
    navigation (~90s) after verifying the file exists on disk.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "load_game_save",
        {"save_name": save_name},
        lambda: gs.load_game_save(save_name),
    )


# ---------------------------------------------------------------------------
# Game Lifecycle (kill / launch / load from menu)
# ---------------------------------------------------------------------------
# These tools do NOT require a FireTuner connection — they manage the game
# process itself. Hardcoded to Civ 6 only (no arbitrary system commands).


@mcp.tool(annotations={"destructiveHint": True})
async def kill_game(ctx: Context) -> str:
    """Kill the Civ 6 game process and wait for Steam to deregister.

    Only kills Civ 6 processes. Waits ~10 seconds for Steam to deregister
    so the game can be relaunched cleanly.
    """
    blocked = _forbidden_in_handoff(ctx, "kill_game")
    if blocked:
        return blocked
    return await game_launcher.kill_game()


@mcp.tool(annotations={"destructiveHint": True})
async def launch_game(ctx: Context) -> str:
    """Launch Civ 6 via Steam.

    Starts the game and waits for the process to appear (~15-30 seconds).
    The game will be at the main menu after launch — use load_save or
    restart_and_load to load a specific save.

    NOTE: FireTuner connection is NOT available at the main menu.
    Only in-game MCP tools work after a save is loaded.
    """
    blocked = _forbidden_in_handoff(ctx, "launch_game")
    if blocked:
        return blocked
    return await game_launcher.launch_game()


@mcp.tool(annotations={"destructiveHint": True})
async def load_save_from_menu(ctx: Context, save_name: str | None = None) -> str:
    """Navigate the main menu to load a save via OCR-guided clicking.

    Args:
        save_name: Autosave name (e.g. "AutoSave_0221"). If not provided,
                   loads the most recent autosave.

    Requires the game to be running and at the main menu. Uses macOS Vision
    OCR to find and click menu elements. Takes 30-90 seconds.

    After loading, wait ~10 seconds then call get_game_overview to verify.

    Requires pyobjc: uv pip install 'civ6-mcp[launcher]'
    """
    blocked = _forbidden_in_handoff(ctx, "load_save_from_menu")
    if blocked:
        return blocked
    return await game_launcher.load_save_from_menu(save_name)


@mcp.tool(annotations={"destructiveHint": True})
async def restart_and_load(ctx: Context, save_name: str | None = None) -> str:
    """Full game recovery: kill, relaunch, and load a save.

    Args:
        save_name: Autosave name (e.g. "AutoSave_0221"). If not provided,
                   loads the most recent autosave.

    This is the recommended tool for recovering from game hangs (e.g. AI turn
    processing stuck in infinite loop). Takes 60-120 seconds total:
    1. Kills the game process
    2. Waits for Steam to deregister (~10s)
    3. Relaunches via Steam (~15-30s for process start + main menu)
    4. Navigates menus via OCR to load the save (~30-60s)

    After completion, wait ~10 seconds then call get_game_overview to verify.
    """
    blocked = _forbidden_in_handoff(ctx, "restart_and_load")
    if blocked:
        return blocked
    gs = _get_game(ctx)
    identity_before = gs._game_identity

    result = await game_launcher.restart_and_load(save_name)

    # Reconnect and verify correct game loaded
    conn = gs.conn
    for attempt in range(30):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                break
        except ConnectionError:
            pass
        await asyncio.sleep(1)

    if conn.gamecore_index is not None and identity_before is not None:
        try:
            actual = await gs.get_game_identity()
            if actual != identity_before:
                log.warning(
                    "restart_and_load: wrong game loaded "
                    "(expected %s, got %s) — retrying",
                    identity_before,
                    actual,
                )
                result2 = await game_launcher.restart_and_load(save_name)
                for attempt in range(30):
                    try:
                        await conn.reconnect()
                        if conn.gamecore_index is not None:
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
                try:
                    actual2 = await gs.get_game_identity()
                    if actual2 != identity_before:
                        return (
                            f"{result2} | WARNING: Wrong game loaded "
                            f"(expected {identity_before[0]}, "
                            f"got {actual2[0]}). Manual recovery needed."
                        )
                except Exception:
                    pass
                return f"{result2} | Reloaded after wrong-game detection."
        except Exception:
            log.debug("Post-load identity check failed", exc_info=True)

    return result


async def _narrate(
    query_fn: Callable[[], Awaitable[Any]], narrate_fn: Callable[..., str]
) -> str:
    """Helper: call a query function then narrate the result."""
    data = await query_fn()
    return narrate_fn(data)


# Computed once, after every @mcp.tool has been registered above.
_WRITE_TOOLS: frozenset[str] = _write_tool_names()

# Tools that only make sense when several agents share one game.
_HANDOFF_TOOLS = (
    "get_seats",
    "claim_seat",
    "release_seat",
    "get_turn_status",
    "wait_for_turn",
    "reinstall_handoff",
)


def main():
    """Entry point for the MCP server."""
    import signal

    logging.basicConfig(level=logging.INFO)

    # Remap SIGTERM → SIGINT so asyncio's existing SIGINT handler triggers a
    # graceful shutdown (cancels all tasks → lifespan finally block runs →
    # conn.disconnect() closes the FireTuner TCP connection cleanly).
    # Without this, SIGTERM kills the process immediately, leaving the game
    # with an abrupt TCP RST which can cause it to crash.
    # SIGTERM is not available on Windows, so skip the remap there.
    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM, lambda sig, frame: os.kill(os.getpid(), signal.SIGINT)
        )

    if os.environ.get("CIV_MCP_DISABLE_LUA"):
        mcp._tool_manager.remove_tool("run_lua")

    if not HANDOFF_CONFIG.enabled:
        for name in _HANDOFF_TOOLS:
            mcp._tool_manager.remove_tool(name)

    # stdio serves exactly one client. A human-vs-agent game needs several
    # agents on one connection to the game, so that setup runs over HTTP:
    # the FireTuner protocol broadcasts print() output to every connected
    # client, so two server processes would each parse the other's replies.
    transport = os.environ.get("CIV_MCP_TRANSPORT", "").strip() or (
        "streamable-http" if HANDOFF_CONFIG.enabled else "stdio"
    )
    if transport != "stdio":
        mcp.settings.host = os.environ.get("CIV_MCP_HTTP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("CIV_MCP_HTTP_PORT", "8765"))
        log.info(
            "Serving MCP over %s at http://%s:%d/mcp",
            transport,
            mcp.settings.host,
            mcp.settings.port,
        )

    mcp.run(transport=transport)
