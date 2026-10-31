import Foundation
import GroundProtocol
import SocketIOMini
import XCTest
@testable import FlightRisk

private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try JSONDecoder().decode(T.self, from: Data(json.utf8))
}

private func reasoning(_ name: String) throws -> ReasoningProviderPayload {
    try decode(ReasoningProviderPayload.self, try ContractFixtures.payloadJSON("reasoning_provider.json", name))
}

private func alert(_ name: String) throws -> MatchAlert {
    try decode(MatchAlert.self, try ContractFixtures.payloadJSON("match_alert.json", name))
}

/// The read-only reasoning card and the read-only ground match alerts.
final class GroundAlertTests: XCTestCase {
    // MARK: reasoning

    func testCloudAllowedFixture() throws {
        let card = try XCTUnwrap(MissionFormat.reasoningCard(try reasoning("cloud_allowed")))
        XCTAssertEqual(card.statusText, "Gemma (gemma4:latest)")
        XCTAssertEqual(card.cloudBadge, "Cloud allowed")
        XCTAssertTrue(card.cloudAllowed)
        XCTAssertEqual(card.availableText, "Available: Claude, Gemma, Off")
        XCTAssertTrue(card.unavailableNotes.isEmpty)
    }

    func testCloudAllowedWithoutKeyShowsTheGroundsReason() throws {
        let p = try reasoning("cloud_allowed_no_key")
        let card = try XCTUnwrap(MissionFormat.reasoningCard(p))
        XCTAssertEqual(card.availableText, "Available: Gemma, Off")
        XCTAssertEqual(card.unavailableNotes, ["Claude: " + GroundText.shorten(p.unavailable?["claude"])])
        XCTAssertTrue(card.unavailableNotes[0].contains("FLIGHTRISK_CLAUDE_API_KEY is not set."))
    }

    func testLocalGemmaCloudBlocked() throws {
        let p = try reasoning("local_gemma_cloud_blocked")
        let card = try XCTUnwrap(MissionFormat.reasoningCard(p))
        XCTAssertEqual(card.cloudBadge, "Cloud not allowed")
        XCTAssertFalse(card.cloudAllowed)
        XCTAssertEqual(card.unavailableNotes.count, 1)
        XCTAssertTrue(card.unavailableNotes[0].hasPrefix("Claude: Cloud reasoning is not enabled."))
    }

    func testReasoningOff() throws {
        let card = try XCTUnwrap(MissionFormat.reasoningCard(try reasoning("off")))
        XCTAssertEqual(card.statusText, "Reasoning off: matches use ReID and face scores only")
        XCTAssertEqual(card.cloudBadge, "Cloud not allowed")
    }

    func testNoPayloadHidesTheCardAndAMissingProviderIsSaid() throws {
        XCTAssertNil(MissionFormat.reasoningCard(nil))
        XCTAssertEqual(MissionFormat.reasoningCard(try! decode(ReasoningProviderPayload.self, "{}"))?.statusText, "Provider not reported")
        XCTAssertEqual(MissionFormat.reasoningCard(try! decode(ReasoningProviderPayload.self, "{}"))?.cloudBadge, "Cloud not allowed")
    }

    func testTenThousandAvailableEntriesAreBounded() throws {
        var p = try reasoning("cloud_allowed")
        p.available = (0..<10_000).map { .unknown("provider-\($0)-" + String(repeating: "z", count: 500)) }
        let text = try XCTUnwrap(MissionFormat.reasoningCard(p)?.availableText)
        XCTAssertTrue(text.hasSuffix(" +9992 more"), String(text.suffix(30)))
        XCTAssertLessThan(text.count, 8 * 60)
        // Repeated names count once.
        p.available = Array(repeating: .gemma, count: 10_000) + [.off]
        XCTAssertEqual(MissionFormat.reasoningCard(p)?.availableText, "Available: Gemma, Off")
    }

    func testTenThousandUnavailableEntriesAndLongTextAreBounded() throws {
        var p = try reasoning("cloud_allowed")
        var map: [String: String] = [:]
        for i in 0..<10_000 { map["name\(i)"] = String(repeating: "r", count: 10_000) }
        p.unavailable = map
        p.label = String(repeating: "L", count: 10_000)
        p.model = String(repeating: "M", count: 10_000)
        let card = try XCTUnwrap(MissionFormat.reasoningCard(p))
        XCTAssertEqual(card.unavailableNotes.count, MissionFormat.maxUnavailable)
        XCTAssertTrue(card.unavailableNotes.allSatisfy { $0.count <= 345 })
        XCTAssertLessThanOrEqual(card.statusText.count, 170)
    }

    func testFirstKeysAreTheSmallestInOrder() {
        var table: [String: String] = [:]
        for i in 0..<1_000 { table["k\(String(format: "%04d", i))"] = "v" }
        XCTAssertEqual(MissionFormat.firstKeys(of: table, count: 3), ["k0000", "k0001", "k0002"])
        XCTAssertEqual(MissionFormat.firstKeys(of: ["b": "", "a": ""], count: 8), ["a", "b"])
        var p = try! reasoning("cloud_allowed")
        p.unavailable = table
        XCTAssertEqual(MissionFormat.reasoningCard(p)?.unavailableNotes.first, "K0000: v")
    }

    func testReasoningNotLiveTag() throws {
        XCTAssertEqual(MissionFormat.reasoningCard(try reasoning("off"), isStale: true)?.notLiveNote, "Not live: last values from the ground.")
        XCTAssertNil(MissionFormat.reasoningCard(try reasoning("off"), isStale: false)?.notLiveNote)
    }

    // MARK: alerts

    func testEdgeMatchFixtureRow() throws {
        let state = GroundAlertsState(alerts: [try alert("edge_match")], isStale: false)
        let row = try XCTUnwrap(state.rows.first)
        XCTAssertEqual(row.levelText, "Possible match")
        XCTAssertEqual(row.trackText, "Track 8_7")
        XCTAssertEqual(row.timeText, "12:34:56")
        XCTAssertEqual(row.scoreText, "Score 86% (ReID 86%, face 0%)")
        XCTAssertNil(row.reasonText)
        XCTAssertFalse(row.isReminder)
        XCTAssertNil(state.notLiveNote)
    }

    func testReminderOnConnectShape() throws {
        let row = try XCTUnwrap(GroundAlertsState(alerts: [try alert("reminder_on_connect")], isStale: false).rows.first)
        XCTAssertTrue(row.isReminder)
        XCTAssertTrue(row.accessibilitySummary.contains("Reminder at 12:34:56: the ground said this alert was waiting for the operator."))
        XCTAssertEqual(GroundAlertsState.reminderTag(time: "12:34:56"), "Reminder at 12:34:56: the ground said this alert was waiting for the operator.")
        XCTAssertEqual(row.scoreText, "Score 86% (ReID 86%, face 0%)")
    }

    func testListIsNewestFirstAndCappedAtOneHundred() throws {
        var alerts: [MatchAlert] = []
        for i in 0..<250 {
            var a = try alert("edge_match")
            a.alertId = "a\(i)"
            a.time = "t\(i)"
            alerts.append(a)
        }
        let rows = GroundAlertsState(alerts: alerts, isStale: false).rows
        XCTAssertEqual(rows.count, 100)
        XCTAssertEqual(rows.first?.timeText, "t249")
        XCTAssertEqual(rows.last?.timeText, "t150")
    }

    func testReasonTextIsShortenedAndUnsafeCharactersReplaced() throws {
        var a = try alert("edge_match")
        a.reasoning = String(repeating: "x", count: 10_000)
        a.alertLevel = .unknown(String(repeating: "y", count: 500))
        a.time = "12:00\u{202E}:00"
        a.score = .nan
        a.reidScore = 7
        let row = try XCTUnwrap(GroundAlertsState(alerts: [a], isStale: false).rows.first)
        XCTAssertEqual(row.reasonText?.count, 300)
        XCTAssertLessThanOrEqual(row.levelText.count, 40)
        XCTAssertFalse(row.timeText.unicodeScalars.contains("\u{202E}"))
        XCTAssertEqual(row.scoreText, "Score -- (ReID 100%, face 0%)")
    }

    func testAlertsNotLiveTag() throws {
        XCTAssertEqual(GroundAlertsState(alerts: [try alert("edge_match")], isStale: true).notLiveNote, "Not live: last values from the ground.")
    }

    func testEmptyListHasNoRows() {
        XCTAssertTrue(GroundAlertsState(alerts: [], isStale: false).rows.isEmpty)
    }
}

/// Alerts and reasoning through the view model: read only, nothing is sent for them.
@MainActor
final class GroundAlertViewModelTests: XCTestCase {
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

    func testAlertsAndReminderAndReasoningArriveAndNothingIsSent() async throws {
        let t = await harness.connect()
        let sentBefore = t.sent.count
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        await harness.push(t, "match_alert_reminder", try ContractFixtures.payloadJSON("match_alert.json", "reminder_on_connect")) { self.vm.matchAlerts.first?.renotify == true }
        XCTAssertEqual(vm.matchAlerts.count, 1, "a reminder for a known alert updates it")
        await harness.push(t, "reasoning_provider", try ContractFixtures.payloadJSON("reasoning_provider.json", "off")) { self.vm.reasoningProvider != nil }
        await harness.settle()
        XCTAssertEqual(t.sent.count, sentBefore, "showing alerts and reasoning sends nothing")
        let state = GroundAlertsState(alerts: vm.matchAlerts, isStale: vm.isStateStale)
        XCTAssertEqual(state.rows.count, 1)
        XCTAssertTrue(state.rows[0].isReminder)
        XCTAssertTrue(vm.isStateStale, "no fleet_status yet: the cards say not live")
        XCTAssertNotNil(state.notLiveNote)
        XCTAssertNotNil(MissionFormat.reasoningCard(vm.reasoningProvider, isStale: vm.isStateStale)?.notLiveNote)
    }

    func testTheSnapshotIsNotKept() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        XCTAssertNil(vm.matchAlerts[0].snapshot)
        XCTAssertEqual(vm.matchAlerts[0].score, 0.86)
        await harness.push(t, "match_alert_reminder", try ContractFixtures.payloadJSON("match_alert.json", "reminder_on_connect")) { self.vm.matchAlerts.first?.renotify == true }
        XCTAssertNil(vm.matchAlerts[0].snapshot)
    }

    func testThePhoneCannotBuildAMatchAlertResponse() {
        XCTAssertFalse(GroundCommand.allowedEventNames.contains("match_alert_response"))
        XCTAssertFalse(GroundViewModel.emittableEventNames.contains("match_alert_response"))
        XCTAssertFalse(GroundViewModel.emittableEventNames.contains("set_reasoning_provider"))
    }

    func testNoSecretInAnyDump() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        var dumped = ""
        dump(vm, to: &dumped)
        dump(GroundAlertsState(alerts: vm.matchAlerts, isStale: false), to: &dumped)
        XCTAssertFalse(dumped.contains(GroundHarness.testKey))
    }
}
