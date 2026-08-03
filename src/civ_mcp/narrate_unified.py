"""Format a ``FullGameState`` into a single LLM-readable text block.

Each section gets a ``## Header`` and delegates to the existing
``narrate_*()`` functions in ``civ_mcp.narrate``.
"""

from __future__ import annotations

from civ_mcp import narrate as nr
from civ_mcp.lua.models import FullGameState


def narrate_full_state(state: FullGameState) -> str:
    """Render a FullGameState into a single formatted string.

    Sections with no data are omitted.  The output is designed for
    modern LLMs with large context windows — verbose but complete.
    """
    parts: list[str] = []

    # ── Game Overview ──────────────────────────────────────────────
    if state.overview is not None:
        parts.append("## Game Overview\n")
        parts.append(nr.narrate_overview(state.overview))

    # ── Units ──────────────────────────────────────────────────────
    if state.units:
        parts.append("\n## Units\n")
        parts.append(nr.narrate_units(state.units, None, state.trade_routes))

    # ── Spies ──────────────────────────────────────────────────────
    if state.spies:
        parts.append("\n## Spies\n")
        parts.append(nr.narrate_spies(state.spies))

    # ── Cities ─────────────────────────────────────────────────────
    if state.cities:
        parts.append("\n## Cities\n")
        parts.append(nr.narrate_cities(state.cities, state.city_distances))

    # ── Diplomacy ──────────────────────────────────────────────────
    if state.diplomacy:
        parts.append("\n## Diplomacy\n")
        parts.append(nr.narrate_diplomacy(state.diplomacy))

    # ── Pending Diplomacy Sessions ─────────────────────────────────
    if state.diplomacy_sessions:
        parts.append("\n## Pending Diplomacy\n")
        parts.append(nr.narrate_diplomacy_sessions(state.diplomacy_sessions))

    # ── Pending Trade Deals ────────────────────────────────────────
    if state.pending_deals:
        parts.append("\n## Pending Trades\n")
        parts.append(nr.narrate_pending_deals(state.pending_deals))

    # ── Research ───────────────────────────────────────────────────
    if state.tech_civics is not None:
        parts.append("\n## Research & Civics\n")
        parts.append(nr.narrate_tech_civics(state.tech_civics))

    # ── Trade Routes ───────────────────────────────────────────────
    if state.trade_routes is not None:
        parts.append("\n## Trade Routes\n")
        parts.append(nr.narrate_trade_routes(state.trade_routes))

    # ── Empire Resources ───────────────────────────────────────────
    if state.empire_resources is not None:
        parts.append("\n## Empire Resources\n")
        stockpiles, owned, nearby, luxuries = state.empire_resources
        parts.append(nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries))

    # ── Builder Tasks ──────────────────────────────────────────────
    if state.builder_tasks is not None:
        tasks, builders_info = state.builder_tasks
        if tasks or builders_info:
            parts.append("\n## Builder Tasks\n")
            parts.append(nr.narrate_builder_tasks(tasks, builders_info))

    # ── Governors ──────────────────────────────────────────────────
    if state.governors is not None:
        parts.append("\n## Governors\n")
        parts.append(nr.narrate_governors(state.governors))

    # ── Policies ───────────────────────────────────────────────────
    if state.policies is not None:
        parts.append("\n## Policies\n")
        parts.append(nr.narrate_policies(state.policies))

    # ── City-States ────────────────────────────────────────────────
    if state.city_states is not None:
        parts.append("\n## City-States\n")
        parts.append(nr.narrate_city_states(state.city_states))

    # ── Great People ───────────────────────────────────────────────
    if state.great_people:
        parts.append("\n## Great People\n")
        parts.append(nr.narrate_great_people(state.great_people))

    # ── Victory Progress ───────────────────────────────────────────
    if state.victory_progress is not None:
        parts.append("\n## Victory Progress\n")
        parts.append(nr.narrate_victory_progress(state.victory_progress))

    # ── Religion ───────────────────────────────────────────────────
    if state.religion_status is not None:
        parts.append("\n## Religion\n")
        parts.append(nr.narrate_religion_status(state.religion_status))

    # ── World Congress ─────────────────────────────────────────────
    if state.world_congress is not None:
        parts.append("\n## World Congress\n")
        parts.append(nr.narrate_world_congress(state.world_congress))

    # ── Notifications ──────────────────────────────────────────────
    if state.notifications:
        parts.append("\n## Notifications\n")
        parts.append(nr.narrate_notifications(state.notifications))

    # ── Strategic Map ──────────────────────────────────────────────
    if state.strategic_map is not None:
        parts.append("\n## Strategic Map\n")
        parts.append(nr.narrate_strategic_map(state.strategic_map))

    return "\n".join(parts)
