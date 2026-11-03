"""The appearance (ReID) embedding the ground compares, as plain constants.

``flightrisk/vision/reid.py`` builds its model from these values, and the
mobile contract fixtures (``scripts/export_mobile_contract_fixtures.py``)
publish them, so a phone acting as an edge knows which embeddings the ground
can compare with its target. This module imports nothing heavy, so the
exporter and the tests can read it without loading PyTorch.

An embedding is comparable only when it comes from the same model and the same
weights as the ground's target embedding. A vector of the right length from
another model (for example OpenAI CLIP ViT-B/32) gives a cosine similarity
that means nothing, so a phone must not send one.
"""

#: The library the model and its preprocessing come from.
REID_LIBRARY = "open_clip"

#: The default model name. ``config.vision.reid_model`` can change it on the
#: ground; a phone edge is built for this default only.
REID_DEFAULT_MODEL = "ViT-B-16"

#: The pretrained weights ``PersonReID`` loads.
REID_PRETRAINED = "laion2b_s34b_b88k"

#: Length of the image embedding of ``REID_DEFAULT_MODEL``.
REID_EMBEDDING_DIM = 512

#: Image preprocessing of these weights in open_clip: resize the shortest side
#: to 224 pixels (bicubic), center crop 224 x 224, then normalize each RGB
#: channel with this mean and standard deviation.
REID_INPUT_SIZE = 224
REID_MEAN = (0.48145466, 0.4578275, 0.40821073)
REID_STD = (0.26862954, 0.26130258, 0.27577711)
