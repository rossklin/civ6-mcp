"""Spectator-mode background services for video recording.

Two asyncio services that run alongside the agent and share the GameConnection:

CameraController — hops the in-game camera to key locations as the agent acts.
  Tools push (x, y) events; the controller replays them at 1-second intervals.
  Pauses automatically when a diplomacy screen is active.

PopupWatcher — dismisses non-critical popups at two scheduled points in an
  agent's turn (shortly after turn start, and shortly after a command batch),
  never during the human's turn.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from civ_mcp.connection import GameConnection

log = logging.getLogger(__name__)

SENTINEL = "---END---"

# How long (seconds) the camera dwells at each location before the next hop.
CAMERA_DWELL = 1.0

# Maximum queued camera events — oldest dropped when full.
CAMERA_QUEUE_MAX = 6

# Delay (seconds) after a managed seat's turn starts before popups are
# dismissed once. Rollover popups (civic complete, era change, ...) land
# before the agent's first state read.
POPUP_TURN_START_DELAY = 5.0

# Delay (seconds) after an execute_commands batch completes before popups
# are dismissed once. Never fires per command, only per batch.
POPUP_POST_BATCH_DELAY = 2.0

# Non-critical popups that will be auto-dismissed.
_NONCRITICAL_POPUPS = [
    "InGamePopup",
    "GenericPopup",
    "PopupDialog",
    "TechCivicCompletedPopup",
    "BoostUnlockedPopup",
    "GreatWorkShowcase",
    "NaturalWonderPopup",
    "WonderBuiltPopup",
    "EraCompletePopup",
    "HistoricMoments",
    "MomentPopup",
    "ProjectBuiltPopup",
    "RockBandPopup",
    "RockBandMoviePopup",
    "NaturalDisasterPopup",
]

# Critical screens — pause both camera and popup watcher while visible.
_CRITICAL_SCREENS = [
    "DiplomacyActionView",
    "DiplomacyDealView",
]

# Lua snippet that returns CLEAR / POPUP / CRITICAL in one roundtrip.
_POPUP_POLL_LUA = (
    "local r='CLEAR' "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then r='CRITICAL' end end "
        for n in _CRITICAL_SCREENS
    )
    + "if r=='CLEAR' then "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then r='POPUP' end end "
        for n in _NONCRITICAL_POPUPS
    )
    + "end "
    + f"print(r) print('{SENTINEL}')"
)

# Lua snippet to check for active diplomacy screens (used by camera).
_DIPLOMACY_CHECK_LUA = (
    "local active=false "
    + "".join(
        f"do local c=ContextPtr:LookUpControl('/InGame/{n}') "
        f"if c and not c:IsHidden() then active=true end end "
        for n in _CRITICAL_SCREENS
    )
    + f"print(active and 'YES' or 'NO') print('{SENTINEL}')"
)


@dataclass
class CameraEvent:
    x: int
    y: int
    label: str = ""


class CameraController:
    """Hops the game camera to locations pushed by tool handlers.

    Call push(x, y) from any tool. The controller dequeues events in the
    background, fires UI.LookAtPlot, and waits CAMERA_DWELL seconds before
    the next hop. Pauses automatically during active diplomacy screens.
    """

    def __init__(self, conn: "GameConnection") -> None:
        self._conn = conn
        self._queue: asyncio.Queue[CameraEvent] = asyncio.Queue(
            maxsize=CAMERA_QUEUE_MAX
        )
        self._task: asyncio.Task | None = None

    def push(self, x: int, y: int, label: str = "") -> None:
        """Push a camera event. Drops the oldest event if the queue is full."""
        event = CameraEvent(x, y, label)
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def clear(self) -> None:
        """Drain all pending events (call when a turn advances)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="camera-controller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _is_diplomacy_active(self) -> bool:
        try:
            lines = await self._conn.execute_write(_DIPLOMACY_CHECK_LUA, timeout=2.0)
            return any(line.strip() == "YES" for line in lines)
        except Exception:
            return False

    async def _look_at(self, x: int, y: int) -> None:
        lua = (
            f"local p=Map.GetPlot({x},{y}) "
            f"if p then pcall(function() UI.LookAtPlot(p) end) end "
            f"print('{SENTINEL}')"
        )
        try:
            await self._conn.execute_write(lua, timeout=2.0)
        except Exception:
            pass

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            # Hold until diplomacy screen closes.
            while True:
                try:
                    if not await self._is_diplomacy_active():
                        break
                except Exception:
                    break
                await asyncio.sleep(0.5)
            await self._look_at(event.x, event.y)
            await asyncio.sleep(CAMERA_DWELL)


class PopupWatcher:
    """Dismisses non-critical popups at two scheduled points in an agent's turn.

    Popup dismissal is a visual nicety, not a functional requirement, and
    every dismissal competes for the single FireTuner connection lock — a
    2 Hz polling watcher was measured stalling command batches for roughly
    half their wall time.  So there is no polling: server.py schedules

    - ``schedule_turn_start_dismiss()`` — once, POPUP_TURN_START_DELAY
      seconds into a managed seat's turn (wait_for_turn / claim_seat).
    - ``schedule_post_batch_dismiss()`` — once, POPUP_POST_BATCH_DELAY
      seconds after an execute_commands batch completes.  A new batch
      cancels the pending dismissal, so back-to-back batches only ever
      queue one.

    Both re-verify at fire time that a managed agent still holds the
    local-player slot — never the human (their dialogs are theirs) and
    never a processing built-in AI (InGame queries can stall it) — and
    skip while a diplomacy screen is up.
    """

    def __init__(self, conn: "GameConnection", handoff_config) -> None:
        self._conn = conn
        self._cfg = handoff_config
        self._turn_start_task: asyncio.Task | None = None
        self._post_batch_task: asyncio.Task | None = None

    async def stop(self) -> None:
        """Cancel any pending dismissal (call during shutdown)."""
        tasks = (self._turn_start_task, self._post_batch_task)
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._turn_start_task = None
        self._post_batch_task = None

    # -- scheduling API (sync, fire-and-forget) --

    def schedule_turn_start_dismiss(self) -> None:
        """Dismiss popups once, POPUP_TURN_START_DELAY seconds from now."""
        self._turn_start_task = self._reschedule(
            self._turn_start_task, POPUP_TURN_START_DELAY, "turn-start"
        )

    def schedule_post_batch_dismiss(self) -> None:
        """Dismiss popups once, POPUP_POST_BATCH_DELAY seconds from now."""
        self._post_batch_task = self._reschedule(
            self._post_batch_task, POPUP_POST_BATCH_DELAY, "post-batch"
        )

    def _reschedule(
        self, old: asyncio.Task | None, delay: float, label: str
    ) -> asyncio.Task:
        if old and not old.done():
            old.cancel()
        return asyncio.create_task(
            self._dismiss_after(delay, label), name=f"popup-dismiss-{label}"
        )

    async def _dismiss_after(self, delay: float, label: str) -> None:
        await asyncio.sleep(delay)
        try:
            await self._dismiss_if_allowed(label)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Popup dismissal (%s) failed", label, exc_info=True)

    async def _dismiss_if_allowed(self, label: str) -> None:
        from civ_mcp.game_lifecycle import dismiss_popup

        if not await self._agent_on_clock():
            return
        # Skip while a diplomacy screen is up (CRITICAL) — dismissal only
        # targets non-critical popups.
        if await self._poll() == "CRITICAL":
            return
        result = await dismiss_popup(self._conn)
        if "Dismissed" in result:
            log.info("PopupWatcher: %s dismiss: %s", label, result)

    async def _agent_on_clock(self) -> bool:
        """True only while a managed agent holds the local-player slot.

        Classic (non-handoff) mode has no human in the game — the agent is
        the only player — so dismissal is always allowed there.
        """
        if not self._cfg.enabled:
            return True
        from civ_mcp import handoff

        try:
            own = await handoff.try_ownership(self._conn)
        except Exception:
            return False
        return own.local_player is not None and own.local_player in self._cfg.agent_ids

    async def _poll(self) -> str:
        """Returns 'POPUP', 'CRITICAL', or 'CLEAR'."""
        try:
            lines = await self._conn.execute_write(_POPUP_POLL_LUA, timeout=2.0)
            for line in lines:
                s = line.strip()
                if s in ("POPUP", "CRITICAL", "CLEAR"):
                    return s
        except Exception:
            pass
        return "CLEAR"
