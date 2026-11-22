"""Post-move visibility report: builders, parsers and the appended text.

The Lua itself only runs in a live game; these tests cover the Python side:
that the templates are assembled completely, that the diff/describe output
is parsed, and that the text appended to a move result is right.
"""

from civ_mcp import lua as lq
from civ_mcp.game_state import _format_move_visibility
from civ_mcp.lua._helpers import SENTINEL


def test_pre_move_snapshot_is_one_query_ending_in_the_goody_snapshot():
    q = lq.build_pre_move_snapshot_query(3, 4)
    assert "__civmcp_vis = {" in q  # visibility snapshot is included
    assert "GOODY_SEQ|" in q  # ...followed by the tribal-village snapshot
    assert q.index("__civmcp_vis = {") < q.index("GOODY_SEQ|")
    assert q.count(f'print("{SENTINEL}")') == 1  # a single terminator
    assert "__MCP" not in q and "__TARGET" not in q


def test_visibility_diff_query_has_no_placeholders():
    q = lq.build_visibility_diff_query()
    assert "__MCP" not in q
    assert "VIS_NEW|" in q and "VIS_UNITS|" in q


def test_map_and_describe_queries_share_the_tile_describer():
    map_q = lq.build_map_query()
    desc_q = lq.build_describe_tiles_query([(10, 22), (24, 20)])
    for q in (map_q, desc_q):
        # injected exactly once: a token left in a comment would inject it again
        assert q.count("local function describeTile(plot)") == 1
        assert "__LUA_TILE_DESCRIBE__" not in q and "__MCP" not in q
    assert "{10, 22}, {24, 20}" in desc_q


def test_parse_visibility_diff():
    new, units = lq.parse_visibility_diff(["VIS_NEW|10,22;11,23", "VIS_UNITS|24,20"])
    assert new == [(10, 22), (11, 23)]
    assert units == [(24, 20)]


def test_parse_visibility_diff_empty_and_missing_snapshot():
    assert lq.parse_visibility_diff(["VIS_NEW|", "VIS_UNITS|"]) == ([], [])
    assert lq.parse_visibility_diff(["VIS_NONE"]) == ([], [])


def test_parse_described_tiles_keeps_pipes_inside_the_line():
    out = lq.parse_described_tiles(["TILE|10,22|  (10,22): COAST {F:1 P:0 G:1}, a|b"])
    assert out == {(10, 22): "  (10,22): COAST {F:1 P:0 G:1}, a|b"}


def test_format_lists_new_tiles_and_units_in_view():
    described = {
        (10, 22): "  (10,22): COAST, neighbours: E (11,22)",
        (24, 20): "  (24,20): PLAINS **[Rome SCOUT(76/100hp)]**, neighbours: E (25,20)",
    }
    text = _format_move_visibility([(10, 22)], [(24, 20)], described)
    assert text == (
        "\nRevealed 1 new tile(s):"
        "\n  (10,22): COAST, neighbours: E (11,22)"
        "\nForeign units now in view:"
        "\n  (24,20): PLAINS **[Rome SCOUT(76/100hp)]**, neighbours: E (25,20)"
    )


def test_format_keeps_count_truthful_when_a_description_is_missing():
    text = _format_move_visibility([(1, 2), (3, 4)], [], {(1, 2): "  (1,2): GRASS"})
    assert "Revealed 2 new tile(s):" in text
    assert "  (3,4): (no detail)" in text
    assert "Foreign units" not in text


def test_format_is_empty_when_nothing_changed():
    assert _format_move_visibility([], [], {}) == ""
