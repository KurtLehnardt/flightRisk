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
```

## Editing the dashboard

`grafana/dashboards/flightrisk.json` is provisioned read-only from disk but `allowUiUpdates`
is on: edit in Grafana, then use *Share > Export > Save to file* and replace the JSON to keep changes.

## Notes

- All published ports bind to `127.0.0.1`. Do not expose them; logs and traces describe an active search.
  Change the Grafana admin password (`GRAFANA_ADMIN_PASSWORD`) before using this anywhere shared.
- Image versions are pinned in `docker-compose.grafana.yml`. Retention is 7 days for metrics, logs and traces.
- Data lives in named Docker volumes. `docker compose -f docker-compose.grafana.yml down -v` wipes it.
