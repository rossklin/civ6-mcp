"""Diary feature — persistent plans written once per turn, always on.

Writes JSONL diary files stored in ~/.civ6-mcp/.  Each row has three fields:
- ``next_turn_plan`` — overwritten each turn (only the most recent matters)
- ``long_term_plans`` — passed in full each time; last write wins
- ``notes`` — append-only learnings; each call's content is appended to the
  running body (with a turn marker), so notes accumulate across turns
"""

from __future__ import annotations

import json
from pathlib import Path

DIARY_DIR = Path.home() / ".civ6-mcp"
_PLAN_FIELDS = ("next_turn_plan", "long_term_plans")


def diary_path(civ: str, seed: int, run_id: str) -> Path:
    """Per-game diary file: diary_{civ}_{seed}_{run_id}.jsonl"""
    return DIARY_DIR / f"diary_{civ}_{seed}_{run_id}.jsonl"


def get_current_plans(path: Path) -> dict[str, str]:
    """Return the most recent ``next_turn_plan``, ``long_term_plans``, and ``notes``.

    Reads the last entry in the JSONL file.  Returns ``{"next_turn_plan": "",
    "long_term_plans": "", "notes": ""}`` if the file is missing or empty.
    """
    if not path.exists():
        return {"next_turn_plan": "", "long_term_plans": "", "notes": ""}
    lines = path.read_text().strip().splitlines()
    if not lines:
        return {"next_turn_plan": "", "long_term_plans": "", "notes": ""}
    # Walk backwards to find the last valid row with plan/notes fields
    for i in range(len(lines) - 1, -1, -1):
        try:
            row = json.loads(lines[i])
            if "next_turn_plan" in row or "long_term_plans" in row or "notes" in row:
                return {
                    "next_turn_plan": row.get("next_turn_plan", ""),
                    "long_term_plans": row.get("long_term_plans", ""),
                    "notes": row.get("notes", ""),
                }
        except json.JSONDecodeError:
            continue
    return {"next_turn_plan": "", "long_term_plans": "", "notes": ""}


def read_diary_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def format_diary_entry(e: dict) -> str:
    """Format a single diary entry for display."""
    t = e.get("turn", "?")

    # New plan-format entry
    if "next_turn_plan" in e or "long_term_plans" in e or "notes" in e:
        return _format_plan_entry(e)

    # New flat format (v2) — detected by "v" key
    if "v" in e:
        return _format_flat_entry(e)

    # Legacy nested format
    return _format_legacy_entry(e)


def _format_plan_entry(e: dict) -> str:
    """Format a plan-format diary entry (next_turn_plan + long_term_plans + notes)."""
    t = e.get("turn", "?")
    ntp = e.get("next_turn_plan", "").strip()
    ltp = e.get("long_term_plans", "").strip()
    notes = e.get("notes", "").strip()

    header = f"=== Turn {t} Plans ==="
    parts = [header]
    if ltp:
        parts.append(f"Long-term Plans:\n{ltp}")
    else:
        parts.append("Long-term Plans: (none)")
    if ntp:
        parts.append(f"Next-Turn Plan:\n{ntp}")
    else:
        parts.append("Next-Turn Plan: (none)")
    if notes:
        parts.append(f"Notes:\n{notes}")
    return "\n".join(parts)


def _format_flat_entry(e: dict) -> str:
    """Format a v2 flat-key diary entry (one row per player, is_agent=True)."""
    t = e.get("turn", "?")
    r = e.get("reflections") or {}
    header = f"=== Turn {t} ==="
    score_line = (
        f"  Score: {e.get('score', '?')} | Cities: {e.get('cities', '?')} | "
        f"Pop: {e.get('pop', '?')} | "
        f"Sci: {e.get('science', '?')} | Cul: {e.get('culture', '?')} | "
        f"Gold: {e.get('gold', '?')} ({e.get('gold_per_turn', '?')}/t) | "
        f"Faith: {e.get('faith', '?')} | Favor: {e.get('favor', '?')} | "
        f"Explored: {e.get('exploration_pct', '?')}% | "
        f"Era: {e.get('era', '?')} ({e.get('era_score', '?')})"
    )
    stk = e.get("stockpiles")
    stk_line = ""
    if stk:
        parts_parts = [f"{k}: {v}" for k, v in stk.items()]
        stk_line = "\n  Resources: " + ", ".join(parts_parts)
    ref_lines = "\n".join(f"  {k}: {v}" for k, v in r.items())
    return f"{header}\n{score_line}{stk_line}\n{ref_lines}"


def _format_legacy_entry(e: dict) -> str:
    """Format a legacy nested-format diary entry."""
    t = e.get("turn", "?")
    s = e.get("score") or {}
    r = e.get("reflections") or {}
    header = f"=== Turn {t} ==="
    pop_str = f" Pop: {s['population']} |" if "population" in s else ""
    score_line = (
        f"  Score: {s.get('total', '?')} | Cities: {s.get('cities', '?')} |{pop_str} "
        f"Sci: {s.get('science', '?')} | Cul: {s.get('culture', '?')} | "
        f"Gold: {s.get('gold', '?')} ({s.get('gold_per_turn', '?')}/t) | "
        f"Faith: {s.get('faith', '?')} | Favor: {s.get('favor', '?')} | "
        f"Explored: {s.get('exploration_pct', '?')}% | "
        f"Era: {s.get('era', '?')} ({s.get('era_score', '?')})"
    )
    stk = s.get("stockpiles")
    stk_line = ""
    if stk:
        parts = []
        for name, v in stk.items():
            net = v.get("per_turn", 0) - v.get("demand", 0)
            parts.append(f"{name}: {v['amount']} ({net:+d}/t)")
        stk_line = "\n  Resources: " + ", ".join(parts)
    ref_lines = "\n".join(f"  {k}: {v}" for k, v in r.items())
    return f"{header}\n{score_line}{stk_line}\n{ref_lines}"
