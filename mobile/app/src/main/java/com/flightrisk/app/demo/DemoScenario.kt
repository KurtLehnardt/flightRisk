package com.flightrisk.app.demo

import com.flightrisk.app.ground.JsonGuard
import com.flightrisk.contract.ServerEvents
import com.flightrisk.contract.parseJsonOrNull
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * The built-in demo mission: `tests/fixtures/mobile_contract/demo_scenario.json`, produced by the real
 * ground code (`scripts/export_mobile_contract_fixtures.py`) and copied into the app as the asset
 * [ASSET_PATH]. It holds:
 *
 * - [steps]: dashboard events with the time, in milliseconds after the demo connects, at which the
 *   demo replays each one (fleet status, the plan, drones flying their lanes, an obstacle cut, a match
 *   alert and its AI check verdict, then landing);
 * - [state]: the answers to the state requests (`detection_profiles`, `ceiling_settings`,
 *   `reasoning_provider`, `manual_status`), keyed by event name;
 * - [replies]: the events the demo answers each operator command with, keyed by the command's event
 *   name (`land_all`, `drone_command`, `manual_resume_search`);
 * - [landedFleetStatus]: the fleet status once every drone has landed.
 *
 * Every payload is a contract payload, so the demo drives the same client, decoders and screens a real
 * ground does. Nothing here is ever sent anywhere.
 */
class DemoScenario(
    val steps: List<Step>,
    val state: Map<String, JsonObject>,
    val replies: Map<String, List<Reply>>,
    val landedFleetStatus: JsonObject?,
) {
    /** One replayed event. */
    data class Step(val atMs: Long, val event: String, val payload: JsonObject)

    /** One event a command is answered with. */
    data class Reply(val event: String, val payload: JsonObject)

    /** How long the replay runs, in milliseconds. */
    val durationMs: Long get() = steps.lastOrNull()?.atMs ?: 0L

    companion object {
        /** Where the scenario is packaged in the app. */
        const val ASSET_PATH = "demo/demo_scenario.json"

        /** The fixture case that holds the scenario. */
        const val CASE = "scenario"

        /** More steps than a demo needs; a larger file is not a demo scenario. */
        const val MAX_STEPS = 1_000

        /** The largest scenario text accepted (the fixture is well under 256 KiB). */
        const val MAX_TEXT_CHARS = 4 * 1024 * 1024

        /** The longest replay accepted: one hour. */
        const val MAX_AT_MS = 3_600_000L

        /**
         * Reads the scenario from the fixture file's text, or returns null when it is not a usable
         * scenario. Steps with an unknown event, a payload that is not an object or a bad time are skipped.
         */
        fun parse(text: String): DemoScenario? {
            if (text.length > MAX_TEXT_CHARS || JsonGuard.check(text) != null) return null
            val root = parseJsonOrNull(text) as? JsonObject ?: return null
            val payload = ((root["cases"] as? JsonObject)?.get(CASE) as? JsonObject)?.get("payload") as? JsonObject
                ?: return null
            val rawSteps = payload["steps"] as? JsonArray ?: return null
            if (rawSteps.size > MAX_STEPS) return null
            val steps = rawSteps.mapNotNull { element ->
                val o = element as? JsonObject ?: return@mapNotNull null
                val at = (o["at_ms"] as? JsonPrimitive)?.takeIf { !it.isString }?.content?.toDoubleOrNull() ?: return@mapNotNull null
                if (!at.isFinite() || at < 0 || at > MAX_AT_MS || at != Math.floor(at)) return@mapNotNull null
                val event = (o["event"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return@mapNotNull null
                if (event !in ServerEvents.HANDLED) return@mapNotNull null
                val body = o["payload"] as? JsonObject ?: return@mapNotNull null
                Step(at.toLong(), event, body)
            }.sortedBy { it.atMs }
            if (steps.isEmpty()) return null
            val state = (payload["state"] as? JsonObject).orEmpty()
                .filter { (name, value) -> name in ServerEvents.HANDLED && value is JsonObject }
                .mapValues { it.value as JsonObject }
            val replies = (payload["replies"] as? JsonObject).orEmpty().mapValues { (_, list) ->
                (list as? JsonArray).orEmpty().mapNotNull { element ->
                    val o = element as? JsonObject ?: return@mapNotNull null
                    val event = (o["event"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return@mapNotNull null
                    if (event !in ServerEvents.HANDLED) return@mapNotNull null
                    Reply(event, o["payload"] as? JsonObject ?: return@mapNotNull null)
                }
            }
            return DemoScenario(steps, state, replies, payload["landed_fleet_status"] as? JsonObject)
        }
    }
}
