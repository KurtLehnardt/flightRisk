package com.flightrisk.app.ui.ground

import androidx.compose.ui.graphics.Color
import com.flightrisk.contract.CeilingDrone
import com.flightrisk.contract.CeilingSettings
import com.flightrisk.contract.DetectionProfiles
import com.flightrisk.contract.DisplayText
import com.flightrisk.contract.DroneProfileState
import com.flightrisk.contract.FleetPlan
import com.flightrisk.contract.PlanDrawing
import com.flightrisk.contract.PlanGeometry
import com.flightrisk.contract.ReasoningProvider
import com.flightrisk.contract.ReasoningProviderKind

/*
 * Pure text and state builders for the mission cards and the plan canvas. Nothing here uses
 * Android classes, so every state of every card is covered by JVM tests. The wording follows the
 * dashboard (`ui.js`, `ceiling_settings.html`) and the iOS Ground tab. Every string that came
 * from the ground goes through GroundText.shorten (300 characters, credentials removed).
 */

enum class Severity { INFO, WARN, BAD }

// ------------------------------------------------------------------ detection profiles

/** One choice in a profile selector. [value] is the wire name; the empty string means "follow the fleet default" (or "no default"). */
data class ProfileOption(val value: String, val label: String, val selected: Boolean)

data class DroneProfileRow(
    val id: String,
    val displayId: String,
    /** The status line under the selector (the dashboard's `profileStatusCore`). */
    val statusText: String,
    val severity: Severity,
    val supported: Boolean,
    /** Empty for a drone whose edge does not report profiles. */
    val options: List<ProfileOption>,
    /** True when the selector may be used: supported, and the card as a whole is selectable. */
    val canSelect: Boolean,
)

data class ProfilesCardUi(
    val fleetDefaultText: String,
    val fleetOptions: List<ProfileOption>,
    /** Why no selector can be used, or null when selectors are live. */
    val blockedNote: String?,
    val canSelect: Boolean,
    val drones: List<DroneProfileRow>,
    /** True when the list of profiles or drones was cut by a display cap. */
    val partial: Boolean,
)

object MissionFormat {
    const val NOT_SUPPORTED = "Not supported"
    const val FOLLOW_FLEET_DEFAULT = "Follow fleet default"
    const val NO_FLEET_DEFAULT = "Keep each drone's startup profile"
    const val MAX_PROFILES = 24
    const val MAX_DRONES = 64
    const val NOT_CONNECTED_NOTE = "Not connected to the ground: profile changes are off."
    const val NOT_LIVE_NOTE = "Not live: profile changes are off until the ground sends a fresh status."
    const val CONTROLS_OFF_NOTE = "The ground has detection profile changes turned off."
    const val UNSUPPORTED_EDGE =
        "This edge does not report detection profiles (older edge); it keeps its own model."

    /** The dashboard's `profileLabel`: the profile's label, else its name, else `--`. */
    fun profileLabel(profiles: DetectionProfiles, name: String?): String {
        val p = profiles.profiles.firstOrNull { it.name == name }
        p?.label?.let { return GroundText.shorten(it, 80) }
        if (!name.isNullOrEmpty()) return GroundText.shorten(name, 80)
        return DisplayText.DASH
    }

    const val DETECTOR_FAILING = "DETECTOR FAILING: the edge cannot process frames. "

    /**
     * The status line under a drone's profile selector: the dashboard's `profileStatus`, which puts
     * [DETECTOR_FAILING] in front of the status and marks it bad when `detector_ok` is false.
     */
    fun profileStatus(d: DroneProfileState, profiles: DetectionProfiles): Pair<String, Severity> {
        val (text, severity) = profileStatusCore(d, profiles)
        if (d.detectorOk == false) return DETECTOR_FAILING + text to Severity.BAD
        return text to severity
    }

    /** The dashboard's `profileStatusCore`. */
    fun profileStatusCore(d: DroneProfileState, profiles: DetectionProfiles): Pair<String, Severity> {
        fun label(n: String?) = profileLabel(profiles, n)
        if (d.supported != true) return UNSUPPORTED_EDGE to Severity.WARN
        if (d.pending == true) return "Switching to ${label(d.requested)}..." to Severity.INFO
        val r = d.refusal
        if (r != null) {
            val reason = r.reason?.raw ?: ""
            val detail = GroundText.shorten(r.detail ?: "")
            if (d.mismatch == true && reason == "in_flight") {
                return "MISMATCH: chosen ${label(d.desired)}, edge runs ${label(d.active)}. " +
                    "Refused in flight; sent again when the drone is on the ground." to Severity.BAD
            }
            val why = if (reason == "in_flight") "refused in flight" else "refused (${GroundText.shorten(reason, 60)})"
            return "Active: ${label(d.active)}. Switch $why: $detail" to Severity.BAD
        }
        when (d.note) {
            "no_reply" -> return "Active: ${label(d.active)}. No reply from the edge; try again." to Severity.WARN
            "not_connected" -> return "Active: ${label(d.active)}. Not connected; the choice is sent again when it reconnects." to Severity.WARN
            "send_failed" -> return "Active: ${label(d.active)}. The command could not be sent." to Severity.BAD
        }
        if (d.fallback == true) {
            val asked = d.requested?.takeIf { it.isNotEmpty() }?.let { label(it) } ?: "the requested profile"
            val code = d.fallbackReason?.raw
            val why = if (code == "weights_missing") {
                "weights missing on the edge"
            } else {
                "could not be used (${GroundText.shorten(code?.takeIf { it.isNotEmpty() } ?: "unknown", 60)})"
            }
            return "Active: ${label(d.active)} (fallback: $asked $why)." to Severity.WARN
        }
        val air = if (d.flying == true) " In flight: profile changes will be refused." else ""
        if (d.mismatch == true) {
            return "MISMATCH: chosen ${label(d.desired)}, edge runs ${label(d.active)}. " +
                "Sent again when the drone is on the ground or reconnects.$air" to Severity.WARN
        }
        return "Active: ${label(d.active)}.$air" to Severity.INFO
    }

    /**
     * Why profile selectors cannot be used right now, or null when they can: not connected, the
     * state is not live, or the ground says controls are off (`controls_enabled` is not `true`).
     */
    fun profileBlockedReason(profiles: DetectionProfiles, isStale: Boolean, connected: Boolean): String? = when {
        !connected -> NOT_CONNECTED_NOTE
        isStale -> NOT_LIVE_NOTE
        profiles.controlsEnabled != true ->
            profiles.controlsNote?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it) } ?: CONTROLS_OFF_NOTE
        else -> null
    }

    /** The card for [profiles], or null when the ground has no profile board (hide the section). */
    fun profilesCard(profiles: DetectionProfiles?, isStale: Boolean, connected: Boolean): ProfilesCardUi? {
        if (profiles == null || !profiles.available) return null
        val named = profiles.profiles.filter { !it.name.isNullOrEmpty() }
        val shownProfiles = named.take(MAX_PROFILES)
        val blocked = profileBlockedReason(profiles, isStale, connected)
        fun options(current: String?, noneLabel: String): List<ProfileOption> =
            listOf(ProfileOption("", noneLabel, current.isNullOrEmpty())) +
                shownProfiles.map { ProfileOption(it.name!!, profileLabel(profiles, it.name), it.name == current) }
        val fleetText = "Fleet default: " +
            (profiles.fleetDefault?.takeIf { it.isNotEmpty() }?.let { profileLabel(profiles, it) } ?: "none")
        val rows = profiles.drones.entries.take(MAX_DRONES).map { (id, d) ->
            val (text, severity) = profileStatus(d, profiles)
            val supported = d.supported == true
            DroneProfileRow(
                id = id,
                displayId = GroundText.shortId(id),
                statusText = text,
                severity = severity,
                supported = supported,
                options = if (supported) options(d.override, FOLLOW_FLEET_DEFAULT) else emptyList(),
                canSelect = supported && blocked == null && d.pending != true,
            )
        }
        return ProfilesCardUi(
            fleetDefaultText = fleetText,
            fleetOptions = options(profiles.fleetDefault, NO_FLEET_DEFAULT),
            blockedNote = blocked,
            canSelect = blocked == null,
            drones = rows,
            partial = named.size > MAX_PROFILES || profiles.drones.size > MAX_DRONES,
        )
    }

    // -------------------------------------------------------------- selection texts

    fun profileNotSent(target: String?, why: String): String =
        (if (target == null) "Fleet default profile was NOT changed: " else "Profile for ${GroundText.shortId(target)} was NOT changed: ") + why

    fun profileSent(target: String?, label: String): String =
        (if (target == null) "Fleet default profile change sent: $label." else "Profile change sent for ${GroundText.shortId(target)}: $label.") +
            " Waiting for the ground's answer."

    const val WHY_PENDING = "a switch for this drone is still waiting for the edge."
    const val PROFILE_NO_ANSWER = "Profile change: no answer from the ground yet. Check the active profile."
    /** Shown on the read-only cards while the state is not live. */
    const val NOT_LIVE_VALUES = "Not live: last values from the ground."

    fun notLiveNote(isStale: Boolean): String? = if (isStale) NOT_LIVE_VALUES else null

    const val WHY_NOT_CONNECTED = "this phone is not connected to the ground."
    const val WHY_SEND_FAILED = "the connection failed while sending."
    const val WHY_NO_USABLE_ID = "this drone has no usable id."
    const val WHY_UNKNOWN_PROFILE = "the ground does not list that profile."
    const val WHY_UNSUPPORTED = "this drone's edge does not report detection profiles."
    const val WHY_NO_BOARD = "the ground has no detection profile board."
    const val WHY_UNKNOWN_DRONE = "the ground does not list this drone."
    const val WHY_LINK_LOST = "the connection was lost before the ground answered. Check the active profile."

    // -------------------------------------------------------------- ceiling

    data class CeilingDroneRow(
        val id: String,
        val displayId: String,
        /** "Slot 100 cm", or "Slot 120 cm (own; default 130 cm)". */
        val slotText: String,
        val problem: String?,
        val edgeText: String,
        val edgeSeverity: Severity,
    )

    data class CeilingCardUi(
        val headline: String,
        val maxSlotText: String,
        /** "Locked in flight: <reason>", or null. */
        val lockedText: String?,
        val loadErrorText: String?,
        val drones: List<CeilingDroneRow>,
    )

    /** The edge sync line of one drone (`ceilingEdgeText` in the dashboard's `ceiling_settings.html`). */
    fun ceilingEdgeText(d: CeilingDrone): Pair<String, Severity> {
        val e = d.edge
        if (e == null || e.supported != true) {
            return "Edge does not report a ceiling (older edge): it flies the planned slot only." to Severity.INFO
        }
        if (e.pending == true) return "Sending to the edge..." to Severity.INFO
        val r = e.refusal
        if (r != null) {
            val why = r.detail?.takeIf { it.isNotEmpty() } ?: r.reason?.raw ?: ""
            return "Edge refused: ${GroundText.shorten(why)}" to Severity.WARN
        }
        e.note?.takeIf { it.isNotEmpty() }?.let { return "Edge: ${GroundText.shorten(it.replace('_', ' '))}" to Severity.WARN }
        val s = "Edge: ceiling ${DisplayText.fmt(e.ceilingCm)}, margin ${DisplayText.fmt(e.marginCm)}, slot ${DisplayText.fmt(e.slotCm)} cm"
        return if (e.inSync == true) s to Severity.INFO else "$s (not yet applied)" to Severity.WARN
    }

    /** The read-only ceiling card, or null when the ground has no ceiling board (hide the section). */
    fun ceilingCard(c: CeilingSettings?): CeilingCardUi? {
        if (c == null || !c.available) return null
        val rows = c.drones.take(MAX_DRONES).map { d ->
            val id = d.droneId ?: ""
            val own = d.ownSlotCm
            val slot = "Slot ${DisplayText.fmt(d.slotCm)} cm" +
                if (own != null) " (own; default ${DisplayText.fmt(d.defaultSlotCm)} cm)" else ""
            val (edge, severity) = ceilingEdgeText(d)
            CeilingDroneRow(
                id = id,
                displayId = if (id.isEmpty()) DisplayText.DASH else GroundText.shortId(id),
                slotText = slot,
                problem = d.problem?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it) },
                edgeText = edge,
                edgeSeverity = severity,
            )
        }
        return CeilingCardUi(
            headline = "Ceiling ${DisplayText.fmt(c.ceilingCm)} cm, margin ${DisplayText.fmt(c.marginCm)} cm",
            maxSlotText = "Highest allowed slot: ${DisplayText.fmt(c.maxSlotCm)} cm (ceiling minus margin)",
            lockedText = if (c.editable == false) {
                "Locked in flight: " + (c.lockedReason?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it) } ?: "the ground gave no reason.")
            } else {
                null
            },
            loadErrorText = c.loadError?.takeIf { it.isNotEmpty() }?.let {
                "Saved settings could not be read (${GroundText.shorten(it)}). Plan and Start are refused until valid settings are saved on the ground."
            },
            drones = rows,
        )
    }

    // -------------------------------------------------------------- reasoning

    data class ReasoningCardUi(
        /** "Gemma (gemma4:latest)", or "Reasoning off: matches use ReID and face scores only". */
        val statusText: String,
        /** "Available: Gemma, Off", or null when the ground listed none. */
        val availableText: String?,
        /** "Cloud allowed" or "Cloud not allowed" (also when the ground did not say). */
        val cloudBadge: String,
        val cloudAllowed: Boolean,
        /** One "Claude: <reason>" per unavailable provider, as the ground wrote it. */
        val unavailableNotes: List<String>,
    )

    fun providerName(kind: ReasoningProviderKind): String = when (kind) {
        ReasoningProviderKind.Gemma -> "Gemma"
        ReasoningProviderKind.Claude -> "Claude"
        ReasoningProviderKind.Off -> "Off"
        is ReasoningProviderKind.Unknown -> GroundText.shorten(kind.raw, 40)
    }

    const val MAX_AVAILABLE = 8

    /** "Available: Gemma, Off", cut to [MAX_AVAILABLE] distinct entries and "+N more" before any text is built. */
    fun availableText(available: List<ReasoningProviderKind>): String? {
        if (available.isEmpty()) return null
        // Raw names are compared before shortening, so a flood of entries costs one pass and no regex work.
        val distinct = available.distinctBy { it.raw }
        val more = distinct.size - MAX_AVAILABLE
        val shown = distinct.take(MAX_AVAILABLE).joinToString(", ") { providerName(it) }
        return "Available: $shown" + if (more > 0) " +$more more" else ""
    }

    fun reasoningCard(r: ReasoningProvider?): ReasoningCardUi? {
        if (r == null) return null
        val status = when {
            r.provider == ReasoningProviderKind.Off -> "Reasoning off: matches use ReID and face scores only"
            r.provider == null -> "Provider not reported"
            else -> {
                val label = r.label?.takeIf { it.isNotEmpty() }?.let { GroundText.shorten(it, 80) } ?: providerName(r.provider!!)
                val model = r.model?.takeIf { it.isNotEmpty() }?.let { " (${GroundText.shorten(it, 80)})" } ?: ""
                label + model
            }
        }
        return ReasoningCardUi(
            statusText = status,
            availableText = availableText(r.available),
            cloudBadge = if (r.cloudAllowed == true) "Cloud allowed" else "Cloud not allowed",
            cloudAllowed = r.cloudAllowed == true,
            unavailableNotes = r.unavailable.entries.take(8).map { (k, v) -> "${GroundText.shorten(k.replaceFirstChar { c -> c.uppercase() }, 40)}: ${GroundText.shorten(v)}" },
        )
    }

    // -------------------------------------------------------------- plan canvas

    /** The plan canvas as plain data, so the empty and not-live states are testable without a view. Read only. */
    @androidx.compose.runtime.Immutable
    data class PlanCanvasUi(
        val drawing: PlanDrawing?,
        /** True when the ground state is not live. */
        val isStale: Boolean,
        /** False until a `fleet_plan` arrives in the current session. */
        val planIsCurrent: Boolean,
    ) {
        val isEmpty: Boolean get() = drawing?.isEmpty ?: true

        /** True when the plan must not read as the live plan. */
        val isNotLive: Boolean get() = isStale || !planIsCurrent

        /** "Search plan", "Last plan" while stale, "Last plan (not confirmed this session)" until the ground sends a plan. */
        val title: String
            get() = when {
                !planIsCurrent -> LAST_PLAN_UNCONFIRMED
                isStale -> "Last plan"
                else -> "Search plan"
            }

        val gapNote: String? get() = drawing?.gapCm?.let { "Gap between zones: ${cm(it)}" }

        /** Notes about data that could not be drawn as given, or null when everything was drawn. */
        val skippedNote: String?
            get() {
                val d = drawing ?: return null
                val parts = ArrayList<String>()
                if (d.skippedLanes > 0) {
                    parts += "${d.skippedLanes} lane${if (d.skippedLanes == 1) "" else "s"} not drawn (unknown direction or distance)"
                }
                if (d.skippedUncovered > 0) {
                    parts += "${d.skippedUncovered} uncovered entr${if (d.skippedUncovered == 1) "y" else "ies"} not drawn"
                }
                for (id in d.takeoffFallbackDroneIds.take(MAX_DISTINCT_REASONS)) {
                    parts += "takeoff point out of range for ${GroundText.shortId(id)}; drawn from the zone corner"
                }
                if (d.takeoffFallbackDroneIds.size > MAX_DISTINCT_REASONS) {
                    parts += "and ${d.takeoffFallbackDroneIds.size - MAX_DISTINCT_REASONS} more with a takeoff point out of range"
                }
                var text = if (parts.isEmpty()) "" else parts.joinToString("; ") + "."
                if (d.truncated) text += (if (text.isEmpty()) "" else " ") + PARTIAL_NOTE
                return text.ifEmpty { null }
            }

        /** Up to three distinct reasons the ground gave. */
        val laneReasons: List<String> get() = distinct(drawing?.uncoveredLanes?.map { it.reason } ?: emptyList())
        val areaReasons: List<String> get() = distinct(drawing?.uncoveredAreas?.map { it.reason } ?: emptyList())

        val accessibilitySummary: String
            get() {
                val d = drawing
                if (d == null || d.isEmpty) return EMPTY_TEXT
                val lost = d.zones.count { it.lost }
                val sb = StringBuilder("$title: ${d.zones.size} zone${if (d.zones.size == 1) "" else "s"}")
                if (lost > 0) sb.append(", $lost lost")
                if (d.uncoveredLanes.isNotEmpty()) sb.append(", ${d.uncoveredLanes.size} ${UNCOVERED_LEGEND.lowercase()}")
                if (d.uncoveredAreas.isNotEmpty()) sb.append(", ${d.uncoveredAreas.size} ${AREA_LEGEND.lowercase()}")
                gapNote?.let { sb.append(". ").append(it) }
                skippedNote?.let { sb.append(". ").append(it) }
                return sb.toString()
            }
    }

    const val EMPTY_TEXT = "No plan yet. Plan from the dashboard."
    const val UNCOVERED_LEGEND = "Not searched"
    const val AREA_LEGEND = "May not be covered (camera model)"
    const val PARTIAL_NOTE = "Only part of the plan is drawn."
    const val LAST_PLAN_UNCONFIRMED = "Last plan (not confirmed this session)"
    const val LOST_LEGEND = "LOST: the drone is lost; its zone is greyed"
    const val MAX_DISTINCT_REASONS = 3

    private fun distinct(reasons: List<String?>): List<String> {
        val out = ArrayList<String>()
        for (r in reasons) {
            if (r.isNullOrEmpty()) continue
            val s = GroundText.shorten(r)
            if (s !in out) out += s
            if (out.size == MAX_DISTINCT_REASONS) break
        }
        return out
    }

    /** "100 cm", or "37.5 cm". */
    fun cm(v: Double): String = if (v == Math.rint(v) && Math.abs(v) < 1e15) "${v.toLong()} cm" else "%.1f cm".format(java.util.Locale.ROOT, v)

    fun planCanvas(plan: FleetPlan?, isStale: Boolean, planIsCurrent: Boolean, drawing: PlanDrawing? = plan?.let(PlanGeometry::drawing)): PlanCanvasUi =
        PlanCanvasUi(drawing, isStale, planIsCurrent)

    /**
     * The dashboard's per-drone colours (`DRONE_COLORS` in `ui.js`) are picked by the number at the
     * end of the drone id.
     */
    fun colorIndex(droneId: String, count: Int = PlanColors.droneColorCount): Int {
        val digits = droneId.takeLastWhile { it in '0'..'9' }
        val n = digits.toIntOrNull() ?: return 0
        if (n < 1) return 0
        return (n - 1) % count
    }
}

/**
 * The plan canvas colours. The canvas is drawn on its own background ([lightBackground],
 * [darkBackground]) so that every colour below keeps at least 4.5:1 contrast on it in both
 * themes; a unit test checks each pair.
 */
object PlanColors {
    val lightBackground = Color(0xFFFFFFFF)
    val darkBackground = Color(0xFF1A1C1E)

    /** The dashboard's drone colours: the light variants are darker so labels reach 4.5:1 on white. */
    val droneLight = listOf(Color(0xFF0369A1), Color(0xFFB45309), Color(0xFF6D28D9), Color(0xFF047857))
    val droneDark = listOf(Color(0xFF0EA5E9), Color(0xFFF59E0B), Color(0xFFA78BFA), Color(0xFF10B981))
    val droneColorCount: Int get() = droneLight.size

    val warningLight = Color(0xFFC2410C)
    val warningDark = Color(0xFFFB923C)
    val lostLight = Color(0xFF52525B)
    val lostDark = Color(0xFFA1A1AA)
    val textLight = Color(0xFF1A1C1E)
    val textDark = Color(0xFFE3E2E6)
    val secondaryTextLight = Color(0xFF44474E)
    val secondaryTextDark = Color(0xFFC4C6D0)

    /** Every (foreground, background) pair the canvas draws text or strokes with. */
    val pairs: List<Triple<String, Color, Color>>
        get() = buildList {
            droneLight.forEachIndexed { i, c -> add(Triple("drone ${i + 1} light", c, lightBackground)) }
            droneDark.forEachIndexed { i, c -> add(Triple("drone ${i + 1} dark", c, darkBackground)) }
            add(Triple("warning light", warningLight, lightBackground))
            add(Triple("warning dark", warningDark, darkBackground))
            add(Triple("lost light", lostLight, lightBackground))
            add(Triple("lost dark", lostDark, darkBackground))
            add(Triple("text light", textLight, lightBackground))
            add(Triple("text dark", textDark, darkBackground))
            add(Triple("secondary text light", secondaryTextLight, lightBackground))
            add(Triple("secondary text dark", secondaryTextDark, darkBackground))
        }

    /** Strokes and fills are dimmed to this opacity while the plan is not live; labels stay opaque. */
    const val DIM_ALPHA = 0.8f

    /** The colour of a zone label: always opaque, live or not. */
    fun label(c: Color): Color = c

    fun drone(index: Int, dark: Boolean): Color = (if (dark) droneDark else droneLight)[index.coerceIn(0, droneColorCount - 1)]
    fun warning(dark: Boolean): Color = if (dark) warningDark else warningLight
    fun lost(dark: Boolean): Color = if (dark) lostDark else lostLight
    fun text(dark: Boolean): Color = if (dark) textDark else textLight
    fun secondaryText(dark: Boolean): Color = if (dark) secondaryTextDark else secondaryTextLight
    fun background(dark: Boolean): Color = if (dark) darkBackground else lightBackground
}
