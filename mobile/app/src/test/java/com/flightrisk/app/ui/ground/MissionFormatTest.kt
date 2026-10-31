package com.flightrisk.app.ui.ground

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.contract.CeilingSettings
import com.flightrisk.contract.ContractJson
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.FleetPlan
import com.flightrisk.contract.ReasoningProvider
import com.flightrisk.contract.ServerEvent
import com.flightrisk.contract.ServerEvents
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The mission cards and the plan canvas as plain data: every fixture state, no Android classes. */
class MissionFormatTest {
    private inline fun <reified T : ServerEvent> fixture(file: String, case: String): T {
        val c = ContractFixtures.case(file, case)
        val name = (c["event"] as kotlinx.serialization.json.JsonPrimitive).content
        return ServerEvents.decode(name, c["payload"]) as T
    }

    private fun profiles(case: String) = fixture<DetectionProfiles>("detection_profiles.json", case)
    private fun card(case: String, stale: Boolean = false, connected: Boolean = true) =
        MissionFormat.profilesCard(profiles(case), stale, connected)!!

    private fun plan(json: String): FleetPlan =
        FleetPlan.fromJson(ContractJson.parseToJsonElement(json) as JsonObject)

    // ---- profiles card

    @Test
    fun `pending shows Switching with the profile label`() {
        val c = card("pending")
        val row = c.drones.single()
        assertEquals("Switching to Top-down search (HERIDAL)...", row.statusText)
        assertEquals(Severity.INFO, row.severity)
        assertTrue(row.canSelect)
        assertNull(c.blockedNote)
    }

    @Test
    fun `fallback shows the requested profile and the reason`() {
        val row = card("fallback").drones.single()
        assertEquals("Active: Startup default (yolo11n) (fallback: Top-down search (HERIDAL) weights missing on the edge).", row.statusText)
        assertEquals(Severity.WARN, row.severity)
    }

    @Test
    fun `refused in flight shows the mismatch and stays bad`() {
        val row = card("refused").drones.single()
        assertEquals(
            "MISMATCH: chosen Top-down search (HERIDAL), edge runs Startup default (yolo11n). " +
                "Refused in flight; sent again when the drone is on the ground.",
            row.statusText,
        )
        assertEquals(Severity.BAD, row.severity)
    }

    @Test
    fun `a refusal without a mismatch shows the ground's own detail`() {
        val p = profiles("refused")
        val d = p.drones.getValue("drone-1").copy(mismatch = false)
        val (text, severity) = MissionFormat.profileStatus(d, p)
        assertEquals("Active: Startup default (yolo11n). Switch refused in flight: detection profiles change only on the ground", text)
        assertEquals(Severity.BAD, severity)
    }

    @Test
    fun `unsupported shows not supported and offers no selector for that drone`() {
        val c = card("unsupported")
        assertEquals(listOf("drone-1", "drone-2"), c.drones.map { it.id })
        val unsupported = c.drones[1]
        assertFalse(unsupported.supported)
        assertTrue(unsupported.options.isEmpty())
        assertFalse(unsupported.canSelect)
        assertEquals("This edge does not report detection profiles (older edge); it keeps its own model.", unsupported.statusText)
        assertEquals(MissionFormat.NOT_SUPPORTED, "Not supported")
        assertEquals("Active: Startup default (yolo11n).", c.drones[0].statusText)
        assertTrue(c.drones[0].canSelect)
        assertEquals("Fleet default: Startup default (yolo11n)", c.fleetDefaultText)
        assertEquals("default", c.fleetOptions.single { it.selected }.value)
    }

    @Test
    fun `standby with controls disabled blocks every selector and shows the ground's note`() {
        val c = card("standby_controls_disabled")
        assertEquals(
            "This ground station is the standby. Edges take detection profile changes only from the leader ground; change them there.",
            c.blockedNote,
        )
        assertFalse(c.canSelect)
        assertTrue(c.drones.none { it.canSelect })
        // The options still exist so the current choice stays visible, but nothing is enabled.
        assertTrue(c.drones.single().options.isNotEmpty())
    }

    @Test
    fun `a ground without a profile board hides the card`() {
        assertNull(MissionFormat.profilesCard(profiles("no_board"), false, true))
        assertNull(MissionFormat.profilesCard(null, false, true))
    }

    @Test
    fun `a stale or disconnected state blocks selection even when the ground says controls are on`() {
        val stale = card("pending", stale = true)
        assertEquals(MissionFormat.NOT_LIVE_NOTE, stale.blockedNote)
        assertTrue(stale.drones.none { it.canSelect })
        assertFalse(stale.canSelect)
        val offline = card("pending", connected = false)
        assertEquals(MissionFormat.NOT_CONNECTED_NOTE, offline.blockedNote)
        assertFalse(offline.canSelect)
    }

    @Test
    fun `options list follow default first and mark the drone's override`() {
        val row = card("pending").drones.single()
        assertEquals(listOf("", "default", "top_down", "level"), row.options.map { it.value })
        assertEquals("Follow fleet default", row.options[0].label)
        // override is top_down in this fixture.
        assertEquals("top_down", row.options.single { it.selected }.value)
        assertEquals("No default", card("pending").fleetOptions[0].label)
        assertTrue(card("pending").fleetOptions[0].selected)
    }

    @Test
    fun `long text from the ground is cut`() {
        val long = "x".repeat(5000)
        val p = profiles("refused")
        val d = p.drones.getValue("drone-1").copy(mismatch = false, refusal = p.drones.getValue("drone-1").refusal!!.copy(detail = long))
        val text = MissionFormat.profileStatus(d, p).first
        assertTrue(text.length < 400)
    }

    // ---- ceiling card

    private fun ceiling(case: String) = MissionFormat.ceilingCard(fixture<CeilingSettings>("ceiling_settings.json", case))

    @Test
    fun `ceiling in sync shows values and the synced edge lines`() {
        val c = ceiling("on_ground_in_sync")!!
        assertEquals("Ceiling 200 cm, margin 60 cm", c.headline)
        assertEquals("Highest allowed slot: 140 cm (ceiling minus margin)", c.maxSlotText)
        assertNull(c.lockedText)
        assertEquals(listOf("drone-1", "drone-2"), c.drones.map { it.id })
        assertEquals("Slot 100 cm", c.drones[0].slotText)
        assertEquals("Edge: ceiling 200, margin 60, slot 100 cm", c.drones[0].edgeText)
        assertEquals(Severity.INFO, c.drones[0].edgeSeverity)
    }

    @Test
    fun `edge pending shows sending and the own slot`() {
        val c = ceiling("edge_pending")!!
        assertEquals("Ceiling 180 cm, margin 50 cm", c.headline)
        assertEquals("Sending to the edge...", c.drones[0].edgeText)
        assertEquals("Slot 120 cm (own; default 130 cm)", c.drones[1].slotText)
    }

    @Test
    fun `edge refused shows the ground's detail`() {
        val c = ceiling("edge_refused")!!
        assertEquals("Edge refused: the ceiling changes only on the ground", c.drones[1].edgeText)
        assertEquals(Severity.WARN, c.drones[1].edgeSeverity)
    }

    @Test
    fun `slot above limit shows the problem and older edges`() {
        val c = ceiling("slot_above_limit")!!
        assertEquals("drone-2 slot 160 cm is above the ceiling limit 140 cm (ceiling 200 cm minus margin 60 cm)", c.drones[1].problem)
        assertNull(c.drones[0].problem)
        assertEquals("Edge does not report a ceiling (older edge): it flies the planned slot only.", c.drones[0].edgeText)
    }

    @Test
    fun `locked in flight shows the reason`() {
        val c = ceiling("locked_in_flight")!!
        assertTrue(c.lockedText!!.startsWith("Locked in flight: the ceiling and the altitude slots change only while every drone is on the ground"))
    }

    @Test
    fun `unavailable ceiling hides the card`() {
        assertNull(ceiling("unavailable"))
        assertNull(MissionFormat.ceilingCard(null))
    }

    @Test
    fun `a not yet applied edge is a warning`() {
        val p = fixture<CeilingSettings>("ceiling_settings.json", "on_ground_in_sync")
        val d = p.drones[0]
        val out = MissionFormat.ceilingEdgeText(d.copy(edge = d.edge!!.copy(inSync = false)))
        assertEquals("Edge: ceiling 200, margin 60, slot 100 cm (not yet applied)", out.first)
        assertEquals(Severity.WARN, out.second)
    }

    // ---- reasoning card

    private fun reasoning(case: String) = MissionFormat.reasoningCard(fixture<ReasoningProvider>("reasoning_provider.json", case))!!

    @Test
    fun `reasoning with cloud blocked shows the ground's reason`() {
        val r = reasoning("local_gemma_cloud_blocked")
        assertEquals("Gemma (gemma4:latest)", r.statusText)
        assertEquals("Cloud not allowed", r.cloudBadge)
        assertFalse(r.cloudAllowed)
        assertEquals("Available: Gemma, Off", r.availableText)
        assertEquals(1, r.unavailableNotes.size)
        assertTrue(r.unavailableNotes[0].startsWith("Claude: Cloud reasoning is not enabled."))
    }

    @Test
    fun `reasoning off cloud allowed and no key`() {
        assertEquals("Reasoning off: matches use ReID and face scores only", reasoning("off").statusText)
        val allowed = reasoning("cloud_allowed")
        assertEquals("Cloud allowed", allowed.cloudBadge)
        assertTrue(allowed.unavailableNotes.isEmpty())
        val noKey = reasoning("cloud_allowed_no_key")
        assertEquals("Cloud allowed", noKey.cloudBadge)
        assertEquals(listOf("Claude: FLIGHTRISK_CLAUDE_API_KEY is not set."), noKey.unavailableNotes)
        assertNull(MissionFormat.reasoningCard(null))
    }

    // ---- plan canvas data

    @Test
    fun `no plan shows the empty text`() {
        val ui = MissionFormat.planCanvas(null, isStale = false, planIsCurrent = true)
        assertTrue(ui.isEmpty)
        assertEquals("No plan yet. Plan from the dashboard.", MissionFormat.EMPTY_TEXT)
        assertEquals("No plan yet. Plan from the dashboard.", ui.accessibilitySummary)
        assertTrue(MissionFormat.planCanvas(plan("{}"), false, true).isEmpty)
    }

    @Test
    fun `titles follow the live state`() {
        val p = fixture<FleetPlan>("fleet_plan.json", "two_drones")
        assertEquals("Search plan", MissionFormat.planCanvas(p, false, true).title)
        assertEquals("Last plan", MissionFormat.planCanvas(p, true, true).title)
        assertEquals("Last plan (not confirmed this session)", MissionFormat.planCanvas(p, false, false).title)
        assertEquals("Last plan (not confirmed this session)", MissionFormat.planCanvas(p, true, false).title)
        assertFalse(MissionFormat.planCanvas(p, false, true).isNotLive)
        assertTrue(MissionFormat.planCanvas(p, false, false).isNotLive)
        assertTrue(MissionFormat.planCanvas(p, true, true).isNotLive)
    }

    @Test
    fun `the gap note and the accessibility summary`() {
        val p = fixture<FleetPlan>("fleet_plan.json", "two_drones")
        val ui = MissionFormat.planCanvas(p, false, true)
        assertEquals("Gap between zones: 100 cm", ui.gapNote)
        assertNull(ui.skippedNote)
        assertEquals("Search plan: 2 zones. Gap between zones: 100 cm", ui.accessibilitySummary)
    }

    @Test
    fun `lost zone and uncovered legends with at most three distinct reasons`() {
        val p = fixture<FleetPlan>("fleet_plan.json", "map_plan_lost_zone_uncovered")
        val ui = MissionFormat.planCanvas(p, false, true)
        assertTrue(ui.drawing!!.hasLostZone)
        assertEquals("Not searched", MissionFormat.UNCOVERED_LEGEND)
        assertEquals("May not be covered (camera model)", MissionFormat.AREA_LEGEND)
        assertTrue(ui.laneReasons.size in 1..3)
        assertTrue(ui.areaReasons.size in 1..3)
        assertTrue(ui.accessibilitySummary.contains("1 lost"))
        assertTrue(ui.accessibilitySummary.contains("2 not searched"))
        assertTrue(ui.accessibilitySummary.contains("3 may not be covered (camera model)"))
        val many = plan(
            """{"uncovered":[${(1..9).joinToString(",") { """{"kind":"area","x_cm":$it,"y_cm":0,"w_cm":5,"h_cm":5,"reason":"reason ${it % 5}"}""" }}]}""",
        )
        val reasons = MissionFormat.planCanvas(many, false, true).areaReasons
        assertEquals(listOf("reason 1", "reason 2", "reason 3"), reasons)
    }

    @Test
    fun `skipped, partial and out of range notes are shown and spoken`() {
        val lane = """{"direction":"forward","distance_cm":1}"""
        val lanes = """{"direction":"up","distance_cm":5},""" + List(1002) { lane }.joinToString(",")
        val p = plan(
            """{"zones":[{"drone_id":"drone-7","x_cm":5,"y_cm":6,"w_cm":10,"h_cm":10,"takeoff_x_cm":1e12,"takeoff_y_cm":40,"lanes":[$lanes]}],
               "uncovered":[{"kind":"polygon"},{"kind":"polygon"}]}""",
        )
        val ui = MissionFormat.planCanvas(p, false, true)
        val note = ui.skippedNote!!
        assertTrue(note, note.contains("1 lane not drawn (unknown direction or distance)"))
        assertTrue(note, note.contains("2 uncovered entries not drawn"))
        assertTrue(note, note.contains("takeoff point out of range for drone-7; drawn from the zone corner"))
        assertTrue(note, note.endsWith("Only part of the plan is drawn."))
        assertTrue(ui.accessibilitySummary.contains(note))
    }

    @Test
    fun `drone colours are picked by the number at the end of the id`() {
        assertEquals(0, MissionFormat.colorIndex("drone-1"))
        assertEquals(1, MissionFormat.colorIndex("drone-2"))
        assertEquals(3, MissionFormat.colorIndex("tello4"))
        assertEquals(0, MissionFormat.colorIndex("drone-5"))
        assertEquals(0, MissionFormat.colorIndex("alpha"))
        assertEquals(0, MissionFormat.colorIndex("drone-0"))
        assertEquals(0, MissionFormat.colorIndex("d-99999999999999999999"))
    }

    // ---- contrast

    @Test
    fun `every plan canvas colour keeps 4_5 to 1 on its background in both themes`() {
        assertEquals(16, PlanColors.pairs.size)
        for ((name, fg, bg) in PlanColors.pairs) {
            val ratio = GroundColors.contrast(fg, bg)
            assertTrue("$name has contrast $ratio", ratio >= 4.5)
        }
        assertEquals(PlanColors.droneColorCount, PlanColors.droneLight.size)
        assertEquals(PlanColors.droneColorCount, PlanColors.droneDark.size)
    }

    @Test
    fun `no state of the cards carries a secret`() {
        val all = listOf("pending", "fallback", "refused", "unsupported", "standby_controls_disabled").map { card(it).toString() } +
            listOf("edge_pending", "edge_refused", "locked_in_flight").map { ceiling(it).toString() } +
            listOf("cloud_allowed", "cloud_allowed_no_key").map { reasoning(it).toString() }
        assertNotNull(all)
        assertTrue(all.none { it.contains("test-key") || it.contains("sk-") })
    }
}
