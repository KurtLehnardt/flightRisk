# Grafana observability stack

Traces, metrics and logs from the FlightRisk app, with a ready-made **FlightRisk** dashboard.

```
app --OTLP--> otel-collector --> Tempo       (traces)
                             --> Prometheus  (metrics)
                             --> Loki        (logs)
                                      \--> Grafana  http://localhost:3000
```

## Start it

```bash
docker compose -f docker-compose.grafana.yml up -d
```

Then run the app with OpenTelemetry pointed at the collector.

PowerShell (Windows):

```powershell
$env:FLIGHTRISK_OTEL_ENABLED = "true"
$env:OTEL_EXPORTER = "otlp-grpc"
$env:OTEL_ENDPOINT = "http://localhost:4317"
venv\Scripts\python.exe -m flightrisk --dashboard --target photo_of_child.jpg
```

bash (Linux/macOS):

```bash
FLIGHTRISK_OTEL_ENABLED=true OTEL_EXPORTER=otlp-grpc OTEL_ENDPOINT=http://localhost:4317 \
  python -m flightrisk --dashboard --target photo_of_child.jpg
```

Open <http://localhost:3000> (user `admin`, password `$GRAFANA_ADMIN_PASSWORD`, default `admin`).
The **FlightRisk** dashboard is the home page and lives in the *FlightRisk* folder.

If the app itself runs in Docker, set `OTEL_ENDPOINT=http://host.docker.internal:4317`
(or put it on the same network and use `http://otel-collector:4317`).

The collector uses host ports 4317/4318, the same as Jaeger in
`docker-compose.otel.yml` and in `docker-compose.yml`. Run one or the other.

## Two laptops on the Thunderbolt cable

One stack on the ground station collects from both laptops, and one Grafana shows both.

| Laptop | Role | Cable address | `service.instance.id` |
| --- | --- | --- | --- |
| MacBook (runs this stack) | ground station | `10.42.0.1` (`bridge0`) | `mac-ground` |
| HP ZBook (PLEXBOX) | edge-2 | `10.42.0.2` | `edge-2` |

The OTLP ports (4317 gRPC, 4318 HTTP) listen on the cable address only, so the
ZBook can reach them and nothing on Wi-Fi can. Grafana, Prometheus, Tempo and Loki
stay on `127.0.0.1`.

Start the stack on the Mac:

```bash
OTLP_BIND=10.42.0.1 docker compose -f docker-compose.grafana.yml up -d
# if port 3000 is already taken (e.g. a Homebrew Grafana), also set GRAFANA_PORT=3001
```

Check that the collector is on the cable and not on Wi-Fi:

```bash
lsof -nP -iTCP:4317 -sTCP:LISTEN   # expect 10.42.0.1:4317
nc -z -G 2 10.42.0.1 4317          # succeeds
nc -z -G 2 <Mac Wi-Fi IP> 4317     # must fail
```

Never use `OTLP_BIND=0.0.0.0`. Without `OTLP_BIND` the ports stay on `127.0.0.1`.

**Colima.** Colima (and Lima) forward container ports from inside their VM, so a host
address such as `10.42.0.1` cannot be bound there: `up` fails with
`failed to bind host port 10.42.0.1:4317/tcp: cannot assign requested address` and the
collector does not start. That fails closed (nothing is exposed). With Colima, run the
stack without `OTLP_BIND`; the Mac's own processes can still export to `127.0.0.1:4317`,
but the ZBook cannot reach the collector until the port is forwarded to the cable address
by some other means.

Each process names its node with the standard `OTEL_RESOURCE_ATTRIBUTES` variable.

Mac (ground station; use `http://127.0.0.1:4317` if the collector is on loopback only):

```bash
FLIGHTRISK_OTEL_ENABLED=true OTEL_EXPORTER=otlp-grpc OTEL_ENDPOINT=http://10.42.0.1:4317 \
OTEL_RESOURCE_ATTRIBUTES=service.instance.id=mac-ground \
  python -m flightrisk.dashboard --source edge ...
```

ZBook (edge-2), PowerShell from the repo root:

```powershell
$env:FLIGHTRISK_OTEL_ENABLED = "true"; $env:OTEL_EXPORTER = "otlp-grpc"; $env:OTEL_ENDPOINT = "http://10.42.0.1:4317"; $env:OTEL_RESOURCE_ATTRIBUTES = "service.instance.id=edge-2"
.\scripts\demo_edge.ps1
```

The edge process (`python -m flightrisk.edge_device`) exports its logs the same way the
dashboard does; it records no metrics or spans today, so edge-2 shows up in the log panels
only. In Grafana, pick nodes with the **Node** drop-down at the top of the FlightRisk
dashboard (multi-select, *All* by default). A process that does not set
`service.instance.id` appears as `unknown`.

The cable path does not use Wi-Fi, so exporting keeps working while the ZBook's Wi-Fi is
joined to a Tello. If the collector is unreachable (cable out, stack stopped), telemetry is
dropped: export runs on background threads with a 3 s timeout, the frame loop and the
edge's send path are not slowed, and the flush at exit gives up after 3 s.

Firewall: the macOS application firewall may ask whether to accept incoming connections
for the process that owns port 4317 (Docker's port forwarder). Allow it, or exports from
the ZBook time out and are dropped. The ZBook only makes outgoing connections.

Stop the stack:

```bash
docker compose -f docker-compose.grafana.yml down       # keeps the data
docker compose -f docker-compose.grafana.yml down -v    # also wipes metrics, logs and traces
```

## What the dashboard shows

| Section | Panels |
| --- | --- |
| Overview | FPS, drone battery, p95 frame latency, face detection rate, matches, errors |
| Pipeline performance | FPS, frame latency p50/p95/p99, persons detected per frame |
| Matching | matches by type, match-score distribution, face detection rate |
| Drone & health | battery (warning line at 20%), errors by component/type |
| Logs | log volume by level, warnings and errors, all logs |
| Traces | recent traces from Tempo (`detect`, `reid_match`, `face_match` spans) |

## Metrics

Names are the OTel names from `flightrisk/telemetry.py` with dots turned into underscores
(the collector runs with `add_metric_suffixes: false`, so there is no `_total` or unit suffix).

| Prometheus name | Type |
| --- | --- |
| `flightrisk_fps` | gauge |
| `flightrisk_drone_battery` | gauge (%) |
| `flightrisk_face_detection_rate` | gauge (%) |
| `flightrisk_frame_duration_ms_{bucket,sum,count}` | histogram (ms) |
| `flightrisk_detections_count` | running total of persons seen (gauge) |
| `flightrisk_match_count{match_type}` | counter |
| `flightrisk_match_score_{bucket,sum,count}{match_type}` | histogram, buckets 0.1 to 1.0 |
| `flightrisk_errors_count{component,error_type}` | counter |

`flightrisk_reasoning_duration_ms` is defined but nothing records it today, so the dashboard has no panel for it.

## Logs

`StructuredLogger` output still goes to stdout. In the `otlp-*` modes it is also sent to Loki as
OTLP logs. Each line is the same JSON the app prints, so use `| json` in LogQL:

```logql
{service_name="flightrisk"} | json | event="detection_tick"
{service_name="flightrisk"} | json | level="error"
{service_name="flightrisk", service_instance_id="edge-2"}
```

## Editing the dashboard

`grafana/dashboards/flightrisk.json` is provisioned read-only from disk but `allowUiUpdates`
is on: edit in Grafana, then use *Share > Export > Save to file* and replace the JSON to keep changes.

## Notes

- All published ports bind to `127.0.0.1`. Do not expose them; logs and traces describe an active search.
  The one exception is opt-in: `OTLP_BIND=10.42.0.1` puts the OTLP ports on the Thunderbolt cable address.
  Change the Grafana admin password (`GRAFANA_ADMIN_PASSWORD`) before using this anywhere shared.
- Image versions are pinned in `docker-compose.grafana.yml`. Retention is 7 days for metrics, logs and traces.
- Data lives in named Docker volumes. `docker compose -f docker-compose.grafana.yml down -v` wipes it.
