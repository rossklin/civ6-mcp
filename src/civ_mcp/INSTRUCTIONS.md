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
5. `end_turn` — advance the turn and get your post-turn report.
6. Think about what to do next turn and whether your long-term plans need updating.
7. `update_diary(next_turn_plan=..., long_term_plans=..., notes=...)` — record your plans.
8. `wait_for_turn()` — block until your next turn starts. Call again on timeout.

## Tool execute_commands
Execute a batch of game commands sequentially. Here is the full reference of each command.

Args:
    commands_json: A JSON array of command objects. Each object has:
        - action: The command name (a game action name, case-sensitive)
        - params: Dict of parameters for that command

Example:
    [{"action": "move_unit", "params": {"unit_index": 0, "target_x": 10, "target_y": 20}},
        {"action": "set_city_production", "params": {"city_id": 3, "item_type": "UNIT", "item_name": "UNIT_SETTLER"}},
        {"action": "set_research", "params": {"tech_name": "TECH_IRON_WORKING"}}]

Commands execute in order. Movement/attack commands return visibility
intel (newly revealed tiles, enemy units) and combat results inline, so
you can call this tool multiple times per turn to scout first, then act
on what you learn. Prefer fewer, larger batches where possible.

Conventions:
    - Coordinates: ``target_x``/``target_y`` are map tiles.
    - ``unit_index``: the ``idx`` field from the Units section of
    get_full_game_state. Most unit commands take this.
    - ``unit_id``: unit id from Units section.
    - ``city_id``: city ID from the Cities section.
    - Player IDs: ``other_player_id`` / ``city_state_player_id`` are the P0,
    P1, ... IDs from get_seats.
    - String enums are case-insensitive (uppercased internally). The
    ``item_name``/``tech_name``/etc. values are GameInfo type strings
    (e.g. UNIT_WARRIOR, TECH_IRON_WORKING) — use the names shown in
    get_full_game_state's production/research lists.

Command reference (params in parentheses; ``?`` = optional):

Units:
    move_unit(unit_index, target_x, target_y) — move toward a tile
    attack_unit(unit_index, target_x, target_y) — attack enemy at tile
    fortify_unit(unit_index)
    skip_unit(unit_index)
    skip_remaining_units() — fortify combat units, then skip the rest
    automate_explore(unit_index)
    heal_unit(unit_index)
    alert_unit(unit_index)
    sleep_unit(unit_index)
    delete_unit(unit_index)
    enter_formation(unit_index, target_unit_index) — join escort formation
    exit_formation(unit_index)
    promote_unit(unit_id, promotion_type) — e.g. PROMOTION_CITY_ASSAULT
    upgrade_unit(unit_id)
    check_unit_upgrade(unit_id) — returns upgrade cost/availability

Settling & cities:
    found_city(unit_index)
    resolve_city_capture(action) — action: keep | reject | raze |
    liberate_founder | liberate_previous
    set_city_production(city_id, item_type, item_name, target_x?, target_y?)
        item_type: UNIT | BUILDING | DISTRICT; item_name e.g. UNIT_WARRIOR,
        BUILDING_GRANARY, DISTRICT_CAMPUS. DISTRICTs and wonders require
        target_x/target_y (use get_district_advisor / get_wonder_advisor).
        purchase_item(city_id, item_type, item_name, yield_type="YIELD_GOLD")
        item_type: UNIT | BUILDING; yield_type: YIELD_GOLD | YIELD_FAITH.
    list_city_production(city_id) — what the city can build now
    set_city_focus(city_id, focus) — focus: DEFAULT (clear) | FOOD |
        PRODUCTION | GOLD | SCIENCE | CULTURE | FAITH
    purchase_tile(city_id, x, y)
    city_attack(city_id, target_x, target_y) — ranged attack from a city

Builders & improvements:
    improve_tile(unit_index, improvement_name) — e.g. IMPROVEMENT_MINE
    remove_feature(unit_index)
    repair_improvement(unit_index)
    remove_improvement(unit_index)
    build_route(unit_index)
    sacrifice_builder_charges(unit_index)

Research & civics:
    set_research(tech_name) — e.g. TECH_IRON_WORKING
    set_civic(civic_name) — e.g. CIVIC_CRAFTSMANSHIP

Diplomacy & trade (other_player_id = target player ID):
    send_diplomatic_action(other_player_id, action) — action: DIPLOMATIC_DELEGATION
        | RESIDENT_EMBASSY | DECLARE_FRIENDSHIP | DENOUNCE
        | DECLARE_SURPRISE_WAR | DECLARE_FORMAL_WAR | DECLARE_HOLY_WAR
        | DECLARE_LIBERATION_WAR | DECLARE_RECONQUEST_WAR
        | DECLARE_PROTECTORATE_WAR | DECLARE_COLONIAL_WAR | DECLARE_TERRITORIAL_WAR.
        For the three response-able actions (DIPLOMATIC_DELEGATION,
        RESIDENT_EMBASSY, DECLARE_FRIENDSHIP) targeting a managed civ, the
        proposal is filed in the DIPLOMACY MAILBOX instead of the engine —
        opening a session would let the target's built-in AI auto-respond. The
        target answers on its own turn; your action takes effect on your next
        turn. One-way actions (DENOUNCE, war) and actions to unmanaged civs go
        straight to the engine.
    get_diplomacy_sessions(): Check for open diplomacy sessions and return choices
    diplomacy_respond(other_player_id, response) — response: POSITIVE | NEGATIVE
        | EXIT (reply to an open leader dialogue; check get_diplomacy_sessions first)
    propose_trade(other_player_id, ...) — pass FLAT params (auto-converted):
        offer_gold, offer_gold_per_turn, offer_resources (comma-separated
        RESOURCE_TYPE names), offer_favor, offer_open_borders (bool),
        plus the request_* equivalents; joint_war_target (player ID) for a joint war.
    propose_peace(other_player_id, ...) — propose peace, you can add trade items here
    form_alliance(other_player_id, alliance_type, ...) — alliance_type:
        MILITARY | RESEARCH | CULTURAL | ECONOMIC | RELIGIOUS (required).
        You can also add trade items.
    test_trade(other_player_id, offer_items, request_items) — dry-run check against default AI player.
        Each item dict: {type: GOLD|RESOURCE|FAVOR|AGREEMENT|CITY, amount,
        name, duration, subtype, city_id}.
    respond_to_deal(other_player_id, accept: bool) — accept/reject an
        AI-proposed deal.
    respond_to_trade(other_player_id, accept: bool) — accept/reject an
        incoming mailbox deal from a managed civ (see DEAL MAILBOX in state).
    respond_to_diplo_action(other_player_id, accept: bool) — accept/reject
        an incoming DIPLOMACY MAILBOX proposal (friendship/delegation/embassy)
        from a managed civ. Accept marks it; the proposer's action takes effect
        on the proposer's next turn. Reject discards it.

Messaging (managed-player chat; see MESSAGES in state):
    send_message(other_player_id, text) — send a free-text message to a
        managed civ or the human. To a managed civ it is filed for that agent
        to read next turn; to the human it is also rendered in their native
        in-game chat panel. Incoming messages to this seat appear in the
        === MESSAGES === section of get_full_game_state.

Governance:
    set_policies(assignments) — assignments: {slot_index: "POLICY_TYPE"};
        use "NONE" to clear a slot
    change_government(government_type) — e.g. GOVERNMENT_OLIGARCHY
    appoint_governor(governor_type) — e.g. GOVERNOR_THE_CARDINAL
    assign_governor(governor_type, city_id)
    promote_governor(governor_type, promotion_type)
    send_envoy(city_state_player_id)
    choose_dedication(dedication_index)

Religion & Great People:
    choose_pantheon(belief_type) — e.g. BELIEF_RELIGIOUS_SETTLEMENTS
    found_religion(religion_type, follower_belief, founder_belief)
    recruit_great_person(individual_id)
    patronize_great_person(individual_id, yield_type="YIELD_GOLD")
    reject_great_person(individual_id)
    activate_great_person(unit_index)
    spread_religion(unit_index)

Trade routes & spies:
    make_trade_route(unit_index, target_x, target_y)
    teleport_to_city(unit_index, target_x, target_y) — relocate a trader
    spy_travel(unit_index, target_x, target_y)
    spy_mission(unit_index, mission_type, target_x, target_y) — mission_type:
        COUNTERSPY | GAIN_SOURCES | SIPHON_FUNDS | STEAL_TECH_BOOST
        | SABOTAGE_PRODUCTION | GREAT_WORK_HEIST | RECRUIT_PARTISANS
        | NEUTRALIZE_GOVERNOR | FABRICATE_SCANDAL (offensive missions need
        the spy in the target city first)

World Congress:
    queue_wc_votes(votes) — votes: list of {hash, option (1=A|2=B), target,
        votes}; registers a one-shot handler that casts them at end of turn
    vote_world_congress(resolution_hash, option, target_index, num_votes)
        — option 1=A, 2=B; target_index is 0-based
    submit_congress() — submit votes and resume the turn

## Diary

The diary is your persistent memory across sessions and turns. When you start a turn, `get_full_game_state` includes your diary, so you pick up exactly where you left off. The diary has three parts:

- **`next_turn_plan`**: Your concrete plan for the NEXT turn. Be specific — unit movements, production choices, research targets. This is overwritten each turn, so only the most recent entry matters. Write this from the perspective of what you intend to do when you get the turn back.
- **`long_term_plans`**: Your long-term strategy — victory path, expansion goals, tech progression timeline, diplomatic posture. Pass the complete current version each time you call `update_diary`; last write wins.
- **`notes`**: Durable learnings worth remembering across the whole game — game rules you discovered, mistakes you made and corrected, things the civilopedia taught you. Unlike the plan fields, notes are **appended** to the existing notes each call (with a turn marker) rather than replacing them, so they accumulate over the game. Leave empty to leave the notes unchanged. Use this for facts, not transient plans. Example: you tried to move onto an enemy unit's tile and the action was rejected — read the civilopedia, learned you must declare war first — record that in `notes` so you never repeat the mistake.

Call `update_diary` after `end_turn()` and before `wait_for_turn()` — once per turn cycle. Think while other players are on the clock: based on what you observed this turn and the post-turn report (which `wait_for_turn` will return), what should you do next? Do your long-term plans still hold? Write the answers into the diary so your next invocation starts with context.

The diary appears in `get_full_game_state` output — no separate tool needed. When you resume a game after context compaction, `get_full_game_state` alone is enough to reconstruct your strategic context.

## Strategic Patterns

### Moving units
Moving into a tile costs movement points depending on the terrain and features. Hills cost 2 movement, forests/jungles cost 2, and they stack (forest-hills = 3+). A unit which hasn't moved yet can always move into an adjacent tile regardless of cost, but once it has moved it can't 
enter an adjacent tile unless it has sufficient points left. Map tiles show movement cost (`[mv:2]`, `[mv:3]`) and road presence — route 
units along roads when possible.

Before moving a builder, settler, or trader to a new tile, consider if there are threats. Civilians have zero combat strength — a single barbarian scout captures them. The cost of losing a builder (5-7 turns of production + charges) is almost always worse than taking one extra turn to check or escort.

`get_pathing_estimate(unit_id, target_x, target_y)` estimates how many turns a unit needs to reach a destination, using the game's actual pathfinding. Use it before committing units to long marches, but beware that it may take a weird path to avoid temporary blockage such as 
units or unexplored tiles.

Common reasons unit movement does not go as expected:
- failure to account for map features like crossing a river
- miscalculated adjacency, eg (x+1, y+1) is adjacent to (x,y) but (x+2, y+2) is not adjacent to (x+1, y+1)
- zone of control (ZOC): when your unit moves adjacent to an object which exerts ZOC, it can't move further that turn
except to attack the ZOC object. Melee units (land and naval), cities, encampments and units with a ZOC promotion exert ZOC.
However cavalry units ignore ZOC.

### Builder Management
Idle builders are wasted production. The builder tasks section shows all tiles needing improvements across your empire, prioritized (URGENT > HIGH > NORMAL), with the nearest idle builder for each task. These tasks are naive recommendations and you need to make your own judgement. For instance they may recommend you to build a mine when it is better to keep the forest on the hill, or to place an improvement that you do not have the required tech to place.

Before building an improvement, consider whether it actually improves the yields of the city. For instance if your city is currently working
a grassland hill with forest (2 food, 2 production), building a farm on a plains would create a 2 food 1 production tile which would not
be worthwhile for the city to work. 

### Spending Gold & Faith
Gold and faith sitting idle lose value over time. `purchase_item(city_id, item_type, item_name)` buys units/buildings instantly with gold (or faith via `yield_type="YIELD_FAITH"`). `purchase_tile(city_id, x, y)` buys a specific tile. `patronize_great_person` buys a GP outright. If you're saving, name the item and the turn — otherwise, deploy it.

### Expansion
Each city multiplies your districts, yields, and Great Person generation. The gap between a 3-city and 5-city empire by the Medieval era is hard to recover from. If city count is lagging, a settler is typically the highest-impact production choice — more so than most infrastructure in existing cities. Check loyalty before settling: negative-loyalty sites near rivals need a governor assigned immediately via `assign_governor(governor_type, city_id)` or they'll flip.

### Growth
Stagnant cities fall behind exponentially. If any city has food surplus ≤ 0, that's worth fixing this turn (Farm, Granary, domestic Trade Route, or `set_city_focus(city_id, "FOOD")`). Turns-to-growth over 15 is a signal the city needs food or housing.

### Exploration
You can't settle what you can't see, and you can't counter threats you don't know exist. A scout set to `automate_explore` is one of the best investments in the early game. If a scout is lost or stuck, replacing it early keeps the information flow going.

### Diplomacy
Diplomatic and trade actions are issued through `execute_commands`; see the Diplomacy reference section for the full action list. In shared (handoff) games, proposals to managed civs are routed through a mailbox so the built-in AI does not auto-answer them. You can also use the send_message command to strategize, cooperate with or manipulate your opponents.

### Favor resource
Diplomacy generates yield: each alliance +1 favor/turn per alliance level, each suzerainty +1 favor/turn. Government tier also gives favor. This compounds. Friendships don't give favor directly but enable alliances (which do). Delegations (25g) are cheap on first meeting. Friendships open up when a civ is Friendly. Alliances require friendship (30+ turns) and Diplomatic Service civic. Embassies are available once Writing is researched.

If favor is accumulating above 100 with no World Congress imminent, it's worth thinking about whether it could be better deployed in trade or alliance building.

### War Declaration
War declarations take effect for diplomacy immediately but the **combat engine does not sync until the next turn**. After declaring war via `send_diplomatic_action`, units cannot attack the new enemy until the following turn. Plan accordingly: declare war on turn N, position units adjacent to targets, then attack on turn N+1. Do not reload or retry if attacks return `NO_ENEMY` on the declaration turn — this is expected behavior.

### Wartime
Cities with walls can fire at enemies via `city_attack(city_id, target_x, target_y)` (range 2). Cities that fall are expensive to recover — when you capture a city, `resolve_city_capture(action)` with `keep`, `reject`, `raze`, or `liberate_founder`/`liberate_previous` resolves the decision. If your military strength is significantly below an enemy's and you're not making progress, `propose_peace(other_player_id)` — available after a 10-turn cooldown — is usually better than a war of attrition while the rest of the map moves on.

### Military Readiness
Keep an eye on opponents' military strength. A neighbor at 2x+ your strength who isn't a friend or ally is a risk worth taking seriously. Make sure you have a plan to handle if your opponent becomes aggressive. Units become progressively weaker relative to rivals if not upgraded (Slinger→Archer with Archery, Warrior→Swordsman with Iron Working) — use `upgrade_unit`.

### Barbarian Camps
Camps upgrade with the era — an Ancient-era camp spawns Warriors; the same camp in the Medieval era spawns Man-at-Arms. Clearing a camp within a few turns of finding it is almost always easier than fighting the units it produces over many turns.

### Religion
Religious victory is the easiest win condition to miss because it produces no notifications and unfolds slowly. If a rival religion reaches majority in most civs, the window for a response narrows quickly. Religious units bought from a city carry **that city's majority religion** — buy them from cities where your own religion is majority, not a converted city.

To found a religion: build a Holy Site → earn a Great Prophet → `get_religion_beliefs()` to see available beliefs → `found_religion(religion_type, follower_belief, founder_belief)`. The Great Prophet pool fills early (roughly half the major civs).

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
| `move_unit` | Move to tile | unit_index, target_x, target_y required |
| `attack_unit` | Attack enemy | unit_index, target_x, target_y; shows damage estimate; melee/ranged auto-detected |
| `fortify_unit` | +4 defense, heals | Military only |
| `heal_unit` | Fortify until full HP | Auto-wakes at full HP |
| `alert_unit` | Sleep, wake on enemy | Sentry use |
| `sleep_unit` | Sleep indefinitely | Manual wake required |
| `skip_unit` | End unit's turn | Always works |
| `automate_explore` | Auto-explore | Scouts only |
| `delete_unit` | Disband unit | Removes maintenance |
| `found_city` | Settle | Settlers only |
| `improve_tile` | Build improvement | Builders and Military Engineers; see improvements below |
| `remove_feature` | Chop/harvest feature | Builders only; removes forest, jungle, or marsh from tile |
| `build_route` | Build road/railroad | Military Engineers only; on current tile; no charges used |
| `make_trade_route` | Start route | Traders; target_x/y of destination city |
| `teleport_to_city` | Move idle trader | Traders only; target_x/y of city |
| `activate_great_person` | Use Great Person | Must be on completed matching district |
| `spread_religion` | Spread religion | Missionaries/Apostles |

Common improvements: `IMPROVEMENT_FARM`, `IMPROVEMENT_MINE`, `IMPROVEMENT_QUARRY`, `IMPROVEMENT_PLANTATION`, `IMPROVEMENT_PASTURE`, `IMPROVEMENT_CAMP`, `IMPROVEMENT_FISHING_BOATS`, `IMPROVEMENT_LUMBER_MILL`

Feature removal: Forest, jungle, and marsh tiles block most improvements (e.g. Farm). Use `remove_feature` to chop/harvest the feature first, then `improve_tile` to build. Lumber Mill and Camp work on forest/jungle without removal. Check `valid_improvements` in `get_units` output — if FARM isn't listed on a tile you expect it, the tile likely has a blocking feature.

Builders repair tile improvements via `repair_improvement(unit_index)`. Pillaged **district buildings** (Workshop, Arena, etc.) are repaired via `set_city_production`.

Military Engineers (requires Encampment + Armory): `build_route` builds a railroad on the current tile (no charges consumed; costs 1 Iron + 1 Coal per tile). `improve_tile` with `IMPROVEMENT_FORT` or `IMPROVEMENT_AIRSTRIP` uses charges. Building a railroad consumes all movement — one tile per engineer per turn.

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
- **City Capture**: conquered or disloyal city — `resolve_city_capture("keep"/"reject"/"raze"/"liberate_founder"/"liberate_previous")`
- Move responses show the **target tile**, not arrival position (async pathfinding)

## Diplomacy

**Reactive (AI-initiated):** AI encounters block turn progression. Use `diplomacy_respond(other_player_id, response)` with POSITIVE/NEGATIVE/EXIT to reply to an open leader dialogue. Diplomacy sessions do not affect unit movement or orders — continue commanding units normally afterward.

**Proactive:**
- `send_diplomatic_action(other_player_id, action)` — action: DIPLOMATIC_DELEGATION (25g, worth sending on first meeting), DECLARE_FRIENDSHIP (requires Friendly status), RESIDENT_EMBASSY (requires Writing tech), plus DENOUNCE and the war declarations. For the three response-able actions (delegation/embassy/friendship) targeting a managed civ, the proposal is filed in the DIPLOMACY MAILBOX instead of the engine — the target answers on its own turn and your action takes effect on your next turn. One-way actions (DENOUNCE, war) and actions to unmanaged civs go straight to the engine.
- `form_alliance(other_player_id, alliance_type)` — alliance_type: MILITARY/RESEARCH/CULTURAL/ECONOMIC/RELIGIOUS; requires declared friendship + Diplomatic Service civic. Targeting a managed civ routes through the deal mailbox after an eligibility check.
- `propose_trade(other_player_id, ...)` — pass FLAT params: offer_gold, offer_gold_per_turn, offer_resources (comma-separated RESOURCE_TYPE names), offer_favor, offer_open_borders, plus the request_* equivalents; joint_war_target (player ID) for a joint war. Targeting a managed civ routes through the deal mailbox.
- `test_trade(other_player_id, offer_items, request_items)` — dry-run check against the default AI player without committing. Each item dict: {type: GOLD|RESOURCE|FAVOR|AGREEMENT|CITY, amount, name, duration, subtype, city_id}.
- `propose_peace(other_player_id)` — white peace; eligibility (at war, past cooldown) is checked first. Targeting a managed civ routes through the deal mailbox.
- `respond_to_deal(other_player_id, accept)` — accept/reject an AI-proposed deal.
- `respond_to_trade(other_player_id, accept)` — accept/reject an incoming mailbox deal from a managed civ (see DEAL MAILBOX in get_full_game_state).
- `respond_to_diplo_action(other_player_id, accept)` — accept/reject an incoming DIPLOMACY MAILBOX proposal (friendship/delegation/embassy) from a managed civ. Accept marks it; the proposer's action takes effect on the proposer's next turn.
- Check the diplomacy section of `get_full_game_state` for defensive pacts before declaring war.
- Leader agendas appear in the diplomacy section of `get_full_game_state` — historical agendas are always visible; random agendas require Secret diplomatic visibility (spy in their capital or alliance). Use agendas to predict AI behavior and avoid relationship penalties.

**Espionage:** `spy_travel(unit_index, target_x, target_y)` to a city first, then `spy_mission(unit_index, mission_type, target_x, target_y)` to run operations. mission_type: COUNTERSPY | GAIN_SOURCES | SIPHON_FUNDS | STEAL_TECH_BOOST | SABOTAGE_PRODUCTION | GREAT_WORK_HEIST | RECRUIT_PARTISANS | NEUTRALIZE_GOVERNOR | FABRICATE_SCANDAL. Offensive missions only work after the spy arrives.

**City-states:** `send_envoy(city_state_player_id)`. Suzerainty = +1 favor/turn. Types: Scientific/Industrial/Trade/Cultural/Religious/Militaristic.

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
- `make_trade_route(unit_index, target_x, target_y)` → start route
- Domestic routes: food + production to new cities. International: gold.
- Capacity: 1 from Foreign Trade civic, +1 per Market/Lighthouse
- Idle routes are free yields going uncollected

## Great People

- `recruit_great_person(individual_id)` — recruit with accumulated GP points (check `[CAN RECRUIT]`)
- `patronize_great_person(individual_id)` — buy instantly with gold or faith
- `reject_great_person(individual_id)` — pass, advance to next candidate in that class
- Rivals will recruit what you pass on — recruiting quickly tends to be worth it
- Once recruited, move the GP to its matching completed district; `activate_great_person(unit_index)`
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

## Additional getters
Some parts of the game state were deemed rarely needed and kept in separate getters:

get_pathing_estimate
get_available_governors
get_pantheon_beliefs
get_religion_beliefs
get_dedications
get_trade_destinations
