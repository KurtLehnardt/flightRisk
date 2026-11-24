"""The registry's edge-builder hook and model lookup."""

from __future__ import annotations

import pytest

from flightrisk.drone import registry


def _host(options):
    return "127.0.0.1"


def _factory(drone_id, host, options):
    raise AssertionError("never built in these tests")


@pytest.fixture
def temp_name():
    name = "xca2_temp"
    yield name
    registry.unregister(name)


def test_register_without_edge_factory_still_works(temp_name):
    spec = registry.register(temp_name, _factory, default_host=_host)
    assert spec.edge_factory is None
    assert registry.get(temp_name) is spec


def test_for_model_finds_builtin_backends():
    assert registry.for_model("tello") is registry.get("tello")
    assert registry.for_model("tello_edu") is registry.get("tello")
    assert registry.for_model("generic_multirotor") is registry.get("mavlink")


def test_for_model_unknown_raises():
    with pytest.raises(registry.UnknownBackendError):
        registry.for_model("no_such_model")
    with pytest.raises(KeyError):
        registry.for_model("no_such_model")


def test_spec_without_models_is_never_returned(temp_name):
    registry.register(temp_name, _factory, default_host=_host)
    with pytest.raises(registry.UnknownBackendError):
        registry.for_model("")
    assert registry.for_model("tello").name != temp_name


def test_edge_kinds_empty_on_builtin_registrations():
    assert registry.edge_kinds() == ()


def test_edge_kinds_lists_spec_with_edge_factory(temp_name):
    def edge(*args, **kwargs):
        return None

    spec = registry.register(temp_name, _factory, default_host=_host, edge_factory=edge)
    assert spec.edge_factory is edge
    assert registry.edge_kinds() == (temp_name,)
    registry.unregister(temp_name)
    assert registry.edge_kinds() == ()
