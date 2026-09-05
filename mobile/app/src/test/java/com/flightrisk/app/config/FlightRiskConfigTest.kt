package com.flightrisk.app.config

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotSame
import org.junit.Test

/**
 * Unit tests for [FlightRiskConfig] verifying Python parity and
 * data-class behavior.
 *
 * These tests do NOT require an Android device or emulator -- they
 * run as plain JVM unit tests via `./gradlew test`.
 */
class FlightRiskConfigTest {

    // ------------------------------------------------------------------
    // Default values must match Python's flightrisk/config.py exactly
    // ------------------------------------------------------------------

    @Test
    fun `default VisionConfig matches Python VisionConfig`() {
        val v = VisionConfig()
        assertEquals("yolo11n.pt", v.detectorModel)
        assertEquals(0.4, v.detectorConfidence, 1e-9)
        assertEquals(0.55, v.reidThreshold, 1e-9)
        assertEquals("ViT-B-16", v.reidModel)
        assertEquals(Pair(640, 640), v.faceDetSize)
        assertEquals(0.45, v.faceMatchThreshold, 1e-9)
        assertEquals(0.45, v.scorerMatchThreshold, 1e-9)
        assertEquals(0.35, v.scorerReidWeight, 1e-9)
        assertEquals(0.40, v.scorerFaceWeight, 1e-9)
        assertEquals(0.25, v.scorerReasoningWeight, 1e-9)
        assertEquals(0.3, v.trackerIouThreshold, 1e-9)
        assertEquals(15, v.trackerMaxMissing)
        assertEquals(8, v.trackerScoreWindow)
    }

    @Test
    fun `default ReasoningConfig matches Python ReasoningConfig`() {
        val r = ReasoningConfig()
        assertEquals("gemma4:latest", r.model)
        assertEquals(10, r.queueMaxSize)
        assertEquals(10.0, r.alertCooldown, 1e-9)
        assertEquals(5.0, r.gemmaRateLimit, 1e-9)
        assertEquals(50, r.spatialGridSize)
        assertEquals(1.0, r.trackUpdateInterval, 1e-9)
        assertEquals(5.0, r.reasoningInterval, 1e-9)
        assertEquals(10.0, r.metricsInterval, 1e-9)
        assertEquals(3, r.corroborationThreshold)
    }

    @Test
    fun `default DroneConfig matches Python DroneConfig`() {
        val d = DroneConfig()
        assertEquals(5.0, d.autoConnectInterval, 1e-9)
        assertEquals(20, d.batteryWarnThreshold)
        assertEquals(10, d.batteryCriticalThreshold)
        assertEquals("192.168.10.1", d.telloDefaultHost)
        assertEquals("udp://:14540", d.mavlinkDefaultAddress)
        assertEquals(30.0, d.mavlinkCmdTimeout, 1e-9)
    }

    // ------------------------------------------------------------------
    // Sensitivity presets
    // ------------------------------------------------------------------

    @Test
    fun `MORE_ALERTS preset lowers thresholds`() {
        val cfg = FlightRiskConfig.fromPreset(SensitivityPreset.MORE_ALERTS)
        assertEquals(0.40, cfg.vision.reidThreshold, 1e-9)
        assertEquals(0.35, cfg.vision.scorerMatchThreshold, 1e-9)
        assertEquals(0.30, cfg.vision.faceMatchThreshold, 1e-9)
    }

    @Test
    fun `BALANCED preset matches defaults`() {
        val cfg = FlightRiskConfig.fromPreset(SensitivityPreset.BALANCED)
        val defaults = FlightRiskConfig()
        assertEquals(defaults.vision.reidThreshold, cfg.vision.reidThreshold, 1e-9)
        assertEquals(defaults.vision.scorerMatchThreshold, cfg.vision.scorerMatchThreshold, 1e-9)
        assertEquals(defaults.vision.faceMatchThreshold, cfg.vision.faceMatchThreshold, 1e-9)
    }

    @Test
    fun `FEWER_ALERTS preset raises thresholds`() {
        val cfg = FlightRiskConfig.fromPreset(SensitivityPreset.FEWER_ALERTS)
        assertEquals(0.70, cfg.vision.reidThreshold, 1e-9)
        assertEquals(0.60, cfg.vision.scorerMatchThreshold, 1e-9)
        assertEquals(0.55, cfg.vision.faceMatchThreshold, 1e-9)
    }

    @Test
    fun `preset thresholds are ordered MORE less than BALANCED less than FEWER`() {
        val more = SensitivityPreset.MORE_ALERTS
        val balanced = SensitivityPreset.BALANCED
        val fewer = SensitivityPreset.FEWER_ALERTS

        assert(more.reidThreshold < balanced.reidThreshold)
        assert(balanced.reidThreshold < fewer.reidThreshold)

        assert(more.scorerMatchThreshold < balanced.scorerMatchThreshold)
        assert(balanced.scorerMatchThreshold < fewer.scorerMatchThreshold)

        assert(more.faceMatchThreshold < balanced.faceMatchThreshold)
        assert(balanced.faceMatchThreshold < fewer.faceMatchThreshold)
    }

    // ------------------------------------------------------------------
    // Data class behavior
    // ------------------------------------------------------------------

    @Test
    fun `FlightRiskConfig is a proper data class with equals`() {
        val a = FlightRiskConfig()
        val b = FlightRiskConfig()
        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
    }

    @Test
    fun `FlightRiskConfig copy produces independent instance`() {
        val original = FlightRiskConfig()
        val modified = original.copy(
            vision = original.vision.copy(reidThreshold = 0.99)
        )
        assertNotEquals(original, modified)
        assertNotSame(original, modified)
        assertEquals(0.99, modified.vision.reidThreshold, 1e-9)
        // Original is unchanged
        assertEquals(0.55, original.vision.reidThreshold, 1e-9)
    }

    @Test
    fun `VisionConfig copy preserves unmodified fields`() {
        val v = VisionConfig()
        val modified = v.copy(reidThreshold = 0.80)
        assertEquals(0.80, modified.reidThreshold, 1e-9)
        // Everything else stays the same
        assertEquals(v.detectorModel, modified.detectorModel)
        assertEquals(v.detectorConfidence, modified.detectorConfidence, 1e-9)
        assertEquals(v.scorerReidWeight, modified.scorerReidWeight, 1e-9)
        assertEquals(v.trackerMaxMissing, modified.trackerMaxMissing)
    }

    // ------------------------------------------------------------------
    // Scorer weights sum to 1.0
    // ------------------------------------------------------------------

    @Test
    fun `default scorer weights sum to 1_0`() {
        val v = VisionConfig()
        val sum = v.scorerReidWeight + v.scorerFaceWeight + v.scorerReasoningWeight
        assertEquals(1.0, sum, 1e-9)
    }
}
