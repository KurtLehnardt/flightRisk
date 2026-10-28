# Grafana observability stack

Traces, metrics and logs from the FlightRisk app, with five provisioned dashboards, alert
rules, and links between the three signals. The event names, fields and metrics are listed
in [`docs/observability-events.md`](../docs/observability-events.md).

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

To also see what the operator typed and the target image in Grafana, add
`FLIGHTRISK_TELEMETRY_CONTENT=1` (see "Operator content and the target image" below; it is
off unless you set it).

Open <http://localhost:3000> (user `admin`, password `$GRAFANA_ADMIN_PASSWORD`, default `admin`).
**FlightRisk / Overview** is the home page; all dashboards live in the *FlightRisk* folder.

If the app itself runs in Docker, set `OTEL_ENDPOINT=http://host.docker.internal:4317`
(or put it on the same network and use `http://otel-collector:4317`).

The collector uses host ports 4317/4318, the same as Jaeger in
`docker-compose.otel.yml` and in `docker-compose.yml`. Run one or the other. All three
files publish those ports (and Jaeger's UI on 16686) on `127.0.0.1` unless `OTLP_BIND` is
set.

If the collector cannot start (for example `OTLP_BIND` names an address this machine does
not have), Prometheus, Tempo, Loki and Grafana still start, because they do not depend on
it; only OTLP ingest is missing.

On the Windows edge laptop, `scripts\install_windows.ps1` installs
`requirements-edge.txt`, which includes the OpenTelemetry packages. If
`FLIGHTRISK_OTEL_ENABLED` is on but they are missing, the edge logs a warning
(`opentelemetry_unavailable`) and exports nothing.

## Two laptops on the Thunderbolt cable

**Limitation: `OTLP_BIND=10.42.0.1` fails under Colima, so the cable path does not work
there. Docker Desktop and Linux are untested.** Read the Colima note below before you
start.

One stack on the ground station collects from both laptops, and one Grafana shows both.

| Laptop | Role | Cable address | `service.instance.id` |
| --- | --- | --- | --- |
| MacBook (runs this stack) | ground station | `10.42.0.1` (`bridge0`) | `mac-ground` |
| HP ZBook (PLEXBOX) | edge-2 | `10.42.0.2` | `edge-2` |

The OTLP ports (4317 gRPC, 4318 HTTP) listen on the cable address only, so the
ZBook can reach them and nothing on Wi-Fi can. Grafana, Prometheus, Tempo and Loki
stay on `127.0.0.1`.

Start the stack on the Mac. First check the value: `scripts/check_otlp_bind.sh` accepts
only `127.0.0.1` and `10.42.0.1` and refuses anything else (`0.0.0.0` or `::` would put the
OTLP receiver, which has no authentication, on Wi-Fi).

```bash
OTLP_BIND=10.42.0.1 scripts/check_otlp_bind.sh
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
OTEL_RESOURCE_ATTRIBUTES=service.instance.id=mac-ground FLIGHTRISK_TELEMETRY_CONTENT=1 \
  python -m flightrisk.dashboard --source edge ...
```

`FLIGHTRISK_TELEMETRY_CONTENT=1` turns on operator text and the target image preview for the
field setup (the code default is off); leave it out to log numbers and ids only.

ZBook (edge-2), PowerShell from the repo root:

```powershell
$env:FLIGHTRISK_OTEL_ENABLED = "true"; $env:OTEL_EXPORTER = "otlp-grpc"; $env:OTEL_ENDPOINT = "http://10.42.0.1:4317"; $env:OTEL_RESOURCE_ATTRIBUTES = "service.instance.id=edge-2"; $env:FLIGHTRISK_TELEMETRY_CONTENT = "1"
.\scripts\demo_edge.ps1
```

The edge process (`python -m flightrisk.edge_device`) exports its logs the same way the
dashboard does, plus its side of the drone communication (message counts, sends, reconnects)
and the lane executor's flight-safety events. In Grafana, pick nodes with the **Node**
drop-down at the top of each dashboard (multi-select, *All* by default). A process that does not set
`service.instance.id` appears as `unknown`: FlightRisk sets that itself, because the
OpenTelemetry SDK would otherwise pick a random id on every start and each restart would be
a new node.

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

## Dashboards

Five dashboards are generated from `grafana/build_dashboards.py` (dashboards as code) and
provisioned from `grafana/dashboards/`. They share the **Node**, **Drone**, **Level** and
**Component** variables, link to each other (top right), and annotate flight-safety events,
alert transitions and target changes.

| Dashboard (uid) | What it answers |
| --- | --- |
| FlightRisk / Overview (`fr-overview`) | Golden signals: request rate, handler error ratio, latency p50/p95/p99 (with exemplar links to traces), saturation (CPU, memory, threads, open files, queues), uptime, active alert; SLOs for frame-loop availability, drone command success and edge link availability with an error-budget burn rate; recent warnings and errors; recent traces |
| FlightRisk / Flight Safety & Comms (`fr-flight-comm`) | Per-drone battery, height with its source, telemetry age, speed, attitude, flying; command outcomes and latency, land durations, flight-safety events, obstacle checks; link age, clock offset and RTT, connections and reconnects; message rates by type, acks and nacks with reasons, undelivered messages; live drone communication, flight-safety and telemetry-snapshot logs |
| FlightRisk / Detection & Matching (`fr-detection`) | FPS, frame and per-stage latency, persons per frame, per-signal scores, alert levels, alert latch and operator response time, model loads, reasoning calls, latency and tokens, pipeline summaries |
| FlightRisk / Operator Input & Target (`fr-operator`) | Every operator action with its outcome, rejected and unauthorized requests, the operator input log, the target image (numbers, hash and thumbnail), alert responses |
| FlightRisk / Errors & Logs (`fr-errors`) | Error rate by component, top error fingerprints, log volume by level and component, dropped console records, an error log and a log explorer filtered by level and component, traces with errors |

The original **FlightRisk** dashboard (`flightrisk-overview`, from PR #88) is still provisioned.

To change a dashboard, edit `grafana/build_dashboards.py` and run it; a test fails when the
committed JSON differs from the generator's output. `allowUiUpdates` is on for quick
experiments, but provisioned changes made in the UI are lost on restart.

## Alerts

Grafana alert rules are provisioned from `grafana/alerting/` (mounted into
`/etc/grafana/provisioning/alerting`): drone battery below 20 % (warning) and 10 %
(critical), telemetry older than 5 s while flying, a link-loss or watchdog landing (or a lost
drone), a land that was not executed or not confirmed, dropped fleet messages, an error-rate
spike (more than 10 per minute) and a stalled frame loop (no frame for 10 s). They notify a
placeholder webhook on Grafana's own loopback, so nothing leaves the machine; replace its URL
in `grafana/alerting/contact-points.yml` with a local receiver to get notifications.

## Links between logs, metrics and traces

- Every log line written inside a span carries `trace_id` and `span_id`; the OTLP record
  carries the same ids, which Loki stores as structured metadata. In Explore, a Loki line
  has a **View trace** link (derived field `trace_id`).
- From a Tempo span, **Logs for this span** runs
  `{service_name=~"flightrisk.*"} | trace_id="<id>"` in Loki.
- Latency histograms carry exemplars. The collector exposes them in OpenMetrics format and
  Prometheus stores them (`--enable-feature=exemplar-storage`); the exemplar dots on the
  latency panels open the trace.

## Logs

Every module writes the same JSON line (`flightrisk/obs_log.py`) to stderr and, in the
`otlp-*` modes, to Loki. Query with `| json`:

```logql
{service_name=~"flightrisk.*"} | json | event="user_input"
{service_name=~"flightrisk.*"} | json | category="flight_safety"
{service_name=~"flightrisk.*", service_instance_id="edge-2"} | json | level=~"error|critical"
```

`FLIGHTRISK_LOG_LEVEL` (default `INFO`) sets the level; `DEBUG` also adds stack traces to
error lines. Console output goes through a bounded queue and a writer thread, so a slow
terminal never blocks the frame loop: when the queue is full, records are dropped and counted
(`flightrisk_log_dropped`). Operator console lines from the Tello, detector, ReID, recorder
and reasoning modules are still printed to stdout as before and also logged as events;
`FLIGHTRISK_CONSOLE_ECHO=0` stops the stdout copy.

## Operator content and the target image

`FLIGHTRISK_TELEMETRY_CONTENT` decides whether operator content is captured. It is **off by
default in code**. The two-laptop instructions above and the field launch in
`docs/fleet-setup.md` turn it on.

| | Off (default) | On (`FLIGHTRISK_TELEMETRY_CONTENT=1`) |
| --- | --- | --- |
| Operator actions (`user_input`) | event name, outcome, actor, remote address, numbers, short tokens (preset, command, drone id); free text as its length only | the same, plus free text (description, notes) after redaction, capped at 500 characters |
| Target image (`target_image`) | size, dimensions, MIME type, quality scores, face-report numbers | the same, plus the sha256, a local artifact and a signed preview URL |
| Image bytes in logs, metrics or labels | never | never |

Never captured, whatever the switch: face or appearance embeddings, crops of bystanders,
video frames, the reasoning model's text (only provider, model, latency, tokens and outcome
are recorded), and secrets (API keys, `FLIGHTRISK_EDGE_TOKEN`, `Authorization` headers and
anything matching a known key pattern are redacted before a line is written).

### The target image preview

With content capture on, the target photo is stored once per distinct image in
`~/.flightrisk/artifacts/targets/` (`FLIGHTRISK_ARTIFACT_DIR`): the photo as uploaded
(`<sha256>.jpg`) and a thumbnail of at most 160 px (`<sha256>.thumb.jpg`). The directory is
mode 0700 and the files 0600. Files older than `FLIGHTRISK_ARTIFACT_RETENTION_DAYS` (default 7)
are deleted, and at most `FLIGHTRISK_ARTIFACT_MAX_FILES` (default 50) targets are kept.

The **Target image** panel shows the thumbnail. A Grafana panel cannot send the dashboard's
`Authorization` header, and the dashboard's deny-by-default API-key gate is not changed to
allow one. Instead, FlightRisk runs a separate preview server that:

- listens on `127.0.0.1` only (`FLIGHTRISK_PREVIEW_PORT`, default 5560; `0` turns it off),
- serves only thumbnails, never the full photo,
- requires a capability URL signed with HMAC-SHA256 over the image hash and an expiry
  (`FLIGHTRISK_PREVIEW_TTL_S`, default 24 h), with the key from `FLIGHTRISK_PREVIEW_SECRET` or
  a random key kept in `<artifact dir>/preview.key` (mode 0600),
- answers 404 to anything else (wrong signature, expired, unknown hash, other paths).

Each `target_image` event carries its `preview_url`. The trade-off: anyone who can read the
Loki logs and reach this machine's loopback can open that thumbnail until the URL expires.
The URL is a bearer capability, so treat the logs as sensitive while it is valid (they
describe an active search anyway). Set `FLIGHTRISK_PREVIEW_PORT=0` to keep the artifact but
serve no preview.

## What is stored, and where

| Data | Where | Kept for |
| --- | --- | --- |
| Logs (all events above) | Loki volume `loki_data` in the Docker stack on the ground station | 7 days (`LOKI_RETENTION_PERIOD`, default `168h`; deleted by the compactor) |
| Metrics | Prometheus volume `prometheus_data` | 7 days (`--storage.tsdb.retention.time`) |
| Traces | Tempo volume `tempo_data` | 7 days (`block_retention`) |
| Target photo and thumbnail (content capture on) | `~/.flightrisk/artifacts/targets/` on the machine that received the upload | 7 days, at most 50 targets |
| Preview signing key | `~/.flightrisk/artifacts/preview.key` | until deleted |
| Console output | the terminal (stderr), and wherever you redirect it | not managed |

Nothing is sent outside the machines you run: the collector listens on loopback (or, with
`OTLP_BIND=10.42.0.1`, on the laptop-to-laptop cable only), Grafana, Prometheus, Tempo and
Loki stay on `127.0.0.1`, and the alert contact point is a local placeholder.

## Checking the dashboards against a live stack

```bash
python scripts/audit_grafana_dashboards.py --grafana http://127.0.0.1:3000 --minutes 10
python scripts/bench_telemetry_overhead.py --endpoint http://127.0.0.1:4317
```

The audit runs every panel query through Grafana and lists panels that returned no data.
The benchmark measures what the instrumentation adds to the frame loop and to the lane
executor's monitor tick, with the collector reachable, on a closed port and on an address
that never answers.

## Notes

- All published ports bind to `127.0.0.1`, in all three compose files. Do not expose them; logs and traces describe an active search.
  The one exception is opt-in: `OTLP_BIND=10.42.0.1` puts the OTLP ports on the Thunderbolt cable address.
  Change the Grafana admin password (`GRAFANA_ADMIN_PASSWORD`) before using this anywhere shared.
- Image versions are pinned in `docker-compose.grafana.yml`. Retention is 7 days for metrics, logs and traces
  (`LOKI_RETENTION_PERIOD` changes the log retention).
- Data lives in named Docker volumes. `docker compose -f docker-compose.grafana.yml down -v` wipes it.
