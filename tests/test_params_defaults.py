"""The shared registry default lookup, ``flightrisk/params/defaults.py``.

The Tello adapter, its bound client, the fake drone, the temperature preflight and the flight guards derive their
constants through this lookup, so it must return a row's default unchanged in value and type, name a missing row,
and refuse a default that is not a number. Patched values go into a copy of the table; the real table is never
edited.
"""

from __future__ import annotations

import dataclasses

import pytest

from flightrisk.params import defaults
from flightrisk.params import table as registry
from flightrisk.params.defaults import model_default, model_range

PROFILE = registry.T


def _patched_table(monkeypatch, param_id: str, default):
    """Replace ``registry.TABLE`` with a copy whose ``param_id`` row has a new default in ``PROFILE``."""
    rows = [
        dataclasses.replace(spec, defaults={**spec.defaults, PROFILE: default}) if spec.id == param_id else spec
        for spec in registry.TABLE
    ]
    monkeypatch.setattr(registry, "TABLE", tuple(rows))


def _same(actual, expected) -> bool:
    return actual == expected and type(actual) is type(expected)


def test_an_int_default_is_returned_as_an_int():
    assert _same(model_default("tello_sdk.command_port", PROFILE), 8889)
    assert _same(model_default("envelope.max_altitude_cm", PROFILE), 1000)


def test_a_float_default_is_returned_as_a_float():
    assert _same(model_default("tello_sdk.command_reply_timeout_s", PROFILE), 7.0)


def test_a_missing_row_is_a_key_error_naming_the_row():
    with pytest.raises(KeyError, match="no.such_row"):
        model_default("no.such_row", PROFILE)
    with pytest.raises(KeyError, match="no.such_row"):
        model_range("no.such_row", PROFILE)


def test_a_missing_model_column_is_a_key_error_naming_the_row():
    with pytest.raises(KeyError, match="tello_sdk.command_port"):
        model_default("tello_sdk.command_port", "no_such_model")


@pytest.mark.parametrize("value", [None, "unset", "deferred", True, False, (1, 2)])
def test_a_default_that_is_not_a_number_is_a_type_error(monkeypatch, value):
    _patched_table(monkeypatch, "tello_sdk.command_port", value)
    with pytest.raises(TypeError, match="tello_sdk.command_port"):
        model_default("tello_sdk.command_port", PROFILE)


def test_a_range_default_is_returned_as_its_tuple():
    assert _same(model_range("tello_sdk.temp_reading_range_c", PROFILE), (-40, 150))


@pytest.mark.parametrize("value", [None, "unset", 5, (), (1, "2"), (True, 3)])
def test_a_range_that_is_not_a_tuple_of_numbers_is_a_type_error(monkeypatch, value):
    _patched_table(monkeypatch, "tello_sdk.temp_reading_range_c", value)
    with pytest.raises(TypeError, match="tello_sdk.temp_reading_range_c"):
        model_range("tello_sdk.temp_reading_range_c", PROFILE)


def test_the_row_index_is_built_once_per_table(monkeypatch):
    model_default("tello_sdk.command_port", PROFILE)
    first = defaults._index
    model_default("tello_sdk.state_port", PROFILE)
    assert defaults._index is first
    _patched_table(monkeypatch, "tello_sdk.command_port", 9999)
    assert model_default("tello_sdk.command_port", PROFILE) == 9999
    assert defaults._index is not first
    assert defaults._index[0] is registry.TABLE
