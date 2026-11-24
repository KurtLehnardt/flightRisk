"""Checked-in baseline of today's Tello coupling outside the Tello backend.

``tests/test_tello_coupling.py`` reads this file and compares it with a fresh scan of the repository
(SEAM-9 (proposed)). Each section maps a key ``(posix path, enclosing qualname, kind, token)`` to the exact number
of findings the scan reports for it. The counts are exact and may only shrink: a new key or a higher
count fails the test, and so does a lower count or a key that no longer occurs.

Every section is named after the phase that owns the code in it. When a phase removes Tello coupling from
its files, it deletes the entries it removed in its own ledger chunk. A phase named in ``CLOSED_PHASES``
must have an empty section; the tuple stays empty until a phase has finished.

The ``"A2"`` section holds the implicit inventory's Tello default (INV-4) from
``flightrisk/hardware/inventory.py``. The ``"catalogue-data"`` section holds the Tello facts of the hardware
catalogue and the parameter registry. No phase is asked to shrink it, because those files describe the
Tello as one catalogued model.
"""

from __future__ import annotations

COUPLING_BASELINE: dict[str, dict[tuple[str, str, str, str], int]] = {
    "X-core": {
        # flightrisk/hardware/features.py
        ('flightrisk/hardware/features.py', '', 'model_default', 'tello_sdk_udp'): 1,
        ('flightrisk/hardware/features.py', '_is_tello', 'compare', 'TELLO_ADAPTER'): 1,
        ('flightrisk/hardware/features.py', 'feature_reason', 'name', '_is_tello'): 3,
    },
    "X-legacy": {
        # flightrisk/drone/fleet.py
        ('flightrisk/drone/fleet.py', 'DroneFleet', 'literal', '192.168.10.1'): 1,
        ('flightrisk/drone/fleet.py', '_default_factory', 'import', 'flightrisk.drone.tello'): 1,
        ('flightrisk/drone/fleet.py', '_default_factory', 'name', 'TelloController'): 1,
    },
    "A3": {
        # flightrisk/drone/flight_guards.py
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSALS_PER_FLIGHT'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSAL_HOLD_S'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSAL_RETRIES'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_SAMPLES'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM'): 1,
        ('flightrisk/drone/flight_guards.py', '', 'literal', 'FLIGHTRISK_TELLO_TAKEOFF_LATE_S'): 1,
        # flightrisk/drone/temp_preflight.py
        ('flightrisk/drone/temp_preflight.py', '', 'literal', 'FLIGHTRISK_TELLO_TEMP_REFUSE_C'): 1,
        ('flightrisk/drone/temp_preflight.py', '', 'literal', 'FLIGHTRISK_TELLO_TEMP_WARN_C'): 1,
        ('flightrisk/drone/temp_preflight.py', 'TempCheck.operator_text', 'name', 'ENV_TELLO_TEMP_REFUSE_C'): 1,
        ('flightrisk/drone/temp_preflight.py', 'TempLimits.from_env', 'name', 'ENV_TELLO_TEMP_REFUSE_C'): 2,
        ('flightrisk/drone/temp_preflight.py', 'TempLimits.from_env', 'name', 'ENV_TELLO_TEMP_WARN_C'): 2,
        ('flightrisk/drone/temp_preflight.py', 'live_refusal', 'name', 'ENV_TELLO_TEMP_REFUSE_C'): 1,
        # flightrisk/edge_device.py
        ('flightrisk/edge_device.py', '', 'literal', 'FLIGHTRISK_TELLO_IFINDEX'): 1,
        ('flightrisk/edge_device.py', '', 'literal', 'FLIGHTRISK_TELLO_LOCAL_IP'): 1,
        ('flightrisk/edge_device.py', '_build_drone', 'import', 'flightrisk.drone.tello'): 2,
        ('flightrisk/edge_device.py', '_build_drone', 'import', 'flightrisk.drone.tello_bound'): 1,
        ('flightrisk/edge_device.py', '_build_drone', 'name', 'FakeTelloController'): 1,
        ('flightrisk/edge_device.py', '_build_drone', 'name', 'TelloBindingError'): 1,
        ('flightrisk/edge_device.py', '_build_drone', 'name', 'TelloController'): 2,
        ('flightrisk/edge_device.py', '_build_drone', 'name', 'tello_host'): 4,
        ('flightrisk/edge_device.py', '_build_parser', 'name', '_TELLO_IFINDEX_ENV_VAR'): 1,
        ('flightrisk/edge_device.py', '_build_parser', 'name', '_TELLO_LOCAL_IP_ENV_VAR'): 1,
        ('flightrisk/edge_device.py', '_check_capture_stamps', 'name', '_TELLO_LOCAL_IP_ENV_VAR'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'import', 'flightrisk.drone.tello_bound'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'TelloBinding'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'TelloBindingError'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'tello_default_host'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'tello_host'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'tello_ifindex'): 1,
        ('flightrisk/edge_device.py', '_check_tello_binding', 'name', 'tello_local_ip'): 1,
        ('flightrisk/edge_device.py', '_no_capture_stamps', 'compare', 'tello'): 1,
        ('flightrisk/edge_device.py', '_resolve_drone_and_source', 'compare', 'tello'): 1,
        ('flightrisk/edge_device.py', '_resolve_drone_and_source', 'compare', 'tello_host'): 1,
        ('flightrisk/edge_device.py', '_resolve_drone_and_source', 'compare', 'tello_ifindex'): 1,
        ('flightrisk/edge_device.py', '_resolve_drone_and_source', 'compare', 'tello_local_ip'): 1,
        ('flightrisk/edge_device.py', '_resolve_frame_stride', 'compare', 'tello'): 1,
        ('flightrisk/edge_device.py', '_resolve_tello_binding', 'compare', 'tello'): 1,
        ('flightrisk/edge_device.py', '_resolve_tello_binding', 'import', 'flightrisk.drone.tello_bound'): 1,
        ('flightrisk/edge_device.py', '_resolve_tello_binding', 'name', '_TELLO_IFINDEX_ENV_VAR'): 1,
        ('flightrisk/edge_device.py', '_resolve_tello_binding', 'name', '_TELLO_LOCAL_IP_ENV_VAR'): 1,
        ('flightrisk/edge_device.py', '_temp_limits', 'name', 'ENV_TELLO_TEMP_REFUSE_C'): 1,
        ('flightrisk/edge_device.py', '_temp_limits', 'name', 'ENV_TELLO_TEMP_WARN_C'): 1,
        ('flightrisk/edge_device.py', 'main', 'name', '_check_tello_binding'): 1,
        ('flightrisk/edge_device.py', 'main', 'name', '_resolve_tello_binding'): 1,
    },
    "A4": {
        # flightrisk/config.py
        ('flightrisk/config.py', 'DroneConfig', 'literal', '192.168.10.1'): 1,
        # flightrisk/dashboard/state.py
        ('flightrisk/dashboard/state.py', 'SourceConfig', 'model_default', 'tello'): 1,
        # flightrisk/drone/coordinator.py
        ('flightrisk/drone/coordinator.py', 'FleetCoordinator.plan', 'compare', 'TELLO_MIN_MOVE_CM'): 1,
    },
    "A5": {
        # flightrisk/launcher/adaptermap.py
        ('flightrisk/launcher/adaptermap.py', 'candidate_adapters', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/adaptermap.py', 'confirmed_mapping', 'compare', 'tello'): 1,
        ('flightrisk/launcher/adaptermap.py', 'propose_adapters', 'compare', 'tello'): 2,
        ('flightrisk/launcher/adaptermap.py', 'propose_adapters', 'name', 'tello_host'): 1,
        # flightrisk/launcher/claude_uplink.py
        ('flightrisk/launcher/claude_uplink.py', '_route_row', 'name', 'on_tello_subnet'): 1,
        ('flightrisk/launcher/claude_uplink.py', 'on_tello_subnet', 'compare', 'TELLO_SUBNET'): 1,
        # flightrisk/launcher/commands.py
        ('flightrisk/launcher/commands.py', '', 'literal', 'FLIGHTRISK_TELLO_IFINDEX'): 1,
        ('flightrisk/launcher/commands.py', '', 'literal', 'FLIGHTRISK_TELLO_LOCAL_IP'): 1,
        ('flightrisk/launcher/commands.py', 'edge_spec', 'compare', 'tello'): 1,
        ('flightrisk/launcher/commands.py', 'edge_spec', 'name', 'tello_host'): 1,
        # flightrisk/launcher/preflight.py
        ('flightrisk/launcher/preflight.py', '', 'name', 'TelloProbe'): 1,
        ('flightrisk/launcher/preflight.py', 'PreflightEnv', 'name', 'TelloProbe'): 1,
        ('flightrisk/launcher/preflight.py', 'PreflightEnv', 'name', 'probe_tello'): 1,
        ('flightrisk/launcher/preflight.py', 'PreflightEnv', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/preflight.py', '_ask', 'name', 'TELLO_CMD_PORT'): 1,
        ('flightrisk/launcher/preflight.py', '_no_local_ip_check', 'compare', 'tello_count'): 1,
        ('flightrisk/launcher/preflight.py', '_no_local_ip_check', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/preflight.py', '_safe_adapter_to_name', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/preflight.py', 'probe_tello', 'name', 'TELLO_CMD_PORT'): 1,
        ('flightrisk/launcher/preflight.py', 'probe_tello', 'name', 'TelloProbe'): 7,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'compare', 'tello'): 1,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'name', 'TELLO_MODULES'): 1,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'name', 'local_tellos'): 6,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'name', 'local_tellos_with_unknown_adapter'): 1,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'name', 'local_tellos_without_adapter'): 2,
        ('flightrisk/launcher/preflight.py', 'run_preflight', 'name', 'tello_probe'): 1,
        # flightrisk/launcher/settings.py
        ('flightrisk/launcher/settings.py', '', 'literal', '192.168.10.1'): 1,
        ('flightrisk/launcher/settings.py', 'DroneSettings', 'model_default', 'tello'): 1,
        ('flightrisk/launcher/settings.py', 'DroneSettings', 'name', 'DEFAULT_TELLO_HOST'): 1,
        ('flightrisk/launcher/settings.py', '_drone_from_json', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/settings.py', '_validate_adapters', 'compare', 'tello'): 1,
        ('flightrisk/launcher/settings.py', '_validate_adapters', 'name', 'local_tellos'): 5,
        ('flightrisk/launcher/settings.py', '_validate_drones', 'compare', 'tello'): 1,
        ('flightrisk/launcher/settings.py', '_validate_drones', 'name', 'tello_host'): 2,
        ('flightrisk/launcher/settings.py', '_validate_shared_subnet', 'name', 'local_tellos_with_unknown_adapter'): 1,
        ('flightrisk/launcher/settings.py', '_validate_shared_subnet', 'name', 'local_tellos_without_adapter'): 1,
        ('flightrisk/launcher/settings.py', 'builtin_presets', 'model_default', 'tello'): 1,
        ('flightrisk/launcher/settings.py', 'choose_adapter_message', 'name', 'tello_host'): 1,
        ('flightrisk/launcher/settings.py', 'local_tellos_with_unknown_adapter', 'compare', 'tello'): 1,
        ('flightrisk/launcher/settings.py', 'local_tellos_without_adapter', 'compare', 'tello'): 1,
        ('flightrisk/launcher/settings.py', 'local_tellos_without_adapter', 'name', 'tello_host'): 1,
    },
    "X-scripts": {
        # scripts/bench_telemetry_overhead.py
        ('scripts/bench_telemetry_overhead.py', 'worker', 'import', 'flightrisk.drone.tello'): 1,
        ('scripts/bench_telemetry_overhead.py', 'worker', 'name', 'FakeTelloController'): 1,
        ('scripts/bench_telemetry_overhead.py', 'worker', 'name', 'tello'): 2,
        # scripts/check_wildcard_guard.py
        ('scripts/check_wildcard_guard.py', '', 'import', 'flightrisk.drone.tello_bound'): 1,
        ('scripts/check_wildcard_guard.py', 'Report', 'name', 'TelloBinding'): 1,
        ('scripts/check_wildcard_guard.py', 'Report.guard', 'name', 'TelloBindingError'): 1,
        ('scripts/check_wildcard_guard.py', '_bind_edge', 'name', 'TelloBindingError'): 1,
        ('scripts/check_wildcard_guard.py', 'main', 'name', 'TelloBinding'): 2,
        # scripts/fleet_env.ps1
        ('scripts/fleet_env.ps1', '', 'shell', '192.168.10.1'): 1,
        ('scripts/fleet_env.ps1', '', 'shell', 'FLIGHTRISK_TELLO_HOST'): 5,
        # scripts/fleet_env.sh
        ('scripts/fleet_env.sh', '', 'shell', '192.168.10.1'): 1,
        ('scripts/fleet_env.sh', '', 'shell', 'FLIGHTRISK_TELLO_HOST'): 5,
        # scripts/obstacle_record.py
        ('scripts/obstacle_record.py', 'main', 'import', 'flightrisk.drone.tello'): 1,
        ('scripts/obstacle_record.py', 'main', 'import', 'flightrisk.drone.tello_bound'): 1,
        ('scripts/obstacle_record.py', 'main', 'name', 'TelloController'): 1,
        ('scripts/obstacle_record.py', 'main', 'name', 'tello_host'): 3,
        ('scripts/obstacle_record.py', 'main', 'name', 'tello_ifindex'): 1,
        ('scripts/obstacle_record.py', 'main', 'name', 'tello_local_ip'): 1,
        ('scripts/obstacle_record.py', 'parse_args', 'literal', '192.168.10.1'): 1,
        # scripts/tello_hw_check.py
        ('scripts/tello_hw_check.py', '', 'literal', '192.168.10.1'): 1,
    },
    "D3": {
        # flightrisk/drone/coverage.py
        ('flightrisk/drone/coverage.py', '', 'model_default', 'Tello'): 1,
        ('flightrisk/drone/coverage.py', '', 'name', 'TELLO'): 1,
        ('flightrisk/drone/coverage.py', 'tello_sweep_model', 'name', 'TELLO'): 1,
        ('flightrisk/drone/coverage.py', 'tello_sweep_model', 'name', 'TELLO_VIDEO_HEIGHT_PX'): 1,
        ('flightrisk/drone/coverage.py', 'tello_sweep_model', 'name', 'TELLO_VIDEO_WIDTH_PX'): 1,
    },
    "C5": {},
    "catalogue-data": {
        # flightrisk/hardware/catalogue.py
        ('flightrisk/hardware/catalogue.py', '', 'model_default', 'Tello'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'model_default', 'tello'): 3,
        ('flightrisk/hardware/catalogue.py', '', 'model_default', 'tello_edu'): 1,
        ('flightrisk/hardware/catalogue.py', '', 'model_default', 'tello_sdk_udp'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_COVERAGE_SOURCE'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_EDU'): 1,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_FRONT'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_MANUAL'): 1,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_SOURCES'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_TELLO_SPECS'): 1,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_tello_envelope'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_tello_facts'): 2,
        ('flightrisk/hardware/catalogue.py', '', 'name', '_tello_safety'): 2,
        ('flightrisk/hardware/catalogue.py', '_tello_envelope', 'name', '_TELLO_SPECS'): 4,
        ('flightrisk/hardware/catalogue.py', '_tello_facts', 'name', '_TELLO_MANUAL'): 2,
        ('flightrisk/hardware/catalogue.py', '_tello_facts', 'name', '_TELLO_SPECS'): 4,
        # flightrisk/params/table.py
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSALS_PER_FLIGHT'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSAL_HOLD_S'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_REFUSAL_RETRIES'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_SAMPLES'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_TIMEOUT_S'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_SETTLE_TOLERANCE_CM'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_TAKEOFF_LATE_S'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_TEMP_REFUSE_C'): 2,
        ('flightrisk/params/table.py', '', 'literal', 'FLIGHTRISK_TELLO_TEMP_WARN_C'): 2,
        ('flightrisk/params/table.py', '', 'model_default', 'tello'): 1,
    },
    "A2": {
        # flightrisk/hardware/inventory.py
        ('flightrisk/hardware/inventory.py', '', 'model_default', 'tello'): 1,
    },
}

CLOSED_PHASES: tuple[str, ...] = ()
