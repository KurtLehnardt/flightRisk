import GroundProtocol
import SwiftUI

// The detection-profile card of the Ground tab (view and select), plus the text and state
// builders the other mission cards (ceiling, reasoning) share. The wording follows the dashboard
// (`profileStatus` in flightrisk/dashboard/static/ui/ui.js) and the Android ProfilesCard. Every
// string that came from the ground goes through `GroundText.shorten`, and every list from the
// ground is cut to a fixed size, before any text is built.

/// How serious a status line is.
typealias MissionSeverity = GroundText.Severity

/// One choice in a profile selector. `value` is the wire name; the empty string means "follow the
/// fleet default" (per drone) or "no default" (fleet).
struct ProfileOption: Equatable {
    let value: String
    let label: String
    let selected: Bool
}

struct DroneProfileRow: Equatable, Identifiable {
    let id: String
    let displayId: String
    /// The status line under the selector (the dashboard's `profileStatus`).
    let statusText: String
    let severity: MissionSeverity
    let supported: Bool
    /// Empty for a drone whose edge does not report profiles.
    let options: [ProfileOption]
    /// True when this drone's selector may be used: supported, nothing pending, card selectable.
    let canSelect: Bool
}

struct ProfilesCardState: Equatable {
    let fleetDefaultText: String
    let fleetOptions: [ProfileOption]
    /// Why no selector can be used right now, or nil when selectors are live.
    let blockedNote: String?
    /// True when the ground itself has profile changes turned off (standby ground): selectors are
    /// hidden and only the ground's note shows.
    let controlsOff: Bool
    let canSelect: Bool
    let drones: [DroneProfileRow]
    /// True when the list of profiles or drones was cut by a display cap.
    let partial: Bool
}

/// The result of the last profile selection, shown under the card.
struct ProfileFeedback: Equatable {
    var attempt: Int
    var text: String
    var isFailure: Bool
    /// True while the phone waits for the ground's `detection_profiles` answer.
    var awaitsAnswer: Bool
    var startedAt: Date
}

enum MissionFormat {
    static let notSupported = "Not supported"
    static let followFleetDefault = "Follow fleet default"
    static let noFleetDefault = "Keep each drone's startup profile"
    static let maxProfiles = 24
    static let maxDrones = 64
    static let notConnectedNote = "Not connected to the ground: profile changes are off."
    static let notLiveNote = "Not live: profile changes are off until the ground sends a fresh status."
    static let controlsOffNote = "The ground has detection profile changes turned off."
    static let unsupportedEdge = "This edge does not report detection profiles (older edge); it keeps its own model."
    static let detectorFailing = "DETECTOR FAILING: the edge cannot process frames. "
    /// Shown on the read-only cards while the state is not live.
    static let notLiveValues = "Not live: last values from the ground."

    static func notLiveNote(isStale: Bool) -> String? { isStale ? notLiveValues : nil }

    // MARK: Selection texts

    static let whyPending = "a switch for this drone is still waiting for the edge."
    static let whyNotConnected = "this phone is not connected to the ground."
    static let whySendFailed = "the connection failed while sending."
    static let whyNoUsableID = "this drone has no usable id."
    static let whyUnknownProfile = "the ground does not list that profile."
    static let whyUnsupported = "this drone's edge does not report detection profiles."
    static let whyNoBoard = "the ground has no detection profile board."
    static let whyUnknownDrone = "the ground does not list this drone."
    static let whyLinkLost = "the connection was lost before the ground answered. Check the active profile."
    static let profileNoAnswer = "Profile change: no answer from the ground yet. Check the active profile."

    static func profileNotSent(target: String?, why: String) -> String {
        if let target { return "Profile for \(GroundText.shortId(target)) was NOT changed: \(why)" }
        return "Fleet default profile was NOT changed: \(why)"
    }

    static func profileSent(target: String?, label: String) -> String {
        let head = target.map { "Profile change sent for \(GroundText.shortId($0)): \(label)." }
            ?? "Fleet default profile change sent: \(label)."
        return head + " Waiting for the ground's answer."
    }

    // MARK: Detection profiles

    /// The dashboard's `profileLabel`: the profile's label, else its name, else `--`.
    static func profileLabel(_ profiles: DetectionProfilesPayload, _ name: String?) -> String {
        if let p = profiles.profiles?.first(where: { $0.name == name }), let l = p.label {
            return GroundText.shorten(l, max: 80)
        }
        if let n = name, !n.isEmpty { return GroundText.shorten(n, max: 80) }
        return "--"
    }

    /// The status line under a drone's selector: the dashboard's `profileStatus`. When
    /// `detector_ok` is false it puts `detectorFailing` in front of the status and marks it bad.
    static func profileStatus(_ d: DroneProfileState, _ profiles: DetectionProfilesPayload) -> (text: String, severity: MissionSeverity) {
        let core = profileStatusCore(d, profiles)
        if d.detectorOk == false { return (detectorFailing + core.text, .bad) }
        return core
    }

    /// The dashboard's `profileStatusCore`.
    static func profileStatusCore(_ d: DroneProfileState, _ profiles: DetectionProfilesPayload) -> (text: String, severity: MissionSeverity) {
        func label(_ n: String?) -> String { profileLabel(profiles, n) }
        if d.supported != true { return (unsupportedEdge, .warn) }
        if d.pending == true { return ("Switching to \(label(d.requested))...", .info) }
        if let r = d.refusal {
            let reason = r.reason?.wire ?? ""
            let detail = GroundText.shorten(r.detail)
            if d.mismatch == true, reason == "in_flight" {
                return ("MISMATCH: chosen \(label(d.desired)), edge runs \(label(d.active)). Refused in flight; sent again when the drone is on the ground.", .bad)
            }
            let why = reason == "in_flight" ? "refused in flight" : "refused (\(GroundText.shorten(reason, max: 60)))"
            return ("Active: \(label(d.active)). Switch \(why): \(detail)", .bad)
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
            let code = d.fallbackReason?.wire
            let why: String
            if code == "weights_missing" {
                why = "weights missing on the edge"
            } else {
                let raw = (code?.isEmpty == false ? code : nil) ?? "unknown"
                why = "could not be used (\(GroundText.shorten(raw, max: 60)))"
            }
            return ("Active: \(label(d.active)) (fallback: \(asked) \(why)).", .warn)
        }
        let air = d.flying == true ? " In flight: profile changes will be refused." : ""
        if d.mismatch == true {
            return ("MISMATCH: chosen \(label(d.desired)), edge runs \(label(d.active)). Sent again when the drone is on the ground or reconnects.\(air)", .warn)
        }
        return ("Active: \(label(d.active)).\(air)", .info)
    }

    /// Why profile selectors cannot be used right now, or nil when they can: not connected, the
    /// state is not live, or the ground says controls are off (`controls_enabled` is not `true`).
    static func profileBlockedReason(_ profiles: DetectionProfilesPayload, isStale: Bool, connected: Bool) -> String? {
        if !connected { return notConnectedNote }
        if isStale { return notLiveNote }
        if profiles.controlsEnabled != true {
            if let note = profiles.controlsNote, !note.isEmpty { return GroundText.shorten(note) }
            return controlsOffNote
        }
        return nil
    }

    /// True when the board exists. A payload that does not say `available` still has a board;
    /// only `available: false` (no edge source) hides the section.
    static func hasBoard(_ profiles: DetectionProfilesPayload?) -> Bool {
        guard let profiles else { return false }
        return profiles.available != false
    }

    /// The card for `profiles`, or nil when the ground has no profile board (hide the section).
    static func profilesCard(_ profiles: DetectionProfilesPayload?, isStale: Bool, connected: Bool) -> ProfilesCardState? {
        guard let profiles, hasBoard(profiles) else { return nil }
        let named = (profiles.profiles ?? []).filter { !($0.name ?? "").isEmpty }
        let shownProfiles = Array(named.prefix(maxProfiles))
        let blocked = profileBlockedReason(profiles, isStale: isStale, connected: connected)
        func options(current: String?, noneLabel: String) -> [ProfileOption] {
            [ProfileOption(value: "", label: noneLabel, selected: (current ?? "").isEmpty)]
                + shownProfiles.map { p in
                    ProfileOption(value: p.name ?? "", label: profileLabel(profiles, p.name), selected: p.name == current)
                }
        }
        let allDrones = profiles.drones ?? [:]
        // A dictionary has no order; sort so the list does not reshuffle between updates.
        let ids = Array(allDrones.keys.sorted().prefix(maxDrones))
        let rows: [DroneProfileRow] = ids.compactMap { id in
            guard let d = allDrones[id] else { return nil }
            let status = profileStatus(d, profiles)
            let supported = d.supported == true
            return DroneProfileRow(
                id: id,
                displayId: GroundText.shortId(id),
                statusText: status.text,
                severity: status.severity,
                supported: supported,
                options: supported ? options(current: d.override, noneLabel: followFleetDefault) : [],
                canSelect: supported && blocked == nil && d.pending != true
            )
        }
        let fleetName = (profiles.fleetDefault ?? "").isEmpty ? nil : profiles.fleetDefault
        return ProfilesCardState(
            fleetDefaultText: "Fleet default: " + (fleetName.map { profileLabel(profiles, $0) } ?? "none"),
            fleetOptions: options(current: profiles.fleetDefault, noneLabel: noFleetDefault),
            blockedNote: blocked,
            controlsOff: blocked != nil && profiles.controlsEnabled != true,
            canSelect: blocked == nil,
            drones: rows,
            partial: named.count > maxProfiles || allDrones.count > maxDrones
        )
    }
}

/// Colours of the mission cards. Every text pair keeps at least 4.5:1 contrast in both themes
/// on the backgrounds the cards sit on; a test checks each pair. The system's own secondary
/// label colour does not reach that on a white page, so these cards do not use it.
enum MissionPalette {
    typealias RGB = (r: Int, g: Int, b: Int)

    struct Pair {
        let name: String
        let light: RGB
        let dark: RGB
    }

    static let text = Pair(name: "text", light: (0x1A, 0x1C, 0x1E), dark: (0xF2, 0xF2, 0xF7))
    static let secondaryText = Pair(name: "secondary text", light: (0x44, 0x47, 0x4E), dark: (0xC4, 0xC6, 0xD0))
    static let warning = Pair(name: "warning", light: (0xB4, 0x3C, 0x0A), dark: (0xFB, 0x92, 0x3C))
    static let bad = Pair(name: "bad", light: (0xB9, 0x1C, 0x1C), dark: (0xFC, 0xA5, 0xA5))
    /// Text on a selected option, and the option's fill.
    static let selectedText = Pair(name: "selected text", light: (0xFF, 0xFF, 0xFF), dark: (0x0B, 0x12, 0x20))
    static let selectedFill = Pair(name: "selected fill", light: (0x1D, 0x4E, 0xD8), dark: (0x93, 0xC5, 0xFD))

    /// The pages the cards are drawn on: white and the grouped grey in light, black and the
    /// elevated grey in dark. The card tint is a few percent of grey over these.
    static let lightBackgrounds: [RGB] = [(0xFF, 0xFF, 0xFF), (0xF2, 0xF2, 0xF7)]
    static let darkBackgrounds: [RGB] = [(0x00, 0x00, 0x00), (0x1C, 0x1C, 0x1E)]

    /// Every (foreground, background) pair the cards draw text with, per theme.
    static var textPairs: [(name: String, dark: Bool, foreground: RGB, background: RGB)] {
        var out: [(String, Bool, RGB, RGB)] = []
        for p in [text, secondaryText, warning, bad] {
            for bg in lightBackgrounds { out.append((p.name + " light", false, p.light, bg)) }
            for bg in darkBackgrounds { out.append((p.name + " dark", true, p.dark, bg)) }
        }
        out.append(("selected light", false, selectedText.light, selectedFill.light))
        out.append(("selected dark", true, selectedText.dark, selectedFill.dark))
        return out
    }

    static func color(_ pair: Pair) -> Color {
        Color(UIColor { trait in
            let c = trait.userInterfaceStyle == .dark ? pair.dark : pair.light
            return UIColor(red: CGFloat(c.r) / 255, green: CGFloat(c.g) / 255, blue: CGFloat(c.b) / 255, alpha: 1)
        })
    }

    static func color(for severity: MissionSeverity) -> Color {
        switch severity {
        case .info: return color(secondaryText)
        case .warn: return color(warning)
        case .bad: return color(bad)
        }
    }
}

/// The frame every mission card shares: a titled, rounded block with an optional not-live line.
struct MissionCardFrame<Content: View>: View {
    let title: String
    let identifier: String
    var notLiveNote: String?
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.headline)
                .foregroundStyle(MissionPalette.color(MissionPalette.text))
                .accessibilityAddTraits(.isHeader)
            if let notLiveNote {
                Text(notLiveNote)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(MissionPalette.color(MissionPalette.text))
                    .accessibilityIdentifier(identifier + ".notLive")
            }
            content()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifier)
    }
}

/// A status line: plain for information, coloured and bold for warnings and failures.
struct MissionStatusLine: View {
    let text: String
    let severity: MissionSeverity

    var body: some View {
        Text(text)
            .font(.footnote.weight(severity == .bad ? .bold : .regular))
            .foregroundStyle(MissionPalette.color(for: severity))
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Detection profiles: the fleet default and each drone's choice. Selecting sends exactly one
/// command to the ground, through the view model; the ground's answer (switching, active,
/// fallback, refusal) shows in the status line under each drone.
struct DetectionProfilesView: View {
    let viewModel: GroundViewModel

    var body: some View {
        if let card = MissionFormat.profilesCard(
            viewModel.detectionProfiles, isStale: viewModel.isStateStale, connected: viewModel.linkStatus.isConnected
        ) {
            MissionCardFrame(title: "Detection profiles", identifier: "ground.profiles") {
                if let note = card.blockedNote {
                    Text(note)
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        .accessibilityIdentifier("ground.profiles.blocked")
                }
                Text(card.fleetDefaultText)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(MissionPalette.color(MissionPalette.text))
                if !card.controlsOff {
                    ProfileOptionRow(options: card.fleetOptions, enabled: card.canSelect, who: "Fleet default", identifier: "ground.profiles.fleet") { value in
                        Task { await viewModel.selectFleetProfile(value) }
                    }
                }
                ForEach(card.drones) { row in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.displayId)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        if !row.supported {
                            Text(MissionFormat.notSupported)
                                .font(.caption.weight(.bold))
                                .foregroundStyle(MissionPalette.color(MissionPalette.text))
                        } else if !card.controlsOff {
                            ProfileOptionRow(options: row.options, enabled: row.canSelect, who: row.displayId, identifier: "ground.profiles.drone.\(row.id)") { value in
                                Task { await viewModel.selectDroneProfile(row.id, profile: value) }
                            }
                        }
                        MissionStatusLine(text: row.statusText, severity: row.severity)
                            .accessibilityIdentifier("ground.profiles.status.\(row.id)")
                    }
                }
                if card.partial {
                    Text("Only part of the list is shown.")
                        .font(.footnote)
                        .foregroundStyle(MissionPalette.color(MissionPalette.secondaryText))
                }
                if let feedback = viewModel.profileFeedback {
                    VStack(alignment: .leading, spacing: 4) {
                        MissionStatusLine(text: feedback.text, severity: feedback.isFailure ? .bad : .info)
                            .accessibilityIdentifier("ground.profiles.feedback")
                        if feedback.isFailure {
                            Button("Dismiss") { viewModel.dismissProfileFeedback() }
                                .font(.footnote)
                                .frame(minHeight: 44)
                                .accessibilityIdentifier("ground.profiles.feedbackDismiss")
                        }
                    }
                }
            }
        }
    }
}

private struct ProfileOptionRow: View {
    let options: [ProfileOption]
    let enabled: Bool
    let who: String
    let identifier: String
    let onSelect: (String) -> Void

    var body: some View {
        // One button per choice, wrapped over as many lines as needed.
        MissionWrapLayout(spacing: 8) {
            ForEach(options, id: \.value) { option in
                Button {
                    // Tapping the current choice does nothing.
                    if !option.selected { onSelect(option.value) }
                } label: {
                    Text(option.label)
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(MissionPalette.color(option.selected ? MissionPalette.selectedText : MissionPalette.text))
                        .padding(.horizontal, 12)
                        .frame(minHeight: 44)
                        .background(
                            RoundedRectangle(cornerRadius: 8)
                                .fill(option.selected ? MissionPalette.color(MissionPalette.selectedFill) : Color.clear)
                        )
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(MissionPalette.color(MissionPalette.secondaryText), lineWidth: option.selected ? 0 : 1))
                }
                .buttonStyle(.plain)
                .disabled(!enabled)
                .accessibilityLabel("\(who): \(option.label)" + (option.selected ? ", selected" : ""))
                .accessibilityAddTraits(option.selected ? .isSelected : [])
                .accessibilityIdentifier("\(identifier).\(option.value.isEmpty ? "none" : option.value)")
            }
        }
    }
}

/// Lays children out left to right and wraps to the next line when a row is full.
struct MissionWrapLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrange(width: bounds.width, subviews: subviews)
        for (index, origin) in result.origins.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = []
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var maxX: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            origins.append(CGPoint(x: x, y: y))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxX = max(maxX, x - spacing)
        }
        return (CGSize(width: maxX, height: y + rowHeight), origins)
    }
}
