import GroundProtocol
import SwiftUI

// The ceiling and altitude card of the Ground tab. Read only: the ground decides every value, and
// this card has no edit controls and sends nothing. The wording follows the dashboard's
// ceiling_settings.html and the Android CeilingCard.

struct CeilingDroneRow: Equatable {
    let id: String
    let displayId: String
    /// "Slot 100 cm", or "Slot 120 cm (own; default 130 cm)".
    let slotText: String
    let problem: String?
    let edgeText: String
    let edgeSeverity: MissionSeverity
}

struct CeilingCardState: Equatable {
    let headline: String
    let maxSlotText: String
    /// "Locked in flight: <reason>", or nil.
    let lockedText: String?
    let loadErrorText: String?
    let drones: [CeilingDroneRow]
    let partial: Bool
    /// Set while the ground state is not live.
    let notLiveNote: String?
}

extension MissionFormat {
    /// The edge sync line of one drone (`ceilingEdgeText` in the dashboard's ceiling_settings.html).
    static func ceilingEdgeText(_ d: CeilingDrone) -> (text: String, severity: MissionSeverity) {
        guard let e = d.edge, e.supported == true else {
            return ("Edge does not report a ceiling (older edge): it flies the planned slot only.", .info)
        }
        if e.pending == true { return ("Sending to the edge...", .info) }
        if let r = e.refusal {
            let why = (r.detail?.isEmpty == false ? r.detail : nil) ?? r.reason?.wire ?? ""
            return ("Edge refused: \(GroundText.shorten(why))", .warn)
        }
        if let note = e.note, !note.isEmpty {
            return ("Edge: \(GroundText.shorten(note).replacingOccurrences(of: "_", with: " "))", .warn)
        }
        let s = "Edge: ceiling \(GroundText.number(e.ceilingCm)), margin \(GroundText.number(e.marginCm)), slot \(GroundText.number(e.slotCm)) cm"
        return e.inSync == true ? (s, .info) : (s + " (not yet applied)", .warn)
    }

    /// The read-only ceiling card, or nil when the ground has no ceiling board (hide the section).
    static func ceilingCard(_ c: CeilingSettingsPayload?, isStale: Bool = false) -> CeilingCardState? {
        guard let c, c.available != false else { return nil }
        let all = c.drones ?? []
        let rows = all.prefix(maxDrones).map { d -> CeilingDroneRow in
            let id = d.droneId ?? ""
            var slot = "Slot \(GroundText.number(d.slotCm)) cm"
            if d.ownSlotCm != nil { slot += " (own; default \(GroundText.number(d.defaultSlotCm)) cm)" }
            let edge = ceilingEdgeText(d)
            return CeilingDroneRow(
                id: id,
                displayId: id.isEmpty ? "--" : GroundText.shortId(id),
                slotText: slot,
                problem: (d.problem?.isEmpty == false) ? GroundText.shorten(d.problem) : nil,
                edgeText: edge.text,
                edgeSeverity: edge.severity
            )
        }
        var locked: String?
        if c.editable == false {
            let why = (c.lockedReason?.isEmpty == false) ? GroundText.shorten(c.lockedReason) : "the ground gave no reason."
            locked = "Locked in flight: " + why
        }
        var loadError: String?
        if let e = c.loadError, !e.isEmpty {
            loadError = "Saved settings could not be read (\(GroundText.shorten(e))). Plan and Start are refused until valid settings are saved on the ground."
        }
        return CeilingCardState(
            headline: "Ceiling \(GroundText.number(c.ceilingCm)) cm, margin \(GroundText.number(c.marginCm)) cm",
            maxSlotText: "Highest allowed slot: \(GroundText.number(c.maxSlotCm)) cm (ceiling minus margin)",
            lockedText: locked,
            loadErrorText: loadError,
            drones: Array(rows),
            partial: all.count > maxDrones,
            notLiveNote: notLiveNote(isStale: isStale)
        )
    }
}

struct CeilingView: View {
    let viewModel: GroundViewModel

    var body: some View {
        if let card = MissionFormat.ceilingCard(viewModel.ceilingSettings, isStale: viewModel.isStateStale) {
            MissionCardFrame(title: "Ceiling and altitude", identifier: "ground.ceiling", notLiveNote: card.notLiveNote) {
                Text(card.headline)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(MissionPalette.color(MissionPalette.text))
                Text(card.maxSlotText)
                    .font(.footnote)
                    .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                if let locked = card.lockedText {
                    Text(locked)
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        .accessibilityIdentifier("ground.ceiling.locked")
                }
                if let error = card.loadErrorText {
                    MissionStatusLine(text: error, severity: .bad)
                        .accessibilityIdentifier("ground.ceiling.loadError")
                }
                ForEach(card.drones.indices, id: \.self) { index in
                    let row = card.drones[index]
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(row.displayId): \(row.slotText)")
                            .font(.subheadline)
                            .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        if let problem = row.problem {
                            MissionStatusLine(text: problem, severity: .bad)
                        }
                        MissionStatusLine(text: row.edgeText, severity: row.edgeSeverity)
                    }
                    .accessibilityIdentifier("ground.ceiling.drone.\(index)")
                }
                if card.partial {
                    Text("Only part of the list is shown.")
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                }
            }
        }
    }
}
