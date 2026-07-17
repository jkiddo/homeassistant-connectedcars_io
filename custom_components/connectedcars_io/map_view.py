"""HTTP view serving an interactive map of recent trips.

The page is reachable at /api/connectedcars_io/trips_map/<map_token> without
HA authentication, so it can be embedded in a dashboard Webpage (iframe) card.
The token is a per-config-entry random secret persisted in the entry data; it
grants access to this page only. The LastTrip sensor exposes the URL as its
"Map URL" attribute.
"""

from datetime import UTC, datetime, timedelta
import json
import logging
import secrets

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAX_COLORED_TRIPS = 8  # categorical palette slots; older trips render neutral

# Categorical palette (light/dark mode steps) and status colors for event
# severity. Validated with the dataviz palette validator against the map tile
# surfaces; identity never rides on color alone (legend + letter glyphs).
SERIES_LIGHT = [
    "#2a78d6",
    "#1baf7a",
    "#eda100",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#e87ba4",
    "#eb6834",
]
SERIES_DARK = [
    "#3987e5",
    "#199e70",
    "#c98500",
    "#008300",
    "#9085e9",
    "#e66767",
    "#d55181",
    "#d95926",
]
NEUTRAL = "#898781"
SEVERITY_COLORS = {"high": "#d03b3b", "medium": "#ec835a", "low": "#fab219"}
EVENT_LETTERS = {
    "acceleration": "A",
    "brake": "B",
    "speeding": "F",
    "turn_left": "S",
    "turn_right": "S",
}
EVENT_NAMES_DA = {
    "acceleration": "Acceleration",
    "brake": "Opbremsning",
    "speeding": "Fartoverskridelse",
    "turn_left": "Sving (venstre)",
    "turn_right": "Sving (højre)",
}
SEVERITY_NAMES_DA = {"high": "kraftig", "medium": "middel", "low": "let"}


def _parse_ts(value):
    """ISO-8601 string to epoch seconds, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _event_positions(trip):
    """Attach coordinates to each detected event by time-interpolating the
    trip's GPS track."""
    track = []
    for pos in trip.get("positions") or []:
        ts = _parse_ts(pos.get("time"))
        if ts is not None and pos.get("latitude") is not None:
            track.append((ts, pos["latitude"], pos["longitude"]))
    track.sort()

    events = []
    for event in trip.get("profilings") or []:
        ts = _parse_ts(event.get("time"))
        if ts is None or not track:
            continue
        after = next((p for p in track if p[0] >= ts), None)
        before = next((p for p in reversed(track) if p[0] <= ts), None)
        if before is None or after is None:
            point = before or after
            lat, lon = point[1], point[2]
        elif after[0] == before[0]:
            lat, lon = before[1], before[2]
        else:
            frac = (ts - before[0]) / (after[0] - before[0])
            lat = before[1] + frac * (after[1] - before[1])
            lon = before[2] + frac * (after[2] - before[2])
        parts = str(event.get("type", "")).rsplit("_", 1)
        kind, severity = parts[0], (parts[1] if len(parts) == 2 else "low")
        events.append(
            {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "letter": EVENT_LETTERS.get(kind, "?"),
                "name": EVENT_NAMES_DA.get(kind, kind),
                "severity": severity,
                "severityName": SEVERITY_NAMES_DA.get(severity, severity),
                "color": SEVERITY_COLORS.get(severity, NEUTRAL),
                "time": event.get("time"),
                "g": event.get("gForce"),
            }
        )
    return events


def _trip_path(trip):
    """GPS track as [lat, lon] pairs; falls back to the start/end points."""
    path = [
        [round(p["latitude"], 6), round(p["longitude"], 6)]
        for p in (trip.get("positions") or [])
        if p.get("latitude") is not None and p.get("longitude") is not None
    ]
    if not path:
        for key in ("start", "end"):
            lat, lon = trip.get(f"{key}Latitude"), trip.get(f"{key}Longitude")
            if lat is not None and lon is not None:
                path.append([round(lat, 6), round(lon, 6)])
    return path


def build_payload(vehicle, trips, days):
    """JSON-serializable payload embedded in the map page."""
    out = []
    for idx, trip in enumerate(trips):
        path = _trip_path(trip)
        if len(path) < 2:
            continue
        colored = idx < MAX_COLORED_TRIPS
        out.append(
            {
                "colorLight": SERIES_LIGHT[idx] if colored else NEUTRAL,
                "colorDark": SERIES_DARK[idx] if colored else NEUTRAL,
                "startTime": trip.get("startTime"),
                "distanceKm": trip.get("mileage"),
                "durationMin": trip.get("duration"),
                "fromAddress": trip.get("startAddressString"),
                "toAddress": trip.get("endAddressString"),
                "path": path,
                "events": _event_positions(trip),
            }
        )
    return {"vehicle": vehicle.get("name"), "days": days, "trips": out}


def async_ensure_map_token(hass, entry):
    """Make sure the config entry carries a persistent map token.

    Must run before the entry's update listener is registered, so the
    data update does not trigger a reload loop.
    """
    if "map_token" not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "map_token": secrets.token_hex(16)}
        )
    return entry.data["map_token"]


class ConnectedCarsTripsMapView(HomeAssistantView):
    """Serve the trips map page."""

    url = "/api/connectedcars_io/trips_map/{token}"
    name = "api:connectedcars_io:trips_map"
    requires_auth = False

    def __init__(self, hass) -> None:
        """Initialize."""
        self.hass = hass

    async def get(self, request, token):
        """Render the map page."""
        client = None
        for config in self.hass.data.get(DOMAIN, {}).values():
            candidate = config.get("map_token")
            if candidate and secrets.compare_digest(candidate, token):
                client = config["connectedcarsclient"]
                break
        if client is None:
            return web.Response(status=404, text="Unknown map token")

        try:
            days = min(max(int(request.query.get("days", 7)), 1), 365)
            limit = min(max(int(request.query.get("limit", 8)), 1), 200)
        except ValueError:
            return web.Response(status=400, text="Bad days/limit")
        vin = request.query.get("vin")

        vehicles = await client.get_vehicle_instances()
        vehicle = next(
            (v for v in vehicles if vin is None or v["vin"] == vin), None
        )
        if vehicle is None:
            return web.Response(status=404, text="Unknown VIN")

        from_iso = (
            (datetime.now(UTC) - timedelta(days=days))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        trips = (
            await client.get_trips(
                vehicle["id"],
                from_iso=from_iso,
                limit=limit,
                include_events=True,
                include_positions=True,
            )
            or []
        )
        payload = build_payload(vehicle, trips, days)
        return web.Response(
            text=render_map_html(payload),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )


def render_map_html(payload):
    """Self-contained Leaflet page; payload is inlined as JSON."""
    return _MAP_HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))


_MAP_HTML = """<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ture</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {
    --surface-1: #fcfcfb;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --border: rgba(11,11,11,0.10);
    --ring: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface-1: #1a1a19;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --border: rgba(255,255,255,0.10);
      --ring: #1a1a19;
    }
  }
  html, body { margin: 0; height: 100%; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  #map { height: 100%; }
  .legend {
    position: absolute; top: 10px; left: 10px; z-index: 1000;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 8px 10px; max-height: 55%; overflow: auto;
    font-size: 12px; line-height: 1.45; max-width: 46%;
    box-shadow: 0 1px 4px rgba(0,0,0,0.15);
  }
  .legend h1 { font-size: 12px; margin: 0 0 4px; font-weight: 600; }
  .ranges { display: flex; gap: 4px; margin: 0 0 6px; }
  .ranges button {
    font: inherit; font-size: 11px; color: var(--text-secondary);
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; padding: 2px 7px; cursor: pointer;
  }
  .ranges button:hover { color: var(--text-primary); }
  .ranges button.active {
    color: var(--text-primary); font-weight: 600;
    border-color: var(--text-secondary);
  }
  .legend .trip { display: flex; align-items: center; gap: 6px; cursor: pointer; white-space: nowrap; }
  .legend .trip:hover { text-decoration: underline; }
  .legend .chip { width: 10px; height: 10px; border-radius: 3px; flex: none; }
  .legend .muted { color: var(--text-secondary); }
  .legend .total { font-weight: 600; margin: 0 0 4px; }
  .legend .empty { color: var(--text-secondary); }
  .ev-icon {
    display: flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; border-radius: 50%;
    border: 2px solid var(--ring);
    color: #0b0b0b; font-size: 9px; font-weight: 700;
    box-sizing: border-box;
  }
  .start-icon {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--ring); border: 3px solid #000; box-sizing: border-box;
  }
  .leaflet-tooltip { font-family: inherit; }
</style>
</head>
<body>
<div id="map"></div>
<div class="legend" id="legend"></div>
<script>
"use strict";
const DATA = __PAYLOAD__;
const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;

const map = L.map("map", { zoomControl: true });
L.tileLayer(
  dark
    ? "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
    : "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
  { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>', maxZoom: 19 }
).addTo(map);

const ring = dark ? "#1a1a19" : "#ffffff";
const fmt = (iso, withDate) => {
  const d = new Date(iso);
  const opts = withDate
    ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" };
  return d.toLocaleString("da-DK", opts);
};

const legend = document.getElementById("legend");
legend.innerHTML = "<h1>" + (DATA.vehicle || "Bil") + " · ture, seneste " + DATA.days + " dage</h1>";

const ranges = document.createElement("div");
ranges.className = "ranges";
[[7, "7 dage"], [30, "30 dage"], [90, "90 dage"], [365, "1 år"]].forEach(([days, label]) => {
  const btn = document.createElement("button");
  btn.textContent = label;
  if (days === DATA.days) btn.className = "active";
  btn.addEventListener("click", () => {
    const url = new URL(window.location);
    url.searchParams.set("days", days);
    window.location = url;
  });
  ranges.appendChild(btn);
});
legend.appendChild(ranges);

const totalKm = DATA.trips.reduce((sum, t) => sum + (t.distanceKm || 0), 0);
const total = document.createElement("div");
total.className = "total";
total.textContent = "I alt " +
  totalKm.toLocaleString("da-DK", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) +
  " km · " + DATA.trips.length + (DATA.trips.length === 1 ? " tur" : " ture");
legend.appendChild(total);

const allBounds = [];
DATA.trips.forEach((trip) => {
  const color = dark ? trip.colorDark : trip.colorLight;
  const label = fmt(trip.startTime, true) + " · " +
    (trip.distanceKm != null ? trip.distanceKm.toFixed(1) : "?") + " km" +
    (trip.durationMin != null ? " · " + trip.durationMin + " min" : "");

  // white/dark casing under the line separates overlapping trips
  L.polyline(trip.path, { color: ring, weight: 7, opacity: 0.8, interactive: false }).addTo(map);
  const line = L.polyline(trip.path, { color: color, weight: 3 }).addTo(map);
  line.bindTooltip(label, { sticky: true });
  line.on("mouseover", () => line.setStyle({ weight: 5 }));
  line.on("mouseout", () => line.setStyle({ weight: 3 }));

  L.marker(trip.path[0], {
    icon: L.divIcon({
      className: "",
      html: '<div class="start-icon" style="border-color:' + color + '"></div>',
      iconSize: [10, 10], iconAnchor: [5, 5],
    }),
    interactive: false,
  }).addTo(map);

  trip.events.forEach((ev) => {
    const marker = L.marker([ev.lat, ev.lon], {
      icon: L.divIcon({
        className: "",
        html: '<div class="ev-icon" style="background:' + ev.color + '">' + ev.letter + "</div>",
        iconSize: [16, 16], iconAnchor: [8, 8],
      }),
    }).addTo(map);
    marker.bindTooltip(
      ev.name + " (" + ev.severityName + ") · " + fmt(ev.time, false) +
      (ev.g != null ? " · " + ev.g + " g" : "")
    );
  });

  const row = document.createElement("div");
  row.className = "trip";
  row.innerHTML = '<span class="chip" style="background:' + color + '"></span>' +
    "<span>" + label + "</span>";
  row.title = (trip.fromAddress || "?") + " → " + (trip.toAddress || "?");
  row.addEventListener("click", () => map.fitBounds(line.getBounds(), { padding: [30, 30] }));
  row.addEventListener("mouseenter", () => line.setStyle({ weight: 6 }));
  row.addEventListener("mouseleave", () => line.setStyle({ weight: 3 }));
  legend.appendChild(row);

  allBounds.push(line.getBounds());
});

if (allBounds.length) {
  const bounds = allBounds.reduce((acc, b) => acc.extend(b), L.latLngBounds(allBounds[0]));
  map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
} else {
  legend.innerHTML += '<div class="empty">Ingen ture i perioden.</div>';
  map.setView([56.0, 10.5], 6);
}
const note = document.createElement("div");
note.className = "muted";
note.textContent = "A: acceleration · B: opbremsning · F: fart · S: sving";
legend.appendChild(note);
</script>
</body>
</html>
"""
