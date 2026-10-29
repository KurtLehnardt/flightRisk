import Foundation

// State derivations. These copy the dashboard rules in flightrisk/dashboard/static/ui/ui.js
// (`connState`, `modeText`, `manualDisabledReason`) exactly, as written in
// docs/mobile-contract.md, section "Derived state, exactly as the dashboard shows it".

/// Connection state of one drone, from its `fleet_status` entry. First match wins.
public enum DroneLinkState: Equatable, Hashable, Sendable {
    case connected
    case down
    case lost
    /// Shown as `--`. This is the state of every drone of a ground without a fleet.
    case unknown

    /// `lost == true` is LOST, `connected == false` is DOWN, `connected == true` is CONNECTED,
    /// anything else (absent, null) is unknown.
    public init(lost: Bool?, connected: Bool?) {
        if lost == true { self = .lost }
        else if connected == false { self = .down }
        else if connected == true { self = .connected }
        else { self = .unknown }
    }

    public init(_ record: DroneRecord) {
        self.init(lost: record.lost, connected: record.connected)
    }

    /// The text of the dashboard chip.
    public var text: String {
        switch self {
        case .connected: return "CONNECTED"
        case .down: return "DOWN"
        case .lost: return "LOST"
        case .unknown: return "--"
        }
    }

    /// The dashboard's `key`, used for styling.
    public var key: String {
        switch self {
        case .connected: return "connected"
        case .down: return "down"
        case .lost: return "lost"
        case .unknown: return ""
        }
    }

    /// DOWN and LOST cards show the last values received, not live ones.
    public var isStale: Bool { self == .down || self == .lost }
}

/// What the drone is doing when nothing else (hold, manual) applies.
public enum DroneActivity: Equatable, Hashable, Sendable {
    case flying
    case onGround
    case unknown
}

/// Control state of one drone, from the merged `fleet_status` and `manual_status` entry.
/// First match wins: orphan countdown, controlled manually, held, manual mode, activity.
public enum ControlState: Equatable, Hashable, Sendable {
    /// HELD. `orphanLandInS` is set while the ground counts down to landing a held drone that
    /// nobody controls, and nil for a plain hold.
    case held(orphanLandInS: Int?)
    /// MANUAL. `underOperatorControl` is true when the dashboard's controller holds this drone.
    case manual(underOperatorControl: Bool)
    /// Under the autonomous search (or no control information).
    case autonomous(DroneActivity)

    /// - Parameters:
    ///   - id: the drone id; an empty id never matches `manualControlled`.
    ///   - manualControlled: `fleet_status.manual_controlled` or `manual_status.controlled`.
    public init(id: String, record: DroneRecord, manualControlled: String?) {
        if let landIn = record.orphanLandInS, landIn.isFinite {
            let whole = landIn.rounded(.towardZero)
            let clamped = min(max(whole, Double(Int32.min)), Double(Int32.max))
            self = .held(orphanLandInS: Int(clamped))
        } else if !id.isEmpty, let m = manualControlled, id == m {
            self = .manual(underOperatorControl: true)
        } else if record.controlMode == .held || record.edgeControlMode == .held {
            self = .held(orphanLandInS: nil)
        } else if record.controlMode == .manual {
            self = .manual(underOperatorControl: false)
        } else if record.isFlying == true {
            self = .autonomous(.flying)
        } else if record.isFlying == false {
            self = .autonomous(.onGround)
        } else {
            self = .autonomous(.unknown)
        }
    }

    /// Exactly the dashboard's `modeText` text. Empty means show nothing.
    public var dashboardText: String {
        switch self {
        case .held(let n?): return "HELD: auto-landing in \(n) s, no controller"
        case .held(nil): return "HELD: hovering, search paused"
        case .manual(true): return "MANUAL: under your control"
        case .manual(false): return "MANUAL"
        case .autonomous(.flying): return "Flying"
        case .autonomous(.onGround): return "On the ground"
        case .autonomous(.unknown): return ""
        }
    }

    /// The text for a phone, which never controls a drone: a drone the dashboard controls is
    /// just MANUAL (the laptop controls it). Everything else is the dashboard text.
    public var text: String {
        if case .manual = self { return "MANUAL" }
        return dashboardText
    }

    /// True for the two states that get the "held" styling and the HELD badge.
    public var isHeld: Bool {
        if case .held = self { return true }
        return false
    }

    /// The seconds shown on the countdown, if any.
    public var orphanLandInS: Int? {
        if case .held(let n) = self { return n }
        return nil
    }

    /// The dashboard's video-tile badge: MANUAL or HELD, or the countdown text while counting.
    public var tileBadge: String {
        switch self {
        case .held(let n?): return "HELD: auto-landing in \(n) s, no controller"
        case .held(nil): return "HELD"
        case .manual: return "MANUAL"
        case .autonomous: return ""
        }
    }
}

/// The local HELD countdown: count down between updates, replace with every new value.
public enum OrphanCountdown {
    /// Whole seconds left, never below zero.
    public static func remaining(received: Int, elapsedSeconds: Double) -> Int {
        guard elapsedSeconds.isFinite, elapsedSeconds > 0 else { return max(0, received) }
        let elapsed = Int(min(elapsedSeconds.rounded(.down), Double(Int32.max)))
        return max(0, received - elapsed)
    }
}

/// When the phone may offer "resume search": the drone's `control_mode` is held and nobody
/// controls any drone (`manual_controlled` is null). Land is never gated; resume is.
public func canResumeHeldDrone(_ record: DroneRecord, manualControlled: String?) -> Bool {
    record.controlMode == .held && (manualControlled ?? "").isEmpty
}

/// Battery level class of the dashboard's `batteryClass`.
public enum BatteryLevel: Equatable, Sendable {
    case normal, warn, bad

    public init(_ battery: Double?) {
        guard let b = battery else { self = .normal; return }
        self = b < 15 ? .bad : b < 30 ? .warn : .normal
    }
}

/// Everything a drone status card shows, derived once from a `fleet_status` entry that was
/// merged with its `manual_status` entry.
public struct DroneCardState: Equatable, Sendable {
    public let id: String
    public let link: DroneLinkState
    public let control: ControlState
    /// "87%" or "--".
    public let batteryText: String
    public let batteryLevel: BatteryLevel
    /// "100 cm" or "--".
    public let altitudeText: String
    /// "n/m", only when both numbers are known.
    public let laneText: String?
    /// "Drone link down: ... Land still works." or nil.
    public let linkNotice: String?
    /// "No live link: values are the last ones received" for DOWN and LOST cards.
    public let staleNote: String?

    public init(id: String, record: DroneRecord, manualControlled: String?) {
        self.id = id
        link = DroneLinkState(record)
        control = ControlState(id: id, record: record, manualControlled: manualControlled)
        batteryText = record.battery.map { GroundText.number($0) + "%" } ?? "--"
        batteryLevel = BatteryLevel(record.battery)
        altitudeText = record.displayHeightCm.map { GroundText.number($0) + " cm" } ?? "--"
        if let i = record.laneIndex, let n = record.lanesTotal { laneText = "\(i)/\(n)" } else { laneText = nil }
        linkNotice = GroundText.droneLinkNotice(record)
        staleNote = link.isStale ? GroundText.staleValuesNote : nil
    }
}
