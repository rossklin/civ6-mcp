# Human vs Agent: Turn-Boundary Local-Player Handoff

> **Status: Validated by live experiment (2026-07-28). Not yet implemented.**
>
> Goal: a human plays Civ 6 in the normal UI while an MCP-driven agent plays a
> rival civilization in the same single-player game, alternating turns.

## Summary

This works. The complete loop was demonstrated end-to-end against a live game
(Vietnam P0 = human, Mongolia P1 = agent, Kongo P2 = built-in AI, turns 5–9).

**No mod is required.** The Civ 6 event tables are reachable from the FireTuner
Lua states with a working `.Add`, so the interception hook can be registered
over the existing TCP connection at startup.

The entire mechanism is one persistent handler:

```lua
GameEvents.PlayerTurnStarted.Add(function(pid)
  if pid == HUMAN_ID or pid == AGENT_ID then
    PlayerManager.SetLocalPlayerAndObserver(pid)
  end
end)
```

Because the agent's civ becomes a genuine local human player, **every existing
MCP tool works unmodified** — the ~66 `Game.GetLocalPlayer()` call sites in
`src/civ_mcp/lua/` resolve to whichever civ currently holds the human slot.

## The Core Rule

Civ 6 single-player permits exactly one human player at a time, and
`PlayerManager.SetLocalPlayerAndObserver(N)` **swaps** the human designation
rather than granting it:

- The target player becomes `IsHuman() == true`, `IsTurnActive() == true`, with
  full movement, and the engine waits indefinitely for its input.
- The player being left behind becomes AI-controlled.

The consequence that dictates the whole design:

> **Never switch away from a civ whose turn is still active.** If you do, the
> built-in AI immediately plays that civ's remaining turn.

Switching only at turn boundaries — when the abandoned civ is already
turn-complete — avoids this entirely. That is the difference between the design
below and the failed first attempt described in "Rejected approaches".

## Turn Event Ordering

Captured from a live turn cycle by registering probe handlers via the tuner.
GameCore and InGame events interleave as follows for each player:

```
GameEvents.PlayerTurnStarted(pid)         <-- HOOK HERE (GameCore state)
GameEvents.PlayerTurnStartComplete(pid)
    *** built-in AI acts for pid here ***
Events.PlayerTurnActivated(pid, true)     (InGame state)
Events.PlayerTurnDeactivated(pid)
```

Players are processed strictly sequentially: `0, 1, 2, … 5, 62, 63`, then the
turn counter increments and it starts again at player 0. (62/63 are barbarians
and free cities.)

`PlayerTurnStarted` is the correct hook. At that point the engine has already
set `IsTurnActive() == true` for the player but the AI has **not** yet acted, so
the switch lands cleanly. The timing is not tight — this is not a race in
practice.

## The Loop

| # | Event | Action | Result |
|---|-------|--------|--------|
| 1 | Human plays turn N in the UI, presses End Turn | — | P0 turn-complete, no longer active |
| 2 | `PlayerTurnStarted(AGENT)` fires | Hook switches local player to AGENT | Engine **halts** and waits for human input as AGENT; full movement available |
| 3 | Agent plays via MCP tools | Normal tool calls | Unlimited thinking time — the engine is idle |
| 4 | Agent calls `end_turn` | `ACTION_ENDTURN` | AGENT turn-complete |
| 5 | Remaining AI civs process | — | Normal AI turns |
| 6 | `PlayerTurnStarted(HUMAN)` fires, turn N+1 | Hook switches local player back | Human resumes, units untouched |

## Experimental Evidence

All results from the live session on 2026-07-28.

**API surface.** Probed via `run_lua`:

- `PlayerManager.SetLocalPlayerAndObserver` — exists, **GameCore context only**
  (`PlayerManager` is `nil` in the InGame state).
- `Game.SetLocalPlayer` — does not exist. `UI.SetLocalPlayer` — does not exist.
- Player object exposes `IsHuman`, `IsTurnActive`, `IsTurnActiveComplete`.
  There is **no `SetTurnActive`** — an AI turn cannot be held open directly.
- `GameEvents` is present in GameCore, absent in InGame. `Events` is present in
  InGame (and empty in GameCore). Both are lazy proxy tables: `pairs()`
  under-reports, but direct access by name returns a table with working
  `Add`/`Remove`/`RemoveAll`.

**Interception, at `PlayerTurnStarted(1)` after the human ended turn 8:**

```
PRE    p1_human=false p1_active=true  p0_active=false local=0
SWITCH ok=true err=nil
POST   p1_human=true  p1_active=true  p0_active=false local=1
```

The game then sat at turn 8 with `local=1` indefinitely. It did not advance.

**Agent command path.** With local player switched to P1, a normal InGame
operation executed correctly:

```lua
UnitManager.CanStartOperation(u, UnitOperationTypes.MOVE_TO, nil, params)  --> true
UnitManager.RequestOperation(u, UnitOperationTypes.MOVE_TO, params)
-- warrior 131073: (39,8) -> (39,9), moves 2 -> 1
```

**Handback.** After the agent ended P1's turn, remaining AI civs processed and
turn 9 began with `local=0`, `P0 human=true active=true`, and P0's units at
exactly their recorded pre-handoff positions (Scout `(39,14)` mv=3, Warrior
`(37,17)` mv=2). The AI did not touch the human's civ.

## Rejected Approaches

**Multiplayer / hotseat — impossible.** FireTuner's listener is disabled for all
multiplayer modes. Reported behavior: the tuner connects at the main menu and
disconnects the moment a hotseat game loads. This is deliberate anti-cheat with
no known config override.

**Holding an AI turn open — impossible from Lua.** There is no `SetTurnActive`
setter. Community consensus is that suppressing or forcing gamecore turn
scheduling requires DLL-level modification. Freezing units with `FinishMoves()`
prevents the AI acting but makes the turn end *sooner*, not later — it does not
make the engine wait.

**Promoting an AI to a human slot mid-game — does not work.** In the InGame
context `PlayerConfigurations[N]` exposes setters (`SetSlotStatus`,
`SetMajorCiv`, `SetHotseatName`, …). Calling
`PlayerConfigurations[1]:SetSlotStatus(SlotStatus.SS_TAKEN)` succeeds and flips
the config layer (`GetSlotStatus()` → 3, `IsHuman()` → true), but the gamecore
scheduler ignores it entirely: `Players[1]:IsHuman()` stayed `false` and the
turn passed 6→7 instantly with the AI playing Mongolia. The two layers are
independent and the gamecore one is authoritative.

**Mid-turn switching — actively harmful.** The first attempt switched to P1
while P0's turn was still active. P0 became AI-controlled with an active turn
and the built-in AI immediately played the human's turn 5, advancing the game to
turn 6. This is the failure mode the turn-boundary design exists to avoid.

## Impact on `docs/agent-vs-agent.md`

That proposal should be revised. Two of its load-bearing assumptions are wrong:

1. Its Phase 1 has Agent A holding the human slot while Agent B is puppeteered
   during Agent B's turn. Swap semantics make this impossible — Agent A's civ
   would be AI-played. The turn-boundary handoff is the fix.
2. Its Phase 2 ("parametrize all 66 `build_*` call sites by `player_id`") is
   **unnecessary**. Under a local-player handoff, `Game.GetLocalPlayer()`
   already resolves to the correct civ. This removes the single largest chunk of
   work in that document.

Its GameCore/InGame API tables and the `PlayerTurnStartComplete` analysis remain
accurate and useful.

## Implementation Plan

### Phase 1 — Handoff installer

New module, e.g. `src/civ_mcp/handoff.py`.

- `install(conn, human_id, agent_id)` — registers the `PlayerTurnStarted`
  handler in the **gamecore** state. Keep the closure in a Lua global
  (`__handoff_fn`) so it can be passed to `GameEvents.PlayerTurnStarted.Remove`
  later. Do **not** use `RemoveAll()` — it strips the game's own listeners.
- Store a `__handoff_enabled` global the handler checks on every call, so the
  handoff can be disabled without needing the original function reference.
- Handlers live in the tuner Lua state and are destroyed on game load.
  Re-register on connect and after every `load_game_save` / `restart_and_load`
  in `game_lifecycle.py`.
- Have the handler append to a bounded ring buffer global for diagnostics
  (`turn`, `activated player`, `local before/after`).

### Phase 2 — Turn-ownership gating

Without this, nothing stops the human and the agent acting on the same turn.

- Add a `current_owner()` check reading `Game.GetLocalPlayer()`.
- Every write tool refuses with a clear message when
  `GetLocalPlayer() != agent_id`. Read tools may stay open, subject to the
  fairness question below.
- `end_turn` needs care: it must end only the *agent's* turn. Confirm its
  existing blocker-resolution machinery in `end_turn.py` behaves correctly —
  it should, since the agent is a real local human hitting real
  `EndTurnBlockingTypes`.

### Phase 3 — Configuration and UX

- Config for `human_id` / `agent_id` (default 0 / 1). Consider resolving the
  agent civ by name rather than index.
- Surface the current owner in `get_game_overview` so the agent can tell whether
  it is on the clock.
- Decide what happens if the human alt-tabs in during the agent's turn. The
  screen renders the agent civ's territory and fog while it thinks; this is
  inherent to the approach and cannot be fixed, only documented.

### Phase 4 — Open questions requiring test

1. **Does the AI still choose the agent civ's research and production?** The
   hook catches unit actions, but it is unverified whether the engine commits
   research/production selections for P1 before `PlayerTurnStarted` fires. Test:
   read P1's current research and city build queue at the moment of handoff and
   check whether they were set without agent involvement. If they are, the agent
   must overwrite them each turn.
2. **Diplomacy.** A civ that becomes human mid-game may be approached
   differently by other AIs. AI-initiated diplomacy sessions targeting the agent
   need routing through `get_pending_diplomacy` / `respond_to_diplomacy` during
   the agent's window.
3. **Fairness.** The agent has gamecore read access to the entire map
   regardless of its own visibility. Enforcing fog of war requires visibility
   checks in the query layer; the engine will not do it.
4. **Save/load.** Confirm a game saved mid-handoff (agent holding the human
   slot) reloads sanely, and that the local player on load is the human.
5. **Longevity.** The demonstration covered one full cycle. Run 20+ turns to
   check for drift, leaked handlers, or state corruption.

## Reproducing the Experiment

With a game running and the tuner connected, via `run_lua`:

```lua
-- gamecore: arm the handoff
__log = {}
GameEvents.PlayerTurnStarted.Add(function(pid)
  if pid == 0 or pid == 1 then
    pcall(function() PlayerManager.SetLocalPlayerAndObserver(pid) end)
    __log[#__log+1] = string.format("turn=%s activate=P%d local=%s human=%s",
      tostring(Game.GetCurrentGameTurn()), pid,
      tostring(Game.GetLocalPlayer()), tostring(Players[pid]:IsHuman()))
  end
end)
```

```lua
-- ingame: end the human turn, then poll __log and Game.GetLocalPlayer()
UI.RequestAction(ActionTypes.ACTION_ENDTURN)
```

Expect the game to halt with `Game.GetLocalPlayer() == 1` and the turn counter
unchanged. Note that once armed, the handler persists for the rest of the
session and the game will pause on the agent civ's turn every turn; load any
save to clear it.
