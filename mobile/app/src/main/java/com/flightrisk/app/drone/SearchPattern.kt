package com.flightrisk.app.drone

import kotlin.math.PI
import kotlin.math.max
import kotlin.math.min

enum class PatternType(val displayName: String) {
    EXPANDING_SQUARE("Expanding Square"),
    SECTOR("Sector/Radial"),
    PARALLEL_TRACK("Parallel Track"),
    TRACK_LINE("Track Line"),
    SPIRAL("Spiral"),
}

data class Waypoint(
    val direction: String,
    val distanceCm: Int,
    val rotateDegrees: Int = 0,
)

object SearchPattern {

    private fun clampDistance(cm: Int): Int = max(20, min(500, cm))

    private fun splitLongMove(direction: String, totalCm: Int): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        var remaining = totalCm
        while (remaining > 0) {
            val segment = clampDistance(min(remaining, 500))
            waypoints.add(Waypoint(direction, segment))
            remaining -= segment
        }
        return waypoints
    }

    fun generateExpandingSquare(
        initialSideCm: Int = 100,
        growthCm: Int = 100,
        numExpansions: Int = 4,
    ): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        var side = initialSideCm

        repeat(numExpansions) {
            for (i in 0 until 4) {
                waypoints.addAll(splitLongMove("forward", side))
                waypoints.add(Waypoint("forward", 0, rotateDegrees = 90))
                if (i % 2 == 1) {
                    side += growthCm
                }
            }
        }
        return waypoints
    }

    fun generateSector(
        radiusCm: Int = 300,
        numSectors: Int = 6,
    ): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        val sectorAngle = 360 / numSectors

        repeat(numSectors) {
            waypoints.addAll(splitLongMove("forward", radiusCm))
            waypoints.add(Waypoint("forward", 0, rotateDegrees = 180))
            waypoints.addAll(splitLongMove("forward", radiusCm))
            waypoints.add(Waypoint("forward", 0, rotateDegrees = 180 + sectorAngle))
        }
        return waypoints
    }

    fun generateParallelTrack(
        widthCm: Int = 400,
        depthCm: Int = 400,
        stripWidthCm: Int = 150,
    ): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        val numStrips = max(1, widthCm / stripWidthCm)
        var goingForward = true

        for (i in 0 until numStrips) {
            val direction = if (goingForward) "forward" else "back"
            waypoints.addAll(splitLongMove(direction, depthCm))
            if (i < numStrips - 1) {
                waypoints.addAll(splitLongMove("right", stripWidthCm))
            }
            goingForward = !goingForward
        }
        return waypoints
    }

    fun generateTrackLine(
        lengthCm: Int = 500,
        sweepWidthCm: Int = 100,
        numSweeps: Int = 3,
    ): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        val segment = lengthCm / (numSweeps * 2 + 1)

        repeat(numSweeps) {
            waypoints.addAll(splitLongMove("forward", segment))
            waypoints.addAll(splitLongMove("left", sweepWidthCm))
            waypoints.addAll(splitLongMove("forward", segment))
            waypoints.addAll(splitLongMove("right", sweepWidthCm * 2))
            waypoints.addAll(splitLongMove("forward", segment))
            waypoints.addAll(splitLongMove("left", sweepWidthCm))
        }
        waypoints.addAll(splitLongMove("forward", segment))
        return waypoints
    }

    fun generateSpiral(
        radiusCm: Int = 50,
        growthPerTurnCm: Int = 100,
        numTurns: Int = 3,
        segmentsPerTurn: Int = 8,
    ): List<Waypoint> {
        val waypoints = mutableListOf<Waypoint>()
        val rotationPerSegment = 360 / segmentsPerTurn

        for (turn in 0 until numTurns) {
            val currentRadius = radiusCm + (turn * growthPerTurnCm)
            val segmentLength = clampDistance(
                (2 * PI * currentRadius / segmentsPerTurn).toInt()
            )
            repeat(segmentsPerTurn) {
                waypoints.add(Waypoint("forward", segmentLength, rotationPerSegment))
            }
        }
        return waypoints
    }

    fun generate(pattern: PatternType): List<Waypoint> = when (pattern) {
        PatternType.EXPANDING_SQUARE -> generateExpandingSquare()
        PatternType.SECTOR -> generateSector()
        PatternType.PARALLEL_TRACK -> generateParallelTrack()
        PatternType.TRACK_LINE -> generateTrackLine()
        PatternType.SPIRAL -> generateSpiral()
    }
}
