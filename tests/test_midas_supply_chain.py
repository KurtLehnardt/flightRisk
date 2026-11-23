"""MiDaS supply chain: pinned code, SHA-256 checked weights, no prompt, no download in flight.

These tests use the real ``torch.hub`` (nothing in it is mocked) against a fake hub cache in
a temporary TORCH_HOME, with the network blocked and stdin closed. The cache holds
``intel-isl_MiDaS_master`` and ``isl-org_MiDaS_master`` folders whose ``hubconf.py`` writes a
marker when imported, which is what a compromised upstream or a reclaimed organisation name
would put there. Control tests show that the old loading path runs that code and that torch
asks on stdin for an untrusted repository, so the other tests' "never ran" and "never asked"
are not vacuous.

The weights are a deterministic stand-in (``tests/support/midas_weights.py``) with the real
model's layout; each test pins its SHA-256 the way ``midas_model.WEIGHTS_SHA256`` pins the
release file.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import ipaddress
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch

from flightrisk.dashboard.app import app as dashboard_app
from flightrisk.dashboard.app import socketio as dashboard_socketio
from flightrisk.dashboard.state import app_state
from flightrisk.drone import midas_model
from flightrisk.drone.midas_model import DownloadFailed, MidasUnavailable
from flightrisk.third_party.midas_small import midas_small, small_transform
from tests.single_drone_doubles import ready_single_drone
from tests.support.midas_weights import (
    hub_markers,
    output_fingerprint,
    poison_hub_cache,
    reference_image,
    synthetic_state_dict,
)
from tests.test_fleet_dashboard import edge_pipeline  # noqa: F401 - shared fixture

ROOT = Path(__file__).resolve().parent.parent

#: Output of upstream MiDaS_small (isl-org/MiDaS 4545977, gen-efficientnet 771ce08) with the
#: synthetic weights on reference_image(), computed once from the upstream code.
UPSTREAM_REFERENCE = {
    "shape": [1, 192, 256],
    "mean": 0.050151862743708385,
    "std": 0.09742307983189367,
    "samples": [
        0.3243477940559387, 0.4493229389190674, 0.4725992679595947, 0.44268232583999634,
        0.5254075527191162, 0.1286429762840271, 0.09738418459892273, 0.30943354964256287,
        0.6502023935317993, 0.0, 0.0, 0.0, 0.4703361392021179, 0.0, 0.0, 0.0,
    ],
}

#: SHA-256 of "name shape dtype" lines of the real midas_v21_small_256.pt, sorted by name.
PINNED_WEIGHTS_LAYOUT = "cadc628834d011acb2e9929c2629e95a5e5028e679536b1599aa2a0439e210b5"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def weights_file(tmp_path_factory) -> tuple[Path, str]:
    """A MiDaS-shaped weights file with known values, and its SHA-256."""
    path = tmp_path_factory.mktemp("midas") / midas_model.WEIGHTS_FILENAME
    torch.save(synthetic_state_dict(midas_small().state_dict()), path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def pinned(weights_file, monkeypatch) -> Path:
    """Pin the stand-in weights: midas_model now expects exactly that file at that path."""
    path, digest = weights_file
    monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", path)
    monkeypatch.setattr(midas_model, "WEIGHTS_SHA256", digest)
    return path


def _is_loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def network_blocked(monkeypatch) -> list:
    """Every connection or lookup of a non-loopback host raises; the attempts are recorded."""
    attempts: list = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            attempts.append(host)
            raise OSError(f"network blocked in this test: lookup of {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if isinstance(address, tuple) and not _is_loopback(host):
            attempts.append(host)
            raise OSError(f"network blocked in this test: connect to {address}")
        return real_connect(self, address)

    def connect_ex(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if isinstance(address, tuple) and not _is_loopback(host):
            attempts.append(host)
            raise OSError(f"network blocked in this test: connect to {address}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return attempts


@pytest.fixture
def stdin_closed(monkeypatch) -> list:
    """stdin is closed; every call of input() is recorded and then fails as it would."""
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdin", closed)
    calls: list = []
    real_input = builtins.input

    def recording_input(*args, **kwargs):
        calls.append(args)
        return real_input(*args, **kwargs)

    monkeypatch.setattr(builtins, "input", recording_input)
    return calls


@pytest.fixture
def poisoned_hub(tmp_path, monkeypatch) -> Path:
    """A TORCH_HOME whose hub cache holds unpinned MiDaS code that leaves a marker when run."""
    torch_home = tmp_path / "torch"
    hub_dir = torch_home / "hub"
    poison_hub_cache(hub_dir, ("intel-isl_MiDaS_master", "isl-org_MiDaS_master"))
    (hub_dir / "trusted_list").write_text("intel-isl_MiDaS\n")
    monkeypatch.setenv("TORCH_HOME", str(torch_home))
    monkeypatch.delenv("TORCH_HUB", raising=False)
    monkeypatch.setattr(torch.hub, "_hub_dir", None)
    assert torch.hub.get_dir() == str(hub_dir)
    return hub_dir


def _hub_snapshot(hub_dir: Path) -> tuple[list[str], str]:
    files = sorted(str(p.relative_to(hub_dir)) for p in hub_dir.rglob("*"))
    return files, (hub_dir / "trusted_list").read_text()


def _malicious_pickle(path: Path, marker: Path) -> None:
    """A torch file whose unpickling writes *marker*: code execution through the weights."""

    class Payload:
        def __reduce__(self):
            return (exec, (f"open({str(marker)!r}, 'w').write('ran')",))

    torch.save({"pretrained.layer1.0.weight": Payload()}, path)


# ---------------------------------------------------------------------------
# Controls: the hazards are live in this environment
# ---------------------------------------------------------------------------


class TestControls:
    def test_the_old_loading_path_runs_unpinned_cached_code(self, poisoned_hub, network_blocked, stdin_closed):
        """What ObstacleGuard used to call: torch.hub imports the cached hubconf, no questions asked."""
        with pytest.raises(RuntimeError, match="poisoned hubconf entry point ran"):
            torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True)
        assert hub_markers(poisoned_hub), "the poisoned hubconf did not run, so the tests below prove nothing"

    def test_an_untrusted_repository_asks_on_stdin(self, poisoned_hub, network_blocked, stdin_closed):
        """What MiDaS's own blocks.py does: torch.hub.load of gen-efficientnet without trust_repo."""
        with pytest.raises((ValueError, EOFError, OSError)):
            torch.hub.load("rwightman/gen-efficientnet-pytorch:master", "tf_efficientnet_lite3")
        assert len(stdin_closed) == 1, "torch did not ask, so the no-prompt tests below prove nothing"

    def test_the_network_is_blocked(self, network_blocked):
        with pytest.raises(OSError):
            socket.create_connection(("github.com", 443), timeout=1)
        assert network_blocked == ["github.com"]


# ---------------------------------------------------------------------------
# No prompt, no unpinned code, no download
# ---------------------------------------------------------------------------


class TestNoPrompt:
    def test_guard_loads_with_stdin_closed_and_network_blocked(
        self, pinned, poisoned_hub, network_blocked, stdin_closed,
    ):
        from flightrisk.drone.obstacle import ObstacleGuard

        before = _hub_snapshot(poisoned_hub)
        guard = ObstacleGuard(check_interval=0.0)
        result = guard.check_path(np.full((120, 160, 3), 96, dtype=np.uint8))

        assert set(result) >= {"safe", "action", "center_depth"}
        assert stdin_closed == []
        assert network_blocked == []
        assert hub_markers(poisoned_hub) == []
        assert _hub_snapshot(poisoned_hub) == before  # nothing fetched, nothing trusted

    def test_console_start_with_closed_stdin_in_a_fresh_process(self, pinned, poisoned_hub):
        """The reported failure: a console start whose stdin is a pipe raised EOFError."""
        script = textwrap.dedent("""
            import os, socket, sys
            sys.stdin.close()

            def blocked(*args, **kwargs):
                raise OSError("network blocked in this test")

            socket.getaddrinfo = blocked
            socket.create_connection = blocked
            socket.socket.connect = blocked

            from flightrisk.drone import midas_model
            midas_model.DEFAULT_WEIGHTS_PATH = midas_model.Path(os.environ["TEST_MIDAS_WEIGHTS"])
            midas_model.WEIGHTS_SHA256 = os.environ["TEST_MIDAS_SHA256"]

            import numpy as np
            from flightrisk.drone.obstacle import ObstacleGuard

            guard = ObstacleGuard(check_interval=0.0)
            result = guard.check_path(np.full((120, 160, 3), 96, dtype=np.uint8))
            print("LOADED", result["action"])
        """)
        env = {
            **os.environ,
            "TORCH_HOME": str(poisoned_hub.parent),
            "TEST_MIDAS_WEIGHTS": str(pinned),
            "TEST_MIDAS_SHA256": midas_model.WEIGHTS_SHA256,
            "PYTHONPATH": str(ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=180, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "LOADED" in proc.stdout
        assert "trust" not in proc.stdout.lower() + proc.stderr.lower()
        assert hub_markers(poisoned_hub) == []


class TestUnpinnedRepoRefused:
    def test_hub_loads_are_refused_while_the_model_is_built(self, poisoned_hub, network_blocked, stdin_closed):
        real_load = torch.hub.load
        with midas_model._hub_refused():
            with pytest.raises(MidasUnavailable, match="refused torch.hub.load"):
                torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True)
            with pytest.raises(MidasUnavailable, match="refused torch.hub.load"):
                torch.hub.load("rwightman/gen-efficientnet-pytorch", "tf_efficientnet_lite3", pretrained=True)
            with pytest.raises(MidasUnavailable, match="refused torch.hub.load_state_dict_from_url"):
                torch.hub.load_state_dict_from_url(midas_model.WEIGHTS_URL)
        assert torch.hub.load is real_load
        assert hub_markers(poisoned_hub) == []
        assert stdin_closed == []
        assert network_blocked == []

    def test_upstreams_inner_hub_load_is_refused_if_it_comes_back(
        self, pinned, poisoned_hub, network_blocked, stdin_closed, monkeypatch,
    ):
        """Reintroduce upstream's blocks.py call: the load refuses instead of fetching or asking."""
        from flightrisk.third_party.midas_small import midas_net_small

        def upstream_style():
            effnet = torch.hub.load(
                "rwightman/gen-efficientnet-pytorch", "tf_efficientnet_lite3", pretrained=True, exportable=True,
            )
            return midas_net_small._make_efficientnet_backbone(effnet)

        monkeypatch.setattr(midas_net_small, "_make_pretrained_efficientnet_lite3", upstream_style)
        with pytest.raises(MidasUnavailable, match="rwightman/gen-efficientnet-pytorch"):
            midas_model.load_midas_small()
        assert stdin_closed == []
        assert network_blocked == []
        assert hub_markers(poisoned_hub) == []

    def test_obstacle_guard_never_calls_torch_hub(self, pinned, monkeypatch):
        from flightrisk.drone.obstacle import ObstacleGuard

        calls = []
        for name in ("load", "list", "help", "load_state_dict_from_url", "download_url_to_file"):
            monkeypatch.setattr(torch.hub, name, lambda *a, _n=name, **k: calls.append(_n))
        ObstacleGuard(check_interval=0.0)
        assert calls == []


# ---------------------------------------------------------------------------
# Weights: pinned SHA-256, weights_only, fail closed
# ---------------------------------------------------------------------------


class TestWeights:
    def test_hash_mismatch_is_refused_before_unpickling(self, pinned, tmp_path, monkeypatch):
        marker = tmp_path / "pickle_ran"
        bad = tmp_path / "tampered.pt"
        _malicious_pickle(bad, marker)
        monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", bad)

        from flightrisk.drone.obstacle import ObstacleGuard

        with pytest.raises(MidasUnavailable, match="do not match the pinned SHA-256") as info:
            ObstacleGuard()
        assert midas_model.INSTALL_COMMAND in str(info.value)
        assert not marker.exists()

    def test_a_single_flipped_byte_is_refused(self, pinned, tmp_path, monkeypatch):
        data = bytearray(pinned.read_bytes())
        data[len(data) // 2] ^= 0x01
        flipped = tmp_path / "flipped.pt"
        flipped.write_bytes(bytes(data))
        monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", flipped)
        with pytest.raises(MidasUnavailable, match="do not match the pinned SHA-256"):
            midas_model.load_midas_small()

    def test_code_in_the_pickle_never_runs_even_with_a_matching_digest(self, tmp_path):
        """weights_only=True: a pinned digest of a hostile file still does not execute it."""
        marker = tmp_path / "pickle_ran"
        hostile = tmp_path / "hostile.pt"
        _malicious_pickle(hostile, marker)
        digest = hashlib.sha256(hostile.read_bytes()).hexdigest()
        with pytest.raises(MidasUnavailable, match="not a plain state dict of tensors"):
            midas_model.load_midas_small(hostile, digest)
        assert not marker.exists()

    def test_missing_weights_fail_closed_naming_the_install_step(self, tmp_path, monkeypatch, network_blocked):
        monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", tmp_path / "models" / midas_model.WEIGHTS_FILENAME)
        downloads = []
        monkeypatch.setattr(midas_model, "urlopen", lambda *a, **k: downloads.append(a))

        from flightrisk.drone.obstacle import ObstacleGuard

        with pytest.raises(MidasUnavailable, match="not installed") as info:
            ObstacleGuard()
        assert midas_model.INSTALL_COMMAND in str(info.value)
        assert downloads == [] and network_blocked == []

    def test_wrong_layout_is_refused(self, tmp_path):
        state = synthetic_state_dict(midas_small().state_dict())
        state.pop(min(state))
        path = tmp_path / "short.pt"
        torch.save(state, path)
        with pytest.raises(MidasUnavailable, match="do not fit MiDaS v2.1 Small"):
            midas_model.load_midas_small(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_the_loaded_model_is_in_eval_mode_with_the_pinned_weights(self, pinned):
        model, _transform = midas_model.load_midas_small()
        assert not model.training
        expected = synthetic_state_dict(midas_small().state_dict())
        loaded = model.state_dict()
        assert all(torch.equal(loaded[k], expected[k]) for k in expected)

    def test_pins_are_full_length(self):
        assert len(midas_model.MIDAS_COMMIT) == 40 and len(midas_model.GEN_EFFICIENTNET_COMMIT) == 40
        assert len(midas_model.WEIGHTS_SHA256) == 64
        assert midas_model.WEIGHTS_URL.startswith("https://github.com/isl-org/MiDaS/releases/download/")


# ---------------------------------------------------------------------------
# The vendored code is upstream's model
# ---------------------------------------------------------------------------


class TestVendoredModel:
    def test_layout_matches_the_pinned_weights(self):
        state = midas_small().state_dict()
        lines = "\n".join(f"{k} {tuple(t.shape)} {t.dtype}" for k, t in sorted(state.items()))
        assert hashlib.sha256(lines.encode()).hexdigest() == PINNED_WEIGHTS_LAYOUT

    def test_output_matches_upstream_at_the_pinned_commits(self):
        model = midas_small()
        model.load_state_dict(synthetic_state_dict(model.state_dict()), strict=True)
        model.eval()
        with torch.no_grad():
            out = model(small_transform()(reference_image()))
        got = output_fingerprint(out)
        assert got["shape"] == UPSTREAM_REFERENCE["shape"]
        assert got["mean"] == pytest.approx(UPSTREAM_REFERENCE["mean"], rel=1e-3)
        assert got["std"] == pytest.approx(UPSTREAM_REFERENCE["std"], rel=1e-3)
        assert got["samples"] == pytest.approx(UPSTREAM_REFERENCE["samples"], rel=1e-3, abs=1e-4)

    def test_the_vendored_code_neither_imports_timm_nor_touches_torch_hub(self):
        import ast

        import flightrisk.third_party.midas_small as pkg

        for path in Path(pkg.__file__).parent.glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    names = []
                assert not any(n.startswith(("timm", "torch.hub")) for n in names), (path.name, names)
                is_hub = (isinstance(node, ast.Attribute) and node.attr == "hub"
                          and isinstance(node.value, ast.Name) and node.value.id == "torch")
                assert not is_hub, f"{path.name}:{node.lineno} uses torch.hub"


# ---------------------------------------------------------------------------
# install: the only download, pinned URL, pinned digest
# ---------------------------------------------------------------------------


class _Release:
    """Stands in for the GitHub release download (urlopen of WEIGHTS_URL)."""

    def __init__(self, data: bytes | None = None, error: Exception | None = None):
        self.data, self.error, self.urls = data, error, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return io.BytesIO(self.data)


class TestInstall:
    @pytest.fixture
    def dest(self, weights_file, tmp_path, monkeypatch) -> Path:
        _path, digest = weights_file
        monkeypatch.setattr(midas_model, "WEIGHTS_SHA256", digest)
        target = tmp_path / "models" / midas_model.WEIGHTS_FILENAME
        monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", target)
        return target

    def test_downloads_the_pinned_url_and_verifies(self, dest, weights_file, monkeypatch):
        release = _Release(weights_file[0].read_bytes())
        monkeypatch.setattr(midas_model, "urlopen", release)
        assert midas_model.main(["install"]) == 0
        assert release.urls == [midas_model.WEIGHTS_URL]
        assert dest.read_bytes() == weights_file[0].read_bytes()
        assert sorted(p.name for p in dest.parent.iterdir()) == [dest.name]
        assert midas_model.main(["verify"]) == 0

    def test_a_download_that_does_not_match_is_deleted(self, dest, monkeypatch):
        monkeypatch.setattr(midas_model, "urlopen", _Release(b"not the weights"))
        with pytest.raises(MidasUnavailable, match="does not match the pinned SHA-256"):
            midas_model.install()
        assert list(dest.parent.iterdir()) == []
        assert midas_model.main(["install"]) == 1

    def test_installed_weights_are_not_downloaded_again(self, dest, weights_file, monkeypatch):
        dest.parent.mkdir(parents=True)
        dest.write_bytes(weights_file[0].read_bytes())
        release = _Release(error=AssertionError("downloaded again"))
        monkeypatch.setattr(midas_model, "urlopen", release)
        assert midas_model.install() == dest
        assert release.urls == []

    def test_a_wrong_file_is_replaced_by_the_verified_download(self, dest, weights_file, monkeypatch):
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"stale")
        monkeypatch.setattr(midas_model, "urlopen", _Release(weights_file[0].read_bytes()))
        midas_model.install()
        assert hashlib.sha256(dest.read_bytes()).hexdigest() == midas_model.WEIGHTS_SHA256

    def test_offline_install_says_so_and_leaves_nothing(self, dest, monkeypatch):
        monkeypatch.setattr(midas_model, "urlopen", _Release(error=urllib.error.URLError("offline")))
        with pytest.raises(DownloadFailed):
            midas_model.install()
        assert list(dest.parent.iterdir()) == []
        assert midas_model.main(["install"]) == 2

    def test_verify_fails_when_missing(self, dest):
        assert midas_model.main(["verify"]) == 1


# ---------------------------------------------------------------------------
# The ground loads MiDaS only for the single-drone search
# ---------------------------------------------------------------------------


@pytest.fixture
def load_spy(monkeypatch) -> list:
    """Record every load_midas_small call (and still load)."""
    calls: list = []
    real = midas_model.load_midas_small

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(midas_model, "load_midas_small", spy)
    return calls


def test_fleet_mode_ground_never_loads_midas(
    edge_pipeline, pinned, poisoned_hub, network_blocked, stdin_closed, load_spy,  # noqa: F811 - the imported fixture
):
    app_state.obstacle_guard = None
    edge_pipeline.start(fleet_drones=("drone-1", "drone-2"))
    assert app_state.fleet_supervisor is not None

    # edge_pipeline swaps the module's socketio for a recorder; the handlers live on the real one.
    client = dashboard_socketio.test_client(dashboard_app)
    try:
        client.emit("start_search", {"pattern": "parallel_track"})  # edge mode: no local drone
        time.sleep(0.2)
    finally:
        client.disconnect()

    assert load_spy == []
    assert app_state.obstacle_guard is None
    assert hub_markers(poisoned_hub) == [] and stdin_closed == [] and network_blocked == []


@pytest.fixture
def single_drone(clean_app_state, monkeypatch):
    drone = MagicMock()
    drone.state.is_flying = True
    drone.get_frame.return_value = None
    drone.move.return_value = True
    drone.rotate.return_value = True
    ready_single_drone(drone)
    app_state.fleet = MagicMock(primary=drone)
    app_state.fleet_supervisor = None
    app_state.obstacle_guard = None
    monkeypatch.setattr("flightrisk.dashboard.app.time.sleep", lambda s: None)
    client = dashboard_socketio.test_client(dashboard_app)
    client.get_received()
    yield drone, client
    app_state.search_active = False
    client.disconnect()


def test_single_drone_search_builds_the_guard_when_it_starts(single_drone, pinned, load_spy):
    from flightrisk.drone.obstacle import ObstacleGuard

    _drone, client = single_drone
    client.emit("start_search", {"pattern": "parallel_track"})
    received = client.get_received()
    assert [r["name"] for r in received if r["name"] == "search_started"] == ["search_started"]
    assert isinstance(app_state.obstacle_guard, ObstacleGuard)
    assert len(load_spy) == 1


def test_single_drone_search_refuses_without_the_guard(single_drone, tmp_path, monkeypatch):
    monkeypatch.setattr(midas_model, "DEFAULT_WEIGHTS_PATH", tmp_path / "absent.pt")
    drone, client = single_drone
    client.emit("start_search", {"pattern": "parallel_track"})
    received = client.get_received()
    errors = [r["args"][0]["message"] for r in received if r["name"] == "error"]
    assert errors and "obstacle guard is unavailable" in errors[0]
    assert midas_model.INSTALL_COMMAND in errors[0]
    assert not any(r["name"] == "search_started" for r in received)
    assert app_state.search_active is False
    assert app_state.obstacle_guard is None
    time.sleep(0.1)
    drone.move.assert_not_called()

