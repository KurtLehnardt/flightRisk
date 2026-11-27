# FlightRisk iOS

The iPhone app has two jobs. It searches the video of a connected drone for the person in a reference photo, on the device, and it is a console for a FlightRisk ground station (the Ground tab). A search starts only while a drone is connected: the phone's own camera is never used to search, and is used only to take the target photo. It is not an edge: it never sends detections, crops or video to a ground station.

The full description, with the safety display rules, the address policy and what has not been verified, is in [`docs/ios-ground-client.md`](../docs/ios-ground-client.md). The wire contract shared with Android is [`docs/mobile-contract.md`](../docs/mobile-contract.md). Open items are in [`docs/mobile-followups.md`](../docs/mobile-followups.md).

## What the phone can command

LAND ALL, Land for one drone, Resume search of a held drone that nobody controls, and the selection of a detection profile. Nothing else. LAND ALL and per-drone Land are always enabled, and the phone never resends a command by itself.

## Layout

```
ios/
├── project.yml                  # xcodegen spec (iOS 17, iPhone only, portrait)
├── FlightRisk.xcodeproj         # generated from project.yml
├── Config/Signing.xcconfig      # signing team (empty); includes the gitignored Signing.local.xcconfig
├── FlightRisk/                  # the app
│   ├── Assets.xcassets          # AppIcon (one 1024 px image, made by branding/make_app_icon.py)
│   ├── PrivacyInfo.xcprivacy    # privacy manifest (see docs/app-store-compliance.md)
│   ├── Vision, Pipeline         # on-device detection and matching (drone video frames)
│   ├── LLM, Config              # reasoning backends, cloud opt-in, Keychain
│   ├── State/GroundViewModel.swift
│   └── UI/Ground/               # the Ground tab
├── FlightRiskGround/            # local Swift package, no third-party dependencies
│   ├── Sources/GroundProtocol   # models, reducer, allow-list, display text, plan geometry
│   ├── Sources/SocketIOMini     # Engine.IO 4 / Socket.IO 5 client, LAN address policy
│   ├── Sources/PersonVision     # detector and embedder pre- and postprocessing, user-model checks
│   └── Tests/
├── FlightRiskTests/             # the app's unit tests
├── FlightRiskUITests/           # UI tests of the demo (LAND ALL never covered)
└── scripts/check.sh
```

## Setup

Set your signing team (below), open `ios/FlightRisk.xcodeproj` and run. Regenerate the project with [xcodegen](https://github.com/yonaskolb/XcodeGen) only after editing `project.yml`.

### Signing

The team ID is never committed. Both targets take their signing settings from `ios/Config/Signing.xcconfig`, which leaves `DEVELOPMENT_TEAM` empty and then includes `ios/Config/Signing.local.xcconfig` if it exists. That file is gitignored. To sign:

```
cp ios/Config/Signing.local.xcconfig.example ios/Config/Signing.local.xcconfig
# edit it: DEVELOPMENT_TEAM = <your ten-character team ID>
```

Do not pick the team in Xcode's Signing & Capabilities pane: Xcode would write it into `project.pbxproj`, and the next `xcodegen` run would drop it. The bundle ID is `com.flightrisk.app` (`PRODUCT_BUNDLE_IDENTIFIER` in `project.yml`); register it, or change it, in your developer account before the first device build.

### Privacy policy address

The onboarding notice links to the address in the `FLIGHTRISK_PRIVACY_POLICY_URL` build setting (default: the policy on GitHub). It reaches the app through the `FlightRiskPrivacyPolicyURL` key in `Info.plist`; an address that is not `https://` falls back to the default. Change it in `project.yml`, or override it in `Signing.local.xcconfig`. A `//` in an xcconfig value starts a comment, so `FLIGHTRISK_PRIVACY_POLICY_URL = https://example.com/privacy` is cut off at `https:` and the app falls back to the default address without any error. Write it as `https:/$()/example.com/privacy`: the empty `$()` stops the two slashes from being read as a comment. Both the onboarding notice and Settings link to the policy. `PrivacyComplianceTests` compares the address in the built app with the build setting, and a debug build stops with an assertion if the address was rejected.

### Build number

`CFBundleVersion` is `$(CURRENT_PROJECT_VERSION)`, set in `project.yml` (currently 1). App Store Connect rejects an upload whose build number is not higher than the previous one for the same version, so raise it for each upload: change `CURRENT_PROJECT_VERSION` in `project.yml` and run `xcodegen`. That is the only supported method. Do not use `agvtool new-version -all`: it overwrites `$(CURRENT_PROJECT_VERSION)` in `Info.plist` with a literal number, and the build number then no longer follows `project.yml`. The marketing version (`CFBundleShortVersionString`) is separate.

### Models and the licence guard (release builds)

Before each archive for App Store Connect, on the release build machine:

1. Clear old models. A developer checkout may still hold the old compiled models (`YOLOPersonDetector.mlmodelc`, `SCRFDFaceDetector.mlmodelc`, `ArcFaceMobile.mlmodelc`, `CLIPVisual.mlmodelc`) in `ios/FlightRisk/Models`. They are gitignored and must never ship. Archive from a fresh clone, or list them with `git clean -ndX ios/FlightRisk/Models` and remove them with `git clean -fdX ios/FlightRisk/Models`.
2. Build and install the models: `scripts/mobile_models/make_models.py all --targets ios`, in its own virtual environment installed with `pip install --require-hashes -r scripts/mobile_models/requirements.lock` (`docs/mobile-models.md`). It installs `YOLOXs.mlpackage`, `CLIPViTB16LAION.mlpackage`, `mobile-models.json` and `model-notices.md` into `ios/FlightRisk/Models`.
3. Archive. The FlightRisk target's last build phase, "Licence guard (Release)", runs `scripts/mobile_models/licence_guard.py` (with `/usr/bin/env python3`, 3.9 or newer) on the built `.app` in every Release build, archives included, and fails the build if the app carries any Core ML model whose metadata does not name the pinned weights, any other model (by name, SHA-256 or bytes), or any zip or gzip file anywhere in the app, frameworks included. Debug builds skip it. To check an archive by hand: `python3 scripts/mobile_models/licence_guard.py path/to/FlightRisk.xcarchive`.

The models are optional for a build. The FlightRisk target's build phase "Core ML models (optional)" compiles `YOLOXs.mlpackage` and `CLIPViTB16LAION.mlpackage` from `ios/FlightRisk/Models` into the app when they are there and copies `mobile-models.json` and `model-notices.md` beside them; `project.yml` keeps the folder out of the sources, because Xcode fails a build whose sources name a missing file. Without the models the app builds (a note in Debug, a warning in Release) and its Search tab shows "Person detection unavailable", so never archive for the store without them. Matching is by appearance only: the app has no face detection or face recognition model. Settings > About > Model licences shows `model-notices.md`. Settings > Person Detector can import a user's own YOLO model (off by default; see "Your own person detector (iOS)" in `docs/mobile-models.md`).

### App icon

`FlightRisk/Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png` is a full-bleed 1024 x 1024 crop of `branding/flightRiskIcon.png`, with no alpha channel. Make it again after changing the artwork:

```
venv/bin/python branding/make_app_icon.py
```

To use the Ground tab, open Settings, section "Ground Station": enter the address (`ws://192.168.1.20:5555`), enter the ground's `FLIGHTRISK_API_KEY` (it goes into the Keychain and is never shown again), tap "Test connection", and switch on "Connect to the ground station". The link is off until you do. A `ws://` or `http://` address must be on the local network; `wss://` and `https://` are allowed anywhere. Certificates are not pinned.

Cloud reasoning is off by default. See "Cloud reasoning opt-in" in the full description.

## Tests

```
ios/scripts/check.sh                  # package build and tests; app build if a Metal toolchain exists
cd ios/FlightRiskGround && swift test # the package alone, no Xcode project needed
```

The app's unit tests need the release Xcode, selected explicitly, and the iPhone simulator:

```
cd ios
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -scheme FlightRisk -destination 'platform=iOS Simulator,name=iPhone 17' \
  -skipPackageUpdates -onlyUsePackageVersionsFromResolvedFile \
  -only-testing:FlightRiskTests -collect-test-diagnostics never test
```

The model tests in `FlightRiskTests` (`OnDeviceModelTests`) run only in a build with the models installed and need the golden fixtures of `make_models.py verify`: pass `TEST_RUNNER_FLIGHTRISK_GOLDEN_DIR=<repo>/build/mobile_models/golden` to `xcodebuild` (the package tests read `FLIGHTRISK_GOLDEN_DIR`, and both fall back to `build/mobile_models/golden` in the checkout). They are skipped otherwise. `CustomDetectorTests` imports a user's own model end to end only when `TEST_RUNNER_FLIGHTRISK_TEST_CUSTOM_DETECTOR` names one on the test machine; never commit such a model.

The demo's UI tests (`FlightRiskUITests`) run the same way with `-only-testing:FlightRiskUITests`. They start the demo from onboarding and check that LAND ALL stays visible and tappable at the default and the largest accessibility text size; they take a few minutes.

The MLX package needs the Metal Toolchain component of Xcode. If `xcode-select` points at an Xcode where it is not installed, the build fails with "missing Metal Toolchain"; install it with `xcodebuild -downloadComponent MetalToolchain` (no administrator password needed), or select another Xcode with `DEVELOPER_DIR`. The three live interop tests in the package are skipped unless `FLIGHTRISK_LIVE_TEST_URL` names a throwaway dashboard. Never point them at a real ground.

## Not verified

No physical iPhone has run the app. App Transport Security and Network.framework behaviour on a device, mDNS names on a real network, the on-screen layout and Dynamic Type, and a real ground station have not been checked. See the full list in `docs/ios-ground-client.md`.
