"""World model: spatial indexing, collision geometry, line-of-sight and cover analysis."""
from __future__ import annotations

import array
import json
import math
import os
from typing import Dict, List, Optional, Set, Tuple

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]
Segment = Tuple[float, float, float, float, float, float]  # x1, y1, x2, y2, base, top


def line_intersect_2d(p1: Vec2, p2: Vec2, p3: Vec2, p4: Vec2) -> Optional[float]:
    """Returns parameter t along segment p1->p2 [0, 1] if p1->p2 intersects p3->p4, else None."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1)
    if denom == 0.0:
        return None
    ua = ((x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)) / denom
    ub = ((x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)) / denom
    if 0.0 <= ua <= 1.0 and 0.0 <= ub <= 1.0:
        return ua
    return None


class WorldModel:
    CELL_SIZE = 25.0

    def __init__(self, map_dir: str):
        self.map_dir = map_dir
        self.grid: Dict[Tuple[int, int], List[Segment]] = {}
        self.prisms: List[dict] = []
        self.terrain_arr: Optional[array.array] = None
        self.terrain_params = {
            "minX": -513.0,
            "minY": -334.0,
            "step": 1.0,
            "nx": 1070,
            "ny": 724,
        }
        self._load_world()
        self._load_terrain()

    def _load_world(self) -> None:
        world_path = os.path.join(self.map_dir, "v2", "world.json")
        if not os.path.exists(world_path):
            world_path = os.path.join(self.map_dir, "world.json")
        if not os.path.exists(world_path):
            return

        with open(world_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.prisms = data.get("prisms", [])
        cell_size = self.CELL_SIZE

        for prism in self.prisms:
            ring = prism.get("ring", [])
            base = float(prism.get("base", -100.0))
            top = float(prism.get("top", 100.0))
            n = len(ring) // 2
            if n < 3:
                continue

            for i in range(n):
                x1, y1 = float(ring[2 * i]), float(ring[2 * i + 1])
                next_i = (i + 1) % n
                x2, y2 = float(ring[2 * next_i]), float(ring[2 * next_i + 1])

                seg: Segment = (x1, y1, x2, y2, base, top)
                min_gx = int(math.floor(min(x1, x2) / cell_size))
                max_gx = int(math.floor(max(x1, x2) / cell_size))
                min_gy = int(math.floor(min(y1, y2) / cell_size))
                max_gy = int(math.floor(max(y1, y2) / cell_size))

                for gx in range(min_gx, max_gx + 1):
                    for gy in range(min_gy, max_gy + 1):
                        self.grid.setdefault((gx, gy), []).append(seg)

    def _load_terrain(self) -> None:
        terrain_path = os.path.join(self.map_dir, "v2", "terrain.i16")
        if not os.path.exists(terrain_path):
            terrain_path = os.path.join(self.map_dir, "terrain.i16")
        if not os.path.exists(terrain_path):
            return

        try:
            with open(terrain_path, "rb") as f:
                raw = f.read()
            arr = array.array("h")
            arr.frombytes(raw)
            self.terrain_arr = arr
        except Exception:
            self.terrain_arr = None

    def get_ground_height(self, x: float, y: float) -> float:
        if self.terrain_arr is None:
            return 0.0
        p = self.terrain_params
        fx = (x - p["minX"]) / p["step"]
        fy = (y - p["minY"]) / p["step"]
        ix = int(fx)
        iy = int(fy)
        nx, ny = p["nx"], p["ny"]
        if 0 <= ix < nx - 1 and 0 <= iy < ny - 1:
            rx = fx - ix
            ry = fy - iy
            idx = iy * nx + ix
            h00 = self.terrain_arr[idx]
            h10 = self.terrain_arr[idx + 1]
            h01 = self.terrain_arr[idx + nx]
            h11 = self.terrain_arr[idx + nx + 1]
            h = (h00 * (1 - rx) + h10 * rx) * (1 - ry) + (h01 * (1 - rx) + h11 * rx) * ry
            return h * 0.01
        return 0.0

    def has_los(self, p_start: List[float], p_end: List[float]) -> bool:
        """Checks if a straight line of sight between 3D points p_start and p_end is unblocked by prisms."""
        x1, y1, z1 = p_start[0], p_start[1], p_start[2]
        x2, y2, z2 = p_end[0], p_end[1], p_end[2]

        cell_size = self.CELL_SIZE
        min_gx = int(math.floor(min(x1, x2) / cell_size))
        max_gx = int(math.floor(max(x1, x2) / cell_size))
        min_gy = int(math.floor(min(y1, y2) / cell_size))
        max_gy = int(math.floor(max(y1, y2) / cell_size))

        checked: Set[int] = set()
        p_line_start = (x1, y1)
        p_line_end = (x2, y2)

        for gx in range(min_gx, max_gx + 1):
            for gy in range(min_gy, max_gy + 1):
                segs = self.grid.get((gx, gy))
                if not segs:
                    continue
                for seg in segs:
                    seg_id = id(seg)
                    if seg_id in checked:
                        continue
                    checked.add(seg_id)
                    sx1, sy1, sx2, sy2, base, top = seg
                    t = line_intersect_2d(p_line_start, p_line_end, (sx1, sy1), (sx2, sy2))
                    if t is not None:
                        z_hit = z1 + t * (z2 - z1)
                        if base <= z_hit <= top:
                            return False
        return True

    def get_wall_repulsion(self, x: float, y: float, z: float, check_radius: float = 6.0) -> Tuple[float, float]:
        """Calculates 2D repulsive vector away from nearby solid building walls."""
        cell_size = self.CELL_SIZE
        gx = int(math.floor(x / cell_size))
        gy = int(math.floor(y / cell_size))
        rep_x, rep_y = 0.0, 0.0
        checked: Set[int] = set()

        for cgx in range(gx - 1, gx + 2):
            for cgy in range(gy - 1, gy + 2):
                segs = self.grid.get((cgx, cgy))
                if not segs:
                    continue
                for seg in segs:
                    seg_id = id(seg)
                    if seg_id in checked:
                        continue
                    checked.add(seg_id)
                    sx1, sy1, sx2, sy2, base, top = seg
                    if not (base <= z + 1.5 and z <= top):
                        continue

                    dx = sx2 - sx1
                    dy = sy2 - sy1
                    l2 = dx * dx + dy * dy
                    if l2 == 0:
                        continue
                    t = max(0.0, min(1.0, ((x - sx1) * dx + (y - sy1) * dy) / l2))
                    px = sx1 + t * dx
                    py = sy1 + t * dy
                    dist = math.hypot(x - px, y - py)
                    if dist < check_radius:
                        weight = (check_radius - dist) / check_radius
                        if dist > 0.05:
                            rep_x += ((x - px) / dist) * weight
                            rep_y += ((y - py) / dist) * weight
                        else:
                            rep_x += 1.0
                            rep_y += 0.5
        return rep_x, rep_y

    def find_cover(
        self,
        self_pos: List[float],
        threat_pos: List[float],
        zone_center: List[float],
        zone_radius: float,
        search_radius: float = 20.0,
    ) -> Optional[Vec2]:
        """Finds a candidate 2D position within search_radius that breaks line-of-sight from threat_pos."""
        sx, sy, sz = self_pos[0], self_pos[1], self_pos[2]
        tx, ty, tz = threat_pos[0], threat_pos[1], threat_pos[2]

        best_pt: Optional[Vec2] = None
        best_score = float("inf")

        # Test candidate positions in radial pattern
        angles = [i * (math.pi / 6) for i in range(12)]
        distances = [6.0, 12.0, 18.0]

        threat_eye = [tx, ty, tz + 4.5]

        for d in distances:
            for ang in angles:
                cx = sx + math.cos(ang) * d
                cy = sy + math.sin(ang) * d

                # Must be inside safe zone
                if math.hypot(cx - zone_center[0], cy - zone_center[1]) > zone_radius - 5.0:
                    continue

                # Ground clearance check: not inside wall
                cz = self.get_ground_height(cx, cy)
                rx, ry = self.get_wall_repulsion(cx, cy, cz, check_radius=3.0)
                if math.hypot(rx, ry) > 2.0:
                    continue

                candidate_eye = [cx, cy, cz + 4.5]
                # If threat cannot see this candidate point:
                if not self.has_los(threat_eye, candidate_eye):
                    # Score based on distance from current pos and distance to zone center
                    dist_self = d
                    dist_zone = math.hypot(cx - zone_center[0], cy - zone_center[1])
                    score = dist_self + dist_zone * 0.3
                    if score < best_score:
                        best_score = score
                        best_pt = (cx, cy)

        return best_pt
