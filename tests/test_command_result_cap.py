"""Per-command result cap in the batch executor, and the short policy report."""

from civ_mcp import lua as lq
from civ_mcp.command_executor import _MAX_RESULT_CHARS, _cap_result


def test_short_results_pass_through_untouched():
    text = "MOVING_TO|1,2\nRevealed 1 new tile(s):\n  (10,22): COAST"
    assert _cap_result(text) == text


def test_a_move_listing_a_dozen_tiles_is_not_cut():
    # a map line runs to roughly 200 characters
    tiles = "\n".join(f"  ({i},22): " + "x" * 180 for i in range(12))
    text = "MOVING_TO|1,2\nRevealed 12 new tile(s):\n" + tiles
    assert _cap_result(text) == text


def test_long_results_are_cut_on_a_line_break_and_say_so():
    lines = [f"line {i} " + "x" * 90 for i in range(100)]
    out = _cap_result("\n".join(lines))
    body, note = out.rsplit("\n", 1)
    assert len(body) <= _MAX_RESULT_CHARS
    kept = body.split("\n")
    assert kept == lines[: len(kept)]  # whole lines only, in order
    assert note == f"... ({100 - len(kept)} more line(s) not shown)"


def test_policy_report_modes():
    full = lq.build_policies_query()
    short = lq.build_policies_query(slots_only=True)
    assert "if false then" in full and "if true then" in short
    for q in (full, short):
        assert "__MCP" not in q
