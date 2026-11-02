import GroundProtocol
import SwiftUI

// The reasoning provider the ground uses, read only. The phone cannot switch it and cannot opt
// the ground into cloud use; only the ground's own setting does that. The phone's own on-device
// reasoning settings are separate and live in Settings.

struct ReasoningCardState: Equatable {
    /// "Gemma (gemma4:latest)", or "Reasoning off: matches use ReID and face scores only".
    let statusText: String
    /// "Available: Gemma, Off", or nil when the ground listed none.
    let availableText: String?
    /// "Cloud allowed" or "Cloud not allowed" (also when the ground did not say).
    let cloudBadge: String
    let cloudAllowed: Bool
    /// One "Claude: <reason>" per unavailable provider, as the ground wrote it.
    let unavailableNotes: [String]
    let notLiveNote: String?
}

extension MissionFormat {
    static let maxAvailable = 8
    static let maxUnavailable = 8

    static func providerName(_ name: ReasoningProviderName) -> String {
        switch name {
        case .gemma: return "Gemma"
        case .claude: return "Claude"
        case .off: return "Off"
        case let .unknown(raw): return GroundText.shorten(raw, max: 40)
        }
    }

    /// "Available: Gemma, Off", cut to `maxAvailable` distinct entries and "+N more" before any
    /// text is built, so a flood of entries costs one cheap pass.
    static func availableText(_ available: [ReasoningProviderName]?) -> String? {
        guard let available, !available.isEmpty else { return nil }
        // Raw names are compared before shortening: the pass keeps at most `maxAvailable`
        // entries and counts the rest without building any text for them.
        var seen = Set<String>()
        var shown: [ReasoningProviderName] = []
        for name in available where seen.insert(name.wire).inserted {
            if shown.count < maxAvailable { shown.append(name) }
        }
        let more = seen.count - shown.count
        return "Available: " + shown.map(providerName).joined(separator: ", ") + (more > 0 ? " +\(more) more" : "")
    }

    /// The `count` smallest keys in order, found in one pass without sorting the whole table.
    static func firstKeys(of table: [String: String], count: Int) -> [String] {
        var best: [String] = []
        for key in table.keys {
            if best.count == count, let last = best.last, key >= last { continue }
            let at = best.firstIndex { key < $0 } ?? best.count
            best.insert(key, at: at)
            if best.count > count { best.removeLast() }
        }
        return best
    }

    static func reasoningCard(_ r: ReasoningProviderPayload?, isStale: Bool = false) -> ReasoningCardState? {
        guard let r else { return nil }
        let status: String
        switch r.provider {
        case .off?:
            status = "Reasoning off: matches use ReID and face scores only"
        case nil:
            status = "Provider not reported"
        case let provider?:
            let label = (r.label?.isEmpty == false) ? GroundText.shorten(r.label, max: 80) : providerName(provider)
            let model = (r.model?.isEmpty == false) ? " (\(GroundText.shorten(r.model, max: 80)))" : ""
            status = label + model
        }
        // Keys are cut and sorted before text is built, so 10,000 entries cost one sort of the keys.
        let unavailable = r.unavailable ?? [:]
        let notes = firstKeys(of: unavailable, count: maxUnavailable).map { key -> String in
            let cut = GroundText.shorten(key, max: 40)
            return "\(cut.prefix(1).uppercased() + cut.dropFirst()): \(GroundText.shorten(unavailable[key]))"
        }
        return ReasoningCardState(
            statusText: status,
            availableText: availableText(r.available),
            cloudBadge: r.cloudAllowed == true ? "Cloud allowed" : "Cloud not allowed",
            cloudAllowed: r.cloudAllowed == true,
            unavailableNotes: Array(notes),
            notLiveNote: notLiveNote(isStale: isStale)
        )
    }
}

struct GroundReasoningView: View {
    let viewModel: GroundViewModel

    var body: some View {
        if let card = MissionFormat.reasoningCard(viewModel.reasoningProvider, isStale: viewModel.isStateStale) {
            MissionCardFrame(title: "Reasoning", identifier: "ground.reasoning", notLiveNote: card.notLiveNote) {
                Text(card.statusText)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(MissionPalette.color(MissionPalette.text))
                Text(card.cloudBadge)
                    .font(.footnote.weight(.bold))
                    .foregroundStyle(MissionPalette.color(MissionPalette.text))
                    .accessibilityIdentifier("ground.reasoning.cloud")
                if let available = card.availableText {
                    Text(available)
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.text))
                }
                ForEach(card.unavailableNotes.indices, id: \.self) { index in
                    Text(card.unavailableNotes[index])
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                }
            }
        }
    }
}
