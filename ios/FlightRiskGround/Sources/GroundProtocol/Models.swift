import Foundation

// Lenient models for every dashboard payload a phone reads (docs/mobile-contract.md).
// Rules: every field is optional, unknown keys are ignored at every depth, a field with an
// unexpected type decodes to nil, and unknown enum values map to `.unknown(String)`.
// A missing or null value means "not reported" and is shown as a dash, never as zero.

// MARK: - status

/// `status`: sent once per connect. `fleet.drones` is a COUNT here, unlike `fleet_status.drones`.
public struct StatusPayload: Codable, Equatable, Sendable {
    @Lenient public var connected: Bool?
    @Lenient public var source: String?
    @Lenient public var appearanceOnly: Bool?
    @Lenient public var tiled: Bool?
    @Lenient public var fleet: FleetSummary?

    enum CodingKeys: String, CodingKey {
        case connected, source, tiled, fleet
        case appearanceOnly = "appearance_only"
    }

    /// `fleet.drones > 0` means fleet mode. Nil when the ground did not say.
    public var isFleetMode: Bool? { fleet?.drones.map { $0 > 0 } }
}

public struct FleetSummary: Codable, Equatable, Sendable {
    /// A count of drones, not a map.
    @Lenient public var drones: Int?
    @Lenient public var net: String?
    @Lenient public var synced: Bool?
}

// MARK: - fleet_status

/// One drone as `fleet_status` reports it. Carries both the fleet fields and the legacy
/// (non-fleet) fields; whichever the ground did not send is nil. `manual_status` entries share
/// `controlMode`, `edgeControlMode`, `manualLast`, `manualSupported` and `orphanLandInS`.
public struct DroneRecord: Codable, Equatable, Sendable {
    @Lenient public var droneId: String?
    @Lenient public var altCm: Double?
    @Lenient public var battery: Double?
    @Lenient public var heightCm: Double?
    @Lenient public var isFlying: Bool?
    @Lenient public var headingDeg: Double?
    @Lenient public var laneIndex: Int?
    @Lenient public var lanesTotal: Int?
    @Lenient public var synced: Bool?
    @Lenient public var acked: Bool?
    @Lenient public var lost: Bool?
    @Lenient public var droneLinkOk: Bool?
    @Lenient public var droneLinkDetail: String?
    @Lenient public var relXCm: Double?
    @Lenient public var relYCm: Double?
    @Lenient public var connected: Bool?
    @Lenient public var linkAgeS: Double?
    @Lenient public var controlMode: ControlMode?
    @Lenient public var edgeControlMode: ControlMode?
    @Lenient public var manualSupported: Bool?
    @Lenient public var manualLast: JSONValue?
    @Lenient public var orphanLandInS: Double?
    @Lenient public var offsetMs: Double?
    @Lenient public var rttMs: Double?
    @Lenient public var skewMs: Double?
    // Legacy (non-fleet) shape.
    @Lenient public var height: Double?
    @Lenient public var temperature: Double?
    @Lenient public var flightTime: Double?
    @Lenient public var isConnected: Bool?

    enum CodingKeys: String, CodingKey {
        case battery, synced, acked, lost, connected, height, temperature
        case droneId = "drone_id", altCm = "alt_cm", heightCm = "height_cm"
        case isFlying = "is_flying", headingDeg = "heading_deg", laneIndex = "lane_index"
        case lanesTotal = "lanes_total", droneLinkOk = "drone_link_ok"
        case droneLinkDetail = "drone_link_detail", relXCm = "rel_x_cm", relYCm = "rel_y_cm"
        case linkAgeS = "link_age_s", controlMode = "control_mode"
        case edgeControlMode = "edge_control_mode", manualSupported = "manual_supported"
        case manualLast = "manual_last", orphanLandInS = "orphan_land_in_s"
        case offsetMs = "offset_ms", rttMs = "rtt_ms", skewMs = "skew_ms"
        case flightTime = "flight_time", isConnected = "is_connected"
    }

    /// Height in cm as the dashboard shows it: `height_cm`, else the legacy `height`.
    public var displayHeightCm: Double? { heightCm ?? height }

    /// Overlay a `manual_status` entry, as the dashboard does with Object.assign: the five keys
    /// a `manual_status` entry always carries replace this record's values, null included.
    public func merging(manual entry: ManualDroneEntry) -> DroneRecord {
        var copy = self
        copy.controlMode = entry.controlMode
        copy.edgeControlMode = entry.edgeControlMode
        copy.manualLast = entry.manualLast
        copy.manualSupported = entry.manualSupported
        copy.orphanLandInS = entry.orphanLandInS
        return copy
    }
}

/// `fleet_status`. Fleet shape: `drones` keyed by id, `net`, `synced`, `manual_controlled`.
/// Legacy shape (no fleet): `{drones, count}` with legacy drone fields.
public struct FleetStatusPayload: Codable, Equatable, Sendable {
    @Lenient public var drones: [String: DroneRecord]?
    @Lenient public var net: String?
    @Lenient public var synced: Bool?
    @Lenient public var manualControlled: String?
    /// Present only in the legacy (non-fleet) shape.
    @Lenient public var count: Int?

    enum CodingKeys: String, CodingKey {
        case drones, net, synced, count
        case manualControlled = "manual_controlled"
    }

    /// True for the legacy `{drones, count}` shape.
    public var isLegacyShape: Bool { count != nil }
    /// Drone ids sorted for display.
    public var sortedDroneIds: [String] { (drones ?? [:]).keys.sorted() }
}

// MARK: - manual_status, manual_ack, manual_result

public struct ManualDroneEntry: Codable, Equatable, Sendable {
    @Lenient public var controlMode: ControlMode?
    @Lenient public var edgeControlMode: ControlMode?
    @Lenient public var manualLast: JSONValue?
    @Lenient public var manualSupported: Bool?
    @Lenient public var orphanLandInS: Double?

    enum CodingKeys: String, CodingKey {
        case controlMode = "control_mode", edgeControlMode = "edge_control_mode"
        case manualLast = "manual_last", manualSupported = "manual_supported"
        case orphanLandInS = "orphan_land_in_s"
    }
}

public struct ManualStatusPayload: Codable, Equatable, Sendable {
    /// The id of the drone under manual control, or nil.
    @Lenient public var controlled: String?
    @Lenient public var drones: [String: ManualDroneEntry]?
}

/// `manual_ack` and `manual_result` share one shape. `reason` is a refusal reason when `ok` is
/// false (`no_reply` when the edge did not answer in time).
public struct ManualReply: Codable, Equatable, Sendable {
    @Lenient public var droneId: String?
    @Lenient public var action: ManualAction?
    @Lenient public var ok: Bool?
    @Lenient public var reason: NackReason?
    @Lenient public var detail: String?
    /// Sent with a successful `resume_search` result.
    @Lenient public var controlMode: ControlMode?

    enum CodingKeys: String, CodingKey {
        case action, ok, reason, detail
        case droneId = "drone_id", controlMode = "control_mode"
    }
}
public typealias ManualAck = ManualReply
public typealias ManualResult = ManualReply

// MARK: - fleet_event

/// One lane in a `retask`, `retask_failed` or `uncovered` entry.
public struct PlanLane: Codable, Equatable, Sendable {
    @Lenient public var index: Int?
    @Lenient public var direction: LaneDirection?
    @Lenient public var distanceCm: Double?
    @Lenient public var xCm: Double?
    @Lenient public var yCm: Double?
    @Lenient public var endXCm: Double?
    @Lenient public var endYCm: Double?
    @Lenient public var droneId: String?
    @Lenient public var reason: String?

    enum CodingKeys: String, CodingKey {
        case index, direction, reason
        case distanceCm = "distance_cm", xCm = "x_cm", yCm = "y_cm"
        case endXCm = "end_x_cm", endYCm = "end_y_cm", droneId = "drone_id"
    }
}

/// `fleet_event`. The kind is `event`; like the dashboard, `type` and `kind` are accepted as
/// fallbacks. Use `resolvedKind`, which never fails and keeps unknown kinds as raw text.
public struct FleetEvent: Codable, Equatable, Sendable {
    @Lenient public var event: FleetEventKind?
    @Lenient public var type: FleetEventKind?
    @Lenient public var kind: FleetEventKind?
    @Lenient public var detail: String?
    @Lenient public var droneId: String?
    @Lenient public var droneIds: [String]?
    /// `retask`: lanes accepted per receiving drone.
    @Lenient public var to: [String: Int]?
    @Lenient public var lanes: [PlanLane]?
    @Lenient public var awaiting: [PlanLane]?

    enum CodingKeys: String, CodingKey {
        case event, type, kind, detail, to, lanes, awaiting
        case droneId = "drone_id", droneIds = "drone_ids"
    }

    public var resolvedKind: FleetEventKind? { event ?? type ?? kind }
}

// MARK: - fleet_plan

public struct PlanRect: Codable, Equatable, Sendable {
    @Lenient public var xCm: Double?
    @Lenient public var yCm: Double?
    @Lenient public var wCm: Double?
    @Lenient public var hCm: Double?

    enum CodingKeys: String, CodingKey {
        case xCm = "x_cm", yCm = "y_cm", wCm = "w_cm", hCm = "h_cm"
    }
}

public struct PlanZoneLane: Codable, Equatable, Sendable {
    @Lenient public var direction: LaneDirection?
    @Lenient public var distanceCm: Double?

    enum CodingKeys: String, CodingKey {
        case direction
        case distanceCm = "distance_cm"
    }
}

public struct PlanZone: Codable, Equatable, Sendable {
    @Lenient public var droneId: String?
    @Lenient public var xCm: Double?
    @Lenient public var yCm: Double?
    @Lenient public var wCm: Double?
    @Lenient public var hCm: Double?
    @Lenient public var takeoffXCm: Double?
    @Lenient public var takeoffYCm: Double?
    @Lenient public var altCm: Double?
    @Lenient public var lanes: [PlanZoneLane]?
    @Lenient public var lost: Bool?

    enum CodingKeys: String, CodingKey {
        case lanes, lost
        case droneId = "drone_id", xCm = "x_cm", yCm = "y_cm", wCm = "w_cm", hCm = "h_cm"
        case takeoffXCm = "takeoff_x_cm", takeoffYCm = "takeoff_y_cm", altCm = "alt_cm"
    }
}

/// An `uncovered` entry. Two kinds share the list: a re-tasked lane nobody took (no `kind`;
/// lane fields) and a coverage gap of a map plan (`kind == "area"`; rectangle fields).
public struct UncoveredEntry: Codable, Equatable, Sendable {
    /// Absent for lane entries, `"area"` for coverage gaps. Unknown kinds keep their text.
    @Lenient public var kind: String?
    @Lenient public var reason: String?
    // Lane entries.
    @Lenient public var index: Int?
    @Lenient public var direction: LaneDirection?
    @Lenient public var distanceCm: Double?
    @Lenient public var endXCm: Double?
    @Lenient public var endYCm: Double?
    @Lenient public var droneId: String?
    // Shared by both kinds.
    @Lenient public var xCm: Double?
    @Lenient public var yCm: Double?
    // Area entries.
    @Lenient public var wCm: Double?
    @Lenient public var hCm: Double?

    enum CodingKeys: String, CodingKey {
        case kind, reason, index, direction
        case distanceCm = "distance_cm", endXCm = "end_x_cm", endYCm = "end_y_cm"
        case droneId = "drone_id", xCm = "x_cm", yCm = "y_cm", wCm = "w_cm", hCm = "h_cm"
    }

    public var isArea: Bool { kind == "area" }
}

public struct PlanCoverage: Codable, Equatable, Sendable {
    @Lenient public var cellCm: Double?
    @Lenient public var footprintCm: Double?
    @Lenient public var fraction: Double?
    @Lenient public var model: String?
    @Lenient public var note: String?

    enum CodingKeys: String, CodingKey {
        case fraction, model, note
        case cellCm = "cell_cm", footprintCm = "footprint_cm"
    }
}

/// The map editor's own view of a plan. A phone may ignore it; the few fields a phone could
/// show are typed and everything else is dropped.
public struct PlanDetails: Codable, Equatable, Sendable {
    @Lenient public var ok: Bool?
    @Lenient public var mode: String?
    @Lenient public var pattern: String?
    @Lenient public var problems: [String]?
    @Lenient public var area: PlanRect?
    @Lenient public var coverage: PlanCoverage?
    @Lenient public var gapCm: Double?
    @Lenient public var stripCm: Double?
    @Lenient public var geofenceCm: Double?
    @Lenient public var snapToleranceCm: Double?
    @Lenient public var takeoffLayout: [String]?

    enum CodingKeys: String, CodingKey {
        case ok, mode, pattern, problems, area, coverage
        case gapCm = "gap_cm", stripCm = "strip_cm", geofenceCm = "geofence_cm"
        case snapToleranceCm = "snap_tolerance_cm", takeoffLayout = "takeoff_layout"
    }
}

public struct FleetPlanPayload: Codable, Equatable, Sendable {
    @Lenient public var grid: PlanRect?
    @Lenient public var zones: [PlanZone]?
    @Lenient public var gapCm: Double?
    @Lenient public var stripCm: Double?
    @Lenient public var uncovered: [UncoveredEntry]?
    @Lenient public var details: PlanDetails?

    enum CodingKeys: String, CodingKey {
        case grid, zones, uncovered, details
        case gapCm = "gap_cm", stripCm = "strip_cm"
    }
}

// MARK: - fleet_detection, frame, match_alert

public struct FleetDetection: Codable, Equatable, Sendable {
    @Lenient public var id: Int?
    @Lenient public var droneId: String?
    @Lenient public var droneIds: [String]?
    @Lenient public var count: Int?
    @Lenient public var xCm: Double?
    @Lenient public var yCm: Double?
    @Lenient public var relXCm: Double?
    @Lenient public var relYCm: Double?
    /// Base64 JPEG.
    @Lenient public var thumbnail: String?
    @Lenient public var lat: Double?
    @Lenient public var lon: Double?

    enum CodingKeys: String, CodingKey {
        case id, count, thumbnail, lat, lon
        case droneId = "drone_id", droneIds = "drone_ids", xCm = "x_cm", yCm = "y_cm"
        case relXCm = "rel_x_cm", relYCm = "rel_y_cm"
    }
}

/// A detection box. `bboxNorm` is `[x1, y1, x2, y2]` in 0...1.
public struct FrameBox: Codable, Equatable, Sendable {
    @Lenient public var bboxNorm: [Double]?
    @Lenient public var score: Double?
    @Lenient public var matched: Bool?
    @Lenient public var trackId: String?

    enum CodingKeys: String, CodingKey {
        case score, matched
        case bboxNorm = "bbox_norm", trackId = "track_id"
    }

    /// The four coordinates, or nil when the array is not exactly four numbers.
    public var corners: (x1: Double, y1: Double, x2: Double, y2: Double)? {
        guard let b = bboxNorm, b.count == 4 else { return nil }
        return (b[0], b[1], b[2], b[3])
    }
}

public struct FrameTelemetry: Codable, Equatable, Sendable {
    @Lenient public var battery: Double?
    @Lenient public var height: Double?
    @Lenient public var temperature: Double?
}

public struct FramePayload: Codable, Equatable, Sendable {
    /// Base64 JPEG, or nil when the ground sends detections only (`stream_video` false).
    @Lenient public var image: String?
    @Lenient public var boxes: [FrameBox]?
    @Lenient public var fps: Double?
    @Lenient public var persons: Int?
    @Lenient public var match: Bool?
    @Lenient public var matchScore: Double?
    @Lenient public var streamVideo: Bool?
    @Lenient public var droneId: String?
    @Lenient public var frameWidth: Double?
    @Lenient public var frameHeight: Double?
    @Lenient public var telemetry: FrameTelemetry?
    @Lenient public var recording: Bool?

    enum CodingKeys: String, CodingKey {
        case image, boxes, fps, persons, match, telemetry, recording
        case matchScore = "match_score", streamVideo = "stream_video", droneId = "drone_id"
        case frameWidth = "frame_width", frameHeight = "frame_height"
    }
}

/// `match_alert` and `match_alert_reminder` (a reminder adds `renotify` and `renotifyCount`).
public struct MatchAlert: Codable, Equatable, Sendable {
    @Lenient public var alertId: String?
    @Lenient public var time: String?
    @Lenient public var score: Double?
    @Lenient public var faceScore: Double?
    @Lenient public var reidScore: Double?
    @Lenient public var alertLevel: AlertLevel?
    @Lenient public var trackId: String?
    /// Base64 JPEG.
    @Lenient public var snapshot: String?
    @Lenient public var type: String?
    @Lenient public var reasoning: String?
    @Lenient public var gemmaConfidence: JSONValue?
    @Lenient public var gemmaMatch: JSONValue?
    @Lenient public var renotify: Bool?
    @Lenient public var renotifyCount: Int?

    enum CodingKeys: String, CodingKey {
        case time, score, snapshot, type, reasoning, renotify
        case alertId = "alert_id", faceScore = "face_score", reidScore = "reid_score"
        case alertLevel = "alert_level", trackId = "track_id"
        case gemmaConfidence = "gemma_confidence", gemmaMatch = "gemma_match"
        case renotifyCount = "renotify_count"
    }
}

// MARK: - detection_profiles

public struct DetectionProfile: Codable, Equatable, Sendable {
    @Lenient public var name: String?
    @Lenient public var label: String?
    @Lenient public var description: String?
    @Lenient public var confidence: Double?
    @Lenient public var faceWeightScale: Double?
    @Lenient public var reidWeightScale: Double?
    @Lenient public var fallback: String?
    @Lenient public var minPersonPx: Double?
    @Lenient public var tiled: Bool?
    @Lenient public var view: String?
    @Lenient public var weights: String?
    @Lenient public var weightsRequired: Bool?

    enum CodingKeys: String, CodingKey {
        case name, label, description, confidence, fallback, tiled, view, weights
        case faceWeightScale = "face_weight_scale", reidWeightScale = "reid_weight_scale"
        case minPersonPx = "min_person_px", weightsRequired = "weights_required"
    }
}

public struct ProfileRefusal: Codable, Equatable, Sendable {
    @Lenient public var reason: ProfileRefusalReason?
    @Lenient public var detail: String?
}

public struct DroneProfileState: Codable, Equatable, Sendable {
    @Lenient public var droneId: String?
    @Lenient public var supported: Bool?
    @Lenient public var active: String?
    @Lenient public var fallback: Bool?
    @Lenient public var fallbackReason: ProfileFallbackReason?
    @Lenient public var requested: String?
    @Lenient public var override: String?
    @Lenient public var desired: String?
    @Lenient public var mismatch: Bool?
    @Lenient public var pending: Bool?
    @Lenient public var refusal: ProfileRefusal?
    /// Free text from the ground, for example `no_reply`, `not_connected` or `send_failed`.
    @Lenient public var note: String?
    @Lenient public var flying: Bool?
    @Lenient public var switchable: Bool?
    @Lenient public var detectorOk: Bool?

    enum CodingKeys: String, CodingKey {
        case supported, active, fallback, requested, override, desired, mismatch, pending
        case refusal, note, flying, switchable
        case droneId = "drone_id", fallbackReason = "fallback_reason", detectorOk = "detector_ok"
    }
}

public struct DetectionProfilesPayload: Codable, Equatable, Sendable {
    /// `false` when the ground has no edge source.
    @Lenient public var available: Bool?
    @Lenient public var profiles: [DetectionProfile]?
    @Lenient public var fleetDefault: String?
    @Lenient public var drones: [String: DroneProfileState]?
    @Lenient public var controlsEnabled: Bool?
    @Lenient public var controlsNote: String?

    enum CodingKeys: String, CodingKey {
        case available, profiles, drones
        case fleetDefault = "fleet_default", controlsEnabled = "controls_enabled"
        case controlsNote = "controls_note"
    }

    /// The dashboard's `profileLabel`: the profile's label, else its name, else `--`.
    public func label(for name: String?) -> String {
        if let p = profiles?.first(where: { $0.name == name }), let l = p.label { return l }
        return (name?.isEmpty == false ? name : nil) ?? "--"
    }
}

// MARK: - ceiling_settings

public struct CeilingLimits: Codable, Equatable, Sendable {
    @Lenient public var maxCeilingCm: Double?
    @Lenient public var maxSlotCm: Double?
    @Lenient public var minCeilingCm: Double?
    @Lenient public var minMarginCm: Double?
    @Lenient public var minSlotCm: Double?

    enum CodingKeys: String, CodingKey {
        case maxCeilingCm = "max_ceiling_cm", maxSlotCm = "max_slot_cm"
        case minCeilingCm = "min_ceiling_cm", minMarginCm = "min_margin_cm"
        case minSlotCm = "min_slot_cm"
    }
}

public struct CeilingRefusal: Codable, Equatable, Sendable {
    @Lenient public var reason: CeilingRefusalReason?
    @Lenient public var detail: String?
}

public struct CeilingEdge: Codable, Equatable, Sendable {
    @Lenient public var supported: Bool?
    @Lenient public var ceilingCm: Double?
    @Lenient public var marginCm: Double?
    @Lenient public var slotCm: Double?
    @Lenient public var flying: Bool?
    @Lenient public var inSync: Bool?
    @Lenient public var pending: Bool?
    @Lenient public var refusal: CeilingRefusal?
    @Lenient public var note: String?

    enum CodingKeys: String, CodingKey {
        case supported, flying, pending, refusal, note
        case ceilingCm = "ceiling_cm", marginCm = "margin_cm", slotCm = "slot_cm"
        case inSync = "in_sync"
    }
}

public struct CeilingDrone: Codable, Equatable, Sendable {
    @Lenient public var droneId: String?
    @Lenient public var slotCm: Double?
    @Lenient public var ownSlotCm: Double?
    @Lenient public var defaultSlotCm: Double?
    @Lenient public var problem: String?
    @Lenient public var edge: CeilingEdge?

    enum CodingKeys: String, CodingKey {
        case problem, edge
        case droneId = "drone_id", slotCm = "slot_cm", ownSlotCm = "own_slot_cm"
        case defaultSlotCm = "default_slot_cm"
    }
}

/// `ceiling_settings`. Read only on a phone. `{available: false}` when there is no edge source.
public struct CeilingSettingsPayload: Codable, Equatable, Sendable {
    @Lenient public var available: Bool?
    @Lenient public var ceilingCm: Double?
    @Lenient public var marginCm: Double?
    @Lenient public var maxSlotCm: Double?
    @Lenient public var limits: CeilingLimits?
    /// A list, unlike the other per-drone collections.
    @Lenient public var drones: [CeilingDrone]?
    @Lenient public var editable: Bool?
    @Lenient public var lockedReason: String?
    @Lenient public var loadError: String?
    @Lenient public var file: String?

    enum CodingKeys: String, CodingKey {
        case available, limits, drones, editable, file
        case ceilingCm = "ceiling_cm", marginCm = "margin_cm", maxSlotCm = "max_slot_cm"
        case lockedReason = "locked_reason", loadError = "load_error"
    }
}

// MARK: - reasoning_provider and small events

/// `reasoning_provider`. Display only on a phone.
public struct ReasoningProviderPayload: Codable, Equatable, Sendable {
    @Lenient public var provider: ReasoningProviderName?
    @Lenient public var label: String?
    @Lenient public var model: String?
    @Lenient public var available: [ReasoningProviderName]?
    /// Provider name to the reason it cannot be used.
    @Lenient public var unavailable: [String: String]?
    @Lenient public var cloudAllowed: Bool?
    @Lenient public var error: String?

    enum CodingKeys: String, CodingKey {
        case provider, label, model, available, unavailable, error
        case cloudAllowed = "cloud_allowed"
    }
}

/// `land_all_ack` (flat shape). Each count is how many drones that path reached, or nil.
public struct LandAllAck: Codable, Equatable, Sendable {
    @Lenient public var coordinator: Int?
    @Lenient public var transport: Int?
    @Lenient public var local: Int?
    @Lenient public var reachedAny: Bool?

    enum CodingKeys: String, CodingKey {
        case coordinator, transport, local
        case reachedAny = "reached_any"
    }
}

/// `command_ack`: the answer to `drone_command` on a ground without a fleet.
public struct CommandAck: Codable, Equatable, Sendable {
    @Lenient public var command: String?
}

public struct StreamVideoState: Codable, Equatable, Sendable {
    @Lenient public var enabled: Bool?
}

/// `error` and `warning`. Show as a toast; never a disconnect.
public struct ErrorPayload: Codable, Equatable, Sendable {
    @Lenient public var message: String?
}
