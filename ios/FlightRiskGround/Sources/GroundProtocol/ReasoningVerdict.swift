import Foundation

// The ground's reasoning verdict for a match alert (#185). The ground raises an alert on its
// ReID and face scores first, then asks its reasoning provider (Gemma or Claude) to compare the
// person's crop with the reference photo. The answer arrives later as two events, both keyed by
// `alert_id` (`flightrisk/dashboard/alerts.py`, `resolve_reasoning` and `_gemma_worker`):
//
// - `reasoning_result`: `{track_id, alert_id, type: "analyze", result: {match, confidence, reasoning}}`.
//   `confidence` is `high`, `medium` or `low` for a verdict, or `error`, `skipped`,
//   `unavailable`, `off` or `unknown` when there is none.
// - `alert_upgrade`: `{track_id, alert_id, new_level, reasoning}`, sent when the verdict
//   raised the alert's level. A level only ever goes up, and the phone, like the dashboard
//   (`ui.js`, `alert_upgrade`), ignores an upgrade that does not rank higher than the level shown.
//
// The dashboard handles both in `ui.js` (`reasoning_result`, `alert_upgrade`). A reminder or a
// reconnect carries the latest verdict in the alert itself (`gemma_match`, `gemma_confidence`,
// `reasoning`), because the ground updates its stored copy of the alert.

// MARK: - Payloads

/// The `result` object of a `reasoning_result`.
public struct ReasoningResultBody: Codable, Equatable, Sendable {
    @Lenient public var match: Bool?
    @Lenient public var confidence: String?
    @Lenient public var reasoning: String?

    public init(match: Bool? = nil, confidence: String? = nil, reasoning: String? = nil) {
        self.match = match
        self.confidence = confidence
        self.reasoning = reasoning
    }
}

/// `reasoning_result`. Only `type == "analyze"` with an `alert_id` belongs to a match alert; a
/// `description` result (text-description matching) has no alert and is ignored, as the
/// dashboard ignores it.
public struct ReasoningResultPayload: Codable, Equatable, Sendable {
    @Lenient public var trackId: String?
    @Lenient public var alertId: String?
    @Lenient public var type: String?
    @Lenient public var result: ReasoningResultBody?

    enum CodingKeys: String, CodingKey {
        case type, result
        case trackId = "track_id", alertId = "alert_id"
    }

    /// True when this result is the verdict for one match alert.
    public var isAlertVerdict: Bool {
        type == "analyze" && !(alertId ?? "").isEmpty && result != nil
    }
}

/// `alert_upgrade`: the verdict raised an alert's level.
public struct AlertUpgradePayload: Codable, Equatable, Sendable {
    @Lenient public var trackId: String?
    @Lenient public var alertId: String?
    @Lenient public var newLevel: AlertLevel?
    @Lenient public var reasoning: String?

    enum CodingKeys: String, CodingKey {
        case reasoning
        case trackId = "track_id", alertId = "alert_id", newLevel = "new_level"
    }
}

// MARK: - Verdict

/// One alert's reasoning verdict as the phone knows it.
public struct AlertVerdict: Equatable, Sendable {
    /// The provider's yes or no, or nil when it did not say.
    public var match: Bool?
    /// The raw `confidence` word, or nil.
    public var confidence: String?
    /// The provider's explanation, already cut to `AlertVerdictText.maxStoredRationale`.
    public var rationale: String?

    public init(match: Bool?, confidence: String?, rationale: String?) {
        self.match = match
        self.confidence = confidence
        self.rationale = rationale.map { AlertVerdictText.clip($0, max: AlertVerdictText.maxStoredRationale) }
    }

    /// The verdict a `match_alert` or reminder carries, or nil when it carries none. A
    /// `gemma_confidence` of null means the ground ran no reasoning for this alert.
    public init?(alert: MatchAlert) {
        guard case let .string(confidence)? = alert.gemmaConfidence, !confidence.isEmpty else { return nil }
        var match: Bool?
        if case let .bool(b)? = alert.gemmaMatch { match = b }
        self.init(match: match, confidence: confidence, rationale: alert.reasoning)
    }

    /// What kind of answer this is.
    public var kind: Kind {
        switch confidence {
        case "pending"?: return .pending
        case "error"?, "skipped"?, "unavailable"?, "off"?, "unknown"?: return .noVerdict
        default: return match == nil ? .noVerdict : .verdict
        }
    }

    public enum Kind: Equatable, Sendable {
        /// The ground is still waiting for the provider.
        case pending
        /// The provider answered yes or no.
        case verdict
        /// No verdict will come (error, skipped, not run, reasoning off, no clear answer).
        case noVerdict
    }

    /// True for a final answer (anything but pending).
    public var isFinal: Bool { kind != .pending }
}

/// Text for a verdict, for the alert row and its detail. Pure; the views only lay it out.
public enum AlertVerdictText {
    /// The longest rationale kept from the ground.
    public static let maxStoredRationale = 600
    /// The rationale on the alert row.
    public static let maxRowRationale = 140
    /// The rationale in the row's detail.
    public static let maxDetailRationale = 600

    public struct Display: Equatable, Sendable {
        /// "AI check: match (high confidence)" or a neutral note.
        public let headline: String
        /// "high", or nil when the answer has no confidence (a neutral note, pending).
        public let confidence: String?
        /// The short rationale for the row, or nil.
        public let rowRationale: String?
        /// The longer rationale for the detail, or nil.
        public let detailRationale: String?
        /// True for a neutral note (error, skipped, not run, off, pending): no verdict to act on.
        public let isNeutral: Bool
    }

    public static let pendingNote = "AI check pending: the ground is waiting for its reasoning provider."
    public static let errorNote = "AI check failed. The alert stands on its match scores."
    public static let skippedNote = "AI check skipped: the alert was answered before it ran."
    public static let notRunNote = "AI check not run. The alert stands on its match scores."
    public static let offNote = "AI check off on the ground. The alert stands on its match scores."
    public static let noClearVerdictNote = "AI check gave no clear verdict. The alert stands on its match scores."

    public static func display(_ v: AlertVerdict) -> Display {
        func rationale(_ max: Int) -> String? {
            guard let r = v.rationale?.trimmingCharacters(in: .whitespacesAndNewlines), !r.isEmpty else { return nil }
            return clip(r, max: max)
        }
        switch v.confidence {
        case "pending"?:
            return Display(headline: pendingNote, confidence: nil, rowRationale: nil, detailRationale: nil, isNeutral: true)
        case "error"?:
            // The ground's text for an error can be an exception message: it is not shown.
            return Display(headline: errorNote, confidence: nil, rowRationale: nil, detailRationale: nil, isNeutral: true)
        case "skipped"?:
            return Display(headline: skippedNote, confidence: nil, rowRationale: nil, detailRationale: nil, isNeutral: true)
        case "unavailable"?:
            // The ground says why ("AI check not run (queue full)."): kept for the detail only.
            return Display(headline: notRunNote, confidence: nil, rowRationale: nil, detailRationale: rationale(maxDetailRationale), isNeutral: true)
        case "off"?:
            return Display(headline: offNote, confidence: nil, rowRationale: nil, detailRationale: nil, isNeutral: true)
        default:
            break
        }
        guard let match = v.match, v.confidence != "unknown" else {
            return Display(headline: noClearVerdictNote, confidence: nil, rowRationale: nil, detailRationale: rationale(maxDetailRationale), isNeutral: true)
        }
        let answer = match ? "match" : "no match"
        let word = v.confidence.map { clip($0, max: 20) }.flatMap { $0.isEmpty ? nil : $0 }
        let headline = "AI check: " + answer + (word.map { " (\($0) confidence)" } ?? "")
        return Display(headline: headline, confidence: word, rowRationale: rationale(maxRowRationale),
                       detailRationale: rationale(maxDetailRationale), isNeutral: false)
    }

    /// At most `max` characters (the last an ellipsis when cut), counted and cut as grapheme
    /// clusters, so an accented letter or an emoji is never split. A character holding a control,
    /// line or paragraph separator or bidirectional override or isolate becomes one space; the
    /// invisible direction marks (U+200E, U+200F, U+061C) are removed. Reads the text only up to
    /// `max` characters.
    public static func clip(_ text: String, max: Int) -> String {
        guard max > 0 else { return "" }
        var kept: [String] = []
        var wasCut = false
        for character in text {
            var scalars = String.UnicodeScalarView()
            scalars.append(contentsOf: character.unicodeScalars.filter { !isDirectionMark($0) })
            if scalars.isEmpty { continue }
            if kept.count == max {
                wasCut = true
                break
            }
            kept.append(scalars.contains(where: isUnsafe) ? " " : String(scalars))
        }
        if wasCut {
            kept.removeLast()
            kept.append("\u{2026}")
        }
        return kept.joined()
    }

    /// Left-to-right, right-to-left and Arabic letter marks: invisible, and able to reorder the
    /// text around them.
    private static func isDirectionMark(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value == 0x200E || scalar.value == 0x200F || scalar.value == 0x061C
    }

    private static func isUnsafe(_ scalar: Unicode.Scalar) -> Bool {
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

// MARK: - Book

/// The verdicts and raised levels of the match alerts the phone has seen, by `alert_id`.
///
/// Feed it every `match_alert`, `match_alert_reminder`, `reasoning_result` and `alert_upgrade`
/// in arrival order. Rules (the dashboard's, `ui.js`):
/// - A verdict or upgrade for an alert id the phone has not seen in a `match_alert` or reminder
///   changes nothing.
/// - `reasoning_result` counts only with `type == "analyze"`, an `alert_id` and a `result`.
/// - A level only goes up: `alert_upgrade` applies only when it ranks higher, and a later
///   reminder with a lower level does not lower it. An upgrade that does not rank higher changes
///   nothing at all, its verdict included.
/// - An upgrade stands in for a verdict only when it raised the alert to `possible_match` or
///   `confirmed_match` and no `reasoning_result` gave one.
/// - A final verdict is never replaced by "pending" from a re-sent alert.
/// - An `alert_id` longer than `maxAlertIdLength` is ignored. At most `maxAlerts` ids are kept.
public struct AlertVerdictBook: Equatable, Sendable {
    public static let maxAlerts = 100
    public static let maxAlertIdLength = 128

    private var verdicts: [String: AlertVerdict] = [:]
    private var levels: [String: AlertLevel] = [:]
    private var order: [String] = []

    public init() {}

    public mutating func apply(_ event: GroundEvent) {
        switch event {
        case let .matchAlert(a), let .matchAlertReminder(a):
            guard let id = usableId(a.alertId) else { return }
            remember(id)
            if let level = AlertLevel.raised(levels[id], to: a.alertLevel) { levels[id] = level }
            if let carried = AlertVerdict(alert: a) {
                // A re-sent alert may carry an older copy: it never replaces a final verdict.
                if verdicts[id]?.isFinal != true || carried.isFinal { verdicts[id] = carried }
            }
        case let .reasoningResult(r):
            guard r.isAlertVerdict, let id = usableId(r.alertId), order.contains(id), let body = r.result else { return }
            verdicts[id] = AlertVerdict(match: body.match, confidence: body.confidence, rationale: body.reasoning)
        case let .alertUpgrade(u):
            guard let id = usableId(u.alertId), order.contains(id), let newLevel = u.newLevel else { return }
            // The dashboard's rule (`ui.js`): an upgrade that does not rank higher than the level
            // shown is ignored, so it can neither lower the level nor write a verdict.
            guard newLevel.rank > (levels[id]?.rank ?? 0) else { return }
            levels[id] = newLevel
            // An upgrade to a match level means the provider said yes. Without a
            // `reasoning_result` (an older ground), it is the only verdict the phone gets.
            let confidence: String?
            switch newLevel {
            case .confirmedMatch: confidence = "high"
            case .possibleMatch: confidence = "medium"
            default: return
            }
            if verdicts[id]?.kind != .verdict {
                verdicts[id] = AlertVerdict(match: true, confidence: confidence, rationale: u.reasoning)
            }
        default:
            break
        }
    }

    /// The verdict for an alert, or nil when the ground has reported none.
    public func verdict(for alertId: String?) -> AlertVerdict? {
        guard let id = alertId else { return nil }
        return verdicts[id]
    }

    /// The level to show for `alert`: the highest level seen for its id, never lower than the
    /// alert's own level.
    public func level(for alert: MatchAlert) -> AlertLevel? {
        guard let id = alert.alertId else { return alert.alertLevel }
        return AlertLevel.raised(levels[id], to: alert.alertLevel)
    }

    /// The display text of an alert's verdict, or nil when there is none.
    public func display(for alert: MatchAlert) -> AlertVerdictText.Display? {
        verdict(for: alert.alertId).map(AlertVerdictText.display)
    }

    public var count: Int { order.count }

    private func usableId(_ id: String?) -> String? {
        guard let id, !id.isEmpty, id.count <= Self.maxAlertIdLength else { return nil }
        return id
    }

    private mutating func remember(_ id: String) {
        guard !order.contains(id) else { return }
        order.append(id)
        while order.count > Self.maxAlerts {
            let dropped = order.removeFirst()
            verdicts[dropped] = nil
            levels[dropped] = nil
        }
    }
}
