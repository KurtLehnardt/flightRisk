package com.flightrisk.contract

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Field-level checks of the typed models against the fixtures. */
class ServerEventsTest {
    @Test
    fun statusCarriesFleetCount() {
        val fleet = Fixtures.eventAs<Status>("status.json", "fleet_two_drones")
        assertEquals(true, fleet.connected)
        assertEquals("edge", fleet.source)
        assertEquals(true, fleet.appearanceOnly)
        assertEquals(false, fleet.tiled)
        assertEquals(FleetSummary(drones = 2, net = "station", synced = false), fleet.fleet)
        assertTrue(fleet.isFleetMode)
        assertFalse(Fixtures.eventAs<Status>("status.json", "no_fleet_sim_source").isFleetMode)
    }

    @Test
    fun fleetStatusFleetShape() {
        val s = Fixtures.eventAs<FleetStatus.Fleet>("fleet_status.json", "all_connected")
        assertEquals(listOf("drone-1", "drone-2"), s.droneIds)
        assertEquals("station", s.net)
        assertEquals(true, s.synced)
        assertNull(s.manualControlled)
        val d = s.drones.getValue("drone-2")
        assertEquals("drone-2", d.id)
        assertEquals(87.0, d.battery)
        assertEquals(100.0, d.heightCm)
        assertEquals(130.0, d.altCm)
        assertEquals(true, d.connected)
        assertEquals(false, d.lost)
        assertEquals(0.1, d.linkAgeS)
        assertEquals(ControlMode.Autonomous, d.controlMode)
        assertEquals(ControlMode.Autonomous, d.edgeControlMode)
        assertEquals(true, d.droneLinkOk)
        assertNull(d.droneLinkDetail)
        assertNull(d.laneIndex)
        assertNull(d.orphanLandInS)
        assertEquals(250.0, d.relXCm)

        val lost = Fixtures.eventAs<FleetStatus.Fleet>("fleet_status.json", "one_lost")
        assertEquals(1, lost.drones.getValue("drone-2").laneIndex)
        assertEquals(3, lost.drones.getValue("drone-2").lanesTotal)
        assertEquals("drone-2", Fixtures.eventAs<FleetStatus.Fleet>("fleet_status.json", "one_manual").manualControlled)
    }

    @Test
    fun fleetStatusLegacyShape() {
        val one = Fixtures.eventAs<FleetStatus.Legacy>("fleet_status.json", "non_fleet_local_drone")
        assertEquals(1, one.count)
        assertEquals(
            LegacyDrone("drone-1", battery = 100.0, height = 0.0, temperature = 40.0, flightTime = 0.0, isFlying = false, isConnected = true),
            one.drones.getValue("drone-1"),
        )
        val none = Fixtures.eventAs<FleetStatus.Legacy>("fleet_status.json", "non_fleet_no_drone")
        assertEquals(0, none.count)
        assertTrue(none.drones.isEmpty())
    }

    @Test
    fun everyFleetEventCaseMapsToItsKind() {
        val expected = mapOf(
            "plan_refused_unconnected" to FleetEvent.PlanRefused::class,
            "plan_refused_manual_control" to FleetEvent.PlanRefused::class,
            "plan_invalidated" to FleetEvent.PlanInvalidated::class,
            "start" to FleetEvent.Start::class,
            "start_refused" to FleetEvent.StartRefused::class,
            "start_refused_drone_unreachable" to FleetEvent.StartRefused::class,
            "drone_lost" to FleetEvent.DroneLost::class,
            "retask" to FleetEvent.Retask::class,
            "retask_failed" to FleetEvent.RetaskFailed::class,
            "land_all" to FleetEvent.LandAll::class,
            "land_all_failed" to FleetEvent.LandAll::class,
            "profile_switch_pending" to FleetEvent.ProfileSwitchPending::class,
        )
        assertEquals(expected.keys, Fixtures.cases("fleet_event.json").keys)
        for ((case, cls) in expected) {
            val e = Fixtures.eventAs<FleetEvent>("fleet_event.json", case)
            assertEquals(case, cls, e::class)
            assertEquals(case, Fixtures.payload("fleet_event.json", case)["event"].stringValue(), e.kind)
        }
        // Every kind the ground sends today appears in the fixtures.
        assertEquals(
            FleetEvent.KNOWN_KINDS,
            expected.keys.map { Fixtures.payload("fleet_event.json", it)["event"].stringValue() }.toSet(),
        )
    }

    @Test
    fun fleetEventDetails() {
        val lost = Fixtures.eventAs<FleetEvent.DroneLost>("fleet_event.json", "drone_lost")
        assertEquals("drone-2", lost.droneId)
        assertNull(lost.detail)

        val retask = Fixtures.eventAs<FleetEvent.Retask>("fleet_event.json", "retask")
        assertEquals("drone-2", retask.droneId)
        assertTrue(retask.to.isEmpty())
        assertTrue(retask.lanes.isEmpty())
        assertEquals(2, retask.awaiting!!.size)
        assertEquals(
            RetaskLane(1, LaneDirection.Right, 75.0, 250.0, 400.0, 325.0, 400.0, "drone-1", null),
            retask.awaiting!![0],
        )

        val failed = Fixtures.eventAs<FleetEvent.RetaskFailed>("fleet_event.json", "retask_failed")
        assertEquals("drone-1", failed.droneId)
        assertEquals(listOf("refused by drone-2 (geofence)", "refused by drone-2 (geofence)"), failed.lanes.map { it.reason })

        val pending = Fixtures.eventAs<FleetEvent.ProfileSwitchPending>("fleet_event.json", "profile_switch_pending")
        assertEquals(listOf("drone-2"), pending.droneIds)

        assertEquals("failed: drone-2", Fixtures.eventAs<FleetEvent.LandAll>("fleet_event.json", "land_all_failed").detail)
    }

    @Test
    fun fleetEventKindFallsBackToTypeThenKind() {
        assertEquals(FleetEvent.Start(null), FleetEvent.fromJson(obj("""{"type":"start"}""")))
        assertEquals(FleetEvent.Start(null), FleetEvent.fromJson(obj("""{"kind":"start"}""")))
    }

    @Test
    fun unknownFleetEventKindIsKeptByName() {
        val e = ServerEvents.decode("fleet_event", """{"event":"swarm_merge","detail":"two fleets merged","drone_id":"drone-3"}""")
        assertEquals(FleetEvent.Unknown("swarm_merge", "drone-3", "two fleets merged"), e)
    }

    @Test
    fun fleetPlanTwoDrones() {
        val plan = Fixtures.eventAs<FleetPlan>("fleet_plan.json", "two_drones")
        assertEquals(PlanRect(0.0, 0.0, 400.0, 400.0), plan.grid)
        assertEquals(100.0, plan.gapCm)
        assertEquals(75.0, plan.stripCm)
        assertTrue(plan.uncovered.isEmpty())
        assertEquals(listOf("drone-1", "drone-2"), plan.zones.map { it.droneId })
        val z = plan.zones[1]
        assertEquals(250.0, z.xCm)
        assertNull(z.takeoffXCm)
        assertEquals(130.0, z.altCm)
        assertEquals(false, z.lost)
        assertEquals(
            listOf(PlanLane(LaneDirection.Forward, 400.0), PlanLane(LaneDirection.Right, 75.0), PlanLane(LaneDirection.Back, 400.0)),
            z.lanes,
        )
    }

    @Test
    fun fleetPlanUncoveredEntriesWithAndWithoutKind() {
        val plan = Fixtures.eventAs<FleetPlan>("fleet_plan.json", "map_plan_lost_zone_uncovered")
        val lanes = plan.uncovered.filterIsInstance<UncoveredEntry.Lane>()
        val areas = plan.uncovered.filterIsInstance<UncoveredEntry.Area>()
        assertEquals(plan.uncovered.size, lanes.size + areas.size)
        assertEquals(2, lanes.size)
        assertEquals(3, areas.size)
        assertEquals("drone-1", lanes[0].lane.droneId)
        assertEquals(PlanRect(110.0, 240.0, 290.0, 160.0), areas[0].rect)
        assertTrue(plan.zones.any { it.lost == true })
    }

    @Test
    fun unknownUncoveredKindIsKeptByName() {
        val entry = UncoveredEntry.fromJson(obj("""{"kind":"volume","reason":"above the ceiling"}"""))
        assertEquals(UncoveredEntry.Unknown("volume", "above the ceiling"), entry)
    }

    @Test
    fun fleetDetection() {
        val d = Fixtures.eventAs<FleetDetection>("fleet_detection.json", "new_detection")
        assertEquals(1L, d.id)
        assertEquals(listOf("drone-1"), d.droneIds)
        assertEquals(120.0, d.xCm)
        assertTrue(d.thumbnail!!.startsWith("/9j/"))
        assertNull(d.lat)
    }

    @Test
    fun frames() {
        val f = Fixtures.eventAs<Frame>("frame.json", "edge_video_with_match")
        assertEquals("drone-1", f.droneId)
        assertEquals(2, f.boxes.size)
        assertEquals(true, f.boxes[0].matched)
        assertEquals(4, f.boxes[0].bboxNorm.size)
        assertEquals(FrameTelemetry(null, null, null), f.telemetry)
        assertEquals(960, f.frameWidth)
        val g = Fixtures.eventAs<Frame>("frame.json", "edge_detections_only")
        assertNull(g.image)
        assertEquals(false, g.streamVideo)
    }

    @Test
    fun matchAlertAndReminder() {
        val a = Fixtures.eventAs<MatchAlert>("match_alert.json", "edge_match")
        assertFalse(a.isReminder)
        assertEquals(AlertLevel.PossibleMatch, a.alertLevel)
        assertEquals("8_7", a.trackId)
        assertEquals(0.86, a.score)
        val r = Fixtures.eventAs<MatchAlert>("match_alert.json", "reminder_on_connect")
        assertTrue(r.isReminder)
        assertEquals(true, r.renotify)
        assertEquals(0, r.renotifyCount)
    }

    @Test
    fun detectionProfiles() {
        val pending = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "pending")
        assertTrue(pending.available)
        assertEquals(listOf("default", "top_down", "level"), pending.profiles.map { it.name })
        val d = pending.drones.getValue("drone-1")
        assertEquals(true, d.pending)
        assertEquals("top_down", d.desired)
        assertEquals(true, d.mismatch)

        val fallback = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "fallback").drones.getValue("drone-1")
        assertEquals(ReasonCode("weights_missing"), fallback.fallbackReason)
        assertTrue(fallback.fallbackReason!!.known)

        val refused = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "refused").drones.getValue("drone-1")
        assertEquals(Refusal(ReasonCode("in_flight"), "detection profiles change only on the ground"), refused.refusal)

        val unsupported = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "unsupported")
        assertEquals(false, unsupported.drones.getValue("drone-2").supported)
        assertEquals("default", unsupported.fleetDefault)

        val standby = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "standby_controls_disabled")
        assertEquals(false, standby.controlsEnabled)
        assertTrue(standby.controlsNote!!.startsWith("This ground station is the standby."))

        val noBoard = Fixtures.eventAs<DetectionProfiles>("detection_profiles.json", "no_board")
        assertFalse(noBoard.available)
        assertTrue(noBoard.profiles.isEmpty())
        assertTrue(noBoard.drones.isEmpty())
    }

    @Test
    fun ceilingSettings() {
        val s = Fixtures.eventAs<CeilingSettings>("ceiling_settings.json", "edge_refused")
        assertTrue(s.available)
        assertEquals(180.0, s.ceilingCm)
        assertEquals(50.0, s.marginCm)
        assertEquals(CeilingLimits(150.0, 1000.0, 30.0, 20.0, 500.0), s.limits)
        assertEquals(listOf("drone-1", "drone-2"), s.drones.map { it.droneId })
        val edge = s.drones[1].edge!!
        assertEquals(false, edge.inSync)
        assertEquals(Refusal(ReasonCode("in_flight"), "the ceiling changes only on the ground"), edge.refusal)
        assertEquals(120.0, s.drones[1].ownSlotCm)

        val locked = Fixtures.eventAs<CeilingSettings>("ceiling_settings.json", "locked_in_flight")
        assertEquals(false, locked.editable)
        assertTrue(locked.lockedReason!!.contains("drone-1 reported flying"))

        val above = Fixtures.eventAs<CeilingSettings>("ceiling_settings.json", "slot_above_limit")
        assertTrue(above.drones[1].problem!!.startsWith("drone-2 slot 160 cm is above the ceiling limit"))
        assertNull(above.drones[0].edge!!.ceilingCm)

        assertFalse(Fixtures.eventAs<CeilingSettings>("ceiling_settings.json", "unavailable").available)
    }

    @Test
    fun reasoningProvider() {
        val blocked = Fixtures.eventAs<ReasoningProvider>("reasoning_provider.json", "local_gemma_cloud_blocked")
        assertEquals(ReasoningProviderKind.Gemma, blocked.provider)
        assertEquals("gemma4:latest", blocked.model)
        assertEquals(false, blocked.cloudAllowed)
        assertEquals(listOf(ReasoningProviderKind.Gemma, ReasoningProviderKind.Off), blocked.available)
        assertTrue(blocked.unavailable.getValue("claude").startsWith("Cloud reasoning is not enabled."))

        val off = Fixtures.eventAs<ReasoningProvider>("reasoning_provider.json", "off")
        assertEquals(ReasoningProviderKind.Off, off.provider)
        assertNull(off.label)

        val allowed = Fixtures.eventAs<ReasoningProvider>("reasoning_provider.json", "cloud_allowed")
        assertEquals(true, allowed.cloudAllowed)
        assertTrue(allowed.unavailable.isEmpty())
        assertTrue(ReasoningProviderKind.Claude in allowed.available)
    }

    @Test
    fun manualStatusAndReplies() {
        val st = Fixtures.eventAs<ManualStatus>("manual_status.json", "held_orphan_countdown")
        assertNull(st.controlled)
        assertEquals(ControlMode.Held, st.drones.getValue("drone-2").controlMode)
        assertEquals(120.0, st.drones.getValue("drone-2").orphanLandInS)
        assertEquals("drone-2", Fixtures.eventAs<ManualStatus>("manual_status.json", "manual_controlled").controlled)

        val ok = Fixtures.eventAs<ManualReply>("manual_ack.json", "land_ok")
        assertEquals(ManualReply(ManualReply.Kind.ACK, "drone-1", ManualAction.Land, true, null, null, null), ok)
        val unknown = Fixtures.eventAs<ManualReply>("manual_ack.json", "land_unknown_drone")
        assertEquals(ReasonCode("invalid"), unknown.reason)
        assertEquals(ManualAction.ResumeSearch, Fixtures.eventAs<ManualReply>("manual_ack.json", "resume_search_ok").action)
        assertEquals(ReasonCode("not_manual"), Fixtures.eventAs<ManualReply>("manual_ack.json", "resume_search_not_manual").reason)

        val noReply = Fixtures.eventAs<ManualReply>("manual_result.json", "land_no_reply")
        assertEquals(ManualReply.Kind.RESULT, noReply.kind)
        assertEquals(ReasonCode("no_reply"), noReply.reason)
        assertEquals("no answer from drone-2 within 4 s", noReply.detail)
        assertEquals(ControlMode.Autonomous, Fixtures.eventAs<ManualReply>("manual_result.json", "resume_search_acked").controlMode)
    }

    @Test
    fun landAllAckIsFlat() {
        assertEquals(LandAllAck(2, 2, null, true), Fixtures.event("land_all_ack.json", "fleet_reached_all"))
        assertEquals(LandAllAck(null, null, 1, true), Fixtures.event("land_all_ack.json", "local_fleet"))
        val none = Fixtures.eventAs<LandAllAck>("land_all_ack.json", "reached_no_drone")
        assertTrue(none.failed)
        assertFalse(LandAllAck(2, 2, null, true).failed)
        // An ack without reached_any is not treated as success.
        assertTrue(LandAllAck(2, null, null, null).failed)
    }

    @Test
    fun smallEvents() {
        assertEquals(CommandAck("land"), Fixtures.event("command_ack.json", "land"))
        assertEquals(StreamVideoState(false), Fixtures.event("stream_video_state.json", "off"))
        assertEquals(
            ServerMessage(ServerMessage.Level.ERROR, "No drones connected"),
            Fixtures.event("error.json", "no_drones_connected"),
        )
        assertEquals(
            ServerMessage(ServerMessage.Level.WARNING, "careful"),
            ServerEvents.decode("warning", """{"message":"careful"}"""),
        )
    }

    @Test
    fun unknownVocabularyValuesNeverThrow() {
        val drone = FleetDrone.fromJson("d", obj("""{"control_mode":"turbo","edge_control_mode":"held"}"""))
        assertEquals(ControlMode.Unknown("turbo"), drone.controlMode)
        assertEquals("turbo", drone.controlMode!!.raw)
        assertEquals(ControlMode.Held, drone.edgeControlMode)
        assertEquals(ReasoningProviderKind.Unknown("llama"), ReasoningProviderKind.of("llama"))
        assertEquals(AlertLevel.Unknown("certain"), AlertLevel.of("certain"))
        assertEquals(LaneDirection.Unknown("up"), LaneDirection.of("up"))
        assertEquals(ManualAction.Unknown("takeoff"), ManualAction.of("takeoff"))
        assertFalse(ReasonCode("cosmic_rays").known)
        for (known in ControlMode.known()) assertEquals(known, ControlMode.of(known.raw))
    }

    @Test
    fun unknownEventNamesAreIgnoredAndBadPayloadsReported() {
        assertEquals(ServerEvent.Ignored("fleet_telemetry"), ServerEvents.decode("fleet_telemetry", """{"a":1}"""))
        assertEquals(ServerEvent.Ignored("plan_preview"), ServerEvents.decode("plan_preview", JsonPrimitive(3)))
        assertTrue(ServerEvents.decode("fleet_status", JsonPrimitive("x")) is ServerEvent.Malformed)
        assertTrue(ServerEvents.decode("fleet_status", "not json {") is ServerEvent.Malformed)
        assertTrue(ServerEvents.decode("status", null as kotlinx.serialization.json.JsonElement?) is ServerEvent.Malformed)
    }

    @Test
    fun booleansAreStrictLikeTheDashboard() {
        // ui.js tests `connected === true`; the string "true" or the number 1 is not a boolean.
        val d = FleetDrone.fromJson("d", obj("""{"connected":"true","lost":1,"battery":"87","lane_index":2.0}"""))
        assertNull(d.connected)
        assertNull(d.lost)
        assertNull(d.battery)
        assertEquals(2, d.laneIndex)
    }

    private fun obj(text: String) = ContractJson.parseToJsonElement(text).jsonObject
}
