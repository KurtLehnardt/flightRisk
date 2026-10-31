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
        XCTAssertEqual(s.accessibilitySummary, "Search plan: 2 zones, 1 lost")
    }

    func testUncoveredAndSkippedAreReportedWithoutJudgement() throws {
        let json = #"{"zones":[{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"up","distance_cm":3}]}],"uncovered":[{"kind":"area","x_cm":0,"y_cm":0,"w_cm":5,"h_cm":5}]}"#
        let s = PlanGridViewState(plan: try plan(json), isStale: false)
        XCTAssertEqual(PlanGridViewState.uncoveredLegend, "Not searched")
        XCTAssertTrue(s.accessibilitySummary.contains("not searched"))
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
}
