"""Stand-ins for the MiDaS weights and a poisoned torch.hub cache, for the supply-chain tests.

``synthetic_state_dict`` gives every parameter and buffer of MiDaS v2.1 Small a value from
its own seeded generator, so a test can build the real model with known weights without the
86 MB release file. The same function produced the reference output that the vendored model
is compared with (computed once from the upstream code at the pinned commits).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import torch

#: Marker a poisoned hubconf writes into its cache folder when torch.hub imports it.
HUB_MARKER = "HUBCONF_RAN"

_POISONED_HUBCONF = '''\
import pathlib
pathlib.Path(__file__).with_name("{marker}").write_text("ran")

def MiDaS_small(*args, **kwargs):
    raise RuntimeError("poisoned hubconf entry point ran")

def transforms(*args, **kwargs):
    raise RuntimeError("poisoned hubconf entry point ran")

def tf_efficientnet_lite3(*args, **kwargs):
    raise RuntimeError("poisoned hubconf entry point ran")
'''


def synthetic_state_dict(template: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    """Deterministic weights with *template*'s names, shapes and dtypes."""
    out = {}
    for i, name in enumerate(sorted(template)):
        t = template[name]
        g = torch.Generator().manual_seed(1000 + i)
        if not t.is_floating_point():
            out[name] = torch.zeros_like(t)
        elif name == "scratch.output_conv.4.bias":
            # Lifts the last layer above its ReLU, so most of the map carries a value.
            out[name] = torch.full(t.shape, 0.3)
        elif name.endswith("running_var"):
            out[name] = torch.rand(t.shape, generator=g) * 0.5 + 0.75
        elif name.endswith("running_mean"):
            out[name] = torch.randn(t.shape, generator=g) * 0.05
        elif t.dim() > 1:
            out[name] = torch.randn(t.shape, generator=g) / math.sqrt(t[0].numel())
        elif name.endswith(".weight"):
            out[name] = 1.0 + torch.randn(t.shape, generator=g) * 0.05
        else:
            out[name] = torch.randn(t.shape, generator=g) * 0.05
    return out


def reference_image() -> np.ndarray:
    """The fixed RGB image the reference output was computed from."""
    return np.random.default_rng(7).integers(0, 256, (120, 160, 3), dtype=np.uint8)


def output_fingerprint(depth: torch.Tensor) -> dict:
    """Mean, standard deviation and a 4x4 grid of samples of a 1xHxW depth map."""
    d = depth.detach().double()
    return {
        "shape": list(d.shape),
        "mean": float(d.mean()),
        "std": float(d.std()),
        "samples": [float(v) for v in d[0, ::48, ::64].flatten()],
    }


def poison_hub_cache(hub_dir: Path, repo_dirs: tuple[str, ...]) -> None:
    """Put a hubconf that writes ``HUB_MARKER`` when imported into each of *repo_dirs*."""
    hub_dir.mkdir(parents=True, exist_ok=True)
    for name in repo_dirs:
        repo = hub_dir / name
        repo.mkdir()
        (repo / "hubconf.py").write_text(_POISONED_HUBCONF.format(marker=HUB_MARKER))


def hub_markers(hub_dir: Path) -> list[Path]:
    """Every marker a poisoned hubconf left, anywhere under *hub_dir*."""
    return sorted(hub_dir.rglob(HUB_MARKER))
