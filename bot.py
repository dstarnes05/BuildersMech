"""AegisApex: Master-Tier Mech Royale Tournament Agent.

Strategic Capabilities:
1. In-depth 3D Spatial Geometry: Pre-indexed building prisms & LiDAR terrain for microsecond raycast LOS & corner cover finding.
2. Independent Torso Mechanics: Backpedals & retreats with front torso facing pursuing threats, eliminating the 1.5x rear damage vulnerability and laying down suppressing fire.
3. Out-of-Combat Regeneration Tactics: Actively seeks LOS-breaking obstacles when injured, triggering 45 HP/s (or 72 HP/s with Nanites) healing to full vitality.
4. Tri-Weapon Precision Ballistics:
   - Arm Gun: Exact quadratic lead solver for 252 m/s straight projectiles.
   - Laser: Intelligent charge & release timing for instant 315-504 damage hitscan strikes (punishing Mortar carriers with +60% cook-off).
   - Lightning: 45m close-range hitscan arc to shut down charging enemy lasers and shred rushers.
   - Mortar: High-trajectory ballistic arc calculations to shell targets behind obstacles.
5. Zone & Positioning Awareness: Dynamic vector field combining goal navigation, building wall repulsion, storm wall repulsion, and laser dodging.
6. Economy & Scaling: Snipes power cubes, captures uncontested supply drops for instant full-HP restores, and auto-picks top-tier upgrades.
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
    "armor": 92,     # -8% damage taken (up to -24%)
    "plating": 88,   # +15% max HP (up to +45%)
    "laser_charge": 84,
    "laser_damage": 80,
    "gun_damage": 75,
    "gun_velocity": 70,
    "servos": 66,    # +8% movement speed
    "jets": 60,
    "lightning_range": 58,
    "lightning_efficiency": 58,
    "sensors": 52,
    "gun_rate": 45,
    "gun_mag": 45,
    "mortar_reload": 40,
    "reactor": 35,
    "capacitor": 30,
}


class Bot:
    name = "AegisApex"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.slot = hello.get("slot", 0)
        self.pickup_radius = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.cube_radius = cfg.get("cubes", {}).get("pickup_radius", 3.5)

        # Weapon configurations
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

        # Mech specifications
        mech_cfg = cfg.get("mech", {})
        self.walk_speed = mech_cfg.get("walk_speed", 11.7)
        self.boost_speed = mech_cfg.get("boost_speed", 19.5)
        self.regen_delay = mech_cfg.get("regen_delay", 5.0)

        # Load arena geometry
        map_dir = hello.get("map", {}).get("dir", "/arena")
        self.world = WorldModel(map_dir)

        # Combat & state tracking
        self.last_damaged_t: float = -100.0
        self.last_damage_bearing: Optional[float] = None
        self.is_charging_laser: bool = False
        self.target_id: Optional[int] = None

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

        # 1. Process damage events
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damaged_t = t
                self.last_damage_bearing = ev.get("bearing")

        # 2. Upgrade evaluation
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

        # 3. Weapon Management & Pickups
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
                    # Prioritize laser or lightning in slot 1
                    if p_kind == "laser" and w1.get("kind") != "laser" and not self.is_charging_laser:
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break
                    elif p_kind == "lightning" and w1.get("kind") not in ("laser", "lightning"):
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break

        # 4. Zone Tracking
        zone_info = obs.get("zone", {})
        cur_center, cur_radius = self._get_current_zone(zone_info, t)
        next_center = zone_info.get("next_center", cur_center)
        next_radius = float(zone_info.get("next_radius", cur_radius))

        # 5. Threat Perception & Target Selection
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", 7.0))
        my_muzzle = [x, y, z + muzzle_h]
        my_eyes = [x, y, z + 8.0]

        all_enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]

        primary_target: Optional[dict] = None
        target_com: Optional[List[float]] = None
        target_has_los = False

        # Weapon triage: Laser > Lightning > Gun > Mortar
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

        # Score potential targets
        best_target_score = -1e9
        for enemy in all_enemies:
            epos = enemy["pos"]
            ecom = [epos[0], epos[1], epos[2] + 4.5]
            dist_e = math.hypot(epos[0] - x, epos[1] - y)
            has_los = self.world.has_los(my_muzzle, ecom)

            score = 500.0 - dist_e
            if has_los:
                score += 1000.0
            # Target cookoff advantage: enemy holding mortar while we hold laser (+60% damage)
            if laser_slot is not None and "mortar" in enemy.get("weapons", []):
                score += 400.0
            # Low HP finish priority
            if enemy.get("hp", 1400.0) < 400.0:
                score += 300.0

            if score > best_target_score:
                best_target_score = score
                primary_target = enemy
                target_com = ecom
                target_has_los = has_los

        # 6. Combat Aiming & Trigger Control
        commanded_aim_yaw = me["aim"][0]
        commanded_aim_pitch = me["aim"][1]
        fire_slot0 = False
        fire_slot1 = False

        if primary_target and target_com:
            t_pos = primary_target["pos"]
            t_vel = primary_target.get("vel", [0.0, 0.0, 0.0])
            dist_to_target = math.hypot(t_pos[0] - x, t_pos[1] - y)

            # Arm Gun Interception
            lead = ballistics.solve_interception(my_muzzle, self.gun_speed, target_com, t_vel)
            if lead is not None:
                lead_yaw, lead_pitch, _, pred_pt = lead
                commanded_aim_yaw = lead_yaw
                commanded_aim_pitch = lead_pitch

                yaw_err = abs(ballistics.normalize_angle(me["aim"][0] - lead_yaw))
                if target_has_los and yaw_err < math.radians(6.5) and dist_to_target < self.gun_range:
                    if gun_slot == 0:
                        fire_slot0 = True
                    elif gun_slot == 1:
                        fire_slot1 = True

                mf.draw.line(my_muzzle, pred_pt, "#ff3322")

            # Lightning Arc (45m instant hitscan)
            if lightning_slot is not None and dist_to_target < 43.0 and target_has_los:
                yaw_to_target = math.atan2(t_pos[1] - y, t_pos[0] - x)
                angle_diff = abs(ballistics.normalize_angle(me["aim"][0] - yaw_to_target))
                if angle_diff < math.radians(28.0) and energy > 5.0:
                    if lightning_slot == 0:
                        fire_slot0 = True
                    else:
                        fire_slot1 = True

            # Laser Charge & Hitscan Fire
            if laser_slot is not None:
                laser_w = weapons[laser_slot]
                current_charge = float(laser_w.get("charge", 0.0)) if laser_w else 0.0
                direct_yaw, direct_pitch = ballistics.solve_direct_aim(my_muzzle, target_com)
                yaw_err_direct = abs(ballistics.normalize_angle(me["aim"][0] - direct_yaw))

                if target_has_los and dist_to_target < self.laser_range:
                    if current_charge < self.laser_charge_time:
                        # Charge laser
                        if laser_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
                        self.is_charging_laser = True
                    else:
                        # Full charge ready: Release trigger for hitscan strike!
                        if yaw_err_direct < math.radians(4.0):
                            if laser_slot == 0:
                                fire_slot0 = False
                            else:
                                fire_slot1 = False
                            self.is_charging_laser = False
                        else:
                            # Hold until torso slews on target
                            if laser_slot == 0:
                                fire_slot0 = True
                            else:
                                fire_slot1 = True
                else:
                    self.is_charging_laser = False

            # Mortar ballistic trajectory
            if mortar_slot is not None and not target_has_los and dist_to_target < 200.0:
                mortar_aim = ballistics.solve_mortar_aim(
                    [x, y, z + 9.5], target_com, self.mortar_speed, self.mortar_gravity
                )
                if mortar_aim:
                    commanded_aim_yaw, commanded_aim_pitch = mortar_aim
                    yaw_err_m = abs(ballistics.normalize_angle(me["aim"][0] - commanded_aim_yaw))
                    if yaw_err_m < math.radians(8.0):
                        if mortar_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
        else:
            # When no enemies are seen: aim towards recent damage or sound
            if self.last_damage_bearing is not None and (t - self.last_damaged_t) < 3.0:
                commanded_aim_yaw = self.last_damage_bearing
            elif obs.get("sounds"):
                recent_sound = obs["sounds"][0]
                commanded_aim_yaw = recent_sound.get("bearing", commanded_aim_yaw)

            # Auto-reload gun when safe
            if w0 and w0.get("kind") == "gun":
                ammo = w0.get("ammo", 12)
                if ammo <= 4 and w0.get("reload", 0.0) == 0.0:
                    act["reload"] = True

        # 7. Strategic Movement & Cover Mechanics
        is_injured = (hp < max_hp * 0.70) or ((t - self.last_damaged_t) < 4.0)
        goal_x, goal_y = next_center[0], next_center[1]
        request_boost = False

        if is_injured and primary_target:
            # Seek solid building cover to break enemy LOS and regenerate HP!
            cover_pt = self.world.find_cover(
                [x, y, z],
                primary_target["pos"],
                cur_center,
                cur_radius,
                search_radius=24.0,
            )
            if cover_pt:
                goal_x, goal_y = cover_pt[0], cover_pt[1]
                mf.draw.point([goal_x, goal_y, z], "#00ff88", 8)
                request_boost = (energy > 20.0)
            else:
                # Disengage away from threat
                away_x = x - primary_target["pos"][0]
                away_y = y - primary_target["pos"][1]
                dist_away = math.hypot(away_x, away_y)
                if dist_away > 0.1:
                    goal_x = x + (away_x / dist_away) * 35.0
                    goal_y = y + (away_y / dist_away) * 35.0
                request_boost = (energy > 25.0)

        elif primary_target and target_has_los:
            # Active kiting: maintain 38m - 50m standoff
            t_pos = primary_target["pos"]
            dist_t = math.hypot(t_pos[0] - x, t_pos[1] - y)
            vec_to_t_x = (t_pos[0] - x) / (dist_t + 1e-4)
            vec_to_t_y = (t_pos[1] - y) / (dist_t + 1e-4)

            if dist_t < 36.0:
                # Backpedal while torso continues aiming at enemy
                goal_x = x - vec_to_t_x * 20.0
                goal_y = y - vec_to_t_y * 20.0
            elif dist_t > 58.0:
                goal_x = x + vec_to_t_x * 15.0
                goal_y = y + vec_to_t_y * 15.0
            else:
                # Lateral circle-strafe
                perp_x = -vec_to_t_y
                perp_y = vec_to_t_x
                goal_x = x + perp_x * 12.0
                goal_y = y + perp_y * 12.0
        else:
            # Check supply drops: full HP, full energy, +3 cubes!
            ready_drop = None
            for drop in obs.get("drops", []):
                if drop.get("lands_in", 99.0) == 0.0:
                    d_pos = drop["pos"]
                    dist_drop = math.hypot(d_pos[0] - x, d_pos[1] - y)
                    if dist_drop < 45.0 and math.hypot(d_pos[0] - cur_center[0], d_pos[1] - cur_center[1]) < (cur_radius - 12.0):
                        ready_drop = drop
                        break

            if ready_drop:
                goal_x = ready_drop["pos"][0]
                goal_y = ready_drop["pos"][1]
                mf.draw.point([goal_x, goal_y, z], "#ffd700", 10)
            else:
                # Scavenge power cubes
                cubes = obs.get("cubes", [])
                safe_cubes = [
                    c for c in cubes
                    if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < 32.0
                    and math.hypot(c["pos"][0] - cur_center[0], c["pos"][1] - cur_center[1]) < (cur_radius - 12.0)
                ]
                if safe_cubes:
                    safe_cubes.sort(key=lambda c: math.hypot(c["pos"][0] - x, c["pos"][1] - y))
                    goal_x = safe_cubes[0]["pos"][0]
                    goal_y = safe_cubes[0]["pos"][1]
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

        # Repulsion from prism walls
        wall_rx, wall_ry = self.world.get_wall_repulsion(x, y, z, check_radius=5.5)
        vx += wall_rx * 1.8
        vy += wall_ry * 1.8

        # Storm wall repulsion: keep safely inside safe zone
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

        # Dodge charging lasers
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
                        vx += (dodge_dx / d_len) * 2.2
                        vy += (dodge_dy / d_len) * 2.2
                        request_boost = True

        # Clamp and format move vector
        mv_len = math.hypot(vx, vy)
        if mv_len > 0.05:
            move_cmd = [vx / mv_len, vy / mv_len]
        else:
            move_cmd = [0.0, 0.0]

        # Jump handling
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

        mf.draw.label(f"HP {hp:.0f}/{max_hp:.0f} · {'Cover' if is_injured else 'Combat'}")
        mf.draw.point([goal_x, goal_y, z], "#40c0ff", 6)

        return act


if __name__ == "__main__":
    mf.run(Bot)
