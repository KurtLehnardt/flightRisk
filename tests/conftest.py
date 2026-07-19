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
