import Foundation

// Wire vocabularies. `nack_reasons.json` lists today's values; a test fails when it gains one
// that is missing here. Unknown values map to `.unknown`, never to a decoding failure.

/// Why a command was refused or not answered: the union of the assign_lanes, fenced v2 and
/// manual command vocabularies, plus `no_reply`, which the ground itself reports in
/// `manual_result` when the edge does not answer within 4 seconds.
public enum NackReason: WireEnum {
    case altitude, battery, busy, ceiling, droneUnreachable, floor, geofence, invalid, landing
    case noValidPlan, notAuthority, notFlying, notManual, otherDrone, planMismatch, slotBand
    case staleEpoch, stalePlan, unsupported
    /// Ground-reported: the edge did not answer in time. Not in `nack_reasons.json`.
    case noReply
    case unknown(String)

    private static let table: [(String, NackReason)] = [
        ("altitude", .altitude), ("battery", .battery), ("busy", .busy), ("ceiling", .ceiling),
        ("drone_unreachable", .droneUnreachable), ("floor", .floor), ("geofence", .geofence),
        ("invalid", .invalid), ("landing", .landing), ("no_valid_plan", .noValidPlan),
        ("not_authority", .notAuthority), ("not_flying", .notFlying), ("not_manual", .notManual),
        ("other_drone", .otherDrone), ("plan_mismatch", .planMismatch), ("slot_band", .slotBand),
        ("stale_epoch", .staleEpoch), ("stale_plan", .stalePlan), ("unsupported", .unsupported),
        ("no_reply", .noReply),
    ]
    public static let knownWireValues = table.map { $0.0 }
    public init(wire: String) { self = Self.table.first { $0.0 == wire }?.1 ?? .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return Self.table.first { $0.1 == self }!.0
    }
}

/// Why a detection profile switch was refused.
public enum ProfileRefusalReason: WireEnum {
    case busy, inFlight, invalid, loadFailed, unavailable, unknownProfile
    case unknown(String)

    private static let table: [(String, ProfileRefusalReason)] = [
        ("busy", .busy), ("in_flight", .inFlight), ("invalid", .invalid),
        ("load_failed", .loadFailed), ("unavailable", .unavailable), ("unknown_profile", .unknownProfile),
    ]
    public static let knownWireValues = table.map { $0.0 }
    public init(wire: String) { self = Self.table.first { $0.0 == wire }?.1 ?? .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return Self.table.first { $0.1 == self }!.0
    }
}

/// Why an edge refused a ceiling change.
public enum CeilingRefusalReason: WireEnum {
    case aboveCeiling, inFlight, invalid, notAuthority, staleEpoch
    case unknown(String)

    private static let table: [(String, CeilingRefusalReason)] = [
        ("above_ceiling", .aboveCeiling), ("in_flight", .inFlight), ("invalid", .invalid),
        ("not_authority", .notAuthority), ("stale_epoch", .staleEpoch),
    ]
    public static let knownWireValues = table.map { $0.0 }
    public init(wire: String) { self = Self.table.first { $0.0 == wire }?.1 ?? .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return Self.table.first { $0.1 == self }!.0
    }
}

/// Why an edge fell back to another detection profile.
public enum ProfileFallbackReason: WireEnum {
    case weightsMissing
    case unknown(String)

    public static let knownWireValues = ["weights_missing"]
    public init(wire: String) { self = wire == "weights_missing" ? .weightsMissing : .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return "weights_missing"
    }
}

/// A drone's control mode as the edge and ground report it.
public enum ControlMode: WireEnum {
    case autonomous, held, manual
    case unknown(String)

    public static let knownWireValues = ["autonomous", "held", "manual"]
    public init(wire: String) {
        switch wire {
        case "autonomous": self = .autonomous
        case "held": self = .held
        case "manual": self = .manual
        default: self = .unknown(wire)
        }
    }
    public var wire: String {
        switch self {
        case .autonomous: return "autonomous"
        case .held: return "held"
        case .manual: return "manual"
        case .unknown(let s): return s
        }
    }
}

/// Why a drone is held.
public enum HoldReason: WireEnum {
    case alert, handover, `operator`
    case unknown(String)

    public static let knownWireValues = ["alert", "handover", "operator"]
    public init(wire: String) {
        switch wire {
        case "alert": self = .alert
        case "handover": self = .handover
        case "operator": self = .operator
        default: self = .unknown(wire)
        }
    }
    public var wire: String {
        switch self {
        case .alert: return "alert"
        case .handover: return "handover"
        case .operator: return "operator"
        case .unknown(let s): return s
        }
    }
}

/// What an edge reports it is doing.
public enum EdgeState: WireEnum {
    case flyingLanes, holding, hovering, idle, landing, takingOff
    case unknown(String)

    private static let table: [(String, EdgeState)] = [
        ("flying_lanes", .flyingLanes), ("holding", .holding), ("hovering", .hovering),
        ("idle", .idle), ("landing", .landing), ("taking_off", .takingOff),
    ]
    public static let knownWireValues = table.map { $0.0 }
    public init(wire: String) { self = Self.table.first { $0.0 == wire }?.1 ?? .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return Self.table.first { $0.1 == self }!.0
    }
}

/// The action a manual ack or result answers. A phone only ever asks for `land` and `resume_search`.
public enum ManualAction: WireEnum {
    case land, resumeSearch
    case unknown(String)

    public static let knownWireValues = ["land", "resume_search"]
    public init(wire: String) {
        switch wire {
        case "land": self = .land
        case "resume_search": self = .resumeSearch
        default: self = .unknown(wire)
        }
    }
    public var wire: String {
        switch self {
        case .land: return "land"
        case .resumeSearch: return "resume_search"
        case .unknown(let s): return s
        }
    }
}

/// The ground's reasoning provider.
public enum ReasoningProviderName: WireEnum {
    case gemma, claude, off
    case unknown(String)

    public static let knownWireValues = ["gemma", "claude", "off"]
    public init(wire: String) {
        switch wire {
        case "gemma": self = .gemma
        case "claude": self = .claude
        case "off": self = .off
        default: self = .unknown(wire)
        }
    }
    public var wire: String {
        switch self {
        case .gemma: return "gemma"
        case .claude: return "claude"
        case .off: return "off"
        case .unknown(let s): return s
        }
    }
}

/// Match alert level.
public enum AlertLevel: WireEnum {
    case possibleMatch
    case unknown(String)

    public static let knownWireValues = ["possible_match"]
    public init(wire: String) { self = wire == "possible_match" ? .possibleMatch : .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return "possible_match"
    }
}

/// Direction of a plan lane.
public enum LaneDirection: WireEnum {
    case forward, back, left, right
    case unknown(String)

    public static let knownWireValues = ["forward", "back", "left", "right"]
    public init(wire: String) {
        switch wire {
        case "forward": self = .forward
        case "back": self = .back
        case "left": self = .left
        case "right": self = .right
        default: self = .unknown(wire)
        }
    }
    public var wire: String {
        switch self {
        case .forward: return "forward"
        case .back: return "back"
        case .left: return "left"
        case .right: return "right"
        case .unknown(let s): return s
        }
    }
}

/// The kind of a `fleet_event`. Kinds this build does not know keep their raw text.
public enum FleetEventKind: WireEnum {
    case planRefused, planInvalidated, start, startRefused, droneLost, retask, retaskFailed
    case landAll, profileSwitchPending
    case unknown(String)

    private static let table: [(String, FleetEventKind)] = [
        ("plan_refused", .planRefused), ("plan_invalidated", .planInvalidated), ("start", .start),
        ("start_refused", .startRefused), ("drone_lost", .droneLost), ("retask", .retask),
        ("retask_failed", .retaskFailed), ("land_all", .landAll),
        ("profile_switch_pending", .profileSwitchPending),
    ]
    public static let knownWireValues = table.map { $0.0 }
    public init(wire: String) { self = Self.table.first { $0.0 == wire }?.1 ?? .unknown(wire) }
    public var wire: String {
        if case .unknown(let s) = self { return s }
        return Self.table.first { $0.1 == self }!.0
    }
}
