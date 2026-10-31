import Foundation
import GroundProtocol
import Security
import SocketIOMini
import XCTest
@testable import FlightRisk

/// Tests for the review round of the Ground tab (PR #156): command answers that never come,
/// stale display, failures the ground reports for a drone, LAND ALL ack attribution and
/// partial acks, bounded memory, the locked Keychain, toast eviction, the state re-read race
/// and resume for a DOWN drone. The only key is "test-key".
@MainActor
final class GroundReviewRoundTests: XCTestCase {
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

    private func tick(_ seconds: TimeInterval) {
        harness.clock.advance(seconds)
        vm.tick(now: harness.clock.now)
    }

    // MARK: - B1: accepted commands without a final answer, and link loss

    func testAcceptedLandWithoutFinalAnswerBecomesAFailure() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-2")
        await harness.push(t, "manual_ack", #"{"action":"land","detail":null,"drone_id":"drone-2","ok":true,"reason":null}"#) {
            self.vm.landFeedback["drone-2"]?.phase == .accepted
        }
        tick(GroundViewModel.replyTimeout + GroundViewModel.manualReplyTimeout - 0.5)
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .accepted)
        tick(0.5)
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .failed)
        XCTAssertEqual(vm.landFeedback["drone-2"]?.text, "drone-2: no final answer from the drone's edge. Land it with its own controls.")
        XCTAssertEqual(vm.landFeedback["drone-2"]?.offersRetry, true)
        tick(600)
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .failed, "the failure is sticky")
        XCTAssertEqual(t.sentEventNames.filter { $0 == "drone_command" }.count, 1, "nothing is resent")
    }

    func testLinkLossTurnsWaitingCommandsIntoFailures() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-1")
        await vm.landDrone("drone-2")
        await harness.push(t, "manual_ack", #"{"action":"land","detail":null,"drone_id":"drone-2","ok":true,"reason":null}"#) {
            self.vm.landFeedback["drone-2"]?.phase == .accepted
        }
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)

        t.serverDrop()
        await harness.waitUntil("reconnecting") { !self.vm.linkStatus.isConnected }
        XCTAssertEqual(vm.landFeedback["drone-1"]?.text, "drone-1: connection lost before the answer; land it with its own controls.")
        XCTAssertEqual(vm.landFeedback["drone-2"]?.text, "drone-2: connection lost before the answer; land it with its own controls.")
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .failed)
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllConnectionLost)

        let before = harness.recorder.transports.count
        vm.retryConnection()
        let t2 = await harness.handshake(after: before)
        await harness.push(t2, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        XCTAssertEqual(vm.landFeedback["drone-2"]?.phase, .failed, "the failure stays after the reconnect")
        XCTAssertFalse(t2.sentEventNames.contains("drone_command"), "nothing is resent after the reconnect")
    }

    // MARK: - B3: failures for actions the phone did not send

    func testFailedAutoLandIsShownOnTheCard() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.heldNobody) { !self.vm.isStateStale }
        await harness.push(t, "manual_result", #"{"action":"auto_land","detail":"send failed","drone_id":"drone-1","ok":false,"reason":"not_delivered"}"#) {
            self.vm.droneCards.first?.alert != nil
        }
        XCTAssertEqual(vm.droneCards[0].alert?.text, "drone-1: auto_land failed (send failed). Land it with its own controls.")
        XCTAssertTrue(vm.banners.isEmpty, "a drone with a card shows it on the card")
        tick(600)
        XCTAssertNotNil(vm.droneCards[0].alert, "sticky until dismissed")

        await harness.push(t, "manual_result", #"{"action":"auto_land","detail":null,"drone_id":"drone-1","ok":false,"reason":"no_reply"}"#) {
            self.vm.droneCards.first?.alert?.text.contains("no reply from the edge") == true
        }
        XCTAssertEqual(vm.droneCards[0].alert?.text, "drone-1: auto_land failed (no reply from the edge). Land it with its own controls.")
        vm.dismissDroneAlert("drone-1")
        XCTAssertNil(vm.droneCards[0].alert)
    }

    func testManualReplyFixturesAreAllShown() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        // A land the phone did not send that the edge never answered.
        let noReply = try ContractFixtures.payloadJSON("manual_result.json", "land_no_reply")
        await harness.push(t, "manual_result", noReply) { self.vm.droneAlerts["drone-2"] != nil }
        XCTAssertEqual(vm.droneAlerts["drone-2"]?.text, "drone-2: land failed (no answer from drone-2 within 4 s). Land it with its own controls.")
        // Successful results add no alert.
        for name in ["land_acked", "resume_search_acked"] {
            let ok = try ContractFixtures.payloadJSON("manual_result.json", name)
            t.push(GroundTestFrames.event("manual_result", ok))
        }
        // A refusal for a drone without a card becomes a failure banner.
        let unknown = try ContractFixtures.payloadJSON("manual_ack.json", "land_unknown_drone")
        await harness.push(t, "manual_ack", unknown) { self.vm.banners.count == 1 }
        XCTAssertEqual(vm.banners.first?.text, "drone-9: land failed ('drone-9' is not in the fleet). Land it with its own controls.")
        XCTAssertEqual(vm.banners.first?.isFailure, true)
        XCTAssertEqual(Set(vm.droneAlerts.keys), ["drone-2", "drone-9"])
    }

    // MARK: - N1: command_ack on a ground without a fleet

    func testLegacyCommandAcksAnswerTheOldestLand() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", #"{"count":2,"drones":{"a":{"battery":80},"b":{"battery":70}}}"#) {
            self.vm.droneCards.count == 2
        }
        await vm.landDrone("a")
        await vm.landDrone("b")
        await harness.push(t, "command_ack", #"{"command":"land"}"#) { self.vm.landFeedback["a"]?.phase == .accepted }
        XCTAssertEqual(vm.landFeedback["a"]?.text, "a: Land accepted by the ground (no confirmation from the drone).")
        XCTAssertEqual(vm.landFeedback["b"]?.phase, .handedOver, "the second land is still unanswered")
    }

    // MARK: - N4 and N5: LAND ALL acks

    func testLandAllAcksAreAttributedInOrder() async {
        let t = await harness.connect()
        await vm.landAll()
        tick(2)
        await vm.landAll()
        XCTAssertEqual(t.sentEventNames.filter { $0 == "land_all" }.count, 2)
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedAll) { self.vm.landAllFeedback?.detail != nil }
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver, "the first ack answers the first tap, not the second")
        XCTAssertEqual(vm.landAllFeedback?.detail, "Earlier LAND ALL: LAND ALL reached drones (coordinator 2, transport 2)")
        tick(GroundViewModel.replyTimeout)
        XCTAssertEqual(vm.landAllFeedback?.phase, .noReplyYet, "the second tap still shows its own missing answer")
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedNone) { self.vm.landAllFeedback?.phase == .failed }
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllFailedNoDrone)
    }

    func testEarlierFailedAckIsAStickyBannerWhileTheLatestTapWaits() async {
        let t = await harness.connect()
        await vm.landAll()
        await vm.landAll()
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedNone) { self.vm.banners.count == 1 }
        XCTAssertEqual(vm.banners.first?.text, GroundText.landAllFailedNoDrone)
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
    }

    func testPartialLandAllAckIsANonFadingWarning() async {
        let t = await harness.connect()
        await vm.landAll()
        await harness.push(t, "land_all_ack", GroundSamples.ackPartial) { self.vm.landAllFeedback?.phase == .partial }
        XCTAssertEqual(vm.landAllFeedback?.text,
                       "LAND ALL reached only some drones. Reached: coordinator 2. Reached none: transport. Land the rest by hand.")
        XCTAssertEqual(vm.landAllFeedback?.offersRetry, true)
        tick(600)
        XCTAssertEqual(vm.landAllFeedback?.phase, .partial, "a partial result never fades")
    }

    // MARK: - N6: bounded memory

    func testFramesAreCappedAndDecodedFromTheFrameStream() async {
        let t = await harness.connect()
        for index in 0..<20 {
            t.push(GroundTestFrames.event("frame", #"{"drone_id":"d-\#(index)","image":null,"boxes":[],"fps":10}"#))
        }
        await harness.waitUntil("the last frame") { self.vm.latestFrames["d-19"] != nil }
        XCTAssertLessThanOrEqual(vm.latestFrames.count, GroundViewModel.maxFrameTiles)
        XCTAssertEqual(vm.latestFrames.count, vm.latestFrameAt.count)
        XCTAssertEqual(vm.latestFrames["d-19"]?.fps, 10)
    }

    func testMatchAlertsAreCapped() {
        for index in 0..<(GroundViewModel.maxMatchAlerts + 5) {
            let json = #"{"alert_id":"a-\#(index)","score":0.9}"#
            vm.handle(GroundProtocol.GroundEvent.decode(name: "match_alert", payload: Data(json.utf8)))
        }
        XCTAssertEqual(vm.matchAlerts.count, GroundViewModel.maxMatchAlerts)
        XCTAssertEqual(vm.matchAlerts.last?.alertId, "a-104")
        XCTAssertEqual(vm.matchAlerts.first?.alertId, "a-5")
    }

    // MARK: - N7: locked Keychain

    func testLockedKeychainIsNotReportedAsNoKey() async {
        harness.configure()
        harness.keyStore.isLocked = true
        vm.setEnabled(true)
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, 0)
        XCTAssertEqual(vm.keyState, .unavailable)
        XCTAssertEqual(vm.linkStatus, .keychainLocked)
        XCTAssertEqual(vm.linkStatus.text, "Keychain locked: unlock the phone to connect to the ground station.")
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.text, GroundUIText.landAllNotSentKeychainLocked)
        let probe = await vm.testConnection()
        XCTAssertEqual(probe, "Keychain locked: unlock the phone and try again.")

        harness.keyStore.isLocked = false
        vm.appDidEnterBackground()
        vm.appWillEnterForeground()
        XCTAssertEqual(vm.keyState, .saved, "the key state is read again on return to the foreground")
        await harness.waitUntil("a transport after unlocking") { self.harness.recorder.transports.count == 1 }
    }

    func testKeychainStatusMapping() {
        XCTAssertEqual(KeychainGroundKeyStore.lookup(status: errSecItemNotFound, data: nil), .notFound)
        XCTAssertEqual(KeychainGroundKeyStore.lookup(status: errSecInteractionNotAllowed, data: nil), .unavailable)
        XCTAssertEqual(KeychainGroundKeyStore.lookup(status: errSecAuthFailed, data: nil), .unavailable)
        XCTAssertEqual(KeychainGroundKeyStore.lookup(status: errSecSuccess, data: Data("test-key".utf8)), .found(GroundAPIKey("test-key")))
        XCTAssertEqual(KeychainGroundKeyStore.lookup(status: errSecSuccess, data: Data()), .notFound)
        XCTAssertEqual(KeychainGroundKeyStore.service, "com.flightrisk.app")
        XCTAssertEqual(KeychainGroundKeyStore.account, "flightrisk_api_key_ground")
    }

    // MARK: - N8: toasts

    func testWarningsDoNotEvictAnError() {
        vm.handle(GroundProtocol.GroundEvent.decode(name: "error", payload: Data(#"{"message":"the error"}"#.utf8)))
        for index in 0..<6 {
            vm.handle(GroundProtocol.GroundEvent.decode(name: "warning", payload: Data(#"{"message":"w\#(index)"}"#.utf8)))
        }
        XCTAssertEqual(vm.toasts.count, GroundViewModel.maxToasts)
        XCTAssertTrue(vm.toasts.contains { $0.isError && $0.text == "the error" })
        XCTAssertEqual(vm.toasts.first?.text, "w5")
    }

    // MARK: - N9: state re-read after a gap

    func testReReadAfterGapDoesNotOverwriteANewerState() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        vm.handle(SocketIOMini.GroundEvent.eventsDropped(count: 3))
        // A newer state arrives before the re-read finishes; the client itself still says connected.
        vm.handle(SocketIOMini.GroundEvent.stateChanged(.reconnecting(attempt: 1, retryIn: .seconds(5), reason: .pingTimeout)))
        await harness.waitUntil("the re-requests") { t.sentEventNames.count >= 10 }
        await harness.settle()
        XCTAssertFalse(vm.linkStatus.isConnected, "the late re-read must not show LIVE over a newer reconnecting state")
    }

    // MARK: - N10: resume for a DOWN drone

    func testResumeIsNotOfferedForADownDrone() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.heldOneDown) { !self.vm.isStateStale }
        XCTAssertTrue(vm.canResume("drone-1"))
        XCTAssertFalse(vm.canResume("drone-2"), "the ground refuses a resume for a drone whose link is down")
        XCTAssertEqual(vm.droneCards.map(\.canResume), [true, false])
    }

    // MARK: - N11: small display rules

    func testPillTextsAndFeedbackAge() async {
        XCTAssertEqual(GroundLinkStatus.connecting(attempt: 1).pillText, "CONNECTING")
        XCTAssertEqual(GroundLinkStatus.reconnecting(attempt: 1, retryInSeconds: 1, reason: "x").pillText, "RECONNECTING")

        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-1")
        await harness.push(t, "manual_result", #"{"action":"land","detail":null,"drone_id":"drone-1","ok":true,"reason":null}"#) {
            self.vm.landFeedback["drone-1"]?.phase == .succeeded
        }
        tick(12)
        XCTAssertEqual(vm.droneCards[0].landFeedbackText, "drone-1: Land acknowledged by the drone's edge. (12 s ago)")
        tick(GroundViewModel.droneSuccessFadeAfter)
        XCTAssertNil(vm.landFeedback["drone-1"], "a per-drone success fades")
    }

    func testTickDoesNotRewriteUnchangedState() {
        vm.handle(GroundProtocol.GroundEvent.decode(name: "error", payload: Data(#"{"message":"stays"}"#.utf8)))
        let before = vm.toasts
        tick(30)
        XCTAssertEqual(vm.toasts, before)
    }
}
