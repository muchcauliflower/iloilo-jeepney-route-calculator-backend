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
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Generator
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
    start = (req.start_lat, req.start_lng)
    dest  = (req.dest_lat,  req.dest_lng)

    routes = load_routes("data/jeepney_routes.json")
    finder = MultiJeepneyRouteFinder()

    result = finder.find_best_route_with_transfer(routes, start, dest, debug=False)

    if result is None:
        raise HTTPException(status_code=404, detail="No route found within walking limits.")

    best = build_route_response(start, dest, result)

    alternatives = []
    for alt in finder._last_direct_alternatives:
        alternatives.append(build_route_response(start, dest, alt))
    for alt in finder._last_multi_alternatives:
        alternatives.append(build_route_response(start, dest, alt))

    return {"best": best, "alternatives": alternatives[:2]}


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