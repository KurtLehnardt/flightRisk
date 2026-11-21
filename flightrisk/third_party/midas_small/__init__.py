"""MiDaS v2.1 Small: the model code the obstacle guard runs, vendored from pinned commits.

* ``midas_net_small.py``: from https://github.com/isl-org/MiDaS at commit
  454597711a62eabcbf7d1e89f3fb9f569051ac9b (MIT License, ``LICENSE-MiDaS``).
* ``efficientnet_lite.py``: from https://github.com/rwightman/gen-efficientnet-pytorch at
  commit 771ce082b2ce6d033f55b3d47c1f77389ad3c180 (Apache License 2.0,
  ``LICENSE-gen-efficientnet``).

Upstream builds this model through ``torch.hub``, which downloads the newest code of both
repositories (MiDaS's ``hubconf.py`` loads gen-efficientnet itself, without a ref) and can
ask on stdin whether to trust them. Vendoring the few hundred lines this model needs means
no repository code is fetched or trusted at run time. The weights are loaded and verified by
``flightrisk.drone.midas_model``. Each file's docstring lists what was changed from upstream.
"""

from .midas_net_small import MidasNet_small, midas_small, small_transform

__all__ = ["MidasNet_small", "midas_small", "small_transform"]
