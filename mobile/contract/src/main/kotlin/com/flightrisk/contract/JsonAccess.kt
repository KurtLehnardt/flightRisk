package com.flightrisk.contract

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull

/**
 * The JSON configuration shared by every decoder in this module.
 *
 * Decoders read the parsed element tree field by field instead of binding
 * whole classes, so unknown keys are ignored at every depth by construction,
 * a value of the wrong type reads as "not reported" (null) rather than
 * throwing, and a boolean is only ever `true` or `false` when the wire carries
 * a JSON boolean, which is exactly how the dashboard's `=== true` and
 * `=== false` tests behave. `ignoreUnknownKeys` is still set so that any
 * future class-bound decoding inherits the same forward compatibility.
 */
val ContractJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
}

/** Thrown only by the `decodeOrThrow` helpers; the normal decoders return a result type. */
class ContractDecodeException(message: String) : IllegalArgumentException(message)

/** Parses text into a JSON element, or returns null when the text is not JSON. */
fun parseJsonOrNull(text: String): JsonElement? =
    try {
        ContractJson.parseToJsonElement(text)
    } catch (e: IllegalArgumentException) {
        // kotlinx.serialization reports malformed JSON as a SerializationException,
        // which is an IllegalArgumentException.
        null
    }

internal fun JsonElement?.asObject(): JsonObject? = this as? JsonObject

internal fun JsonElement?.asArray(): JsonArray? = this as? JsonArray

/** A JSON string value, or null for anything else (absent, null, number, boolean, object). */
internal fun JsonElement?.stringValue(): String? {
    val p = this as? JsonPrimitive ?: return null
    return if (p.isString) p.content else null
}

/** A JSON boolean value, or null for anything else. The string "true" is not a boolean. */
internal fun JsonElement?.boolValue(): Boolean? {
    val p = this as? JsonPrimitive ?: return null
    return if (p.isString) null else p.booleanOrNull
}

/** A JSON number value, or null for anything else (a numeric string is not a number). */
internal fun JsonElement?.numberValue(): Double? {
    val p = this as? JsonPrimitive ?: return null
    if (p.isString) return null
    if (p.booleanOrNull != null) return null
    return p.doubleOrNull?.takeIf { it.isFinite() }
}

/** A whole JSON number (`2` or `2.0`) as an Int, or null when absent, fractional or out of range. */
internal fun JsonElement?.intValue(): Int? {
    val d = numberValue() ?: return null
    if (d != Math.floor(d)) return null
    if (d < Int.MIN_VALUE.toDouble() || d > Int.MAX_VALUE.toDouble()) return null
    return d.toInt()
}

/** A whole JSON number as a Long, or null when absent, fractional or out of range. */
internal fun JsonElement?.longValue(): Long? {
    val d = numberValue() ?: return null
    if (d != Math.floor(d)) return null
    if (d < -9.007199254740991E15 || d > 9.007199254740991E15) return null
    return d.toLong()
}

internal fun JsonObject.str(key: String): String? = this[key].stringValue()
internal fun JsonObject.bool(key: String): Boolean? = this[key].boolValue()
internal fun JsonObject.num(key: String): Double? = this[key].numberValue()
internal fun JsonObject.int(key: String): Int? = this[key].intValue()
internal fun JsonObject.long(key: String): Long? = this[key].longValue()
internal fun JsonObject.obj(key: String): JsonObject? = this[key].asObject()
internal fun JsonObject.arr(key: String): JsonArray? = this[key].asArray()

/** A list of the string elements of an array; non-string elements are skipped. */
internal fun JsonObject.strings(key: String): List<String>? =
    arr(key)?.mapNotNull { it.stringValue() }

/** A list of the numeric elements of an array; non-numeric elements are skipped. */
internal fun JsonObject.numbers(key: String): List<Double>? =
    arr(key)?.mapNotNull { it.numberValue() }

/** The object elements of an array decoded with [decode]; non-object elements are skipped. */
internal fun <T> JsonObject.objects(key: String, decode: (JsonObject) -> T): List<T>? =
    arr(key)?.mapNotNull { it.asObject()?.let(decode) }

/**
 * An object keyed by id (drone id, provider name) decoded with [decode]. The
 * result keeps the ids sorted, which is the display order the dashboard uses.
 * Entries whose value is not an object are skipped.
 */
internal fun <T> JsonObject.keyedObjects(key: String, decode: (String, JsonObject) -> T): Map<String, T>? {
    val o = obj(key) ?: return null
    val out = sortedMapOf<String, T>()
    for ((id, value) in o) {
        val entry = value.asObject() ?: continue
        out[id] = decode(id, entry)
    }
    return out
}

/** Formats a number the way JavaScript's string concatenation does (`120`, `12.5`). */
fun jsNumber(value: Double): String {
    if (value.isNaN()) return "NaN"
    if (value.isInfinite()) return if (value > 0) "Infinity" else "-Infinity"
    if (value == Math.floor(value) && Math.abs(value) < 1e21) return value.toLong().toString()
    return value.toString()
}
