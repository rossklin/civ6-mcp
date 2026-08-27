"""High-level game state API with server-side narration.

Wraps GameConnection + lua into typed async methods that return
both structured data and human-readable narrated text. Has ZERO MCP
dependency — enabling multi-agent architectures where specialist servers
import the same GameState class but expose different tool subsets.
"""

from __future__ import annotations

import asyncio
import logging
import re

from typing import TYPE_CHECKING

from civ_mcp import lua as lq
from civ_mcp.connection import GameConnection
from civ_mcp.diary import (
    game_keyed_diary_path as _diary_path,
    get_current_plans as _get_current_plans,
)
from civ_mcp.lua._helpers import load_lua_template
from civ_mcp.narrate import (
    narrate_goody_rewards,
    narrate_move_discoveries,
    narrate_settle_candidates,
    narrate_test_trade,
)
from civ_mcp.narrate_unified import narrate_full_state

if TYPE_CHECKING:
    from civ_mcp.spatial import SpatialTracker

log = logging.getLogger(__name__)


class GameState:
    """High-level async API for Civ 6 game state + actions."""

    def __init__(self, connection: GameConnection):
        self.conn = connection
        self.spatial: SpatialTracker | None = None
        self._last_snapshot: lq.TurnSnapshot | None = None
        self._game_identity: tuple[str, int] | None = None  # (civ_type, seed)
        self._diary_written_turn: int | None = (
            None  # guard against double-write per turn
        )
        self._end_turn_blocked: bool = False  # last end_turn hit a blocker (diplo/WC)
        self._pending_end_turn: bool = False  # ACTION_ENDTURN already in flight
        self._pending_end_turn_from: int | None = (
            None  # turn number when ACTION_ENDTURN was sent
        )
        self._high_water_turn: int = 0  # highest turn seen (for regression detection)
        self._local_player_id: int = 0  # human player (always 0 in single-player)
        self._hang_retry_active: bool = False  # guard against recursive hang recovery
        self._last_game_over: lq.GameOverStatus | None = (
            None  # captured by execute_end_turn for server.py
        )
        # (ts, turn, save_name) for each successful save load — used to detect
        # save scumming in _check_save_scumming(). Bounded to last 50 entries.
        self._save_load_history: list[tuple[float, int, str]] = []
        self._run_aborted: bool = False  # set when save scumming threshold is exceeded
        # Per-turn advisor call budget — prevents compulsive advisor loops
        # (e.g. Gemini Pro's 1,567 get_wonder_advisor calls in a single turn).
        # Reset in execute_end_turn on successful turn advance.
        self._advisor_calls_this_turn: int = 0
        # One-shot warning from the most recent advisor call, consumed and
        # cleared by the server wrapper.
        self._advisor_budget_warning: str | None = None
        # Informational notes from auto-resolved diplomacy (e.g. war
        # declarations) stashed during end_turn, drained into the turn
        # report by build_post_turn_report.  Proposal sessions and pending
        # deals are auto-resolved SILENTLY — they expect a reaction the
        # agent has no path to give, and surfacing them invites hunting.
        self._diplo_auto_notes: list[str] = []
        # Diplomatic-state watch (pid -> last seen state name, e.g. "WAR"):
        # transitions into WAR/DENOUNCED are reported as turn-report notes
        # (end_turn._auto_clear_diplomacy).  Pre-marked by our own
        # declarations so they are not announced as foreign events.
        self._diplo_state_watch: dict[int, str] = {}

    async def get_game_identity(self) -> tuple[str, int]:
        """Return (civ_type_lower, random_seed) for the current game.

        Always queries the game so we detect new-game loads.  When the
        identity changes, all per-game cached state is reset.
        """
        code = (
            "local me = Game.GetLocalPlayer() "
            "local cfg = PlayerConfigurations[me] "
            'print("GAMESEED|" .. cfg:GetCivilizationTypeName() '
            '.. "|" .. tostring(GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED"))) '
            'print("---END---")'
        )
        lines = await self.conn.execute_write(code)
        for line in lines:
            if line.startswith("GAMESEED|"):
                parts = line.split("|")
                civ = parts[1].replace("CIVILIZATION_", "").lower()
                seed = int(parts[2])
                new_id = (civ, seed)
                if self._game_identity is not None and new_id != self._game_identity:
                    log.info("Game changed: %s → %s", self._game_identity, new_id)
                    self._last_snapshot = None
                    self._diary_written_turn = None
                    self._last_game_over = None
                    self._save_load_history = []
                    self._run_aborted = False
                    self._advisor_calls_this_turn = 0
                    self._advisor_budget_warning = None
                self._game_identity = new_id
                return self._game_identity
        return ("unknown", 0)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def check_game_over(self) -> lq.GameOverStatus | None:
        """Check if the game has ended (victory/defeat screen showing).

        Tries InGame context first (full detection with UI checks).
        Falls back to GameCore context (read-only, survives defeat screen)
        when InGame fails — this catches victories that freeze the InGame UI.
        """
        try:
            lines = await self.conn.execute_write(lq.build_gameover_check())
            return lq.parse_gameover_response(lines)
        except Exception:
            log.debug("Game-over check failed in InGame, trying GameCore")
        # Fallback: GameCore-only check (survives defeat screen)
        try:
            lines = await self.conn.execute_read(lq.build_gameover_check_gamecore())
            return lq.parse_gameover_response(lines)
        except Exception:
            log.debug("Game-over check failed in GameCore too", exc_info=True)
            return None

    async def spy_travel(self, unit_index: int, target_x: int, target_y: int) -> str:
        lua = lq.build_spy_travel(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spy_mission(
        self, unit_index: int, mission_type: str, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_spy_mission(unit_index, mission_type, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Action methods (run in InGame context for UnitManager access)
    # ------------------------------------------------------------------

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Pre-dismiss any blocking popups that would silently eat the move
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        # Pre-move: install the tribal-village (goody hut) reward listener and
        # snapshot the reward sequence. Best-effort — never blocks the move.
        # The listener is idempotent and re-installs after a save load, so this
        # also recovers from a recycled Lua state.
        goody_before_seq: int = -1
        goody_expect: bool = False
        try:
            snap_lines = await self.conn.execute_read(
                lq.build_goody_snapshot_query(target_x, target_y)
            )
            for sline in snap_lines:
                if sline.startswith("GOODY_SEQ|"):
                    sp = sline.split("|")
                    goody_before_seq = int(sp[1])
                    goody_expect = len(sp) > 2 and sp[2] == "1"
                    break
        except Exception:
            log.debug("Goody snapshot failed", exc_info=True)
        lua = lq.build_move_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Post-move: read actual position from GameCore (move is async in InGame)
        if result.startswith("MOVING_TO") or result.startswith("CAPTURE_MOVE"):
            try:
                pos_lines = await self.conn.execute_read(
                    lq.build_unit_position_query(
                        unit_index,
                        move_target_x=target_x,
                        move_target_y=target_y,
                    )
                )
                for line in pos_lines:
                    if line.startswith("POS|") and "GONE" not in line:
                        parts = line.split("|")
                        now_x, now_y = int(parts[1]), int(parts[2])
                        result += f"|now_at:{now_x},{now_y}"
                        from_match = re.search(r"\|from:(\d+),(\d+)", result)
                        if from_match:
                            from_x = int(from_match.group(1))
                            from_y = int(from_match.group(2))
                            if now_x == from_x and now_y == from_y:
                                reason = lq.parse_blocked_diagnostic(pos_lines)
                                result += f"|BLOCKED ({reason})"
                            else:
                                dx = now_x - from_x
                                dy = (
                                    now_y - from_y
                                )  # positive dy = south (higher Y = south in Civ 6)
                                result += f"|(moved dx:{dx:+d} dy:{dy:+d})"
                                tgt_match = re.search(
                                    r"(?:MOVING_TO|CAPTURE_MOVE)\|(\d+),(\d+)", result
                                )
                                if tgt_match:
                                    tx, ty = (
                                        int(tgt_match.group(1)),
                                        int(tgt_match.group(2)),
                                    )
                                    if (now_x, now_y) != (tx, ty):
                                        result += f"|STOPPED_MID_PATH (moves exhausted)"
                        break
            except Exception:
                pass
        # Post-move: capture any tribal-village (goody hut) reward the unit
        # received. The listener (installed above) logs each reward with a
        # monotonic seq; we diff against the pre-move snapshot. The reward
        # event fires during sim resolution and can lag the position read by a
        # frame, so when the unit moved onto a known goody hut we poll briefly.
        if goody_before_seq >= 0 and "|BLOCKED" not in result and (
            result.startswith("MOVING_TO") or result.startswith("CAPTURE_MOVE")
        ):
            try:
                rewards: list[lq.GoodyReward] = []
                attempts = 4 if goody_expect else 1
                for _ in range(attempts):
                    glog_lines = await self.conn.execute_read(
                        lq.build_read_goody_log(goody_before_seq)
                    )
                    rewards = lq.parse_goody_log(glog_lines)
                    if rewards or not goody_expect:
                        break
                    await asyncio.sleep(0.15)
                # Attribute to this unit by index (event passes either the full
                # unit ID or the bare index; % 65536 normalizes both). Fall back
                # to all new rewards if none match — during our turn only the
                # commanded unit is moving, so any new reward is ours.
                mine = [
                    r
                    for r in rewards
                    if (r.unit_id % 65536) == (unit_index % 65536)
                    or r.unit_id == unit_index
                ]
                reward_text = narrate_goody_rewards(mine if mine else rewards)
                if reward_text:
                    result += "\n" + reward_text
            except Exception:
                log.debug("Goody reward capture failed", exc_info=True)
        # Post-move: visibility diff for discovery feedback
        blocked = "|BLOCKED" in result
        if not blocked and self.spatial is not None and self.spatial._revealed_seeded:
            try:
                # Extract actual position from result
                now_match = re.search(r"now_at:(\d+),(\d+)", result)
                if now_match:
                    vis_x, vis_y = int(now_match.group(1)), int(now_match.group(2))
                    vis_lines = await self.conn.execute_read(
                        lq.build_post_move_visibility_query(vis_x, vis_y)
                    )
                    vis_tiles = lq.parse_post_move_visibility(vis_lines)
                    all_revealed = {(x, y) for x, y, _ in vis_tiles}
                    newly_revealed = self.spatial.mark_revealed(all_revealed)
                    if newly_revealed:
                        new_tile_data = [
                            (x, y, m)
                            for x, y, m in vis_tiles
                            if (x, y) in newly_revealed
                        ]
                        discovery_text = narrate_move_discoveries(
                            new_tile_data, len(newly_revealed)
                        )
                        if discovery_text:
                            result += "\n" + discovery_text
                        # Record discovery event in spatial tracker
                        await self.spatial.record_discovery(
                            "unit_action",
                            (vis_x, vis_y),
                            newly_revealed,
                            0,
                        )
            except Exception:
                log.debug("Post-move visibility diff failed", exc_info=True)
        return result

    async def attack_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Pre-attack: dismiss any blocking popups that would silently eat the attack
        try:
            await self.dismiss_popup()
        except Exception:
            pass
        lua = lq.build_attack_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Combat estimates now live in the game state (units section), not here.
        # Post-combat HP is unreadable from InGame within the same turn (async
        # combat resolution), but GameCore (execute_read) reflects the true
        # state once resolved. Poll GameCore for the actual outcome.
        is_melee = result.startswith("MELEE_ATTACK")
        is_ranged = result.startswith("RANGE_ATTACK")
        is_air = result.startswith("AIR_ATTACK")
        if not (is_melee or is_ranged or is_air):
            return result
        pre_enemy_hp = _extract_pre_hp(result)
        pre_att_hp = _extract_attacker_pre_hp(result)
        outcome: lq.AttackOutcome | None = None
        resolved = False
        for _ in range(4):
            await asyncio.sleep(0.4)
            try:
                out_lines = await self.conn.execute_read(
                    lq.build_attack_outcome_query(unit_index, target_x, target_y)
                )
            except Exception as e:
                log.debug("Attack outcome read failed: %s", e)
                continue
            outcome = lq.parse_attack_outcome(out_lines)
            if outcome is None:
                continue
            if not outcome.enemy_present:
                resolved = True
                break  # enemy gone -> killed
            if pre_enemy_hp is not None and outcome.enemy_hp != pre_enemy_hp:
                resolved = True
                break  # enemy took damage
            if (
                pre_att_hp is not None
                and outcome.attacker_hp >= 0
                and outcome.attacker_hp != pre_att_hp
            ):
                resolved = True
                break  # attacker took damage
        outcome_str = _format_attack_outcome(
            outcome, pre_enemy_hp, pre_att_hp, resolved
        )
        # City targets: wall/garrison HP comes from InGame (district defense
        # APIs are InGame-only). Fetch once when the target tile is a city.
        if outcome and outcome.is_city:
            try:
                await asyncio.sleep(0.2)
                followup = await self.conn.execute_write(
                    lq.build_attack_followup_query(target_x, target_y)
                )
                city_def = _extract_city_defense(followup)
                if city_def:
                    w_hp, w_max, g_hp, g_max = city_def
                    if w_max > 0:
                        outcome_str += f" | walls {w_hp}/{w_max}"
                    if g_max > 0:
                        outcome_str += f" | garrison {g_hp}/{g_max}"
            except Exception as e:
                log.debug("City defense followup failed: %s", e)
        return result + "\n  Post-combat: " + outcome_str

    async def city_attack(self, city_id: int, target_x: int, target_y: int) -> str:
        lua = lq.build_city_attack(city_id, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("CITY_RANGE_ATTACK"):
            pre_hp = _extract_pre_hp(result)
            await asyncio.sleep(0.3)
            followup: list[str] = []
            try:
                followup = await self.conn.execute_write(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception:
                followup = []
            try:
                followup_str = _format_attack_followup(followup)
                post_hp = _extract_post_hp(followup)
                damage_info = ""
                if pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif not any(l.startswith("UNIT|") for l in followup):
                    if pre_hp is not None:
                        damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("City attack followup failed: %s", e)
        return result

    async def resolve_city_capture(self, action: str) -> str:
        lua = lq.build_resolve_city_capture(action)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def found_city(self, unit_index: int) -> str:
        # Pre-dismiss any blocking popups (tech completion, era change, etc.)
        try:
            await self.dismiss_popup()
        except Exception:
            pass

        lua = lq.build_found_city(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if result.startswith("FOUNDED|"):
            # Extract coordinates from "FOUNDED|x,y"
            parts = result.split("|")[1].split(",")
            x, y = int(parts[0]), int(parts[1])
            # Verify city was actually created (RequestOperation is async)
            verify_lua = lq.build_verify_city_at(x, y)
            verify_lines = await self.conn.execute_read(verify_lua)
            # City IDs can be 0 (e.g. the capital), so check `is None`
            # rather than truthiness.
            city_id = lq.parse_verify_city_at(verify_lines)
            if city_id is None:
                # Retry once — popup may have blocked the async operation
                try:
                    await self.dismiss_popup()
                    lines = await self.conn.execute_write(lua)
                    retry_result = _action_result(lines)
                    if retry_result.startswith("FOUNDED|"):
                        verify_lines = await self.conn.execute_read(verify_lua)
                        city_id = lq.parse_verify_city_at(verify_lines)
                        if city_id is not None:
                            result = retry_result
                except Exception:
                    log.debug(
                        "found_city retry after popup dismiss failed", exc_info=True
                    )
                if city_id is None:
                    result = (
                        f"Error: FOUND_FAILED|Founding at {x},{y} was requested but "
                        "city did not appear despite popup dismissal."
                    )
            if city_id is not None:
                result += f"|city_id:{city_id}"

        # On settle failure, run the settle advisor to suggest alternatives
        if result.startswith("Error: CANNOT_FOUND") or result.startswith(
            "Error: FOUND_FAILED"
        ):
            try:
                advisor_result = await self.get_settle_advisor(unit_index)
                result += "\n\n" + advisor_result
            except Exception as e:
                log.debug("Settle advisor failed: %s", e)
        return result

    async def get_settle_advisor(self, unit_index: int) -> str:
        lua = lq.build_settle_advisor_query(unit_index)
        lines = await self.conn.execute_read(lua)
        candidates = lq.parse_settle_advisor_response(lines)
        if candidates:
            return narrate_settle_candidates(candidates)
        # Auto-fallback to global scan when no local candidates
        try:
            global_candidates = await self.get_global_settle_scan()
            if global_candidates:
                header = "No valid settle locations within 5 tiles. Best sites on revealed map:\n"
                return header + narrate_settle_candidates(global_candidates[:5])
        except Exception:
            log.debug("Global settle fallback failed", exc_info=True)
        return "No valid settle locations found within 5 tiles or on revealed map."

    async def get_global_settle_scan(self) -> list[lq.SettleCandidate]:
        lua = lq.build_global_settle_scan()
        lines = await self.conn.execute_read(lua)
        return lq.parse_settle_advisor_response(lines)

    async def fortify_unit(self, unit_index: int) -> str:
        lua = lq.build_fortify_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("SLEEPING"):
            return "Unit is sleeping (this unit type cannot fortify)"
        return result

    async def skip_unit(self, unit_index: int) -> str:
        lua = lq.build_skip_unit(unit_index)
        lines = await self.conn.execute_read(lua)
        return _action_result(lines)

    async def exit_formation(self, unit_index: int) -> str:
        lua = lq.build_exit_formation(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def enter_formation(self, unit_index: int, target_unit_index: int) -> str:
        lua = lq.build_enter_formation(unit_index, target_unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def skip_remaining_units(self) -> str:
        # First try to fortify/heal combat units (InGame context)
        fortify_result = ""
        try:
            lua_fort = lq.build_fortify_remaining_units()
            fort_lines = await self.conn.execute_write(lua_fort)
            fortify_result = _action_result(fort_lines)
        except Exception as e:
            log.debug("Fortify remaining failed: %s", e)
        # Then skip anything still with moves (GameCore context)
        lua = lq.build_skip_remaining_units()
        lines = await self.conn.execute_read(lua)
        skip_result = _action_result(lines)
        if fortify_result and not fortify_result.startswith("Error"):
            return f"{fortify_result}\n{skip_result}"
        return skip_result

    async def automate_explore(self, unit_index: int) -> str:
        lua = lq.build_automate_explore(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def heal_unit(self, unit_index: int) -> str:
        lua = lq.build_heal_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def alert_unit(self, unit_index: int) -> str:
        lua = lq.build_alert_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sleep_unit(self, unit_index: int) -> str:
        lua = lq.build_sleep_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def delete_unit(self, unit_index: int) -> str:
        lua = lq.build_delete_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def improve_tile(self, unit_index: int, improvement_name: str) -> str:
        lua = lq.build_improve_tile(unit_index, improvement_name)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_feature(self, unit_index: int) -> str:
        lua = lq.build_remove_feature(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def repair_improvement(self, unit_index: int) -> str:
        lua = lq.build_repair_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_improvement(self, unit_index: int) -> str:
        lua = lq.build_remove_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sacrifice_builder_charges(self, unit_index: int) -> str:
        lua = lq.build_sacrifice_builder_charges(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def build_route(self, unit_index: int) -> str:
        lua = lq.build_build_route(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def set_city_production(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        target_x: int | None = None,
        target_y: int | None = None,
    ) -> str:
        itype = item_type.upper()

        lua = lq.build_produce_item(city_id, item_type, item_name, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        # If CanStartOperation failed but CanProduce passed, verify via readback
        if any("MAYBE:" in l for l in lines):
            try:
                verify_lines = await self.conn.execute_read(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in l for l in verify_lines):
                    turns = ""
                    for vl in verify_lines:
                        if vl.startswith("CONFIRMED|"):
                            turns = vl.split("|", 1)[1]
                    return f"PRODUCING|{item_name}|{turns} (bypassed stale CanStartOperation)"
                else:
                    hint = ""
                    if itype == "DISTRICT":
                        hint = f" Tried ({target_x},{target_y})."
                        try:
                            placements = await self.get_district_advisor(
                                city_id, item_name
                            )
                            if isinstance(placements, list) and placements:
                                alts = ", ".join(
                                    f"({p.x},{p.y}) Adj +{p.total_adjacency}"
                                    for p in placements[:5]
                                )
                                hint += f" Valid tiles: {alts}."
                        except Exception:
                            pass
                        hint += " Use get_district_advisor for details."
                    elif itype == "BUILDING":
                        # Check if Lua reported pillaged districts
                        pillaged_dists = ""
                        for ml in lines:
                            if "PILLAGED:" in ml:
                                pillaged_dists = ml.split("PILLAGED:", 1)[1]
                                break
                        if pillaged_dists:
                            hint = (
                                f" Prerequisite district is pillaged:"
                                f" {pillaged_dists}. Repair it first via"
                                " set_city_production(city_id, 'DISTRICT',"
                                " 'DISTRICT_NAME', x, y) — use get_cities"
                                " to find district coordinates."
                            )
                        else:
                            bld_info = item_name.replace("BUILDING_", "")
                            hint = (
                                f" Hint: {bld_info} may require a completed"
                                " district or prerequisite building."
                            )
                    return f"Error: CANNOT_START|{item_name} cannot start.{hint}"
            except Exception:
                log.debug("Production readback failed", exc_info=True)
                return f"Error: CANNOT_START|{item_name} (readback failed)"

        # OK-path verification. RequestOperation is fire-and-forget; even
        # when CanStartOperation returned true it can silently no-op if the
        # queue is in a degenerate state. Round-trip read to confirm.
        if result.startswith("PRODUCING|"):
            try:
                verify_lines = await self.conn.execute_read(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in vl for vl in verify_lines):
                    return result
                not_set = next(
                    (vl for vl in verify_lines if vl.startswith("NOT_SET|")),
                    "NOT_SET|unknown",
                )
                return (
                    f"Error: SILENT_FAILURE|{item_name} appeared to set but "
                    f"the game engine did not persist it ({not_set}). Retry "
                    f"the same call, or use purchase_item to force-commit "
                    f"with gold/faith."
                )
            except Exception:
                log.debug("OK-path production verify failed", exc_info=True)

        return result

    async def purchase_item(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        yield_type: str = "YIELD_GOLD",
    ) -> str:
        lua = lq.build_purchase_item(city_id, item_type, item_name, yield_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def set_research(self, tech_name: str) -> str:
        lua = lq.build_set_research(tech_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "RESEARCHING" in result:
            # Verify InGame actually accepted it by comparing tech INDEX.
            # RequestPlayerOperation is fire-and-forget — it can silently no-op
            # while GetResearchingTech() still returns the OLD tech's index (!= -1).
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Technologies() do "
                f'if row.TechnologyType == "{tech_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetTechs():GetResearchingTech(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                gc_lua = lq.build_set_research_gamecore(tech_name)
                gc_lines = await self.conn.execute_read(gc_lua)
                return _action_result(gc_lines)
        return result

    async def set_civic(self, civic_name: str) -> str:
        lua = lq.build_set_civic(civic_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROGRESSING" in result:
            # Verify InGame actually accepted it by comparing civic INDEX.
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Civics() do "
                f'if row.CivicType == "{civic_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetCulture():GetProgressingCivic(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                lua_gc = lq.build_set_civic_gamecore(civic_name)
                gc_lines = await self.conn.execute_read(lua_gc)
                return _action_result(gc_lines)
        return result

    # ------------------------------------------------------------------
    # Diplomacy methods
    # ------------------------------------------------------------------

    async def get_diplomacy_sessions(self) -> list[lq.DiplomacySession]:
        """Scan for engine diplomacy sessions targeting the local player.

        Internal helper (not agent-reachable): AI-initiated sessions toward
        a managed agent are auto-resolved by end_turn — see
        ``end_turn._auto_clear_diplomacy``.  The agent has no interactive
        path for them by design.
        """
        lua = lq.build_diplomacy_session_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_diplomacy_sessions(lines)

    async def get_pending_deals(self) -> list[lq.PendingDeal]:
        """Scan for incoming trade-deal offers targeting the local player.

        Internal helper (not agent-reachable): AI-initiated deals toward a
        managed agent are auto-declined by end_turn — see
        ``end_turn._auto_clear_diplomacy``.
        """
        lua = lq.build_pending_deals_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pending_deals_response(lines)

    async def send_diplomatic_action(self, other_player_id: int, action: str) -> str:
        if action.upper() == "OPEN_BORDERS":
            # Session-based OPEN_BORDERS causes AI turn hang.
            # Route through the trade deal API instead (mutual open borders).
            return await self.propose_trade(
                other_player_id,
                offer_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
                request_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
            )
        is_war = action.upper().endswith("_WAR") and action.upper().startswith(
            "DECLARE_"
        )
        lua = lq.build_send_diplo_action(other_player_id, action.upper())
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if not result.startswith("Error"):
            # _action_result renders Lua ERR: lines as "Error: ..." — never
            # compare against the raw "ERR:" prefix.
            if is_war:
                # War session left open for ~8s so the leader animation
                # plays. Background task will close session + dismiss
                # DiplomacyActionView.
                asyncio.create_task(
                    self._cleanup_war_diplomacy(other_player_id)
                )
                # Pre-mark the state watch so end_turn's diff does not
                # announce OUR declaration as a foreign war event.
                self._diplo_state_watch[other_player_id] = "WAR"
            else:
                # One-way statement (denounce): the builder closes the
                # session and restores the UI in the same chunk, but the
                # statement events pop DiplomacyActionView asynchronously —
                # the same-frame hide runs before the view processes them,
                # leaving the leader screen up. Dismiss in a separate,
                # delayed round-trip.
                asyncio.create_task(self._cleanup_diplo_screen())
                if action.upper() == "DENOUNCE":
                    # Their state toward us may sour after we denounce them —
                    # pre-mark so the watcher does not report it as their
                    # denouncement of us.
                    self._diplo_state_watch[other_player_id] = "DENOUNCED"

        return result

    async def _cleanup_diplo_screen(self) -> None:
        """Background: dismiss the leader screen after a one-way statement.

        Same idiom as :meth:`_cleanup_war_diplomacy` and the diplo recipe
        teardown (DIPLO_EXECUTION_PLAN.md §2): engine-driven session events
        reach the view on later frames, so the dismiss must be its own
        delayed round-trip, never same-frame with the statement. It must
        run in the DiplomacyActionView context so it goes through the
        view's own ``Close()`` — raw hide events skip the teardown and
        froze the UI live (unbalanced bulk-hide bookkeeping).
        """
        await asyncio.sleep(2)
        try:
            from civ_mcp import handoff

            lines = await self.conn.execute_in_named_state(
                handoff.DIPLO_SHIM_STATE,
                handoff.build_dismiss_leader_screen_lua(),
            )
            if not any(l.startswith("DIPLO_VIEW_DISMISSED") for l in lines):
                # Close() failed — the screen is left open on purpose (raw
                # hide events would risk freezing the UI); the human can
                # close it manually.
                err = next(
                    (l[4:] for l in lines if l.startswith("ERR:")), "no result"
                )
                log.warning(
                    "Diplo screen dismiss failed (left open for manual "
                    "close): %s",
                    err,
                )
        except Exception as e:
            log.warning("Diplo screen cleanup failed: %s", e)

    async def _cleanup_war_diplomacy(self, other_player_id: int) -> None:
        """Background: dismiss war declaration diplomacy view after animation.

        Two-phase cleanup (must be separate Lua calls — the engine fires
        OnDiplomacySessionClosed asynchronously so the view needs a frame
        to transition from CONVERSATION_MODE to OVERVIEW_MODE):
        1. CloseSession — view transitions to OVERVIEW_MODE
        2. The view's own Close(), executed in the DiplomacyActionView
           context (handoff.build_dismiss_leader_screen_lua).  The old
           NaturalWonderPopup trick + raw hide events were unreliable and
           could leave the UI input-blocked.
        """
        await asyncio.sleep(8)
        try:
            from civ_mcp import handoff

            # Phase 1: close session → view goes to OVERVIEW_MODE
            await self.conn.execute_write(
                lq.build_war_close_session(other_player_id)
            )

            # Let engine process OnDiplomacySessionClosed
            await asyncio.sleep(1)

            # Phase 2: dismiss the view through its own Close()
            await self.conn.execute_in_named_state(
                handoff.DIPLO_SHIM_STATE,
                handoff.build_dismiss_leader_screen_lua(),
            )
        except Exception as e:
            log.warning("War diplomacy cleanup failed: %s", e)

    # ------------------------------------------------------------------
    # Trade deal methods (InGame context)
    # ------------------------------------------------------------------

    async def respond_to_deal(self, other_player_id: int, accept: bool) -> str:
        lua = lq.build_respond_to_deal(other_player_id, accept)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def propose_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_propose_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Dismiss the diplomacy UI the trade session popped.  Same idiom as
        # send_diplomatic_action: the session events reach the view on later
        # frames, so the dismiss is a separate delayed round-trip through the
        # view's own Close() (DiplomacyActionView context) — raw hide events
        # skip the view's teardown and can freeze the UI (see
        # handoff.build_dismiss_leader_screen_lua).
        asyncio.create_task(self._cleanup_diplo_screen())
        return result

    async def test_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_test_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = lq.parse_test_trade_response(lines)
        return narrate_test_trade(result)

    async def propose_peace(self, other_player_id: int) -> str:
        lua = lq.build_propose_peace(other_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("Error"):
            return result
        # War state is async — verify with a second round-trip
        verify_lines = await self.conn.execute_write(
            lq.build_check_war_state(other_player_id)
        )
        at_peace = any("AT_PEACE" in l for l in verify_lines)
        name = result.split("|", 1)[1] if "|" in result else f"player {other_player_id}"
        if at_peace:
            return f"ACCEPTED|Peace established with {name}"
        else:
            return f"REJECTED|{name} rejected your peace offer"

    async def form_alliance(self, other_player_id: int, alliance_type: str) -> str:
        lua = lq.build_form_alliance(other_player_id, alliance_type.upper())
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Policy methods (InGame context)
    # ------------------------------------------------------------------

    async def set_policies(self, assignments: dict[int, str]) -> str:
        lua = lq.build_set_policies(assignments)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        # The verification does not work, for some reason get_policies always returns
        # empty in this context. Sleeping for several seconds does not help. 
        # Have verified that the policies go through. If they fail the
        # agent will see it in the game state next turn. Leaving this here so
        # we can try to fix it later.

        # if not result.startswith("Error"):
        #     # Post-verify: RequestPolicyChanges can silently no-op (e.g. during era transitions)
        #     status = await self.get_policies()
        #     slot_map = {s.slot_index: s.current_policy for s in status.slots}
        #     mismatches = []
        #     for idx, pol in assignments.items():
        #         expected = None if pol.upper() == "NONE" else pol
        #         actual = slot_map.get(idx)
        #         if actual != expected:
        #             wanted = "EMPTY" if expected is None else pol
        #             got = actual or "EMPTY"
        #             mismatches.append(f"slot {idx} (wanted {wanted}, got {got})")
        #     if mismatches:
        #         result += (
        #             f"\nWARN:SILENT_FAILURE — engine rejected: {', '.join(mismatches)}. "
        #             "Try a different policy or retry next turn."
        #         )
        return result

    # ------------------------------------------------------------------
    # Governor methods (InGame context)
    # ------------------------------------------------------------------

    async def get_governors(self) -> lq.GovernorStatus:
        lua = lq.build_governors_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_governors_response(lines)

    async def appoint_governor(self, governor_type: str) -> str:
        lua = lq.build_appoint_governor(governor_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def assign_governor(self, governor_type: str, city_id: int) -> str:
        lua = lq.build_assign_governor(governor_type, city_id)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def promote_governor(self, governor_type: str, promotion_type: str) -> str:
        lua = lq.build_promote_governor(governor_type, promotion_type)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROMOTED" in result:
            # Verify promotion actually applied (RequestPlayerOperation is async)
            verify = await self.conn.execute_write(
                f"local me = Game.GetLocalPlayer(); "
                f"local pGovs = Players[me]:GetGovernors(); "
                f'local gov = GameInfo.Governors["{governor_type}"]; '
                f'local promo = GameInfo.GovernorPromotions["{promotion_type}"]; '
                f"if gov and promo then "
                f"  local g = pGovs:GetGovernor(gov.Hash); "
                f"  if g and g:HasPromotion(promo.Index) then "
                f'    print("VERIFIED") '
                f"  else "
                f'    print("ERR:PROMOTION_FAILED|{promotion_type} was not applied") '
                f"  end "
                f"else "
                f'  print("ERR:LOOKUP_FAILED") '
                f"end; "
                f'print("{lq.SENTINEL}")'
            )
            if any("ERR:" in l for l in verify):
                return _action_result(verify)
        return result

    # ------------------------------------------------------------------
    # Promotion methods
    # ------------------------------------------------------------------

    async def promote_unit(self, unit_id: int, promotion_type: str) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_promote_unit(unit_index, promotion_type)
        lines = await self.conn.execute_write(lua)  # InGame context
        return _action_result(lines)

    # ------------------------------------------------------------------
    # City-state / Envoy methods (InGame context)
    # ------------------------------------------------------------------

    async def send_envoy(self, city_state_player_id: int) -> str:
        lua = lq.build_send_envoy(city_state_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("OK:ENVOY_SENT"):
            # Verify token actually decremented (async race condition workaround)
            await asyncio.sleep(0.1)
            try:
                verify_lines = await self.conn.execute_write(
                    f"local me = Game.GetLocalPlayer(); "
                    f"print(Players[me]:GetInfluence():GetTokensToGive()); "
                    f'print("{lq.SENTINEL}")'
                )
                if verify_lines and verify_lines[0].strip().lstrip("-").isdigit():
                    actual = int(verify_lines[0].strip())
                    result += f" (verified remaining: {actual})"
            except Exception:
                log.debug("Envoy verification failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Pantheon methods (InGame context)
    # ------------------------------------------------------------------

    async def get_pantheon_status(self) -> lq.PantheonStatus:
        lua = lq.build_pantheon_status_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pantheon_status_response(lines)

    async def choose_pantheon(self, belief_type: str) -> str:
        lua = lq.build_choose_pantheon(belief_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Religion founding methods (InGame context)
    # ------------------------------------------------------------------

    async def get_religion_founding_status(self) -> lq.ReligionFoundingStatus:
        lua = lq.build_religion_beliefs_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_religion_beliefs_response(lines)

    async def found_religion(
        self, religion_type: str, follower_belief: str, founder_belief: str
    ) -> str:
        lua = lq.build_found_religion(religion_type, follower_belief, founder_belief)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Unit upgrade methods (InGame context)
    # ------------------------------------------------------------------

    async def check_unit_upgrade(self, unit_id: int) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_unit_upgrade_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def upgrade_unit(self, unit_id: int) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_upgrade_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Dedications / Commemorations
    # ------------------------------------------------------------------

    async def get_dedications(self) -> lq.DedicationStatus:
        lua = lq.build_dedications_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_dedications_response(lines)

    async def choose_dedication(self, dedication_index: int) -> str:
        lua = lq.build_choose_dedication(dedication_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # District / wonder advisor (with per-turn budget)
    # ------------------------------------------------------------------

    # Pathological loops (Gemini Pro's 1,567 calls in one turn) motivate a
    # per-turn budget. Opus averages 2-4 advisor calls/turn so 20 leaves
    # 5x headroom for legitimate exploration.
    ADVISOR_BUDGET_SOFT = 10
    ADVISOR_BUDGET_HARD = 20

    def _advisor_budget_check(self) -> tuple[str | None, str | None]:
        """Check advisor budget. Returns (hard_error, soft_warning).

        - hard_error: short-circuit string if budget exceeded (caller returns it)
        - soft_warning: string to prepend to the result, or None
        """
        # Increment unconditionally — the hard-cap path stays sticky until
        # the end-of-turn reset, and reporting the true call count is more
        # honest for logs and telemetry.
        self._advisor_calls_this_turn += 1
        n = self._advisor_calls_this_turn
        if n > self.ADVISOR_BUDGET_HARD:
            return (
                f"ERR:ADVISOR_BUDGET_EXCEEDED|You have made {n} advisor calls "
                f"this turn (limit {self.ADVISOR_BUDGET_HARD}). The advisors "
                f"rank placements; they are not for brute-forcing every "
                f"wonder or district. Make a decision with the information "
                f"you already have, skip this step, or end your turn. Budget "
                f"resets next turn.",
                None,
            )
        if n >= self.ADVISOR_BUDGET_SOFT:
            return (
                None,
                f"ADVISOR BUDGET WARNING: {n}/{self.ADVISOR_BUDGET_HARD} "
                f"advisor calls this turn. Consolidate your queries — the "
                f"advisors rank placements, not iterate through options.",
            )
        return None, None

    async def get_district_advisor(
        self, city_id: int, district_type: str
    ) -> list[lq.DistrictPlacement] | str:
        """Returns placements list, or an error string if placement is impossible."""
        hard_err, soft_warn = self._advisor_budget_check()
        if hard_err:
            return hard_err
        lua = lq.build_district_advisor_query(city_id, district_type)
        lines = await self.conn.execute_write(lua)
        # Check for error bail lines (parser only looks for DPLOT| and silently
        # discards errors, losing the actual reason for failure)
        for line in lines:
            if line.startswith("ERR:"):
                return line  # propagate the specific error to the agent
        # Warning only attaches to the success path — error-string returns
        # bypass the server wrapper's narration branch and would otherwise
        # leave a stale warning for the next advisor call.
        self._advisor_budget_warning = soft_warn
        return lq.parse_district_advisor_response(lines)

    # ------------------------------------------------------------------
    # Tile purchase methods (InGame context)
    # ------------------------------------------------------------------

    async def purchase_tile(self, city_id: int, x: int, y: int) -> str:
        lua = lq.build_purchase_tile(city_id, x, y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Government change (InGame context)
    # ------------------------------------------------------------------

    async def change_government(self, government_type: str) -> str:
        lua = lq.build_change_government(government_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great People (InGame context)
    # ------------------------------------------------------------------

    async def recruit_great_person(self, individual_id: int) -> str:
        lua = lq.build_recruit_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    async def patronize_great_person(
        self, individual_id: int, yield_type: str = "YIELD_GOLD"
    ) -> str:
        lua = lq.build_patronize_great_person(individual_id, yield_type)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    async def reject_great_person(self, individual_id: int) -> str:
        lua = lq.build_reject_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    # ------------------------------------------------------------------
    # Trade route methods (InGame context)
    # ------------------------------------------------------------------

    async def get_trade_destinations(
        self, unit_index: int
    ) -> list[lq.TradeDestination]:
        lua = lq.build_trade_destinations_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return lq.parse_trade_destinations_response(lines)

    async def make_trade_route(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_make_trade_route(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great Person activation (InGame context)
    # ------------------------------------------------------------------

    async def activate_great_person(self, unit_index: int) -> str:
        lua = lq.build_activate_great_person(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spread_religion(self, unit_index: int) -> str:
        lua = lq.build_spread_religion(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Trader teleport (InGame context)
    # ------------------------------------------------------------------

    async def teleport_to_city(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_teleport_to_city(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # World Congress (InGame context)
    # ------------------------------------------------------------------

    async def get_world_congress(self) -> lq.WorldCongressStatus:
        lua = lq.build_world_congress_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_world_congress_response(lines)

    async def vote_world_congress(
        self, resolution_hash: int, option: int, target_index: int, num_votes: int
    ) -> str:
        lua = lq.build_congress_vote(resolution_hash, option, target_index, num_votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def submit_congress(self) -> str:
        lua = lq.build_congress_submit()
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def queue_wc_votes(self, votes: list[dict]) -> str:
        """Store agent voting preferences and register WC event handler."""
        lua = lq.build_register_wc_voter(votes=votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # City yield focus (InGame context)
    # ------------------------------------------------------------------

    async def set_city_focus(self, city_id: int, focus: str) -> str:
        lua = lq.build_set_yield_focus(city_id, focus)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Snapshot-diff for turn event detection
    # ------------------------------------------------------------------

    async def _take_snapshot(
        self, overview: lq.GameOverview | None = None
    ) -> lq.TurnSnapshot:
        """Capture current game state for diffing."""
        if overview is None:
            ov_lines = await self.conn.execute_write(lq.build_overview_query())
            overview = lq.parse_overview_response(ov_lines)

        unit_lines = await self.conn.execute_write(lq.build_units_query())
        units = lq.parse_units_response(unit_lines)

        city_lines = await self.conn.execute_write(lq.build_cities_query())
        cities, _ = lq.parse_cities_response(city_lines)

        try:
            stk_lines = await self.conn.execute_write(lq.build_stockpile_query())
            stockpiles = lq.parse_stockpile_response(stk_lines)
        except Exception:
            log.debug("Stockpile query failed", exc_info=True)
            stockpiles = []

        return lq.TurnSnapshot(
            turn=overview.turn,
            units={u.unit_id: u for u in units},
            cities={
                c.city_id: lq.CitySnapshot(
                    city_id=c.city_id,
                    name=c.name,
                    population=c.population,
                    currently_building=c.currently_building,
                    food_surplus=c.food_surplus,
                    turns_to_grow=c.turns_to_grow,
                    loyalty=c.loyalty,
                    loyalty_per_turn=c.loyalty_per_turn,
                )
                for c in cities
            },
            current_research=overview.current_research,
            current_civic=overview.current_civic,
            stockpiles=stockpiles,
        )

    @staticmethod
    def _diff_snapshots(
        before: lq.TurnSnapshot, after: lq.TurnSnapshot
    ) -> list[lq.TurnEvent]:
        """Compare two snapshots and generate events."""
        events: list[lq.TurnEvent] = []

        # --- Unit events ---
        for uid, ub in before.units.items():
            if uid not in after.units:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="unit",
                        message=f"Your {ub.name} ({ub.unit_type}) was killed! Last seen at ({ub.x},{ub.y}).",
                    )
                )
            else:
                ua = after.units[uid]
                dmg = ub.health - ua.health
                if dmg > 0:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) took {dmg} damage! HP: {ua.health}/{ua.max_health} at ({ua.x},{ua.y}).",
                        )
                    )
                elif dmg < 0:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) healed {-dmg} HP. HP: {ua.health}/{ua.max_health}.",
                        )
                    )

        for uid, ua in after.units.items():
            if uid not in before.units:
                events.append(
                    lq.TurnEvent(
                        priority=3,
                        category="unit",
                        message=f"New unit: {ua.name} ({ua.unit_type}) at ({ua.x},{ua.y}).",
                    )
                )

        # --- City events ---
        for cid, cb in before.cities.items():
            if cid not in after.cities:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="city",
                        message=f"City {cb.name} was lost!",
                    )
                )
            else:
                ca = after.cities[cid]
                if ca.population > cb.population:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="city",
                            message=f"{ca.name} grew to population {ca.population}.",
                        )
                    )
                if (
                    cb.currently_building != "NONE"
                    and ca.currently_building != cb.currently_building
                ):
                    now = ca.currently_building
                    if now in ("NONE", "nothing"):
                        now = "nothing"
                    elif now == "CORRUPTED_QUEUE":
                        now = "nothing (queue invalidated — set new production)"
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="city",
                            message=f"{ca.name} finished building {cb.currently_building}. Now: {now}.",
                        )
                    )

        for cid, ca in after.cities.items():
            if cid not in before.cities:
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="city",
                        message=f"New city founded: {ca.name}!",
                    )
                )

        # --- Research/civic events ---
        if (
            before.current_research != "None"
            and after.current_research != before.current_research
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="research",
                    message=f"Research complete: {before.current_research}! Now: {after.current_research}.",
                )
            )

        if (
            before.current_civic != "None"
            and after.current_civic != before.current_civic
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="civic",
                    message=f"Civic complete: {before.current_civic}! Now: {after.current_civic}.",
                )
            )

        # --- Stockpile events ---
        before_stk = {s.name: s for s in before.stockpiles}
        after_stk = {s.name: s for s in after.stockpiles}
        for name, sa in after_stk.items():
            sb = before_stk.get(name)
            if sb and sb.amount > 0 and sa.amount == 0:
                net = sa.per_turn - sa.demand + sa.imported
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="resources",
                        message=f"DEPLETED: {name} stockpile hit 0 ({net:+d}/t) — units requiring {name} may be disbanded.",
                    )
                )

        events.sort(key=lambda e: e.priority)
        return events

    @staticmethod
    def _build_turn_report(
        turn_before: int,
        turn_after: int,
        events: list[lq.TurnEvent],
        stockpiles: list[lq.ResourceStockpile] | None = None,
        score: int | None = None,
    ) -> str:
        """Format turn events and notifications into a scannable report."""
        header = f"Turn {turn_before} -> {turn_after}"
        if score is not None:
            header += f" | Score: {score}"
        lines = [header]

        if stockpiles:
            visible = [
                s for s in stockpiles if s.amount > 0 or s.per_turn > 0 or s.demand > 0
            ]
            if visible:
                parts = []
                for s in visible:
                    net = s.per_turn - s.demand + s.imported
                    parts.append(f"{s.name} {s.amount}/{s.cap} ({net:+d}/t)")
                lines.append(f"Resources: {', '.join(parts)}")

        if events:
            lines.append("")
            lines.append("== Events ==")
            icons = {1: "!!!", 2: ">>", 3: "--"}
            for e in events:
                icon = icons.get(e.priority, "--")
                lines.append(f"  {icon} {e.message}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    async def end_turn(self, seat=None) -> str:
        """End the turn with snapshot-diff event detection.

        ``seat`` is supplied in human-vs-agent handoff mode so end-of-turn is
        detected by handing off the local-player slot rather than by the game
        turn incrementing (which only happens once the whole round completes).
        """
        from civ_mcp.end_turn import execute_end_turn

        return await execute_end_turn(self, seat)

    async def dismiss_popup(self) -> str:
        """Dismiss any blocking popup or UI overlay."""
        from civ_mcp.game_lifecycle import dismiss_popup

        return await dismiss_popup(self.conn)

    async def list_saves(self) -> str:
        """List available save files."""
        from civ_mcp.game_lifecycle import list_saves

        return await list_saves(self.conn)

    async def load_save(self, save_index: int) -> str:
        """Load a save file by index."""
        import time
        from civ_mcp.game_lifecycle import load_save

        result = await load_save(self.conn, save_index)
        if not result.startswith(("Error", "ERR", "FAILED")):
            self._record_save_load(f"index:{save_index}")
        return result

    async def load_game_save(self, save_name: str) -> str:
        """Load a save file by name (no list_saves prerequisite)."""
        from civ_mcp.game_lifecycle import load_game_save

        result = await load_game_save(self.conn, save_name)
        if not result.startswith(("Error", "ERR", "FAILED")):
            self._record_save_load(save_name)
        return result

    def _record_save_load(self, save_name: str) -> None:
        """Record a successful save load for scumming detection."""
        import time

        ts = time.time()
        turn = self._high_water_turn
        self._save_load_history.append((ts, turn, save_name))
        # Keep bounded
        if len(self._save_load_history) > 50:
            self._save_load_history = self._save_load_history[-50:]

    async def execute_lua(self, code: str, context: str = "gamecore") -> str:
        """Escape hatch: run arbitrary Lua code."""
        from civ_mcp.game_lifecycle import execute_lua

        return await execute_lua(self.conn, code, context)

    # ── Unified State Query ────────────────────────────────────────

    async def get_full_game_state(self, managed_ids: tuple[int, ...]) -> str:
        """Fetch all game state in a single Lua query.

        Returns a ``FullGameState`` dataclass with all read sections
        populated.  Use ``narrate_unified.narrate_full_state()`` to
        format for LLM consumption.
        """
        from civ_mcp.unified_state import fetch_full_state

        # First run section queries in the old format, that go through the parser/narrator roundabout
        state = await fetch_full_state(self.conn)
        # Unset available governors because they should be accessed through the get_available_governors tool        
        state.governors.available_to_appoint = []
        text = narrate_full_state(state, managed_ids)

        # Then run queries in the new format that output the correct format directly
        # Append the tech/civics query output
        lines: list[str] = await self.conn.execute_write(
            load_lua_template("tech_civics.lua")
        )
        text = text + "\n\n## Research & Civics\n" + "\n".join(lines)

        # Append the map query output
        lines: list[str] = await self.conn.execute_write(load_lua_template("map.lua"))
        text = text + """

## Map
The map consists of hexagonal tiles so each tile has six neighbours.
If the tile is a valid settle location it is marked [VSL].
Note: neighbours with "RC" have a river crossing.

"""
        text = text + "\n".join(lines)

        # Append the cities query output
        lines: list[str] = await self.conn.execute_write(load_lua_template("cities.lua"))
        text = text + "\n\n## Cities\n" + "\n".join(lines)

        # Append diary plans (long-term + next-turn) from the JSONL file
        try:
            civ_type, seed = await self.get_game_identity()
            path = _diary_path(civ_type, seed)
            plans = _get_current_plans(path)
            ntp = plans.get("next_turn_plan", "").strip()
            ltp = plans.get("long_term_plans", "").strip()
            notes = plans.get("notes", "").strip()
            if ltp or ntp or notes:
                text += "\n\n## DIARY"
                if ltp:
                    text += f"\nLong-term Plans:\n{ltp}"
                else:
                    text += "\nLong-term Plans: (none)"
                if ntp:
                    text += f"\n\nPlan for This Turn (from last turn):\n{ntp}"
                else:
                    text += "\n\nPlan for This Turn: (none)"
                if notes:
                    text += f"\n\nNotes (accumulated learnings):\n{notes}"
        except Exception:
            log.debug("Failed to append diary to full game state", exc_info=True)

        return text


def _action_result(lines: list[str]) -> str:
    """Parse OK:/ERR: prefixed action responses.

    Scans all lines for the first OK:/ERR: prefix, since LuaEvent
    callbacks (e.g. ShowIngameUI → BulkHide debug prints) can inject
    spurious output before the actual result line.
    """
    if not lines:
        return "Action completed (no response)."
    for line in lines:
        if line.startswith("OK:"):
            return line[3:]
        if line.startswith("ERR:"):
            return f"Error: {line[4:]}"
    # No OK/ERR found — return all lines for debugging
    return "\n".join(lines)


def _format_attack_followup(lines: list[str], attacker_owner: int = 0) -> str:
    """Format the GameCore follow-up read after an attack.

    Filters out units belonging to ``attacker_owner`` so that after a melee
    kill (where the attacker moves onto the target tile) we don't misreport
    our own unit's HP as the defender's.

    Also includes city wall/garrison HP when attacking a walled city.
    """
    parts = []
    for line in lines:
        if line.startswith("UNIT|"):
            fields = line.split("|")
            if len(fields) >= 4:
                # fields: UNIT|TYPE|hp/max|owner:N
                owner_str = fields[3]  # "owner:N"
                try:
                    owner_id = int(owner_str.split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                label = "(yours) " if owner_id == attacker_owner else ""
                parts.append(f"{label}{fields[1]} {fields[2]}")
            elif len(fields) >= 3:
                parts.append(f"{fields[1]} {fields[2]}")
    city_def = _extract_city_defense(lines)
    if city_def:
        wall_hp, wall_max, gar_hp, gar_max = city_def
        if wall_max > 0:
            parts.append(f"Walls {wall_hp}/{wall_max}")
        if gar_max > 0:
            parts.append(f"City garrison {gar_hp}/{gar_max}")
    if not parts:
        return "Target eliminated"
    return ", ".join(parts)


def _extract_pre_hp(result: str) -> int | None:
    """Extract pre-attack enemy HP from the attack result line (``pre_hp:N/``)."""
    import re

    m = re.search(r"pre_hp:(\d+)/", result)
    if m:
        return int(m.group(1))
    return None


def _extract_attacker_pre_hp(result: str) -> int | None:
    """Extract pre-attack attacker HP from the ``your HP:N`` field."""
    import re

    m = re.search(r"your HP:(\d+)", result)
    if m:
        return int(m.group(1))
    return None


def _format_attack_outcome(
    outcome: lq.AttackOutcome | None,
    pre_enemy_hp: int | None,
    pre_att_hp: int | None,
    resolved: bool,
) -> str:
    """Build the human-readable post-combat line from a GameCore outcome read."""
    if outcome is None:
        return "outcome unavailable (GameCore read failed) — verify with get_units"
    parts: list[str] = []
    if not outcome.enemy_present:
        if pre_enemy_hp is not None:
            parts.append(f"enemy HP: {pre_enemy_hp} -> KILLED")
        else:
            parts.append("enemy KILLED")
    else:
        if pre_enemy_hp is not None:
            parts.append(
                f"enemy HP: {pre_enemy_hp} -> {outcome.enemy_hp}/{outcome.enemy_max}"
            )
        else:
            parts.append(f"enemy HP: {outcome.enemy_hp}/{outcome.enemy_max}")
    if outcome.attacker_hp >= 0:
        if pre_att_hp is not None:
            parts.append(f"your HP: {pre_att_hp} -> {outcome.attacker_hp}")
        else:
            parts.append(f"your HP: {outcome.attacker_hp}/{outcome.attacker_max}")
    if not resolved:
        parts.append("(HP unchanged — combat may not have resolved; verify with get_units)")
    return " | ".join(parts)


def _extract_post_hp(followup_lines: list[str], attacker_owner: int = 0) -> int | None:
    """Extract post-combat *enemy* HP from followup query lines.

    Followup format: UNIT|UNIT_TYPE|hp/max|owner:N
    Skips units belonging to ``attacker_owner`` (after melee kill, attacker
    occupies the target tile and would otherwise be misread as defender).
    Returns HP of first enemy unit found (None if eliminated).
    """
    for line in followup_lines:
        if line.startswith("UNIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    owner_id = int(parts[3].split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                if owner_id == attacker_owner:
                    continue  # our unit, not the target
            if len(parts) >= 3:
                hp_part = parts[2].split("/")[0]
                try:
                    return int(hp_part)
                except ValueError:
                    pass
    return None


def _extract_city_defense(
    followup_lines: list[str],
) -> tuple[int, int, int, int] | None:
    """Extract wall and garrison HP from CITY_DEF followup line.

    Returns ``(wall_hp, wall_max, garrison_hp, garrison_max)`` or *None*
    when the target tile has no city defenses.
    """
    for line in followup_lines:
        if line.startswith("CITY_DEF|"):
            # CITY_DEF|wall:74/100|garrison:197/200
            wall_hp = wall_max = gar_hp = gar_max = 0
            for part in line.split("|")[1:]:
                if part.startswith("wall:"):
                    hp, mx = part[5:].split("/")
                    wall_hp, wall_max = int(hp), int(mx)
                elif part.startswith("garrison:"):
                    hp, mx = part[9:].split("/")
                    gar_hp, gar_max = int(hp), int(mx)
            return (wall_hp, wall_max, gar_hp, gar_max)
    return None
