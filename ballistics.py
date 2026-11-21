"""Ballistics and aiming calculation for Gun, Laser, Mortar and Lightning."""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


def solve_interception(
    shooter_pos: List[float],
    proj_speed: float,
    target_pos: List[float],
    target_vel: List[float],
) -> Optional[Tuple[float, float, float, List[float]]]:
    """Computes (yaw, pitch, time_to_hit, predicted_pos) to hit a moving target with straight projectile."""
    sx, sy, sz = shooter_pos[0], shooter_pos[1], shooter_pos[2]
    tx, ty, tz = target_pos[0], target_pos[1], target_pos[2]
    vx, vy, vz = target_vel[0], target_vel[1], target_vel[2]

    dx = tx - sx
    dy = ty - sy
    dz = tz - sz

    # Quadratic equation: A*t^2 + B*t + C = 0
    # |P + V*t|^2 = (s*t)^2
    v_sq = vx * vx + vy * vy + vz * vz
    s_sq = proj_speed * proj_speed
    a = v_sq - s_sq
    b = 2.0 * (dx * vx + dy * vy + dz * vz)
    c = dx * dx + dy * dy + dz * dz

    if abs(a) < 1e-6:
        if abs(b) < 1e-6:
            return None
        t = -c / b
        if t <= 0:
            return None
    else:
        disc = b * b - 4.0 * a * c
        if disc < 0:
            return None
        sqrt_disc = math.sqrt(disc)
        t1 = (-b - sqrt_disc) / (2.0 * a)
        t2 = (-b + sqrt_disc) / (2.0 * a)
        candidates = [t for t in (t1, t2) if t > 0.0]
        if not candidates:
            return None
        t = min(candidates)

    pred_x = tx + vx * t
    pred_y = ty + vy * t
    pred_z = tz + vz * t

    pdx = pred_x - sx
    pdy = pred_y - sy
    pdz = pred_z - sz
    dist_xy = math.hypot(pdx, pdy)

    yaw = math.atan2(pdy, pdx)
    pitch = math.atan2(pdz, dist_xy)

    return yaw, pitch, t, [pred_x, pred_y, pred_z]


def solve_direct_aim(shooter_pos: List[float], target_pos: List[float]) -> Tuple[float, float]:
    """Instant hitscan aim: yaw and pitch directly at target."""
    dx = target_pos[0] - shooter_pos[0]
    dy = target_pos[1] - shooter_pos[1]
    dz = target_pos[2] - shooter_pos[2]
    dist_xy = math.hypot(dx, dy)
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(dz, dist_xy)
    return yaw, pitch


def solve_mortar_aim(
    shooter_pos: List[float],
    target_pos: List[float],
    speed: float = 72.0,
    gravity: float = 22.0,
    high_arc: bool = False,
) -> Optional[Tuple[float, float]]:
    """Ballistic mortar trajectory calculation."""
    sx, sy, sz = shooter_pos[0], shooter_pos[1], shooter_pos[2]
    tx, ty, tz = target_pos[0], target_pos[1], target_pos[2]

    dx = tx - sx
    dy = ty - sy
    dz = tz - sz
    d = math.hypot(dx, dy)
    if d < 1.0:
        return math.atan2(dy, dx), math.pi / 4

    v2 = speed * speed
    v4 = v2 * v2
    under_root = v4 - gravity * (gravity * d * d + 2.0 * dz * v2)
    if under_root < 0:
        # Out of range, aim at 45 degrees
        return math.atan2(dy, dx), math.pi / 4

    root = math.sqrt(under_root)
    # tan(theta) = (v^2 +- sqrt) / (g * d)
    term = (v2 + root) if high_arc else (v2 - root)
    tan_theta = term / (gravity * d)
    pitch = math.atan(tan_theta)
    yaw = math.atan2(dy, dx)
    return yaw, pitch


def normalize_angle(angle: float) -> float:
    """Wraps angle into [-pi, pi]."""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle
