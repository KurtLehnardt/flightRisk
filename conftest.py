"""Shared test fixtures for the Amber test suite."""

import numpy as np
import pytest


@pytest.fixture
def sample_frame():
    """640x480 random BGR numpy array simulating a camera frame."""
    return np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)


@pytest.fixture
def sample_crop():
    """224x224 random BGR numpy array simulating a person crop."""
    return np.random.randint(0, 256, (224, 224, 3), dtype=np.uint8)


@pytest.fixture
def temp_dir(tmp_path):
    """Temporary directory for test file output."""
    return tmp_path
