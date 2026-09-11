"""Game lifecycle management — kill, launch, and load saves via OCR.

Safety guardrails for automated agents:
- Only kills Civ 6 processes (hardcoded process names)
- Only launches Civ 6 via Steam (hardcoded app ID 289070)
- Only loads saves from the known autosave directory
- No config file modifications, no arbitrary system commands
- All process/file interactions are scoped to Civ 6 only

Platform support:
- macOS: fully supported (process mgmt, OCR, window automation)
  Install with: uv pip install 'civ6-mcp[launcher-macos]'
- Windows: fully supported (process mgmt, OCR, window automation)
  Install with: uv pip install 'civ6-mcp[launcher-windows]'
- Linux: fully supported (process mgmt, OCR, window automation)
  Install with: uv pip install 'civ6-mcp[launcher-linux]'
  System deps: sudo apt install xdotool tesseract-ocr
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import socket
import subprocess
import sys
import time
from typing import NamedTuple

log = logging.getLogger(__name__)

# Enable per-monitor DPI awareness on Windows so we get true pixel
# coordinates and window dimensions (not DPI-virtualized values).
if sys.platform == "win32":
    try:
        import ctypes as _ctypes

        _ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass  # Older Windows or already set


class WindowInfo(NamedTuple):
    """Game window metadata (Quartz on macOS, win32gui on Windows, xdotool on Linux)."""

    window_id: int  # CGWindowNumber on macOS, HWND on Windows, XID on Linux
    x: int  # screen points
    y: int  # screen points
    w: int  # screen points
    h: int  # screen points
    pid: int


# ---------------------------------------------------------------------------
# Constants — hardcoded for safety (not configurable by agents)
# ---------------------------------------------------------------------------

STEAM_APP_ID = "289070"
_ALLOWED_PROCESS_PATTERNS = ("Civ6",)  # pkill -f pattern — only matches Civ 6
# CGWindowList/AppKit report app name ("Civilization VI"), not binary name ("Civ6_Exe")
_APP_NAME_PATTERNS = ("Civilization",)

if sys.platform == "darwin":
    _PROCESS_NAMES = ("Civ6_Exe_Child", "Civ6_Exe", "Civ6")
    _SAVE_BASE = os.path.expanduser(
        "~/Library/Application Support/Sid Meier's Civilization VI/"
        "Sid Meier's Civilization VI/Saves/Single"
    )
    SAVE_DIR = os.path.join(_SAVE_BASE, "auto")  # autosaves
    SINGLE_SAVE_DIR = _SAVE_BASE  # regular saves (including benchmark)
elif sys.platform == "win32":
    _PROCESS_NAMES = (
        "CivilizationVI_DX12.exe",
        "CivilizationVI.exe",
        "Civ6_Exe_Child.exe",
        "Civ6_Exe.exe",
    )
    _SAVE_BASE = os.path.expanduser(
        "~/Documents/My Games/Sid Meier's Civilization VI/Saves/Single"
    )
    SAVE_DIR = os.path.join(_SAVE_BASE, "auto")
    SINGLE_SAVE_DIR = _SAVE_BASE
elif sys.platform == "linux":
    _PROCESS_NAMES = ("Civ6", "Civ6Sub")
    _SAVE_BASE = os.path.expanduser(
        "~/.local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single"
    )
    SAVE_DIR = os.path.join(_SAVE_BASE, "auto")
    SINGLE_SAVE_DIR = _SAVE_BASE
else:
    _PROCESS_NAMES = ()
    SAVE_DIR = ""
    SINGLE_SAVE_DIR = ""

# How long to wait after kill for Steam to deregister the game
_KILL_SETTLE_SECONDS = 10
# How long to wait for game process to appear after launch
_LAUNCH_TIMEOUT_SECONDS = 60
# How long to wait for FireTuner port to open after game process starts
_PORT_POLL_TIMEOUT = 180
# Tuner TCP port
_TUNER_PORT = 4318


def _require_gui_deps() -> None:
    """Validate GUI dependencies are available, raising clear error if missing."""
    if sys.platform == "win32":
        try:
            import win32gui  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Game launcher requires pywin32. Install with: uv pip install pywin32"
            )
        try:
            from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Game launcher requires Windows OCR support. "
                "Install with: uv pip install 'civ6-mcp[launcher-windows]'"
            )
        return
    if sys.platform == "linux":
        import shutil

        missing = []
        if shutil.which("xdotool") is None:
            missing.append("xdotool (sudo apt install xdotool)")
        try:
            import mss  # noqa: F401
        except ImportError:
            missing.append("python-mss (uv pip install 'civ6-mcp[launcher-linux]')")
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            missing.append("pytesseract (uv pip install 'civ6-mcp[launcher-linux]')")
        if shutil.which("tesseract") is None:
            missing.append("tesseract-ocr (sudo apt install tesseract-ocr)")
        if missing:
            raise RuntimeError("Game launcher GUI requires: " + ", ".join(missing))
        return
    if sys.platform != "darwin":
        raise NotImplementedError(f"GUI automation not supported on {sys.platform}")
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "Game launcher requires pyobjc GUI dependencies. "
            "Install with: uv pip install 'civ6-mcp[launcher-macos]'"
        )


# ---------------------------------------------------------------------------
# Process management (no GUI deps needed)
# ---------------------------------------------------------------------------


def is_game_running() -> bool:
    """Check if Civ 6 is running."""
    if sys.platform in ("darwin", "linux"):
        r = subprocess.run(
            ["pgrep", "-f", _ALLOWED_PROCESS_PATTERNS[0]],
            capture_output=True,
        )
        return r.returncode == 0
    elif sys.platform == "win32":
        for name in _PROCESS_NAMES:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
            )
            if name.lower() in r.stdout.lower():
                return True
        return False
    raise NotImplementedError(f"is_game_running not supported on {sys.platform}")


def _dismiss_crash_dialog() -> bool:
    """Dismiss macOS crash report dialog if present.

    Returns True if a dialog was dismissed, False if none found.
    """
    if sys.platform != "darwin":
        return False
    try:
        # The crash dialog is owned by UserNotificationCenter with an empty
        # window name.  Buttons are "Reopen", "Report...", "Ignore".
        # Click "Ignore" to dismiss without relaunching the crashed app.
        script = (
            'tell application "System Events"\n'
            "    set found to false\n"
            '    tell process "UserNotificationCenter"\n'
            "        repeat with win in every window\n"
            "            try\n"
            '                click button "Ignore" of win\n'
            "                set found to true\n"
            "            end try\n"
            "        end repeat\n"
            "    end tell\n"
            "    return found\n"
            "end tell"
        )
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dismissed = r.stdout.strip() == "true"
        if dismissed:
            log.info("Dismissed macOS crash report dialog")
        return dismissed
    except Exception as e:
        log.debug("Crash dialog check failed: %s", e)
        return False


def _kill_game_sync() -> str:
    """Kill Civ 6 and wait for Steam to deregister. Blocking."""
    if not is_game_running():
        return "Game is not running."

    if sys.platform in ("darwin", "linux"):
        # Kill only the game binary — NOT Steam or other processes.
        # Using -x (exact match) instead of -f (pattern) to avoid
        # killing Steam when its command line contains "Civ6".
        for proc_name in _PROCESS_NAMES:
            subprocess.run(["pkill", "-9", "-x", proc_name], capture_output=True)
    elif sys.platform == "win32":
        for name in _PROCESS_NAMES:
            subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True)
    else:
        raise NotImplementedError(f"kill not supported on {sys.platform}")
    log.info("Killed Civ 6, waiting %ds for Steam to deregister", _KILL_SETTLE_SECONDS)

    # Wait for process to actually die
    for _ in range(10):
        if not is_game_running():
            break
        time.sleep(1)

    # Extra wait for Steam to deregister
    time.sleep(_KILL_SETTLE_SECONDS)

    if is_game_running():
        return "WARNING: Game process may still be running after kill attempt."
    return "Game killed. Steam deregistration wait complete."


def _click_aspyr_launcher_sync() -> str | None:
    """Click PLAY on the Aspyr launcher if it appears (macOS only).

    On macOS, `steam://run/289070` opens the Aspyr LaunchPad — a splash
    screen with a PLAY button — before the actual game binary starts.
    This function detects that screen via OCR and clicks through it.

    Uses fullscreen OCR directly because the Aspyr launcher window
    cannot be captured via CGWindowListCreateImage (different process).

    Returns None on success, error string on failure.
    """
    if sys.platform != "darwin":
        return None  # no Aspyr launcher on other platforms

    try:
        _require_gui_deps()
    except (RuntimeError, NotImplementedError):
        log.warning("GUI deps not available — cannot auto-click Aspyr launcher")
        return "GUI deps not available. Click PLAY on the Aspyr launcher manually."

    log.info("Waiting for Aspyr launcher PLAY button...")
    start = time.time()
    while time.time() - start < 30:
        # Bring game window to front if it exists
        win = _find_game_window()
        if win:
            _bring_to_front(pid=win.pid)
            time.sleep(0.3)
        # Use fullscreen OCR — Aspyr launcher can't be window-captured
        results = _ocr_fullscreen()
        match = _find_text(results, "PLAY", exact=True)
        if match:
            text, x, y, w, h = match
            log.info("OCR: found '%s' at (%d,%d) — clicking", text, x, y)
            # The Aspyr launcher is a separate process from the game.
            # CGEventPost clicks require the target to be frontmost.
            # Click the mouse position directly — the launcher should
            # already be visible since we just OCR'd it on screen.
            _bring_to_front()
            time.sleep(0.3)
            _click(x, y)
            # Double-click in case the first was absorbed by the focus change
            time.sleep(0.5)
            _click(x, y)
            time.sleep(3)
            log.info("Clicked PLAY on Aspyr launcher")
            return None
        time.sleep(3)

    # Launcher may not appear if game was already past it
    log.info("Aspyr launcher PLAY button not found — may have been skipped")
    return None


def _wait_for_game_process(timeout: int = _LAUNCH_TIMEOUT_SECONDS) -> int | None:
    """Wait for the actual game process to appear. Returns seconds waited, or None."""
    for i in range(timeout):
        if is_game_running():
            log.info("Game process detected after %ds", i)
            return i
        time.sleep(1)
    return None


def _is_tuner_port_open() -> bool:
    """Check if the FireTuner port accepts TCP connections."""
    try:
        s = socket.create_connection(("127.0.0.1", _TUNER_PORT), timeout=2)
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def _click_continue_positional() -> None:
    """Click the CONTINUE GAME button by its known position on the leader screen.

    The button is a teal ribbon at a fixed relative position across all
    resolutions: roughly (38%, 75%) of the game window.  Used as a fallback
    when OCR cannot read the low-contrast teal-on-teal text.

    On macOS, kCGWindowBounds includes the title bar + shadow, so we detect
    the content area offset the same way _ocr_game_window does (capture →
    measure → compare) to avoid clicking ~30px too high.
    """
    win = _find_game_window()
    if win is None:
        log.warning("Positional click: no game window found")
        return

    # Detect the content area offset to avoid clicking on decorations.
    # macOS: kCGWindowBounds includes title bar + shadow → measure via capture.
    # Linux: xdotool geometry may include decorations → use mss capture region.
    content_x = win.x
    content_y = win.y
    content_w = win.w
    content_h = win.h
    if sys.platform == "linux":
        try:
            _, capture_geo = _capture_window_linux(win.window_id)
            content_x = capture_geo["left"]
            content_y = capture_geo["top"]
            content_w = capture_geo["width"]
            content_h = capture_geo["height"]
        except Exception:
            log.debug("Positional click: could not get capture geometry")
    elif sys.platform == "darwin":
        try:
            import Quartz

            cg_image = _capture_window(win.window_id)
            img_px_w = Quartz.CGImageGetWidth(cg_image)
            img_px_h = Quartz.CGImageGetHeight(cg_image)
            if win.w and img_px_w and img_px_h:
                scale = img_px_w / win.w
                img_pt_h = img_px_h / scale
                gap = win.h - img_pt_h
                if gap > 5:
                    content_y = win.y + gap
                    content_h = int(img_pt_h)
        except Exception:
            log.debug("Positional click: could not detect title bar offset")

    # Try multiple positions — the button's relative position varies across
    # resolutions and window modes:
    #   4K fullscreen:    ~38%, 75%
    #   1080p fullscreen: ~35%, 82%
    #   1280x962 window:  ~32%, 77% (Linux windowed, empirically verified)
    #   1600x900 window:  ~15%, 88% (button shifts left on smaller windows)
    #   1280x973 window:  ~15-20%, 85-88%
    positions = [
        (0.32, 0.77),  # 1280x962 Linux windowed (verified on MAEVE)
        (0.15, 0.88),  # small windowed (left-aligned panel)
        (0.20, 0.86),  # small windowed (slight offset)
        (0.15, 0.85),  # small windowed (slightly higher)
        (0.25, 0.84),  # medium windowed
        (0.35, 0.82),  # 1080p fullscreen
        (0.38, 0.80),  # between 1080p and 4K
        (0.35, 0.78),  # 1080p variant
        (0.38, 0.75),  # 4K fullscreen
    ]

    # Log all positions upfront for debugging
    coords = [
        (content_x + int(content_w * px), content_y + int(content_h * py), px, py)
        for px, py in positions
    ]
    log.info(
        "Positional click grid [%d positions]: %s "
        "[window %dx%d at (%d,%d), content_y=%d content_h=%d]",
        len(coords),
        ", ".join(f"({x},{y})[{px:.0%},{py:.0%}]" for x, y, px, py in coords),
        win.w,
        win.h,
        win.x,
        win.y,
        content_y,
        content_h,
    )

    _bring_to_front()
    time.sleep(0.3)
    for i, (abs_x, abs_y, pct_x, pct_y) in enumerate(coords, 1):
        log.info(
            "Grid click [%d/%d] at (%d,%d) [%.0f%%,%.0f%%]",
            i,
            len(coords),
            abs_x,
            abs_y,
            pct_x * 100,
            pct_y * 100,
        )
        _click(abs_x, abs_y)
        time.sleep(0.5)


def _wait_for_tuner_port(timeout: int = _PORT_POLL_TIMEOUT) -> bool:
    """Poll TCP 4318 until it accepts connections.

    Returns True if port became reachable within timeout.
    """
    interval = 3

    for i in range(int(timeout / interval)):
        if _is_tuner_port_open():
            log.info("FireTuner port reachable after %ds", i * interval)
            return True

        if i % 10 == 0 and i > 0:
            log.info("Waiting for FireTuner port... %ds elapsed", i * interval)
        time.sleep(interval)

    return False


def _find_game_exe_win32() -> str | None:
    """Find the Civ 6 DX12 EXE via Steam library folders."""
    import re

    steam_dir = os.path.join(
        os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "Steam"
    )
    vdf_path = os.path.join(steam_dir, "steamapps", "libraryfolders.vdf")

    if not os.path.exists(vdf_path):
        return None

    # Parse VDF to find library paths containing app 289070
    try:
        with open(vdf_path, "r") as f:
            content = f.read()
    except OSError:
        return None

    # Split into library blocks and find ones containing our app ID
    blocks = re.split(r'"\d+"\s*\{', content)
    for block in blocks:
        if f'"{STEAM_APP_ID}"' not in block:
            continue
        # Extract path from this block
        m = re.search(r'"path"\s+"([^"]+)"', block)
        if not m:
            continue
        lib_path = m.group(1).replace("\\\\", "\\")
        exe = os.path.join(
            lib_path,
            "steamapps",
            "common",
            "Sid Meier's Civilization VI",
            "Base",
            "Binaries",
            "Win64Steam",
            "CivilizationVI_DX12.exe",
        )
        if os.path.exists(exe):
            return exe

    # Fallback: check default location directly
    exe = os.path.join(
        steam_dir,
        "steamapps",
        "common",
        "Sid Meier's Civilization VI",
        "Base",
        "Binaries",
        "Win64Steam",
        "CivilizationVI_DX12.exe",
    )
    return exe if os.path.exists(exe) else None


def _launch_game_sync() -> str:
    """Launch Civ 6 and wait for the FireTuner port to open. Blocking.

    On macOS, Steam opens the Aspyr LaunchPad first (a splash screen
    with a PLAY button). This function auto-clicks through it if GUI
    deps are available.

    On Windows, falls back to direct EXE launch if steam://run fails.
    """
    # Dismiss any crash reporter dialogs blocking relaunch
    _dismiss_crash_dialogs_sync()

    if is_game_running():
        if _is_tuner_port_open():
            return "Game is already running and FireTuner port is open."
        # Process exists but port not open — wait for it
        log.info("Game process running but port not open yet, waiting...")
        if _wait_for_tuner_port():
            return "Game was starting up. FireTuner port is now open."
        # Stale process — kill it and launch fresh
        log.warning(
            "Game process running but tuner never opened — killing stale process"
        )
        _kill_game_sync()
        time.sleep(5)

    # Launch via Steam
    if sys.platform == "darwin":
        subprocess.run(["open", f"steam://run/{STEAM_APP_ID}"])
    elif sys.platform == "linux":
        # Use -applaunch (not steam:// URI) — the URI scheme is unreliable
        # when Steam is already running (silently ignored by some builds).
        subprocess.Popen(
            ["steam", "-applaunch", str(STEAM_APP_ID)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif sys.platform == "win32":
        os.startfile(f"steam://run/{STEAM_APP_ID}")  # noqa: S606 — hardcoded Steam URL
    else:
        raise NotImplementedError(f"launch not supported on {sys.platform}")
    log.info("Launched Civ 6 via Steam, waiting for process...")

    # macOS: click through the Aspyr launcher if it appears
    launcher_err = _click_aspyr_launcher_sync()
    if launcher_err:
        return f"WARNING: {launcher_err}"

    # Wait for actual game process (Linux/Proton can be slow to start)
    waited = _wait_for_game_process(timeout=60)
    if waited is None and sys.platform == "win32":
        # steam://run may have silently failed — try direct EXE launch
        exe_path = _find_game_exe_win32()
        if exe_path:
            log.info(
                "steam://run did not start game — launching EXE directly: %s", exe_path
            )
            subprocess.Popen([exe_path])  # noqa: S603 — hardcoded game path
            waited = _wait_for_game_process()

    if waited is None:
        return "WARNING: Game process not detected after launch. Check Steam."

    # Wait for FireTuner port to open (replaces blind sleep)
    log.info("Game process started after %ds, waiting for FireTuner port...", waited)
    if _wait_for_tuner_port():
        return (
            f"Game launched. Process started after {waited}s, FireTuner port is open."
        )

    return f"WARNING: Game launched (process after {waited}s) but FireTuner port did not open within {_PORT_POLL_TIMEOUT}s."


# ---------------------------------------------------------------------------
# OCR + GUI helpers (require pyobjc on macOS, winrt on Windows, xdotool on Linux)
# ---------------------------------------------------------------------------


def _find_game_window() -> WindowInfo | None:
    """Find the Civ 6 game window.

    Uses Quartz CGWindowList on macOS, win32gui on Windows, xdotool on Linux.
    Returns WindowInfo with window ID, bounds (screen points), and PID.
    Returns None if no matching window is found on screen.
    """
    if sys.platform == "win32":
        return _find_game_window_win32()
    if sys.platform == "linux":
        return _find_game_window_linux()
    _require_gui_deps()
    import Quartz

    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if not window_list:
        return None

    for w in window_list:
        owner = w.get("kCGWindowOwnerName", "")
        layer = w.get("kCGWindowLayer", -1)
        if layer != 0:
            continue
        if not (
            any(p in owner for p in _PROCESS_NAMES)
            or any(p in owner for p in _APP_NAME_PATTERNS)
        ):
            continue
        bounds = w.get("kCGWindowBounds", {})
        info = WindowInfo(
            window_id=w.get("kCGWindowNumber", 0),
            x=int(bounds.get("X", 0)),
            y=int(bounds.get("Y", 0)),
            w=int(bounds.get("Width", 0)),
            h=int(bounds.get("Height", 0)),
            pid=w.get("kCGWindowOwnerPID", 0),
        )
        log.info(
            "Window found: wid=%s pos=(%d,%d) size=%dx%d pid=%d",
            info.window_id,
            info.x,
            info.y,
            info.w,
            info.h,
            info.pid,
        )
        return info
    log.info("No game window found")
    return None


def _find_game_window_win32() -> WindowInfo | None:
    """Find the Civ 6 window via win32gui.EnumWindows.

    Returns the CLIENT area rect in physical pixel coordinates (matching
    _capture_window_win32 which captures the DX framebuffer at native
    resolution). Uses DPI-aware context for ClientToScreen so that the
    window origin is also in physical space.
    """
    import ctypes

    import win32gui
    import win32process

    # Switch to DPI-aware so ClientToScreen returns physical coordinates,
    # matching GetClientRect which returns physical pixels for DX windows.
    user32 = ctypes.windll.user32
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_ssize_t(-4)
    old_ctx = None
    try:
        old_ctx = user32.SetThreadDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except Exception:
        pass

    results: list[WindowInfo] = []

    def callback(hwnd: int, _: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if any(p in title for p in _APP_NAME_PATTERNS):
            # Use client rect (not window rect) to match PW_CLIENTONLY capture
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            # ClientToScreen maps client (0,0) to screen coordinates
            screen_x, screen_y = win32gui.ClientToScreen(hwnd, (cl, ct))
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            results.append(
                WindowInfo(
                    window_id=hwnd,
                    x=screen_x,
                    y=screen_y,
                    w=cr - cl,
                    h=cb - ct,
                    pid=pid,
                )
            )
        return True

    win32gui.EnumWindows(callback, None)

    if old_ctx:
        user32.SetThreadDpiAwarenessContext(ctypes.c_ssize_t(old_ctx))

    if results:
        w = results[0]
        log.info(
            "Window found: hwnd=%s pos=(%d,%d) size=%dx%d pid=%d",
            w.window_id,
            w.x,
            w.y,
            w.w,
            w.h,
            w.pid,
        )
    else:
        log.info("No game window found")

    return results[0] if results else None


def _get_frame_extents(wid: int) -> tuple[int, int, int, int]:
    """Return (left, right, top, bottom) X11 frame extents for a window.

    Queries ``_NET_FRAME_EXTENTS`` via xprop. Returns (0,0,0,0) if
    unavailable (undecorated window, Wayland without XWayland, etc.).
    """
    try:
        r = subprocess.run(
            ["xprop", "-id", str(wid), "_NET_FRAME_EXTENTS"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Output: _NET_FRAME_EXTENTS(CARDINAL) = left, right, top, bottom
        if "=" in r.stdout:
            vals = r.stdout.split("=")[1].strip().split(",")
            if len(vals) >= 4:
                extents = tuple(int(v.strip()) for v in vals[:4])
                if extents[2] > 0:  # top > 0 means decorated
                    log.info(
                        "Frame extents: left=%d right=%d top=%d bottom=%d (wid=%d)",
                        *extents,
                        wid,
                    )
                return extents
    except Exception:
        pass
    return (0, 0, 0, 0)


def _find_game_window_linux() -> WindowInfo | None:
    """Find the Civ 6 window via xdotool on Linux.

    Searches for windows whose title exactly matches "Civilization VI"
    to avoid false positives (e.g. a browser tab showing civ6-mcp docs).
    When multiple windows match, prefers the one owned by a Civ6 process.
    """
    try:
        r = subprocess.run(
            ["xdotool", "search", "--name", "Civilization VI"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None

        candidates = r.stdout.strip().split("\n")

        # Score candidates: prefer windows owned by the game process
        best: WindowInfo | None = None
        for wid_str in candidates:
            wid = int(wid_str)

            name_r = subprocess.run(
                ["xdotool", "getwindowname", str(wid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            name = name_r.stdout.strip() if name_r.returncode == 0 else ""
            if name != "Civilization VI":
                continue

            r2 = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", str(wid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            geo: dict[str, int] = {}
            for line in r2.stdout.strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    if v.isdigit():
                        geo[k] = int(v)

            r3 = subprocess.run(
                ["xdotool", "getwindowpid", str(wid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pid = int(r3.stdout.strip()) if r3.returncode == 0 else 0

            # NOTE: On GNOME/X11, xdotool getwindowgeometry already
            # returns the content area (excluding title bar). The
            # _NET_FRAME_EXTENTS property describes additional decoration
            # but xdotool accounts for it. No compensation needed.
            info = WindowInfo(
                window_id=wid,
                x=geo.get("X", 0),
                y=geo.get("Y", 0),
                w=geo.get("WIDTH", 0),
                h=geo.get("HEIGHT", 0),
                pid=pid,
            )

            # Prefer the window whose dimensions match a standard game
            # resolution (the inner client window, not the decorated one)
            if best is None:
                best = info
            elif info.w <= best.w and info.h <= best.h and info.w > 0:
                # Smaller "Civilization VI" window is the client area
                best = info

        if best:
            log.info(
                "Window found: wid=%s pos=(%d,%d) size=%dx%d pid=%d",
                best.window_id,
                best.x,
                best.y,
                best.w,
                best.h,
                best.pid,
            )
        else:
            log.info("No game window found")
        return best
    except Exception as e:
        log.debug("xdotool window search failed: %s", e)
        return None


def _capture_window(window_id: int) -> object:
    """Capture a single window as an in-memory image.

    Returns a CGImageRef on macOS, PIL Image on Windows/Linux.
    Falls back to screencapture subprocess on macOS 15+ where
    CGWindowListCreateImage is obsoleted.
    """
    if sys.platform == "win32":
        return _capture_window_win32(window_id)
    if sys.platform == "linux":
        return _capture_window_linux(window_id)
    import Quartz

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if image is not None:
        return image

    # macOS 15: CGWindowListCreateImage obsoleted — screencapture fallback
    log.debug("CGWindowListCreateImage returned nil, trying screencapture -l")
    return _capture_window_screencapture(window_id)


def _capture_window_screencapture(window_id: int) -> object:
    """Capture a window via screencapture subprocess (macOS 15 fallback).

    Uses ScreenCaptureKit internally. Returns a CGImageRef compatible
    with _ocr_vision().
    """
    import tempfile

    import Quartz
    from Foundation import NSData

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        subprocess.run(
            ["screencapture", "-x", "-l", str(window_id), path],
            check=True,
            timeout=5,
            capture_output=True,
        )
        data = NSData.dataWithContentsOfFile_(path)
        if data is None:
            raise RuntimeError(
                f"screencapture produced no output for window {window_id}"
            )
        provider = Quartz.CGDataProviderCreateWithCFData(data)
        cg_image = Quartz.CGImageCreateWithPNGDataProvider(
            provider, None, True, Quartz.kCGRenderingIntentDefault
        )
        if cg_image is None:
            raise RuntimeError(
                f"Failed to create CGImage from screencapture for window {window_id}"
            )
        return cg_image
    finally:
        os.unlink(path)


def _capture_fullscreen_screencapture() -> object | None:
    """Capture the full screen via screencapture (macOS 15 fallback).

    Returns a CGImageRef compatible with _ocr_vision(), or None on failure.
    Unlike the per-window variant, this captures the entire display.
    """
    import tempfile

    import Quartz
    from Foundation import NSData

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    try:
        subprocess.run(
            ["screencapture", "-x", path],
            check=True,
            timeout=5,
            capture_output=True,
        )
        data = NSData.dataWithContentsOfFile_(path)
        if data is None:
            log.warning("screencapture fullscreen produced no output")
            return None
        provider = Quartz.CGDataProviderCreateWithCFData(data)
        cg_image = Quartz.CGImageCreateWithPNGDataProvider(
            provider, None, True, Quartz.kCGRenderingIntentDefault
        )
        if cg_image is None:
            log.warning("Failed to create CGImage from fullscreen screencapture")
            return None
        return cg_image
    except Exception as e:
        log.warning("screencapture fullscreen failed: %s", e)
        return None
    finally:
        os.unlink(path)


def _capture_window_win32(hwnd: int) -> "PIL.Image.Image":
    """Capture a window via PrintWindow + BitBlt into a PIL Image."""
    import ctypes

    import win32gui
    import win32ui
    from PIL import Image

    # Get the client area dimensions
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w = right - left
    h = bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"Window {hwnd} has no client area ({w}x{h})")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)

    # PrintWindow with PW_CLIENTONLY|PW_RENDERFULLCONTENT for DX windows
    PW_CLIENTONLY = 0x1
    PW_RENDERFULLCONTENT = 0x2
    ctypes.windll.user32.PrintWindow(
        hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT
    )

    bmp_info = bitmap.GetInfo()
    bmp_bits = bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmp_info["bmWidth"], bmp_info["bmHeight"]),
        bmp_bits,
        "raw",
        "BGRX",
        0,
        1,
    )

    # Cleanup GDI resources
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())

    return img


def _capture_window_linux(
    window_id: int,
) -> tuple["PIL.Image.Image", dict[str, int]]:
    """Capture a window region on Linux using python-mss.

    mss captures screen regions (not window content by ID), so we use
    xdotool to get the window geometry and grab that screen region.
    The window should be in the foreground for accurate capture.
    Clamps the region to display bounds to avoid XGetImage failures.

    Returns (pil_image, capture_region) where capture_region is the exact
    screen region captured (left, top, width, height). The caller should
    use this — not the WindowInfo geometry — as the OCR origin/extent,
    because xdotool geometry may include window decorations that mss
    doesn't capture (or vice versa depending on WM).
    """
    import mss
    from PIL import Image

    r = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", str(window_id)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    geo: dict[str, int] = {}
    for line in r.stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            if v.isdigit():
                geo[k] = int(v)

    x, y = geo.get("X", 0), geo.get("Y", 0)
    w, h = geo.get("WIDTH", 0), geo.get("HEIGHT", 0)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"Window {window_id} has no geometry ({w}x{h})")

    # Clamp to display bounds — mss fails if the region exceeds the screen
    with mss.mss() as sct:
        disp = sct.monitors[0]  # virtual display (union of all monitors)
        disp_r = disp["left"] + disp["width"]
        disp_b = disp["top"] + disp["height"]
        x = max(x, disp["left"])
        y = max(y, disp["top"])
        w = min(w, disp_r - x)
        h = min(h, disp_b - y)
        if w <= 0 or h <= 0:
            raise RuntimeError(f"Window {window_id} is off-screen")

        monitor = {"left": x, "top": y, "width": w, "height": h}
        screenshot = sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        return img, monitor


def _ocr_vision(
    cg_image: object,
    origin_x: int,
    origin_y: int,
    extent_w: int,
    extent_h: int,
) -> list[tuple[str, int, int, int, int]]:
    """Run Vision OCR on a CGImage, mapping results to screen points (macOS).

    Coordinates are mapped using the capture region's known bounds in
    screen points (not retina pixels), making this retina-independent:
        screen_x = origin_x + normalized_center_x * extent_w
        screen_y = origin_y + (1 - norm_y - norm_h/2) * extent_h
    """
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        cg_image, None
    )
    success, error = handler.performRequests_error_([request], None)
    if not success:
        log.warning("Vision OCR failed: %s", error)
        return []

    results = []
    _last_rejected_ocr.clear()
    for obs in request.results() or []:
        candidate = obs.topCandidates_(1)[0]
        text = candidate.string()
        conf = candidate.confidence()  # 0.0–1.0
        bbox = obs.boundingBox()
        # Vision: normalized [0,1], origin bottom-left → screen points
        norm_cx = bbox.origin.x + bbox.size.width / 2
        norm_cy = 1 - bbox.origin.y - bbox.size.height / 2  # flip Y
        sx = origin_x + norm_cx * extent_w
        sy = origin_y + norm_cy * extent_h
        sw = bbox.size.width * extent_w
        sh = bbox.size.height * extent_h
        entry = (text, int(sx), int(sy), int(sw), int(sh))
        if conf < 0.5:
            log.debug("Vision OCR: rejected '%s' (conf=%.2f < 0.5)", text, conf)
            _last_rejected_ocr.append(entry)
            continue
        results.append(entry)
    log.info(
        "Vision OCR: %d lines found (%d rejected)",
        len(results),
        len(_last_rejected_ocr),
    )
    if results:
        log.debug("Vision OCR sample: %s", [r[0] for r in results[:5]])
    return results


def _ocr_winrt(
    pil_image: "PIL.Image.Image",
    origin_x: int,
    origin_y: int,
    extent_w: int,
    extent_h: int,
) -> list[tuple[str, int, int, int, int]]:
    """Run Windows Runtime OCR on a PIL Image, mapping results to screen points.

    Uses Windows.Media.Ocr (built-in to Windows 10+, no external binaries).
    """
    import asyncio
    import io

    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import (
        DataWriter,
        InMemoryRandomAccessStream,
    )

    # Convert PIL Image → PNG bytes → WinRT SoftwareBitmap
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    async def _run_ocr():
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            log.warning("WinRT OCR: no engine available")
            return []

        ocr_result = await engine.recognize_async(bitmap)
        return ocr_result

    # Run the async OCR — handle nested event loops
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        # Already in an async context — run in a new thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ocr_result = pool.submit(lambda: asyncio.run(_run_ocr())).result(timeout=10)
    else:
        ocr_result = asyncio.run(_run_ocr())

    if ocr_result is None:
        return []

    img_w, img_h = pil_image.size
    results = []
    for line in ocr_result.lines:
        text = line.text
        # WinRT OCR: bounding box in pixel coordinates
        words = list(line.words)
        if not words:
            continue
        # Use first word's x and union of all words for the line bounds
        x0 = min(w.bounding_rect.x for w in words)
        y0 = min(w.bounding_rect.y for w in words)
        x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        # Map pixel coords to screen coords
        cx = (x0 + x1) / 2 / img_w
        cy = (y0 + y1) / 2 / img_h
        bw = (x1 - x0) / img_w
        bh = (y1 - y0) / img_h
        sx = origin_x + cx * extent_w
        sy = origin_y + cy * extent_h
        sw = bw * extent_w
        sh = bh * extent_h
        results.append((text, int(sx), int(sy), int(sw), int(sh)))
    log.info("WinRT OCR: %d lines found", len(results))
    if results:
        log.debug("WinRT OCR sample: %s", [r[0] for r in results[:5]])
    return results


# Low-confidence OCR results from the most recent OCR call (any engine).
# Used by _find_text as a fuzzy fallback when confident results don't match.
_last_rejected_ocr: list[tuple[str, int, int, int, int]] = []


def _ocr_tesseract(
    pil_image: "PIL.Image.Image",
    origin_x: int,
    origin_y: int,
    extent_w: int,
    extent_h: int,
) -> list[tuple[str, int, int, int, int]]:
    """Run Tesseract OCR on a PIL Image, mapping results to screen points (Linux).

    Groups words into text regions using tesseract's block/par/line hierarchy,
    then splits regions where a large horizontal gap exists between words
    (common in two-column game menus where tesseract merges both columns
    into one line).
    """
    import pytesseract

    img_w, img_h = pil_image.size
    # --psm 11 (sparse text) handles scattered game UI text better than
    # the default page segmentation, especially for faded/low-contrast buttons.
    #
    # Two-pass OCR: normal image first, then a thresholded version to catch
    # faded/low-contrast UI elements (e.g. greyed-out buttons). Results are
    # merged with the normal pass taking priority (higher confidence).
    data = pytesseract.image_to_data(
        pil_image,
        config="--psm 11",
        output_type=pytesseract.Output.DICT,
    )
    # Threshold pass: grayscale → binary at brightness 80
    gray = pil_image.convert("L")
    thresh = gray.point(lambda x: 255 if x > 80 else 0)
    data_thresh = pytesseract.image_to_data(
        thresh,
        config="--psm 11",
        output_type=pytesseract.Output.DICT,
    )
    # Append threshold results with a tag so we can de-duplicate
    n_orig = len(data["text"])
    for key in data:
        data[key].extend(data_thresh[key])

    # Group words by block + par + line (not just block + line)
    lines: dict[tuple[int, int, int], list[int]] = {}
    n = len(data["text"])
    _last_rejected_ocr.clear()
    for i in range(n):
        conf = int(data["conf"][i])
        text_val = data["text"][i].strip()
        if conf < 50:
            if text_val:
                log.debug("OCR: rejected '%s' (conf=%d < 50)", text_val, conf)
                # Store with screen coords for fuzzy fallback matching
                cx = (data["left"][i] + data["width"][i] / 2) / img_w
                cy = (data["top"][i] + data["height"][i] / 2) / img_h
                _last_rejected_ocr.append(
                    (
                        text_val,
                        int(origin_x + cx * extent_w),
                        int(origin_y + cy * extent_h),
                        int(data["width"][i] / img_w * extent_w),
                        int(data["height"][i] / img_h * extent_h),
                    )
                )
            continue
        if not text_val:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    # Split lines at large horizontal gaps (e.g. two-column menus).
    # A gap > 3x the median word width suggests separate text regions.
    def _split_line(indices: list[int]) -> list[list[int]]:
        if len(indices) <= 1:
            return [indices]
        sorted_idx = sorted(indices, key=lambda i: data["left"][i])
        groups: list[list[int]] = [[sorted_idx[0]]]
        for prev, cur in zip(sorted_idx, sorted_idx[1:]):
            gap = data["left"][cur] - (data["left"][prev] + data["width"][prev])
            avg_h = (data["height"][prev] + data["height"][cur]) / 2
            # Gap larger than 3x the average character height = column break
            if gap > avg_h * 3:
                groups.append([cur])
            else:
                groups[-1].append(cur)
        return groups

    results = []
    for indices in lines.values():
        for group in _split_line(indices):
            text = " ".join(data["text"][i] for i in group)
            x0 = min(data["left"][i] for i in group)
            y0 = min(data["top"][i] for i in group)
            x1 = max(data["left"][i] + data["width"][i] for i in group)
            y1 = max(data["top"][i] + data["height"][i] for i in group)
            cx = (x0 + x1) / 2 / img_w
            cy = (y0 + y1) / 2 / img_h
            bw = (x1 - x0) / img_w
            bh = (y1 - y0) / img_h
            sx = origin_x + cx * extent_w
            sy = origin_y + cy * extent_h
            sw = bw * extent_w
            sh = bh * extent_h
            results.append((text, int(sx), int(sy), int(sw), int(sh)))

    # De-duplicate: threshold pass may re-find text the normal pass got.
    # Keep only one region per unique (normalized_text, approximate_position).
    seen: set[tuple[str, int, int]] = set()
    deduped = []
    for text, sx, sy, sw, sh in results:
        # Bucket position to ~20px grid to catch near-duplicates
        key = (_normalize(text), sx // 20, sy // 20)
        if key not in seen:
            seen.add(key)
            deduped.append((text, sx, sy, sw, sh))
    log.info(
        "Tesseract OCR: %d lines found (%d before dedup)", len(deduped), len(results)
    )
    if deduped:
        log.debug("Tesseract OCR sample: %s", [r[0] for r in deduped[:5]])
    return deduped


def _ocr_game_window(win: WindowInfo) -> list[tuple[str, int, int, int, int]]:
    """Capture the game window and OCR it. All coords in screen points."""
    if sys.platform == "win32":
        pil_image = _capture_window_win32(win.window_id)
        return _ocr_winrt(pil_image, win.x, win.y, win.w, win.h)
    if sys.platform == "linux":
        pil_image, capture_geo = _capture_window_linux(win.window_id)
        # Use the actual capture region as OCR origin/extent — not the
        # WindowInfo geometry, which may differ due to title bar / WM
        # decoration offsets on some compositors.
        return _ocr_tesseract(
            pil_image,
            capture_geo["left"],
            capture_geo["top"],
            capture_geo["width"],
            capture_geo["height"],
        )
    cg_image = _capture_window(win.window_id)
    # On macOS 15+, CGWindowListCreateImage returns nil and we fall back
    # to `screencapture -l` which captures the FULL window (title bar +
    # content + shadow).  The window bounds from Quartz (win.*) also
    # include title bar and shadow, so the mapping is 1:1.
    #
    # On older macOS, kCGWindowImageBoundsIgnoreFraming captures content
    # only — but the bounds still include framing. Detect this by
    # comparing the capture's aspect ratio to the window bounds.
    import Quartz

    img_px_w = Quartz.CGImageGetWidth(cg_image)
    img_px_h = Quartz.CGImageGetHeight(cg_image)
    if win.w and win.h and img_px_w and img_px_h:
        scale = img_px_w / win.w
        img_pt_h = img_px_h / scale
        # If capture is significantly shorter than window bounds, the
        # title bar / shadow was excluded — offset accordingly.
        gap = win.h - img_pt_h
        if gap > 5:
            return _ocr_vision(cg_image, win.x, win.y + gap, win.w, int(img_pt_h))
    return _ocr_vision(cg_image, win.x, win.y, win.w, win.h)


def _ocr_fullscreen() -> list[tuple[str, int, int, int, int]]:
    """Full-screen OCR fallback for when no game window exists.

    Used during the Aspyr launcher phase before the game process starts.
    Maps via display dimensions in points (retina-independent).
    """
    if sys.platform == "win32":
        return _ocr_fullscreen_win32()
    if sys.platform == "linux":
        return _ocr_fullscreen_linux()

    import Quartz

    main_display = Quartz.CGMainDisplayID()
    display_bounds = Quartz.CGDisplayBounds(main_display)
    disp_w = int(display_bounds.size.width)
    disp_h = int(display_bounds.size.height)
    log.info("Fullscreen OCR: capturing %dx%d (macOS)", disp_w, disp_h)

    image = Quartz.CGWindowListCreateImage(
        display_bounds,
        Quartz.kCGWindowListOptionAll,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if image is None:
        # macOS 15: CGWindowListCreateImage obsoleted — screencapture fallback
        log.info(
            "Fullscreen OCR: CGWindowListCreateImage returned nil, trying screencapture"
        )
        image = _capture_fullscreen_screencapture()
        if image is None:
            return []

    results = _ocr_vision(image, 0, 0, disp_w, disp_h)
    log.info("Fullscreen OCR: %d text regions found", len(results))
    return results


def _ocr_fullscreen_win32() -> list[tuple[str, int, int, int, int]]:
    """Full-screen capture + OCR on Windows.

    Captures in physical pixel coordinates (DPI-aware) so that results
    are in the same coordinate space as game window OCR (which captures
    DX framebuffers at native resolution).
    """
    import ctypes

    import win32gui
    import win32ui
    from PIL import Image

    user32 = ctypes.windll.user32

    # Switch to DPI-aware to get physical primary monitor dimensions.
    # This ensures the capture and coordinates match physical screen space,
    # consistent with DX fullscreen window captures.
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_ssize_t(-4)
    old_ctx = None
    try:
        old_ctx = user32.SetThreadDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except Exception:
        pass

    w = user32.GetSystemMetrics(0)  # SM_CXSCREEN (physical when DPI-aware)
    h = user32.GetSystemMetrics(1)  # SM_CYSCREEN (physical when DPI-aware)
    log.info(
        "Fullscreen OCR: capturing %dx%d (DPI-aware=%s)", w, h, old_ctx is not None
    )

    desktop_hwnd = win32gui.GetDesktopWindow()
    desktop_dc = win32gui.GetWindowDC(desktop_hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(desktop_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)
    save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), 0x00CC0020)  # SRCCOPY

    bmp_info = bitmap.GetInfo()
    bmp_bits = bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmp_info["bmWidth"], bmp_info["bmHeight"]),
        bmp_bits,
        "raw",
        "BGRX",
        0,
        1,
    )

    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(desktop_hwnd, desktop_dc)
    win32gui.DeleteObject(bitmap.GetHandle())

    if old_ctx:
        user32.SetThreadDpiAwarenessContext(ctypes.c_ssize_t(old_ctx))

    results = _ocr_winrt(img, 0, 0, w, h)
    log.info("Fullscreen OCR: %d text regions found", len(results))
    return results


def _ocr_fullscreen_linux() -> list[tuple[str, int, int, int, int]]:
    """Full-screen capture + OCR on Linux using mss + pytesseract."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        log.info(
            "Fullscreen OCR: capturing %dx%d (Linux)",
            monitor["width"],
            monitor["height"],
        )
        screenshot = sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        results = _ocr_tesseract(
            img,
            monitor["left"],
            monitor["top"],
            monitor["width"],
            monitor["height"],
        )
        log.info("Fullscreen OCR: %d text regions found", len(results))
        return results


def _normalize(s: str) -> str:
    """Normalize text for OCR comparison.

    Handles common OCR confusions:
    - Underscores ↔ spaces (game UI shows underscores, OCR reads spaces)
    - 0 ↔ O (OCR confuses zero and capital O, especially in save names like 0A_...)
    - Leading/trailing punctuation noise from tesseract (e.g. ": Load Game =:")
    """
    import re

    s = s.lower().strip().replace("_", " ").replace("0", "o")
    # Strip leading/trailing non-alphanumeric chars (OCR artifacts)
    s = re.sub(r"^[^a-z0-9]+", "", s)
    s = re.sub(r"[^a-z0-9]+$", "", s)
    return s


def _fuzzy_suffix_match(
    target: str,
    rejected: list[tuple[str, int, int, int, int]],
) -> tuple[str, int, int, int, int] | None:
    """Match by numeric suffix against low-confidence OCR results.

    Tesseract often misreads the prefix of save names (e.g. '0_MCP_0135'
    becomes 'VICP_0135') but the trailing digits are reliable. This matches
    on a suffix of 3+ digits extracted from the target.
    """
    import re

    suffix = re.search(r"(\d{3,})$", target.replace(" ", "").replace("_", ""))
    if not suffix:
        return None
    suffix_str = suffix.group(1)
    for text, x, y, w, h in rejected:
        clean = text.replace(" ", "").replace("_", "")
        if clean.endswith(suffix_str):
            return (text, x, y, w, h)
    return None


def _find_text(
    ocr_results: list[tuple[str, int, int, int, int]],
    target: str,
    exact: bool = False,
    prefer_bottom: bool = False,
    min_y_fraction: float = 0.0,
) -> tuple[str, int, int, int, int] | None:
    """Find OCR result matching target text.

    Normalizes underscores to spaces for comparison, since the game UI
    may display underscores but OCR can read them as spaces (or vice versa).

    Args:
        prefer_bottom: When True and multiple matches exist, return the one
            with the largest y coordinate (lowest on screen). Useful when a
            label and a button have the same text (e.g. "Load Game" title
            at top vs "Load Game" button at bottom).
        min_y_fraction: Reject matches in the top portion of the screen.
            0.7 means only accept matches in the bottom 30%. Requires a
            game window to determine screen bounds; ignored if no window.
    """
    target_norm = _normalize(target)
    matches = []
    for text, x, y, w, h in ocr_results:
        text_norm = _normalize(text)
        if exact and text_norm == target_norm:
            matches.append((text, x, y, w, h))
        elif not exact and target_norm in text_norm:
            matches.append((text, x, y, w, h))
    if min_y_fraction > 0 and matches:
        # Filter by screen position — use the max y from all results as proxy
        # for screen bottom (avoids needing window info here)
        max_y = max(r[2] for r in ocr_results)
        min_y = max_y * min_y_fraction
        matches = [(t, x, y, w, h) for t, x, y, w, h in matches if y >= min_y]
    if not matches:
        # Second-chance: fuzzy suffix match on rejected low-confidence results
        fuzzy = _fuzzy_suffix_match(target, _last_rejected_ocr) or _fuzzy_suffix_match(
            target, ocr_results
        )
        if fuzzy:
            log.info(
                "_find_text: '%s' not in confident results, "
                "fuzzy suffix matched rejected '%s'",
                target,
                fuzzy[0],
            )
            return fuzzy
        log.debug(
            "_find_text: '%s' not found in %d OCR results", target, len(ocr_results)
        )
        return None
    log.debug("_find_text: '%s' -> %d matches", target, len(matches))
    if prefer_bottom:
        return max(matches, key=lambda m: m[2])
    return matches[0]


def _click(x: int, y: int) -> None:
    """Click at screen coordinates (points)."""
    if sys.platform == "win32":
        return _click_win32(x, y)
    if sys.platform == "linux":
        return _click_linux(x, y)
    _require_gui_deps()
    import Quartz

    log.info("Click: screen=(%d,%d) via Quartz", x, y)
    e = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    time.sleep(0.3)
    e = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
    time.sleep(0.1)
    e = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), 0)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def _click_win32(x: int, y: int) -> None:
    """Click at screen coordinates using SendInput (Windows)."""
    import ctypes
    import ctypes.wintypes

    # MOUSEEVENTF_ABSOLUTE + MOUSEEVENTF_VIRTUALDESK maps 0-65535 to the
    # physical virtual screen. All OCR coordinates are in physical pixel
    # space (game window captures DX framebuffers at native resolution,
    # fullscreen captures use DPI-aware mode).
    user32 = ctypes.windll.user32
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_ssize_t(-4)
    old_ctx = None
    try:
        old_ctx = user32.SetThreadDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except Exception:
        pass

    vx0 = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy0 = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN

    if old_ctx:
        user32.SetThreadDpiAwarenessContext(ctypes.c_ssize_t(old_ctx))

    abs_x = int((x - vx0) * 65536 / vw)
    abs_y = int((y - vy0) * 65536 / vh)
    log.info(
        "Click: screen=(%d,%d) abs=(%d,%d) vscreen=(%d,%d)+%dx%d",
        x,
        y,
        abs_x,
        abs_y,
        vx0,
        vy0,
        vw,
        vh,
    )

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    ABS_VIRT = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    # Move
    move = INPUT(
        type=0,
        mi=MOUSEINPUT(
            dx=abs_x,
            dy=abs_y,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | ABS_VIRT,
            time=0,
            dwExtraInfo=None,
        ),
    )
    user32.SendInput(1, ctypes.byref(move), ctypes.sizeof(INPUT))
    time.sleep(0.15)

    # Click down
    down = INPUT(
        type=0,
        mi=MOUSEINPUT(
            dx=abs_x,
            dy=abs_y,
            mouseData=0,
            dwFlags=MOUSEEVENTF_LEFTDOWN | ABS_VIRT,
            time=0,
            dwExtraInfo=None,
        ),
    )
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(0.05)

    # Click up
    up = INPUT(
        type=0,
        mi=MOUSEINPUT(
            dx=abs_x,
            dy=abs_y,
            mouseData=0,
            dwFlags=MOUSEEVENTF_LEFTUP | ABS_VIRT,
            time=0,
            dwExtraInfo=None,
        ),
    )
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def _click_linux(x: int, y: int) -> None:
    """Click at screen coordinates using xdotool (Linux)."""
    log.info("Click: screen=(%d,%d) via xdotool", x, y)
    subprocess.run(
        ["xdotool", "mousemove", str(x), str(y)],
        capture_output=True,
        timeout=5,
    )
    time.sleep(0.15)
    subprocess.run(
        ["xdotool", "click", "1"],
        capture_output=True,
        timeout=5,
    )


def _is_window_focused() -> bool:
    """Check if a Civ 6 window is the frontmost application."""
    if sys.platform == "win32":
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            return any(p in title for p in _APP_NAME_PATTERNS)
        except Exception:
            return False
    if sys.platform == "linux":
        try:
            r = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode != 0:
                return False
            title = r.stdout.strip()
            return any(p in title for p in _APP_NAME_PATTERNS)
        except Exception:
            return False
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSWorkspace

        active = NSWorkspace.sharedWorkspace().frontmostApplication()
        if active is None:
            return False
        name = active.localizedName() or ""
        return any(p in name for p in _PROCESS_NAMES) or any(
            p in name for p in _APP_NAME_PATTERNS
        )
    except ImportError:
        return False


def _bring_to_front(pid: int | None = None) -> None:
    """Bring the game window to front.

    Args:
        pid: Process ID from WindowInfo. If None, looks up via
            _find_game_window().
    """
    if sys.platform == "win32":
        return _bring_to_front_win32()
    if sys.platform == "linux":
        return _bring_to_front_linux()
    if sys.platform != "darwin":
        raise NotImplementedError(f"Window focus not supported on {sys.platform}")
    try:
        from AppKit import (
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except ImportError:
        log.warning("AppKit not available for window focus")
        return

    if pid is None:
        win = _find_game_window()
        if win is None:
            log.debug("Cannot bring to front: no game window found")
            return
        pid = win.pid

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        log.warning("Cannot bring to front: no app with PID %d", pid)
        return

    for attempt in range(3):
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.3)
        if app.isActive():
            return
        if attempt < 2:
            log.debug("Window focus attempt %d failed, retrying...", attempt + 1)
    log.warning("Could not confirm window focus after 3 attempts")


def _bring_to_front_win32() -> None:
    """Bring the game window to foreground on Windows.

    Uses the thread-attach trick to bypass Windows' foreground lock:
    attach our thread to the foreground window's thread, then call
    SetForegroundWindow, then detach.
    """
    import ctypes
    import win32gui

    win = _find_game_window_win32()
    if win is None:
        log.debug("Cannot bring to front: no game window found")
        return

    hwnd = win.window_id
    user32 = ctypes.windll.user32

    # If already foreground, nothing to do
    if user32.GetForegroundWindow() == hwnd:
        return

    try:
        # Attach our thread to the foreground window's thread
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        our_thread = user32.GetCurrentThreadId()
        attached = False
        if fg_thread != our_thread:
            attached = user32.AttachThreadInput(our_thread, fg_thread, True)

        # Restore if minimized, then bring to front
        SW_RESTORE = 9
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)

        if attached:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    except Exception:
        log.debug("Could not bring window to front (non-fatal)")


def _bring_to_front_linux() -> None:
    """Bring the game window to foreground using xdotool (Linux)."""
    win = _find_game_window_linux()
    if win is None:
        log.debug("Cannot bring to front: no game window found")
        return
    subprocess.run(
        ["xdotool", "windowactivate", "--sync", str(win.window_id)],
        capture_output=True,
        timeout=5,
    )
    time.sleep(0.3)


def _wait_for_text(
    target: str,
    timeout: int = 60,
    exact: bool = False,
    interval: float = 1.5,
    prefer_bottom: bool = False,
    min_y_fraction: float = 0.0,
) -> tuple[str, int, int, int, int] | None:
    """Wait until OCR finds target text in game window.

    Captures only the game window (not the full screen). Falls back to
    full-screen capture when no game window exists (e.g. Aspyr launcher).
    """
    _require_gui_deps()
    start = time.time()
    focused = False
    last_results: list[tuple[str, int, int, int, int]] = []
    while time.time() - start < timeout:
        win = _find_game_window()
        if win is None:
            log.debug("No game window found, using full-screen OCR")
            results = _ocr_fullscreen()
            focused = False
        else:
            if not focused:
                _bring_to_front(pid=win.pid)
                time.sleep(0.3)
                focused = True
            try:
                results = _ocr_game_window(win)
            except RuntimeError:
                log.debug("Window capture failed, falling back to full-screen OCR")
                results = _ocr_fullscreen()

        last_results = results
        match = _find_text(
            results,
            target,
            exact=exact,
            prefer_bottom=prefer_bottom,
            min_y_fraction=min_y_fraction,
        )
        elapsed = time.time() - start
        if match:
            log.info("_wait_for_text: '%s' found after %.1fs", target, elapsed)
            return match
        log.debug(
            "_wait_for_text: '%s' not found (%.1fs/%ds, %d results)",
            target,
            elapsed,
            timeout,
            len(results),
        )
        time.sleep(interval)

    # Log what OCR actually saw on failure — critical for diagnosing misses
    elapsed = time.time() - start
    if last_results:
        seen = [f"'{t}'" for t, *_ in last_results[:20]]
        log.warning(
            "_wait_for_text: '%s' not found after %.0fs. Saw %d items: %s",
            target,
            elapsed,
            len(last_results),
            ", ".join(seen),
        )
    else:
        log.warning(
            "_wait_for_text: '%s' not found after %.0fs (no OCR results at all)",
            target,
            elapsed,
        )
    return None


def _click_text(
    target: str,
    timeout: int = 30,
    exact: bool = False,
    post_delay: float = 1,
    prefer_bottom: bool = False,
    min_y_fraction: float = 0.0,
    y_offset: int = 0,
) -> bool:
    """Find text via OCR and click it. Returns success.

    Args:
        y_offset: Pixels to shift the click vertically from bbox center.
            Positive = down, negative = up.  Useful when menu items are
            tightly packed and OCR bbox centers can land between items.
    """
    match = _wait_for_text(
        target,
        timeout=timeout,
        exact=exact,
        prefer_bottom=prefer_bottom,
        min_y_fraction=min_y_fraction,
    )
    if not match:
        return False
    text, x, y, w, h = match
    click_y = y + y_offset
    log.info(
        "OCR: found '%s' at (%d,%d) [%dx%d] — clicking (%d,%d)",
        text,
        x,
        y,
        w,
        h,
        x,
        click_y,
    )
    _bring_to_front()
    time.sleep(0.3)
    _click(x, click_y)
    time.sleep(post_delay)
    return True


# ---------------------------------------------------------------------------
# Crash dialog detection / dismissal (Windows only)
# ---------------------------------------------------------------------------

# Dialog signatures: (title_substring, button_text_to_click)
_CRASH_DIALOG_SIGNATURES = [
    ("Unhandled Exception", "OK"),
    ("Firaxis Crash Reporter", "No"),
]


def _detect_crash_dialogs_sync() -> list[str]:
    """Find Firaxis crash reporter / exception dialogs WITHOUT touching them.

    Returns a list of visible dialog titles.  Detection is safe any time;
    dismissal is a human decision during play (the dialogs carry the crash
    details) and is reserved for explicit recovery flows.
    """
    if sys.platform != "win32":
        return []

    try:
        import win32gui
    except ImportError:
        return []

    found: list[str] = []

    def _enum_callback(hwnd: int, results: list) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        for title_substr, _ in _CRASH_DIALOG_SIGNATURES:
            if title_substr in title:
                results.append(title)
        return True

    try:
        win32gui.EnumWindows(_enum_callback, found)
    except Exception:
        return []
    return found


async def detect_crash_dialogs() -> list[str]:
    """Async wrapper for crash dialog detection (never dismisses)."""
    return await asyncio.to_thread(_detect_crash_dialogs_sync)


def _dismiss_crash_dialogs_sync() -> list[str]:
    """Find and dismiss Firaxis crash reporter / exception dialogs (Windows).

    These are standard Win32 dialogs that appear on top of the game after
    an EXCEPTION_ACCESS_VIOLATION or similar crash.  The game continues
    running underneath but Lua calls return degraded data until the
    dialogs are dismissed.

    Only used by explicit recovery flows (restart_and_load): crash dialogs
    are the human's to read during play — end_turn aborts with a warning
    instead (see detect_crash_dialogs).

    Returns list of dismissed dialog descriptions.
    """
    if sys.platform != "win32":
        return []

    try:
        import win32con
        import win32gui
    except ImportError:
        return []

    dismissed: list[str] = []

    for title_substr, target_button in _CRASH_DIALOG_SIGNATURES:
        # Find all top-level windows matching the title
        def _enum_callback(hwnd: int, results: list) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if title_substr in title:
                results.append(hwnd)
            return True

        matches: list[int] = []
        try:
            win32gui.EnumWindows(_enum_callback, matches)
        except Exception:
            continue

        for hwnd in matches:
            # Find the target button among child windows
            def _enum_children(child_hwnd: int, buttons: list) -> bool:
                try:
                    text = win32gui.GetWindowText(child_hwnd)
                    # Strip Win32 accelerator prefix (&Yes -> Yes, &No -> No)
                    clean = text.replace("&", "")
                    if clean == target_button:
                        buttons.append(child_hwnd)
                except Exception:
                    pass
                return True

            buttons: list[int] = []
            try:
                win32gui.EnumChildWindows(hwnd, _enum_children, buttons)
            except Exception:
                continue

            if buttons:
                try:
                    # BM_CLICK message to press the button
                    win32gui.SendMessage(buttons[0], win32con.BM_CLICK, 0, 0)
                    title = win32gui.GetWindowText(hwnd)
                    dismissed.append(f"{title} (clicked '{target_button}')")
                    log.info(
                        "Dismissed crash dialog: '%s' -> clicked '%s'",
                        title,
                        target_button,
                    )
                except Exception as e:
                    log.debug("Failed to click '%s': %s", target_button, e)

    return dismissed


async def dismiss_crash_dialogs() -> list[str]:
    """Async wrapper for crash dialog dismissal."""
    return await asyncio.to_thread(_dismiss_crash_dialogs_sync)


# ---------------------------------------------------------------------------
# Save discovery
# ---------------------------------------------------------------------------


def get_latest_autosave() -> str | None:
    """Find the most recent autosave name (without extension)."""
    saves = glob.glob(os.path.join(SAVE_DIR, "AutoSave_*.Civ6Save"))
    if not saves:
        return None
    saves.sort(key=os.path.getmtime, reverse=True)
    return os.path.basename(saves[0]).replace(".Civ6Save", "")


def list_autosaves(limit: int = 10) -> list[str]:
    """List recent autosave names, newest first."""
    saves = glob.glob(os.path.join(SAVE_DIR, "AutoSave_*.Civ6Save"))
    saves.sort(key=os.path.getmtime, reverse=True)
    return [os.path.basename(s).replace(".Civ6Save", "") for s in saves[:limit]]


# ---------------------------------------------------------------------------
# Menu navigation (blocking — run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _navigate_to_save_sync(save_name: str, tab: str | None = "Autosaves") -> str:
    """Navigate: Main Menu → Single Player → Load Game → [tab] → select → Load.

    Args:
        save_name: Display name of the save (no extension).
        tab: Filter checkbox to click (e.g. "Autosaves"), or None to use
            the default view (regular saves shown by default).

    Blocking operation — takes 30-90 seconds. Returns status message.
    """
    _require_gui_deps()  # Fail fast if deps missing
    nav_start = time.time()
    steps = []

    # Launch the game if it's not running
    if not is_game_running():
        log.info("Game not running — launching before OCR navigation")
        launch_result = _launch_game_sync()
        log.info("Launch result: %s", launch_result)
        # After launch, game should be at main menu (Aspyr launcher handled by _launch_game_sync)
    else:
        # Dismiss crash dialog if present — it overlays the menu and blocks OCR
        _dismiss_crash_dialog()
        # Click through Aspyr launcher if present (macOS shows PLAY button before main menu)
        _click_aspyr_launcher_sync()

    log.info("[1/7] Waiting for main menu (Single Player)...")
    if not _click_text("Single Player", timeout=90, exact=True, post_delay=0.5):
        return "FAILED: Could not find 'Single Player' on main menu. Is the game at the main menu?"
    steps.append("Clicked Single Player")

    log.info("[2/7] Clicking 'Load Game'...")
    # y_offset nudges the click down from bbox center to avoid hitting
    # "Resume Game" directly above in the tightly-packed single player menu.
    if not _click_text("Load Game", timeout=5, exact=True, post_delay=0.5, y_offset=15):
        return "FAILED: Could not find 'Load Game' button."
    steps.append("Clicked Load Game")

    if tab is not None:
        log.info("[3/6] Clicking '%s' filter...", tab)
        if not _click_text(tab, timeout=10, exact=True, post_delay=1):
            log.info("%s filter not found — may already be active", tab)
            steps.append(f"{tab} filter (may already be active)")
        else:
            steps.append(f"Clicked {tab} filter")
    else:
        log.info("[3/6] Using default save list (no filter needed)")
        steps.append("Default save list (regular saves)")

    log.info("[4/6] Looking for save '%s'...", save_name)
    if not _click_text(save_name, timeout=15, post_delay=1):
        return (
            f"FAILED: Save '{save_name}' not found. Steps completed: {', '.join(steps)}"
        )
    steps.append(f"Selected save {save_name}")

    log.info("[5/6] Clicking 'Load Game' button (bottom, not title)...")
    # prefer_bottom picks the button over the page title. If the only match
    # is the title (y < 50% of screen), skip it — the button wasn't detected.
    if not _click_text(
        "Load Game", timeout=10, post_delay=1, prefer_bottom=True, min_y_fraction=0.7
    ):
        steps.append("Load Game button not found (may have loaded from double-click)")
    else:
        steps.append("Clicked Load Game button")

    # Wait for save to load, then click through the leader intro screen.
    #
    # NOTE (Windows): PrintWindow + SetForegroundWindow during the DX12
    # loading phase can crash the renderer.  macOS (Quartz) and Linux (mss)
    # are safe to poll during loading since they don't inject window messages.

    log.info("[6/6] Waiting 15s for save to load, then looking for CONTINUE GAME...")
    time.sleep(15)

    # Screen-aware CONTINUE detection: poll OCR and check WHAT we see.
    # If we see "Single Player" / "Multiplayer", we're on the main menu
    # (save load failed) — redo the navigation instead of blindly clicking.
    continue_found = False
    main_menu_detected = False
    poll_start = time.time()
    poll_timeout = 105
    last_status_log = 0

    while time.time() - poll_start < poll_timeout:
        elapsed = time.time() - poll_start
        win = _find_game_window()
        try:
            results = _ocr_game_window(win) if win else _ocr_fullscreen()
        except Exception:
            results = _ocr_fullscreen()

        # Check for CONTINUE (leader screen — good)
        match = _find_text(results, "CONTINUE")
        if match:
            text, x, y, w, h = match
            log.info(
                "CONTINUE wait: found '%s' at (%d,%d) after %.0fs — clicking",
                text,
                x,
                y,
                elapsed,
            )
            _bring_to_front()
            _click(x, y)
            time.sleep(3)
            continue_found = True
            steps.append("Clicked CONTINUE")
            break

        # Check for main menu (wrong screen — save load failed)
        menu_match = _find_text(results, "Single Player")
        if menu_match and elapsed > 20:  # give 20s grace for loading transition
            log.warning(
                "CONTINUE wait: ABORT — detected main menu ('Single Player' visible) "
                "after %.0fs. Save load likely failed. Will retry navigation.",
                elapsed,
            )
            main_menu_detected = True
            steps.append("ABORT: main menu detected during CONTINUE wait")
            break

        # Periodic status log (every 15s)
        if int(elapsed) // 15 > last_status_log:
            last_status_log = int(elapsed) // 15
            seen = [t for t, *_ in (results or [])[:8]]
            log.info("CONTINUE wait: %.0fs elapsed, OCR sees: %s", elapsed, seen)

        time.sleep(2.5)

    if main_menu_detected:
        # Save load failed — we're back at main menu. Redo from step 1.
        log.warning("Restarting save navigation from main menu")
        if _click_text("Single Player", timeout=15, post_delay=2):
            _click_text("Load Game", timeout=10, post_delay=1, prefer_bottom=False)
            time.sleep(1)
            if _click_text(save_name, timeout=15, post_delay=0.5):
                _click_text(
                    "Load Game",
                    timeout=10,
                    post_delay=1,
                    prefer_bottom=True,
                    min_y_fraction=0.7,
                )
                time.sleep(15)
                # One more attempt at CONTINUE
                retry_match = _wait_for_text("CONTINUE", timeout=60, interval=2.5)
                if retry_match:
                    text, x, y, w, h = retry_match
                    log.info("Retry: found CONTINUE at (%d,%d) — clicking", x, y)
                    _bring_to_front()
                    _click(x, y)
                    time.sleep(3)
                    steps.append("Retry: clicked CONTINUE after re-navigation")
                else:
                    log.warning(
                        "Retry: CONTINUE still not found — using positional click"
                    )
                    _click_continue_positional()
                    time.sleep(3)
                    steps.append("Retry: CONTINUE not found — positional click")
            else:
                steps.append("Retry: could not find save name in list")
        else:
            steps.append("Retry: could not find Single Player menu item")
    elif not continue_found:
        # OCR timeout — neither CONTINUE nor main menu detected.
        # Use positional click grid as last resort.
        log.warning(
            "OCR: CONTINUE not found after %ds — using positional click grid",
            poll_timeout,
        )
        _click_continue_positional()
        time.sleep(3)
        steps.append("CONTINUE not found via OCR — used positional click fallback")

    # Verify game loaded by checking FireTuner port
    if _is_tuner_port_open():
        steps.append("FireTuner port confirmed open")
    else:
        steps.append("WARNING: FireTuner port not open after load")

    nav_elapsed = time.time() - nav_start
    return f"Save loading ({nav_elapsed:.0f}s). Steps: {', '.join(steps)}. Wait ~10s then use get_game_overview to verify."


# ---------------------------------------------------------------------------
# Async public API (called by MCP tools)
# ---------------------------------------------------------------------------


async def kill_game() -> str:
    """Kill Civ 6 and wait for Steam to deregister."""
    return await asyncio.to_thread(_kill_game_sync)


async def launch_game() -> str:
    """Launch Civ 6 via Steam and wait for process."""
    return await asyncio.to_thread(_launch_game_sync)


async def load_save_from_menu(save_name: str | None = None) -> str:
    """Navigate the main menu to load a save via OCR.

    Args:
        save_name: Save name without extension (e.g. "AutoSave_0221" or
            "0A_GROUND_CONTROL"). If None, loads most recent autosave.

    Checks both regular saves and autosaves directories. Uses the
    appropriate tab in the Load Game screen.

    Requires the game to be at the main menu (launched but no game loaded).
    """
    if save_name is None:
        save_name = get_latest_autosave()
        if save_name is None:
            return "No autosaves found in save directory."

    # The Load Game screen shows regular saves by default.
    # "Autosaves" is a checkbox filter — only check it for autosaves.
    auto_path = os.path.join(SAVE_DIR, f"{save_name}.Civ6Save")
    single_path = os.path.join(SINGLE_SAVE_DIR, f"{save_name}.Civ6Save")

    if os.path.exists(auto_path):
        tab = "Autosaves"  # need to toggle the Autosaves checkbox
    elif os.path.exists(single_path):
        tab = None  # regular saves shown by default, no tab click needed
    else:
        available = list_autosaves(5)
        # Also list regular saves
        regular = glob.glob(os.path.join(SINGLE_SAVE_DIR, "*.Civ6Save"))
        regular = [
            os.path.basename(s).replace(".Civ6Save", "")
            for s in sorted(regular, key=os.path.getmtime, reverse=True)[:5]
        ]
        avail_str = ", ".join(available + regular) if (available or regular) else "none"
        return f"Save '{save_name}' not found. Available: {avail_str}"

    return await asyncio.to_thread(_navigate_to_save_sync, save_name, tab)


async def restart_and_load(save_name: str | None = None) -> str:
    """Kill game, relaunch, and load a save. Full recovery sequence.

    This is the recommended tool for recovering from game hangs.
    Takes 60-120 seconds total.
    """
    results = []

    # Dismiss crash dialogs before kill — they block the process from exiting
    pre_dismissed = await dismiss_crash_dialogs()
    if pre_dismissed:
        log.info("Pre-kill: dismissed %s", pre_dismissed)

    # Step 1: Kill
    kill_result = await kill_game()
    results.append(f"Kill: {kill_result}")

    # Step 2: Launch
    launch_result = await launch_game()
    results.append(f"Launch: {launch_result}")
    if "not detected" in launch_result:
        return " | ".join(results) + " | ABORTED: Game failed to launch."

    # Dismiss any lingering crash dialog from previous session (both platforms)
    await dismiss_crash_dialogs()

    # Step 3: Load save via OCR
    load_result = await load_save_from_menu(save_name)
    results.append(f"Load: {load_result}")

    return " | ".join(results)
