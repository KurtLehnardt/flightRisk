import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

/// Tests for the hardening of the Ground tab: long text from the ground, the layout budget of
/// the top bar, the 30 s freshness timeout and the 40 s expiry of unanswered LAND ALL taps.
@MainActor
final class GroundHardeningTests: XCTestCase {
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

    /// A JSON string value of `count` characters.
    private func filler(_ count: Int, _ character: Character = "x") -> String {
        String(repeating: character, count: count)
    }

    private func deliver(_ name: String, _ json: String) {
        vm.handle(GroundProtocol.GroundEvent.decode(name: name, payload: Data(json.utf8)))
    }

    // MARK: - Shortening

    func testShortenCutsAtTheCapWithAnEllipsis() {
        let cut = GroundText.shorten(filler(1_200))
        XCTAssertEqual(cut.count, 300)
        XCTAssertTrue(cut.hasSuffix("\u{2026}"))
        XCTAssertEqual(GroundText.shorten(filler(300)), filler(300), "text at the cap is not cut")
        XCTAssertEqual(GroundText.shorten("short"), "short")
        XCTAssertEqual(GroundText.shorten(nil), "")
        let id = GroundText.shortId(filler(500))
        XCTAssertEqual(id.count, 40)
        XCTAssertTrue(id.hasSuffix("\u{2026}"))
    }

    func testShortenReplacesControlAndBidiCharacters() {
        XCTAssertEqual(GroundText.shorten("a\nb\u{0}c\u{202E}d"), "a b c d")
    }

    func testShortenOfThreeMegabytesIsFast() {
        let huge = filler(3_000_000)
        let started = Date()
        let cut = GroundText.shorten(huge)
        XCTAssertEqual(cut.count, 300)
        XCTAssertLessThan(Date().timeIntervalSince(started), 0.5)
    }

    // MARK: - Every string from the ground is capped

    /// The longest text the screen may show for one piece of ground text: the cap plus the
    /// fixed words the phone adds around it.
    private static let displayedLimit = 300 + 160

    private func displayedTexts() -> [String] {
        var texts = vm.banners.map(\.text) + vm.toasts.map(\.text)
        texts += vm.droneCards.flatMap { [$0.displayId, $0.card.linkNotice, $0.alertText, $0.landFeedbackText, $0.resumeFeedbackText].compactMap { $0 } }
        texts += [vm.landAllFeedback?.text, vm.landAllFeedback?.detail].compactMap { $0 }
        return texts
    }

    private func assertCapped(_ what: String, file: StaticString = #filePath, line: UInt = #line) {
        for text in displayedTexts() {
            XCTAssertLessThanOrEqual(text.count, Self.displayedLimit, "\(what): a displayed text has \(text.count) characters", file: file, line: line)
        }
    }

    func testLongGroundTextIsCappedEverywhere() {
        for size in [1_200, 3_000_000] {
            let started = Date()
            let big = filler(size)
            let bigId = filler(size, "d")
            deliver("fleet_status", #"{"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"drone_link_ok":false,"drone_link_detail":"\#(big)"}},"manual_controlled":null}"#)
            for kind in ["retask_failed", "retask", "profile_switch_pending", "plan_refused", "start_refused"] {
                deliver("fleet_event", #"{"event":"\#(kind)","detail":"\#(big)","drone_id":"\#(bigId)"}"#)
            }
            deliver("fleet_event", #"{"event":"\#(big)","detail":"\#(big)"}"#)
            deliver("fleet_event", #"{"event":"land_all","detail":"failed: \#(big)"}"#)
            deliver("manual_ack", #"{"action":"land","drone_id":"drone-1","ok":false,"reason":"\#(big)","detail":"\#(big)"}"#)
            deliver("manual_result", #"{"action":"\#(big)","drone_id":"\#(bigId)","ok":false,"reason":"\#(big)","detail":"\#(big)"}"#)
            deliver("manual_result", #"{"action":"auto_land","drone_id":"drone-1","ok":false,"reason":"\#(big)","detail":null}"#)
            deliver("error", #"{"message":"\#(big)"}"#)
            deliver("warning", #"{"message":"\#(big)"}"#)

            XCTAssertFalse(vm.banners.isEmpty)
            XCTAssertGreaterThanOrEqual(vm.toasts.count, 2)
            XCTAssertNotNil(vm.landAllFeedback, "the failed LAND ALL event shows in the bar")
            XCTAssertFalse(vm.droneAlerts.isEmpty)
            XCTAssertNotNil(vm.droneCards.first?.card.linkNotice)
            assertCapped("\(size) characters")
            XCTAssertLessThan(Date().timeIntervalSince(started), 10, "no hang")
        }
    }

    func testOwnLandRefusalWithLongDetailIsCapped() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        await vm.landDrone("drone-1")
        deliver("manual_ack", #"{"action":"land","drone_id":"drone-1","ok":false,"reason":"busy","detail":"\#(filler(1_200))"}"#)
        let text = vm.landFeedback["drone-1"]?.text ?? ""
        XCTAssertEqual(vm.landFeedback["drone-1"]?.phase, .failed)
        XCTAssertTrue(text.contains("\u{2026}"))
        assertCapped("own land refusal")
    }

    func testLongDroneIdsAreShownCutAndStillSent() async {
        let id = "drone-" + filler(200, "z")
        deliver("fleet_status", #"{"drones":{"\#(id)":{"drone_id":"\#(id)","connected":true}},"manual_controlled":null}"#)
        let card = vm.droneCards[0]
        XCTAssertEqual(card.id, id, "the real id is kept for commands")
        XCTAssertEqual(card.displayId.count, 40)
        let t = await harness.connect()
        await harness.push(t, "fleet_status", #"{"drones":{"\#(id)":{"drone_id":"\#(id)","connected":true}},"manual_controlled":null}"#) { !self.vm.isStateStale }
        await vm.landDrone(id)
        XCTAssertTrue(t.sent.contains { $0.contains(id) }, "the command carries the full id")
        XCTAssertLessThanOrEqual(vm.landFeedback[id]?.text.count ?? 0, 120)
        assertCapped("long id")
    }

    func testNormalTextIsUnchanged() {
        deliver("fleet_event", #"{"detail":"drone-2: not connected.","event":"plan_refused"}"#)
        XCTAssertEqual(vm.banners.first?.text, "Plan refused: drone-2: not connected.")
    }

    // MARK: - Layout budget

    func testLayoutPortrait() {
        // iPhone 17 portrait, about 874 points: 35% of the height, list far above its minimum.
        let height: CGFloat = 874
        XCTAssertEqual(GroundLayout.feedbackMaxHeight(available: height), height * 0.35, accuracy: 0.001)
        XCTAssertGreaterThanOrEqual(GroundLayout.listHeight(available: height), GroundLayout.minListHeight)
        // A small phone: the share (245) would leave 700 - 160 - 245 = 295, still above the minimum.
        XCTAssertGreaterThanOrEqual(GroundLayout.listHeight(available: 667), GroundLayout.minListHeight)
    }

    func testLayoutLandscape() {
        // iPhone landscape, about 402 points: the share (140) would leave 102 for the list, so
        // the text area shrinks to what lets the list keep its 180.
        let height: CGFloat = 402
        let feedback = GroundLayout.feedbackMaxHeight(available: height)
        XCTAssertEqual(feedback, height - GroundLayout.fixedBarHeight - GroundLayout.minListHeight, accuracy: 0.001)
        XCTAssertLessThan(feedback, height * 0.35)
        XCTAssertEqual(GroundLayout.listHeight(available: height), GroundLayout.minListHeight, accuracy: 0.001)
    }

    func testLayoutTinyAndInvalidHeightsKeepTheMinimumTextArea() {
        XCTAssertEqual(GroundLayout.feedbackMaxHeight(available: 320), GroundLayout.minFeedbackHeight)
        XCTAssertEqual(GroundLayout.feedbackMaxHeight(available: 0), GroundLayout.minFeedbackHeight)
        XCTAssertEqual(GroundLayout.feedbackMaxHeight(available: .nan), GroundLayout.minFeedbackHeight)
        XCTAssertEqual(GroundLayout.feedbackMaxHeight(available: -5), GroundLayout.minFeedbackHeight)
    }

    func testLayoutNeverExceedsTheShareAndKeepsTheListWhereTheScreenAllows() {
        for height in stride(from: 300 as CGFloat, through: 1_400, by: 10) {
            let feedback = GroundLayout.feedbackMaxHeight(available: height)
            XCTAssertGreaterThanOrEqual(feedback, GroundLayout.minFeedbackHeight)
            XCTAssertLessThanOrEqual(feedback, max(GroundLayout.minFeedbackHeight, height * GroundLayout.feedbackShare) + 0.001)
            if height >= GroundLayout.fixedBarHeight + GroundLayout.minListHeight + GroundLayout.minFeedbackHeight {
                XCTAssertGreaterThanOrEqual(GroundLayout.listHeight(available: height), GroundLayout.minListHeight - 0.001, "height \(height)")
            }
        }
    }

    // MARK: - Freshness

    func testNoFleetStatusFor30SecondsMarksTheStateStale() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale && self.vm.droneCards.count == 2 }
        tick(29)
        XCTAssertFalse(vm.isStateStale)
        tick(1)
        XCTAssertTrue(vm.isStateStale)
        XCTAssertTrue(vm.hasNoRecentUpdate)
        XCTAssertEqual(vm.staleNote, "No recent update from the ground: no fleet status for 30 s.")
        XCTAssertTrue(vm.linkStatus.isConnected, "the link itself is fine")
        let card = vm.droneCards[0]
        XCTAssertEqual(card.linkChipText, "LAST: CONNECTED")
        XCTAssertTrue(card.batteryText.hasSuffix("(last value)"))
        XCTAssertTrue(vm.readoutText.hasSuffix("(last values)"))

        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale }
        XCTAssertFalse(vm.hasNoRecentUpdate)
        XCTAssertNil(vm.staleNote)
        XCTAssertEqual(vm.droneCards[0].linkChipText, "CONNECTED")
        // The timer starts again from the new fleet_status.
        tick(29)
        XCTAssertFalse(vm.isStateStale)
        tick(1)
        XCTAssertTrue(vm.isStateStale)
    }

    func testAFleetStatusInsideTheWindowRestartsTheTimer() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        tick(20)
        deliver("fleet_status", GroundSamples.fleetTwo)
        tick(20)
        XCTAssertFalse(vm.isStateStale, "40 s after the first, 20 s after the second")
        tick(10)
        XCTAssertTrue(vm.isStateStale)
    }

    func testNoFreshnessTimeoutWithoutAConnectionOrAFleetStatus() async {
        tick(120)
        XCTAssertFalse(vm.hasNoRecentUpdate, "never connected")
        _ = await harness.connect()
        tick(120)
        XCTAssertFalse(vm.hasNoRecentUpdate, "connected but no fleet_status yet: the refreshing note already applies")
    }

    func testALinkDropReplacesTheNoRecentUpdateNote() async {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { !self.vm.isStateStale && self.vm.droneCards.count == 2 }
        tick(30)
        XCTAssertTrue(vm.hasNoRecentUpdate)
        t.serverDrop()
        await harness.waitUntil("reconnecting") { !self.vm.linkStatus.isConnected }
        XCTAssertFalse(vm.hasNoRecentUpdate)
        XCTAssertEqual(vm.staleNote, GroundUIText.staleAfterDisconnect)
    }

    // MARK: - LAND ALL expiry

    func testUnansweredLandAllExpiresAfter40Seconds() async {
        let t = await harness.connect()
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
        tick(GroundViewModel.replyTimeout)
        XCTAssertEqual(vm.landAllFeedback?.phase, .noReplyYet)
        tick(34)
        XCTAssertEqual(vm.landAllFeedback?.phase, .noReplyYet, "39 s")
        tick(1)
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed)
        XCTAssertTrue(vm.landAllFeedback?.text.contains("the ground never answered; land the drones with their own controls") == true)
        XCTAssertTrue(vm.landAllFeedback?.isFailure == true)
        XCTAssertTrue(vm.landAllFeedback?.offersRetry == true)
        tick(600)
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed, "the failure is sticky")
        XCTAssertEqual(t.sentEventNames.filter { $0 == "land_all" }.count, 1, "never resent automatically")
    }

    func testAnAckAfterTheExpiryIsCreditedToTheNewerTap() async {
        let t = await harness.connect()
        await vm.landAll()  // Tap 1.
        tick(25)
        await vm.landAll()  // Tap 2.
        let second = vm.landAllFeedback?.attempt
        tick(15)  // Tap 1 is 40 s old, tap 2 is 15 s old.
        XCTAssertEqual(vm.landAllFeedback?.attempt, second)
        XCTAssertEqual(vm.landAllFeedback?.phase, .noReplyYet, "tap 2 still waits")
        XCTAssertTrue(vm.banners.contains { $0.text == GroundUIText.earlierLandAllNoAnswer && $0.isFailure })

        await harness.push(t, "land_all_ack", GroundSamples.ackReachedAll) { self.vm.landAllFeedback?.phase == .succeeded }
        XCTAssertEqual(vm.landAllFeedback?.attempt, second, "the ack answers tap 2, not the expired tap 1")
        XCTAssertEqual(t.sentEventNames.filter { $0 == "land_all" }.count, 2, "nothing was resent")
    }

    func testAnswerInTimeDoesNotExpire() async {
        let t = await harness.connect()
        await vm.landAll()
        tick(30)
        await harness.push(t, "land_all_ack", GroundSamples.ackReachedAll) { self.vm.landAllFeedback?.phase == .succeeded }
        tick(100)
        XCTAssertTrue(vm.banners.isEmpty)
    }
}
