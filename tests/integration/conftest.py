"""Shared fixtures for the integration tests; they act only when a SITL address is set.

With no ``FLIGHTRISK_SITL_ADDRESS`` (or harness export) every fixture here does nothing, so the default run and
the unit tests are unchanged. See ``tests/integration/sitl_support.py`` for why the reaper exists.
"""
from __future__ import annotations

import pytest

from tests.integration import sitl_support


@pytest.fixture(scope="session", autouse=True)
def _no_foreign_mavsdk_servers():
    """Stop the run when another process's mavsdk_server already serves one of the SITL vehicles."""
    addresses = sitl_support.sitl_env().addresses
    if addresses:
        foreign = sitl_support.foreign_mavsdk_servers(addresses)
        if foreign:
            lines = "\n".join(f"  pid {pid}: {command}" for pid, command in foreign)
            pytest.exit(
                "another process already runs a mavsdk_server for these SITL vehicles, which would keep the "
                f"vehicle's connection slot; stop it first:\n{lines}",
                returncode=3,
            )
    yield


@pytest.fixture(scope="module", autouse=True)
def _reap_mavsdk_servers():
    """After each module, stop the mavsdk_server children this process left behind and wait for the ports.

    Autouse fixtures are set up first, so this teardown runs after the module's own fixtures (such as
    ``sitl_available``) have been finalized.
    """
    yield
    addresses = sitl_support.sitl_env().addresses
    if not addresses:
        return
    sitl_support.reap_child_mavsdk_servers()
    for address in addresses:
        sitl_support.wait_port_released(address)


@pytest.fixture(scope="session")
def sitl() -> sitl_support.SitlEnv:
    """The SITL fleet description with the autopilot resolved (probed once when undeclared); skips without one."""
    env = sitl_support.sitl_env()
    if not env.addresses:
        pytest.skip(f"set {sitl_support.ENV_ADDRESS} to a running SITL (docs/sitl-demo.md)")
    try:
        autopilot = sitl_support.resolve_autopilot(env)
    except sitl_support.SitlNotReady as exc:
        pytest.fail(f"could not tell which autopilot the SITL runs: {exc}")
    return sitl_support.with_autopilot(env, autopilot)
