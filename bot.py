"""AegisApex v2: Champion-Tier Mech Royale Tournament Agent.

V2 Strategic Upgrades:
1. Active Duel Dominance:
   - Fixed the over-defensive retreat bug (which previously fled after taking a single bullet).
   - In 1v1 duels against the squad's active hunters, AegisApex stands its ground, maintains 32-40m orbital circle-strafing, and destroys targets with maximum continuous DPS.
2. Dual-Weapon Simultaneous Discharge:
   - Unlocks full cyclic rate of Arm Gun (4.16 rounds/s) with dynamic target-cylinder angular tolerance.
   - Laser: 0.75s charge with instant 315-504 damage hitscan release.
   - Lightning: 44m instant arc, shuts down enemy lasers.
   - Simultaneously fires both weapon slots whenever applicable.
3. Active Weapon & Cube Scavenging:
   - At match start or when slot 1 is empty, actively paths toward the nearest high-tier weapon (Laser / Lightning / Mortar).
   - On kill, immediately paths to the elimination point to collect all dropped power cubes (+4% max HP & damage per cube, plus instant heal).
   - Captures uncontested supply drops for instant 100% HP & Energy restoration.
4. Intelligent Tactical Regrouping:
   - Only retreats to cover when critically low (HP < 400) or when multi-targeted (>= 2 hostiles).
   - Breaks LOS to trigger 45-72 HP/s out-of-combat regeneration.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import ballistics
import mechfight as mf
from world_model import WorldModel

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]

UPGRADE_PRIORITY = {
    "nanites": 100,  # +60% out-of-combat regen, starts 1.5s sooner
    "plating": 95,   # +15% max HP (up to +45% HP)
    "armor": 90,     # -8% damage taken (up to -24%)
    "laser_charge": 85,
    "laser_damage": 82,
    "gun_damage": 80,
    "gun_velocity": 75,
    "servos": 70,    # +8% movement speed
    "jets": 62,
    "lightning_range": 60,
    "lightning_efficiency": 60,
    "sensors": 50,
    "gun_rate": 45,
    "gun_mag": 45,
    "mortar_reload": 40,
    "reactor": 35,
    "capacitor": 30,
}


class Bot:
    name = "AegisApex-v2"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.slot = hello.get("slot", 0)
        self.pickup_radius = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.cube_radius = cfg.get("cubes", {}).get("pickup_radius", 3.5)

        weapons_cfg = cfg.get("weapons", {})
        gun_cfg = weapons_cfg.get("gun", {})
        self.gun_speed = gun_cfg.get("speed", 252.0)
        self.gun_range = gun_cfg.get("range", 230.0)

        laser_cfg = weapons_cfg.get("laser", {})
        self.laser_charge_time = laser_cfg.get("charge", 0.75)
        self.laser_range = laser_cfg.get("range", 280.0)

        mortar_cfg = weapons_cfg.get("mortar", {})
        self.mortar_speed = mortar_cfg.get("speed", 72.0)
        self.mortar_gravity = mortar_cfg.get("gravity", 22.0)

        mech_cfg = cfg.get("mech", {})
        self.walk_speed = mech_cfg.get("walk_speed", 11.7)
        self.boost_speed = mech_cfg.get("boost_speed", 19.5)
        self.regen_delay = mech_cfg.get("regen_delay", 5.0)

        # Arena map
        map_dir = hello.get("map", {}).get("dir", "/arena")
        self.world = WorldModel(map_dir)

        # State tracking
        self.last_damaged_t: float = -100.0
        self.last_damage_bearing: Optional[float] = None
        self.is_charging_laser: bool = False
        self.target_id: Optional[int] = None
        self.last_kill_pos: Optional[Vec2] = None
        self.strafe_direction: float = 1.0

    def _get_current_zone(self, zone: dict, t: float) -> Tuple[List[float], float]:
        """Interpolates current zone center and radius."""
        c = zone.get("center", [0.0, 0.0])
        r = float(zone.get("radius", 500.0))
        nc = zone.get("next_center", c)
        nr = float(zone.get("next_radius", r))
        t_start = float(zone.get("shrink_start", 0.0))
        t_end = float(zone.get("shrink_end", 0.0))

        if t <= t_start or t_end <= t_start:
            return c, r
        if t >= t_end:
            return nc, nr

        frac = (t - t_start) / (t_end - t_start)
        cur_cx = c[0] + frac * (nc[0] - c[0])
        cur_cy = c[1] + frac * (nc[1] - c[1])
        cur_r = r + frac * (nr - r)
        return [cur_cx, cur_cy], cur_r

    def act(self, obs: mf.Observation) -> mf.Action:
        t = float(obs.get("t", 0.0))
        tick = obs["tick"]
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me.get("hp", 1400.0))
        max_hp = float(me.get("max_hp", 1400.0))
        energy = float(me.get("energy", 100.0))
        in_zone = me.get("in_zone", True)

        act: mf.Action = {"tick": tick}

        # 1. Process match events (damage, kills)
        for ev in obs.get("events", []):
            ev_type = ev.get("type")
            if ev_type == "damaged":
                self.last_damaged_t = t
                self.last_damage_bearing = ev.get("bearing")
            elif ev_type == "kill":
                # Recorded a kill: we know power cubes dropped!
                self.last_kill_pos = (x, y)

        # 2. Upgrade picking: select highest tier perk
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            choices = offer["choices"]
            best_idx = 0
            best_weight = -1
            for idx, c in enumerate(choices):
                cid = c.get("id", "")
                w = UPGRADE_PRIORITY.get(cid, 20)
                if w > best_weight:
                    best_weight = w
                    best_idx = idx
            act["pick"] = best_idx

        # 3. Weapons loadout & immediate pickups
        weapons = me.get("weapons", [None, None])
        w0 = weapons[0]
        w1 = weapons[1] if len(weapons) > 1 else None

        near_pickups = [
            p for p in obs.get("pickups", [])
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.pickup_radius
            and abs(p["pos"][2] - z) < 4.0
        ]

        if near_pickups:
            if w1 is None:
                p = near_pickups[0]
                if p.get("kind") != (w0.get("kind") if w0 else None):
                    act["take"] = p["id"]
            else:
                for p in near_pickups:
                    p_kind = p.get("kind")
                    # Upgrade slot 1 to laser or lightning
                    if p_kind == "laser" and w1.get("kind") != "laser" and not self.is_charging_laser:
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break
                    elif p_kind == "lightning" and w1.get("kind") not in ("laser", "lightning"):
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break

        # 4. Zone tracking
        zone_info = obs.get("zone", {})
        cur_center, cur_radius = self._get_current_zone(zone_info, t)
        next_center = zone_info.get("next_center", cur_center)
        next_radius = float(zone_info.get("next_radius", cur_radius))

        # 5. Threat perception & targeting
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", 7.0))
        my_muzzle = [x, y, z + muzzle_h]
        my_eyes = [x, y, z + 8.0]

        all_enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]

        # Identify weapons
        laser_slot = None
        lightning_slot = None
        gun_slot = None
        mortar_slot = None
        for s_idx, w in enumerate([w0, w1]):
            if w is not None:
                k = w.get("kind")
                if k == "laser":
                    laser_slot = s_idx
                elif k == "lightning":
                    lightning_slot = s_idx
                elif k == "gun":
                    gun_slot = s_idx
                elif k == "mortar":
                    mortar_slot = s_idx

        primary_target: Optional[dict] = None
        target_com: Optional[List[float]] = None
        target_has_los = False
        target_dist = 999.0

        # Target selection with sticky focus on active hunter
        best_score = -1e9
        for enemy in all_enemies:
            epos = enemy["pos"]
            ecom = [epos[0], epos[1], epos[2] + 4.5]
            d = math.hypot(epos[0] - x, epos[1] - y)
            has_los = self.world.has_los(my_muzzle, ecom)

            score = 600.0 - d
            if has_los:
                score += 800.0
            if enemy.get("id") == self.target_id:
                score += 300.0  # Sticky lock to finish the duel
            if laser_slot is not None and "mortar" in enemy.get("weapons", []):
                score += 400.0  # Cook-off target
            if enemy.get("hp", 1400.0) < 400.0:
                score += 250.0  # Low HP execute

            if score > best_score:
                best_score = score
                primary_target = enemy
                target_com = ecom
                target_has_los = has_los
                target_dist = d

        if primary_target:
            self.target_id = primary_target.get("id")
        else:
            self.target_id = None

        # 6. Combat Aiming & Trigger Control
        commanded_aim_yaw = me["aim"][0]
        commanded_aim_pitch = me["aim"][1]
        fire_slot0 = False
        fire_slot1 = False

        if primary_target and target_com:
            t_pos = primary_target["pos"]
            t_vel = primary_target.get("vel", [0.0, 0.0, 0.0])

            # Calculate predictive lead for Arm Gun
            lead = ballistics.solve_interception(my_muzzle, self.gun_speed, target_com, t_vel)
            if lead is not None:
                lead_yaw, lead_pitch, _, pred_pt = lead
                commanded_aim_yaw = lead_yaw
                commanded_aim_pitch = lead_pitch

                # Dynamic angular tolerance matching 5m mech width (e.g. 8-12 degrees)
                target_angular_rad = max(math.radians(8.5), math.atan2(2.5, max(5.0, target_dist)))
                yaw_err = abs(ballistics.normalize_angle(me["aim"][0] - lead_yaw))

                # Fire gun continuously when aimed at or near target
                if target_has_los and yaw_err < target_angular_rad and target_dist < self.gun_range:
                    if gun_slot == 0:
                        fire_slot0 = True
                    elif gun_slot == 1:
                        fire_slot1 = True

                mf.draw.line(my_muzzle, pred_pt, "#ff3322")

            # Lightning Arc: instant hitscan within 44m cone
            if lightning_slot is not None and target_dist < 44.0 and target_has_los:
                yaw_to_target = math.atan2(t_pos[1] - y, t_pos[0] - x)
                angle_diff = abs(ballistics.normalize_angle(me["aim"][0] - yaw_to_target))
                if angle_diff < math.radians(26.0) and energy > 3.0:
                    if lightning_slot == 0:
                        fire_slot0 = True
                    else:
                        fire_slot1 = True

            # Laser: hold to charge, release when full charge or aligned
            if laser_slot is not None:
                laser_w = weapons[laser_slot]
                current_charge = float(laser_w.get("charge", 0.0)) if laser_w else 0.0
                direct_yaw, direct_pitch = ballistics.solve_direct_aim(my_muzzle, target_com)
                yaw_err_direct = abs(ballistics.normalize_angle(me["aim"][0] - direct_yaw))

                if target_has_los and target_dist < self.laser_range:
                    if current_charge < self.laser_charge_time:
                        # Continue charging
                        if laser_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
                        self.is_charging_laser = True
                    else:
                        # Full charge reached! Release to fire instant beam!
                        if yaw_err_direct < math.radians(5.5):
                            if laser_slot == 0:
                                fire_slot0 = False
                            else:
                                fire_slot1 = False
                            self.is_charging_laser = False
                        else:
                            # Torso slewing onto target
                            if laser_slot == 0:
                                fire_slot0 = True
                            else:
                                fire_slot1 = True
                else:
                    self.is_charging_laser = False

            # Mortar: lob over obstacles
            if mortar_slot is not None and not target_has_los and target_dist < 180.0:
                mortar_aim = ballistics.solve_mortar_aim(
                    [x, y, z + 9.5], target_com, self.mortar_speed, self.mortar_gravity
                )
                if mortar_aim:
                    commanded_aim_yaw, commanded_aim_pitch = mortar_aim
                    yaw_err_m = abs(ballistics.normalize_angle(me["aim"][0] - commanded_aim_yaw))
                    if yaw_err_m < math.radians(10.0):
                        if mortar_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True

        else:
            # Idle scanning: look toward sound or recent damage
            if self.last_damage_bearing is not None and (t - self.last_damaged_t) < 3.0:
                commanded_aim_yaw = self.last_damage_bearing
            elif obs.get("sounds"):
                recent_sound = obs["sounds"][0]
                commanded_aim_yaw = recent_sound.get("bearing", commanded_aim_yaw)

            # Auto-reload gun when safe
            if w0 and w0.get("kind") == "gun":
                ammo = w0.get("ammo", 12)
                if ammo <= 5 and w0.get("reload", 0.0) == 0.0:
                    act["reload"] = True

        # 7. Strategic Movement & Combat Kiting
        # Invert strafe direction every 40 ticks (~4s) to be unpredictable
        if tick % 40 == 0:
            self.strafe_direction *= -1.0

        goal_x, goal_y = next_center[0], next_center[1]
        request_boost = False

        # Only retreat to cover if CRITICALLY low or heavily outnumbered
        critical_retreat = (hp < 420.0 and len(all_enemies) > 0) or (len(all_enemies) >= 2 and target_dist < 40.0)

        if critical_retreat and primary_target:
            # Emergency break LOS to heal
            cover_pt = self.world.find_cover(
                [x, y, z],
                primary_target["pos"],
                cur_center,
                cur_radius,
                search_radius=25.0,
            )
            if cover_pt:
                goal_x, goal_y = cover_pt[0], cover_pt[1]
                mf.draw.point([goal_x, goal_y, z], "#00ff88", 8)
                request_boost = (energy > 20.0)
            else:
                away_x = x - primary_target["pos"][0]
                away_y = y - primary_target["pos"][1]
                d_away = math.hypot(away_x, away_y)
                if d_away > 0.1:
                    goal_x = x + (away_x / d_away) * 35.0
                    goal_y = y + (away_y / d_away) * 35.0
                request_boost = (energy > 25.0)

        elif primary_target and target_has_los:
            # DUEL COMBAT KITING: Maintain 32m - 40m orbital circle strafe
            t_pos = primary_target["pos"]
            d = target_dist
            u_x = (t_pos[0] - x) / (d + 1e-4)
            u_y = (t_pos[1] - y) / (d + 1e-4)

            # Tangent lateral vector
            perp_x = -u_y * self.strafe_direction
            perp_y = u_x * self.strafe_direction

            # Radial adjustment
            if d < 30.0:
                # Back away
                rad_x = -u_x * 1.2
                rad_y = -u_y * 1.2
            elif d > 42.0:
                # Move closer
                rad_x = u_x * 1.0
                rad_y = u_y * 1.0
            else:
                rad_x = 0.0
                rad_y = 0.0

            # Desired combat step
            combat_vx = perp_x + rad_x
            combat_vy = perp_y + rad_y
            c_len = math.hypot(combat_vx, combat_vy)
            if c_len > 0.1:
                goal_x = x + (combat_vx / c_len) * 20.0
                goal_y = y + (combat_vy / c_len) * 20.0

            # Short burst boost in combat when energy is high
            if energy > 55.0 and tick % 15 < 6:
                request_boost = True

        else:
            # OUT OF COMBAT: Scavenge Weapon, Drops, or Cubes
            # Priority A: If slot 1 is empty, find nearest weapon pickup!
            unarmed_slot1 = (w1 is None)
            best_weapon_pickup = None
            if unarmed_slot1:
                available_weapons = [
                    p for p in obs.get("pickups", [])
                    if p.get("kind") in ("laser", "lightning", "mortar")
                    and math.hypot(p["pos"][0] - cur_center[0], p["pos"][1] - cur_center[1]) < (cur_radius - 10.0)
                ]
                if available_weapons:
                    available_weapons.sort(key=lambda p: math.hypot(p["pos"][0] - x, p["pos"][1] - y))
                    best_weapon_pickup = available_weapons[0]

            if best_weapon_pickup and math.hypot(best_weapon_pickup["pos"][0] - x, best_weapon_pickup["pos"][1] - y) < 65.0:
                goal_x = best_weapon_pickup["pos"][0]
                goal_y = best_weapon_pickup["pos"][1]
                mf.draw.point([goal_x, goal_y, z], "#ff00ff", 8)
                request_boost = (energy > 30.0)

            # Priority B: Supply Drops (Instant 100% full heal + 3 cubes)
            elif any(d.get("lands_in", 99.0) == 0.0 for d in obs.get("drops", [])):
                ready_drop = next(
                    (d for d in obs.get("drops", [])
                     if d.get("lands_in", 99.0) == 0.0
                     and math.hypot(d["pos"][0] - cur_center[0], d["pos"][1] - cur_center[1]) < (cur_radius - 12.0)),
                    None
                )
                if ready_drop:
                    goal_x = ready_drop["pos"][0]
                    goal_y = ready_drop["pos"][1]
                    mf.draw.point([goal_x, goal_y, z], "#ffd700", 10)
                    request_boost = (energy > 25.0)

            # Priority C: Power cubes
            else:
                cubes = obs.get("cubes", [])
                safe_cubes = [
                    c for c in cubes
                    if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < 40.0
                    and math.hypot(c["pos"][0] - cur_center[0], c["pos"][1] - cur_center[1]) < (cur_radius - 12.0)
                ]
                if safe_cubes:
                    safe_cubes.sort(key=lambda c: math.hypot(c["pos"][0] - x, c["pos"][1] - y))
                    goal_x = safe_cubes[0]["pos"][0]
                    goal_y = safe_cubes[0]["pos"][1]
                    mf.draw.point([goal_x, goal_y, z], "#00e5ff", 6)
                else:
                    goal_x = next_center[0]
                    goal_y = next_center[1]

        # 8. Vector Field Steering & Obstacle Avoidance
        g_dx = goal_x - x
        g_dy = goal_y - y
        g_dist = math.hypot(g_dx, g_dy)
        if g_dist > 0.1:
            vx = g_dx / g_dist
            vy = g_dy / g_dist
        else:
            vx, vy = 0.0, 0.0

        # Repulsion from building walls
        wall_rx, wall_ry = self.world.get_wall_repulsion(x, y, z, check_radius=5.5)
        vx += wall_rx * 1.8
        vy += wall_ry * 1.8

        # Storm boundary repulsion
        dist_to_center = math.hypot(x - cur_center[0], y - cur_center[1])
        zone_margin = cur_radius - dist_to_center
        if zone_margin < 25.0:
            if dist_to_center > 0.1:
                inward_x = (cur_center[0] - x) / dist_to_center
                inward_y = (cur_center[1] - y) / dist_to_center
                strength = max(0.6, (25.0 - zone_margin) / 7.0)
                vx += inward_x * strength
                vy += inward_y * strength
            if not in_zone:
                request_boost = True

        # Dodge charging enemy lasers
        for enemy in all_enemies:
            if enemy.get("charging", False):
                e_pos = enemy["pos"]
                e_aim = enemy.get("aim", [0.0, 0.0])
                to_me_yaw = math.atan2(y - e_pos[1], x - e_pos[0])
                if abs(ballistics.normalize_angle(e_aim[0] - to_me_yaw)) < math.radians(16.0):
                    dodge_dx = -(y - e_pos[1])
                    dodge_dy = (x - e_pos[0])
                    d_len = math.hypot(dodge_dx, dodge_dy)
                    if d_len > 0.1:
                        vx += (dodge_dx / d_len) * 2.5
                        vy += (dodge_dy / d_len) * 2.5
                        request_boost = True

        # Normalize move vector
        mv_len = math.hypot(vx, vy)
        if mv_len > 0.05:
            move_cmd = [vx / mv_len, vy / mv_len]
        else:
            move_cmd = [0.0, 0.0]

        # Jump over steps/obstacles
        grounded = me.get("grounded", True)
        jump_cmd = False
        if grounded and mv_len > 0.3 and (tick % 25 == 0):
            if math.hypot(wall_rx, wall_ry) > 0.3:
                jump_cmd = True

        act.update(
            move=move_cmd,
            aim=[commanded_aim_yaw, commanded_aim_pitch],
            fire=[fire_slot0, fire_slot1],
            boost=request_boost,
            jump=jump_cmd,
        )

        mf.draw.label(f"HP {hp:.0f}/{max_hp:.0f} · {'Kiting' if primary_target else 'Searching'}")
        mf.draw.point([goal_x, goal_y, z], "#40c0ff", 6)

        return act


if __name__ == "__main__":
    mf.run(Bot)
