package com.flightrisk.contract

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The expected polylines are written out by hand, mirroring the iOS `PlanGeometryTests`, which
 * were cross-checked once against `flightrisk.drone.plan_paths.trace_lanes` on the same zones.
 */
class PlanGeometryTest {
    private fun pts(vararg pairs: Pair<Int, Int>): List<PlanPoint> = pairs.map { PlanPoint(it.first.toDouble(), it.second.toDouble()) }

    private fun plan(json: String): FleetPlan = FleetPlan.fromJson(ContractJson.parseToJsonElement(json) as kotlinx.serialization.json.JsonObject)

    private fun zone(json: String): PlanZone = PlanZone.fromJson(ContractJson.parseToJsonElement(json) as kotlinx.serialization.json.JsonObject)

    private fun box(a: Int, b: Int, c: Int, d: Int) = PlanBox(a.toDouble(), b.toDouble(), c.toDouble(), d.toDouble())

    // ---- fixtures

    @Test
    fun `two drones fixture`() {
        val d = PlanGeometry.drawing(Fixtures.eventAs<FleetPlan>("fleet_plan.json", "two_drones"))
        assertEquals(box(0, 0, 400, 400), d.grid)
        assertEquals(box(0, 0, 400, 400), d.bounds)
        assertEquals(2, d.zones.size)
        assertEquals("drone-1", d.zones[0].droneId)
        assertEquals(pts(0 to 0, 0 to 400, 75 to 400, 75 to 0), d.zones[0].points)
        assertEquals(box(0, 0, 150, 400), d.zones[0].rect)
        assertEquals("drone-2", d.zones[1].droneId)
        assertEquals(pts(250 to 0, 250 to 400, 325 to 400, 325 to 0), d.zones[1].points)
        assertEquals(box(250, 0, 400, 400), d.zones[1].rect)
        assertEquals(130.0, d.zones[1].altCm)
        assertEquals(listOf(LaneDirection.Forward, LaneDirection.Right, LaneDirection.Back), d.zones[0].segments.map { it.direction })
        assertEquals(100.0, d.gapCm)
        assertEquals(75.0, d.stripCm)
        assertFalse(d.hasUncovered)
        assertFalse(d.hasLostZone)
        assertFalse(d.isEmpty)
        assertEquals(0, d.skippedLanes)
        assertFalse(d.truncated)
    }

    @Test
    fun `lost zone and uncovered fixture`() {
        val d = PlanGeometry.drawing(Fixtures.eventAs<FleetPlan>("fleet_plan.json", "map_plan_lost_zone_uncovered"))
        assertTrue(d.hasLostZone)
        assertTrue(d.zones[0].lost)
        assertFalse(d.zones[1].lost)
        // The lost zone is still drawn, and it counts for the bounds.
        assertEquals(pts(0 to 0, 0 to 400), d.zones[0].points)
        assertEquals(pts(250 to 0, 250 to 200), d.zones[1].points)
        assertEquals(PlanPoint(0.0, 0.0), d.zones[0].start)
        assertEquals(2, d.uncoveredLanes.size)
        assertEquals(PlanPoint(0.0, 400.0), d.uncoveredLanes[0].from)
        assertEquals(PlanPoint(75.0, 400.0), d.uncoveredLanes[0].to)
        assertEquals(PlanPoint(75.0, 400.0), d.uncoveredLanes[1].from)
        assertEquals(PlanPoint(75.0, 0.0), d.uncoveredLanes[1].to)
        assertEquals("drone-1", d.uncoveredLanes[0].droneId)
        assertEquals(
            listOf(box(110, 240, 400, 400), box(290, 0, 400, 240), box(110, 0, 210, 240)),
            d.uncoveredAreas.map { it.rect },
        )
        assertTrue(d.uncoveredAreas.all { !it.reason.isNullOrEmpty() })
        assertEquals(0, d.skippedUncovered)
        assertEquals(box(0, 0, 400, 400), d.bounds)
    }

    // ---- conventions

    @Test
    fun `direction convention is forward plus y, back minus y, right plus x, left minus x`() {
        val z = zone("""{"drone_id":"drone-1","x_cm":10,"y_cm":20,"w_cm":50,"h_cm":50,"lanes":[{"direction":"forward","distance_cm":5},{"direction":"left","distance_cm":3},{"direction":"back","distance_cm":2},{"direction":"right","distance_cm":4}]}""")
        assertEquals(pts(10 to 20, 10 to 25, 7 to 25, 7 to 23, 11 to 23), PlanGeometry.zoneDrawing(z).points)
    }

    @Test
    fun `takeoff point wins over the zone corner only when both are present`() {
        val z = zone("""{"drone_id":"drone-1","x_cm":0,"y_cm":0,"w_cm":100,"h_cm":100,"takeoff_x_cm":30,"takeoff_y_cm":40,"lanes":[{"direction":"forward","distance_cm":10}]}""")
        assertEquals(pts(30 to 40, 30 to 50), PlanGeometry.zoneDrawing(z).points)
        val half = zone("""{"drone_id":"drone-1","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"takeoff_x_cm":30,"lanes":[]}""")
        assertEquals(PlanPoint(5.0, 6.0), PlanGeometry.zoneDrawing(half).start)
    }

    @Test
    fun `an offset takeoff point starts the path and stays inside the bounds`() {
        val d = PlanGeometry.drawing(
            plan(
                """{"zones":[{"drone_id":"drone-1","x_cm":100,"y_cm":0,"w_cm":150,"h_cm":400,"takeoff_x_cm":175,"takeoff_y_cm":-50,
                  "lanes":[{"direction":"forward","distance_cm":450},{"direction":"right","distance_cm":75},{"direction":"back","distance_cm":400}]}]}""",
            ),
        )
        assertEquals(pts(175 to -50, 175 to 400, 250 to 400, 250 to 0), d.zones[0].points)
        assertEquals(PlanPoint(175.0, -50.0), d.zones[0].start)
        assertEquals(box(100, -50, 250, 400), d.bounds)
    }

    @Test
    fun `zero distance lane repeats the point`() {
        val z = zone("""{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"forward","distance_cm":0},{"direction":"right","distance_cm":5}]}""")
        val d = PlanGeometry.zoneDrawing(z)
        assertEquals(pts(0 to 0, 0 to 0, 5 to 0), d.points)
        assertEquals(listOf(true, false), d.segments.map { it.isZeroLength })
        assertEquals(0, d.skippedLanes)
    }

    // ---- odd data

    @Test
    fun `unknown or missing direction is skipped and counted`() {
        val z = zone("""{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"up","distance_cm":5},{"direction":"forward","distance_cm":7},{"distance_cm":3}]}""")
        val d = PlanGeometry.zoneDrawing(z)
        assertEquals(pts(0 to 0, 0 to 7), d.points)
        assertEquals(2, d.skippedLanes)
        assertEquals(2, PlanGeometry.drawing(plan("""{"zones":[{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[{"direction":"up","distance_cm":5},{"distance_cm":3}]}]}""")).skippedLanes)
    }

    @Test
    fun `hostile numbers are skipped, never drawn`() {
        fun lane(dist: Double?) = PlanLane(LaneDirection.Forward, dist)
        var z = PlanZone(
            "d", 0.0, 0.0, 10.0, 10.0, null, null, null,
            listOf(lane(Double.NaN), lane(Double.POSITIVE_INFINITY), lane(-5.0), lane(1.0e300), lane(null), PlanLane(LaneDirection.Right, 2.0)),
            false,
        )
        val d = PlanGeometry.zoneDrawing(z)
        assertEquals(pts(0 to 0, 2 to 0), d.points)
        assertEquals(5, d.skippedLanes)

        z = z.copy(xCm = Double.NaN, yCm = Double.NEGATIVE_INFINITY)
        val noStart = PlanGeometry.zoneDrawing(z)
        assertNull(noStart.start)
        assertNull(noStart.rect)
        assertTrue(noStart.points.isEmpty())
        assertEquals(6, noStart.skippedLanes)

        val drawing = PlanGeometry.drawing(FleetPlan(listOf(z), PlanRect(Double.NaN, 0.0, 10.0, 10.0), Double.NaN, 1.0e300, emptyList()))
        assertNull(drawing.grid)
        assertEquals(PlanGeometry.fallbackBounds, drawing.bounds)
        assertNull(drawing.gapCm)
        assertNull(drawing.stripCm)
        val t = PlanGeometry.transform(drawing.bounds, Double.NaN, Double.POSITIVE_INFINITY, Double.NaN)
        assertEquals(0.0, t.scale, 0.0)
        assertTrue(t.map(PlanPoint(1.0, 1.0)).x.isFinite())
    }

    @Test
    fun `a lane that would leave the drawable range is skipped`() {
        val z = zone("""{"drone_id":"d","x_cm":9999990,"y_cm":0,"w_cm":5,"h_cm":5,"lanes":[{"direction":"right","distance_cm":100},{"direction":"left","distance_cm":4}]}""")
        val d = PlanGeometry.zoneDrawing(z)
        assertEquals(1, d.skippedLanes)
        assertEquals(PlanPoint(9999986.0, 0.0), d.points.last())
    }

    @Test
    fun `zero size and negative grids are not drawn`() {
        for (g in listOf("""{"x_cm":0,"y_cm":0,"w_cm":0,"h_cm":0}""", """{"x_cm":0,"y_cm":0,"w_cm":-5,"h_cm":10}""")) {
            val d = PlanGeometry.drawing(plan("""{"grid":$g,"zones":[{"drone_id":"d","x_cm":10,"y_cm":10,"w_cm":20,"h_cm":30,"lanes":[]}]}"""))
            assertNull(d.grid)
            assertEquals(box(10, 10, 30, 40), d.bounds)
        }
    }

    @Test
    fun `a zone outside the grid extends the bounds`() {
        val d = PlanGeometry.drawing(plan("""{"grid":{"x_cm":0,"y_cm":0,"w_cm":100,"h_cm":100},"zones":[{"drone_id":"d","x_cm":-50,"y_cm":90,"w_cm":20,"h_cm":60,"lanes":[]}]}"""))
        assertEquals(box(0, 0, 100, 100), d.grid)
        assertEquals(box(-50, 0, 100, 150), d.bounds)
    }

    // ---- caps

    @Test
    fun `thousands of lanes are capped and counted apart from unusable ones`() {
        val lane = """{"direction":"forward","distance_cm":1}"""
        val lanes = List(5000) { lane }.joinToString(",")
        val d = PlanGeometry.drawing(plan("""{"zones":[{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[$lanes]}]}"""))
        assertEquals(PlanGeometry.MAX_LANES_PER_ZONE + 1, d.zones[0].points.size)
        assertEquals(5000 - PlanGeometry.MAX_LANES_PER_ZONE, d.zones[0].droppedLanes)
        assertEquals(0, d.zones[0].skippedLanes)
        assertEquals(5000 - PlanGeometry.MAX_LANES_PER_ZONE, d.droppedLanes)
        assertEquals(0, d.skippedLanes)
        assertTrue(d.truncated)
    }

    @Test
    fun `many zones and uncovered entries are capped`() {
        val z = """{"drone_id":"d","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10,"lanes":[]}"""
        val u = """{"kind":"area","x_cm":0,"y_cm":0,"w_cm":10,"h_cm":10}"""
        val d = PlanGeometry.drawing(plan("""{"zones":[${List(200) { z }.joinToString(",")}],"uncovered":[${List(900) { u }.joinToString(",")}]}"""))
        assertEquals(PlanGeometry.MAX_ZONES, d.zones.size)
        assertEquals(PlanGeometry.MAX_UNCOVERED_ENTRIES, d.uncoveredAreas.size)
        assertTrue(d.truncated)
    }

    @Test
    fun `uncovered entry variants`() {
        val d = PlanGeometry.drawing(
            plan(
                """{"uncovered":[
                  {"index":0,"direction":"left","distance_cm":30,"x_cm":100,"y_cm":100,"drone_id":"drone-1"},
                  {"kind":"area","x_cm":0,"y_cm":0,"w_cm":0,"h_cm":5},
                  {"kind":"polygon","x_cm":0,"y_cm":0},
                  {"index":2,"direction":"sideways","distance_cm":30,"x_cm":1,"y_cm":1}
                ]}""",
            ),
        )
        assertEquals(1, d.uncoveredLanes.size)
        assertEquals(PlanPoint(70.0, 100.0), d.uncoveredLanes[0].to)
        assertTrue(d.uncoveredAreas.isEmpty())
        assertEquals(3, d.skippedUncovered)
        assertTrue(d.hasUncovered)
    }

    @Test
    fun `empty plans are empty`() {
        assertTrue(PlanGeometry.drawing(plan("{}")).isEmpty)
        assertTrue(PlanGeometry.drawing(plan("""{"grid":{"x_cm":0,"y_cm":0,"w_cm":400,"h_cm":400},"zones":[],"uncovered":[]}""")).isEmpty)
    }

    @Test
    fun `an unusable takeoff falls back to the zone corner and is reported`() {
        var z = zone("""{"drone_id":"drone-7","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"takeoff_x_cm":30,"takeoff_y_cm":40,"lanes":[{"direction":"forward","distance_cm":1}]}""")
        assertFalse(PlanGeometry.zoneDrawing(z).takeoffFellBack)
        z = z.copy(takeoffXCm = Double.NaN)
        val d = PlanGeometry.zoneDrawing(z)
        assertTrue(d.takeoffFellBack)
        assertEquals(pts(5 to 6, 5 to 7), d.points)
        z = z.copy(takeoffXCm = 1.0e12)
        assertTrue(PlanGeometry.zoneDrawing(z).takeoffFellBack)
        assertEquals(listOf("drone-7"), PlanGeometry.drawing(FleetPlan(listOf(z), null, null, null, emptyList())).takeoffFallbackDroneIds)
        val plain = zone("""{"drone_id":"d","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"lanes":[]}""")
        assertFalse(PlanGeometry.zoneDrawing(plain).takeoffFellBack)
    }

    // ---- mapping

    @Test
    fun `transform fits, pads and flips y`() {
        val t = PlanGeometry.transform(box(0, 0, 400, 400), 220.0, 220.0, 10.0)
        assertEquals(0.5, t.scale, 1e-9)
        val origin = t.map(PlanPoint(0.0, 0.0))
        assertEquals(10.0, origin.x, 1e-9)
        assertEquals(210.0, origin.y, 1e-9)
        val far = t.map(PlanPoint(400.0, 400.0))
        assertEquals(210.0, far.x, 1e-9)
        assertEquals(10.0, far.y, 1e-9)
    }

    @Test
    fun `transform centres the shorter axis`() {
        val t = PlanGeometry.transform(box(0, 0, 400, 200), 400.0, 400.0, 0.0)
        assertEquals(1.0, t.scale, 1e-9)
        assertEquals(300.0, t.map(PlanPoint(0.0, 0.0)).y, 1e-9)
        assertEquals(100.0, t.map(PlanPoint(0.0, 200.0)).y, 1e-9)
    }

    @Test
    fun `mapping round trips`() {
        val t = PlanGeometry.transform(box(-120, 30, 530, 410), 333.0, 271.0, 12.0)
        for (p in listOf(PlanPoint(-120.0, 30.0), PlanPoint(530.0, 410.0), PlanPoint(17.5, 222.25))) {
            val m = t.map(p)
            val back = t.unmap(m.x, m.y)
            assertEquals(p.x, back.x, 1e-6)
            assertEquals(p.y, back.y, 1e-6)
            assertTrue(m.x >= 12 - 1e-9 && m.x <= 333 - 12 + 1e-9)
            assertTrue(m.y >= 12 - 1e-9 && m.y <= 271 - 12 + 1e-9)
        }
    }

    @Test
    fun `degenerate bounds get a minimum extent`() {
        val t = PlanGeometry.transform(box(50, 50, 50, 50), 200.0, 200.0, 0.0)
        assertEquals(2.0, t.scale, 1e-9)
        val m = t.map(PlanPoint(50.0, 50.0))
        assertEquals(100.0, m.x, 1e-9)
        assertEquals(100.0, m.y, 1e-9)
    }

    @Test
    fun `a tiny canvas gives zero scale`() {
        val t = PlanGeometry.transform(PlanGeometry.fallbackBounds, 10.0, 10.0, 20.0)
        assertEquals(0.0, t.scale, 0.0)
        val p = t.unmap(5.0, 5.0)
        assertTrue(p.x.isFinite() && p.y.isFinite())
    }

    @Test
    fun `fitted height is clamped`() {
        val sq = box(0, 0, 400, 400)
        assertEquals(300.0, PlanGeometry.fittedHeight(sq, 300.0, 10.0, 100.0, 500.0), 1e-9)
        assertEquals(500.0, PlanGeometry.fittedHeight(sq, 900.0, 10.0, 100.0, 500.0), 0.0)
        assertEquals(100.0, PlanGeometry.fittedHeight(sq, Double.NaN, 10.0, 100.0, 500.0), 0.0)
    }
}
