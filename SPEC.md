# SeismicLog — Specification

Version 0.1. Authoritative source for implementation. The implementer should
treat every section as binding; deviations require a comment in the code.

---

## 1. Domain & Motivation

SeismicLog is a single-user, single-tenant web application that aggregates
global earthquake activity from the USGS public feed and converts it into
two views: a live global feed (map + table over the last 24 hours / 7 days
/ 30 days), and a per-address long-term risk profile for one or more
"watch" locations the user cares about (home, family, secondary residence).
The motivation is to replace the doom-scrolling experience of news-driven
earthquake reporting with a sober, government-portal-style dashboard that
answers two concrete questions: "what is happening now" and "what does
the 30-year record say about the place where my parents live". The
LLM is used in exactly one place — turning the seven numbers of a risk
assessment into a two-paragraph plain-English briefing for a non-expert
resident.

---

## 2. Stack

| Layer       | Choice                                                        |
|-------------|---------------------------------------------------------------|
| Language    | Python 3.12                                                   |
| Framework   | Flask 3.x                                                     |
| ORM         | SQLAlchemy 2.x (declarative, typed `Mapped[...]`)             |
| Validation  | marshmallow 3.x (one schema per request/response shape)       |
| WSGI server | gunicorn (2 workers, threaded class, port 8002)               |
| Storage     | SQLite at `seismiclog/data/seismiclog.db`                     |
| HTTP client | Python stdlib `urllib.request` (no `requests`, no `httpx`)    |
| Frontend    | Server-rendered single HTML page + vanilla JS + Leaflet 1.9   |
| Map tiles   | OpenStreetMap raster tiles via Leaflet, attributed            |
| Charts      | None. Numeric tiles and tables only.                          |
| Container   | `python:3.12-slim`, multi-stage, non-root user `quake` (uid 1000) |

### Fonts (Google Fonts, loaded from CDN in the page head)

- `Public Sans` weights 400, 500, 700 — body, headings, UI labels.
- `Roboto Mono` weights 400, 500 — magnitudes, coordinates, timestamps,
  any tabular numeric.

No other font family is permitted anywhere in the app.

### Theme — government data portal, light

| Token              | Value     | Used for                          |
|--------------------|-----------|-----------------------------------|
| `--bg`             | `#ffffff` | page background                   |
| `--surface`        | `#f7f7f7` | cards, tiles, detail panels       |
| `--sidebar`        | `#f1f3f4` | watch list, filter bar background |
| `--border`         | `#d9d9d9` | every 1px border in the app       |
| `--text`           | `#1a1a1a` | primary text                      |
| `--text-muted`     | `#5f6368` | secondary text, table headers     |
| `--link`           | `#1a73e8` | links, primary buttons            |

Magnitude severity ramp (used for map markers, magnitude badges in tables,
and the big magnitude number on the watch detail):

| Magnitude   | Color     | Label              |
|-------------|-----------|--------------------|
| `< 3`       | `#9aa0a6` | "Micro"            |
| `3.0–3.9`   | `#1a73e8` | "Minor"            |
| `4.0–4.9`   | `#f9a825` | "Light"            |
| `5.0–5.9`   | `#e8731c` | "Moderate"         |
| `6.0–6.9`   | `#c83737` | "Strong"           |
| `>= 7.0`    | `#7c1d6f` | "Major / Great"    |

Visual rules: rectangular tables, 1 px solid `--border` on cells and panels,
no box-shadows anywhere, border-radius is `0` for panels and tables and
`2px` for buttons and inputs only. No gradients. No animations except a
2-second fade-in on the map after data loads.

---

## 3. External API Contracts

### 3.1 USGS Earthquake API

- Base URL: `https://earthquake.usgs.gov/fdsnws/event/1/query`
- Format: `format=geojson` (always).
- Query params used by SeismicLog:
  - `starttime` — ISO 8601 (`YYYY-MM-DDTHH:MM:SS`), UTC.
  - `endtime`   — same format. Optional; default is "now".
  - `minmagnitude` — float, default `2.5` for the feed, `4.0` for risk
    counts (M≥4 spec).
  - `minlatitude`, `maxlatitude`, `minlongitude`, `maxlongitude` — used
    only when computing per-watch risk over a bounding box around the
    address (cheap pre-filter before haversine).
- No API key, no auth header. Set `User-Agent: seismiclog-demo/0.1
  (+https://example.invalid)`.
- Response shape (GeoJSON `FeatureCollection`). Fields parsed per feature:

```json
{
  "id": "us7000abcd",
  "properties": {
    "mag": 5.2,
    "place": "20 km SSW of Town, Country",
    "time": 1715600000000,
    "url":  "https://earthquake.usgs.gov/earthquakes/eventpage/..."
  },
  "geometry": {
    "coordinates": [-122.5, 37.8, 12.3]
  }
}
```

Mapping:

- `properties.mag` → `Event.magnitude` (nullable; skip feature if null).
- `properties.place` → `Event.place` (string, max 200 chars, truncate).
- `properties.time` (milliseconds since epoch UTC) → `Event.occurred_at`
  (`datetime`, UTC).
- `geometry.coordinates[0]` → `Event.lng`.
- `geometry.coordinates[1]` → `Event.lat`.
- `geometry.coordinates[2]` → `Event.depth_km` (nullable; default 10 if
  missing).
- `id` → `Event.usgs_id` (unique).
- `Event.source` = `"usgs"`.

Timeout: 8 seconds connect + read. On any exception (DNS, TLS, HTTP 5xx,
JSON decode, schema mismatch), fall through to offline seed data and log
`usgs.unreachable: <reason>` at WARNING.

### 3.2 Nominatim geocoder

- URL: `https://nominatim.openstreetmap.org/search?q={address}&format=json&limit=1&addressdetails=1`
- Headers: `User-Agent: seismiclog-demo/0.1` (required by Nominatim ToS).
- Politeness: maximum 1 request per second. The app is single-user, so a
  simple `time.sleep(1.0)` between calls is sufficient. No background
  scraping.
- Response (array, take first element). Fields used:

```json
[{
  "lat": "37.7749",
  "lon": "-122.4194",
  "display_name": "San Francisco, California, USA",
  "address": { "country_code": "us", "state": "California", "city": "San Francisco" }
}]
```

Mapping:

- `lat` → `Watch.lat` (float).
- `lon` → `Watch.lng` (float).
- `display_name` is presented in the UI for confirmation but not stored.

Errors: empty array → 422 `{"error": "Address not found.", "field":
"address"}`. Network error → fall through to the offline geocoder, which
contains the three demo addresses (San Francisco, Tokyo, Reykjavik) and
returns 422 for everything else with a hint that geocoding is offline.

### 3.3 Offline / mock-demo mode

A boolean `DEMO_OFFLINE` (env var, default `0`) and an automatic
fall-through both route to seeded data:

- Events: 200 pre-generated events covering the last 30 days, distributed
  across the Pacific Ring of Fire (~55%), Mediterranean / Anatolian fault
  (~15%), Iceland / Mid-Atlantic ridge (~10%), Himalayan front (~10%),
  and scattered intraplate (~10%). Magnitudes drawn from a clipped
  Gutenberg-Richter distribution: 60% in [2.5, 4), 30% in [4, 5), 8% in
  [5, 6), 2% in [6, 7.5]. Depths in [5, 600] km with a heavy left-skew.
- Watches: three pre-geocoded demo addresses (see §9).

When the seed path is used, `Event.source = "seed"` so the table can
optionally indicate "demo data". When the live USGS path succeeds, do
not delete seed events — they coexist; the feed query filters by
`occurred_at` window and not by source.

---

## 4. LLM Contract

### 4.1 Provider priority

1. OpenRouter, if `OPENROUTER_API_KEY` is set. Model from
   `OPENROUTER_MODEL`, default `meta-llama/llama-3.3-70b-instruct:free`.
2. Anthropic, if `ANTHROPIC_API_KEY` is set. Model from `ANTHROPIC_MODEL`,
   default `claude-haiku-4-5`.
3. Local template fallback (always works, no network).

All three are implemented in `app/ai.py` using `urllib.request` only.
Timeout: 20 seconds. On any exception, fall to the next provider.

### 4.2 Prompt template (exact text)

```
Address: {address}
Region: {region}
Last 30 years within 100 km:
  - events M>=4: {n_events_30y}
  - max magnitude observed: {max_mag} ({max_mag_date})
  - dominant depth band: {depth_band}
Soil class (Vs30 inferred): {soil_class}
Estimated probability of M>=5 in next 30 years: {p_m5_30y:.1%}

Write a 2-paragraph seismic risk briefing for the resident.
Paragraph 1: what the historical record actually shows for this address.
Paragraph 2: practical guidance on what this number means and one
preparation action that fits the risk level.
Plain English, no jargon, no bullet lists, no emojis.
```

System prompt (for chat-style APIs): `"You are a calm, factual seismic
hazard analyst writing for a non-expert resident. Two short paragraphs.
Do not invent numbers."`

Decoding: temperature `0.3`, max tokens `400`.

### 4.3 OpenRouter request

- Endpoint: `https://openrouter.ai/api/v1/chat/completions`
- Headers: `Authorization: Bearer $OPENROUTER_API_KEY`,
  `HTTP-Referer: https://example.invalid/seismiclog`,
  `X-Title: SeismicLog`, `Content-Type: application/json`.
- Body: `{"model": ..., "messages": [{"role":"system","content":...},
  {"role":"user","content":...}], "temperature":0.3, "max_tokens":400}`.
- Parse: `choices[0].message.content`.

### 4.4 Anthropic request

- Endpoint: `https://api.anthropic.com/v1/messages`
- Headers: `x-api-key: $ANTHROPIC_API_KEY`,
  `anthropic-version: 2023-06-01`, `Content-Type: application/json`.
- Body: `{"model": ..., "system": "...", "messages":[{"role":"user","content":...}],
  "max_tokens":400, "temperature":0.3}`.
- Parse: `content[0].text`.

### 4.5 Local template fallback

Two-paragraph string built by Python f-string. Three risk tiers picked by
`p_m5_30y`:

- `p < 0.10` — "low" tone, suggested action: "secure tall furniture to wall studs".
- `0.10 <= p < 0.40` — "moderate" tone, suggested action: "keep a 72-hour water + food kit and know the gas shut-off".
- `p >= 0.40` — "elevated" tone, suggested action: "drill `drop, cover, hold` quarterly with the household and confirm the building's seismic retrofit status".

Exact template text in `app/ai.py` (the implementer should keep it
to the same two-paragraph shape so swap-in is invisible).

### 4.6 Caching

`RiskAssessment.llm_summary` and `RiskAssessment.llm_model` are written
once on first generation and re-used until either the user hits
`POST /api/watch/<id>/recompute` or the underlying numeric fields change.
The recompute endpoint always regenerates the summary; the watch detail
endpoint never regenerates if a cached summary exists.

---

## 5. Risk Computation (`app/risk.py`)

### 5.1 Haversine distance

```
R = 6371.0  # km
dlat = radians(lat2 - lat1)
dlng = radians(lng2 - lng1)
a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlng/2)**2
d = 2 * R * asin(sqrt(a))
```

Returns kilometers as `float`. A pre-filter using a square bounding box
of ±1.0° latitude and ±(1.0 / cos(lat)) longitude is applied before the
haversine pass — keeps the per-watch computation fast over ~200k rows.

### 5.2 Per-watch metrics

Given a watch at `(lat0, lng0)`:

- `n_events_30y` — count of `Event` rows where `magnitude >= 4.0`,
  `occurred_at >= now - 30 years`, and `haversine(lat0,lng0,lat,lng) <=
  100.0`. In demo mode the 30-year window collapses to "all seed events"
  since the seed only spans 30 days; this is documented in the UI tile
  subtitle as "based on demo data" when `source = "seed"`.
- `max_magnitude` — the maximum `magnitude` over the same set; `null`
  if the set is empty.
- `max_magnitude_date` — `occurred_at` of the row with `max_magnitude`;
  `null` if empty.
- `dominant_depth_band` — for the same set, classify each event:
  - `shallow` if `depth_km < 30`
  - `intermediate` if `30 <= depth_km <= 300`
  - `deep` if `depth_km > 300`

  Return the band with the highest count; ties broken by `shallow >
  intermediate > deep`. If the set is empty, return `"unknown"`.

### 5.3 Soil class lookup

A built-in dictionary keyed by `(country_code, state_or_region_lower)`
with a fall-back per `country_code` and a global default. Ten entries:

| Region                               | Soil class | Vs30 band (m/s) |
|--------------------------------------|------------|-----------------|
| `us`, `california`                   | `D`        | `180–360`       |
| `us`, `washington`                   | `D`        | `180–360`       |
| `us`, `alaska`                       | `C`        | `360–760`       |
| `jp`, *any*                          | `D`        | `180–360`       |
| `is`, *any*                          | `B`        | `760–1500`      |
| `it`, *any*                          | `C`        | `360–760`       |
| `tr`, *any*                          | `D`        | `180–360`       |
| `cl`, *any*                          | `C`        | `360–760`       |
| `nz`, *any*                          | `D`        | `180–360`       |
| `mx`, *any*                          | `D`        | `180–360`       |
| *fallback*                           | `C`        | `360–760`       |

The label exposed to the UI is `"D — soft soil / Vs30 180–360 m/s"` etc.
The choice is presented as a heuristic — the soil tile subtitle reads
"regional inference; not site-specific".

### 5.4 Probability heuristic

```
p_m5_30y = min(0.95, n_events_M4_30y / 25.0)
```

Disclaimer string surfaced under the probability tile (always shown):

> "Rough heuristic from the local M≥4 rate, not a USGS official forecast.
> Real forecasts require a national seismic hazard model."

Stored as a float in `[0, 0.95]`. The UI formats it as `"{p*100:.0f}%"`.

### 5.5 Region string for the prompt

`Region` is built as `"{address.city or state}, {address.country}"`,
falling back to the raw display name from Nominatim. For seed watches it
is hardcoded (see §9).

---

## 6. Database Schema

SQLite. SQLAlchemy 2.x declarative. All times stored as UTC `datetime`,
all floats double-precision.

```python
class Event(Base):
    __tablename__ = "event"
    id:            Mapped[int]      = mapped_column(primary_key=True)
    usgs_id:       Mapped[str]      = mapped_column(String(64), unique=True, index=True)
    occurred_at:   Mapped[datetime] = mapped_column(index=True)
    lat:           Mapped[float]
    lng:           Mapped[float]
    depth_km:      Mapped[float]    = mapped_column(default=10.0)
    magnitude:     Mapped[float]
    place:         Mapped[str]      = mapped_column(String(200))
    source:        Mapped[str]      = mapped_column(String(8))  # "usgs" | "seed"

class Watch(Base):
    __tablename__ = "watch"
    id:         Mapped[int]      = mapped_column(primary_key=True)
    label:      Mapped[str]      = mapped_column(String(80))
    address:    Mapped[str]      = mapped_column(String(240))
    lat:        Mapped[float]
    lng:        Mapped[float]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(tz=UTC))

class RiskAssessment(Base):
    __tablename__ = "risk_assessment"
    id:                    Mapped[int]      = mapped_column(primary_key=True)
    watch_id:              Mapped[int]      = mapped_column(ForeignKey("watch.id"), unique=True)
    computed_at:           Mapped[datetime] = mapped_column(default=lambda: datetime.now(tz=UTC))
    n_events_30y:          Mapped[int]
    max_magnitude:         Mapped[float | None]
    max_magnitude_date:    Mapped[datetime | None]
    dominant_depth_band:   Mapped[str]      = mapped_column(String(16))
    soil_class:            Mapped[str]      = mapped_column(String(40))
    p_m5_30y:              Mapped[float]
    llm_summary:           Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model:             Mapped[str | None] = mapped_column(String(80), nullable=True)
```

Indexes:

- `event(occurred_at)` — for time-window feed queries.
- `event(lat, lng)` — composite, for the bounding-box pre-filter.
- `event(usgs_id)` — unique, dedupe on refresh.

Migrations: none. The factory calls `Base.metadata.create_all()` on
startup. The DB lives in `seismiclog/data/`, which is git-ignored and
gets seeded once if empty.

---

## 7. API Endpoints

All endpoints return JSON unless noted. Charset UTF-8. Status codes:
200 OK, 201 Created, 204 No Content, 400 Bad Request (schema failure),
404 Not Found, 422 Unprocessable (semantic, e.g. geocoder), 500 Internal.

Error response shape (every error):

```json
{ "error": "Human-readable message.", "field": "address" }
```

`field` is optional and only included for validation errors.

### 7.1 `GET /api/events`

Query params:
- `days` — int in {1, 7, 30}. Default `1`. Maps to `starttime = now - days`.
- `min_mag` — float in `[0, 9]`. Default `2.5`.
- `bbox` — optional, `lat1,lng1,lat2,lng2` (south-west, north-east).

Response:

```json
{
  "count": 142,
  "window_days": 1,
  "min_mag": 2.5,
  "source": "usgs",
  "events": [
    {
      "id": 17,
      "usgs_id": "us7000abcd",
      "occurred_at": "2026-05-14T03:21:09Z",
      "lat": 37.81, "lng": -122.51,
      "depth_km": 12.3,
      "magnitude": 5.2,
      "place": "20 km SSW of Bolinas, CA",
      "severity": "moderate"
    }
  ]
}
```

Order: `occurred_at DESC`. Hard cap 1000 rows.

### 7.2 `GET /api/events/<id>`

`id` is the internal `Event.id`. Returns one event in the shape above
plus a `"usgs_url"` field (computed from `usgs_id`). 404 if not found.

### 7.3 `POST /api/events/refresh`

No body. Pulls the last 24 hours from USGS at `minmagnitude=2.5`,
upserts on `usgs_id`. Response:

```json
{ "fetched": 187, "inserted": 12, "updated": 175, "source": "usgs" }
```

If USGS is unreachable: 200 with `{"fetched": 0, "inserted": 0, "updated":
0, "source": "seed", "note": "USGS unreachable; using seeded data."}`.

### 7.4 `GET /api/watch`

Response:

```json
{
  "count": 3,
  "watches": [
    { "id": 1, "label": "Home", "address": "San Francisco, CA",
      "lat": 37.7749, "lng": -122.4194,
      "created_at": "2026-05-14T03:21:09Z",
      "has_assessment": true }
  ]
}
```

### 7.5 `POST /api/watch`

Request:

```json
{ "label": "Mum's place", "address": "Tokyo, Japan" }
```

Validation: `label` 1–80 chars, `address` 3–240 chars. Server geocodes,
saves the watch, immediately computes the risk assessment (without LLM
summary — that runs lazily on first detail view). Response 201:

```json
{
  "id": 4, "label": "Mum's place", "address": "Tokyo, Japan",
  "lat": 35.6762, "lng": 139.6503,
  "created_at": "2026-05-14T03:21:09Z",
  "has_assessment": true
}
```

Errors:

- 400 if either field is missing or wrong length.
- 422 if geocoder returns no result (`{"error": "Address not found.",
  "field": "address"}`).

### 7.6 `DELETE /api/watch/<id>`

204 on success. 404 if not found. Cascades delete of `RiskAssessment`.

### 7.7 `GET /api/watch/<id>`

Returns the watch with embedded assessment. If assessment exists but
`llm_summary is null`, generates the summary now (lazy) and caches it.

```json
{
  "id": 4, "label": "Mum's place", "address": "Tokyo, Japan",
  "lat": 35.6762, "lng": 139.6503,
  "created_at": "2026-05-14T03:21:09Z",
  "assessment": {
    "computed_at": "2026-05-14T03:21:09Z",
    "n_events_30y": 41,
    "max_magnitude": 6.4,
    "max_magnitude_date": "2024-11-02T17:08:00Z",
    "dominant_depth_band": "intermediate",
    "soil_class": "D — soft soil / Vs30 180–360 m/s",
    "p_m5_30y": 0.95,
    "p_m5_30y_label": "95%",
    "llm_summary": "Two paragraphs ...",
    "llm_model": "meta-llama/llama-3.3-70b-instruct:free"
  }
}
```

### 7.8 `POST /api/watch/<id>/recompute`

No body. Recomputes the numeric assessment AND regenerates the LLM
summary (even if cached). Returns the same shape as 7.7. 404 if not
found.

### 7.9 `GET /api/health`

```json
{ "status": "ok", "version": "0.1", "demo_offline": false,
  "event_count": 1842, "watch_count": 3 }
```

---

## 8. Frontend Pages

Single HTML page rendered by Flask at `GET /`. All interaction is JS;
no full-page reloads after the initial render. The page contains two
top-level tabs, switched by a JS router that swaps a `data-tab` attribute
on `<body>`. Tab state is reflected in the URL hash (`#feed` /
`#watches`).

### 8.1 Top bar

Full width, 56 px tall, surface `--bg`, bottom border 1 px `--border`.

- Left: text wordmark `SeismicLog` in Public Sans 700, plus the muted
  subtitle `Earthquake monitor & address risk`.
- Right: a `Refresh from USGS` button (visible only on Feed tab) and the
  health status dot (green if `/api/health` returned ok, gray otherwise).

Two tab links below the wordmark: `Feed` and `Watches`. Active tab
underlined `2px solid --link`.

### 8.2 Feed tab

Vertical split:

- Top 60 vh: full-width `<div id="map">` with Leaflet map. Default view:
  world (`[20, 0]`, zoom 2). Circle markers for events: radius =
  `4 + (mag - 2.5) * 3` px clamped to `[4, 28]`, fill color from the
  severity ramp, fill-opacity `0.65`, stroke `#1a1a1a` 1 px. Marker
  popup: place, magnitude, depth, occurred-at (local time).
- Bottom 40 vh: table with sticky header.

Filter bar between map and table (44 px tall, `--sidebar` background,
1 px top + bottom border):

- Time range: segmented control `24h` / `7d` / `30d` (default `24h`).
- Min magnitude: range slider from `0.0` to `7.0`, step `0.5`, default
  `2.5`. Live label `M ≥ X.X` in Roboto Mono.
- Spacer.
- Result count, right-aligned: `142 events`.

Table columns (in order):

| Column         | Format                                         | Width |
|----------------|------------------------------------------------|-------|
| Time (UTC)     | `YYYY-MM-DD HH:MM` Roboto Mono                 | 18%   |
| Magnitude      | Colored severity pill + `5.2` in Roboto Mono   | 12%   |
| Depth          | `12.3 km` Roboto Mono                          | 10%   |
| Place          | Plain text                                     | 45%   |
| USGS           | Small text link `view ↗`                       | 15%   |

Interactions:

- Click row → map flies to event coords (`flyTo(zoom=7, duration=0.8s)`).
- Click marker → corresponding table row gets `aria-selected` and a
  1 px `--link` left border; table auto-scrolls it into view.
- `Refresh from USGS` button → `POST /api/events/refresh`, then re-fetch
  `/api/events`. Disabled state with the text `Refreshing…` until
  response returns.

Empty state: `No events match the current filter.` centered in the table
area, `--text-muted`, Public Sans 400 16 px.

Loading state: a 100% width skeleton row with three gray bars, repeated
6 times, animating only opacity (1.5 s cycle).

Error state (network failure): top-of-table banner `Could not reach
USGS. Showing seeded demo data from the last 30 days.`, background
`#fdf3da`, 1 px border `#f9a825`, dismissible.

### 8.3 Watches tab

Horizontal split:

- Left 30%: watch list. Each row: 56 px tall, label (Public Sans 500
  15 px), address (Public Sans 400 13 px `--text-muted`). Active row has
  `--surface` background and a 2 px `--link` left border. Bottom of the
  list: `+ Add watch` button (full width, outlined).
- Right 70%: detail pane.

Detail pane contents (top to bottom):

1. Header: label (Public Sans 700 22 px) and address (`--text-muted`).
   Right-aligned `Delete` link (`--text-muted`, hover `#c83737`).
2. Small Leaflet map `<div id="watch-map">`, 240 px tall, centered on
   the watch with zoom 7. One marker at the watch coords, plus a
   `L.circle` with radius `100_000` meters, `color: #1a73e8`,
   `weight: 1`, `fillOpacity: 0.08`.
3. Four numeric tiles in a 4-column grid (each tile 1 px border,
   `--surface` bg, 16 px padding):

   - `M≥4 events, last 30 y`: big number Roboto Mono 500 28 px, subtitle
     "within 100 km".
   - `Max magnitude`: big number with severity color, subtitle date.
   - `p(M≥5 in 30 y)`: big number as percent, subtitle "rough heuristic;
     not an official USGS forecast".
   - `Soil class`: letter (B/C/D) in 28 px Roboto Mono 500, subtitle
     Vs30 band.

4. LLM briefing block: 2 paragraphs, Public Sans 400 16 px, line-height
   1.55, max-width 70 ch. Below the paragraphs, in `--text-muted` 12 px:
   `Generated by {llm_model} · cached {computed_at relative}`.
5. `Recompute` button, right-aligned, primary style (`--link` background,
   white text, 2 px radius). Disabled with text `Recomputing…` while
   `POST /api/watch/<id>/recompute` is in flight.

Empty pane (no watch selected): centered text `Select a watch on the
left, or add a new one.`

Empty list (no watches at all): the list shows a single dashed-border
card `No watches yet. Add an address you care about.` with the
`+ Add watch` button below.

### 8.4 Add-watch modal

Center-screen modal, 480 px wide, `--bg`, 1 px `--border`, no shadow.
Backdrop `rgba(0,0,0,0.35)`.

- Title `Add watch`.
- Field `Label` (text input, max 80) — placeholder "Home".
- Field `Address` (text input, max 240) — placeholder "1600
  Pennsylvania Ave NW, Washington, DC".
- Hint below the address field, 12 px `--text-muted`: "Geocoded via
  OpenStreetMap Nominatim. Be specific (city + country)."
- Buttons: `Cancel` (text), `Add` (primary).

Errors render inline below the failing field in `#c83737` 13 px.

### 8.5 Disclaimers (always visible)

Footer line at the bottom of every tab, `--text-muted` 12 px, centered:

> "Data: USGS (CC0) and OpenStreetMap (ODbL). Risk numbers are
> heuristic and not a substitute for an official seismic hazard
> assessment."

---

## 9. Seed Data

### 9.1 Events (200 rows)

Generator in `app/seed.py`. Deterministic — seeded with `random.seed(7)`
so reruns produce identical data.

Cluster centers (lat, lng, radius_deg, weight):

| Region                  | Lat, Lng       | Radius (deg) | Weight |
|-------------------------|----------------|--------------|--------|
| Cascadia                | 47.0, -122.5   | 4            | 0.10   |
| California              | 36.0, -120.0   | 4            | 0.10   |
| Aleutians / Alaska      | 56.0, -158.0   | 8            | 0.10   |
| Japan trench            | 37.0, 142.0    | 5            | 0.12   |
| Philippines / Indonesia | -2.0, 125.0    | 10           | 0.13   |
| Chile / Peru            | -23.0, -70.0   | 8            | 0.10   |
| Mediterranean           | 38.0, 22.0     | 6            | 0.08   |
| Turkey / Anatolian      | 39.0, 37.0     | 5            | 0.07   |
| Iceland / MAR           | 64.5, -19.0    | 3            | 0.10   |
| Himalayan front         | 28.0, 85.0     | 6            | 0.10   |

For each event: pick a cluster by weight, sample `(lat, lng)` uniformly
inside the radius, sample magnitude from the clipped GR (see §3.3),
sample depth from `lognormal(mean=ln 25, sigma=1.0)` clipped to
`[5, 600]`, sample `occurred_at` uniformly over the last 30 days.
`usgs_id` = `"seed-" + uuid4().hex[:12]`, `place` = a short generated
string `"NN km <dir> of <region>"`, `source = "seed"`.

### 9.2 Demo watches (3 rows)

Inserted on first start if `Watch` is empty. Each one is geocoded
locally (no Nominatim call) and gets a pre-computed `RiskAssessment`
with `llm_summary = null` (so the first detail view triggers the LLM).

| Label      | Address                       | Lat       | Lng        | Region for prompt           | Soil |
|------------|-------------------------------|-----------|------------|-----------------------------|------|
| Home       | San Francisco, California, US | 37.7749   | -122.4194  | "San Francisco, USA"        | D    |
| Mum's      | Tokyo, Japan                  | 35.6762   | 139.6503   | "Tokyo, Japan"              | D    |
| Cabin      | Reykjavik, Iceland            | 64.1466   | -21.9426   | "Reykjavik, Iceland"        | B    |

Pre-computed risk numbers, anchored to the seed (the implementer should
verify these match the computation against the seeded events; if not,
recompute and update):

| Label      | n_events_30y | max_mag | depth_band   | p_m5_30y |
|------------|--------------|---------|--------------|----------|
| Home       | 6            | 5.4     | shallow      | 0.24     |
| Mum's      | 24           | 6.4     | intermediate | 0.95     |
| Cabin      | 18           | 5.1     | shallow      | 0.72     |

---

## 10. File Layout

```
seismiclog/
  app/
    __init__.py        Flask factory (create_app), DB init, seed-if-empty
    routes.py          all routes registered to a single Blueprint "api"
                       plus the GET / index route
    schemas.py         marshmallow request/response schemas
    models.py          SQLAlchemy ORM classes (§6)
    usgs.py            USGS client + offline fallback
    geocode.py         Nominatim wrapper + soil lookup table
    risk.py            haversine, GR heuristic, assessment builder,
                       LLM-briefing orchestration
    ai.py              OpenRouter / Anthropic / template fallback,
                       urllib only
    seed.py            200 events + 3 demo watches generator
  wsgi.py              gunicorn entry: `from app import create_app;
                       app = create_app()`
  requirements.txt     Flask==3.*, SQLAlchemy==2.*, marshmallow==3.*,
                       gunicorn==22.*
  static/
    styles.css         palette tokens + government-portal layout
    app.js             vanilla JS, Leaflet wiring, fetch helpers,
                       tab router, modal
  templates/
    index.html         single page; <link rel="stylesheet"> to Public
                       Sans + Roboto Mono + Leaflet CSS; <script> to
                       Leaflet JS + app.js
  data/                SQLite volume (gitignored)
  Dockerfile           multi-stage, python:3.12-slim, non-root quake,
                       gunicorn on 8002
  docker-compose.yml   port 8002, env_file .env, volume ./data:/app/data
  .env.example         OPENROUTER_API_KEY=, OPENROUTER_MODEL=...,
                       ANTHROPIC_API_KEY=, ANTHROPIC_MODEL=,
                       DEMO_OFFLINE=0, FLASK_ENV=production
  .gitignore           data/, .env, __pycache__/, *.pyc
  SPEC.md              this file
  README.md            (see §13)
```

---

## 11. Dev Workflow

### 11.1 Without Docker

Host needs Python 3.12. From `seismiclog/`:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app wsgi run --port 8002 --debug
```

The factory creates the DB and seeds it on first run. `DEMO_OFFLINE=1`
in `.env` forces the seed-only path even when the network is up.

For a production-style run without Docker:

```sh
gunicorn -w 2 -b 0.0.0.0:8002 wsgi:app
```

### 11.2 With docker-compose

From `seismiclog/`:

```sh
docker compose up --build
docker compose up -d
docker compose down
```

The compose file mounts `./data` so the SQLite file survives container
rebuilds. Reset by removing the directory.

---

## 12. Environment Variables

| Variable             | Default                                      | Purpose                                          |
|----------------------|----------------------------------------------|--------------------------------------------------|
| `OPENROUTER_API_KEY` | *(unset)*                                    | Enables OpenRouter as the primary LLM provider.  |
| `OPENROUTER_MODEL`   | `meta-llama/llama-3.3-70b-instruct:free`     | Model id for OpenRouter.                         |
| `ANTHROPIC_API_KEY`  | *(unset)*                                    | Enables Anthropic as the secondary LLM provider. |
| `ANTHROPIC_MODEL`    | `claude-haiku-4-5`                           | Model id for Anthropic.                          |
| `DEMO_OFFLINE`       | `0`                                          | `1` to skip all external calls and use seed + template. |
| `DATABASE_URL`       | `sqlite:////app/data/seismiclog.db`          | Overridable for tests.                           |
| `FLASK_ENV`          | `production`                                 | `development` enables Flask debugger.            |
| `PORT`               | `8002`                                       | Gunicorn bind port.                              |

`.env.example` ships with every variable listed and commented; `.env`
is git-ignored.

---

## 13. README Outline

One line per section. The README tone is dry, declarative, USGS-portal:
no exclamation marks, no marketing copy, no first-person plural.

1. `# SeismicLog` — short tagline: "Earthquake monitor with per-address
   long-term risk briefings."
2. `## What it does` — three bullet points: live feed, watch addresses,
   LLM-generated briefing.
3. `## Stack` — single-paragraph stack summary.
4. `## Run with Docker` — one fenced block.
5. `## Run without Docker` — one fenced block (venv + flask).
6. `## Environment` — table of env vars (subset of §12).
7. `## AI provider priority` — three-line list (OpenRouter, Anthropic,
   template).
8. `## Data sources` — credits to USGS (CC0) and OpenStreetMap (ODbL).
9. `## Disclaimer` — the heuristic-not-a-forecast paragraph from §5.4.
10. `## License` — points to repo-root LICENSE.

---

## 14. Out of Scope

- No user accounts, no auth, no sessions, no CSRF. Single-user demo.
- No email, SMS, push, or webhook notifications. The watch detail view
  is the only way to read a briefing.
- No historical bulk import beyond the 30-day USGS window. If a user
  wants longer history, that is a roadmap item, not v0.1.
- No offline raster tile pack for Leaflet. The map requires internet
  for tiles. If tiles fail to load, the map shows a gray background and
  markers still appear over it.
- No real Vs30 / NSHM dataset. Soil class is a regional lookup; the
  probability is a heuristic; both are flagged in the UI.
- No internationalisation. UI strings are English-only.
- No analytics, no telemetry, no third-party fonts beyond Google Fonts
  loaded over HTTPS, no third-party JS beyond Leaflet from unpkg.
- No tests in v0.1 beyond a `pytest` smoke that boots the app and hits
  `/api/health`. (Implementer may add more, but it is not required.)
