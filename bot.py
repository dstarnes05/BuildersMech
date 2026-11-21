"""AegisApex v6: Master of the Arena.

V6 Strategic Breakthroughs:
1. Exact Parabolic Mortar Impact Prediction & Evasion:
   - Predicts 3D parabolic trajectory of mortar shells falling under gravity (22 m/s^2).
   - If an impact falls within 8.5m of our mech (t < 1.2s), executes an emergency radial boost away from the 7m blast circle, completely avoiding the 360 damage and 45-energy concussion drain.
2. Tactical Supply Drop Ring Capture:
   - Does not blindly rush the center pole if an enemy is contesting.
   - Stays within the 8m capture ring (at 4m-6m radius) while maintaining continuous fire, ensuring the 4-second capture clock completes for an instant 100% full heal, full energy, and +3 cubes.
3. Lightning Laser Interruption:
   - When any enemy within 44m begins charging a laser (charging: true), immediately arcs Lightning to short their laser, wiping out the charge and locking it offline for 2 seconds.
4. Dynamic Shrink Envelope (No Storm Overtake):
   - Computes safe radius relative to next_center during shrinks, preventing the trailing storm wall from catching the mech from behind.
5. Multi-Threat Funneling & Front Armor Defense:
   - In 2v1 engagements against medium hunters, kites so that both hostiles remain in front of the 180-degree torso shield, denying the 1.5x rear damage vulnerability.
6. Enhanced Mobility Upgrades:
   - Elevates Servos (+8% speed) and Jets (-25% energy drain) to sustain high-speed kiting without exhausting energy reserves.
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
    "plating": 96,   # +15% max HP (up to +45% HP)
    "armor": 92,     # -8% damage taken (up to -24%)
    "laser_charge": 88,
    "laser_damage": 85,
    "servos": 82,    # +8% movement speed (crucial for kiting)
    "jets": 80,      # -25% boost drain (sustained dodging)
    "gun_damage": 78,
    "gun_velocity": 74,
    "lightning_range": 68,
    "lightning_efficiency": 68,
    "sensors": 50,
    "gun_rate": 45,
    "gun_mag": 45,
    "mortar_reload": 40,
    "reactor": 35,
    "capacitor": 30,
}


class Bot:
    name = "AegisApex-v6"

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
        self.target_last_seen_t: float = -100.0
        self.target_last_pos: Optional[List[float]] = None
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

    def _calculate_projectile_dodge(self, me_pos: List[float], projectiles: List[dict], energy: float) -> Tuple[float, float, bool]:
        """Checks for incoming gun bullets & parabolic mortar shells on collision course."""
        mx, my, mz = me_pos[0], me_pos[1], me_pos[2]
        dodge_x, dodge_y = 0.0, 0.0
        urgent_boost = False

        for p in projectiles:
            p_kind = p.get("kind", "gun")
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]

            if p_kind == "mortar":
                # Parabolic mortar impact prediction: 0.5 * g * t^2 - vz * t + (mz - pz) = 0
                g = self.mortar_gravity
                a = 0.5 * g
                b = -vz
                c = mz - pz
                disc = b * b - 4.0 * a * c
                if disc >= 0:
                    sqrt_disc = math.sqrt(disc)
                    t1 = (-b - sqrt_disc) / (2.0 * a)
                    t2 = (-b + sqrt_disc) / (2.0 * a)
                    candidates = [t for t in (t1, t2) if t > 0.05]
                    if candidates:
                        t_land = min(candidates)
                        if t_land < 1.3:
                            imp_x = px + vx * t_land
                            imp_y = py + vy * t_land
                            dist_to_blast = math.hypot(imp_x - mx, imp_y - my)
                            if dist_to_blast < 8.5:
                                # Within 7m blast radius! Radial escape vector away from impact!
                                esc_dx = mx - imp_x
                                esc_dy = my - imp_y
                                esc_len = math.hypot(esc_dx, esc_dy)
                                if esc_len > 0.1:
                                    weight = (8.5 - dist_to_blast) / 8.5
                                    dodge_x += (esc_dx / esc_len) * weight * 3.5
                                    dodge_y += (esc_dy / esc_len) * weight * 3.5
                                    if energy > 15.0:
                                        urgent_boost = True

            else:
                # Straight-line bullet prediction
                v_sq = vx * vx + vy * vy + vz * vz
                if v_sq < 100.0:
                    continue

                dx = mx - px
                dy = my - py
                dz = (mz + 4.5) - pz

                t = (dx * vx + dy * vy + dz * vz) / v_sq
                if 0.0 < t < 0.65:
                    cx = px + vx * t
                    cy = py + vy * t
                    cz = pz + vz * t
                    dist_xy = math.hypot(cx - mx, cy - my)
                    dist_z = abs(cz - (mz + 4.5))

                    if dist_xy < 3.8 and dist_z < 5.5:
                        v_len = math.hypot(vx, vy)
                        if v_len > 1.0:
                            perp_x = -vy / v_len
                            perp_y = vx / v_len
                            dot = (mx - px) * perp_x + (my - py) * perp_y
                            if dot < 0:
                                perp_x = -perp_x
                                perp_y = -perp_y

                            weight = (3.8 - dist_xy) / 3.8
                            dodge_x += perp_x * weight * 3.0
                            dodge_y += perp_y * weight * 3.0

                            if t < 0.35 and energy > 18.0:
                                urgent_boost = True

        d_len = math.hypot(dodge_x, dodge_y)
        if d_len > 3.0:
            dodge_x = (dodge_x / d_len) * 3.0
            dodge_y = (dodge_y / d_len) * 3.0

        return dodge_x, dodge_y, urgent_boost

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

        # 1. Damage tracking
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

        # 3. Weapons loadout & smart dual-energy upgrading
        weapons = me.get("weapons", [None, None])
        w0 = weapons[0]
        w1 = weapons[1] if len(weapons) > 1 else None
        w0_kind = w0.get("kind") if w0 else None
        w1_kind = w1.get("kind") if w1 else None

        near_pickups = [
            p for p in obs.get("pickups", [])
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < self.pickup_radius
            and abs(p["pos"][2] - z) < 4.0
        ]

        if near_pickups:
            if w1 is None:
                p = near_pickups[0]
                if p.get("kind") != w0_kind:
                    act["take"] = p["id"]
            else:
                for p in near_pickups:
                    p_kind = p.get("kind")
                    # Out of combat upgrade slot 0 to complementary energy weapon
                    if w0_kind == "gun" and not self.is_charging_laser:
                        if (w1_kind == "laser" and p_kind == "lightning") or (w1_kind == "lightning" and p_kind == "laser"):
                            act["take"] = p["id"]
                            act["drop"] = 0
                            break

                    # Standard slot 1 upgrade to laser or lightning
                    if p_kind == "laser" and w1_kind != "laser" and not self.is_charging_laser:
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break
                    elif p_kind == "lightning" and w1_kind not in ("laser", "lightning"):
                        act["take"] = p["id"]
                        act["drop"] = 1
                        break

        # 4. Zone tracking
        zone_info = obs.get("zone", {})
        cur_center, cur_radius = self._get_current_zone(zone_info, t)
        next_center = zone_info.get("next_center", cur_center)
        next_radius = float(zone_info.get("next_radius", cur_radius))

        # 5. Threat perception & priority targeting
        muzzle_h = float(me.get("stats", {}).get("muzzle_height", 7.0))
        my_muzzle = [x, y, z + muzzle_h]
        my_eyes = [x, y, z + 8.0]

        all_enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]

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

        best_score = -1e9
        for enemy in all_enemies:
            epos = enemy["pos"]
            ecom = [epos[0], epos[1], epos[2] + 4.5]
            d = math.hypot(epos[0] - x, epos[1] - y)
            has_los = self.world.has_los(my_muzzle, ecom)
            e_hp = float(enemy.get("hp", 1400.0))

            score = 700.0 - d
            if has_los:
                score += 850.0

            # Close-range threat priority (< 25m)
            if d < 25.0:
                score += 1000.0 * (1.0 - (d / 25.0))

            if enemy.get("id") == self.target_id:
                score += 250.0
            if laser_slot is not None and "mortar" in enemy.get("weapons", []):
                score += 400.0  # Cook-off target
            if e_hp < 500.0:
                score += 550.0 * (1.0 - (e_hp / 500.0))

            if score > best_score:
                best_score = score
                primary_target = enemy
                target_com = ecom
                target_has_los = has_los
                target_dist = d

        if primary_target:
            self.target_id = primary_target.get("id")
            self.target_last_seen_t = t
            self.target_last_pos = primary_target["pos"]
        else:
            if (t - self.target_last_seen_t) > 4.0:
                self.target_id = None
                self.target_last_pos = None

        # 6. Combat Aiming & Trigger Control
        commanded_aim_yaw = me["aim"][0]
        commanded_aim_pitch = me["aim"][1]
        fire_slot0 = False
        fire_slot1 = False
        shooting_crate = False

        if primary_target and target_com:
            t_pos = primary_target["pos"]
            t_vel = primary_target.get("vel", [0.0, 0.0, 0.0])

            # Arm Gun Interception lead
            lead = ballistics.solve_interception(my_muzzle, self.gun_speed, target_com, t_vel)
            if lead is not None:
                lead_yaw, lead_pitch, _, pred_pt = lead
                commanded_aim_yaw = lead_yaw
                commanded_aim_pitch = lead_pitch

                target_angular_rad = max(math.radians(9.0), math.atan2(2.5, max(5.0, target_dist)))
                yaw_err = abs(ballistics.normalize_angle(me["aim"][0] - lead_yaw))

                if target_has_los and yaw_err < target_angular_rad and target_dist < self.gun_range:
                    if gun_slot == 0:
                        fire_slot0 = True
                    elif gun_slot == 1:
                        fire_slot1 = True

                mf.draw.line(my_muzzle, pred_pt, "#ff3322")

            # Lightning Arc: instant hitscan within 44m cone + Laser shorting!
            if lightning_slot is not None and target_dist < 44.0 and target_has_los:
                yaw_to_target = math.atan2(t_pos[1] - y, t_pos[0] - x)
                angle_diff = abs(ballistics.normalize_angle(me["aim"][0] - yaw_to_target))
                
                # Check if enemy is charging a laser: immediate short interrupt!
                is_charging = primary_target.get("charging", False)
                if (angle_diff < math.radians(28.0) or is_charging) and energy > 3.0:
                    if lightning_slot == 0:
                        fire_slot0 = True
                    else:
                        fire_slot1 = True

            # Laser: ONLY charge when target has Line of Sight!
            if laser_slot is not None:
                laser_w = weapons[laser_slot]
                current_charge = float(laser_w.get("charge", 0.0)) if laser_w else 0.0
                direct_yaw, direct_pitch = ballistics.solve_direct_aim(my_muzzle, target_com)
                yaw_err_direct = abs(ballistics.normalize_angle(me["aim"][0] - direct_yaw))

                if target_has_los and target_dist < self.laser_range:
                    if current_charge < self.laser_charge_time:
                        if laser_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
                        self.is_charging_laser = True
                    else:
                        if yaw_err_direct < math.radians(6.0):
                            if laser_slot == 0:
                                fire_slot0 = False
                            else:
                                fire_slot1 = False
                            self.is_charging_laser = False
                        else:
                            if laser_slot == 0:
                                fire_slot0 = True
                            else:
                                fire_slot1 = True
                else:
                    self.is_charging_laser = False

            # Mortar: Active fire direct and indirect
            if mortar_slot is not None and target_dist < 200.0:
                mortar_aim = ballistics.solve_mortar_aim(
                    [x, y, z + 9.5], target_com, self.mortar_speed, self.mortar_gravity
                )
                if mortar_aim:
                    m_yaw, m_pitch = mortar_aim
                    yaw_err_m = abs(ballistics.normalize_angle(me["aim"][0] - m_yaw))
                    if yaw_err_m < math.radians(10.0):
                        if mortar_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True

            # Tactical reload when target behind cover
            if not target_has_los and gun_slot is not None:
                gun_w = weapons[gun_slot]
                if gun_w and gun_w.get("ammo", 12) <= 6 and gun_w.get("reload", 0.0) == 0.0:
                    act["reload"] = True

        elif self.target_last_pos and (t - self.target_last_seen_t) < 3.0:
            tx, ty, tz = self.target_last_pos
            commanded_aim_yaw = math.atan2(ty - y, tx - x)
            commanded_aim_pitch = 0.0
            self.is_charging_laser = False

        else:
            self.is_charging_laser = False
            # Out of combat: Shoot crates with Laser (1-shot) or Gun
            near_crates = [
                c for c in obs.get("crates", [])
                if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < 28.0
                and self.world.has_los(my_muzzle, [c["pos"][0], c["pos"][1], c["pos"][2] + 1.0])
            ]
            if near_crates:
                c_pos = near_crates[0]["pos"]
                yaw_c, pitch_c = ballistics.solve_direct_aim(my_muzzle, [c_pos[0], c_pos[1], c_pos[2] + 1.0])
                commanded_aim_yaw = yaw_c
                commanded_aim_pitch = pitch_c
                if abs(ballistics.normalize_angle(me["aim"][0] - yaw_c)) < math.radians(8.0):
                    if laser_slot is not None:
                        if laser_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
                    elif gun_slot is not None:
                        if gun_slot == 0:
                            fire_slot0 = True
                        else:
                            fire_slot1 = True
                shooting_crate = True

            elif self.last_damage_bearing is not None and (t - self.last_damaged_t) < 3.0:
                commanded_aim_yaw = self.last_damage_bearing
            elif obs.get("sounds"):
                recent_sound = obs["sounds"][0]
                commanded_aim_yaw = recent_sound.get("bearing", commanded_aim_yaw)

            if not shooting_crate and gun_slot is not None:
                gun_w = weapons[gun_slot]
                if gun_w and gun_w.get("ammo", 12) <= 6 and gun_w.get("reload", 0.0) == 0.0:
                    act["reload"] = True

        # 7. Strategic Movement, Ring Capturing & Multi-Threat Funneling
        if tick % 45 == 0:
            self.strafe_direction *= -1.0

        goal_x, goal_y = next_center[0], next_center[1]
        request_boost = False

        # Supply Drop Combat Interception: Check if a ready drop is within 38m and uncontested
        ready_drop = None
        for d in obs.get("drops", []):
            if d.get("lands_in", 99.0) == 0.0:
                d_dist = math.hypot(d["pos"][0] - x, d["pos"][1] - y)
                if d_dist < 38.0 and math.hypot(d["pos"][0] - cur_center[0], d["pos"][1] - cur_center[1]) < (cur_radius - 10.0):
                    # Check if an enemy is already inside the 8m ring
                    enemy_in_ring = any(
                        math.hypot(e["pos"][0] - d["pos"][0], e["pos"][1] - d["pos"][1]) < 8.0
                        for e in all_enemies
                    )
                    if not enemy_in_ring or d_dist < 8.0:
                        ready_drop = d
                        break

        critical_retreat = (hp < 350.0 and len(all_enemies) > 0) or (len(all_enemies) >= 2 and target_dist < 30.0)

        if ready_drop and hp < 1150.0:
            # Tactical drop capture: if inside 8m ring, stay inside at radius 5m; else advance into ring
            d_pos = ready_drop["pos"]
            dist_to_center = math.hypot(d_pos[0] - x, d_pos[1] - y)
            if dist_to_center < 7.0:
                # Inside ring! Circle-strafe within the 8m perimeter while maintaining capture
                u_dx = (d_pos[0] - x) / (dist_to_center + 1e-4)
                u_dy = (d_pos[1] - y) / (dist_to_center + 1e-4)
                goal_x = x - u_dy * 8.0
                goal_y = y + u_dx * 8.0
            else:
                goal_x = d_pos[0]
                goal_y = d_pos[1]
                if energy > 20.0:
                    request_boost = True
            mf.draw.point([goal_x, goal_y, z], "#ffd700", 10)

        elif critical_retreat and primary_target:
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
                request_boost = (energy > 20.0)

        elif primary_target:
            t_pos = primary_target["pos"]
            d = target_dist

            if not target_has_los:
                # Flank corner to re-acquire LOS
                u_x = (t_pos[0] - x) / (d + 1e-4)
                u_y = (t_pos[1] - y) / (d + 1e-4)
                goal_x = x + u_x * 15.0
                goal_y = y + u_y * 15.0
            else:
                # ANTI-RUSH & MULTI-THREAT FUNNELING KITING
                u_x = (t_pos[0] - x) / (d + 1e-4)
                u_y = (t_pos[1] - y) / (d + 1e-4)
                perp_x = -u_y * self.strafe_direction
                perp_y = u_x * self.strafe_direction

                # Aggressive backpedal if enemy closes inside 26m!
                if d < 26.0:
                    rad_x = -u_x * 2.6
                    rad_y = -u_y * 2.6
                    if energy > 15.0:
                        request_boost = True
                elif d > 42.0:
                    rad_x = u_x * 1.0
                    rad_y = u_y * 1.0
                else:
                    rad_x = 0.0
                    rad_y = 0.0

                # Multi-threat repulsion: keep ALL visible enemies in front of our 180 front arc
                flank_push_x, flank_push_y = 0.0, 0.0
                for other in all_enemies:
                    if other.get("id") != primary_target.get("id"):
                        ox, oy = other["pos"][0], other["pos"][1]
                        od = math.hypot(ox - x, oy - y)
                        if od < 40.0:
                            flank_push_x -= ((ox - x) / (od + 1e-4)) * 1.5
                            flank_push_y -= ((oy - y) / (od + 1e-4)) * 1.5

                combat_vx = perp_x + rad_x + flank_push_x
                combat_vy = perp_y + rad_y + flank_push_y
                c_len = math.hypot(combat_vx, combat_vy)
                if c_len > 0.1:
                    goal_x = x + (combat_vx / c_len) * 20.0
                    goal_y = y + (combat_vy / c_len) * 20.0

                if energy > 50.0 and (tick % 10 < 4):
                    request_boost = True

        else:
            # OUT OF COMBAT: Weapon Scavenge -> Supply Drops -> Crates -> Cubes -> Zone Envelope
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

            if best_weapon_pickup and math.hypot(best_weapon_pickup["pos"][0] - x, best_weapon_pickup["pos"][1] - y) < 70.0:
                goal_x = best_weapon_pickup["pos"][0]
                goal_y = best_weapon_pickup["pos"][1]
                mf.draw.point([goal_x, goal_y, z], "#ff00ff", 8)
                request_boost = (energy > 25.0)

            elif any(d.get("lands_in", 99.0) == 0.0 for d in obs.get("drops", [])):
                any_ready_drop = next(
                    (d for d in obs.get("drops", [])
                     if d.get("lands_in", 99.0) == 0.0
                     and math.hypot(d["pos"][0] - cur_center[0], d["pos"][1] - cur_center[1]) < (cur_radius - 12.0)),
                    None
                )
                if any_ready_drop:
                    goal_x = any_ready_drop["pos"][0]
                    goal_y = any_ready_drop["pos"][1]
                    mf.draw.point([goal_x, goal_y, z], "#ffd700", 10)
                    request_boost = (energy > 25.0)

            else:
                near_crates = [
                    c for c in obs.get("crates", [])
                    if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < 35.0
                    and math.hypot(c["pos"][0] - cur_center[0], c["pos"][1] - cur_center[1]) < (cur_radius - 12.0)
                ]
                if near_crates:
                    goal_x = near_crates[0]["pos"][0]
                    goal_y = near_crates[0]["pos"][1]
                else:
                    cubes = obs.get("cubes", [])
                    safe_cubes = [
                        c for c in cubes
                        if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < 45.0
                        and math.hypot(c["pos"][0] - cur_center[0], c["pos"][1] - cur_center[1]) < (cur_radius - 12.0)
                    ]
                    if safe_cubes:
                        safe_cubes.sort(key=lambda c: math.hypot(c["pos"][0] - x, c["pos"][1] - y))
                        goal_x = safe_cubes[0]["pos"][0]
                        goal_y = safe_cubes[0]["pos"][1]
                        mf.draw.point([goal_x, goal_y, z], "#00e5ff", 6)
                    else:
                        # Dynamic Shrink Envelope: Position safely inside future safe circle
                        if cur_radius <= 160.0:
                            safe_target_r = max(15.0, next_radius - 18.0)
                            angle_to_me = math.atan2(y - next_center[1], x - next_center[0])
                            goal_x = next_center[0] + math.cos(angle_to_me) * safe_target_r
                            goal_y = next_center[1] + math.sin(angle_to_me) * safe_target_r
                        else:
                            goal_x = next_center[0]
                            goal_y = next_center[1]

        # 8. Vector Field Steering, Obstacle Avoidance & Projectile Dodging
        g_dx = goal_x - x
        g_dy = goal_y - y
        g_dist = math.hypot(g_dx, g_dy)
        if g_dist > 0.1:
            vx = g_dx / g_dist
            vy = g_dy / g_dist
        else:
            vx, vy = 0.0, 0.0

        # Building wall repulsion
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

        # ACTIVE PROJECTILE DODGING (Bullets & Mortar Impact Escape)
        proj_dodge_x, proj_dodge_y, proj_boost = self._calculate_projectile_dodge([x, y, z], obs.get("projectiles", []), energy)
        vx += proj_dodge_x
        vy += proj_dodge_y
        if proj_boost:
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
                        vx += (dodge_dx / d_len) * 2.8
                        vy += (dodge_dy / d_len) * 2.8
                        if energy > 18.0:
                            request_boost = True

        # Move vector normalization
        mv_len = math.hypot(vx, vy)
        if mv_len > 0.05:
            move_cmd = [vx / mv_len, vy / mv_len]
        else:
            move_cmd = [0.0, 0.0]

        # Jump handling over ledges/steps
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

        mf.draw.label(f"HP {hp:.0f}/{max_hp:.0f} · {'Combat' if primary_target else 'Searching'}")
        mf.draw.point([goal_x, goal_y, z], "#40c0ff", 6)

        return act


if __name__ == "__main__":
    mf.run(Bot)
