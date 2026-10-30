import XCTest
@testable import GroundProtocol

/// R-13 and R-15: the phone emits only the allow-list in `client_emits.json`.
final class CommandTests: XCTestCase {
    private func clientEmits() throws -> [Fixtures.Case] { try Fixtures.cases("client_emits") }

    private var allowed: [Fixtures.Case] {
        get throws { try clientEmits().filter { ($0.raw["allowed"] as? Bool) == true } }
    }
    private var forbidden: [Fixtures.Case] {
        get throws { try clientEmits().filter { ($0.raw["allowed"] as? Bool) == false } }
    }

    func testEmittedEventNamesEqualTheFixtureAllowListExactly() throws {
        let fixtureAllowed = Set(try allowed.compactMap(\.event))
        XCTAssertEqual(fixtureAllowed.count, 10)
        XCTAssertEqual(GroundCommand.allowedEventNames, fixtureAllowed)
        XCTAssertEqual(Set(GroundCommand.examples.map(\.eventName)), fixtureAllowed)
        XCTAssertEqual(GroundCommand.examples.count, fixtureAllowed.count, "one example per event, none repeated")
    }

    func testNoForbiddenEventCanBeProduced() throws {
        let names = try forbidden.compactMap(\.event)
        XCTAssertEqual(names.count, 14)
        for forbiddenName in names + ["match_alert_response", "set_reasoning_provider", "set_stream_video"] {
            XCTAssertFalse(GroundCommand.allowedEventNames.contains(forbiddenName), forbiddenName)
        }
        // Whatever the arguments, only allow-listed names come out.
        let wild: [GroundCommand] = [
            .landDrone(droneId: "plan_fleet"), .resumeHeldDrone(droneId: "start_fleet"),
            .setDetectionProfile(droneId: "manual_take_control", profile: "manual_command"),
            .setFleetDetectionProfile(profile: "set_ceiling_settings"),
        ]
        for c in wild { XCTAssertTrue(GroundCommand.allowedEventNames.contains(c.eventName)) }
    }

    func testTheSourceNamesNoForbiddenEvent() throws {
        let forbiddenNames = try forbidden.compactMap(\.event) + ["match_alert_response"]
        let files = try FileManager.default.contentsOfDirectory(at: Fixtures.sourcesDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        XCTAssertGreaterThanOrEqual(files.count, 6)
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            for name in forbiddenNames {
                XCTAssertFalse(text.contains("\"\(name)\""), "\(file.lastPathComponent) mentions \(name)")
            }
        }
    }

    func testEveryAllowedFixtureCaseIsReproducedByteForByte() throws {
        func command(for c: Fixtures.Case) throws -> GroundCommand {
            let p = c.payload as? [String: Any]
            switch try XCTUnwrap(c.event) {
            case "land_all": return .landAll
            case "drone_command":
                XCTAssertEqual(p?["command"] as? String, "land")
                return .landDrone(droneId: try XCTUnwrap(p?["drone_id"] as? String))
            case "manual_resume_search": return .resumeHeldDrone(droneId: try XCTUnwrap(p?["drone_id"] as? String))
            case "set_detection_profile":
                return .setDetectionProfile(droneId: try XCTUnwrap(p?["drone_id"] as? String), profile: try XCTUnwrap(p?["profile"] as? String))
            case "set_fleet_detection_profile": return .setFleetDetectionProfile(profile: try XCTUnwrap(p?["profile"] as? String))
            case "get_fleet_status": return .getFleetStatus
            case "get_detection_profiles": return .getDetectionProfiles
            case "get_ceiling_settings": return .getCeilingSettings
            case "get_reasoning_provider": return .getReasoningProvider
            case "get_manual_status": return .getManualStatus
            default: XCTFail("allow-list gained \(c.event ?? "?"); add it deliberately"); throw CocoaError(.fileReadUnknown)
            }
        }
        for c in try allowed {
            let cmd = try command(for: c)
            XCTAssertEqual(cmd.eventName, c.event)
            if let expected = c.payloadData {
                XCTAssertEqual(cmd.encodedPayload(), expected, c.name)
            } else {
                XCTAssertNil(cmd.encodedPayload(), c.name)
                XCTAssertNil(cmd.payload, c.name)
            }
        }
    }

    func testLandDroneAlwaysCarriesCommandLandAndNothingElse() throws {
        for id in ["drone-1", "drone-2", "x y"] {
            let cmd = GroundCommand.landDrone(droneId: id)
            XCTAssertEqual(cmd.eventName, "drone_command")
            XCTAssertEqual(cmd.payload, ["command": "land", "drone_id": id])
        }
    }

    func testStateRequestsAreTheFiveGetEventsInOrder() {
        XCTAssertEqual(GroundCommand.stateRequests.map(\.eventName),
                       ["get_fleet_status", "get_detection_profiles", "get_ceiling_settings",
                        "get_reasoning_provider", "get_manual_status"])
        XCTAssertTrue(GroundCommand.stateRequests.allSatisfy { $0.payload == nil })
    }

    func testEmptyProfileIsPassedThroughToFollowTheFleetDefault() {
        XCTAssertEqual(GroundCommand.setDetectionProfile(droneId: "drone-1", profile: "").payload, ["drone_id": "drone-1", "profile": ""])
        XCTAssertEqual(GroundCommand.setFleetDetectionProfile(profile: "").payload, ["profile": ""])
    }

    /// Exhaustiveness guard: adding a case without an event name or example fails to compile
    /// here, and the test then forces the allow-list comparison above to be revisited.
    func testSwitchOverEveryCaseIsExhaustive() {
        for c in GroundCommand.examples {
            switch c {
            case .landAll, .landDrone, .resumeHeldDrone, .setDetectionProfile, .setFleetDetectionProfile,
                 .getFleetStatus, .getDetectionProfiles, .getCeilingSettings, .getReasoningProvider, .getManualStatus:
                break
            }
        }
    }
}
