"""Execute a batch of game commands sequentially.

Each command is a dict with ``action`` (matching a ``GameState`` method name)
and ``params`` (keyword arguments for that method).

Unit IDs (composite) in params are automatically converted to unit indices
via ``unit_id % 65536`` before calling the underlying method.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from civ_mcp.game_state import GameState

log = logging.getLogger(__name__)

# Commands excluded from the dispatcher — these are standalone tools or
# handled by the server layer.
_EXCLUDED_COMMANDS: frozenset[str] = frozenset(
    {
        "end_turn",
        "list_saves",
        "load_save",
        "load_game_save",
        "get_game_identity",
        "get_game_overview",
        "get_diary_snapshot",
        "get_rival_snapshot",
        "check_game_over",
        # All get_* read queries are excluded — they're in get_full_game_state
    }
)

# Params whose name ends with "unit_id" and should be converted from
# composite ID to unit_index.
_UNIT_ID_PARAMS: frozenset[str] = frozenset({"unit_id", "trader_id"})


def _convert_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convert composite unit IDs to raw indices and handle other transforms."""
    converted: dict[str, Any] = {}
    for key, value in params.items():
        if key in _UNIT_ID_PARAMS and isinstance(value, int):
            converted[key] = value % 65536
        elif key == "accept" and isinstance(value, str):
            converted[key] = value.lower() in ("true", "yes", "1", "accept")
        else:
            converted[key] = value
    return converted


async def execute_commands(gs: GameState, commands_json: str) -> str:
    """Parse and execute a JSON array of commands.

    Args:
        gs: The GameState instance for this seat.
        commands_json: JSON string like
            ``[{"action": "move_unit", "params": {...}}, ...]``.

    Returns:
        A multi-line string with one line per command result.
    """
    try:
        commands: list[dict[str, Any]] = json.loads(commands_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"

    if not isinstance(commands, list):
        return "Error: commands_json must be a JSON array of command objects."

    if not commands:
        return "No commands to execute."

    results: list[str] = []
    total = len(commands)

    for i, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            results.append(f"[{i + 1}/{total}] SKIPPED: not a dict")
            continue

        action = cmd.get("action", "")
        params = cmd.get("params", {})

        if not action:
            results.append(f"[{i + 1}/{total}] SKIPPED: no action specified")
            continue

        if not isinstance(params, dict):
            results.append(f"[{i + 1}/{total}] {action}: SKIPPED: params must be a dict")
            continue

        if action in _EXCLUDED_COMMANDS:
            results.append(
                f"[{i + 1}/{total}] {action}: REFUSED — use the standalone tool instead"
            )
            continue

        # Convert params (unit_id → unit_index, etc.)
        try:
            converted = _convert_params(params)
        except Exception:
            results.append(
                f"[{i + 1}/{total}] {action}: ERROR converting params"
            )
            continue

        # Look up method on GameState
        method = getattr(gs, action, None)
        if method is None:
            results.append(
                f"[{i + 1}/{total}] {action}: UNKNOWN — no such command"
            )
            continue

        # Execute
        try:
            result = await method(**converted)
            # Truncate very long results for readability
            if isinstance(result, str) and len(result) > 500:
                result = result[:497] + "..."
            results.append(f"[{i + 1}/{total}] {action}: {result}")
        except TypeError as e:
            results.append(
                f"[{i + 1}/{total}] {action}: BAD PARAMS — {e}"
            )
        except Exception as e:
            results.append(
                f"[{i + 1}/{total}] {action}: ERROR — {e}"
            )

    return "\n".join(results)
