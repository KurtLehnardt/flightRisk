import XCTest
@testable import GroundProtocol

/// The expected polylines are written out by hand and were cross-checked once against
/// `flightrisk.drone.plan_paths.trace_lanes` on the same fixture zones.
final class PlanGeometryTests: XCTestCase {
    private func pts(_ pairs: [(Double, Double)]) -> [PlanPoint] { pairs.map { PlanPoint(x: $0.0, y: $0.1) } }

    private func decode(_ json: String) throws -> FleetPlanPayload {
        try JSONDecoder().decode(FleetPlanPayload.self, from: Data(json.utf8))
    }

    private func zone(_ json: String) throws -> PlanZone {
        try JSONDecoder().decode(PlanZone.self, from: Data(json.utf8))
    }

    // MARK: fixtures

    func testTwoDronesFixture() throws {
        let plan = try Fixtures.decode(FleetPlanPayload.self, "fleet_plan", "two_drones")
        let d = PlanGeometry.drawing(plan)
        XCTAssertEqual(d.grid, PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400))
        XCTAssertEqual(d.bounds, PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400))
        XCTAssertEqual(d.zones.count, 2)
        XCTAssertEqual(d.zones[0].droneId, "drone-1")
        XCTAssertEqual(d.zones[0].points, pts([(0, 0), (0, 400), (75, 400), (75, 0)]))
        XCTAssertEqual(d.zones[0].rect, PlanBox(minX: 0, minY: 0, maxX: 150, maxY: 400))
        XCTAssertEqual(d.zones[1].droneId, "drone-2")
        XCTAssertEqual(d.zones[1].points, pts([(250, 0), (250, 400), (325, 400), (325, 0)]))
        XCTAssertEqual(d.zones[1].rect, PlanBox(minX: 250, minY: 0, maxX: 400, maxY: 400))
        XCTAssertEqual(d.zones[1].altCm, 130)
        XCTAssertEqual(d.zones[0].segments.map(\.direction), [.forward, .right, .back])
        XCTAssertEqual(d.gapCm, 100)
        XCTAssertEqual(d.stripCm, 75)
        XCTAssertFalse(d.hasUncovered)
        XCTAssertFalse(d.hasLostZone)
        XCTAssertFalse(d.isEmpty)
        XCTAssertEqual(d.skippedLanes, 0)
        XCTAssertFalse(d.truncated)
    }

    func testLostZoneAndUncoveredFixture() throws {
        let plan = try Fixtures.decode(FleetPlanPayload.self, "fleet_plan", "map_plan_lost_zone_uncovered")
        let d = PlanGeometry.drawing(plan)
        XCTAssertTrue(d.hasLostZone)
        XCTAssertTrue(d.zones[0].lost)
        XCTAssertFalse(d.zones[1].lost)
        // The lost zone is still drawn, and it counts for the bounds.
        XCTAssertEqual(d.zones[0].points, pts([(0, 0), (0, 400)]))
        XCTAssertEqual(d.zones[1].points, pts([(250, 0), (250, 200)]))
        XCTAssertEqual(d.zones[0].start, PlanPoint(x: 0, y: 0))
        XCTAssertEqual(d.uncoveredLanes.count, 2)
        XCTAssertEqual(d.uncoveredLanes[0].from, PlanPoint(x: 0, y: 400))
        XCTAssertEqual(d.uncoveredLanes[0].to, PlanPoint(x: 75, y: 400))
        XCTAssertEqual(d.uncoveredLanes[1].from, PlanPoint(x: 75, y: 400))
        XCTAssertEqual(d.uncoveredLanes[1].to, PlanPoint(x: 75, y: 0))
        XCTAssertEqual(d.uncoveredLanes[0].droneId, "drone-1")
        XCTAssertEqual(d.uncoveredAreas.map(\.rect), [
            PlanBox(minX: 110, minY: 240, maxX: 400, maxY: 400),
            PlanBox(minX: 290, minY: 0, maxX: 400, maxY: 240),
            PlanBox(minX: 110, minY: 0, maxX: 210, maxY: 240),
        ])
        XCTAssertEqual(d.skippedUncovered, 0)
        XCTAssertEqual(d.bounds, PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400))
    }

    // MARK: conventions

    func testDirectionConvention() throws {
        let z = try zone(#"{"drone_id":"drone-1","x_cm":10,"y_cm":20,"w_cm":50,"h_cm":50,"lanes":[{"direction":"forward","distance_cm":5},{"direction":"left","distance_cm":3},{"direction":"back","distance_cm":2},{"direction":"right","distance_cm":4}]}"#)
        let d = PlanGeometry.zoneDrawing(z)
        XCTAssertEqual(d.points, pts([(10, 20), (10, 25), (7, 25), (7, 23), (11, 23)]))
    }

    func testTakeoffPointWinsOverZoneCorner() throws {
        let z = try zone(#"{"drone_id":"drone-1","x_cm":0,"y_cm":0,"w_cm":100,"h_cm":100,"takeoff_x_cm":30,"takeoff_y_cm":40,"lanes":[{"direction":"forward","distance_cm":10}]}"#)
        XCTAssertEqual(PlanGeometry.zoneDrawing(z).points, pts([(30, 40), (30, 50)]))
        // Only one of the two takeoff values: the zone corner is used.
        let half = try zone(#"{"drone_id":"drone-1","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"takeoff_x_cm":30,"lanes":[]}"#)
        XCTAssertEqual(PlanGeometry.zoneDrawing(half).start, PlanPoint(x: 5, y: 6))
    }

    func testZeroDistanceLaneRepeatsThePoint() throws {
        let z = try zone(#"{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"forward","distance_cm":0},{"direction":"right","distance_cm":5}]}"#)
        let d = PlanGeometry.zoneDrawing(z)
        XCTAssertEqual(d.points, pts([(0, 0), (0, 0), (5, 0)]))
        XCTAssertEqual(d.segments.map(\.isZeroLength), [true, false])
        XCTAssertEqual(d.skippedLanes, 0)
    }

    // MARK: odd data

    func testUnknownDirectionIsSkippedAndCounted() throws {
        let z = try zone(#"{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"up","distance_cm":5},{"direction":"forward","distance_cm":7},{"distance_cm":3}]}"#)
        let d = PlanGeometry.zoneDrawing(z)
        XCTAssertEqual(d.points, pts([(0, 0), (0, 7)]))
        XCTAssertEqual(d.skippedLanes, 2)
        var plan = try decode("{}")
        plan.zones = [z]
        XCTAssertEqual(PlanGeometry.drawing(plan).skippedLanes, 2)
    }

    func testHostileNumbers() throws {
        var z = try zone(#"{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"forward","distance_cm":1},{"direction":"forward","distance_cm":1},{"direction":"forward","distance_cm":1},{"direction":"forward","distance_cm":1},{"direction":"right","distance_cm":2}]}"#)
        var lanes = try XCTUnwrap(z.lanes)
        lanes[0].distanceCm = .nan
        lanes[1].distanceCm = .infinity
        lanes[2].distanceCm = -5
        lanes[3].distanceCm = 1.0e300
        z.lanes = lanes
        let d = PlanGeometry.zoneDrawing(z)
        XCTAssertEqual(d.points, pts([(0, 0), (2, 0)]))
        XCTAssertEqual(d.skippedLanes, 4)

        z.xCm = .nan
        z.yCm = -.infinity
        let noStart = PlanGeometry.zoneDrawing(z)
        XCTAssertNil(noStart.start)
        XCTAssertNil(noStart.rect)
        XCTAssertTrue(noStart.points.isEmpty)

        var plan = try decode("{}")
        plan.zones = [z]
        var grid = try JSONDecoder().decode(PlanRect.self, from: Data(#"{"x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10}"#.utf8))
        grid.xCm = .nan
        plan.grid = grid
        let drawing = PlanGeometry.drawing(plan)
        XCTAssertNil(drawing.grid)
        XCTAssertEqual(drawing.bounds, PlanGeometry.fallbackBounds)
        let t = PlanGeometry.transform(bounds: drawing.bounds, canvasWidth: .nan, canvasHeight: .infinity, padding: .nan)
        XCTAssertEqual(t.scale, 0)
        XCTAssertTrue(t.map(PlanPoint(x: 1, y: 1)).x.isFinite)
    }

    func testZeroSizeAndNegativeGridAreNotDrawn() throws {
        for g in [#"{"x_cm":0,"y_cm":0,"w_cm":0,"h_cm":0}"#, #"{"x_cm":0,"y_cm":0,"w_cm":-5,"h_cm":10}"#] {
            let plan = try decode(#"{"grid":\#(g),"zones":[{"drone_id":"d","x_cm":10,"y_cm":10,"w_cm":20,"h_cm":30,"lanes":[]}]}"#)
            let d = PlanGeometry.drawing(plan)
            XCTAssertNil(d.grid)
            XCTAssertEqual(d.bounds, PlanBox(minX: 10, minY: 10, maxX: 30, maxY: 40))
        }
    }

    func testZoneOutsideTheGridExtendsTheBounds() throws {
        let plan = try decode(#"{"grid":{"x_cm":0,"y_cm":0,"w_cm":100,"h_cm":100},"zones":[{"drone_id":"d","x_cm":-50,"y_cm":90,"w_cm":20,"h_cm":60,"lanes":[]}]}"#)
        let d = PlanGeometry.drawing(plan)
        XCTAssertEqual(d.grid, PlanBox(minX: 0, minY: 0, maxX: 100, maxY: 100))
        XCTAssertEqual(d.bounds, PlanBox(minX: -50, minY: 0, maxX: 100, maxY: 150))
    }

    func testThousandsOfLanesAreCapped() throws {
        let lane = #"{"direction":"forward","distance_cm":1}"#
        let lanes = Array(repeating: lane, count: 5000).joined(separator: ",")
        let plan = try decode(#"{"zones":[{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[\#(lanes)]}]}"#)
        let d = PlanGeometry.drawing(plan)
        XCTAssertEqual(d.zones[0].points.count, PlanGeometry.maxLanesPerZone + 1)
        // Lanes cut by the cap are counted apart from unusable ones and mark the drawing as partial.
        XCTAssertEqual(d.zones[0].droppedLanes, 5000 - PlanGeometry.maxLanesPerZone)
        XCTAssertEqual(d.zones[0].skippedLanes, 0)
        XCTAssertEqual(d.droppedLanes, 5000 - PlanGeometry.maxLanesPerZone)
        XCTAssertEqual(d.skippedLanes, 0)
        XCTAssertTrue(d.truncated)
    }

    func testManyZonesAndUncoveredEntriesAreCapped() throws {
        let z = #"{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[]}"#
        let u = #"{"kind":"area","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10}"#
        let plan = try decode(#"{"zones":[\#(Array(repeating: z, count: 200).joined(separator: ","))],"uncovered":[\#(Array(repeating: u, count: 900).joined(separator: ","))]}"#)
        let d = PlanGeometry.drawing(plan)
        XCTAssertEqual(d.zones.count, PlanGeometry.maxZones)
        XCTAssertEqual(d.uncoveredAreas.count, PlanGeometry.maxUncoveredEntries)
        XCTAssertTrue(d.truncated)
    }

    func testUncoveredEntryVariants() throws {
        let plan = try decode(#"""
        {"uncovered":[
          {"index":0,"direction":"left","distance_cm":30,"x_cm":100,"y_cm":100,"drone_id":"drone-1"},
          {"kind":"area","x_cm":0,"y_cm":0,"w_cm":0,"h_cm":5},
          {"kind":"polygon","x_cm":0,"y_cm":0},
          {"index":2,"direction":"sideways","distance_cm":30,"x_cm":1,"y_cm":1}
        ]}
        """#)
        let d = PlanGeometry.drawing(plan)
        XCTAssertEqual(d.uncoveredLanes.count, 1)
        XCTAssertEqual(d.uncoveredLanes[0].to, PlanPoint(x: 70, y: 100))
        XCTAssertTrue(d.uncoveredAreas.isEmpty)
        XCTAssertEqual(d.skippedUncovered, 3)
        XCTAssertTrue(d.hasUncovered)
    }

    func testEmptyPlans() throws {
        XCTAssertTrue(PlanGeometry.drawing(try decode("{}")).isEmpty)
        XCTAssertTrue(PlanGeometry.drawing(try decode("{}")).isEmpty)
        XCTAssertTrue(PlanGeometry.drawing(try decode(#"{"grid":{"x_cm":0,"y_cm":0,"w_cm":400,"h_cm":400},"zones":[],"uncovered":[]}"#)).isEmpty)
    }

    func testUnusableTakeoffFallsBackToTheZoneCornerAndIsReported() throws {
        var z = try zone(#"{"drone_id":"drone-7","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"takeoff_x_cm":30,"takeoff_y_cm":40,"lanes":[{"direction":"forward","distance_cm":1}]}"#)
        XCTAssertFalse(PlanGeometry.zoneDrawing(z).takeoffFellBack)
        z.takeoffXCm = .nan
        let d = PlanGeometry.zoneDrawing(z)
        XCTAssertTrue(d.takeoffFellBack)
        XCTAssertEqual(d.points, pts([(5, 6), (5, 7)]))
        z.takeoffXCm = 1.0e12
        XCTAssertTrue(PlanGeometry.zoneDrawing(z).takeoffFellBack)
        var plan = try decode("{}")
        plan.zones = [z]
        XCTAssertEqual(PlanGeometry.drawing(plan).takeoffFallbackDroneIds, ["drone-7"])
        // A takeoff that is simply absent is not a fallback.
        let plain = try zone(#"{"drone_id":"d","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"lanes":[]}"#)
        XCTAssertFalse(PlanGeometry.zoneDrawing(plain).takeoffFellBack)
    }

    // MARK: mapping

    func testTransformFitsPadsAndFlipsY() {
        let b = PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400)
        let t = PlanGeometry.transform(bounds: b, canvasWidth: 220, canvasHeight: 220, padding: 10)
        XCTAssertEqual(t.scale, 0.5, accuracy: 1e-9)
        let origin = t.map(PlanPoint(x: 0, y: 0))
        XCTAssertEqual(origin.x, 10, accuracy: 1e-9)
        XCTAssertEqual(origin.y, 210, accuracy: 1e-9)
        let far = t.map(PlanPoint(x: 400, y: 400))
        XCTAssertEqual(far.x, 210, accuracy: 1e-9)
        XCTAssertEqual(far.y, 10, accuracy: 1e-9)
    }

    func testTransformCentresTheShorterAxis() {
        let b = PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 200)
        let t = PlanGeometry.transform(bounds: b, canvasWidth: 400, canvasHeight: 400, padding: 0)
        XCTAssertEqual(t.scale, 1, accuracy: 1e-9)
        XCTAssertEqual(t.map(PlanPoint(x: 0, y: 0)).y, 300, accuracy: 1e-9)
        XCTAssertEqual(t.map(PlanPoint(x: 0, y: 200)).y, 100, accuracy: 1e-9)
    }

    func testMappingRoundTrips() {
        let b = PlanBox(minX: -120, minY: 30, maxX: 530, maxY: 410)
        let t = PlanGeometry.transform(bounds: b, canvasWidth: 333, canvasHeight: 271, padding: 12)
        for p in [PlanPoint(x: -120, y: 30), PlanPoint(x: 530, y: 410), PlanPoint(x: 17.5, y: 222.25)] {
            let m = t.map(p)
            let back = t.unmap(x: m.x, y: m.y)
            XCTAssertEqual(back.x, p.x, accuracy: 1e-6)
            XCTAssertEqual(back.y, p.y, accuracy: 1e-6)
            XCTAssertGreaterThanOrEqual(m.x, 12 - 1e-9)
            XCTAssertLessThanOrEqual(m.x, 333 - 12 + 1e-9)
            XCTAssertGreaterThanOrEqual(m.y, 12 - 1e-9)
            XCTAssertLessThanOrEqual(m.y, 271 - 12 + 1e-9)
        }
    }

    func testDegenerateBoundsGetAMinimumExtent() {
        let b = PlanBox(minX: 50, minY: 50, maxX: 50, maxY: 50)
        let t = PlanGeometry.transform(bounds: b, canvasWidth: 200, canvasHeight: 200, padding: 0)
        XCTAssertTrue(t.scale.isFinite)
        XCTAssertEqual(t.scale, 2, accuracy: 1e-9)
        let m = t.map(PlanPoint(x: 50, y: 50))
        XCTAssertEqual(m.x, 100, accuracy: 1e-9)
        XCTAssertEqual(m.y, 100, accuracy: 1e-9)
    }

    func testTinyCanvasGivesZeroScale() {
        let t = PlanGeometry.transform(bounds: PlanGeometry.fallbackBounds, canvasWidth: 10, canvasHeight: 10, padding: 20)
        XCTAssertEqual(t.scale, 0)
        let p = t.unmap(x: 5, y: 5)
        XCTAssertTrue(p.x.isFinite && p.y.isFinite)
    }

    func testFittedHeightIsClamped() {
        let sq = PlanBox(minX: 0, minY: 0, maxX: 400, maxY: 400)
        XCTAssertEqual(PlanGeometry.fittedHeight(bounds: sq, canvasWidth: 300, padding: 10, minHeight: 100, maxHeight: 500), 300, accuracy: 1e-9)
        XCTAssertEqual(PlanGeometry.fittedHeight(bounds: sq, canvasWidth: 900, padding: 10, minHeight: 100, maxHeight: 500), 500)
        XCTAssertEqual(PlanGeometry.fittedHeight(bounds: sq, canvasWidth: .nan, padding: 10, minHeight: 100, maxHeight: 500), 100)
    }
}
