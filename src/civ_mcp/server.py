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
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

from civ_mcp import game_launcher, handoff, heartbeat, seats as seats_mod
from civ_mcp.game_over_watchdog import GameOverWatchdog
from civ_mcp import narrate as nr
from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.diary import (
    game_keyed_diary_path as _diary_path,
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
from civ_mcp.deal_mailbox import DealMailbox, PendingProposal, SerializedDealItem
from civ_mcp.diplo_mailbox import DiploMailbox, PendingDiploProposal
from civ_mcp.message_mailbox import Message, MessageMailbox
from civ_mcp.lua.diplomacy import RESPONSEABLE_DIPLO_ACTIONS
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
    # Deal mailbox — intercepts trades with managed civs so the built-in AI
    # doesn't auto-answer them.
    mailbox: DealMailbox | None = None
    # Diplomacy mailbox — same idea for response-able diplo actions
    # (friendship/delegation/embassy) to managed civs. See diplo_mailbox.py.
    diplo_mailbox: DiploMailbox | None = None
    # Message mailbox — free-text chat between managed players and the human.
    # See message_mailbox.py and docs/managed-player-messaging.md.
    message_mailbox: MessageMailbox | None = None


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

    # Deal mailbox — intercepts trades with managed civs.
    mailbox = DealMailbox()
    # Diplomacy mailbox — intercepts response-able diplo actions to managed civs.
    diplo_mailbox = DiploMailbox()
    # Message mailbox — free-text chat between managed players and the human.
    message_mailbox = MessageMailbox()
    _deal_notify_task: asyncio.Task | None = None
    if cfg.enabled:
        # Install the DiplomacyDealView shim (idempotent).
        try:
            shim_status = await handoff.install_deal_shim(conn, cfg.managed_ids)
            log.info("Deal shim: %s", shim_status)
        except Exception:
            log.warning("Deal shim install failed", exc_info=True)

        # Install the chat shim (ChatPanel state) and unhide the chat panel
        # (WorldTracker state). Both are idempotent. The shim reroutes the
        # human's typed messages to the message mailbox; the unhide force-shows
        # the native chat panel in single-player.
        try:
            chat_status = await handoff.install_chat_shim(conn)
            log.info("Chat shim: %s", chat_status)
        except Exception:
            log.warning("Chat shim install failed", exc_info=True)
        try:
            await conn.execute_in_named_state(
                handoff.WORLDTRACKER_STATE,
                handoff.build_unhide_chat_lua(),
            )
        except Exception:
            log.warning("Chat panel unhide failed", exc_info=True)

        # Register the notification click handler in InGame.
        try:
            note_lines = await conn.execute_write(
                handoff.build_notification_handler_lua(cfg.managed_ids),
                perspective=False,
            )
            note_status = next(
                (l for l in note_lines if l.startswith("NOTE_HANDLER|")), "unknown"
            )
            log.info("Notification handler: %s", note_status)
        except Exception:
            log.warning("Notification handler install failed", exc_info=True)

        # Register the deal event callback.
        _notified_proposals: set[str] = set()

        def _on_deal_event(event_type: str, data: dict) -> None:
            if event_type == "proposed":
                # Human proposed a deal to a managed civ via the native UI.
                _handle_human_deal_proposed(mailbox, data, cfg)
            elif event_type == "click":
                # Human clicked a deal notification.
                asyncio.ensure_future(
                    _handle_deal_notification_click(
                        conn, mailbox, data, cfg, gs
                    )
                )
            elif event_type == "trace":
                log.info("DEAL TRACE: %s", data.get("msg", ""))
            elif event_type == "health":
                if not data.get("ok"):
                    log.warning("Deal shim health check failed — may be missing")

        conn.add_deal_callback(_on_deal_event)

        # Register the chat event callback (same drain channel as deal events).
        def _on_chat_event(event_type: str, data: dict) -> None:
            if event_type == "chat_send":
                # Human typed a message in the native chat panel.
                asyncio.ensure_future(
                    _handle_human_chat(conn, message_mailbox, data, cfg, gs)
                )

        conn.add_deal_callback(_on_chat_event)

        # Start the background deal monitor.
        await conn.start_deal_monitor()

        # Background task: notify the human about pending deals at turn start.
        _deal_notify_task = asyncio.create_task(
            _deal_notify_loop(
                conn, mailbox, cfg, _notified_proposals
            ),
            name="deal-notify",
        )

        # Re-arm the deal shim and notification handler on every keeper cycle
        # (UI contexts are rebuilt on save load and lose their wrappers).
        async def _rearm_deal_shim():
            await handoff.install_deal_shim(conn, cfg.managed_ids)

        async def _rearm_note_handler():
            try:
                lines = await conn.execute_write(
                    handoff.build_notification_handler_lua(cfg.managed_ids),
                    perspective=False,
                )
                status = next(
                    (l for l in lines if l.startswith("NOTE_HANDLER|")),
                    "unknown",
                )
                log.debug("Notification handler re-armed: %s", status)
            except Exception:
                log.debug("Notification handler re-arm failed", exc_info=True)

        keeper.add_post_install_hook(_rearm_deal_shim)
        keeper.add_post_install_hook(_rearm_note_handler)

        # Re-arm the chat shim and chat-panel unhide on every keeper cycle
        # (the ChatPanel and WorldTracker contexts are rebuilt on save load).
        async def _rearm_chat_shim():
            await handoff.install_chat_shim(conn)

        async def _rearm_chat_unhide():
            try:
                await conn.execute_in_named_state(
                    handoff.WORLDTRACKER_STATE,
                    handoff.build_unhide_chat_lua(),
                )
            except Exception:
                log.debug("Chat unhide re-arm failed", exc_info=True)

        keeper.add_post_install_hook(_rearm_chat_shim)
        keeper.add_post_install_hook(_rearm_chat_unhide)

        # Stash for cleanup.

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
            mailbox=mailbox,
            diplo_mailbox=diplo_mailbox,
            message_mailbox=message_mailbox,
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
        if _deal_notify_task is not None:
            _deal_notify_task.cancel()
            try:
                await _deal_notify_task
            except asyncio.CancelledError:
                pass
        await conn.stop_deal_monitor()
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
    "Read game state and issue commands to a running Civ 6 game."
    "Call get_agent_reference first to orient yourself."
    "Then call get_full_game_state to get the info needed to make decisions."
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

def _load_instructions() -> str:
    """Load INSTRUCTIONS.md (the packaged agent reference) for the agent.
    """
    path = Path(__file__).resolve().parent / "INSTRUCTIONS.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    log.warning("INSTRUCTIONS.md not found; server instructions will be empty.")
    return ""

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
        "list_saves",
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


# ---------------------------------------------------------------------------
# Deal mailbox helpers
# ---------------------------------------------------------------------------


def _get_mailbox(ctx: Context) -> DealMailbox | None:
    return _app(ctx).mailbox


def _is_managed_target(player_id: int, cfg: HandoffConfig) -> bool:
    """True if *player_id* is an agent-managed civ (not the human)."""
    return player_id in cfg.managed_ids


def _handle_human_deal_proposed(
    mailbox: DealMailbox, data: dict, cfg: HandoffConfig
) -> None:
    """Callback: human proposed a deal to a managed civ via the native UI."""
    from_pid = data.get("from", -1)
    to_pid = data.get("to", -1)
    items_data = data.get("items", [])

    items = [
        SerializedDealItem(
            item_type=it.get("item_type", "UNKNOWN"),
            from_player_id=it.get("from", -1),
            amount=it.get("amount", 0),
            duration=it.get("duration", 0),
            value_type=it.get("value", -1),
            subtype=it.get("sub", -1),
        )
        for it in items_data
    ]

    # Split items by who provides them.
    # Items FROM the proposer are what the proposer OFFERS.
    # Items FROM the target are what the proposer REQUESTS.
    proposer_items = [i for i in items if i.from_player_id == from_pid]
    target_items = [i for i in items if i.from_player_id == to_pid]

    proposal = PendingProposal(
        proposal_id="",
        from_player=from_pid,
        to_player=to_pid,
        items_from_proposer=proposer_items,
        items_from_target=target_items,
        turn_proposed=0,
        proposed_by="human",
    )
    proposal_id = mailbox.propose(proposal)
    log.info(
        "Deal proposed: human P%d → managed P%d (%d items) [%s]",
        from_pid,
        to_pid,
        len(items),
        proposal_id,
    )

async def _handle_deal_notification_click(
    conn: GameConnection,
    mailbox: DealMailbox,
    data: dict,
    cfg: HandoffConfig,
    gs: GameState,
) -> None:
    """Callback: human clicked a deal notification in-game.

    Injects the proposal into the OUTGOING deal, opens a MAKE_DEAL session,
    and forces Accept/Refuse buttons so the human can respond natively.
    """
    proposal_id = data.get("proposal_id", "")
    proposal = mailbox.get(proposal_id)
    if proposal is None:
        log.warning("Notification click for unknown proposal %s", proposal_id)
        return

    human_pid = cfg.human_id
    agent_pid = proposal.from_player

    # 1. Inject deal items into OUTGOING working deal.
    try:
        await conn.execute_write(
            handoff.build_inject_deal_lua(human_pid, agent_pid, proposal),
            perspective=False,
        )
    except Exception:
        log.warning("Deal injection failed", exc_info=True)
        return

    # 2. Open the MAKE_DEAL session.
    try:
        await conn.execute_write(
            handoff.build_present_deal_lua(human_pid, agent_pid),
            perspective=False,
        )
    except Exception:
        log.warning("Deal session open failed", exc_info=True)
        return

    # 3. Force Accept/Refuse buttons in the deal view.
    try:
        await conn.execute_in_named_state(
            handoff.DEAL_SHIM_STATE,
            handoff.build_force_deal_buttons_lua(agent_pid, proposal_id),
        )
    except Exception:
        log.warning("Deal button force failed", exc_info=True)

    log.info("Presented proposal %s to human on native deal screen", proposal_id)


async def _deal_notify_loop(
    conn: GameConnection,
    mailbox: DealMailbox,
    cfg: HandoffConfig,
    notified: set,
    poll_interval: float = 5.0,
) -> None:
    """Background loop: send deal notifications when the human gets the slot."""
    previous_local: int | None = None
    while True:
        try:
            own = await handoff.try_ownership(conn)
            if own.local_player == cfg.human_id and own.local_player != previous_local:
                # Human just got the slot — check for pending deals.
                await _send_deal_notifications(conn, mailbox, cfg, notified)
            previous_local = own.local_player
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Deal notify loop error", exc_info=True)
        await asyncio.sleep(poll_interval)


async def _send_deal_notifications(
    conn: GameConnection,
    mailbox: DealMailbox,
    cfg: HandoffConfig,
    notified: set,
) -> None:
    """Send in-game notifications for pending proposals the human hasn't seen."""
    human_pid = cfg.human_id
    pending = mailbox.get_pending_for(human_pid)
    for proposal in pending:
        if proposal.proposal_id in notified:
            continue
        # Build a human-readable summary.
        parts: list[str] = []
        for item in proposal.items_from_proposer:
            if item.item_type == "GOLD":
                if item.duration > 0:
                    parts.append(f"{item.amount} GPT")
                else:
                    parts.append(f"{item.amount} Gold")
            elif item.item_type == "RESOURCE":
                parts.append("Resources")
            elif item.item_type == "FAVOR":
                parts.append(f"{item.amount} Favor")
            elif item.item_type == "AGREEMENT":
                parts.append("Agreement")
            elif item.item_type == "CITY":
                parts.append("City")
        offer_str = ", ".join(parts) if parts else "items"

        # Get agent civ name.
        try:
            roster = await handoff.get_roster(conn, (proposal.from_player,))
            _, leader, _ = roster.get(
                proposal.from_player, ("?", "Unknown", True)
            )
        except Exception:
            leader = f"P{proposal.from_player}"

        summary = f"{leader} proposes: {offer_str}. Click to review."

        try:
            await conn.execute_write(
                handoff.build_send_deal_notification_lua(
                    human_pid,
                    proposal.from_player,
                    proposal.proposal_id,
                    summary,
                ),
                perspective=False,
            )
            notified.add(proposal.proposal_id)
            log.info("Sent notification for proposal %s", proposal.proposal_id)
        except Exception:
            log.debug(
                "Failed to send notification for proposal %s",
                proposal.proposal_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Managed-player messaging (chat)
# ---------------------------------------------------------------------------


async def _current_turn(conn: GameConnection) -> int:
    """Fetch the current game turn (one round-trip). Chat is infrequent."""
    try:
        lines = await conn.execute_read(
            "print(tostring(Game.GetCurrentGameTurn())) print('---END---')",
            perspective=False,
        )
        for line in lines:
            line = line.strip()
            if line and not line.startswith("---"):
                try:
                    return int(float(line))
                except ValueError:
                    continue
    except Exception:
        log.debug("current turn fetch failed", exc_info=True)
    return 0


async def _send_message(app, seat, params: dict) -> str:
    """Post a chat message from the calling seat to a target player.

    - Target is a managed civ: file in the mailbox only (the target agent reads
      it in ``get_full_game_state`` next turn). No UI action.
    - Target is the human: file in the mailbox AND render into the human's
      native chat panel via ``OnChat`` in the ChatPanel state.
    - Target is an unmanaged (built-in AI) civ: file for the log, but no agent
      reads it.
    """
    mb = app.message_mailbox
    if mb is None:
        return "Error: message mailbox not available"
    target = params.get("other_player_id", -1)
    text = params.get("text", "")
    if not isinstance(target, int) or target < 0 or not text:
        return "Error: other_player_id (int) and non-empty text are required"
    agent_pid = seat.player_id
    cfg = app.handoff_config
    conn = app.game.conn
    turn = await _current_turn(conn)

    mb.post(Message(
        from_player=agent_pid,
        to_player=target,
        text=text,
        turn=turn,
        direction="out",
    ))

    if target == cfg.human_id:
        try:
            await conn.execute_in_named_state(
                handoff.CHAT_SHIM_STATE,
                handoff.build_send_chat_message_lua(agent_pid, target, text),
            )
        except Exception:
            log.warning("Chat message push to human failed", exc_info=True)
            return f"Posted to mailbox but UI push failed: {text[:60]!r}"
        # Record last sender so the human's reply routes back to this agent.
        mb.set_last_inbound_sender(target, agent_pid)
        return f"Message sent to human P{target}: {text[:60]!r}"

    if target in cfg.managed_ids:
        return f"Message sent to P{target} (managed): {text[:60]!r}"

    return (
        f"Message logged to P{target} (unmanaged AI — no agent reads it): "
        f"{text[:60]!r}"
    )


async def _handle_human_chat(
    conn: GameConnection,
    mb: MessageMailbox,
    data: dict,
    cfg: HandoffConfig,
    gs: GameState,
) -> None:
    """Human typed a message in the native chat panel — route to the mailbox.

    The recipient is resolved by :func:`_resolve_chat_recipient` (which may
    strip a leader-name prefix from ``data["text"]``). If no recipient can be
    resolved, a hint is echoed back to the human's chat log.
    """
    try:
        human_pid = data.get("from", cfg.human_id)
        text = data.get("text", "")
        if not text:
            return
        target = await _resolve_chat_recipient(conn, mb, data, cfg)
        log.info(
            "Human chat: from P%d, text=%r, resolved target=%s",
            human_pid, text, target,
        )
        if target is None:
            try:
                await conn.execute_in_named_state(
                    handoff.CHAT_SHIM_STATE,
                    handoff.build_send_chat_message_lua(
                        human_pid,
                        human_pid,
                        "(No recipient resolved. Address a leader by name "
                        "or reply after they message you.)",
                    ),
                )
            except Exception:
                log.warning("Chat hint echo failed", exc_info=True)
            return
        final_text = data.get("text", text)
        turn = await _current_turn(conn)
        mb.post(Message(
            from_player=human_pid,
            to_player=target,
            text=final_text,
            turn=turn,
            direction="in",
        ))
    except Exception:
        log.warning("_handle_human_chat failed", exc_info=True)


async def _resolve_chat_recipient(
    conn: GameConnection,
    mb: MessageMailbox,
    data: dict,
    cfg: HandoffConfig,
) -> int | None:
    """Decide which managed civ a human's chat message is addressed to.

    The native chat pulldown cannot target managed civs (they are AI to the
    engine during another player's turn), so routing is decided here, in
    priority order:

    1. Explicit whisper ``targetID`` that is a managed civ.
    2. A leader/civ name prefix in the text (stripped from the stored text).
    3. Reply to last sender (most recent managed civ that messaged this human).
    4. ``None`` — caller echoes a hint.
    """
    human_pid = cfg.human_id

    # 1. Explicit whisper target that is a managed civ.
    target_id = data.get("to", -1)
    if isinstance(target_id, int) and target_id in cfg.managed_ids:
        return target_id

    text = data.get("text", "")

    # 2. Name-in-text prefix. Resolve via the roster: {pid: (civ, leader, alive)}.
    roster = await handoff.get_roster(conn, cfg.managed_ids)
    if roster:
        # Longest leader-name-first so longer names win prefix matches.
        candidates = sorted(roster.items(), key=lambda kv: -len(kv[1][1]))
        for pid, (civ, leader, _alive) in candidates:
            if pid == human_pid:
                continue
            for name in (leader, civ):
                if name and name != "?" and text.lower().startswith(name.lower()):
                    # Strip the matched prefix from the stored text.
                    data["text"] = text[len(name):].lstrip(" :,-")
                    return pid

    # 3. Reply to last sender.
    last = mb.last_inbound_sender(human_pid)
    if last is not None and last in cfg.managed_ids:
        return last

    return None


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

@mcp.tool(annotations={"readOnlyHint": True})
async def get_agent_reference(ctx: Context) -> str:
    """Get the agent reference instructions.
    Describes the game rules, game concepts and how to use the other tools.
    """
    return _load_instructions()

# ---------------------------------------------------------------------------
# Unified tools — get_full_game_state and execute_commands
# ---------------------------------------------------------------------------
# These two tools consolidate ~70 individual query/action tools into two.
# The code for the original tools is preserved in game_state.py and the
# lua/ modules — they're just not exposed as individual MCP tools.


@mcp.tool(
    annotations={"readOnlyHint": True},
    meta={"anthropic/maxResultSizeChars": 500000},
)
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
        app = _app(ctx)
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

        # The available governors section is to verbose to appear here, there is
        # a separate tool to get the governors available to appoint.
        state.governors.available_to_appoint = []
        text = narrate_full_state(state, app.handoff_config.managed_ids)

        # Prepend the deferred post-turn report (snapshot diff, threats,
        # warnings) stashed when this seat ended its last turn. Built once —
        # on the first get_full_game_state call after the seat is back on the
        # clock — then consumed, so subsequent calls this turn don't re-diff
        # against a stale baseline. Only built when we actually hold the
        # local-player slot; otherwise the snapshot is mid-round and querying
        # stalls the AI processing the turn.
        try:
            seat = _get_seat(ctx)
            pending = seat.pending_report
            if pending is not None:
                own = await handoff.try_ownership(gs.conn)
                if own.local_player == seat.player_id:
                    # Consume before building so a build failure can't loop.
                    seat.pending_report = None
                    from civ_mcp.end_turn import build_post_turn_report

                    try:
                        report = await build_post_turn_report(
                            gs,
                            pending.snapshot,
                            pending.turn_before,
                            own.turn,
                            pending.threats_before,
                        )
                        text = (
                            "\n=== TURN ROLLOVER REPORT ===\n" + report + "\n\n" + text
                        )
                    except Exception:
                        log.warning(
                            "Deferred turn report failed", exc_info=True
                        )
                        text = (
                            "=== TURN REPORT ===\n"
                            "Turn report failed to build; query state "
                            "directly.\n\n" + text
                        )
        except Exception:
            log.debug(
                "Failed to prepend deferred turn report", exc_info=True
            )

        # Append diary plans (long-term + next-turn) from the JSONL file
        try:
            civ_type, seed = await gs.get_game_identity()
            path = _diary_path(civ_type, seed)
            plans = _get_current_plans(path)
            ntp = plans.get("next_turn_plan", "").strip()
            ltp = plans.get("long_term_plans", "").strip()
            notes = plans.get("notes", "").strip()
            if ltp or ntp or notes:
                text += "\n\n## DIARY"
                if ltp:
                    text += f"\nLong-term Plans:\n{ltp}"
                else:
                    text += "\nLong-term Plans: (none)"
                if ntp:
                    text += f"\n\nPlan for This Turn (from last turn):\n{ntp}"
                else:
                    text += "\n\nPlan for This Turn: (none)"
                if notes:
                    text += f"\n\nNotes (accumulated learnings):\n{notes}"
        except Exception:
            log.debug("Failed to append diary to full game state", exc_info=True)

        # Append mailbox deals (managed civ proposals).
        try:
            mailbox = _get_mailbox(ctx)
            if mailbox is not None:
                seat = _get_seat(ctx)
                pending = mailbox.get_pending_for(seat.player_id)
                sent = mailbox.get_sent_by(seat.player_id)
                if pending or sent:
                    text += "\n\n=== DEAL MAILBOX ==="
                    for p in pending:
                        text += f"\n\nIncoming from P{p.from_player}"
                        text += f" (proposal {p.proposal_id}):"
                        if p.items_from_proposer:
                            text += "\n  They offer:"
                            for item in p.items_from_proposer:
                                text += _format_mailbox_item(item, indent="    ")
                        if p.items_from_target:
                            text += "\n  They request:"
                            for item in p.items_from_target:
                                text += _format_mailbox_item(item, indent="    ")
                        text += "\n  Use execute_commands with action='respond_to_trade'"
                        text += f" (other_player_id={p.from_player}, accept=True/False)"
                    for p in sent:
                        text += f"\n\nOutgoing to P{p.to_player}"
                        text += f" (proposal {p.proposal_id}): awaiting response."
        except Exception:
            log.debug("Failed to append mailbox deals", exc_info=True)

        # Append diplo mailbox (response-able actions to managed civs).
        try:
            app = _app(ctx)
            diplo_mb = app.diplo_mailbox
            if diplo_mb is not None:
                seat = _get_seat(ctx)
                incoming = diplo_mb.get_pending_for(seat.player_id)
                outgoing = diplo_mb.get_sent_by(seat.player_id)
                if incoming or outgoing:
                    text += "\n\n=== DIPLOMACY MAILBOX ==="
                    for p in incoming:
                        text += (
                            f"\n\nIncoming from P{p.from_player}"
                            f" (proposal {p.proposal_id}): {p.action_name}"
                        )
                        text += (
                            "\n  Use execute_commands with "
                            "action='respond_to_diplo_action'"
                            f" (other_player_id={p.from_player}, "
                            "accept=True/False)"
                        )
                    for p in outgoing:
                        if p.status == "pending":
                            status = "awaiting response"
                        elif p.status == "accepted":
                            status = "accepted — takes effect next turn"
                        else:
                            status = "rejected"
                        text += (
                            f"\n\nOutgoing to P{p.to_player}"
                            f" (proposal {p.proposal_id}): {p.action_name}"
                            f" — {status}."
                        )
        except Exception:
            log.debug("Failed to append diplo mailbox", exc_info=True)

        # Append chat messages (managed-player messaging).
        try:
            app = _app(ctx)
            mb = app.message_mailbox
            if mb is not None:
                seat = _get_seat(ctx)
                msgs = mb.for_player(seat.player_id, limit=50)
                if msgs:
                    text += "\n\n=== MESSAGES ==="
                    for m in msgs:
                        if m.from_player == seat.player_id:
                            who = "To"
                            other = m.to_player
                        else:
                            who = "From"
                            other = m.from_player
                        text += f"\n[{who} P{other} (T{m.turn})] {m.text}"
        except Exception:
            log.debug("Failed to append messages", exc_info=True)

        return text

    return await _logged(ctx, "get_full_game_state", {}, _run)


@mcp.tool()
async def execute_commands(ctx: Context, commands_json: str) -> str:
    """Execute a batch of game commands sequentially. This is your main interface for taking actions in the game.
    See the agent_reference for the full specification of each command.
    """
    gs = _get_game(ctx)
    app = _app(ctx)
    seat = _get_seat(ctx)

    async def _run():
        # Parse once to check for mailbox-aware trade commands.
        try:
            commands: list[dict] = json.loads(commands_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"
        if not isinstance(commands, list):
            return "Error: commands_json must be a JSON array."

        # Whitelist of actions documented in this tool's doc comment. Any
        # command whose action is not in this set aborts the whole batch —
        # nothing executes — so an unknown/typo'd action can never reach the
        # engine.
        _ALLOWED_ACTIONS = frozenset({
            # Units
            "move_unit", "attack_unit", "fortify_unit", "skip_unit",
            "skip_remaining_units", "automate_explore", "heal_unit",
            "alert_unit", "sleep_unit", "delete_unit", "enter_formation",
            "exit_formation", "promote_unit", "upgrade_unit",
            "check_unit_upgrade",
            # Settling & cities
            "found_city", "resolve_city_capture", "set_city_production",
            "purchase_item", "list_city_production", "set_city_focus",
            "purchase_tile", "city_attack",
            # Builders & improvements
            "improve_tile", "remove_feature", "repair_improvement",
            "remove_improvement", "build_route", "sacrifice_builder_charges",
            # Research & civics
            "set_research", "set_civic",
            # Diplomacy & trade
            "send_diplomatic_action", "diplomacy_respond", "propose_peace",
            "form_alliance", "propose_trade", "test_trade",
            "respond_to_deal", "respond_to_trade", "respond_to_diplo_action",
            "get_diplomacy_sessions",
            # Messaging (managed-player chat)
            "send_message",
            # Governance
            "set_policies", "change_government", "appoint_governor",
            "assign_governor", "promote_governor", "send_envoy",
            "choose_dedication",
            # Religion & Great People
            "choose_pantheon", "found_religion", "recruit_great_person",
            "patronize_great_person", "reject_great_person",
            "activate_great_person", "spread_religion",
            # Trade routes & spies
            "make_trade_route", "teleport_to_city", "spy_travel",
            "spy_mission",
            # World Congress
            "queue_wc_votes", "vote_world_congress", "submit_congress",
        })
        unknown = sorted(
            {cmd.get("action", "") for cmd in commands}
            - _ALLOWED_ACTIONS
        )
        if unknown:
            return (
                "Error: refusing to execute commands — unknown action(s): "
                + ", ".join(unknown)
                + ". Only actions listed in the execute_commands doc "
                "comment are permitted."
            )

        results: list[str] = []
        remaining: list[dict] = []
        for cmd in commands:
            action = cmd.get("action", "")
            params = cmd.get("params", {})
            if action == "send_message" and app.message_mailbox is not None:
                # Managed-player chat — never reaches the engine.
                result = await _send_message(app, seat, params)
                results.append(f"send_message: {result}")
                continue
            if action == "propose_trade" and app.mailbox is not None:
                target = params.get("other_player_id", -1)
                if (target == -1):
                    results.append(
                        "propose_trade: other_player_id is required in params"
                    )
                    continue

                # First verify no peace or alliance items are present (those are handled by propose_peace/form_alliance).
                if(params.get("offer_peace") or params.get("offer_alliance")):
                    results.append(
                        "propose_trade: peace/alliance items are not allowed in propose_trade; use propose_peace/form_alliance instead."
                    )
                    continue

                # Open Borders requires the Early Empire civic on the party
                # granting it (offer_open_borders → proposer grants;
                # request_open_borders → target grants). The mailbox
                # forced-deal path bypasses the engine's own validation, so
                # guard here to avoid filing/executing a deal that the engine
                # path would otherwise accept. Covers both the mailbox target
                # and the engine target within this block.
                ob_err = await _check_open_borders_civics(
                    gs, seat.player_id, target, params
                )
                if ob_err:
                    results.append(f"propose_trade: {ob_err}")
                    continue

                if target in app.handoff_config.managed_ids:
                    # Managed target — route through mailbox.
                    result = await _mailbox_propose_trade(
                        app, seat, target, params
                    )
                    results.append(f"propose_trade: {result}")
                    continue
                # Non-managed target — convert flat params and pass to engine.
                offer_items, request_items = _parse_trade_params(params)
                converted = {
                    "other_player_id": target,
                    "offer_items": offer_items,
                    "request_items": request_items,
                }
                cmd["params"] = converted
                remaining.append(cmd)
                continue
            elif (
                action in ("propose_peace", "form_alliance")
                and app.mailbox is not None
            ):
                target = params.get("other_player_id", -1)
                if target in app.handoff_config.managed_ids:
                    # Managed target — the default AI would auto-answer a
                    # peace/alliance proposal inside SendWorkingDeal, so route
                    # through the deal mailbox instead.  Check eligibility
                    # first (same guards the engine path uses), then convert
                    # to a propose_trade mailbox filing.
                    kind = "MAKE_PEACE" if action == "propose_peace" else "ALLIANCE"
                    if kind == "ALLIANCE":
                        alliance_type = params.get("alliance_type")
                        if not alliance_type:
                            results.append(
                                "form_alliance: alliance_type is required "
                                "(MILITARY|RESEARCH|CULTURAL|ECONOMIC|RELIGIOUS)"
                            )
                            continue
                    ok, reason = await _check_proposal_eligibility(
                        gs, target, kind
                    )
                    if not ok:
                        results.append(f"{action}: {reason}")
                        continue
                    if kind == "MAKE_PEACE":
                        params["offer_peace"] = True
                    else:
                        params["offer_alliance"] = alliance_type
                        params.pop("alliance_type", None)
                    result = await _mailbox_propose_trade(
                        app, seat, target, params
                    )
                    results.append(f"{action}: {result}")
                    continue

                # Unmanaged target — fall through to gs.propose_peace /
                # gs.form_alliance (the default AI responds). These don't accept
                # trade items so strip them.
                cmd_buf: dict = {"action": action, "params": {"other_player_id": target}}
                if (action == "form_alliance"):
                    alliance_type = params.get("alliance_type")
                    if alliance_type:
                        cmd_buf["params"]["alliance_type"] = alliance_type
                remaining.append(cmd_buf)
                continue
            elif action == "send_diplomatic_action" and app.diplo_mailbox is not None:
                target = params.get("other_player_id", -1)
                action_name = params.get("action_name", "")
                # Only response-able actions (friendship/delegation/embassy)
                # to a managed target are mailbox-routed. One-way actions
                # (denounce, war declarations) always go to the engine, and
                # so do response-able actions to non-managed (built-in AI)
                # civs — the AI responding is the correct behaviour there.
                if (
                    target in app.handoff_config.managed_ids
                    and action_name in RESPONSEABLE_DIPLO_ACTIONS
                ):
                    result = await _mailbox_propose_diplo(
                        app, seat, target, action_name
                    )
                    results.append(f"send_diplomatic_action: {result}")
                    continue
                # Non-managed target or one-way action — engine path.
                remaining.append(cmd)
                continue
            elif action == "respond_to_trade" and app.handoff_config.enabled:
                target = params.get("other_player_id", -1)
                accept = params.get("accept", False)
                if isinstance(accept, str):
                    accept = accept.lower() in ("true", "yes", "1", "accept")
                # Check mailbox first.
                mailbox = app.mailbox
                if mailbox is not None:
                    pending = mailbox.get_pending_for(seat.player_id)
                    for proposal in pending:
                        if proposal.from_player == target:
                            if accept:
                                result = await _execute_mailbox_deal(
                                    gs, mailbox, proposal, seat.player_id
                                )
                            else:
                                mailbox.reject(proposal.proposal_id)
                                result = (
                                    f"Deal from P{target} rejected."
                                )
                            results.append(f"respond_to_trade: {result}")
                            break
                    else:
                        # Not a mailbox deal — fall through to engine.
                        remaining.append(cmd)
                    continue
                remaining.append(cmd)
                continue
            elif action == "respond_to_diplo_action" and app.handoff_config.enabled:
                target = params.get("other_player_id", -1)
                accept = params.get("accept", False)
                if isinstance(accept, str):
                    accept = accept.lower() in ("true", "yes", "1", "accept")
                # Look up a pending diplo proposal from `target` to this seat.
                diplo_mb = app.diplo_mailbox
                if diplo_mb is not None:
                    pending = diplo_mb.get_pending_for(seat.player_id)
                    proposal = next(
                        (p for p in pending if p.from_player == target), None
                    )
                    if proposal is not None:
                        if accept:
                            # Mark accepted; the proposer executes the action
                            # on its next turn (see _drain_diplo_proposals).
                            diplo_mb.accept(proposal.proposal_id)
                            result = (
                                f"Accepted P{target}'s {proposal.action_name} "
                                f"proposal — takes effect on P{target}'s next "
                                f"turn."
                            )
                        else:
                            diplo_mb.reject(proposal.proposal_id)
                            result = (
                                f"Rejected P{target}'s "
                                f"{proposal.action_name} proposal."
                            )
                        results.append(f"respond_to_diplo_action: {result}")
                        continue
                    # No mailbox proposal — fall through to engine
                    # respond_to_diplomacy (a real AI-opened session caught by
                    # _check_mid_turn_diplomacy).
                    remaining.append(cmd)
                    continue
                remaining.append(cmd)
                continue
            remaining.append(cmd)

        # Pass remaining commands to the executor.
        if remaining:
            rem_json = json.dumps(remaining)
            rest_result = await _execute_commands(gs, rem_json)
            if results:
                results.append(rest_result)
            else:
                return rest_result

        return "\n".join(results) if results else "No commands to execute."

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
    """Block until it is your turn.

    Args:
        timeout_seconds: How long to wait before returning. Capped at 600.

    Returns a short status telling you your turn has started.  Call update_diary() 
    first to record your plans, then call wait_for_turn() to block until your next 
    turn. Call it again on timeout — no diary interaction happens here.
    """
    app = _app(ctx)
    seat = app.seats.resolve(seats_mod.session_key(ctx))
    if seat is app.seats.default:
        return (
            "You hold no seat — call get_seats() then claim_seat(player_id=N) "
            "before waiting for a turn."
        )
    timeout = max(1.0, min(float(timeout_seconds), 600.0))

    async def _wait_and_drain() -> str:
        report = await handoff.wait_for_turn(
            seat.game, seat, app.handoff_config, timeout
        )
        # If the turn actually started (not a timeout), drain diplo proposals
        # this seat filed that the target has since answered. The proposer is
        # local now, so executing them via the engine path is safe.
        diplo_mb = app.diplo_mailbox
        if diplo_mb is not None and diplo_mb.pending_count:
            try:
                own = await handoff.try_ownership(seat.game.conn)
            except Exception:
                own = None
            if own is not None and own.local_player == seat.player_id:
                drain = await _drain_diplo_proposals(
                    seat.game, diplo_mb, seat.player_id
                )
                if drain:
                    report = report + "\n\n=== DIPLOMACY RESOLVED ===\n" + "\n".join(
                        drain
                    )
        return report

    return await _logged(
        ctx,
        "wait_for_turn",
        {"timeout_seconds": timeout},
        _wait_and_drain,
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

@mcp.tool(annotations={"readOnlyHint": True})
async def get_available_governors(ctx: Context) -> str:
    """Get available governors and their bonuses.

    Shows which governors are unlocked, their promotion trees, and
    the bonuses they provide. Use appoint_governor to assign one to a city.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_governors()
        return nr.narrate_governors(status)

    return await _logged(ctx, "get_available_governors", {}, _run)

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

@mcp.tool(annotations={"destructiveHint": True})
async def end_turn(ctx: Context) -> str:
    """End the current turn.

    Make sure you've moved all units, set production, and chosen research
    before ending the turn.

    After end_turn(), think about what to do next turn and call
    update_diary(next_turn_plan=..., long_term_plans=..., notes=...) to
    record your plans. Then call wait_for_turn() to block until your next
    turn starts.
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
    notes: str = "",
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
        notes: Learnings worth remembering — game rules you discovered,
            mistakes you made and corrected, things the civilopedia taught
            you. Unlike the plan fields, notes are APPENDED to the existing
            notes each call rather than replacing them, so they accumulate
            across turns. Leave empty to leave the notes unchanged. Use this
            for durable facts (e.g. "tried to move onto an enemy unit's tile
            — illegal; must declare war first"), not transient plans.
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

    path = _diary_path(civ_type, seed)
    path.parent.mkdir(parents=True, exist_ok=True)

    ntp = next_turn_plan.strip()
    ltp = long_term_plans.strip()
    notes_in = notes.strip()

    # Inherit previous long-term plans if the agent left them empty.
    # next_turn_plan is always overwritten (empty = no plan for next turn).
    # notes is append-only: a non-empty value is appended to the running
    # body (with a turn marker); an empty value preserves the existing notes.
    previous = _get_current_plans(path)
    if not ltp:
        ltp = previous.get("long_term_plans", "")
    prev_notes = previous.get("notes", "")
    if notes_in:
        entry = f"[Turn {diary_turn}] {notes_in}"
        combined_notes = f"{prev_notes}\n{entry}" if prev_notes else entry
    else:
        combined_notes = prev_notes

    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "turn": diary_turn,
        "next_turn_plan": ntp,
        "long_term_plans": ltp,
        "notes": combined_notes,
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
    if notes_in:
        parts.append(f"Notes appended ({len(notes_in)} chars).")
    return " ".join(parts)

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
        log.info("Removed run_lua tool because CIV_MCP_DISABLE_LUA is set")

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

async def _mailbox_propose_trade(
    app, seat, target: int, params: dict
) -> str:
    """Build a PendingProposal from flat params and file it in the mailbox."""
    mailbox = app.mailbox
    if mailbox is None:
        return "Error: deal mailbox not available"

    agent_pid = seat.player_id
    offer_items, request_items = _parse_trade_params(params)

    proposer_items = [
        SerializedDealItem(
            item_type=it["type"],
            from_player_id=agent_pid,
            amount=it.get("amount", 0),
            duration=it.get("duration", 0),
            value_type=it.get("value_type", -1),
            subtype=it.get("subtype", -1),
            name=it.get("name", ""),
            alliance_type=it.get("alliance_type", ""),
        )
        for it in offer_items
    ]
    target_items = [
        SerializedDealItem(
            item_type=it["type"],
            from_player_id=target,
            amount=it.get("amount", 0),
            duration=it.get("duration", 0),
            value_type=it.get("value_type", -1),
            subtype=it.get("subtype", -1),
            name=it.get("name", ""),
            alliance_type=it.get("alliance_type", ""),
        )
        for it in request_items
    ]

    proposal = PendingProposal(
        from_player=agent_pid,
        to_player=target,
        items_from_proposer=proposer_items,
        items_from_target=target_items,
        proposed_by="agent",
    )
    proposal_id = mailbox.propose(proposal)
    return (
        f"Trade proposed to P{target} — awaiting response. "
        f"Proposal: {proposal_id}"
    )


async def _mailbox_propose_diplo(
    app, seat, target: int, action_name: str
) -> str:
    """File a response-able diplo action to a managed civ in the diplo mailbox.

    The action is NOT sent to the engine here.  Opening a session would let the
    target's built-in AI auto-respond: during this turn only the proposer is
    human-type, so every other player (managed or not) is AI to the engine and
    answers a freshly-opened session within seconds.  The target instead records
    accept/reject on its own turn, and the proposer executes the action on its
    *next* turn via :func:`_drain_diplo_proposals` — reusing the engine path
    that forces ``POSITIVE`` and closes same-frame, before the target's AI can
    decide.  Because the proposer opens the session, the action's direction
    (delegation/embassy belong to the proposer) is correct.
    """
    diplo_mb = app.diplo_mailbox
    if diplo_mb is None:
        return "Error: diplo mailbox not available"
    agent_pid = seat.player_id
    proposal = PendingDiploProposal(
        from_player=agent_pid,
        to_player=target,
        action_name=action_name,
        proposed_by="agent",
    )
    proposal_id = diplo_mb.propose(proposal)
    return (
        f"{action_name} proposed to P{target} — awaiting response. "
        f"Proposal: {proposal_id}"
    )


async def _drain_diplo_proposals(
    gs, diplo_mb: DiploMailbox, proposer_pid: int
) -> list[str]:
    """Execute/report diplo proposals the proposer filed that have been answered.

    Called at the start of the proposer's turn (the proposer is local again), so
    :meth:`GameState.send_diplomatic_action` — which forces ``POSITIVE`` and
    closes the session same-frame — is safe: the target pre-consented via the
    mailbox, and the target's AI never gets a turn to decide.  Re-validation
    inside the engine builder drops proposals that became invalid (war declared,
    delegation obsolete, etc.); those surface as a failure line rather than a
    crash.  Returns one message line per drained proposal.
    """
    lines: list[str] = []
    for proposal in diplo_mb.get_drainable_by(proposer_pid):
        if proposal.status == "accepted":
            try:
                result = await gs.send_diplomatic_action(
                    proposal.to_player, proposal.action_name
                )
            except Exception as e:  # pragma: no cover - defensive
                result = f"execution failed: {e}"
            lines.append(
                f"Your {proposal.action_name} to P{proposal.to_player} "
                f"took effect: {result}"
            )
        else:  # rejected
            lines.append(
                f"P{proposal.to_player} rejected your "
                f"{proposal.action_name} proposal."
            )
        diplo_mb.remove(proposal.proposal_id)
    return lines


def _parse_trade_params(params: dict) -> tuple[list[dict], list[dict]]:
    """Parse flat trade params into (offer_items, request_items) lists."""
    offer_items: list[dict] = []
    request_items: list[dict] = []

    offer_gold = params.get("offer_gold", 0)
    offer_gpt = params.get("offer_gold_per_turn", 0)
    offer_res = params.get("offer_resources", "")
    offer_favor = params.get("offer_favor", 0)
    offer_ob = params.get("offer_open_borders", False)
    offer_peace = params.get("offer_peace", False)
    offer_alliance = params.get("offer_alliance", "")
    req_gold = params.get("request_gold", 0)
    req_gpt = params.get("request_gold_per_turn", 0)
    req_res = params.get("request_resources", "")
    req_favor = params.get("request_favor", 0)
    req_ob = params.get("request_open_borders", False)
    joint_war = params.get("joint_war_target", 0)

    if offer_gold > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})
    if offer_gpt > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gpt, "duration": 30})
    if isinstance(offer_res, str):
        for res in offer_res.split(","):
            res = res.strip()
            if res:
                offer_items.append({"type": "RESOURCE", "name": res, "amount": 1, "duration": 30})
    if offer_favor > 0:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})
    if offer_ob:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if offer_peace:
        offer_items.append({"type": "AGREEMENT", "subtype": "MAKE_PEACE"})
        request_items.append({"type": "AGREEMENT", "subtype": "MAKE_PEACE"})
    if offer_alliance:
        offer_items.append(
            {"type": "AGREEMENT", "subtype": "ALLIANCE", "alliance_type": offer_alliance.upper()}
        )
        request_items.append(
            {"type": "AGREEMENT", "subtype": "ALLIANCE", "alliance_type": offer_alliance.upper()}
        )
    if req_gold > 0:
        request_items.append({"type": "GOLD", "amount": req_gold, "duration": 0})
    if req_gpt > 0:
        request_items.append({"type": "GOLD", "amount": req_gpt, "duration": 30})
    if isinstance(req_res, str):
        for res in req_res.split(","):
            res = res.strip()
            if res:
                request_items.append({"type": "RESOURCE", "name": res, "amount": 1, "duration": 30})
    if req_favor > 0:
        request_items.append({"type": "FAVOR", "amount": req_favor})
    if req_ob:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if joint_war > 0:
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})

    return offer_items, request_items


async def _check_open_borders_civics(
    gs: GameState, proposer_pid: int, target_pid: int, params: dict
) -> str | None:
    """Guard: parties granting Open Borders must have the Early Empire civic.

    Open Borders is unlocked by the Early Empire civic, and the party
    *granting* it is the one that must have it: ``offer_open_borders`` → the
    proposer grants; ``request_open_borders`` → the target grants.  Returns an
    error message for the first violating party, or *None* when the deal is
    allowed (or contains no Open Borders items).
    """
    offer_ob = bool(params.get("offer_open_borders", False))
    request_ob = bool(params.get("request_open_borders", False))
    if not offer_ob and not request_ob:
        return None

    # HasCivic is GameCore-safe, so execute_read suffices. One round-trip
    # checks both parties; output: EE|<proposer_pid>=0|<target_pid>=1
    lua = (
        'local ee = GameInfo.Civics["CIVIC_EARLY_EMPIRE"] '
        "local eeIdx = ee and ee.Index or -1 "
        "local function hasEE(pid) "
        '  if pid < 0 or not Players[pid] or not Players[pid]:IsAlive() then return false end '
        "  if eeIdx < 0 then return false end "
        "  local ok, res = pcall(function() return Players[pid]:GetCulture():HasCivic(eeIdx) end) "
        "  return ok and res == true "
        "end "
        f'print("EE|{proposer_pid}=" .. (hasEE({proposer_pid}) and "1" or "0") '
        f'  .. "|{target_pid}=" .. (hasEE({target_pid}) and "1" or "0")) '
        'print("---END---")'
    )
    try:
        lines = await gs.conn.execute_read(lua)
    except Exception as e:
        return f"Open Borders civic check failed: {e}"

    proposer_has: bool | None = None
    target_has: bool | None = None
    for line in lines:
        if line.startswith("EE|"):
            for field in line[3:].split("|"):
                if "=" in field:
                    pid_s, val = field.split("=", 1)
                    try:
                        pid = int(pid_s)
                    except ValueError:
                        continue
                    has = val == "1"
                    if pid == proposer_pid:
                        proposer_has = has
                    elif pid == target_pid:
                        target_has = has
            break

    if proposer_has is None or target_has is None:
        return (
            "Open Borders requires the Early Empire civic, but the civic "
            "status check returned no result."
        )
    if offer_ob and not proposer_has:
        return (
            "Open Borders requires the Early Empire civic, which you "
            f"(P{proposer_pid}) do not have."
        )
    if request_ob and not target_has:
        return (
            "Open Borders requires the Early Empire civic, which "
            f"P{target_pid} does not have."
        )
    return None


async def _check_proposal_eligibility(
    gs: GameState, other_player_id: int, kind: str
) -> tuple[bool, str]:
    """Run the peace/alliance eligibility guard against the live game.

    Returns ``(True, "")`` if the proposal is allowed, otherwise
    ``(False, "<reason>")`` where ``<reason>`` is the ``ERR:REASON|...``
    string the Lua guard bailed with (or a connection error message).
    """
    from civ_mcp.lua import diplomacy as diplo_lua

    lua = diplo_lua.build_check_proposal_eligibility(other_player_id, kind)
    try:
        lines = await gs.conn.execute_write(lua, perspective=False)
    except Exception as e:
        return False, f"eligibility check failed: {e}"
    for line in lines:
        if line.startswith("OK|"):
            return True, ""
        if line.startswith("ERR:"):
            # Strip the leading "ERR:" for a cleaner message.
            return False, line[4:]
    return False, "eligibility check returned no result"


async def _execute_mailbox_deal(
    gs: GameState, mailbox: DealMailbox, proposal: PendingProposal, accepter_pid: int
) -> str:
    """Execute a mailbox proposal via the forced-deal primitive."""
    # Determine who the accepter is relative to the proposal.
    # If the accepter is the original proposer's target, the deal is
    # accepted as-is.  If the accepter is the proposer themselves...
    # that shouldn't happen (they can't accept their own proposal).
    from civ_mcp.lua import diplomacy as diplo_lua

    lua = handoff.build_execute_deal_lua(proposal, accepter_pid)
    try:
        lines = await gs.conn.execute_write(lua, perspective=False)
        result = next(
            (l for l in lines if l.startswith("DEAL_EXECUTED|")),
            "DEAL_EXECUTED|unknown",
        )
        mailbox.accept(proposal.proposal_id)
        return (
            f"Deal accepted and executed: {result}\n"
            f"Proposal {proposal.proposal_id} resolved."
        )
    except Exception as e:
        return f"Deal execution failed: {e}"

def _format_mailbox_item(item: SerializedDealItem, indent: str = "  ") -> str:
    """One-line summary of a mailbox deal item."""
    t = item.item_type.upper()
    if t == "GOLD":
        if item.duration > 0:
            return f"{indent}- {item.amount} Gold per turn ({item.duration} turns)\n"
        return f"{indent}- {item.amount} Gold\n"
    elif t == "RESOURCE":
        return f"{indent}- Resource (id={item.value_type}) x{item.amount}\n"
    elif t == "FAVOR":
        return f"{indent}- {item.amount} Diplomatic Favor\n"
    elif t == "AGREEMENT":
        return f"{indent}- Agreement (sub={item.subtype})\n"
    elif t == "CITY":
        return f"{indent}- City (id={item.value_type})\n"
    else:
        return f"{indent}- {t} (amount={item.amount})\n"
