# Map and flight plan editing

This document describes how the operator edits the fleet and single-drone
search plans on the dashboard map, how an edited path becomes moves the drones
can really fly, which safety checks every edited plan passes before Start, and
how map imagery is shown online and cached for offline use in the field.

It is both the design and the operator guide. Section 9 lists what is not
verified.

## 1. Goals

- Pick a search pattern, then edit the search area by dragging and resizing it
  on the map or by typing numbers, in the relative grid view (centimetres) or
  in the lat/lon view (an anchor point and a compass heading). An edit in one
  view is shown in the other.
- After the pattern generates each drone's path, drag the path's vertices, add
  a vertex by clicking a segment, delete a vertex, or revert a drone to the
  generated pattern. Each drone keeps its own path: editing one never changes
  another.
- Show the plan over satellite imagery, a topographic map, a street map, or no
  imagery, both with internet access and offline (the Mac has no internet
  while it is joined to a Tello's Wi-Fi network).
- Never weaken safety: every edited plan goes through the same checks as a
  generated plan, plus a new drone-to-drone separation check, and Start keeps
  its confirmation and refusal rules.

## 2. Frames and the anchor

### 2.1 The plan frame

Plans are made in the *plan frame*, in whole centimetres. `x` grows to the
right of the takeoff heading and `y` grows forward along it. This is the frame
the fleet coordinator already uses (`flightrisk/drone/coordinator.py`) and the
frame each edge dead-reckons in (`flightrisk/drone/safety.py`): a lane plan only
contains `forward`, `back`, `left` and `right` moves, and nothing rotates.

- In the fleet, every drone takes off at the first vertex of its path. For a
  generated plan that is the near-left corner of its zone, as before. The
  coordinator now records that takeoff point explicitly in the zone
  (`takeoff_x_cm`, `takeoff_y_cm`), which the fleet protocol already allows,
  and adds it to the drone's reported relative position.
- In the single-drone flow (`start_search` on a drone connected directly to
  the dashboard), the origin is where the drone hovers when Start pattern is
  pressed.

### 2.2 The anchor

A standard Tello has no GPS. Its position is dead-reckoned in centimetres from
the point it took off from, so a map in latitude and longitude needs an
*anchor*: the latitude, longitude and compass heading of the plan frame's
origin. The heading is the compass direction of the plan's `+y` (forward)
axis, which is the direction the drones' noses point at takeoff.

The operator sets the anchor in the Search plan card in one of three ways:

1. Type the latitude, longitude and heading.
2. Press **Pick anchor on map** and click the takeoff point on the map.
3. For a drone that reports GPS (a MAVLink drone), press **Use drone GPS**, which
   copies the drone's reported latitude, longitude and heading.

The dashboard converts centimetres to latitude and longitude with the same
flat-earth formula as `flightrisk/drone/geo.py` (accurate to well under a
metre over a few kilometres):

```
east_m  = (x_cm * cos(h) + y_cm * sin(h)) / 100
north_m = (y_cm * cos(h) - x_cm * sin(h)) / 100
lat     = lat0 + degrees(north_m / R)
lon     = lon0 + degrees(east_m / (R * cos(lat0)))
```

where `h` is the anchor heading and `R` is the mean Earth radius. The inverse
is used when the operator drags something in the lat/lon view.

Positions of Tellos shown in the lat/lon view are dead-reckoned and only as
good as the anchor and the drift of each move. The map says so in a label
whenever it shows them. A drone that reports its own latitude and longitude
(MAVLink) is drawn at its GPS position instead and labelled GPS.

The anchor is never used by any safety check. The checks are all in
centimetres in the plan frame, so a wrong anchor moves the picture, never the
drones.

## 3. Patterns and editing

### 3.1 Patterns

The pattern list is the one the single-drone selector already offered:
parallel track (lawnmower), expanding square, track line, sector and spiral.
For the fleet, the search area is split into side-by-side zones separated by
the zone gap (`gap_cm`, 100 cm by default), exactly as the coordinator splits
it today, and each zone gets the pattern fitted to its rectangle:

- **Parallel track** is the coordinator's existing pattern: strips `strip_cm`
  apart from the zone's near-left corner. An unedited parallel-track plan has
  the same waypoints as the plan the Plan button made before this change.
- **Expanding square** flies from the corner to the zone centre, then an
  axis-aligned square spiral outward with legs growing by `strip_cm`, stopping
  before it would leave the zone.
- **Track line** flies to the zone's centre line, then forward along it with
  lateral sweeps of up to `strip_cm` to each side.
- **Sector** and **spiral** are defined with turns of 60 and 45 degrees. A Tello
  in a fleet never turns (its geofence and position depend on a fixed
  heading), so their slanted legs are snapped into axis-aligned staircases
  (section 4). The map shows the staircase, which is what will fly.

### 3.2 Editing the area

The area is the whole search rectangle (`x_cm`, `y_cm`, `w_cm`, `h_cm`). The
operator can drag its centre handle to move it, drag its far corner handle to
resize it, or type the four numbers. In the lat/lon view, the area's centre
latitude and longitude can be typed as well, and the corner positions are
shown. Changing the area or the pattern regenerates every drone's path that
the operator has not edited. A drone whose path was edited keeps it, and the
card says so; **Revert path** applies the current pattern to it again.

### 3.3 Editing a path

Each drone's path is drawn in its colour with a start marker, direction
arrows, and a handle on every vertex.

- Drag a vertex to move it. Dragging the first vertex moves that drone's
  takeoff spot, which is also the centre of its geofence.
- Click a segment to add a vertex there.
- Double-click a vertex (or right-click it) to delete it. A path keeps at
  least two vertices.
- **Revert path** restores the generated pattern for the selected drone, or for
  every drone when none is selected.

The drawn path is shown thin and dashed. The path that will actually fly (the
snapped path, section 4) is shown solid on top of it. Every edit is checked by
the dashboard server as the operator works (`preview_plan`), and the card
lists each problem with the drone, the vertex and the place.

## 4. From an edited path to executable lanes

The edge lane executor (`flightrisk/drone/lane_plan.py`,
`flightrisk/drone/lane_executor.py`) accepts only horizontal moves along the
takeoff axes: `forward`, `back`, `left` and `right`, each a whole number of
centimetres between 20 and 500. Lanes never rotate the drone and never change
its altitude. `rotate_degrees` exists only in the legacy single-drone
generators.

So a drawn path is converted as follows (`flightrisk/drone/plan_paths.py`,
mirrored for live display in `static/ui/plan_geom.js`):

1. Every drawn vertex is rounded to a whole centimetre.
2. A segment along one axis becomes one leg.
3. A slanted segment from `(0, 0)` to `(dx, dy)` becomes a staircase of `k`
   steps. Each step moves along the axis with the larger component first and
   then along the other one, and the step lengths add up exactly to `dx` and
   `dy`. `k` is the smallest number of steps that keeps each step's corner
   within 25 cm of the drawn segment, limited so that every step is at least
   the 20 cm minimum move on both axes. A segment whose smaller component is
   below 20 cm becomes an L-shape.
4. Each leg is split into equal moves of at most 500 cm. A leg shorter than
   20 cm is flown as an overshoot pair (20 cm past the end and back, or 20 cm
   back first and then through), choosing the variant that stays inside the
   geofence.

Guarantees, covered by tests: the snapped path passes exactly through every
drawn vertex, and no point of it is 40 cm or more from the drawn path
(`SNAP_TOLERANCE_CM`). The tests fly the converted lanes on the fake drone
through the real lane executor and compare the dead-reckoned trace with the
drawn path.

Edited plans reach the edges through the existing `assign_lanes` message
(`lanes`, `alt_cm` and `zone`). Neither the fleet protocol nor the edge
changes. In the single-drone flow, the same lanes are flown as relative moves
through the existing pattern loop, without any rotation, so the drone must
face the plan's forward direction when Start pattern is pressed.

## 5. Safety checks

Every plan built from the map, generated or edited, goes through all of these
on the dashboard server before anything is sent to a drone. A plan with any
problem is refused and nothing is sent.

1. **Geofence.** Every point of the snapped path, including the extra point of
   an overshoot pair, must be inside the square of half-width `geofence_cm`
   around that drone's takeoff point. The value is `FLIGHTRISK_GEOFENCE_CM`, the
   value the edges enforce (450 cm when started from the launcher). A path that
   leaves the fence is refused, and the vertex that ends the offending segment
   is highlighted on the map. Each edge still checks the fence again itself.
2. **Ceiling and altitude slots.** Each drone keeps the altitude slot from the
   ceiling settings (`docs/ceiling-and-altitude.md`). Plan is refused when a
   slot is above the ceiling limit and Start when the slots no longer match, as
   before. Lanes never change altitude.
3. **The fleet plan check.** `fleet_plan_check.check_plan` runs on the plan:
   every lane is a 20-500 cm horizontal move, a drone has at most the
   protocol's lane cap, every `assign_lanes` message is valid and fits the
   protocol's size cap, and every altitude slot is in range. Its region-based
   gap rule is replaced by the path-based separation check below, and its
   camera coverage gate is not applied (coverage is reported instead, see 5.5).
4. **Drone-to-drone separation (new).** For a plan with two or more drones, the
   snapped paths of every pair of drones, takeoff points included, must stay at
   least `gap_cm` apart horizontally (100 cm by default) at every point,
   whatever the timing. Altitude slots are only about 30 cm apart, so they are
   never relied on alone. A violation is refused with both drone ids and the
   place where the paths come closest.
5. **Coverage, reported honestly.** The plan's `uncovered` list gains one entry
   (`kind: "area"`) per rectangle of the search area that no drone's camera
   sees, and `coverage` gives the covered fraction. The camera is modelled as a
   square of side `strip_cm` centred under the drone, the same assumption that
   spaces the generated lawnmower lanes `strip_cm` apart. It is measured on a
   grid of cells of at least 10 cm. Coverage is shown, never a reason to
   refuse, because the real forward camera footprint depends on altitude and
   the operator decides whether a gap is acceptable.

Plan and Start keep their rules. Start needs a valid plan, a confirmation, and
every planned drone connected and acknowledged. A plan edited after Plan was
pressed is invalidated: the dashboard disables Start at once, tells the server
(`fleet_plan_edited`), and the server refuses Start until Plan is pressed again.
LAND ALL, manual takeover and the landing paths are unchanged.

## 6. Map imagery

### 6.1 Layers

In the lat/lon view, a layer switch in the map's corner offers:

| Layer | Provider | Online | Offline cache |
| --- | --- | --- | --- |
| Satellite | USGS The National Map, USGSImageryOnly | yes | yes |
| Topo map | USGS The National Map, USGSTopo | yes | yes |
| Street map | OpenStreetMap standard tiles | yes | no |
| None | (plan only) | yes | yes |

The required attribution of the visible layer is shown in the map's corner.

### 6.2 Why these providers

The terms were checked on 2026-10-29.

- **USGS The National Map** (`basemap.nationalmap.gov`). USGS data is in the
  U.S. public domain and USGS asks for credit as "U.S. Geological Survey"
  (https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits).
  No bulk-download prohibition is published, so a small offline cache is
  acceptable. The imagery is NAIP orthophotography at 0.6 to 1 m, which covers
  Utah well. It covers the United States only. The tile URL order is
  `{z}/{y}/{x}`.
- **OpenStreetMap standard tiles** (`tile.openstreetmap.org`). The tile usage
  policy (https://operations.osmfoundation.org/policies/tiles/) allows normal
  interactive viewing with the attribution "© OpenStreetMap contributors" and a
  valid Referer, but forbids bulk downloading, defined as fetching tiles ahead
  of viewing them, and names "download for offline use" as an example. The
  street map is therefore online only, and the cache tool refuses it.
- **Esri World Imagery** was not included. Its item page says the layer is
  licensed under the Esri Master License Agreement and "is not intended to be
  used to export tiles for offline" use; the export variant needs an ArcGIS
  account. The USGS imagery covers the same need in the United States without
  those conditions.
- **OpenTopoMap** and **EOX Sentinel-2 cloudless** were not included. OpenTopoMap
  has no explicit offline rule and asks not to be strained by mass downloads,
  and the current Sentinel-2 cloudless licence is non-commercial
  (CC BY-NC-SA 4.0), which fits poorly with a GPL tool anyone may use.

No provider here needs an API key. The cache tool supports a key for a
self-hosted or licensed server through an environment variable named on the
command line (`--key-env`); the key is substituted only into the request and
is never printed or written.

### 6.3 Online mode

In the browser, a cacheable layer for which an area has been cached first asks
the dashboard for each tile (`/tiles/<provider>/<z>/<x>/<y>`). When the
dashboard has no copy of that tile, it answers 404, and the browser loads the
tile straight from the provider if the computer is online. A layer with no
cached area loads from the provider directly. A tile that is neither cached nor
reachable is drawn blank, so the map keeps working offline. The street map
loads only from OpenStreetMap.

The dashboard never proxies or fetches tiles itself, so it makes no outgoing
requests and opens no new endpoint to other origins. The dashboard sets no
Content-Security-Policy today, so remote images need no CSP change; if one is
added later, its `img-src` must list exactly `https://basemap.nationalmap.gov`
and `https://tile.openstreetmap.org`.

### 6.4 Offline workflow

Before going to the field, on a computer with internet access:

```
venv/bin/python scripts/cache_map_tiles.py --provider usgs-imagery \
    --center 40.6894,-111.7310 --radius-m 300 --min-zoom 14 --max-zoom 16
venv/bin/python scripts/cache_map_tiles.py --provider usgs-topo \
    --center 40.6894,-111.7310 --radius-m 300 --min-zoom 14 --max-zoom 16
```

A check against the real service on 2026-10-29 found USGS imagery tiles at
zoom 14 to 16 and none at zoom 17 or above near the Utah demo site, so zoom 16
is the useful maximum there. Both layers return JPEG tiles. The map can zoom
in further (to zoom 23, about 1.4 cm per pixel, because a Tello search area is
only a few metres wide) and simply enlarges the zoom-16 imagery.

`--bbox SOUTH,WEST,NORTH,EAST` can replace `--center` and `--radius-m`, and
`--dry-run` prints the number of tiles without downloading anything. Tiles are
stored under `~/.flightrisk/tiles/<provider>/<z>/<x>/<y>.<ext>` (or
`FLIGHTRISK_TILES_DIR`, or `--out`), together with a `manifest.json` that
records each cached area and its attribution. A second run skips tiles that are
already present.

The tool is deliberately small and polite:

- It refuses providers whose terms forbid caching (OpenStreetMap, Esri,
  OpenTopoMap) even when given as a URL template.
- It refuses more than 2,000 tiles, an area more than 4 km wide or deep (a
  2 km radius) or a zoom above 19 unless `--allow-large` is given, and never
  allows more than 20,000 tiles or a zoom above 20. It also refuses to follow a
  redirect to a provider whose terms forbid caching.
- It downloads one tile at a time, at most 2 per second by default
  (`--rate`), with a descriptive User-Agent, and backs off on HTTP 429 and 5xx
  answers (honouring `Retry-After`).
- It accepts only image responses of at most 2 MB.

On the field computer, start the dashboard as usual. The lat/lon view shows,
for the visible layer and view, how much of it is cached ("Offline: 100% of
this view cached"). The dashboard serves cached tiles at `/tiles/...` with
`Cache-Control: public, max-age=86400`, validates the provider name against
its list and the tile numbers as integers in range, and resolves every path
inside the tile directory, so no other file can be read. The route is behind
the same API-key gate as every other route.

## 7. Saved plans

**Save plan** stores the current plan as JSON in
`~/.flightrisk/plans/<name>.json` (or `FLIGHTRISK_PLANS_DIR`): the plan spec
(pattern, area, `strip_cm` and every edited path), the anchor and the map view
(mode, centre, zoom and layer). Names are limited to letters, digits, spaces,
dots, dashes and underscores. A saved plan holds no secret and no drone
address. **Load** restores it; Plan must still be pressed, and every check runs
again on the loaded plan.

## 8. Socket events

| Event | Direction | Purpose |
| --- | --- | --- |
| `get_map_config` / `map_config` | browser to server / reply | mode (fleet or single), drone ids, defaults, geofence, gap, providers and cache status |
| `preview_plan` / `plan_preview` | browser to server / reply | build and check a spec without sending anything |
| `fleet_plan_edited` | browser to server | invalidates the current plan; Start is refused until Plan |
| `plan_fleet` | browser to server | with a `plan` spec: check it and send its lanes; without one: as before |
| `start_search` | browser to server | with a `plan` spec: fly the checked lanes; without one: as before |
| `map_cache_status` | browser to server / reply | how much of a view is cached for a layer |
| `save_plan`, `list_plans`, `load_plan` | browser to server / reply | saved plans |

## 9. Limitations and what is not verified

- No edited path has been flown on a real Tello. The conversion is verified on
  the fake drone through the real lane executor only.
- Tello positions on the map are dead-reckoned and drift; the anchor is only as
  accurate as the operator's placement and heading.
- Sector and spiral patterns are flown as staircases, so they take more moves
  than the drawn path suggests.
- Coverage uses a square camera model of `strip_cm`; it is not the forward
  camera model of `fleet_plan_check`.
- USGS imagery covers the United States only. Outside it, the satellite and
  topo layers stay blank and only the online street map works.
- When `FLIGHTRISK_API_KEY` is set, the browser's tile requests carry no
  Authorization header, so cached tiles are refused like every other route.
- The map was tested in headless Chrome only. Offline use with a real provider
  and GPS (MAVLink) drones were not tested.
