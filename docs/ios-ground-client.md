# FlightRisk iOS: standalone detector and ground console

This document describes the iOS app as it is on `main`. Every statement was checked against the Swift code under `ios/`. Where this document and the code disagree, the code is right and this document has a bug. The wire contract that the iOS and Android apps share is `docs/mobile-contract.md`. The list of things that are still open is `docs/mobile-followups.md`.

## What the iOS app is

The iOS app does two separate jobs.

1. **Standalone detector.** The phone camera finds people, compares them with a reference photo the user chose, and raises a match alert on the phone. Person detection, appearance matching and face matching run on the device (Core ML models in `ios/FlightRisk/Vision`). The optional language-model step ("reasoning") runs on the device by default. It has two other settings: off, or cloud reasoning with Claude after an explicit opt-in (see "Cloud reasoning opt-in").
2. **Ground console.** The Ground tab connects to a FlightRisk ground station, which is a computer the user runs, and shows what the ground reports (drones, video, the plan, detection profiles, the ceiling settings, the reasoning provider and match alerts). It can send a small fixed set of commands (see "What the phone can command").

The iOS app is not an edge. It never sends detections, crops, video or the reference photo to a ground station. The Android app can do that (see `mobile/README.md`). The iOS app has no direct link to a drone: the earlier direct Tello stack was removed.

The tabs are Search, Target, Settings and Ground. The Ground tab and the "Ground Station" section of Settings are the parts described here.

## The Ground tab

The tab is a pinned top bar above a scrolling list. Everything in the list is a function of the state that the ground last reported.

**Top bar (always visible, never inside the scrolling list):**

- A link pill: LIVE, CONNECTING, RECONNECTING, WRONG KEY, LOCKED (the Keychain cannot be read), NOT SET UP, REFUSED (the saved address is refused by the address policy), OFF or OFFLINE.
- An APPEARANCE ONLY badge when the ground reports that it matches by appearance only.
- The LAND ALL button.
- A text line with the connection state, and the result of the last LAND ALL (sending, failed, reached some drones, reached drones). A Retry button appears for a wrong key, and Retry now while connecting or reconnecting. After a LAND ALL that failed or got no answer, "Send LAND ALL again" and Dismiss appear. The text area has a capped height and scrolls; the buttons stay outside it.

**Scrolling list, from the top:**

- A stale banner, first in the list, whenever the state is not live (see "Safety display rules").
- Error and warning toasts from the ground (errors stay until dismissed, warnings fade) and banners for fleet events such as `RETASK FAILED`, `DRONE LOST`, `LAND ALL sent` and refused plans or starts.
- A readout such as "2 drones, 2 connected".
- One card per drone: its id (cut to 40 characters), a link chip (CONNECTED, DOWN or LOST), a control chip (HELD or MANUAL) with the HELD countdown ("HELD: auto-landing in N s, no controller"), battery, height, lane progress, the "Drone link down: ... Land still works." notice, failures the ground reported for that drone, a Land button, and a Resume search button only for a drone that is held while nobody controls it.
- Video tiles, one per drone, with the detection boxes drawn over the latest frame. A tile whose newest frame is older than 3 seconds shows NO VIDEO and no boxes. If the ground says it is not streaming video, the tile says "Video is off at the ground. Turn it on from the dashboard." The phone cannot turn video on.
- A plan grid: the zones, lanes and uncovered areas of the current plan, read only. When no plan exists it says "No plan yet. Plan from the dashboard."
- A detection profiles card, where the user can pick a profile per drone and a fleet default (see below).
- A ceiling and altitude card, read only.
- A reasoning card showing the ground's own reasoning provider, read only, with a badge "Cloud allowed" or "Cloud not allowed".
- A match alerts card, shown only once the ground has reported an alert. Rows are read only, newest first, at most 100: the level ("Possible match"), the track, the time, the score ("Score 86% (ReID 86%, face 0%)") and the ground's reasoning text. The snapshot image is dropped on arrival and never shown. The card says "Reported by the ground. Answer match alerts on the laptop."

Pull to refresh asks the ground for the full state again.

## What the phone can command

The phone can emit exactly the events in the contract allow-list (`client_emits.json`), and the Swift type `GroundCommand` has no case for anything else, so the compiler keeps other commands out. The user-visible commands are:

- **LAND ALL** (`land_all`).
- **Land** one drone (`drone_command` with `command: "land"`).
- **Resume search** of a drone that is held while nobody controls it (`manual_resume_search`).
- **Select a detection profile** for one drone, or the fleet default (`set_detection_profile`, `set_fleet_detection_profile`). This is offered only while the ground says profile changes are enabled and the state is live.

Besides those, the app sends five state requests (`get_fleet_status`, `get_detection_profiles`, `get_ceiling_settings`, `get_reasoning_provider`, `get_manual_status`) on every connect and reconnect and on pull to refresh.

The phone cannot plan or start a mission, take manual control, move or take off a drone, change the ceiling, change the ground's reasoning provider, turn video streaming on or off, set the target, answer a match alert, register or remove drones, or restart the ground. The laptop stays the mission console. A second fence, `GroundEmitPolicy`, narrows the client to the same set.

The ground decides every refusal (geofence, ceiling, slots, plan validity, link checks). The phone shows the ground's verdict and never computes a refusal of its own.

## Safety display rules

- **LAND ALL is pinned and always enabled.** It is in the top bar, outside the scrolling list and outside any collapsible section, and it is the only red element. One tap sends `land_all` with no confirmation, as on the dashboard. No drone or fleet state disables it.
- **Per-drone Land is always enabled** on every card, including DOWN, LOST, HELD and drone-link-down cards. Only the values on a card are greyed when they are not live. Land and Resume are never dimmed.
- **When the phone is not connected, LAND ALL still responds.** A tap says plainly that nothing was sent ("LAND ALL was NOT sent: this phone is not connected to the ground. Land the drones now with their own controls, or press LAND ALL on the ground laptop.") and starts a reconnect. There are separate texts for a refused key, a link that is off or not set up, a refused address, a locked Keychain, a send that failed, and a connection lost before the ground answered. The phone never queues a command for later and never resends one by itself.
- **No answer is not success.** "LAND ALL sending" turns into "no reply from the ground yet" after 5 seconds. An accepted per-drone command that gets no final answer from the drone's edge within 9 seconds (5 seconds plus the ground's 4 second edge reply timeout) becomes a failure: "no final answer from the drone's edge. Land it with its own controls." A LAND ALL tap with no ack for 40 seconds expires as a failure ("the ground never answered; land the drones with their own controls"), and a later ack is credited to a newer tap, not to the expired one. The ground itself answers a LAND ALL within 35 seconds.
- **A failed LAND ALL looks like a failure.** An ack with `reached_any` that is not exactly true, or a `fleet_event` of kind `land_all` whose detail starts with "failed", shows "LAND ALL FAILED" text that stays until dismissed. A partial ack lists which paths reached drones and which reached none.
- **Stale values are marked.** When the link drops, the last drone state is kept but marked: the link chip reads "LAST: CONNECTED" (or "LAST: DOWN", "LAST: LOST") in grey, battery and height end in "(last value)", the HELD countdown is frozen, and the stale banner is first in the list.
- **Freshness.** A connected link with no `fleet_status` for 30 seconds marks the whole state stale with "No recent update from the ground: no fleet status for 30 s." The next `fleet_status` clears it.
- **Text from the ground is cut before it is shown.** Every string from the ground is limited to 300 characters (with a trailing ellipsis) and control and bidirectional override characters are replaced by spaces; drone ids are shown cut to 40 characters but commands carry the real id. Lists are capped: 100 match alerts, 16 video tiles, 64 boxes per tile, 24 profiles, 64 drones in the profile and ceiling cards, 8 reasoning providers, 10 banners, 5 toasts, and in the plan 64 zones, 1000 lanes per zone and 500 uncovered entries. A card says when only part of a list is shown.

## Setup

1. On the ground, bind a LAN address (`--host` or `FLIGHTRISK_DASHBOARD_HOST`) and set `FLIGHTRISK_API_KEY`. The ground refuses a non-loopback bind without the key (`docs/mobile-contract.md`, "Endpoints and ports").
2. In the app, open Settings, section "Ground Station". Type the ground address (`ws://192.168.1.20:5555`; the port defaults to 5555 when omitted) and tap "Save address".
3. Type the dashboard API key into the secure field and tap "Save key". The key is saved in the iOS Keychain, the field is cleared at once, and the key is never shown again. Only "A key is saved in the Keychain." is shown. "Remove saved key" deletes it.
4. Tap "Test connection". It asks the ground for `GET /api/health` (public) and then `GET /api/status` with the key, and reports whether the ground answered, the version, and whether the key was accepted. It opens no socket.
5. Switch on "Connect to the ground station". The link is off until the user does this. The link is closed while the app is in the background and reopens when the app returns.

The address is stored in `UserDefaults`. The key is stored in the Keychain with `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` (service `com.flightrisk.app`, account `flightrisk_api_key_ground`). The contract names this secret `FLIGHTRISK_DASHBOARD_API_KEY`. The key is sent only in the Socket.IO connect packet (after the server's Engine.IO open) and in the `Authorization: Bearer` header of the health check. It is not logged, shown or put in a URL. A wrong key stops the connection and waits for the user; the app does not retry a refused key in a loop.

## LAN address policy

The ground link is plain `ws://` and `http://` on the fleet's network. The app limits cleartext as follows (`ios/FlightRiskGround/Sources/SocketIOMini/LanAddressPolicy.swift`):

- **Cleartext goes only to local addresses.** A `ws://` or `http://` connection is allowed only to 127/8, 10/8, 172.16/12, 192.168/16, 169.254/16, IPv6 `::1`, `fc00::/7`, `fe80::/10`, and an IPv4-mapped IPv6 address whose IPv4 part is allowed. Everything else is refused, including 100.64.0.0/10 (carrier-grade NAT, used by Tailscale), `0.0.0.0`, broadcast, multicast, `::` and every public address. The check runs before the key or any request is sent.
- **Names.** A host name is resolved once, off the main thread. The app connects only to those answers that are allowed (IPv4 first, then IPv6, each tried in turn) and refuses the name only when none is allowed. A name that publishes a public address next to a private one is therefore used by its private address, and the public address is never connected to. Every connection goes to the checked IP address, never to the name, so a second lookup cannot change the destination. The original name is sent as the `Host` header. **This differs from Android**, which refuses a name when any answer is public.
- **Numeric spelling.** Address text is accepted only as four plain decimal numbers (`192.168.1.20`, no leading zeros, no hex, octal or short forms) or as an IPv6 literal. Other spellings such as `0x7f.1` or `2130706433` are refused with "write the address as four decimal numbers like 192.168.1.20".
- **Proxies and cellular.** Cleartext sockets set `preferNoProxies` (only a preference: a proxy may still be used if the direct connection fails) and prohibit the cellular interface. The cleartext health check uses an empty proxy dictionary and disallows cellular access.
- **`wss://` and `https://` are allowed to any host.** They are encrypted and not subject to the cleartext rule. They connect by name, so the system's TLS validation applies. **The app does not pin certificates.**
- **The link is not encrypted for `ws://` and `http://`.** The key travels in the clear on such a link. Use the fleet access point or a trusted network. Away from it, use `wss://`, `https://`, or a VPN that gives the ground an address in a private range.
- **Lookup.** A name lookup that takes longer than the open timeout (10 seconds) is abandoned, and a lookup failure reads "Could not find HOST on the network".

Info.plist sets `NSAppTransportSecurity` with `NSAllowsLocalNetworking` and nothing broader, and `NSLocalNetworkUsageDescription` names the ground station address the user enters. The WebSocket uses `NWConnection`, which App Transport Security does not cover; the address policy above is what restricts it.

## Cloud reasoning opt-in

Cloud reasoning is off by default. The code (`ios/FlightRisk/Config/ReasoningSettings.swift`) sends the cropped image of a detected person, the reference photo and the typed description to Anthropic (`https://api.anthropic.com/v1/messages`) only when all three are true: "Allow cloud reasoning" is on (after the user confirms the consent dialog), a Claude API key is saved, and Cloud Claude is selected. A saved key alone sends nothing. Turning the switch off, selecting another provider or removing the key stops sending at once and cancels a request in progress. Claude is the only cloud provider; the removed OpenAI and Gemini entries are deleted on the first launch after they were removed. The choice and the opt-in are stored in `UserDefaults` (suite `flightrisk_config`), and the Claude key is in the Keychain. The ground's own reasoning provider is display only on the phone.

The on-device model is `mlx-community/gemma-2-2b-it-4bit`, downloaded from Hugging Face only when the user taps the download button in Settings.

## Package layout

The shared protocol code is a local Swift package, `ios/FlightRiskGround`, with no third-party dependencies. The app depends on its two products.

- **`GroundProtocol`** (pure Swift, no networking): the typed models for every server event, the state reducer and derivations (connection state, control mode, the HELD countdown), `GroundCommand` (the allow-list), the display text, the plan geometry and the fleet and refusal vocabularies. It decodes leniently: unknown keys are ignored and missing keys are "not reported".
- **`SocketIOMini`**: a small Engine.IO 4 and Socket.IO 5 client (`GroundClient`, `SocketIOCodec`), the `NWConnection` WebSocket transport, the LAN address policy, the health probe and the redaction of secrets. It was written in place of a Socket.IO library so that it can be tested offline against frames the real server encoded, and so that the address policy sits in front of every socket.

The app side is `ios/FlightRisk/State/GroundViewModel.swift` (the one view model, with the freshness, expiry and feedback logic) and `ios/FlightRisk/UI/Ground/*` (the views). Note that `GroundTransport` in `SocketIOMini` is a live protocol (the transport seam); it is not the removed Android class of the same name.

## Running the tests

Run these from the repository root.

- **Everything the machine can check:** `ios/scripts/check.sh`. It runs `swift build` and `swift test` in `ios/FlightRiskGround`, then, only if `xcrun -find metal` succeeds, an `xcodebuild build` of the app for the simulator (it sets `DEVELOPER_DIR` to `/Applications/Xcode.app/Contents/Developer` when that exists). It does not run the app's unit tests.
- **The package alone (no Xcode project, no Metal):** `cd ios/FlightRiskGround && swift test`. The three live interop tests are skipped unless `FLIGHTRISK_LIVE_TEST_URL` names a throwaway dashboard (never a real ground).
- **The app's unit tests:**

  ```
  cd ios
  DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
    -scheme FlightRisk -destination 'platform=iOS Simulator,name=iPhone 17' \
    -skipPackageUpdates -onlyUsePackageVersionsFromResolvedFile \
    -only-testing:FlightRiskTests -collect-test-diagnostics never test
  ```

  Notes from the machine this was developed on: `xcode-select` pointed at an Xcode beta whose Metal Toolchain component was not installed, so the MLX package failed to compile there (`cannot execute tool 'metal' due to missing Metal Toolchain`). Selecting the release Xcode with `DEVELOPER_DIR` avoided that. If a test fails, `xcodebuild` can spend about ten minutes collecting simulator diagnostics unless `-collect-test-diagnostics never` is given. The default `iPhone 17` simulator crashed on launch in one review round; running with the other simulator's id worked.
- The project file is generated from `ios/project.yml` with xcodegen. Regenerate it only after editing `project.yml`. Do not commit a project file regenerated with the CoreML model files in place (see the root `README.md`).

## What is not verified

None of the following has been done. The phrase "tests pass" means unit tests with scripted ground traffic and an injected resolver, not any of these:

- **No physical iPhone has run the app.** Only the simulator ran the unit tests.
- **App Transport Security and Network.framework behaviour on a device:** connecting to a `ws://` URL whose host is the checked IP address, the extra `Host` header on that connection, a scoped link-local IPv6 URL, setting the TLS server name for `wss://` to a name, and whether `NSAllowsLocalNetworking` covers a bare IP address.
- **mDNS (`.local`) names on a real network.** Only an injected resolver was used.
- **Real system proxy settings and a real VPN.**
- **The local network permission prompt** and the onboarding and Settings screens were not looked at on a device.
- **The on-screen layout**, including the capped, scrolling top-bar text area and **Dynamic Type**. The layout budget is a pure function that is unit tested; the SwiftUI views were compiled and their state was tested, not seen.
- **A real ground station or edge.** The live interop tests were run by hand against a throwaway dashboard during development; nothing in the app has been run against a real fleet.
- **The on-device Gemma model and a real call to Anthropic.** Gemma availability was faked, and no real cloud request was made.
- **Hugging Face's own handling** of the download request.

## Known follow-ups

See `docs/mobile-followups.md` for the full list with reasons. The ones that matter most for iOS:

- The cloud opt-in and provider are in `UserDefaults`, which device backups are expected to carry to a new phone (not verified on a device), while the Claude key (`ThisDeviceOnly`) is not restored. Pasting a key on the new phone would turn cloud reasoning on without the consent dialog. Android excludes the equivalent files from backup; iOS does not yet.
- After a profile selection was sent and the link was then lost, the text says the profile "was NOT changed" although the ground may have received it. The same wording exists on Android.
- The ground sends `match_alert_resolved` when the operator answers an alert. The phone ignores it, so a reminder row keeps its "the ground said this alert was waiting for the operator" text after the operator answered.
- `PrivacyNotice.migrateLegacyAcknowledgement` runs only on the onboarding path.
