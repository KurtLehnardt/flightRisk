import numpy as np
import pytest


@pytest.fixture
def sample_frame():
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)


@pytest.fixture
def sample_crop():
    return np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)


@pytest.fixture
def solid_blue_frame():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 0] = 255  # Blue channel
    return frame


@pytest.fixture
def tiny_crop():
    return np.random.randint(0, 255, (1, 1, 3), dtype=np.uint8)


@pytest.fixture
def large_crop():
    return np.random.randint(0, 255, (2000, 2000, 3), dtype=np.uint8)


@pytest.fixture(autouse=False)
def clean_app_state():
    """Reset amber.dashboard.app module-level state for test isolation.

    Request this fixture explicitly in any test that reads or mutates
    `amber.dashboard.app._state` or `_alerted_tracks` — it snapshots both
    before the test body runs and restores them afterward so tests can't
    leak drone/session/target state into each other.
    """
    from amber.dashboard import app as app_module

    original_state = app_module._state.copy()
    original_alerted = app_module._alerted_tracks.copy()
    yield
    app_module._state.clear()
    app_module._state.update(original_state)
    app_module._alerted_tracks.clear()
    app_module._alerted_tracks.update(original_alerted)
