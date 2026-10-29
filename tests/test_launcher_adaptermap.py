"""No hardcoded adapter addresses: adapters are detected, proposed and remembered by name."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import flightrisk.launcher as launcher_package
from flightrisk.launcher.adaptermap import (
    AUTO,
    REMEMBERED,
    confirmed_mapping,
    propose_adapters,
)
from flightrisk.launcher.netinfo import Adapter
from flightrisk.launcher.settings import (
    DroneSettings,
    LauncherSettings,
    PresetStore,
    builtin_presets,
    errors,
    validate,
)

LO = Adapter("lo0", "127.0.0.1", 1, True)
WIFI = Adapter("Wi-Fi", "192.168.10.7", 12, True)
WIFI2 = Adapter("Wi-Fi 2", "192.168.10.31", 23, True)
CABLE = Adapter("Ethernet", "10.42.0.1", 5, True)


def _two_real() -> LauncherSettings:
    return builtin_presets()["Layout B (everything here)"]


def _one_real() -> LauncherSettings:
    return builtin_presets()["Single drone"]


def test_no_preset_holds_an_adapter_address_or_interface_index():
    for name, preset in builtin_presets().items():
        assert all(d.local_ip == "" and d.ifindex is None for d in preset.drones), name


def test_no_adapter_address_is_hardcoded_anywhere_in_the_launcher():
    # 192.168.10.1 is the Tello itself and 192.168.10.0/24 its network; any other address there is an adapter.
    pattern = re.compile(r"192\.168\.10\.(?!1\b)(?!0/24)\d+")
    root = Path(launcher_package.__file__).parent
    hits = [f"{p.name}: {m.group(0)}" for p in root.rglob("*") if p.suffix in {".py", ".js", ".html", ".css"}
            for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_one_drone_and_one_candidate_is_preselected_as_auto_detected():
    [p] = propose_adapters(_one_real(), [LO, WIFI, CABLE])
    assert p.selected == WIFI and p.reason == AUTO and "auto-detected" in p.note
    assert p.candidates == (WIFI,), "the cable adapter is not in the Tello host's network"


def test_two_candidates_for_one_drone_preselect_nothing_and_validation_keeps_failing():
    adapters = [LO, WIFI, WIFI2]
    [p] = propose_adapters(_one_real(), adapters)
    assert p.selected is None and p.reason is None and p.candidates == (WIFI, WIFI2) and "choose the adapter" in p.note
    messages = [i.message for i in errors(validate(_one_real(), adapters=adapters))]
    assert any("choose the adapter" in m for m in messages)
    # Once the operator picks one, the error is gone.
    picked = replace(_one_real(), drones=(replace(_one_real().drones[0], local_ip=WIFI2.ipv4),))
    assert not any("choose the adapter" in i.message for i in errors(validate(picked, adapters=adapters)))


def test_two_drones_are_never_preselected_even_with_candidates():
    for adapters in ([LO, WIFI], [LO, WIFI, WIFI2]):
        proposals = propose_adapters(_two_real(), adapters)
        assert [p.selected for p in proposals] == [None, None]
    messages = [i.message for i in errors(validate(_two_real(), adapters=[LO, WIFI, WIFI2]))]
    assert any("needs the local IP" in m or "choose the adapter" in m for m in messages)


def test_candidates_come_in_a_stable_order():
    shuffled = propose_adapters(_one_real(), [WIFI2, LO, WIFI])
    assert [a.name for a in shuffled[0].candidates] == ["Wi-Fi", "Wi-Fi 2"]
    assert propose_adapters(_one_real(), [WIFI, WIFI2]) [0].candidates == shuffled[0].candidates


def test_a_remembered_adapter_is_found_again_by_name_after_its_address_changes():
    renewed = Adapter("Wi-Fi 2", "192.168.10.99", 23, True)  # DHCP gave the adapter another address
    proposals = propose_adapters(_two_real(), [LO, WIFI, renewed], {"drone-1": "Wi-Fi", "drone-2": "Wi-Fi 2"})
    assert [(p.selected.name, p.selected.ipv4, p.reason) for p in proposals] == [
        ("Wi-Fi", "192.168.10.7", REMEMBERED), ("Wi-Fi 2", "192.168.10.99", REMEMBERED)]


def test_a_remembered_adapter_that_is_gone_or_shared_is_not_preselected():
    assert propose_adapters(_two_real(), [LO, WIFI, WIFI2], {"drone-1": "Wi-Fi 3"})[0].selected is None
    both = propose_adapters(_two_real(), [LO, WIFI, WIFI2], {"drone-1": "Wi-Fi", "drone-2": "Wi-Fi"})
    assert [p.selected for p in both] == [None, None]


def test_a_remembered_choice_applies_to_the_same_drone_id_only():
    assert propose_adapters(_one_real(), [LO, WIFI, WIFI2], {"drone-7": "Wi-Fi"})[0].selected is None
    assert propose_adapters(_one_real(), [LO, WIFI, WIFI2], {"drone-1": "Wi-Fi 2"})[0].selected == WIFI2


def test_fake_drones_and_drones_on_other_computers_need_no_adapter():
    rehearsal = builtin_presets()["Rehearsal with fake drones"]
    assert propose_adapters(rehearsal, []) == []
    assert errors(validate(rehearsal, adapters=[LO])) == []
    layout_a = builtin_presets()["Layout A (ground here)"]
    assert propose_adapters(layout_a, [LO, WIFI]) == []


def test_no_candidate_says_to_join_the_drones_wifi():
    [p] = propose_adapters(_one_real(), [LO, CABLE])
    assert p.selected is None and p.candidates == () and "no adapter" in p.note


def test_confirmed_mapping_uses_adapter_names_not_addresses():
    settings = replace(_two_real(), drones=(replace(_two_real().drones[0], local_ip=WIFI.ipv4),
                                            replace(_two_real().drones[1], local_ip="192.168.10.200")))
    assert confirmed_mapping(settings, [LO, WIFI, WIFI2]) == {"drone-1": "Wi-Fi"}


def test_the_mapping_is_stored_by_adapter_name_apart_from_presets_and_never_with_the_token(tmp_path):
    path = tmp_path / "launcher.json"
    store = PresetStore(path, secret=lambda: "tok-SECRET-123")
    store.remember_adapters({"drone-1": "Wi-Fi", "drone-2": "Wi-Fi 2"})
    store.save_preset("mine", builtin_presets()["Single drone"])
    store.save_last(builtin_presets()["Single drone"])
    data = json.loads(path.read_text())
    assert data["adapter_choices"] == {"drone-1": {"adapter": "Wi-Fi"}, "drone-2": {"adapter": "Wi-Fi 2"}}
    assert "adapter_choices" not in data["presets"]["mine"] and "tok-SECRET-123" not in path.read_text()
    assert "192.168.10" not in json.dumps(data["adapter_choices"])
    assert PresetStore(path).adapter_choices() == {"drone-1": "Wi-Fi", "drone-2": "Wi-Fi 2"}
    # A later save of presets keeps the remembered mapping, and an update replaces only the drones named.
    store.remember_adapters({"drone-2": "Wi-Fi 3"})
    assert PresetStore(path).adapter_choices() == {"drone-1": "Wi-Fi", "drone-2": "Wi-Fi 3"}


def test_a_malformed_mapping_in_the_file_is_ignored(tmp_path):
    path = tmp_path / "launcher.json"
    path.write_text(json.dumps({"version": 1, "presets": {}, "last": None,
                                "adapter_choices": {"drone-1": "Wi-Fi", "drone-2": {"adapter": 5}, "drone-3": {"adapter": "ok"}}}))
    assert PresetStore(path).adapter_choices() == {"drone-3": "ok"}
    path.write_text(json.dumps({"version": 1, "adapter_choices": ["x"]}))
    assert PresetStore(path).adapter_choices() == {}


def test_drone_settings_default_to_no_adapter():
    d = DroneSettings()
    assert d.local_ip == "" and d.ifindex is None
