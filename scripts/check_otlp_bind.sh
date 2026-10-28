#!/usr/bin/env bash
# Pre-flight for docker-compose.grafana.yml: refuse OTLP_BIND values that would
# expose the OTLP receiver beyond this machine and the Thunderbolt cable.
#
# The OTLP receiver (ports 4317 and 4318) has no authentication. Anyone who can
# reach it can inject traces, metrics and logs into the dashboards. So only two
# addresses are accepted:
#   127.0.0.1  this machine only (the default when OTLP_BIND is unset or empty)
#   10.42.0.1  the Thunderbolt cable address of the ground station
# Anything else, 0.0.0.0 and :: in particular, would listen on Wi-Fi as well.
#
# Usage:
#   scripts/check_otlp_bind.sh && docker compose -f docker-compose.grafana.yml up -d
#   OTLP_BIND=10.42.0.1 scripts/check_otlp_bind.sh && \
#     OTLP_BIND=10.42.0.1 docker compose -f docker-compose.grafana.yml up -d
# An address can also be passed as the first argument; it overrides OTLP_BIND.
set -u

bind="${1-${OTLP_BIND:-127.0.0.1}}"
bind="${bind:-127.0.0.1}"

case "$bind" in
  127.0.0.1)
    echo "OTLP_BIND=127.0.0.1: the OTLP receiver listens on this machine only."
    ;;
  10.42.0.1)
    echo "OTLP_BIND=10.42.0.1: the OTLP receiver listens on the Thunderbolt cable."
    echo "Warning: the OTLP receiver has no authentication; anyone on the cable can send telemetry to it."
    ;;
  *)
    echo "Refusing OTLP_BIND=$bind: only 127.0.0.1 or 10.42.0.1 are allowed." >&2
    echo "The OTLP receiver has no authentication; 0.0.0.0 or :: would expose it to Wi-Fi." >&2
    exit 1
    ;;
esac
