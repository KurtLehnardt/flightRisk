package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/**
 * The `fleet_plan` event (`fleet_plan.json`). All positions are centimetres in
 * the plan frame shared by every drone (`grid`). The map editor's own
 * `details` block is not decoded: a phone draws from `zones`, `grid` and
 * `uncovered` only (docs/mobile-contract.md).
 */
data class FleetPlan(
    val zones: List<PlanZone>,
    val grid: PlanRect?,
    val gapCm: Double?,
    val stripCm: Double?,
    val uncovered: List<UncoveredEntry>,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): FleetPlan = FleetPlan(
            zones = o.objects("zones", PlanZone::fromJson) ?: emptyList(),
            grid = o.obj("grid")?.let(PlanRect::fromJson),
            gapCm = o.num("gap_cm"),
            stripCm = o.num("strip_cm"),
            uncovered = o.objects("uncovered", UncoveredEntry::fromJson) ?: emptyList(),
        )
    }
}

/** A rectangle in the plan frame, in centimetres. */
data class PlanRect(val xCm: Double?, val yCm: Double?, val wCm: Double?, val hCm: Double?) {
    companion object {
        fun fromJson(o: JsonObject): PlanRect = PlanRect(o.num("x_cm"), o.num("y_cm"), o.num("w_cm"), o.num("h_cm"))
    }
}

/**
 * One drone's zone. The lanes start at the zone's takeoff mark
 * (`takeoff_x_cm`/`takeoff_y_cm` when present, else `x_cm`/`y_cm`).
 */
data class PlanZone(
    val droneId: String?,
    val xCm: Double?,
    val yCm: Double?,
    val wCm: Double?,
    val hCm: Double?,
    val takeoffXCm: Double?,
    val takeoffYCm: Double?,
    val altCm: Double?,
    val lanes: List<PlanLane>,
    val lost: Boolean?,
) {
    companion object {
        fun fromJson(o: JsonObject): PlanZone = PlanZone(
            droneId = o.str("drone_id"),
            xCm = o.num("x_cm"),
            yCm = o.num("y_cm"),
            wCm = o.num("w_cm"),
            hCm = o.num("h_cm"),
            takeoffXCm = o.num("takeoff_x_cm"),
            takeoffYCm = o.num("takeoff_y_cm"),
            altCm = o.num("alt_cm"),
            lanes = o.objects("lanes", PlanLane::fromJson) ?: emptyList(),
            lost = o.bool("lost"),
        )
    }
}

/** One leg of a zone's path: a direction and a distance. */
data class PlanLane(val direction: LaneDirection?, val distanceCm: Double?) {
    companion object {
        fun fromJson(o: JsonObject): PlanLane = PlanLane(
            direction = o.str("direction")?.let(LaneDirection::of),
            distanceCm = o.num("distance_cm"),
        )
    }
}

/**
 * An entry of `fleet_plan.uncovered`. Entries without a `kind` key are
 * re-tasked lanes nobody took; `kind: "area"` entries are coverage gaps of a
 * map plan; any other kind is kept by name.
 */
sealed interface UncoveredEntry {
    val reason: String?

    data class Lane(val lane: RetaskLane) : UncoveredEntry {
        override val reason: String? get() = lane.reason
    }

    data class Area(val rect: PlanRect, override val reason: String?) : UncoveredEntry

    data class Unknown(val kind: String, override val reason: String?) : UncoveredEntry

    companion object {
        fun fromJson(o: JsonObject): UncoveredEntry {
            if (!o.containsKey("kind")) return Lane(RetaskLane.fromJson(o))
            return when (val kind = o.str("kind")) {
                "area" -> Area(PlanRect.fromJson(o), o.str("reason"))
                else -> Unknown(kind ?: "", o.str("reason"))
            }
        }
    }
}

/** The `fleet_detection` event: a person seen by a fleet drone, placed on the plan (`fleet_detection.json`). */
data class FleetDetection(
    val id: Long?,
    val droneId: String?,
    val droneIds: List<String>,
    val count: Int?,
    val xCm: Double?,
    val yCm: Double?,
    val relXCm: Double?,
    val relYCm: Double?,
    /** Base64 JPEG, or null. */
    val thumbnail: String?,
    val lat: Double?,
    val lon: Double?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): FleetDetection = FleetDetection(
            id = o.long("id"),
            droneId = o.str("drone_id"),
            droneIds = o.strings("drone_ids") ?: emptyList(),
            count = o.int("count"),
            xCm = o.num("x_cm"),
            yCm = o.num("y_cm"),
            relXCm = o.num("rel_x_cm"),
            relYCm = o.num("rel_y_cm"),
            thumbnail = o.str("thumbnail"),
            lat = o.num("lat"),
            lon = o.num("lon"),
        )
    }
}

/**
 * The `frame` event (`frame.json`): one video frame or detection overlay per
 * drone. A phone does not show per-drone video, but it decodes the event so
 * that the overlay and telemetry are available.
 */
data class Frame(
    /** Base64 JPEG, or null when the ground streams detections only. */
    val image: String?,
    val boxes: List<FrameBox>,
    val fps: Double?,
    val persons: Int?,
    val match: Boolean?,
    val matchScore: Double?,
    val streamVideo: Boolean?,
    val droneId: String?,
    val frameWidth: Int?,
    val frameHeight: Int?,
    val telemetry: FrameTelemetry?,
    val recording: Boolean?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): Frame = Frame(
            image = o.str("image"),
            boxes = o.objects("boxes", FrameBox::fromJson) ?: emptyList(),
            fps = o.num("fps"),
            persons = o.int("persons"),
            match = o.bool("match"),
            matchScore = o.num("match_score"),
            streamVideo = o.bool("stream_video"),
            droneId = o.str("drone_id"),
            frameWidth = o.int("frame_width"),
            frameHeight = o.int("frame_height"),
            telemetry = o.obj("telemetry")?.let(FrameTelemetry::fromJson),
            recording = o.bool("recording"),
        )
    }
}

/** A detection box in normalised `[x1, y1, x2, y2]` coordinates (0..1). */
data class FrameBox(
    val bboxNorm: List<Double>,
    val score: Double?,
    val matched: Boolean?,
    /** A number or a string on the wire; kept as text. */
    val trackId: String?,
) {
    companion object {
        fun fromJson(o: JsonObject): FrameBox = FrameBox(
            bboxNorm = o.numbers("bbox_norm") ?: emptyList(),
            score = o.num("score"),
            matched = o.bool("matched"),
            trackId = o.str("track_id") ?: o.num("track_id")?.let(::jsNumber),
        )
    }
}

/** The telemetry the dashboard reads from a frame (`ui.js`: battery, height, temperature). */
data class FrameTelemetry(val battery: Double?, val height: Double?, val temperature: Double?) {
    companion object {
        fun fromJson(o: JsonObject): FrameTelemetry =
            FrameTelemetry(o.num("battery"), o.num("height"), o.num("temperature"))
    }
}

/**
 * The `match_alert` and `match_alert_reminder` events (`match_alert.json`). A
 * phone shows the alert and fires its own haptic and sound; the operator
 * answers on the laptop.
 */
data class MatchAlert(
    /** True for `match_alert_reminder`, sent on connect while an alert waits. */
    val isReminder: Boolean,
    val alertId: String?,
    val time: String?,
    val score: Double?,
    val faceScore: Double?,
    val reidScore: Double?,
    val alertLevel: AlertLevel?,
    /** Base64 JPEG, or null. */
    val snapshot: String?,
    val trackId: String?,
    val type: String?,
    val reasoning: String?,
    val renotify: Boolean?,
    val renotifyCount: Int?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject, isReminder: Boolean = false): MatchAlert = MatchAlert(
            isReminder = isReminder,
            alertId = o.str("alert_id") ?: o.num("alert_id")?.let(::jsNumber),
            time = o.str("time"),
            score = o.num("score"),
            faceScore = o.num("face_score"),
            reidScore = o.num("reid_score"),
            alertLevel = o.str("alert_level")?.let(AlertLevel::of),
            snapshot = o.str("snapshot"),
            trackId = o.str("track_id") ?: o.num("track_id")?.let(::jsNumber),
            type = o.str("type"),
            reasoning = o.str("reasoning"),
            renotify = o.bool("renotify"),
            renotifyCount = o.int("renotify_count"),
        )
    }
}
