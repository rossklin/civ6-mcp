"""Victory progress: "us" is the player the query ran for, not player 0.

In a shared game the human is player 0 and the agent plays another seat, so
the narrator must take our identity from the query's ME| line.
"""

from civ_mcp import narrate as nr
from civ_mcp.lua.victory import (
    build_victory_progress_query,
    parse_victory_progress_response,
)

# PLAYER|pid|name|score|sciVP|sciNeeded|diploVP|tourism|mil|techs|civics|relCities|stay|hasRel|cities|sci|cul|gold
_LINES = [
    "ME|1",
    "PLAYER|0|Cree|135|0|50|0|0|67|10|9|1|5|true|3|12.0|14.0|18.0",
    "PLAYER|1|Maya|106|0|50|0|0|70|10|8|0|3|false|3|18.4|13.4|20.0",
    "CULTURE|Cree|0|5|false",
    "PLAYER|2|Rome|121|0|50|0|0|113|7|8|2|7|true|4|9.0|16.0|12.0",
    "CULTURE|Rome|0|7|false",
]


def _section(text: str, header: str) -> list[str]:
    """Lines of one block of the narration, from its header to the next blank."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    block = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        block.append(line)
    return block


def test_query_reports_the_local_player():
    assert 'print("ME|" .. me)' in build_victory_progress_query()


def test_parser_reads_our_player_id():
    assert parse_victory_progress_response(_LINES).our_player_id == 1


def test_marker_is_on_our_civ_not_player_zero():
    text = nr.narrate_victory_progress(parse_victory_progress_response(_LINES))
    marked = [line for line in text.split("\n") if line.endswith("<--")]
    assert marked, "expected our civ to be marked"
    assert all(line.strip().startswith("Maya:") for line in marked)


def test_our_domestic_tourists_are_ours():
    text = nr.narrate_victory_progress(parse_victory_progress_response(_LINES))
    assert "Our domestic tourists: 3" in text


def test_rival_intelligence_excludes_us():
    text = nr.narrate_victory_progress(parse_victory_progress_response(_LINES))
    rivals = _section(text, "RIVAL INTELLIGENCE")
    assert [line.strip().split(":")[0] for line in rivals] == ["Cree", "Rome"]


def test_no_marker_when_our_player_is_unknown():
    vp = parse_victory_progress_response(_LINES[1:])  # no ME| line
    assert vp.our_player_id == -1
    text = nr.narrate_victory_progress(vp)
    assert "<--" not in text
    assert "Our domestic tourists" not in text
