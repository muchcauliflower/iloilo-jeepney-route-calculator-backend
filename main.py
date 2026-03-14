"""
main.py — FastAPI entry point for the Iloilo Jeepney Route Finder API

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Endpoints:
    POST /route    → find best jeepney route and return polylines + metadata
    GET  /health   → simple health check
"""

import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from routing.jeepney_route_picker import load_routes, MultiJeepneyRouteFinder
from routing.route_core import build_route_response

load_dotenv()

app = FastAPI(title="Iloilo Jeepney Route Finder", version="1.0.0")

# Allow React Native (Expo) dev server and production origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production
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


# ---------------------------------------------------------------------------
# Routes
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
    finder.load_transfer_spots("data/transfer_spots.json")

    result = finder.find_best_route_with_transfer(routes, start, dest, debug=False)

    if result is None:
        raise HTTPException(status_code=404, detail="No route found within walking limits.")

    best = build_route_response(start, dest, result)

    # Build alternatives from the caches populated during the search.
    # _last_direct_alternatives: list of (JeepneyRoute, RouteEvaluationMeta)
    # _last_multi_alternatives:  list of MultiJeepneyRouteResult
    alternatives = []
    for alt in finder._last_direct_alternatives:
        alternatives.append(build_route_response(start, dest, alt))
    for alt in finder._last_multi_alternatives:
        alternatives.append(build_route_response(start, dest, alt))

    return {"best": best, "alternatives": alternatives[:2]}


# ---------------------------------------------------------------------------
# Local dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)