import XCTest
@testable import GroundProtocol

/// R-9: drone state derivation, one test per dashboard rule (ui.js connState, modeText,
/// manualDisabledReason).
final class DerivationTests: XCTestCase {
    private func fleet(_ name: String) throws -> FleetStatusPayload {
        try Fixtures.decode(FleetStatusPayload.self, "fleet_status", name)
    }

    private func record(_ json: String) throws -> DroneRecord {
        try JSONDecoder().decode(DroneRecord.self, from: Data(json.utf8))
    }

    private func card(_ s: FleetStatusPayload, _ id: String) throws -> DroneCardState {
        DroneCardState(id: id, record: try XCTUnwrap(s.drones?[id]), manualControlled: s.manualControlled)
    }

    // MARK: connection state, rule by rule

    func testLinkRule1LostBeatsEverything() {
        XCTAssertEqual(DroneLinkState(lost: true, connected: true), .lost)
        XCTAssertEqual(DroneLinkState(lost: true, connected: false), .lost)
        XCTAssertEqual(DroneLinkState(lost: true, connected: nil), .lost)
    }

    func testLinkRule2ConnectedFalseIsDown() {
        XCTAssertEqual(DroneLinkState(lost: false, connected: false), .down)
        XCTAssertEqual(DroneLinkState(lost: nil, connected: false), .down)
    }

    func testLinkRule3ConnectedTrueIsConnected() {
        XCTAssertEqual(DroneLinkState(lost: false, connected: true), .connected)
        XCTAssertEqual(DroneLinkState(lost: nil, connected: true), .connected)
    }

    func testLinkRule4OtherwiseUnknown() {
        XCTAssertEqual(DroneLinkState(lost: nil, connected: nil), .unknown)
        XCTAssertEqual(DroneLinkState(lost: false, connected: nil), .unknown)
    }

    func testLinkTextAndKeysMatchTheDashboard() {
        XCTAssertEqual(DroneLinkState.connected.text, "CONNECTED")
        XCTAssertEqual(DroneLinkState.down.text, "DOWN")
        XCTAssertEqual(DroneLinkState.lost.text, "LOST")
        XCTAssertEqual(DroneLinkState.unknown.text, "--")
        XCTAssertEqual([DroneLinkState.connected, .down, .lost, .unknown].map(\.key), ["connected", "down", "lost", ""])
        XCTAssertEqual([DroneLinkState.connected, .down, .lost, .unknown].map(\.isStale), [false, true, true, false])
    }

    // MARK: connection state, fixture cases

    func testFixtureAllConnected() throws {
        let s = try fleet("all_connected")
        for id in s.sortedDroneIds {
            let c = try card(s, id)
            XCTAssertEqual(c.link, .connected, id)
            XCTAssertEqual(c.control, .autonomous(.onGround), id)
            XCTAssertEqual(c.control.text, "On the ground")
            XCTAssertNil(c.linkNotice)
            XCTAssertNil(c.staleNote)
        }
    }

    func testFixtureOneDown() throws {
        let s = try fleet("one_down")
        XCTAssertEqual(try card(s, "drone-1").link, .connected)
        let down = try card(s, "drone-2")
        XCTAssertEqual(down.link, .down)
        XCTAssertEqual(down.staleNote, "No live link: values are the last ones received")
        XCTAssertNil(down.linkNotice, "drone_link_ok is true, so no drone link notice")
    }

    func testFixtureOneLost() throws {
        let s = try fleet("one_lost")
        let lost = try card(s, "drone-2")
        XCTAssertEqual(lost.link, .lost, "connected is true but lost wins")
        XCTAssertEqual(s.drones?["drone-2"]?.connected, true)
        XCTAssertNotNil(lost.staleNote)
        XCTAssertEqual(try card(s, "drone-1").laneText, "0/3")
        XCTAssertEqual(lost.laneText, "1/3")
    }

    func testFixtureNonFleetShapesAreUnknownLink() throws {
        let s = try fleet("non_fleet_local_drone")
        let c = try card(s, "drone-1")
        XCTAssertEqual(c.link, .unknown, "is_connected is not connected; the legacy shape has neither key")
        XCTAssertEqual(c.link.text, "--")
        XCTAssertNil(c.staleNote)
        XCTAssertEqual(c.control, .autonomous(.onGround))
        XCTAssertEqual(c.batteryText, "100%")
        XCTAssertEqual(c.altitudeText, "0 cm", "legacy height 0 shows 0, not a dash")
        XCTAssertNil(c.laneText)
        XCTAssertNil(c.linkNotice)
        let none = try fleet("non_fleet_no_drone")
        XCTAssertTrue(none.sortedDroneIds.isEmpty)
        XCTAssertTrue(none.isLegacyShape)
    }

    // MARK: control state, rule by rule

    func testControlRule1OrphanCountdownWinsOverEverything() throws {
        let r = try record(#"{"orphan_land_in_s":120,"control_mode":"manual","edge_control_mode":"held","is_flying":true}"#)
        let s = ControlState(id: "drone-2", record: r, manualControlled: "drone-2")
        XCTAssertEqual(s, .held(orphanLandInS: 120))
        XCTAssertEqual(s.dashboardText, "HELD: auto-landing in 120 s, no controller")
        XCTAssertEqual(s.text, "HELD: auto-landing in 120 s, no controller")
        XCTAssertEqual(s.orphanLandInS, 120)
        XCTAssertTrue(s.isHeld)
        let zero = try record(#"{"orphan_land_in_s":0}"#)
        XCTAssertEqual(ControlState(id: "a", record: zero, manualControlled: nil), .held(orphanLandInS: 0), "0 is a number")
    }

    func testControlRule2ManualControlledDroneIsManualUnderYourControl() throws {
        let r = try record(#"{"control_mode":"manual","is_flying":true}"#)
        let s = ControlState(id: "drone-2", record: r, manualControlled: "drone-2")
        XCTAssertEqual(s, .manual(underOperatorControl: true))
        XCTAssertEqual(s.dashboardText, "MANUAL: under your control")
        XCTAssertEqual(s.text, "MANUAL", "a phone never controls a drone; the laptop does")
        let other = ControlState(id: "drone-1", record: r, manualControlled: "drone-2")
        XCTAssertEqual(other, .manual(underOperatorControl: false))
        let heldAndControlled = try record(#"{"control_mode":"held"}"#)
        XCTAssertEqual(ControlState(id: "drone-2", record: heldAndControlled, manualControlled: "drone-2"),
                       .manual(underOperatorControl: true), "controlled beats held")
        XCTAssertEqual(ControlState(id: "", record: r, manualControlled: ""), .manual(underOperatorControl: false),
                       "an empty id never matches")
    }

    func testControlRule3HeldFromControlModeOrEdgeControlMode() throws {
        for json in [#"{"control_mode":"held"}"#, #"{"edge_control_mode":"held"}"#,
                     #"{"control_mode":"autonomous","edge_control_mode":"held"}"#] {
            let s = ControlState(id: "d", record: try record(json), manualControlled: nil)
            XCTAssertEqual(s, .held(orphanLandInS: nil), json)
            XCTAssertEqual(s.text, "HELD: hovering, search paused")
            XCTAssertEqual(s.tileBadge, "HELD")
        }
    }

    func testControlRule4ManualModeIsManual() throws {
        let s = ControlState(id: "d", record: try record(#"{"control_mode":"manual","is_flying":true}"#), manualControlled: nil)
        XCTAssertEqual(s, .manual(underOperatorControl: false))
        XCTAssertEqual(s.text, "MANUAL")
        XCTAssertEqual(s.tileBadge, "MANUAL")
    }

    func testControlRule5FlyingOnGroundOrNothing() throws {
        XCTAssertEqual(ControlState(id: "d", record: try record(#"{"is_flying":true}"#), manualControlled: nil).text, "Flying")
        XCTAssertEqual(ControlState(id: "d", record: try record(#"{"is_flying":false}"#), manualControlled: nil).text, "On the ground")
        let none = ControlState(id: "d", record: try record(#"{}"#), manualControlled: nil)
        XCTAssertEqual(none, .autonomous(.unknown))
        XCTAssertEqual(none.text, "")
        let unknownMode = ControlState(id: "d", record: try record(#"{"control_mode":"future_mode","is_flying":true}"#), manualControlled: nil)
        XCTAssertEqual(unknownMode, .autonomous(.flying), "an unknown control_mode falls through to activity")
    }

    // MARK: control state, fixture cases

    func testFixtureOneHeldOrphanCountdown() throws {
        let s = try fleet("one_held_orphan_countdown")
        XCTAssertEqual(try card(s, "drone-1").control.text, "Flying")
        let held = try card(s, "drone-2")
        XCTAssertEqual(held.control, .held(orphanLandInS: 120))
        XCTAssertEqual(held.control.text, "HELD: auto-landing in 120 s, no controller")
        XCTAssertEqual(held.link, .connected)
    }

    func testFixtureOneManual() throws {
        let s = try fleet("one_manual")
        XCTAssertEqual(s.manualControlled, "drone-2")
        let m = try card(s, "drone-2")
        XCTAssertEqual(m.control, .manual(underOperatorControl: true))
        XCTAssertEqual(m.control.dashboardText, "MANUAL: under your control")
        XCTAssertEqual(m.control.text, "MANUAL")
        XCTAssertEqual(try card(s, "drone-1").control.text, "Flying")
    }

    func testFixtureDroneLinkDown() throws {
        let s = try fleet("drone_link_down")
        XCTAssertNil(try card(s, "drone-1").linkNotice)
        let d = try card(s, "drone-2")
        XCTAssertEqual(d.link, .connected, "the edge is connected; only the drone link is down")
        XCTAssertEqual(d.linkNotice, "Drone link down: Tello unreachable (no state for 12 s). Land still works.")
    }

    func testDroneLinkNoticeFallbackAndAbsence() throws {
        XCTAssertEqual(GroundText.droneLinkNotice(try record(#"{"drone_link_ok":false}"#)),
                       "Drone link down: the edge cannot reach the drone. Land still works.")
        XCTAssertEqual(GroundText.droneLinkNotice(try record(#"{"drone_link_ok":false,"drone_link_detail":""}"#)),
                       "Drone link down: the edge cannot reach the drone. Land still works.")
        XCTAssertNil(GroundText.droneLinkNotice(try record(#"{"drone_link_ok":true}"#)))
        XCTAssertNil(GroundText.droneLinkNotice(try record(#"{}"#)))
        XCTAssertEqual(GroundText.controlsDisabledReason(try record(#"{"connected":false,"drone_link_ok":false}"#)),
                       "Edge disconnected: controls are off (Land still works).")
    }

    // MARK: manual_status overlay, countdown, resume

    func testReducerLaysManualStatusOverFleetStatusWhenItArrivesLater() throws {
        var r = DroneStateReducer()
        r.apply(.fleetStatus(try fleet("all_connected")))
        r.apply(.manualStatus(try Fixtures.decode(ManualStatusPayload.self, "manual_status", "held_orphan_countdown")))
        XCTAssertEqual(r.card(for: "drone-2")?.control, .held(orphanLandInS: 120))
        XCTAssertEqual(r.drones["drone-2"]?.battery, 87, "other fields are kept")
        r.apply(.manualStatus(try Fixtures.decode(ManualStatusPayload.self, "manual_status", "manual_controlled")))
        XCTAssertNil(r.drones["drone-2"]?.orphanLandInS, "a null in manual_status replaces the old value")
        XCTAssertEqual(r.manualControlled, "drone-2")
    }

    func testLocalCountdownCountsDownAndReplacesWithNewValues() {
        XCTAssertEqual(OrphanCountdown.remaining(received: 120, elapsedSeconds: 0), 120)
        XCTAssertEqual(OrphanCountdown.remaining(received: 120, elapsedSeconds: 7.9), 113)
        XCTAssertEqual(OrphanCountdown.remaining(received: 29.6, elapsedSeconds: 2), 27.6, accuracy: 1e-9)
        XCTAssertEqual(OrphanCountdown.remaining(received: 120, elapsedSeconds: 500), 0)
        XCTAssertEqual(OrphanCountdown.remaining(received: 120, elapsedSeconds: -3), 120)
    }

    func testResumeOnlyForHeldDroneNobodyControls() throws {
        let held = try record(#"{"control_mode":"held","edge_control_mode":"held"}"#)
        XCTAssertTrue(canResumeHeldDrone(held, manualControlled: nil))
        XCTAssertFalse(canResumeHeldDrone(held, manualControlled: "drone-1"))
        XCTAssertFalse(canResumeHeldDrone(try record(#"{"control_mode":"autonomous"}"#), manualControlled: nil))
        XCTAssertFalse(canResumeHeldDrone(try record(#"{"edge_control_mode":"held"}"#), manualControlled: nil),
                       "the rule reads control_mode")
        XCTAssertFalse(canResumeHeldDrone(try record(#"{}"#), manualControlled: nil))
    }

    func testNumberFormattingMatchesTheDashboardFmt() {
        XCTAssertEqual(GroundText.number(nil), "--")
        XCTAssertEqual(GroundText.number(87), "87")
        XCTAssertEqual(GroundText.number(87.04), "87")
        XCTAssertEqual(GroundText.number(87.25), "87.3")
        XCTAssertEqual(GroundText.number(0), "0")
        XCTAssertEqual(BatteryLevel(14), .bad)
        XCTAssertEqual(BatteryLevel(15), .warn)
        XCTAssertEqual(BatteryLevel(29.9), .warn)
        XCTAssertEqual(BatteryLevel(30), .normal)
        XCTAssertEqual(BatteryLevel(nil), .normal)
    }
}
