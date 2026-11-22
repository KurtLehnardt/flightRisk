"""MiDaS v2.1 Small for the obstacle guard, built offline from pinned and verified parts.

The obstacle guard used to call ``torch.hub.load("intel-isl/MiDaS", ...)``. That ran
whatever code the default branch of the repository held when the cache was empty, then
MiDaS's own ``hubconf`` loaded ``rwightman/gen-efficientnet-pytorch`` the same way without
``trust_repo`` (so torch asked "Do you trust this repository (y/N)?" on stdin), and the
weights were unpickled with ``weights_only=False`` and never hash-checked. Here:

* **Code.** The model code is vendored in ``flightrisk/third_party/midas_small`` from
  isl-org/MiDaS at ``MIDAS_COMMIT`` and rwightman/gen-efficientnet-pytorch at
  ``GEN_EFFICIENTNET_COMMIT``. It is reviewed in this repository and needs only torch,
  numpy and OpenCV (upstream's ``hubconf`` imports ``timm`` for backbones this model does
  not use). torch.hub is never called: while the model is built, ``torch.hub``'s loaders
  are replaced with ones that raise, so a repository can be neither fetched nor trusted,
  and nothing can prompt on stdin.
* **Weights.** ``midas_v21_small_256.pt`` from ``WEIGHTS_URL``. It is read once, its SHA-256
  is compared with ``WEIGHTS_SHA256`` before anything unpickles it, and those same bytes are
  loaded with ``weights_only=True`` (the file is a plain state dict of tensors), into the
  model with a strict ``load_state_dict``.
* **Offline.** Only ``install`` downloads, and only the pinned URL; the installers run it
  (``python -m flightrisk.drone.midas_model install``). Loading never downloads: a missing
  or different file raises ``MidasUnavailable`` with the install step in its message, and
  the edge then refuses to take off (obstacle sensor unavailable).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.request import urlopen

#: Commits the vendored model code was taken from (see flightrisk/third_party/midas_small).
MIDAS_COMMIT = "454597711a62eabcbf7d1e89f3fb9f569051ac9b"
GEN_EFFICIENTNET_COMMIT = "771ce082b2ce6d033f55b3d47c1f77389ad3c180"

#: The MiDaS v2.1 Small weights: the release asset MiDaS's hubconf names, and its digest.
WEIGHTS_URL = "https://github.com/isl-org/MiDaS/releases/download/v2_1/midas_v21_small_256.pt"
WEIGHTS_SHA256 = "70d6b9c891758c67f974a6097fb0c608c7ee67fb81ac3e5588847d5596d56fca"
WEIGHTS_SIZE = 85_761_505
WEIGHTS_FILENAME = "midas_v21_small_256.pt"

#: Where the weights live: the models folder of this checkout or install (git ignores it).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEIGHTS_PATH = REPO_ROOT / "models" / WEIGHTS_FILENAME

#: The one-time step that puts the weights in place, named in every refusal.
INSTALL_COMMAND = "python -m flightrisk.drone.midas_model install"
INSTALL_HINT = (
    f"run the FlightRisk installer again while online, or run `{INSTALL_COMMAND}` once "
    f"with FlightRisk's venv Python in {REPO_ROOT}"
)

# A file larger than this is refused before it is read (the real one is about 86 MB).
_MAX_WEIGHTS_BYTES = 4 * WEIGHTS_SIZE
_DOWNLOAD_TIMEOUT_S = 60.0
_CHUNK = 1 << 20

# torch.hub functions that fetch, trust or import repository code or download weights.
_HUB_FUNCTIONS = ("load", "list", "help", "load_state_dict_from_url", "download_url_to_file")

# One model build at a time: the torch.hub guard is process-wide while it is in place.
_load_lock = threading.Lock()


class MidasUnavailable(RuntimeError):
    """The MiDaS model cannot be loaded from the pinned, verified files."""


class DownloadFailed(MidasUnavailable):
    """The pinned weights could not be downloaded (offline, say)."""


def weights_path() -> Path:
    """The weights file ``load_midas_small`` and ``install`` use."""
    return DEFAULT_WEIGHTS_PATH


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_verified_weights(path: Path | str | None = None, expected_sha256: str | None = None) -> bytes:
    """The bytes of the weights file, refused unless their SHA-256 is the pinned one.

    The bytes are read once, so what was checked is exactly what is later loaded.
    """
    path = Path(path) if path is not None else weights_path()
    expected = (expected_sha256 or WEIGHTS_SHA256).lower()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise MidasUnavailable(
            f"MiDaS weights are not installed (no {path}); nothing is downloaded during a flight: {INSTALL_HINT}"
        ) from None
    except OSError as exc:
        raise MidasUnavailable(f"MiDaS weights at {path} cannot be read ({exc}): {INSTALL_HINT}") from exc
    if size > _MAX_WEIGHTS_BYTES:
        raise MidasUnavailable(
            f"MiDaS weights at {path} are {size} bytes, not the pinned file; they were not loaded. "
            f"Delete the file and {INSTALL_HINT}"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MidasUnavailable(f"MiDaS weights at {path} cannot be read ({exc}): {INSTALL_HINT}") from exc
    actual = _sha256_hex(data)
    if actual != expected:
        raise MidasUnavailable(
            f"MiDaS weights at {path} do not match the pinned SHA-256 (expected {expected}, got {actual}); "
            f"they were not loaded. Delete the file and {INSTALL_HINT}"
        )
    return data


def _state_dict_from_bytes(data: bytes, source: Path) -> Mapping[str, Any]:
    import torch

    try:
        state = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    except Exception as exc:  # any refusal of the unpickler is a refusal of the file
        raise MidasUnavailable(
            f"MiDaS weights at {source} are not a plain state dict of tensors and were not loaded "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    if not isinstance(state, Mapping) or not all(
        isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in state.items()
    ):
        raise MidasUnavailable(f"MiDaS weights at {source} are not a state dict of tensors; they were not loaded")
    return state


@contextlib.contextmanager
def _hub_refused() -> Iterator[None]:
    """Make every torch.hub loader raise while the model is built.

    The vendored code never calls torch.hub; this keeps it that way. A call that would
    fetch or trust a repository (``rwightman/gen-efficientnet-pytorch`` from MiDaS's own
    ``blocks.py``, say) or download weights raises ``MidasUnavailable`` before torch reads
    its cache, checks its trust list or reads stdin.
    """
    from torch import hub

    saved = {name: getattr(hub, name) for name in _HUB_FUNCTIONS}

    def refuse(name: str) -> Callable[..., Any]:
        def refused(*args: Any, **kwargs: Any) -> Any:
            target = args[0] if args else kwargs.get("repo_or_dir") or kwargs.get("github") or kwargs.get("url")
            raise MidasUnavailable(
                f"refused torch.hub.{name}({target!r}) while building MiDaS: only the vendored model code "
                f"(MiDaS {MIDAS_COMMIT[:12]}, gen-efficientnet {GEN_EFFICIENTNET_COMMIT[:12]}) and the "
                "SHA-256 checked weights are used"
            )

        return refused

    for name in _HUB_FUNCTIONS:
        setattr(hub, name, refuse(name))
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(hub, name, fn)


def load_midas_small(
    path: Path | str | None = None, expected_sha256: str | None = None,
) -> tuple[Any, Callable[[Any], Any]]:
    """MiDaS v2.1 Small with the verified weights, in eval mode on the CPU, and its transform.

    Raises ``MidasUnavailable`` when the weights are missing, differ from the pinned file
    or are anything but a state dict of tensors. Never downloads and never reads stdin.
    """
    source = Path(path) if path is not None else weights_path()
    data = read_verified_weights(source, expected_sha256)
    state = _state_dict_from_bytes(data, source)
    del data
    from flightrisk.third_party.midas_small import midas_small, small_transform

    with _load_lock, _hub_refused():
        model = midas_small()
        try:
            model.load_state_dict(state, strict=True)
        except RuntimeError as exc:
            raise MidasUnavailable(f"MiDaS weights at {source} do not fit MiDaS v2.1 Small: {exc}") from exc
    model.eval()
    return model, small_transform()


# --- install: the only download ---------------------------------------------------------------


def _download(url: str, dest_dir: Path) -> tuple[Path, str]:
    """Stream *url* into a new file in *dest_dir*; return its path and SHA-256."""
    digest = hashlib.sha256()
    total = 0
    fd, tmp_name = tempfile.mkstemp(prefix=f".{WEIGHTS_FILENAME}.", suffix=".download", dir=dest_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as response:  # the pinned https URL
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_WEIGHTS_BYTES:
                    raise MidasUnavailable(f"{url} sent more than {_MAX_WEIGHTS_BYTES} bytes; not installed")
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, digest.hexdigest()


def install(path: Path | str | None = None) -> Path:
    """Put the pinned weights at *path* (default ``weights_path()``) and verify them.

    A file already there with the pinned SHA-256 is kept. Otherwise ``WEIGHTS_URL`` is
    downloaded beside it and moved into place only when its SHA-256 is the pinned one;
    a different download is deleted and refused. Raises ``DownloadFailed`` when the
    download itself fails and ``MidasUnavailable`` when the file is wrong.
    """
    dest = Path(path) if path is not None else weights_path()
    try:
        read_verified_weights(dest)
        return dest
    except MidasUnavailable:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp, actual = _download(WEIGHTS_URL, dest.parent)
    except MidasUnavailable:
        raise
    except Exception as exc:  # network and file errors all mean "not downloaded"
        raise DownloadFailed(f"could not download {WEIGHTS_URL} ({type(exc).__name__}: {exc})") from exc
    if actual != WEIGHTS_SHA256:
        tmp.unlink(missing_ok=True)
        raise MidasUnavailable(
            f"the download of {WEIGHTS_URL} does not match the pinned SHA-256 (expected {WEIGHTS_SHA256}, "
            f"got {actual}); it was deleted and nothing was installed"
        )
    os.replace(tmp, dest)
    read_verified_weights(dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    """``install``: download and verify the weights. ``verify``: check them, offline.

    Exit status: 0 done; 1 the file is missing or wrong (or the download did not match);
    2 the download failed (no internet, say), so run it again before flying.
    """
    parser = argparse.ArgumentParser(
        prog="python -m flightrisk.drone.midas_model",
        description="MiDaS obstacle-guard weights: pinned URL, pinned SHA-256, no download during flights.",
    )
    parser.add_argument("command", choices=("install", "verify"))
    parser.add_argument("--path", type=Path, default=None, help=f"weights file (default {DEFAULT_WEIGHTS_PATH})")
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            dest = install(args.path)
            print(f"MiDaS weights installed and verified: {dest} (sha256 {WEIGHTS_SHA256})")
        else:
            dest = Path(args.path) if args.path is not None else weights_path()
            read_verified_weights(dest)
            print(f"MiDaS weights verified: {dest} (sha256 {WEIGHTS_SHA256})")
    except DownloadFailed as exc:
        print(f"MiDaS weights not installed: {exc}", file=sys.stderr)
        return 2
    except MidasUnavailable as exc:
        print(f"MiDaS weights refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
