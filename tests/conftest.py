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
    """Reset flightrisk.dashboard.app module-level state for test isolation.

    Request this fixture explicitly in any test that reads or mutates
    ``app_state`` or ``_alerted_tracks`` -- it snapshots both before the
    test body runs and restores them afterward so tests can't leak
    drone/session/target state into each other.
    """
    import copy
    import dataclasses
    from flightrisk.dashboard import app as app_module
    from flightrisk.dashboard.state import app_state, AppState

    # Snapshot the current AppState fields (shallow copy is fine for
    # the scalar / None fields that tests typically touch).
    original_fields = {f.name: copy.copy(getattr(app_state, f.name)) for f in dataclasses.fields(AppState)}
    original_alerted = app_module._alerted_tracks.copy()
    yield
    # Restore each field on the singleton
    for name, value in original_fields.items():
        setattr(app_state, name, value)
    app_module._alerted_tracks.clear()
    app_module._alerted_tracks.update(original_alerted)
