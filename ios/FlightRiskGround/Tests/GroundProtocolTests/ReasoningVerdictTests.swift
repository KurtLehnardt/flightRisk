import XCTest
@testable import GroundProtocol

/// The reasoning verdict for match alerts (#185): `reasoning_result` and `alert_upgrade` decode,
/// apply by `alert_id`, never lower an alert's level, and read as a verdict or a neutral note.
///
/// The payloads are inline JSON with the shapes `flightrisk/dashboard/alerts.py` emits
/// (`resolve_reasoning`, `_gemma_worker`). The shared contract fixtures do not cover these two
/// events yet; switch to them once `scripts/export_mobile_contract_fixtures.py` exports them.
final class ReasoningVerdictTests: XCTestCase {
    private let alertId = "00000000000040008000000000000001"

    private func event(_ name: String, _ json: String) -> GroundEvent {
        GroundEvent.decode(name: name, payload: Data(json.utf8))
    }

    private func alert(level: String = "possible_match", confidence: String? = nil, match: Bool? = nil,
                       reasoning: String? = nil, reminder: Bool = false, id: String? = nil) -> GroundEvent {
        var fields = [#""alert_id":"\#(id ?? alertId)""#, #""alert_level":"\#(level)""#, #""score":0.86"#, #""track_id":"8_7""#]
        fields.append(#""gemma_confidence":"# + (confidence.map { #""\#($0)""# } ?? "null"))
        fields.append(#""gemma_match":"# + (match.map { $0 ? "true" : "false" } ?? "null"))
        fields.append(#""reasoning":"# + (reasoning.map { #""\#($0)""# } ?? "null"))
        if reminder { fields.append(#""renotify":true"#) }
        return event(reminder ? "match_alert_reminder" : "match_alert", "{" + fields.joined(separator: ",") + "}")
    }

    private func result(confidence: String, match: Bool = true, reasoning: String = "Same jacket and build.",
                        type: String = "analyze", id: String? = nil) -> GroundEvent {
        event("reasoning_result", #"""
        {"track_id":"8_7","alert_id":"\#(id ?? alertId)","type":"\#(type)",
         "result":{"match":\#(match),"confidence":"\#(confidence)","reasoning":"\#(reasoning)"}}
        """#)
    }

    private func upgrade(_ level: String, id: String? = nil, reasoning: String = "Same jacket and build.") -> GroundEvent {
        event("alert_upgrade", #"{"track_id":"8_7","alert_id":"\#(id ?? alertId)","new_level":"\#(level)","reasoning":"\#(reasoning)"}"#)
    }

    private func matchAlert(_ e: GroundEvent) throws -> MatchAlert {
        switch e {
        case let .matchAlert(a), let .matchAlertReminder(a): return a
        default: throw XCTSkip("not an alert: \(e)")
        }
    }

    // MARK: Decoding

    func testBothEventsAreHandledAndDecode() throws {
        XCTAssertTrue(GroundEvent.handledEventNames.contains("reasoning_result"))
        XCTAssertTrue(GroundEvent.handledEventNames.contains("alert_upgrade"))
        guard case let .reasoningResult(r) = result(confidence: "high") else { return XCTFail("not decoded") }
        XCTAssertEqual(r.alertId, alertId)
        XCTAssertEqual(r.trackId, "8_7")
        XCTAssertEqual(r.type, "analyze")
        XCTAssertEqual(r.result, ReasoningResultBody(match: true, confidence: "high", reasoning: "Same jacket and build."))
        XCTAssertTrue(r.isAlertVerdict)
        guard case let .alertUpgrade(u) = upgrade("confirmed_match") else { return XCTFail("not decoded") }
        XCTAssertEqual(u.alertId, alertId)
        XCTAssertEqual(u.newLevel, .confirmedMatch)
        XCTAssertEqual(u.reasoning, "Same jacket and build.")
    }

    func testLenientDecodingUnknownKeysAndWrongTypes() {
        let odd = event("reasoning_result", #"{"alert_id":7,"type":"analyze","x_future_field":{"a":1},"result":{"match":"yes","confidence":3,"reasoning":null,"x_future_field":1}}"#)
        guard case let .reasoningResult(r) = odd else { return XCTFail("a wrong-typed field must not make the payload undecodable") }
        XCTAssertNil(r.alertId)
        XCTAssertEqual(r.result, ReasoningResultBody())
        XCTAssertFalse(r.isAlertVerdict)
        guard case let .alertUpgrade(u) = event("alert_upgrade", #"{"alert_id":"a","new_level":"certain"}"#) else { return XCTFail() }
        XCTAssertEqual(u.newLevel, .unknown("certain"))
        XCTAssertEqual(event("alert_upgrade", "[1]"), .undecodable(name: "alert_upgrade"))
    }

    func testDescriptionResultsAndResultsWithoutAnAlertAreNotVerdicts() {
        guard case let .reasoningResult(d) = result(confidence: "high", type: "description") else { return XCTFail() }
        XCTAssertFalse(d.isAlertVerdict)
        guard case let .reasoningResult(noId) = event("reasoning_result", #"{"track_id":"8_7","type":"analyze","result":{"match":true,"confidence":"high"}}"#) else { return XCTFail() }
        XCTAssertFalse(noId.isAlertVerdict)
        var book = AlertVerdictBook()
        book.apply(alert())
        book.apply(result(confidence: "high", type: "description"))
        XCTAssertNil(book.verdict(for: alertId))
    }

    func testAlertLevelVocabularyAndRanks() throws {
        XCTAssertEqual(Set(AlertLevel.knownWireValues), ["no_match", "weak_signal", "possible_match", "confirmed_match"])
        for wire in AlertLevel.knownWireValues {
            XCTAssertEqual(AlertLevel(wire: wire).wire, wire)
            XCTAssertTrue(AlertLevel(wire: wire).isKnown)
        }
        XCTAssertEqual([AlertLevel.noMatch, .weakSignal, .possibleMatch, .confirmedMatch, .unknown("x")].map(\.rank), [0, 1, 2, 3, 0])
        XCTAssertEqual(AlertLevel.raised(.possibleMatch, to: .confirmedMatch), .confirmedMatch)
        XCTAssertEqual(AlertLevel.raised(.confirmedMatch, to: .possibleMatch), .confirmedMatch)
        XCTAssertEqual(AlertLevel.raised(.possibleMatch, to: .unknown("definite")), .possibleMatch)
        XCTAssertEqual(AlertLevel.raised(nil, to: .weakSignal), .weakSignal)
        XCTAssertEqual(AlertLevel.raised(.weakSignal, to: nil), .weakSignal)
    }

    // MARK: The book

    func testVerdictLandsOnItsAlertAndReadsAsAVerdict() throws {
        var book = AlertVerdictBook()
        let first = alert(confidence: "pending", reasoning: "Awaiting Claude reasoning...")
        book.apply(first)
        let a = try matchAlert(first)
        XCTAssertEqual(book.verdict(for: alertId)?.kind, .pending)
        XCTAssertEqual(book.display(for: a)?.headline, AlertVerdictText.pendingNote)
        XCTAssertEqual(book.display(for: a)?.isNeutral, true)

        book.apply(result(confidence: "high"))
        let shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, "AI check: match (high confidence)")
        XCTAssertEqual(shown.confidence, "high")
        XCTAssertEqual(shown.rowRationale, "Same jacket and build.")
        XCTAssertEqual(shown.detailRationale, "Same jacket and build.")
        XCTAssertFalse(shown.isNeutral)

        book.apply(result(confidence: "medium", match: false, reasoning: "Different hair colour."))
        XCTAssertEqual(book.display(for: a)?.headline, "AI check: no match (medium confidence)")
    }

    func testAVerdictForAnAlertThePhoneNeverSawChangesNothing() throws {
        var book = AlertVerdictBook()
        book.apply(result(confidence: "high", id: "other"))
        book.apply(upgrade("confirmed_match", id: "other"))
        XCTAssertNil(book.verdict(for: "other"))
        XCTAssertEqual(book.count, 0)
        // A later alert with that id starts from its own level.
        let later = alert(id: "other")
        book.apply(later)
        XCTAssertEqual(book.level(for: try matchAlert(later)), .possibleMatch)
    }

    func testTheAlertLevelOnlyGoesUp() throws {
        var book = AlertVerdictBook()
        let first = try matchAlert(alert())
        book.apply(.matchAlert(first))
        XCTAssertEqual(book.level(for: first), .possibleMatch)
        book.apply(upgrade("weak_signal"))
        XCTAssertEqual(book.level(for: first), .possibleMatch, "a lower upgrade is ignored")
        book.apply(upgrade("confirmed_match"))
        XCTAssertEqual(book.level(for: first), .confirmedMatch)
        // A reminder that carries an older copy of the alert does not lower the level.
        let reminder = alert(level: "possible_match", reminder: true)
        book.apply(reminder)
        XCTAssertEqual(book.level(for: try matchAlert(reminder)), .confirmedMatch)
        // Nor does an unknown level, which ranks lowest.
        book.apply(upgrade("certain"))
        XCTAssertEqual(book.level(for: first), .confirmedMatch)
    }

    func testAReSentAlertNeverTurnsAFinalVerdictBackIntoPending() throws {
        var book = AlertVerdictBook()
        book.apply(alert(confidence: "pending"))
        book.apply(result(confidence: "high"))
        book.apply(alert(confidence: "pending", reminder: true))
        XCTAssertEqual(book.verdict(for: alertId)?.confidence, "high")
        // A reminder that carries the final verdict (the ground's latch copy) is taken.
        book.apply(alert(confidence: "low", match: true, reasoning: "Updated.", reminder: true))
        XCTAssertEqual(book.verdict(for: alertId)?.confidence, "low")
        XCTAssertEqual(book.verdict(for: alertId)?.rationale, "Updated.")
    }

    func testAnAlertOnReconnectCarriesItsVerdict() throws {
        var book = AlertVerdictBook()
        let reminder = alert(level: "confirmed_match", confidence: "high", match: true, reasoning: "Same jacket.", reminder: true)
        book.apply(reminder)
        let a = try matchAlert(reminder)
        XCTAssertEqual(book.level(for: a), .confirmedMatch)
        XCTAssertEqual(book.display(for: a)?.headline, "AI check: match (high confidence)")
    }

    func testAnAlertWithoutReasoningHasNoVerdict() throws {
        var book = AlertVerdictBook()
        let plain = alert()
        book.apply(plain)
        XCTAssertNil(book.display(for: try matchAlert(plain)))
    }

    func testErrorAndSkippedReadAsNeutralNotes() throws {
        let a = try matchAlert(alert())
        var book = AlertVerdictBook()
        book.apply(.matchAlert(a))
        book.apply(result(confidence: "error", match: false, reasoning: "Traceback: ValueError at 0x1234"))
        var shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, AlertVerdictText.errorNote)
        XCTAssertTrue(shown.isNeutral)
        XCTAssertNil(shown.rowRationale, "an error's text can be an exception message and is not shown")
        XCTAssertNil(shown.detailRationale)
        XCTAssertNil(shown.confidence)

        book.apply(result(confidence: "skipped", match: false, reasoning: "AI check skipped: alert was already answered."))
        shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, AlertVerdictText.skippedNote)
        XCTAssertTrue(shown.isNeutral)
        XCTAssertNil(shown.rowRationale)
    }

    func testOtherAnswersWithoutAVerdictAreNeutral() throws {
        let a = try matchAlert(alert())
        func shown(_ confidence: String, match: Bool = false, reasoning: String = "") throws -> AlertVerdictText.Display {
            var book = AlertVerdictBook()
            book.apply(.matchAlert(a))
            book.apply(result(confidence: confidence, match: match, reasoning: reasoning))
            return try XCTUnwrap(book.display(for: a))
        }
        let notRun = try shown("unavailable", reasoning: "AI check not run (queue full).")
        XCTAssertEqual(notRun.headline, AlertVerdictText.notRunNote)
        XCTAssertNil(notRun.rowRationale)
        XCTAssertEqual(notRun.detailRationale, "AI check not run (queue full).")
        XCTAssertTrue(notRun.isNeutral)
        XCTAssertEqual(try shown("off").headline, AlertVerdictText.offNote)
        XCTAssertEqual(try shown("unknown", reasoning: "MATCH: maybe").headline, AlertVerdictText.noClearVerdictNote)
        XCTAssertTrue(try shown("unknown").isNeutral)
        // A confidence word this build does not know is shown as the ground sent it.
        XCTAssertEqual(try shown("very_high", match: true).headline, "AI check: match (very_high confidence)")
    }

    func testAnUpgradeWithoutAResultIsStillAVerdict() throws {
        let a = try matchAlert(alert(confidence: "pending"))
        var book = AlertVerdictBook()
        book.apply(.matchAlert(a))
        book.apply(upgrade("confirmed_match", reasoning: "Same jacket."))
        XCTAssertEqual(book.display(for: a)?.headline, "AI check: match (high confidence)")
        XCTAssertEqual(book.level(for: a), .confirmedMatch)
        // The result that follows replaces it.
        book.apply(result(confidence: "medium"))
        XCTAssertEqual(book.verdict(for: alertId)?.confidence, "medium")
        XCTAssertEqual(book.level(for: a), .confirmedMatch, "the verdict text never lowers the level")
    }

    /// The dashboard ignores an `alert_upgrade` that does not rank higher than the level shown
    /// (`ui.js`); so does the phone, and such an upgrade writes no verdict either.
    func testAnUpgradeThatDoesNotRaiseTheLevelChangesNothing() throws {
        // The critic's probe: a confirmed alert without a verdict, then weak_signal and no_match.
        var book = AlertVerdictBook()
        let confirmed = try matchAlert(alert(level: "confirmed_match"))
        book.apply(.matchAlert(confirmed))
        XCTAssertNil(book.display(for: confirmed))
        book.apply(upgrade("weak_signal", reasoning: "x"))
        XCTAssertNil(book.display(for: confirmed), "a lower upgrade writes no verdict")
        XCTAssertEqual(book.level(for: confirmed), .confirmedMatch)
        book.apply(event("alert_upgrade", #"{"alert_id":"\#(alertId)","new_level":"no_match"}"#))
        XCTAssertNil(book.display(for: confirmed))
        book.apply(upgrade("confirmed_match"))
        XCTAssertNil(book.display(for: confirmed), "an equal level is not a raise")

        // An error, then an upgrade to the level the alert already has: the error stays.
        book = AlertVerdictBook()
        let possible = try matchAlert(alert(level: "possible_match", confidence: "pending"))
        book.apply(.matchAlert(possible))
        book.apply(result(confidence: "error", match: false, reasoning: "boom"))
        book.apply(upgrade("possible_match", reasoning: "r"))
        XCTAssertEqual(book.display(for: possible)?.headline, AlertVerdictText.errorNote)
        XCTAssertEqual(book.verdict(for: alertId)?.confidence, "error")
    }

    /// A raise to `weak_signal` moves the level but is not a match verdict; only a raise to
    /// `possible_match` or `confirmed_match` stands in for one.
    func testOnlyARaiseToAMatchLevelStandsInForAVerdict() throws {
        var book = AlertVerdictBook()
        let noMatch = try matchAlert(alert(level: "no_match"))
        book.apply(.matchAlert(noMatch))
        book.apply(upgrade("weak_signal"))
        XCTAssertEqual(book.level(for: noMatch), .weakSignal)
        XCTAssertNil(book.verdict(for: alertId))
        book.apply(upgrade("possible_match", reasoning: "Similar coat."))
        XCTAssertEqual(book.level(for: noMatch), .possibleMatch)
        XCTAssertEqual(book.display(for: noMatch)?.headline, "AI check: match (medium confidence)")
        XCTAssertEqual(book.display(for: noMatch)?.rowRationale, "Similar coat.")
        // An upgrade without a level changes nothing.
        book.apply(event("alert_upgrade", #"{"alert_id":"\#(alertId)","reasoning":"no level"}"#))
        XCTAssertEqual(book.level(for: noMatch), .possibleMatch)
        XCTAssertEqual(book.display(for: noMatch)?.rowRationale, "Similar coat.")
    }

    /// A result without `confidence` is still a yes or no; a result without `match` is no clear
    /// verdict. Neither crashes nor shows a confidence the ground did not send.
    func testAResultMissingConfidenceOrMatch() throws {
        let a = try matchAlert(alert(confidence: "pending"))
        var book = AlertVerdictBook()
        book.apply(.matchAlert(a))
        book.apply(event("reasoning_result", #"{"alert_id":"\#(alertId)","type":"analyze","result":{"match":true,"reasoning":"Same coat."}}"#))
        var shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, "AI check: match")
        XCTAssertNil(shown.confidence)
        XCTAssertFalse(shown.isNeutral)
        XCTAssertEqual(shown.rowRationale, "Same coat.")

        book.apply(event("reasoning_result", #"{"alert_id":"\#(alertId)","type":"analyze","result":{"confidence":"high","reasoning":"Unsure."}}"#))
        shown = try XCTUnwrap(book.display(for: a))
        XCTAssertEqual(shown.headline, AlertVerdictText.noClearVerdictNote)
        XCTAssertTrue(shown.isNeutral)
        XCTAssertNil(shown.rowRationale)

        book.apply(event("reasoning_result", #"{"alert_id":"\#(alertId)","type":"analyze","result":{}}"#))
        XCTAssertEqual(book.display(for: a)?.headline, AlertVerdictText.noClearVerdictNote)
    }

    /// `clip` counts and cuts grapheme clusters, never splitting a letter and its accent or an
    /// emoji, and removes the invisible direction marks.
    func testClipCutsOnGraphemeClustersAndRemovesDirectionMarks() {
        let accented = String(repeating: "e\u{301}", count: 10)
        XCTAssertEqual(AlertVerdictText.clip(accented, max: 10), accented)
        let cut = AlertVerdictText.clip(accented, max: 4)
        XCTAssertEqual(cut, "e\u{301}e\u{301}e\u{301}\u{2026}")
        XCTAssertEqual(cut.count, 4)
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}"
        XCTAssertEqual(AlertVerdictText.clip(family + family + family, max: 2), family + "\u{2026}")
        XCTAssertEqual(AlertVerdictText.clip("a\u{200E}b\u{200F}c\u{061C}d", max: 10), "abcd")
        XCTAssertEqual(AlertVerdictText.clip("\u{200E}\u{200F}abc", max: 2), "a\u{2026}", "a removed mark is not counted")
        XCTAssertEqual(AlertVerdictText.clip("a\r\nb\u{202E}c\u{2066}d", max: 10), "a b c d")
        XCTAssertEqual(AlertVerdictText.clip("abc", max: 0), "")
    }

    func testTheRationaleIsShortAndSafe() throws {
        let a = try matchAlert(alert())
        var book = AlertVerdictBook()
        book.apply(.matchAlert(a))
        let long = String(repeating: "word ", count: 400) + "\u{202E}"
        book.apply(result(confidence: "high", reasoning: "Line one\\nline two \\u202Ereversed " + long))
        let shown = try XCTUnwrap(book.display(for: a))
        let row = try XCTUnwrap(shown.rowRationale)
        XCTAssertEqual(row.count, AlertVerdictText.maxRowRationale)
        XCTAssertTrue(row.hasSuffix("\u{2026}"))
        XCTAssertTrue(row.hasPrefix("Line one line two  reversed"))
        XCTAssertFalse(row.unicodeScalars.contains { $0.value == 0x202E || $0.value == 0x0A })
        XCTAssertEqual(shown.detailRationale?.count, AlertVerdictText.maxDetailRationale)
        XCTAssertLessThanOrEqual(book.verdict(for: alertId)?.rationale?.count ?? 0, AlertVerdictText.maxStoredRationale)
    }

    func testOverlongIdsAreIgnoredAndTheBookIsBounded() throws {
        var book = AlertVerdictBook()
        let longId = String(repeating: "a", count: AlertVerdictBook.maxAlertIdLength + 1)
        book.apply(alert(id: longId))
        book.apply(result(confidence: "high", id: longId))
        XCTAssertEqual(book.count, 0)
        XCTAssertNil(book.verdict(for: longId))
        for i in 0..<(AlertVerdictBook.maxAlerts + 5) {
            book.apply(alert(confidence: "high", match: true, id: "id-\(i)"))
        }
        XCTAssertEqual(book.count, AlertVerdictBook.maxAlerts)
        XCTAssertNil(book.verdict(for: "id-0"), "the oldest ids are dropped first")
        XCTAssertNotNil(book.verdict(for: "id-\(AlertVerdictBook.maxAlerts + 4)"))
    }

    // MARK: Obstacle cut banner

    func testLaneBlockedIsAKnownFleetEventWithTheDashboardsText() {
        guard case let .fleetEvent(ev) = event("fleet_event", #"""
        {"event":"lane_blocked","drone_id":"drone-2","lane_index":0,"x_cm":250.0,"y_cm":210.0,
         "unflown_cm":190.0,"reason":"obstacle","detail":"drone-2: lane 0 stopped at (250, 210) cm, 190 cm not searched (obstacle)"}
        """#) else { return XCTFail("not decoded") }
        XCTAssertEqual(ev.resolvedKind, .laneBlocked)
        XCTAssertEqual(GroundText.fleetEventBanner(ev), "OBSTACLE drone-2: lane 0 stopped at (250, 210) cm, 190 cm not searched")
        var person = ev
        person.reason = "person"
        person.unflownCm = nil
        person.laneIndex = nil
        XCTAssertEqual(GroundText.fleetEventBanner(person),
                       "OBSTACLE drone-2: a lane stopped at (250, 210) cm; a person: re-flown only if the operator confirms it on the laptop")
        XCTAssertEqual(GroundText.fleetEventBannerInfo(ev)?.isFailure, false)
    }

    /// A cut that was not an obstacle or a person names its reason: no obstacle was seen.
    func testLaneBlockedNamesAReasonOtherThanAnObstacleOrAPerson() {
        guard case let .fleetEvent(ev) = event("fleet_event", #"""
        {"event":"lane_blocked","drone_id":"drone-2","lane_index":1,"x_cm":250.0,"y_cm":210.0,"unflown_cm":190.0,"reason":"sensor_down"}
        """#) else { return XCTFail("not decoded") }
        let base = "OBSTACLE drone-2: lane 1 stopped at (250, 210) cm, 190 cm not searched"
        XCTAssertEqual(GroundText.fleetEventBanner(ev), base + " (obstacle sensor down)")
        var cut = ev
        cut.reason = "sensor_stale"
        XCTAssertEqual(GroundText.fleetEventBanner(cut), base + " (obstacle sensor could not look)")
        cut.reason = "autopilot"
        XCTAssertEqual(GroundText.fleetEventBanner(cut), base + " (stopped by the autopilot's obstacle avoidance)")
        cut.reason = "skipped"
        XCTAssertEqual(GroundText.fleetEventBanner(cut), base + " (reason: skipped)")
        cut.reason = "new_reason\u{202E}" + String(repeating: "x", count: 100)
        let unknown = GroundText.fleetEventBanner(cut) ?? ""
        XCTAssertTrue(unknown.hasPrefix(base + " (reason: new_reason "), unknown)
        XCTAssertFalse(unknown.unicodeScalars.contains("\u{202E}"))
        XCTAssertLessThan(unknown.count, base.count + 60)
        for plain in ["obstacle", "", nil] as [String?] {
            cut.reason = plain
            XCTAssertEqual(GroundText.fleetEventBanner(cut), base)
        }
    }
}
