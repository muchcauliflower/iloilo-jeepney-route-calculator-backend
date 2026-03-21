"""
main.py — FastAPI entry point for the Iloilo Jeepney Route Finder API

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Endpoints:
    POST /route          → find best jeepney route, return polylines + metadata
    POST /route/traffic  → fetch TomTom traffic for a set of segments
    GET  /health         → simple health check
"""

import os
import json
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Generator, Tuple
from dotenv import load_dotenv

from routing.jeepney_route_picker import load_routes, MultiJeepneyRouteFinder
from routing.route_core import build_route_response, get_traffic_flow_for_segment

load_dotenv()

app = FastAPI(title="Iloilo Jeepney Route Finder", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class RouteRequest(BaseModel):
    start_lat: float
    start_lng: float
    dest_lat: float
    dest_lng: float


class TrafficSegmentRequest(BaseModel):
    segment_index: int
    route_number: str
    # jeepney_polyline uses {latitude, longitude} — same shape as route response
    jeepney_polyline: List[dict]


class TrafficRequest(BaseModel):
    segments: List[TrafficSegmentRequest]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/route")
def find_route(req: RouteRequest):
    """
    Streams results as NDJSON so the frontend can render the best route
    immediately while alternatives are still being built.

    Line 1: {"type": "best",        ...route response...}
    Line 2: {"type": "alternative",  ...route response...}  (0–2 lines)
    Line 3: {"type": "done"}
    """
    start = (req.start_lat, req.start_lng)
    dest  = (req.dest_lat,  req.dest_lng)

    def _stream() -> Generator[str, None, None]:
        routes = load_routes("data/jeepney_routes.json")
        finder = MultiJeepneyRouteFinder()

        # Wrap evaluate_route with a per-request LRU cache.
        # Same (route coords, start, dest) triple is often evaluated multiple
        # times across recursive paths — cache avoids redundant geometry work.
        _orig_evaluate = finder._single_route_finder.evaluate_route

        @functools.lru_cache(maxsize=512)
        def _cached_evaluate(coords_key: tuple, start_: Tuple, dest_: Tuple, board_d: float, alight_d: float):
            # coords_key is a hashable tuple of the route's coordinates
            route_coords = list(coords_key)
            return _orig_evaluate(route_coords, start_, dest_,
                                  max_board_distance=board_d,
                                  max_alight_distance=alight_d)

        def _patched_evaluate(route_coords, start_, dest_, **kwargs):
            board_d  = kwargs.get("max_board_distance", 800.0)
            alight_d = kwargs.get("max_alight_distance", 500.0)
            return _cached_evaluate(tuple(route_coords), start_, dest_, board_d, alight_d)

        finder._single_route_finder.evaluate_route = _patched_evaluate

        result = finder.find_best_route_with_transfer(routes, start, dest, debug=False)

        if result is None:
            yield json.dumps({"type": "error", "detail": "No route found within walking limits."}) + "\n"
            return

        # Stream best route immediately
        best = build_route_response(start, dest, result)
        yield json.dumps({"type": "best", **best}) + "\n"

        # Stream alternatives as they're built
        alts = []
        for alt in finder._last_direct_alternatives:
            alts.append(build_route_response(start, dest, alt))
        for alt in finder._last_multi_alternatives:
            alts.append(build_route_response(start, dest, alt))

        for alt in alts[:2]:
            yield json.dumps({"type": "alternative", **alt}) + "\n"

        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.post("/route/traffic")
def fetch_traffic(req: TrafficRequest):
    """
    Streams traffic data segment-by-segment as each TomTom call completes.
    Each line is a newline-delimited JSON object the frontend can process
    immediately — the map overlay updates per segment as results arrive
    rather than waiting for all TomTom calls to finish.
    """
    def _fetch_one(seg: TrafficSegmentRequest) -> dict:
        polyline = [(p["latitude"], p["longitude"]) for p in seg.jeepney_polyline]
        traffic  = get_traffic_flow_for_segment(
            jeepney_polyline=polyline,
            route_number=seg.route_number,
            segment_index=seg.segment_index,
        )
        return {"segment_index": seg.segment_index, "traffic": traffic}

    def _stream() -> Generator[str, None, None]:
        with ThreadPoolExecutor(max_workers=min(len(req.segments), 8)) as executor:
            futures = {executor.submit(_fetch_one, seg): seg for seg in req.segments}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception:
                    seg = futures[future]
                    result = {
                        "segment_index": seg.segment_index,
                        "traffic": {"status": "error", "overall": None, "samples": []},
                    }
                # Each completed segment is yielded immediately as a JSON line
                yield json.dumps(result) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# Local dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)