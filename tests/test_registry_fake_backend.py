"""The backend the fake drone simulates is named, not inherited from the default."""

from __future__ import annotations

from flightrisk.drone import registry


def test_fake_drone_backend_is_tello():
    assert registry.FAKE_DRONE_BACKEND == "tello"


def test_fake_drone_backend_is_registered():
    assert registry.is_registered(registry.FAKE_DRONE_BACKEND)


def test_fake_drone_backend_ignores_default_backend(monkeypatch):
    monkeypatch.setattr(registry, "DEFAULT_BACKEND", "something_else")
    assert registry.FAKE_DRONE_BACKEND == "tello"
