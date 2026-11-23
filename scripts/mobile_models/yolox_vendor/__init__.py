"""Inference-only YOLOX model code, vendored from Megvii-BaseDetection/YOLOX.

The files in this directory come from YOLOX at ``YOLOX_COMMIT`` under the
Apache License 2.0 (``LICENSE``; attribution and the list of changes are in
``NOTICE``). ``network_blocks.py``, ``darknet.py`` and ``yolo_pafpn.py`` are
unchanged. ``yolo_head.py`` and ``yolox.py`` are cut down to inference and say
so at the top. This ``__init__`` is new: it builds YOLOX-S the way upstream's
``exps/default/yolox_s.py`` and ``yolox/exp/yolox_base.py`` (``get_model``) do.

The code needs only torch. It is used by ``scripts/mobile_models/make_models.py``
to convert the pinned upstream weights; nothing in the FlightRisk runtime imports it.
"""

from __future__ import annotations

#: The upstream commit the files were taken from.
YOLOX_COMMIT = "6ddff4824372906469a7fae2dc3206c7aa4bbaee"

#: YOLOX-S hyperparameters from exps/default/yolox_s.py and yolox/exp/yolox_base.py.
YOLOX_S_DEPTH = 0.33
YOLOX_S_WIDTH = 0.50
YOLOX_NUM_CLASSES = 80
YOLOX_IN_CHANNELS = (256, 512, 1024)
YOLOX_ACT = "silu"


def build_yolox_s(num_classes: int = YOLOX_NUM_CLASSES):
    """An untrained YOLOX-S in eval mode, laid out like the upstream checkpoint.

    Upstream's ``get_model`` sets every BatchNorm2d to ``eps=1e-3`` and
    ``momentum=0.03``. ``eps`` is not stored in the checkpoint but changes the
    inference output, so it is set here too.
    """
    from torch import nn

    from .yolo_head import YOLOXHead
    from .yolo_pafpn import YOLOPAFPN
    from .yolox import YOLOX

    in_channels = list(YOLOX_IN_CHANNELS)
    backbone = YOLOPAFPN(YOLOX_S_DEPTH, YOLOX_S_WIDTH, in_channels=in_channels, act=YOLOX_ACT)
    head = YOLOXHead(num_classes, YOLOX_S_WIDTH, in_channels=in_channels, act=YOLOX_ACT)
    model = YOLOX(backbone, head)
    for module in model.modules():
        if isinstance(module, nn.BatchNorm2d):
            module.eps = 1e-3
            module.momentum = 0.03
    model.head.decode_in_inference = True
    return model.eval()
