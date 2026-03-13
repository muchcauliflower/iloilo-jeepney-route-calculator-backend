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

  # Flat markers list — used to place pins on the map
  "markers": [
    {"type": "start",  "lat": float, "lng": float},
    {"type": "board",  "lat": float, "lng": float, "route_number": str},
    {"type": "alight", "lat": float, "lng": float, "route_number": str},
    ...
    {"type": "dest",   "lat": float, "lng": float}
  ],

  # One entry per jeepney leg
  "segments": [
    {
      "segment_index":       int,             # 0-based
      "route_number":        str,
      "direction":           str,
      "board_point":         {"lat": float, "lng": float},
      "alight_point":        {"lat": float, "lng": float},
      "board_dist_m":        float,           # walk from prev point → board
      "alight_dist_m":       float,           # walk from alight → next point
      "jeepney_dist_m":      float,
      "score":               float,
      "transfer_spot_name":  str | None,      # name of transfer spot, if applicable

      # The jeepney polyline — ready for react-native-maps <Polyline>
      # Each point is {latitude, longitude} (React Native convention)
      "jeepney_polyline": [{"latitude": float, "longitude": float}, ...],

      # Walking polylines fetched from ORS (or straight-line fallback)
      # walk_to:   walk from previous location to this segment's board point
      # walk_from: walk from this segment's alight point to the next location
      "walk_to_polyline":   [{"latitude": float, "longitude": float}, ...],
      "walk_from_polyline": [{"latitude": float, "longitude": float}, ...]
    }
  ]
}
"""

import os
import openrouteservice
from typing import Union, Tuple, List

from routing.jeepney_route_picker import (
    MultiJeepneyRouteResult,
    MultiRouteSegment,
    JeepneyRoute,
    RouteEvaluationMeta,
)

LatLng = Tuple[float, float]

# Lazy-initialise so importing this module never crashes if key is missing
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
# Segment builder helpers
# ---------------------------------------------------------------------------

def _build_segment_dict(
    index: int,
    route_number: str,
    direction: str,
    meta: RouteEvaluationMeta,
    walk_from_prev: LatLng,   # previous location  → board point
    walk_to_next: LatLng,     # alight point        → next location
) -> dict:
    jeepney_polyline = [_rn_point(lat, lng) for (lat, lng) in meta.jeepney_segment]
    walk_to_polyline   = get_walking_polyline(walk_from_prev, meta.board_point)
    walk_from_polyline = get_walking_polyline(meta.alight_point, walk_to_next)

    return {
        "segment_index":    index,
        "route_number":     route_number,
        "direction":        direction,
        "board_point":      {"lat": meta.board_point[0],  "lng": meta.board_point[1]},
        "alight_point":     {"lat": meta.alight_point[0], "lng": meta.alight_point[1]},
        "board_dist_m":     meta.board_dist_m,
        "alight_dist_m":    meta.alight_dist_m,
        "jeepney_dist_m":   meta.jeepney_dist_m,
        "score":            meta.score,
        "jeepney_polyline": jeepney_polyline,
        "walk_to_polyline":   walk_to_polyline,
        "walk_from_polyline": walk_from_polyline,
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
            # Determine the "next location" for the final walk of this segment:
            #   - for all but the last segment: the next segment's board point
            #     (which equals the transfer spot location)
            #   - for the last segment: the final destination
            if i < len(result.segments) - 1:
                next_loc = result.transfers[i].to_board_point
            else:
                next_loc = dest

            # Walk to this segment's board comes from:
            #   - segment 0: the start point
            #   - segment n: the transfer spot (previous alight → transfer spot
            #     was already resolved; transfer spot IS the new start)
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

            # Add transfer spot name if this segment has a transfer after it
            if i < len(result.transfers):
                try:
                    segment_dict["transfer_spot_name"] = result.transfers[i].transfer_spot.name
                except AttributeError:
                    segment_dict["transfer_spot_name"] = None
            else:
                segment_dict["transfer_spot_name"] = None

            segments_raw.append(segment_dict)

        return {
            "type":               "transfer",
            "summary":            result.route_summary,
            "number_of_transfers": result.number_of_transfers,
            "total_score":        result.total_score,
            "total_distance_m":   result.total_distance,
            "total_duration_s":   result.total_duration,
            "markers":            _build_markers(start, dest, segments_raw),
            "segments":           segments_raw,
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
            "type":               "direct",
            "summary":            route.route_number,
            "number_of_transfers": 0,
            "total_score":        meta.score,
            "total_distance_m":   meta.board_dist_m + meta.jeepney_dist_m + meta.alight_dist_m,
            "total_duration_s":   None,   # not computed for direct routes currently
            "markers":            _build_markers(start, dest, [seg]),
            "segments":           [seg],
        }