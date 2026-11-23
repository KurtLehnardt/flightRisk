# Mobile models for store builds

The Android and iOS apps run two neural networks on the phone: a person detector
and an appearance embedder. This page describes the models that may ship in a
store build, how they are built, the exact preprocessing the apps must use, how
closely the converted models match PyTorch, and the open questions.

The models are not committed to this repository and are not hosted by
FlightRisk. On the release build machine, `scripts/mobile_models/make_models.py`
downloads the pinned upstream weights, checks their SHA-256, converts them,
checks the conversions against PyTorch and copies them into each app's
gitignored model folder. This follows the MiDaS approach of #181
(`flightrisk/drone/midas_model.py`): pinned files, verified before use, failing
closed on any mismatch.

## What ships, and why

| Role | Model | Why |
|---|---|---|
| Person detector | **YOLOX-S**, official Megvii release 0.1.1rc0, COCO (80 classes, person is class 0) | Apache-2.0 code and weights. Plain convolutions, SiLU, upsampling and concatenation, which run on the Core ML GPU and Neural Engine and on every ONNX Runtime execution provider. D-FINE-N was considered and deferred: its `grid_sampler` converts to a Core ML operation that runs only on the CPU, and no physical-device benchmark is possible yet. |
| Appearance embedder | **open_clip ViT-B-16, pretrained `laion2b_s34b_b88k`**, image tower only, 512 values, L2-normalised | MIT weights. It is the ground's own model (`open_clip:ViT-B-16:laion2b_s34b_b88k`, `flightrisk/vision/reid_spec.py` and `ReidEmbedding.MODEL_ID` in `mobile/contract`), so a phone's embeddings can be compared with the ground's target embedding with no contract change. |
| Face recogniser | **None.** | No face recognition or face detection model ships in a store build. |

These must never be in a store build, and `scripts/mobile_models/licence_guard.py`
fails the build if they are (by file or folder name, and by the author recorded
in a model's metadata):

- every YOLO or Ultralytics model (`yolo*` other than `yolox*`, `ultralytics*`): AGPL-3.0 or the Ultralytics commercial licence;
- every InsightFace model (`scrfd*`, `arcface*`, `insightface*`, `*buffalo*`, and the files inside the packs such as `det_500m`, `w600k_mbf`): non-commercial research weights;
- MobileCLIP (`mobileclip*`): research-only weights;
- OSNet (`osnet*`): weights trained on research-only re-identification datasets;
- every output of `scripts/download_matching_models.sh`, and the model names the apps loaded before this pipeline (`yolo11n.onnx`, `YOLOPersonDetector`, `scrfd_500m.onnx`, `SCRFDFaceDetector`, `arcface_mobilefacenet.onnx`, `ArcFaceMobile`, `clip_visual.onnx`, `CLIPVisual`).

The app integration PRs remove the code that loads those models; this pipeline
only provides the replacements and the guard.

## Licence table

| Component | Pinned source | Licence | In the app? |
|---|---|---|---|
| YOLOX-S weights | `https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.pth`, SHA-256 `f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30`, 72,089,125 bytes | Apache-2.0 (Megvii Inc.) | Yes, converted |
| YOLOX model code | commit `6ddff4824372906469a7fae2dc3206c7aa4bbaee`, inference parts vendored in `scripts/mobile_models/yolox_vendor/` with `LICENSE` and `NOTICE` | Apache-2.0 | Its graph is part of the converted model |
| ViT-B-16 `laion2b_s34b_b88k` weights | `laion/CLIP-ViT-B-16-laion2B-s34B-b88K` on Hugging Face at revision `7288da5a0d6f0b51c4a2b27c624837a9236d0112`, file `open_clip_model.safetensors`, SHA-256 `3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1`, 598,516,980 bytes | MIT (declared in the model card) | Yes, image tower only, converted |
| open_clip model code | `open_clip_torch` 3.3.0 (the ground's version) | MIT | Its graph is part of the converted model |
| OpenAI CLIP (where open_clip's model code comes from) | github.com/openai/CLIP | MIT | Its graph is part of the converted model |
| YOLOX demo picture `assets/dog.jpg` | YOLOX commit `6ddff48`, SHA-256 `5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866` | Apache-2.0 | No; a parity input for `verify` only (a dog, a bicycle and a truck, no person) |
| Conversion tools: PyTorch, torchvision, coremltools, ONNX, ONNX Runtime | `scripts/mobile_models/requirements.txt` | BSD-3-Clause, BSD-3-Clause, BSD-3-Clause, Apache-2.0, MIT | No; build machine only |

`scripts/mobile_models/NOTICES.md` holds the full Apache-2.0 text, the MIT
texts with every copyright line, and the list of changes made. `install`
copies it next to the models as `model-notices.md` for the apps' licences
screens; show it in full.

## Building the models before a release build

Run this on the release build machine, online, before every release build.
The conversion tools go in their own virtual environment; never install them
into FlightRisk's `venv`, whose test suites depend on the versions there.

```bash
python3.13 -m venv .venv-mobile-models
.venv-mobile-models/bin/pip install -r scripts/mobile_models/requirements.txt
.venv-mobile-models/bin/python scripts/mobile_models/make_models.py all
```

`all` runs four steps and stops at the first failure:

1. `fetch` downloads the three pinned files (about 670 MB) into
   `build/mobile_models/downloads`. A file whose size or SHA-256 differs from
   its pin is deleted and the step fails; a matching file already there is kept.
2. `convert` re-checks every download against its pin (the bytes that are
   checked are the bytes that are loaded; checkpoints are read with
   `weights_only=True`), refuses tool versions other than the pinned ones
   (override with `--allow-unpinned-tools`, which is recorded), and writes
   `build/mobile_models/out` and `build.json`. About 3 minutes on an M1 Max.
3. `verify` runs every output and PyTorch on the same inputs, checks the
   tolerances below and the model interfaces, runs the licence guard over the
   outputs, and writes `build/mobile_models/verify.json`. About 2 minutes.
4. `install` copies one detector and one embedder per platform into
   `mobile/app/src/main/assets` and `ios/FlightRisk/Models` (change them with
   `--android` and `--ios`), together with `mobile-models.json` (the installed
   files, their digests, their parity and the preprocessing contract) and
   `model-notices.md`. It refuses any file whose digest is not the one
   `verify` passed, then runs the licence guard over both folders.

Core ML predictions need macOS, so on Linux use `--targets android`. Variants
are chosen with `--android-detector fp32|fp16`,
`--android-embedder int8-weights|int8-dynamic|fp32` and
`--ios-embedder fp16|palettized8`. The installed file names do not depend on
the variant, so the app code does not either.

After building the app, run the guard on the built artifact too:

```bash
python3 scripts/mobile_models/licence_guard.py mobile/app/build/outputs/bundle/release/app-release.aab
python3 scripts/mobile_models/licence_guard.py path/to/FlightRisk.xcarchive
```

It needs only the Python standard library, so it can run in a Gradle task or
an Xcode build phase. It exits 0 when clean, 1 when it finds a forbidden model
(each is listed) and 2 when a path cannot be checked.

## Outputs

Sizes are from the run recorded in the PR. Defaults are what `install` copies.

| File | Platform | Variant | Size | Installed as | Default |
|---|---|---|---|---|---|
| `yolox_s.onnx` | Android | fp32 | 36.0 MB | `yolox_s.onnx` | yes |
| `yolox_s_fp16.onnx` | Android | fp16 weights and arithmetic, fp32 input and output | 18.1 MB | `yolox_s.onnx` | no |
| `clip_vit_b16_laion2b.onnx` | Android | fp32 | 345.0 MB | `clip_vit_b16_laion2b.onnx` | no |
| `clip_vit_b16_laion2b_w8.onnx` | Android | int8 weights (per channel, `DequantizeLinear`), fp32 arithmetic | 89.4 MB | `clip_vit_b16_laion2b.onnx` | yes |
| `clip_vit_b16_laion2b_int8.onnx` | Android | dynamic int8 (ONNX Runtime `quantize_dynamic`) | 96.6 MB | `clip_vit_b16_laion2b.onnx` | no |
| `YOLOXs.mlpackage` | iOS | ML program, fp16, iOS 17 | 18.1 MB | `YOLOXs.mlpackage` | yes |
| `CLIPViTB16LAION.mlpackage` | iOS | ML program, fp16, iOS 17 | 172.6 MB | `CLIPViTB16LAION.mlpackage` | yes |
| `CLIPViTB16LAION8bit.mlpackage` | iOS | ML program, fp16 with 8-bit k-means palettised weights, iOS 17 | 86.5 MB | `CLIPViTB16LAION.mlpackage` | no |

All ONNX files use opset 17 and IR version 8, which the apps' ONNX Runtime
(`onnxruntime-android` 1.18.0) reads. The dynamic int8 copy was asked for and
is kept, but its embeddings move too far for matching (see Parity), so the
Android default is the weight-only int8 copy: about the same size, with fp32
arithmetic.

## Preprocessing contracts

The executable form of both contracts is
`scripts/mobile_models/mobile_preprocess.py`; the tests check it against
upstream YOLOX's `preproc` and against open_clip's own transform, and `verify`
feeds every converted model through it.

### Detector: YOLOX-S

Input name `images`, shape `[1, 3, 640, 640]`, float32, NCHW.

1. Take the camera frame as 8-bit **BGR**. On Android, the tensor's first
   channel is blue and its third is red (an Android bitmap is RGB, so swap
   them). On iOS, the Core ML input is declared BGR, so Core ML orders the
   channels of the pixel buffer itself.
2. `r = min(640 / height, 640 / width)`. Resize the frame to
   `int(width * r)` x `int(height * r)` (truncate toward zero) with bilinear
   interpolation.
3. Place the resized frame at the **top-left** corner of a 640 x 640 canvas.
   Fill the rest of the canvas with **114** in all three channels. Do not centre it.
4. Feed the canvas as float values **0..255**: no division by 255, no mean,
   no standard deviation. (Upstream's `legacy` flag, which normalised, applies
   only to weights older than 0.1.1rc0; it does not apply here.)
5. On iOS the Core ML input `images` is a 640 x 640 image whose colour layout
   is BGR; pass the letterboxed canvas as a pixel buffer with
   `imageCropAndScaleOption = .scaleFill` (a no-op at 640 x 640) or directly as
   an `MLFeatureValue(pixelBuffer:)`. Load the model with
   `MLModelConfiguration.computeUnits = .cpuAndGPU` (see Parity).

Output name `detections`, shape **`[1, 8400, 85]`**, float32. Each row is
`cx, cy, w, h, objectness, s0 ... s79`:

- `cx, cy, w, h` are the box centre and size in pixels of the 640 x 640 input
  (already decoded with the grid and strides). Divide all four by `r` to get
  frame pixels; corners are `cx - w/2, cy - h/2, cx + w/2, cy + h/2`.
- `objectness` and the 80 class scores already have the sigmoid applied.
  The detection score is **objectness times class score**; for persons use
  class 0: `score = row[4] * row[5]`.
- Rows run over the stride-8 grid (80 x 80, rows 0 to 6399), then stride 16
  (40 x 40, rows 6400 to 7999), then stride 32 (20 x 20, rows 8000 to 8399),
  each row-major (y outer, x inner).
- Then drop rows under the score threshold and run non-maximum suppression per
  class at IoU 0.45, as upstream's `yolox.utils.postprocess` does. The apps
  choose their own score threshold; `verify` compares detections at 0.25.

### Appearance embedder: open_clip ViT-B-16 `laion2b_s34b_b88k`

Input name `input`, shape `[1, 3, 224, 224]`, float32, NCHW, **RGB**. This is
exactly what the ground does in `flightrisk/vision/reid.py` (the crop is
converted from BGR to RGB, then open_clip's evaluation transform runs).

1. Take the person crop as 8-bit **RGB**.
2. Resize so the **shortest side is 224** with **bicubic** interpolation. The
   long side becomes `int(224 * long / short)` (truncate toward zero). The
   ground uses Pillow's bicubic filter.
3. Cut out the **centre 224 x 224**. The top offset is
   `round((height - 224) / 2)` and the left offset `round((width - 224) / 2)`,
   with halves rounded to the even neighbour (a surplus of 1 gives 0, of 3 gives 2).
4. Scale each value to 0..1 (`v / 255`), then normalise each channel with
   mean **(0.48145466, 0.4578275, 0.40821073)** and standard deviation
   **(0.26862954, 0.26130258, 0.27577711)**: `(v / 255 - mean) / std`.

Output name `embedding`, shape `[1, 512]`, float32, **L2-normalised** by the
model. Send it through `ReidEmbedding.toWire` as before (it normalises again
and rounds to float32), with the model id `ReidEmbedding.MODEL_ID`.

Resampling differs slightly between Pillow, Android and Core Graphics. The
parity figures below use Pillow, as the ground does; an app's own resize adds
a small, separate difference that the app PRs should measure on a device.

## Parity

`verify` compares every output with PyTorch fp32 (upstream's own code and
decoding) on the same inputs:

- Detector: the three synthetic aerial fixtures in `tests/fixtures/aerial_tiny`,
  YOLOX's demo picture at full size and scaled to 0.5, 0.25 and 0.12 (so its
  objects are as small as a person seen from altitude), and three seeded
  synthetic pictures (noise, coloured blocks, a gradient). No photograph of a
  person is used.
- Embedder: the three fixtures, the demo picture, its dog and bicycle as
  detection-shaped crops, and the three synthetic pictures. `verify` also
  checks that the documented preprocessing gives exactly open_clip's tensors
  (largest difference measured: 0).
- PyTorch YOLOX-S must find the demo picture's dog and bicycle with a score of
  at least 0.8 (measured 0.913 and 0.954); this checks the weights and the
  BGR, 0..255 contract.

Metrics: for the detector, the largest difference of any score (objectness
times class) over all 8400 rows and 80 classes, the largest box difference in
input pixels over rows PyTorch scores at least 0.05, and the smallest IoU of
matched detections after NMS (no detection may appear or disappear unless it is
within the score tolerance of the threshold). For the embedder, the smallest
cosine similarity with PyTorch, and the largest change in the similarity the
ground would compute between a PyTorch target and the model's embedding of any
of the inputs (the ground matches at 0.55 by default).

Measured on macOS 27, Apple M1 Max, with the pinned tools:

| Output | Run on | Score diff | Box diff | Min matched IoU | Tolerance |
|---|---|---|---|---|---|
| `yolox_s.onnx` | ONNX Runtime CPU | 1.4e-5 | 0.001 px | 1.0000 (21 matched) | 1e-3, 0.05 px, 0.999 |
| `yolox_s_fp16.onnx` | ONNX Runtime CPU | 0.0056 | 0.66 px | 0.9891 | 0.02, 2 px, 0.95 |
| `YOLOXs.mlpackage` | Core ML CPU and GPU (gate) | 0.0057 | 0.69 px | 0.9891 | 0.02, 2 px, 0.95 |
| `YOLOXs.mlpackage` | Core ML all units (Neural Engine) | 0.132 | 8.0 px | 0.9399 | recorded only |
| `YOLOXs.mlpackage` | Core ML CPU only | 0.073 | 8.1 px | 0.9504 | recorded only |

| Output | Run on | Min cosine | Mean cosine | Max similarity change | Tolerance |
|---|---|---|---|---|---|
| `clip_vit_b16_laion2b.onnx` | ONNX Runtime CPU | 1.000000 | 1.000000 | 2.3e-6 | 0.9999, 0.001 |
| `clip_vit_b16_laion2b_w8.onnx` | ONNX Runtime CPU | 0.999379 | 0.999716 | 0.0051 | 0.995, 0.01 |
| `clip_vit_b16_laion2b_int8.onnx` | ONNX Runtime CPU | 0.979960 | 0.991060 | 0.0469 | 0.97, 0.06 |
| `CLIPViTB16LAION.mlpackage` | Core ML all units (gate) | 0.999661 | 0.999825 | 0.0047 | 0.99, 0.02 |
| `CLIPViTB16LAION.mlpackage` | Core ML CPU and GPU | 0.999983 | | 0.0005 | recorded only |
| `CLIPViTB16LAION8bit.mlpackage` | Core ML all units (gate) | 0.997993 | 0.999210 | 0.0150 | 0.99, 0.03 |

Findings:

- **Run the iOS detector on the CPU and GPU, not the Neural Engine.** On the
  Neural Engine (and on the CPU alone) the fp16 detector's scores move by up
  to 0.13 and its boxes by up to 8 px, worst on the smallest objects. On the
  CPU and GPU it matches as closely as the ONNX fp16 copy. The embedder is
  fine on every unit.
- **Dynamic int8 is not good enough for matching.** Quantised as ONNX Runtime
  does by default, the embedder's cosine with PyTorch drops to about 0.86: the
  input of block 6's MLP output projection carries the outlier activations
  CLIP ViTs are known for, and per-tensor run-time quantisation of it destroys
  the embedding. With that one layer kept in fp32 (and the attention's
  activation-by-activation products never quantised) the copy reaches 0.980,
  but a similarity can still move by 0.047. The weight-only int8 copy keeps
  fp32 arithmetic, is a little smaller, and moves similarities by at most 0.005.
- The ONNX outputs were byte-identical across repeated conversions; the Core
  ML packages were not (their manifests carry fresh identifiers), which is why
  `install` checks digests against `verify.json` rather than fixed values.

## Follow-ups

- **D-FINE-N benchmark.** Measure D-FINE-N against YOLOX-S on a physical
  iPhone and Android phone. Its `grid_sampler` becomes a CPU-only Core ML
  operation, so the benchmark decides whether it can replace YOLOX-S.
- **DINOv2 ViT-S/14** as the appearance embedder (smaller and Apache-2.0). It
  changes the embedding contract, so the ground's `reid_spec.py` and
  `ReidEmbedding` must change with it.
- **HERIDAL fine-tune.** Fine-tune the detector on HERIDAL (aerial
  search-and-rescue imagery; `scripts/finetune_heridal.py` and
  `docs/altitude-benchmark.md`) after checking the dataset's licence.
- **Loosen the 12% / 0.8% box filters** in both `PersonDetector` files
  (`mobile/app/src/main/java/com/flightrisk/app/vision/PersonDetector.kt` and
  `ios/FlightRisk/Vision/PersonDetector.swift`: a box must be at least 12% of
  the frame height and 0.8% of its area, `MIN_BBOX_HEIGHT_RATIO = 0.12f` and
  `MIN_BBOX_AREA_RATIO = 0.008f` in Kotlin, `minBboxHeightRatio` and
  `minBboxAreaRatio` in Swift). They drop people who are small in the frame,
  which is the normal case from altitude.
- **Neural Engine accuracy for the detector** (mixed precision or
  quantisation-aware conversion), so the iOS detector can use the Neural Engine.
- **On-device checks.** The Android fp16 detector and every model's speed and
  memory on real phones are unmeasured.
- **App PRs.** Load these files with these contracts, remove the YOLO,
  InsightFace, MobileCLIP and OSNet code paths, run the licence guard in the
  Gradle and Xcode release builds, show `model-notices.md` on the licences
  screens, and update the privacy disclosures for sending embeddings
  (`docs/mobile-followups.md`, "Embeddings for phone-as-edge").

## Legal questions

This section lists questions for the owner and, where needed, a lawyer. It is
**not legal advice** and does not answer them.

> **GPL and the App Store.** FlightRisk is licensed **GPL-3.0-or-later**. The
> Free Software Foundation's position is that the Apple App Store's terms of
> service conflict with the GPL, because they add restrictions on use and
> redistribution that the GPL forbids. Distributing FlightRisk through the App
> Store therefore likely needs an **additional permission under GPLv3 section 7**
> from **every copyright holder** of the code in the app (or another licensing
> arrangement). Whether and how to do that is the owner's decision; nothing in
> this pipeline settles it. Whether Google Play's terms raise the same issue is
> a separate question that was not reviewed here.

Other questions:

1. Is shipping `model-notices.md` on the in-app licences screen enough to meet
   the Apache-2.0 (section 4) and MIT notice conditions for the converted
   models, and is the change statement in `NOTICES.md` adequate?
2. Do converted model files count as part of the GPL "Corresponding Source"
   question for the apps, and is pointing to the pinned upstream weights and
   this pipeline sufficient?
3. The LAION model card says that "any deployed use case of the model, whether
   commercial or not, is currently out of scope" and that surveillance and
   facial recognition uses are "always out-of-scope". These are statements of
   intended use, not terms of the MIT licence. Does person re-identification
   for search and rescue fall within what the card warns against, and does the
   owner accept that position?
4. Training data: the embedder was trained on LAION-2B-en (web-crawled images;
   LAION-5B, its parent, was withdrawn in December 2023 after a report of
   abusive content links and re-released as Re-LAION-5B). The detector was
   trained on COCO (Flickr images under their own licences; annotations
   CC BY 4.0). Do these carry obligations or risks for distributing the weights?
5. Appearance embeddings are treated as biometric data in the apps
   (`ReidEmbedding.kt`). Which privacy rules apply when a phone sends them to
   the ground, and do the store privacy labels cover it?
6. The on-device language model downloads (Gemma, from Hugging Face or Google)
   are outside this pipeline; their terms were not reviewed here.
