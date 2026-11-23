#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# Copyright (c) Megvii Inc. All rights reserved.
#
# MODIFIED BY THE FLIGHTRISK AUTHORS (2026): this file is YOLOX's
# yolox/models/yolox.py at commit 6ddff4824372906469a7fae2dc3206c7aa4bbaee,
# cut down to inference only (the training branch and visualize() are
# removed). See NOTICE in this directory.

import torch.nn as nn

from .yolo_head import YOLOXHead
from .yolo_pafpn import YOLOPAFPN


class YOLOX(nn.Module):
    """
    YOLOX model module. The module list is defined by create_yolov3_modules function.
    This vendored copy only runs inference and returns detection results.
    """

    def __init__(self, backbone=None, head=None):
        super().__init__()
        if backbone is None:
            backbone = YOLOPAFPN()
        if head is None:
            head = YOLOXHead(80)

        self.backbone = backbone
        self.head = head

    def forward(self, x):
        # fpn output content features of [dark3, dark4, dark5]
        fpn_outs = self.backbone(x)
        return self.head(fpn_outs)
