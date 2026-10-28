# Face recognition review and speed-up notes

Scope: `flightrisk/vision/face.py` (`FaceRecognizer`), its callers in
`flightrisk/dashboard/{app,pipeline}.py`, `edge.py`, and the optional
`insightface_face` signal in `flightrisk/vision/signals.py`.
Benchmark machine: Windows 11, 8 logical CPUs, CPU only, Python 3.13,
insightface 2.1, onnxruntime 1.31. (The execution-provider findings below were
reproduced separately on Apple Silicon with onnxruntime 1.27, insightface 1.0.1.)

## Findings (before the change)

1. **`FaceRecognizer()` crashed on non-macOS** (`face.py:51-54`). It always
   passed `CoreMLExecutionProvider`; insightface 2.1 then tries to build a
   CoreML session and raises `CoreMLSessionError` on Windows. Providers are now
   filtered against `onnxruntime.get_available_providers()`. (`signals.py:238`
   and `:376` still hardcode CoreML; ORT normally just warns there, left as is.)
2. **Whole buffalo_l pack ran for every crop** (`face.py:51`, `app.get`): besides
   detection and recognition it ran `1k3d68`, `2d106det` and `genderage`, whose
   output is never used. Measured: `app.get` 326 ms vs ~100 ms for the two
   needed models on the same crop.
3. **Fixed 640x640 detection canvas for every person crop** (`face_det_size`).
   A 90x220 crop was upscaled to 640 px: 92 ms of SCRFD vs 10-30 ms at a size
   fitted to the crop.
4. **The face was re-detected and re-embedded in every frame for the same
   person** and a second time in `pipeline.py:203` (`face.compare(...)` after
   `find_match` already scored the same crop).
5. **Largest face wins** (`face.py:78`): a neighbour's face spilling into a
   person box could be picked over the subject. On the synthetic benchmark this
   cost recall (about 83-86% instead of 100% at 60-80 px) and added false
   positives.
6. **Reference photo**: no quality check, no message when the face is tiny,
   blurry or one of many; a single embedding from a single view; the result was
   only a boolean. Also `app.py` pushed to edge devices an embedding recomputed
   from the whole photo rather than the enrolled one.
7. `signals.py` `InsightFaceR18Signal._preprocess` (`~:395`) resizes the *whole
   person crop / photo* to 112x112 and feeds it to ArcFace with no detection or
   alignment. ArcFace needs a landmark-aligned face; this signal (enabled by
   default when the ONNX file exists, weight 0.35) is likely close to noise on
   body crops. **Not changed here** (needs its own validation and the model
   file); recommended follow-up: feed it aligned faces from `FaceRecognizer`
   or drop it.

## What changed

* `vision/face.py` rewritten around SCRFD + ArcFace primitives
  (`_detect`, `_embed`, `_observe`), same public API (`set_target`,
  `set_target_from_file`, `clear_target`, `compare`, `find_match`,
  `extract_embedding`, `has_target`, `match_threshold`). New: `target_embedding`,
  `target_report` (`TargetReport`), optional `model_name`.
  * detection + recognition modules only; per-crop detection size fitted to the
    crop (multiples of 32, longest side 192-320 px; tall person crops are
    searched in the top 60 % only);
  * subject-face selection = width x quality x centrality (not largest);
  * `find_match` accepts an optional `track_id` per detection. Per track it keeps
    the best 5 quality-weighted embeddings, scores the aggregate, skips
    re-embedding once a track has 3 good observations (refresh every 4 frames),
    and backs off on repeated misses. `pipeline.py` now attaches `track_id`;
  * `compare()` of a crop just scored by `find_match` is served from a memo
    (removes the duplicate inference at `pipeline.py:203`);
  * enrolment: best-face choice, hard rejection with a clear message
    (`no_face`, `face_too_small` < 32 px), warnings (several
    faces, face < 80 px, turned > 0.45 inter-eye distances, blurry), template =
    aligned face + mirror at three resolutions (112, 56, 32 px re-upsampled),
    best-matching row wins. When enrolment fails the face target is cleared (the
    other recognisers have already switched to the new photo, so keeping the
    previous child's face would match the wrong person); the dashboard shows
    "FACE MATCHING OFF" until a usable photo is enrolled.
    `app.py` returns the report as `face_report` from `/api/upload-target`-style
    responses, the `target_set` event and on socket connect. Only the
    full-resolution row of the template (`target_embedding`) is pushed to the
    ground scorer and the edge devices; the low-resolution rows stay in the
    dashboard's `FaceRecognizer`, so the ground/edge path does not get the
    low-resolution recall gain (sending the whole template would need a protocol
    change; not done).
* `config.py`: `vision.face_model` (default `buffalo_l`), env
  `FLIGHTRISK_FACE_MODEL`. `buffalo_s` (SCRFD-500M + MobileFaceNet) is a
  supported drop-in **only if every node uses it**: the ground station and
  each edge device choose their model independently from their own
  `FLIGHTRISK_FACE_MODEL`, and both packs produce 512-d embeddings, so a
  mismatch cannot be seen from the embedding itself. Set the same
  `FLIGHTRISK_FACE_MODEL` on the ground station and on every edge. To catch
  mistakes, detection messages now carry an optional `face_model` field (older
  peers ignore it); the ground logs a startup warning with its own model and,
  when an edge reports a different one, logs `face_model_mismatch` once per
  drone and ignores that edge's face scores. An edge that does not send the
  field (older software) cannot be checked.
* Model files: only the SCRFD (`det_*`) and ArcFace (`w600k_*`) files of the pack
  are loaded; the landmark and gender/age sessions are never built (a pack with
  another file layout falls back to `FaceAnalysis`, which builds them all and
  discards the unused ones, so start-up is slower). Nothing is downloaded
  automatically: if the pack is not in `~/.insightface/models/<name>` the
  recogniser refuses to start (face matching off, with a clear log line) unless
  `FLIGHTRISK_ALLOW_MODEL_DOWNLOAD=1` is set.
* Execution providers: the SCRFD detector always runs on the CPU provider. It
  is fed a per-call input size (multiples of 32, e.g. 128x192), and CoreML
  only accepts the fixed 640x640 shape it was prepared with; on any other size
  it raises a static-output-shape error on every call. The recognition model
  has a fixed 112x112 input and may use CoreML/CUDA. `scripts/bench_face.py`
  forces CPU for both models in its baseline, so its timings are not directly
  comparable with a CoreML recogniser.
* Stored per-track observations expire after `TRACK_OBS_MAX_AGE` (60)
  `find_match` calls, so a track does not keep, or pass to another person, a
  score for a face that is no longer visible.
* `scripts/bench_face.py` (benchmark), `tests/test_face_logic.py` (model-free),
  `tests/test_face_review_fixes.py` and `tests/integration/test_face.py`.

## Benchmark methodology

`scripts/bench_face.py` uses only insightface's bundled public sample image
`t1` (six adults). Each person is an identity. Probe = person-style crop
(head + torso with neighbours partly visible) downscaled so the face is
22/30/45/60/80 px wide, random mirror, rotation +-20 deg, Gaussian blur,
brightness shift, JPEG q40-85; 6 probes per identity per size; reference = clean
head-and-shoulders crop (about 100 px face). 1080 genuine/impostor comparisons.
`--tracks` feeds 8-frame "one second" sequences through `find_match` with a
`track_id`. Caveats: only 6 adult identities, mostly frontal, reference and
probes come from one photo, so absolute scores are optimistic and AUC saturates
near 1.0; no children, no real profile or occluded faces, no age gap. Use it for
relative comparisons and timing. Timings are noisy on this laptop (+-15 %).

## Results

Cost per person crop (stateless `compare`, mean over the grid):

| Config | ms/crop (mean / p95) |
|---|---|
| before (full pack, 640 canvas) | 583 / 855 |
| after, `buffalo_l` | 173-182 / 207 |
| after, `buffalo_s` | 24.5 / 45 |
| tall 85x221 crops: before / `buffalo_l` / `buffalo_s` | 360 / 146 / 50 (face found 11 / 16 / 12 of 18) |

Tracked 8-frame sequences, `buffalo_l`, ms per `find_match` frame: 184-200
without track reuse vs 89-139 with it; `buffalo_s` 16-27 vs 10-21. Track-level
aggregation also fixes the "only the last frame counts" weakness: for 22 px faces
the final-frame-only recall was 0.50 vs 1.00 with aggregation (30 px: 0.83 vs
1.00), at 0 false positives in both.

Accuracy at the existing thresholds (recall / false-positive rate of impostor
probes):

| face px | before @0.35 | after `buffalo_l` @0.35 | before @0.45 | after @0.45 |
|---|---|---|---|---|
| 22 | 0.56 / 0.000 | 0.75 / 0.000 | 0.28 / 0.000 | 0.50 / 0.000 |
| 30 | 0.92 / 0.000 | 1.00 / 0.000 | 0.81 / 0.000 | 0.86 / 0.000 |
| 45 | 1.00 / 0.000 | 1.00 / 0.000 | 1.00 / 0.000 | 1.00 / 0.000 |
| 60 | 1.00 / 0.000 | 0.94 / 0.011 | 1.00 / 0.000 | 0.94 / 0.011 |
| 80 | 1.00 / 0.000 | 1.00 / 0.000 | 1.00 / 0.000 | 1.00 / 0.000 |

Overall AUC 1.000 before, 0.996 after: the single regression is one impostor
probe and one genuine probe at 60 px (1 of ~90 each), so within noise for n=6
identities but not zero. The low-resolution template rows are what lifts the
22-30 px recall (without them: 0.56 / 0.89 @0.35); they also nudge the mean
impostor score from 0.04 to 0.05. `buffalo_s` matched `buffalo_l` at 30-80 px
but found 97 % of 22 px faces and only 12/18 tall crops, so it is **not** the
default. Switch with `FLIGHTRISK_FACE_MODEL=buffalo_s` when frame rate matters
more than tiny-face recall (cost 24 ms vs 175 ms per face).

Reference-photo behaviour (synthetic): clean 107 px face accepted without
warning; Gaussian blur sigma >= 2 (Laplacian variance of the aligned face < 60)
warns; faces 21 px and 12 px wide are rejected with a message; a group photo
enrols with a multiple-faces warning; noise gives `no_face`.

## Ideas evaluated and rejected / not done

* **Lower detector threshold (0.4)**: identical results on the benchmark; left
  at 0.5.
* **Flip test-time augmentation on probes**: doubles recognition cost (the
  dominant term, 80-140 ms for ResNet50) for an effect not measured here; flip
  is used for the reference template only, where it is free.
* **Batching probes**: ResNet50 batch-2 cost 270 ms vs 142 ms for batch-1 on this
  CPU, so no gain; detection sizes differ per crop anyway.
* **ORT thread/graph options**: default (all cores, ORT_ENABLE_ALL) was already
  fastest for r50 (83 ms) vs 1-8 explicit intra-op threads (95-237 ms).
* **AdaFace / MagFace / RetinaFace / YuNet**: no ONNX weights are bundled and
  none could be validated honestly here (no low-quality face dataset on the
  machine); SCRFD + ArcFace (w600k) is already the standard stack, and
  `det_10g` handles small faces better than YuNet. AdaFace is the one worth a
  future trial for low resolution; it needs a labelled low-res benchmark.
* **Age robustness for children**: no data to validate (no child images were
  used by design). Recommendation: let operators enrol several photos of the
  child from different ages and average them; the template structure here
  already supports rows from multiple photos.
* **int8 quantisation of the recogniser**: accuracy risk on 22-30 px faces,
  not validated.
* **Fixing `InsightFaceR18Signal`** (finding 7): needs the model file and its own
  evaluation.

## Follow-ups and mobile parity

* `ios/FlightRisk/Vision/FaceRecognizer.swift` and
  `InsightFaceMatcher.swift` use SCRFD-500M + MobileFaceNet, i.e. the
  `buffalo_s` family, with a fixed 640x640 detector, largest-face selection and
  a single-embedding target. Worth porting: subject-face selection with
  centrality, fitted detection size, per-track embedding aggregation, and the
  reference quality report (use the same codes and messages). The Android app
  (`mobile/`, see `MatchingAlgorithmConfig.kt`) should get the same.
* Thresholds (0.35 dashboard / 0.45 config) are shared across `buffalo_l` and
  MobileFaceNet; MobileFaceNet scores are slightly higher for genuine pairs at
  small sizes in this benchmark, but retune on a real validation set before
  changing anything.
* Edge devices (`edge.py`) still send single-frame embeddings; the ground
  station could aggregate per track the same way. The ground also only has the
  full-resolution template row (see above).
* Build a small consented low-resolution child/adult validation set from field
  tests (`docs/field-test-protocol.md`) to calibrate thresholds, the quality
  warnings (blur 60, yaw 0.45, 80 px are heuristics) and to evaluate AdaFace.
