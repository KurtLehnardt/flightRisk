package com.flightrisk.contract

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import java.io.File

/**
 * The shared contract fixtures in `tests/fixtures/mobile_contract` at the
 * repository root. Gradle passes the directory as the system property
 * `flightrisk.fixturesDir`; without it, the path is resolved from the module
 * directory (`mobile/contract`), which is the test working directory.
 */
object Fixtures {
    /** Every fixture file docs/mobile-contract.md lists. */
    val EXPECTED_FILES: Set<String> = setOf(
        "ceiling_settings.json", "client_emits.json", "command_ack.json", "detection_profiles.json",
        "edge_edge_to_ground.json", "edge_ground_to_edge.json", "error.json", "fleet_detection.json",
        "fleet_event.json", "fleet_plan.json", "fleet_status.json", "frame.json", "land_all_ack.json",
        "manual_ack.json", "manual_result.json", "manual_status.json", "match_alert.json",
        "nack_reasons.json", "reasoning_provider.json", "socketio_frames.json", "status.json",
        "stream_video_state.json", "unknown_keys.json",
    )

    val dir: File by lazy {
        val configured = System.getProperty("flightrisk.fixturesDir")
        val candidate = (configured?.let(::File) ?: File("../../tests/fixtures/mobile_contract")).canonicalFile
        if (!candidate.isDirectory) {
            bad(
                "Contract fixture directory not found at $candidate. The fixtures live in " +
                    "tests/fixtures/mobile_contract at the repository root; run the tests from mobile/ " +
                    "with ./gradlew :contract:test so the path resolves from mobile/contract.",
            )
        }
        candidate
    }

    private val cache = mutableMapOf<String, JsonObject>()

    /** The whole fixture file. */
    fun file(name: String): JsonObject = cache.getOrPut(name) {
        val f = File(dir, name)
        if (!f.isFile) bad("Fixture file $name not found in $dir")
        ContractJson.parseToJsonElement(f.readText()).jsonObject
    }

    fun meta(name: String): JsonObject = file(name)["_meta"] as JsonObject

    /** The named cases of a fixture file, in file order. */
    fun cases(name: String): Map<String, JsonObject> {
        val cases = file(name)["cases"] as? JsonObject ?: bad("$name has no cases")
        return cases.mapValues { (_, v) -> v as JsonObject }
    }

    fun case(name: String, case: String): JsonObject =
        cases(name)[case] ?: bad("$name has no case $case")

    /** The wire payload of a case (an object for every event fixture). */
    fun payload(name: String, case: String): JsonObject =
        case(name, case)["payload"] as? JsonObject ?: bad("$name/$case has no object payload")

    /** Decodes a Socket.IO event case with [ServerEvents]. */
    fun event(name: String, case: String): ServerEvent {
        val c = case(name, case)
        val eventName = (c["event"] as JsonPrimitive).content
        return ServerEvents.decode(eventName, c["payload"])
    }

    inline fun <reified T : ServerEvent> eventAs(name: String, case: String): T {
        val e = event(name, case)
        return e as? T ?: error("$name/$case decoded to ${e::class.simpleName}, expected ${T::class.simpleName}")
    }
}

/** Fails the test with [message]. */
fun bad(message: String): Nothing = throw AssertionError(message)

/**
 * JSON equality where numbers compare by value (`512` equals `512.0`), as
 * Python and JavaScript read them.
 */
fun jsonSemanticallyEqual(a: JsonElement?, b: JsonElement?): Boolean {
    if (a == null || a is JsonNull) return b == null || b is JsonNull
    if (b == null || b is JsonNull) return false
    return when (a) {
        is JsonObject -> b is JsonObject && a.keys == b.keys && a.keys.all { jsonSemanticallyEqual(a[it], b[it]) }
        is JsonArray -> b is JsonArray && a.size == b.size && a.indices.all { jsonSemanticallyEqual(a[it], b[it]) }
        is JsonPrimitive -> {
            val pb = b as? JsonPrimitive ?: return false
            when {
                a.isString || pb.isString -> a.isString && pb.isString && a.content == pb.content
                a.booleanOrNull != null || pb.booleanOrNull != null -> a.booleanOrNull == pb.booleanOrNull
                else -> a.doubleOrNull != null && a.doubleOrNull == pb.doubleOrNull
            }
        }
    }
}
