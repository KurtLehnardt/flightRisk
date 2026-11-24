"""Inventory tests: INV-1 to INV-4, INV-6, INV-7 and CAP-5."""

import json
import threading

import pytest

from flightrisk.hardware.inventory import (
    STALE_REV, Inventory, InventoryRefused, InventoryStore, implicit_inventory, inventory_path, parse_inventory,
)

HEX = {"extends": "generic_multirotor", "name": "Site hexacopter",
       "param_bounds": {"envelope.ceiling_cm": {"lo": 2000, "hi": 6000}},
       "param_defaults": {"envelope.ceiling_cm": 2500}}


def doc(**over):
    base = {"version": 1, "rev": 0, "models": {}, "units": [
        {"unit_id": "drone-1", "name": "Tello A", "model": "tello",
         "link": {"kind": "wifi_ap", "ssid": "TELLO-5A1B2C", "host": "192.168.10.1"},
         "serial": "0TQDG7REDB1234", "available": True, "features": {"birdseye_search": False}}]}
    base.update(over)
    return base


def unit(**over):
    return {**doc()["units"][0], **over}


def test_inv1_roundtrip_atomic_and_rev(tmp_path):
    store = InventoryStore(tmp_path / "inventory.json")
    assert store.load() is None
    first = store.write(doc(models={"site_hex": HEX}), 0)
    assert first.rev == 1 and set(first.models) == {"site_hex"}
    loaded = store.load()
    assert loaded.rev == 1 and loaded.unit("drone-1")["model"] == "tello"
    assert json.loads((tmp_path / "inventory.json").read_text())["version"] == 1
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
    assert store.write(doc(), 1).rev == 2


def test_inv1_unreadable_file_is_refused_not_replaced(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text("{not json")
    store = InventoryStore(path)
    with pytest.raises(InventoryRefused):
        store.load()
    with pytest.raises(InventoryRefused):
        store.write(doc(), 0)
    assert path.read_text() == "{not json"


def test_inv1_rejects_bad_version_and_fields():
    for bad in ({"version": 2}, {"rev": -1}, {"rev": True}, {"extra": 1}):
        with pytest.raises(InventoryRefused):
            parse_inventory(doc(**bad))


def test_inv1_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIGHTRISK_INVENTORY_FILE", str(tmp_path / "x.json"))
    assert inventory_path() == tmp_path / "x.json"


@pytest.mark.parametrize("key", ["password", "wifi_passphrase", "secret", "edge_token", "api_key"])
def test_inv2_secret_keys_refused(key):
    with pytest.raises(InventoryRefused, match="secret"):
        parse_inventory(doc(units=[unit(link={"kind": "wifi_ap", key: "x"})]))


def test_inv2_edge_token_value_refused():
    data = doc(units=[unit(name="s3cr3t-token-value")])
    parse_inventory(data)
    with pytest.raises(InventoryRefused, match="edge token"):
        parse_inventory(data, edge_token="s3cr3t-token-value")


@pytest.mark.parametrize("bad", ["", "-x", "a b", "a/b", "x" * 65, None, 3])
def test_inv3_unit_id_rule(bad):
    with pytest.raises(InventoryRefused):
        parse_inventory(doc(units=[unit(unit_id=bad)]))


def test_inv3_duplicate_ids_refused():
    with pytest.raises(InventoryRefused, match="twice"):
        parse_inventory(doc(units=[unit(), unit()]))


def test_inv4_implicit_inventory():
    inv = implicit_inventory(["drone-1", "drone-2"], {"drone-2": "tello_edu"})
    assert [(u["unit_id"], u["model"]) for u in inv.units] == [("drone-1", "tello"), ("drone-2", "tello_edu")]
    assert inv.rev == 0 and len(inv.available_units()) == 2
    with pytest.raises(InventoryRefused):
        implicit_inventory(["bad id"])


def test_inv6_models_must_be_builtin_or_defined():
    with pytest.raises(InventoryRefused, match="neither built in"):
        parse_inventory(doc(units=[unit(model="site_hex")]))
    inv = parse_inventory(doc(models={"site_hex": HEX}, units=[unit(unit_id="hex-1", model="site_hex")]))
    assert isinstance(inv, Inventory) and inv.models["site_hex"].bounds["envelope.ceiling_cm"].lo == 2000


def test_custom_model_cannot_widen_bounds():
    wide = {**HEX, "param_bounds": {"envelope.ceiling_cm": {"lo": 100, "hi": 60000}}}
    with pytest.raises(InventoryRefused, match="widens"):
        parse_inventory(doc(models={"site_hex": wide}))


@pytest.mark.parametrize("field", ["environment", "indoor_profile_measured"])
def test_cap5_custom_model_cannot_set_environment(field):
    with pytest.raises(InventoryRefused):
        parse_inventory(doc(models={"site_hex": {**HEX, field: True}}))


def test_fact_row_id_in_unit_is_refused():
    for record in (unit(takeoff={"takeoff.late_s": 5}), unit(features={"takeoff.late_s": True}),
                   {**unit(), "takeoff.settle_samples": 3}):
        with pytest.raises(InventoryRefused, match="not a setting"):
            parse_inventory(doc(units=[record]))


def test_safety_switch_off_and_unknown_feature_refused():
    with pytest.raises(InventoryRefused, match="safety switch"):
        parse_inventory(doc(units=[unit(features={"forward_obstacle_sensing": False})]))
    with pytest.raises(InventoryRefused, match="unknown feature"):
        parse_inventory(doc(units=[unit(features={"nope": True})]))


def test_unit_mount_needs_numeric_pitch():
    with pytest.raises(InventoryRefused, match="mission_pitch_deg"):
        parse_inventory(doc(units=[unit(mounts={"gimbal": {"mission_pitch_deg": "down"}})]))


def test_inv7_stale_rev_refused(tmp_path):
    store = InventoryStore(tmp_path / "inventory.json")
    store.write(doc(), 0)
    with pytest.raises(InventoryRefused) as err:
        store.write(doc(), 0)
    assert str(err.value) == STALE_REV
    assert store.load().rev == 1


def test_inv7_two_threads_same_rev_one_success(tmp_path):
    store = InventoryStore(tmp_path / "inventory.json")
    store.write(doc(), 0)
    barrier = threading.Barrier(2)
    results = []

    def writer(name):
        barrier.wait()
        try:
            store.write(doc(units=[unit(name=name)]), 1)
            results.append("ok")
        except InventoryRefused as exc:
            results.append(str(exc))

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == sorted(["ok", STALE_REV])
    assert store.load().rev == 2
