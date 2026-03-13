"""
route_finder.py
Full Python translation of route_finder.dart + multi_jeepney_route_finder.dart
- With the aid of claude code for translating the main algorithm
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set

# ---------------------------------------------------------------------------
# Type alias: coordinates are always (lat, lng) tuples
# ---------------------------------------------------------------------------
LatLng = Tuple[float, float]


# ---------------------------------------------------------------------------
# Fast geometry helpers  (replaces geopy.geodesic — ~50-100x faster)
# ---------------------------------------------------------------------------

def haversine_distance(p1: LatLng, p2: LatLng) -> float:
    """Haversine distance in metres. Port of the Dart Distance() call."""
    R = 6_371_000.0
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_bearing(p1: LatLng, p2: LatLng) -> float:
    """Forward bearing in degrees [0, 360). Port of the Dart bearing utility."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    d_lon = lon2 - lon1
    y = math.sin(d_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def is_forward(route_bearing: float, target_bearing: float, tolerance: float = 60.0) -> bool:
    """True when the route is heading toward the target. Port of Dart isForward."""
    diff = (route_bearing - target_bearing + 540) % 360 - 180
    return abs(diff) <= tolerance


# ---------------------------------------------------------------------------
# Data Models  (route_loader equivalent)
# ---------------------------------------------------------------------------

@dataclass
class JeepneyRoute:
    route_number: str
    direction: str
    coordinates: List[LatLng]


# ---------------------------------------------------------------------------
# Models from route_finder.dart
# ---------------------------------------------------------------------------

@dataclass
class DirectionStep:
    step_number: int
    instruction: str
    distance_m: float
    duration_s: float
    street: str
    type: int

    @property
    def formatted_distance(self) -> str:
        if self.distance_m < 1000:
            return f"{self.distance_m:.0f} m"
        return f"{self.distance_m / 1000:.1f} km"

    @property
    def formatted_duration(self) -> str:
        if self.duration_s < 60:
            return f"{self.duration_s:.0f} sec"
        minutes = self.duration_s / 60
        if minutes < 60:
            return f"{minutes:.0f} min"
        hours = minutes // 60
        remaining = minutes % 60
        return f"{hours:.0f} hr {remaining:.0f} min"


@dataclass
class WalkingSegment:
    title: str
    total_distance_m: float
    total_duration_s: float
    steps: List[DirectionStep]
    coordinates: List[LatLng]

    @property
    def formatted_distance(self) -> str:
        if self.total_distance_m < 1000:
            return f"{self.total_distance_m:.0f} m"
        return f"{self.total_distance_m / 1000:.1f} km"

    @property
    def formatted_duration(self) -> str:
        minutes = self.total_duration_s / 60
        return f"{minutes:.0f} min"


@dataclass
class JeepneySegmentInfo:
    route_number: str
    direction: str
    distance_m: float
    duration_s: float
    board_instruction: str
    alight_instruction: str
    coordinates: List[LatLng]
    board_idx: int
    alight_idx: int

    @property
    def formatted_distance(self) -> str:
        return f"{self.distance_m / 1000:.1f} km"

    @property
    def formatted_duration(self) -> str:
        minutes = self.duration_s / 60
        return f"~{minutes:.0f} min"


@dataclass
class DestinationCandidate:
    point: LatLng
    index: int
    distance: float
    type: str  # 'node' or 'segment'


@dataclass
class BoardingCandidate:
    point: LatLng
    index: int
    actual_distance: float
    effective_distance: float
    type: str  # 'node' or 'segment'


@dataclass
class RouteEvaluationMeta:
    board_point: LatLng
    board_idx: int
    board_dist_m: float
    alight_point: LatLng
    alight_idx: int
    alight_dist_m: float
    jeepney_segment: List[LatLng]
    jeepney_dist_m: float
    score: float
    ref_board_idx: int
    optimized: bool
    direction_adjusted: bool
    dest_candidates_evaluated: int


@dataclass
class EnhancedRouteResult:
    success: bool
    message: str
    total_distance_m: float
    total_duration_s: float
    walk_to_boarding: Optional[WalkingSegment]
    jeepney_ride: JeepneySegmentInfo
    walk_to_destination: Optional[WalkingSegment]
    start_marker: LatLng
    dest_marker: LatLng

    @property
    def formatted_total_distance(self) -> str:
        return f"{self.total_distance_m / 1000:.1f} km"

    @property
    def formatted_total_duration(self) -> str:
        minutes = self.total_duration_s / 60
        return f"{minutes:.0f} min"


# ---------------------------------------------------------------------------
# Models from multi_jeepney_route_finder.dart
# ---------------------------------------------------------------------------

@dataclass
class TransferSpot:
    name: str
    location: LatLng
    routes: List[str]
    priority: str

    @staticmethod
    def from_json(data: dict) -> "TransferSpot":
        return TransferSpot(
            name=data["name"],
            location=(float(data["latitude"]), float(data["longitude"])),
            routes=[str(r) for r in data["routes"]],
            priority=data["priority"],
        )


@dataclass
class MultiRouteSegment:
    route: JeepneyRoute
    meta: RouteEvaluationMeta
    segment_order: int


@dataclass
class TransferConnection:
    transfer_spot: TransferSpot
    from_segment: MultiRouteSegment
    from_alight_point: LatLng
    to_board_point: LatLng
    walk_distance: float


@dataclass
class MultiJeepneyRouteResult:
    segments: List[MultiRouteSegment]
    transfers: List[TransferConnection]
    total_score: float
    total_distance: float
    total_duration: float

    @property
    def number_of_transfers(self) -> int:
        return len(self.transfers)

    @property
    def route_summary(self) -> str:
        return " → ".join(s.route.route_number for s in self.segments)


@dataclass
class _PartialPath:
    segments: List[MultiRouteSegment]
    transfers: List[TransferConnection]
    current_location: LatLng
    accumulated_score: float
    accumulated_distance: float
    used_routes: Set[str]

    @staticmethod
    def initial(start: LatLng) -> "_PartialPath":
        return _PartialPath(
            segments=[],
            transfers=[],
            current_location=start,
            accumulated_score=0.0,
            accumulated_distance=0.0,
            used_routes=set(),
        )


# ---------------------------------------------------------------------------
# EnhancedRouteFinder  (route_finder.dart)
# ---------------------------------------------------------------------------

class EnhancedRouteFinder:

    @staticmethod
    def _dist(p1: LatLng, p2: LatLng) -> float:
        return haversine_distance(p1, p2)

    # ------------------------------------------------------------------
    # Point-to-segment closest point & distance
    # ------------------------------------------------------------------
    def _point_to_segment_distance(
        self, point: LatLng, seg_start: LatLng, seg_end: LatLng
    ) -> Tuple[float, LatLng]:
        """Returns (distance_m, closest_point_on_segment)."""
        px, py = point[1], point[0]          # lng, lat
        x1, y1 = seg_start[1], seg_start[0]
        x2, y2 = seg_end[1], seg_end[0]

        dx, dy = x2 - x1, y2 - y1

        if dx == 0 and dy == 0:
            return self._dist(point, seg_start), seg_start

        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))

        closest: LatLng = (y1 + t * dy, x1 + t * dx)
        return self._dist(point, closest), closest

    # ------------------------------------------------------------------
    # Find all destination candidates within maxAlightDistance
    # ------------------------------------------------------------------
    def _find_all_nearby_destination_candidates(
        self,
        route_coords: List[LatLng],
        dest: LatLng,
        max_alight_distance: float,
    ) -> List[DestinationCandidate]:
        candidates: List[DestinationCandidate] = []

        # Nodes
        for i, coord in enumerate(route_coords):
            d = self._dist(coord, dest)
            if d <= max_alight_distance:
                candidates.append(DestinationCandidate(point=coord, index=i, distance=d, type="node"))

        # Interpolated segment points
        for i in range(len(route_coords) - 1):
            d, closest = self._point_to_segment_distance(dest, route_coords[i], route_coords[i + 1])
            if d <= max_alight_distance:
                candidates.append(DestinationCandidate(point=closest, index=i, distance=d, type="segment"))

        candidates.sort(key=lambda c: c.distance)
        return candidates

    # ------------------------------------------------------------------
    # Direction penalty score
    # ------------------------------------------------------------------
    def _calculate_direction_score(
        self,
        route_coords: List[LatLng],
        board_idx: int,
        dest_idx: int,
        dest_point: LatLng,
    ) -> float:
        if board_idx >= len(route_coords) - 1:
            return 0.0

        segments_to_check = min(10, dest_idx - board_idx)
        if segments_to_check < 2:
            return 0.0

        initial_dist = self._dist(route_coords[board_idx], dest_point)
        check_idx = min(board_idx + segments_to_check, len(route_coords) - 1)
        later_dist = self._dist(route_coords[check_idx], dest_point)

        if later_dist > initial_dist:
            return (later_dist - initial_dist) * 2.0
        return 0.0

    # ------------------------------------------------------------------
    # Find best boarding point before destination index
    # ------------------------------------------------------------------
    def _find_best_boarding_point_before_destination(
        self,
        route_coords: List[LatLng],
        start: LatLng,
        dest_idx: int,
        dest_point: LatLng,
        max_board_distance: float,
        consider_direction: bool,
    ) -> Optional[BoardingCandidate]:
        candidates: List[BoardingCandidate] = []

        # Check all nodes before destination
        for i in range(dest_idx):
            coord = route_coords[i]
            d = self._dist(start, coord)
            if d <= max_board_distance:
                penalty = (
                    self._calculate_direction_score(route_coords, i, dest_idx, dest_point)
                    if consider_direction else 0.0
                )
                candidates.append(BoardingCandidate(
                    point=coord, index=i,
                    actual_distance=d,
                    effective_distance=d + penalty,
                    type="node",
                ))

        # Check all segments before destination
        for i in range(dest_idx):
            if i + 1 >= len(route_coords):
                continue
            d, closest = self._point_to_segment_distance(start, route_coords[i], route_coords[i + 1])
            if d <= max_board_distance:
                penalty = (
                    self._calculate_direction_score(route_coords, i, dest_idx, dest_point)
                    if consider_direction else 0.0
                )
                candidates.append(BoardingCandidate(
                    point=closest, index=i,
                    actual_distance=d,
                    effective_distance=d + penalty,
                    type="segment",
                ))

        if not candidates:
            return None

        candidates.sort(key=lambda c: c.effective_distance)
        return candidates[0]

    # ------------------------------------------------------------------
    # Nearest node on route
    # ------------------------------------------------------------------
    def _nearest_point_on_route(
        self, route_coords: List[LatLng], point: LatLng
    ) -> Tuple[LatLng, int, float]:
        """Returns (closest_point, index, distance)."""
        min_dist = float("inf")
        min_idx = 0
        min_pt = route_coords[0]

        for i, coord in enumerate(route_coords):
            d = self._dist(coord, point)
            if d < min_dist:
                min_dist = d
                min_idx = i
                min_pt = coord

        return min_pt, min_idx, min_dist

    # ------------------------------------------------------------------
    # Path distance helper
    # ------------------------------------------------------------------
    def _calculate_path_distance(self, coords: List[LatLng]) -> float:
        if len(coords) < 2:
            return 0.0
        return sum(self._dist(coords[k], coords[k + 1]) for k in range(len(coords) - 1))

    # ------------------------------------------------------------------
    # Main evaluation (with loop/interpolation support)
    # ------------------------------------------------------------------
    def evaluate_route(
        self,
        route_coords: List[LatLng],
        start: LatLng,
        dest: LatLng,
        walk_board_weight: float = 1.5,
        walk_alight_weight: float = 3.0,
        jeepney_distance_weight: float = 0.5,
        alight_priority_factor: float = 5.0,
        max_board_distance: float = 800.0,
        max_alight_distance: float = 500.0,
    ) -> Optional[RouteEvaluationMeta]:

        dest_candidates = self._find_all_nearby_destination_candidates(
            route_coords, dest, max_alight_distance
        )
        if not dest_candidates:
            return None

        best_solution: Optional[RouteEvaluationMeta] = None
        best_score = float("inf")

        ref_pt, ref_board_idx, _ = self._nearest_point_on_route(route_coords, start)

        for dest_candidate in dest_candidates:
            board_candidate = self._find_best_boarding_point_before_destination(
                route_coords, start,
                dest_candidate.index, dest_candidate.point,
                max_board_distance, True,
            )
            if board_candidate is None:
                continue

            # Build jeepney segment
            jeepney_segment: List[LatLng] = (
                [board_candidate.point]
                + route_coords[board_candidate.index + 1: dest_candidate.index + 1]
                + [dest_candidate.point]
            )
            jeepney_dist = self._calculate_path_distance(jeepney_segment)

            score = (
                board_candidate.actual_distance * walk_board_weight
                + dest_candidate.distance * walk_alight_weight
                + jeepney_dist * jeepney_distance_weight
                + dest_candidate.distance * alight_priority_factor
            )

            if score < best_score:
                best_score = score

                _, closest_board_idx, closest_board_dist = self._nearest_point_on_route(
                    route_coords[: dest_candidate.index], start
                )
                direction_adjusted = (
                    board_candidate.index != closest_board_idx
                    and board_candidate.actual_distance > closest_board_dist + 10
                )

                best_solution = RouteEvaluationMeta(
                    board_point=board_candidate.point,
                    board_idx=board_candidate.index,
                    board_dist_m=board_candidate.actual_distance,
                    alight_point=dest_candidate.point,
                    alight_idx=dest_candidate.index,
                    alight_dist_m=dest_candidate.distance,
                    jeepney_segment=jeepney_segment,
                    jeepney_dist_m=jeepney_dist,
                    score=score,
                    ref_board_idx=ref_board_idx,
                    optimized=(
                        board_candidate.index != ref_board_idx
                        or board_candidate.point != ref_pt
                    ),
                    direction_adjusted=direction_adjusted,
                    dest_candidates_evaluated=len(dest_candidates),
                )

        return best_solution

    # ------------------------------------------------------------------
    # Find best route across all routes
    # ------------------------------------------------------------------
    def find_best_route(
        self,
        routes: List[JeepneyRoute],
        start: LatLng,
        dest: LatLng,
        max_board_distance: float = 800.0,
        max_alight_distance: float = 500.0,
        debug: bool = False,
    ) -> Tuple[Optional[JeepneyRoute], Optional[RouteEvaluationMeta]]:

        best_route: Optional[JeepneyRoute] = None
        best_meta: Optional[RouteEvaluationMeta] = None
        best_score = float("inf")

        if debug:
            print(f"\n🔍 Evaluating {len(routes)} jeepney routes with loop support...\n")

        for route in routes:
            if debug:
                print(f"\n🔹 Route {route.route_number} ({route.direction})")
                print(f"     Route has {len(route.coordinates)} nodes")

            meta = self.evaluate_route(
                route.coordinates, start, dest,
                max_board_distance=max_board_distance,
                max_alight_distance=max_alight_distance,
            )

            if meta is None:
                if debug:
                    dest_candidates = self._find_all_nearby_destination_candidates(
                        route.coordinates, dest, max_alight_distance
                    )
                    if not dest_candidates:
                        print(f"  ❌ Route {route.route_number}: No destinations within {max_alight_distance}m")
                    else:
                        print(f"  ❌ Route {route.route_number}: Found {len(dest_candidates)} dest candidates but no valid boarding points")
                continue

            if debug:
                opt_marker = "🎯 OPTIMIZED" if meta.optimized else "○ Direct"
                dir_msg = " 🧭 Direction-corrected" if meta.direction_adjusted else ""
                print(
                    f"  ✅ Route {route.route_number} | Score={meta.score:.1f} | "
                    f"{opt_marker} [{meta.dest_candidates_evaluated} dest candidates checked]{dir_msg}"
                )
                if meta.optimized:
                    print(f"     Reference board idx={meta.ref_board_idx} → Optimized to idx={meta.board_idx}")
                print(f"     Board idx={meta.board_idx} dist={meta.board_dist_m:.1f}m")
                print(f"     Alight idx={meta.alight_idx} dist={meta.alight_dist_m:.1f}m")
                print(f"     Jeepney ride distance={meta.jeepney_dist_m:.1f}m")

            if meta.score < best_score:
                best_score = meta.score
                best_route = route
                best_meta = meta
                if debug:
                    print("     ⭐ New best route!")

        if best_route is None:
            if debug:
                print("\n❌ No suitable route found.")
            return None, None

        if debug:
            print(f"\n🎯 BEST ROUTE: {best_route.route_number} ({best_route.direction})")
            print(f"   Boarding optimization: {'USED' if best_meta.optimized else 'Not needed'}")
            if best_meta.optimized:
                print(f"   → Improved from ref idx {best_meta.ref_board_idx} to idx {best_meta.board_idx}")
            print(f"   Destination candidates evaluated: {best_meta.dest_candidates_evaluated}")
            print(f"   Board idx: {best_meta.board_idx} | Alight idx: {best_meta.alight_idx}")
            print(f"   Board dist: {best_meta.board_dist_m:.1f}m | Alight dist: {best_meta.alight_dist_m:.1f}m")
            print(f"   Jeepney dist: {best_meta.jeepney_dist_m:.1f}m | Score: {best_meta.score:.1f}")

        return best_route, best_meta

    # ------------------------------------------------------------------
    # Boarding zone polygon
    # ------------------------------------------------------------------
    def create_boarding_zone_polygon(
        self,
        route_coords: List[LatLng],
        center_idx: int,
        nodes_before: int = 2,
        nodes_after: int = 2,
        buffer_width: float = 30.0,
    ) -> Optional[List[LatLng]]:
        start_idx = max(0, center_idx - nodes_before)
        end_idx = min(len(route_coords) - 1, center_idx + nodes_after)
        segment = route_coords[start_idx: end_idx + 1]

        if len(segment) < 2:
            return None

        left_side: List[LatLng] = []
        right_side: List[LatLng] = []
        meters_to_deg = buffer_width / 111000

        for i, pt in enumerate(segment):
            lat, lon = pt

            if i == 0:
                nxt = segment[i + 1]
                angle = math.atan2(nxt[0] - lat, nxt[1] - lon)
            elif i == len(segment) - 1:
                prv = segment[i - 1]
                angle = math.atan2(lat - prv[0], lon - prv[1])
            else:
                prv, nxt = segment[i - 1], segment[i + 1]
                angle = math.atan2(nxt[0] - prv[0], nxt[1] - prv[1])

            perp_angle = angle + math.pi / 2
            offset_lon = meters_to_deg * math.cos(perp_angle) / math.cos(math.radians(lat))
            offset_lat = meters_to_deg * math.sin(perp_angle)

            left_side.append((lat + offset_lat, lon + offset_lon))
            right_side.append((lat - offset_lat, lon - offset_lon))

        return left_side + list(reversed(right_side))


# ---------------------------------------------------------------------------
# MultiJeepneyRouteFinder  (multi_jeepney_route_finder.dart)
# ---------------------------------------------------------------------------

class MultiJeepneyRouteFinder:

    MAX_TRANSFERS = 2
    MAX_TOTAL_DURATION_MINUTES = 120.0
    MAX_TOTAL_WALK_DISTANCE = 1000.0
    PRUNING_THRESHOLD_MULTIPLIER = 1.5

    WALK_SPEED = 1.4    # m/s
    JEEPNEY_SPEED = 5.56  # m/s (~20 km/h)

    def __init__(self, transfer_spots: Optional[List[TransferSpot]] = None):
        self._transfer_spots: List[TransferSpot] = transfer_spots or []
        self._single_route_finder = EnhancedRouteFinder()

    def load_transfer_spots(self, path: str) -> None:
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._transfer_spots = [TransferSpot.from_json(d) for d in data]
            print(f"✅ Loaded {len(self._transfer_spots)} transfer spots")
        except Exception as e:
            print(f"❌ Error loading transfer spots: {e}")
            self._transfer_spots = []

    # ------------------------------------------------------------------
    def _dist(self, p1: LatLng, p2: LatLng) -> float:
        return haversine_distance(p1, p2)

    def _find_routes_near_location(
        self, all_routes: List[JeepneyRoute], location: LatLng, max_distance: float
    ) -> List[JeepneyRoute]:
        nearby = []
        for route in all_routes:
            for coord in route.coordinates:
                if self._dist(coord, location) <= max_distance:
                    nearby.append(route)
                    break
        return nearby

    def _find_transfer_spots_for_route(
        self, route: JeepneyRoute, max_distance_from_route: float
    ) -> List[TransferSpot]:
        accessible = []
        for spot in self._transfer_spots:
            if route.route_number not in spot.routes:
                continue
            for coord in route.coordinates:
                if self._dist(coord, spot.location) <= max_distance_from_route:
                    accessible.append(spot)
                    break
        return accessible

    def _dist_to_destination(self, current: LatLng, dest: LatLng) -> float:
        return self._dist(current, dest)

    # ------------------------------------------------------------------
    def _estimate_total_duration(
        self,
        path: _PartialPath,
        final_meta: RouteEvaluationMeta,
        final_transfer_walk: float,
    ) -> float:
        duration = 0.0
        for seg in path.segments:
            duration += seg.meta.board_dist_m / self.WALK_SPEED
            duration += seg.meta.jeepney_dist_m / self.JEEPNEY_SPEED
            duration += seg.meta.alight_dist_m / self.WALK_SPEED
        for transfer in path.transfers:
            duration += transfer.walk_distance / self.WALK_SPEED
        duration += final_meta.board_dist_m / self.WALK_SPEED
        duration += final_meta.jeepney_dist_m / self.JEEPNEY_SPEED
        duration += final_meta.alight_dist_m / self.WALK_SPEED
        duration += final_transfer_walk / self.WALK_SPEED
        return duration

    # ------------------------------------------------------------------
    def _find_routes_recursive(
        self,
        all_routes: List[JeepneyRoute],
        current_path: _PartialPath,
        destination: LatLng,
        transfers_remaining: int,
        current_best_score: float,
        max_board_distance: float,
        max_alight_distance: float,
        max_transfer_walk_distance: float,
        transfer_penalty: float,
        transfer_walk_weight: float,
        debug: bool,
    ) -> List[MultiJeepneyRouteResult]:
        results: List[MultiJeepneyRouteResult] = []

        # PRUNING 1: score already too high
        if current_path.accumulated_score > current_best_score * self.PRUNING_THRESHOLD_MULTIPLIER:
            return results

        # PRUNING 2: total walking exceeds limit
        total_walking = sum(t.walk_distance for t in current_path.transfers)
        if total_walking > self.MAX_TOTAL_WALK_DISTANCE:
            return results

        # PRUNING 3: geographical progress check
        current_dist_to_dest = self._dist_to_destination(current_path.current_location, destination)

        is_top_level = len(current_path.segments) == 0

        # ---- Base case: try direct route to destination ----
        direct_candidates = self._find_routes_near_location(
            all_routes, destination, max_alight_distance
        )

        for route in direct_candidates:
            if route.route_number in current_path.used_routes:
                continue

            board_dist = (
                max_board_distance if not current_path.segments
                else max_transfer_walk_distance
            )

            meta = self._single_route_finder.evaluate_route(
                route.coordinates,
                current_path.current_location,
                destination,
                max_board_distance=board_dist,
                max_alight_distance=max_alight_distance,
            )
            if meta is None:
                continue

            final_score = current_path.accumulated_score + meta.score
            final_distance = (
                current_path.accumulated_distance
                + meta.board_dist_m
                + meta.jeepney_dist_m
                + meta.alight_dist_m
            )
            final_duration = self._estimate_total_duration(current_path, meta, 0)

            # PRUNING 4: max duration
            if final_duration > self.MAX_TOTAL_DURATION_MINUTES * 60:
                continue

            segments = current_path.segments + [
                MultiRouteSegment(route=route, meta=meta,
                                  segment_order=len(current_path.segments) + 1)
            ]

            results.append(MultiJeepneyRouteResult(
                segments=segments,
                transfers=list(current_path.transfers),
                total_score=final_score,
                total_distance=final_distance,
                total_duration=final_duration,
            ))

            if debug and is_top_level:
                route_str = " → ".join(s.route.route_number for s in segments)
                print(f"   ✅ Found path: {route_str} ({final_duration / 60:.0f}min)")

        # ---- Recursive case: try adding a transfer ----
        if transfers_remaining > 0:
            for transfer_spot in self._transfer_spots:
                candidate_routes = [
                    r for r in all_routes
                    if r.route_number not in current_path.used_routes
                    and r.route_number in transfer_spot.routes
                ]
                if not candidate_routes:
                    continue

                for route in candidate_routes:
                    board_dist = (
                        max_board_distance if not current_path.segments
                        else max_transfer_walk_distance
                    )

                    meta = self._single_route_finder.evaluate_route(
                        route.coordinates,
                        current_path.current_location,
                        transfer_spot.location,
                        max_board_distance=board_dist,
                        max_alight_distance=max_transfer_walk_distance,
                    )
                    if meta is None:
                        continue

                    transfer_walk_dist = self._dist(meta.alight_point, transfer_spot.location)
                    if transfer_walk_dist > max_transfer_walk_distance:
                        continue

                    new_score = (
                        current_path.accumulated_score
                        + meta.score
                        + transfer_walk_dist * transfer_walk_weight
                        + transfer_penalty
                    )
                    new_distance = (
                        current_path.accumulated_distance
                        + meta.board_dist_m
                        + meta.jeepney_dist_m
                        + meta.alight_dist_m
                        + transfer_walk_dist
                    )

                    # PRUNING 5: must be making progress toward destination
                    new_dist_to_dest = self._dist_to_destination(transfer_spot.location, destination)
                    if new_dist_to_dest >= current_dist_to_dest * 1.3:
                        continue

                    new_segment = MultiRouteSegment(
                        route=route, meta=meta,
                        segment_order=len(current_path.segments) + 1,
                    )
                    new_transfer = TransferConnection(
                        transfer_spot=transfer_spot,
                        from_segment=new_segment,
                        from_alight_point=meta.alight_point,
                        to_board_point=transfer_spot.location,
                        walk_distance=transfer_walk_dist,
                    )
                    new_path = _PartialPath(
                        segments=current_path.segments + [new_segment],
                        transfers=current_path.transfers + [new_transfer],
                        current_location=transfer_spot.location,
                        accumulated_score=new_score,
                        accumulated_distance=new_distance,
                        used_routes=current_path.used_routes | {route.route_number},
                    )

                    if debug and is_top_level:
                        print(f"   🔄 Trying: {route.route_number} → {transfer_spot.name}")

                    sub_results = self._find_routes_recursive(
                        all_routes=all_routes,
                        current_path=new_path,
                        destination=destination,
                        transfers_remaining=transfers_remaining - 1,
                        current_best_score=current_best_score,
                        max_board_distance=max_board_distance,
                        max_alight_distance=max_alight_distance,
                        max_transfer_walk_distance=max_transfer_walk_distance,
                        transfer_penalty=transfer_penalty,
                        transfer_walk_weight=transfer_walk_weight,
                        debug=debug,
                    )
                    results.extend(sub_results)

        return results

    # ------------------------------------------------------------------
    def find_best_multi_route(
        self,
        all_routes: List[JeepneyRoute],
        start: LatLng,
        dest: LatLng,
        max_board_distance: float = 800.0,
        max_alight_distance: float = 500.0,
        max_transfer_walk_distance: float = 300.0,
        transfer_penalty: float = 500.0,
        transfer_walk_weight: float = 4.0,
        debug: bool = False,
    ) -> Optional[MultiJeepneyRouteResult]:

        if debug:
            print("🔄 Searching for multi-jeepney routes...")

        initial_path = _PartialPath.initial(start)

        all_results = self._find_routes_recursive(
            all_routes=all_routes,
            current_path=initial_path,
            destination=dest,
            transfers_remaining=self.MAX_TRANSFERS,
            current_best_score=float("inf"),
            max_board_distance=max_board_distance,
            max_alight_distance=max_alight_distance,
            max_transfer_walk_distance=max_transfer_walk_distance,
            transfer_penalty=transfer_penalty,
            transfer_walk_weight=transfer_walk_weight,
            debug=debug,
        )

        if not all_results:
            if debug:
                print("❌ No multi-route found\n")
            return None

        all_results.sort(key=lambda r: r.total_score)
        best = all_results[0]

        if debug:
            print(f"\n✅ BEST ROUTE: {best.route_summary}")
            print(f"   Transfers: {best.number_of_transfers}")
            print(f"   Distance: {best.total_distance / 1000:.2f}km")
            print(f"   Duration: {best.total_duration / 60:.0f}min")
            if len(all_results) > 1:
                print(f"   (Found {len(all_results)} alternatives)\n")

        return best

    # ------------------------------------------------------------------
    def find_best_route_with_transfer(
        self,
        all_routes: List[JeepneyRoute],
        start: LatLng,
        dest: LatLng,
        max_board_distance: float = 800.0,
        max_alight_distance: float = 500.0,
        debug: bool = False,
    ):
        """Try a single route first; fall back to multi-route with transfer."""
        best_route, best_meta = self._single_route_finder.find_best_route(
            all_routes, start, dest,
            max_board_distance=max_board_distance,
            max_alight_distance=max_alight_distance,
            debug=debug,
        )

        if best_route is not None:
            if debug:
                print("✅ Direct route found\n")
            return best_route, best_meta

        if debug:
            print("\n🔄 No direct route. Searching with transfers...\n")

        multi_result = self.find_best_multi_route(
            all_routes, start, dest,
            max_board_distance=max_board_distance,
            max_alight_distance=max_alight_distance,
            debug=debug,
        )

        if multi_result is not None:
            return multi_result

        if debug:
            print("❌ No route found\n")
        return None


# ---------------------------------------------------------------------------
# Convenience loader (mirrors the original get_jeepney_navigation helper)
# ---------------------------------------------------------------------------

def load_routes(routes_path: str) -> List[JeepneyRoute]:
    with open(routes_path, "r") as f:
        data = json.load(f)["routes"]
    return [
        JeepneyRoute(
            route_number=str(r["route_number"]),
            direction=r["direction"],
            coordinates=[(c["lat"], c["lng"]) for c in r["coordinates"]],
        )
        for r in data
    ]


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root_dir = Path(__file__).resolve().parent.parent
    routes_path = root_dir / "data" / "jeepney_routes.json"
    transfers_path = root_dir / "data" / "transfer_spots.json"

    routes = load_routes(str(routes_path))

    finder = MultiJeepneyRouteFinder()
    finder.load_transfer_spots(str(transfers_path))

    my_start: LatLng = (10.7202, 122.5621)
    my_dest: LatLng  = (10.7015, 122.5690)

    result = finder.find_best_route_with_transfer(
        routes, my_start, my_dest, debug=True
    )

    if result is None:
        print("Could not find a route.")
    elif isinstance(result, MultiJeepneyRouteResult):
        print(f"\nFound Transfer Route! Summary: {result.route_summary}")
        print(f"Total Score: {result.total_score:.2f} | Distance: {result.total_distance / 1000:.2f}km")
        for i, seg in enumerate(result.segments):
            m = seg.meta
            print(f"  {i+1}. Take Jeepney {seg.route.route_number} ({seg.route.direction})")
            print(f"     Board at: {m.board_point} (Walk: {m.board_dist_m:.0f}m)")
            print(f"     Alight at: {m.alight_point} (Walk: {m.alight_dist_m:.0f}m)")
            print(f"     Ride: {m.jeepney_dist_m:.0f}m")
    else:
        # Single route result: (JeepneyRoute, RouteEvaluationMeta)
        route, meta = result
        print(f"\nFound Direct Route: {route.route_number} ({route.direction})")
        print(f"  Board at: {meta.board_point} (Walk: {meta.board_dist_m:.0f}m)")
        print(f"  Alight at: {meta.alight_point} (Walk: {meta.alight_dist_m:.0f}m)")
        print(f"  Ride: {meta.jeepney_dist_m:.0f}m | Score: {meta.score:.2f}")