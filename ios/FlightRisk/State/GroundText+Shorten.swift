import Foundation
import GroundProtocol

// Every string that comes from the ground is cut before the Ground tab stores or shows it. The
// ground is trusted for its facts, not for the length of its text: a detail of several
// megabytes must not push a Land button off the screen or cost the phone time.

extension GroundText {
    /// The longest piece of text from the ground that the screen shows (a detail, a reason, a banner part).
    static let maxServerText = 300
    /// The longest drone id the screen shows. The real id is still what a command carries.
    static let maxIdText = 40

    /// Text that came from the ground, made safe to show: at most `max` characters (the last one is
    /// an ellipsis when the text was cut) and control and bidirectional override characters replaced
    /// by spaces. The text is read only up to `max` characters, so a multi-megabyte string costs
    /// the same as a short one.
    static func shorten(_ text: String?, max: Int = maxServerText) -> String {
        guard let text, max > 0 else { return "" }
        var kept = String.UnicodeScalarView()
        var count = 0
        var wasCut = false
        for scalar in text.unicodeScalars {
            if count == max {
                wasCut = true
                break
            }
            kept.append(isUnsafeForDisplay(scalar) ? " " : scalar)
            count += 1
        }
        if wasCut {
            kept.removeLast()
            kept.append("\u{2026}")
        }
        return String(kept)
    }

    /// A drone id for display. The real id is still what a command carries.
    static func shortId(_ id: String) -> String { shorten(id, max: maxIdText) }

    private static func isUnsafeForDisplay(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.properties.generalCategory {
        case .control, .lineSeparator, .paragraphSeparator: return true
        default: break
        }
        switch scalar.value {
        case 0x202A...0x202E, 0x2066...0x2069: return true
        default: return false
        }
    }
}

/// Cuts the strings of a decoded server event, once, where it enters the view model. Ids that a
/// command or a lookup depends on (the keys of `fleet_status`, the drone id of a manual reply)
/// keep their real value; the places that show them cut them with `GroundText.shortId`.
enum GroundIngest {
    static func sanitized(_ event: GroundProtocol.GroundEvent) -> GroundProtocol.GroundEvent {
        switch event {
        case var .fleetStatus(payload):
            if var drones = payload.drones {
                for (id, record) in drones where record.droneLinkDetail != nil {
                    var copy = record
                    copy.droneLinkDetail = GroundText.shorten(record.droneLinkDetail)
                    drones[id] = copy
                }
                payload.drones = drones
            }
            return .fleetStatus(payload)
        case let .fleetEvent(ev):
            return .fleetEvent(sanitized(ev))
        case let .manualAck(reply):
            return .manualAck(sanitized(reply))
        case let .manualResult(reply):
            return .manualResult(sanitized(reply))
        case var .error(payload):
            payload.message = payload.message.map { GroundText.shorten($0) }
            return .error(payload)
        case var .warning(payload):
            payload.message = payload.message.map { GroundText.shorten($0) }
            return .warning(payload)
        default:
            return event
        }
    }

    static func sanitized(_ event: FleetEvent) -> FleetEvent {
        var copy = event
        copy.event = event.event.map(kind)
        copy.type = event.type.map(kind)
        copy.kind = event.kind.map(kind)
        copy.detail = event.detail.map { GroundText.shorten($0) }
        copy.droneId = event.droneId.map { GroundText.shortId($0) }
        copy.droneIds = event.droneIds.map { Array($0.prefix(100)).map { GroundText.shortId($0) } }
        return copy
    }

    static func sanitized(_ reply: ManualReply) -> ManualReply {
        var copy = reply
        copy.detail = reply.detail.map { GroundText.shorten($0) }
        if case let .unknown(raw)? = reply.reason { copy.reason = .unknown(GroundText.shorten(raw, max: 60)) }
        if case let .unknown(raw)? = reply.action { copy.action = .unknown(GroundText.shorten(raw, max: 40)) }
        return copy
    }

    private static func kind(_ kind: FleetEventKind) -> FleetEventKind {
        if case let .unknown(raw) = kind { return .unknown(GroundText.shorten(raw)) }
        return kind
    }
}
