# Engine and project notes

Hard-won facts about the Civ 6 engine and about how this server works around it. Most were verified against a live game; the date and turn are given where known. They are point-in-time observations, so check the code before relying on a function name or line reference.

Terms used throughout:

- **InGame context**: the UI Lua state, reached with `connection.execute_write`.
- **GameCore context**: the simulation Lua state, reached with `connection.execute_read`.
- **DAV context**: the `DiplomacyActionView` UI state, reached with `execute_in_named_state("DiplomacyActionView", ...)`.
- **Managed civ**: a civ played by an agent or the human through this server, as opposed to a built-in AI civ.

## Lua API pitfalls

### `Locale.Lookup(nil)` raises an error

`Locale.Lookup(nil)` throws instead of returning `""`. A `pcall` catches it, but an unguarded call kills the surrounding section of the script.

This bites on nullable `GameInfo` columns. `Units`, `Buildings`, `Districts` and `Improvements` rows often have a nil `Description`. `Traits` rows can have nil `Name` and `Description` for marker traits. Some civics (Political Philosophy, Mysticism, Civil Service) have a nil `Description`.

Always guard before the lookup:

```lua
local function _loc(rawKey)
    if rawKey == nil then return "" end
    return _cleanText(Locale.Lookup(rawKey))
end
```

### `Game.GetWinningTeam()` returns nil when nobody has won

It returns nil, not -1, in both the InGame and GameCore contexts (verified 2026-09-09). Wrapping the call in `pcall` does not protect a later `winTeam < 0` comparison, which fails with "attempt to compare nil with number" and ends the script with no output.

The game-over checks in `src/civ_mcp/lua/overview.py` use `Game.GetWinningTeam() or -1`. Keep that guard in any new code that reads the winning team.

### `DiplomacyManager` is nil in GameCore

Diplomacy session reads need the InGame context.

### Player and city operations land asynchronously

The effects of `UI.RequestPlayerOperation` and `CityManager.RequestOperation` are not visible in the same script round-trip. Verifying there gives false negatives. Verify on the following round-trip.

## Which context has fresh state

The two Lua contexts update at different times for different subsystems, and the direction is opposite for combat and diplomacy.

| Subsystem | Fresh in | Stale in | Read with |
|---|---|---|---|
| Unit HP after combat | GameCore | InGame | `execute_read` |
| Diplomacy validity and state | InGame | GameCore | `execute_write` |

### Combat HP

After `UnitManager.RequestOperation` issues a melee, ranged or air attack from InGame, `unit:GetDamage()` read from InGame returns the pre-combat value for the rest of the turn, however long you wait. A GameCore read (`Players[me]:GetUnits():FindID(...)`) shows the true result. In one live case a melee kill left the attacker at 88 HP in GameCore while InGame still showed 61 HP.

Unit position does update synchronously in InGame. `attack_unit` therefore reads the outcome with `build_attack_outcome_query` (GameCore) in a short polling loop.

City district defense APIs, such as wall and garrison `GetMaxDamage(DefenseTypes.*)`, exist only in InGame, so those still need an InGame read.

### Diplomacy state

After a diplomatic effect registers, such as a completed friendship, a GameCore read still returned the old state: `IsDiplomaticActionValid` true and `GetDiplomaticStateIndex` -1. The InGame read and the game UI showed validity flipped to false and both state indices at 1 (verified at T85).

Read diplomacy outcomes through InGame, as `_await_diplo_ai_answer` and `build_diplo_effect_check` do. Same-frame staleness still applies in InGame, so re-read in a separate round-trip after about 0.5 s.

## Handoff: only the local player is human

In the human-vs-agent handoff, the keeper makes one managed civ the local player at the start of its turn. The engine treats that civ as human and every other player as built-in AI for the whole turn. That includes the other managed civs.

Consequence (verified at T49): opening a diplomacy session with `DiplomacyManager.RequestSession` to any non-local player makes the engine's AI answer for that player within seconds. A `DECLARE_FRIEND` to AI Brazil was auto-accepted. The same request to the managed Cree civ was auto-rejected during the agent's turn. `SendWorkingDeal` auto-resolves deals for managed civs for the same reason. There is no Lua-visible moment between "proposed" and "AI decided".

So an "open the session now, let the managed target answer later" model cannot work. Diplomatic actions toward managed civs use the mailbox model instead:

1. The proposal is filed in Python at propose time, with no engine session.
2. The target records accept or reject on its own turn.
3. On accept, the server runs the completion recipe below while the target is the local player.

## Diplomacy sessions

Full background and implementation steps are in [DIPLO_EXECUTION_PLAN.md](DIPLO_EXECUTION_PLAN.md). The recipe was verified live on 2026-08-22 in both directions (human to agent and agent to human).

### What does not work

A proposer-side `RequestSession` + two `AddResponse(sid, me, POSITIVE)` + `CloseSession` never applies the effect. The engine silently ignores `AddResponse` on a Lua-created session. The old builder printed `OK:ACCEPTED` unconditionally, which was a false positive.

### Proposals to built-in AI civs

`build_send_diplo_action` routes the three response-able actions (friendship, delegation, embassy) to `build_send_diplo_proposal.lua`. It validates and then does only what the native UI button does: a bare `RequestSession`. The AI answers on later frames. Python polls the validity flip in `_await_diplo_ai_answer` and tears down the session and leader screen.

### Completion recipe for managed targets

`server._execute_diplo_agreement` runs this at accept time. The target must be the local player and it must be the target's turn.

1. Validate with `Players[from]:GetDiplomacy():IsDiplomaticActionValid(...)`. Cross-player reads work.
2. Bare-close any stale session for the pair.
3. `RequestSession(from, to, str)`. A non-local initiator is fine, but the session is an orphan that the UI ignores.
4. `SendAction(from, to, DiplomacyActionTypes.DECLARE_FRIEND | SET_DELEGATION | SET_EMBASSY, {})`.
5. From the DAV context, send `AddResponse(sid, to, POSITIVE)`. This does not complete the session. It makes the view adopt it: `ms_ActiveSessionID` is set and the native dialogue appears.
6. Poll `ms_ActiveSessionID` from the DAV context.
7. Send a second `AddResponse(sid, to, POSITIVE)` from the DAV context. The engine applies the effect. A human clicking Accept makes the same call.
8. Verify in a separate round-trip after about 0.5 s: the validity flip, or `HasDelegationAt`.
9. Bare `CloseSession`, wait, then dismiss the UI.

Details that matter:

- Orphan sessions never enter `HasQueuedSession`. No other response, session string or context advances them; all were tried.
- Closing and dismissing in the same frame leaves a stale `ms_ActiveSessionID` that swallows the next session's presentation.
- The shim's `__MCP_diplo_proposal_id` flag is armed after adoption polling, not before the first `AddResponse`. The shim consumes the flag on the first reported `AddResponse` involving a managed player, so arming it earlier makes our own nudge fire a spurious `MCPDIPLO_RESPONDED|POSITIVE`.
- For delegations, the 25 gold is charged to the proposer when the effect applies.
- On the raw engine path the executor keyword is `action`, not `action_name`. Mailbox routing reads `action_name`.
- The `_bail` and `_bail_lua` helpers do not close their own `if`. Every call site must append `end`. A Lua block-balance test guards this; it strips string literals per line before comment tails, because the `"---END---"` sentinel contains `--`.

### Dismissing the leader screen

Raw hide events from InGame (`SetHide`, `HideLeaderScreen`, `ShowIngameUI`) hide the DiplomacyActionView but skip its teardown. The UI froze completely when this was tried: the bulk-hide counter went negative, and the NaturalWonderPopup trick did not release it.

Only the DAV-context `Close()` global releases the input block. `handoff.build_dismiss_leader_screen_lua()` calls `pcall(Close)` first and must run in the DAV context. Denounces schedule a delayed dismiss through `game_state._cleanup_diplo_screen`. All view dismissal goes through this path; do not add raw-hide calls.

### AI-initiated diplomacy toward an agent

`end_turn._auto_clear_diplomacy` resolves it automatically (policy since 2026-08-25):

1. Pending deals are rejected with `SendWorkingDeal` first. A deal is a session, and a bare `CloseSession` leaves its offers in limbo.
2. Remaining sessions are closed silently.
3. Only wars are reported, in the turn report.

The agent-facing `get_diplomacy_sessions`, `diplomacy_respond` and `respond_to_deal` tools were removed. The methods are internal.

## Ending the turn

Findings from a full bisection at T85 of a handoff game (2026-09-09, across several reloads).

- **`ACTION_ENDTURN` behaves like the native End Turn button.** The engine first processes queued unit moves (multi-turn goto orders). If a queued move completes or fails with movement points left, the engine aborts the end-turn to ask for new orders. The request is consumed with no error, the turn does not advance, and the arriving units show as needing orders. A second `ACTION_ENDTURN` succeeds, because the first one consumed the queued orders.
- **A unit with moves left but no queued orders does not block the turn.** Neither do the `ENDTURN_BLOCKING_UNITS`, `ENDTURN_BLOCKING_CIVIC` or `ENDTURN_BLOCKING_PRODUCTION` notifications.
- **`UI.CanEndTurn()` cannot predict the abort.** It returned true in every state tested, including while the request was being swallowed.
- **Visible popups do not gate `ACTION_ENDTURN`.** A turn advanced with BoostUnlockedPopup open. The earlier popup theory was a confound: fresh reloads reset unit orders, so queued moves were pending whenever popup chains were up.
- **The fix.** `end_turn` Phase 1.5 drains popups and then re-sends `ACTION_ENDTURN` unconditionally about 4 s in if the turn has not advanced. Phase 3 remains the long-timeout fallback.
- **Stale `_pending_end_turn` flag.** Cancelling an in-flight `end_turn` call used to leave the flag set, so every later call skipped sending and only polled. It is now cleared on `CancelledError` and healed when the turn is seen to have advanced.
- **Government change.** `Players[me]:GetCulture():SetGovernmentChangeConsidered(true)` from InGame is what the native GovernmentScreen.lua does, and it cleanly removes the `CONSIDER_GOVERNMENT_CHANGE` notification. An apparent hang after it was correlation: the civic that raised the notification also left queued moves pending.
- **Units needing orders.** `UnitManager.FinishMoves(u)` from InGame did not clear them. The working path is the `skip_remaining_units` logic (fortify, heal and skip operations).
- **Popups stack.** Only the top exclusive popup is visible, and a popup's own Lua state can lag its reveal by more than a settle plus a round-trip. The `dismiss_popup` drain loop retries, bounded, while its precheck still sees one visible.

## FireTuner connection

### Unsolicited prints can arrive mid-command

The tuner socket carries command responses and unsolicited Lua `print()` output on one stream. A shim print (`MCPDEAL`, `MCPDIPLO` and so on) that arrives between a command being sent and its sentinel used to be appended to that command's result and never parsed, so the event was lost.

Seen live on 2026-08-21: the human's first "Declare friendship" click closed the leader screen, but the mailbox never received the proposal. The notify loops poll every 5 s, which makes the mid-command window likely on any UI click.

`GameConnection._dispatch_unsolicited()` in `connection.py` now routes messages from the main read loop as well as the pre-send and post-sentinel drains. A new shim that prints event lines needs no extra plumbing. Regression tests are in `tests/test_connection_unsolicited.py`. Still, never assume a print reached Python just because the Lua side ran.

## MCP tool output size

Claude Code moves MCP tool results larger than about 50 KB to a file and inlines only a 2 KB preview. `MAX_MCP_OUTPUT_TOKENS` does not change this; it caps the token stream, which is a separate mechanism. Setting it to 100000 had no effect.

The fix is a per-tool annotation, with a hard ceiling of 500,000 characters:

```python
@mcp.tool(
    annotations={"readOnlyHint": True},
    meta={"anthropic/maxResultSizeChars": 500000},
)
```

`meta=` maps to `Tool._meta` in the `tools/list` response. It is applied to `get_full_game_state` and `get_agent_reference` in `src/civ_mcp/server.py`. The tool list is built at startup, so a change needs a server restart.

## GameInfo data sources

### Civ and leader unique abilities

Fetched inside `build_diplomacy_query` in `src/civ_mcp/lua/diplomacy.py` and shown in the "Known Civilizations" section of the full game state. Our own civ's abilities come from `build_own_abilities_query` and appear under "Our Civilization".

Per civ, the Lua resolves:

- `GameInfo.CivilizationTraits` (`CivilizationType`, `TraitType`) and `GameInfo.LeaderTraits` (`LeaderType`, `TraitType`), then `GameInfo.Traits[traitType]` (`Name`, `Description`).
- Unique units, buildings, districts and improvements through the `TraitType` column on `GameInfo.Units`, `Buildings`, `Districts` and `Improvements`.

These column names were verified in a live game at T60. Cree, Rome and Brazil each resolved their abilities and uniques.

Notes:

- Marker traits are filtered out by requiring a non-empty `Traits.Description`.
- Descriptions are cleaned of `[ICON_*]`, `[NEWLINE]` and `[COLOR]` tokens by a `_cleanText` Lua helper. Agendas are not cleaned.
- All lookups go through the nil-safe `_loc` helper described above.

### Tech and civic ability text

The abilities shown in the tech and civic trees come from the sources below. See `descStrFor`, `buildUnlocks` and `awardsByCivic` in `src/civ_mcp/lua/tech_civics.lua`.

1. **The `Description` field** on `GameInfo.Technologies` and `GameInfo.Civics` rows. This is the authored prose and covers almost all ability text, for example "Allows Builders to embark." (Sailing) and "Allows clearing of Marsh, and harvesting of Bananas." (Irrigation). Do not hand-build clear and harvest text from `Features.RemoveTech` and `Resource_Harvests.PrereqTech`; `Description` already contains it. Strip the `[ICON_*]` and `[NEWLINE]` tags.
2. **Unlockable items.** The canonical API is `GetUnlockablesForTech` and `GetUnlockablesForCivic` in `Base/Assets/UI/TechAndCivicUnlockables.lua`. That file has no `include()` calls, so it is usable from InGame. It matches `PrereqTech`/`PrereqCivic` or `InitiatorPrereqTech`/`InitiatorPrereqCivic`, and covers governments, policies, buildings, districts, units, improvements, projects, resources and diplomatic actions. Diplomatic actions use `InitiatorPrereqCivic`, for example Joint War from Foreign Trade and Alliance from Civil Service. Skip `DiplomaticActions` rows with a nil `Name`; they are alliance subtypes.
3. **Envoy awards.** `GameInfo.CivicModifiers` joined to `GameInfo.Modifiers` where `ModifierType == "MODIFIER_PLAYER_GRANT_INFLUENCE_TOKEN"`. The count is the `Amount` argument in `GameInfo.ModifierArguments`. This is the only modifier type the civic tree draws as its own icon.
4. **Governor titles.** The same join with `ModifierType == "MODIFIER_PLAYER_ADJUST_GOVERNOR_POINTS"` and the `Delta` argument. The tree shows no icon for these, but we surface them. Thirteen civics grant one.

Governments and policies are unlocked only by civics, never by techs.

Relevant game UI source files, under `Base/Assets/UI`: `TechAndCivicSupport.lua`, `TechAndCivicUnlockables.lua`, `Screens/TechTree.lua` and `Screens/CivicsTree.lua`.
