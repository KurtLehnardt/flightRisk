# FlightRisk Mobile -- Field Test Protocol

## Objective

Validate the FlightRisk Mobile pipeline in real-world conditions with a cooperative target. This protocol measures detection distance, match accuracy, false positive rate, alert UX, and system stability over a 30-minute outdoor session.

## Prerequisites

- Android phone (API 26+) with FlightRisk installed and all permissions granted
- Cooperative "target" person (friend/volunteer)
- Recent, clear photo of the target person (front-facing, good lighting)
- Outdoor location with moderate foot traffic (park, mall entrance, campus quad)
- Second person to operate the phone (tester)
- Clipboard or phone notes for recording results
- Fully charged phone (or >=80% battery)
- 30 minutes minimum test duration

## Test Protocol

### Phase 1: Setup (5 min)

1. Launch FlightRisk on phone.
2. Complete onboarding flow (grant camera, location, notification permissions).
3. Navigate to target picker and select/take the target photo.
4. Verify quality report shows grade **B or better** (if grade C/D, retake photo with better lighting).
5. Open Settings and set sensitivity to **Balanced**.
6. Return to search screen and verify "Searching..." state is active with camera preview visible.
7. Note starting battery percentage: **____%**

### Phase 2: Detection Distance Test (10 min)

Have the target stand at each distance, facing the camera, for at least 10 seconds per station. The tester holds the phone steady at chest height.

| Station | Distance | Detected? | Match Score | Alert Level | Notes |
|---------|----------|-----------|-------------|-------------|-------|
| 1       | 5m       |           |             |             |       |
| 2       | 10m      |           |             |             |       |
| 3       | 15m      |           |             |             |       |
| 4       | 20m      |           |             |             |       |

Record the time-to-detection (seconds from target entering frame to first alert).

### Phase 3: Angle and Occlusion Test (5 min)

Target stands at 10m and changes pose. Hold each pose for 10 seconds.

| Pose | Description                        | Detected? | Match Score | Alert Level |
|------|------------------------------------|-----------|-------------|-------------|
| A    | Facing camera directly (0 deg)     |           |             |             |
| B    | Turned 45 degrees                  |           |             |             |
| C    | Profile view (90 deg)              |           |             |             |
| D    | Partially behind another person    |           |             |             |
| E    | Wearing hat/sunglasses (if available) |        |             |             |

### Phase 4: False Positive Test (5 min)

1. Have the **target step out of frame** entirely.
2. Walk through an area with at least 5 other people visible in the camera feed.
3. Record any false match alerts triggered on non-target individuals.
4. For each false alert:
   - Note the alert level and match score.
   - Tap "Not my child" dismiss button.
   - Verify the dismissed track does **not** re-alert within the next 30 seconds.

| False Alert # | Alert Level | Match Score | Dismissed OK? | Re-alerted? |
|---------------|-------------|-------------|---------------|-------------|
| 1             |             |             |               |             |
| 2             |             |             |               |             |
| 3             |             |             |               |             |

### Phase 5: Alert UX Test (5 min)

1. Have the target walk into frame from outside the camera view.
2. Verify the progressive confidence indicator appears on the detection overlay.
3. Verify an alert fires with the expected level (possible_match or confirmed_match).
4. Verify alert actions:
   - **Audio**: alarm tone plays (confirmed_match only).
   - **Haptic**: vibration fires (confirmed_match and possible_match).
   - **Visual**: alert card appears on screen.
5. Tap "Navigate" button on alert card -- verify it opens a map/directions intent with the GPS coordinates from the match event.
6. Tap "Not my child" dismiss on a subsequent alert -- verify audio stops, vibration stops, and the track is suppressed.

| Alert Action   | Working? | Notes |
|----------------|----------|-------|
| Audio (alarm)  |          |       |
| Haptic (vibration) |      |       |
| Visual (card)  |          |       |
| Navigate button |         |       |
| Dismiss button |          |       |

## Success Criteria

| # | Criterion                                                    | Threshold  |
|---|--------------------------------------------------------------|------------|
| 1 | Target detected within 30 seconds of entering frame at <=15m | Required   |
| 2 | Match score >= 0.45 for direct frontal match                 | Required   |
| 3 | Zero false match alerts on non-target individuals during 30-min test | Required |
| 4 | "Not my child" dismiss prevents re-alert on that track       | Required   |
| 5 | Alert audio/haptic/visual all functional                     | Required   |
| 6 | GPS coordinates recorded on match events                     | Required   |
| 7 | App does not crash during 30-minute session                  | Required   |
| 8 | Battery drain <= 15% over 30 minutes                         | Required   |

## Test Report Template

Fill in after completing all phases.

```
Test Date:       ____-____-____
Tester Name:     ________________
Device Model:    ________________
Android Version: ________________
App Version:     ________________
Location:        ________________
Weather:         ________________
Start Battery:   ____%
End Battery:     ____%
```

| Metric                          | Result     | Threshold | Pass/Fail |
|---------------------------------|------------|-----------|-----------|
| Detection distance (max)        | ___m       | >= 15m    |           |
| Time-to-detection at 10m        | ___s       | <= 30s    |           |
| Frontal match score             | ___        | >= 0.45   |           |
| 45-degree match score           | ___        | Any       |           |
| Profile match score             | ___        | Any       |           |
| Occluded match score            | ___        | Any       |           |
| False positive count            | ___        | 0         |           |
| Dismiss prevents re-alert       | Y/N        | Y         |           |
| Alert audio fired correctly     | Y/N        | Y         |           |
| Alert haptic fired correctly    | Y/N        | Y         |           |
| Alert visual fired correctly    | Y/N        | Y         |           |
| Navigate button functional      | Y/N        | Y         |           |
| GPS recorded on match           | Y/N        | Y         |           |
| 30-min stability (no crash)     | Y/N        | Y         |           |
| Battery drain                   | ____%      | <= 15%    |           |
| App crashes                     | ___        | 0         |           |

### Overall Result: [ PASS / FAIL ]

### Notes and Observations

```
(Free-form notes: lighting conditions, crowd density, any anomalies observed)
```

## Appendix: Troubleshooting

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| No detections at any distance | Camera permission not granted or ONNX model missing from assets | Check permissions; verify yolo11n.onnx in app assets |
| Match score always 0.0 | Target photo not loaded or ReID model missing | Re-select target photo; verify clip_visual.onnx in assets |
| No face scores | Face not visible or face models missing | Try frontal pose; verify scrfd_500m.onnx and arcface_mobilefacenet.onnx |
| Alert fires but no sound | Phone on silent/vibrate mode or alarm stream muted | Set phone to normal ringer mode; raise alarm volume |
| GPS shows null | Location permission denied or GPS not locked | Grant fine location permission; wait for GPS lock outdoors |
| High battery drain (>20%) | Screen brightness too high or background apps competing | Lower brightness; close other apps |
| App crashes on detection | OOM on large frame or model inference failure | Check logcat for OOM; try lower-resolution camera setting |
