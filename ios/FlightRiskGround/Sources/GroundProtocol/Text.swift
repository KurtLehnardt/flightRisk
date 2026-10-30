import Foundation

/// Human-readable text for status chips and refusals. Pure functions, no UI. Wording copies the
/// dashboard (`flightrisk/dashboard/static/ui/ui.js`) wherever docs/mobile-contract.md gives it.
public enum GroundText {
    /// The dashboard's `fmt`: `Math.round(v * 10) / 10`, `--` for absent values. JavaScript
    /// rounds halves toward positive infinity, so -0.25 gives -0.2, not -0.3.
    public static func number(_ v: Double?) -> String {
        guard let v = v, v.isFinite else { return "--" }
        return jsNumber(((v * 10) + 0.5).rounded(.down) / 10)
    }

    /// A number as JavaScript prints it in string concatenation: 120 not 120.0, 29.6 as is,
    /// and -0 as 0.
    public static func jsNumber(_ v: Double) -> String {
        guard v.isFinite else { return "--" }
        if v == 0 { return "0" }
        if v == v.rounded(), abs(v) < 1e15 { return String(Int64(v)) }
        return String(v)
    }

    public static let staleValuesNote = "No live link: values are the last ones received"

    /// "Drone link down: <detail>. Land still works." when `drone_link_ok == false`, else nil.
    public static func droneLinkNotice(_ record: DroneRecord) -> String? {
        guard record.droneLinkOk == false else { return nil }
        var detail = "the edge cannot reach the drone"
        if let d = record.droneLinkDetail, !d.isEmpty { detail = d }
        return "Drone link down: \(detail). Land still works."
    }

    /// The dashboard's `manualDisabledReason`: why manual controls are off, else nil. A phone
    /// has no manual controls, but shows the same reasons.
    public static func controlsDisabledReason(_ record: DroneRecord) -> String? {
        if record.connected == false { return "Edge disconnected: controls are off (Land still works)." }
        return droneLinkNotice(record)
    }

    // MARK: LAND ALL

    /// The sentence the ground itself broadcasts as `error` when `reached_any` is false.
    public static let landAllReachedNoDrone =
        "LAND ALL reached no drone. Land every drone with its local control now."
    public static let landAllSending = "LAND ALL sending\u{2026}"
    public static let landAllNoReplyYet = "LAND ALL: no reply from the ground yet. Check the connection."
    public static let landAllNotSent =
        "LAND ALL was not sent: not connected to the ground. Reconnecting. Use the laptop's LAND ALL (Shift+L) or the drones' own controls."
    private static let landByHand = "Land each drone by hand (per-drone Land, or the drone's own controls)."
    /// Shown when an ack reports that no drone was reached, or says nothing.
    public static let landAllFailedNoDrone = "LAND ALL FAILED: no drone was reached. " + landByHand

    /// Text for a `land_all_ack`. Anything other than `reached_any == true` is a failure: a
    /// missing, null, string or false value never counts as success.
    public static func landAllResult(_ ack: LandAllAck) -> (text: String, isFailure: Bool) {
        if ack.reachedAny != true { return (landAllFailedNoDrone, true) }
        var parts: [String] = []
        if let n = ack.coordinator { parts.append("coordinator \(n)") }
        if let n = ack.transport { parts.append("transport \(n)") }
        if let n = ack.local { parts.append("local \(n)") }
        let counts = parts.isEmpty ? "" : " (" + parts.joined(separator: ", ") + ")"
        return ("LAND ALL reached drones" + counts, false)
    }

    // MARK: fleet events

    /// A `fleet_event` shown as a banner. `isFailure` is true for a failed LAND ALL.
    public struct Banner: Equatable, Sendable {
        public let text: String
        public let isFailure: Bool
    }

    /// The banner for a `fleet_event`, or nil when none is shown. A `land_all` event whose detail
    /// starts with "failed" (the ground sends "failed: drone-2" when the emergency land could not
    /// reach a link) is a failure, not "LAND ALL sent".
    public static func fleetEventBannerInfo(_ ev: FleetEvent) -> Banner? {
        if ev.resolvedKind == .landAll, let d = ev.detail, d.hasPrefix("failed") {
            return Banner(text: "LAND ALL FAILED: \(d). " + landByHand, isFailure: true)
        }
        guard let text = fleetEventBanner(ev) else { return nil }
        return Banner(text: text, isFailure: false)
    }

    /// The banner text the dashboard shows for a `fleet_event`, or nil when it shows none. Use
    /// `fleetEventBannerInfo` to also get the failure flag and the failed LAND ALL text.
    public static func fleetEventBanner(_ ev: FleetEvent) -> String? {
        guard let kind = ev.resolvedKind else { return nil }
        var who = ""
        if let id = ev.droneId, !id.isEmpty { who = " " + id }
        var detail: String?
        if let d = ev.detail, !d.isEmpty { detail = d }
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
        var detail = "unknown"
        if let d = ev.detail, !d.isEmpty { detail = d }
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
        var why = reply.reason.map { nackReasonText($0) } ?? "refused"
        if let d = reply.detail, !d.isEmpty { why = d }
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
            var asked = "the requested profile"
            if let r = d.requested, !r.isEmpty { asked = label(r) }
            let why: String
            if d.fallbackReason?.wire == "weights_missing" { why = "weights missing on the edge" }
            else {
                var raw = "unknown"
                if let w = d.fallbackReason?.wire, !w.isEmpty { raw = w }
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
