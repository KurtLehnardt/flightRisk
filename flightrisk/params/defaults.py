"""Read one registry row's default for one model, for code that derives a constant from the registry.

The Tello adapter, its bound client, the fake drone, the temperature preflight and the flight guards each
derive constants from registry rows instead of keeping a copy. They share this one lookup, so a missing row or
a default that is not a number fails the same way everywhere, at import.

The row index is built once per registry table. A test that replaces ``table.TABLE`` with a patched copy gets a
fresh index for that copy, and the real table is never edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from numbers import Real

from flightrisk.params import table as _table
from flightrisk.params.spec import ParamSpec

__all__ = ["model_default", "model_range"]

#: ``(table the index was built from, row id -> row)``, swapped as one value.
_index: tuple[object, Mapping[str, ParamSpec]] = ((), {})


def _rows() -> Mapping[str, ParamSpec]:
    """The registry rows by id, rebuilt only when ``table.TABLE`` is a different object."""
    global _index
    source, rows = _index
    if source is not _table.TABLE:
        source = _table.TABLE
        rows = {spec.id: spec for spec in source}
        _index = (source, rows)
    return rows


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _raw_default(row_name: str, model_id: str) -> object:
    spec = _rows().get(row_name)
    if spec is None:
        raise KeyError(f"no registry row {row_name!r}")
    if model_id not in spec.defaults:
        raise KeyError(f"registry row {row_name!r} has no {model_id!r} column")
    return spec.defaults[model_id]


def model_default(row_name: str, model_id: str) -> int | float:
    """The default of registry row ``row_name`` in the ``model_id`` profile column, unchanged in value and type.

    Raises KeyError naming the row when the row or the column is missing, and TypeError when the default is not
    a real number (None, a text such as ``unset`` or ``deferred``, a bool, or a tuple).
    """
    value = _raw_default(row_name, model_id)
    if not _is_number(value):
        raise TypeError(f"registry row {row_name!r} default for {model_id!r} is not a number: {value!r}")
    return value


def model_range(row_name: str, model_id: str) -> tuple[int | float, ...]:
    """The tuple default of registry row ``row_name`` in the ``model_id`` profile column, such as a reading range.

    Raises KeyError naming the row when the row or the column is missing, and TypeError unless the default is a
    tuple of real numbers (no bool).
    """
    value = _raw_default(row_name, model_id)
    if not isinstance(value, tuple) or not value or not all(_is_number(part) for part in value):
        raise TypeError(f"registry row {row_name!r} default for {model_id!r} is not a tuple of numbers: {value!r}")
    return value
