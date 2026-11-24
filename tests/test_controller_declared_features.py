"""Declared-feature vocabulary: ``DroneCapabilities.inferred_from`` on duck-typed objects."""

from __future__ import annotations

import pytest

from flightrisk.drone.controller import BASIC_FLIGHT, Capability, DroneCapabilities

METHOD_FOR = {
    Capability.SETTLED_HEIGHT: "settled_height",
    Capability.TAKEOFF_REPLY_TIME: "last_takeoff_reply_s",
    Capability.MEASURED_HEADING: "measured_heading_deg",
    Capability.STATE_TEMPERATURE: "state_temperature",
    Capability.MEASURED_POSE: "measured_pose",
    Capability.LINK_HEALTH: "link_status",
}


def _with_method(name: str):
    obj = type("Duck", (), {name: lambda self: None})()
    return obj


@pytest.mark.parametrize("capability,method", list(METHOD_FOR.items()))
def test_method_presence_infers_capability(capability, method):
    assert DroneCapabilities.inferred_from(_with_method(method)).supports(capability)


@pytest.mark.parametrize("capability,method", list(METHOD_FOR.items()))
def test_missing_method_does_not_infer_capability(capability, method):
    assert not DroneCapabilities.inferred_from(object()).supports(capability)


@pytest.mark.parametrize("capability,method", list(METHOD_FOR.items()))
def test_non_callable_attribute_does_not_infer(capability, method):
    obj = type("Duck", (), {method: 5})()
    assert not DroneCapabilities.inferred_from(obj).supports(capability)


def test_takeoff_with_abort_keyword_infers_takeoff_abort():
    class Duck:
        def takeoff(self, abort=None):
            return None

    assert DroneCapabilities.inferred_from(Duck()).supports(Capability.TAKEOFF_ABORT)


def test_takeoff_without_abort_keyword_does_not_infer():
    class Duck:
        def takeoff(self):
            return None

    assert not DroneCapabilities.inferred_from(Duck()).supports(Capability.TAKEOFF_ABORT)


def test_no_takeoff_does_not_infer_and_does_not_raise():
    assert not DroneCapabilities.inferred_from(object()).supports(Capability.TAKEOFF_ABORT)
    duck = type("Duck", (), {"takeoff": 3})()
    assert not DroneCapabilities.inferred_from(duck).supports(Capability.TAKEOFF_ABORT)


def test_unreadable_signature_does_not_infer_takeoff_abort():
    class Unreadable:
        def __call__(self, abort=None):
            return None

        @property
        def __signature__(self):
            raise ValueError("no signature")

    duck = type("Duck", (), {})()
    duck.takeoff = Unreadable()
    assert not DroneCapabilities.inferred_from(duck).supports(Capability.TAKEOFF_ABORT)


def test_bare_object_infers_exactly_basic_flight():
    assert DroneCapabilities.inferred_from(object()).features == BASIC_FLIGHT


def test_basic_flight_excludes_new_members():
    assert not BASIC_FLIGHT & (set(METHOD_FOR) | {Capability.TAKEOFF_ABORT})


def test_constructor_defaults_unchanged():
    caps = DroneCapabilities()
    assert caps.min_move_cm == 20
    assert caps.max_move_cm == 500
    assert caps.max_altitude_m == 120
    assert caps.flight_scale == "room"
    assert caps.features == BASIC_FLIGHT
    assert caps.has_gps is False
