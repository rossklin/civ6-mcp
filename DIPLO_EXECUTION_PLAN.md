# Plan: Fix diplo action execution (friendship / delegation / embassy)

Handoff document for the implementing agent. Written 2026-08-22 after an
extensive live probing session (game T52, P0 = Cree = human, P1 = Maya =
agent). **Everything in "Engine findings" below was verified live against the
running game** unless marked *inferred*. You should not need to re-derive any
of it; where a fact matters, it says so.

---

## 1. Problem

The diplo mailbox (`src/civ_mcp/diplo_mailbox.py`) files
response-able diplomatic proposals (DECLARE_FRIENDSHIP, DIPLOMATIC_DELEGATION,
RESIDENT_EMBASSY) between the human and managed agents so the *target* decides
instead of the built-in AI. Proposing and mailbox filing **work** (live-verified,
human→agent direction). What is broken is **executing an accepted proposal**:

- `src/civ_mcp/lua/build_send_diplo_action.lua` (lines 56–66) opens a session
  as the proposer and calls `DiplomacyManager.AddResponse(sid, me, "POSITIVE")`
  twice, then `CloseSession`. **The engine silently ignores those AddResponse
  calls.** The `OK:ACCEPTED|...` print at lines 93–106 is unconditional, which
  masked the failure (a false positive — friendship/delegation never actually
  registered; confirmed live: validity never flipped, relationship panel
  unchanged).
- Related Python bug: `_action_result` (`src/civ_mcp/game_state.py:1597`)
  converts Lua `ERR:` lines to `"Error: ..."`. Any check like
  `result.startswith("ERR")` can never match. `_drain_human_diplo_proposals`
  in `src/civ_mcp/server.py` (~line 1339) currently has exactly this bug and
  reports failures as "took effect".

## 2. Engine findings (live-verified)

Contexts: each Civ6 Lua context is an isolated Lua state with its own copy of
engine-exposed tables (`DiplomacyManager` in `InGame` is a different table from
the one in `DiplomacyActionView`). Our server can execute in: `InGame`
(`conn.execute_write`), `GameCore` (`conn.execute_read`, read-only-ish), any
named state (`conn.execute_in_named_state(name, lua)`); `run_lua`'s `context`
param also accepts named states since game_lifecycle.py was updated this
session.

| Fact | Detail |
|---|---|
| `RequestSession(from, to, typeStr)` works with a **non-local initiator** | Opens a session, `GetSessionInfo` shows correct From/To. Works from the InGame context. |
| Such a session is an **orphan** | The DiplomacyActionView does not adopt it (`ms_ActiveSessionID` stays nil), it never enters the player's session queue (`HasQueuedSession` = false), opening diplomacy manually shows the normal root menu, and **no `AddResponse` from any context/player/string advances it** (tried: POSITIVE/HUMAN_ACCEPT_DEAL/AI_ACCEPT_DEAL, as target and as initiator, from InGame and DAV contexts). |
| `SendAction(from, to, DiplomacyActionTypes.X, {})` returns true but applies nothing by itself | `TestAction` returns false even for valid actions — do not use it as an oracle. |
| `SendAction` + an `AddResponse` from the **DiplomacyActionView context** makes the view adopt the session | After this, `ms_ActiveSessionID` is set and the native proposal dialogue presents on screen (verified twice). Effect still not applied at this point. |
| Once adopted, one more `AddResponse(sid, target, "POSITIVE")` from the DAV context **completes the proposal** | Verified via a real human click (friendship: validity flipped to invalid, both diplomatic state indices → 1/FRIENDS, UI confirmed) and via a scripted DAV-context response (delegation: completing response sent; final `HasDelegationAt` check was interrupted — **verify on first live test**). |
| Responses must come from the **target** (the local player), in the **DAV context** | Response as initiator was a no-op. InGame-context responses were no-ops even post-adoption? (not re-tested post-adoption — use the DAV context, which is what the native button uses and is proven). |
| `DiplomacyActionTypes` enum (ingame context) | `DECLARE_FRIEND`, `SET_DELEGATION`, `SET_EMBASSY`, `DENOUNCE`, `MAKE_DEAL`, `SET_OPEN_BORDERS`, `SET_WAR_STATE`, `ALLY`. |
| Validity oracle | `Players[X]:GetDiplomacy():IsDiplomaticActionValid("DIPLOACTION_<ACTION>", Y, true)` — reading **another player's** diplomacy object works. All three actions carry a No-current-X gate, so **valid-before + invalid-after execution = effect registered**. For delegation direction: `Players[from]:GetDiplomacy():HasDelegationAt(to)`. |
| Same-frame staleness | Post-action state reads are stale until the next engine frame — verify in a separate round-trip after ~0.3–0.5 s. |
| `ms_*` view globals readable from the DAV context | `ms_ActiveSessionID`, `ms_Mode`, `ms_OtherPlayerID`, `ms_LocalPlayerID`, `ms_InitiatedByPlayerID` — file-scope globals in DiplomacyActionView.lua. Use `ms_ActiveSessionID` to poll adoption. |
| Force-teardown hazard | Closing a session + firing hide events in the same instant left the view with a stale `ms_ActiveSessionID`, which then *swallowed the next session's statement event* (presentation never happened). Teardown must be: bare `CloseSession`, wait ≥0.3 s in a separate round-trip, then dismiss UI. Also see the warning in `game_lifecycle.py` (~line 64): force-closing sessions historically caused turn-processing hangs — keep closes minimal. |

Game-source anchors (for reference only; no re-reading needed):
`Base\Assets\UI\DiplomacyActionView.lua` — `OnSelectInitialDiplomacyStatement`
(~407, button clicks → `RequestSession`), `OnSelectConversationDiplomacyStatement`
(~494, choices → `AddResponse(sid, Game.GetLocalPlayer(), "POSITIVE"/…)`),
`OnDiplomacyStatement` (~2745, adopts sessions / opens the view).

## 3. The verified completion recipe (target-local)

Runs while the **target is the local player** (i.e. during the target's turn —
for the agent targets that is exactly when `respond_to_diplo_action` runs).
`from` = proposer, `to` = target = local.

1. **Pre-validate** (InGame ctx): `Players[from]:GetDiplomacy():IsDiplomaticActionValid("DIPLOACTION_"..action, to, true)` — bail with reason if false. (Reuse `_diplo_action_validity_lua()` from `src/civ_mcp/lua/diplomacy.py`, but parameterize the acting player instead of `Game.GetLocalPlayer()`.)
2. **Clean stale session** (InGame ctx): bare `CloseSession` only if `FindOpenSessionID(to, from)` ≥ 0.
3. **Open** (InGame ctx): `DiplomacyManager.RequestSession(from, to, "<session string>")` — session string from `DIPLO_SESSION_STRING_MAP` (`DECLARE_FRIENDSHIP`→`"DECLARE_FRIEND"`, others identity). Get `sid` via `FindOpenSessionID(to, from)`.
4. **Prime** (InGame ctx): `DiplomacyManager.SendAction(from, to, DiplomacyActionTypes.<TYPE>, {})` where TYPE = `DECLARE_FRIEND`/`SET_DELEGATION`/`SET_EMBASSY`. Map lives in `DIPLO_SESSION_STRING_MAP` values (they coincide: `"DECLARE_FRIEND"` etc. — but `DiplomacyActionTypes` keys are `DECLARE_FRIEND`, `SET_DELEGATION`, `SET_EMBASSY`, so add a small `DIPLO_ACTION_TO_ENUM` map: DECLARE_FRIENDSHIP→DECLARE_FRIEND, DIPLOMATIC_DELEGATION→SET_DELEGATION, RESIDENT_EMBASSY→SET_EMBASSY).
5. **Adoption nudge** (DAV ctx, via `execute_in_named_state("DiplomacyActionView", …)`): `DiplomacyManager.AddResponse(sid, to, "POSITIVE")` — this does NOT complete the orphan; it triggers the statement delivery that the view adopts.
6. **Poll adoption** (DAV ctx): read `ms_ActiveSessionID` every ~0.3 s until it equals `sid` (timeout ~3 s). If it also equals… note `ms_Mode` may stay nil — that is fine; the dialogue presents anyway (observed).
7. **Complete** (DAV ctx): `DiplomacyManager.AddResponse(sid, to, "POSITIVE")` again.
8. **Verify** (separate round-trip, InGame ctx, after ~0.5 s): validity flipped to false (and for delegation `Players[from]:GetDiplomacy():HasDelegationAt(to)` is true). **This — not any OK print — is the source of truth.**
9. **Teardown** (separate round-trips): bare `CloseSession(sid)`; sleep ~0.5 s; then `handoff.build_dismiss_leader_screen_lua()` (already exists in `src/civ_mcp/handoff.py`) so the human's screen (which shows the leader dialogue during agent turns) returns to the game.

The engine applies gold costs (delegation 25, embassy 50) to the **proposer**
at effect time — no manual handling.

## 4. New architecture

**Execute at accept time on the target's turn** (replaces all proposer-side
drain execution):

- **human→agent**: human's click is intercepted by the diplo shim
  (working, live-verified) → mailbox. On the agent's turn the agent calls
  `respond_to_diplo_action(accept=True)` → **run the recipe immediately**
  (agent is local) → verify → report result to the agent in the tool output.
  On reject: no engine call (as today).
- **agent→agent**: identical — the accepting agent executes at accept time.
- **agent→human**: at notification click (human's turn, human local), run
  recipe steps 1–5 with `from`=agent, `to`=human, and arm
  `__MCP_diplo_proposal_id` (existing `build_set_diplo_proposal_lua`) so the
  shim's AddResponse wrapper reports `MCPDIPLO_RESPONDED` — **the human's own
  Accept/Decline click is the completing response** (step 7) and the engine
  applies the effect natively (this exact completion was live-verified with
  the friendship click). Python then marks executed/rejected as today
  (`_handle_diplo_response`).
  ⚠ Open risk: step 5's nudge is an `AddResponse(sid, human, "POSITIVE")`
  sent *before* the human has decided. In the delegation probe this nudge did
  **not** complete the orphan session (effect stayed unregistered until the
  post-adoption response), so it is believed safe — but on the first live
  test, verify after step 6 that validity has NOT flipped (i.e. the nudge
  didn't auto-accept). If it ever auto-accepts, fall back to: notify via
  chat, human replies accept/decline in chat (existing chat mailbox), and
  complete via the scripted target-local recipe during the human's turn.

Status model (`diplo_mailbox.py`): keep `pending/accepted/rejected/executed`.
`accepted` now only ever exists transiently (or when execution failed — see
below). The proposer-side drains become **report-only**:
- `_drain_diplo_proposals` (agent proposers): drop the engine-execution
  branch; report `executed` as "took effect", `rejected` as rejected, remove.
- `_drain_human_diplo_proposals` (human proposers): drop the execution
  branch; send the human a chat message (`_push_chat`, exists) on
  `executed` ("Your friendship proposal took effect.") / `rejected`
  ("P1 declined your …"). No engine calls at the human's slot start anymore.

Failure handling at accept time: if the recipe fails validation or the
verification flip doesn't happen, report the honest error string to the
accepting agent and **remove** the proposal (no retry loops). The proposer
side just never hears "took effect".

## 5. Implementation steps (file by file)

1. **`src/civ_mcp/lua/diplomacy.py`**
   - Add `DIPLO_ACTION_TO_ENUM: dict[str, str]` (action_name →
     `DiplomacyActionTypes` key).
   - Parameterize `_diplo_action_validity_lua()` with the acting player id
     (default `Game.GetLocalPlayer()` so `build_send_diplo_action.lua` is
     unchanged for one-way actions).
   - Add small builders for the recipe's individual Lua steps (or one builder
     per step — the orchestration lives in Python because of the
     round-trip/delay requirements):
     `build_diplo_open_step(from, to, session_str)` (stale close + RequestSession + sid print),
     `build_diplo_prime_step(from, to, enum_key)` (SendAction),
     `build_diplo_response_step(sid, to)` (AddResponse POSITIVE — DAV ctx),
     `build_diplo_adoption_check(sid)` (prints `ADOPTED|true/false` from `ms_ActiveSessionID`),
     `build_diplo_effect_check(from, to, action_name)` (validity + HasDelegationAt + state index prints).
     All `pcall`-guarded, sentinel-terminated, `MCP_TRACE|`-prefixed
     diagnostics welcome (parser routes those to the server log).
   - `build_send_diplo_action`: keep for **one-way actions only** (DENOUNCE,
     wars). Route the three response-able actions away from it (see server.py
     below) or make it refuse them outright with a clear ERR.

2. **`src/civ_mcp/handoff.py`**
   - `build_dismiss_leader_screen_lua()` — **already added this session**
     (untested live; it copies the proven `propose_trade` dismiss idiom).
   - `build_open_diplo_session_lua` / `build_set_diplo_proposal_lua` /
     `build_clear_diplo_flag_lua` — already exist, keep.
   - If recipe-step builders end up here instead of diplomacy.py, fine —
     mirror the existing naming conventions.

3. **`src/civ_mcp/diplo_mailbox.py`** — docstring update (accept-time
   execution model); `mark_executed` and drainable statuses already support
   this. No structural change expected.

4. **`src/civ_mcp/server.py`**
   - New `_execute_diplo_agreement(gs, conn, from_player, to_player, action_name) -> tuple[bool, str]`:
     Python orchestration of recipe steps 1–9 using `conn.execute_write`
     (InGame steps) and `conn.execute_in_named_state(handoff.DIPLO_SHIM_STATE, …)`
     (DAV steps), with the delays/polls from §3. Returns (ok, message) where
     ok is decided **only** by the step-8 verification flip.
   - `respond_to_diplo_action` routing in `execute_commands`: on
     `accept=True`, after `diplo_mb.accept(...)`, call
     `_execute_diplo_agreement(...)` with from=proposal.from_player,
     to=seat.player_id. On success mark `executed` instead of `accepted`;
     on failure remove + return the error text. (Turn-gating already ensures
     the seat is on its turn = target-local.)
   - `_handle_diplo_notification_click`: insert recipe steps 1–6 (open,
     prime, nudge, poll adoption, assert not-yet-applied) around the existing
     flag-arming + session-open logic; keep `build_set_diplo_proposal_lua`
     arming BEFORE the nudge so the human's click is reported.
   - `_drain_diplo_proposals` / `_drain_human_diplo_proposals`: reduce to
     report-only per §4. **Fix the `startswith("ERR")` bug** — check
     `"Error"`/`"execution failed"` prefixes (or better, the verification
     result) wherever results are judged.
   - `get_full_game_state` outgoing status text: already renders `executed`.

5. **Tests** (`tests/test_diplo_mailbox.py`, style: fakes in
   `_FakeGameState`/`_FakeConn`, `_patch_engine` monkeypatching)
   - New: accept-time execution — `respond_to_diplo_action(accept=True)`
     calls `_execute_diplo_agreement` with (from=proposer, to=seat); success
     marks `executed`; failure removes + surfaces error.
   - New: `_execute_diplo_agreement` ordering — InGame open+prime, DAV
     nudge, adoption poll, DAV complete, verify flip decides ok; timeout /
     non-adoption / no-flip paths.
   - Update: `_drain_diplo_proposals` tests (accepted no longer executes at
     drain — or keep accepted-executes only if you keep a fallback; prefer
     report-only), `_drain_human_diplo_proposals` tests (no engine calls,
     chat notifications only).
   - Builders: assert the new Lua builders' text (tags, enum keys, sid
     threading) like existing builder tests.
   - Run: `uv run python -m pytest ./tests -q` (448 passed as of the last
     run; `build_dismiss_leader_screen_lua` landed after that — rerun).

## 6. Live verification checklist (needs the handoff save, human P0 + agent P1)

1. **Delegation flip** (carried over from the interrupted probe): on P1's
   turn, if the probe's session is still around, check
   `Players[0]:GetDiplomacy():HasDelegationAt(1)` — the completing response
   was sent; if false, re-run the full recipe once and watch each step.
2. **human→agent end-to-end**: human clicks Send Delegation on P1 → chat
   confirmation → agent accepts via `respond_to_diplo_action` → recipe runs
   → verify flip + `HasDelegationAt` direction (P0's delegation at P1's
   court) + human's screen returns to normal after teardown.
3. **agent→human end-to-end**: agent proposes friendship → human gets
   notification at slot start → click → leader dialogue presents (recipe
   steps 1–6 ran) → **check validity has NOT flipped before the click**
   (§4 open risk) → human clicks Accept → friendship in UI +
   `MCPDIPLO_RESPONDED` in log → agent's drain reports "took effect".
4. **Regression**: one-way actions (denounce) unaffected; the human proposing
   to a non-managed AI civ behaves vanilla (shim passes through).
5. Watch for turn-processing hangs after teardown (historical hazard, §2).

## 7. Current state of the code

- `src/civ_mcp/lua/diplo_shim.lua` — installed & live-verified
  (hook=engine; MCPDIPLO naming). The RequestSession interception works; the
  AddResponse reporting path is armed but not yet live-exercised end-to-end.
- `src/civ_mcp/game_lifecycle.py` — `execute_lua` accepts named states
  (done, live-verified via `run_lua context="DiplomacyActionView"`).
- `src/civ_mcp/handoff.py` — diplo shim plumbing + notification builders +
  `build_dismiss_leader_screen_lua` (new, untested).
- `src/civ_mcp/server.py` — human-side handlers wired; drains still have the
  old proposer-execution logic + the `startswith("ERR")` bug.
- A temporary click-trace wrapper (`__TRACE_orig_AR`/`__TRACE_orig_OCDR`) and
  the shim are installed in the live game's DAV state; both are idempotent
  pass-throughs — harmless, and wiped by the next save load.
