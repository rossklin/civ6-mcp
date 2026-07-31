# Human vs Agent: Turn-Boundary Local-Player Handoff

> **Status: Implemented and validated live.** Mechanism proven 2026-07-28,
> implemented 2026-07-29 with support for several agent opponents at once, and
> confirmed the same day in a 4-player game (human Portugal P0, agents Sumeria
> P1 and Egypt P2, Cree P3 built-in AI) through turn 5 — see
> [Live multi-agent validation](#live-multi-agent-validation). Blockers,
> diplomacy, World Congress and save/load under handoff remain untested; see
> [Remaining unknowns](#remaining-unknowns).
>
> Goal: a human plays Civ 6 in the normal UI while one or more MCP-driven
> agents play rival civilizations in the same single-player game, taking turns
> in order.

## Summary

This works. The complete loop was demonstrated end-to-end against a live game
(Vietnam P0 = human, Mongolia P1 = agent, Kongo P2 = built-in AI, turns 5–9).

**No mod is required.** The Civ 6 event tables are reachable from the FireTuner
Lua states with a working `.Add`, so the interception hook can be registered
over the existing TCP connection at startup.

The entire mechanism is one persistent handler:

```lua
GameEvents.PlayerTurnStarted.Add(function(pid)
  if MANAGED[pid] then
    PlayerManager.SetLocalPlayerAndObserver(pid)
  end
end)
```

Because an agent's civ becomes a genuine local human player, **every existing
MCP tool works unmodified** — the ~100 `Game.GetLocalPlayer()` call sites in
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

It is also why the read layer never switches the local player to answer a
query. An agent reading during someone else's active turn would hand that
player's turn to the AI. See [Read perspective](#read-perspective) for what the
implementation does instead.

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

Sequential processing is what makes multiple agents work without any extra
coordination: the engine already visits players in id order, so the hook simply
stops on each managed player in turn.

With the human at P0 and agents at P1 and P2:

| # | Event | Action | Result |
|---|-------|--------|--------|
| 1 | Human plays turn N in the UI, presses End Turn | — | P0 turn-complete |
| 2 | `PlayerTurnStarted(1)` fires | Hook switches local player to P1 | Engine **halts** waiting for input as P1; full movement available |
| 3 | Agent A plays via MCP tools | Normal tool calls | Unlimited thinking time — the engine is idle |
| 4 | Agent A calls `end_turn` | `ACTION_ENDTURN` | P1 turn-complete |
| 5 | `PlayerTurnStarted(2)` fires | Hook switches local player to P2 | Agent B is now on the clock |
| 6 | Agent B plays, calls `end_turn` | — | P2 turn-complete |
| 7 | Remaining AI civs process | — | Normal AI turns |
| 8 | `PlayerTurnStarted(0)` fires, turn N+1 | Hook switches back to the human | Human resumes, units untouched |

Note that the game turn counter only increments at step 8. An agent ending its
turn at step 4 does **not** see the turn advance — the signal that its turn is
over is losing the local-player slot. This drives two design points below.

## Architecture

```
   human ──────────────► Civ 6 UI  ◄──── plays P0 in the normal game window
                            │
   agent A ─┐               │ TCP :4318 (FireTuner)
   agent B ─┼── HTTP ──► MCP server ──────┘
   agent C ─┘   :8765     (one process, one connection, N seats)
```

**One server process, one connection — enforced by the game.** FireTuner's
listener accepts a *single* client. A second process trying to connect to
127.0.0.1:4318 while another holds it gets `ConnectionRefusedError`
(`WinError 1225`) — measured on 2026-07-29, with one `civ-mcp` connected and a
second one starting up. So a shared game cannot be several server processes; it
has to be one server serving several MCP clients, which means HTTP rather than
stdio (stdio serves exactly one client). Setting `CIV_MCP_AGENT_PLAYERS`
switches the default transport to `streamable-http` for this reason.

The practical consequence: **close any other MCP client attached to the game
before starting the handoff server.** An editor session with `civ6` configured
from `.mcp.json` counts — it holds the tuner port and the handoff server will
never reach the game.

**The lifespan is per session, not per process.** The MCP SDK enters the server
lifespan inside `Server.run()`, which `streamable_http_manager` calls once per
client session — so a naive lifespan would give every agent its own
`GameConnection`, its own `SeatRegistry`, and a second web dashboard on an
already-bound port. `lifespan` therefore refcounts a process-wide context built
by `_open_app_context`: the first session creates it, the rest share it, and the
last one out tears it down. In stdio mode there is exactly one session, so this
changes nothing.

When the last agent disconnects, teardown disarms the hook and returns the human
slot to the human (`handoff.hand_back`). Otherwise the game would halt forever
on an agent civ's turn with nobody to play it. If an agent civ held the slot at
that moment, the built-in AI finishes its turn — the one case where handing an
active turn to the AI is the right outcome.

### Seats

A *seat* is a player id an agent is allowed to drive. `SeatRegistry`
(`src/civ_mcp/seats.py`) holds one seat per configured agent player. Each MCP
session claims a seat with `claim_seat(player_id=N)`; the binding key is the
identity of the session object, which is stable per client connection under
both stdio and HTTP. Every other tool is refused until a seat is claimed, so an
agent cannot start playing an unassigned civ by accident.

Each seat owns its own `GameState`, `GameLogger`, `SpatialTracker`,
`MapCapture` and telemetry emitter over the one shared `GameConnection`. This
matters because `GameState` carries player-scoped state — last snapshot, pending
end-turn flag, diary bookkeeping, save-load history. Sharing one instance would
diff one agent's turn against another's baseline. Per-seat emitters also give
each agent its own diary and log files (`..._p1.jsonl`, `..._p2.jsonl`) instead
of interleaved rows in one.

A seat held by a session that has gone away can be reclaimed — an agent whose
client reconnects must be able to get its civ back.

### Read perspective

Under handoff, `Game.GetLocalPlayer()` resolves to whoever currently holds the
human slot. An agent reading during another player's turn would therefore see
*that* player's empire — `get_units` would list someone else's units.

Switching the local player to answer the query is not an option: it would hand
the active player's turn to the AI (see [The Core Rule](#the-core-rule)).

Instead, the caller's seat is published in a `ContextVar` and
`GameConnection` rewrites `Game.GetLocalPlayer()` to that literal player id
before sending the Lua. Every builder spells the expression exactly one way, so
one textual substitution at the connection layer covers all ~100 call sites —
this is why the `player_id` parametrization proposed in
[agent-vs-agent.md](agent-vs-agent.md) is not needed. The rewrite is a no-op
while the seat holds the slot, since the substituted id *is* the local player.

Turn-ownership probes and the handoff installer pass `perspective=False` so they
see the real local player.

The rewrite applies to `run_lua` too, which is a trap worth knowing: an agent
running `print(Game.GetLocalPlayer())` gets *its own seat id* back, not whoever
actually holds the slot. Consistent with the rule ("reads answer as your civ")
and correct for gameplay queries, but useless for introspecting the handoff.
`get_turn_status` and `reinstall_handoff` report the truth.

Caveat: a few InGame APIs are local-player-only (`UI.*`,
`NotificationManager`), so tools depending on them are degraded while off the
clock. The empire-level reads an agent actually needs for planning —
`get_units`, `get_cities`, `get_map_area`, `get_diplomacy`, `get_tech_civics` —
go through `Players[id]:…` and work for any player.

### Turn-ownership gating

Every tool call passes through `_check_turn_gate` in `server.py`:

| Tool class | Off the clock | On the clock |
|---|---|---|
| Read (`readOnlyHint`) | allowed, answers for your civ | allowed |
| Write (everything else) | refused with the current turn owner | allowed |
| Seat / turn tools | always allowed | always allowed |
| `load_*`, `restart_and_load`, `kill_game`, `launch_game` | always refused | always refused |
| `run_lua` | allowed for `gamecore`, refused for `ingame` | allowed |

The write set is derived from the absence of `readOnlyHint`, so a newly added
write tool is gated by default rather than by remembering to list it.

Game-reloading tools are refused outright: in a shared game they would throw
away the human's session and every other agent's progress. Automatic hang
recovery (which restarts the game) is disabled in handoff mode for the same
reason — a hang is reported to the agent to relay to the human instead.

Reads stay open deliberately. The human and the agents can see each other's
state in this design; enforcing fog of war would mean visibility checks
throughout the query layer, and the engine will not do it. The tradeoff buys the
thing that makes multi-agent play tolerable: an agent can study the map and plan
during the whole rest of the round instead of sitting idle.

### Ending a turn, and waiting for the next one

The turn counter does not move when a seat ends its turn, so
`execute_end_turn`'s advancement check is generalised: for a seated agent,
"advanced" means the game turn incremented **or** the seat lost the
local-player slot (`end_turn.py:_poll_advanced`).

The post-turn report (snapshot diff, threats, notifications, empire warnings)
cannot be built at that moment either — the round is half-finished, and
hammering the InGame context while other civs process is a known cause of AI
stalls. So `end_turn` stashes the baseline on the seat and returns immediately;
`build_post_turn_report` (split out of `execute_end_turn`) runs later, when the
seat gets the slot back and the game is idle.

That gives agents a two-call turn loop:

```
end_turn(...)          -> "your turn is over, play has passed on"
  ... read tools, plan, study the map, off the clock ...
wait_for_turn()        -> blocks, then returns the full turn report
```

`wait_for_turn` returns after `timeout_seconds` (default 90, capped at 600)
with the current status rather than blocking past typical MCP client request
timeouts; the agent just calls it again. `get_turn_status()` is the
non-blocking check.

Two per-turn housekeeping items are keyed off the game turn rather than off
`end_turn`, since several seats end their turn inside one game turn: the
per-turn advisor call budget resets when a seat picks up a new turn, and the
`0_MCP_NNNN` autosave is written once per turn (guarded on the connection,
which is shared, not on `GameState`, which is not).

### Keeping the hook armed

The handler lives in the tuner Lua state and is destroyed whenever a save loads
or the game restarts — which would silently drop every agent out of the game.
Rather than chase every load path, `HandoffKeeper` polls ownership every 10s and
re-arms the hook whenever it goes missing. `reinstall_handoff()` forces it
immediately and dumps the diagnostic ring buffer.

The install script is idempotent: re-running updates the managed player set
without stacking duplicate listeners, and it never calls `RemoveAll()` (that
would strip the game's own `PlayerTurnStarted` listeners).

## Setup

### 1. Start a single-player game

Set it up in the normal way, with as many major civs as you want agents plus
one for yourself. Note which player id each civ has — P0 is you, and the AI
civs are numbered in the order the game lists them. `get_seats()` reports the
civ and leader for each id once the server is up, so it is fine to guess and
check.

Play at least the first turn yourself so the game is fully loaded.

### 2. Start the server

```powershell
# PowerShell — human is P0, agents play P1 and P2
$env:CIV_MCP_AGENT_PLAYERS="1,2"; uv run civ-mcp
```

```bash
# bash / zsh
CIV_MCP_AGENT_PLAYERS=1,2 uv run civ-mcp
```

The server serves `streamable-http` on `http://127.0.0.1:8765/mcp`. It does not
touch the game yet: because the MCP lifespan is per session, the connection is
opened and the hook armed when the **first agent connects**, and torn down when
the last one leaves. So `Handoff armed (hook=True, …)` appears in the log at
first connection, not at startup. The spectator camera stays off for the whole
run (the human owns the camera).

The web dashboard also starts, on port 8000 by default. If that port is busy the
dashboard is skipped with a warning — it is not required to play.

| Variable | Default | Meaning |
|---|---|---|
| `CIV_MCP_AGENT_PLAYERS` | *(unset)* | Comma-separated agent player ids. Setting it enables handoff mode. |
| `CIV_MCP_HUMAN_PLAYER` | `0` | Player id of the human. |
| `CIV_MCP_TRANSPORT` | `streamable-http` in handoff mode, else `stdio` | MCP transport. |
| `CIV_MCP_HTTP_HOST` | `127.0.0.1` | Bind host for HTTP transport. |
| `CIV_MCP_HTTP_PORT` | `8765` | Bind port for HTTP transport. |
| `CIV_MCP_WEB_PORT` | `8000` | Web dashboard port. |

### 3. Point one agent at each seat

Give each agent its own working directory with an `.mcp.json` that **points at
the running server**. Copy [`docs/mcp-http.json`](mcp-http.json):

```json
{
  "mcpServers": {
    "civ6": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

> **Do not copy the repo's own `.mcp.json`.** That one is the stdio *launcher*
> (`"command": "uv", "args": [... "civ-mcp"]`); it tells the client to spawn its
> own private server rather than connect to yours. Two agents doing that get two
> classic-mode servers with no seat tools (`get_seats` "does not exist") racing
> for the tuner port, so one works and the other reports "Cannot connect to Civ 6
> at 127.0.0.1:4318" — while the real handoff server sits idle with nobody
> connected. Observed on 2026-07-31; the `type`/`url` form above is the fix.

Both agents should stay connected for the whole game. The context is torn down
when the last session disconnects, which disarms the hook and hands the slot
back — deliberate, so the human is never stuck waiting on an agent that has
gone away, but it means the game reverts to ordinary single-player if every
agent quits.

Then start each agent with its seat:

```
You are playing Civilization VI against a human and another agent.
Call get_seats(), then claim_seat(player_id=1), then play your turns.
After end_turn(), call wait_for_turn() to pick up your next turn.
```

The server's MCP instructions already say this, so an agent that reads them
will do the right thing unprompted.

### 4. Play

Press End Turn in the game as usual. The game will pause on each agent's turn
while it thinks, then come back to you.

> **Only press End Turn while your own civ is on the clock.** The swap makes the
> agent's civ genuinely the local player, so the End Turn button ends *its* turn,
> not yours. Doing it by accident costs that agent the rest of its turn (observed
> on 2026-07-29); nothing corrupts, and the agent's own queued writes are then
> refused rather than leaking into the next player's turn, but the civ forfeits
> its orders. Nothing in Lua can intercept the human's key press, so this is a
> discipline rather than a guard.

While an agent is on the clock the screen renders that civ's territory and fog.
This is inherent to swapping the local player and cannot be fixed — only
tabbed away from. Two related rough edges:

- **Founding a city as an agent raises a modal on the human's screen** with no
  close button. It clears when play moves on (the popup watcher, or the turn
  ending), but it is intrusive while it is up. `execute_end_turn` pre-dismisses
  `ExclusivePopupManager` popups; this panel is evidently not one of them.
- Whichever civ holds the slot is the one the UI describes, so the on-screen
  leader is not a reliable indicator of whose turn it is mid-round. Use
  `get_turn_status`, which reads the real local player.

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

## Live multi-agent validation

Run on 2026-07-29: 4-player game, human Portugal (P0), agents Sumeria (P1) and
Egypt (P2) each on their own MCP session, Cree (P3) built-in AI. Played from
turn 1 to turn 5.

The handoff ring buffer (`turn|activated|local before|after|result`) over turns
2–5, read back with `reinstall_handoff`:

```
2|0|2|0|ok    3|0|2|0|ok    4|0|2|0|ok    5|0|2|0|ok
2|1|0|1|ok    3|1|0|1|ok    4|1|0|1|ok
2|2|1|2|ok    3|2|1|2|ok    4|2|1|2|ok
```

Ten consecutive switches, every one `ok`, always the cycle `0 → 1 → 2 → 0` with
the turn counter incrementing only on the wrap. No missed switches, no
duplicated listeners, no drift.

Confirmed in the same run:

- **The hook installs over the tuner** for a three-player managed set, with no
  mod. This was the one assumption that could have invalidated everything.
- **Per-seat reads while off the clock.** With P0 holding the slot,
  `get_game_overview` on the P1 session reported Sumeria and on the P2 session
  reported Egypt, each with its own unit list and exploration percentage
  (18 vs 31 tiles). The `Game.GetLocalPlayer()` rewrite does what it claims.
- **Writes work for a swapped-in seat.** `found_city` (Uruk at 51,19; Thebes at
  40,13), `fortify`, `set_city_production`, `set_research` all succeeded through
  `RequestOperation` while the agent held the slot.
- **The gate holds.** `unit_action` off the clock was refused with the current
  turn owner. When a seat's queued commands arrived late (after the human ended
  that agent's turn by mistake), they were refused rather than leaking orders
  into the next player's turn.
- **Deferred turn reports span the right turns.** P2 produced `1→2`, `2→3`,
  `3→4` on consecutive reacquisitions; P1 produced `1→2` then `3→4`, the gap
  being exactly the turn the human ended on its behalf.
- **The built-in AI is unaffected.** Cree founded its city on turn 1 and kept
  playing normally; the hook ignores unmanaged players.
- **Two sessions share one context.** Refcounted lifespan logged
  `MCP session opened (1 active)` → `(2 active)` and one shared connection.
- **Destructive tools refused.** `load_game_save` was refused for a seated
  agent even on its own turn.

Bugs this run found and fixed: the web dashboard's `sys.exit(1)` on a port clash
escaping as `SystemExit` and killing the whole ASGI app, and the per-session
lifespan described above.

## Rejected Approaches

**Multiplayer / hotseat — impossible.** FireTuner's listener is disabled for all
multiplayer modes. Reported behavior: the tuner connects at the main menu and
disconnects the moment a hotseat game loads. This is deliberate anti-cheat with
no known config override.

**One MCP server process per agent — impossible.** The tuner listener is
single-client: the second process gets `ConnectionRefusedError` and never
reaches the game at all. (Even if it could connect, the processes would collide
on the web dashboard port and the autosave name.)

**Switching the local player to answer a read — actively harmful.** It hands
the currently active player's turn to the built-in AI. Textual rewriting of
`Game.GetLocalPlayer()` gives per-seat reads with no engine-visible effect.

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
   **unnecessary**. `Game.GetLocalPlayer()` already resolves to the correct civ
   on the seat's own turn, and off-turn reads are handled by one textual rewrite
   at the connection layer.

Its GameCore/InGame API tables and the `PlayerTurnStartComplete` analysis remain
accurate and useful.

## Remaining unknowns

The mechanism itself is settled — see
[Live multi-agent validation](#live-multi-agent-validation). What is left needs a
longer game, since these only arise after the opening turns. None of them block
play.

1. **End-turn blockers under handoff.** Not one fired in turns 1–5, so the
   blocker-resolution machinery is untested in this mode. The first will be a
   completed civic (Code of Laws) or tech needing a next choice; era changes,
   governor points and policy slots follow. This is the highest-value next test,
   because `end_turn`'s blocker path is the most intricate code the handoff
   touches.
2. **Does the AI still choose an agent civ's research and production?**
   Suggestive but not conclusive so far: P1's research stayed on the Animal
   Husbandry the agent picked across four turns. Test properly by reading an
   agent's research and build queue at the moment of handoff, *before* the agent
   acts. If the engine pre-commits them, agents must overwrite every turn.
3. **Diplomacy.** A civ that becomes human mid-game may be approached
   differently by other AIs. AI-initiated diplomacy sessions targeting an agent
   route through `get_pending_diplomacy` / `respond_to_diplomacy` during that
   agent's window, but AI-to-AI diplomacy between two agent civs has no API.
4. **Fairness.** Agents have gamecore read access to the entire map regardless
   of their own visibility, and so can see the human's state. Accepted for now;
   enforcing fog of war requires visibility checks in the query layer.
5. **Save/load.** Confirm a game saved mid-handoff (an agent holding the human
   slot) reloads sanely, and that the local player on load is the human. The
   keeper re-arms the hook after a load either way.
6. **Longevity.** Four full rounds with two agents showed no drift or leaked
   handlers. Still worth 20+ turns, where era changes and unit counts grow.
7. **The city-founding modal.** Founding a city as an agent raises a modal on
   the human's screen with no close button; it clears only when play moves
   on. Cosmetic but intrusive. A targeted fix would explicitly close the
   production panel in the seated `end_turn` path.
8. **World Congress with several human-slot civs.** WC fires synchronously
   inside `ACTION_ENDTURN`; with multiple seats voting in the same session the
   ordering has not been exercised.

## Reproducing the Experiment by Hand

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

## Code map

| File | Role |
|---|---|
| `src/civ_mcp/handoff.py` | Config, the GameCore install/status/roster/hand-back Lua, `HandoffKeeper`, `wait_for_turn` |
| `src/civ_mcp/seats.py` | `SeatRegistry`, `Seat`, the read-perspective `ContextVar` |
| `src/civ_mcp/connection.py` | `apply_perspective` — the `Game.GetLocalPlayer()` rewrite |
| `src/civ_mcp/end_turn.py` | `_poll_advanced` (seat-aware advancement), `build_post_turn_report` (deferred report) |
| `src/civ_mcp/server.py` | Shared lifespan, seat resolution, `_check_turn_gate`, seat/turn tools, transport selection |
| `tests/test_handoff.py`, `tests/test_seats.py` | Config, generated Lua, parsing, registry, gating, `wait_for_turn` |
