import GroundProtocol
import SwiftUI

// Match alerts the ground reports, read only. The phone shows what the ground found; the operator
// answers on the laptop (`match_alert_response` is not a command the phone can send). Alerts are
// plain list rows in the Ground tab, never a sheet or an overlay, so nothing covers the pinned
// LAND ALL bar or a drone's Land button.

struct GroundAlertRow: Equatable, Identifiable {
    /// The ground's alert id, or a position-based id for an alert without one.
    let id: String
    /// "Possible match", or the ground's own level text, shortened.
    let levelText: String
    /// "Track 8_7", or nil when the ground named none.
    let trackText: String?
    let timeText: String
    /// "Score 86% (ReID 86%, face 0%)".
    let scoreText: String
    let reasonText: String?
    /// True for a `match_alert_reminder` that the ground has not since reported as answered.
    let isReminder: Bool
    /// How the operator answered on the laptop, or nil while the ground has not said it was answered.
    let answer: MatchAlertAction?

    var accessibilitySummary: String {
        var parts = [levelText]
        if let trackText { parts.append(trackText) }
        parts.append("at " + timeText)
        parts.append(scoreText)
        if let reasonText { parts.append(reasonText) }
        if isReminder { parts.append(GroundAlertsState.reminderTag(time: timeText)) }
        if let answer { parts.append(GroundAlertsState.answerTag(answer)) }
        return parts.joined(separator: ", ")
    }
}

struct GroundAlertsState: Equatable {
    static let maxAlerts = 100
    static let note = "Reported by the ground. Answer match alerts on the laptop."
    /// The ground sends a reminder while an alert waits and a separate event when the operator
    /// answers it. The reminder tag says what the ground said, and when; the answer tag replaces it
    /// once the ground reports the answer.
    static func reminderTag(time: String) -> String {
        "Reminder at \(time): the ground said this alert was waiting for the operator."
    }

    static func answerTag(_ answer: MatchAlertAction) -> String {
        switch answer {
        case .confirm: return "Answered on the laptop: confirmed."
        case .dismiss: return "Answered on the laptop: dismissed."
        case .unknown: return "Answered on the laptop."
        }
    }

    let rows: [GroundAlertRow]
    let notLiveNote: String?

    /// Newest first, at most `maxAlerts`. `alerts` is oldest first, as the view model keeps it.
    /// `answers` holds the operator's answers by alert id (`GroundViewModel.alertAnswers`).
    init(alerts: [MatchAlert], isStale: Bool, answers: [String: MatchAlertAction] = [:]) {
        var out: [GroundAlertRow] = []
        for (offset, alert) in alerts.suffix(Self.maxAlerts).reversed().enumerated() {
            out.append(Self.row(alert, fallbackID: "alert-\(offset)", answer: alert.alertId.flatMap { answers[$0] }))
        }
        rows = out
        notLiveNote = MissionFormat.notLiveNote(isStale: isStale)
    }

    static func level(_ level: AlertLevel?) -> String {
        switch level {
        case .possibleMatch?: return "Possible match"
        case let .unknown(raw)?: return GroundText.shorten(raw, max: 40)
        case nil: return "Match alert"
        }
    }

    /// A score from 0 to 1 as a percent; "--" for a value that is absent or not a number.
    static func percent(_ v: Double?) -> String {
        guard let v, v.isFinite else { return "--" }
        return "\(Int((min(max(v, 0), 1) * 100).rounded()))%"
    }

    static func row(_ a: MatchAlert, fallbackID: String, answer: MatchAlertAction? = nil) -> GroundAlertRow {
        let id = a.alertId.map { GroundText.shortId($0) }.flatMap { $0.isEmpty ? nil : $0 } ?? fallbackID
        let track = a.trackId.flatMap { $0.isEmpty ? nil : "Track " + GroundText.shortId($0) }
        let time = a.time.flatMap { $0.isEmpty ? nil : GroundText.shorten($0, max: 40) } ?? "--"
        let reason = a.reasoning.flatMap { $0.isEmpty ? nil : GroundText.shorten($0) }
        return GroundAlertRow(
            id: id,
            levelText: level(a.alertLevel),
            trackText: track,
            timeText: time,
            scoreText: "Score \(percent(a.score)) (ReID \(percent(a.reidScore)), face \(percent(a.faceScore)))",
            reasonText: reason,
            isReminder: a.renotify == true && answer == nil,
            answer: answer
        )
    }
}

struct GroundMatchAlertView: View {
    let viewModel: GroundViewModel

    var body: some View {
        let state = GroundAlertsState(alerts: viewModel.matchAlerts, isStale: viewModel.isStateStale, answers: viewModel.alertAnswers)
        // Nothing to show until the ground reports an alert: no empty card in the tab.
        if !state.rows.isEmpty {
            MissionCardFrame(title: "Match alerts", identifier: "ground.alerts", notLiveNote: state.notLiveNote) {
                Text(GroundAlertsState.note)
                    .font(.footnote)
                    .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                ForEach(state.rows.indices, id: \.self) { index in
                    let row = state.rows[index]
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(row.levelText)
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(MissionPalette.color(MissionPalette.text))
                            Spacer(minLength: 4)
                            Text(row.timeText)
                                .font(.footnote.monospacedDigit())
                                .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                        }
                        if let track = row.trackText {
                            Text(track)
                                .font(.footnote)
                                .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                        }
                        Text(row.scoreText)
                            .font(.footnote)
                            .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        if let reason = row.reasonText {
                            Text(reason)
                                .font(.footnote)
                                .foregroundStyle(MissionPalette.color(MissionPalette.text))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let answer = row.answer {
                            Text(GroundAlertsState.answerTag(answer))
                                .font(.footnote.weight(.semibold))
                                .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                        }
                        if row.isReminder {
                            Text(GroundAlertsState.reminderTag(time: row.timeText))
                                .font(.footnote.weight(.semibold))
                                .foregroundStyle(MissionPalette.color(MissionPalette.warning))
                        }
                    }
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(row.accessibilitySummary)
                    .accessibilityIdentifier("ground.alerts.row.\(index)")
                }
            }
        }
    }
}
