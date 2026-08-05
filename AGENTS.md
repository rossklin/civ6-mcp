# Civ 6 MCP — Agent Reference

An MCP server connecting to a live Civilization VI game via FireTuner. You can read full game state and issue commands. All commands respect game rules.

## Two-Tool Architecture

This server exposes **two primary tools** plus admin utilities:

- **`get_full_game_state`** — returns ALL game state in a single call: overview, units, cities, diplomacy, research, trade routes, resources, victory progress, religion, governors, policies, city-states, builder tasks, great people, world congress, notifications, strategic map, and **diary** (long-term plans + next-turn plan). Call this at the start of each turn.
- **`execute_commands`** — runs a batch of game commands from a JSON array. Callable multiple times per turn (scout first, review intel, then commit remaining moves). Unit movements return visibility intel inline.

There are also a couple getters that were kept separate because they are only needed rarely.

## Coordinate System

**Hex grid: (X, Y) where higher Y = visually north.**
- Y increases → north (down). Y decreases → north (up).
- X increases → east. X decreases → west.
- Moving from (9,24) to (9,26) is **north**

**Hex adjacency: simple rules by row parity.**

The game uses offset hex coordinates. Moving ±1 in ONLY X or ONLY Y is **always** adjacent. For diagonal moves (±1 in both axes), use the parity rules below. The Map section header shows the parity hint for quick reference.

**Left shifted rows: NW neighbor at (x-1, y+1)**
| Direction | Offset |
|-----------|--------|
| NE | (x, y+1) |
| E | (x+1, y) |
| SE | (x, y-1) |
| SW | (x-1, y-1) |
| W | (x-1, y) |
| NW | (x-1, y+1) |

**Right shifted rows: NW neighbor at (x, y+1)**
| Direction | Offset |
|-----------|--------|
| NE | (x+1, y+1) |
| E | (x+1, y) |
| SE | (x+1, y-1) |
| SW | (x, y-1) |
| W | (x-1, y) |
| NW | (x, y+1) |

When calculating movement: check the `[mv:N]` notation in the map output. A tile costing `[mv:3]` (hills + forest/jungle) cannot be entered or attacked by a unit with only 2 moves remaining, except if it is an adjacent tile and 2 moves is the full movement points for that unit. Check map movement costs BEFORE issuing move or attack commands.

## Game Start

Before your first turn:
1. Read your civ's unique abilities, units, and buildings — what is this civ designed to do?
2. Identify the tech/civic that unlocks your unique unit; plan a research path to reach it.
3. Form a working hypothesis for a victory path. Hold it loosely — geography and rivals will clarify things through the Classical era.

Early choices compound. Each decision shapes what's available 20, 40, 60 turns later. A scout reveals the map early; a defensive unit lets your settlers move safely; more cities mean more districts which mean more everything. Religious civs often benefit from Holy Site infrastructure before the Great Prophet pool fills. What you don't build early, you pay for later.

## Shared Games (human vs agent)

If `get_seats` and `claim_seat` are available, this game is shared: a human is
playing one civ in the game's own UI and you (plus possibly other agents) play
rival civs, taking turns in order.

The first thing to do is to call `get_seats` to see which seats have which civs. 
Then `claim_seat(player_id=N)`, the human should tell you what seat to claim. 
Every other tool is refused until you do this.

Off the clock, read tools still answer for **your** civ, so the gap between your
turns is time to scout, run strategic checkpoints, and plan — not time to sit
idle. Write tools are refused until your turn, and `get_turn_status()` tells you
where you stand without blocking.

Save/load and game-restart tools are disabled: reloading would destroy the
human's session and the other agents' progress. If the game hangs, say so and
let the human recover it.

### Turn Loop (human vs agent)

Each turn in order:
1. `get_full_game_state` — all game state including your diary (long-term plans + the next-turn plan you wrote last turn). If resuming after context compaction, this is all you need to reconstruct your strategic context.
2. Plan your moves based on the full state and your existing plans. Consider whether notifications or enemy movements since last turn require adjusting your next-turn plan.
3. `execute_commands` with your planned actions (move units, set production, set research, etc.).
4. If unit movements reveal new intel (visible in the command results), you may call `execute_commands` again to act on it. Prefer fewer calls where possible.
5. `end_turn` — advance the turn.
6. Think about what to do next turn and whether your long-term plans need updating.
7. `update_diary(next_turn_plan=..., long_term_plans=...)` — record your plans.
8. `wait_for_turn()` — block until your next turn starts. Call again on timeout.

## Diary

The diary is your persistent memory across sessions and turns. When you start a turn, `get_full_game_state` includes your diary, so you pick up exactly where you left off. The diary has two parts:

- **`next_turn_plan`**: Your concrete plan for the NEXT turn. Be specific — unit movements, production choices, research targets. This is overwritten each turn, so only the most recent entry matters. Write this from the perspective of what you intend to do when you get the turn back.
- **`long_term_plans`**: Your long-term strategy — victory path, expansion goals, tech progression timeline, diplomatic posture. Pass the complete current version each time you call `update_diary`; last write wins.

Call `update_diary` after `end_turn()` and before `wait_for_turn()` — once per turn cycle. Think while other players are on the clock: based on what you observed this turn and the post-turn report (which `wait_for_turn` will return), what should you do next? Do your long-term plans still hold? Write the answers into the diary so your next invocation starts with context.

The diary appears in `get_full_game_state` output — no separate tool needed. When you resume a game after context compaction, `get_full_game_state` alone is enough to reconstruct your strategic context.

## Strategic Patterns

### Moving units
Before moving a builder, settler, or trader to a new tile, consider if there are threats. Civilians have zero combat strength — a single barbarian scout captures them. The cost of losing a builder (5-7 turns of production + charges) is almost always worse than taking one extra turn to check or escort.

Hills cost 2 movement, forests/jungles cost 2, and they stack (forest-hills = 3+). A unit with 2 base moves arriving on forest-hills uses all movement and can't act until next turn. Route through flat terrain when possible, or plan to arrive one turn early. Map tiles now show movement cost (`[mv:2]`, `[mv:3]`) and road presence — route units along roads when possible.

`get_pathing_estimate(unit_id, target_x, target_y)` estimates how many turns a unit needs to reach a destination, using the game's actual pathfinding. Use it before committing units to long marches.

### Builder Management
Idle builders are wasted production. The builder tasks section shows all tiles needing improvements across your empire, prioritized (URGENT > HIGH > NORMAL), with the nearest idle builder for each task. These tasks are naive recommendations and you need to make your own judgement. For instance they may recommend you to build a mine when it is better to keep the forest on the hill, or to place an improvement that you do not have the required tech to place.

### Spending Gold & Faith
Gold and faith sitting idle lose value over time. `purchase_item(city_id, item_type, item_name)` buys units/buildings instantly with gold (or faith via `yield_type="YIELD_FAITH"`). `purchase_tile(city_id, x, y)` buys a specific tile. `patronize_great_person` buys a GP outright. If you're saving, name the item and the turn — otherwise, deploy it.

### Expansion
Each city multiplies your districts, yields, and Great Person generation. The gap between a 3-city and 5-city empire by the Medieval era is hard to recover from. If city count is lagging, a settler is typically the highest-impact production choice — more so than most infrastructure in existing cities. Check loyalty before settling: negative-loyalty sites near rivals need a governor assigned immediately via `assign_governor(governor_type, city_id)` or they'll flip.

### Growth
Stagnant cities fall behind exponentially. If any city has food surplus ≤ 0, that's worth fixing this turn (Farm, Granary, domestic Trade Route, or `set_city_focus(city_id, "FOOD")`). Turns-to-growth over 15 is a signal the city needs food or housing.

### Exploration
You can't settle what you can't see, and you can't counter threats you don't know exist. A scout set to `automate` is one of the best investments in the early game. If a scout is lost or stuck, replacing it early keeps the information flow going.

### Diplomacy
NOTE diplomacy / deals temporarily disabled due to a bug in the MCP, skip for now

### Favor resource
Diplomacy generates yield: each alliance +1 favor/turn per alliance level, each suzerainty +1 favor/turn. Government tier also gives favor. This compounds. Friendships don't give favor directly but enable alliances (which do). Delegations (25g) are cheap on first meeting. Friendships open up when a civ is Friendly. Alliances require friendship (30+ turns) and Diplomatic Service civic. Embassies are available once Writing is researched.

If favor is accumulating above 100 with no World Congress imminent, it's worth thinking about whether it could be better deployed in trade or alliance building.

### War Declaration
War declarations take effect for diplomacy immediately but the **combat engine does not sync until the next turn**. After declaring war via `send_diplomatic_action`, units cannot attack the new enemy until the following turn. Plan accordingly: declare war on turn N, position units adjacent to targets, then attack on turn N+1. Do not reload or retry if attacks return `NO_ENEMY` on the declaration turn — this is expected behavior.

### Wartime
Cities with walls can fire at enemies via `city_action(city_id, "attack", target_x, target_y)` (range 2). Cities that fall are expensive to recover — when you capture a city, `city_action` with `keep`, `raze`, or `liberate_founder`/`liberate_previous` resolves the decision. If your military strength is significantly below an enemy's and you're not making progress, `propose_peace(player_id)` — available after a 10-turn cooldown — is usually better than a war of attrition while the rest of the map moves on.

### Military Readiness
Keep an eye on opponents' military strength. A neighbor at 2x+ your strength who isn't a friend or ally is a risk worth taking seriously. Make sure you have a plan to handle if your opponent becomes aggressive. Units become progressively weaker relative to rivals if not upgraded (Slinger→Archer with Archery, Warrior→Swordsman with Iron Working) — use `upgrade_unit`.

### Barbarian Camps
Camps upgrade with the era — an Ancient-era camp spawns Warriors; the same camp in the Medieval era spawns Man-at-Arms. Clearing a camp within a few turns of finding it is almost always easier than fighting the units it produces over many turns.

### Religion
Religious victory is the easiest win condition to miss because it produces no notifications and unfolds slowly. If a rival religion reaches majority in most civs, the window for a response narrows quickly. Religious units bought from a city carry **that city's majority religion** — buy them from cities where your own religion is majority, not a converted city.

To found a religion: build a Holy Site → earn a Great Prophet → `get_religion_beliefs()` to see available beliefs → `found_religion(name, beliefs)`. The Great Prophet pool fills early (roughly half the major civs).

Trade routes spread the origin city's religion to the destination — worth factoring into routing decisions if conversion pressure is a concern.

### Victory Path Viability
General overview of what you need for each victory type:

- **Science**: Campuses → Universities → Spaceport → 4 space projects. Research Alliances and Great Scientists accelerate.
- **Culture**: Tourism (offense) vs rival domestic tourists (defense). Theater Squares, Great Works, Wonders, Open Borders (+25%), Trade Routes (+25%). Late-game: National Parks, Rock Bands, Seaside Resorts.
- **Religious**: Requires a founded religion (Great Prophet pool fills early). Missionaries spread; Apostles fight theological combat (killing = 250 pressure in 10-tile radius). Buy religious units only from cities where your religion is majority.
- **Diplomatic**: 20 DVP. World Congress resolutions, scored competitions, wonders. Favor from government tier, alliances, suzerainties. If a DVP-stripping resolution targets you, vote Option B on yourself (net 0 vs -2).

## Game Rules Reference

The in-game Civilopedia is available at: https://www.civilopedia.net/en-US/gathering-storm/concepts/intro/

Key URL patterns for looking up rules:
- Movement: `concepts/movement_1/` through `movement_5/`
- Combat: `concepts/combat_1/` through `combat_13/`
- Terrain & Features: `features/terrain_grass_hills/`, `features/feature_forest/`
- Technologies: `technologies/` (full enum index)
- Civics: `civics/` (full enum index)
- Units: `units/` (full enum index)
- Improvements: `improvements/` (full enum index)

When unsure about game mechanics (movement costs, combat formulas, tech prerequisites, improvement requirements), use `WebFetch` to look up the relevant page. The concept pages provide overviews; individual item pages have specific stats. You should also use this if you get error messages when trying to execute commands, unless you are sure you understand what went wrong and how to fix it.

## Combat Quick Reference

- Ranged attacks don't take damage; melee attacks do
- Forests/mountains block ranged LOS — targets with blocked LOS are filtered from `get_units` attack lists
- Fortified units: +4 defense, heal each turn
- Combat estimates include promotion CS bonuses, flanking (+2 per adjacent friendly to defender), support (+2 per defender's adjacent friendly), and forest/jungle defense (+3)

## Unit Actions Reference

| Action | Effect | Notes |
|--------|--------|-------|
| `move` | Move to tile | target_x, target_y required |
| `attack` | Attack enemy | Shows damage estimate; melee/ranged auto-detected |
| `fortify` | +4 defense, heals | Military only |
| `heal` | Fortify until full HP | Auto-wakes at full HP |
| `alert` | Sleep, wake on enemy | Sentry use |
| `sleep` | Sleep indefinitely | Manual wake required |
| `skip` | End unit's turn | Always works |
| `automate` | Auto-explore | Scouts only |
| `delete` | Disband unit | Removes maintenance |
| `found_city` | Settle | Settlers only |
| `improve` | Build improvement | Builders and Military Engineers; see improvements below |
| `remove_feature` | Chop/harvest feature | Builders only; removes forest, jungle, or marsh from tile |
| `build_route` | Build road/railroad | Military Engineers only; on current tile; no charges used |
| `trade_route` | Start route | Traders; target_x/y of destination city |
| `teleport` | Move idle trader | Traders only; target_x/y of city |
| `activate` | Use Great Person | Must be on completed matching district |
| `spread_religion` | Spread religion | Missionaries/Apostles |

Common improvements: `IMPROVEMENT_FARM`, `IMPROVEMENT_MINE`, `IMPROVEMENT_QUARRY`, `IMPROVEMENT_PLANTATION`, `IMPROVEMENT_PASTURE`, `IMPROVEMENT_CAMP`, `IMPROVEMENT_FISHING_BOATS`, `IMPROVEMENT_LUMBER_MILL`

Feature removal: Forest, jungle, and marsh tiles block most improvements (e.g. Farm). Use `remove_feature` to chop/harvest the feature first, then `improve` to build. Lumber Mill and Camp work on forest/jungle without removal. Check `valid_improvements` in `get_units` output — if FARM isn't listed on a tile you expect it, the tile likely has a blocking feature.

Builders repair tile improvements. Pillaged **district buildings** (Workshop, Arena, etc.) are repaired via `set_city_production`.

Military Engineers (requires Encampment + Armory): `build_route` builds a railroad on the current tile (no charges consumed; costs 1 Iron + 1 Coal per tile). `improve` with `IMPROVEMENT_FORT` or `IMPROVEMENT_AIRSTRIP` uses charges. Building a railroad consumes all movement — one tile per engineer per turn.

| Other unit tools | |
|--------|--------|
| `skip_remaining_units` | Skip all units with remaining moves (useful after diplomacy) |
| `upgrade_unit(unit_id)` | Upgrade to next type (requires tech + resources + gold) |

## End Turn Blockers

`end_turn` resolves blockers before advancing. If it returns a blocker:
- **Units**: unmoved units need orders (move / skip / fortify)
- **Production**: city queue empty — set new production
- **Research/Civic**: completed — choose next
- **Governor**: point available — `appoint_governor` / `assign_governor(governor_type, city_id)` / `promote_governor(governor_type, promotion_type)`
- **Promotion**: unit has XP — `promote_unit`
- **Policy Slot**: empty — `set_policies`
- **Pantheon/Religion**: faith threshold reached — `get_pantheon_beliefs` → `choose_pantheon`; for founding: `get_religion_beliefs` → `found_religion`
- **Envoys**: tokens available — `send_envoy`
- **Dedication**: new era — `get_dedications` → `choose_dedication`
- **City Capture**: conquered or disloyal city — `city_action(city_id, "keep"/"raze"/"liberate_founder"/"liberate_previous")`
- Move responses show the **target tile**, not arrival position (async pathfinding)

## Diplomacy

**Reactive (AI-initiated):** AI encounters block turn progression. Use `respond_to_diplomacy` (POSITIVE/NEGATIVE, 2-3 rounds). Diplomacy sessions do not affect unit movement or orders — continue commanding units normally afterward.

**Proactive: this section is temporarily disabled, do not use**
- `send_diplomatic_action(action="DIPLOMATIC_DELEGATION")` — 25g, worth sending on first meeting
- `send_diplomatic_action(action="DECLARE_FRIENDSHIP")` — requires Friendly status
- `send_diplomatic_action(action="RESIDENT_EMBASSY")` — requires Writing tech
- `form_alliance(player_id, type)` — types: MILITARY/RESEARCH/CULTURAL/ECONOMIC/RELIGIOUS; requires friendship 30t + Diplomatic Service civic
- `propose_trade(player_id, ...)` — trade gold/GPT/resources/favor/open borders/cities. Use `mode="test"` first to see the AI's counter-offer without committing, then `mode="send"` to finalize. Cities use `city_id` from `get_trade_options`.
- `propose_peace(player_id)` — white peace; 10t war cooldown required
- `get_trade_options(other_player_id)` — see what a civ has available to trade (gold, resources, favor, cities, agreements)
- `get_pending_trades` — check incoming trade offers; `respond_to_trade(player_id, accept)` to accept/reject
- Check `get_diplomacy` for defensive pacts before declaring war
- `get_diplomacy` shows leader agendas — historical agendas are always visible; random agendas require Secret diplomatic visibility (spy in their capital or alliance). Use agendas to predict AI behavior and avoid relationship penalties.

**Espionage:** `spy_action(spy_id, action, ...)`. Actions: `travel` to a city first, then run operations (steal tech, neutralize governors, etc.). Offensive missions only work after the spy arrives.

**City-states:** `send_envoy`. Suzerainty = +1 favor/turn. Types: Scientific/Industrial/Trade/Cultural/Religious/Militaristic.

## Production & Research

**Wonders:** high-production cities can slot these between infrastructure.

**Research:** try to research techs and civics in such an order that you make optimal use of eurekas and inspirations.

**Tiles:** the game state shows purchasable tiles by city. `purchase_tile(city_id, x, y)` — buy border tiles with gold for strategic resources or district placement.

## District Placement

Use `set_city_production` with target_x/y to place districts. Plan your empire so that districts will get high adjacency bonuses later on. There are later game policies which multiply the adjacency bonuses so having well planned district placement can yield a lot of value.

| District | Adjacency bonuses |
|----------|------------------|
| Campus | +1 per mountain, +1 per 2 jungles, +2 per geothermal/reef |
| Holy Site | +1 per mountain, +1 per 2 forests, +2 per natural wonder |
| Industrial Zone | +1 per mine/quarry, +2 per aqueduct/dam/canal |
| Commercial Hub | +2 per river, +2 per harbor |
| Theater Square | +2 per wonder, +2 per Entertainment Complex/water park |
| Harbor | +1 per sea resource, +2 per city center |

In addition, there is a general +1 per 2 adjacent districts.

## Trade Routes

- `get_trade_destinations(unit_id)` → available destinations
- `unit_action(action='trade_route', target_x, target_y)` → start route
- Domestic routes: food + production to new cities. International: gold.
- Capacity: 1 from Foreign Trade civic, +1 per Market/Lighthouse
- Idle routes are free yields going uncollected

## Great People

- `recruit_great_person(individual_id)` — recruit with accumulated GP points (check `[CAN RECRUIT]`)
- `patronize_great_person(individual_id)` — buy instantly with gold or faith
- `reject_great_person(individual_id)` — pass, advance to next candidate in that class
- Rivals will recruit what you pass on — recruiting quickly tends to be worth it
- Once recruited, move the GP to its matching completed district; `unit_action(action='activate')`
- If activation fails, the error message includes the requirements (district type, buildings needed)
- Don't delete GPs — they show 0 builder charges but that's a different system; they're not consumed until activated

## World Congress

WC fires synchronously inside `end_turn()` — register votes **before** calling end_turn.
There is currently a bug where the mcp always indicates wc voting is active.

**Voting flow:**
1. Review resolutions (options A/B, target list, favor costs)
2. `queue_wc_votes(votes='[{"hash": H, "option": 1, "target": 0, "votes": N}]')`
3. `end_turn()` — handler fires, votes deploy, turn advances

- `hash`: from `get_world_congress`; `option`: 1=A / 2=B; `target`: player_id resolved to list index at runtime; `votes`: max to spend
- 1 free vote per resolution (costs nothing — worth casting)
- Extra votes cost 6/18/36/60/90/126... cumulative favor
- Keeping 50-100 favor in reserve between sessions provides flexibility for the next session
- DVP resolutions: read what each option actually awards before voting. Concentrate favor on the single most impactful resolution rather than spreading thin. Verify your vote blocks the rival, not accidentally helps them

## Victory Conditions

| Victory | Win Condition |
|---------|---------------|
| Science | 4 space projects complete |
| Domination | Own all rival original capitals |
| Culture | Foreign tourists > every civ's domestic |
| Religious | Your religion majority in ALL civs |
| Diplomatic | 20 diplomatic victory points |
| Score | Highest score at turn limit |

All victories trigger immediately when the condition is met — they do not wait for a turn boundary or WC session. A rival reaching 20 DVP wins before your next turn. The only counter is stripping DVP at a World Congress *before* they reach 20.

`end_turn` runs a victory proximity scan every turn and a full snapshot every 10 turns. These warnings are the primary signal for invisible victories — worth paying attention to.

## Game Recovery

**MCP autosaves:** `end_turn` automatically saves every turn as `0_MCP_NNNN` (last 5 kept). These are your primary recovery points.

**Load by name** (preferred — no `list_saves` needed):
```
load_game_save("0_MCP_0079")  # load specific turn (~5s via Lua, ~90s via menu fallback)
get_game_overview              # verify load
```

**When the game hangs** (AI turn loop):
```
restart_and_load("0_MCP_NNNN")   # kill + relaunch + load (~90s)
get_game_overview                 # verify load
```

**Turn regression detection:** If you accidentally load a wrong save (e.g. the T1 scenario save instead of your autosave), `end_turn` will emit a CRITICAL warning with the correct autosave name to reload.

Other tools: `list_saves`, `load_save(index)`, `kill_game`, `launch_game`, `load_save_from_menu(name)`.
Save names omit extension: `"AutoSave_0221"` not `"AutoSave_0221.Civ6Save"`.
