"""The ``--nodes`` option of scripts/demo_fake_fleet.py: two ground stations, edges on one."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_fake_fleet.py"
_spec = importlib.util.spec_from_file_location("demo_fake_fleet_nodes", SCRIPT)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_one_node_is_exactly_what_it_was():
    base = launcher.build_commands(3, 9000, 5555, python="py")
    assert launcher.build_commands(3, 9000, 5555, python="py", nodes=1, edge_node=1) == base
    assert [label for label, _ in base] == ["ground", "drone-1", "drone-2", "drone-3"]


def test_two_nodes_get_consecutive_ports_and_edges_attach_to_node_one():
    commands = dict(launcher.build_commands(2, 9000, 5555, python="py", nodes=2))
    assert list(commands) == ["ground-1", "ground-2", "drone-1", "drone-2"]
    assert (_flag(commands["ground-1"], "--edge-port"), _flag(commands["ground-1"], "--port")) == ("9000", "5555")
    assert (_flag(commands["ground-2"], "--edge-port"), _flag(commands["ground-2"], "--port")) == ("9001", "5556")
    for drone in ("drone-1", "drone-2"):
        assert _flag(commands[drone], "--ws-url") == "ws://127.0.0.1:9000"


def test_edge_node_two_points_the_edges_at_the_second_ground():
    commands = dict(launcher.build_commands(2, 9000, 5555, nodes=2, edge_node=2))
    assert _flag(commands["drone-1"], "--ws-url") == "ws://127.0.0.1:9001"


def test_attach_starts_no_ground_with_two_nodes():
    commands = launcher.build_commands(2, 9000, 5555, attach=True, nodes=2, edge_node=2)
    assert [label for label, _ in commands] == ["drone-1", "drone-2"]


@pytest.mark.parametrize("kwargs", [{"nodes": 0}, {"nodes": 3}, {"nodes": 2, "edge_node": 3}, {"nodes": 1, "edge_node": 2}])
def test_bad_node_arguments_are_rejected(kwargs):
    with pytest.raises(ValueError):
        launcher.build_commands(2, 9000, 5555, **kwargs)


def test_dry_run_lists_both_nodes_and_the_reserved_peer_ports(capsys):
    assert launcher.main(["--n", "2", "--nodes", "2", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("flightrisk.dashboard") == 2 and out.count("flightrisk.edge_device") == 2
    assert "peer :9100" in out and "peer :9101" in out
    assert "0.0.0.0" not in out


def test_dry_run_with_one_node_prints_no_node_summary(capsys):
    assert launcher.main(["--n", "2", "--dry-run"]) == 0
    assert "Ground nodes" not in capsys.readouterr().out


def test_a_bad_edge_node_exits_with_status_two(capsys):
    assert launcher.main(["--n", "2", "--nodes", "2", "--edge-node", "5", "--dry-run"]) == 2
    assert "--edge-node" in capsys.readouterr().err
