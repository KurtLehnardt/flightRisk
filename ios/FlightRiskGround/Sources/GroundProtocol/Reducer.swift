import Foundation

/// Combines `fleet_status` and `manual_status` into one view of the drones, strictly in the order
/// the events arrive. THE GROUND TAB MUST FEED EVERY `fleet_status` AND `manual_status` EVENT
/// THROUGH THIS REDUCER and build its cards from `cards` or `card(for:)`; combining the two
/// payloads in any other order shows a held drone as normal or a stale countdown.
///
/// Rules (the dashboard's "newest wins", ui.js lines 227 to 236 and 1670 to 1673):
/// - `fleet_status` replaces every drone's whole record, which drops any earlier manual overlay,
///   and removes drones it no longer lists (partial ones included). It also sets
///   `manualControlled` from `manual_controlled`.
/// - `manual_status` replaces only the keys it carries on that drone's record and sets
///   `manualControlled` from `controlled`. A drone it names that no `fleet_status` has listed
///   gets a partial record (flagged in `partialDroneIds`) that the next `fleet_status` replaces.
///   This happens after a ground restart, when `get_manual_status` is answered before the edge
///   has sent its first telemetry.
/// - An empty `controlled` or `manual_controlled` means nobody.
public struct DroneStateReducer: Equatable, Sendable {
    public private(set) var drones: [String: DroneRecord] = [:]
    /// Ids whose record came from `manual_status` alone and carries only the manual keys.
    public private(set) var partialDroneIds: Set<String> = []
    /// The drone under manual control, or nil.
    public private(set) var manualControlled: String?
    /// True when the last `fleet_status` was the legacy `{drones, count}` shape.
    public private(set) var isLegacyShape: Bool = false

    public init() {}

    /// Feed any server event; events other than the two status events are ignored.
    public mutating func apply(_ event: GroundEvent) {
        switch event {
        case .fleetStatus(let p): apply(p)
        case .manualStatus(let p): apply(p)
        default: break
        }
    }

    public mutating func apply(_ status: FleetStatusPayload) {
        guard let incoming = status.drones else { return }
        var next: [String: DroneRecord] = [:]
        for (id, record) in incoming {
            var r = record
            if r.droneId == nil { r.droneId = id }
            next[id] = r
        }
        drones = next
        partialDroneIds = []
        manualControlled = Self.normalized(status.manualControlled)
        isLegacyShape = status.isLegacyShape
    }

    public mutating func apply(_ status: ManualStatusPayload) {
        manualControlled = Self.normalized(status.controlled)
        for (id, entry) in status.drones ?? [:] {
            if let existing = drones[id] {
                drones[id] = existing.overlaying(entry)
            } else {
                var r = DroneRecord()
                r.droneId = id
                drones[id] = r.overlaying(entry)
                partialDroneIds.insert(id)
            }
        }
    }

    /// Forget everything, for example when the socket disconnects.
    public mutating func reset() { self = DroneStateReducer() }

    public var sortedDroneIds: [String] { drones.keys.sorted() }

    public func isPartial(_ id: String) -> Bool { partialDroneIds.contains(id) }

    public func card(for id: String) -> DroneCardState? {
        guard let r = drones[id] else { return nil }
        return DroneCardState(id: id, record: r, manualControlled: manualControlled)
    }

    /// One card per drone, sorted by id.
    public var cards: [DroneCardState] { sortedDroneIds.compactMap(card(for:)) }

    /// Whether to offer "resume search" for this drone (see `canResumeHeldDrone`).
    public func canResume(_ id: String) -> Bool {
        guard let r = drones[id] else { return false }
        return canResumeHeldDrone(r, manualControlled: manualControlled)
    }

    private static func normalized(_ id: String?) -> String? {
        guard let id = id, !id.isEmpty else { return nil }
        return id
    }
}
