# Game state and command output: remaining issues

Status as of 2026-11-23. This document hands over a debugging pass on the MCP
server's output, done by playing turn 85 of a test game as an agent and
recording everything that was wrong, missing or hard to read. Most of the
findings are fixed and committed. This document lists what is left, with what
is already known about each item, so the work can be finished without the
original conversation.

## 1. How to work on these

**The agreed process.** Take one issue at a time. For anything with a design
choice, write a short plan and get the user's go-ahead before editing. The
user verifies and commits each fix before the next one starts, so every task
begins from a clean git tree.

**Test setup.** The user keeps a save at the start of turn 85 with the human
on P0 (Cree) and the agent on P1 (Maya). To test:

1. Ask the user to reload the save and restart the MCP server. Python and Lua
   changes only take effect after a server restart.
2. `claim_seat(player_id=1)`.
3. Use `run_lua` to try engine calls before writing them into a template. Use
   `context: "ingame"` for anything touching `UnitManager`, `CityManager` or
   UI state.
4. `get_full_game_state` is about 45k tokens per call. Prefer a targeted
   `run_lua` query when checking one value.

The turn timer prints a "YELLOW CARD" warning on commands once the budget is
exceeded. The user has said to ignore it in debugging sessions.

**Finding the right engine call.** Do not guess API names. The game's own UI
Lua is the reference, and several bugs fixed in this pass came from guessed
calls that failed silently inside `pcall`:

    C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\Base\Assets\UI
    C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\DLC\Expansion2\UI

Find the screen that shows the value in the game, read how it computes it,
then confirm with `run_lua`.

**The output pattern to follow.** The user prefers a single Lua template that
prints the final text, over the older query, parser, model, narrator chain.
Sections already converted: Policies (`lua/policies.lua`), Governors
(`lua/governors.lua`), Map (`lua/map.lua` with the shared
`lua/tile_describe.lua`), Cities, Research and Civics, Known Civilizations.
They are appended in `GameState.get_full_game_state` in
`src/civ_mcp/game_state.py`. Convert a section only when the task already
requires reworking it. The Units section deliberately keeps its parser,
because the structured `UnitInfo` is used by the turn snapshot code.

**Two traps found in this pass.**

- A placeholder token written out in a template's comments gets replaced too.
  `str.replace` replaces every occurrence, so naming `__LUA_TILE_DESCRIBE__`
  in a header comment injected the shared snippet twice. Describe tokens in
  comments without spelling them.
- `command_executor.py` caps each command result at 4000 characters (it was
  500). A command that returns a long report must stay under that or be
  trimmed on purpose.

**Icon tags.** The user wants `[ICON_...]` tags removed from output: they
duplicate the word that follows. The pattern used so far, in Lua:

    s:gsub("%[ICON_[%w_]+%]%s*", "")

## 2. Remaining issues, in suggested order

### 2.1 Future-era civics listed as available

**Symptom.** "Available civics" in the Research and Civics section lists five
Future-era civics at turn 85 (Exodus Imperative, Cultural Hegemony, Global
Warming Mitigation, Information Warfare, Smart Power Doctrine) with 285+
turns each.

**Where.** `src/civ_mcp/lua/tech_civics.lua`, the block starting at the
comment "Available civics (prereqs satisfied)", about line 260. It builds a
prerequisite map from `GameInfo.CivicPrereqs` (about line 164) and treats a
civic with no unmet prerequisite rows as available. The Future-era civics
appear to have no rows in that table, so they pass.

**Not yet known.** How the game decides a civic can be progressed. Look at
the civics tree UI (`CivicsTree.lua` and its Expansion2 replacement) for the
check it uses, likely a call on the player's culture object. Verify with
`run_lua` that it returns false for the five civics above and true for Drama
and Poetry and Games and Recreation. Check whether the tech list has the
same problem with Future-era techs; it did not show at turn 85.

**Explicitly on ice.** Moving distant techs and civics to a separate getter
tool to shorten the state. The user decided against it for now: the state is
about 20k tokens against a 1M context, and a separate getter risks extra
tool calls. Do not implement this.

### 2.2 Thin action results from `unit_action`

**Symptom.** A successful builder improvement returns only
`ACTION|UNITOPERATION_BUILD_IMPROVEMENT done`. It does not name the
improvement, the tile or the charges left, so the agent cannot confirm what
happened without re-fetching the state.

**Where.** `src/civ_mcp/lua/unit_action.lua`, the final
`print("OK:ACTION|" .. actionId .. " done")` at about line 301.

**Suggested shape.** Append the unit's tile, and for `BUILD_IMPROVEMENT` the
improvement type and remaining build charges. Keep it one line. Whether the
improvement is readable in the same Lua call right after the request is
unverified; if it is not, report what was requested and say so.

### 2.3 Owning city on map tiles

**Symptom.** Map lines say `(owned by Maya)` but not which city owns the
tile. District placement only accepts tiles owned by the placing city, so
the agent cannot plan placements from the map. The diary notes for this game
record the agent hitting exactly this.

**Where.** `src/civ_mcp/lua/tile_describe.lua`, the "Owner annotation"
block that builds `ownerStr`. This one function serves both the map dump and
the post-move tile reports.

**Not yet known.** The engine call for a plot's owning city. Look for how
the game's plot tooltip or city panel resolves it, then verify with
`run_lua`. Suggested output: `(owned by Maya: Coba)` for the agent's own
tiles only; other civs' tiles can stay as they are.

### 2.4 Small items

Each is independent and small.

**a. Luxuries from trade deals.** Empire Resources shows `IVORY: 2` with one
improved ivory tile and `COTTON: 1` with two cotton tiles. The user confirmed
the counts are correct and differ because of an active trade deal with Cree.
The user suggested showing that in the state. Query:
`build_empire_resources_query` in `src/civ_mcp/lua/map.py` (about line 505);
text: `narrate_empire_resources` in `src/civ_mcp/narrate.py`. Suggested:
mark imported and exported amounts per luxury.

**b. "Nearby Unclaimed" distances.** Each entry reads "N tiles from <city>",
but the city is not the nearest one. Horses at (15,17) show as "7 tiles from
Wak Kab'nal" when Coba is about 4 away. Same query as (a), the `NEARBY|`
line at about line 579: it appears to report the first city in range, not
the closest.

**c. Great People ability text.** Some abilities print raw modifier ids:
`UNIT_GRANT_PLOT, +60 GOLD` and
`SINGLE_CITY_GRANT_BUILDING_IN_CITY_IGNORE, CITIES_ADJUST_BUILDING_YIELD_CHANGE (amount=1)`.
The text is assembled by `getAbility` in `src/civ_mcp/lua/great_people.py`
from modifier rows. Check what the game's Great People screen shows for the
same individuals; it likely has a localized description to use instead.

**d. Diary note markers.** Appended notes show doubled and off-by-one turn
markers, for example `[Turn 85] [Turn 84] T84: ...` for a note written at
the end of turn 84. See the note-appending code in `src/civ_mcp/diary.py`
and the `update_diary` tool in `server.py`. The marker is probably stamped
with the turn after `end_turn`, and re-stamped on already-marked text.

**e. Embassy line in the agent reference.** `src/civ_mcp/INSTRUCTIONS.md`,
the "Favor resource" section, says "Embassies are available once Writing is
researched." That is wrong; the Diplomacy section further down correctly
says Diplomatic Service. The user confirmed. Fix the sentence.

**f. Maintenance label.** The overview prints
`Maintenance: -8 (units: 1)`. The "units" figure comes from summing the
database `Maintenance` column per unit in `src/civ_mcp/lua/overview.py`
(about line 44), which does not match what the label implies. Either compute
real unit maintenance the way the game's treasury report does, or drop the
bracket.

**g. Icon tags in other sections.** Still present in Known Civilizations
(agenda text such as Brazil's "Patron of the Arts") and possibly in civ
ability text. Known Civilizations is `src/civ_mcp/lua/diplomacy.lua`; own
abilities go through `narrate_own_abilities` in `narrate.py`.

**h. Stale command name in old diaries.** Saved diary plans mention an
`improve_tile` command that no longer exists (it is now `unit_action` with
`UNITOPERATION_BUILD_IMPROVEMENT`). This is data in old diary files, not
code. No fix needed unless the user wants an alias in
`command_executor._PARAM_ALIASES`-style handling for the action name.

### 2.5 Accepted as they are

Decided by the user; do not change.

- The "New deals available." notification comes from a mod and is part of a
  generic notification list.
- MESSAGES, DEAL MAILBOX and DIPLOMACY MAILBOX sections are absent when
  empty.
- Builder Tasks suggests improvements the player lacks the tech for. The
  section carries its own warning.

## 3. Verification still owed for finished work

These fixes are committed but could not be exercised in the turn-85 save.
Check them when a game reaches the right state.

| Fix | What to check | Needs |
|---|---|---|
| Dark Age policy cards | The Policies section shows a "Dark Age" group listing only that era's cards, including under a government with no Wildcard slot. The template includes a card if the engine says it is slottable or its era window matches, because it was not confirmed whether `CanPolicyBeSlotted` is false without a Wildcard slot. | A civ in a Dark Age |
| Losing-loyalty warning | The city line adds `!! LOSING LOYALTY - flips in N turns` with a sensible N, and nothing for stable cities. | A city under loyalty pressure |
| Space project costs | A project being built shows its real cost and percentage; "ready" versus "unlocked" is right. | Late game, a Spaceport |
| Long command results | A result over 500 characters comes through whole, for example a scout move revealing many tiles. | Any such command |
| Discovery telemetry | A move that reveals tiles writes a `discovery` event through `SpatialTracker.record_discovery`. | Check the log after a revealing move |
| Per-turn city snapshot | Recorded city amenities now hold what the city has, not has-plus-needed, so they drop compared with older logs. | Compare a new log row with the city panel |

One behaviour to know about, by design: a move reports a foreign unit when
the unit's tile changes from fogged to visible during that move. A unit that
walks into an already-visible tile is not reported by moves; it shows up in
the next state fetch.

## 4. What was done, for orientation

All committed. Listed so a new reader knows which parts of the output have
already been reworked and verified live at turn 85.

- **Policies:** unlocked governments with slot layout and bonuses; all
  unlocked cards, including ones the current government has no slot for;
  obsolete cards dropped; wildcard cards no longer lost; icon tags removed.
  `change_government` returns the new slot layout.
- **Governors:** full promotion tree per governor, tagged OWNED, AVAILABLE or
  LOCKED with prerequisites; civ-specific governors excluded.
- **Victory Progress:** "us" is the player the query ran for (it was the
  first player listed, which is the human); own civ removed from Rival
  Intelligence; the broken ten-turn snapshot removed from `end_turn`.
- **Religion slots:** maximum taken from the map size, as the game's religion
  screen does.
- **Cities:** production costs are the real scaled costs; amenities show
  have, needs and net; loyalty no longer prints "turns to flip:0".
- **Moves:** each move reports newly revealed tiles and foreign units that
  came into view, using an in-game before and after comparison and the same
  tile text as the map.
- **Units:** full action ids; `[MOVING TO (x,y)]` for queued moves; reasons
  on blocked builder actions.
- **Command results:** cap raised from 500 to 4000 characters, cut at a line
  break with a note.

Tests for these are in `tests/test_victory_progress.py`,
`tests/test_move_visibility.py`, `tests/test_command_result_cap.py` and
`tests/test_unit_actions.py`. Lua templates cannot be run by the Python
suite, so the tests cover assembly, parsing and formatting only.
