package com.flightrisk.app.demo

import com.flightrisk.app.ground.ContractFixtures
import com.flightrisk.app.ui.ground.DemoFixtures
import com.flightrisk.contract.ContractJson
import com.flightrisk.contract.ServerEvents
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** The packaged demo mission is the contract fixture, and it parses into the whole story. */
class DemoScenarioTest {

    private val asset: File = listOf(File("src/main/assets"), File("app/src/main/assets"), File("mobile/app/src/main/assets"))
        .map { File(it, DemoScenario.ASSET_PATH) }.first { it.isFile }

    private fun withoutCommit(text: String): JsonObject {
        val root = ContractJson.parseToJsonElement(text).jsonObject
        val meta = root.getValue("_meta").jsonObject
        return JsonObject(root + ("_meta" to JsonObject(meta - "generated_at_commit")))
    }

    @Test
    fun `the packaged asset is the contract fixture`() {
        val fixture = File(ContractFixtures.dir, "demo_scenario.json").readText()
        assertEquals(
            "mobile/app/src/main/assets/demo/demo_scenario.json is stale; copy tests/fixtures/mobile_contract/demo_scenario.json over it",
            withoutCommit(fixture), withoutCommit(asset.readText()),
        )
        assertNotNull(DemoScenario.parse(asset.readText()))
    }

    @Test
    fun `the scenario tells the whole story in order`() {
        val s = DemoFixtures.scenario
        assertEquals(s.steps.sortedBy { it.atMs }, s.steps)
        assertTrue(s.steps.all { it.event in ServerEvents.HANDLED })
        val names = s.steps.map { it.event }
        val kinds = s.steps.filter { it.event == ServerEvents.FLEET_EVENT }.map { it.payload["event"]?.jsonPrimitive?.content }
        assertTrue(names.containsAll(listOf("status", "fleet_status", "fleet_plan", "fleet_detection", "match_alert", "reasoning_result", "alert_upgrade")))
        assertTrue(kinds.containsAll(listOf("start", "lane_blocked", "land_all")))

        // The alert gets its verdict and upgrade later, under the same alert id.
        val alert = s.steps.first { it.event == "match_alert" }
        val verdict = s.steps.first { it.event == "reasoning_result" }
        val upgrade = s.steps.first { it.event == "alert_upgrade" }
        val id = alert.payload["alert_id"]
        assertEquals(id, verdict.payload["alert_id"])
        assertEquals(id, upgrade.payload["alert_id"])
        assertTrue(alert.atMs < verdict.atMs && verdict.atMs <= upgrade.atMs)
        assertEquals("pending", alert.payload["gemma_confidence"]?.jsonPrimitive?.content)

        // Drones fly between the start and the landing, and every drone is on the ground at the end.
        val flying = s.steps.filter { it.event == "fleet_status" }.map { step ->
            step.payload["drones"]!!.jsonObject.values.count { (it as JsonObject)["is_flying"]?.jsonPrimitive?.boolean == true }
        }
        assertTrue("drones fly during the mission: $flying", flying.any { it > 0 })
        assertEquals(0, flying.last())
        val landed = s.landedFleetStatus!!["drones"]!!.jsonObject.values
        assertTrue(landed.all { (it as JsonObject)["is_flying"]?.jsonPrimitive?.boolean == false })

        assertEquals(setOf("detection_profiles", "ceiling_settings", "reasoning_provider", "manual_status"), s.state.keys)
        assertEquals(listOf("land_all_ack", "fleet_event"), s.replies["land_all"]!!.map { it.event })
        assertEquals(listOf("manual_ack", "manual_result"), s.replies["drone_command"]!!.map { it.event })
        assertEquals(listOf("manual_ack", "manual_result"), s.replies["manual_resume_search"]!!.map { it.event })
        assertTrue(s.durationMs in 30_000..120_000)
        // Every id starts with demo- (as on iOS), and one drone is held long enough to try Resume search.
        val ids = s.steps.flatMap { step -> (step.payload["drones"] as? JsonObject)?.keys.orEmpty() }.toSet()
        assertEquals(setOf("demo-drone-1", "demo-drone-2"), ids)
        assertEquals("demo-alert-1", alert.payload["alert_id"]?.jsonPrimitive?.content)
        assertEquals("demo-track-1", alert.payload["track_id"]?.jsonPrimitive?.content)
        val held = s.held!!
        assertEquals("demo-drone-2", held.droneId)
        assertTrue(held.untilMs - held.fromMs >= 12_000)
    }

    @Test
    fun `the demo pictures in the scenario are the tiny grey stand-in, never a photo`() {
        val snapshots = DemoFixtures.scenario.steps.mapNotNull { (it.payload["snapshot"] as? JsonPrimitive)?.content }
        assertTrue(snapshots.isNotEmpty())
        // The 8 x 8 grey JPEG of the fixtures is 631 bytes; a photo would be far larger.
        assertTrue(snapshots.all { java.util.Base64.getDecoder().decode(it).size < 1_000 })
    }

    @Test
    fun `broken scenarios are refused and bad steps skipped`() {
        assertNull(DemoScenario.parse(""))
        assertNull(DemoScenario.parse("not json"))
        assertNull(DemoScenario.parse("[".repeat(100) + "]".repeat(100)))
        assertNull(DemoScenario.parse("""{"cases":{"scenario":{"payload":{"steps":"no"}}}}"""))
        assertNull(DemoScenario.parse("""{"cases":{"scenario":{"payload":{"steps":[]}}}}"""))
        val many = (0..DemoScenario.MAX_STEPS).joinToString(",") { """{"at_ms":0,"event":"status","payload":{}}""" }
        assertNull(DemoScenario.parse("""{"cases":{"scenario":{"payload":{"steps":[$many]}}}}"""))
        val mixed = DemoScenario.parse(
            """{"cases":{"scenario":{"payload":{"steps":[
              {"at_ms":5,"event":"status","payload":{}},
              {"at_ms":-1,"event":"status","payload":{}},
              {"at_ms":1.5,"event":"status","payload":{}},
              {"at_ms":"7","event":"status","payload":{}},
              {"at_ms":3,"event":"set_target","payload":{}},
              {"at_ms":4,"event":"fleet_status","payload":[]},
              {"at_ms":1,"event":"warning","payload":{"message":"m"}}
            ],"replies":{"land_all":[{"event":"restart_dashboard","payload":{}},{"event":"land_all_ack","payload":{}}]}}}}}""",
        )!!
        assertEquals(listOf(1L to "warning", 5L to "status"), mixed.steps.map { it.atMs to it.event })
        assertEquals(listOf("land_all_ack"), mixed.replies["land_all"]!!.map { it.event })
        assertNull(mixed.landedFleetStatus)
        assertNull(mixed.held)
    }
}
