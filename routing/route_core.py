"""
routing/route_core.py
---------------------
Pure routing logic — no Streamlit, no Folium.

Exports:
    build_route_response(start, dest, result) -> dict

This is the single source of truth for converting a finder result into
JSON-serializable data. Both the FastAPI (main.py) and the Streamlit UI
(route_finder.py) import from here.

Response shape
--------------
{
  "type":             "direct" | "transfer",
  "summary":          str,               # e.g. "10B" or "10B → 2A"
  "number_of_transfers": int,
  "total_score":      float,
  "total_distance_m": float,
  "total_duration_s": float,

  "markers": [
    {"type": "start",  "lat": float, "lng": float},
    {"type": "board",  "lat": float, "lng": float, "route_number": str},
    {"type": "alight", "lat": float, "lng": float, "route_number": str},
    {"type": "dest",   "lat": float, "lng": float}
  ],

  "segments": [
    {
      "segment_index":       int,
      "route_number":        str,
      "direction":           str,
      "board_point":         {"lat": float, "lng": float},
      "alight_point":        {"lat": float, "lng": float},
      "board_dist_m":        float,
      "alight_dist_m":       float,
      "jeepney_dist_m":      float,
      "score":               float,
      "transfer_spot_name":  str | None,
      "jeepney_polyline":    [{"latitude": float, "longitude": float}, ...],
      "walk_to_polyline":    [{"latitude": float, "longitude": float}, ...],
      "walk_from_polyline":  [{"latitude": float, "longitude": float}, ...],

      "traffic": {
        "status":  "ok" | "no_data" | "disabled",
        "overall": "CLEAR" | "MODERATE" | "HEAVY" | null,
        "samples": [
          {
            "lat": float, "lng": float,
            "current_speed_kmph":   float | null,
            "free_flow_speed_kmph": float | null,
            "ratio":                float | null,
            "congestion":           "CLEAR" | "MODERATE" | "HEAVY" | "NO_DATA",
            "road_description":     str | null
          }
        ]
      }
    }
  ]
}
"""

import os
import requests
import openrouteservice
from typing import Tuple, List, Optional

from routing.jeepney_route_picker import (
    MultiJeepneyRouteResult,
    MultiRouteSegment,
    JeepneyRoute,
    RouteEvaluationMeta,
)

LatLng = Tuple[float, float]

# ---------------------------------------------------------------------------
# ORS client (lazy init)
# ---------------------------------------------------------------------------

_ors_client = None


def _get_ors() -> openrouteservice.Client:
    global _ors_client
    if _ors_client is None:
        _ors_client = openrouteservice.Client(key=os.getenv("itsasecret"))
    return _ors_client


# ---------------------------------------------------------------------------
# Walking polyline helper
# ---------------------------------------------------------------------------

def _rn_point(lat: float, lng: float) -> dict:
    """React Native / Expo MapView uses {latitude, longitude}."""
    return {"latitude": lat, "longitude": lng}


def get_walking_polyline(start: LatLng, end: LatLng) -> List[dict]:
    """
    Returns a list of {latitude, longitude} dicts for the walking path.
    Falls back to a straight line if ORS fails.
    """
    coords = [[start[1], start[0]], [end[1], end[0]]]  # ORS: [lon, lat]
    try:
        route = _get_ors().directions(
            coordinates=coords, profile="foot-walking", format="geojson"
        )
        raw = route["features"][0]["geometry"]["coordinates"]  # [[lon, lat], ...]
        return [_rn_point(p[1], p[0]) for p in raw]
    except Exception:
        return [_rn_point(start[0], start[1]), _rn_point(end[0], end[1])]


# ---------------------------------------------------------------------------
# TomTom Traffic Flow
# ---------------------------------------------------------------------------

# How many evenly-spaced points along the jeepney polyline to sample.
_TRAFFIC_SAMPLE_COUNT = 5

# Congestion thresholds (currentSpeed / freeFlowSpeed ratio)
_THRESHOLD_HEAVY    = 0.50   # < 50%  → HEAVY
_THRESHOLD_MODERATE = 0.75   # < 75%  → MODERATE  (else CLEAR)

_TOMTOM_FLOW_URL = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/"
    "absolute/10/json"
)


def _sample_polyline(polyline: List[LatLng], n: int) -> List[LatLng]:
    """Return n evenly-spaced points from a polyline (always includes endpoints)."""
    if len(polyline) <= n:
        return polyline
    indices = [round(i * (len(polyline) - 1) / (n - 1)) for i in range(n)]
    return [polyline[i] for i in indices]


def _query_flow_segment(lat: float, lng: float, api_key: str) -> Optional[dict]:
    """
    Call TomTom Flow Segment Data for a single point.
    Returns the flowSegmentData dict, or None on any failure.
    """
    try:
        resp = requests.get(
            _TOMTOM_FLOW_URL,
            params={
                "key":   api_key,
                "point": f"{lat},{lng}",
                "unit":  "KMPH",
            },
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("flowSegmentData")
        return None
    except Exception:
        return None


def _congestion_label(ratio: float) -> str:
    if ratio < _THRESHOLD_HEAVY:
        return "HEAVY"
    if ratio < _THRESHOLD_MODERATE:
        return "MODERATE"
    return "CLEAR"


def get_traffic_flow_for_segment(
    jeepney_polyline: List[LatLng],
    route_number: str,
    segment_index: int,
) -> dict:
    """
    Sample the jeepney polyline, query TomTom Flow for each sample point,
    and return a traffic summary dict to attach to the segment.
    """
    api_key = os.getenv("TOMTOM_API_KEY")
    if not api_key:
        return {"status": "disabled", "overall": None, "samples": []}

    sample_points = _sample_polyline(jeepney_polyline, _TRAFFIC_SAMPLE_COUNT)
    samples = []
    ratios  = []

    for lat, lng in sample_points:
        data = _query_flow_segment(lat, lng, api_key)
        if data is None:
            samples.append({
                "lat": lat, "lng": lng,
                "current_speed_kmph":   None,
                "free_flow_speed_kmph": None,
                "ratio":                None,
                "congestion":           "NO_DATA",
                "road_description":     None,
            })
            continue

        current   = data.get("currentSpeed")
        free_flow = data.get("freeFlowSpeed")
        coords    = data.get("coordinates", {}).get("coordinate", [])
        road_desc = f"{len(coords)}-point segment" if coords else None

        ratio = (current / free_flow) if (current and free_flow and free_flow > 0) else None
        if ratio is not None:
            ratios.append(ratio)

        samples.append({
            "lat": lat, "lng": lng,
            "current_speed_kmph":   current,
            "free_flow_speed_kmph": free_flow,
            "ratio":                round(ratio, 3) if ratio is not None else None,
            "congestion":           _congestion_label(ratio) if ratio is not None else "NO_DATA",
            "road_description":     road_desc,
        })

    if not ratios:
        overall = None
        status  = "no_data"
    else:
        overall = _congestion_label(min(ratios))
        status  = "ok"

    _print_traffic_summary(route_number, segment_index, samples, overall, status)

    return {"status": status, "overall": overall, "samples": samples}


def _print_traffic_summary(
    route_number: str,
    segment_index: int,
    samples: list,
    overall: Optional[str],
    status: str,
) -> None:
    """Print a readable traffic summary to the console for this segment."""
    icon_map = {"CLEAR": "🟢", "MODERATE": "🟡", "HEAVY": "🔴", "NO_DATA": "⚪"}
    overall_icon = icon_map.get(overall, "⚪") if overall else "⚪"

    print(f"\n  🚦 Traffic — Route {route_number} (Leg {segment_index + 1})")
    print(f"     Overall : {overall_icon} {overall or 'NO DATA'}")

    if status == "disabled":
        print("     ⚠️  TOMTOM_API_KEY not set — traffic disabled")
        return
    if status == "no_data":
        print("     ℹ️  No flow data returned for this corridor (coverage gap)")
        return

    for i, s in enumerate(samples, start=1):
        cong  = s["congestion"]
        icon  = icon_map.get(cong, "⚪")
        if s["ratio"] is not None:
            pct = round(s["ratio"] * 100)
            speed_info = (
                f"{s['current_speed_kmph']:.0f} km/h"
                f" (free-flow {s['free_flow_speed_kmph']:.0f} km/h, {pct}%)"
            )
        else:
            speed_info = "no data"
        print(f"     Sample {i}: {icon} {cong:<8}  {speed_info}"
              f"  @ ({s['lat']:.5f}, {s['lng']:.5f})")


# ---------------------------------------------------------------------------
# Segment builder helpers
# ---------------------------------------------------------------------------

def _build_segment_dict(
    index: int,
    route_number: str,
    direction: str,
    meta: RouteEvaluationMeta,
    walk_from_prev: LatLng,
    walk_to_next: LatLng,
) -> dict:
    jeepney_polyline   = [_rn_point(lat, lng) for (lat, lng) in meta.jeepney_segment]
    walk_to_polyline   = get_walking_polyline(walk_from_prev, meta.board_point)
    walk_from_polyline = get_walking_polyline(meta.alight_point, walk_to_next)

    traffic = get_traffic_flow_for_segment(
        jeepney_polyline=meta.jeepney_segment,
        route_number=route_number,
        segment_index=index,
    )

    return {
        "segment_index":      index,
        "route_number":       route_number,
        "direction":          direction,
        "board_point":        {"lat": meta.board_point[0],  "lng": meta.board_point[1]},
        "alight_point":       {"lat": meta.alight_point[0], "lng": meta.alight_point[1]},
        "board_dist_m":       meta.board_dist_m,
        "alight_dist_m":      meta.alight_dist_m,
        "jeepney_dist_m":     meta.jeepney_dist_m,
        "score":              meta.score,
        "jeepney_polyline":   jeepney_polyline,
        "walk_to_polyline":   walk_to_polyline,
        "walk_from_polyline": walk_from_polyline,
        "traffic":            traffic,
    }


def _build_markers(start: LatLng, dest: LatLng, segments_raw: list) -> list:
    markers = [{"type": "start", "lat": start[0], "lng": start[1]}]
    for seg in segments_raw:
        markers.append({
            "type": "board",
            "lat": seg["board_point"]["lat"],
            "lng": seg["board_point"]["lng"],
            "route_number": seg["route_number"],
        })
        markers.append({
            "type": "alight",
            "lat": seg["alight_point"]["lat"],
            "lng": seg["alight_point"]["lng"],
            "route_number": seg["route_number"],
        })
    markers.append({"type": "dest", "lat": dest[0], "lng": dest[1]})
    return markers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_route_response(
    start: LatLng,
    dest: LatLng,
    result,   # (JeepneyRoute, RouteEvaluationMeta)  OR  MultiJeepneyRouteResult
) -> dict:
    """
    Convert a finder result into a JSON-serializable dict suitable for
    both the FastAPI response and the Streamlit UI.
    """

    if isinstance(result, MultiJeepneyRouteResult):
        # ---- Transfer route ------------------------------------------------
        segments_raw = []
        for i, seg in enumerate(result.segments):
            if i < len(result.segments) - 1:
                next_loc = result.transfers[i].to_board_point
            else:
                next_loc = dest

            if i == 0:
                prev_loc = start
            else:
                prev_loc = result.transfers[i - 1].to_board_point

            segment_dict = _build_segment_dict(
                index=i,
                route_number=seg.route.route_number,
                direction=seg.route.direction,
                meta=seg.meta,
                walk_from_prev=prev_loc,
                walk_to_next=next_loc,
            )

            if i < len(result.transfers):
                try:
                    segment_dict["transfer_spot_name"] = result.transfers[i].transfer_spot.name
                except AttributeError:
                    segment_dict["transfer_spot_name"] = None
            else:
                segment_dict["transfer_spot_name"] = None

            segments_raw.append(segment_dict)

        return {
            "type":                "transfer",
            "summary":             result.route_summary,
            "number_of_transfers": result.number_of_transfers,
            "total_score":         result.total_score,
            "total_distance_m":    result.total_distance,
            "total_duration_s":    result.total_duration,
            "markers":             _build_markers(start, dest, segments_raw),
            "segments":            segments_raw,
        }

    else:
        # ---- Direct route --------------------------------------------------
        route, meta = result
        seg = _build_segment_dict(
            index=0,
            route_number=route.route_number,
            direction=route.direction,
            meta=meta,
            walk_from_prev=start,
            walk_to_next=dest,
        )
        seg["transfer_spot_name"] = None

        return {
            "type":                "direct",
            "summary":             route.route_number,
            "number_of_transfers": 0,
            "total_score":         meta.score,
            "total_distance_m":    meta.board_dist_m + meta.jeepney_dist_m + meta.alight_dist_m,
            "total_duration_s":    None,
            "markers":             _build_markers(start, dest, [seg]),
            "segments":            [seg],
        }