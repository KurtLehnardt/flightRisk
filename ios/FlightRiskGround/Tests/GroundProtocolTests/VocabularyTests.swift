import XCTest
@testable import GroundProtocol

/// The nack and mode vocabularies are checked against `nack_reasons.json`. When the fixture gains
/// a reason, these tests fail, so the apps notice a protocol change.
final class VocabularyTests: XCTestCase {
    private func values(_ name: String) throws -> [String] {
        let c = try Fixtures.theCase("nack_reasons", name)
        return try XCTUnwrap(c.payload as? [String])
    }

    private func check<E: WireEnum>(_ type: E.Type, _ caseNames: [String], extras: Set<String> = [], file: StaticString = #filePath, line: UInt = #line) throws {
        var fixtureValues = Set<String>()
        for n in caseNames { fixtureValues.formUnion(try values(n)) }
        XCTAssertFalse(fixtureValues.isEmpty, file: file, line: line)
        for v in fixtureValues {
            let decoded = try JSONDecoder().decode(E.self, from: JSONEncoder().encode(v))
            XCTAssertTrue(decoded.isKnown, "\(E.self) lacks a case for fixture value \(v)", file: file, line: line)
            XCTAssertEqual(decoded.wire, v, file: file, line: line)
        }
        XCTAssertEqual(Set(E.knownWireValues), fixtureValues.union(extras), "\(E.self) differs from the fixture", file: file, line: line)
        XCTAssertEqual(Set(E.knownWireValues).count, E.knownWireValues.count, "duplicate case in \(E.self)", file: file, line: line)
        let future = try JSONDecoder().decode(E.self, from: JSONEncoder().encode("brand_new_value"))
        XCTAssertFalse(future.isKnown, file: file, line: line)
        XCTAssertEqual(future.wire, "brand_new_value", file: file, line: line)
        XCTAssertEqual(try JSONDecoder().decode(E.self, from: JSONEncoder().encode(future)), future, file: file, line: line)
    }

    func testFixtureHasExactlyTheNineKnownVocabularies() throws {
        let names = Set(try Fixtures.cases("nack_reasons").map(\.name))
        XCTAssertEqual(names, ["assign_lanes_v1", "fenced_command_v2", "set_detection_profile", "profile_fallback",
                               "set_ceiling", "manual_command", "control_modes", "hold_reasons", "edge_states"],
                       "a vocabulary was added or removed; map it to an enum here")
    }

    func testNackReasonCoversAssignFencedAndManualVocabularies() throws {
        // `no_reply` is reported by the ground in manual_result and is not in the fixture.
        try check(NackReason.self, ["assign_lanes_v1", "fenced_command_v2", "manual_command"], extras: ["no_reply"])
        for sub in ["assign_lanes_v1", "fenced_command_v2"] {
            XCTAssertTrue(Set(try values(sub)).isSubset(of: Set(try values("manual_command"))), sub)
        }
    }

    func testProfileRefusalReasons() throws { try check(ProfileRefusalReason.self, ["set_detection_profile"]) }
    func testCeilingRefusalReasons() throws { try check(CeilingRefusalReason.self, ["set_ceiling"]) }
    func testProfileFallbackReasons() throws { try check(ProfileFallbackReason.self, ["profile_fallback"]) }
    func testControlModes() throws { try check(ControlMode.self, ["control_modes"]) }
    func testHoldReasons() throws { try check(HoldReason.self, ["hold_reasons"]) }
    func testEdgeStates() throws { try check(EdgeState.self, ["edge_states"]) }

    func testUnknownNackReasonInsideAPayloadIsNotAFailure() throws {
        let r = try JSONDecoder().decode(ManualReply.self, from: Data(#"{"ok":false,"reason":"rotor_fault","drone_id":"d"}"#.utf8))
        XCTAssertEqual(r.reason, .unknown("rotor_fault"))
        XCTAssertEqual(GroundText.nackReasonText(.unknown("rotor_fault")), "rotor_fault")
        XCTAssertEqual(GroundText.manualRefusal(r), "d: rotor_fault")
    }

    func testOtherVocabulariesKeepTheirWireValues() {
        XCTAssertEqual(ManualAction.knownWireValues.sorted(), ["land", "resume_search"])
        XCTAssertEqual(ReasoningProviderName.knownWireValues.sorted(), ["claude", "gemma", "off"])
        XCTAssertEqual(FleetEventKind.knownWireValues.count, 9)
        XCTAssertEqual(LaneDirection(wire: "up"), .unknown("up"))
    }

    func testEveryNackReasonHasReadableText() throws {
        for v in NackReason.knownWireValues {
            let text = GroundText.nackReasonText(NackReason(wire: v))
            XCTAssertFalse(text.isEmpty, v)
            XCTAssertNotEqual(text, v, "\(v) has no readable text")
        }
    }
}
