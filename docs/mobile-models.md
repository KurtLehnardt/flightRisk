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

## The licence guard

`scripts/mobile_models/licence_guard.py` decides what may be in a store build.
It needs only the Python standard library (3.9 or newer, so the
`/usr/bin/python3` of Xcode's build phases works), and it runs:

- in every Android release build: `bundleRelease` and `assembleRelease` are
  followed by `licenceGuardReleaseBundle` and `licenceGuardReleaseApk`
  (`mobile/app/build.gradle.kts`), which check the built bundle or APK and fail
  the build on any finding, when the guard cannot run, or when there is no file
  to check (set the Python with `-Pflightrisk.python=...`; default `python3`).
  A rejected APK or bundle is renamed to `*.licence-guard-rejected` (for
  example `app-release.aab.licence-guard-rejected`), so it cannot be uploaded
  by mistake; if the next build has nothing new to package, it fails asking
  for a clean release build;
- in every iOS Release build, archives included: the FlightRisk target's last
  build phase, "Licence guard (Release)" (`ios/project.yml`), checks
  `$TARGET_BUILD_DIR/$WRAPPER_NAME`, the built `.app`; Debug builds skip it;
- at the end of `make_models.py install`, over both app folders;
- by hand, on any folder, `.app`, `.xcarchive`, APK, AAB, APKS or IPA:

```bash
python3 scripts/mobile_models/licence_guard.py mobile/app/build/outputs/bundle/release/app-release.aab
python3 scripts/mobile_models/licence_guard.py path/to/FlightRisk.xcarchive
```

It exits 0 when clean, 1 when it finds something (each finding is listed) and
2 when a path cannot be checked. Its rules:

1. **Allowlist.** Only the models `make_models.py install` put there may ship.
   Every model-like file must be listed, with the same SHA-256, in the
   `mobile-models.json` that `install` writes next to it. A file is model-like
   when its extension is a model or archive format's (`.onnx`, `.tflite`,
   `.pt`, `.safetensors`, `.zip`, `.gz` and others), or when its first bytes
   are those of a model or an archive, whatever it is called: an ONNX or Core
   ML protobuf, TFLite (`TFL3`), ONNX Runtime (`ORTM`), safetensors, GGUF, HDF5,
   NumPy, a Python pickle, ncnn, zip (`PK`), gzip, xz, zstd, 7z or bzip2. So a
   renamed model (`config.dat`, `textures.bin`) is caught by its bytes. Zip
   archives are opened whatever their name and their entries checked the same
   way. A Core ML package or compiled model (`.mlpackage`, `.mlmodelc`) must
   carry the SHA-256 of one of the pinned upstream weights as its
   `flightrisk.weights_sha256` metadata (and must have the listed digest where a
   manifest lists it); Xcode compiles the installed packages into `.mlmodelc`
   folders that keep that metadata.

   Archives are model-like in the app asset folders (an APK's `assets/`, an
   AAB's `<module>/assets/`, the whole `.app`, and any folder given to the
   guard), so a gzip or a zip there fails the build. Elsewhere in an APK or AAB
   (the Java resources: OkHttp ships its public suffix list gzipped) a gzip,
   bzip2 or xz file is opened and refused only if it holds a model or an
   archive, a zip is opened and its entries checked, and zstd and 7z files,
   which the guard cannot open, are refused. A model format outside an asset
   folder (`res/raw/detector.tflite`) is refused like any unlisted model.

   On iOS the whole `.app` is an asset folder, so a zip or gzip anywhere in the
   iOS app (in any framework or resource bundle too) fails the Release build,
   whatever it holds. Today's app has none; a dependency that adds one must be
   reviewed, and the guard changed on purpose, before it can ship.
2. **Denylist.** No file may have the SHA-256 of a known forbidden model file,
   whatever its name or place: every file of InsightFace's buffalo_l, buffalo_m,
   buffalo_s, buffalo_sc and antelopev2 packs (release v0.7) and the pack
   archives themselves, and the models the apps loaded before this pipeline
   (`yolo11n.onnx`, `scrfd_500m.onnx`, `arcface_mobilefacenet.onnx`,
   `clip_visual.onnx` and the weights of the old `.mlmodelc` folders). The
   digests were computed from the packs and from a developer checkout's app
   folders; only files of 64 KiB or more are listed.
3. **Names.** No path component may name a forbidden family or a known file of
   one: every YOLO or Ultralytics model (`yolo*` other than `yolox*`,
   `ultralytics*`: AGPL-3.0 or the Ultralytics commercial licence); every
   InsightFace model (`scrfd*`, `arcface*`, `insightface*`, `*buffalo*`,
   `*antelope*`, and the files inside the packs such as `det_500m`,
   `w600k_mbf`: non-commercial research weights); MobileCLIP (`mobileclip*`:
   research-only weights); OSNet (`osnet*`: weights trained on research-only
   re-identification datasets); every output of
   `scripts/download_matching_models.sh`; and the model names the apps loaded
   before this pipeline (`yolo11n.onnx`, `YOLOPersonDetector`,
   `scrfd_500m.onnx`, `SCRFDFaceDetector`, `arcface_mobilefacenet.onnx`,
   `ArcFaceMobile`, `clip_visual.onnx`, `CLIPVisual`).
4. **Content.** No model file may name Ultralytics, InsightFace or MobileCLIP
   in its bytes (an Ultralytics export records its author in its metadata).
5. **Symbolic links** are refused, in folders and in archives: a link could
   point at a model outside what is checked.

The guard does not know MiDaS: `midas_small.onnx`, which the Android
`ObstacleGuard` loads, is not made by this pipeline, so a release build that
carries it fails until MiDaS is added to `make_models.py` (follow-up below).

What the guard is for, and what it is not: it stops a forbidden or unvetted
model from reaching a store build by accident, under any name, in any of the
usual model and archive formats. Deliberate hiding is out of scope: a model
stored as raw deflate or another stream without a header, XOR-ed or otherwise
encoded, split into pieces, or inside a container format the guard does not
recognise is not detected. Code review of what goes into the app folders and
the app's dependencies remains the control for that.

The app integration PRs remove the code that loads the forbidden models; this
pipeline provides the replacements and the guard.

## Licence table

| Component | Pinned source | Licence | In the app? |
|---|---|---|---|
| YOLOX-S weights | `https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.pth`, SHA-256 `f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30`, 72,089,125 bytes | Apache-2.0 (Megvii Inc.) | Yes, converted |
| YOLOX model code | commit `6ddff4824372906469a7fae2dc3206c7aa4bbaee`, inference parts vendored in `scripts/mobile_models/yolox_vendor/` with `LICENSE` and `NOTICE` | Apache-2.0 | Its graph is part of the converted model |
| ViT-B-16 `laion2b_s34b_b88k` weights | `laion/CLIP-ViT-B-16-laion2B-s34B-b88K` on Hugging Face at revision `7288da5a0d6f0b51c4a2b27c624837a9236d0112`, file `open_clip_model.safetensors`, SHA-256 `3f25d29d3cc74e1d25d47e0593b4dd0864ced1e9b4d8e486a247ca4502f227f1`, 598,516,980 bytes | MIT (declared in the model card) | Yes, image tower only, converted |
| open_clip model code | `open_clip_torch` 3.3.0 (the ground's version) | MIT | Its graph is part of the converted model |
| OpenAI CLIP (where open_clip's model code comes from) | github.com/openai/CLIP | MIT | Its graph is part of the converted model |
| YOLOX demo picture `assets/dog.jpg` | YOLOX commit `6ddff48`, SHA-256 `5a9522051c3cec2bbd2f6323fccba32e8fbf3ddcc2b3e2fd46b04c720bc6f866` | Apache-2.0 | No; a parity input for `verify` only (a dog, a bicycle and a truck, no person) |
| "Group Hiking on Munch's Coulee National Hiking Trail" | U.S. Fish and Wildlife Service, Mountain-Prairie Region, via Wikimedia Commons: `https://upload.wikimedia.org/wikipedia/commons/4/4d/Group_Hiking_on_Munch%27s_Coulee_National_Hiking_Trail_%285220995838%29.jpg`, SHA-256 `0b7230075f66aeda713b53924e6b78ccb768e7f0875e83cca7bfbfe1939be581`, 1,567,122 bytes, 2816 x 1584 | Public domain (a work of the U.S. federal government) | No; a parity input for `verify` only, kept in the gitignored downloads folder, never committed or installed |
| Conversion tools: PyTorch, torchvision, coremltools, ONNX, ONNX Runtime | `scripts/mobile_models/requirements.lock` | BSD-3-Clause, BSD-3-Clause, BSD-3-Clause, Apache-2.0, MIT | No; build machine only |

`scripts/mobile_models/NOTICES.md` holds the full Apache-2.0 text, the MIT
texts with every copyright line, and the list of changes made. `install`
copies it next to the models as `model-notices.md` for the apps' licences
screens; show it in full.

## Building the models before a release build

Run this on the release build machine, online, before every release build.
The conversion tools go in their own virtual environment, installed from the
hash-locked `requirements.lock` (every package and dependency at one version,
with its SHA-256 hashes, for every platform); never install them into
FlightRisk's `venv`, whose test suites depend on the versions there.

```bash
python3.13 -m venv .venv-mobile-models
.venv-mobile-models/bin/pip install --require-hashes -r scripts/mobile_models/requirements.lock
.venv-mobile-models/bin/python scripts/mobile_models/make_models.py all
```

`requirements.txt` holds the top-level pins; after changing one, regenerate
the lock with the command in `requirements.txt` and commit both.

First clear the app folders of models from older builds. A developer checkout
may still hold `yolo11n.onnx`, `scrfd_500m.onnx`, `arcface_mobilefacenet.onnx`
and `clip_visual.onnx` in `mobile/app/src/main/assets`, and
`YOLOPersonDetector.mlmodelc` and friends in `ios/FlightRisk/Models`. They are
gitignored, so `git status` never shows them, and they must never ship. Build
from a fresh clone, or list them with
`git clean -ndX mobile/app/src/main/assets ios/FlightRisk/Models` and remove
them with `git clean -fdX` on the same folders. `install` replaces only the
files it installs (the installed names do not depend on the variant, so a
reinstall replaces the previous one) and does not delete anything else; it
fails while anything else model-like is in the folders, and so does the
release build's guard.

`all` runs four steps and stops at the first failure:

1. `fetch` downloads the four pinned files (about 672 MB) into
   `build/mobile_models/downloads`, over https only: a redirect to another
   scheme is refused. A file whose size or SHA-256 differs from its pin is
   deleted and the step fails; a matching file already there is kept.
2. `convert` refuses to run unless the installed packages are exactly the
   locked set (every locked package at its locked version, nothing else but
   pip), re-checks every download against its pin (the bytes that are checked
   are the bytes that are loaded; checkpoints are read with
   `weights_only=True`, and every weight or config download is refused while
   the embedder is built), and writes `build/mobile_models/out` and
   `build.json`. About 3 minutes on an M1 Max.
3. `verify` runs every output and PyTorch on the same inputs, checks the
   tolerances below and the model interfaces, runs the licence guard over the
   outputs (without the allowlist: they are not installed yet), writes the
   golden fixtures and `build/mobile_models/verify.json`. About 5 minutes.
4. `install` copies one detector and one embedder per platform into
   `mobile/app/src/main/assets` and `ios/FlightRisk/Models` (change them with
   `--android` and `--ios`), together with `mobile-models.json` (the installed
   files, their digests, their parity, whether the tools were pinned, and the
   preprocessing contract) and `model-notices.md`. It refuses any file whose
   digest is not the one `verify` passed. Each file is staged under a hidden
   name next to its destination, checked, and moved into place with
   `os.replace`; the old manifest is deleted first and the new one written
   last, so an interrupted install leaves a folder that lists nothing, which
   the guard refuses. Then it runs the licence guard over both folders.

`--allow-unpinned-tools` lets `convert` and `verify` run with other tool
versions; the build is then recorded as unpinned (`tools_pinned: false` in
`build.json`, `verify.json` and `mobile-models.json`), and `install` refuses it
unless `--allow-unpinned-build` is given too.

Core ML predictions need macOS, so on Linux use `--targets android`. Variants
are chosen with `--android-detector fp32|fp16`,
`--android-embedder int8-weights|int8-dynamic|fp32`,
`--ios-detector fp32|fp16` and `--ios-embedder palettized8|fp16`. The installed
file names do not depend on the variant, so the app code does not either. The
ground compares Android embeddings with its own (phone-as-edge), so `install`
refuses an Android embedder whose person parity does not gate `verify`: today
that is `int8-dynamic`, which needs `--allow-ungated-embedder` too.

## Outputs

Sizes are from the run recorded in the PR. Defaults are what `install` copies.

| File | Platform | Variant | Size | Installed as | Default |
|---|---|---|---|---|---|
| `yolox_s.onnx` | Android | fp32 | 36.0 MB | `yolox_s.onnx` | yes |
| `yolox_s_fp16.onnx` | Android | fp16 weights and arithmetic, fp32 input and output | 18.1 MB | `yolox_s.onnx` | no |
| `clip_vit_b16_laion2b.onnx` | Android | fp32 | 345.0 MB | `clip_vit_b16_laion2b.onnx` | no |
| `clip_vit_b16_laion2b_w8.onnx` | Android | int8 weights (per output channel), `DequantizeLinear` back to fp32 on every inference, fp32 arithmetic | 89.4 MB | `clip_vit_b16_laion2b.onnx` | yes |
| `clip_vit_b16_laion2b_int8.onnx` | Android | dynamic int8 (ONNX Runtime `quantize_dynamic`) | 96.6 MB | `clip_vit_b16_laion2b.onnx` | no |
| `YOLOXs.mlpackage` | iOS | ML program, fp32, iOS 17, any compute units | 36.1 MB | `YOLOXs.mlpackage` | yes |
| `YOLOXsFP16.mlpackage` | iOS | ML program, fp16, iOS 17, **`computeUnits = .cpuAndGPU` only** | 18.1 MB | `YOLOXs.mlpackage` | no |
| `CLIPViTB16LAION.mlpackage` | iOS | ML program, fp16, iOS 17 | 172.6 MB | `CLIPViTB16LAION.mlpackage` | no |
| `CLIPViTB16LAION8bit.mlpackage` | iOS | ML program, fp16 with 8-bit k-means palettised weights, iOS 17 | 86.5 MB | `CLIPViTB16LAION.mlpackage` | yes |

Installed by default: Android 36.0 + 89.4 = 125.4 MB, iOS 36.1 + 86.5 = 122.6 MB.

All ONNX files use opset 17 and IR version 8, which the apps' ONNX Runtime
(`onnxruntime-android` 1.23.2) reads. The weight-only int8 embedder keeps its
`DequantizeLinear` nodes: ONNX Runtime does not fold them into constants (its
optimised graph still has all 49), so the int8 weights are turned back into
fp32 on every inference and the arithmetic is fp32. The dynamic int8 copy was
asked for and is kept, but its embeddings move too far for matching (see
Parity), so the Android default is the weight-only copy: about the same size,
with fp32 arithmetic.

Why these iOS defaults: the fp32 detector matches PyTorch on every Core ML
compute unit (the fp16 one does not on the Neural Engine or the CPU alone), so
the app needs no compute-unit setting, for 18 MB more. The palettised embedder
is half the size of the fp16 one and stays within 0.998 cosine of PyTorch. An
iPhone only compares embeddings it made itself (the target's and the
detections'), so a small shared offset against PyTorch does not matter there;
phone-as-edge, which sends embeddings to the ground to compare with PyTorch's,
is Android only, and Android uses the closer weight-only int8 copy.

Each iOS package records how it must be loaded as its
`flightrisk.coreml_compute_units` metadata (`all` or `cpuAndGPU`), and
`mobile-models.json` repeats it per model and in the contract
(`contract.detector.coreml_compute_units`). An app that installs
`--ios-detector fp16` must read it and load the detector with
`MLModelConfiguration.computeUnits = .cpuAndGPU`.

### For the app integration PRs

- iOS: done; see "The iOS app" below.
- Android: load the files with the contracts below, show `model-notices.md` in
  full on the licences screen, remove the YOLO, InsightFace, MobileCLIP and
  OSNet code paths, and test each stage against the golden fixtures.

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
   an `MLFeatureValue(pixelBuffer:)`. The default fp32 package runs on any
   compute units; the fp16 package must be loaded with
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

### From a detection to the embedder's crop

Cut the crop from the **full-resolution camera frame**, not from the 640 x 640
detector input, exactly as the ground does (`flightrisk/vision/detector.py`,
`crop = frame[y1:y2, x1:x2]` after its detector clips the box to the frame and
`astype(int)` truncates it):

1. Take the box corners in frame pixels: `x1, y1, x2, y2 = (cx - w/2, cy - h/2,
   cx + w/2, cy + h/2) / r`.
2. Clip `x1` and `x2` to `[0, width]` and `y1` and `y2` to `[0, height]`.
3. Truncate each corner to an integer toward zero.
4. Take `frame[y1:y2, x1:x2]`: **no margin**, no padding, no squaring.

`mobile_preprocess.crop_box` is this rule; `mobile-models.json` carries it as
`contract.embedder.crop_from_detection`.

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

### Golden fixtures for the apps' unit tests

`verify` writes `build/mobile_models/golden/` (gitignored), made from a drawn
frame, not a photograph (`make_models.silhouette_scene`: person-like figures
16 to 160 px tall on a grass-like texture, 800 x 480):

| File | What |
|---|---|
| `frame.png` | the frame, lossless |
| `detector_input.f32` | the letterboxed `[1, 3, 640, 640]` BGR tensor (float32, little-endian) |
| `detector_output.f32` | PyTorch's `[1, 8400, 85]` output for it |
| `crop.png` | the crop of the best person detection, cut by the rule above |
| `embedder_input.f32` | the crop's `[1, 3, 224, 224]` RGB tensor |
| `embedding.f32` | PyTorch's `[1, 512]` embedding |
| `golden.json` | SHA-256 and shape of each file, the detections, the letterbox ratio, the crop box (float and integer), the contract and the tolerances |

An app test decodes `frame.png` the way the app decodes a camera frame, runs
its own preprocessing, model and crop code, and compares each stage. The
frame's colours are lopsided on purpose: feeding it as RGB instead of BGR
moves the detector's scores by 0.42, and feeding the crop as BGR instead of
RGB drops the embedding's cosine to 0.957 (`golden.json`,
`channel_order_check`; `verify` fails if either stops being clear). That is
the check of the channel order; the YOLOX demo picture's sanity check below
does not check the channel order (its objects are found either way).

## The iOS app

The iPhone app loads the default iOS outputs, `YOLOXs.mlpackage` (fp32) and
`CLIPViTB16LAION.mlpackage` (the 8-bit palettised embedder), and nothing else.
It has no face detection or face recognition model and no YOLO (Ultralytics),
InsightFace, MobileCLIP or OSNet code path: matching is by appearance only.

**Bundling.** `ios/project.yml` keeps `Models/**` out of the target's sources,
because Xcode fails a build whose sources name a missing file (XcodeGen's
`optional` only skips its own check) and the models are not committed.
Instead the FlightRisk target's build phase "Core ML models (optional)" runs
before the licence guard on every build: for each of `YOLOXs` and
`CLIPViTB16LAION` it removes the compiled model left by an earlier build and,
when `ios/FlightRisk/Models/<name>.mlpackage` exists, compiles it into the app
with `xcrun coremlc compile ... --platform ios --container bundle-resources`
(the command Xcode itself runs for a model in the sources; under a second per
model). It copies `mobile-models.json` and `model-notices.md` beside them. A
missing package, `mobile-models.json` or `model-notices.md` (a partial install
included) is a warning in Debug that names every missing item; the build
succeeds and the Search tab shows "Person detection unavailable". In Release,
including archives, it is an error that fails the build, as Android's
`checkReleaseModels` does. CI's model-less Release builds pass
`FLIGHTRISK_ALLOW_RELEASE_WITHOUT_MODELS=YES` as a build setting to skip that
check. Store builds must never set it. The compiled
`.mlmodelc` folders keep the `flightrisk.weights_sha256` metadata, so the
Release licence guard checks them as it checks the packages. The detector is
loaded with the compute units `mobile-models.json` names for it (`all` for the
default fp32 detector, `cpuAndGPU` for the fp16 one).

**Where the contract lives.** The preprocessing and postprocessing are plain
Swift in the `PersonVision` library of the `ios/FlightRiskGround` package, with
no Core ML, so `swift test` checks them on a Mac:

- `Letterbox` and `LetterboxGeometry`: the YOLOX canvas (ratio, sides
  truncated, top-left, padding 114). The resize is bilinear with OpenCV's
  sampling positions but rounds in floating point, where OpenCV rounds in
  fixed point (and its arm64 build takes yet another path). It is exact at
  integer frame-to-canvas ratios, which include the app's 1920 x 1080 frames
  (`LetterboxOpenCVExactnessTests` checks 1920 x 1080, 1280 x 720, 3840 x 2160
  and 1080 x 1920 against OpenCV 5.1 `INTER_LINEAR` by SHA-256). At non-integer
  ratios, such as 4:3, 800 x 480 and the Tello, it differs by at most one
  level: about a tenth of the canvas values of the golden frame differ from
  `detector_input.f32`, each by exactly one level; none by more. Porting
  Android's `ImageResize.bilinearCv` for those sources is a follow-up.
- `YOLOXDecoder` and `NonMaxSuppression`: upstream's postprocess (best class
  per row, objectness times class score, NMS per class at 0.45, boxes divided
  by the ratio in float32). On the golden output it reproduces `golden.json`'s
  detections exactly, scores and boxes bit for bit.
- `CropRule`: the crop rule above.
- `CLIPPreprocess` and `PillowBicubic`: a port of Pillow's bicubic resampler
  (`Resample.c`, fixed point), so the embedder input is the ground's bit for
  bit: its SHA-256 equals `embedder_input.f32`'s. It matches Pillow 13.0.0,
  including its pass order: vertical first when `(h - th) > 0 && (h - th) > 2 *
  (w - tw)`. `requirements.lock` pins Pillow 13.0.0, but a ground venv installed
  from `requirements-ground.txt` (`Pillow>=10.4`) may have an older Pillow, for
  example 12.3.0, which always resizes horizontally first. Pinning the ground
  is a follow-up.
- `PersonBoxFilter`: which person boxes are kept (below).

The app's `PersonDetector` letterboxes each camera frame, hands the canvas to
Core ML as a 32BGRA pixel buffer (the model's input is declared BGR, so Core
ML orders the channels), decodes the persons (class 0) at the app's score
threshold (0.4, `FlightRiskConfig.vision.detectorConfidence`), filters the
boxes and cuts each crop from the full frame by the crop rule. `PersonReID`
embeds each crop and the reference photo (the whole photo, as the ground does)
and compares them by cosine similarity (match at 0.55). The phone compares
only embeddings it made itself, so the palettised embedder's small shared
offset from PyTorch does not affect a match.

**Box filter.** The app used to drop every box under 12% of the frame height
or 0.8% of its area, which dropped people who are small in the frame: the
normal case from altitude. The default is now `PersonBoxFilter.aerial`: at
least 1% of the frame height (11 pixels of a 1080-pixel frame) and 0.005% of
its area, and between 0.1 and 4 times as wide as tall (so that a person lying
down, seen from the side or from above, is kept). These are the values
Android's `AERIAL` uses. Detection keeps every box that passes this filter;
matching is limited separately: only crops at least 40 pixels tall get a ReID
or CLIP score, and at most 5 crops are scored per frame, the highest detector
scores first. Smaller detections are still detected and published, but not
scored. The limits are `FlightRiskConfig.vision.matchMinCropHeightPx` (40) and
`matchMaxPerFrame` (5), as on Android. Each
limit can be changed with `FLIGHTRISK_MIN_BOX_HEIGHT_RATIO`,
`FLIGHTRISK_MIN_BOX_AREA_RATIO`, `FLIGHTRISK_MIN_BOX_ASPECT_RATIO` and
`FLIGHTRISK_MAX_BOX_ASPECT_RATIO` (and `FLIGHTRISK_MATCH_MIN_CROP_HEIGHT_PX`,
`FLIGHTRISK_MATCH_MAX_PER_FRAME` for the matching limits) in the app's `flightrisk_config` defaults
suite.

**Tests.** `PersonVisionTests` (package) run without any model: the golden
numbers in `Tests/PersonVisionTests/Fixtures/golden-subset.json` (the 54
detector output rows whose objectness times best class score is 0.2 or more, the detections,
the crop's pixels and the embedder input's digest, all from the drawn frame)
and synthetic tensors for the letterbox, decoding, NMS, filter, crop rule,
the user-model checks and the zip reader. With the full fixtures they also
check the letterbox against `detector_input.f32`. The app's
`OnDeviceModelTests` and `CustomDetectorTests` check the Core ML glue always,
and, in a build with the models installed, the models themselves: the
detector's output on the exact golden input against `detector_output.f32`
(scores within 1e-3, boxes within 0.05 px: the fp32 gate of `verify`), the
persons found from `frame.png` against the golden detections (IoU 0.95 or
more; scores within 0.05, because of the one-level canvas differences), the
embedder on `embedder_input.f32` and on `crop.png` (cosine 0.99 or more with
`embedding.f32`, the palettised gate, and the two within 1e-4 of each other),
and that YOLOX-S finds a person in the check picture. The full fixtures are
found at `$FLIGHTRISK_GOLDEN_DIR` (for the app tests, pass
`TEST_RUNNER_FLIGHTRISK_GOLDEN_DIR` to `xcodebuild`) or at
`build/mobile_models/golden`; the tests that need them or the models are
skipped otherwise. In the PR's run (iOS 26.4 simulator, which runs Core ML on
the CPU) every one of them passed.

```bash
cd ios/FlightRiskGround && FLIGHTRISK_GOLDEN_DIR=$PWD/../../build/mobile_models/golden swift test
cd ios && TEST_RUNNER_FLIGHTRISK_GOLDEN_DIR=$PWD/../build/mobile_models/golden xcodebuild test \
  -scheme FlightRisk -destination 'platform=iOS Simulator,name=iPhone 17' -only-testing:FlightRiskTests
```

### Your own person detector (iOS)

Settings > Person Detector can use a person detector the user already has
instead of YOLOX-S. FlightRisk never bundles, hosts, downloads or logs such a
model, and the feature is off by default.

- **Gate.** "Import my own detector" opens a screen that says YOLO models from
  Ultralytics (YOLOv8, YOLO11 and later) are AGPL-3.0 or need an Ultralytics
  Enterprise licence, links to Ultralytics' licensing page and to the AGPL
  text, lists what the model must be, and asks the user to confirm they have
  the right to use the model. The file picker is disabled until they do.
- **File.** `UIDocumentPickerViewController` (a copy, so no security scope is
  kept) for a `.mlmodel`, or a `.mlpackage` folder compressed into a `.zip`.
  iOS has no public unzip API, so `PersonVision.ZipExtractor` reads the zip
  itself: stored or deflated entries only, no encryption, ZIP64, symbolic
  links, absolute paths or `..`, sizes capped before unpacking and every
  entry's CRC-32 checked; the Finder's `__MACOSX` entries are skipped. The
  archive must hold exactly one `.mlpackage` at its top level.
- **Checks** (`CustomModelValidator`), in order: the file is at most 100 MB
  (and so is the unpacked model); its SHA-256 is computed and shown in
  Settings; `MLModel.compileModel(at:)` compiles it on the phone (documented
  for `.mlmodel` and `.mlpackage` from iOS 16, so on the app's iOS 17 minimum;
  checked on the iOS 26.4 simulator, the oldest runtime available here); it
  has exactly one input, a 640 x 640 image; exactly one output is a YOLOv8 or
  YOLO11 head without NMS (`[1, 4 + classes, 8400]`, or transposed) or an
  NMS-free head (`[1, N, 6]`: `x1, y1, x2, y2, score, class`); when it names
  its classes (an Ultralytics export records `names` in its metadata), class 0
  is "person"; and it finds a person (class 0, score 0.25 or more) in the
  bundled check picture.
- **Check picture.** `ios/FlightRisk/Resources/person-check-scene.png` is
  drawn, not photographed: thirteen figures 24 to 120 pixels tall on a
  grass-like texture, made by `make_models.silhouette_scene` (seed 20261205;
  `ios/scripts/make_person_check_scene.py`). YOLOX-S finds seven people in it
  (best 0.73) and a YOLO11n export nine at 0.25 or more (best 0.64).
- **Running it.** A user model expects the frame centred on the canvas
  (Ultralytics' `LetterBox`: sides rounded, padding split), so the app
  letterboxes it that way, decodes persons (best class per anchor, class 0)
  and runs NMS at 0.45. The size filter, crop rule and embedder are the same.
- **Storage.** The compiled model and a small record (digest, size, interface,
  test score, import time) go to Application Support/CustomDetector, excluded
  from backups, and the model is switched on. Switching it off, removing it,
  or a model that no longer loads goes back to YOLOX-S; with no bundled model
  either, the Search tab says detection is unavailable.
- **Tests.** The validator, the head decoders and the zip reader have unit
  tests on synthetic data. `CustomDetectorTests` check the refusals (no
  statement, wrong file type, over 100 MB, empty, not a zip, no package, not
  a model) and, only when `$FLIGHTRISK_TEST_CUSTOM_DETECTOR` names a model on
  the test machine, a whole import. For the PR it was run with a YOLO11n Core
  ML export made locally for the test and deleted afterwards (input `image`,
  RGB; output `[1, 84, 8400]`; best score 0.64 on the check picture).

## Parity

`verify` compares every output with PyTorch fp32 (upstream's own code and
decoding, and open_clip's own transform) on the same inputs:

- **General**, which gates every output. Detector: the three synthetic aerial
  fixtures in `tests/fixtures/aerial_tiny`, YOLOX's demo picture at full size
  and scaled to 0.5, 0.25 and 0.12, and three seeded synthetic pictures (noise,
  coloured blocks, a gradient). Embedder: the three fixtures, the demo picture,
  its dog and bicycle as detection-shaped crops, and the three synthetic
  pictures.
- **Person**, which also gates the Android default detector (`yolox_s.onnx`)
  and the Android embedders whose embeddings the ground may compare with its
  own (`clip_vit_b16_laion2b_w8.onnx`, the default, and
  `clip_vit_b16_laion2b.onnx`), and is recorded for every other output. The pinned public-domain
  photograph of a group of people walking through grassland, seen from
  behind. Detector: the whole photo, a 640 x 640 window of it at native
  resolution (people about 75 to 260 px tall), and the photo scaled so its
  people's median height is 48 px, 32 px, 24 px and 16 px, each as a
  640 x 640 frame. Embedder: the seven person detections PyTorch makes in the
  native window (score 0.5 or more), cut by the crop rule, each at native size
  and scaled down to 48 px, 32 px, 24 px and 16 px tall (35 crops; the
  embedder's resize then scales them up to 224).
- **Silhouette**, recorded only, needs no download: the drawn frame of the
  golden fixtures and its seven figures (16 to 160 px tall) as crops.
- PyTorch YOLOX-S must find the demo picture's dog and bicycle with a score of
  at least 0.8 (measured 0.913 and 0.954), and at least three people in the
  whole photo and four at native size (measured four and seven). This checks
  the weights and the 0..255, unnormalised input; it does not check the
  channel order, which the golden fixtures do.
- Any output with a value that is not finite (NaN or infinite) fails, whatever
  the tolerance.

Metrics: for the detector, the largest difference of any score (objectness
times class) over all 8400 rows and 80 classes, the largest box difference in
input pixels over rows PyTorch scores at least 0.05, and the smallest IoU of
matched detections after NMS (no detection may appear or disappear unless it is
within the score tolerance of the threshold). For the embedder, the smallest
cosine similarity with PyTorch, and the largest change in the similarity the
ground would compute between a PyTorch embedding and the model's embedding of
any pair of inputs (the ground matches at 0.55 by default). The similarity
change is a maximum over every pair, so it grows with the number of inputs:
over the 35 person crops the weight-only int8 embedder moves a similarity by up
to 0.013 (as the Core ML fp16 embedder does), against 0.005 over the 9 general
inputs, while its smallest cosine stays at 0.9987. Its person tolerance is
therefore 0.995 cosine and 0.02 similarity over all pairs, and 0.01 over the
pairs whose PyTorch similarity is within 0.05 of 0.55, where a change can flip
a match decision (`similarity_near_threshold_max_abs`, measured 0.0059 over
134 such pairs; `PERSON_TOLERANCES`).

Measured on macOS 27, Apple M1 Max, with the locked tools. Detector (score
diff, box diff, smallest matched IoU; 21 general and 29 person detections
matched, none missing or extra on gated runs):

| Output | Run on | General | Person | Tolerance |
|---|---|---|---|---|
| `yolox_s.onnx` | ONNX Runtime CPU (gate, person gate) | 1.4e-5, 0.001 px, 1.0000 | 3.8e-6, 0.001 px, 1.0000 | 1e-3, 0.05 px, 0.999 |
| `yolox_s_fp16.onnx` | ONNX Runtime CPU (gate) | 0.0056, 0.66 px, 0.9891 | 0.0026, 0.36 px, 0.9855 | 0.02, 2 px, 0.95 |
| `YOLOXs.mlpackage` (fp32) | Core ML, all four compute units (gate on each) | 1.5e-5, 0.001 px, 1.0000 | 5.1e-6, 0.001 px, 1.0000 | 1e-3, 0.05 px, 0.999 |
| `YOLOXsFP16.mlpackage` | Core ML CPU and GPU (gate) | 0.0057, 0.69 px, 0.9891 | 0.0033, 0.36 px, 0.9855 | 0.02, 2 px, 0.95 |
| `YOLOXsFP16.mlpackage` | Core ML all units / Neural Engine | 0.132, 8.0 px, 0.9399 | 0.075, 4.2 px, 0.9327 | recorded only |
| `YOLOXsFP16.mlpackage` | Core ML CPU only | 0.073, 8.1 px, 0.9504 | 0.032, 6.5 px, 0.7665 | recorded only |

Embedder (smallest cosine, largest similarity change; person: smallest cosine
at native size / 48 / 32 / 24 / 16 px tall):

| Output | Run on | General | Person | Person by size | Tolerance |
|---|---|---|---|---|---|
| `clip_vit_b16_laion2b.onnx` | ONNX Runtime CPU (gate, person gate) | 1.000000, 2.3e-6 | 1.000000, 1.2e-6 | 1.0000 at every size | 0.9999, 0.001 |
| `clip_vit_b16_laion2b_w8.onnx` | ONNX Runtime CPU (gate, person gate) | 0.999379, 0.0051 | 0.998734, 0.0134 (0.0059 near 0.55) | 0.99921 / 0.99926 / 0.99938 / 0.99946 / 0.99873 | 0.995, 0.01; person 0.995, 0.02, 0.01 near 0.55 |
| `clip_vit_b16_laion2b_int8.onnx` | ONNX Runtime CPU | 0.979960, 0.0469 | 0.973428, 0.0416 | 0.99057 / 0.98851 / 0.98860 / 0.98219 / 0.97343 | 0.97, 0.06 |
| `CLIPViTB16LAION.mlpackage` | Core ML all units (gate) | 0.999661, 0.0047 | 0.998214, 0.0130 | 0.99821 / 0.99956 / 0.99935 / 0.99950 / 0.99942 | 0.99, 0.02 |
| `CLIPViTB16LAION8bit.mlpackage` | Core ML all units (gate) | 0.997993, 0.0150 | 0.998389, 0.0115 | 0.99839 / 0.99844 / 0.99910 / 0.99913 / 0.99885 | 0.99, 0.03 |

Silhouettes (recorded only): every detector, on every compute unit, matches
all seven PyTorch detections in the drawn frame (four of them people); the
embedders' smallest cosines are 1.000000
(fp32), 0.999327 (weight-only int8), 0.987648 (dynamic int8), 0.999095 (Core ML
fp16) and 0.999260 (palettised). `verify.json` has every run, every compute
unit and every input.

Findings:

- **The iOS detector default is fp32.** On the Neural Engine (and on the CPU
  alone) the fp16 detector's scores move by up to 0.13 and its boxes by up to
  8 px, worst on the smallest objects; on the photo's people its smallest
  matched IoU on the CPU alone is 0.77. The fp32 package matches PyTorch as
  closely as the ONNX fp32 copy on every compute unit, so it is gated on all
  four and the app may use any. The fp16 package is kept as an option gated
  on the CPU and GPU only.
- **Small people.** No converted output loses or gains a person detection on
  the photo scaled down to 16 px people, and the embedders hold their cosine
  on 16 px crops (the dynamic int8 copy is the worst at 0.973).
- **Dynamic int8 is not good enough for matching.** Quantised as ONNX Runtime
  does by default, the embedder's cosine with PyTorch drops to about 0.86: the
  input of block 6's MLP output projection carries the outlier activations
  CLIP ViTs are known for, and per-tensor run-time quantisation of it destroys
  the embedding. With that one layer kept in fp32 (and the attention's
  activation-by-activation products never quantised) the copy reaches 0.980,
  but a similarity can still move by 0.047. The weight-only int8 copy keeps
  fp32 arithmetic, is a little smaller, and moves similarities by at most 0.005
  on the general inputs.
- The ONNX outputs were byte-identical across repeated conversions; the Core
  ML packages were not (their manifests carry fresh identifiers), which is why
  `install` checks digests against `verify.json` rather than fixed values.

### Re-pinning the person photo

The person photo is a parity input only. To replace it (if Wikimedia moves or
changes the file, or a better photo is found): choose a photograph in the
public domain or under CC0, of several people at a distance, with no
identifiable faces; check its licence on its Commons page; download the
original file with a descriptive User-Agent (Wikimedia refuses Python's
default); record its URL (`upload.wikimedia.org`, https), size and SHA-256 in
`PERSON_PHOTO` in `make_models.py`, its Commons page in the comment above it,
and its row in the licence table here; update `test_the_recorded_checksums`;
then run `make_models.py fetch` and `verify` and replace the person numbers
in this page and in the PR. `verify` needs PyTorch to find at least three
people in the whole photo and four at native resolution. Never commit the
photo or install it into an app.

## Tests

`tests/test_mobile_models.py` needs no download and no real weights. In
FlightRisk's venv the conversion round trips are skipped:

```bash
venv/bin/python -m pytest tests/test_mobile_models.py
```

In the conversion venv (pytest is in the lock) everything runs, including the
round trips that convert small or randomly initialised models to ONNX and Core
ML; `--noconftest` skips the repository's `conftest.py`, which needs the
ground's dependencies:

```bash
.venv-mobile-models/bin/python -m pytest --noconftest tests/test_mobile_models.py
```

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
- **Loosen the 12% / 0.8% box filters on Android**
  (`mobile/app/src/main/java/com/flightrisk/app/vision/PersonDetector.kt`:
  `MIN_BBOX_HEIGHT_RATIO = 0.12f` and `MIN_BBOX_AREA_RATIO = 0.008f`). They
  drop people who are small in the frame, the normal case from altitude. iOS
  uses `PersonBoxFilter.aerial` (above); Android should match it.
- **Pin the ground's embedder download.** `flightrisk/vision/reid.py` calls
  `open_clip.create_model_and_transforms(..., pretrained="laion2b_s34b_b88k")`,
  which downloads from Hugging Face without a pinned revision or a digest
  check. Its cache happens to hold the file this pipeline pins (same SHA-256),
  but a fresh ground could get another revision. Pin the revision and check the
  digest there as this pipeline does.
- **MiDaS in the pipeline.** Android's `ObstacleGuard` loads
  `midas_small.onnx`, which this pipeline does not make or list, so the release
  guard refuses a build that carries it. Add it to `make_models.py` (pinned and
  checked as in `flightrisk/drone/midas_model.py`) before shipping obstacle
  avoidance.
- **Neural Engine accuracy for the detector** (mixed precision or
  quantisation-aware conversion), so the smaller fp16 package can use the
  Neural Engine.
- **On-device checks.** The Android fp16 detector and every model's speed and
  memory on real phones are unmeasured.
- **Android app PR.** Load these files with these contracts (test against the
  golden fixtures), remove the YOLO, InsightFace, MobileCLIP and OSNet code
  paths, show `model-notices.md` on the licences screen, and update the
  privacy disclosures for sending embeddings (`docs/mobile-followups.md`,
  "Embeddings for phone-as-edge"). The iOS app is done (above).
- **iOS on a device.** Speed, memory and heat of both models on a real
  iPhone, the Neural Engine path of the fp32 detector, and the import of a
  user's model on iOS 17 itself (the simulator runs Core ML on the CPU only,
  and no iOS 17 runtime was available).
- **Embed the person in the reference photo.** The app and the ground embed
  the whole reference photo; cutting the person out first (with the detector
  and the crop rule) would compare like with like. It changes the ground too.
- **Letterbox at non-integer ratios.** The Swift letterbox is exact at integer
  ratios (including 1920 x 1080) and differs by at most one level at others,
  such as 4:3, 800 x 480 and the Tello. Porting Android's
  `ImageResize.bilinearCv` is a follow-up for those sources.
- **Pin the ground's Pillow.** The ground venv may have an older Pillow than
  the 13.0.0 the Swift port matches; pin it in `requirements-ground.txt`.

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
3. The LAION model card (at the pinned revision, section "Out-of-Scope Use")
   says: "**Any** deployed use case of the model - whether commercial or not -
   is currently out of scope." and "Certain use cases which would fall under
   the domain of surveillance and facial recognition are always out-of-scope
   regardless of performance of the model." These are statements of intended
   use, not terms of the MIT licence. Does person re-identification for search
   and rescue fall within what the card warns against, and does the owner
   accept that position?
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
