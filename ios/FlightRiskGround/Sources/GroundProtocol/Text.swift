import Foundation

/// Human-readable text for status chips and refusals. Pure functions, no UI. Wording copies the
/// dashboard (`flightrisk/dashboard/static/ui/ui.js`) wherever docs/mobile-contract.md gives it.
public enum GroundText {
    /// The dashboard's `fmt`: one decimal at most, `--` for absent values.
    public static func number(_ v: Double?) -> String {
        guard let v = v, v.isFinite else { return "--" }
        let rounded = (v * 10).rounded() / 10
        if rounded == rounded.rounded(), abs(rounded) < 1e15 { return String(Int64(rounded)) }
        return String(rounded)
    }

    public static let staleValuesNote = "No live link: values are the last ones received"

    /// "Drone link down: <detail>. Land still works." when `drone_link_ok == false`, else nil.
    public static func droneLinkNotice(_ record: DroneRecord) -> String? {
        guard record.droneLinkOk == false else { return nil }
        let detail = (record.droneLinkDetail?.isEmpty == false) ? record.droneLinkDetail! : "the edge cannot reach the drone"
        return "Drone link down: \(detail). Land still works."
    }

    /// The dashboard's `manualDisabledReason`: why manual controls are off, else nil. A phone
    /// has no manual controls, but shows the same reasons.
    public static func controlsDisabledReason(_ record: DroneRecord) -> String? {
        if record.connected == false { return "Edge disconnected: controls are off (Land still works)." }
        return droneLinkNotice(record)
    }

    // MARK: LAND ALL

    public static let landAllReachedNoDrone =
        "LAND ALL reached no drone. Land every drone with its local control now."
    public static let landAllSending = "Sending LAND ALL"
    public static let landAllNoReplyYet = "No reply yet to LAND ALL"
    public static let landAllNotSent =
        "LAND ALL was not sent: not connected to the ground. Reconnecting. Use the laptop's LAND ALL (Shift+L) or the drones' own controls."

    /// Text for a `land_all_ack`. `isFailure` is true when `reached_any == false`.
    public static func landAllResult(_ ack: LandAllAck) -> (text: String, isFailure: Bool) {
        if ack.reachedAny == false { return (landAllReachedNoDrone, true) }
        var parts: [String] = []
        if let n = ack.coordinator { parts.append("coordinator \(n)") }
        if let n = ack.transport { parts.append("transport \(n)") }
        if let n = ack.local { parts.append("local \(n)") }
        let counts = parts.isEmpty ? "" : " (" + parts.joined(separator: ", ") + ")"
        return ("LAND ALL reached drones" + counts, false)
    }

    // MARK: fleet events

    /// The banner the dashboard shows for a `fleet_event`, or nil when it shows none.
    public static func fleetEventBanner(_ ev: FleetEvent) -> String? {
        guard let kind = ev.resolvedKind else { return nil }
        let who = (ev.droneId?.isEmpty == false) ? " " + ev.droneId! : ""
        let detail = (ev.detail?.isEmpty == false) ? ev.detail : nil
        switch kind {
        case .retaskFailed: return "RETASK FAILED: " + (detail ?? ev.droneId ?? "")
        case .retask: return "RETASK:" + who + (detail.map { " " + $0 } ?? "")
        case .droneLost: return "DRONE LOST:" + who
        case .landAll: return "LAND ALL sent"
        case .profileSwitchPending: return "WARNING: " + (detail ?? "detection profile switch pending")
        case .planInvalidated: return "PLAN EDITED: press Plan search again"
        case .start: return "start"
        case .planRefused: return "PLAN REFUSED"
        case .startRefused: return "start_refused"
        case .unknown(let raw): return raw.isEmpty ? nil : raw
        }
    }

    /// The error line for a refused plan or start, as the dashboard words it; nil for other kinds.
    /// The ground's `detail` is shown as sent; the phone never rewrites or second-guesses it.
    public static func fleetRefusal(_ ev: FleetEvent) -> String? {
        let detail = (ev.detail?.isEmpty == false) ? ev.detail! : "unknown"
        switch ev.resolvedKind {
        case .planRefused?: return "Plan refused: " + detail
        case .startRefused?: return "Start refused: " + detail
        default: return nil
        }
    }

    // MARK: manual replies

    /// A one-line message for a `manual_ack` or `manual_result`; nil when it succeeded.
    public static func manualRefusal(_ reply: ManualReply) -> String? {
        guard reply.ok == false else { return nil }
        let id = reply.droneId ?? "drone"
        let why = (reply.detail?.isEmpty == false) ? reply.detail! : (reply.reason.map { nackReasonText($0) } ?? "refused")
        return "\(id): \(why)"
    }

    /// Short readable text for a refusal reason. Unknown reasons are shown by name.
    public static func nackReasonText(_ reason: NackReason) -> String {
        switch reason {
        case .altitude: return "altitude not allowed"
        case .battery: return "battery too low"
        case .busy: return "drone is busy"
        case .ceiling: return "above the ceiling"
        case .droneUnreachable: return "drone unreachable"
        case .floor: return "below the floor"
        case .geofence: return "outside the geofence"
        case .invalid: return "invalid request"
        case .landing: return "drone is landing"
        case .noValidPlan: return "no valid plan"
        case .notAuthority: return "this ground is not the authority"
        case .notFlying: return "drone is not flying"
        case .notManual: return "drone is not under manual control"
        case .otherDrone: return "conflicts with another drone"
        case .planMismatch: return "plan mismatch"
        case .slotBand: return "outside the altitude slot"
        case .staleEpoch: return "stale command epoch"
        case .stalePlan: return "stale plan"
        case .unsupported: return "not supported by this edge"
        case .noReply: return "no reply from the edge"
        case .unknown(let raw): return raw
        }
    }

    // MARK: detection profiles

    public enum Severity: Equatable, Sendable { case info, warn, bad }

    /// The dashboard's `profileStatusCore`: the status line under a drone's profile selector.
    public static func profileStatus(_ d: DroneProfileState, in payload: DetectionProfilesPayload) -> (text: String, severity: Severity) {
        func label(_ n: String?) -> String { payload.label(for: n) }
        if d.supported != true {
            return ("This edge does not report detection profiles (older edge); it keeps its own model.", .warn)
        }
        if d.pending == true { return ("Switching to \(label(d.requested))...", .info) }
        if let r = d.refusal {
            let reason = r.reason?.wire ?? ""
            if d.mismatch == true, reason == "in_flight" {
                return ("MISMATCH: chosen \(label(d.desired)), edge runs \(label(d.active)). Refused in flight; sent again when the drone is on the ground.", .bad)
            }
            let why = reason == "in_flight" ? "refused in flight" : "refused (\(reason))"
            return ("Active: \(label(d.active)). Switch \(why): \(r.detail ?? "")", .bad)
        }
        switch d.note {
        case "no_reply"?: return ("Active: \(label(d.active)). No reply from the edge; try again.", .warn)
        case "not_connected"?: return ("Active: \(label(d.active)). Not connected; the choice is sent again when it reconnects.", .warn)
        case "send_failed"?: return ("Active: \(label(d.active)). The command could not be sent.", .bad)
        default: break
        }
        if d.fallback == true {
            let asked = (d.requested?.isEmpty == false) ? label(d.requested) : "the requested profile"
            let why: String
            if d.fallbackReason?.wire == "weights_missing" { why = "weights missing on the edge" }
            else {
                let raw = (d.fallbackReason?.wire.isEmpty == false) ? d.fallbackReason!.wire : "unknown"
                why = "could not be used (\(raw))"
            }
            return ("Active: \(label(d.active)) (fallback: \(asked) \(why)).", .warn)
        }
        let air = d.flying == true ? " In flight: profile changes will be refused." : ""
        if d.mismatch == true {
            return ("MISMATCH: chosen \(label(d.desired)), edge runs \(label(d.active)). Sent again when the drone is on the ground or reconnects.\(air)", .warn)
        }
        return ("Active: \(label(d.active)).\(air)", .info)
    }
}
