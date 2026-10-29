package com.flightrisk.contract

import kotlinx.serialization.json.JsonObject

/** A refusal from an edge: a reason code and a sentence for the operator. */
data class Refusal(val reason: ReasonCode?, val detail: String?) {
    companion object {
        fun fromJson(o: JsonObject): Refusal = Refusal(o.str("reason")?.let(::ReasonCode), o.str("detail"))
    }
}

/**
 * The `detection_profiles` event (`detection_profiles.json`). Without the
 * edge source the ground sends `{available: false, profiles: [], drones: {},
 * fleet_default: null}`; [available] is then false.
 */
data class DetectionProfiles(
    /** False only when the ground says `available: false`; absent means available. */
    val available: Boolean,
    val profiles: List<DetectionProfile>,
    val fleetDefault: String?,
    /** Keyed by drone id, sorted by id. */
    val drones: Map<String, DroneProfileState>,
    val controlsEnabled: Boolean?,
    val controlsNote: String?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): DetectionProfiles = DetectionProfiles(
            available = o.bool("available") != false,
            profiles = o.objects("profiles", DetectionProfile::fromJson) ?: emptyList(),
            fleetDefault = o.str("fleet_default"),
            drones = o.keyedObjects("drones") { id, d -> DroneProfileState.fromJson(id, d) } ?: emptyMap(),
            controlsEnabled = o.bool("controls_enabled"),
            controlsNote = o.str("controls_note"),
        )
    }
}

/** One registered detection profile. */
data class DetectionProfile(
    val name: String?,
    val label: String?,
    val description: String?,
    val view: String?,
    val weights: String?,
    val weightsRequired: Boolean?,
    val confidence: Double?,
    val tiled: Boolean?,
    val minPersonPx: Double?,
    val fallback: String?,
    val faceWeightScale: Double?,
    val reidWeightScale: Double?,
) {
    companion object {
        fun fromJson(o: JsonObject): DetectionProfile = DetectionProfile(
            name = o.str("name"),
            label = o.str("label"),
            description = o.str("description"),
            view = o.str("view"),
            weights = o.str("weights"),
            weightsRequired = o.bool("weights_required"),
            confidence = o.num("confidence"),
            tiled = o.bool("tiled"),
            minPersonPx = o.num("min_person_px"),
            fallback = o.str("fallback"),
            faceWeightScale = o.num("face_weight_scale"),
            reidWeightScale = o.num("reid_weight_scale"),
        )
    }
}

/** One drone's detection profile state. */
data class DroneProfileState(
    val id: String,
    val droneId: String?,
    val supported: Boolean?,
    val active: String?,
    val fallback: Boolean?,
    val fallbackReason: ReasonCode?,
    val requested: String?,
    val override: String?,
    val desired: String?,
    val mismatch: Boolean?,
    val pending: Boolean?,
    val refusal: Refusal?,
    val note: String?,
    val flying: Boolean?,
    val switchable: Boolean?,
    val detectorOk: Boolean?,
) {
    companion object {
        fun fromJson(id: String, o: JsonObject): DroneProfileState = DroneProfileState(
            id = id,
            droneId = o.str("drone_id"),
            supported = o.bool("supported"),
            active = o.str("active"),
            fallback = o.bool("fallback"),
            fallbackReason = o.str("fallback_reason")?.let(::ReasonCode),
            requested = o.str("requested"),
            override = o.str("override"),
            desired = o.str("desired"),
            mismatch = o.bool("mismatch"),
            pending = o.bool("pending"),
            refusal = o.obj("refusal")?.let(Refusal::fromJson),
            note = o.str("note"),
            flying = o.bool("flying"),
            switchable = o.bool("switchable"),
            detectorOk = o.bool("detector_ok"),
        )
    }
}

/**
 * The `ceiling_settings` event (`ceiling_settings.json`). Read only on a
 * phone. Without the edge source the ground sends `{available: false}`.
 */
data class CeilingSettings(
    val available: Boolean,
    val ceilingCm: Double?,
    val marginCm: Double?,
    val maxSlotCm: Double?,
    val limits: CeilingLimits?,
    /** A list, not a map, in drone order from the ground. */
    val drones: List<CeilingDrone>,
    val editable: Boolean?,
    val lockedReason: String?,
    val loadError: String?,
    val file: String?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): CeilingSettings = CeilingSettings(
            available = o.bool("available") != false,
            ceilingCm = o.num("ceiling_cm"),
            marginCm = o.num("margin_cm"),
            maxSlotCm = o.num("max_slot_cm"),
            limits = o.obj("limits")?.let(CeilingLimits::fromJson),
            drones = o.objects("drones", CeilingDrone::fromJson) ?: emptyList(),
            editable = o.bool("editable"),
            lockedReason = o.str("locked_reason"),
            loadError = o.str("load_error"),
            file = o.str("file"),
        )
    }
}

data class CeilingLimits(
    val minCeilingCm: Double?,
    val maxCeilingCm: Double?,
    val minMarginCm: Double?,
    val minSlotCm: Double?,
    val maxSlotCm: Double?,
) {
    companion object {
        fun fromJson(o: JsonObject): CeilingLimits = CeilingLimits(
            minCeilingCm = o.num("min_ceiling_cm"),
            maxCeilingCm = o.num("max_ceiling_cm"),
            minMarginCm = o.num("min_margin_cm"),
            minSlotCm = o.num("min_slot_cm"),
            maxSlotCm = o.num("max_slot_cm"),
        )
    }
}

/** One drone's altitude slot and the edge's sync state. */
data class CeilingDrone(
    val droneId: String?,
    val slotCm: Double?,
    val ownSlotCm: Double?,
    val defaultSlotCm: Double?,
    val problem: String?,
    val edge: CeilingEdgeState?,
) {
    companion object {
        fun fromJson(o: JsonObject): CeilingDrone = CeilingDrone(
            droneId = o.str("drone_id"),
            slotCm = o.num("slot_cm"),
            ownSlotCm = o.num("own_slot_cm"),
            defaultSlotCm = o.num("default_slot_cm"),
            problem = o.str("problem"),
            edge = o.obj("edge")?.let(CeilingEdgeState::fromJson),
        )
    }
}

data class CeilingEdgeState(
    val supported: Boolean?,
    val ceilingCm: Double?,
    val marginCm: Double?,
    val slotCm: Double?,
    val flying: Boolean?,
    val inSync: Boolean?,
    val pending: Boolean?,
    val refusal: Refusal?,
    val note: String?,
) {
    companion object {
        fun fromJson(o: JsonObject): CeilingEdgeState = CeilingEdgeState(
            supported = o.bool("supported"),
            ceilingCm = o.num("ceiling_cm"),
            marginCm = o.num("margin_cm"),
            slotCm = o.num("slot_cm"),
            flying = o.bool("flying"),
            inSync = o.bool("in_sync"),
            pending = o.bool("pending"),
            refusal = o.obj("refusal")?.let(Refusal::fromJson),
            note = o.str("note"),
        )
    }
}

/** The `reasoning_provider` event (`reasoning_provider.json`). Read only on a phone. */
data class ReasoningProvider(
    val provider: ReasoningProviderKind?,
    val label: String?,
    val model: String?,
    val available: List<ReasoningProviderKind>,
    /** Provider name to the reason it is unavailable, sorted by name. */
    val unavailable: Map<String, String>,
    val cloudAllowed: Boolean?,
) : ServerEvent {
    companion object {
        fun fromJson(o: JsonObject): ReasoningProvider = ReasoningProvider(
            provider = o.str("provider")?.let(ReasoningProviderKind::of),
            label = o.str("label"),
            model = o.str("model"),
            available = o.strings("available")?.map(ReasoningProviderKind::of) ?: emptyList(),
            unavailable = o.obj("unavailable")?.let { u ->
                u.entries.mapNotNull { (k, v) -> v.stringValue()?.let { k to it } }.toMap(sortedMapOf())
            } ?: emptyMap(),
            cloudAllowed = o.bool("cloud_allowed"),
        )
    }
}
