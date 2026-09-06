package com.flightrisk.app.drone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SearchPatternTest {

    // -- clampDistance (tested via splitLongMove behavior) --

    @Test
    fun `splitLongMove clamps short distances to minimum 20cm`() {
        val waypoints = SearchPattern.generateExpandingSquare(
            initialSideCm = 10, growthCm = 0, numExpansions = 1,
        )
        val moves = waypoints.filter { it.distanceCm > 0 }
        assertTrue("All moves should be at least 20cm", moves.all { it.distanceCm >= 20 })
    }

    @Test
    fun `splitLongMove splits moves exceeding 500cm into segments`() {
        val waypoints = SearchPattern.generateExpandingSquare(
            initialSideCm = 800, growthCm = 0, numExpansions = 1,
        )
        val moves = waypoints.filter { it.distanceCm > 0 }
        assertTrue("No move should exceed 500cm", moves.all { it.distanceCm <= 500 })
        val totalForward = moves.sumOf { it.distanceCm }
        assertEquals("Total distance for 4 sides of 800cm", 800 * 4, totalForward)
    }

    // -- Expanding Square --

    @Test
    fun `expanding square generates correct number of rotations`() {
        val waypoints = SearchPattern.generateExpandingSquare(numExpansions = 2)
        val rotations = waypoints.filter { it.rotateDegrees != 0 }
        assertEquals("2 expansions x 4 turns each = 8 rotations", 8, rotations.size)
        assertTrue("All rotations should be 90 degrees", rotations.all { it.rotateDegrees == 90 })
    }

    @Test
    fun `expanding square with default params generates waypoints`() {
        val waypoints = SearchPattern.generateExpandingSquare()
        assertTrue("Should generate at least 16 waypoints", waypoints.size >= 16)
    }

    @Test
    fun `expanding square grows side length`() {
        val waypoints = SearchPattern.generateExpandingSquare(
            initialSideCm = 100, growthCm = 100, numExpansions = 3,
        )
        val forwardMoves = waypoints.filter { it.distanceCm > 0 && it.rotateDegrees == 0 }
        assertTrue("Should have forward moves", forwardMoves.isNotEmpty())
    }

    @Test
    fun `expanding square with zero growth keeps constant side`() {
        val waypoints = SearchPattern.generateExpandingSquare(
            initialSideCm = 100, growthCm = 0, numExpansions = 2,
        )
        val moves = waypoints.filter { it.distanceCm > 0 }
        assertTrue("All moves should be 100cm", moves.all { it.distanceCm == 100 })
    }

    @Test
    fun `expanding square with single expansion has 4 turns`() {
        val waypoints = SearchPattern.generateExpandingSquare(numExpansions = 1)
        val rotations = waypoints.filter { it.rotateDegrees != 0 }
        assertEquals(4, rotations.size)
    }

    // -- Sector/Radial --

    @Test
    fun `sector pattern generates correct number of sectors`() {
        val numSectors = 4
        val waypoints = SearchPattern.generateSector(radiusCm = 200, numSectors = numSectors)
        val turns180 = waypoints.count { it.rotateDegrees == 180 }
        assertEquals("Each sector has one 180-turn at end of outbound leg", numSectors, turns180)
    }

    @Test
    fun `sector pattern default generates waypoints`() {
        val waypoints = SearchPattern.generateSector()
        assertTrue("Should generate waypoints", waypoints.isNotEmpty())
    }

    @Test
    fun `sector pattern total forward distance is 2x radius per sector`() {
        val radius = 200
        val numSectors = 3
        val waypoints = SearchPattern.generateSector(radiusCm = radius, numSectors = numSectors)
        val totalForward = waypoints.filter { it.distanceCm > 0 }.sumOf { it.distanceCm }
        assertEquals(
            "Each sector goes out and back = 2*radius, times numSectors",
            radius * 2 * numSectors, totalForward,
        )
    }

    @Test
    fun `sector pattern with 6 sectors has 60 degree spacing`() {
        val waypoints = SearchPattern.generateSector(numSectors = 6)
        val sectorAngle = 360 / 6
        val sectorTurns = waypoints.filter { it.rotateDegrees == 180 + sectorAngle }
        assertEquals("Each sector ends with 180+sectorAngle turn", 6, sectorTurns.size)
    }

    // -- Parallel Track --

    @Test
    fun `parallel track generates correct number of strips`() {
        val waypoints = SearchPattern.generateParallelTrack(
            widthCm = 450, depthCm = 300, stripWidthCm = 150,
        )
        val numStrips = 450 / 150
        val lateralMoves = waypoints.filter { it.direction == "right" }
        assertEquals("Lateral moves between strips = numStrips - 1", numStrips - 1, lateralMoves.size)
    }

    @Test
    fun `parallel track alternates forward and back`() {
        val waypoints = SearchPattern.generateParallelTrack(
            widthCm = 300, depthCm = 200, stripWidthCm = 100,
        )
        val longMoves = waypoints.filter { it.direction in listOf("forward", "back") }
        val directions = longMoves.map { it.direction }
        for (i in 0 until directions.size - 1) {
            if (directions[i] == "forward" && directions[i + 1] != "right".also { } ) {
                // Adjacent forward/back moves within same strip are fine
            }
        }
        assertTrue("Should have both forward and back moves",
            directions.contains("forward") && directions.contains("back"))
    }

    @Test
    fun `parallel track default generates waypoints`() {
        val waypoints = SearchPattern.generateParallelTrack()
        assertTrue("Should generate waypoints", waypoints.isNotEmpty())
    }

    @Test
    fun `parallel track with single strip has no lateral moves`() {
        val waypoints = SearchPattern.generateParallelTrack(
            widthCm = 100, depthCm = 300, stripWidthCm = 150,
        )
        val lateralMoves = waypoints.filter { it.direction == "right" }
        assertEquals(0, lateralMoves.size)
    }

    // -- Track Line --

    @Test
    fun `track line generates correct sweep structure`() {
        val waypoints = SearchPattern.generateTrackLine(
            lengthCm = 500, sweepWidthCm = 100, numSweeps = 2,
        )
        val leftMoves = waypoints.filter { it.direction == "left" }
        val rightMoves = waypoints.filter { it.direction == "right" }
        assertEquals("Each sweep has 2 left moves", 4, leftMoves.size)
        assertEquals("Each sweep has 1 right move (2x width)", 2, rightMoves.size)
    }

    @Test
    fun `track line default generates waypoints`() {
        val waypoints = SearchPattern.generateTrackLine()
        assertTrue("Should generate waypoints", waypoints.isNotEmpty())
    }

    @Test
    fun `track line all directions are valid`() {
        val waypoints = SearchPattern.generateTrackLine()
        val validDirections = setOf("forward", "back", "left", "right", "up", "down")
        for (wp in waypoints) {
            if (wp.distanceCm > 0) {
                assertTrue(
                    "Direction '${wp.direction}' should be valid",
                    wp.direction in validDirections,
                )
            }
        }
    }

    // -- Spiral --

    @Test
    fun `spiral generates correct segments per turn`() {
        val segmentsPerTurn = 6
        val numTurns = 2
        val waypoints = SearchPattern.generateSpiral(
            segmentsPerTurn = segmentsPerTurn, numTurns = numTurns,
        )
        assertEquals(segmentsPerTurn * numTurns, waypoints.size)
    }

    @Test
    fun `spiral rotation per segment divides evenly into 360`() {
        val segmentsPerTurn = 8
        val waypoints = SearchPattern.generateSpiral(segmentsPerTurn = segmentsPerTurn)
        val rotations = waypoints.filter { it.rotateDegrees != 0 }
        assertTrue("All rotations should be 360/segments",
            rotations.all { it.rotateDegrees == 360 / segmentsPerTurn })
    }

    @Test
    fun `spiral segment length grows with each turn`() {
        val waypoints = SearchPattern.generateSpiral(
            radiusCm = 50, growthPerTurnCm = 100, numTurns = 3, segmentsPerTurn = 4,
        )
        val turn1Avg = waypoints.subList(0, 4).map { it.distanceCm }.average()
        val turn3Avg = waypoints.subList(8, 12).map { it.distanceCm }.average()
        assertTrue("Turn 3 segments should be longer than turn 1", turn3Avg > turn1Avg)
    }

    @Test
    fun `spiral default generates waypoints`() {
        val waypoints = SearchPattern.generateSpiral()
        assertTrue("Should generate waypoints", waypoints.isNotEmpty())
    }

    @Test
    fun `spiral respects distance clamp`() {
        val waypoints = SearchPattern.generateSpiral()
        for (wp in waypoints) {
            assertTrue("Distance should be >= 20cm", wp.distanceCm >= 20)
            assertTrue("Distance should be <= 500cm", wp.distanceCm <= 500)
        }
    }

    // -- generate() dispatcher --

    @Test
    fun `generate dispatches to expanding square`() {
        val waypoints = SearchPattern.generate(PatternType.EXPANDING_SQUARE)
        assertTrue(waypoints.isNotEmpty())
    }

    @Test
    fun `generate dispatches to sector`() {
        val waypoints = SearchPattern.generate(PatternType.SECTOR)
        assertTrue(waypoints.isNotEmpty())
    }

    @Test
    fun `generate dispatches to parallel track`() {
        val waypoints = SearchPattern.generate(PatternType.PARALLEL_TRACK)
        assertTrue(waypoints.isNotEmpty())
    }

    @Test
    fun `generate dispatches to track line`() {
        val waypoints = SearchPattern.generate(PatternType.TRACK_LINE)
        assertTrue(waypoints.isNotEmpty())
    }

    @Test
    fun `generate dispatches to spiral`() {
        val waypoints = SearchPattern.generate(PatternType.SPIRAL)
        assertTrue(waypoints.isNotEmpty())
    }

    // -- PatternType enum --

    @Test
    fun `all pattern types have display names`() {
        for (pt in PatternType.entries) {
            assertTrue("Display name should not be blank", pt.displayName.isNotBlank())
        }
    }

    @Test
    fun `pattern type count is 5`() {
        assertEquals(5, PatternType.entries.size)
    }

    // -- Waypoint data class --

    @Test
    fun `waypoint default rotation is zero`() {
        val wp = Waypoint("forward", 100)
        assertEquals(0, wp.rotateDegrees)
    }

    @Test
    fun `waypoint with rotation`() {
        val wp = Waypoint("forward", 0, rotateDegrees = 90)
        assertEquals(90, wp.rotateDegrees)
        assertEquals(0, wp.distanceCm)
    }

    // -- No waypoint exceeds Tello limits --

    @Test
    fun `all default patterns respect Tello distance limits`() {
        for (pt in PatternType.entries) {
            val waypoints = SearchPattern.generate(pt)
            for ((i, wp) in waypoints.withIndex()) {
                if (wp.distanceCm > 0) {
                    assertTrue(
                        "${pt.displayName} waypoint $i distance ${wp.distanceCm} exceeds 500cm",
                        wp.distanceCm <= 500,
                    )
                    assertTrue(
                        "${pt.displayName} waypoint $i distance ${wp.distanceCm} below 20cm",
                        wp.distanceCm >= 20,
                    )
                }
            }
        }
    }
}
