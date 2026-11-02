import Foundation
import GroundProtocol
import XCTest
@testable import FlightRisk

final class PlanGridViewStateTests: XCTestCase {
    private func plan(_ json: String) throws -> FleetPlanPayload {
        try JSONDecoder().decode(FleetPlanPayload.self, from: Data(json.utf8))
    }

    private let twoZones = #"{"grid":{"x_cm":0,"y_cm":0,"w_cm":400,"h_cm":400},"gap_cm":100,"zones":[{"drone_id":"drone-1","x_cm":0,"y_cm":0,"w_cm":150,"h_cm":400,"lanes":[{"direction":"forward","distance_cm":400}]},{"drone_id":"drone-2","x_cm":250,"y_cm":0,"w_cm":150,"h_cm":400,"lost":true,"lanes":[]}]}"#

    func testNoPlanIsTheEmptyState() {
        let s = PlanGridViewState(plan: nil, isStale: false)
        XCTAssertTrue(s.isEmpty)
        XCTAssertEqual(PlanGridViewState.emptyText, "No plan yet. Plan from the dashboard.")
        XCTAssertEqual(s.accessibilitySummary, PlanGridViewState.emptyText)
        XCTAssertEqual(s.title, "Search plan")
    }

    func testAPlanWithNothingToDrawIsEmptyToo() throws {
        let s = PlanGridViewState(plan: try plan(#"{"grid":{"x_cm":0,"y_cm":0,"w_cm":400,"h_cm":400},"zones":[],"uncovered":[]}"#), isStale: false)
        XCTAssertTrue(s.isEmpty)
    }

    func testStalePlanIsLabelledLastPlan() throws {
        let live = PlanGridViewState(plan: try plan(twoZones), isStale: false)
        let stale = PlanGridViewState(plan: try plan(twoZones), isStale: true)
        XCTAssertFalse(live.isEmpty)
        XCTAssertEqual(live.title, "Search plan")
        XCTAssertEqual(stale.title, "Last plan")
        XCTAssertTrue(stale.accessibilitySummary.hasPrefix("Last plan"))
    }

    func testNotesAndSummary() throws {
        let s = PlanGridViewState(plan: try plan(twoZones), isStale: false)
        XCTAssertEqual(s.gapNote, "Gap between zones: 100 cm")
        XCTAssertNil(s.skippedNote)
        XCTAssertEqual(s.accessibilitySummary, "Search plan: 2 zones, 1 lost. Gap between zones: 100 cm")
    }

    func testUncoveredAndSkippedAreReportedWithoutJudgement() throws {
        let json = #"{"zones":[{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"up","distance_cm":3}]}],"uncovered":[{"kind":"area","x_cm":0,"y_cm":0,"w_cm":5,"h_cm":5}]}"#
        let s = PlanGridViewState(plan: try plan(json), isStale: false)
        XCTAssertEqual(PlanGridViewState.uncoveredLegend, "Not searched")
        XCTAssertTrue(s.accessibilitySummary.contains("may not be covered (camera model)"))
        XCTAssertEqual(s.skippedNote, "1 lane not drawn (unknown direction or distance).")
    }

    func testDroneColourFollowsTheTrailingNumber() {
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: "drone-1"), 0)
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: "drone-2"), 1)
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: "drone-5"), 0)
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: "alpha"), 0)
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: ""), 0)
        XCTAssertEqual(PlanGridViewState.colorIndex(droneId: "drone-99999999999999999999"), 0)
    }

    func testAPlanNotConfirmedThisSessionIsNeverLabelledLive() throws {
        let s = PlanGridViewState(plan: try plan(twoZones), isStale: false, planIsCurrent: false)
        XCTAssertEqual(s.title, "Last plan (not confirmed this session)")
        XCTAssertTrue(s.isNotLive)
        XCTAssertTrue(s.accessibilitySummary.hasPrefix("Last plan (not confirmed this session)"))
        let live = PlanGridViewState(plan: try plan(twoZones), isStale: false, planIsCurrent: true)
        XCTAssertEqual(live.title, "Search plan")
        XCTAssertFalse(live.isNotLive)
    }

    func testCapAndTakeoffNotesAreShownAndSpoken() throws {
        let lane = #"{"direction":"forward","distance_cm":1}"#
        let lanes = Array(repeating: lane, count: PlanGeometry.maxLanesPerZone + 5).joined(separator: ",")
        var p = try plan(#"{"zones":[{"drone_id":"drone-3","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"takeoff_x_cm":1,"takeoff_y_cm":2,"lanes":[\#(lanes)]}]}"#)
        var zones = try XCTUnwrap(p.zones)
        zones[0].takeoffXCm = .infinity
        p.zones = zones
        let s = PlanGridViewState(plan: p, isStale: false)
        let note = try XCTUnwrap(s.skippedNote)
        XCTAssertTrue(note.contains("takeoff point out of range for drone-3; drawn from the zone corner"))
        XCTAssertTrue(note.contains("Only part of the plan is drawn."))
        XCTAssertFalse(note.contains("unknown direction"), "lanes cut by the cap are not unknown lanes")
        XCTAssertTrue(s.accessibilitySummary.contains("Only part of the plan is drawn."))
    }

    func testAreasAndLanesHaveSeparateNeutralLegendsAndKeepTheirReasons() throws {
        let json = #"{"uncovered":[{"index":1,"direction":"right","distance_cm":5,"x_cm":0,"y_cm":0,"end_x_cm":5,"end_y_cm":0,"reason":"lane reason"},{"kind":"area","x_cm":0,"y_cm":0,"w_cm":5,"h_cm":5,"reason":"area reason"},{"kind":"area","x_cm":9,"y_cm":9,"w_cm":5,"h_cm":5,"reason":"area reason"}]}"#
        let s = PlanGridViewState(plan: try plan(json), isStale: false)
        XCTAssertEqual(PlanGridViewState.uncoveredLegend, "Not searched")
        XCTAssertEqual(PlanGridViewState.areaLegend, "May not be covered (camera model)")
        XCTAssertEqual(s.laneReasons, ["lane reason"])
        XCTAssertEqual(s.areaReasons, ["area reason"])
        XCTAssertTrue(s.accessibilitySummary.contains("1 not searched"))
        XCTAssertTrue(s.accessibilitySummary.contains("2 may not be covered (camera model)"))
    }

    func testRepeatedStatesForTheSamePlanShareTheDrawing() throws {
        let p = try plan(twoZones)
        XCTAssertEqual(PlanGridViewState(plan: p, isStale: false).drawing, PlanGridViewState(plan: p, isStale: true).drawing)
    }
}
