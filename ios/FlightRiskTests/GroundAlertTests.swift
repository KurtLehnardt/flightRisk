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
        XCTAssertNil(row.verdict)
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

    func testRawReasoningIsNeverShownAndUnsafeCharactersAreReplaced() throws {
        var a = try alert("edge_match")
        a.reasoning = String(repeating: "x", count: 10_000)
        a.alertLevel = .unknown(String(repeating: "y", count: 500))
        a.time = "12:00\u{202E}:00"
        a.score = .nan
        a.reidScore = 7
        let row = try XCTUnwrap(GroundAlertsState(alerts: [a], isStale: false).rows.first)
        XCTAssertNil(row.verdict, "no gemma_confidence: no verdict, and the raw reasoning is not shown")
        XCTAssertFalse(row.accessibilitySummary.contains("xxx"))
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

    /// The levels as the dashboard names them: a `no_match` alert reads "Weak signal", and a
    /// strong match says to verify in person.
    func testLevelTexts() {
        XCTAssertEqual(GroundAlertsState.level(.confirmedMatch), "Strong match: verify in person")
        XCTAssertEqual(GroundAlertsState.level(.possibleMatch), "Possible match")
        XCTAssertEqual(GroundAlertsState.level(.weakSignal), "Weak signal")
        XCTAssertEqual(GroundAlertsState.level(.noMatch), "Weak signal")
        XCTAssertEqual(GroundAlertsState.level(nil), "Match alert")
    }

    /// An alert the verdict book does not keep (no `alert_id`, or one too long) still shows the
    /// verdict it carries, through the same text rules: an error is the neutral note, and the
    /// raw reasoning (here an exception with part of a key) is never shown.
    func testAnAlertTheBookDoesNotKeepShowsItsOwnVerdictAndNeverTheRawReasoning() throws {
        let leaked = "anthropic.APIError: 401 key sk-ant-xxx"
        for id in [nil, String(repeating: "a", count: AlertVerdictBook.maxAlertIdLength + 1)] as [String?] {
            var a = try alert("edge_match")
            a.alertId = id
            a.gemmaConfidence = .string("error")
            a.gemmaMatch = .bool(false)
            a.reasoning = leaked
            var book = AlertVerdictBook()
            book.apply(.matchAlert(a))
            XCTAssertNil(book.display(for: a), "the book keeps nothing for this alert")
            let row = try XCTUnwrap(GroundAlertsState(alerts: [a], isStale: false, verdicts: book).rows.first)
            XCTAssertEqual(row.verdict?.headline, AlertVerdictText.errorNote)
            XCTAssertNil(row.verdict?.rowRationale)
            XCTAssertNil(row.verdict?.detailRationale)
            XCTAssertFalse(row.accessibilitySummary.contains("sk-ant"), row.accessibilitySummary)
            XCTAssertFalse(row.accessibilitySummary.contains("APIError"))

            // A carried match verdict is shown as a verdict.
            a.gemmaConfidence = .string("high")
            a.gemmaMatch = .bool(true)
            a.reasoning = "Same coat."
            let matched = try XCTUnwrap(GroundAlertsState(alerts: [a], isStale: false, verdicts: book).rows.first)
            XCTAssertEqual(matched.verdict?.headline, "AI check: match (high confidence)")
            XCTAssertEqual(matched.verdict?.rowRationale, "Same coat.")
        }
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

    func testResolvedEventMarksTheAlertAnsweredAndEndsTheReminder() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        await harness.push(t, "match_alert_reminder", try ContractFixtures.payloadJSON("match_alert.json", "reminder_on_connect")) { self.vm.matchAlerts.first?.renotify == true }
        let before = GroundAlertsState(alerts: vm.matchAlerts, isStale: false, answers: vm.alertAnswers)
        XCTAssertTrue(before.rows[0].isReminder)
        XCTAssertNil(before.rows[0].answer)
        let sentBefore = t.sent.count
        await harness.push(t, "match_alert_resolved", try ContractFixtures.payloadJSON("match_alert.json", "resolved_dismiss")) { !self.vm.alertAnswers.isEmpty }
        await harness.settle()
        XCTAssertEqual(vm.alertAnswers["00000000000040008000000000000001"], .dismiss)
        XCTAssertEqual(vm.matchAlerts.count, 1, "the row stays so the operator can see how it ended")
        let after = GroundAlertsState(alerts: vm.matchAlerts, isStale: false, answers: vm.alertAnswers)
        XCTAssertFalse(after.rows[0].isReminder, "an answered alert no longer reads as waiting")
        XCTAssertEqual(after.rows[0].answer, .dismiss)
        XCTAssertTrue(after.rows[0].accessibilitySummary.contains("dismissed"))
        XCTAssertFalse(after.rows[0].accessibilitySummary.contains("Reminder at"))
        XCTAssertEqual(t.sent.count, sentBefore, "an answer reported by the ground sends nothing")
        // A late reminder for the same alert does not undo the answer.
        await harness.push(t, "match_alert_reminder", try ContractFixtures.payloadJSON("match_alert.json", "reminder_on_connect")) { true }
        await harness.settle()
        let late = GroundAlertsState(alerts: vm.matchAlerts, isStale: false, answers: vm.alertAnswers)
        XCTAssertFalse(late.rows[0].isReminder)
        XCTAssertEqual(late.rows[0].answer, .dismiss)
    }

    func testConfirmAnswerAndAnswerArrivingBeforeTheAlert() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert_resolved", try ContractFixtures.payloadJSON("match_alert.json", "resolved_confirm")) { self.vm.alertAnswers.count == 1 }
        XCTAssertEqual(vm.alertAnswers["00000000000040008000000000000002"], .confirm)
        XCTAssertTrue(vm.matchAlerts.isEmpty, "an answer never invents an alert row")
        XCTAssertEqual(GroundAlertsState.answerTag(.confirm), "Answered on the laptop: confirmed.")
        XCTAssertEqual(GroundAlertsState.answerTag(.dismiss), "Answered on the laptop: dismissed.")
        XCTAssertEqual(GroundAlertsState.answerTag(.unknown("x")), "Answered on the laptop.")
    }

    func testHostileOrIncompleteResolvedEventsChangeNothing() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        for body in [#"{"ok":true,"action":"confirm"}"#, #"{"ok":true,"alert_id":"","action":"confirm"}"#,
                     #"{"ok":false,"alert_id":"00000000000040008000000000000001","action":"confirm"}"#,
                     #"{"ok":true,"alert_id":9,"action":"confirm"}"#] {
            t.push(GroundTestFrames.event("match_alert_resolved", body))
        }
        t.push(GroundTestFrames.event("match_alert_resolved", "[1]"))
        await harness.settle()
        XCTAssertTrue(vm.alertAnswers.isEmpty)
        let state = GroundAlertsState(alerts: vm.matchAlerts, isStale: false, answers: vm.alertAnswers)
        XCTAssertNil(state.rows[0].answer)
    }

    func testTheFirstAnswerWinsAndOkMustBeTrue() async throws {
        let t = await harness.connect()
        let id = "00000000000040008000000000000001"
        t.push(GroundTestFrames.event("match_alert_resolved", #"{"alert_id":"\#(id)","action":"dismiss"}"#))
        await harness.settle()
        XCTAssertTrue(vm.alertAnswers.isEmpty, "a missing ok is not an answer")
        await harness.push(t, "match_alert_resolved", #"{"ok":true,"alert_id":"\#(id)","action":"dismiss"}"#) { self.vm.alertAnswers.count == 1 }
        t.push(GroundTestFrames.event("match_alert_resolved", #"{"ok":true,"alert_id":"\#(id)","action":"confirm"}"#))
        await harness.settle()
        XCTAssertEqual(vm.alertAnswers[id], .dismiss, "a later answer does not change the first")
    }

    func testOverlongIdsAreIgnoredAndUnknownActionsAreCut() async throws {
        let t = await harness.connect()
        let longId = String(repeating: "a", count: 129)
        t.push(GroundTestFrames.event("match_alert_resolved", #"{"ok":true,"alert_id":"\#(longId)","action":"confirm"}"#))
        await harness.settle()
        XCTAssertTrue(vm.alertAnswers.isEmpty)
        let rawAction = String(repeating: "x", count: 500)
        await harness.push(t, "match_alert_resolved", #"{"ok":true,"alert_id":"a-1","action":"\#(rawAction)"}"#) { self.vm.alertAnswers.count == 1 }
        guard case let .unknown(shown)? = vm.alertAnswers["a-1"] else { return XCTFail("expected an unknown action") }
        XCTAssertLessThan(shown.count, 100)
        let ok = String(repeating: "b", count: 128)
        await harness.push(t, "match_alert_resolved", #"{"ok":true,"alert_id":"\#(ok)","action":"dismiss"}"#) { self.vm.alertAnswers.count == 2 }
    }

    func testAnswersAreForgottenOnReset() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert_resolved", try ContractFixtures.payloadJSON("match_alert.json", "resolved_dismiss")) { self.vm.alertAnswers.count == 1 }
        XCTAssertNil(vm.saveAddress("ws://192.168.1.77:5555"), "a different ground")
        XCTAssertTrue(vm.alertAnswers.isEmpty)
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

/// The reasoning verdict on the alert rows (#185) and the demo mode, through the real view model.
@MainActor
final class GroundVerdictAndDemoViewModelTests: XCTestCase {
    private var harness: GroundHarness!
    private var vm: GroundViewModel { harness.viewModel }
    private let alertId = "00000000000040008000000000000001"

    override func setUp() async throws {
        try await super.setUp()
        harness = GroundHarness()
    }

    override func tearDown() async throws {
        if vm.isDemoActive { vm.stopDemo() }
        await harness.tearDown()
        harness = nil
        try await super.tearDown()
    }

    private var rows: [GroundAlertRow] {
        GroundAlertsState(alerts: vm.matchAlerts, isStale: false, answers: vm.alertAnswers, verdicts: vm.alertVerdicts).rows
    }

    func testTheVerdictAndTheUpgradeReachTheAlertRowAndNothingIsSent() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        let sentBefore = t.sent.count
        await harness.push(t, "reasoning_result", #"{"track_id":"8_7","alert_id":"\#(alertId)","type":"analyze","result":{"match":true,"confidence":"high","reasoning":"Same jacket and build."}}"#) {
            self.vm.alertVerdicts.verdict(for: self.alertId) != nil
        }
        await harness.push(t, "alert_upgrade", #"{"track_id":"8_7","alert_id":"\#(alertId)","new_level":"confirmed_match","reasoning":"Same jacket and build."}"#) {
            self.rows.first?.levelText == "Strong match: verify in person"
        }
        var row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.verdict?.headline, "AI check: match (high confidence)")
        XCTAssertEqual(row.verdict?.rowRationale, "Same jacket and build.")
        XCTAssertFalse(row.accessibilitySummary.contains("Awaiting"), "the verdict replaces the alert's own reasoning text")
        XCTAssertTrue(row.accessibilitySummary.contains("AI check: match (high confidence)"))
        // A reminder that carries the older level does not lower the row.
        await harness.push(t, "match_alert_reminder", try ContractFixtures.payloadJSON("match_alert.json", "reminder_on_connect")) { self.vm.matchAlerts.first?.renotify == true }
        row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.levelText, "Strong match: verify in person")
        XCTAssertEqual(row.verdict?.headline, "AI check: match (high confidence)")
        await harness.settle()
        XCTAssertEqual(t.sent.count, sentBefore, "a verdict from the ground sends nothing")
    }

    func testAnErrorVerdictIsANeutralNoteAndAVerdictIsForgottenWithTheGround() async throws {
        let t = await harness.connect()
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        await harness.push(t, "reasoning_result", #"{"alert_id":"\#(alertId)","type":"analyze","result":{"match":false,"confidence":"error","reasoning":"Traceback"}}"#) {
            self.vm.alertVerdicts.verdict(for: self.alertId) != nil
        }
        let row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.verdict?.isNeutral, true)
        XCTAssertEqual(row.verdict?.headline, AlertVerdictText.errorNote)
        XCTAssertEqual(row.levelText, "Possible match")
        XCTAssertNil(vm.saveAddress("ws://192.168.1.77:5555"), "a different ground")
        XCTAssertEqual(vm.alertVerdicts.count, 0)
    }

    func testTheDemoClosesTheLiveLinkAndAnswersCommandsOnThePhone() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        await harness.push(t, "match_alert", try ContractFixtures.payloadJSON("match_alert.json", "edge_match")) { self.vm.matchAlerts.count == 1 }
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
        let transportsBefore = harness.recorder.transports.count
        vm.startDemo()
        XCTAssertTrue(vm.isDemoActive)
        XCTAssertEqual(vm.linkStatus, .demo)
        XCTAssertEqual(vm.linkStatus.pillText, "DEMO")
        XCTAssertFalse(vm.isEnabled, "the live link is switched off, not only closed")
        XCTAssertFalse(vm.matchAlerts.contains { $0.alertId == alertId }, "the live ground's alert is gone")
        XCTAssertEqual(vm.droneCards.map(\.id), ["demo-drone-1", "demo-drone-2"], "the demo ground's drones")
        // The closing link may still send its leave frame (41); no event frame may follow.
        let eventsBefore = t.sentEventNames

        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
        harness.clock.advance(1)
        await harness.waitUntil("the demo's land_all_ack") { self.vm.landAllFeedback?.phase == .succeeded }
        await vm.landDrone(DemoGround.spotterDroneId)
        harness.clock.advance(2)
        await harness.waitUntil("the demo's manual_result") { self.vm.landFeedback[DemoGround.spotterDroneId]?.phase == .succeeded }

        // The live link cannot come back while the demo runs.
        vm.setEnabled(true)
        vm.retryConnection()
        vm.appWillEnterForeground()
        let probe = await vm.testConnection()
        XCTAssertEqual(probe, GroundUIText.demoBlocksLink)
        await harness.settle()
        XCTAssertFalse(vm.isEnabled)
        XCTAssertEqual(vm.linkStatus, .demo)
        XCTAssertEqual(harness.recorder.transports.count, transportsBefore, "no new connection")
        XCTAssertEqual(t.sentEventNames, eventsBefore, "no demo command reached the live socket")
        XCTAssertTrue(harness.http.headers.isEmpty, "no health probe")
    }

    func testTheDemoScenarioReachesTheAlertRowsAndLeavingClearsEverything() async throws {
        XCTAssertEqual(vm.demoStartStep, .startNow, "no live link: no confirmation")
        vm.startDemo()
        harness.clock.advance(DemoGround.Moment.verdict + 1)
        await harness.waitUntil("the demo's verdict") { self.vm.alertVerdicts.verdict(for: DemoGround.alertId)?.confidence == "high" }
        let row = try XCTUnwrap(rows.first)
        XCTAssertEqual(row.levelText, "Strong match: verify in person")
        XCTAssertEqual(row.verdict?.headline, "AI check: match (high confidence)")
        XCTAssertFalse(vm.banners.isEmpty, "the obstacle cut is shown")
        vm.stopDemo()
        XCTAssertFalse(vm.isDemoActive)
        XCTAssertNotEqual(vm.linkStatus, .demo)
        XCTAssertTrue(vm.matchAlerts.isEmpty)
        XCTAssertEqual(vm.alertVerdicts.count, 0)
        XCTAssertTrue(vm.droneCards.isEmpty)
        XCTAssertTrue(vm.banners.isEmpty)
        XCTAssertNil(vm.fleetPlan)
        XCTAssertNil(vm.landAllFeedback)
        // A stopped demo delivers nothing more.
        harness.clock.advance(30)
        await harness.settle()
        await harness.settle()
        XCTAssertTrue(vm.droneCards.isEmpty)
    }

    // MARK: Demo safety (PR #196 review)

    /// The demo is refused while live drones are in the air: it would close the link they depend
    /// on. Nothing changes; the dialog gets the reason.
    func testTheDemoIsRefusedWhileLiveDronesAreInTheAir() async throws {
        let t = await harness.connect()
        // drone-1 flying; drone-2 held with the orphan countdown (no flying flag): both in the air.
        await harness.push(t, "fleet_status", GroundSamples.fleetTwo) { self.vm.droneCards.count == 2 }
        let transportsBefore = harness.recorder.transports.count
        XCTAssertEqual(vm.demoStartStep, .refused(DemoModePolicy.flyingRefusal(2, stale: false)))
        XCTAssertTrue(DemoModePolicy.flyingRefusal(2, stale: false).hasPrefix("2 drones are in the air. Land them first."))
        XCTAssertFalse(vm.startDemo())
        XCTAssertFalse(vm.isDemoActive)
        XCTAssertTrue(vm.isEnabled, "the live link stays switched on")
        XCTAssertEqual(vm.linkStatus, .connected)
        XCTAssertEqual(vm.droneCards.map(\.id), ["drone-1", "drone-2"])
        // Off the link, the last values still count: the drones may still be up.
        vm.setEnabled(false)
        XCTAssertEqual(vm.demoStartStep, .refused(DemoModePolicy.flyingRefusal(2, stale: true)))
        XCTAssertFalse(vm.startDemo())
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, transportsBefore)
    }

    /// The demo is refused while a live LAND ALL waits for its answer, failed, or left a failure
    /// banner; starting it never clears that result.
    func testTheDemoIsRefusedWhileALiveLandAllIsUnansweredOrFailed() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)
        XCTAssertEqual(vm.demoStartStep, .refused(DemoModePolicy.landAllUnansweredRefusal))
        XCTAssertFalse(vm.startDemo())
        XCTAssertEqual(vm.landAllFeedback?.phase, .handedOver)

        await harness.push(t, "land_all_ack", GroundSamples.ackReachedNone) { self.vm.landAllFeedback?.phase == .failed }
        XCTAssertEqual(vm.demoStartStep, .refused(DemoModePolicy.landAllFailedRefusal))
        XCTAssertFalse(vm.startDemo())
        XCTAssertFalse(vm.isDemoActive)
        XCTAssertEqual(vm.landAllFeedback?.phase, .failed, "a live LAND ALL failure is never cleared by the demo")
        XCTAssertEqual(vm.landAllFeedback?.text, GroundText.landAllFailedNoDrone)

        vm.dismissLandAllFeedback()
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)

        // A failed LAND ALL the ground reports as a fleet event leaves a banner; dismissing only the
        // top bar result is not enough.
        await harness.push(t, "fleet_event", GroundSamples.fleetEventLandAllFailed) { self.vm.landAllFeedback?.phase == .failed }
        vm.dismissLandAllFeedback()
        let banner = try XCTUnwrap(vm.banners.first { $0.isLandAllFailure })
        XCTAssertEqual(vm.demoStartStep, .refused(DemoModePolicy.landAllFailedRefusal))
        vm.dismissBanner(banner.id)
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
        XCTAssertTrue(vm.startDemo())
        XCTAssertTrue(vm.isDemoActive)
    }

    /// While the demo runs, LAND ALL and every result say "demo", and VoiceOver hears when the
    /// demo starts and stops.
    func testTheDemoMarksEveryResultAndAnnouncesStartAndStop() async throws {
        var announcements: [String] = []
        vm.announce = { announcements.append($0) }
        XCTAssertTrue(vm.startDemo())
        XCTAssertEqual(announcements, [GroundUIText.demoStartedAnnouncement])
        XCTAssertEqual(GroundUIText.landAllButtonDemo, "LAND ALL (demo)")
        XCTAssertEqual(GroundUIText.landAllHintDemo,
                       "Demo: lands only the simulated drones on this phone. Nothing is sent to a ground station.")

        harness.clock.advance(DemoGround.Moment.takeoff + 1)
        await harness.waitUntil("the demo drones flying") { self.vm.droneCards.count == 2 && self.vm.reducer.drones[DemoGround.spotterDroneId]?.isFlying == true }
        await vm.landAll()
        XCTAssertEqual(vm.landAllFeedbackText?.hasPrefix("DEMO: LAND ALL"), true, vm.landAllFeedbackText ?? "")
        harness.clock.advance(1)
        await harness.waitUntil("the demo's land_all_ack") { self.vm.landAllFeedback?.phase == .succeeded }
        XCTAssertEqual(vm.landAllFeedbackText, "DEMO: LAND ALL reached drones (coordinator 2, transport 2)")
        await vm.landDrone(DemoGround.cutDroneId)
        let sending = try XCTUnwrap(vm.droneCards.first { $0.id == DemoGround.cutDroneId }?.landFeedbackText)
        XCTAssertTrue(sending.hasPrefix("DEMO: demo-drone-2: Land"), sending)
        harness.clock.advance(2)
        await harness.waitUntil("the demo's manual_result") { self.vm.landFeedback[DemoGround.cutDroneId]?.phase == .succeeded }
        let done = try XCTUnwrap(vm.droneCards.first { $0.id == DemoGround.cutDroneId }?.landFeedbackText)
        XCTAssertTrue(done.hasPrefix("DEMO: demo-drone-2: Land acknowledged"), done)
        XCTAssertFalse(vm.banners.isEmpty)
        for banner in vm.banners { XCTAssertTrue(banner.text.hasPrefix("DEMO: "), banner.text) }
        // The stored text is the ground's; only what is shown carries the prefix.
        XCTAssertFalse(vm.landAllFeedback?.text.hasPrefix("DEMO") ?? true)

        vm.stopDemo()
        XCTAssertEqual(announcements, [GroundUIText.demoStartedAnnouncement, GroundUIText.demoStoppedAnnouncement])
        XCTAssertTrue(vm.toasts.isEmpty, "the demo did not switch a link off")
        XCTAssertEqual(vm.shownCommandText("x"), "x", "no prefix outside the demo")
    }

    /// After the demo the link is off (or not configured), stays switched off, and the Ground tab
    /// says that the demo switched it off.
    func testLeavingTheDemoLeavesTheLinkOffAndSaysSo() async throws {
        // Not configured: nothing to switch off, nothing to say.
        XCTAssertTrue(vm.startDemo())
        vm.stopDemo()
        XCTAssertEqual(vm.linkStatus, .notConfigured)
        XCTAssertFalse(vm.isEnabled)
        XCTAssertTrue(vm.toasts.isEmpty)

        var announcements: [String] = []
        vm.announce = { announcements.append($0) }
        let t0 = await harness.connect()
        await harness.push(t0, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
        XCTAssertTrue(vm.startDemo())
        let transports = harness.recorder.transports.count
        vm.restartDemo()
        XCTAssertTrue(vm.toasts.isEmpty, "playing the demo again is not leaving it")
        vm.stopDemo()
        XCTAssertEqual(vm.linkStatus, .off)
        XCTAssertFalse(vm.isEnabled)
        XCTAssertEqual(vm.toasts.map(\.text), [GroundUIText.demoSwitchedLinkOff])
        XCTAssertEqual(vm.toasts.first?.isError, true, "it stays until dismissed")
        XCTAssertEqual(announcements.last, GroundUIText.demoStoppedAnnouncement + " " + GroundUIText.demoSwitchedLinkOff)
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, transports, "the link did not come back by itself")
        // Said once: the next demo, with the link already off, says nothing more.
        vm.dismissToast(try XCTUnwrap(vm.toasts.first).id)
        XCTAssertTrue(vm.startDemo())
        vm.stopDemo()
        XCTAssertTrue(vm.toasts.isEmpty)
    }

    /// While the demo runs, saving an address or a key, pull to refresh and a profile change send
    /// nothing to any network: no transport, no event frame, no HTTP request.
    func testSettingsRefreshAndProfilesSendNothingDuringTheDemo() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        XCTAssertTrue(vm.startDemo())
        await harness.settle()
        let transports = harness.recorder.transports.count
        // The closing link may still send its leave frame (41); no event frame may follow.
        let events = t.sentEventNames
        XCTAssertNil(vm.saveAddress("ws://192.168.1.77:5555"))
        XCTAssertNil(vm.saveKey("another-key"))
        await vm.refresh()
        harness.clock.advance(1)
        await harness.waitUntil("the demo's profiles") { self.vm.detectionProfiles?.drones?[DemoGround.spotterDroneId] != nil }
        await vm.selectDroneProfile(DemoGround.spotterDroneId, profile: "top_down")
        await vm.selectFleetProfile("level")
        harness.clock.advance(2)
        await harness.waitUntil("the demo's answer to the profile change") {
            self.vm.detectionProfiles?.drones?[DemoGround.spotterDroneId]?.active == "top_down"
        }
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, transports, "no new connection")
        XCTAssertEqual(t.sentEventNames, events, "nothing reached the live socket")
        XCTAssertTrue(harness.http.headers.isEmpty, "no HTTP request")
        XCTAssertTrue(vm.isDemoActive)
        XCTAssertEqual(vm.linkStatus, .demo)
        XCTAssertFalse(vm.isEnabled)
    }

    /// A LAND ALL whose send was still in flight when "Play the demo again" ran belongs to the
    /// old demo session: it is not waited for, so no "earlier LAND ALL got no answer" banner
    /// appears 40 seconds later.
    func testPlayingTheDemoAgainLeavesNoStaleLandAllBehind() async throws {
        XCTAssertTrue(vm.startDemo())
        let tap = Task { await self.vm.landAll() }
        // Let the tap run up to its send, which leaves the main actor.
        await Task.yield()
        XCTAssertEqual(vm.landAllFeedback?.phase, .sending, "the tap is in flight")
        vm.restartDemo()
        await tap.value
        XCTAssertTrue(vm.isDemoActive)
        harness.clock.advance(GroundViewModel.landAllExpiresAfter + 1)
        vm.tick(now: harness.clock.now)
        await harness.settle()
        vm.tick(now: harness.clock.now)
        XCTAssertFalse(vm.banners.contains { $0.text.contains(GroundUIText.earlierLandAllNoAnswer) }, "\(vm.banners.map(\.text))")
        XCTAssertFalse(vm.banners.contains { $0.isLandAllFailure })
    }

    // MARK: Round 2 fixes

    private static let oneGroundedFleet = #"{"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"lost":false,"is_flying":false,"control_mode":"autonomous"}}}"#
    private static let oneFlyingFleet = #"{"drones":{"drone-1":{"drone_id":"drone-1","connected":true,"lost":false,"is_flying":true,"control_mode":"autonomous"}}}"#

    /// Before the first fleet status, an unknown fleet state is not "no drones in the air": the
    /// demo is refused, and the live link stays as it is. Once the ground reports its drones the
    /// demo asks for the confirmation as usual.
    func testTheDemoIsRefusedBeforeTheGroundHasReportedItsDrones() async throws {
        let t = await harness.connect()
        XCTAssertTrue(vm.droneCards.isEmpty)
        XCTAssertEqual(vm.demoStartStep, .refused(GroundViewModel.fleetUnknownRefusal))
        XCTAssertEqual(GroundViewModel.fleetUnknownRefusal, "The ground has not reported its drones yet. Wait for it, or switch the link off in Settings first.")
        XCTAssertFalse(vm.startDemo())
        XCTAssertFalse(vm.isDemoActive)
        XCTAssertTrue(vm.isEnabled)
        XCTAssertFalse(t.closed, "the live link was not closed")

        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
    }

    /// A drone takes off while the confirmation shows; confirming must not start the demo.
    func testNothingStartsWhenADroneTakesOffWhileTheConfirmationShows() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
        let transports = harness.recorder.transports.count
        await harness.push(t, "fleet_status", Self.oneFlyingFleet) { self.vm.reducer.drones["drone-1"]?.isFlying == true }
        let reason = DemoStartConfirmation.start(vm)
        XCTAssertEqual(reason, DemoModePolicy.flyingRefusal(1, stale: false))
        XCTAssertFalse(vm.isDemoActive)
        XCTAssertTrue(vm.isEnabled)
        XCTAssertEqual(vm.linkStatus, .connected)
        XCTAssertFalse(t.closed, "the live link was not closed")
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, transports)
    }

    /// The scene hook, the foreground hooks and Retry open nothing during the demo or after it,
    /// even when the link was reconnecting before the demo started.
    func testSceneDidBecomeActiveDuringAndAfterTheDemoOpensNothing() async throws {
        let t = await harness.connect()
        await harness.push(t, "fleet_status", Self.oneGroundedFleet) { self.vm.droneCards.count == 1 }
        t.serverDrop()
        await harness.waitUntil("reconnecting") {
            if case .reconnecting = self.vm.linkStatus { return true }
            return false
        }
        XCTAssertEqual(vm.demoStartStep, .confirmDisconnectFirst)
        XCTAssertTrue(vm.startDemo())
        let transports = harness.recorder.transports.count
        for _ in 0..<3 {
            vm.sceneDidBecomeActive()
            vm.appDidEnterBackground()
            vm.appWillEnterForeground()
            vm.sceneDidBecomeActive()
            vm.retryConnection()
        }
        await harness.settle()
        XCTAssertEqual(vm.linkStatus, .demo)
        XCTAssertEqual(harness.recorder.transports.count, transports, "no new transport during the demo")
        vm.stopDemo()
        vm.sceneDidBecomeActive()
        vm.appWillEnterForeground()
        await harness.settle()
        XCTAssertEqual(harness.recorder.transports.count, transports, "no new transport after the demo")
        XCTAssertFalse(vm.isEnabled)
    }
}
