# FlightRisk iOS

The iPhone app has two jobs. It is a standalone detector (the camera finds people and compares them with a reference photo, on the device), and it is a console for a FlightRisk ground station (the Ground tab). It is not an edge: it never sends detections, crops or video to a ground station, and it has no direct link to a drone.

The full description, with the safety display rules, the address policy and what has not been verified, is in [`docs/ios-ground-client.md`](../docs/ios-ground-client.md). The wire contract shared with Android is [`docs/mobile-contract.md`](../docs/mobile-contract.md). Open items are in [`docs/mobile-followups.md`](../docs/mobile-followups.md).

## What the phone can command

LAND ALL, Land for one drone, Resume search of a held drone that nobody controls, and the selection of a detection profile. Nothing else. LAND ALL and per-drone Land are always enabled, and the phone never resends a command by itself.

## Layout

```
ios/
├── project.yml                  # xcodegen spec (iOS 17, iPhone only, portrait)
├── FlightRisk.xcodeproj         # generated from project.yml
├── FlightRisk/                  # the app
│   ├── Camera, Vision, Pipeline # on-device detection and matching
│   ├── LLM, Config              # reasoning backends, cloud opt-in, Keychain
│   ├── State/GroundViewModel.swift
│   └── UI/Ground/               # the Ground tab
├── FlightRiskGround/            # local Swift package, no third-party dependencies
│   ├── Sources/GroundProtocol   # models, reducer, allow-list, display text, plan geometry
│   ├── Sources/SocketIOMini     # Engine.IO 4 / Socket.IO 5 client, LAN address policy
│   └── Tests/
├── FlightRiskTests/             # the app's unit tests
└── scripts/check.sh
```

## Setup

Open `ios/FlightRisk.xcodeproj`, choose a signing team and run. Regenerate the project with [xcodegen](https://github.com/yonaskolb/XcodeGen) only after editing `project.yml`.

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

The MLX package needs the Metal Toolchain component of Xcode. If `xcode-select` points at an Xcode where it is not installed, the build fails with "missing Metal Toolchain"; selecting another Xcode with `DEVELOPER_DIR` avoids it. The three live interop tests in the package are skipped unless `FLIGHTRISK_LIVE_TEST_URL` names a throwaway dashboard. Never point them at a real ground.

## Not verified

No physical iPhone has run the app. App Transport Security and Network.framework behaviour on a device, mDNS names on a real network, the on-screen layout and Dynamic Type, and a real ground station have not been checked. See the full list in `docs/ios-ground-client.md`.
