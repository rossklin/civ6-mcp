"""Build and parse a single unified game-state query.

Concatenates all domain read queries into one Lua string sent as a single
``execute_write`` call, then splits the combined output on sentinel markers
and routes each section to the appropriate parser.

The output is a ``FullGameState`` dataclass (see ``lua.models``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from civ_mcp import lua as lq
from civ_mcp.lua.models import FullGameState

if TYPE_CHECKING:
    from civ_mcp.connection import GameConnection

log = logging.getLogger(__name__)

# Sentinel markers used between sections.
# Each must be unique and not appear in any domain's pipe-delimited output.
_SENTINEL_PREFIX = "===FS_"

# Ordered list of (section_name, build_fn, needs InGame context).
# Sections using execute_read (GameCore) are skipped in the combined
# execute_write call and can be fetched separately.
_SECTIONS: list[tuple[str, str, bool]] = [
    # (section_name, attribute_name on FullGameState, uses_write_context)
    ("overview", "overview", True),
    ("units", "units", True),
    ("cities", "cities", True),
    ("spies", "spies", True),
    ("diplomacy", "diplomacy", True),
    ("pending_deals", "pending_deals", True),
    ("diplomacy_sessions", "diplomacy_sessions", True),
    ("tech_civics", "tech_civics", True),
    ("trade_routes", "trade_routes", True),
    ("empire_resources", "empire_resources", True),
    ("strategic_map", "strategic_map", True),
    ("victory_progress", "victory_progress", True),
    ("religion_status", "religion_status", True),
    ("governors", "governors", True),
    ("policies", "policies", True),
    ("city_states", "city_states", True),
    ("builder_tasks", "builder_tasks", True),
    ("great_people", "great_people", True),
    ("world_congress", "world_congress", True),
    ("notifications", "notifications", True),
]


def _sentinel(section: str) -> str:
    """Unique sentinel string for a section."""
    return f"{_SENTINEL_PREFIX}{section.upper()}"


def build_unified_query() -> str:
    """Build a single Lua string that runs all read queries sequentially.

    Each domain query is wrapped in a ``do ... end`` block, preceded by
    a ``print()`` of the section sentinel.  The final line is the standard
    ``---END---`` sentinel.
    """
    parts: list[str] = []
    for section_name, _attr, _uses_write in _SECTIONS:
        try:
            builder_name = f"build_{section_name}_query"
            builder = getattr(lq, builder_name, None)
            if builder is None:
                log.warning("No builder %s for section %s", builder_name, section_name)
                continue
            lua_code = builder()
            if not lua_code.strip():
                continue
            # Wrap in do...end and add sentinel
            parts.append(f'print("{_sentinel(section_name)}")')
            parts.append("do")
            parts.append(lua_code)
            parts.append("end")
        except Exception:
            log.warning(
                "Failed to build query for section %s", section_name, exc_info=True
            )
    return "\n".join(parts)


async def fetch_full_state(conn: GameConnection) -> FullGameState:
    """Execute the unified query and parse all sections.

    Returns a ``FullGameState`` with every section populated (or None
    for sections that failed to parse).
    """
    state = FullGameState()
    lua_str = build_unified_query()
    if not lua_str.strip():
        log.error("Unified query is empty — no sections built")
        return state

    # Execute as single write (InGame context)
    try:
        lines = await conn.execute_write(lua_str)
    except Exception:
        log.exception("Unified state query failed")
        return state

    # Split on sentinels
    sections = _split_sections(lines)
    _parse_sections(state, sections)

    return state


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split combined output into per-section line lists."""
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines:
        line = line.rstrip("\n\r")
        if line.startswith(_SENTINEL_PREFIX):
            # Save previous section
            if current_section is not None:
                sections[current_section] = current_lines
            # Start new section
            current_section = line[len(_SENTINEL_PREFIX):].lower()
            current_lines = []
        elif line == lq.SENTINEL and current_section is not None:
            # End sentinel — save and reset
            sections[current_section] = current_lines
            current_section = None
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)

    # Don't miss the last section if no trailing sentinel
    if current_section is not None and current_lines:
        sections[current_section] = current_lines

    return sections


def _parse_sections(state: FullGameState, sections: dict[str, list[str]]) -> None:
    """Route each section's lines to the appropriate parser."""
    for section_name, lines in sections.items():
        if not lines:
            continue
        parser_name = f"parse_{section_name}_response"
        parser = getattr(lq, parser_name, None)
        if parser is None:
            # Some sections have special parsing
            _parse_special(state, section_name, lines)
            continue
        try:
            result = parser(lines)
            setattr(state, section_name, result)
        except Exception:
            log.warning(
                "Failed to parse section %s (%d lines)",
                section_name,
                len(lines),
                exc_info=True,
            )


def _parse_special(
    state: FullGameState, section_name: str, lines: list[str]
) -> None:
    """Handle sections that don't follow the standard parse_*_response pattern."""
    if section_name == "cities":
        # parse_cities_response returns (cities, distances)
        try:
            cities, distances = lq.parse_cities_response(lines)
            state.cities = cities
            state.city_distances = distances
        except Exception:
            log.warning("Failed to parse cities section", exc_info=True)

    elif section_name == "empire_resources":
        try:
            result = lq.parse_empire_resources_response(lines)
            state.empire_resources = result
        except Exception:
            log.warning("Failed to parse empire_resources section", exc_info=True)

    elif section_name == "builder_tasks":
        try:
            tasks, builders_info = lq.parse_builder_tasks(lines)
            state.builder_tasks = (tasks, builders_info)
        except Exception:
            log.warning("Failed to parse builder_tasks section", exc_info=True)

    elif section_name == "pending_deals":
        try:
            deals = lq.parse_pending_deals_response(lines)
            state.pending_deals = deals
        except Exception:
            log.warning("Failed to parse pending_deals section", exc_info=True)

    elif section_name == "diplomacy_sessions":
        try:
            sessions = lq.parse_diplomacy_sessions(lines)
            state.diplomacy_sessions = sessions
        except Exception:
            log.warning(
                "Failed to parse diplomacy_sessions section", exc_info=True
            )

    elif section_name == "religion_status":
        try:
            status = lq.parse_religion_status_response(lines)
            state.religion_status = status
        except Exception:
            log.warning("Failed to parse religion_status section", exc_info=True)
