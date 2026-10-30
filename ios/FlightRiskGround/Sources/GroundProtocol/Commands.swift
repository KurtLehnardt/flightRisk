import Foundation

/// Every command the phone may send, and nothing else. The set of cases is the allow-list in
/// `tests/fixtures/mobile_contract/client_emits.json`. There is deliberately no case for plan,
/// start, take control, moves, takeoff, ceiling edits, reasoning provider changes, stream
/// toggles or match alert responses: the compiler, not a runtime check, keeps them out.
///
/// Landing is never gated here. The only gate is socket connectivity, decided by the caller.
public enum GroundCommand: Equatable, Hashable, Sendable {
    case landAll
    /// `drone_command` with `command: "land"`. The only `drone_command` the phone can build.
    case landDrone(droneId: String)
    /// `manual_resume_search`. Offer it only for a held drone nobody controls
    /// (see `canResumeHeldDrone`).
    case resumeHeldDrone(droneId: String)
    /// An empty `profile` follows the fleet default.
    case setDetectionProfile(droneId: String, profile: String)
    /// An empty `profile` clears the fleet default.
    case setFleetDetectionProfile(profile: String)
    case getFleetStatus
    case getDetectionProfiles
    case getCeilingSettings
    case getReasoningProvider
    case getManualStatus

    /// The Socket.IO event name.
    public var eventName: String {
        switch self {
        case .landAll: return "land_all"
        case .landDrone: return "drone_command"
        case .resumeHeldDrone: return "manual_resume_search"
        case .setDetectionProfile: return "set_detection_profile"
        case .setFleetDetectionProfile: return "set_fleet_detection_profile"
        case .getFleetStatus: return "get_fleet_status"
        case .getDetectionProfiles: return "get_detection_profiles"
        case .getCeilingSettings: return "get_ceiling_settings"
        case .getReasoningProvider: return "get_reasoning_provider"
        case .getManualStatus: return "get_manual_status"
        }
    }

    /// The payload object, or nil for events that carry none.
    public var payload: [String: String]? {
        switch self {
        case .landDrone(let id): return ["command": "land", "drone_id": id]
        case .resumeHeldDrone(let id): return ["drone_id": id]
        case .setDetectionProfile(let id, let profile): return ["drone_id": id, "profile": profile]
        case .setFleetDetectionProfile(let profile): return ["profile": profile]
        case .landAll, .getFleetStatus, .getDetectionProfiles, .getCeilingSettings,
             .getReasoningProvider, .getManualStatus:
            return nil
        }
    }

    /// False when a drone id is empty or only whitespace. Such a command must never be sent: in
    /// fleet mode the ground reads a missing `drone_id` as the drone the laptop controls, so an
    /// empty id would land (or resume) the wrong drone.
    public var isSendable: Bool {
        switch self {
        case .landDrone(let id), .resumeHeldDrone(let id), .setDetectionProfile(let id, _):
            return !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        default:
            return true
        }
    }

    /// The payload as compact JSON with sorted keys and unescaped slashes (byte-identical to the
    /// Android app), or nil for events without a payload and for commands that are not sendable.
    public func encodedPayload() -> Data? {
        guard isSendable, let payload = payload else { return nil }
        return try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys, .withoutEscapingSlashes])
    }

    /// What to put on the wire, or nil when the command is not sendable. The client must send
    /// only through this, never from `eventName` and `payload` directly.
    public var wireMessage: GroundWireMessage? {
        guard isSendable else { return nil }
        return GroundWireMessage(eventName: eventName, payloadJSON: encodedPayload())
    }

    /// The five state requests, sent on every connect and every reconnect.
    public static let stateRequests: [GroundCommand] = [
        .getFleetStatus, .getDetectionProfiles, .getCeilingSettings, .getReasoningProvider, .getManualStatus,
    ]

    /// One example of every case. A test checks that its event names equal the fixture allow-list.
    public static let examples: [GroundCommand] = [
        .landAll, .landDrone(droneId: "drone-1"), .resumeHeldDrone(droneId: "drone-1"),
        .setDetectionProfile(droneId: "drone-1", profile: "default"),
        .setFleetDetectionProfile(profile: "default"),
    ] + stateRequests

    /// Event names the phone may emit. Derived from the cases, so it cannot drift from them.
    public static var allowedEventNames: Set<String> { Set(examples.map(\.eventName)) }
}

/// An event name and its optional compact JSON payload, ready for Socket.IO framing.
public struct GroundWireMessage: Equatable, Sendable {
    public let eventName: String
    public let payloadJSON: Data?
}
