"""
routing/route_finder.py — Streamlit dev UI for the Jeepney Route Finder

Run via UI.py:
    python3 UI.py

Map rendering (folium) lives here. All routing logic lives in route_core.py.
"""

import streamlit as st
import folium
from streamlit_folium import st_folium

from routing.jeepney_route_picker import load_routes, MultiJeepneyRouteFinder, MultiJeepneyRouteResult
from routing.route_core import build_route_response

# ---------------------------------------------------------------------------
# Colour scheme (UI-only constants)
#   🟢 Green dashed  — walking legs
#   🔵 Blue solid    — jeepney ride (1st)
#   🟠 Orange solid  — jeepney ride (2nd)
#   🟣 Purple solid  — jeepney ride (3rd)
# ---------------------------------------------------------------------------
JEEPNEY_COLORS = ["blue", "orange", "purple"]
WALK_COLOR   = "green"
WALK_DASH    = "8 6"
WALK_WEIGHT  = 3
RIDE_WEIGHT  = 5
RIDE_OPACITY = 0.85


# ---------------------------------------------------------------------------
# Map builder  (consumes the same JSON dict that the API returns)
# ---------------------------------------------------------------------------

def build_map(start: tuple, dest: tuple, response: dict) -> folium.Map:
    """Build a folium map from the standardised route_core response dict."""

    centre_lat = (start[0] + dest[0]) / 2
    centre_lon = (start[1] + dest[1]) / 2
    m = folium.Map(location=[centre_lat, centre_lon], zoom_start=14)

    folium.Marker(start, popup="📍 Start",
                  icon=folium.Icon(color="blue", icon="home")).add_to(m)
    folium.Marker(dest,  popup="🏁 Destination",
                  icon=folium.Icon(color="red",  icon="flag")).add_to(m)

    for seg in response["segments"]:
        color = JEEPNEY_COLORS[seg["segment_index"] % len(JEEPNEY_COLORS)]

        walk_to = [(p["latitude"], p["longitude"]) for p in seg["walk_to_polyline"]]
        folium.PolyLine(walk_to, color=WALK_COLOR, weight=WALK_WEIGHT,
                        dash_array=WALK_DASH,
                        tooltip="Walk to jeepney").add_to(m)

        board = (seg["board_point"]["lat"], seg["board_point"]["lng"])
        folium.Marker(board,
                      popup=f"🟢 Board Jeepney {seg['route_number']}",
                      icon=folium.Icon(color="green", icon="arrow-up")).add_to(m)

        ride = [(p["latitude"], p["longitude"]) for p in seg["jeepney_polyline"]]
        folium.PolyLine(ride, color=color, weight=RIDE_WEIGHT,
                        opacity=RIDE_OPACITY,
                        tooltip=f"Jeepney {seg['route_number']}").add_to(m)

        alight = (seg["alight_point"]["lat"], seg["alight_point"]["lng"])
        folium.Marker(alight,
                      popup=f"🔴 Alight Jeepney {seg['route_number']}",
                      icon=folium.Icon(color="red", icon="arrow-down")).add_to(m)

        walk_from = [(p["latitude"], p["longitude"]) for p in seg["walk_from_polyline"]]
        folium.PolyLine(walk_from, color=WALK_COLOR, weight=WALK_WEIGHT,
                        dash_array=WALK_DASH,
                        tooltip="Walk to destination / transfer").add_to(m)

    legend_html = """
    <div style="
        position: fixed; bottom: 30px; right: 10px; z-index: 1000;
        background: white; padding: 10px 14px; border-radius: 8px;
        box-shadow: 2px 2px 6px rgba(0,0,0,0.3); font-size: 13px; color: black;
    ">
        <b>Legend</b>
        <table style="border-collapse: collapse; margin-top: 6px;">
            <tr>
                <td style="padding: 3px 8px 3px 0;">
                    <svg width="30" height="10"><line x1="0" y1="5" x2="30" y2="5" stroke="green"
                    stroke-width="2.5" stroke-dasharray="6,4"/></svg>
                </td>
                <td style="padding: 3px 0;">Walking</td>
            </tr>
            <tr>
                <td style="padding: 3px 8px 3px 0;">
                    <svg width="30" height="10"><line x1="0" y1="5" x2="30" y2="5" stroke="blue" stroke-width="3"/></svg>
                </td>
                <td style="padding: 3px 0;">Jeepney (1st)</td>
            </tr>
            <tr>
                <td style="padding: 3px 8px 3px 0;">
                    <svg width="30" height="10"><line x1="0" y1="5" x2="30" y2="5" stroke="orange" stroke-width="3"/></svg>
                </td>
                <td style="padding: 3px 0;">Jeepney (2nd)</td>
            </tr>
            <tr>
                <td style="padding: 3px 8px 3px 0;">
                    <svg width="30" height="10"><line x1="0" y1="5" x2="30" y2="5" stroke="purple" stroke-width="3"/></svg>
                </td>
                <td style="padding: 3px 0;">Jeepney (3rd)</td>
            </tr>
        </table>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))
    return m


# ---------------------------------------------------------------------------
# Result label
# ---------------------------------------------------------------------------

def _make_result_label(response: dict) -> str:
    if response["type"] == "transfer":
        km   = response["total_distance_m"] / 1000
        mins = (response["total_duration_s"] or 0) / 60
        return (
            f"Transfer Route: {response['summary']} — "
            f"{response['number_of_transfers']} transfer(s) | "
            f"Score: {response['total_score']:.2f} | "
            f"Distance: {km:.2f} km | "
            f"~{mins:.0f} min"
        )
    else:
        seg = response["segments"][0]
        return (
            f"Direct Route: {seg['route_number']} ({seg['direction']}) — "
            f"Score: {seg['score']:.2f} | "
            f"Board walk: {seg['board_dist_m']:.0f} m | "
            f"Ride: {seg['jeepney_dist_m']:.0f} m | "
            f"Alight walk: {seg['alight_dist_m']:.0f} m"
        )


# ---------------------------------------------------------------------------
# Traffic UI helpers
# ---------------------------------------------------------------------------

def _traffic_badge(overall: str | None, status: str) -> str:
    """Return a short emoji+text badge for the traffic overall status."""
    if status == "disabled":
        return "🔘 Traffic: N/A"
    if status == "no_data" or overall is None:
        return "⚪ Traffic: No coverage data"
    icons = {"CLEAR": "🟢", "MODERATE": "🟡", "HEAVY": "🔴"}
    return f"{icons.get(overall, '⚪')} Traffic: {overall}"


def _render_traffic_info(st_col, segments: list) -> None:
    """Render per-segment traffic info as Streamlit expanders."""
    st_col.markdown("#### 🚦 Traffic Conditions")
    for seg in segments:
        traffic = seg.get("traffic", {})
        status  = traffic.get("status", "disabled")
        overall = traffic.get("overall")
        badge   = _traffic_badge(overall, status)

        label = f"Route {seg['route_number']} (Leg {seg['segment_index'] + 1})  —  {badge}"
        with st_col.expander(label, expanded=(overall in ("HEAVY", "MODERATE"))):
            if status == "disabled":
                st_col.info(
                    "TOMTOM_API_KEY is not set. "
                    "Add it to your .env file to enable traffic data."
                )
            elif status == "no_data":
                st_col.warning(
                    "TomTom returned no flow data for this corridor. "
                    "This is likely a coverage gap — not necessarily clear roads."
                )
            else:
                samples  = traffic.get("samples", [])
                icon_map = {"CLEAR": "🟢", "MODERATE": "🟡", "HEAVY": "🔴", "NO_DATA": "⚪"}
                for i, s in enumerate(samples, start=1):
                    cong = s["congestion"]
                    icon = icon_map.get(cong, "⚪")
                    if s["ratio"] is not None:
                        pct        = round(s["ratio"] * 100)
                        speed_text = (
                            f"{s['current_speed_kmph']:.0f} km/h "
                            f"(free-flow {s['free_flow_speed_kmph']:.0f} km/h — {pct}%)"
                        )
                    else:
                        speed_text = "No data"
                    st_col.write(
                        f"{icon} **Sample {i}** — {cong}  |  {speed_text}  "
                        f"@ `{s['lat']:.5f}, {s['lng']:.5f}`"
                    )


# ---------------------------------------------------------------------------
# Streamlit app entry point
# ---------------------------------------------------------------------------

def runUI():
    for key in ("map_obj", "result_label", "error_msg", "response"):
        if key not in st.session_state:
            st.session_state[key] = None

    st.title("🚌 Iloilo Jeepney Route Finder")

    col1, col2 = st.columns(2)
    with col1:
        start_lat = st.number_input("Start Latitude",        value=10.7202,  format="%.6f")
        start_lon = st.number_input("Start Longitude",       value=122.5621, format="%.6f")
    with col2:
        dest_lat  = st.number_input("Destination Latitude",  value=10.7015,  format="%.6f")
        dest_lon  = st.number_input("Destination Longitude", value=122.5690, format="%.6f")

    if st.button("Find Route"):
        start_node = (start_lat, start_lon)
        dest_node  = (dest_lat,  dest_lon)

        with st.spinner("Finding route..."):
            routes = load_routes("data/jeepney_routes.json")
            finder = MultiJeepneyRouteFinder()
            finder.load_transfer_spots("data/transfer_spots.json")
            result = finder.find_best_route_with_transfer(
                routes, start_node, dest_node, debug=True
            )

        if result is not None:
            with st.spinner("Fetching walking directions & traffic..."):
                response = build_route_response(start_node, dest_node, result)

            st.session_state.map_obj      = build_map(start_node, dest_node, response)
            st.session_state.result_label = _make_result_label(response)
            st.session_state.response     = response
            st.session_state.error_msg    = None
        else:
            st.session_state.map_obj      = None
            st.session_state.result_label = None
            st.session_state.response     = None
            st.session_state.error_msg    = "No route found within walking limits."

    if st.session_state.map_obj is not None:
        st.success(st.session_state.result_label)
        st_folium(st.session_state.map_obj, width=700, height=520, returned_objects=[])
        _render_traffic_info(st, st.session_state.response["segments"])
    elif st.session_state.error_msg:
        st.error(st.session_state.error_msg)