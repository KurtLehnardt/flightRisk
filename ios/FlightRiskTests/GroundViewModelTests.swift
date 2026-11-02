import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

/// Tests for the Ground tab's view model (R-10 to R-15 and the U7 behaviour list). The view
/// model runs against the real `GroundClient` over a scripted WebSocket; the only key is
/// "test-key", and no test touches the network or the real Keychain.
@MainActor
final class GroundViewModelTests: XCTestCase {
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

    // MARK: - Starting the connection

    func testNoConnectionUntilConfiguredAndEnabled() async {
        XCTAssertEqual(vm.linkStatus, .notConfigured)
        vm.setEnabled(true)
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, 0, "enabled without an address or key must not connect")
        XCTAssertEqual(vm.linkStatus, .notConfigured)

        vm.setEnabled(false)
        harness.configure()
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, 0, "configured but switched off must not connect")
        XCTAssertEqual(vm.linkStatus, .off)

        vm.start()
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, 0, "start() must not connect while the link is off")

        vm.setEnabled(true)
        await harness.waitUntil("a transport") { self.harness.recorder.transports.count == 1 }
    }

    func testStateRequestsSentOnConnectThroughWireMessages() async {
        let t = await harness.connect()
        let expected = GroundCommand.stateRequests.map(\.eventName)
        XCTAssertEqual(t.sentEventNames, expected)
        XCTAssertEqual(t.sent.filter { $0.hasPrefix("42") }, expected.map { "42[\"\($0)\"]" })
    }

    // MARK: - Reducer wiring and stale state

    func testFleetStatusFeedsReducerAndClearsStale() async {
        let t = await harness.connect()
        XCTAssertTrue(vm.isStateStale, "state is stale after a connect until a fresh fleet_status")
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        XCTAssertFalse(vm.isStateStale)
        XCTAssertEqual(vm.reducer.cards.map(\.id), ["drone-1", "drone-2"])

        let one = vm.droneCards[0]
        XCTAssertEqual(one.card.link.text, "CONNECTED")
        XCTAssertEqual(one.card.batteryText, "87%")
        XCTAssertEqual(one.card.altitudeText, "100 cm")
        XCTAssertEqual(one.card.laneText, "1/4")
        XCTAssertFalse(one.isStale)
        XCTAssertNil(one.card.linkNotice)

        let two = vm.droneCards[1]
        XCTAssertEqual(two.card.link.text, "DOWN")
        XCTAssertEqual(two.controlChip, "HELD")
        XCTAssertEqual(two.controlText, "HELD: auto-landing in 120 s, no controller")
        XCTAssertEqual(two.card.linkNotice, "Drone link down: no telemetry from the drone for 3 s. Land still works.")
        XCTAssertEqual(two.card.altitudeText, "--", "an absent height shows a dash, never zero")
        XCTAssertNil(two.card.laneText)
        XCTAssertTrue(two.isStale, "a DOWN card shows the last values")
        XCTAssertEqual(two.staleNote, GroundText.staleValuesNote)
        XCTAssertEqual(vm.readoutText, "2 drones, 1 connected")
    }

    func testManualStatusGoesThroughReducerAndMarksPartial() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await harness.push(t, "manual_status", GroundSamples.manualHeld) { self.vm.droneCards.count == 3 }
        let three = vm.droneCards.first { $0.id == "drone-3" }
        XCTAssertEqual(three?.isPartial, true)
        XCTAssertEqual(three?.controlText, "HELD: auto-landing in 90 s, no controller")
        // The next fleet_status replaces everything, dropping the partial record.
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        XCTAssertFalse(vm.droneCards.contains { $0.isPartial })
    }

    func testHeldCountdownTicksLocallyAndFreezesWhenStale() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        harness.clock.advance(3.4)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.droneCards[1].controlText, "HELD: auto-landing in 117 s, no controller")

        t.serverDrop()
        await harness.waitUntil("stale") { self.vm.isStateStale }
        harness.clock.advance(30)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.droneCards[1].controlText, "HELD: auto-landing in 117 s, no controller (last value)",
                       "a stale countdown is frozen at its last value, never extrapolated, and says so")
        XCTAssertEqual(vm.droneCards[1].staleNote, GroundUIText.staleAfterDisconnect)
    }

    func testDisconnectKeepsCardsMarkedStale() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        t.serverDrop()
        await harness.waitUntil("reconnecting") {
            if case .reconnecting = self.vm.linkStatus { return true }
            return false
        }
        XCTAssertTrue(vm.isStateStale)
        XCTAssertEqual(vm.droneCards.count, 2)
        XCTAssertTrue(vm.droneCards.allSatisfy { $0.isStale && $0.staleNote == GroundUIText.staleAfterDisconnect })
        XCTAssertEqual(vm.readoutText, "2 drones, 1 connected (last values)")
        // A stale card never shows a live CONNECTED chip or bare values.
        let one = vm.droneCards[0]
        XCTAssertEqual(one.linkChipText, "LAST: CONNECTED")
        XCTAssertTrue(one.linkChipGreyed)
        XCTAssertEqual(one.batteryText, "87% (last value)")
        XCTAssertEqual(one.altitudeText, "100 cm (last value)")
        XCTAssertEqual(vm.droneCards[1].linkChipText, "LAST: DOWN")
        XCTAssertEqual(vm.droneCards[1].altitudeText, "--", "an absent value stays a dash")
        XCTAssertFalse(vm.canResume("drone-2"), "resume is never offered on stale state")
    }

    func testReconnectRequestsFullStateAgainAndStaysStaleUntilFleetStatus() async {
        let t1 = await harness.connect()
        await harness.push(t1, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        t1.serverDrop()
        await harness.waitUntil("reconnecting") { !self.vm.linkStatus.isConnected }
        let before = harness.recorder.transports.count
        vm.retryConnection()
        let t2 = await harness.handshake(after: before)
        XCTAssertEqual(t2.sentEventNames, GroundCommand.stateRequests.map(\.eventName))
        XCTAssertTrue(vm.isStateStale)
        await harness.push(t2, "manual_status", GroundSamples.manualHeld) { self.vm.droneCards.count == 3 }
        XCTAssertTrue(vm.isStateStale, "manual_status alone does not make the state fresh")
        await harness.push(t2, "fleet_status", GroundSamples.heldNobody) { !self.vm.isStateStale }
        XCTAssertEqual(vm.droneCards.map(\.id), ["drone-1"])
    }

    func testEventsDroppedRequestsFullStateAndMarksStale() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        vm.handle(SocketIOMini.GroundEvent.eventsDropped(count: 7))
        XCTAssertTrue(vm.isStateStale)
        XCTAssertEqual(vm.staleNote, GroundUIText.staleRefreshing)
        await harness.waitUntil("five more state requests") { t.sentEventNames.count == 10 }
        XCTAssertEqual(Array(t.sentEventNames.suffix(5)), GroundCommand.stateRequests.map(\.eventName))
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
    }

    func testUnknownAndUndecodableEventsAreIgnored() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        t.push(GroundTestFrames.event("fleet_telemetry", #"{"x":1}"#))
        t.push(GroundTestFrames.event("fleet_status", #""not an object""#))
        await harness.push(t, "warning", #"{"message":"marker"}"#) { self.vm.toasts.count == 1 }
        XCTAssertEqual(vm.droneCards.count, 2)
        XCTAssertTrue(vm.linkStatus.isConnected)
    }

    // MARK: - LAND ALL

    func testLandAllConnectedShowsHandedOverThenAckCounts() async {
        let t = await harness.connect()
        await vm.landAll()
        XCTAssertEqual(t.sentEventNames.last, "land_all")
        XCTAssertEqual(t.sent.last, #"42["land_all"]"#)
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllHandedOver)
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedAll) { self.vm.landAllFeedback?.phase == .succeeded }
        XCTAssertEqual(vm.landAllFeedback?.text, "LAND ALL reached drones (coordinator 2, transport 2)")
        harness.clock.advance(GroundViewModel.successFadeAfter + 1)
        vm.tick(now: harness.clock.now)
        XCTAssertNil(vm.landAllFeedback, "a success text may fade")
    }

    func testLandAllAckReachedNoDroneIsStickyFailure() async {
        let t = await harness.connect()
        await vm.landAll()
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedNone) { self.vm.landAllFeedback?.phase == .failed }
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllFailedNoDrone)
        harness.clock.advance(600)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllFailedNoDrone, "failures never fade")
        XCTAssertEqual(vm.landAllFeedback?.offersRetry, true)
    }

    func testLandAllNoReplyAfterTimeoutOffersSendingAgain() async {
        let t = await harness.connect()
        await vm.landAll()
        harness.clock.advance(GroundViewModel.replyTimeout - 1)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
        harness.clock.advance(1)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landAllFeedback?.phase, .noReplyYet)
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllNoReplyYet)
        XCTAssertEqual(vm.landAllFeedback?.offersRetry, true)
        XCTAssertEqual(t.sentEventNames.filter { $0 == "land_all" }.count, 1, "nothing is retried automatically")
        await vm.landAll()
        XCTAssertEqual(t.sentEventNames.filter { $0 == "land_all" }.count, 2)
    }

    func testLandAllFailedFleetEventStaysVisibleOverLaterSuccessAck() async {
        let t = await harness.connect()
        await vm.landAll()
        await harness.push(t, "fleet_event", GroundSamples.fleetEventLandAllFailed) { self.vm.landAllFeedback?.phase == .failed }
        let failedText = "LAND ALL FAILED: failed: drone-2. Land each drone by hand (per-drone Land, or the drone's own controls)."
        XCTAssertEqual(vm.landAllFeedback?.text, failedText)
        XCTAssertEqual(vm.banners.first?.text, failedText)
        XCTAssertEqual(vm.banners.first?.isFailure, true)
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedAll) { self.vm.landAllFeedback?.detail != nil }
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        XCTAssertEqual(vm.landAllFeedback?.text, failedText)
        harness.clock.advance(600)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landAllFeedback?.text, failedText)
    }

    func testLandAllWhileReconnectingSaysNotSentAndStartsReconnect() async {
        let t = await harness.connect()
        t.serverDrop()
        await harness.waitUntil("reconnecting") {
            if case .reconnecting = self.vm.linkStatus { return true }
            return false
        }
        let before = harness.recorder.transports.count
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        XCTAssertEqual(vm.landAllFeedback?.text,
                       "LAND ALL was NOT sent: this phone is not connected to the ground. Land the drones now with their own controls, or press LAND ALL on the ground laptop.")
        // The 60 s backoff is cut short by the not-sent LAND ALL.
        await harness.waitUntil("a reconnect attempt") { self.harness.recorder.transports.count > before }
        let t2 = harness.recorder.transports[before]
        XCTAssertFalse(t2.sentEventNames.contains("land_all"), "a not-sent LAND ALL is never queued")
        _ = await harness.handshake(after: before)
        await harness.settle()
        XCTAssertFalse(t2.sentEventNames.contains("land_all"), "a not-sent LAND ALL is never delivered later")
    }

    func testLandAllWhileConnectingSaysNotSent() async {
        harness.configure()
        vm.setEnabled(true)
        await harness.waitUntil("a transport") { self.harness.recorder.transports.count == 1 }
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllNotSent)
    }

    func testLandAllWithWrongKeyIsStillEnabledAndSaysNotSent() async {
        harness.configure()
        vm.setEnabled(true)
        await harness.waitUntil("a transport") { self.harness.recorder.transports.count == 1 }
        let t = harness.recorder.transports[0]
        t.push(GroundTestFrames.open)
        await harness.waitUntil("the namespace connect") { t.sent.contains { $0.hasPrefix("40") } }
        t.push(GroundTestFrames.connectError)
        await harness.waitUntil("wrong key") { self.vm.linkStatus == .wrongKey }
        XCTAssertNotEqual(vm.linkStatus.text, GroundLinkStatus.reconnecting(attempt: 1, retryInSeconds: 1, reason: "x").text)
        XCTAssertTrue(vm.linkStatus.text.hasPrefix("Wrong key"))
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllNotSentWrongKey)
        XCTAssertEqual(vm.landAllFeedback?.text,
                       "LAND ALL was NOT sent: the ground refused this phone's key. Land the drones now with their own controls, or press LAND ALL on the ground laptop.")
        await harness.waitUntil("a new attempt after the not-sent LAND ALL") { self.harness.recorder.transports.count == 2 }

        // The banner's Retry ends the wait for the user at once.
        let t2 = harness.recorder.transports[1]
        t2.push(GroundTestFrames.open)
        await harness.waitUntil("connect") { t2.sent.contains { $0.hasPrefix("40") } }
        t2.push(GroundTestFrames.connectError)
        await harness.waitUntil("wrong key again") { self.vm.linkStatus == .wrongKey }
        vm.retryConnection()
        await harness.waitUntil("retry") { self.harness.recorder.transports.count == 3 }
    }

    func testLandAllWithLinkOffOrNotConfiguredSaysNotSent() async {
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllNotSentLinkOff)
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        harness.configure()
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllNotSentLinkOff)
        XCTAssertEqual(harness.recorder.transports.count, 0, "LAND ALL does not switch a disabled link on")
    }

    func testLandAllSendFailureIsShown() async {
        let t = await harness.connect()
        t.failSends()
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllSendFailed)
    }

    func testLandAllSentWithNoDrones() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", #"{"count":0,"drones":{}}"#) { !self.vm.isStateStale }
        XCTAssertTrue(vm.droneCards.isEmpty)
        await vm.landAll()
        XCTAssertEqual(t.sentEventNames.last, "land_all")
    }

    // MARK: - Per-drone Land

    func testLandDroneWorksWhenDroneLinkIsDown() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        XCTAssertNotNil(vm.droneCards[1].card.linkNotice)
        await vm.landDrone("drone-2")
        XCTAssertEqual(t.sent.last, #"42["drone_command",{"command":"land","drone_id":"drone-2"}]"#)
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .handedOver)
        await harness.push(t, "manual_ack", #"{"action":"land","detail":null,"drone_id":"drone-2","ok":true,"reason":null}"#) {
            self.vm.landFeedback["drone-2"]?.phase == .accepted
        }
        await harness.push(t, "manual_result", #"{"action":"land","detail":null,"drone_id":"drone-2","ok":true,"reason":null}"#) {
            self.vm.landFeedback["drone-2"]?.phase == .succeeded
        }
        XCTAssertEqual(vm.droneCards[1].landFeedback?.text, "drone-2: Land acknowledged by the drone's edge.")
    }

    func testLandDroneNoReplyFromEdgeIsAFailure() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-2")
        await harness.push(t, "manual_result", #"{"action":"land","detail":"no answer from drone-2 within 4 s","drone_id":"drone-2","ok":false,"reason":"no_reply"}"#) {
            self.vm.landFeedback["drone-2"]?.phase == .failed
        }
        XCTAssertEqual(vm.landFeedback["drone-2"]?.text, "drone-2: no answer from drone-2 within 4 s. Land the drone with its own controls.")
    }

    func testLandDroneWithoutReplyShowsNoReplyYet() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-1")
        harness.clock.advance(GroundViewModel.replyTimeout)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landFeedback["drone-1"]?.text, "drone-1: Land: no reply from the ground yet. Check the connection.")
        XCTAssertEqual(t.sentEventNames.filter { $0 == "drone_command" }.count, 1)
    }

    func testLandDroneWhileDisconnectedSaysNotSent() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        t.serverDrop()
        await harness.waitUntil("stale") { self.vm.isStateStale }
        await vm.landDrone("drone-1")
        XCTAssertEqual(vm.landFeedback["drone-1"]?.phase, .failed)
        XCTAssertEqual(vm.landFeedback["drone-1"]?.text, GroundUIText.droneNotSent("drone-1", action: "Land", linkOff: false))
    }

    func testEmptyDroneIdIsRefusedWithoutSending() async {
        let t = await harness.connect()
        let before = t.sent.count
        await vm.landDrone("")
        await vm.landDrone("   ")
        await vm.resumeHeldDrone("")
        await harness.settle()
        XCTAssertEqual(t.sent.count, before)
        let landText = "Land was not sent: this drone has no usable id. Land it with its own controls."
        XCTAssertEqual(vm.landFeedback[""]?.text, landText)
        XCTAssertEqual(vm.landFeedback["   "]?.text, landText)
        XCTAssertEqual(vm.landFeedback[""]?.phase, .failed)
        XCTAssertEqual(vm.resumeFeedback[""]?.text, "Resume search was not sent: this drone has no usable id.")
    }

    func testLegacyGroundCommandAckAnswersTheLand() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", #"{"count":1,"drones":{"tello":{"battery":80,"height":0,"is_flying":true}}}"#) {
            self.vm.droneCards.count == 1
        }
        XCTAssertEqual(vm.droneCards[0].card.link.text, "--")
        XCTAssertEqual(vm.readoutText, "1 drone, 0 connected (no fleet: local drone only)")
        await vm.landDrone("tello")
        await harness.push(t, "command_ack", #"{"command":"land"}"#) { self.vm.landFeedback["tello"]?.phase == .accepted }
        XCTAssertTrue(vm.landFeedback["tello"]?.text.hasPrefix(GroundUIText.legacyLandAccepted("tello")) == true)
        harness.clock.advance(60)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.landFeedback["tello"]?.phase, .accepted, "a ground without a fleet sends no final answer; no timeout")
    }

    // MARK: - Resume search

    func testResumeOfferedOnlyForHeldDroneNobodyControls() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.heldNobody) { !self.vm.isStateStale }
        XCTAssertTrue(vm.canResume("drone-1"))
        XCTAssertEqual(vm.droneCards[0].canResume, true)
        await vm.resumeHeldDrone("drone-1")
        XCTAssertEqual(t.sent.last, #"42["manual_resume_search",{"drone_id":"drone-1"}]"#)
        await harness.push(t, "manual_ack", #"{"action":"resume_search","detail":"drone-1 is not under manual control","drone_id":"drone-1","ok":false,"reason":"not_manual"}"#) {
            self.vm.resumeFeedback["drone-1"]?.phase == .failed
        }

        await harness.push(t, "fleet_status", GroundSamples.heldButControlled) { self.vm.droneCards.count == 2 }
        XCTAssertFalse(vm.canResume("drone-1"), "someone controls a drone: no resume")
        XCTAssertFalse(vm.canResume("drone-2"))
        let sentBefore = t.sent.count
        await vm.resumeHeldDrone("drone-1")
        await harness.settle()
        XCTAssertEqual(t.sent.count, sentBefore, "resume is not sent when it is not offered")

        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 && self.vm.droneCards[0].id == "drone-1" && self.vm.droneCards[0].controlChip == nil }
        XCTAssertFalse(vm.canResume("drone-1"), "an autonomous drone gets no resume")
    }

    // MARK: - Allow-list

    func testEmittableCommandSetIsTheAllowList() {
        XCTAssertEqual(GroundViewModel.emittableEventNames, GroundEmitPolicy.contractEvents)
        let forbidden = [
            "plan_fleet", "start_fleet", "start_search", "stop_search", "manual_take_control", "manual_command",
            "set_ceiling_settings", "set_reasoning_provider", "set_stream_video", "set_target", "register_drone",
            "deregister_drone", "remove_all_drones", "restart_dashboard", "match_alert_response",
        ]
        for name in forbidden {
            XCTAssertFalse(GroundViewModel.emitPolicy.allows(name), name)
            XCTAssertFalse(GroundViewModel.emittableEventNames.contains(name), name)
        }
        XCTAssertTrue(GroundViewModel.emitPolicy.allows("land_all"))
    }

    func testEveryActionEmitsOnlyAllowedEvents() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.heldNobody) { !self.vm.isStateStale }
        await vm.landAll()
        await vm.landDrone("drone-1")
        await vm.resumeHeldDrone("drone-1")
        await vm.refresh()
        let names = Set(t.sentEventNames)
        XCTAssertTrue(names.isSubset(of: GroundCommand.allowedEventNames), "\(names)")
        XCTAssertEqual(names, Set(["land_all", "drone_command", "manual_resume_search"]).union(GroundCommand.stateRequests.map(\.eventName)))
        // Every drone_command is a land.
        let droneCommands = t.sent.filter { $0.hasPrefix(#"42["drone_command""#) }
        XCTAssertTrue(droneCommands.allSatisfy { $0.contains(#""command":"land""#) })
    }

    // MARK: - Banners and toasts

    func testFleetEventsAndRefusalsAreBannersAndErrorsAreToasts() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_event", GroundSamples.fleetEventPlanRefused) { self.vm.banners.count == 2 }
        XCTAssertEqual(vm.banners.map(\.text), ["Plan refused: drone-2: not connected.", "PLAN REFUSED"])
        await harness.push(t, "fleet_event", #"{"event":"something_new","detail":"x"}"#) { self.vm.banners.count == 3 }
        XCTAssertEqual(vm.banners.first?.text, "something_new", "an unknown kind is shown by name")
        await harness.push(t, "error", #"{"message":"LAND ALL reached no drone. Land every drone with its local control now."}"#) {
            self.vm.toasts.count == 1
        }
        await harness.push(t, "warning", #"{"message":"heads up"}"#) { self.vm.toasts.count == 2 }
        harness.clock.advance(GroundViewModel.warningFadeAfter + 1)
        vm.tick(now: harness.clock.now)
        XCTAssertEqual(vm.toasts.map(\.text), ["LAND ALL reached no drone. Land every drone with its local control now."],
                       "warnings fade, errors stay")
        XCTAssertTrue(vm.linkStatus.isConnected, "an error is never a disconnect")
    }

    // MARK: - Lifecycle

    func testBackgroundClosesTheLinkAndForegroundReconnects() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        let client = harness.clients[0]
        vm.appDidEnterBackground()
        XCTAssertEqual(vm.linkStatus, .paused)
        XCTAssertTrue(vm.isStateStale)
        await harness.waitUntil("the client stopped") { t.closed }
        let stopped = await client.state
        XCTAssertEqual(stopped, .stopped)
        XCTAssertTrue(t.sent.contains("41"), "the client leaves the namespace gracefully")
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllNotSentLinkOff)

        let before = harness.recorder.transports.count
        vm.appWillEnterForeground()
        let t2 = await harness.handshake(after: before)
        XCTAssertTrue(vm.isStateStale)
        await harness.push(t2, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
    }

    func testReleasingTheViewModelShutsTheClientDown() async {
        var model: GroundViewModel? = harness.makeViewModel()
        XCTAssertNil(model?.saveAddress(GroundHarness.address))
        XCTAssertNil(model?.saveKey(GroundHarness.testKey))
        model?.setEnabled(true)
        await harness.waitUntil("a transport") { self.harness.recorder.transports.count == 1 }
        let client = harness.clients[0]
        weak var weakModel = model
        model = nil
        XCTAssertNil(weakModel, "nothing keeps the view model alive")
        await harness.waitUntil("shutdown") { harness.recorder.transports[0].closed }
        await harness.settle()
        let state = await client.state
        XCTAssertEqual(state, .stopped)
    }

    func testDisablingClosesTheLink() async {
        let t = await harness.connect()
        vm.setEnabled(false)
        XCTAssertEqual(vm.linkStatus, .off)
        await harness.waitUntil("closed") { t.closed }
        XCTAssertFalse(harness.defaults.bool(forKey: GroundConnectionSettings.enabledKey))
    }
}
