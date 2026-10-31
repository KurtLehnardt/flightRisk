import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try JSONDecoder().decode(T.self, from: Data(json.utf8))
}

private func fixture(_ file: String, _ name: String) throws -> String {
    try ContractFixtures.payloadJSON(file, name)
}

private func profiles(_ name: String) throws -> DetectionProfilesPayload {
    try decode(DetectionProfilesPayload.self, try fixture("detection_profiles.json", name))
}

private func ceiling(_ name: String) throws -> CeilingSettingsPayload {
    try decode(CeilingSettingsPayload.self, try fixture("ceiling_settings.json", name))
}

/// The pure state of the profile and ceiling cards: every fixture state, the dashboard wording,
/// the caps, the not-live tags and the contrast of the colours.
final class ProfilesCeilingViewStateTests: XCTestCase {
    // MARK: profiles, fixture states

    func testFallbackShowsWeightsMissingAsAWarning() throws {
        let p = try profiles("fallback")
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        let row = try XCTUnwrap(card.drones.first)
        XCTAssertEqual(row.statusText, "Active: Startup default (yolo11n) (fallback: \(MissionFormat.profileLabel(p, "top_down")) weights missing on the edge).")
        XCTAssertEqual(row.severity, .warn)
        XCTAssertNil(card.blockedNote)
        XCTAssertTrue(row.canSelect)
    }

    func testPendingShowsSwitchingAndBlocksThatDrone() throws {
        let p = try profiles("pending")
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        let row = card.drones[0]
        XCTAssertEqual(row.statusText, "Switching to \(MissionFormat.profileLabel(p, "top_down"))...")
        XCTAssertEqual(row.severity, .info)
        XCTAssertFalse(row.canSelect, "no selection while a switch is pending")
    }

    func testRefusedInFlightShowsMismatchAsBad() throws {
        let p = try profiles("refused")
        let row = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true)?.drones.first)
        XCTAssertTrue(row.statusText.hasPrefix("MISMATCH: chosen "), row.statusText)
        XCTAssertTrue(row.statusText.contains("Refused in flight; sent again when the drone is on the ground."))
        XCTAssertEqual(row.severity, .bad)
    }

    func testUnsupportedDroneHasNoSelector() throws {
        let p = try profiles("unsupported")
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        let two = try XCTUnwrap(card.drones.first { $0.id == "drone-2" })
        XCTAssertFalse(two.supported)
        XCTAssertTrue(two.options.isEmpty)
        XCTAssertFalse(two.canSelect)
        XCTAssertEqual(two.statusText, MissionFormat.unsupportedEdge)
        XCTAssertEqual(card.fleetDefaultText, "Fleet default: \(MissionFormat.profileLabel(p, "default"))")
        XCTAssertEqual(card.fleetOptions.first { $0.selected }?.value, "default")
    }

    func testStandbyControlsOffHidesSelectorsAndShowsTheGroundsNote() throws {
        let p = try profiles("standby_controls_disabled")
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        XCTAssertTrue(card.controlsOff)
        XCTAssertFalse(card.canSelect)
        XCTAssertEqual(card.blockedNote, GroundText.shorten(p.controlsNote))
        XCTAssertTrue(card.blockedNote?.contains("standby") == true)
        XCTAssertFalse(card.drones[0].canSelect)
    }

    func testNoBoardHidesTheSection() throws {
        XCTAssertNil(MissionFormat.profilesCard(try profiles("no_board"), isStale: false, connected: true))
        XCTAssertNil(MissionFormat.profilesCard(nil, isStale: false, connected: true))
    }

    func testControlsEnabledMissingMeansOff() throws {
        var p = try profiles("fallback")
        p.controlsEnabled = nil
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        XCTAssertEqual(card.blockedNote, MissionFormat.controlsOffNote)
        XCTAssertFalse(card.canSelect)
    }

    func testStaleAndDisconnectedBlockSelection() throws {
        let p = try profiles("fallback")
        let stale = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: true, connected: true))
        XCTAssertEqual(stale.blockedNote, MissionFormat.notLiveNote)
        XCTAssertFalse(stale.canSelect)
        XCTAssertFalse(stale.drones[0].canSelect)
        let off = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: false))
        XCTAssertEqual(off.blockedNote, MissionFormat.notConnectedNote)
        XCTAssertFalse(off.controlsOff, "the selectors stay, disabled, when only the link is down")
    }

    func testFleetOptionWordingAndSelection() throws {
        let p = try profiles("fallback")
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        XCTAssertEqual(card.fleetOptions.first?.label, "Keep each drone's startup profile")
        XCTAssertEqual(card.fleetOptions.first?.selected, true, "no fleet default selects the first option")
        XCTAssertEqual(card.drones[0].options.first?.label, "Follow fleet default")
        XCTAssertEqual(card.fleetOptions.map(\.value), ["", "default", "top_down", "level"])
    }

    // MARK: the detector-failing wrapper

    func testDetectorFailingWrapsTheStatusForTrueFalseAndMissing() throws {
        var p = try profiles("fallback")
        let base = MissionFormat.profileStatusCore(p.drones!["drone-1"]!, p)
        p.drones!["drone-1"]!.detectorOk = false
        let failing = MissionFormat.profileStatus(p.drones!["drone-1"]!, p)
        XCTAssertEqual(failing.text, "DETECTOR FAILING: the edge cannot process frames. " + base.text)
        XCTAssertEqual(failing.severity, .bad)
        for value in [true, nil] as [Bool?] {
            p.drones!["drone-1"]!.detectorOk = value
            let r = MissionFormat.profileStatus(p.drones!["drone-1"]!, p)
            XCTAssertEqual(r.text, base.text, "detector_ok \(String(describing: value)) leaves the status alone")
            XCTAssertEqual(r.severity, base.severity)
        }
        // The wrapper also goes in front of a pending status.
        var q = try profiles("pending")
        q.drones!["drone-1"]!.detectorOk = false
        let pending = MissionFormat.profilesCard(q, isStale: false, connected: true)!.drones[0]
        XCTAssertTrue(pending.statusText.hasPrefix(MissionFormat.detectorFailing + "Switching to"))
        XCTAssertEqual(pending.severity, .bad)
    }

    // MARK: caps and shortening

    func testProfileAndDroneListsAreCapped() throws {
        var p = try profiles("fallback")
        p.profiles = (0..<10_000).map { i in
            var d = try! decode(DetectionProfile.self, "{}")
            d.name = "p\(i)"
            d.label = String(repeating: "L", count: 5_000)
            return d
        }
        var drones: [String: DroneProfileState] = [:]
        for i in 0..<10_000 {
            var s = try! decode(DroneProfileState.self, "{}")
            s.supported = true
            s.active = "p1"
            s.note = "no_reply"
            drones[String(repeating: "d", count: 5_000) + "\(i)"] = s
        }
        p.drones = drones
        let card = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true))
        XCTAssertEqual(card.drones.count, MissionFormat.maxDrones)
        XCTAssertEqual(card.fleetOptions.count, MissionFormat.maxProfiles + 1)
        XCTAssertTrue(card.partial)
        XCTAssertLessThanOrEqual(card.fleetOptions[1].label.count, 80)
        XCTAssertLessThanOrEqual(card.drones[0].displayId.count, GroundText.maxIdText)
        XCTAssertLessThanOrEqual(card.drones[0].statusText.count, 400)
    }

    func testGroundTextIsShortenedInStatusLines() throws {
        var p = try profiles("refused")
        p.drones!["drone-1"]!.refusal!.reason = .unknown(String(repeating: "r", count: 10_000))
        p.drones!["drone-1"]!.refusal!.detail = String(repeating: "x", count: 10_000)
        p.drones!["drone-1"]!.mismatch = false
        let row = try XCTUnwrap(MissionFormat.profilesCard(p, isStale: false, connected: true)?.drones.first)
        XCTAssertLessThan(row.statusText.count, 500)
        XCTAssertTrue(row.statusText.contains("\u{2026}"))
    }

    // MARK: ceiling

    func testCeilingOnGroundInSync() throws {
        let c = try ceiling("on_ground_in_sync")
        let card = try XCTUnwrap(MissionFormat.ceilingCard(c))
        XCTAssertEqual(card.headline, "Ceiling 200 cm, margin 60 cm")
        XCTAssertEqual(card.maxSlotText, "Highest allowed slot: 140 cm (ceiling minus margin)")
        XCTAssertNil(card.lockedText)
        XCTAssertEqual(card.drones[0].slotText, "Slot 100 cm")
        XCTAssertEqual(card.drones[0].edgeText, "Edge: ceiling 200, margin 60, slot 100 cm")
        XCTAssertEqual(card.drones[0].edgeSeverity, .info)
        XCTAssertNil(card.notLiveNote)
    }

    func testCeilingEdgePendingAndOwnSlot() throws {
        let card = try XCTUnwrap(MissionFormat.ceilingCard(try ceiling("edge_pending")))
        XCTAssertEqual(card.headline, "Ceiling 180 cm, margin 50 cm")
        XCTAssertTrue(card.drones.contains { $0.edgeText == "Sending to the edge..." && $0.edgeSeverity == .info })
        let own = try XCTUnwrap(card.drones.first { $0.id == "drone-2" })
        XCTAssertEqual(own.slotText, "Slot 120 cm (own; default 130 cm)")
    }

    func testCeilingEdgeRefusedShowsTheEdgesText() throws {
        let c = try ceiling("edge_refused")
        let card = try XCTUnwrap(MissionFormat.ceilingCard(c))
        let refused = try XCTUnwrap(card.drones.first { $0.edgeText.hasPrefix("Edge refused: ") })
        XCTAssertEqual(refused.edgeSeverity, .warn)
        let detail = c.drones?.compactMap { $0.edge?.refusal?.detail }.first
        XCTAssertEqual(refused.edgeText, "Edge refused: " + GroundText.shorten(detail))
    }

    func testCeilingLockedInFlight() throws {
        let c = try ceiling("locked_in_flight")
        let card = try XCTUnwrap(MissionFormat.ceilingCard(c))
        XCTAssertEqual(card.lockedText, "Locked in flight: " + GroundText.shorten(c.lockedReason))
    }

    func testCeilingSlotAboveLimitShowsTheProblemAndOlderEdge() throws {
        let c = try ceiling("slot_above_limit")
        let card = try XCTUnwrap(MissionFormat.ceilingCard(c))
        let two = try XCTUnwrap(card.drones.first { $0.problem != nil })
        XCTAssertEqual(two.problem, GroundText.shorten(c.drones?.first { $0.problem != nil }?.problem))
        XCTAssertEqual(two.edgeText, "Edge does not report a ceiling (older edge): it flies the planned slot only.")
    }

    func testCeilingUnavailableHidesTheSection() throws {
        XCTAssertNil(MissionFormat.ceilingCard(try ceiling("unavailable")))
        XCTAssertNil(MissionFormat.ceilingCard(nil))
    }

    func testCeilingLoadErrorAndCaps() throws {
        var c = try ceiling("on_ground_in_sync")
        c.loadError = String(repeating: "e", count: 10_000)
        c.drones = (0..<10_000).map { i in
            var d = try! decode(CeilingDrone.self, "{}")
            d.droneId = String(repeating: "d", count: 1_000) + "\(i)"
            d.problem = String(repeating: "p", count: 10_000)
            return d
        }
        let card = try XCTUnwrap(MissionFormat.ceilingCard(c))
        XCTAssertEqual(card.drones.count, MissionFormat.maxDrones)
        XCTAssertTrue(card.partial)
        XCTAssertLessThan(card.loadErrorText?.count ?? .max, 500)
        XCTAssertLessThanOrEqual(card.drones[0].problem?.count ?? .max, 300)
    }

    func testNotLiveTagOnTheReadOnlyCards() throws {
        XCTAssertEqual(MissionFormat.ceilingCard(try ceiling("on_ground_in_sync"), isStale: true)?.notLiveNote, "Not live: last values from the ground.")
        XCTAssertNil(MissionFormat.ceilingCard(try ceiling("on_ground_in_sync"), isStale: false)?.notLiveNote)
    }

    // MARK: contrast

    func testEveryTextPairKeepsAtLeast4Point5To1InBothThemes() {
        func luminance(_ c: MissionPalette.RGB) -> Double {
            func channel(_ v: Int) -> Double {
                let s = Double(v) / 255
                return s <= 0.03928 ? s / 12.92 : pow((s + 0.055) / 1.055, 2.4)
            }
            return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b)
        }
        func ratio(_ a: MissionPalette.RGB, _ b: MissionPalette.RGB) -> Double {
            let (hi, lo) = (max(luminance(a), luminance(b)), min(luminance(a), luminance(b)))
            return (hi + 0.05) / (lo + 0.05)
        }
        let pairs = MissionPalette.textPairs
        XCTAssertEqual(pairs.count, 18)
        for pair in pairs {
            XCTAssertGreaterThanOrEqual(ratio(pair.foreground, pair.background), 4.5, "\(pair.name) on \(pair.background)")
        }
    }
}

/// Selection through the view model: the exact payload, the refusals, the waiting text and its
/// timeout, the link loss, and the set of commands the view model can emit.
@MainActor
final class ProfileSelectionTests: XCTestCase {
    private var harness: GroundHarness!
    private var vm: GroundViewModel { harness.viewModel }

    override func setUp() async throws {
        try await super.setUp()
        harness = GroundHarness()
    }

    override func tearDown() async throws {
        await harness.tearDown()
        harness = nil
        try await super.tearDown()
    }

    /// Connected, live and holding the named detection_profiles fixture.
    private func live(_ fixtureName: String, json: String? = nil) async throws -> FakeGroundTransport {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        let payload = try json ?? fixture("detection_profiles.json", fixtureName)
        await harness.push(t, "detection_profiles", payload) { self.vm.detectionProfiles != nil }
        return t
    }

    private func sentPayloads(_ t: FakeGroundTransport, _ event: String) throws -> [String] {
        try t.sent.compactMap { frame in
            guard frame.hasPrefix("42") else { return nil }
            let array = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(frame.dropFirst(2).utf8)) as? [Any])
            guard array.first as? String == event, array.count > 1 else { return nil }
            let data = try JSONSerialization.data(withJSONObject: array[1], options: [.sortedKeys, .withoutEscapingSlashes])
            return String(decoding: data, as: UTF8.self)
        }
    }

    private func setCount(_ t: FakeGroundTransport) -> Int {
        t.sentEventNames.filter { $0 == "set_detection_profile" || $0 == "set_fleet_detection_profile" }.count
    }

    func testDroneSelectionSendsExactlyTheContractPayload() async throws {
        let t = try await live("fallback")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertEqual(try sentPayloads(t, "set_detection_profile"), [#"{"drone_id":"drone-1","profile":"top_down"}"#])
        XCTAssertEqual(setCount(t), 1)
        XCTAssertEqual(vm.profileFeedback?.awaitsAnswer, true)
        XCTAssertEqual(vm.profileFeedback?.text, "Profile change sent for drone-1: \(MissionFormat.profileLabel(vm.detectionProfiles!, "top_down")). Waiting for the ground's answer.")
    }

    func testFollowFleetDefaultSendsAnEmptyProfile() async throws {
        var p = try profiles("fallback")
        p.drones!["drone-1"]!.override = "top_down"
        let json = String(decoding: try JSONEncoder().encode(p), as: UTF8.self)
        let t = try await live("", json: json)
        await vm.selectDroneProfile("drone-1", profile: "")
        XCTAssertEqual(try sentPayloads(t, "set_detection_profile"), [#"{"drone_id":"drone-1","profile":""}"#])
        XCTAssertTrue(vm.profileFeedback?.text.contains("Follow fleet default") == true)
    }

    func testFleetSelectionAndClearingTheDefault() async throws {
        let t = try await live("unsupported")
        await vm.selectFleetProfile("top_down")
        XCTAssertEqual(try sentPayloads(t, "set_fleet_detection_profile"), [#"{"profile":"top_down"}"#])
        await vm.selectFleetProfile("")
        XCTAssertEqual(try sentPayloads(t, "set_fleet_detection_profile").last, #"{"profile":""}"#)
        XCTAssertTrue(vm.profileFeedback?.text.contains("Keep each drone's startup profile") == true)
    }

    func testUnchangedChoiceSendsNothing() async throws {
        let t = try await live("unsupported")
        await vm.selectFleetProfile("default")   // already the fleet default
        XCTAssertEqual(setCount(t), 0)
        XCTAssertNil(vm.profileFeedback)
        let refused = try await live2OverrideCase()
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertEqual(setCount(refused), 0)
    }

    private func live2OverrideCase() async throws -> FakeGroundTransport {
        // drone-1 already has the override top_down and is not pending ("refused" fixture).
        let t = harness.recorder.last!
        await harness.push(t, "detection_profiles", try fixture("detection_profiles.json", "refused")) { self.vm.detectionProfiles?.drones?["drone-1"]?.override == "top_down" }
        return t
    }

    func testRefusedWhilePending() async throws {
        let t = try await live("pending")
        await vm.selectDroneProfile("drone-1", profile: "level")
        XCTAssertEqual(setCount(t), 0)
        XCTAssertEqual(vm.profileFeedback?.text, "Profile for drone-1 was NOT changed: " + MissionFormat.whyPending)
        XCTAssertEqual(vm.profileFeedback?.isFailure, true)
    }

    func testRefusedWhileStale() async throws {
        let t = await harness.connect()   // connected, but no fleet_status yet: the state is not live
        await harness.push(t, "detection_profiles", try fixture("detection_profiles.json", "fallback")) { self.vm.detectionProfiles != nil }
        XCTAssertTrue(vm.isStateStale)
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        await vm.selectFleetProfile("top_down")
        XCTAssertEqual(setCount(t), 0)
        XCTAssertEqual(vm.profileFeedback?.text, "Fleet default profile was NOT changed: " + MissionFormat.notLiveNote)
    }

    func testRefusedWhenTheGroundTurnedControlsOff() async throws {
        let t = try await live("standby_controls_disabled")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertEqual(setCount(t), 0)
        XCTAssertTrue(vm.profileFeedback?.text.contains("standby") == true)
    }

    func testRefusedForUnknownUnsupportedAndBlankTargets() async throws {
        let t = try await live("unsupported")
        await vm.selectDroneProfile("drone-1", profile: "no_such_profile")
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyUnknownProfile) == true)
        await vm.selectDroneProfile("drone-9", profile: "top_down")
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyUnknownDrone) == true)
        await vm.selectDroneProfile("drone-2", profile: "top_down")
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyUnsupported) == true)
        await vm.selectDroneProfile("", profile: "top_down")
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyUnknownDrone) == true)
        await vm.selectDroneProfile("  ", profile: "top_down")
        XCTAssertEqual(setCount(t), 0)
    }

    func testBlankDroneIdInTheGroundsListIsNeverSent() async throws {
        var p = try profiles("fallback")
        p.drones = [" ": p.drones!["drone-1"]!]
        let t = try await live("", json: String(decoding: try JSONEncoder().encode(p), as: UTF8.self))
        await vm.selectDroneProfile(" ", profile: "top_down")
        XCTAssertEqual(setCount(t), 0)
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyNoUsableID) == true)
    }

    func testRefusedWithoutABoard() async throws {
        let t = try await live("no_board")
        await vm.selectFleetProfile("top_down")
        XCTAssertEqual(setCount(t), 0)
        XCTAssertTrue(vm.profileFeedback?.text.hasSuffix(MissionFormat.whyNoBoard) == true)
    }

    func testNotConnectedSendsNothing() async throws {
        let t = try await live("fallback")
        t.serverDrop()
        await harness.waitUntil("link down") { !self.vm.linkStatus.isConnected }
        let before = setCount(t)
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertEqual(setCount(t), before)
        XCTAssertEqual(vm.profileFeedback?.isFailure, true)
    }

    func testWaitingTextTimesOutAfterFiveSeconds() async throws {
        _ = try await live("fallback")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertEqual(vm.profileFeedback?.awaitsAnswer, true)
        harness.clock.advance(4.9)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.profileFeedback?.awaitsAnswer, true, "4.9 s is still waiting")
        harness.clock.advance(0.1)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.profileFeedback?.text, "Profile change: no answer from the ground yet. Check the active profile.")
        XCTAssertEqual(vm.profileFeedback?.isFailure, true)
        XCTAssertEqual(vm.profileFeedback?.awaitsAnswer, false)
    }

    func testTheGroundsAnswerClearsTheWaitingText() async throws {
        let t = try await live("fallback")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        XCTAssertNotNil(vm.profileFeedback)
        await harness.push(t, "detection_profiles", try fixture("detection_profiles.json", "pending")) { self.vm.profileFeedback == nil }
    }

    func testLinkLossWhileWaitingIsShown() async throws {
        let t = try await live("fallback")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        t.serverDrop()
        await harness.waitUntil("link down") { !self.vm.linkStatus.isConnected }
        XCTAssertEqual(vm.profileFeedback?.text, "Profile change: " + MissionFormat.whyLinkLost)
        XCTAssertEqual(vm.profileFeedback?.awaitsAnswer, false)
    }

    func testDismissClearsAFailure() async throws {
        _ = try await live("pending")
        await vm.selectDroneProfile("drone-1", profile: "level")
        XCTAssertNotNil(vm.profileFeedback)
        vm.dismissProfileFeedback()
        XCTAssertNil(vm.profileFeedback)
    }

    // MARK: allow-list and secrets

    func testTheEmittableCommandsEqualTheContractAllowList() throws {
        var url = URL(fileURLWithPath: "\(#filePath)").deletingLastPathComponent()
        var allowed: Set<String>?
        while url.path != "/" {
            let candidate = url.appendingPathComponent("tests/fixtures/mobile_contract/client_emits.json")
            if let data = try? Data(contentsOf: candidate) {
                let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
                let cases = try XCTUnwrap(root["cases"] as? [String: [String: Any]])
                allowed = Set(cases.filter { $0.value["allowed"] as? Bool == true }.keys)
                break
            }
            url.deleteLastPathComponent()
        }
        XCTAssertEqual(GroundViewModel.emittableEventNames, try XCTUnwrap(allowed))
        XCTAssertFalse(GroundViewModel.emittableEventNames.contains("match_alert_response"))
        XCTAssertFalse(GroundViewModel.emittableEventNames.contains("set_ceiling_settings"))
        XCTAssertFalse(GroundViewModel.emittableEventNames.contains("set_reasoning_provider"))
    }

    func testNoSecretInAnyDump() async throws {
        let t = try await live("fallback")
        await vm.selectDroneProfile("drone-1", profile: "top_down")
        var dumped = ""
        dump(vm, to: &dumped)
        dump(vm.detectionProfiles as Any, to: &dumped)
        dump(vm.profileFeedback as Any, to: &dumped)
        XCTAssertFalse(dumped.contains(GroundHarness.testKey))
        XCTAssertFalse(harness.logs.lines.joined().contains(GroundHarness.testKey))
        _ = t
    }
}
