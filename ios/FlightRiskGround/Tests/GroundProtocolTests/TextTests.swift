import XCTest
@testable import GroundProtocol

final class TextTests: XCTestCase {
    func testLandAllResults() throws {
        let none = GroundText.landAllResult(try Fixtures.decode(LandAllAck.self, "land_all_ack", "reached_no_drone"))
        XCTAssertTrue(none.isFailure)
        XCTAssertEqual(none.text, "LAND ALL FAILED: no drone was reached. Land each drone by hand (per-drone Land, or the drone's own controls).")
        let all = GroundText.landAllResult(try Fixtures.decode(LandAllAck.self, "land_all_ack", "fleet_reached_all"))
        XCTAssertFalse(all.isFailure)
        XCTAssertEqual(all.text, "LAND ALL reached drones (coordinator 2, transport 2)")
        let local = GroundText.landAllResult(try Fixtures.decode(LandAllAck.self, "land_all_ack", "local_fleet"))
        XCTAssertEqual(local.text, "LAND ALL reached drones (local 1)")
        // The server's own error text is the same sentence.
        let err = try Fixtures.decode(ErrorPayload.self, "error", "land_all_reached_no_drone")
        XCTAssertEqual(err.message, GroundText.landAllReachedNoDrone)
    }

    func testPlanRefusalMessagesCarryTheGroundDetailVerbatim() throws {
        let unconnected = try Fixtures.decode(FleetEvent.self, "fleet_event", "plan_refused_unconnected")
        XCTAssertEqual(GroundText.fleetRefusal(unconnected), "Plan refused: " + (unconnected.detail ?? ""))
        XCTAssertTrue(GroundText.fleetRefusal(unconnected)!.hasPrefix("Plan refused: drone-2: not connected."))
        XCTAssertEqual(GroundText.fleetEventBanner(unconnected), "PLAN REFUSED")
        let manual = try Fixtures.decode(FleetEvent.self, "fleet_event", "plan_refused_manual_control")
        XCTAssertEqual(GroundText.fleetRefusal(manual), "Plan refused: drone-2 under manual control; press Resume search first")
        let start = try Fixtures.decode(FleetEvent.self, "fleet_event", "start_refused_drone_unreachable")
        XCTAssertEqual(GroundText.fleetRefusal(start), "Start refused: drone-2: Tello unreachable (no state for 12 s)")
        XCTAssertEqual(GroundText.fleetEventBanner(start), "start_refused")
        let bare = try JSONDecoder().decode(FleetEvent.self, from: Data(#"{"event":"plan_refused"}"#.utf8))
        XCTAssertEqual(GroundText.fleetRefusal(bare), "Plan refused: unknown")
        XCTAssertNil(GroundText.fleetRefusal(try Fixtures.decode(FleetEvent.self, "fleet_event", "start")))
    }

    func testFleetEventBannersMatchTheDashboard() throws {
        func banner(_ name: String) throws -> String? {
            GroundText.fleetEventBanner(try Fixtures.decode(FleetEvent.self, "fleet_event", name))
        }
        XCTAssertEqual(try banner("drone_lost"), "DRONE LOST: drone-2")
        XCTAssertEqual(try banner("land_all"), "LAND ALL sent")
        XCTAssertEqual(try banner("land_all_failed"), "LAND ALL sent", "plain banner text; use fleetEventBannerInfo for the failure")
        let failed = try XCTUnwrap(GroundText.fleetEventBannerInfo(try Fixtures.decode(FleetEvent.self, "fleet_event", "land_all_failed")))
        XCTAssertTrue(failed.isFailure)
        XCTAssertEqual(failed.text, "LAND ALL FAILED: failed: drone-2. Land each drone by hand (per-drone Land, or the drone's own controls).")
        XCTAssertEqual(try banner("plan_invalidated"), "PLAN EDITED: press Plan search again")
        XCTAssertEqual(try banner("start"), "start")
        XCTAssertEqual(try banner("retask"), "RETASK: drone-2 no lanes accepted yet; 2 lanes awaiting drone-1")
        XCTAssertEqual(try banner("retask_failed"), "RETASK FAILED: 2 lanes of drone-1 uncovered: refused by drone-2 (geofence)")
        XCTAssertTrue(try XCTUnwrap(try banner("profile_switch_pending")).hasPrefix("WARNING: detection profile switch still pending"))
    }

    func testManualRefusalText() throws {
        XCTAssertNil(GroundText.manualRefusal(try Fixtures.decode(ManualAck.self, "manual_ack", "land_ok")))
        XCTAssertEqual(GroundText.manualRefusal(try Fixtures.decode(ManualAck.self, "manual_ack", "land_unknown_drone")),
                       "drone-9: 'drone-9' is not in the fleet")
        XCTAssertEqual(GroundText.manualRefusal(try Fixtures.decode(ManualResult.self, "manual_result", "land_no_reply")),
                       "drone-2: no answer from drone-2 within 4 s")
    }

    func testProfileStatusLines() throws {
        let refused = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "refused")
        let r = GroundText.profileStatus(try XCTUnwrap(refused.drones?["drone-1"]), in: refused)
        XCTAssertEqual(r.severity, .bad)
        XCTAssertEqual(r.text, "MISMATCH: chosen Top-down search (HERIDAL), edge runs Startup default (yolo11n). Refused in flight; sent again when the drone is on the ground.")
        let pending = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "pending")
        XCTAssertEqual(GroundText.profileStatus(try XCTUnwrap(pending.drones?["drone-1"]), in: pending).text, "Switching to Top-down search (HERIDAL)...")
        let fb = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "fallback")
        let f = GroundText.profileStatus(try XCTUnwrap(fb.drones?["drone-1"]), in: fb)
        XCTAssertEqual(f.severity, .warn)
        XCTAssertEqual(f.text, "Active: Startup default (yolo11n) (fallback: Top-down search (HERIDAL) weights missing on the edge).")
        let un = try Fixtures.decode(DetectionProfilesPayload.self, "detection_profiles", "unsupported")
        XCTAssertTrue(GroundText.profileStatus(try XCTUnwrap(un.drones?["drone-2"]), in: un).text.hasPrefix("This edge does not report detection profiles"))
        XCTAssertEqual(GroundText.profileStatus(try XCTUnwrap(un.drones?["drone-1"]), in: un).text, "Active: Startup default (yolo11n).")
    }

    func testConnectionStrings() {
        XCTAssertEqual(GroundText.landAllSending, "LAND ALL sending\u{2026}")
        XCTAssertEqual(GroundText.landAllNoReplyYet, "LAND ALL: no reply from the ground yet. Check the connection.")
        XCTAssertTrue(GroundText.landAllNotSent.hasPrefix("LAND ALL was not sent"))
        XCTAssertTrue(GroundText.landAllNotSent.contains("Shift+L"))
    }
}
